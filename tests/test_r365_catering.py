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
    assert agg[tab]["3.2026"]["Lunchdrop"] == 150.25
    assert agg[tab]["4.2026"]["Lunchdrop"] == 7.00


def test_aggregate_keeps_stores_separate():
    records = [
        record("2026-03-02", "Tso Chinese Cherrywood", "4420", 100.00),
        record("2026-03-02", "Tso Chinese Menchaca", "4420", 200.00),
    ]
    agg = rc.aggregate(records)
    assert agg[rc.STORE_TABS["Tso Chinese Cherrywood"]]["3.2026"]["Lunchdrop"] == 100.00
    assert agg[rc.STORE_TABS["Tso Chinese Menchaca"]]["3.2026"]["Lunchdrop"] == 200.00


def test_ezcater_tax_exempt_goes_to_its_own_column():
    # 4440 taxable + 4442 discounts feed "EZCater"; 4441 tax-exempt has its OWN
    # column, "EZCater (non-Tax)". Folding 4441 into EZCater inflates it -- an
    # earlier version did exactly that.
    records = [
        record("2026-03-02", "Tso Chinese Cherrywood", "4440", 500.00),
        record("2026-03-03", "Tso Chinese Cherrywood", "4441", 120.00),
        record("2026-03-04", "Tso Chinese Cherrywood", "4442", -20.00),
    ]
    agg = rc.aggregate(records)
    cells = agg[rc.STORE_TABS["Tso Chinese Cherrywood"]]["3.2026"]
    assert cells["EZCater"] == 480.00
    assert cells["EZCater (non-Tax)"] == 120.00


def test_column_letters_are_resolved_per_tab_not_hardcoded():
    # THE BUG: BM is "America To Go" on Cherrywood but "Try Hungry" on Round
    # Rock and "Event" on TsoCo. Hardcoding one tab's layout compared Round
    # Rock's Try Hungry cells against America To Go revenue.
    base = [""] * rc.column_index("BE")
    cherrywood = base + ["", "In-house Catering (Square, FlexCater)",
                         "In-house Catering (Square, Spoonfed)(Non-Taxable)",
                         "Lunchdrop", "Sharebite", "EZCater", "EZCater (non-Tax)",
                         "My Hot Lunchbox", "America To Go", "Event"]
    round_rock = base + ["", "In-house Catering (Square, FlexCater)",
                         "In-house Catering (Square, Spoonfed)(Non-Taxable)",
                         "Lunchdrop", "Sharebite", "EZCater", "EZCater (non-Tax)",
                         "My Hot Lunchbox", "Try Hungry", "Event"]

    cw_writable, _, _ = rc.resolve_columns(cherrywood)
    rr_writable, _, _ = rc.resolve_columns(round_rock)

    assert cw_writable["America To Go"][0] == "BM"
    assert "America To Go" not in rr_writable      # Round Rock has no ATG column
    assert rr_writable["Try Hungry"][0] == "BM"    # same letter, different meaning
    # Lunchdrop happens to be BH on both, which is why it validated cleanly.
    assert cw_writable["Lunchdrop"][0] == rr_writable["Lunchdrop"][0] == "BH"


def test_unknown_headers_are_reported_never_guessed():
    row = [""] * rc.column_index("BE") + ["", "Lunchdrop", "Some New Vendor"]
    writable, _skipped, unknown = rc.resolve_columns(row)
    assert "Lunchdrop" in writable
    assert unknown == {"Some New Vendor": "BG"}


def test_column_letter_index_roundtrip():
    for letters in ("A", "Z", "AA", "BE", "BF", "BM", "BR"):
        assert rc.column_letter(rc.column_index(letters)) == letters


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
    assert agg[rc.STORE_TABS["Tso Chinese Cherrywood"]]["3.2026"]["Lunchdrop"] == 70.00


def test_aggregate_does_not_write_bf():
    # BF must stay out of the mapping until its account set is confirmed.
    assert "BF" not in rc.HEADER_ACCOUNTS
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
    assert agg[rc.STORE_TABS["Tso Chinese Cherrywood"]]["3.2026"]["Lunchdrop"] == 105.00


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
    mapped = {n for nums in rc.HEADER_ACCOUNTS.values() for n in nums}
    assert mapped <= set(rc.AUDIT_ACCOUNTS)


def test_post_lag_pad_is_generous_enough_to_catch_late_journals():
    # Journals post after the sale; too small a pad silently truncates months.
    assert rc.POST_LAG_PAD >= 7


