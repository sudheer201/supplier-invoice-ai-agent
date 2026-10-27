"""Deterministic stand-ins for the LLM, used when APP_MODE=mock.
They let the full graph, tests and eval run offline. In aws mode Bedrock replaces them."""
from __future__ import annotations

import re

_FIELD_PATTERNS = {
    "invoice_number": r"^Invoice No:\s*(.+)$",
    "invoice_date": r"^Invoice Date:\s*(.+)$",
    "vendor_name": r"^Vendor:\s*(.+)$",
    "vendor_tax_id": r"^Vendor Tax ID:\s*(.+)$",
    "po_number": r"^PO Number:\s*(.+)$",
    "bank_account": r"^Bank Account:\s*(.+)$",
    "currency": r"^Currency:\s*(.+)$",
    "subtotal": r"^Subtotal:\s*([\d,]+\.?\d*)$",
    "tax": r"^Tax:\s*([\d,]+\.?\d*)$",
    "total": r"^Total:\s*([\d,]+\.?\d*)$",
    "notes": r"^Notes:\s*(.+)$",
}
_LINE_RE = re.compile(r"^(\S+)\s+(.+?)\s+([\d.]+)\s+([\d,]+\.\d{2})\s+([\d,]+\.\d{2})$")
_NUMERIC = {"subtotal", "tax", "total"}


def _num(s: str) -> float:
    return float(s.replace(",", ""))


def mock_extract(doc_text: str) -> dict:
    out: dict = {}
    for key, pat in _FIELD_PATTERNS.items():
        m = re.search(pat, doc_text, re.MULTILINE)
        if m:
            out[key] = _num(m.group(1)) if key in _NUMERIC else m.group(1).strip()
        else:
            out[key] = None
    items = []
    in_table = False
    for line in doc_text.splitlines():
        line = line.strip()
        if line.startswith("SKU") and "Amount" in line:
            in_table = True
            continue
        if in_table:
            if line.startswith("Subtotal"):
                break
            m = _LINE_RE.match(line)
            if m:
                items.append({"sku": m.group(1), "description": m.group(2), "quantity": _num(m.group(3)),
                              "unit_price": _num(m.group(4)), "amount": _num(m.group(5))})
    out["line_items"] = items
    if out.get("tax") is None:
        out["tax"] = 0.0
    return out


def mock_risk(issues: list[dict]) -> dict:
    weights = {"critical": 45, "warning": 20, "info": 5}
    score = min(100, sum(weights.get(i["severity"], 0) for i in issues))
    codes = [i["code"] for i in issues]
    if not issues:
        return {"risk_score": 3, "summary": "All checks passed: vendor active, PO and goods receipt match, totals reconcile.",
                "recommended_action": "approve"}
    if any(c in codes for c in ("DUPLICATE_INVOICE", "VENDOR_BLOCKED")):
        action = "reject"
    else:
        action = "review"
    return {"risk_score": score,
            "summary": f"Flagged {len(issues)} issue(s): {', '.join(codes)}. "
                       f"Reviewer should verify these against the PO and vendor master before payment.",
            "recommended_action": action}
