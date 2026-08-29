"""The explain command: tracing one product through the run.

Every silent skip so far was found the same way — a person noticing an
absence on a draft and asking "why is X missing?". These tests pin the
answers explain gives for each of the ways a product can vanish, so the
command stays a complete answer to that question.
"""

from __future__ import annotations

import httpx

from cin7_reorder.client import Cin7Client, NullRateLimiter
from cin7_reorder.config import ApiConfig, Config, Credentials, SupplierConfig
from cin7_reorder.models import RunResult, SkipReason, SkippedProduct
from cin7_reorder.pipeline import Pipeline
from cin7_reorder.report import render_markdown

PINNED = "sup-biopak"
UNPINNED = "sup-somage"


def handler(request: httpx.Request) -> httpx.Response:
    tail = request.url.path.split("/ExternalApi/v2/", 1)[-1]
    page = int(request.url.params.get("page", 1))

    def page1(key, rows):
        return httpx.Response(200, json={key: rows if page == 1 else []})

    if tail == "supplier":
        return page1(
            "SupplierList",
            [
                {"ID": PINNED, "Name": "BioPak"},
                {"ID": UNPINNED, "Name": "Somage Fine Foods"},
            ],
        )
    if tail == "product":
        return page1(
            "Products",
            [
                {
                    # Below its minimum, supplier exists but is not pinned.
                    "ID": "p-chai",
                    "SKU": "CHAI1",
                    "Name": "Bond St Chai",
                    "MinimumBeforeReorder": 12,
                    "ReorderQuantity": 24,
                    "Suppliers": [
                        {"SupplierID": UNPINNED, "SupplierName": "Somage Fine Foods"}
                    ],
                },
                {
                    # The API returns no supplier at all.
                    "ID": "p-napkin",
                    "SKU": "NAPKIN1",
                    "Name": "Napkin Cocktail White",
                    "MinimumBeforeReorder": 10,
                    "ReorderQuantity": 20,
                },
                {
                    # Points at a supplier id absent from the supplier list.
                    "ID": "p-granola",
                    "SKU": "GRANOLA1",
                    "Name": "Granola 1kg",
                    "MinimumBeforeReorder": 5,
                    "ReorderQuantity": 10,
                    "Suppliers": [
                        {"SupplierID": "sup-gone", "SupplierName": "Merre Granola"}
                    ],
                },
                {
                    # A pack: never evaluated against its own stock.
                    "ID": "p-chai-box",
                    "SKU": "CHAIBOX",
                    "Name": "Bond St Chai - Box of 6",
                    "Suppliers": [{"SupplierID": UNPINNED}],
                    "BillOfMaterialsProducts": [
                        {"ProductID": "p-chai", "Quantity": 6}
                    ],
                },
            ],
        )
    if tail.endswith("productAvailability"):
        if not tail.startswith("ref/"):
            return httpx.Response(200, text="<html>Object moved</html>")
        return page1(
            "ProductAvailabilityList",
            [{"ProductID": "p-chai", "Location": "WA", "OnHand": 4}],
        )
    if tail == "purchaseList":
        return page1("PurchaseList", [])
    return httpx.Response(200, text="<html>Object moved</html>")


def explain(fragments):
    client = Cin7Client(
        Credentials(account_id="a", app_key="k"),
        ApiConfig(daily_call_budget=200),
        read_only=True,
        transport=httpx.MockTransport(handler),
        rate_limiter=NullRateLimiter(),
    )
    pipeline = Pipeline(
        client=client,
        config=Config(suppliers=SupplierConfig(pin=("BioPak",))),
        state_path=None,
        dry_run=True,
    )
    return pipeline.explain(fragments)


def test_an_unpinned_supplier_is_named_with_both_remedies():
    out = explain(["chai"])
    assert "Somage Fine Foods" in out
    assert "NOT automated" in out
    assert "suppliers.pin" in out
    # Below its minimum, so the trigger fact is stated too.
    assert "at or below its minimum and WOULD order" in out


def test_a_product_with_no_api_supplier_says_so_plainly():
    out = explain(["napkin"])
    assert "NONE" in out
    assert "Suppliers tab" in out


def test_a_stale_supplier_link_is_distinguished_from_an_unpinned_one():
    """'Not in the pin' and 'points at a deleted supplier' need different
    fixes; lumping them together sends the user to edit the wrong thing."""
    out = explain(["granola"])
    assert "does not exist in the account's supplier list" in out


def test_a_pack_sku_explains_the_component_indirection():
    out = explain(["CHAIBOX"])
    assert "PACK SKU" in out
    assert "CHAI1" in out


def test_no_match_is_an_answer_not_an_error():
    out = explain(["zzz-not-a-product"])
    assert "No product in the catalogue matches" in out


def test_supplier_not_opted_in_rows_sort_above_the_skip_table_overflow():
    """The live burial: sorted alphabetically by reason value,
    supplier_not_opted_in came last — behind 100+ supplier-less gift cards —
    and fell into the truncated overflow, invisible in the Markdown."""
    result = RunResult()
    for n in range(60):
        result.skipped.append(
            SkippedProduct(
                base_product_id=f"junk-{n:03}",
                base_sku=f"AAA-{n:03}",
                location="",
                reason=SkipReason.NO_SUPPLIER,
                detail="gift card",
            )
        )
    result.skipped.append(
        SkippedProduct(
            base_product_id="p-chai",
            base_sku="ZZZ-CHAI",
            location="WA",
            reason=SkipReason.SUPPLIER_NOT_OPTED_IN,
            detail="Bond St Chai is at or below its minimum",
        )
    )

    markdown = render_markdown(result, dry_run=True)
    assert "ZZZ-CHAI" in markdown
