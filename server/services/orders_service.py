"""Orb Hospital OS — Orders / CPOE-lite Service

Computerized provider order entry for labs, imaging, medications, procedures and
nursing orders, with a guarded status machine mirroring Helix's discipline.

Order codes are validated against the local `terminology` subset on write (see
services.terminology_service): unknown LOINC/SNOMED/ICD-10 codes are rejected
rather than silently accepted. `nursing` and `other` orders may be free-text
(no code required).

State machine
─────────────
    draft ──▶ active ──▶ in_progress ──▶ completed
                 └──────────┴──▶ resulted        (lab / imaging only)
    any non-terminal ──▶ cancelled

Terminal statuses: completed, resulted, cancelled.
Every transition writes an `order_events` row. Transitioning to the current
status is an idempotent no-op success.

Laboratory intake (lis-6). When a LAB order becomes 'active' (created active,
or moved draft to active), and only while the laboratory module (ORB_LAB) is
on, the order is handed to services/lab_order_service AFTER the commit that
activated it. The hand-off never raises and never changes what these
functions return: a laboratory failure cannot undo or delay the order. With
ORB_LAB off nothing is imported and nothing else happens.

Orders the laboratory manages (lis-7). Once intake has written
lab_order_items for an order, the laboratory owns its lifecycle FOR LIFE,
whatever ORB_LAB says now: transition_order refuses every move except to
'active' (an idempotent no-op by then) and add_result refuses outright, both
with OrderManagedByLab, which the route answers with 409. The laboratory moves
the order itself through the two conn-taking helpers below
(transition_in_conn, set_lab_result_text_in_conn), which keep the legality
check and the order_events row but not the guard, and which only
services/lab_order_service may call. Without this a ward could complete,
result or cancel an order behind the laboratory's back, and a 'completed'
order is billed (billing_service._COMPLETED_ORDER_STATUSES) before any test
was run. Orders with no laboratory items (a bedside test, an order activated
while the laboratory was off, an unmapped code) keep the free-text path.

Outpatient investigations (opd-11). POST /api/opd/visits/{visit_id}/orders
calls create_order with opd_visit_id: the order carries the visit, and the
visit is re-read inside the order's own transaction (_check_opd_visit), so an
order is never written against a cancelled or finished visit, another
patient's visit, a patient admitted meanwhile, or a test already ordered on
the visit. The orders routes (routes/orders_routes.py) are unchanged and never
pass it.
"""
import json
import logging
import uuid
from datetime import datetime, timezone

from database import db_session
from services import terminology_service

logger = logging.getLogger("orb.orders")


# ══════════════════════════════════════════════════════════════════════════
#  STATE MACHINE
# ══════════════════════════════════════════════════════════════════════════
LEGAL_TRANSITIONS = {
    "draft":        ["active", "cancelled"],
    "active":       ["in_progress", "resulted", "cancelled"],
    "in_progress":  ["completed", "resulted", "cancelled"],
    "completed":    [],   # terminal
    "resulted":     [],   # terminal
    "cancelled":    [],   # terminal
}
TERMINAL_STATUSES = {"completed", "resulted", "cancelled"}

# `resulted` only makes clinical sense for orders that produce a result.
RESULTABLE_ORDER_TYPES = {"lab", "imaging"}

# Order types that do NOT require a coded concept (free-text is acceptable).
CODELESS_ORDER_TYPES = {"nursing", "other"}


class OrderAlreadyResulted(ValueError):
    """A second, different result for an order that already has one.

    A ValueError subclass so callers that only know "bad request" still refuse;
    the route maps it to 409.
    """


#: What a ward sees when it tries to move an order the laboratory manages.
LAB_MANAGED_MESSAGE = (
    "This order is managed by the laboratory. Results arrive from the laboratory; "
    "cancel it from the laboratory request."
)


class OrderManagedByLab(ValueError):
    """A manual status change or free-text result on an order the laboratory
    manages (it has lab_order_items). A ValueError subclass so any caller
    that only knows "bad request" still refuses; the route maps it to 409."""

    def __init__(self, message: str = LAB_MANAGED_MESSAGE):
        super().__init__(message)


class OpdVisitOrderRefused(ValueError):
    """An order tied to an outpatient visit (opd_visit_id) that the visit no
    longer allows (opd-11). Raised inside the order's own transaction, so
    nothing is written. `code` is detail.error, `status` the HTTP status the
    OPD route answers with. A ValueError subclass so a caller that only knows
    "bad request" still refuses."""

    def __init__(self, code: str, message: str, status: int = 409):
        super().__init__(message)
        self.code = code
        self.status = status


