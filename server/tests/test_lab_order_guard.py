"""The laboratory owns its orders (lis-7).

Pinned here:
  * The guard. An order with lab_order_items refuses a manual move to
    in_progress, completed, resulted or cancelled, and a free-text result,
    with 409 order_managed_by_lab, with ORB_LAB on AND off: the guard is keyed
    on the items, never on the switch. Orders the laboratory does not manage
    keep today's behaviour exactly.
  * The completion rule (ruling LIS-R16, option a): all cancelled is
    cancelled and never bills; some released and the rest released or
    cancelled is resulted with the fixed text; a rejected item with no
    decision keeps the order in progress and is on the laboratory's list.
  * The laboratory-mediated cancel: cancels what is not released, withdraws
    unreleased results, keeps released ones, cancels empty specimens, is
    audited in the same transaction, scoped like every order write, and is
    served while the module drains.
  * An order moved by hand before the guard existed never blocks the
    laboratory: its step commits and an order_state_conflict is recorded.
  * The order list says which orders the laboratory manages, where each test
    is, and why an order was not taken in.
  * Only lab_order_service calls the conn-taking helpers.
"""
from __future__ import annotations

import ast
import pathlib
import uuid

import pytest
from fastapi.testclient import TestClient

import config
import main
from auth import create_token, hash_password
from database import db_session
from services import billing_service, lab_module, lab_order_service as los
from services import lab_master_service as lm
from services import orders_service, terminology_service
from services.audit_service import AuditWriteError
from services.clinical_time import utc_iso_now

client = TestClient(main.app)

DOCTOR_ID = "D001"
WARD = "General Medicine"
OTHER_WARD = "Cardiology"


# ── fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module", autouse=True)
def _catalogue():
    """The starter catalogue, and at the end every laboratory row this file
    wrote removed, so no open work makes the module 'draining' for later
    files."""
    terminology_service.seed_terminology()
    lm.load_starter_catalogue()
    created: list[str] = []
    yield created
    if created:
        marks = ", ".join("?" for _ in created)
        ids = tuple(created)
        with db_session() as db:
            db.execute(f"DELETE FROM lab_exceptions WHERE order_id IN "
                       f"(SELECT id FROM orders WHERE patient_id IN ({marks}))", ids)
            db.execute(f"DELETE FROM lab_result_events WHERE patient_id IN ({marks})", ids)
            db.execute(f"DELETE FROM lab_results WHERE patient_id IN ({marks})", ids)
            db.execute(f"DELETE FROM lab_order_items WHERE patient_id IN ({marks})", ids)
            db.execute(f"DELETE FROM lab_specimen_events WHERE patient_id IN ({marks})", ids)
            db.execute(f"DELETE FROM lab_specimens WHERE patient_id IN ({marks})", ids)
            db.execute(f"UPDATE patients SET status = 'discharged' WHERE id IN ({marks})", ids)
            db.execute(f"UPDATE admissions SET status = 'discharged' WHERE patient_id IN ({marks})",
                       ids)
    lab_module.refresh_drain_state()


@pytest.fixture
def lab_on(monkeypatch):
    monkeypatch.setattr(config, "LAB_ENABLED", True)
    with db_session() as db:
        event_id = db.execute(
            "INSERT INTO lab_module_events (event, occurred_at, detail) VALUES ('enabled', ?, ?)",
            (utc_iso_now(), "test cutover")).lastrowid
    yield
    with db_session() as db:
        db.execute("DELETE FROM lab_module_events WHERE id = ?", (event_id,))


def _patient(created, dept=WARD, name="Guard Test"):
    pid = f"PLOG{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute("INSERT INTO patients (id, name, mrn, status) VALUES (?, ?, ?, 'admitted')",
                   (pid, name, f"MRN-LOG-{uuid.uuid4().hex[:8]}"))
        db.execute("INSERT INTO admissions (id, patient_id, admitted_by, department, status) "
                   "VALUES (?, ?, 'D001', ?, 'active')",
                   (f"ALOG{uuid.uuid4().hex[:8].upper()}", pid, dept))
    created.append(pid)
    return pid


