"""The reverse BOM index."""

from __future__ import annotations

from cin7_reorder.bom import BomIndex
from cin7_reorder.models import BillOfMaterials, BomComponent

from .conftest import BOX, CUP, CUP_BOX, SINGLE, SLEEVE, UNITS_PER_BOX


def test_resolves_base_sku_to_its_pack(bom):
    link = bom.resolve(SLEEVE)
    assert link is not None
    assert link.pack_product_id == BOX
    assert link.units_per_pack == UNITS_PER_BOX


def test_product_with_no_pack_resolves_to_none(bom):
    assert bom.resolve(SINGLE) is None
    assert bom.conflict_for(SINGLE) is None


def test_multiple_parents_are_a_conflict_not_a_choice():
    """A sleeve in both a 12-box and a 24-box is a data problem.

    Guessing would mean ordering the wrong quantity of the wrong product, so
    the index records a conflict and the caller skips the product.
    """
    index = BomIndex.build(
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

    assert index.resolve(SLEEVE) is None
    conflict = index.conflict_for(SLEEVE)
    assert conflict is not None
    assert conflict.pack_product_ids == ("box-12", "box-24")


def test_same_parent_listed_twice_sums_rather_than_conflicting():
    """One BOM naming the same component on two lines is not ambiguous."""
    index = BomIndex.build(
        [
            BillOfMaterials(
                parent_product_id="box-mixed",
                components=(
                    BomComponent(component_product_id=SLEEVE, quantity=10),
                    BomComponent(component_product_id=SLEEVE, quantity=14),
                ),
            )
        ]
    )
    link = index.resolve(SLEEVE)
    assert link is not None
    assert link.units_per_pack == 24
    assert index.conflict_for(SLEEVE) is None


def test_zero_quantity_components_are_dropped():
    """A zero ratio would divide by zero when computing pack counts."""
    index = BomIndex.build(
        [
            BillOfMaterials(
                parent_product_id=BOX,
                components=(BomComponent(component_product_id=SLEEVE, quantity=0),),
            )
        ]
    )
    assert index.resolve(SLEEVE) is None


def test_is_pack_identifies_parents(bom):
    assert bom.is_pack(BOX) is True
    assert bom.is_pack(CUP_BOX) is True
    assert bom.is_pack(SLEEVE) is False


def test_units_in_base_converts_pack_quantities(bom):
    product_id, quantity = bom.units_in_base(BOX, 3)
    assert product_id == SLEEVE
    assert quantity == 72.0


def test_units_in_base_passes_through_base_products(bom):
    product_id, quantity = bom.units_in_base(SINGLE, 7)
    assert product_id == SINGLE
    assert quantity == 7.0


def test_empty_index_is_usable(bom):
    index = BomIndex.build([])
    assert len(index) == 0
    assert index.resolve(SLEEVE) is None
    assert index.units_in_base(SLEEVE, 5) == (SLEEVE, 5)
