"""Command line interface.

Three commands, in the order they should be used:

    probe    read-only; answers the gating questions against your account
    plan     read-only; full calculation, prints the run report, writes nothing
    apply    creates and updates draft purchase orders

Safety posture, deliberately awkward in the right places:

* ``plan`` is the default. ``apply`` needs an explicit flag.
* ``apply`` refuses to run without a supplier pin unless ``--no-pin`` is
  passed, so the first live runs cannot touch every supplier at once.
* Nothing here ever authorises a purchase order or emails a supplier.
"""

from __future__ import annotations

import json
import logging
import sys
from dataclasses import replace
from pathlib import Path
from typing import Optional

import typer

from . import schema
from .client import Cin7Client
from .config import Config, ConfigError, Credentials
from .dump import (
    compare_list_and_detail,
    deep_lookup,
    fetch_detail,
    find_product,
    find_products_with_bom,
    render,
    render_deep,
    render_flag_sweep,
    sweep_include_flags,
)
from .pipeline import Pipeline
from .probe import format_findings, run_probe
from .report import render_json, render_markdown

app = typer.Typer(
    add_completion=False,
    help="Reorder automation for Cin7 Core. Creates draft purchase orders; "
    "never authorises or emails them.",
)

DEFAULT_STATE = Path(__file__).resolve().parent.parent / "state" / "fingerprints.json"


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)-8s %(message)s",
        stream=sys.stderr,
    )


def _load(config_path: Optional[Path]) -> tuple[Credentials, Config]:
    try:
        credentials = Credentials.from_env()
        config = Config.load(config_path)
    except ConfigError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)
    return credentials, config


