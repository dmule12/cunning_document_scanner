"""Domain types.

Deliberately plain: these are the shapes the arithmetic works on, decoupled
from whatever Cin7 actually returns. Translation from API payloads lives in
``schema.py`` so that the calculation code never touches a raw response key.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


# ---------------------------------------------------------------------------
# Catalogue
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Product:
    """A product record. May be a base SKU (sleeve) or a pack SKU (box)."""

    id: str
    sku: str
    name: str
    supplier_id: Optional[str] = None
    supplier_name: Optional[str] = None


@dataclass(frozen=True)
class Availability:
    """Stock position for one product at one location.

    ``on_order`` is captured for reporting and diagnostics only. It is
    deliberately NOT used in the reorder calculation: an open purchase order
    for a box SKU does not appear against the sleeve SKU's on-order figure,
    so the number is misleading for exactly the products this tool exists to
    handle. Inbound stock is reconstructed from open POs instead. See
    ``inbound.py``.
    """

    product_id: str
    location: str
    on_hand: float = 0.0
    allocated: float = 0.0
    on_order: float = 0.0

    @property
    def available(self) -> float:
        """Cin7's own definition: available = on hand - allocated."""
        return self.on_hand - self.allocated


@dataclass(frozen=True)
class BomComponent:
    """One component line of an assembly BOM."""

    component_product_id: str
    quantity: float


@dataclass(frozen=True)
class BillOfMaterials:
    """A parent product and the components it decomposes into.

    For our purposes the parent is the box SKU and the (single) component is
    the sleeve SKU, with ``quantity`` being sleeves per box.
    """

    parent_product_id: str
    components: tuple[BomComponent, ...] = ()
    #: The parent's SKU. Carried through because it is what a human reads on
    #: the purchase order and what MOQ overrides are keyed by; a GUID is
    #: neither. Falls back to the id when the record does not carry one.
    parent_sku: str = ""


# ---------------------------------------------------------------------------
# Purchase orders
# ---------------------------------------------------------------------------


class PurchaseStatus(str, Enum):
    """Order status values we care about.

    ``UNKNOWN`` is a real and expected case: Cin7 exposes more statuses than
    this tool models, and an unrecognised one must never be silently treated
    as "safe to ignore". Callers surface it for review.
    """

    DRAFT = "DRAFT"
    AUTHORISED = "AUTHORISED"
    RECEIVED = "RECEIVED"
    COMPLETED = "COMPLETED"
    VOIDED = "VOIDED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class PurchaseLine:
    """One line of a purchase order, with what has actually landed so far."""

    product_id: str
    sku: str
    ordered_quantity: float
    received_quantity: float = 0.0

    @property
    def outstanding_quantity(self) -> float:
        """Quantity still to arrive.

        Received stock has already been auto-disassembled into base units and
        counted in on-hand. Using ``ordered_quantity`` here instead would
        double-count it, understate the shortfall, and silently suppress
        reorders that are genuinely needed.

        Clamped at zero: over-receipts happen, and they must not produce
        negative inbound that inflates a reorder.
        """
        return max(0.0, self.ordered_quantity - self.received_quantity)


@dataclass(frozen=True)
class PurchaseOrder:
    id: str
    status: PurchaseStatus
    supplier_id: Optional[str]
    location: str
    reference: Optional[str] = None
    lines: tuple[PurchaseLine, ...] = ()
    raw_status: str = ""


