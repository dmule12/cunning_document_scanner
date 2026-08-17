"""Answering the gating questions against a live account.

Run this first. Everything else in this package is written against field
names that could not be verified when it was built, and this command is how
that gets settled.

It is strictly read-only. It creates nothing, changes nothing, and touches no
purchase order.

Each check prints what it actually found and states plainly whether the
assumption in ``schema.py`` holds. Where it doesn't, the constant to change
is named.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from . import schema
from .client import Cin7Client, Cin7Error


@dataclass
class ProbeFinding:
    question: str
    answer: str
    ok: Optional[bool]
    detail: str = ""
    sample_keys: list[str] = field(default_factory=list)
    fix: str = ""


def run_probe(client: Cin7Client, *, sample_size: int = 5) -> list[ProbeFinding]:
    return [
        _probe_connectivity(client),
        _probe_bom_components(client, sample_size),
        _probe_purchase_receipts(client, sample_size),
        _probe_draft_update(client),
        _probe_supplier_attributes(client, sample_size),
        _probe_reorder_parameters(client, sample_size),
    ]


# ---------------------------------------------------------------------------


def _probe_connectivity(client: Cin7Client) -> ProbeFinding:
    try:
        payload = client.get(schema.ENDPOINT_LOCATION, page=1, limit=5)
    except Cin7Error as exc:
        return ProbeFinding(
            question="Can we authenticate?",
            answer="NO",
            ok=False,
            detail=str(exc),
            fix="Check CIN7_ACCOUNT_ID and CIN7_APP_KEY.",
        )

    records = schema.extract_list(payload)
    return ProbeFinding(
        question="Can we authenticate?",
        answer="yes",
        ok=True,
        detail=f"{len(records)} location(s) returned.",
        sample_keys=sorted(records[0].keys()) if records else [],
    )


def _probe_bom_components(client: Cin7Client, sample_size: int) -> ProbeFinding:
    """GATING #1: does GET /BillOfMaterials return components with quantities?"""
    question = "Does GET /BillOfMaterials return components with quantities?"

    try:
        payload = client.get(
            schema.ENDPOINT_BILL_OF_MATERIALS,
            page=1,
            limit=sample_size,
            onlyProductsWithBOM="true",
        )
    except Cin7Error as exc:
        return ProbeFinding(
            question=question,
            answer="ENDPOINT FAILED",
            ok=False,
            detail=str(exc),
            fix=(
                "Without this endpoint there is no way to map a base SKU to "
                "its pack SKU. The whole design depends on it."
            ),
        )

    records = schema.extract_list(payload)
    if not records:
        return ProbeFinding(
            question=question,
            answer="NO PRODUCTS WITH A BOM",
            ok=False,
            detail=(
                "The endpoint responded but returned nothing. Either no "
                "product has a BOM configured, or the onlyProductsWithBOM "
                "filter is not being applied as expected."
            ),
            fix=(
                "Confirm in the Cin7 UI that your boxed products have an "
                "additional unit of measure / BOM set up."
            ),
        )

    sample = records[0]
    parsed = [schema.parse_bill_of_materials(r) for r in records]
    with_components = [p for p in parsed if p and p.components]

    if not with_components:
        return ProbeFinding(
            question=question,
            answer="NO — components not found under the expected keys",
            ok=False,
            detail=(
                f"Top-level keys on a BOM record: {sorted(sample.keys())}. "
                "None of "
                f"{list(schema.BOM_COMPONENT_KEYS)} held a component list."
            ),
            sample_keys=sorted(sample.keys()),
            fix=(
                "Find the key holding the components in the output above and "
                "add it to BOM_COMPONENT_KEYS in schema.py."
            ),
        )

    example = with_components[0]
    first = example.components[0]
    return ProbeFinding(
        question=question,
        answer="YES",
        ok=True,
        detail=(
            f"{len(with_components)}/{len(records)} sampled BOMs parsed. "
            f"Example: parent {example.parent_product_id} contains "
            f"{first.quantity:g} × {first.component_product_id}."
        ),
        sample_keys=sorted(sample.keys()),
    )


