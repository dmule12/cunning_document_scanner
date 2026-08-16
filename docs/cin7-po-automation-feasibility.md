# Cin7 Core PO Automation — Feasibility Write-Up

**Question asked:** *Can I set up a job that runs twice a week, works out what to reorder from par
and reorder levels, and raises purchase orders in Cin7? Is there an API limitation that stops
purchase orders being fired off?*

**Short answer:** No such limitation exists. The Cin7 Core API creates purchase orders perfectly
well. The two things Cin7 genuinely will not do for you are (1) **email a PO to a supplier from the
API**, and (2) **express a reorder suggestion in the unit your supplier actually sells in**. The
design below sidesteps the first entirely and solves the second — which is where the whole value of
this automation sits.

---

## How to read the confidence markers

The Cin7 documentation sites (`help.core.cin7.com`, `api.cin7.com`) are blocked by the network
egress proxy in the environment where this was researched. Everything below is sourced from search
result excerpts of those same docs plus the public Apiary reference. Claims are therefore marked:

| Marker | Meaning |
| --- | --- |
| **[V]** | Verified — stated explicitly in Cin7 documentation |
| **[C]** | To confirm — inferred from the feature model or permission structure; check against a live account before writing code |

Two **[C]** items in [§8](#8-open-questions) are *gating*: they decide the shape of the build. Please
resolve those before anyone writes a line of code.

---

## 1. Verdict

| Capability | Status |
| --- | --- |
| Create a PO via API | ✅ `POST /purchase` **[V]** |
| Create it as a DRAFT | ✅ POs land in DRAFT, authorised as a separate step **[V]** |
| Authorise via API | ✅ Supported; a `PURCHASE_ORDER_AUTHORISED` webhook exists **[V]** |
| Read stock, suppliers, locations, BOMs | ✅ All exposed **[V]** |
| **Email the PO to the supplier via API** | ❌ **No documented endpoint** **[C]** |
| **Round a reorder need up to whole cases** | ❌ Not a Cin7 behaviour **[V]** |
| **Pick box vs base unit per product** | ❌ Not a Cin7 behaviour **[V]** |

The last three are the honest limitations. Only one of them is a real obstacle, and it is not the
one that prompted the question.

**On emailing:** in Cin7 Core you send a PO by opening it and clicking **Email → Purchase Order**,
which attaches the PDF automatically **[V]**. There is no API equivalent. Building around this would
mean generating your own PO document and sending from your own mail system — abandoning Cin7's
templates, numbering and audit trail. **We are not doing that.** The chosen design stops at DRAFT
and a human sends, which turns this limitation into a non-issue.

---

## 2. The units problem

This is the actual problem worth solving.

Cin7 tracks inventory in **base units** — sleeves. Reorder alerts are therefore computed and
reported in sleeves. But suppliers sell by the **box**. An alert saying *"you need 37 sleeves"* is
not an order anyone can place.

### What Cin7 already gives you: Additional Units of Measure

Cin7 Core's **Additional Units of Measure (AUOM)** feature exists for exactly this shape of problem
**[V]**:

- The base unit stays the sleeve.
- You add an additional UOM — "Box" — with a conversion ratio entered in the **"Number of..."**
  field (integer or decimal) **[V]**.
- **Auto-disassembly fires when goods are received** (the Receive tab in Purchase). Receiving 2
  boxes automatically stocks 48 sleeves **[V]**.
- The mirror image also exists: auto-assembly triggers when a sales order is authorised, if you sell
  in cases but stock in singles **[V]**.

Editing AUOMs requires both the **Inventory – Products & Families** and **Product – Bill of
Materials** permissions **[V]** — which is a strong hint about how it is implemented internally, and
[§3](#3-unit-resolution--how-the-script-knows-which-unit-to-use) leans on that.

### What AUOM does *not* fix

**AUOM changes the purchase document and the receipt conversion. It does not change the reorder
suggestion.** Low-stock reorder still computes in base units. Nothing in Cin7 turns *"need 37
sleeves"* into *"order 2 boxes."*

That rounding step — trivial arithmetic, but nobody's job to do 200 times — is the first half of
what the script is for.

### Interim workaround, no code required

Set the per-supplier **reorder quantity** to the case pack size. Suggestions then land on orderable
multiples of the box quantity. The PO still *reads* in sleeves, so a supplier expecting case
quantities may still bounce it — but the numbers at least become fillable. Worth doing today
regardless of whether the automation gets built.

---

## 3. Unit resolution — how the script knows which unit to use

Some products order by the box. Others order by the base unit. The script must know which, per
product, without anyone maintaining a spreadsheet that drifts out of sync with reality.

**The ordering unit is a property of the product, not of the supplier** — a SKU orders the same way
from whoever supplies it. That is a meaningful simplification: the mapping is one-dimensional.

### Primary source: the BOM endpoint

AUOM in Cin7 Core appears to be **implemented as an assembly BOM**. The evidence:

- Editing AUOMs requires the **Product – Bill of Materials** permission **[V]**.
- Cin7's own BOM documentation describes assembly BOMs as being for *"joining together multiple
  items in a pack or kit, or disassembling a case of stock into its component units"* **[V]** —
  a description of AUOM behaviour in BOM vocabulary.

`GET /BillOfMaterials` supports an `onlyProductsWithBOM=true` filter **[V]**. One paged call should
therefore return every SKU that has a case representation, along with its component quantity:

```
GET /BillOfMaterials?onlyProductsWithBOM=true
```

That single response gives the script both things it needs:

| Question | Answer from the response |
| --- | --- |
| Does this SKU have a pack unit? | Present in the result set → yes. Absent → no. |
| How many base units per pack? | The BOM component quantity. |

**Cin7 remains the single source of truth.** No parallel spreadsheet, no drift. When someone sets up
a new boxed product in Cin7 the normal way, the script picks it up on the next run with no code
change.

> **[C] — gating.** The exact BOM payload shape, and confirmation that AUOM definitions actually
> surface through this endpoint, could not be verified from blocked docs. This is inferred from the
> permission model and the feature description, not read off a schema.

### Fallback: Additional Attributes

If AUOM turns out not to be BOM-backed, the fallback stays inside Cin7 rather than escaping to a
config file. Cin7 Core supports **Additional Attributes** — custom fields on products, suppliers and
customers, managed under *Settings → Reference books → Other Items → Additional Attributes*, and
attachable by default to the purchase process **[V]**. A `Case Pack` attribute on each boxed product
gives the same lookup with slightly more setup effort.

### Resolution ladder

The script tries, in order:

1. **BOM / AUOM ratio** — preferred, zero maintenance.
2. **Additional Attribute** (`Case Pack`) — if configured.
3. **Base units** — default, *and the SKU is flagged in the run report*.

**Unknown pack size never blocks the run and never guesses a box quantity.** It orders in sleeves —
always a valid, if inconvenient, order — and tells the reviewer it did so. This is safe precisely
because a human reviews every PO before it leaves the building ([§7](#7-the-run-report)).

---

## 4. Writing the PO line

Knowing the unit is one thing; getting it onto the purchase order is another. Three options:

### (a) AUOM line written in boxes — preferred

The order line carries the Box UOM and a quantity of 2. The PO reads the way the supplier expects,
and auto-disassembly converts on receipt. Clean end to end.

> **[C] — gating.** Whether `POST /purchase` accepts a UOM on order lines is **unverified**. This is
> the single most important thing to check. If it does, build (a) and stop reading.

### (b) Base-unit line, rounded to a whole-case multiple — the safe fallback

The line reads "48 sleeves" — a quantity that *is* two boxes, expressed in the wrong vocabulary. It
always works technically, but a supplier expecting case quantities may query it. Acceptable as a
fallback if (a) is impossible; the reviewer can adjust the UOM in the UI before emailing.

### (c) A separate purchasable "box" SKU

Model the box as its own product, disassembled into sleeves on receipt. Most control, most setup,
most ongoing maintenance — a second SKU per boxed product forever. Recommended only if both (a) and
(b) fail.

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
| 1 | `GET /ref/location`, `GET /supplier` | Resolve IDs once, cache for the run |
| 2 | `GET /product?page=N&limit=500` | SKU master, default supplier, base UOM |
| 3 | `GET /BillOfMaterials?onlyProductsWithBOM=true` | Pack ratios ([§3](#3-unit-resolution--how-the-script-knows-which-unit-to-use)) |
| 4 | `GET /productAvailability?page=N&limit=500` | OnHand / Available / Allocated / OnOrder |
| 5 | `GET /purchaseList?OrderStatus=...` | Open POs, for idempotency |
| 6 | `POST /purchase` | One PO per (supplier, location). Lands in DRAFT |

**Never called:** any authorise or email step. The run ends with drafts.

### A trap in `productAvailability`

This endpoint **only returns rows where available, on hand and on order are not all zero** **[V]**.
A product that has sold out completely with nothing on order **disappears from the response** — and
a stocked-out product is precisely the one most urgently needing a PO.

**Do not use availability as the spine of the reorder calculation.** Drive from the product list
(call 2) and left-join availability onto it, treating a missing row as all-zeros. Getting this
backwards produces a script that silently never reorders the things it most needs to.

Pagination defaults to 100 per page, up to 500 with `?limit=500` **[V]**.

---

## 6. Reorder maths

```
need_base  = par − (OnHand + OnOrder − Allocated)
order_qty  = ceil(need_base / pack_size)     # in pack UOM, when a pack applies
           = need_base                        # in base units, when it does not
then apply supplier MOQ
```

`Available = OnHand − Allocated` in Cin7's own model **[V]**; `OnOrder` is included above so that
stock already inbound on an existing PO is not ordered twice.

**Where do par levels come from?** A decision is needed. Cin7's native model is **lead, safety and
reorder quantity, set per supplier and per location**, from the Suppliers tab **[V]**. Locations
inherit supplier values by default; a location-level value takes priority; if neither exists Cin7
cannot generate a suggestion at all **[V]**.

Two options:

- **Read par levels from Cin7** — one source of truth, and the built-in reorder reports stay
  meaningful. Constrained to Cin7's lead/safety/reorder-quantity vocabulary.
- **Hold them in our own config** — arbitrary logic (velocity, seasonality), at the cost of a second
  place where reorder policy lives, and Cin7's own reports drifting away from what the script does.

**Recommendation: read from Cin7** unless the reorder logic genuinely exceeds what
lead/safety/reorder-quantity can express. Two sources of truth for reorder policy is a problem that
compounds quietly.

---

## 7. The run report

Every run emits a report alongside the draft POs:

- Every line: product, resolved unit, raw need in base units, rounding applied, final quantity.
- **SKUs that fell back to base units** because no pack size resolved — the reviewer's checklist.
- Anything skipped, and why.
- API calls made, against the daily budget.

This report is what makes "default to base units" a safe choice rather than a silent failure mode.
Without it, the fallback becomes invisible and wrong-unit POs reach suppliers.

---

## 8. Constraints

### Rate limits

**3 calls/second, 60/minute, 5,000/day**, returning HTTP 429 with a `Retry-After` header in seconds
**[V]**.

The daily cap is the one to design for. With paged bulk reads the whole run costs on the order of
tens of calls — a few hundred pages of products and availability, plus one POST per PO. That is
comfortable. It becomes a problem only if someone writes the naive per-SKU version:
`GET /productAvailability/{id}` in a loop over 3,000 products blows the daily budget in a single
run. **Page the bulk endpoints; never loop per SKU.**

Implement 429 handling with `Retry-After` honoured and exponential backoff from the start, not as a
later fix.

### No transactions

There is no rollback across a multi-PO run. Dying after creating 23 of 40 POs leaves 23 real drafts
in Cin7. A re-run must not duplicate them.

**Idempotency:** stamp a deterministic per-run reference (e.g. `AUTO-2026W33-TUE`) on each PO, and
query `purchaseList` for that reference before creating anything. Already present → skip. This makes
the run safely re-runnable, which matters more than it sounds for a job on a schedule nobody watches.

### Draft accumulation

Drafts pile up if nobody reviews them. Two runs a week with no review discipline means an
ever-growing pile of stale suggestions computed against stock levels that have since moved. Needs a
staleness policy: either the run voids its own unreviewed drafts from the previous cycle, or the
report escalates their age. **Decide this before going live** — it is the most likely way this
quietly stops being useful.

---

## 9. Build vs configure

Cin7 Core already ships:

- **Low stock reorder** — reorder points per product, per location, with automatic PO generation
  **[V]**.
- **Smart reorder suggestions** — suggested quantities and replenishment recommendations **[V]**.
- Per-supplier, per-location **lead, safety and reorder quantity** parameters **[V]**.

**What custom code adds, and only this:**

1. Case-rounding — turning a base-unit need into whole boxes.
2. Per-product unit selection — box vs base unit, resolved automatically.
3. Reorder logic beyond lead/safety/reorder-quantity, if you need it.

**If the reorder-quantity workaround in [§2](#2-the-units-problem) proves good enough in practice,
do not build this.** Configure the built-in low stock reorder, set reorder quantities to case packs,
and spend the effort elsewhere. The custom build earns its keep when the case-pack workaround
produces POs your suppliers keep querying, or when the reorder maths outgrows Cin7's model.

---

## 10. Open questions

Ordered by how much each changes the design.

| # | Question | Impact |
| --- | --- | --- |
| 1 | **Does `POST /purchase` accept a UOM on order lines?** | **Gating.** Decides between designs (a), (b) and (c) in [§4](#4-writing-the-po-line) |
| 2 | **Does `GET /BillOfMaterials` expose AUOM pack ratios?** | **Gating.** Decides whether unit resolution is free or needs Additional Attributes |
| 3 | Are AUOMs already configured on the affected SKUs? | Determines whether there is a setup project before any automation |
| 4 | Where do par levels live today? How many SKUs × locations? | Sizes the run and settles the [§6](#6-reorder-maths) question |
| 5 | Is MOQ recorded anywhere in Cin7? | If not, it needs a home — likely an Additional Attribute |

Questions 1 and 2 are answerable in about ten minutes against a live account with a REST client.
Both should be settled before any code is written; between them they determine whether this is a
clean build or a compromised one.

---

## 11. Recommended sequence

1. **Answer questions 1 and 2** against the live account. Ten minutes, decides everything.
2. **Configure AUOM** on boxed products if not already done — this has standalone value: it fixes
   receipt conversion whether or not the automation ever gets built.
3. **Set per-supplier reorder quantities to case packs** — the no-code interim fix from
   [§2](#2-the-units-problem). Ship this now.
4. **Live with steps 2–3 for a few weeks.** If the built-in low stock reorder now produces orders
   your suppliers accept, stop here and save the build.
5. **If not, build**, in this order: read-only reorder calculation with a dry-run report first, so
   the maths can be checked against reality before anything writes to Cin7; then PO creation behind
   the idempotency guard; then scheduling.

Step 5's dry-run-first ordering matters. The risk in this project is not API failure — it is
correct-looking POs built on wrong par levels or a misread pack size. Prove the numbers before
granting write access.

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
- [Bill Of Materials](https://help.core.cin7.com/hc/en-us/articles/9034480650127-Bill-Of-Materials) — API filters
- [Bill of materials (assembly)](https://help.core.cin7.com/hc/en-us/articles/9034480629647-Bill-of-materials-assembly)
- [Additional Attributes](https://help.core.cin7.com/hc/en-us/articles/9034463879055-Additional-Attributes)
- [Low stock reorder](https://help.core.cin7.com/hc/en-us/articles/9034475105167-Low-stock-reorder)
- [Set reorder parameters](https://help.core.cin7.com/hc/en-us/articles/10553627492239-Set-reorder-parameters)
- [Smart reorder suggestions](https://help.core.cin7.com/hc/en-us/articles/10955759303055-Smart-reorder-suggestions)
- [Configure product suppliers](https://help.core.cin7.com/hc/en-us/articles/12047242513039-Configure-product-suppliers)
- [Email templates and emailing suppliers/customers](https://help.core.cin7.com/hc/en-us/articles/9034435809551-Email-templates-and-emailing-suppliers-customers)
- [Cin7 Core Developer Portal (Apiary)](https://dearinventory.docs.apiary.io/)
