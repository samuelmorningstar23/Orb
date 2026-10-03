"""Helix order lifecycle: what an order needs, who is told, and the way back.

Defects pinned here (gap review 2026-10-02, F062 and F066):

  * approve accepted a blank dose, route and frequency (the Sage card sent ''
    for whatever the model left out), and verify signed off a dose-less order;
  * a rejection needed no reason and nobody was told: only a verification
    reached the case room;
  * `scheduled` allowed only `dispensed`, so a stockout found at dispense was a
    dead end, and the error named the drug by its row id and pointed at a
    restock step Orb does not have;
  * a screen opened before someone moved the order on got
    "Illegal transition: reviewing -> reviewing" instead of the current order;
  * the Verify Queue printed "severe_ack_required: ... medication 1596".
"""
import uuid

import pytest
from fastapi.testclient import TestClient

from main import app
from auth import hash_password
from database import db_session
from services import helix_service
from services import pharmacy_verification_service as pharmacy
from services.clinical_records_service import add_allergy
from services.helix_db import init_helix_db

client = TestClient(app)

_MED_ID = "HXLIFEORIG"
_MED_NAME = "Zylocycle Lifecycle Fixture 250mg Capsule"
_SUB_ID = "HXLIFESUB"
_SUB_NAME = "Zylocycle Lifecycle Brand B 250mg Capsule"
_DRY_ID = "HXLIFEDRY"
_DRY_NAME = "Zylodry Lifecycle Fixture"


@pytest.fixture(scope="module", autouse=True)
def _seed_module():
    init_helix_db()
    with db_session() as db:
        for mid, name, strength in ((_MED_ID, _MED_NAME, "250mg"),
                                    (_SUB_ID, _SUB_NAME, "250mg"),
                                    (_DRY_ID, _DRY_NAME, "40 mg")):
            db.execute(
                "INSERT OR IGNORE INTO helix_medication_master (id, name, generic_name, form, strength) "
                "VALUES (?, ?, 'zylocycle', 'capsule', ?)",
                (mid, name, strength),
            )
        db.execute(
            "INSERT OR IGNORE INTO helix_inventory_lots (id, medication_id, lot_number, quantity, expiration_date) "
            "VALUES ('LOTLIFE1', ?, 'LOT-LIFE-1', 500, '2030-01-01')",
            (_MED_ID,),
        )
        # The case-room messages are sent as the 'system' user.
        db.execute(
            "INSERT OR IGNORE INTO users (id, name, role, password_hash) "
            "VALUES ('system', 'Orb System', 'admin', 'x')"
        )


def _mk_user(role):
    uid = f"ULIFE{uuid.uuid4().hex[:6].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT OR IGNORE INTO users (id, name, role, password_hash) VALUES (?,?,?,?)",
            (uid, f"{role.title()} {uid}", role, hash_password("secretpass")),
        )
    return uid


def _login_hdr(user_id):
    resp = client.post("/api/auth/login", json={"employee_id": user_id, "password": "secretpass"})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['token']}"}


def _mk_patient_with_room():
    pid = f"PATLIFE{uuid.uuid4().hex[:6].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO patients (id, name, mrn, status, bed, ward) VALUES (?, ?, ?, 'admitted', '7', 'Ward 4')",
            (pid, f"Lifecycle Case {uuid.uuid4().hex[:5]}", f"MRN-{uuid.uuid4().hex[:6]}"),
        )
        db.execute(
            "INSERT INTO channels (id, name, type, patient_id) VALUES (?, ?, 'case_room', ?)",
            (f"case-{pid}", f"Case {pid}", pid),
        )
    return pid


def _order(pid, actor, *, med=_MED_NAME, dose="250mg", walk=("reviewing",)):
    order = helix_service.create_order(
        patient_id=pid, medication_name=med, dose=dose, route="PO", frequency="TDS", prescribed_by=actor,
    )
    for target in walk:
        helix_service.transition_order_status(order_id=order["id"], target_status=target, actor_id=actor)
    return order["id"]


def _row(oid):
    with db_session() as db:
        return dict(db.execute("SELECT * FROM helix_patient_med_orders WHERE id = ?", (oid,)).fetchone())


