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
    """Cin7's per-supplier, per-location reorder settings.

    Cin7's documented precedence: a location-level value wins over the
    supplier-level default; if neither exists, Cin7 cannot generate a
    suggestion at all, and neither can we.
    """

    product_id: str
    supplier_id: str
    location: Optional[str] = None
    lead_days: Optional[float] = None
    safety_days: Optional[float] = None
    reorder_quantity: Optional[float] = None

    @property
    def is_complete(self) -> bool:
        return self.lead_days is not None and self.safety_days is not None


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


class LineFlag(str, Enum):
    """Why a suggested line needs a human's attention."""

    ORDERED_AS_BASE_UNIT = "ordered_as_base_unit"
    CAP_EXCEEDED = "cap_exceeded"
    MOQ_APPLIED = "moq_applied"


class SkipReason(str, Enum):
    """Why a product produced no line at all."""

    MULTIPLE_BOM_PARENTS = "multiple_bom_parents"
    NO_REORDER_PARAMETERS = "no_reorder_parameters"
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

    par: float
    on_hand: float
    allocated: float
    inbound_base: float
    need_base: float

    units_per_pack: float
    quantity: float

    flags: tuple[LineFlag, ...] = ()
    inbound_sources: tuple[str, ...] = ()

    @property
    def is_pack(self) -> bool:
        return self.order_product_id != self.base_product_id


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
    api_calls: int = 0
    aborted: Optional[str] = None
