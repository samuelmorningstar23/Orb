"""The drug lists are hidden on an Orb Ward Record computer (9 October 2026).

Samuel's ruling: "hide the drug lists". The founding programme sells the
observation record only, and the drug chart stays on paper (SALES_PLAN_2026-10.md
section 3, step 2: "Everything else stays only on paper: ... the drug chart";
"No orders"). Under ORB_WARD_RECORD every screen and every API that feeds a
screen sends no drug data and says nothing about medicines, and every
medication and order write answers 404 "The drug chart is not part of Orb Ward
Record" before any patient or drug is read (services/drug_chart). The allergy
list stays. With the profile off every one of these is as it was.
"""
import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import config
from auth import create_token
from database import db_session
from main import app
from services.drug_chart import DRUG_CHART_OFF

client = TestClient(app)
DEPT = "Ward Record Drug Chart Ward"
DRUG = "Amoxicillin"
#: Any word that tells a reader about medicines, in any screen's text.
MEDICINE_WORDS = ("medication", "Medication", "medicine", "Medicine", "MEDICATION",
                  "MEDICINE", " meds", " med.", "active med", DRUG)


def _no_medicine_words(text: str) -> list[str]:
    return [w for w in MEDICINE_WORDS if w in text]


@pytest.fixture()
def ward_record(monkeypatch):
    def _set(on: bool):
        monkeypatch.setattr(config, "WARD_RECORD", on, raising=False)
    return _set


def _mk_user(role, department=DEPT):
    uid = f"WDC{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO users (id, name, role, password_hash, department) VALUES (?, ?, ?, 'x', ?)",
            (uid, f"Drug chart {role}", role, department),
        )
    return uid, {"Authorization": f"Bearer {create_token(uid, role)}"}


@pytest.fixture()
def patient():
    """An admitted patient with a penicillin allergy, amoxicillin charted and
    given once, a medication order and a laboratory order, and a portal login."""
    tag = uuid.uuid4().hex[:8].upper()
    pid, portal_uid = f"WDCP{tag}", f"WDCU{tag}"
    started = (datetime.now(timezone.utc) - timedelta(hours=6)).isoformat()
    with db_session() as db:
        db.execute(
            "INSERT OR IGNORE INTO users (id, name, role, password_hash) "
            "VALUES ('WDCADM', 'Drug Chart Admitter', 'doctor', 'x')")
        db.execute("INSERT INTO users (id, name, role, password_hash) VALUES (?, 'Portal', 'patient', 'x')",
                   (portal_uid,))
        db.execute(
            """INSERT INTO patients (id, user_id, name, mrn, bed, ward, age, sex, diagnosis, status,
                                     admission_date)
               VALUES (?, ?, 'Drug Chart Patient', ?, 'DC-1', 'DC', 66, 'F', 'Pneumonia',
                       'admitted', ?)""",
            (pid, portal_uid, f"MRN-{tag}", started))
        db.execute(
            "INSERT INTO admissions (patient_id, department, status, admitted_by) "
            "VALUES (?, ?, 'active', 'WDCADM')", (pid, DEPT))
        db.execute(
            "INSERT INTO patient_allergies (patient_id, substance, substance_class, reaction, "
            "severity, status) VALUES (?, 'Penicillin', 'penicillin', 'Anaphylaxis', 'severe', 'active')",
            (pid,))
        cur = db.execute(
            """INSERT INTO medications (patient_id, drug_name, dosage, route, frequency,
                                        status, prescribed_by, source, started_at)
               VALUES (?, ?, '500 mg', 'Oral', 'Q8H', 'active', 'WDCADM', 'clinician', ?)""",
            (pid, DRUG, started))
        med_id = cur.lastrowid
        db.execute(
            """INSERT INTO medication_administrations (medication_id, patient_id, administered_by,
                   administered_at, status, dose_given, route_given)
               VALUES (?, ?, 'WDCADM', ?, 'given', '500 mg', 'Oral')""",
            (med_id, pid, started))
        for kind, what in (("medication", f"{DRUG} 500 mg oral"), ("lab", "Full blood count")):
            oid = f"WDCO-{kind}-{tag}"
            db.execute(
                "INSERT INTO orders (id, patient_id, order_type, description, status, ordered_by, "
                "created_at) VALUES (?, ?, ?, ?, 'active', 'WDCADM', ?)",
                (oid, pid, kind, what, started))
            db.execute(
                "INSERT INTO order_events (order_id, prior_status, new_status, actor_id, created_at) "
                "VALUES (?, 'draft', 'active', 'WDCADM', ?)", (oid, started))
    yield {"pid": pid, "med_id": med_id, "portal": portal_uid,
           "med_order": f"WDCO-medication-{tag}", "lab_order": f"WDCO-lab-{tag}"}
    with db_session() as db:
        db.execute("UPDATE patients SET status = 'discharged' WHERE id = ?", (pid,))
        db.execute("UPDATE admissions SET status = 'discharged' WHERE patient_id = ?", (pid,))


