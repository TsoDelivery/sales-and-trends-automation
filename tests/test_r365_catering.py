"""Unit tests for the R365 catering source: pure logic, no network.

The network layer (fetch_lines) is exercised by scripts/validate-r365-catering.py
against real data. These tests pin the aggregation and calendar logic that the
grain bug lived in.
"""

import datetime as dt
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))

import r365_catering as rc


# ------------------------------------------------------------- month labelling

def test_month_label_is_calendar_month_not_fiscal_period():
    # This is the whole point: '2026-03' is March, and it renders as the row
    # label '3.2026' -- which elsewhere in this repo means fiscal period 3.
    assert rc.month_label("2026-03") == "3.2026"
    assert rc.month_label("2025-11") == "11.2025"
    assert rc.month_label("2026-01") == "1.2026"


def test_month_label_strips_leading_zeros():
    assert rc.month_label("2026-09") == "9.2026"


# --------------------------------------------------------------- month ranges

def test_month_range_only_returns_whole_months():
    # March is partial on both ends -> excluded. April and May are whole.
    months = rc.month_range(dt.date(2026, 3, 15), dt.date(2026, 6, 10))
    assert months == ["2026-04", "2026-05"]


def test_month_range_includes_exact_month_boundaries():
    months = rc.month_range(dt.date(2026, 3, 1), dt.date(2026, 4, 30))
    assert months == ["2026-03", "2026-04"]


def test_month_range_crosses_year_boundary():
    months = rc.month_range(dt.date(2025, 11, 1), dt.date(2026, 2, 28))
    assert months == ["2025-11", "2025-12", "2026-01", "2026-02"]


def test_month_range_empty_when_no_whole_month():
    assert rc.month_range(dt.date(2026, 3, 5), dt.date(2026, 3, 20)) == []


def test_month_bounds_handles_february_and_december():
    assert rc.month_bounds("2026-02") == (dt.date(2026, 2, 1), dt.date(2026, 2, 28))
    assert rc.month_bounds("2025-12") == (dt.date(2025, 12, 1), dt.date(2025, 12, 31))
    assert rc.month_bounds("2026-03") == (dt.date(2026, 3, 1), dt.date(2026, 3, 31))


def test_month_bounds_leap_year():
    assert rc.month_bounds("2028-02")[1] == dt.date(2028, 2, 29)


# ---------------------------------------------------------------- aggregation

def record(date, store, account, net, approved=True):
    return {"date": date, "month": date[:7], "store": store,
            "tab": rc.STORE_TABS.get(store), "account": account,
            "account_name": f"acct {account}", "net": net, "approved": approved}


def test_aggregate_sums_by_month_store_and_column():
    records = [
        record("2026-03-02", "Tso Chinese Cherrywood", "4420", 100.00),
        record("2026-03-17", "Tso Chinese Cherrywood", "4420", 50.25),
        record("2026-04-01", "Tso Chinese Cherrywood", "4420", 7.00),
    ]
    agg = rc.aggregate(records)
    tab = rc.STORE_TABS["Tso Chinese Cherrywood"]
    assert agg[tab]["3.2026"]["BH"] == 150.25
    assert agg[tab]["4.2026"]["BH"] == 7.00


def test_aggregate_keeps_stores_separate():
    records = [
        record("2026-03-02", "Tso Chinese Cherrywood", "4420", 100.00),
        record("2026-03-02", "Tso Chinese Menchaca", "4420", 200.00),
    ]
    agg = rc.aggregate(records)
    assert agg[rc.STORE_TABS["Tso Chinese Cherrywood"]]["3.2026"]["BH"] == 100.00
    assert agg[rc.STORE_TABS["Tso Chinese Menchaca"]]["3.2026"]["BH"] == 200.00


def test_aggregate_combines_ezcater_accounts_into_one_column():
    # 4440 taxable + 4441 tax-exempt + 4442 discounts all land in BJ.
    records = [
        record("2026-03-02", "Tso Chinese Cherrywood", "4440", 500.00),
        record("2026-03-03", "Tso Chinese Cherrywood", "4441", 120.00),
        record("2026-03-04", "Tso Chinese Cherrywood", "4442", -20.00),
    ]
    agg = rc.aggregate(records)
    assert agg[rc.STORE_TABS["Tso Chinese Cherrywood"]]["3.2026"]["BJ"] == 600.00


