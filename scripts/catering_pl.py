"""Pure extraction + mapping for TRIS Preliminary Financial Statement catering revenue.

No network, no Google, no Gmail -- so this is unit-testable in isolation.

WHY GL-LABEL MATCHING, NOT ROW NUMBERS
--------------------------------------
The catering block sits at a different row on every store sheet (Cherrywood 51,
Arboretum 52, South Congress 51, Round Rock 53, Menchaca 50 in P03 2026) and
shifts between periods as TRIS adds accounts. Row-number extraction silently
reads the wrong account. We match the exact GL label in column A instead.

WHY "Total 4300", NOT the component rows
----------------------------------------
Verified against 9 hand-keyed historical cells: sheet column BF equals
`Total 4300 - Direct Catering Sales`, which ALREADY INCLUDES the tax-exempt
account 4313. Summing the components (or additionally writing 4313 into BG)
double-counts catering revenue. BG/BK are therefore never written -- see
UNMAPPED_ACCOUNTS.
"""

import re

# P&L worksheet name (and known aliases) -> Sales & Trends tab name.
STORE_SHEETS = {
    "Cherrywood": "Cherrywood Monthly Sales ",
    "Arboretum": "Arbor Monthly Sales",
    "South Congress": "TsoCo Monthly Sales",
    "Round Rock": "Round Rock Monthly Sales",
    "Menchaca": "Menchaca Monthly Sales",
}

# Alternate P&L tab spellings seen across periods -> canonical key above.
SHEET_ALIASES = {
    "arbor": "Arboretum",
    "arboretum": "Arboretum",
    "cherrywood": "Cherrywood",
    "south congress": "South Congress",
    "tsoco": "South Congress",
    "soco": "South Congress",
    "round rock": "Round Rock",
    "roundrock": "Round Rock",
    "menchaca": "Menchaca",
}

# Sales & Trends column -> exact P&L GL label. One label per column, on purpose:
# every mapping below was verified cent-for-cent against hand-keyed history.
COLUMN_MAP = {
    "BF": "Total 4300 - Direct Catering Sales",
    "BH": "4420 - Lunchdrop Catering Sales",
    "BJ": "Total 4440 - EZ Cater Sales",
    "BM": "4445 - America To Go Catering Sales",
}

# Accounts deliberately NOT written, with the reason. Surfaced in the report so
# a real person decides rather than the script guessing.
UNMAPPED_ACCOUNTS = {
    "4313 - Flex In-house Catering Sales (Tax Exempt)":
        "already inside Total 4300 -> BF; writing it to BG would double-count",
    "4441 - EZ Cater Sales (Tax Exempt)":
        "already inside Total 4440 -> BJ; writing it to BK would double-count",
    "4130 - Square Catering Sales":
        "sits in Direct Sales, NOT in Total 4300; no verified Sales & Trends column",
}

TOLERANCE = 0.50  # cents-vs-whole-dollars hand keying is not a real disagreement


def column_index(letter):
    """'BF' -> 57 (zero-based)."""
    n = 0
    for ch in letter.strip().upper():
        if not ("A" <= ch <= "Z"):
            raise ValueError(f"bad column letter: {letter!r}")
        n = n * 26 + (ord(ch) - 64)
    return n - 1


def period_label(year, period):
    """Sales & Trends row label. Period 3 of FY2026 -> '3.2026'."""
    return f"{int(period)}.{int(year)}"


def parse_period_header(value):
    """'Period 3, 2026' -> (2026, 3); anything else -> None."""
    m = re.match(r"^Period\s+(\d{1,2}),\s*(\d{4})$", str(value or "").strip())
    return (int(m.group(2)), int(m.group(1))) if m else None


def canonical_sheet(name):
    return SHEET_ALIASES.get(str(name or "").strip().lower())


def _period_columns(ws, header_row=5):
    out = {}
    for c in range(1, ws.max_column + 1):
        parsed = parse_period_header(ws.cell(row=header_row, column=c).value)
        if parsed:
            out[parsed] = c
    return out


def _label_rows(ws):
    """Exact GL label -> [row numbers]. Labels repeat (header + detail), so all rows are kept."""
    out = {}
    for r in range(1, ws.max_row + 1):
        label = str(ws.cell(row=r, column=1).value or "").strip()
        if label:
            out.setdefault(label, []).append(r)
    return out


