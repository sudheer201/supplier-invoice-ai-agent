import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_MODE", "mock")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("CHECKPOINT_PATH", str(tmp_path / "ck.db"))
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "uploads"))
    monkeypatch.setenv("LOG_LEVEL", "WARNING")
    monkeypatch.delenv("API_KEY", raising=False)
    from app.config import reload_settings
    reload_settings()
    from app import db
    db.init_db()
    yield


@pytest.fixture
def make_pdf(tmp_path):
    from eval.scenarios import SCENARIOS
    from scripts.generate_invoices import draw_invoice

    by_id = {s["id"]: s for s in SCENARIOS}

    def _make(scenario_id: str, **overrides) -> Path:
        inv = dict(by_id[scenario_id]["invoice"], **overrides)
        p = tmp_path / f"{scenario_id}.pdf"
        draw_invoice(inv, p)
        return p

    return _make
