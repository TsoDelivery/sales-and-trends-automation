#!/usr/bin/env python3
"""Write catering revenue from R365 into the Sales & Trends catering columns.

USAGE
  # See what would change, touch nothing (default):
  scripts/ingest-catering-r365.py --month 2026-07

  # Actually write:
  scripts/ingest-catering-r365.py --month 2026-07 --commit

WHY R365 AND NOT THE TRIS P&L
  These columns hold CALENDAR-MONTH revenue. The P&L only reports 28-day fiscal
  periods, so feeding it in understates catering by ~20-27%. R365 is the system
  the P&L is generated FROM, so we aggregate its journals by business date.
  See docs/catering-grain-investigation/.

SAFETY RULES ENFORCED HERE
  1. Dry-run by default. Writing requires --commit.
  2. Only WHOLE, CLOSED calendar months. A month is not closed until the last
     weekly journal has posted, which lags the month end -- see --settle-days.
  3. Never overwrite a differing existing value without --overwrite. Six
     historical cells are stale partial-month entries; correcting them is a
     deliberate act, not a side effect of a routine run.
  4. Only columns in COLUMN_ACCOUNTS are written. BF (In-house) is excluded
     because its account definition is unconfirmed.
  5. Any completeness or coverage warning blocks a commit unless --force.
"""

import argparse
import datetime as dt
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import r365_catering as rc
import sheets_io
import catering_pl as cp

# A month is only safe to write once its final weekly journal has posted.
# Observed posting lag in the target window: median 8 days, p95 58, max 110.
# 45 days clears the weekly cadence with room to spare without waiting forever.
DEFAULT_SETTLE_DAYS = 45


def parse_args():
    ap = argparse.ArgumentParser()
    ap.add_argument("--month", required=True, help="calendar month to write, e.g. 2026-07")
    ap.add_argument("--commit", action="store_true", help="actually write (default: dry run)")
    ap.add_argument("--overwrite", action="store_true",
                    help="replace existing values that differ (default: skip and report)")
    ap.add_argument("--force", action="store_true",
                    help="commit despite completeness/coverage warnings")
    ap.add_argument("--settle-days", type=int, default=DEFAULT_SETTLE_DAYS,
                    help=f"days after month end before it is writable (default {DEFAULT_SETTLE_DAYS})")
    ap.add_argument("--today", help="override today's date, for testing")
    return ap.parse_args()


def month_is_settled(month_key, today, settle_days):
    """(ok, reason) -- has this month closed AND had time for journals to post?"""
    _, last_day = rc.month_bounds(month_key)
    if today <= last_day:
        return False, f"{month_key} is not over yet (ends {last_day}, today {today})"
    age = (today - last_day).days
    if age < settle_days:
        return False, (f"{month_key} ended {last_day}, only {age} days ago. Journals post "
                       f"up to ~110 days late; waiting for {settle_days} days so the month "
                       f"is not written half-complete. Use --settle-days to override.")
    return True, f"{month_key} closed {age} days ago"