def _room_messages(pid):
    with db_session() as db:
        return [r["content"] for r in db.execute(
            "SELECT content FROM messages WHERE channel_id = ? ORDER BY id", (f"case-{pid}",)).fetchall()]


def _transition(hdr, oid, **body):
    return client.post(f"/api/helix/orders/{oid}/transition", json=body, headers=hdr)


@pytest.fixture()
def pharmacist():
    uid = _mk_user("pharmacist")
    return uid, _login_hdr(uid)


# ── approve: an order needs a drug, a dose, a route and a frequency ──────────
@pytest.mark.parametrize("blank", ["dose", "route", "frequency", "medication"])
def test_approve_refuses_a_blank_field_with_422_and_writes_nothing(pharmacist, blank):
    _, hdr = pharmacist
    pid = _mk_patient_with_room()
    body = {"patient_id": pid, "medication": _MED_NAME, "dose": "250mg", "route": "PO", "frequency": "TDS"}
    body[blank] = "   "
    resp = client.post("/api/helix/orders/approve", json=body, headers=hdr)
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert detail["error"] == "order_incomplete"
    assert detail["missing"] == [blank]
    assert blank in detail["message"]
    with db_session() as db:
        assert db.execute("SELECT count(*) FROM helix_patient_med_orders WHERE patient_id = ?",
                          (pid,)).fetchone()[0] == 0


def test_approve_names_every_missing_field(pharmacist):
    _, hdr = pharmacist
    resp = client.post("/api/helix/orders/approve",
                       json={"patient_id": _mk_patient_with_room(), "medication": _MED_NAME},
                       headers=hdr)
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"]["message"] == (
        "Enter the dose, route and frequency before sending this order to pharmacy.")


def test_verify_refuses_an_order_with_no_dose(pharmacist):
    actor, hdr = pharmacist
    pid = _mk_patient_with_room()
    oid = _order(pid, actor, dose="")
    resp = _transition(hdr, oid, target_status="verified")
    assert resp.status_code == 422, resp.text
    assert "no dose" in resp.json()["detail"]["message"]
    assert _row(oid)["status"] == "reviewing"


# ── reject: a reason, and the prescriber is told ─────────────────────────────
def test_reject_without_a_reason_is_422(pharmacist):
    actor, hdr = pharmacist
    oid = _order(_mk_patient_with_room(), actor)
    resp = _transition(hdr, oid, target_status="rejected", pharmacist_notes="  ")
    assert resp.status_code == 422, resp.text
    assert "reason" in resp.json()["detail"]["message"]
    assert _row(oid)["status"] == "reviewing"


def test_rejection_reaches_the_case_room_with_drug_and_reason(pharmacist):
    actor, hdr = pharmacist
    pid = _mk_patient_with_room()
    oid = _order(pid, actor)
    resp = _transition(hdr, oid, target_status="rejected", pharmacist_notes="Duplicate of the charted course")
    assert resp.status_code == 200, resp.text
    messages = _room_messages(pid)
    assert any("rejected" in m and _MED_NAME in m and "Duplicate of the charted course" in m
               for m in messages), messages


def test_substitute_and_verify_messages_name_the_substitute(pharmacist):
    actor, hdr = pharmacist
    pid = _mk_patient_with_room()
    oid = _order(pid, actor)
    resp = _transition(hdr, oid, target_status="substitute_suggested",
                       substitute_medication_id=_SUB_ID, pharmacist_notes="Brand A out of stock")
    assert resp.status_code == 200, resp.text
    for target in ("reviewing", "verified"):
        assert _transition(hdr, oid, target_status=target).status_code == 200
    suggested, verified = _room_messages(pid)[-2:]
    assert _SUB_NAME in suggested and _MED_NAME in suggested and "Brand A out of stock" in suggested
    assert verified.startswith(f"SYSTEM: Pharmacy has verified the order for {_SUB_NAME}")
    assert f"substituted for {_MED_NAME}" in verified