@pytest.fixture
def patient(_catalogue):
    return _patient(_catalogue)


def _user(role, dept=WARD):
    uid = f"LOG{uuid.uuid4().hex[:6].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO users (id, name, role, department, password_hash) VALUES (?, ?, ?, ?, ?)",
            (uid, f"{role.title()} {uid}", role, dept, hash_password("password")))
    return uid, {"Authorization": f"Bearer {create_token(uid, role)}"}


def _hdr(role, dept=WARD):
    return _user(role, dept)[1]


def _order(patient_id, code="2823-3", system="LOINC", *, status="active"):
    return orders_service.create_order(
        patient_id=patient_id, order_type="lab", description=f"{system} {code}",
        code=code, code_system=system, ordered_by=DOCTOR_ID, status=status)


def _items(order_id):
    with db_session() as db:
        return [dict(r) for r in db.execute(
            "SELECT * FROM lab_order_items WHERE order_id = ? ORDER BY id", (order_id,)).fetchall()]


def _order_row(order_id):
    with db_session() as db:
        return dict(db.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone())


def _specimen(specimen_id):
    with db_session() as db:
        return dict(db.execute("SELECT * FROM lab_specimens WHERE id = ?",
                               (specimen_id,)).fetchone())


def _exceptions(order_id, kind, *, open_only=True):
    sql = "SELECT * FROM lab_exceptions WHERE order_id = ? AND kind = ?"
    if open_only:
        sql += " AND resolved_at IS NULL"
    with db_session() as db:
        return [dict(r) for r in db.execute(sql, (order_id, kind)).fetchall()]


def _set_items(order_id, status, *, test_ids=None):
    with db_session() as db:
        if test_ids:
            marks = ", ".join("?" for _ in test_ids)
            db.execute(f"UPDATE lab_order_items SET status = ? WHERE order_id = ? "
                       f"AND test_id IN ({marks})", (status, order_id, *test_ids))
        else:
            db.execute("UPDATE lab_order_items SET status = ? WHERE order_id = ?",
                       (status, order_id))


def _reconcile(order_id):
    with db_session(immediate=True) as db:
        return los.reconcile_order_status(db, order_id, actor_id="LABTEST")


def _managed_order(patient_id, code="2823-3"):
    order = _order(patient_id, code)
    assert _items(order["id"]), "the laboratory should have taken the order in"
    return order


def _billed_order_ids(patient_id):
    with db_session() as db:
        return {c["source_ref"] for c in billing_service._clinical_candidates(
            db, patient_id=patient_id) if c["source"] == "order"}


# ── 1. The guard ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("target", ["in_progress", "completed", "resulted", "cancelled"])
def test_a_nurse_cannot_move_a_lab_managed_order_with_the_lab_on(lab_on, patient, target):
    order = _managed_order(patient)
    resp = client.post(f"/api/orders/{order['id']}/transition", json={"to_status": target},
                       headers=_hdr("nurse"))
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["error"] == "order_managed_by_lab"
    assert "managed by the laboratory" in resp.json()["detail"]["message"]
    assert _order_row(order["id"])["status"] == "active"


@pytest.mark.parametrize("target", ["in_progress", "completed", "resulted", "cancelled"])
def test_the_guard_holds_with_the_lab_switched_off(lab_on, patient, monkeypatch, target):
    order = _managed_order(patient)
    monkeypatch.setattr(config, "LAB_ENABLED", False)
    resp = client.post(f"/api/orders/{order['id']}/transition", json={"to_status": target},
                       headers=_hdr("doctor"))
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["error"] == "order_managed_by_lab"


