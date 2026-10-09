"""Outpatient charges (opd-13, W7-A, 9 October 2026).

What this file pins:

  1. FLAG OFF, NOTHING CHANGED: with the 'opd' feature off a completed visit
     is charged OPD-CONSULT (or the caller's code) whatever rules exist, and no
     visit keeps a payer snapshot.
  2. WHICH CODE: the doctor's rule beats the department's, which beats a
     hospital-wide one; a rule for one visit type beats a rule for every type;
     the caller's own code still wins; no rule is OPD-CONSULT.
  3. FOLLOW-UP: a completed visit in the same clinic inside the window makes
     the next visit OPD-FOLLOWUP; outside the window, a cancelled visit, another
     clinic's visit or (for a doctor's rule) another doctor's visit does not.
  4. UNPRICED STAYS UNPRICED: OPD-FOLLOWUP and REG-FEE are put on the price
     list unpriced and their lines carry rate_is_configured 0, never Rs 0.
  5. THE REGISTRATION FEE is raised once per patient, only with a live rule; a
     replayed registration raises nothing more.
  6. THE PAYER: recorded at registration with no admission; the visit keeps
     the tariff it was registered under and is priced on it.
  7. HOSPITAL DAY: at 02:15 IST the follow-up window and the bill's date are
     the hospital's day, not UTC's.
  8. PREPAID (ORB_OPD_CHARGE_AT=registration): one line per visit whatever
     replays; completion finds it; a cancel, LWBS included, voids it while
     pending and leaves an invoiced one for a refund; with completion timing
     LWBS raises nothing.
  9. THE RULE ROUTES: switched off with registration, billing.tariff writes,
     refusals, audit, retire.
"""
from __future__ import annotations

import json
import random
import string
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import config
from auth import create_token
from database import db_session
from main import app
from services import frontdesk_service as fd
from services import opd_charges_service as charges
from services.hospital_time import hospital_today

client = TestClient(app)

CLINIC = "Charges Test Medicine"
OTHER = "Charges Test Surgery"
TAG = uuid.uuid4().hex[:6].upper()


# ── fixtures ─────────────────────────────────────────────────────────────────

def _mk_user(role, department=None, name=None):
    uid = f"OCH{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute("INSERT INTO users (id, name, role, password_hash, department, is_active) "
                   "VALUES (?, ?, ?, 'x', ?, 1)",
                   (uid, name or f"Charges {role} {uid[-4:]}", role, department))
    return uid


def _hdr(uid, role):
    return {"Authorization": f"Bearer {create_token(uid, role)}"}


@pytest.fixture(scope="module")
def staff():
    return {
        "doc": _mk_user("doctor", CLINIC, "Dr Charges One"),
        "doc2": _mk_user("doctor", CLINIC, "Dr Charges Two"),
        "doc_other": _mk_user("doctor", OTHER, "Dr Charges Other"),
        "recep": _mk_user("receptionist"),
        "admin": _mk_user("admin"),
    }


@pytest.fixture(autouse=True)
def _clean_rules():
    """Every rule a test makes is removed after it: a hospital-wide rule left
    behind would change what every later OPD test charges."""
    with db_session() as db:
        before = {r["id"] for r in db.execute("SELECT id FROM opd_charge_rules").fetchall()}
    yield
    with db_session() as db:
        rows = db.execute("SELECT id FROM opd_charge_rules").fetchall()
        for r in rows:
            if r["id"] not in before:
                db.execute("DELETE FROM opd_charge_rules WHERE id = ?", (r["id"],))


@pytest.fixture
def opd_on(monkeypatch):
    from ws import realtime
    monkeypatch.setattr(config, "FRONTDESK_ENABLED", True)
    monkeypatch.setattr(config, "REGISTRATION_ENABLED", True)
    monkeypatch.setattr(config, "OPD_ENABLED", True)
    monkeypatch.setattr(config, "OPD_CHARGE_AT", "completion")
    monkeypatch.setattr(config, "UHID_SOURCE", "hospital")
    monkeypatch.setattr(config, "UHID_PATTERN", None)
    monkeypatch.setattr(config, "DUPLICATE_MODE", "warn")
    monkeypatch.setattr(realtime, "schedule_on_loop", lambda make_coro, what: True)
    assert config.feature_enabled("opd")
    yield


