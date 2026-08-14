"""Tests for the catering P&L ingestion: fiscal calendar, extraction, write planning.

Run: python3 -m pytest tests/test_catering_pl.py -q
"""

import datetime as dt
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import catering_pl as cp
import fiscal_calendar as fc


# --------------------------------------------------------------------------
# Fiscal calendar
# --------------------------------------------------------------------------

def test_anchor_period_matches_pl_header():
    # P03 2026 P&L header: "12 Periods Ending 03/21/2026"
    assert fc.period_end(2026, 3) == dt.date(2026, 3, 21)


def test_period_8_2026_matches_shipped_vendor_automation():
    # Independently fixed by the vendor-profitability automation: P8 = Jul 12 - Aug 8.
    assert fc.period_range(2026, 8) == (dt.date(2026, 7, 12), dt.date(2026, 8, 8))


def test_every_period_ends_on_saturday():
    for period in range(1, 14):
        assert fc.period_end(2026, period).weekday() == 5


def test_periods_are_28_days_and_contiguous():
    for period in range(1, 13):
        end = fc.period_end(2026, period)
        nxt = fc.period_start(2026, period + 1)
        assert (nxt - end).days == 1
        start = fc.period_start(2026, period)
        assert (end - start).days == 27


def test_year_boundary_is_contiguous():
    assert (fc.period_start(2027, 1) - fc.period_end(2026, 13)).days == 1


def test_period_for_date_endpoints_inclusive():
    start, end = fc.period_range(2026, 8)
    assert fc.period_for_date(start) == (2026, 8)
    assert fc.period_for_date(end) == (2026, 8)
    assert fc.period_for_date(end + dt.timedelta(days=1)) == (2026, 9)


def test_most_recent_closed_period_excludes_in_flight():
    _, end = fc.period_range(2026, 8)
    # On the final day, P8 is not yet closed.
    assert fc.most_recent_closed_period(end) == (2026, 7)
    # Day after, it is.
    assert fc.most_recent_closed_period(end + dt.timedelta(days=1)) == (2026, 8)


def test_most_recent_closed_period_rolls_back_across_year():
    day_after = fc.period_end(2026, 13) + dt.timedelta(days=1)
    assert fc.most_recent_closed_period(day_after) == (2026, 13)
    assert fc.most_recent_closed_period(fc.period_start(2027, 1)) == (2026, 13)


def test_subject_and_label_formatting():
    assert fc.subject_for(2026, 8) == "TSO Preliminary Financial Statement Package | P08 2026"
    assert fc.subject_for(2026, 12) == "TSO Preliminary Financial Statement Package | P12 2026"
    assert fc.label_for(2026, 8) == "8.2026"


def test_invalid_period_rejected():
    with pytest.raises(ValueError):
        fc.period_end(2026, 14)
    with pytest.raises(ValueError):
        fc.period_end(2026, 0)


# --------------------------------------------------------------------------
# Column helpers
# --------------------------------------------------------------------------

@pytest.mark.parametrize("letter,index", [("A", 0), ("Z", 25), ("AA", 26), ("BF", 57), ("BZ", 77)])
def test_column_index(letter, index):
    assert cp.column_index(letter) == index


def test_column_index_rejects_junk():
    with pytest.raises(ValueError):
        cp.column_index("B2")


def test_parse_period_header():
    assert cp.parse_period_header("Period 3, 2026") == (2026, 3)
    assert cp.parse_period_header("Period 12, 2025") == (2025, 12)
    assert cp.parse_period_header("Total") is None
    assert cp.parse_period_header(None) is None


def test_canonical_sheet_aliases():
    assert cp.canonical_sheet("South Congress") == "South Congress"
    assert cp.canonical_sheet("TsoCo") == "South Congress"
    assert cp.canonical_sheet("  arbor ") == "Arboretum"
    assert cp.canonical_sheet("Balance Sheet") is None


# --------------------------------------------------------------------------
# Extraction (synthetic workbook -> no network, no fixtures)
# --------------------------------------------------------------------------

