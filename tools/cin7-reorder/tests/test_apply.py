"""Creating a draft purchase order, end to end.

The suite had thorough coverage of *what to order* and none at all of
*whether the order arrives*. So the first live apply created a real draft
purchase order for BioPak with no lines on it: Cin7 accepts an `Order` block
in `POST /purchase`, answers 200, and silently ignores it. Lines are a
separate sub-resource.

Everything here asserts on the requests actually sent, because a run that
reports "1 draft created" while creating an empty one is the failure that
happened.
"""

from __future__ import annotations

import json

import httpx
import pytest

from cin7_reorder.client import Cin7Client, NullRateLimiter
from cin7_reorder.config import (
    ApiConfig,
    Config,
    Credentials,
    PurchaseConfig,
    SupplierConfig,
)
from cin7_reorder.pipeline import Pipeline

SUPPLIER = "sup-1"
NEW_ID = "created-po-1"


def build(*, order_verb_fails: str = "", existing_draft: dict | None = None):
    """A Cin7 that records every write. ``order_verb_fails`` rejects PUT or POST."""
    sent: list[tuple[str, str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        tail = path.split("/ExternalApi/v2/", 1)[-1]
        page = int(request.url.params.get("page", 1))

        def page1(key: str, rows: list) -> httpx.Response:
            return httpx.Response(200, json={key: rows if page == 1 else []})

        if request.method in ("POST", "PUT"):
            body = json.loads(request.content or b"{}")
            sent.append((request.method, tail, body))
            if tail == "purchase/order" and request.method == order_verb_fails:
                return httpx.Response(400, text="wrong verb for this account")
            if tail == "purchase":
                return httpx.Response(200, json={"ID": NEW_ID})
            return httpx.Response(200, json={})

        if tail == "supplier":
            return page1(
                "SupplierList",
                [{"ID": SUPPLIER, "Name": "BioPak", "AdditionalAttribute1": "Yes"}],
            )

        if tail == "product":
            return page1(
                "Products",
                [
                    {
                        "ID": "p1",
                        "SKU": "CUP",
                        "MinimumBeforeReorder": 100,
                        "ReorderQuantity": 48,
                        "Suppliers": [{"SupplierID": SUPPLIER}],
                    }
                ],
            )

        if tail.endswith("productAvailability"):
            if not tail.startswith("ref/"):
                return httpx.Response(200, text="<html>Object moved</html>")
            return page1(
                "ProductAvailabilityList",
                [{"ProductID": "p1", "Location": "WA", "OnHand": 0}],
            )

        if tail == "purchaseList":
            rows = []
            if existing_draft:
                rows.append(
                    {
                        "ID": existing_draft["ID"],
                        "OrderStatus": "DRAFT",
                        "SupplierID": SUPPLIER,
                        "CombinedReceivingStatus": "NOT RECEIVED",
                    }
                )
            return page1("PurchaseList", rows)

        if tail == "purchase":
            return httpx.Response(200, json=existing_draft or {})

        return httpx.Response(200, text="<html>Object moved</html>")

    return handler, sent


def run(tmp_path, handler, state_path=None):
    client = Cin7Client(
        Credentials(account_id="a", app_key="k"),
        ApiConfig(daily_call_budget=200),
        read_only=False,
        transport=httpx.MockTransport(handler),
        rate_limiter=NullRateLimiter(),
    )
    return Pipeline(
        client=client,
        config=Config(
            suppliers=SupplierConfig(attribute_field="AdditionalAttribute1"),
            purchase=PurchaseConfig(
                extra_fields={"TaxRule": "GST on Expenses"},
                line_fields={"TaxRule": "GST on Expenses"},
            ),
        ),
        state_path=state_path or (tmp_path / "state.json"),
        dry_run=False,
    ).run()


def writes(sent, path):
    return [(verb, body) for verb, tail, body in sent if tail == path]


def test_the_lines_actually_reach_cin7(tmp_path):
    """The bug: a draft created with nothing on it.

    Asserting "a draft was created" passes on an empty purchase order. The
    only assertion that catches this is the one about the lines.
    """
    handler, sent = build()
    result = run(tmp_path, handler)

    assert result.drafts_created, result.warnings

    order_writes = writes(sent, "purchase/order")
    assert order_writes, "no lines were ever sent — the draft would be empty"

    _verb, body = order_writes[0]
    assert body["TaskID"] == NEW_ID
    assert body["Status"] == "DRAFT"
    assert [ln["SKU"] for ln in body["Lines"]] == ["CUP"]
    assert body["Lines"][0]["Quantity"] > 0


def test_the_header_never_carries_lines(tmp_path):
    """Cin7 accepts them there and ignores them. Sending them invites the bug back."""
    handler, sent = build()
    run(tmp_path, handler)

    _verb, header = writes(sent, "purchase")[0]
    assert "Order" not in header
    assert "Lines" not in header


def test_a_failed_line_write_is_not_reported_as_success(tmp_path):
    """An empty purchase order in Cin7 must never read as a draft created.

    Somebody has to go and delete it, and they will not go looking if the run
    said it worked.
    """
    handler, sent = build(order_verb_fails="POST")

    def both_fail(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("purchase/order"):
            body = json.loads(request.content or b"{}")
            sent.append((request.method, "purchase/order", body))
            return httpx.Response(400, text="nope")
        return handler(request)

    result = run(tmp_path, both_fail)

    assert not result.drafts_created
    assert any("Failed to write draft" in w for w in result.warnings)


def test_post_is_tried_first_and_put_is_only_a_fallback(tmp_path):
    """Confirmed live: PUT answers 405 at the endpoint, not per record.

    POST both creates and updates, so trying PUT first wasted a rejected call
    on every update. It stays as a fallback because a wrong guess costs one
    call while not trying costs a draft that never picks up its quantities.
    """
    handler, sent = build()
    result = run(tmp_path, handler)

    assert [verb for verb, _body in writes(sent, "purchase/order")] == ["POST"]
    assert result.drafts_created, result.warnings


def test_put_is_still_tried_when_post_is_refused(tmp_path):
    handler, sent = build(order_verb_fails="POST")
    result = run(tmp_path, handler)

    verbs = [verb for verb, _body in writes(sent, "purchase/order")]
    assert verbs == ["POST", "PUT"]
    assert result.drafts_created, result.warnings
