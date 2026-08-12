#!/usr/bin/env python3
"""Pull Tso revenue from R365 SalesDetail for a date window, by location and day.

Usage:
    export OP_SERVICE_ACCOUNT_TOKEN=$(cat ~/.op_service_account_token)
    python3 r365_sales_window.py --start 2026-07-29 --end 2026-08-03

--end is INCLUSIVE (the script converts to the exclusive OData boundary).

Encodes the hard-won R365 quirks: quote-not-quote_plus encoding, no $select,
Z-suffixed date literals, manual $skip pagination, dedupe by salesdetailID, and
MANDATORY client-side date revalidation (the server returns out-of-range rows on
deep $skip pages). See references/salesdetail-odata-quirks.md.
"""
import argparse
import base64
import datetime as dt
import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from collections import defaultdict

BASE = "https://odata.restaurant365.net/api/v2/views/"
Q = urllib.parse.quote  # NEVER quote_plus - '+' spaces make $filter silently match nothing


def op_field(label):
    return subprocess.check_output(
        ["op", "item", "get", "Tsora Restaurant365", "--vault",
         "Administrative Assistants", "--fields", f"label={label}", "--reveal"],
        text=True, timeout=45).strip()


def auth_token():
    tok_path = os.path.expanduser("~/.op_service_account_token")
    os.environ.setdefault("OP_SERVICE_ACCOUNT_TOKEN", open(tok_path).read().strip())
    password = None
    for label in ("NewPassword", "password"):  # NewPassword is the live one
        try:
            v = op_field(label)
            if v:
                password = v
                break
        except subprocess.SubprocessError:
            continue
    if not password:
        sys.exit("Could not read R365 password from 1Password")
    user = op_field("username")
    tok = base64.b64encode(f"tsochinese\\{user}:{password}".encode()).decode()
    del user, password
    return tok


def get(url, token, tries=5):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"Authorization": "Basic " + token})
            with urllib.request.urlopen(req, timeout=180) as r:
                return json.load(r)
        except Exception as e:  # transient TargetInvocationException 500s are common
            last = e
            time.sleep(3 * (i + 1))
    raise last


def location_map(token):
    locs = {}
    for v in get(BASE + "Location?$top=200", token).get("value", []):
        lid = v.get("locationId") or v.get("locationID") or v.get("id")
        if lid:
            locs[str(lid)] = v.get("name") or v.get("locationName") or str(lid)
    return locs


def fetch_day(day, token):
    """Yield raw SalesDetail rows for one calendar day (UTC)."""
    nxt = day + dt.timedelta(days=1)
    skip = 0
    while True:
        params = {
            "$top": "1000",
            "$skip": str(skip),
            "$orderby": "date,salesdetailID",  # deterministic + unique tiebreak
            # NO $select - it silently returns 0 rows at large $top
            "$filter": (f"date ge {day.isoformat()}T00:00:00Z and "
                        f"date lt {nxt.isoformat()}T00:00:00Z"),
        }
        data = get(BASE + "SalesDetail?" + urllib.parse.urlencode(params, quote_via=Q), token)
        vals = data.get("value", [])
        yield from vals
        if len(vals) < 1000:
            return
        skip += 1000


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", required=True, help="YYYY-MM-DD, inclusive")
    ap.add_argument("--end", required=True, help="YYYY-MM-DD, INCLUSIVE")
    a = ap.parse_args()
    start = dt.date.fromisoformat(a.start)
    end = dt.date.fromisoformat(a.end)
    if end < start:
        sys.exit("--end precedes --start")
    if (end - start).days + 1 > 31:
        sys.exit("R365 windows must be under 31 days; split the request")

    token = auth_token()
    locs = location_map(token)

    by_loc = defaultdict(float)
    by_day = defaultdict(float)
    by_loc_day = defaultdict(float)
    by_acct = defaultdict(float)
    seen = set()
    rejected_n = 0
    rejected_amt = 0.0
    voided = 0

    day = start
    while day <= end:
        n = 0
        for r in fetch_day(day, token):
            rid = r.get("salesdetailID")
            if rid in seen:
                continue
            seen.add(rid)
            stamp = str(r.get("date") or "")[:10]
            # MANDATORY: server returns out-of-range rows on deep $skip pages
            if not (a.start <= stamp <= a.end):
                rejected_n += 1
                rejected_amt += float(r.get("amount") or 0)
                continue
            if r.get("void") is True:
                voided += 1
                continue
            amt = float(r.get("amount") or 0)
            loc = locs.get(str(r.get("location")), str(r.get("location")))
            by_loc[loc] += amt
            by_day[stamp] += amt
            by_loc_day[(loc, stamp)] += amt
            by_acct[str(r.get("salesAccount"))] += amt
            n += 1
        print(f"  {day}: {n} in-range rows", file=sys.stderr, flush=True)
        day += dt.timedelta(days=1)

    total = sum(by_loc.values())
    # three-way reconciliation
    assert abs(total - sum(by_day.values())) < 0.01, "location vs day totals disagree"
    assert abs(total - sum(by_loc_day.values())) < 0.01, "loc-day grid disagrees"

    print(f"\nWindow {a.start} .. {a.end} (inclusive, UTC-bucketed)")
    print(f"unique rows fetched: {len(seen)} | voided skipped: {voided}")
    print(f"OUT-OF-RANGE REJECTED: {rejected_n} rows / ${rejected_amt:,.2f}")
    print(f"\nTOTAL REVENUE: ${total:,.2f}\n")
    print("BY LOCATION")
    for k, v in sorted(by_loc.items(), key=lambda x: -x[1]):
        print(f"  {k}: ${v:,.2f}")
    print("\nBY DAY")
    for k, v in sorted(by_day.items()):
        print(f"  {k}: ${v:,.2f}")
    print("\nTOP SALES ACCOUNTS")
    for k, v in sorted(by_acct.items(), key=lambda x: -x[1])[:15]:
        print(f"  {k}: ${v:,.2f}")
    print("\nLOCATION x DAY")
    for (l, d), v in sorted(by_loc_day.items()):
        print(f"  {l} | {d} | ${v:,.2f}")


if __name__ == "__main__":
    main()
