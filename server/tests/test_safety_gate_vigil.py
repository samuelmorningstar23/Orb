"""W1 — order-time drug-safety gate on the primary Vigil med-add path.

POST /api/vigil/patients/{id}/medications must screen BEFORE any write:
clean orders chart with the safety payload attached, severe allergy alerts
428 until the clinician re-submits with an acknowledged safety_ack (the
override lands on the audit hash chain), drugs outside the interaction KB
are flagged as NOT screened, and the Idempotency-Key replay contract holds.
"""
import uuid

import pytest
from fastapi.testclient import TestClient

from main import app
from database import db_session
from auth import hash_password
from services.clinical_records_service import add_allergy

client = TestClient(app)


# ── fixtures ──────────────────────────────────────────────────────────────────
@pytest.fixture()
def patient():
    pid = f"PATGATE{uuid.uuid4().hex[:5].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO patients (id, name, mrn) VALUES (?, ?, ?)",
            (pid, "Gate Test", f"MRN-{uuid.uuid4().hex[:6]}"),
        )
    return pid


@pytest.fixture()
def doctor():
    uid = f"DGATE{uuid.uuid4().hex[:5].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT OR IGNORE INTO users (id, name, role, password_hash) VALUES (?,?,?,?)",
            (uid, f"Dr Gate {uid}", "doctor", hash_password("secretpass")),
        )
    return uid


@pytest.fixture()
def auth_hdr(doctor):
    resp = client.post("/api/auth/login",
                       json={"employee_id": doctor, "password": "secretpass"})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['token']}"}


def _order(hdr, patient_id, drug, key=None, ack=None):
    body = {"drug_name": drug, "dosage": "500mg", "frequency": "BD"}
    if ack is not None:
        body["safety_ack"] = ack
    headers = dict(hdr)
    if key:
        headers["Idempotency-Key"] = key
    return client.post(f"/api/vigil/patients/{patient_id}/medications",
                       json=body, headers=headers)


# ── (a) clean drug charts, safety payload in response ─────────────────────────
def test_clean_drug_orders_with_safety_payload(patient, auth_hdr):
    resp = _order(auth_hdr, patient, "Ibuprofen")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "success"
    assert body["safety"]["safe"] is True
    assert body["safety"]["max_severity"] == "none"
    assert body["safety"]["interaction_screened"] is True
    with db_session() as db:
        row = db.execute("SELECT source, prescribed_by FROM medications WHERE id = ?",
                         (body["id"],)).fetchone()
    assert row["source"] == "clinician"


# ── (b) severe allergy blocks with 428, nothing charted ───────────────────────
def test_severe_allergy_blocks_with_428(patient, auth_hdr):
    add_allergy(patient, "amoxicillin", reaction="anaphylaxis", severity="severe")
    resp = _order(auth_hdr, patient, "Amoxicillin 500mg")
    assert resp.status_code == 428, resp.text
    detail = resp.json()["detail"]
    assert detail["error"] == "drug_safety_acknowledgement_required"
    assert detail["safety"]["max_severity"] == "severe"
    assert any(a["type"] == "allergy" for a in detail["safety"]["allergy_alerts"])
    with db_session() as db:
        n = db.execute("SELECT count(*) FROM medications WHERE patient_id = ?",
                       (patient,)).fetchone()[0]
    assert n == 0, "blocked order must not reach the chart"


# ── (c) acknowledged override charts + audited on the chain ───────────────────
def test_acknowledged_override_orders_and_audits(patient, auth_hdr):
    add_allergy(patient, "amoxicillin", reaction="anaphylaxis", severity="severe")
    resp = _order(auth_hdr, patient, "Amoxicillin 500mg",
                  ack={"acknowledged": True, "reason": "desensitisation protocol"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["safety"]["override"]["reason"] == "desensitisation protocol"
    with db_session() as db:
        row = db.execute("SELECT id FROM medications WHERE id = ?", (body["id"],)).fetchone()
        log = db.execute(
            "SELECT * FROM audit_logs WHERE action_type = 'drug_safety_override' "
            "AND patient_id = ? ORDER BY timestamp DESC LIMIT 1",
            (patient,),
        ).fetchone()
    assert row is not None
    assert log is not None, "override must be audited"
    assert log["outcome"] == "partial_success"
    assert "desensitisation protocol" in log["summary_message"]


# ── (d) unknown drug charts but is flagged as NOT interaction-screened ────────
def test_unknown_drug_flagged_as_unscreened(patient, auth_hdr):
    resp = _order(auth_hdr, patient, "Obscurax Herbal Tonic")
    assert resp.status_code == 200, resp.text
    safety = resp.json()["safety"]
    assert safety["interaction_screened"] is False
    assert safety["coverage_note"], "unscreened drug must carry a coverage note"


# ── (e) Idempotency-Key replay returns the same order, no duplicate ───────────
def test_idempotent_replay_returns_same_order(patient, auth_hdr):
    key = f"idem-{uuid.uuid4().hex}"
    first = _order(auth_hdr, patient, "Ibuprofen", key=key)
    assert first.status_code == 200, first.text
    second = _order(auth_hdr, patient, "Ibuprofen", key=key)
    assert second.status_code == 200, second.text
    assert second.json()["id"] == first.json()["id"]
    assert "safety" in second.json()  # replay still surfaces the screen result
    with db_session() as db:
        n = db.execute("SELECT count(*) FROM medications WHERE idempotency_key = ?",
                       (key,)).fetchone()[0]
    assert n == 1
