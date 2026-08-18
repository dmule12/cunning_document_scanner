# Cin7 reorder automation

Computes stock shortfalls on base SKUs and raises **draft** purchase orders against the
pack SKUs your suppliers actually sell.

Background and reasoning: [`docs/cin7-po-automation-feasibility.md`](../../docs/cin7-po-automation-feasibility.md).

---

## Status against a real account

A first `probe` run has settled some of this. Current state:

| | Status |
| --- | --- |
| The arithmetic — shortfalls, pack conversion, inbound reconstruction, rounding | Tested, 195 passing tests, no network needed |
| The wiring — pipeline stages, supplier filtering, safety caps | Tested against a mock Cin7 |
| Authentication | ✅ Confirmed live |
| Per-line received quantities on `GET /purchase` | ✅ Confirmed live — partial receipts net off correctly |
| `MinimumBeforeReorder` / `ReorderQuantity` on products | ✅ Confirmed live |
| Supplier attributes | ✅ Found — ten numbered slots, see below |
| Bills of materials, supplier links, reorder levels | ✅ Reachable — behind include-flags, see below |
| Stock levels | ✅ At `ref/productAvailability`, not the documented `productAvailability` |
| Advanced and Service purchases | ✅ At `advanced-purchase` — hyphenated, resolved at runtime |
| Purchase list statuses, supplier keys, order type | ✅ Surveyed across 2312 live orders |
| Whether a draft purchase can be updated | Untested — needs a manual write |

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
cd tools/cin7-reorder
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
```

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

### Draft updates may not be possible

Cin7's documented Purchase methods are GET, POST and DELETE. PUT is unconfirmed. If it turns
out drafts can't be updated, the fallback is delete-and-recreate — note that changes the PO
number every run, and **voiding in Cin7 is permanent.**

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

195 tests, offline, well under a second. The ones that matter most:

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
