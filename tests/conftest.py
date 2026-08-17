"""Synthetic fixtures. Nothing here touches the network."""

from __future__ import annotations

import pytest

from cin7_reorder.bom import BomIndex
from cin7_reorder.config import Config, SafetyConfig
from cin7_reorder.models import (
    BillOfMaterials,
    BomComponent,
    Product,
    PurchaseLine,
    PurchaseOrder,
    PurchaseStatus,
)

# The running example throughout: sleeves are counted, boxes are ordered.
SLEEVE = "prod-sleeve-001"
BOX = "prod-box-024"
UNITS_PER_BOX = 24.0

CUP = "prod-cup-014"
CUP_BOX = "prod-cupbox-050"

SINGLE = "prod-single-099"  # genuinely sold as singles, no pack parent

SUPPLIER = "sup-1"
LOCATION = "Main Warehouse"


@pytest.fixture
def bom() -> BomIndex:
    return BomIndex.build(
        [
            BillOfMaterials(
                parent_product_id=BOX,
                components=(
                    BomComponent(component_product_id=SLEEVE, quantity=UNITS_PER_BOX),
                ),
            ),
            BillOfMaterials(
                parent_product_id=CUP_BOX,
                components=(BomComponent(component_product_id=CUP, quantity=50.0),),
            ),
        ]
    )


@pytest.fixture
def sleeve_product() -> Product:
    return Product(
        id=SLEEVE, sku="SLV-001", name="Sleeve", supplier_id=SUPPLIER
    )


@pytest.fixture
def single_product() -> Product:
    return Product(
        id=SINGLE, sku="SNG-099", name="Sold as singles", supplier_id=SUPPLIER
    )


@pytest.fixture
def config() -> Config:
    # Caps off by default so tests assert on arithmetic, not on cap tripping.
    return Config(
        safety=SafetyConfig(
            max_line_quantity=None,
            max_reorder_quantity_multiple=None,
            max_total_lines=None,
        )
    )


def purchase(
    *,
    purchase_id: str = "po-1",
    status: PurchaseStatus = PurchaseStatus.AUTHORISED,
    product_id: str = BOX,
    ordered: float = 10.0,
    received: float = 0.0,
    location: str = LOCATION,
    reference: str | None = None,
) -> PurchaseOrder:
    return PurchaseOrder(
        id=purchase_id,
        status=status,
        supplier_id=SUPPLIER,
        location=location,
        reference=reference,
        lines=(
            PurchaseLine(
                product_id=product_id,
                sku=product_id,
                ordered_quantity=ordered,
                received_quantity=received,
            ),
        ),
    )
