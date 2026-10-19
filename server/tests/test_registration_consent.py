"""The privacy notice and consent step at registration (opd-19, W9-G).

What this file pins:

  1. FLAG OFF: with the registration feature off, a registration carrying the
     consent step is 404 like every registration route, and nothing is
     written.
  2. THE CONSENT ROW IS WRITTEN WITH THE REGISTRATION, through consent_service
     (one consent_records row, purpose 'treatment'), in the same transaction:
     the notice version and language the patient was given, the retention
     line (registration details are part of the medical record) as its scope,
     who recorded it, and an in-transaction patient_identity audit row. A
     failed consent write or a failed audit row leaves no patient behind.
  3. THE LANGUAGE is English or an Eighth Schedule language (DPDP s.5(3)); it
     defaults to the patient's preferred language when that is one.
  4. EACH STRICT RULE: both facts must be true (no coercion of "true"), a
     version is required, the portal method is not a desk method, a guardian
     or proxy must be named, and an unknown field is refused.
  5. RULING OPD-N3: the receptionist does not hold consent.record until counsel
     agrees, so her consent step is 403 consent_permission_required before
     anything is written, and audited; without the step she registers as
     before.
  6. WITHDRAWAL: the registration's consent is withdrawn through the existing
     consent route as a new row; the original is never edited. The consent
     route keeps its admission scope (no outpatient arm).
  7. A replay of the same request records no second consent.
  8. WHO GAVE IT FITS HOW AND THE AGE (DPDP s.9; consent rows are immutable,
     so a contradiction is refused, never stored): the guardian method is
     never given by the patient (consent_given_by_mismatch), and a patient
     under 18 by date of birth or stated age never consents for themselves
     (consent_guardian_required). The guardian or proxy is then named.
"""
from __future__ import annotations

import json
import random
import string
import uuid
from datetime import date, datetime, timezone

import pytest
from fastapi.testclient import TestClient

import config
import permissions as P
from auth import create_token
from database import db_session
from main import app
from services import consent_service
from services import registration_service as reg

client = TestClient(app)

CARDIO = "Regconsent Cardiology"


def _mk_user(role, department=None):
    uid = f"RGC{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute("INSERT INTO users (id, name, role, password_hash, department, is_active) "
                   "VALUES (?, ?, ?, 'x', ?, 1)",
                   (uid, f"Regconsent {role} {uid[-4:]}", role, department))
    return uid


@pytest.fixture(scope="module")
def staff():
    return {
        "recep": (_mk_user("receptionist"), "receptionist"),
        "doc_cardio": (_mk_user("doctor", CARDIO), "doctor"),
        "doc_other": (_mk_user("doctor", "Regconsent Neurology"), "doctor"),
    }


def H(staff, who):
    uid, role = staff[who]
    return {"Authorization": f"Bearer {create_token(uid, role)}"}


@pytest.fixture
def reg_on(monkeypatch):
    monkeypatch.setattr(config, "REGISTRATION_ENABLED", True)
    monkeypatch.setattr(config, "UHID_SOURCE", "hospital")
    monkeypatch.setattr(config, "UHID_PATTERN", None)
    monkeypatch.setattr(config, "DUPLICATE_MODE", "warn")
    assert config.feature_enabled("registration")


def _name():
    def word():
        return "Q" + "".join(random.choice("bdfgklmnprstv") + random.choice("aeiou") for _ in range(3))
    return f"{word()} {word()}"


def _body(**kw):
    body = {"name": _name(), "sex": "F",
            "date_of_birth": f"{random.randint(1940, 2004)}-{random.randint(1, 12):02d}-"
                             f"{random.randint(1, 28):02d}",
            "phone": "7" + "".join(random.choice(string.digits) for _ in range(9)),
            "uhid": f"RGC{uuid.uuid4().hex[:8].upper()}"}
    body.update(kw)
    return body


def _consent(**kw):
    step = {"notice_acknowledged": True, "treatment_consent": True,
            "notice_version": "DPN-2026-1", "method": "written_signature"}
    step.update(kw)
    return step


