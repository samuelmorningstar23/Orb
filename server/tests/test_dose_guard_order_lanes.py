"""F061: the dose guard on every single-order lane (lane parity).

drug_safety_service.evaluate_dose ran in the order-set lane and at the bedside,
and nowhere else: Vigil add-medication accepted paracetamol 8 g, Helix approve
and the Relay agentic confirm wrote it into pharmacy, and a pharmacist could
verify enoxaparin 400 mg with no warning. The same 8 g order is now refused the
same way on all four lanes, and only a prescriber with a real reason clears it.
"""
import hashlib
import uuid

import pytest
from fastapi.testclient import TestClient

from auth import hash_password
from database import db_session
from main import app

client = TestClient(app)

GOOD_REASON = "loading dose agreed with the consultant, weight 120 kg, LFTs normal"


def _user(role: str, pin: str | None = None) -> str:
    uid = f"DG{role[:3].upper()}{uuid.uuid4().hex[:6].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO users (id, name, role, password_hash, pin_hash) VALUES (?,?,?,?,?)",
            (uid, f"{role} {uid}", role, hash_password("secretpass"),
             hashlib.sha256(pin.encode()).hexdigest() if pin else None),
        )
    return uid


def _hdr(uid: str) -> dict:
    resp = client.post("/api/auth/login", json={"employee_id": uid, "password": "secretpass"})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['token']}"}


@pytest.fixture(autouse=True, scope="module")
def helix_tables():
    # Helix and the formulary the dose guard resolves brands through live in
    # tables the session fixture does not create.
    from services.helix_db import init_helix_db
    init_helix_db()


@pytest.fixture()
def patient():
    pid = f"PATDG{uuid.uuid4().hex[:6].upper()}"
    with db_session() as db:
        db.execute("INSERT INTO patients (id, name, mrn) VALUES (?, ?, ?)",
                   (pid, "Dose Guard Test", f"MRN-{uuid.uuid4().hex[:6]}"))
    yield pid
    with db_session() as db:
        db.execute("UPDATE patients SET status = 'discharged' WHERE id = ?", (pid,))


def _assert_dose_refusal(resp):
    """The one refusal shape every lane answers with."""
    assert resp.status_code == 428, resp.text
    detail = resp.json()["detail"]
    safety = detail["safety"]
    assert safety["blocking"] is True
    check = safety["dose_check"]
    assert check["decision"] == "block"
    assert check["override_requires_permission"] == "medication.prescribe"
    assert any("at or above" in (f["message"] or "") for f in check["findings"])
    return detail


def _medication_rows(pid: str) -> int:
    with db_session() as db:
        return db.execute("SELECT count(*) FROM medications WHERE patient_id = ?",
                          (pid,)).fetchone()[0]


# ── 1. Vigil add-medication ───────────────────────────────────────────────────
def _vigil_add(hdr, pid, ack=None, dose="8 g"):
    body = {"drug_name": "Paracetamol", "dosage": dose, "route": "Oral", "frequency": "QDS"}
    if ack is not None:
        body["safety_ack"] = ack
    return client.post(f"/api/vigil/patients/{pid}/medications", json=body, headers=hdr)


def test_vigil_add_medication_refuses_an_8g_paracetamol(patient):
    hdr = _hdr(_user("doctor"))
    detail = _assert_dose_refusal(_vigil_add(hdr, patient))
    assert detail["error"] == "drug_safety_acknowledgement_required"
    assert "dose check stopped this order" in detail["message"]
    assert _medication_rows(patient) == 0


def test_an_ordinary_dose_is_untouched(patient):
    hdr = _hdr(_user("doctor"))
    resp = _vigil_add(hdr, patient, dose="1 g")
    assert resp.status_code == 200, resp.text
    assert resp.json()["safety"]["dose_check"]["decision"] == "allow"


def test_a_junk_reason_does_not_clear_it(patient):
    hdr = _hdr(_user("doctor"))
    detail = _assert_dose_refusal(_vigil_add(hdr, patient, ack={"acknowledged": True, "reason": "ok"}))
    assert detail["safety"]["ack_refused"]
    assert _medication_rows(patient) == 0


def test_a_nurse_cannot_clear_it_with_any_reason(patient):
    hdr = _hdr(_user("nurse"))
    detail = _assert_dose_refusal(
        _vigil_add(hdr, patient, ack={"acknowledged": True, "reason": GOOD_REASON}))
    assert "Only a prescriber" in detail["safety"]["ack_refused"]
    assert _medication_rows(patient) == 0


def test_a_prescriber_clears_it_with_a_reason_and_it_is_audited(patient):
    hdr = _hdr(_user("doctor"))
    resp = _vigil_add(hdr, patient, ack={"acknowledged": True, "reason": GOOD_REASON})
    assert resp.status_code == 200, resp.text
    check = resp.json()["safety"]["dose_check"]
    assert check["override"]["reason"] == GOOD_REASON
    assert _medication_rows(patient) == 1
    with db_session() as db:
        log = db.execute(
            "SELECT outcome FROM audit_logs WHERE action_type = 'dose_guard_override' "
            "AND patient_id = ? ORDER BY id DESC LIMIT 1", (patient,)).fetchone()
    assert log is not None and log["outcome"] == "partial_success"


