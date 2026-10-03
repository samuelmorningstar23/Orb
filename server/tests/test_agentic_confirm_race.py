"""An agentic confirm that loses its race must leave NOTHING behind in Helix.

Defect found 2026-10-03 by an independent reviewer: _confirm_agentic_action
committed the Helix medication order (helix_service.create_order opens and
commits its own session) BEFORE _persist_confirmation's conditional state
UPDATE. A dismissal through the transition endpoint (which does not take
_confirm_lock) landing during the confirm's awaits, or a confirm on another
worker, made the conditional UPDATE refuse and the route answer 409, but the
pending order was already in the pharmacist queue: an order for an action a
clinician had dismissed, with no agentic_med_confirm audit row behind it.

The mirror case: the confirm's state write did not bump sage_actions.version,
and version is the transition endpoint's only write guard (its state read is
outside its transaction). A dismissal that read the action just before the
confirm committed therefore still matched on version and overwrote 'confirmed'
with 'dismissed', leaving a dismissed action with a live order.

Fixed the same day: _persist_confirmation claims the action (bumping version),
inserts the order through helix_service.insert_order and writes the audit row
in one transaction, and orders the reconciled catalogue row by id.

The race windows are opened deterministically with monkeypatches, at points
that exist whichever way the order write is structured, so these tests keep
exercising the race after the fix instead of passing vacuously.
"""
import threading
from contextlib import contextmanager
from types import SimpleNamespace


from fastapi import HTTPException

import routes.relay_routes as relay_routes
import services.medication_safety_gate as safety_gate
from database import db_session
# The agentic-order fixture (autouse: doctors with PINs, PAT-ORDER-1, its case
# room, the Helix catalogue and stock) and its helpers, reused so this file
# exercises exactly the setup the confirm tests already pass on.
from tests.test_optimized_agentic_orders import (  # noqa: F401
    _confirm,
    _login,
    _seed_action,
    setup_agentic_order_data,
)

DOSE = {"clinician_pin": "1234", "dose_confirmation": {"dose": "500mg", "frequency": "TID"}}
DISMISSER = {"id": "D-COSIGN-DOC", "name": "Dr. Cosign Co", "role": "doctor"}


def _action(action_id: str) -> dict:
    with db_session() as db:
        return dict(db.execute(
            "SELECT state, version FROM sage_actions WHERE id = ?", (action_id,)
        ).fetchone())


def _orders_for_patient() -> list[dict]:
    with db_session() as db:
        return [dict(r) for r in db.execute(
            "SELECT id, status FROM helix_patient_med_orders WHERE patient_id = 'PAT-ORDER-1'"
        ).fetchall()]


def _confirm_audit_rows(action_id: str) -> int:
    with db_session() as db:
        return db.execute(
            "SELECT COUNT(*) AS c FROM audit_logs WHERE action_type = 'agentic_med_confirm' "
            "AND metadata_json LIKE ?", (f'%"{action_id}"%',)
        ).fetchone()["c"]


def _during_safety_screen(monkeypatch, side_effect):
    """Run side_effect inside the confirm's drug-safety screen: after the
    confirm has read the action as unresolved, before anything is written."""
    real = safety_gate.enforce_medication_safety

    def screen_then_race(*args, **kwargs):
        result = real(*args, **kwargs)
        side_effect()
        return result

    monkeypatch.setattr(safety_gate, "enforce_medication_safety", screen_then_race)


def test_dismissal_during_confirm_leaves_no_helix_order(monkeypatch):
    """The reported defect: dismissed mid-confirm, the confirm 409s, and no
    pending order may be left in the pharmacist queue."""
    token = _login("D-PRIMARY-DOC")
    action_id = _seed_action("amoxcilin")

    def dismiss():
        # Exactly the write the transition endpoint makes (PATCH .../transition).
        relay_routes._transition_action(
            action_id, "case-PAT-ORDER-1",
            SimpleNamespace(state="dismissed", version=_action(action_id)["version"],
                            reason="Not indicated"),
            DISMISSER,
        )

    _during_safety_screen(monkeypatch, dismiss)
    resp = _confirm(token, action_id, DOSE)

    assert resp.status_code == 409, resp.text
    assert _action(action_id)["state"] == "dismissed"
    assert _orders_for_patient() == [], (
        "a 409 confirm left a Helix order in the pharmacist queue for a dismissed action"
    )
    assert _confirm_audit_rows(action_id) == 0