# ── The chart ────────────────────────────────────────────────────────────────

def test_the_chart_sends_no_drug_under_the_profile(ward_record, patient):
    ward_record(True)
    _, hdr = _mk_user("doctor")
    r = client.get(f"/api/vigil/patients/{patient['pid']}", headers=hdr)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["medications"] == []
    assert body["drug_chart"] is False
    assert DRUG not in r.text
    # The allergy list is a safety record and stays.
    a = client.get(f"/api/patients/{patient['pid']}/allergies", headers=hdr)
    assert a.status_code == 200 and "Penicillin" in a.text


def test_the_chart_sends_the_drug_chart_with_the_profile_off(ward_record, patient):
    ward_record(False)
    _, hdr = _mk_user("doctor")
    body = client.get(f"/api/vigil/patients/{patient['pid']}", headers=hdr).json()
    assert [m["drug_name"] for m in body["medications"]] == [DRUG]
    assert body["drug_chart"] is True


def test_reconciliation_and_adding_a_drug_answer_404_under_the_profile(ward_record, patient):
    ward_record(True)
    _, hdr = _mk_user("doctor")
    r = client.get(f"/api/vigil/patients/{patient['pid']}/medication-reconciliation", headers=hdr)
    assert r.status_code == 404 and r.json()["detail"] == DRUG_CHART_OFF
    r = client.post(f"/api/vigil/patients/{patient['pid']}/medications", headers=hdr,
                    json={"drug_name": "Paracetamol", "dosage": "1 g", "frequency": "Q6H"})
    assert r.status_code == 404 and r.json()["detail"] == DRUG_CHART_OFF
    with db_session() as db:
        n = db.execute("SELECT COUNT(*) FROM medications WHERE patient_id = ?",
                       (patient["pid"],)).fetchone()[0]
    assert n == 1


def test_reconciliation_answers_with_the_profile_off(ward_record, patient):
    ward_record(False)
    _, hdr = _mk_user("doctor")
    r = client.get(f"/api/vigil/patients/{patient['pid']}/medication-reconciliation", headers=hdr)
    assert r.status_code == 200 and DRUG in r.text


# ── The story and the timeline ───────────────────────────────────────────────

@pytest.mark.parametrize("path", ["/api/vigil/patients/{pid}/story",
                                  "/api/chart/patients/{pid}/timeline"])
def test_the_story_and_timeline_carry_no_drug_under_the_profile(ward_record, patient, path):
    ward_record(True)
    _, hdr = _mk_user("doctor")
    r = client.get(path.format(pid=patient["pid"]), headers=hdr)
    assert r.status_code == 200, r.text
    kinds = {e.get("type") or e.get("kind") for e in r.json()["events"]}
    assert not kinds & {"medication", "administration"}
    assert _no_medicine_words(r.text) == []
    # Every other order stays.
    assert "Full blood count" in r.text


@pytest.mark.parametrize("path", ["/api/vigil/patients/{pid}/story",
                                  "/api/chart/patients/{pid}/timeline"])
def test_the_story_and_timeline_carry_the_drug_chart_with_the_profile_off(ward_record, patient, path):
    ward_record(False)
    _, hdr = _mk_user("doctor")
    r = client.get(path.format(pid=patient["pid"]), headers=hdr)
    kinds = {e.get("type") or e.get("kind") for e in r.json()["events"]}
    assert {"medication", "administration"} <= kinds
    assert f"{DRUG} 500 mg oral" in r.text


# ── Orders, order sets, Helix, the verification queue, the order store ───────

