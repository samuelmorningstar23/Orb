"""The medication-order API: scope, the prescriber's steps, the queues, the
admin actions and the socket event (medorders-4, W5-E).

Pinned here:
  * the store off: every route answers 404 feature_disabled; record mode:
    reads and the admin adopt answer, the lifecycle writes still 404;
  * a cross-ward read or write answers 404 after a patient_scope_denied row,
    the same answer an unknown id gets;
  * a nurse cannot sign or create as prescribed (403); she can create a
    draft; a doctor signing meets the 428 on a severe allergy, and only a
    prescriber's acknowledgement can clear it;
  * a stale version is 409 stale_version; an allergy charted between the
    screen and the write is 409 screen_stale and writes nothing;
  * sign and create-as-prescribed write the ward row, linked, with the signer
    as prescriber; a same-ingredient drug comes back as a notice;
  * cancel: a draft by its creator or any order.write holder in scope, with a
    reason; a nurse cannot cancel a signed order (403), a prescriber is told
    it is not offered yet (409);
  * the to-sign queue is scoped, oldest first, marks a draft waiting over 24
    hours, lists nurse-charted live orders, and is audited;
  * parity and adopt are admin.overview; the patient list writes nothing;
  * 'medorders:update' carries ids, status and version only, to the
    patient's department room and dept:__all__.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import config
from auth import create_token
from database import db_session
from main import app
from services import medication_safety_gate
from services.clinical_records_service import add_allergy
from services.clinical_time import format_utc
from services.medorders import chart_lane
from routes.medorders import core as routes_core
from ws import realtime

client = TestClient(app)
DEPT_A, DEPT_B = "Cardiology", "Oncology"
_PATIENTS: list[str] = []


@pytest.fixture(scope="module", autouse=True)
def _cleanup_store_rows():
    yield
    if not _PATIENTS:
        return
    marks = ", ".join("?" for _ in _PATIENTS)
    with db_session() as db:
        db.execute(f"DELETE FROM med_order_events WHERE patient_id IN ({marks})", _PATIENTS)
        db.execute(f"DELETE FROM med_orders WHERE patient_id IN ({marks})", _PATIENTS)
        db.execute(f"UPDATE medications SET med_order_id = NULL WHERE patient_id IN ({marks})",
                   _PATIENTS)


def _user(role, department=None):
    uid = f"MORT{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute("INSERT INTO users (id, name, role, password_hash, department) "
                   "VALUES (?, ?, ?, 'x', ?)", (uid, f"Routes {role} {uid}", role, department))
    return uid


def _patient(department):
    pid = f"PMORT{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute("INSERT INTO patients (id, name, mrn, status) VALUES (?, ?, ?, 'admitted')",
                   (pid, f"Routes {department}", f"MRN-{uuid.uuid4().hex[:8]}"))
        db.execute("INSERT OR IGNORE INTO users (id, name, role, password_hash) "
                   "VALUES ('MORTADM', 'Routes admitter', 'doctor', 'x')")
        db.execute("INSERT INTO admissions (id, patient_id, department, ward, bed, status, "
                   "admitted_by) VALUES (?, ?, ?, ?, 'B-2', 'active', 'MORTADM')",
                   (f"ADM-{pid}", pid, department, department))
    _PATIENTS.append(pid)
    return pid


class Who:
    def __init__(self, role, department=None):
        self.role = role
        self.id = _user(role, department)
        self.hdr = {"Authorization": f"Bearer {create_token(self.id, role)}"}


@pytest.fixture()
def world():
    return {
        "pa": _patient(DEPT_A), "pb": _patient(DEPT_B),
        "doctor_a": Who("doctor", DEPT_A), "doctor_b": Who("doctor", DEPT_B),
        "nurse_a": Who("nurse", DEPT_A), "nurse_a2": Who("nurse", DEPT_A),
        "pharmacist": Who("pharmacist"), "admin": Who("admin"),
    }


@pytest.fixture()
def store_on(monkeypatch):
    monkeypatch.setattr(config, "MED_ORDER_STORE_ENABLED", True)


@pytest.fixture()
def lifecycle_on(store_on, monkeypatch):
    monkeypatch.setattr(config, "MED_ORDER_LIFECYCLE_ENABLED", True)


@pytest.fixture()
def pushes(monkeypatch):
    sent = []
    monkeypatch.setattr(realtime, "schedule_patient_vitals",
                        lambda event, payload: sent.append((event, payload)) or True)
    return sent


def _create(who, pid, *, drug="Paracetamol", status="draft", dose="500 mg",
            route="Oral", frequency="TDS", key=None, ack=None, **extra):
    body = {"patient_id": pid, "drug_name": drug, "status": status, "dose": dose,
            "route": route, "frequency": frequency, **extra}
    if ack is not None:
        body["safety_ack"] = ack
    headers = dict(who.hdr)
    if key:
        headers["Idempotency-Key"] = key
    return client.post("/api/medorders/orders", json=body, headers=headers)


def _draft(who, pid, **kw):
    resp = _create(who, pid, **kw)
    assert resp.status_code == 200, resp.text
    return resp.json()["order"]


def _sign(who, order, *, version=None, ack=None):
    body = {"expected_version": order["version"] if version is None else version}
    if ack is not None:
        body["safety_ack"] = ack
    return client.post(f"/api/medorders/orders/{order['id']}/sign", json=body, headers=who.hdr)


def _cancel(who, order, reason="Entered on the wrong patient"):
    return client.post(f"/api/medorders/orders/{order['id']}/cancel",
                       json={"reason": reason, "expected_version": order["version"]},
                       headers=who.hdr)


def _order(order_id):
    with db_session() as db:
        return dict(db.execute("SELECT * FROM med_orders WHERE id = ?", (order_id,)).fetchone())


def _ward_rows(pid):
    with db_session() as db:
        return [dict(r) for r in db.execute(
            "SELECT * FROM medications WHERE patient_id = ? ORDER BY id", (pid,)).fetchall()]


def _audit_rows(action, **where):
    sql, args = "SELECT * FROM audit_logs WHERE action_type = ?", [action]
    for col, val in where.items():
        sql += f" AND {col} = ?"
        args.append(val)
    with db_session() as db:
        return [dict(r) for r in db.execute(sql + " ORDER BY rowid", args).fetchall()]


# ── The switches ──────────────────────────────────────────────────────────────

def _every_route(order_id, pid):
    return [
        ("get", f"/api/medorders/patient/{pid}", None),
        ("get", f"/api/medorders/orders/{order_id}", None),
        ("get", f"/api/medorders/orders/{order_id}/events", None),
        ("post", "/api/medorders/orders", {"patient_id": pid, "drug_name": "Paracetamol"}),
        ("post", f"/api/medorders/orders/{order_id}/sign", {"expected_version": 1}),
        ("post", f"/api/medorders/orders/{order_id}/cancel", {"reason": "x"}),
        ("get", "/api/medorders/queue/to-sign", None),
        ("get", "/api/medorders/admin/parity", None),
        ("post", "/api/medorders/admin/adopt", {}),
    ]


def test_every_route_is_404_with_the_store_off(world):
    for who in (world["doctor_a"], world["admin"]):
        for method, path, body in _every_route(str(uuid.uuid4()), world["pa"]):
            kw = {"json": body} if body is not None else {}
            resp = getattr(client, method)(path, headers=who.hdr, **kw)
            assert resp.status_code == 404, (method, path, resp.text)
            assert resp.json()["detail"]["error"] == "feature_disabled", (method, path)


def test_record_mode_opens_reads_and_adopt_but_not_the_lifecycle_writes(world, store_on):
    doc, admin = world["doctor_a"], world["admin"]
    assert client.get(f"/api/medorders/patient/{world['pa']}", headers=doc.hdr).status_code == 200
    assert client.get("/api/medorders/queue/to-sign", headers=doc.hdr).status_code == 200
    resp = _create(doc, world["pa"])
    assert resp.status_code == 404
    assert resp.json()["detail"]["feature"] == "med_order_lifecycle"
    resp = client.post("/api/medorders/admin/adopt", json={"patient_ids": [world["pa"]]},
                       headers=admin.hdr)
    assert resp.status_code == 200, resp.text


# ── Scope ─────────────────────────────────────────────────────────────────────

def test_a_cross_ward_read_or_write_is_an_audited_404(world, lifecycle_on):
    order = _draft(world["doctor_b"], world["pb"])
    doc_a = world["doctor_a"]
    unknown = str(uuid.uuid4())
    for path in (f"/api/medorders/orders/{order['id']}",
                 f"/api/medorders/orders/{order['id']}/events"):
        resp = client.get(path, headers=doc_a.hdr)
        assert resp.status_code == 404 and resp.json()["detail"] == "Medication order not found"
    same = client.get(f"/api/medorders/orders/{unknown}", headers=doc_a.hdr)
    assert same.status_code == 404 and same.json() == resp.json()
    assert _sign(doc_a, order).status_code == 404
    assert _cancel(doc_a, order).status_code == 404
    denied = _audit_rows("patient_scope_denied", actor_user_id=doc_a.id, target_id=order["id"])
    assert len(denied) == 4
    assert all(r["patient_id"] == world["pb"] and r["outcome"] == "denied" for r in denied)
    assert _order(order["id"])["status"] == "draft"

    resp = _create(doc_a, world["pb"])
    assert resp.status_code == 404 and resp.json()["detail"] == "Patient not found"
    assert _audit_rows("patient_scope_denied", actor_user_id=doc_a.id, target_id=world["pb"])
    resp = client.get(f"/api/medorders/patient/{world['pb']}", headers=doc_a.hdr)
    assert resp.status_code == 404


# ── Who may do what ───────────────────────────────────────────────────────────

def test_a_nurse_creates_a_draft_but_cannot_sign_or_create_as_prescribed(world, lifecycle_on):
    nurse = world["nurse_a"]
    resp = _create(nurse, world["pa"], status="prescribed")
    assert resp.status_code == 403
    assert resp.json()["detail"]["missing"] == ["medication.prescribe"]
    assert _audit_rows("permission_denied", actor_user_id=nurse.id)
    order = _draft(nurse, world["pa"])
    assert order["status"] == "draft" and order["entered_by_role"] == "nurse"
    assert order["source"] == "cpoe" and order["prescriber_id"] is None
    assert _sign(nurse, order).status_code == 403
    assert _sign(nurse, order, ack={"acknowledged": True, "reason": "x"}).status_code == 403
    assert _order(order["id"])["status"] == "draft"
    assert _ward_rows(world["pa"]) == []


def test_a_pharmacist_cannot_create_or_read_through_these_routes(world, lifecycle_on):
    ph = world["pharmacist"]
    assert _create(ph, world["pa"]).status_code == 403
    order = _draft(world["doctor_a"], world["pa"])
    assert client.get(f"/api/medorders/orders/{order['id']}", headers=ph.hdr).status_code == 403


def test_a_severe_allergy_is_a_428_at_sign_and_only_a_prescriber_clears_it(world, lifecycle_on):
    pid = world["pa"]
    add_allergy(pid, "amoxicillin", reaction="anaphylaxis", severity="severe")
    order = _draft(world["nurse_a"], pid, drug="Amoxicillin 500mg")
    resp = _sign(world["doctor_a"], order)
    assert resp.status_code == 428, resp.text
    assert resp.json()["detail"]["error"] == "drug_safety_acknowledgement_required"
    assert _order(order["id"])["status"] == "draft" and _ward_rows(pid) == []
    # The nurse never reaches the gate, whatever she types.
    assert _sign(world["nurse_a"], order,
                 ack={"acknowledged": True, "reason": "consultant asked"}).status_code == 403
    resp = _sign(world["doctor_a"], order,
                 ack={"acknowledged": True, "reason": "Desensitisation protocol, ICU"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["safety"]["override"]["reason"] == "Desensitisation protocol, ICU"
    assert _order(order["id"])["status"] == "prescribed"


# ── Sign ──────────────────────────────────────────────────────────────────────

def test_signing_writes_the_ward_row_with_the_signer_as_prescriber(world, lifecycle_on, pushes):
    pid, doc = world["pa"], world["doctor_a"]
    order = _draft(world["nurse_a"], pid, drug="Pantoprazole", dose="40 mg", frequency="OD")
    resp = _sign(doc, order)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    signed = _order(order["id"])
    assert signed["status"] == "prescribed" and signed["prescriber_id"] == doc.id
    assert signed["entered_by"] == world["nurse_a"].id and signed["version"] == 2
    assert signed["screen_fingerprint"].startswith("v1:")
    (row,) = _ward_rows(pid)
    assert row["id"] == body["medication_id"] == signed["legacy_medication_id"]
    assert row["med_order_id"] == order["id"] and row["status"] == "active"
    assert row["prescribed_by"] == doc.id and row["dosage"] == "40 mg"
    assert _audit_rows("med_order_signed", target_id=order["id"])
    assert body["order"]["fhir_status"] == "active"
    assert pushes[-1] == ("medorders:update", {"patient_id": pid, "med_order_id": order["id"],
                                               "status": "prescribed", "version": 2})


def test_a_stale_version_is_409_with_the_current_order(world, lifecycle_on):
    order = _draft(world["doctor_a"], world["pa"])
    resp = _sign(world["doctor_a"], order, version=order["version"] + 1)
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["error"] == "stale_version"
    assert detail["order"] == {"id": order["id"], "status": "draft", "version": 1}


def test_an_allergy_charted_between_the_screen_and_the_write_is_409_screen_stale(
        world, lifecycle_on, monkeypatch):
    pid = world["pa"]
    order = _draft(world["doctor_a"], pid, drug="Ceftriaxone", dose="1 g", frequency="OD")
    real_gate = medication_safety_gate.enforce_medication_safety

    def _gate_then_chart_an_allergy(*a, **k):
        verdict = real_gate(*a, **k)
        add_allergy(pid, "ceftriaxone", reaction="rash", severity="moderate")
        return verdict

    monkeypatch.setattr(medication_safety_gate, "enforce_medication_safety",
                        _gate_then_chart_an_allergy)
    resp = _sign(world["doctor_a"], order)
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["error"] == "screen_stale"
    assert resp.json()["detail"]["message"] == chart_lane.STALE_SCREEN_MESSAGE
    assert _order(order["id"])["status"] == "draft" and _ward_rows(pid) == []


@pytest.mark.parametrize("missing", ["dose", "route", "frequency"])
def test_a_draft_without_a_dose_route_or_frequency_cannot_be_signed(world, lifecycle_on, missing):
    """The ward row needs all three, and the route is asked for, never
    defaulted to the column's 'Oral' (a route nobody chose)."""
    pid = world["pa"]
    order = _draft(world["doctor_a"], pid, **{missing: None})
    resp = _sign(world["doctor_a"], order)
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"] == "dose_route_and_frequency_required"
    resp = _create(world["doctor_a"], pid, status="prescribed", **{missing: "  "})
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"] == "dose_route_and_frequency_required"
    assert _order(order["id"])["status"] == "draft" and _ward_rows(pid) == []


