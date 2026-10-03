"""Helix re-screens the drug it is about to commit, substitute included.

Until 2026-10-01 a pharmacist could substitute a cephalosporin for a patient
with documented cephalosporin anaphylaxis, verify it and dispense it, and no
step screened anything: prescribing, eMAR and the Verify Queue all ran the
drug-safety gate and Helix was the one lane that did not. These tests pin the
gate at each step that commits a drug (substitute_suggested, verified,
dispensed), on the EFFECTIVE drug, plus the read the review modal renders.
"""
import uuid

import pytest
from fastapi.testclient import TestClient

from main import app
from database import db_session
from auth import hash_password
from services import helix_service
from services.helix_db import init_helix_db

client = TestClient(app)

_ORIG_ID = "HXSUBSAFEORIG"
_ORIG_NAME = "Zyloquin Substitution Fixture 500mg Tablet"
_SUB_ID = "HXSUBSAFECEF"
_SUB_NAME = "Zylocef Substitution Fixture 1g Injection"


@pytest.fixture(scope="module", autouse=True)
def _seed_module():
    init_helix_db()
    with db_session() as db:
        db.execute(
            "INSERT OR IGNORE INTO helix_medication_master (id, name, generic_name, form, strength) "
            "VALUES (?, ?, 'zyloquin', 'tablet', '500mg')",
            (_ORIG_ID, _ORIG_NAME),
        )
        # The brand says nothing about its molecule. The screen must still see
        # ceftriaxone through the generic.
        db.execute(
            "INSERT OR IGNORE INTO helix_medication_master (id, name, generic_name, form, strength) "
            "VALUES (?, ?, 'Ceftriaxone', 'vial', '1g')",
            (_SUB_ID, _SUB_NAME),
        )
        for lot_id, med in (("LOTSUBSAFE1", _ORIG_ID), ("LOTSUBSAFE2", _SUB_ID)):
            db.execute(
                "INSERT OR IGNORE INTO helix_inventory_lots (id, medication_id, lot_number, quantity, expiration_date) "
                "VALUES (?, ?, ?, 500, '2030-01-01')",
                (lot_id, med, lot_id),
            )


def _mk_pharmacist():
    uid = f"USUB{uuid.uuid4().hex[:6].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO users (id, name, role, password_hash) VALUES (?,?,?,?)",
            (uid, f"Pharmacist {uid}", "pharmacist", hash_password("secretpass")),
        )
    resp = client.post("/api/auth/login", json={"employee_id": uid, "password": "secretpass"})
    assert resp.status_code == 200, resp.text
    return uid, {"Authorization": f"Bearer {resp.json()['token']}"}


def _mk_patient(allergy: str | None = "Cephalosporins"):
    pid = f"PATSUB{uuid.uuid4().hex[:6].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO patients (id, name, mrn, status, bed, ward) "
            "VALUES (?, ?, ?, 'admitted', 'B-2', 'GM-2')",
            (pid, f"Substitution Case {uuid.uuid4().hex[:5]}", f"MRN-{uuid.uuid4().hex[:6]}"),
        )
        db.execute(
            "INSERT INTO admissions (patient_id, department, ward, bed, status, admitted_by) "
            "VALUES (?, 'General Medicine', 'GM-2', 'B-2', 'active', 'system')",
            (pid,),
        )
        if allergy:
            db.execute(
                "INSERT INTO patient_allergies (patient_id, substance, reaction, severity, status) "
                "VALUES (?, ?, 'anaphylaxis', 'severe', 'active')",
                (pid, allergy),
            )
    return pid


def _order(pid, actor, *, walk=("reviewing",)):
    order = helix_service.create_order(
        patient_id=pid, medication_name=_ORIG_NAME,
        dose="500mg", route="PO", frequency="BD", prescribed_by=actor,
    )
    for target in walk:
        helix_service.transition_order_status(order_id=order["id"], target_status=target, actor_id=actor)
    return order["id"]


def _row(oid):
    with db_session() as db:
        return dict(db.execute("SELECT * FROM helix_patient_med_orders WHERE id = ?", (oid,)).fetchone())


def _transition(hdr, oid, **body):
    return client.post(f"/api/helix/orders/{oid}/transition", json=body, headers=hdr)


_ACK = {"acknowledged": True, "reason": "allergy service cleared after test dose"}


def test_substituting_a_cephalosporin_for_a_cephalosporin_anaphylaxis_patient_is_428():
    actor, hdr = _mk_pharmacist()
    pid = _mk_patient()
    oid = _order(pid, actor)

    resp = _transition(hdr, oid, target_status="substitute_suggested",
                       substitute_medication_id=_SUB_ID)
    assert resp.status_code == 428, resp.text
    detail = resp.json()["detail"]
    assert detail["error"] == "drug_safety_acknowledgement_required"
    assert detail["safety"]["max_severity"] == "severe"
    row = _row(oid)
    assert row["status"] == "reviewing", "a refused substitution must not write"
    assert row["substitute_medication_id"] is None


