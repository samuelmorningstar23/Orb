"""Facility scope for drug stock and the pharmacy store (multisite-14 and
multisite-8h3, W13-C, wave 13, 20 October 2026).

Orb keeps the facilities of one hospital group apart by row-level facility
ids (ruling MS-R1 (a)). Ruling MS-R9 (a) makes pharmacy stock and the
pharmacy store per facility: a drug licence is per premises and stock must be
where the patient is, so F109's "hospital-wide" pharmacy means facility-wide.
This module is the one place the stock and store lanes ask which facility a
store, lot or document is at, and whether the caller works there.

ONE FACILITY ON RECORD (every install today, the Ward Record and the demos).
The caller's facility scope is None and multi() is False, both answered from
facility_service's cached count. Every clause here is then the empty string,
every gate returns before reading anything, and every lane runs exactly the
statement it always ran.

WHERE A ROW'S FACILITY IS
  store            stores.facility_id (migration 75; stamped once by
                   procurement's store core from its department, ward or the
                   person in charge, and never moved).
  lot              helix_inventory_lots.facility_id, stamped from its store by
                   pharmacy_stock_service; a lot not yet in a store (legacy
                   stock) is at facility 1. A lot in a store is at a facility
                   only when its own stamp AND its store both say so, so a lot
                   whose two disagree is at no facility: no facility-bound
                   reader sees it, no dispense takes it, and reconcile()
                   reports it.
  movement         helix_stock_movements.facility_id, stamped from its lot.
  purchase order,
  goods receipt,
  indent           their own facility_id (migration 75, procurement's cores).
  transfer         both its stores (a transfer never crosses facilities).
  vendor return,
  stock count,
  NDPS variance    their store's.
  ward return      its patient's (the chart rule) and its store's.

THE SCOPE VALUES follow permissions.scope_patient_ids_sql: None is no facility
limit (one facility on record, a group-wide account by enterprise grant, a
service account); the empty set is nothing at all (a department the master
does not list); otherwise the facility ids the caller works at.

The clauses inline the facility ids as integer literals (each one passed
through int()), so the predicates the stock service already documents by
their placeholder count keep that count.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from typing import Any

from database import db_session
from fastapi import HTTPException

MAIN_FACILITY_ID = 1

#: The lane an authorization 'facility_document_denied' row names.
LANE = "pharmacy_store"

#: The predicate an empty scope adds: true for no row.
NEVER = " AND 0"


# ── The scope ─────────────────────────────────────────────────────────────────


def multi(db: Any = None) -> bool:
    """True once more than one facilities row exists (the cached count; a
    statement at most every facility_service.CACHE_SECONDS)."""
    from services import facility_service

    return bool(facility_service.multi_facility_data(db=db))


def scope(actor: Mapping | None, *, db: Any = None) -> frozenset[int] | None:
    """The facilities `actor` works at, or None for no facility limit
    (facility_service.facility_scope_for, the one resolver). BLOCKING on a
    cache miss: plain-def code only, and called before the caller's write
    transaction opens (or with that transaction's connection as `db`)."""
    from services import facility_service

    return facility_service.facility_scope_for(dict(actor or {}), db=db)


def _ids(facilities: Iterable[int]) -> tuple[int, ...]:
    return tuple(sorted({int(f) for f in facilities}))


def ids_sql(facilities: Iterable[int]) -> str:
    """'1, 2': the ids as integer literals for an IN list."""
    return ", ".join(str(i) for i in _ids(facilities))


def in_scope(facility_id: Any, facilities: Iterable[int] | None) -> bool:
    """Is a row at `facility_id` inside the scope? Always for None; never
    for a row whose facility is unknown (None) while a limit applies."""
    if facilities is None:
        return True
    if facility_id is None:
        return False
    return int(facility_id) in set(_ids(facilities))


def column_sql(column: str, facilities: Iterable[int] | None) -> str:
    """' AND <column> IN (1, 2)' for a row that carries its own facility;
    '' for None and NEVER for the empty set. `column` is code, never request
    text."""
    if facilities is None:
        return ""
    ids = _ids(facilities)
    if not ids:
        return NEVER
    return f" AND {column} IN ({ids_sql(ids)})"


def store_ids_sql(column: str, facilities: Iterable[int] | None) -> str:
    """' AND <column> IN (the stores at those facilities)' for a row that
    names a store (a transfer end, a register entry, a variance)."""
    if facilities is None:
        return ""
    ids = _ids(facilities)
    if not ids:
        return NEVER
    return (
        f" AND {column} IN (SELECT pf_s.id FROM stores pf_s "
        f"WHERE pf_s.facility_id IN ({ids_sql(ids)}))"
    )


def lot_sql(alias: str, facilities: Iterable[int] | None) -> str:
    """' AND <the lot is at one of those facilities>' for a
    helix_inventory_lots row aliased `alias`: its own stamp, and its store's
    when it is in one (see the module note). '' for None."""
    if facilities is None:
        return ""
    ids = _ids(facilities)
    if not ids:
        return NEVER
    marks = ids_sql(ids)
    return (
        f" AND {alias}.facility_id IN ({marks}) AND ({alias}.store_id IS NULL OR EXISTS ("
        f"SELECT 1 FROM stores pf_ls WHERE pf_ls.id = {alias}.store_id "
        f"AND pf_ls.facility_id IN ({marks})))"
    )


def facility_ids(db: Any) -> list[int]:
    """Every facility on record, lowest id first."""
    return [int(r[0]) for r in db.execute("SELECT id FROM facilities ORDER BY id").fetchall()]


# ── One row's facility ────────────────────────────────────────────────────────


def _one(db: Any, sql: str, params: tuple) -> int | None:
    row = db.execute(sql, params).fetchone()
    if row is None or row[0] is None:
        return None
    return int(row[0])


def store_facility(db: Any, store_id: Any) -> int | None:
    """The facility a pharmacy store is at, or None for no such store."""
    try:
        sid = int(store_id)
    except (TypeError, ValueError):
        return None
    return _one(
        db, "SELECT facility_id FROM stores WHERE id = ? AND pharmacy_kind IS NOT NULL", (sid,)
    )


def store_code_facility(db: Any, code: Any) -> int | None:
    """The facility of the pharmacy store with this code (ignoring case)."""
    text = code.strip().upper() if isinstance(code, str) else ""
    if not text:
        return None
    return _one(
        db,
        "SELECT facility_id FROM stores WHERE upper(code) = ? AND pharmacy_kind IS NOT NULL",
        (text,),
    )


def lot_facility(db: Any, lot_id: Any) -> int | None:
    """The facility a lot is at: its store's when it is in one, else its own
    stamp (a legacy lot). A lot whose stamp and store disagree is at none
    (None), so no facility-bound caller can take it."""
    row = db.execute(
        "SELECT l.facility_id, l.store_id, s.facility_id AS store_facility "
        "FROM helix_inventory_lots l LEFT JOIN stores s ON s.id = l.store_id WHERE l.id = ?",
        (lot_id,),
    ).fetchone()
    if row is None:
        return None
    own = int(row["facility_id"]) if row["facility_id"] is not None else MAIN_FACILITY_ID
    if row["store_id"] is None:
        return own
    if row["store_facility"] is None or int(row["store_facility"]) != own:
        return None
    return own


def _document(table: str, *, domain: str | None = None) -> Callable[[Any, Any], set[int]]:
    def lookup(db: Any, doc_id: Any) -> set[int]:
        try:
            did = int(doc_id)
        except (TypeError, ValueError):
            return set()
        sql = f"SELECT facility_id FROM {table} WHERE id = ?"
        params: tuple = (did,)
        if domain is not None:
            sql += " AND domain = ?"
            params = (did, domain)
        fid = _one(db, sql, params)
        return set() if fid is None else {fid}

    return lookup


def _via_store(table: str) -> Callable[[Any, Any], set[int]]:
    def lookup(db: Any, doc_id: Any) -> set[int]:
        try:
            did = int(doc_id)
        except (TypeError, ValueError):
            return set()
        row = db.execute(f"SELECT store_id FROM {table} WHERE id = ?", (did,)).fetchone()
        if row is None or row[0] is None:
            return set()
        fid = store_facility(db, row[0])
        return set() if fid is None else {fid}

    return lookup


def _transfer(db: Any, transfer_id: Any) -> set[int]:
    try:
        tid = int(transfer_id)
    except (TypeError, ValueError):
        return set()
    row = db.execute(
        "SELECT from_store_id, to_store_id FROM pharmacy_transfers WHERE id = ?", (tid,)
    ).fetchone()
    if row is None:
        return set()
    found = {store_facility(db, row[0]), store_facility(db, row[1])}
    found.discard(None)
    return {int(f) for f in found if f is not None}


def _store_by_id(db: Any, store_id: Any) -> set[int]:
    fid = store_facility(db, store_id)
    return set() if fid is None else {fid}


def _store_by_code(db: Any, code: Any) -> set[int]:
    fid = store_code_facility(db, code)
    return set() if fid is None else {fid}


#: What each kind of document is, for the lookups and the audit row.
_LOOKUPS: dict[str, tuple[str, Callable[[Any, Any], set[int]]]] = {
    "store": ("pharmacy_store", _store_by_id),
    "store_code": ("pharmacy_store", _store_by_code),
    "purchase_order": ("pharmacy_purchase_order", _document("purchase_orders", domain="pharmacy")),
    "goods_receipt": ("pharmacy_goods_receipt", _document("goods_receipts", domain="pharmacy")),
    "indent": ("pharmacy_indent", _document("indents", domain="pharmacy")),
    "transfer": ("pharmacy_transfer", _transfer),
    "vendor_return": ("pharmacy_vendor_return", _via_store("pharmacy_vendor_returns")),
    "stock_count": ("pharmacy_stock_count", _via_store("pharmacy_stock_counts")),
    "ndps_variance": ("ndps_variance", _via_store("ndps_variances")),
}

KINDS = tuple(_LOOKUPS)


def document_facilities(db: Any, kind: str, doc_id: Any) -> set[int]:
    """The facilities one document is at (two only for a transfer whose ends
    disagree, which create_transfer never writes); the empty set for a
    document no row holds."""
    return _LOOKUPS[kind][1](db, doc_id)


# ── The gate ──────────────────────────────────────────────────────────────────


def _meta(audit_ctx: Any) -> dict:
    """The request fields of an audit row, from a Starlette request or the
    {request_path, http_method, ip_address} dict the pharmacy routes build."""
    if audit_ctx is None:
        return {}
    if isinstance(audit_ctx, Mapping):
        return dict(audit_ctx)
    from services.facility_documents import request_meta

    return request_meta(audit_ctx)


def document_outside(
    actor: Mapping,
    kind: str,
    doc_id: Any,
    *,
    audit_ctx: Any = None,
    facilities: Any = None,
    target_type: str | None = None,
) -> bool:
    """True, after an authorization 'facility_document_denied' row, when the
    document is at a facility outside the caller's scope. False with one
    facility on record (nothing is read), for a group-wide caller, and for a
    document no row holds (the lane then answers as it always did).

    Reads in a session of its own, so it is called BEFORE the caller's write
    transaction opens: the audit row is written whatever the lane does next."""
    if facilities is None:
        facilities = scope(actor)
    if facilities is None:
        return False
    with db_session() as db:
        found = document_facilities(db, kind, doc_id)
    if not found or all(in_scope(f, facilities) for f in found):
        return False
    outside_at = min(f for f in found if not in_scope(f, facilities))
    from services.facility_documents import audit_document_denial

    audit_document_denial(
        user=dict(actor or {}),
        lane=LANE,
        target_type=target_type or _LOOKUPS[kind][0],
        target_id=None if doc_id is None else str(doc_id),
        document_facility_id=outside_at,
        facilities=frozenset(facilities),
        meta=_meta(audit_ctx),
    )
    return True


def gate(
    actor: Mapping,
    kind: str,
    doc_id: Any,
    request: Any = None,
    *,
    message: str,
    facilities: Any = None,
) -> None:
    """The route-side gate: a 404 {error: not_found, message} (the same answer
    as a document that does not exist) after the authorization row, for a
    document at another facility. A no-op with one facility on record."""
    if document_outside(actor, kind, doc_id, audit_ctx=request, facilities=facilities):
        raise HTTPException(status_code=404, detail={"error": "not_found", "message": message})


# ── Words and labels ──────────────────────────────────────────────────────────


def facility_view(facility_id: Any, *, db: Any = None) -> dict | None:
    """{id, code, name} of one facility, or None."""
    if facility_id is None:
        return None
    from services import facility_service

    ident = facility_service.facility_identity(int(facility_id), db=db)
    if not ident:
        return {"id": int(facility_id), "code": None, "name": None}
    return {"id": ident.get("id"), "code": ident.get("code"), "name": ident.get("name")}


def scope_view(facilities: Iterable[int] | None, *, db: Any = None) -> dict | None:
    """The one facility a screen's stock belongs to, as {id, code, name}:
    only when more than one facility is on record and the caller works at
    exactly one. None otherwise (one facility, or a group-wide caller)."""
    if facilities is None:
        return None
    ids = _ids(facilities)
    if len(ids) != 1 or not multi(db):
        return None
    return facility_view(ids[0], db=db)


def facility_words(facility_id: Any, *, db: Any = None) -> str:
    """'<name>' (or its code, or 'this facility') for a refusal a pharmacist
    reads."""
    view = facility_view(facility_id, db=db) or {}
    return view.get("name") or view.get("code") or "this facility"


def patient_facility(db: Any, patient_id: Any) -> int | None:
    """The facility a patient's medicines are dispensed at: the active
    admission's, else the latest encounter's (the chart rule,
    facility_identity.document_facility_id). None with one facility on
    record, with nothing read beyond the cached count."""
    if not multi(db):
        return None
    from services import facility_identity

    return int(facility_identity.document_facility_id(db, patient_id=patient_id))


def account_works_at(db: Any, user_id: Any, facility_id: Any) -> bool:
    """Does this account's facility scope hold `facility_id`? True for a
    group-wide account, and for everyone with one facility on record."""
    if facility_id is None or not multi(db):
        return True
    row = db.execute(
        "SELECT id, role, department FROM users WHERE id = ?", (str(user_id),)
    ).fetchone()
    if row is None:
        return False
    found = scope({"id": row[0], "role": row[1], "department": row[2]}, db=db)
    return in_scope(facility_id, found)


__all__ = [
    "KINDS",
    "LANE",
    "MAIN_FACILITY_ID",
    "NEVER",
    "account_works_at",
    "column_sql",
    "document_facilities",
    "document_outside",
    "facility_ids",
    "facility_view",
    "facility_words",
    "gate",
    "ids_sql",
    "in_scope",
    "lot_facility",
    "lot_sql",
    "multi",
    "patient_facility",
    "scope",
    "scope_view",
    "store_code_facility",
    "store_facility",
    "store_ids_sql",
]
