"""W4 — wrong-patient fix on Helix approve + drug-safety gate on CPOE med orders.

POST /api/helix/orders/approve must never guess the patient: an ambiguous
bare name fails 422 with the candidate count (no LIKE scan, no 'UNKNOWN'
fallback), an explicit patient_id is used verbatim (404 if unknown), and the
resolved order passes the medication safety gate (severe allergy -> 428 until
acknowledged). POST /api/orders medication orders pass the same gate after
terminology validation and before persisting; non-medication orders are
unaffected.
"""
import uuid

import pytest
from fastapi.testclient import TestClient

from main import app
from database import db_session
from auth import hash_password
from services.clinical_records_service import add_allergy
from services.helix_db import init_helix_db
from services.terminology_service import seed_terminology

client = TestClient(app)

# Real RxNorm ingredient code so a coded medication order can pass CPOE
# terminology validation (the curated seed ships no RXNORM rows).
_RXNORM_AMOX = ("RXNORM", "723", "amoxicillin")


# ── fixtures ──────────────────────────────────────────────────────────────────
@pytest.fixture(scope="module", autouse=True)
def _seed_module():
    """Helix schema + catalog row, terminology (incl. an RXNORM med code)."""
    init_helix_db()
    seed_terminology()
    with db_session() as db:
        db.execute(
            "INSERT OR IGNORE INTO terminology (code_system, code, display, is_active) "
            "VALUES (?, ?, ?, 1)",
            _RXNORM_AMOX,
        )
        db.execute(
            "INSERT OR IGNORE INTO helix_medication_master (id, name, generic_name, form, strength) "
            "VALUES ('HXGATEAMOX', 'Amoxicillin 500mg Capsule', 'amoxicillin', 'capsule', '500mg')",
        )


def _mk_patient(name):
    pid = f"PATW4{uuid.uuid4().hex[:6].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO patients (id, name, mrn) VALUES (?, ?, ?)",
            (pid, name, f"MRN-{uuid.uuid4().hex[:6]}"),
        )
    return pid


@pytest.fixture()
def patient():
    return _mk_patient(f"Gate Order {uuid.uuid4().hex[:5]}")


def _mk_user(role):
    uid = f"UW4{uuid.uuid4().hex[:5].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT OR IGNORE INTO users (id, name, role, password_hash) VALUES (?,?,?,?)",
            (uid, f"{role.title()} {uid}", role, hash_password("secretpass")),
        )
    return uid


def _login_hdr(user_id):
    resp = client.post("/api/auth/login",
                       json={"employee_id": user_id, "password": "secretpass"})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['token']}"}


@pytest.fixture()
def pharmacist_hdr():
    return _login_hdr(_mk_user("pharmacist"))


@pytest.fixture()
def doctor_hdr():
    return _login_hdr(_mk_user("doctor"))


def _approve(hdr, medication="Amoxicillin", **body):
    return client.post("/api/helix/orders/approve",
                       json={"medication": medication, "dose": "500mg",
                             "route": "PO", "frequency": "TDS", **body},
                       headers=hdr)


def _helix_order_count(patient_ids):
    qs = ",".join("?" for _ in patient_ids)
    with db_session() as db:
        return db.execute(
            f"SELECT count(*) FROM helix_patient_med_orders WHERE patient_id IN ({qs})",
            tuple(patient_ids),
        ).fetchone()[0]


# ── helix approve: patient resolution ─────────────────────────────────────────
def test_ambiguous_name_fails_422_with_candidate_count(pharmacist_hdr):
    name = f"Casey Duplicate {uuid.uuid4().hex[:5]}"
    ids = [_mk_patient(name), _mk_patient(name)]
    resp = _approve(pharmacist_hdr, patient_name=name)
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert detail["error"] == "patient_unresolved"
    assert detail["candidates"] == 2
    assert _helix_order_count(ids) == 0, "ambiguous approve must not create an order"


def test_unmatched_name_fails_422_not_unknown_fallback(pharmacist_hdr):
    resp = _approve(pharmacist_hdr, patient_name=f"Nobody {uuid.uuid4().hex}")
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"]["candidates"] == 0
    assert _helix_order_count(["UNKNOWN"]) == 0


def test_unknown_explicit_patient_id_404(pharmacist_hdr):
    resp = _approve(pharmacist_hdr, patient_id="PAT-DOES-NOT-EXIST")
    assert resp.status_code == 404, resp.text


def test_explicit_patient_id_wins_over_ambiguous_name(pharmacist_hdr):
    name = f"Casey Duplicate {uuid.uuid4().hex[:5]}"
    target, _other = _mk_patient(name), _mk_patient(name)
    resp = _approve(pharmacist_hdr, patient_id=target, patient_name=name)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["order"]["patient_id"] == target
    assert body["safety"]["safe"] is True


