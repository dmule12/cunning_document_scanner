# Cin7 reorder automation

Computes stock shortfalls on base SKUs and raises **draft** purchase orders against the
pack SKUs your suppliers actually sell.

Background and reasoning: [`docs/cin7-po-automation-feasibility.md`](../../docs/cin7-po-automation-feasibility.md).

---

## Read this before running anything

**The API layer has never run against a real Cin7 account.** Cin7's API documentation was
not reachable from the environment where this was written, so every response field name in
[`cin7_reorder/schema.py`](cin7_reorder/schema.py) is an educated guess.

What that means concretely:

| | Status |
| --- | --- |
| The arithmetic — shortfalls, pack conversion, inbound reconstruction, rounding | Tested, 126 passing tests, no network needed |
| The wiring — pipeline stages, supplier filtering, safety caps | Tested against a mock Cin7 |
| **The field names — does Cin7 actually respond in these shapes?** | **Unverified. Run `probe`.** |

`probe` is the first command for a reason. Do not schedule `plan`, and certainly do not run
`apply`, until it comes back clean.

---

## What it does

1. Reads suppliers, keeps only those opted in via an **`Auto Reorder`** additional attribute.
2. Reads products and their stored reorder points (`MinimumBeforeReorder`, `ReorderQuantity`).
3. Reads bills of materials and inverts them into a **sleeve → box** index.
4. Reads stock levels, driving from the product list so stocked-out products stay visible.
5. Reads every open purchase order and **reconstructs inbound stock** in base units.
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

126 tests, offline, well under a second. The ones that matter most:

- `test_inbound.py` — partial receipts. 10 boxes ordered, 4 received: 96 sleeves are already
  in on-hand, only 144 are still inbound. Counting all 240 suppresses real reorders while
  looking completely normal.
- `test_reorder.py` — a missing `productAvailability` row means zero stock, not "skip".
- `test_bom.py` — a sleeve in two different boxes is a conflict to report, never a guess.
- `test_drafts.py` — an edited draft is never overwritten.

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
