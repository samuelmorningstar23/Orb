"""Orb, the drug stock ledger: the ONLY writer of drug stock (grn-4).

Design: docs/his_design/grn.json, section D. Migration 54 gave Helix's lots a
store and a status, and its movements a type, an actor, a balance and a seq.
This module is now the one place in server code that INSERTs, UPDATEs or
DELETEs helix_inventory_lots or helix_stock_movements. A structural test
(tests/test_pharmacy_stock_single_writer.py) walks every Python file under
server/ and fails on a write to either table anywhere else, apart from the
migrations, the Helix DDL and the DELETE-only wipes in the two seed scripts.

WHY ONE WRITER. Two ledgers for one physical drug is the failure the
procurement module's header warns about. A lot's quantity must always equal the
sum of its movements, and that holds only if every change to the quantity is
also a row in the ledger, written in the same statement sequence. reconcile()
checks the invariant; this module keeps it.

THE RULES EVERY WRITE HERE FOLLOWS
  * It runs on the CALLER's connection, inside the CALLER's transaction, and
    never opens, commits or nests a session. The caller holds one
    db_session(immediate=True), so the read that chose a lot and the write
    that moved it sit under one write lock, and the module is safe with
    ORB_DB_POOL=0 (a nested session there would be a second connection that
    deadlocks against the first).
  * A quantity changes only by a RELATIVE, guarded update:
        UPDATE ... SET quantity = quantity + ? WHERE id = ? AND quantity + ? >= 0
    so two writers can never both take the last unit, and a decrement that
    would go below zero changes nothing and is refused.
  * Every change is a movement row with a type, an actor, the balance after
    it, the canonical UTC instant (services/clinical_time) and a seq, the
    ledger's ordering key (rowid on this TEXT-keyed table can change on
    VACUUM). seq is MAX(seq) + 1 read under the write lock.
  * Enumerations on the Helix tables are validated HERE, against the
    constants below, because migration 54 could put no CHECK on an FK parent.
    reconcile() reports any stored value outside them.

WHAT IS DISPENSABLE (one predicate, used by dispense, pre-flight, the Helix
formulary and every later stock report): units on hand, a recorded expiry on
or after the hospital's date, status 'available', and a location that is the
dispensing shelf. The shelf is a legacy lot (store NULL, before a dispensing
store adopted it), the active dispensing store, or an active NDPS vault for a
drug classified narcotic or psychotropic (narcotics are dispensed from the
vault, GRN-R14). The predicate reads data only, never a switch, so turning the
module off after use can never make quarantined, sub-store or ward stock
dispensable. For a site that never turns the module on every lot is a legacy,
available lot and the rule is exactly the one F064 shipped: in date, FEFO.

THE MOVEMENT HOOK. post_movement calls every registered hook after its row is
written, in the same transaction. Nothing registers one yet. grn-13 registers
the NDPS register writer there, and builds its two failure modes (ruling
GRN-RS7) in this module; until then an exception from a hook rolls the whole
movement back.
"""
from __future__ import annotations

import logging
import re
import uuid
from datetime import date, datetime, timezone
from typing import Any, Callable, Iterable, Optional

from services.clinical_time import format_utc, parse_to_utc

logger = logging.getLogger("orb.pharmacy_stock")

# ═════════════════════════════════════════════════════════════════════════════
#  Enumerations (no CHECK on the Helix tables, so these are the rule)
# ═════════════════════════════════════════════════════════════════════════════

#: helix_inventory_lots.status. 'available' is the only dispensable one.
LOT_STATUSES: tuple[str, ...] = (
    "available", "quarantined", "recalled", "nsq", "pending_inspection",
    "written_off", "returned_to_vendor",
)

#: helix_stock_movements.movement_type. 'op_sale' is reserved for the OPD
#: counter; 'consumption' for ward stock used on an eMAR dose (grn-17).
MOVEMENT_TYPES: tuple[str, ...] = (
    "opening", "receipt", "dispense", "transfer_out", "transfer_in", "ward_return",
    "vendor_return", "write_off", "adjustment", "count_variance", "consumption", "op_sale",
)

#: Movements that only ever add units, and those that only ever take them.
#: 'adjustment' and 'count_variance' go either way.
INWARD_MOVEMENT_TYPES = frozenset({"opening", "receipt", "transfer_in", "ward_return"})
OUTWARD_MOVEMENT_TYPES = frozenset({
    "dispense", "transfer_out", "vendor_return", "write_off", "consumption", "op_sale",
})

#: helix_medication_master.drug_schedule and ndps_category. NULL means NOT
#: CLASSIFIED and is never read as 'none'.
DRUG_SCHEDULES: tuple[str, ...] = ("none", "G", "H", "H1", "X")
NDPS_CATEGORIES: tuple[str, ...] = ("none", "narcotic", "psychotropic")
NDPS_CONTROLLED: tuple[str, ...] = ("narcotic", "psychotropic")

#: helix_medication_master.storage_condition. The last two are cold chain.
STORAGE_CONDITIONS: tuple[str, ...] = ("room_temperature", "cool", "refrigerated", "frozen")
COLD_CHAIN_STORAGE: tuple[str, ...] = ("refrigerated", "frozen")

