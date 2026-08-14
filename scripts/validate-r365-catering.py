#!/usr/bin/env python3
"""Validate the R365 catering source against the hand-keyed Sales & Trends history.

This is the gate the P&L version failed. Before this source is allowed to write
anything, it must reproduce numbers a person entered by hand and it did not
author.

Columns are resolved from each tab's OWN header row. The catering block is not
in the same order on every tab -- BM is "America To Go" on Cherrywood and Arbor,
"Try Hungry" on Round Rock, "Event" on TsoCo and Menchaca -- so comparing by
column letter produces nonsense.

  scripts/validate-r365-catering.py --start 2025-06-01 --end 2026-07-31 --refresh
"""

import argparse
import collections
import datetime as dt
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import r365_catering as rc
import sheets_io

CACHE = "/tmp/r365_catering_cache.json"
OUT = "/tmp/r365_validation.json"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--refresh", action="store_true", help="re-pull R365 instead of using the cache")
    args = ap.parse_args()

    start = dt.date.fromisoformat(args.start)
    end = dt.date.fromisoformat(args.end)

    numbers = sorted({n for nums in rc.HEADER_ACCOUNTS.values() for n in nums})

    if args.refresh or not os.path.exists(CACHE):
        headers = rc.auth_headers()
        records, warnings = rc.fetch_lines(numbers, start, end, headers)
        with open(CACHE, "w") as fh:
            json.dump(records, fh)
        print(f"cached {len(records)} records -> {CACHE}")
    else:
        records = json.load(open(CACHE))
        warnings = []
        print(f"using cached {len(records)} records from {CACHE} (--refresh to re-pull)")

    months = rc.month_range(start, end)
    agg = rc.aggregate(records)
    warnings, notes = rc.verify_completeness(records, months, list(warnings))
    for w in warnings:
        print(f"WARNING {w}", file=sys.stderr)

    sheets_io.load_env()
    service = sheets_io.sheets_service()
    sheet_id = sheets_io.spreadsheet_id()
    tabs = sorted(rc.STORE_TABS.values())
    sheet = sheets_io.read_tabs(service, sheet_id, tabs)

    results = []
    layouts = {}
    for tab in tabs:
        rows = sheet.get(tab) or []
        if not rows:
            continue
        writable, skipped, unknown = rc.resolve_columns(rows[0])
        layouts[tab] = (writable, skipped, unknown)
        row_of = sheets_io.row_index_by_label(rows)
        for month in months:
            label = rc.month_label(month)
            if label not in row_of:
                continue
            row = rows[row_of[label] - 1]
            for header, (letter, _accounts) in sorted(writable.items()):
                idx = rc.column_index(letter)
                raw = row[idx] if len(row) > idx else ""
                r365 = agg.get(tab, {}).get(label, {}).get(header)
                if raw in ("", None) and r365 is None:
                    continue
                try:
                    sheet_val = float(str(raw).replace("$", "").replace(",", "")) if raw not in ("", None) else None
                except ValueError:
                    sheet_val = None

                if sheet_val is None:
                    bucket = "sheet_blank"
                elif r365 is None:
                    bucket = "r365_missing"
                elif abs(sheet_val - r365) < 0.01:
                    bucket = "exact"
                elif abs(sheet_val - r365) < 1.01:
                    bucket = "rounding"
                else:
                    bucket = "mismatch"
                results.append({"tab": tab, "label": label, "header": header,
                                "column": letter, "sheet": sheet_val,
                                "r365": r365, "bucket": bucket})

    print("\n=== per-tab catering layout (from each tab's own header row) ===")
    for tab in tabs:
        if tab not in layouts:
            continue
        writable, skipped, unknown = layouts[tab]
        cols = "  ".join(f"{l}={h}" for h, (l, _) in sorted(writable.items(), key=lambda kv: kv[1][0]))
        print(f"  {tab[:26]:28} {cols}")
        if unknown:
            print(f"  {'':28} UNKNOWN: {unknown}")

    counts = collections.Counter(r["bucket"] for r in results)
    print(f"\n=== {len(results)} comparable cells ===")
    for bucket in ("exact", "rounding", "mismatch", "sheet_blank", "r365_missing"):
        if counts[bucket]:
            print(f"  {bucket:14} {counts[bucket]}")

    print("\n=== agreement by column (exact + rounding) ===")
    per_header = collections.defaultdict(lambda: [0, 0])
    for r in results:
        if r["bucket"] in ("exact", "rounding", "mismatch"):
            per_header[r["header"]][1] += 1
            if r["bucket"] != "mismatch":
                per_header[r["header"]][0] += 1
    for header in sorted(per_header):
        ok, total = per_header[header]
        pct = (100.0 * ok / total) if total else 0.0
        print(f"  {header[:24]:26} {ok:3}/{total:<3} {pct:5.1f}%")

    for bucket, title in (("mismatch", "MISMATCHES"), ("r365_missing", "IN SHEET, NOT IN R365")):
        rows_ = [r for r in results if r["bucket"] == bucket]
        if not rows_:
            continue
        print(f"\n=== {title} ({len(rows_)}) ===")
        for r in sorted(rows_, key=lambda x: (x["tab"], x["label"])):
            sv = f"{r['sheet']:,.2f}" if r["sheet"] is not None else "blank"
            rv = f"{r['r365']:,.2f}" if r["r365"] is not None else "none"
            print(f"  {r['tab'][:22]:24} {r['label']:9} {r['column']:3} {r['header'][:20]:22} "
                  f"sheet={sv:>12} r365={rv:>12}")

    with open(OUT, "w") as fh:
        json.dump(results, fh, indent=1)
    print(f"\nwrote {OUT}")


if __name__ == "__main__":
    main()
