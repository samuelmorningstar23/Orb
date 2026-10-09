"""The admission doors join the patient index (opd-5b, W6-B).

Always on: a hospital number (MRN) already on another record is refused at the
admission form with 409 'mrn_in_use', naming nobody, instead of the UNIQUE
constraint's raw 500.

With ORB_REGISTRATION on, both doors (POST /api/admissions/admit and Scribe's
POST /api/scribe/quick-admit) behave as the registration desk does:
  * hospital mode needs the typed number (422 uhid_required), never a random
    one; Orb mode issues it from the UHID series and refuses a typed one;
  * the patient index is searched and the desk's duplicate rule applied
    (ruling OPD-S2, built strictest): the same number is a hard refusal in
    every mode, 'block' refuses a probable duplicate with no override, and a
    reviewable match needs a duplicate_review naming every candidate with a
    reason, audited under patient_identity and put on the worklist;
  * a refusal shows minimal index cards (no date of birth) and is audited
    fail-closed on its own write (503 and nobody shown when it cannot be).
With registration off the doors are exactly as before, except the 409.
"""
from __future__ import annotations

import json
import random
import string
import uuid

import pytest
from fastapi.testclient import TestClient

import config
from auth import create_token
from database import db_session
from main import app
from services import admission_service as adm
from services.audit_service import AuditWriteError

client = TestClient(app)
_SEEDED: list[str] = []
DEPT = "General Medicine"


@pytest.fixture(autouse=True)
def _discharge_seeded():
    """Close what this module admits, so no later module counts it."""
    yield
    if not _SEEDED:
        return
    with db_session() as db:
        db.executemany("UPDATE patients SET status = 'discharged' WHERE id = ?",
                       [(p,) for p in _SEEDED])
        db.executemany("UPDATE admissions SET status = 'discharged' "
                       "WHERE patient_id = ? AND status = 'active'", [(p,) for p in _SEEDED])
    _SEEDED.clear()


@pytest.fixture
def registration_on(monkeypatch):
    monkeypatch.setattr(config, "REGISTRATION_ENABLED", True)
    monkeypatch.setattr(config, "UHID_SOURCE", "hospital")
    monkeypatch.setattr(config, "UHID_PATTERN", None)
    monkeypatch.setattr(config, "DUPLICATE_MODE", "block")


@pytest.fixture
def warn_mode(registration_on, monkeypatch):
    monkeypatch.setattr(config, "DUPLICATE_MODE", "warn")


@pytest.fixture
def orb_mode(registration_on, monkeypatch):
    monkeypatch.setattr(config, "UHID_SOURCE", "orb")
    with db_session() as db:
        before = dict(db.execute(
            "SELECT * FROM document_number_series WHERE series_code = 'UHID'").fetchone())
    yield before
    with db_session() as db:
        db.execute("UPDATE document_number_series SET prefix = ?, is_configured = ?, "
                   "pad_width = ? WHERE series_code = 'UHID'",
                   (before["prefix"], before["is_configured"], before["pad_width"]))


def _doctor(department=DEPT):
    uid = f"AIM{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute("INSERT INTO users (id, name, role, password_hash, department, is_active) "
                   "VALUES (?, ?, 'doctor', 'x', ?, 1)", (uid, f"Dr Index {uid[-4:]}", department))
    return uid


def _hdr(uid, role="doctor"):
    return {"Authorization": f"Bearer {create_token(uid, role)}"}


def _name():
    """Letters only and random, so no other module's patient is a candidate."""
    def word():
        return "Z" + "".join(random.choice("bcdfgjklmnprtvz") + random.choice("aeiou")
                             for _ in range(3))
    return f"{word()} {word()}"


def _phone():
    return "9" + "".join(random.choice(string.digits) for _ in range(9))


def _uhid():
    return f"AIM-{uuid.uuid4().hex[:8].upper()}"


def _form(attending, **over):
    body = {"name": _name(), "age": 47, "sex": "F", "phone": _phone(),
            "admission_source": "er", "admission_type": "emergency", "department": DEPT,
            "ward": "AIM-W", "bed": f"B{random.randint(100, 999)}",
            "attending_doctor_id": attending, "chief_complaint": "Fever"}
    body.update(over)
    return body


def _admit(doc, body):
    resp = client.post("/api/admissions/admit", json=body, headers=_hdr(doc))
    if resp.status_code in (200, 201):
        _SEEDED.append(resp.json()["patient_id"])
    return resp