def _consents_for(pid):
    with db_session() as db:
        return [dict(r) for r in db.execute(
            "SELECT * FROM consent_records WHERE patient_id = ? ORDER BY rowid", (pid,)).fetchall()]


def _patients_named(name):
    with db_session() as db:
        return db.execute("SELECT COUNT(*) FROM patients WHERE name = ?", (name,)).fetchone()[0]


def _audit(action, patient_id=None):
    with db_session() as db:
        sql = "SELECT * FROM audit_logs WHERE action_type = ?"
        args = [action]
        if patient_id:
            sql += " AND patient_id = ?"
            args.append(patient_id)
        return [dict(r) for r in db.execute(sql + " ORDER BY id", args).fetchall()]


def _post(staff, who, body, key=None):
    headers = H(staff, who)
    if key:
        headers = {**headers, "Idempotency-Key": key}
    return client.post("/api/registration/patients", json=body, headers=headers)


# ═════════════════════════════════════════════════════════════════════════════
# 1. Flag off
# ═════════════════════════════════════════════════════════════════════════════

def test_flag_off_the_consent_step_is_404_and_writes_nothing(staff):
    assert not config.feature_enabled("registration")
    body = _body(consent=_consent())
    resp = _post(staff, "doc_cardio", body)
    assert resp.status_code == 404, resp.text
    assert resp.json()["detail"]["error"] == "feature_disabled"
    assert _patients_named(body["name"]) == 0


# ═════════════════════════════════════════════════════════════════════════════
# 2. The consent row, in the registration's transaction
# ═════════════════════════════════════════════════════════════════════════════

def test_the_doctor_records_notice_and_consent_with_the_registration(staff, reg_on):
    body = _body(preferred_language="hindi", consent=_consent(evidence_ref="Form 12"))
    resp = _post(staff, "doc_cardio", body)
    assert resp.status_code == 201, resp.text
    out = resp.json()
    pid = out["patient"]["patient_id"]
    rows = _consents_for(pid)
    assert len(rows) == 1
    row = rows[0]
    assert row["purpose"] == "treatment"
    assert row["notice_version"] == "DPN-2026-1"
    # The patient's preferred language, in its listed spelling.
    assert row["notice_language"] == "Hindi"
    assert row["method"] == "written_signature"
    assert row["given_by"] == "patient" and row["proxy_name"] is None
    assert row["evidence_ref"] == "Form 12"
    assert row["recorded_by"] == staff["doc_cardio"][0]
    assert row["recorded_by_role"] == "doctor"
    # The retention line: registration details are part of the medical record.
    assert reg.REGISTRATION_RETENTION_TEXT in row["scope_detail"]
    assert "part of the medical record" in row["scope_detail"]
    # The consent register reads it like any other consent.
    assert consent_service.has_active_consent(pid, "treatment") is True
    # The answer carries what was recorded.
    assert out["consents"] == [{
        "id": row["id"], "purpose": "treatment",
        "purpose_label": consent_service.CONSENT_PURPOSES["treatment"],
        "notice_version": "DPN-2026-1", "notice_language": "Hindi",
        "method": "written_signature",
        "method_label": consent_service.CONSENT_METHODS["written_signature"],
        "given_by": "patient", "granted_at": row["granted_at"], "effective_status": "active"}]
    # Audited fail-closed, in the registration's correlation.
    audits = _audit("consent_recorded", pid)
    assert len(audits) == 1
    audit = audits[0]
    assert audit["event_category"] == "patient_identity"
    assert audit["target_type"] == "consent_record" and audit["target_id"] == row["id"]
    assert audit["correlation_id"] == f"registration-{pid}"
    meta = json.loads(audit["metadata_json"])
    assert meta["notice_acknowledged"] is True and meta["notice_language"] == "Hindi"
    assert meta["lane"] == "registration"
    registered = _audit("patient_registered", pid)
    assert json.loads(registered[-1]["metadata_json"])["consent_id"] == row["id"]
    # The preferred language is stored as typed.
    with db_session() as db:
        assert db.execute("SELECT preferred_language FROM patient_demographics WHERE patient_id = ?",
                          (pid,)).fetchone()[0] == "hindi"