# ── Create ────────────────────────────────────────────────────────────────────

def test_create_as_prescribed_writes_the_ward_row_and_names_a_duplicate(world, lifecycle_on):
    pid, doc = world["pa"], world["doctor_a"]
    with db_session() as db:
        db.execute("INSERT INTO medications (patient_id, drug_name, dosage, route, frequency, "
                   "status, source) VALUES (?, 'Paracetamol', '1 g', 'Oral', 'QDS', 'active', "
                   "'clinician')", (pid,))
    resp = _create(doc, pid, drug="Paracetamol 500 mg", status="prescribed")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["order"]["status"] == "prescribed"
    assert body["order"]["prescriber_id"] == doc.id
    assert [d["drug_name"] for d in body["duplicates"]] == ["Paracetamol"]
    linked = [r for r in _ward_rows(pid) if r["med_order_id"] == body["order"]["id"]]
    assert len(linked) == 1 and linked[0]["id"] == body["medication_id"]
    assert body["order"]["admission_id"] == f"ADM-{pid}"


def test_a_create_replay_returns_one_order_and_one_event(world, lifecycle_on, pushes):
    key = f"create-{uuid.uuid4()}"
    first = _create(world["doctor_a"], world["pa"], key=key)
    again = _create(world["doctor_a"], world["pa"], key=key)
    assert first.status_code == again.status_code == 200
    assert first.json()["order"]["id"] == again.json()["order"]["id"]
    assert again.json()["replayed"] is True
    with db_session() as db:
        assert db.execute("SELECT COUNT(*) FROM med_order_events WHERE med_order_id = ?",
                          (first.json()["order"]["id"],)).fetchone()[0] == 1
    assert len(pushes) == 1
    other = _create(world["doctor_a"], world["pa"], key=key, drug="Ibuprofen")
    assert other.status_code == 409
    assert other.json()["detail"]["error"] == "idempotency_key_reused"


