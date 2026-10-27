from fastapi.testclient import TestClient


def client():
    from app.api import app
    return TestClient(app)


def test_upload_review_flow(make_pdf):
    with client() as c:
        r = c.post("/invoices", files={"file": ("inv.pdf", make_pdf("S02_price_mismatch").read_bytes(), "application/pdf")})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "pending_review"

        pending = c.get("/reviews/pending").json()
        assert [p["id"] for p in pending] == [body["invoice_id"]]

        r = c.post(f"/reviews/{body['invoice_id']}", json={"action": "approve", "reviewer": "priya"})
        assert r.json()["status"] == "approved"

        r = c.post(f"/reviews/{body['invoice_id']}", json={"action": "approve", "reviewer": "priya"})
        assert r.status_code == 409                    # cannot review twice

        m = c.get("/metrics").json()
        assert m["invoices_by_status"]["approved"] == 1
        assert m["llm_calls"] == 2


def test_upload_guardrail(make_pdf):
    with client() as c:
        r = c.post("/invoices", files={"file": ("x.pdf", b"hello", "application/pdf")})
        assert r.status_code == 422
        assert r.json()["detail"]["code"] == "NOT_A_PDF"


def test_api_key(monkeypatch):
    monkeypatch.setenv("API_KEY", "secret")
    with client() as c:
        assert c.get("/invoices").status_code == 401
        assert c.get("/invoices", headers={"x-api-key": "secret"}).status_code == 200
        assert c.get("/health").status_code == 200


def test_dashboard_served():
    with client() as c:
        r = c.get("/")
        assert r.status_code == 200 and "AP Review Desk" in r.text
