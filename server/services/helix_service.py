"""Orb Hospital OS — Helix Service Layer

Strict state machine, idempotent transitions, and atomic inventory operations.
"""
import uuid
import json
from datetime import datetime
from typing import Callable, Optional
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


class DispenseRefused(ValueError):
    """A dispense the pharmacist shaped (a quantity or a batch, grn-14) that
    the shelf refuses. Carries `code` and the HTTP `status` the route answers
    (pharmacy_stock_service.DISPENSE_REFUSAL_STATUS): 422 for a request that
    can never be met for this order (a batch of another drug, a quantity over
    the site maximum), 409 for one the shelf cannot meet as asked right now.
    Nothing was written."""

    def __init__(self, message: str, code: str, status: int):
        super().__init__(message)
        self.code = code
        self.status = status


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

#: helix_medication_master.identity_status of a drug that may be ORDERED or
#: SUGGESTED (grn-6, ruling GRN-RS1). A draft waits for a second pharmacist and
#: a retired drug is out of use. Every ordering and suggestion path in this
#: module reads only these rows; the safety screens (allergy_interlock,
#: patient_specific_screen, drug_safety_service) and the Sage lexicon keep
#: reading every row, so a draft brand is screened by its molecule from the
#: moment it is saved. With the pharmacy store never switched on, every row is
#: 'active' (migration 54's default), so nothing changes there.
ORDERABLE_IDENTITY = "active"


def _identity_refusal(med_name: str, status: str) -> str:
    if status == "draft":
        return (f"{med_name} is waiting for a second pharmacist to confirm it, so it cannot "
                "be ordered yet.")
    return f"{med_name} has been retired from the formulary and cannot be ordered."


def get_medication_catalog(*, orderable_only: bool = False):
    """Retrieve the medications in the Helix master catalog.

    Every row by default, each carrying its identity_status: the procurement
    drug-boundary screen must see drafts and retired drugs too (or a draft's
    name could enter the stores item master), and the board looks up the form
    of a drug on an existing order, which may since have been retired. A
    caller choosing a drug to ORDER or SUGGEST passes orderable_only=True, and
    the substitute and order writers below refuse any other row whatever a
    screen offered."""
    with db_session() as conn:
        if orderable_only:
            rows = conn.execute("SELECT * FROM helix_medication_master "
                                "WHERE identity_status = ?", (ORDERABLE_IDENTITY,)).fetchall()
        else:
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

def _with_linked_orders(conn, rows: list[dict]) -> list[dict]:
    """Add `linked_order` to each supply row LINKED to a medication order
    (medorders-5b): the order's id, status, version and who verified it and
    when, so the pharmacy screen can say "Order verified by X at T" and treat
    its own verification as a supply step. A row with no link is returned
    exactly as before (no new key), and so is every row on a database that
    never used the order store: nothing is read unless a link exists."""
    ids = sorted({r["med_order_id"] for r in rows if r.get("med_order_id")})
    if not ids:
        return rows
    marks = ", ".join("?" for _ in ids)
    # needs_verification (medorders-5c): whether the order still waits for a
    # pharmacist's own check, so the screen can say so in words.
    found = {
        o["id"]: {**dict(o), "needs_verification": bool(o["needs_verification"])}
        for o in conn.execute(
            f"SELECT mo.id, mo.status, mo.version, mo.verified_by, mo.verified_at, "
            f"mo.needs_verification, u.name AS verified_by_name FROM med_orders mo "
            f"LEFT JOIN users u ON u.id = mo.verified_by WHERE mo.id IN ({marks})",
            tuple(ids),
        ).fetchall()
    }
    for r in rows:
        if r.get("med_order_id"):
            r["linked_order"] = found.get(r["med_order_id"])
    return rows


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
        return _with_linked_orders(conn, [dict(row) for row in rows])

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
        return _with_linked_orders(conn, [dict(row) for row in rows])

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
        return _with_linked_orders(conn, [dict(row)])[0] if row else None

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