def _patients_named(name):
    with db_session() as db:
        return [dict(r) for r in db.execute(
            "SELECT id, mrn, status FROM patients WHERE name = ?", (name,)).fetchall()]


def _existing(**over):
    """A record already on the index: a registered outpatient (no admission)."""
    pid = f"PAT-AIM{uuid.uuid4().hex[:8].upper()}"
    row = {"name": _name(), "mrn": _uhid(), "phone": _phone(), "sex": "F",
           "date_of_birth": f"19{random.randint(40, 99)}-0{random.randint(1, 9)}-1{random.randint(0, 9)}",
           "age": None}
    row.update(over)
    with db_session() as db:
        db.execute("INSERT INTO patients (id, name, mrn, phone, sex, date_of_birth, age, status) "
                   "VALUES (?, ?, ?, ?, ?, ?, ?, 'registered')",
                   (pid, row["name"], row["mrn"], row["phone"], row["sex"],
                    row["date_of_birth"], row["age"]))
    return {"patient_id": pid, **row}


def _audits(action, since_id=0):
    with db_session() as db:
        return [dict(r) for r in db.execute(
            "SELECT * FROM audit_logs WHERE action_type = ? AND id > ? ORDER BY id",
            (action, since_id)).fetchall()]


def _last_audit_id():
    with db_session() as db:
        return db.execute("SELECT COALESCE(MAX(id), 0) FROM audit_logs").fetchone()[0]


# ═════════════════════════════════════════════════════════════════════════════
#  1. Always on: a duplicate MRN is 409 naming nobody, never 500
# ═════════════════════════════════════════════════════════════════════════════

def test_a_typed_mrn_already_on_a_record_is_409_naming_nobody_with_every_switch_off():
    assert config.feature_enabled("registration") is False
    doc = _doctor()
    first = _admit(doc, _form(doc, mrn=_uhid()))
    assert first.status_code == 200, first.text
    taken = first.json()["mrn"]
    other = _form(doc, mrn=taken)
    resp = _admit(doc, other)
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["error"] == "mrn_in_use"
    assert set(detail) == {"error", "message"}
    text = json.dumps(detail)
    assert first.json()["patient_id"] not in text and taken not in text
    assert _patients_named(other["name"]) == []


def test_the_unique_constraint_race_is_the_same_409(monkeypatch):
    """Two admissions with one typed number can both pass the check; the
    UNIQUE constraint refuses the second and it answers as the check does."""
    from db_backend import DBIntegrityError

    # The alias is the tuple of every backend's IntegrityError; raise one.
    integrity = DBIntegrityError[0] if isinstance(DBIntegrityError, tuple) else DBIntegrityError

    def racing(*a, **k):
        raise integrity("UNIQUE constraint failed: patients.mrn")

    monkeypatch.setattr(adm, "_admit", racing)
    with pytest.raises(adm.AdmissionRefused) as caught:
        adm.admit_patient({"name": "Race Case"}, {"id": "D001", "role": "doctor"})
    assert caught.value.code == "mrn_in_use" and caught.value.status == 409

    def other_integrity(*a, **k):
        raise integrity("FOREIGN KEY constraint failed")

    monkeypatch.setattr(adm, "_admit", other_integrity)
    with pytest.raises(DBIntegrityError):
        adm.admit_patient({"name": "Race Case"}, {"id": "D001", "role": "doctor"})


def test_switches_off_the_doors_are_as_before():
    """No MRN still mints one, a duplicate_review is ignored, nothing about
    the patient index is written or answered."""
    doc = _doctor()
    mark = _last_audit_id()
    body = _form(doc, duplicate_review={"decision": "admit_new", "candidate_ids": [],
                                        "reason": "ignored while off"})
    resp = _admit(doc, body)
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert out["mrn"].startswith("MRN-") and "index_check" not in out
    with db_session() as db:
        keys = db.execute("SELECT 1 FROM patient_match_keys WHERE patient_id = ?",
                          (out["patient_id"],)).fetchone()
    assert keys is None
    assert _audits("admission_identity_checked", mark) == []

    quick = client.post("/api/scribe/quick-admit", headers=_hdr(doc), json={
        "name": _name(), "age": 40, "sex": "M", "department": DEPT, "ward": "AIM-Q",
        "attending_doctor_id": doc, "chief_complaint": "Cough", "mrn": _uhid()})
    assert quick.status_code == 200, quick.text
    _SEEDED.append(quick.json()["patient_id"])
    assert quick.json()["mrn"].startswith("MRN-")   # the quick door mints, as it did


