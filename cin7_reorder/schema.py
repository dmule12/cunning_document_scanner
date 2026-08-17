"""Translation between Cin7 API payloads and this tool's domain types.

============================================================================
THIS IS THE ONLY MODULE THAT READS RAW API RESPONSE KEYS. Keep it that way.
============================================================================

Why it exists
-------------
Cin7's API documentation was not reachable when this was written, and three
response shapes could not be verified against a live account:

  1. Does ``GET /BillOfMaterials`` return components with quantities, and
     under what keys?
  2. Can an existing draft purchase be updated, and via which endpoint?
  3. Does ``GET /purchase`` expose per-line RECEIVED quantities?

The arithmetic in this package is testable and, I believe, correct. The field
names below are educated guesses. Isolating them here means that when reality
disagrees, the fix is confined to one file instead of scattered through the
calculation.

Run ``cin7-reorder probe`` against a live account first. It prints the real
shapes and tells you exactly which constants here need changing.

Every extractor is written defensively: it tries several plausible key
spellings, tolerates missing values, and never raises on an unexpected shape.
A field that cannot be found becomes ``None`` or a zero and is surfaced in the
run report, rather than crashing the run or — far worse — silently reading as
a valid number.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Optional, Sequence

from .models import (
    Availability,
    BillOfMaterials,
    BomComponent,
    Product,
    PurchaseLine,
    PurchaseOrder,
    PurchaseStatus,
    ReorderParameters,
)

# ---------------------------------------------------------------------------
# Endpoint paths
# ---------------------------------------------------------------------------

ENDPOINT_PRODUCT = "product"
ENDPOINT_PRODUCT_AVAILABILITY = "productAvailability"
ENDPOINT_BILL_OF_MATERIALS = "BillOfMaterials"
ENDPOINT_SUPPLIER = "supplier"
ENDPOINT_LOCATION = "ref/location"
ENDPOINT_PURCHASE_LIST = "purchaseList"
ENDPOINT_PURCHASE = "purchase"

#: Envelope keys under which Cin7 nests the actual list in a paged response.
#: Cin7 varies this per endpoint (``Products``, ``PurchaseList``, ...), so we
#: fall back to "the first list-valued key" rather than hard-coding each one.
LIST_ENVELOPE_KEYS = (
    "Products",
    "ProductAvailabilityList",
    "ProductAvailability",
    "BillOfMaterials",
    "SupplierList",
    "Suppliers",
    "PurchaseList",
    "LocationList",
    "Locations",
    "Items",
    "List",
)


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------


def get_first(payload: Mapping[str, Any], *keys: str, default: Any = None) -> Any:
    """Return the first present, non-None value among ``keys``.

    Matching is case-insensitive, because Cin7 is not perfectly consistent
    about casing between endpoints.
    """
    if not isinstance(payload, Mapping):
        return default

    lowered = {str(k).lower(): v for k, v in payload.items()}
    for key in keys:
        value = lowered.get(key.lower())
        if value is not None:
            return value
    return default


def as_float(value: Any, default: float = 0.0) -> float:
    """Coerce to float, tolerating strings, blanks and nulls."""
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def as_optional_float(value: Any) -> Optional[float]:
    """Like ``as_float`` but preserves "absent" as ``None``.

    The distinction matters for reorder parameters: a lead time of 0 and a
    missing lead time mean very different things, and conflating them would
    silently produce a par level of zero.
    """
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def as_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def extract_list(payload: Any) -> list[dict]:
    """Pull the list of records out of a paged response envelope."""
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, Mapping):
        return []

    for key in LIST_ENVELOPE_KEYS:
        for actual_key, value in payload.items():
            if str(actual_key).lower() == key.lower() and isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]

    # Fall back to the first list-of-dicts we can find. Cin7 adds endpoints
    # faster than the envelope list above gets updated.
    for value in payload.values():
        if isinstance(value, list) and all(isinstance(i, dict) for i in value):
            return value

    return []


# ---------------------------------------------------------------------------
# Products
# ---------------------------------------------------------------------------


def parse_product(payload: Mapping[str, Any]) -> Optional[Product]:
    product_id = as_str(get_first(payload, "ID", "ProductID", "Id"))
    if not product_id:
        return None

    return Product(
        id=product_id,
        sku=as_str(get_first(payload, "SKU", "Sku", "Code")) or product_id,
        name=as_str(get_first(payload, "Name", "ProductName", "Description")) or "",
        supplier_id=as_str(get_first(payload, "DefaultSupplierID", "SupplierID")),
        supplier_name=as_str(get_first(payload, "DefaultSupplier", "Supplier")),
    )


# ---------------------------------------------------------------------------
# Availability
# ---------------------------------------------------------------------------


def parse_availability(payload: Mapping[str, Any]) -> Optional[Availability]:
    product_id = as_str(get_first(payload, "ProductID", "ID", "Id"))
    if not product_id:
        return None

    return Availability(
        product_id=product_id,
        location=as_str(get_first(payload, "Location", "LocationName")) or "",
        on_hand=as_float(get_first(payload, "OnHand", "QtyOnHand", "StockOnHand")),
        allocated=as_float(get_first(payload, "Allocated", "QtyAllocated")),
        on_order=as_float(get_first(payload, "OnOrder", "QtyOnOrder")),
    )


# ---------------------------------------------------------------------------
# Bills of materials
# ---------------------------------------------------------------------------

#: GATING UNKNOWN #1. Whether components come back on ``GET`` at all, and
#: under which key, is unverified. ``BOMComponents`` is documented for the
#: ``PUT`` response, so it is the best available guess for ``GET``.
BOM_COMPONENT_KEYS = ("BOMComponents", "BillOfMaterialsComponents", "Components")


def parse_bill_of_materials(
    payload: Mapping[str, Any],
) -> Optional[BillOfMaterials]:
    parent_id = as_str(get_first(payload, "ID", "ProductID", "Id"))
    if not parent_id:
        return None

    raw_components = get_first(payload, *BOM_COMPONENT_KEYS, default=[])
    if not isinstance(raw_components, list):
        raw_components = []

    components: list[BomComponent] = []
    for raw in raw_components:
        if not isinstance(raw, Mapping):
            continue
        component_id = as_str(
            get_first(raw, "ProductID", "ComponentProductID", "ID", "Id")
        )
        if not component_id:
            continue
        quantity = as_float(
            get_first(raw, "Quantity", "Qty", "ComponentQuantity"), default=0.0
        )
        if quantity <= 0:
            # A zero or negative ratio would produce a division error or a
            # nonsensical order quantity. Drop it here; bom.py reports the
            # parent as unusable.
            continue
        components.append(
            BomComponent(component_product_id=component_id, quantity=quantity)
        )

    return BillOfMaterials(
        parent_product_id=parent_id, components=tuple(components)
    )


# ---------------------------------------------------------------------------
# Suppliers
# ---------------------------------------------------------------------------

#: Where additional attributes hang off a supplier record. Unverified: the
#: attributes feature is documented for suppliers, but its API representation
#: is not.
SUPPLIER_ATTRIBUTE_CONTAINER_KEYS = (
    "AdditionalAttributes",
    "Attributes",
    "AdditionalAttributeList",
)


def parse_supplier_id(payload: Mapping[str, Any]) -> Optional[str]:
    return as_str(get_first(payload, "ID", "SupplierID", "Id"))


def parse_supplier_name(payload: Mapping[str, Any]) -> Optional[str]:
    return as_str(get_first(payload, "Name", "SupplierName"))


def extract_supplier_attribute(
    payload: Mapping[str, Any], attribute_name: str
) -> Any:
    """Find a named additional attribute on a supplier record.

    Handles the three shapes Cin7 plausibly uses:

      * a nested list of ``{"Name": ..., "Value": ...}`` objects,
      * a nested mapping of name to value,
      * a flat key on the supplier itself.
    """
    wanted = attribute_name.strip().lower()

    container = get_first(payload, *SUPPLIER_ATTRIBUTE_CONTAINER_KEYS)

    if isinstance(container, list):
        for entry in container:
            if not isinstance(entry, Mapping):
                continue
            name = as_str(get_first(entry, "Name", "AttributeName", "Key"))
            if name and name.strip().lower() == wanted:
                return get_first(entry, "Value", "AttributeValue", "Val")

    if isinstance(container, Mapping):
        for key, value in container.items():
            if str(key).strip().lower() == wanted:
                return value

    # Flat attribute directly on the supplier record.
    for key, value in payload.items():
        if str(key).strip().lower() == wanted:
            return value

    return None


# ---------------------------------------------------------------------------
# Reorder parameters
# ---------------------------------------------------------------------------

#: Cin7's stored reorder point and its companion order quantity.
#: ``MinimumBeforeReorder`` is documented on the Product/ProductFamily
#: structures, so these are better grounded than most keys in this module.
MINIMUM_KEYS = ("MinimumBeforeReorder", "MinimumBeforeReOrder", "ReorderLevel")
REORDER_QUANTITY_KEYS = ("ReorderQuantity", "ReOrderQuantity", "ReorderQty")

#: Where per-location reorder overrides live on a product record.
PRODUCT_LOCATION_CONTAINER_KEYS = (
    "ReorderLevels",
    "Locations",
    "ProductLocations",
    "LocationList",
    "StockLocations",
)


def parse_reorder_parameters(
    product_payload: Mapping[str, Any],
) -> list[ReorderParameters]:
    """Extract reorder points from a product record.

    Returns the product-level default (``location=None``) plus one entry per
    location override. Precedence between them is applied in
    ``reorderpoints.py``, not here — this function only reports what is
    stored.
    """
    product_id = as_str(get_first(product_payload, "ID", "ProductID", "Id"))
    if not product_id:
        return []

    supplier_id = as_str(
        get_first(product_payload, "DefaultSupplierID", "SupplierID")
    )

    results: list[ReorderParameters] = [
        ReorderParameters(
            product_id=product_id,
            supplier_id=supplier_id,
            location=None,
            minimum_before_reorder=as_optional_float(
                get_first(product_payload, *MINIMUM_KEYS)
            ),
            reorder_quantity=as_optional_float(
                get_first(product_payload, *REORDER_QUANTITY_KEYS)
            ),
        )
    ]

    for entry in _location_entries(product_payload):
        location = as_str(get_first(entry, "Location", "LocationName", "Name"))
        if not location:
            continue
        results.append(
            ReorderParameters(
                product_id=product_id,
                supplier_id=supplier_id,
                location=location,
                minimum_before_reorder=as_optional_float(
                    get_first(entry, *MINIMUM_KEYS)
                ),
                reorder_quantity=as_optional_float(
                    get_first(entry, *REORDER_QUANTITY_KEYS)
                ),
            )
        )

    return results


def _location_entries(product_payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Per-location reorder blocks, wherever Cin7 hangs them.

    Checked both directly on the product and nested inside a supplier block,
    since Cin7's UI presents low-stock reorder points on the Suppliers tab
    and the API shape for that is unconfirmed.
    """
    entries: list[Mapping[str, Any]] = []

    direct = get_first(product_payload, *PRODUCT_LOCATION_CONTAINER_KEYS, default=[])
    if isinstance(direct, list):
        entries.extend(e for e in direct if isinstance(e, Mapping))

    suppliers = get_first(
        product_payload, "Suppliers", "ProductSuppliers", "SupplierList", default=[]
    )
    if isinstance(suppliers, list):
        for supplier in suppliers:
            if not isinstance(supplier, Mapping):
                continue
            nested = get_first(
                supplier, *PRODUCT_LOCATION_CONTAINER_KEYS, default=[]
            )
            if isinstance(nested, list):
                entries.extend(e for e in nested if isinstance(e, Mapping))

    return entries


