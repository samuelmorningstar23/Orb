"""Orb Hospital OS — Helix Service Layer

Strict state machine, idempotent transitions, and atomic inventory operations.
"""
import uuid
import json
from datetime import datetime
from typing import Optional
from database import db_session
from services import pharmacy_stock_service as stock
from services.clinical_time import to_utc_iso

# ═══════════════════════════════════════════════════════════════════
#  LEGAL STATE MACHINE
# ═══════════════════════════════════════════════════════════════════
LEGAL_TRANSITIONS = {
    "pending":               ["reviewing"],
    "reviewing":             ["verified", "rejected", "substitute_suggested"],
    "substitute_suggested":  ["reviewing"],
    # Back to review or rejected until the drug leaves the shelf: a stockout
    # found at dispense used to be a dead end, with no way to substitute or
    # tell the prescriber.
    "verified":              ["scheduled", "reviewing", "rejected"],
    "scheduled":             ["dispensed", "reviewing", "rejected"],
    "dispensed":             ["delivered"],
    "rejected":              [],      # Terminal
    "delivered":             [],      # Terminal
}

# The words the pharmacy screens use for each status, for refusals a person reads.
STATUS_LABELS = {
    "pending": "Pending",
    "reviewing": "Reviewing",
    "verified": "Verified",
    "rejected": "Rejected",
    "substitute_suggested": "Substitute suggested",
    "scheduled": "Scheduled",
    "dispensed": "Dispensed",
    "delivered": "Delivered",
}


def _is_legal_transition(from_status: str, to_status: str) -> bool:
    return to_status in LEGAL_TRANSITIONS.get(from_status, [])


class StaleOrderState(ValueError):
    """The order is no longer in a state that allows the step asked for,
    almost always because someone moved it on after this screen loaded. The
    route answers 409 with the order as it is now, so the screen can reload it
    instead of printing 'Illegal transition: reviewing -> reviewing'."""

    def __init__(self, message: str, current_status: str):
        super().__init__(message)
        self.current_status = current_status


class TransitionNeedsInput(ValueError):
    """A step the pharmacist cannot take without something only they can
    supply (a reason to reject) or the order lacks (a dose to verify). 422."""


# The pharmacy steps that commit a drug to a patient, each re-screened for
# allergy and interaction against the EFFECTIVE drug (the substitute when one is
# set). Re-screened at every step, not once, because allergies are charted and
# active meds change between review and dispense. Prescribing, eMAR and the
# Verify Queue all screened; Helix was the one lane that swapped a drug unseen.
SCREENED_TARGETS = ("substitute_suggested", "verified", "dispensed")


def _screen_name(med) -> str:
    """The name the drug-safety screen runs on. The formulary name plus its
    generic when the name does not already say it, so a brand ("Monocef 1g
    Injection") is screened as its molecule (ceftriaxone) too: the class and
    allergy matchers work on substrings of whatever they are given."""
    name = (med["name"] or "").strip()
    generic = (med["generic_name"] or "").strip()
    if generic and generic.lower() not in name.lower():
        return f"{name} ({generic})" if name else generic
    return name or generic


def _drug_name(conn, medication_id: str) -> str:
    """The formulary name with its strength when the name does not already say
    it ("Amoxicillin 500 mg"), for messages a person reads. Never the row id."""
    med = conn.execute(
        "SELECT name, strength FROM helix_medication_master WHERE id = ?",
        (medication_id,),
    ).fetchone()
    if not med:
        return "this medication"
    name = (med["name"] or "").strip()
    strength = (med["strength"] or "").strip()
    if strength and _norm(strength) not in _norm(name):
        return f"{name} {strength}".strip()
    return name or "this medication"


def _order_drug_label(conn, medication_id: str, substitute_medication_id: str | None) -> str:
    """The drug that will leave the shelf, saying what it replaced when pharmacy
    substituted it."""
    if substitute_medication_id and substitute_medication_id != medication_id:
        return (f"{_drug_name(conn, substitute_medication_id)} "
                f"(substituted for {_drug_name(conn, medication_id)})")
    return _drug_name(conn, medication_id)


class SelfReceiptRefused(Exception):
    """One account tried to both dispense an order and receive it.

    Its own type, not a ValueError, so the route answers 403 (a policy refusal
    naming a control) instead of the 400 that every malformed transition gets.
    A separation-of-duties breach that reads as "bad request" in the logs is a
    breach nobody reviews."""


# ═══════════════════════════════════════════════════════════════════
#  READ OPERATIONS
# ═══════════════════════════════════════════════════════════════════

def get_medication_catalog():
    """Retrieve all medications in the Helix master catalog."""
    with db_session() as conn:
        rows = conn.execute("SELECT * FROM helix_medication_master").fetchall()
        return [dict(row) for row in rows]