# ---------------------------------------------------------------------------
# Reorder parameters
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReorderParameters:
    """Cin7's stored reorder settings for a product.

    ``minimum_before_reorder`` is the trigger level and
    ``reorder_quantity`` is how much to order when it is reached — Cin7's own
    low-stock reorder model, read rather than reconstructed.

    ``location`` is ``None`` for the product-level default and set for a
    per-location override, which takes precedence.
    """

    product_id: str
    supplier_id: Optional[str] = None
    location: Optional[str] = None
    minimum_before_reorder: Optional[float] = None
    reorder_quantity: Optional[float] = None

    @property
    def is_complete(self) -> bool:
        """Whether this entry can actually drive an order.

        A minimum of zero counts as unset: Cin7 defaults the field to 0, so
        treating it as a real trigger would pull the whole catalogue into
        scope.
        """
        return (
            self.minimum_before_reorder is not None
            and self.minimum_before_reorder > 0
            and self.reorder_quantity is not None
            and self.reorder_quantity > 0
        )


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


class LineFlag(str, Enum):
    """Why a suggested line needs a human's attention."""

    ORDERED_AS_BASE_UNIT = "ordered_as_base_unit"
    CAP_EXCEEDED = "cap_exceeded"
    MOQ_APPLIED = "moq_applied"
    #: The reorder quantity does not lift stock back above the minimum, so
    #: this product will trigger again on the next run. Usually means the
    #: reorder quantity is set too low in Cin7 for current demand.
    BELOW_MINIMUM_AFTER_ORDER = "below_minimum_after_order"


class SkipReason(str, Enum):
    """Why a product produced no line at all."""

    MULTIPLE_BOM_PARENTS = "multiple_bom_parents"
    NO_REORDER_PARAMETERS = "no_reorder_parameters"
    NO_REORDER_QUANTITY = "no_reorder_quantity"
    NO_SUPPLIER = "no_supplier"
    SUPPLIER_NOT_OPTED_IN = "supplier_not_opted_in"
    SUFFICIENT_STOCK = "sufficient_stock"


@dataclass(frozen=True)
class SuggestedLine:
    """One line the run proposes to put on a draft purchase order."""

    base_product_id: str
    base_sku: str
    order_product_id: str
    order_sku: str
    location: str
    supplier_id: str

    #: Cin7's MinimumBeforeReorder — the trigger, not a target.
    reorder_point: float
    on_hand: float
    allocated: float
    inbound_base: float

    #: How far below the trigger the position sits. Reported for the
    #: reviewer; it does not drive the quantity, because Cin7's model orders
    #: a fixed ReorderQuantity rather than topping up to the minimum.
    shortfall: float
    #: The ReorderQuantity from Cin7, in base units, before pack rounding.
    order_base: float

    units_per_pack: float
    quantity: float

    flags: tuple[LineFlag, ...] = ()
    inbound_sources: tuple[str, ...] = ()

    @property
    def is_pack(self) -> bool:
        return self.order_product_id != self.base_product_id

    @property
    def position(self) -> float:
        """Stock position the trigger was evaluated against."""
        return self.on_hand + self.inbound_base - self.allocated


@dataclass(frozen=True)
class SkippedProduct:
    base_product_id: str
    base_sku: str
    location: Optional[str]
    reason: SkipReason
    detail: str = ""


@dataclass
class RunResult:
    """Everything one run produced. Rendered by ``report.py``."""

    lines: list[SuggestedLine] = field(default_factory=list)
    skipped: list[SkippedProduct] = field(default_factory=list)
    suppliers_considered: list[str] = field(default_factory=list)
    suppliers_skipped: list[str] = field(default_factory=list)
    drafts_created: list[str] = field(default_factory=list)
    drafts_updated: list[str] = field(default_factory=list)
    drafts_left_alone: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    #: Things worth knowing that are not problems — how much of the account
    #: the run actually looked at, and what it deliberately did not. Kept
    #: apart from ``warnings`` so that a run with nothing wrong with it does
    #: not report warnings, which is how warnings stop being read.
    notes: list[str] = field(default_factory=list)
    #: One row per open purchase order read, and what it contributed to
    #: inbound stock. Typed loosely to keep this module free of imports from
    #: the arithmetic; see :class:`cin7_reorder.inbound.InboundAudit`.
    inbound_audit: list = field(default_factory=list)
    api_calls: int = 0
    aborted: Optional[str] = None
