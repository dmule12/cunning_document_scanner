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
    "carton",
    "family",
    "parent",
    "child",
    "conversion",
    "reorder",
    "quantitytoproduce",
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
        result = client.try_get(schema.ENDPOINT_PRODUCT, page=1, limit=20, **params)
        if not result.ok:
            notes.append(f"lookup by {label}: {result.detail}")
            continue

        records = schema.extract_list(result.payload)
        if not records and isinstance(result.payload, dict):
            # A by-ID fetch may return the object directly rather than a list.
            if schema.get_first(result.payload, "ID", "ProductID"):
                records = [result.payload]

        if not records:
            notes.append(f"lookup by {label}: no records")
            continue

        if sku:
            exact = [
                r
                for r in records
                if (schema.as_str(schema.get_first(r, "SKU")) or "").upper()
                == sku.upper()
            ]
            if exact:
                notes.append(f"lookup by {label}: exact SKU match")
                return exact[0], notes

            # Cin7 ignores query parameters it does not recognise and returns
            # an unfiltered page instead. Returning the first row here would
            # present an unrelated product as though it were the one asked
            # for — which is worse than finding nothing.
            notes.append(
                f"lookup by {label}: {len(records)} record(s) but none matched "
                f"SKU {sku!r} — filter appears to have been ignored"
            )
            continue

        notes.append(f"lookup by {label}: {len(records)} record(s)")
        return records[0], notes

    return None, notes


def fetch_detail(client: Cin7Client, product_id: str) -> Optional[dict]:
    """Fetch one product by ID.

    The list endpoint returns scalar fields but empties nested collections —
    ``BillOfMaterialsProducts``, ``ReorderLevels`` and ``Suppliers`` all come
    back as ``[]`` there even when populated. Detail is the only way to see
    them.
    """
    result = client.try_get(
        schema.ENDPOINT_PRODUCT, ID=product_id, **schema.PRODUCT_INCLUDE_FLAGS
    )
    if not result.ok:
        return None

    records = schema.extract_list(result.payload)
    if records:
        return records[0]
    if isinstance(result.payload, dict) and schema.get_first(result.payload, "ID"):
        return result.payload
    return None


#: Collections the v2 product endpoint returns empty, on both list and detail
#: reads. Each is needed by the reorder run, so if none of the deep-lookup
#: strategies fills them the data has to come from somewhere other than the
#: product endpoint.
WANTED_COLLECTIONS = ("BillOfMaterialsProducts", "ReorderLevels", "Suppliers")


#: Cin7 gates each nested collection behind its own opt-in query flag.
#: ``IncludeBOM=true`` is confirmed to populate ``BillOfMaterialsProducts``;
#: ``IncludeAll=true`` does nothing, so the others need their own flags.
#: This is the candidate list for finding them.
INCLUDE_FLAG_CANDIDATES = (
    "IncludeBOM",
    "IncludeBillOfMaterials",
    "IncludeSuppliers",
    "IncludeSupplier",
    "IncludeProductSuppliers",
    "IncludeReorderLevels",
    "IncludeReorder",
    "IncludeReorderLevel",
    "IncludeLocations",
    "IncludeStockLocations",
    "IncludeDetails",
    "IncludeAttachments",
    "IncludeMovements",
    "IncludePriceTiers",
)


def sweep_include_flags(
    client: Cin7Client, product_id: str
) -> tuple[list[tuple[str, dict[str, Any]]], dict[str, str]]:
    """Try each candidate include-flag alone, and see what it fills in.

    Returns the per-flag results plus a mapping of collection name to the
    first flag that populated it, so the caller can report exactly which
    flags the real run needs to send.
    """
    rows: list[tuple[str, dict[str, Any]]] = []
    winners: dict[str, str] = {}

    for flag in INCLUDE_FLAG_CANDIDATES:
        result = client.try_get(
            schema.ENDPOINT_PRODUCT, ID=product_id, **{flag: "true"}
        )
        row: dict[str, Any] = {"ok": result.ok, "detail": result.detail}

        if result.ok:
            records = schema.extract_list(result.payload)
            if not records and isinstance(result.payload, dict):
                if schema.get_first(result.payload, "ID"):
                    records = [result.payload]

            if records:
                record = records[0]
                for key in WANTED_COLLECTIONS:
                    value = record.get(key)
                    count = len(value) if isinstance(value, list) else 0
                    row[key] = count
                    if count > 0 and key not in winners:
                        winners[key] = flag
            else:
                row["ok"] = False
                row["detail"] = "no records"

        rows.append((flag, row))

    return rows, winners