def test_medication_orders_and_order_sets_are_refused_under_the_profile(ward_record, patient):
    ward_record(True)
    _, hdr = _mk_user("doctor")
    r = client.get(f"/api/orders/patient/{patient['pid']}", headers=hdr)
    assert r.status_code == 200, r.text
    assert [o["id"] for o in r.json()["orders"]] == [patient["lab_order"]]
    for path in (f"/api/orders/{patient['med_order']}",
                 f"/api/orders/{patient['med_order']}/events"):
        r = client.get(path, headers=hdr)
        assert r.status_code == 404 and r.json()["detail"] == DRUG_CHART_OFF, path
    r = client.post(f"/api/orders/{patient['med_order']}/transition", headers=hdr,
                    json={"to_status": "cancelled", "reason": "x"})
    assert r.status_code == 404 and r.json()["detail"] == DRUG_CHART_OFF
    r = client.post("/api/orders", headers=hdr, json={
        "patient_id": patient["pid"], "order_type": "medication",
        "description": "Paracetamol 1 g", "code": "161", "code_system": "RXNORM"})
    assert r.status_code == 404 and r.json()["detail"] == DRUG_CHART_OFF
    for method, path in (("get", "/api/orders/sets"), ("get", "/api/orders/sets/any"),
                         ("post", "/api/orders/sets/any/preview"),
                         ("post", "/api/orders/sets/any/apply"),
                         ("get", f"/api/orders/sets/applications/{patient['pid']}")):
        r = getattr(client, method)(path, headers=hdr,
                                    **({"json": {"patient_id": patient["pid"]}} if method == "post" else {}))
        assert r.status_code == 404 and r.json()["detail"] == DRUG_CHART_OFF, path
    # A laboratory order is not the drug chart.
    assert client.get(f"/api/orders/{patient['lab_order']}", headers=hdr).status_code == 200


def test_orders_are_as_before_with_the_profile_off(ward_record, patient):
    ward_record(False)
    _, hdr = _mk_user("doctor")
    ids = {o["id"] for o in client.get(f"/api/orders/patient/{patient['pid']}",
                                       headers=hdr).json()["orders"]}
    assert ids == {patient["lab_order"], patient["med_order"]}
    assert client.get(f"/api/orders/{patient['med_order']}", headers=hdr).status_code == 200
    assert client.get("/api/orders/sets", headers=hdr).status_code == 200


@pytest.mark.parametrize("prefix", ["/api/helix", "/api/pharmacy", "/api/medorders"])
def test_every_pharmacy_route_is_refused_first_under_the_profile(ward_record, prefix):
    ward_record(True)
    from fastapi.routing import APIRoute
    routes = [r for r in app.routes if isinstance(r, APIRoute) and r.path.startswith(prefix + "/")]
    assert routes, prefix
    for route in routes:
        first = route.dependant.dependencies[0].call
        assert getattr(first, "__name__", "") == "require_drug_chart", route.path
    _, hdr = _mk_user("pharmacist")
    r = client.get({"/api/helix": "/api/helix/orders/pending",
                    "/api/pharmacy": "/api/pharmacy/pending",
                    "/api/medorders": "/api/medorders/queue/to-sign"}[prefix], headers=hdr)
    assert r.status_code == 404 and r.json()["detail"] == DRUG_CHART_OFF


def test_the_pharmacy_queues_answer_with_the_profile_off(ward_record):
    ward_record(False)
    _, hdr = _mk_user("pharmacist")
    assert client.get("/api/helix/orders/pending", headers=hdr).status_code == 200
    assert client.get("/api/pharmacy/pending", headers=hdr).status_code == 200


# ── The downtime pack ────────────────────────────────────────────────────────

def test_the_downtime_snapshot_and_ward_pack_carry_no_drug_under_the_profile(ward_record, patient):
    from services import downtime_service
    ward_record(True)
    env = downtime_service.capture_snapshot(patient["pid"], "WDCADM")
    snap = env["snapshot"]
    assert snap["medications"] == [] and snap["drug_chart"] is False
    assert "medications" not in snap["counts"]
    assert _no_medicine_words(env["summary"]) == []
    assert "Penicillin" in json.dumps(snap["allergies"])
    _, hdr = _mk_user("doctor")
    for path in ("latest", "history"):
        r = client.get(f"/api/downtime/{patient['pid']}/{path}", headers=hdr)
        assert r.status_code == 200, (path, r.text)
        assert DRUG not in r.text and '"drug_name"' not in r.text, path
        rows = r.json() if isinstance(r.json(), list) else [r.json()]
        for env in (rows if path == "latest" else r.json().get("snapshots", rows)):
            assert _no_medicine_words(env.get("summary") or "") == [], path
    pack = client.get(f"/api/downtime/{patient['pid']}/ward-pack", headers=hdr)
    assert pack.status_code == 200
    # The printed pack is words a nurse reads: none of them about medicines.
    assert _no_medicine_words(pack.text) == [], _no_medicine_words(pack.text)
    assert "Penicillin" in pack.text


