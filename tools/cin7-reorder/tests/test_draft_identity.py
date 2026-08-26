"""Telling a draft from an authorised order, and our work from someone else's.

Both were wrong at once, in opposite directions, for the same reason.

Cin7 reports a purchase whose order stage is still a draft as
``Status: "ORDERING"`` at the top level, and puts no ``OrderStatus`` on the
detail record at all. "ORDERING" parses as authorised — reasonably enough,
it means an order is in progress — so reading the overall status made every
draft look authorised.

That meant the tool never recognised its own standing draft and raised a
fresh purchase order every run; and it counted other people's abandoned
drafts, including one from 2020, as stock on its way, which suppresses
reorders for goods that are never coming.

The shapes here are copied from live records.
"""

from __future__ import annotations

import pytest

from cin7_reorder import schema
from cin7_reorder.bom import BomIndex
from cin7_reorder.drafts import DraftDecision, decide, fingerprint_purchase, is_ours
from cin7_reorder.inbound import reconstruct
from cin7_reorder.models import PurchaseStatus

REFERENCE = "AUTO-REORDER-2026W35-WA-Warehouse-ba7067f4"


def purchase_record(*, order_status: str, marker: str | None = None, quantity: float = 2.0):
    """A purchase detail record shaped as Cin7 actually returns one."""
    record = {
        "ID": "po-1",
        # The trap: a draft reports ORDERING here, and there is no
        # OrderStatus key on a detail record at all.
        "Status": "ORDERING",
        "SupplierID": "sup-1",
        "Location": "WA Warehouse",
        "Order": {
            "Status": order_status,
            "Lines": [{"ProductID": "p1", "SKU": "CUP", "Quantity": quantity}],
        },
    }
    if marker is not None:
        record["Note"] = marker
        record["Order"]["Memo"] = marker
    return record


def test_a_draft_is_not_read_as_authorised():
    parsed = schema.parse_purchase(purchase_record(order_status="DRAFT"))

    assert parsed.is_draft is True
    # The overall status still parses the way Cin7 means it. The point is
    # that draft-ness is a different question, answered elsewhere.
    assert parsed.status is PurchaseStatus.AUTHORISED


def test_an_authorised_order_is_not_read_as_a_draft():
    parsed = schema.parse_purchase(purchase_record(order_status="AUTHORISED"))
    assert parsed.is_draft is False


def test_a_record_with_no_order_block_falls_back_to_the_overall_status():
    """order_mapping falls back to the payload, which must not be mistaken
    for an Order block — taking the top-level Status there would defeat the
    whole distinction."""
    parsed = schema.parse_purchase({"ID": "po-1", "Status": "DRAFT"})
    assert parsed.is_draft is True

    parsed = schema.parse_purchase({"ID": "po-1", "Status": "ORDERING"})
    assert parsed.is_draft is False, "ORDERING alone is not evidence of a draft"


def test_an_abandoned_draft_is_not_stock_on_its_way():
    """The expensive half of the bug, and the quiet one.

    A draft nobody authorised is not an order. Counting one as inbound
    suppresses a real reorder — on the live account a purchase order raised
    in 2020 was contributing 410 base units of stock that is never arriving.
    """
    bom = BomIndex.build([])
    parsed = schema.parse_purchase(purchase_record(order_status="DRAFT", quantity=10))

    inbound = reconstruct([parsed], bom)

    assert inbound.get("p1", "WA Warehouse") == 0.0
    verdict = inbound.audit[0].verdict
    assert "draft" in verdict, verdict


def test_an_authorised_order_still_counts_as_inbound():
    bom = BomIndex.build([])
    parsed = schema.parse_purchase(
        purchase_record(order_status="AUTHORISED", quantity=10)
    )

    inbound = reconstruct([parsed], bom)

    assert inbound.get("p1", "WA Warehouse") == 10.0


# ---------------------------------------------------------------------------
# The fingerprint, carried on the record rather than in a local file
# ---------------------------------------------------------------------------


def test_the_fingerprint_survives_the_round_trip():
    """It used to live in a JSON file that did not exist on a fresh checkout
    and did not survive CI — the cache save simply failed. On the record, the
    history travels with the thing it describes."""
    order = schema.build_order_payload(
        purchase_id="po-1",
        reference=REFERENCE,
        lines=[],
        fingerprint="abc123",
    )
    assert order["Memo"] == f"{REFERENCE} fp=abc123"

    reference, fp = schema.split_marker(order["Memo"])
    assert reference == REFERENCE
    assert fp == "abc123"


def test_a_marker_without_a_fingerprint_is_still_ours():
    """Drafts written by earlier versions carry no fingerprint. Reading them
    as somebody else's work would strand them permanently."""
    parsed = schema.parse_purchase(
        purchase_record(order_status="DRAFT", marker=REFERENCE)
    )

    assert is_ours(parsed)
    assert parsed.reference == REFERENCE
    assert parsed.fingerprint is None


def test_our_own_unedited_draft_is_updated_not_duplicated():
    """The reported symptom: a new purchase order every run instead of one."""
    record = purchase_record(order_status="DRAFT", marker=REFERENCE)
    parsed = schema.parse_purchase(record)
    written = fingerprint_purchase(parsed)

    # Re-read it as the next run would, with the fingerprint we wrote.
    record["Note"] = schema.build_marker(REFERENCE, written)
    record["Order"]["Memo"] = record["Note"]
    reread = schema.parse_purchase(record)

    plan = decide(
        existing=reread,
        reference=REFERENCE,
        stored_fingerprint=reread.fingerprint,
    )

    assert plan.decision == DraftDecision.UPDATE
    assert plan.purchase_id == "po-1"


def test_a_hand_edited_draft_is_still_left_alone():
    """The guarantee that makes updating in place safe at all."""
    record = purchase_record(order_status="DRAFT", quantity=2)
    written = fingerprint_purchase(schema.parse_purchase(record))

    # Somebody changed the quantity after we wrote it.
    record["Order"]["Lines"][0]["Quantity"] = 99
    record["Note"] = schema.build_marker(REFERENCE, written)
    record["Order"]["Memo"] = record["Note"]
    edited = schema.parse_purchase(record)

    plan = decide(
        existing=edited,
        reference=REFERENCE,
        stored_fingerprint=edited.fingerprint,
    )

    assert plan.decision == DraftDecision.LEAVE_ALONE
    assert "edited" in plan.reason
