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

from dataclasses import dataclass
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

#: Cin7 hides each nested collection on a product behind its own opt-in query
#: flag. Without them the collections come back as empty lists on both the
#: list and the by-ID read, which looks exactly like "this product has no
#: bill of materials" — a silent, expensive misreading.
#:
#: All three confirmed against a live account by sweeping candidate flags.
#: ``IncludeAll=true`` does NOT work — each collection needs its own.
PRODUCT_INCLUDE_FLAGS = {
    "IncludeBOM": "true",  # -> BillOfMaterialsProducts
    "IncludeReorderLevels": "true",  # -> ReorderLevels
    "IncludeSuppliers": "true",  # -> Suppliers
}
#: Confirmed against a live account. Note the ``ref/`` prefix — the bare
#: ``productAvailability`` path that Cin7's own documentation implies returns
#: a not-found redirect. The alternatives are kept as fallbacks in case other
#: accounts or API versions differ.
ENDPOINT_PRODUCT_AVAILABILITY = "ref/productAvailability"

AVAILABILITY_ENDPOINT_CANDIDATES = (
    "ref/productAvailability",
    "productAvailability",
    "ProductAvailability",
    "productavailability",
    "availability",
    "stockAvailability",
    "productAvailabilityList",
    "stockOnHand",
    "stockLevels",
)
ENDPOINT_BILL_OF_MATERIALS = "BillOfMaterials"
ENDPOINT_SUPPLIER = "supplier"
ENDPOINT_LOCATION = "ref/location"
ENDPOINT_PURCHASE_LIST = "purchaseList"
ENDPOINT_PURCHASE = "purchase"

#: Order lines live on their own sub-resource, not in the POST /purchase body.
#:
#: Confirmed the expensive way: POST /purchase with an ``Order`` block answers
#: 200 and creates a purchase order with no lines on it. No error, no warning —
#: the same "valid request, orders nothing" failure the payload builder guards
#: against in config. Cin7 models a purchase as a header plus the tabs you see
#: in its UI (Order, Stock received, Invoice), and each is written separately.
ENDPOINT_PURCHASE_ORDER = "purchase/order"

#: Cin7 has more than one kind of purchase order. ``/purchase`` answers a
#: 400 for Advanced and Service purchases, telling you to use this instead.
#: Both are needed: neither endpoint serves every purchase.
ENDPOINT_ADVANCED_PURCHASE = "AdvancedPurchase"
ADVANCED_PURCHASE_CANDIDATES = (
    "AdvancedPurchase",
    "advancedPurchase",
    "advancedpurchase",
    "AdvancedPurchases",
    "advancedPurchases",
    "advanced-purchase",
    "purchase/advanced",
    "purchaseAdvanced",
    "ref/advancedPurchase",
    "advancedPurchaseOrder",
)

#: Marker in the 400 body that means "wrong endpoint for this purchase type",
#: as opposed to a genuinely bad request.
DEPRECATED_ENDPOINT_MARKER = "AdvancedPurchase endpoint"

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

    supplier_id, supplier_name = _parse_default_supplier(payload)

    return Product(
        id=product_id,
        sku=as_str(get_first(payload, "SKU", "Sku", "Code")) or product_id,
        name=as_str(get_first(payload, "Name", "ProductName", "Description")) or "",
        supplier_id=supplier_id,
        supplier_name=supplier_name,
    )


