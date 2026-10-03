"""eMAR — bedside medication administration records.

Covers the service layer (given defaults from the order + audited on the hash
chain; not_given requires a reason; unverified AI meds and non-active orders
are not administrable; idempotent replay) and the HTTP routes wired with
medication.write / patient.read_all auth.
"""
import json
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from database import db_session, verify_audit_chain
from services import emar_service
from routes.emar_routes import router as emar_router
from auth import create_token


# ── fixtures ──────────────────────────────────────────────────────────────────
@pytest.fixture()
def patient():
    pid = f"PATEM{uuid.uuid4().hex[:6].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO patients (id, name, mrn) VALUES (?, ?, ?)",
            (pid, "eMAR Test", f"MRN-{uuid.uuid4().hex[:6]}"),
        )
    return pid


@pytest.fixture()
def nurse():
    uid = f"NUR{uuid.uuid4().hex[:6].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT OR IGNORE INTO users (id, name, role, password_hash) VALUES (?, ?, 'nurse', 'x')",
            (uid, "Nurse eMAR"),
        )
    return uid


def _chart_med(patient_id, drug="Paracetamol", dosage="500mg", route="Oral",
               frequency="QDS", status="active", source="clinician", verified_by=None):
    """Chart a medication directly, return its id."""
    with db_session() as db:
        cur = db.execute(
            """INSERT INTO medications (patient_id, drug_name, dosage, route, frequency,
                                        status, prescribed_by, source, verified_by)
               VALUES (?, ?, ?, ?, ?, ?, 'D001', ?, ?)""",
            (patient_id, drug, dosage, route, frequency, status, source, verified_by),
        )
        return cur.lastrowid


def _latest_audit(action_type, patient_id):
    with db_session() as db:
        return db.execute(
            """SELECT * FROM audit_logs
               WHERE action_type = ? AND patient_id = ?
               ORDER BY id DESC LIMIT 1""",
            (action_type, patient_id),
        ).fetchone()


# ── service: given happy path ─────────────────────────────────────────────────
def test_given_defaults_from_order_and_audited(patient, nurse):
    med_id = _chart_med(patient)
    record = emar_service.record_administration(med_id, nurse, status="given")

    assert record["status"] == "given"
    assert record["dose_given"] == "500mg"      # defaulted from the order
    assert record["route_given"] == "Oral"      # defaulted from the order
    assert record["drug_name"] == "Paracetamol"
    assert record["administered_by"] == nurse
    assert record["administered_by_name"] == "Nurse eMAR"
    assert record["administered_at"] is not None
    assert record["replayed"] is False

    with db_session() as db:
        row = db.execute(
            "SELECT * FROM medication_administrations WHERE id = ?", (record["id"],)
        ).fetchone()
    assert row["medication_id"] == med_id
    assert row["patient_id"] == patient
    assert row["confirmation_method"] == "typed"

    audit = _latest_audit("medication_administered", patient)
    assert audit is not None, "expected a clinical_access audit row for the administration"
    assert audit["event_category"] == "clinical_access"
    assert audit["outcome"] == "success"
    assert audit["actor_user_id"] == nurse
    meta = json.loads(audit["metadata_json"])
    assert meta["medication_id"] == med_id
    assert meta["drug_name"] == "Paracetamol"
    assert meta["dose_given"] == "500mg"
    assert meta["confirmation_method"] == "typed"

    chain = verify_audit_chain()
    assert chain["ok"], f"audit chain broken: {chain['broken_row_ids']}"


def test_given_explicit_dose_overrides_order(patient, nurse):
    med_id = _chart_med(patient, dosage="1g", route="Oral")
    record = emar_service.record_administration(
        med_id, nurse, status="given", dose_given="500mg", route_given="NG tube",
        confirmation_method="barcode", notes="half dose per Dr order",
    )
    assert record["dose_given"] == "500mg"
    assert record["route_given"] == "NG tube"
    assert record["confirmation_method"] == "barcode"
    assert record["notes"] == "half dose per Dr order"