def render_flag_sweep(
    rows: list[tuple[str, dict[str, Any]]], winners: dict[str, str]
) -> str:
    out = ["", "=" * 78, "INCLUDE-FLAG SWEEP", "=" * 78, ""]
    out.append("  Each flag sent alone. Numbers are entries returned.")
    out.append("")

    header = f"  {'flag':<26} {'BOM':>6} {'Reorder':>8} {'Suppliers':>10}"
    out.append(header)
    out.append("  " + "-" * (len(header) - 2))

    for flag, row in rows:
        if not row["ok"]:
            out.append(f"  {flag:<26} {row['detail'][:38]}")
            continue
        out.append(
            f"  {flag:<26} "
            f"{row.get('BillOfMaterialsProducts', 0):>6} "
            f"{row.get('ReorderLevels', 0):>8} "
            f"{row.get('Suppliers', 0):>10}"
        )

    out.append("")
    out.append("  RESULT")
    for key in WANTED_COLLECTIONS:
        flag = winners.get(key)
        if flag:
            out.append(f"    {key:<26} ← {flag}=true")
        else:
            out.append(f"    {key:<26} ← NO FLAG FOUND")
    out.append("")

    missing = [k for k in WANTED_COLLECTIONS if k not in winners]
    if missing:
        out.append(
            "  Collections with no working flag may simply be empty on this\n"
            "  product. Try --flags against a product you know has suppliers\n"
            "  and a per-location reorder level set."
        )
        out.append("")

    return "\n".join(out)


def deep_lookup(client: Cin7Client, product_id: str) -> list[tuple[str, dict[str, Any]]]:
    """Try every plausible way to get a product's nested collections.

    ``GET /product?ID=`` returns the right product but with every collection
    empty. Before concluding the API cannot supply them, this tries the older
    API version, a path-style fetch, and a couple of include-style flags.

    Returns one row per strategy: its label, whether it worked, and how many
    entries each wanted collection came back with.
    """
    v1 = client.base_url_v1
    strategies: list[tuple[str, Optional[str], str, dict[str, Any]]] = [
        ("v2 product?ID=", None, schema.ENDPOINT_PRODUCT, {"ID": product_id}),
        ("v1 Product?ID=", v1, "Product", {"ID": product_id}),
        ("v1 product?ID=", v1, "product", {"ID": product_id}),
        (
            "v2 product?ID=&IncludeBOM",
            None,
            schema.ENDPOINT_PRODUCT,
            {"ID": product_id, "IncludeBOM": "true"},
        ),
        (
            "v2 product?ID=&IncludeAll",
            None,
            schema.ENDPOINT_PRODUCT,
            {"ID": product_id, "IncludeAll": "true"},
        ),
        ("v2 productFamily?ID=", None, "productFamily", {"ID": product_id}),
        ("v2 product/{id}", None, f"product/{product_id}", {}),
    ]

    rows: list[tuple[str, dict[str, Any]]] = []

    for label, base, path, params in strategies:
        result = client.try_get(path, base_url=base, **params)
        row: dict[str, Any] = {"ok": result.ok, "detail": result.detail}

        if result.ok:
            records = schema.extract_list(result.payload)
            if not records and isinstance(result.payload, dict):
                if schema.get_first(result.payload, "ID"):
                    records = [result.payload]

            if records:
                record = records[0]
                row["sku"] = schema.as_str(schema.get_first(record, "SKU"))
                for key in WANTED_COLLECTIONS:
                    value = record.get(key)
                    row[key] = len(value) if isinstance(value, list) else "absent"
            else:
                row["ok"] = False
                row["detail"] = "no records"

        rows.append((label, row))

    return rows


