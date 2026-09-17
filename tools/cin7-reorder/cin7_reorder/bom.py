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
        recipe_components: dict[str, tuple[str, ...]] | None = None,
        cycles: tuple[tuple[str, ...], ...] = (),
    ) -> None:
        self._links = links
        self._conflicts = conflicts
        self._pack_product_ids = pack_product_ids
        #: base product -> the parents that contain a FRACTION of it. Recipes
        #: rather than packs, so not a way to buy the component.
        self._recipe_components = recipe_components or {}
        #: Groups of products that contain each other, directly or through a
        #: chain. See :meth:`cycles`.
        self._cycles = cycles
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
        recipes: dict[str, set[str]] = {}

        for bom in boms:
            for component in bom.components:
                if component.quantity <= 0:
                    # Guarded again here as well as in schema.py: a zero ratio
                    # would divide by zero downstream.
                    continue
                if component.quantity < 1:
                    # A parent containing LESS THAN ONE of the component is a
                    # recipe, not a pack: buying one gets you a fraction of a
                    # unit. Seen live — a coffee blend whose bill of materials
                    # says 0.368 of a green bean SKU, which resolved to
                    # "order 2661 blends to obtain 980 green beans".
                    #
                    # Green beans are bought from a green bean supplier, not
                    # obtained by buying the thing they are roasted into, so
                    # this is not a purchasing route at all. Excluded from the
                    # links and reported, leaving the component to be ordered
                    # as itself.
                    recipes.setdefault(
                        component.component_product_id, set()
                    ).add(bom.parent_sku or bom.parent_product_id)
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
            cycles=_find_cycles(pack_components),
            recipe_components={
                base: tuple(sorted(parents))
                for base, parents in recipes.items()
                # Only interesting where no real pack exists: a component
                # with both a pack and a recipe parent orders via the pack,
                # and saying so would be noise.
                if base not in links
            },
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
    def cycles(self) -> tuple[tuple[str, ...], ...]:
        """Products whose bills of materials contain each other.

        Confirmed live on 'Napkins Plain': the Box of 2000 lists 20 x Pack of
        100, correctly, and the Pack of 100 lists 20 x Box of 2000 — entered
        backwards. Both records therefore claim to be a pack, packs are never
        evaluated against their own stock, and so BOTH napkin products became
        invisible to the run. No order line, no skip row, nothing in the
        report: the tool simply never considered them, for months.

        Which way round the relationship really goes is not something to
        guess — that is the same "wrong quantity of the wrong product
        arriving" risk a conflict carries — so these are reported and left
        for somebody to fix in Cin7.
        """
        return self._cycles

    @property
    def recipe_components(self) -> dict[str, tuple[str, ...]]:
        """Components only ever found as a fraction of something else.

        Reported so that "ordered as base units" on such a product reads as
        a decision rather than a missing pack link.
        """
        return dict(self._recipe_components)

    @property
    def link_count(self) -> int:
        return len(self._links)

    def __len__(self) -> int:
        return len(self._links)


def _find_cycles(
    pack_components: dict[str, tuple],
) -> tuple[tuple[str, ...], ...]:
    """Products that contain each other, directly or through a chain.

    A bill of materials describes "this is made of that", so it cannot
    legitimately come back to where it started: a pack of 100 cannot contain
    boxes of 2000 that contain packs of 100. A cycle is always a data entry
    mistake, and an expensive one to leave alone, because every product in it
    counts as a pack and packs are never evaluated for reordering. The
    products vanish from the run rather than being reported.

    Plain depth-first search over the parent -> component graph, returning
    each cycle once, ordered from its smallest id so the same cycle reads the
    same way from run to run.
    """
    found: set[tuple[str, ...]] = set()
    visited: set[str] = set()

    def walk(node: str, path: list[str], on_path: set[str]) -> None:
        if node in on_path:
            cycle = path[path.index(node):]
            # Rotate to start at the smallest id: the same cycle is otherwise
            # reported differently depending on which product we happened to
            # start walking from.
            pivot = cycle.index(min(cycle))
            found.add(tuple(cycle[pivot:] + cycle[:pivot]))
            return
        if node in visited:
            return
        visited.add(node)
        on_path.add(node)
        path.append(node)
        for component in pack_components.get(node, ()):
            walk(component.component_product_id, path, on_path)
        path.pop()
        on_path.discard(node)

    for parent in pack_components:
        walk(parent, [], set())

    return tuple(sorted(found))
