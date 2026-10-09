"""Orb, the NDPS register: narcotic and psychotropic drugs (grn-13).

Design: docs/his_design/grn.json, increment grn-13, sections C, D and G.
Reached through routes/pharmacy_store/ndps.py (behind ORB_PHARMACY_STORE) and
through pharmacy_stock_service.post_movement, which calls write_movement_entry
for every movement of a drug classified narcotic or psychotropic while the
register is on.

THE SWITCH. The register writes nothing unless ORB_PHARMACY_NDPS_REGISTER is
on AND ORB_PHARMACY_NDPS_ON_FAILURE names one of the two failure modes below
(ruling GRN-RS7, held for Samuel). config.py refuses to switch the feature on
without the mode and logs an ERROR at boot; register_on() checks both again at
call time, so a test or a site that patches one of them alone gets nothing.
While the register is off, post_movement never calls into this module.

ONE RUNNING BALANCE per drug, store and lane. Each entry is a signed quantity
with the balance after it; the balance never goes below zero (a CHECK on the
table and a refusal here). seq is the entry's number in its drug and store,
allocated as MAX(seq) + 1 under the caller's write lock. The table is
append-only by trigger: a mistake is corrected by a later entry, never by an
edit. Rows carry a lane: 'live' on the prod deployment profile, else
'non_production', so a development reseed clears the practice entries its own
testing wrote and a live entry can never be deleted (migration 54's trigger,
reset_db_and_seed._CONDITIONAL_DELETE_PREDICATES). Live entries are kept
through a patient's erasure (consent_service.ERASURE_RETAINED_TABLES).

WHAT EACH MOVEMENT BECOMES (MOVEMENT_ENTRY). A dispense names its patient, its
order and the prescriber. A receipt names its vendor, a transfer the other
store, a vendor return its vendor. A write-off may be recorded as wastage or
destruction by the lane that writes it (entry_context). Ward consumption and
outpatient sales have no register entry yet, so such a movement of a
controlled drug is refused while the register is on.

THE WITNESS. Wastage, destruction, write-off, adjustment, count variance,
vendor return and any transfer into or out of the NDPS vault are entered only
with a witness: a second active person holding pharmacy_store.ndps who
confirmed with their own signing PIN (verify_witness, run BEFORE the action's
transaction opens because verify_pin may write). The witness reaches the entry
through entry_context or, for a transfer, through
pharmacy_transfer_service.current_transfer_leg(). The witness is never the
person making the entry (a CHECK says so too).

OPENING THE REGISTER. Reconciliation runs from the latest 'opening' entry of a
drug in a store. A register is opened:
  * by itself, at zero, by the first movement into a store that holds none of
    the drug (a new vault receiving its first delivery);
  * when a drug holding stock is classified narcotic or psychotropic
    (open_after_classification, called by pharmacy_master_service in the
    classification's transaction);
  * by a witnessed opening on the NDPS screen (open_register), for stock that
    was already on the shelf when the register was switched on, or after the
    register was off for a while. Until then a movement of that drug in that
    store is refused, because the register cannot vouch for a balance nobody
    counted.
An opening never resets a register that disagrees with the shelf quietly.
Where the register already open for a drug in a store differs from the shelf
or its running balance is broken, the opening (either lane above that writes
over an existing register) first raises a 'reconciliation_mismatch' variance
for every part of that difference the open variances do not record, with its
'security' audit row, in the opening's own transaction. Each reconciliation
variance belongs to the register it was raised against (the drug and store's
latest opening, in its movement_ref), and after every check and opening the
OPEN variances of a register that differs from the shelf record at least its
whole difference, in its direction: the first records the whole difference
(old register balance expected, the shelf found), a later one only what the
open ones do not (_reconciliation_variance). A reconciled variance no longer
counts, so a difference still there when it is closed, or a new one after,
is raised again: a loss can be recorded twice, never left out, and a shortage
is never recorded as a surplus. An opening starts a new register, so a
difference found at the next opening is always recorded by a variance of its
own, however many are still open. A shortfall therefore stays in
reconciliation until a pharmacist closes it, witnessed.

THE TWO FAILURE MODES (GRN-RS7), for a DISPENSE only (every other movement
always blocks, because nothing waits on it at a bedside):
  'block'   the entry is written inside the dispense's transaction; if it
            cannot be written the dispense is refused and nothing changes.
  'record'  the entry is written in a SAVEPOINT; if it cannot be written the
            savepoint rolls back (the register leg only), the dispense commits,
            and an ndps_variances row plus a 'security' audit row are written
            in the same transaction for same-day reconciliation. If the
            variance or its audit row cannot be written either, the dispense is
            refused: a discrepancy is never left unrecorded.
"""
from __future__ import annotations

import contextlib
import contextvars
import csv
import io
import logging
import uuid
from typing import Any, Iterator, Mapping, Optional, Sequence

import config
from database import db_session
from services import pharmacy_stock_service as stock
from services.clinical_time import utc_iso_now

logger = logging.getLogger("orb.ndps")

# ═════════════════════════════════════════════════════════════════════════════
#  Words, enumerations and limits
# ═════════════════════════════════════════════════════════════════════════════

#: ndps_register.entry_type, as migration 54's CHECK spells it.
ENTRY_TYPES: tuple[str, ...] = (
    "opening", "receipt", "dispense", "transfer_in", "transfer_out", "ward_return",
    "vendor_return", "wastage", "destruction", "write_off", "adjustment", "count_variance",
)

ENTRY_LABELS = {
    "opening": "Opening balance",
    "receipt": "Received from a vendor",
    "dispense": "Dispensed to a patient",
    "transfer_in": "Transferred in",
    "transfer_out": "Transferred out",
    "ward_return": "Returned from a ward",
    "vendor_return": "Returned to the vendor",
    "wastage": "Wastage",
    "destruction": "Destroyed",
    "write_off": "Written off",
    "adjustment": "Adjustment",
    "count_variance": "Stock count difference",
}

#: A stock movement type -> the register entry it becomes. A type missing here
#: (consumption, op_sale) has no register entry yet and is refused.
MOVEMENT_ENTRY = {
    # A lot opened by an 'opening' movement brings units into the books
    # without a document: an adjustment, witnessed.
    "opening": "adjustment",
    "receipt": "receipt",
    "dispense": "dispense",
    "transfer_out": "transfer_out",
    "transfer_in": "transfer_in",
    "ward_return": "ward_return",
    "vendor_return": "vendor_return",
    "write_off": "write_off",
    "adjustment": "adjustment",
    "count_variance": "count_variance",
}

#: A write-off may be entered as one of these (entry_context(entry_type=...)).
WRITE_OFF_ENTRIES: tuple[str, ...] = ("write_off", "wastage", "destruction")

#: Entries made only with a witness, besides any transfer touching the vault.
WITNESSED_ENTRIES = frozenset({
    "wastage", "destruction", "write_off", "adjustment", "count_variance", "vendor_return",
})

#: Movements the 'record' failure mode applies to (GRN-RS7 is about the
#: dispense). Every other movement blocks in both modes.
RECORD_MODE_MOVEMENTS = frozenset({"dispense"})

FAILURE_MODES: tuple[str, ...] = ("block", "record")
LANES: tuple[str, ...] = ("live", "non_production")

#: ndps_variances.variance_kind, as migration 54's CHECK spells it.
VARIANCE_KINDS: tuple[str, ...] = (
    "register_write_failed", "negative_balance", "count_mismatch", "reconciliation_mismatch",
)
VARIANCE_LABELS = {
    "register_write_failed": "The register could not record a movement",
    "negative_balance": "The register would have gone below zero",
    "count_mismatch": "Units sent and units received differ",
    "reconciliation_mismatch": "The register and the shelf disagree",
}
VARIANCE_STATUSES: tuple[str, ...] = ("open", "reconciled")

BALANCE_STATUS_LABELS = {
    "matches": "Matches the shelf",
    "differs": "Differs from the shelf",
    "not_opened": "Register not opened",
    "chain_broken": "Running balance broken",
}

#: What the screen calls the lanes. The non-production words are an honesty
#: label: those entries were written on a computer not set up as the
#: hospital's live system.
LANE_LABELS = {
    "live": "Live register",
    "non_production": "Practice entries, not the live register",
}

COUNTERPARTY_TYPES: tuple[str, ...] = ("patient", "vendor", "store", "destruction", "none")

MAX_TEXT = 1000
MAX_PAGE = 200
MAX_EXPORT_ROWS = 50_000
#: Patient ids named on one audit row's metadata.
MAX_AUDITED_PATIENT_IDS = 200

#: Shown instead of a patient the caller's department does not cover.
OUT_OF_SCOPE_PATIENT = "A patient outside your department"

#: The savepoint the 'record' mode wraps the register leg in.
SAVEPOINT = "ndps_register_leg"


