# Cin7 notes for label reordering

## API

- Base URL: `https://inventory.dearsystems.com/ExternalApi/v2`. Send the headers
  `api-auth-accountid: $CIN7_ACCOUNT_ID` and `api-auth-applicationkey: $CIN7_APP_KEY`.
- The limit is 60 calls a minute. The script waits 1.1 s between calls, so a full run takes
  about 2 minutes.
- `product?Name=` matches the **start** of the name only ("label" misses "Brand Labels…").
  Page through `product?Limit=1000&IncludeSuppliers=true` and filter on supplier instead.
- `ref/productavailability?Name=` matches anywhere in the name, so the same item can come back
  twice. Query by `Sku=` and keep exact SKU matches only.
- `product?Sku=<sku>&IncludeMovements=true` returns the stock movement history used for usage.
- Supplier name in Cin7: `Cohesion Labels & Labelling`.
- Stock is held at WA Warehouse (Marketing Room shelves).

## Draft purchase order (only after David says yes)

1. Get the supplier's ID from `supplier?Name=Cohesion`.
2. `POST purchase` with:
   - `SupplierID`
   - `Location: "WA Warehouse"`
   - `Approach: "INVOICE"`
   - `TaxRule` copied from Cohesion's supplier record
3. `POST purchase/order` with:
   - `TaskID` from step 2
   - `Status: "DRAFT"`
   - one line per SKU: `ProductID`, `SKU`, `Name`, `Quantity` in rolls, `Price` in rolls

Leave the order in DRAFT. Use the quote's price per roll for 50mm lines. For other lines, use
the product's last purchase cost, or leave the price blank. If any call errors, stop and give
David the table to enter by hand.

## Fallback when Cin7 credentials are missing

Ask David for two exports from Cin7:
- the **Product Availability** report
- a **Stock movements** or **Sales by product** report for the last 12 months

These can be CSV or XLSX. Filter them to Cohesion labels using `label-map.csv` and apply the
same rules as `label_stock.py`. Say plainly that this run used the exports he provided.

## Known data issues (Oct 2026)

- 9782701 "Brand Labels Supernatural (Black) – Roll of 250" has its unit set to "Roll of 500".
  The quote confirms 250 labels a roll.
- 97827011111 "Brand Labels Supernatural (Black) – Bndl/Roll of 500" is an old duplicate,
  unused since 2021.
- PO-83076 (15 May 2026) has its sushi quantities in labels (1,000, 3,000, 5,000) rather than
  rolls.
- The can labels (LBLCAN…) were set up on 16 Sep 2026 and have never been received.
