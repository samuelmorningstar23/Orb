"""The outpatient prescription on the shared medication-order store (opd-12,
W8-C, 10 October 2026).

What this file defends:

  1. Off by default: every prescription route answers 404 while ORB_OPD is
     off, 404 feature_disabled 'med_order_store' while the store is off, and
     404 on an Orb Ward Record computer (the drug chart stays on paper); 401
     with no session.
  2. Who: writing and cancelling need opd.consult AND medication.prescribe; a
     nurse, a receptionist and the administrator are 403; another clinic's
     doctor is 404.
  3. Ruling OPD-S7 (a), applied strictly: no line until an allergy review
     lets the note be signed. A chart with allergy rows but no review is
     refused too, as is an earlier 'unable to obtain'. Nothing is screened or
     written before the refusal.
  4. The gate: a penicillin-allergic outpatient is 428 for amoxicillin until a
     prescriber acknowledges with a reason; the dose guard 428s an 8 g
     paracetamol; the outpatient interaction screen 428s aspirin on top of a
     live outpatient warfarin line and leaves out a course that has ended.
  5. What is written: one med_orders row, encounter_type 'op', source
     'opd_prescription', prescribed by the caller, no admission, the course
     end; the visit, duration, quantity and instructions on its 'created'
     event; a fail-closed med_order_created audit row naming the visit. Never
     a medications, administration, Helix or CPOE row; no vitals or
     risk_scores row.
  6. After conversion to an inpatient, the outpatient line is not on the
     inpatient chart or MAR (ruling OPD-S4 (a)); the preview lists it for
     reconciliation.
  7. Visit refusals (cancelled, finished, old, admitted), validation (422),
     replays (Idempotency-Key per prescriber), a stale screen (409).
  8. The visit's list (no stored screen, audited), cancel (reason, audited,
     says when a print listed the line), and the print (live lines only,
     the allergy review as status and time, never an allergen, date of birth
     or phone, the doctor's registration number, audited fail-closed).
  9. The lane's source never writes the ward projection, Helix or scoring
     tables (an AST guard).
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
from services.drug_chart import DRUG_CHART_OFF
from services.medorders import core, outpatient

client = TestClient(app)

CLINIC = "Opd Rx Test Medicine"
OTHER_CLINIC = "Opd Rx Test Surgery"
WARD_DEPT = "Opd Rx Test Ward"
REG_NUMBER = "KMC-77120"
DOB = "1971-05-06"
PHONE = "9876512340"

_SEEDED: list[str] = []


def _mk_user(role, department, name=None, registration_number=None):
    uid = f"ORX{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO users (id, name, role, password_hash, department, is_active, "
            "registration_number) VALUES (?, ?, ?, 'x', ?, 1, ?)",
            (uid, name or f"Opd Rx {role} {uid[-4:]}", role, department, registration_number))
    return uid


def _auth(uid, role="doctor", key=None):
    out = {"Authorization": f"Bearer {create_token(uid, role)}"}
    if key:
        out["Idempotency-Key"] = key
    return out


@pytest.fixture(scope="module", autouse=True)
def helix_tables():
    # The formulary the dose guard resolves brands through, and the Helix
    # supply table this lane must never write, live in tables the session
    # fixture does not create.
    from services.helix_db import init_helix_db
    init_helix_db()


@pytest.fixture(scope="module")
def staff():
    return {
        "doc": _mk_user("doctor", CLINIC, "Dr Meera Prescriber", REG_NUMBER),
        "doc2": _mk_user("doctor", CLINIC, "Dr Arjun Colleague"),
        "other_doc": _mk_user("doctor", OTHER_CLINIC, "Dr Other Clinic"),
        "ward_doc": _mk_user("doctor", WARD_DEPT, "Dr Ward Rx"),
        "nurse": _mk_user("nurse", CLINIC),
        "recep": _mk_user("receptionist", CLINIC),
        "admin": _mk_user("admin", None),
    }


@pytest.fixture(autouse=True)
def _discharge_seeded():
    """Close what this module admits, so no later module counts it."""
    yield
    if not _SEEDED:
        return
    with db_session() as db:
        db.executemany("UPDATE admissions SET status = 'discharged' "
                       "WHERE patient_id = ? AND status = 'active'", [(p,) for p in _SEEDED])
        db.executemany("UPDATE patients SET status = 'discharged' WHERE id = ? "
                       "AND status = 'admitted'", [(p,) for p in _SEEDED])
    _SEEDED.clear()


@pytest.fixture
def rx_on(monkeypatch):
    from ws import realtime
    monkeypatch.setattr(config, "FRONTDESK_ENABLED", True)
    monkeypatch.setattr(config, "REGISTRATION_ENABLED", True)
    monkeypatch.setattr(config, "OPD_ENABLED", True)
    monkeypatch.setattr(config, "ADT_ENABLED", False)
    monkeypatch.setattr(config, "LAB_ENABLED", False)
    monkeypatch.setattr(config, "MED_ORDER_STORE_ENABLED", True)
    monkeypatch.setattr(config, "WARD_RECORD", False, raising=False)
    monkeypatch.setattr(realtime, "schedule_on_loop", lambda make_coro, what: True)
    assert config.feature_enabled("opd") and config.feature_enabled("med_order_store")
    yield


# ── helpers ──────────────────────────────────────────────────────────────────

def _outpatient():
    pid = f"ORXP{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO patients (id, name, mrn, age, sex, status, diagnosis, date_of_birth, "
            "phone, address, created_at) VALUES (?, ?, ?, 54, 'F', 'registered', "
            "'Opd rx fixture', ?, ?, '9 Hill Road', ?)",
            (pid, f"Rx Patient {pid[-4:]}", f"UH-{uuid.uuid4().hex[:8].upper()}", DOB, PHONE,
             datetime.now(timezone.utc).isoformat()))
    _SEEDED.append(pid)
    return pid


def _stated(pid):
    with db_session() as db:
        r = db.execute("SELECT name, mrn FROM patients WHERE id = ?", (pid,)).fetchone()
    return {"name": r["name"], "date_of_birth": DOB, "uhid": r["mrn"]}


def _visit(doc, pid=None):
    pid = pid or _outpatient()
    resp = client.post("/api/frontdesk/opd/visits",
                       json={"patient_id": pid, "doctor_id": doc,
                             "chief_complaint": "Cough for a week",
                             "identity_check": _stated(pid)},
                       headers=_auth(doc))
    assert resp.status_code == 200, resp.text
    return resp.json()


def _review(vid, doc, status="none_known", substances=None, reason=None):
    body = {"status": status}
    if substances is not None:
        body["substances"] = substances
    if reason is not None:
        body["reason"] = reason
    resp = client.post(f"/api/opd/visits/{vid}/allergy-review", json=body, headers=_auth(doc))
    assert resp.status_code == 200, resp.text
    return resp.json()


def _reviewed_visit(doc, pid=None, **review):
    visit = _visit(doc, pid)
    _review(visit["id"], doc, **review)
    return visit


LINE = {"drug_name": "Paracetamol", "dose": "500 mg", "route": "Oral", "frequency": "TDS",
        "duration_days": 5, "quantity": 15, "instructions": "After food"}


def _write(vid, uid, role="doctor", key=None, **over):
    body = {**LINE, **over}
    return client.post(f"/api/opd/visits/{vid}/prescriptions", json=body,
                       headers=_auth(uid, role, key))


def _one(sql, params=()):
    with db_session() as db:
        row = db.execute(sql, params).fetchone()
    return dict(row) if row is not None else None


def _all(sql, params=()):
    with db_session() as db:
        return [dict(r) for r in db.execute(sql, params).fetchall()]


def _count(table, pid):
    return _one(f"SELECT COUNT(*) AS n FROM {table} WHERE patient_id = ?", (pid,))["n"]


def _orders(pid):
    return _all("SELECT * FROM med_orders WHERE patient_id = ? ORDER BY created_at, id", (pid,))


def _audits(action, pid):
    return _all("SELECT * FROM audit_logs WHERE action_type = ? AND patient_id = ? ORDER BY id",
                (action, pid))


def _meta(row):
    return json.loads(row["metadata_json"] or "{}")


def _never_written_elsewhere(pid):
    for table in ("medications", "medication_administrations", "helix_patient_med_orders",
                  "vitals", "risk_scores"):
        assert _count(table, pid) == 0, table
    assert _all("SELECT id FROM orders WHERE patient_id = ? AND order_type = 'medication'",
                (pid,)) == []


def _gate_screens(pid):
    return [r for r in _all("SELECT action_type, metadata_json FROM audit_logs "
                            "WHERE patient_id = ? AND event_category = 'clinical_safety'", (pid,))
            if _meta(r).get("entry_point") == outpatient.ENTRY_POINT]


ROUTES = (("GET", "/api/opd/visits/X/prescriptions"),
          ("POST", "/api/opd/visits/X/prescriptions"),
          ("POST", "/api/opd/visits/X/prescriptions/O/cancel"),
          ("GET", "/api/opd/visits/X/prescriptions/print"))


# ═════════════════════════════════════════════════════════════════════════════
# 1. Off by default
# ═════════════════════════════════════════════════════════════════════════════

def test_no_session_is_401(rx_on):
    for method, path in ROUTES:
        assert client.request(method, path, json={}).status_code == 401, (method, path)


def test_every_route_is_404_with_opd_off(staff, monkeypatch):
    monkeypatch.setattr(config, "FRONTDESK_ENABLED", True)
    monkeypatch.setattr(config, "OPD_ENABLED", False)
    monkeypatch.setattr(config, "MED_ORDER_STORE_ENABLED", True)
    for method, path in ROUTES:
        resp = client.request(method, path, json={}, headers=_auth(staff["doc"]))
        assert resp.status_code == 404, (method, path)
        assert resp.json()["detail"]["feature"] == "opd"


def test_every_route_is_404_with_the_store_off(staff, rx_on, monkeypatch):
    visit = _reviewed_visit(staff["doc"])
    monkeypatch.setattr(config, "MED_ORDER_STORE_ENABLED", False)
    for method, path in ROUTES:
        path = path.replace("/X/", f"/{visit['id']}/")
        resp = client.request(method, path, json=LINE, headers=_auth(staff["doc"]))
        assert resp.status_code == 404, (method, path)
        detail = resp.json()["detail"]
        assert (detail["error"], detail["feature"]) == ("feature_disabled", "med_order_store")
    assert _orders(visit["patient_id"]) == []
    # The rest of the clinic is untouched by the store switch.
    assert client.get(f"/api/opd/visits/{visit['id']}/consultation",
                      headers=_auth(staff["doc"])).status_code == 200


def test_every_route_is_404_on_an_orb_ward_record_computer(staff, rx_on, monkeypatch):
    visit = _reviewed_visit(staff["doc"])
    monkeypatch.setattr(config, "WARD_RECORD", True, raising=False)
    for method, path in ROUTES:
        path = path.replace("/X/", f"/{visit['id']}/")
        resp = client.request(method, path, json=LINE, headers=_auth(staff["doc"]))
        assert resp.status_code == 404, (method, path)
        assert resp.json()["detail"] == DRUG_CHART_OFF
    assert _orders(visit["patient_id"]) == []


# ═════════════════════════════════════════════════════════════════════════════
# 2. Who may prescribe
# ═════════════════════════════════════════════════════════════════════════════

def test_a_nurse_a_receptionist_and_the_administrator_cannot_prescribe(staff, rx_on):
    visit = _reviewed_visit(staff["doc"])
    for key, role in (("nurse", "nurse"), ("recep", "receptionist"), ("admin", "admin")):
        assert _write(visit["id"], staff[key], role=role).status_code == 403, role
        hdr = _auth(staff[key], role)
        assert client.get(f"/api/opd/visits/{visit['id']}/prescriptions",
                          headers=hdr).status_code == 403, role
        assert client.post(f"/api/opd/visits/{visit['id']}/prescriptions/O/cancel",
                           json={"reason": "wrong"}, headers=hdr).status_code == 403, role
    assert _orders(visit["patient_id"]) == []


def test_another_clinic_cannot_see_or_write(staff, rx_on):
    visit = _reviewed_visit(staff["doc"])
    assert _write(visit["id"], staff["other_doc"]).status_code == 404
    hdr = _auth(staff["other_doc"])
    assert client.get(f"/api/opd/visits/{visit['id']}/prescriptions",
                      headers=hdr).status_code == 404
    assert client.get(f"/api/opd/visits/{visit['id']}/prescriptions/print",
                      headers=hdr).status_code == 404
    assert _orders(visit["patient_id"]) == []


# ═════════════════════════════════════════════════════════════════════════════
# 3. Ruling OPD-S7 (a)
# ═════════════════════════════════════════════════════════════════════════════

def test_no_line_until_an_allergy_review_exists(staff, rx_on):
    visit = _visit(staff["doc"])
    pid = visit["patient_id"]
    resp = _write(visit["id"], staff["doc"])
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["error"] == "allergy_review_required"
    assert detail["allergy_review"]["can_sign"] is False
    assert _orders(pid) == [] and _gate_screens(pid) == []

    _review(visit["id"], staff["doc"])
    resp = _write(visit["id"], staff["doc"])
    assert resp.status_code == 201, resp.text


def test_allergies_charted_without_a_review_are_not_enough(staff, rx_on):
    visit = _visit(staff["doc"])
    pid = visit["patient_id"]
    with db_session() as db:
        db.execute("INSERT INTO patient_allergies (patient_id, substance, severity, status) "
                   "VALUES (?, 'sulfa', 'moderate', 'active')", (pid,))
    resp = _write(visit["id"], staff["doc"])
    assert resp.status_code == 409 and resp.json()["detail"]["error"] == "allergy_review_required"
    assert _orders(pid) == []


def test_an_earlier_unable_to_obtain_does_not_carry_to_a_new_visit(staff, rx_on):
    first = _visit(staff["doc"])
    pid = first["patient_id"]
    _review(first["id"], staff["doc"], status="unable_to_obtain",
            reason="Patient drowsy, nobody with him")
    with db_session() as db:
        db.execute("UPDATE opd_visits SET status = 'completed' WHERE id = ?", (first["id"],))
    second = _visit(staff["doc"], pid)
    resp = _write(second["id"], staff["doc"])
    assert resp.status_code == 409 and resp.json()["detail"]["error"] == "allergy_review_required"
    # Asked again on this visit, whatever the answer, the line can be written.
    _review(second["id"], staff["doc"], status="unable_to_obtain",
            reason="Still drowsy, family on the way")
    assert _write(second["id"], staff["doc"]).status_code == 201


# ═════════════════════════════════════════════════════════════════════════════
# 4. The gate and the outpatient interaction screen
# ═════════════════════════════════════════════════════════════════════════════

def test_the_allergy_interlock_428s_amoxicillin_for_a_penicillin_allergic_outpatient(staff, rx_on):
    visit = _reviewed_visit(staff["doc"], status="allergies_recorded",
                            substances=["penicillin"])
    pid = visit["patient_id"]
    amox = {"drug_name": "Amoxicillin", "dose": "500 mg", "route": "Oral", "frequency": "TDS"}
    resp = _write(visit["id"], staff["doc"], **amox)
    assert resp.status_code == 428, resp.text
    detail = resp.json()["detail"]
    assert detail["error"] == "drug_safety_acknowledgement_required"
    assert detail["safety"]["allergy_alerts"]
    assert detail["safety"]["override_requires_permission"] == "medication.prescribe"
    assert _orders(pid) == []
    blocks = [r for r in _gate_screens(pid) if r["action_type"] == "drug_safety_block"]
    assert blocks

    # A blank reason is still a block.
    resp = _write(visit["id"], staff["doc"], safety_ack={"acknowledged": True, "reason": " "},
                  **amox)
    assert resp.status_code == 428

    resp = _write(visit["id"], staff["doc"],
                  safety_ack={"acknowledged": True,
                              "reason": "Tolerated amoxicillin twice since; allergist cleared"},
                  **amox)
    assert resp.status_code == 201, resp.text
    assert resp.json()["safety"]["override"]["by"] == staff["doc"]
    (order,) = _orders(pid)
    (audit,) = _audits("med_order_created", pid)
    assert _meta(audit)["safety"]["overridden"] is True
    assert _meta(audit)["opd_visit_id"] == visit["id"]
    assert [r for r in _gate_screens(pid) if r["action_type"] == "drug_safety_override"]
    _never_written_elsewhere(pid)


def test_the_dose_guard_blocks_a_tenfold_dose(staff, rx_on):
    visit = _reviewed_visit(staff["doc"])
    resp = _write(visit["id"], staff["doc"], dose="8 g", frequency="QDS")
    assert resp.status_code == 428, resp.text
    assert resp.json()["detail"]["safety"]["dose_check"]["decision"] == "block"
    assert _orders(visit["patient_id"]) == []


def test_a_second_line_is_screened_against_the_first(staff, rx_on):
    visit = _reviewed_visit(staff["doc"])
    pid = visit["patient_id"]
    assert _write(visit["id"], staff["doc"], drug_name="Warfarin", dose="5 mg",
                  frequency="OD").status_code == 201
    aspirin = {"drug_name": "Aspirin", "dose": "75 mg", "frequency": "OD"}
    resp = _write(visit["id"], staff["doc"], **aspirin)
    assert resp.status_code == 428, resp.text
    safety = resp.json()["detail"]["safety"]
    op = [a for a in safety["interaction_alerts"] if a.get("setting") == "outpatient"]
    assert op and op[0]["severity"] == "severe" and op[0]["with"] == "Warfarin"
    assert safety["blocking"] is True and safety["max_severity"] == "severe"
    assert len(_orders(pid)) == 1

    resp = _write(visit["id"], staff["doc"],
                  safety_ack={"acknowledged": True,
                              "reason": "Cardiology advised both after valve repair"},
                  **aspirin)
    assert resp.status_code == 201, resp.text
    assert resp.json()["safety"]["outpatient_override"]["by"] == staff["doc"]
    overrides = [r for r in _gate_screens(pid) if r["action_type"] == "drug_safety_override"]
    assert any(_meta(r).get("screen") == "outpatient_interactions" for r in overrides)
    created = _audits("med_order_created", pid)
    assert _meta(created[-1])["outpatient_override"] is True


def test_a_gate_block_shows_the_outpatient_alerts_too(staff, rx_on):
    """One acknowledgement must never cover an alert the prescriber was not
    shown: the gate's 428 carries the outpatient interaction alerts."""
    visit = _reviewed_visit(staff["doc"], status="allergies_recorded", substances=["aspirin"])
    pid = visit["patient_id"]
    assert _write(visit["id"], staff["doc"], drug_name="Warfarin", dose="5 mg",
                  frequency="OD").status_code == 201
    aspirin = {"drug_name": "Aspirin", "dose": "75 mg", "frequency": "OD"}
    resp = _write(visit["id"], staff["doc"], **aspirin)
    assert resp.status_code == 428, resp.text
    safety = resp.json()["detail"]["safety"]
    assert safety["allergy_alerts"]
    assert any(a.get("setting") == "outpatient" and a["with"] == "Warfarin"
               for a in safety["interaction_alerts"])
    resp = _write(visit["id"], staff["doc"],
                  safety_ack={"acknowledged": True,
                              "reason": "Desensitised last year; anticoagulation clinic agreed"},
                  **aspirin)
    assert resp.status_code == 201, resp.text
    overrides = [_meta(r) for r in _gate_screens(pid) if r["action_type"] == "drug_safety_override"]
    assert any(m.get("screen") == "outpatient_interactions" for m in overrides)
    assert any(m.get("allergy_alerts") for m in overrides)