def get_inventory():
    """The lots on the dispensing shelf, across all medications.

    `in_date` is the dispense rule's own date verdict on each lot (a recorded
    expiry on or after the hospital's date, pharmacy_stock_service.site_today),
    so a screen that sums "available" stock counts exactly what a dispense
    could take and never re-derives expiry on its own clock. An empty lot keeps
    its date verdict, so it reads as empty, not as expired.

    Only shelf lots are listed (pharmacy_stock_service.shelf_lot_sql): legacy
    lots, the dispensing store's, and an NDPS vault's for a narcotic or
    psychotropic, each with status available. A quarantined lot, or one in a
    sub-store or returns quarantine, is not stock this screen may count, and
    listing it here would have the formulary call it expired. With the
    pharmacy store module off every lot is a legacy lot, so the list is the
    whole inventory exactly as before.
    """
    with db_session() as conn:
        rows = conn.execute(f"""
            SELECT l.*, m.name, m.strength,
                   CASE WHEN date(l.expiration_date) >= ? THEN 1 ELSE 0 END AS in_date
            FROM helix_inventory_lots l
            JOIN helix_medication_master m ON l.medication_id = m.id
            WHERE {stock.shelf_lot_sql('l')}
        """, (_expiry_cutoff(),)).fetchall()
        return [dict(row) for row in rows]

def get_pending_orders():
    """Retrieve pending patient medication orders."""
    with db_session() as conn:
        rows = conn.execute("""
            SELECT o.*, m.name as medication_name, p.name as patient_name,
                   u.name as prescribed_by_name, p.ward, p.bed
            FROM helix_patient_med_orders o
            JOIN helix_medication_master m ON o.medication_id = m.id
            JOIN patients p ON o.patient_id = p.id
            LEFT JOIN users u ON o.prescribed_by = u.id
            WHERE o.status = 'pending'
            ORDER BY o.created_at DESC
        """).fetchall()
        return [dict(row) for row in rows]

def get_all_orders():
    """Retrieve all orders across all statuses."""
    with db_session() as conn:
        rows = conn.execute("""
            SELECT o.*, m.name as medication_name, p.name as patient_name,
                   u.name as prescribed_by_name, p.ward, p.bed,
                   sm.name as substitute_name
            FROM helix_patient_med_orders o
            JOIN helix_medication_master m ON o.medication_id = m.id
            JOIN patients p ON o.patient_id = p.id
            LEFT JOIN users u ON o.prescribed_by = u.id
            LEFT JOIN helix_medication_master sm ON o.substitute_medication_id = sm.id
            ORDER BY o.created_at DESC
        """).fetchall()
        return [dict(row) for row in rows]

def get_order(order_id: str) -> Optional[dict]:
    """One order in the same shape as get_all_orders, so a screen holding a
    stale copy can swap in the current one."""
    with db_session() as conn:
        row = conn.execute("""
            SELECT o.*, m.name as medication_name, p.name as patient_name,
                   u.name as prescribed_by_name, p.ward, p.bed,
                   sm.name as substitute_name
            FROM helix_patient_med_orders o
            JOIN helix_medication_master m ON o.medication_id = m.id
            JOIN patients p ON o.patient_id = p.id
            LEFT JOIN users u ON o.prescribed_by = u.id
            LEFT JOIN helix_medication_master sm ON o.substitute_medication_id = sm.id
            WHERE o.id = ?
        """, (order_id,)).fetchone()
        return dict(row) if row else None

def get_stock_movements():
    """Retrieve all stock movements for audit."""
    with db_session() as conn:
        rows = conn.execute("""
            SELECT sm.*, m.name as medication_name, l.lot_number
            FROM helix_stock_movements sm
            LEFT JOIN helix_medication_master m ON sm.medication_id = m.id
            LEFT JOIN helix_inventory_lots l ON sm.lot_id = l.id
            ORDER BY sm.timestamp DESC
        """).fetchall()
        return [dict(row) for row in rows]

def get_order_transitions(order_id: str):
    """Retrieve transition history for a specific order."""
    with db_session() as conn:
        rows = conn.execute("""
            SELECT t.*, u.name as actor_name
            FROM helix_transitions t
            LEFT JOIN users u ON t.actor_id = u.id
            WHERE t.order_id = ?
            ORDER BY t.created_at ASC
        """, (order_id,)).fetchall()
        return [dict(row) for row in rows]


# ═══════════════════════════════════════════════════════════════════
#  WRITE OPERATIONS — IDEMPOTENT STATE MACHINE
# ═══════════════════════════════════════════════════════════════════