# ── Cancel ────────────────────────────────────────────────────────────────────

def test_cancel_rules(world, lifecycle_on):
    pid = world["pa"]
    nurse, nurse2, doc = world["nurse_a"], world["nurse_a2"], world["doctor_a"]

    own = _draft(nurse, pid)
    resp = _cancel(nurse, own)
    assert resp.status_code == 200, resp.text
    assert resp.json()["order"]["status"] == "cancelled"
    assert _order(own["id"])["status_reason"] == "Entered on the wrong patient"

    other = _draft(nurse, pid, drug="Ibuprofen")
    assert _cancel(nurse2, other).status_code == 200  # any order.write holder in scope

    blank = _draft(nurse, pid, drug="Ondansetron")
    resp = _cancel(nurse, blank, reason="   ")
    assert resp.status_code == 422 and resp.json()["detail"]["error"] == "reason_required"

    signed = _draft(doc, pid, drug="Pantoprazole")
    signed = _sign(doc, signed).json()["order"]
    resp = _cancel(nurse, signed)
    assert resp.status_code == 403 and resp.json()["detail"]["error"] == "prescriber_required"
    assert _audit_rows("med_order_step_refused", actor_user_id=nurse.id)
    resp = _cancel(doc, signed)
    assert resp.status_code == 409 and resp.json()["detail"]["error"] == "cancel_not_offered"
    assert _order(signed["id"])["status"] == "prescribed"
    assert _cancel(world["pharmacist"], blank).status_code == 403


