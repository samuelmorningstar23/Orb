"""Orb, pharmacy stock reports: what is on the shelves, where, and what to do
about it (grn-9).

Design: docs/his_design/grn.json, increment grn-9. Reached only through
routes/pharmacy_store/stock.py and the purchasing service, both behind
ORB_PHARMACY_STORE (default off).

READ-ONLY. Nothing here writes a row. Drug stock has one writer,
services/pharmacy_stock_service.py, and every report below reads the same
tables it writes, with its predicates (dispensable_lot_sql, site_today), so a
report can never call a lot dispensable that the dispense would refuse, or the
reverse. Each function takes an optional open connection, so a caller inside
its own immediate transaction (the purchasing service turning reorder
suggestions into a draft order) reads under its own write lock; without one
the function opens a plain read session.

THE BUCKETS. Every lot holding units falls in exactly one:
  * usable        status 'available' and in date in its own store. On the
                  dispensing shelf this is also DISPENSABLE; in a sub-store it
                  is stock that has to be transferred to the shelf first.
  * expired       status 'available' but past its expiry, or with no expiry
                  recorded (date() reads a missing or malformed date as NULL,
                  and the dispense refuses it the same way).
  * quarantined   held back: quarantined, recalled, not of standard quality,
                  or awaiting inspection.
  * other         anything else still holding units (written off or returned
                  to the vendor should hold none); shown so the totals add up.
In transit is counted separately: units on a dispatched transfer line not yet
received, against the store they are going to. They are on no shelf.

REORDER, "counting dispensable units only": a store's position is its usable
units plus what is in transit to it plus what is on order for it, and a drug's
hospital default is judged against its dispensable units on the shelf. A level
nobody has set is never read as 0: the row says 'No reorder level set'.

MONEY is integer paise. A lot whose cost was never recorded reads 'Rate not
configured', never Rs 0, and one with no MRP or no units per pack reads 'MRP
not recorded'.

NO PATIENT DATA. The ledger names the kind of reference a movement carries; a
movement tied to a patient's medicine order (a dispense, a ward return) shows
neither the order id nor its remarks, so these screens never widen who can
link stock to a patient.
"""
from __future__ import annotations

import contextlib
from datetime import date, timedelta
from typing import Any, Iterator, Optional

from database import db_session
from services import pharmacy_stock_service as stock

# ═════════════════════════════════════════════════════════════════════════════
#  Words
# ═════════════════════════════════════════════════════════════════════════════

#: Lot statuses that hold stock back from use (the 'quarantined' bucket).
HELD_STATUSES: tuple[str, ...] = ("quarantined", "recalled", "nsq", "pending_inspection")

LOT_STATUS_LABELS = {
    "available": "Available",
    "quarantined": "Quarantined",
    "recalled": "Recalled",
    "nsq": "Not of standard quality",
    "pending_inspection": "Awaiting inspection",
    "written_off": "Written off",
    "returned_to_vendor": "Returned to vendor",
}

MOVEMENT_LABELS = {
    "opening": "Opening balance",
    "receipt": "Received",
    "dispense": "Dispensed",
    "transfer_out": "Transferred out",
    "transfer_in": "Transferred in",
    "ward_return": "Returned from a ward",
    "vendor_return": "Returned to the vendor",
    "write_off": "Written off",
    "adjustment": "Adjustment",
    "count_variance": "Stock count difference",
    "consumption": "Used on a ward",
    "op_sale": "Outpatient sale",
}

STORE_KIND_LABELS = {
    "dispensing": "Dispensing pharmacy",
    "sub_store": "Sub-store",
    "ndps_vault": "NDPS vault",
    "returns_quarantine": "Returns quarantine",
    "ward_imprest": "Ward stock",
    "crash_cart": "Crash cart",
}

LEGACY_STORE_LABEL = "Not yet in a store"
NO_LEVEL_LABEL = "No reorder level set"
RATE_NOT_CONFIGURED = "Rate not configured"
MRP_NOT_RECORDED = "MRP not recorded"
PATIENT_ORDER_LABEL = "A patient's medicine order"

#: Reference types on a movement that point at a patient's record. Their id
#: and remarks are never returned by the ledger.
PATIENT_REFERENCE_TYPES = frozenset({"helix_order", "pharmacy_ward_return", "ward_return"})

_REFERENCE_LABELS = {
    "legacy_lot_merge": "Merged when the dispensing store adopted legacy stock",
    "goods_receipt": "Goods receipt",
    "pharmacy_goods_receipt": "Goods receipt",
    "pharmacy_transfer": "Transfer between stores",
    "pharmacy_stock_count": "Stock count",
    "pharmacy_vendor_return": "Return to vendor",
}

