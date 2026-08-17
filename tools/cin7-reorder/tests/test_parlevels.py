"""Par level derivation.

This is the least-verified part of the system: Cin7 stores lead days, safety
days and reorder quantity, none of which is a consumption rate, so the
conversion to a par level is our inference rather than a documented contract.
These tests pin the behaviour we chose; they cannot tell us it matches what
Cin7's own reorder report would suggest.
"""

from __future__ import annotations

from cin7_reorder.config import ParLevelConfig
from cin7_reorder.models import ReorderParameters
from cin7_reorder.parlevels import (
    SalesHistoryDemand,
    StaticDemand,
    par_level,
    resolve_parameters,
)

from .conftest import LOCATION, SLEEVE, SUPPLIER


def params(**overrides) -> ReorderParameters:
    base = dict(
        product_id=SLEEVE,
        supplier_id=SUPPLIER,
        location=None,
        lead_days=14.0,
        safety_days=7.0,
        reorder_quantity=2.0,
    )
    base.update(overrides)
    return ReorderParameters(**base)


# ---------------------------------------------------------------------------
# Precedence
# ---------------------------------------------------------------------------


def test_location_values_override_supplier_values():
    """Cin7's documented precedence."""
    resolved = resolve_parameters(
        [params(), params(location=LOCATION, lead_days=5, safety_days=2)],
        supplier_id=SUPPLIER,
        location=LOCATION,
    )
    assert resolved.lead_days == 5
    assert resolved.safety_days == 2
    assert resolved.source == "location"


def test_supplier_values_used_when_no_location_override():
    resolved = resolve_parameters(
        [params()], supplier_id=SUPPLIER, location=LOCATION
    )
    assert resolved.lead_days == 14
    assert resolved.source == "supplier"


def test_location_entry_for_a_different_location_is_ignored():
    resolved = resolve_parameters(
        [params(), params(location="Elsewhere", lead_days=99, safety_days=99)],
        supplier_id=SUPPLIER,
        location=LOCATION,
    )
    assert resolved.lead_days == 14


def test_incomplete_parameters_yield_none():
    """Cin7 cannot generate a suggestion either; we match that."""
    assert (
        resolve_parameters(
            [params(lead_days=None, safety_days=None)],
            supplier_id=SUPPLIER,
            location=LOCATION,
        )
        is None
    )


def test_incomplete_location_falls_back_to_complete_supplier():
    resolved = resolve_parameters(
        [params(), params(location=LOCATION, lead_days=None, safety_days=None)],
        supplier_id=SUPPLIER,
        location=LOCATION,
    )
    assert resolved.source == "supplier"
    assert resolved.lead_days == 14


def test_other_suppliers_are_not_considered():
    assert (
        resolve_parameters([params()], supplier_id="sup-other", location=LOCATION)
        is None
    )


def test_zero_lead_time_is_valid_not_missing():
    """A same-day supplier is real; 0 must not read as absent."""
    resolved = resolve_parameters(
        [params(lead_days=0, safety_days=3)],
        supplier_id=SUPPLIER,
        location=LOCATION,
    )
    assert resolved is not None
    assert resolved.cover_days == 3


# ---------------------------------------------------------------------------
# Par levels
# ---------------------------------------------------------------------------


def test_par_is_daily_demand_times_cover_days():
    resolved = resolve_parameters(
        [params(lead_days=14, safety_days=7)],
        supplier_id=SUPPLIER,
        location=LOCATION,
    )
    demand = StaticDemand({(SLEEVE, LOCATION): 4.0})
    assert par_level(resolved, demand, SLEEVE, LOCATION, ParLevelConfig()) == 84.0


def test_no_demand_history_yields_none_not_zero():
    """A par of zero would silently retire the product from reordering."""
    resolved = resolve_parameters([params()], supplier_id=SUPPLIER, location=LOCATION)
    assert (
        par_level(resolved, StaticDemand({}), SLEEVE, LOCATION, ParLevelConfig())
        is None
    )


def test_min_daily_demand_floor_applies():
    resolved = resolve_parameters(
        [params(lead_days=10, safety_days=0)],
        supplier_id=SUPPLIER,
        location=LOCATION,
    )
    demand = StaticDemand({(SLEEVE, LOCATION): 0.01})
    config = ParLevelConfig(min_daily_demand=1.0)
    assert par_level(resolved, demand, SLEEVE, LOCATION, config) == 10.0


# ---------------------------------------------------------------------------
# Demand estimation
# ---------------------------------------------------------------------------


def test_sales_history_averages_over_the_window():
    demand = SalesHistoryDemand(
        units_by_product_location={(SLEEVE, LOCATION): 360.0}, window_days=90
    )
    assert demand.daily_demand(SLEEVE, LOCATION) == 4.0


def test_sales_history_returns_none_for_unknown_product():
    demand = SalesHistoryDemand(units_by_product_location={}, window_days=90)
    assert demand.daily_demand(SLEEVE, LOCATION) is None


def test_zero_window_is_guarded():
    demand = SalesHistoryDemand(
        units_by_product_location={(SLEEVE, LOCATION): 10.0}, window_days=0
    )
    assert demand.daily_demand(SLEEVE, LOCATION) is None
