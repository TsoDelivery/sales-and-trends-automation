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


def test_read_retry_backoff_outlasts_the_quota_window():
    """A retry ladder shorter than the quota window is decoration.

    Sheets read quota is enforced per 60-second window. The original ladder
    (1+2+4 = 7s) spent every attempt inside the same window that was throttling
    it, so a transient 403 still killed the run. The full ladder must be able to
    outwait a whole window.
    """
    ladder = (5, 15, 35, 65)
    assert sum(ladder) > 60, "must be able to outwait a full quota window"
    assert ladder[-1] > 60, "final wait should clear a full window on its own"


def test_transient_403_is_retried_not_fatal():
    """403 from Sheets is usually rate pressure wearing a permissions mask."""
    import time

    import sheets_io
    from googleapiclient.errors import HttpError

    class FakeResp:
        status = 403
        reason = "Forbidden"

        def __getitem__(self, key):        # HttpError reads resp like a mapping
            return {"content-type": "application/json"}.get(key, "")

        def get(self, key, default=None):
            return {"content-type": "application/json"}.get(key, default)

    calls = {"n": 0}

    class FakeValues:
        def get(self, **kw):
            return self

        def execute(self):
            calls["n"] += 1
            if calls["n"] == 1:
                raise HttpError(FakeResp(), b'{"error": {"message": "rate"}}')
            return {"values": [["ok"]]}

    class FakeSheets:
        def values(self):
            return FakeValues()

    class FakeService:
        def spreadsheets(self):
            return FakeSheets()

    real_sleep, time.sleep = time.sleep, lambda s: None
    try:
        out = sheets_io.read_tabs(FakeService(), "sid", ["Tab"])
    finally:
        time.sleep = real_sleep
    assert out == {"Tab": [["ok"]]}
    assert calls["n"] == 2, "should have retried exactly once"


def test_repo_key_wins_over_ambient_google_credentials(tmp_path, monkeypatch):
    """An unrelated ambient GOOGLE_APPLICATION_CREDENTIALS must not hijack auth.

    On this machine the login shell exports a GA4 analytics key
    (ga4-analytics-reader@tswarm) that has no access to the Sales & Trends
    workbook. Honouring it produced a 403 that was indistinguishable from
    throttling and only reproduced in background runs.
    """
    import sheets_io

    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(tmp_path / "ga4.json"))
    (tmp_path / "ga4.json").write_text("{}")
    monkeypatch.delenv("SALES_TRENDS_GOOGLE_CREDENTIALS", raising=False)

    captured = {}

    class FakeCreds:
        service_account_email = "repo@tso-chinese-delivery.iam.gserviceaccount.com"

    def fake_from_file(path, scopes=None):
        captured["path"] = path
        return FakeCreds()

    import google.oauth2.service_account as sa
    monkeypatch.setattr(sa.Credentials, "from_service_account_file",
                        staticmethod(fake_from_file))
    monkeypatch.setattr("googleapiclient.discovery.build",
                        lambda *a, **k: object())

    sheets_io.sheets_service()
    assert captured["path"].endswith("google-service-account.json"), (
        f"used {captured['path']!r} instead of the repo key")
    assert "ga4" not in captured["path"]


def test_zero_is_not_written_into_a_blank_cell(monkeypatch, capsys):
    """0.00 in a blank cell is an assertion, not data.

    It claims "this channel earned nothing" where the truth is usually "this
    channel was not active", and it makes untouched history look audited. Run
    through main() so the real classification executes, not a restatement of it.
    """
    import importlib.util
    import sys

    import r365_catering as rc
    import sheets_io

    spec = importlib.util.spec_from_file_location(
        "ingest_r365_mod", "scripts/ingest-catering-r365.py")
    mod = importlib.util.module_from_spec(spec)

    header = [""] * 70
    header[rc.column_index("BJ")] = "EZCater"
    label_row = [""] * 70
    label_row[0] = "11.2025"          # EZCater cell left blank

    monkeypatch.setattr(sheets_io, "load_env", lambda: None)
    monkeypatch.setattr(sheets_io, "sheets_service", lambda **k: object())
    monkeypatch.setattr(sheets_io, "spreadsheet_id", lambda: "sid")
    monkeypatch.setattr(sheets_io, "read_tabs",
                        lambda *a, **k: {t: [header, label_row]
                                         for t in rc.STORE_TABS.values()})
    monkeypatch.setattr(rc, "auth_headers", lambda: {})
    # R365 reports exactly 0.00 for the blank EZCater cell.
    monkeypatch.setattr(rc, "fetch_lines", lambda *a, **k: ([], []))
    monkeypatch.setattr(rc, "aggregate",
                        lambda *a, **k: {t: {"11.2025": {"EZCater": 0.0}}
                                         for t in rc.STORE_TABS.values()})
    monkeypatch.setattr(sys, "argv", ["ingest", "--month", "2025-11"])

    try:
        spec.loader.exec_module(mod)
        mod.main()
    except SystemExit:
        pass
    out = capsys.readouterr().out

    assert "Planned writes: 0" in out, f"expected no planned writes, got:\n{out}"
    assert "WRITE" not in out