# ── service: not_given ────────────────────────────────────────────────────────
def test_not_given_requires_reason(patient, nurse):
    med_id = _chart_med(patient)
    with pytest.raises(ValueError):
        emar_service.record_administration(med_id, nurse, status="not_given")
    with pytest.raises(ValueError):
        emar_service.record_administration(med_id, nurse, status="not_given",
                                           not_given_reason="   ")


def test_not_given_with_reason_recorded_and_audited(patient, nurse):
    med_id = _chart_med(patient)
    record = emar_service.record_administration(
        med_id, nurse, status="not_given", not_given_reason="Patient refused",
    )
    assert record["status"] == "not_given"
    assert record["not_given_reason"] == "Patient refused"
    assert record["dose_given"] is None  # nothing was given

    audit = _latest_audit("medication_not_given", patient)
    assert audit is not None
    assert json.loads(audit["metadata_json"])["not_given_reason"] == "Patient refused"


# ── service: administrability guards ──────────────────────────────────────────
def test_unverified_ai_med_not_administrable(patient, nurse):
    med_id = _chart_med(patient, drug="Amoxicillin", source="llm_extracted")
    with pytest.raises(ValueError, match="unverified medication"):
        emar_service.record_administration(med_id, nurse, status="given")


def test_verified_ai_med_is_administrable(patient, nurse):
    med_id = _chart_med(patient, drug="Amoxicillin", source="llm_extracted",
                        verified_by="D001")
    record = emar_service.record_administration(med_id, nurse, status="given")
    assert record["status"] == "given"


def test_discontinued_med_not_administrable(patient, nurse):
    med_id = _chart_med(patient, status="discontinued")
    with pytest.raises(ValueError, match="not an active order"):
        emar_service.record_administration(med_id, nurse, status="given")


def test_unknown_med_and_bad_inputs(patient, nurse):
    with pytest.raises(ValueError, match="not found"):
        emar_service.record_administration(999999999, nurse, status="given")
    med_id = _chart_med(patient)
    with pytest.raises(ValueError, match="status"):
        emar_service.record_administration(med_id, nurse, status="maybe")
    with pytest.raises(ValueError, match="confirmation_method"):
        emar_service.record_administration(med_id, nurse, status="given",
                                           confirmation_method="rfid")


# ── service: idempotency + summary ────────────────────────────────────────────
def test_idempotent_replay_returns_existing_row(patient, nurse):
    med_id = _chart_med(patient)
    key = f"emar-{uuid.uuid4().hex}"
    first = emar_service.record_administration(med_id, nurse, status="given",
                                               idempotency_key=key)
    replay = emar_service.record_administration(med_id, nurse, status="given",
                                                idempotency_key=key)
    assert replay["id"] == first["id"]
    assert replay["replayed"] is True
    with db_session() as db:
        count = db.execute(
            "SELECT COUNT(*) AS c FROM medication_administrations WHERE idempotency_key = ?",
            (key,),
        ).fetchone()["c"]
    assert count == 1


def test_audit_failure_rolls_back_administration(patient, nurse, monkeypatch):
    """Fail-closed means ATOMIC: if the clinical_access audit write fails, the
    administration row must not survive — otherwise an idempotent replay would
    return a committed-but-forever-unaudited dose."""
    med_id = _chart_med(patient)
    key = f"emar-{uuid.uuid4().hex}"

    def _boom(*args, **kwargs):
        raise RuntimeError("audit store unavailable")

    monkeypatch.setattr(emar_service, "log_audit_event", _boom)
    with pytest.raises(RuntimeError, match="audit store unavailable"):
        emar_service.record_administration(med_id, nurse, status="given",
                                           idempotency_key=key)
    monkeypatch.undo()

    with db_session() as db:
        count = db.execute(
            "SELECT COUNT(*) AS c FROM medication_administrations WHERE medication_id = ?",
            (med_id,),
        ).fetchone()["c"]
    assert count == 0, "administration row must roll back with the failed audit"
    # A retry after the audit outage is a fresh administration, not a replay.
    record = emar_service.record_administration(med_id, nurse, status="given",
                                                idempotency_key=key)
    assert record["replayed"] is False