@pytest.fixture
def prepaid(opd_on, monkeypatch):
    monkeypatch.setattr(config, "OPD_CHARGE_AT", "registration")
    yield


@pytest.fixture
def opd_off(monkeypatch):
    monkeypatch.setattr(config, "FRONTDESK_ENABLED", True)
    monkeypatch.setattr(config, "REGISTRATION_ENABLED", False)
    monkeypatch.setattr(config, "OPD_ENABLED", False)
    assert not config.feature_enabled("opd")
    yield


def _outpatient():
    pid = f"OCP{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute("INSERT INTO patients (id, name, mrn, age, sex, status) "
                   "VALUES (?, ?, ?, 40, 'F', 'registered')",
                   (pid, f"Charges Patient {pid[-4:]}", f"OCM-{uuid.uuid4().hex[:8]}"))
    return pid


def _price(code, paise, name="Charges item"):
    with db_session() as db:
        db.execute(
            """INSERT OR REPLACE INTO service_items
                   (code, name, category, uom, gst_treatment, gst_rate_bp,
                    default_rate_paise, rate_is_configured, is_active)
               VALUES (?, ?, 'consultation', 'each', 'exempt', 0, ?, 1, 1)""",
            (code, name, paise))


def _code(stem):
    return f"OCH-{stem}-{TAG}"


def _rule(admin_id, **fields):
    return charges.create_rule(fields, actor={"id": admin_id, "role": "admin"})


def _visit(pid, doctor, *, dept=CLINIC, visit_date=None, key=None, visit_type="new"):
    return fd.register_opd_visit(patient_id=pid, department=dept, doctor_id=doctor,
                                 registered_by=doctor, visit_date=visit_date,
                                 idempotency_key=key, visit_type=visit_type)


def _past_visit(pid, doctor, days_ago, *, dept=CLINIC, status="completed"):
    """A visit dated `days_ago` hospital days back, in the state asked for."""
    day = (hospital_today() - timedelta(days=days_ago)).isoformat()
    visit = _visit(pid, doctor, dept=dept, visit_date=day)
    with db_session() as db:
        db.execute("UPDATE opd_visits SET status = ?, completed_at = ? WHERE id = ?",
                   (status, datetime.now(timezone.utc).isoformat(), visit["id"]))
    return visit


def _complete(visit_id, by, **kw):
    return fd.complete_opd_visit(visit_id, completed_by=by, **kw)["consultation_charge"]


def _charges_for(visit_id):
    with db_session() as db:
        return [dict(r) for r in db.execute(
            "SELECT * FROM encounter_charges WHERE source_ref = ? ORDER BY id",
            (visit_id,)).fetchall()]


def _audit(action, target_id=None, patient_id=None):
    sql = "SELECT * FROM audit_logs WHERE action_type = ?"
    params = [action]
    if target_id is not None:
        sql += " AND target_id = ?"
        params.append(str(target_id))
    if patient_id is not None:
        sql += " AND patient_id = ?"
        params.append(patient_id)
    with db_session() as db:
        return [dict(r) for r in db.execute(sql + " ORDER BY id", params).fetchall()]


def _name():
    def word():
        return "Q" + "".join(random.choice("bdfgklmnprstv") + random.choice("aeiou") for _ in range(3))
    return f"{word()} {word()}"


def _registration_body(**kw):
    body = {"name": _name(), "sex": "F", "age_years": random.randint(20, 80),
            "phone": "7" + "".join(random.choice(string.digits) for _ in range(9)),
            "uhid": f"OCH{uuid.uuid4().hex[:8].upper()}"}
    body.update(kw)
    return body


# ═════════════════════════════════════════════════════════════════════════════
# 1. Flag off
# ═════════════════════════════════════════════════════════════════════════════

