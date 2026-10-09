"""A registered outpatient becomes an inpatient (opd-17a, W7-B, 9 October 2026).

What this file defends:

  1. OFF BY DEFAULT. With ORB_OPD off both routes answer 404; with no session
     401. The admission form and its index refusal are as before.
  2. THE SAME RECORD. The patient keeps their id and hospital number, no
     second patient row exists, and name, age, sex and date of birth are
     never written. One portal login is created only when the patient has
     none (never a second); one case room for life (an existing one is reused
     with a 'new admission' line); the profile is created, never blanked;
     admissions.opd_visit_id and opd_visits.admission_id link the two.
  3. ONE TRANSACTION. The visit is finished as 'admitted' and its
     consultation charge raised (encounter_type 'op', no admission, so the
     stay's discharge advisory counts it) with the admission or not at all;
     an unwritable conversion record leaves nothing behind (503).
  4. RULINGS BUILT STRICTEST. OPD-S1 (a): no identity check or a mismatch is
     refused, audited and writes nothing. OPD-S3 (a): nothing reaches vitals
     or risk_scores and the body refuses observations. OPD-S4 (a): no
     outpatient prescription reaches medications or med_orders; the form
     lists them for reference. OPD-N8: a signed note or a reason.
  5. ACCESS. Before: the clinic reads the outpatient record, the ward cannot
     open the chart. After: the ward can, the outpatient arm is closed.
  6. REFUSALS. A discharged patient (readmission is opd-17c), an admitted
     patient, a finished, cancelled or earlier visit, another clinic's visit
     (404), a nurse or the administrator (403), a missing Idempotency-Key.
  7. REPLAYS AND RACES. The same key returns the first admission (200, no
     password, no second charge); the key on another visit is 409; two
     conversions at once give one admission and one refusal.
  8. ORB_ADT ON. The admission is placed through the bed master's writer
     (an 'admission' movement and its patient_movement record).
"""
from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import config
from auth import create_token
from database import db_session
from main import app
from services import admission_service as adm
from services.audit_service import AuditWriteError

client = TestClient(app)

CLINIC = "Conversion Test Medicine"
WARD_DEPT = "Conversion Test Surgery"
OTHER_CLINIC = "Conversion Test Dermatology"

_SEEDED: list[str] = []


def _mk_user(role, department, name=None):
    uid = f"CVU{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO users (id, name, role, password_hash, department, is_active) "
            "VALUES (?, ?, ?, 'x', ?, 1)",
            (uid, name or f"Conversion {role} {uid[-4:]}", role, department))
    return uid


def _auth(uid, role="doctor", key=None):
    out = {"Authorization": f"Bearer {create_token(uid, role)}"}
    if key:
        out["Idempotency-Key"] = key
    return out


@pytest.fixture(scope="module")
def staff():
    return {
        "doc": _mk_user("doctor", CLINIC, "Dr Ira Conversion"),
        "ward_doc": _mk_user("doctor", WARD_DEPT, "Dr Ward Conversion"),
        "other_doc": _mk_user("doctor", OTHER_CLINIC, "Dr Other Conversion"),
        "nurse": _mk_user("nurse", CLINIC),
        "admin": _mk_user("admin", None),
    }


@pytest.fixture(autouse=True)
def _discharge_seeded():
    """Close what this module admits, so no later module counts it."""
    yield
    if not _SEEDED:
        return
    with db_session() as db:
        db.executemany("UPDATE patients SET status = 'discharged' WHERE id = ? "
                       "AND status = 'admitted'", [(p,) for p in _SEEDED])
        db.executemany("UPDATE admissions SET status = 'discharged' "
                       "WHERE patient_id = ? AND status = 'active'", [(p,) for p in _SEEDED])
    _SEEDED.clear()


@pytest.fixture
def opd_on(monkeypatch):
    from ws import realtime
    monkeypatch.setattr(config, "FRONTDESK_ENABLED", True)
    monkeypatch.setattr(config, "REGISTRATION_ENABLED", True)
    monkeypatch.setattr(config, "OPD_ENABLED", True)
    monkeypatch.setattr(config, "ADT_ENABLED", False)
    monkeypatch.setattr(realtime, "schedule_on_loop", lambda make_coro, what: True)
    assert config.feature_enabled("opd")
    yield


@pytest.fixture
def opd_off(monkeypatch):
    monkeypatch.setattr(config, "FRONTDESK_ENABLED", True)
    monkeypatch.setattr(config, "OPD_ENABLED", False)
    yield


# ── helpers ──────────────────────────────────────────────────────────────────

def _outpatient(status="registered", user_id=None):
    pid = f"CVP{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO patients (id, user_id, name, mrn, age, date_of_birth, sex, phone, "
            "status, created_at) VALUES (?, ?, ?, ?, 58, '1968-03-14', 'F', '9876501234', ?, ?)",
            (pid, user_id, f"Conversion Patient {pid[-4:]}",
             f"UHID-{uuid.uuid4().hex[:8].upper()}", status,
             datetime.now(timezone.utc).isoformat()))
    _SEEDED.append(pid)
    return pid


def _stated(pid):
    with db_session() as db:
        r = db.execute("SELECT name, mrn FROM patients WHERE id = ?", (pid,)).fetchone()
    return {"name": r["name"], "date_of_birth": "1968-03-14", "uhid": r["mrn"]}