@pytest.mark.parametrize("lab_switch", [True, False])
def test_a_free_text_result_on_a_lab_managed_order_is_refused(lab_on, patient, monkeypatch,
                                                             lab_switch):
    order = _managed_order(patient)
    monkeypatch.setattr(config, "LAB_ENABLED", lab_switch)
    resp = client.post(f"/api/orders/{order['id']}/result", json={"result_text": "K 4.1"},
                       headers=_hdr("nurse"))
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["error"] == "order_managed_by_lab"
    row = _order_row(order["id"])
    assert row["status"] == "active" and row["result_text"] is None


def test_a_stale_replay_of_the_current_status_is_refused_too(lab_on, patient):
    order = _managed_order(patient)
    with db_session(immediate=True) as db:
        orders_service.transition_in_conn(db, order["id"], "in_progress", actor_id="LABTEST")
    with pytest.raises(orders_service.OrderManagedByLab):
        orders_service.transition_order(order["id"], "in_progress", "N001")


def test_moving_a_lab_managed_order_to_active_stays_a_no_op(lab_on, patient):
    order = _managed_order(patient)
    resp = client.post(f"/api/orders/{order['id']}/transition", json={"to_status": "active"},
                       headers=_hdr("nurse"))
    assert resp.status_code == 200, resp.text
    assert resp.json()["order"]["status"] == "active"


def test_orders_the_lab_does_not_manage_keep_todays_behaviour(patient):
    assert config.LAB_ENABLED is not True
    first = _order(patient)
    second = _order(patient)
    hdr = _hdr("nurse")
    assert _items(first["id"]) == []
    for target in ("in_progress", "completed"):
        resp = client.post(f"/api/orders/{first['id']}/transition",
                           json={"to_status": target}, headers=hdr)
        assert resp.status_code == 200, resp.text
    resp = client.post(f"/api/orders/{second['id']}/result", json={"result_text": "K 4.1"},
                       headers=hdr)
    assert resp.status_code == 200 and resp.json()["order"]["status"] == "resulted"


def test_a_bedside_test_keeps_the_free_text_path_with_the_lab_on(lab_on, patient):
    order = _order(patient, "ORB-LAB-BLOOD-KETONES", "LOCAL")
    assert _items(order["id"]) == []
    resulted = orders_service.add_result(order["id"], "Ketones 0.4 mmol/L", DOCTOR_ID)
    assert resulted["status"] == "resulted"


def test_the_service_raises_for_every_caller_not_only_the_route(lab_on, patient):
    order = _managed_order(patient)
    with pytest.raises(orders_service.OrderManagedByLab):
        orders_service.transition_order(order["id"], "completed", DOCTOR_ID)
    with pytest.raises(orders_service.OrderManagedByLab):
        orders_service.add_result(order["id"], "anything", DOCTOR_ID)
    # A ValueError subclass, so any caller that only knows "bad request" refuses.
    assert issubclass(orders_service.OrderManagedByLab, ValueError)


# ── 2. The completion rule ───────────────────────────────────────────────────

@pytest.mark.parametrize("statuses,expected", [
    ([], None),
    (["cancelled", "cancelled"], "cancelled"),
    (["released", "cancelled"], "resulted"),
    (["released", "released"], "resulted"),
    (["released", "rejected"], "in_progress"),
    (["rejected", "cancelled"], "in_progress"),
    (["released", "received"], "in_progress"),
    (["awaiting_collection", "cancelled"], "active"),
    (["collected", "awaiting_collection"], "in_progress"),
    (["awaiting_collection"], "active"),
])
def test_the_completion_rule(statuses, expected):
    assert los.order_target_status(statuses) == expected


def test_a_partly_released_and_cancelled_order_is_resulted_with_the_fixed_text(lab_on, patient):
    order = _managed_order(patient, "58410-2")     # CBC: five tests on one tube
    tests = [i["test_id"] for i in _items(order["id"])]
    _set_items(order["id"], "released", test_ids=tests[:2])
    _set_items(order["id"], "cancelled", test_ids=tests[2:])
    outcome = _reconcile(order["id"])
    assert outcome["status"] == "resulted" and outcome["changed"]
    row = _order_row(order["id"])
    assert row["result_text"] == los.LAB_RESULT_TEXT
    assert "accession" not in row["result_text"].lower()
    assert order["id"] in _billed_order_ids(patient)