def test_flag_off_a_completed_visit_is_charged_as_before_whatever_the_rules(staff, opd_off):
    code = _code("OFFDEPT")
    _price(code, 50000)
    _rule(staff["admin"], rule_kind="consultation", department=CLINIC, service_code=code)
    pid = _outpatient()
    visit = _visit(pid, staff["doc"])
    with db_session() as db:
        row = db.execute("SELECT payer_id, tariff_plan_id, registration_charge_id "
                         "FROM opd_visits WHERE id = ?", (visit["id"],)).fetchone()
    assert (row["payer_id"], row["tariff_plan_id"], row["registration_charge_id"]) == (None, None, None)
    assert "prepaid_charge" not in visit
    charge = _complete(visit["id"], staff["doc"])
    assert charge["service_code"] == fd.OPD_CONSULTATION_SERVICE_CODE
    # The caller's own code still goes through untouched.
    other = _visit(_outpatient(), staff["doc"])
    assert _complete(other["id"], staff["doc"], service_code=code)["service_code"] == code


# ═════════════════════════════════════════════════════════════════════════════
# 2. Which code
# ═════════════════════════════════════════════════════════════════════════════

def test_no_rule_is_opd_consult(staff, opd_on):
    visit = _visit(_outpatient(), staff["doc"])
    assert _complete(visit["id"], staff["doc"])["service_code"] == "OPD-CONSULT"


def test_the_doctor_beats_the_department_which_beats_the_hospital(staff, opd_on):
    hosp, dept, doc = _code("HOSP"), _code("DEPT"), _code("DOC")
    for c in (hosp, dept, doc):
        _price(c, 40000)
    _rule(staff["admin"], rule_kind="consultation", service_code=hosp)
    other_visit = _visit(_outpatient(), staff["doc_other"], dept=OTHER)
    assert _complete(other_visit["id"], staff["doc_other"])["service_code"] == hosp

    _rule(staff["admin"], rule_kind="consultation", department=CLINIC, service_code=dept)
    _rule(staff["admin"], rule_kind="consultation", doctor_id=staff["doc"], service_code=doc)
    mine = _visit(_outpatient(), staff["doc"])
    colleague = _visit(_outpatient(), staff["doc2"])
    unassigned = _visit(_outpatient(), None)
    assert _complete(mine["id"], staff["doc"])["service_code"] == doc
    assert _complete(colleague["id"], staff["doc2"])["service_code"] == dept
    assert _complete(unassigned["id"], staff["doc2"])["service_code"] == dept
    # Another clinic still falls through to the hospital-wide rule.
    later = _visit(_outpatient(), staff["doc_other"], dept=OTHER)
    assert _complete(later["id"], staff["doc_other"])["service_code"] == hosp


def test_a_rule_for_one_visit_type_beats_a_rule_for_every_type(staff, opd_on):
    general, procedure = _code("GEN"), _code("PROC")
    _price(general, 30000)
    _price(procedure, 90000)
    _rule(staff["admin"], rule_kind="consultation", department=CLINIC, service_code=general)
    _rule(staff["admin"], rule_kind="consultation", department=CLINIC, visit_type="procedure",
          service_code=procedure)
    new = _visit(_outpatient(), staff["doc"])
    proc = _visit(_outpatient(), staff["doc"], visit_type="procedure")
    assert _complete(new["id"], staff["doc"])["service_code"] == general
    assert _complete(proc["id"], staff["doc"])["service_code"] == procedure


def test_the_audit_row_of_a_completion_names_the_code_and_why(staff, opd_on):
    code = _code("AUDDEPT")
    _price(code, 30000)
    rule = _rule(staff["admin"], rule_kind="consultation", department=CLINIC, service_code=code)
    visit = _visit(_outpatient(), staff["doc"])
    seen = {}

    def on_done(db, v, facts):
        seen.update(facts)

    fd.complete_opd_visit(visit["id"], completed_by=staff["doc"], on_done=on_done)
    assert seen["charge_code"] == code
    assert seen["charge_code_basis"] == "department"
    assert seen["charge_rule_id"] == rule["id"]


