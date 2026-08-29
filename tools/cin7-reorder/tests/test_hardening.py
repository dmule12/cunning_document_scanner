"""Fixes from the adversarial review, each pinned so it cannot quietly revert.

Every test here corresponds to a defect that survived three independent
refutation attempts. The two themes: failures that wedge a supplier out of
ordering permanently, and arithmetic that understates inbound — both of which
cost money while producing a report that looks perfectly normal.
"""

from __future__ import annotations

import json

import httpx
import pytest

from cin7_reorder import schema
from cin7_reorder.bom import BomIndex
from cin7_reorder.client import Cin7Client, Cin7Error, NullRateLimiter
from cin7_reorder.config import (
    ApiConfig,
    Config,
    ConfigError,
    Credentials,
    SafetyConfig,
    SupplierConfig,
)
from cin7_reorder.drafts import DraftDecision, decide, fingerprint_purchase
from cin7_reorder.inbound import reconstruct
from cin7_reorder.models import BillOfMaterials, BomComponent, SkipReason
from cin7_reorder.pipeline import Pipeline

SUPPLIER = "sup-1"
DRAFT_ID = "standing-draft-1"
NEW_ID = "created-po-1"


# ---------------------------------------------------------------------------
# The empty-draft wedge
# ---------------------------------------------------------------------------


def _empty_draft_record(marker: str) -> dict:
    return {
        "ID": DRAFT_ID,
        "Status": "ORDERING",
        "SupplierID": SUPPLIER,
        "Location": "WA",
        "Note": marker,
        "Order": {"Status": "DRAFT", "Memo": marker, "Lines": []},
    }


def test_an_empty_draft_of_ours_is_repaired_not_wedged():
    """The worst confirmed defect.

    A run that created the header and failed writing the lines used to leave
    a fingerprint-bearing empty draft. The fingerprint could never match zero
    lines, so every later run read the emptiness as a human edit and refused
    to order for that supplier+location — permanently, with the report
    blaming a person. An empty draft has nothing a human could have edited,
    so there is nothing for the never-clobber rule to protect.
    """
    marker = schema.build_marker("AUTO-REORDER-2026W35-WA-x", "deadbeef")
    parsed = schema.parse_purchase(_empty_draft_record(marker))
    assert parsed.is_draft and not parsed.lines

    plan = decide(
        existing=parsed,
        reference="AUTO-REORDER-2026W36-WA-x",
        stored_fingerprint=parsed.fingerprint,
        desired_fingerprint="something-new",
    )

    assert plan.decision == DraftDecision.UPDATE
    assert "empty" in plan.reason


# ---------------------------------------------------------------------------
# Receipt pool shared across duplicate lines
# ---------------------------------------------------------------------------


def test_duplicate_product_lines_share_the_receipt_pool():
    """Receipts are per product; lines can duplicate a product.

    Handing every duplicate line the full per-product total netted the same
    receipt off repeatedly: 15 ordered, 8 received used to read as 7 short on
    one line and fully received on the other — total outstanding 2 instead of
    7 — which understates inbound and re-orders goods already on their way...
    in the other direction: it OVERstates received, understates outstanding.
    Either way the number was wrong; now it is consumed.
    """
    purchase = schema.parse_purchase(
        {
            "ID": "po-1",
            "Status": "ORDERING",
            "SupplierID": SUPPLIER,
            "Location": "WA",
            "Order": {
                "Status": "AUTHORISED",
                "Lines": [
                    {"ProductID": "p1", "SKU": "A", "Quantity": 10},
                    {"ProductID": "p1", "SKU": "A", "Quantity": 5},
                ],
            },
            "StockReceived": {
                "Lines": [{"ProductID": "p1", "Quantity": 8}],
            },
        }
    )

    outstanding = sum(l.outstanding_quantity for l in purchase.lines)
    assert outstanding == 7, f"15 ordered, 8 received -> 7 still coming, got {outstanding}"


# ---------------------------------------------------------------------------
# Multi-component packs
# ---------------------------------------------------------------------------


