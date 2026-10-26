#!/usr/bin/env python3
"""
azure_prices.py - fetch REAL Azure list prices from the public Azure Retail
Prices API (https://prices.azure.com/api/retail/prices, no auth) and save a
timestamped snapshot.  scale_plan.py only uses prices that came from here;
nothing is hard-coded or invented.

Usage:  python azure_prices.py [--region eastus] [--vm Standard_D32as_v5]
Output: plans/azure_prices_<region>.json  (every meter keeps its query URL,
        meterId, effectiveStartDate and the fetch timestamp)
"""
from __future__ import annotations
import argparse, datetime as dt, json, os, sys, urllib.parse
import requests

API = "https://prices.azure.com/api/retail/prices"
HERE = os.path.dirname(os.path.abspath(__file__))
ADLS = "Azure Data Lake Storage Gen2 Hierarchical Namespace"


def query(flt: str):
    url, params, items = API, {"$filter": flt}, []
    first = API + "?" + urllib.parse.urlencode({"$filter": flt})
    while url:
        r = requests.get(url, params=params, timeout=60)
        r.raise_for_status()
        j = r.json(); items += j["Items"]; url = j.get("NextPageLink"); params = None
    return first, items


def slim(i):
    keep = ("productName", "skuName", "meterName", "meterId", "unitOfMeasure", "tierMinimumUnits",
            "retailPrice", "currencyCode", "armRegionName", "effectiveStartDate", "type", "armSkuName")
    return {k: i.get(k) for k in keep}


def fetch(region: str, vm: str):
    fetched = dt.datetime.now(dt.timezone.utc).isoformat()
    out = {"source": API, "fetched_utc": fetched, "region": region, "vm_sku": vm, "groups": {}}
    groups = {
        "adls_storage": f"serviceName eq 'Storage' and armRegionName eq '{region}' and productName eq '{ADLS}' "
                        f"and (skuName eq 'Hot LRS' or skuName eq 'Cool LRS' or skuName eq 'Cold LRS' or skuName eq 'Archive LRS')",
        "vm": f"serviceName eq 'Virtual Machines' and armRegionName eq '{region}' and armSkuName eq '{vm}' and priceType eq 'Consumption'",
        "bandwidth": f"serviceName eq 'Bandwidth' and armRegionName eq '{region}'",
        "acr": f"serviceName eq 'Container Registry' and armRegionName eq '{region}'",
    }
    for g, flt in groups.items():
        url, items = query(flt)
        out["groups"][g] = {"query_url": url, "items": [slim(i) for i in items if i.get("type") == "Consumption"]}
    return out


def pick(snapshot, group, *, sku=None, meter=None, product_contains=None, exclude_product=None):
    """Return list of matching meters (all tiers) or [] - never a made-up number."""
    res = []
    for i in snapshot["groups"][group]["items"]:
        if sku and i["skuName"] != sku: continue
        if meter and i["meterName"] != meter: continue
        if product_contains and product_contains not in i["productName"]: continue
        if exclude_product and exclude_product in i["productName"]: continue
        res.append(i)
    return sorted(res, key=lambda x: x["tierMinimumUnits"])


def tiered_cost(meters, units):
    """Apply graduated tiers (tierMinimumUnits) to `units`. Returns (cost, breakdown)."""
    if not meters:
        return None, []
    cost, parts = 0.0, []
    for n, m in enumerate(meters):
        lo = m["tierMinimumUnits"]; hi = meters[n + 1]["tierMinimumUnits"] if n + 1 < len(meters) else float("inf")
        q = max(0.0, min(units, hi) - lo)
        if q > 0:
            cost += q * m["retailPrice"]; parts.append({"from": lo, "units": q, "unit_price": m["retailPrice"]})
    return cost, parts


def load_or_fetch(region="eastus", vm="Standard_D32as_v5", refresh=False):
    fn = os.path.join(HERE, "plans", f"azure_prices_{region}.json")
    if not refresh and os.path.exists(fn):
        s = json.load(open(fn))
        if s.get("vm_sku") == vm:
            return s
    s = fetch(region, vm)
    os.makedirs(os.path.dirname(fn), exist_ok=True)
    json.dump(s, open(fn, "w"), indent=1)
    return s


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--region", default="eastus"); ap.add_argument("--vm", default="Standard_D32as_v5")
    a = ap.parse_args()
    s = load_or_fetch(a.region, a.vm, refresh=True)
    for g, v in s["groups"].items():
        print(f"{g}: {len(v['items'])} meters")
    print("fetched", s["fetched_utc"])