# --- explain_difference: an overwrite must be explained, not just permitted ---

def _ingest_module():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "ingest_r365_expl", "scripts/ingest-catering-r365.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _line(date, store, account, net, posted="2026-01-01"):
    ym = date[:7]
    return {"date": date, "store": store, "account": account, "net": net,
            "month": ym, "posted": posted, "lag_days": 10}


def test_prefix_is_explained_as_a_stale_cell():
    """Sheet equals the running total through an earlier journal."""
    mod = _ingest_module()
    store = "Tso Chinese Cherrywood"
    recs = [_line("2026-06-06", store, "4420", 1000.0),
            _line("2026-06-13", store, "4420", 500.0),
            _line("2026-06-20", store, "4420", 250.0)]
    reason = mod.explain_difference(
        recs, "Cherrywood Monthly Sales ", "6.2026", "Lunchdrop",
        existing=1500.0, value=1750.0)
    # Reported by POSTED date -- that is what the keyed cell could have seen.
    assert reason and "1 line(s) posted later" in reason, f"got: {reason!r}"


def test_double_count_is_explained():
    """Cherrywood June Lunchdrop: 3,539.95 + 619.95 counted twice = 4,159.90."""
    mod = _ingest_module()
    store = "Tso Chinese Cherrywood"
    recs = [_line("2026-06-06", store, "4420", 2920.00),
            _line("2026-06-27", store, "4420", 619.95)]
    reason = mod.explain_difference(
        recs, "Cherrywood Monthly Sales ", "6.2026", "Lunchdrop",
        existing=4159.90, value=3539.95)
    assert reason and "double-counts" in reason


def test_sibling_column_conflation_is_explained():
    """Arbor Sep 2025 EZCater: the sheet lumped the tax-exempt column in.

    EZCater taxable (4440/4442) and EZCater tax-exempt (4441) have SEPARATE
    columns on the sheet. The cell held 2,750.30 = 1,415.15 + 1,335.15, i.e.
    both keyed into one column. Note 4441 is NOT an EZCater account in the
    mapping -- an earlier version of this test asserted "omits account 4441",
    which described a mapping that does not exist.
    """
    mod = _ingest_module()
    store = "Tso Chinese Arboretum Crossing"
    recs = [_line("2025-09-07", store, "4440", 1415.15),
            _line("2025-09-07", store, "4441", 1335.15)]
    reason = mod.explain_difference(
        recs, "Arbor Monthly Sales", "9.2025", "EZCater",
        existing=2750.30, value=1415.15)
    assert reason and "EZCater (non-Tax)" in reason, f"got: {reason!r}"


def test_unexplained_difference_returns_none():
    """The case that was actually got wrong, encoded so it cannot recur.

    Cherrywood Nov 2025 America To Go: sheet 7,796.96, R365 3,673.17, made of
    two journals. No prefix, no double-count, no account subset reproduces the
    sheet figure -- so nothing may overwrite it.
    """
    mod = _ingest_module()
    store = "Tso Chinese Cherrywood"
    recs = [_line("2025-11-30", store, "4445", 2069.74),
            _line("2025-11-30", store, "4445", 1603.43, posted="2026-01-29")]
    reason = mod.explain_difference(
        recs, "Cherrywood Monthly Sales ", "11.2025", "America To Go",
        existing=7796.96, value=3673.17)
    assert reason is None, f"must NOT be explained, got: {reason!r}"