def test_every_component_of_a_pack_counts_as_inbound():
    """A coffee pack contains the bag, the beans and the label.

    An open order for 2 packs used to credit inbound to whichever component
    came first in the index and zero to the rest — understated inbound for
    every other component, which means re-ordering goods already coming.
    """
    bom = BomIndex.build(
        [
            BillOfMaterials(
                parent_product_id="pack-1",
                parent_sku="PACK",
                components=(
                    BomComponent(component_product_id="bag", quantity=1),
                    BomComponent(component_product_id="beans", quantity=24),
                ),
            )
        ]
    )
    purchase = schema.parse_purchase(
        {
            "ID": "po-1",
            "Status": "ORDERING",
            "SupplierID": SUPPLIER,
            "Location": "WA",
            "Order": {
                "Status": "AUTHORISED",
                "Lines": [{"ProductID": "pack-1", "Quantity": 2}],
            },
        }
    )

    inbound = reconstruct([purchase], bom)

    assert inbound.get("bag", "WA") == 2.0
    assert inbound.get("beans", "WA") == 48.0


def test_a_conflicted_component_still_counts_as_inbound():
    """A conflict decides which pack to ORDER, not what a pack CONTAINS."""
    boms = [
        BillOfMaterials(
            parent_product_id=f"pack-{n}",
            components=(BomComponent(component_product_id="shared", quantity=n),),
        )
        for n in (1, 2)
    ]
    bom = BomIndex.build(boms)
    assert bom.conflict_for("shared") is not None

    assert bom.components_in_base("pack-2", 3) == [("shared", 6)]


# ---------------------------------------------------------------------------
# Write retries
# ---------------------------------------------------------------------------


def _client(handler, **api):
    return Cin7Client(
        Credentials(account_id="a", app_key="k"),
        ApiConfig(daily_call_budget=200, **api),
        read_only=False,
        transport=httpx.MockTransport(handler),
        rate_limiter=NullRateLimiter(),
    )


def test_a_write_is_never_retried_on_a_5xx(monkeypatch):
    """A 5xx on a POST is ambiguous — Cin7 may have applied it.

    Retrying POST /purchase on that ambiguity is how duplicate purchase
    orders get created. One attempt, then the error surfaces.
    """
    monkeypatch.setattr("cin7_reorder.client.time.sleep", lambda *_: None)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(500, text="boom")

    with pytest.raises(Cin7Error, match="Not retried"):
        _client(handler).post("purchase", {"SupplierID": "s1"})
    assert calls["n"] == 1


def test_a_write_is_never_retried_on_a_transport_error(monkeypatch):
    monkeypatch.setattr("cin7_reorder.client.time.sleep", lambda *_: None)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ConnectError("reset")

    with pytest.raises(Cin7Error, match="Not retried"):
        _client(handler).post("purchase", {"SupplierID": "s1"})
    assert calls["n"] == 1


def test_a_write_is_still_retried_on_429(monkeypatch):
    """429 guarantees the request was NOT processed, so retrying is safe."""
    monkeypatch.setattr("cin7_reorder.client.time.sleep", lambda *_: None)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "1"})
        return httpx.Response(200, json={"ID": "po-1"})

    assert _client(handler).post("purchase", {})["ID"] == "po-1"
    assert calls["n"] == 2


def test_reads_still_retry_transport_errors(monkeypatch):
    """try_get reads every purchase in a run; a blip must not read as an
    unreadable purchase (which aborts an apply)."""
    monkeypatch.setattr("cin7_reorder.client.time.sleep", lambda *_: None)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("blip")
        return httpx.Response(200, json={"ID": "po-1"})

    result = _client(handler).try_get("purchase", ID="po-1")
    assert result.ok and result.payload["ID"] == "po-1"
    assert calls["n"] == 2


def test_retry_after_is_capped():
    """An unattended run sleeping for hours on one header is worse than
    retrying a little early."""
    response = httpx.Response(429, headers={"Retry-After": "7200"})
    assert Cin7Client._retry_after_seconds(response, 0) == 120.0


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def test_an_explicit_missing_config_path_is_an_error(tmp_path):
    """Silently running on defaults would drop the supplier pin and the
    location excludes — the two settings keeping this from ordering for the
    whole account."""
    with pytest.raises(ConfigError, match="does not exist"):
        Config.load(tmp_path / "nope.yaml")


# ---------------------------------------------------------------------------
# Pipeline end to end: update, repair, caps, staleness, unreadable purchases
# ---------------------------------------------------------------------------


