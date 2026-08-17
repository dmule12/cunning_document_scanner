"""The reorder calculation.

Cin7's own model, read from stored data rather than reconstructed:

    position = on_hand + inbound_base - allocated
    trigger  = position <= MinimumBeforeReorder
    order    = ceil(ReorderQuantity / units_per_pack)     packs

Two things about that are worth being explicit on, because both are easy to
assume otherwise:

* **The minimum is a trigger, not a target.** Reaching it means "order", and
  what you order is the stored `ReorderQuantity` — a fixed amount, not
  however much would lift stock back above the minimum. That is how Cin7's
  low-stock reorder behaves, so our suggestions stay comparable with its own.

* **`inbound_base` comes from :mod:`inbound`, never from Cin7's `OnOrder`.**
  An open purchase order for packs is invisible against the base SKU, so
  `OnOrder` reads zero while stock is in transit.

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
#: without this, a position 1e-13 above the minimum would skip an order that
#: should have fired.
EPSILON = 1e-9


@dataclass(frozen=True)
class Demand:
    """Everything needed to evaluate one product at one location."""

    product: Product
    location: str
    reorder_point: float
    on_hand: float
    allocated: float
    inbound_base: float
    reorder_quantity: Optional[float] = None
    inbound_sources: tuple[str, ...] = ()


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

    # Trigger is inclusive: reaching the minimum means reorder. It is a floor
    # you are not meant to sit on, not a level you are allowed to touch.
    if position > demand.reorder_point + EPSILON:
        return None, SkippedProduct(
            base_product_id=product.id,
            base_sku=product.sku,
            location=demand.location,
            reason=SkipReason.SUFFICIENT_STOCK,
            detail=(
                f"position {position:g} above minimum {demand.reorder_point:g} "
                f"(on hand {demand.on_hand:g}, inbound {demand.inbound_base:g}, "
                f"allocated {demand.allocated:g})"
            ),
        )

    if demand.reorder_quantity is None or demand.reorder_quantity <= 0:
        # A trigger with nothing to fire. Ordering the shortfall instead would
        # silently substitute our judgement for a value someone left blank.
        return None, SkippedProduct(
            base_product_id=product.id,
            base_sku=product.sku,
            location=demand.location,
            reason=SkipReason.NO_REORDER_QUANTITY,
            detail=(
                f"{product.sku} is at or below its minimum of "
                f"{demand.reorder_point:g} but has no reorder quantity set in "
                "Cin7, so there is nothing to order."
            ),
        )

    shortfall = demand.reorder_point - position
    order_base = float(demand.reorder_quantity)

    link = bom.resolve(product.id)
    flags: list[LineFlag] = []

    if link is None:
        # No pack exists, so the base SKU is what gets ordered. Legitimate for
        # products genuinely sold as singles, but also what a missing BOM
        # looks like — hence the flag, so a reviewer can tell them apart.
        order_product_id = product.id
        order_sku = product.sku
        units_per_pack = 1.0
        quantity = _round_up(order_base)
        flags.append(LineFlag.ORDERED_AS_BASE_UNIT)
    else:
        order_product_id = link.pack_product_id
        order_sku = link.pack_product_id
        units_per_pack = link.units_per_pack
        quantity = math.ceil((order_base - EPSILON) / units_per_pack)

    quantity, moq_applied = _apply_moq(order_sku, order_product_id, quantity, config)
    if moq_applied:
        flags.append(LineFlag.MOQ_APPLIED)

    # Cin7's model orders a fixed amount, which may not clear the gap. Say so
    # rather than quietly ordering more: it means the reorder quantity is set
    # too low for current demand, and the product will trigger again next run.
    delivered_base = quantity * units_per_pack
    if position + delivered_base < demand.reorder_point - EPSILON:
        flags.append(LineFlag.BELOW_MINIMUM_AFTER_ORDER)

    if _exceeds_caps(quantity, demand.reorder_quantity, units_per_pack, config):
        flags.append(LineFlag.CAP_EXCEEDED)

    return (
        SuggestedLine(
            base_product_id=product.id,
            base_sku=product.sku,
            order_product_id=order_product_id,
            order_sku=order_sku,
            location=demand.location,
            supplier_id=product.supplier_id,
            reorder_point=demand.reorder_point,
            on_hand=demand.on_hand,
            allocated=demand.allocated,
            inbound_base=demand.inbound_base,
            shortfall=shortfall,
            order_base=order_base,
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
    quantity: float,
    reorder_quantity: Optional[float],
    units_per_pack: float,
    config: Config,
) -> bool:
    """Sanity caps.

    A wrong BOM ratio or a stale reorder point produces a line that looks
    entirely plausible on screen. These caps do not stop the run; they mark
    the line so a reviewer looks.

    The multiple is compared in base units, since ``ReorderQuantity`` is
    stored in base units while ``quantity`` is in packs.
    """
    safety = config.safety

    if safety.max_line_quantity is not None and quantity > safety.max_line_quantity:
        return True

    if (
        safety.max_reorder_quantity_multiple is not None
        and reorder_quantity
        and reorder_quantity > 0
        and quantity * units_per_pack
        > reorder_quantity * safety.max_reorder_quantity_multiple
    ):
        return True

    return False