def _parse_default_supplier(
    payload: Mapping[str, Any],
) -> tuple[Optional[str], Optional[str]]:
    """The supplier to order this product from.

    A product record carries no ``DefaultSupplierID`` field — the supplier
    lives in the ``Suppliers`` collection, which only appears when
    ``IncludeSuppliers=true`` is sent. Without that flag every product looks
    supplier-less and the whole run skips silently.

    Where several suppliers exist, one flagged as default wins; otherwise the
    first is used, which matches how Cin7's own reorder picks one.
    """
    # A flat field, in case some accounts or versions expose one.
    flat_id = as_str(get_first(payload, "DefaultSupplierID", "SupplierID"))
    if flat_id:
        return flat_id, as_str(get_first(payload, "DefaultSupplier", "Supplier"))

    suppliers = get_first(
        payload, "Suppliers", "ProductSuppliers", "SupplierList", default=[]
    )
    if not isinstance(suppliers, list) or not suppliers:
        return None, None

    entries = [s for s in suppliers if isinstance(s, Mapping)]
    if not entries:
        return None, None

    default = next(
        (
            s
            for s in entries
            if get_first(s, "IsDefault", "Default", "IsPreferred") is True
        ),
        entries[0],
    )

    return (
        as_str(get_first(default, "SupplierID", "ID", "Id")),
        as_str(get_first(default, "SupplierName", "Name", "Supplier")),
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

#: RESOLVED against a live account: bills of materials are not a separate
#: endpoint. Every product record carries its own, under
#: ``BillOfMaterialsProducts``, alongside ``BillOfMaterial`` (a boolean) and
#: ``BOMType``. Earlier revisions hunted for a /BillOfMaterials endpoint that
#: does not exist.
BOM_COMPONENT_KEYS = (
    "BillOfMaterialsProducts",
    "BOMComponents",
    "BillOfMaterialsComponents",
    "Components",
)

#: Flags on the product record marking it as an assembly/pack.
BOM_FLAG_KEYS = ("BillOfMaterial", "IsBillOfMaterial")
BOM_TYPE_KEYS = ("BOMType", "BillOfMaterialType")

#: ``BOMType`` values meaning "no bill of materials".
BOM_TYPE_NONE = {"", "none", "null"}


def product_has_bom(payload: Mapping[str, Any]) -> bool:
    """Whether a product record carries a usable bill of materials.

    Checks the components list rather than trusting the flags alone: a
    product can be marked as an assembly while its component list is empty,
    and an empty BOM is no use for mapping a pack to its base units.
    """
    components = get_first(payload, *BOM_COMPONENT_KEYS)
    if isinstance(components, list) and components:
        return True

    flag = get_first(payload, *BOM_FLAG_KEYS)
    if flag is True:
        return True

    bom_type = (as_str(get_first(payload, *BOM_TYPE_KEYS)) or "").strip().lower()
    return bool(bom_type) and bom_type not in BOM_TYPE_NONE


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
        parent_product_id=parent_id,
        components=tuple(components),
        parent_sku=as_str(get_first(payload, "SKU", "ProductCode")) or parent_id,
    )


# ---------------------------------------------------------------------------
# Suppliers
# ---------------------------------------------------------------------------

#: Cin7 exposes supplier additional attributes as ten flat numbered slots —
#: ``AdditionalAttribute1`` .. ``AdditionalAttribute10`` — alongside an
#: ``AttributeSet`` naming which set is in use. Confirmed against a live
#: account. The human-readable name of each slot lives in the attribute set
#: definition, not on the supplier record, so a slot cannot be resolved from
#: a name without a second lookup; config names the slot directly instead.
SUPPLIER_ATTRIBUTE_SLOTS = tuple(f"AdditionalAttribute{n}" for n in range(1, 11))

#: Older/alternative shapes, kept as a fallback in case some accounts or API
#: versions nest attributes instead.
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
    """Read an additional attribute from a supplier record.

    ``attribute_name`` may be either a slot name (``AdditionalAttribute1``),
    which is how Cin7 actually exposes these, or a human-readable label for
    the nested shapes some accounts may use.

    Returns ``None`` when absent or blank. Blank matters: an unset slot comes
    back as an empty string, and treating that as a value would opt every
    supplier into automation.
    """
    wanted = attribute_name.strip().lower()

    # The shape confirmed against a live account: a flat numbered slot.
    for slot in SUPPLIER_ATTRIBUTE_SLOTS:
        if slot.lower() == wanted:
            return _blank_to_none(get_first(payload, slot))

    container = get_first(payload, *SUPPLIER_ATTRIBUTE_CONTAINER_KEYS)

    if isinstance(container, list):
        for entry in container:
            if not isinstance(entry, Mapping):
                continue
            name = as_str(get_first(entry, "Name", "AttributeName", "Key"))
            if name and name.strip().lower() == wanted:
                return _blank_to_none(
                    get_first(entry, "Value", "AttributeValue", "Val")
                )

    if isinstance(container, Mapping):
        for key, value in container.items():
            if str(key).strip().lower() == wanted:
                return _blank_to_none(value)

    # Flat attribute directly on the supplier record, by label.
    for key, value in payload.items():
        if str(key).strip().lower() == wanted:
            return _blank_to_none(value)

    return None


