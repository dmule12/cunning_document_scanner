"""The reorder calculation."""

from __future__ import annotations

from dataclasses import replace

from cin7_reorder.config import Config, SafetyConfig
from cin7_reorder.models import Availability, LineFlag, Product, SkipReason
from cin7_reorder.reorder import Demand, availability_for, evaluate

from .conftest import BOX, LOCATION, SINGLE, SLEEVE, SUPPLIER, UNITS_PER_BOX


def demand(product: Product, **overrides) -> Demand:
    base = dict(
        product=product,
        location=LOCATION,
        par=100.0,
        on_hand=0.0,
        allocated=0.0,
        inbound_base=0.0,
        inbound_sources=(),
        reorder_quantity=None,
    )
    base.update(overrides)
    return Demand(**base)


# ---------------------------------------------------------------------------
# Core arithmetic
# ---------------------------------------------------------------------------


def test_orders_the_pack_sku_not_the_base_sku(bom, config, sleeve_product):
    line, skip = evaluate(demand(sleeve_product, par=100, on_hand=0), bom, config)

    assert skip is None
    assert line is not None
    # The line points at the box, which is what the supplier sells.
    assert line.order_product_id == BOX
    assert line.base_product_id == SLEEVE
    assert line.is_pack is True


def test_rounds_up_to_whole_packs(bom, config, sleeve_product):
    """Need 37 sleeves at 24 per box -> 2 boxes, not 1.54."""
    line, _ = evaluate(demand(sleeve_product, par=37, on_hand=0), bom, config)
    assert line.need_base == 37
    assert line.quantity == 2


def test_exact_multiple_does_not_round_up(bom, config, sleeve_product):
    """48 sleeves is exactly 2 boxes; float dust must not make it 3."""
    line, _ = evaluate(demand(sleeve_product, par=48, on_hand=0), bom, config)
    assert line.quantity == 2


def test_inbound_stock_reduces_the_order(bom, config, sleeve_product):
    """Par 100, 10 on hand, 48 already on the way -> need 42 -> 2 boxes."""
    line, _ = evaluate(
        demand(sleeve_product, par=100, on_hand=10, inbound_base=48), bom, config
    )
    assert line.need_base == 42
    assert line.quantity == 2


def test_allocated_stock_increases_the_need(bom, config, sleeve_product):
    """Stock promised to orders is not available to cover par."""
    line, _ = evaluate(
        demand(sleeve_product, par=100, on_hand=60, allocated=20), bom, config
    )
    assert line.need_base == 60


def test_sufficient_stock_produces_no_line(bom, config, sleeve_product):
    line, skip = evaluate(
        demand(sleeve_product, par=100, on_hand=120), bom, config
    )
    assert line is None
    assert skip.reason is SkipReason.SUFFICIENT_STOCK


def test_inbound_alone_can_satisfy_par(bom, config, sleeve_product):
    """The whole point of reconstructing inbound: don't reorder twice."""
    line, skip = evaluate(
        demand(sleeve_product, par=100, on_hand=0, inbound_base=120), bom, config
    )
    assert line is None
    assert skip.reason is SkipReason.SUFFICIENT_STOCK


def test_exactly_at_par_does_not_order(bom, config, sleeve_product):
    line, skip = evaluate(demand(sleeve_product, par=100, on_hand=100), bom, config)
    assert line is None
    assert skip.reason is SkipReason.SUFFICIENT_STOCK


def test_float_dust_does_not_trigger_a_spurious_pack(bom, config, sleeve_product):
    """Cin7 returns floats; 100 - 99.9999999999 must not become an order."""
    line, skip = evaluate(
        demand(sleeve_product, par=100.0, on_hand=99.9999999999), bom, config
    )
    assert line is None
    assert skip.reason is SkipReason.SUFFICIENT_STOCK


# ---------------------------------------------------------------------------
# Fallbacks
# ---------------------------------------------------------------------------


def test_product_without_pack_orders_base_units_and_is_flagged(
    bom, config, single_product
):
    """Legitimate for singles, but also what a missing BOM looks like.

    The flag is what lets a reviewer tell the two apart.
    """
    line, _ = evaluate(demand(single_product, par=37, on_hand=0), bom, config)
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

    line, skip = evaluate(demand(sleeve_product, par=100), conflicted, config)
    assert line is None
    assert skip.reason is SkipReason.MULTIPLE_BOM_PARENTS
    assert "box-12" in skip.detail


def test_product_without_supplier_is_skipped(bom, config):
    orphan = Product(id=SLEEVE, sku="SLV-001", name="Sleeve", supplier_id=None)
    line, skip = evaluate(demand(orphan, par=100), bom, config)
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
    line, _ = evaluate(demand(sleeve_product, par=25), bom, config)
    # 25 sleeves would be 2 boxes; MOQ of 3 lifts it.
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
    line, _ = evaluate(demand(sleeve_product, par=240), bom, config)
    assert line.quantity == 10
    assert LineFlag.MOQ_APPLIED not in line.flags


def test_absurd_quantity_trips_the_line_cap(bom, sleeve_product):
    """A wrong BOM ratio or stale par level looks plausible on screen.

    The cap does not block the line; it marks it so a human looks.
    """
    config = Config(
        safety=SafetyConfig(
            max_line_quantity=10,
            max_reorder_quantity_multiple=None,
            max_total_lines=None,
        )
    )
    line, _ = evaluate(demand(sleeve_product, par=10_000), bom, config)
    assert LineFlag.CAP_EXCEEDED in line.flags


def test_quantity_far_above_reorder_quantity_trips_the_multiple_cap(
    bom, sleeve_product
):
    config = Config(
        safety=SafetyConfig(
            max_line_quantity=None,
            max_reorder_quantity_multiple=3,
            max_total_lines=None,
        )
    )
    line, _ = evaluate(
        demand(sleeve_product, par=2400, reorder_quantity=2), bom, config
    )
    assert line.quantity == 100
    assert LineFlag.CAP_EXCEEDED in line.flags


def test_reasonable_quantity_trips_nothing(bom, sleeve_product):
    config = Config(
        safety=SafetyConfig(
            max_line_quantity=100,
            max_reorder_quantity_multiple=5,
            max_total_lines=None,
        )
    )
    line, _ = evaluate(
        demand(sleeve_product, par=48, reorder_quantity=2), bom, config
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
        demand(sleeve_product, par=100, on_hand=stock.on_hand), bom, config
    )
    assert skip is None
    assert line.quantity == 5  # ceil(100 / 24)


def test_present_availability_row_is_used():
    existing = Availability(
        product_id=SLEEVE, location=LOCATION, on_hand=30, allocated=5
    )
    found = availability_for({(SLEEVE, LOCATION): existing}, SLEEVE, LOCATION)
    assert found.on_hand == 30
    assert found.available == 25
