"""The two AI lanes through the medication-order store (medorders-8, W8-E;
services/medorders/ai_lanes.py).

Pinned:
  * store off: the Sage handoff and Scribe extraction write exactly what they
    always wrote (a chart row, and for Sage an unlinked supply row), and no
    medication order;
  * store on, Sage handoff: the chart row, one order (prescribed, needs a
    pharmacist, prescriber = the confirming clinician, linked both ways) and
    one supply row linked to it, with 'created' and 'supply_requested' events
    and their clinical_access audit rows, in ONE transaction; the product is
    the one create_order's name match would have chosen; no match is still
    'charted-only' with the same words; the order carries no formulary_id
    (a name match is never an exact binding);
  * a fault after the chart row leaves neither row (with the store off the
    old best-effort lane still leaves the chart row: shown, not changed);
  * store on, Scribe: one order per started drug, linked to its chart row,
    prescriber = the note's author, the [SAFETY] note kept, no supply row; a
    fault writing an order rolls the whole extraction back;
  * record mode: the Verify Queue's check and a Helix step stay separate, each
    writing its own event; lifecycle: one pharmacist decision covers both
    rows, and Helix still refuses the placeholder dose (F-A).
"""
from __future__ import annotations

import json
import uuid

import pytest
from fastapi.testclient import TestClient

import config
from auth import create_token
from database import db_session
from main import app
from services import audit_service, helix_service
from services import nervous_system_triggers as nst
from services import pharmacy_verification_service as pharmacy
from services import sage_handoff_service as handoff
from services.clinical_records_service import add_allergy
from services.event_bus import StateEvent
from services.helix_db import init_helix_db
from services.medorders import ai_lanes, core

client = TestClient(app)

DEPT = "W8E AI Lane Ward"
# Every word of a fixture product's name becomes a drug token for the session
# (tests/test_patient_specific_screen.py checks none is ordinary English).
DRUG_ID, DRUG_NAME = "HXW8EQRV", "Quorvex W8e Fixture 250mg Tablet"
TARGET = "Quorvex"
UNLISTED = "Brivolane"
_PATIENTS: list[str] = []


@pytest.fixture(scope="module", autouse=True)
def _seed_module():
    init_helix_db()
    with db_session() as db:
        db.execute("INSERT OR IGNORE INTO helix_medication_master "
                   "(id, name, generic_name, form, strength) VALUES (?, ?, 'quorvex', "
                   "'tablet', '250mg')", (DRUG_ID, DRUG_NAME))
        db.execute("INSERT OR IGNORE INTO helix_inventory_lots "
                   "(id, medication_id, lot_number, quantity, expiration_date) "
                   "VALUES (?, ?, 'L-W8E-QRV', 200, '2030-01-01')", (f"LOT-{DRUG_ID}", DRUG_ID))
        db.execute("INSERT OR IGNORE INTO users (id, name, role, password_hash) "
                   "VALUES ('system', 'Orb System', 'admin', 'x')")
    yield
    if not _PATIENTS:
        return
    marks = ", ".join("?" for _ in _PATIENTS)
    with db_session() as db:
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


@pytest.fixture()
def record_mode(monkeypatch):
    monkeypatch.setattr(config, "MED_ORDER_STORE_ENABLED", True)


@pytest.fixture()
def lifecycle(monkeypatch):
    monkeypatch.setattr(config, "MED_ORDER_STORE_ENABLED", True)
    monkeypatch.setattr(config, "MED_ORDER_LIFECYCLE_ENABLED", True)


# ── People, patients and actions ──────────────────────────────────────────────

def _user(role):
    uid = f"W8E{role[:3].upper()}{uuid.uuid4().hex[:6].upper()}"
    with db_session() as db:
        db.execute("INSERT INTO users (id, name, role, password_hash, department) "
                   "VALUES (?, ?, ?, 'x', ?)",
                   (uid, f"{role.title()} {uid}", role, DEPT if role in ("doctor", "nurse") else None))
    return uid


def _hdr(uid, role):
    return {"Authorization": f"Bearer {create_token(uid, role)}"}


