"""Inbound reconstruction — the highest-value tests in this suite.

Cin7 does not show inbound pack stock against the base SKU, so these numbers
have no equivalent in the UI to check against. If the arithmetic here is
wrong, nothing downstream will notice and the resulting purchase orders will
look entirely plausible.
"""

from __future__ import annotations

from cin7_reorder.inbound import reconstruct
from cin7_reorder.models import PurchaseLine, PurchaseOrder, PurchaseStatus

from .conftest import BOX, CUP, CUP_BOX, LOCATION, SINGLE, SLEEVE, SUPPLIER, purchase


def test_open_box_po_converts_to_base_units(bom):
    """2 boxes of 24 outstanding is 48 sleeves inbound."""
    inbound = reconstruct([purchase(ordered=2, received=0)], bom)
    assert inbound.get(SLEEVE, LOCATION) == 48.0


def test_partial_receipt_counts_only_the_outstanding_portion(bom):
    """The worked example from the feasibility write-up.

    10 boxes of 24 ordered, 4 received. The 4 have already auto-disassembled
    into 96 sleeves sitting in on-hand. Only the remaining 6 boxes — 144
    sleeves — are still inbound.

    Counting all 10 (240 sleeves) double-counts the received 96, understates
    the shortfall, and suppresses a reorder that is genuinely needed.
    """
    inbound = reconstruct([purchase(ordered=10, received=4)], bom)
    assert inbound.get(SLEEVE, LOCATION) == 144.0


def test_fully_received_line_contributes_nothing(bom):
    inbound = reconstruct([purchase(ordered=10, received=10)], bom)
    assert inbound.get(SLEEVE, LOCATION) == 0.0


def test_over_receipt_never_produces_negative_inbound(bom):
    """Receiving more than ordered must not inflate a reorder.

    A negative inbound would subtract from the stock position and make the
    tool order *more*, which is precisely backwards.
    """
    inbound = reconstruct([purchase(ordered=10, received=12)], bom)
    assert inbound.get(SLEEVE, LOCATION) == 0.0


def test_voided_purchase_is_ignored(bom):
    inbound = reconstruct(
        [purchase(ordered=10, status=PurchaseStatus.VOIDED)], bom
    )
    assert inbound.get(SLEEVE, LOCATION) == 0.0


def test_received_purchase_is_ignored(bom):
    """Already in on-hand; counting it again would double it."""
    inbound = reconstruct(
        [purchase(ordered=10, status=PurchaseStatus.RECEIVED)], bom
    )
    assert inbound.get(SLEEVE, LOCATION) == 0.0


def test_draft_purchase_is_not_treated_as_inbound(bom):
    """A draft is not a commitment — nobody has sent it to the supplier.

    Counting drafts as inbound would let an unreviewed draft suppress a real
    reorder for as long as it sits there.
    """
    inbound = reconstruct(
        [purchase(ordered=10, status=PurchaseStatus.DRAFT)], bom
    )
    assert inbound.get(SLEEVE, LOCATION) == 0.0


def test_unknown_status_is_reported_not_guessed(bom):
    inbound = reconstruct(
        [purchase(purchase_id="po-x", ordered=10, status=PurchaseStatus.UNKNOWN)],
        bom,
    )
    assert inbound.get(SLEEVE, LOCATION) == 0.0
    assert "po-x" in inbound.unknown_status_orders


def test_base_sku_orders_count_without_conversion(bom):
    """A product with no pack parent passes through at 1:1."""
    inbound = reconstruct([purchase(product_id=SINGLE, ordered=30)], bom)
    assert inbound.get(SINGLE, LOCATION) == 30.0


def test_multiple_purchases_accumulate_with_provenance(bom):
    inbound = reconstruct(
        [
            purchase(purchase_id="po-1", ordered=2),
            purchase(purchase_id="po-2", ordered=3, received=1),
        ],
        bom,
    )
    # 2 boxes + (3 - 1) boxes = 4 boxes = 96 sleeves
    assert inbound.get(SLEEVE, LOCATION) == 96.0
    assert inbound.sources_for(SLEEVE, LOCATION) == ("po-1", "po-2")


def test_excluded_purchases_are_skipped(bom):
    """Our own standing drafts are about to be rewritten.

    Counting them would make the run see its own unsent suggestion as stock
    already coming, and propose nothing.
    """
    inbound = reconstruct(
        [purchase(purchase_id="po-ours", ordered=5)],
        bom,
        exclude_purchase_ids={"po-ours"},
    )
    assert inbound.get(SLEEVE, LOCATION) == 0.0


def test_locations_are_kept_separate(bom):
    inbound = reconstruct(
        [
            purchase(purchase_id="po-1", ordered=2, location="Main Warehouse"),
            purchase(purchase_id="po-2", ordered=5, location="Second Site"),
        ],
        bom,
    )
    assert inbound.get(SLEEVE, "Main Warehouse") == 48.0
    assert inbound.get(SLEEVE, "Second Site") == 120.0


def test_mixed_products_on_one_purchase(bom):
    order = PurchaseOrder(
        id="po-mixed",
        status=PurchaseStatus.AUTHORISED,
        supplier_id=SUPPLIER,
        location=LOCATION,
        lines=(
            PurchaseLine(product_id=BOX, sku="BOX", ordered_quantity=2),
            PurchaseLine(
                product_id=CUP_BOX, sku="CUPBOX", ordered_quantity=3, received_quantity=1
            ),
        ),
    )
    inbound = reconstruct([order], bom)
    assert inbound.get(SLEEVE, LOCATION) == 48.0
    assert inbound.get(CUP, LOCATION) == 100.0  # (3 - 1) * 50


def test_empty_input_is_empty_not_an_error(bom):
    inbound = reconstruct([], bom)
    assert len(inbound) == 0
    assert inbound.get(SLEEVE, LOCATION) == 0.0
