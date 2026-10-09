"""Pharmacy supply for a medication order, the Helix hook and the dose's
custody link (medorders-5b, W7-E; design sections 5 and 6).

Pinned here:
  * POST /api/medorders/orders/{id}/request-supply: off with either switch off;
    a prescriber or a pharmacist only; an audited 404 outside the department;
    one open supply row per order (a second request answers the same row); a
    new row only once the open one is closed; refused for an order that is not
    live, is held, names no formulary product, is supplied another way or
    still carries a placeholder;
  * POST /api/medorders/orders/{id}/supply-source (MO-R12 (a)): the
    prescriber alone picks pharmacy, ward stock or the patient's own supply,
    as an 'amended' event with its audit row; ward stock means pharmacy is not
    asked; moving away from pharmacy waits for the open supply to close;
    refused for an ended order and a stale version;
  * record mode: Helix steps on a linked row write supply events and leave the
    order alone; with both switches off a linked row still records them (keyed
    on the link);
  * lifecycle: a Sage order verified from either queue is verified on both
    surfaces and has exactly one supply row; a Helix rejection of a waiting
    order rejects it; a substituted supply never verifies the order; only a
    pharmacist decides the order;
  * a version race in the hook rolls the Helix step back, its case-room
    message included; a replayed key is answered as before;
  * the linked screen: the dose guard fires at Helix verify, the order's own
    chart row is left out of the interaction check (but a substitute is still
    screened against it, R002 being held), a placeholder or an out-of-date
    dose is never verified, and nothing is committed to an ended order;
  * custody: two received units leave the dose 'supply not linked', one unit
    links it, and a second dose does not reuse it;
  * unlinked rows exactly as before.
"""
from __future__ import annotations

import json
import uuid

import pytest
from fastapi.testclient import TestClient

import config
from auth import hash_password
from database import db_session
from main import app
from routes import helix_routes
from services import emar_service, helix_service
from services import pharmacy_verification_service as pharmacy
from services.helix_db import init_helix_db
from services.medorders import core, supply

client = TestClient(app)

DEPT = "W7E Link Ward"
OTHER_DEPT = "W7E Other Ward"
# Every word of a fixture product's name is a drug token for the session
# (tests/test_patient_specific_screen.py checks none is ordinary English), so
# the names carry no English word.
DRUG_ID, DRUG_NAME = "HXW7ELINK", "Zylomed W7e Fixture 500mg Tablet"
SUB_ID, SUB_NAME = "HXW7ESUB", "Zylomed W7e Brand B 500mg Tablet"
PARA_ID, PARA_NAME = "HXW7EPARA", "Paracetamol W7e Fixture 500mg Tablet"
ENOX_ID, ENOX_NAME = "HXW7EENOX", "Clexane W7e Fixture 40mg Injection"
ENOX_SUB_ID, ENOX_SUB_NAME = "HXW7EENOXB", "Lomoh W7e Fixture 40mg Injection"
ADMITTED_BY = "W7EADMITTER"
GOOD_REASON = "loading dose agreed with the consultant, weight 120 kg, LFTs normal"
_PATIENTS: list[str] = []


@pytest.fixture(scope="module", autouse=True)
def _seed_module():
    init_helix_db()
    with db_session() as db:
        for mid, name, generic, form, strength in (
                (DRUG_ID, DRUG_NAME, "zylomed", "tablet", "500mg"),
                (SUB_ID, SUB_NAME, "zylomed", "tablet", "500mg"),
                (PARA_ID, PARA_NAME, "paracetamol", "tablet", "500mg"),
                (ENOX_ID, ENOX_NAME, "enoxaparin", "injection", "40mg"),
                (ENOX_SUB_ID, ENOX_SUB_NAME, "enoxaparin", "injection", "40mg")):
            db.execute("INSERT OR IGNORE INTO helix_medication_master "
                       "(id, name, generic_name, form, strength) VALUES (?, ?, ?, ?, ?)",
                       (mid, name, generic, form, strength))
            db.execute("INSERT OR IGNORE INTO helix_inventory_lots "
                       "(id, medication_id, lot_number, quantity, expiration_date) "
                       "VALUES (?, ?, ?, 500, '2030-01-01')", (f"LOT-{mid}", mid, f"L-{mid}"))
        # Helix posts its case-room lines as 'system'; the admissions here are
        # recorded by an account of the module's own.
        db.execute("INSERT OR IGNORE INTO users (id, name, role, password_hash) "
                   "VALUES ('system', 'Orb System', 'admin', 'x')")
        db.execute("INSERT OR IGNORE INTO users (id, name, role, password_hash) "
                   "VALUES (?, 'W7E Admitting Clerk', 'admin', 'x')", (ADMITTED_BY,))
    yield
    if not _PATIENTS:
        return
    marks = ", ".join("?" for _ in _PATIENTS)
    with db_session() as db:
        # Nothing this module wrote may keep pointing at the 'system' account
        # (tests/test_sage_assessment.py deletes it).
        db.execute(f"DELETE FROM messages WHERE channel_id IN ({marks})",
                   [f"case-{pid}" for pid in _PATIENTS])
        db.execute(f"DELETE FROM med_order_events WHERE patient_id IN ({marks})", _PATIENTS)
        db.execute(f"DELETE FROM med_orders WHERE patient_id IN ({marks})", _PATIENTS)
        db.execute(f"UPDATE medications SET med_order_id = NULL, held_at = NULL, "
                   f"hold_reason = NULL WHERE patient_id IN ({marks})", _PATIENTS)
        db.execute(f"UPDATE helix_patient_med_orders SET med_order_id = NULL "
                   f"WHERE patient_id IN ({marks})", _PATIENTS)
        db.execute(f"UPDATE admissions SET status = 'discharged' WHERE patient_id IN ({marks})",
                   _PATIENTS)
        db.execute(f"UPDATE patients SET status = 'discharged' WHERE id IN ({marks})", _PATIENTS)


