"""The run report.

This is not decoration. Several behaviours in this tool are only safe
*because* they are reported:

* a product ordered as a base SKU because no pack was found is
  indistinguishable from a missing BOM without a flag;
* the inbound figure exists nowhere in Cin7's own UI, so if the script is
  ever accused of over- or under-ordering, this report is the only evidence
  available;
* a draft left alone because a human edited it needs someone to reconcile it.

Rendered as Markdown for humans and JSON for anything downstream.
"""

from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from enum import Enum
from typing import Any

from .models import LineFlag, RunResult, SkipReason

_FLAG_LABEL = {
    LineFlag.ORDERED_AS_BASE_UNIT: "base units — no pack SKU found",
    LineFlag.CAP_EXCEEDED: "EXCEEDS SAFETY CAP",
    LineFlag.MOQ_APPLIED: "raised to MOQ",
    LineFlag.BELOW_MINIMUM_AFTER_ORDER: "still below minimum after this order",
}

_SKIP_LABEL = {
    SkipReason.MULTIPLE_BOM_PARENTS: "Multiple pack parents — BOM data needs fixing",
    SkipReason.NO_REORDER_PARAMETERS: "No MinimumBeforeReorder set in Cin7",
    SkipReason.NO_REORDER_QUANTITY: "Below minimum but no reorder quantity set",
    SkipReason.NO_SUPPLIER: "No supplier on the product",
    SkipReason.SUPPLIER_NOT_OPTED_IN: "Supplier not opted in",
    SkipReason.SUFFICIENT_STOCK: "Sufficient stock",
    SkipReason.COVERED_BY_INBOUND: "Covered by stock already on its way",
}

#: Above this many, the not-automated supplier list is summarised rather than
#: printed. A report nobody scrolls to the end of is a report nobody reads.
_MAX_SUPPLIER_NAMES = 25

#: Same reasoning for the needs-attention skip table. The JSON report always
#: carries every row.
_MAX_SKIP_ROWS = 40