def test_an_outpatient_screen_that_cannot_run_never_passes_the_line(staff, rx_on, monkeypatch):
    visit = _reviewed_visit(staff["doc"])

    def broken(conn, patient_id, now):
        raise RuntimeError("the interaction table could not be read")

    monkeypatch.setattr(outpatient, "_live_outpatient_drugs", broken)
    resp = _write(visit["id"], staff["doc"])
    assert resp.status_code == 503, resp.text
    assert resp.json()["detail"]["error"] == "drug_safety_screen_unavailable"
    assert _orders(visit["patient_id"]) == []


def test_a_course_that_has_ended_is_left_out_of_the_screen(staff, rx_on):
    visit = _reviewed_visit(staff["doc"])
    pid = visit["patient_id"]
    assert _write(visit["id"], staff["doc"], drug_name="Warfarin", dose="5 mg",
                  frequency="OD", duration_days=1).status_code == 201
    past = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    with db_session() as db:
        db.execute("UPDATE med_orders SET end_at = ? WHERE patient_id = ?", (past, pid))
    alerts, _ = outpatient.outpatient_interactions(pid, "Aspirin")
    assert alerts == []
    assert _write(visit["id"], staff["doc"], drug_name="Aspirin", dose="75 mg",
                  frequency="OD").status_code == 201


