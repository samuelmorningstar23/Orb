"""The one-click patient erase is retired; DPDP erasure is adjudicated.

POST /api/admin/patients/{id}/erase used to delete every patient table except
audit_logs in one call, with no retention check, legal hold, minor floor or
erasure_requests row. consent_service.py calls exactly that outcome illegal
under Indian medical-record law. These tests pin the replacement contract:

  * the legacy route answers 410 and deletes NOTHING, and the attempt is
    audited;
  * the adjudicated path refuses the medical record WITH the citation that
    compels retention, so the page that replaced the button has something
    lawful to show.
"""
import uuid

from fastapi.testclient import TestClient

from auth import create_token
from database import db_session
from main import app

client = TestClient(app)


def _auth(uid, role="admin"):
    return {"Authorization": f"Bearer {create_token(uid, role)}"}


def _admin():
    uid = f"ERADM{uuid.uuid4().hex[:6].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT OR IGNORE INTO users (id, name, role, password_hash) "
            "VALUES (?, 'Erase Retired Admin', 'admin', 'x')", (uid,))
    return uid


def _discharged_patient_with_a_vital():
    pid = f"ERP{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO patients (id, name, mrn, age, sex, diagnosis, status) "
            "VALUES (?, 'Erase Retired Fixture', ?, 44, 'F', 'Fixture', 'discharged')",
            (pid, f"MRN-{pid}"))
        db.execute(
            "INSERT INTO vitals (patient_id, heart_rate, timestamp, source) "
            "VALUES (?, 82, CURRENT_TIMESTAMP, 'manual')", (pid,))
    return pid


def test_the_legacy_erase_route_is_gone_and_deletes_nothing():
    admin, pid = _admin(), _discharged_patient_with_a_vital()

    resp = client.post(f"/api/admin/patients/{pid}/erase", headers=_auth(admin))

    assert resp.status_code == 410, resp.text
    assert "erasure request" in resp.json()["detail"]
    with db_session() as db:
        patient = db.execute("SELECT name FROM patients WHERE id = ?", (pid,)).fetchone()
        vitals = db.execute("SELECT COUNT(*) c FROM vitals WHERE patient_id = ?",
                            (pid,)).fetchone()["c"]
        refused = db.execute(
            "SELECT outcome FROM audit_logs WHERE action_type = 'patient_erase_refused' "
            "AND patient_id = ? ORDER BY id DESC LIMIT 1", (pid,)).fetchone()
    assert patient["name"] == "Erase Retired Fixture", "the retired route anonymised the patient"
    assert vitals == 1, "the retired route deleted clinical rows"
    assert refused is not None and refused["outcome"] == "denied", \
        "the refused attempt left no audit trace"


def test_the_adjudicated_path_refuses_the_medical_record_with_its_citation():
    admin, pid = _admin(), _discharged_patient_with_a_vital()

    req = client.post("/api/admin/compliance/erasure", headers=_auth(admin),
                      json={"patient_id": pid, "note": "patient asked in person"})
    assert req.status_code == 200, req.text
    decided = client.post(f"/api/admin/compliance/erasure/{req.json()['id']}/adjudicate",
                          headers=_auth(admin))
    assert decided.status_code == 200, decided.text

    by_cat = {d["category"]: d for d in decided.json()["decisions"]}
    clinical = by_cat["clinical_record"]
    assert clinical["disposition"] == "refused"
    assert "Indian Medical Council" in (clinical["authority"] or ""), \
        "a refusal of the medical record must cite what compels retention"
    # Deciding destroys nothing; execution is a separate act.
    with db_session() as db:
        vitals = db.execute("SELECT COUNT(*) c FROM vitals WHERE patient_id = ?",
                            (pid,)).fetchone()["c"]
    assert vitals == 1