# ═════════════════════════════════════════════════════════════════════════════
# 3 and 4. Follow-up, and unpriced stays unpriced
# ═════════════════════════════════════════════════════════════════════════════

def test_inside_the_window_a_visit_is_a_follow_up_and_stays_unpriced(staff, opd_on):
    rule = _rule(staff["admin"], rule_kind="follow_up", department=CLINIC,
                 follow_up_window_days=7)
    assert rule["service_code"] == "OPD-FOLLOWUP"
    with db_session() as db:
        item = db.execute("SELECT * FROM service_items WHERE code = 'OPD-FOLLOWUP'").fetchone()
    assert item is not None
    pid = _outpatient()
    _past_visit(pid, staff["doc"], 3)
    visit = _visit(pid, staff["doc2"])
    charge = _complete(visit["id"], staff["doc2"])
    assert charge["service_code"] == "OPD-FOLLOWUP"
    if not int(item["rate_is_configured"] or 0):
        # Nobody priced it: unpriced, never Rs 0.
        assert charge["rate_is_configured"] is False
        assert charge["rate_paise"] is None
        assert charge["total_display"] != "₹0.00"
        [row] = _charges_for(visit["id"])
        assert row["rate_is_configured"] == 0 and row["status"] == "pending"
        assert row["notes"] == "Rate not configured at time of consultation"


def test_outside_the_window_or_another_clinic_is_a_consultation(staff, opd_on):
    _rule(staff["admin"], rule_kind="follow_up", department=CLINIC, follow_up_window_days=7)
    late = _outpatient()
    _past_visit(late, staff["doc"], 8)
    assert _complete(_visit(late, staff["doc"])["id"], staff["doc"])["service_code"] == "OPD-CONSULT"

    cancelled = _outpatient()
    _past_visit(cancelled, staff["doc"], 2, status="cancelled")
    assert _complete(_visit(cancelled, staff["doc"])["id"],
                     staff["doc"])["service_code"] == "OPD-CONSULT"

    elsewhere = _outpatient()
    _past_visit(elsewhere, staff["doc_other"], 2, dept=OTHER)
    assert _complete(_visit(elsewhere, staff["doc"])["id"],
                     staff["doc"])["service_code"] == "OPD-CONSULT"


def test_a_doctors_follow_up_rule_counts_only_that_doctors_visits(staff, opd_on):
    code = _code("DOCFU")
    _price(code, 10000)
    _rule(staff["admin"], rule_kind="follow_up", doctor_id=staff["doc"],
          follow_up_window_days=14, service_code=code)
    pid = _outpatient()
    _past_visit(pid, staff["doc2"], 2)
    # Seen by a colleague, not this doctor: a consultation.
    assert _complete(_visit(pid, staff["doc"])["id"], staff["doc"])["service_code"] == "OPD-CONSULT"
    again = _outpatient()
    _past_visit(again, staff["doc"], 2)
    assert _complete(_visit(again, staff["doc"])["id"], staff["doc"])["service_code"] == code


def test_the_window_and_the_bill_use_the_hospitals_day_at_two_in_the_morning(
        staff, prepaid, monkeypatch):
    """02:15 IST on the 15th is 20:45 UTC on the 14th. A prior visit on the
    13th is two hospital days back, outside a one-day window: a UTC day key
    would read it as one day back and charge a follow-up."""
    from services import hospital_time
    from services.vitals_source import hl7v2

    frozen = datetime(2027, 3, 14, 20, 45, tzinfo=timezone.utc)
    hl7v2.set_default_timezone("Asia/Kolkata")
    monkeypatch.setattr(hospital_time, "_utc_now", lambda: frozen)
    monkeypatch.setattr(fd, "_now", lambda: frozen.isoformat())
    try:
        _rule(staff["admin"], rule_kind="follow_up", department=CLINIC, follow_up_window_days=1)
        pid = _outpatient()
        prior = _visit(pid, staff["doc"], visit_date="2027-03-13")
        with db_session() as db:
            db.execute("UPDATE opd_visits SET status = 'completed' WHERE id = ?", (prior["id"],))
        visit = _visit(pid, staff["doc"])
        assert visit["visit_date"] == "2027-03-15"
        assert visit["prepaid_charge"]["service_code"] == "OPD-CONSULT"
        [row] = _charges_for(visit["id"])
        assert row["service_date"] == "2027-03-15"
    finally:
        hl7v2.set_default_timezone(None)


