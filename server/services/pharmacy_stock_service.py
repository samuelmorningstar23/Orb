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

STOCK IS PER FACILITY (multisite-14, ruling MS-R9 (a)). Once more than one
facility is on record, a new lot takes its store's facility, a movement takes
its lot's, legacy stock is adopted only by the dispensing store of its own
facility, and a dispense passes the facility of the patient's admission
(facility_id=) so it takes only that facility's lots, still in date and
earliest expiry first; with none there it is refused, never drawn from
another facility (services/pharmacy_facility has the rule for where a lot
is). With one facility on record nothing here reads or writes anything new.

THE MOVEMENT HOOKS. post_movement calls, after its row is written and in the
same transaction:
  1. the NDPS register leg (grn-13, _ndps_register_leg), which is not in the
     registry and cannot be unregistered. Only while the register is on
     (ORB_PHARMACY_NDPS_REGISTER plus a failure mode in
     ORB_PHARMACY_NDPS_ON_FAILURE, ruling GRN-RS7) and only for a drug
     classified narcotic or psychotropic. With the register off it never calls
     into services/ndps_service at all. Its two failure modes:
       'block'  any failure to write the entry refuses the movement (a
                StockError, code ndps_register_failed or the register's own
                code); nothing changes;
       'record' for a DISPENSE only: the entry is written in a SAVEPOINT; on
                failure the savepoint rolls back, the dispense goes ahead, and
                an ndps_variances row plus a 'security' audit row are written
                in the same transaction (if either cannot be written, the
                dispense is refused). Every other movement blocks in both
                modes;
  2. every registered hook (register_movement_hook); an exception from one
     rolls the whole movement back.

