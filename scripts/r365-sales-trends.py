#!/usr/bin/env python3
"""R365 → Sales & Trends Google Sheet.

Fetches revenue from R365 SalesDetail and ticket counts from SalesPayment,
then writes to the Sales & Trends spreadsheet for each store.

Uses the proven daily-pagination pattern from r365_sales_window.py.

Usage:
    # Read-only validation against the live sheet
    python3 scripts/r365-sales-trends.py --month 2026-07 --validate

    # Fill only empty supported R365 columns
    python3 scripts/r365-sales-trends.py --month 2026-07 --write

    # Specific stores only
    python3 scripts/r365-sales-trends.py --month 2026-07 --validate --stores arbor,tsoco
"""

import argparse
import datetime as dt
import json
import os
import re
import sys
import urllib.parse
import base64
from collections import defaultdict
from pathlib import Path

# ── R365 OData (proven approach from r365_sales_window.py) ──────────────────────

sys.path.insert(0, os.path.expanduser("~/.hermes/skills/openclaw-imports/r365-odata-reporting/scripts"))
from r365_odata import get, fetch_day


def auth_token():
    """Build R365 Basic auth from runner env or local 1Password helper."""
    username = os.environ.get("R365_USERNAME")
    password = os.environ.get("R365_PASSWORD")
    if username and password:
        return base64.b64encode(f"tsochinese\\{username}:{password}".encode()).decode()
    from r365_odata import auth_token as local_auth_token
    return local_auth_token()

# ── Google Sheets ───────────────────────────────────────────────────────────────

from google.oauth2 import service_account
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# ── Store configuration ─────────────────────────────────────────────────────────

# Map store key → (locationId, sheet tab name, site ID)
# GUIDs from R365 Location view (2026-08-12)
STORES = {
    "cherrywood": {
        "locationId": "f26f3c02-6c6b-4d4e-a670-b17ee70f3a5a",
        "tab": "Cherrywood Monthly Sales ",
        "name": "Cherrywood",
    },
    "arbor": {
        "locationId": "a74ff0f9-760b-403e-8614-74e065082c94",
        "tab": "Arbor Monthly Sales",
        "name": "Arbor",
    },
    "tsoco": {
        "locationId": "e3a4967b-3fad-4d03-9776-d5b2d549d09c",
        "tab": "TsoCo Monthly Sales",
        "name": "TsoCo",
    },
    "round-rock": {
        "locationId": "d23ea6b7-1623-4c5e-9e06-067bbb0181dd",
        "tab": "Round Rock Monthly Sales",
        "name": "Round Rock",
    },
    "menchaca": {
        "locationId": "e029fd9e-2be2-47f1-80fc-19791150c8d1",
        "tab": "Menchaca Monthly Sales",
        "name": "Menchaca",
    },
}

# ── Channel classification ──────────────────────────────────────────────────────

# Order matters: more specific patterns before broader ones
CHANNEL_RULES = [
    # 3P delivery platforms. R365 amount is gross revenue; the sheet may show
    # net-of-promos for some platforms.
    ("3P:Uber Eats",  lambda sa: sa.startswith("Uber Eats")),
    ("3P:DoorDash",   lambda sa: sa.startswith("DoorDash") or sa.startswith("Doordash")),
    ("3P:Grubhub",    lambda sa: sa.startswith("Grubhub")),
    ("3P:Favor",      lambda sa: sa.startswith("Favor")),
    # 1P: Kiosk
    ("1P:Kiosk",      lambda sa: "Kiosk" in sa and "Carry Over" not in sa),
    # 1P: Phone AI (UrbanPiper / AIAssistant.co / Voicify)
    ("1P:Phone AI",   lambda sa: any(w in sa for w in ["AIAssistant", "UrbanPiper", "Voicify"])),
    # Note: Takeout and Delivery DO NOT EXIST as channel-level accounts in R365 SalesDetail.
    # R365 records only "tsochinese.com Delivery - Carry Over" / "tsochinese.com Take Out - Carry Over"
    # which are tiny adjustments (~$250-500/store/month) vs actual revenue (~$30-40K/store/month).
    # These channels must remain covered by Tray/Grafana.
    # Unknown / unclassified
    ("1P:Other",       lambda sa: True),
]

