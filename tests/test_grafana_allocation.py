"""Tests for the P&L-level / Grafana-ratio allocation.

The P&L is authoritative for revenue LEVEL but combines carryout+delivery into
one line. Grafana can attribute per order but its level runs 3-5% high. So the
level comes from the P&L and the split from Grafana -- and the two written cells
must always sum back to the P&L figure exactly.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

import grafana_web_sales as gws


REV = {"carryout": 26356.0, "delivery": 45223.0}   # Cherrywood June, ex-tax
PL_TOTAL = 67650.0                                  # P&L "Total Grafana Sales"


def test_allocation_sums_to_pl_total_exactly():
    car, del_ = gws.allocate_pl_total(REV, PL_TOTAL)
    assert round(car + del_, 2) == PL_TOTAL


def test_allocation_preserves_grafana_ratio():
    car, del_ = gws.allocate_pl_total(REV, PL_TOTAL)
    src_share = REV["carryout"] / (REV["carryout"] + REV["delivery"])
    # Cent rounding perturbs the ratio very slightly; anything beyond a few
    # parts per million would mean the split itself is wrong.
    assert abs(car / (car + del_) - src_share) < 1e-6


def test_allocation_scales_down_from_db_level():
    # Grafana runs high, so both allocated cells sit below the raw DB figures.
    car, del_ = gws.allocate_pl_total(REV, PL_TOTAL)
    assert car < REV["carryout"] and del_ < REV["delivery"]


def test_allocation_handles_penny_rounding():
    # Deliberately awkward total: the remainder must land in delivery, not vanish.
    car, del_ = gws.allocate_pl_total({"carryout": 1.0, "delivery": 2.0}, 100.01)
    assert round(car + del_, 2) == 100.01


def test_allocation_none_without_pl_total():
    assert gws.allocate_pl_total(REV, None) is None


def test_allocation_none_when_a_channel_is_missing():
    assert gws.allocate_pl_total({"carryout": 100.0, "delivery": None}, 500.0) is None


def test_allocation_none_on_zero_db_total():
    # No orders means no ratio; refuse rather than divide by zero.
    assert gws.allocate_pl_total({"carryout": 0.0, "delivery": 0.0}, 500.0) is None


def _sheet_row(carryout="26,278", delivery="44,671"):
    row = [""] * 60
    row[0] = "6.2026"
    row[gws.COL_CARRYOUT] = carryout
    row[gws.COL_DELIVERY] = delivery
    return row


def test_compare_with_pl_total_targets_allocated_values():
    f = gws.compare(REV, _sheet_row(), allow_overwrite=True, pl_total=PL_TOTAL)
    assert round(sum(x["pl"] for x in f), 2) == PL_TOTAL


def test_compare_with_pl_total_records_basis_and_raw_db():
    f = gws.compare(REV, _sheet_row(), allow_overwrite=True, pl_total=PL_TOTAL)
    car = [x for x in f if x["name"] == "Carryout"][0]
    assert "P&L total split" in car["basis"]
    assert car["raw_db"] == REV["carryout"]   # audit trail for the unexplained gap


def test_compare_without_pl_total_uses_raw_db():
    f = gws.compare(REV, _sheet_row(), allow_overwrite=True)
    car = [x for x in f if x["name"] == "Carryout"][0]
    assert car["pl"] == REV["carryout"]
    assert "ordering DB" in car["basis"]