def extract_workbook(workbook):
    """openpyxl workbook (data_only=True) -> {tab: {'3.2026': {'BF': 1190.25, ...}}}.

    Returns (data, findings). findings lists non-fatal anomalies worth reporting.
    """
    data = {}
    findings = []
    seen = set()

    for ws in workbook.worksheets:
        key = canonical_sheet(ws.title)
        if key is None:
            continue
        seen.add(key)
        tab = STORE_SHEETS[key]
        periods = _period_columns(ws)
        if not periods:
            findings.append(f"{ws.title}: no 'Period N, YYYY' headers on row 5 -- sheet skipped")
            continue
        labels = _label_rows(ws)

        for (year, period), col in periods.items():
            bucket = {}
            for target, gl_label in COLUMN_MAP.items():
                total = None
                for r in labels.get(gl_label, []):
                    v = ws.cell(row=r, column=col).value
                    if isinstance(v, (int, float)):
                        total = (total or 0) + v
                if total is not None:
                    bucket[target] = round(float(total), 2)
            if bucket:
                data.setdefault(tab, {})[period_label(year, period)] = bucket

        for gl_label, reason in UNMAPPED_ACCOUNTS.items():
            if gl_label in labels:
                has_value = any(
                    isinstance(ws.cell(row=r, column=c).value, (int, float))
                    for r in labels[gl_label] for c in periods.values()
                )
                if has_value:
                    findings.append(f"{ws.title}: '{gl_label}' has data but is not written ({reason})")

    for key in STORE_SHEETS:
        if key not in seen:
            findings.append(f"expected store sheet '{key}' not found in the P&L workbook")

    return data, findings


def plan_updates(extracted, sheet_values, allow_overwrite=False, only_period=None):
    """Decide, per cell, whether to write. Returns (updates, report).

    Statuses:
      fill     -> sheet blank, P&L has a value: safe to write
      match    -> agrees within TOLERANCE: no-op
      diff     -> both present and disagree: SKIPPED unless allow_overwrite
      blocked  -> paired tax-exempt column is populated: never auto-resolved
      no_row   -> sheet has no row for that fiscal period yet
    """
    updates = []
    report = []

    for tab, periods in extracted.items():
        rows = sheet_values.get(tab) or []
        row_of = {}
        for i, row in enumerate(rows):
            key = str(row[0]).strip() if row else ""
            if key and key not in row_of:
                row_of[key] = i

        for label in sorted(periods, key=lambda s: (int(s.split(".")[1]), int(s.split(".")[0]))):
            if only_period and label != only_period:
                continue
            values = periods[label]
            if label not in row_of:
                report.append({"tab": tab, "period": label, "column": "-",
                               "status": "no_row", "pl": None, "sheet": None})
                continue

            row = rows[row_of[label]]
            row_number = row_of[label] + 1

            def cell(letter):
                i = column_index(letter)
                return row[i] if len(row) > i else ""

            for col in sorted(values):
                pl_value = values[col]
                current = cell(col)

                # EZCater: some months were hand-split across BJ (taxable) and
                # BK (tax-exempt). Total 4440 covers both, so writing BJ while BK
                # holds a value would double-count. Never guess -- report it.
                blocked = None
                if col == "BJ" and cell("BK") not in ("", None):
                    blocked = "BK (EZCater non-Tax) is populated; BJ total would double-count"

                entry = {"tab": tab, "period": label, "column": col, "row": row_number,
                         "pl": pl_value, "sheet": current}

                if blocked:
                    entry.update(status="blocked", reason=blocked)
                elif current in ("", None):
                    entry.update(status="fill")
                    updates.append({"tab": tab, "row": row_number, "column": col, "value": pl_value})
                else:
                    try:
                        delta = pl_value - float(str(current).replace("$", "").replace(",", ""))
                    except (TypeError, ValueError):
                        entry.update(status="diff", reason=f"sheet value not numeric: {current!r}")
                        report.append(entry)
                        continue
                    entry["delta"] = round(delta, 2)
                    if abs(delta) < TOLERANCE:
                        entry.update(status="match")
                    else:
                        entry.update(status="diff")
                        if allow_overwrite:
                            updates.append({"tab": tab, "row": row_number,
                                            "column": col, "value": pl_value})
                report.append(entry)

    return updates, report


def summarize(report):
    counts = {}
    for item in report:
        counts[item["status"]] = counts.get(item["status"], 0) + 1
    return counts
