"""Illegible readings and "entered in error" (Orb Ward Record, 6 October 2026).

An illegible reading on the paper sheet is keyed as illegible, stored empty and
named in vitals.illegible, never guessed. A wrong set is marked entered in
error with a reason by a nurse or doctor (vitals.write); the clerk may strike
out only her own copies (tests/test_ward_record_clerk_void.py). The row stays
in the table, and every latest, window and trend reader skips it.
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from auth import create_token
from database import db_session
from routes.nurse_routes import router as nurse_router

DEPT_A = "Ward Record Void Medicine"
DEPT_B = "Ward Record Void Surgery"


@pytest.fixture()
def client():
    app = FastAPI()
    app.include_router(nurse_router)
    return TestClient(app)


def _auth(uid, role):
    return {"Authorization": f"Bearer {create_token(uid, role)}"}


def _mk_user(role, department):
    uid = f"WRV{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO users (id, name, role, password_hash, department) "
            "VALUES (?, ?, ?, 'x', ?)",
            (uid, f"Ward Record Void {role}", role, department),
        )
    return uid


def _mk_patient(department):
    pid = f"WRVP{uuid.uuid4().hex[:8].upper()}"
    # Admitted three days ago: a copied set may not be timed before the stay
    # began (nurse_shift_service.refuse_sheet_before_admission).
    now = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    with db_session() as db:
        db.execute(
            "INSERT OR IGNORE INTO users (id, name, role, password_hash) "
            "VALUES ('WRVADM', 'Ward Record Void Admitter', 'doctor', 'x')"
        )
        db.execute(
            """INSERT INTO patients (id, name, mrn, bed, ward, age, sex, diagnosis,
                                     status, admission_date)
               VALUES (?, ?, ?, 'B-7', 'WV-1', 71, 'F', 'Ward record void fixture',
                       'admitted', ?)""",
            (pid, f"WRV {department}", f"MRN-{uuid.uuid4().hex[:8].upper()}", now),
        )
        db.execute(
            "INSERT INTO admissions (patient_id, department, ward, bed, status, admitted_by, "
            "created_at) "
            "VALUES (?, ?, 'WV-1', 'B-7', 'active', 'WRVADM', ?)",
            (pid, department, now),
        )
    return pid


_GOOD = {"heart_rate": 82, "systolic_bp": 126, "diastolic_bp": 78,
         "respiratory_rate": 16, "temperature": 36.9, "spo2": 97,
         "consciousness": "alert", "on_oxygen": False}
# A mis-keyed set: the heart rate typed as 182.
_WRONG = {**_GOOD, "heart_rate": 182, "respiratory_rate": 28}


def _post(client, pid, uid, role, body, minutes_ago):
    taken = (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()
    r = client.post(f"/api/nurse/patients/{pid}/observations",
                    json={**body, "taken_at": taken}, headers=_auth(uid, role))
    assert r.status_code == 200, r.text
    return r.json()


def _row(vitals_id):
    with db_session() as db:
        return dict(db.execute("SELECT * FROM vitals WHERE id = ?", (vitals_id,)).fetchone())


# ── Illegible ────────────────────────────────────────────────────────────────

def test_a_transcribed_set_with_an_illegible_spo2_stores_null_and_the_marker(client):
    pid = _mk_patient(DEPT_A)
    clerk = _mk_user("ward_clerk", DEPT_A)
    out = _post(client, pid, clerk, "ward_clerk",
                {**_GOOD, "spo2": 96, "illegible": ["spo2"]}, minutes_ago=90)
    assert out["illegible"] == ["spo2"]
    row = _row(out["vitals_id"])
    assert row["spo2"] is None
    assert row["illegible"] == "spo2"
    assert row["heart_rate"] == 82
    assert row["source"] == "clerk_transcribed"


def test_illegible_acvpu_and_oxygen_are_stored_as_not_assessed(client):
    pid = _mk_patient(DEPT_A)
    clerk = _mk_user("ward_clerk", DEPT_A)
    out = _post(client, pid, clerk, "ward_clerk",
                {**_GOOD, "illegible": ["on_oxygen", "consciousness", "temperature"]},
                minutes_ago=60)
    row = _row(out["vitals_id"])
    assert row["consciousness"] is None and row["on_oxygen"] is None
    assert row["temperature"] is None
    # Stored in the fixed field order, whatever order was sent.
    assert row["illegible"] == "temperature,consciousness,on_oxygen"


def test_a_set_that_is_all_illegible_is_still_a_round(client):
    pid = _mk_patient(DEPT_A)
    clerk = _mk_user("ward_clerk", DEPT_A)
    every = ["heart_rate", "systolic_bp", "diastolic_bp", "spo2", "temperature",
             "respiratory_rate", "consciousness", "on_oxygen"]
    out = _post(client, pid, clerk, "ward_clerk", {"illegible": every}, minutes_ago=30)
    assert _row(out["vitals_id"])["illegible"] == ",".join(every)


def test_an_unknown_illegible_field_is_refused(client):
    pid = _mk_patient(DEPT_A)
    clerk = _mk_user("ward_clerk", DEPT_A)
    r = client.post(f"/api/nurse/patients/{pid}/observations",
                    json={**_GOOD, "illegible": ["pulse"]}, headers=_auth(clerk, "ward_clerk"))
    assert r.status_code == 422


def test_a_set_without_illegible_stores_no_marker(client):
    pid = _mk_patient(DEPT_A)
    nurse = _mk_user("nurse", DEPT_A)
    out = _post(client, pid, nurse, "nurse", _GOOD, minutes_ago=10)
    assert out["illegible"] == []
    assert _row(out["vitals_id"])["illegible"] is None


# ── Entered in error ─────────────────────────────────────────────────────────

def test_a_nurse_voids_a_set_and_the_row_stays_with_an_audit_entry(client):
    pid = _mk_patient(DEPT_A)
    nurse = _mk_user("nurse", DEPT_A)
    out = _post(client, pid, nurse, "nurse", _WRONG, minutes_ago=5)
    r = client.post(f"/api/nurse/observations/{out['vitals_id']}/void",
                    json={"reason": "Heart rate keyed as 182, sheet says 82"},
                    headers=_auth(nurse, "nurse"))
    assert r.status_code == 200, r.text
    row = _row(out["vitals_id"])
    assert row["heart_rate"] == 182          # the original is kept
    assert row["voided_at"] and row["voided_by"] == nurse
    assert row["void_reason"] == "Heart rate keyed as 182, sheet says 82"
    with db_session() as db:
        audit = db.execute(
            "SELECT COUNT(*) FROM audit_logs WHERE action_type = 'observation_voided' "
            "AND target_id = ?", (str(out["vitals_id"]),)).fetchone()[0]
    assert audit == 1


def test_a_doctor_may_void(client):
    pid = _mk_patient(DEPT_A)
    nurse = _mk_user("nurse", DEPT_A)
    doctor = _mk_user("doctor", DEPT_A)
    out = _post(client, pid, nurse, "nurse", _WRONG, minutes_ago=5)
    r = client.post(f"/api/nurse/observations/{out['vitals_id']}/void",
                    json={"reason": "Wrong patient"}, headers=_auth(doctor, "doctor"))
    assert r.status_code == 200


def test_the_clerk_cannot_void_a_nurses_set(client):
    pid = _mk_patient(DEPT_A)
    nurse = _mk_user("nurse", DEPT_A)
    clerk = _mk_user("ward_clerk", DEPT_A)
    out = _post(client, pid, nurse, "nurse", _WRONG, minutes_ago=5)
    r = client.post(f"/api/nurse/observations/{out['vitals_id']}/void",
                    json={"reason": "I typed it wrong"}, headers=_auth(clerk, "ward_clerk"))
    assert r.status_code == 403
    assert r.json()["detail"] == "You can only strike out a set you copied yourself."
    assert _row(out["vitals_id"])["voided_at"] is None


def test_a_reason_is_required(client):
    pid = _mk_patient(DEPT_A)
    nurse = _mk_user("nurse", DEPT_A)
    out = _post(client, pid, nurse, "nurse", _WRONG, minutes_ago=5)
    url = f"/api/nurse/observations/{out['vitals_id']}/void"
    assert client.post(url, json={}, headers=_auth(nurse, "nurse")).status_code == 422
    assert client.post(url, json={"reason": "   "}, headers=_auth(nurse, "nurse")).status_code == 422
    assert _row(out["vitals_id"])["voided_at"] is None


def test_a_second_void_is_refused_and_keeps_the_first_reason(client):
    pid = _mk_patient(DEPT_A)
    nurse = _mk_user("nurse", DEPT_A)
    out = _post(client, pid, nurse, "nurse", _WRONG, minutes_ago=5)
    url = f"/api/nurse/observations/{out['vitals_id']}/void"
    assert client.post(url, json={"reason": "first"}, headers=_auth(nurse, "nurse")).status_code == 200
    assert client.post(url, json={"reason": "second"}, headers=_auth(nurse, "nurse")).status_code == 409
    assert _row(out["vitals_id"])["void_reason"] == "first"


def test_void_is_ward_scoped_through_the_row(client):
    stranger = _mk_patient(DEPT_B)
    nurse_b = _mk_user("nurse", DEPT_B)
    nurse_a = _mk_user("nurse", DEPT_A)
    out = _post(client, stranger, nurse_b, "nurse", _WRONG, minutes_ago=5)
    r = client.post(f"/api/nurse/observations/{out['vitals_id']}/void",
                    json={"reason": "probe"}, headers=_auth(nurse_a, "nurse"))
    assert r.status_code == 404
    missing = client.post("/api/nurse/observations/999999999/void",
                          json={"reason": "probe"}, headers=_auth(nurse_a, "nurse"))
    assert missing.status_code == 404
    assert r.json() == missing.json()
    assert _row(out["vitals_id"])["voided_at"] is None
    with db_session() as db:
        denied = db.execute(
            "SELECT COUNT(*) FROM audit_logs WHERE action_type = 'patient_scope_denied' "
            "AND actor_user_id = ? AND patient_id = ?", (nurse_a, stranger)).fetchone()[0]
    assert denied == 1


# ── A voided set is not the latest anywhere ──────────────────────────────────

@pytest.fixture()
def voided_latest(client):
    """A good set 20 minutes ago, then a wrong NEWS2-high set 2 minutes ago
    that the nurse marks entered in error."""
    pid = _mk_patient(DEPT_A)
    nurse = _mk_user("nurse", DEPT_A)
    good = _post(client, pid, nurse, "nurse", _GOOD, minutes_ago=20)
    wrong = _post(client, pid, nurse, "nurse", _WRONG, minutes_ago=2)
    r = client.post(f"/api/nurse/observations/{wrong['vitals_id']}/void",
                    json={"reason": "mis-keyed"}, headers=_auth(nurse, "nurse"))
    assert r.status_code == 200
    return {"pid": pid, "good": good["vitals_id"], "wrong": wrong["vitals_id"]}


def test_vigil_latest_risk_and_is_latest_skip_the_voided_set(voided_latest):
    from services import vigil_service
    pid = voided_latest["pid"]
    with db_session() as db:
        assert vigil_service.is_latest_vitals_row(db, pid, voided_latest["good"])
        assert not vigil_service.is_latest_vitals_row(db, pid, voided_latest["wrong"])
    risk = vigil_service.get_patient_risk(pid)
    assert risk["level"] == "stable"


def test_the_board_scores_the_set_before_the_voided_one(voided_latest):
    from services import vigil_service
    scored = vigil_service.scored_admitted_patients(DEPT_A)
    mine = [(p, v, n) for p, v, n in scored if p["id"] == voided_latest["pid"]]
    assert mine, "patient missing from the board"
    assert mine[0][1]["id"] == voided_latest["good"]


def test_the_nurse_shift_board_skips_the_voided_set(voided_latest):
    from services import nurse_shift_service
    _patients, latest_vitals, *_rest = nurse_shift_service._load_board_rows(DEPT_A)
    assert latest_vitals[voided_latest["pid"]]["id"] == voided_latest["good"]


def test_ipass_downtime_and_fhir_skip_the_voided_set(voided_latest):
    from services import downtime_service, fhir_export_service, ipass_service
    pid = voided_latest["pid"]
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    with db_session() as db:
        window = ipass_service._get_vitals_window(db, pid, cutoff)
    assert [v["id"] for v in window] == [voided_latest["good"]]
    obs = fhir_export_service.observations_to_fhir(pid)
    assert obs and all(f"orb-obs-{voided_latest['wrong']}-" not in o["id"] for o in obs)
    assert any(f"orb-obs-{voided_latest['good']}-" in o["id"] for o in obs)
    snap = downtime_service.capture_snapshot(pid, created_by="test")
    assert snap["snapshot"]["latest_vitals"]["heart_rate"] == 82


def test_the_chart_reads_skip_the_voided_set(voided_latest):
    from routes import vigil_routes
    pid = voided_latest["pid"]
    bundle = vigil_routes._load_patient_bundle(pid)
    chart_vitals = bundle[1]
    assert [dict(v)["id"] for v in chart_vitals] == [voided_latest["good"]]
    recent = vigil_routes._load_recent_vitals(pid, 1)
    assert [dict(v)["id"] for v in recent] == [voided_latest["good"]]


def test_the_shift_board_names_the_last_unvoided_set_for_the_void_action(voided_latest):
    from services import nurse_shift_service
    board = nurse_shift_service.build_board(DEPT_A)
    card = next(c for c in board["patients"] if c["patient_id"] == voided_latest["pid"])
    assert card["last_obs_vitals_id"] == voided_latest["good"]