def _charting_meanwhile(monkeypatch, substance):
    """An allergy charted between the screen and the write."""
    real = outpatient.outpatient_interactions

    def charting(patient_id, drug_name, **kw):
        with db_session() as db:
            db.execute("INSERT INTO patient_allergies (patient_id, substance, severity, status) "
                       "VALUES (?, ?, 'mild', 'active')", (patient_id, substance))
        return real(patient_id, drug_name, **kw)

    monkeypatch.setattr(outpatient, "outpatient_interactions", charting)


def test_an_allergy_charted_while_the_line_is_checked_makes_it_stale(staff, rx_on, monkeypatch):
    visit = _reviewed_visit(staff["doc"], status="allergies_recorded", substances=["sulfa"])
    pid = visit["patient_id"]
    _charting_meanwhile(monkeypatch, "latex")
    resp = _write(visit["id"], staff["doc"])
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["error"] == "screen_stale"
    assert _orders(pid) == []


def test_an_allergy_that_contradicts_no_known_allergies_mid_way_needs_a_new_review(
        staff, rx_on, monkeypatch):
    visit = _reviewed_visit(staff["doc"])
    _charting_meanwhile(monkeypatch, "latex")
    resp = _write(visit["id"], staff["doc"])
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["error"] == "allergy_review_required"
    assert _orders(visit["patient_id"]) == []


