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

from cin7_reorder import schema
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


def test_a_received_order_costs_nothing_even_while_still_authorised(tmp_path):
    """Cin7 never moves Status off AUTHORISED.

    An order placed, received and invoiced two years ago still reads as
    AUTHORISED, so Status alone makes nearly every purchase the account has
    ever raised look open. On a real account that was five pages of list rows
    and a detail call for each.
    """
    rows = [
        {
            "ID": f"done-{n}",
            "OrderStatus": "AUTHORISED",
            "CombinedReceivingStatus": "RECEIVED",
            "SupplierID": OURS,
        }
        for n in range(50)
    ]
    rows.append(
        {
            "ID": "live-1",
            "OrderStatus": "AUTHORISED",
            "CombinedReceivingStatus": "NOT RECEIVED",
            "SupplierID": OURS,
        }
    )

    _result, fetched = run(tmp_path, rows)

    assert fetched == ["live-1"]


def test_partially_received_is_not_received(tmp_path):
    """The string trap, and the expensive direction to get it wrong.

    "PARTIALLY RECEIVED" contains "RECEIVED". Matching on a substring or a
    token would close an order that still has stock on the water, which
    understates inbound and re-orders goods already paid for.
    """
    rows = [
        {
            "ID": "part-1",
            "OrderStatus": "AUTHORISED",
            "CombinedReceivingStatus": "PARTIALLY RECEIVED",
            "SupplierID": OURS,
        }
    ]

    _result, fetched = run(tmp_path, rows)

    assert fetched == ["part-1"], "an order still in transit was dropped"


def test_an_order_with_no_receiving_status_is_treated_as_open(tmp_path):
    """Silence is not evidence of arrival."""
    rows = [{"ID": "quiet-1", "OrderStatus": "AUTHORISED", "SupplierID": OURS}]

    _result, fetched = run(tmp_path, rows)

    assert fetched == ["quiet-1"]


#: Rows shaped exactly as a live account returns them, from a survey of 2312
#: purchase orders. The three status fields disagree with each other on almost
#: every row, which is the whole reason this needs testing rather than reading.
LIVE_SHAPES = [
    # 1946 rows looked like this: authorised forever, received years ago.
    ({"OrderStatus": "AUTHORISED", "Status": "COMPLETED",
      "CombinedReceivingStatus": "FULLY RECEIVED"}, True),
    # 32 rows. OrderStatus=CLOSED was read as UNKNOWN, so as open.
    ({"OrderStatus": "CLOSED", "Status": "COMPLETED",
      "CombinedReceivingStatus": "FULLY RECEIVED"}, True),
    ({"OrderStatus": "VOIDED", "Status": "VOIDED",
      "CombinedReceivingStatus": "NOT AVAILABLE"}, True),
    # The ones that genuinely have stock coming.
    ({"OrderStatus": "AUTHORISED", "Status": "ORDERED",
      "CombinedReceivingStatus": "NOT RECEIVED"}, False),
    ({"OrderStatus": "AUTHORISED", "Status": "RECEIVING",
      "CombinedReceivingStatus": "PARTIALLY RECEIVED"}, False),
    ({"OrderStatus": "DRAFT", "Status": "DRAFT",
      "CombinedReceivingStatus": ""}, False),
    # Invoiced but nothing received: the money moved, the goods have not.
    ({"OrderStatus": "AUTHORISED", "Status": "INVOICED",
      "CombinedReceivingStatus": "NOT RECEIVED"}, False),
    # Seen live on an order whose only status was INVOICED. It must read as
    # open, not as an unrecognised value: unrecognised warns on every run.
    ({"OrderStatus": "INVOICED", "CombinedReceivingStatus": "NOT RECEIVED"}, False),
]


def test_invoiced_is_a_recognised_status():
    """An invoice is paperwork, not a delivery.

    Left unmapped this warned on every run, and the warning had no action
    behind it — whether stock is still coming is settled per line, by
    subtracting received from ordered, not by the status string.
    """
    entry = schema.parse_purchase_list_entry({"ID": "x", "OrderStatus": "INVOICED"})
    assert entry.status is not schema.PurchaseStatus.UNKNOWN
    assert not entry.is_closed


@pytest.mark.parametrize("row,closed", LIVE_SHAPES)
def test_live_status_combinations(row, closed):
    entry = schema.parse_purchase_list_entry({"ID": "x", **row})
    assert entry.is_closed is closed, (
        f"{row} was read as {'closed' if entry.is_closed else 'open'}"
    )


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


def build_advanced(rows: list[dict], *, simple_ids: set[str] = frozenset()):
    """A Cin7 where purchases are Advanced unless listed in ``simple_ids``.

    ``/purchase`` refuses an Advanced purchase with a 400 naming the right
    endpoint, so reading one the naive way costs two calls: a refusal and an
    answer.
    """
    handler, fetched = build(rows)
    calls: list[str] = []

    deprecated = (
        '[{"ErrorCode":400,"Exception":"This endpoint is deprecated and does '
        "not support Advanced Purchase and Service Purchase. Please use "
        'AdvancedPurchase endpoint"}]'
    )

    def wrapped(request: httpx.Request) -> httpx.Response:
        tail = request.url.path.rsplit("/", 1)[-1]
        purchase_id = request.url.params.get("ID")

        if tail == "purchase":
            calls.append(f"purchase:{purchase_id}")
            if purchase_id in simple_ids:
                return handler(request)
            return httpx.Response(400, text=deprecated)

        if tail == "advanced-purchase":
            calls.append(f"advanced:{purchase_id}")
            if purchase_id in simple_ids:
                return httpx.Response(400, text="not an advanced purchase")
            return httpx.Response(
                200,
                json={
                    "ID": purchase_id,
                    "OrderStatus": "AUTHORISED",
                    "Location": "Main",
                    "Order": {"Lines": [{"ProductID": "p1", "Quantity": 1}]},
                },
            )

        if tail.lower().replace("-", "") == "advancedpurchase":
            # Every other spelling 404s, which Cin7 serves as a redirect to an
            # HTML error page rather than a status code.
            return httpx.Response(200, text="<html>Object moved</html>")

        return handler(request)

    return wrapped, calls