def test_acknowledged_substitution_proceeds_and_the_override_is_audited():
    actor, hdr = _mk_pharmacist()
    pid = _mk_patient()
    oid = _order(pid, actor)

    resp = _transition(hdr, oid, target_status="substitute_suggested",
                       substitute_medication_id=_SUB_ID, safety_ack=_ACK)
    assert resp.status_code == 200, resp.text
    assert resp.json()["safety"]["override"]["reason"] == _ACK["reason"]
    assert _row(oid)["substitute_medication_id"] == _SUB_ID
    with db_session() as db:
        log = db.execute(
            "SELECT * FROM audit_logs WHERE action_type = 'drug_safety_override' "
            "AND patient_id = ? ORDER BY timestamp DESC LIMIT 1",
            (pid,),
        ).fetchone()
    assert log is not None, "the override must land on the audit chain"
    assert "helix.substitute_suggested" in log["summary_message"]


def test_verified_re_screens_the_substitute_already_on_the_order():
    """An acknowledgement at substitution is not a standing pass: the verify
    step screens the effective drug again."""
    actor, hdr = _mk_pharmacist()
    pid = _mk_patient()
    oid = _order(pid, actor)
    assert _transition(hdr, oid, target_status="substitute_suggested",
                       substitute_medication_id=_SUB_ID, safety_ack=_ACK).status_code == 200
    assert _transition(hdr, oid, target_status="reviewing").status_code == 200

    resp = _transition(hdr, oid, target_status="verified")
    assert resp.status_code == 428, resp.text
    assert _row(oid)["status"] == "reviewing"


def test_dispense_re_screens_against_an_allergy_charted_after_verification():
    actor, hdr = _mk_pharmacist()
    pid = _mk_patient(allergy=None)
    oid = _order(pid, actor)
    assert _transition(hdr, oid, target_status="substitute_suggested",
                       substitute_medication_id=_SUB_ID).status_code == 200
    for target in ("reviewing", "verified", "scheduled"):
        assert _transition(hdr, oid, target_status=target).status_code == 200, target

    with db_session() as db:
        db.execute(
            "INSERT INTO patient_allergies (patient_id, substance, reaction, severity, status) "
            "VALUES (?, 'Ceftriaxone', 'anaphylaxis', 'severe', 'active')",
            (pid,),
        )
        before = db.execute(
            "SELECT quantity FROM helix_inventory_lots WHERE id = 'LOTSUBSAFE2'").fetchone()[0]

    resp = _transition(hdr, oid, target_status="dispensed")
    assert resp.status_code == 428, resp.text
    with db_session() as db:
        after = db.execute(
            "SELECT quantity FROM helix_inventory_lots WHERE id = 'LOTSUBSAFE2'").fetchone()[0]
    assert after == before, "a refused dispense must not decrement stock"
    assert _row(oid)["status"] == "scheduled"


def test_a_clean_transition_carries_the_screen_in_its_response():
    actor, hdr = _mk_pharmacist()
    pid = _mk_patient(allergy=None)
    oid = _order(pid, actor)
    resp = _transition(hdr, oid, target_status="verified")
    assert resp.status_code == 200, resp.text
    assert resp.json()["safety"]["blocking"] is False


def test_unknown_substitute_is_refused():
    actor, hdr = _mk_pharmacist()
    pid = _mk_patient(allergy=None)
    oid = _order(pid, actor)
    resp = _transition(hdr, oid, target_status="substitute_suggested",
                       substitute_medication_id="NO-SUCH-DRUG")
    assert resp.status_code == 400, resp.text
    assert _row(oid)["status"] == "reviewing"


def test_order_safety_read_previews_the_substitute():
    """The review modal renders this before the pharmacist proposes anything."""
    actor, hdr = _mk_pharmacist()
    pid = _mk_patient()
    oid = _order(pid, actor)

    plain = client.get(f"/api/helix/orders/{oid}/safety", headers=hdr)
    assert plain.status_code == 200, plain.text
    assert plain.json()["is_substitute"] is False
    assert plain.json()["safety"]["allergy_alerts"] == []

    preview = client.get(f"/api/helix/orders/{oid}/safety",
                         params={"substitute_medication_id": _SUB_ID}, headers=hdr)
    assert preview.status_code == 200, preview.text
    body = preview.json()
    assert body["is_substitute"] is True
    assert body["drug_name"] == _SUB_NAME
    assert body["safety"]["max_severity"] == "severe"
    assert body["safety"]["allergy_alerts"], "the cephalosporin allergy must show"


def test_order_safety_read_is_404_for_an_unknown_order():
    _, hdr = _mk_pharmacist()
    resp = client.get("/api/helix/orders/no-such-order/safety", headers=hdr)
    assert resp.status_code == 404