@pytest.fixture(scope="module")
def people():
    return {"doctor": _user("doctor"), "pharmacist": _user("pharmacist")}


def _patient(admitted_by):
    pid = f"PW8E{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute("INSERT INTO patients (id, name, mrn, status, ward, bed) "
                   "VALUES (?, ?, ?, 'admitted', 'W8E', '7')",
                   (pid, f"AI Lane {uuid.uuid4().hex[:5]}", f"MRN-{uuid.uuid4().hex[:8]}"))
        db.execute("INSERT INTO admissions (id, patient_id, department, ward, bed, status, "
                   "admitted_by) VALUES (?, ?, ?, 'W8E', '7', 'active', ?)",
                   (f"ADM-{pid}", pid, DEPT, admitted_by))
        db.execute("INSERT INTO channels (id, name, type, patient_id) "
                   "VALUES (?, ?, 'case_room', ?)", (f"case-{pid}", f"Case {pid}", pid))
    _PATIENTS.append(pid)
    return pid


def _action(pid, target=TARGET):
    action = {
        "id": str(uuid.uuid4()), "room_id": f"case-{pid}", "patient_id": pid,
        "action_type": "prescribe", "target": target,
        "rationale": "Start it by mouth twice a day.", "confidence": 0.9,
        "state": "confirmed", "version": 1,
    }
    with db_session() as db:
        db.execute("INSERT INTO sage_actions (id, room_id, patient_id, action_type, target, "
                   "rationale, confidence, state, version) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                   (action["id"], action["room_id"], pid, action["action_type"], target,
                    action["rationale"], action["confidence"], action["state"],
                    action["version"]))
    return action


def _q(sql, *params):
    with db_session() as db:
        return [dict(r) for r in db.execute(sql, params).fetchall()]


def _chart(pid):
    return _q("SELECT * FROM medications WHERE patient_id = ? ORDER BY id", pid)


def _orders(pid):
    return _q("SELECT * FROM med_orders WHERE patient_id = ? ORDER BY created_at, id", pid)


def _supply(pid):
    return _q("SELECT * FROM helix_patient_med_orders WHERE patient_id = ? ORDER BY created_at",
              pid)


def _events(order_id):
    return _q("SELECT * FROM med_order_events WHERE med_order_id = ? ORDER BY created_at, id",
              order_id)


def _old_like(name):
    """The lookup create_order and the Sage lane made before medorders-8,
    spelled out as it was."""
    rows = _q("SELECT id FROM helix_medication_master "
              "WHERE (name LIKE ? OR generic_name LIKE ?) AND identity_status = 'active'",
              f"%{name}%", f"%{name}%")
    return rows[0]["id"] if rows else None


# ── The name match is unchanged ───────────────────────────────────────────────

def test_resolve_by_name_is_the_lookup_create_order_always_made():
    names = [r["name"] for r in _q("SELECT name FROM helix_medication_master")]
    generics = [r["generic_name"] for r in _q("SELECT generic_name FROM helix_medication_master "
                                              "WHERE generic_name IS NOT NULL")]
    probes = set(names) | set(generics) | {n.split()[0] for n in names if n and n.split()}
    probes |= {TARGET, UNLISTED, "a", "500", "mg"}
    assert len(probes) > 3
    with db_session() as db:
        for name in sorted(probes):
            found = helix_service.resolve_by_name_in_txn(db, name)
            assert (found["id"] if found else None) == _old_like(name), name


def test_the_sage_lane_binds_the_same_product_with_the_store_on_and_off(people, monkeypatch):
    doctor = people["doctor"]
    off_pid = _patient(doctor)
    handoff.create_and_dispatch_handoff(_action(off_pid), doctor)
    monkeypatch.setattr(config, "MED_ORDER_STORE_ENABLED", True)
    on_pid = _patient(doctor)
    handoff.create_and_dispatch_handoff(_action(on_pid), doctor)
    assert [s["medication_id"] for s in _supply(off_pid)] == [DRUG_ID]
    assert [s["medication_id"] for s in _supply(on_pid)] == [DRUG_ID]
    assert _old_like(TARGET) == DRUG_ID