def test_without_the_step_a_registration_writes_no_consent_and_answers_as_before(staff, reg_on):
    resp = _post(staff, "doc_cardio", _body())
    assert resp.status_code == 201, resp.text
    out = resp.json()
    assert "consents" not in out
    assert _consents_for(out["patient"]["patient_id"]) == []


def test_a_named_notice_language_wins_over_the_preferred_one(staff, reg_on):
    resp = _post(staff, "doc_cardio", _body(preferred_language="Tamil",
                                            consent=_consent(notice_language="  english ")))
    assert resp.status_code == 201, resp.text
    assert _consents_for(resp.json()["patient"]["patient_id"])[0]["notice_language"] == "English"


def test_a_guardian_consent_is_recorded_with_the_guardian_named(staff, reg_on):
    resp = _post(staff, "doc_cardio", _body(consent=_consent(
        notice_language="Marathi", method="verbal_witnessed", given_by="guardian",
        proxy_name="Ramesh Patil", proxy_relationship="Father")))
    assert resp.status_code == 201, resp.text
    row = _consents_for(resp.json()["patient"]["patient_id"])[0]
    assert (row["given_by"], row["proxy_name"], row["proxy_relationship"]) == (
        "guardian", "Ramesh Patil", "Father")


def test_a_failed_consent_write_leaves_no_patient(staff, reg_on, monkeypatch):
    def broken(db, **kwargs):
        raise ValueError("simulated consent failure")

    monkeypatch.setattr(consent_service, "record_consent_on", broken)
    body = _body(consent=_consent(notice_language="Hindi"))
    resp = _post(staff, "doc_cardio", body)
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"]["error"] == "bad_consent"
    assert _patients_named(body["name"]) == 0


def test_a_failed_consent_audit_answers_503_and_saves_nothing(staff, reg_on, monkeypatch):
    from services.audit_service import AuditWriteError
    real = reg.log_audit_event

    def failing(*args, **kwargs):
        if kwargs.get("action_type") == "consent_recorded":
            raise AuditWriteError("simulated outage")
        return real(*args, **kwargs)

    monkeypatch.setattr(reg, "log_audit_event", failing)
    body = _body(consent=_consent(notice_language="Hindi"))
    resp = _post(staff, "doc_cardio", body)
    assert resp.status_code == 503 and resp.json()["detail"]["error"] == "audit_unavailable"
    assert _patients_named(body["name"]) == 0
    with db_session() as db:
        assert db.execute("SELECT COUNT(*) FROM consent_records c JOIN patients p "
                          "ON p.id = c.patient_id WHERE p.name = ?",
                          (body["name"],)).fetchone()[0] == 0


def test_a_replay_records_no_second_consent(staff, reg_on):
    body = _body(consent=_consent(notice_language="Hindi"))
    key = f"rgc-{uuid.uuid4().hex}"
    first = _post(staff, "doc_cardio", body, key=key)
    again = _post(staff, "doc_cardio", body, key=key)
    assert first.status_code == 201 and again.status_code == 200, again.text
    pid = first.json()["patient"]["patient_id"]
    assert again.json()["replayed"] is True and "consents" not in again.json()
    assert len(_consents_for(pid)) == 1