#: helix_medication_master.identity_status (governed by grn-6).
IDENTITY_STATUSES: tuple[str, ...] = ("draft", "active", "retired")

#: stores.pharmacy_kind values this module reads.
DISPENSING_STORE_KIND = "dispensing"
NDPS_VAULT_KIND = "ndps_vault"

#: Which lot status may follow which. 'recalled' and 'nsq' never return to
#: 'available' (every option of ruling GRN-RS2 agrees); a lot leaves through a
#: write-off or a return to the vendor, after its units have left by a
#: movement. Written off and returned are terminal. The two moves back to the
#: shelf listed here (release from quarantine, restock of an inspected return)
#: are further held behind the two switches below.
LOT_STATUS_TRANSITIONS: dict[str, frozenset[str]] = {
    "available": frozenset({"quarantined", "recalled", "nsq", "written_off",
                            "returned_to_vendor"}),
    "quarantined": frozenset({"available", "recalled", "nsq", "written_off",
                              "returned_to_vendor"}),
    "pending_inspection": frozenset({"available", "quarantined", "written_off",
                                     "returned_to_vendor"}),
    "recalled": frozenset({"written_off", "returned_to_vendor"}),
    "nsq": frozenset({"written_off", "returned_to_vendor"}),
    "written_off": frozenset(),
    "returned_to_vendor": frozenset(),
}

#: Statuses a lot may only enter once its units have left by a movement, so a
#: status change never hides stock that is still counted.
EMPTY_ONLY_STATUSES = frozenset({"written_off", "returned_to_vendor"})

#: Rulings GRN-RS2 (may a quarantined batch go back on the shelf?) and GRN-RS3
#: (may a ward return be restocked?) are held for Samuel. Until each is ruled
#: the STRICTEST option named applies, option (c) of each: a quarantined lot is
#: never released and a returned unit is never restocked; it leaves by a
#: write-off or a return to the vendor. The option (b) checks in set_lot_status
#: (a different person releases; only an in-date, non cold-chain, non-NDPS
#: return is restocked) stay written, so a ruling of (b) is one switch here.
QUARANTINE_RELEASE_ALLOWED = False  # GRN-RS2, strictest until ruled
RETURN_RESTOCK_ALLOWED = False      # GRN-RS3, strictest until ruled

