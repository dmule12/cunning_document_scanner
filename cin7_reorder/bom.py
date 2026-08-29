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
    #: What a human reads on the purchase order, and what MOQ overrides in
    #: config.yaml are keyed by. Defaults to the id so nothing can render as
    #: empty, but a report showing a GUID here means the SKU never arrived.
    pack_sku: str = ""

    @property
    def display_sku(self) -> str:
        return self.pack_sku or self.pack_product_id


@dataclass(frozen=True)
class Conflict:
    """A base product that resolves to more than one pack.

    Not a decision the tool is allowed to make. Ordering the wrong pack size
    means the wrong quantity of the wrong product arriving, so these are
    reported and the product is skipped.
    """

    base_product_id: str
    pack_product_ids: tuple[str, ...]
    #: The packs by SKU. A conflict is a data problem someone has to go and
    #: fix in Cin7, and a list of GUIDs is not something anyone can act on.
    pack_skus: tuple[str, ...] = ()


class BomIndex:
    """Immutable lookup from base product to the pack that contains it."""

    def __init__(
        self,
        links: dict[str, PackLink],
        conflicts: dict[str, Conflict],
        pack_product_ids: frozenset[str],
        pack_components: dict[str, tuple] | None = None,
    ) -> None:
        self._links = links
        self._conflicts = conflicts
        self._pack_product_ids = pack_product_ids
        #: parent -> its full component list, straight off the BOMs. Kept
        #: separately from _links because the two answer different questions:
        #: _links answers "which pack do I ORDER for this component" and
        #: excludes conflicted components, while this answers "what does an
        #: ordered pack CONTAIN" — where a conflict is irrelevant and every
        #: component counts.
        self._pack_components = pack_components or {}

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
                        pack_sku=bom.parent_sku or bom.parent_product_id,
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
                    pack_sku=found[0].pack_sku,
                )
            else:
                conflicts[base_id] = Conflict(
                    base_product_id=base_id,
                    pack_product_ids=tuple(sorted(distinct_parents)),
                    pack_skus=tuple(
                        sorted({link.display_sku for link in found})
                    ),
                )

        pack_ids = frozenset(bom.parent_product_id for bom in boms)
        pack_components = {
            bom.parent_product_id: tuple(
                c for c in bom.components if c.quantity > 0
            )
            for bom in boms
        }
        return cls(
            links=links,
            conflicts=conflicts,
            pack_product_ids=pack_ids,
            pack_components=pack_components,
        )

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

    def components_in_base(
        self, product_id: str, quantity: float
    ) -> list[tuple[str, float]]:
        """Everything a quantity of ``product_id`` becomes, in base units.

        This is how an inbound purchase-order line for boxes becomes numbers
        of sleeves — plural on purpose. A pack can contain several different
        components (a coffee pack holds the bag, the beans and the label),
        and an earlier version credited the whole line to whichever component
        happened to come first, returning zero inbound for the rest — which
        understates inbound and re-orders goods already on their way.

        Conflicted components are included here. A conflict decides which
        pack to ORDER for a component, not what an ordered pack CONTAINS.

        A product with no bill of materials passes through unchanged.
        """
        components = self._pack_components.get(product_id)
        if not components:
            return [(product_id, quantity)]
        return [
            (c.component_product_id, quantity * c.quantity) for c in components
        ]

    # -- diagnostics -------------------------------------------------------

    @property
    def conflicts(self) -> tuple[Conflict, ...]:
        return tuple(self._conflicts.values())

    @property
    def link_count(self) -> int:
        return len(self._links)

    def __len__(self) -> int:
        return len(self._links)
