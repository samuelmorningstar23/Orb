"""The ward end of a pharmacy dispense: `dispensed -> delivered`.

Until 2026-09-07 this hop did not exist as a separate act. It was gated on
`helix.write`, so the pharmacist who dispensed also recorded that the ward had
received the drug, and the order row kept no trace of who took delivery. These
tests pin the three things that make it a real custody step:

  * pharmacy CANNOT reach `delivered`, by permission or by naming the status,
  * the ward CAN, and the row records who and when,
  * one account cannot do both halves even when it holds both permissions.

Plus the billing defect the same scaffolding caused: every dispense used to
flag itself `is_mock_event = 1`, which billing then excluded, so the pharmacy
revenue line was structurally zero on every site.
"""
import uuid

import pytest
from fastapi.testclient import TestClient

from main import app
from database import db_session
from auth import hash_password
from services import helix_service
from services.helix_db import init_helix_db

client = TestClient(app)

_MED_ID = "HXRECEIVEPARA"
# helix_service.create_order resolves the master row with LIKE '%name%', so a
# common drug name collides with whatever other modules seeded in the shared
# test DB (a 'Dolo' row, with no stock, matched 'Paracetamol' and made every
# dispense here fail). The fixture owns a name nothing else can match.
_MED_NAME = "Zylotram Receipt Fixture 500mg Tablet"


@pytest.fixture(scope="module", autouse=True)
def _seed_module():
    init_helix_db()
    with db_session() as db:
        db.execute(
            "INSERT OR IGNORE INTO helix_medication_master (id, name, generic_name, form, strength) "
            "VALUES (?, ?, 'zylotram', 'tablet', '500mg')",
            (_MED_ID, _MED_NAME),
        )
        # One lot, ample for the module: each dispense decrements by 1.
        db.execute(
            "INSERT OR IGNORE INTO helix_inventory_lots (id, medication_id, lot_number, quantity, expiration_date) "
            "VALUES ('LOTRECEIVE1', ?, 'LOT-RCV-1', 500, '2030-01-01')",
            (_MED_ID,),
        )


def _mk_user(role, department=None):
    uid = f"URCV{uuid.uuid4().hex[:6].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT OR IGNORE INTO users (id, name, role, password_hash, department) VALUES (?,?,?,?,?)",
            (uid, f"{role.title()} {uid}", role, hash_password("secretpass"), department),
        )
    return uid


def _login_hdr(user_id):
    resp = client.post("/api/auth/login",
                       json={"employee_id": user_id, "password": "secretpass"})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['token']}"}


def _mk_admitted_patient(department="General Medicine"):
    pid = f"PATRCV{uuid.uuid4().hex[:6].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO patients (id, name, mrn, status, bed, ward) "
            "VALUES (?, ?, ?, 'admitted', 'B-1', 'GM-1')",
            (pid, f"Receipt Case {uuid.uuid4().hex[:5]}", f"MRN-{uuid.uuid4().hex[:6]}"),
        )
        db.execute(
            "INSERT INTO admissions (patient_id, department, ward, bed, status, admitted_by) "
            "VALUES (?, ?, 'GM-1', 'B-1', 'active', 'system')",
            (pid, department),
        )
    return pid


def _order_at(status, patient_id, prescriber, dispenser=None):
    """An order walked to `status` through the real service, so the transition
    log is real too (the dispatch actor is read from it, not from a column)."""
    order = helix_service.create_order(
        patient_id=patient_id, medication_name=_MED_NAME,
        dose="500mg", route="PO", frequency="TDS", prescribed_by=prescriber,
    )
    oid = order["id"]
    walk = {"reviewing": ["reviewing"],
            "verified": ["reviewing", "verified"],
            "scheduled": ["reviewing", "verified", "scheduled"],
            "dispensed": ["reviewing", "verified", "scheduled", "dispensed"]}[status]
    for target in walk:
        helix_service.transition_order_status(
            order_id=oid, target_status=target,
            actor_id=dispenser or prescriber,
        )
    return oid


def _row(order_id):
    with db_session() as db:
        return dict(db.execute(
            "SELECT * FROM helix_patient_med_orders WHERE id = ?", (order_id,)).fetchone())


