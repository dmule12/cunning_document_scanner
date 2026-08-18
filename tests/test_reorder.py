"""The reorder calculation.

Cin7's model: the minimum is a *trigger*, and what gets ordered is the stored
ReorderQuantity — a fixed amount, not however much would close the gap.
"""

from __future__ import annotations

from cin7_reorder.config import Config, SafetyConfig
from cin7_reorder.models import Availability, LineFlag, Product, SkipReason
from cin7_reorder.reorder import Demand, availability_for, evaluate

from .conftest import BOX, LOCATION, SINGLE, SLEEVE, UNITS_PER_BOX


def demand(product: Product, **overrides) -> Demand:
    base = dict(
        product=product,
        location=LOCATION,
        reorder_point=100.0,
        on_hand=0.0,
        allocated=0.0,
        inbound_base=0.0,
        reorder_quantity=48.0,
        inbound_sources=(),
    )
    base.update(overrides)
    return Demand(**base)


# ---------------------------------------------------------------------------
# The trigger
# ---------------------------------------------------------------------------


def test_position_above_minimum_does_not_order(bom, config, sleeve_product):
    line, skip = evaluate(
        demand(sleeve_product, reorder_point=100, on_hand=120), bom, config
    )
    assert line is None
    assert skip.reason is SkipReason.SUFFICIENT_STOCK


def test_position_exactly_at_minimum_orders(bom, config, sleeve_product):
    """A minimum is a floor you are not meant to sit on.

    Reaching it triggers; only being strictly above it does not.
    """
    line, skip = evaluate(
        demand(sleeve_product, reorder_point=100, on_hand=100), bom, config
    )
    assert skip is None
    assert line is not None


def test_float_dust_at_the_minimum_still_triggers(bom, config, sleeve_product):
    """Cin7 returns floats, so "exactly at the minimum" is fuzzy.

    A position within epsilon of the minimum is treated as having reached it.
    Erring this way orders very slightly early; the alternative is a product
    that sits a billionth above its trigger and is never reordered.
    """
    line, _ = evaluate(
        demand(sleeve_product, reorder_point=100.0, on_hand=100.0000000001),
        bom,
        config,
    )
    assert line is not None


def test_position_meaningfully_above_the_minimum_does_not_trigger(
    bom, config, sleeve_product
):
    line, skip = evaluate(
        demand(sleeve_product, reorder_point=100.0, on_hand=100.01), bom, config
    )
    assert line is None
    assert skip.reason is SkipReason.SUFFICIENT_STOCK


def test_inbound_stock_counts_towards_the_position(bom, config, sleeve_product):
    """The whole reason inbound is reconstructed: don't reorder twice.

    Reported under its own reason, not lumped in with genuine sufficiency.
    60 on the shelf against a minimum of 100 is short — Cin7's own low-stock
    reorder would raise this, because the boxes on their way sit against the
    pack SKU and never show against the sleeve. Counting these separately is
    the only visible evidence the reconstruction is doing anything at all.
    """
    line, skip = evaluate(
        demand(sleeve_product, reorder_point=100, on_hand=60, inbound_base=48),
        bom,
        config,
    )
    assert line is None
    assert skip.reason is SkipReason.COVERED_BY_INBOUND
    assert "already on its way" in skip.detail


def test_plenty_on_the_shelf_is_not_credited_to_inbound(bom, config, sleeve_product):
    """Don't claim credit for a duplicate order that was never going to happen.

    Stock above the minimum without counting anything inbound would not have
    triggered either way, so it is ordinary sufficiency — inflating the
    prevented-duplicates count with these would make the number meaningless.
    """
    line, skip = evaluate(
        demand(sleeve_product, reorder_point=100, on_hand=200, inbound_base=48),
        bom,
        config,
    )
    assert line is None
    assert skip.reason is SkipReason.SUFFICIENT_STOCK


def test_allocated_stock_reduces_the_position(bom, config, sleeve_product):
    """Stock promised to orders is not available to cover the minimum."""
    line, _ = evaluate(
        demand(sleeve_product, reorder_point=100, on_hand=120, allocated=30),
        bom,
        config,
    )
    assert line is not None
    assert line.position == 90


