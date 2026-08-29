# Cin7 reorder automation

Computes stock shortfalls on base SKUs and raises **draft** purchase orders against the
pack SKUs your suppliers actually sell.

Background and reasoning: [`docs/cin7-po-automation-feasibility.md`](../../docs/cin7-po-automation-feasibility.md).

---

## Status against a real account

A first `probe` run has settled some of this. Current state:

| | Status |
| --- | --- |
| The arithmetic — shortfalls, pack conversion, inbound reconstruction, rounding | Tested, 257 passing tests, no network needed |
| The wiring — pipeline stages, supplier filtering, safety caps | Tested against a mock Cin7 |
| Authentication | ✅ Confirmed live |
| Per-line received quantities on `GET /purchase` | ✅ Confirmed live — partial receipts net off correctly |
| `MinimumBeforeReorder` / `ReorderQuantity` on products | ✅ Confirmed live |
| Supplier attributes | ✅ Found — ten numbered slots, see below |
| Bills of materials, supplier links, reorder levels | ✅ Reachable — behind include-flags, see below |
| Stock levels | ✅ At `ref/productAvailability`, not the documented `productAvailability` |
| Advanced and Service purchases | ✅ At `advanced-purchase` — hyphenated, resolved at runtime |
| Purchase list statuses, supplier keys, order type | ✅ Surveyed across 2312 live orders |
| Inbound reconstruction | ✅ Working on live data — 4 duplicate orders prevented on the first clean run |
| Creating a draft purchase order | ✅ Confirmed live — header, then lines to `purchase/order` |
| The suggestions themselves | ✅ One run reviewed and ordered by hand from |
| Recognising its own standing draft | ✅ Fixed — read `Order.Status`, not the overall status |
| Updating a standing draft | ✅ Confirmed live — `POST purchase/order`; `PUT` answers 405 |
| Behaviour across a supplier lead time | Untested — needs partial receipts against real inbound |
| Adversarial review | ✅ 6 dimensions, 93 agents; 21 confirmed defects fixed, each pinned in `test_hardening.py` |

`probe` is still the first command to run.

### Cin7 hides nested collections behind opt-in flags

This is the single most important thing to know about this API, and it cost several rounds to
find.

`BillOfMaterialsProducts`, `ReorderLevels` and `Suppliers` all come back as **empty lists**
unless you explicitly ask for them — no error, no warning. An empty `BillOfMaterialsProducts`
is indistinguishable from a product that genuinely has no bill of materials, and an empty
`Suppliers` makes every product look supplier-less, which would make the whole run skip the
catalogue while reporting success.

Each collection needs its own flag. `IncludeAll=true` does nothing.

```
IncludeBOM=true            → BillOfMaterialsProducts
IncludeReorderLevels=true  → ReorderLevels
IncludeSuppliers=true      → Suppliers
```

These live in `PRODUCT_INCLUDE_FLAGS` in `schema.py` and are sent on every product read. A
test asserts they are present on each call, because nothing at runtime would tell you if they
went missing.

There is also no `DefaultSupplierID` field on a product — the supplier comes from the
`Suppliers` collection, which is why the flag matters as much as the BOM one.

### Supplier attributes are numbered slots

Cin7 returns `AdditionalAttribute1` … `AdditionalAttribute10` as flat fields, with the
human-readable labels held in the attribute set definition rather than on the supplier. So the
slot is named directly in config:

```yaml
suppliers:
  attribute_field: AdditionalAttribute1
```

`probe` prints which slots hold values on each supplier, so you can match them by content.

---

## What it does

1. Reads suppliers, keeps only those opted in via an **`Auto Reorder`** additional attribute.
2. Reads products and their stored reorder points (`MinimumBeforeReorder`, `ReorderQuantity`).
3. Reads bills of materials and inverts them into a **sleeve → box** index.
4. Reads stock levels, driving from the product list so stocked-out products stay visible.
5. Reads the open purchase orders **belonging to those suppliers** and
   **reconstructs inbound stock** in base units.
6. Works out what to order, rounds up to whole packs, applies MOQs and safety caps.
7. Creates or updates **draft** purchase orders.

It **never authorises a purchase order and never emails a supplier.** Cin7 has no API for
sending a PO anyway, so a person opens the draft, checks it, and clicks Email.

---

## Why it exists at all

Cin7 has built-in low-stock reorder. It does not help here, for one reason:

> **Cin7 reorders the SKU that ran low. That's the sleeve. Your supplier sells boxes.**

Setting reorder quantities to case packs doesn't fix that — it changes the *quantity*, not
the *product*. You get a request for 48 sleeves instead of 37, still against a SKU the
supplier won't fill.