# ── pharmacy cannot reach `delivered` ────────────────────────────────────────
def test_pharmacy_transition_route_refuses_delivered():
    """Naming the status in the body must reach nothing. This is the hole the
    old design left open: `delivered` was just another `helix.write` target."""
    pharmacist = _mk_user("pharmacist")
    hdr = _login_hdr(pharmacist)
    pid = _mk_admitted_patient()
    oid = _order_at("dispensed", pid, pharmacist)

    resp = client.post(f"/api/helix/orders/{oid}/transition",
                       json={"target_status": "delivered"}, headers=hdr)
    assert resp.status_code == 400, resp.text
    assert "ward" in resp.json()["detail"].lower()
    assert _row(oid)["status"] == "dispensed", "refused transition must not write"


def test_pharmacist_cannot_call_the_receive_route():
    """The permission, not just the route. A pharmacist holds `helix.write` and
    deliberately not `helix.receive`."""
    pharmacist = _mk_user("pharmacist")
    hdr = _login_hdr(pharmacist)
    pid = _mk_admitted_patient()
    oid = _order_at("dispensed", pid, pharmacist)

    resp = client.post(f"/api/helix/orders/{oid}/receive", json={}, headers=hdr)
    assert resp.status_code == 403, resp.text
    assert _row(oid)["status"] == "dispensed"


def test_service_refuses_delivered_directly():
    """Belt and braces below the route: anything calling the service reaches the
    same refusal, so a future caller cannot reopen the hole."""
    pharmacist = _mk_user("pharmacist")
    pid = _mk_admitted_patient()
    oid = _order_at("dispensed", pid, pharmacist)
    with pytest.raises(ValueError, match="ward"):
        helix_service.transition_order_status(
            order_id=oid, target_status="delivered", actor_id=pharmacist)


# ── the ward can, and it is recorded ─────────────────────────────────────────
def test_nurse_receives_and_the_row_records_who_and_when():
    pharmacist = _mk_user("pharmacist")
    nurse = _mk_user("nurse", department="General Medicine")
    pid = _mk_admitted_patient("General Medicine")
    oid = _order_at("dispensed", pid, pharmacist)

    resp = client.post(f"/api/helix/orders/{oid}/receive", json={}, headers=_login_hdr(nurse))
    assert resp.status_code == 200, resp.text

    row = _row(oid)
    assert row["status"] == "delivered"
    assert row["received_by"] == nurse, "the ward's attestation names the ward"
    assert row["received_at"], "and when"


def test_receipt_lands_on_the_transition_log_as_its_own_hop():
    """The custody chain is the transition log. A receipt that only updated the
    order row would leave the chain saying pharmacy did the last step."""
    pharmacist = _mk_user("pharmacist")
    nurse = _mk_user("nurse", department="General Medicine")
    pid = _mk_admitted_patient("General Medicine")
    oid = _order_at("dispensed", pid, pharmacist)
    client.post(f"/api/helix/orders/{oid}/receive", json={}, headers=_login_hdr(nurse))

    with db_session() as db:
        hop = db.execute(
            "SELECT * FROM helix_transitions WHERE order_id = ? AND to_status = 'delivered'",
            (oid,)).fetchone()
    assert hop is not None
    assert hop["from_status"] == "dispensed"
    assert hop["actor_id"] == nurse, "the receipt hop is attributed to the receiver"


def test_receive_is_idempotent_under_a_repeated_key():
    pharmacist = _mk_user("pharmacist")
    nurse = _mk_user("nurse", department="General Medicine")
    pid = _mk_admitted_patient("General Medicine")
    oid = _order_at("dispensed", pid, pharmacist)
    hdr = _login_hdr(nurse)
    key = f"rcv-{uuid.uuid4().hex}"

    first = client.post(f"/api/helix/orders/{oid}/receive",
                        json={"idempotency_key": key}, headers=hdr)
    second = client.post(f"/api/helix/orders/{oid}/receive",
                         json={"idempotency_key": key}, headers=hdr)
    assert first.status_code == 200 and second.status_code == 200, second.text
    assert first.json()["already_applied"] is False
    assert second.json()["already_applied"] is True

    with db_session() as db:
        n = db.execute(
            "SELECT count(*) FROM helix_transitions WHERE order_id = ? AND to_status = 'delivered'",
            (oid,)).fetchone()[0]
    assert n == 1, "a replayed receipt must not write a second custody hop"


