"""Creating and updating the draft purchase orders.

Each run leaves at most one standing draft per (supplier, location), and
recalculates it rather than piling up a second one. That keeps the draft
current against today's stock, so it cannot go stale while it waits for
review.

The hazard is obvious once stated: if someone has already adjusted the draft
— changed a quantity, added a line, corrected a delivery date — a blind
overwrite destroys that work and leaves no trace. A duplicate PO is visible
and annoying; a silently discarded manual correction is invisible and worse.

So every write is fingerprinted. On the next run the draft is re-read and
compared: unchanged since we wrote it means safe to overwrite; anything else
means leave it alone and tell the reviewer.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Iterable, Optional

from .models import PurchaseOrder, SuggestedLine

#: Prefix identifying purchase orders this tool created. Used to tell our own
#: drafts apart from ones raised by hand, which we must never touch.
REFERENCE_PREFIX = "AUTO-REORDER"


def run_reference(supplier_id: str, location: str, when: Optional[date] = None) -> str:
    """A stable, human-legible reference for one supplier/location/week.

    Deterministic within a run so a crash-retry recognises its own work
    rather than creating a second PO.
    """
    when = when or date.today()
    iso_year, iso_week, _ = when.isocalendar()
    slug = location.replace(" ", "-")[:20] or "default"
    return f"{REFERENCE_PREFIX}-{iso_year}W{iso_week:02d}-{slug}-{supplier_id[:8]}"


def is_ours(purchase: PurchaseOrder) -> bool:
    return bool(purchase.reference and purchase.reference.startswith(REFERENCE_PREFIX))


def fingerprint(lines: Iterable[SuggestedLine]) -> str:
    """Stable hash of what we wrote, order-independent.

    Only the fields we actually set are included. If Cin7 decorates a draft
    with its own defaults — tax rules, account codes, dates — that must not
    read as a human edit, or every draft would be flagged and the update path
    would never run.
    """
    payload = sorted(
        (line.order_product_id, round(float(line.quantity), 6))
        for line in lines
    )
    encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def fingerprint_purchase(purchase: PurchaseOrder) -> str:
    """The same hash computed from a purchase order as it currently stands."""
    payload = sorted(
        (line.product_id, round(float(line.ordered_quantity), 6))
        for line in purchase.lines
    )
    encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class DraftDecision(str):
    CREATE = "create"
    UPDATE = "update"
    LEAVE_ALONE = "leave_alone"


@dataclass(frozen=True)
class DraftPlan:
    decision: str
    purchase_id: Optional[str]
    reference: str
    reason: str


def decide(
    *,
    existing: Optional[PurchaseOrder],
    reference: str,
    stored_fingerprint: Optional[str],
) -> DraftPlan:
    """Whether to create, update in place, or keep hands off."""
    if existing is None:
        return DraftPlan(
            decision=DraftDecision.CREATE,
            purchase_id=None,
            reference=reference,
            reason="no standing draft for this supplier and location",
        )

    if not is_ours(existing):
        return DraftPlan(
            decision=DraftDecision.LEAVE_ALONE,
            purchase_id=existing.id,
            reference=existing.reference or "",
            reason="draft was not created by this tool",
        )

    if stored_fingerprint is None:
        # Our reference, but no record of what we wrote — most likely the
        # state file was lost. Overwriting could destroy an edit we cannot
        # detect, so decline and let a human decide.
        return DraftPlan(
            decision=DraftDecision.LEAVE_ALONE,
            purchase_id=existing.id,
            reference=existing.reference or reference,
            reason=(
                "no stored fingerprint for this draft, so a human edit cannot "
                "be ruled out"
            ),
        )

    current = fingerprint_purchase(existing)
    if current != stored_fingerprint:
        return DraftPlan(
            decision=DraftDecision.LEAVE_ALONE,
            purchase_id=existing.id,
            reference=existing.reference or reference,
            reason="draft has been edited since we last wrote it",
        )

    return DraftPlan(
        decision=DraftDecision.UPDATE,
        purchase_id=existing.id,
        reference=existing.reference or reference,
        reason="draft is unchanged since we wrote it",
    )


class FingerprintStore:
    """Fingerprints persisted between runs, keyed by purchase id.

    A JSON file rather than a database: the data is tiny, and being able to
    read and hand-edit it matters more than anything a database would offer.
    In CI it is carried between runs as a workflow artifact.

    A missing or corrupt store is not fatal. It degrades to "leave every
    existing draft alone", which is the safe direction.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._data: dict[str, str] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self._data = {}
            return
        if isinstance(raw, dict):
            self._data = {str(k): str(v) for k, v in raw.items()}

    def get(self, purchase_id: str) -> Optional[str]:
        return self._data.get(purchase_id)

    def set(self, purchase_id: str, value: str) -> None:
        self._data[purchase_id] = value

    def forget(self, purchase_id: str) -> None:
        self._data.pop(purchase_id, None)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self._data, indent=2, sort_keys=True), encoding="utf-8"
        )
