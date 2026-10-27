"""Document reading layer.

aws mode  -> Amazon Textract AnalyzeExpense (purpose-built invoice/receipt OCR).
             Its summary fields + line items + raw lines are turned into a compact
             text view that the extractor agent (LLM) normalises into our schema.
mock mode -> pdfplumber text extraction for digital PDFs.

Note: synchronous AnalyzeExpense accepts single-page documents as bytes. For
multi-page or scanned batches use StartExpenseAnalysis with an S3 object.
"""
from __future__ import annotations

from pathlib import Path

from app.config import settings
from app.guardrails import GuardrailViolation
from app.observability import get_logger, log_event, timed

log = get_logger("extract")
MAX_PAGES = 5


def read_document(path: str) -> dict:
    with timed() as t:
        if settings.mode == "aws":
            out = _textract(path)
        else:
            out = _pdfplumber(path)
    if not out["text"].strip():
        raise GuardrailViolation("NO_TEXT", "No readable text found in document (scanned image? use Textract mode).")
    log_event(log, "document_read", source=out["source"], pages=out["pages"], chars=len(out["text"]), latency_ms=t["ms"])
    out["latency_ms"] = t["ms"]
    return out


def _pdfplumber(path: str) -> dict:
    import pdfplumber

    with pdfplumber.open(path) as pdf:
        if len(pdf.pages) > MAX_PAGES:
            raise GuardrailViolation("TOO_MANY_PAGES", f"Invoice has more than {MAX_PAGES} pages.")
        text = "\n".join((p.extract_text() or "") for p in pdf.pages)
        return {"text": text, "pages": len(pdf.pages), "source": "pdfplumber"}


def _textract(path: str) -> dict:
    import boto3

    client = boto3.client("textract", region_name=settings.aws_region)
    resp = client.analyze_expense(Document={"Bytes": Path(path).read_bytes()})
    parts: list[str] = []
    pages = 0
    for doc in resp.get("ExpenseDocuments", []):
        parts.append("## SUMMARY FIELDS")
        for f in doc.get("SummaryFields", []):
            label = (f.get("LabelDetection") or {}).get("Text") or f.get("Type", {}).get("Text", "")
            value = (f.get("ValueDetection") or {}).get("Text", "")
            ftype = f.get("Type", {}).get("Text", "")
            if value:
                parts.append(f"{ftype} | {label}: {value}")
        parts.append("## LINE ITEMS")
        for group in doc.get("LineItemGroups", []):
            for item in group.get("LineItems", []):
                cells = [f"{c.get('Type', {}).get('Text', '')}={(c.get('ValueDetection') or {}).get('Text', '')}"
                         for c in item.get("LineItemExpenseFields", [])]
                parts.append(" ; ".join(cells))
        parts.append("## RAW TEXT")
        for b in doc.get("Blocks", []):
            if b.get("BlockType") == "LINE":
                parts.append(b.get("Text", ""))
            if b.get("BlockType") == "PAGE":
                pages += 1
    return {"text": "\n".join(parts), "pages": max(pages, 1), "source": "textract"}