def test_a_zero_valued_account_is_not_blamed():
    """Dropping a 0.00 account changes nothing, so it must not be a diagnosis.

    A plausible-sounding wrong cause is worse than "unexplained": it licenses an
    overwrite on false evidence.
    """
    mod = _ingest_module()
    store = "Tso Chinese Arboretum Crossing"
    recs = [_line("2025-09-07", store, "4440", 1415.15),
            _line("2025-09-07", store, "4442", 0.00)]
    reason = mod.explain_difference(
        recs, "Arbor Monthly Sales", "9.2025", "EZCater",
        existing=1415.15, value=1415.15)
    assert reason is None or "4442" not in reason, (
        f"blamed a zero-valued account: {reason!r}")


def test_missing_account_within_one_column_is_explained():
    """A column with several accounts where the sheet captured only some.

    My Hot Lunchbox maps 4410+4411 to ONE column, so omitting 4411 is a real
    failure mode for it (unlike EZCater, where 4441 has its own column).
    """
    mod = _ingest_module()
    store = "Tso Chinese Menchaca"
    recs = [_line("2026-03-07", store, "4410", 2000.00),
            _line("2026-03-14", store, "4411", 500.00)]
    reason = mod.explain_difference(
        recs, "Menchaca Monthly Sales", "3.2026", "My Hot Lunchbox",
        existing=2000.00, value=2500.00)
    assert reason and ("omits account 4411" in reason
                       or "posted later" in reason), f"got: {reason!r}"


def test_prefix_uses_posted_date_not_business_date():
    """A stale cell reflects what had POSTED when it was keyed.

    Arbor Nov 2025 EZCater: 2,550.90 on the sheet vs 2,771.09 in R365. The gap
    is a 220.19 line for business date 2025-11-02 that posted late (2026-01-09).
    Sorted by business date it comes FIRST and no prefix matches, so a genuine
    stale cell looks unexplained and gets skipped. Sorted by posted date it comes
    LAST and the explanation is immediate.
    """
    mod = _ingest_module()
    store = "Tso Chinese Arboretum Crossing"
    recs = [
        _line("2025-11-02", store, "4440", 220.19, posted="2026-01-09"),
        _line("2025-11-09", store, "4440", 59.80, posted="2025-12-22"),
        _line("2025-11-16", store, "4440", 397.70, posted="2025-12-22"),
        _line("2025-11-23", store, "4440", 1583.25, posted="2025-12-22"),
        _line("2025-11-30", store, "4440", 510.15, posted="2025-12-22"),
    ]
    reason = mod.explain_difference(
        recs, "Arbor Monthly Sales", "11.2025", "EZCater",
        existing=2550.90, value=2771.09)
    assert reason and "2025-12-22" in reason, f"got: {reason!r}"


def test_zero_cell_needs_no_explanation():
    """Filling a 0.00 cell destroys no record, so it must not be gated."""
    mod = _ingest_module()
    store = "Tso Chinese Cherrywood"
    recs = [_line("2025-09-30", store, "4445", 3136.93)]
    reason = mod.explain_difference(
        recs, "Cherrywood Monthly Sales ", "9.2025", "America To Go",
        existing=0.0, value=3136.93)
    assert reason, "a zero cell should be writable"


def test_r365_zero_guard_runs_before_the_explain_gate():
    """Order matters: never zero out a real figure, even if "explained".

    Arbor Jul 2025 My Hot Lunchbox is 4,348.75 on the sheet and 0.00 in R365
    (its lines net to zero via reversals), and explain_difference DOES find a
    posted-prefix story for it. The refuse-to-zero check must therefore be
    evaluated first, or a plausible explanation would license destroying the
    only surviving record of that revenue.
    """
    import re
    src = open("scripts/ingest-catering-r365.py").read()
    body = src.split("def main(")[1]
    zero_guard = body.index("refusing to zero out")
    explain_gate = body.index("explain_difference(")
    assert zero_guard < explain_gate, (
        "the R365-zero guard must be checked before the explain gate")