def test_cannot_receive_an_order_that_was_never_dispensed():
    pharmacist = _mk_user("pharmacist")
    nurse = _mk_user("nurse", department="General Medicine")
    pid = _mk_admitted_patient("General Medicine")
    oid = _order_at("verified", pid, pharmacist)

    resp = client.post(f"/api/helix/orders/{oid}/receive", json={}, headers=_login_hdr(nurse))
    assert resp.status_code == 400, resp.text
    assert _row(oid)["status"] == "verified"


# ── separation of duties survives a permission grant ─────────────────────────
def test_one_account_cannot_both_dispense_and_receive(monkeypatch):
    """THE CONTROL THAT MATTERS. The permission split is policy and an
    administrator can undo it by granting one login both. The service-level
    actor check is what still holds when they do, so it is tested against
    exactly that situation rather than against the happy-path grant."""
    from permissions import ROLE_PERMISSIONS, HELIX_RECEIVE
    monkeypatch.setitem(ROLE_PERMISSIONS, "pharmacist",
                        ROLE_PERMISSIONS["pharmacist"] | {HELIX_RECEIVE})

    pharmacist = _mk_user("pharmacist")
    pid = _mk_admitted_patient()
    oid = _order_at("dispensed", pid, pharmacist)

    resp = client.post(f"/api/helix/orders/{oid}/receive", json={},
                       headers=_login_hdr(pharmacist))
    assert resp.status_code == 403, resp.text
    assert "dispensed" in resp.json()["detail"].lower()
    assert _row(oid)["status"] == "dispensed", "self-receipt must not write"


# ── the ward's queue is ward-scoped ──────────────────────────────────────────
def test_awaiting_receipt_is_scoped_to_the_nurses_ward():
    """A nurse must not be handed the pharmacy's whole order book to find her
    own rows. Scope comes from the USER ROW, never from the request."""
    pharmacist = _mk_user("pharmacist")
    mine = _mk_admitted_patient("General Medicine")
    theirs = _mk_admitted_patient("Cardiology")
    oid_mine = _order_at("dispensed", mine, pharmacist)
    oid_theirs = _order_at("dispensed", theirs, pharmacist)

    nurse = _mk_user("nurse", department="General Medicine")
    resp = client.get("/api/helix/orders/awaiting-receipt", headers=_login_hdr(nurse))
    assert resp.status_code == 200, resp.text
    ids = {o["id"] for o in resp.json()["orders"]}
    assert oid_mine in ids
    assert oid_theirs not in ids, "another ward's in-transit drugs are not this nurse's"


def test_awaiting_receipt_names_the_dispatching_account():
    """The nurse is attesting against a specific dispatch. If the screen cannot
    say whose, the separation of duties is invisible to the person performing
    it."""
    pharmacist = _mk_user("pharmacist")
    nurse = _mk_user("nurse", department="General Medicine")
    pid = _mk_admitted_patient("General Medicine")
    oid = _order_at("dispensed", pid, pharmacist)

    resp = client.get("/api/helix/orders/awaiting-receipt", headers=_login_hdr(nurse))
    row = next(o for o in resp.json()["orders"] if o["id"] == oid)
    assert row["dispensed_by"] == pharmacist
    assert row["dispensed_at"]


def test_received_orders_leave_the_queue():
    pharmacist = _mk_user("pharmacist")
    nurse = _mk_user("nurse", department="General Medicine")
    pid = _mk_admitted_patient("General Medicine")
    oid = _order_at("dispensed", pid, pharmacist)
    hdr = _login_hdr(nurse)

    client.post(f"/api/helix/orders/{oid}/receive", json={}, headers=hdr)
    resp = client.get("/api/helix/orders/awaiting-receipt", headers=hdr)
    assert oid not in {o["id"] for o in resp.json()["orders"]}