def build(*, draft_detail=None, on_hand=0.0, purchase_detail_override=None):
    """A Cin7 with one product below its minimum, recording every write."""
    sent: list[tuple[str, str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        tail = request.url.path.split("/ExternalApi/v2/", 1)[-1]
        page = int(request.url.params.get("page", 1))

        def page1(key, rows):
            return httpx.Response(200, json={key: rows if page == 1 else []})

        if request.method in ("POST", "PUT"):
            sent.append((request.method, tail, json.loads(request.content or b"{}")))
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
                [{"ProductID": "p1", "Location": "WA", "OnHand": on_hand}],
            )
        if tail == "purchaseList":
            rows = []
            if draft_detail is not None:
                rows.append(
                    {
                        "ID": draft_detail["ID"],
                        "OrderStatus": "DRAFT",
                        "SupplierID": SUPPLIER,
                        "CombinedReceivingStatus": "NOT RECEIVED",
                    }
                )
            return page1("PurchaseList", rows)
        if tail == "purchase":
            if purchase_detail_override is not None:
                return httpx.Response(200, json=purchase_detail_override)
            return httpx.Response(200, json=draft_detail or {"ID": "x"})
        return httpx.Response(200, text="<html>Object moved</html>")

    return handler, sent


def run(tmp_path, handler, *, dry_run=False, safety=None):
    client = Cin7Client(
        Credentials(account_id="a", app_key="k"),
        ApiConfig(daily_call_budget=200),
        read_only=dry_run,
        transport=httpx.MockTransport(handler),
        rate_limiter=NullRateLimiter(),
    )
    return Pipeline(
        client=client,
        config=Config(
            suppliers=SupplierConfig(attribute_field="AdditionalAttribute1"),
            safety=safety or SafetyConfig(),
        ),
        state_path=tmp_path / "state.json",
        dry_run=dry_run,
    ).run()


def _our_draft(lines, fingerprint=None):
    marker = schema.build_marker("AUTO-REORDER-2026W35-WA-x", fingerprint)
    return {
        "ID": DRAFT_ID,
        "Status": "ORDERING",
        "SupplierID": SUPPLIER,
        "Location": "WA",
        "Note": marker,
        "Order": {"Status": "DRAFT", "Memo": marker, "Lines": lines},
    }


def test_end_to_end_update_reuses_the_standing_draft(tmp_path):
    """The gap the review named: no test drove the whole pipeline through
    recognising its own draft and updating it without a second header."""
    detail = _our_draft([{"ProductID": "p1", "SKU": "CUP", "Quantity": 48}])
    stored = fingerprint_purchase(schema.parse_purchase(detail))
    detail = _our_draft(
        [{"ProductID": "p1", "SKU": "CUP", "Quantity": 48}], fingerprint=stored
    )

    handler, sent = build(draft_detail=detail)
    result = run(tmp_path, handler)

    assert result.aborted is None
    assert result.drafts_updated and not result.drafts_created
    headers = [x for x in sent if x[1] == "purchase"]
    assert not headers, "an update must never create a second header"
    ((verb, _, body),) = [x for x in sent if x[1] == "purchase/order"]
    assert verb == "POST" and body["TaskID"] == DRAFT_ID
    assert body["Status"] == "DRAFT"


def test_end_to_end_empty_draft_is_repaired(tmp_path):
    detail = _our_draft([], fingerprint="deadbeef")
    handler, sent = build(draft_detail=detail)
    result = run(tmp_path, handler)

    assert result.aborted is None
    assert result.drafts_updated, result.warnings
    assert not [x for x in sent if x[1] == "purchase"]
    ((_, _, body),) = [x for x in sent if x[1] == "purchase/order"]
    assert body["TaskID"] == DRAFT_ID and body["Lines"]


def test_capped_lines_are_reported_not_ordered(tmp_path):
    """config.yaml promises 'a line that trips a cap is reported, not
    ordered'; until this test, the full quantity went onto the draft."""
    handler, sent = build()
    result = run(
        tmp_path, handler, safety=SafetyConfig(max_line_quantity=5)
    )

    assert result.lines, "the line should still be computed and reported"
    assert not sent, "nothing may be written when every line is capped"
    assert any("safety cap" in w and "NOT put on the draft" in w for w in result.warnings)


def test_a_stale_standing_draft_is_reported(tmp_path):
    """Demand recovered, draft remains: without this, last month's
    quantities sit in Cin7 waiting for someone to authorise them."""
    detail = _our_draft([{"ProductID": "p1", "SKU": "CUP", "Quantity": 48}])
    handler, sent = build(draft_detail=detail, on_hand=500)
    result = run(tmp_path, handler)

    assert not sent
    assert any("stale" in entry for entry in result.drafts_left_alone)


def test_apply_refuses_when_a_purchase_cannot_be_parsed(tmp_path):
    """Fetched-but-unparseable is the same hole as unfetchable: unknown
    inbound. Apply promised to stop on that and silently did not."""
    handler, _ = build(
        draft_detail={"ID": "opaque-1"},
        purchase_detail_override={"Order": {"Lines": []}},  # no ID anywhere
    )
    result = run(tmp_path, handler)

    assert result.aborted is not None
    assert "Refusing" in result.aborted


def test_plan_warns_when_a_purchase_cannot_be_parsed(tmp_path):
    handler, _ = build(
        draft_detail={"ID": "opaque-1"},
        purchase_detail_override={"Order": {"Lines": []}},
    )
    result = run(tmp_path, handler, dry_run=True)

    assert result.aborted is None
    assert any("INBOUND STOCK MAY BE UNDERSTATED" in w for w in result.warnings)


def test_a_reorder_point_with_no_supplier_is_reported(tmp_path):
    """A minimum with no supplier can never be auto-ordered, and nothing
    else would ever say so."""
    def handler(request: httpx.Request) -> httpx.Response:
        tail = request.url.path.split("/ExternalApi/v2/", 1)[-1]
        page = int(request.url.params.get("page", 1))

        def page1(key, rows):
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
                        "SKU": "ORPHAN",
                        "MinimumBeforeReorder": 100,
                        "ReorderQuantity": 48,
                        "Suppliers": [],
                    }
                ],
            )
        if tail.endswith("productAvailability"):
            if not tail.startswith("ref/"):
                return httpx.Response(200, text="<html>x</html>")
            return page1(
                "ProductAvailabilityList",
                [{"ProductID": "p1", "Location": "WA", "OnHand": 0}],
            )
        if tail == "purchaseList":
            return page1("PurchaseList", [])
        return httpx.Response(200, text="<html>x</html>")

    result = run(tmp_path, handler, dry_run=True)

    skip = next(s for s in result.skipped if s.reason is SkipReason.NO_SUPPLIER)
    assert skip.base_sku == "ORPHAN"