#: Purchase order statuses whose open lines count as "on order" (drafts
#: included, so a reorder suggestion is not drafted twice).
ON_ORDER_PO_STATUSES: tuple[str, ...] = (
    "draft", "pending_approval", "approved", "issued", "partially_received")
ON_ORDER_LINE_STATUSES: tuple[str, ...] = ("open", "partially_received")

MAX_NEAR_EXPIRY_DAYS = 730
MAX_LEDGER_PAGE = 200


@contextlib.contextmanager
def _reading(conn) -> Iterator[Any]:
    """The caller's connection, or a fresh read session."""
    if conn is not None:
        yield conn
        return
    with db_session() as fresh:
        yield fresh


def _marks(values) -> str:
    return ",".join("?" for _ in values)


def _store_label(row: Any) -> str:
    if row is None or row.get("store_id") is None:
        return LEGACY_STORE_LABEL
    return row.get("store_name") or row.get("store_code") or f"Store {row['store_id']}"


def _round_div(numerator: int, denominator: int) -> int:
    """Half-up integer division for non-negative money."""
    return (2 * numerator + denominator) // (2 * denominator)


def lot_value(lot: dict) -> dict:
    """{cost_value_paise, cost_label, mrp_value_paise, mrp_label} for the units
    a lot holds. None and a label when the rate was never recorded."""
    units = int(lot.get("quantity") or 0)
    cost = lot.get("unit_cost_paise")
    out: dict[str, Any] = {}
    if cost is None:
        out["cost_value_paise"] = None
        out["cost_label"] = RATE_NOT_CONFIGURED
    else:
        out["cost_value_paise"] = int(cost) * units
        out["cost_label"] = None
    mrp_pack = lot.get("mrp_pack_paise")
    per_pack = lot.get("units_per_pack")
    if mrp_pack is None or not per_pack:
        out["mrp_value_paise"] = None
        out["mrp_label"] = MRP_NOT_RECORDED
    else:
        out["mrp_value_paise"] = _round_div(int(mrp_pack) * units, int(per_pack))
        out["mrp_label"] = None
    return out


def _drug_fields(row: dict) -> dict:
    return {
        "drug_id": row.get("drug_id"),
        "drug_name": row.get("drug_name") or row.get("drug_id"),
        "generic_name": row.get("generic_name"),
        "form": row.get("form"),
        "strength": row.get("strength"),
        "dispense_unit": row.get("dispense_unit"),
    }


# ═════════════════════════════════════════════════════════════════════════════
#  In transit and on order
# ═════════════════════════════════════════════════════════════════════════════

def in_transit_units(conn) -> dict[tuple[int, str], int]:
    """{(to_store_id, drug_id): units} on dispatched transfer lines that have
    not been received. Those units are counted in neither store."""
    out: dict[tuple[int, str], int] = {}
    for r in conn.execute(
            "SELECT t.to_store_id AS store_id, tl.helix_medication_id AS drug_id, "
            "SUM(tl.quantity_units) AS units "
            "FROM pharmacy_transfer_lines tl "
            "JOIN pharmacy_transfers t ON t.id = tl.pharmacy_transfer_id "
            "WHERE t.status IN ('dispatched', 'partially_received') "
            "AND tl.received_units IS NULL "
            "GROUP BY t.to_store_id, tl.helix_medication_id").fetchall():
        out[(int(r["store_id"]), r["drug_id"])] = int(r["units"] or 0)
    return out


def on_order_units(conn) -> dict[tuple[Optional[int], str], int]:
    """{(store_id, drug_id): units} still outstanding on open pharmacy
    purchase order lines, drafts included. Units are whole packs times the
    pack size snapshotted on the line."""
    out: dict[tuple[Optional[int], str], int] = {}
    rows = conn.execute(
        "SELECT p.store_id AS store_id, pl.helix_medication_id AS drug_id, "
        "pl.quantity_milli, pl.received_quantity_milli, pl.units_per_pack "
        "FROM purchase_order_lines pl JOIN purchase_orders p ON p.id = pl.purchase_order_id "
        f"WHERE p.domain = 'pharmacy' AND p.status IN ({_marks(ON_ORDER_PO_STATUSES)}) "
        f"AND pl.status IN ({_marks(ON_ORDER_LINE_STATUSES)}) "
        "AND pl.helix_medication_id IS NOT NULL AND pl.units_per_pack IS NOT NULL",
        (*ON_ORDER_PO_STATUSES, *ON_ORDER_LINE_STATUSES)).fetchall()
    for r in rows:
        outstanding = max(0, int(r["quantity_milli"] or 0) - int(r["received_quantity_milli"] or 0))
        units = outstanding * int(r["units_per_pack"]) // 1000
        if units <= 0:
            continue
        key = (r["store_id"], r["drug_id"])
        out[key] = out.get(key, 0) + units
    return out


