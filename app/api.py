"""FastAPI service.

POST /invoices                 upload a PDF -> runs the multi-agent pipeline
GET  /invoices                 list (optional ?status=pending_review)
GET  /invoices/{id}            detail: extracted data, issues, risk, audit trail, LLM calls
GET  /reviews/pending          the human-review queue
POST /reviews/{id}             {"action": "approve"|"reject", "reviewer": "...", "comment": "..."}
GET  /metrics                  straight-through rate, cost, tokens, latency per agent
GET  /health
GET  /                         reviewer dashboard (static HTML)

Set API_KEY to require an `x-api-key` header on every /invoices, /reviews and /metrics call.
"""
from __future__ import annotations

import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal, Optional

from fastapi import Depends, FastAPI, File, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app import db
from app.agents.graph import process_invoice, resume_review
from app.config import settings
from app.guardrails import GuardrailViolation, check_upload
from app.observability import get_logger, log_event

log = get_logger("api")
STATIC = Path(__file__).parent / "static"

@asynccontextmanager
async def lifespan(_app: FastAPI):
    db.init_db()
    Path(settings.upload_dir).mkdir(parents=True, exist_ok=True)
    log_event(log, "startup", mode=settings.mode, model=settings.bedrock_model_id if settings.mode == "aws" else "mock")
    yield


app = FastAPI(title="Invoice Processing Agent", version="1.0.0", lifespan=lifespan,
              description="Multi-agent invoice processing on Amazon Bedrock with human-in-the-loop approval.")


def require_key(x_api_key: Optional[str] = Header(default=None)) -> None:
    expected = os.getenv("API_KEY")
    if expected and x_api_key != expected:
        raise HTTPException(status_code=401, detail="Invalid or missing x-api-key")


class ReviewIn(BaseModel):
    action: Literal["approve", "reject"]
    reviewer: str = Field(min_length=1, max_length=80)
    comment: str = Field(default="", max_length=1000)


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "mode": settings.mode}


@app.get("/")
def dashboard() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.post("/invoices", dependencies=[Depends(require_key)])
def upload_invoice(file: UploadFile = File(...)) -> dict:
    data = file.file.read()
    name = Path(file.filename or "upload.pdf").name
    try:
        check_upload(name, data)                     # input guardrail
    except GuardrailViolation as g:
        db.audit(None, "guardrail", "upload_rejected", {"file": name, "code": g.code})
        raise HTTPException(status_code=422, detail={"code": g.code, "message": g.message})
    path = Path(settings.upload_dir) / f"{uuid.uuid4().hex}_{name}"
    path.write_bytes(data)
    invoice_id = db.create_invoice(name)
    try:
        return process_invoice(invoice_id, str(path), name)
    except Exception as e:
        log.exception("pipeline failed")
        db.update_invoice(invoice_id, status="error")
        db.audit(invoice_id, "system", "pipeline_error", str(e))
        raise HTTPException(status_code=500, detail=f"Processing failed for invoice {invoice_id}")


@app.get("/invoices", dependencies=[Depends(require_key)])
def list_invoices(status: Optional[str] = None) -> list[dict]:
    return db.list_invoices(status)


@app.get("/invoices/{invoice_id}", dependencies=[Depends(require_key)])
def get_invoice(invoice_id: int) -> dict:
    inv = db.get_invoice(invoice_id)
    if not inv:
        raise HTTPException(404, "Invoice not found")
    return inv


@app.get("/reviews/pending", dependencies=[Depends(require_key)])
def pending_reviews() -> list[dict]:
    return [db.get_invoice(i["id"]) for i in db.list_invoices("pending_review")]


@app.post("/reviews/{invoice_id}", dependencies=[Depends(require_key)])
def submit_review(invoice_id: int, body: ReviewIn) -> dict:
    inv = db.get_invoice(invoice_id)
    if not inv:
        raise HTTPException(404, "Invoice not found")
    if inv["status"] != "pending_review":
        raise HTTPException(409, f"Invoice is '{inv['status']}', not pending review")
    try:
        return resume_review(invoice_id, body.action, body.reviewer, body.comment)
    except LookupError as e:
        raise HTTPException(409, str(e))


@app.get("/metrics", dependencies=[Depends(require_key)])
def metrics() -> dict:
    return db.metrics()