def run_advanced(tmp_path, rows, *, simple_ids=frozenset()):
    handler, calls = build_advanced(rows, simple_ids=simple_ids)
    client = Cin7Client(
        Credentials(account_id="a", app_key="k"),
        ApiConfig(daily_call_budget=2000),
        read_only=True,
        transport=httpx.MockTransport(handler),
        rate_limiter=NullRateLimiter(),
    )
    result = Pipeline(
        client=client,
        config=Config(suppliers=SupplierConfig(attribute_field="AdditionalAttribute1")),
        state_path=tmp_path / "state.json",
        dry_run=True,
    ).run()
    return result, calls


def test_advanced_purchases_stop_paying_the_400_tax(tmp_path):
    """Every order on the account is Advanced. Don't ask /purchase each time.

    The naive order costs two calls per purchase: a 400 telling us the
    endpoint is wrong, then the answer. Accounts are not mixed at random —
    whichever endpoint served the last one is overwhelmingly likely to serve
    the next.
    """
    rows = [
        {"ID": f"ours-{n}", "OrderStatus": "AUTHORISED", "SupplierID": OURS}
        for n in range(6)
    ]

    result, calls = run_advanced(tmp_path, rows)

    assert result.aborted is None
    refusals = [c for c in calls if c.startswith("purchase:")]
    assert len(refusals) == 1, (
        f"paid the 400 tax {len(refusals)} times; it should be paid once, "
        "on the first purchase, and never again"
    )
    assert len([c for c in calls if c.startswith("advanced:")]) == 6


def test_the_type_field_skips_the_400_entirely(tmp_path):
    """The list row says which kind of purchase it is. Believe it.

    ``Type`` is "Simple Purchase" / "Advanced Purchase" / "Service Purchase",
    which means the wasted 400 need never be paid at all — not even once.
    """
    rows = [
        {
            "ID": f"ours-{n}",
            "OrderStatus": "AUTHORISED",
            "SupplierID": OURS,
            "Type": "Advanced Purchase",
        }
        for n in range(6)
    ]

    result, calls = run_advanced(tmp_path, rows)

    assert result.aborted is None
    assert not [c for c in calls if c.startswith("purchase:")], (
        "asked /purchase even though the row said the order was Advanced"
    )


def test_a_wrong_type_field_costs_a_call_not_an_order(tmp_path):
    """The hint is a starting point, never a filter.

    If Type says Simple and the endpoint disagrees, the order must still be
    found. A dropped order is inbound stock that vanishes, and this tool
    exists to stop exactly that.
    """
    rows = [
        {
            "ID": "adv-1",
            "OrderStatus": "AUTHORISED",
            "SupplierID": OURS,
            "Type": "Simple Purchase",  # a lie, as far as the endpoints care
        }
    ]

    result, _calls = run_advanced(tmp_path, rows)

    assert result.aborted is None
    line = next(ln for ln in result.lines if ln.base_sku == "CUP")
    assert line.inbound_base == 1, "the order was dropped rather than retried"


def test_a_simple_purchase_among_advanced_ones_is_still_read(tmp_path):
    """The preference is an optimisation, never a filter.

    Once the run prefers the Advanced endpoint, a plain purchase order will
    be refused by it — and must then fall back, not be dropped. A dropped
    order is inbound stock that vanishes.
    """
    rows = [
        {"ID": "adv-1", "OrderStatus": "AUTHORISED", "SupplierID": OURS},
        {"ID": "simple-1", "OrderStatus": "AUTHORISED", "SupplierID": OURS},
        {"ID": "adv-2", "OrderStatus": "AUTHORISED", "SupplierID": OURS},
    ]

    result, _calls = run_advanced(tmp_path, rows, simple_ids={"simple-1"})

    assert result.aborted is None
    assert not [
        w for w in result.warnings if "INBOUND STOCK MAY BE UNDERSTATED" in w
    ], "an order was dropped rather than fetched from the other endpoint"
    line = next(ln for ln in result.lines if ln.base_sku == "CUP")
    assert line.inbound_base == 3, "all three orders should count as inbound"


def test_a_response_for_a_different_purchase_is_rejected(tmp_path):
    """Trying endpoints in turn means occasionally asking the wrong one.

    A wrong endpoint answering 200 with something unrelated would be read as
    a purchase with no lines — which silently removes stock from the inbound
    figure and re-orders goods already on their way. Worse than an error.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        base, _ = build([{"ID": "ours-1", "OrderStatus": "AUTHORISED", "SupplierID": OURS}])
        if request.url.path.rsplit("/", 1)[-1] == "purchase":
            return httpx.Response(
                200, json={"ID": "somebody-elses-order", "Order": {"Lines": []}}
            )
        return base(request)

    client = Cin7Client(
        Credentials(account_id="a", app_key="k"),
        ApiConfig(daily_call_budget=2000),
        read_only=True,
        transport=httpx.MockTransport(handler),
        rate_limiter=NullRateLimiter(),
    )
    result = Pipeline(
        client=client,
        config=Config(suppliers=SupplierConfig(attribute_field="AdditionalAttribute1")),
        state_path=tmp_path / "state.json",
        dry_run=True,
    ).run()

    assert any(
        "INBOUND STOCK MAY BE UNDERSTATED" in w for w in result.warnings
    ), "a mismatched purchase was accepted as though it were the right one"


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
