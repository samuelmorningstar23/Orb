"""The /api/registration routes (opd-4, W4-A, 4 October 2026).

What this file pins over HTTP:

  1. FLAG OFF: every registration route answers 404 feature_disabled to a
     signed-in caller (401 with no session).
  2. WHO MAY DO WHAT: registering needs registration.write (the receptionist
     and the doctor, never the administrator, ruling OPD-N3); the index search
     and match are the desk's read; the identity check is the desk's write.
  3. SCOPE: the record, its changes, its card and the PATCH sit behind
     require_patient_access(..., opd_arm=True). A Cardiology doctor gets 404
     for an Oncology inpatient and for a pure outpatient Cardiology never saw;
     a no-department receptionist reads both; a Cardiology OPD visit opens the
     outpatient to Cardiology only while the OPD feature is on.
  4. MINIMAL SEARCH: no date of birth, address or full phone, audited with the
     matched ids.
  5. The identity check never echoes a stored value and the sixth failure is
     429; a correction needs a reason and the row_version; the UHID is the
     administrator's; a body field the desk may not record is refused.
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
from services.hospital_time import hospital_today

client = TestClient(app)

CARDIO = "Regroute Cardiology"
ONCO = "Regroute Oncology"

PATHS = [
    ("GET", "/api/registration/config", None),
    ("GET", "/api/registration/search?q=abcd", None),
    ("POST", "/api/registration/match", {"name": "Abc Def"}),
    ("POST", "/api/registration/patients", {"name": "Abc Def"}),
    ("POST", "/api/registration/identity-check", {"patient_id": "X"}),
    ("GET", "/api/registration/patients/PAT-NOPE", None),
    ("PATCH", "/api/registration/patients/PAT-NOPE", {"reason": "x"}),
    ("GET", "/api/registration/patients/PAT-NOPE/changes", None),
    ("GET", "/api/registration/patients/PAT-NOPE/card", None),
    ("GET", "/api/registration/duplicates", None),
    ("POST", "/api/registration/duplicates/1/resolve", {"resolution": "not_duplicate"}),
    ("POST", "/api/registration/duplicates/sweep", {}),
]


# ── fixtures ─────────────────────────────────────────────────────────────────

def _mk_user(role, department=None):
    uid = f"RGR{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute("INSERT INTO users (id, name, role, password_hash, department, is_active) "
                   "VALUES (?, ?, ?, 'x', ?, 1)", (uid, f"Regroute {role} {uid[-4:]}", role, department))
    return uid


def _hdr(uid, role):
    return {"Authorization": f"Bearer {create_token(uid, role)}"}


@pytest.fixture(scope="module")
def staff():
    return {
        "recep": _mk_user("receptionist"),
        "recep_cardio": _mk_user("receptionist", CARDIO),
        "doc_cardio": _mk_user("doctor", CARDIO),
        "doc_onco": _mk_user("doctor", ONCO),
        "nurse_cardio": _mk_user("nurse", CARDIO),
        "admin": _mk_user("admin"),
    }


def H(staff, who):
    role = {"recep": "receptionist", "recep_cardio": "receptionist", "doc_cardio": "doctor",
            "doc_onco": "doctor", "nurse_cardio": "nurse", "admin": "admin"}[who]
    return _hdr(staff[who], role)


@pytest.fixture
def reg_on(monkeypatch):
    monkeypatch.setattr(config, "REGISTRATION_ENABLED", True)
    monkeypatch.setattr(config, "UHID_SOURCE", "hospital")
    monkeypatch.setattr(config, "UHID_PATTERN", None)
    monkeypatch.setattr(config, "DUPLICATE_MODE", "warn")
    monkeypatch.setattr(config, "ABDM_ENABLED", True)
    assert config.feature_enabled("registration")


@pytest.fixture
def opd_on(reg_on, monkeypatch):
    monkeypatch.setattr(config, "FRONTDESK_ENABLED", True)
    monkeypatch.setattr(config, "OPD_ENABLED", True)
    monkeypatch.setattr(config, "OPD_SCOPE_DAYS", 7)
    assert config.feature_enabled("opd")


def _name():
    def word():
        return "Z" + "".join(random.choice("bdfgklmnprstv") + random.choice("aeiou") for _ in range(3))
    return f"{word()} {word()}"


def _phone():
    return "8" + "".join(random.choice(string.digits) for _ in range(9))


def _dob():
    return f"{random.randint(1935, 2005)}-{random.randint(1, 12):02d}-{random.randint(1, 28):02d}"


def _body(**kw):
    body = {"name": _name(), "sex": "M", "date_of_birth": _dob(), "phone": _phone(),
            "uhid": f"RGR{uuid.uuid4().hex[:8].upper()}", "address_line": "9 Hidden Road",
            "city": "Mysuru"}
    body.update(kw)
    return body


def _register(staff, who="recep", **kw):
    body = _body(**kw)
    resp = client.post("/api/registration/patients", json=body, headers=H(staff, who))
    assert resp.status_code == 201, resp.text
    return resp.json()["patient"], body


def _inpatient(dept):
    pid = f"PAT-RGR{uuid.uuid4().hex[:6].upper()}"
    now = datetime.now(timezone.utc).isoformat()
    with db_session() as db:
        db.execute("INSERT OR IGNORE INTO users (id, name, role, password_hash) "
                   "VALUES ('RGRADMITTER', 'Regroute Admitter', 'doctor', 'x')")
        db.execute("INSERT INTO patients (id, name, mrn, age, sex, status, date_of_birth, phone, "
                   "address, created_at) VALUES (?, ?, ?, 55, 'F', 'admitted', '1971-01-01', ?, "
                   "'1 Ward Lane', ?)", (pid, _name(), f"RGR{uuid.uuid4().hex[:8]}", _phone(), now))
        db.execute("INSERT INTO admissions (id, patient_id, admitted_by, department, ward, bed, "
                   "status, created_at) VALUES (?, ?, 'RGRADMITTER', ?, ?, '1', 'active', ?)",
                   (f"ADM-RGR{uuid.uuid4().hex[:6]}", pid, dept, f"{dept} ward", now))
    return pid


def _visit(pid, dept):
    with db_session() as db:
        db.execute("INSERT INTO opd_visits (id, patient_id, department, visit_date, token_number, "
                   "status, registered_by, registered_at) VALUES (?, ?, ?, ?, ?, 'waiting', "
                   "'RGRADMITTER', ?)", (f"OPD-RGR{uuid.uuid4().hex[:6]}", pid, dept,
                                         hospital_today().isoformat(), random.randint(1000, 99999),
                                         datetime.now(timezone.utc).isoformat()))


def _audit_rows(action, patient_id=None):
    with db_session() as db:
        if patient_id:
            rows = db.execute("SELECT * FROM audit_logs WHERE action_type = ? AND patient_id = ? "
                              "ORDER BY id", (action, patient_id)).fetchall()
        else:
            rows = db.execute("SELECT * FROM audit_logs WHERE action_type = ? ORDER BY id",
                              (action,)).fetchall()
    return [dict(r) for r in rows]


def _call(method, path, body, headers):
    return client.request(method, path, json=body, headers=headers)


# ═════════════════════════════════════════════════════════════════════════════
# 1. Flag off
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("method, path, body", PATHS)
def test_flag_off_every_route_is_switched_off(staff, method, path, body):
    assert not config.feature_enabled("registration")
    resp = _call(method, path, body, H(staff, "recep"))
    assert resp.status_code == 404, resp.text
    assert resp.json()["detail"]["error"] == "feature_disabled"


@pytest.mark.parametrize("method, path, body", PATHS)
def test_no_session_is_401_whatever_the_switch(reg_on, method, path, body):
    assert _call(method, path, body, {}).status_code == 401


# ═════════════════════════════════════════════════════════════════════════════
# 2. Who may do what
# ═════════════════════════════════════════════════════════════════════════════

def test_the_receptionist_and_the_doctor_register_the_administrator_and_nurse_do_not(staff, reg_on):
    assert client.post("/api/registration/patients", json=_body(),
                       headers=H(staff, "recep")).status_code == 201
    assert client.post("/api/registration/patients", json=_body(),
                       headers=H(staff, "doc_cardio")).status_code == 201
    assert client.post("/api/registration/patients", json=_body(),
                       headers=H(staff, "admin")).status_code == 403
    assert client.post("/api/registration/patients", json=_body(),
                       headers=H(staff, "nurse_cardio")).status_code == 403


def test_the_config_route_carries_no_patient_data(staff, reg_on):
    body = client.get("/api/registration/config", headers=H(staff, "recep")).json()
    assert body["uhid_source"] == "hospital" and body["duplicate_mode"] == "warn"
    assert body["allergies_at_desk"] is False
    assert "patients" not in json.dumps(body).lower()


@pytest.mark.parametrize("field, value", [
    ("allergies", ["penicillin"]),
    ("no_known_allergies", True),
    ("aadhaar_number", "123412341234"),
    ("id_document_number", "123412341234"),
])
def test_a_field_the_desk_may_not_record_is_refused(staff, reg_on, field, value):
    resp = client.post("/api/registration/patients", json=_body(**{field: value}),
                       headers=H(staff, "recep"))
    assert resp.status_code == 422, resp.text


def test_register_then_replay_with_the_idempotency_header(staff, reg_on):
    body = _body()
    key = {"Idempotency-Key": f"rgr-{uuid.uuid4().hex}"}
    first = client.post("/api/registration/patients", json=body, headers={**H(staff, "recep"), **key})
    again = client.post("/api/registration/patients", json=body, headers={**H(staff, "recep"), **key})
    assert first.status_code == 201 and again.status_code == 200
    assert first.json()["patient"]["patient_id"] == again.json()["patient"]["patient_id"]
    assert again.json()["replayed"] is True


def test_a_replay_cannot_read_past_the_registrars_scope(staff, reg_on):
    """A Cardiology doctor registers a walk-in with a key. The patient is then
    admitted to Oncology and the central desk corrects the address and adds an
    email. The doctor's GET is 404, and replaying the first request answers the
    id and hospital number only."""
    body = _body()
    key = {"Idempotency-Key": f"rgr-{uuid.uuid4().hex}"}
    doc = {**H(staff, "doc_cardio"), **key}
    first = client.post("/api/registration/patients", json=body, headers=doc)
    assert first.status_code == 201, first.text
    pid, uhid = first.json()["patient"]["patient_id"], first.json()["patient"]["uhid"]
    with db_session() as db:
        db.execute("INSERT OR IGNORE INTO users (id, name, role, password_hash) "
                   "VALUES ('RGRADMITTER', 'Regroute Admitter', 'doctor', 'x')")
        db.execute("INSERT INTO admissions (id, patient_id, admitted_by, department, ward, bed, "
                   "status, created_at) VALUES (?, ?, 'RGRADMITTER', ?, ?, '2', 'active', ?)",
                   (f"ADM-RGR{uuid.uuid4().hex[:6]}", pid, ONCO, f"{ONCO} ward",
                    datetime.now(timezone.utc).isoformat()))
    url = f"/api/registration/patients/{pid}"
    version = client.get(url, headers=H(staff, "recep")).json()["row_version"]
    fixed = client.patch(url, headers=H(staff, "recep"), json={
        "row_version": version, "reason": "Patient gave a new address",
        "address_line": "77 Secret Lane", "email": "secret@example.com"})
    assert fixed.status_code == 200, fixed.text
    assert client.get(url, headers=H(staff, "doc_cardio")).status_code == 404

    again = client.post("/api/registration/patients", json=body, headers=doc)
    assert again.status_code == 200, again.text
    out = again.json()
    assert out["replayed"] is True
    assert out["patient"] == {"patient_id": pid, "uhid": uhid, "record_returned": False}
    text = json.dumps(out)
    for leaked in ("Secret Lane", "secret@example.com", "inpatient_now", "Hidden Road",
                   body["date_of_birth"]):
        assert leaked not in text


def test_a_replay_after_a_day_is_409_and_registers_no_one(staff, reg_on):
    from services import registration_service as reg
    body = _body()
    key = {"Idempotency-Key": f"rgr-{uuid.uuid4().hex}"}
    first = client.post("/api/registration/patients", json=body, headers={**H(staff, "recep"), **key})
    assert first.status_code == 201, first.text
    pid = first.json()["patient"]["patient_id"]
    stale = (datetime.now(timezone.utc) - reg.REPLAY_WINDOW - timedelta(hours=1)).isoformat()
    with db_session() as db:
        db.execute("UPDATE patient_demographics SET registered_at = ? WHERE patient_id = ?",
                   (stale, pid))
    again = client.post("/api/registration/patients", json=body, headers={**H(staff, "recep"), **key})
    assert again.status_code == 409, again.text
    detail = again.json()["detail"]
    assert detail["error"] == "registration_key_expired"
    assert pid not in json.dumps(detail)
    with db_session() as db:
        assert db.execute("SELECT COUNT(*) FROM patients WHERE name = ?",
                          (body["name"],)).fetchone()[0] == 1


def test_an_audit_outage_answers_503_and_saves_nothing(staff, reg_on, monkeypatch):
    from services import registration_service as reg
    from services.audit_service import AuditWriteError
    real = reg.log_audit_event

    def failing(*args, **kwargs):
        if kwargs.get("event_category") == "patient_identity":
            raise AuditWriteError("simulated outage")
        return real(*args, **kwargs)

    monkeypatch.setattr(reg, "log_audit_event", failing)
    body = _body()
    resp = client.post("/api/registration/patients", json=body, headers=H(staff, "recep"))
    assert resp.status_code == 503 and resp.json()["detail"]["error"] == "audit_unavailable"
    with db_session() as db:
        assert db.execute("SELECT COUNT(*) FROM patients WHERE mrn = ?",
                          (body["uhid"],)).fetchone()[0] == 0


# ═════════════════════════════════════════════════════════════════════════════
# 3. Scope on the record, changes, card and PATCH
# ═════════════════════════════════════════════════════════════════════════════

def _record(pid, staff, who):
    return client.get(f"/api/registration/patients/{pid}", headers=H(staff, who))


def test_a_cardiology_doctor_cannot_open_an_oncology_inpatient(staff, reg_on):
    pid = _inpatient(ONCO)
    for suffix in ("", "/changes", "/card"):
        resp = client.get(f"/api/registration/patients/{pid}{suffix}", headers=H(staff, "doc_cardio"))
        assert resp.status_code == 404, (suffix, resp.text)
    patch = client.patch(f"/api/registration/patients/{pid}",
                         json={"row_version": 0, "reason": "Card shows a new city", "city": "Hubli"},
                         headers=H(staff, "doc_cardio"))
    assert patch.status_code == 404
    assert _audit_rows("patient_scope_denied", pid)
    assert _record(pid, staff, "doc_onco").status_code == 200


def test_a_pure_outpatient_is_closed_to_a_clinic_that_never_saw_them(staff, reg_on):
    patient, _ = _register(staff, "recep")
    pid = patient["patient_id"]
    assert _record(pid, staff, "doc_cardio").status_code == 404
    assert _record(pid, staff, "recep_cardio").status_code == 404
    patch = client.patch(f"/api/registration/patients/{pid}",
                         json={"row_version": 1, "reason": "Card shows a new city", "city": "Hubli"},
                         headers=H(staff, "doc_cardio"))
    assert patch.status_code == 404


def test_a_no_department_receptionist_reads_hospital_wide(staff, reg_on):
    inpatient = _inpatient(ONCO)
    outpatient, _ = _register(staff, "recep")
    for pid in (inpatient, outpatient["patient_id"]):
        resp = _record(pid, staff, "recep")
        assert resp.status_code == 200, resp.text
        assert resp.json()["access"]["arm"] == "hospital_wide"
    rows = _audit_rows("registration_record_viewed", inpatient)
    assert rows and rows[-1]["event_category"] == "phi_access"


def test_a_clinic_visit_opens_the_outpatient_to_that_clinic_only_with_opd_on(staff, opd_on):
    patient, _ = _register(staff, "recep")
    pid = patient["patient_id"]
    _visit(pid, CARDIO)
    resp = _record(pid, staff, "doc_cardio")
    assert resp.status_code == 200, resp.text
    assert resp.json()["access"]["arm"] == "opd_visit"
    assert _record(pid, staff, "doc_onco").status_code == 404


def test_a_reader_let_in_by_a_visit_sees_no_dob_contact_or_address(staff, opd_on):
    patient, body = _register(staff, "recep", id_document_type="aadhaar", id_document_last4="4321",
                              email="kept@example.com", guardian_name="Hidden Guardian",
                              abha_number="".join(random.choice(string.digits) for _ in range(14)))
    pid = patient["patient_id"]
    # The central desk corrects a city, so the change log has a hidden value.
    fixed = client.patch(f"/api/registration/patients/{pid}", headers=H(staff, "recep"), json={
        "row_version": 1, "reason": "Patient moved to a new city", "city": "Belagavi"})
    assert fixed.status_code == 200, fixed.text
    _visit(pid, CARDIO)
    resp = _record(pid, staff, "doc_cardio")
    assert resp.status_code == 200, resp.text
    rec = resp.json()
    assert rec["access"]["arm"] == "opd_visit"
    for field in ("date_of_birth", "phone", "address", "emergency_contact_name"):
        assert rec[field] is None
    assert rec["phone_last4"] == body["phone"][-4:]
    assert rec["abha"] == [] and rec["abha_on_file"] is True
    assert all(rec["demographics"][f] is None for f in
               ("address_line", "city", "email", "guardian_name", "id_document_last4"))
    for secret in (body["date_of_birth"], body["phone"], "Hidden Road", "kept@example.com",
                   "Hidden Guardian", "4321", "Belagavi"):
        assert secret not in resp.text
    changes = client.get(f"/api/registration/patients/{pid}/changes", headers=H(staff, "doc_cardio"))
    assert changes.status_code == 200
    row = changes.json()["changes"][0]
    assert row["field"] == "city" and row["values_hidden"] is True and row["new_value"] is None
    assert "Belagavi" not in changes.text and "Mysuru" not in changes.text
    # The central desk still sees everything.
    full = _record(pid, staff, "recep").json()
    assert full["date_of_birth"] == body["date_of_birth"] and full["demographics"]["city"] == "Belagavi"


def test_through_a_visit_hidden_details_cannot_be_corrected(staff, opd_on):
    patient, _ = _register(staff, "recep")
    pid = patient["patient_id"]
    _visit(pid, CARDIO)
    url = f"/api/registration/patients/{pid}"
    refused = client.patch(url, headers=H(staff, "doc_cardio"), json={
        "row_version": 1, "reason": "Patient gave a new phone", "phone": _phone()})
    assert refused.status_code == 403
    assert refused.json()["detail"]["error"] == "needs_full_scope"
    ok = client.patch(url, headers=H(staff, "doc_cardio"), json={
        "row_version": 1, "reason": "Patient gave an occupation", "occupation": "Weaver"})
    assert ok.status_code == 200, ok.text
    assert ok.json()["patient"]["date_of_birth"] is None
    assert ok.json()["patient"]["demographics"]["occupation"] == "Weaver"


def test_without_opd_a_visit_opens_nothing(staff, reg_on):
    patient, _ = _register(staff, "recep")
    _visit(patient["patient_id"], CARDIO)
    assert _record(patient["patient_id"], staff, "doc_cardio").status_code == 404


def test_the_nurse_has_no_registration_read(staff, reg_on):
    pid = _inpatient(CARDIO)
    assert _record(pid, staff, "nurse_cardio").status_code == 403


def test_the_card_and_changes_reads_are_audited(staff, reg_on):
    patient, body = _register(staff, "recep")
    pid = patient["patient_id"]
    card = client.get(f"/api/registration/patients/{pid}/card", headers=H(staff, "recep"))
    assert card.status_code == 200 and body["date_of_birth"] not in card.text
    changes = client.get(f"/api/registration/patients/{pid}/changes", headers=H(staff, "recep"))
    assert changes.status_code == 200 and changes.json()["changes"] == []
    assert _audit_rows("uhid_card_printed", pid) and _audit_rows("registration_changes_viewed", pid)


# ═════════════════════════════════════════════════════════════════════════════
# 4. The minimal-field search
# ═════════════════════════════════════════════════════════════════════════════

def test_search_returns_no_dob_address_or_full_phone_and_is_audited(staff, reg_on):
    patient, body = _register(staff, "recep")
    resp = client.get("/api/registration/search", params={"q": body["name"]},
                      headers=H(staff, "nurse_cardio"))
    assert resp.status_code == 200, resp.text
    hits = [r for r in resp.json()["results"] if r["patient_id"] == patient["patient_id"]]
    assert hits and set(hits[0]) == {"patient_id", "uhid", "name", "sex", "age", "age_stated",
                                     "phone_last4", "last_visit_date", "inpatient_now"}
    for secret in (body["date_of_birth"], "Hidden Road", body["phone"]):
        assert secret not in resp.text
    rows = _audit_rows("patient_index_searched")
    meta = json.loads(rows[-1]["metadata_json"])
    assert patient["patient_id"] in meta["matched_patient_ids"]
    assert rows[-1]["event_category"] == "phi_access"


def test_search_shows_an_inpatient_as_inpatient_now(staff, reg_on):
    pid = _inpatient(ONCO)
    with db_session() as db:
        mrn = db.execute("SELECT mrn FROM patients WHERE id = ?", (pid,)).fetchone()[0]
    resp = client.get("/api/registration/search", params={"q": mrn}, headers=H(staff, "doc_cardio"))
    assert resp.json()["results"][0]["inpatient_now"] is True
    assert "Ward Lane" not in resp.text and "1971" not in resp.text


def test_a_short_search_is_refused(staff, reg_on):
    resp = client.get("/api/registration/search", params={"q": "ab"}, headers=H(staff, "recep"))
    assert resp.status_code == 422 and resp.json()["detail"]["error"] == "query_too_short"


# ═════════════════════════════════════════════════════════════════════════════
# 5. Identity check, corrections
# ═════════════════════════════════════════════════════════════════════════════

def test_the_identity_check_never_echoes_the_stored_dob(staff, reg_on):
    patient, body = _register(staff, "recep", date_of_birth="1958-02-11")
    checker = _mk_user("receptionist")
    hdr = _hdr(checker, "receptionist")
    ok = client.post("/api/registration/identity-check", headers=hdr, json={
        "patient_id": patient["patient_id"], "name": body["name"], "date_of_birth": "1958-02-11",
        "phone_last4": body["phone"][-4:]})
    bad = client.post("/api/registration/identity-check", headers=hdr, json={
        "patient_id": patient["patient_id"], "name": body["name"], "date_of_birth": "1958-02-12",
        "phone_last4": body["phone"][-4:]})
    assert ok.status_code == 200 and ok.json()["result"] == "match"
    assert bad.status_code == 200 and bad.json()["result"] == "no_match"
    for resp in (ok, bad):
        assert "1958-02-11" not in resp.text and set(resp.json()) == {"result", "checked", "checked_at"}


def test_the_sixth_failed_identity_check_in_an_hour_is_429(staff, reg_on):
    patient, body = _register(staff, "recep")
    checker = _mk_user("doctor", CARDIO)
    hdr = _hdr(checker, "doctor")
    wrong = {"patient_id": patient["patient_id"], "name": body["name"], "age_years": 1,
             "uhid": patient["uhid"]}
    for _ in range(5):
        assert client.post("/api/registration/identity-check", headers=hdr,
                           json=wrong).json()["result"] == "no_match"
    sixth = client.post("/api/registration/identity-check", headers=hdr, json={
        **wrong, "age_years": None, "date_of_birth": body["date_of_birth"]})
    assert sixth.status_code == 429
    assert sixth.json()["detail"]["error"] == "identity_check_locked"


def test_a_ward_cannot_probe_another_wards_inpatient(staff, reg_on):
    pid = _inpatient(ONCO)
    with db_session() as db:
        row = db.execute("SELECT name, mrn FROM patients WHERE id = ?", (pid,)).fetchone()
    body = {"patient_id": pid, "name": row["name"], "date_of_birth": "1971-01-01",
            "uhid": row["mrn"]}
    cardio = client.post("/api/registration/identity-check", headers=H(staff, "doc_cardio"), json=body)
    assert cardio.status_code == 404
    assert _audit_rows("patient_scope_denied", pid)
    onco = client.post("/api/registration/identity-check", headers=H(staff, "doc_onco"), json=body)
    assert onco.status_code == 200 and onco.json()["result"] == "match"
    central = client.post("/api/registration/identity-check", headers=H(staff, "recep"), json=body)
    assert central.status_code == 200 and central.json()["result"] == "match"
    # A registered outpatient is checkable by any ward: that is what the check is for.
    patient, reg_body = _register(staff, "recep")
    out = client.post("/api/registration/identity-check", headers=H(staff, "doc_cardio"), json={
        "patient_id": patient["patient_id"], "name": reg_body["name"],
        "date_of_birth": reg_body["date_of_birth"], "uhid": patient["uhid"]})
    assert out.status_code == 200 and out.json()["result"] == "match"


# ── A date of birth the caller may not read (review round 1) ────────────────
# The stored date of birth is the discharge-link second factor that the
# identity check rate-limits (ruling OPD-S1). A department-scoped caller must
# not be able to tell a right guess from a wrong one through POST /match or a
# refused POST /patients for a patient outside their department.

def _born_away_from_today(years):
    """A date of birth whose day before and day after give the same age today,
    whatever day the suite runs on."""
    from datetime import date, timedelta
    mid = hospital_today() - timedelta(days=182)
    return date(mid.year - years, mid.month, min(mid.day, 28))


def _guesses(dob):
    from datetime import timedelta
    return [(dob + timedelta(days=d)).isoformat() for d in (-1, 0, 1)]


def _inpatient_born(dept, dob):
    pid = _inpatient(dept)
    with db_session() as db:
        db.execute("UPDATE patients SET date_of_birth = ? WHERE id = ?", (dob.isoformat(), pid))
        row = db.execute("SELECT name, mrn, phone FROM patients WHERE id = ?", (pid,)).fetchone()
    return pid, dict(row)


def _match_entry(staff, who, body, pid):
    resp = client.post("/api/registration/match", headers=H(staff, who), json=body)
    assert resp.status_code == 200, resp.text
    return [c for c in resp.json()["candidates"] if c["patient_id"] == pid]


def test_a_scoped_caller_cannot_tell_a_right_date_of_birth_guess_through_the_match(staff, reg_on):
    dob = _born_away_from_today(52)
    pid, row = _inpatient_born(ONCO, dob)
    assert _record(pid, staff, "doc_cardio").status_code == 404
    probes = ({"uhid": row["mrn"]}, {"name": row["name"]}, {"phone": row["phone"]},
              {"uhid": row["mrn"], "name": row["name"], "phone": row["phone"]})
    for who in ("nurse_cardio", "doc_cardio", "recep_cardio"):
        for probe in probes:
            answers = [_match_entry(staff, who, {**probe, "date_of_birth": g}, pid)
                       for g in _guesses(dob)]
            assert answers[0] and answers[0] == answers[1] == answers[2], (who, probe)
            assert all("dob_exact" not in c["reasons"] for c in answers[1]), (who, probe)
        # The reproduction: the UHID and a guess came back ['uhid', 'dob_exact']
        # only for the right date.
        assert _match_entry(staff, who, {"uhid": row["mrn"], "date_of_birth": dob.isoformat()},
                            pid)[0]["reasons"] == ["uhid"]
    # Oncology, and a desk with no department, read the record, so they compare it.
    for who in ("doc_onco", "recep"):
        right = _match_entry(staff, who, {"uhid": row["mrn"], "date_of_birth": dob.isoformat()}, pid)
        wrong = _match_entry(staff, who, {"uhid": row["mrn"], "date_of_birth": _guesses(dob)[0]}, pid)
        assert right[0]["reasons"] == ["uhid", "dob_exact"] and wrong[0]["reasons"] == ["uhid"]


def test_a_clinic_visit_does_not_open_the_date_of_birth_to_the_match(staff, opd_on):
    dob = _born_away_from_today(41)
    patient, body = _register(staff, "recep", date_of_birth=dob.isoformat())
    _visit(patient["patient_id"], CARDIO)
    answers = [_match_entry(staff, "doc_cardio", {"name": body["name"], "date_of_birth": g},
                            patient["patient_id"]) for g in _guesses(dob)]
    assert answers[0] == answers[1] == answers[2]
    assert answers[0][0]["reasons"] == ["name_exact", "dob_hidden"]
    assert answers[0][0]["band"] == "probable"
    assert answers[0][0]["reason_labels"][1] == ("Same age, date of birth not shown outside "
                                                 "your department")


@pytest.mark.parametrize("mode, code", [("warn", "duplicate_review_required"),
                                        ("block", "probable_duplicate")])
def test_a_refused_registration_answers_every_date_of_birth_guess_alike(staff, reg_on,
                                                                      monkeypatch, mode, code):
    monkeypatch.setattr(config, "DUPLICATE_MODE", mode)
    dob = _born_away_from_today(47)
    pid, row = _inpatient_born(ONCO, dob)
    outcomes = []
    for guess in _guesses(dob):
        resp = client.post("/api/registration/patients", headers=H(staff, "doc_cardio"),
                           json=_body(name=row["name"], sex="F", date_of_birth=guess))
        assert resp.status_code == 409, resp.text
        detail = resp.json()["detail"]
        outcomes.append((detail["error"], detail["message"],
                         [c for c in detail["candidates"] if c["patient_id"] == pid]))
    assert outcomes[0] == outcomes[1] == outcomes[2]
    # Never weaker: the true duplicate is still probable, so block still refuses it.
    assert outcomes[0][0] == code
    assert outcomes[0][2][0]["band"] == "probable"
    assert outcomes[0][2][0]["reasons"] == ["name_exact", "dob_hidden"]


def test_the_scoped_preview_and_the_registration_agree(staff, reg_on):
    dob = _born_away_from_today(38)
    pid, row = _inpatient_born(ONCO, dob)
    body = _body(name=row["name"], sex="F", date_of_birth=_guesses(dob)[2])
    preview = client.post("/api/registration/match", headers=H(staff, "doc_cardio"), json={
        k: body[k] for k in ("name", "sex", "date_of_birth", "phone", "uhid")}).json()
    ids = [c["patient_id"] for c in preview["candidates"]]
    assert pid in ids and preview["review_required"] is True
    resp = client.post("/api/registration/patients", headers=H(staff, "doc_cardio"), json={
        **body, "duplicate_review": {"decision": "register_new", "candidate_ids": ids,
                                     "reason": "Checked at the desk: a different person"}})
    assert resp.status_code == 201, resp.text


def test_the_nurse_cannot_run_the_identity_check(staff, reg_on):
    patient, body = _register(staff, "recep")
    resp = client.post("/api/registration/identity-check", headers=H(staff, "nurse_cardio"), json={
        "patient_id": patient["patient_id"], "name": body["name"], "date_of_birth": body["date_of_birth"],
        "uhid": patient["uhid"]})
    assert resp.status_code == 403


def test_a_correction_without_a_reason_is_422_and_a_stale_one_409(staff, reg_on):
    patient, _ = _register(staff, "recep")
    url = f"/api/registration/patients/{patient['patient_id']}"
    no_reason = client.patch(url, json={"row_version": 1, "city": "Hubli"}, headers=H(staff, "recep"))
    assert no_reason.status_code == 422 and no_reason.json()["detail"]["error"] == "reason_required"
    ok = client.patch(url, json={"row_version": 1, "reason": "Patient moved house",
                                 "city": "Hubli"}, headers=H(staff, "recep"))
    assert ok.status_code == 200, ok.text
    assert ok.json()["patient"]["row_version"] == 2
    stale = client.patch(url, json={"row_version": 1, "reason": "Patient moved again",
                                    "city": "Dharwad"}, headers=H(staff, "recep"))
    assert stale.status_code == 409 and stale.json()["detail"]["error"] == "stale_row_version"


def test_a_correction_never_leaves_neither_a_date_of_birth_nor_a_stated_age(staff, reg_on):
    """The reviewer's probes: removing a date of birth alone, and clearing a
    stated age, are both refused; removing a date of birth with the stated age
    records that age with today's date."""
    patient, _ = _register(staff, "recep", date_of_birth="1981-03-04")
    url = f"/api/registration/patients/{patient['patient_id']}"
    alone = client.patch(url, headers=H(staff, "recep"), json={
        "row_version": 1, "reason": "Date of birth was a guess", "date_of_birth": None})
    assert alone.status_code == 422, alone.text
    assert alone.json()["detail"]["error"] == "age_or_date_of_birth_required"
    record = client.get(url, headers=H(staff, "recep")).json()
    assert record["date_of_birth"] == "1981-03-04" and record["row_version"] == 1

    swapped = client.patch(url, headers=H(staff, "recep"), json={
        "row_version": 1, "reason": "Date of birth was a guess", "date_of_birth": None,
        "age_years": 44})
    assert swapped.status_code == 200, swapped.text
    out = swapped.json()["patient"]
    assert out["date_of_birth"] is None and out["age"] == 44 and out["age_stated"] is True
    assert out["age_recorded_on"] == hospital_today().isoformat()

    stated, body = _register(staff, "recep", date_of_birth=None, age_years=40)
    url = f"/api/registration/patients/{stated['patient_id']}"
    cleared = client.patch(url, headers=H(staff, "recep"), json={
        "row_version": 1, "reason": "Age was not given", "age_years": None})
    assert cleared.status_code == 422, cleared.text
    assert cleared.json()["detail"]["error"] == "age_or_date_of_birth_required"
    record = client.get(url, headers=H(staff, "recep")).json()
    assert record["age"] == 40 and record["date_of_birth"] is None