def _check_opd_visit(conn, opd_visit_id: str, patient_id: str, *,
                     code_system: str | None, code: str | None) -> None:
    """The visit an outpatient order names, read in the order's own
    transaction: it exists and is this patient's, it was not cancelled or
    finished, the patient has no active admission anywhere (the inpatient
    chart owns their orders then), and the same test is not already ordered
    on it (a second tap of Order is not a second blood draw)."""
    visit = conn.execute(
        "SELECT id, patient_id, status FROM opd_visits WHERE id = ?", (opd_visit_id,)
    ).fetchone()
    if visit is None or visit["patient_id"] != patient_id:
        raise OpdVisitOrderRefused("visit_not_found", "OPD visit not found", status=404)
    if visit["status"] == "cancelled":
        raise OpdVisitOrderRefused("visit_cancelled",
                                   "This visit was cancelled; nothing can be ordered on it.")
    if visit["status"] == "completed":
        raise OpdVisitOrderRefused(
            "visit_completed",
            "This visit is finished. Order from the patient's current visit.")
    if conn.execute(
        "SELECT 1 FROM admissions WHERE patient_id = ? AND status = 'active' LIMIT 1",
        (patient_id,),
    ).fetchone() is not None:
        raise OpdVisitOrderRefused(
            "patient_admitted",
            "This patient is now admitted. Order from the inpatient chart.")
    if code and conn.execute(
        "SELECT 1 FROM orders WHERE opd_visit_id = ? AND upper(code_system) = ? AND code = ? "
        "AND status != 'cancelled' LIMIT 1",
        (opd_visit_id, (code_system or "").upper(), code),
    ).fetchone() is not None:
        raise OpdVisitOrderRefused(
            "already_ordered", "This test is already ordered on this visit.")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def is_lab_managed(conn, order_id: str) -> bool:
    """Does the laboratory manage this order? Keyed on lab_order_items
    existing for it, never on ORB_LAB, so switching the module off can never
    hand a half-processed order back to the free-text path. A database
    without the laboratory tables manages nothing."""
    try:
        return conn.execute(
            "SELECT 1 FROM lab_order_items WHERE order_id = ? LIMIT 1", (order_id,)
        ).fetchone() is not None
    except Exception as exc:  # sqlite3 or sqlcipher3 OperationalError
        if "no such table" in str(exc).lower():
            return False
        raise


def _is_legal_transition(from_status: str, to_status: str) -> bool:
    return to_status in LEGAL_TRANSITIONS.get(from_status, [])