There is a second gap that costs money more quietly. **An open purchase order for boxes does
not appear against the sleeve's `OnOrder`.** Cin7 only connects them at receipt, when
auto-disassembly runs. So anything reading `OnOrder` sees nothing on its way and reorders the
same shortfall every run — roughly four duplicate orders over a two-week lead time on a
twice-weekly schedule.

This tool ignores `OnOrder` entirely and rebuilds the number from open POs.

---

## Leaving a warehouse out

```yaml
locations:
  exclude:
    - VIC Warehouse
```

Excluded locations get no order lines, and their per-location reorder points
are never read — a location-level minimum only ever applies to its own
location, so an excluded warehouse's levels cannot leak into another's
calculation.

Use `exclude` rather than `include` for "ignore that one warehouse". An
allowlist silently drops a warehouse opened later and nothing in the report
would say so; a denylist lets a new one in, where the worst case is a draft
purchase order somebody reads and deletes.

Not every shortfall is something to buy. A warehouse stocked by transfer from
another will read as short of everything, and orders raised against it look
entirely normal on the report — real SKUs, real quantities, real supplier.

Names are matched ignoring case and surrounding spaces, and a filter naming no
warehouse on the account is reported rather than ignored. That is the failure
worth catching: configured, and doing nothing.

---

## Where the API calls go

Everything except purchase orders pages in bulk — 500 records a call — so the
whole catalogue costs a handful of requests. Purchase orders are read one at a
time, one call each and **two for an Advanced or Service purchase**, which
answers a 400 naming the right endpoint before it answers anything useful.

That one stage is the whole cost of a run, and on a busy account it will
exhaust Cin7's 5000/day allowance and spend the rest of the run being 429'd.
So the list is filtered before anything is fetched:

- **received orders** — see below; this is the big one
- **voided and completed orders** — nothing is on its way, so nothing to count
- **other suppliers' orders** — a run ordering from one supplier does not need
  to read everyone else's paperwork

An order whose list row names no supplier at all is read anyway. Skipping it
would be cheaper and occasionally wrong, and being wrong here means ordering
goods that are already on the water.

### `Status` does not tell you whether an order is open

Cin7 leaves a purchase **AUTHORISED for its whole life**. An order placed,
received and invoiced two years ago still reads as authorised, and tracking of
what actually arrived lives in a separate field. So `Status` alone makes very
nearly every purchase the account has ever raised look open — five pages of
list rows, and a detail call for each.

A list row carries **three** status fields and they disagree with each other on
almost every row. Measured across 2312 orders on a live account:

| | |
| --- | --- |
| `OrderStatus=AUTHORISED` | 1946 |
| `Status=COMPLETED` | 1966 |
| `CombinedReceivingStatus=FULLY RECEIVED` | 2214 |
| Genuinely open | **34** |

`CombinedReceivingStatus` is the field that answers the question, with both
status fields as a cross-check. It is matched against the whole string, never a
token, because **"PARTIALLY RECEIVED" contains "RECEIVED"** and means the
opposite. Getting that backwards would close an order with stock still on the
water, which understates inbound and re-orders goods already paid for — quieter
and more expensive than the mistake it replaced.

An order with no receiving status, or one this code does not recognise, is
treated as open. Silence is not evidence of arrival. Being wrong in that
direction costs a call; being wrong in the other costs a duplicate order.

`OrderStatus=CLOSED` is real and was missing from the status map — 32 orders
were reading as unknown, and unknown means open.

### Advanced purchases cost double, but need not

`/purchase` refuses an Advanced or Service purchase with a 400 naming the right
endpoint, so reading one the obvious way costs two calls: a refusal and an
answer.

The list row's **`Type`** field — "Simple Purchase", "Advanced Purchase",
"Service Purchase" — says which it is, so the refusal need never be paid at
all. Where `Type` is missing, whichever endpoint served the last purchase is
tried first, since accounts tend to use one kind for nearly everything.

Both are only ever a starting point. The other endpoint is still tried on a
miss, so a wrong guess costs a call and never an order.

A response is only accepted if it carries the ID that was asked for. Trying
endpoints in turn means occasionally asking the wrong one, and a wrong endpoint
answering `200` with an empty shell would read as a purchase with no lines:
inbound stock silently disappearing, which is the worst failure available here.

### Seeing it for yourself

```bash
.venv/bin/python -m cin7_reorder dump --purchases
```

