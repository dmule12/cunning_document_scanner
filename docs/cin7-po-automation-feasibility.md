# Cin7 Core PO Automation — Feasibility Write-Up

**Question asked:** *Can I set up a job that runs twice a week, works out what to reorder from par
and reorder levels, and raises purchase orders in Cin7? Is there an API limitation that stops
purchase orders being fired off?*

**Short answer:** No such limitation exists. `POST /purchase` creates purchase orders without
restriction. The real work is elsewhere: **the thing you count is not the thing you order.**

---

## How to read the confidence markers

The Cin7 documentation sites (`help.core.cin7.com`, `api.cin7.com`) are blocked by the network
egress proxy in the environment where this was researched. Everything below is sourced from search
result excerpts of those same docs plus the public Apiary reference. Claims are therefore marked:

| Marker | Meaning |
| --- | --- |
| **[V]** | Verified — stated explicitly in Cin7 documentation |
| **[C]** | To confirm — inferred from the feature model or permission structure; check against a live account before writing code |

The **[C]** items in [§9](#9-open-questions) marked *gating* decide the shape of the build. Resolve
those before anyone writes code.

---

## 1. Verdict

| Capability | Status |
| --- | --- |
| Create a PO via API | ✅ `POST /purchase` **[V]** |
| Create it as a DRAFT | ✅ POs land in DRAFT, authorised as a separate step **[V]** |
| Authorise via API | ✅ Supported; a `PURCHASE_ORDER_AUTHORISED` webhook exists **[V]** |
| Read stock, suppliers, locations, BOMs | ✅ All exposed **[V]** |
| **Email the PO to the supplier via API** | ❌ No documented endpoint **[C]** |
| **Order a different SKU than the one that ran low** | ❌ Not a Cin7 behaviour **[V]** |
| **Restrict automatic ordering to chosen suppliers** | ❌ Not a Cin7 behaviour — we build it **[V]** |

**On emailing:** in Cin7 Core you send a PO by opening it and clicking **Email → Purchase Order**,
which attaches the PDF automatically **[V]**. There is no API equivalent. Working around it would
mean generating your own PO document and sending from your own mail system — abandoning Cin7's
templates, numbering and audit trail. **We are not doing that.** The job stops at DRAFT and a person
clicks send, which turns this limitation into a non-issue.

---

## 2. The core problem: two SKUs, not two units

Cin7 counts inventory in **sleeves**. The supplier sells **boxes**. Critically:

> **The box is a separate SKU. It is not a unit of measure on the sleeve SKU.**

This is the fact the whole design turns on, and it rules out the obvious approach. You cannot fix
this by setting a UOM on the purchase order line, because the line has to point at a different
product record entirely.

So the job is not unit conversion. It is **SKU substitution**: detect that the *sleeve* SKU is below
par, then order the *box* SKU that contains it.

### What links the two SKUs

Cin7's **Additional Units of Measure (AUOM)** is the feature that ties them together, and it is
evidently **implemented as an assembly BOM**:

- Editing AUOMs requires the **Product – Bill of Materials** permission **[V]**.
- Cin7's BOM documentation describes assembly BOMs as being for *"joining together multiple items in
  a pack or kit, or disassembling a case of stock into its component units"* **[V]** — AUOM
  behaviour, in BOM vocabulary.
- **Auto-disassembly fires when goods are received** (the Receive tab in Purchase). Receiving 2 boxes
  stocks 48 sleeves automatically **[V]**.

Your observation that these are separate SKUs is consistent with that: AUOM creates a distinct
product record for the pack, with a BOM linking it to its components. The receipt-side conversion
should therefore already work — you order boxes, you receive sleeves into stock.

### The consequence for the built-in features

This significantly weakens Cin7's own low-stock reorder for your case, and weakens the interim
workaround suggested in earlier drafts of this document.

**Cin7's built-in low stock reorder reorders the SKU that is low.** The SKU that goes low is the
sleeve. So the built-in feature raises a PO for sleeves — a product your supplier does not sell.
Setting the per-supplier reorder quantity to the case pack does not fix this: it changes the
*quantity*, not the *product*. You get a request for 48 sleeves rather than 37, still against a SKU
the supplier will not fill.

**Cross-SKU substitution is the thing Cin7 does not do, and the reason this build is justified.**

---

## 3. SKU resolution — finding the box from the sleeve

The script needs a **reverse BOM index**: given a sleeve SKU, which box SKU contains it?

`GET /BillOfMaterials` supports an `onlyProductsWithBOM=true` filter **[V]**, returning products
along with their `BOMComponents` **[V]**. That is parent → components. One paged call retrieves the
whole set; the script inverts it in memory:

```
GET /BillOfMaterials?onlyProductsWithBOM=true

  BOX-SLV-24  ──BOMComponents──▶  SLV-001 × 24
  BOX-CUP-50  ──BOMComponents──▶  CUP-014 × 50

invert ▼

  SLV-001  ──▶  BOX-SLV-24  (24 base units per box)
  CUP-014  ──▶  BOX-CUP-50  (50 base units per box)
```

That single inverted map answers both questions the script has:

| Question | Answer from the index |
| --- | --- |
| Is this SKU ordered as a box? | Present as a component → yes, order the parent. Absent → order it directly. |
| How many sleeves per box? | The BOM component quantity. |

**Cin7 stays the single source of truth.** No parallel spreadsheet of pack sizes, nothing to drift.
When someone sets up a new boxed product in Cin7 the normal way, the next run picks it up with no
code change.

> **[C] — gating.** That `GET /BillOfMaterials` returns `BOMComponents` with quantities for every
> parent needs confirming against a live account. The component structure is documented for the
> `PUT` response **[V]**; the `GET` shape is assumed to match. This is the one query the whole design
> depends on.

### Resolution rules

**One box per sleeve SKU.** Each sleeve SKU belongs to exactly one box SKU, so the index is a simple
one-to-one map. If the script ever finds a base SKU with **two or more parents**, that is a data
problem, not a decision to make: it must **report the conflict and skip the product**, never guess
which pack was intended. A silent wrong guess here means ordering the wrong quantity of the wrong
product.

**No parent found → order the base SKU.** Some products genuinely are sold as singles. The script
orders the base SKU directly and flags the line in the run report so the reviewer can confirm it was
deliberate rather than a missing BOM.

---

## 4. Choosing which suppliers are automated

Automatic ordering should apply to **chosen suppliers only** — not everyone in the system.

Cin7 Core supports **Additional Attributes**: custom fields on products, suppliers and customers,
managed under *Settings → Reference books → Other Items → Additional Attributes* **[V]**.

**Design: an `Auto Reorder` attribute on the supplier record.** The run reads suppliers, keeps only
those where the attribute is set, and ignores everything else.

This matters more than a config setting normally would:

- **It is opt-in, not opt-out.** A newly created supplier is automated only when someone deliberately
  turns it on. The failure mode of the alternative — a new supplier silently receiving automated POs
  — is exactly the kind of thing that erodes trust in a scheduled job.
- **You control it without a deploy.** Toggling a supplier on or off is a field edit in Cin7, not a
  code change.
- **It lives next to the data it governs**, visible to anyone looking at the supplier record and
  wondering why orders appear.

Suggest a config-level allowlist *as well* during rollout, so the first live runs can be pinned to a
single supplier regardless of how the attributes are set. Remove the pin once it is trusted.

> **[C]** Confirm that supplier Additional Attributes are returned on `GET /supplier`. Attributes are
> documented for suppliers **[V]**, but their appearance in the API response is unconfirmed. If they
> are not exposed, fall back to a config allowlist of supplier IDs — a real but modest downgrade.

---

## 5. Endpoint map for one run

Auth on every request **[V]**:

```
api-auth-accountid:      <Account ID>
api-auth-applicationkey: <Application Key>
```

Both are created on the API setup page at `inventory.dearsystems.com/ExternalAPI` and are
**equivalent to a login and password** — they belong in a secret store, never in the repo **[V]**.

| # | Call | Purpose |
| --- | --- | --- |
| 1 | `GET /ref/location`, `GET /supplier` | Resolve IDs; read the `Auto Reorder` attribute and build the supplier allowlist |
| 2 | `GET /product?page=N&limit=500` | SKU master, supplier links |
| 3 | `GET /BillOfMaterials?onlyProductsWithBOM=true` | Build the reverse sleeve → box index ([§3](#3-sku-resolution--finding-the-box-from-the-sleeve)) |
| 4 | `GET /productAvailability?page=N&limit=500` | OnHand / Available / Allocated / OnOrder, **on the sleeve SKU** |
| 5 | `GET /purchaseList?OrderStatus=...` | Open POs, for idempotency |
| 6 | `POST /purchase` | One PO per (supplier, location), lines against the **box SKU**. Lands in DRAFT |

**Never called:** any authorise or email step. The run ends with drafts.

Note the asymmetry in calls 4 and 6: **stock is read on the sleeve SKU, the order is written on the
box SKU.** Any code review of this script should check that boundary specifically — it is the
likeliest place for a subtle bug, and the resulting PO would look perfectly plausible.

### A trap in `productAvailability`

This endpoint **only returns rows where available, on hand and on order are not all zero** **[V]**. A
product that has sold out completely with nothing on order **disappears from the response** — and a
stocked-out product is precisely the one most urgently needing a PO.

**Do not use availability as the spine of the reorder calculation.** Drive from the product list
(call 2) and left-join availability onto it, treating a missing row as all-zeros. Getting this
backwards produces a script that silently never reorders the things it most needs to.

Pagination defaults to 100 per page, up to 500 with `?limit=500` **[V]**.

---

## 6. Reorder maths

```
# computed on the SLEEVE SKU
need_base   = par − (OnHand + OnOrder − Allocated)

# ordered on the BOX SKU
boxes       = ceil(need_base / base_units_per_box)
order_line  = { product: box_sku, quantity: boxes }

# or, when no box SKU exists
order_line  = { product: base_sku, quantity: need_base }   # flagged in the report

then apply supplier MOQ
```

`Available = OnHand − Allocated` in Cin7's own model **[V]**; `OnOrder` is included so that stock
already inbound on an existing PO is not ordered twice.

**One caution on `OnOrder`:** an outstanding PO is recorded against the **box** SKU, while the
shortfall is computed against the **sleeve** SKU. Whether Cin7 reflects an inbound box in the
sleeve's `OnOrder` figure — or only on receipt, when auto-disassembly runs — determines whether the
script double-orders across consecutive runs. **This is a gating question** ([§9](#9-open-questions),
Q2). If box POs do not show up in the sleeve's `OnOrder`, the script must compute inbound quantities
itself from open POs in call 5 and convert them through the BOM index.

**Where do par levels come from?** Cin7's native model is **lead, safety and reorder quantity, set
per supplier and per location**, from the Suppliers tab **[V]**. Locations inherit supplier values by
default; a location-level value takes priority; if neither exists Cin7 cannot generate a suggestion
at all **[V]**.

**Recommendation: read par levels from Cin7** unless the reorder logic genuinely exceeds what
lead/safety/reorder-quantity can express. Two sources of truth for reorder policy is a problem that
compounds quietly. Note the levels belong on the **sleeve** SKU, since that is what depletes.

---

## 7. The run report

Every run emits a report alongside the draft POs:

- Every line: sleeve SKU, resolved box SKU, raw need in base units, boxes ordered, rounding applied.
- **Products ordered as base SKUs** because no box parent was found — the reviewer's checklist.
- **Products skipped** because a base SKU resolved to multiple box parents — a data fix queue.
- Suppliers considered vs suppliers skipped for not being opted in.
- API calls made, against the daily budget.

This report is what makes the fallback behaviours safe rather than silent. Without it, a missing BOM
looks identical to a correctly ordered single.

---

## 8. Constraints

### Rate limits

**3 calls/second, 60/minute, 5,000/day**, returning HTTP 429 with a `Retry-After` header in seconds
**[V]**.

The daily cap is the one to design for. With paged bulk reads the whole run costs on the order of
tens of calls — a few hundred pages of products, BOMs and availability, plus one POST per PO. That is
comfortable. It becomes a problem only in the naive per-SKU version:
`GET /productAvailability/{id}` looped over 3,000 products blows the daily budget in a single run.
**Page the bulk endpoints; never loop per SKU.**

Implement 429 handling with `Retry-After` honoured and exponential backoff from the start, not as a
later fix.

### No transactions

There is no rollback across a multi-PO run. Dying after creating 23 of 40 POs leaves 23 real drafts
in Cin7. A re-run must not duplicate them.

**Idempotency:** stamp a deterministic per-run reference — e.g. `AUTO-2026W33-TUE` — on each PO, and
query `purchaseList` for that reference before creating anything. Already present → skip. This makes
the run safely re-runnable, which matters more than it sounds for a job on a schedule nobody watches.

### Draft accumulation

Drafts pile up if nobody reviews them. Two runs a week with no review discipline means an
ever-growing pile of stale suggestions computed against stock levels that have since moved. Needs a
staleness policy: either the run voids its own unreviewed drafts from the previous cycle, or the
report escalates their age. **Decide this before going live** — it is the most likely way this
quietly stops being useful.

---

## 9. Open questions

| # | Question | Impact |
| --- | --- | --- |
| 1 | **Does `GET /BillOfMaterials` return components with quantities?** | **Gating.** The entire sleeve → box index depends on it |
| 2 | **Does an open box-SKU PO appear in the sleeve SKU's `OnOrder`?** | **Gating.** If not, the script must compute inbound stock itself or it will double-order |
| 3 | Are supplier Additional Attributes returned by `GET /supplier`? | Decides whether the allowlist lives in Cin7 or in config |
| 4 | Is every boxed product's BOM actually configured, and one box per sleeve? | Determines the size of the data-cleanup task before go-live |
| 5 | Where do par levels live today? How many SKUs × locations? | Sizes the run and settles the [§6](#6-reorder-maths) question |
| 6 | Is MOQ recorded anywhere in Cin7? | If not, it needs a home — likely an Additional Attribute |

**Resolved since the first draft:** whether `POST /purchase` accepts a UOM on order lines. Now moot —
the box is a separate SKU, so the line points at a product, not a unit.

Questions 1 and 2 are answerable in roughly fifteen minutes against a live account with a REST
client: pull one BOM, then check a product with an open box PO and read its availability record.

---

## 10. Build vs configure

Earlier drafts of this document suggested the built-in **Low stock reorder** might make the build
unnecessary. **The separate-SKU fact removes that option.**

Cin7's built-in reorder raises POs for the SKU that ran low. That is the sleeve, which your supplier
does not sell. No configuration of reorder quantities changes which *product* the PO points at.

**What custom code adds, and only it can:**

1. **Cross-SKU substitution** — ordering the box because the sleeve ran low. This is the whole ball
   game and Cin7 does not do it.
2. **Case rounding** — turning a base-unit shortfall into a whole number of boxes.
3. **Supplier-scoped automation** — running only for suppliers you opted in.

The build is justified. Its risk is not technical difficulty but data quality: it is only as good as
the BOM links and par levels behind it.

---

## 11. Recommended sequence

1. **Answer gating questions 1 and 2** against the live account. Roughly fifteen minutes, and Q2 in
   particular can change the design.
2. **Audit the BOM data.** Every boxed product needs exactly one box SKU with a correct component
   quantity. Export the BOM list and check it against what you actually buy. This is likely the
   largest task in the project and it is not code.
3. **Add the `Auto Reorder` supplier attribute** and set it on one supplier.
4. **Build the calculation read-only**, emitting the run report and creating nothing. Run it against
   real data twice a week and check the suggestions against what you would have ordered by hand.
5. **Enable PO creation** behind the idempotency guard, pinned to that one supplier.
6. **Widen to further suppliers** by toggling the attribute, as confidence builds.

Step 4 matters most. The risk here is not API failure — it is correct-looking POs built on a wrong
BOM quantity or a stale par level. A PO for 12 boxes instead of 2 looks entirely plausible on screen
and costs real money. **Prove the numbers before granting write access.**

---

## Sources

- [Connecting to the Cin7 Core API](https://help.core.cin7.com/hc/en-us/articles/9982480315407-Connecting-to-the-Cin7-Core-API) — auth headers, rate limits
- [API V2 introduction](https://help.core.cin7.com/hc/en-us/articles/10113609017359-API-V2-introduction)
- [List of Endpoints](https://help.core.cin7.com/hc/en-us/articles/9034487144079-List-of-Endpoints)
- [Purchase_POST](https://help.core.cin7.com/hc/en-us/articles/9034511096335-Purchase-POST)
- [Process a Purchase](https://help.core.cin7.com/hc/en-us/articles/9034492644879-Process-a-Purchase) — authorise flow, Email → Purchase Order
- [ProductAvailability](https://help.core.cin7.com/hc/en-us/articles/9034523140879-ProductAvailability)
- [Calculation of Available, On Hand and Allocated Stock quantity](https://help.core.cin7.com/hc/en-us/articles/12429786547983-Calculation-of-Available-On-Hand-and-Allocated-Stock-quantity)
- [Additional Units of Measure](https://help.core.cin7.com/hc/en-us/articles/9034463961231-Additional-Units-of-Measure)
- [Bill Of Materials](https://help.core.cin7.com/hc/en-us/articles/9034480650127-Bill-Of-Materials) — API filters, component structure
- [Bill of materials (assembly)](https://help.core.cin7.com/hc/en-us/articles/9034480629647-Bill-of-materials-assembly)
- [Additional Attributes](https://help.core.cin7.com/hc/en-us/articles/9034463879055-Additional-Attributes)
- [Low stock reorder](https://help.core.cin7.com/hc/en-us/articles/9034475105167-Low-stock-reorder)
- [Set reorder parameters](https://help.core.cin7.com/hc/en-us/articles/10553627492239-Set-reorder-parameters)
- [Configure product suppliers](https://help.core.cin7.com/hc/en-us/articles/12047242513039-Configure-product-suppliers)
- [Email templates and emailing suppliers/customers](https://help.core.cin7.com/hc/en-us/articles/9034435809551-Email-templates-and-emailing-suppliers-customers)
- [Cin7 Core Developer Portal (Apiary)](https://dearinventory.docs.apiary.io/)
