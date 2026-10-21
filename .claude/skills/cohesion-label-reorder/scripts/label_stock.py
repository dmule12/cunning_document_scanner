#!/usr/bin/env python3
"""Pull Cohesion label stock and usage from Cin7 Core and work out what to reorder.

Reads CIN7_ACCOUNT_ID and CIN7_APP_KEY from the environment. Read-only: it never
writes to Cin7. Prints a summary table and writes full results as JSON.

Usage:
  python3 label_stock.py [--order-below 6] [--order-up-to 7] [--today YYYY-MM-DD] [--out result.json]
"""
import argparse
import csv
import json
import math
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta

BASE = "https://inventory.dearsystems.com/ExternalApi/v2"
HERE = os.path.dirname(os.path.abspath(__file__))
REF = os.path.join(HERE, "..", "reference")
SUPPLIER = "Cohesion Labels & Labelling"
USAGE_TYPES = ("Sale", "SaleMultiple", "Finished Goods")


def get(path, **params):
    url = f"{BASE}/{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={
        "api-auth-accountid": os.environ["CIN7_ACCOUNT_ID"],
        "api-auth-applicationkey": os.environ["CIN7_APP_KEY"],
    })
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                time.sleep(1.1)  # Cin7 Core allows 60 calls a minute
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code in (429, 503) and attempt < 4:
                time.sleep(15 * (attempt + 1))
                continue
            raise


def load_map():
    with open(os.path.join(REF, "label-map.csv"), newline="") as f:
        return list(csv.DictReader(f))


