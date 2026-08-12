import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
MODULE_PATH = ROOT / "scripts" / "r365-sales-trends.py"
spec = importlib.util.spec_from_file_location("r365_sales_trends", MODULE_PATH)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_uber_eats_expected_promo_gap_passes():
    status, note = module.channel_status("3P:Uber Eats", 57983.75, 46903.56)
    assert status == "PASS"
    assert "expected promo" in note


def test_doordash_expected_promo_gap_passes():
    status, note = module.channel_status("3P:DoorDash", 35950.21, 31425.26)
    assert status == "PASS"
    assert "expected promo" in note


def test_promo_adjusted_channel_still_flags_extreme_gap():
    status, note = module.channel_status("3P:Uber Eats", 1000.0, 600.0)
    assert status == "FLAG"
    assert "outside expected promo" in note


def test_non_promo_channel_keeps_five_percent_threshold():
    status, note = module.channel_status("1P:Kiosk", 1000.0, 1060.0)
    assert status == "FLAG"
    assert note == "outside 5%"


def test_zero_sheet_value_flags_nonzero_r365():
    status, note = module.channel_status("3P:Favor", 1000.0, 0.0)
    assert status == "FLAG"
    assert "outside 5%" in note


def test_zero_both_passes():
    status, note = module.channel_status("3P:Favor", 0.0, 0.0)
    assert status == "PASS"
    assert note == ""


def test_old_july_doordash_menchaca_value_passes_expected_band():
    status, note = module.channel_status("3P:DoorDash", 22857.82, 22940.12)
    assert status == "PASS"
    assert "expected promo" in note


def test_uber_ratio_below_expected_band_flags():
    status, note = module.channel_status("3P:Uber Eats", 1000.0, 740.0)
    assert status == "FLAG"
    assert "outside expected promo" in note


def test_expected_band_constants_are_documented():
    assert module.PROMO_NET_RATIO_BANDS["3P:Uber Eats"] == (0.75, 0.90)
    assert module.PROMO_NET_RATIO_BANDS["3P:DoorDash"] == (0.85, 1.05)


def test_column_letter_supports_extended_sheet_columns():
    assert module.column_letter(77) == "BZ"
    assert module.column_letter(78) == "CA"


def test_catering_placeholder_uses_prior_same_year_rows_only():
    assert module.catering_formula("BZ", 9, "2026") == (
        '=IFERROR(AVERAGEIF($A$2:$A$8,"*.2026",BZ$2:BZ$8),"")'
    )


def test_catering_placeholder_is_blank_for_first_month_without_prior_ytd_data():
    assert module.catering_formula("BY", 15, "2026") == (
        '=IFERROR(AVERAGEIF($A$2:$A$14,"*.2026",BY$2:BY$14),"")'
    )


def test_total_catering_header_is_found_case_insensitively():
    assert module.find_header_index(["Total Sales", "TOTAL CATERING", "C"], "total catering") == 1
    assert module.find_header_index(["Total Sales"], "total catering") is None


def test_catering_placeholder_is_named_and_not_a_revenue_amount():
    assert "average" in module.CATERING_FORMULA_PLACEHOLDER.lower()
    assert "pending" in module.CATERING_FORMULA_PLACEHOLDER.lower()
