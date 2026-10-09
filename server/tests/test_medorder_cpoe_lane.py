"""CPOE and order-set medicines reach the ward after a prescriber signs
(medorders-7, W7-F).

Pinned here:
  * lifecycle off: a CPOE medication order is written exactly as before (a
    code is still required, no medication order, nothing on the chart, the
    shadow action at create); test_order_sets, test_orders_service,
    test_cpoe_dose_guard and test_caller_named_patient_scope pin the rest;
  * lifecycle on: a CPOE medicine is an orders draft AND a linked draft
    medication order, written together; the drug, dose, route and frequency
    are required fields, the description must name the drug, a code is
    optional (ruling MO-R11 (a)); the gate still runs at create; nothing
    reaches the board, the eMAR or the interaction screen until signed;
  * an order set applies as N drafts, the medication items linked, none on
    the board; each signed medicine then is;
  * sign (POST /api/medorders/orders/{id}/sign) writes the ward row and
    mirrors orders.status to active with an order_events row, in the store's
    transaction; a nurse cannot sign; a mirror that finds the orders row
    changed refuses to put the drug on the ward;
  * /api/orders/{id}/transition on a linked order: cancel is handed to the
    store (reason required; a nurse may cancel a draft, gets an audited 403
    on a signed order; a prescriber gets 409 cancel_not_offered); every other
    target, 'active' and a replay included, is 409 managed_by_medication_order;
    all of it keyed on the link, so it holds with the switches off again;
  * exactly one shadow clinical.action per signed medicine (the signer as
    actor, none at draft creation); non-medication orders unchanged;
  * the orders list carries the linked order's state, the 24-hour marker
    (ruling MO-R10 (a)) and whether the drug is on the formulary;
  * only services/medorders/cpoe.py calls the orders mirror helper.
"""
from __future__ import annotations

import ast
import json
import pathlib
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import config
from auth import create_token
from database import db_session
from main import app
from services import drug_safety_service, emar_service, orders_service, shadow_service
from services import order_sets_service, terminology_service
from services.clinical_records_service import add_allergy
from services.clinical_time import format_utc
from services.medorders import core, cpoe

client = TestClient(app)
DEPT_A, DEPT_B = "CPOE Lane Medicine", "CPOE Lane Surgery"
_PATIENTS: list[str] = []


@pytest.fixture(scope="module", autouse=True)
def _world():
    terminology_service.seed_terminology()
    yield
    if not _PATIENTS:
        return
    marks = ", ".join("?" for _ in _PATIENTS)
    with db_session() as db:
        db.execute(f"DELETE FROM med_order_events WHERE patient_id IN ({marks})", _PATIENTS)
        db.execute(f"DELETE FROM med_orders WHERE patient_id IN ({marks})", _PATIENTS)
        db.execute(f"UPDATE medications SET med_order_id = NULL, status = 'discontinued' "
                   f"WHERE patient_id IN ({marks})", _PATIENTS)
        db.execute(f"UPDATE patients SET status = 'discharged' WHERE id IN ({marks})", _PATIENTS)
        db.execute(f"UPDATE admissions SET status = 'discharged' WHERE patient_id IN ({marks})",
                   _PATIENTS)


def _user(role, department=None):
    uid = f"MOCP{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute("INSERT INTO users (id, name, role, password_hash, department) "
                   "VALUES (?, ?, ?, 'x', ?)", (uid, f"CPOE {role} {uid}", role, department))
    return uid


class Who:
    def __init__(self, role, department=None):
        self.role = role
        self.id = _user(role, department)
        self.hdr = {"Authorization": f"Bearer {create_token(self.id, role)}"}


def _patient(department=DEPT_A):
    pid = f"PMOCP{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute("INSERT INTO patients (id, name, mrn, status) VALUES (?, ?, ?, 'admitted')",
                   (pid, f"CPOE {department}", f"MRN-{uuid.uuid4().hex[:8]}"))
        db.execute("INSERT OR IGNORE INTO users (id, name, role, password_hash) "
                   "VALUES ('MOCPADM', 'CPOE admitter', 'doctor', 'x')")
        db.execute("INSERT INTO admissions (id, patient_id, department, ward, bed, status, "
                   "admitted_by) VALUES (?, ?, ?, ?, 'C-3', 'active', 'MOCPADM')",
                   (f"ADM-{pid}", pid, department, department))
    _PATIENTS.append(pid)
    return pid


