#!/usr/bin/env python3
"""Validate the R365 catering source against the hand-keyed Sales & Trends history.

This is the gate the P&L version failed. Before this source is allowed to write
anything, it has to reproduce history it did not author.

Reports, per column, how many populated historical cells the R365
calendar-month aggregate reproduces -- exactly, within a dollar (history was
keyed to whole dollars), and outright mismatches.
"""
import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import r365_catering as rc
import catering_pl as cp

REPO = Path(__file__).resolve().parent.parent


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--cache", default="/tmp/r365_catering_cache.json")
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--audit", action="store_true",
                    help="pull every catering account, not just mapped ones")
    args = ap.parse_args()

    start = dt.date.fromisoformat(args.start)
    end = dt.date.fromisoformat(args.end)

    numbers = rc.AUDIT_ACCOUNTS if args.audit else sorted(
        {n for nums in rc.COLUMN_ACCOUNTS.values() for n in nums})

    cache = Path(args.cache)
    if cache.exists() and not args.refresh:
        records = json.loads(cache.read_text())
        print(f"cache: {len(records)} lines from {cache}", file=sys.stderr)
        warnings = []
    else:
        headers = rc.auth_headers()
        records, warnings = rc.fetch_lines(numbers, start, end, headers)
        cache.write_text(json.dumps(records))
        print(f"fetched {len(records)} lines -> {cache}", file=sys.stderr)

    months = rc.month_range(start, end)
    agg = rc.aggregate(records)
    warnings = rc.verify_completeness(records, months, list(warnings))

    for w in warnings:
        print(f"WARNING {w}", file=sys.stderr)

    # Live sheet, read-only.
    import sheets_io

    sheets_io.load_env()
    service = sheets_io.sheets_service()
    sheet_id = sheets_io.spreadsheet_id()
    tabs = sorted(rc.STORE_TABS.values())
    sheet = sheets_io.read_tabs(service, sheet_id, tabs)

    stats = {}
    details = []
    for tab in tabs:
        rows = sheet.get(tab) or []
        row_of = {}
        for i, row in enumerate(rows):
            key = str(row[0]).strip() if row else ""
            if key and key not in row_of:
                row_of[key] = i
        for month in months:
            label = rc.month_label(month)
            if label not in row_of:
                continue
            row = rows[row_of[label]]
            for column in sorted(rc.COLUMN_ACCOUNTS):
                idx = cp.column_index(column)
                raw = row[idx] if len(row) > idx else ""
                r365 = agg.get(tab, {}).get(label, {}).get(column)
                if raw in ("", None):
                    continue
                try:
                    sheet_value = float(str(raw).replace("$", "").replace(",", ""))
                except ValueError:
                    continue
                if r365 is None:
                    bucket = "r365_missing"
                    delta = None
                else:
                    delta = round(r365 - sheet_value, 2)
                    if abs(delta) < 0.005:
                        bucket = "exact"
                    elif abs(delta) < 1.0:
                        bucket = "within_dollar"
                    elif abs(delta) < 5.0:
                        bucket = "within_five"
                    else:
                        bucket = "mismatch"
                stats.setdefault(column, {}).setdefault(bucket, 0)
                stats[column][bucket] += 1
                details.append({"tab": tab, "label": label, "column": column,
                                "sheet": sheet_value, "r365": r365,
                                "delta": delta, "bucket": bucket})

    print(f"\nWindow {args.start} .. {args.end}   months={len(months)}")
    print("\nPer-column agreement against hand-keyed history:")
    order = ["exact", "within_dollar", "within_five", "mismatch", "r365_missing"]
    for column in sorted(stats):
        total = sum(stats[column].values())
        parts = "  ".join(f"{k}={stats[column].get(k, 0)}" for k in order)
        good = stats[column].get("exact", 0) + stats[column].get("within_dollar", 0)
        print(f"  {column}: n={total:3}  {parts}   agree={good}/{total} "
              f"({100.0 * good / total:.0f}%)" if total else f"  {column}: no data")

    bad = [d for d in details if d["bucket"] in ("mismatch", "r365_missing")]
    if bad:
        print(f"\n{len(bad)} cell(s) that do NOT agree:")
        for d in sorted(bad, key=lambda x: (x["column"], x["tab"], x["label"]))[:40]:
            r = f"{d['r365']:,.2f}" if d["r365"] is not None else "MISSING"
            dl = f"{d['delta']:>12,.2f}" if d["delta"] is not None else " " * 12
            print(f"  {d['column']} {d['tab'][:24]:26} {d['label']:9} "
                  f"sheet={d['sheet']:>12,.2f}  r365={r:>14}  delta={dl}")

    gaps = rc.coverage_gaps(records, months)
    if gaps:
        print(f"\nStore-months with NO catering journal ({len(gaps)}) -- "
              f"missing journal or genuinely zero, needs a human:")
        for month, store in gaps[:20]:
            print(f"  {month}  {store}")

    un = rc.unapproved(records)
    if un:
        print(f"\n{len(un)} unapproved journal line(s) included -- reported, not hidden")

    json.dump(details, open("/tmp/r365_validation.json", "w"))
    print("\ndetail -> /tmp/r365_validation.json")


if __name__ == "__main__":
    main()