# ── People, patients and orders ───────────────────────────────────────────────

def _user(role, department=None):
    uid = f"W7E{role[:3].upper()}{uuid.uuid4().hex[:6].upper()}"
    with db_session() as db:
        db.execute("INSERT INTO users (id, name, role, password_hash, department) "
                   "VALUES (?, ?, ?, ?, ?)",
                   (uid, f"{role.title()} {uid}", role, hash_password("secretpass"), department))
    return uid


def _hdr(uid):
    resp = client.post("/api/auth/login", json={"employee_id": uid, "password": "secretpass"})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['token']}"}


@pytest.fixture(scope="module")
def people():
    ids = {"doctor": _user("doctor", DEPT), "other_doctor": _user("doctor", OTHER_DEPT),
           "pharmacist": _user("pharmacist"), "nurse": _user("nurse", DEPT),
           "admin": _user("admin")}
    return {"ids": ids, "hdr": {k: _hdr(v) for k, v in ids.items()}}


def _patient():
    pid = f"PW7E{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute("INSERT INTO patients (id, name, mrn, status, ward, bed) "
                   "VALUES (?, ?, ?, 'admitted', 'W7E', '4')",
                   (pid, f"Helix Link {uuid.uuid4().hex[:5]}", f"MRN-{uuid.uuid4().hex[:8]}"))
        db.execute("INSERT INTO admissions (patient_id, department, ward, bed, status, admitted_by) "
                   "VALUES (?, ?, 'W7E', '4', 'active', ?)", (pid, DEPT, ADMITTED_BY))
        db.execute("INSERT INTO channels (id, name, type, patient_id) "
                   "VALUES (?, ?, 'case_room', ?)", (f"case-{pid}", f"Case {pid}", pid))
    _PATIENTS.append(pid)
    return pid


def _order(prescriber, *, drug=DRUG_NAME, formulary_id=DRUG_ID, dose="500 mg", route="Oral",
           freq="TDS", source="chart", ward_source="clinician", status="prescribed",
           verified_by=None, pid=None):
    """A live order with its ward row, linked both ways, as the store's lanes
    write them. source 'sage_handoff' with ward_source 'llm_extracted' is a
    Sage order still waiting for its pharmacist."""
    pid = pid or _patient()
    with db_session(immediate=True) as conn:
        write = core.create_in_txn(
            conn, patient_id=pid, drug_name=drug, source=source, status=status,
            entered_by=prescriber, entered_by_role="doctor", actor_id=prescriber,
            actor_role="doctor", entry_point="test.helix_link",
            needs_verification=source == "sage_handoff", formulary_id=formulary_id,
            dose=dose, route=route, frequency=freq, prescriber_id=prescriber,
            verified_by=verified_by, verified_at="2026-10-09T08:00:00+00:00" if verified_by else None)
        med_id = conn.execute(
            "INSERT INTO medications (patient_id, drug_name, dosage, route, frequency, status, "
            "prescribed_by, source, verified_by) VALUES (?, ?, ?, ?, ?, 'active', ?, ?, ?)",
            (pid, drug, dose, route, freq, prescriber, ward_source, verified_by)).lastrowid
        core.link_projection_in_txn(conn, write.order["id"], med_id)
    return {"order_id": write.order["id"], "med_id": med_id, "patient_id": pid}


def _move(order_id, to_status, *, actor, reason="Clinical review"):
    with db_session(immediate=True) as conn:
        return core.transition_in_txn(conn, order_id, to_status, expected_version=None,
                                      actor_id=actor, actor_role="doctor",
                                      entry_point="test.move", reason=reason).order


def _request(order_id, actor):
    """The store's request, called directly (the route needs the lifecycle)."""
    return supply.request_supply(order_id, actor_id=actor, actor_role="doctor")["supply"]


@pytest.fixture()
def record_mode(monkeypatch):
    monkeypatch.setattr(config, "MED_ORDER_STORE_ENABLED", True)


@pytest.fixture()
def lifecycle(monkeypatch):
    monkeypatch.setattr(config, "MED_ORDER_STORE_ENABLED", True)
    monkeypatch.setattr(config, "MED_ORDER_LIFECYCLE_ENABLED", True)


# ── Reads ─────────────────────────────────────────────────────────────────────

def _order_row(order_id):
    with db_session() as db:
        return dict(db.execute("SELECT * FROM med_orders WHERE id = ?", (order_id,)).fetchone())


def _events(order_id, kind=None):
    sql, args = "SELECT * FROM med_order_events WHERE med_order_id = ?", [order_id]
    if kind:
        sql += " AND kind = ?"
        args.append(kind)
    with db_session() as db:
        rows = [dict(r) for r in db.execute(sql + " ORDER BY id", args).fetchall()]
    for r in rows:
        r["payload"] = json.loads(r["payload_json"]) if r["payload_json"] else None
    return rows