# ═════════════════════════════════════════════════════════════════════════════
# 3 and 4. The strict rules, each refused before anything is written
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("step, status, code", [
    (_consent(notice_acknowledged=False, notice_language="Hindi"), 422, "consent_notice_required"),
    (_consent(notice_acknowledged=None, notice_language="Hindi"), 422, "consent_notice_required"),
    (_consent(treatment_consent=False, notice_language="Hindi"), 422, "consent_treatment_required"),
    (_consent(notice_version=None, notice_language="Hindi"), 422, "notice_version_required"),
    (_consent(notice_version="   ", notice_language="Hindi"), 422, "notice_version_required"),
    (_consent(notice_language="Tulu"), 422, "bad_notice_language"),
    (_consent(), 422, "bad_notice_language"),  # no language and no preferred one
    (_consent(notice_language="Hindi", method="digital_portal"), 422, "bad_consent_method"),
    (_consent(notice_language="Hindi", method="smoke_signal"), 422, "bad_consent_method"),
    (_consent(notice_language="Hindi", method=None), 422, "bad_consent_method"),
    (_consent(notice_language="Hindi", given_by="neighbour"), 422, "bad_consent_given_by"),
    (_consent(notice_language="Hindi", given_by="guardian"), 422, "consent_proxy_required"),
    (_consent(notice_language="Hindi", given_by="proxy", proxy_name="A Person"), 422,
     "consent_proxy_required"),
])
def test_each_strict_rule_refuses_and_writes_nothing(staff, reg_on, step, status, code):
    body = _body(consent=step)
    resp = _post(staff, "doc_cardio", body)
    assert resp.status_code == status, resp.text
    assert resp.json()["detail"]["error"] == code
    assert _patients_named(body["name"]) == 0


@pytest.mark.parametrize("step", [
    _consent(notice_language="Hindi", notice_acknowledged="true"),
    _consent(notice_language="Hindi", treatment_consent=1),
    _consent(notice_language="Hindi", allergies="none"),
    _consent(notice_language="Hindi", purpose="marketing_communications"),
])
def test_a_coerced_fact_or_an_unknown_field_is_refused_by_the_body(staff, reg_on, step):
    body = _body(consent=step)
    resp = _post(staff, "doc_cardio", body)
    assert resp.status_code == 422, resp.text
    assert _patients_named(body["name"]) == 0


def test_notice_languages_are_english_and_the_eighth_schedule():
    assert reg.NOTICE_LANGUAGES[0] == "English"
    assert len(reg.NOTICE_LANGUAGES) == 23 and len(set(reg.NOTICE_LANGUAGES)) == 23
    for language in ("Hindi", "Bengali", "Tamil", "Telugu", "Marathi", "Urdu", "Santali",
                     "Bodo", "Dogri", "Maithili", "Konkani", "Manipuri", "Nepali", "Sindhi"):
        assert language in reg.NOTICE_LANGUAGES
    assert reg._notice_language(" malayalam ") == "Malayalam"
    assert reg._notice_language("Tulu") is None
    assert reg._notice_language(None) is None


def test_the_config_describes_the_step_without_patient_data(staff, reg_on):
    body = client.get("/api/registration/config", headers=H(staff, "recep")).json()
    step = body["consent"]
    assert step["purpose"] == "treatment"
    methods = [m["value"] for m in step["methods"]]
    assert "digital_portal" not in methods and "written_signature" in methods
    assert set(methods) == set(consent_service.CONSENT_METHODS) - {"digital_portal"}
    assert step["notice_languages"] == list(reg.NOTICE_LANGUAGES)
    assert step["given_by"] == list(consent_service.GIVEN_BY)
    assert step["guardian_methods"] == ["proxy_guardian"]
    assert step["adult_age"] == 18
    assert "part of the medical record" in step["retention_text"]
    assert "patients" not in json.dumps(body).lower()


# ═════════════════════════════════════════════════════════════════════════════
# 5. Ruling OPD-N3: no consent capture by the receptionist until counsel agrees
# ═════════════════════════════════════════════════════════════════════════════

def test_the_receptionist_does_not_hold_consent_record_and_the_doctor_does():
    assert P.CONSENT_RECORD not in P.ROLE_PERMISSIONS["receptionist"]
    assert P.CONSENT_RECORD in P.ROLE_PERMISSIONS["doctor"]
    assert P.CONSENT_RECORD in P.ROLE_PERMISSIONS["clinician"]
    assert P.CONSENT_RECORD not in P.ROLE_PERMISSIONS["admin"]


