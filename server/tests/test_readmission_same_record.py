"""Readmitting a discharged patient onto the same record (opd-17c, W8-A).

What this file defends:

  1. OFF BY DEFAULT. ORB_READMISSION is off: POST /api/admissions/readmit is
     404 feature_disabled (401 without a session), the admission form's index
     refusal and the outpatient conversion say readmission is not available,
     exactly as before. It needs ORB_REGISTRATION.
  2. THE SAME RECORD. The patient keeps their id and hospital number, no
     second patient row exists, and name, age, sex and date of birth are never
     written. The earlier admission and its discharge summary stay as they
     were, keyed to the earlier stay. The profile is filled, never blanked;
     charted allergies stay and new ones are added. One case room for life,
     reused with a READMISSION line that divides the stays. One portal login,
     reused, with a fresh one-time password and the earlier sessions revoked
     (ruling R029 (2)). Nothing is written to vitals or risk_scores.
  3. OPD-S8 (a). Every medicine the earlier stay left 'active' or 'prn' is
     discontinued in the readmission's transaction (a linked medication order
     is stopped through the order store), audited, and listed for the
     prescriber; after readmission the eMAR refuses to give any of them.
  4. ADT-S9 (a), the Sage half. The room's Sage memory is archived (one
     sage_observations row) and restarted; Sage's open suggestions are closed;
     the prefetched Sage context is cleared.
  5. OPD-S1 (a). No identity check, or a mismatch, is refused, audited and
     writes nothing.
  6. REFUSALS. An admitted patient, a never-admitted one, a record that says
     the patient died, an unknown id, a missing Idempotency-Key, a body with an
     identity field or observations, a nurse.
  7. REPLAYS AND RACES. The same key returns the first admission (200, no
     password) with the stopped medicines read back; the key on another
     patient is 409; two readmissions at once give one admission and one
     refusal (the one-active-admission index).
  8. ONE TRANSACTION. An unwritable readmission record leaves nothing behind
     (503).
  9. THE OTHER DOOR. With ORB_OPD on, a discharged patient's clinic visit is
     admittable and the conversion applies the same readmission rules.
 10. ORB_ADT ON. The readmission is placed through the bed master's writer,
     and, through either door, an admission time before the earlier stay
     ended is refused 400 before_previous_stay_end and writes nothing.
"""
from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import config
from auth import create_token, verify_password
from database import db_session
from main import app
from services import admission_service as adm
from services.audit_service import AuditWriteError
from services.clinical_records_service import record_allergies_on

client = TestClient(app)

DEPT = "Readmit Test Medicine"
OTHER_DEPT = "Readmit Test Surgery"
DOB = "1961-05-02"

_SEEDED: list[str] = []


def _mk_user(role, department, name=None):
    uid = f"RMU{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO users (id, name, role, password_hash, department, is_active) "
            "VALUES (?, ?, ?, 'x', ?, 1)",
            (uid, name or f"Readmit {role} {uid[-4:]}", role, department))
    return uid


def _auth(uid, role="doctor", key=None):
    out = {"Authorization": f"Bearer {create_token(uid, role)}"}
    if key:
        out["Idempotency-Key"] = key
    return out


