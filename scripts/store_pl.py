"""Pure extraction + comparison for the monthly TRIS P&L (Profit & Loss - Location).

No network, no Google -- unit-testable in isolation.

WHY THIS SOURCE
---------------
The reconciled P&L is the accounting team's final word: TRIS -> email -> Min's
Drive folder -> (previously) hand-keyed into Sales & Trends. This module replaces
the hand-keying step.

The header carries an explicit date range ("06/01/2026 - 06/30/2026"). We PARSE
that range and refuse to proceed unless it is a whole calendar month matching the
requested row, because the Sales & Trends store tabs are calendar-month grain
(verified: 127/144 rows match calendar days, 0/144 match the 28-day fiscal
period). A fiscal-period P&L export must never be written into these rows -- it
understates by 10-27%.

WHY "Total <channel>" ROWS, NOT THE COMPONENT ROWS
-------------------------------------------------
Each channel appears as gross + Discounts + Adj/Refunds + a Total. Angell's
direction (2026-08-14): the sheet should hold the P&L's NET figure. So we read
the Total rows, which are net of discounts and refunds.

Verified against June 2026, all five stores:
  * Kiosk, Favor, 7NOW      -> match the sheet within 0.5% (same definition)
  * Uber Eats, DoorDash     -> P&L is 2-11% LOWER (sheet held a different net)
  * Grafana (carryout+deliv)-> P&L is 4-6% LOWER (same reason)
The systematic one-directional gap is a definitional difference, not noise.

WHY LABEL MATCHING, NOT ROW NUMBERS
-----------------------------------
Row positions shift between periods as TRIS adds accounts. We match the exact
label in column A. Note "Doordash Discounts" vs "DoorDash Sales" -- TRIS spells
the same brand inconsistently, so all matching is case-insensitive.
"""

import datetime as dt
import re

# P&L header location name -> Sales & Trends tab name.
PL_LOCATIONS = {
    "tso chinese cherrywood": "Cherrywood Monthly Sales ",
    "tso chinese arboretum crossing": "Arbor Monthly Sales",
    "tsoco south congress": "TsoCo Monthly Sales",
    "tso chinese round rock": "Round Rock Monthly Sales",
    "tso chinese menchaca": "Menchaca Monthly Sales",
}

# Non-store columns that must never be treated as a store.
NON_STORE = {"corporate office", "prep/commissary", "total", ""}

# Sales & Trends column index (0-based) -> P&L label.
# Only channels with a single unambiguous P&L Total row are listed.
COLUMN_MAP = {
    22: "Total Tray Kiosk Sales",       # W  - 1P Kiosk Take Out
    27: "Food Sales: AIAssistant",      # AB - 1P Phone Order (Ai)
    32: "Total Uber Eats Sales",        # AG - 3P UberEats
    37: "Total DoorDash Sales",         # AL - 3P DoorDash
    42: "Favor Sales",                  # AQ - 3P Favor
    47: "Total Grubhub Sales",          # AV - 3P Grubhub
    52: "7 Now Sales",                  # BA - 3P 7Now
}

# Human-readable names for the report.
COLUMN_NAMES = {
    22: "Kiosk", 27: "Phone AI", 32: "UberEats", 37: "DoorDash",
    42: "Favor", 47: "Grubhub", 52: "7NOW",
}

# Carryout + Delivery are a SINGLE P&L line ("Total Grafana Sales") but TWO sheet
# columns (M carryout, R delivery). The P&L cannot apportion between them, so we
# never write these -- we only report the combined variance.
COMBINED_ONLY = {
    "Total Grafana Sales": (12, 17, "Carryout+Delivery"),
}

# Cents-vs-whole-dollars hand keying is not a real disagreement.
TOLERANCE = 1.01


def parse_period_range(value):
    """Parse "06/01/2026 - 06/30/2026" -> (date, date). None if unparseable."""
    if not value:
        return None
    m = re.search(r"(\d{2})/(\d{2})/(\d{4})\s*[-–]\s*(\d{2})/(\d{2})/(\d{4})", str(value))
    if not m:
        return None
    a = dt.date(int(m.group(3)), int(m.group(1)), int(m.group(2)))
    b = dt.date(int(m.group(6)), int(m.group(4)), int(m.group(5)))
    return (a, b)


def is_whole_calendar_month(start, end):
    """True only if start is the 1st and end is the last day of the SAME month."""
    if start.day != 1 or start.year != end.year or start.month != end.month:
        return False
    if end.month == 12:
        nxt = dt.date(end.year + 1, 1, 1)
    else:
        nxt = dt.date(end.year, end.month + 1, 1)
    return end == nxt - dt.timedelta(days=1)