def _visit(doc, pid=None):
    pid = pid or _outpatient()
    resp = client.post("/api/frontdesk/opd/visits",
                       json={"patient_id": pid, "doctor_id": doc,
                             "chief_complaint": "Fever for three days",
                             "identity_check": _stated(pid)},
                       headers=_auth(doc))
    assert resp.status_code == 200, resp.text
    return resp.json()


def _body(visit, doc, **over):
    body = {
        "opd_visit_id": visit["id"],
        "identity_check": _stated(visit["patient_id"]),
        "no_signed_note_reason": "Note written on paper in the clinic",
        "admission_source": "clinic", "admission_type": "inpatient",
        "department": WARD_DEPT, "ward": "CV-W1", "bed": f"B{uuid.uuid4().hex[:3]}",
        "attending_doctor_id": doc, "chief_complaint": "Fever for three days",
        "initial_diagnosis": "Possible pneumonia",
    }
    body.update(over)
    return body


def _convert(doc, body, key=None, role="doctor"):
    return client.post("/api/admissions/admit-existing", json=body,
                       headers=_auth(doc, role, key or uuid.uuid4().hex))


def _one(sql, params=()):
    with db_session() as db:
        row = db.execute(sql, params).fetchone()
    return dict(row) if row is not None else None


def _all(sql, params=()):
    with db_session() as db:
        return [dict(r) for r in db.execute(sql, params).fetchall()]


def _count(table, pid):
    return _one(f"SELECT COUNT(*) AS n FROM {table} WHERE patient_id = ?", (pid,))["n"]


def _audits(action, pid):
    return _all("SELECT * FROM audit_logs WHERE action_type = ? AND patient_id = ? ORDER BY id",
                (action, pid))


def _meta(row):
    return json.loads(row["metadata_json"] or "{}")


def _nothing_admitted(pid, visit_id):
    assert _one("SELECT status FROM patients WHERE id = ?", (pid,))["status"] == "registered"
    assert _count("admissions", pid) == 0
    assert _count("channels", pid) == 0
    assert _one("SELECT status, admission_id FROM opd_visits WHERE id = ?", (visit_id,)) == {
        "status": "waiting", "admission_id": None}
    assert _one("SELECT COUNT(*) AS n FROM encounter_charges WHERE source_ref = ?",
                (visit_id,))["n"] == 0


# ═════════════════════════════════════════════════════════════════════════════
# 1. Off by default
# ═════════════════════════════════════════════════════════════════════════════

def test_both_routes_are_404_with_the_feature_off(staff, opd_off):
    hdr = _auth(staff["doc"], key="k-off")
    resp = client.get("/api/admissions/admit-existing/visits/X", headers=hdr)
    assert resp.status_code == 404 and resp.json()["detail"]["error"] == "feature_disabled"
    resp = client.post("/api/admissions/admit-existing", json={}, headers=hdr)
    assert resp.status_code == 404 and resp.json()["detail"]["error"] == "feature_disabled"


def test_no_session_is_401(opd_on):
    assert client.get("/api/admissions/admit-existing/visits/X").status_code == 401
    assert client.post("/api/admissions/admit-existing", json={}).status_code == 401


def test_the_index_refusal_points_to_the_visit_only_with_outpatients_on(staff, opd_on,
                                                                        monkeypatch):
    pid = _outpatient()
    uhid = _one("SELECT mrn FROM patients WHERE id = ?", (pid,))["mrn"]
    monkeypatch.setattr(config, "UHID_SOURCE", "hospital")
    monkeypatch.setattr(config, "UHID_PATTERN", None)
    form = {"name": "Someone Else Entirely", "age": 40, "sex": "M", "mrn": uhid,
            "admission_source": "er", "admission_type": "emergency", "department": WARD_DEPT,
            "ward": "CV-W1", "bed": "B900", "attending_doctor_id": staff["ward_doc"],
            "chief_complaint": "Pain"}
    resp = client.post("/api/admissions/admit", json=form, headers=_auth(staff["ward_doc"]))
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["error"] == "possible_existing_patient"
    assert detail["admit_existing_available"] is True
    assert "Advise admission" in detail["message"]
    monkeypatch.setattr(config, "OPD_ENABLED", False)
    resp = client.post("/api/admissions/admit", json=form, headers=_auth(staff["ward_doc"]))
    assert resp.json()["detail"]["admit_existing_available"] is False
    assert resp.json()["detail"]["message"].endswith(adm.ADMIT_EXISTING_NOT_BUILT)


# ═════════════════════════════════════════════════════════════════════════════
# 2. The same record, one transaction
# ═════════════════════════════════════════════════════════════════════════════