def _supplies(order_id):
    with db_session() as db:
        return [dict(r) for r in db.execute(
            "SELECT * FROM helix_patient_med_orders WHERE med_order_id = ? ORDER BY created_at, id",
            (order_id,)).fetchall()]


def _helix(hid):
    with db_session() as db:
        return dict(db.execute("SELECT * FROM helix_patient_med_orders WHERE id = ?",
                               (hid,)).fetchone())


def _ward(med_id):
    with db_session() as db:
        return dict(db.execute("SELECT * FROM medications WHERE id = ?", (med_id,)).fetchone())


def _messages(pid):
    with db_session() as db:
        return [r["content"] for r in db.execute(
            "SELECT content FROM messages WHERE channel_id = ? ORDER BY id",
            (f"case-{pid}",)).fetchall()]


def _audits(action, target_id):
    with db_session() as db:
        return [dict(r) for r in db.execute(
            "SELECT * FROM audit_logs WHERE action_type = ? AND target_id = ? ORDER BY id",
            (action, str(target_id))).fetchall()]


def _step(hdr, hid, target, **body):
    return client.post(f"/api/helix/orders/{hid}/transition",
                       json={"target_status": target, **body}, headers=hdr)


def _walk(hdr, hid, *targets):
    for target in targets:
        notes = {"pharmacist_notes": "Not suitable"} if target == "rejected" else {}
        resp = _step(hdr, hid, target, **notes)
        assert resp.status_code == 200, (target, resp.text)
    return resp


def _deliver(hid, pharmacist, nurse):
    """Walk a supply row to the ward through the service, with the store's hook."""
    for target in ("reviewing", "verified", "scheduled", "dispensed"):
        helix_service.transition_order_status(
            hid, target, pharmacist, on_applied=supply.helix_step_hook(
                actor_id=pharmacist, actor_role="pharmacist", may_verify_order=True))
    helix_service.receive_order(hid, nurse, on_applied=supply.helix_step_hook(
        actor_id=nurse, actor_role="nurse"))


def _request_route(hdr, order_id):
    return client.post(f"/api/medorders/orders/{order_id}/request-supply", headers=hdr)


# ── 1. Request from pharmacy ──────────────────────────────────────────────────

def test_request_supply_is_off_with_the_store_or_the_lifecycle_off(people, monkeypatch):
    o = _order(people["ids"]["doctor"])
    resp = _request_route(people["hdr"]["doctor"], o["order_id"])
    assert resp.status_code == 404
    assert resp.json()["detail"]["feature"] == "med_order_store"
    monkeypatch.setattr(config, "MED_ORDER_STORE_ENABLED", True)
    resp = _request_route(people["hdr"]["doctor"], o["order_id"])
    assert resp.status_code == 404
    assert resp.json()["detail"]["feature"] == "med_order_lifecycle"
    assert _supplies(o["order_id"]) == []


def test_request_supply_writes_one_pending_row_and_a_second_request_returns_it(people, lifecycle):
    o = _order(people["ids"]["doctor"])
    before = _order_row(o["order_id"])
    first = _request_route(people["hdr"]["doctor"], o["order_id"])
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["replayed"] is False
    row = _helix(body["supply"]["id"])
    assert (row["status"], row["med_order_id"], row["medication_id"]) == (
        "pending", o["order_id"], DRUG_ID)
    assert (row["dosage"], row["route"], row["frequency"]) == ("500 mg", "Oral", "TDS")
    assert row["prescribed_by"] == people["ids"]["doctor"]
    after = _order_row(o["order_id"])
    assert after["supply_source"] == "pharmacy"
    assert (after["status"], after["version"]) == (before["status"], before["version"])
    [event] = _events(o["order_id"], "supply_requested")
    assert (event["ref_type"], event["ref_id"]) == ("helix_order", row["id"])
    assert len(_audits("med_order_supply_requested", o["order_id"])) == 1

    second = _request_route(people["hdr"]["doctor"], o["order_id"])
    assert second.status_code == 200, second.text
    assert second.json()["replayed"] is True
    assert second.json()["supply"]["id"] == row["id"]
    assert len(_supplies(o["order_id"])) == 1
    assert len(_events(o["order_id"], "supply_requested")) == 1


def test_a_new_supply_is_requested_only_once_the_open_one_is_closed(people, lifecycle):
    o = _order(people["ids"]["doctor"], status="verified", verified_by=people["ids"]["pharmacist"])
    first = _request_route(people["hdr"]["pharmacist"], o["order_id"]).json()["supply"]
    # A verified order: rejecting this supply is a supply step, not the order's.
    _walk(people["hdr"]["pharmacist"], first["id"], "reviewing", "rejected")
    assert _order_row(o["order_id"])["status"] == "verified"
    assert len(_events(o["order_id"], "supply_rejected")) == 1
    again = _request_route(people["hdr"]["doctor"], o["order_id"])
    assert again.status_code == 200, again.text
    assert again.json()["replayed"] is False and again.json()["supply"]["id"] != first["id"]
    assert len(_supplies(o["order_id"])) == 2