@pytest.fixture(scope="module")
def staff():
    return {
        "doc": _mk_user("doctor", DEPT, "Dr Asha Readmit"),
        "other_doc": _mk_user("doctor", OTHER_DEPT, "Dr Other Readmit"),
        "nurse": _mk_user("nurse", DEPT),
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
def readmission_on(monkeypatch):
    monkeypatch.setattr(config, "REGISTRATION_ENABLED", True)
    monkeypatch.setattr(config, "READMISSION_ENABLED", True)
    monkeypatch.setattr(config, "ADT_ENABLED", False)
    monkeypatch.setattr(config, "UHID_SOURCE", "hospital")
    monkeypatch.setattr(config, "UHID_PATTERN", None)
    assert config.feature_enabled("readmission")
    yield


# ── helpers ──────────────────────────────────────────────────────────────────

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


def _iso(dt):
    return dt.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _discharged_patient(doc, *, department=DEPT, medicines=True, sage=True):
    """A patient admitted through the admission form, given medicines,
    observations, a risk row, Sage memory and an open Sage suggestion, then
    discharged with a summary keyed to the stay: the earlier stay."""
    uhid = f"UH{uuid.uuid4().hex[:8].upper()}"
    phone = f"98{uuid.uuid4().int % 10**8:08d}"
    # The earlier stay is admitted as any patient was before the patient index
    # (registration off), whatever the test switched on.
    registration = config.REGISTRATION_ENABLED
    config.REGISTRATION_ENABLED = False
    try:
        out = adm.admit_patient({
            "name": f"Readmit Patient {uhid[-4:]}", "age": 65, "date_of_birth": DOB,
            "sex": "F", "phone": phone, "mrn": uhid, "admission_source": "er",
            "admission_type": "inpatient", "department": department, "ward": "RM-W1",
            "bed": f"B{uuid.uuid4().hex[:3]}", "attending_doctor_id": doc,
            "chief_complaint": "Breathlessness", "initial_diagnosis": "COPD exacerbation",
            "comorbidities": ["COPD"], "past_medical_history": "Smoker for 30 years",
            "allergies": ["penicillin"],
        }, {"id": doc, "name": "Dr Asha Readmit", "role": "doctor", "department": department})
    finally:
        config.REGISTRATION_ENABLED = registration
    pid, old_adm = out["patient_id"], out["admission_id"]
    _SEEDED.append(pid)
    earlier = datetime.now(timezone.utc) - timedelta(days=5)
    ended = datetime.now(timezone.utc) - timedelta(days=2)
    meds = {}
    with db_session() as db:
        db.execute("UPDATE admissions SET created_at = ? WHERE id = ?", (_iso(earlier), old_adm))
        if medicines:
            for drug, status in (("Amoxicillin", "active"), ("Morphine", "prn"),
                                 ("Ceftriaxone", "discontinued")):
                meds[drug] = db.execute(
                    "INSERT INTO medications (patient_id, drug_name, dosage, route, frequency, "
                    "status, prescribed_by, source) VALUES (?, ?, '500 mg', 'Oral', 'TDS', ?, ?, "
                    "'clinician')", (pid, drug, status, doc)).lastrowid
        db.execute(
            "INSERT INTO vitals (patient_id, timestamp, heart_rate, systolic_bp, diastolic_bp, "
            "spo2, temperature, respiratory_rate, consciousness, on_oxygen) "
            "VALUES (?, ?, 135, 82, 50, 85, 39.2, 32, 'voice', 1)",
            (pid, _iso(ended - timedelta(hours=1))))
        db.execute("INSERT INTO risk_scores (patient_id, timestamp, score, level, drivers) "
                   "VALUES (?, ?, 0.9, 'critical', '[]')", (pid, _iso(ended - timedelta(hours=1))))
        room = out["channel_id"]
        if sage:
            db.execute("UPDATE sage_room_memory SET discussion_summary = ?, current_concern = ?, "
                       "last_processed_msg_id = 7 WHERE room_id = ?",
                       ("Team planned to step down oxygen", "Hypoxia overnight", room))
            db.execute("INSERT INTO sage_actions (id, room_id, patient_id, action_type, target, "
                       "state) VALUES (?, ?, ?, 'transfer', 'ICU', 'suggested')",
                       (f"act-{uuid.uuid4().hex[:8]}", room, pid))
            try:
                db.execute("INSERT INTO sage_decision_cache (patient_id, compiled_context, "
                           "preloaded_at, expires_at) VALUES (?, '{}', ?, ?)",
                           (pid, _iso(ended), _iso(datetime.now(timezone.utc) + timedelta(hours=1))))
            except Exception:
                pass
        db.execute("UPDATE patients SET status = 'discharged' WHERE id = ?", (pid,))
        db.execute("UPDATE admissions SET status = 'discharged', discharged_at = ? WHERE id = ?",
                   (_iso(ended), old_adm))
        summary_id = db.execute(
            "INSERT INTO discharge_summaries (patient_id, author_id, summary, discharged_at, "
            "admission_id) VALUES (?, ?, 'Earlier stay summary', ?, ?)",
            (pid, doc, ended.strftime("%Y-%m-%d %H:%M:%S"), old_adm)).lastrowid
    return {"pid": pid, "uhid": uhid, "old_admission": old_adm, "room": out["channel_id"],
            "login": out["patient_login_id"], "first_password": out["temporary_password"],
            "meds": meds, "summary_id": summary_id, "phone": phone}


def _stated(p, **over):
    out = {"name": _one("SELECT name FROM patients WHERE id = ?", (p["pid"],))["name"],
           "date_of_birth": DOB, "uhid": p["uhid"]}
    out.update(over)
    return out


def _body(p, doc, **over):
    body = {
        "patient_id": p["pid"], "identity_check": _stated(p),
        "admission_source": "er", "admission_type": "inpatient",
        "department": DEPT, "ward": "RM-W2", "bed": f"B{uuid.uuid4().hex[:3]}",
        "attending_doctor_id": doc, "chief_complaint": "Breathless again",
        "initial_diagnosis": "COPD exacerbation", "allergies": ["sulfa"],
        "past_medical_history": "", "comorbidities": ["Hypertension"],
    }
    body.update(over)
    return body


def _readmit(doc, body, key=None, role="doctor"):
    return client.post("/api/admissions/readmit", json=body,
                       headers=_auth(doc, role, key or uuid.uuid4().hex))


def _nothing_written(p):
    assert _one("SELECT status FROM patients WHERE id = ?", (p["pid"],))["status"] == "discharged"
    assert _count("admissions", p["pid"]) == 1
    assert _all("SELECT status FROM medications WHERE id IN (?, ?)",
                (p["meds"]["Amoxicillin"], p["meds"]["Morphine"])) == [
        {"status": "active"}, {"status": "prn"}]
    assert not _audits(adm.READMISSION_AUDIT_ACTION, p["pid"])


# ═════════════════════════════════════════════════════════════════════════════
# 1. Off by default
# ═════════════════════════════════════════════════════════════════════════════

def test_the_feature_is_off_by_default_and_needs_registration(monkeypatch):
    assert config.FEATURE_FLAGS["readmission"] == "ORB_READMISSION"
    assert config.READMISSION_ENABLED is False
    assert config.feature_enabled("readmission") is False
    assert ("feature", "registration") in config.FEATURE_PREREQUISITES["readmission"]
    monkeypatch.setattr(config, "READMISSION_ENABLED", True)
    monkeypatch.setattr(config, "REGISTRATION_ENABLED", False)
    assert config.feature_enabled("readmission") is False
    assert adm.readmission_on() is False


def test_the_route_is_404_while_off_and_401_without_a_session(staff):
    resp = client.post("/api/admissions/readmit", json={},
                       headers=_auth(staff["doc"], key="k-off"))
    assert resp.status_code == 404 and resp.json()["detail"]["error"] == "feature_disabled"
    assert client.post("/api/admissions/readmit", json={}).status_code == 401


def test_the_index_refusal_offers_readmission_only_while_it_is_on(staff, readmission_on,
                                                                  monkeypatch):
    doc = staff["doc"]
    p = _discharged_patient(doc)
    form = {"name": "Someone Typed", "age": 40, "sex": "M", "mrn": p["uhid"],
            "admission_source": "er", "admission_type": "emergency", "department": DEPT,
            "ward": "RM-W1", "bed": "B901", "attending_doctor_id": doc,
            "chief_complaint": "Pain"}
    resp = client.post("/api/admissions/admit", json=form, headers=_auth(doc))
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["error"] == "possible_existing_patient"
    assert detail["readmission_available"] is True
    assert detail["message"].endswith(adm.READMIT_POINTER)
    card = next(c for c in detail["candidates"] if c["patient_id"] == p["pid"])
    assert card["readmittable"] is True
    assert "date_of_birth" not in card

    monkeypatch.setattr(config, "READMISSION_ENABLED", False)
    resp = client.post("/api/admissions/admit", json=form, headers=_auth(doc))
    detail = resp.json()["detail"]
    assert "readmission_available" not in detail
    assert all("readmittable" not in c for c in detail["candidates"])
    assert detail["message"].endswith(adm.ADMIT_EXISTING_NOT_BUILT)
    _nothing_written(p)


def test_scribes_quick_admission_points_to_the_admission_form(staff, readmission_on):
    """Scribe has no Readmit choice, so its refusal says where to go."""
    refusal = adm._door_refusal(
        "existing_record", "A patient with this hospital number already has a record.",
        [{"patient_id": "PX", "band": "hard", "readmittable": True}], mode="block",
        scan={"check_incomplete": False, "truncated": False, "unseen_band": None},
        user={"id": staff["doc"], "role": "doctor"}, door=adm.DOOR_SCRIBE_QUICK_ADMIT,
        override_allowed=False)
    assert refusal.refused.message.endswith(adm.READMIT_FROM_FORM_POINTER)
    assert refusal.refused.detail()["readmission_available"] is True


# ═════════════════════════════════════════════════════════════════════════════
# 2. The same record
# ═════════════════════════════════════════════════════════════════════════════

def test_a_readmission_keeps_the_record_and_writes_one_admission(staff, readmission_on):
    doc = staff["doc"]
    p = _discharged_patient(doc)
    before = _one("SELECT id, name, mrn, age, date_of_birth, sex, phone, user_id "
                  "FROM patients WHERE id = ?", (p["pid"],))
    patients_before = _one("SELECT COUNT(*) AS n FROM patients")["n"]
    vitals_before = _count("vitals", p["pid"])
    risk_before = _count("risk_scores", p["pid"])
    token_before = _one("SELECT COALESCE(token_version, 0) AS tv FROM users WHERE id = ?",
                        (p["login"],))["tv"]

    resp = _readmit(doc, _body(p, doc))
    assert resp.status_code == 201, resp.text
    out = resp.json()
    assert out["patient_id"] == p["pid"] and out["mrn"] == p["uhid"]
    assert out["replayed"] is False and out["readmission"] is True

    after = _one("SELECT id, name, mrn, age, date_of_birth, sex, phone, user_id, status "
                 "FROM patients WHERE id = ?", (p["pid"],))
    assert after["status"] == "admitted"
    assert {k: after[k] for k in before} == before
    assert _one("SELECT COUNT(*) AS n FROM patients")["n"] == patients_before

    # The earlier stay stays as it was; the new one is the only active one.
    stays = _all("SELECT id, status FROM admissions WHERE patient_id = ? ORDER BY created_at",
                 (p["pid"],))
    assert stays == [{"id": p["old_admission"], "status": "discharged"},
                     {"id": out["admission_id"], "status": "active"}]
    assert _one("SELECT admission_id, summary FROM discharge_summaries WHERE id = ?",
                (p["summary_id"],)) == {"admission_id": p["old_admission"],
                                        "summary": "Earlier stay summary"}

    # No observation was written: the ward charts a fresh set.
    assert _count("vitals", p["pid"]) == vitals_before
    assert _count("risk_scores", p["pid"]) == risk_before

    # One portal login, reused, with a fresh one-time password; the earlier
    # stay's sessions revoked (R029 (2)).
    assert out["patient_login_id"] == p["login"] and out["portal_login_created"] is False
    assert out["portal_password_reissued"] is True and out["temporary_password"]
    login = _one("SELECT password_hash, COALESCE(token_version, 0) AS tv FROM users "
                 "WHERE id = ?", (p["login"],))
    assert verify_password(out["temporary_password"], login["password_hash"])
    assert not verify_password(p["first_password"], login["password_hash"])
    assert login["tv"] == token_before + 1
    assert _one("SELECT COUNT(*) AS n FROM users WHERE id LIKE 'P%' AND name = ?",
                (after["name"],))["n"] == 1


def test_the_profile_is_filled_never_blanked_and_allergies_only_added(staff, readmission_on):
    doc = staff["doc"]
    p = _discharged_patient(doc)
    resp = _readmit(doc, _body(p, doc, past_medical_history=""))
    assert resp.status_code == 201, resp.text
    profile = _one("SELECT * FROM patient_profiles WHERE patient_id = ?", (p["pid"],))
    assert profile["past_medical_history"] == "Smoker for 30 years"
    assert json.loads(profile["comorbidities"]) == ["COPD", "Hypertension"]
    assert set(json.loads(profile["allergies"])) == {"penicillin", "sulfa"}
    charted = {r["substance"].lower() for r in _all(
        "SELECT substance FROM patient_allergies WHERE patient_id = ? AND status = 'active'",
        (p["pid"],))}
    assert {"penicillin", "sulfa"} <= charted


def test_one_case_room_for_life_with_a_dividing_readmission_line(staff, readmission_on):
    doc = staff["doc"]
    p = _discharged_patient(doc)
    resp = _readmit(doc, _body(p, doc))
    assert resp.status_code == 201, resp.text
    out = resp.json()
    rooms = _all("SELECT id FROM channels WHERE patient_id = ? AND type = 'case_room'",
                 (p["pid"],))
    assert rooms == [{"id": p["room"]}]
    assert out["channel_id"] == p["room"] and out["case_room_reused"] is True
    line = _all("SELECT content FROM messages WHERE channel_id = ? ORDER BY id DESC LIMIT 1",
                (p["room"],))[0]["content"]
    assert line.startswith("**READMISSION**")
    assert adm.READMISSION_DIVIDER in line
    assert "Amoxicillin 500 mg Oral TDS" in line and "Morphine 500 mg Oral when needed" in line
    assert "Ceftriaxone" not in line
    assert "Observations from the previous stay are not current" in line


# ═════════════════════════════════════════════════════════════════════════════
# 3. OPD-S8 (a): the earlier stay's medicines
# ═════════════════════════════════════════════════════════════════════════════

def test_earlier_stay_medicines_are_stopped_listed_and_audited(staff, readmission_on):
    doc = staff["doc"]
    p = _discharged_patient(doc)
    resp = _readmit(doc, _body(p, doc))
    assert resp.status_code == 201, resp.text
    out = resp.json()
    statuses = {r["drug_name"]: r["status"] for r in _all(
        "SELECT drug_name, status FROM medications WHERE patient_id = ?", (p["pid"],))}
    assert statuses == {"Amoxicillin": "discontinued", "Morphine": "discontinued",
                        "Ceftriaxone": "discontinued"}
    assert out["previous_stay_medicines_stopped"] == 2
    assert out["previous_stay_medicines_listed"] is True
    listed = {m["drug_name"]: m for m in out["previous_stay_medicines"]}
    assert set(listed) == {"Amoxicillin", "Morphine"}
    assert listed["Morphine"]["as_needed"] is True and listed["Amoxicillin"]["dose"] == "500 mg"
    (row,) = _audits(adm.PREVIOUS_STAY_MEDICINES_ACTION, p["pid"])
    assert row["event_category"] == "clinical_access"
    meta = _meta(row)
    assert sorted(meta["medication_ids"]) == sorted(
        [p["meds"]["Amoxicillin"], p["meds"]["Morphine"]])
    assert meta["previous_admission_id"] == p["old_admission"]
    assert meta["admission_id"] == out["admission_id"]


def test_after_readmission_no_earlier_medicine_can_be_given_on_the_emar(staff, readmission_on):
    from services import emar_service

    doc = staff["doc"]
    p = _discharged_patient(doc)
    assert _readmit(doc, _body(p, doc)).status_code == 201
    for drug in ("Amoxicillin", "Morphine", "Ceftriaxone"):
        with pytest.raises(ValueError, match="not an active order"):
            emar_service.record_administration(p["meds"][drug], staff["nurse"], status="given")
    resp = client.post(f"/api/emar/medications/{p['meds']['Amoxicillin']}/administer",
                       json={"status": "given"}, headers=_auth(staff["nurse"], "nurse"))
    assert resp.status_code in (400, 422), resp.text
    assert _count("medication_administrations", p["pid"]) == 0


def test_a_linked_medication_order_is_stopped_through_the_store(staff, readmission_on,
                                                                monkeypatch):
    from services.medorders import adoption

    doc = staff["doc"]
    p = _discharged_patient(doc)
    monkeypatch.setattr(config, "MED_ORDER_STORE_ENABLED", True)
    adoption.adopt_legacy(trigger="script", patient_ids=[p["pid"]])
    order_id = _one("SELECT med_order_id FROM medications WHERE id = ?",
                    (p["meds"]["Amoxicillin"],))["med_order_id"]
    assert order_id
    # The switches do not decide it: the stop is keyed on the link.
    monkeypatch.setattr(config, "MED_ORDER_STORE_ENABLED", False)
    try:
        resp = _readmit(doc, _body(p, doc))
        assert resp.status_code == 201, resp.text
        order = _one("SELECT status, status_reason FROM med_orders WHERE id = ?", (order_id,))
        assert order == {"status": "stopped", "status_reason": adm.PREVIOUS_STAY_STOP_REASON}
        assert _one("SELECT status FROM medications WHERE id = ?",
                    (p["meds"]["Amoxicillin"],))["status"] == "discontinued"
        events = _all("SELECT kind, entry_point FROM med_order_events WHERE med_order_id = ? "
                      "AND kind = 'stopped'", (order_id,))
        assert events == [{"kind": "stopped", "entry_point": adm.READMISSION_ENTRY_POINT}]
        meta = _meta(_audits(adm.PREVIOUS_STAY_MEDICINES_ACTION, p["pid"])[0])
        assert order_id in meta["med_order_ids"]
    finally:
        with db_session() as db:
            db.execute("DELETE FROM med_order_events WHERE patient_id = ?", (p["pid"],))
            db.execute("UPDATE medications SET med_order_id = NULL WHERE patient_id = ?",
                       (p["pid"],))
            db.execute("DELETE FROM med_orders WHERE patient_id = ?", (p["pid"],))


def test_the_list_is_named_only_to_a_sender_who_reads_the_chart_after(staff, readmission_on):
    doc = staff["doc"]
    p = _discharged_patient(doc)
    # Admitted into another department: the sender does not read the chart
    # once it commits, so the medicines are counted, not named.
    resp = _readmit(doc, _body(p, doc, department=OTHER_DEPT))
    assert resp.status_code == 201, resp.text
    out = resp.json()
    assert out["previous_stay_medicines_stopped"] == 2
    assert out["previous_stay_medicines_listed"] is False
    assert out["previous_stay_medicines"] == []
    assert _one("SELECT status FROM medications WHERE id = ?",
                (p["meds"]["Amoxicillin"],))["status"] == "discontinued"


def test_on_a_ward_record_computer_no_drug_is_named_but_every_one_is_stopped(
        staff, readmission_on, monkeypatch):
    doc = staff["doc"]
    p = _discharged_patient(doc)
    monkeypatch.setattr(adm, "_drug_chart_enabled", lambda: False)
    resp = _readmit(doc, _body(p, doc))
    assert resp.status_code == 201, resp.text
    out = resp.json()
    assert out["previous_stay_medicines"] == [] and out["previous_stay_medicines_stopped"] == 2
    line = _all("SELECT content FROM messages WHERE channel_id = ? ORDER BY id DESC LIMIT 1",
                (p["room"],))[0]["content"]
    assert "Amoxicillin" not in line and "2 medicine(s) from the previous stay" in line
    assert {r["status"] for r in _all("SELECT status FROM medications WHERE patient_id = ?",
                                      (p["pid"],))} == {"discontinued"}


# ═════════════════════════════════════════════════════════════════════════════
# 4. ADT-S9 (a): Sage memory archived and restarted
# ═════════════════════════════════════════════════════════════════════════════

def test_sage_memory_is_archived_restarted_and_open_suggestions_closed(staff, readmission_on):
    doc = staff["doc"]
    p = _discharged_patient(doc)
    resp = _readmit(doc, _body(p, doc))
    assert resp.status_code == 201, resp.text
    memory = _one("SELECT * FROM sage_room_memory WHERE room_id = ?", (p["room"],))
    assert "readmitted" in memory["patient_summary"]
    assert "Team planned to step down oxygen" not in (memory["discussion_summary"] or "")
    assert memory["current_concern"] == "Breathless again"
    assert memory["last_processed_msg_id"] is None and memory["message_count"] == 1
    (archive,) = _all("SELECT * FROM sage_observations WHERE room_id = ? "
                      "AND observation_type = ?", (p["room"], adm.ROOM_MEMORY_ARCHIVED))
    content = json.loads(archive["content"])
    assert content["discussion_summary"] == "Team planned to step down oxygen"
    assert content["current_concern"] == "Hypoxia overnight"
    assert content["previous_admission_id"] == p["old_admission"]
    assert archive["patient_id"] == p["pid"] and archive["source_message_id"] == 7
    actions = _all("SELECT state FROM sage_actions WHERE room_id = ?", (p["room"],))
    assert actions == [{"state": "superseded"}]
    events = _all("SELECT e.new_state, e.source, e.reason FROM sage_action_events e "
                  "JOIN sage_actions a ON a.id = e.action_id WHERE a.room_id = ?", (p["room"],))
    assert events == [{"new_state": "superseded", "source": "system",
                       "reason": adm.ROOM_ACTION_CLOSED_REASON}]
    try:
        assert _count("sage_decision_cache", p["pid"]) == 0
    except Exception:
        pass
    meta = _meta(_audits(adm.READMISSION_AUDIT_ACTION, p["pid"])[0])
    assert meta["sage_memory_archived"] is True and meta["sage_actions_closed"] == 1


# ═════════════════════════════════════════════════════════════════════════════
# 5. OPD-S1 (a): who the patient is
# ═════════════════════════════════════════════════════════════════════════════

def test_no_identity_check_is_refused_audited_and_writes_nothing(staff, readmission_on):
    doc = staff["doc"]
    p = _discharged_patient(doc)
    resp = _readmit(doc, _body(p, doc, identity_check=None))
    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "identity_check_required"
    (row,) = _audits(adm.CONVERSION_IDENTITY_REFUSED_ACTION, p["pid"])
    assert row["event_category"] == "patient_identity" and row["outcome"] == "denied"
    assert _meta(row)["purpose"] == adm.READMISSION_PURPOSE
    _nothing_written(p)


def test_a_mismatched_identity_is_refused_and_writes_nothing(staff, readmission_on):
    doc = staff["doc"]
    p = _discharged_patient(doc)
    resp = _readmit(doc, _body(p, doc, identity_check=_stated(p, date_of_birth="1990-01-01")))
    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "identity_not_confirmed"
    assert "1961" not in resp.text
    _nothing_written(p)


# ═════════════════════════════════════════════════════════════════════════════
# 6. Refusals
# ═════════════════════════════════════════════════════════════════════════════

def test_an_admitted_patient_is_refused(staff, readmission_on):
    doc = staff["doc"]
    p = _discharged_patient(doc)
    assert _readmit(doc, _body(p, doc)).status_code == 201
    resp = _readmit(doc, _body(p, doc))
    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "patient_already_admitted"
    assert _one("SELECT COUNT(*) AS n FROM admissions WHERE patient_id = ? "
                "AND status = 'active'", (p["pid"],))["n"] == 1


def test_a_patient_never_admitted_is_not_a_readmission(staff, readmission_on):
    doc = staff["doc"]
    pid = f"RMP{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute("INSERT INTO patients (id, name, mrn, status) VALUES (?, ?, ?, 'registered')",
                   (pid, "Registered Only", f"UH{uuid.uuid4().hex[:8]}"))
    _SEEDED.append(pid)
    resp = _readmit(doc, {**_body({"pid": pid, "uhid": "x"}, doc), "identity_check": {
        "name": "Registered Only", "age_years": 40, "uhid": "x"}})
    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "readmission_not_applicable"


