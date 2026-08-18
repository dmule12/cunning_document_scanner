"""Leaving a warehouse out.

Not every shortfall is something to buy. A warehouse stocked by transfer from
another one will read as short of everything, and raising supplier purchase
orders against it would be wrong in a way that looks entirely normal on the
report — real SKUs, real quantities, real supplier.
"""

from __future__ import annotations

import httpx
import pytest

from cin7_reorder.client import Cin7Client, NullRateLimiter
from cin7_reorder.config import ApiConfig, Config, Credentials, SupplierConfig
from cin7_reorder.models import ReorderParameters
from cin7_reorder.pipeline import Pipeline
from cin7_reorder.reorderpoints import resolve

SUPPLIER = "sup-1"


def test_excluded_location_is_dropped():
    config = Config(locations_exclude=("VIC Warehouse",))
    assert not config.includes_location("VIC Warehouse")
    assert config.includes_location("WA Warehouse")


def test_exclusion_ignores_case_and_stray_spaces():
    """Warehouse names are typed by hand and pasted out of Cin7's UI.

    A near miss here fails in the worst direction: the run orders for a
    warehouse somebody wrote down that they wanted left alone.
    """
    config = Config(locations_exclude=("  vic warehouse ",))
    assert not config.includes_location("VIC Warehouse")


def test_exclude_beats_include():
    """Deny wins over allow, as everywhere else that has both."""
    config = Config(
        locations_include=("VIC Warehouse", "WA Warehouse"),
        locations_exclude=("VIC Warehouse",),
    )
    assert not config.includes_location("VIC Warehouse")
    assert config.includes_location("WA Warehouse")


def test_an_excluded_locations_reorder_point_is_never_borrowed():
    """The second half of "ignore VIC and all of its reorder levels".

    A location-level minimum only ever applies to its own location, so a VIC
    override cannot leak into the WA calculation. If it could, excluding the
    warehouse would silently change what gets ordered for the one kept.
    """
    candidates = [
        ReorderParameters(
            product_id="p1",
            supplier_id=SUPPLIER,
            location="VIC Warehouse",
            minimum_before_reorder=999,
            reorder_quantity=999,
        ),
        ReorderParameters(
            product_id="p1",
            supplier_id=SUPPLIER,
            location=None,
            minimum_before_reorder=100,
            reorder_quantity=48,
        ),
    ]

    point = resolve(candidates, supplier_id=SUPPLIER, location="WA Warehouse")

    assert point.minimum == 100
    assert point.source == "product"


def _handler(locations: list[str]):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        tail = path.rsplit("/", 1)[-1]
        page = int(request.url.params.get("page", 1))

        def page1(key: str, rows: list) -> httpx.Response:
            return httpx.Response(200, json={key: rows if page == 1 else []})

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

        if tail == "productAvailability":
            if "ref/" not in path:
                return httpx.Response(200, text="<html>Object moved</html>")
            return page1(
                "ProductAvailabilityList",
                [
                    {"ProductID": "p1", "Location": loc, "OnHand": 0}
                    for loc in locations
                ],
            )

        if tail == "purchaseList":
            return page1("PurchaseList", [])

        return httpx.Response(200, text="<html>Object moved</html>")

    return handler


def _run(tmp_path, config, locations):
    client = Cin7Client(
        Credentials(account_id="a", app_key="k"),
        ApiConfig(daily_call_budget=200),
        read_only=True,
        transport=httpx.MockTransport(_handler(locations)),
        rate_limiter=NullRateLimiter(),
    )
    return Pipeline(
        client=client,
        config=config,
        state_path=tmp_path / "state.json",
        dry_run=True,
    ).run()


def _config(**kwargs) -> Config:
    return Config(
        suppliers=SupplierConfig(attribute_field="AdditionalAttribute1"), **kwargs
    )


def test_no_order_lines_for_an_excluded_warehouse(tmp_path):
    result = _run(
        tmp_path,
        _config(locations_exclude=("VIC Warehouse",)),
        ["WA Warehouse", "VIC Warehouse"],
    )

    locations = {line.location for line in result.lines}
    assert locations == {"WA Warehouse"}
    # Nor should it clutter the skip list — it was never in scope to skip.
    assert not [s for s in result.skipped if s.location == "VIC Warehouse"]


def test_the_exclusion_is_stated_in_the_report(tmp_path):
    """A warehouse quietly missing from the report is indistinguishable from
    a warehouse the run failed to read."""
    result = _run(
        tmp_path,
        _config(locations_exclude=("VIC Warehouse",)),
        ["WA Warehouse", "VIC Warehouse"],
    )

    assert any("VIC Warehouse" in note for note in result.notes)


def test_a_filter_matching_no_warehouse_is_flagged(tmp_path):
    """The dangerous case: configured, and doing nothing.

    A typo or a rename in Cin7 turns "leave VIC alone" into orders raised for
    VIC, and nothing else in the report would say so.
    """
    result = _run(
        tmp_path,
        _config(locations_exclude=("Victoria Warehouse",)),
        ["WA Warehouse", "VIC Warehouse"],
    )

    warning = next(
        (w for w in result.warnings if "names no warehouse" in w), None
    )
    assert warning is not None
    assert "Victoria Warehouse" in warning
    assert "VIC Warehouse" in warning, "should say what the real names are"