def test_request_supply_needs_a_prescriber_or_a_pharmacist(people, lifecycle):
    o = _order(people["ids"]["doctor"])
    for who in ("nurse", "admin"):
        assert _request_route(people["hdr"][who], o["order_id"]).status_code == 403, who
    assert _supplies(o["order_id"]) == []
    assert _request_route(people["hdr"]["pharmacist"], o["order_id"]).status_code == 200


def test_request_supply_outside_the_department_is_an_audited_404(people, lifecycle):
    o = _order(people["ids"]["doctor"])
    resp = _request_route(people["hdr"]["other_doctor"], o["order_id"])
    assert resp.status_code == 404
    assert _supplies(o["order_id"]) == []
    assert _audits("patient_scope_denied", o["order_id"])


def test_request_supply_refuses_what_pharmacy_cannot_supply(people, lifecycle):
    doctor, hdr = people["ids"]["doctor"], people["hdr"]["doctor"]
    unbound = _order(doctor, formulary_id=None)
    held = _order(doctor)
    _move(held["order_id"], "held", actor=doctor, reason="Nil by mouth")
    stopped = _order(doctor)
    _move(stopped["order_id"], "stopped", actor=doctor)
    placeholder = _order(doctor, dose="REVIEW REQUIRED")
    ward_stock = _order(doctor)
    with db_session() as db:
        db.execute("UPDATE med_orders SET supply_source = 'ward_stock' WHERE id = ?",
                   (ward_stock["order_id"],))
    for o, status, code in ((unbound, 409, "not_formulary_bound"),
                            (held, 409, "order_held"),
                            (stopped, 409, "order_not_live"),
                            (placeholder, 422, "dose_route_and_frequency_required"),
                            (ward_stock, 409, "supply_source_conflict")):
        resp = _request_route(hdr, o["order_id"])
        assert resp.status_code == status, (code, resp.text)
        assert resp.json()["detail"]["error"] == code
        assert _supplies(o["order_id"]) == [], code


def _source_route(hdr, order_id, source, **body):
    return client.post(f"/api/medorders/orders/{order_id}/supply-source",
                       json={"supply_source": source, **body}, headers=hdr)


def test_the_supply_source_is_off_with_the_lifecycle_off(people, record_mode):
    o = _order(people["ids"]["doctor"])
    resp = _source_route(people["hdr"]["doctor"], o["order_id"], "ward_stock")
    assert resp.status_code == 404
    assert resp.json()["detail"]["feature"] == "med_order_lifecycle"
    assert _order_row(o["order_id"])["supply_source"] is None


def test_the_prescriber_picks_ward_stock_and_pharmacy_is_then_not_asked(people, lifecycle):
    o = _order(people["ids"]["doctor"])
    hdr = people["hdr"]["doctor"]
    before = _order_row(o["order_id"])
    resp = _source_route(hdr, o["order_id"], "ward_stock",
                         expected_version=before["version"])
    assert resp.status_code == 200, resp.text
    assert resp.json()["replayed"] is False
    after = _order_row(o["order_id"])
    assert after["supply_source"] == "ward_stock"
    assert (after["status"], after["version"]) == (before["status"], before["version"] + 1)
    [event] = _events(o["order_id"], "amended")
    assert event["payload"] == {"changes": {"supply_source": {"before": None,
                                                              "after": "ward_stock"}}}
    [audit] = _audits("med_order_amended", o["order_id"])
    meta = json.loads(audit["metadata_json"])
    assert audit["event_category"] == "clinical_access"
    assert meta["amended_fields"] == ["supply_source"]
    # The same choice again writes nothing.
    again = _source_route(hdr, o["order_id"], "ward_stock")
    assert again.status_code == 200 and again.json()["replayed"] is True
    assert len(_events(o["order_id"], "amended")) == 1
    # Pharmacy is not asked for a medicine given from ward stock.
    refused = _request_route(hdr, o["order_id"])
    assert refused.status_code == 409
    assert refused.json()["detail"]["error"] == "supply_source_conflict"
    assert _supplies(o["order_id"]) == []
    # Back to pharmacy, then the request goes through.
    assert _source_route(hdr, o["order_id"], "pharmacy").status_code == 200
    assert _request_route(hdr, o["order_id"]).status_code == 200


def test_only_the_prescriber_picks_the_supply_source(people, lifecycle):
    o = _order(people["ids"]["doctor"])
    for who in ("pharmacist", "nurse", "admin"):
        resp = _source_route(people["hdr"][who], o["order_id"], "patient_own")
        assert resp.status_code == 403, (who, resp.text)
    resp = _source_route(people["hdr"]["other_doctor"], o["order_id"], "patient_own")
    assert resp.status_code == 404
    assert _audits("patient_scope_denied", o["order_id"])
    assert _order_row(o["order_id"])["supply_source"] is None
    assert _source_route(people["hdr"]["doctor"], o["order_id"], "elsewhere").status_code == 422


def test_moving_away_from_pharmacy_waits_for_the_open_supply_to_close(people, lifecycle):
    o = _order(people["ids"]["doctor"], status="verified", verified_by=people["ids"]["pharmacist"])
    hdr = people["hdr"]["doctor"]
    hid = _request_route(hdr, o["order_id"]).json()["supply"]["id"]
    resp = _source_route(hdr, o["order_id"], "patient_own")
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["error"] == "supply_open"
    assert _order_row(o["order_id"])["supply_source"] == "pharmacy"
    _walk(people["hdr"]["pharmacist"], hid, "reviewing", "rejected")
    assert _source_route(hdr, o["order_id"], "patient_own").status_code == 200
    assert _order_row(o["order_id"])["supply_source"] == "patient_own"


