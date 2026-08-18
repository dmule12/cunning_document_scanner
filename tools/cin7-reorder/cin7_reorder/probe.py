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
        _probe_include_flags(client),
        _probe_availability(client),
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


def _probe_include_flags(client: Cin7Client) -> ProbeFinding:
    """Do the include flags still work, and do they work together?

    Each was confirmed alone. Sending them combined is what the real run
    does, and an API that honoured only the first would put us straight back
    to silently empty collections.
    """
    question = "Do the product include-flags work when sent together?"

    scanned = 0
    populated: dict[str, int] = {}

    for page in range(1, 4):
        result = client.try_get(
            schema.ENDPOINT_PRODUCT,
            page=page,
            limit=500,
            **schema.PRODUCT_INCLUDE_FLAGS,
        )
        if not result.ok:
            return ProbeFinding(
                question=question,
                answer="PRODUCT ENDPOINT FAILED",
                ok=False,
                detail=result.detail,
                fix=(
                    "Without these flags Cin7 returns every nested collection "
                    "empty, which reads as missing data rather than an error."
                ),
            )

        records = schema.extract_list(result.payload)
        if not records:
            break
        scanned += len(records)

        for record in records:
            for key in ("BillOfMaterialsProducts", "ReorderLevels", "Suppliers"):
                value = record.get(key)
                if isinstance(value, list) and value:
                    populated[key] = populated.get(key, 0) + 1

        if len(populated) == 3 or len(records) < 500:
            break

    missing = [
        key
        for key in ("BillOfMaterialsProducts", "ReorderLevels", "Suppliers")
        if key not in populated
    ]

    summary = ", ".join(
        f"{key}={populated.get(key, 0)}"
        for key in ("BillOfMaterialsProducts", "ReorderLevels", "Suppliers")
    )

    if "Suppliers" in missing:
        return ProbeFinding(
            question=question,
            answer="SUPPLIERS NOT POPULATED",
            ok=False,
            detail=(
                f"Across {scanned} product(s): {summary} (counts are products "
                "with a non-empty collection)."
            ),
            fix=(
                "Without supplier links every product is skipped as having no "
                "supplier, so the run produces nothing. Re-check the flag with "
                "`dump --flags --id <a-product-you-order-regularly>`."
            ),
        )

    return ProbeFinding(
        question=question,
        answer="YES" if not missing else "PARTIALLY",
        ok=not missing,
        detail=(
            f"Across {scanned} product(s): {summary} (counts are products with "
            "a non-empty collection)."
        ),
        fix=(
            ""
            if not missing
            else f"No product had: {', '.join(missing)}. That may just mean "
            "none is configured, rather than a broken flag."
        ),
    )


def _probe_availability(client: Cin7Client) -> ProbeFinding:
    """Where do stock levels live?

    Without these the run cannot tell what is in stock, so every product
    reads as zero on hand and the tool would order the whole catalogue.
    """
    question = "Can we read stock levels?"

    endpoint = client.resolve_endpoint(
        schema.AVAILABILITY_ENDPOINT_CANDIDATES, page=1, limit=5
    )

    if endpoint is None:
        return ProbeFinding(
            question=question,
            answer="NO WORKING ENDPOINT",
            ok=False,
            detail=(
                "Tried: " + ", ".join(schema.AVAILABILITY_ENDPOINT_CANDIDATES)
            ),
            fix=(
                "Nothing can run without stock levels — every product would "
                "read as zero on hand. Find the right path in Cin7's API "
                "reference and add it to AVAILABILITY_ENDPOINT_CANDIDATES in "
                "schema.py."
            ),
        )

    result = client.try_get(endpoint, page=1, limit=5)
    records = schema.extract_list(result.payload) if result.ok else []
    parsed = [schema.parse_availability(r) for r in records]
    usable = [p for p in parsed if p is not None]

    if not usable:
        return ProbeFinding(
            question=question,
            answer=f"ENDPOINT FOUND ({endpoint}) BUT NOTHING PARSED",
            ok=False,
            detail=f"{len(records)} record(s) returned; none had a product ID.",
            sample_keys=sorted(records[0].keys()) if records else [],
            fix=(
                "Check the availability field names in schema.parse_availability "
                "against the keys above."
            ),
        )

    example = usable[0]
    return ProbeFinding(
        question=question,
        answer=f"YES — via {endpoint}",
        ok=True,
        detail=(
            f"Example: product {example.product_id} at "
            f"{example.location or '(no location)'} — on hand "
            f"{example.on_hand:g}, allocated {example.allocated:g}."
        ),
        fix=(
            ""
            if endpoint == schema.ENDPOINT_PRODUCT_AVAILABILITY
            else f"Set ENDPOINT_PRODUCT_AVAILABILITY to '{endpoint}' in schema.py "
            "so the run does not spend calls rediscovering it."
        ),
    )


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
        result = client.try_get(
            schema.ENDPOINT_PRODUCT,
            page=page,
            limit=500,
            **schema.PRODUCT_INCLUDE_FLAGS,
        )
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

    scanned = 0
    with_set = 0
    populated: list[str] = []

    # Scan properly rather than peeking at the first few. Suppliers come back
    # alphabetically, so a five-record sample only ever sees names starting
    # with digits and 'A' — and would report "nothing configured" however
    # carefully the account had been set up.
    for page in range(1, 5):
        result = client.try_get(schema.ENDPOINT_SUPPLIER, page=page, limit=500)
        if not result.ok:
            return ProbeFinding(
                question=question,
                answer="ENDPOINT FAILED",
                ok=False,
                detail=result.detail,
            )

        records = schema.extract_list(result.payload)
        if not records:
            break
        scanned += len(records)

        for record in records:
            name = schema.parse_supplier_name(record) or "(unnamed)"
            attribute_set = schema.as_str(schema.get_first(record, "AttributeSet"))
            if attribute_set:
                with_set += 1

            slots = schema.supplier_attribute_slots(record)
            if slots:
                rendered = ", ".join(f"{k}={v!r}" for k, v in slots.items())
                populated.append(f"{name} [set: {attribute_set or '—'}]: {rendered}")

        if len(records) < 500:
            break

    if not populated:
        return ProbeFinding(
            question=question,
            answer="NO SLOTS POPULATED YET",
            ok=None,
            detail=(
                f"Scanned {scanned} supplier(s); {with_set} have an attribute "
                "set assigned, and none has a value in any slot."
            ),
            fix=(
                "In Cin7: Settings → Reference books → Other Items → Additional "
                "Attributes, create an 'Auto Reorder' attribute (Checkbox is "
                "fine) in a set applied to suppliers. Then — the step that is "
                "easy to miss — open the supplier itself, assign it that "
                "attribute set, tick the box, and save. Creating the set does "
                "not attach it to anyone.\n        "
                "Meanwhile `--supplier \"<name>\"` runs against a chosen "
                "supplier without needing any of this."
            ),
        )

    shown = populated[:10]
    more = len(populated) - len(shown)
    detail = f"Scanned {scanned} supplier(s); {len(populated)} have a slot set.\n        "
    detail += "\n        ".join(shown)
    if more:
        detail += f"\n        …and {more} more."

    return ProbeFinding(
        question=question,
        answer="SLOTS IN USE — pick the right one",
        ok=True,
        detail=detail,
        fix=(
            "Put the slot holding your opt-in flag into "
            "`suppliers.attribute_field` in config.yaml, e.g. "
            "attribute_field: AdditionalAttribute1. The slot names are opaque, "
            "so match them by the values shown above."
        ),
    )


