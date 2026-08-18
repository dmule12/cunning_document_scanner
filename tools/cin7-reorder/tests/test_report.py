"""The report is the product.

Nothing this tool computes reaches anyone except through this file. A run can
be perfectly correct and still useless if the one number that matters is
buried under four hundred lines of supplier names — which is exactly what
happened on the first real account.
"""

from __future__ import annotations

import json

from cin7_reorder.models import RunResult, SkippedProduct, SkipReason
from cin7_reorder.report import render_json, render_markdown


def test_a_huge_supplier_list_is_summarised_not_dumped():
    """438 names, banks and the tax office included, is not information.

    Printing them pushed the order lines, the inbound working and every
    warning off the top of the screen.
    """
    result = RunResult(suppliers_skipped=[f"Supplier {n}" for n in range(438)])

    markdown = render_markdown(result, dry_run=True)

    assert "438 suppliers are not opted in" in markdown
    assert "Supplier 200" not in markdown
    assert len(markdown.splitlines()) < 60


def test_a_short_supplier_list_is_still_named():
    """On a small account the names are the useful part."""
    result = RunResult(suppliers_skipped=["Acme", "Globex"])

    markdown = render_markdown(result, dry_run=True)

    assert "Acme, Globex" in markdown


def test_the_full_supplier_list_survives_in_json():
    """Summarising the prose must not lose the data."""
    result = RunResult(suppliers_skipped=[f"Supplier {n}" for n in range(438)])

    payload = json.loads(render_json(result, dry_run=True))

    assert len(payload["suppliers_skipped"]) == 438


def test_bom_conflicts_explain_themselves_once():
    """Eleven copies of the same paragraph is not eleven times the warning."""
    result = RunResult(
        bom_conflicts=[
            ("BAGWHITE", ("BOX-A", "BOX-B")),
            ("BAGBLACK", ("BOX-C", "BOX-D")),
        ]
    )

    markdown = render_markdown(result, dry_run=True)

    assert markdown.count("wrong product arriving") == 1
    assert "BAGWHITE" in markdown and "BOX-A, BOX-B" in markdown


def test_duplicate_orders_prevented_is_its_own_section():
    """Otherwise the only evidence inbound works is a count of skips."""
    result = RunResult(
        skipped=[
            SkippedProduct(
                base_product_id="p1",
                base_sku="SLV-001",
                location="WA Warehouse",
                reason=SkipReason.COVERED_BY_INBOUND,
                detail="position 108 above minimum 100",
            ),
            SkippedProduct(
                base_product_id="p2",
                base_sku="SLV-002",
                location="WA Warehouse",
                reason=SkipReason.SUFFICIENT_STOCK,
                detail="position 900 above minimum 100",
            ),
        ]
    )

    markdown = render_markdown(result, dry_run=True)

    assert "Duplicate orders prevented" in markdown
    assert "SLV-001" in markdown
    # Ordinary sufficiency stays a count, and is not credited to inbound.
    assert "SLV-002" not in markdown
    assert "1 product/location pair(s) had sufficient stock" in markdown