def test_a_rejected_item_keeps_the_order_in_progress_and_is_on_the_list(lab_on, patient):
    order = _managed_order(patient, "58410-2")
    tests = [i["test_id"] for i in _items(order["id"])]
    _set_items(order["id"], "released", test_ids=tests[:4])
    _set_items(order["id"], "rejected", test_ids=tests[4:])
    assert _reconcile(order["id"])["status"] == "in_progress"
    assert len(_exceptions(order["id"], "rejected_no_decision")) == 1
    _reconcile(order["id"])                                   # no second entry
    assert len(_exceptions(order["id"], "rejected_no_decision")) == 1

    listed = client.get("/api/lab/exceptions", headers=_hdr("lab_technician", None))
    assert listed.status_code == 200, listed.text
    mine = [x for x in listed.json()["exceptions"] if x["order_id"] == order["id"]]
    assert [x["kind"] for x in mine] == ["rejected_no_decision"]
    assert mine[0]["kind_label"] == "Rejected specimen waiting for a decision"
    assert order["id"] not in _billed_order_ids(patient)

    # Cancelling the rejected test is the decision: resulted, and the entry closes.
    resp = client.post(f"/api/lab/orders/{order['id']}/cancel",
                       json={"reason": "Not needed after review"}, headers=_hdr("doctor"))
    assert resp.status_code == 200, resp.text
    assert _order_row(order["id"])["status"] == "resulted"
    assert _exceptions(order["id"], "rejected_no_decision") == []


def test_an_order_moved_by_hand_before_the_guard_never_blocks_the_laboratory(lab_on, patient,
                                                                             caplog):
    order = _managed_order(patient)
    with db_session() as db:            # what an old manual completion left behind
        db.execute("UPDATE orders SET status = 'completed' WHERE id = ?", (order["id"],))
    _set_items(order["id"], "released")
    outcome = _reconcile(order["id"])
    assert outcome["conflict"] and outcome["status"] == "completed"
    assert len(_exceptions(order["id"], "order_state_conflict")) == 1
    assert any("changed outside the laboratory" in r.getMessage() for r in caplog.records)
    # And the laboratory's own step still commits: the items stay released.
    assert {i["status"] for i in _items(order["id"])} == {"released"}


# ── 3. The laboratory-mediated cancel ────────────────────────────────────────

def _audits(action, target_id):
    with db_session() as db:
        return [dict(r) for r in db.execute(
            "SELECT * FROM audit_logs WHERE action_type = ? AND target_id = ?",
            (action, target_id)).fetchall()]


def test_cancel_before_collection_cancels_everything_and_never_bills(lab_on, patient):
    order = _managed_order(patient, "58410-2")
    specimen_id = _items(order["id"])[0]["specimen_id"]
    resp = client.post(f"/api/lab/orders/{order['id']}/cancel",
                       json={"reason": "Ordered on the wrong patient"}, headers=_hdr("nurse"))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["cancelled_items"] == 5 and body["kept_released"] == 0
    assert body["order"]["status"] == "cancelled"
    assert body["cancelled_specimens"] == [specimen_id]
    assert {i["status"] for i in _items(order["id"])} == {"cancelled"}
    assert {i["cancelled_reason"] for i in _items(order["id"])} == {"Ordered on the wrong patient"}
    assert _specimen(specimen_id)["status"] == "cancelled"
    assert order["id"] not in _billed_order_ids(patient)
    audit = _audits("lab_order_cancelled", order["id"])
    assert len(audit) == 1 and audit[0]["event_category"] == "clinical_access"
    events = orders_service.get_order_events(order["id"])
    assert events[-1]["new_status"] == "cancelled"
    assert "Ordered on the wrong patient" in events[-1]["reason"]