# ── Queues ────────────────────────────────────────────────────────────────────

def test_the_to_sign_queue_is_scoped_oldest_first_and_lists_nurse_charted_orders(
        world, lifecycle_on):
    pa, pb, doc = world["pa"], world["pb"], world["doctor_a"]
    old = _draft(world["nurse_a"], pa, drug="Ibuprofen")
    new = _draft(doc, pa, drug="Paracetamol")
    hidden = _draft(world["doctor_b"], pb, drug="Ondansetron")
    with db_session() as db:
        db.execute("UPDATE med_orders SET created_at = ? WHERE id = ?",
                   (format_utc(datetime.now(timezone.utc) - timedelta(hours=25)), old["id"]))
    charted = client.post(f"/api/vigil/patients/{pa}/medications", headers=world["nurse_a"].hdr,
                          json={"drug_name": "Cetirizine", "dosage": "10 mg", "frequency": "OD"})
    assert charted.status_code == 200, charted.text

    resp = client.get("/api/medorders/queue/to-sign", headers=doc.hdr)
    assert resp.status_code == 200, resp.text
    queue = resp.json()
    ids = [d["id"] for d in queue["drafts"]]
    assert hidden["id"] not in ids
    assert ids.index(old["id"]) < ids.index(new["id"])
    flags = {d["id"]: d["waiting_over_24h"] for d in queue["drafts"]}
    assert flags[old["id"]] is True and flags[new["id"]] is False
    nurse_list = [o for o in queue["entered_by_a_nurse"] if o["patient_id"] == pa]
    assert [o["id"] for o in nurse_list] == [charted.json()["med_order_id"]]
    assert all(o["patient_id"] != pb for o in queue["entered_by_a_nurse"])
    (audit,) = _audit_rows("med_orders_to_sign_viewed", actor_user_id=doc.id)
    assert audit["event_category"] == "phi_access"
    assert old["id"] in json.loads(audit["metadata_json"])["med_order_ids"]
    assert client.get("/api/medorders/queue/to-sign",
                      headers=world["nurse_a"].hdr).status_code == 403