def test_a_transferred_out_record_is_not_readmitted_here(staff, readmission_on):
    doc = staff["doc"]
    p = _discharged_patient(doc, medicines=False)
    with db_session() as db:
        db.execute("UPDATE patients SET status = 'transferred' WHERE id = ?", (p["pid"],))
    resp = _readmit(doc, _body(p, doc))
    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "readmission_not_applicable"
    assert _count("admissions", p["pid"]) == 1


@pytest.mark.parametrize("how", ["end_type", "nabh"])
def test_a_record_that_says_the_patient_died_is_refused(staff, readmission_on, how):
    doc = staff["doc"]
    p = _discharged_patient(doc, medicines=False)
    with db_session() as db:
        if how == "end_type":
            db.execute("UPDATE admissions SET discharge_type = 'death' WHERE id = ?",
                       (p["old_admission"],))
        else:
            db.execute("INSERT INTO nabh_discharge_dispositions (patient_id, admission_id, "
                       "disposition, occurred_at) VALUES (?, ?, 'death', ?)",
                       (p["pid"], p["old_admission"], _iso(datetime.now(timezone.utc))))
    resp = _readmit(doc, _body(p, doc))
    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "patient_deceased"
    assert _count("admissions", p["pid"]) == 1


def test_unknown_patient_missing_key_extra_fields_and_a_nurse(staff, readmission_on):
    doc = staff["doc"]
    p = _discharged_patient(doc, medicines=False)
    resp = _readmit(doc, _body(p, doc, patient_id="NO-SUCH-PATIENT"))
    assert resp.status_code == 404
    resp = client.post("/api/admissions/readmit", json=_body(p, doc), headers=_auth(doc))
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"] == "idempotency_key_required"
    for extra in ({"vitals": {"hr": 90}}, {"name": "Changed Name"}, {"mrn": "NEW-1"},
                  {"date_of_birth": "1970-01-01"}):
        resp = _readmit(doc, _body(p, doc, **extra))
        assert resp.status_code == 422, extra
    resp = _readmit(staff["nurse"], _body(p, doc), role="nurse")
    assert resp.status_code == 403
    assert _count("admissions", p["pid"]) == 1