def test_administration_summary_counts_given_only(patient, nurse):
    med_id = _chart_med(patient)
    assert emar_service.administration_summary(med_id) == {"last_given_at": None, "count": 0}
    emar_service.record_administration(med_id, nurse, status="given")
    emar_service.record_administration(med_id, nurse, status="given")
    emar_service.record_administration(med_id, nurse, status="not_given",
                                       not_given_reason="NPO for surgery")
    summary = emar_service.administration_summary(med_id)
    assert summary["count"] == 2
    assert summary["last_given_at"] is not None


# ── HTTP routes ───────────────────────────────────────────────────────────────
@pytest.fixture()
def client():
    app = FastAPI()
    app.include_router(emar_router)
    return TestClient(app)


def _auth(uid, role="nurse"):
    return {"Authorization": f"Bearer {create_token(uid, role)}"}


def test_route_administer_and_history_flow(patient, nurse, client):
    med_id = _chart_med(patient, drug="Aspirin", dosage="75mg", route="Oral")
    hdr = _auth(nurse)

    resp = client.post(f"/api/emar/medications/{med_id}/administer",
                       json={"status": "given"}, headers=hdr)
    assert resp.status_code == 200, resp.text
    record = resp.json()["administration"]
    assert record["dose_given"] == "75mg"
    assert record["route_given"] == "Oral"

    resp = client.post(f"/api/emar/medications/{med_id}/administer",
                       json={"status": "not_given", "not_given_reason": "Held pre-op"},
                       headers=hdr)
    assert resp.status_code == 200, resp.text

    resp = client.get(f"/api/emar/patients/{patient}/administrations", headers=hdr)
    assert resp.status_code == 200
    rows = resp.json()["administrations"]
    assert len(rows) == 2
    # newest first
    assert rows[0]["status"] == "not_given"
    assert rows[1]["status"] == "given"
    assert all(r["drug_name"] == "Aspirin" for r in rows)
    assert rows[0]["administered_by_name"] == "Nurse eMAR"


def test_route_idempotency_key_header_replay(patient, nurse, client):
    med_id = _chart_med(patient)
    hdr = {**_auth(nurse), "Idempotency-Key": f"emar-{uuid.uuid4().hex}"}
    first = client.post(f"/api/emar/medications/{med_id}/administer",
                        json={"status": "given"}, headers=hdr)
    replay = client.post(f"/api/emar/medications/{med_id}/administer",
                         json={"status": "given"}, headers=hdr)
    assert first.status_code == 200 and replay.status_code == 200
    assert replay.json()["administration"]["id"] == first.json()["administration"]["id"]
    assert replay.json()["administration"]["replayed"] is True


def test_route_guards_map_to_422(patient, nurse, client):
    hdr = _auth(nurse)

    # not_given without a reason
    med_id = _chart_med(patient)
    resp = client.post(f"/api/emar/medications/{med_id}/administer",
                       json={"status": "not_given"}, headers=hdr)
    assert resp.status_code == 422
    assert "reason" in resp.json()["detail"]

    # unverified AI/agentic med
    ai_id = _chart_med(patient, drug="Amoxicillin", source="llm_extracted")
    resp = client.post(f"/api/emar/medications/{ai_id}/administer",
                       json={"status": "given"}, headers=hdr)
    assert resp.status_code == 422
    assert "unverified medication" in resp.json()["detail"]

    # unknown medication
    resp = client.post("/api/emar/medications/999999999/administer",
                       json={"status": "given"}, headers=hdr)
    assert resp.status_code == 422


def test_route_history_404_for_unknown_patient(nurse, client):
    resp = client.get("/api/emar/patients/NOPE-404/administrations", headers=_auth(nurse))
    assert resp.status_code == 404


def test_route_forbidden_for_patient_role(patient, client):
    med_id = _chart_med(patient)
    with db_session() as db:
        db.execute("INSERT OR IGNORE INTO users (id, name, role, password_hash) "
                   "VALUES ('PAT-EMAR-1', 'Pat', 'patient', 'x')")
    hdr = _auth("PAT-EMAR-1", "patient")
    resp = client.post(f"/api/emar/medications/{med_id}/administer",
                       json={"status": "given"}, headers=hdr)
    assert resp.status_code == 403
    resp = client.get(f"/api/emar/patients/{patient}/administrations", headers=hdr)
    assert resp.status_code == 403