# ═════════════════════════════════════════════════════════════════════════════
# 5. What is written, and what never is
# ═════════════════════════════════════════════════════════════════════════════

def test_a_line_is_an_outpatient_order_and_nothing_else(staff, rx_on):
    visit = _reviewed_visit(staff["doc"])
    pid = visit["patient_id"]
    resp = _write(visit["id"], staff["doc"])
    assert resp.status_code == 201, resp.text
    body = resp.json()
    line = body["prescription"]
    assert body["replayed"] is False
    assert line["opd_visit_id"] == visit["id"] and line["status"] == "prescribed"
    assert (line["duration_days"], line["quantity"], line["instructions"]) == (5, 15, "After food")
    assert line["prescriber_name"] == "Dr Meera Prescriber"
    assert "last_safety_json" not in line and "last_safety" not in line

    (order,) = _orders(pid)
    assert order["encounter_type"] == "op" and order["source"] == "opd_prescription"
    assert order["status"] == "prescribed" and order["admission_id"] is None
    assert order["prescriber_id"] == staff["doc"] and order["entered_by_role"] == "doctor"
    assert order["legacy_medication_id"] is None and order["cpoe_order_id"] is None
    assert order["duration_text"] == "5 days" and order["notes"] == "After food"
    start, end = (datetime.fromisoformat(order[k].replace("Z", "+00:00"))
                  for k in ("start_at", "end_at"))
    assert end - start == timedelta(days=5)
    (event,) = _all("SELECT * FROM med_order_events WHERE med_order_id = ?", (order["id"],))
    assert event["kind"] == "created" and event["entry_point"] == "opd_prescription"
    assert json.loads(event["payload_json"]) == {
        "setting": "outpatient", "opd_visit_id": visit["id"], "department": CLINIC,
        "duration_days": 5, "quantity": 15, "instructions": "After food"}
    (audit,) = _audits("med_order_created", pid)
    assert audit["event_category"] == "clinical_access" and audit["target_id"] == order["id"]
    meta = _meta(audit)
    assert (meta["setting"], meta["opd_visit_id"], meta["lane"]) == ("outpatient", visit["id"],
                                                                     "opd")
    assert meta["allergy_review"] == "none_known"
    screens = _gate_screens(pid)
    assert [r["action_type"] for r in screens] == ["drug_safety_screen"]
    _never_written_elsewhere(pid)