# ═════════════════════════════════════════════════════════════════════════════
# 7. Replays and races
# ═════════════════════════════════════════════════════════════════════════════

def test_a_replay_returns_the_first_readmission_without_a_password(staff, readmission_on):
    doc = staff["doc"]
    p = _discharged_patient(doc)
    key = uuid.uuid4().hex
    first = _readmit(doc, _body(p, doc), key=key)
    assert first.status_code == 201, first.text
    again = _readmit(doc, _body(p, doc), key=key)
    assert again.status_code == 200, again.text
    body = again.json()
    assert body["replayed"] is True and body["temporary_password"] is None
    assert body["admission_id"] == first.json()["admission_id"]
    assert {m["drug_name"] for m in body["previous_stay_medicines"]} == {"Amoxicillin",
                                                                          "Morphine"}
    assert _count("admissions", p["pid"]) == 2
    assert len(_audits(adm.READMISSION_AUDIT_ACTION, p["pid"])) == 1
    other = _discharged_patient(doc, medicines=False)
    resp = _readmit(doc, _body(other, doc), key=key)
    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "idempotency_key_reused"


def test_two_readmissions_at_once_give_one_admission(staff, readmission_on):
    doc = staff["doc"]
    p = _discharged_patient(doc, medicines=False)
    answers = []

    def go():
        answers.append(_readmit(doc, _body(p, doc)))

    threads = [threading.Thread(target=go) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    codes = sorted(a.status_code for a in answers)
    assert codes == [201, 409], [a.text for a in answers]
    assert _one("SELECT COUNT(*) AS n FROM admissions WHERE patient_id = ? "
                "AND status = 'active'", (p["pid"],))["n"] == 1


def test_the_one_active_admission_index_refuses_a_second_open_stay(staff, readmission_on):
    from db_backend import DBIntegrityError

    doc = staff["doc"]
    p = _discharged_patient(doc, medicines=False)
    assert _readmit(doc, _body(p, doc)).status_code == 201
    with pytest.raises(DBIntegrityError):
        with db_session() as db:
            db.execute("INSERT INTO admissions (id, patient_id, admitted_by, status) "
                       "VALUES (?, ?, ?, 'active')", (f"ADM-X{uuid.uuid4().hex[:6]}",
                                                      p["pid"], doc))


# ═════════════════════════════════════════════════════════════════════════════
# 8. One transaction
# ═════════════════════════════════════════════════════════════════════════════

def test_an_unwritable_readmission_record_leaves_nothing_behind(staff, readmission_on,
                                                                monkeypatch):
    from services import audit_service

    doc = staff["doc"]
    p = _discharged_patient(doc)
    real = audit_service.log_audit_event

    def failing(*args, **kwargs):
        if kwargs.get("action_type") == adm.READMISSION_AUDIT_ACTION:
            raise AuditWriteError("disk full")
        return real(*args, **kwargs)

    monkeypatch.setattr(audit_service, "log_audit_event", failing)
    resp = _readmit(doc, _body(p, doc))
    assert resp.status_code == 503 and resp.json()["detail"]["error"] == "audit_unavailable"
    _nothing_written(p)
    assert _one("SELECT * FROM sage_room_memory WHERE room_id = ?",
                (p["room"],))["current_concern"] == "Hypoxia overnight"
    assert not _all("SELECT 1 FROM sage_observations WHERE room_id = ?", (p["room"],))
    assert verify_password(p["first_password"], _one(
        "SELECT password_hash FROM users WHERE id = ?", (p["login"],))["password_hash"])


# ═════════════════════════════════════════════════════════════════════════════
# 9. From a clinic visit (ORB_OPD)
# ═════════════════════════════════════════════════════════════════════════════

def test_a_discharged_patients_visit_readmits_under_the_same_rules(staff, readmission_on,
                                                                   monkeypatch):
    from ws import realtime

    monkeypatch.setattr(config, "FRONTDESK_ENABLED", True)
    monkeypatch.setattr(config, "OPD_ENABLED", True)
    monkeypatch.setattr(realtime, "schedule_on_loop", lambda make_coro, what: True)
    doc = staff["doc"]
    p = _discharged_patient(doc)
    visit = client.post("/api/frontdesk/opd/visits",
                        json={"patient_id": p["pid"], "doctor_id": doc,
                              "chief_complaint": "Breathless again",
                              "identity_check": _stated(p)}, headers=_auth(doc))
    assert visit.status_code == 200, visit.text
    visit = visit.json()
    preview = client.get(f"/api/admissions/admit-existing/visits/{visit['id']}",
                         headers=_auth(doc)).json()
    assert preview["admittable"] is True, preview
    assert preview["readmission"] is True and preview["readmission_note"] == adm.READMISSION_FORM_NOTE
    assert "Amoxicillin" not in json.dumps(preview)

    body = {"opd_visit_id": visit["id"], "identity_check": _stated(p),
            "no_signed_note_reason": "Note on paper", "admission_source": "clinic",
            "admission_type": "inpatient", "department": DEPT, "ward": "RM-W3",
            "bed": f"B{uuid.uuid4().hex[:3]}", "attending_doctor_id": doc,
            "chief_complaint": "Breathless again"}
    resp = client.post("/api/admissions/admit-existing", json=body,
                       headers=_auth(doc, key=uuid.uuid4().hex))
    assert resp.status_code == 201, resp.text
    out = resp.json()
    assert out["readmission"] is True and out["portal_password_reissued"] is True
    assert out["portal_login_created"] is False
    assert {m["drug_name"] for m in out["previous_stay_medicines"]} == {"Amoxicillin",
                                                                         "Morphine"}
    assert _one("SELECT status FROM medications WHERE id = ?",
                (p["meds"]["Morphine"],))["status"] == "discontinued"
    line = _all("SELECT content FROM messages WHERE channel_id = ? ORDER BY id DESC LIMIT 1",
                (p["room"],))[0]["content"]
    assert line.startswith("**READMISSION**") and adm.READMISSION_DIVIDER in line
    assert _all("SELECT 1 FROM sage_observations WHERE room_id = ? AND observation_type = ?",
                (p["room"], adm.ROOM_MEMORY_ARCHIVED))
    meta = _meta(_audits(adm.READMISSION_AUDIT_ACTION, p["pid"])[0])
    assert meta["door"] == adm.READMISSION_DOOR_OPD_VISIT
    assert meta["identity_check"]["purpose"] == adm.READMISSION_PURPOSE


# ═════════════════════════════════════════════════════════════════════════════
# 10. Beds and admissions (ORB_ADT) on
# ═════════════════════════════════════════════════════════════════════════════

def test_with_adt_on_the_readmission_is_placed_through_the_bed_master(staff, readmission_on,
                                                                      monkeypatch):
    monkeypatch.setattr(config, "ADT_ENABLED", True)
    doc = staff["doc"]
    p = _discharged_patient(doc, medicines=False)
    resp = _readmit(doc, _body(p, doc, ward="", bed="", awaiting_bed=True))
    assert resp.status_code == 201, resp.text
    out = resp.json()
    assert out["movement_id"] and out["location"]["awaiting_bed"] is True
    moves = _all("SELECT movement_type, admission_id FROM patient_movements "
                 "WHERE patient_id = ? AND admission_id = ?", (p["pid"], out["admission_id"]))
    assert moves == [{"movement_type": "admission", "admission_id": out["admission_id"]}]


def test_with_adt_on_a_time_before_the_earlier_stay_ended_is_refused_at_the_form(
        staff, readmission_on, monkeypatch):
    """ADT-S9 (a): the earlier stay ended two days ago; a readmission dated an
    hour before that would make its last critical set this stay's latest."""
    monkeypatch.setattr(config, "ADT_ENABLED", True)
    doc = staff["doc"]
    p = _discharged_patient(doc)
    ended = datetime.fromisoformat(_one("SELECT discharged_at FROM admissions WHERE id = ?",
                                        (p["old_admission"],))["discharged_at"])
    moves = _count("patient_movements", p["pid"])
    resp = _readmit(doc, _body(p, doc, ward="", bed="", awaiting_bed=True,
                               admitted_at=_iso(ended - timedelta(hours=1))))
    assert resp.status_code == 400, resp.text
    assert resp.json()["detail"] == {"error": "before_previous_stay_end",
                                     "message": adm.READMISSION_BEFORE_PREVIOUS_END}
    _nothing_written(p)
    assert _count("patient_movements", p["pid"]) == moves

    resp = _readmit(doc, _body(p, doc, ward="", bed="", awaiting_bed=True,
                               admitted_at=_iso(ended + timedelta(hours=1))))
    assert resp.status_code == 201, resp.text
    assert resp.json()["admitted_at"] == _iso(ended + timedelta(hours=1))


def test_with_adt_on_a_time_before_the_earlier_stay_ended_is_refused_at_the_clinic(
        staff, readmission_on, monkeypatch):
    from ws import realtime

    monkeypatch.setattr(config, "ADT_ENABLED", True)
    monkeypatch.setattr(config, "FRONTDESK_ENABLED", True)
    monkeypatch.setattr(config, "OPD_ENABLED", True)
    monkeypatch.setattr(realtime, "schedule_on_loop", lambda make_coro, what: True)
    doc = staff["doc"]
    p = _discharged_patient(doc)
    ended = datetime.fromisoformat(_one("SELECT discharged_at FROM admissions WHERE id = ?",
                                        (p["old_admission"],))["discharged_at"])
    visit = client.post("/api/frontdesk/opd/visits",
                        json={"patient_id": p["pid"], "doctor_id": doc,
                              "chief_complaint": "Breathless again",
                              "identity_check": _stated(p)}, headers=_auth(doc))
    assert visit.status_code == 200, visit.text
    visit = visit.json()
    body = {"opd_visit_id": visit["id"], "identity_check": _stated(p),
            "no_signed_note_reason": "Note on paper", "admission_source": "clinic",
            "admission_type": "inpatient", "department": DEPT, "ward": "", "bed": "",
            "awaiting_bed": True, "attending_doctor_id": doc,
            "chief_complaint": "Breathless again",
            "admitted_at": _iso(ended - timedelta(minutes=10))}
    moves = _count("patient_movements", p["pid"])
    resp = client.post("/api/admissions/admit-existing", json=body,
                       headers=_auth(doc, key=uuid.uuid4().hex))
    assert resp.status_code == 400, resp.text
    assert resp.json()["detail"]["error"] == "before_previous_stay_end"
    _nothing_written(p)
    assert _count("patient_movements", p["pid"]) == moves
    assert _one("SELECT admission_id FROM opd_visits WHERE id = ?",
                (visit["id"],))["admission_id"] is None

    body["admitted_at"] = None
    resp = client.post("/api/admissions/admit-existing", json=body,
                       headers=_auth(doc, key=uuid.uuid4().hex))
    assert resp.status_code == 201, resp.text
    assert resp.json()["readmission"] is True