# ═════════════════════════════════════════════════════════════════════════════
#  2. Registration on: the hospital number follows ORB_UHID_SOURCE
# ═════════════════════════════════════════════════════════════════════════════

def test_hospital_mode_needs_the_typed_number_and_never_mints_one(registration_on):
    doc = _doctor()
    body = _form(doc)
    resp = _admit(doc, body)
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"]["error"] == "uhid_required"
    assert _patients_named(body["name"]) == []

    quick = client.post("/api/scribe/quick-admit", headers=_hdr(doc), json={
        "name": body["name"], "age": 40, "sex": "F", "department": DEPT, "ward": "AIM-Q",
        "attending_doctor_id": doc, "chief_complaint": "Cough"})
    assert quick.status_code == 422 and quick.json()["detail"]["error"] == "uhid_required"
    assert _patients_named(body["name"]) == []


def test_hospital_mode_keeps_the_typed_number_and_the_site_format(registration_on, monkeypatch):
    doc = _doctor()
    monkeypatch.setattr(config, "UHID_PATTERN", r"AIM-[0-9A-F]{8}")
    bad = _form(doc, mrn="12345")
    resp = _admit(doc, bad)
    assert resp.status_code == 422 and resp.json()["detail"]["error"] == "uhid_format"
    good = _form(doc, mrn=_uhid())
    resp = _admit(doc, good)
    assert resp.status_code == 200, resp.text
    assert resp.json()["mrn"] == good["mrn"]
    assert resp.json()["index_check"] == {"uhid_source": "hospital", "duplicate_review": None}
    monkeypatch.setattr(config, "UHID_PATTERN", "([unclosed")
    resp = _admit(doc, _form(doc, mrn=_uhid()))
    assert resp.status_code == 409 and resp.json()["detail"]["error"] == "uhid_pattern_invalid"


def test_orb_mode_refuses_a_typed_number_and_waits_for_the_series(orb_mode):
    doc = _doctor()
    resp = _admit(doc, _form(doc, mrn=_uhid()))
    assert resp.status_code == 422 and resp.json()["detail"]["error"] == "uhid_assigned_by_orb"
    with db_session() as db:
        db.execute("UPDATE document_number_series SET is_configured = 0 "
                   "WHERE series_code = 'UHID'")
    body = _form(doc)
    resp = _admit(doc, body)
    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "uhid_series_not_configured"
    assert _patients_named(body["name"]) == []


def test_orb_mode_issues_the_number_from_the_series(orb_mode):
    from services.billing_masters import verhoeff_valid

    doc = _doctor()
    with db_session() as db:
        db.execute("UPDATE document_number_series SET prefix = 'AIM', is_configured = 1, "
                   "pad_width = 6 WHERE series_code = 'UHID'")
    resp = _admit(doc, _form(doc))
    assert resp.status_code == 200, resp.text
    mrn = resp.json()["mrn"]
    assert mrn.startswith("AIM") and verhoeff_valid(mrn[len("AIM"):])
    assert resp.json()["index_check"]["uhid_source"] == "orb"


# ═════════════════════════════════════════════════════════════════════════════
#  3. Registration on: the patient index
# ═════════════════════════════════════════════════════════════════════════════

def test_a_registered_outpatient_re_entered_at_the_admit_door_gets_409_not_a_second_chart(
        registration_on):
    from services import registration_service as reg

    desk = f"AIMR{uuid.uuid4().hex[:6].upper()}"
    with db_session() as db:
        db.execute("INSERT INTO users (id, name, role, password_hash, is_active) "
                   "VALUES (?, 'Desk', 'receptionist', 'x', 1)", (desk,))
    name, uhid, phone = _name(), _uhid(), _phone()
    registered = reg.register_patient(
        {"name": name, "sex": "F", "date_of_birth": "1971-03-04", "phone": phone, "uhid": uhid},
        user={"id": desk, "role": "receptionist"})
    pid = registered["patient"]["patient_id"]
    doc = _doctor()
    mark = _last_audit_id()
    body = _form(doc, name=name, mrn=uhid, phone=phone, date_of_birth="1971-03-04")
    resp = _admit(doc, body)
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["error"] == "possible_existing_patient"
    assert detail["reason"] == "existing_record"
    assert detail["override_allowed"] is False and detail["admit_existing_available"] is False
    cards = detail["candidates"]
    assert [c["patient_id"] for c in cards] == [pid]
    assert set(cards[0]) >= {"uhid", "name", "phone_last4", "band", "reasons"}
    assert "date_of_birth" not in cards[0] and "phone" not in cards[0]
    assert len(_patients_named(name)) == 1
    refused = _audits("admission_index_refused", mark)
    assert len(refused) == 1 and refused[0]["event_category"] == "patient_identity"
    meta = json.loads(refused[0]["metadata_json"])
    assert meta["matched_patient_ids"] == [pid] and meta["door"] == "admission_form"

    # A review never gets past the same hospital number.
    body["duplicate_review"] = {"decision": "admit_new", "candidate_ids": [pid],
                                "reason": "Clinician says this is someone else"}
    again = _admit(doc, body)
    assert again.status_code == 409 and again.json()["detail"]["reason"] == "existing_record"
    assert len(_patients_named(name)) == 1


