"""Runs the APP_MODE=aws code path without calling AWS.

botocore's Stubber checks every request against the real Bedrock / Textract API
models, so a malformed Converse or AnalyzeExpense request fails here instead of in production."""
import boto3
import pytest
from botocore.stub import Stubber

from app import db, extract, llm
from app.agents import graph as g
from app.config import reload_settings


@pytest.fixture
def aws_mode(monkeypatch):
    monkeypatch.setenv("APP_MODE", "aws")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    monkeypatch.setenv("BEDROCK_GUARDRAIL_ID", "gr-123")
    reload_settings()
    yield
    llm._client = None


def _converse_response(tool_name, payload, tin=900, tout=200):
    return {
        "output": {"message": {"role": "assistant", "content": [
            {"toolUse": {"toolUseId": "t1", "name": tool_name, "input": payload}}]}},
        "stopReason": "tool_use",
        "usage": {"inputTokens": tin, "outputTokens": tout, "totalTokens": tin + tout},
        "metrics": {"latencyMs": 850},
    }


def test_full_pipeline_in_aws_mode(aws_mode, make_pdf, monkeypatch):
    from eval.scenarios import SCENARIOS
    invoice = next(s for s in SCENARIOS if s["id"] == "S01_clean_acme")["invoice"]

    # --- Textract stub
    textract = boto3.client("textract", region_name="us-east-1")
    t_stub = Stubber(textract)
    t_stub.add_response("analyze_expense", {"ExpenseDocuments": [{
        "ExpenseIndex": 1,
        "SummaryFields": [{"Type": {"Text": "INVOICE_RECEIPT_ID"}, "ValueDetection": {"Text": "INV-1001"}}],
        "LineItemGroups": [],
        "Blocks": [{"BlockType": "PAGE", "Id": "p1"}, {"BlockType": "LINE", "Id": "l1", "Text": "INVOICE"}],
    }]})
    t_stub.activate()
    monkeypatch.setattr(boto3, "client", lambda svc, **kw: textract if svc == "textract" else _orig(svc, **kw))

    # --- Bedrock stub
    bedrock = _orig("bedrock-runtime", region_name="us-east-1")
    b_stub = Stubber(bedrock)
    b_stub.add_response("converse", _converse_response("record_invoice", invoice))
    b_stub.add_response("converse", _converse_response(
        "record_risk", {"risk_score": 4, "summary": "All checks passed.", "recommended_action": "approve"}, 400, 60))
    b_stub.activate()
    llm._client = bedrock

    inv_id = db.create_invoice("a.pdf")
    out = g.process_invoice(inv_id, str(make_pdf("S01_clean_acme")), "a.pdf")

    assert out["status"] == "auto_approved"
    calls = db.get_invoice(inv_id)["llm_calls"]
    assert [c["agent"] for c in calls] == ["extractor", "risk_analyst"]
    assert calls[0]["input_tokens"] == 900 and calls[0]["cost_usd"] > 0
    b_stub.assert_no_pending_responses()
    t_stub.assert_no_pending_responses()


def test_bedrock_throttling_is_retried(aws_mode, monkeypatch):
    bedrock = _orig("bedrock-runtime", region_name="us-east-1")
    stub = Stubber(bedrock)
    stub.add_client_error("converse", service_error_code="ThrottlingException", http_status_code=429)
    stub.add_response("converse", _converse_response(
        "record_risk", {"risk_score": 10, "summary": "ok", "recommended_action": "approve"}))
    stub.activate()
    llm._client = bedrock
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)

    out = llm.call_structured(agent="risk_analyst", system="s", user="u", tool_name="record_risk",
                              tool_description="d", json_schema={"type": "object"}, invoice_id=None,
                              mock_fn=lambda u: {})
    assert out["risk_score"] == 10


_orig = boto3.client