# ═════════════════════════════════════════════════════════════════════════════
#  Stock per store and drug
# ═════════════════════════════════════════════════════════════════════════════

def stock_by_store(conn=None, *, store_id: Optional[int] = None, drug_id: Optional[str] = None,
                   search: Optional[str] = None, today: Optional[str] = None,
                   limit: int = 1000) -> dict:
    """Stock per store and drug, split into the buckets in the module note.
    Returns {"today", "rows", "totals"}. A drug with nothing on a shelf but
    units in transit to a store still has a row."""
    today = today or stock.site_today()
    held = HELD_STATUSES
    sql = (
        "SELECT l.store_id AS store_id, l.medication_id AS drug_id, "
        "s.code AS store_code, s.name AS store_name, s.pharmacy_kind AS store_kind, "
        "s.is_active AS store_active, "
        "m.name AS drug_name, m.generic_name, m.form, m.strength, m.dispense_unit, "
        "m.reorder_level_units, "
        "SUM(l.quantity) AS on_hand_units, "
        f"SUM(CASE WHEN {stock.dispensable_lot_sql('l')} THEN l.quantity ELSE 0 END) "
        "AS dispensable_units, "
        "SUM(CASE WHEN l.status = 'available' AND date(l.expiration_date) >= ? "
        "THEN l.quantity ELSE 0 END) AS usable_units, "
        "SUM(CASE WHEN l.status = 'available' AND (date(l.expiration_date) IS NULL "
        "OR date(l.expiration_date) < ?) THEN l.quantity ELSE 0 END) AS expired_units, "
        "SUM(CASE WHEN l.status = 'available' AND date(l.expiration_date) IS NULL "
        "THEN l.quantity ELSE 0 END) AS undated_units, "
        f"SUM(CASE WHEN l.status IN ({_marks(held)}) THEN l.quantity ELSE 0 END) "
        "AS quarantined_units, "
        "COUNT(*) AS lot_count "
        "FROM helix_inventory_lots l "
        "LEFT JOIN stores s ON s.id = l.store_id "
        "LEFT JOIN helix_medication_master m ON m.id = l.medication_id "
        "WHERE l.quantity > 0"
    )
    params: list[Any] = [today, today, today, *held]
    if store_id is not None:
        sql += " AND l.store_id = ?"
        params.append(int(store_id))
    if drug_id:
        sql += " AND l.medication_id = ?"
        params.append(drug_id)
    if search and search.strip():
        sql += " AND (m.name LIKE ? OR m.generic_name LIKE ? OR l.medication_id LIKE ?)"
        params.extend([f"%{search.strip()}%"] * 3)
    sql += " GROUP BY l.store_id, l.medication_id ORDER BY m.name, s.name LIMIT ?"
    params.append(max(1, min(int(limit), 5000)))

    with _reading(conn) as db:
        rows = [dict(r) for r in db.execute(sql, params).fetchall()]
        transit = in_transit_units(db)
        seen = {(r["store_id"], r["drug_id"]) for r in rows}
        missing = [(sid, did) for (sid, did) in transit
                   if (sid, did) not in seen
                   and (store_id is None or sid == int(store_id))
                   and (not drug_id or did == drug_id)]
        for sid, did in missing:
            info = db.execute(
                "SELECT s.code AS store_code, s.name AS store_name, s.pharmacy_kind AS store_kind, "
                "s.is_active AS store_active, m.name AS drug_name, m.generic_name, m.form, "
                "m.strength, m.dispense_unit, m.reorder_level_units "
                "FROM stores s, helix_medication_master m WHERE s.id = ? AND m.id = ?",
                (sid, did)).fetchone()
            if info is None:
                continue
            name_hit = not (search and search.strip()) or any(
                search.strip().lower() in (v or "").lower()
                for v in (info["drug_name"], info["generic_name"], did))
            if not name_hit:
                continue
            rows.append({**dict(info), "store_id": sid, "drug_id": did, "on_hand_units": 0,
                         "dispensable_units": 0, "usable_units": 0, "expired_units": 0,
                         "undated_units": 0, "quarantined_units": 0, "lot_count": 0})

    out_rows = []
    totals = {k: 0 for k in ("on_hand_units", "dispensable_units", "usable_units",
                             "expired_units", "quarantined_units", "in_transit_units",
                             "other_units")}
    for r in rows:
        on_hand = int(r["on_hand_units"] or 0)
        usable = int(r["usable_units"] or 0)
        expired = int(r["expired_units"] or 0)
        quarantined = int(r["quarantined_units"] or 0)
        moving = transit.get((r["store_id"], r["drug_id"]), 0) if r["store_id"] is not None else 0
        row = {
            "store_id": r["store_id"],
            "store_code": r.get("store_code"),
            "store_label": _store_label(r),
            "store_kind": r.get("store_kind"),
            "store_kind_label": STORE_KIND_LABELS.get(r.get("store_kind") or "", None),
            "store_active": bool(r["store_active"]) if r.get("store_active") is not None else None,
            **_drug_fields(r),
            "on_hand_units": on_hand,
            "dispensable_units": int(r["dispensable_units"] or 0),
            "usable_units": usable,
            "expired_units": expired,
            "undated_units": int(r["undated_units"] or 0),
            "quarantined_units": quarantined,
            "in_transit_units": moving,
            "other_units": max(0, on_hand - usable - expired - quarantined),
            "lot_count": int(r["lot_count"] or 0),
        }
        for key in totals:
            totals[key] += row[key]
        out_rows.append(row)
    out_rows.sort(key=lambda x: ((x["drug_name"] or "").lower(),
                                 x["store_id"] is not None, x["store_label"].lower()))
    return {"today": today, "rows": out_rows, "totals": totals}


