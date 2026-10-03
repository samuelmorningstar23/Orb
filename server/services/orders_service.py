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
"""
import json
import uuid
from datetime import datetime, timezone

from database import db_session
from services import terminology_service


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


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_legal_transition(from_status: str, to_status: str) -> bool:
    return to_status in LEGAL_TRANSITIONS.get(from_status, [])


def _record_event(conn, order_id, prior_status, new_status, actor_id, reason):
    conn.execute(
        """INSERT INTO order_events (order_id, prior_status, new_status, actor_id, reason, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (order_id, prior_status, new_status, actor_id, reason, _now()),
    )


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
) -> dict:
    """Create a new order (default status 'draft') and write an order_events row.

    Validates any supplied code against `terminology`. Unknown codes are
    rejected. `lab`/`imaging`/`medication`/`procedure` orders require a valid
    code; `nursing`/`other` orders may be free-text (no code).
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
    return dict(row)


# ══════════════════════════════════════════════════════════════════════════
#  TRANSITION
# ══════════════════════════════════════════════════════════════════════════

def transition_order(order_id: str, to_status: str, actor_id: str | None = None,
                     detail: str | None = None) -> dict:
    """Guarded, idempotent state transition.

    Raises ValueError for an unknown order or an illegal transition.
    Transitioning to the current status is a no-op success (no event written).
    """
    if to_status not in LEGAL_TRANSITIONS:
        raise ValueError(f"Unknown target status: {to_status}")

    with db_session() as conn:
        order = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
        if not order:
            raise ValueError(f"Order {order_id} not found")

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
    return dict(row)


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