def parse_amount(value):
    """P&L numbers: "43,123" -> 43123.0; "--1,266" -> -1266.0 (TRIS double dash)."""
    s = str(value or "").replace(",", "").replace("$", "").strip()
    if s.startswith("--"):
        s = "-" + s[2:]
    if not s or s == "-":
        return None
    try:
        return float(s)
    except ValueError:
        return None


def locate_stores(header_row):
    """Map tab name -> column index, from the P&L location header row."""
    out = {}
    for idx, cell in enumerate(header_row or []):
        key = " ".join(str(cell or "").split()).lower()
        if not key or key in NON_STORE:
            continue
        tab = PL_LOCATIONS.get(key)
        if tab:
            out[tab] = idx
    return out


def extract(rows):
    """Pull {tab: {label: amount}} plus the parsed period range from P&L rows.

    Raises ValueError if the header range is missing, unparseable, or not a
    whole calendar month -- refusing to guess is the point.
    """
    rng = None
    for r in rows[:8]:
        rng = parse_period_range(r[0] if r else "")
        if rng:
            break
    if not rng:
        raise ValueError("no parseable date range in the P&L header (rows 1-8)")
    start, end = rng
    if not is_whole_calendar_month(start, end):
        raise ValueError(
            f"P&L covers {start}..{end}, which is not a whole calendar month. "
            "The Sales & Trends store tabs are calendar-month grain; writing a "
            "fiscal-period export into them understates revenue by 10-27%."
        )

    header_idx = None
    stores = {}
    for i, r in enumerate(rows[:12]):
        found = locate_stores(r)
        if len(found) >= 3:
            header_idx, stores = i, found
            break
    if not stores:
        raise ValueError("could not locate store columns in the P&L header")

    data = {tab: {} for tab in stores}
    for r in rows[header_idx + 1:]:
        label = " ".join(str(r[0] if r else "").split())
        if not label:
            continue
        key = label.lower()
        for tab, col in stores.items():
            amt = parse_amount(r[col]) if len(r) > col else None
            if amt is not None and key not in data[tab]:
                data[tab][key] = amt
    return {"period": (start, end), "month": (start.year, start.month), "stores": data}


def compare_row(pl_store, sheet_row, allow_overwrite=False):
    """Compare one month-row for one store. Returns a list of findings.

    Each finding: {col, name, pl, sheet, action, reason}
      action == "fill"      -> sheet cell blank/zero, P&L has a figure
      action == "agree"     -> within TOLERANCE
      action == "update"    -> real difference, overwrite authorized
      action == "report"    -> real difference, NOT written
    """
    out = []

    def sheet_val(idx):
        if sheet_row is None or len(sheet_row) <= idx:
            return None
        s = str(sheet_row[idx] or "").replace("$", "").replace(",", "").strip()
        if not s:
            return None
        try:
            return float(s)
        except ValueError:
            return None

    for col, label in COLUMN_MAP.items():
        pl = pl_store.get(label.lower())
        if pl is None:
            continue
        cur = sheet_val(col)
        name = COLUMN_NAMES.get(col, label)
        if cur is None or cur == 0.0:
            out.append({"col": col, "name": name, "pl": pl, "sheet": cur,
                        "action": "fill", "reason": "target cell blank"})
        elif abs(pl - cur) <= TOLERANCE:
            out.append({"col": col, "name": name, "pl": pl, "sheet": cur,
                        "action": "agree", "reason": ""})
        else:
            out.append({
                "col": col, "name": name, "pl": pl, "sheet": cur,
                "action": "update" if allow_overwrite else "report",
                "reason": "P&L is the reconciled source of truth (Angell 2026-08-14)"
                          if allow_overwrite else "differs; --allow-overwrite not set",
            })

    # Combined lines: report only, never written.
    for label, (a, b, name) in COMBINED_ONLY.items():
        pl = pl_store.get(label.lower())
        if pl is None:
            continue
        va, vb = sheet_val(a), sheet_val(b)
        if va is None and vb is None:
            continue
        total = (va or 0) + (vb or 0)
        out.append({
            "col": None, "name": name, "pl": pl, "sheet": total,
            "action": "agree" if abs(pl - total) <= TOLERANCE else "report",
            "reason": "one P&L line covers two sheet columns; cannot apportion",
        })
    return out


def summarize(findings):
    counts = {}
    for f in findings:
        counts[f["action"]] = counts.get(f["action"], 0) + 1
    return counts
