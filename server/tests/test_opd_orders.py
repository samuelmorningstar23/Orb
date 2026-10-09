"""Outpatient investigations, the code-list search and the prints (opd-11,
W6-A, 9 October 2026).

What this file defends:

  1. Off by default: every new /api/opd route answers 404 feature_disabled
     while ORB_OPD is off, and needs opd.consult when it is on.
  2. The code-list search: a diagnosis is ICD-10 or a SNOMED disorder or
     finding, never a SNOMED procedure; an investigation is a laboratory test
     or imaging, never a medicine, a procedure or a bedside test; with the
     laboratory module on, a laboratory test comes from its catalogue only.
     No patient data, nothing audited.
  3. An order carries the visit: POST /api/opd/visits/{visit_id}/orders goes
     through orders_service.create_order(opd_visit_id=...), is active at once,
     takes its words and type from the code list (never the client), is
     audited in its own transaction and shows on the visit and the record.
  4. Refused: another clinic's visit (404), a nurse, receptionist or
     administrator (403), an actively admitted patient (409, re-read inside
     the order's transaction), a cancelled, finished, future or old visit
     (409), the same test twice (409), a code that is not an investigation
     (422). orders_routes is untouched and order sets are not offered.
  5. Cancel needs a reason, stays inside its visit, and is audited.
  6. Prints carry what the record holds and the doctor's name and
     registration number only: no date of birth, phone, address or allergy;
     a draft never prints; every print is audited fail-closed.
  7. Nothing is written to vitals, risk_scores or medications.
"""
from __future__ import annotations

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
from services import orders_service
from services.orders_service import OpdVisitOrderRefused

client = TestClient(app)

DEPT_A = "Opd Orders Test Medicine"
DEPT_B = "Opd Orders Test Surgery"
REG_NUMBER = "KMC-48213"
CBC = ("LOINC", "58410-2")        # CBC panel, a laboratory test
CREATININE = ("LOINC", "2160-0")
CXR = ("LOCAL", "ORB-IMG-CXR")    # imaging


def _mk_user(role, department, name=None, registration_number=None):
    uid = f"OOU{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO users (id, name, role, password_hash, department, is_active, "
            "registration_number) VALUES (?, ?, ?, 'x', ?, 1, ?)",
            (uid, name or f"Opd Orders {role} {uid[-4:]}", role, department,
             registration_number))
    return uid


def _auth(uid, role="doctor"):
    return {"Authorization": f"Bearer {create_token(uid, role)}"}


def _outpatient():
    pid = f"OOP{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO patients (id, name, mrn, age, sex, status, diagnosis, date_of_birth, "
            "phone, address) VALUES (?, ?, ?, 47, 'M', 'registered', 'Opd orders fixture', "
            "'1979-03-04', '9876501234', '12 Lake Road')",
            (pid, f"Orders Patient {pid[-4:]}", f"UH-{uuid.uuid4().hex[:8].upper()}"))
    return pid


def _stated_identity(pid):
    with db_session() as db:
        r = db.execute("SELECT name, mrn, date_of_birth FROM patients WHERE id = ?",
                       (pid,)).fetchone()
    return {"name": r["name"], "date_of_birth": r["date_of_birth"], "uhid": r["mrn"]}


#: The laboratory codes these tests order and search for.
_ORDERED_CODES = (CBC, CREATININE)