def _workbook(sheet_name, rows, periods=("Period 3, 2026",)):
    """Build an in-memory P&L-shaped workbook. `rows` is [(gl_label, [values...])]."""
    openpyxl = pytest.importorskip("openpyxl")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = sheet_name
    for i, header in enumerate(periods):
        ws.cell(row=5, column=3 + i * 3, value=header)
    for r, (label, values) in enumerate(rows, start=10):
        ws.cell(row=r, column=1, value=label)
        for i, v in enumerate(values):
            if v is not None:
                ws.cell(row=r, column=3 + i * 3, value=v)
    return wb


def test_extract_maps_verified_columns():
    wb = _workbook("Cherrywood", [
        ("Total 4300 - Direct Catering Sales", [1190.25]),
        ("4420 - Lunchdrop Catering Sales", [4888.10]),
        ("Total 4440 - EZ Cater Sales", [886.30]),
        ("4445 - America To Go Catering Sales", [1452.62]),
    ])
    data, findings = cp.extract_workbook(wb)
    assert data["Cherrywood Monthly Sales "]["3.2026"] == {
        "BF": 1190.25, "BH": 4888.10, "BJ": 886.30, "BM": 1452.62,
    }
    assert not [f for f in findings if "Cherrywood" in f]


def test_extract_ignores_row_position():
    """Same accounts at different rows must extract identically -- row numbers move."""
    a = _workbook("Menchaca", [
        ("Total 4300 - Direct Catering Sales", [100.0]),
        ("4420 - Lunchdrop Catering Sales", [200.0]),
    ])
    b = _workbook("Menchaca", [
        ("6112 - Wages", [999.0]),
        ("noise", [None]),
        ("4420 - Lunchdrop Catering Sales", [200.0]),
        ("Total 4300 - Direct Catering Sales", [100.0]),
    ])
    da, _ = cp.extract_workbook(a)
    db, _ = cp.extract_workbook(b)
    assert da == db


def test_extract_never_maps_tax_exempt_columns():
    """Regression: 4313/4441 are inside the Total rows. Writing BG/BK double-counts."""
    wb = _workbook("Round Rock", [
        ("Total 4300 - Direct Catering Sales", [2283.05]),
        ("4313 - Flex In-house Catering Sales (Tax Exempt)", [2090.10]),
        ("4441 - EZ Cater Sales (Tax Exempt)", [50.0]),
    ])
    data, findings = cp.extract_workbook(wb)
    bucket = data["Round Rock Monthly Sales"]["3.2026"]
    assert "BG" not in bucket and "BK" not in bucket
    assert bucket["BF"] == 2283.05          # the total, not total+tax-exempt
    assert any("4313" in f for f in findings)
    assert any("4441" in f for f in findings)


def test_extract_reports_unmapped_square_catering():
    wb = _workbook("Round Rock", [
        ("Total 4300 - Direct Catering Sales", [10.0]),
        ("4130 - Square Catering Sales", [777.0]),
    ])
    _, findings = cp.extract_workbook(wb)
    assert any("4130" in f for f in findings)


def test_extract_multiple_periods():
    wb = _workbook("Arbor", [
        ("Total 4300 - Direct Catering Sales", [1.0, 2.0, 3.0]),
    ], periods=("Period 1, 2026", "Period 2, 2026", "Period 3, 2026"))
    data, _ = cp.extract_workbook(wb)
    tab = data["Arbor Monthly Sales"]
    assert tab["1.2026"]["BF"] == 1.0
    assert tab["3.2026"]["BF"] == 3.0


def test_extract_flags_missing_store_sheet():
    wb = _workbook("Cherrywood", [("Total 4300 - Direct Catering Sales", [1.0])])
    _, findings = cp.extract_workbook(wb)
    assert any("Menchaca" in f for f in findings)


def test_extract_skips_non_store_sheets():
    wb = _workbook("Balance Sheet", [("Total 4300 - Direct Catering Sales", [5.0])])
    data, _ = cp.extract_workbook(wb)
    assert data == {}


# --------------------------------------------------------------------------
# Write planning -- the safety-critical half
# --------------------------------------------------------------------------

TAB = "Cherrywood Monthly Sales "