# ---------------------------------------------------------------------------
# Purchase orders
# ---------------------------------------------------------------------------

_STATUS_MAP = {
    "DRAFT": PurchaseStatus.DRAFT,
    "AUTHORISED": PurchaseStatus.AUTHORISED,
    "AUTHORIZED": PurchaseStatus.AUTHORISED,
    "RECEIVED": PurchaseStatus.RECEIVED,
    "COMPLETED": PurchaseStatus.COMPLETED,
    "COMPLETE": PurchaseStatus.COMPLETED,
    "VOIDED": PurchaseStatus.VOIDED,
    "VOID": PurchaseStatus.VOIDED,
}

#: Statuses that mean "no more stock is coming from this PO".
CLOSED_STATUSES = frozenset(
    {PurchaseStatus.VOIDED, PurchaseStatus.RECEIVED, PurchaseStatus.COMPLETED}
)


def parse_status(raw: Any) -> PurchaseStatus:
    text = (as_str(raw) or "").upper()
    # Cin7 combines statuses in list views, e.g. "AUTHORISED / PARTIALLY
    # RECEIVED". Match on the first recognised token rather than the whole
    # string, and fall through to UNKNOWN so callers can surface it.
    for token in text.replace("/", " ").split():
        if token in _STATUS_MAP:
            return _STATUS_MAP[token]
    return _STATUS_MAP.get(text, PurchaseStatus.UNKNOWN)


