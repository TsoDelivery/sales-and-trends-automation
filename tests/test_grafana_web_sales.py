"""Tests for Grafana carryout/delivery extraction.

The most important cases here guard the SILENT-EMPTY failure mode: a wrong
transaction type/status returns zero rows with no error, which would otherwise
be written into the sheet as legitimate zero revenue.
"""

import datetime as dt
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

import grafana_web_sales as gws


# ------------------------------------------------------------------ month bounds

def test_month_bounds_mid_year():
    assert gws.month_bounds(2026, 7) == (dt.date(2026, 7, 1), dt.date(2026, 8, 1))


def test_month_bounds_december_rolls_year():
    assert gws.month_bounds(2026, 12) == (dt.date(2026, 12, 1), dt.date(2027, 1, 1))


def test_month_bounds_february_leap():
    start, end = gws.month_bounds(2028, 2)
    assert (end - start).days == 29


# -------------------------------------------------------------------- sql shape

def test_sql_uses_capture_and_uppercase_succeeded():
    # 'capture'/'SUCCEEDED' are enum values. A typo errors loudly, but a valid
    # wrong value (type='refund') returns plausible-looking rows totalling ~$1k
    # instead of ~$325k, with no error at all.
    sql = gws.build_sql(2026, 7)
    assert "t.type = 'capture'" in sql
    assert "t.status = 'SUCCEEDED'" in sql


def test_sql_subtracts_sales_tax():
    # Sheet columns are net of tax; gross runs ~8-10% high.
    assert "t.amount - coalesce(t.sales_tax, 0)" in gws.build_sql(2026, 7)


def test_sql_uses_half_open_date_range():
    sql = gws.build_sql(2026, 7)
    assert ">= '2026-07-01'" in sql and "<  '2026-08-01'" in sql


def test_sql_excludes_test_and_deleted_orders():
    sql = gws.build_sql(2026, 7)
    assert "coalesce(c.test_, false) = false" in sql
    assert "c.deleted_at is null" in sql


def test_sql_scopes_to_known_locations():
    sql = gws.build_sql(2026, 7)
    assert "c.location_id in (1,2,5,6,20)" in sql


# ------------------------------------------------------------- frame parsing

def _payload(rows):
    return {"results": {"A": {"frames": [{
        "schema": {"fields": [{"name": "location_id"}, {"name": "carryout"},
                              {"name": "tickets"}, {"name": "net_ex_tax"}]},
        "data": {"values": [[r[0] for r in rows], [r[1] for r in rows],
                            [r[2] for r in rows], [r[3] for r in rows]]},
    }]}}}


def test_parse_frames_reads_rows():
    got = gws.parse_frames(_payload([(1, True, 900, 29910.0), (1, False, 1100, 41728.0)]))
    assert got[(1, True)]["net"] == 29910.0
    assert got[(1, False)]["tickets"] == 1100


def test_parse_frames_raises_on_grafana_error():
    with pytest.raises(RuntimeError, match="Grafana query error"):
        gws.parse_frames({"results": {"A": {"error": "relation does not exist"}}})


def test_parse_frames_empty_is_empty_dict_not_crash():
    assert gws.parse_frames({"results": {"A": {"frames": []}}}) == {}


# -------------------------------------------------------- silent-empty guarding

def _full_month():
    rows = {}
    for loc in gws.LOCATIONS:
        rows[(loc, True)] = {"tickets": 900, "net": 30000.0}
        rows[(loc, False)] = {"tickets": 1100, "net": 40000.0}
    return gws.to_store_revenue(rows)


def test_sanity_passes_on_a_full_month():
    assert gws.check_sanity(_full_month()) == []


def test_sanity_flags_completely_empty_result():
    problems = gws.check_sanity({})
    assert problems and "no rows at all" in problems[0]


def test_sanity_flags_missing_store():
    rev = _full_month()
    del rev["Menchaca Monthly Sales"]
    assert any("Menchaca" in p for p in gws.check_sanity(rev))


def test_sanity_flags_implausibly_low_value():
    # A broken query can return a tiny non-zero figure; that is not real revenue.
    rev = _full_month()
    rev["Cherrywood Monthly Sales "]["carryout"] = 12.0
    assert any("below the" in p for p in gws.check_sanity(rev))


def test_sanity_flags_missing_channel():
    rev = _full_month()
    rev["Arbor Monthly Sales"]["delivery"] = None
    assert any("delivery missing" in p for p in gws.check_sanity(rev))


# ------------------------------------------------------------------ attribution

def test_to_store_revenue_splits_carryout_from_delivery():
    rows = {(1, True): {"tickets": 900, "net": 29910.0},
            (1, False): {"tickets": 1100, "net": 41728.0}}
    got = gws.to_store_revenue(rows)["Cherrywood Monthly Sales "]
    assert got["carryout"] == 29910.0
    assert got["delivery"] == 41728.0


def test_to_store_revenue_ignores_unknown_location():
    # A new location must not silently land in another store's row.
    assert gws.to_store_revenue({(999, True): {"tickets": 5, "net": 100.0}}) == {}


# -------------------------------------------------------------------- comparing

def _sheet_row(carryout="29,797", delivery="41,339"):
    row = [""] * 60
    row[0] = "7.2026"
    row[gws.COL_CARRYOUT] = carryout
    row[gws.COL_DELIVERY] = delivery
    return row


def test_agreement_within_tolerance():
    rev = {"carryout": 29797.50, "delivery": 41339.0}
    actions = {f["name"]: f["action"] for f in gws.compare(rev, _sheet_row())}
    assert actions == {"Carryout": "agree", "Delivery": "agree"}


def test_blank_cell_is_filled():
    rev = {"carryout": 29910.0, "delivery": 41728.0}
    f = gws.compare(rev, _sheet_row(carryout=""))
    assert [x for x in f if x["name"] == "Carryout"][0]["action"] == "fill"


def test_difference_is_reported_by_default():
    rev = {"carryout": 31000.0, "delivery": 41339.0}
    f = gws.compare(rev, _sheet_row())
    assert [x for x in f if x["name"] == "Carryout"][0]["action"] == "report"


def test_difference_is_updated_when_authorized():
    rev = {"carryout": 31000.0, "delivery": 41339.0}
    f = gws.compare(rev, _sheet_row(), allow_overwrite=True)
    car = [x for x in f if x["name"] == "Carryout"][0]
    assert car["action"] == "update" and car["pl"] == 31000.0


def test_compare_targets_the_right_columns():
    rev = {"carryout": 1.0, "delivery": 2.0}
    cols = {f["name"]: f["col"] for f in gws.compare(rev, _sheet_row())}
    assert cols == {"Carryout": 12, "Delivery": 17}   # M and R


def test_missing_source_channel_is_skipped():
    assert gws.compare({"carryout": None, "delivery": None}, _sheet_row()) == []