@pytest.fixture(scope="module", autouse=True)
def _ordered_codes_as_seeded():
    """The tests here expect the starter wording for the codes they order, and
    no laboratory catalogue row for them. The session database is shared:
    the laboratory suites that run before this file load their starter
    catalogue into it, and when one of them runs before terminology is seeded
    it registers these codes under the catalogue's own words. Both change
    what the rules under test answer (describe_investigation takes an active
    catalogue name over the terminology wording, and the laboratory search
    offers active catalogue rows), so for this module the catalogue rows for
    these codes are switched off and their terminology wording is the
    starter set's. Both are put back afterwards."""
    from services.terminology_service import STARTER_TERMINOLOGY, seed_terminology
    seed_terminology()
    starter = {(system, code): display for system, code, display in STARTER_TERMINOLOGY}
    with db_session() as db:
        has_catalogue = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'lab_tests'"
        ).fetchone() is not None
        tests_before, terms_before = [], []
        for system, code in _ORDERED_CODES:
            if has_catalogue:
                tests_before += [dict(r) for r in db.execute(
                    "SELECT id, active FROM lab_tests WHERE code_system = ? AND code = ?",
                    (system, code)).fetchall()]
            terms_before += [dict(r) for r in db.execute(
                "SELECT code_system, code, display FROM terminology "
                "WHERE code_system = ? AND code = ?", (system, code)).fetchall()]
        for row in tests_before:
            db.execute("UPDATE lab_tests SET active = 0 WHERE id = ?", (row["id"],))
        for row in terms_before:
            db.execute("UPDATE terminology SET display = ? WHERE code_system = ? AND code = ?",
                       (starter[(row["code_system"], row["code"])], row["code_system"],
                        row["code"]))
    yield
    with db_session() as db:
        for row in tests_before:
            db.execute("UPDATE lab_tests SET active = ? WHERE id = ?",
                       (row["active"], row["id"]))
        for row in terms_before:
            db.execute("UPDATE terminology SET display = ? WHERE code_system = ? AND code = ?",
                       (row["display"], row["code_system"], row["code"]))