class NdpsError(ValueError):
    """A register request the caller must correct, or one the rules refuse.
    `code` maps to HTTP in the route; `detail` carries anything a screen
    needs beyond the message (never patient data)."""

    code = "ndps_error"

    def __init__(self, message: str, *, code: Optional[str] = None,
                 detail: Optional[dict] = None):
        super().__init__(message)
        if code:
            self.code = code
        self.detail = detail or {}


class RegisterWriteError(NdpsError):
    """The register could not record a movement. Raised inside the movement's
    transaction; the stock service turns it into a refusal ('block') or a
    variance ('record', dispense only)."""

    code = "register_write_failed"
    variance_kind = "register_write_failed"


class NegativeBalance(RegisterWriteError):
    """The entry would take the register's balance below zero."""

    code = "negative_balance"
    variance_kind = "negative_balance"


# ═════════════════════════════════════════════════════════════════════════════
#  The switch, the mode and the lane
# ═════════════════════════════════════════════════════════════════════════════

def failure_mode() -> Optional[str]:
    """'block', 'record' or None (ruling GRN-RS7 not set on this site)."""
    mode = getattr(config, "PHARMACY_NDPS_ON_FAILURE", None)
    return mode if mode in FAILURE_MODES else None


def register_on() -> bool:
    """Is the register writing? The feature switch with its prerequisites,
    and a failure mode, both read now."""
    return bool(config.feature_enabled("ndps_register")) and failure_mode() is not None


def current_lane() -> str:
    """'live' on the prod deployment profile, 'non_production' on any other."""
    return "live" if getattr(config, "DEPLOYMENT_PROFILE", "prod") == "prod" else "non_production"


def is_controlled_category(category: Any) -> bool:
    return category in stock.NDPS_CONTROLLED


def is_controlled(conn, medication_id: Any) -> bool:
    """Is this drug classified narcotic or psychotropic now? Unclassified is
    never read as controlled here (nor as 'none' anywhere else)."""
    row = conn.execute("SELECT ndps_category FROM helix_medication_master WHERE id = ?",
                       (medication_id,)).fetchone()
    return bool(row) and is_controlled_category(row["ndps_category"])


# ═════════════════════════════════════════════════════════════════════════════
#  What the lane writing a movement tells the register
# ═════════════════════════════════════════════════════════════════════════════

_CONTEXT: contextvars.ContextVar[Optional[dict]] = contextvars.ContextVar(
    "ndps_entry_context", default=None)


@contextlib.contextmanager
def entry_context(*, witness_id: Optional[str] = None, entry_type: Optional[str] = None,
                  counterparty_type: Optional[str] = None,
                  counterparty_ref: Optional[str] = None,
                  document_ref: Optional[str] = None,
                  remarks: Optional[str] = None) -> Iterator[None]:
    """Around a lane's post_movement calls: the witness that lane verified
    (verify_witness, before its transaction), the entry a write-off is
    (write_off, wastage or destruction), the counterparty and the document.
    Server code only; nothing a request sends reaches here unverified."""
    if entry_type is not None and entry_type not in WRITE_OFF_ENTRIES:
        raise ValueError(f"a lane may only name a write-off entry, not {entry_type!r}")
    if counterparty_type is not None and counterparty_type not in COUNTERPARTY_TYPES:
        raise ValueError(f"unknown counterparty type {counterparty_type!r}")
    token = _CONTEXT.set({"witness_id": witness_id, "entry_type": entry_type,
                          "counterparty_type": counterparty_type,
                          "counterparty_ref": counterparty_ref,
                          "document_ref": document_ref, "remarks": remarks})
    try:
        yield
    finally:
        _CONTEXT.reset(token)


def _context() -> dict:
    return dict(_CONTEXT.get() or {})


def _transfer_leg() -> Optional[dict]:
    from services import pharmacy_transfer_service as transfers

    return transfers.current_transfer_leg()


# ═════════════════════════════════════════════════════════════════════════════
#  Reads on the caller's connection
# ═════════════════════════════════════════════════════════════════════════════

def _last_entry(conn, lane: str, store_id: int, drug_id: str):
    return conn.execute(
        "SELECT * FROM ndps_register WHERE lane = ? AND store_id = ? "
        "AND helix_medication_id = ? ORDER BY seq DESC LIMIT 1",
        (lane, store_id, drug_id)).fetchone()


def _next_seq(conn, store_id: int, drug_id: str) -> int:
    # Across lanes: (store, drug, seq) is unique whatever the lane.
    return int(conn.execute(
        "SELECT COALESCE(MAX(seq), 0) + 1 FROM ndps_register WHERE store_id = ? "
        "AND helix_medication_id = ?", (store_id, drug_id)).fetchone()[0])


def ledger_units(conn, store_id: int, drug_id: str) -> int:
    """Units of the drug on the stock ledger in this store, every status."""
    row = conn.execute(
        "SELECT COALESCE(SUM(COALESCE(quantity, 0)), 0) FROM helix_inventory_lots "
        "WHERE store_id = ? AND medication_id = ?", (store_id, drug_id)).fetchone()
    return int(row[0] or 0)


def _store_kind(conn, store_id: Optional[int]) -> Optional[str]:
    if store_id is None:
        return None
    row = conn.execute("SELECT pharmacy_kind FROM stores WHERE id = ?", (store_id,)).fetchone()
    return row["pharmacy_kind"] if row else None