def _catalogue_handler(products):
    def handler(request: httpx.Request) -> httpx.Response:
        tail = request.url.path.split("/ExternalApi/v2/", 1)[-1]
        page = int(request.url.params.get("page", 1))

        def page1(key, rows):
            return httpx.Response(200, json={key: rows if page == 1 else []})

        if tail == "supplier":
            return page1(
                "SupplierList",
                [{"ID": SUPPLIER, "Name": "BioPak", "AdditionalAttribute1": "Yes"}],
            )
        if tail == "product":
            return page1("Products", products)
        if tail.endswith("productAvailability"):
            if not tail.startswith("ref/"):
                return httpx.Response(200, text="<html>x</html>")
            # At least one row: an envelope of empty lists deliberately does
            # not count as a resolved endpoint.
            return page1(
                "ProductAvailabilityList",
                [{"ProductID": "other", "Location": "WA", "OnHand": 1}],
            )
        if tail == "purchaseList":
            return page1("PurchaseList", [])
        return httpx.Response(200, text="<html>x</html>")

    return handler


def test_junk_without_a_real_minimum_is_not_reported_as_supplierless(tmp_path):
    """The wall of noise a live run produced.

    Cin7 defaults MinimumBeforeReorder to 0 on every product, and
    parse_reorder_parameters returns an entry either way — so gating on mere
    presence reported the entire junk half of the catalogue (FREIGHT, MISC,
    spare parts) as "has a reorder point but no supplier", burying the
    handful of products the section exists to surface.
    """
    result = run(
        tmp_path,
        _catalogue_handler(
            [
                {"ID": "junk", "SKU": "FREIGHT", "Name": "Freight",
                 "MinimumBeforeReorder": 0, "Suppliers": []},
                {"ID": "real", "SKU": "NAPKINS", "Name": "Napkins Plain",
                 "MinimumBeforeReorder": 600, "ReorderQuantity": 600,
                 "Suppliers": []},
            ]
        ),
        dry_run=True,
    )

    supplierless = [
        s for s in result.skipped if s.reason is SkipReason.NO_SUPPLIER
    ]
    assert [s.base_sku for s in supplierless] == ["NAPKINS"]
    # And the detail carries the NAME, because a bare SKU cannot be matched
    # against what Cin7's own screens show.
    assert "Napkins Plain" in supplierless[0].detail
    assert "Suppliers tab" in supplierless[0].detail


