"""The eMAR's order-state check for a ward row linked to a medication order
(medorders-3, W5-E; design section 6).

Pinned here:
  * an unlinked row is untouched: no event, its audit row exactly as before;
  * a linked prescribed or verified order allows 'given', and the
    'administered' event commits with the MAR row; the dose's audit row names
    the order and the event;
  * a held order refuses a new 'given' with words for the nurse (422 through
    the route), and 'not given (held)' is always recorded, with its event;
  * a replay of a dose given before the hold returns the original record;
  * a hold committed between the first check and the INSERT is seen by the
    second check and rolls the dose back;
  * an unsigned order, an ended order and a broken link refuse 'given';
  * a fault writing the event rolls the dose back;
  * keyed on the link, not the switch: a held order stays ungivable with the
    store off (ruling MO-R9 (a)).
"""
from __future__ import annotations

import json
import threading
import uuid

import pytest
from fastapi.testclient import TestClient

import config
from auth import create_token
from database import db_session
from main import app
from services import allergy_interlock, emar_service
from services.medorders import adoption, core, emar_check

_PATIENTS: list[str] = []
HOLD_REASON = "Nil by mouth before theatre"


@pytest.fixture(scope="module", autouse=True)
def _cleanup_store_rows():
    yield
    if not _PATIENTS:
        return
    marks = ", ".join("?" for _ in _PATIENTS)
    with db_session() as db:
        db.execute(f"DELETE FROM med_order_events WHERE patient_id IN ({marks})", _PATIENTS)
        db.execute(f"DELETE FROM med_orders WHERE patient_id IN ({marks})", _PATIENTS)
        db.execute(f"UPDATE medications SET med_order_id = NULL, held_at = NULL, "
                   f"hold_reason = NULL WHERE patient_id IN ({marks})", _PATIENTS)


def _user(role, department=None):
    uid = f"MOEM{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute("INSERT INTO users (id, name, role, password_hash, department) "
                   "VALUES (?, ?, ?, 'x', ?)", (uid, f"eMAR check {role}", role, department))
    return uid


def _patient():
    pid = f"PMOEM{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute("INSERT INTO patients (id, name, mrn, status) VALUES (?, ?, ?, 'admitted')",
                   (pid, "eMAR Check Test", f"MRN-{uuid.uuid4().hex[:8]}"))
    _PATIENTS.append(pid)
    return pid


def _chart(pid, *, drug="Paracetamol", dosage="500 mg", frequency="TDS", status="active"):
    with db_session() as db:
        return db.execute(
            "INSERT INTO medications (patient_id, drug_name, dosage, route, frequency, status, "
            "prescribed_by, source) VALUES (?, ?, ?, 'Oral', ?, ?, NULL, 'clinician')",
            (pid, drug, dosage, frequency, status)).lastrowid


@pytest.fixture()
def nurse():
    return _user("nurse")


@pytest.fixture()
def linked(monkeypatch):
    """A clean chart drug for an admitted patient, adopted into the store
    (prescribed), with the store switched on to adopt it."""
    monkeypatch.setattr(config, "MED_ORDER_STORE_ENABLED", True)
    pid = _patient()
    med_id = _chart(pid)
    adoption.adopt_legacy(trigger="script", patient_ids=[pid])
    return {"patient_id": pid, "med_id": med_id, "order_id": _order_id(med_id)}


def _order_id(med_id):
    with db_session() as db:
        return db.execute("SELECT med_order_id FROM medications WHERE id = ?",
                          (med_id,)).fetchone()[0]


def _order(order_id):
    with db_session() as db:
        return dict(db.execute("SELECT * FROM med_orders WHERE id = ?", (order_id,)).fetchone())


def _events(order_id, kind=None):
    sql, args = "SELECT * FROM med_order_events WHERE med_order_id = ?", [order_id]
    if kind:
        sql += " AND kind = ?"
        args.append(kind)
    with db_session() as db:
        return [dict(r) for r in db.execute(sql + " ORDER BY id", args).fetchall()]


def _mar_rows(med_id):
    with db_session() as db:
        return [dict(r) for r in db.execute(
            "SELECT * FROM medication_administrations WHERE medication_id = ? ORDER BY id",
            (med_id,)).fetchall()]


def _hold(order_id, *, by="MOEMHOLDER"):
    with db_session(immediate=True) as conn:
        core.transition_in_txn(conn, order_id, "held", expected_version=None, actor_id=by,
                               actor_role="doctor", entry_point="test.hold", reason=HOLD_REASON)


