from app import db
from app.agents import graph as g
from app.agents import mock_models


def run(path):
    inv_id = db.create_invoice(path.name)
    return g.process_invoice(inv_id, str(path), path.name)


def test_clean_invoice_auto_approved_and_posted(make_pdf):
    out = run(make_pdf("S01_clean_acme"))
    assert out["status"] == "auto_approved"
    actions = [a["action"] for a in db.get_invoice(out["invoice_id"])["audit"]]
    assert actions[-1] == "posted_to_erp"


def test_human_review_pause_and_approve(make_pdf):
    out = run(make_pdf("S02_price_mismatch"))
    assert out["status"] == "pending_review"
    done = g.resume_review(out["invoice_id"], "approve", "priya", "agreed new price")
    assert done["status"] == "approved"
    inv = db.get_invoice(out["invoice_id"])
    assert inv["reviewer"] == "priya"
    assert "review_approve" in [a["action"] for a in inv["audit"]]


def test_human_review_reject(make_pdf):
    out = run(make_pdf("S08_bank_account_changed"))
    done = g.resume_review(out["invoice_id"], "reject", "ravi", "bank change not verified")
    assert done["status"] == "rejected"


def test_resume_survives_new_graph_instance(make_pdf):
    """Simulates an API restart between upload and review (durable checkpoint)."""
    out = run(make_pdf("S03_qty_exceeds_receipt"))
    g._graph = None                                    # drop in-memory graph
    assert g.resume_review(out["invoice_id"], "approve", "anu")["status"] == "approved"


def test_duplicate_rejected_without_human(make_pdf):
    out = run(make_pdf("S05_duplicate"))
    assert out["status"] == "rejected"
    assert {i["code"] for i in out["issues"]} == {"DUPLICATE_INVOICE"}


def test_same_invoice_twice_is_duplicate(make_pdf):
    p = make_pdf("S11_clean_bright")
    assert run(p)["status"] == "auto_approved"
    assert run(p)["status"] == "rejected"


def test_extractor_self_corrects_after_bad_output(make_pdf, monkeypatch):
    calls = {"n": 0}
    real = mock_models.mock_extract

    def flaky(text):
        calls["n"] += 1
        out = real(text)
        if calls["n"] == 1:
            out["line_items"] = []                     # violates schema (min 1 line)
        return out

    monkeypatch.setattr(mock_models, "mock_extract", flaky)
    out = run(make_pdf("S01_clean_acme"))
    assert calls["n"] == 2 and out["status"] == "auto_approved"


def test_extraction_failure_goes_to_human(make_pdf, monkeypatch):
    monkeypatch.setattr(mock_models, "mock_extract", lambda t: {"invoice_number": ""})
    out = run(make_pdf("S01_clean_acme"))
    assert out["status"] == "pending_review"
    assert out["issues"][0]["code"] == "EXTRACTION_FAILED"


def test_llm_risk_can_escalate_but_not_clear(make_pdf, monkeypatch):
    monkeypatch.setattr(mock_models, "mock_risk", lambda issues: {
        "risk_score": 85, "summary": "Unusual pattern", "recommended_action": "review"})
    out = run(make_pdf("S01_clean_acme"))
    assert out["status"] == "pending_review"
    assert "RISK_ESCALATION" in {i["code"] for i in out["issues"]}

    monkeypatch.setattr(mock_models, "mock_risk", lambda issues: {
        "risk_score": 0, "summary": "Looks fine", "recommended_action": "approve"})
    out = run(make_pdf("S07_blocked_vendor"))
    assert out["status"] == "rejected"                 # LLM cannot override policy


def test_llm_cost_and_latency_recorded(make_pdf):
    out = run(make_pdf("S01_clean_acme"))
    calls = db.get_invoice(out["invoice_id"])["llm_calls"]
    assert {c["agent"] for c in calls} == {"extractor", "risk_analyst"}
    assert all(c["cost_usd"] > 0 and c["input_tokens"] > 0 for c in calls)
