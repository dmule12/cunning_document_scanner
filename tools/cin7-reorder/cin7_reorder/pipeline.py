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

        products, reorder_params = self._load_products()
        bom = self._load_bom(result)
        availability = self._load_availability()
        purchases = self._load_purchases()

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
                if supplier_id in pin:
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
    ) -> tuple[dict[str, Product], dict[str, list[ReorderParameters]]]:
        products: dict[str, Product] = {}
        params: dict[str, list[ReorderParameters]] = {}

        for record in self.client.paginate(schema.ENDPOINT_PRODUCT):
            product = schema.parse_product(record)
            if product is None:
                continue
            products[product.id] = product
            parsed = schema.parse_reorder_parameters(record)
            if parsed:
                params[product.id] = parsed

        return products, params

    def _load_bom(self, result: RunResult) -> BomIndex:
        boms = []
        for record in self.client.paginate(
            schema.ENDPOINT_BILL_OF_MATERIALS,
            onlyProductsWithBOM="true",
        ):
            parsed = schema.parse_bill_of_materials(record)
            if parsed is not None:
                boms.append(parsed)

        index = BomIndex.build(boms)

        if not index.link_count and boms:
            result.warnings.append(
                f"{len(boms)} BOM record(s) were returned but none yielded a "
                "usable component link. The component key in schema.py is "
                "probably wrong — run `probe`. Every product will be ordered "
                "as base units until this is fixed."
            )
        elif not boms:
            result.warnings.append(
                "No bills of materials were returned. Without them, pack SKUs "
                "cannot be resolved and everything falls back to base units. "
                "Run `probe`."
            )

        return index

    def _load_availability(self) -> dict[tuple[str, str], Availability]:
        found: dict[tuple[str, str], Availability] = {}
        for record in self.client.paginate(schema.ENDPOINT_PRODUCT_AVAILABILITY):
            parsed = schema.parse_availability(record)
            if parsed is None:
                continue
            if not self.config.includes_location(parsed.location):
                continue
            found[(parsed.product_id, parsed.location)] = parsed
        return found

    def _load_purchases(self) -> list[PurchaseOrder]:
        """Open purchases, in full.

        The list endpoint is a cheap pre-filter: closed purchases never cost a
        detail call. This is the one place the tool loops per record rather
        than paging in bulk, and it is bounded by open-PO count, not SKU count.
        """
        purchases: list[PurchaseOrder] = []

        for row in self.client.paginate(schema.ENDPOINT_PURCHASE_LIST):
            purchase_id, status, _reference = schema.parse_purchase_list_entry(row)
            if not purchase_id:
                continue
            if status in schema.CLOSED_STATUSES:
                continue

            detail = self.client.get(schema.ENDPOINT_PURCHASE, ID=purchase_id)
            parsed = schema.parse_purchase(detail if isinstance(detail, dict) else {})
            if parsed is not None:
                purchases.append(parsed)

        return purchases

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