WARD STOCK USED ON A DOSE (grn-17, behind ORB_PHARMACY_WARD_STOCK). After an
eMAR dose commits as 'given' for an order given from ward stock (the order's
supply source, MO-R12) and bound to a drug in the master (med_orders.
formulary_id), the eMAR books one unit off the patient's ward imprest store,
FEFO, in a transaction of its own (record_ward_consumption_in_txn). It never
blocks or delays the dose: the dose is already charted, and a failure here is
logged and leaves the dose as it is. This is the ONE decrement allowed to take
a lot below zero (post_movement's allow_negative, for 'consumption' on a ward
imprest lot only): when the ward's books hold no in-date unit, the use is
booked against the ward's own batch of the drug and the negative balance is
flagged on the ward stock overview and by reconcile(), never refused. Such a
use is never presented as certain for that batch (ward_consumption_for_batch).
"""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import Callable, Iterable
from datetime import UTC, date, datetime
from typing import Any

from services.clinical_time import format_utc, parse_to_utc

logger = logging.getLogger("orb.pharmacy_stock")

# ═════════════════════════════════════════════════════════════════════════════
#  Enumerations (no CHECK on the Helix tables, so these are the rule)
# ═════════════════════════════════════════════════════════════════════════════

#: helix_inventory_lots.status. 'available' is the only dispensable one.
LOT_STATUSES: tuple[str, ...] = (
    "available",
    "quarantined",
    "recalled",
    "nsq",
    "pending_inspection",
    "written_off",
    "returned_to_vendor",
)

#: helix_stock_movements.movement_type. 'op_sale' is reserved for the OPD
#: counter; 'consumption' for ward stock used on an eMAR dose (grn-17).
MOVEMENT_TYPES: tuple[str, ...] = (
    "opening",
    "receipt",
    "dispense",
    "transfer_out",
    "transfer_in",
    "ward_return",
    "vendor_return",
    "write_off",
    "adjustment",
    "count_variance",
    "consumption",
    "op_sale",
)

#: Movements that only ever add units, and those that only ever take them.
#: 'adjustment' and 'count_variance' go either way.
INWARD_MOVEMENT_TYPES = frozenset({"opening", "receipt", "transfer_in", "ward_return"})
OUTWARD_MOVEMENT_TYPES = frozenset(
    {
        "dispense",
        "transfer_out",
        "vendor_return",
        "write_off",
        "consumption",
        "op_sale",
    }
)

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
    "available": frozenset({"quarantined", "recalled", "nsq", "written_off", "returned_to_vendor"}),
    "quarantined": frozenset({"available", "recalled", "nsq", "written_off", "returned_to_vendor"}),
    "pending_inspection": frozenset(
        {"available", "quarantined", "written_off", "returned_to_vendor"}
    ),
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
RETURN_RESTOCK_ALLOWED = False  # GRN-RS3, strictest until ruled

_ALIAS = re.compile(r"^[a-z][a-z0-9_]*$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class StockError(ValueError):
    """A stock write the caller must correct or that the shelf refuses. Carries
    a stable `code` for the route layer. A ValueError, so every existing
    caller that answers ValueError with 400 keeps doing so."""

    code = "stock_error"

    def __init__(self, message: str, *, code: str | None = None):
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


def shelf_lot_sql(alias: str = "l", *, facility_id: int | None = None) -> str:
    """SQL condition: the lot is 'available' and sits on the dispensing shelf
    (see the module docstring). No placeholders. `alias` names the
    helix_inventory_lots row in the caller's query.

    With `facility_id` (multisite-14) the shelf is that facility's only: the
    lot's own facility and, for a lot in a store, its store's facility must
    both be it (the id is inlined as an integer, so there is still no
    placeholder). None, the default, is the condition exactly as before."""
    a = _alias(alias)
    sql = (
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
    if facility_id is None:
        return sql
    fid = int(facility_id)
    return (
        f"{sql} AND {a}.facility_id = {fid} AND ({a}.store_id IS NULL OR EXISTS ("
        f"SELECT 1 FROM stores ps_fac WHERE ps_fac.id = {a}.store_id "
        f"AND ps_fac.facility_id = {fid}))"
    )


def dispensable_lot_sql(alias: str = "l", *, facility_id: int | None = None) -> str:
    """SQL condition: the lot can be dispensed today. ONE placeholder, the
    hospital's date (pass site_today()). A lot with no recorded expiry fails
    the date test (date(NULL) is NULL), so it is refused rather than sorted
    anywhere. `facility_id` keeps it to one facility's shelf (shelf_lot_sql)
    and adds no placeholder."""
    a = _alias(alias)
    return (
        f"{a}.quantity > 0 AND date({a}.expiration_date) >= ? "
        f"AND {shelf_lot_sql(a, facility_id=facility_id)}"
    )


def pick_fefo_lot(
    conn: Any,
    medication_id: str,
    units: int = 1,
    *,
    lot_id: str | None = None,
    today: str | None = None,
    facility_id: int | None = None,
) -> dict | None:
    """The dispensable lot a dispense of `units` takes: the earliest expiry
    first, among lots holding at least `units` (a dispense is never split
    across lots). With `lot_id`, that lot only, and only if it qualifies.
    With `facility_id`, that facility's shelf only (multisite-14). None when
    nothing qualifies."""
    units = _whole(units, "units")
    if units < 1:
        raise StockError("a dispense takes at least one unit", code="bad_quantity")
    sql = (
        f"SELECT l.* FROM helix_inventory_lots l WHERE l.medication_id = ? "
        f"AND {dispensable_lot_sql('l', facility_id=facility_id)} AND l.quantity >= ?"
    )
    params: list[Any] = [medication_id, today or site_today(), units]
    if lot_id is not None:
        sql += " AND l.id = ?"
        params.append(lot_id)
    sql += " ORDER BY date(l.expiration_date) ASC, l.rowid ASC LIMIT 1"
    row = conn.execute(sql, params).fetchone()
    return dict(row) if row else None


# ═════════════════════════════════════════════════════════════════════════════
#  A dispense the pharmacist shapes: quantity and batch (grn-14)
# ═════════════════════════════════════════════════════════════════════════════

#: Every refusal choose_dispense_lot can give, with the HTTP status the Helix
#: route answers. 422: the request names something that can never be
#: dispensed for this order. 409: the shelf cannot meet it as asked right now
#: (a dispense is never split between batches, so a smaller quantity or
#: another batch may still work).
DISPENSE_REFUSAL_STATUS: dict[str, int] = {
    "bad_quantity": 422,
    "lot_not_found": 422,
    "lot_wrong_drug": 422,
    "lot_not_dispensable": 422,
    "insufficient_lot_stock": 409,
}

#: A lot status in the words a refusal uses ("Batch B12 is quarantined").
_LOT_STATUS_WORDS = {
    "quarantined": "quarantined",
    "recalled": "recalled",
    "nsq": "marked not of standard quality",
    "pending_inspection": "waiting for inspection",
    "written_off": "written off",
    "returned_to_vendor": "returned to the vendor",
}

#: Where a lot off the dispensing shelf is held, in the refusal's words.
_STORE_KIND_WORDS = {
    "sub_store": "a sub-store",
    "ndps_vault": "the NDPS vault",
    "returns_quarantine": "returns quarantine",
    "ward_imprest": "ward stock",
    "crash_cart": "a crash cart",
    "dispensing": "the dispensing pharmacy",
}


def _units_words(units: int) -> str:
    return f"{units} unit" if units == 1 else f"{units} units"


def _lot_label(lot: Any) -> str:
    number = (lot["lot_number"] or "").strip() if lot["lot_number"] is not None else ""
    return f"Batch {number}" if number else "This batch"


def _not_dispensable_words(conn, lot: Any, today: str) -> str:
    """Why a lot of the right drug cannot be dispensed today, for the
    pharmacist: its status, then where it is, then its expiry."""
    label = _lot_label(lot)
    status = lot["status"] or "available"
    if status != "available":
        words = _LOT_STATUS_WORDS.get(status, "not available")
        return f"{label} is {words}, so it cannot be dispensed. Choose another batch."
    on_shelf = conn.execute(
        f"SELECT 1 FROM helix_inventory_lots l WHERE l.id = ? AND {shelf_lot_sql('l')}",
        (lot["id"],),
    ).fetchone()
    if on_shelf is None:
        where = "another store"
        if lot["store_id"] is not None:
            store = conn.execute(
                "SELECT pharmacy_kind, is_active FROM stores WHERE id = ?", (lot["store_id"],)
            ).fetchone()
            if store is not None and store["is_active"] and store["pharmacy_kind"]:
                where = _STORE_KIND_WORDS.get(store["pharmacy_kind"], "another store")
            elif store is not None:
                where = "a store that is switched off"
        return (
            f"{label} is held in {where}, not on the dispensing shelf, so it cannot be "
            f"dispensed from here. Choose another batch."
        )
    expiry = lot["expiration_date"]
    parsed = None
    try:
        parsed = date.fromisoformat(str(expiry)[:10]) if expiry else None
    except ValueError:
        parsed = None
    if parsed is None:
        return (
            f"{label} has no expiry date recorded, so it cannot be dispensed. Choose another batch."
        )
    if parsed.isoformat() < today:
        return f"{label} expired on {parsed.isoformat()}, so it cannot be dispensed."
    return f"{label} cannot be dispensed today. Choose another batch."


def _at_facility(conn: Any, lot: Any, facility_id: int) -> bool:
    """Is this lot at `facility_id` (its own facility and, in a store, its
    store's: services/pharmacy_facility.lot_facility)?"""
    from services import pharmacy_facility

    found = pharmacy_facility.lot_facility(conn, lot["id"])
    return found is not None and int(found) == int(facility_id)


def choose_dispense_lot(
    conn: Any,
    medication_id: str,
    units: int,
    *,
    lot_id: str | None = None,
    today: str | None = None,
    facility_id: int | None = None,
) -> dict | None:
    """The lot a dispense of `units` of `medication_id` takes, chosen on the
    caller's connection under its write lock (grn-14). Writes nothing.

    `medication_id` is the EFFECTIVE drug: the substitute when pharmacy set
    one, else the drug prescribed. That is the precedence the drug-safety
    screen uses, so the batch that leaves the shelf is always a batch of the
    drug that was screened.

    With `lot_id` (the pharmacist's choice), that lot only, or a StockError:
      lot_not_found           no such lot;
      lot_wrong_drug          the lot holds another drug, the drug prescribed
                              included when a substitute is set;
      lot_not_dispensable     not 'available' (quarantined, recalled, NSQ,
                              waiting for inspection), not on the dispensing
                              shelf (ward stock, a sub-store, returns
                              quarantine), expired, or no expiry recorded;
      insufficient_lot_stock  the lot holds fewer than `units`.
    Without `lot_id`: the earliest-expiry dispensable lot holding at least
    `units` (pick_fefo_lot), or None. Either way a dispense is NEVER split
    between lots.

    With `facility_id` (multisite-14) only that facility's shelf counts: a
    batch the pharmacist names that is at another facility is refused as
    lot_not_found, the same answer as a batch that is not on file.
    """
    units = _whole(units, "units")
    if units < 1:
        raise StockError("A dispense takes at least one unit.", code="bad_quantity")
    today = today or site_today()
    if lot_id is None:
        return pick_fefo_lot(conn, medication_id, units, today=today, facility_id=facility_id)
    lot = conn.execute("SELECT * FROM helix_inventory_lots WHERE id = ?", (lot_id,)).fetchone()
    if lot is None or (facility_id is not None and not _at_facility(conn, lot, facility_id)):
        raise StockError(
            "That batch is not in the pharmacy's stock. Reload the batches and choose again.",
            code="lot_not_found",
        )
    if lot["medication_id"] != medication_id:
        raise StockError(
            f"{_lot_label(lot)} is a batch of another drug, not the drug this "
            "order dispenses. Choose a batch of this drug.",
            code="lot_wrong_drug",
        )
    on_date_and_shelf = conn.execute(
        f"SELECT 1 FROM helix_inventory_lots l WHERE l.id = ? "
        f"AND date(l.expiration_date) >= ? AND {shelf_lot_sql('l', facility_id=facility_id)}",
        (lot_id, today),
    ).fetchone()
    if on_date_and_shelf is None:
        raise StockError(_not_dispensable_words(conn, lot, today), code="lot_not_dispensable")
    held = int(lot["quantity"] or 0)
    if held < units:
        raise StockError(
            f"{_lot_label(lot)} holds {_units_words(held)}; this dispense needs "
            f"{_units_words(units)}. A dispense is never split between batches: choose "
            "another batch or a smaller quantity.",
            code="insufficient_lot_stock",
        )
    return dict(lot)


def dispensable_lots(
    conn: Any, medication_id: str, *, today: str | None = None, facility_id: int | None = None
) -> list[dict]:
    """Every lot of one drug a dispense could take today, earliest expiry
    first (the order pick_fefo_lot reads them in), for the pharmacist's batch
    picker; with `facility_id`, that facility's shelf only. Lot fields and
    the store's name only: no patient data."""
    rows = conn.execute(
        f"SELECT l.id, l.lot_number, l.expiration_date, l.quantity, l.store_id, "
        f"s.name AS store_name FROM helix_inventory_lots l "
        f"LEFT JOIN stores s ON s.id = l.store_id "
        f"WHERE l.medication_id = ? AND {dispensable_lot_sql('l', facility_id=facility_id)} "
        f"ORDER BY date(l.expiration_date) ASC, l.rowid ASC",
        (medication_id, today or site_today()),
    ).fetchall()
    return [dict(r) for r in rows]


def dispensable_units(
    conn: Any, medication_id: str, *, today: str | None = None, facility_id: int | None = None
) -> int:
    """Units of one drug a dispense could take today, across all its lots
    (with `facility_id`, that facility's lots only)."""
    row = conn.execute(
        f"SELECT SUM(l.quantity) AS total FROM helix_inventory_lots l "
        f"WHERE l.medication_id = ? AND {dispensable_lot_sql('l', facility_id=facility_id)}",
        (medication_id, today or site_today()),
    ).fetchone()
    total = row["total"] if row else None
    return int(total) if total is not None else 0


def undispensable_shelf_units(
    conn: Any, medication_id: str, *, today: str | None = None, facility_id: int | None = None
) -> int:
    """Units of one drug on the dispensing shelf that cannot be dispensed
    because they are expired or carry no expiry. Stock elsewhere (a sub-store,
    quarantine, another status, another facility's shelf when `facility_id`
    is given) is not counted: it is not on the shelf."""
    row = conn.execute(
        f"SELECT SUM(l.quantity) AS total FROM helix_inventory_lots l "
        f"WHERE l.medication_id = ? AND l.quantity > 0 "
        f"AND {shelf_lot_sql('l', facility_id=facility_id)} "
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


def _multi_facility(conn: Any) -> bool:
    """More than one facility on record (multisite-14), read from the cached
    count on the caller's own connection, so it never opens a second session
    inside the caller's transaction."""
    from services import pharmacy_facility

    return pharmacy_facility.multi(conn)


def _whole(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise StockError(f"{name} must be a whole number", code="bad_quantity")
    return value


def _text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _date_text(value: object, name: str) -> str | None:
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
        moment = datetime.now(UTC)
    else:
        if not isinstance(occurred_at, (str, datetime)):
            raise TypeError("Expected a timestamp string or datetime")
        moment = parse_to_utc(occurred_at, naive_tz=UTC)
    return format_utc(moment), moment.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")


def _row(row) -> dict | None:
    return dict(row) if row is not None else None


# ═════════════════════════════════════════════════════════════════════════════
#  The movement hook registry
# ═════════════════════════════════════════════════════════════════════════════

MovementHook = Callable[[Any, dict], None]
_MOVEMENT_HOOKS: dict[str, MovementHook] = {}


def register_movement_hook(name: str, hook: MovementHook | None) -> None:
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
    conn: Any,
    *,
    lot_id: str,
    movement_type: str,
    quantity_change: int,
    actor_id: str,
    reference_type: str | None = None,
    reference_id: str | None = None,
    order_id: str | None = None,
    idempotency_key: str | None = None,
    remarks: str | None = None,
    unit_cost_paise: int | None = None,
    occurred_at: str | datetime | None = None,
    allow_negative: bool = False,
) -> dict:
    """Move `quantity_change` units into (+) or out of (-) one lot, and write
    the ledger row that says so. Returns the movement row as a dict.

    A replay of an `idempotency_key` already in the ledger writes nothing and
    returns the earlier row with `replayed` True; the same key on a different
    lot, type or quantity is refused. `order_id` is the Helix order a dispense
    belongs to (the legacy column billing and the ward receipt read).

    `allow_negative` lets the lot go below zero instead of refusing. Only a
    'consumption' on a ward imprest lot may ask for it (grn-17: a dose already
    given is booked, and the gap is flagged, never refused); any other movement
    asking for it is refused (negative_not_allowed)."""
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
        raise StockError(
            "every movement names the person or process that made it", code="missing_actor"
        )
    if unit_cost_paise is not None:
        unit_cost_paise = _whole(unit_cost_paise, "unit_cost_paise")
        if unit_cost_paise < 0:
            raise StockError("a unit cost is never negative", code="bad_cost")
    key = _text(idempotency_key)

    if key:
        prior = conn.execute(
            "SELECT * FROM helix_stock_movements WHERE idempotency_key = ?", (key,)
        ).fetchone()
        if prior is not None:
            if (prior["lot_id"], prior["movement_type"], prior["quantity_change"]) != (
                lot_id,
                movement_type,
                change,
            ):
                raise StockError(
                    "this request key was already used for a different stock "
                    "movement. Nothing was changed.",
                    code="idempotency_conflict",
                )
            replay = dict(prior)
            replay["replayed"] = True
            return replay

    lot = conn.execute(
        "SELECT id, medication_id, store_id FROM helix_inventory_lots WHERE id = ?",
        (lot_id,),
    ).fetchone()
    if lot is None:
        raise StockError(f"no lot {lot_id}", code="lot_not_found")

    if allow_negative:
        _check_negative_allowed(conn, movement_type, lot)
        cur = conn.execute(
            "UPDATE helix_inventory_lots SET quantity = COALESCE(quantity, 0) + ? WHERE id = ?",
            (change, lot_id),
        )
    else:
        # Units coming IN are never refused: a ward batch booked below zero
        # (grn-17) must still take its next delivery, which brings it back up.
        cur = conn.execute(
            "UPDATE helix_inventory_lots SET quantity = COALESCE(quantity, 0) + ? "
            "WHERE id = ? AND (? > 0 OR COALESCE(quantity, 0) + ? >= 0)",
            (change, lot_id, change, change),
        )
    if cur.rowcount != 1:
        raise InsufficientStock(
            "this lot does not hold enough units for that. Nothing was changed."
        )
    balance = int(
        conn.execute(
            "SELECT quantity FROM helix_inventory_lots WHERE id = ?", (lot_id,)
        ).fetchone()[0]
    )
    seq = int(
        conn.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM helix_stock_movements").fetchone()[0]
    )
    occurred, legacy_stamp = _now_pair(occurred_at)
    movement_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO helix_stock_movements (
               id, lot_id, medication_id, order_id, quantity_change, reason, timestamp,
               movement_type, store_id, balance_after_units, unit_cost_paise,
               reference_type, reference_id, actor_id, remarks, idempotency_key,
               occurred_at, seq)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            movement_id,
            lot_id,
            lot["medication_id"],
            order_id,
            change,
            movement_type,
            legacy_stamp,
            movement_type,
            lot["store_id"],
            balance,
            unit_cost_paise,
            _text(reference_type),
            _text(reference_id),
            actor,
            _text(remarks),
            key,
            occurred,
            seq,
        ),
    )
    if _multi_facility(conn):
        # multisite-14: a movement is at its lot's facility. One facility on
        # record keeps the column's default and runs no extra statement.
        conn.execute(
            "UPDATE helix_stock_movements SET facility_id = COALESCE((SELECT l.facility_id "
            "FROM helix_inventory_lots l WHERE l.id = ?), facility_id) WHERE id = ?",
            (lot_id, movement_id),
        )
    movement = dict(
        conn.execute("SELECT * FROM helix_stock_movements WHERE id = ?", (movement_id,)).fetchone()
    )
    _ndps_register_leg(conn, movement)
    for hook in list(_MOVEMENT_HOOKS.values()):
        hook(conn, movement)
    return movement