# ═════════════════════════════════════════════════════════════════════════════
# 5 and 6. The registration fee and the payer
# ═════════════════════════════════════════════════════════════════════════════

def test_no_registration_rule_no_fee_and_the_answer_is_unchanged(staff, opd_on):
    resp = client.post("/api/registration/patients", json=_registration_body(),
                       headers=_hdr(staff["recep"], "receptionist"))
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert "registration_fee" not in body and "payer" not in body
    with db_session() as db:
        n = db.execute("SELECT COUNT(*) FROM encounter_charges WHERE patient_id = ?",
                       (body["patient"]["patient_id"],)).fetchone()[0]
    assert n == 0


def test_the_registration_fee_is_charged_once_and_unpriced_until_priced(staff, opd_on):
    rule = _rule(staff["admin"], rule_kind="registration")
    assert rule["service_code"] == "REG-FEE" and rule["scope"] == "hospital"
    headers = {**_hdr(staff["recep"], "receptionist"), "Idempotency-Key": f"fee-{uuid.uuid4()}"}
    body = _registration_body()
    first = client.post("/api/registration/patients", json=body, headers=headers)
    assert first.status_code == 201, first.text
    fee = first.json()["registration_fee"]
    assert fee["service_code"] == "REG-FEE"
    pid = first.json()["patient"]["patient_id"]
    replay = client.post("/api/registration/patients", json=body, headers=headers)
    assert replay.status_code == 200 and replay.json()["replayed"] is True
    with db_session() as db:
        rows = db.execute("SELECT * FROM encounter_charges WHERE patient_id = ? "
                          "AND service_code = 'REG-FEE'", (pid,)).fetchall()
        item = db.execute("SELECT rate_is_configured FROM service_items "
                          "WHERE code = 'REG-FEE'").fetchone()
    assert len(rows) == 1
    assert rows[0]["admission_id"] is None and rows[0]["encounter_type"] == "op"
    if not int(item["rate_is_configured"] or 0):
        assert fee["rate_is_configured"] is False and rows[0]["rate_is_configured"] == 0
    [audit] = _audit("billing_charge_posted", target_id=rows[0]["id"])
    assert json.loads(audit["metadata_json"])["raised_for"] == "registration_fee"
    [registered] = [r for r in _audit("patient_registered", patient_id=pid)
                    if not json.loads(r["metadata_json"]).get("replay")]
    assert json.loads(registered["metadata_json"])["registration_fee_charge_id"] == rows[0]["id"]
    # A visit later charges nothing more for registration.
    _complete(_visit(pid, staff["doc"])["id"], staff["doc"])
    with db_session() as db:
        again = db.execute("SELECT COUNT(*) FROM encounter_charges WHERE patient_id = ? "
                           "AND service_code = 'REG-FEE'", (pid,)).fetchone()[0]
    assert again == 1


def _insurer_with_plan(code_paise):
    """A payer on its own tariff plan, and a consultation code priced at the
    item default (Rs 300) and at `code_paise` on that plan."""
    plan_code = f"OCHPLAN{TAG}{uuid.uuid4().hex[:4].upper()}"
    payer_code = f"OCHINS{TAG}{uuid.uuid4().hex[:4].upper()}"
    code = _code(f"INS{uuid.uuid4().hex[:4].upper()}")
    _price(code, 30000)
    with db_session() as db:
        cur = db.execute("INSERT INTO tariff_plans (code, name, plan_type, is_default, is_active) "
                         "VALUES (?, 'Charges insurer plan', 'insurer', 0, 1)", (plan_code,))
        plan_id = cur.lastrowid
        item = db.execute("SELECT id FROM service_items WHERE code = ?", (code,)).fetchone()
        db.execute("INSERT INTO tariff_rates (tariff_plan_id, service_item_id, service_code, "
                   "rate_paise, effective_from, is_active) VALUES (?, ?, ?, ?, '1970-01-01', 1)",
                   (plan_id, item["id"], code, code_paise))
        db.execute("INSERT INTO payers (code, name, payer_type, tariff_plan_id, is_active) "
                   "VALUES (?, 'Charges Insurer', 'insurer', ?, 1)", (payer_code, plan_id))
    return payer_code, plan_id, code