def test_the_supply_source_of_an_ended_or_changed_order_is_not_changed(people, lifecycle):
    doctor, hdr = people["ids"]["doctor"], people["hdr"]["doctor"]
    stopped = _order(doctor)
    _move(stopped["order_id"], "stopped", actor=doctor)
    resp = _source_route(hdr, stopped["order_id"], "ward_stock")
    assert resp.status_code == 409 and resp.json()["detail"]["error"] == "order_not_live"
    live = _order(doctor)
    stale = _order_row(live["order_id"])["version"] - 1
    resp = _source_route(hdr, live["order_id"], "ward_stock", expected_version=stale)
    assert resp.status_code == 409 and resp.json()["detail"]["error"] == "stale_version"
    assert _order_row(live["order_id"])["supply_source"] is None


# ── 2. Record mode, and both switches off ─────────────────────────────────────

def test_record_mode_helix_steps_write_supply_events_and_leave_the_order_alone(people, record_mode):
    o = _order(people["ids"]["doctor"], source="sage_handoff", ward_source="llm_extracted")
    hid = _request(o["order_id"], people["ids"]["doctor"])["id"]
    before = _order_row(o["order_id"])
    _walk(people["hdr"]["pharmacist"], hid, "reviewing", "verified", "scheduled", "dispensed")
    resp = client.post(f"/api/helix/orders/{hid}/receive", json={},
                       headers=people["hdr"]["nurse"])
    assert resp.status_code == 200, resp.text
    after = _order_row(o["order_id"])
    assert (after["status"], after["version"]) == ("prescribed", before["version"])
    kinds = [e["kind"] for e in _events(o["order_id"])]
    assert kinds == ["created", "supply_requested", "supply_reviewing", "supply_verified",
                     "dispensed", "received"]
    # Record mode keeps today's semantics: the chart twin still waits for the
    # Verify Queue.
    assert _ward(o["med_id"])["verified_by"] is None
    for action in ("med_order_supply_reviewing", "med_order_supply_verified",
                   "med_order_dispensed", "med_order_received"):
        [audit] = _audits(action, o["order_id"])
        assert audit["event_category"] == "clinical_access"
        assert json.loads(audit["metadata_json"])["helix_order_id"] == hid


def test_with_both_switches_off_a_linked_row_still_records_its_supply_events(people):
    assert not config.feature_enabled("med_order_store")
    o = _order(people["ids"]["doctor"])
    hid = _request(o["order_id"], people["ids"]["doctor"])["id"]
    _walk(people["hdr"]["pharmacist"], hid, "reviewing", "verified")
    assert _order_row(o["order_id"])["status"] == "prescribed"
    assert [e["kind"] for e in _events(o["order_id"])][-2:] == ["supply_reviewing",
                                                               "supply_verified"]


# ── 3. Lifecycle: one verification, two surfaces ──────────────────────────────

def test_a_sage_order_verified_in_helix_is_verified_everywhere_with_one_supply(people, lifecycle):
    pharmacist = people["ids"]["pharmacist"]
    o = _order(people["ids"]["doctor"], source="sage_handoff", ward_source="llm_extracted")
    assert _request_route(people["hdr"]["doctor"], o["order_id"]).status_code == 200
    [row] = _supplies(o["order_id"])
    resp = _walk(people["hdr"]["pharmacist"], row["id"], "reviewing", "verified")
    assert resp.json()["linked_order"]["status"] == "verified"
    order = _order_row(o["order_id"])
    assert (order["status"], order["verified_by"]) == ("verified", pharmacist)
    [verified] = _events(o["order_id"], "verified")
    assert (verified["ref_type"], verified["ref_id"]) == ("helix_order", row["id"])
    assert verified["entry_point"] == "helix.verified"
    # The ward row carries the verifier (I1), so the Verify Queue stops listing it.
    assert _ward(o["med_id"])["verified_by"] == pharmacist
    assert o["med_id"] not in {m["id"] for m in pharmacy.list_pending_verification()}
    assert len(_supplies(o["order_id"])) == 1
    listed = next(r for r in helix_service.get_all_orders() if r["id"] == row["id"])
    assert listed["linked_order"]["status"] == "verified"
    assert listed["linked_order"]["verified_by_name"] == f"Pharmacist {pharmacist}"
    assert any("Pharmacy has verified" in m for m in _messages(o["patient_id"]))


def test_a_sage_order_verified_on_the_verify_queue_makes_helix_a_supply_step(people, lifecycle):
    pharmacist = people["ids"]["pharmacist"]
    o = _order(people["ids"]["doctor"], source="sage_handoff", ward_source="llm_extracted")
    hid = _request_route(people["hdr"]["doctor"], o["order_id"]).json()["supply"]["id"]
    pharmacy.verify_medication(o["med_id"], pharmacist)
    checked = _order_row(o["order_id"])
    assert checked["status"] == "verified"
    assert helix_service.get_order(hid)["linked_order"]["status"] == "verified"
    _walk(people["hdr"]["pharmacist"], hid, "reviewing", "verified")
    after = _order_row(o["order_id"])
    assert (after["status"], after["version"]) == ("verified", checked["version"])
    assert len(_events(o["order_id"], "verified")) == 1
    assert len(_events(o["order_id"], "supply_verified")) == 1
    assert len(_supplies(o["order_id"])) == 1


