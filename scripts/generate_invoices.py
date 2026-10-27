"""Generate realistic sample invoice PDFs (one per eval scenario).

    python -m scripts.generate_invoices            -> writes to samples/
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from eval.scenarios import SCENARIOS  # noqa: E402


def money(v: float) -> str:
    return f"{v:,.2f}"


def draw_invoice(inv: dict, path: Path) -> None:
    c = canvas.Canvas(str(path), pagesize=A4)
    w, h = A4
    y = h - 60
    c.setFillColor(colors.HexColor("#1f3a5f"))
    c.rect(0, h - 90, w, 90, fill=1, stroke=0)
    c.setFillColor(colors.white)
    c.setFont("Helvetica-Bold", 18)
    c.drawString(40, y, inv["vendor_name"].upper())
    c.setFont("Helvetica-Bold", 22)
    c.drawRightString(w - 40, y, "INVOICE")
    c.setFillColor(colors.black)

    y = h - 130
    c.setFont("Helvetica", 10)
    fields = [
        ("Invoice No", inv["invoice_number"]),
        ("Invoice Date", inv["invoice_date"]),
        ("Vendor", inv["vendor_name"]),
        ("Vendor Tax ID", inv["vendor_tax_id"]),
        ("PO Number", inv["po_number"]),
        ("Bank Account", inv["bank_account"]),
        ("Currency", inv["currency"]),
    ]
    for label, value in fields:
        c.drawString(40, y, f"{label}: {value}")
        y -= 16

    y -= 14
    cols = [40, 120, 330, 390, 480]
    c.setFont("Helvetica-Bold", 10)
    for x, t in zip(cols, ["SKU", "Description", "Qty", "Unit Price", "Amount"]):
        c.drawString(x, y, t)
    c.line(40, y - 4, w - 40, y - 4)
    y -= 20
    c.setFont("Helvetica", 10)
    for li in inv["line_items"]:
        qty = int(li["quantity"]) if float(li["quantity"]).is_integer() else li["quantity"]
        for x, t in zip(cols, [li["sku"], li["description"], str(qty), money(li["unit_price"]), money(li["amount"])]):
            c.drawString(x, y, t)
        y -= 16
    c.line(40, y + 6, w - 40, y + 6)

    y -= 14
    c.setFont("Helvetica", 10)
    c.drawRightString(w - 40, y, f"Subtotal: {money(inv['subtotal'])}")
    y -= 16
    c.drawRightString(w - 40, y, f"Tax: {money(inv['tax'])}")
    y -= 18
    c.setFont("Helvetica-Bold", 12)
    c.drawRightString(w - 40, y, f"Total: {money(inv['total'])}")

    if inv.get("notes"):
        y -= 40
        c.setFont("Helvetica-Oblique", 9)
        c.drawString(40, y, f"Notes: {inv['notes']}")

    c.setFont("Helvetica", 8)
    c.setFillColor(colors.grey)
    c.drawString(40, 40, "Payment terms: Net 30. Please quote the invoice number with your remittance.")
    c.save()


def main(out_dir: str = "samples") -> list[Path]:
    out = ROOT / out_dir
    out.mkdir(parents=True, exist_ok=True)
    paths = []
    for s in SCENARIOS:
        p = out / f"{s['id']}.pdf"
        draw_invoice(s["invoice"], p)
        paths.append(p)
    print(f"Wrote {len(paths)} invoices to {out}")
    return paths


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="samples")
    main(ap.parse_args().out)