def test_an_insured_patient_is_priced_on_the_payers_tariff(staff, opd_on):
    payer_code, plan_id, code = _insurer_with_plan(95000)
    _rule(staff["admin"], rule_kind="consultation", department=CLINIC, service_code=code)
    resp = client.post(
        "/api/registration/patients",
        json=_registration_body(payer={"payer_code": payer_code, "policy_number": "POL-123"}),
        headers=_hdr(staff["recep"], "receptionist"))
    assert resp.status_code == 201, resp.text
    payer = resp.json()["payer"]
    assert payer["payer_code"] == payer_code and payer["verified"] is False
    pid = resp.json()["patient"]["patient_id"]
    with db_session() as db:
        policy = db.execute("SELECT * FROM patient_payer_policies WHERE patient_id = ?",
                            (pid,)).fetchone()
    assert policy["admission_id"] is None and policy["status"] == "active"
    assert policy["policy_number"] == "POL-123" and policy["verified_by"] is None

    visit = _visit(pid, staff["doc"])
    with db_session() as db:
        snap = db.execute("SELECT payer_id, tariff_plan_id FROM opd_visits WHERE id = ?",
                          (visit["id"],)).fetchone()
    assert snap["tariff_plan_id"] == plan_id
    charge = _complete(visit["id"], staff["doc"])
    assert charge["rate_paise"] == 95000 and charge["rate_source"] == "tariff_rate"

    # A self-pay patient on the same clinic code pays the item default.
    cash = _visit(_outpatient(), staff["doc"])
    assert _complete(cash["id"], staff["doc"])["rate_paise"] == 30000


def test_an_unknown_payer_registers_no_one(staff, opd_on):
    body = _registration_body(payer={"payer_code": f"NOPE{TAG}"})
    resp = client.post("/api/registration/patients", json=body,
                       headers=_hdr(staff["recep"], "receptionist"))
    assert resp.status_code == 422 and resp.json()["detail"]["error"] == "bad_payer"
    with db_session() as db:
        assert db.execute("SELECT COUNT(*) FROM patients WHERE mrn = ?",
                          (body["uhid"],)).fetchone()[0] == 0


def test_the_desk_lists_the_active_payers(staff, opd_on):
    payer_code, _, _ = _insurer_with_plan(10000)
    resp = client.get("/api/registration/payers", headers=_hdr(staff["recep"], "receptionist"))
    assert resp.status_code == 200
    listed = {p["code"]: p for p in resp.json()["payers"]}
    assert listed[payer_code] == {"code": payer_code, "name": "Charges Insurer",
                                  "payer_type": "insurer"}


# ═════════════════════════════════════════════════════════════════════════════
# 8. Prepaid and cancellation
# ═════════════════════════════════════════════════════════════════════════════

def test_prepaid_one_line_per_visit_whatever_replays(staff, prepaid):
    pid = _outpatient()
    key = f"prepaid-{uuid.uuid4()}"
    first = _visit(pid, staff["doc"], key=key)
    again = _visit(pid, staff["doc"], key=key)
    assert first["id"] == again["id"]
    assert first["prepaid_charge"]["charged_at"] == "registration"
    [row] = _charges_for(first["id"])
    with db_session() as db:
        v = db.execute("SELECT consultation_charge_id, registration_charge_id FROM opd_visits "
                       "WHERE id = ?", (first["id"],)).fetchone()
    assert v["consultation_charge_id"] == v["registration_charge_id"] == row["id"]
    done = _complete(first["id"], staff["doc"])
    done_again = _complete(first["id"], staff["doc"])
    assert done["charge_id"] == done_again["charge_id"] == row["id"]
    assert len(_charges_for(first["id"])) == 1
    [audit] = _audit("billing_charge_posted", target_id=row["id"])
    assert json.loads(audit["metadata_json"])["raised_for"] == "opd_consultation_prepaid"