#: The savepoint the NDPS register leg runs in under the 'record' mode.
NDPS_SAVEPOINT = "ndps_register_leg"


def _ndps_register_leg(conn, movement: dict) -> None:
    """The NDPS register entry for this movement (grn-13), with the failure
    mode ruling GRN-RS7 names (see the module docstring). A no-op unless the
    register is on and the drug is classified narcotic or psychotropic."""
    from services import ndps_service

    if not ndps_service.register_on():
        return
    if not ndps_service.is_controlled(conn, movement["medication_id"]):
        return
    if (
        ndps_service.failure_mode() == "record"
        and movement["movement_type"] in ndps_service.RECORD_MODE_MOVEMENTS
    ):
        conn.execute(f"SAVEPOINT {NDPS_SAVEPOINT}")
        try:
            ndps_service.write_movement_entry(conn, movement)
        except Exception as exc:  # the register leg only; the movement stands
            conn.execute(f"ROLLBACK TO SAVEPOINT {NDPS_SAVEPOINT}")
            conn.execute(f"RELEASE SAVEPOINT {NDPS_SAVEPOINT}")
            ndps_service.record_failure(conn, movement, exc)
            return
        conn.execute(f"RELEASE SAVEPOINT {NDPS_SAVEPOINT}")
        return
    try:
        ndps_service.write_movement_entry(conn, movement)
    except ndps_service.NdpsError as exc:
        raise StockError(
            f"the NDPS register could not record this, so nothing was changed: {exc}", code=exc.code
        ) from exc
    except Exception as exc:
        raise StockError(
            "the NDPS register could not record this, so nothing was changed.",
            code="ndps_register_failed",
        ) from exc


def _pharmacy_store(conn, store_id: int):
    row = conn.execute(
        "SELECT id, code, name, pharmacy_kind, is_active FROM stores WHERE id = ?",
        (store_id,),
    ).fetchone()
    if row is None or row["pharmacy_kind"] is None:
        raise StockError(f"no pharmacy store {store_id}", code="store_not_found")
    return row


def _same_identity_lot(
    conn, medication_id: str, lot_number: str, expiry: str | None, store_id: int | None
):
    return conn.execute(
        "SELECT * FROM helix_inventory_lots WHERE medication_id = ? AND lot_number = ? "
        "AND expiration_date IS ? AND store_id IS ? ORDER BY rowid LIMIT 1",
        (medication_id, lot_number, expiry, store_id),
    ).fetchone()


