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

# Automation owns 2026 forward; 2025 and earlier are frozen history.
#
# Angell's call (2026-08-14): don't worry about 2025. Two 2025 cells could not be
# reconciled against R365 (Cherrywood Nov America To Go, sheet 7,796.96 vs
# 3,673.17; Arbor Jul My Hot Lunchbox, sheet 4,348.75 vs R365 netting to 0.00)
# and chasing them has no forward value.
#
# This is a HARD floor, not a default, because the alternative is remembering to
# scope every invocation correctly forever. A backfill flag with an innocent name
# is exactly how a 2025 cell gets rewritten at 3am by a cron nobody reviewed.
# Overriding requires --i-know-this-rewrites-frozen-history, which is deliberately
# unpleasant to type and impossible to pass by accident.
EARLIEST_MANAGED_MONTH = "2026-01"


def parse_args():
    ap = argparse.ArgumentParser()
    ap.add_argument("--month", required=True, help="calendar month to write, e.g. 2026-07")
    ap.add_argument("--commit", action="store_true", help="actually write (default: dry run)")
    ap.add_argument("--overwrite", action="store_true",
                    help="replace existing values that differ (default: skip and report)")
    ap.add_argument("--only", action="append", default=[],
                    help="restrict overwrites to these tabs (repeatable); everything "
                         "else is skipped even with --overwrite")
    ap.add_argument("--force", action="store_true",
                    help="commit despite completeness/coverage warnings")
    ap.add_argument("--settle-days", type=int, default=DEFAULT_SETTLE_DAYS,
                    help=f"days after month end before it is writable (default {DEFAULT_SETTLE_DAYS})")
    ap.add_argument("--today", help="override today's date, for testing")
    ap.add_argument("--i-know-this-rewrites-frozen-history", action="store_true",
                    dest="unfreeze",
                    help=f"permit writing months before {EARLIEST_MANAGED_MONTH}; "
                         f"2025 and earlier were reconciled by hand and are frozen")
    return ap.parse_args()


def month_is_managed(month_key, unfreeze=False):
    """(ok, reason) -- is this month inside the window automation owns?"""
    if month_key >= EARLIEST_MANAGED_MONTH:
        return True, ""
    if unfreeze:
        return True, f"{month_key} is frozen history, overridden explicitly"
    return False, (
        f"{month_key} is before {EARLIEST_MANAGED_MONTH}. 2025 and earlier were "
        f"reconciled by hand and are frozen -- automation owns 2026 forward. "
        f"Pass --i-know-this-rewrites-frozen-history only if you truly intend to "
        f"rewrite settled history.")


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


