"""Validation Agent: deterministic, tool-using checks against ERP master data.

Implements the classic accounts-payable THREE-WAY MATCH
(Invoice <-> Purchase Order <-> Goods Receipt) plus arithmetic, duplicate,
vendor-status and bank-detail fraud checks. Kept deterministic on purpose:
financial controls must be explainable and repeatable, so the LLM never decides these.
"""
from __future__ import annotations

from app import db
from app.config import settings
from app.guardrails import detect_prompt_injection
from app.schemas import Issue, Severity

AGENT = "validator"

# Issues that end in automatic rejection (no human needed)
REJECT_CODES = {"DUPLICATE_INVOICE", "VENDOR_BLOCKED"}


def _issue(code: str, severity: Severity, message: str) -> dict:
    return Issue(code=code, severity=severity, message=message, agent=AGENT).model_dump(mode="json")


def validate(inv: dict, invoice_id: int | None = None, doc_text: str = "") -> list[dict]:
    issues: list[dict] = []
    tol_total = settings.total_tolerance

    # 1. Arithmetic -------------------------------------------------------------
    for li in inv["line_items"]:
        expected = round(li["quantity"] * li["unit_price"], 2)
        if abs(expected - li["amount"]) > tol_total:
            issues.append(_issue("LINE_AMOUNT_MISMATCH", Severity.warning,
                                 f"{li['sku']}: qty x price = {expected:.2f} but line shows {li['amount']:.2f}"))
    lines_sum = round(sum(li["amount"] for li in inv["line_items"]), 2)
    if abs(lines_sum - inv["subtotal"]) > tol_total:
        issues.append(_issue("SUBTOTAL_MISMATCH", Severity.warning,
                             f"Line items sum to {lines_sum:.2f} but subtotal is {inv['subtotal']:.2f}"))
    expected_total = round(inv["subtotal"] + (inv.get("tax") or 0), 2)
    if abs(expected_total - inv["total"]) > tol_total:
        issues.append(_issue("TOTAL_MISMATCH", Severity.critical,
                             f"Subtotal + tax = {expected_total:.2f} but invoice total is {inv['total']:.2f}"))

    # 2. Vendor master -----------------------------------------------------------
    vendor = db.find_vendor(inv["vendor_name"])
    if not vendor:
        issues.append(_issue("VENDOR_NOT_FOUND", Severity.critical,
                             f"Vendor '{inv['vendor_name']}' is not in the vendor master"))
    else:
        if vendor["status"] == "blocked":
            issues.append(_issue("VENDOR_BLOCKED", Severity.critical, f"Vendor '{vendor['name']}' is blocked for payment"))
        if inv.get("bank_account") and vendor["bank_account"] and inv["bank_account"] != vendor["bank_account"]:
            issues.append(_issue("BANK_ACCOUNT_MISMATCH", Severity.critical,
                                 "Bank account on invoice differs from vendor master (possible payment fraud)"))
        if inv.get("vendor_tax_id") and vendor["tax_id"] and inv["vendor_tax_id"] != vendor["tax_id"]:
            issues.append(_issue("TAX_ID_MISMATCH", Severity.warning, "Tax ID differs from vendor master"))

    # 3. Duplicate detection -----------------------------------------------------
    dups = db.find_duplicates(inv["invoice_number"], inv["vendor_name"], invoice_id)
    if dups:
        issues.append(_issue("DUPLICATE_INVOICE", Severity.critical,
                             f"Invoice {inv['invoice_number']} from this vendor already exists (id {dups[0]['id']}, {dups[0]['status']})"))

    # 4. Three-way match: PO + goods receipt --------------------------------------
    po_number = inv.get("po_number")
    if not po_number:
        issues.append(_issue("PO_MISSING", Severity.critical, "Invoice does not reference a purchase order"))
    else:
        po = db.find_po(po_number)
        if not po:
            issues.append(_issue("PO_NOT_FOUND", Severity.critical, f"Purchase order {po_number} not found"))
        else:
            if vendor is None or po["vendor_id"] != vendor["id"]:
                issues.append(_issue("PO_VENDOR_MISMATCH", Severity.critical,
                                     f"PO {po_number} belongs to a different vendor"))
            if po["status"] != "open":
                issues.append(_issue("PO_CLOSED", Severity.warning, f"PO {po_number} is {po['status']}"))
            if po["currency"] != inv["currency"]:
                issues.append(_issue("CURRENCY_MISMATCH", Severity.warning,
                                     f"Invoice in {inv['currency']}, PO in {po['currency']}"))
            for li in inv["line_items"]:
                pol = po["lines"].get(li["sku"])
                if not pol:
                    issues.append(_issue("SKU_NOT_ON_PO", Severity.warning, f"{li['sku']} is not on PO {po_number}"))
                    continue
                diff_pct = (li["unit_price"] - pol["unit_price"]) / pol["unit_price"] * 100 if pol["unit_price"] else 0
                if diff_pct > settings.price_tolerance_pct:
                    issues.append(_issue("PRICE_MISMATCH", Severity.warning,
                                         f"{li['sku']}: invoiced {li['unit_price']:.2f} vs PO {pol['unit_price']:.2f} (+{diff_pct:.1f}%)"))
                if li["quantity"] > pol["qty_ordered"]:
                    issues.append(_issue("QTY_EXCEEDS_PO", Severity.warning,
                                         f"{li['sku']}: invoiced {li['quantity']:g} > ordered {pol['qty_ordered']:g}"))
                received = po["receipts"].get(li["sku"], 0)
                if li["quantity"] > received:
                    issues.append(_issue("QTY_EXCEEDS_RECEIPT", Severity.warning,
                                         f"{li['sku']}: invoiced {li['quantity']:g} > received {received:g}"))

    # 5. Business policy ---------------------------------------------------------
    if inv["total"] > settings.auto_approve_limit:
        issues.append(_issue("HIGH_VALUE", Severity.warning,
                             f"Total {inv['total']:,.2f} exceeds auto-approval limit {settings.auto_approve_limit:,.0f}"))

    # 6. Content guardrail: instructions hidden in the document -------------------
    hits = detect_prompt_injection(doc_text + "\n" + (inv.get("notes") or ""))
    if hits:
        issues.append(_issue("PROMPT_INJECTION", Severity.critical,
                             f"Document contains instruction-like text aimed at the system: {hits}"))
    return issues