@pytest.fixture()
def world():
    return {
        "pa": _patient(DEPT_A), "pb": _patient(DEPT_B),
        "doctor": Who("doctor", DEPT_A), "doctor_b": Who("doctor", DEPT_B),
        "nurse": Who("nurse", DEPT_A),
    }


@pytest.fixture()
def lifecycle_on(monkeypatch):
    monkeypatch.setattr(config, "MED_ORDER_STORE_ENABLED", True)
    monkeypatch.setattr(config, "MED_ORDER_LIFECYCLE_ENABLED", True)


@pytest.fixture()
def shadow_on(monkeypatch):
    monkeypatch.setenv("ORB_SHADOW_ENABLED", "true")
    shadow_service.reset_registry()
    yield
    shadow_service.reset_registry()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _cpoe(who, pid, *, drug="Paracetamol", dose="500 mg", route="Oral", frequency="TDS",
          description=None, priority="routine", is_prn=False, prn_indication=None,
          code=None, code_system=None, ack=None, detail=True):
    body = {"patient_id": pid, "order_type": "medication",
            "description": description or f"{drug} {dose} {route} {frequency}",
            "priority": priority}
    if detail:
        body["detail"] = {"medication": {
            "drug_name": drug, "dose": dose, "route": route, "frequency": frequency,
            "is_prn": is_prn, "prn_indication": prn_indication}}
    if code:
        body.update(code=code, code_system=code_system)
    if ack is not None:
        body["safety_ack"] = ack
    return client.post("/api/orders", json=body, headers=who.hdr)


def _cpoe_ok(who, pid, **kw):
    resp = _cpoe(who, pid, **kw)
    assert resp.status_code == 200, resp.text
    return resp.json()["order"]


def _linked(cpoe_order_id):
    with db_session() as db:
        return cpoe.linked_order_in_txn(db, cpoe_order_id)


def _sign(who, med_order, ack=None):
    body = {"expected_version": med_order["version"]}
    if ack is not None:
        body["safety_ack"] = ack
    return client.post(f"/api/medorders/orders/{med_order['id']}/sign", json=body,
                       headers=who.hdr)


def _transition(who, order_id, to_status, detail=None):
    body = {"to_status": to_status}
    if detail is not None:
        body["detail"] = detail
    return client.post(f"/api/orders/{order_id}/transition", json=body, headers=who.hdr)


def _ward_rows(pid):
    with db_session() as db:
        return [dict(r) for r in db.execute(
            "SELECT * FROM medications WHERE patient_id = ? ORDER BY id", (pid,)).fetchall()]


def _orders_row(order_id):
    with db_session() as db:
        return dict(db.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone())


def _clinical_actions(pid):
    with db_session() as db:
        return [dict(r) for r in db.execute(
            "SELECT * FROM shadow_events_current WHERE kind = 'clinical.action' "
            "AND patient_id = ? ORDER BY id", (pid,)).fetchall()]


def _open_episode(pid):
    with db_session() as db:
        episode, _ = shadow_service.open_episode(pid, "ingestion", trigger="news2_threshold",
                                                 db=db)
        assert episode
        assert shadow_service.emit("alert.fired", episode, "ingestion", patient_id=pid,
                                   payload={"level": "critical", "raw_news2": 9},
                                   db=db) is not None
    return episode


def _audit_rows(action, **where):
    sql, args = "SELECT * FROM audit_logs WHERE action_type = ?", [action]
    for col, val in where.items():
        sql += f" AND {col} = ?"
        args.append(val)
    with db_session() as db:
        return [dict(r) for r in db.execute(sql + " ORDER BY rowid", args).fetchall()]


# ── Lifecycle off: exactly as before ──────────────────────────────────────────

