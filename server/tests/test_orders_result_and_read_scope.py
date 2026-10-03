"""Lab results are written once, and order reads are ward-scoped and audited.

Two defects on the surrogate-id order routes:

  * POST /orders/{id}/result accepted an order that was ALREADY resulted and
    overwrote result_text and result_at with no order_events row. The UI hides
    '+ Result' once resulted, but a stale tab or a direct POST reached it.
  * GET /orders/{id} and /orders/{id}/events carried no ward check and no PHI
    read audit, so any ORDER_READ holder could read another ward's result by id
    and leave no trace.
"""
import uuid

import pytest
from fastapi.testclient import TestClient

from auth import create_token
from database import db_session
from main import app
from services import orders_service

client = TestClient(app)

DEPT_A = "Order Read Scope Cardiology"
DEPT_B = "Order Read Scope Oncology"


def _auth(uid, role):
    return {"Authorization": f"Bearer {create_token(uid, role)}"}


def _user(role, dept):
    uid = f"ORS{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute("INSERT INTO users (id, name, role, password_hash, department) "
                   "VALUES (?, 'Order Read Scope', ?, 'x', ?)", (uid, role, dept))
    return uid


def _patient(dept):
    pid = f"ORSP{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute("INSERT OR IGNORE INTO users (id, name, role, password_hash) "
                   "VALUES ('ORSADM', 'Order Read Scope Admitter', 'doctor', 'x')")
        db.execute("INSERT INTO patients (id, name, mrn, ward, bed, status) "
                   "VALUES (?, 'Order Read Scope', ?, ?, 'B-1', 'discharged')",
                   (pid, f"MRN-{pid}", dept))
        db.execute("INSERT INTO admissions (id, patient_id, department, ward, status, admitted_by) "
                   "VALUES (?, ?, ?, ?, 'active', 'ORSADM')", (f"ADM-{pid}", pid, dept, dept))
    return pid


def _lab_in_progress(patient_id):
    oid = f"ORS-O-{uuid.uuid4().hex[:8]}"
    with db_session() as db:
        db.execute("INSERT INTO orders (id, patient_id, order_type, description, status) "
                   "VALUES (?, ?, 'lab', 'CBC panel', 'in_progress')", (oid, patient_id))
    return oid


@pytest.fixture()
def world():
    return {
        "doctor_a": _user("doctor", DEPT_A),
        "p_a": _patient(DEPT_A),
        "p_b": _patient(DEPT_B),
    }


def _events(order_id):
    return orders_service.get_order_events(order_id)


# ── one result per order ─────────────────────────────────────────────────────

def test_a_second_different_result_is_refused_with_409(world):
    oid = _lab_in_progress(world["p_a"])
    doc = _auth(world["doctor_a"], "doctor")

    first = client.post(f"/api/orders/{oid}/result", headers=doc,
                        json={"result_text": "Hb 9.1 g/dL"})
    assert first.status_code == 200, first.text
    result_at = first.json()["order"]["result_at"]
    events_after_first = len(_events(oid))

    second = client.post(f"/api/orders/{oid}/result", headers=doc,
                         json={"result_text": "Hb 13.4 g/dL"})

    assert second.status_code == 409, second.text
    order = orders_service.get_order(oid)
    assert order["result_text"] == "Hb 9.1 g/dL", "the first result was overwritten"
    assert order["result_at"] == result_at
    assert len(_events(oid)) == events_after_first


def test_the_same_result_replayed_is_an_idempotent_success(world):
    oid = _lab_in_progress(world["p_a"])
    doc = _auth(world["doctor_a"], "doctor")
    first = client.post(f"/api/orders/{oid}/result", headers=doc, json={"result_text": "K 4.1"})
    replay = client.post(f"/api/orders/{oid}/result", headers=doc, json={"result_text": "K 4.1"})

    assert replay.status_code == 200, replay.text
    assert replay.json()["order"]["result_at"] == first.json()["order"]["result_at"]


def test_the_service_refuses_an_overwrite_directly(world):
    oid = _lab_in_progress(world["p_a"])
    orders_service.add_result(oid, "Na 138", "D001")
    with pytest.raises(orders_service.OrderAlreadyResulted):
        orders_service.add_result(oid, "Na 128", "D001")


# ── ward-scoped, audited reads ───────────────────────────────────────────────

@pytest.mark.parametrize("suffix", ["", "/events"])
def test_another_wards_order_reads_as_not_found(world, suffix):
    oid = _lab_in_progress(world["p_b"])

    resp = client.get(f"/api/orders/{oid}{suffix}", headers=_auth(world["doctor_a"], "doctor"))

    assert resp.status_code == 404, resp.text
    assert resp.json()["detail"] == "Order not found"


@pytest.mark.parametrize("suffix,action", [("", "order_viewed"), ("/events", "order_events_viewed")])
def test_an_in_scope_read_writes_exactly_one_phi_access_row(world, suffix, action):
    oid = _lab_in_progress(world["p_a"])

    def _count():
        with db_session() as db:
            return db.execute(
                "SELECT COUNT(*) c FROM audit_logs WHERE event_category = 'phi_access' "
                "AND action_type = ? AND target_id = ?", (action, oid)).fetchone()["c"]

    before = _count()
    resp = client.get(f"/api/orders/{oid}{suffix}", headers=_auth(world["doctor_a"], "doctor"))

    assert resp.status_code == 200, resp.text
    assert _count() == before + 1