def test_a_cancel_voids_a_pending_prepaid_line(staff, prepaid):
    visit = _visit(_outpatient(), staff["doc"])
    [row] = _charges_for(visit["id"])
    seen = {}

    def on_done(db, v, facts):
        seen.update(facts)

    cancelled = fd.cancel_opd_visit(visit["id"], reason="Left before being seen",
                                    reason_code="left_without_being_seen",
                                    cancelled_by=staff["recep"], on_done=on_done)
    assert cancelled["prepaid_charge"] == {"charge_id": row["id"], "outcome": "voided",
                                           "invoice_id": None}
    assert seen["prepaid_charge"] == "voided"
    [after] = _charges_for(visit["id"])
    assert after["status"] == "cancelled" and after["cancelled_by"] == staff["recep"]
    assert _audit("billing_charge_cancelled", target_id=row["id"])


def test_an_invoiced_prepaid_line_is_left_for_a_refund(staff, prepaid):
    visit = _visit(_outpatient(), staff["doc"])
    [row] = _charges_for(visit["id"])
    with db_session() as db:
        db.execute("UPDATE encounter_charges SET status = 'billed', invoice_id = 999999 "
                   "WHERE id = ?", (row["id"],))
    cancelled = fd.cancel_opd_visit(visit["id"], reason="Patient asked to cancel",
                                    reason_code="patient_request", cancelled_by=staff["recep"])
    assert cancelled["prepaid_charge"]["outcome"] == "invoiced"
    assert cancelled["prepaid_charge"]["invoice_id"] == 999999
    [after] = _charges_for(visit["id"])
    assert after["status"] == "billed"


def test_with_completion_timing_a_lwbs_raises_nothing(staff, opd_on):
    visit = _visit(_outpatient(), staff["doc"])
    assert "prepaid_charge" not in visit
    cancelled = fd.cancel_opd_visit(visit["id"], reason="Left before being seen",
                                    reason_code="left_without_being_seen",
                                    cancelled_by=staff["recep"])
    assert "prepaid_charge" not in cancelled
    assert _charges_for(visit["id"]) == []


def test_the_desk_route_cancel_audit_names_the_voided_line(staff, prepaid):
    visit = _visit(_outpatient(), staff["doc"])
    [row] = _charges_for(visit["id"])
    resp = client.post(f"/api/frontdesk/opd/visits/{visit['id']}/cancel",
                       json={"reason": "Registered in error", "reason_code": "registered_in_error"},
                       headers=_hdr(staff["doc"], "doctor"))
    assert resp.status_code == 200, resp.text
    [audit] = _audit("opd_visit_cancelled", target_id=visit["id"])
    meta = json.loads(audit["metadata_json"])
    assert meta["prepaid_charge"] == "voided" and meta["prepaid_charge_id"] == row["id"]


# ═════════════════════════════════════════════════════════════════════════════
# 9. The rule routes
# ═════════════════════════════════════════════════════════════════════════════

RULE_ROUTES = [
    ("get", "/api/registration/charge-rules", None),
    ("post", "/api/registration/charge-rules", {"rule_kind": "registration"}),
    ("post", "/api/registration/charge-rules/OCR-NOPE/retire", {"reason": "No longer used"}),
    ("get", "/api/registration/payers", None),
]


@pytest.mark.parametrize("method, path, body", RULE_ROUTES)
def test_the_rule_routes_are_switched_off_with_registration(staff, opd_off, method, path, body):
    resp = getattr(client, method)(path, headers=_hdr(staff["admin"], "admin"),
                                   **({"json": body} if body is not None else {}))
    assert resp.status_code == 404 and resp.json()["detail"]["error"] == "feature_disabled"