def test_lifecycle_off_a_cpoe_medicine_is_written_as_before(world, shadow_on):
    doc, pid = world["doctor"], world["pa"]
    _open_episode(pid)
    # A medication order still needs a coded concept with the lifecycle off.
    resp = _cpoe(doc, pid, drug="Aspirin", dose="75 mg", route="oral", frequency="once daily")
    assert resp.status_code == 400, resp.text
    assert "requires a coded concept" in resp.json()["detail"]
    order = _cpoe_ok(doc, pid, drug="Aspirin", dose="75 mg", route="oral",
                     frequency="once daily", code="ORB-MED-ASPIRIN", code_system="LOCAL")
    assert order["status"] == "draft"
    assert "managed_by_medication_order" not in order
    assert _linked(order["id"]) is None
    assert _ward_rows(pid) == []
    # The shadow action is recorded at create, as before.
    actions = _clinical_actions(pid)
    assert len(actions) == 1 and actions[0]["subject_id"] == order["id"]
    # The list carries no new key for an unlinked order.
    listed = client.get(f"/api/orders/patient/{pid}", headers=doc.hdr).json()["orders"]
    assert [o for o in listed if o["id"] == order["id"]][0].get("medication_order") is None
    # The manual state machine still moves it.
    assert _transition(doc, order["id"], "active").status_code == 200


# ── Lifecycle on: create ──────────────────────────────────────────────────────

def test_a_cpoe_medicine_is_a_linked_draft_and_reaches_nothing(world, lifecycle_on, shadow_on):
    doc, pid = world["doctor"], world["pa"]
    _open_episode(pid)
    order = _cpoe_ok(doc, pid, drug="Warfarin", dose="5 mg", route="Oral",
                     frequency="once daily", priority="urgent")
    assert order["status"] == "draft" and order["code"] is None
    assert order["managed_by_medication_order"] is True
    med = _linked(order["id"])
    assert med["status"] == "draft" and med["source"] == "cpoe"
    assert (med["drug_name"], med["dose"], med["route"], med["frequency"], med["priority"]) == (
        "Warfarin", "5 mg", "Oral", "once daily", "urgent")
    assert med["entered_by"] == doc.id and med["entered_by_role"] == "doctor"
    assert med["admission_id"] == f"ADM-{pid}"
    assert order["medication_order"]["id"] == med["id"]
    # Nothing on the ward chart, so nothing on the board, the eMAR or the screen.
    assert _ward_rows(pid) == []
    screen = drug_safety_service.check_medication_safety(pid, "Aspirin")
    assert not screen["interaction_alerts"]
    # No shadow action for an unsigned draft.
    assert _clinical_actions(pid) == []
    # Its creation is on the store's ledger and audited.
    with db_session() as db:
        events = core.events_in_txn(db, med["id"])
    assert [e["kind"] for e in events] == ["created"]
    assert _audit_rows("med_order_created", target_id=med["id"])


def test_create_needs_the_medicine_as_fields(world, lifecycle_on):
    doc, pid = world["doctor"], world["pa"]
    resp = _cpoe(doc, pid, detail=False, description="Paracetamol 1 g")
    assert resp.status_code == 422 and resp.json()["detail"]["error"] == "drug_name_required"
    resp = _cpoe(doc, pid, route="")
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"] == "dose_route_and_frequency_required"
    resp = _cpoe(doc, pid, drug="Paracetamol", description="Ibuprofen 400 mg oral TDS")
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"] == "description_does_not_name_the_drug"
    with db_session() as db:
        assert db.execute("SELECT COUNT(*) FROM orders WHERE patient_id = ?",
                          (pid,)).fetchone()[0] == 0


def test_a_code_given_is_still_validated(world, lifecycle_on):
    doc, pid = world["doctor"], world["pa"]
    resp = _cpoe(doc, pid, code="NOT-A-CODE", code_system="LOCAL")
    assert resp.status_code == 400 and "Unknown LOCAL code" in resp.json()["detail"]
    assert _cpoe_ok(doc, pid, drug="Aspirin", dose="75 mg", route="oral",
                    frequency="once daily", code="ORB-MED-ASPIRIN",
                    code_system="LOCAL")["code"] == "ORB-MED-ASPIRIN"


def test_the_gate_still_runs_at_create(world, lifecycle_on):
    doc, pid = world["doctor"], world["pa"]
    add_allergy(pid, "amoxicillin", reaction="anaphylaxis", severity="severe")
    resp = _cpoe(doc, pid, drug="Amoxicillin", dose="500 mg", route="Oral", frequency="TDS")
    assert resp.status_code == 428, resp.text
    with db_session() as db:
        assert db.execute("SELECT COUNT(*) FROM med_orders WHERE patient_id = ?",
                          (pid,)).fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM orders WHERE patient_id = ?",
                          (pid,)).fetchone()[0] == 0


