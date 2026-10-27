"""Guardrails, applied in layers:

1. Input   - file type / size / page checks before anything is processed.
2. Content - prompt-injection detection on document text (invoices are untrusted input).
3. Output  - LLM output must parse into the Pydantic schema, else retry, else fail safe.
4. Policy  - the LLM can only *advise*. Approve/reject is decided by deterministic rules,
             and any critical issue forces human review or rejection.
5. Privacy - bank accounts / tax ids are masked before logs or prompts leave the service.
(+ optional Amazon Bedrock Guardrails on every model call, see app/llm.py)
"""
from __future__ import annotations

import re

from app.config import settings


class GuardrailViolation(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


ALLOWED_EXT = {".pdf"}

_INJECTION_PATTERNS = [
    r"ignore (all |any )?(previous|prior|above) (instructions|rules)",
    r"disregard (the |all )?(previous|prior|above)",
    r"you are now",
    r"system prompt",
    r"(auto[- ]?)?approve (this|the) invoice",
    r"mark (this|it) as (approved|paid)",
    r"skip (the )?(validation|checks|review)",
    r"do not flag",
]
_INJECTION_RE = re.compile("|".join(_INJECTION_PATTERNS), re.IGNORECASE)


def check_upload(file_name: str, data: bytes) -> None:
    ext = "." + file_name.rsplit(".", 1)[-1].lower() if "." in file_name else ""
    if ext not in ALLOWED_EXT:
        raise GuardrailViolation("UNSUPPORTED_FILE", f"Only PDF invoices are accepted (got '{ext or 'none'}').")
    if len(data) == 0:
        raise GuardrailViolation("EMPTY_FILE", "Uploaded file is empty.")
    if len(data) > settings.max_file_mb * 1024 * 1024:
        raise GuardrailViolation("FILE_TOO_LARGE", f"File exceeds {settings.max_file_mb} MB limit.")
    if not data.startswith(b"%PDF"):
        raise GuardrailViolation("NOT_A_PDF", "File content is not a valid PDF.")


def detect_prompt_injection(text: str) -> list[str]:
    return sorted({m.group(0) for m in _INJECTION_RE.finditer(text or "")})


def mask(value: str | None, keep: int = 4) -> str | None:
    if not value:
        return value
    v = str(value)
    return ("*" * max(0, len(v) - keep)) + v[-keep:]


_IBAN_RE = re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{10,30}\b")


def redact_text(text: str) -> str:
    """Mask IBAN-like account numbers inside free text before logging."""
    return _IBAN_RE.sub(lambda m: mask(m.group(0)), text or "")


def safe_invoice_view(inv: dict) -> dict:
    """Copy of an extracted invoice that is safe to log or show in prompts."""
    out = dict(inv)
    out["bank_account"] = mask(out.get("bank_account"))
    out["vendor_tax_id"] = mask(out.get("vendor_tax_id"))
    return out
