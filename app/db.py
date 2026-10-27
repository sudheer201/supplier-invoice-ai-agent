"""SQLite data layer: ERP-style master data (vendors, POs, goods receipts),
invoices, an audit trail and an LLM call ledger for cost/latency tracking.
Swap the connection for PostgreSQL / Amazon RDS in production."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from app.config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS vendors (
    id INTEGER PRIMARY KEY,
    name TEXT UNIQUE NOT NULL,
    tax_id TEXT,
    bank_account TEXT,
    status TEXT NOT NULL DEFAULT 'active'          -- active | blocked
);
CREATE TABLE IF NOT EXISTS purchase_orders (
    po_number TEXT PRIMARY KEY,
    vendor_id INTEGER NOT NULL REFERENCES vendors(id),
    currency TEXT NOT NULL DEFAULT 'USD',
    status TEXT NOT NULL DEFAULT 'open'            -- open | closed
);
CREATE TABLE IF NOT EXISTS po_lines (
    po_number TEXT NOT NULL REFERENCES purchase_orders(po_number),
    sku TEXT NOT NULL,
    description TEXT,
    qty_ordered REAL NOT NULL,
    unit_price REAL NOT NULL,
    PRIMARY KEY (po_number, sku)
);
CREATE TABLE IF NOT EXISTS goods_receipts (
    po_number TEXT NOT NULL,
    sku TEXT NOT NULL,
    qty_received REAL NOT NULL,
    PRIMARY KEY (po_number, sku)
);
CREATE TABLE IF NOT EXISTS invoices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_name TEXT,
    invoice_number TEXT,
    vendor_name TEXT,
    po_number TEXT,
    total REAL,
    currency TEXT,
    status TEXT NOT NULL DEFAULT 'processing',     -- processing | pending_review | auto_approved | approved | rejected | error
    risk_score INTEGER,
    extracted_json TEXT,
    issues_json TEXT,
    risk_json TEXT,
    reviewer TEXT,
    review_comment TEXT,
    thread_id TEXT,
    created_at TEXT,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_id INTEGER,
    actor TEXT,
    action TEXT,
    detail TEXT,
    ts TEXT
);
CREATE TABLE IF NOT EXISTS llm_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_id INTEGER,
    agent TEXT,
    model TEXT,
    input_tokens INTEGER,
    output_tokens INTEGER,
    latency_ms REAL,
    cost_usd REAL,
    success INTEGER,
    ts TEXT
);
CREATE INDEX IF NOT EXISTS idx_inv_number ON invoices(invoice_number, vendor_name);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    Path(settings.db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db(seed: bool = True) -> None:
    with connect() as c:
        c.executescript(SCHEMA)
        if seed and c.execute("SELECT COUNT(*) FROM vendors").fetchone()[0] == 0:
            _seed(c)


def _seed(c: sqlite3.Connection) -> None:
    vendors = [
        (1, "Acme Industrial Supplies", "TAX-11-2233", "GB29NWBK60161331926819", "active"),
        (2, "Bright Office Solutions", "TAX-44-5566", "GB82WEST12345698765432", "active"),
        (3, "Nimbus Cloud Services", "TAX-77-8899", "GB33BUKB20201555555555", "active"),
        (4, "Shady Parts Ltd", "TAX-00-0000", "GB94BARC10201530093459", "blocked"),
    ]
    c.executemany("INSERT INTO vendors VALUES (?,?,?,?,?)", vendors)
    pos = [
        ("PO-5001", 1, "USD"), ("PO-5002", 2, "USD"), ("PO-5003", 3, "USD"),
        ("PO-5004", 4, "USD"), ("PO-5005", 1, "USD"),
    ]
    c.executemany("INSERT INTO purchase_orders (po_number, vendor_id, currency) VALUES (?,?,?)", pos)
    lines = [
        ("PO-5001", "BRG-6204", "Ball bearing 6204", 100, 4.50),
        ("PO-5001", "BLT-M8", "Hex bolt M8 x 40", 500, 0.30),
        ("PO-5002", "PPR-A4", "A4 paper ream", 50, 5.20),
        ("PO-5002", "TNR-85A", "Toner cartridge 85A", 10, 62.00),
        ("PO-5003", "VM-STD", "Standard VM - monthly", 20, 140.00),
        ("PO-5003", "SUP-PRM", "Premium support - monthly", 1, 900.00),
        ("PO-5004", "GSK-12", "Gasket 12mm", 200, 1.10),
        ("PO-5005", "MTR-3PH", "3-phase motor 5kW", 40, 410.00),
    ]
    c.executemany("INSERT INTO po_lines VALUES (?,?,?,?,?)", lines)
    receipts = [
        ("PO-5001", "BRG-6204", 100), ("PO-5001", "BLT-M8", 500),
        ("PO-5002", "PPR-A4", 50), ("PO-5002", "TNR-85A", 8),   # only 8 toners received
        ("PO-5003", "VM-STD", 20), ("PO-5003", "SUP-PRM", 1),
        ("PO-5004", "GSK-12", 200), ("PO-5005", "MTR-3PH", 40),
    ]
    c.executemany("INSERT INTO goods_receipts VALUES (?,?,?)", receipts)
    # An invoice that was already paid - used to test duplicate detection
    c.execute(
        "INSERT INTO invoices (file_name, invoice_number, vendor_name, po_number, total, currency, status, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        ("historic.pdf", "INV-0999", "Acme Industrial Supplies", "PO-5001", 600.0, "USD", "approved", now(), now()),
    )


# ---------- invoice + audit helpers ----------

def create_invoice(file_name: str) -> int:
    with connect() as c:
        cur = c.execute(
            "INSERT INTO invoices (file_name, status, created_at, updated_at) VALUES (?, 'processing', ?, ?)",
            (file_name, now(), now()),
        )
        inv_id = cur.lastrowid
        c.execute("UPDATE invoices SET thread_id=? WHERE id=?", (f"inv-{inv_id}", inv_id))
    audit(inv_id, "system", "received", {"file_name": file_name})
    return inv_id


def update_invoice(invoice_id: int, **fields: Any) -> None:
    if not fields:
        return
    for k in ("extracted_json", "issues_json", "risk_json"):
        if k in fields and not isinstance(fields[k], str):
            fields[k] = json.dumps(fields[k])
    fields["updated_at"] = now()
    cols = ", ".join(f"{k}=?" for k in fields)
    with connect() as c:
        c.execute(f"UPDATE invoices SET {cols} WHERE id=?", (*fields.values(), invoice_id))


def get_invoice(invoice_id: int) -> dict | None:
    with connect() as c:
        row = c.execute("SELECT * FROM invoices WHERE id=?", (invoice_id,)).fetchone()
        if not row:
            return None
        inv = dict(row)
        for k in ("extracted_json", "issues_json", "risk_json"):
            inv[k] = json.loads(inv[k]) if inv[k] else None
        inv["audit"] = [dict(r) for r in c.execute(
            "SELECT actor, action, detail, ts FROM audit_log WHERE invoice_id=? ORDER BY id", (invoice_id,))]
        inv["llm_calls"] = [dict(r) for r in c.execute(
            "SELECT agent, model, input_tokens, output_tokens, latency_ms, cost_usd, success FROM llm_calls WHERE invoice_id=? ORDER BY id",
            (invoice_id,))]
        return inv


def list_invoices(status: str | None = None) -> list[dict]:
    q = "SELECT id, file_name, invoice_number, vendor_name, po_number, total, currency, status, risk_score, created_at FROM invoices"
    args: tuple = ()
    if status:
        q += " WHERE status=?"
        args = (status,)
    with connect() as c:
        return [dict(r) for r in c.execute(q + " ORDER BY id DESC", args)]


def audit(invoice_id: int | None, actor: str, action: str, detail: Any = None) -> None:
    with connect() as c:
        c.execute(
            "INSERT INTO audit_log (invoice_id, actor, action, detail, ts) VALUES (?,?,?,?,?)",
            (invoice_id, actor, action, json.dumps(detail, default=str) if detail is not None else None, now()),
        )


def record_llm_call(invoice_id: int | None, agent: str, model: str, input_tokens: int,
                    output_tokens: int, latency_ms: float, cost_usd: float, success: bool) -> None:
    with connect() as c:
        c.execute(
            "INSERT INTO llm_calls (invoice_id, agent, model, input_tokens, output_tokens, latency_ms, cost_usd, success, ts)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (invoice_id, agent, model, input_tokens, output_tokens, latency_ms, cost_usd, int(success), now()),
        )


def metrics() -> dict:
    with connect() as c:
        status_counts = {r["status"]: r["n"] for r in c.execute(
            "SELECT status, COUNT(*) n FROM invoices WHERE thread_id IS NOT NULL GROUP BY status")}
        llm = c.execute(
            "SELECT COUNT(*) calls, COALESCE(SUM(input_tokens),0) tin, COALESCE(SUM(output_tokens),0) tout,"
            " COALESCE(SUM(cost_usd),0) cost, COALESCE(AVG(latency_ms),0) avg_lat,"
            " COALESCE(SUM(CASE WHEN success=0 THEN 1 ELSE 0 END),0) failures FROM llm_calls").fetchone()
        lat = sorted(r[0] for r in c.execute("SELECT latency_ms FROM llm_calls"))
        per_agent = [dict(r) for r in c.execute(
            "SELECT agent, COUNT(*) calls, ROUND(AVG(latency_ms),1) avg_latency_ms, ROUND(SUM(cost_usd),6) cost_usd"
            " FROM llm_calls GROUP BY agent")]
    processed = sum(v for k, v in status_counts.items() if k != "processing")
    p95 = lat[min(len(lat) - 1, int(0.95 * len(lat)))] if lat else 0
    auto = status_counts.get("auto_approved", 0)
    return {
        "invoices_by_status": status_counts,
        "straight_through_rate": round(auto / processed, 3) if processed else 0,
        "llm_calls": llm["calls"],
        "llm_failures": llm["failures"],
        "input_tokens": llm["tin"],
        "output_tokens": llm["tout"],
        "total_cost_usd": round(llm["cost"], 6),
        "avg_cost_per_invoice_usd": round(llm["cost"] / processed, 6) if processed else 0,
        "avg_latency_ms": round(llm["avg_lat"], 1),
        "p95_latency_ms": round(p95, 1),
        "per_agent": per_agent,
    }


# ---------- ERP lookup tools (used by the validator agent) ----------

def find_vendor(name: str) -> dict | None:
    with connect() as c:
        r = c.execute("SELECT * FROM vendors WHERE lower(name)=lower(?)", (name.strip(),)).fetchone()
        return dict(r) if r else None


def find_po(po_number: str) -> dict | None:
    with connect() as c:
        po = c.execute("SELECT * FROM purchase_orders WHERE po_number=?", (po_number.strip(),)).fetchone()
        if not po:
            return None
        po = dict(po)
        po["lines"] = {r["sku"]: dict(r) for r in c.execute(
            "SELECT * FROM po_lines WHERE po_number=?", (po["po_number"],))}
        po["receipts"] = {r["sku"]: r["qty_received"] for r in c.execute(
            "SELECT sku, qty_received FROM goods_receipts WHERE po_number=?", (po["po_number"],))}
        return po


def find_duplicates(invoice_number: str, vendor_name: str, exclude_id: int | None) -> list[dict]:
    with connect() as c:
        return [dict(r) for r in c.execute(
            "SELECT id, status FROM invoices WHERE invoice_number=? AND lower(vendor_name)=lower(?)"
            " AND status NOT IN ('rejected','processing','error') AND id != ?",
            (invoice_number, vendor_name, exclude_id or -1))]
