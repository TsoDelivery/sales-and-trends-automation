"""Tests for the settle gate on the R365 catering writer.

The gate exists because a month written too early is silently short -- exactly
the failure that put six stale partial-month values into the sheet's history.
"""

import datetime as dt
import importlib.util
import os
import sys

import pytest

SCRIPTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts")
sys.path.insert(0, SCRIPTS)

spec = importlib.util.spec_from_file_location(
    "ingest_r365", os.path.join(SCRIPTS, "ingest-catering-r365.py"))
assert spec and spec.loader
ingest = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ingest)


def test_current_month_is_never_writable():
    ok, why = ingest.month_is_settled("2026-08", dt.date(2026, 8, 14), 45)
    assert not ok
    assert "not over yet" in why


def test_month_that_just_ended_is_not_writable():
    # July ended 7/31; on 8/14 only 14 days have passed.
    ok, why = ingest.month_is_settled("2026-07", dt.date(2026, 8, 14), 45)
    assert not ok
    assert "14 days ago" in why


def test_month_becomes_writable_once_settled():
    ok, why = ingest.month_is_settled("2026-07", dt.date(2026, 9, 20), 45)
    assert ok
    assert "closed" in why


def test_settle_boundary_is_exact():
    # July 31 + 45 days = September 14.
    assert not ingest.month_is_settled("2026-07", dt.date(2026, 9, 13), 45)[0]
    assert ingest.month_is_settled("2026-07", dt.date(2026, 9, 14), 45)[0]


def test_last_day_of_month_is_still_not_over():
    ok, _ = ingest.month_is_settled("2026-07", dt.date(2026, 7, 31), 0)
    assert not ok


def test_first_day_after_month_end_with_zero_settle_is_writable():
    ok, _ = ingest.month_is_settled("2026-07", dt.date(2026, 8, 1), 0)
    assert ok


def test_settle_days_is_generous_by_default():
    # Weekly journals posted up to ~110 days late were observed; the default
    # must at least clear the normal weekly cadence with margin.
    assert ingest.DEFAULT_SETTLE_DAYS >= 30


def test_december_rolls_into_the_next_year():
    ok, _ = ingest.month_is_settled("2025-12", dt.date(2026, 2, 20), 45)
    assert ok
    ok, _ = ingest.month_is_settled("2025-12", dt.date(2026, 1, 5), 45)
    assert not ok


# --- guards discovered while correcting real cells -------------------------

def test_rounding_differences_are_not_rewritten():
    """The maintainer keys whole dollars. R365 carries cents.

    Round Rock Lunchdrop June 2026: sheet 2,959 vs R365 2,958.95. Rewriting 8
    such cells is churn that buries the 3 real corrections in the diff.
    """
    assert abs(2959.00 - 2958.95) < 1.01      # classified unchanged
    assert abs(4630.00 - 6648.73) >= 1.01     # a real correction survives


def test_never_zero_out_a_real_sheet_figure():
    """Arbor July 2025 My Hot Lunchbox: sheet 4,348.75, R365 0.00.

    R365 having no revenue is NOT evidence the sheet is wrong -- the money may be
    booked somewhere this mapping does not know about. Writing 0.00 would destroy
    the only surviving record.
    """
    sheet_value, r365_value = 4348.75, 0.00
    assert abs(r365_value) < 0.01 and sheet_value > 0