def test_bulk_import_batches_do_not_trigger_the_lag_alarm():
    """One-off history loads must not make the pad alarm fire on every run.

    Tso's backfill posted 163 old lines on 2025-08-25 with lags to 230 days. An
    alarm that always fires is an alarm nobody reads.
    """
    bulk = [dict(record("2025-01-07", "Tso Chinese Cherrywood", "4440", 10.0),
                 posted="2025-08-25", lag_days=230) for _ in range(40)]
    routine = [dict(record("2026-06-27", "Tso Chinese Cherrywood", "4420", 10.0),
                    posted="2026-07-09", lag_days=12)]
    warnings, notes = rc.verify_completeness(bulk + routine, ["2026-06"])
    assert not any("Raise POST_LAG_PAD" in w for w in warnings)
    assert any("bulk-posted on 2025-08-25" in n for n in notes)
    # The import must be reported as a NOTE, never as a blocking warning.
    assert not any("bulk-posted" in w for w in warnings), (
        "a one-off import must not block a write")


def test_a_genuinely_long_routine_lag_still_warns():
    """The alarm must stay capable of firing, or it is decoration.

    Expressed relative to POST_LAG_PAD so raising the pad cannot silently
    neuter this test -- which is exactly what happened when the pad went to 240.
    """
    over = rc.POST_LAG_PAD + 20
    routine = [dict(record("2026-01-05", "Tso Chinese Cherrywood", "4420", 10.0),
                    posted="2026-07-01", lag_days=over)]
    warnings, notes = rc.verify_completeness(routine, ["2026-01"])
    assert any("Raise POST_LAG_PAD" in w for w in warnings)


def test_pad_covers_the_worst_observed_correction_batch():
    """A 5-line correction for bd 2025-07-31 was posted 2026-01-21: 174 days.

    The pad must cover real observed behaviour, not a hopeful guess.
    """
    assert rc.POST_LAG_PAD >= 174



# ---- credential sourcing ---------------------------------------------------
# Credentials must come from 1Password on every run. The old design cached them
# in /tmp, which does not survive a reboot, so the first scheduled run after a
# restart failed and needed a human to re-stage by hand.

def test_auth_reads_from_1password_when_no_cache(tmp_path, monkeypatch, capsys):
    import r365_catering as rc
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        label = [c for c in cmd if c.startswith("label=")][0].split("=", 1)[1]
        val = {"username": "svc_user", "NewPassword": "pw"}[label]
        return type("R", (), {"returncode": 0, "stdout": val + "\n", "stderr": ""})()

    tok = tmp_path / "tok"
    tok.write_text("ops_fake_token")
    monkeypatch.setattr(rc, "OP_TOKEN_FILE", str(tok))
    monkeypatch.setattr(rc.subprocess, "run", fake_run)

    h = rc.auth_headers(user_path=str(tmp_path / "nope_u"),
                        pass_path=str(tmp_path / "nope_p"))
    assert h["Authorization"].startswith("Basic ")
    assert len(calls) == 2, "should read both fields from op"
    assert "from 1Password" in capsys.readouterr().err


def test_auth_never_requires_tmp_files(tmp_path, monkeypatch):
    """A reboot wipes /tmp; that must not be able to break a scheduled run."""
    import r365_catering as rc
    monkeypatch.setattr(rc, "OP_TOKEN_FILE", str(tmp_path / "tok"))
    (tmp_path / "tok").write_text("t")
    monkeypatch.setattr(rc.subprocess, "run", lambda cmd, **kw: type(
        "R", (), {"returncode": 0, "stdout": "v\n", "stderr": ""})())
    # No /tmp files exist at these paths -- must still succeed.
    rc.auth_headers(user_path=str(tmp_path / "u"), pass_path=str(tmp_path / "p"))


def test_op_timeout_reports_the_documented_fix(tmp_path, monkeypatch):
    """An `op` hang is a known failure mode; say so, don't call it 'auth failed'."""
    import r365_catering as rc
    monkeypatch.setattr(rc, "OP_TOKEN_FILE", str(tmp_path / "tok"))
    (tmp_path / "tok").write_text("t")

    def boom(cmd, **kw):
        raise rc.subprocess.TimeoutExpired(cmd, 60)
    monkeypatch.setattr(rc.subprocess, "run", boom)

    with pytest.raises(SystemExit) as e:
        rc.auth_headers(user_path=str(tmp_path / "u"), pass_path=str(tmp_path / "p"))
    assert "daemon" in str(e.value)


def test_auth_error_never_leaks_a_secret(tmp_path, monkeypatch):
    import r365_catering as rc
    monkeypatch.setattr(rc, "OP_TOKEN_FILE", str(tmp_path / "tok"))
    (tmp_path / "tok").write_text("ops_super_secret_token_value")
    monkeypatch.setattr(rc.subprocess, "run", lambda cmd, **kw: type(
        "R", (), {"returncode": 1, "stdout": "",
                  "stderr": "isn't an item in the vault"})())
    with pytest.raises(SystemExit) as e:
        rc.auth_headers(user_path=str(tmp_path / "u"), pass_path=str(tmp_path / "p"))
    assert "ops_super_secret_token_value" not in str(e.value)