# Group channels into 1P vs 3P for sheet columns
ONE_P_CHANNELS = {"1P:Kiosk", "1P:Phone AI", "1P:Other"}
THREE_P_CHANNELS = {"3P:Uber Eats", "3P:DoorDash", "3P:Grubhub", "3P:Favor"}

# SalesPayment paymenttype → channel mapping for ticket counts
PAYMENT_TYPE_MAP = {
    "uber": "3P:Uber Eats",
    "doordash": "3P:DoorDash",
    "favor": "3P:Favor",
    "aiassistant-pickup": "1P:Phone AI",
    "aiassistant": "1P:Phone AI",
}

# ── Sheet column layout (0-indexed, verified from actual sheet headers 2026-08-12) ──
# Each tab has 85-86 columns (A-CH). Column positions are consistent across all 5 store tabs.
# Write/validate target columns:
SHEET_COLUMNS = {
    "1P - Carryout":   {"col": "M",  "idx": 12, "desc": "1P - Carryout Sales"},
    "Carryout Tix":    {"col": "O",  "idx": 14, "desc": "Carryout Tickets"},
    "1P - Delivery":   {"col": "R",  "idx": 17, "desc": "1P - Delivery Sales"},
    "Delivery Tix":    {"col": "T",  "idx": 19, "desc": "Delivery Tix"},
    "1P - Kiosk":      {"col": "W",  "idx": 22, "desc": "1P - Kiosk Take Out"},
    "Walk-In Tix":     {"col": "Y",  "idx": 24, "desc": "Walk-In Tix"},
    "Phone AI Sales":  {"col": "AB", "idx": 27, "desc": "1P - Phone Order (Ai)"},
    "Phone AI Tix":    {"col": "AD", "idx": 29, "desc": "Phone Order (Ai) Tix"},
    "UberEats":        {"col": "AG", "idx": 32, "desc": "UberEats (net of promos)"},
    "UberEats Tix":    {"col": "AI", "idx": 34, "desc": "UberEats Tix"},
    "DoorDash":        {"col": "AL", "idx": 37, "desc": "DoorDash (gross revenue)"},
    "DoorDash Tix":    {"col": "AN", "idx": 39, "desc": "DoorDash Tix"},
    "Favor":           {"col": "AQ", "idx": 42, "desc": "Favor (net of promos)"},
    "Favor Tix":       {"col": "AS", "idx": 44, "desc": "Favor Tix"},
    "Grubhub":         {"col": "AV", "idx": 47, "desc": "Grubhub (net of promos)"},
    "Grubhub Tix":     {"col": "AX", "idx": 49, "desc": "Grubhub Tix"},
    "7Now":            {"col": "BA", "idx": 52, "desc": "7Now (net of promos)"},
    "7Now Tix":        {"col": "BC", "idx": 54, "desc": "7Now Tix"},
    "1P Sales Total":  {"col": "H",  "idx": 7,  "desc": "1P Sales (Carryout+Delivery+Kiosk+Phone AI)"},
    "Total Sales":     {"col": "CA", "idx": 78, "desc": "Total Sales (varies: CA for 86-col, BZ for 85-col)"},
}

# Columns this script can write (white cells). R365 is authoritative for the
# channels below wherever SalesDetail exposes them. Tray remains the fallback
# for Phone AI until its R365 mapping is confirmed in the live P&L.
WRITE_COLUMNS = {
    "W": 22,  # 1P - Kiosk Take Out
    "AG": 32, # UberEats
    "AL": 37, # DoorDash gross revenue
    "AQ": 42, # Favor
    "AV": 47, # Grubhub
    "BA": 52, # 7Now
}


# ══════════════════════════════════════════════════════════════════════════════
# Data fetching
# ══════════════════════════════════════════════════════════════════════════════