# ── Store off: exactly as before ──────────────────────────────────────────────

def test_store_off_the_sage_lane_writes_no_order(people):
    doctor = people["doctor"]
    pid = _patient(doctor)
    result = handoff.create_and_dispatch_handoff(_action(pid), doctor)
    assert result["status"] == "accepted"
    chart, supply = _chart(pid), _supply(pid)
    assert len(chart) == 1 and chart[0]["med_order_id"] is None
    assert chart[0]["source"] == "llm_extracted" and chart[0]["dosage"] == "REVIEW REQUIRED"
    assert len(supply) == 1 and supply[0]["med_order_id"] is None
    assert result["downstream_ref_id"] == supply[0]["id"]
    assert _orders(pid) == []


def test_store_off_scribe_writes_no_order(people):
    doctor = people["doctor"]
    pid = _patient(doctor)
    nst._persist_extraction(901, pid, doctor, {"medications": []},
                            [_start("Quorvex")], [])
    assert len(_chart(pid)) == 1 and _orders(pid) == []


# ── Store on: the Sage handoff ────────────────────────────────────────────────

def test_the_sage_handoff_writes_chart_row_order_and_supply_together(people, record_mode):
    doctor = people["doctor"]
    pid = _patient(doctor)
    action = _action(pid)
    result = handoff.create_and_dispatch_handoff(action, doctor)
    assert result["status"] == "accepted" and result["target_module"] == "helix"
    assert result["error_summary"] is None

    [chart] = _chart(pid)
    [order] = _orders(pid)
    [supply] = _supply(pid)
    assert result["downstream_ref_id"] == supply["id"]
    # The chart row as the lane has always written it, now linked.
    assert chart["source"] == "llm_extracted" and chart["verified_by"] is None
    assert chart["dosage"] == "REVIEW REQUIRED" and chart["prescribed_by"] == doctor
    assert chart["med_order_id"] == order["id"]
    # The order.
    assert order["status"] == "prescribed" and order["source"] == "sage_handoff"
    assert order["needs_verification"] == 1 and order["verified_by"] is None
    assert order["prescriber_id"] == doctor and order["entered_by"] == doctor
    assert order["entered_by_role"] == "doctor" and order["prescribed_at"]
    assert order["legacy_medication_id"] == chart["id"]
    assert order["sage_action_id"] == action["id"] and order["sage_handoff_id"] == result["id"]
    assert order["admission_id"] == f"ADM-{pid}"
    assert order["supply_source"] == "pharmacy"
    # A name match is never the order's exact product.
    assert order["formulary_id"] is None
    assert order["dose"] == "REVIEW REQUIRED" and order["projection_status"] == "active"
    # The supply row, linked, at 'pending', the product of the name match.
    assert supply["med_order_id"] == order["id"] and supply["status"] == "pending"
    assert supply["medication_id"] == DRUG_ID and supply["dosage"] == "REVIEW REQUIRED"
    # The ledger and its audit rows.
    events = _events(order["id"])
    assert [e["kind"] for e in events] == ["created", "supply_requested"]
    payload = json.loads(events[1]["payload_json"])
    assert payload == {"helix_order_id": supply["id"], "product_id": DRUG_ID,
                       "binding": "name_match", "supply_source": "pharmacy"}
    assert events[1]["ref_type"] == "helix_order" and events[1]["ref_id"] == supply["id"]
    audits = _q("SELECT action_type, event_category, metadata_json FROM audit_logs "
                "WHERE target_type = 'med_order' AND target_id = ? ORDER BY id", order["id"])
    assert [(a["action_type"], a["event_category"]) for a in audits] == [
        ("med_order_created", "clinical_access"),
        ("med_order_supply_requested", "clinical_access")]
    meta = json.loads(audits[1]["metadata_json"])
    assert meta["lane"] == "sage_handoff" and meta["binding"] == "name_match"