def test_a_conversion_keeps_the_record_and_writes_one_admission(staff, opd_on):
    doc = staff["doc"]
    visit = _visit(doc)
    pid = visit["patient_id"]
    before = _one("SELECT id, name, mrn, age, date_of_birth, sex, phone FROM patients "
                  "WHERE id = ?", (pid,))
    patients_before = _one("SELECT COUNT(*) AS n FROM patients")["n"]

    resp = _convert(doc, _body(visit, doc))
    assert resp.status_code == 201, resp.text
    out = resp.json()
    assert out["patient_id"] == pid and out["mrn"] == before["mrn"]
    assert out["replayed"] is False and out["portal_login_created"] is True
    assert out["temporary_password"] and out["patient_login_id"]
    assert out["visit"] == {"id": visit["id"], "department": CLINIC, "status": "completed",
                            "disposition": "admitted"}

    assert _one("SELECT COUNT(*) AS n FROM patients")["n"] == patients_before
    after = _one("SELECT id, name, mrn, age, date_of_birth, sex, phone, status, user_id, "
                 "ward, bed, diagnosis FROM patients WHERE id = ?", (pid,))
    for field in ("name", "mrn", "age", "date_of_birth", "sex", "phone"):
        assert after[field] == before[field], field
    assert after["status"] == "admitted" and after["user_id"] == out["patient_login_id"]
    assert after["diagnosis"] == "Possible pneumonia"

    row = _one("SELECT * FROM admissions WHERE patient_id = ?", (pid,))
    assert row["id"] == out["admission_id"] and row["status"] == "active"
    assert row["opd_visit_id"] == visit["id"] and row["department"] == WARD_DEPT
    assert row["initial_vitals_json"] is None
    v = _one("SELECT status, disposition, admission_id, consultation_charge_id "
             "FROM opd_visits WHERE id = ?", (visit["id"],))
    assert v["status"] == "completed" and v["disposition"] == "admitted"
    assert v["admission_id"] == out["admission_id"] and v["consultation_charge_id"]

    logins = _all("SELECT id, role FROM users WHERE id = ?", (out["patient_login_id"],))
    assert logins == [{"id": out["patient_login_id"], "role": "patient"}]
    assert _count("patient_profiles", pid) == 1
    rooms = _all("SELECT id FROM channels WHERE patient_id = ? AND type = 'case_room'", (pid,))
    assert [r["id"] for r in rooms] == [out["channel_id"]]
    lines = [r["content"] for r in _all("SELECT content FROM messages WHERE channel_id = ?",
                                        (out["channel_id"],))]
    assert len(lines) == 1 and lines[0].startswith("**ADMISSION**")
    assert f"From outpatients: {CLINIC} clinic" in lines[0]
    assert "not carried onto the inpatient chart" in lines[0]
    assert _one("SELECT COUNT(*) AS n FROM sage_room_memory WHERE room_id = ?",
                (out["channel_id"],))["n"] == 1

    record = _audits(adm.CONVERSION_AUDIT_ACTION, pid)
    assert len(record) == 1 and record[0]["event_category"] == "clinical_access"
    meta = _meta(record[0])
    assert meta["opd_visit_id"] == visit["id"] and meta["same_record"] is True
    assert meta["identity_check"]["result"] == "match"
    assert meta["opd_vitals_copied"] is False and meta["opd_medicines_copied"] is False
    assert meta["no_signed_note_reason"] == "Note written on paper in the clinic"
    assert "1968" not in record[0]["metadata_json"]
    done = _audits("opd_visit_completed", pid)
    assert len(done) == 1 and _meta(done[0])["disposition"] == "admitted"


def test_a_same_name_patient_is_never_touched(staff, opd_on):
    """The conversion is addressed by the visit, never by a name: another
    registered patient with the same name and date of birth keeps their own
    record, untouched (no admission, login, profile, case room or allergy)."""
    doc = staff["doc"]
    visit = _visit(doc)
    pid = visit["patient_id"]
    twin = f"CVP{uuid.uuid4().hex[:8].upper()}"
    name = _one("SELECT name FROM patients WHERE id = ?", (pid,))["name"]
    with db_session() as db:
        db.execute(
            "INSERT INTO patients (id, name, mrn, age, date_of_birth, sex, phone, status, "
            "created_at) VALUES (?, ?, ?, 58, '1968-03-14', 'F', '9876509999', 'registered', ?)",
            (twin, name, f"UHID-{uuid.uuid4().hex[:8].upper()}",
             datetime.now(timezone.utc).isoformat()))
    _SEEDED.append(twin)
    twin_before = _one("SELECT * FROM patients WHERE id = ?", (twin,))

    resp = _convert(doc, _body(visit, doc, allergies=["Sulfa"]))
    assert resp.status_code == 201, resp.text
    assert resp.json()["patient_id"] == pid
    assert _one("SELECT * FROM patients WHERE id = ?", (twin,)) == twin_before
    for table in ("admissions", "patient_profiles", "channels", "patient_allergies"):
        assert _count(table, twin) == 0, table


def test_the_opd_charge_stays_op_and_counts_in_the_stay_advisory(staff, opd_on):
    from services import billing_service

    doc = staff["doc"]
    visit = _visit(doc)
    out = _convert(doc, _body(visit, doc)).json()
    charges = _all("SELECT encounter_type, admission_id, status FROM encounter_charges "
                   "WHERE source_ref = ?", (visit["id"],))
    assert charges == [{"encounter_type": "op", "admission_id": None, "status": "pending"}]
    summary = billing_service.patient_financial_summary(
        visit["patient_id"], admission_id=out["admission_id"])
    assert summary["unbilled_charge_count"] >= 1