def _sheet(label="3.2026", **cols):
    """One sheet row at index 3 (row 4), with given column letters populated."""
    width = cp.column_index("BZ") + 1
    row = [""] * width
    row[0] = label
    for letter, value in cols.items():
        row[cp.column_index(letter)] = value
    return {TAB: [["", "", ""], ["", "", ""], ["", "", ""], row]}


def test_fill_writes_only_blank_cells():
    extracted = {TAB: {"3.2026": {"BF": 1190.25}}}
    updates, report = cp.plan_updates(extracted, _sheet())
    assert updates == [{"tab": TAB, "row": 4, "column": "BF", "value": 1190.25}]
    assert cp.summarize(report) == {"fill": 1}


def test_matching_value_is_a_noop():
    extracted = {TAB: {"3.2026": {"BF": 1190.25}}}
    updates, report = cp.plan_updates(extracted, _sheet(BF=1190.25))
    assert updates == []
    assert report[0]["status"] == "match"


def test_rounding_difference_counts_as_match():
    """History was hand-keyed to whole dollars; $0.25 is not a disagreement."""
    extracted = {TAB: {"3.2026": {"BF": 1190.25}}}
    updates, report = cp.plan_updates(extracted, _sheet(BF=1190))
    assert updates == []
    assert report[0]["status"] == "match"


def test_real_disagreement_is_never_silently_overwritten():
    extracted = {TAB: {"3.2026": {"BH": 4888.10}}}
    updates, report = cp.plan_updates(extracted, _sheet(BH=6732))
    assert updates == []
    assert report[0]["status"] == "diff"
    assert report[0]["delta"] == pytest.approx(-1843.90)


def test_disagreement_written_only_with_explicit_overwrite():
    extracted = {TAB: {"3.2026": {"BH": 4888.10}}}
    updates, _ = cp.plan_updates(extracted, _sheet(BH=6732), allow_overwrite=True)
    assert updates == [{"tab": TAB, "row": 4, "column": "BH", "value": 4888.10}]


def test_ezcater_blocked_when_tax_exempt_split_is_populated():
    """BJ holds the EZCater TOTAL; if BK is hand-filled, writing BJ double-counts."""
    extracted = {TAB: {"3.2026": {"BJ": 4648.45}}}
    updates, report = cp.plan_updates(extracted, _sheet(BK=3810))
    assert updates == []
    assert report[0]["status"] == "blocked"
    assert "double-count" in report[0]["reason"]


def test_ezcater_block_beats_overwrite_flag():
    extracted = {TAB: {"3.2026": {"BJ": 4648.45}}}
    updates, _ = cp.plan_updates(extracted, _sheet(BK=3810), allow_overwrite=True)
    assert updates == []


def test_missing_period_row_is_reported_not_invented():
    extracted = {TAB: {"9.2026": {"BF": 1.0}}}
    updates, report = cp.plan_updates(extracted, _sheet("3.2026"))
    assert updates == []
    assert report[0]["status"] == "no_row"


def test_non_numeric_sheet_value_is_a_diff_not_a_crash():
    extracted = {TAB: {"3.2026": {"BF": 10.0}}}
    updates, report = cp.plan_updates(extracted, _sheet(BF="n/a"))
    assert updates == []
    assert report[0]["status"] == "diff"


def test_currency_formatted_sheet_value_parses():
    extracted = {TAB: {"3.2026": {"BF": 1190.25}}}
    _, report = cp.plan_updates(extracted, _sheet(BF="$1,190.25"))
    assert report[0]["status"] == "match"


def test_only_period_filter_scopes_the_write():
    extracted = {TAB: {"3.2026": {"BF": 1.0}, "2.2026": {"BF": 2.0}}}
    sheet = {TAB: [["", "", ""], ["", "", ""], ["", "", ""],
                   ["3.2026"] + [""] * 80, ["2.2026"] + [""] * 80]}
    updates, _ = cp.plan_updates(extracted, sheet, only_period="3.2026")
    assert len(updates) == 1 and updates[0]["row"] == 4


def test_unknown_tab_in_sheet_values_yields_no_row():
    extracted = {"Menchaca Monthly Sales": {"3.2026": {"BF": 1.0}}}
    updates, report = cp.plan_updates(extracted, _sheet())
    assert updates == []
    assert report[0]["status"] == "no_row"