def test_a_normalised_hospital_number_is_the_same_number(registration_on):
    existing = _existing(mrn="AIM 77/1234")
    doc = _doctor()
    resp = _admit(doc, _form(doc, mrn="aim-77-1234"))
    assert resp.status_code == 409
    assert resp.json()["detail"]["reason"] == "existing_record"
    assert resp.json()["detail"]["candidates"][0]["patient_id"] == existing["patient_id"]


def test_block_mode_refuses_a_probable_duplicate_with_no_override(registration_on):
    existing = _existing()
    doc = _doctor()
    body = _form(doc, name=existing["name"], phone=existing["phone"], mrn=_uhid(),
                 date_of_birth=existing["date_of_birth"])
    resp = _admit(doc, body)
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert (detail["reason"], detail["mode"], detail["override_allowed"]) == (
        "probable_duplicate", "block", False)
    assert detail["candidates"][0]["patient_id"] == existing["patient_id"]
    body["duplicate_review"] = {"decision": "admit_new",
                                "candidate_ids": [existing["patient_id"]],
                                "reason": "A different person, checked the papers"}
    assert _admit(doc, body).status_code == 409
    assert len(_patients_named(existing["name"])) == 1


def test_warn_mode_admits_past_a_probable_duplicate_only_with_a_reviewed_reason(warn_mode):
    existing = _existing()
    doc = _doctor()
    body = _form(doc, name=existing["name"], phone=existing["phone"], mrn=_uhid())
    resp = _admit(doc, body)
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["reason"] == "duplicate_review_required" and detail["override_allowed"] is True
    listed = [c["patient_id"] for c in detail["candidates"]]
    assert existing["patient_id"] in listed

    body["duplicate_review"] = {"decision": "admit_new", "candidate_ids": listed,
                                "reason": "short"}
    short = _admit(doc, body)
    assert short.status_code == 422
    assert short.json()["detail"]["error"] == "override_reason_required"

    body["duplicate_review"] = {"decision": "admit_new", "candidate_ids": [],
                                "reason": "Different mother's name on the papers"}
    stale = _admit(doc, body)
    assert stale.status_code == 409 and stale.json()["detail"]["reason"] == "duplicate_review_stale"

    mark = _last_audit_id()
    body["duplicate_review"]["candidate_ids"] = listed
    ok = _admit(doc, body)
    assert ok.status_code == 200, ok.text
    out = ok.json()
    assert out["index_check"]["duplicate_review"]["candidate_ids"] == listed
    with db_session() as db:
        pair = sorted((out["patient_id"], existing["patient_id"]))
        work = db.execute(
            "SELECT source, status, overridden_by, override_reason FROM patient_duplicate_candidates "
            "WHERE patient_id = ? AND candidate_patient_id = ?", pair).fetchone()
        keys = db.execute("SELECT name_key, phone_key FROM patient_match_keys WHERE patient_id = ?",
                          (out["patient_id"],)).fetchone()
    assert dict(work) == {"source": "admission", "status": "open", "overridden_by": doc,
                          "override_reason": "Different mother's name on the papers"}
    assert keys is not None and keys["phone_key"]
    override = _audits("admission_duplicate_override", mark)
    assert len(override) == 1 and override[0]["patient_id"] == out["patient_id"]
    assert json.loads(override[0]["metadata_json"])["candidate_ids"] == listed
    checked = _audits("admission_identity_checked", mark)
    assert len(checked) == 1 and checked[0]["event_category"] == "patient_identity"