def test_a_prn_line_needs_what_for_and_a_line_needs_dose_route_and_frequency(staff, rx_on):
    visit = _reviewed_visit(staff["doc"])
    resp = _write(visit["id"], staff["doc"], is_prn=True, frequency="SOS")
    assert resp.status_code == 422 and resp.json()["detail"]["error"] == "prn_indication_required"
    resp = _write(visit["id"], staff["doc"], dose="  ")
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"] == "dose_route_and_frequency_required"
    body = {k: v for k, v in LINE.items() if k != "route"}
    resp = client.post(f"/api/opd/visits/{visit['id']}/prescriptions", json=body,
                       headers=_auth(staff["doc"]))
    assert resp.status_code == 422
    for bad in ({"duration_days": 0}, {"duration_days": 400}, {"quantity": 0},
                {"dispense_now": True}, {"status": "draft"}):
        assert _write(visit["id"], staff["doc"], **bad).status_code == 422, bad
    resp = _write(visit["id"], staff["doc"], is_prn=True, frequency="SOS",
                  prn_indication="Fever above 38.5")
    assert resp.status_code == 201, resp.text
    assert resp.json()["prescription"]["is_prn"] is True
    assert len(_orders(visit["patient_id"])) == 1


def test_visit_state_refusals(staff, rx_on):
    doc = staff["doc"]
    for status, code in (("cancelled", "visit_cancelled"), ("completed", "visit_completed")):
        visit = _reviewed_visit(doc)
        with db_session() as db:
            db.execute("UPDATE opd_visits SET status = ? WHERE id = ?", (status, visit["id"]))
        resp = _write(visit["id"], doc)
        assert resp.status_code == 409 and resp.json()["detail"]["error"] == code
        assert _orders(visit["patient_id"]) == []

    old = _reviewed_visit(doc)
    with db_session() as db:
        db.execute("UPDATE opd_visits SET visit_date = '2026-01-02' WHERE id = ?", (old["id"],))
    resp = _write(old["id"], doc)
    assert resp.status_code == 409 and resp.json()["detail"]["error"] == "visit_not_current"

    admitted = _reviewed_visit(doc)
    with db_session() as db:
        db.execute("INSERT INTO admissions (id, patient_id, admitted_by, department, status, "
                   "created_at) VALUES (?, ?, ?, ?, 'active', ?)",
                   (f"ORXA{uuid.uuid4().hex[:8]}", admitted["patient_id"], doc, WARD_DEPT,
                    datetime.now(timezone.utc).isoformat()))
    resp = _write(admitted["id"], doc)
    assert resp.status_code == 409 and resp.json()["detail"]["error"] == "patient_admitted"
    assert _orders(admitted["patient_id"]) == []