#: The medication-order events the pharmacy screen's timeline shows for a
#: linked supply row (medorders-5c): the order's own life and its supply.
#: Bedside events ('administered', 'not_given') are left out: they are the
#: MAR, which the pharmacy desk does not read, and this screen must not widen
#: who reads it.
TIMELINE_ORDER_EVENT_KINDS = (
    "created", "signed", "verified", "rejected", "held", "resumed", "stopped",
    "completed", "cancelled", "amended", "adopted",
    "supply_requested", "supply_reviewing", "supply_verified", "supply_rejected",
    "dispensed", "received",
)


def order_timeline(order_id: str) -> Optional[dict]:
    """The pharmacy screen's history of one supply row (medorders-5c), or
    None for an unknown row. Read-only.

    {order_id, patient_id, transitions, linked_order, order_events}:
    `transitions` is the row's own Helix steps with who took them;
    `linked_order` and `order_events` are filled only for a row LINKED to a
    medication order (its state, who verified it and when, and its ledger,
    oldest first, without the bedside events). An unlinked row reads nothing
    from the order store."""
    with db_session() as conn:
        # A Helix table older than migration 61 has no link column: nothing
        # on it can be linked.
        link = ("med_order_id" if _has_column(conn, "helix_patient_med_orders", "med_order_id")
                else "NULL AS med_order_id")
        row = conn.execute(
            f"SELECT id, patient_id, {link} FROM helix_patient_med_orders WHERE id = ?",
            (order_id,),
        ).fetchone()
        if row is None:
            return None
        transitions = [dict(r) for r in conn.execute(
            """SELECT t.id, t.from_status, t.to_status, t.actor_id, t.created_at,
                      u.name AS actor_name
                 FROM helix_transitions t LEFT JOIN users u ON u.id = t.actor_id
                WHERE t.order_id = ? ORDER BY t.created_at ASC, t.rowid ASC""",
            (order_id,),
        ).fetchall()]
        out = {"order_id": row["id"], "patient_id": row["patient_id"],
               "transitions": [_with_utc_offsets(t, ("created_at",)) for t in transitions],
               "linked_order": None, "order_events": []}
        if not row["med_order_id"]:
            return out
        linked = _with_linked_orders(conn, [{"med_order_id": row["med_order_id"]}])[0]
        out["linked_order"] = linked.get("linked_order")
        marks = ", ".join("?" for _ in TIMELINE_ORDER_EVENT_KINDS)
        events = conn.execute(
            f"""SELECT e.id, e.kind, e.from_status, e.to_status, e.actor_id, e.actor_role,
                       e.reason, e.ref_type, e.ref_id, e.created_at, u.name AS actor_name
                  FROM med_order_events e LEFT JOIN users u ON u.id = e.actor_id
                 WHERE e.med_order_id = ? AND e.kind IN ({marks})
                 ORDER BY e.created_at, e.id""",
            (row["med_order_id"], *TIMELINE_ORDER_EVENT_KINDS),
        ).fetchall()
        out["order_events"] = [dict(e) for e in events]
        return out


