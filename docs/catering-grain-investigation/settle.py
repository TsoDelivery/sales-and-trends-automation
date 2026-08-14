#!/usr/bin/env python3
"""Settle it: is the row label '3.2026' a CALENDAR MONTH or a FISCAL PERIOD?

Column C is 'Days in Month'. Calendar months give 28/30/31 in a known pattern.
Fiscal periods are always 28. This is decisive.
"""
import os, sys
from pathlib import Path

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

tab = "Cherrywood Monthly Sales "
r = svc.spreadsheets().values().get(
    spreadsheetId=sid, range=f"'{tab}'!A1:E60",
    valueRenderOption="UNFORMATTED_VALUE").execute()
rows = r.get("values", [])

print(f"{'row':>4}  {'label':10} {'B':>12} {'C (days)':>10} {'D':>12}")
print("-" * 56)
for i, row in enumerate(rows, start=1):
    label = str(row[0]).strip() if row else ""
    if not label:
        continue
    b = row[1] if len(row) > 1 else ""
    c = row[2] if len(row) > 2 else ""
    d = row[3] if len(row) > 3 else ""
    print(f"{i:>4}  {label:10} {str(b):>12} {str(c):>10} {str(d):>12}")

print()
print("VERDICT LOGIC:")
print("  If days column shows 31 for '3.2026' and 28 for '2.2026' -> CALENDAR MONTHS")
print("  If days column shows 28 everywhere                       -> FISCAL PERIODS")