def test_a_cross_ward_create_is_404_and_writes_nothing(world, lifecycle_on):
    resp = _cpoe(world["doctor_b"], world["pa"])
    assert resp.status_code == 404
    with db_session() as db:
        assert db.execute("SELECT COUNT(*) FROM med_orders WHERE patient_id = ?",
                          (world["pa"],)).fetchone()[0] == 0


def test_a_nurse_can_enter_a_draft_but_not_sign_it(world, lifecycle_on):
    nurse, pid = world["nurse"], world["pa"]
    order = _cpoe_ok(nurse, pid)
    med = _linked(order["id"])
    assert med["entered_by_role"] == "nurse"
    resp = _sign(nurse, med)
    assert resp.status_code == 403
    assert _linked(order["id"])["status"] == "draft" and _ward_rows(pid) == []


def test_a_prn_order_carries_its_indication(world, lifecycle_on):
    doc, pid = world["doctor"], world["pa"]
    order = _cpoe_ok(doc, pid, drug="Ondansetron", dose="4 mg", route="IV",
                     frequency="8 hourly", is_prn=True, prn_indication="nausea")
    med = _linked(order["id"])
    assert med["is_prn"] == 1 and med["prn_indication"] == "nausea"


# ── Sign ──────────────────────────────────────────────────────────────────────

def test_sign_puts_the_drug_on_the_board_the_emar_and_the_screen(world, lifecycle_on,
                                                                 shadow_on):
    doc, nurse, pid = world["doctor"], world["nurse"], world["pa"]
    _open_episode(pid)
    order = _cpoe_ok(nurse, pid, drug="Warfarin", dose="5 mg", route="Oral",
                     frequency="once daily")
    med = _linked(order["id"])
    resp = _sign(doc, med)
    assert resp.status_code == 200, resp.text
    signed = resp.json()["order"]
    assert signed["status"] == "prescribed" and signed["prescriber_id"] == doc.id

    rows = _ward_rows(pid)
    assert len(rows) == 1
    ward = rows[0]
    assert ward["med_order_id"] == med["id"] and ward["status"] == "active"
    assert (ward["drug_name"], ward["dosage"], ward["route"], ward["frequency"]) == (
        "Warfarin", "5 mg", "Oral", "once daily")
    assert ward["prescribed_by"] == doc.id

    # The CPOE view follows, with its own event.
    row = _orders_row(order["id"])
    assert row["status"] == "active"
    last = orders_service.get_order_events(order["id"])[-1]
    assert (last["prior_status"], last["new_status"], last["actor_id"]) == (
        "draft", "active", doc.id)
    assert "Signed" in last["reason"]

    # The interaction screen now sees it.
    screen = drug_safety_service.check_medication_safety(pid, "Aspirin")
    assert screen["interaction_alerts"], screen

    # The nurse board lists it for the ward.
    from services import nurse_shift_service
    board = nurse_shift_service.build_board(DEPT_A)
    assert "Warfarin" in json.dumps(board, default=str)

    # And the eMAR gives it.
    given = emar_service.record_administration(ward["id"], nurse.id, status="given",
                                               dose_given="5 mg", route_given="Oral")
    assert given

    # Exactly one shadow action, at sign, with the signer as the actor.
    actions = _clinical_actions(pid)
    assert len(actions) == 1
    assert actions[0]["subject_id"] == order["id"] and actions[0]["actor_user_id"] == doc.id
    assert json.loads(actions[0]["payload_json"])["action_class"] == "add_medication"

    # The store audit names the mirror.
    audit = _audit_rows("med_order_signed", target_id=med["id"])[-1]
    meta = json.loads(audit["metadata_json"]) if audit.get("metadata_json") else {}
    assert meta.get("cpoe_mirror", {}).get("outcome") == "mirrored"


def test_a_signed_order_is_listed_with_its_state(world, lifecycle_on):
    doc, pid = world["doctor"], world["pa"]
    order = _cpoe_ok(doc, pid)
    med = _linked(order["id"])
    assert _sign(doc, med).status_code == 200
    listed = client.get(f"/api/orders/patient/{pid}", headers=doc.hdr).json()["orders"]
    mine = [o for o in listed if o["id"] == order["id"]][0]
    assert mine["status"] == "active" and mine["managed_by_medication_order"] is True
    summary = mine["medication_order"]
    assert summary["status"] == "prescribed" and summary["version"] == 2
    assert summary["prescriber_id"] == doc.id and summary["prescriber_name"]
    assert summary["waiting_over_24h"] is False
    one = client.get(f"/api/orders/{order['id']}", headers=doc.hdr).json()["order"]
    assert one["medication_order"]["id"] == med["id"]