#: GATING UNKNOWN #3. Whether received quantities are exposed per line, and
#: where. Two shapes are handled: a received quantity on the order line
#: itself, or a separate list of receipt lines to be summed per product.
PURCHASE_ORDER_CONTAINER_KEYS = ("Order", "PurchaseOrder")
PURCHASE_ORDER_LINE_KEYS = ("Lines", "OrderLines", "PurchaseOrderLines")
PURCHASE_RECEIPT_CONTAINER_KEYS = ("StockReceived", "Receive", "Receipts", "Received")
RECEIVED_QUANTITY_KEYS = ("ReceivedQuantity", "QuantityReceived", "Received", "ReceivedQty")


def parse_purchase(payload: Mapping[str, Any]) -> Optional[PurchaseOrder]:
    """Parse a full purchase, netting receipts off order lines."""
    purchase_id = as_str(get_first(payload, "ID", "PurchaseID", "TaskID", "Id"))
    if not purchase_id:
        return None

    raw_status = as_str(
        get_first(payload, "OrderStatus", "Status", "CombinedReceivingStatus")
    ) or ""
    status = parse_status(raw_status)

    order = get_first(payload, *PURCHASE_ORDER_CONTAINER_KEYS)
    order_mapping = order if isinstance(order, Mapping) else payload

    raw_lines = get_first(order_mapping, *PURCHASE_ORDER_LINE_KEYS, default=[])
    if not isinstance(raw_lines, list):
        raw_lines = []

    received_by_product = _sum_received_quantities(payload)

    lines: list[PurchaseLine] = []
    for raw in raw_lines:
        if not isinstance(raw, Mapping):
            continue
        product_id = as_str(get_first(raw, "ProductID", "ID", "Id"))
        if not product_id:
            continue

        ordered = as_float(get_first(raw, "Quantity", "Qty", "OrderQuantity"))

        # Prefer a per-line received figure if the API gives us one; else use
        # the total summed from receipt lines for this product.
        line_received = as_optional_float(get_first(raw, *RECEIVED_QUANTITY_KEYS))
        if line_received is None:
            line_received = received_by_product.get(product_id, 0.0)

        lines.append(
            PurchaseLine(
                product_id=product_id,
                sku=as_str(get_first(raw, "SKU", "Sku", "Code")) or product_id,
                ordered_quantity=ordered,
                received_quantity=line_received,
            )
        )

    return PurchaseOrder(
        id=purchase_id,
        status=status,
        supplier_id=as_str(get_first(payload, "SupplierID", "Supplier")),
        location=as_str(get_first(payload, "Location", "LocationName")) or "",
        reference=as_str(get_first(payload, "Reference", "PurchaseOrderNumber", "Ref")),
        lines=tuple(lines),
        raw_status=raw_status,
    )