def test_a_helix_rejection_of_a_waiting_order_rejects_the_order(people, lifecycle):
    o = _order(people["ids"]["doctor"], source="sage_handoff", ward_source="llm_extracted")
    hid = _request_route(people["hdr"]["doctor"], o["order_id"]).json()["supply"]["id"]
    _walk(people["hdr"]["pharmacist"], hid, "reviewing", "rejected")
    order = _order_row(o["order_id"])
    assert (order["status"], order["status_reason"]) == ("rejected", "Not suitable")
    assert _ward(o["med_id"])["status"] == "discontinued"
    [event] = _events(o["order_id"], "rejected")
    assert (event["ref_type"], event["ref_id"]) == ("helix_order", hid)
    assert _request_route(people["hdr"]["doctor"], o["order_id"]).json()["detail"]["error"] == (
        "order_not_live")


def test_a_substituted_supply_never_stands_in_for_the_order_check(people, lifecycle):
    o = _order(people["ids"]["doctor"], source="sage_handoff", ward_source="llm_extracted")
    hid = _request_route(people["hdr"]["doctor"], o["order_id"]).json()["supply"]["id"]
    hdr = people["hdr"]["pharmacist"]
    _walk(hdr, hid, "reviewing")
    resp = _step(hdr, hid, "substitute_suggested", substitute_medication_id=SUB_ID,
                 pharmacist_notes="Brand out of stock")
    assert resp.status_code == 200, resp.text
    _walk(hdr, hid, "reviewing", "verified")
    assert _order_row(o["order_id"])["status"] == "prescribed"
    reviewing = _events(o["order_id"], "supply_reviewing")
    assert any((e["payload"] or {}).get("substitute_medication_id") == SUB_ID for e in reviewing)
    assert len(_events(o["order_id"], "supply_verified")) == 1
    assert _ward(o["med_id"])["verified_by"] is None


def test_with_the_lifecycle_on_only_a_pharmacist_decides_the_order(people, lifecycle, monkeypatch):
    o = _order(people["ids"]["doctor"])
    hid = _request_route(people["hdr"]["doctor"], o["order_id"]).json()["supply"]["id"]
    admin = people["hdr"]["admin"]
    _walk(admin, hid, "reviewing")
    resp = _step(admin, hid, "verified")
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"]["error"] == "pharmacist_required"
    assert _helix(hid)["status"] == "reviewing"
    assert _order_row(o["order_id"])["status"] == "prescribed"
    assert _audits("med_order_step_refused", o["order_id"])
    # In record mode the same step is a supply step and the order is untouched.
    monkeypatch.setattr(config, "MED_ORDER_LIFECYCLE_ENABLED", False)
    assert _step(admin, hid, "verified").status_code == 200
    assert _order_row(o["order_id"])["status"] == "prescribed"


# ── 4. Races and replays ──────────────────────────────────────────────────────

def test_a_version_race_in_the_hook_rolls_back_the_helix_step_and_its_message(
        people, lifecycle, monkeypatch):
    doctor = people["ids"]["doctor"]
    o = _order(doctor, source="sage_handoff", ward_source="llm_extracted")
    hid = _request_route(people["hdr"]["doctor"], o["order_id"]).json()["supply"]["id"]
    _walk(people["hdr"]["pharmacist"], hid, "reviewing")
    messages_before = _messages(o["patient_id"])
    real_gate = helix_routes.enforce_medication_safety

    def racing_gate(*args, **kwargs):
        out = real_gate(*args, **kwargs)
        # Another session holds the order while the pharmacist's screen runs.
        _move(o["order_id"], "held", actor=doctor, reason="Nil by mouth")
        return out

    monkeypatch.setattr(helix_routes, "enforce_medication_safety", racing_gate)
    resp = _step(people["hdr"]["pharmacist"], hid, "verified")
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["error"] == "stale_version"
    assert _helix(hid)["status"] == "reviewing"
    with db_session() as db:
        assert db.execute("SELECT COUNT(*) FROM helix_transitions WHERE order_id = ? "
                          "AND to_status = 'verified'", (hid,)).fetchone()[0] == 0
    assert _messages(o["patient_id"]) == messages_before
    order = _order_row(o["order_id"])
    assert (order["status"], order["verified_by"]) == ("held", None)


def test_a_replayed_key_is_answered_as_before(people, lifecycle):
    doctor = people["ids"]["doctor"]
    o = _order(doctor)
    hid = _request_route(people["hdr"]["doctor"], o["order_id"]).json()["supply"]["id"]
    hdr = people["hdr"]["pharmacist"]
    _walk(hdr, hid, "reviewing")
    assert _step(hdr, hid, "verified", idempotency_key="w7e-replay-1").status_code == 200
    _move(o["order_id"], "stopped", actor=doctor)
    resp = _step(hdr, hid, "verified", idempotency_key="w7e-replay-1")
    assert resp.status_code == 200, resp.text
    assert resp.json()["already_applied"] is True


# ── 5. The linked screen ──────────────────────────────────────────────────────