def supplier_attribute_slots(payload: Mapping[str, Any]) -> dict[str, str]:
    """Every populated attribute slot on a supplier, for the probe to show.

    The slot names are opaque (``AdditionalAttribute3``), so seeing which
    ones hold values is the only practical way to work out which slot to
    point the config at.
    """
    found: dict[str, str] = {}
    for slot in SUPPLIER_ATTRIBUTE_SLOTS:
        value = _blank_to_none(get_first(payload, slot))
        if value is not None:
            found[slot] = str(value)
    return found


def _blank_to_none(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    return value


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
    # Stages on the way to authorised. Open: stock is still expected.
    "ORDERED": PurchaseStatus.AUTHORISED,
    "ORDERING": PurchaseStatus.AUTHORISED,
    "RECEIVING": PurchaseStatus.AUTHORISED,
    # Open, not closed. An invoice can be entered before the goods arrive, so
    # INVOICED says something about the paperwork and nothing about the
    # stock. Whether anything is still coming is settled per line, by
    # subtracting what has been received from what was ordered.
    "INVOICED": PurchaseStatus.AUTHORISED,
    "RECEIVED": PurchaseStatus.RECEIVED,
    "COMPLETED": PurchaseStatus.COMPLETED,
    "COMPLETE": PurchaseStatus.COMPLETED,
    # Confirmed on a live account: 32 orders carried OrderStatus=CLOSED and
    # were read as UNKNOWN, which means open, which means a detail call each.
    "CLOSED": PurchaseStatus.COMPLETED,
    "VOIDED": PurchaseStatus.VOIDED,
    "VOID": PurchaseStatus.VOIDED,
}

#: Statuses that mean "no more stock is coming from this PO".
CLOSED_STATUSES = frozenset(
    {PurchaseStatus.VOIDED, PurchaseStatus.RECEIVED, PurchaseStatus.COMPLETED}
)