def test_cancel_after_receipt_withdraws_unreleased_results_and_keeps_released(lab_on, patient):
    order = _managed_order(patient, "58410-2")
    items = _items(order["id"])
    released, received = items[:2], items[2:]
    now = utc_iso_now()
    with db_session() as db:
        db.execute("UPDATE lab_specimens SET status = 'received' WHERE id = ?",
                   (items[0]["specimen_id"],))
        for item in items:
            status = "released" if item in released else "received"
            db.execute("UPDATE lab_order_items SET status = ? WHERE id = ?", (status, item["id"]))
            db.execute(
                "INSERT INTO lab_results (order_item_id, order_id, specimen_id, patient_id, "
                "test_id, value_milli, entered_at, status) VALUES (?, ?, ?, ?, ?, 5000, ?, ?)",
                (item["id"], order["id"], item["specimen_id"], patient, item["test_id"], now,
                 "released" if item in released else "entered"))
        db.execute("UPDATE orders SET status = 'in_progress' WHERE id = ?", (order["id"],))

    resp = client.post(f"/api/lab/orders/{order['id']}/cancel",
                       json={"reason": "Patient transferred out"},
                       headers=_hdr("lab_technician", None))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["cancelled_items"] == 3 and body["withdrawn_results"] == 3
    assert body["kept_released"] == 2
    assert body["order"]["status"] == "resulted"
    assert body["cancelled_specimens"] == []      # the tube keeps its released tests
    with db_session() as db:
        results = {r["order_item_id"]: dict(r) for r in db.execute(
            "SELECT * FROM lab_results WHERE order_id = ?", (order["id"],)).fetchall()}
        withdrawn_events = db.execute(
            "SELECT COUNT(*) FROM lab_result_events WHERE event_type = 'withdrawn' "
            "AND patient_id = ?", (patient,)).fetchone()[0]
    for item in released:
        assert results[item["id"]]["status"] == "released"
    for item in received:
        assert results[item["id"]]["status"] == "withdrawn"
        assert results[item["id"]]["amend_reason"] == "Order cancelled"
    assert withdrawn_events == 3
    assert _order_row(order["id"])["result_text"] == los.LAB_RESULT_TEXT


def test_a_cancel_needs_a_reason(lab_on, patient):
    order = _managed_order(patient)
    resp = client.post(f"/api/lab/orders/{order['id']}/cancel", json={"reason": "  "},
                       headers=_hdr("nurse"))
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"] == "reason_required"
    assert _order_row(order["id"])["status"] == "active"


def test_a_cancel_is_scoped_and_permissioned_like_an_order_write(lab_on, patient):
    order = _managed_order(patient)
    other_ward = client.post(f"/api/lab/orders/{order['id']}/cancel",
                             json={"reason": "x"}, headers=_hdr("nurse", OTHER_WARD))
    assert other_ward.status_code == 404
    assert other_ward.json()["detail"]["error"] == "not_found"
    unknown = client.post("/api/lab/orders/no-such-order/cancel", json={"reason": "x"},
                          headers=_hdr("nurse"))
    assert unknown.status_code == 404
    assert other_ward.json()["detail"] == unknown.json()["detail"]
    # Admin holds neither order.write nor lab.receive.
    admin = client.post(f"/api/lab/orders/{order['id']}/cancel", json={"reason": "x"},
                        headers=_hdr("admin", None))
    assert admin.status_code == 403
    # A phlebotomist collects but does not cancel.
    phleb = client.post(f"/api/lab/orders/{order['id']}/cancel", json={"reason": "x"},
                        headers=_hdr("phlebotomist", None))
    assert phleb.status_code == 403
    assert _order_row(order["id"])["status"] == "active"


