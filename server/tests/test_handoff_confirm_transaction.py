"""Confirming a Sage card through the transition endpoint is ONE transaction.

The handoff lane used to commit in five separate steps: the action's state
(confirmed), the handoff record (pending), the chart row, the Helix order, and
the handoff's final status. A failure after the first step left a card that
read "confirmed" with nothing dispatched and no record saying so; a failure
after the Helix order left a live pending order in the pharmacist's queue
behind a handoff stuck at 'pending' that names no order. The same class as the
agentic confirm defect fixed on 2026-10-03 (tests/test_agentic_confirm_race.py).

Now the claim, the handoff record, everything the adapter writes, and the
final status stand or fall together. A failed dispatch (blocked, failed,
unsupported) is still a committed OUTCOME on a confirmed card, as before; what
can no longer happen is half of one.

Faults are injected with SQLite triggers scoped to one test patient, so they
hit the same write whichever way the code is arranged.
"""
import uuid
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

from database import db_session
from main import app

client = TestClient(app, raise_server_exceptions=False)
DOCTOR = "D-HOTXN"
DEPT = "Internal Medicine"
DRUG = "Txnmycin 250mg Tablet"


def _headers():
    r = client.post("/api/auth/login", json={"employee_id": DOCTOR, "password": "testpass123"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


@pytest.fixture
def ward():
    """An admitted patient, a case room the test doctor can reach, and a
    formulary drug with stock, so a prescribe card produces a real order."""
    from auth import hash_password
    from services.helix_db import init_helix_db
    init_helix_db()
    pid = f"PAT-HT-{uuid.uuid4().hex[:8]}"
    room = f"case-{pid}"
    med_id = f"MED-TXN-{uuid.uuid4().hex[:6]}"
    with db_session() as db:
        db.execute(
            "INSERT OR REPLACE INTO users (id, name, role, password_hash, department, is_active) "
            "VALUES (?, 'Dr Handoff Txn', 'doctor', ?, ?, 1)",
            (DOCTOR, hash_password("testpass123"), DEPT),
        )
        db.execute(
            "INSERT INTO patients (id, name, mrn, diagnosis, status, ward, bed, attending_doctor_id) "
            "VALUES (?, 'Handoff Txn', ?, 'Pneumonia', 'admitted', 'W1', '1', ?)",
            (pid, f"MRN-{pid}", DOCTOR),
        )
        db.execute("INSERT INTO channels (id, name, type, patient_id) VALUES (?, ?, 'case_room', ?)",
                   (room, f"{pid} room", pid))
        db.execute("INSERT INTO admissions (id, patient_id, admitted_by, department, status) "
                   "VALUES (?, ?, ?, ?, 'active')", (f"ADM-{pid}", pid, DOCTOR, DEPT))
        db.execute("INSERT INTO helix_medication_master (id, name, generic_name, form, strength) "
                   "VALUES (?, ?, 'Txnmycin', 'Tablet', '250mg')", (med_id, DRUG))
        db.execute("INSERT INTO helix_inventory_lots (id, medication_id, lot_number, quantity, expiration_date) "
                   "VALUES (?, ?, 'LOTTXN', 20, '2029-12-31')", (f"LOT-{med_id}", med_id))
    yield {"pid": pid, "room": room, "med_id": med_id}
    with db_session() as db:
        db.execute("DELETE FROM sage_handoff_events WHERE handoff_id IN "
                   "(SELECT id FROM sage_handoffs WHERE patient_id = ?)", (pid,))
        db.execute("DELETE FROM sage_handoffs WHERE patient_id = ?", (pid,))
        db.execute("DELETE FROM sage_action_events WHERE action_id IN "
                   "(SELECT id FROM sage_actions WHERE patient_id = ?)", (pid,))
        db.execute("DELETE FROM sage_actions WHERE patient_id = ?", (pid,))
        db.execute("DELETE FROM helix_patient_med_orders WHERE patient_id = ?", (pid,))
        db.execute("DELETE FROM helix_inventory_lots WHERE medication_id = ?", (med_id,))
        db.execute("DELETE FROM helix_medication_master WHERE id = ?", (med_id,))
        db.execute("DELETE FROM medications WHERE patient_id = ?", (pid,))
        db.execute("DELETE FROM surgery_team_assignments WHERE surgery_id IN "
                   "(SELECT id FROM surgeries WHERE patient_id = ?)", (pid,))
        db.execute("DELETE FROM surgeries WHERE patient_id = ?", (pid,))
        db.execute("DELETE FROM messages WHERE channel_id = ?", (room,))
        db.execute("DELETE FROM channels WHERE id = ?", (room,))
        db.execute("DELETE FROM admissions WHERE patient_id = ?", (pid,))
        db.execute("UPDATE patients SET status = 'discharged' WHERE id = ?", (pid,))


def _card(ward, target, action_type):
    aid = str(uuid.uuid4())
    with db_session() as db:
        db.execute(
            """INSERT INTO sage_actions (id, room_id, patient_id, action_type, target, rationale,
                                         confidence, state, version)
               VALUES (?, ?, ?, ?, ?, 'start oral antibiotics', 0.9, 'suggested', 1)""",
            (aid, ward["room"], ward["pid"], action_type, target),
        )
    return aid


def _confirm(ward, aid):
    return client.post(f"/api/relay/channels/{ward['room']}/sage_actions/{aid}/transition",
                       json={"state": "confirmed", "version": 1}, headers=_headers())


def _count(sql, *params):
    with db_session() as db:
        return db.execute(sql, params).fetchone()[0]


def _action(aid):
    with db_session() as db:
        return dict(db.execute("SELECT state, version FROM sage_actions WHERE id = ?", (aid,)).fetchone())


@contextmanager
def _fault(name, table, when):
    """A BEFORE INSERT trigger on `table` that aborts the insert when `when` holds."""
    with db_session() as db:
        db.execute(f"CREATE TRIGGER {name} BEFORE INSERT ON {table} WHEN {when} "
                   "BEGIN SELECT RAISE(ABORT, 'injected fault'); END")
    try:
        yield
    finally:
        with db_session() as db:
            db.execute(f"DROP TRIGGER IF EXISTS {name}")


def _final_status_fault(pid):
    """The handoff's final status write fails: everything before it has run."""
    return _fault("test_fault_final_handoff_status", "sage_handoff_events",
                  f"NEW.new_status <> 'pending' AND NEW.handoff_id IN "
                  f"(SELECT id FROM sage_handoffs WHERE patient_id = '{pid}')")


def test_prescribe_confirm_commits_everything_together(ward):
    aid = _card(ward, DRUG, "prescribe")

    r = _confirm(ward, aid)

    assert r.status_code == 200, r.text
    handoff = r.json()["handoff"]
    assert handoff["status"] == "accepted"
    assert _action(aid)["state"] == "confirmed"
    order = _count("SELECT COUNT(*) FROM helix_patient_med_orders WHERE id = ? AND medication_id = ?",
                   handoff["downstream_ref_id"], ward["med_id"])
    assert order == 1
    assert _count("SELECT COUNT(*) FROM medications WHERE patient_id = ? AND drug_name = ?",
                  ward["pid"], DRUG) == 1


def test_a_failed_final_status_leaves_no_order_and_no_confirm(ward):
    """The defect: the order and the chart row were committed before the
    handoff's final status. Now a failure there takes them back with it."""
    aid = _card(ward, DRUG, "prescribe")

    with _final_status_fault(ward["pid"]):
        r = _confirm(ward, aid)

    assert r.status_code == 500, r.text
    assert _count("SELECT COUNT(*) FROM helix_patient_med_orders WHERE patient_id = ?", ward["pid"]) == 0, (
        "a pending Helix order survived a confirm whose handoff record failed"
    )
    assert _count("SELECT COUNT(*) FROM medications WHERE patient_id = ? AND drug_name = ?",
                  ward["pid"], DRUG) == 0
    assert _count("SELECT COUNT(*) FROM sage_handoffs WHERE source_action_id = ?", aid) == 0
    # The clinician's confirm did not happen, so the card can be confirmed again.
    assert _action(aid) == {"state": "suggested", "version": 1}


def test_a_failed_handoff_record_does_not_leave_a_confirmed_card(ward):
    """A card must never read 'confirmed' with nothing dispatched and no record."""
    aid = _card(ward, DRUG, "prescribe")

    with _fault("test_fault_handoff_record", "sage_handoffs", f"NEW.patient_id = '{ward['pid']}'"):
        r = _confirm(ward, aid)

    assert r.status_code == 500, r.text
    assert _action(aid) == {"state": "suggested", "version": 1}
    assert _count("SELECT COUNT(*) FROM sage_action_events WHERE action_id = ?", aid) == 0
    assert _count("SELECT COUNT(*) FROM helix_patient_med_orders WHERE patient_id = ?", ward["pid"]) == 0


def test_a_failed_final_status_leaves_no_transfer(ward):
    """The transfer adapter moves the admission, posts to the room and books an
    emergency case: none of it may outlive a handoff that failed to record."""
    aid = _card(ward, "General Surgery", "transfer_to_surgery")
    surgeries_before = _count("SELECT COUNT(*) FROM surgeries WHERE patient_id = ?", ward["pid"])

    with _final_status_fault(ward["pid"]):
        r = _confirm(ward, aid)

    assert r.status_code == 500, r.text
    assert _count("SELECT COUNT(*) FROM surgeries WHERE patient_id = ?", ward["pid"]) == surgeries_before
    assert _count("SELECT COUNT(*) FROM admissions WHERE patient_id = ? AND department = ?",
                  ward["pid"], DEPT) == 1
    assert _count("SELECT COUNT(*) FROM messages WHERE channel_id = ? AND content LIKE ?",
                  ward["room"], "SYSTEM: Patient transfer%") == 0
    assert _action(aid) == {"state": "suggested", "version": 1}


def test_transfer_confirm_still_schedules_the_prep_package(ward, monkeypatch):
    """Off the loop, the adapter cannot reach the event loop to schedule the
    surgical prep package (the thread-affinity note in sage_handoff_service).
    It must still be scheduled, once, after the commit."""
    import services.sage_handoff_service as handoff

    scheduled = []

    async def _noop():
        return None

    def record(surgery_id):
        scheduled.append(surgery_id)
        return _noop()

    monkeypatch.setattr(handoff, "generate_transfer_surgery_package_async", record)
    aid = _card(ward, "General Surgery", "transfer_to_surgery")

    r = _confirm(ward, aid)

    assert r.status_code == 200, r.text
    handoff_row = r.json()["handoff"]
    assert handoff_row["status"] == "accepted"
    assert scheduled == [handoff_row["downstream_ref_id"]]


def test_a_step_that_loses_the_whole_transaction_is_not_swallowed():
    """SQLite rolls the WHOLE transaction back on some errors (FULL, IOERR,
    BUSY, NOMEM). An adapter step that hit one must not be recorded as a
    'failed' outcome: the next statement would open a new transaction and the
    handoff would commit without the confirm it belongs to."""
    import sqlite3

    from services.sage_handoff_service import TransactionLost, _savepoint

    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE t (x INTEGER)")
    conn.execute("INSERT INTO t VALUES (1)")  # opens the transaction
    assert conn.in_transaction

    with pytest.raises(TransactionLost):
        with _savepoint(conn, "step"):
            conn.execute("ROLLBACK")  # what SQLite does on its own for those errors
            raise sqlite3.OperationalError("disk I/O error")

    # An ordinary failure still rolls back only its own step.
    conn.execute("INSERT INTO t VALUES (2)")
    with pytest.raises(ValueError):
        with _savepoint(conn, "step"):
            conn.execute("INSERT INTO t VALUES (3)")
            raise ValueError("adapter refused")
    assert conn.in_transaction
    assert [r[0] for r in conn.execute("SELECT x FROM t ORDER BY x")] == [2]
