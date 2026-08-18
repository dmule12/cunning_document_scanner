"""One reorder run, start to finish.

Sequence:

1. Suppliers, and which of them are opted in.
2. Products (the spine — never availability, see below).
3. Bills of materials, inverted into the base -> pack index.
4. Availability, left-joined onto the product list.
5. Open purchase orders, from which inbound stock is reconstructed.
6. Evaluate each product/location, producing lines or skips.
7. Write drafts — only when applying.

The ordering of 2 and 4 matters. ``productAvailability`` omits rows where
on-hand, available and on-order are all zero, so a completely stocked-out
product disappears from it — and that is exactly the product most urgently
needing an order. Driving from the product list and treating a missing
availability row as zeros is what keeps those visible.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from . import schema
from .bom import BomIndex
from .client import CallBudgetExceeded, Cin7Client, Cin7Error
from .config import Config
from .drafts import (
    DraftDecision,
    FingerprintStore,
    decide,
    fingerprint,
    is_ours,
    run_reference,
)
from .inbound import reconstruct
from .models import (
    Availability,
    BillOfMaterials,
    Product,
    PurchaseOrder,
    PurchaseStatus,
    ReorderParameters,
    RunResult,
    SkipReason,
    SkippedProduct,
    SuggestedLine,
)
from .reorder import Demand, availability_for, evaluate
from .reorderpoints import resolve as resolve_reorder_point

log = logging.getLogger(__name__)


def _matches_pin(supplier_id: str, name: str, pin: set[str]) -> bool:
    """Whether a supplier is named by the rollout pin.

    Accepts an exact ID or a case-insensitive fragment of the supplier's
    name. Substring matching is deliberate here and nowhere else: the pin
    exists to make "just this one supplier" easy to express, and a GUID is
    not something anyone types correctly from memory.
    """
    lowered_name = name.strip().lower()
    for entry in pin:
        candidate = entry.strip()
        if not candidate:
            continue
        if candidate == supplier_id:
            return True
        if candidate.lower() in lowered_name:
            return True
    return False


@dataclass
class Pipeline:
    client: Cin7Client
    config: Config
    state_path: Path
    dry_run: bool = True

    def run(self) -> RunResult:
        result = RunResult()
        try:
            self._run(result)
        except CallBudgetExceeded as exc:
            result.aborted = str(exc)
        except Cin7Error as exc:
            result.aborted = f"API error: {exc}"
        finally:
            result.api_calls = self.client.call_count
        return result

    # -- stages ------------------------------------------------------------

    def _run(self, result: RunResult) -> None:
        suppliers = self._load_suppliers(result)
        if not suppliers:
            result.warnings.append(
                "No suppliers are opted in. Set the "
                f"'{self.config.suppliers.attribute_name}' attribute in Cin7, "
                "or list supplier IDs under `suppliers.pin` in config.yaml."
            )
            return

        products, reorder_params, boms = self._load_products()
        bom = self._build_bom_index(boms, result)
        availability = self._load_availability(result)
        purchases = self._load_purchases(result, suppliers)

        our_drafts = {
            p.id: p
            for p in purchases
            if p.status is PurchaseStatus.DRAFT and is_ours(p)
        }

        inbound = reconstruct(
            purchases, bom, exclude_purchase_ids=set(our_drafts)
        )

        for purchase_id in inbound.unknown_status_orders:
            result.warnings.append(
                f"Purchase {purchase_id} has an unrecognised status and was "
                "excluded from inbound stock. If it represents stock on its "
                "way, this run may over-order."
            )

        locations = sorted(
            {a.location for a in availability.values() if a.location}
        ) or [""]

        self._evaluate_all(
            result=result,
            products=products,
            reorder_params=reorder_params,
            bom=bom,
            availability=availability,
            inbound=inbound,
            suppliers=suppliers,
            locations=locations,
        )

        self._enforce_run_caps(result)

        if not self.dry_run and not result.aborted:
            self._write_drafts(result, our_drafts)

    # -- loading -----------------------------------------------------------

    def _load_suppliers(self, result: RunResult) -> dict[str, str]:
        """Supplier id -> name, for suppliers this run may order from."""
        opted_in: dict[str, str] = {}
        pin = set(self.config.suppliers.pin)

        for record in self.client.paginate(schema.ENDPOINT_SUPPLIER):
            supplier_id = schema.parse_supplier_id(record)
            if not supplier_id:
                continue
            name = schema.parse_supplier_name(record) or supplier_id

            if pin:
                # The rollout pin overrides the attribute entirely, so the
                # first live runs cannot touch anyone unexpected.
                #
                # Matched on ID or on a case-insensitive name fragment,
                # because supplier IDs are GUIDs nobody can type from memory
                # and the whole point of the pin is being easy to set safely.
                if _matches_pin(supplier_id, name, pin):
                    opted_in[supplier_id] = name
                    result.suppliers_considered.append(name)
                else:
                    result.suppliers_skipped.append(name)
                continue

            value = schema.extract_supplier_attribute(
                record, self.config.suppliers.lookup_key
            )
            if self.config.suppliers.is_opted_in(value):
                opted_in[supplier_id] = name
                result.suppliers_considered.append(name)
            else:
                result.suppliers_skipped.append(name)

        return opted_in

    def _load_products(
        self,
    ) -> tuple[
        dict[str, Product],
        dict[str, list[ReorderParameters]],
        list[BillOfMaterials],
    ]:
        """One pass over the catalogue, yielding everything it carries.

        Bills of materials live on the product record rather than at their
        own endpoint, so products, reorder points and BOMs all come from the
        same paged read. Confirmed against a live account, where every
        candidate /BillOfMaterials path returned Cin7's not-found redirect.
        """
        products: dict[str, Product] = {}
        params: dict[str, list[ReorderParameters]] = {}
        boms: list[BillOfMaterials] = []

        # The include flags matter enormously: without them Cin7 returns every
        # nested collection as an empty list, which reads as "no product has a
        # bill of materials" rather than as an error. Silent and expensive.
        for record in self.client.paginate(
            schema.ENDPOINT_PRODUCT, **schema.PRODUCT_INCLUDE_FLAGS
        ):
            product = schema.parse_product(record)
            if product is None:
                continue
            products[product.id] = product

            parsed = schema.parse_reorder_parameters(record)
            if parsed:
                params[product.id] = parsed

            if schema.product_has_bom(record):
                bom = schema.parse_bill_of_materials(record)
                if bom is not None and bom.components:
                    boms.append(bom)

        return products, params, boms

    def _build_bom_index(
        self, boms: list[BillOfMaterials], result: RunResult
    ) -> BomIndex:
        index = BomIndex.build(boms)

        if not boms:
            result.warnings.append(
                "No product carries a bill of materials, so no pack SKU can be "
                "resolved and everything falls back to base units. Either no "
                "product has one configured in Cin7, or the component key in "
                "schema.py is wrong — run `dump --with-bom` to tell which."
            )
        elif not index.link_count:
            result.warnings.append(
                f"{len(boms)} product(s) carry a bill of materials but none "
                "yielded a usable component link. Every product will be "
                "ordered as base units until this is fixed."
            )

        for conflict in index.conflicts:
            result.warnings.append(
                f"Product {conflict.base_product_id} is a component of "
                f"{len(conflict.pack_product_ids)} packs; it will be skipped "
                "until the BOM data is corrected."
            )

        return index

    def _load_availability(
        self, result: RunResult
    ) -> dict[tuple[str, str], Availability]:
        endpoint = self.client.resolve_endpoint(
            schema.AVAILABILITY_ENDPOINT_CANDIDATES, page=1, limit=1
        )
        if endpoint is None:
            # Not recoverable: without stock levels every product looks like
            # it has nothing on hand, and the run would order the entire
            # catalogue. Far better to stop than to produce that confidently.
            raise Cin7Error(
                "No working product-availability endpoint. Tried: "
                + ", ".join(schema.AVAILABILITY_ENDPOINT_CANDIDATES)
                + ". Without stock levels every product would read as zero on "
                "hand and the run would order everything, so it stops here."
            )

        found: dict[tuple[str, str], Availability] = {}
        for record in self.client.paginate(endpoint):
            parsed = schema.parse_availability(record)
            if parsed is None:
                continue
            if not self.config.includes_location(parsed.location):
                continue
            found[(parsed.product_id, parsed.location)] = parsed
        return found

    def _load_purchases(
        self, result: RunResult, suppliers: dict[str, str]
    ) -> list[PurchaseOrder]:
        """Open purchases from the suppliers in scope, in full.

        This is the one place the tool fetches per record rather than paging
        in bulk, and it is by far the most expensive stage. Every detail costs
        a request — two for Advanced purchases, which answer a 400 naming the
        right endpoint before they answer anything useful. An account with
        several hundred open orders will exhaust the daily quota here and get
        itself 429'd if the list is not filtered first.

        So the list rows are filtered twice before anything is fetched: by
        status, because a closed order has nothing on its way, and by
        supplier, because a run ordering from one supplier does not need to
        read every other supplier's paperwork.
        """
        supplier_ids = set(suppliers)
        supplier_names = {
            name.strip().lower() for name in suppliers.values() if name
        }

        wanted: list[schema.PurchaseListEntry] = []
        other_suppliers = 0
        unattributed = 0

        for row in self.client.paginate(schema.ENDPOINT_PURCHASE_LIST):
            entry = schema.parse_purchase_list_entry(row)
            if not entry.id:
                continue
            if entry.status in schema.CLOSED_STATUSES:
                continue
            if not entry.is_for_supplier(supplier_ids, supplier_names):
                other_suppliers += 1
                continue
            if not entry.names_a_supplier:
                unattributed += 1
            wanted.append(entry)

        # Read off the client, not off self.config.api. They are the same
        # ApiConfig in production, but the client is the object that actually
        # spends the calls and owns the daily budget, so keeping both limits
        # on one object means they cannot drift apart.
        limit = self.client.config.max_purchase_details
        over_budget = wanted[limit:]
        purchases: list[PurchaseOrder] = []

        for entry in wanted[:limit]:
            detail = self._fetch_purchase(entry.id, result)
            if detail is None:
                continue
            parsed = schema.parse_purchase(detail)
            if parsed is not None:
                purchases.append(parsed)

        self._report_purchase_coverage(
            result,
            read=len(wanted) - len(over_budget),
            other_suppliers=other_suppliers,
            unattributed=unattributed,
            over_budget=len(over_budget),
            limit=limit,
        )

        return purchases

    def _report_purchase_coverage(
        self,
        result: RunResult,
        *,
        read: int,
        other_suppliers: int,
        unattributed: int,
        over_budget: int,
        limit: int,
    ) -> None:
        """Say plainly which open orders were not read, and why.

        Each of these is a way inbound stock can be understated, and an
        understated inbound figure is what makes this tool re-order goods
        already in transit. None of them is visible in the numbers themselves,
        so they have to be stated.
        """
        result.notes.append(
            f"Read {read} open purchase order(s) for inbound stock."
        )

        if other_suppliers:
            result.notes.append(
                f"{other_suppliers} open purchase order(s) belong to suppliers "
                "this run is not ordering from and were not read. If one of "
                "them happens to carry a product listed below, that product's "
                "inbound figure is understated."
            )

        if unattributed:
            result.warnings.append(
                f"{unattributed} open purchase order(s) carry no supplier on "
                "the list endpoint, so each had to be read in full to find "
                "out. If that count is large, the supplier field name in "
                "`parse_purchase_list_entry` in schema.py is wrong and this "
                "run is much more expensive than it needs to be."
            )

        if not over_budget:
            return

        message = (
            f"{over_budget} open purchase order(s) were not read: the run hit "
            f"its limit of {limit} purchase reads (`api.max_purchase_details` "
            "in config.yaml). Whatever they have on order is missing from the "
            "inbound figures, so those products may look shorter than they are."
        )

        if self.dry_run:
            result.warnings.append("INBOUND STOCK MAY BE UNDERSTATED. " + message)
            return

        # Same reasoning as an unreadable purchase: creating orders from a
        # partial view of what is already coming costs real money.
        raise Cin7Error(
            message
            + " Refusing to create purchase orders from an incomplete view of "
            "what is already on its way."
        )

    def _fetch_purchase(
        self, purchase_id: str, result: RunResult
    ) -> Optional[dict]:
        """One purchase, from whichever endpoint serves its type.

        Cin7 has several kinds of purchase order and ``/purchase`` refuses
        Advanced and Service ones with a 400 naming the endpoint to use
        instead. Neither endpoint serves everything, so both are tried.

        When a purchase cannot be read at all, what happens depends on what
        the run is for. Its contents are unknown, so inbound stock is
        understated by an unknown amount — and understated inbound means
        re-ordering goods already in transit, the exact failure this tool
        exists to prevent.

        So ``apply`` stops: writing a purchase order off incomplete inbound
        data costs real money. ``plan`` continues with a prominent warning,
        because a report you can read and judge is more useful than no report
        at all, and it writes nothing either way.
        """
        response = self.client.try_get(schema.ENDPOINT_PURCHASE, ID=purchase_id)
        if response.ok and isinstance(response.payload, dict):
            return response.payload

        reason = response.detail or "unknown error"

        if schema.DEPRECATED_ENDPOINT_MARKER in reason:
            endpoint = self.client.resolve_endpoint(
                schema.ADVANCED_PURCHASE_CANDIDATES, ID=purchase_id
            )
            if endpoint is not None:
                advanced = self.client.try_get(endpoint, ID=purchase_id)
                if advanced.ok and isinstance(advanced.payload, dict):
                    return advanced.payload
            reason = (
                "it is an Advanced or Service purchase, and none of "
                f"{', '.join(schema.ADVANCED_PURCHASE_CANDIDATES)} served it"
            )

        message = (
            f"Could not read open purchase {purchase_id}: {reason}. Anything it "
            "has on order is missing from the inbound figures below, so those "
            "products may look shorter than they are."
        )

        if self.dry_run:
            result.warnings.append(
                "INBOUND STOCK MAY BE UNDERSTATED. " + message
            )
            return None

        raise Cin7Error(
            message
            + " Refusing to create purchase orders from an incomplete view of "
            "what is already on its way."
        )

    # -- evaluation --------------------------------------------------------

    def _evaluate_all(
        self,
        *,
        result: RunResult,
        products: dict[str, Product],
        reorder_params: dict[str, list[ReorderParameters]],
        bom: BomIndex,
        availability: dict[tuple[str, str], Availability],
        inbound,
        suppliers: dict[str, str],
        locations: list[str],
    ) -> None:
        for product in products.values():
            # A pack SKU is not stock in its own right — it disassembles on
            # receipt. Reordering against its own level would double-count.
            if bom.is_pack(product.id):
                continue

            if not product.supplier_id:
                continue

            if product.supplier_id not in suppliers:
                continue

            for location in locations:
                if not self.config.includes_location(location):
                    continue

                point = resolve_reorder_point(
                    reorder_params.get(product.id, []),
                    supplier_id=product.supplier_id,
                    location=location,
                )
                if point is None:
                    # No MinimumBeforeReorder set means nobody has decided
                    # this product should be reordered automatically. Cin7's
                    # own low-stock reorder would ignore it too.
                    result.skipped.append(
                        SkippedProduct(
                            base_product_id=product.id,
                            base_sku=product.sku,
                            location=location,
                            reason=SkipReason.NO_REORDER_PARAMETERS,
                            detail=(
                                "No MinimumBeforeReorder set at product or "
                                "location level."
                            ),
                        )
                    )
                    continue

                stock = availability_for(availability, product.id, location)

                line, skip = evaluate(
                    Demand(
                        product=product,
                        location=location,
                        reorder_point=point.minimum,
                        on_hand=stock.on_hand,
                        allocated=stock.allocated,
                        inbound_base=inbound.get(product.id, location),
                        inbound_sources=inbound.sources_for(product.id, location),
                        reorder_quantity=point.reorder_quantity,
                    ),
                    bom,
                    self.config,
                )

                if line is not None:
                    result.lines.append(line)
                if skip is not None:
                    result.skipped.append(skip)

    def _enforce_run_caps(self, result: RunResult) -> None:
        cap = self.config.safety.max_total_lines
        if cap is not None and len(result.lines) > cap:
            result.aborted = (
                f"Run would create {len(result.lines)} order lines, above the "
                f"configured cap of {cap}. A jump this size is usually a data "
                "problem rather than a genuine restock. Nothing was written. "
                "Raise `safety.max_total_lines` if this is expected."
            )

    # -- writing -----------------------------------------------------------

    def _write_drafts(
        self, result: RunResult, our_drafts: dict[str, PurchaseOrder]
    ) -> None:
        store = FingerprintStore(self.state_path)

        grouped: dict[tuple[str, str], list[SuggestedLine]] = {}
        for line in result.lines:
            grouped.setdefault((line.supplier_id, line.location), []).append(line)

        for (supplier_id, location), lines in sorted(grouped.items()):
            reference = run_reference(supplier_id, location)

            existing = next(
                (
                    draft
                    for draft in our_drafts.values()
                    if draft.supplier_id == supplier_id
                    and draft.location == location
                ),
                None,
            )

            plan = decide(
                existing=existing,
                reference=reference,
                stored_fingerprint=(
                    store.get(existing.id) if existing is not None else None
                ),
            )

            if plan.decision == DraftDecision.LEAVE_ALONE:
                result.drafts_left_alone.append(
                    f"{plan.reference or plan.purchase_id} — {plan.reason}"
                )
                continue

            payload = schema.build_purchase_payload(
                supplier_id=supplier_id,
                location=location,
                reference=reference,
                lines=[
                    schema.build_purchase_line(
                        product_id=line.order_product_id,
                        sku=line.order_sku,
                        quantity=line.quantity,
                    )
                    for line in lines
                ],
            )

            try:
                if plan.decision == DraftDecision.UPDATE and plan.purchase_id:
                    payload["ID"] = plan.purchase_id
                    self.client.put(schema.ENDPOINT_PURCHASE, payload)
                    store.set(plan.purchase_id, fingerprint(lines))
                    result.drafts_updated.append(plan.reference)
                else:
                    response = self.client.post(schema.ENDPOINT_PURCHASE, payload)
                    new_id = schema.as_str(
                        schema.get_first(
                            response if isinstance(response, dict) else {},
                            "ID",
                            "PurchaseID",
                            "TaskID",
                        )
                    )
                    if new_id:
                        store.set(new_id, fingerprint(lines))
                    result.drafts_created.append(plan.reference)
            except Cin7Error as exc:
                result.warnings.append(
                    f"Failed to write draft {reference}: {exc}"
                )

        store.save()
