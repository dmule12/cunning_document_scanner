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


#: Candidate locations for bill-of-materials data. Cin7 answers an unknown
#: path with a 302 to an HTML error page rather than a 404, so each candidate
#: is judged purely on whether it returns JSON.
BOM_ENDPOINT_CANDIDATES = (
    ("v2", "BillOfMaterials"),
    ("v2", "billOfMaterials"),
    ("v2", "productBOM"),
    ("v2", "productBillOfMaterials"),
    ("v2", "assembly"),
    ("v1", "BillOfMaterials"),
    ("v1", "bom"),
)

#: Sent to every candidate on the first pass. A second pass drops them
#: entirely, because an unrecognised query parameter is itself a plausible
#: cause of Cin7's redirect-to-error-page behaviour — and every attempt in
#: the first probe run carried `onlyProductsWithBOM`.
BOM_FILTER_PARAMS = {"onlyProductsWithBOM": "true"}


#: How many pages of products to scan looking for one with a BOM. The whole
#: catalogue would be more thorough but this is a diagnostic, not a run.
BOM_SCAN_PAGES = 10


def _probe_bom_components(client: Cin7Client, sample_size: int) -> ProbeFinding:
    """Can a pack SKU be mapped to the base units it contains?

    Bills of materials live on the product record, not at their own
    endpoint — every candidate /BillOfMaterials path returns Cin7's
    not-found redirect. So this scans the catalogue for products carrying a
    BOM rather than calling a dedicated endpoint.
    """
    question = "Can we map a pack SKU to its base units?"

    scanned = 0
    with_bom: list[dict] = []

    for page in range(1, BOM_SCAN_PAGES + 1):
        result = client.try_get(schema.ENDPOINT_PRODUCT, page=page, limit=500)
        if not result.ok:
            return ProbeFinding(
                question=question,
                answer="PRODUCT ENDPOINT FAILED",
                ok=False,
                detail=result.detail,
            )

        records = schema.extract_list(result.payload)
        if not records:
            break

        scanned += len(records)
        with_bom.extend(r for r in records if schema.product_has_bom(r))

        if len(with_bom) >= sample_size or len(records) < 500:
            break

    if not with_bom:
        return ProbeFinding(
            question=question,
            answer="NO PRODUCT CARRIES A BILL OF MATERIALS",
            ok=False,
            detail=(
                f"Scanned {scanned} product(s); none had a bill of materials. "
                "Cin7 is not recording that any pack contains a number of base "
                "units."
            ),
            fix=(
                "This is a data problem, not a code one — no API call can "
                "return a relationship that was never created. Either "
                "configure Additional Units of Measure on your pack products "
                "in Cin7, or supply the pack sizes from a config file. Run "
                "`dump --sku <a-pack-sku>` to confirm against a specific "
                "product."
            ),
        )

    parsed = [schema.parse_bill_of_materials(r) for r in with_bom]
    usable = [p for p in parsed if p and p.components]

    if not usable:
        return ProbeFinding(
            question=question,
            answer="PRODUCTS MARKED AS ASSEMBLIES BUT NO COMPONENTS PARSED",
            ok=False,
            detail=(
                f"{len(with_bom)} of {scanned} scanned product(s) are flagged "
                "as having a bill of materials, but none yielded components "
                f"under any of {list(schema.BOM_COMPONENT_KEYS)}."
            ),
            sample_keys=sorted(with_bom[0].keys()),
            fix=(
                "Either those products have an empty BOM in Cin7, or the "
                "component key differs. Check the keys above and add the right "
                "one to BOM_COMPONENT_KEYS in schema.py."
            ),
        )

    example = usable[0]
    first = example.components[0]
    return ProbeFinding(
        question=question,
        answer="YES — bills of materials are on the product record",
        ok=True,
        detail=(
            f"{len(usable)} usable BOM(s) in {scanned} scanned product(s). "
            f"Example: parent {example.parent_product_id} contains "
            f"{first.quantity:g} × {first.component_product_id}."
        ),
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
    """Which attribute slot should the opt-in flag live in?

    Cin7 exposes supplier attributes as ten opaque numbered slots. The
    human-readable label lives in the attribute set definition rather than on
    the supplier, so the only practical way to choose a slot is to see which
    ones already hold values.
    """
    question = "Which supplier attribute slot holds the opt-in flag?"

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

    lines: list[str] = []
    any_populated = False

    for record in records:
        name = schema.parse_supplier_name(record) or "(unnamed)"
        attribute_set = schema.as_str(schema.get_first(record, "AttributeSet")) or "—"
        slots = schema.supplier_attribute_slots(record)
        if slots:
            any_populated = True
            rendered = ", ".join(f"{k}={v!r}" for k, v in slots.items())
        else:
            rendered = "no attribute slots populated"
        lines.append(f"{name} [set: {attribute_set}]: {rendered}")

    if not any_populated:
        return ProbeFinding(
            question=question,
            answer="NO SLOTS POPULATED YET",
            ok=None,
            detail=(
                "Supplier attribute slots exist but none of the sampled "
                "suppliers has a value in any of them.\n        "
                + "\n        ".join(lines)
            ),
            fix=(
                "This is setup you still need to do, not a code problem. In "
                "Cin7: Settings → Reference books → Other Items → Additional "
                "Attributes, create an 'Auto Reorder' attribute in a set "
                "applied to suppliers, then set it to Yes on the one supplier "
                "you want automated. Re-run this probe and it will show which "
                "numbered slot the value landed in; put that slot name in "
                "`suppliers.attribute_field` in config.yaml.\n        "
                "Until then, list supplier IDs under `suppliers.pin` instead."
            ),
        )

    return ProbeFinding(
        question=question,
        answer="SLOTS IN USE — pick the right one",
        ok=True,
        detail="\n        ".join(lines),
        fix=(
            "Put the slot holding your opt-in flag into "
            "`suppliers.attribute_field` in config.yaml, e.g. "
            "attribute_field: AdditionalAttribute1. The slot names are opaque, "
            "so match them by the values shown above."
        ),
    )


def _probe_reorder_parameters(client: Cin7Client, sample_size: int) -> ProbeFinding:
    question = "Are MinimumBeforeReorder / ReorderQuantity exposed on products?"

    try:
        payload = client.get(schema.ENDPOINT_PRODUCT, page=1, limit=sample_size)
    except Cin7Error as exc:
        return ProbeFinding(
            question=question, answer="ENDPOINT FAILED", ok=False, detail=str(exc)
        )

    records = schema.extract_list(payload)
    if not records:
        return ProbeFinding(question=question, answer="NO PRODUCTS", ok=False)

    usable: list[str] = []
    minimum_seen = 0
    trigger_without_quantity = 0

    for record in records:
        for params in schema.parse_reorder_parameters(record):
            has_minimum = (
                params.minimum_before_reorder is not None
                and params.minimum_before_reorder > 0
            )
            if has_minimum:
                minimum_seen += 1
            if params.is_complete:
                where = (
                    "product level"
                    if params.location is None
                    else f"location {params.location}"
                )
                usable.append(
                    f"{params.product_id} ({where}): min "
                    f"{params.minimum_before_reorder:g}, reorder qty "
                    f"{params.reorder_quantity:g}"
                )
            elif has_minimum:
                trigger_without_quantity += 1

    if not minimum_seen:
        return ProbeFinding(
            question=question,
            answer="NO REORDER POINTS in the sample",
            ok=None,
            detail=(
                f"Product record keys: {sorted(records[0].keys())}. No product "
                "in the sample had a MinimumBeforeReorder above zero."
            ),
            sample_keys=sorted(records[0].keys()),
            fix=(
                "Either these products genuinely have no reorder point set — "
                "in which case the tool will skip them, which is intended — or "
                "the key differs. Check MINIMUM_KEYS and REORDER_QUANTITY_KEYS "
                "in schema.py against the keys listed above. Try a larger "
                "--sample-size, or a product you know has a low-stock reorder "
                "point configured."
            ),
        )

    detail = (
        f"{len(usable)} usable reorder point(s) in {len(records)} sampled "
        f"product(s). Examples: " + "; ".join(usable[:3])
    )
    if trigger_without_quantity:
        detail += (
            f" — {trigger_without_quantity} had a minimum but no reorder "
            "quantity, and will be skipped and reported."
        )

    return ProbeFinding(
        question=question,
        answer="YES",
        ok=True,
        detail=detail,
        sample_keys=sorted(records[0].keys()),
    )


# ---------------------------------------------------------------------------


def _wrap_keys(keys: list[str], width: int = 68) -> list[str]:
    """Wrap a long key list so it stays readable in a terminal."""
    lines: list[str] = []
    current = ""
    for key in keys:
        candidate = f"{current}, {key}" if current else key
        if len(candidate) > width and current:
            lines.append(current)
            current = key
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines


def format_findings(findings: list[ProbeFinding]) -> str:
    out: list[str] = ["", "=" * 78, "CIN7 REORDER — PROBE RESULTS", "=" * 78, ""]

    for index, finding in enumerate(findings, start=1):
        marker = {True: "[ OK ]", False: "[FAIL]", None: "[ ?? ]"}[finding.ok]
        out.append(f"{marker}  {index}. {finding.question}")
        out.append(f"        -> {finding.answer}")
        if finding.detail:
            out.append(f"        {finding.detail}")
        if finding.sample_keys:
            # These are the whole point of a failed check: the field names
            # actually present are what tell us where the data lives.
            out.append("        Record keys seen:")
            for chunk in _wrap_keys(finding.sample_keys):
                out.append(f"          {chunk}")
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
