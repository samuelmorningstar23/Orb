"""Simulated lab and imaging results are marked, labelled and never flagged (F044).

THE FAILURE. bot_service._generate_lab_result and _create_report wrote rows
indistinguishable from genuine results: Sage read them as chart facts, the
Tier 1 screen raised renal flags off a random creatinine, and the bots ran
under ORB_INGESTION_MODE alone although config said ORB_DEMO_MODE gated them.

NOW. Both write source = 'simulated' (migration 46, which also marks the rows
the bots wrote before it); the chart API returns it; Sage prefixes the line;
Tier 1 does not read it; and the bots only run where ORB_DEMO_MODE allows
synthetic content.
"""
import asyncio
import time
import uuid

from fastapi.testclient import TestClient

from auth import create_token
from database import db_session
from services import bot_service

CREAT_HIGH = "BMP (STAT)\n  Creatinine: 4.2 mg/dL  [H]  (ref: 0.7-1.3)"
CREAT_NORMAL_LOOKING = "BMP\n  Creatinine: 2.6 mg/dL  [H]  (ref: 0.7-1.3)"


def _patient(status: str = "admitted") -> str:
    pid = f"PSIM{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO patients (id, name, mrn, ward, bed, diagnosis, status, age, sex) "
            "VALUES (?, 'Simulated Result Test', ?, 'GM', 'B-2', 'Sepsis', ?, 70, 'F')",
            (pid, f"MRN-{pid}", status),
        )
    return pid


def _lab(pid: str, content: str, *, source, created_at: str = ""):
    created_at = created_at or time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())
    with db_session() as db:
        db.execute(
            "INSERT OR IGNORE INTO users (id, name, role, password_hash) "
            "VALUES ('bot_lab', 'Orb Lab System', 'doctor', 'system')"
        )
        db.execute(
            "INSERT INTO clinical_documents (patient_id, doc_type, title, content, author_id, "
            "created_at, source) VALUES (?, 'lab_result', 'BMP', ?, 'bot_lab', ?, ?)",
            (pid, content, created_at, source),
        )


def test_tier1_raises_no_renal_flag_from_a_simulated_creatinine():
    from services.patient_specific_screen import _latest_creatinine

    pid = _patient()
    _lab(pid, CREAT_HIGH, source="simulated")
    with db_session() as db:
        assert _latest_creatinine(db, pid) is None


def test_tier1_still_reads_a_genuine_creatinine():
    from services.patient_specific_screen import _latest_creatinine

    pid = _patient()
    _lab(pid, CREAT_NORMAL_LOOKING, source=None)
    with db_session() as db:
        found = _latest_creatinine(db, pid)
    assert found is not None and found["value"] == 2.6


def test_a_newer_simulated_result_does_not_hide_an_older_genuine_one():
    from services.patient_specific_screen import _latest_creatinine

    pid = _patient()
    _lab(pid, CREAT_NORMAL_LOOKING, source=None, created_at="2026-10-03T01:00:00+00:00")
    _lab(pid, "BMP\n  Creatinine: 1.0 mg/dL    (ref: 0.7-1.3)", source="simulated",
         created_at="2026-10-03T02:00:00+00:00")
    with db_session() as db:
        found = _latest_creatinine(db, pid)
    # The simulated normal value is not read, so it cannot mask the genuine raised one.
    assert found is not None and found["value"] == 2.6


def test_the_lab_bot_marks_both_copies_simulated(monkeypatch):
    pid = _patient()
    with db_session() as db:
        others = [r["id"] for r in db.execute(
            "SELECT id FROM patients WHERE status = 'admitted' AND id != ?", (pid,)).fetchall()]
    # Only this patient is eligible: everyone else is on cooldown.
    now = time.time()
    monkeypatch.setattr(bot_service, "_lab_cooldown", {o: now for o in others})
    result = bot_service._generate_lab_result(now)
    assert result is not None and result[0] == pid
    with db_session() as db:
        doc = db.execute(
            "SELECT source FROM clinical_documents WHERE patient_id = ? AND doc_type = 'lab_result' "
            "ORDER BY id DESC LIMIT 1", (pid,)).fetchone()
        lens = db.execute(
            "SELECT source FROM lens_analyses WHERE patient_id = ? AND image_type = 'lab' "
            "ORDER BY id DESC LIMIT 1", (pid,)).fetchone()
    assert doc["source"] == "simulated"
    assert lens["source"] == "simulated"


def test_the_content_bots_do_not_run_without_demo_mode(monkeypatch):
    calls = []

    async def no_db(*a, **k):
        calls.append(a)
        raise AssertionError("a content bot wrote with ORB_DEMO_MODE off")

    monkeypatch.setattr("config.DEMO_MODE", False)
    monkeypatch.setattr(bot_service, "run_db", no_db)
    monkeypatch.setattr(bot_service.random, "random", lambda: 0.0)
    asyncio.run(bot_service.run_lab_bot())
    asyncio.run(bot_service.run_reports_bot())
    assert calls == []


def test_the_chart_api_returns_source_on_lab_rows():
    pid = _patient()
    _lab(pid, CREAT_HIGH, source="simulated")
    uid = f"SIMDOC{uuid.uuid4().hex[:6].upper()}"
    with db_session() as db:
        db.execute("INSERT INTO users (id, name, role, password_hash) VALUES (?, 'Dr Sim', 'doctor', 'x')", (uid,))
    from main import app
    r = TestClient(app).get(f"/api/vigil/patients/{pid}",
                            headers={"Authorization": f"Bearer {create_token(uid, 'doctor')}"})
    assert r.status_code == 200, r.text
    labs = [n for n in r.json()["notes"] if n["doc_type"] == "lab_result"]
    assert labs and labs[0]["source"] == "simulated"


def test_sage_names_a_simulated_lab_as_simulated():
    from services.sage_service import SIMULATED_RESULT_PREFIX, _assemble_patient_context

    pid = _patient()
    _lab(pid, CREAT_HIGH, source="simulated")
    with db_session() as db:
        ctx = _assemble_patient_context(db, pid, None)
    assert ctx["recent_labs"][0].startswith(SIMULATED_RESULT_PREFIX)


def test_the_migration_marks_rows_the_bots_wrote_before_it():
    from migrations import _m46_simulated_result_source

    pid = _patient()
    _lab(pid, CREAT_HIGH, source=None)              # an old bot row: author bot_lab, no source
    with db_session() as db:
        db.execute(
            "INSERT INTO clinical_documents (patient_id, doc_type, title, content, author_id) "
            "VALUES (?, 'note', 'Ward round', 'Seen', NULL)", (pid,))
        _m46_simulated_result_source(db)
        _m46_simulated_result_source(db)            # idempotent
        rows = db.execute(
            "SELECT doc_type, source FROM clinical_documents WHERE patient_id = ? ORDER BY id",
            (pid,)).fetchall()
    assert [(r["doc_type"], r["source"]) for r in rows] == [("lab_result", "simulated"), ("note", None)]