def test_a_repeat_cancel_is_harmless_and_a_reported_order_has_nothing_to_cancel(lab_on, patient):
    order = _managed_order(patient)
    hdr = _hdr("doctor")
    first = client.post(f"/api/lab/orders/{order['id']}/cancel", json={"reason": "dup"},
                        headers=hdr)
    again = client.post(f"/api/lab/orders/{order['id']}/cancel", json={"reason": "dup"},
                        headers=hdr)
    assert first.status_code == 200 and again.status_code == 200
    assert again.json()["already_cancelled"] is True
    assert len(_audits("lab_order_cancelled", order["id"])) == 1

    reported = _managed_order(patient)
    _set_items(reported["id"], "released")
    _reconcile(reported["id"])
    resp = client.post(f"/api/lab/orders/{reported['id']}/cancel", json={"reason": "late"},
                       headers=hdr)
    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "nothing_to_cancel"


def test_an_order_the_lab_does_not_manage_is_not_cancelled_here(lab_on, patient):
    order = _order(patient, "ORB-LAB-VBG", "LOCAL")
    resp = client.post(f"/api/lab/orders/{order['id']}/cancel", json={"reason": "x"},
                       headers=_hdr("nurse"))
    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "not_lab_managed"


def test_cancel_is_served_while_the_module_drains(lab_on, patient, monkeypatch):
    order = _managed_order(patient)
    monkeypatch.setattr(config, "LAB_ENABLED", False)
    monkeypatch.setattr(config, "_MODULE_DRAIN_CHECKS", {"lab": lambda: True})
    assert lab_module.module_state() == "draining"
    resp = client.post(f"/api/lab/orders/{order['id']}/cancel",
                       json={"reason": "Module switched off"}, headers=_hdr("nurse"))
    assert resp.status_code == 200, resp.text
    assert _order_row(order["id"])["status"] == "cancelled"


def test_cancel_is_404_once_the_module_is_off(patient, monkeypatch):
    monkeypatch.setattr(config, "_MODULE_DRAIN_CHECKS", {"lab": lambda: False})
    assert lab_module.module_state() == "off"
    resp = client.post("/api/lab/orders/anything/cancel", json={"reason": "x"},
                       headers=_hdr("nurse"))
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "module_disabled"


def test_a_cancel_whose_audit_fails_changes_nothing(lab_on, patient, monkeypatch):
    order = _managed_order(patient)
    real = los.log_audit_event

    def failing(*args, **kwargs):
        if kwargs.get("action_type") == "lab_order_cancelled":
            raise AuditWriteError("audit store unavailable")
        return real(*args, **kwargs)
    monkeypatch.setattr(los, "log_audit_event", failing)
    resp = client.post(f"/api/lab/orders/{order['id']}/cancel", json={"reason": "x"},
                       headers=_hdr("nurse"))
    assert resp.status_code == 503
    assert resp.json()["detail"]["error"] == "audit_unavailable"
    assert _order_row(order["id"])["status"] == "active"
    assert {i["status"] for i in _items(order["id"])} == {"awaiting_collection"}


# ── 4. The order list ────────────────────────────────────────────────────────

def test_the_order_list_says_which_orders_the_lab_manages(lab_on, patient):
    managed = _managed_order(patient, "58410-2")
    tests = [i["test_id"] for i in _items(managed["id"])]
    _set_items(managed["id"], "released", test_ids=tests[:1])
    bedside = _order(patient, "ORB-LAB-BLOOD-KETONES", "LOCAL")
    nursing = orders_service.create_order(
        patient_id=patient, order_type="nursing", description="Strict fluid balance",
        ordered_by=DOCTOR_ID, status="active")

    resp = client.get(f"/api/orders/patient/{patient}", headers=_hdr("doctor"))
    assert resp.status_code == 200, resp.text
    by_id = {o["id"]: o for o in resp.json()["orders"]}
    lab = by_id[managed["id"]]
    assert lab["lab_managed"] is True
    assert lab["lab"]["counts"] == {"Reported": 1, "Waiting for collection": 4}
    assert lab["lab"]["summary"] == "5 tests: 1 reported, 4 waiting for collection"
    assert {t["status_text"] for t in lab["lab"]["tests"]} == {"Reported",
                                                              "Waiting for collection"}
    assert by_id[bedside["id"]]["lab_managed"] is False
    assert "lab" not in by_id[bedside["id"]]
    assert by_id[nursing["id"]]["lab_managed"] is False