Reads the list pages only — five calls, no detail fetches — and reports the
status values your account actually returns, which keys name a supplier, and
how many open orders would cost a fetch. Run it if a `plan` looks expensive:
it names the constant in `schema.py` to change.

`api.max_purchase_details` (default 250) is the backstop if what survives the
filters is still enormous. Reaching it understates inbound stock, so `plan`
says so at the top of the report and `apply` refuses to write at all.

Every run reports what it read and what it left out, under **Coverage**. The
one case the supplier filter gets wrong is an open order from a *different*
supplier that happens to carry a product being reordered here; nothing in the
arithmetic can detect that, which is why the count is printed rather than
assumed away.

---

## Setup

Needs Python 3.10 or newer.

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
```

On Windows the interpreter is at `.venv\Scripts\python` rather than
`.venv/bin/python`; substitute throughout.

### Credentials

Create an application at **https://inventory.dearsystems.com/ExternalAPI** and copy the
**Account ID** and **Application Key**. Cin7's own docs are explicit that these are equivalent
to a login and password.

Either provide them in a file:

```bash
cp .env.example .env
```

```ini
CIN7_ACCOUNT_ID=your-account-id
CIN7_APP_KEY=your-application-key
```

Or as environment variables, which is what CI uses:

```bash
export CIN7_ACCOUNT_ID='your-account-id'
export CIN7_APP_KEY='your-application-key'
```

Environment variables win over the file, so a stray `.env` in a checkout can never shadow a CI
secret. `.env` is gitignored; `.env.example` holds no real values. Quotes and an `export`
prefix in the file are both tolerated, since that is how credentials usually arrive when
pasted.

---

## Usage

### 1. `probe` — settle the unknowns

```bash
.venv/bin/python -m cin7_reorder probe
```

Read-only. Checks authentication, whether BOM components come back with quantities, whether
per-line received quantities are exposed, whether supplier attributes appear, and how many
products actually have a usable `MinimumBeforeReorder`. Where an assumption fails it names
the constant in `schema.py` to change.

That last count is worth reading closely: it tells you how much of your catalogue is
currently eligible for automation at all.

It deliberately does **not** test whether drafts can be updated, because that needs a write.
Do that by hand: create a throwaway draft, then try `PUT /purchase` and `PUT /purchase/order`
against it.

### 2. `plan` — the calculation, writing nothing

```bash
.venv/bin/python -m cin7_reorder plan -o report.md
```

Run this on the real schedule for **at least one full supplier lead time** before letting it
write anything. That is the only window in which the inbound reconstruction gets exercised
against stock genuinely in transit and partially received — and that number has no equivalent
in Cin7's UI to check against.

Compare its suggestions against what you would have ordered by hand.

### On a schedule

`.github/workflows/reorder.yml` runs **`apply`** every Tuesday and Friday at
08:00 Perth, creating draft purchase orders and posting the report to the run
summary page. `plan` and `probe` are available from the dispatch menu for a dry
run.

It needs `CIN7_ACCOUNT_ID` and `CIN7_APP_KEY` as repository secrets, and only
fires from the default branch — a cron on any other branch is inert with
nothing to say so.

There is no state to carry between runs. What the tool last wrote to a draft
is on the draft, in its memo — see below — so a scheduled run, a re-run and a
laptop all read the same truth, and there is no cache to evict, expire or fail
to save.

**`dump` runs from the dispatch menu too**, with its arguments in the
`dump_args` field — `--purchase <guid>`, `--purchases`, `--sku ABC`. The point
is that investigating does not require the Cin7 keys on anyone's laptop: the
runner already has them as secrets, and the output lands on the run summary
page.

Arguments reach the shell through the environment and are restricted to a
plain character set, because an expression pasted straight into a `run:` block
lets anyone who can dispatch the workflow execute arbitrary commands on the
runner.

Dump output includes whole records — supplier names, prices, sometimes
addresses. The run summary is visible to anyone with access to the repository,
which is a reason to keep it private.

**The job goes red when something needs a person**, because nobody reads a
green scheduled run and GitHub only emails on failure. Two conditions qualify:
the run aborted, or a draft write failed and left an empty purchase order in
Cin7 for somebody to delete. Ordinary warnings do not fail it — a job that goes
red twice a week for something nobody must act on is a job people stop looking
at.

### 3. `apply` — create and update drafts

```bash
.venv/bin/python -m cin7_reorder apply -o report.md
```

Refuses to run unless `suppliers.pin` in `config.yaml` names at least one supplier. Pass
`--no-pin` only when you genuinely mean every opted-in supplier.

---

## How much gets ordered

Cin7 stores the reorder point itself — **`MinimumBeforeReorder`**, with a companion
**`ReorderQuantity`**, set on the product and optionally overridden per location. This tool
reads both rather than inferring anything.

The model is a **trigger, not a target**:

```
position = on_hand + inbound − allocated
trigger  = position <= MinimumBeforeReorder
order    = ceil(ReorderQuantity / units_per_pack)   packs
```

So a product 60 units below its minimum with a reorder quantity of 48 gets 48 ordered, not
60. That is what Cin7's own low-stock reorder does, which keeps the two comparable — and it
means a reorder quantity set too low for current demand shows up as a product that keeps
triggering rather than as a number this tool quietly overrode. Those lines are flagged
**"still below minimum after this order"** in the report.

Products with no minimum set — or a minimum of 0, which is Cin7's default — are skipped and
listed. No reorder point means nobody has decided that product should be reordered
automatically.

> An earlier version of this tool got this wrong. It assumed only lead days, safety days and
> reorder quantity were available and derived a par level by estimating demand from sales
> history. That machinery is gone; the number was stored data all along.

---

## Known unknowns

### A draft reports itself as "ORDERING"

Cin7 puts no `OrderStatus` on a purchase *detail* record, and reports a purchase
whose order stage is still a draft as **`Status: "ORDERING"`** at the top level.
That parses as authorised — reasonably, since it means an order is in progress.

Draft-ness comes from **`Order.Status`** instead. Reading the overall status was
wrong in two directions at once:

- the tool never recognised its own standing draft, so it raised a **fresh
  purchase order every run**; and
- other people's abandoned drafts counted as **stock on its way**, which
  suppresses reorders for goods that are never coming. One from 2020 was
  contributing 410 base units.

### The fingerprint lives on the purchase order

What the tool last wrote to a draft is recorded in the order memo, next to the
reference:

```
AUTO-REORDER-2026W35-WA-Warehouse-ba7067f4 fp=9c1f…
```

It used to live in a local JSON file, which did not exist on a fresh checkout,
did not survive a CI cache eviction — or a cache that simply failed to save,
which is what happened — and differed between a laptop and a scheduled run.
Keeping it on the record means every runner reads the same truth, and the
history travels with the thing it describes.

A marker with no fingerprint is still recognised as ours. Drafts written by
earlier versions have none, and reading them as somebody else's work would
strand them permanently.

### A purchase is a header plus sub-resources

`POST /purchase` creates the header. Order lines go to **`POST /purchase/order`** with a
`TaskID`, matching the tabs Cin7's own UI shows: Order, Stock received, Invoice.

Sending lines inside the `POST /purchase` body is accepted, answers `200`, and creates a
purchase order with nothing on it. That happened once, live. `build_purchase_payload` now
takes no lines at all rather than taking them and ignoring them.

---

## Safety

- `plan` is read-only, and the HTTP client physically refuses writes in that mode.
- `apply` requires a supplier pin.
- Never authorises, never emails.
- Per-line and per-run caps mark implausible quantities rather than ordering them.
- **Never overwrites a human's edit.** Every draft we write is fingerprinted; if it differs
  next run, we leave it alone and report it. A duplicate PO is visible and annoying; a
  silently discarded manual correction is invisible and worse.

---

## Tests

```bash
.venv/bin/python -m pytest
```

257 tests, offline, well under a second. The ones that matter most:

- `test_inbound.py` — partial receipts. 10 boxes ordered, 4 received: 96 sleeves are already
  in on-hand, only 144 are still inbound. Counting all 240 suppresses real reorders while
  looking completely normal.
- `test_reorder.py` — a missing `productAvailability` row means zero stock, not "skip".
- `test_bom.py` — a sleeve in two different boxes is a conflict to report, never a guess.
- `test_drafts.py` — an edited draft is never overwritten.
- `test_purchase_filtering.py` — 300 open orders from a supplier we don't order
  from cost zero calls. This one counts requests, not results: a run that gets
  the numbers right by reading the whole account is still broken.

---

## Layout

```
cin7_reorder/
  schema.py      ← every unverified field name lives here, and only here
  client.py      auth, paging, rate limiting, 429 backoff, call budget
  bom.py         reverse sleeve → box index
  inbound.py     inbound reconstruction from open POs
  reorder.py     the core calculation (pure, no I/O)
  reorderpoints.py  reads Cin7's MinimumBeforeReorder / ReorderQuantity
  drafts.py      fingerprinting and the never-clobber rule
  pipeline.py    one run, start to finish
  probe.py       answers the gating questions
  report.py      Markdown + JSON run report
  cli.py         probe / plan / apply
```
