"""Reading Cin7's stored reorder points.

Cin7 stores the reorder point directly, as **`MinimumBeforeReorder`** with a
companion **`ReorderQuantity`**, settable globally on a product and again per
location.

That is worth stating plainly because an earlier version of this tool got it
wrong. It assumed only lead days, safety days and reorder quantity were
available, none of which is a consumption rate, and so derived a par level by
estimating demand from sales history. That machinery is gone. The number was
stored data all along, and reading it means our suggestions are directly
comparable with Cin7's own low-stock reorder rather than approximating them.

The model is a **trigger**, not a target:

* when stock position falls to or below `MinimumBeforeReorder`,
* order `ReorderQuantity` — a fixed amount, not a top-up to the minimum.

Location values take precedence over the product-level defaults, matching how
Cin7 applies them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .models import ReorderParameters


@dataclass(frozen=True)
class ResolvedReorderPoint:
    """A usable reorder point after applying location precedence."""

    minimum: float
    reorder_quantity: Optional[float]
    source: str  # "location" or "product", for the run report

    @property
    def has_orderable_quantity(self) -> bool:
        return self.reorder_quantity is not None and self.reorder_quantity > 0


def resolve(
    candidates: list[ReorderParameters],
    *,
    supplier_id: Optional[str] = None,
    location: str,
) -> Optional[ResolvedReorderPoint]:
    """The reorder point in force for this product at this location.

    Returns ``None`` when no usable minimum exists, so the caller skips and
    reports rather than inventing one. A product with no reorder point set is
    a product nobody has decided should be automatically reordered.

    A minimum of zero counts as unset. Cin7 defaults the field to 0, so
    treating it as a genuine "order when you hit nothing" trigger would sweep
    the entire catalogue into scope the first time this runs.
    """
    relevant = [
        c
        for c in candidates
        if supplier_id is None or c.supplier_id in (supplier_id, None)
    ]
    if not relevant:
        return None

    location_level = next(
        (
            c
            for c in relevant
            if c.location == location and _usable(c.minimum_before_reorder)
        ),
        None,
    )
    if location_level is not None:
        return ResolvedReorderPoint(
            minimum=float(location_level.minimum_before_reorder or 0.0),
            reorder_quantity=location_level.reorder_quantity,
            source="location",
        )

    product_level = next(
        (
            c
            for c in relevant
            if c.location is None and _usable(c.minimum_before_reorder)
        ),
        None,
    )
    if product_level is not None:
        return ResolvedReorderPoint(
            minimum=float(product_level.minimum_before_reorder or 0.0),
            reorder_quantity=product_level.reorder_quantity,
            source="product",
        )

    return None


def _usable(minimum: Optional[float]) -> bool:
    return minimum is not None and minimum > 0