@pytest.fixture(scope="module")
def staff():
    from services.terminology_service import seed_terminology
    seed_terminology()
    return {
        "doc_a": _mk_user("doctor", DEPT_A, "Dr Kavya Orders", REG_NUMBER),
        "doc_b": _mk_user("doctor", DEPT_B, "Dr Rohan Orders"),
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
    monkeypatch.setattr(config, "LAB_ENABLED", False)
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


def _order(vid, uid, code=CBC, role="doctor", **extra):
    return client.post(f"/api/opd/visits/{vid}/orders",
                       json={"code_system": code[0], "code": code[1], **extra},
                       headers=_auth(uid, role))


def _audit(action_type, patient_id):
    with db_session() as db:
        rows = db.execute("SELECT * FROM audit_logs WHERE action_type = ? AND patient_id = ? "
                          "ORDER BY id", (action_type, patient_id)).fetchall()
    return [dict(r) for r in rows]


def _orders_of(pid):
    with db_session() as db:
        return [dict(r) for r in db.execute(
            "SELECT * FROM orders WHERE patient_id = ? ORDER BY created_at, id", (pid,)).fetchall()]


def _counts(pid):
    with db_session() as db:
        return {t: db.execute(f"SELECT COUNT(*) FROM {t} WHERE patient_id = ?",
                              (pid,)).fetchone()[0]
                for t in ("vitals", "risk_scores", "medications")}


def _admit(pid, doc):
    with db_session() as db:
        db.execute("INSERT INTO admissions (id, patient_id, admitted_by, department, ward, bed, "
                   "status, created_at) VALUES (?, ?, ?, ?, 'Bay', '7', 'active', ?)",
                   (f"OOA{uuid.uuid4().hex[:8]}", pid, doc, DEPT_A,
                    datetime.now(timezone.utc).isoformat()))
        db.execute("UPDATE patients SET status = 'admitted' WHERE id = ?", (pid,))


def _discharge(pid):
    with db_session() as db:
        db.execute("UPDATE admissions SET status = 'discharged' WHERE patient_id = ?", (pid,))
        db.execute("UPDATE patients SET status = 'discharged' WHERE id = ?", (pid,))


def _set_visit(vid, **cols):
    sets = ", ".join(f"{k} = ?" for k in cols)
    with db_session() as db:
        db.execute(f"UPDATE opd_visits SET {sets} WHERE id = ?", (*cols.values(), vid))


def _allergy_reviewed(pid):
    with db_session() as db:
        db.execute("INSERT INTO patient_allergy_assessments (patient_id, status, substances_json, "
                   "source, assessed_by, assessed_at) VALUES (?, 'none_known', '[]', 'opd', "
                   "'OOREVIEWER', ?)", (pid, datetime.now(timezone.utc).isoformat()))


# ═════════════════════════════════════════════════════════════════════════════
# 1. Off by default, opd.consult when on
# ═════════════════════════════════════════════════════════════════════════════

def test_every_new_route_is_404_with_the_feature_off(staff, opd_off):
    hdr = _auth(staff["doc_a"])
    for method, path in (("GET", "/api/opd/terminology/search?q=cbc&kind=investigation"),
                         ("GET", "/api/opd/visits/X/orders"),
                         ("POST", "/api/opd/visits/X/orders"),
                         ("POST", "/api/opd/visits/X/orders/O/cancel"),
                         ("GET", "/api/opd/visits/X/print")):
        resp = client.request(method, path, json={}, headers=hdr)
        assert resp.status_code == 404, (method, path, resp.text)
        assert resp.json()["detail"]["error"] == "feature_disabled"


def test_non_consulting_roles_cannot_search_or_order(staff, opd_on):
    visit = _visit(staff["doc_a"])
    for key, role in (("nurse_a", "nurse"), ("recep_a", "receptionist"), ("admin", "admin")):
        hdr = _auth(staff[key], role)
        assert client.get("/api/opd/terminology/search?q=hyper",
                          headers=hdr).status_code == 403, role
        assert _order(visit["id"], staff[key], role=role).status_code == 403, role
        assert client.get(f"/api/opd/visits/{visit['id']}/orders",
                          headers=hdr).status_code == 403, role
        assert client.get(f"/api/opd/visits/{visit['id']}/print?kind=investigations",
                          headers=hdr).status_code == 403, role
    assert _orders_of(visit["patient_id"]) == []


# ═════════════════════════════════════════════════════════════════════════════
# 2. The code-list search
# ═════════════════════════════════════════════════════════════════════════════

def _search(uid, q, kind):
    resp = client.get("/api/opd/terminology/search", params={"q": q, "kind": kind},
                      headers=_auth(uid))
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_diagnosis_search_offers_icd10_and_snomed_disorders_only(staff, opd_on):
    found = _search(staff["doc_a"], "hypert", "diagnosis")
    codes = {(r["code_system"], r["code"]) for r in found["results"]}
    assert ("ICD10", "I10") in codes
    assert ("SNOMED", "38341003") in codes
    by_code = _search(staff["doc_a"], "I10", "diagnosis")["results"]
    assert by_code[0]["code"] == "I10"
    # A SNOMED procedure is not a diagnosis.
    procedures = _search(staff["doc_a"], "procedure", "diagnosis")["results"]
    assert all("(procedure)" not in r["display"] for r in procedures)
    # Laboratory codes never appear as diagnoses.
    assert _search(staff["doc_a"], "glucose", "diagnosis")["results"] == []


def test_a_short_query_searches_nothing_and_a_bad_kind_is_refused(staff, opd_on):
    assert _search(staff["doc_a"], "h", "diagnosis")["results"] == []
    resp = client.get("/api/opd/terminology/search", params={"q": "cbc", "kind": "medication"},
                      headers=_auth(staff["doc_a"]))
    assert resp.status_code == 422


def test_investigation_search_offers_laboratory_and_imaging_only(staff, opd_on):
    doc = staff["doc_a"]
    cbc = _search(doc, "CBC", "investigation")["results"]
    assert any(r["code"] == "58410-2" and r["order_type"] == "lab" for r in cbc)
    xray = _search(doc, "x-ray", "investigation")["results"]
    assert any(r["code"] == "ORB-IMG-CXR" and r["order_type"] == "imaging" for r in xray)
    for q in ("aspirin", "ceftriaxone", "electrocardiogram", "blood gas", "ketones",
              "orb-med", "orb-proc"):
        rows = _search(doc, q, "investigation")["results"]
        assert all(r["order_type"] in ("lab", "imaging") for r in rows), q
        assert not any(r["code"].startswith(("ORB-MED", "ORB-PROC")) for r in rows), q
        assert not any(r["code"] in ("ORB-LAB-ABG", "ORB-LAB-VBG", "ORB-LAB-BLOOD-KETONES")
                       for r in rows), q


_CATALOGUE_CODES = ("OOT-UREA", "OOT-POCT")


def _catalogue_test(code, name, performed_at="lab"):
    now = datetime.now(timezone.utc).isoformat()
    with db_session() as db:
        db.execute(
            "INSERT OR IGNORE INTO lab_tests (id, code, code_system, name, short_name, kind, "
            "performed_at, orderable, active, created_at, updated_at) "
            "VALUES (?, ?, 'LOCAL', ?, ?, 'analyte', ?, 1, 1, ?, ?)",
            (f"LOCAL:{code}", code, name, code[:16], performed_at, now, now))
        db.execute("INSERT OR IGNORE INTO terminology (code_system, code, display, is_active) "
                   "VALUES ('LOCAL', ?, ?, 1)", (code, name))


@pytest.fixture
def catalogue_rows():
    """Two catalogue rows for this test only. The session database is shared,
    and the laboratory suites load their own catalogue into an empty one, so
    the rows (and the orders naming them) are removed again afterwards."""
    _catalogue_test("OOT-UREA", "Opd orders urea test")
    _catalogue_test("OOT-POCT", "Opd orders bedside glucose", performed_at="ward_poct")
    yield
    marks = ",".join("?" for _ in _CATALOGUE_CODES)
    with db_session() as db:
        ids = [r[0] for r in db.execute(
            f"SELECT id FROM orders WHERE code_system = 'LOCAL' AND code IN ({marks})",
            _CATALOGUE_CODES).fetchall()]
        for oid in ids:
            db.execute("DELETE FROM order_events WHERE order_id = ?", (oid,))
            db.execute("DELETE FROM orders WHERE id = ?", (oid,))
        db.execute(f"DELETE FROM lab_tests WHERE code_system = 'LOCAL' AND code IN ({marks})",
                   _CATALOGUE_CODES)
        db.execute(f"DELETE FROM terminology WHERE code_system = 'LOCAL' AND code IN ({marks})",
                   _CATALOGUE_CODES)


def test_with_the_laboratory_on_a_test_comes_from_its_catalogue(staff, opd_on, monkeypatch,
                                                                 catalogue_rows):
    monkeypatch.setattr(config, "LAB_ENABLED", True)
    monkeypatch.setattr("services.lab_order_service.intake_after_activation",
                        lambda order_id, actor_id=None: None)
    doc = staff["doc_a"]
    rows = _search(doc, "opd orders", "investigation")["results"]
    assert [r["code"] for r in rows] == ["OOT-UREA"]
    assert _search(doc, "creatinine", "investigation")["results"] == []
    visit = _visit(doc)
    refused = _order(visit["id"], doc, CREATININE)
    assert refused.status_code == 422
    assert refused.json()["detail"]["error"] == "not_in_lab_catalogue"
    bedside = _order(visit["id"], doc, ("LOCAL", "OOT-POCT"))
    assert bedside.status_code == 422 and bedside.json()["detail"]["error"] == "bedside_test"
    ok = _order(visit["id"], doc, ("LOCAL", "OOT-UREA"))
    assert ok.status_code == 201, ok.text
    assert ok.json()["description"] == "Opd orders urea test"


# ═════════════════════════════════════════════════════════════════════════════
# 3. An order carries the visit
# ═════════════════════════════════════════════════════════════════════════════

def test_an_order_carries_the_visit_and_is_audited_in_its_transaction(staff, opd_on):
    doc = staff["doc_a"]
    visit = _visit(doc)
    vid, pid = visit["id"], visit["patient_id"]
    before = _counts(pid)
    resp = _order(vid, doc, CBC, priority="urgent")
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["opd_visit_id"] == vid
    assert body["order_type"] == "lab"
    assert body["status"] == "active"
    assert body["priority"] == "urgent"
    assert body["description"] == "CBC panel - Blood by Automated count"
    assert body["ordered_by_name"] == "Dr Kavya Orders"
    rows = _orders_of(pid)
    assert len(rows) == 1 and rows[0]["opd_visit_id"] == vid
    assert rows[0]["ordered_by"] == doc and rows[0]["source"] == "clinician"
    with db_session() as db:
        events = db.execute("SELECT new_status FROM order_events WHERE order_id = ?",
                            (rows[0]["id"],)).fetchall()
    assert [e["new_status"] for e in events] == ["active"]
    audit = _audit("opd_investigation_ordered", pid)
    assert len(audit) == 1
    assert audit[0]["event_category"] == "clinical_record"
    assert audit[0]["target_id"] == rows[0]["id"]
    meta = json.loads(audit[0]["metadata_json"])
    assert meta["visit_id"] == vid and meta["written"] == "in_transaction"
    # The visit's list and the outpatient record both show it.
    listed = client.get(f"/api/opd/visits/{vid}/orders", headers=_auth(doc)).json()
    assert [o["id"] for o in listed["orders"]] == [rows[0]["id"]]
    assert _audit("opd_visit_orders_viewed", pid)
    record = client.get(f"/api/opd/patients/{pid}/record", headers=_auth(doc)).json()
    assert [r["id"] for r in record["results"]] == [rows[0]["id"]]
    assert _counts(pid) == before


def test_the_client_cannot_name_the_order_type_or_its_words(staff, opd_on):
    doc = staff["doc_a"]
    visit = _visit(doc)
    for extra in ({"order_type": "medication"}, {"description": "Morphine 10 mg"},
                  {"detail": {"medication": {"dose": "10 mg"}}}, {"status": "draft"}):
        resp = _order(visit["id"], doc, CBC, **extra)
        assert resp.status_code == 422, extra
    imaging = _order(visit["id"], doc, CXR)
    assert imaging.status_code == 201, imaging.text
    assert imaging.json()["order_type"] == "imaging"
    assert _orders_of(visit["patient_id"])[0]["order_type"] == "imaging"


def test_codes_that_are_not_investigations_are_refused(staff, opd_on):
    doc = staff["doc_a"]
    visit = _visit(doc)
    for code, error in ((("LOCAL", "ORB-MED-MORPHINE"), "not_an_investigation"),
                        (("LOCAL", "ORB-PROC-ECG"), "not_an_investigation"),
                        (("ICD10", "I10"), "not_an_investigation"),
                        (("SNOMED", "73211009"), "not_an_investigation"),
                        (("LOCAL", "ORB-LAB-ABG"), "bedside_test"),
                        (("LOCAL", "ORB-LAB-BLOOD-KETONES"), "bedside_test"),
                        (("LOINC", "0000-0"), "unknown_code")):
        resp = _order(visit["id"], doc, code)
        assert resp.status_code == 422, (code, resp.text)
        assert resp.json()["detail"]["error"] == error, code
    assert _orders_of(visit["patient_id"]) == []


def test_the_same_test_twice_on_one_visit_is_refused(staff, opd_on):
    doc = staff["doc_a"]
    visit = _visit(doc)
    assert _order(visit["id"], doc, CBC).status_code == 201
    again = _order(visit["id"], doc, CBC)
    assert again.status_code == 409 and again.json()["detail"]["error"] == "already_ordered"
    assert _order(visit["id"], doc, CREATININE).status_code == 201
    assert len(_orders_of(visit["patient_id"])) == 2


# ═════════════════════════════════════════════════════════════════════════════
# 4. Refusals
# ═════════════════════════════════════════════════════════════════════════════

def test_another_clinics_visit_is_404(staff, opd_on):
    visit = _visit(staff["doc_a"])
    resp = _order(visit["id"], staff["doc_b"])
    assert resp.status_code == 404
    assert client.get(f"/api/opd/visits/{visit['id']}/orders",
                      headers=_auth(staff["doc_b"])).status_code == 404
    assert client.get(f"/api/opd/visits/{visit['id']}/print?kind=investigations",
                      headers=_auth(staff["doc_b"])).status_code == 404
    assert _orders_of(visit["patient_id"]) == []


def test_an_actively_admitted_patient_gets_no_outpatient_order(staff, opd_on):
    doc = staff["doc_a"]
    visit = _visit(doc)
    pid = visit["patient_id"]
    _admit(pid, doc)
    try:
        resp = _order(visit["id"], doc)
        assert resp.status_code == 409 and resp.json()["detail"]["error"] == "patient_admitted"
        assert _orders_of(pid) == []
    finally:
        _discharge(pid)


def test_the_admission_is_read_inside_the_orders_own_transaction(staff, opd_on):
    """The route's checks ran before the admission landed; the order's own
    transaction still refuses and writes nothing."""
    doc = staff["doc_a"]
    visit = _visit(doc)
    pid = visit["patient_id"]
    _admit(pid, doc)
    try:
        with pytest.raises(OpdVisitOrderRefused) as err:
            orders_service.create_order(
                pid, "lab", "CBC panel - Blood by Automated count", code=CBC[1],
                code_system=CBC[0], ordered_by=doc, status="active", opd_visit_id=visit["id"])
        assert err.value.code == "patient_admitted" and err.value.status == 409
        assert _orders_of(pid) == []
    finally:
        _discharge(pid)


def test_an_order_cannot_name_another_patients_visit(staff, opd_on):
    doc = staff["doc_a"]
    visit = _visit(doc)
    other = _outpatient()
    with pytest.raises(OpdVisitOrderRefused) as err:
        orders_service.create_order(other, "lab", "CBC", code=CBC[1], code_system=CBC[0],
                                    ordered_by=doc, status="active", opd_visit_id=visit["id"])
    assert err.value.code == "visit_not_found"
    assert _orders_of(other) == []


def test_an_audit_failure_inside_the_transaction_keeps_nothing(staff, opd_on):
    doc = staff["doc_a"]
    visit = _visit(doc)

    def _boom(conn, order):
        raise RuntimeError("audit down")

    with pytest.raises(RuntimeError):
        orders_service.create_order(visit["patient_id"], "lab", "CBC", code=CBC[1],
                                    code_system=CBC[0], ordered_by=doc, status="active",
                                    opd_visit_id=visit["id"], in_transaction=_boom)
    assert _orders_of(visit["patient_id"]) == []


@pytest.mark.parametrize("status,error", [("cancelled", "visit_cancelled"),
                                          ("completed", "visit_completed")])
def test_a_cancelled_or_finished_visit_takes_no_order(staff, opd_on, status, error):
    doc = staff["doc_a"]
    visit = _visit(doc)
    _set_visit(visit["id"], status=status)
    resp = _order(visit["id"], doc)
    assert resp.status_code == 409 and resp.json()["detail"]["error"] == error
    assert _orders_of(visit["patient_id"]) == []


@pytest.mark.parametrize("days", [-30, 3])
def test_an_old_or_future_visit_takes_no_order(staff, opd_on, days):
    from services.hospital_time import hospital_today
    doc = staff["doc_a"]
    visit = _visit(doc)
    _set_visit(visit["id"], visit_date=(hospital_today() + timedelta(days=days)).isoformat())
    resp = _order(visit["id"], doc)
    assert resp.status_code == 409 and resp.json()["detail"]["error"] == "visit_not_current"
    assert _orders_of(visit["patient_id"]) == []


def test_orders_routes_are_untouched_and_order_sets_are_not_offered(staff, opd_on):
    server = pathlib.Path(__file__).resolve().parent.parent
    source = (server / "routes" / "orders_routes.py").read_text()
    assert "opd_visit_id" not in source
    opd_source = (server / "routes" / "opd_routes.py").read_text()
    assert "order_sets_service" not in opd_source
    from main import app as live_app
    opd_paths = [getattr(r, "path", "") for r in live_app.routes
                 if getattr(r, "path", "").startswith("/api/opd/")]
    assert opd_paths and not any("set" in p.split("/") or "sets" in p for p in opd_paths)
    # The inpatient order-set lane does not reach an outpatient either.
    visit = _visit(staff["doc_a"])
    from services import order_sets_service
    sets = order_sets_service.list_sets()
    if sets:
        set_id = sets[0]["id"]
        resp = client.post(f"/api/orders/sets/{set_id}/apply",
                           json={"patient_id": visit["patient_id"]},
                           headers=_auth(staff["doc_a"]))
        assert resp.status_code in (400, 403, 404, 409), resp.text
    assert _orders_of(visit["patient_id"]) == []


# ═════════════════════════════════════════════════════════════════════════════
# 5. Cancel
# ═════════════════════════════════════════════════════════════════════════════

def test_cancel_needs_a_reason_stays_in_its_visit_and_is_audited(staff, opd_on):
    doc = staff["doc_a"]
    visit = _visit(doc)
    other = _visit(doc)
    order = _order(visit["id"], doc).json()
    path = f"/api/opd/visits/{visit['id']}/orders/{order['id']}/cancel"
    assert client.post(path, json={"reason": ""}, headers=_auth(doc)).status_code == 422
    wrong = client.post(f"/api/opd/visits/{other['id']}/orders/{order['id']}/cancel",
                        json={"reason": "Wrong patient"}, headers=_auth(doc))
    assert wrong.status_code == 404
    assert client.post(path, json={"reason": "Ordered twice"},
                       headers=_auth(staff["doc_b"])).status_code == 404
    done = client.post(path, json={"reason": "Ordered in error"}, headers=_auth(doc))
    assert done.status_code == 200, done.text
    assert done.json()["status"] == "cancelled"
    audit = _audit("opd_investigation_cancelled", visit["patient_id"])
    assert len(audit) == 1
    assert json.loads(audit[0]["metadata_json"])["reason"] == "Ordered in error"
    # A cancelled test may be ordered again on the same visit.
    assert _order(visit["id"], doc).status_code == 201


def test_an_order_the_laboratory_manages_is_cancelled_from_the_laboratory(staff, opd_on,
                                                                          monkeypatch):
    doc = staff["doc_a"]
    visit = _visit(doc)
    order = _order(visit["id"], doc).json()
    monkeypatch.setattr(orders_service, "is_lab_managed", lambda conn, order_id: True)
    resp = client.post(f"/api/opd/visits/{visit['id']}/orders/{order['id']}/cancel",
                       json={"reason": "Not needed"}, headers=_auth(doc))
    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "order_managed_by_lab"
    assert _orders_of(visit["patient_id"])[0]["status"] == "active"


def test_cancel_is_refused_once_the_patient_is_admitted(staff, opd_on):
    doc = staff["doc_a"]
    visit = _visit(doc)
    order = _order(visit["id"], doc).json()
    _admit(visit["patient_id"], doc)
    try:
        resp = client.post(f"/api/opd/visits/{visit['id']}/orders/{order['id']}/cancel",
                           json={"reason": "Not needed"}, headers=_auth(doc))
        assert resp.status_code == 409
        assert resp.json()["detail"]["error"] == "patient_admitted"
    finally:
        _discharge(visit["patient_id"])


# ═════════════════════════════════════════════════════════════════════════════
# 6. Prints
# ═════════════════════════════════════════════════════════════════════════════

def _walk(value):
    if isinstance(value, dict):
        for k, v in value.items():
            yield k, v
            yield from _walk(v)
    elif isinstance(value, list):
        for v in value:
            yield from _walk(v)


def test_a_draft_never_prints_and_a_signed_note_prints_what_the_record_holds(staff, opd_on):
    doc = staff["doc_a"]
    visit = _visit(doc)
    vid, pid = visit["id"], visit["patient_id"]
    printed = client.get(f"/api/opd/visits/{vid}/print?kind=consultation", headers=_auth(doc))
    assert printed.status_code == 409 and printed.json()["detail"]["error"] == "not_signed"
    put = client.put(f"/api/opd/visits/{vid}/consultation", headers=_auth(doc), json={
        "chief_complaint": "Cough for a week", "plan": "Chest X-ray, review in 5 days",
        "diagnoses": [{"text": "Essential hypertension", "code": "I10", "code_system": "ICD10"}],
        "vitals": {"values": {"heart_rate": 92, "spo2": 95},
                   "measured_at": datetime.now(timezone.utc).isoformat()}})
    assert put.status_code == 200, put.text
    _allergy_reviewed(pid)
    assert _order(vid, doc, CXR).status_code == 201
    signed = client.post(f"/api/opd/visits/{vid}/consultation/sign",
                         json={"row_version": put.json()["row_version"]}, headers=_auth(doc))
    assert signed.status_code == 200, signed.text
    resp = client.get(f"/api/opd/visits/{vid}/print?kind=consultation", headers=_auth(doc))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["kind"] == "consultation"
    assert body["doctors"] == [{"name": "Dr Kavya Orders", "registration_number": REG_NUMBER}]
    assert body["patient"]["name"] == visit["patient_name"]
    assert body["patient"]["uhid"] == visit["patient_mrn"]
    assert body["consultation"]["chief_complaint"] == "Cough for a week"
    assert body["consultation"]["vitals"]["values"] == {"heart_rate": 92, "spo2": 95}
    assert body["consultation"]["diagnoses"][0]["code"] == "I10"
    assert [i["description"] for i in body["investigations"]] == [
        "Chest X-ray, portable (Orb local code)"]
    keys = {k for k, _ in _walk(body)}
    for hidden in ("date_of_birth", "phone", "address", "allergies", "allergy_review",
                   "substances", "doctor_id", "signed_by", "ordered_by", "author_id",
                   "measured_by", "score", "news2", "band"):
        assert hidden not in keys, hidden
    text = json.dumps(body)
    assert "1979-03-04" not in text and "9876501234" not in text and "Lake Road" not in text
    audit = _audit("opd_consultation_printed", pid)
    assert len(audit) == 1 and audit[0]["event_category"] == "phi_access"


def test_the_investigation_slip_names_the_ordering_doctor(staff, opd_on):
    doc = staff["doc_a"]
    visit = _visit(doc)
    vid, pid = visit["id"], visit["patient_id"]
    none = client.get(f"/api/opd/visits/{vid}/print?kind=investigations", headers=_auth(doc))
    assert none.status_code == 409 and none.json()["detail"]["error"] == "no_investigations"
    first = _order(vid, doc, CBC).json()
    _order(vid, doc, CREATININE)
    client.post(f"/api/opd/visits/{vid}/orders/{first['id']}/cancel",
                json={"reason": "Not needed"}, headers=_auth(doc))
    resp = client.get(f"/api/opd/visits/{vid}/print?kind=investigations", headers=_auth(doc))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [i["description"] for i in body["investigations"]] == [
        "Creatinine [Mass/volume] in Serum or Plasma"]
    assert body["doctors"] == [{"name": "Dr Kavya Orders", "registration_number": REG_NUMBER}]
    assert body["consultation"] is None
    assert _audit("opd_investigation_slip_printed", pid)


def test_a_print_is_not_returned_when_its_audit_row_cannot_be_written(staff, opd_on,
                                                                       monkeypatch):
    from routes import opd_routes
    from services.audit_service import AuditWriteError
    doc = staff["doc_a"]
    visit = _visit(doc)
    _order(visit["id"], doc, CBC)

    def _down(**kwargs):
        raise AuditWriteError("down")

    monkeypatch.setattr(opd_routes, "log_phi_read", _down)
    resp = client.get(f"/api/opd/visits/{visit['id']}/print?kind=investigations",
                      headers=_auth(doc))
    assert resp.status_code == 503
    assert resp.json()["detail"]["error"] == "audit_unavailable"
