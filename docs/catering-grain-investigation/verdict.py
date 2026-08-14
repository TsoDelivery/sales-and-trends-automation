#!/usr/bin/env python3
"""FINAL VERDICT: for every disagreement, compare the sheet against R365 summed
over the CALENDAR MONTH matching the row label, and against the fiscal-period P&L.

Whichever source R365 agrees with is the correct one.
"""
import json
from collections import defaultdict

raw = json.loads(open("/tmp/p03test/r365_by_month.json").read())
r365 = defaultdict(float)
for key, v in raw.items():
    store, acct, month = key.split("|")
    r365[(store, acct, month)] += v

rows = json.loads(open("/tmp/p03test/disagreements.json").read())

STORE = {"Cherrywood Monthly Sales ": "Tso Chinese Cherrywood",
         "Arbor Monthly Sales": "Tso Chinese Arboretum Crossing",
         "TsoCo Monthly Sales": "TsoCo South Congress",
         "Round Rock Monthly Sales": "Tso Chinese Round Rock",
         "Menchaca Monthly Sales": "Tso Chinese Menchaca"}
# Sheet column -> the R365 GL numbers that column represents.
COL_ACCTS = {"BF": ["4310", "4311", "4312"], "BG": ["4313"], "BH": ["4420"],
             "BJ": ["4440"], "BK": ["4441"], "BM": ["4445"]}
COL_NAME = {"BF": "In-house", "BG": "In-house(NT)", "BH": "Lunchdrop",
            "BJ": "EZCater", "BK": "EZCater(NT)", "BM": "AmericaToGo"}


def month_key(period_label):
    """Row label '3.2026' -> calendar month '2026-03'."""
    m, y = period_label.split(".")
    return f"{y}-{int(m):02d}"


print("=" * 104)
print("FINAL VERDICT -- R365 (source of truth) summed over the CALENDAR MONTH of the row label")
print("=" * 104)
print(f"{'store':11} {'row':8} {'column':12} {'sheet':>11} {'P&L fiscal':>11} "
      f"{'R365 cal-month':>14} {'verdict':>16}")
print("-" * 104)

sheet_wins = pl_wins = neither = 0
detail = []
for r in rows:
    if r["status"] != "diff":
        continue
    try:
        sheet = float(r["sheet"])
    except (TypeError, ValueError):
        continue
    store_long = STORE[r["tab"]]
    mk = month_key(r["period"])
    rv = sum(r365.get((store_long, a, mk), 0.0) for a in COL_ACCTS[r["column"]])

    d_sheet, d_pl = abs(rv - sheet), abs(rv - r["pl"])
    if d_sheet <= 1.0 and d_sheet < d_pl:
        verdict, sheet_wins = "SHEET correct", sheet_wins + 1
    elif d_pl <= 1.0 and d_pl < d_sheet:
        verdict, pl_wins = "P&L correct", pl_wins + 1
    elif d_sheet < d_pl:
        verdict, sheet_wins = f"sheet closer", sheet_wins + 1
    else:
        verdict, neither = "unresolved", neither + 1

    print(f"{r['tab'].split()[0]:11} {r['period']:8} {COL_NAME[r['column']]:12} "
          f"{sheet:>11,.2f} {r['pl']:>11,.2f} {rv:>14,.2f} {verdict:>16}")
    detail.append({"store": r["tab"].split()[0], "row": r["period"],
                   "column": r["column"], "sheet": sheet, "pl": r["pl"],
                   "r365_cal_month": round(rv, 2), "verdict": verdict})

print("-" * 104)
print(f"SHEET matches R365 calendar month : {sheet_wins}")
print(f"P&L matches R365                  : {pl_wins}")
print(f"unresolved                        : {neither}")
json.dump(detail, open("/tmp/p03test/verdict.json", "w"), indent=1)
