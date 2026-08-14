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