# ── Reads ─────────────────────────────────────────────────────────────────────

def test_the_patient_list_shows_orders_and_unadopted_rows_and_writes_nothing(world, store_on):
    pid = world["pa"]
    with db_session() as db:
        med_id = db.execute(
            "INSERT INTO medications (patient_id, drug_name, dosage, route, frequency, status, "
            "source) VALUES (?, 'Ondansetron', '4 mg', 'IV', 'PRN', 'prn', 'clinician')",
            (pid,)).lastrowid
        before = db.execute("SELECT COUNT(*) FROM med_orders").fetchone()[0]
    resp = client.get(f"/api/medorders/patient/{pid}", headers=world["nurse_a"].hdr)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    (legacy,) = [r for r in body["legacy"] if r["medication_id"] == med_id]
    assert legacy["status"] == "prescribed" and legacy["is_prn"] is True
    assert legacy["legacy"] is True
    with db_session() as db:
        assert db.execute("SELECT COUNT(*) FROM med_orders").fetchone()[0] == before
        assert db.execute("SELECT med_order_id FROM medications WHERE id = ?",
                          (med_id,)).fetchone()[0] is None
    assert _audit_rows("med_orders_viewed", actor_user_id=world["nurse_a"].id, patient_id=pid)


def test_an_order_and_its_events_are_read_with_a_phi_row(world, lifecycle_on):
    doc = world["doctor_a"]
    order = _draft(doc, world["pa"])
    resp = client.get(f"/api/medorders/orders/{order['id']}", headers=doc.hdr)
    assert resp.status_code == 200 and resp.json()["order"]["id"] == order["id"]
    resp = client.get(f"/api/medorders/orders/{order['id']}/events", headers=doc.hdr)
    assert [e["kind"] for e in resp.json()["events"]] == ["created"]
    assert _audit_rows("med_order_viewed", target_id=order["id"])
    assert _audit_rows("med_order_events_viewed", target_id=order["id"])


