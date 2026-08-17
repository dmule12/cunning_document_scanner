"""The fingerprint guard.

A duplicate purchase order is visible and annoying. A silently overwritten
manual correction is invisible and worse. These tests pin the behaviour that
keeps the second from happening.
"""

from __future__ import annotations

from datetime import date

from cin7_reorder.drafts import (
    DraftDecision,
    FingerprintStore,
    decide,
    fingerprint,
    fingerprint_purchase,
    is_ours,
    run_reference,
)
from cin7_reorder.models import (
    PurchaseLine,
    PurchaseOrder,
    PurchaseStatus,
    SuggestedLine,
)

from .conftest import BOX, LOCATION, SLEEVE, SUPPLIER


def suggested(quantity: float, product_id: str = BOX) -> SuggestedLine:
    return SuggestedLine(
        base_product_id=SLEEVE,
        base_sku="SLV-001",
        order_product_id=product_id,
        order_sku=product_id,
        location=LOCATION,
        supplier_id=SUPPLIER,
        par=100,
        on_hand=0,
        allocated=0,
        inbound_base=0,
        need_base=100,
        units_per_pack=24,
        quantity=quantity,
    )


def draft(
    *,
    purchase_id: str = "po-1",
    quantity: float = 5,
    reference: str | None = "AUTO-REORDER-2026W33-Main-sup-1",
    product_id: str = BOX,
) -> PurchaseOrder:
    return PurchaseOrder(
        id=purchase_id,
        status=PurchaseStatus.DRAFT,
        supplier_id=SUPPLIER,
        location=LOCATION,
        reference=reference,
        lines=(
            PurchaseLine(
                product_id=product_id, sku=product_id, ordered_quantity=quantity
            ),
        ),
    )


# ---------------------------------------------------------------------------
# References
# ---------------------------------------------------------------------------


def test_reference_is_stable_within_a_week():
    a = run_reference(SUPPLIER, LOCATION, date(2026, 8, 17))  # Monday
    b = run_reference(SUPPLIER, LOCATION, date(2026, 8, 21))  # Friday, same week
    assert a == b


def test_reference_changes_between_weeks():
    a = run_reference(SUPPLIER, LOCATION, date(2026, 8, 17))
    b = run_reference(SUPPLIER, LOCATION, date(2026, 8, 24))
    assert a != b


def test_our_references_are_recognisable():
    assert is_ours(draft()) is True
    assert is_ours(draft(reference="PO-00123")) is False
    assert is_ours(draft(reference=None)) is False


# ---------------------------------------------------------------------------
# Fingerprints
# ---------------------------------------------------------------------------


def test_fingerprint_is_order_independent():
    a = fingerprint([suggested(2, "box-a"), suggested(3, "box-b")])
    b = fingerprint([suggested(3, "box-b"), suggested(2, "box-a")])
    assert a == b


def test_fingerprint_changes_with_quantity():
    assert fingerprint([suggested(2)]) != fingerprint([suggested(3)])


def test_suggested_and_purchase_fingerprints_agree():
    """The two hashes must match for the update path to ever run."""
    assert fingerprint([suggested(5)]) == fingerprint_purchase(draft(quantity=5))


# ---------------------------------------------------------------------------
# Decisions
# ---------------------------------------------------------------------------


def test_no_existing_draft_means_create():
    plan = decide(existing=None, reference="ref", stored_fingerprint=None)
    assert plan.decision == DraftDecision.CREATE


def test_unchanged_draft_is_updated_in_place():
    existing = draft(quantity=5)
    plan = decide(
        existing=existing,
        reference="ref",
        stored_fingerprint=fingerprint_purchase(existing),
    )
    assert plan.decision == DraftDecision.UPDATE
    assert plan.purchase_id == "po-1"


def test_edited_draft_is_left_alone():
    """Someone changed 5 boxes to 8. That edit must survive."""
    original = draft(quantity=5)
    stored = fingerprint_purchase(original)

    edited = draft(quantity=8)
    plan = decide(existing=edited, reference="ref", stored_fingerprint=stored)

    assert plan.decision == DraftDecision.LEAVE_ALONE
    assert "edited" in plan.reason


def test_added_line_counts_as_an_edit():
    original = draft(quantity=5)
    stored = fingerprint_purchase(original)

    with_extra = PurchaseOrder(
        id="po-1",
        status=PurchaseStatus.DRAFT,
        supplier_id=SUPPLIER,
        location=LOCATION,
        reference=original.reference,
        lines=original.lines
        + (PurchaseLine(product_id="box-other", sku="box-other", ordered_quantity=1),),
    )

    plan = decide(existing=with_extra, reference="ref", stored_fingerprint=stored)
    assert plan.decision == DraftDecision.LEAVE_ALONE


def test_someone_elses_draft_is_never_touched():
    plan = decide(
        existing=draft(reference="PO-00123"),
        reference="ref",
        stored_fingerprint=None,
    )
    assert plan.decision == DraftDecision.LEAVE_ALONE
    assert "not created by this tool" in plan.reason


def test_missing_fingerprint_declines_to_overwrite():
    """Lost state file: an edit cannot be ruled out, so don't risk it."""
    plan = decide(existing=draft(), reference="ref", stored_fingerprint=None)
    assert plan.decision == DraftDecision.LEAVE_ALONE
    assert "fingerprint" in plan.reason


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


def test_store_round_trips(tmp_path):
    path = tmp_path / "state" / "fingerprints.json"
    store = FingerprintStore(path)
    store.set("po-1", "abc")
    store.save()

    assert FingerprintStore(path).get("po-1") == "abc"


def test_missing_store_is_empty_not_an_error(tmp_path):
    assert FingerprintStore(tmp_path / "nope.json").get("po-1") is None


def test_corrupt_store_degrades_safely(tmp_path):
    """A corrupt state file must not crash the run.

    Empty fingerprints mean every existing draft is left alone, which is the
    safe direction to fail in.
    """
    path = tmp_path / "fingerprints.json"
    path.write_text("{ not json at all", encoding="utf-8")
    assert FingerprintStore(path).get("po-1") is None