# ---------------------------------------------------------------------------
# The quantity
# ---------------------------------------------------------------------------


def test_orders_the_reorder_quantity_not_the_shortfall(bom, config, sleeve_product):
    """Cin7's model orders a fixed amount.

    Position 0 against a minimum of 100 is a shortfall of 100, but the stored
    reorder quantity is 48, so that is what gets ordered — 2 boxes.
    """
    line, _ = evaluate(
        demand(sleeve_product, reorder_point=100, on_hand=0, reorder_quantity=48),
        bom,
        config,
    )
    assert line.shortfall == 100
    assert line.order_base == 48
    assert line.quantity == 2


def test_orders_the_pack_sku_not_the_base_sku(bom, config, sleeve_product):
    line, _ = evaluate(demand(sleeve_product), bom, config)
    assert line.order_product_id == BOX
    assert line.base_product_id == SLEEVE
    assert line.is_pack is True
    assert line.units_per_pack == UNITS_PER_BOX


def test_rounds_up_to_whole_packs(bom, config, sleeve_product):
    """A reorder quantity of 30 sleeves at 24 per box is 2 boxes."""
    line, _ = evaluate(demand(sleeve_product, reorder_quantity=30), bom, config)
    assert line.quantity == 2


def test_exact_multiple_does_not_round_up(bom, config, sleeve_product):
    line, _ = evaluate(demand(sleeve_product, reorder_quantity=48), bom, config)
    assert line.quantity == 2


def test_flags_when_the_order_will_not_clear_the_shortfall(bom, config, sleeve_product):
    """Reorder quantity set too low for current demand.

    Ordering more would override a number someone deliberately set, so the
    tool orders what it was told and says the product will trigger again.
    """
    line, _ = evaluate(
        demand(sleeve_product, reorder_point=500, on_hand=0, reorder_quantity=48),
        bom,
        config,
    )
    assert LineFlag.BELOW_MINIMUM_AFTER_ORDER in line.flags


def test_no_flag_when_the_order_clears_the_shortfall(bom, config, sleeve_product):
    line, _ = evaluate(
        demand(sleeve_product, reorder_point=100, on_hand=90, reorder_quantity=48),
        bom,
        config,
    )
    assert LineFlag.BELOW_MINIMUM_AFTER_ORDER not in line.flags


# ---------------------------------------------------------------------------
# Fallbacks and skips
# ---------------------------------------------------------------------------


def test_minimum_reached_but_no_reorder_quantity_is_skipped(
    bom, config, sleeve_product
):
    """A trigger with nothing to fire.

    Substituting the shortfall would quietly replace a value someone left
    blank with our own judgement.
    """
    line, skip = evaluate(
        demand(sleeve_product, reorder_quantity=None), bom, config
    )
    assert line is None
    assert skip.reason is SkipReason.NO_REORDER_QUANTITY


def test_zero_reorder_quantity_is_skipped(bom, config, sleeve_product):
    line, skip = evaluate(demand(sleeve_product, reorder_quantity=0), bom, config)
    assert line is None
    assert skip.reason is SkipReason.NO_REORDER_QUANTITY


def test_product_without_pack_orders_base_units_and_is_flagged(
    bom, config, single_product
):
    line, _ = evaluate(demand(single_product, reorder_quantity=37), bom, config)
    assert line.order_product_id == SINGLE
    assert line.quantity == 37
    assert line.is_pack is False
    assert LineFlag.ORDERED_AS_BASE_UNIT in line.flags


def test_conflicting_bom_parents_skips_the_product(config, sleeve_product):
    from cin7_reorder.bom import BomIndex
    from cin7_reorder.models import BillOfMaterials, BomComponent

    conflicted = BomIndex.build(
        [
            BillOfMaterials(
                parent_product_id="box-12",
                components=(BomComponent(component_product_id=SLEEVE, quantity=12),),
            ),
            BillOfMaterials(
                parent_product_id="box-24",
                components=(BomComponent(component_product_id=SLEEVE, quantity=24),),
            ),
        ]
    )

    line, skip = evaluate(demand(sleeve_product), conflicted, config)
    assert line is None
    assert skip.reason is SkipReason.MULTIPLE_BOM_PARENTS
    assert "box-12" in skip.detail