# ── 2. Helix approve ──────────────────────────────────────────────────────────
def test_helix_approve_refuses_it_and_a_pharmacist_cannot_type_it_through(patient):
    hdr = _hdr(_user("pharmacist"))
    body = {"patient_id": patient, "medication": "Paracetamol", "dose": "8 g",
            "route": "Oral", "frequency": "QDS"}
    _assert_dose_refusal(client.post("/api/helix/orders/approve", json=body, headers=hdr))
    detail = _assert_dose_refusal(client.post(
        "/api/helix/orders/approve",
        json={**body, "safety_ack": {"acknowledged": True, "reason": GOOD_REASON}},
        headers=hdr))
    assert "Only a prescriber" in detail["safety"]["ack_refused"]
    with db_session() as db:
        n = db.execute("SELECT count(*) FROM helix_patient_med_orders WHERE patient_id = ?",
                       (patient,)).fetchone()[0]
    assert n == 0


# ── 3. Relay agentic confirm ──────────────────────────────────────────────────
def test_relay_agentic_confirm_refuses_it(patient):
    doctor = _user("doctor", pin="4321")
    room = f"case-{patient}"
    action_id = str(uuid.uuid4())
    with db_session() as db:
        # In the formulary and in stock, so nothing but the dose can stop it.
        db.execute(
            """INSERT OR IGNORE INTO helix_medication_master (id, name, generic_name, form, strength)
               VALUES ('MED-DG-PCM', 'Paracetamol 500mg Tablet', 'Paracetamol', 'Tablet', '500mg')""")
        db.execute(
            """INSERT OR IGNORE INTO helix_inventory_lots (id, medication_id, lot_number, quantity, expiration_date)
               VALUES ('LOT-DG-PCM', 'MED-DG-PCM', 'LOTDGPCM', 50, '2029-12-31')""")
        db.execute("INSERT INTO channels (id, name, type, patient_id) VALUES (?, ?, 'case_room', ?)",
                   (room, "Dose guard room", patient))
        db.execute(
            """INSERT INTO sage_actions (id, room_id, patient_id, action_type, target, rationale, state)
               VALUES (?, ?, ?, 'prescribe', 'Paracetamol', 'fever', 'suggested')""",
            (action_id, room, patient))
    resp = client.post(
        f"/api/relay/channels/{room}/sage_actions/{action_id}/confirm_agentic",
        headers=_hdr(doctor),
        json={"clinician_pin": "4321",
              "dose_confirmation": {"dose": "8 g", "frequency": "QDS", "route": "Oral"}},
    )
    _assert_dose_refusal(resp)
    with db_session() as db:
        n = db.execute("SELECT count(*) FROM helix_patient_med_orders WHERE patient_id = ?",
                       (patient,)).fetchone()[0]
    assert n == 0


# ── 4. Pharmacist verification of an AI-extracted order ───────────────────────
def test_pharmacist_cannot_verify_a_corrected_dose_the_guard_blocks(patient):
    with db_session() as db:
        med_id = db.execute(
            """INSERT INTO medications (patient_id, drug_name, dosage, route, frequency, status, source)
               VALUES (?, 'Enoxaparin', 'REVIEW REQUIRED', 'Review', 'Review', 'active', 'llm_extracted')""",
            (patient,)).lastrowid
    hdr = _hdr(_user("pharmacist"))
    body = {"dosage": "400 mg", "route": "SC", "frequency": "OD"}
    for attempt in (body, {**body, "ack_reason": GOOD_REASON}):
        resp = client.post(f"/api/pharmacy/{med_id}/verify", json=attempt, headers=hdr)
        assert resp.status_code == 428, resp.text
        safety = resp.json()["detail"]["safety"]
        assert safety["dose_check"]["decision"] == "block"
        # In the list the verify queue already renders.
        assert any(a["type"] == "dose check" and a["severity"] == "severe" for a in safety["alerts"])
    with db_session() as db:
        row = db.execute("SELECT verified_by, dosage FROM medications WHERE id = ?",
                         (med_id,)).fetchone()
    assert row["verified_by"] is None
    assert row["dosage"] == "REVIEW REQUIRED"


def test_pharmacist_verifies_an_ordinary_corrected_dose(patient):
    with db_session() as db:
        med_id = db.execute(
            """INSERT INTO medications (patient_id, drug_name, dosage, route, frequency, status, source)
               VALUES (?, 'Enoxaparin', 'REVIEW REQUIRED', 'Review', 'Review', 'active', 'llm_extracted')""",
            (patient,)).lastrowid
    hdr = _hdr(_user("pharmacist"))
    resp = client.post(f"/api/pharmacy/{med_id}/verify",
                       json={"dosage": "40 mg", "route": "SC", "frequency": "OD"}, headers=hdr)
    assert resp.status_code == 200, resp.text
    assert resp.json()["safety"]["dose_check"]["decision"] == "allow"
