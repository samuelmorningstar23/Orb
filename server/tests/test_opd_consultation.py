"""The OPD consultation record (opd-10, W4-F, 4 October 2026).

What this file defends:

  1. A draft saves with row_version; a stale version is refused 409; signing
     makes the note final (the route and the database both refuse an edit);
     addenda append and never change.
  2. Diagnoses are free text: no code saves ('none'), an unknown code saves
     as 'unvalidated', a known code is 'validated' and, when ticked, joins the
     problem list at signing.
  3. OPD vitals stay inside the note with their time and author; they are
     range-checked, never in the future, and NOTHING is written to vitals,
     risk_scores or medications.
  4. Signing needs an allergy review (ruling OPD-S7, strictest option): with
     nothing charted it is refused 409 allergy_review_required.
  5. Completion with a disposition needs a signed note or a reason (ruling
     OPD-N8) and raises exactly one charge, however often it is repeated.
  6. Scope: another clinic's visit is 404; admin, nurse and receptionist are
     403; an admitted patient's OPD note is closed to writes; another clinic's
     draft is never shown.
  7. The record view and the chart timeline show signed notes and OPD visits
     only while the 'opd' feature is on.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import config
from auth import create_token
from database import db_session
from db_backend import DBIntegrityError
from main import app

client = TestClient(app)

DEPT_A = "Consult Test Medicine"
DEPT_B = "Consult Test Dermatology"


def _mk_user(role, department, name=None):
    uid = f"CTU{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO users (id, name, role, password_hash, department, is_active) "
            "VALUES (?, ?, ?, 'x', ?, 1)",
            (uid, name or f"Consult {role} {uid[-4:]}", role, department))
    return uid


def _auth(uid, role="doctor"):
    return {"Authorization": f"Bearer {create_token(uid, role)}"}


def _outpatient():
    pid = f"CTP{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO patients (id, name, mrn, age, sex, status, diagnosis) "
            "VALUES (?, ?, ?, 61, 'F', 'registered', 'Consult fixture')",
            (pid, f"Consult Patient {pid[-4:]}", f"MRN-{uuid.uuid4().hex[:8].upper()}"))
    return pid


def _chart_allergy(pid, substance="penicillin"):
    """A charted allergy AND the allergy review that recorded it (opd-10a):
    since W5-A a charted allergy with no review does not let a note be signed
    (ruling OPD-S7), so the signing tests below record both, as a doctor's
    earlier review would have."""
    with db_session() as db:
        db.execute("INSERT INTO patient_allergies (patient_id, substance, severity, status) "
                   "VALUES (?, ?, 'severe', 'active')", (pid, substance))
        db.execute("INSERT INTO patient_allergy_assessments (patient_id, status, "
                   "substances_json, source, assessed_by, assessed_at) "
                   "VALUES (?, 'allergies_recorded', ?, 'opd', 'CTREVIEWER', ?)",
                   (pid, json.dumps([substance]), datetime.now(timezone.utc).isoformat()))


def _stated_identity(pid):
    """What the patient states at the desk (opd-6a, ruling OPD-S1): with the
    registration feature on, every visit registered through the front desk
    carries it. Read from the fixture row: name, age and UHID."""
    from services import patient_matching as pm
    with db_session() as db:
        r = db.execute(
            "SELECT p.name, p.mrn, p.date_of_birth, p.age, p.created_at, d.age_recorded_on "
            "FROM patients p LEFT JOIN patient_demographics d ON d.patient_id = p.id "
            "WHERE p.id = ?", (pid,)).fetchone()
    if r is None:
        return {"name": "Not On File", "age_years": 40, "uhid": "NOT-ON-FILE"}
    age, _ = pm.current_age(date_of_birth=r["date_of_birth"], stated_age=r["age"],
                            age_recorded_on=r["age_recorded_on"], created_at=r["created_at"])
    return {"name": r["name"], "age_years": age, "uhid": r["mrn"]}


@pytest.fixture(scope="module")
def staff():
    from services.terminology_service import seed_terminology
    seed_terminology()
    return {
        "doc_a": _mk_user("doctor", DEPT_A, "Dr Meera Consult"),
        "doc_b": _mk_user("doctor", DEPT_B, "Dr Bhavna Consult"),
        "nurse_a": _mk_user("nurse", DEPT_A),
        "recep_a": _mk_user("receptionist", DEPT_A),
        "admin": _mk_user("admin", None),
    }


@pytest.fixture
def opd_on(monkeypatch):
    from ws import realtime
    monkeypatch.setattr(config, "FRONTDESK_ENABLED", True)
    monkeypatch.setattr(config, "REGISTRATION_ENABLED", True)
    monkeypatch.setattr(config, "OPD_ENABLED", True)
    monkeypatch.setattr(realtime, "schedule_on_loop", lambda make_coro, what: True)
    assert config.feature_enabled("opd")
    yield


@pytest.fixture
def opd_off(monkeypatch):
    monkeypatch.setattr(config, "FRONTDESK_ENABLED", True)
    monkeypatch.setattr(config, "OPD_ENABLED", False)
    yield


def _visit(doc, pid=None):
    pid = pid or _outpatient()
    resp = client.post("/api/frontdesk/opd/visits",
                       json={"patient_id": pid, "doctor_id": doc,
                             "identity_check": _stated_identity(pid)},
                       headers=_auth(doc))
    assert resp.status_code == 200, resp.text
    return resp.json()


def _put(vid, uid, body, role="doctor"):
    return client.put(f"/api/opd/visits/{vid}/consultation", json=body, headers=_auth(uid, role))


def _sign(vid, uid, version, role="doctor"):
    return client.post(f"/api/opd/visits/{vid}/consultation/sign",
                       json={"row_version": version}, headers=_auth(uid, role))


def _complete(vid, uid, body, role="doctor"):
    return client.post(f"/api/opd/visits/{vid}/complete", json=body, headers=_auth(uid, role))


def _counts(pid):
    with db_session() as db:
        return {t: db.execute(f"SELECT COUNT(*) FROM {t} WHERE patient_id = ?",
                              (pid,)).fetchone()[0]
                for t in ("vitals", "risk_scores", "medications")}


def _audit(action_type, patient_id):
    with db_session() as db:
        rows = db.execute("SELECT * FROM audit_logs WHERE action_type = ? AND patient_id = ? "
                          "ORDER BY id", (action_type, patient_id)).fetchall()
    return [dict(r) for r in rows]


def _meta(row):
    return json.loads(row["metadata_json"] or "{}")


_NOW = lambda: datetime.now(timezone.utc).isoformat()  # noqa: E731


# ═════════════════════════════════════════════════════════════════════════════
# 1. Off by default
# ═════════════════════════════════════════════════════════════════════════════

def test_every_consultation_route_is_404_with_the_feature_off(staff, opd_off):
    hdr = _auth(staff["doc_a"])
    for method, path in (("GET", "/api/opd/visits/X/consultation"),
                         ("PUT", "/api/opd/visits/X/consultation"),
                         ("POST", "/api/opd/visits/X/consultation/sign"),
                         ("POST", "/api/opd/visits/X/consultation/addenda"),
                         ("POST", "/api/opd/visits/X/complete"),
                         ("GET", "/api/opd/patients/X/consultations")):
        resp = client.request(method, path, json={}, headers=hdr)
        assert resp.status_code == 404, (method, path, resp.text)
        assert resp.json()["detail"]["error"] == "feature_disabled"


# ═════════════════════════════════════════════════════════════════════════════
# 2. Draft, save, sign, addenda
# ═════════════════════════════════════════════════════════════════════════════

def test_draft_sign_then_edit_refused(staff, opd_on):
    doc = staff["doc_a"]
    visit = _visit(doc)
    vid, pid = visit["id"], visit["patient_id"]
    _chart_allergy(pid)

    empty = client.get(f"/api/opd/visits/{vid}/consultation", headers=_auth(doc)).json()
    assert empty["consultation"] is None
    assert empty["allergy_review"]["can_sign"] is True

    first = _put(vid, doc, {"chief_complaint": "Cough for two weeks",
                            "diagnoses": [{"text": "Acute bronchitis"}]})
    assert first.status_code == 200, first.text
    note = first.json()
    assert note["status"] == "draft" and note["row_version"] == 1
    assert note["diagnoses"] == [{"text": "Acute bronchitis", "code_system": None,
                                  "code": None, "code_status": "none",
                                  "add_to_problems": False}]

    second = _put(vid, doc, {"row_version": 1, "plan": "Steam inhalation, review in a week"})
    assert second.status_code == 200, second.text
    assert second.json()["row_version"] == 2
    assert second.json()["chief_complaint"] == "Cough for two weeks"

    stale = _put(vid, doc, {"row_version": 1, "plan": "Overwrites"})
    assert stale.status_code == 409
    assert stale.json()["detail"]["error"] == "stale_version"
    assert stale.json()["detail"]["current_version"] == 2

    stale_sign = _sign(vid, doc, 1)
    assert stale_sign.status_code == 409 and stale_sign.json()["detail"]["error"] == "stale_version"
    signed = _sign(vid, doc, 2)
    assert signed.status_code == 200, signed.text
    body = signed.json()
    assert body["status"] == "signed" and body["signed_by"] == doc
    assert body["signed_by_name"] == "Dr Meera Consult" and body["signed_at"]

    edit = _put(vid, doc, {"row_version": body["row_version"], "plan": "Changed"})
    assert edit.status_code == 409 and edit.json()["detail"]["error"] == "consultation_signed"
    again = _sign(vid, doc, body["row_version"])
    assert again.status_code == 409
    with pytest.raises(DBIntegrityError, match="signed"):
        with db_session() as db:
            db.execute("UPDATE opd_consultations SET plan = 'tampered' WHERE visit_id = ?", (vid,))

    for action in ("opd_consultation_saved", "opd_consultation_signed"):
        rows = _audit(action, pid)
        assert rows and all(_meta(r)["written"] == "in_transaction" for r in rows), action
    assert [_meta(r)["created"] for r in _audit("opd_consultation_saved", pid)] == [True, False]


def test_addenda_are_append_only(staff, opd_on):
    doc = staff["doc_a"]
    visit = _visit(doc)
    vid, pid = visit["id"], visit["patient_id"]
    _chart_allergy(pid)
    _put(vid, doc, {"examination": "Chest clear"})
    early = client.post(f"/api/opd/visits/{vid}/consultation/addenda",
                        json={"body": "Too early"}, headers=_auth(doc))
    assert early.status_code == 409 and early.json()["detail"]["error"] == "not_signed"
    assert _sign(vid, doc, 1).status_code == 200
    blank = client.post(f"/api/opd/visits/{vid}/consultation/addenda",
                        json={"body": "   "}, headers=_auth(doc))
    assert blank.status_code == 422
    for text in ("Sputum culture sent", "Culture negative"):
        resp = client.post(f"/api/opd/visits/{vid}/consultation/addenda",
                           json={"body": text}, headers=_auth(doc))
        assert resp.status_code == 200, resp.text
    note = resp.json()
    assert [a["body"] for a in note["addenda"]] == ["Sputum culture sent", "Culture negative"]
    assert note["addenda"][0]["author_name"] == "Dr Meera Consult"
    with pytest.raises(DBIntegrityError, match="append-only"):
        with db_session() as db:
            db.execute("UPDATE opd_consultation_addenda SET body = 'edited' WHERE visit_id = ?",
                       (vid,))
    assert len(_audit("opd_consultation_addendum_added", pid)) == 2


# ═════════════════════════════════════════════════════════════════════════════
# 3. Diagnoses and the problem list
# ═════════════════════════════════════════════════════════════════════════════

def test_diagnoses_free_text_unknown_and_known_codes(staff, opd_on):
    doc = staff["doc_a"]
    visit = _visit(doc)
    vid, pid = visit["id"], visit["patient_id"]
    _chart_allergy(pid, "sulfonamides")
    resp = _put(vid, doc, {"diagnoses": [
        {"text": "Essential hypertension", "code": "I10", "add_to_problems": True},
        {"text": "Something rare", "code_system": "ICD10", "code": "Z99.999"},
        "Anxiety about results",
    ]})
    assert resp.status_code == 200, resp.text
    statuses = [(d["code"], d["code_status"], d["add_to_problems"]) for d in resp.json()["diagnoses"]]
    assert statuses == [("I10", "validated", True), ("Z99.999", "unvalidated", False),
                        (None, "none", False)]
    bad = _put(vid, doc, {"row_version": 1, "diagnoses": [{"code": "I10"}]})
    assert bad.status_code == 422, "a code with no words is not a diagnosis"
    bad_system = _put(vid, doc, {"row_version": 1,
                                 "diagnoses": [{"text": "X", "code_system": "LOINC", "code": "1"}]})
    assert bad_system.status_code == 422

    signed = _sign(vid, doc, 1)
    assert signed.status_code == 200, signed.text
    with db_session() as db:
        problems = db.execute("SELECT problem, icd10_code, recorded_by FROM problem_list "
                              "WHERE patient_id = ?", (pid,)).fetchall()
    assert [tuple(p) for p in problems] == [("Essential hypertension", "I10", doc)]
    assert _meta(_audit("opd_consultation_signed", pid)[-1])["problem_ids"]


# ═════════════════════════════════════════════════════════════════════════════
# 4. OPD vitals are not vitals
# ═════════════════════════════════════════════════════════════════════════════

def test_opd_vitals_stay_in_the_note_and_nothing_is_scored(staff, opd_on):
    doc = staff["doc_a"]
    visit = _visit(doc)
    vid, pid = visit["id"], visit["patient_id"]
    _chart_allergy(pid)
    before = _counts(pid)
    measured = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()

    no_time = _put(vid, doc, {"vitals": {"values": {"heart_rate": 88}}})
    assert no_time.status_code == 422
    future = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
    assert _put(vid, doc, {"vitals": {"values": {"heart_rate": 88},
                                      "measured_at": future}}).status_code == 422
    assert _put(vid, doc, {"vitals": {"values": {"spo2": 140},
                                      "measured_at": measured}}).status_code == 422
    assert _put(vid, doc, {"vitals": {"values": {"news2": 3},
                                      "measured_at": measured}}).status_code == 422

    ok = _put(vid, doc, {"vitals": {"values": {"heart_rate": 88, "systolic_bp": 142,
                                               "diastolic_bp": 90, "spo2": 97,
                                               "temperature": 37.4},
                                    "measured_at": measured},
                         "plan": "Review"})
    assert ok.status_code == 200, ok.text
    vitals = ok.json()["vitals"]
    assert vitals["values"] == {"heart_rate": 88, "systolic_bp": 142, "diastolic_bp": 90,
                                "spo2": 97, "temperature": 37.4}
    assert vitals["measured_by"] == doc and vitals["measured_by_name"] == "Dr Meera Consult"
    assert vitals["measured_at"].endswith("+00:00")
    text = json.dumps(ok.json()).lower()
    for word in ("news2", "band", "risk", "score"):
        assert word not in text, word

    assert _sign(vid, doc, 1).status_code == 200
    done = _complete(vid, doc, {"disposition": "home"})
    assert done.status_code == 200, done.text
    assert _counts(pid) == before == {"vitals": 0, "risk_scores": 0, "medications": 0}


# ═════════════════════════════════════════════════════════════════════════════
# 5. Signing needs an allergy review (OPD-S7, strict)
# ═════════════════════════════════════════════════════════════════════════════

def test_signing_is_refused_until_allergies_are_charted(staff, opd_on):
    doc = staff["doc_a"]
    visit = _visit(doc)
    vid, pid = visit["id"], visit["patient_id"]
    _put(vid, doc, {"plan": "Paracetamol as needed"})
    view = client.get(f"/api/opd/visits/{vid}/consultation", headers=_auth(doc)).json()
    # W5-A (opd-10a) added reviewed_this_visit, basis and assessment.
    assert {k: view["allergy_review"][k] for k in ("status", "can_sign", "charted_count")} \
        == {"status": "not_reviewed", "can_sign": False, "charted_count": 0}
    assert view["allergy_review"]["assessment"] is None
    refused = _sign(vid, doc, 1)
    assert refused.status_code == 409
    assert refused.json()["detail"]["error"] == "allergy_review_required"
    with db_session() as db:
        assert db.execute("SELECT status FROM opd_consultations WHERE visit_id = ?",
                          (vid,)).fetchone()[0] == "draft"
    _chart_allergy(pid, "aspirin")
    assert _sign(vid, doc, 1).status_code == 200


def test_an_empty_note_cannot_be_signed(staff, opd_on):
    doc = staff["doc_a"]
    visit = _visit(doc)
    _chart_allergy(visit["patient_id"])
    _put(visit["id"], doc, {"advice": "Drink water"})
    resp = _sign(visit["id"], doc, 1)
    assert resp.status_code == 422 and resp.json()["detail"]["error"] == "empty_note"


# ═════════════════════════════════════════════════════════════════════════════
# 6. Completion with a disposition (OPD-N8)
# ═════════════════════════════════════════════════════════════════════════════

def _charges(pid):
    with db_session() as db:
        return [dict(r) for r in db.execute(
            "SELECT id, encounter_type, source_ref FROM encounter_charges WHERE patient_id = ?",
            (pid,)).fetchall()]


def test_completion_needs_a_signed_note_or_a_reason_and_raises_one_charge(staff, opd_on):
    doc = staff["doc_a"]
    visit = _visit(doc)
    vid, pid = visit["id"], visit["patient_id"]
    client.post(f"/api/frontdesk/opd/visits/{vid}/call", headers=_auth(doc))

    bad = _complete(vid, doc, {"disposition": "admitted"})
    assert bad.status_code == 422 and bad.json()["detail"]["error"] == "bad_disposition"
    refused = _complete(vid, doc, {"disposition": "home"})
    assert refused.status_code == 409
    assert refused.json()["detail"]["error"] == "signed_note_required"
    assert _charges(pid) == []

    done = _complete(vid, doc, {"disposition": "follow_up",
                                "no_signed_note_reason": "Note written on paper"})
    assert done.status_code == 200, done.text
    body = done.json()
    assert body["status"] == "completed" and body["disposition"] == "follow_up"
    assert body["consultation_charge"]["charge_id"]
    for _ in range(2):
        assert _complete(vid, doc, {"disposition": "home"}).status_code == 200
    charges = _charges(pid)
    assert len(charges) == 1 and charges[0]["encounter_type"] == "op"
    assert charges[0]["source_ref"] == vid
    with db_session() as db:
        assert db.execute("SELECT disposition FROM opd_visits WHERE id = ?",
                          (vid,)).fetchone()[0] == "follow_up"
    rows = _audit("opd_visit_completed", pid)
    first = _meta(rows[0])
    assert first["signed_note"] is False
    assert first["no_signed_note_reason"] == "Note written on paper"
    assert first["disposition"] == "follow_up" and first["written"] == "in_transaction"
    assert [_meta(r)["replay"] for r in rows] == [False, True, True]


def test_a_signed_note_completes_without_a_reason(staff, opd_on):
    doc = staff["doc_a"]
    visit = _visit(doc)
    vid, pid = visit["id"], visit["patient_id"]
    _chart_allergy(pid)
    _put(vid, doc, {"diagnoses": ["Viral fever"]})
    assert _sign(vid, doc, 1).status_code == 200
    done = _complete(vid, doc, {"disposition": "home"})
    assert done.status_code == 200, done.text
    assert len(_charges(pid)) == 1
    assert _meta(_audit("opd_visit_completed", pid)[0])["signed_note"] is True


# ═════════════════════════════════════════════════════════════════════════════
# 7. Scope and permission
# ═════════════════════════════════════════════════════════════════════════════

def test_another_clinic_gets_404_and_other_roles_403(staff, opd_on):
    doc = staff["doc_a"]
    visit = _visit(doc)
    vid = visit["id"]
    _put(vid, doc, {"plan": "x"})
    other = staff["doc_b"]
    assert client.get(f"/api/opd/visits/{vid}/consultation", headers=_auth(other)).status_code == 404
    assert _put(vid, other, {"row_version": 1, "plan": "y"}).status_code == 404
    assert _sign(vid, other, 1).status_code == 404
    assert _complete(vid, other, {"disposition": "home",
                                  "no_signed_note_reason": "paper"}).status_code == 404
    assert client.post(f"/api/opd/visits/{vid}/consultation/addenda", json={"body": "z"},
                       headers=_auth(other)).status_code == 404
    for who, role in (("admin", "admin"), ("nurse_a", "nurse"), ("recep_a", "receptionist")):
        hdr = _auth(staff[who], role)
        assert client.get(f"/api/opd/visits/{vid}/consultation", headers=hdr).status_code == 403
        assert _put(vid, staff[who], {"plan": "z"}, role).status_code == 403
        assert _sign(vid, staff[who], 1, role).status_code == 403
        assert _complete(vid, staff[who], {"disposition": "home"}, role).status_code == 403
        assert client.get(f"/api/opd/patients/{visit['patient_id']}/consultations",
                          headers=hdr).status_code == 403


def test_an_admitted_patients_outpatient_note_is_closed_to_writes(staff, opd_on):
    doc = staff["doc_a"]
    visit = _visit(doc)
    vid, pid = visit["id"], visit["patient_id"]
    _put(vid, doc, {"plan": "Admit if worse"})
    with db_session() as db:
        db.execute("INSERT INTO admissions (id, patient_id, admitted_by, department, ward, bed, "
                   "status, created_at) VALUES (?, ?, ?, ?, 'Bay', '3', 'active', ?)",
                   (f"CTA{uuid.uuid4().hex[:8]}", pid, doc, DEPT_A, _NOW()))
        db.execute("UPDATE patients SET status = 'admitted' WHERE id = ?", (pid,))
    try:
        resp = _put(vid, doc, {"row_version": 1, "plan": "Changed"})
        assert resp.status_code == 409 and resp.json()["detail"]["error"] == "patient_admitted"
        resp = _complete(vid, doc, {"disposition": "home", "no_signed_note_reason": "paper"})
        assert resp.status_code == 409 and resp.json()["detail"]["error"] == "patient_admitted"
        # Still readable by the clinic that wrote it.
        assert client.get(f"/api/opd/visits/{vid}/consultation",
                          headers=_auth(doc)).status_code == 200
    finally:
        with db_session() as db:
            db.execute("UPDATE admissions SET status = 'discharged' WHERE patient_id = ?", (pid,))
            db.execute("UPDATE patients SET status = 'discharged' WHERE id = ?", (pid,))


def test_the_patient_list_shows_signed_notes_of_every_clinic_and_own_drafts(staff, opd_on):
    doc_a, doc_b = staff["doc_a"], staff["doc_b"]
    pid = _outpatient()
    _chart_allergy(pid)
    va = _visit(doc_a, pid)
    vb = _visit(doc_b, pid)
    _put(va["id"], doc_a, {"plan": "Medicine draft only"})
    _put(vb["id"], doc_b, {"plan": "Dermatology plan", "diagnoses": ["Eczema"],
                           "vitals": {"values": {"heart_rate": 72},
                                      "measured_at": _NOW()}})
    assert _sign(vb["id"], doc_b, 1).status_code == 200

    seen_by_a = client.get(f"/api/opd/patients/{pid}/consultations", headers=_auth(doc_a))
    assert seen_by_a.status_code == 200, seen_by_a.text
    by_visit = {c["visit_id"]: c for c in seen_by_a.json()["consultations"]}
    assert set(by_visit) == {va["id"], vb["id"]}
    assert by_visit[vb["id"]]["status"] == "signed" and by_visit[vb["id"]]["own_clinic"] is False
    assert by_visit[va["id"]]["status"] == "draft" and by_visit[va["id"]]["own_clinic"] is True

    # Dermatology sees its own signed note and not Medicine's draft.
    seen_by_b = client.get(f"/api/opd/patients/{pid}/consultations", headers=_auth(doc_b)).json()
    assert [c["visit_id"] for c in seen_by_b["consultations"]] == [vb["id"]]

    rows = _audit("opd_consultations_viewed", pid)
    assert rows and all(r["event_category"] == "phi_access" for r in rows)
    assert _meta(rows[-1])["opd_arm"] is True

    # A clinic with no visit for this patient cannot read the list.
    stranger = _mk_user("doctor", "Consult Test Stranger Clinic")
    assert client.get(f"/api/opd/patients/{pid}/consultations",
                      headers=_auth(stranger)).status_code == 404

    # The record view lists the signed note as a summary without its vitals.
    record = client.get(f"/api/opd/patients/{pid}/record", headers=_auth(doc_a)).json()
    assert record["consultations_available"] is True
    assert [c["visit_id"] for c in record["consultations"]] == [vb["id"]]
    summary = record["consultations"][0]
    assert summary["diagnoses"] == ["Eczema"] and "vitals" not in summary
    assert summary["signed_by_name"] == "Dr Bhavna Consult"


# ═════════════════════════════════════════════════════════════════════════════
# 8. The chart timeline
# ═════════════════════════════════════════════════════════════════════════════

def test_the_timeline_shows_opd_visits_and_signed_notes_only_with_the_feature(
        staff, opd_on, monkeypatch):
    from services.patient_timeline_service import build_chart_timeline

    doc = staff["doc_a"]
    visit = _visit(doc)
    pid = visit["patient_id"]
    _chart_allergy(pid)
    _put(visit["id"], doc, {"diagnoses": ["Migraine"], "plan": "Sumatriptan trial"})
    with_on = build_chart_timeline(pid)
    kinds = [e["kind"] for e in with_on["events"]]
    assert "opd_visit" in kinds and "opd_consultation" not in kinds, "a draft is not on the rail"
    assert _sign(visit["id"], doc, 1).status_code == 200
    events = build_chart_timeline(pid)["events"]
    note = next(e for e in events if e["kind"] == "opd_consultation")
    assert note["title"] == f"Outpatient note signed: {DEPT_A}"
    assert "Migraine" in note["detail"] and note["badge"] == "OPD"
    opd = next(e for e in events if e["kind"] == "opd_visit")
    assert opd["title"] == f"Outpatient visit: {DEPT_A}"

    monkeypatch.setattr(config, "OPD_ENABLED", False)
    kinds_off = [e["kind"] for e in build_chart_timeline(pid)["events"]]
    assert "opd_visit" not in kinds_off and "opd_consultation" not in kinds_off