def main():
    args = parse_args()
    today = dt.date.fromisoformat(args.today) if args.today else dt.date.today()
    month = args.month
    try:
        first, last = rc.month_bounds(month)
    except Exception:
        raise SystemExit(f"--month must look like 2026-07, got {month!r}")

    print(f"Target: {month} ({first} .. {last})  ->  sheet row '{rc.month_label(month)}'")
    print(f"Mode:   {'COMMIT' if args.commit else 'DRY RUN (nothing will be written)'}")

    # ---- gate 1: month must be closed and settled --------------------------
    settled, reason = month_is_settled(month, today, args.settle_days)
    print(f"Settle: {reason}")
    if not settled and args.commit:
        raise SystemExit("BLOCKED: refusing to write an unsettled month.")

    # ---- pull R365, padded well past the posting lag ------------------------
    numbers = sorted({n for nums in rc.COLUMN_ACCOUNTS.values() for n in nums})
    headers = rc.auth_headers()
    records, warnings = rc.fetch_lines(numbers, first, last, headers)
    warnings = rc.verify_completeness(records, [month], list(warnings))

    # Coverage: a store with no journal is either a real zero or a missing feed.
    gaps = rc.coverage_gaps(records, [month])
    for gap_month, store in gaps:
        warnings.append(f"no catering journal at all for {store} in {gap_month} "
                        f"-- real zero, or a missing feed? Needs a human.")

    un = rc.unapproved(records)
    if un:
        warnings.append(f"{len(un)} unapproved journal line(s) are included in these totals")

    agg = rc.aggregate(records)
    label = rc.month_label(month)

    # ---- read the sheet -----------------------------------------------------
    sheets_io.load_env()
    service = sheets_io.sheets_service()
    sheet_id = sheets_io.spreadsheet_id()
    tabs = sorted(rc.STORE_TABS.values())
    sheet = sheets_io.read_tabs(service, sheet_id, tabs)

    planned, skipped, unchanged = [], [], []
    for tab in tabs:
        rows = sheet.get(tab) or []
        row_of = sheets_io.row_index_by_label(rows)
        if label not in row_of:
            warnings.append(f"{tab}: no row labelled {label} -- nothing written for this store")
            continue
        row_number = row_of[label]
        row = rows[row_number - 1]
        for column in sorted(rc.COLUMN_ACCOUNTS):
            value = agg.get(tab, {}).get(label, {}).get(column)
            if value is None:
                continue
            idx = cp.column_index(column)
            raw = row[idx] if len(row) > idx else ""
            entry = {"tab": tab, "row": row_number, "column": column,
                     "value": value, "existing": raw}
            if raw in ("", None):
                planned.append(entry)
                continue
            try:
                existing = float(str(raw).replace("$", "").replace(",", ""))
            except ValueError:
                skipped.append({**entry, "why": f"existing value is not a number: {raw!r}"})
                continue
            if abs(existing - value) < 0.01:
                unchanged.append(entry)
            elif args.overwrite:
                planned.append({**entry, "why": f"overwriting {existing:,.2f}"})
            else:
                skipped.append({**entry, "why": f"differs from existing {existing:,.2f} "
                                               f"(delta {value - existing:+,.2f}); needs --overwrite"})

    # ---- report -------------------------------------------------------------
    print(f"\nR365 totals for {label}:")
    for tab in tabs:
        cells = agg.get(tab, {}).get(label, {})
        if cells:
            parts = "  ".join(f"{c}={cells[c]:,.2f}" for c in sorted(cells))
            print(f"  {tab[:26]:28} {parts}")
        else:
            print(f"  {tab[:26]:28} (no catering revenue)")

    if warnings:
        print(f"\n{len(warnings)} WARNING(S):")
        for w in warnings:
            print(f"  ! {w}")

    print(f"\nPlanned writes: {len(planned)}   unchanged: {len(unchanged)}   skipped: {len(skipped)}")
    for p in planned:
        why = f"   [{p['why']}]" if p.get("why") else ""
        print(f"  WRITE {p['tab'][:24]:26} {p['column']}{p['row']:<4} = {p['value']:>11,.2f}{why}")
    for s in skipped:
        print(f"  SKIP  {s['tab'][:24]:26} {s['column']}{s['row']:<4} "
              f"r365={s['value']:>11,.2f}  {s['why']}")

    # ---- gate 2: warnings block a commit -----------------------------------
    if args.commit and warnings and not args.force:
        raise SystemExit(
            f"\nBLOCKED: {len(warnings)} warning(s) above. Nothing was written.\n"
            f"Resolve them, or pass --force if you have reviewed each one."
        )

    if not args.commit:
        print("\nDry run -- nothing written. Re-run with --commit to apply.")
        return

    if not planned:
        print("\nNothing to write.")
        return

    updated = sheets_io.write_updates(service, sheet_id, planned)
    print(f"\nWrote {updated} cell(s).")

    # ---- read back: prove the write landed ---------------------------------
    fresh = sheets_io.read_tabs(service, sheet_id, sorted({p["tab"] for p in planned}))
    bad = []
    for p in planned:
        rows = fresh.get(p["tab"]) or []
        row = rows[p["row"] - 1] if len(rows) >= p["row"] else []
        idx = cp.column_index(p["column"])
        raw = row[idx] if len(row) > idx else ""
        try:
            got = float(str(raw).replace("$", "").replace(",", ""))
        except (ValueError, TypeError):
            bad.append((p, raw))
            continue
        if abs(got - p["value"]) >= 0.01:
            bad.append((p, raw))
    if bad:
        print(f"\nREAD-BACK FAILED for {len(bad)} cell(s):")
        for p, raw in bad:
            print(f"  {p['tab']} {p['column']}{p['row']}: expected {p['value']:,.2f}, found {raw!r}")
        raise SystemExit("write did not land as expected -- investigate before rerunning")
    print("Read-back confirmed: every written cell holds the expected value.")


if __name__ == "__main__":
    main()