def test_the_dose_guard_stops_a_linked_helix_verification(people, record_mode):
    o = _order(people["ids"]["doctor"], drug="Paracetamol", formulary_id=PARA_ID, dose="8 g",
               route="PO", freq="QDS")
    hid = _request(o["order_id"], people["ids"]["doctor"])["id"]
    hdr = people["hdr"]["pharmacist"]
    _walk(hdr, hid, "reviewing")
    resp = _step(hdr, hid, "verified")
    assert resp.status_code == 428, resp.text
    check = resp.json()["detail"]["safety"]["dose_check"]
    assert check["decision"] == "block"
    assert check["override_requires_permission"] == "medication.prescribe"
    resp = _step(hdr, hid, "verified", safety_ack={"acknowledged": True, "reason": GOOD_REASON})
    assert resp.status_code == 428
    assert "Only a prescriber" in resp.json()["detail"]["safety"]["ack_refused"]
    assert _helix(hid)["status"] == "reviewing"
    # An unlinked supply row is screened exactly as before (no dose guard here).
    pid = _patient()
    with db_session() as db:
        plain = helix_service.insert_order(db, pid, PARA_ID, "8 g", "PO", "QDS",
                                           people["ids"]["doctor"])
    _walk(hdr, plain["id"], "reviewing", "verified")


def test_a_linked_drug_is_not_screened_against_its_own_chart_row(people, record_mode):
    o = _order(people["ids"]["doctor"], drug="Enoxaparin", formulary_id=ENOX_ID, dose="40 mg",
               route="SC", freq="OD")
    hid = _request(o["order_id"], people["ids"]["doctor"])["id"]
    hdr = people["hdr"]["pharmacist"]
    resp = _walk(hdr, hid, "reviewing", "verified")
    assert resp.json()["safety"]["max_severity"] != "severe"
    # Unlinked, the same drug for a patient with it on the chart still meets
    # itself (defect F-B, unchanged for unlinked rows).
    with db_session() as db:
        plain = helix_service.insert_order(db, o["patient_id"], ENOX_ID, "40 mg", "SC", "OD",
                                           people["ids"]["doctor"])
    _walk(hdr, plain["id"], "reviewing")
    assert _step(hdr, plain["id"], "verified").status_code == 428


def test_a_substitute_on_a_linked_row_keeps_the_screen_against_the_chart_row(people, record_mode):
    """Substitution is unchanged while held ruling R002 is open: a substitute
    is screened against every drug on the chart, the order's own row
    included, exactly as on an unlinked row (the dose guard is added)."""
    o = _order(people["ids"]["doctor"], drug="Enoxaparin", formulary_id=ENOX_ID, dose="40 mg",
               route="SC", freq="OD")
    hid = _request(o["order_id"], people["ids"]["doctor"])["id"]
    hdr = people["hdr"]["pharmacist"]
    _walk(hdr, hid, "reviewing")
    resp = _step(hdr, hid, "substitute_suggested", substitute_medication_id=ENOX_SUB_ID,
                 pharmacist_notes="Brand out of stock")
    assert resp.status_code == 428, resp.text
    assert _helix(hid)["status"] == "reviewing"
    assert _events(o["order_id"], "supply_reviewing")[-1]["payload"].get(
        "substitute_medication_id") is None


def test_a_placeholder_dose_is_never_verified_on_a_linked_row(people, record_mode):
    o = _order(people["ids"]["doctor"], source="sage_handoff", ward_source="llm_extracted")
    with db_session() as db:
        row = helix_service.insert_order(db, o["patient_id"], DRUG_ID, "REVIEW REQUIRED", "Oral",
                                         "TDS", people["ids"]["doctor"])
        db.execute("UPDATE helix_patient_med_orders SET med_order_id = ? WHERE id = ?",
                   (o["order_id"], row["id"]))
    hdr = people["hdr"]["pharmacist"]
    _walk(hdr, row["id"], "reviewing")
    resp = _step(hdr, row["id"], "verified")
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"]["error"] == "placeholder_dose"
    assert _helix(row["id"])["status"] == "reviewing"


def test_a_supply_the_order_has_moved_on_from_is_never_verified(people, record_mode):
    o = _order(people["ids"]["doctor"])
    hid = _request(o["order_id"], people["ids"]["doctor"])["id"]
    with db_session() as db:
        db.execute("UPDATE med_orders SET dose = '1000 mg', version = version + 1 WHERE id = ?",
                   (o["order_id"],))
    hdr = people["hdr"]["pharmacist"]
    _walk(hdr, hid, "reviewing")
    resp = _step(hdr, hid, "verified")
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["error"] == "supply_out_of_date"


def test_nothing_is_committed_to_an_ended_order(people, record_mode):
    doctor = people["ids"]["doctor"]
    o = _order(doctor)
    hid = _request(o["order_id"], doctor)["id"]
    hdr = people["hdr"]["pharmacist"]
    _walk(hdr, hid, "reviewing")
    _move(o["order_id"], "stopped", actor=doctor)
    resp = _step(hdr, hid, "verified")
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["error"] == "order_not_live" and "was stopped" in detail["message"]
    _walk(hdr, hid, "rejected")
    assert _order_row(o["order_id"])["status"] == "stopped"
    assert len(_events(o["order_id"], "supply_rejected")) == 1


# ── 6. The dose's custody link ────────────────────────────────────────────────

def _give(med_id, nurse, status="given"):
    return emar_service.record_administration(
        med_id, nurse, status=status,
        not_given_reason="Patient asleep" if status == "not_given" else None)