def insert_order(conn, patient_id: str, medication_id: str, dose: str, route: str,
                 frequency: str, prescribed_by: str) -> dict:
    """Write a pending order for a catalogue row the caller has ALREADY resolved,
    on the caller's connection and inside the caller's transaction. Never commits.

    For a caller whose order must stand or fall with its own writes. The agentic
    confirm is one: its order used to be committed here in a session of its own
    before the confirm's conditional state write, so a confirm that lost a race
    to a dismissal answered 409 and left a pending order in the pharmacist queue.
    Taking the id, not a name, also orders exactly the row the caller screened:
    create_order's LIKE lookup can land on a different row whose name merely
    contains this one.
    """
    if not conn.execute("SELECT 1 FROM helix_medication_master WHERE id = ?",
                        (medication_id,)).fetchone():
        raise ValueError(f"Medication {medication_id} not found in Helix master catalog.")
    order_id = str(uuid.uuid4())
    conn.execute("""
        INSERT INTO helix_patient_med_orders (id, patient_id, medication_id, dosage, route, frequency, status, prescribed_by)
        VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)
    """, (order_id, patient_id, medication_id, dose, route, frequency, prescribed_by))
    return dict(conn.execute("SELECT * FROM helix_patient_med_orders WHERE id = ?", (order_id,)).fetchone())


def create_order(patient_id: str, medication_name: str, dose: str, route: str, frequency: str, prescribed_by: str):
    """Create a new medication order in the Helix system."""
    with db_session() as conn:
        med = conn.execute(
            "SELECT id FROM helix_medication_master WHERE name LIKE ? OR generic_name LIKE ?",
            (f"%{medication_name}%", f"%{medication_name}%")
        ).fetchone()
        if not med:
            raise ValueError(f"Medication {medication_name} not found in Helix master catalog.")

        order = insert_order(conn, patient_id, med["id"], dose, route, frequency, prescribed_by)
        conn.commit()
        return order