# ═════════════════════════════════════════════════════════════════════════════
#  Lots
# ═════════════════════════════════════════════════════════════════════════════

def list_lots(conn=None, *, drug_id: Optional[str] = None, store_id: Optional[int] = None,
              include_empty: bool = False, today: Optional[str] = None,
              near_days: Optional[int] = None, available_by: Optional[str] = None,
              limit: int = 500) -> dict:
    """Lots, earliest expiry first (the order a dispense takes them), with
    their status, expiry state and value. Returns {"today", "lots"}.

    `available_by` (YYYY-MM-DD) keeps only available lots holding units whose
    expiry is on or before that date or not recorded: the near-expiry query."""
    import config

    today = today or stock.site_today()
    days = int(near_days if near_days is not None else config.PHARMACY_NEAR_EXPIRY_DAYS)
    cutoff = (date.fromisoformat(today) + timedelta(days=max(0, days))).isoformat()
    sql = (
        "SELECT l.id, l.medication_id AS drug_id, l.lot_number, l.expiration_date, "
        "l.quantity, l.status, l.store_id, l.units_per_pack, l.mrp_pack_paise, "
        "l.unit_cost_paise, l.mfg_date, l.received_at, "
        "s.code AS store_code, s.name AS store_name, s.pharmacy_kind AS store_kind, "
        "m.name AS drug_name, m.generic_name, m.form, m.strength, m.dispense_unit, "
        f"CASE WHEN {stock.dispensable_lot_sql('l')} THEN 1 ELSE 0 END AS dispensable, "
        "CASE WHEN date(l.expiration_date) IS NULL THEN 'undated' "
        "WHEN date(l.expiration_date) < ? THEN 'expired' "
        "WHEN date(l.expiration_date) <= ? THEN 'near_expiry' ELSE 'in_date' END "
        "AS expiry_state "
        "FROM helix_inventory_lots l "
        "LEFT JOIN stores s ON s.id = l.store_id "
        "LEFT JOIN helix_medication_master m ON m.id = l.medication_id WHERE 1 = 1"
    )
    params: list[Any] = [today, today, cutoff]
    if not include_empty:
        sql += " AND l.quantity > 0"
    if drug_id:
        sql += " AND l.medication_id = ?"
        params.append(drug_id)
    if store_id is not None:
        sql += " AND l.store_id = ?"
        params.append(int(store_id))
    if available_by is not None:
        sql += (" AND l.status = 'available' AND l.quantity > 0 AND "
                "(date(l.expiration_date) IS NULL OR date(l.expiration_date) <= ?)")
        params.append(available_by)
    sql += (" ORDER BY date(l.expiration_date) IS NULL, date(l.expiration_date), "
            "l.rowid LIMIT ?")
    params.append(max(1, min(int(limit), 5000)))
    with _reading(conn) as db:
        rows = [dict(r) for r in db.execute(sql, params).fetchall()]
    lots = []
    for r in rows:
        lot = {
            "id": r["id"],
            **_drug_fields(r),
            "lot_number": r["lot_number"],
            "expiration_date": (r["expiration_date"] or None) and str(r["expiration_date"])[:10],
            "expiry_state": r["expiry_state"],
            "quantity": int(r["quantity"] or 0),
            "status": r["status"],
            "status_label": LOT_STATUS_LABELS.get(r["status"], r["status"]),
            "dispensable": bool(r["dispensable"]),
            "store_id": r["store_id"],
            "store_code": r["store_code"],
            "store_label": _store_label(r),
            "store_kind": r["store_kind"],
            "units_per_pack": r["units_per_pack"],
            "unit_cost_paise": r["unit_cost_paise"],
            "mrp_pack_paise": r["mrp_pack_paise"],
            "mfg_date": r["mfg_date"],
            "received_at": r["received_at"],
        }
        lot.update(lot_value(lot))
        lots.append(lot)
    return {"today": today, "near_expiry_days": days, "lots": lots}