def open_or_merge_lot(
    conn: Any,
    *,
    medication_id: str,
    lot_number: str,
    expiration_date: object,
    units: int,
    actor_id: str,
    store_id: int | None = None,
    lot_id: str | None = None,
    status: str = "available",
    movement_type: str = "opening",
    units_per_pack: int | None = None,
    mrp_pack_paise: int | None = None,
    unit_cost_paise: int | None = None,
    mfg_date: object = None,
    vendor_id: int | None = None,
    receipt_line_ref: int | None = None,
    received_at: object = None,
    reference_type: str | None = None,
    reference_id: str | None = None,
    idempotency_key: str | None = None,
    remarks: str | None = None,
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
        raise StockError(
            f"a lot is opened by an inward movement, not {movement_type!r}",
            code="bad_movement_type",
        )
    if status not in LOT_STATUSES:
        raise StockError(f"unknown lot status {status!r}", code="bad_status")
    units = _whole(units, "units")
    if units < 0:
        raise StockError("a lot is opened with zero or more units", code="bad_quantity")
    batch = _text(lot_number)
    if not batch:
        raise StockError("a lot needs its batch number", code="missing_batch")
    expiry = _date_text(expiration_date, "expiry date")
    if (
        conn.execute(
            "SELECT 1 FROM helix_medication_master WHERE id = ?", (medication_id,)
        ).fetchone()
        is None
    ):
        raise StockError(f"no drug {medication_id} in the formulary", code="drug_not_found")
    if store_id is not None:
        _pharmacy_store(conn, store_id)
    for name, value in (
        ("units_per_pack", units_per_pack),
        ("mrp_pack_paise", mrp_pack_paise),
        ("unit_cost_paise", unit_cost_paise),
    ):
        if value is not None and _whole(value, name) < 0:
            raise StockError(f"{name} is never negative", code="bad_quantity")
    if units_per_pack is not None and units_per_pack < 1:
        raise StockError("units per pack is at least 1", code="bad_pack_size")

    key = _text(idempotency_key)
    if key:
        prior = conn.execute(
            "SELECT * FROM helix_stock_movements WHERE idempotency_key = ?", (key,)
        ).fetchone()
        if prior is not None:
            lot = conn.execute(
                "SELECT * FROM helix_inventory_lots WHERE id = ?", (prior["lot_id"],)
            ).fetchone()
            replay = dict(prior)
            replay["replayed"] = True
            return {"lot": _row(lot), "movement": replay, "merged": True}

    existing = None
    if lot_id is not None:
        existing = conn.execute(
            "SELECT * FROM helix_inventory_lots WHERE id = ?", (lot_id,)
        ).fetchone()
        if existing is not None and (
            existing["medication_id"],
            existing["lot_number"],
            existing["expiration_date"],
            existing["store_id"],
        ) != (medication_id, batch, expiry, store_id):
            raise StockError(
                f"lot id {lot_id} is already a different batch. Nothing was changed.",
                code="lot_id_taken",
            )
        if existing is None and store_id is not None:
            # One lot per drug, batch and expiry in a store (migration 54's
            # partial unique index): a second id for the same batch is refused
            # here, in words, rather than at the index.
            twin = _same_identity_lot(conn, medication_id, batch, expiry, store_id)
            if twin is not None:
                raise StockError(
                    f"batch {batch} is already held in this store as lot "
                    f"{twin['id']}. Nothing was changed.",
                    code="batch_exists",
                )
    else:
        existing = _same_identity_lot(conn, medication_id, batch, expiry, store_id)

    if existing is not None and existing["status"] != status:
        raise StockError(
            f"batch {batch} is already held here as {existing['status'].replace('_', ' ')} "
            "stock and is not merged with stock of another status. Nothing was changed.",
            code="lot_status_conflict",
        )

    merged = existing is not None
    if merged:
        assert existing is not None
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
            (
                target_id,
                medication_id,
                batch,
                expiry,
                store_id,
                status,
                units_per_pack,
                mrp_pack_paise,
                unit_cost_paise,
                _date_text(mfg_date, "manufacture date"),
                vendor_id,
                receipt_line_ref,
                _now_pair(received_at)[0] if received_at is not None else None,
                created,
            ),
        )
        if store_id is not None and _multi_facility(conn):
            # multisite-14: a lot is at its store's facility (a goods receipt
            # into a facility store, a transfer or a ward return landing
            # there). A lot opened in no store keeps facility 1's default.
            conn.execute(
                "UPDATE helix_inventory_lots SET facility_id = COALESCE((SELECT s.facility_id "
                "FROM stores s WHERE s.id = ?), facility_id) WHERE id = ?",
                (store_id, target_id),
            )

    movement = None
    if units > 0:
        movement = post_movement(
            conn,
            lot_id=target_id,
            movement_type=movement_type,
            quantity_change=units,
            actor_id=actor_id,
            reference_type=reference_type,
            reference_id=reference_id,
            idempotency_key=key,
            remarks=remarks,
            unit_cost_paise=unit_cost_paise,
        )
    lot = conn.execute("SELECT * FROM helix_inventory_lots WHERE id = ?", (target_id,)).fetchone()
    return {"lot": dict(lot), "movement": movement, "merged": merged}


# ═════════════════════════════════════════════════════════════════════════════
#  Goods received from a vendor (grn-8)
# ═════════════════════════════════════════════════════════════════════════════

#: reference_type on the receipt movement of a pharmacy goods receipt line.
GRN_REFERENCE = "pharmacy_grn"

#: Pharmacy store kinds a vendor delivery lands in (pharmacy purchasing's
#: ORDERABLE_STORE_KINDS): returns quarantine holds returns, and ward stores are
#: filled by transfers, never by a vendor.
RECEIVING_STORE_KINDS: tuple[str, ...] = (DISPENSING_STORE_KIND, "sub_store", NDPS_VAULT_KIND)


def _batch_key(text: object) -> str:
    """A batch number compared without case or spaces: 'b 123a' is 'B123A'."""
    return "".join(str(text or "").upper().split())


def batch_conflict(
    conn,
    *,
    medication_id: str,
    lot_number: str,
    expiration_date: object,
    store_id: int | None = None,
    mrp_pack_paise: int | None = None,
) -> dict | None:
    """Why this batch of this drug cannot come in, or None. Read-only.

      * batch_expiry_mismatch: the drug's batch is already on file, in any
        store and any status, with ANOTHER expiry. A batch has one expiry, so
        one of the two dates is a typing error, and neither is guessed at.
      * batch_mrp_mismatch: the batch is already held in this store at another
        MRP per pack. A batch carries one printed MRP, and the patient is
        billed at it (GRN-R12).
    Returns {code, message, lot_id, held_expiry, held_mrp_pack_paise, spelling},
    where spelling is how the batch is already written on file."""
    expiry = _date_text(expiration_date, "expiry date")
    want = _batch_key(lot_number)
    for row in conn.execute(
        "SELECT id, lot_number, expiration_date, store_id, mrp_pack_paise "
        "FROM helix_inventory_lots WHERE medication_id = ? ORDER BY rowid",
        (medication_id,),
    ).fetchall():
        if _batch_key(row["lot_number"]) != want:
            continue
        held = str(row["expiration_date"])[:10] if row["expiration_date"] else None
        if held != expiry:
            return {
                "code": "batch_expiry_mismatch",
                "lot_id": row["id"],
                "held_expiry": held,
                "held_mrp_pack_paise": row["mrp_pack_paise"],
                "spelling": row["lot_number"],
                "message": (
                    f"batch {row['lot_number']} of this drug is already on file "
                    f"with expiry {held or 'not recorded'}. One batch has one "
                    "expiry date: check the pack against the earlier receipt. "
                    "Nothing was changed."
                ),
            }
        if (
            store_id is not None
            and row["store_id"] == store_id
            and mrp_pack_paise is not None
            and row["mrp_pack_paise"] is not None
            and int(row["mrp_pack_paise"]) != int(mrp_pack_paise)
        ):
            return {
                "code": "batch_mrp_mismatch",
                "lot_id": row["id"],
                "held_expiry": held,
                "held_mrp_pack_paise": int(row["mrp_pack_paise"]),
                "spelling": row["lot_number"],
                "message": (
                    f"batch {row['lot_number']} is already held in this store at "
                    "another MRP per pack. One batch carries one printed MRP: "
                    "check the pack. Nothing was changed."
                ),
            }
    return None


def batch_spelling(conn, medication_id: str, lot_number: object) -> str:
    """How a batch is written on the lot: the spelling already on file for
    this drug when the batch is known (so 'b123' merges with 'B123'), else the
    typed batch in capitals with single spaces."""
    want = _batch_key(lot_number)
    for row in conn.execute(
        "SELECT lot_number FROM helix_inventory_lots WHERE medication_id = ? ORDER BY rowid",
        (medication_id,),
    ).fetchall():
        if _batch_key(row["lot_number"]) == want:
            return row["lot_number"]
    return " ".join(str(lot_number or "").upper().split())