def test_no_opd_observation_and_no_opd_prescription_reaches_the_inpatient_chart(staff, opd_on):
    doc = staff["doc"]
    visit = _visit(doc)
    pid = visit["patient_id"]
    now = datetime.now(timezone.utc)
    measured = (now - timedelta(hours=2)).isoformat()
    with db_session() as db:
        db.execute(
            "INSERT INTO opd_consultations (id, visit_id, patient_id, department, author_id, "
            "status, chief_complaint, vitals_json, diagnoses_json, created_at, updated_at, "
            "updated_by) VALUES (?, ?, ?, ?, ?, 'draft', 'Cough and fever', ?, ?, ?, ?, ?)",
            (f"CNS-{uuid.uuid4().hex[:8]}", visit["id"], pid, CLINIC, doc,
             json.dumps({"values": {"heart_rate": 118, "spo2": 91}, "measured_at": measured,
                         "measured_by": doc}),
             json.dumps([{"text": "Community acquired pneumonia"}]),
             now.isoformat(), now.isoformat(), doc))
        db.execute(
            "INSERT INTO med_orders (id, patient_id, encounter_type, status, source, drug_name, "
            "dose, route, frequency, entered_by, prescriber_id, prescribed_at, created_at, "
            "updated_at) VALUES (?, ?, 'op', 'prescribed', 'cpoe', 'Amoxicillin', '500 mg', "
            "'Oral', 'TDS', ?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), pid, doc, doc, measured, measured, measured))

    preview = client.get(f"/api/admissions/admit-existing/visits/{visit['id']}",
                         headers=_auth(doc))
    assert preview.status_code == 200, preview.text
    p = preview.json()
    assert p["opd_vitals"] == [{"measured_at": measured, "measured_by_name": "Dr Ira Conversion",
                                "values": {"heart_rate": 118, "spo2": 91}}]
    assert [m["drug_name"] for m in p["opd_medicines"]] == ["Amoxicillin"]
    assert p["carried"] == {"chief_complaint": "Cough and fever",
                            "initial_diagnosis": "Community acquired pneumonia"}

    refused = _convert(doc, _body(visit, doc, vitals={"hr": 118}))
    assert refused.status_code == 422
    resp = _convert(doc, _body(visit, doc))
    assert resp.status_code == 201, resp.text
    for table in ("vitals", "risk_scores", "medications"):
        assert _count(table, pid) == 0, table
    assert _count("med_orders", pid) == 1
    assert _one("SELECT initial_vitals_json FROM admissions WHERE patient_id = ?",
                (pid,))["initial_vitals_json"] is None


def test_charted_allergies_are_kept_and_new_ones_added(staff, opd_on):
    doc = staff["doc"]
    visit = _visit(doc)
    pid = visit["patient_id"]
    with db_session() as db:
        db.execute("INSERT INTO patient_allergies (patient_id, substance, reaction, severity, "
                   "status) VALUES (?, 'penicillin', 'anaphylaxis', 'severe', 'active')", (pid,))
    resp = _convert(doc, _body(visit, doc, allergies=["Penicillin", "Sulfa"]))
    assert resp.status_code == 201, resp.text
    rows = _all("SELECT substance, reaction, severity, status FROM patient_allergies "
                "WHERE patient_id = ? ORDER BY substance", (pid,))
    assert rows == [
        {"substance": "penicillin", "reaction": "anaphylaxis", "severity": "severe",
         "status": "active"},
        {"substance": "sulfa", "reaction": None, "severity": "unknown", "status": "active"},
    ]
    assert resp.json()["allergies_added"] == ["sulfa"]
    line = _one("SELECT content FROM messages WHERE channel_id = ?",
                (resp.json()["channel_id"],))["content"]
    assert "Allergies: penicillin, sulfa" in line


def test_an_existing_login_and_case_room_are_reused_never_doubled(staff, opd_on):
    doc = staff["doc"]
    login = f"PCV{uuid.uuid4().hex[:5].upper()}"
    with db_session() as db:
        db.execute("INSERT INTO users (id, name, role, password_hash) VALUES (?, 'Portal', "
                   "'patient', 'x')", (login,))
    pid = _outpatient(user_id=login)
    room = f"chan_cv{uuid.uuid4().hex[:8]}"
    with db_session() as db:
        db.execute("INSERT INTO channels (id, name, type, patient_id, created_at) "
                   "VALUES (?, 'Case: earlier', 'case_room', ?, ?)",
                   (room, pid, datetime.now(timezone.utc).isoformat()))
    visit = _visit(doc, pid)
    users_before = _one("SELECT COUNT(*) AS n FROM users")["n"]
    out = _convert(doc, _body(visit, doc)).json()
    assert out["patient_login_id"] == login and out["temporary_password"] is None
    assert out["portal_login_created"] is False
    assert _one("SELECT COUNT(*) AS n FROM users")["n"] == users_before
    assert out["channel_id"] == room and out["case_room_reused"] is True
    assert _count("channels", pid) == 1
    assert _one("SELECT content FROM messages WHERE channel_id = ?",
                (room,))["content"].startswith("**NEW ADMISSION**")


def test_an_unwritable_conversion_record_leaves_nothing_behind(staff, opd_on, monkeypatch):
    import services.audit_service as audit_service

    doc = staff["doc"]
    visit = _visit(doc)
    pid = visit["patient_id"]
    real = audit_service.log_audit_event

    def failing(*args, **kwargs):
        if kwargs.get("action_type") == adm.CONVERSION_AUDIT_ACTION:
            raise AuditWriteError("disk full")
        return real(*args, **kwargs)

    monkeypatch.setattr(audit_service, "log_audit_event", failing)
    users_before = _one("SELECT COUNT(*) AS n FROM users")["n"]
    resp = _convert(doc, _body(visit, doc))
    assert resp.status_code == 503 and resp.json()["detail"]["error"] == "audit_unavailable"
    _nothing_admitted(pid, visit["id"])
    assert _one("SELECT COUNT(*) AS n FROM users")["n"] == users_before
    assert _count("patient_profiles", pid) == 0


# ═════════════════════════════════════════════════════════════════════════════
# 3. Who the patient is (OPD-S1) and the note (OPD-N8)
# ═════════════════════════════════════════════════════════════════════════════

def test_no_identity_check_is_refused_audited_and_writes_nothing(staff, opd_on):
    doc = staff["doc"]
    visit = _visit(doc)
    body = _body(visit, doc)
    body.pop("identity_check")
    resp = _convert(doc, body)
    assert resp.status_code == 409 and resp.json()["detail"]["error"] == "identity_check_required"
    _nothing_admitted(visit["patient_id"], visit["id"])
    rows = _audits(adm.CONVERSION_IDENTITY_REFUSED_ACTION, visit["patient_id"])
    assert len(rows) == 1 and rows[0]["event_category"] == "patient_identity"


def test_a_mismatched_identity_is_refused_and_writes_nothing(staff, opd_on):
    doc = staff["doc"]
    visit = _visit(doc)
    wrong = {"name": "Not This Person", "date_of_birth": "1990-01-01", "uhid": "NOPE-1"}
    resp = _convert(doc, _body(visit, doc, identity_check=wrong))
    assert resp.status_code == 409 and resp.json()["detail"]["error"] == "identity_not_confirmed"
    _nothing_admitted(visit["patient_id"], visit["id"])
    assert _audits("identity_check_failed", visit["patient_id"])


def test_repeated_mismatches_lock_the_check(staff, opd_on):
    from services import registration_service as reg

    doc = staff["doc"]
    visit = _visit(doc)
    wrong = {"name": "Not This Person", "date_of_birth": "1990-01-01", "uhid": "NOPE-1"}
    for _ in range(reg.IDENTITY_FAILURE_LIMIT):
        assert _convert(doc, _body(visit, doc, identity_check=wrong)).status_code == 409
    resp = _convert(doc, _body(visit, doc))
    assert resp.status_code == 429 and resp.json()["detail"]["error"] == "identity_check_locked"
    _nothing_admitted(visit["patient_id"], visit["id"])


def test_identity_fields_cannot_be_retyped(staff, opd_on):
    doc = staff["doc"]
    visit = _visit(doc)
    for field, value in (("name", "Someone Else"), ("mrn", "X-1"), ("age", 30),
                         ("date_of_birth", "1990-01-01")):
        resp = _convert(doc, _body(visit, doc, **{field: value}))
        assert resp.status_code == 422, field
    _nothing_admitted(visit["patient_id"], visit["id"])


def test_an_unsigned_note_needs_a_reason_and_a_signed_one_does_not(staff, opd_on):
    doc = staff["doc"]
    visit = _visit(doc)
    resp = _convert(doc, _body(visit, doc, no_signed_note_reason=None))
    assert resp.status_code == 409 and resp.json()["detail"]["error"] == "signed_note_required"
    _nothing_admitted(visit["patient_id"], visit["id"])

    now = datetime.now(timezone.utc).isoformat()
    with db_session() as db:
        db.execute(
            "INSERT INTO opd_consultations (id, visit_id, patient_id, department, author_id, "
            "status, created_at, updated_at, updated_by, signed_by, signed_at) "
            "VALUES (?, ?, ?, ?, ?, 'signed', ?, ?, ?, ?, ?)",
            (f"CNS-{uuid.uuid4().hex[:8]}", visit["id"], visit["patient_id"], CLINIC, doc,
             now, now, doc, doc, now))
    resp = _convert(doc, _body(visit, doc, no_signed_note_reason=None))
    assert resp.status_code == 201, resp.text


# ═════════════════════════════════════════════════════════════════════════════
# 4. Access moves from the clinic to the ward
# ═════════════════════════════════════════════════════════════════════════════

def test_outpatient_access_ends_and_ward_access_begins(staff, opd_on):
    doc, ward_doc = staff["doc"], staff["ward_doc"]
    visit = _visit(doc)
    pid = visit["patient_id"]
    assert client.get(f"/api/opd/patients/{pid}/record", headers=_auth(doc)).status_code == 200
    assert client.get(f"/api/vigil/patients/{pid}", headers=_auth(ward_doc)).status_code == 404

    assert _convert(doc, _body(visit, doc)).status_code == 201
    assert client.get(f"/api/vigil/patients/{pid}", headers=_auth(ward_doc)).status_code == 200
    assert client.get(f"/api/opd/patients/{pid}/record", headers=_auth(doc)).status_code == 404


# ═════════════════════════════════════════════════════════════════════════════
# 5. Refusals
# ═════════════════════════════════════════════════════════════════════════════

def test_a_discharged_patient_is_refused_here(staff, opd_on):
    doc = staff["doc"]
    pid = _outpatient()
    visit = _visit(doc, pid)
    with db_session() as db:
        db.execute("INSERT INTO admissions (id, patient_id, admitted_by, department, status, "
                   "created_at) VALUES (?, ?, ?, ?, 'discharged', ?)",
                   (f"ADM-CV{uuid.uuid4().hex[:6]}", pid, staff["ward_doc"], WARD_DEPT,
                    (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()))
        db.execute("UPDATE patients SET status = 'discharged' WHERE id = ?", (pid,))
    preview = client.get(f"/api/admissions/admit-existing/visits/{visit['id']}",
                         headers=_auth(doc)).json()
    assert preview["admittable"] is False
    assert preview["refusal"]["error"] == "readmission_not_available"
    assert set(preview) == {"visit", "admittable", "refusal"}
    resp = _convert(doc, _body(visit, doc))
    assert resp.status_code == 409 and resp.json()["detail"]["error"] == "readmission_not_available"
    assert _count("admissions", pid) == 1
    assert _one("SELECT status FROM opd_visits WHERE id = ?", (visit["id"],))["status"] == "waiting"


def test_closed_and_earlier_visits_are_refused(staff, opd_on):
    doc = staff["doc"]
    finished = _visit(doc)
    with db_session() as db:
        db.execute("UPDATE opd_visits SET status = 'completed' WHERE id = ?", (finished["id"],))
    resp = _convert(doc, _body(finished, doc))
    assert resp.status_code == 409 and resp.json()["detail"]["error"] == "visit_completed"

    cancelled = _visit(doc)
    with db_session() as db:
        db.execute("UPDATE opd_visits SET status = 'cancelled' WHERE id = ?", (cancelled["id"],))
    resp = _convert(doc, _body(cancelled, doc))
    assert resp.status_code == 409 and resp.json()["detail"]["error"] == "visit_cancelled"

    earlier = _visit(doc)
    with db_session() as db:
        db.execute("UPDATE opd_visits SET visit_date = '2026-01-02' WHERE id = ?",
                   (earlier["id"],))
    resp = _convert(doc, _body(earlier, doc))
    assert resp.status_code == 409 and resp.json()["detail"]["error"] == "visit_not_current"
    _nothing_admitted(earlier["patient_id"], earlier["id"])


def test_another_clinic_a_nurse_and_the_administrator_cannot_convert(staff, opd_on):
    doc = staff["doc"]
    visit = _visit(doc)
    resp = _convert(staff["other_doc"], _body(visit, staff["other_doc"]))
    assert resp.status_code == 404
    assert client.get(f"/api/admissions/admit-existing/visits/{visit['id']}",
                      headers=_auth(staff["other_doc"])).status_code == 404
    assert _convert(staff["nurse"], _body(visit, doc), role="nurse").status_code == 403
    assert _convert(staff["admin"], _body(visit, doc), role="admin").status_code == 403
    _nothing_admitted(visit["patient_id"], visit["id"])


def test_an_admitted_patient_and_a_missing_key_are_refused(staff, opd_on):
    doc = staff["doc"]
    visit = _visit(doc)
    resp = client.post("/api/admissions/admit-existing", json=_body(visit, doc),
                       headers=_auth(doc))
    assert resp.status_code == 422 and resp.json()["detail"]["error"] == "idempotency_key_required"
    with db_session() as db:
        db.execute("UPDATE patients SET status = 'admitted' WHERE id = ?",
                   (visit["patient_id"],))
    resp = _convert(doc, _body(visit, doc))
    assert resp.status_code == 409 and resp.json()["detail"]["error"] == "patient_already_admitted"


# ═════════════════════════════════════════════════════════════════════════════
# 6. Replays and races
# ═════════════════════════════════════════════════════════════════════════════

def test_a_replay_returns_the_first_admission_and_writes_nothing(staff, opd_on):
    doc = staff["doc"]
    visit = _visit(doc)
    pid = visit["patient_id"]
    first = _convert(doc, _body(visit, doc), key="cv-replay-1")
    assert first.status_code == 201
    again = _convert(doc, _body(visit, doc), key="cv-replay-1")
    assert again.status_code == 200, again.text
    out = again.json()
    assert out["replayed"] is True and out["temporary_password"] is None
    assert out["admission_id"] == first.json()["admission_id"]
    assert out["channel_id"] == first.json()["channel_id"]
    assert _count("admissions", pid) == 1
    assert len(_audits(adm.CONVERSION_AUDIT_ACTION, pid)) == 1
    assert _one("SELECT COUNT(*) AS n FROM encounter_charges WHERE source_ref = ?",
                (visit["id"],))["n"] == 1

    other = _visit(doc)
    resp = _convert(doc, _body(other, doc), key="cv-replay-1")
    assert resp.status_code == 409 and resp.json()["detail"]["error"] == "idempotency_key_reused"
    _nothing_admitted(other["patient_id"], other["id"])


def test_a_second_conversion_with_another_key_is_refused(staff, opd_on):
    doc = staff["doc"]
    visit = _visit(doc)
    assert _convert(doc, _body(visit, doc)).status_code == 201
    resp = _convert(doc, _body(visit, doc))
    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] in ("visit_completed", "patient_already_admitted")
    assert _count("admissions", visit["patient_id"]) == 1


def test_two_conversions_at_once_give_one_admission(staff, opd_on):
    doc = staff["doc"]
    visit = _visit(doc)
    user = {"id": doc, "role": "doctor", "department": CLINIC, "name": "Dr Ira Conversion"}
    barrier = threading.Barrier(2)
    results: dict[str, object] = {}

    def run(key: str) -> None:
        body = _body(visit, doc)
        data = {k: v for k, v in body.items() if k not in ("opd_visit_id", "identity_check")}
        barrier.wait()
        try:
            results[key] = adm.admit_existing_patient(
                visit["id"], data, user, scope_department=CLINIC,
                identity=body["identity_check"], idempotency_key=key)
        except Exception as exc:  # noqa: BLE001 - the loser's refusal is the point
            results[key] = exc

    threads = [threading.Thread(target=run, args=(f"race-{n}-{uuid.uuid4().hex}",))
               for n in (1, 2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    wins = [r for r in results.values() if isinstance(r, dict)]
    losses = [r for r in results.values() if isinstance(r, adm.AdmissionRefused)]
    assert len(wins) == 1 and len(losses) == 1, results
    assert losses[0].code in ("visit_completed", "patient_already_admitted")
    assert _count("admissions", visit["patient_id"]) == 1
    assert _count("channels", visit["patient_id"]) == 1


# ═════════════════════════════════════════════════════════════════════════════
# 7. The preview
# ═════════════════════════════════════════════════════════════════════════════

def test_the_preview_shows_identity_without_the_date_of_birth_and_is_audited(staff, opd_on):
    doc = staff["doc"]
    visit = _visit(doc)
    pid = visit["patient_id"]
    resp = client.get(f"/api/admissions/admit-existing/visits/{visit['id']}",
                      headers=_auth(doc))
    assert resp.status_code == 200, resp.text
    p = resp.json()
    assert set(p["patient"]) == {"id", "name", "uhid", "age", "sex", "portal_login",
                                 "date_of_birth_on_file"}
    assert "1968" not in resp.text and "9876501234" not in resp.text
    assert p["admittable"] is True and p["refusal"] is None
    assert p["opd_vitals"] == [] and p["allergy_status"] == "none_recorded"
    rows = _audits(adm.CONVERSION_PREVIEW_AUDIT_ACTION, pid)
    assert len(rows) == 1 and rows[0]["event_category"] == "phi_access"


def test_the_ward_record_lists_no_medicine(staff, opd_on, monkeypatch):
    monkeypatch.setattr(config, "WARD_RECORD", True, raising=False)
    doc = staff["doc"]
    visit = _visit(doc)
    p = client.get(f"/api/admissions/admit-existing/visits/{visit['id']}",
                   headers=_auth(doc)).json()
    assert p["medicines_listed"] is False and p["opd_medicines"] == []


# A refused preview is the visit and the refusal, nothing about the patient.
# The visit check (same clinic) is not the patient's scope: the clinic reads a
# patient only through the OPD arm, which a finished-long-ago, cancelled or
# admitted-elsewhere patient's visit no longer opens, and the clinic's queue
# lists every visit id of any day.

_VISIT_REFERENCE_KEYS = {"id", "visit_date", "department", "doctor_name", "token_number",
                         "status"}


def _chart_reference_data(pid, visit_id, doc, substance="penicillin"):
    """An allergy, a consultation with observations and a live outpatient
    prescription: everything an admittable preview would show."""
    now = datetime.now(timezone.utc).isoformat()
    with db_session() as db:
        db.execute("INSERT INTO patient_allergies (patient_id, substance, reaction, severity, "
                   "status) VALUES (?, ?, 'anaphylaxis', 'severe', 'active')", (pid, substance))
        db.execute(
            "INSERT INTO opd_consultations (id, visit_id, patient_id, department, author_id, "
            "status, chief_complaint, vitals_json, diagnoses_json, created_at, updated_at, "
            "updated_by) VALUES (?, ?, ?, ?, ?, 'draft', 'Breathless on stairs', ?, ?, ?, ?, ?)",
            (f"CNS-{uuid.uuid4().hex[:8]}", visit_id, pid, CLINIC, doc,
             json.dumps({"values": {"heart_rate": 131}, "measured_at": now, "measured_by": doc}),
             json.dumps([{"text": "Heart failure"}]), now, now, doc))
        db.execute(
            "INSERT INTO med_orders (id, patient_id, encounter_type, status, source, drug_name, "
            "dose, route, frequency, entered_by, prescriber_id, prescribed_at, created_at, "
            "updated_at) VALUES (?, ?, 'op', 'prescribed', 'cpoe', 'Furosemide', '40 mg', "
            "'Oral', 'OD', ?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), pid, doc, doc, now, now, now))


def _assert_refusal_only(resp, pid, code, substance="penicillin"):
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) == {"visit", "admittable", "refusal"}, body
    assert set(body["visit"]) == _VISIT_REFERENCE_KEYS
    assert body["admittable"] is False and body["refusal"]["error"] == code
    record = _one("SELECT name, mrn FROM patients WHERE id = ?", (pid,))
    for secret in (pid, record["name"], record["mrn"], substance, "anaphylaxis",
                   "Furosemide", "heart_rate", "Breathless", "Heart failure", "1968",
                   "9876501234"):
        assert secret not in resp.text, secret
    rows = _audits(adm.CONVERSION_PREVIEW_AUDIT_ACTION, pid)
    assert rows and rows[-1]["event_category"] == "phi_access"
    meta = _meta(rows[-1])
    assert meta["admittable"] is False and meta["refusal"] == code
    assert meta["patient_details_shown"] is False and meta["opd_medicines_listed"] == 0


@pytest.mark.parametrize("status, code, day", [
    ("completed", "visit_completed", "2025-01-02"),
    ("waiting", "visit_not_current", "2025-01-03"),
])
def test_an_old_visit_shows_the_refusal_and_nothing_about_the_patient(staff, opd_on,
                                                                      status, code, day):
    doc = staff["doc"]
    visit = _visit(doc)
    pid = visit["patient_id"]
    _chart_reference_data(pid, visit["id"], doc)
    with db_session() as db:
        db.execute("UPDATE opd_visits SET visit_date = ?, status = ? WHERE id = ?",
                   (day, status, visit["id"]))
    # The clinic is outside this patient's OPD scope now ...
    assert client.get(f"/api/opd/patients/{pid}/record",
                      headers=_auth(doc)).status_code == 404
    # ... so the admission form's read of the old visit shows only the refusal.
    resp = client.get(f"/api/admissions/admit-existing/visits/{visit['id']}",
                      headers=_auth(doc))
    _assert_refusal_only(resp, pid, code)
    assert resp.json()["visit"]["visit_date"] == day


def test_a_cancelled_visit_shows_the_refusal_and_nothing_about_the_patient(staff, opd_on):
    doc = staff["doc"]
    visit = _visit(doc)
    pid = visit["patient_id"]
    _chart_reference_data(pid, visit["id"], doc)
    with db_session() as db:
        db.execute("UPDATE opd_visits SET status = 'cancelled' WHERE id = ?", (visit["id"],))
    assert client.get(f"/api/opd/patients/{pid}/record",
                      headers=_auth(doc)).status_code == 404
    resp = client.get(f"/api/admissions/admit-existing/visits/{visit['id']}",
                      headers=_auth(doc))
    _assert_refusal_only(resp, pid, "visit_cancelled")


def test_a_patient_admitted_by_another_clinic_shows_nothing_to_this_one(staff, opd_on):
    doc, other = staff["doc"], staff["other_doc"]
    pid = _outpatient()
    here = _visit(doc, pid)
    there = _visit(other, pid)
    _chart_reference_data(pid, here["id"], doc, substance="sulfa")
    converted = _convert(other, _body(there, other))
    assert converted.status_code == 201, converted.text
    # An active admission anywhere closes the OPD arm completely ...
    assert client.get(f"/api/opd/patients/{pid}/record",
                      headers=_auth(doc)).status_code == 404
    # ... so this clinic's still-waiting visit tells it why, and nothing else.
    assert _one("SELECT status FROM opd_visits WHERE id = ?", (here["id"],))["status"] == "waiting"
    resp = client.get(f"/api/admissions/admit-existing/visits/{here['id']}",
                      headers=_auth(doc))
    _assert_refusal_only(resp, pid, "patient_already_admitted", substance="sulfa")


def test_an_admittable_preview_is_always_inside_the_opd_arm(staff, opd_on):
    """The patient block is shown only for an admittable visit, and such a
    visit (open, today, this clinic, no admission) always holds the OPD arm
    open, so the form reads nothing the outpatient record view would not."""
    import permissions

    doc = staff["doc"]
    visit = _visit(doc)
    pid = visit["patient_id"]
    _chart_reference_data(pid, visit["id"], doc)
    resp = client.get(f"/api/admissions/admit-existing/visits/{visit['id']}",
                      headers=_auth(doc))
    assert resp.status_code == 200 and resp.json()["admittable"] is True
    assert resp.json()["allergies"][0]["substance"] == "penicillin"
    with db_session() as db:
        decision = permissions.opd_scope_decision(db, pid, CLINIC)
    assert decision is not None and decision["arm"] == "opd_visit"
    assert decision["visit_id"] == visit["id"]
    assert client.get(f"/api/opd/patients/{pid}/record",
                      headers=_auth(doc)).status_code == 200
    meta = _meta(_audits(adm.CONVERSION_PREVIEW_AUDIT_ACTION, pid)[-1])
    assert meta["patient_details_shown"] is True and meta["opd_medicines_listed"] == 1


# ═════════════════════════════════════════════════════════════════════════════
# 8. Beds and admissions (ORB_ADT) on
# ═════════════════════════════════════════════════════════════════════════════

def test_with_adt_on_the_conversion_is_placed_through_the_bed_master(staff, opd_on,
                                                                     monkeypatch):
    monkeypatch.setattr(config, "ADT_ENABLED", True)
    doc = staff["doc"]
    visit = _visit(doc)
    pid = visit["patient_id"]
    resp = _convert(doc, _body(visit, doc, ward="", bed="", awaiting_bed=True))
    assert resp.status_code == 201, resp.text
    out = resp.json()
    assert out["movement_id"] and out["location"]["awaiting_bed"] is True
    moves = _all("SELECT movement_type, admission_id FROM patient_movements "
                 "WHERE patient_id = ?", (pid,))
    assert moves == [{"movement_type": "admission", "admission_id": out["admission_id"]}]
    assert _one("SELECT admitted_at FROM admissions WHERE id = ?",
                (out["admission_id"],))["admitted_at"]
    assert _audits(adm.CONVERSION_AUDIT_ACTION, pid)