# ═════════════════════════════════════════════════════════════════════════════
#  The ledger, paged by seq
# ═════════════════════════════════════════════════════════════════════════════

def ledger_page(conn=None, *, drug_id: Optional[str] = None, store_id: Optional[int] = None,
                lot_id: Optional[str] = None, before_seq: Optional[int] = None,
                limit: int = 50) -> dict:
    """One page of the drug ledger, newest first by seq (the ledger's own
    ordering key, stable under VACUUM and under new rows arriving while a
    reader pages back). Pass the returned next_before_seq to read the next
    older page. Returns {"movements", "next_before_seq", "has_more"}."""
    size = max(1, min(int(limit), MAX_LEDGER_PAGE))
    sql = (
        "SELECT mv.id, mv.seq, mv.occurred_at, mv.timestamp, mv.movement_type, mv.reason, "
        "mv.quantity_change, mv.balance_after_units, mv.unit_cost_paise, mv.reference_type, "
        "mv.reference_id, mv.order_id, mv.actor_id, mv.remarks, mv.lot_id, "
        "mv.medication_id AS drug_id, mv.store_id, "
        "l.lot_number, l.expiration_date, s.code AS store_code, s.name AS store_name, "
        "m.name AS drug_name, m.generic_name, m.form, m.strength, m.dispense_unit, "
        "u.name AS actor_name "
        "FROM helix_stock_movements mv "
        "LEFT JOIN helix_inventory_lots l ON l.id = mv.lot_id "
        "LEFT JOIN stores s ON s.id = mv.store_id "
        "LEFT JOIN helix_medication_master m ON m.id = mv.medication_id "
        "LEFT JOIN users u ON u.id = mv.actor_id "
        "WHERE mv.seq IS NOT NULL"
    )
    params: list[Any] = []
    if drug_id:
        sql += " AND mv.medication_id = ?"
        params.append(drug_id)
    if store_id is not None:
        sql += " AND mv.store_id = ?"
        params.append(int(store_id))
    if lot_id:
        sql += " AND mv.lot_id = ?"
        params.append(lot_id)
    if before_seq is not None:
        sql += " AND mv.seq < ?"
        params.append(int(before_seq))
    sql += " ORDER BY mv.seq DESC LIMIT ?"
    params.append(size + 1)
    with _reading(conn) as db:
        rows = [dict(r) for r in db.execute(sql, params).fetchall()]
    has_more = len(rows) > size
    rows = rows[:size]
    movements = []
    for r in rows:
        kind = r["movement_type"] or r["reason"]
        patient_linked = bool(r["order_id"]) or (r["reference_type"] in PATIENT_REFERENCE_TYPES)
        if patient_linked:
            reference_label = PATIENT_ORDER_LABEL
        elif r["reference_type"]:
            reference_label = _REFERENCE_LABELS.get(r["reference_type"],
                                                    r["reference_type"].replace("_", " "))
        else:
            reference_label = None
        movements.append({
            "id": r["id"],
            "seq": int(r["seq"]),
            "occurred_at": r["occurred_at"] or r["timestamp"],
            "movement_type": kind,
            "movement_label": MOVEMENT_LABELS.get(kind or "", (kind or "Movement").replace("_", " ")),
            "quantity_change": int(r["quantity_change"] or 0),
            "balance_after_units": r["balance_after_units"],
            "unit_cost_paise": r["unit_cost_paise"],
            "lot_id": r["lot_id"],
            "lot_number": r["lot_number"],
            "expiration_date": (r["expiration_date"] or None) and str(r["expiration_date"])[:10],
            **_drug_fields(r),
            "store_id": r["store_id"],
            "store_code": r["store_code"],
            "store_label": _store_label(r),
            "actor_id": r["actor_id"],
            "actor_name": r["actor_name"],
            "reference_type": r["reference_type"],
            "reference_id": None if patient_linked else r["reference_id"],
            "reference_label": reference_label,
            "patient_linked": patient_linked,
            "remarks": None if patient_linked else r["remarks"],
        })
    return {
        "movements": movements,
        "has_more": has_more,
        "next_before_seq": movements[-1]["seq"] if has_more and movements else None,
    }