def receive_grn_lines(
    conn,
    *,
    grn_id: int,
    store_id: int,
    vendor_id: int | None,
    lines: Iterable[dict],
    actor_id: str,
    today: str | None = None,
    received_at: object = None,
) -> list[dict]:
    """Put the accepted lines of a posted pharmacy goods receipt on the shelf,
    in the caller's transaction (grn-8). Returns one dict per line:
    {line_no, lot_id, movement_id, merged, units, unit_cost_paise}.

    Each line is {line_no, line_id, medication_id, batch_no, expiry_date,
    units (paid plus free, in dispense units), units_per_pack, mrp_pack_paise,
    unit_cost_paise, mfg_date}. Every line becomes a 'receipt' movement into
    the receipt's store at status 'available', keyed
    'pharmacy_grn:<grn>:line:<n>' so it can never land twice. The same drug,
    batch and expiry already in that store merges into one lot, whose unit cost
    becomes the weighted cost of what it then holds (GRN-R12, lot cost).

    Refused (StockError, so the caller's whole transaction rolls back): a store
    that does not take vendor deliveries; a narcotic or psychotropic outside an
    NDPS vault, or anything else inside one (GRN-R14); an expiry before the
    hospital's date; a batch already on file with another expiry or, in this
    store, another MRP; and a merge into a lot of another status."""
    _require_transaction(conn)
    actor = _text(actor_id)
    if not actor:
        raise StockError("a receipt names who received it", code="missing_actor")
    store = _pharmacy_store(conn, store_id)
    if not store["is_active"]:
        raise StockError(f"{store['name']} is closed", code="store_inactive")
    if store["pharmacy_kind"] not in RECEIVING_STORE_KINDS:
        raise StockError(
            f"{store['name']} does not take deliveries from vendors", code="store_not_orderable"
        )
    vault = store["pharmacy_kind"] == NDPS_VAULT_KIND
    day = today or site_today()
    results: list[dict] = []
    for line in lines:
        medication_id = line["medication_id"]
        units = _whole(line.get("units"), "units")
        if units < 1:
            raise StockError("a receipt line brings in at least one unit", code="bad_quantity")
        drug = conn.execute(
            "SELECT id, name, ndps_category FROM helix_medication_master WHERE id = ?",
            (medication_id,),
        ).fetchone()
        if drug is None:
            raise StockError(f"no drug {medication_id} in the formulary", code="drug_not_found")
        controlled = drug["ndps_category"] in NDPS_CONTROLLED
        if controlled and not vault:
            raise StockError(
                f"{drug['name']} is a narcotic or psychotropic drug and is received "
                "only into the NDPS vault",
                code="ndps_needs_vault",
            )
        if vault and not controlled:
            raise StockError(
                f"{drug['name']} is not a narcotic or psychotropic drug, so it is "
                "not received into the NDPS vault",
                code="vault_is_for_ndps",
            )
        expiry = _date_text(line.get("expiry_date"), "expiry date")
        if expiry is None:
            raise StockError("a drug is received with its expiry date", code="expiry_required")
        if expiry < day:
            raise StockError(
                f"batch {line.get('batch_no')} expired on {expiry}; expired stock "
                "is never received",
                code="expired_on_receipt",
            )
        mrp = line.get("mrp_pack_paise")
        conflict = batch_conflict(
            conn,
            medication_id=medication_id,
            lot_number=line["batch_no"],
            expiration_date=expiry,
            store_id=store_id,
            mrp_pack_paise=mrp,
        )
        if conflict:
            raise StockError(conflict["message"], code=conflict["code"])
        batch = batch_spelling(conn, medication_id, line.get("batch_no"))
        cost = line.get("unit_cost_paise")
        opened = open_or_merge_lot(
            conn,
            medication_id=medication_id,
            lot_number=batch,
            expiration_date=expiry,
            units=units,
            actor_id=actor,
            store_id=store_id,
            status="available",
            movement_type="receipt",
            units_per_pack=line.get("units_per_pack"),
            mrp_pack_paise=mrp,
            unit_cost_paise=cost,
            mfg_date=line.get("mfg_date"),
            vendor_id=vendor_id,
            receipt_line_ref=line.get("line_id"),
            received_at=received_at,
            reference_type=GRN_REFERENCE,
            reference_id=str(grn_id),
            idempotency_key=f"{GRN_REFERENCE}:{grn_id}:line:{line['line_no']}",
            remarks=f"Received on goods receipt {grn_id}, line {line['line_no']}",
        )
        lot, movement = opened["lot"], opened["movement"] or {}
        if opened["merged"] and not movement.get("replayed"):
            after = int(lot["quantity"] or 0)
            before = after - units
            held_cost = lot["unit_cost_paise"]
            if cost is not None:
                weighted = (
                    cost
                    if (held_cost is None or before <= 0)
                    else ((before * int(held_cost) + units * int(cost) + after // 2) // after)
                )
                conn.execute(
                    "UPDATE helix_inventory_lots SET unit_cost_paise = ?, "
                    "mrp_pack_paise = COALESCE(mrp_pack_paise, ?), "
                    "units_per_pack = COALESCE(units_per_pack, ?) WHERE id = ?",
                    (weighted, mrp, line.get("units_per_pack"), lot["id"]),
                )
                lot = dict(
                    conn.execute(
                        "SELECT * FROM helix_inventory_lots WHERE id = ?", (lot["id"],)
                    ).fetchone()
                )
        results.append(
            {
                "line_no": line["line_no"],
                "lot_id": lot["id"],
                "movement_id": movement.get("id"),
                "merged": opened["merged"],
                "units": units,
                "unit_cost_paise": lot["unit_cost_paise"],
                "replayed": bool(movement.get("replayed")),
            }
        )
    return results


def _quarantined_by(conn, lot_id: str) -> str | None:
    row = conn.execute(
        "SELECT actor_id FROM pharmacy_lot_events WHERE lot_id = ? AND to_status = 'quarantined' "
        "ORDER BY id DESC LIMIT 1",
        (lot_id,),
    ).fetchone()
    return row["actor_id"] if row else None


def set_lot_status(
    conn,
    lot_id: str,
    to_status: str,
    *,
    actor_id: str,
    reason: str,
    second_actor_id: str | None = None,
    reference_type: str | None = None,
    reference_id: str | None = None,
    today: str | None = None,
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
        (lot_id,),
    ).fetchone()
    if lot is None:
        raise StockError(f"no lot {lot_id}", code="lot_not_found")
    current = lot["status"]
    if current == to_status:
        raise StockError(f"this lot is already {to_status.replace('_', ' ')}", code="same_status")
    if to_status not in LOT_STATUS_TRANSITIONS.get(current, frozenset()):
        raise StockError(
            f"a {str(current).replace('_', ' ')} lot cannot become {to_status.replace('_', ' ')}",
            code="illegal_lot_transition",
        )
    if to_status in EMPTY_ONLY_STATUSES and int(lot["quantity"] or 0) != 0:
        raise StockError(
            "this lot still holds units. Record the write-off or vendor return movement "
            "first, then close the lot.",
            code="lot_not_empty",
        )
    if current == "quarantined" and to_status == "available":
        if not QUARANTINE_RELEASE_ALLOWED:
            raise StockError(
                "a quarantined lot is not released back to the shelf on this site. Write it "
                "off or return it to the vendor. Nothing was changed.",
                code="release_not_allowed",
            )
        if _quarantined_by(conn, lot_id) == actor:
            raise StockError(
                "the person who quarantined this lot cannot also release it. Ask another "
                "pharmacist to release it.",
                code="same_person_release",
            )
    if current == "pending_inspection" and to_status == "available":
        if not RETURN_RESTOCK_ALLOWED:
            raise StockError(
                "a returned medicine is not put back on the shelf on this site. Write it "
                "off or return it to the vendor. Nothing was changed.",
                code="restock_not_allowed",
            )
        expiry = lot["expiration_date"]
        if lot["ndps_category"] in NDPS_CONTROLLED:
            raise StockError("a returned NDPS drug is never restocked", code="restock_refused")
        if lot["storage_condition"] in COLD_CHAIN_STORAGE:
            raise StockError(
                "a returned cold-chain drug is never restocked", code="restock_refused"
            )
        if not expiry or str(expiry)[:10] < (today or site_today()):
            raise StockError(
                "an expired or undated return is never restocked", code="restock_refused"
            )

    cur = conn.execute(
        "UPDATE helix_inventory_lots SET status = ? WHERE id = ? AND status = ?",
        (to_status, lot_id, current),
    )
    if cur.rowcount != 1:
        raise StockError(
            "this lot changed while you were looking at it. Nothing was changed.",
            code="lot_changed",
        )
    occurred, _ = _now_pair()
    event_id = conn.execute(
        """INSERT INTO pharmacy_lot_events (lot_id, helix_medication_id, store_id,
               from_status, to_status, reason, actor_id, second_actor_id,
               reference_type, reference_id, occurred_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            lot_id,
            lot["medication_id"],
            lot["store_id"],
            current,
            to_status,
            why,
            actor,
            second,
            _text(reference_type),
            _text(reference_id),
            occurred,
        ),
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


def adopt_legacy_lots(conn: Any, dispensing_store_id: int, *, actor_id: str) -> dict:
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
    (awaiting_adoption_sql), so a second adoption writes nothing for it.

    Once more than one facility is on record (multisite-14), a dispensing
    store adopts only the legacy lots at its own facility: legacy stock is
    facility 1's, so a second facility's dispensing store adopts nothing."""
    _require_transaction(conn)
    actor = _text(actor_id)
    if not actor:
        raise StockError("adoption names who made it", code="missing_actor")
    store = _pharmacy_store(conn, dispensing_store_id)
    if store["pharmacy_kind"] != DISPENSING_STORE_KIND or not store["is_active"]:
        raise StockError(
            "legacy stock is adopted only by the active dispensing store",
            code="not_dispensing_store",
        )
    facility_sql = ""
    if _multi_facility(conn):
        from services import pharmacy_facility

        home = pharmacy_facility.store_facility(conn, dispensing_store_id)
        facility_sql = pharmacy_facility.column_sql(
            "l.facility_id", frozenset() if home is None else frozenset({home})
        )

    groups: dict[tuple, list] = {}
    for lot in conn.execute(
        f"SELECT * FROM helix_inventory_lots l WHERE {awaiting_adoption_sql('l')}{facility_sql} "
        "ORDER BY l.rowid"
    ).fetchall():
        groups.setdefault(
            (lot["medication_id"], lot["lot_number"], lot["expiration_date"]), []
        ).append(lot)

    adopted: list[str] = []
    merged: list[dict] = []
    conflicts: list[dict] = []
    occurred, _ = _now_pair()
    for (medication_id, batch, expiry), lots in groups.items():
        held = _same_identity_lot(conn, medication_id, batch, expiry, dispensing_store_id)
        statuses = {lot["status"] for lot in lots} | ({held["status"]} if held else set())
        if len(statuses) > 1:
            conflicts.append(
                {
                    "medication_id": medication_id,
                    "lot_number": batch,
                    "expiration_date": expiry,
                    "lot_ids": [lot["id"] for lot in lots],
                    "reason": "the same batch is held in more than one status",
                }
            )
            continue
        if held is not None:
            survivor, duplicates = held, lots
        else:
            survivor, duplicates = lots[0], lots[1:]
        if any(int(dup["quantity"] or 0) < 0 for dup in duplicates):
            conflicts.append(
                {
                    "medication_id": medication_id,
                    "lot_number": batch,
                    "expiration_date": expiry,
                    "lot_ids": [lot["id"] for lot in lots],
                    "reason": "a duplicate lot holds a negative quantity",
                }
            )
            continue
        if held is None:
            conn.execute(
                "UPDATE helix_inventory_lots SET store_id = ? WHERE id = ? AND store_id IS NULL",
                (dispensing_store_id, survivor["id"]),
            )
            adopted.append(survivor["id"])
        for dup in duplicates:
            units = int(dup["quantity"] or 0)
            if units > 0:
                post_movement(
                    conn,
                    lot_id=dup["id"],
                    movement_type="transfer_out",
                    quantity_change=-units,
                    actor_id=actor,
                    reference_type=MERGE_REFERENCE,
                    reference_id=survivor["id"],
                    remarks="Merged into the same batch when the dispensing "
                    "store adopted legacy stock",
                )
                post_movement(
                    conn,
                    lot_id=survivor["id"],
                    movement_type="transfer_in",
                    quantity_change=units,
                    actor_id=actor,
                    reference_type=MERGE_REFERENCE,
                    reference_id=dup["id"],
                    remarks="Merged from a duplicate legacy lot of the same batch",
                )
            conn.execute(
                """INSERT INTO pharmacy_lot_events (lot_id, helix_medication_id, store_id,
                       from_status, to_status, reason, actor_id, reference_type,
                       reference_id, occurred_at)
                   VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    dup["id"],
                    medication_id,
                    dup["status"],
                    dup["status"],
                    f"Merged into lot {survivor['id']} when the dispensing store adopted "
                    "legacy stock",
                    actor,
                    MERGE_REFERENCE,
                    survivor["id"],
                    occurred,
                ),
            )
            merged.append({"from": dup["id"], "into": survivor["id"], "units": units})
    return {
        "store_id": dispensing_store_id,
        "adopted": adopted,
        "merged": merged,
        "conflicts": conflicts,
    }


# ═════════════════════════════════════════════════════════════════════════════
#  Reconciliation
# ═════════════════════════════════════════════════════════════════════════════


def _in_clause(values: Iterable[str]) -> tuple[str, list]:
    items = list(values)
    return ",".join("?" for _ in items), items


def reconcile(conn: Any, *, lot_ids: Iterable[str] | None = None) -> dict:
    """Check the ledger against the shelf. Read-only. `clean` is True only when
    every lot's quantity equals the sum of its movements, no lot is negative,
    every running balance is right, every movement is typed and sequenced,
    and no stored lot status, movement type or drug classification is outside
    this module's constants. `lot_ids` limits the lot checks to those lots
    (the drug-master check always runs over the whole master)."""
    lot_params: list[int]
    lot_filter, lot_params = "", []
    move_filter = ""
    if lot_ids is not None:
        marks, lot_params = _in_clause(lot_ids)
        if not lot_params:
            marks = "NULL"
        lot_filter = f" WHERE l.id IN ({marks})"
        move_filter = f" AND lot_id IN ({marks})"

    mismatched = [
        dict(r)
        for r in conn.execute(
            "SELECT l.id AS lot_id, l.medication_id, COALESCE(l.quantity, 0) AS quantity, "
            "COALESCE((SELECT SUM(COALESCE(m.quantity_change, 0)) FROM helix_stock_movements m "
            "          WHERE m.lot_id = l.id), 0) AS ledger_units "
            f"FROM helix_inventory_lots l{lot_filter}",
            lot_params,
        ).fetchall()
        if r["quantity"] != r["ledger_units"]
    ]
    negative = [
        r["lot_id"]
        for r in conn.execute(
            f"SELECT l.id AS lot_id FROM helix_inventory_lots l{lot_filter}"
            f"{' AND' if lot_filter else ' WHERE'} COALESCE(l.quantity, 0) < 0",
            lot_params,
        ).fetchall()
    ]
    status_marks = ",".join("?" for _ in LOT_STATUSES)
    bad_statuses = [
        dict(r)
        for r in conn.execute(
            f"SELECT l.id AS lot_id, l.status FROM helix_inventory_lots l{lot_filter}"
            f"{' AND' if lot_filter else ' WHERE'} (l.status IS NULL OR l.status NOT IN "
            f"({status_marks}))",
            [*lot_params, *LOT_STATUSES],
        ).fetchall()
    ]

    untyped = int(
        conn.execute(
            f"SELECT COUNT(*) FROM helix_stock_movements WHERE movement_type IS NULL{move_filter}",
            lot_params,
        ).fetchone()[0]
    )
    type_marks = ",".join("?" for _ in MOVEMENT_TYPES)
    unknown_types = sorted(
        {
            r[0]
            for r in conn.execute(
                "SELECT DISTINCT movement_type FROM helix_stock_movements WHERE movement_type "
                f"IS NOT NULL AND movement_type NOT IN ({type_marks}){move_filter}",
                [*MOVEMENT_TYPES, *lot_params],
            ).fetchall()
        }
    )
    unsequenced = int(
        conn.execute(
            f"SELECT COUNT(*) FROM helix_stock_movements WHERE seq IS NULL{move_filter}", lot_params
        ).fetchone()[0]
    )

    breaks: list[dict] = []
    running: dict[str, int] = {}
    for r in conn.execute(
        "SELECT id, lot_id, quantity_change, balance_after_units FROM helix_stock_movements "
        f"WHERE lot_id IS NOT NULL AND seq IS NOT NULL{move_filter} ORDER BY seq",
        lot_params,
    ).fetchall():
        running[r["lot_id"]] = running.get(r["lot_id"], 0) + int(r["quantity_change"] or 0)
        if (
            r["balance_after_units"] is None
            or int(r["balance_after_units"]) != running[r["lot_id"]]
        ):
            breaks.append(
                {
                    "movement_id": r["id"],
                    "lot_id": r["lot_id"],
                    "balance_after_units": r["balance_after_units"],
                    "running_units": running[r["lot_id"]],
                }
            )

    bad_master: list[dict] = []
    for column, allowed in (
        ("drug_schedule", DRUG_SCHEDULES),
        ("ndps_category", NDPS_CATEGORIES),
        ("storage_condition", STORAGE_CONDITIONS),
        ("identity_status", IDENTITY_STATUSES),
    ):
        marks = ",".join("?" for _ in allowed)
        for r in conn.execute(
            f"SELECT id, {column} AS value FROM helix_medication_master "
            f"WHERE {column} IS NOT NULL AND {column} NOT IN ({marks})",
            allowed,
        ).fetchall():
            bad_master.append({"medication_id": r["id"], "field": column, "value": r["value"]})

    # multisite-14: once more than one facility is on record, a lot whose own
    # facility is not its store's, or a movement not at its lot's facility, is
    # at no facility (services/pharmacy_facility) and is reported here.
    facility_mismatches: list[dict] | None = None
    if _multi_facility(conn):
        facility_mismatches = [
            {"kind": "lot", **dict(r)}
            for r in conn.execute(
                "SELECT l.id AS lot_id, l.facility_id, s.facility_id AS store_facility_id "
                f"FROM helix_inventory_lots l JOIN stores s ON s.id = l.store_id{lot_filter}"
                f"{' AND' if lot_filter else ' WHERE'} l.facility_id IS NOT s.facility_id",
                lot_params,
            ).fetchall()
        ] + [
            {"kind": "movement", **dict(r)}
            for r in conn.execute(
                "SELECT mv.id AS movement_id, mv.lot_id, mv.facility_id, "
                "l.facility_id AS lot_facility_id FROM helix_stock_movements mv "
                "JOIN helix_inventory_lots l ON l.id = mv.lot_id "
                f"WHERE mv.facility_id IS NOT l.facility_id{move_filter}",
                lot_params,
            ).fetchall()
        ]

    lots_checked = int(
        conn.execute(
            f"SELECT COUNT(*) FROM helix_inventory_lots l{lot_filter}", lot_params
        ).fetchone()[0]
    )
    clean = not (
        mismatched
        or negative
        or bad_statuses
        or untyped
        or unknown_types
        or unsequenced
        or breaks
        or bad_master
        or facility_mismatches
    )
    out: dict[str, Any] = {
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
    if facility_mismatches is not None:
        out["facility_mismatches"] = facility_mismatches
    return out


# ═════════════════════════════════════════════════════════════════════════════
#  Ward stock used on an eMAR dose (grn-17)
# ═════════════════════════════════════════════════════════════════════════════

#: reference_type on a consumption movement; reference_id is the eMAR
#: administration's id, which names the patient (the movement row names none).
CONSUMPTION_REFERENCE = "medication_administration"

#: The one store kind a dose is booked against. A crash cart's use is found
#: and confirmed by the cart's own check (grn-23), never by a dose.
CONSUMPTION_STORE_KIND = "ward_imprest"

#: Units one given dose takes off the ward's books: one, like a Helix dispense
#: today (GRN-R13 keeps the dispense quantity at one unit, and a dose's custody
#: names one supply unit). A schedule-derived quantity waits for W117; a dose
#: that used more than one unit is put right by the ward's count.
CONSUMPTION_UNITS_PER_DOSE = 1

#: med_orders.supply_source of an order given from ward stock (MO-R12). Only
#: such an order is booked: a dose from the patient's own pharmacy supply or
#: the patient's own medicines never touches the ward's books.
WARD_STOCK_SUPPLY = "ward_stock"

#: Why a given dose took nothing off a ward's books, in words for the log.
CONSUMPTION_SKIP_WORDS = {
    "switched_off": "ward stock is switched off on this site",
    "not_linked": "the dose's ward row is not linked to a medication order",
    "not_given": "the administration is not a given dose",
    "not_formulary_bound": "the order names no drug in the drug master",
    "not_from_ward_stock": "the order is not given from ward stock",
    "no_department": "the patient has no department on record",
    "no_ward_store": "the patient's department has no ward stock store",
    "not_ward_stock": "the drug is not ward stock in the patient's department",
    "store_ambiguous": "more than one ward store in the department holds the drug",
    "no_ward_lot": "the ward store holds no available batch of the drug",
}


def ward_consumption_on() -> bool:
    """ORB_PHARMACY_WARD_STOCK (under ORB_PHARMACY_STORE), read at call time.
    DB-free, so the eMAR asks it before opening anything."""
    import config

    return bool(config.feature_enabled("pharmacy_ward_stock"))


def consumption_key(administration_id: Any) -> str:
    """The movement's idempotency key: one booking per administration, so a
    second call for the same dose answers the first and writes nothing."""
    return f"{CONSUMPTION_REFERENCE}:{int(administration_id)}:consumption"


def _check_negative_allowed(conn, movement_type: str, lot: Any) -> None:
    """post_movement's allow_negative: a consumption on a ward imprest lot
    only. Everything else keeps the guarded decrement."""
    store = None
    if lot["store_id"] is not None:
        store = conn.execute(
            "SELECT pharmacy_kind FROM stores WHERE id = ?", (lot["store_id"],)
        ).fetchone()
    if (
        movement_type != "consumption"
        or store is None
        or store["pharmacy_kind"] != CONSUMPTION_STORE_KIND
    ):
        raise StockError(
            "only ward stock used on a dose may take a lot below zero. Nothing was changed.",
            code="negative_not_allowed",
        )


def _skip(reason: str, **extra: Any) -> dict:
    return {
        "status": "skipped",
        "reason": reason,
        "reason_words": CONSUMPTION_SKIP_WORDS.get(reason, reason),
        **extra,
    }


def _patient_location(conn, patient_id: str) -> tuple[str | None, str | None]:
    """(department, ward) of the patient's current stay: the department by the
    same rule department scope reads (permissions.PATIENT_DEPARTMENT_SQL), the
    ward from that same admission row, else the patients row."""
    from permissions import PATIENT_DEPARTMENT_SQL

    row = conn.execute(
        f"SELECT {PATIENT_DEPARTMENT_SQL} AS department, p.ward AS ward "
        "FROM patients p WHERE p.id = ?",
        (patient_id,),
    ).fetchone()
    if row is None:
        return None, None
    stay = conn.execute(
        "SELECT a.ward FROM admissions a WHERE a.patient_id = ? "
        "ORDER BY (a.status = 'active') DESC, a.created_at DESC, a.rowid DESC LIMIT 1",
        (patient_id,),
    ).fetchone()
    ward = (stay["ward"] if stay is not None and _text(stay["ward"]) else None) or row["ward"]
    return _text(row["department"]), _text(ward)


def _ward_store_for(
    conn: Any, department: str, ward: str | None, drug_id: str, *, facility_id: int | None = None
) -> tuple[dict | None, str | None]:
    """The patient's ward imprest store that holds this drug: (store, None),
    or (None, reason). A store holds the drug when the drug is on its list (an
    active par level above zero) or it has available units of it. Departments
    are compared as department scope compares them (case and outer spaces
    ignored). With two such stores in the department, the patient's ward
    decides; when it does not, nothing is guessed. With `facility_id`
    (multisite-14) only a store at the patient's facility counts."""
    kind_and_dept = (
        "s.pharmacy_kind = ? AND s.is_active = 1 "
        "AND lower(trim(COALESCE(s.department, ''))) = lower(trim(?))"
    )
    if facility_id is not None:
        kind_and_dept += f" AND s.facility_id = {int(facility_id)}"
    stores = [
        dict(r)
        for r in conn.execute(
            "SELECT s.id, s.code, s.name, s.department, s.ward FROM stores s "
            f"WHERE {kind_and_dept} AND ("
            "  EXISTS (SELECT 1 FROM pharmacy_par_levels p WHERE p.store_id = s.id "
            "          AND p.helix_medication_id = ? AND p.is_active = 1 AND p.par_units > 0) "
            "  OR EXISTS (SELECT 1 FROM helix_inventory_lots l WHERE l.store_id = s.id "
            "          AND l.medication_id = ? AND l.status = 'available' AND l.quantity > 0)) "
            "ORDER BY s.id",
            (CONSUMPTION_STORE_KIND, department, drug_id, drug_id),
        ).fetchall()
    ]
    if not stores:
        any_store = conn.execute(
            f"SELECT 1 FROM stores s WHERE {kind_and_dept} LIMIT 1",
            (CONSUMPTION_STORE_KIND, department),
        ).fetchone()
        return None, ("not_ward_stock" if any_store else "no_ward_store")
    if len(stores) == 1:
        return stores[0], None
    want = (ward or "").strip().lower()
    on_ward = [s for s in stores if want and (s["ward"] or "").strip().lower() == want]
    if len(on_ward) == 1:
        return on_ward[0], None
    return None, "store_ambiguous"


def _consumption_lot(
    conn, store_id: int, drug_id: str, units: int, today: str
) -> tuple[dict | None, bool]:
    """(lot, below_zero). FEFO among the store's available, in-date lots of the
    drug holding the units. When none does, the ward's own batch the use is
    booked against, below zero: an available lot of the drug already at or
    below zero, in date first (earliest expiry), else the latest expiry. A
    lot that is held (quarantined, recalled, NSQ, awaiting inspection) or
    expired with units left is never taken: nobody may use those units, and a
    negative booking against them would hide them. (None, False) when the store
    has no such lot."""
    row = conn.execute(
        "SELECT l.* FROM helix_inventory_lots l WHERE l.store_id = ? AND l.medication_id = ? "
        "AND l.status = 'available' AND date(l.expiration_date) >= ? AND l.quantity >= ? "
        "ORDER BY date(l.expiration_date) ASC, l.rowid ASC LIMIT 1",
        (store_id, drug_id, today, units),
    ).fetchone()
    if row is not None:
        return dict(row), False
    row = conn.execute(
        "SELECT l.* FROM helix_inventory_lots l WHERE l.store_id = ? AND l.medication_id = ? "
        "AND l.status = 'available' AND COALESCE(l.quantity, 0) < ? "
        "ORDER BY CASE WHEN date(l.expiration_date) >= ? THEN 0 ELSE 1 END, "
        "CASE WHEN date(l.expiration_date) >= ? THEN julianday(l.expiration_date) "
        "ELSE -julianday(l.expiration_date) END, l.rowid DESC LIMIT 1",
        (store_id, drug_id, units, today, today),
    ).fetchone()
    return (dict(row), True) if row is not None else (None, False)


def plan_ward_consumption(
    conn: Any, administration_id: Any, *, med_order_id: str | None, today: str | None = None
) -> dict:
    """What one given dose takes off the ward's books, decided on the caller's
    connection. Writes nothing. Returns {status: 'consume', ...} with the
    patient, drug, store, lot, units and below_zero, or a skip
    {status: 'skipped', reason, reason_words} (reasons: CONSUMPTION_SKIP_WORDS).

    Booked only when every one of these holds: ward stock is on; the
    administration is a given dose; its ward row is linked to a medication
    order of the same patient; the order names its drug in the drug master
    (formulary_id) and is given from ward stock (supply_source); the patient's
    department has exactly one ward imprest store that holds the drug (the
    ward decides between two); and that store has an available batch of it."""
    if not ward_consumption_on():
        return _skip("switched_off")
    if not med_order_id:
        return _skip("not_linked")
    adm = conn.execute(
        "SELECT id, patient_id, status FROM medication_administrations WHERE id = ?",
        (administration_id,),
    ).fetchone()
    if adm is None or adm["status"] != "given":
        return _skip("not_given")
    order = conn.execute(
        "SELECT id, patient_id, formulary_id, supply_source FROM med_orders WHERE id = ?",
        (med_order_id,),
    ).fetchone()
    if order is None or order["patient_id"] != adm["patient_id"]:
        return _skip("not_linked")
    drug_id = _text(order["formulary_id"])
    if not drug_id:
        return _skip("not_formulary_bound")
    if order["supply_source"] != WARD_STOCK_SUPPLY:
        return _skip("not_from_ward_stock")
    department, ward = _patient_location(conn, adm["patient_id"])
    if not department:
        return _skip("no_department")
    from services import pharmacy_facility

    store, reason = _ward_store_for(
        conn,
        department,
        ward,
        drug_id,
        facility_id=pharmacy_facility.patient_facility(conn, adm["patient_id"]),
    )
    if store is None:
        return _skip(reason or "not_ward_stock")
    units = CONSUMPTION_UNITS_PER_DOSE
    lot, below_zero = _consumption_lot(
        conn, int(store["id"]), drug_id, units, today or site_today()
    )
    if lot is None:
        return _skip("no_ward_lot", store=store)
    return {
        "status": "consume",
        "reason": None,
        "administration_id": int(adm["id"]),
        "patient_id": adm["patient_id"],
        "med_order_id": order["id"],
        "drug_id": drug_id,
        "store": store,
        "lot": lot,
        "units": units,
        "below_zero": below_zero,
    }


def record_ward_consumption_in_txn(
    conn,
    administration_id: Any,
    *,
    med_order_id: str | None,
    actor_id: str,
    today: str | None = None,
) -> dict:
    """Book one given dose's ward stock use, in the caller's transaction
    (db_session(immediate=True), opened by the eMAR AFTER the dose committed).
    Returns {status: 'consumed' | 'already' | 'skipped', ...}.

    The 'consumption' movement (reference medication_administration and the
    administration's id, key consumption_key) and its fail-closed
    clinical_access audit row 'ward_stock_consumed' are written together or not
    at all. A second call for the same administration answers 'already' and
    writes nothing. When the ward's books hold no in-date unit the movement
    takes the batch below zero (allow_negative): booked and flagged, never
    refused."""
    _require_transaction(conn)
    key = consumption_key(administration_id)
    prior = conn.execute(
        "SELECT * FROM helix_stock_movements WHERE idempotency_key = ?", (key,)
    ).fetchone()
    if prior is not None:
        return {"status": "already", "reason": None, "movement": dict(prior)}
    plan = plan_ward_consumption(conn, administration_id, med_order_id=med_order_id, today=today)
    if plan["status"] != "consume":
        return plan
    lot, store, below_zero = plan["lot"], plan["store"], plan["below_zero"]
    remarks = (
        "Used on the ward for a dose given (eMAR)"
        if not below_zero
        else "Used on the ward for a dose given (eMAR) when the ward's books held none of "
        "this drug: the batch given is not certain. Count this drug."
    )
    movement = post_movement(
        conn,
        lot_id=lot["id"],
        movement_type="consumption",
        quantity_change=-plan["units"],
        actor_id=actor_id,
        reference_type=CONSUMPTION_REFERENCE,
        reference_id=str(plan["administration_id"]),
        idempotency_key=key,
        remarks=remarks,
        allow_negative=below_zero,
    )
    drug = conn.execute(
        "SELECT name FROM helix_medication_master WHERE id = ?", (plan["drug_id"],)
    ).fetchone()
    drug_name = (drug["name"] if drug is not None else None) or plan["drug_id"]
    store_name = store["name"] or store["code"]
    balance = int(movement["balance_after_units"])
    from services import audit_service

    audit_service.log_audit_event(
        correlation_id=str(uuid.uuid4()),
        event_category="clinical_access",
        action_type="ward_stock_consumed",
        outcome="success",
        db=conn,
        summary_message=(
            f"Ward stock: {_units_words(plan['units'])} of {drug_name} taken off {store_name} "
            f"for eMAR administration {plan['administration_id']}"
            + (f", with none on the books (batch balance now {balance})" if below_zero else "")
        ),
        actor_user_id=actor_id,
        target_type="medication_administration",
        target_id=str(plan["administration_id"]),
        patient_id=plan["patient_id"],
        metadata={
            "administration_id": plan["administration_id"],
            "med_order_id": plan["med_order_id"],
            "helix_medication_id": plan["drug_id"],
            "store_id": int(store["id"]),
            "store_code": store["code"],
            "lot_id": lot["id"],
            "batch_no": lot["lot_number"],
            "units": plan["units"],
            "movement_id": movement["id"],
            "balance_after_units": balance,
            "below_zero": below_zero,
        },
    )
    return {
        "status": "consumed",
        "reason": None,
        "movement": movement,
        "store_id": int(store["id"]),
        "lot_id": lot["id"],
        "below_zero": below_zero,
        "balance_after_units": balance,
    }


def ward_consumption_for_batch(conn, medication_id: str, batch_no: Any) -> list[dict]:
    """Every dose booked off a ward's books from this drug's batch (compared
    without case or spaces), oldest first, each attributed to its patient
    through the eMAR administration it records: {movement_id,
    administration_id, patient_id, store_id, store_code, store_name,
    department, lot_id, batch_no, expiry_date, units, occurred_at,
    batch_certain}. batch_certain is False for a use booked when the ward's
    books held none (the movement took the batch below zero), so a recall
    trace lists that patient as possibly given the batch, never as certainly.
    The hook grn-10's recall trace reads. Takes the caller's connection and
    never writes. It returns patient ids: the caller audits the disclosure."""
    key = _batch_key(batch_no)
    if not key:
        return []
    out: list[dict] = []
    for r in conn.execute(
        "SELECT mv.id, mv.lot_id, mv.store_id, mv.quantity_change, mv.balance_after_units, "
        "mv.occurred_at, mv.reference_id, l.lot_number, l.expiration_date, "
        "s.code AS store_code, s.name AS store_name, s.department, "
        "ma.patient_id, ma.id AS administration_id "
        "FROM helix_stock_movements mv "
        "JOIN helix_inventory_lots l ON l.id = mv.lot_id "
        "LEFT JOIN stores s ON s.id = mv.store_id "
        "LEFT JOIN medication_administrations ma ON ma.id = CAST(mv.reference_id AS INTEGER) "
        "WHERE mv.movement_type = 'consumption' AND mv.reference_type = ? "
        "AND mv.medication_id = ? ORDER BY mv.seq",
        (CONSUMPTION_REFERENCE, medication_id),
    ).fetchall():
        if _batch_key(r["lot_number"]) != key:
            continue
        balance = r["balance_after_units"]
        out.append(
            {
                "movement_id": r["id"],
                "administration_id": (
                    int(r["administration_id"]) if r["administration_id"] is not None else None
                ),
                "patient_id": r["patient_id"],
                "store_id": r["store_id"],
                "store_code": r["store_code"],
                "store_name": r["store_name"] or r["store_code"],
                "department": r["department"],
                "lot_id": r["lot_id"],
                "batch_no": r["lot_number"],
                "expiry_date": str(r["expiration_date"])[:10] if r["expiration_date"] else None,
                "units": -int(r["quantity_change"] or 0),
                "occurred_at": r["occurred_at"],
                "batch_certain": balance is not None and int(balance) >= 0,
            }
        )
    return out
