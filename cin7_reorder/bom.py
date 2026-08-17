"""The reverse BOM index: given a sleeve SKU, which box SKU do we order?

Cin7 stores the relationship parent-to-child: a box product has a bill of
materials listing the sleeves it decomposes into. The reorder calculation
needs the opposite direction, because it starts from the sleeve that ran low.

So one paged read of every product with a BOM is inverted in memory into
``component -> parent``. Cin7 stays the single source of truth for pack
sizes; there is no spreadsheet to drift.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

from .models import BillOfMaterials


@dataclass(frozen=True)
class PackLink:
    """How to order a base product: via ``pack_product_id``, N base units at a time."""

    base_product_id: str
    pack_product_id: str
    units_per_pack: float


@dataclass(frozen=True)
class Conflict:
    """A base product that resolves to more than one pack.

    Not a decision the tool is allowed to make. Ordering the wrong pack size
    means the wrong quantity of the wrong product arriving, so these are
    reported and the product is skipped.
    """

    base_product_id: str
    pack_product_ids: tuple[str, ...]


class BomIndex:
    """Immutable lookup from base product to the pack that contains it."""

    def __init__(
        self,
        links: dict[str, PackLink],
        conflicts: dict[str, Conflict],
        pack_product_ids: frozenset[str],
    ) -> None:
        self._links = links
        self._conflicts = conflicts
        self._pack_product_ids = pack_product_ids

    # -- construction ------------------------------------------------------

    @classmethod
    def build(cls, boms: Iterable[BillOfMaterials]) -> "BomIndex":
        candidates: dict[str, list[PackLink]] = {}

        for bom in boms:
            for component in bom.components:
                if component.quantity <= 0:
                    # Guarded again here as well as in schema.py: a zero ratio
                    # would divide by zero downstream.
                    continue
                candidates.setdefault(component.component_product_id, []).append(
                    PackLink(
                        base_product_id=component.component_product_id,
                        pack_product_id=bom.parent_product_id,
                        units_per_pack=component.quantity,
                    )
                )

        links: dict[str, PackLink] = {}
        conflicts: dict[str, Conflict] = {}

        for base_id, found in candidates.items():
            distinct_parents = {link.pack_product_id for link in found}

            if len(distinct_parents) == 1:
                # Same parent listed more than once (a BOM naming the same
                # component on two lines) sums to one effective ratio.
                total = sum(link.units_per_pack for link in found)
                links[base_id] = PackLink(
                    base_product_id=base_id,
                    pack_product_id=found[0].pack_product_id,
                    units_per_pack=total,
                )
            else:
                conflicts[base_id] = Conflict(
                    base_product_id=base_id,
                    pack_product_ids=tuple(sorted(distinct_parents)),
                )

        pack_ids = frozenset(bom.parent_product_id for bom in boms)
        return cls(links=links, conflicts=conflicts, pack_product_ids=pack_ids)

    # -- queries -----------------------------------------------------------

    def resolve(self, base_product_id: str) -> Optional[PackLink]:
        """The pack to order for this base product, or ``None`` to order it directly.

        ``None`` covers two different situations that the caller must keep
        apart: no pack exists (order the base SKU, flag it), and a conflict
        exists (skip entirely). Check :meth:`conflict_for` before treating a
        ``None`` as "order singles".
        """
        return self._links.get(base_product_id)

    def conflict_for(self, base_product_id: str) -> Optional[Conflict]:
        return self._conflicts.get(base_product_id)

    def is_pack(self, product_id: str) -> bool:
        """True if this product is itself a pack (has a BOM).

        Used to avoid computing a reorder for the box SKU as though it were
        stock in its own right — the boxes get disassembled on receipt, so
        their own stock level is not what we reorder against.
        """
        return product_id in self._pack_product_ids

    def units_in_base(self, product_id: str, quantity: float) -> tuple[str, float]:
        """Convert a quantity of ``product_id`` into base units.

        If the product is a pack, returns its component's id and the quantity
        multiplied by the pack ratio. Otherwise the product is already a base
        unit and passes through unchanged.

        This is how an inbound purchase-order line for boxes becomes a number
        of sleeves.
        """
        for link in self._links.values():
            if link.pack_product_id == product_id:
                return link.base_product_id, quantity * link.units_per_pack
        return product_id, quantity

    # -- diagnostics -------------------------------------------------------

    @property
    def conflicts(self) -> tuple[Conflict, ...]:
        return tuple(self._conflicts.values())

    @property
    def link_count(self) -> int:
        return len(self._links)

    def __len__(self) -> int:
        return len(self._links)