def test_the_receptionist_consent_step_is_403_audited_and_writes_nothing(staff, reg_on):
    before = len(_audit("permission_denied"))
    body = _body(consent=_consent(notice_language="Hindi"))
    resp = _post(staff, "recep", body)
    assert resp.status_code == 403, resp.text
    detail = resp.json()["detail"]
    assert detail["error"] == "consent_permission_required"
    assert detail["missing"] == [P.CONSENT_RECORD]
    assert _patients_named(body["name"]) == 0
    denials = _audit("permission_denied")[before:]
    mine = [d for d in denials if d["actor_user_id"] == staff["recep"][0]]
    assert mine, "the refusal is on the audit trail"
    meta = json.loads(mine[-1]["metadata_json"])
    assert meta["missing_permissions"] == [P.CONSENT_RECORD]
    assert meta["lane"] == "registration_consent"


def test_the_permission_is_checked_before_the_step_is_read(staff, reg_on):
    """Even a malformed step from the receptionist is 403, never a 422 that
    would tell her what the step needs."""
    resp = _post(staff, "recep", _body(consent={"notice_acknowledged": False}))
    assert resp.status_code == 403
    assert resp.json()["detail"]["error"] == "consent_permission_required"


def test_the_receptionist_still_registers_without_the_step(staff, reg_on):
    resp = _post(staff, "recep", _body(preferred_language="Kannada"))
    assert resp.status_code == 201, resp.text
    assert "consents" not in resp.json()
    assert _consents_for(resp.json()["patient"]["patient_id"]) == []


# ═════════════════════════════════════════════════════════════════════════════
# 6. Withdrawal
# ═════════════════════════════════════════════════════════════════════════════

def _admit(pid, dept):
    with db_session() as db:
        db.execute("INSERT OR IGNORE INTO users (id, name, role, password_hash) "
                   "VALUES ('RGCADMITTER', 'Regconsent Admitter', 'doctor', 'x')")
        db.execute("INSERT INTO admissions (id, patient_id, admitted_by, department, ward, bed, "
                   "status, created_at) VALUES (?, ?, 'RGCADMITTER', ?, ?, '3', 'active', ?)",
                   (f"ADM-RGC{uuid.uuid4().hex[:6]}", pid, dept, f"{dept} ward",
                    datetime.now(timezone.utc).isoformat()))


def test_the_registration_consent_is_withdrawn_as_a_new_row(staff, reg_on):
    resp = _post(staff, "doc_cardio", _body(consent=_consent(notice_language="Hindi")))
    assert resp.status_code == 201, resp.text
    pid = resp.json()["patient"]["patient_id"]
    consent_id = resp.json()["consents"][0]["id"]
    original = _consents_for(pid)[0]
    _admit(pid, CARDIO)

    wd = client.post(f"/api/consent/{pid}/{consent_id}/withdraw",
                     json={"method": "written_signature", "reason": "Patient asked"},
                     headers=H(staff, "doc_cardio"))
    assert wd.status_code == 200, wd.text
    out = wd.json()
    assert out["consent"]["effective_status"] == "withdrawn"
    assert out["withdrawal"]["erasure_review_required"] is True
    assert out["withdrawal"]["auto_deleted"] is False
    # The original row is untouched; the withdrawal is its own row.
    assert _consents_for(pid) == [original]
    with db_session() as db:
        assert db.execute("SELECT COUNT(*) FROM consent_withdrawals WHERE consent_id = ?",
                          (consent_id,)).fetchone()[0] == 1
    assert consent_service.has_active_consent(pid, "treatment") is False
    # A second withdrawal is refused.
    again = client.post(f"/api/consent/{pid}/{consent_id}/withdraw",
                        json={"method": "written_signature"}, headers=H(staff, "doc_cardio"))
    assert again.status_code == 409


def test_the_consent_route_keeps_its_admission_scope(staff, reg_on):
    """No outpatient arm on the consent routes: another department's doctor,
    and the registering doctor for a patient no ward owns, get 404."""
    resp = _post(staff, "doc_cardio", _body(consent=_consent(notice_language="Hindi")))
    pid = resp.json()["patient"]["patient_id"]
    consent_id = resp.json()["consents"][0]["id"]
    for who in ("doc_cardio", "doc_other"):
        wd = client.post(f"/api/consent/{pid}/{consent_id}/withdraw",
                         json={"method": "written_signature"}, headers=H(staff, who))
        assert wd.status_code == 404, (who, wd.text)
    assert consent_service.has_active_consent(pid, "treatment") is True
    _admit(pid, CARDIO)
    wd = client.post(f"/api/consent/{pid}/{consent_id}/withdraw",
                     json={"method": "written_signature"}, headers=H(staff, "doc_other"))
    assert wd.status_code == 404


