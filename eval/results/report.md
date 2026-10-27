# Evaluation report (mock, mock)

**14/14 scenarios passed**

| Metric | Value |
|---|---|
| extraction_field_accuracy | 1.0 |
| decision_accuracy | 1.0 |
| issue_precision | 1.0 |
| issue_recall | 1.0 |
| avg_latency_ms | 36.5 |
| max_latency_ms | 63.93 |
| avg_cost_per_invoice_usd | 0.000338 |

| Scenario | Expected | Got | Issues raised | Fields | Pass |
|---|---|---|---|---|---|
| S01_clean_acme | auto_approved | auto_approved | - | 100% | ✅ |
| S02_price_mismatch | human_review | human_review | PRICE_MISMATCH | 100% | ✅ |
| S03_qty_exceeds_receipt | human_review | human_review | QTY_EXCEEDS_RECEIPT | 100% | ✅ |
| S04_total_mismatch | human_review | human_review | TOTAL_MISMATCH | 100% | ✅ |
| S05_duplicate | rejected | rejected | DUPLICATE_INVOICE | 100% | ✅ |
| S06_unknown_po | human_review | human_review | PO_NOT_FOUND | 100% | ✅ |
| S07_blocked_vendor | rejected | rejected | VENDOR_BLOCKED | 100% | ✅ |
| S08_bank_account_changed | human_review | human_review | BANK_ACCOUNT_MISMATCH | 100% | ✅ |
| S09_prompt_injection | human_review | human_review | PROMPT_INJECTION | 100% | ✅ |
| S10_high_value | human_review | human_review | HIGH_VALUE | 100% | ✅ |
| S11_clean_bright | auto_approved | auto_approved | - | 100% | ✅ |
| S12_po_vendor_mismatch | human_review | human_review | PO_VENDOR_MISMATCH | 100% | ✅ |
| S13_unknown_vendor | human_review | human_review | PO_VENDOR_MISMATCH, VENDOR_NOT_FOUND | 100% | ✅ |
| S14_sku_not_on_po | human_review | human_review | SKU_NOT_ON_PO | 100% | ✅ |
