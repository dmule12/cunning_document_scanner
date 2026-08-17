"""Resolving Cin7's stored reorder points."""

from __future__ import annotations

from cin7_reorder.models import ReorderParameters
from cin7_reorder.reorderpoints import resolve

from .conftest import LOCATION, SLEEVE, SUPPLIER


def params(**overrides) -> ReorderParameters:
    base = dict(
        product_id=SLEEVE,
        supplier_id=SUPPLIER,
        location=None,
        minimum_before_reorder=100.0,
        reorder_quantity=48.0,
    )
    base.update(overrides)
    return ReorderParameters(**base)


# ---------------------------------------------------------------------------
# Precedence
# ---------------------------------------------------------------------------


def test_location_override_wins():
    point = resolve(
        [params(), params(location=LOCATION, minimum_before_reorder=30, reorder_quantity=24)],
        supplier_id=SUPPLIER,
        location=LOCATION,
    )
    assert point.minimum == 30
    assert point.reorder_quantity == 24
    assert point.source == "location"


def test_product_level_used_without_an_override():
    point = resolve([params()], supplier_id=SUPPLIER, location=LOCATION)
    assert point.minimum == 100
    assert point.source == "product"


def test_override_for_another_location_is_ignored():
    point = resolve(
        [params(), params(location="Elsewhere", minimum_before_reorder=5)],
        supplier_id=SUPPLIER,
        location=LOCATION,
    )
    assert point.minimum == 100
    assert point.source == "product"


def test_location_override_without_a_minimum_falls_back_to_product():
    point = resolve(
        [params(), params(location=LOCATION, minimum_before_reorder=None)],
        supplier_id=SUPPLIER,
        location=LOCATION,
    )
    assert point.minimum == 100
    assert point.source == "product"


# ---------------------------------------------------------------------------
# Absent reorder points
# ---------------------------------------------------------------------------


def test_missing_minimum_yields_none():
    """No reorder point means nobody opted this product into reordering."""
    assert (
        resolve(
            [params(minimum_before_reorder=None)],
            supplier_id=SUPPLIER,
            location=LOCATION,
        )
        is None
    )


def test_zero_minimum_counts_as_unset():
    """Cin7 defaults the field to 0.

    Treating that as a genuine "order when you hit nothing" trigger would
    sweep the entire catalogue into scope on the first run.
    """
    assert (
        resolve(
            [params(minimum_before_reorder=0)],
            supplier_id=SUPPLIER,
            location=LOCATION,
        )
        is None
    )


def test_no_candidates_yields_none():
    assert resolve([], supplier_id=SUPPLIER, location=LOCATION) is None


# ---------------------------------------------------------------------------
# Reorder quantity
# ---------------------------------------------------------------------------


def test_minimum_without_reorder_quantity_still_resolves():
    """Resolution succeeds; the caller reports the missing quantity.

    Keeping these separate means the run report can say "at its minimum but
    nothing to order" rather than silently treating the product as having no
    reorder point at all.
    """
    point = resolve(
        [params(reorder_quantity=None)], supplier_id=SUPPLIER, location=LOCATION
    )
    assert point is not None
    assert point.minimum == 100
    assert point.has_orderable_quantity is False


def test_zero_reorder_quantity_is_not_orderable():
    point = resolve(
        [params(reorder_quantity=0)], supplier_id=SUPPLIER, location=LOCATION
    )
    assert point.has_orderable_quantity is False


def test_positive_reorder_quantity_is_orderable():
    point = resolve([params()], supplier_id=SUPPLIER, location=LOCATION)
    assert point.has_orderable_quantity is True


# ---------------------------------------------------------------------------
# Supplier scoping
# ---------------------------------------------------------------------------


def test_entries_without_a_supplier_are_still_considered():
    """Reorder points are a property of the product, not the supplier.

    A product-level MinimumBeforeReorder carries no supplier, so filtering it
    out by supplier would discard the most common case.
    """
    point = resolve(
        [params(supplier_id=None)], supplier_id=SUPPLIER, location=LOCATION
    )
    assert point is not None


def test_other_suppliers_entries_are_excluded():
    assert (
        resolve([params(supplier_id="sup-other")], supplier_id=SUPPLIER, location=LOCATION)
        is None
    )