def test_a_drug_not_in_the_catalogue_is_still_charted_only(people, record_mode):
    doctor = people["doctor"]
    pid = _patient(doctor)
    result = handoff.create_and_dispatch_handoff(_action(pid, UNLISTED), doctor)
    assert result["status"] == "accepted" and result["downstream_ref_id"] == "charted-only"
    assert result["error_summary"] == (
        f"Charted for review, but not sent to pharmacy: Medication {UNLISTED} not found in "
        "Helix master catalog. Place the order in Helix.")
    [chart] = _chart(pid)
    [order] = _orders(pid)
    assert _supply(pid) == []
    assert chart["med_order_id"] == order["id"] and order["supply_source"] is None
    assert [e["kind"] for e in _events(order["id"])] == ["created"]


def test_store_off_same_words_for_charted_only(people):
    doctor = people["doctor"]
    pid = _patient(doctor)
    result = handoff.create_and_dispatch_handoff(_action(pid, UNLISTED), doctor)
    assert result["error_summary"] == (
        f"Charted for review, but not sent to pharmacy: Medication {UNLISTED} not found in "
        "Helix master catalog. Place the order in Helix.")


def _broken_insert(*args, **kwargs):
    raise RuntimeError("supply write failed")


def test_a_fault_after_the_chart_row_leaves_neither_row(people, record_mode, monkeypatch):
    doctor = people["doctor"]
    monkeypatch.setattr(helix_service, "insert_order", _broken_insert)
    pid = _patient(doctor)
    result = handoff.create_and_dispatch_handoff(_action(pid), doctor)
    assert result["status"] == "failed" and "supply write failed" in result["error_summary"]
    assert _chart(pid) == [] and _orders(pid) == [] and _supply(pid) == []

    # The standalone lane rolls back with its own session.
    pid2 = _patient(doctor)
    with pytest.raises(RuntimeError, match="supply write failed"):
        handoff._dispatch_to_helix({"id": str(uuid.uuid4()), "patient_id": pid2,
                                    "target": TARGET, "rationale": ""}, doctor)
    assert _chart(pid2) == [] and _orders(pid2) == [] and _supply(pid2) == []


def test_with_the_store_off_the_old_lane_still_leaves_the_chart_row(people, monkeypatch):
    """Not changed by medorders-8: shown so the difference is on record."""
    doctor = people["doctor"]
    monkeypatch.setattr(handoff, "insert_order", _broken_insert)
    pid = _patient(doctor)
    result = handoff.create_and_dispatch_handoff(_action(pid), doctor)
    assert result["downstream_ref_id"] == "charted-only"
    assert len(_chart(pid)) == 1 and _supply(pid) == []


def test_an_unwritable_order_audit_writes_nothing(people, record_mode, monkeypatch):
    doctor = people["doctor"]
    real = audit_service.log_audit_event

    def failing(*args, **kwargs):
        if kwargs.get("action_type") == "med_order_created":
            raise audit_service.AuditWriteError("audit store unavailable")
        return real(*args, **kwargs)

    monkeypatch.setattr(audit_service, "log_audit_event", failing)
    monkeypatch.setattr(core, "log_audit_event", failing)
    pid = _patient(doctor)
    result = handoff.create_and_dispatch_handoff(_action(pid), doctor)
    assert result["status"] == "failed"
    assert _chart(pid) == [] and _orders(pid) == [] and _supply(pid) == []


def test_a_blocked_order_writes_no_order(people, record_mode):
    doctor = people["doctor"]
    pid = _patient(doctor)
    add_allergy(pid, "amoxicillin", reaction="anaphylaxis", severity="severe")
    result = handoff.create_and_dispatch_handoff(_action(pid, "Amoxicillin"), doctor)
    assert result["status"] == "blocked"
    assert _chart(pid) == [] and _orders(pid) == [] and _supply(pid) == []


# ── Store on: Scribe extraction ───────────────────────────────────────────────

def _start(drug, dose="REVIEW REQUIRED", notes=None, note_id=901):
    return {"drug": drug, "dosage": dose, "route": "REVIEW REQUIRED",
            "frequency": "REVIEW REQUIRED", "notes": notes, "source_note_id": note_id,
            "source_sentence": f"Start {drug}."}