def test_a_sign_finding_the_orders_row_changed_puts_nothing_on_the_ward(world, lifecycle_on):
    doc, pid = world["doctor"], world["pa"]
    order = _cpoe_ok(doc, pid)
    med = _linked(order["id"])
    with db_session() as db:   # a hand edit no route can make
        db.execute("UPDATE orders SET status = 'cancelled' WHERE id = ?", (order["id"],))
    resp = _sign(doc, med)
    assert resp.status_code == 409 and resp.json()["detail"]["error"] == "cpoe_order_changed"
    assert _linked(order["id"])["status"] == "draft"
    assert _ward_rows(pid) == []


def test_sign_screens_again_with_the_signers_role(world, lifecycle_on):
    doc, nurse, pid = world["doctor"], world["nurse"], world["pa"]
    order = _cpoe_ok(nurse, pid, drug="Ceftriaxone", dose="1 g", route="IV",
                     frequency="once daily")
    add_allergy(pid, "ceftriaxone", reaction="anaphylaxis", severity="severe")
    med = _linked(order["id"])
    resp = _sign(doc, med)
    assert resp.status_code == 428, resp.text
    assert _ward_rows(pid) == [] and _orders_row(order["id"])["status"] == "draft"
    resp = _sign(doc, med, ack={"acknowledged": True,
                                "reason": "Discussed with microbiology, desensitised"})
    assert resp.status_code == 200, resp.text
    assert _orders_row(order["id"])["status"] == "active"


# ── Order sets ────────────────────────────────────────────────────────────────

def test_an_order_set_applies_as_drafts_none_on_the_board(world, lifecycle_on, shadow_on):
    doc, pid = world["doctor"], world["pa"]
    _open_episode(pid)
    definition = order_sets_service.get_set("acs_initial")
    resp = client.post("/api/orders/sets/acs_initial/apply", json={"patient_id": pid},
                       headers=doc.hdr)
    assert resp.status_code == 200, resp.text
    created = resp.json()["orders"]
    assert len(created) == len(definition["items"])
    assert {o["status"] for o in created} == {"draft"}
    medication = [o for o in created if o["order_type"] == "medication"]
    others = [o for o in created if o["order_type"] != "medication"]
    assert medication and others
    for o in medication:
        med = _linked(o["id"])
        assert med["status"] == "draft" and med["source"] == "order_set"
        assert med["order_set_application_id"] == resp.json()["application_id"]
        assert med["dose"] and med["route"] and med["frequency"]
        assert o["managed_by_medication_order"] is True
    for o in others:
        assert _linked(o["id"]) is None and "managed_by_medication_order" not in o
    assert _ward_rows(pid) == []
    # A shadow action for every non-medication item now, none for a medicine.
    actions = _clinical_actions(pid)
    assert {a["subject_id"] for a in actions} == {o["id"] for o in others}

    # Sign the aspirin: it alone reaches the ward, and is counted once.
    aspirin = [o for o in medication if "Aspirin" in o["description"]][0]
    assert _sign(doc, _linked(aspirin["id"])).status_code == 200
    rows = _ward_rows(pid)
    assert [r["drug_name"] for r in rows] == ["Aspirin"] and rows[0]["status"] == "active"
    assert _orders_row(aspirin["id"])["status"] == "active"
    signed_actions = [a for a in _clinical_actions(pid) if a["subject_id"] == aspirin["id"]]
    assert len(signed_actions) == 1 and signed_actions[0]["actor_user_id"] == doc.id


def test_lifecycle_off_an_order_set_links_nothing(world):
    doc, pid = world["doctor"], world["pa"]
    result = order_sets_service.apply_set(pid, "acs_initial", actor_id=doc.id,
                                          actor_role="doctor")
    for o in result["orders"]:
        assert _linked(o["id"]) is None and "managed_by_medication_order" not in o
    assert _ward_rows(pid) == []


# ── /api/orders transitions on a linked order ─────────────────────────────────