def _insert_entry(conn, *, lane: str, store_id: int, drug_id: str, entry_type: str,
                  quantity: int, balance_after: int, actor_id: str, occurred_at: str,
                  lot_id: Optional[str] = None, batch_no: Optional[str] = None,
                  counterparty_type: Optional[str] = None,
                  counterparty_ref: Optional[str] = None, patient_id: Optional[str] = None,
                  helix_order_id: Optional[str] = None, prescriber_id: Optional[str] = None,
                  document_ref: Optional[str] = None, movement_ref: Optional[str] = None,
                  witness_id: Optional[str] = None, remarks: Optional[str] = None) -> dict:
    if balance_after < 0:
        raise NegativeBalance("the register's balance would go below zero")
    seq = _next_seq(conn, store_id, drug_id)
    entry_id = conn.execute(
        """INSERT INTO ndps_register (lane, store_id, helix_medication_id, seq, lot_id,
               batch_no, entry_type, quantity_units, balance_after_units, counterparty_type,
               counterparty_ref, patient_id, helix_order_id, prescriber_id, document_ref,
               movement_ref, actor_id, witness_id, occurred_at, remarks)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (lane, store_id, drug_id, seq, lot_id, batch_no, entry_type, quantity, balance_after,
         counterparty_type, counterparty_ref, patient_id, helix_order_id, prescriber_id,
         document_ref, movement_ref, actor_id, witness_id, occurred_at,
         (remarks or None) and str(remarks)[:MAX_TEXT])).lastrowid
    return dict(conn.execute("SELECT * FROM ndps_register WHERE id = ?",
                             (entry_id,)).fetchone())


# ═════════════════════════════════════════════════════════════════════════════
#  The movement hook (called by pharmacy_stock_service.post_movement)
# ═════════════════════════════════════════════════════════════════════════════

def write_movement_entry(conn, movement: Mapping) -> Optional[dict]:
    """Write the register entry for one stock movement of a controlled drug,
    in the movement's transaction. Returns the entry, or None for a drug that
    is not controlled. Raises RegisterWriteError (NegativeBalance for a balance
    below zero) when the entry cannot be written; nothing here commits."""
    drug_id = movement["medication_id"]
    drug = conn.execute("SELECT id, name, ndps_category FROM helix_medication_master "
                        "WHERE id = ?", (drug_id,)).fetchone()
    if drug is None or not is_controlled_category(drug["ndps_category"]):
        return None
    drug_name = drug["name"] or drug_id
    movement_type = movement["movement_type"]
    ctx = _context()
    entry_type = MOVEMENT_ENTRY.get(movement_type)
    if entry_type is None:
        raise RegisterWriteError(
            f"the NDPS register has no entry for a {str(movement_type).replace('_', ' ')} "
            f"movement, so {drug_name} cannot move this way while the register is on")
    if entry_type == "write_off" and ctx.get("entry_type"):
        entry_type = ctx["entry_type"]
    store_id = movement["store_id"]
    if store_id is None:
        raise RegisterWriteError(
            f"this lot of {drug_name} is not in a pharmacy store yet, and the register is "
            "kept per store. Create the dispensing store or the NDPS vault first")
    actor_id = str(movement["actor_id"] or "")
    quantity = int(movement["quantity_change"])
    lot = conn.execute("SELECT id, lot_number, vendor_id FROM helix_inventory_lots WHERE id = ?",
                       (movement["lot_id"],)).fetchone()
    leg = _transfer_leg() if movement_type in ("transfer_in", "transfer_out") else None

    # Who the units went to or came from.
    patient_id = order_id = prescriber_id = None
    counterparty_type = ctx.get("counterparty_type")
    counterparty_ref = ctx.get("counterparty_ref")
    if entry_type == "dispense":
        order_id = movement["order_id"] or movement["reference_id"]
        order = conn.execute(
            "SELECT id, patient_id, prescribed_by FROM helix_patient_med_orders WHERE id = ?",
            (order_id,)).fetchone() if order_id else None
        if order is None or not order["patient_id"]:
            raise RegisterWriteError(
                f"a dispense of {drug_name} is entered with its patient and order, and this "
                "one names no order on file")
        patient_id, prescriber_id = str(order["patient_id"]), order["prescribed_by"]
        order_id = str(order["id"])
        counterparty_type, counterparty_ref = "patient", patient_id
    elif counterparty_type is None:
        if entry_type in ("receipt", "vendor_return"):
            vendor = lot["vendor_id"] if lot is not None else None
            counterparty_type = "vendor" if vendor is not None else "none"
            counterparty_ref = str(vendor) if vendor is not None else None
        elif entry_type in ("transfer_in", "transfer_out"):
            other = None
            if leg:
                other = leg.get("to_store_id") if entry_type == "transfer_out" \
                    else leg.get("from_store_id")
            counterparty_type = "store"
            counterparty_ref = str(other) if other is not None else None
        elif entry_type == "destruction":
            counterparty_type = "destruction"
        else:
            counterparty_type = "none"

    # The witness.
    witness_id = ctx.get("witness_id")
    if leg and not witness_id:
        witness_id = leg.get("witness_id")
    vault_transfer = entry_type in ("transfer_in", "transfer_out") and (
        _store_kind(conn, store_id) == stock.NDPS_VAULT_KIND
        or (leg is not None and stock.NDPS_VAULT_KIND in (
            _store_kind(conn, leg.get("from_store_id")),
            _store_kind(conn, leg.get("to_store_id")))))
    if (entry_type in WITNESSED_ENTRIES or vault_transfer) and not witness_id:
        raise RegisterWriteError(
            f"a {ENTRY_LABELS[entry_type].lower()} entry for {drug_name} needs a witness who "
            "confirmed with their signing PIN")
    if witness_id and str(witness_id) == actor_id:
        raise RegisterWriteError("the witness must be somebody other than the person making "
                                 "the entry")

    lane = current_lane()
    occurred = movement["occurred_at"] or utc_iso_now()
    last = _last_entry(conn, lane, store_id, drug_id)
    if last is None:
        before = ledger_units(conn, store_id, drug_id) - quantity
        if before != 0:
            raise RegisterWriteError(
                f"the NDPS register has not been opened for {drug_name} in this store, which "
                f"held {before} units before this movement. Open the register with a "
                "witnessed count on the NDPS screen first",
                code="register_not_opened")
        last = _insert_entry(
            conn, lane=lane, store_id=store_id, drug_id=drug_id, entry_type="opening",
            quantity=0, balance_after=0, actor_id=actor_id, occurred_at=occurred,
            counterparty_type="none",
            remarks="Register opened at zero by the first movement into this store")
    balance = int(last["balance_after_units"]) + quantity
    if balance < 0:
        raise NegativeBalance(
            f"the register holds {int(last['balance_after_units'])} units of {drug_name} "
            f"in this store, so {abs(quantity)} cannot leave it")
    document_ref = ctx.get("document_ref")
    if document_ref is None and movement["reference_type"]:
        document_ref = f"{movement['reference_type']}:{movement['reference_id'] or ''}"
    return _insert_entry(
        conn, lane=lane, store_id=store_id, drug_id=drug_id, entry_type=entry_type,
        quantity=quantity, balance_after=balance, actor_id=actor_id, occurred_at=occurred,
        lot_id=movement["lot_id"], batch_no=lot["lot_number"] if lot is not None else None,
        counterparty_type=counterparty_type, counterparty_ref=counterparty_ref,
        patient_id=patient_id, helix_order_id=order_id, prescriber_id=prescriber_id,
        document_ref=document_ref, movement_ref=str(movement["id"]),
        witness_id=str(witness_id) if witness_id else None,
        remarks=ctx.get("remarks") or movement["remarks"])


def _audit(conn, action: str, *, summary: str, actor_id: Optional[str],
           actor_role: Optional[str] = None, target_type: str, target_id: Any,
           metadata: Optional[dict] = None, patient_id: Optional[str] = None,
           category: str = "security", outcome: str = "success",
           audit_ctx: Optional[Mapping] = None) -> None:
    """One audit row. With conn, in the caller's transaction: 'security' and
    'phi_access' are fail-closed, so a failed write rolls the action back."""
    from services import audit_service

    ctx = audit_ctx or {}
    audit_service.log_audit_event(
        correlation_id=str(uuid.uuid4()), event_category=category, action_type=action,
        outcome=outcome, db=conn, summary_message=summary,
        actor_user_id=str(actor_id) if actor_id else None, actor_role=actor_role,
        target_type=target_type, target_id=str(target_id) if target_id is not None else None,
        patient_id=patient_id, request_path=ctx.get("request_path"),
        http_method=ctx.get("http_method"), ip_address=ctx.get("ip_address"),
        metadata=metadata or {})


def _insert_variance(conn, *, kind: str, store_id: Optional[int], drug_id: str,
                     raised_by: str, lot_id: Optional[str] = None,
                     helix_order_id: Optional[str] = None, movement_ref: Optional[str] = None,
                     expected: Optional[int] = None, found: Optional[int] = None,
                     detail: Optional[str] = None) -> int:
    variance = (found - expected) if (found is not None and expected is not None) else None
    return int(conn.execute(
        """INSERT INTO ndps_variances (lane, store_id, helix_medication_id, lot_id,
               helix_order_id, movement_ref, variance_kind, expected_units, found_units,
               variance_units, detail, status, raised_by, raised_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)""",
        (current_lane(), store_id, drug_id, lot_id, helix_order_id, movement_ref, kind,
         expected, found, variance, (detail or None) and str(detail)[:MAX_TEXT], raised_by,
         utc_iso_now())).lastrowid)


#: balances() statuses a reconciliation raises a variance for.
MISMATCH_STATUSES = frozenset({"differs", "chain_broken"})


def _register_ref(row: Mapping) -> str:
    """Which register a 'reconciliation_mismatch' variance is about, kept in
    its movement_ref: the drug and store's latest opening when it was raised.
    An opening starts a new register, so variances raised against the one
    before it never count for the new one."""
    return f"ndps_register:opening:{int(row['opening_seq'])}"


def _units_words(units: int) -> str:
    n = abs(int(units))
    words = f"{n} unit{'' if n == 1 else 's'}"
    if units < 0:
        return f"{words} short"
    return f"{words} over" if units > 0 else "no units"


def _reconciliation_variance(conn, row: Mapping, *, raised_by: str,
                             when: Optional[str] = None) -> tuple[Optional[int], bool]:
    """Record, in the caller's transaction, the part of a balances() row's
    difference from the shelf that the OPEN 'reconciliation_mismatch'
    variances of its register do not record: (variance id, raised now).
    (None, False) for a row that matches or was never opened. The caller
    writes the audit row for a variance raised now.

    Only open variances raised against this register (_register_ref: since
    its latest opening) count. A reconciled variance was closed for what was
    found when it was closed, so it never accounts for the difference now:
    once it is closed, a difference still there, or a new one, is raised
    again. That can record a loss that was already closed a second time; it
    never leaves a loss out, and a closed variance never absorbs a later one.

    With D the register's difference (the shelf less the register) and C what
    its open variances record, a variance is raised:
      * whenever none is open, for all of D (so a broken running balance is
        recorded even where the units agree);
      * otherwise only when the open ones do not record all of D in D's
        direction (C above D for a shortage, below it for a surplus), for the
        rest, D less C: expected the register plus C, found the shelf. The
        rest then always has D's sign, so a shortage is never recorded as a
        surplus, nor a surplus as a shortage.
    Where the open variances already record all of D, or more of it (the
    shelf came partly back while they were open), nothing is raised and the
    newest open one is returned: what they record is never netted away. So
    after every check and opening the open variances of a register that
    differs from the shelf record at least its whole difference."""
    if row.get("status") not in MISMATCH_STATUSES or row.get("opening_seq") is None:
        return None, False
    ref = _register_ref(row)
    still_open = conn.execute(
        "SELECT id, variance_units FROM ndps_variances WHERE lane = ? AND store_id = ? AND "
        "helix_medication_id = ? AND variance_kind = 'reconciliation_mismatch' AND "
        "movement_ref = ? AND status = 'open' ORDER BY id",
        (current_lane(), row["store_id"], row["drug_id"], ref)).fetchall()
    register_units, shelf = int(row["register_units"]), int(row["shelf_units"])
    difference = shelf - register_units
    covered = sum(int(r["variance_units"] or 0) for r in still_open)
    rest = difference - covered
    if still_open and not ((difference < 0 and rest < 0) or (difference > 0 and rest > 0)):
        return int(still_open[-1]["id"]), False
    detail = (f"{row['status_label']}: the register holds {register_units} "
              f"units, the shelf {shelf}")
    if still_open:
        detail += (f". Variances still open for this register record "
                   f"{_units_words(covered)}; this one records the rest, {_units_words(rest)}")
    if when:
        detail += f". Raised when {when}"
    variance_id = _insert_variance(
        conn, kind="reconciliation_mismatch", store_id=row["store_id"],
        drug_id=row["drug_id"], raised_by=raised_by, expected=register_units + covered,
        found=shelf, movement_ref=ref, detail=detail)
    return variance_id, True


def _variance_before_opening(conn, row: Mapping, *, raised_by: str, actor_role: Optional[str],
                             opened_by: str, when: str,
                             audit_ctx: Optional[Mapping] = None) -> tuple[Optional[int], bool]:
    """Before an opening writes over a register that disagrees with the shelf:
    the variance recording whatever part of that register's difference its
    open variances do not record, with its 'security' audit row (fail-closed, so
    the opening rolls back if either cannot be written). Returns
    _reconciliation_variance's answer."""
    variance_id, raised = _reconciliation_variance(conn, row, raised_by=raised_by, when=when)
    if raised:
        made = conn.execute("SELECT expected_units, found_units, variance_units FROM "
                            "ndps_variances WHERE id = ?", (variance_id,)).fetchone()
        _audit(conn, "ndps_variance_raised",
               summary=(f"NDPS register opened again over a difference: the register held "
                        f"{row['register_units']} units, the shelf {row['shelf_units']}; "
                        f"a variance was raised for {_units_words(made['variance_units'])}"),
               actor_id=raised_by, actor_role=actor_role, target_type="ndps_variance",
               target_id=variance_id,
               metadata={"variance_id": str(variance_id),
                         "variance_kind": "reconciliation_mismatch",
                         "opened_by": opened_by, "lane": current_lane(),
                         "store_id": str(row["store_id"]),
                         "medication_id": str(row["drug_id"]),
                         "previous_status": row["status"],
                         "register_units": row["register_units"],
                         "shelf_units": row["shelf_units"],
                         "expected_units": made["expected_units"],
                         "found_units": made["found_units"],
                         "variance_units": made["variance_units"]},
               audit_ctx=audit_ctx)
    return variance_id, raised


def record_failure(conn, movement: Mapping, exc: BaseException) -> int:
    """'record' mode: the register leg was rolled back; write the variance and
    its 'security' audit row in the movement's transaction. Returns the
    variance id. Raises (so the movement rolls back) if either cannot be
    written: a discrepancy is never left unrecorded."""
    kind = getattr(exc, "variance_kind", "register_write_failed")
    if kind not in VARIANCE_KINDS:
        kind = "register_write_failed"
    store_id = movement["store_id"]
    drug_id = movement["medication_id"]
    lane = current_lane()
    expected = found = None
    if store_id is not None:
        last = _last_entry(conn, lane, store_id, drug_id)
        found = ledger_units(conn, store_id, drug_id)
        if last is not None:
            expected = int(last["balance_after_units"]) + int(movement["quantity_change"])
    order_id = movement["order_id"] or (movement["reference_id"]
                                        if movement["reference_type"] == "helix_order" else None)
    variance_id = _insert_variance(
        conn, kind=kind, store_id=store_id, drug_id=drug_id,
        raised_by=str(movement["actor_id"] or "system"), lot_id=movement["lot_id"],
        helix_order_id=str(order_id) if order_id else None, movement_ref=str(movement["id"]),
        expected=expected, found=found, detail=str(exc))
    _audit(conn, "ndps_variance_raised",
           summary=(f"NDPS register entry not written for a {movement['movement_type']}; "
                    "the movement went ahead and a variance was raised"),
           actor_id=movement["actor_id"], target_type="ndps_variance", target_id=variance_id,
           metadata={"variance_id": str(variance_id), "variance_kind": kind,
                     "failure_mode": "record", "movement_id": str(movement["id"]),
                     "movement_type": movement["movement_type"],
                     "medication_id": str(drug_id),
                     "store_id": str(store_id) if store_id is not None else None,
                     "order_id": str(order_id) if order_id else None,
                     "reason": str(exc)[:MAX_TEXT]})
    logger.error("NDPS register entry not written for movement %s (%s); variance %s raised",
                 movement["id"], kind, variance_id)
    return variance_id


def open_after_classification(conn, drug_id: str, *, actor_id: str,
                              category: Optional[str] = None) -> list[dict]:
    """A drug holding stock has just been classified narcotic or psychotropic,
    in the caller's transaction: open its register in every store that holds
    it, at what the stock ledger holds there. Nothing while the register is
    off. Returns the opening entries.

    A drug classified controlled AGAIN may still have a register from before
    in a store; where that register differs from the shelf (or its running
    balance is broken) a 'reconciliation_mismatch' variance for the part of
    the difference its open variances do not record, and its 'security' audit row,
    are raised first, in this transaction, so the opening never drops any of
    the difference out of reconciliation."""
    if not register_on():
        return []
    lane = current_lane()
    occurred = utc_iso_now()
    words = (category or "controlled").replace("_", " ")
    # The register as it stands, read before any opening is written.
    before = {int(r["store_id"]): r for r in balances(conn, drug_id=drug_id)["rows"]}
    out: list[dict] = []
    for row in conn.execute(
            "SELECT store_id, COALESCE(SUM(COALESCE(quantity, 0)), 0) AS units "
            "FROM helix_inventory_lots WHERE medication_id = ? AND store_id IS NOT NULL "
            "GROUP BY store_id ORDER BY store_id", (drug_id,)).fetchall():
        units = int(row["units"] or 0)
        if units <= 0 and _last_entry(conn, lane, row["store_id"], drug_id) is None:
            continue
        previous = before.get(int(row["store_id"]))
        if previous is not None:
            _variance_before_opening(
                conn, previous, raised_by=str(actor_id), actor_role=None,
                opened_by="classification",
                when=f"the drug was classified {words} again and its register opened from "
                     "the stock ledger")
        out.append(_insert_entry(
            conn, lane=lane, store_id=int(row["store_id"]), drug_id=drug_id,
            entry_type="opening", quantity=max(units, 0), balance_after=max(units, 0),
            actor_id=str(actor_id), occurred_at=occurred, counterparty_type="none",
            document_ref=f"classification:{drug_id}",
            remarks=f"Register opened when the drug was classified {words}, from the "
                    "stock ledger"))
    return out


def record_transit_shortfalls(conn, *, transfer_id: int, lines: Sequence[Mapping],
                              actor_id: str) -> list[int]:
    """A controlled transfer was received short, in the receipt's transaction:
    one 'count_mismatch' variance per short line of a narcotic or psychotropic
    drug, with its 'security' audit row. Nothing while the register is off.
    Returns the variance ids."""
    if not register_on():
        return []
    row = conn.execute("SELECT to_store_id FROM pharmacy_transfers WHERE id = ?",
                       (transfer_id,)).fetchone()
    to_store = row["to_store_id"] if row else None
    ids: list[int] = []
    for line in lines:
        short = int(line.get("shortfall_units") or 0)
        drug_id = line.get("drug_id")
        if short <= 0 or not drug_id or not is_controlled(conn, drug_id):
            continue
        sent, got = int(line.get("sent_units") or 0), int(line.get("received_units") or 0)
        variance_id = _insert_variance(
            conn, kind="count_mismatch", store_id=to_store, drug_id=drug_id,
            raised_by=str(actor_id), lot_id=line.get("to_lot_id"),
            movement_ref=f"pharmacy_transfer:{transfer_id}:line:{line.get('line_no')}",
            expected=sent, found=got,
            detail=(f"Transfer {transfer_id}, line {line.get('line_no')}: {sent} units sent, "
                    f"{got} received. {line.get('reason') or ''}").strip())
        _audit(conn, "ndps_variance_raised",
               summary=f"Controlled transfer {transfer_id} received {short} units short",
               actor_id=actor_id, target_type="ndps_variance", target_id=variance_id,
               metadata={"variance_id": str(variance_id), "variance_kind": "count_mismatch",
                         "transfer_id": str(transfer_id), "line_no": line.get("line_no"),
                         "medication_id": str(drug_id), "sent_units": sent,
                         "received_units": got})
        ids.append(variance_id)
    return ids


# ═════════════════════════════════════════════════════════════════════════════
#  The witness (before an action's transaction)
# ═════════════════════════════════════════════════════════════════════════════

def _actor_id(actor: Mapping) -> str:
    uid = actor.get("id") if actor else None
    if not uid:
        raise NdpsError("the request has no signed-in person behind it", code="no_actor")
    return str(uid)


def may_ndps(role: Any) -> bool:
    from permissions import PHARMACY_STORE_NDPS, has_permission

    return has_permission(str(role or ""), PHARMACY_STORE_NDPS)


def verify_witness(*, actor: Mapping, witness_id: Any, witness_pin: Any, purpose: str,
                   target_type: str, target_id: Any,
                   audit_ctx: Optional[Mapping] = None) -> str:
    """Confirm a witness BEFORE the action's transaction opens (verify_pin may
    write to upgrade a legacy hash). Returns the witness's user id, or refuses:
    witness_required (422), witness_same_person, witness_not_eligible,
    witness_no_pin, witness_pin_wrong (403), witness_locked (429). A wrong PIN
    counts toward that person's lockout and is audited."""
    from services import pin_security

    me = _actor_id(actor)
    wid = witness_id.strip() if isinstance(witness_id, str) else ""
    pin = witness_pin if isinstance(witness_pin, str) else ""
    if not wid or not pin:
        raise NdpsError(f"{purpose} involves controlled stock, so a second pharmacist "
                        "witnesses it: choose the witness and have them enter their signing PIN.",
                        code="witness_required")
    if wid == me:
        raise NdpsError("the witness must be somebody other than you",
                        code="witness_same_person")
    if pin_security.is_pin_locked(wid):
        raise NdpsError("too many wrong PINs for this witness. PIN entry is locked for 15 "
                        "minutes.", code="witness_locked")
    with db_session() as conn:
        row = conn.execute("SELECT id, name, role, pin_hash, is_active FROM users WHERE id = ?",
                           (wid,)).fetchone()
    if (row is None or (row["is_active"] is not None and not row["is_active"])
            or not may_ndps(row["role"])):
        raise NdpsError("that person cannot witness controlled stock",
                        code="witness_not_eligible")
    if not row["pin_hash"]:
        raise NdpsError("the witness has no signing PIN yet. Ask an administrator to set one.",
                        code="witness_no_pin")
    if not pin_security.verify_pin(row, pin):
        pin_security.record_pin_failure(wid)
        _audit(None, "ndps_witness_refused", summary=f"Witness PIN refused: {purpose}",
               actor_id=me, actor_role=actor.get("role"), target_type=target_type,
               target_id=target_id, outcome="failure",
               metadata={"purpose": purpose, "witness_id": wid}, audit_ctx=audit_ctx)
        raise NdpsError("the witness's PIN is not right. Nothing was changed.",
                        code="witness_pin_wrong")
    pin_security.clear_pin_failures(wid)
    return wid


def witnesses(conn, *, exclude: Optional[str] = None) -> list[dict]:
    """Active people who may witness controlled stock, with a signing PIN,
    never `exclude`. Names only."""
    from permissions import PHARMACY_STORE_NDPS, ROLE_PERMISSIONS

    roles = sorted(r for r, perms in ROLE_PERMISSIONS.items()
                   if isinstance(r, str) and PHARMACY_STORE_NDPS in perms)
    if not roles:
        return []
    marks = ",".join("?" for _ in roles)
    return [{"id": u["id"], "name": u["name"] or u["id"]} for u in conn.execute(
        f"SELECT id, name FROM users WHERE role IN ({marks}) "
        "AND (is_active IS NULL OR is_active = 1) AND id <> ? "
        "AND pin_hash IS NOT NULL AND pin_hash <> '' ORDER BY name, id LIMIT 500",
        (*roles, exclude or "")).fetchall()]


# ═════════════════════════════════════════════════════════════════════════════
#  Reads for the NDPS screen
# ═════════════════════════════════════════════════════════════════════════════

def _names(conn, ids) -> dict[str, str]:
    from services.actor_names import names_for

    return names_for(conn, ids)


def _store_rows(conn) -> dict[int, dict]:
    return {int(r["id"]): dict(r) for r in conn.execute(
        "SELECT id, code, name, pharmacy_kind, is_active FROM stores "
        "WHERE pharmacy_kind IS NOT NULL").fetchall()}


def _drug_rows(conn, ids) -> dict[str, dict]:
    ids = sorted({str(i) for i in ids if i})
    out: dict[str, dict] = {}
    for start in range(0, len(ids), 500):
        chunk = ids[start:start + 500]
        for r in conn.execute(
                "SELECT id, name, generic_name, strength, form, ndps_category, dispense_unit "
                f"FROM helix_medication_master WHERE id IN ({','.join('?' * len(chunk))})",
                chunk).fetchall():
            out[r["id"]] = dict(r)
    return out


def balances(conn=None, *, store_id: Optional[int] = None,
             drug_id: Optional[str] = None) -> dict:
    """The register against the shelf, per drug and store, in this lane.
    Read-only. Reconciliation runs from the latest opening entry: every entry
    after it must carry on the running balance, and the balance must equal
    what the stock ledger holds there. Rows: every store and controlled drug
    with units on the shelf, plus every store and drug with register entries.
    `clean` is True when every row matches."""
    if conn is None:
        with db_session() as fresh:
            return balances(fresh, store_id=store_id, drug_id=drug_id)
    lane = current_lane()
    pairs: set[tuple[int, str]] = set()
    where, params = "", []
    if store_id is not None:
        where += " AND l.store_id = ?"
        params.append(store_id)
    if drug_id:
        where += " AND l.medication_id = ?"
        params.append(drug_id)
    for r in conn.execute(
            "SELECT DISTINCT l.store_id, l.medication_id FROM helix_inventory_lots l "
            "JOIN helix_medication_master m ON m.id = l.medication_id "
            "WHERE l.store_id IS NOT NULL AND COALESCE(l.quantity, 0) <> 0 "
            f"AND m.ndps_category IN ('narcotic', 'psychotropic'){where}", params).fetchall():
        pairs.add((int(r["store_id"]), str(r["medication_id"])))
    rwhere, rparams = "", [lane]
    if store_id is not None:
        rwhere += " AND store_id = ?"
        rparams.append(store_id)
    if drug_id:
        rwhere += " AND helix_medication_id = ?"
        rparams.append(drug_id)
    for r in conn.execute("SELECT DISTINCT store_id, helix_medication_id FROM ndps_register "
                          f"WHERE lane = ?{rwhere}", rparams).fetchall():
        pairs.add((int(r["store_id"]), str(r["helix_medication_id"])))
    unplaced = int(conn.execute(
        "SELECT COALESCE(SUM(COALESCE(l.quantity, 0)), 0) FROM helix_inventory_lots l "
        "JOIN helix_medication_master m ON m.id = l.medication_id WHERE l.store_id IS NULL "
        "AND m.ndps_category IN ('narcotic', 'psychotropic')"
        + (" AND l.medication_id = ?" if drug_id else ""),
        ([drug_id] if drug_id else [])).fetchone()[0] or 0)

    stores = _store_rows(conn)
    drugs = _drug_rows(conn, [d for _, d in pairs])
    open_variances: dict[tuple, int] = {}
    for r in conn.execute("SELECT store_id, helix_medication_id, COUNT(*) AS n FROM ndps_variances "
                          "WHERE lane = ? AND status = 'open' GROUP BY store_id, "
                          "helix_medication_id", (lane,)).fetchall():
        open_variances[(r["store_id"], r["helix_medication_id"])] = int(r["n"])
    rows: list[dict] = []
    for sid, did in sorted(pairs, key=lambda p: (stores.get(p[0], {}).get("name") or "",
                                                 drugs.get(p[1], {}).get("name") or p[1])):
        opening = conn.execute(
            "SELECT * FROM ndps_register WHERE lane = ? AND store_id = ? AND "
            "helix_medication_id = ? AND entry_type = 'opening' ORDER BY seq DESC LIMIT 1",
            (lane, sid, did)).fetchone()
        shelf = ledger_units(conn, sid, did)
        chain_ok = True
        register_balance = None
        entries_since = 0
        if opening is not None:
            running = int(opening["balance_after_units"])
            for e in conn.execute(
                    "SELECT seq, entry_type, quantity_units, balance_after_units FROM "
                    "ndps_register WHERE lane = ? AND store_id = ? AND helix_medication_id = ? "
                    "AND seq > ? ORDER BY seq", (lane, sid, did, opening["seq"])).fetchall():
                entries_since += 1
                if e["entry_type"] == "opening":  # pragma: no cover - latest opening found above
                    running = int(e["balance_after_units"])
                    continue
                running += int(e["quantity_units"])
                if running != int(e["balance_after_units"]):
                    chain_ok = False
                    running = int(e["balance_after_units"])
            register_balance = running
        if opening is None:
            status = "not_opened"
        elif not chain_ok:
            status = "chain_broken"
        elif register_balance != shelf:
            status = "differs"
        else:
            status = "matches"
        store = stores.get(sid, {})
        drug = drugs.get(did, {})
        rows.append({
            "store_id": sid, "store_code": store.get("code"),
            "store_name": store.get("name") or store.get("code") or f"Store {sid}",
            "store_kind": store.get("pharmacy_kind"),
            "drug_id": did, "drug_name": drug.get("name") or did,
            "strength": drug.get("strength"), "form": drug.get("form"),
            "ndps_category": drug.get("ndps_category"),
            "controlled": is_controlled_category(drug.get("ndps_category")),
            "register_units": register_balance, "shelf_units": shelf,
            "difference_units": (shelf - register_balance) if register_balance is not None
            else None,
            "opened_at": opening["occurred_at"] if opening is not None else None,
            "opening_seq": int(opening["seq"]) if opening is not None else None,
            "entries_since_opening": entries_since,
            "status": status, "status_label": BALANCE_STATUS_LABELS[status],
            "open_variances": open_variances.get((sid, did), 0),
        })
    return {"lane": lane, "lane_label": LANE_LABELS[lane], "rows": rows,
            "clean": all(r["status"] == "matches" for r in rows),
            "unplaced_units": unplaced}


def _patient_rows(conn, ids) -> dict[str, dict]:
    ids = sorted({str(i) for i in ids if i})
    out: dict[str, dict] = {}
    for start in range(0, len(ids), 500):
        chunk = ids[start:start + 500]
        for r in conn.execute(f"SELECT id, name, mrn FROM patients WHERE id IN "
                              f"({','.join('?' * len(chunk))})", chunk).fetchall():
            out[r["id"]] = dict(r)
    return out


def _decorate_entries(conn, rows: list[dict], *, dept: Optional[str]) -> tuple[list[dict], list]:
    """Words for each entry. A patient outside the caller's department is
    named only as such. Returns (entries, patient ids disclosed)."""
    from permissions import patient_in_scope

    stores = _store_rows(conn)
    drugs = _drug_rows(conn, [r["helix_medication_id"] for r in rows])
    names = _names(conn, [x for r in rows for x in (r["actor_id"], r["witness_id"],
                                                    r["prescriber_id"])])
    patients = _patient_rows(conn, [r["patient_id"] for r in rows])
    vendors: dict[str, str] = {}
    vendor_ids = {r["counterparty_ref"] for r in rows if r["counterparty_type"] == "vendor"
                  and r["counterparty_ref"]}
    for vid in vendor_ids:
        v = conn.execute("SELECT name, code FROM vendors WHERE id = ?", (vid,)).fetchone()
        if v is not None:
            vendors[vid] = v["name"] or v["code"]
    scope_cache: dict[str, bool] = {}
    disclosed: list[str] = []
    out: list[dict] = []
    for r in rows:
        store = stores.get(int(r["store_id"]), {})
        drug = drugs.get(r["helix_medication_id"], {})
        pid = r["patient_id"]
        patient_name = patient_mrn = None
        in_scope = True
        if pid:
            if pid not in scope_cache:
                scope_cache[pid] = patient_in_scope(conn, pid, dept)
            in_scope = scope_cache[pid]
            if in_scope:
                p = patients.get(pid, {})
                patient_name, patient_mrn = p.get("name"), p.get("mrn")
                if pid not in disclosed:
                    disclosed.append(pid)
        party = r["counterparty_type"]
        if party == "patient":
            party_words = (patient_name or "Patient not on file") if in_scope \
                else OUT_OF_SCOPE_PATIENT
        elif party == "vendor":
            party_words = vendors.get(str(r["counterparty_ref"]), "Vendor not on file")
        elif party == "store":
            other = stores.get(int(r["counterparty_ref"])) if (r["counterparty_ref"] or "").isdigit() \
                else None
            party_words = (other or {}).get("name") or "Another store"
        elif party == "destruction":
            party_words = "Destroyed"
        else:
            party_words = ""
        out.append({
            "id": r["id"], "seq": r["seq"], "lane": r["lane"],
            "lane_label": LANE_LABELS.get(r["lane"], r["lane"]),
            "store_id": r["store_id"], "store_name": store.get("name") or store.get("code"),
            "drug_id": r["helix_medication_id"],
            "drug_name": drug.get("name") or r["helix_medication_id"],
            "strength": drug.get("strength"), "form": drug.get("form"),
            "lot_id": r["lot_id"], "batch_no": r["batch_no"],
            "entry_type": r["entry_type"],
            "entry_label": ENTRY_LABELS.get(r["entry_type"], r["entry_type"]),
            "quantity_units": r["quantity_units"], "balance_after_units": r["balance_after_units"],
            "counterparty_type": party, "counterparty": party_words,
            "patient_id": pid if in_scope else None,
            "patient_name": patient_name, "patient_mrn": patient_mrn,
            "patient_in_scope": in_scope if pid else None,
            "helix_order_id": r["helix_order_id"] if in_scope else None,
            "prescriber_name": (names.get(str(r["prescriber_id"])) or r["prescriber_id"])
            if (in_scope and r["prescriber_id"]) else None,
            "document_ref": r["document_ref"], "movement_ref": r["movement_ref"],
            "actor_id": r["actor_id"], "actor_name": names.get(str(r["actor_id"])),
            "witness_id": r["witness_id"],
            "witness_name": names.get(str(r["witness_id"])) if r["witness_id"] else None,
            "occurred_at": r["occurred_at"], "recorded_at": r["recorded_at"],
            "remarks": r["remarks"] if in_scope else None,
        })
    return out, disclosed


def _register_query(*, store_id: Optional[int], drug_id: Optional[str],
                    lane: Optional[str], date_from: Optional[str] = None,
                    date_to: Optional[str] = None) -> tuple[str, list]:
    where, params = ["1 = 1"], []
    if lane:
        where.append("lane = ?")
        params.append(lane)
    if store_id is not None:
        where.append("store_id = ?")
        params.append(store_id)
    if drug_id:
        where.append("helix_medication_id = ?")
        params.append(drug_id)
    if date_from:
        where.append("substr(occurred_at, 1, 10) >= ?")
        params.append(date_from)
    if date_to:
        where.append("substr(occurred_at, 1, 10) <= ?")
        params.append(date_to)
    return " AND ".join(where), params


def _clean_date(value: Any, name: str) -> Optional[str]:
    if value in (None, ""):
        return None
    text = str(value).strip()[:10]
    from datetime import date

    try:
        date.fromisoformat(text)
    except ValueError:
        raise NdpsError(f"{name} must be a date (YYYY-MM-DD)", code="bad_date") from None
    return text


def register_page(*, actor: Mapping, store_id: Optional[int] = None,
                  drug_id: Optional[str] = None, before_id: Optional[int] = None,
                  limit: int = 100, audit_ctx: Optional[Mapping] = None) -> dict:
    """Register entries in this lane, newest first, paged by entry id. A read
    that names a patient writes one fail-closed phi_access row in the same
    session before anything is returned."""
    from permissions import department_scope

    me = _actor_id(actor)
    limit = max(1, min(int(limit or 100), MAX_PAGE))
    lane = current_lane()
    sql, params = _register_query(store_id=store_id, drug_id=drug_id, lane=lane)
    if before_id is not None:
        sql += " AND id < ?"
        params.append(int(before_id))
    with db_session() as conn:
        rows = [dict(r) for r in conn.execute(
            f"SELECT * FROM ndps_register WHERE {sql} ORDER BY id DESC LIMIT ?",
            (*params, limit + 1)).fetchall()]
        more = len(rows) > limit
        rows = rows[:limit]
        entries, disclosed = _decorate_entries(conn, rows, dept=department_scope(actor))
        if disclosed:
            _audit(conn, "ndps_register_viewed",
                   summary=f"NDPS register read: {len(entries)} entries naming "
                           f"{len(disclosed)} patients",
                   actor_id=me, actor_role=actor.get("role"), category="phi_access",
                   target_type="ndps_register", target_id=str(store_id or "all"),
                   patient_id=disclosed[0] if len(disclosed) == 1 else None,
                   metadata={"patient_count": len(disclosed),
                             "patient_ids": disclosed[:MAX_AUDITED_PATIENT_IDS],
                             "store_id": str(store_id) if store_id is not None else None,
                             "drug_id": drug_id, "entries": len(entries)},
                   audit_ctx=audit_ctx)
    return {"lane": lane, "lane_label": LANE_LABELS[lane], "entries": entries,
            "next_before_id": entries[-1]["id"] if (more and entries) else None}


def list_variances(*, status: Optional[str] = "open", limit: int = 200) -> dict:
    """Variances in this lane, newest first. No patient is named: a dispense
    variance carries its order id only."""
    if status not in (None, "", "all") and status not in VARIANCE_STATUSES:
        raise NdpsError("unknown variance filter", code="bad_value")
    lane = current_lane()
    sql = "SELECT * FROM ndps_variances WHERE lane = ?"
    params: list[Any] = [lane]
    if status in VARIANCE_STATUSES:
        sql += " AND status = ?"
        params.append(status)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(max(1, min(int(limit or 200), 500)))
    with db_session() as conn:
        rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
        stores = _store_rows(conn)
        drugs = _drug_rows(conn, [r["helix_medication_id"] for r in rows])
        names = _names(conn, [x for r in rows for x in (r["raised_by"], r["reconciled_by"])])
    out = []
    for r in rows:
        store = stores.get(int(r["store_id"])) if r["store_id"] is not None else None
        out.append(r | {
            "variance_label": VARIANCE_LABELS.get(r["variance_kind"], r["variance_kind"]),
            "store_name": (store or {}).get("name") or ((store or {}).get("code")),
            "drug_name": drugs.get(r["helix_medication_id"], {}).get("name")
            or r["helix_medication_id"],
            "raised_by_name": names.get(str(r["raised_by"])),
            "reconciled_by_name": names.get(str(r["reconciled_by"])) if r["reconciled_by"]
            else None,
            "lane_label": LANE_LABELS.get(r["lane"], r["lane"]),
        })
    return {"lane": lane, "variances": out}


def options(actor: Mapping) -> dict:
    """What the NDPS screen needs: the switch, the failure mode, the lane, the
    pharmacy stores, the controlled drugs and who may witness (never the
    caller). No patient data."""
    me = str(actor.get("id") or "")
    with db_session() as conn:
        stores = [{"id": s["id"], "code": s["code"], "name": s["name"] or s["code"],
                   "pharmacy_kind": s["pharmacy_kind"]} for s in conn.execute(
            "SELECT id, code, name, pharmacy_kind FROM stores WHERE pharmacy_kind IS NOT NULL "
            "AND is_active = 1 ORDER BY name, code").fetchall()]
        drugs = [{"id": d["id"], "name": d["name"] or d["id"], "strength": d["strength"],
                  "form": d["form"], "ndps_category": d["ndps_category"]}
                 for d in conn.execute(
                     "SELECT id, name, strength, form, ndps_category FROM helix_medication_master "
                     "WHERE ndps_category IN ('narcotic', 'psychotropic') ORDER BY name, id "
                     "LIMIT 1000").fetchall()]
        people = witnesses(conn, exclude=me)
    lane = current_lane()
    return {"register_on": register_on(), "failure_mode": failure_mode(),
            "requested": bool(getattr(config, "_FEATURE_REQUESTED", {}).get("ndps_register")),
            "lane": lane, "lane_label": LANE_LABELS[lane], "stores": stores, "drugs": drugs,
            "witnesses": people, "may_ndps": may_ndps(actor.get("role")),
            "entry_labels": ENTRY_LABELS, "variance_labels": VARIANCE_LABELS}


# ═════════════════════════════════════════════════════════════════════════════
#  Actions on the NDPS screen, each in one immediate transaction
# ═════════════════════════════════════════════════════════════════════════════

def _text(value: Any, name: str, *, required: bool = False) -> Optional[str]:
    if value is None:
        text = ""
    elif isinstance(value, str):
        text = value.strip()
    else:
        raise NdpsError(f"{name} must be text", code="bad_value")
    if len(text) > MAX_TEXT:
        raise NdpsError(f"{name} is at most {MAX_TEXT} characters", code="bad_value")
    if required and not text:
        raise NdpsError(f"give {name}", code="missing_field")
    return text or None


def _require_register_on() -> None:
    if not register_on():
        raise NdpsError(
            "the NDPS register is off on this site. It turns on only when the hospital has "
            "chosen what happens if an entry cannot be written during a dispense.",
            code="register_off")


def _require_ndps(actor: Mapping) -> None:
    if not may_ndps(actor.get("role")):
        raise NdpsError("this needs the NDPS permission", code="ndps_permission_needed")


def open_register(*, actor: Mapping, reason: Any, witness_id: Any, witness_pin: Any,
                  store_id: Optional[int] = None, drug_id: Optional[str] = None,
                  audit_ctx: Optional[Mapping] = None) -> dict:
    """Open (or open again) the register from the stock on the shelf: one
    'opening' entry per controlled drug in each pharmacy store (or the one
    named) that holds it or already has register entries, at what the stock
    ledger holds there, witnessed. Reconciliation then runs from these
    openings. Counting the shelf itself is the stock take's job (grn-12).

    Opening again over a register that differs from the shelf (or whose
    running balance is broken) never resets it quietly: in the same
    transaction, before the opening, a 'reconciliation_mismatch' variance for
    the part of that register's difference its open variances do not record
    (all of it when none is open against that register) and its 'security'
    audit row are raised for that drug and store. Each opening names the
    variance it raised, or the newest open one already recording the
    difference."""
    me = _actor_id(actor)
    _require_ndps(actor)
    _require_register_on()
    why = _text(reason, "the reason for opening the register", required=True)
    witness = verify_witness(actor=actor, witness_id=witness_id, witness_pin=witness_pin,
                             purpose="opening the NDPS register", target_type="ndps_register",
                             target_id=str(store_id or "all"), audit_ctx=audit_ctx)
    lane = current_lane()
    occurred = utc_iso_now()
    with db_session(immediate=True) as conn:
        if store_id is not None:
            row = conn.execute("SELECT id FROM stores WHERE id = ? AND pharmacy_kind IS NOT NULL",
                               (store_id,)).fetchone()
            if row is None:
                raise NdpsError(f"no pharmacy store {store_id}", code="not_found")
        report = balances(conn, store_id=store_id, drug_id=drug_id)
        opened: list[dict] = []
        for r in report["rows"]:
            if not r["controlled"]:
                continue
            # The old difference goes into reconciliation before the opening
            # writes over it (nothing for a register that matches or was
            # never opened).
            variance_id, raised = _variance_before_opening(
                conn, r, raised_by=me, actor_role=actor.get("role"), opened_by="ndps_opening",
                when="the register was opened again from the shelf", audit_ctx=audit_ctx)
            entry = _insert_entry(
                conn, lane=lane, store_id=r["store_id"], drug_id=r["drug_id"],
                entry_type="opening", quantity=max(int(r["shelf_units"]), 0),
                balance_after=max(int(r["shelf_units"]), 0), actor_id=me,
                occurred_at=occurred, counterparty_type="none", witness_id=witness,
                document_ref="ndps_opening", remarks=why)
            opened.append({"entry_id": entry["id"], "store_id": r["store_id"],
                           "drug_id": r["drug_id"], "units": entry["balance_after_units"],
                           "previous_register_units": r["register_units"],
                           "previous_status": r["status"],
                           "variance_id": variance_id, "variance_raised": raised})
        if not opened:
            raise NdpsError("there is no controlled stock to open the register for",
                            code="nothing_to_open")
        raised_count = sum(1 for o in opened if o["variance_raised"])
        _audit(conn, "ndps_register_opened",
               summary=(f"NDPS register opened from the shelf for {len(opened)} drug and store "
                        "pairs, witnessed"
                        + (f"; {raised_count} differences raised as variances first"
                           if raised_count else "")),
               actor_id=me, actor_role=actor.get("role"), target_type="ndps_register",
               target_id=str(store_id or "all"),
               metadata={"lane": lane, "witness_id": witness, "reason": why,
                         "openings": opened, "variances_raised": raised_count},
               audit_ctx=audit_ctx)
        after = balances(conn, store_id=store_id, drug_id=drug_id)
    return {"opened": opened, "balances": after}


def raise_reconciliation_variances(*, actor: Mapping,
                                   audit_ctx: Optional[Mapping] = None) -> dict:
    """Run reconciliation: for every drug and store whose register differs
    from the shelf or whose running balance is broken, raise one
    'reconciliation_mismatch' variance for the part of the difference the
    variances still open against that register do not record (none when
    they record all of it; a reconciled one never counts;
    _reconciliation_variance)."""
    me = _actor_id(actor)
    _require_ndps(actor)
    _require_register_on()
    raised: list[dict] = []
    with db_session(immediate=True) as conn:
        report = balances(conn)
        for r in report["rows"]:
            variance_id, new = _reconciliation_variance(conn, r, raised_by=me)
            if not new:
                continue
            made = conn.execute("SELECT expected_units, found_units, variance_units FROM "
                                "ndps_variances WHERE id = ?", (variance_id,)).fetchone()
            raised.append({"variance_id": variance_id, "store_id": r["store_id"],
                           "drug_id": r["drug_id"], "register_units": r["register_units"],
                           "shelf_units": r["shelf_units"], "status": r["status"],
                           "expected_units": made["expected_units"],
                           "found_units": made["found_units"],
                           "variance_units": made["variance_units"]})
        if raised:
            _audit(conn, "ndps_variance_raised",
                   summary=f"NDPS reconciliation raised {len(raised)} variances",
                   actor_id=me, actor_role=actor.get("role"), target_type="ndps_register",
                   target_id="reconciliation",
                   metadata={"variances": raised, "lane": current_lane()},
                   audit_ctx=audit_ctx)
        report = balances(conn)
    return {"raised": raised, "balances": report}


def reconcile_variance(variance_id: Any, *, actor: Mapping, note: Any, witness_id: Any,
                       witness_pin: Any, audit_ctx: Optional[Mapping] = None) -> dict:
    """Close an open variance with what was found and done, witnessed. The
    register itself is never edited: a correction is a later entry (or a new
    witnessed opening)."""
    me = _actor_id(actor)
    _require_ndps(actor)
    text = _text(note, "what was found and done", required=True)
    try:
        vid = int(variance_id)
    except (TypeError, ValueError):
        raise NdpsError(f"no variance {variance_id}", code="not_found") from None
    with db_session() as conn:
        row = conn.execute("SELECT * FROM ndps_variances WHERE id = ? AND lane = ?",
                           (vid, current_lane())).fetchone()
        if row is None:
            raise NdpsError(f"no variance {vid}", code="not_found")
        if row["status"] != "open":
            raise NdpsError("this variance is already reconciled", code="already_reconciled")
    witness = verify_witness(actor=actor, witness_id=witness_id, witness_pin=witness_pin,
                             purpose="reconciling an NDPS variance", target_type="ndps_variance",
                             target_id=vid, audit_ctx=audit_ctx)
    with db_session(immediate=True) as conn:
        cur = conn.execute(
            "UPDATE ndps_variances SET status = 'reconciled', reconciled_by = ?, "
            "reconciled_at = ?, reconciliation_note = ? WHERE id = ? AND status = 'open'",
            (me, utc_iso_now(), text, vid))
        if cur.rowcount != 1:
            raise NdpsError("this variance changed while you were working on it. Nothing was "
                            "changed.", code="already_reconciled")
        _audit(conn, "ndps_variance_reconciled",
               summary=f"NDPS variance {vid} reconciled, witnessed",
               actor_id=me, actor_role=actor.get("role"), target_type="ndps_variance",
               target_id=vid,
               metadata={"variance_id": str(vid), "witness_id": witness, "note": text},
               audit_ctx=audit_ctx)
        fresh = dict(conn.execute("SELECT * FROM ndps_variances WHERE id = ?",
                                  (vid,)).fetchone())
    return {"variance": fresh}


def draft_incident(variance_id: Any, *, actor: Mapping,
                   audit_ctx: Optional[Mapping] = None) -> dict:
    """Report a variance to the hospital's incident register (GRN-R14) as a
    'security' incident naming no patient, and link it to the variance. Needs
    the incident register on this site (ORB_NABH)."""
    me = _actor_id(actor)
    _require_ndps(actor)
    if not getattr(config, "NABH_ENABLED", False):
        raise NdpsError("the incident register is switched off on this site, so report this "
                        "variance on paper", code="incident_register_off")
    try:
        vid = int(variance_id)
    except (TypeError, ValueError):
        raise NdpsError(f"no variance {variance_id}", code="not_found") from None
    with db_session() as conn:
        row = conn.execute("SELECT * FROM ndps_variances WHERE id = ? AND lane = ?",
                           (vid, current_lane())).fetchone()
        if row is None:
            raise NdpsError(f"no variance {vid}", code="not_found")
        if row["incident_ref"]:
            raise NdpsError(f"this variance is already reported as incident "
                            f"{row['incident_ref']}", code="already_reported",
                            detail={"incident_ref": row["incident_ref"]})
        drug = conn.execute("SELECT name FROM helix_medication_master WHERE id = ?",
                            (row["helix_medication_id"],)).fetchone()
        store = conn.execute("SELECT name, code FROM stores WHERE id = ?",
                             (row["store_id"],)).fetchone() if row["store_id"] is not None \
            else None
    from services import nabh_service

    drug_name = (drug["name"] if drug else None) or row["helix_medication_id"]
    store_name = ((store["name"] or store["code"]) if store else None) or "a pharmacy store"
    incident = nabh_service.report_incident(
        incident_type="security", severity="no_harm",
        title=f"Controlled drug discrepancy: {drug_name}",
        description=(f"NDPS variance {vid} in {store_name}: "
                     f"{VARIANCE_LABELS.get(row['variance_kind'], row['variance_kind'])}. "
                     f"{row['detail'] or ''}").strip(),
        location=store_name, reported_by=me, reporter_role=actor.get("role"))
    reference = incident.get("reference") or str(incident.get("id"))
    with db_session(immediate=True) as conn:
        conn.execute("UPDATE ndps_variances SET incident_ref = ? WHERE id = ? AND "
                     "incident_ref IS NULL", (reference, vid))
        _audit(conn, "ndps_variance_incident_drafted",
               summary=f"NDPS variance {vid} reported as incident {reference}",
               actor_id=me, actor_role=actor.get("role"), target_type="ndps_variance",
               target_id=vid,
               metadata={"variance_id": str(vid), "incident_ref": reference,
                         "incident_id": str(incident.get("id"))}, audit_ctx=audit_ctx)
    return {"incident_ref": reference, "incident_id": incident.get("id")}


_CSV_COLUMNS = (
    "Entry", "Time (UTC)", "Store", "Drug", "Strength", "Batch", "Entry type", "Quantity",
    "Balance after", "From or to", "Patient", "Hospital number", "Order", "Prescriber",
    "Document", "Made by", "Witness", "Register", "Remarks",
)


def _csv_cell(value: Any) -> Any:
    """A cell a spreadsheet will never run as a formula."""
    if value is None:
        return ""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value
    text = str(value)
    if text[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + text
    return text


def export_csv(*, actor: Mapping, store_id: Optional[int] = None,
               drug_id: Optional[str] = None, date_from: Any = None, date_to: Any = None,
               audit_ctx: Optional[Mapping] = None) -> tuple[str, str]:
    """The register in this lane as CSV, oldest first: (filename, text). The
    export is audited ('security', fail-closed) in the same session BEFORE the
    text is returned; no audit row, no file. A patient outside the caller's
    department is named only as such. Statutory form layouts (NDPS Forms 3D,
    3E, 3H) are W170's and are not this file."""
    from permissions import department_scope

    me = _actor_id(actor)
    _require_ndps(actor)
    start = _clean_date(date_from, "the first date")
    end = _clean_date(date_to, "the last date")
    if start and end and start > end:
        raise NdpsError("the first date is after the last date", code="bad_date")
    lane = current_lane()
    sql, params = _register_query(store_id=store_id, drug_id=drug_id, lane=lane,
                                  date_from=start, date_to=end)
    with db_session() as conn:
        rows = [dict(r) for r in conn.execute(
            f"SELECT * FROM ndps_register WHERE {sql} ORDER BY store_id, helix_medication_id, "
            "seq LIMIT ?", (*params, MAX_EXPORT_ROWS + 1)).fetchall()]
        if len(rows) > MAX_EXPORT_ROWS:
            raise NdpsError(f"more than {MAX_EXPORT_ROWS} entries: narrow the export by store, "
                            "drug or dates", code="export_too_large")
        entries, disclosed = _decorate_entries(conn, rows, dept=department_scope(actor))
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(_CSV_COLUMNS)
        for e in entries:
            writer.writerow([_csv_cell(v) for v in (
                e["seq"], e["occurred_at"], e["store_name"], e["drug_name"], e["strength"],
                e["batch_no"], e["entry_label"], e["quantity_units"], e["balance_after_units"],
                e["counterparty"],
                e["patient_name"] if e["patient_in_scope"] is not False else OUT_OF_SCOPE_PATIENT,
                e["patient_mrn"], e["helix_order_id"], e["prescriber_name"], e["document_ref"],
                e["actor_name"] or e["actor_id"], e["witness_name"] or e["witness_id"],
                e["lane_label"], e["remarks"])])
        _audit(conn, "ndps_register_exported",
               summary=f"NDPS register exported: {len(entries)} entries naming "
                       f"{len(disclosed)} patients",
               actor_id=me, actor_role=actor.get("role"), target_type="ndps_register",
               target_id=str(store_id or "all"),
               patient_id=disclosed[0] if len(disclosed) == 1 else None,
               metadata={"lane": lane, "entries": len(entries),
                         "patient_count": len(disclosed),
                         "patient_ids": disclosed[:MAX_AUDITED_PATIENT_IDS],
                         "store_id": str(store_id) if store_id is not None else None,
                         "drug_id": drug_id, "date_from": start, "date_to": end},
               audit_ctx=audit_ctx)
    stamp = utc_iso_now()[:10]
    return f"ndps-register-{lane.replace('_', '-')}-{stamp}.csv", buf.getvalue()