def test_the_downtime_capture_reads_no_drug_under_the_profile(ward_record, monkeypatch, patient):
    # Not only stripped on the way out: the rows are never read.
    from services import downtime_service
    ward_record(True)
    monkeypatch.setattr(downtime_service, "_without_drug_chart", lambda snap: snap)
    env = downtime_service.capture_snapshot(patient["pid"], "WDCADM")
    assert env["snapshot"]["medications"] == []


def test_a_snapshot_taken_before_the_profile_reads_back_without_its_drugs(ward_record, patient):
    from services import downtime_service
    ward_record(False)
    before = downtime_service.capture_snapshot(patient["pid"], "WDCADM")
    assert [m["drug_name"] for m in before["snapshot"]["medications"]] == [DRUG]
    assert "1 active med" in before["summary"]
    ward_record(True)
    env = downtime_service.get_latest_snapshot(patient["pid"])
    assert env["snapshot"]["medications"] == [] and env["snapshot"]["drug_chart"] is False
    assert _no_medicine_words(env["summary"]) == []


# ── Discharge ────────────────────────────────────────────────────────────────

def test_the_recorded_discharge_summary_says_nothing_about_medicines_under_the_profile(
        ward_record, patient):
    from services.discharge_service import (build_medication_reconciliation,
                                            offline_discharge_summary)
    ward_record(True)
    rec = build_medication_reconciliation(patient["pid"])
    assert rec["continued"] == [] and rec["stopped"] == [] and rec["unverified"] == []
    text = offline_discharge_summary(patient["pid"])
    assert _no_medicine_words(text) == []
    assert "Penicillin" in text
    ward_record(False)
    assert "MEDICATION RECONCILIATION" in offline_discharge_summary(patient["pid"])


# ── Bridge ───────────────────────────────────────────────────────────────────

def test_bridge_shows_the_patient_no_drug_under_the_profile(ward_record, patient):
    ward_record(True)
    hdr = {"Authorization": f"Bearer {create_token(patient['portal'], 'patient')}"}
    r = client.get("/api/bridge/reports", headers=hdr)
    assert r.status_code == 200, r.text
    assert r.json()["medications"] == [] and r.json()["drug_chart"] is False
    assert DRUG not in r.text
    r = client.get("/api/bridge/medications", headers=hdr)
    assert r.status_code == 404 and r.json()["detail"] == DRUG_CHART_OFF
    ward_record(False)
    r = client.get("/api/bridge/reports", headers=hdr)
    assert r.json()["drug_chart"] is True and DRUG in r.text
    assert client.get("/api/bridge/medications", headers=hdr).status_code == 200


# ── The write lanes no route reaches under the profile ───────────────────────

def test_a_sage_medication_handoff_is_blocked_before_any_screen(ward_record, monkeypatch):
    from services import sage_handoff_service as h
    ward_record(True)

    def _no_screen(*a, **k):
        raise AssertionError("the drug screen ran under the profile")
    monkeypatch.setattr(h, "_screen_helix", _no_screen)
    action = {"action_type": "prescribe", "patient_id": "WDC-NONE", "target": DRUG}
    blocked = h.prescreen_handoff(action, "WDCADM")
    assert isinstance(blocked, h.HandoffBlocked) and blocked.reason == DRUG_CHART_OFF
    with pytest.raises(h.HandoffBlocked):
        h._write_helix(None, action, "WDCADM", {"blocking": False})
    with pytest.raises(h.HandoffBlocked):
        h._dispatch_to_helix(action, "WDCADM")


def test_a_signed_note_charts_no_medicine_under_the_profile(ward_record, monkeypatch, patient):
    from services import nervous_system_triggers as nst
    ward_record(True)

    def _no_gate(*a, **k):
        raise AssertionError("the drug screen ran under the profile")
    monkeypatch.setattr(nst, "enforce_medication_safety", _no_gate, raising=False)
    updates = {"medications": [{"drug_name": "Paracetamol", "action": "start", "dosage": "1 g"},
                               {"drug_name": DRUG, "action": "stop"}]}
    assert nst._screen_medications(patient["pid"], "WDCADM", 1, updates, "") == ([], [])


def test_revenue_integrity_names_no_drug_under_the_profile(ward_record, patient):
    from services import coding_service
    ward_record(True)
    assert coding_service._load_patient_context(patient["pid"])["medications"] == []
    ward_record(False)
    assert coding_service._load_patient_context(patient["pid"])["medications"] == [DRUG]