_ALIAS = re.compile(r"^[a-z][a-z0-9_]*$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class StockError(ValueError):
    """A stock write the caller must correct or that the shelf refuses. Carries
    a stable `code` for the route layer. A ValueError, so every existing
    caller that answers ValueError with 400 keeps doing so."""

    code = "stock_error"

    def __init__(self, message: str, *, code: Optional[str] = None):
        super().__init__(message)
        if code:
            self.code = code


class InsufficientStock(StockError):
    """A decrement would take a lot below zero. Nothing was changed."""
    code = "insufficient_stock"


# ═════════════════════════════════════════════════════════════════════════════
#  The hospital's date
# ═════════════════════════════════════════════════════════════════════════════

def site_today() -> str:
    """Today's date where the hospital is (YYYY-MM-DD): the first date a lot
    must still be good on to count as stock.

    The site's configured timezone when there is one, else the appliance's
    local clock. Never the UTC date on its own: in India that is still
    yesterday until 05:30, which would keep a lot that expired yesterday
    dispensable for the first hours of the day. (Moved here from
    helix_service._expiry_cutoff, which now delegates to it.)"""
    try:
        from services.site_config_service import current_site_config
        tz = current_site_config().timezone
        if tz:
            from zoneinfo import ZoneInfo
            return datetime.now(ZoneInfo(tz)).date().isoformat()
    except Exception:
        pass
    return date.today().isoformat()


# ═════════════════════════════════════════════════════════════════════════════
#  The dispensable predicate
# ═════════════════════════════════════════════════════════════════════════════

def _alias(alias: str) -> str:
    if not _ALIAS.match(alias or ""):
        raise ValueError(f"not a table alias: {alias!r}")
    return alias


def shelf_lot_sql(alias: str = "l") -> str:
    """SQL condition: the lot is 'available' and sits on the dispensing shelf
    (see the module docstring). No placeholders. `alias` names the
    helix_inventory_lots row in the caller's query."""
    a = _alias(alias)
    return (
        f"{a}.status = 'available' AND ("
        f"{a}.store_id IS NULL OR EXISTS ("
        "SELECT 1 FROM stores ps_store "
        f"WHERE ps_store.id = {a}.store_id AND ps_store.is_active = 1 AND ("
        f"ps_store.pharmacy_kind = '{DISPENSING_STORE_KIND}' OR ("
        f"ps_store.pharmacy_kind = '{NDPS_VAULT_KIND}' AND EXISTS ("
        "SELECT 1 FROM helix_medication_master ps_drug "
        f"WHERE ps_drug.id = {a}.medication_id "
        "AND ps_drug.ndps_category IN ('narcotic', 'psychotropic'))))))"
    )


def dispensable_lot_sql(alias: str = "l") -> str:
    """SQL condition: the lot can be dispensed today. ONE placeholder, the
    hospital's date (pass site_today()). A lot with no recorded expiry fails
    the date test (date(NULL) is NULL), so it is refused rather than sorted
    anywhere."""
    a = _alias(alias)
    return f"{a}.quantity > 0 AND date({a}.expiration_date) >= ? AND {shelf_lot_sql(a)}"


def pick_fefo_lot(conn, medication_id: str, units: int = 1, *, lot_id: Optional[str] = None,
                  today: Optional[str] = None) -> Optional[dict]:
    """The dispensable lot a dispense of `units` takes: the earliest expiry
    first, among lots holding at least `units` (a dispense is never split
    across lots). With `lot_id`, that lot only, and only if it qualifies.
    None when nothing qualifies."""
    units = _whole(units, "units")
    if units < 1:
        raise StockError("a dispense takes at least one unit", code="bad_quantity")
    sql = (f"SELECT l.* FROM helix_inventory_lots l WHERE l.medication_id = ? "
           f"AND {dispensable_lot_sql('l')} AND l.quantity >= ?")
    params: list[Any] = [medication_id, today or site_today(), units]
    if lot_id is not None:
        sql += " AND l.id = ?"
        params.append(lot_id)
    sql += " ORDER BY date(l.expiration_date) ASC, l.rowid ASC LIMIT 1"
    row = conn.execute(sql, params).fetchone()
    return dict(row) if row else None


def dispensable_units(conn, medication_id: str, *, today: Optional[str] = None) -> int:
    """Units of one drug a dispense could take today, across all its lots."""
    row = conn.execute(
        f"SELECT SUM(l.quantity) AS total FROM helix_inventory_lots l "
        f"WHERE l.medication_id = ? AND {dispensable_lot_sql('l')}",
        (medication_id, today or site_today()),
    ).fetchone()
    total = row["total"] if row else None
    return int(total) if total is not None else 0


def undispensable_shelf_units(conn, medication_id: str, *, today: Optional[str] = None) -> int:
    """Units of one drug on the dispensing shelf that cannot be dispensed
    because they are expired or carry no expiry. Stock elsewhere (a sub-store,
    quarantine, another status) is not counted: it is not on the shelf."""
    row = conn.execute(
        f"SELECT SUM(l.quantity) AS total FROM helix_inventory_lots l "
        f"WHERE l.medication_id = ? AND l.quantity > 0 AND {shelf_lot_sql('l')} "
        f"AND (l.expiration_date IS NULL OR date(l.expiration_date) IS NULL "
        f"     OR date(l.expiration_date) < ?)",
        (medication_id, today or site_today()),
    ).fetchone()
    total = row["total"] if row else None
    return int(total) if total is not None else 0


# ═════════════════════════════════════════════════════════════════════════════
#  Helpers
# ═════════════════════════════════════════════════════════════════════════════

def _require_transaction(conn) -> None:
    """Every write here belongs to the caller's transaction. A connection with
    none open would commit each statement on its own, so a quantity could
    change without its movement."""
    if not getattr(conn, "in_transaction", False):
        raise RuntimeError(
            "pharmacy_stock_service writes only inside the caller's transaction: "
            "open db_session(immediate=True) and pass its connection."
        )


def _whole(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise StockError(f"{name} must be a whole number", code="bad_quantity")
    return value


def _text(value: object) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _date_text(value: object, name: str) -> Optional[str]:
    """A calendar date as YYYY-MM-DD, or None. A malformed value is refused,
    never stored, because date() reads it as NULL and the lot would silently
    never be dispensable."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value).strip()[:10]
    if not _DATE.match(text):
        raise StockError(f"{name} must be a date (YYYY-MM-DD)", code="bad_date")
    try:
        date.fromisoformat(text)
    except ValueError:
        raise StockError(f"{name} must be a real calendar date", code="bad_date") from None
    return text


def _now_pair(occurred_at: object = None) -> tuple[str, str]:
    """(canonical UTC instant, the legacy CURRENT_TIMESTAMP spelling) for one
    movement. The legacy `timestamp` column is what the existing Helix readers
    order by, so it is kept filled."""
    if occurred_at is None:
        moment = datetime.now(timezone.utc)
    else:
        moment = parse_to_utc(occurred_at, naive_tz=timezone.utc)
    return format_utc(moment), moment.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _row(row) -> Optional[dict]:
    return dict(row) if row is not None else None


# ═════════════════════════════════════════════════════════════════════════════
#  The movement hook registry
# ═════════════════════════════════════════════════════════════════════════════

MovementHook = Callable[[Any, dict], None]
_MOVEMENT_HOOKS: dict[str, MovementHook] = {}


def register_movement_hook(name: str, hook: Optional[MovementHook]) -> None:
    """Run `hook(conn, movement)` after every movement, in the movement's own
    transaction. None unregisters `name`. Reserved for the NDPS register
    (grn-13); an exception from a hook rolls the movement back."""
    if hook is None:
        _MOVEMENT_HOOKS.pop(name, None)
    else:
        _MOVEMENT_HOOKS[name] = hook


def registered_movement_hooks() -> tuple[str, ...]:
    return tuple(_MOVEMENT_HOOKS)


# ═════════════════════════════════════════════════════════════════════════════
#  Writes
# ═════════════════════════════════════════════════════════════════════════════

def post_movement(
    conn,
    *,
    lot_id: str,
    movement_type: str,
    quantity_change: int,
    actor_id: str,
    reference_type: Optional[str] = None,
    reference_id: Optional[str] = None,
    order_id: Optional[str] = None,
    idempotency_key: Optional[str] = None,
    remarks: Optional[str] = None,
    unit_cost_paise: Optional[int] = None,
    occurred_at: object = None,
) -> dict:
    """Move `quantity_change` units into (+) or out of (-) one lot, and write
    the ledger row that says so. Returns the movement row as a dict.

    A replay of an `idempotency_key` already in the ledger writes nothing and
    returns the earlier row with `replayed` True; the same key on a different
    lot, type or quantity is refused. `order_id` is the Helix order a dispense
    belongs to (the legacy column billing and the ward receipt read)."""
    _require_transaction(conn)
    if movement_type not in MOVEMENT_TYPES:
        raise StockError(f"unknown movement type {movement_type!r}", code="bad_movement_type")
    change = _whole(quantity_change, "quantity_change")
    if change == 0:
        raise StockError("a movement moves at least one unit", code="bad_quantity")
    if movement_type in INWARD_MOVEMENT_TYPES and change < 0:
        raise StockError(f"a {movement_type} movement adds units", code="bad_quantity")
    if movement_type in OUTWARD_MOVEMENT_TYPES and change > 0:
        raise StockError(f"a {movement_type} movement takes units", code="bad_quantity")
    actor = _text(actor_id)
    if not actor:
        raise StockError("every movement names the person or process that made it",
                         code="missing_actor")
    if unit_cost_paise is not None:
        unit_cost_paise = _whole(unit_cost_paise, "unit_cost_paise")
        if unit_cost_paise < 0:
            raise StockError("a unit cost is never negative", code="bad_cost")
    key = _text(idempotency_key)

    if key:
        prior = conn.execute("SELECT * FROM helix_stock_movements WHERE idempotency_key = ?",
                             (key,)).fetchone()
        if prior is not None:
            if (prior["lot_id"], prior["movement_type"], prior["quantity_change"]) != (
                    lot_id, movement_type, change):
                raise StockError("this request key was already used for a different stock "
                                 "movement. Nothing was changed.", code="idempotency_conflict")
            replay = dict(prior)
            replay["replayed"] = True
            return replay

    lot = conn.execute(
        "SELECT id, medication_id, store_id FROM helix_inventory_lots WHERE id = ?",
        (lot_id,),
    ).fetchone()
    if lot is None:
        raise StockError(f"no lot {lot_id}", code="lot_not_found")

    cur = conn.execute(
        "UPDATE helix_inventory_lots SET quantity = COALESCE(quantity, 0) + ? "
        "WHERE id = ? AND COALESCE(quantity, 0) + ? >= 0",
        (change, lot_id, change),
    )
    if cur.rowcount != 1:
        raise InsufficientStock(
            "this lot does not hold enough units for that. Nothing was changed.")
    balance = int(conn.execute("SELECT quantity FROM helix_inventory_lots WHERE id = ?",
                               (lot_id,)).fetchone()[0])
    seq = int(conn.execute(
        "SELECT COALESCE(MAX(seq), 0) + 1 FROM helix_stock_movements").fetchone()[0])
    occurred, legacy_stamp = _now_pair(occurred_at)
    movement_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO helix_stock_movements (
               id, lot_id, medication_id, order_id, quantity_change, reason, timestamp,
               movement_type, store_id, balance_after_units, unit_cost_paise,
               reference_type, reference_id, actor_id, remarks, idempotency_key,
               occurred_at, seq)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (movement_id, lot_id, lot["medication_id"], order_id, change, movement_type,
         legacy_stamp, movement_type, lot["store_id"], balance, unit_cost_paise,
         _text(reference_type), _text(reference_id), actor, _text(remarks), key,
         occurred, seq),
    )
    movement = dict(conn.execute("SELECT * FROM helix_stock_movements WHERE id = ?",
                                 (movement_id,)).fetchone())
    for hook in list(_MOVEMENT_HOOKS.values()):
        hook(conn, movement)
    return movement


def _pharmacy_store(conn, store_id: int):
    row = conn.execute(
        "SELECT id, code, name, pharmacy_kind, is_active FROM stores WHERE id = ?",
        (store_id,),
    ).fetchone()
    if row is None or row["pharmacy_kind"] is None:
        raise StockError(f"no pharmacy store {store_id}", code="store_not_found")
    return row


def _same_identity_lot(conn, medication_id: str, lot_number: str, expiry: Optional[str],
                       store_id: Optional[int]):
    return conn.execute(
        "SELECT * FROM helix_inventory_lots WHERE medication_id = ? AND lot_number = ? "
        "AND expiration_date IS ? AND store_id IS ? ORDER BY rowid LIMIT 1",
        (medication_id, lot_number, expiry, store_id),
    ).fetchone()


def open_or_merge_lot(
    conn,
    *,
    medication_id: str,
    lot_number: str,
    expiration_date: object,
    units: int,
    actor_id: str,
    store_id: Optional[int] = None,
    lot_id: Optional[str] = None,
    status: str = "available",
    movement_type: str = "opening",
    units_per_pack: Optional[int] = None,
    mrp_pack_paise: Optional[int] = None,
    unit_cost_paise: Optional[int] = None,
    mfg_date: object = None,
    vendor_id: Optional[int] = None,
    receipt_line_ref: Optional[int] = None,
    received_at: object = None,
    reference_type: Optional[str] = None,
    reference_id: Optional[str] = None,
    idempotency_key: Optional[str] = None,
    remarks: Optional[str] = None,
) -> dict:
    """Bring `units` of one batch into stock. Returns
    {"lot": row, "movement": row or None, "merged": bool}.

    The same drug, batch and expiry already held in the same place and status
    is MERGED: its quantity grows by a movement, and no second lot appears.
    Otherwise a new lot is created at quantity 0 and the units arrive by an
    inward movement, so the lot's quantity is its ledger from the first row.
    A batch is never merged into a lot of another status: that would release
    quarantined units, or quarantine good ones, without anybody deciding it.
    `units` may be 0 (a lot recorded empty, with no movement)."""
    _require_transaction(conn)
    if movement_type not in INWARD_MOVEMENT_TYPES:
        raise StockError(f"a lot is opened by an inward movement, not {movement_type!r}",
                         code="bad_movement_type")
    if status not in LOT_STATUSES:
        raise StockError(f"unknown lot status {status!r}", code="bad_status")
    units = _whole(units, "units")
    if units < 0:
        raise StockError("a lot is opened with zero or more units", code="bad_quantity")
    batch = _text(lot_number)
    if not batch:
        raise StockError("a lot needs its batch number", code="missing_batch")
    expiry = _date_text(expiration_date, "expiry date")
    if conn.execute("SELECT 1 FROM helix_medication_master WHERE id = ?",
                    (medication_id,)).fetchone() is None:
        raise StockError(f"no drug {medication_id} in the formulary", code="drug_not_found")
    if store_id is not None:
        _pharmacy_store(conn, store_id)
    for name, value in (("units_per_pack", units_per_pack), ("mrp_pack_paise", mrp_pack_paise),
                        ("unit_cost_paise", unit_cost_paise)):
        if value is not None and _whole(value, name) < 0:
            raise StockError(f"{name} is never negative", code="bad_quantity")
    if units_per_pack is not None and units_per_pack < 1:
        raise StockError("units per pack is at least 1", code="bad_pack_size")

    key = _text(idempotency_key)
    if key:
        prior = conn.execute("SELECT * FROM helix_stock_movements WHERE idempotency_key = ?",
                             (key,)).fetchone()
        if prior is not None:
            lot = conn.execute("SELECT * FROM helix_inventory_lots WHERE id = ?",
                               (prior["lot_id"],)).fetchone()
            replay = dict(prior)
            replay["replayed"] = True
            return {"lot": _row(lot), "movement": replay, "merged": True}

    existing = None
    if lot_id is not None:
        existing = conn.execute("SELECT * FROM helix_inventory_lots WHERE id = ?",
                                (lot_id,)).fetchone()
        if existing is not None and (
                existing["medication_id"], existing["lot_number"],
                existing["expiration_date"], existing["store_id"]) != (
                medication_id, batch, expiry, store_id):
            raise StockError(f"lot id {lot_id} is already a different batch. Nothing was "
                             "changed.", code="lot_id_taken")
        if existing is None and store_id is not None:
            # One lot per drug, batch and expiry in a store (migration 54's
            # partial unique index): a second id for the same batch is refused
            # here, in words, rather than at the index.
            twin = _same_identity_lot(conn, medication_id, batch, expiry, store_id)
            if twin is not None:
                raise StockError(f"batch {batch} is already held in this store as lot "
                                 f"{twin['id']}. Nothing was changed.", code="batch_exists")
    else:
        existing = _same_identity_lot(conn, medication_id, batch, expiry, store_id)

    if existing is not None and existing["status"] != status:
        raise StockError(
            f"batch {batch} is already held here as {existing['status'].replace('_', ' ')} "
            "stock and is not merged with stock of another status. Nothing was changed.",
            code="lot_status_conflict")

    merged = existing is not None
    if merged:
        target_id = existing["id"]
    else:
        target_id = lot_id or f"LOT-{uuid.uuid4().hex[:12].upper()}"
        created, _ = _now_pair()
        conn.execute(
            """INSERT INTO helix_inventory_lots (
                   id, medication_id, lot_number, quantity, expiration_date, store_id,
                   status, units_per_pack, mrp_pack_paise, unit_cost_paise, mfg_date,
                   vendor_id, receipt_line_ref, received_at, created_at)
               VALUES (?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (target_id, medication_id, batch, expiry, store_id, status, units_per_pack,
             mrp_pack_paise, unit_cost_paise, _date_text(mfg_date, "manufacture date"),
             vendor_id, receipt_line_ref,
             _now_pair(received_at)[0] if received_at is not None else None, created),
        )

    movement = None
    if units > 0:
        movement = post_movement(
            conn, lot_id=target_id, movement_type=movement_type, quantity_change=units,
            actor_id=actor_id, reference_type=reference_type, reference_id=reference_id,
            idempotency_key=key, remarks=remarks, unit_cost_paise=unit_cost_paise,
        )
    lot = conn.execute("SELECT * FROM helix_inventory_lots WHERE id = ?",
                       (target_id,)).fetchone()
    return {"lot": dict(lot), "movement": movement, "merged": merged}


def _quarantined_by(conn, lot_id: str) -> Optional[str]:
    row = conn.execute(
        "SELECT actor_id FROM pharmacy_lot_events WHERE lot_id = ? AND to_status = 'quarantined' "
        "ORDER BY id DESC LIMIT 1", (lot_id,)).fetchone()
    return row["actor_id"] if row else None


def set_lot_status(
    conn,
    lot_id: str,
    to_status: str,
    *,
    actor_id: str,
    reason: str,
    second_actor_id: Optional[str] = None,
    reference_type: Optional[str] = None,
    reference_id: Optional[str] = None,
    today: Optional[str] = None,
) -> dict:
    """Change one lot's status and append the pharmacy_lot_events row that
    records who did it and why. Returns {"lot": row, "event_id": int}.

    Strict defaults, each a ruling held for Samuel's confirmation:
      * GRN-RS2: a quarantined lot is NEVER released back to the shelf while
        QUARANTINE_RELEASE_ALLOWED is off (option (c), the strictest). With it
        on (option (b)), only somebody other than the person who quarantined
        it may release it. Recalled and NSQ lots never return either way.
      * GRN-RS3: a returned lot NEVER leaves inspection for the shelf while
        RETURN_RESTOCK_ALLOWED is off (option (c)). With it on (option (b)),
        only an in-date, non cold-chain, non-NDPS return may. Whether the seal
        is intact is the inspecting service's check, made before this call.
      * A lot is written off or returned to the vendor only once its units
        have left by a movement, so a status never hides counted stock."""
    _require_transaction(conn)
    if to_status not in LOT_STATUSES:
        raise StockError(f"unknown lot status {to_status!r}", code="bad_status")
    actor = _text(actor_id)
    if not actor:
        raise StockError("a status change names who made it", code="missing_actor")
    why = _text(reason)
    if not why:
        raise StockError("give a reason for changing this lot's status", code="missing_reason")
    second = _text(second_actor_id)
    if second and second == actor:
        raise StockError("the second person must be somebody else", code="same_person")
    lot = conn.execute(
        "SELECT l.*, m.ndps_category, m.storage_condition FROM helix_inventory_lots l "
        "LEFT JOIN helix_medication_master m ON m.id = l.medication_id WHERE l.id = ?",
        (lot_id,)).fetchone()
    if lot is None:
        raise StockError(f"no lot {lot_id}", code="lot_not_found")
    current = lot["status"]
    if current == to_status:
        raise StockError(f"this lot is already {to_status.replace('_', ' ')}",
                         code="same_status")
    if to_status not in LOT_STATUS_TRANSITIONS.get(current, frozenset()):
        raise StockError(
            f"a {str(current).replace('_', ' ')} lot cannot become "
            f"{to_status.replace('_', ' ')}", code="illegal_lot_transition")
    if to_status in EMPTY_ONLY_STATUSES and int(lot["quantity"] or 0) != 0:
        raise StockError(
            "this lot still holds units. Record the write-off or vendor return movement "
            "first, then close the lot.", code="lot_not_empty")
    if current == "quarantined" and to_status == "available":
        if not QUARANTINE_RELEASE_ALLOWED:
            raise StockError(
                "a quarantined lot is not released back to the shelf on this site. Write it "
                "off or return it to the vendor. Nothing was changed.",
                code="release_not_allowed")
        if _quarantined_by(conn, lot_id) == actor:
            raise StockError(
                "the person who quarantined this lot cannot also release it. Ask another "
                "pharmacist to release it.", code="same_person_release")
    if current == "pending_inspection" and to_status == "available":
        if not RETURN_RESTOCK_ALLOWED:
            raise StockError(
                "a returned medicine is not put back on the shelf on this site. Write it "
                "off or return it to the vendor. Nothing was changed.",
                code="restock_not_allowed")
        expiry = lot["expiration_date"]
        if lot["ndps_category"] in NDPS_CONTROLLED:
            raise StockError("a returned NDPS drug is never restocked", code="restock_refused")
        if lot["storage_condition"] in COLD_CHAIN_STORAGE:
            raise StockError("a returned cold-chain drug is never restocked",
                             code="restock_refused")
        if not expiry or str(expiry)[:10] < (today or site_today()):
            raise StockError("an expired or undated return is never restocked",
                             code="restock_refused")

    cur = conn.execute(
        "UPDATE helix_inventory_lots SET status = ? WHERE id = ? AND status = ?",
        (to_status, lot_id, current))
    if cur.rowcount != 1:
        raise StockError("this lot changed while you were looking at it. Nothing was "
                         "changed.", code="lot_changed")
    occurred, _ = _now_pair()
    event_id = conn.execute(
        """INSERT INTO pharmacy_lot_events (lot_id, helix_medication_id, store_id,
               from_status, to_status, reason, actor_id, second_actor_id,
               reference_type, reference_id, occurred_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (lot_id, lot["medication_id"], lot["store_id"], current, to_status, why, actor,
         second, _text(reference_type), _text(reference_id), occurred),
    ).lastrowid
    fresh = conn.execute("SELECT * FROM helix_inventory_lots WHERE id = ?", (lot_id,)).fetchone()
    return {"lot": dict(fresh), "event_id": event_id}


MERGE_REFERENCE = "legacy_lot_merge"


def awaiting_adoption_sql(alias: str = "l") -> str:
    """SQL condition: a legacy lot (store NULL) that adoption still has to
    place. An emptied duplicate that was already merged into its batch stays
    store NULL for good (the store's unique batch index has no room for a
    second row of that batch) and is history, not stock waiting for a store,
    so it is left out. Should it ever hold units again it is counted again.
    No placeholders."""
    a = _alias(alias)
    return (
        f"{a}.store_id IS NULL AND NOT (COALESCE({a}.quantity, 0) = 0 AND EXISTS ("
        "SELECT 1 FROM pharmacy_lot_events ps_merge "
        f"WHERE ps_merge.lot_id = {a}.id AND ps_merge.reference_type = '{MERGE_REFERENCE}'))"
    )


def adopt_legacy_lots(conn, dispensing_store_id: int, *, actor_id: str) -> dict:
    """Put every legacy lot (store NULL) into the active dispensing store, in
    the caller's transaction. Returns {"store_id", "adopted": [lot ids],
    "merged": [{"from", "into", "units"}], "conflicts": [{...}]}.

    A legacy lot whose drug, batch and expiry the store already holds, or that
    repeats another legacy lot, is MERGED, never deleted: its units move by a
    paired transfer_out and transfer_in, and the emptied row stays with an
    event naming the lot it went into, so the ledger still adds up. A batch
    held in two statuses is left exactly as it is and reported as a conflict:
    merging it would release quarantined units or quarantine good ones. A
    duplicate merged by an earlier adoption is not merged again
    (awaiting_adoption_sql), so a second adoption writes nothing for it."""
    _require_transaction(conn)
    actor = _text(actor_id)
    if not actor:
        raise StockError("adoption names who made it", code="missing_actor")
    store = _pharmacy_store(conn, dispensing_store_id)
    if store["pharmacy_kind"] != DISPENSING_STORE_KIND or not store["is_active"]:
        raise StockError("legacy stock is adopted only by the active dispensing store",
                         code="not_dispensing_store")

    groups: dict[tuple, list] = {}
    for lot in conn.execute(
            f"SELECT * FROM helix_inventory_lots l WHERE {awaiting_adoption_sql('l')} "
            "ORDER BY l.rowid").fetchall():
        groups.setdefault((lot["medication_id"], lot["lot_number"], lot["expiration_date"]),
                          []).append(lot)

    adopted: list[str] = []
    merged: list[dict] = []
    conflicts: list[dict] = []
    occurred, _ = _now_pair()
    for (medication_id, batch, expiry), lots in groups.items():
        held = _same_identity_lot(conn, medication_id, batch, expiry, dispensing_store_id)
        statuses = {lot["status"] for lot in lots} | ({held["status"]} if held else set())
        if len(statuses) > 1:
            conflicts.append({"medication_id": medication_id, "lot_number": batch,
                              "expiration_date": expiry,
                              "lot_ids": [lot["id"] for lot in lots],
                              "reason": "the same batch is held in more than one status"})
            continue
        if held is not None:
            survivor, duplicates = held, lots
        else:
            survivor, duplicates = lots[0], lots[1:]
        if any(int(dup["quantity"] or 0) < 0 for dup in duplicates):
            conflicts.append({"medication_id": medication_id, "lot_number": batch,
                              "expiration_date": expiry,
                              "lot_ids": [lot["id"] for lot in lots],
                              "reason": "a duplicate lot holds a negative quantity"})
            continue
        if held is None:
            conn.execute("UPDATE helix_inventory_lots SET store_id = ? "
                         "WHERE id = ? AND store_id IS NULL",
                         (dispensing_store_id, survivor["id"]))
            adopted.append(survivor["id"])
        for dup in duplicates:
            units = int(dup["quantity"] or 0)
            if units > 0:
                post_movement(conn, lot_id=dup["id"], movement_type="transfer_out",
                              quantity_change=-units, actor_id=actor,
                              reference_type=MERGE_REFERENCE, reference_id=survivor["id"],
                              remarks="Merged into the same batch when the dispensing "
                                      "store adopted legacy stock")
                post_movement(conn, lot_id=survivor["id"], movement_type="transfer_in",
                              quantity_change=units, actor_id=actor,
                              reference_type=MERGE_REFERENCE, reference_id=dup["id"],
                              remarks="Merged from a duplicate legacy lot of the same batch")
            conn.execute(
                """INSERT INTO pharmacy_lot_events (lot_id, helix_medication_id, store_id,
                       from_status, to_status, reason, actor_id, reference_type,
                       reference_id, occurred_at)
                   VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?, ?)""",
                (dup["id"], medication_id, dup["status"], dup["status"],
                 f"Merged into lot {survivor['id']} when the dispensing store adopted "
                 "legacy stock", actor, MERGE_REFERENCE, survivor["id"], occurred))
            merged.append({"from": dup["id"], "into": survivor["id"], "units": units})
    return {"store_id": dispensing_store_id, "adopted": adopted, "merged": merged,
            "conflicts": conflicts}


# ═════════════════════════════════════════════════════════════════════════════
#  Reconciliation
# ═════════════════════════════════════════════════════════════════════════════

def _in_clause(values: Iterable[str]) -> tuple[str, list]:
    items = list(values)
    return ",".join("?" for _ in items), items


def reconcile(conn, *, lot_ids: Optional[Iterable[str]] = None) -> dict:
    """Check the ledger against the shelf. Read-only. `clean` is True only when
    every lot's quantity equals the sum of its movements, no lot is negative,
    every running balance is right, every movement is typed and sequenced,
    and no stored lot status, movement type or drug classification is outside
    this module's constants. `lot_ids` limits the lot checks to those lots
    (the drug-master check always runs over the whole master)."""
    lot_filter, lot_params = "", []
    move_filter = ""
    if lot_ids is not None:
        marks, lot_params = _in_clause(lot_ids)
        if not lot_params:
            marks = "NULL"
        lot_filter = f" WHERE l.id IN ({marks})"
        move_filter = f" AND lot_id IN ({marks})"

    mismatched = [dict(r) for r in conn.execute(
        "SELECT l.id AS lot_id, l.medication_id, COALESCE(l.quantity, 0) AS quantity, "
        "COALESCE((SELECT SUM(COALESCE(m.quantity_change, 0)) FROM helix_stock_movements m "
        "          WHERE m.lot_id = l.id), 0) AS ledger_units "
        f"FROM helix_inventory_lots l{lot_filter}", lot_params).fetchall()
        if r["quantity"] != r["ledger_units"]]
    negative = [r["lot_id"] for r in conn.execute(
        f"SELECT l.id AS lot_id FROM helix_inventory_lots l{lot_filter}"
        f"{' AND' if lot_filter else ' WHERE'} COALESCE(l.quantity, 0) < 0",
        lot_params).fetchall()]
    status_marks = ",".join("?" for _ in LOT_STATUSES)
    bad_statuses = [dict(r) for r in conn.execute(
        f"SELECT l.id AS lot_id, l.status FROM helix_inventory_lots l{lot_filter}"
        f"{' AND' if lot_filter else ' WHERE'} (l.status IS NULL OR l.status NOT IN "
        f"({status_marks}))", [*lot_params, *LOT_STATUSES]).fetchall()]

    untyped = int(conn.execute(
        "SELECT COUNT(*) FROM helix_stock_movements WHERE movement_type IS NULL"
        f"{move_filter}", lot_params).fetchone()[0])
    type_marks = ",".join("?" for _ in MOVEMENT_TYPES)
    unknown_types = sorted({r[0] for r in conn.execute(
        "SELECT DISTINCT movement_type FROM helix_stock_movements WHERE movement_type "
        f"IS NOT NULL AND movement_type NOT IN ({type_marks}){move_filter}",
        [*MOVEMENT_TYPES, *lot_params]).fetchall()})
    unsequenced = int(conn.execute(
        f"SELECT COUNT(*) FROM helix_stock_movements WHERE seq IS NULL{move_filter}",
        lot_params).fetchone()[0])

    breaks: list[dict] = []
    running: dict[str, int] = {}
    for r in conn.execute(
            "SELECT id, lot_id, quantity_change, balance_after_units FROM helix_stock_movements "
            f"WHERE lot_id IS NOT NULL AND seq IS NOT NULL{move_filter} ORDER BY seq",
            lot_params).fetchall():
        running[r["lot_id"]] = running.get(r["lot_id"], 0) + int(r["quantity_change"] or 0)
        if r["balance_after_units"] is None or int(r["balance_after_units"]) != running[r["lot_id"]]:
            breaks.append({"movement_id": r["id"], "lot_id": r["lot_id"],
                           "balance_after_units": r["balance_after_units"],
                           "running_units": running[r["lot_id"]]})

    bad_master: list[dict] = []
    for column, allowed in (("drug_schedule", DRUG_SCHEDULES),
                            ("ndps_category", NDPS_CATEGORIES),
                            ("storage_condition", STORAGE_CONDITIONS),
                            ("identity_status", IDENTITY_STATUSES)):
        marks = ",".join("?" for _ in allowed)
        for r in conn.execute(
                f"SELECT id, {column} AS value FROM helix_medication_master "
                f"WHERE {column} IS NOT NULL AND {column} NOT IN ({marks})", allowed).fetchall():
            bad_master.append({"medication_id": r["id"], "field": column, "value": r["value"]})

    lots_checked = int(conn.execute(
        f"SELECT COUNT(*) FROM helix_inventory_lots l{lot_filter}", lot_params).fetchone()[0])
    clean = not (mismatched or negative or bad_statuses or untyped or unknown_types
                 or unsequenced or breaks or bad_master)
    return {
        "clean": clean,
        "lots_checked": lots_checked,
        "mismatched_lots": mismatched,
        "negative_lots": negative,
        "bad_lot_statuses": bad_statuses,
        "untyped_movements": untyped,
        "unknown_movement_types": unknown_types,
        "unsequenced_movements": unsequenced,
        "balance_breaks": breaks,
        "bad_master_values": bad_master,
    }
