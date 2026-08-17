"""Print a raw product record.

The probe answers questions I thought to ask. This answers the one I could
not: what does a product record on *this* account actually look like?

It exists because the bill-of-materials lookup failed at every candidate
endpoint, which leaves two very different possibilities:

  * BOM data rides along on the product record, under a key I have not
    guessed — in which case the design barely changes; or
  * the pack SKU and the base SKU have no link recorded in Cin7 at all, and
    the mapping has to come from somewhere else entirely.

Only looking at a real record can tell those apart, and the difference
decides the shape of the rest of the build.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from . import schema
from .client import Cin7Client

#: Key fragments worth calling out — anything that might carry a link between
#: a pack SKU and the base units it contains.
INTERESTING_FRAGMENTS = (
    "bom",
    "billofmaterial",
    "component",
    "uom",
    "unitofmeasure",
    "measure",
    "assembl",
    "pack",
    "family",
    "parent",
    "child",
    "conversion",
    "child",
)


def find_product(
    client: Cin7Client, *, sku: Optional[str] = None, product_id: Optional[str] = None
) -> tuple[Optional[dict], list[str]]:
    """Fetch one product, trying the lookups Cin7 might support."""
    notes: list[str] = []

    attempts: list[tuple[str, dict[str, Any]]] = []
    if product_id:
        attempts.append(("ID", {"ID": product_id}))
    if sku:
        attempts.append(("SKU", {"SKU": sku}))
        attempts.append(("Search", {"Search": sku}))
        attempts.append(("Name", {"Name": sku}))

    for label, params in attempts:
        result = client.try_get(schema.ENDPOINT_PRODUCT, page=1, limit=5, **params)
        if not result.ok:
            notes.append(f"lookup by {label}: {result.detail}")
            continue

        records = schema.extract_list(result.payload)
        if not records and isinstance(result.payload, dict):
            # A by-ID fetch may return the object directly rather than a list.
            if schema.get_first(result.payload, "ID", "ProductID"):
                records = [result.payload]

        if records:
            notes.append(f"lookup by {label}: {len(records)} record(s)")
            # Prefer an exact SKU match when the query was fuzzy.
            if sku:
                for record in records:
                    if (schema.as_str(schema.get_first(record, "SKU")) or "").upper() == sku.upper():
                        return record, notes
            return records[0], notes

        notes.append(f"lookup by {label}: no records")

    return None, notes


def interesting_keys(record: dict) -> list[str]:
    """Keys that might carry a pack-to-base-unit relationship."""
    found = []
    for key in record:
        lowered = str(key).lower()
        if any(fragment in lowered for fragment in INTERESTING_FRAGMENTS):
            found.append(str(key))
    return sorted(set(found))


def render(record: dict, notes: list[str], *, keys_only: bool) -> str:
    out: list[str] = ["", "=" * 78, "PRODUCT RECORD", "=" * 78, ""]

    for note in notes:
        out.append(f"  {note}")
    out.append("")

    sku = schema.as_str(schema.get_first(record, "SKU")) or "(no SKU)"
    name = schema.as_str(schema.get_first(record, "Name")) or ""
    out.append(f"  SKU:  {sku}")
    out.append(f"  Name: {name}")
    out.append(f"  ID:   {schema.as_str(schema.get_first(record, 'ID'))}")
    out.append("")

    flagged = interesting_keys(record)
    out.append("  Keys that might link a pack to its base units:")
    if flagged:
        for key in flagged:
            value = record.get(key)
            rendered = json.dumps(value, default=str)
            if len(rendered) > 300:
                rendered = rendered[:300] + " …(truncated)"
            out.append(f"    {key} = {rendered}")
    else:
        out.append("    NONE FOUND — no BOM, component, UOM or assembly key present.")
        out.append("    If this is a pack SKU, Cin7 is not recording what it")
        out.append("    contains, and the mapping must come from elsewhere.")
    out.append("")

    out.append("  All keys on this record:")
    for key in sorted(record):
        out.append(f"    {key}")
    out.append("")

    if not keys_only:
        out.append("-" * 78)
        out.append("FULL RECORD")
        out.append("-" * 78)
        out.append(json.dumps(record, indent=2, default=str, sort_keys=True))
        out.append("")
        out.append(
            "NOTE: this includes whatever the record holds, which may cover "
            "cost prices and supplier terms.\nTrim anything you would rather "
            "not share before pasting it anywhere."
        )
        out.append("")

    return "\n".join(out)