def test_every_manual_move_of_a_linked_order_is_409(world, lifecycle_on):
    doc, pid = world["doctor"], world["pa"]
    order = _cpoe_ok(doc, pid)
    med = _linked(order["id"])
    for target in ("active", "draft", "in_progress", "completed"):
        resp = _transition(doc, order["id"], target)
        assert resp.status_code == 409, (target, resp.text)
        detail = resp.json()["detail"]
        assert detail["error"] == "managed_by_medication_order"
        assert detail["med_order_id"] == med["id"]
    assert _sign(doc, med).status_code == 200
    for target in ("active", "completed", "in_progress"):
        resp = _transition(doc, order["id"], target)
        assert resp.status_code == 409, (target, resp.text)
    assert _orders_row(order["id"])["status"] == "active"
    # An unknown status is still the plain 400.
    assert _transition(doc, order["id"], "bogus").status_code == 400


def test_a_nurse_cancels_a_draft_with_a_reason_through_the_store(world, lifecycle_on):
    doc, nurse, pid = world["doctor"], world["nurse"], world["pa"]
    order = _cpoe_ok(doc, pid)
    resp = _transition(nurse, order["id"], "cancelled")
    assert resp.status_code == 422 and resp.json()["detail"]["error"] == "reason_required"
    assert _linked(order["id"])["status"] == "draft"
    resp = _transition(nurse, order["id"], "cancelled", "Entered on the wrong patient")
    assert resp.status_code == 200, resp.text
    assert resp.json()["order"]["status"] == "cancelled"
    med = _linked(order["id"])
    assert med["status"] == "cancelled" and med["status_reason"] == "Entered on the wrong patient"
    last = orders_service.get_order_events(order["id"])[-1]
    assert (last["prior_status"], last["new_status"], last["actor_id"]) == (
        "draft", "cancelled", nurse.id)
    assert "Entered on the wrong patient" in last["reason"]
    # A second cancel writes nothing and answers as it stands (a retried tap).
    with db_session() as db:
        events_before = len(core.events_in_txn(db, med["id"]))
    again = _transition(nurse, order["id"], "cancelled", "again")
    assert again.status_code == 200 and again.json()["order"]["status"] == "cancelled"
    with db_session() as db:
        assert len(core.events_in_txn(db, med["id"])) == events_before
    assert orders_service.get_order_events(order["id"])[-1]["id"] == last["id"]


def test_a_signed_linked_order_is_not_cancelled_from_orders(world, lifecycle_on):
    doc, nurse, pid = world["doctor"], world["nurse"], world["pa"]
    order = _cpoe_ok(doc, pid)
    med = _linked(order["id"])
    assert _sign(doc, med).status_code == 200
    resp = _transition(nurse, order["id"], "cancelled", "Not needed")
    assert resp.status_code == 403, resp.text
    assert errorish(resp) == "prescriber_required"
    assert _audit_rows("med_order_step_refused", actor_user_id=nurse.id, target_id=med["id"])
    resp = _transition(doc, order["id"], "cancelled", "Not needed")
    assert resp.status_code == 409 and resp.json()["detail"]["error"] == "cancel_not_offered"
    assert _orders_row(order["id"])["status"] == "active"
    assert _ward_rows(pid)[0]["status"] == "active"


def errorish(resp):
    body = resp.json().get("detail")
    return body.get("error") if isinstance(body, dict) else None


def test_a_cross_ward_transition_is_404(world, lifecycle_on):
    order = _cpoe_ok(world["doctor"], world["pa"])
    resp = _transition(world["doctor_b"], order["id"], "cancelled", "x")
    assert resp.status_code == 404
    assert _linked(order["id"])["status"] == "draft"


def test_the_link_decides_after_the_switches_go_off(world, monkeypatch):
    """Ruling MO-R9 (a): a linked order stays the store's when the lifecycle
    is switched off; it can be cancelled from Orders and nothing else."""
    doc, pid = world["doctor"], world["pa"]
    monkeypatch.setattr(config, "MED_ORDER_STORE_ENABLED", True)
    monkeypatch.setattr(config, "MED_ORDER_LIFECYCLE_ENABLED", True)
    order = _cpoe_ok(doc, pid)
    keep = _cpoe_ok(doc, pid, drug="Pantoprazole", dose="40 mg", route="Oral",
                    frequency="once daily")
    monkeypatch.setattr(config, "MED_ORDER_LIFECYCLE_ENABLED", False)
    monkeypatch.setattr(config, "MED_ORDER_STORE_ENABLED", False)
    assert _transition(doc, order["id"], "active").status_code == 409
    resp = _transition(doc, order["id"], "cancelled", "Course changed")
    assert resp.status_code == 200, resp.text
    assert _linked(order["id"])["status"] == "cancelled"
    listed = client.get(f"/api/orders/patient/{pid}", headers=doc.hdr).json()["orders"]
    assert [o for o in listed if o["id"] == keep["id"]][0]["medication_order"]["status"] == "draft"


