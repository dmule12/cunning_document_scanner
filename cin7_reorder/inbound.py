"""Reconstructing inbound stock from open purchase orders.

Why this module exists
----------------------
Cin7 records an outstanding purchase order against the product that was
ordered — the box SKU. It does **not** reflect that inbound quantity against
the sleeve SKU that the boxes will become. Confirmed against a live account.
The sleeve's ``OnOrder`` reads zero while boxes are in transit.

So a script that trusts ``OnOrder`` sees nothing on its way and reorders the
same shortfall on every run. With a two-week lead time and a twice-weekly
schedule that is roughly four duplicate orders before the first delivery.

This module rebuilds the number Cin7 will not give us: for every open PO
line, convert the outstanding quantity through the BOM ratio into base units
and accumulate it per (base product, location).

Two rules that are easy to get wrong
------------------------------------
1. **Outstanding, not ordered.** Received stock has already been
   auto-disassembled and counted in on-hand. Counting the full ordered
   quantity double-counts it and suppresses genuinely needed reorders. See
   :attr:`~cin7_reorder.models.PurchaseLine.outstanding_quantity`.

2. **Never mix with ``OnOrder``.** Using Cin7's figure for base-SKU orders
   and reconstructing only for packs double-counts the moment a product is
   ordered both ways. This module is the single source of inbound truth.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Iterable, Optional

from .bom import BomIndex
from .models import PurchaseOrder, PurchaseStatus
from .schema import CLOSED_STATUSES


@dataclass
class InboundStock:
    """Inbound base units per (base product id, location), with provenance."""

    quantities: dict[tuple[str, str], float] = field(
        default_factory=lambda: defaultdict(float)
    )
    sources: dict[tuple[str, str], set[str]] = field(
        default_factory=lambda: defaultdict(set)
    )
    unknown_status_orders: list[str] = field(default_factory=list)

    def add(
        self, product_id: str, location: str, quantity: float, purchase_id: str
    ) -> None:
        if quantity <= 0:
            return
        key = (product_id, location)
        self.quantities[key] += quantity
        self.sources[key].add(purchase_id)

    def get(self, product_id: str, location: str) -> float:
        return self.quantities.get((product_id, location), 0.0)

    def sources_for(self, product_id: str, location: str) -> tuple[str, ...]:
        return tuple(sorted(self.sources.get((product_id, location), set())))

    def __len__(self) -> int:
        return len(self.quantities)


def reconstruct(
    purchases: Iterable[PurchaseOrder],
    bom: BomIndex,
    *,
    exclude_purchase_ids: Optional[set[str]] = None,
) -> InboundStock:
    """Total inbound base units implied by the given purchase orders.

    ``exclude_purchase_ids`` holds the automation's own standing drafts. They
    are about to be recalculated and rewritten, so counting them as inbound
    would suppress the very order they represent — the run would see its own
    unsent suggestion as stock already coming and propose nothing.
    """
    excluded = exclude_purchase_ids or set()
    inbound = InboundStock()

    for purchase in purchases:
        if purchase.id in excluded:
            continue

        if purchase.status in CLOSED_STATUSES:
            # Fully received stock is already in on-hand; voided stock is
            # never arriving.
            continue

        if purchase.status is PurchaseStatus.DRAFT:
            # A draft is not a commitment — nobody has sent it to the
            # supplier. Counting it as inbound would let an unsent draft
            # suppress a real reorder indefinitely. Drafts the automation
            # owns are excluded above and rewritten; anyone else's draft is
            # deliberately ignored here.
            continue

        if purchase.status is PurchaseStatus.UNKNOWN:
            # Do not guess. An unrecognised status might mean stock is
            # coming or might not, and either assumption can be wrong in an
            # expensive direction.
            inbound.unknown_status_orders.append(purchase.id)
            continue

        for line in purchase.lines:
            outstanding = line.outstanding_quantity
            if outstanding <= 0:
                continue

            base_product_id, base_quantity = bom.units_in_base(
                line.product_id, outstanding
            )
            inbound.add(
                base_product_id, purchase.location, base_quantity, purchase.id
            )

    return inbound