def pick_tier(tiers, needed):
    """Cheapest tier that covers `needed` rolls; among tiers within 2% of that
    price, take the one with the most rolls (e.g. 24 rolls for $501.60 beats 8 for $500)."""
    fits = [t for t in tiers if t["rolls"] >= needed]
    if not fits:
        return None
    cheapest = min(t["total"] for t in fits)
    return max((t for t in fits if t["total"] <= cheapest * 1.02), key=lambda t: t["rolls"])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--order-below", type=float, default=6, help="reorder when cover is under this many months")
    ap.add_argument("--order-up-to", type=float, default=7, help="order enough for this many months")
    ap.add_argument("--today", default=datetime.now().strftime("%Y-%m-%d"))
    ap.add_argument("--out", default="label_stock.json")
    a = ap.parse_args()
    today = datetime.fromisoformat(a.today)

    lmap = load_map()
    by_sku = {r["cin7_sku"]: r for r in lmap if r["cin7_sku"] and r["include"] == "yes"}

    # Every product, so nothing is missed because of how it is named in Cin7.
    products, page = [], 1
    while True:
        batch = get("product", Limit=1000, Page=page, IncludeSuppliers="true").get("Products", [])
        products += batch
        if len(batch) < 1000:
            break
        page += 1
    cohesion = {p["SKU"] for p in products
                if any((s.get("SupplierName") or "") == SUPPLIER for s in p.get("Suppliers") or [])}
    skus = sorted(cohesion | set(by_sku))
    unmapped_cohesion = sorted(cohesion - set(by_sku))

    rows = []
    for sku in skus:
        p = (get("product", Sku=sku, IncludeMovements="true", IncludeReorderLevels="true").get("Products") or [None])[0]
        if not p:
            rows.append({"sku": sku, "status": "missing", "flags": ["SKU in label map not found in Cin7"]})
            continue
        avail = get("ref/productavailability", Sku=sku, Limit=100).get("ProductAvailabilityList", [])
        avail = [x for x in avail if x["SKU"] == sku]  # the filter is loose; keep exact matches only
        on_hand = sum(x.get("OnHand") or 0 for x in avail)
        on_order = sum(x.get("OnOrder") or 0 for x in avail)

        mv = p.get("Movements") or []
        out = [m for m in mv if m["Type"] in USAGE_TYPES and m["Quantity"] < 0]
        used = lambda days: -sum(m["Quantity"] for m in out if datetime.fromisoformat(m["Date"]) >= today - timedelta(days=days))
        u6, u12 = used(182), used(365)
        rate = max(u6 / 6, u12 / 12)  # the busier of the two recent rates
        buys = sorted((m for m in mv if m["Type"] == "Purchase"), key=lambda m: m["Date"])
        last_buy = buys[-1] if buys else None

        flags = []
        name, uom = p["Name"], p.get("UOM") or ""
        for n in ("250", "500", "750"):
            if f"of {n}" in name.lower() or f"({n})" in name:
                if n not in uom:
                    flags.append(f"name says {n} but unit is '{uom}'")
        if last_buy and last_buy["Quantity"] >= 100:
            flags.append(f"last purchase {last_buy['Number']} entered as {last_buy['Quantity']:.0f} (looks like labels, not rolls)")

        if not mv and on_hand == 0:
            status, order = "never stocked", 0
        elif rate == 0:
            status, order = "not moving", 0
        elif on_hand / rate < a.order_below:
            status, order = "order", max(0, int(math.ceil(rate * a.order_up_to) - on_hand - on_order))
        else:
            status, order = "ok", 0

        m = by_sku.get(sku, {})
        rows.append({
            "sku": sku, "name": name, "uom": uom,
            "cohesion_code": m.get("cohesion_code", ""), "quote": m.get("quote", ""),
            "on_hand": on_hand, "on_order": on_order, "used_6m": u6, "used_12m": u12,
            "per_month": round(rate, 2), "cover_months": round(on_hand / rate, 1) if rate else None,
            "last_used": max((x["Date"][:10] for x in out), default=None),
            "last_purchase": {"date": last_buy["Date"][:10], "qty": last_buy["Quantity"], "po": last_buy["Number"]} if last_buy else None,
            "status": status, "order": order, "order_before_price_breaks": order, "flags": flags,
        })

    # Price breaks: top an order up to the cheapest sensible tier, giving the
    # extra rolls one at a time to whichever label on that quote runs out soonest.
    with open(os.path.join(REF, "quotes.json")) as f:
        quotes = json.load(f)
    price_notes = []
    for qno, q in quotes.items():
        group = [r for r in rows if r.get("quote") == qno and r.get("per_month")]
        needed = sum(r["order"] for r in group)
        if not needed:
            continue
        tier = pick_tier(q["tiers"], needed)
        if not tier:
            continue
        for _ in range(tier["rolls"] - needed):
            r = min(group, key=lambda r: (r["on_hand"] + r["on_order"] + r["order"]) / r["per_month"])
            r["order"] += 1
        price_notes.append({"quote": qno, "title": q["title"], "rolls_needed": needed, "rolls_ordered": tier["rolls"],
                            "price_per_roll": tier["price_per_roll"], "total": tier["total"], "freight": q["freight"]})

    result = {"today": a.today, "rows": rows, "price_breaks": price_notes,
              "cohesion_skus_not_in_label_map": unmapped_cohesion}
    with open(a.out, "w") as f:
        json.dump(result, f, indent=1)

    order_rows = [r for r in rows if r.get("order")]
    print(f"{'SKU':11} {'Label':46} {'OnHand':>6} {'/mo':>5} {'Cover':>5} {'Order':>5}")
    for r in sorted(order_rows, key=lambda r: r["cover_months"]):
        extra = r["order"] - r["order_before_price_breaks"]
        print(f"{r['sku']:11} {r['name'][:46]:46} {r['on_hand']:6.0f} {r['per_month']:5.2f} {r['cover_months']:5.1f} {r['order']:5d}"
              + (f"  (+{extra} for price break)" if extra else ""))
    for n in price_notes:
        print(f"Quote {n['quote']}: {n['rolls_needed']} rolls needed -> order {n['rolls_ordered']} at ${n['price_per_roll']:.2f}/roll = ${n['total']:.2f} + ${n['freight']:.2f} freight")
    for s in ("never stocked", "missing"):
        hit = [r["sku"] for r in rows if r["status"] == s]
        if hit:
            print(f"{s}: {', '.join(hit)}")
    for r in rows:
        for fl in r.get("flags", []):
            print(f"check {r['sku']}: {fl}")
    if unmapped_cohesion:
        print("Cohesion products in Cin7 not in the label map:", ", ".join(unmapped_cohesion))
    print(f"Full results: {a.out}")


if __name__ == "__main__":
    if not (os.environ.get("CIN7_ACCOUNT_ID") and os.environ.get("CIN7_APP_KEY")):
        sys.exit("CIN7_ACCOUNT_ID and CIN7_APP_KEY are not set; use the CSV fallback in SKILL.md.")
    main()