def test_a_repeated_key_answers_the_first_line_and_another_use_is_refused(staff, rx_on):
    visit = _reviewed_visit(staff["doc"])
    pid = visit["patient_id"]
    key = uuid.uuid4().hex
    first = _write(visit["id"], staff["doc"], key=key)
    assert first.status_code == 201, first.text
    again = _write(visit["id"], staff["doc"], key=key)
    assert again.status_code == 200, again.text
    assert again.json()["replayed"] is True
    assert again.json()["prescription"]["id"] == first.json()["prescription"]["id"]
    assert len(_orders(pid)) == 1
    other = _write(visit["id"], staff["doc"], key=key, drug_name="Cetirizine", dose="10 mg",
                   frequency="OD")
    assert other.status_code == 409
    assert other.json()["detail"]["error"] == "idempotency_key_reused"
    # A colleague's same key is a different request.
    assert _write(visit["id"], staff["doc2"], key=key).status_code == 201
    assert len(_orders(pid)) == 2
    assert len(_audits("med_order_created", pid)) == 2


# ═════════════════════════════════════════════════════════════════════════════
# 6. After conversion to an inpatient (ruling OPD-S4 (a))
# ═════════════════════════════════════════════════════════════════════════════

def _admission_body(visit, doc):
    return {
        "opd_visit_id": visit["id"],
        "identity_check": _stated(visit["patient_id"]),
        "no_signed_note_reason": "Note written on paper in the clinic",
        "admission_source": "clinic", "admission_type": "inpatient",
        "department": WARD_DEPT, "ward": "RX-W1", "bed": f"B{uuid.uuid4().hex[:3]}",
        "attending_doctor_id": doc, "chief_complaint": "Cough for a week",
        "initial_diagnosis": "Possible pneumonia",
    }