def render_markdown(result: RunResult, *, dry_run: bool) -> str:
    lines: list[str] = []
    mode = "PLAN (nothing written)" if dry_run else "APPLY"

    lines.append(f"# Cin7 reorder run — {mode}")
    lines.append("")

    if result.aborted:
        lines.append(f"> **RUN ABORTED:** {result.aborted}")
        lines.append("")

    # -- summary -----------------------------------------------------------
    flagged = [ln for ln in result.lines if ln.flags]
    capped = [ln for ln in result.lines if LineFlag.CAP_EXCEEDED in ln.flags]

    lines.append("## Summary")
    lines.append("")
    lines.append("| | |")
    lines.append("| --- | --- |")
    lines.append(f"| Order lines | {len(result.lines)} |")
    lines.append(f"| Lines needing a look | {len(flagged)} |")
    lines.append(f"| Lines over a safety cap | {len(capped)} |")
    lines.append(f"| Suppliers included | {len(result.suppliers_considered)} |")
    lines.append(f"| Suppliers skipped | {len(result.suppliers_skipped)} |")
    lines.append(f"| Drafts created | {len(result.drafts_created)} |")
    lines.append(f"| Drafts updated | {len(result.drafts_updated)} |")
    lines.append(f"| Drafts left alone | {len(result.drafts_left_alone)} |")
    lines.append(f"| API calls | {result.api_calls} |")
    lines.append("")

    if capped:
        lines.append(
            f"> **{len(capped)} line(s) exceeded a safety cap.** These are usually a "
            "wrong BOM ratio or a stale reorder point rather than a genuine "
            "restock. Check them before sending anything."
        )
        lines.append("")

    undercovering = [
        ln for ln in result.lines if LineFlag.BELOW_MINIMUM_AFTER_ORDER in ln.flags
    ]
    if undercovering:
        lines.append(
            f"> **{len(undercovering)} line(s) will still be below the minimum "
            "after this order.** Their ReorderQuantity in Cin7 is too small for "
            "current demand, so they will trigger again on the next run."
        )
        lines.append("")

    # -- warnings ----------------------------------------------------------
    if result.warnings:
        lines.append("## Warnings")
        lines.append("")
        for warning in result.warnings:
            lines.append(f"- {warning}")
        lines.append("")

    # -- coverage ----------------------------------------------------------
    # What the run looked at, and what it left out on purpose. Not warnings:
    # nothing here is wrong, but "inbound was computed from 12 open orders,
    # and 300 others were out of scope" is the context the numbers need.
    if result.notes:
        lines.append("## Coverage")
        lines.append("")
        for note in result.notes:
            lines.append(f"- {note}")
        lines.append("")

    # -- BOM conflicts -----------------------------------------------------
    if result.bom_conflicts:
        lines.append("## Components belonging to more than one pack")
        lines.append("")
        lines.append(
            f"These **{len(result.bom_conflicts)}** products cannot be "
            "reordered automatically, because there is no way to tell which "
            "pack to order. Guessing would mean the wrong quantity of the "
            "wrong product arriving. Pick one pack per component in Cin7, or "
            "accept that these stay manual."
        )
        lines.append("")
        lines.append("| Component | Packs it belongs to |")
        lines.append("| --- | --- |")
        for base_sku, packs in sorted(result.bom_conflicts):
            lines.append(f"| {base_sku} | {', '.join(packs)} |")
        lines.append("")

    # -- inbound working ---------------------------------------------------
    # The one number here with no equivalent in Cin7's UI. It is reconstructed
    # precisely because Cin7 will not report it against the base SKU, which
    # means nobody can check it against anything — so the working is shown.
    if result.inbound_audit:
        lines.append("## Inbound stock — the working")
        lines.append("")
        lines.append(
            "| Purchase | Status | Location | Lines | Still coming | Base units | |"
        )
        lines.append("| --- | --- | --- | ---: | ---: | ---: | --- |")
        for row in result.inbound_audit:
            lines.append(
                f"| {row.purchase_id[:8]} "
                f"| {row.status} "
                f"| {row.location} "
                f"| {row.lines} "
                f"| {row.lines_outstanding} "
                f"| {row.base_units:g} "
                f"| {row.verdict} |"
            )
        lines.append("")
        lines.append(
            "_Base units are what the order becomes after the bill of "
            "materials is applied — boxes converted to sleeves. A row reading "
            "0 with lines still coming means the pack link is missing._"
        )
        lines.append("")

    # -- order lines -------------------------------------------------------
    if result.lines:
        lines.append("## Proposed order lines")
        lines.append("")
        lines.append(
            "| Base SKU | Ordered as | Location | Min | On hand | Alloc | Inbound "
            "| Position | Short by | Reorder qty | Pack | Qty | Notes |"
        )
        lines.append(
            "| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: "
            "| ---: | ---: | --- |"
        )
        for line in sorted(
            result.lines, key=lambda ln: (ln.location, ln.base_sku)
        ):
            notes = ", ".join(_FLAG_LABEL.get(f, f.value) for f in line.flags)
            pack = f"×{line.units_per_pack:g}" if line.is_pack else "—"
            lines.append(
                f"| {line.base_sku} "
                f"| {line.order_sku} "
                f"| {line.location} "
                f"| {line.reorder_point:g} "
                f"| {line.on_hand:g} "
                f"| {line.allocated:g} "
                f"| {line.inbound_base:g} "
                f"| {line.position:g} "
                f"| {line.shortfall:g} "
                f"| {line.order_base:g} "
                f"| {pack} "
                f"| **{line.quantity:g}** "
                f"| {notes} |"
            )
        lines.append("")
        lines.append(
            "_Min is Cin7's MinimumBeforeReorder — a trigger, not a target. The "
            "quantity ordered is the stored ReorderQuantity rounded up to whole "
            "packs, which is what Cin7's own low-stock reorder does; it is not "
            "sized to close the shortfall._"
        )
        lines.append("")

    # -- inbound provenance ------------------------------------------------
    with_inbound = [ln for ln in result.lines if ln.inbound_base > 0]
    if with_inbound:
        lines.append("## Inbound stock, and where it came from")
        lines.append("")
        lines.append(
            "Cin7 does not show inbound pack quantities against the base SKU, so "
            "these figures were reconstructed from open purchase orders. This "
            "table is the only place they can be audited."
        )
        lines.append("")
        lines.append("| Base SKU | Location | Inbound (base units) | From POs |")
        lines.append("| --- | --- | ---: | --- |")
        for line in sorted(with_inbound, key=lambda ln: (ln.location, ln.base_sku)):
            sources = ", ".join(line.inbound_sources) or "—"
            lines.append(
                f"| {line.base_sku} | {line.location} "
                f"| {line.inbound_base:g} | {sources} |"
            )
        lines.append("")

    # -- drafts ------------------------------------------------------------
    if result.drafts_left_alone:
        lines.append("## Drafts left alone")
        lines.append("")
        lines.append(
            "Left as they are, each for the reason given. A draft that "
            "differs from what this tool last wrote has been edited by "
            "somebody and needs reconciling by hand; the other reasons do "
            "not mean that."
        )
        lines.append("")
        for entry in result.drafts_left_alone:
            lines.append(f"- {entry}")
        lines.append("")

    # -- duplicate orders prevented ----------------------------------------
    # The tool's entire reason for existing, and otherwise invisible: these
    # products are below their minimum on the shelf. Cin7's own low-stock
    # reorder would raise every one of them, because the boxes on their way
    # sit against the pack SKU and never show against the base SKU.
    covered = [
        s for s in result.skipped if s.reason is SkipReason.COVERED_BY_INBOUND
    ]
    if covered:
        lines.append("## Duplicate orders prevented")
        lines.append("")
        lines.append(
            f"**{len(covered)}** product/location pair(s) are below their "
            "minimum on the shelf but covered by stock already on its way. "
            "Cin7's own low-stock reorder would have re-ordered all of them."
        )
        lines.append("")
        lines.append("| SKU | Location | Working |")
        lines.append("| --- | --- | --- |")
        for skip in sorted(covered, key=lambda s: (s.location, s.base_sku)):
            lines.append(
                f"| {skip.base_sku} | {skip.location or '—'} | {skip.detail} |"
            )
        lines.append("")

    # -- skips that matter -------------------------------------------------
    actionable = [
        s
        for s in result.skipped
        if s.reason
        not in (SkipReason.SUFFICIENT_STOCK, SkipReason.COVERED_BY_INBOUND)
    ]
    if actionable:
        lines.append("## Skipped — needs attention")
        lines.append("")
        lines.append("| SKU | Location | Reason | Detail |")
        lines.append("| --- | --- | --- | --- |")
        shown = sorted(actionable, key=lambda s: (s.reason.value, s.base_sku))
        for skip in shown[:_MAX_SKIP_ROWS]:
            lines.append(
                f"| {skip.base_sku} "
                f"| {skip.location or '—'} "
                f"| {_SKIP_LABEL.get(skip.reason, skip.reason.value)} "
                f"| {skip.detail} |"
            )
        overflow = len(shown) - _MAX_SKIP_ROWS
        if overflow > 0:
            lines.append(
                f"| … | | | and {overflow} more — full list in the JSON "
                "report alongside this one |"
            )
        lines.append("")

    sufficient = len(result.skipped) - len(actionable) - len(covered)
    if sufficient:
        lines.append(
            f"_{sufficient} product/location pair(s) had sufficient stock on "
            "the shelf, without counting anything inbound._"
        )
        lines.append("")

    if result.suppliers_skipped:
        # Named in full only while the list is short enough to read. On a real
        # account this is every supplier the business has ever paid — 438 of
        # them, banks and the tax office included — and printing that wall
        # buries everything above it. The count is the useful part; the names
        # are in the JSON report for anyone who wants them.
        skipped = sorted(result.suppliers_skipped)
        lines.append("## Suppliers not automated")
        lines.append("")
        if len(skipped) <= _MAX_SUPPLIER_NAMES:
            lines.append(", ".join(skipped))
        else:
            lines.append(
                f"{len(skipped)} suppliers are not opted in. Full list in the "
                "JSON report alongside this one."
            )
        lines.append("")

    return "\n".join(lines)


def render_json(result: RunResult, *, dry_run: bool) -> str:
    payload = {
        "mode": "plan" if dry_run else "apply",
        "aborted": result.aborted,
        "api_calls": result.api_calls,
        "warnings": result.warnings,
        "notes": result.notes,
        "inbound_audit": [_encode(row) for row in result.inbound_audit],
        "bom_conflicts": [
            {"component": sku, "packs": list(packs)}
            for sku, packs in result.bom_conflicts
        ],
        "suppliers_considered": result.suppliers_considered,
        "suppliers_skipped": result.suppliers_skipped,
        "drafts_created": result.drafts_created,
        "drafts_updated": result.drafts_updated,
        "drafts_left_alone": result.drafts_left_alone,
        "lines": [_encode(line) for line in result.lines],
        "skipped": [_encode(skip) for skip in result.skipped],
    }
    return json.dumps(payload, indent=2, default=_default)


def _encode(obj: Any) -> Any:
    if is_dataclass(obj):
        return {k: _default(v) for k, v in asdict(obj).items()}
    return _default(obj)


def _default(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (list, tuple)):
        return [_default(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _default(v) for k, v in value.items()}
    return value