def _set_status(order_id, status):
    """Put an order in a state no route reaches yet (a test of the check
    itself, not of how the order got there)."""
    with db_session() as db:
        db.execute("UPDATE med_orders SET status = ?, held_from = NULL, projection_status = "
                   "CASE WHEN ? IN ('prescribed', 'verified') THEN 'active' ELSE NULL END, "
                   "verified_by = CASE WHEN ? = 'verified' THEN 'MOEMPHARM' ELSE verified_by END "
                   "WHERE id = ?", (status, status, status, order_id))


def _give(med_id, nurse, **kw):
    return emar_service.record_administration(med_id, nurse, status="given", **kw)


def _dose_audit(admin_id):
    with db_session() as db:
        for r in db.execute("SELECT * FROM audit_logs WHERE action_type = "
                            "'medication_administered' ORDER BY rowid DESC LIMIT 50").fetchall():
            meta = json.loads(r["metadata_json"])
            if meta.get("administration_id") == admin_id:
                return meta
    return None


# ── Unlinked rows: exactly as before ──────────────────────────────────────────

def test_an_unlinked_row_is_untouched(nurse):
    pid = _patient()
    med_id = _chart(pid)
    rec = _give(med_id, nurse)
    assert rec["replayed"] is False
    meta = _dose_audit(rec["id"])
    assert "med_order_id" not in meta and "med_order_event_id" not in meta
    with db_session() as db:
        assert db.execute("SELECT COUNT(*) FROM med_order_events WHERE patient_id = ?",
                          (pid,)).fetchone()[0] == 0


# ── Linked rows ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("status", ["prescribed", "verified"])
def test_a_live_order_allows_the_dose_and_the_event_commits_with_it(linked, nurse, status):
    _set_status(linked["order_id"], status)
    rec = _give(linked["med_id"], nurse, confirmation_method="typed")
    (event,) = _events(linked["order_id"], "administered")
    assert event["ref_type"] == "medication_administration"
    assert event["ref_id"] == str(rec["id"])
    assert (event["from_status"], event["to_status"]) == (status, status)
    assert event["actor_id"] == nurse and event["actor_role"] == "nurse"
    assert event["entry_point"] == emar_check.ENTRY_POINT
    assert json.loads(event["payload_json"])["medication_id"] == linked["med_id"]
    meta = _dose_audit(rec["id"])
    assert meta["med_order_id"] == linked["order_id"]
    assert meta["med_order_event_id"] == event["id"]


def test_a_held_order_refuses_a_new_dose_in_words(linked, nurse):
    _hold(linked["order_id"])
    with pytest.raises(emar_check.OrderStateRefused) as exc:
        _give(linked["med_id"], nurse)
    assert isinstance(exc.value, ValueError)
    assert "on hold" in str(exc.value) and "not given (held)" in str(exc.value)
    assert exc.value.verdict["hold_reason"] == HOLD_REASON
    assert _mar_rows(linked["med_id"]) == []
    assert _events(linked["order_id"], "administered") == []


def test_the_route_answers_a_held_dose_with_422_and_the_words(linked):
    nurse = _user("nurse")
    _hold(linked["order_id"])
    client = TestClient(app)
    resp = client.post(f"/api/emar/medications/{linked['med_id']}/administer",
                       headers={"Authorization": f"Bearer {create_token(nurse, 'nurse')}"},
                       json={"status": "given"})
    assert resp.status_code == 422
    assert "on hold" in resp.json()["detail"]
    assert _mar_rows(linked["med_id"]) == []


def test_not_given_held_is_always_recorded_on_a_held_order(linked, nurse):
    _hold(linked["order_id"])
    rec = emar_service.record_administration(linked["med_id"], nurse, status="not_given",
                                             not_given_reason="held")
    assert rec["status"] == "not_given"
    (event,) = _events(linked["order_id"], "not_given")
    assert event["reason"] == "held" and event["to_status"] == "held"
    assert event["ref_id"] == str(rec["id"])


def test_a_replay_of_a_dose_given_before_the_hold_returns_the_original(linked, nurse):
    key = f"dose-{uuid.uuid4()}"
    first = _give(linked["med_id"], nurse, idempotency_key=key)
    _hold(linked["order_id"])
    again = _give(linked["med_id"], nurse, idempotency_key=key)
    assert again["replayed"] is True and again["id"] == first["id"]
    assert len(_mar_rows(linked["med_id"])) == 1
    assert len(_events(linked["order_id"], "administered")) == 1
    # ...while a NEW dose on the held order is refused.
    with pytest.raises(emar_check.OrderStateRefused):
        _give(linked["med_id"], nurse, idempotency_key=f"dose-{uuid.uuid4()}")