# ═════════════════════════════════════════════════════════════════════════════
#  Reorder
# ═════════════════════════════════════════════════════════════════════════════

def _ceil_div(a: int, b: int) -> int:
    return -(-a // b)


def _shelf_stores(conn) -> dict[str, Optional[dict]]:
    """The active dispensing store and the first active NDPS vault, where a
    drug's hospital reorder level is ordered into."""
    out: dict[str, Optional[dict]] = {"dispensing": None, "ndps_vault": None}
    for kind in out:
        row = conn.execute(
            "SELECT id, code, name, pharmacy_kind FROM stores WHERE pharmacy_kind = ? "
            "AND is_active = 1 ORDER BY id LIMIT 1", (kind,)).fetchone()
        out[kind] = dict(row) if row else None
    return out


def reorder_report(conn=None, *, store_id: Optional[int] = None, include_unset: bool = False,
                   needs_only: bool = False, today: Optional[str] = None) -> dict:
    """What needs ordering. Returns {"today", "rows"}.

    Each row is one of:
      * basis 'par': a store's active par level. Its trigger is the store's
        reorder level when set (at or below it), else below par. Its target is
        the store maximum, else par. The position is the store's usable units
        plus units in transit to it plus units on order for it.
      * basis 'drug_default': the drug master's reorder level, for a drug with
        no par level at the store it is dispensed from. Judged on dispensable
        units on the shelf plus units in transit to, and on order for, the
        shelf store (the NDPS vault for a narcotic or psychotropic, else the
        dispensing store). The suggestion is the drug's reorder quantity, or
        none when that is not set.
      * basis 'none' (only with include_unset): an active drug with no level
        anywhere. Its level is None and reads 'No reorder level set'.
    """
    today = today or stock.site_today()
    with _reading(conn) as db:
        usable: dict[tuple[int, str], int] = {}
        for r in db.execute(
                "SELECT l.store_id, l.medication_id, SUM(l.quantity) AS units "
                "FROM helix_inventory_lots l WHERE l.quantity > 0 AND l.store_id IS NOT NULL "
                "AND l.status = 'available' AND date(l.expiration_date) >= ? "
                "GROUP BY l.store_id, l.medication_id", (today,)).fetchall():
            usable[(int(r["store_id"]), r["medication_id"])] = int(r["units"] or 0)
        dispensable: dict[str, int] = {}
        for r in db.execute(
                f"SELECT l.medication_id, SUM(l.quantity) AS units FROM helix_inventory_lots l "
                f"WHERE {stock.dispensable_lot_sql('l')} GROUP BY l.medication_id",
                (today,)).fetchall():
            dispensable[r["medication_id"]] = int(r["units"] or 0)
        transit = in_transit_units(db)
        ordered = on_order_units(db)
        shelves = _shelf_stores(db)
        pars = [dict(r) for r in db.execute(
            "SELECT p.store_id, p.helix_medication_id AS drug_id, p.par_units, "
            "p.reorder_level_units, p.max_units, s.code AS store_code, s.name AS store_name, "
            "s.pharmacy_kind AS store_kind "
            "FROM pharmacy_par_levels p JOIN stores s ON s.id = p.store_id "
            "WHERE p.is_active = 1 AND s.is_active = 1 AND s.pharmacy_kind IS NOT NULL"
        ).fetchall()]
        drugs = {r["id"]: dict(r) for r in db.execute(
            "SELECT id, name, generic_name, form, strength, dispense_unit, pack_size_units, "
            "purchase_uom, reorder_level_units, reorder_qty_units, ndps_category "
            "FROM helix_medication_master WHERE is_active = 1 "
            "AND COALESCE(identity_status, 'active') = 'active'").fetchall()}

    def drug_info(did: str) -> dict:
        d = drugs.get(did) or {}
        return {"drug_id": did, "drug_name": d.get("name") or did,
                "generic_name": d.get("generic_name"), "form": d.get("form"),
                "strength": d.get("strength"), "dispense_unit": d.get("dispense_unit"),
                "pack_size_units": d.get("pack_size_units"),
                "purchase_uom": d.get("purchase_uom"),
                "ndps": (d.get("ndps_category") in stock.NDPS_CONTROLLED)}

    def packs_for(units: Optional[int], did: str) -> Optional[int]:
        size = (drugs.get(did) or {}).get("pack_size_units")
        if units is None or not size:
            return None
        return _ceil_div(int(units), int(size))

    rows: list[dict] = []
    par_keys = set()
    for p in pars:
        did = p["drug_id"]
        if did not in drugs:
            continue
        sid = int(p["store_id"])
        par_keys.add((sid, did))
        level_set = p["reorder_level_units"] is not None
        level = int(p["reorder_level_units"]) if level_set else int(p["par_units"])
        target = int(p["max_units"]) if p["max_units"] is not None else int(p["par_units"])
        on_hand = usable.get((sid, did), 0)
        moving = transit.get((sid, did), 0)
        coming = ordered.get((sid, did), 0)
        position = on_hand + moving + coming
        triggered = position <= level if level_set else position < level
        suggested = max(0, target - position) if triggered else 0
        rows.append({
            "store_id": sid, "store_code": p["store_code"],
            "store_label": p["store_name"] or p["store_code"],
            "store_kind": p["store_kind"], **drug_info(did),
            "basis": "par",
            "basis_label": "Store par level",
            "reorder_level_units": level,
            "level_label": (f"Reorder at {level}" if level_set else f"Keep at least {level}"),
            "target_units": target,
            "stock_units": on_hand, "in_transit_units": moving, "on_order_units": coming,
            "position_units": position,
            "needs_reorder": bool(triggered and suggested > 0),
            "suggested_units": suggested if triggered and suggested > 0 else None,
            "suggested_packs": packs_for(suggested, did) if triggered and suggested > 0 else None,
            "note": None,
        })

    for did, d in drugs.items():
        ndps = d.get("ndps_category") in stock.NDPS_CONTROLLED
        shelf = shelves["ndps_vault" if ndps else "dispensing"]
        sid = int(shelf["id"]) if shelf else None
        if sid is not None and (sid, did) in par_keys:
            continue
        if d.get("reorder_level_units") is None:
            if include_unset and not any(k[1] == did for k in par_keys):
                rows.append({
                    "store_id": sid, "store_code": shelf["code"] if shelf else None,
                    "store_label": (shelf["name"] if shelf else LEGACY_STORE_LABEL),
                    "store_kind": shelf["pharmacy_kind"] if shelf else None, **drug_info(did),
                    "basis": "none", "basis_label": NO_LEVEL_LABEL,
                    "reorder_level_units": None, "level_label": NO_LEVEL_LABEL,
                    "target_units": None,
                    "stock_units": dispensable.get(did, 0),
                    "in_transit_units": transit.get((sid, did), 0) if sid is not None else 0,
                    "on_order_units": sum(u for (s, x), u in ordered.items() if x == did),
                    "position_units": None, "needs_reorder": False,
                    "suggested_units": None, "suggested_packs": None, "note": None,
                })
            continue
        level = int(d["reorder_level_units"])
        on_shelf = dispensable.get(did, 0)
        moving = transit.get((sid, did), 0) if sid is not None else 0
        coming = sum(u for (s, x), u in ordered.items() if x == did)
        position = on_shelf + moving + coming
        triggered = position <= level
        qty = d.get("reorder_qty_units")
        note = None
        if triggered and qty is None:
            note = "Reorder quantity not set"
        if sid is None:
            note = ("Set up the NDPS vault before ordering this drug" if ndps
                    else "Set up the dispensing store before ordering")
        rows.append({
            "store_id": sid, "store_code": shelf["code"] if shelf else None,
            "store_label": (shelf["name"] if shelf else LEGACY_STORE_LABEL),
            "store_kind": shelf["pharmacy_kind"] if shelf else None, **drug_info(did),
            "basis": "drug_default", "basis_label": "Drug reorder level",
            "reorder_level_units": level, "level_label": f"Reorder at {level}",
            "target_units": None,
            "stock_units": on_shelf, "in_transit_units": moving, "on_order_units": coming,
            "position_units": position,
            "needs_reorder": bool(triggered),
            "suggested_units": int(qty) if triggered and qty is not None else None,
            "suggested_packs": packs_for(int(qty), did) if triggered and qty is not None else None,
            "note": note,
        })

    if store_id is not None:
        rows = [r for r in rows if r["store_id"] == int(store_id)]
    if needs_only:
        rows = [r for r in rows if r["needs_reorder"]]
    rows.sort(key=lambda r: (not r["needs_reorder"], r["basis"] == "none",
                             (r["drug_name"] or "").lower(), r["store_label"] or ""))
    return {"today": today, "rows": rows}


# ═════════════════════════════════════════════════════════════════════════════
#  Near expiry
# ═════════════════════════════════════════════════════════════════════════════

def _clamp_days(days: Optional[int]) -> int:
    import config

    value = config.PHARMACY_NEAR_EXPIRY_DAYS if days is None else days
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("the near-expiry window is a whole number of days")
    if value < 1 or value > MAX_NEAR_EXPIRY_DAYS:
        raise ValueError(f"the near-expiry window is 1 to {MAX_NEAR_EXPIRY_DAYS} days")
    return value


def near_expiry(conn=None, *, days: Optional[int] = None, store_id: Optional[int] = None,
                today: Optional[str] = None) -> dict:
    """Available lots holding units that expire within `days` (default
    ORB_PHARMACY_NEAR_EXPIRY_DAYS), soonest first, valued at cost and at MRP,
    and the available lots already past expiry or undated that are still on a
    shelf. A lot with no units left is not listed. Returns {"today", "days",
    "cutoff", "lots", "expired", "totals"}."""
    window = _clamp_days(days)
    today = today or stock.site_today()
    cutoff = (date.fromisoformat(today) + timedelta(days=window)).isoformat()
    listing = list_lots(conn, store_id=store_id, today=today, near_days=window,
                        available_by=cutoff, limit=5000)
    soon = [lot for lot in listing["lots"]
            if lot["status"] == "available" and lot["quantity"] > 0
            and lot["expiry_state"] == "near_expiry"]
    gone = [lot for lot in listing["lots"]
            if lot["status"] == "available" and lot["quantity"] > 0
            and lot["expiry_state"] in ("expired", "undated")]

    def total(lots: list[dict]) -> dict:
        return {
            "lots": len(lots),
            "units": sum(lot["quantity"] for lot in lots),
            "cost_value_paise": sum(lot["cost_value_paise"] for lot in lots
                                    if lot["cost_value_paise"] is not None),
            "cost_unpriced_lots": sum(1 for lot in lots if lot["cost_value_paise"] is None),
            "mrp_value_paise": sum(lot["mrp_value_paise"] for lot in lots
                                   if lot["mrp_value_paise"] is not None),
            "mrp_unpriced_lots": sum(1 for lot in lots if lot["mrp_value_paise"] is None),
        }

    return {"today": today, "days": window, "cutoff": cutoff, "lots": soon, "expired": gone,
            "totals": {"near_expiry": total(soon), "expired": total(gone)}}


# ═════════════════════════════════════════════════════════════════════════════
#  Overview tiles and reconciliation
# ═════════════════════════════════════════════════════════════════════════════

def overview(conn=None) -> dict:
    """The numbers the Stock screen leads with."""
    import config

    today = stock.site_today()
    with _reading(conn) as db:
        summary = stock_by_store(db, today=today, limit=5000)
        reorder = reorder_report(db, include_unset=True, today=today)
        expiry = near_expiry(db, days=int(config.PHARMACY_NEAR_EXPIRY_DAYS), today=today)
        po_counts = {r["status"]: int(r["n"]) for r in db.execute(
            "SELECT status, COUNT(*) AS n FROM purchase_orders WHERE domain = 'pharmacy' "
            "GROUP BY status").fetchall()}
        legacy = int(db.execute(
            f"SELECT COUNT(*) FROM helix_inventory_lots l WHERE l.quantity > 0 AND "
            f"{stock.awaiting_adoption_sql('l')}").fetchone()[0])
    rows = reorder["rows"]
    return {
        "today": today,
        "below_reorder_level": sum(1 for r in rows if r["needs_reorder"]),
        "without_reorder_level": sum(1 for r in rows if r["basis"] == "none"),
        "near_expiry_days": expiry["days"],
        "near_expiry_lots": expiry["totals"]["near_expiry"]["lots"],
        "near_expiry_units": expiry["totals"]["near_expiry"]["units"],
        "expired_on_shelf_units": summary["totals"]["expired_units"],
        "quarantined_units": summary["totals"]["quarantined_units"],
        "in_transit_units": summary["totals"]["in_transit_units"],
        "dispensable_units": summary["totals"]["dispensable_units"],
        "legacy_lots": legacy,
        "purchase_orders": {
            "draft": po_counts.get("draft", 0),
            "awaiting_approval": po_counts.get("pending_approval", 0),
            "approved": po_counts.get("approved", 0),
            "open_with_vendor": po_counts.get("issued", 0) + po_counts.get("partially_received", 0),
        },
    }


def reconcile_report(conn=None) -> dict:
    """pharmacy_stock_service.reconcile, read-only: does every lot's quantity
    equal the sum of its ledger?"""
    with _reading(conn) as db:
        return stock.reconcile(db)