def _probe_purchase_receipts(client: Cin7Client, sample_size: int) -> ProbeFinding:
    """GATING #3: does GET /purchase expose per-line received quantities?"""
    question = "Does GET /purchase expose per-line received quantities?"

    try:
        listing = client.get(schema.ENDPOINT_PURCHASE_LIST, page=1, limit=25)
    except Cin7Error as exc:
        return ProbeFinding(
            question=question,
            answer="PURCHASE LIST FAILED",
            ok=False,
            detail=str(exc),
        )

    rows = schema.extract_list(listing)
    if not rows:
        return ProbeFinding(
            question=question,
            answer="NO PURCHASE ORDERS TO INSPECT",
            ok=None,
            detail="The account has no purchases, so this cannot be checked yet.",
        )

    inspected = 0
    partial_example: Optional[str] = None
    detail_keys: list[str] = []

    for row in rows[:sample_size]:
        purchase_id, _status, _ref = schema.parse_purchase_list_entry(row)
        if not purchase_id:
            continue
        try:
            detail = client.get(schema.ENDPOINT_PURCHASE, ID=purchase_id)
        except Cin7Error:
            continue

        inspected += 1
        if isinstance(detail, dict) and not detail_keys:
            detail_keys = sorted(detail.keys())

        parsed = schema.parse_purchase(detail)
        if parsed and any(
            line.received_quantity > 0 for line in parsed.lines
        ):
            line = next(ln for ln in parsed.lines if ln.received_quantity > 0)
            partial_example = (
                f"purchase {parsed.id}: {line.sku} ordered "
                f"{line.ordered_quantity:g}, received {line.received_quantity:g}, "
                f"outstanding {line.outstanding_quantity:g}"
            )
            break

    if not inspected:
        return ProbeFinding(
            question=question,
            answer="COULD NOT FETCH PURCHASE DETAIL",
            ok=False,
            detail="No purchase detail call succeeded.",
        )

    if partial_example is None:
        return ProbeFinding(
            question=question,
            answer="UNCONFIRMED — no received quantities seen",
            ok=None,
            detail=(
                f"Inspected {inspected} purchase(s); none had received "
                f"quantities parse above zero. Purchase detail keys: "
                f"{detail_keys}. This may simply mean nothing has been "
                "received yet."
            ),
            sample_keys=detail_keys,
            fix=(
                "Re-run against a purchase you know is PARTIALLY received. If "
                "receipts still read zero, find the receipt structure in the "
                "keys above and update PURCHASE_RECEIPT_CONTAINER_KEYS or "
                "RECEIVED_QUANTITY_KEYS in schema.py. Getting this wrong makes "
                "inbound stock too high and suppresses real reorders."
            ),
        )

    return ProbeFinding(
        question=question,
        answer="YES",
        ok=True,
        detail=partial_example,
        sample_keys=detail_keys,
    )


def _probe_draft_update(client: Cin7Client) -> ProbeFinding:
    """GATING #2: can an existing draft purchase be updated?

    Deliberately does NOT attempt a write. A probe that mutates the account
    to learn something is not a probe. This reports what is known and tells
    the operator how to settle it deliberately.
    """
    return ProbeFinding(
        question="Can an existing draft purchase be updated via the API?",
        answer="NOT TESTED — requires a write",
        ok=None,
        detail=(
            "Cin7's documented Purchase methods are GET, POST and DELETE; PUT "
            "is unconfirmed. This probe will not write to your account to find "
            "out."
        ),
        fix=(
            "Create a throwaway draft purchase by hand, then try "
            "PUT /purchase (and PUT /purchase/order) against it with a REST "
            "client. If neither works, set drafts to delete-and-recreate — note "
            "that changes the PO number on every run, and voiding in Cin7 is "
            "permanent."
        ),
    )