def test_after_conversion_the_outpatient_line_is_not_on_the_inpatient_chart(staff, rx_on):
    doc = staff["doc"]
    visit = _reviewed_visit(doc)
    pid = visit["patient_id"]
    assert _write(visit["id"], doc, drug_name="Azithromycin", dose="500 mg",
                  frequency="OD", duration_days=3, quantity=3).status_code == 201

    preview = client.get(f"/api/admissions/admit-existing/visits/{visit['id']}",
                         headers=_auth(doc))
    assert preview.status_code == 200, preview.text
    assert [m["drug_name"] for m in preview.json()["opd_medicines"]] == ["Azithromycin"]

    resp = client.post("/api/admissions/admit-existing", json=_admission_body(visit, doc),
                       headers=_auth(doc, key=uuid.uuid4().hex))
    assert resp.status_code == 201, resp.text
    _never_written_elsewhere(pid)
    (order,) = _orders(pid)
    assert order["status"] == "prescribed" and order["admission_id"] is None
    chart = client.get(f"/api/vigil/patients/{pid}", headers=_auth(staff["ward_doc"]))
    assert chart.status_code == 200, chart.text
    assert chart.json()["medications"] == []
    # The clinic no longer writes once the patient is an inpatient.
    resp = _write(visit["id"], doc, drug_name="Cetirizine", dose="10 mg", frequency="OD")
    assert resp.status_code == 409


# ═════════════════════════════════════════════════════════════════════════════
# 7. The list, cancel and the print
# ═════════════════════════════════════════════════════════════════════════════

def test_the_visit_list_shows_every_state_names_no_allergen_and_is_audited(staff, rx_on):
    visit = _reviewed_visit(staff["doc"], status="allergies_recorded", substances=["sulfa"])
    pid = visit["patient_id"]
    _write(visit["id"], staff["doc"])
    other_visit = _reviewed_visit(staff["doc"])
    _write(other_visit["id"], staff["doc"])
    resp = client.get(f"/api/opd/visits/{visit['id']}/prescriptions", headers=_auth(staff["doc"]))
    assert resp.status_code == 200, resp.text
    lines = resp.json()["prescriptions"]
    assert [x["drug_name"] for x in lines] == ["Paracetamol"]
    assert "sulfa" not in resp.text.lower()
    (audit,) = _audits(outpatient.LIST_AUDIT_ACTION, pid)
    assert audit["event_category"] == "phi_access"
    assert _meta(audit)["order_ids"] == [lines[0]["id"]]


def test_cancel_needs_a_reason_and_says_when_a_print_listed_the_line(staff, rx_on):
    doc = staff["doc"]
    visit = _reviewed_visit(doc)
    pid = visit["patient_id"]
    line = _write(visit["id"], doc).json()["prescription"]
    url = f"/api/opd/visits/{visit['id']}/prescriptions/{line['id']}/cancel"
    resp = client.post(url, json={"reason": "x"}, headers=_auth(doc))
    assert resp.status_code == 422 and resp.json()["detail"]["error"] == "reason_required"
    assert client.get(f"/api/opd/visits/{visit['id']}/prescriptions/print",
                      headers=_auth(doc)).status_code == 200
    resp = client.post(url, json={"reason": "Wrong strength", "expected_version": 1},
                       headers=_auth(doc))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["printed_before"] is True
    assert body["prescription"]["status"] == "cancelled"
    assert body["prescription"]["cancel_reason"] == "Wrong strength"
    (order,) = _orders(pid)
    assert order["status"] == "cancelled" and order["projection_status"] is None
    (audit,) = _audits("med_order_cancelled", pid)
    assert _meta(audit)["opd_visit_id"] == visit["id"]
    assert _meta(audit)["printed_before_cancel"] is True
    # A second cancel is not a legal step.
    again = client.post(url, json={"reason": "Wrong strength"}, headers=_auth(doc))
    assert again.status_code == 409
    _never_written_elsewhere(pid)

    # Nothing printed yet: the answer says so.
    line2 = _write(visit["id"], doc, drug_name="Cetirizine", dose="10 mg",
                   frequency="OD").json()["prescription"]
    resp = client.post(f"/api/opd/visits/{visit['id']}/prescriptions/{line2['id']}/cancel",
                       json={"reason": "Not needed"}, headers=_auth(doc))
    assert resp.status_code == 200 and resp.json()["printed_before"] is False


def test_cancel_stays_inside_its_open_visit(staff, rx_on):
    doc = staff["doc"]
    visit = _reviewed_visit(doc)
    line = _write(visit["id"], doc).json()["prescription"]
    other = _reviewed_visit(doc)
    resp = client.post(f"/api/opd/visits/{other['id']}/prescriptions/{line['id']}/cancel",
                       json={"reason": "Wrong patient"}, headers=_auth(doc))
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "prescription_not_found"
    with db_session() as db:
        db.execute("UPDATE opd_visits SET status = 'completed' WHERE id = ?", (visit["id"],))
    resp = client.post(f"/api/opd/visits/{visit['id']}/prescriptions/{line['id']}/cancel",
                       json={"reason": "Changed my mind"}, headers=_auth(doc))
    assert resp.status_code == 409 and resp.json()["detail"]["error"] == "visit_completed"
    assert _orders(visit["patient_id"])[0]["status"] == "prescribed"