def test_record_consent_still_opens_its_own_session():
    """record_consent is unchanged for the bedside route: same rules, same row,
    committed on its own."""
    pid = f"PAT-RGC{uuid.uuid4().hex[:6].upper()}"
    with db_session() as db:
        db.execute("INSERT INTO patients (id, name, mrn, age, sex, status, created_at) "
                   "VALUES (?, ?, ?, 40, 'M', 'registered', ?)",
                   (pid, _name(), f"RGC{uuid.uuid4().hex[:8]}",
                    datetime.now(timezone.utc).isoformat()))
    rec = consent_service.record_consent(patient_id=pid, purpose="treatment",
                                         method="verbal_witnessed", recorded_by="RGCADMITTER",
                                         notice_version="v1", notice_language="English")
    assert rec["effective_status"] == "active" and rec["patient_id"] == pid
    assert consent_service.get_consent(rec["id"])["notice_version"] == "v1"
    with pytest.raises(ValueError):
        consent_service.record_consent(patient_id=pid, purpose="no_such_purpose",
                                       method="verbal_witnessed", recorded_by="RGCADMITTER")
    with pytest.raises(ValueError):
        consent_service.record_consent(patient_id="PAT-NOPE-RGC", purpose="treatment",
                                       method="verbal_witnessed", recorded_by="RGCADMITTER")
    assert len(_consents_for(pid)) == 1


# ═════════════════════════════════════════════════════════════════════════════
# 8. Who gave consent fits how it was given and the patient's age
# ═════════════════════════════════════════════════════════════════════════════

def _child_dob(years_old=4):
    """A date of birth in the year `years_old` years ago: the patient is
    years_old or one less, a child either way."""
    return f"{date.today().year - years_old}-06-15"


def _refused(staff, body, code):
    resp = _post(staff, "doc_cardio", body)
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"]["error"] == code, resp.text
    assert _patients_named(body["name"]) == 0


def test_the_reviewers_probe_a_guardian_method_with_no_guardian_is_refused(staff, reg_on):
    """age_years 4, method proxy_guardian, given_by left to default: before the
    fix this wrote an immutable row 'given by a guardian' AND 'by the patient'
    naming no guardian, and has_active_consent answered True."""
    body = _body(date_of_birth=None, age_years=4, consent=_consent(
        notice_version="v1", notice_language="Hindi", method="proxy_guardian"))
    _refused(staff, body, "consent_given_by_mismatch")
    with db_session() as db:
        assert db.execute("SELECT COUNT(*) FROM consent_records c JOIN patients p "
                          "ON p.id = c.patient_id WHERE p.name = ?",
                          (body["name"],)).fetchone()[0] == 0


@pytest.mark.parametrize("given_by", [None, "patient"])
def test_the_guardian_method_is_never_given_by_the_patient_even_for_an_adult(
        staff, reg_on, given_by):
    step = _consent(notice_language="Hindi", method="proxy_guardian")
    if given_by:
        step["given_by"] = given_by
    _refused(staff, _body(consent=step), "consent_given_by_mismatch")


@pytest.mark.parametrize("age", [
    {"age_years": 4}, {"age_years": 17}, {"age_years": 0}, {"date_of_birth": "child"},
])
def test_a_patient_under_18_does_not_consent_for_themselves(staff, reg_on, age):
    if age.get("date_of_birth") == "child":
        age = {"date_of_birth": _child_dob()}
    else:
        age = {"date_of_birth": None, **age}
    for step in (_consent(notice_language="Hindi"),
                 _consent(notice_language="Hindi", given_by="patient",
                          proxy_name="Ignored", proxy_relationship="Ignored")):
        _refused(staff, _body(consent=step, **age), "consent_guardian_required")