def test_the_service_refuses_a_linked_order_too(world, lifecycle_on):
    order = _cpoe_ok(world["doctor"], world["pa"])
    with pytest.raises(orders_service.OrderManagedByMedicationOrder) as caught:
        orders_service.transition_order(order["id"], "cancelled", actor_id=world["doctor"].id)
    assert caught.value.med_order_id == _linked(order["id"])["id"]


def test_non_medication_orders_are_unchanged(world, lifecycle_on, shadow_on):
    doc, pid = world["doctor"], world["pa"]
    _open_episode(pid)
    resp = client.post("/api/orders", json={"patient_id": pid, "order_type": "nursing",
                                            "description": "Hourly observations"},
                       headers=doc.hdr)
    assert resp.status_code == 200, resp.text
    order = resp.json()["order"]
    assert "managed_by_medication_order" not in order and _linked(order["id"]) is None
    assert len(_clinical_actions(pid)) == 1
    assert _transition(doc, order["id"], "active").status_code == 200
    assert _transition(doc, order["id"], "cancelled").status_code == 200


# ── The list: MO-R10 and MO-R11 ───────────────────────────────────────────────

def test_a_draft_waiting_over_a_day_is_marked(world, lifecycle_on):
    doc, pid = world["doctor"], world["pa"]
    order = _cpoe_ok(doc, pid)
    med = _linked(order["id"])
    old = format_utc(datetime.now(timezone.utc) - timedelta(hours=30))
    with db_session() as db:
        db.execute("UPDATE med_orders SET created_at = ? WHERE id = ?", (old, med["id"]))
    listed = client.get(f"/api/orders/patient/{pid}", headers=doc.hdr).json()["orders"]
    mine = [o for o in listed if o["id"] == order["id"]][0]
    assert mine["medication_order"]["waiting_over_24h"] is True
    # Never cancelled by the system.
    assert _linked(order["id"])["status"] == "draft"


def test_the_list_says_whether_the_drug_is_on_the_formulary(world, lifecycle_on):
    doc, pid = world["doctor"], world["pa"]
    name = f"Formulary Test {uuid.uuid4().hex[:6]}"
    with db_session() as db:
        db.execute("INSERT INTO helix_medication_master (id, name, generic_name) "
                   "VALUES (?, ?, ?)", (f"HMM-{uuid.uuid4().hex[:8]}", f"{name} 10 mg", name))
    on = _cpoe_ok(doc, pid, drug=name, dose="10 mg", route="Oral", frequency="once daily")
    off = _cpoe_ok(doc, pid, drug="Zzzunlistedmab", dose="1 mg", route="IV",
                   frequency="once")
    listed = {o["id"]: o for o in client.get(f"/api/orders/patient/{pid}",
                                             headers=doc.hdr).json()["orders"]}
    assert listed[on["id"]]["medication_order"]["on_formulary"] is True
    assert listed[off["id"]]["medication_order"]["on_formulary"] is False


# ── Structure ─────────────────────────────────────────────────────────────────

def test_only_the_cpoe_lane_calls_the_orders_mirror():
    root = pathlib.Path(__file__).resolve().parents[1]
    allowed = {root / "services" / "orders_service.py",
               root / "services" / "medorders" / "cpoe.py"}
    offenders = []
    for path in root.rglob("*.py"):
        if ".venv" in path.parts or "tests" in path.parts or path in allowed:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            name = (node.attr if isinstance(node, ast.Attribute)
                    else node.id if isinstance(node, ast.Name) else None)
            if name == "mirror_medication_status_in_conn":
                offenders.append(f"{path.relative_to(root)}:{node.lineno}")
    assert offenders == []


def test_the_mirror_map_fits_the_orders_check():
    for status in core.STATUSES:
        assert core.cpoe_status_for(status) in orders_service.LEGAL_TRANSITIONS