def test_scribe_writes_one_order_per_start_and_no_supply(people, record_mode):
    doctor = people["doctor"]
    pid = _patient(doctor)
    nst._persist_extraction(
        902, pid, doctor, {"medications": []},
        [_start("Quorvex", dose="250mg", note_id=902),
         _start("Brivolane", notes="[SAFETY] severe alert", note_id=902)],
        ["Warfarin"])
    chart = _chart(pid)
    orders = _orders(pid)
    assert len(chart) == 2 and len(orders) == 2
    by_drug = {o["drug_name"]: o for o in orders}
    for row in chart:
        order = by_drug[row["drug_name"]]
        assert row["med_order_id"] == order["id"] and order["legacy_medication_id"] == row["id"]
        assert order["source"] == "scribe_extracted" and order["status"] == "prescribed"
        assert order["needs_verification"] == 1
        assert order["prescriber_id"] == doctor and order["entered_by"] == doctor
        assert order["scribe_note_id"] == "902" and order["supply_source"] is None
        created = _events(order["id"])
        assert [e["kind"] for e in created] == ["created"]
        assert json.loads(created[0]["payload_json"])["source_note_id"] == "902"
    assert by_drug["Quorvex"]["dose"] == "250mg"
    assert by_drug["Brivolane"]["notes"] == "[SAFETY] severe alert"
    # A stop is only ever proposed; nothing goes to pharmacy.
    assert _supply(pid) == []