def render_deep(product_id: str, rows: list[tuple[str, dict[str, Any]]]) -> str:
    out = ["", "=" * 78, f"DEEP LOOKUP — {product_id}", "=" * 78, ""]
    out.append(
        "  Each strategy, and how many entries each collection came back with."
    )
    out.append("  A number above zero anywhere means the data is reachable.")
    out.append("")

    header = f"  {'strategy':<28} {'BOM':>6} {'Reorder':>8} {'Suppliers':>10}"
    out.append(header)
    out.append("  " + "-" * (len(header) - 2))

    any_success = False
    for label, row in rows:
        if not row["ok"]:
            out.append(f"  {label:<28} {row['detail'][:40]}")
            continue
        bom = row.get("BillOfMaterialsProducts", "?")
        reorder = row.get("ReorderLevels", "?")
        suppliers = row.get("Suppliers", "?")
        if any(isinstance(v, int) and v > 0 for v in (bom, reorder, suppliers)):
            any_success = True
        out.append(f"  {label:<28} {bom:>6} {reorder:>8} {suppliers:>10}")

    out.append("")
    if any_success:
        out.append("  ✓ At least one strategy returned data. Use that one.")
    else:
        out.append(
            "  ✗ Every strategy returned empty collections.\n"
            "    If the Cin7 UI shows a bill of materials for this product,\n"
            "    the API is withholding it and the mapping must come from\n"
            "    config. If the UI is also empty, the BOM was never filled in\n"
            "    and this is a data task in Cin7."
        )
    out.append("")
    return "\n".join(out)


def compare_list_and_detail(list_record: dict, detail: Optional[dict]) -> list[str]:
    """Report which nested collections the detail call filled in."""
    if detail is None:
        return ["  detail fetch FAILED — could not retrieve this product by ID"]

    notes = []
    for key in ("BillOfMaterialsProducts", "ReorderLevels", "Suppliers"):
        in_list = list_record.get(key)
        in_detail = detail.get(key)
        list_n = len(in_list) if isinstance(in_list, list) else "?"
        detail_n = len(in_detail) if isinstance(in_detail, list) else "?"
        verdict = (
            "FILLED IN BY DETAIL"
            if (detail_n or 0) > (list_n or 0)
            else "still empty"
            if not detail_n
            else "same"
        )
        notes.append(f"  {key}: list={list_n}, detail={detail_n}  → {verdict}")
    return notes


def find_products_with_bom(
    client: Cin7Client, *, max_pages: int = 20, wanted: int = 3
) -> tuple[list[dict], int, list[str]]:
    """Page the catalogue looking for products that carry a bill of materials.

    More useful than a SKU lookup when the goal is "show me a real pack
    product": it finds them by structure rather than by guessing a code, and
    it reports how many were scanned so an empty result is unambiguous.
    """
    found: list[dict] = []
    scanned = 0
    notes: list[str] = []

    for page in range(1, max_pages + 1):
        result = client.try_get(
            schema.ENDPOINT_PRODUCT,
            page=page,
            limit=500,
            **schema.PRODUCT_INCLUDE_FLAGS,
        )
        if not result.ok:
            notes.append(f"page {page}: {result.detail}")
            break

        records = schema.extract_list(result.payload)
        if not records:
            break

        scanned += len(records)

        for record in records:
            if schema.product_has_bom(record):
                found.append(record)
                if len(found) >= wanted:
                    notes.append(
                        f"scanned {scanned} product(s) across {page} page(s)"
                    )
                    return found, scanned, notes

        if len(records) < 500:
            break

    notes.append(f"scanned {scanned} product(s)")
    return found, scanned, notes


#: Key fragments on a purchaseList row that might name the supplier. The
#: supplier filter is worth nothing if it reads a key the account does not
#: return, and the failure is silent: every row looks unattributed, so every
#: row gets fetched, which is exactly the behaviour the filter exists to stop.
SUPPLIER_FRAGMENTS = ("supplier", "vendor", "contact")
STATUS_FRAGMENTS = ("status",)