def test_awaiting_receipt_names_the_substitute_that_was_dispensed():
    """The nurse signs for what is in the tray. With a substitute on the order
    the queue named the PRESCRIBED drug, so she attested to a drug that never
    left the pharmacy."""
    sub_id, sub_name = "HXRECEIVESUB", "Zylomol Substitute Fixture 650mg Tablet"
    with db_session() as db:
        db.execute(
            "INSERT OR IGNORE INTO helix_medication_master (id, name, generic_name, form, strength) "
            "VALUES (?, ?, 'zylomol', 'tablet', '650mg')",
            (sub_id, sub_name),
        )
        db.execute(
            "INSERT OR IGNORE INTO helix_inventory_lots (id, medication_id, lot_number, quantity, expiration_date) "
            "VALUES ('LOTRECEIVESUB', ?, 'LOT-RCV-SUB', 50, '2030-01-01')",
            (sub_id,),
        )
    pharmacist = _mk_user("pharmacist")
    nurse = _mk_user("nurse", department="General Medicine")
    pid = _mk_admitted_patient("General Medicine")
    order = helix_service.create_order(
        patient_id=pid, medication_name=_MED_NAME,
        dose="500mg", route="PO", frequency="TDS", prescribed_by=pharmacist,
    )
    oid = order["id"]
    helix_service.transition_order_status(order_id=oid, target_status="reviewing", actor_id=pharmacist)
    helix_service.transition_order_status(
        order_id=oid, target_status="substitute_suggested", actor_id=pharmacist,
        substitute_medication_id=sub_id, pharmacist_notes="Out of stock, prescriber agreed",
    )
    for target in ("reviewing", "verified", "scheduled", "dispensed"):
        helix_service.transition_order_status(order_id=oid, target_status=target, actor_id=pharmacist)

    resp = client.get("/api/helix/orders/awaiting-receipt", headers=_login_hdr(nurse))
    assert resp.status_code == 200, resp.text
    row = next(o for o in resp.json()["orders"] if o["id"] == oid)
    assert row["medication_name"] == sub_name
    assert row["strength"] == "650mg"
    assert row["original_medication_name"] == _MED_NAME
    assert row["pharmacist_notes"] == "Out of stock, prescriber agreed"


def test_awaiting_receipt_has_no_original_name_without_a_substitute():
    pharmacist = _mk_user("pharmacist")
    nurse = _mk_user("nurse", department="General Medicine")
    pid = _mk_admitted_patient("General Medicine")
    oid = _order_at("dispensed", pid, pharmacist)
    resp = client.get("/api/helix/orders/awaiting-receipt", headers=_login_hdr(nurse))
    row = next(o for o in resp.json()["orders"] if o["id"] == oid)
    assert row["medication_name"] == _MED_NAME
    assert row["original_medication_name"] is None


# ── the billing defect the same scaffolding caused ───────────────────────────
def test_a_dispense_is_billable():
    """EVERY DISPENSE USED TO BE UNBILLABLE. `helix_service` set
    `is_mock_event = 1` on every transition into dispensed/delivered, and
    `billing_service` excluded exactly those rows, so the pharmacy revenue line
    was structurally zero and silently so.

    Asserts the INVARIANT (a dispensed drug appears as a billing candidate),
    not the absence of a column — a future flag with a different name would
    reintroduce the same defect and this test would still catch it."""
    from services.billing_service import _clinical_candidates

    pharmacist = _mk_user("pharmacist")
    pid = _mk_admitted_patient()
    oid = _order_at("dispensed", pid, pharmacist)

    with db_session() as db:
        candidates = _clinical_candidates(db, patient_id=pid)
    assert any(c["source"] == "pharmacy" and c["source_ref"] == oid for c in candidates), \
        "a dispensed drug must reach billing as a charge candidate"


def test_stock_movement_is_recorded_as_a_real_dispense():
    """The movement row used to say reason='mock_dispense' and flag itself
    is_mock=1, while decrementing a real lot FIFO by expiry."""
    pharmacist = _mk_user("pharmacist")
    pid = _mk_admitted_patient()
    oid = _order_at("dispensed", pid, pharmacist)

    with db_session() as db:
        mv = db.execute(
            "SELECT * FROM helix_stock_movements WHERE order_id = ?", (oid,)).fetchone()
    assert mv is not None, "dispensing decrements stock and records the movement"
    assert mv["reason"] == "dispense"
    assert mv["quantity_change"] == -1


def test_awaiting_receipt_times_carry_their_utc_offset():
    """The dispatch time is stored by CURRENT_TIMESTAMP as a bare UTC string.
    Sent as is, the browser read it as local time and the ward saw a 06:51 PM
    dispense as 01:21 PM. The queue now spells it with '+00:00'."""
    pharmacist = _mk_user("pharmacist")
    pid = _mk_admitted_patient("General Medicine")
    oid = _order_at("dispensed", pid, pharmacist)

    row = next(o for o in helix_service.orders_awaiting_receipt(None) if o["id"] == oid)
    for column in ("dispensed_at", "created_at"):
        assert row[column].endswith("+00:00"), (column, row[column])
        assert "T" in row[column]
