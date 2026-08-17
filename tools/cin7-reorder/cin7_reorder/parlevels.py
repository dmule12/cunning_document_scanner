"""Deriving par levels from Cin7's reorder parameters.

=========================================================================
THIS IS THE LEAST-VERIFIED PART OF THE SYSTEM. Read this before trusting
any number it produces.
=========================================================================

Cin7 stores three reorder settings per supplier, and optionally per location
within a supplier: **lead days**, **safety days**, and **reorder quantity**.
Location-level values take precedence over supplier-level ones, and if
neither is set Cin7 itself cannot generate a suggestion.

Those three numbers do not contain a consumption rate, so they are not a par
level. Converting them needs demand:

    par = daily_demand * (lead_days + safety_days)

Cin7 derives daily demand from sales history for its own Smart Reorder. This
module does the same in :class:`SalesHistoryDemand`, but the exact window,
weighting and treatment of stockout periods that Cin7 uses are not
documented, so **our number will not match theirs exactly.**

What that means in practice: run ``plan`` read-only for a full supplier lead
time and compare its suggestions against both Cin7's own reorder report and
what you would have ordered by hand. If they disagree, this file is the most
likely thing that needs changing — not the arithmetic downstream of it.

The strategy interface exists so an entirely different definition of "par"
(a flat per-SKU table, a seasonal curve, a forecast from elsewhere) can be
dropped in without touching the rest of the package.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol

from .config import ParLevelConfig
from .models import ReorderParameters


@dataclass(frozen=True)
class ResolvedParameters:
    """Reorder parameters after applying Cin7's location-over-supplier precedence."""

    lead_days: float
    safety_days: float
    reorder_quantity: Optional[float]
    source: str  # "location" or "supplier", for the run report

    @property
    def cover_days(self) -> float:
        return self.lead_days + self.safety_days


def resolve_parameters(
    candidates: list[ReorderParameters],
    *,
    supplier_id: str,
    location: str,
) -> Optional[ResolvedParameters]:
    """Apply Cin7's documented precedence: location wins, else supplier.

    Returns ``None`` when neither level has usable values — matching Cin7,
    which cannot produce a suggestion in that case either.
    """
    for_supplier = [c for c in candidates if c.supplier_id == supplier_id]
    if not for_supplier:
        return None

    location_level = next(
        (c for c in for_supplier if c.location == location and c.is_complete),
        None,
    )
    if location_level is not None:
        return ResolvedParameters(
            lead_days=float(location_level.lead_days or 0.0),
            safety_days=float(location_level.safety_days or 0.0),
            reorder_quantity=location_level.reorder_quantity,
            source="location",
        )

    supplier_level = next(
        (c for c in for_supplier if c.location is None and c.is_complete), None
    )
    if supplier_level is not None:
        return ResolvedParameters(
            lead_days=float(supplier_level.lead_days or 0.0),
            safety_days=float(supplier_level.safety_days or 0.0),
            reorder_quantity=supplier_level.reorder_quantity,
            source="supplier",
        )

    return None


class DemandEstimator(Protocol):
    """Daily consumption of a base product at a location."""

    def daily_demand(self, product_id: str, location: str) -> Optional[float]:
        ...


@dataclass
class SalesHistoryDemand:
    """Daily demand as total units shipped divided by the window length.

    Simple on purpose. A more sophisticated estimator (weighted recency,
    excluding stockout periods, seasonality) belongs here once the plain
    version has been compared against reality — optimising an unvalidated
    formula is how you get confidently wrong numbers.

    ``units_by_product_location`` is total base units consumed over
    ``window_days``, keyed by ``(product_id, location)``.
    """

    units_by_product_location: dict[tuple[str, str], float]
    window_days: int
    min_daily_demand: float = 0.0

    def daily_demand(self, product_id: str, location: str) -> Optional[float]:
        if self.window_days <= 0:
            return None
        total = self.units_by_product_location.get((product_id, location))
        if total is None:
            return None
        return max(self.min_daily_demand, total / float(self.window_days))


@dataclass
class StaticDemand:
    """Fixed daily demand per product/location. Used by tests and overrides."""

    values: dict[tuple[str, str], float]

    def daily_demand(self, product_id: str, location: str) -> Optional[float]:
        return self.values.get((product_id, location))


def par_level(
    parameters: ResolvedParameters,
    demand: DemandEstimator,
    product_id: str,
    location: str,
    config: ParLevelConfig,
) -> Optional[float]:
    """Par level in base units, or ``None`` if it cannot be determined.

    ``None`` means "skip and report", never "assume zero" — a par of zero
    would quietly mean this product is never reordered again.
    """
    daily = demand.daily_demand(product_id, location)
    if daily is None:
        return None

    daily = max(daily, config.min_daily_demand)
    return daily * parameters.cover_days