def survey_purchase_list(
    client: Cin7Client, *, max_pages: int = 20
) -> dict[str, Any]:
    """Page ``purchaseList`` and report what there is to filter on.

    Reads list pages only — 500 rows a call, no detail fetches — so this is
    cheap to run against an account with thousands of orders. That is the
    point: the expensive stage is deciding which rows deserve a detail call,
    and this shows what that decision has to work with.
    """
    total = 0
    keys_seen: set[str] = set()
    statuses: dict[str, int] = {}
    supplier_key_hits: dict[str, int] = {}
    sample: Optional[dict] = None

    open_rows = 0
    parsed_status: dict[str, int] = {}
    attributed = 0
    closed_by_receiving = 0

    for page in range(1, max_pages + 1):
        result = client.try_get(
            schema.ENDPOINT_PURCHASE_LIST, page=page, limit=500
        )
        if not result.ok:
            break

        records = schema.extract_list(result.payload)
        if not records:
            break

        for record in records:
            total += 1
            if sample is None:
                sample = record
            keys_seen.update(str(k) for k in record)

            for key, value in record.items():
                lowered = str(key).lower()
                if any(f in lowered for f in SUPPLIER_FRAGMENTS):
                    if value not in (None, ""):
                        supplier_key_hits[str(key)] = (
                            supplier_key_hits.get(str(key), 0) + 1
                        )
                if any(f in lowered for f in STATUS_FRAGMENTS):
                    label = f"{key}={value}"
                    statuses[label] = statuses.get(label, 0) + 1

            entry = schema.parse_purchase_list_entry(record)
            name = entry.status.name
            parsed_status[name] = parsed_status.get(name, 0) + 1
            if entry.is_closed:
                if entry.status not in schema.CLOSED_STATUSES:
                    closed_by_receiving += 1
            else:
                open_rows += 1
                if entry.names_a_supplier:
                    attributed += 1

        if len(records) < 500:
            break

    return {
        "total": total,
        "keys": sorted(keys_seen),
        "statuses": statuses,
        "supplier_key_hits": supplier_key_hits,
        "sample": sample,
        "open_rows": open_rows,
        "parsed_status": parsed_status,
        "attributed": attributed,
        "closed_by_receiving": closed_by_receiving,
    }


def render_purchase_survey(survey: dict[str, Any]) -> str:
    out = ["", "=" * 78, "PURCHASE LIST SURVEY", "=" * 78, ""]

    total = survey["total"]
    if not total:
        out.append("  No purchase orders returned at all.")
        out.append("")
        return "\n".join(out)

    open_rows = survey["open_rows"]
    attributed = survey["attributed"]

    out.append(f"  {total} purchase order(s) on the list endpoint.")
    out.append(f"  {open_rows} of them read as open, so each may cost a detail call.")
    if survey["closed_by_receiving"]:
        out.append(
            f"  {survey['closed_by_receiving']} were closed by their receiving "
            "status alone —\n  Cin7 leaves those AUTHORISED forever, so Status "
            "would have called them open."
        )
    out.append("")

    out.append("  STATUS VALUES AS RETURNED")
    for label, count in sorted(
        survey["statuses"].items(), key=lambda kv: -kv[1]
    ):
        out.append(f"    {count:>6}  {label}")
    out.append("")

    out.append("  HOW parse_status READ THEM")
    for name, count in sorted(
        survey["parsed_status"].items(), key=lambda kv: -kv[1]
    ):
        marker = ""
        if name == "UNKNOWN":
            marker = "  ← not recognised, treated as open"
        out.append(f"    {count:>6}  {name}{marker}")
    out.append("")

    out.append("  SUPPLIER-ISH KEYS WITH VALUES")
    if survey["supplier_key_hits"]:
        for key, count in sorted(
            survey["supplier_key_hits"].items(), key=lambda kv: -kv[1]
        ):
            out.append(f"    {count:>6}  {key}")
    else:
        out.append("    NONE. No key on any row names a supplier.")
    out.append("")

    out.append("  WHAT THE SUPPLIER FILTER CAN DO WITH THAT")
    if not open_rows:
        out.append("    Nothing to filter — no open orders.")
    elif attributed == open_rows:
        out.append(
            f"    All {open_rows} open order(s) name a supplier, so only the "
            "ones\n    belonging to opted-in suppliers cost a detail call."
        )
    elif attributed == 0:
        out.append(
            f"    NONE of the {open_rows} open order(s) name a supplier the "
            "parser\n    recognises, so every one of them has to be fetched. "
            "That is\n    roughly the whole cost of a run.\n\n"
            "    Fix: find the supplier key in the sample row below and add it "
            "to\n    parse_purchase_list_entry in schema.py."
        )
    else:
        out.append(
            f"    {attributed} of {open_rows} open order(s) name a supplier; "
            f"the other\n    {open_rows - attributed} must be fetched to find "
            "out who they are from."
        )
    out.append("")

    out.append("  ALL KEYS ON A ROW")
    for key in survey["keys"]:
        out.append(f"    {key}")
    out.append("")

    out.append("-" * 78)
    out.append("SAMPLE ROW")
    out.append("-" * 78)
    out.append(json.dumps(survey["sample"], indent=2, default=str, sort_keys=True))
    out.append("")
    out.append(
        "NOTE: this is one real purchase order, including supplier and possibly "
        "totals.\nTrim anything you would rather not share before pasting it "
        "anywhere."
    )
    out.append("")

    return "\n".join(out)


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