def classify_sales_account(sa):
    """Return (channel_group, channel_name) for a salesAccount string."""
    sa = str(sa or "")
    if "TAX" in sa.upper():
        return None  # Skip TAX rows
    for name, matcher in CHANNEL_RULES:
        if matcher(sa):
            if name == "Other":
                return ("Other", sa)
            is_3p = name.startswith("3P:")
            grp = "3P" if is_3p else ("1P" if name.startswith("1P:") else "Other")
            return (grp, name)
    return ("Other", sa)


def fetch_month_revenue(token, year, month):
    """Fetch a full month of SalesDetail data, grouped by location and channel.

    Returns dict: locationId → {channel_name: total_revenue}
    """
    start = dt.date(year, month, 1)
    if month == 12:
        end = dt.date(year + 1, 1, 1)
    else:
        end = dt.date(year, month + 1, 1)

    by_loc = defaultdict(lambda: defaultdict(float))
    seen = set()

    day = start
    while day < end:
        n = 0
        for r in fetch_day(day, token):
            rid = r.get("salesdetailID")
            if rid in seen:
                continue
            seen.add(rid)

            stamp = str(r.get("date") or "")[:10]
            if not (start.isoformat() <= stamp < end.isoformat()):
                continue
            if r.get("void") is True:
                continue

            amt = float(r.get("amount") or 0)
            if amt == 0:
                continue

            sa = r.get("salesAccount", "")
            result = classify_sales_account(sa)
            if result is None:
                continue  # TAX row
            grp, ch_name = result

            loc = str(r.get("location") or "")
            by_loc[loc][ch_name] += amt
            n += 1

        print(f"    {day}: {n} in-range rows", file=sys.stderr, flush=True)
        day += dt.timedelta(days=1)

    return by_loc


def fetch_month_tickets(token, year, month):
    """Fetch SalesPayment for a month and extract ticket counts by payment type.

    Returns dict: locationId → {channel_name: ticket_count}
    """
    start = dt.date(year, month, 1).isoformat()
    if month == 12:
        nxt = dt.date(year + 1, 1, 1).isoformat()
    else:
        nxt = dt.date(year, month + 1, 1).isoformat()

    # Use daily pagination to avoid HTTP 500
    day = dt.date(year, month, 1)
    end = dt.date(year + 1, 1, 1) if month == 12 else dt.date(year, month + 1, 1)

    by_loc = defaultdict(lambda: defaultdict(lambda: {"orders": set(), "rev": 0.0}))

    while day < end:
        nxt_day = day + dt.timedelta(days=1)
        params = (f"$filter=date ge {day.isoformat()}T00:00:00Z and "
                  f"date lt {nxt_day.isoformat()}T00:00:00Z&$top=5000&$orderby=date")
        url = "https://odata.restaurant365.net/api/v2/views/SalesPayment?" + urllib.parse.quote(params, safe="&=,()'")
        try:
            data = get(url, token)
            rows = data.get("value", [])
        except Exception:
            rows = []

        for r in rows:
            pt = str(r.get("paymenttype") or "").lower()
            ch = PAYMENT_TYPE_MAP.get(pt)
            if ch is None:
                continue
            loc = str(r.get("location") or "")
            sid = r.get("salesID")
            if sid:
                by_loc[loc][ch]["orders"].add(sid)
            by_loc[loc][ch]["rev"] += float(r.get("amount") or 0)

        day = nxt_day

    # Convert to simple counts
    result = defaultdict(dict)
    for loc, channels in by_loc.items():
        for ch, data in channels.items():
            result[loc][ch] = len(data["orders"])
    return result


# ══════════════════════════════════════════════════════════════════════════════
# Sheet operations
# ══════════════════════════════════════════════════════════════════════════════

def get_sheets_service(credentials_path):
    creds = service_account.Credentials.from_service_account_file(
        credentials_path, scopes=SCOPES
    )
    return build("sheets", "v4", credentials=creds)


def display_month(ym):
    m = int(ym[5:7])
    y = ym[:4]
    return f"{m}.{y}"