@app.command()
def probe(
    config_path: Optional[Path] = typer.Option(
        None, "--config", help="Path to config.yaml."
    ),
    sample_size: int = typer.Option(5, help="Records to sample per check."),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Check this tool's API assumptions against your live account.

    Read-only. Run this before trusting anything `plan` prints — the field
    names in schema.py were written without access to a live account or to
    Cin7's API documentation.
    """
    _setup_logging(verbose)
    credentials, config = _load(config_path)

    with Cin7Client(credentials, config.api, read_only=True) as client:
        findings = run_probe(client, sample_size=sample_size)
        typer.echo(format_findings(findings))
        typer.echo(f"API calls used: {client.call_count}")

    if any(f.ok is False for f in findings):
        raise typer.Exit(code=1)


@app.command()
def dump(
    sku: Optional[str] = typer.Option(None, "--sku", help="Product SKU to look up."),
    product_id: Optional[str] = typer.Option(
        None, "--id", help="Product ID (GUID) to look up."
    ),
    with_bom: bool = typer.Option(
        False,
        "--with-bom",
        help="Find products that carry a bill of materials, instead of by SKU.",
    ),
    deep: bool = typer.Option(
        False,
        "--deep",
        help="Try every way of retrieving a product's nested collections. Needs --id.",
    ),
    flags: bool = typer.Option(
        False,
        "--flags",
        help="Sweep candidate Include* flags to see which fills which collection. Needs --id.",
    ),
    keys_only: bool = typer.Option(
        False,
        "--keys-only",
        help="Show field names and the interesting values only, not the full record.",
    ),
    config_path: Optional[Path] = typer.Option(None, "--config"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Print product records, to see what Cin7 actually returns.

    Read-only. ``--with-bom`` searches the catalogue for products that carry
    a bill of materials, which is more reliable than guessing a SKU when the
    question is "show me a real pack product".
    """
    if not sku and not product_id and not with_bom:
        typer.secho(
            "Give --sku, --id, or --with-bom.", fg=typer.colors.RED, err=True
        )
        raise typer.Exit(code=2)

    if (deep or flags) and not product_id:
        typer.secho(
            "--deep and --flags need --id.", fg=typer.colors.RED, err=True
        )
        raise typer.Exit(code=2)

    _setup_logging(verbose)
    credentials, config = _load(config_path)

    with Cin7Client(credentials, config.api, read_only=True) as client:
        if flags:
            rows, winners = sweep_include_flags(client, product_id)
            typer.echo(render_flag_sweep(rows, winners))
            typer.echo(f"API calls used: {client.call_count}")
            return

        if deep:
            rows = deep_lookup(client, product_id)
            typer.echo(render_deep(product_id, rows))
            typer.echo(f"API calls used: {client.call_count}")
            return

        if with_bom:
            records, scanned, notes = find_products_with_bom(client)

            if not records:
                typer.secho(
                    f"\nNo product in the catalogue carries a bill of materials "
                    f"({'; '.join(notes)}).\n\n"
                    "That means Cin7 is not recording that a pack contains any "
                    "number of base units — so no API call can supply that "
                    "mapping. The options are to configure Additional Units of "
                    "Measure on your pack products in Cin7, or to supply the "
                    "mapping from a config file.\n",
                    fg=typer.colors.YELLOW,
                )
                raise typer.Exit(code=1)

            typer.echo(
                f"\nFound {len(records)} product(s) with a bill of materials "
                f"({'; '.join(notes)})."
            )
            typer.echo(
                "\nThe list endpoint empties nested collections, so each one is "
                "re-fetched by ID to see the real contents.\n"
            )

            for record in records:
                product_id = schema.as_str(schema.get_first(record, "ID"))
                detail = fetch_detail(client, product_id) if product_id else None

                typer.echo("=" * 78)
                typer.echo(
                    f"{schema.as_str(schema.get_first(record, 'SKU'))} — "
                    f"{schema.as_str(schema.get_first(record, 'Name'))}"
                )
                typer.echo("=" * 78)
                for line in compare_list_and_detail(record, detail):
                    typer.echo(line)
                typer.echo("")

                if detail is not None:
                    components = schema.get_first(
                        detail, *schema.BOM_COMPONENT_KEYS, default=[]
                    )
                    if isinstance(components, list) and components:
                        typer.echo("  Components:")
                        for component in components:
                            typer.echo(f"    {json.dumps(component, default=str)}")
                    else:
                        typer.echo(
                            "  Components: STILL EMPTY after the detail fetch."
                        )
                    typer.echo("")
                    typer.echo(
                        render(detail, ["from detail fetch"], keys_only=keys_only)
                    )

            typer.echo(f"API calls used: {client.call_count}")
            return

        record, notes = find_product(client, sku=sku, product_id=product_id)

        if record is None:
            typer.secho(
                "No product found.\n" + "\n".join(f"  {n}" for n in notes),
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(code=1)

        typer.echo(render(record, notes, keys_only=keys_only))
        typer.echo(f"API calls used: {client.call_count}")


@app.command()
def plan(
    config_path: Optional[Path] = typer.Option(
        None, "--config", help="Path to config.yaml."
    ),
    output: Optional[Path] = typer.Option(
        None, "--output", "-o", help="Write the Markdown report to this file."
    ),
    json_output: Optional[Path] = typer.Option(
        None, "--json", help="Write the JSON report to this file."
    ),
    state_path: Path = typer.Option(DEFAULT_STATE, "--state"),
    supplier: Optional[list[str]] = typer.Option(
        None,
        "--supplier",
        help="Limit to these suppliers (ID or part of the name). Repeatable. "
        "Overrides the config pin and the opt-in attribute.",
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Compute the reorder and print the report. Writes nothing to Cin7."""
    _run(
        config_path=config_path,
        output=output,
        json_output=json_output,
        state_path=state_path,
        verbose=verbose,
        dry_run=True,
        allow_no_pin=True,
        supplier=supplier,
    )


@app.command()
def apply(
    config_path: Optional[Path] = typer.Option(
        None, "--config", help="Path to config.yaml."
    ),
    output: Optional[Path] = typer.Option(
        None, "--output", "-o", help="Write the Markdown report to this file."
    ),
    json_output: Optional[Path] = typer.Option(
        None, "--json", help="Write the JSON report to this file."
    ),
    state_path: Path = typer.Option(DEFAULT_STATE, "--state"),
    supplier: Optional[list[str]] = typer.Option(
        None,
        "--supplier",
        help="Limit to these suppliers (ID or part of the name). Repeatable.",
    ),
    no_pin: bool = typer.Option(
        False,
        "--no-pin",
        help="Allow running against every opted-in supplier, not just pinned ones.",
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Create and update draft purchase orders in Cin7.

    Drafts only. Never authorises, never emails a supplier.
    """
    _run(
        config_path=config_path,
        output=output,
        json_output=json_output,
        state_path=state_path,
        verbose=verbose,
        dry_run=False,
        allow_no_pin=no_pin,
        supplier=supplier,
    )


def _run(
    *,
    config_path: Optional[Path],
    output: Optional[Path],
    json_output: Optional[Path],
    state_path: Path,
    verbose: bool,
    dry_run: bool,
    allow_no_pin: bool,
    supplier: Optional[list[str]] = None,
) -> None:
    _setup_logging(verbose)
    credentials, config = _load(config_path)

    if supplier:
        # An explicit --supplier is a deliberate, visible narrowing, so it
        # replaces the config pin rather than adding to it.
        config = replace(
            config, suppliers=replace(config.suppliers, pin=tuple(supplier))
        )
        typer.echo(f"Limited to supplier(s): {', '.join(supplier)}\n")

    if not dry_run and not config.suppliers.pin and not allow_no_pin:
        typer.secho(
            "Refusing to apply with no supplier pin set.\n\n"
            "Add a supplier ID to `suppliers.pin` in config.yaml so the first "
            "live runs are limited to one supplier, or pass --no-pin if you "
            "genuinely mean every opted-in supplier.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=2)

    with Cin7Client(credentials, config.api, read_only=dry_run) as client:
        pipeline = Pipeline(
            client=client,
            config=config,
            state_path=state_path,
            dry_run=dry_run,
        )
        result = pipeline.run()

    markdown = render_markdown(result, dry_run=dry_run)

    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(markdown, encoding="utf-8")
        typer.echo(f"Wrote report to {output}")
    else:
        typer.echo(markdown)

    if json_output:
        json_output.parent.mkdir(parents=True, exist_ok=True)
        json_output.write_text(render_json(result, dry_run=dry_run), encoding="utf-8")
        typer.echo(f"Wrote JSON to {json_output}")

    if result.aborted:
        # Writing a report and exiting quietly buries the reason. Say it on
        # the terminal, where the person who ran the command is looking.
        typer.secho(
            f"\nRUN ABORTED: {result.aborted}", fg=typer.colors.RED, err=True
        )
        raise typer.Exit(code=1)

    if not result.lines:
        typer.secho(
            "\nNo order lines. That is a real answer if nothing is below its "
            "reorder point — check the skip reasons in the report to be sure.",
            fg=typer.colors.YELLOW,
        )


def main() -> None:
    app()


if __name__ == "__main__":
    main()