def _probe_supplier_attributes(client: Cin7Client, sample_size: int) -> ProbeFinding:
    question = "Do supplier additional attributes come back on GET /supplier?"

    try:
        payload = client.get(schema.ENDPOINT_SUPPLIER, page=1, limit=sample_size)
    except Cin7Error as exc:
        return ProbeFinding(
            question=question, answer="ENDPOINT FAILED", ok=False, detail=str(exc)
        )

    records = schema.extract_list(payload)
    if not records:
        return ProbeFinding(
            question=question, answer="NO SUPPLIERS RETURNED", ok=False
        )

    sample = records[0]
    container_present = any(
        schema.get_first(r, *schema.SUPPLIER_ATTRIBUTE_CONTAINER_KEYS) is not None
        for r in records
    )

    return ProbeFinding(
        question=question,
        answer="yes" if container_present else "NOT FOUND",
        ok=container_present,
        detail=(
            f"Supplier record keys: {sorted(sample.keys())}."
            if not container_present
            else "An attributes container was present on at least one supplier."
        ),
        sample_keys=sorted(sample.keys()),
        fix=(
            ""
            if container_present
            else (
                "If attributes are absent, fall back to listing supplier IDs in "
                "`suppliers.pin` in config.yaml instead of using the "
                "'Auto Reorder' attribute."
            )
        ),
    )


def _probe_reorder_parameters(client: Cin7Client, sample_size: int) -> ProbeFinding:
    question = "Are per-supplier reorder parameters (lead / safety / qty) exposed?"

    try:
        payload = client.get(schema.ENDPOINT_PRODUCT, page=1, limit=sample_size)
    except Cin7Error as exc:
        return ProbeFinding(
            question=question, answer="ENDPOINT FAILED", ok=False, detail=str(exc)
        )

    records = schema.extract_list(payload)
    if not records:
        return ProbeFinding(question=question, answer="NO PRODUCTS", ok=False)

    found: list[str] = []
    for record in records:
        for params in schema.parse_reorder_parameters(record):
            if params.is_complete:
                found.append(
                    f"product {params.product_id} / supplier {params.supplier_id}"
                    f"{'' if params.location is None else ' @ ' + params.location}: "
                    f"lead {params.lead_days:g}d, safety {params.safety_days:g}d, "
                    f"reorder qty {params.reorder_quantity}"
                )

    if not found:
        return ProbeFinding(
            question=question,
            answer="NOT FOUND in the sample",
            ok=None,
            detail=(
                f"Product record keys: {sorted(records[0].keys())}. No complete "
                "lead/safety pair was parsed from the sampled products."
            ),
            sample_keys=sorted(records[0].keys()),
            fix=(
                "Either these products have no reorder parameters set, or the "
                "keys differ. Check PRODUCT_SUPPLIER_CONTAINER_KEYS and the "
                "lead/safety key names in schema.parse_reorder_parameters. "
                "Note that par levels also need a demand rate, which these "
                "three numbers do not contain — see parlevels.py."
            ),
        )

    return ProbeFinding(
        question=question,
        answer="yes",
        ok=True,
        detail="; ".join(found[:3]),
        sample_keys=sorted(records[0].keys()),
    )


# ---------------------------------------------------------------------------


def format_findings(findings: list[ProbeFinding]) -> str:
    out: list[str] = ["", "=" * 78, "CIN7 REORDER — PROBE RESULTS", "=" * 78, ""]

    for index, finding in enumerate(findings, start=1):
        marker = {True: "[ OK ]", False: "[FAIL]", None: "[ ?? ]"}[finding.ok]
        out.append(f"{marker}  {index}. {finding.question}")
        out.append(f"        -> {finding.answer}")
        if finding.detail:
            out.append(f"        {finding.detail}")
        if finding.fix:
            out.append(f"        FIX: {finding.fix}")
        out.append("")

    failures = [f for f in findings if f.ok is False]
    unknowns = [f for f in findings if f.ok is None]

    out.append("-" * 78)
    if failures:
        out.append(
            f"{len(failures)} check(s) FAILED. Fix schema.py before trusting "
            "any output from `plan`."
        )
    if unknowns:
        out.append(
            f"{len(unknowns)} check(s) could not be settled automatically and "
            "need a manual look."
        )
    if not failures and not unknowns:
        out.append("All checks passed. `plan` output can be trusted to parse correctly.")
    out.append("-" * 78)
    out.append("")

    return "\n".join(out)