def find_month_row(service, spreadsheet_id, tab, ym):
    """Find the row containing the given month label in column A."""
    expected = display_month(ym)
    result = service.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id,
        range=f"'{tab}'!A:A",
        valueRenderOption="FORMATTED_VALUE",
    ).execute()
    rows = result.get("values", [])
    for i, row in enumerate(rows):
        if str(row[0] if row else "").strip() == expected:
            return i + 1  # 1-indexed
    return None



def read_existing_row(service, spreadsheet_id, tab, row_num, max_cols=99):
    """Read the full month row for validation or safe writes."""
    result = service.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id,
        range=f"'{tab}'!A{row_num}:CU{row_num}",
        valueRenderOption="FORMATTED_VALUE",
    ).execute()
    existing = result.get("values", [[]])[0]
    existing.extend([""] * (max_cols - len(existing)))
    return existing[:max_cols]


def read_header_row(service, spreadsheet_id, tab, max_cols=99):
    """Read the header row so formulas follow the live tab's layout."""
    result = service.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id,
        range=f"'{tab}'!A1:CU1",
        valueRenderOption="FORMATTED_VALUE",
    ).execute()
    headers = result.get("values", [[]])[0]
    headers.extend([""] * (max_cols - len(headers)))
    return headers[:max_cols]


def find_header_index(headers, label):
    """Find a header by normalized text, returning its 0-based index."""
    wanted = str(label).strip().casefold()
    for idx, header in enumerate(headers):
        if str(header).strip().casefold() == wanted:
            return idx
    return None


def column_letter(index):
    """Convert a 0-based column index to an A1 column letter."""
    if index < 0:
        raise ValueError("column index must be non-negative")
    result = ""
    index += 1
    while index:
        index, remainder = divmod(index - 1, 26)
        result = chr(65 + remainder) + result
    return result


def catering_formula(column, row_num, year):
    """Return a YTD-average placeholder using prior same-year rows only."""
    start_row = row_num + 1
    return (
        f'=IFERROR(AVERAGEIF($A${start_row}:$A$200,"*.{year}",'
        f'{column}{start_row}:{column}200),"")'
    )


def is_empty(v):
    return v in ("", None, 0, "0", "#DIV/0!", "#REF!", "#N/A")


def to_num(v):
    if is_empty(v):
        return 0.0
    try:
        return float(str(v).replace(",", "").replace("$", ""))
    except (TypeError, ValueError):
        return 0.0


def write_to_sheet(service, spreadsheet_id, updates):
    body = {"valueInputOption": "USER_ENTERED", "data": updates}
    result = service.spreadsheets().values().batchUpdate(
        spreadsheetId=spreadsheet_id, body=body
    ).execute()
    return result.get("totalUpdatedCells", 0)


def compute_store_values(month_revenue, month_tickets, store_key, store):
    """Return supported R365 channel values keyed by sheet column."""
    rev = month_revenue.get(store["locationId"], {})
    return {
        "W": rev.get("1P:Kiosk", 0.0),
        "AG": rev.get("3P:Uber Eats", 0.0),
        "AL": rev.get("3P:DoorDash", 0.0),
        "AQ": rev.get("3P:Favor", 0.0),
        "AV": rev.get("3P:Grubhub", 0.0),
        "BA": 0.0,  # 7NOW is not present in R365 SalesDetail
    }


# R365 can validate these channels. Takeout/Delivery remain explicitly N/A:
# they are not channel-level R365 accounts; only small Carry Over adjustments exist.
VALIDATION_CHANNELS = [
    ("Kiosk", "W", 22, "1P:Kiosk", "R365 gross revenue"),
    ("UberEats", "AG", 32, "3P:Uber Eats", "R365 gross; sheet is net of promos"),
    ("DoorDash", "AL", 37, "3P:DoorDash", "R365 gross; sheet is net of promos"),
    ("Favor", "AQ", 42, "3P:Favor", ""),
    ("Grubhub", "AV", 47, "3P:Grubhub", ""),
    ("7Now", "BA", 52, None, "Not present in R365 SalesDetail"),
    ("Carryout", "M", 12, None, "Not present in R365 as real channel revenue"),
    ("Delivery", "R", 17, None, "Not present in R365 as real channel revenue"),
    ("Phone AI", "AB", 27, None, "Handled by existing Tray/UrbanPiper automation"),
]
THRESHOLD_PCT = 5.0

