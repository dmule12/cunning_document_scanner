"""What the run refuses to spend API calls on.

Purchases are the only records fetched one at a time. Everything else pages
in bulk at 500 records a call; a purchase costs one call each, or two for an
Advanced purchase, which answers a 400 naming the right endpoint before it
answers anything useful.

So an account with several hundred open orders will exhaust Cin7's 5000/day
allowance in this one stage and spend the rest of the run being 429'd — while
looking, from the outside, like it is simply slow. An earlier version did
exactly that: it filtered the purchase list by status but not by supplier, so
a run pinned to one supplier still read every other supplier's paperwork.

The tests here count requests, because that is the failure. A run that
produces correct numbers by reading the whole account is still broken.
"""

from __future__ import annotations

import httpx
import pytest

from cin7_reorder.client import Cin7Client, NullRateLimiter
from cin7_reorder.config import ApiConfig, Config, Credentials, SupplierConfig
from cin7_reorder.pipeline import Pipeline

OURS = "sup-ours"
THEIRS = "sup-theirs"


def build(rows: list[dict]):
    """A Cin7 whose purchase list is exactly ``rows``, counting detail reads."""
    fetched: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        tail = path.rsplit("/", 1)[-1]
        page = int(request.url.params.get("page", 1))

        def page1(key: str, records: list) -> httpx.Response:
            return httpx.Response(200, json={key: records if page == 1 else []})

        if tail == "supplier":
            return page1(
                "SupplierList",
                [
                    {"ID": OURS, "Name": "BioPak", "AdditionalAttribute1": "Yes"},
                    {"ID": THEIRS, "Name": "Someone Else"},
                ],
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
                        "Suppliers": [{"SupplierID": OURS}],
                    }
                ],
            )

        if tail == "productAvailability":
            if "ref/" not in path:
                return httpx.Response(200, text="<html>Object moved</html>")
            return page1(
                "ProductAvailabilityList",
                [{"ProductID": "p1", "Location": "Main", "OnHand": 5}],
            )

        if tail == "purchaseList":
            return page1("PurchaseList", rows)

        if tail == "purchase":
            purchase_id = request.url.params.get("ID")
            fetched.append(purchase_id)
            return httpx.Response(
                200,
                json={
                    "ID": purchase_id,
                    "OrderStatus": "AUTHORISED",
                    "Location": "Main",
                    "Order": {"Lines": [{"ProductID": "p1", "Quantity": 1}]},
                },
            )

        return httpx.Response(200, text="<html>Object moved</html>")

    return handler, fetched


def run(tmp_path, rows, *, dry_run=True, max_details=250):
    handler, fetched = build(rows)
    client = Cin7Client(
        Credentials(account_id="a", app_key="k"),
        ApiConfig(daily_call_budget=2000, max_purchase_details=max_details),
        read_only=dry_run,
        transport=httpx.MockTransport(handler),
        rate_limiter=NullRateLimiter(),
    )
    result = Pipeline(
        client=client,
        config=Config(suppliers=SupplierConfig(attribute_field="AdditionalAttribute1")),
        state_path=tmp_path / "state.json",
        dry_run=dry_run,
    ).run()
    return result, fetched


def test_other_suppliers_orders_are_never_fetched(tmp_path):
    """The bug this file exists for.

    Three hundred open orders from a supplier we are not ordering from cost
    three hundred calls and tell us nothing. On the account this was found on
    they were most of the list.
    """
    rows = [{"ID": f"theirs-{n}", "OrderStatus": "AUTHORISED", "SupplierID": THEIRS} for n in range(300)]
    rows.append({"ID": "ours-1", "OrderStatus": "AUTHORISED", "SupplierID": OURS})

    result, fetched = run(tmp_path, rows)

    assert fetched == ["ours-1"], f"read {len(fetched)} purchases, expected 1"
    assert result.aborted is None


def test_supplier_matched_by_name_when_the_row_has_no_id(tmp_path):
    """Cin7's list rows are not consistent about which it gives you."""
    rows = [
        {"ID": "ours-1", "OrderStatus": "AUTHORISED", "Supplier": "biopak"},
        {"ID": "theirs-1", "OrderStatus": "AUTHORISED", "Supplier": "Someone Else"},
    ]

    _result, fetched = run(tmp_path, rows)

    assert fetched == ["ours-1"]


def test_a_row_with_no_supplier_at_all_is_still_read(tmp_path):
    """Cheapness must not win over correctness here.

    An open order we skip is inbound stock we cannot see, and inbound stock we
    cannot see is a duplicate order for goods already on the water. If the row
    does not say who it is from, the only safe answer is to look.
    """
    rows = [
        {"ID": "unknown-1", "OrderStatus": "AUTHORISED"},
        {"ID": "theirs-1", "OrderStatus": "AUTHORISED", "SupplierID": THEIRS},
    ]

    result, fetched = run(tmp_path, rows)

    assert fetched == ["unknown-1"]
    assert any(
        "carry no supplier" in w for w in result.warnings
    ), "reading every unattributed order should be flagged as expensive"


def test_closed_orders_still_cost_nothing(tmp_path):
    rows = [
        {"ID": "done-1", "OrderStatus": "COMPLETED", "SupplierID": OURS},
        {"ID": "void-1", "OrderStatus": "VOIDED", "SupplierID": OURS},
        {"ID": "ours-1", "OrderStatus": "AUTHORISED", "SupplierID": OURS},
    ]

    _result, fetched = run(tmp_path, rows)

    assert fetched == ["ours-1"]


def test_coverage_is_reported_not_silent(tmp_path):
    """A number computed from a partial view has to say so.

    Skipping other suppliers' orders is right almost always and wrong in one
    case: when one of them happens to carry a product we are about to reorder.
    Nothing in the arithmetic can detect that, so the report says what it did
    not read and leaves the judgement to a person.
    """
    rows = [
        {"ID": "ours-1", "OrderStatus": "AUTHORISED", "SupplierID": OURS},
        {"ID": "theirs-1", "OrderStatus": "AUTHORISED", "SupplierID": THEIRS},
    ]

    result, _fetched = run(tmp_path, rows)

    assert any("Read 1 open purchase order" in n for n in result.notes)
    assert any(
        "1 open purchase order(s) belong to suppliers" in n for n in result.notes
    )
    # Not a warning: nothing is wrong. Warnings that fire on every healthy run
    # are warnings nobody reads.
    assert not any("belong to suppliers" in w for w in result.warnings)


def test_plan_warns_when_it_runs_out_of_purchase_reads(tmp_path):
    rows = [
        {"ID": f"ours-{n}", "OrderStatus": "AUTHORISED", "SupplierID": OURS}
        for n in range(10)
    ]

    result, fetched = run(tmp_path, rows, max_details=4)

    assert len(fetched) == 4
    assert result.aborted is None, "plan should still produce a report"
    assert any("INBOUND STOCK MAY BE UNDERSTATED" in w for w in result.warnings)
    assert any("max_purchase_details" in w for w in result.warnings)


def test_apply_refuses_when_it_runs_out_of_purchase_reads(tmp_path):
    """Writing orders off a truncated inbound view costs real money."""
    rows = [
        {"ID": f"ours-{n}", "OrderStatus": "AUTHORISED", "SupplierID": OURS}
        for n in range(10)
    ]

    result, _fetched = run(tmp_path, rows, dry_run=False, max_details=4)

    assert result.aborted is not None
    assert "Refusing to create purchase orders" in result.aborted
    assert not result.drafts_created
