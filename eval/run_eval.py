"""Evaluation harness - run it after every prompt or model change.

    python -m eval.run_eval                 # mock mode
    APP_MODE=aws python -m eval.run_eval    # real Bedrock + Textract

Measures
- extraction accuracy  : per-field exact match vs labels (numbers within 0.01)
- decision accuracy    : auto_approved / human_review / rejected vs expected
- issue precision/recall on the issue codes the validator raises
- latency and LLM cost per invoice
Writes eval/results/report.json + report.md. Exits 1 if below thresholds (use in CI).
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TMP = Path(tempfile.mkdtemp(prefix="inv_eval_"))
os.environ["DB_PATH"] = str(TMP / "eval.db")
os.environ["CHECKPOINT_PATH"] = str(TMP / "ck.db")
os.environ.setdefault("LOG_LEVEL", "WARNING")

from app import db  # noqa: E402
from app.agents.graph import process_invoice  # noqa: E402
from app.config import reload_settings, settings  # noqa: E402
from eval.scenarios import SCENARIOS  # noqa: E402
from scripts.generate_invoices import draw_invoice  # noqa: E402

THRESHOLDS = {"extraction_field_accuracy": 0.95, "decision_accuracy": 0.90, "issue_recall": 0.90}
FIELDS = ["invoice_number", "vendor_name", "po_number", "bank_account", "currency", "subtotal", "tax", "total"]
STATUS_TO_DECISION = {"pending_review": "human_review"}


def _eq(a, b) -> bool:
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(a - b) < 0.011
    return (str(a).strip() if a is not None else None) == (str(b).strip() if b is not None else None)


def field_scores(pred: dict | None, gold: dict) -> tuple[int, int, list[str]]:
    if not pred:
        n = len(FIELDS) + len(gold["line_items"]) * 4
        return 0, n, ["<no extraction>"]
    ok, n, wrong = 0, 0, []
    for f in FIELDS:
        n += 1
        if _eq(pred.get(f), gold.get(f)):
            ok += 1
        else:
            wrong.append(f)
    pl = {li["sku"]: li for li in pred.get("line_items", [])}
    for gl in gold["line_items"]:
        for k in ("sku", "quantity", "unit_price", "amount"):
            n += 1
            p = pl.get(gl["sku"], {})
            if _eq(p.get(k), gl[k]):
                ok += 1
            else:
                wrong.append(f"{gl['sku']}.{k}")
    return ok, n, wrong


def main() -> int:
    reload_settings()
    db.init_db()
    pdf_dir = TMP / "pdfs"
    pdf_dir.mkdir()
    rows, f_ok, f_n, d_ok, tp, fp, fn = [], 0, 0, 0, 0, 0, 0

    for s in SCENARIOS:
        pdf = pdf_dir / f"{s['id']}.pdf"
        draw_invoice(s["invoice"], pdf)
        inv_id = db.create_invoice(pdf.name)
        out = process_invoice(inv_id, str(pdf), pdf.name)
        detail = db.get_invoice(inv_id)

        ok, n, wrong = field_scores(detail["extracted_json"], s["invoice"])
        f_ok, f_n = f_ok + ok, f_n + n

        decision = STATUS_TO_DECISION.get(out["status"], out["status"])
        dec_ok = decision == s["expected"]["decision"]
        d_ok += dec_ok

        got = {i["code"] for i in out["issues"]}
        exp = set(s["expected"]["issues"])
        tp, fp, fn = tp + len(got & exp), fp + len(got - exp), fn + len(exp - got)

        cost = sum(c["cost_usd"] for c in detail["llm_calls"])
        rows.append({
            "scenario": s["id"], "expected": s["expected"]["decision"], "got": decision, "decision_ok": dec_ok,
            "issues_expected": sorted(exp), "issues_got": sorted(got),
            "field_accuracy": round(ok / n, 3), "wrong_fields": wrong,
            "latency_ms": out["pipeline_latency_ms"], "llm_cost_usd": round(cost, 6),
            "pass": dec_ok and got == exp and ok == n,
        })

    summary = {
        "mode": settings.mode,
        "model": settings.bedrock_model_id if settings.mode == "aws" else "mock",
        "scenarios": len(rows),
        "passed": sum(r["pass"] for r in rows),
        "extraction_field_accuracy": round(f_ok / f_n, 4),
        "decision_accuracy": round(d_ok / len(rows), 4),
        "issue_precision": round(tp / (tp + fp), 4) if tp + fp else 1.0,
        "issue_recall": round(tp / (tp + fn), 4) if tp + fn else 1.0,
        "avg_latency_ms": round(sum(r["latency_ms"] for r in rows) / len(rows), 1),
        "max_latency_ms": max(r["latency_ms"] for r in rows),
        "avg_cost_per_invoice_usd": round(sum(r["llm_cost_usd"] for r in rows) / len(rows), 6),
        "thresholds": THRESHOLDS,
    }

    out_dir = ROOT / "eval" / "results"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "report.json").write_text(json.dumps({"summary": summary, "rows": rows}, indent=2))
    md = [f"# Evaluation report ({summary['mode']}, {summary['model']})", "",
          f"**{summary['passed']}/{summary['scenarios']} scenarios passed**", "",
          "| Metric | Value |", "|---|---|"]
    for k in ("extraction_field_accuracy", "decision_accuracy", "issue_precision", "issue_recall",
              "avg_latency_ms", "max_latency_ms", "avg_cost_per_invoice_usd"):
        md.append(f"| {k} | {summary[k]} |")
    md += ["", "| Scenario | Expected | Got | Issues raised | Fields | Pass |", "|---|---|---|---|---|---|"]
    for r in rows:
        md.append(f"| {r['scenario']} | {r['expected']} | {r['got']} | {', '.join(r['issues_got']) or '-'} "
                  f"| {r['field_accuracy']:.0%} | {'✅' if r['pass'] else '❌'} |")
    (out_dir / "report.md").write_text("\n".join(md) + "\n")

    print("\n".join(md))
    shutil.rmtree(TMP, ignore_errors=True)
    failed = [k for k, v in THRESHOLDS.items() if summary[k] < v]
    if failed:
        print(f"\nFAILED thresholds: {failed}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