# Catering is not yet exposed as a validated SalesDetail channel. Keep this as
# an explicit placeholder rather than inventing a revenue number. Once R365
# financials are complete, replace it with the approved YTD-average formula.
CATERING_FORMULA_PLACEHOLDER = "YTD average pending R365 financials"

# R365 reports gross 3P revenue while Sales & Trends records net revenue after
# promotions. These bands are intentionally channel-specific and were calibrated
# from the July 2026 closed-month comparison. They flag a change in the promo
# relationship, rather than re-flagging the known gross-vs-net difference.
PROMO_NET_RATIO_BANDS = {
    "3P:Uber Eats": (0.75, 0.90),
    "3P:DoorDash": (0.85, 1.05),
}


def channel_status(channel, r365_value, sheet_value):
    """Return (status, note) using a promo-aware rule where appropriate."""
    if not r365_value and not sheet_value:
        return "PASS", ""

    if channel in PROMO_NET_RATIO_BANDS:
        if r365_value <= 0 or sheet_value <= 0:
            return "FLAG", "outside expected promo-adjusted range"
        ratio = sheet_value / r365_value
        low, high = PROMO_NET_RATIO_BANDS[channel]
        if low <= ratio <= high:
            return "PASS", f"expected promo-adjusted range ({low:.0%}-{high:.0%} net/gross)"
        return "FLAG", f"outside expected promo-adjusted range ({low:.0%}-{high:.0%} net/gross)"

    delta_pct = (abs(r365_value - sheet_value) / sheet_value * 100.0) if sheet_value else 100.0
    if delta_pct <= THRESHOLD_PCT:
        return "PASS", "R365 gross revenue" if channel == "1P:Kiosk" else ""
    return "FLAG", f"outside {THRESHOLD_PCT:.0f}%"


def validate_store(month_revenue, sheet_row, store):
    rev = month_revenue.get(store["locationId"], {})
    results = []
    for name, col, idx, channel, note in VALIDATION_CHANNELS:
        sheet_value = to_num(sheet_row[idx])
        if channel is None:
            results.append((name, col, sheet_value, None, None, "N/A", note))
            continue
        r365_value = float(rev.get(channel, 0.0))
        status, note = channel_status(channel, r365_value, sheet_value)
        if channel in PROMO_NET_RATIO_BANDS and r365_value and sheet_value:
            delta_pct = abs(r365_value - sheet_value) / sheet_value * 100.0
        else:
            delta_pct = (abs(r365_value - sheet_value) / sheet_value * 100.0) if sheet_value else (100.0 if r365_value else 0.0)
        if note and not (channel in PROMO_NET_RATIO_BANDS):
            note = note or ""
        results.append((name, col, sheet_value, r365_value, delta_pct, status, note))
    return results


def print_validation(store_name, results):
    print(f"\n{store_name}")
    print(f"{'Channel':<12} {'R365':>13} {'Sheet':>13} {'Delta':>8}  Status")
    print("-" * 58)
    for name, col, sheet_value, r365_value, delta_pct, status, note in results:
        r365_text = "N/A" if r365_value is None else f"${r365_value:,.2f}"
        sheet_text = f"${sheet_value:,.2f}"
        delta_text = "N/A" if delta_pct is None else f"{delta_pct:.1f}%"
        suffix = f" — {note}" if note else ""
        print(f"{name:<12} {r365_text:>13} {sheet_text:>13} {delta_text:>8}  {status}{suffix}")