#: Receiving statuses on a list row that mean everything has arrived.
#:
#: Matched against the whole normalised string and never against a token,
#: because "PARTIALLY RECEIVED" contains "RECEIVED" and means the opposite —
#: stock is still coming, and treating it as closed would understate inbound
#: and re-order goods in transit.
FULLY_RECEIVED_STATUSES = frozenset(
    {"RECEIVED", "FULLY RECEIVED", "FULL", "COMPLETE", "COMPLETED"}
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

    marker_reference, marker_fingerprint = split_marker(
        as_str(get_first(payload, *PURCHASE_MARKER_KEYS))
        or as_str(get_first(order_mapping, "Memo", "Reference"))
    )

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
        # Not OrderNumber: that is Cin7's own PO-81146, present on every
        # purchase ever raised, and matching on it would claim the lot.
        reference=marker_reference,
        fingerprint=marker_fingerprint,
        # The order stage's own status. Only read when there really is an
        # Order block: order_mapping falls back to the payload itself, and
        # taking the top-level Status here would defeat the whole point of
        # distinguishing the two.
        order_status=(
            as_str(get_first(order, "Status")) or ""
            if isinstance(order, Mapping)
            else ""
        ),
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


@dataclass(frozen=True)
class PurchaseListEntry:
    """One row of ``purchaseList``, before any detail is fetched.

    Every field here is a chance to avoid a detail call. On a real account
    the list runs to thousands of rows and each detail costs one request —
    two for Advanced purchases, which answer a 400 first — so filtering here
    is the difference between a run that finishes and one that exhausts the
    daily quota.
    """

    id: Optional[str]
    status: PurchaseStatus
    reference: Optional[str] = None
    supplier_id: Optional[str] = None
    supplier_name: Optional[str] = None
    receiving_status: Optional[str] = None
    #: Cin7's own lifecycle status, which is a different field from the order
    #: status and frequently disagrees with it. On a live account 1966 orders
    #: read ``Status=COMPLETED`` while ``OrderStatus=AUTHORISED``.
    lifecycle_status: PurchaseStatus = PurchaseStatus.UNKNOWN
    #: "Simple Purchase", "Advanced Purchase", "Service Purchase".
    order_type: Optional[str] = None

    @property
    def is_closed(self) -> bool:
        """Whether this order can have no more stock on its way.

        A single status field is not enough, because Cin7 keeps three of them
        and they disagree. An order placed, received and invoiced in 2019 can
        still read ``OrderStatus=AUTHORISED`` — arrival is tracked separately.
        Taking that at face value makes almost every purchase the account has
        ever raised look open, and each one then costs a detail call.

        So: closed if either status says so, or if the receiving status says
        everything has arrived. Anything else is open, including a receiving
        status this code does not recognise — being wrong in that direction
        costs a call, and being wrong in the other costs a duplicate order.
        """
        if self.status in CLOSED_STATUSES:
            return True
        if self.lifecycle_status in CLOSED_STATUSES:
            return True
        if self.receiving_status is None:
            return False
        return self.receiving_status.strip().upper() in FULLY_RECEIVED_STATUSES

    @property
    def is_advanced(self) -> Optional[bool]:
        """Whether ``/purchase`` will refuse this one, if the row says.

        ``None`` means the row does not say and both endpoints must be tried.
        Knowing in advance is worth a call per purchase: ``/purchase`` answers
        an Advanced or Service purchase with a 400 naming the right endpoint,
        so asking it first is a wasted request.
        """
        if not self.order_type:
            return None
        lowered = self.order_type.strip().lower()
        if "advanced" in lowered or "service" in lowered:
            return True
        if "simple" in lowered:
            return False
        return None

    @property
    def names_a_supplier(self) -> bool:
        """Whether the row says anything at all about who it is from."""
        return bool(self.supplier_id or self.supplier_name)

    def is_for_supplier(
        self, supplier_ids: Iterable[str], names: Iterable[str]
    ) -> bool:
        """Whether this row belongs to one of the suppliers in scope.

        Returns ``True`` when the row carries no supplier information at all:
        the caller must then fetch the detail to find out, and wrongly
        skipping an open order would understate inbound stock.

        ``names`` are compared lower-cased, because the list endpoint and the
        supplier endpoint do not reliably agree on capitalisation.
        """
        if self.supplier_id and self.supplier_id in set(supplier_ids):
            return True
        if self.supplier_name and self.supplier_name.strip().lower() in set(names):
            return True
        return not self.names_a_supplier


def parse_purchase_list_entry(payload: Mapping[str, Any]) -> PurchaseListEntry:
    """Pull the pre-filterable fields from a purchaseList row."""
    return PurchaseListEntry(
        id=as_str(get_first(payload, "ID", "PurchaseID", "TaskID", "Id")),
        status=parse_status(
            get_first(payload, "OrderStatus", "Status", "CombinedReceivingStatus")
        ),
        reference=as_str(
            get_first(payload, "Reference", "PurchaseOrderNumber", "Ref")
        ),
        supplier_id=as_str(get_first(payload, "SupplierID", "SupplierId")),
        supplier_name=as_str(get_first(payload, "Supplier", "SupplierName")),
        receiving_status=as_str(
            get_first(
                payload,
                "CombinedReceivingStatus",
                "ReceivingStatus",
                "ReceiptStatus",
                "StockReceivedStatus",
            )
        ),
        lifecycle_status=parse_status(get_first(payload, "Status")),
        order_type=as_str(get_first(payload, "Type", "PurchaseType", "OrderType")),
    )


# ---------------------------------------------------------------------------
# Building request payloads
# ---------------------------------------------------------------------------


#: Required on every POST /purchase. Confirmed live twice over: omitting it
#: answers ``Required Attribute 'Approach' not specified``, and every purchase
#: on the account carries ``"STOCK"``.
#:
#: Read off a real order rather than guessed. "SIMPLE" was the guess, and it
#: was wrong — the docs describe the concept, not the string.
PURCHASE_APPROACH = "STOCK"

#: Order status for what we create. Never anything else: this tool exists to
#: put a draft in front of a person, and an authorised purchase order is one
#: nobody agreed to.
DRAFT_ORDER_STATUS = "DRAFT"

#: Where the "we made this" marker is written, and read back from.
#:
#: ``Reference`` is what the marker was originally written to, and a real
#: purchase record does not carry that field at all — Cin7 has ``OrderNumber``
#: (its own PO-81146) and ``Note``. Had that gone unnoticed, every run would
#: have failed to recognise its own standing draft and raised another one:
#: duplicate purchase orders, twice a week, from the tool built to prevent
#: exactly that.
#:
#: So it goes in several places and is read back from all of them.
#:
#: Confirmed live: ``Note`` and ``Order.Memo`` both survive the write.
PURCHASE_MARKER_KEYS = ("Reference", "Note", "Ref")

#: Separates the reference from the fingerprint inside the marker.
#:
#: The fingerprint records what this tool last wrote, so a human's edit to a
#: draft can be told from its own work and left alone. It used to live in a
#: local JSON file, which meant it did not exist on a fresh checkout, did not
#: survive a CI cache eviction — or a cache that simply failed to save, which
#: is what happened — and differed between a laptop and a scheduled run.
#:
#: Keeping it on the purchase order removes all of that: the record carries
#: its own history, and every runner reads the same truth.
MARKER_FINGERPRINT_SEP = " fp="


def build_marker(reference: str, fingerprint: Optional[str] = None) -> str:
    """The string written to the marker fields."""
    if not fingerprint:
        return reference
    return f"{reference}{MARKER_FINGERPRINT_SEP}{fingerprint}"


def split_marker(text: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """Recover ``(reference, fingerprint)`` from a marker string.

    Tolerates a marker with no fingerprint — drafts written by earlier
    versions have none, and they must still be recognised as ours rather than
    read as somebody else's work.
    """
    if not text:
        return None, None
    reference, separator, fingerprint = text.partition(MARKER_FINGERPRINT_SEP)
    if not separator:
        return text.strip() or None, None
    return reference.strip() or None, fingerprint.strip() or None


def build_purchase_payload(
    *,
    supplier_id: str,
    location: str,
    reference: str,
    order_date: Optional[str] = None,
    extra: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """Assemble a ``POST /purchase`` body — the header only.

    Takes no lines, deliberately. Cin7 accepts them here, answers 200, and
    creates a purchase order with nothing on it; a parameter that looks like
    it works is how that happened once already.
    See :func:`build_order_payload`.

    Cin7 validates this one attribute at a time — each rejection names a
    single missing field and says nothing about the next — so the required
    set has to be discovered by trial. ``dump --purchase <id>`` prints a real
    purchase from the account, which is a better source than guessing: the
    fields an existing order carries are the fields this account needs.

    ``extra`` is merged in last, so anything else the account turns out to
    demand can be added from `purchase.extra_fields` in config.yaml without a
    code change.
    """
    payload: dict[str, Any] = {
        "SupplierID": supplier_id,
        "Location": location,
        "Approach": PURCHASE_APPROACH,
    }
    # The marker, everywhere it might survive. Whichever field this account
    # keeps is the one that lets the next run find this draft again.
    for key in PURCHASE_MARKER_KEYS:
        payload[key] = reference
    if order_date:
        payload["OrderDate"] = order_date
    if extra:
        for key, value in extra.items():
            # An `Order` block here is silently ignored by Cin7 anyway, and
            # accepting one would suggest it did something. Lines go to
            # ENDPOINT_PURCHASE_ORDER.
            if key == "Order":
                continue
            payload[key] = value
    return payload


def build_order_payload(
    *,
    purchase_id: str,
    reference: str,
    lines: Sequence[Mapping[str, Any]],
    fingerprint: Optional[str] = None,
    extra: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """Assemble a ``POST``/``PUT /purchase/order`` body — the lines themselves.

    ``Status`` is set here and is never taken from config: it is the whole
    difference between a suggestion and a commitment, and nothing outside this
    function may set it to anything but DRAFT.
    """
    payload: dict[str, Any] = dict(extra or {})
    payload.update(
        {
            "TaskID": purchase_id,
            "Memo": build_marker(reference, fingerprint),
            "Status": DRAFT_ORDER_STATUS,
            "Lines": [dict(line) for line in lines],
        }
    )
    return payload


def build_purchase_line(
    *,
    product_id: str,
    sku: str,
    quantity: float,
    extra: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """One order line.

    ``extra`` carries whatever this account requires on a line — a tax rule,
    typically — from `purchase.line_fields` in config.yaml. Product, SKU and
    quantity are ours and cannot be overridden: they are the entire content of
    the decision, and a config file has no business changing them.
    """
    line = dict(extra or {})
    line.update(
        {
            "ProductID": product_id,
            "SKU": sku,
            "Quantity": quantity,
        }
    )
    return line


def iter_records(payload: Any) -> Iterable[dict]:
    """Convenience wrapper around :func:`extract_list`."""
    yield from extract_list(payload)