# ── Administration ────────────────────────────────────────────────────────────

def test_parity_and_adopt_are_the_administrators(world, store_on):
    admin, doc = world["admin"], world["doctor_a"]
    pid = world["pa"]
    with db_session() as db:
        med_id = db.execute(
            "INSERT INTO medications (patient_id, drug_name, dosage, route, frequency, status, "
            "source) VALUES (?, 'Aspirin', '75 mg', 'Oral', 'OD', 'active', 'clinician')",
            (pid,)).lastrowid
    assert client.get("/api/medorders/admin/parity", headers=doc.hdr).status_code == 403
    assert client.post("/api/medorders/admin/adopt", json={}, headers=doc.hdr).status_code == 403
    report = client.get("/api/medorders/admin/parity", headers=admin.hdr)
    assert report.status_code == 200 and "clean" in report.json()
    assert _audit_rows("med_order_parity_report", actor_user_id=admin.id)
    resp = client.post("/api/medorders/admin/adopt", json={"patient_ids": [pid]},
                       headers=admin.hdr)
    assert resp.status_code == 200, resp.text
    assert resp.json()["adopted"] >= 1
    with db_session() as db:
        assert db.execute("SELECT med_order_id FROM medications WHERE id = ?",
                          (med_id,)).fetchone()[0] is not None


# ── The socket event ──────────────────────────────────────────────────────────

def test_the_update_carries_ids_only(world, lifecycle_on, pushes):
    order = _draft(world["doctor_a"], world["pa"], drug="Metoprolol", dose="25 mg")
    (event, payload) = pushes[-1]
    assert event == routes_core.MEDORDERS_UPDATE_EVENT == "medorders:update"
    assert set(payload) == {"patient_id", "med_order_id", "status", "version"}
    assert payload["med_order_id"] == order["id"] and payload["status"] == "draft"
    assert "Metoprolol" not in json.dumps(payload) and "25 mg" not in json.dumps(payload)


def test_the_update_goes_to_the_department_room_and_all(world, monkeypatch):
    sent = []

    async def _emit(event, payload, room=None):
        sent.append((event, payload, room))

    monkeypatch.setattr(realtime.sio, "emit", _emit)
    payload = {"patient_id": world["pa"], "med_order_id": str(uuid.uuid4()),
               "status": "draft", "version": 1}
    asyncio.run(realtime.emit_patient_vitals("medorders:update", dict(payload)))
    assert sorted(room for _, _, room in sent) == sorted(["dept:__all__", f"dept:{DEPT_A}"])
    assert all(p == payload for _, p, _ in sent), "nothing is added to the payload"