def test_product_without_supplier_is_skipped(bom, config):
    orphan = Product(id=SLEEVE, sku="SLV-001", name="Sleeve", supplier_id=None)
    line, skip = evaluate(demand(orphan), bom, config)
    assert line is None
    assert skip.reason is SkipReason.NO_SUPPLIER


# ---------------------------------------------------------------------------
# MOQ and safety caps
# ---------------------------------------------------------------------------


def test_moq_raises_a_small_order(bom, sleeve_product):
    config = Config(
        moq={BOX: 3},
        safety=SafetyConfig(
            max_line_quantity=None,
            max_reorder_quantity_multiple=None,
            max_total_lines=None,
        ),
    )
    line, _ = evaluate(demand(sleeve_product, reorder_quantity=24), bom, config)
    assert line.quantity == 3
    assert LineFlag.MOQ_APPLIED in line.flags


def test_moq_does_not_lower_a_larger_order(bom, sleeve_product):
    config = Config(
        moq={BOX: 2},
        safety=SafetyConfig(
            max_line_quantity=None,
            max_reorder_quantity_multiple=None,
            max_total_lines=None,
        ),
    )
    line, _ = evaluate(demand(sleeve_product, reorder_quantity=240), bom, config)
    assert line.quantity == 10
    assert LineFlag.MOQ_APPLIED not in line.flags


def test_absurd_quantity_trips_the_line_cap(bom, sleeve_product):
    config = Config(
        safety=SafetyConfig(
            max_line_quantity=10,
            max_reorder_quantity_multiple=None,
            max_total_lines=None,
        )
    )
    line, _ = evaluate(demand(sleeve_product, reorder_quantity=10_000), bom, config)
    assert LineFlag.CAP_EXCEEDED in line.flags


def test_moq_far_above_the_reorder_quantity_trips_the_multiple_cap(
    bom, sleeve_product
):
    """The multiple is compared in base units.

    ReorderQuantity is stored in base units while the line quantity is in
    packs, so comparing them directly would fire on almost every line.
    """
    config = Config(
        moq={BOX: 20},
        safety=SafetyConfig(
            max_line_quantity=None,
            max_reorder_quantity_multiple=3,
            max_total_lines=None,
        ),
    )
    line, _ = evaluate(demand(sleeve_product, reorder_quantity=48), bom, config)
    # MOQ forces 20 boxes = 480 base units, against 48 * 3 = 144 allowed.
    assert line.quantity == 20
    assert LineFlag.CAP_EXCEEDED in line.flags


def test_ordinary_order_trips_nothing(bom, sleeve_product):
    config = Config(
        safety=SafetyConfig(
            max_line_quantity=100,
            max_reorder_quantity_multiple=5,
            max_total_lines=None,
        )
    )
    line, _ = evaluate(
        demand(sleeve_product, reorder_point=100, on_hand=90, reorder_quantity=48),
        bom,
        config,
    )
    assert line.flags == ()


# ---------------------------------------------------------------------------
# The productAvailability omission trap
# ---------------------------------------------------------------------------


def test_missing_availability_row_reads_as_zero_stock():
    """A stocked-out product vanishes from productAvailability entirely.

    Cin7 omits rows where on-hand, available and on-order are all zero. That
    is precisely the product most urgently needing a purchase order, so a
    missing row must mean "no stock", never "skip this product".
    """
    stock = availability_for({}, SLEEVE, LOCATION)
    assert stock.on_hand == 0.0
    assert stock.allocated == 0.0
    assert stock.product_id == SLEEVE
    assert stock.location == LOCATION


def test_stocked_out_product_still_generates_an_order(bom, config, sleeve_product):
    stock = availability_for({}, SLEEVE, LOCATION)
    line, skip = evaluate(
        demand(sleeve_product, on_hand=stock.on_hand), bom, config
    )
    assert skip is None
    assert line.quantity == 2


def test_present_availability_row_is_used():
    existing = Availability(
        product_id=SLEEVE, location=LOCATION, on_hand=30, allocated=5
    )
    found = availability_for({(SLEEVE, LOCATION): existing}, SLEEVE, LOCATION)
    assert found.on_hand == 30
    assert found.available == 25