def test_an_order_the_lab_could_not_take_in_says_why(lab_on, patient):
    with db_session() as db:             # a code terminology knows, the catalogue does not
        row = db.execute(
            "SELECT t.code_system, t.code FROM terminology t WHERE t.is_active = 1 "
            "AND t.code_system = 'SNOMED' AND NOT EXISTS (SELECT 1 FROM lab_tests l "
            "WHERE l.code_system = t.code_system AND l.code = t.code) LIMIT 1").fetchone()
    order = _order(patient, row["code"], row["code_system"])
    resp = client.get(f"/api/orders/patient/{patient}", headers=_hdr("doctor"))
    row = {o["id"]: o for o in resp.json()["orders"]}[order["id"]]
    assert row["lab_managed"] is False
    assert row["lab_intake"]["kind"] == "unmapped_code"
    assert "not in the laboratory catalogue" in row["lab_intake"]["message"]


def test_a_cancelled_order_says_who_cancelled_it(lab_on, patient):
    order = _managed_order(patient)
    uid, hdr = _user("nurse")
    client.post(f"/api/lab/orders/{order['id']}/cancel", json={"reason": "Duplicate"},
                headers=hdr)
    resp = client.get(f"/api/orders/patient/{patient}", headers=hdr)
    row = {o["id"]: o for o in resp.json()["orders"]}[order["id"]]
    assert row["lab"]["cancelled"]["by_name"] == f"Nurse {uid}"
    assert row["lab"]["cancelled"]["reason"] == "Duplicate"
    assert row["lab"]["cancelled"]["at"]


def test_with_the_lab_never_used_the_list_marks_nothing_managed(patient):
    order = _order(patient)
    resp = client.get(f"/api/orders/patient/{patient}", headers=_hdr("doctor"))
    row = {o["id"]: o for o in resp.json()["orders"]}[order["id"]]
    assert row["lab_managed"] is False and "lab" not in row and "lab_intake" not in row


# ── 5. The conn-taking helpers ───────────────────────────────────────────────

def test_the_helpers_need_the_callers_transaction(lab_on, patient):
    order = _managed_order(patient)
    with db_session() as db:
        with pytest.raises(RuntimeError):
            orders_service.transition_in_conn(db, order["id"], "in_progress")
        with pytest.raises(RuntimeError):
            orders_service.set_lab_result_text_in_conn(db, order["id"], "x")


def test_the_helpers_keep_the_state_machine_and_write_the_event(lab_on, patient):
    order = _managed_order(patient)
    with db_session(immediate=True) as db:
        with pytest.raises(ValueError):
            orders_service.transition_in_conn(db, order["id"], "completed")   # active -> completed
        moved = orders_service.transition_in_conn(db, order["id"], "in_progress",
                                                  actor_id="LABTEST", detail="collected")
    assert moved["status"] == "in_progress"
    last = orders_service.get_order_events(order["id"])[-1]
    assert (last["prior_status"], last["new_status"], last["actor_id"]) == (
        "active", "in_progress", "LABTEST")


def test_only_the_lab_order_service_calls_the_conn_taking_helpers():
    root = pathlib.Path(__file__).resolve().parents[1]
    allowed = {root / "services" / "orders_service.py",
               root / "services" / "lab_order_service.py"}
    offenders = []
    for path in root.rglob("*.py"):
        if ".venv" in path.parts or "tests" in path.parts or path in allowed:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            name = (node.attr if isinstance(node, ast.Attribute)
                    else node.id if isinstance(node, ast.Name) else None)
            if name in ("transition_in_conn", "set_lab_result_text_in_conn"):
                offenders.append(f"{path.relative_to(root)}:{node.lineno}")
    assert offenders == []
