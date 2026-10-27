import copy

from app.agents.validator import validate
from eval.scenarios import SCENARIOS

BY_ID = {s["id"]: s for s in SCENARIOS}


def codes(inv, **kw):
    return {i["code"] for i in validate(inv, **kw)}


def test_clean_invoice_has_no_issues():
    assert codes(BY_ID["S01_clean_acme"]["invoice"]) == set()


def test_each_scenario_raises_expected_codes():
    for s in SCENARIOS:
        assert codes(s["invoice"]) == set(s["expected"]["issues"]), s["id"]


def test_price_within_tolerance_is_ok():
    inv = copy.deepcopy(BY_ID["S01_clean_acme"]["invoice"])
    inv["line_items"][0]["unit_price"] = 4.55          # +1.1% < 2% tolerance
    inv["line_items"][0]["amount"] = 455.0
    inv["subtotal"], inv["tax"], inv["total"] = 605.0, 60.5, 665.5
    assert "PRICE_MISMATCH" not in codes(inv)


def test_line_arithmetic_error_detected():
    inv = copy.deepcopy(BY_ID["S01_clean_acme"]["invoice"])
    inv["line_items"][0]["amount"] = 999.0
    assert {"LINE_AMOUNT_MISMATCH", "SUBTOTAL_MISMATCH"} <= codes(inv)


def test_missing_po():
    inv = copy.deepcopy(BY_ID["S01_clean_acme"]["invoice"])
    inv["po_number"] = None
    assert "PO_MISSING" in codes(inv)
