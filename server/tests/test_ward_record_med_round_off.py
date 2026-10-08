"""The medication round is off on an Orb Ward Record computer (9 October 2026).

SALES_PLAN_2026-10.md section 3, step 2, "What is off": the drug checks and
the interlock are off, and there are no orders. The nurse shift board still
built medication tasks from the charted drugs, counted "doses overdue" and
"blocked by allergy", and named an allergy block that needs prescriber
authorisation. Under ORB_WARD_RECORD the board now carries no medication task,
no dose count and no allergy block line, and every eMAR route answers 404 in
plain words. With only ORB_SCORING_ENABLED off the round stays as it was.
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import config
from auth import create_token
from database import db_session
from routes.emar_routes import MEDICATION_ROUND_SWITCHED_OFF
from routes.emar_routes import router as emar_router
from routes.nurse_routes import router as nurse_router
from services import nurse_shift_service as nurse

DEPT = "Ward Record Med Round Ward"


@pytest.fixture()
def client():
    app = FastAPI()
    app.include_router(nurse_router)
    app.include_router(emar_router)
    return TestClient(app)


def _mk_user(role):
    uid = f"WMR{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO users (id, name, role, password_hash, department) VALUES (?, ?, ?, 'x', ?)",
            (uid, f"Med round {role}", role, DEPT),
        )
    return uid, {"Authorization": f"Bearer {create_token(uid, role)}"}


@pytest.fixture()
def patient():
    """An admitted patient with a penicillin allergy, and amoxicillin and
    paracetamol charted for two days with no dose given."""
    pid = f"WMRP{uuid.uuid4().hex[:8].upper()}"
    started = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    with db_session() as db:
        db.execute(
            "INSERT OR IGNORE INTO users (id, name, role, password_hash) "
            "VALUES ('WMRADM', 'Med Round Admitter', 'doctor', 'x')")
        db.execute(
            """INSERT INTO patients (id, name, mrn, bed, ward, age, sex, diagnosis, status)
               VALUES (?, 'Med Round Patient', ?, 'MR-1', 'MR', 70, 'F', 'Fixture', 'admitted')""",
            (pid, f"MRN-{uuid.uuid4().hex[:8].upper()}"))
        db.execute(
            "INSERT INTO admissions (patient_id, department, status, admitted_by) "
            "VALUES (?, ?, 'active', 'WMRADM')", (pid, DEPT))
        db.execute(
            "INSERT INTO patient_allergies (patient_id, substance, substance_class, reaction, "
            "severity, status) VALUES (?, 'Penicillin', 'penicillin', 'Anaphylaxis', 'severe', 'active')",
            (pid,))
        for drug in ("Amoxicillin", "Paracetamol"):
            db.execute(
                """INSERT INTO medications (patient_id, drug_name, dosage, route, frequency,
                                            status, prescribed_by, source, started_at)
                   VALUES (?, ?, '500mg', 'Oral', 'Q6H', 'active', 'WMRADM', 'clinician', ?)""",
                (pid, drug, started))
        med_id = db.execute(
            "SELECT id FROM medications WHERE patient_id = ? ORDER BY id LIMIT 1", (pid,)
        ).fetchone()["id"]
    yield pid, med_id
    with db_session() as db:
        db.execute("UPDATE patients SET status = 'discharged' WHERE id = ?", (pid,))
        db.execute("UPDATE admissions SET status = 'discharged' WHERE patient_id = ?", (pid,))


def _card(board, pid):
    return next(c for c in board["patients"] if c["patient_id"] == pid)


def test_the_board_carries_no_medication_round_under_the_profile(monkeypatch, patient):
    pid, _ = patient
    monkeypatch.setattr(config, "WARD_RECORD", True, raising=False)
    board = nurse.build_board(DEPT)
    assert board["medication_round"] is False
    card = _card(board, pid)
    assert card["medications"] == []
    assert card["meds_overdue"] is None
    assert card["meds_due_soon"] is None
    assert card["meds_allergy_blocked"] is None
    assert board["totals"]["meds_overdue"] is None
    assert board["totals"]["meds_allergy_blocked"] is None
    # The allergy itself is a fact about the patient and stays on the card.
    assert [a["substance"] for a in card["allergies"]] == ["Penicillin"]


def test_scoring_off_alone_keeps_the_medication_round(monkeypatch, patient):
    pid, _ = patient
    monkeypatch.setattr(config, "WARD_RECORD", False, raising=False)
    monkeypatch.setattr(config, "SCORING_ENABLED", False, raising=False)
    board = nurse.build_board(DEPT)
    assert board["medication_round"] is True
    card = _card(board, pid)
    assert {m["drug_name"] for m in card["medications"]} == {"Amoxicillin", "Paracetamol"}
    assert isinstance(card["meds_overdue"], int) and isinstance(card["meds_due_soon"], int)
    assert card["meds_allergy_blocked"] == 1
    assert isinstance(board["totals"]["meds_overdue"], int)
    assert board["totals"]["meds_allergy_blocked"] >= 1


def test_the_board_route_sends_no_medication_round_under_the_profile(monkeypatch, client, patient):
    pid, _ = patient
    monkeypatch.setattr(config, "WARD_RECORD", True, raising=False)
    _, hdr = _mk_user("nurse")
    r = client.get("/api/nurse/board", headers=hdr)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["medication_round"] is False
    assert _card(body, pid)["medications"] == []
    assert "Amoxicillin" not in r.text


def test_every_emar_route_answers_404_under_the_profile(monkeypatch, client, patient):
    pid, med_id = patient
    monkeypatch.setattr(config, "WARD_RECORD", True, raising=False)
    _, hdr = _mk_user("nurse")
    r = client.get(f"/api/emar/patients/{pid}/administrations", headers=hdr)
    assert r.status_code == 404 and r.json()["detail"] == MEDICATION_ROUND_SWITCHED_OFF
    r = client.post(f"/api/emar/medications/{med_id}/administer", headers=hdr,
                    json={"status": "given", "allergy_override_reason": "challenge"})
    assert r.status_code == 404 and r.json()["detail"] == MEDICATION_ROUND_SWITCHED_OFF
    with db_session() as db:
        given = db.execute(
            "SELECT COUNT(*) FROM medication_administrations WHERE medication_id = ?",
            (med_id,)).fetchone()[0]
    assert given == 0
    for route in emar_router.routes:
        assert any(getattr(d.dependency, "__name__", "") == "_medication_round_switched_on"
                   for d in route.dependencies), route.path


def test_the_emar_answers_as_before_with_scoring_off_alone(monkeypatch, client, patient):
    pid, _ = patient
    monkeypatch.setattr(config, "WARD_RECORD", False, raising=False)
    monkeypatch.setattr(config, "SCORING_ENABLED", False, raising=False)
    _, hdr = _mk_user("nurse")
    r = client.get(f"/api/emar/patients/{pid}/administrations", headers=hdr)
    assert r.status_code == 200, r.text
