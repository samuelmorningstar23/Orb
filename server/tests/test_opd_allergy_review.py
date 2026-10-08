"""The OPD allergy review (opd-10a, W5-A, ruling OPD-S7, migration 63).

An empty allergy list means "no known allergies" or "never asked", and the
interlocks cannot tell them apart. The consulting doctor's allergy review says
which, in patient_allergy_assessments (append-only), and charts what the
patient names through clinical_records_service.record_allergies_on in the same
transaction.

What this file defends:

  1. OFF BY DEFAULT: the route is 404 feature_disabled with ORB_OPD off.
  2. A CHARTED ALLERGY IS SEEN BY THE INTERLOCK: after a review naming
     penicillin, enforce_medication_safety stops amoxicillin for the patient
     (it did not before). An allergy on the chart but marked inactive is made
     active again when the patient names it.
  3. 'NONE KNOWN' charts nothing and is recorded with the clinician and the
     time; it is refused while an allergy is active on the chart. 'nkda'
     typed as an allergen is never charted.
  4. 'UNABLE TO OBTAIN' needs a reason and names nothing.
  5. WHO: a receptionist and an administrator get 403; another clinic's
     visit is 404; an admitted patient's visit is closed (409).
  6. THE RECORD IS APPEND-ONLY: the database refuses an UPDATE of a review;
     DELETE is left to a signed erasure, like migrations 60 to 62.
  7. SIGNING FOLLOWS OPD-S7: no review, no signature; a charted allergy with
     no review does not count; a review on this visit counts whatever it
     says; an earlier 'allergies recorded' or 'no known allergies' counts, an
     earlier 'unable to obtain' does not, and a 'no known allergies' the
     chart now contradicts does not.
  8. The audit row is written in the review's transaction, and the record
     view says who asked and when.
  9. AN OLD VISIT ID READS NOTHING (review round 1, 9 October 2026): visit
     ids of any date reach the caller's clinic, while the record view is
     limited by the OPD arm. A review is refused on a visit outside the OPD
     scope window (409 visit_not_current, before the chart is read), and the
     visit-addressed answers (the consultation read, the signing refusal, the
     review's own answer) carry a review as status, label and time only: no
     allergen, note, reason, clinic or clinician.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import config
from auth import create_token
from database import db_session
from db_backend import DBIntegrityError
from main import app
from services.hospital_time import hospital_today

client = TestClient(app)

DEPT_A = "Allergy Review Medicine"
DEPT_B = "Allergy Review Dermatology"


def _mk_user(role, department, name=None):
    uid = f"ARU{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO users (id, name, role, password_hash, department, is_active) "
            "VALUES (?, ?, ?, 'x', ?, 1)",
            (uid, name or f"Allergy {role} {uid[-4:]}", role, department))
    return uid


def _auth(uid, role="doctor"):
    return {"Authorization": f"Bearer {create_token(uid, role)}"}


def _outpatient():
    pid = f"ARP{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO patients (id, name, mrn, age, sex, status, diagnosis) "
            "VALUES (?, ?, ?, 58, 'F', 'registered', 'Allergy review fixture')",
            (pid, f"Allergy Patient {pid[-4:]}", f"MRN-{uuid.uuid4().hex[:8].upper()}"))
    return pid


def _visit(pid, dept=DEPT_A, *, days_ago=0):
    vid = f"OPD-AR{uuid.uuid4().hex[:6].upper()}"
    day = (hospital_today() - timedelta(days=days_ago)).isoformat()
    with db_session() as db:
        db.execute(
            "INSERT INTO opd_visits (id, patient_id, department, visit_date, token_number, "
            "status, registered_by, registered_at) VALUES (?, ?, ?, ?, ?, 'in_consultation', "
            "NULL, ?)",
            (vid, pid, dept, day, 100 + abs(hash(vid)) % 9000,
             datetime.now(timezone.utc).isoformat()))
    return vid


def _review(vid, uid, body, role="doctor"):
    return client.post(f"/api/opd/visits/{vid}/allergy-review", json=body,
                       headers=_auth(uid, role))


def _put(vid, uid, body):
    return client.put(f"/api/opd/visits/{vid}/consultation", json=body, headers=_auth(uid))


def _sign(vid, uid, version):
    return client.post(f"/api/opd/visits/{vid}/consultation/sign",
                       json={"row_version": version}, headers=_auth(uid))


def _draft_and_sign(vid, uid):
    saved = _put(vid, uid, {"plan": "Review in a week"})
    assert saved.status_code == 200, saved.text
    return _sign(vid, uid, saved.json()["row_version"])


def _allergies(pid, status=None):
    with db_session() as db:
        sql = "SELECT substance, status, source, recorded_by FROM patient_allergies WHERE patient_id = ?"
        args = [pid]
        if status:
            sql += " AND status = ?"
            args.append(status)
        return [dict(r) for r in db.execute(sql + " ORDER BY substance", args).fetchall()]


def _assessments(pid):
    with db_session() as db:
        return [dict(r) for r in db.execute(
            "SELECT * FROM patient_allergy_assessments WHERE patient_id = ? ORDER BY id",
            (pid,)).fetchall()]


def _audit(action_type, patient_id):
    with db_session() as db:
        rows = db.execute("SELECT * FROM audit_logs WHERE action_type = ? AND patient_id = ? "
                          "ORDER BY id", (action_type, patient_id)).fetchall()
    return [dict(r) for r in rows]


@pytest.fixture(scope="module")
def staff():
    return {
        "doc_a": _mk_user("doctor", DEPT_A, "Dr Meera Allergy"),
        "doc_b": _mk_user("doctor", DEPT_B, "Dr Bose Allergy"),
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


# ── 1. Off by default ────────────────────────────────────────────────────────

def test_the_route_is_404_with_the_feature_off(staff, opd_off):
    resp = _review("OPD-X", staff["doc_a"], {"status": "none_known"})
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "feature_disabled"


# ── 2. The interlock sees what the review charts ─────────────────────────────

def _amoxicillin_blocked(pid, doc) -> bool:
    from services.medication_safety_gate import enforce_medication_safety
    try:
        enforce_medication_safety(pid, "Amoxicillin 500mg", actor_id=doc, actor_role="doctor",
                                  entry_point="opd_prescription")
    except HTTPException as exc:
        assert exc.status_code == 428
        assert "penicillin" in json.dumps(exc.detail).lower()
        return True
    return False


def test_a_charted_penicillin_allergy_is_seen_by_the_medication_gate(staff, opd_on):
    doc = staff["doc_a"]
    pid = _outpatient()
    vid = _visit(pid)
    assert _amoxicillin_blocked(pid, doc) is False
    resp = _review(vid, doc, {"status": "allergies_recorded", "substances": ["Penicillin"]})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["charted"] == ["penicillin"]
    # The visit-addressed answer carries the review as status, label and
    # time; the allergy list and the full review are on the record view.
    assert "allergies" not in body
    assert set(body["assessment"]) == {"status", "status_label", "assessed_at"}
    assert body["assessment"]["status"] == "allergies_recorded"
    record = client.get(f"/api/opd/patients/{pid}/record", headers=_auth(doc)).json()
    assert record["allergy_assessment"]["substances"] == ["penicillin"]
    assert record["allergy_assessment"]["assessed_by_name"] == "Dr Meera Allergy"
    assert _allergies(pid) == [{"substance": "penicillin", "status": "active", "source": "opd",
                                "recorded_by": doc}]
    assert _amoxicillin_blocked(pid, doc) is True


def test_free_text_splits_into_allergens_and_an_existing_one_is_kept(staff, opd_on):
    doc = staff["doc_a"]
    pid = _outpatient()
    with db_session() as db:
        db.execute("INSERT INTO patient_allergies (patient_id, substance, reaction, severity, "
                   "status, source) VALUES (?, 'sulfa', 'rash', 'moderate', 'active', 'clinician')",
                   (pid,))
    resp = _review(_visit(pid), doc, {"status": "allergies_recorded",
                                      "substances": ["aspirin; Sulfa"]})
    assert resp.status_code == 200, resp.text
    assert resp.json()["charted"] == ["aspirin"]
    with db_session() as db:
        sulfa = db.execute("SELECT reaction, severity, source FROM patient_allergies "
                           "WHERE patient_id = ? AND substance = 'sulfa'", (pid,)).fetchone()
    assert dict(sulfa) == {"reaction": "rash", "severity": "moderate", "source": "clinician"}
    assert json.loads(_assessments(pid)[0]["substances_json"]) == ["aspirin", "sulfa"]


def test_an_inactive_allergy_the_patient_names_is_made_active_again(staff, opd_on):
    doc = staff["doc_a"]
    pid = _outpatient()
    with db_session() as db:
        db.execute("INSERT INTO patient_allergies (patient_id, substance, severity, status, "
                   "source) VALUES (?, 'penicillin', 'unknown', 'inactive', 'clinician')", (pid,))
    assert _amoxicillin_blocked(pid, doc) is False
    resp = _review(_visit(pid), doc, {"status": "allergies_recorded", "substances": ["penicillin"]})
    assert resp.status_code == 200, resp.text
    assert resp.json()["reactivated"] == ["penicillin"]
    assert _allergies(pid, "active")[0]["substance"] == "penicillin"
    assert _amoxicillin_blocked(pid, doc) is True


def test_confirming_the_charted_allergies_needs_no_new_name(staff, opd_on):
    doc = staff["doc_a"]
    pid = _outpatient()
    with db_session() as db:
        db.execute("INSERT INTO patient_allergies (patient_id, substance, severity, status) "
                   "VALUES (?, 'ibuprofen', 'mild', 'active')", (pid,))
    resp = _review(_visit(pid), doc, {"status": "allergies_recorded"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["charted"] == []
    assert "ibuprofen" not in resp.text.lower()
    assert json.loads(_assessments(pid)[0]["substances_json"]) == ["ibuprofen"]


# ── 3. None known ────────────────────────────────────────────────────────────

def test_none_known_charts_nothing_and_names_the_clinician(staff, opd_on):
    doc = staff["doc_a"]
    pid = _outpatient()
    vid = _visit(pid)
    resp = _review(vid, doc, {"status": "none_known", "note": "Asked the patient and daughter"})
    assert resp.status_code == 200, resp.text
    assert _allergies(pid) == []
    (row,) = _assessments(pid)
    assert row["status"] == "none_known"
    assert row["assessed_by"] == doc
    assert row["opd_visit_id"] == vid and row["department"] == DEPT_A
    assert row["substances_json"] is None and row["reason"] is None
    assert row["note"] == "Asked the patient and daughter"
    assert row["assessed_at"].endswith("Z") or "+00:00" in row["assessed_at"]
    record = client.get(f"/api/opd/patients/{pid}/record", headers=_auth(doc)).json()
    assert record["allergy_status"] == "none_recorded"
    assert record["allergy_assessment"]["status"] == "none_known"
    assert record["allergy_assessment"]["assessed_by_name"] == "Dr Meera Allergy"


def test_the_record_says_not_yet_asked_before_any_review(staff, opd_on):
    pid = _outpatient()
    _visit(pid)
    record = client.get(f"/api/opd/patients/{pid}/record", headers=_auth(staff["doc_a"])).json()
    assert record["allergy_assessment"] is None
    assert record["allergy_status"] == "none_recorded"


def test_nkda_typed_as_an_allergen_is_never_charted(staff, opd_on):
    doc = staff["doc_a"]
    pid = _outpatient()
    vid = _visit(pid)
    refused = _review(vid, doc, {"status": "allergies_recorded", "substances": ["NKDA", "nil"]})
    assert refused.status_code == 422
    assert refused.json()["detail"]["error"] == "no_allergies_named"
    assert _allergies(pid) == [] and _assessments(pid) == []
    ok = _review(vid, doc, {"status": "none_known", "substances": ["nkda"]})
    assert ok.status_code == 200, ok.text
    assert _allergies(pid) == []


def test_none_known_is_refused_while_an_allergy_is_on_the_chart(staff, opd_on):
    doc = staff["doc_a"]
    pid = _outpatient()
    with db_session() as db:
        db.execute("INSERT INTO patient_allergies (patient_id, substance, severity, status) "
                   "VALUES (?, 'latex', 'severe', 'active')", (pid,))
    resp = _review(_visit(pid), doc, {"status": "none_known"})
    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "allergies_on_chart"
    assert resp.json()["detail"]["charted"] == ["latex"]
    assert _assessments(pid) == []


def test_none_known_with_an_allergen_named_is_refused(staff, opd_on):
    resp = _review(_visit(_outpatient()), staff["doc_a"],
                   {"status": "none_known", "substances": ["penicillin"]})
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"] == "substances_not_allowed"


# ── 4. Unable to obtain ──────────────────────────────────────────────────────

def test_unable_to_obtain_needs_a_reason_and_names_nothing(staff, opd_on):
    doc = staff["doc_a"]
    pid = _outpatient()
    vid = _visit(pid)
    assert _review(vid, doc, {"status": "unable_to_obtain"}).json()["detail"]["error"] \
        == "reason_required"
    named = _review(vid, doc, {"status": "unable_to_obtain", "reason": "Unconscious",
                               "substances": ["penicillin"]})
    assert named.json()["detail"]["error"] == "substances_not_allowed"
    ok = _review(vid, doc, {"status": "unable_to_obtain",
                            "reason": "Patient aphasic, no relative present"})
    assert ok.status_code == 200, ok.text
    (row,) = _assessments(pid)
    assert row["reason"] == "Patient aphasic, no relative present"
    assert _allergies(pid) == []


def test_an_unknown_status_or_field_is_refused(staff, opd_on):
    vid = _visit(_outpatient())
    assert _review(vid, staff["doc_a"], {"status": "probably_fine"}).status_code == 422
    assert _review(vid, staff["doc_a"], {"status": "none_known", "severity": "mild"}).status_code \
        == 422


# ── 5. Who may record one ────────────────────────────────────────────────────

def test_a_receptionist_and_an_administrator_get_403(staff, opd_on):
    pid = _outpatient()
    vid = _visit(pid)
    assert _review(vid, staff["recep_a"], {"status": "none_known"}, role="receptionist") \
        .status_code == 403
    assert _review(vid, staff["admin"], {"status": "none_known"}, role="admin").status_code == 403
    assert _assessments(pid) == []


def test_another_clinics_visit_is_404(staff, opd_on):
    pid = _outpatient()
    resp = _review(_visit(pid, DEPT_A), staff["doc_b"], {"status": "none_known"})
    assert resp.status_code == 404
    assert _assessments(pid) == []


def test_an_admitted_patients_visit_is_closed(staff, opd_on):
    pid = _outpatient()
    vid = _visit(pid)
    with db_session() as db:
        db.execute("INSERT INTO admissions (id, patient_id, admitted_by, department, ward, bed, "
                   "status, created_at) VALUES (?, ?, ?, ?, 'Bay', '1', 'active', ?)",
                   (f"ARA{uuid.uuid4().hex[:8].upper()}", pid, staff["doc_a"], DEPT_A,
                    datetime.now(timezone.utc).isoformat()))
    resp = _review(vid, staff["doc_a"], {"status": "none_known"})
    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "patient_admitted"


# ── 6. Append-only ───────────────────────────────────────────────────────────

def test_a_review_cannot_be_updated(staff, opd_on):
    pid = _outpatient()
    assert _review(_visit(pid), staff["doc_a"], {"status": "none_known"}).status_code == 200
    with pytest.raises(DBIntegrityError):
        with db_session() as db:
            db.execute("UPDATE patient_allergy_assessments SET status = 'allergies_recorded' "
                       "WHERE patient_id = ?", (pid,))
    assert _assessments(pid)[0]["status"] == "none_known"


def test_only_a_signed_erasure_removes_a_review(staff, opd_on):
    """No route deletes a review; the schema-derived erase plan (a retention
    order signed by a person) can, like migrations 60 to 62 chose."""
    from routes.admin_routes import _build_erase_plan
    with db_session() as db:
        planned = {table for table, _, _ in _build_erase_plan(db)}
    assert "patient_allergy_assessments" in planned
    for route in app.routes:
        if "allergy-review" in getattr(route, "path", ""):
            assert getattr(route, "methods", set()) == {"POST"}


# ── 7. Signing follows OPD-S7 ────────────────────────────────────────────────

def test_no_review_no_signature_and_a_charted_allergy_alone_does_not_count(staff, opd_on):
    doc = staff["doc_a"]
    pid = _outpatient()
    vid = _visit(pid)
    refused = _draft_and_sign(vid, doc)
    assert refused.status_code == 409
    assert refused.json()["detail"]["error"] == "allergy_review_required"
    with db_session() as db:
        db.execute("INSERT INTO patient_allergies (patient_id, substance, severity, status) "
                   "VALUES (?, 'aspirin', 'unknown', 'active')", (pid,))
    view = client.get(f"/api/opd/visits/{vid}/consultation", headers=_auth(doc)).json()
    assert view["allergy_review"]["can_sign"] is False
    assert view["allergy_review"]["status"] == "not_reviewed"
    assert view["allergy_review"]["charted_count"] == 1
    assert _sign(vid, doc, 1).status_code == 409
    assert _review(vid, doc, {"status": "allergies_recorded"}).status_code == 200
    signed = _sign(vid, doc, 1)
    assert signed.status_code == 200, signed.text
    (row,) = _audit("opd_consultation_signed", pid)
    meta = json.loads(row["metadata_json"])
    assert meta["allergy_review"] == "allergies_recorded"
    assert meta["allergy_review_basis"] == "this_visit"


def test_a_review_on_this_visit_counts_whatever_it_says(staff, opd_on):
    doc = staff["doc_a"]
    pid = _outpatient()
    vid = _visit(pid)
    assert _review(vid, doc, {"status": "unable_to_obtain",
                              "reason": "Confused, no family present"}).status_code == 200
    assert _draft_and_sign(vid, doc).status_code == 200


def test_an_earlier_none_known_counts_on_the_next_visit(staff, opd_on):
    doc = staff["doc_a"]
    pid = _outpatient()
    assert _review(_visit(pid, days_ago=2), doc, {"status": "none_known"}).status_code == 200
    vid = _visit(pid)
    view = client.get(f"/api/opd/visits/{vid}/consultation", headers=_auth(doc)).json()
    review = view["allergy_review"]
    assert review["can_sign"] is True and review["basis"] == "earlier_review"
    assert review["reviewed_this_visit"] is False
    assert review["assessment"]["status"] == "none_known"
    assert _draft_and_sign(vid, doc).status_code == 200


def test_an_earlier_unable_to_obtain_does_not_count(staff, opd_on):
    doc = staff["doc_a"]
    pid = _outpatient()
    assert _review(_visit(pid, days_ago=1), doc,
                   {"status": "unable_to_obtain", "reason": "Unconscious on arrival"}).status_code \
        == 200
    vid = _visit(pid)
    assert _draft_and_sign(vid, doc).status_code == 409


def test_a_none_known_the_chart_now_contradicts_does_not_count(staff, opd_on):
    doc = staff["doc_a"]
    pid = _outpatient()
    assert _review(_visit(pid, days_ago=3), doc, {"status": "none_known"}).status_code == 200
    with db_session() as db:
        db.execute("INSERT INTO patient_allergies (patient_id, substance, severity, status, "
                   "source) VALUES (?, 'ceftriaxone', 'unknown', 'active', 'admission')", (pid,))
    vid = _visit(pid)
    view = client.get(f"/api/opd/visits/{vid}/consultation", headers=_auth(doc)).json()
    assert view["allergy_review"]["can_sign"] is False
    assert _draft_and_sign(vid, doc).status_code == 409
    assert _review(vid, doc, {"status": "allergies_recorded"}).status_code == 200
    assert _sign(vid, doc, 1).status_code == 200


# ── 8. Audit ─────────────────────────────────────────────────────────────────

def test_the_review_is_audited_in_its_own_transaction(staff, opd_on):
    doc = staff["doc_a"]
    pid = _outpatient()
    vid = _visit(pid)
    assert _review(vid, doc, {"status": "allergies_recorded",
                              "substances": ["cotrimoxazole"]}).status_code == 200
    (row,) = _audit("opd_allergy_review_recorded", pid)
    meta = json.loads(row["metadata_json"])
    assert row["event_category"] == "clinical_record"
    assert row["actor_user_id"] == doc
    assert meta["written"] == "in_transaction"
    assert meta["status"] == "allergies_recorded"
    assert meta["charted"] == ["cotrimoxazole"]
    assert meta["visit_id"] == vid
    assert row["target_id"] == str(_assessments(pid)[0]["id"])


def test_a_failed_audit_rolls_the_review_and_the_allergy_back(staff, opd_on, monkeypatch):
    from routes import opd_routes
    from services.audit_service import AuditWriteError

    def _boom(*a, **k):
        raise AuditWriteError("audit down")

    monkeypatch.setattr(opd_routes, "log_audit_event", _boom)
    pid = _outpatient()
    resp = _review(_visit(pid), staff["doc_a"], {"status": "allergies_recorded",
                                                 "substances": ["penicillin"]})
    assert resp.status_code == 503
    assert _allergies(pid) == [] and _assessments(pid) == []


# ── 9. An old visit id reads nothing (review round 1) ────────────────────────

WINDOW_DAYS = 7
SECRET_NOTE = "Anaphylaxis to penicillin per daughter"
SECRET_REASON = "Sedated for the endoscopy, husband away"
_SECRETS = ("latex", "hives", "penicillin", SECRET_NOTE.lower(), SECRET_REASON.lower(),
            DEPT_B.lower(), "dr bose")


@pytest.fixture
def window(monkeypatch):
    monkeypatch.setattr(config, "OPD_SCOPE_DAYS", WINDOW_DAYS)
    return WINDOW_DAYS


def _charted_by_a_ward(pid):
    with db_session() as db:
        db.execute("INSERT INTO patient_allergies (patient_id, substance, reaction, severity, "
                   "status, source) VALUES (?, 'latex', 'hives', 'severe', 'active', "
                   "'admission')", (pid,))


def _assert_names_nothing(text):
    low = text.lower()
    for secret in _SECRETS:
        assert secret not in low, f"{secret!r} reached a visit-addressed answer"


def test_an_old_visit_reveals_no_allergen_name(staff, opd_on, window):
    """The reviewer's case: a clinic doctor holding an old visit id of their
    own clinic, for a patient whose allergies a ward charted and another
    clinic reviewed recently. The record view is closed to them (the OPD
    arm), and so is every allergen through the visit id."""
    doc = staff["doc_a"]
    pid = _outpatient()
    _charted_by_a_ward(pid)
    recent_b = _visit(pid, DEPT_B, days_ago=1)
    assert _review(recent_b, staff["doc_b"],
                   {"status": "allergies_recorded", "substances": ["penicillin"],
                    "note": SECRET_NOTE}).status_code == 200
    old_a = _visit(pid, DEPT_A, days_ago=window + 5)

    # The scoped read says no: the patient is outside clinic A's OPD arm.
    assert client.get(f"/api/opd/patients/{pid}/record",
                      headers=_auth(doc)).status_code == 404

    # The consultation read still answers for the clinic's own visit, but
    # carries the review as status, label and time only.
    view = client.get(f"/api/opd/visits/{old_a}/consultation", headers=_auth(doc))
    assert view.status_code == 200, view.text
    _assert_names_nothing(view.text)
    review = view.json()["allergy_review"]
    assert review["status"] == "allergies_recorded"
    assert set(review["assessment"]) == {"status", "status_label", "assessed_at"}

    # A review on the old visit is refused before the chart is read, whatever
    # it says: no allergen in the refusal, nothing charted, nothing recorded.
    before = len(_assessments(pid))
    for body in ({"status": "none_known"},
                 {"status": "allergies_recorded"},
                 {"status": "allergies_recorded", "substances": ["aspirin"]},
                 {"status": "unable_to_obtain", "reason": "Patient asleep"}):
        refused = _review(old_a, doc, body)
        assert refused.status_code == 409, refused.text
        assert refused.json()["detail"]["error"] == "visit_not_current"
        _assert_names_nothing(refused.text)
    assert len(_assessments(pid)) == before
    assert [a["substance"] for a in _allergies(pid)] == ["latex", "penicillin"]
    assert [r["actor_user_id"] for r in _audit("opd_allergy_review_recorded", pid)] \
        == [staff["doc_b"]]


def test_the_signing_refusal_names_nothing(staff, opd_on, window):
    """An earlier 'unable to obtain' from another clinic does not let the
    note be signed; the 409 says so without its reason, clinic or author."""
    doc = staff["doc_a"]
    pid = _outpatient()
    recent_b = _visit(pid, DEPT_B, days_ago=2)
    assert _review(recent_b, staff["doc_b"],
                   {"status": "unable_to_obtain", "reason": SECRET_REASON}).status_code == 200
    old_a = _visit(pid, DEPT_A, days_ago=window + 10)
    refused = _draft_and_sign(old_a, doc)
    assert refused.status_code == 409, refused.text
    detail = refused.json()["detail"]
    assert detail["error"] == "allergy_review_required"
    assert detail["allergy_review"]["status"] == "unable_to_obtain"
    assert set(detail["allergy_review"]["assessment"]) == {"status", "status_label",
                                                          "assessed_at"}
    _assert_names_nothing(refused.text)


def test_the_window_edges_are_the_arms_edges(staff, opd_on, window):
    """The last day of the window takes a review; the day before it does not;
    a booking for tomorrow opens nothing until its day."""
    doc = staff["doc_a"]
    edge = _visit(_outpatient(), days_ago=window)
    assert _review(edge, doc, {"status": "none_known"}).status_code == 200
    stale_pid = _outpatient()
    stale = _review(_visit(stale_pid, days_ago=window + 1), doc, {"status": "none_known"})
    assert stale.status_code == 409
    assert stale.json()["detail"]["error"] == "visit_not_current"
    assert "too old" in stale.json()["detail"]["message"]
    assert _assessments(stale_pid) == []
    booked_pid = _outpatient()
    booked = _review(_visit(booked_pid, days_ago=-1), doc, {"status": "none_known"})
    assert booked.status_code == 409
    assert booked.json()["detail"]["error"] == "visit_not_current"
    assert "later day" in booked.json()["detail"]["message"]
    assert _assessments(booked_pid) == []


def test_the_window_follows_the_setting(staff, opd_on, monkeypatch):
    """ORB_OPD_SCOPE_DAYS is read at call time: 0 means today's visit only."""
    monkeypatch.setattr(config, "OPD_SCOPE_DAYS", 0)
    doc = staff["doc_a"]
    assert _review(_visit(_outpatient()), doc, {"status": "none_known"}).status_code == 200
    yesterday = _review(_visit(_outpatient(), days_ago=1), doc, {"status": "none_known"})
    assert yesterday.status_code == 409
    assert yesterday.json()["detail"]["error"] == "visit_not_current"