def test_the_uhid_correction_is_the_administrators(staff, reg_on):
    patient, _ = _register(staff, "recep")
    url = f"/api/registration/patients/{patient['patient_id']}"
    new = f"RGR{uuid.uuid4().hex[:8].upper()}"
    by_desk = client.patch(url, json={"row_version": 1, "reason": "Typed the wrong number",
                                      "uhid": new}, headers=H(staff, "recep"))
    assert by_desk.status_code == 403
    assert by_desk.json()["detail"]["error"] == "uhid_correction_admin_only"
    denials = [r for r in _audit_rows("permission_denied", patient["patient_id"])]
    assert denials and denials[-1]["event_category"] == "authorization"
    by_admin_fields = client.patch(url, json={"row_version": 1, "reason": "Typed the wrong city",
                                              "city": "Hubli"}, headers=H(staff, "admin"))
    assert by_admin_fields.status_code == 403
    by_admin = client.patch(url, json={"row_version": 1, "reason": "Typed the wrong number",
                                       "uhid": new}, headers=H(staff, "admin"))
    assert by_admin.status_code == 200, by_admin.text
    assert by_admin.json()["patient"]["uhid"] == new
    assert _audit_rows("patient_uhid_corrected", patient["patient_id"])


def test_the_scope_drift_guard_still_has_no_exemptions():
    import inspect
    import tests.test_patient_scope_routes as guard
    src = inspect.getsource(guard.test_every_patient_id_route_that_returns_phi_carries_the_guard)
    assert "exempt: set[str] = set()" in src
