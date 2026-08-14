"""Tests for store-tab P&L extraction and comparison.

The grain guard is the most important thing here: a fiscal-period P&L written
into calendar-month rows understates revenue by 10-27%, silently.
"""

import datetime as dt
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

import store_pl as sp


# --------------------------------------------------------------- amount parsing

def test_parse_amount_plain():
    assert sp.parse_amount("43,123") == 43123.0


def test_parse_amount_tris_double_dash_negative():
    # TRIS renders negatives as "--1,266"; a naive float() would crash.
    assert sp.parse_amount("--1,266") == -1266.0


def test_parse_amount_blank_and_dash():
    assert sp.parse_amount("") is None
    assert sp.parse_amount("-") is None
    assert sp.parse_amount(None) is None


# ------------------------------------------------------------------ grain guard

def test_parse_period_range():
    assert sp.parse_period_range("06/01/2026 - 06/30/2026") == (
        dt.date(2026, 6, 1), dt.date(2026, 6, 30))


def test_whole_calendar_month_accepts_june():
    assert sp.is_whole_calendar_month(dt.date(2026, 6, 1), dt.date(2026, 6, 30))


def test_whole_calendar_month_accepts_february_leap():
    assert sp.is_whole_calendar_month(dt.date(2028, 2, 1), dt.date(2028, 2, 29))


def test_whole_calendar_month_rejects_fiscal_period():
    # P07 2026 = Jun 14 - Jul 11: a 28-day fiscal window, NOT a calendar month.
    assert not sp.is_whole_calendar_month(dt.date(2026, 6, 14), dt.date(2026, 7, 11))


def test_whole_calendar_month_rejects_partial_month():
    assert not sp.is_whole_calendar_month(dt.date(2026, 6, 1), dt.date(2026, 6, 15))


def _rows(date_header="06/01/2026 - 06/30/2026"):
    return [
        ["Profit & Loss - Location"],
        [],
        [date_header],
        [],
        ["", "Corporate Office", "", "Tso Chinese Arboretum Crossing", "",
         "Tso Chinese Cherrywood", "", "Tso Chinese Menchaca", "",
         "Tso Chinese Round Rock", "", "TsoCo South Congress", "", "Total"],
        ["Total Tray Kiosk Sales", "", "", "13,008", "", "10,611", "",
         "15,004", "", "12,178", "", "22,878", "", "73,679"],
        ["Total Uber Eats Sales", "", "", "33,974", "", "38,685", "",
         "18,623", "", "32,962", "", "45,573", "", "169,817"],
        ["Total Grafana Sales", "", "", "72,678", "", "67,650", "",
         "47,405", "", "54,822", "", "65,534", "", "308,089"],
        ["Grafana Discounts", "", "", "--1,266", "", "--1,172", "",
         "--900", "", "--800", "", "--1,000", "", "--5,138"],
    ]


def test_extract_rejects_fiscal_period_export():
    with pytest.raises(ValueError, match="not a whole calendar month"):
        sp.extract(_rows("06/14/2026 - 07/11/2026"))


def test_extract_rejects_missing_date_header():
    with pytest.raises(ValueError, match="no parseable date range"):
        sp.extract(_rows("Preliminary Financial Statements"))


def test_extract_maps_all_five_stores():
    got = sp.extract(_rows())
    assert set(got["stores"]) == {
        "Cherrywood Monthly Sales ", "Arbor Monthly Sales", "TsoCo Monthly Sales",
        "Round Rock Monthly Sales", "Menchaca Monthly Sales"}
    assert got["month"] == (2026, 6)


def test_extract_excludes_corporate_and_total():
    got = sp.extract(_rows())
    # Corporate Office and Total are not stores; a mis-parse here would write
    # company-wide figures into one store's row.
    assert not any("corporate" in t.lower() or t.lower() == "total"
                   for t in got["stores"])


def test_extract_reads_correct_store_column():
    got = sp.extract(_rows())
    assert got["stores"]["Cherrywood Monthly Sales "]["total tray kiosk sales"] == 10611.0
    assert got["stores"]["Arbor Monthly Sales"]["total tray kiosk sales"] == 13008.0


def test_extract_keeps_negative_discounts():
    got = sp.extract(_rows())
    assert got["stores"]["Arbor Monthly Sales"]["grafana discounts"] == -1266.0


# -------------------------------------------------------------------- comparing

def _sheet_row(**over):
    row = [""] * 60
    row[0] = "6.2026"
    row[12] = "35,000"   # M carryout
    row[17] = "35,949"   # R delivery
    row[22] = "10,669"   # W kiosk
    row[32] = "42,319"   # AG ubereats
    for k, v in over.items():
        row[int(k[1:])] = v
    return row


def test_agreement_within_tolerance_is_noop():
    pl = {"total tray kiosk sales": 10669.50}
    f = sp.compare_row(pl, _sheet_row())
    assert [x["action"] for x in f if x["name"] == "Kiosk"] == ["agree"]


def test_blank_cell_is_filled():
    pl = {"total grubhub sales": 1379.0}
    f = sp.compare_row(pl, _sheet_row())
    grub = [x for x in f if x["name"] == "Grubhub"][0]
    assert grub["action"] == "fill"


def test_zero_cell_counts_as_blank():
    pl = {"total grubhub sales": 1379.0}
    f = sp.compare_row(pl, _sheet_row(c47="0"))
    assert [x for x in f if x["name"] == "Grubhub"][0]["action"] == "fill"


def test_real_disagreement_is_reported_not_written_by_default():
    # UberEats: P&L 38,685 vs sheet 42,319 -- the systematic 8% gap.
    pl = {"total uber eats sales": 38685.0}
    f = sp.compare_row(pl, _sheet_row())
    ue = [x for x in f if x["name"] == "UberEats"][0]
    assert ue["action"] == "report"


def test_real_disagreement_is_updated_when_authorized():
    pl = {"total uber eats sales": 38685.0}
    f = sp.compare_row(pl, _sheet_row(), allow_overwrite=True)
    ue = [x for x in f if x["name"] == "UberEats"][0]
    assert ue["action"] == "update"
    assert ue["pl"] == 38685.0


def test_combined_grafana_line_is_never_written():
    # One P&L line covers two sheet columns, so it can only be reported.
    pl = {"total grafana sales": 67650.0}
    f = sp.compare_row(pl, _sheet_row())
    combo = [x for x in f if x["name"] == "Carryout+Delivery"][0]
    assert combo["col"] is None
    assert combo["action"] == "report"


def test_combined_grafana_line_even_with_overwrite_has_no_column():
    pl = {"total grafana sales": 67650.0}
    f = sp.compare_row(pl, _sheet_row(), allow_overwrite=True)
    combo = [x for x in f if x["name"] == "Carryout+Delivery"][0]
    assert combo["col"] is None  # nothing to write into


def test_missing_pl_label_is_skipped_silently():
    f = sp.compare_row({}, _sheet_row())
    assert f == []


def test_summarize_counts_actions():
    pl = {"total uber eats sales": 38685.0, "total tray kiosk sales": 10669.0}
    counts = sp.summarize(sp.compare_row(pl, _sheet_row()))
    assert counts.get("agree") == 1
    assert counts.get("report") == 1
