---
name: cohesion-label-reorder
description: >
  Checks stock of every label The Café Collective buys from Cohesion Labels (sushi, bowls,
  wraps, pots, salads, sandwiches, cans, 50mm dietary rounds, Supernatural and other brand
  labels) in Cin7, works out how many months each will last at the current rate of use, and
  builds a reorder list sized to about 7 months, topped up to Cohesion's price breaks. Offers
  to put it into Cin7 as a draft purchase order, never authorised. Use whenever David asks
  which labels are low, what to reorder from Cohesion, how many labels to order, whether he is
  short on a label code like 220075, or mentions Cohesion, label stock or a label PO.
allowed-tools: Bash, Read
---

# Cohesion Label Reorder

Answers "which Cohesion labels am I low on, and how many should I order?" from Cin7, the
same way every time. The saved Cohesion page, with the full label list and quote pricing, is
https://claude.ai/artifact/HfBftvwxFcrMTU7dSHXkVv.

## Step 1 — Pull stock and usage from Cin7

Run `python3 -I scripts/label_stock.py --out <scratchpad>/label_stock.json` from this skill's
folder. It is read-only. It reads every product in Cin7 and keeps those whose supplier is
Cohesion Labels & Labelling, plus everything in `reference/label-map.csv`.

Do not search Cin7 by name. The Name filter only matches the start of a name, and that is how
"Brand Labels Supernatural (Black)" (Cohesion 220075) got missed the first time.

If `CIN7_ACCOUNT_ID` / `CIN7_APP_KEY` are not set, use the fallback in `reference/cin7.md`.

## Step 2 — Apply the rules

The script does this. Check its output against these rules rather than redoing the maths:

- **Usage** is sales, multi-sales and finished-goods movements out. The monthly rate is the
  busier of the last 6 months and the last 12 months.
- **Low** means under 6 months of cover at that rate.
- **Order quantity** is enough for 7 months, minus what is on hand and on order, rounded up
  to whole rolls.
- **Not moving** means no use in 12 months. These are never reordered; list them separately.
- **Never stocked** means no movements and nothing on hand (the can labels in Oct 2026).
  Flag these. They are either a missing order or a delivery not entered in Cin7.
- **Price breaks** come from `reference/quotes.json`. Quote 719491 (all 50mm rounds) is
  priced on the total rolls across every variant, and 24 rolls cost about the same as 4 or 8.
  So the 50mm order is filled to the cheapest sensible tier. Extra rolls go one at a time to
  whichever 50mm label runs out soonest.

The 7- and 6-month figures assume Cohesion takes about a month to print and deliver. If David
gives a lead time, change them with `--order-up-to` and `--order-below`.

## Step 3 — Show the reorder list

One table, soonest-to-run-out first:

| Label | SKU | On hand | Uses per month | Order |

Under it, give:
- the 50mm price-break line (rolls, price per roll, total, $28 freight, ex GST)
- never-stocked labels
- every `check` line from the script

Name the SKU and the PO in each flag. Standing flags as of Oct 2026:
- 9782701's unit says Roll of 500. Cohesion's quote confirms 50mm rolls are 250.
- PO-83076's sushi quantities were entered as label counts, not rolls.

Keep the answer short. Leave out cover months unless asked, because David asked for the
table without the "lasts about" column.

## Step 4 — Draft PO (approval gate)

Offer it once, stating exactly what will be created:

> Draft PO to Cohesion Labels & Labelling in Cin7: 9 lines, 48 rolls, 50mm lines at $20.90
> a roll ($501.60 + $28 freight). It stays a draft and is not authorised or sent. Create it?

Only on a yes, create the purchase in Cin7 as a draft (see `reference/cin7.md`). Never
authorise, receive or email it. Quantities are in rolls, never in labels. If the API call
fails, give David the table to key in by hand and say what failed.

## Step 5 — Keep the reference files current

When David uploads a new Cohesion product list or quote:
- Add it to the saved page, keeping the same link.
- Add the quote's tiers to `reference/quotes.json`.
- Add any new codes to `reference/label-map.csv`.

If the script reports a Cohesion product that isn't in the label map, ask which Cohesion code
it is before adding it.

## What not to do

- **Don't invent prices.** Only quote 719491 is saved. The sushi, bowl, pots, salad,
  sandwich and can quotes (720116–720120, 721841) aren't, so say their price is unknown.
- **Don't reorder not-moving labels**, even if Cin7's reorder point is triggered. Cin7's
  points are mostly 1 roll and are not used here.
- **Don't treat the Fast Printing app labels as Cohesion's.** Cin7 has 13424292 and 13424325
  under Fast Printing, so they're excluded from the label map.

## Reference files

- `reference/label-map.csv`: each Cohesion code with its Cin7 SKU, how sure the match is,
  and whether it's included
- `reference/quotes.json`: Cohesion quote price breaks used for top-ups
- `reference/cin7.md`: API notes, the draft PO call, and the no-Cin7 fallback
- `scripts/label_stock.py`: the read-only stock and reorder calculation
