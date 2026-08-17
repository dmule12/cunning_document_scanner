"""The reorder calculation.

    need_base = par - (on_hand + inbound_base - allocated)
    quantity  = ceil(need_base / units_per_pack)     when a pack exists
              = need_base                            otherwise

``inbound_base`` comes from :mod:`inbound`, never from Cin7's ``OnOrder``.

Everything here is pure: no I/O, no API. That is deliberate — this is where
the money is won or lost, so it is the part that gets exhaustively tested
against synthetic fixtures without needing an account.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

from .bom import BomIndex
from .config import Config
from .models import (
    Availability,
    LineFlag,
    Product,
    SkipReason,
    SkippedProduct,
    SuggestedLine,
)

#: Quantities below this are treated as zero. Cin7 returns floats, and float
#: arithmetic on quantities that should be whole numbers leaves dust behind;
#: without this, a need of 1e-13 would round up to a full pack.
EPSILON = 1e-9


@dataclass(frozen=True)
class Demand:
    """Everything needed to evaluate one product at one location."""

    product: Product
    location: str
    par: float
    on_hand: float
    allocated: float
    inbound_base: float
    inbound_sources: tuple[str, ...] = ()
    reorder_quantity: Optional[float] = None


def evaluate(
    demand: Demand,
    bom: BomIndex,
    config: Config,
) -> tuple[Optional[SuggestedLine], Optional[SkippedProduct]]:
    """Decide what, if anything, to order for one product at one location.

    Returns exactly one of (line, None) or (None, skip).
    """
    product = demand.product

    conflict = bom.conflict_for(product.id)
    if conflict is not None:
        return None, SkippedProduct(
            base_product_id=product.id,
            base_sku=product.sku,
            location=demand.location,
            reason=SkipReason.MULTIPLE_BOM_PARENTS,
            detail=(
                f"{product.sku} is a component of "
                f"{len(conflict.pack_product_ids)} packs "
                f"({', '.join(conflict.pack_product_ids)}). Cannot choose one "
                "safely — fix the BOM data."
            ),
        )

    if not product.supplier_id:
        return None, SkippedProduct(
            base_product_id=product.id,
            base_sku=product.sku,
            location=demand.location,
            reason=SkipReason.NO_SUPPLIER,
            detail=f"{product.sku} has no supplier set.",
        )

    position = demand.on_hand + demand.inbound_base - demand.allocated
    need_base = demand.par - position

    if need_base <= EPSILON:
        return None, SkippedProduct(
            base_product_id=product.id,
            base_sku=product.sku,
            location=demand.location,
            reason=SkipReason.SUFFICIENT_STOCK,
            detail=(
                f"position {position:g} >= par {demand.par:g} "
                f"(on hand {demand.on_hand:g}, inbound {demand.inbound_base:g}, "
                f"allocated {demand.allocated:g})"
            ),
        )

    link = bom.resolve(product.id)
    flags: list[LineFlag] = []

    if link is None:
        # No pack exists, so the base SKU is what gets ordered. Legitimate for
        # products genuinely sold as singles, but also what a missing BOM
        # looks like — hence the flag, so a reviewer can tell them apart.
        order_product_id = product.id
        order_sku = product.sku
        units_per_pack = 1.0
        quantity = _round_up(need_base)
        flags.append(LineFlag.ORDERED_AS_BASE_UNIT)
    else:
        order_product_id = link.pack_product_id
        order_sku = link.pack_product_id
        units_per_pack = link.units_per_pack
        quantity = math.ceil((need_base - EPSILON) / units_per_pack)

    quantity, moq_applied = _apply_moq(order_sku, order_product_id, quantity, config)
    if moq_applied:
        flags.append(LineFlag.MOQ_APPLIED)

    if _exceeds_caps(quantity, demand.reorder_quantity, config):
        flags.append(LineFlag.CAP_EXCEEDED)

    return (
        SuggestedLine(
            base_product_id=product.id,
            base_sku=product.sku,
            order_product_id=order_product_id,
            order_sku=order_sku,
            location=demand.location,
            supplier_id=product.supplier_id,
            par=demand.par,
            on_hand=demand.on_hand,
            allocated=demand.allocated,
            inbound_base=demand.inbound_base,
            need_base=need_base,
            units_per_pack=units_per_pack,
            quantity=float(quantity),
            flags=tuple(flags),
            inbound_sources=demand.inbound_sources,
        ),
        None,
    )


def availability_for(
    availability: dict[tuple[str, str], Availability],
    product_id: str,
    location: str,
) -> Availability:
    """Look up stock, treating an absent row as all zeros.

    ``productAvailability`` omits records where on-hand, available and
    on-order are all zero. A product that has sold out completely with
    nothing on order therefore vanishes from the response — and that is
    precisely the product most urgently needing a purchase order.

    Driving the calculation from this function, with the product list as the
    spine, is what stops the run silently ignoring everything it most needs
    to reorder.
    """
    found = availability.get((product_id, location))
    if found is not None:
        return found
    return Availability(product_id=product_id, location=location)


def _round_up(value: float) -> int:
    return int(math.ceil(value - EPSILON))


def _apply_moq(
    order_sku: str, order_product_id: str, quantity: int, config: Config
) -> tuple[int, bool]:
    moq = config.moq.get(order_sku)
    if moq is None:
        moq = config.moq.get(order_product_id)
    if moq is None or quantity >= moq:
        return quantity, False
    return int(math.ceil(moq)), True


def _exceeds_caps(
    quantity: float, reorder_quantity: Optional[float], config: Config
) -> bool:
    """Sanity caps.

    A wrong BOM ratio or a stale par level produces a line that looks
    entirely plausible on screen. These caps do not stop the run; they mark
    the line so a reviewer looks at it before the PO is sent.
    """
    safety = config.safety

    if safety.max_line_quantity is not None and quantity > safety.max_line_quantity:
        return True

    if (
        safety.max_reorder_quantity_multiple is not None
        and reorder_quantity
        and reorder_quantity > 0
        and quantity > reorder_quantity * safety.max_reorder_quantity_multiple
    ):
        return True

    return False