def test_the_skip_table_is_capped_in_markdown_not_json(tmp_path):
    from cin7_reorder.models import RunResult, SkippedProduct
    from cin7_reorder.report import render_json, render_markdown

    result = RunResult(
        skipped=[
            SkippedProduct(
                base_product_id=str(n), base_sku=f"SKU{n:03}", location="WA",
                reason=SkipReason.NO_SUPPLIER, detail="d",
            )
            for n in range(120)
        ]
    )

    markdown = render_markdown(result, dry_run=True)
    assert "SKU039" in markdown and "SKU041" not in markdown
    assert "80 more" in markdown

    payload = json.loads(render_json(result, dry_run=True))
    assert len(payload["skipped"]) == 120


def test_a_low_product_of_an_unpinned_supplier_is_named_not_silent(tmp_path):
    """The last silent skip, and the question a person actually asks.

    "Why is the chai missing from the order?" had no answer in the report
    when the product's default supplier was not the pinned one — a product
    can list several suppliers in Cin7 and this tool follows the default, so
    the wrong default made products vanish without a trace. Products that
    are NOT below their minimum stay silent, or the section would carry the
    whole catalogue's supplier assignments.
    """
    products = [
        {
            "ID": "chai", "SKU": "CHAI1KG", "Name": "Chai Bond St Natural - 1kg",
            "MinimumBeforeReorder": 60, "ReorderQuantity": 60,
            "Suppliers": [{"SupplierID": "sup-other", "Name": "Old Wholesaler"}],
        },
        {
            "ID": "fine", "SKU": "STOCKED", "Name": "Plenty on hand",
            "MinimumBeforeReorder": 1, "ReorderQuantity": 1,
            "Suppliers": [{"SupplierID": "sup-other", "Name": "Old Wholesaler"}],
        },
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        tail = request.url.path.split("/ExternalApi/v2/", 1)[-1]
        page = int(request.url.params.get("page", 1))

        def page1(key, rows):
            return httpx.Response(200, json={key: rows if page == 1 else []})

        if tail == "supplier":
            return page1(
                "SupplierList",
                [
                    {"ID": SUPPLIER, "Name": "BioPak", "AdditionalAttribute1": "Yes"},
                    {"ID": "sup-other", "Name": "Old Wholesaler"},
                ],
            )
        if tail == "product":
            return page1("Products", products)
        if tail.endswith("productAvailability"):
            if not tail.startswith("ref/"):
                return httpx.Response(200, text="<html>x</html>")
            return page1(
                "ProductAvailabilityList",
                [
                    {"ProductID": "chai", "Location": "WA", "OnHand": 50},
                    {"ProductID": "fine", "Location": "WA", "OnHand": 400},
                ],
            )
        if tail == "purchaseList":
            return page1("PurchaseList", [])
        return httpx.Response(200, text="<html>x</html>")

    client = Cin7Client(
        Credentials(account_id="a", app_key="k"),
        ApiConfig(daily_call_budget=200),
        read_only=True,
        transport=httpx.MockTransport(handler),
        rate_limiter=NullRateLimiter(),
    )
    result = Pipeline(
        client=client,
        config=Config(
            suppliers=SupplierConfig(
                attribute_field="AdditionalAttribute1", pin=("BioPak",)
            )
        ),
        state_path=tmp_path / "state.json",
        dry_run=True,
    ).run()

    rows = [
        s for s in result.skipped
        if s.reason is SkipReason.SUPPLIER_NOT_OPTED_IN
    ]
    assert [s.base_sku for s in rows] == ["CHAI1KG"]
    assert "Chai Bond St" in rows[0].detail
    assert "Old Wholesaler" in rows[0].detail
    assert "default" in rows[0].detail.lower()
