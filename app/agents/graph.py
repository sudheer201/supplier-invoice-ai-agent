"""LangGraph multi-agent workflow.

    read_document -> extractor -> validator -> risk_analyst -> supervisor
                         |  ^                                     |
                         |  +-- self-correction retry             +--> finalize            (auto approve / reject)
                         |                                        +--> human_review ------> finalize
                         +--> (extraction failed) ----------------------> human_review

human_review uses LangGraph `interrupt()`. State is checkpointed to SQLite, so the
graph pauses (for hours or days) and resumes exactly where it stopped when a reviewer
approves or rejects via the API.
"""
from __future__ import annotations

import operator
import sqlite3
from pathlib import Path
from typing import Annotated, Any, Optional, TypedDict

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from pydantic import ValidationError

from app import db
from app.agents import mock_models, prompts
from app.agents.validator import REJECT_CODES, validate
from app.config import settings
from app.extract import read_document
from app.guardrails import GuardrailViolation, safe_invoice_view
from app.llm import call_structured
from app.observability import get_logger, log_event, timed, trace_id_var
from app.schemas import Invoice, RiskAssessment

log = get_logger("graph")


class InvoiceState(TypedDict, total=False):
    invoice_id: int
    file_path: str
    file_name: str
    doc_text: str
    doc_source: str
    extracted: Optional[dict]
    extraction_error: Optional[str]
    extract_attempts: int
    issues: Annotated[list[dict], operator.add]
    risk: Optional[dict]
    decision: str
    review: Optional[dict]
    status: str
    error: Optional[str]


# ----------------------------------------------------------------- nodes

def read_document_node(state: InvoiceState) -> dict:
    trace_id_var.set(f"inv-{state['invoice_id']}")
    try:
        doc = read_document(state["file_path"])
    except GuardrailViolation as g:
        db.audit(state["invoice_id"], "guardrail", "blocked", {"code": g.code, "message": g.message})
        return {"status": "error", "error": f"{g.code}: {g.message}"}
    except Exception as e:  # corrupt PDF, Textract failure, ...
        log.exception("read_document failed")
        return {"status": "error", "error": f"READ_FAILED: {e}"}
    db.audit(state["invoice_id"], "reader", "document_read", {"source": doc["source"], "pages": doc["pages"]})
    return {"doc_text": doc["text"], "doc_source": doc["source"], "extract_attempts": 0}


def extractor_agent(state: InvoiceState) -> dict:
    attempt = state.get("extract_attempts", 0) + 1
    user = f"<document>\n{state['doc_text']}\n</document>"
    if state.get("extraction_error"):
        user += (f"\n\nYour previous output failed schema validation:\n{state['extraction_error']}\n"
                 "Fix these problems and call the tool again.")
    try:
        raw = call_structured(
            agent="extractor", system=prompts.EXTRACTOR_SYSTEM, user=user,
            tool_name="record_invoice", tool_description=prompts.EXTRACTOR_TOOL_DESC,
            json_schema=prompts.INVOICE_JSON_SCHEMA, invoice_id=state["invoice_id"],
            mock_fn=lambda _u: mock_models.mock_extract(state["doc_text"]),
        )
        inv = Invoice.model_validate(raw).model_dump(mode="json")   # output guardrail
    except ValidationError as ve:
        err = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in ve.errors())
        log_event(log, "extraction_invalid", attempt=attempt, error=err)
        return {"extracted": None, "extraction_error": err, "extract_attempts": attempt}
    except Exception as e:
        log.exception("extractor call failed")
        return {"extracted": None, "extraction_error": f"LLM_ERROR: {e}", "extract_attempts": attempt}

    db.update_invoice(state["invoice_id"], invoice_number=inv["invoice_number"], vendor_name=inv["vendor_name"],
                      po_number=inv.get("po_number"), total=inv["total"], currency=inv["currency"],
                      extracted_json=inv)
    db.audit(state["invoice_id"], "extractor", "extracted", safe_invoice_view(inv))
    return {"extracted": inv, "extraction_error": None, "extract_attempts": attempt}


def route_after_extract(state: InvoiceState) -> str:
    if state.get("extracted"):
        return "validator"
    if state.get("extract_attempts", 0) <= settings.max_extract_retries and not str(
            state.get("extraction_error", "")).startswith("LLM_ERROR"):
        return "extractor"          # self-correction loop
    return "extraction_failed"