def _record_event(conn, order_id, prior_status, new_status, actor_id, reason):
    conn.execute(
        """INSERT INTO order_events (order_id, prior_status, new_status, actor_id, reason, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (order_id, prior_status, new_status, actor_id, reason, _now()),
    )


def _after_lab_activation(order: dict, actor_id: str | None) -> None:
    """Post-commit: hand a newly active laboratory order to the laboratory
    (lis-6). Off unless ORB_LAB is on; never raises."""
    if order.get("order_type") != "lab" or order.get("status") != "active":
        return
    import config
    if config.LAB_ENABLED is not True:
        return
    try:
        from services import lab_order_service
        lab_order_service.intake_after_activation(order["id"], actor_id=actor_id)
    except Exception:
        logger.exception("Laboratory intake hook failed for order %s; the laboratory's "
                         "sweep will pick it up", order.get("id"))


# ══════════════════════════════════════════════════════════════════════════
#  CREATE
# ══════════════════════════════════════════════════════════════════════════

def create_order(
    patient_id: str,
    order_type: str,
    description: str,
    *,
    code: str | None = None,
    code_system: str | None = None,
    priority: str = "routine",
    ordered_by: str | None = None,
    source: str = "clinician",
    detail: dict | None = None,
    status: str = "draft",
    pre_persist_gate=None,
    opd_visit_id: str | None = None,
    in_transaction=None,
) -> dict:
    """Create a new order (default status 'draft') and write an order_events row.

    Validates any supplied code against `terminology`. Unknown codes are
    rejected. `lab`/`imaging`/`medication`/`procedure` orders require a valid
    code; `nursing`/`other` orders may be free-text (no code).

    `opd_visit_id` (opd-11) ties the order to an outpatient visit
    (orders.opd_visit_id, migration 50). The write then runs in an immediate
    transaction that first re-reads the visit (_check_opd_visit) and raises
    OpdVisitOrderRefused, writing nothing, when the visit is not this
    patient's, was cancelled or finished, the patient is now admitted, or the
    same code is already ordered on it. `in_transaction(conn, order)` runs
    inside the same transaction after the order and its event are written
    (the OPD route's audit row); if it raises, nothing is kept. With both left
    as None the order is written exactly as before.
    """
    if order_type not in {"lab", "imaging", "medication", "procedure", "nursing", "other"}:
        raise ValueError(f"Invalid order_type: {order_type}")
    if priority not in {"stat", "urgent", "routine"}:
        raise ValueError(f"Invalid priority: {priority}")
    # An order may only be CREATED in an initial (non-terminal, pre-progress)
    # state. Allowing creation directly into completed/resulted/cancelled/
    # in_progress would let callers skip required states and forge the audit
    # trail (a lab could be born 'resulted' with no ordering event). The state
    # machine (transition_order) is the only path to later states.
    if status not in {"draft", "active"}:
        raise ValueError(
            f"Orders may only be created in 'draft' or 'active' status, not '{status}'. "
            f"Use transition_order to advance an order."
        )
    if not description or not description.strip():
        raise ValueError("description is required")

    # ── Terminology validation ──
    if code:
        if not code_system:
            raise ValueError("code_system is required when a code is supplied")
        if not terminology_service.validate_code(code_system, code):
            raise ValueError(
                f"Unknown {code_system} code '{code}', not present in terminology. "
                f"Codes must be validated before ordering."
            )
    elif order_type not in CODELESS_ORDER_TYPES:
        raise ValueError(
            f"order_type '{order_type}' requires a coded concept "
            f"(code + code_system). Only {sorted(CODELESS_ORDER_TYPES)} may be free-text."
        )

    # Caller-supplied gate (e.g. the route's medication drug-safety screen)
    # runs after ALL validation and immediately before the write, so a blocked
    # order never persists and an invalid order never triggers a screen.
    if pre_persist_gate is not None:
        pre_persist_gate()

    order_id = str(uuid.uuid4())
    now = _now()
    detail_json = json.dumps(detail) if detail is not None else None

    if opd_visit_id is None and in_transaction is None:
        with db_session() as conn:
            conn.execute(
                """INSERT INTO orders
                   (id, patient_id, order_type, code, code_system, description, priority,
                    status, ordered_by, source, detail_json, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (order_id, patient_id, order_type, code,
                 code_system.upper() if code_system else None,
                 description, priority, status, ordered_by, source, detail_json, now, now),
            )
            _record_event(conn, order_id, None, status, ordered_by, "order created")
            row = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    else:
        with db_session(immediate=True) as conn:
            if opd_visit_id is not None:
                _check_opd_visit(conn, opd_visit_id, patient_id,
                                 code_system=code_system, code=code)
            conn.execute(
                """INSERT INTO orders
                   (id, patient_id, order_type, code, code_system, description, priority,
                    status, ordered_by, source, detail_json, created_at, updated_at,
                    opd_visit_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (order_id, patient_id, order_type, code,
                 code_system.upper() if code_system else None,
                 description, priority, status, ordered_by, source, detail_json, now, now,
                 opd_visit_id),
            )
            _record_event(conn, order_id, None, status, ordered_by, "order created")
            row = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
            if in_transaction is not None:
                in_transaction(conn, dict(row))
    created = dict(row)
    _after_lab_activation(created, ordered_by)
    return created


# ══════════════════════════════════════════════════════════════════════════
#  TRANSITION
# ══════════════════════════════════════════════════════════════════════════

def transition_order(order_id: str, to_status: str, actor_id: str | None = None,
                     detail: str | None = None, *, before_move=None,
                     in_transaction=None) -> dict:
    """Guarded, idempotent state transition.

    Raises ValueError for an unknown order or an illegal transition, and
    OrderManagedByLab for any move other than to 'active' on an order the
    laboratory manages (lis-7), checked before the idempotency rule so a
    stale tab's replay is refused too. Transitioning to the current status is
    otherwise a no-op success (no event written).

    IMMEDIATE (lis-7): the "does the laboratory manage it?" read and the write
    are one transaction, so an order the laboratory takes in at the same
    instant cannot also be moved by hand.

    `before_move(conn, order)` runs in the transaction after the laboratory
    guard and before anything is written, and may raise to refuse (the OPD
    cancel checks the order is its visit's). `in_transaction(conn, prior,
    moved)` runs after a real move is written, before commit (its audit row),
    and is not called for a no-op. Both None: exactly the old behaviour.
    """
    if to_status not in LEGAL_TRANSITIONS:
        raise ValueError(f"Unknown target status: {to_status}")

    with db_session(immediate=True) as conn:
        order = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
        if not order:
            raise ValueError(f"Order {order_id} not found")

        # ── The laboratory's own orders (lis-7) ──
        if to_status != "active" and is_lab_managed(conn, order_id):
            raise OrderManagedByLab()

        if before_move is not None:
            before_move(conn, dict(order))

        current = order["status"]

        # ── Idempotency: already in the target state ──
        if current == to_status:
            return dict(order)

        # ── Legality ──
        if not _is_legal_transition(current, to_status):
            raise ValueError(
                f"Illegal transition: {current} -> {to_status}. "
                f"Allowed from '{current}': {LEGAL_TRANSITIONS.get(current, [])}"
            )

        # ── `resulted` only for result-producing order types ──
        if to_status == "resulted" and order["order_type"] not in RESULTABLE_ORDER_TYPES:
            raise ValueError(
                f"Cannot set status 'resulted' for order_type "
                f"'{order['order_type']}', only {sorted(RESULTABLE_ORDER_TYPES)} produce results."
            )

        conn.execute(
            "UPDATE orders SET status = ?, updated_at = ? WHERE id = ?",
            (to_status, _now(), order_id),
        )
        _record_event(conn, order_id, current, to_status, actor_id, detail)
        row = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
        if in_transaction is not None:
            in_transaction(conn, dict(order), dict(row))
    moved = dict(row)
    if to_status == "active":
        _after_lab_activation(moved, actor_id)
    return moved


# ══════════════════════════════════════════════════════════════════════════
#  RESULT
# ══════════════════════════════════════════════════════════════════════════

def add_result(order_id: str, result_text: str, actor_id: str | None = None) -> dict:
    """Attach a result to a lab/imaging order and transition it to 'resulted'.

    A result is written ONCE. This used to accept an already-resulted order and
    overwrite result_text and result_at with no order_events row, so a stale tab
    or a direct POST could replace a lab value a clinician had already acted on
    and leave no trace of the original. Now:

      * the same text again is an idempotent no-op (an offline replay of the
        same submission), returning the order untouched;
      * different text raises OrderAlreadyResulted. Orb has no amended-result
        workflow yet, so a correction must be charted, never overwritten.

    An order the laboratory manages (lis-7) raises OrderManagedByLab whatever
    the text: its results arrive from the laboratory.
    """
    if not result_text or not result_text.strip():
        raise ValueError("result_text is required")

    # IMMEDIATE: the "already resulted?" read and the write must be one
    # transaction, or two concurrent results both see 'in_progress' and the
    # second silently replaces the first.
    with db_session(immediate=True) as conn:
        order = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
        if not order:
            raise ValueError(f"Order {order_id} not found")

        # The laboratory reports its own orders (lis-7): no free text, ever.
        if is_lab_managed(conn, order_id):
            raise OrderManagedByLab()

        if order["order_type"] not in RESULTABLE_ORDER_TYPES:
            raise ValueError(
                f"Cannot result order_type '{order['order_type']}', "
                f"only {sorted(RESULTABLE_ORDER_TYPES)} produce results."
            )

        current = order["status"]
        if current == "resulted":
            if (order["result_text"] or "") == result_text:
                return dict(order)
            raise OrderAlreadyResulted(
                "This order already has a result. A result is never overwritten; "
                "chart a correction as a clinical note."
            )
        if not _is_legal_transition(current, "resulted"):
            raise ValueError(
                f"Cannot add result from status '{current}'. "
                f"Order must be 'active' or 'in_progress' first."
            )

        now = _now()
        conn.execute(
            "UPDATE orders SET result_text = ?, result_at = ?, status = 'resulted', updated_at = ? WHERE id = ?",
            (result_text, now, now, order_id),
        )
        _record_event(conn, order_id, current, "resulted", actor_id, "result attached")
        row = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    return dict(row)


# ══════════════════════════════════════════════════════════════════════════
#  THE LABORATORY'S OWN HELPERS (lis-7)
# ══════════════════════════════════════════════════════════════════════════
# Only services/lab_order_service calls these (tests/test_lab_order_guard.py
# fails any other caller). Each takes the caller's open write transaction,
# never opens or commits one, keeps the state machine's legality rules and
# writes the order_events row, and skips the laboratory guard, because the
# laboratory is the one party allowed past it.

def _require_transaction(conn) -> None:
    if not getattr(conn, "in_transaction", False):
        raise RuntimeError(
            "The laboratory's order helpers need the caller's write transaction: open "
            "it with db_session(immediate=True) and pass that connection.")


def transition_in_conn(conn, order_id: str, to_status: str, *, actor_id: str | None = None,
                       detail: str | None = None) -> dict:
    """transition_order for the laboratory, on its own transaction. Same
    legality rules and order_events row; no guard and no activation hook (the
    laboratory never activates an order). Moving to the current status is a
    no-op that writes nothing."""
    _require_transaction(conn)
    if to_status not in LEGAL_TRANSITIONS:
        raise ValueError(f"Unknown target status: {to_status}")
    order = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    if not order:
        raise ValueError(f"Order {order_id} not found")
    current = order["status"]
    if current == to_status:
        return dict(order)
    if not _is_legal_transition(current, to_status):
        raise ValueError(
            f"Illegal transition: {current} -> {to_status}. "
            f"Allowed from '{current}': {LEGAL_TRANSITIONS.get(current, [])}"
        )
    if to_status == "resulted" and order["order_type"] not in RESULTABLE_ORDER_TYPES:
        raise ValueError(
            f"Cannot set status 'resulted' for order_type '{order['order_type']}'.")
    conn.execute("UPDATE orders SET status = ?, updated_at = ? WHERE id = ?",
                 (to_status, _now(), order_id))
    _record_event(conn, order_id, current, to_status, actor_id, detail)
    return dict(conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone())


def set_lab_result_text_in_conn(conn, order_id: str, result_text: str, *,
                                actor_id: str | None = None,
                                detail: str = "reported by the laboratory") -> dict:
    """Mark an order 'resulted' with the laboratory's fixed result text, on
    the laboratory's own transaction. The text names no accession and no
    version, so an amended laboratory result never makes it stale.

    The same text again is a no-op; a different text already on the order
    (a free-text result written before the guard existed) raises
    OrderAlreadyResulted, exactly as add_result would."""
    _require_transaction(conn)
    if not result_text or not result_text.strip():
        raise ValueError("result_text is required")
    order = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    if not order:
        raise ValueError(f"Order {order_id} not found")
    if order["order_type"] not in RESULTABLE_ORDER_TYPES:
        raise ValueError(f"Cannot result order_type '{order['order_type']}'.")
    current = order["status"]
    if current == "resulted":
        if (order["result_text"] or "") == result_text:
            return dict(order)
        raise OrderAlreadyResulted("This order already has a different result.")
    if not _is_legal_transition(current, "resulted"):
        raise ValueError(f"Cannot result an order whose status is '{current}'.")
    now = _now()
    conn.execute(
        "UPDATE orders SET result_text = ?, result_at = ?, status = 'resulted', updated_at = ? "
        "WHERE id = ?", (result_text, now, now, order_id))
    _record_event(conn, order_id, current, "resulted", actor_id, detail)
    return dict(conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone())


# ══════════════════════════════════════════════════════════════════════════
#  READ
# ══════════════════════════════════════════════════════════════════════════

def list_orders(patient_id: str, status: str | None = None) -> list[dict]:
    """List a patient's orders, newest first, optionally filtered by status."""
    with db_session() as conn:
        if status:
            rows = conn.execute(
                "SELECT * FROM orders WHERE patient_id = ? AND status = ? ORDER BY created_at DESC",
                (patient_id, status),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM orders WHERE patient_id = ? ORDER BY created_at DESC",
                (patient_id,),
            ).fetchall()
        return [dict(r) for r in rows]


def list_visit_orders(opd_visit_id: str) -> list[dict]:
    """The orders raised from one outpatient visit (opd-11), oldest first, so
    an investigation slip reads in the order the doctor asked."""
    with db_session() as conn:
        rows = conn.execute(
            "SELECT * FROM orders WHERE opd_visit_id = ? ORDER BY created_at ASC, id ASC",
            (opd_visit_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_order(order_id: str) -> dict | None:
    with db_session() as conn:
        row = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
        return dict(row) if row else None


def get_order_events(order_id: str) -> list[dict]:
    """Return the ordered event/audit trail for an order (oldest first)."""
    with db_session() as conn:
        rows = conn.execute(
            "SELECT * FROM order_events WHERE order_id = ? ORDER BY id ASC",
            (order_id,),
        ).fetchall()
        return [dict(r) for r in rows]