def test_confirm_racing_another_confirm_leaves_one_order(monkeypatch):
    """A second worker (outside this process's _confirm_lock) confirms the same
    action mid-confirm. The loser's 409 must not leave a second, duplicate order."""
    from services.helix_service import create_order

    token = _login("D-PRIMARY-DOC")
    action_id = _seed_action("amoxcilin")

    def other_worker_wins():
        # What a winning confirm on another worker commits: its order and the state.
        create_order(patient_id="PAT-ORDER-1", medication_name="Amoxicillin 500mg Oral Capsule",
                     dose="500mg", route="Oral", frequency="TID", prescribed_by="D-COSIGN-DOC")
        with db_session() as db:
            db.execute("UPDATE sage_actions SET state = 'confirmed', version = version + 1 "
                       "WHERE id = ?", (action_id,))

    _during_safety_screen(monkeypatch, other_worker_wins)
    resp = _confirm(token, action_id, DOSE)

    assert resp.status_code == 409, resp.text
    orders = _orders_for_patient()
    assert len(orders) == 1, f"duplicate Helix orders for one confirmed action: {orders}"


def test_dismissal_that_read_before_a_confirm_commit_cannot_overwrite_it(monkeypatch):
    """The mirror race. A dismissal reads the action as 'suggested', a confirm
    commits in that gap, then the dismissal writes. Its version predicate is
    its only guard, so the confirm must have moved the version."""
    token = _login("D-PRIMARY-DOC")
    action_id = _seed_action("amoxcilin")
    loaded_version = _action(action_id)["version"]
    test_thread = threading.get_ident()
    confirm_responses = []
    real_session = relay_routes.db_session

    class ConfirmAfterRead:
        """Forwards to the real connection; after the dismissal's state read,
        lets the confirm run to completion before the dismissal's UPDATE."""

        def __init__(self, conn):
            self._conn = conn

        def execute(self, sql, *params):
            cur = self._conn.execute(sql, *params)
            if sql.lstrip().startswith("SELECT state, version FROM sage_actions") \
                    and not confirm_responses:
                rows = cur.fetchall()
                confirm_responses.append(_confirm(token, action_id, DOSE))
                return SimpleNamespace(fetchone=lambda: rows[0] if rows else None,
                                       fetchall=lambda: rows)
            return cur

        def __getattr__(self, name):
            return getattr(self._conn, name)

    @contextmanager
    def session_for_dismissal(*args, **kwargs):
        with real_session(*args, **kwargs) as conn:
            # Only the dismissal, which runs on this test thread. The confirm
            # runs in the app's threads and must see the real connection.
            yield ConfirmAfterRead(conn) if threading.get_ident() == test_thread else conn

    monkeypatch.setattr(relay_routes, "db_session", session_for_dismissal)

    try:
        relay_routes._transition_action(
            action_id, "case-PAT-ORDER-1",
            SimpleNamespace(state="dismissed", version=loaded_version, reason="Not indicated"),
            DISMISSER,
        )
        refused_with = None
    except HTTPException as e:
        refused_with = e.status_code

    assert confirm_responses and confirm_responses[0].status_code == 200, (
        confirm_responses[0].text if confirm_responses else "confirm never ran"
    )
    assert _action(action_id)["state"] == "confirmed", (
        "a dismissal that read before the confirm committed overwrote it: "
        "a dismissed action now has a live Helix order"
    )
    assert refused_with == 409


def test_confirm_orders_the_reconciled_row_not_a_name_match(monkeypatch):
    """The order is for the catalogue row the reconciler matched and the safety
    screen checked. It used to be looked up again by name LIKE '%name%', which
    lands on the first row whose name merely CONTAINS the matched one."""
    import services.rxnorm_reconciler as reconciler

    with db_session() as db:
        db.execute(
            "INSERT INTO helix_medication_master (id, name, generic_name, form, strength) "
            "VALUES ('MED-AMOX-SHORT', 'Amoxicillin 500mg Oral', 'Amoxicillin', 'Capsule', '500mg')")
        db.execute(
            "INSERT INTO helix_inventory_lots (id, medication_id, lot_number, quantity, expiration_date) "
            "VALUES ('LOT-AMOX-SHORT', 'MED-AMOX-SHORT', 'LOTSHORT1', 20, '2029-12-31')")
        matched = dict(db.execute(
            "SELECT * FROM helix_medication_master WHERE id = 'MED-AMOX-SHORT'").fetchone())
        # Precondition: the old name lookup picks a DIFFERENT row for this name.
        by_name = db.execute(
            "SELECT id FROM helix_medication_master WHERE name LIKE ? OR generic_name LIKE ?",
            ("%Amoxicillin 500mg Oral%", "%Amoxicillin 500mg Oral%")).fetchone()
    assert by_name["id"] == "MED-AMOX"

    monkeypatch.setattr(reconciler, "reconcile_medication", lambda name: (matched, 1.0))
    token = _login("D-PRIMARY-DOC")
    action_id = _seed_action("amoxicillin 500mg oral")

    resp = _confirm(token, action_id, DOSE)

    assert resp.status_code == 200, resp.text
    with db_session() as db:
        order = db.execute("SELECT medication_id FROM helix_patient_med_orders WHERE id = ?",
                           (resp.json()["order_id"],)).fetchone()
    assert order["medication_id"] == "MED-AMOX-SHORT"