def extraction_failed_node(state: InvoiceState) -> dict:
    issue = {"code": "EXTRACTION_FAILED", "severity": "critical", "agent": "extractor",
             "message": f"Could not extract a valid invoice: {state.get('extraction_error')}"}
    db.audit(state["invoice_id"], "extractor", "failed", issue)
    return {"issues": [issue], "risk": {"risk_score": 50, "summary": "Manual data entry required.",
                                        "recommended_action": "review"}, "decision": "human_review"}


def validator_agent(state: InvoiceState) -> dict:
    with timed() as t:
        issues = validate(state["extracted"], state["invoice_id"], state.get("doc_text", ""))
    db.audit(state["invoice_id"], "validator", "validated",
             {"issue_codes": [i["code"] for i in issues], "latency_ms": t["ms"]})
    log_event(log, "validated", issues=[i["code"] for i in issues], latency_ms=t["ms"])
    return {"issues": issues}


def risk_analyst_agent(state: InvoiceState) -> dict:
    issues = state.get("issues", [])
    safe = safe_invoice_view(state["extracted"])
    user = f"<invoice>\n{safe}\n</invoice>\n<issues>\n{issues}\n</issues>"
    try:
        raw = call_structured(
            agent="risk_analyst", system=prompts.RISK_SYSTEM, user=user,
            tool_name="record_risk", tool_description=prompts.RISK_TOOL_DESC,
            json_schema=prompts.RISK_JSON_SCHEMA, invoice_id=state["invoice_id"],
            mock_fn=lambda _u: mock_models.mock_risk(issues), max_tokens=600,
        )
        risk = RiskAssessment.model_validate(raw).model_dump()
    except Exception as e:
        # Fail safe: unknown risk -> a human looks at it
        log.exception("risk agent failed")
        risk = {"risk_score": 50, "summary": f"Risk agent unavailable ({type(e).__name__}); manual review.",
                "recommended_action": "review"}
    db.update_invoice(state["invoice_id"], risk_score=risk["risk_score"], risk_json=risk)
    db.audit(state["invoice_id"], "risk_analyst", "assessed", risk)
    return {"risk": risk}


def supervisor(state: InvoiceState) -> dict:
    """Policy layer. Deterministic rules decide; the LLM's opinion can only escalate."""
    issues = state.get("issues", [])
    codes = {i["code"] for i in issues}
    risk = state.get("risk") or {}
    blocking = [i for i in issues if i["severity"] in ("warning", "critical")]
    new_issues: list[dict] = []

    if state.get("decision") == "human_review":     # came from extraction_failed
        decision = "human_review"
    elif codes & REJECT_CODES:
        decision = "rejected"
    elif blocking:
        decision = "human_review"
    elif risk.get("risk_score", 0) >= 70 or risk.get("recommended_action") in ("review", "reject"):
        decision = "human_review"                   # the LLM may escalate a "clean" invoice, never clear one
        new_issues = [{"code": "RISK_ESCALATION", "severity": "warning", "agent": "risk_analyst",
                       "message": f"Risk analyst escalated: {risk.get('summary')}"}]
    else:
        decision = "auto_approved"

    all_issues = issues + new_issues
    db.update_invoice(state["invoice_id"], issues_json=all_issues,
                      **({"status": "pending_review"} if decision == "human_review" else {}))
    db.audit(state["invoice_id"], "supervisor", "decided",
             {"decision": decision, "codes": sorted(i["code"] for i in all_issues)})
    return {"decision": decision, "issues": new_issues}


def route_after_supervisor(state: InvoiceState) -> str:
    return "human_review" if state["decision"] == "human_review" else "finalize"


def human_review(state: InvoiceState) -> dict:
    # Pauses the graph. Nothing with side effects may run before interrupt(),
    # because LangGraph re-runs this node from the top when resumed.
    answer = interrupt({
        "invoice_id": state["invoice_id"],
        "issues": state.get("issues", []),
        "risk": state.get("risk"),
        "question": "Approve or reject this invoice?",
    })
    action = (answer or {}).get("action")
    if action not in ("approve", "reject"):
        raise ValueError("review action must be 'approve' or 'reject'")
    return {"review": answer, "decision": "approved" if action == "approve" else "rejected"}