def main():
    ap = argparse.ArgumentParser(description="R365 Sales & Trends validator")
    ap.add_argument("--month", required=True, help="YYYY-MM")
    ap.add_argument("--validate", action="store_true", help="Compare R365 against the live sheet")
    ap.add_argument("--write", action="store_true", help="Fill only empty supported R365 columns")
    ap.add_argument("--fail-on-flag", action="store_true", help="Exit 2 when a supported channel exceeds the threshold")
    ap.add_argument("--stores", help="Comma-separated store keys")
    args = ap.parse_args()
    if not re.fullmatch(r"\d{4}-\d{2}", args.month):
        ap.error("--month must be YYYY-MM")

    year, month = int(args.month[:4]), int(args.month[5:7])
    aliases = {"rr": "round-rock", "roundrock": "round-rock"}
    selected = STORES
    if args.stores:
        selected = {}
        for raw in args.stores.split(","):
            key = aliases.get(raw.strip().lower().replace(" ", "-").replace("_", "-"), raw.strip().lower())
            if key not in STORES:
                ap.error(f"Unknown store '{raw}'. Options: {', '.join(STORES)}")
            selected[key] = STORES[key]

    print("Authenticating with R365...", flush=True)
    token = auth_token()
    print(f"Fetching SalesDetail for {args.month}...", flush=True)
    month_revenue = fetch_month_revenue(token, year, month)

    repo_dir = Path(__file__).resolve().parent.parent
    spreadsheet_id = ""
    env_path = repo_dir / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            if line.strip().startswith("SALES_TRENDS_SPREADSHEET_ID="):
                spreadsheet_id = line.split("=", 1)[1].strip().strip("\"'")
                break
    spreadsheet_id = spreadsheet_id or os.environ.get("SALES_TRENDS_SPREADSHEET_ID", "")
    if not spreadsheet_id:
        raise SystemExit("SALES_TRENDS_SPREADSHEET_ID not found")
    credentials_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", str(repo_dir / ".secrets" / "google-service-account.json"))
    service = get_sheets_service(credentials_path)

    mode = "VALIDATION" if args.validate else ("WRITE" if args.write else "DRY RUN")
    print(f"\nSales & Trends — {mode} — {args.month}")
    flagged = 0
    for store_key, store in selected.items():
        row = find_month_row(service, spreadsheet_id, store["tab"], args.month)
        if row is None:
            print(f"\n{store['name']}: no row for {display_month(args.month)}")
            continue
        rev = month_revenue.get(store["locationId"], {})
        if not rev:
            print(f"\n{store['name']}: no R365 revenue rows")
            continue
        sheet_row = read_existing_row(service, spreadsheet_id, store["tab"], row)
        headers = read_header_row(service, spreadsheet_id, store["tab"])
        if args.validate:
            results = validate_store(month_revenue, sheet_row, store)
            print_validation(store["name"], results)
            flagged += sum(1 for result in results if result[5] == "FLAG")
            if not args.write:
                continue
        values = compute_store_values(month_revenue, {}, store_key, store)
        print(f"\n{store['name']}: Kiosk ${values['W']:,.2f}; UberEats ${values['AG']:,.2f} gross; DoorDash ${values['AL']:,.2f} gross; Favor ${values['AQ']:,.2f}; Grubhub ${values['AV']:,.2f}")
        print(f"   Catering: formula placeholder ({CATERING_FORMULA_PLACEHOLDER})")
        if args.write:
            updates = []
            for col, idx in WRITE_COLUMNS.items():
                value = values.get(col, 0)
                if value > 0 and is_empty(sheet_row[idx]):
                    updates.append({"range": f"'{store['tab']}'!{col}{row}", "values": [[round(value, 2)]]})
            catering_idx = find_header_index(headers, "Total Catering")
            if catering_idx is not None and is_empty(sheet_row[catering_idx]):
                catering_col = column_letter(catering_idx)
                updates.append({
                    "range": f"'{store['tab']}'!{catering_col}{row}",
                    "values": [[catering_formula(catering_col, row, args.month[:4])]],
                })
            print(f"Wrote {write_to_sheet(service, spreadsheet_id, updates)} cell(s)" if updates else "No empty supported cells to write")
    if not args.validate and not args.write:
        print("\nNo cells written. Use --validate for comparison or --write for supported empty cells.")
    if args.validate and args.fail_on_flag and flagged:
        print(f"\nValidation failed: {flagged} supported channel comparison(s) exceeded {THRESHOLD_PCT:.1f}%.")
        raise SystemExit(2)


if __name__ == "__main__":
    main()