# ── the way back from verified and scheduled ─────────────────────────────────
def test_scheduled_can_return_to_review_and_drops_its_slot(pharmacist):
    actor, hdr = pharmacist
    oid = _order(_mk_patient_with_room(), actor, walk=("reviewing", "verified"))
    assert _transition(hdr, oid, target_status="scheduled",
                       scheduled_at="2026-10-03T09:00:00+00:00").status_code == 200
    assert _row(oid)["scheduled_at"]
    resp = _transition(hdr, oid, target_status="reviewing")
    assert resp.status_code == 200, resp.text
    row = _row(oid)
    assert row["status"] == "reviewing"
    assert row["scheduled_at"] is None


def test_verified_can_be_rejected_with_a_reason(pharmacist):
    actor, hdr = pharmacist
    oid = _order(_mk_patient_with_room(), actor, walk=("reviewing", "verified"))
    resp = _transition(hdr, oid, target_status="rejected", pharmacist_notes="Prescriber cancelled")
    assert resp.status_code == 200, resp.text
    assert _row(oid)["status"] == "rejected"


def test_a_stockout_names_the_drug_and_the_two_ways_on(pharmacist):
    actor, hdr = pharmacist
    oid = _order(_mk_patient_with_room(), actor, med=_DRY_NAME, walk=("reviewing", "verified", "scheduled"))
    resp = _transition(hdr, oid, target_status="dispensed")
    assert resp.status_code == 400, resp.text
    message = resp.json()["detail"]
    assert message == f"No stock of {_DRY_NAME} 40 mg. Return to review to substitute, or reject."
    assert _DRY_ID not in message
    # And the way on it names is open.
    assert _transition(hdr, oid, target_status="reviewing").status_code == 200


# ── a stale screen gets the order as it is now ───────────────────────────────
def test_a_step_from_a_stale_screen_is_409_with_the_current_order(pharmacist):
    actor, hdr = pharmacist
    oid = _order(_mk_patient_with_room(), actor, walk=())
    assert _transition(hdr, oid, target_status="reviewing").status_code == 200
    resp = _transition(hdr, oid, target_status="reviewing")
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["error"] == "order_state_changed"
    assert detail["current_status"] == "reviewing"
    assert detail["order"]["id"] == oid and detail["order"]["status"] == "reviewing"
    assert detail["order"]["medication_name"] == _MED_NAME
    assert "Illegal" not in detail["message"] and "->" not in detail["message"]


# ── the Verify Queue reads people's words, and knows where the patient is ────
def test_verify_queue_428_message_has_no_marker_or_row_id():
    pharmacist = _mk_user("pharmacist")
    pid = _mk_patient_with_room()
    add_allergy(pid, "amoxicillin", reaction="anaphylaxis", severity="severe")
    with db_session() as db:
        med_id = db.execute(
            """INSERT INTO medications (patient_id, drug_name, dosage, route, frequency, status, prescribed_by, source)
               VALUES (?, 'Amoxicillin 500mg', 'REVIEW REQUIRED', 'REVIEW REQUIRED', 'REVIEW REQUIRED',
                       'active', ?, 'llm_extracted')""",
            (pid, pharmacist),
        ).lastrowid
    resp = client.post(f"/api/pharmacy/{med_id}/verify",
                       json={"dosage": "500mg", "route": "Oral", "frequency": "TDS"},
                       headers=_login_hdr(pharmacist))
    assert resp.status_code == 428, resp.text
    message = resp.json()["detail"]["message"]
    assert message == "Severe drug-safety alerts were found for Amoxicillin 500mg. Give a reason to verify it anyway."


def test_verify_queue_rows_carry_ward_bed_prescriber_and_charted_time():
    doctor = _mk_user("doctor")
    pid = _mk_patient_with_room()
    with db_session() as db:
        med_id = db.execute(
            """INSERT INTO medications (patient_id, drug_name, dosage, route, frequency, status, prescribed_by, source)
               VALUES (?, 'Paracetamol', 'REVIEW REQUIRED', 'REVIEW REQUIRED', 'REVIEW REQUIRED',
                       'active', ?, 'llm_extracted')""",
            (pid, doctor),
        ).lastrowid
    row = next(p for p in pharmacy.list_pending_verification() if p["id"] == med_id)
    assert row["ward"] == "Ward 4"
    assert row["bed"] == "7"
    assert row["prescriber"] == f"Doctor {doctor}"
    assert row["started_at"]