def _has_column(conn, table: str, column: str) -> bool:
    return column in {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def dispense_options(order_id: str) -> Optional[dict]:
    """What the pharmacist may choose when dispensing this order (grn-14),
    or None for an unknown order. Read-only; no patient data.

    {order_id, enabled, max_units, medication_id, drug_name, lots}. With the
    pharmacy store module off, `enabled` is False and nothing is listed: a
    dispense is one unit, earliest expiry first. With it on, `max_units` is
    ORB_PHARMACY_MAX_DISPENSE_UNITS and `lots` are the dispensable batches of
    the EFFECTIVE drug (the substitute when one is set), earliest expiry
    first, which is the only drug a batch may be chosen from."""
    import config
    with db_session() as conn:
        order = conn.execute(
            "SELECT id, medication_id, substitute_medication_id "
            "FROM helix_patient_med_orders WHERE id = ?", (order_id,),
        ).fetchone()
        if order is None:
            return None
        effective = order["substitute_medication_id"] or order["medication_id"]
        enabled = config.module_state("pharmacy-store") == "on"
        lots = []
        if enabled:
            lots = [{"id": lot["id"], "lot_number": lot["lot_number"],
                     "expiration_date": lot["expiration_date"],
                     "quantity": int(lot["quantity"] or 0),
                     "store_name": lot["store_name"]}
                    for lot in stock.dispensable_lots(conn, effective,
                                                      today=_expiry_cutoff())]
        return {
            "order_id": order["id"],
            "enabled": enabled,
            "max_units": int(config.PHARMACY_MAX_DISPENSE_UNITS) if enabled else 1,
            "medication_id": effective,
            "drug_name": _drug_name(conn, effective),
            "lots": lots,
        }


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

    Only a confirmed drug (identity_status 'active') is ordered: a draft
    waiting for its second pharmacist, or a retired drug, is refused here
    whatever lane resolved it (grn-6).
    """
    med = conn.execute("SELECT name, identity_status FROM helix_medication_master WHERE id = ?",
                       (medication_id,)).fetchone()
    if not med:
        raise ValueError(f"Medication {medication_id} not found in Helix master catalog.")
    if (med["identity_status"] or ORDERABLE_IDENTITY) != ORDERABLE_IDENTITY:
        raise ValueError(_identity_refusal(med["name"] or medication_id, med["identity_status"]))
    order_id = str(uuid.uuid4())
    conn.execute("""
        INSERT INTO helix_patient_med_orders (id, patient_id, medication_id, dosage, route, frequency, status, prescribed_by)
        VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)
    """, (order_id, patient_id, medication_id, dose, route, frequency, prescribed_by))
    return dict(conn.execute("SELECT * FROM helix_patient_med_orders WHERE id = ?", (order_id,)).fetchone())


def resolve_by_name_in_txn(conn, medication_name: str) -> Optional[dict]:
    """The confirmed formulary row whose name or generic name CONTAINS
    `medication_name` (LIKE '%name%'), on the caller's connection, or None.

    Exactly the lookup create_order has always made, moved here unchanged
    (medorders-8) so the Sage handoff binds its supply row on its own
    transaction to the same product create_order would have chosen.
    Confirmed drugs only (grn-6): a draft or retired row that also matches
    the name is never the one found. It is a NAME MATCH, not an exact binding:
    a name can be contained in several products, and the first SQLite returns
    is taken, as it always was. Exact binding for that lane is medorders-20.
    """
    med = conn.execute(
        "SELECT id FROM helix_medication_master "
        "WHERE (name LIKE ? OR generic_name LIKE ?) AND identity_status = ?",
        (f"%{medication_name}%", f"%{medication_name}%", ORDERABLE_IDENTITY)
    ).fetchone()
    return dict(med) if med else None


def create_order(patient_id: str, medication_name: str, dose: str, route: str, frequency: str, prescribed_by: str):
    """Create a new medication order in the Helix system."""
    with db_session() as conn:
        # Confirmed drugs only (grn-6): a draft or retired row that also
        # matches the name is never the one ordered.
        med = resolve_by_name_in_txn(conn, medication_name)
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
    on_applied: Optional[Callable] = None,
    dispense_quantity_units=None,
    lot_id: str | None = None,
) -> dict:
    """
    Guarded, idempotent state transition engine.

    DISPENSE QUANTITY AND BATCH (grn-14, ruling GRN-R13: the pharmacist enters
    it). With the pharmacy store module on, a 'dispensed' step takes
    `dispense_quantity_units` (default 1, at most ORB_PHARMACY_MAX_DISPENSE_UNITS)
    and an optional `lot_id`. The batch must be dispensable today and a batch
    of the EFFECTIVE drug (the substitute when one is set), checked here under
    the write lock; a batch holding too few units is refused, never split.
    Refusals raise DispenseRefused (422 or 409). With the module off both are
    ignored and a dispense is one unit, earliest expiry first, as before.

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

    `on_applied(conn, order, from_status, to_status, transition_id)` is the
    medication-order store's hook (medorders-5b): called inside this same
    transaction after every write above and before the commit, with the order
    as it now stands. Whatever it raises rolls the whole step back, the stock
    movement and the case-room message included. Not called for a replay
    (nothing was applied). None, the default, changes nothing.
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
            sub = conn.execute(
                "SELECT name, identity_status FROM helix_medication_master WHERE id = ?",
                (substitute_medication_id,),
            ).fetchone()
            if not sub:
                raise ValueError(
                    f"Medication {substitute_medication_id} is not in the Helix formulary."
                )
            # A substitute is an ordering decision: only a confirmed drug
            # (grn-6). A draft or retired row is refused before anything moves.
            if (sub["identity_status"] or ORDERABLE_IDENTITY) != ORDERABLE_IDENTITY:
                raise ValueError(_identity_refusal(sub["name"] or substitute_medication_id,
                                                   sub["identity_status"]))

        # ── Dispense: Atomic Inventory Decrement ──
        stock_movement_id = None
        shaped = None
        if target_status == "dispensed":
            med_id = order["substitute_medication_id"] or order["medication_id"]
            # grn-14: a quantity and a batch, honoured only with the pharmacy
            # store module on (None otherwise: one unit, earliest expiry).
            shaped = _shaped_dispense(dispense_quantity_units, lot_id)
            if shaped is None:
                stock_movement_id = _atomic_dispense(conn, order_id, med_id, actor_id)
            else:
                stock_movement_id = _atomic_dispense(
                    conn, order_id, med_id, actor_id,
                    units=shaped["units"], lot_id=shaped["lot_id"])

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
        transition_payload = {
            "pharmacist_notes": pharmacist_notes,
            "substitute_medication_id": substitute_medication_id,
            "scheduled_at": scheduled_at,
            "stock_movement_id": stock_movement_id,
        }
        if shaped is not None:
            # grn-14: what the pharmacist asked for, beside the movement that
            # records what left the shelf. Absent with the module off.
            transition_payload["dispense_quantity_units"] = shaped["units"]
            transition_payload["lot_id"] = shaped["lot_id"]
        payload_json = json.dumps(transition_payload)
        conn.execute("""
            INSERT INTO helix_transitions (id, order_id, from_status, to_status, idempotency_key, actor_id, payload)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (transition_id, order_id, current_status, target_status, idempotency_key, actor_id, payload_json))

        if stock_movement_id:
            _audit_dispense(conn, order, transition_id, stock_movement_id, actor_id,
                            shaped=shaped)

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

        # The medication-order store's hook, last, inside this transaction:
        # a refusal from it rolls back everything written above.
        if on_applied is not None:
            on_applied(conn, dict(updated_order), current_status, target_status, transition_id)

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
    # A proposed substitute that is not a confirmed drug (grn-6) is refused in
    # words here, before any screen runs, so no override is audited against a
    # suggestion the state machine would then refuse.
    if target_status == "substitute_suggested" and substitute_medication_id:
        with db_session() as conn:
            sub = conn.execute(
                "SELECT name, identity_status FROM helix_medication_master WHERE id = ?",
                (substitute_medication_id,),
            ).fetchone()
        if sub and (sub["identity_status"] or ORDERABLE_IDENTITY) != ORDERABLE_IDENTITY:
            raise ValueError(_identity_refusal(sub["name"] or substitute_medication_id,
                                               sub["identity_status"]))
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

    A supply row LINKED to a medication order also carries `linked_order`
    (_with_linked_orders: the order's id, status, version and who verified
    it), so the ward can see which order the tray belongs to and whether it is
    still live (medorders-10, W9-D). An unlinked row, and every row on a
    database that never used the order store, is returned exactly as before.
    """
    with db_session() as conn:
        params: list = []
        dept_clause = ""
        if department:
            dept_clause = " AND a.department = ?"
            params.append(department)

        rows = conn.execute(
            f"""SELECT o.id, o.patient_id, o.dosage, o.route, o.frequency,
                       o.status, o.pharmacist_notes, o.created_at, o.med_order_id,
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
        out = _with_linked_orders(
            conn, [_with_utc_offsets(dict(r), ("created_at", "dispensed_at")) for r in rows])
        for row in out:
            if not row.get("med_order_id"):
                row.pop("med_order_id", None)
        return out


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


def receive_order(order_id: str, actor_id: str, idempotency_key: str | None = None,
                  on_applied: Optional[Callable] = None) -> dict:
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

    `on_applied` is the same medication-order store hook as on
    `transition_order_status`: called in this transaction before its commit,
    with the order as it now stands; whatever it raises rolls the receipt back.
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

        # The medication-order store's hook, before the commit: a refusal
        # from it rolls the receipt back.
        if on_applied is not None:
            applied = conn.execute(
                "SELECT * FROM helix_patient_med_orders WHERE id = ?", (order_id,)
            ).fetchone()
            on_applied(conn, dict(applied), current_status, "delivered", transition_id)

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


def _shaped_dispense(dispense_quantity_units, lot_id) -> Optional[dict]:
    """The pharmacist's quantity and batch for a dispense (grn-14), checked,
    or None when the dispense is the default one: the pharmacy store module is
    off (both fields are then IGNORED, so the dispense is exactly as before),
    or neither field was sent.

    The quantity is a whole number from 1 to ORB_PHARMACY_MAX_DISPENSE_UNITS
    (ruling GRN-R13: pharmacist-entered and bounded; the default maximum is 1,
    which is what a dispense has always moved). Raises DispenseRefused (422)
    for anything else."""
    import config
    if config.module_state("pharmacy-store") != "on":
        return None
    if dispense_quantity_units is None and not lot_id:
        return None
    maximum = int(config.PHARMACY_MAX_DISPENSE_UNITS)
    units = 1 if dispense_quantity_units is None else dispense_quantity_units
    if isinstance(units, bool) or not isinstance(units, int):
        raise DispenseRefused("Enter the number of units to dispense as a whole number.",
                              "bad_quantity", stock.DISPENSE_REFUSAL_STATUS["bad_quantity"])
    if units < 1 or units > maximum:
        most = "1 unit" if maximum == 1 else f"{maximum} units"
        raise DispenseRefused(
            f"Dispense between 1 and {maximum} units at a time. This hospital allows at most "
            f"{most} in one dispense.",
            "quantity_out_of_range", 422)
    if lot_id is not None and (not isinstance(lot_id, str) or not lot_id.strip()):
        raise DispenseRefused("Choose a batch from the list, or leave it to the earliest expiry.",
                              "lot_not_found", stock.DISPENSE_REFUSAL_STATUS["lot_not_found"])
    return {"units": units, "lot_id": lot_id.strip() if lot_id else None,
            "lot_choice": "pharmacist" if lot_id else "earliest_expiry",
            "max_units": maximum}


def _atomic_dispense(conn, order_id: str, medication_id: str, actor_id: str, *,
                     units: int = 1, lot_id: str | None = None) -> str:
    """
    Take one unit for a dispense, earliest expiry first, from a dispensable lot
    only (in date, available, on the dispensing shelf). Runs inside the
    caller's transaction and writes through pharmacy_stock_service, the only
    writer of drug stock: a relative, guarded decrement plus its ledger row.

    A shaped dispense (grn-14, module on) takes `units` from ONE lot: the
    pharmacist's `lot_id`, checked to be a dispensable batch of
    `medication_id` (the effective drug) holding enough units, or else the
    earliest-expiry lot holding them all. Never split between lots.

    Returns: stock_movement_id
    Raises: ValueError if there is no dispensable stock; DispenseRefused for a
    shaped dispense the shelf refuses.
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
    if units == 1 and lot_id is None:
        lot = stock.pick_fefo_lot(conn, medication_id, 1, today=today)
    else:
        try:
            lot = stock.choose_dispense_lot(conn, medication_id, units, lot_id=lot_id,
                                            today=today)
        except stock.StockError as e:
            status = stock.DISPENSE_REFUSAL_STATUS.get(e.code)
            if status is None:
                raise
            raise DispenseRefused(str(e), e.code, status) from None
        if not lot and units > 1:
            # No single batch holds them all. Never split: say what there is.
            drug = _drug_name(conn, medication_id)
            total = stock.dispensable_units(conn, medication_id, today=today)
            if total >= units:
                raise DispenseRefused(
                    f"No single batch of {drug} holds {units} units ({total} in stock across "
                    f"batches). A dispense is never split between batches: dispense fewer "
                    f"units, or choose a batch.", "insufficient_lot_stock",
                    stock.DISPENSE_REFUSAL_STATUS["insufficient_lot_stock"])
            if total > 0:
                raise DispenseRefused(
                    f"Only {total} unit{'' if total == 1 else 's'} of {drug} "
                    f"{'is' if total == 1 else 'are'} in stock; this dispense needs {units}. "
                    f"Dispense fewer units, return to review to substitute, or reject.",
                    "insufficient_lot_stock",
                    stock.DISPENSE_REFUSAL_STATUS["insufficient_lot_stock"])

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

    # One unit (or the pharmacist's bounded quantity, grn-14), a real decrement
    # against a real lot, recorded as a dispense movement. The key makes a
    # second dispense of the same order write nothing: an order leaves the
    # shelf once.
    try:
        movement = stock.post_movement(
            conn, lot_id=lot["id"], movement_type="dispense", quantity_change=-units,
            actor_id=actor_id, order_id=order_id, reference_type="helix_order",
            reference_id=order_id, idempotency_key=f"helix_order:{order_id}:dispense",
        )
    except stock.InsufficientStock:
        # Unreachable under the immediate session (the pick and the decrement
        # hold one write lock); kept so a caller without it still gets words.
        raise ValueError(f"No stock of {_drug_name(conn, medication_id)}. "
                         f"Return to review to substitute, or reject.") from None
    return movement["id"]


def _audit_dispense(conn, order, transition_id: str, movement_id: str, actor_id: str, *,
                    shaped: Optional[dict] = None) -> None:
    """The dispense's own audit row, in the dispense's transaction (grn-4).

    clinical_access is fail-closed: if this row cannot be written,
    log_audit_event raises and the whole dispense rolls back, so no drug leaves
    the shelf unrecorded. Every id is written as a string (an int target_id is
    hashed as a number but stored as TEXT, which breaks the audit chain).

    A dispense the pharmacist shaped (grn-14, module on) also records the
    batch number, who chose the batch ('pharmacist' or 'earliest_expiry'), the
    units asked for and the site maximum. quantity_units and lot_id are read
    from the movement itself, so they say what actually left the shelf."""
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
            **({
                "lot_number": movement["lot_number"] if movement else None,
                "lot_choice": shaped["lot_choice"],
                "requested_units": shaped["units"],
                "max_dispense_units": shaped["max_units"],
            } if shaped is not None else {}),
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

        # Only a confirmed drug is ever suggested (grn-6): a draft waiting for
        # its second pharmacist, or a retired one, is never offered.
        candidates = conn.execute(
            "SELECT * FROM helix_medication_master WHERE id != ? AND identity_status = ? "
            "ORDER BY name",
            (medication_id, ORDERABLE_IDENTITY)
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