def test_a_fault_writing_an_order_rolls_the_whole_extraction_back(people, record_mode,
                                                                   monkeypatch):
    doctor = people["doctor"]
    pid = _patient(doctor)
    real = core.create_in_txn
    calls = {"n": 0}

    def second_fails(conn, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise core.MedOrderInvalid("order write failed", code="test_fault")
        return real(conn, **kwargs)

    monkeypatch.setattr(core, "create_in_txn", second_fails)
    with pytest.raises(core.MedOrderInvalid):
        nst._persist_extraction(903, pid, doctor,
                                {"medications": [], "vitals": {"heart_rate": 88}},
                                [_start("Quorvex"), _start("Brivolane")], [])
    assert _chart(pid) == [] and _orders(pid) == []
    assert _q("SELECT 1 FROM vitals WHERE patient_id = ?", pid) == []
    assert _q("SELECT 1 FROM clinical_documents WHERE patient_id = ?", pid) == []


async def test_a_signed_note_with_a_severe_alert_keeps_the_safety_note(people, record_mode,
                                                                       monkeypatch):
    doctor = people["doctor"]
    pid = _patient(doctor)
    add_allergy(pid, "amoxicillin", reaction="anaphylaxis", severity="severe")
    with db_session() as db:
        note_id = db.execute(
            "INSERT INTO scribe_notes (patient_id, author_id, raw_transcript, structured_note, "
            "status, note_format) VALUES (?, ?, 'Transcript', 'PLAN:\nStart amoxicillin.', "
            "'signed', 'soap')", (pid, doctor)).lastrowid

    async def _fake_extract(note, patient, text):
        return {"medications": [{"drug_name": "Amoxicillin", "action": "start",
                                 "dosage": "500mg", "frequency": "TDS"}],
                "vitals": {}, "problems": []}

    monkeypatch.setattr(nst, "_extract_updates", _fake_extract)
    await nst.on_scribe_note_signed(StateEvent(
        topic="scribe.note.signed", source_node="scribe", event_type="note_signed",
        patient_id=pid, payload={"note_id": note_id, "signer_id": doctor}))
    [row] = _chart(pid)
    [order] = _orders(pid)
    assert row["med_order_id"] == order["id"]
    assert "[SAFETY]" in (row["notes"] or "") and order["notes"] == row["notes"]
    assert order["scribe_note_id"] == str(note_id)
    # Still the pharmacist's to check: in the queue, flagged severe.
    pending = next(p for p in pharmacy.list_pending_verification() if p["id"] == row["id"])
    assert pending["max_severity"] == "severe"


# ── One pharmacist decision or two ────────────────────────────────────────────

def _helix(order_id, actor, target, notes=""):
    return client.post(f"/api/helix/orders/{order_id}/transition",
                       json={"target_status": target, "pharmacist_notes": notes},
                       headers=_hdr(actor, "pharmacist"))


def _in_queue(med_id):
    return med_id in [p["id"] for p in pharmacy.list_pending_verification()]


def test_record_mode_the_two_checks_stay_separate(people, record_mode):
    doctor, pharmacist = people["doctor"], people["pharmacist"]
    pid = _patient(doctor)
    handoff.create_and_dispatch_handoff(_action(pid), doctor)
    [chart], [order], [supply] = _chart(pid), _orders(pid), _supply(pid)

    assert _helix(supply["id"], pharmacist, "reviewing").status_code == 200
    resp = _helix(supply["id"], pharmacist, "rejected", notes="Out of stock this week")
    assert resp.status_code == 200, resp.text
    # The supply is refused; the order still waits for its check.
    assert _orders(pid)[0]["status"] == "prescribed" and _in_queue(chart["id"])
    kinds = [e["kind"] for e in _events(order["id"])]
    assert kinds == ["created", "supply_requested", "supply_reviewing", "supply_rejected"]

    pharmacy.verify_medication(chart["id"], pharmacist, dosage="250 mg", route="Oral",
                               frequency="BD")
    [now] = _orders(pid)
    assert now["status"] == "verified" and now["verified_by"] == pharmacist
    assert not _in_queue(chart["id"])
    kinds = [e["kind"] for e in _events(order["id"])]
    assert kinds[-2:] == ["amended", "verified"]


def test_lifecycle_one_helix_decision_covers_both_rows(people, lifecycle):
    doctor, pharmacist = people["doctor"], people["pharmacist"]
    pid = _patient(doctor)
    handoff.create_and_dispatch_handoff(_action(pid), doctor)
    [chart], [order], [supply] = _chart(pid), _orders(pid), _supply(pid)
    assert _in_queue(chart["id"])

    assert _helix(supply["id"], pharmacist, "reviewing").status_code == 200
    resp = _helix(supply["id"], pharmacist, "rejected", notes="Not indicated for this patient")
    assert resp.status_code == 200, resp.text
    assert resp.json()["linked_order"]["status"] == "rejected"
    [now] = _orders(pid)
    assert now["status"] == "rejected"
    # The chart row left the Verify Queue with the same decision.
    assert _q("SELECT status FROM medications WHERE id = ?", chart["id"])[0]["status"] \
        == "discontinued"
    assert not _in_queue(chart["id"])
    assert [e["kind"] for e in _events(order["id"])][-1] == "rejected"


def test_lifecycle_verify_queue_checks_the_order_and_helix_refuses_the_placeholder(
        people, lifecycle):
    doctor, pharmacist = people["doctor"], people["pharmacist"]
    pid = _patient(doctor)
    handoff.create_and_dispatch_handoff(_action(pid), doctor)
    [chart], [order], [supply] = _chart(pid), _orders(pid), _supply(pid)

    pharmacy.verify_medication(chart["id"], pharmacist, dosage="250 mg", route="Oral",
                               frequency="BD")
    [now] = _orders(pid)
    assert now["status"] == "verified" and now["dose"] == "250 mg"
    assert not _in_queue(chart["id"])

    # The supply row still says 'REVIEW REQUIRED': Helix never verifies it (F-A).
    assert _helix(supply["id"], pharmacist, "reviewing").status_code == 200
    resp = _helix(supply["id"], pharmacist, "verified")
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"]["error"] == "placeholder_dose"
    assert _supply(pid)[0]["status"] == "reviewing"


def test_the_lane_names_are_the_gate_entry_points():
    assert ai_lanes.ENTRY_SAGE == "sage.agentic_handoff"
    assert ai_lanes.ENTRY_SCRIBE == "scribe.auto_extraction"
    assert ai_lanes.SOURCE_SAGE in core.SOURCES and ai_lanes.SOURCE_SCRIBE in core.SOURCES
    assert ai_lanes.WARD_SOURCE in core.UNVERIFIED_SOURCES


# ── The pharmacy review's timeline (medorders-5c) ─────────────────────────────

def _timeline(order_id, actor, role="pharmacist"):
    return client.get(f"/api/helix/orders/{order_id}/timeline", headers=_hdr(actor, role))


def _timeline_audits(supply_id):
    return [a for a in _q("SELECT * FROM audit_logs WHERE action_type = "
                          "'helix_order_timeline_viewed' ORDER BY id")
            if json.loads(a["metadata_json"] or "{}").get("helix_order_id") == supply_id]


def test_an_unlinked_rows_timeline_is_its_helix_steps_only(people):
    doctor, pharmacist = people["doctor"], people["pharmacist"]
    pid = _patient(doctor)
    handoff.create_and_dispatch_handoff(_action(pid), doctor)
    [supply] = _supply(pid)
    assert _helix(supply["id"], pharmacist, "reviewing").status_code == 200
    resp = _timeline(supply["id"], pharmacist)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["linked_order"] is None and body["order_events"] == []
    assert [(t["from_status"], t["to_status"]) for t in body["transitions"]] == [
        ("pending", "reviewing")]
    assert body["transitions"][0]["actor_name"] == f"Pharmacist {pharmacist}"
    assert body["transitions"][0]["created_at"].endswith("+00:00")
    # Nothing from the order store was read, so nothing new is audited.
    assert _timeline_audits(supply["id"]) == []


def test_a_linked_rows_timeline_names_the_check_and_leaves_out_the_bedside(people,
                                                                           record_mode):
    doctor, pharmacist = people["doctor"], people["pharmacist"]
    pid = _patient(doctor)
    handoff.create_and_dispatch_handoff(_action(pid), doctor)
    [chart], [order], [supply] = _chart(pid), _orders(pid), _supply(pid)
    pharmacy.verify_medication(chart["id"], pharmacist, dosage="250 mg", route="Oral",
                               frequency="BD")
    with db_session(immediate=True) as conn:
        core.record_event_in_txn(conn, order=core.get_order_in_txn(conn, order["id"]),
                                 kind="administered", actor_id=doctor, actor_role="nurse",
                                 entry_point="emar.administer")

    resp = _timeline(supply["id"], pharmacist)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    linked = body["linked_order"]
    assert linked["id"] == order["id"] and linked["status"] == "verified"
    assert linked["verified_by"] == pharmacist and linked["verified_at"]
    assert linked["verified_by_name"] == f"Pharmacist {pharmacist}"
    assert linked["needs_verification"] is True
    kinds = [e["kind"] for e in body["order_events"]]
    assert kinds == ["created", "supply_requested", "amended", "verified"]
    assert "administered" not in kinds
    assert body["order_events"][0]["actor_name"] == f"Doctor {doctor}"
    # The read of the order's ledger is on the audit chain.
    [audit] = _timeline_audits(supply["id"])
    assert audit["event_category"] == "phi_access" and audit["patient_id"] == pid
    assert audit["target_type"] == "med_order" and audit["target_id"] == order["id"]


def test_the_timeline_is_scoped_and_needs_helix_read(people):
    doctor = people["doctor"]
    pid = _patient(doctor)
    handoff.create_and_dispatch_handoff(_action(pid), doctor)
    [supply] = _supply(pid)
    with db_session() as db:
        elsewhere = f"W8EPHA{uuid.uuid4().hex[:6].upper()}"
        db.execute("INSERT INTO users (id, name, role, password_hash, department) "
                   "VALUES (?, 'Ward pharmacist', 'pharmacist', 'x', 'W8E Elsewhere Ward')",
                   (elsewhere,))
        nurse = f"W8ENUR{uuid.uuid4().hex[:6].upper()}"
        db.execute("INSERT INTO users (id, name, role, password_hash, department) "
                   "VALUES (?, 'Ward nurse', 'nurse', 'x', ?)", (nurse, DEPT))
    assert _timeline(supply["id"], elsewhere).status_code == 404
    assert _timeline(supply["id"], nurse, "nurse").status_code == 403
    assert _timeline("NO-SUCH-SUPPLY", people["pharmacist"]).status_code == 404
