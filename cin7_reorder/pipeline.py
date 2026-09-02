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
from dataclasses import dataclass, field, replace
from datetime import date
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
    LineFlag,
    Product,
    PurchaseOrder,
    ReorderParameters,
    RunResult,
    SkipReason,
    SkippedProduct,
    SuggestedLine,
)
from .reorder import EPSILON, Demand, availability_for, evaluate
from .reorderpoints import resolve as resolve_reorder_point

log = logging.getLogger(__name__)

#: How many matching products `explain` will trace before asking for a
#: narrower fragment. A one-letter fragment matches half the catalogue.
_MAX_EXPLAIN_MATCHES = 30


def _is_purchase(payload: object, purchase_id: str) -> bool:
    """Whether a response really is the purchase that was asked for.

    Trying endpoints in turn means occasionally asking the wrong one, and a
    wrong endpoint that answers 200 with something unrelated — or with an
    empty shell — would be read as a purchase with no lines. That is the worst
    possible failure here: it silently removes stock from the inbound figure,
    and the run re-orders goods already on their way.

    A payload that carries no ID at all is accepted, since not every Cin7
    response echoes one back; a payload carrying a *different* ID is not.
    """
    if not isinstance(payload, dict):
        return False
    returned = schema.as_str(schema.get_first(payload, "ID", "PurchaseID", "TaskID"))
    if returned is None:
        return True
    return returned.strip().lower() == purchase_id.strip().lower()


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

    #: Every supplier on the account, id -> name, filled by _load_suppliers.
    #: Kept so `explain` can tell "this supplier is not automated" apart from
    #: "this product points at a supplier id that does not exist".
    _all_suppliers: dict = field(default_factory=dict, init=False, repr=False)

    #: Whether to try the Advanced/Service purchase endpoint before
    #: ``/purchase``. Set by whichever one last served a purchase. Purely an
    #: optimisation — both are still tried — but on an account where every
    #: order is an Advanced purchase it halves the cost of the whole run.
    _prefer_advanced: bool = field(default=False, init=False, repr=False)

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

        products, reorder_params, boms, _ = self._load_products()
        bom = self._build_bom_index(boms, products, result)
        availability = self._load_availability(result)
        purchases = self._load_purchases(result, suppliers)

        our_drafts = {
            p.id: p
            for p in purchases
            if p.is_draft and is_ours(p)
        }

        inbound = reconstruct(
            purchases, bom, exclude_purchase_ids=set(our_drafts)
        )

        result.inbound_audit = list(inbound.audit)

        for purchase_id in inbound.unknown_status_orders:
            result.warnings.append(
                f"Purchase {purchase_id} carries a status this tool does not "
                "recognise. It was counted as inbound anyway — the list "
                "endpoint says it is open, and leaving it out would make its "
                "contents look like stock that still needs ordering. Check it "
                "in the inbound table below and add the status to "
                "`_STATUS_MAP` in schema.py."
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
            self._all_suppliers[supplier_id] = name

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
        self, capture_fragments: tuple[str, ...] = ()
    ) -> tuple[
        dict[str, Product],
        dict[str, list[ReorderParameters]],
        list[BillOfMaterials],
        dict[str, dict],
    ]:
        """One pass over the catalogue, yielding everything it carries.

        Bills of materials live on the product record rather than at their
        own endpoint, so products, reorder points and BOMs all come from the
        same paged read. Confirmed against a live account, where every
        candidate /BillOfMaterials path returned Cin7's not-found redirect.

        ``capture_fragments`` keeps the raw API record of any product whose
        SKU or name contains one of the fragments (case-insensitively), for
        `explain` — which reports what the API actually returned, not just
        what was parsed out of it.
        """
        products: dict[str, Product] = {}
        params: dict[str, list[ReorderParameters]] = {}
        boms: list[BillOfMaterials] = []
        captured: dict[str, dict] = {}

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

            if capture_fragments:
                haystack = f"{product.sku} {product.name}".lower()
                if any(f in haystack for f in capture_fragments):
                    captured[product.id] = dict(record)

            parsed = schema.parse_reorder_parameters(record)
            if parsed:
                params[product.id] = parsed

            if schema.product_has_bom(record):
                bom = schema.parse_bill_of_materials(record)
                if bom is not None and bom.components:
                    boms.append(bom)

        return products, params, boms, captured

    def _build_bom_index(
        self,
        boms: list[BillOfMaterials],
        products: dict[str, Product],
        result: RunResult,
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

        # Collected rather than warned one by one: on a real account there are
        # a dozen, and eleven copies of the same paragraph pushed everything
        # else out of view. The explanation belongs in the report once.
        for conflict in index.conflicts:
            base = products.get(conflict.base_product_id)
            result.bom_conflicts.append(
                (
                    base.sku if base else conflict.base_product_id,
                    tuple(conflict.pack_skus or conflict.pack_product_ids),
                )
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
        seen: set[str] = set()

        for record in self.client.paginate(endpoint):
            parsed = schema.parse_availability(record)
            if parsed is None:
                continue
            if parsed.location:
                seen.add(parsed.location)
            if not self.config.includes_location(parsed.location):
                continue
            found[(parsed.product_id, parsed.location)] = parsed

        self._report_location_filters(result, seen)
        return found

    def _report_location_filters(
        self, result: RunResult, seen: set[str]
    ) -> None:
        """Say which warehouses were left out, and flag a filter that missed.

        A location filter that matches nothing is the dangerous case: the run
        looks configured and behaves as though it were not. Somebody wrote a
        warehouse name down meaning to exclude it, and a typo — or a rename in
        Cin7 — turns that into orders being raised for it anyway.
        """
        left_out = sorted(
            name for name in seen if not self.config.includes_location(name)
        )
        if left_out:
            result.notes.append(
                "Not ordering for " + ", ".join(left_out) + " (config)."
            )

        configured = (
            self.config.locations_exclude + self.config.locations_include
        )
        unmatched = [
            name
            for name in configured
            if not any(name.strip().lower() == s.strip().lower() for s in seen)
        ]
        if unmatched:
            result.warnings.append(
                "Location filter names no warehouse on this account: "
                + ", ".join(repr(n) for n in unmatched)
                + ". Cin7 reports "
                + (", ".join(sorted(seen)) or "no locations at all")
                + ". A filter that matches nothing has no effect, so this run "
                "may be ordering for a warehouse you meant to leave out."
            )

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
            if entry.is_closed:
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
            detail = self._fetch_purchase(
                entry.id, result, advanced_hint=entry.is_advanced
            )
            if detail is None:
                continue
            parsed = schema.parse_purchase(detail)
            if parsed is None:
                # Fetched but unparseable is the same hole as unfetchable:
                # whatever this order has coming is missing from inbound. The
                # old code dropped it silently — in apply mode, which promises
                # to stop rather than write from an incomplete view.
                message = (
                    f"Open purchase {entry.id} was fetched but could not be "
                    "parsed (no ID echoed back). Anything it has on order is "
                    "missing from the inbound figures."
                )
                if self.dry_run:
                    result.warnings.append(
                        "INBOUND STOCK MAY BE UNDERSTATED. " + message
                    )
                    continue
                raise Cin7Error(
                    message
                    + " Refusing to create purchase orders from an incomplete "
                    "view of what is already on its way."
                )
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

    def _read_advanced_purchase(self, purchase_id: str) -> Optional[dict]:
        """The Advanced/Service purchase endpoint, or ``None`` if it won't serve.

        Resolution is cached on the client, so this costs one call after the
        first purchase of the run.
        """
        _endpoint, response = self.client.get_resolved(
            schema.ADVANCED_PURCHASE_CANDIDATES, ID=purchase_id
        )
        if response is None or not response.ok:
            return None
        if not _is_purchase(response.payload, purchase_id):
            return None

        self._prefer_advanced = True
        return response.payload

    def _fetch_purchase(
        self,
        purchase_id: str,
        result: RunResult,
        *,
        advanced_hint: Optional[bool] = None,
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

        Which endpoint to try first is decided in order of confidence:
        ``advanced_hint`` from the list row's ``Type`` field, which says
        outright; otherwise whichever endpoint served the last purchase,
        since accounts tend to use one kind for nearly everything. Both are
        only ever a starting point — the other endpoint is still tried on a
        miss, so a wrong guess costs a call and never an order.
        """
        prefer_advanced = (
            self._prefer_advanced if advanced_hint is None else advanced_hint
        )

        if prefer_advanced:
            payload = self._read_advanced_purchase(purchase_id)
            if payload is not None:
                return payload

        response = self.client.try_get(schema.ENDPOINT_PURCHASE, ID=purchase_id)
        if response.ok and _is_purchase(response.payload, purchase_id):
            self._prefer_advanced = False
            return response.payload

        reason = response.detail or "unknown error"

        if schema.DEPRECATED_ENDPOINT_MARKER in reason:
            payload = self._read_advanced_purchase(purchase_id)
            if payload is not None:
                return payload
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

    @staticmethod
    def _order_supplier(
        product: Product, bom: BomIndex, products: dict[str, Product]
    ) -> Product:
        """The product, carrying the supplier the order would actually go to.

        A base SKU often has no supplier of its own because nobody buys the
        base unit — the pack on its bill of materials is the thing bought,
        and the supplier lives there. Confirmed live on 'Cup Bio Pak Art
        Series 16oz': the sleeve names no supplier, the box it is ordered as
        does. The purchase order is raised against the pack, so when the
        base names nobody, the pack's supplier is followed instead.

        Only a fallback: a supplier set on the base product still wins, so
        nothing that ordered correctly before changes hands.
        """
        if product.supplier_id:
            return product
        link = bom.resolve(product.id)
        if link is None:
            return product
        pack = products.get(link.pack_product_id)
        if pack is None or not pack.supplier_id:
            return product
        return replace(
            product,
            supplier_id=pack.supplier_id,
            supplier_name=pack.supplier_name,
        )

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

            product = self._order_supplier(product, bom, products)

            if not product.supplier_id:
                # Only reportable when somebody set a USABLE reorder point —
                # a minimum above zero. parse_reorder_parameters returns an
                # entry for every product because Cin7 defaults the field to
                # 0, and gating on mere presence put the entire junk half of
                # the catalogue (freight lines, marketing items, spare parts)
                # into the report, burying the handful of real products the
                # section exists to surface.
                has_real_minimum = any(
                    p.minimum_before_reorder and p.minimum_before_reorder > 0
                    for p in reorder_params.get(product.id, [])
                )
                if has_real_minimum:
                    link = bom.resolve(product.id)
                    where = "on the product's Suppliers tab in Cin7."
                    if link is not None:
                        # The pack was checked too (_order_supplier), so a
                        # remedy naming only the base would send someone to
                        # fix half of the actual problem.
                        where = (
                            "in Cin7, on the product's own Suppliers tab or "
                            f"on pack {link.display_sku} — what actually "
                            "gets ordered; either works."
                        )
                    result.skipped.append(
                        SkippedProduct(
                            base_product_id=product.id,
                            base_sku=product.sku,
                            location="",
                            reason=SkipReason.NO_SUPPLIER,
                            detail=(
                                f"{product.name or product.sku} has a reorder "
                                "point but no supplier, so it can never be "
                                "ordered automatically. Add the supplier "
                                + where
                            ),
                        )
                    )
                continue

            if product.supplier_id not in suppliers:
                # The last silent skip, and it answers the question a person
                # actually asks: "why is X missing from the order?" A product
                # below its minimum whose supplier is not opted in is not a
                # mistake — but invisibly dropping it means the only way to
                # discover the supplier attribution is to notice an absence.
                # Products NOT below their minimum stay silent; reporting the
                # whole catalogue's supplier assignments helps no one.
                self._report_not_opted_in(
                    result=result,
                    product=product,
                    reorder_params=reorder_params,
                    availability=availability,
                    inbound=inbound,
                    locations=locations,
                )
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
                                f"{product.name or product.sku}: no "
                                "MinimumBeforeReorder set at product or "
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

    def _report_not_opted_in(
        self,
        *,
        result: RunResult,
        product: Product,
        reorder_params: dict[str, list[ReorderParameters]],
        availability: dict[tuple[str, str], Availability],
        inbound,
        locations: list[str],
    ) -> None:
        """Name a product that WOULD have been ordered, were its supplier in.

        Uses the same trigger arithmetic as evaluate(), because the point is
        to answer "why is this missing" with the same judgement the order
        lines were built with. Two causes look identical from the outside and
        the detail distinguishes them: the supplier genuinely is not meant to
        be automated, or the product's DEFAULT supplier in Cin7 is not the
        one the user buys it from — a product can list several suppliers, and
        this tool follows the default.
        """
        for location in locations:
            if not self.config.includes_location(location):
                continue
            point = resolve_reorder_point(
                reorder_params.get(product.id, []),
                supplier_id=product.supplier_id,
                location=location,
            )
            if point is None:
                continue
            stock = availability_for(availability, product.id, location)
            position = (
                stock.on_hand
                + inbound.get(product.id, location)
                - stock.allocated
            )
            if position > point.minimum + EPSILON:
                continue
            supplier = (
                product.supplier_name or product.supplier_id or "(unknown)"
            )
            result.skipped.append(
                SkippedProduct(
                    base_product_id=product.id,
                    base_sku=product.sku,
                    location=location,
                    reason=SkipReason.SUPPLIER_NOT_OPTED_IN,
                    detail=(
                        f"{product.name or product.sku} is at or below its "
                        f"minimum ({position:g} vs {point.minimum:g}) but its "
                        f"supplier '{supplier}' is not automated. Either add "
                        "that supplier to `suppliers.pin`, or — if you buy "
                        "this from someone else — fix the DEFAULT supplier "
                        "on the product's Suppliers tab in Cin7: the product "
                        "may list several and this tool follows the default."
                    ),
                )
            )

    # -- explain -----------------------------------------------------------

    def explain(self, fragments: list[str]) -> str:
        """Trace named products through the run: why is each (not) ordered?

        Exists because every silent skip so far was discovered the same way —
        a person noticing an absence on a draft and asking "why is X
        missing?". The report shows categories; this answers for one product
        at a time, from the raw facts the decision was made from, including
        the suppliers the API actually returns for the product (which can
        differ from what the Cin7 screen shows).

        Read-only, and independent of dry_run: it never writes.
        """
        wanted = tuple(f.strip().lower() for f in fragments if f and f.strip())
        if not wanted:
            return "Nothing to explain — give a SKU or product-name fragment."

        result = RunResult()
        opted_in = self._load_suppliers(result)
        products, reorder_params, boms, raw_records = self._load_products(
            capture_fragments=wanted
        )
        bom = BomIndex.build(boms)
        availability = self._load_availability(result)
        purchases = self._load_purchases(result, opted_in)
        our_drafts = {p.id: p for p in purchases if p.is_draft and is_ours(p)}
        inbound = reconstruct(
            purchases, bom, exclude_purchase_ids=set(our_drafts)
        )
        locations = sorted(
            {a.location for a in availability.values() if a.location}
        ) or [""]

        matches = sorted(
            (
                p
                for p in products.values()
                if any(f in f"{p.sku} {p.name}".lower() for f in wanted)
            ),
            key=lambda p: p.sku,
        )

        out: list[str] = []
        if not matches:
            return (
                "No product in the catalogue matches "
                + ", ".join(repr(f) for f in fragments)
                + ". Matching is a case-insensitive substring of the SKU or "
                "the name, so a miss means the product is named differently "
                "in Cin7 than expected — search the product list there for "
                "what it is actually called."
            )

        shown = matches[:_MAX_EXPLAIN_MATCHES]
        if len(matches) > len(shown):
            out.append(
                f"{len(matches)} products match; showing the first "
                f"{len(shown)}. Use a narrower fragment for the rest."
            )
            out.append("")

        for product in shown:
            out.extend(
                self._explain_product(
                    product=product,
                    raw=raw_records.get(product.id, {}),
                    opted_in=opted_in,
                    reorder_params=reorder_params,
                    bom=bom,
                    availability=availability,
                    inbound=inbound,
                    locations=locations,
                    products=products,
                )
            )
            out.append("")

        for warning in result.warnings:
            out.append(f"NOTE: {warning}")

        return "\n".join(out).rstrip() + "\n"

    def _explain_product(
        self,
        *,
        product: Product,
        raw: dict,
        opted_in: dict[str, str],
        reorder_params: dict[str, list[ReorderParameters]],
        bom: BomIndex,
        availability: dict[tuple[str, str], Availability],
        inbound,
        locations: list[str],
        products: dict[str, Product],
    ) -> list[str]:
        out = [
            f"=== {product.sku} — {product.name or '(no name)'}",
            f"    product id {product.id}",
        ]

        listed = schema.parse_product_suppliers(raw)
        effective = self._order_supplier(product, bom, products)
        via_pack = bool(effective.supplier_id) and not product.supplier_id

        if listed:
            out.append("    Suppliers the API returns for this product:")
            for sid, sname, is_default in listed:
                tags = []
                if is_default:
                    tags.append("marked default")
                if sid and sid in opted_in:
                    tags.append("automated")
                suffix = f"  [{', '.join(tags)}]" if tags else ""
                out.append(
                    f"      - {sname or '(unnamed)'} ({sid or 'no id'}){suffix}"
                )
        elif via_pack:
            out.append(
                "    Suppliers the API returns for this product: none on "
                "the product itself."
            )
        else:
            out.append(
                "    Suppliers the API returns for this product: NONE. "
                "Whatever the Cin7 screen shows, the API returns no supplier "
                "link — open the product's Suppliers tab in Cin7, add or "
                "re-save the supplier, then run explain again to confirm it "
                "took."
            )

        if via_pack:
            pack_link = bom.resolve(product.id)
            out.append(
                f"    Pack {pack_link.display_sku} — what actually gets "
                f"ordered — names "
                f"{effective.supplier_name or effective.supplier_id}, and "
                "the run follows that."
            )
            product = effective

        followed = product.supplier_id
        if not followed:
            out.append(
                "    -> Invisible to the run: with no supplier there is "
                "nothing to raise a purchase order against."
            )
        else:
            name = (
                product.supplier_name
                or self._all_suppliers.get(followed)
                or followed
            )
            if followed in opted_in:
                out.append(f"    The run orders this from: {name} — automated.")
            elif followed in self._all_suppliers:
                pin = ", ".join(self.config.suppliers.pin) or "(empty)"
                out.append(
                    f"    The run attributes this to: "
                    f"{self._all_suppliers[followed]} — NOT automated (the "
                    f"pin is: {pin}). Add that supplier to `suppliers.pin`, "
                    "or — if you buy this from someone already automated — "
                    "make that supplier the default on the product's "
                    "Suppliers tab in Cin7."
                )
            else:
                out.append(
                    f"    The product points at supplier id {followed} "
                    f"('{name}'), which does not exist in the account's "
                    "supplier list. The link is stale — the supplier was "
                    "deprecated or merged. Re-pick the supplier on the "
                    "product's Suppliers tab in Cin7."
                )

        if bom.is_pack(product.id):
            parts = []
            for component_id, qty in bom.components_in_base(product.id, 1.0):
                component = products.get(component_id)
                parts.append(
                    f"{qty:g} × {component.sku if component else component_id}"
                )
            out.append(
                "    This is a PACK SKU — its bill of materials says one "
                f"contains {', '.join(parts)}. Packs are never evaluated "
                "against their own stock level (they disassemble on "
                "receipt); the component is what gets checked, and this "
                "pack is what lands on the order when the component "
                "resolves to it. Run explain on the component to see that "
                "side."
            )
            return out

        conflict = bom.conflict_for(product.id)
        if conflict is not None:
            out.append(
                "    BOM CONFLICT: this product is a component of more than "
                "one pack ("
                + ", ".join(conflict.pack_skus or conflict.pack_product_ids)
                + "), so there is no way to tell which pack to order. It is "
                "skipped everywhere until one pack per component is chosen "
                "in Cin7."
            )

        link = bom.resolve(product.id)
        if link is not None:
            out.append(
                f"    Ordered as pack {link.display_sku} "
                f"({link.units_per_pack:g} per pack)."
            )

        for location in locations:
            if not self.config.includes_location(location):
                out.append(
                    f"    {location}: excluded by the location filter in "
                    "config.yaml."
                )
                continue

            point = resolve_reorder_point(
                reorder_params.get(product.id, []),
                supplier_id=product.supplier_id,
                location=location,
            )
            stock = availability_for(availability, product.id, location)
            incoming = inbound.get(product.id, location)
            position = stock.on_hand + incoming - stock.allocated
            facts = (
                f"on hand {stock.on_hand:g}, allocated {stock.allocated:g}, "
                f"inbound {incoming:g} -> position {position:g}"
            )

            if point is None:
                out.append(
                    f"    {location}: no usable MinimumBeforeReorder (unset "
                    f"or 0) at product or location level, so it never "
                    f"triggers. {facts}."
                )
                continue

            quantity = (
                f"{point.reorder_quantity:g}"
                if point.reorder_quantity is not None
                else "UNSET"
            )
            detail = (
                f"minimum {point.minimum:g} ({point.source} level), "
                f"reorder quantity {quantity}, {facts}"
            )

            if position > point.minimum + EPSILON:
                out.append(
                    f"    {location}: above its minimum — nothing to order. "
                    f"{detail}."
                )
            elif not point.has_orderable_quantity:
                out.append(
                    f"    {location}: AT OR BELOW its minimum but "
                    f"ReorderQuantity is unset — a trigger with nothing to "
                    f"fire. Set it in Cin7. {detail}."
                )
            elif (
                product.supplier_id
                and product.supplier_id in opted_in
                and conflict is None
            ):
                out.append(
                    f"    {location}: WOULD ORDER {quantity} base units on "
                    f"the next run. {detail}."
                )
            else:
                if conflict is not None:
                    blocker = "the BOM conflict above"
                elif product.supplier_id:
                    blocker = "its supplier is not automated (see above)"
                else:
                    blocker = "it has no supplier"
                out.append(
                    f"    {location}: at or below its minimum and WOULD "
                    f"order, but {blocker}. {detail}."
                )

        return out

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

    def _write_order_lines(
        self,
        *,
        purchase_id: str,
        reference: str,
        lines: list[dict],
        fingerprint: str,
    ) -> None:
        """Put the lines on a purchase.

        POST both creates and updates. Confirmed live: PUT answers 405, "the
        requested resource does not support http method 'PUT'" — an
        endpoint-level refusal, not a per-record one, so PUT will not start
        working on some other purchase order.

        It is still tried as a fallback, because a wrong guess costs one
        rejected call while not trying costs a draft that never picks up its
        new quantities — or a second purchase order raised alongside it.
        """
        payload = schema.build_order_payload(
            purchase_id=purchase_id,
            reference=reference,
            lines=lines,
            fingerprint=fingerprint,
            extra=self.config.purchase.order_fields,
        )

        first, second = self.client.post, self.client.put

        # Python unbinds `except ... as name` when the block exits, so the
        # first error is captured into an outer name to survive to the
        # re-raise below.
        first_error: Optional[Cin7Error] = None
        try:
            first(schema.ENDPOINT_PURCHASE_ORDER, payload)
            return
        except Cin7Error as exc:
            first_error = exc
            log.info(
                "First verb rejected for %s on %s (%s); trying the other",
                schema.ENDPOINT_PURCHASE_ORDER,
                purchase_id,
                exc,
            )

        try:
            second(schema.ENDPOINT_PURCHASE_ORDER, payload)
        except Cin7Error:
            # The fallback verb answers a bare 405 here, which would bury the
            # error that actually names the problem — a 400 saying which
            # required attribute is missing. Report the informative one.
            raise first_error

    def _write_drafts(
        self, result: RunResult, our_drafts: dict[str, PurchaseOrder]
    ) -> None:
        store = FingerprintStore(self.state_path)

        grouped: dict[tuple[str, str], list[SuggestedLine]] = {}
        for line in result.lines:
            grouped.setdefault((line.supplier_id, line.location), []).append(line)

        for (supplier_id, location), lines in sorted(grouped.items()):
            reference = run_reference(supplier_id, location)

            matching = [
                draft
                for draft in our_drafts.values()
                if draft.supplier_id == supplier_id and draft.location == location
            ]

            if len(matching) > 1:
                # Only one standing draft per supplier and location is ever
                # intended. More than one means an earlier run failed to
                # recognise its own work and raised another — the duplicate
                # ordering this tool exists to prevent, aimed at itself. It
                # updates one and says which others to go and look at, rather
                # than guessing which is the real one.
                others = ", ".join(
                    d.reference or d.id for d in matching[1:]
                )
                result.warnings.append(
                    f"{len(matching)} standing drafts for {reference}. Only "
                    f"the first is being updated; check and delete: {others}. "
                    "More than one means a previous run did not recognise its "
                    "own draft, which is worth understanding before this runs "
                    "unattended."
                )

            existing = matching[0] if matching else None

            # Capped lines are reported, never ordered — config.yaml and the
            # README both promise exactly that, and until now the promise was
            # false: the flag made it to the report while the full quantity
            # went onto the draft.
            writable = [
                line for line in lines if LineFlag.CAP_EXCEEDED not in line.flags
            ]
            held_back = len(lines) - len(writable)
            if held_back:
                result.warnings.append(
                    f"{held_back} line(s) for {reference} exceed a safety cap "
                    "and were NOT put on the draft. They are in the report "
                    "with their computed quantities; a cap trip usually means "
                    "a wrong BOM ratio or a stale reorder point."
                )
            if not writable:
                continue

            written = fingerprint(writable)

            plan = decide(
                existing=existing,
                reference=reference,
                desired_fingerprint=written,
                # The purchase order's own memo first, the local file only as
                # a fallback for drafts written before the memo carried it.
                # The record travels with its own history; the file does not
                # exist on a fresh checkout and did not survive CI.
                stored_fingerprint=(
                    (existing.fingerprint or store.get(existing.id))
                    if existing is not None
                    else None
                ),
            )

            if plan.decision == DraftDecision.LEAVE_ALONE:
                result.drafts_left_alone.append(
                    f"{plan.reference or plan.purchase_id} — {plan.reason}"
                )
                continue

            lines = writable
            payload = schema.build_purchase_payload(
                supplier_id=supplier_id,
                location=location,
                reference=reference,
                fingerprint=written,
                order_date=f"{date.today().isoformat()}T00:00:00",
                extra=self.config.purchase.extra_fields,
            )

            order_lines = [
                schema.build_purchase_line(
                    product_id=line.order_product_id,
                    sku=line.order_sku,
                    quantity=line.quantity,
                    extra=self.config.purchase.line_fields,
                )
                for line in lines
            ]

            purchase_id: Optional[str] = None
            updating = False
            try:
                if plan.decision == DraftDecision.UPDATE and plan.purchase_id:
                    purchase_id = plan.purchase_id
                    updating = True
                else:
                    response = self.client.post(schema.ENDPOINT_PURCHASE, payload)
                    purchase_id = schema.as_str(
                        schema.get_first(
                            response if isinstance(response, dict) else {},
                            "ID",
                            "PurchaseID",
                            "TaskID",
                        )
                    )
                    if not purchase_id:
                        # Without the id the lines have nowhere to go, and a
                        # header with no lines is a purchase order for nothing.
                        result.warnings.append(
                            f"Created a purchase for {reference} but Cin7 "
                            "returned no ID, so its lines could not be added. "
                            "There is now an empty draft in Cin7 to delete."
                        )
                        continue

                # The lines are a separate write. A purchase created without
                # them is a real, visible, empty purchase order — so if this
                # fails, say which one to go and delete.
                self._write_order_lines(
                    purchase_id=purchase_id,
                    reference=reference,
                    lines=order_lines,
                    fingerprint=written,
                )

                store.set(purchase_id, written)
                if updating:
                    result.drafts_updated.append(plan.reference)
                else:
                    result.drafts_created.append(plan.reference)
            except Cin7Error as exc:
                if purchase_id and not updating:
                    # The header exists but the lines never landed: there is
                    # a real, visible, EMPTY purchase order in Cin7 right now.
                    # Say so, name it, and say what happens next — the next
                    # run recognises an empty draft of ours and fills it in.
                    result.warnings.append(
                        f"Failed to write draft {reference}: {exc}. The "
                        f"header was created, so an empty purchase order "
                        f"({purchase_id}) now exists in Cin7. The next run "
                        "will fill it in; delete it by hand only if you want "
                        "it gone sooner."
                    )
                else:
                    result.warnings.append(
                        f"Failed to write draft {reference}: {exc}"
                    )

        # 3. Standing drafts whose demand has cleared. _write_drafts only
        # visits groups that have lines THIS run, so without this a draft
        # whose products recovered would sit in Cin7 indefinitely, stale,
        # waiting for someone to authorise last month's quantities.
        for draft in our_drafts.values():
            if (draft.supplier_id, draft.location) not in grouped:
                result.drafts_left_alone.append(
                    f"{draft.reference or draft.id} — nothing is below its "
                    "minimum for this supplier and location any more; the "
                    "draft is stale. Delete it in Cin7."
                )

        store.save()