def test_a_new_patient_is_admitted_keyed_and_recorded(registration_on):
    doc = _doctor()
    mark = _last_audit_id()
    body = _form(doc, mrn=_uhid(), date_of_birth="1988-08-08")
    resp = _admit(doc, body)
    assert resp.status_code == 200, resp.text
    pid = resp.json()["patient_id"]
    with db_session() as db:
        keys = db.execute("SELECT dob, sex FROM patient_match_keys WHERE patient_id = ?",
                          (pid,)).fetchone()
    assert dict(keys) == {"dob": "1988-08-08", "sex": "F"}
    checked = _audits("admission_identity_checked", mark)
    assert [a["patient_id"] for a in checked] == [pid]
    meta = json.loads(checked[0]["metadata_json"])
    assert meta["uhid_source"] == "hospital" and meta["door"] == "admission_form"


def test_scribe_quick_admit_behaves_the_same(registration_on):
    existing = _existing()
    doc = _doctor()
    resp = client.post("/api/scribe/quick-admit", headers=_hdr(doc), json={
        "name": _name(), "age": 50, "sex": "F", "department": DEPT, "ward": "AIM-Q",
        "attending_doctor_id": doc, "chief_complaint": "Cough", "mrn": existing["mrn"]})
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["error"] == "possible_existing_patient" and detail["reason"] == "existing_record"
    refused = _audits("admission_index_refused")
    assert json.loads(refused[-1]["metadata_json"])["door"] == "scribe_quick_admit"

    fresh = client.post("/api/scribe/quick-admit", headers=_hdr(doc), json={
        "name": _name(), "age": 50, "sex": "F", "department": DEPT, "ward": "AIM-Q",
        "attending_doctor_id": doc, "chief_complaint": "Cough", "mrn": _uhid()})
    assert fresh.status_code == 200, fresh.text
    _SEEDED.append(fresh.json()["patient_id"])
    assert fresh.json()["index_check"]["uhid_source"] == "hospital"


def test_a_refusal_whose_record_cannot_be_written_is_503_and_shows_nobody(
        registration_on, monkeypatch):
    from services import audit_service

    existing = _existing()
    doc = _doctor()
    real = audit_service.log_audit_event

    def failing(*a, **k):
        if k.get("action_type") == "admission_index_refused":
            raise AuditWriteError("audit store unavailable")
        return real(*a, **k)

    monkeypatch.setattr(audit_service, "log_audit_event", failing)
    resp = _admit(doc, _form(doc, mrn=existing["mrn"]))
    assert resp.status_code == 503
    assert existing["patient_id"] not in resp.text and existing["name"] not in resp.text


def test_a_caller_who_may_not_search_the_index_is_told_a_count_not_a_name(registration_on):
    existing = _existing()
    with pytest.raises(adm.AdmissionRefused) as caught:
        adm.admit_patient(
            {"name": _name(), "mrn": existing["mrn"], "department": DEPT, "ward": "AIM-W",
             "bed": "1", "attending_doctor_id": "D001", "chief_complaint": "Fever"},
            {"id": "N001", "name": "Nurse Test", "role": "nurse"})
    detail = caught.value.detail()
    assert detail["candidates"] == [] and detail["candidates_hidden"] is True
    assert detail["candidate_count"] == 1 and detail["override_allowed"] is False


def test_a_bad_review_shape_is_422(registration_on):
    doc = _doctor()
    resp = _admit(doc, _form(doc, mrn=_uhid(), duplicate_review={"decision": "merge"}))
    assert resp.status_code == 422 and resp.json()["detail"]["error"] == "bad_duplicate_review"


def test_with_beds_and_admissions_on_a_replay_is_answered_before_the_index(
        registration_on, monkeypatch):
    """The same Idempotency-Key returns the first admission (its own patient
    would otherwise match itself as the same hospital number)."""
    monkeypatch.setattr(config, "ADT_ENABLED", True)
    doc = _doctor()
    key = f"aim-{uuid.uuid4().hex}"
    body = _form(doc, mrn=_uhid(), ward="", bed="", awaiting_bed=True)
    headers = {**_hdr(doc), "Idempotency-Key": key}
    first = client.post("/api/admissions/admit", json=body, headers=headers)
    assert first.status_code == 201, first.text
    _SEEDED.append(first.json()["patient_id"])
    assert first.json()["index_check"]["uhid_source"] == "hospital"
    again = client.post("/api/admissions/admit", json=body, headers=headers)
    assert again.status_code == 200, again.text
    assert again.json()["replayed"] is True
    assert again.json()["patient_id"] == first.json()["patient_id"]
    assert len(_patients_named(body["name"])) == 1
    other = client.post("/api/admissions/admit", json=body,
                        headers={**_hdr(doc), "Idempotency-Key": f"aim-{uuid.uuid4().hex}"})
    assert other.status_code == 409 and other.json()["detail"]["reason"] == "existing_record"
