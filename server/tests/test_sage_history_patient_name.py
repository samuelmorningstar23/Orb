"""The history sidebar labels a past conversation with its patient (F026).

/history projected only timing, so every reopened thread read "General
Medical", including threads about a patient. The live turn already named the
patient (metadata.patient_name); the stored turn now keeps that name and
/history hands it back. A row without one names nobody.
"""
import json

import pytest
from fastapi.testclient import TestClient

from auth import hash_password
from database import db_session
from main import app
from routes.sage_routes import _chart_patient_name, _stored_answer_meta
from services.sage_service import save_conversation

client = TestClient(app)

DOC = "HISTNAME-DOC"
DEPT = "General Medicine"


@pytest.fixture(autouse=True)
def _fresh():
    with db_session() as db:
        db.execute(
            "INSERT OR REPLACE INTO users (id, name, role, department, password_hash) "
            "VALUES (?, ?, ?, ?, ?)",
            (DOC, "Dr. Hist Name", "doctor", DEPT, hash_password("password")),
        )
        db.execute("DELETE FROM sage_conversations WHERE user_id = ?", (DOC,))
    yield
    with db_session() as db:
        db.execute("DELETE FROM sage_conversations WHERE user_id = ?", (DOC,))


def _history() -> list:
    r = client.post("/api/auth/login", json={"employee_id": DOC, "password": "password"})
    assert r.status_code == 200, r.text
    h = client.get("/api/sage/history",
                   headers={"Authorization": f"Bearer {r.json()['token']}"})
    assert h.status_code == 200, h.text
    return h.json()["messages"]


def test_history_projects_the_patient_the_turn_was_about():
    save_conversation(DOC, "user", "how is she overnight?")
    save_conversation(DOC, "assistant", "Stable overnight.",
                      sources=json.dumps({"patient_name": "Lakshmi Devi",
                                          "timing": {"ttft_ms": 900, "total_ms": 4000}}))
    answer = [m for m in _history() if m["role"] == "assistant"][-1]
    assert answer["metadata"]["patient_name"] == "Lakshmi Devi"
    assert answer["metadata"]["timing"]["total_ms"] == 4000


def test_an_untimed_row_still_carries_its_patient():
    save_conversation(DOC, "user", "q")
    save_conversation(DOC, "assistant", "a",
                      sources=json.dumps({"patient_name": "Lakshmi Devi", "timing": None}))
    answer = [m for m in _history() if m["role"] == "assistant"][-1]
    assert answer["metadata"] == {"patient_name": "Lakshmi Devi"}


def test_a_row_without_a_patient_names_nobody():
    save_conversation(DOC, "user", "what is qSOFA?")
    save_conversation(DOC, "assistant", "A bedside score.",
                      sources=json.dumps({"patient_name": None,
                                          "timing": {"ttft_ms": 500, "total_ms": 900}}))
    answer = [m for m in _history() if m["role"] == "assistant"][-1]
    assert "patient_name" not in answer["metadata"]


@pytest.mark.parametrize("blob", ['{"patient_name": ""}', '{"patient_name": 7}', "{}"])
def test_stored_answer_meta_ignores_an_empty_or_odd_name(blob):
    assert _stored_answer_meta(blob) is None


def test_chart_patient_name_reads_the_demographics_line():
    assert _chart_patient_name({"demographics": "PATIENT: Lakshmi Devi, 72y/F, Bed GM-4"}) == "Lakshmi Devi"
    assert _chart_patient_name(None) is None
    assert _chart_patient_name({"patient_id": "P1", "voice_chart_text": "..."}) is None


def test_a_live_patient_turn_stores_the_name(monkeypatch):
    chart = {"patient_id": "HN-P1", "demographics": "PATIENT: Hist Patient, 60y/M, Bed GM-9",
             "completeness": {"missing": []}}

    def _ctx(query, user, patient_id=None, history=None):
        return ("CONTEXT BLOCK", "broad", [], "high", False, chart, "PATIENT_SPECIFIC")

    async def _llm(*args, **kwargs):
        kwargs["route_info"].update({"provider": "stub", "model": "medgemma:27b"})
        yield "Stable overnight."

    monkeypatch.setattr("routes.sage_routes.get_relevant_context", _ctx)
    monkeypatch.setattr("routes.sage_routes.query_ollama", _llm)
    r = client.post("/api/auth/login", json={"employee_id": DOC, "password": "password"})
    resp = client.post("/api/sage/query", json={"query": "how is Hist Patient?"},
                       headers={"Authorization": f"Bearer {r.json()['token']}"})
    assert resp.status_code == 200, resp.text
    with db_session() as db:
        row = db.execute("SELECT sources FROM sage_conversations WHERE user_id = ? "
                         "AND role = 'assistant' ORDER BY id DESC LIMIT 1", (DOC,)).fetchone()
    assert json.loads(row["sources"])["patient_name"] == "Hist Patient"