def test_only_the_price_list_permission_writes_a_rule(staff, opd_on):
    body = {"rule_kind": "consultation", "department": CLINIC, "service_code": "OPD-CONSULT"}
    for uid, role in ((staff["recep"], "receptionist"), (staff["doc"], "doctor")):
        resp = client.post("/api/registration/charge-rules", json=body, headers=_hdr(uid, role))
        assert resp.status_code == 403, resp.text
    made = client.post("/api/registration/charge-rules", json=body,
                       headers=_hdr(staff["admin"], "admin"))
    assert made.status_code == 201, made.text
    rule = made.json()
    assert rule["department"] == CLINIC and rule["scope"] == "department"
    [audit] = _audit("opd_charge_rule_created", target_id=rule["id"])
    assert audit["event_category"] == "configuration"
    dup = client.post("/api/registration/charge-rules", json=body,
                      headers=_hdr(staff["admin"], "admin"))
    assert dup.status_code == 409 and dup.json()["detail"]["rule_id"] == rule["id"]
    listed = client.get("/api/registration/charge-rules",
                        headers=_hdr(staff["recep"], "receptionist")).json()
    assert rule["id"] in {r["id"] for r in listed["rules"]}
    assert listed["charge_at"] == "completion"


@pytest.mark.parametrize("body, code", [
    ({"rule_kind": "registration", "department": CLINIC}, "registration_rule_is_hospital_wide"),
    ({"rule_kind": "follow_up", "department": CLINIC}, "bad_window"),
    ({"rule_kind": "follow_up", "department": CLINIC, "follow_up_window_days": 400}, "bad_window"),
    ({"rule_kind": "consultation", "follow_up_window_days": 7}, "bad_window"),
    ({"rule_kind": "consultation", "department": CLINIC, "service_code": "NO-SUCH-CODE-OCH"},
     "unknown_service_code"),
    ({"rule_kind": "consultation", "department": "Charges Nowhere Ward"}, "unknown_department"),
    ({"rule_kind": "consultation", "department": CLINIC, "doctor_id": "X"}, "doctor_or_department"),
    ({"rule_kind": "consultation", "doctor_id": "OCH-NOBODY"}, "not_a_consulting_doctor"),
    ({"rule_kind": "follow_up", "visit_type": "new", "follow_up_window_days": 7},
     "follow_up_takes_no_visit_type"),
    ({"rule_kind": "dinner"}, "bad_rule_kind"),
])
def test_a_rule_that_cannot_be_right_is_refused(staff, opd_on, body, code):
    resp = client.post("/api/registration/charge-rules", json=body,
                       headers=_hdr(staff["admin"], "admin"))
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"]["error"] == code


def test_a_retired_rule_charges_nothing_and_keeps_its_history(staff, opd_on):
    code = _code("RETIRE")
    _price(code, 20000)
    rule = _rule(staff["admin"], rule_kind="consultation", department=CLINIC, service_code=code)
    short = client.post(f"/api/registration/charge-rules/{rule['id']}/retire",
                        json={"reason": "no"}, headers=_hdr(staff["admin"], "admin"))
    assert short.status_code == 422
    done = client.post(f"/api/registration/charge-rules/{rule['id']}/retire",
                       json={"reason": "Clinic code withdrawn"},
                       headers=_hdr(staff["admin"], "admin"))
    assert done.status_code == 200 and done.json()["is_active"] is False
    again = client.post(f"/api/registration/charge-rules/{rule['id']}/retire",
                        json={"reason": "Clinic code withdrawn"},
                        headers=_hdr(staff["admin"], "admin"))
    assert again.status_code == 409
    assert _complete(_visit(_outpatient(), staff["doc"])["id"],
                     staff["doc"])["service_code"] == "OPD-CONSULT"
    with db_session() as db:
        kept = db.execute("SELECT retired_reason FROM opd_charge_rules WHERE id = ?",
                          (rule["id"],)).fetchone()
    assert kept["retired_reason"] == "Clinic code withdrawn"
    assert _audit("opd_charge_rule_retired", target_id=rule["id"])