def _probe_reorder_parameters(client: Cin7Client, sample_size: int) -> ProbeFinding:
    question = "How much of the catalogue has a usable reorder point?"

    scanned = 0
    products_orderable = 0
    trigger_without_quantity = 0
    with_supplier = 0
    examples: list[str] = []
    first_keys: list[str] = []

    # Scan the catalogue rather than sampling. The answer people actually
    # need here is "how much of my catalogue is eligible", and that is a
    # proportion, not an anecdote from the first five rows.
    for page in range(1, 11):
        result = client.try_get(
            schema.ENDPOINT_PRODUCT,
            page=page,
            limit=500,
            **schema.PRODUCT_INCLUDE_FLAGS,
        )
        if not result.ok:
            return ProbeFinding(
                question=question,
                answer="ENDPOINT FAILED",
                ok=False,
                detail=result.detail,
            )

        records = schema.extract_list(result.payload)
        if not records:
            break
        if not first_keys:
            first_keys = sorted(records[0].keys())
        scanned += len(records)

        for record in records:
            product = schema.parse_product(record)
            has_supplier = bool(product and product.supplier_id)

            complete = [
                p for p in schema.parse_reorder_parameters(record) if p.is_complete
            ]
            partial = [
                p
                for p in schema.parse_reorder_parameters(record)
                if not p.is_complete
                and p.minimum_before_reorder
                and p.minimum_before_reorder > 0
            ]

            if complete:
                products_orderable += 1
                if has_supplier:
                    with_supplier += 1
                if len(examples) < 3:
                    p = complete[0]
                    where = (
                        "product level"
                        if p.location is None
                        else f"location {p.location}"
                    )
                    examples.append(
                        f"{product.sku if product else p.product_id} ({where}): "
                        f"min {p.minimum_before_reorder:g}, qty "
                        f"{p.reorder_quantity:g}"
                    )
            elif partial:
                trigger_without_quantity += 1

        if len(records) < 500:
            break

    if not products_orderable:
        return ProbeFinding(
            question=question,
            answer="NONE",
            ok=False,
            detail=(
                f"Scanned {scanned} product(s); none had both a "
                "MinimumBeforeReorder above zero and a ReorderQuantity."
            ),
            sample_keys=first_keys,
            fix=(
                "Either no product has a low-stock reorder point configured in "
                "Cin7 — in which case the tool correctly has nothing to do — or "
                "the field names differ. Check MINIMUM_KEYS and "
                "REORDER_QUANTITY_KEYS in schema.py against the keys above."
            ),
        )

    pct = 100.0 * products_orderable / scanned if scanned else 0.0
    detail = (
        f"{products_orderable} of {scanned} product(s) scanned ({pct:.0f}%) have "
        f"both a reorder point and a quantity; {with_supplier} of those also "
        f"have a supplier and so can actually be ordered.\n        "
        f"Examples: " + "; ".join(examples)
    )
    if trigger_without_quantity:
        detail += (
            f"\n        {trigger_without_quantity} product(s) have a minimum but "
            "no reorder quantity — those are skipped and listed in the report."
        )

    return ProbeFinding(
        question=question,
        answer=f"{products_orderable} product(s) eligible",
        ok=True,
        detail=detail,
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