def _sum_received_quantities(payload: Mapping[str, Any]) -> dict[str, float]:
    """Total received quantity per product across all receipt lines.

    Cin7 supports partial receipts by appending stock-received lines, so a
    product can legitimately appear several times and must be summed rather
    than overwritten.
    """
    totals: dict[str, float] = {}

    container = get_first(payload, *PURCHASE_RECEIPT_CONTAINER_KEYS)
    receipt_groups: list[Any] = []

    if isinstance(container, list):
        receipt_groups = container
    elif isinstance(container, Mapping):
        receipt_groups = [container]

    for group in receipt_groups:
        if not isinstance(group, Mapping):
            continue
        raw_lines = get_first(group, "Lines", "StockLines", "Items", default=[])
        if not isinstance(raw_lines, list):
            # Some shapes put the line fields directly on the group.
            raw_lines = [group]

        for raw in raw_lines:
            if not isinstance(raw, Mapping):
                continue
            product_id = as_str(get_first(raw, "ProductID", "ID", "Id"))
            if not product_id:
                continue
            quantity = as_float(
                get_first(raw, "Quantity", "Qty", *RECEIVED_QUANTITY_KEYS)
            )
            totals[product_id] = totals.get(product_id, 0.0) + quantity

    return totals


def parse_purchase_list_entry(payload: Mapping[str, Any]) -> tuple[Optional[str], PurchaseStatus, Optional[str]]:
    """Pull (id, status, reference) from a purchaseList row.

    Cheap pre-filter: the list endpoint tells us which purchases are worth
    fetching in full, so closed ones never cost a detail call.
    """
    purchase_id = as_str(get_first(payload, "ID", "PurchaseID", "TaskID", "Id"))
    raw_status = get_first(
        payload, "OrderStatus", "Status", "CombinedReceivingStatus"
    )
    reference = as_str(get_first(payload, "Reference", "PurchaseOrderNumber", "Ref"))
    return purchase_id, parse_status(raw_status), reference


# ---------------------------------------------------------------------------
# Building request payloads
# ---------------------------------------------------------------------------


def build_purchase_payload(
    *,
    supplier_id: str,
    location: str,
    reference: str,
    lines: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Assemble a ``POST /purchase`` body.

    Unverified: Cin7's Purchase POST schema includes many more fields than
    this (tax rules, terms, accounts), and a real account may reject a body
    this sparse. ``probe`` reports the shape of an existing purchase so the
    required extras can be added.
    """
    return {
        "SupplierID": supplier_id,
        "Location": location,
        "Reference": reference,
        "Order": {
            "Lines": [dict(line) for line in lines],
        },
    }


def build_purchase_line(
    *, product_id: str, sku: str, quantity: float
) -> dict[str, Any]:
    return {
        "ProductID": product_id,
        "SKU": sku,
        "Quantity": quantity,
    }


def iter_records(payload: Any) -> Iterable[dict]:
    """Convenience wrapper around :func:`extract_list`."""
    yield from extract_list(payload)