def test_the_print_carries_live_lines_the_review_and_the_registration_number(staff, rx_on):
    doc = staff["doc"]
    visit = _reviewed_visit(doc, status="allergies_recorded", substances=["sulfa"])
    pid = visit["patient_id"]
    url = f"/api/opd/visits/{visit['id']}/prescriptions/print"
    resp = client.get(url, headers=_auth(doc))
    assert resp.status_code == 409 and resp.json()["detail"]["error"] == "no_prescriptions"

    kept = _write(visit["id"], doc, is_prn=True, frequency="SOS",
                  prn_indication="Fever above 38.5").json()["prescription"]
    dropped = _write(visit["id"], doc, drug_name="Cetirizine", dose="10 mg",
                     frequency="OD").json()["prescription"]
    client.post(f"/api/opd/visits/{visit['id']}/prescriptions/{dropped['id']}/cancel",
                json={"reason": "Not needed"}, headers=_auth(doc))
    resp = client.get(url, headers=_auth(doc))
    assert resp.status_code == 200, resp.text
    p = resp.json()
    assert p["kind"] == "prescription"
    assert [x["drug_name"] for x in p["prescriptions"]] == ["Paracetamol"]
    assert p["prescriptions"][0]["prn_indication"] == "Fever above 38.5"
    assert p["doctors"] == [{"name": "Dr Meera Prescriber", "registration_number": REG_NUMBER}]
    assert set(p["patient"]) == {"name", "uhid", "age", "sex"}
    assert p["allergy_review"]["status"] == "allergies_recorded"
    assert set(p["allergy_review"]) == {"status", "status_label", "assessed_at"}
    assert p["diagnoses"] == []          # the note is not signed
    text = resp.text.lower()
    for never in ("sulfa", DOB, PHONE, "hill road", "date_of_birth"):
        assert never not in text, never
    (audit,) = _audits(outpatient.PRINT_AUDIT_ACTION, pid)
    assert audit["event_category"] == "phi_access"
    assert _meta(audit)["order_ids"] == [kept["id"]]


def test_the_print_refuses_when_its_audit_cannot_be_written(staff, rx_on, monkeypatch):
    from services.audit_service import AuditWriteError
    visit = _reviewed_visit(staff["doc"])
    _write(visit["id"], staff["doc"])

    def refuse(**kw):
        raise AuditWriteError("down")

    monkeypatch.setattr("routes.opd_routes.log_phi_read", refuse)
    resp = client.get(f"/api/opd/visits/{visit['id']}/prescriptions/print",
                      headers=_auth(staff["doc"]))
    assert resp.status_code == 503 and resp.json()["detail"]["error"] == "audit_unavailable"


def test_a_write_whose_audit_cannot_be_written_keeps_nothing(staff, rx_on, monkeypatch):
    from services.audit_service import AuditWriteError
    visit = _reviewed_visit(staff["doc"])
    real = core.audit_in_txn

    def refuse(conn, **kw):
        if kw.get("action") == "med_order_created":
            raise AuditWriteError("down")
        return real(conn, **kw)

    monkeypatch.setattr(core, "audit_in_txn", refuse)
    resp = _write(visit["id"], staff["doc"])
    assert resp.status_code == 503 and resp.json()["detail"]["error"] == "audit_unavailable"
    assert _orders(visit["patient_id"]) == []
    assert _all("SELECT id FROM med_order_events WHERE patient_id = ?",
                (visit["patient_id"],)) == []


# ═════════════════════════════════════════════════════════════════════════════
# 8. The lane's source
# ═════════════════════════════════════════════════════════════════════════════

_SOURCE = pathlib.Path(outpatient.__file__)


def _string_constants(tree) -> list[str]:
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                docstrings.add(id(body[0].value))
    return [n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docstrings]


def test_the_lane_writes_no_ward_row_supply_row_or_scoring_table():
    import re
    tree = ast.parse(_SOURCE.read_text())
    write = re.compile(r"\b(?:INSERT(?:\s+OR\s+\w+)?\s+INTO|REPLACE\s+INTO|UPDATE|DELETE\s+FROM)"
                       r"\s+[\"'`\[]?(\w+)", re.IGNORECASE)
    written = {m.group(1).lower() for s in _string_constants(tree) for m in write.finditer(s)}
    assert written == set(), f"outpatient.py writes SQL itself: {written}"
    banned_modules = {"services.helix_service", "services.medorders.chart_lane",
                      "services.medorders.supply", "services.medorders.cpoe",
                      "services.vigil_service", "services.nurse_shift_service",
                      "services.emar_service", "services.orders_service"}
    banned_names = {"insert_ward_row_in_txn", "link_projection_in_txn", "project_in_txn",
                    "compute_news2", "insert_order", "create_order"}
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if (node.module or "") in banned_modules:
                found.append(node.module)
            found += [a.name for a in node.names if a.name in banned_names]
        elif isinstance(node, ast.Import):
            found += [a.name for a in node.names if a.name in banned_modules]
        elif isinstance(node, ast.Attribute) and node.attr in banned_names:
            found.append(node.attr)
    assert not found, found


def test_the_store_knows_the_outpatient_source():
    assert outpatient.SOURCE in core.SOURCES
    assert outpatient.ENCOUNTER_TYPE in core.ENCOUNTER_TYPES