def test_exact_name_match_is_case_insensitive_single_hit(pharmacist_hdr):
    name = f"Solo Match {uuid.uuid4().hex[:5]}"
    pid = _mk_patient(name)
    resp = _approve(pharmacist_hdr, patient_name=name.upper())
    assert resp.status_code == 200, resp.text
    assert resp.json()["order"]["patient_id"] == pid


# ── helix approve: safety gate ────────────────────────────────────────────────
def test_helix_severe_allergy_428_then_ack_succeeds(pharmacist_hdr, patient):
    add_allergy(patient, "amoxicillin", reaction="anaphylaxis", severity="severe")

    resp = _approve(pharmacist_hdr, patient_id=patient)
    assert resp.status_code == 428, resp.text
    detail = resp.json()["detail"]
    assert detail["error"] == "drug_safety_acknowledgement_required"
    assert detail["safety"]["max_severity"] == "severe"
    assert _helix_order_count([patient]) == 0, "blocked approve must not create an order"

    resp = _approve(pharmacist_hdr, patient_id=patient,
                    safety_ack={"acknowledged": True, "reason": "desensitisation protocol"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["order"]["patient_id"] == patient
    assert body["safety"]["override"]["reason"] == "desensitisation protocol"
    assert _helix_order_count([patient]) == 1
    with db_session() as db:
        log = db.execute(
            "SELECT * FROM audit_logs WHERE action_type = 'drug_safety_override' "
            "AND patient_id = ? ORDER BY timestamp DESC LIMIT 1",
            (patient,),
        ).fetchone()
    assert log is not None, "override must land on the audit chain"
    assert log["outcome"] == "partial_success"


# ── CPOE: medication orders gated ─────────────────────────────────────────────
def _cpoe_order(hdr, patient_id, *, order_type="medication",
                description="Amoxicillin 500mg PO",
                code=_RXNORM_AMOX[1], code_system=_RXNORM_AMOX[0], ack=None):
    body = {"patient_id": patient_id, "order_type": order_type,
            "description": description, "code": code, "code_system": code_system}
    if ack is not None:
        body["safety_ack"] = ack
    return client.post("/api/orders", json=body, headers=hdr)


def _cpoe_order_count(patient_id):
    with db_session() as db:
        return db.execute("SELECT count(*) FROM orders WHERE patient_id = ?",
                          (patient_id,)).fetchone()[0]


def test_cpoe_med_severe_allergy_428_then_ack_succeeds(doctor_hdr, patient):
    add_allergy(patient, "amoxicillin", reaction="anaphylaxis", severity="severe")

    resp = _cpoe_order(doctor_hdr, patient)
    assert resp.status_code == 428, resp.text
    detail = resp.json()["detail"]
    assert detail["error"] == "drug_safety_acknowledgement_required"
    assert detail["safety"]["max_severity"] == "severe"
    assert _cpoe_order_count(patient) == 0, "blocked med order must not persist"

    resp = _cpoe_order(doctor_hdr, patient,
                       ack={"acknowledged": True, "reason": "desensitisation protocol"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["order"]["patient_id"] == patient
    assert body["order"]["order_type"] == "medication"
    assert body["safety"]["override"]["reason"] == "desensitisation protocol"
    assert _cpoe_order_count(patient) == 1


def test_cpoe_med_clean_screen_returns_safety(doctor_hdr, patient):
    resp = _cpoe_order(doctor_hdr, patient)
    assert resp.status_code == 200, resp.text
    safety = resp.json()["safety"]
    assert safety["safe"] is True
    assert safety["interaction_screened"] is True


def test_cpoe_invalid_code_400_before_safety_screen(doctor_hdr, patient):
    # Terminology validation precedes the gate: a bad code is a plain 400
    # even when the drug would have raised a severe (428) alert.
    add_allergy(patient, "amoxicillin", reaction="anaphylaxis", severity="severe")
    resp = _cpoe_order(doctor_hdr, patient, code="0000-BAD")
    assert resp.status_code == 400, resp.text
    assert _cpoe_order_count(patient) == 0


def test_cpoe_non_medication_order_unaffected(doctor_hdr, patient):
    add_allergy(patient, "amoxicillin", reaction="anaphylaxis", severity="severe")
    resp = _cpoe_order(doctor_hdr, patient, order_type="lab",
                       description="Lactate", code="2524-7", code_system="LOINC")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "success"
    assert "safety" not in body, "non-medication orders are not drug-screened"
    assert _cpoe_order_count(patient) == 1