def test_the_refusal_names_the_rule_and_says_what_to_do(staff, reg_on):
    resp = _post(staff, "doc_cardio", _body(date_of_birth=None, age_years=9,
                                            consent=_consent(notice_language="Hindi")))
    message = resp.json()["detail"]["message"]
    assert "under 18" in message and "guardian" in message
    assert "\u2014" not in message


def test_a_child_with_a_named_guardian_is_recorded(staff, reg_on):
    resp = _post(staff, "doc_cardio", _body(
        date_of_birth=None, age_years=4, consent=_consent(
            notice_language="Hindi", method="proxy_guardian", given_by="guardian",
            proxy_name="Ramesh Patil", proxy_relationship="Father")))
    assert resp.status_code == 201, resp.text
    pid = resp.json()["patient"]["patient_id"]
    row = _consents_for(pid)[0]
    assert (row["method"], row["given_by"], row["proxy_name"], row["proxy_relationship"]) == (
        "proxy_guardian", "guardian", "Ramesh Patil", "Father")
    assert consent_service.has_active_consent(pid, "treatment") is True


def test_a_child_by_date_of_birth_with_a_named_proxy_is_recorded(staff, reg_on):
    resp = _post(staff, "doc_cardio", _body(
        date_of_birth=_child_dob(10), consent=_consent(
            notice_language="Tamil", method="verbal_witnessed", given_by="proxy",
            proxy_name="Lakshmi Iyer", proxy_relationship="Grandmother")))
    assert resp.status_code == 201, resp.text
    row = _consents_for(resp.json()["patient"]["patient_id"])[0]
    assert (row["given_by"], row["proxy_name"]) == ("proxy", "Lakshmi Iyer")


def test_a_guardian_named_without_a_relationship_is_still_refused_for_a_child(staff, reg_on):
    _refused(staff, _body(date_of_birth=None, age_years=6, consent=_consent(
        notice_language="Hindi", method="proxy_guardian", given_by="guardian",
        proxy_name="Ramesh Patil")), "consent_proxy_required")


def test_an_adult_of_18_consents_for_themselves(staff, reg_on):
    resp = _post(staff, "doc_cardio", _body(date_of_birth=None, age_years=18,
                                            consent=_consent(notice_language="Hindi")))
    assert resp.status_code == 201, resp.text
    assert _consents_for(resp.json()["patient"]["patient_id"])[0]["given_by"] == "patient"


def test_younger_than_counts_to_the_birthday(monkeypatch):
    class Today(date):
        @classmethod
        def today(cls):
            return cls(2026, 10, 10)

    class Now(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 10, 6, 0, tzinfo=timezone.utc)

    monkeypatch.setattr(reg, "date", Today)
    monkeypatch.setattr(reg, "datetime", Now)
    assert reg._younger_than(18, "2008-10-10", None) is False
    assert reg._younger_than(18, "2008-10-11", None) is True
    assert reg._younger_than(18, "2008-02-29", None) is False
    assert reg._younger_than(18, None, 17) is True
    assert reg._younger_than(18, None, 18) is False
    # Neither (the registration refuses that first) or unreadable: younger.
    assert reg._younger_than(18, None, None) is True
    assert reg._younger_than(18, "10/10/2008", None) is True


def test_younger_than_takes_the_earlier_clock_on_a_disputed_birthday(monkeypatch):
    """Local date already 11 October, UTC still 10 October: a patient born on
    11 October 2008 is 18 by one clock and 17 by the other, so 17."""
    class Today(date):
        @classmethod
        def today(cls):
            return cls(2026, 10, 11)

    class Now(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 10, 20, 0, tzinfo=timezone.utc)

    monkeypatch.setattr(reg, "date", Today)
    monkeypatch.setattr(reg, "datetime", Now)
    assert reg._younger_than(18, "2008-10-11", None) is True
    assert reg._younger_than(18, "2008-10-10", None) is False