def test_two_received_units_leave_the_dose_unlinked(people, record_mode):
    ids = people["ids"]
    o = _order(ids["doctor"])
    for _ in range(2):
        _deliver(_request(o["order_id"], ids["doctor"])["id"], ids["pharmacist"], ids["nurse"])
    record = _give(o["med_id"], ids["nurse"])
    [event] = _events(o["order_id"], "administered")
    assert event["payload"]["supply"] == {"linked": False, "helix_order_id": None,
                                          "note": "supply not linked", "received_unused": 2}
    [audit] = _audits("medication_administered", o["med_id"])
    meta = json.loads(audit["metadata_json"])
    assert meta["supply_helix_order_id"] is None and meta["administration_id"] == record["id"]


def test_one_received_unit_links_the_dose_and_a_second_dose_does_not_reuse_it(people, record_mode):
    ids = people["ids"]
    o = _order(ids["doctor"])
    hid = _request(o["order_id"], ids["doctor"])["id"]
    _deliver(hid, ids["pharmacist"], ids["nurse"])
    _give(o["med_id"], ids["nurse"])
    _give(o["med_id"], ids["nurse"])
    first, second = _events(o["order_id"], "administered")
    assert first["payload"]["supply"] == {"linked": True, "helix_order_id": hid}
    assert second["payload"]["supply"]["linked"] is False
    assert second["payload"]["supply"]["received_unused"] == 0


def test_an_unlinked_row_and_a_dose_not_given_carry_no_custody(people, record_mode):
    ids = people["ids"]
    pid = _patient()
    with db_session() as db:
        med_id = db.execute(
            "INSERT INTO medications (patient_id, drug_name, dosage, route, frequency, status, "
            "prescribed_by, source) VALUES (?, 'Zylomed', '500 mg', 'Oral', 'TDS', 'active', ?, "
            "'clinician')", (pid, ids["doctor"])).lastrowid
    _give(med_id, ids["nurse"])
    [audit] = _audits("medication_administered", med_id)
    assert "supply_helix_order_id" not in json.loads(audit["metadata_json"])
    o = _order(ids["doctor"])
    _deliver(_request(o["order_id"], ids["doctor"])["id"], ids["pharmacist"], ids["nurse"])
    _give(o["med_id"], ids["nurse"], status="not_given")
    [event] = _events(o["order_id"], "not_given")
    assert "supply" not in event["payload"]


# ── 7. Unlinked rows and the hook itself ──────────────────────────────────────

def test_unlinked_rows_read_and_move_exactly_as_before(people, lifecycle):
    pid = _patient()
    with db_session() as db:
        plain = helix_service.insert_order(db, pid, DRUG_ID, "500 mg", "Oral", "TDS",
                                           people["ids"]["doctor"])
    o = _order(people["ids"]["doctor"])
    linked = _request(o["order_id"], people["ids"]["doctor"])
    listed = {r["id"]: r for r in helix_service.get_all_orders()}
    assert "linked_order" not in listed[plain["id"]]
    assert listed[linked["id"]]["linked_order"]["id"] == o["order_id"]
    assert "linked_order" not in helix_service.get_order(plain["id"])
    pending = {r["id"]: r for r in helix_service.get_pending_orders()}
    assert "linked_order" not in pending[plain["id"]]
    resp = _walk(people["hdr"]["pharmacist"], plain["id"], "reviewing", "verified")
    assert "linked_order" not in resp.json()


def test_the_hook_does_nothing_for_an_unlinked_row():
    hook = supply.helix_step_hook(actor_id="x", actor_role="pharmacist")
    with db_session(immediate=True) as conn:
        assert hook(conn, {"id": "H1", "med_order_id": None}, "reviewing", "verified", "T1") is None


def test_the_plan_names_what_each_step_does():
    live = {"status": "prescribed", "held_from": None, "dose": "500 mg", "route": "Oral",
            "frequency": "TDS"}
    row = {"id": "H1", "medication_id": DRUG_ID, "substitute_medication_id": None,
           "dosage": "500 mg", "route": "Oral", "frequency": "TDS"}
    plan = supply.plan_helix_step
    assert plan(row, live, "verified", lifecycle=True, may_verify_order=True) == "decide"
    assert plan(row, live, "verified", lifecycle=False, may_verify_order=True) == "event"
    assert plan(row, live, "scheduled", lifecycle=True, may_verify_order=True) == "none"
    assert plan(row, {**live, "status": "verified"}, "verified", lifecycle=True,
                may_verify_order=False) == "event"
    held_live = {**live, "status": "held", "held_from": "prescribed"}
    assert plan(row, held_live, "rejected", lifecycle=True, may_verify_order=True) == "decide"
    with pytest.raises(core.NotPermitted):
        plan(row, live, "rejected", lifecycle=True, may_verify_order=False)
    with pytest.raises(core.MedOrderConflict) as broken:
        plan(row, None, "reviewing", lifecycle=False, may_verify_order=False)
    assert broken.value.code == "order_link_broken"
    for ended in ("stopped", "completed", "cancelled", "rejected", "draft"):
        with pytest.raises(core.TransitionNotAllowed):
            plan(row, {**live, "status": ended}, "dispensed", lifecycle=False,
                 may_verify_order=True)
        assert plan(row, {**live, "status": ended}, "reviewing", lifecycle=False,
                    may_verify_order=True) == "event"
