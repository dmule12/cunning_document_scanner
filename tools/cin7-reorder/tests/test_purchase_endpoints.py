"""Reading purchases across Cin7's several purchase-order types.

``/purchase`` refuses Advanced and Service purchases with a 400 naming the
endpoint to use instead. Missing one of those means understating inbound
stock, which means re-ordering goods already on their way — the exact
failure this tool exists to prevent.
"""

from __future__ import annotations

import json
import pathlib

import httpx
import pytest

from cin7_reorder.client import Cin7Client, Cin7Error, NullRateLimiter
from cin7_reorder.config import ApiConfig, Config, Credentials, SupplierConfig
from cin7_reorder.pipeline import Pipeline

ADVANCED_ID = "adv-1"
SIMPLE_ID = "simple-1"

DEPRECATED_BODY = json.dumps(
    [
        {
            "ErrorCode": 400,
            "Exception": (
                "This endpoint is deprecated and does not support Advanced "
                "Purchase and Service Purchase. Please use AdvancedPurchase "
                "endpoint"
            ),
        }
    ]
)

NOT_FOUND_HTML = "<html>Object moved to <a href='/Error/NotFound'>here</a>.</html>"


def make_handler(*, advanced_works: bool = True):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        tail = path.rsplit("/", 1)[-1]
        product_id = request.url.params.get("ID")

        if tail == "productAvailability":
            if "ref/" in path:
                return httpx.Response(
                    200,
                    json={
                        "ProductAvailabilityList": [
                            {
                                "ProductID": "p1",
                                "Location": "Main",
                                "OnHand": 5,
                                "Allocated": 0,
                            }
                        ]
                    },
                )
            return httpx.Response(200, text=NOT_FOUND_HTML)

        if tail == "purchase":
            if product_id == ADVANCED_ID:
                return httpx.Response(400, text=DEPRECATED_BODY)
            return httpx.Response(
                200,
                json={
                    "ID": product_id,
                    "OrderStatus": "AUTHORISED",
                    "Location": "Main",
                    "Order": {"Lines": []},
                },
            )

        if tail.lower() == "advancedpurchase":
            if not advanced_works:
                return httpx.Response(200, text=NOT_FOUND_HTML)
            return httpx.Response(
                200,
                json={
                    "ID": product_id,
                    "OrderStatus": "AUTHORISED",
                    "Location": "Main",
                    "SupplierID": "s1",
                    "Order": {
                        "Lines": [{"ProductID": "p1", "SKU": "CUP", "Quantity": 10}]
                    },
                },
            )

        if tail == "supplier":
            return httpx.Response(
                200,
                json={
                    "SupplierList": [
                        {
                            "ID": "s1",
                            "Name": "BioPak",
                            "AdditionalAttribute1": "true",
                        }
                    ]
                },
            )

        if tail == "product":
            return httpx.Response(
                200,
                json={
                    "Products": [
                        {
                            "ID": "p1",
                            "SKU": "CUP",
                            "MinimumBeforeReorder": 100,
                            "ReorderQuantity": 48,
                            "Suppliers": [{"SupplierID": "s1"}],
                        }
                    ]
                },
            )

        if tail == "purchaseList":
            return httpx.Response(
                200,
                json={
                    "PurchaseList": [
                        {"ID": SIMPLE_ID, "OrderStatus": "AUTHORISED"},
                        {"ID": ADVANCED_ID, "OrderStatus": "AUTHORISED"},
                    ]
                },
            )

        # Cin7's answer for an unknown path: a redirect to an HTML error page,
        # not a 404. Reproduced so endpoint resolution is tested against the
        # real behaviour rather than a friendly empty JSON body.
        return httpx.Response(200, text=NOT_FOUND_HTML)

    return handler


def run_pipeline(tmp_path, *, advanced_works: bool = True, dry_run: bool = True):
    client = Cin7Client(
        Credentials(account_id="a", app_key="k"),
        ApiConfig(daily_call_budget=200),
        read_only=dry_run,
        transport=httpx.MockTransport(make_handler(advanced_works=advanced_works)),
        rate_limiter=NullRateLimiter(),
    )
    return Pipeline(
        client=client,
        config=Config(
            suppliers=SupplierConfig(attribute_field="AdditionalAttribute1")
        ),
        state_path=tmp_path / "state.json",
        dry_run=dry_run,
    ).run()


def test_advanced_purchase_stock_counts_as_inbound(tmp_path):
    """The 400 is a redirect in disguise, not a failure."""
    result = run_pipeline(tmp_path)

    assert result.aborted is None
    line = next(ln for ln in result.lines if ln.base_sku == "CUP")
    assert line.inbound_base == 10, "advanced purchase was not counted as inbound"


def test_plan_warns_loudly_but_still_reports(tmp_path):
    """A read-only run should show its working, not vanish.

    The inbound figure is understated by an unknown amount, which matters —
    but plan writes nothing, and a report you can read and judge beats no
    report at all. The warning has to be impossible to miss.
    """
    result = run_pipeline(tmp_path, advanced_works=False, dry_run=True)

    assert result.aborted is None
    assert result.lines, "plan produced nothing at all"

    warning = next(
        (w for w in result.warnings if "INBOUND STOCK MAY BE UNDERSTATED" in w),
        None,
    )
    assert warning is not None, "the understatement was not flagged"
    assert "70426f26" in warning or "adv-1" in warning


def test_apply_refuses_to_write_from_incomplete_inbound(tmp_path):
    """Creating a purchase order off partial inbound data costs real money.

    Understated inbound means re-ordering goods already in transit, so the
    write path stops rather than producing a plausible duplicate.
    """
    result = run_pipeline(tmp_path, advanced_works=False, dry_run=False)

    assert result.aborted is not None
    assert "Refusing to create purchase orders" in result.aborted
    assert not result.drafts_created


def test_availability_resolves_past_the_documented_path(tmp_path):
    """The bare productAvailability path 404s; ref/productAvailability works."""
    result = run_pipeline(tmp_path)
    assert result.aborted is None
    line = next(ln for ln in result.lines if ln.base_sku == "CUP")
    assert line.on_hand == 5