def finalize(state: InvoiceState) -> dict:
    inv_id = state["invoice_id"]
    if state.get("status") == "error":
        db.update_invoice(inv_id, status="error", issues_json=[{"code": "READ_FAILED", "severity": "critical",
                                                                 "agent": "reader", "message": state.get("error")}])
        db.audit(inv_id, "system", "error", state.get("error"))
        return {"status": "error"}

    decision = state["decision"]
    review = state.get("review") or {}
    fields: dict[str, Any] = {"status": decision}
    if review:
        fields.update(reviewer=review.get("reviewer"), review_comment=review.get("comment"))
        db.audit(inv_id, f"human:{review.get('reviewer')}", f"review_{review.get('action')}", review.get("comment"))
    db.update_invoice(inv_id, **fields)
    if decision in ("auto_approved", "approved"):
        # In production: post to SAP / Oracle Fusion / NetSuite AP via API here.
        db.audit(inv_id, "erp_connector", "posted_to_erp", {"invoice_number": state["extracted"]["invoice_number"]})
    log_event(log, "finalized", decision=decision)
    return {"status": decision}


def route_after_read(state: InvoiceState) -> str:
    return "finalize" if state.get("status") == "error" else "extractor"


# ----------------------------------------------------------------- build

def build_graph(checkpointer=None):
    g = StateGraph(InvoiceState)
    g.add_node("read_document", read_document_node)
    g.add_node("extractor", extractor_agent)
    g.add_node("extraction_failed", extraction_failed_node)
    g.add_node("validator", validator_agent)
    g.add_node("risk_analyst", risk_analyst_agent)
    g.add_node("supervisor", supervisor)
    g.add_node("human_review", human_review)
    g.add_node("finalize", finalize)

    g.add_edge(START, "read_document")
    g.add_conditional_edges("read_document", route_after_read, ["extractor", "finalize"])
    g.add_conditional_edges("extractor", route_after_extract, ["validator", "extractor", "extraction_failed"])
    g.add_edge("extraction_failed", "supervisor")
    g.add_edge("validator", "risk_analyst")
    g.add_edge("risk_analyst", "supervisor")
    g.add_conditional_edges("supervisor", route_after_supervisor, ["human_review", "finalize"])
    g.add_edge("human_review", "finalize")
    g.add_edge("finalize", END)
    return g.compile(checkpointer=checkpointer)


_graph = None
_graph_ckpt_path = None


def get_graph():
    """Singleton graph with a durable SQLite checkpointer."""
    global _graph, _graph_ckpt_path
    if _graph is None or _graph_ckpt_path != settings.checkpoint_path:
        Path(settings.checkpoint_path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(settings.checkpoint_path, check_same_thread=False)
        _graph = build_graph(SqliteSaver(conn))
        _graph_ckpt_path = settings.checkpoint_path
    return _graph


def _summarise(invoice_id: int) -> dict:
    inv = db.get_invoice(invoice_id)
    return {
        "invoice_id": invoice_id,
        "status": inv["status"],
        "invoice_number": inv["invoice_number"],
        "vendor_name": inv["vendor_name"],
        "total": inv["total"],
        "risk_score": inv["risk_score"],
        "issues": inv["issues_json"] or [],
        "risk": inv["risk_json"],
    }


def process_invoice(invoice_id: int, file_path: str, file_name: str) -> dict:
    graph = get_graph()
    cfg = {"configurable": {"thread_id": f"inv-{invoice_id}"}}
    with timed() as t:
        graph.invoke({"invoice_id": invoice_id, "file_path": file_path, "file_name": file_name, "issues": []}, cfg)
    out = _summarise(invoice_id)
    out["pipeline_latency_ms"] = t["ms"]
    log_event(log, "pipeline_done", invoice_id=invoice_id, status=out["status"], latency_ms=t["ms"])
    return out


def resume_review(invoice_id: int, action: str, reviewer: str, comment: str = "") -> dict:
    graph = get_graph()
    cfg = {"configurable": {"thread_id": f"inv-{invoice_id}"}}
    snapshot = graph.get_state(cfg)
    if "human_review" not in (snapshot.next or ()):
        raise LookupError("Invoice is not waiting for review")
    graph.invoke(Command(resume={"action": action, "reviewer": reviewer, "comment": comment}), cfg)
    return _summarise(invoice_id)