def test_a_hold_committed_between_the_first_check_and_the_insert_rolls_the_dose_back(
        linked, nurse, monkeypatch):
    """The window the post-INSERT read closes. The hold is committed on
    another thread (its own connection) after the first check has passed and
    before the MAR row is written."""
    real_screen = allergy_interlock.screen_for_administration
    fired = {}

    def _screen_then_hold(*a, **k):
        if not fired:
            fired["at"] = "after the first check, before the INSERT"
            errors = []

            def _other_session():
                try:
                    _hold(linked["order_id"], by="MOEMRACER")
                except Exception as e:  # noqa: BLE001
                    errors.append(e)
            t = threading.Thread(target=_other_session)
            t.start()
            t.join(timeout=60)
            assert not errors, errors
        return real_screen(*a, **k)

    real_check = emar_check.check_dose_in_txn
    calls = []

    def _counted(conn, *a, **k):
        calls.append(bool(getattr(conn, "in_transaction", False)))
        return real_check(conn, *a, **k)

    monkeypatch.setattr(allergy_interlock, "screen_for_administration", _screen_then_hold)
    monkeypatch.setattr(emar_check, "check_dose_in_txn", _counted)
    with pytest.raises(emar_check.OrderStateRefused):
        _give(linked["med_id"], nurse)
    assert fired, "the injected hold never ran"
    # The first read passed outside the write transaction; the refusal came
    # from the second, under the write lock the INSERT took.
    assert calls == [False, True]
    assert _order(linked["order_id"])["status"] == "held"
    assert _mar_rows(linked["med_id"]) == []
    assert _events(linked["order_id"], "administered") == []


@pytest.mark.parametrize("status,code", [("draft", "order_not_signed"),
                                         ("stopped", "order_ended"),
                                         ("rejected", "order_ended")])
def test_an_unsigned_or_ended_order_refuses_given(linked, nurse, status, code):
    """The ward row is left active on purpose, so only the order can refuse."""
    _set_status(linked["order_id"], status)
    with pytest.raises(emar_check.OrderStateRefused) as exc:
        _give(linked["med_id"], nurse)
    assert exc.value.verdict["code"] == code
    assert _mar_rows(linked["med_id"]) == []


def test_a_broken_link_refuses_given_and_still_records_not_given(nurse):
    pid = _patient()
    med_id = _chart(pid)
    with db_session() as db:
        db.execute("UPDATE medications SET med_order_id = ? WHERE id = ?",
                   (str(uuid.uuid4()), med_id))
    with pytest.raises(emar_check.OrderStateRefused) as exc:
        _give(med_id, nurse)
    assert exc.value.verdict["code"] == "order_state_unknown"
    rec = emar_service.record_administration(med_id, nurse, status="not_given",
                                             not_given_reason="refused")
    assert rec["status"] == "not_given"


def test_a_fault_writing_the_event_rolls_the_dose_back(linked, nurse, monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("injected fault writing the bedside event")
    monkeypatch.setattr(core, "record_event_in_txn", _boom)
    with pytest.raises(RuntimeError):
        _give(linked["med_id"], nurse)
    assert _mar_rows(linked["med_id"]) == []


def test_a_held_order_stays_ungivable_with_the_store_switched_off(linked, nurse, monkeypatch):
    """Ruling MO-R9 (a): the check follows the link, not the switch."""
    _hold(linked["order_id"])
    monkeypatch.setattr(config, "MED_ORDER_STORE_ENABLED", False)
    monkeypatch.setattr(config, "MED_ORDER_LIFECYCLE_ENABLED", False)
    with pytest.raises(emar_check.OrderStateRefused):
        _give(linked["med_id"], nurse)
    rec = emar_service.record_administration(linked["med_id"], nurse, status="not_given",
                                             not_given_reason="held")
    assert rec["status"] == "not_given"


def test_the_legacy_checks_still_come_first(linked, nurse):
    """An inactive ward row is refused by the ward row's own check, as
    before, whatever the order says."""
    with db_session() as db:
        db.execute("UPDATE medications SET status = 'discontinued' WHERE id = ?",
                   (linked["med_id"],))
    with pytest.raises(ValueError) as exc:
        _give(linked["med_id"], nurse)
    assert not isinstance(exc.value, emar_check.OrderStateRefused)
    assert "not an active order" in str(exc.value)