def test_aggregate_ignores_unmapped_accounts():
    # 4130 Square is deliberately not mapped -- BF is unverified.
    records = [record("2026-03-02", "Tso Chinese Cherrywood", "4130", 999.00)]
    assert rc.aggregate(records) == {}


def test_aggregate_ignores_unknown_stores():
    records = [record("2026-03-02", "Corporate Office", "4420", 999.00)]
    assert rc.aggregate(records) == {}


def test_aggregate_nets_refunds_rather_than_dropping_them():
    records = [
        record("2026-03-02", "Tso Chinese Cherrywood", "4420", 100.00),
        record("2026-03-09", "Tso Chinese Cherrywood", "4420", -30.00),
    ]
    agg = rc.aggregate(records)
    assert agg[rc.STORE_TABS["Tso Chinese Cherrywood"]]["3.2026"]["BH"] == 70.00


def test_aggregate_does_not_write_bf():
    # BF must stay out of the mapping until its account set is confirmed.
    assert "BF" not in rc.COLUMN_ACCOUNTS
    assert "BF" in rc.UNVERIFIED_COLUMNS


def test_bf_exclusion_is_documented_with_a_reason():
    assert len(rc.UNVERIFIED_COLUMNS["BF"]) > 40


# ------------------------------------------------------------------- coverage

def test_coverage_gaps_flags_missing_store_month():
    records = [record("2026-03-02", "Tso Chinese Cherrywood", "4420", 100.00)]
    gaps = rc.coverage_gaps(records, ["2026-03"])
    stores = {store for _, store in gaps}
    assert "Tso Chinese Cherrywood" not in stores
    assert "Tso Chinese Menchaca" in stores
    assert len(gaps) == len(rc.STORE_TABS) - 1


def test_coverage_gaps_empty_when_every_store_reported():
    records = [record("2026-03-02", store, "4420", 10.0) for store in rc.STORE_TABS]
    assert rc.coverage_gaps(records, ["2026-03"]) == []


def test_unapproved_is_surfaced_not_dropped():
    records = [
        record("2026-03-02", "Tso Chinese Cherrywood", "4420", 100.00, approved=True),
        record("2026-03-03", "Tso Chinese Cherrywood", "4420", 5.00, approved=False),
    ]
    assert len(rc.unapproved(records)) == 1
    # ...and it still counts toward the total; the report is the safeguard.
    agg = rc.aggregate(records)
    assert agg[rc.STORE_TABS["Tso Chinese Cherrywood"]]["3.2026"]["BH"] == 105.00


# ------------------------------------------------------------------- windowing

def test_windows_respect_the_31_day_api_limit():
    spans = list(rc.windows(dt.date(2026, 1, 1), dt.date(2026, 3, 31)))
    assert all((hi - lo).days <= 31 for lo, hi in spans)
    assert spans[0][0] == dt.date(2026, 1, 1)
    # Half-open: the last window ends the day after `end`.
    assert spans[-1][1] == dt.date(2026, 4, 1)


def test_windows_cover_every_day_exactly_once():
    start, end = dt.date(2026, 1, 1), dt.date(2026, 3, 31)
    days = []
    for lo, hi in rc.windows(start, end):
        cursor = lo
        while cursor < hi:
            days.append(cursor)
            cursor += dt.timedelta(days=1)
    assert len(days) == len(set(days)) == (end - start).days + 1


def test_windows_single_day_range():
    spans = list(rc.windows(dt.date(2026, 5, 5), dt.date(2026, 5, 5)))
    assert spans == [(dt.date(2026, 5, 5), dt.date(2026, 5, 6))]


# ------------------------------------------------------------ config integrity

def test_store_tabs_match_the_pl_extractor_tabs():
    import catering_pl as cp
    assert set(rc.STORE_TABS.values()) == set(cp.STORE_SHEETS.values())


def test_audit_accounts_include_every_mapped_account():
    mapped = {n for nums in rc.COLUMN_ACCOUNTS.values() for n in nums}
    assert mapped <= set(rc.AUDIT_ACCOUNTS)


def test_post_lag_pad_is_generous_enough_to_catch_late_journals():
    # Journals post after the sale; too small a pad silently truncates months.
    assert rc.POST_LAG_PAD >= 7
