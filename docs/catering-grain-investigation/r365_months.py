#!/usr/bin/env python3
"""Confirm across ALL disagreements: does R365 summed over the CALENDAR MONTH
match the sheet cell?

Cherrywood 3.2026 Lunchdrop: sheet 6,732.00 vs R365 calendar March 6,731.85.
That is a rounding-only difference. If this holds broadly, the sheet's catering
columns are CALENDAR-MONTH figures and the fiscal-period P&L is simply the
wrong grain -- my extraction, not the sheet, is what is wrong.
"""
import base64, json, urllib.request, urllib.parse, datetime as dt, calendar
from collections import defaultdict

BASE = "https://odata.restaurant365.net/api/v2/views/"
u = open("/tmp/.r365u").read().strip()
p = open("/tmp/.r365p").read().strip()
tok = base64.b64encode(("tsochinese" + chr(92) + u + ":" + p).encode()).decode()
PAGE = 5000


def get_all(entity, params):
    out, skip = [], 0
    while True:
        q = dict(params); q["$top"] = str(PAGE); q["$skip"] = str(skip)
        req = urllib.request.Request(
            BASE + entity + "?" + urllib.parse.urlencode(q),
            headers={"Authorization": "Basic " + tok, "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=300) as r:
            b = json.loads(r.read().decode())["value"]
        out.extend(b)
        if len(b) < PAGE:
            return out
        skip += PAGE


ACCTS = {"4310", "4311", "4312", "4313", "4420", "4440", "4441", "4445", "4130"}
accounts = get_all("GlAccount", {"$select": "glAccountId,glAccountNumber,name"})
want = {a["glAccountId"]: str(a["glAccountNumber"])
        for a in accounts if str(a.get("glAccountNumber") or "") in ACCTS}
loc = {l["locationId"]: l["name"]
       for l in get_all("Location", {"$select": "locationId,name"})}

# Disagreements span 7.2025 .. 3.2026 -> pull business dates Jun 2025..Apr 2026.
detail = []
cur, end = dt.date(2025, 6, 1), dt.date(2026, 5, 15)
while cur <= end:
    stop = min(cur + dt.timedelta(days=24), end)
    rows = get_all("TransactionDetail", {
        "$select": "transactionId,locationId,glAccountId,credit,debit,createdOn",
        "$filter": f"createdOn ge {cur.isoformat()}T00:00:00Z and "
                   f"createdOn le {stop.isoformat()}T23:59:59Z"})
    detail.extend([d for d in rows if d.get("glAccountId") in want])
    print(f"  TD {cur}: {len(rows):>6} -> running {len(detail)}", flush=True)
    cur = stop + dt.timedelta(days=1)

txn = {}
cur, end = dt.date(2025, 5, 1), dt.date(2026, 5, 15)
while cur <= end:
    stop = min(cur + dt.timedelta(days=24), end)
    for t in get_all("Transaction", {
            "$select": "transactionId,date",
            "$filter": f"date ge {cur.isoformat()}T00:00:00Z and "
                       f"date le {stop.isoformat()}T23:59:59Z"}):
        txn[t["transactionId"]] = t["date"][:10]
    cur = stop + dt.timedelta(days=1)

agg = defaultdict(float)
for d in detail:
    bd = txn.get(d["transactionId"])
    if not bd:
        continue
    agg[(loc.get(d["locationId"], "?"), want[d["glAccountId"]], bd[:7])] += \
        (d.get("credit") or 0) - (d.get("debit") or 0)

json.dump({"|".join(k): v for k, v in agg.items()},
          open("/tmp/p03test/r365_by_month.json", "w"), indent=1)
print(f"\nwrote {len(agg)} store/account/month buckets", flush=True)