def explain_difference(records, tab, label, header, existing, value, tol=1.01):
    """Why might the sheet hold `existing` when R365 says `value`?

    Returns a short human reason, or None if nothing explains it. This is the
    hand diagnosis that preceded every correction, turned into code so the
    reasoning is applied consistently instead of when someone remembers.

    Recognised explanations, all evidence-based:

    - PREFIX: the sheet equals the running total through some earlier journal.
      The cell was keyed before the month finished posting. This is the common
      case and the only one that is unambiguously a stale cell.
    - DOUBLE-COUNT: the sheet equals the full month plus one journal counted
      twice (Cherrywood June Lunchdrop: 3,539.95 + 619.95 again = 4,160.00).
    - ACCOUNT SUBSET: the sheet equals the total of a subset of the mapped
      accounts, i.e. someone missed one (Arbor Sep: 4440 only, omitting 4441).

    Anything else returns None and the cell is left alone. "I cannot explain it"
    is a finding, not an obstacle to route around.
    """
    store = next((s for s, t in rc.STORE_TABS.items() if t == tab), None)
    if store is None:
        return None
    accounts = rc.HEADER_ACCOUNTS.get(header, [])
    lines = [r for r in records
             if r["store"] == store and r["account"] in accounts
             and rc.month_label(r["month"]) == label]
    if not lines:
        return None

    lines.sort(key=lambda r: (r["date"], r["posted"]))

    # A cell that reads 0.00 has nothing to preserve, so filling it destroys no
    # record. Requiring an "explanation" to replace a zero would block the safest
    # write there is.
    if abs(existing) < 0.01:
        return "sheet cell was 0.00"

    # PREFIX: keyed before the month finished posting.
    #
    # Ordered by POSTED date, not business date. A cell keyed on some day could
    # only reflect journals that had posted BY that day, whatever business dates
    # they carry. This distinction is not academic: Arbor Nov 2025 EZCater held
    # 2,550.90 against 2,771.09, the difference being a 220.19 line for business
    # date 2025-11-02 that posted late on 2026-01-09. In business-date order that
    # line sorts FIRST and no prefix matches, so a real stale cell looked
    # unexplained; in posted order it sorts LAST and the story is obvious.
    by_posted = sorted(lines, key=lambda r: (r["posted"], r["date"]))
    running = 0.0
    for i, r in enumerate(by_posted):
        running += r["net"]
        if abs(running - existing) < tol and i < len(by_posted) - 1:
            missing = len(by_posted) - i - 1
            return (f"sheet matches R365 as of {r['posted']}, "
                    f"{missing} line(s) posted later")

    # DOUBLE-COUNT: full month plus one journal again.
    for r in lines:
        if abs((value + r["net"]) - existing) < tol:
            return f"sheet double-counts {r['date']} ({r['net']:,.2f})"

    # SIBLING CONFLATION: the sheet lumped another channel column into this one.
    #
    # This is the Arbor Sep 2025 case. EZCater taxable (4440/4442) and EZCater
    # tax-exempt (4441) have SEPARATE columns on the sheet, but the cell held
    # 2,750.30 = 1,415.15 + 1,335.15 -- both accounts keyed into one column.
    # Checked before the subset rule because it is the specific, provable story;
    # a subset match on the same numbers would be a vaguer description of it.
    for other_header, other_accounts in rc.HEADER_ACCOUNTS.items():
        if other_header == header:
            continue
        sibling = sum(r["net"] for r in records
                      if r["store"] == store and r["account"] in other_accounts
                      and rc.month_label(r["month"]) == label)
        if abs(sibling) < 0.01:
            continue
        if abs((value + sibling) - existing) < tol:
            return (f"sheet also included '{other_header}' ({sibling:,.2f}) "
                    f"in this column")

    # ACCOUNT SUBSET: someone missed one of the mapped accounts.
    #
    # Only a NON-ZERO account can explain a difference. Dropping an account that
    # contributed 0.00 changes nothing, so it "matches" trivially and would name
    # an innocent account as the cause -- a plausible-sounding wrong diagnosis,
    # which is worse than none.
    if len(accounts) > 1:
        by_account = {}
        for r in lines:
            by_account[r["account"]] = by_account.get(r["account"], 0.0) + r["net"]
        for drop in accounts:
            if abs(by_account.get(drop, 0.0)) < 0.01:
                continue
            subset = sum(v for a, v in by_account.items() if a != drop)
            if abs(subset - existing) < tol:
                return f"sheet omits account {drop}"

    return None


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

    # ---- gate 0: month must be inside the window automation owns ------------
    # First gate deliberately: blocks in DRY RUN too, so a frozen month reports
    # as out of scope instead of printing a plausible plan someone might chase.
    managed, why = month_is_managed(month, args.unfreeze)
    if not managed:
        raise SystemExit(f"OUT OF SCOPE: {why}")
    if why:
        print(f"Scope:  {why}")

    # ---- gate 1: month must be closed and settled --------------------------
    settled, reason = month_is_settled(month, today, args.settle_days)
    print(f"Settle: {reason}")
    if not settled and args.commit:
        raise SystemExit("BLOCKED: refusing to write an unsettled month.")

    # ---- pull R365, padded well past the posting lag ------------------------
    numbers = sorted({n for nums in rc.HEADER_ACCOUNTS.values() for n in nums})
    headers = rc.auth_headers()
    records, warnings = rc.fetch_lines(numbers, first, last, headers)
    warnings, notes = rc.verify_completeness(records, [month], list(warnings))

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
    service = sheets_io.sheets_service(verbose=True)
    sheet_id = sheets_io.spreadsheet_id()
    tabs = sorted(rc.STORE_TABS.values())
    sheet = sheets_io.read_tabs(service, sheet_id, tabs)

    planned, skipped, unchanged = [], [], []
    layouts = {}
    for tab in tabs:
        rows = sheet.get(tab) or []
        if not rows:
            warnings.append(f"{tab}: empty tab")
            continue
        # Resolve columns from THIS tab's header row. Letters are not portable.
        writable, skipped_cols, unknown = rc.resolve_columns(rows[0])
        layouts[tab] = (writable, skipped_cols, unknown)
        for label_, letter_ in unknown.items():
            warnings.append(f"{tab}: unrecognised catering column {letter_} "
                            f"'{label_}' -- not written, needs a mapping decision")
        row_of = sheets_io.row_index_by_label(rows)
        if label not in row_of:
            warnings.append(f"{tab}: no row labelled {label} -- nothing written for this store")
            continue
        row_number = row_of[label]
        row = rows[row_number - 1]
        for header, (letter, _accounts) in sorted(writable.items()):
            value = agg.get(tab, {}).get(label, {}).get(header)
            if value is None:
                continue
            idx = rc.column_index(letter)
            raw = row[idx] if len(row) > idx else ""
            entry = {"tab": tab, "row": row_number, "column": letter,
                     "header": header, "value": value, "existing": raw}
            if raw in ("", None):
                if abs(value) < 0.01:
                    # Writing 0.00 into a blank cell is not information. It
                    # asserts "this channel earned nothing" where the truth is
                    # "this channel was not active", and it makes an untouched
                    # history look audited.
                    unchanged.append({**entry, "why": "blank cell, R365 has 0.00"})
                else:
                    planned.append(entry)
                continue
            try:
                existing = float(str(raw).replace("$", "").replace(",", ""))
            except ValueError:
                skipped.append({**entry, "why": f"existing value is not a number: {raw!r}"})
                continue
            if abs(existing - value) < 0.01:
                unchanged.append(entry)
            elif abs(existing - value) < 1.01:
                # The maintainer rounds to whole dollars. Rewriting these with
                # cents is churn, not a correction.
                unchanged.append({**entry, "why": "rounding"})
            elif abs(value) < 0.01:
                # NEVER blank out a real figure because R365 has nothing. Arbor's
                # July 2025 My Hot Lunchbox is 4,348.75 on the sheet and 0.00 in
                # R365 -- writing the zero would destroy the only record of it.
                skipped.append({**entry, "why": f"R365 has 0.00 but sheet has "
                                               f"{existing:,.2f}; refusing to zero out a "
                                               f"real figure -- needs --force"})
            elif args.only and f"{letter}:{tab}" not in args.only and tab not in args.only:
                skipped.append({**entry, "why": f"differs from existing {existing:,.2f} "
                                               f"(delta {value - existing:+,.2f}); not in --only"})
            elif args.overwrite:
                # An overwrite must be EXPLAINED, not merely permitted.
                #
                # A sheet figure that R365 cannot reconstruct is evidence of
                # something this mapping does not model -- a channel booked to an
                # account we do not know, or revenue that never reached R365.
                # Replacing it with the R365 number destroys the only surviving
                # record of it and calls the destruction a correction.
                #
                # This guard exists because --overwrite let exactly that happen:
                # Cherrywood Nov 2025 America To Go was 7,796.96 on the sheet
                # against 3,673.17 in R365, with no prefix, subset, or cumulative
                # reading that reproduced it. It was overwritten anyway and had
                # to be restored by hand. A documented policy that the tool does
                # not enforce is a policy the tool will break.
                reason = explain_difference(records, tab, label, header, existing, value)
                if reason:
                    planned.append({**entry, "why": f"overwriting {existing:,.2f} ({reason})"})
                else:
                    skipped.append({**entry, "why":
                                    f"differs from existing {existing:,.2f} "
                                    f"(delta {value - existing:+,.2f}) and R365 cannot "
                                    f"explain the sheet figure -- refusing to overwrite "
                                    f"an unexplained value; needs --force"})
            else:
                skipped.append({**entry, "why": f"differs from existing {existing:,.2f} "
                                               f"(delta {value - existing:+,.2f}); needs --overwrite"})

    # ---- report -------------------------------------------------------------
    print("\nPer-tab catering layout (read from each tab's own header row):")
    for tab in tabs:
        if tab not in layouts:
            continue
        writable, _s, unknown = layouts[tab]
        cols = "  ".join(f"{l}={h[:18]}" for h, (l, _) in
                         sorted(writable.items(), key=lambda kv: kv[1][0]))
        print(f"  {tab[:26]:28} {cols}")

    print(f"\nR365 totals for {label}:")
    for tab in tabs:
        cells = agg.get(tab, {}).get(label, {})
        if cells:
            parts = "  ".join(f"{c}={cells[c]:,.2f}" for c in sorted(cells))
            print(f"  {tab[:26]:28} {parts}")
        else:
            print(f"  {tab[:26]:28} (no catering revenue)")

    if notes:
        print(f"\n{len(notes)} note(s) (context only, does not block):")
        for n in notes:
            print(f"  - {n}")

    if warnings:
        print(f"\n{len(warnings)} WARNING(S):")
        for w in warnings:
            print(f"  ! {w}")

    print(f"\nPlanned writes: {len(planned)}   unchanged: {len(unchanged)}   skipped: {len(skipped)}")
    for p in planned:
        why = f"   [{p['why']}]" if p.get("why") else ""
        print(f"  WRITE {p['tab'][:22]:24} {p['column']}{p['row']:<4} "
              f"{p['header'][:18]:20} = {p['value']:>11,.2f}{why}")
    for s in skipped:
        print(f"  SKIP  {s['tab'][:22]:24} {s['column']}{s['row']:<4} "
              f"{s['header'][:18]:20} r365={s['value']:>11,.2f}  {s['why']}")

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
        idx = rc.column_index(p["column"])
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
