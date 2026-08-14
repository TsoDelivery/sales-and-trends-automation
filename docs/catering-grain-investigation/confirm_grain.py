#!/usr/bin/env python3
"""Confirm the grain conclusion across ALL cells, not just the 26 disagreements.

If the sheet's catering columns are calendar-month figures, then R365 aggregated
by calendar month should match the sheet far more often than the fiscal-period
P&L does. Quantify both.
"""
import json, os, sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, "/Users/tsora/work/sales-and-trends-automation/scripts")

raw = json.loads(open("/tmp/p03test/r365_by_month.json").read())
r365 = defaultdict(float)
for key, v in raw.items():
    store, acct, month = key.split("|")
    r365[(store, acct, month)] += v

REPO = Path("/Users/tsora/work/sales-and-trends-automation")
for line in (REPO / ".env").read_text().splitlines():
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

from google.oauth2 import service_account
from googleapiclient.discovery import build

creds = service_account.Credentials.from_service_account_file(
    str(REPO / ".secrets" / "google-service-account.json"),
    scopes=["https://www.googleapis.com/auth/spreadsheets"])
svc = build("sheets", "v4", credentials=creds, cache_discovery=False)
sid = os.environ["SALES_TRENDS_SPREADSHEET_ID"]

TABS = {"Cherrywood Monthly Sales ": "Tso Chinese Cherrywood",
        "Arbor Monthly Sales": "Tso Chinese Arboretum Crossing",
        "TsoCo Monthly Sales": "TsoCo South Congress",
        "Round Rock Monthly Sales": "Tso Chinese Round Rock",
        "Menchaca Monthly Sales": "Tso Chinese Menchaca"}
COL_ACCTS = {"BF": ["4310", "4311", "4312"], "BH": ["4420"],
             "BJ": ["4440"], "BM": ["4445"]}
COL_IDX = {"BF": 0, "BG": 1, "BH": 2, "BI": 3, "BJ": 4, "BK": 5, "BL": 6, "BM": 7}

match = near = diff = blank_r365 = 0
examples = []

for tab, store_long in TABS.items():
    labels = svc.spreadsheets().values().get(
        spreadsheetId=sid, range=f"'{tab}'!A1:A70",
        valueRenderOption="FORMATTED_VALUE").execute().get("values", [])
    block = svc.spreadsheets().values().get(
        spreadsheetId=sid, range=f"'{tab}'!BF1:BM70",
        valueRenderOption="UNFORMATTED_VALUE").execute().get("values", [])

    for i, lab_row in enumerate(labels):
        label = str(lab_row[0]).strip() if lab_row else ""
        if not label or "." not in label:
            continue
        try:
            m, y = label.split(".")
            mk = f"{int(y)}-{int(m):02d}"
        except ValueError:
            continue
        if not (2025 <= int(y) <= 2026):
            continue
        vals = block[i] if i < len(block) else []
        for col, accts in COL_ACCTS.items():
            j = COL_IDX[col]
            sv = vals[j] if j < len(vals) else None
            if not isinstance(sv, (int, float)) or sv == 0:
                continue
            rv = sum(r365.get((store_long, a, mk), 0.0) for a in accts)
            if rv == 0:
                blank_r365 += 1
                continue
            d = abs(rv - sv)
            if d < 0.01:
                match += 1
            elif d <= 1.0:
                near += 1
            else:
                diff += 1
                if len(examples) < 12:
                    examples.append((tab.split()[0], label, col, sv, rv, rv - sv))

print("=" * 88)
print("SHEET vs R365 aggregated by CALENDAR MONTH -- all populated catering cells")
print("=" * 88)
print(f"  exact to the cent      : {match}")
print(f"  within $1 (rounding)   : {near}")
print(f"  genuinely different    : {diff}")
print(f"  no R365 data for cell  : {blank_r365}")
print()
if examples:
    print("Remaining differences:")
    print(f"  {'store':11} {'row':8} {'col':4} {'sheet':>11} {'R365 month':>11} {'delta':>10}")
    for s, l, c, sv, rv, dl in examples:
        print(f"  {s:11} {l:8} {c:4} {sv:>11,.2f} {rv:>11,.2f} {dl:>10,.2f}")
