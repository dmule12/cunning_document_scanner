"""End-to-end run against a fake Cin7, exercising the wiring.

The responses here are shaped the way ``schema.py`` *assumes* Cin7 responds.
That assumption is unverified — this suite proves the pipeline is correctly
wired, not that the field names are right. Only ``probe`` against a real
account can settle that.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from cin7_reorder.client import Cin7Client, NullRateLimiter
from cin7_reorder.config import ApiConfig, Config, Credentials, SafetyConfig, SupplierConfig
from cin7_reorder.models import LineFlag, SkipReason
from cin7_reorder.parlevels import StaticDemand
from cin7_reorder.pipeline import Pipeline

SLEEVE = "prod-sleeve"
BOX = "prod-box"
STOCKED_OUT = "prod-stockedout"
SUPPLIER_ON = "sup-on"
SUPPLIER_OFF = "sup-off"
LOCATION = "Main"


def _products() -> list[dict]:
    supplier_block = [
        {
            "SupplierID": SUPPLIER_ON,
            "Lead": 10,
            "Safety": 5,
            "ReorderQuantity": 2,
        }
    ]
    return [
        {
            "ID": SLEEVE,
            "SKU": "SLV-001",
            "Name": "Sleeve",
            "DefaultSupplierID": SUPPLIER_ON,
            "Suppliers": supplier_block,
        },
        {
            "ID": STOCKED_OUT,
            "SKU": "SLV-OUT",
            "Name": "Sold out sleeve",
            "DefaultSupplierID": SUPPLIER_ON,
            "Suppliers": supplier_block,
        },
        {
            "ID": BOX,
            "SKU": "BOX-024",
            "Name": "Box of 24",
            "DefaultSupplierID": SUPPLIER_ON,
            "Suppliers": supplier_block,
        },
        {
            "ID": "prod-other",
            "SKU": "OTH-1",
            "Name": "Other supplier's product",
            "DefaultSupplierID": SUPPLIER_OFF,
            "Suppliers": [
                {"SupplierID": SUPPLIER_OFF, "Lead": 5, "Safety": 5}
            ],
        },
    ]


def handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path.rsplit("/", 1)[-1]
    page = int(request.url.params.get("page", 1))

    def page1(key: str, rows: list) -> httpx.Response:
        return httpx.Response(200, json={key: rows if page == 1 else []})

    if path == "supplier":
        return page1(
            "SupplierList",
            [
                {
                    "ID": SUPPLIER_ON,
                    "Name": "Opted-in Supplier",
                    "AdditionalAttributes": [
                        {"Name": "Auto Reorder", "Value": "Yes"}
                    ],
                },
                {"ID": SUPPLIER_OFF, "Name": "Not Automated"},
            ],
        )

    if path == "product":
        return page1("Products", _products())

    if path == "BillOfMaterials":
        return page1(
            "BillOfMaterials",
            [{"ID": BOX, "BOMComponents": [{"ProductID": SLEEVE, "Quantity": 24}]}],
        )

    if path == "productAvailability":
        # Note what is NOT here: STOCKED_OUT has no row at all, because Cin7
        # omits records where on-hand, available and on-order are all zero.
        return page1(
            "ProductAvailabilityList",
            [
                {
                    "ProductID": SLEEVE,
                    "Location": LOCATION,
                    "OnHand": 10,
                    "Allocated": 0,
                    "OnOrder": 0,
                }
            ],
        )

    if path == "purchaseList":
        return page1(
            "PurchaseList",
            [{"ID": "po-open", "OrderStatus": "AUTHORISED", "Reference": "PO-1"}],
        )

    if path == "purchase":
        return httpx.Response(
            200,
            json={
                "ID": "po-open",
                "OrderStatus": "AUTHORISED",
                "SupplierID": SUPPLIER_ON,
                "Location": LOCATION,
                "Reference": "PO-1",
                "Order": {
                    "Lines": [{"ProductID": BOX, "SKU": "BOX-024", "Quantity": 3}]
                },
                "StockReceived": [
                    {"Lines": [{"ProductID": BOX, "Quantity": 1}]}
                ],
            },
        )

    return httpx.Response(200, json={})


@pytest.fixture
def pipeline(tmp_path: Path) -> Pipeline:
    client = Cin7Client(
        Credentials(account_id="acct", app_key="key"),
        ApiConfig(page_size=500, daily_call_budget=200),
        read_only=True,
        transport=httpx.MockTransport(handler),
        rate_limiter=NullRateLimiter(),
    )
    config = Config(
        suppliers=SupplierConfig(),
        safety=SafetyConfig(
            max_line_quantity=None,
            max_reorder_quantity_multiple=None,
            max_total_lines=None,
        ),
    )
    return Pipeline(
        client=client,
        config=config,
        demand=StaticDemand(
            {(SLEEVE, LOCATION): 6.0, (STOCKED_OUT, LOCATION): 2.0}
        ),
        state_path=tmp_path / "fingerprints.json",
        dry_run=True,
    )


def test_run_completes_without_aborting(pipeline):
    result = pipeline.run()
    assert result.aborted is None


def test_only_opted_in_suppliers_are_considered(pipeline):
    result = pipeline.run()
    assert result.suppliers_considered == ["Opted-in Supplier"]
    assert result.suppliers_skipped == ["Not Automated"]


def test_orders_the_box_and_nets_off_partial_receipt(pipeline):
    """The whole design in one assertion.

    par        = 6/day * (10 lead + 5 safety) = 90 sleeves
    on hand    = 10
    inbound    = (3 ordered - 1 received) boxes * 24 = 48 sleeves
    need       = 90 - (10 + 48) = 32 sleeves
    order      = ceil(32 / 24) = 2 boxes, against the BOX sku
    """
    result = pipeline.run()
    line = next(ln for ln in result.lines if ln.base_product_id == SLEEVE)

    assert line.par == 90
    assert line.inbound_base == 48
    assert line.need_base == 32
    assert line.order_product_id == BOX
    assert line.quantity == 2
    assert line.inbound_sources == ("po-open",)


def test_stocked_out_product_missing_from_availability_still_ordered(pipeline):
    """The productAvailability omission trap.

    STOCKED_OUT has no availability row at all. It must be treated as zero
    stock and ordered, not silently skipped — a product with nothing left is
    the one most urgently needing a PO.
    """
    result = pipeline.run()
    line = next(
        (ln for ln in result.lines if ln.base_product_id == STOCKED_OUT), None
    )

    assert line is not None, "stocked-out product was silently dropped"
    assert line.on_hand == 0
    # par = 2/day * 15 days = 30; no pack parent, so ordered as base units.
    assert line.need_base == 30
    assert LineFlag.ORDERED_AS_BASE_UNIT in line.flags


def test_pack_skus_are_not_reordered_in_their_own_right(pipeline):
    """Boxes disassemble on receipt; their own stock level is not a target."""
    result = pipeline.run()
    assert all(ln.base_product_id != BOX for ln in result.lines)


def test_other_suppliers_products_are_untouched(pipeline):
    result = pipeline.run()
    assert all(ln.base_product_id != "prod-other" for ln in result.lines)


def test_dry_run_writes_nothing(pipeline):
    result = pipeline.run()
    assert result.drafts_created == []
    assert result.drafts_updated == []
    assert not pipeline.state_path.exists()


def test_read_only_client_refuses_writes(pipeline):
    from cin7_reorder.client import ReadOnlyViolation

    with pytest.raises(ReadOnlyViolation):
        pipeline.client.post("purchase", {"anything": True})


def test_total_line_cap_aborts_the_run(tmp_path):
    client = Cin7Client(
        Credentials(account_id="a", app_key="k"),
        ApiConfig(daily_call_budget=200),
        read_only=True,
        transport=httpx.MockTransport(handler),
        rate_limiter=NullRateLimiter(),
    )
    config = Config(
        safety=SafetyConfig(
            max_line_quantity=None,
            max_reorder_quantity_multiple=None,
            max_total_lines=1,
        )
    )
    result = Pipeline(
        client=client,
        config=config,
        demand=StaticDemand(
            {(SLEEVE, LOCATION): 6.0, (STOCKED_OUT, LOCATION): 2.0}
        ),
        state_path=tmp_path / "s.json",
        dry_run=True,
    ).run()

    assert result.aborted is not None
    assert "cap" in result.aborted


def test_report_renders(pipeline):
    from cin7_reorder.report import render_json, render_markdown

    result = pipeline.run()
    markdown = render_markdown(result, dry_run=True)

    assert "Cin7 reorder run" in markdown
    assert "SLV-001" in markdown
    # The inbound provenance table is the only place this number is auditable.
    assert "po-open" in markdown

    parsed = json.loads(render_json(result, dry_run=True))
    assert parsed["mode"] == "plan"
    assert len(parsed["lines"]) == len(result.lines)