def transition_order_status(
    order_id: str,
    target_status: str,
    actor_id: str,
    idempotency_key: str | None = None,
    pharmacist_notes: str = "",
    substitute_medication_id: str | None = None,
    scheduled_at: str | None = None,
) -> dict:
    """
    Guarded, idempotent state transition engine.
    
    Returns: {"ok": True, "order": {...}, "transition": {...}, "already_applied": bool}
    Raises: ValueError for illegal transitions or insufficient stock.

    ONE IMMEDIATE TRANSACTION (grn-4). The session takes the write lock before
    its first read, so the status it checked and the lot it chose cannot change
    under it, and the stock decrement, the order update, the transition row, the
    case-room message and the dispense audit commit together or not at all. Two
    pharmacists dispensing the last unit are serialised: the second finds no
    stock. The session commits on exit; nothing here commits on its own (an
    in-body commit would leave the session's COMMIT with no transaction).

    EVERY DISPENSE IS AUDITED in that transaction (clinical_access,
    helix_dispense_recorded, fail-closed). If the audit row cannot be written
    the dispense rolls back: no movement, status unchanged.
    """
    # Generate idempotency key if not provided
    if not idempotency_key:
        idempotency_key = f"{order_id}:{target_status}:{uuid.uuid4().hex[:8]}"

    with db_session(immediate=True) as conn:
        # ── Idempotency Check ──
        existing = conn.execute(
            "SELECT * FROM helix_transitions WHERE idempotency_key = ?",
            (idempotency_key,)
        ).fetchone()
        if existing:
            order = conn.execute("SELECT * FROM helix_patient_med_orders WHERE id = ?", (order_id,)).fetchone()
            return {
                "ok": True,
                "order": dict(order) if order else {},
                "transition": dict(existing),
                "already_applied": True,
            }

        # ── Fetch current order ──
        order = conn.execute("SELECT * FROM helix_patient_med_orders WHERE id = ?", (order_id,)).fetchone()
        if not order:
            raise ValueError(f"Order {order_id} not found.")
        
        current_status = order["status"]

        # ── Receipt is not a pharmacy transition ──
        # `delivered` records that the drug ARRIVED ON THE WARD, which is the
        # ward's attestation and not the pharmacy's. It has its own entry point
        # (`receive_order`), its own permission (`helix.receive`) and its own
        # actor-separation guard. Refused here so a pharmacy caller cannot reach
        # it by naming the status — that is exactly how it used to work, and it
        # let one desk both dispatch and receive.
        if target_status == "delivered":
            raise ValueError(
                "'delivered' is recorded by the ward, not by pharmacy. "
                "Use receive_order() / POST /helix/orders/{id}/receive."
            )

        # ── Legal Transition Check ──
        if not _is_legal_transition(current_status, target_status):
            label = STATUS_LABELS.get(current_status, current_status)
            raise StaleOrderState(
                f"This order is now {label}. It may have been changed since you "
                f"opened it; the current state is shown.",
                current_status,
            )

        # The prescriber is told why (see the case-room message below), so a
        # rejection with no reason tells them nothing they can act on.
        if target_status == "rejected" and not (pharmacist_notes or "").strip():
            raise TransitionNeedsInput("Give a reason for rejecting this order. The prescriber is told.")

        # Verified is the pharmacist's sign-off on what will be given. An order
        # with no dose has nothing to sign off; it goes back to the prescriber.
        if target_status == "verified" and not (order["dosage"] or "").strip():
            raise TransitionNeedsInput(
                "This order has no dose, so it cannot be verified. Reject it and ask "
                "the prescriber to order it again with a dose."
            )

        # A substitute must be a formulary row. Without this an unknown id was
        # stored and the dispense later failed on "insufficient stock" for a
        # drug that does not exist.
        if substitute_medication_id and target_status == "substitute_suggested":
            if not conn.execute(
                "SELECT 1 FROM helix_medication_master WHERE id = ?",
                (substitute_medication_id,),
            ).fetchone():
                raise ValueError(
                    f"Medication {substitute_medication_id} is not in the Helix formulary."
                )

        # ── Dispense: Atomic Inventory Decrement ──
        stock_movement_id = None
        if target_status == "dispensed":
            med_id = order["substitute_medication_id"] or order["medication_id"]
            stock_movement_id = _atomic_dispense(conn, order_id, med_id, actor_id)

        # ── Apply Transition ──
        transition_id = str(uuid.uuid4())
        now = datetime.utcnow().isoformat()

        # Build SET clause dynamically
        updates = ["status = ?", "last_transition_id = ?"]
        params = [target_status, transition_id]

        if target_status in ("verified", "rejected", "substitute_suggested", "reviewing"):
            updates.extend(["reviewed_by = ?", "reviewed_at = ?"])
            params.extend([actor_id, now])
        
        if pharmacist_notes:
            updates.append("pharmacist_notes = ?")
            params.append(pharmacist_notes)
        
        if substitute_medication_id and target_status == "substitute_suggested":
            updates.append("substitute_medication_id = ?")
            params.append(substitute_medication_id)
        
        if scheduled_at and target_status == "scheduled":
            updates.append("scheduled_at = ?")
            params.append(scheduled_at)
        elif current_status == "scheduled" and target_status in ("reviewing", "rejected"):
            # Taken back off the dispense list: a stale time would still read
            # as booked.
            updates.append("scheduled_at = NULL")

        params.append(order_id)
        conn.execute(
            f"UPDATE helix_patient_med_orders SET {', '.join(updates)} WHERE id = ?",
            params
        )

        # ── Record Transition ──
        payload_json = json.dumps({
            "pharmacist_notes": pharmacist_notes,
            "substitute_medication_id": substitute_medication_id,
            "scheduled_at": scheduled_at,
            "stock_movement_id": stock_movement_id,
        })
        conn.execute("""
            INSERT INTO helix_transitions (id, order_id, from_status, to_status, idempotency_key, actor_id, payload)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (transition_id, order_id, current_status, target_status, idempotency_key, actor_id, payload_json))

        if stock_movement_id:
            _audit_dispense(conn, order, transition_id, stock_movement_id, actor_id)

        # ── Close the loop in Relay: tell the case room what pharmacy decided ──
        # Verified, rejected and a suggested substitute each change what the
        # patient will get, so the prescriber hears about all three. Only the
        # verification used to be posted, and a rejection reached nobody.
        sys_msg_content = None
        if target_status == "verified":
            # Name the drug that will actually be dispensed. With a substitute
            # on the order, naming the prescribed drug told the case room the
            # wrong thing was coming.
            med_name = _order_drug_label(conn, order["medication_id"], order["substitute_medication_id"])
            sys_msg_content = f"SYSTEM: Pharmacy has verified the order for {med_name}. Dispensing will commence."
        elif target_status == "rejected":
            med_name = _order_drug_label(conn, order["medication_id"], order["substitute_medication_id"])
            sys_msg_content = (f"SYSTEM: Pharmacy rejected the order for {med_name}. "
                               f"Reason: {pharmacist_notes.strip()}")
        elif target_status == "substitute_suggested" and substitute_medication_id:
            sub_name = _drug_name(conn, substitute_medication_id)
            orig_name = _drug_name(conn, order["medication_id"])
            sys_msg_content = f"SYSTEM: Pharmacy suggests {sub_name} in place of {orig_name}."
            if (pharmacist_notes or "").strip():
                sys_msg_content += f" Reason: {pharmacist_notes.strip()}"

        if sys_msg_content:
            # Find the active case room for this patient
            channel = conn.execute(
                "SELECT id FROM channels WHERE patient_id = ? AND type = 'case_room' LIMIT 1",
                (order["patient_id"],)
            ).fetchone()

            if channel:
                conn.execute(
                    """INSERT INTO messages (channel_id, sender_id, sender_name, sender_role, content, message_type, is_bot, source)
                       VALUES (?, 'system', 'Orb System', 'system', ?, 'system', 1, 'system')""",
                    (channel["id"], sys_msg_content)
                )

        # Read back inside the block, before the session commits on exit.
        updated_order = conn.execute("SELECT * FROM helix_patient_med_orders WHERE id = ?", (order_id,)).fetchone()
        transition_record = conn.execute("SELECT * FROM helix_transitions WHERE id = ?", (transition_id,)).fetchone()

        return {
            "ok": True,
            "order": dict(updated_order),
            "transition": dict(transition_record),
            "already_applied": False,
        }


def order_screen_target(order_id: str, substitute_medication_id: str | None = None) -> Optional[dict]:
    """The patient and the EFFECTIVE drug an order would put into them.

    The effective drug is, in order: a substitute being proposed right now, the
    substitute already on the order, then the drug as prescribed. That is the
    same precedence `_atomic_dispense` uses to pick the lot, so what is screened
    is what leaves the shelf.

    Returns None for an unknown order. Raises ValueError for a substitute that is
    not in the formulary, which is a malformed request rather than a drug to
    screen.
    """
    with db_session() as conn:
        order = conn.execute(
            """SELECT id, patient_id, status, medication_id, substitute_medication_id, dosage,
                      prescribed_by
                 FROM helix_patient_med_orders WHERE id = ?""",
            (order_id,),
        ).fetchone()
        if not order:
            return None
        med_id = (substitute_medication_id
                  or order["substitute_medication_id"]
                  or order["medication_id"])
        med = conn.execute(
            "SELECT id, name, generic_name FROM helix_medication_master WHERE id = ?",
            (med_id,),
        ).fetchone()
        if not med:
            raise ValueError(f"Medication {med_id} is not in the Helix formulary.")
        return {
            "order_id": order["id"],
            "patient_id": order["patient_id"],
            "status": order["status"],
            "medication_id": med["id"],
            "drug_name": med["name"],
            "screen_name": _screen_name(med),
            "is_substitute": med["id"] != order["medication_id"],
            "has_dose": bool((order["dosage"] or "").strip()),
            "prescribed_by": order["prescribed_by"],
        }


def transition_screen_target(
    order_id: str,
    target_status: str,
    substitute_medication_id: str | None = None,
    idempotency_key: str | None = None,
) -> Optional[dict]:
    """The drug-safety screen a pharmacy transition must pass, or None if none.

    None when the target is not a step that commits a drug, when the order is
    unknown or the transition illegal (the state machine refuses those with its
    own error, and an override audited against a step that never happens is
    noise), when verification will be refused for a missing dose (the same
    reason), or when the idempotency key was already applied (a replay writes
    nothing, so it has nothing to screen).
    """
    if target_status not in SCREENED_TARGETS:
        return None
    if idempotency_key:
        with db_session() as conn:
            if conn.execute(
                "SELECT 1 FROM helix_transitions WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone():
                return None
    target = order_screen_target(
        order_id,
        substitute_medication_id if target_status == "substitute_suggested" else None,
    )
    if not target or not _is_legal_transition(target["status"], target_status):
        return None
    # A dose-less order is refused at verification (TransitionNeedsInput), so
    # screening it would audit an override for a step that cannot happen.
    if target_status == "verified" and not target["has_dose"]:
        return None
    return target


def order_patient_id(order_id: str) -> Optional[str]:
    """The patient an order belongs to, or None. For ward-scoping a route before
    it writes anything."""
    with db_session() as conn:
        row = conn.execute(
            "SELECT patient_id FROM helix_patient_med_orders WHERE id = ?",
            (order_id,),
        ).fetchone()
        return row["patient_id"] if row else None


def orders_awaiting_receipt(department: str | None) -> list[dict]:
    """Orders that left the pharmacy and nobody on the ward has signed for yet.

    Scoped by ADMISSION DEPARTMENT, the same way the nurse shift board is
    (`permissions.department_scope` -> `nurse_shift_service._load_board_rows`),
    and never by anything the client can name. A department-less user row reads
    hospital-wide, which is the existing convention for a governance account.

    This is deliberately NOT `get_all_orders()` filtered client-side: a nurse
    holds `helix.receive` and nothing else in the pharmacy family, so the ward
    must never be handed the pharmacy's whole order book to find its own three
    rows. Only what is in flight to this ward, and no inventory at all.

    The drug named is the one that LEFT THE SHELF: the substitute when pharmacy
    set one (the same precedence `_atomic_dispense` uses), with the prescribed
    drug alongside as `original_medication_name`. Naming the prescribed drug had
    the nurse sign for something other than what was in the tray.
    """
    with db_session() as conn:
        params: list = []
        dept_clause = ""
        if department:
            dept_clause = " AND a.department = ?"
            params.append(department)

        rows = conn.execute(
            f"""SELECT o.id, o.patient_id, o.dosage, o.route, o.frequency,
                       o.status, o.pharmacist_notes, o.created_at,
                       p.name AS patient_name, p.bed, p.ward,
                       m.name AS medication_name, m.strength, m.form,
                       CASE WHEN o.substitute_medication_id IS NOT NULL
                                 AND o.substitute_medication_id != o.medication_id
                            THEN om.name END AS original_medication_name,
                       t.actor_id AS dispensed_by,
                       t.created_at AS dispensed_at,
                       u.name AS dispensed_by_name
                  FROM helix_patient_med_orders o
                  JOIN patients p ON p.id = o.patient_id
                  JOIN admissions a ON a.patient_id = p.id AND a.status = 'active'
                  LEFT JOIN helix_medication_master m
                         ON m.id = COALESCE(o.substitute_medication_id, o.medication_id)
                  LEFT JOIN helix_medication_master om ON om.id = o.medication_id
                  LEFT JOIN helix_transitions t
                         ON t.id = o.last_transition_id
                  LEFT JOIN users u ON u.id = t.actor_id
                 WHERE o.status = 'dispensed'{dept_clause}
                 ORDER BY t.created_at ASC NULLS LAST, o.created_at ASC""",
            tuple(params),
        ).fetchall()
        return [_with_utc_offsets(dict(r), ("created_at", "dispensed_at")) for r in rows]


def _with_utc_offsets(row: dict, columns: tuple[str, ...]) -> dict:
    """Spell CURRENT_TIMESTAMP columns as ISO with '+00:00'.

    A bare "2026-10-02 13:21:00" is UTC, but a browser reads it as local time,
    so the ward saw a 06:51 PM dispense as 01:21 PM. A value that will not parse
    is left as it was rather than guessed at.
    """
    for column in columns:
        raw = row.get(column)
        if raw:
            try:
                row[column] = to_utc_iso(raw)
            except (TypeError, ValueError):
                pass
    return row


def receive_order(order_id: str, actor_id: str, idempotency_key: str | None = None) -> dict:
    """The ward signs for a dispensed drug. `dispensed` -> `delivered`.

    THE ONLY WAY TO REACH `delivered`. `transition_order_status` refuses that
    target outright, because until 2026-09-07 the pharmacist who dispensed also
    clicked the button that said the ward had received it. Two steps, one desk,
    and the row recorded neither who took delivery nor when.

    SEPARATION OF DUTIES IS ENFORCED HERE, not only by the permission. The
    permission grant (`helix.receive` to the nurse, deliberately not to the
    pharmacist) is the policy; this check is what holds when an administrator
    later grants both to one account, which is the realistic way the control
    erodes. The person who dispensed cannot be the person who receives.

    Returns the same envelope as `transition_order_status`, including
    `already_applied`, so the two share one client contract.
    """
    if not idempotency_key:
        idempotency_key = f"{order_id}:delivered:{uuid.uuid4().hex[:8]}"

    with db_session() as conn:
        existing = conn.execute(
            "SELECT * FROM helix_transitions WHERE idempotency_key = ?",
            (idempotency_key,),
        ).fetchone()
        if existing:
            order = conn.execute(
                "SELECT * FROM helix_patient_med_orders WHERE id = ?", (order_id,)
            ).fetchone()
            return {
                "ok": True,
                "order": dict(order) if order else {},
                "transition": dict(existing),
                "already_applied": True,
            }

        order = conn.execute(
            "SELECT * FROM helix_patient_med_orders WHERE id = ?", (order_id,)
        ).fetchone()
        if not order:
            raise ValueError(f"Order {order_id} not found.")

        current_status = order["status"]
        if current_status != "dispensed":
            raise ValueError(
                f"Cannot receive an order in status '{current_status}'. "
                "Only a dispensed order is in transit to the ward."
            )

        # WHO SENT IT. Read from the transition log rather than a column, because
        # the log is the record that cannot be overwritten by a later update.
        dispatch = conn.execute(
            """SELECT actor_id FROM helix_transitions
                WHERE order_id = ? AND to_status = 'dispensed'
                ORDER BY created_at DESC LIMIT 1""",
            (order_id,),
        ).fetchone()
        if dispatch and dispatch["actor_id"] == actor_id:
            raise SelfReceiptRefused(
                "The account that dispensed this order cannot also record that "
                "the ward received it. Receipt is the ward's attestation."
            )

        transition_id = str(uuid.uuid4())
        now = datetime.utcnow().isoformat()

        conn.execute(
            """UPDATE helix_patient_med_orders
                  SET status = 'delivered', last_transition_id = ?,
                      received_by = ?, received_at = ?
                WHERE id = ?""",
            (transition_id, actor_id, now, order_id),
        )

        conn.execute(
            """INSERT INTO helix_transitions
                   (id, order_id, from_status, to_status, idempotency_key, actor_id, payload)
               VALUES (?, ?, ?, 'delivered', ?, ?, ?)""",
            (transition_id, order_id, current_status, idempotency_key, actor_id,
             json.dumps({"received_at": now,
                         "dispensed_by": dispatch["actor_id"] if dispatch else None})),
        )

        conn.commit()

        updated_order = conn.execute(
            "SELECT * FROM helix_patient_med_orders WHERE id = ?", (order_id,)
        ).fetchone()
        transition_record = conn.execute(
            "SELECT * FROM helix_transitions WHERE id = ?", (transition_id,)
        ).fetchone()

        return {
            "ok": True,
            "order": dict(updated_order),
            "transition": dict(transition_record),
            "already_applied": False,
        }


def _expiry_cutoff() -> str:
    """Today's date where the hospital is (YYYY-MM-DD): the first date a lot
    must still be good on to count as stock. The rule now lives in
    pharmacy_stock_service.site_today (grn-4), the one stock ledger's clock;
    this name stays because callers and tests know it."""
    return stock.site_today()


def _in_date_stock(conn, medication_id: str) -> int:
    """Units of a drug a dispense could take today: the shared dispensable
    predicate (pharmacy_stock_service.dispensable_lot_sql), so pre-flight, the
    formulary and the dispense can never disagree about what is on the shelf."""
    return stock.dispensable_units(conn, medication_id, today=_expiry_cutoff())


def _atomic_dispense(conn, order_id: str, medication_id: str, actor_id: str) -> str:
    """
    Take one unit for a dispense, earliest expiry first, from a dispensable lot
    only (in date, available, on the dispensing shelf). Runs inside the
    caller's transaction and writes through pharmacy_stock_service, the only
    writer of drug stock: a relative, guarded decrement plus its ledger row.

    Returns: stock_movement_id
    Raises: ValueError if there is no dispensable stock.
    """
    # The transition calls this inside its immediate session. A caller holding
    # a plain legacy-mode connection with no transaction open yet gets one here,
    # taken before the lot is chosen, so the pick and the decrement share one
    # write lock. An autocommit connection is refused by the stock service.
    if not getattr(conn, "in_transaction", False) and conn.isolation_level is not None:
        conn.execute("BEGIN IMMEDIATE")

    # Earliest-expiring lot that is still in date. This used to pick the
    # earliest expiry with no date filter at all, so an expired lot was always
    # the FIRST one dispensed, and a lot with no expiry (NULL sorts first in
    # SQLite) went ahead of everything.
    today = _expiry_cutoff()
    lot = stock.pick_fefo_lot(conn, medication_id, 1, today=today)

    if not lot:
        # Say why when there are units on the shelf that cannot be used, so the
        # pharmacist quarantines them instead of hunting for a stock error.
        # Named by drug, never by row id, and pointing at the two ways on that
        # exist from here (Orb has no restock step to send them to).
        drug = _drug_name(conn, medication_id)
        units = stock.undispensable_shelf_units(conn, medication_id, today=today)
        if units:
            on_hand = ("1 unit on hand is expired or has" if units == 1
                       else f"{units} units on hand are expired or have")
            raise ValueError(
                f"No in-date stock of {drug}: {on_hand} no expiry date recorded and "
                f"cannot be dispensed. Quarantine {'it' if units == 1 else 'them'}, then "
                f"return to review to substitute, or reject."
            )
        raise ValueError(f"No stock of {drug}. Return to review to substitute, or reject.")

    # One unit, a real decrement against a real lot, recorded as a dispense
    # movement. The key makes a second dispense of the same order write
    # nothing: an order leaves the shelf once.
    try:
        movement = stock.post_movement(
            conn, lot_id=lot["id"], movement_type="dispense", quantity_change=-1,
            actor_id=actor_id, order_id=order_id, reference_type="helix_order",
            reference_id=order_id, idempotency_key=f"helix_order:{order_id}:dispense",
        )
    except stock.InsufficientStock:
        # Unreachable under the immediate session (the pick and the decrement
        # hold one write lock); kept so a caller without it still gets words.
        raise ValueError(f"No stock of {_drug_name(conn, medication_id)}. "
                         f"Return to review to substitute, or reject.") from None
    return movement["id"]


def _audit_dispense(conn, order, transition_id: str, movement_id: str, actor_id: str) -> None:
    """The dispense's own audit row, in the dispense's transaction (grn-4).

    clinical_access is fail-closed: if this row cannot be written,
    log_audit_event raises and the whole dispense rolls back, so no drug leaves
    the shelf unrecorded. Every id is written as a string (an int target_id is
    hashed as a number but stored as TEXT, which breaks the audit chain)."""
    from services.audit_service import log_audit_event

    movement = conn.execute(
        "SELECT m.lot_id, m.medication_id, m.quantity_change, m.store_id, l.lot_number "
        "FROM helix_stock_movements m LEFT JOIN helix_inventory_lots l ON l.id = m.lot_id "
        "WHERE m.id = ?", (movement_id,)).fetchone()
    units = -int(movement["quantity_change"]) if movement else 1
    effective = order["substitute_medication_id"] or order["medication_id"]
    log_audit_event(
        correlation_id=str(transition_id),
        event_category="clinical_access",
        action_type="helix_dispense_recorded",
        outcome="success",
        db=conn,
        summary_message=(f"Dispensed {units} unit{'' if units == 1 else 's'} of "
                         f"{_drug_name(conn, effective)} from batch "
                         f"{(movement['lot_number'] if movement else None) or 'not recorded'}"),
        actor_user_id=str(actor_id) if actor_id is not None else None,
        target_type="helix_order",
        target_id=str(order["id"]),
        patient_id=str(order["patient_id"]) if order["patient_id"] is not None else None,
        metadata={
            "order_id": str(order["id"]),
            "transition_id": str(transition_id),
            "movement_id": str(movement_id),
            "lot_id": str(movement["lot_id"]) if movement else None,
            "medication_id": str(effective),
            "prescribed_medication_id": str(order["medication_id"]),
            "substituted": bool(order["substitute_medication_id"]
                                and order["substitute_medication_id"] != order["medication_id"]),
            "quantity_units": units,
            "store_id": (str(movement["store_id"])
                         if movement and movement["store_id"] is not None else None),
        },
    )


def pre_flight_inventory_check(medication_id: str) -> dict:
    """Checks lot stock levels synchronously for a medication.

    Counts dispensable lots only (in date, available, on the dispensing shelf),
    the same rule `_atomic_dispense` applies, so a pre-flight never promises
    stock the dispense would then refuse.

    Returns {"medication_id": str, "in_stock": bool, "total_quantity": int}
    """
    with db_session() as conn:
        total = _in_date_stock(conn, medication_id)
        return {
            "medication_id": medication_id,
            "in_stock": total > 0,
            "total_quantity": total
        }

def _norm(value) -> str:
    """Lower-case with all whitespace removed, so "500 mg" and "500mg" match."""
    return "".join((value or "").lower().split())


def pre_flight_alternative_suggestion(medication_id: str) -> Optional[dict]:
    """An in-stock EQUIVALENT of a drug that is out of stock, or None.

    Equivalent means the same molecule, strength and form: another brand or
    pack of the same thing, nothing else. It is returned as a SUGGESTION for
    the prescriber to accept or not, and no caller may switch an order to it.

    This used to fall back to a hard-coded class map that offered warfarin for
    heparin, sodium chloride or magnesium for potassium, and norepinephrine or
    dopamine for epinephrine, and the agentic confirm route switched the order
    to whatever came back without telling anyone. Choosing a different molecule
    is a prescribing decision. A hospital-approved interchange list, if a site
    wants one, belongs to the pharmacy and therapeutics committee, not to code.
    """
    with db_session() as conn:
        med = conn.execute(
            "SELECT name, generic_name, strength, form FROM helix_medication_master WHERE id = ?",
            (medication_id,)
        ).fetchone()
        if not med:
            return None
        molecule, strength, form = _norm(med["generic_name"]), _norm(med["strength"]), _norm(med["form"])
        # Without all three on the record there is no basis to call anything
        # equivalent, so nothing is suggested.
        if not (molecule and strength and form):
            return None

        candidates = conn.execute(
            "SELECT * FROM helix_medication_master WHERE id != ? ORDER BY name",
            (medication_id,)
        ).fetchall()
        for cand in candidates:
            if (_norm(cand["generic_name"]), _norm(cand["strength"]), _norm(cand["form"])) != (molecule, strength, form):
                continue
            total = _in_date_stock(conn, cand["id"])
            if total > 0:
                cand_dict = dict(cand)
                cand_dict["stock_status"] = {"in_stock": True, "total_quantity": total}
                cand_dict["equivalence"] = "Same molecule, strength and form"
                return cand_dict

        return None
        
        name_lower = med["name"].lower()
        generic_lower = (med["generic_name"] or "").lower()

        # Step 1: Query database for meds sharing exact same generic name but different ID
        candidates = conn.execute(
            "SELECT * FROM helix_medication_master WHERE generic_name = ? AND id != ?",
            (med["generic_name"], medication_id)
        ).fetchall()

        for cand in candidates:
            # Check if candidate has stock
            stock_row = conn.execute(
                "SELECT SUM(quantity) as total FROM helix_inventory_lots WHERE medication_id = ?",
                (cand["id"],)
            ).fetchone()
            total = stock_row["total"] if stock_row and stock_row["total"] is not None else 0
            if total > 0:
                cand_dict = dict(cand)
                cand_dict["stock_status"] = {"in_stock": True, "total_quantity": total}
                return cand_dict

        # Step 2: Query database for clinical SOP alternatives based on name/generic keywords
        sop_keys = []
        for key in SOP_SUBSTITUTIONS:
            if key in name_lower or key in generic_lower:
                sop_keys.extend(SOP_SUBSTITUTIONS[key])

        for key in sop_keys:
            # Search Master Catalog for this alternative drug name
            cands = conn.execute(
                "SELECT * FROM helix_medication_master WHERE name LIKE ? OR generic_name LIKE ?",
                (f"%{key}%", f"%{key}%")
            ).fetchall()
            for cand in cands:
                stock_row = conn.execute(
                    "SELECT SUM(quantity) as total FROM helix_inventory_lots WHERE medication_id = ?",
                    (cand["id"],)
                ).fetchone()
                total = stock_row["total"] if stock_row and stock_row["total"] is not None else 0
                if total > 0:
                    cand_dict = dict(cand)
                    cand_dict["stock_status"] = {"in_stock": True, "total_quantity": total}
                    return cand_dict

        return None
