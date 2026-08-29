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


@dataclass(frozen=True)
class InboundAudit:
    """What one open purchase order contributed, and why.

    Inbound is the one number in this tool with no equivalent in Cin7's UI to
    check against — it is reconstructed precisely because Cin7 will not tell
    you. A figure nobody can verify is a figure nobody should trust, so every
    purchase that was read is accounted for here whether it contributed
    anything or not.
    """

    purchase_id: str
    status: str
    location: str
    lines: int
    lines_outstanding: int
    base_units: float
    verdict: str


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
    audit: list[InboundAudit] = field(default_factory=list)

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
        # The raw string, not the parsed enum. "UNKNOWN" tells a reader
        # nothing they can act on; the actual value is what goes into
        # _STATUS_MAP to stop it happening again.
        status = purchase.raw_status or (
            purchase.status.value if purchase.status else "?"
        )

        def record(verdict: str, base_units: float = 0.0, outstanding: int = 0) -> None:
            inbound.audit.append(
                InboundAudit(
                    purchase_id=purchase.id,
                    status=status,
                    location=purchase.location,
                    lines=len(purchase.lines),
                    lines_outstanding=outstanding,
                    base_units=base_units,
                    verdict=verdict,
                )
            )

        if purchase.id in excluded:
            record("skipped — this run's own draft, about to be rewritten")
            continue

        if purchase.status in CLOSED_STATUSES:
            # Fully received stock is already in on-hand; voided stock is
            # never arriving.
            record("skipped — closed, nothing more coming")
            continue

        if purchase.is_draft:
            # A draft is not a commitment — nobody has sent it to the
            # supplier. Counting it as inbound would let an unsent draft
            # suppress a real reorder indefinitely. Drafts the automation
            # owns are excluded above and rewritten; anyone else's draft is
            # deliberately ignored here.
            record("skipped — someone else's draft, not a commitment")
            continue

        if purchase.status is PurchaseStatus.UNKNOWN:
            # Counted, not skipped. The list endpoint already established
            # this order is open; a status string this code does not
            # recognise is a gap in the parser, not evidence that nothing is
            # coming. Excluding it would understate inbound, and understated
            # inbound means re-ordering goods already in transit — the exact
            # failure this module exists to prevent. It is named in the
            # report either way.
            inbound.unknown_status_orders.append(purchase.id)

        counted = 0.0
        outstanding_lines = 0

        for line in purchase.lines:
            outstanding = line.outstanding_quantity
            if outstanding <= 0:
                continue

            outstanding_lines += 1
            for base_product_id, base_quantity in bom.components_in_base(
                line.product_id, outstanding
            ):
                counted += base_quantity
                inbound.add(
                    base_product_id, purchase.location, base_quantity, purchase.id
                )

        if not purchase.lines:
            verdict = "no order lines on the record"
        elif not outstanding_lines:
            verdict = "everything on it has already been received"
        else:
            verdict = "counted"
        if purchase.status is PurchaseStatus.UNKNOWN:
            verdict += " (status not recognised — counted anyway)"

        record(verdict, base_units=counted, outstanding=outstanding_lines)

    return inbound
