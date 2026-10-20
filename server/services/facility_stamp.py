"""Which facility a money, stores or asset row belongs to (multisite-7b, W11-C,
migration 75).

Migration 75 gave invoices, receipts, refunds, claims, deposits, cash
counters, charges, stores, indents, purchase orders, goods receipts,
housekeeping units, biomedical assets, kitchen indents, NABH incidents and
evidence packs, and ABDM care contexts their own facility_id. This module is
the one place their writers ask which facility a new row belongs to, so every
writer answers the question the same way.

THE RULE
  * One facility on record (every install today, the Ward Record, the demos):
    facility 1, decided from facility_service's cached facility count. No
    statement is run and nothing else is read, so single-site behaviour is
    exactly what it was.
  * More than one: the facility the row's own sources name. A writer lists
    its sources in priority order (the counter a receipt is taken at, the
    bill it pays, the store an order buys for, the department an asset sits
    in), and they must agree: two sources at different facilities refuse the
    write in words (facility_mismatch) before anything is written, because a
    document belongs to one facility. A writer whose sources name nothing
    falls back to a weaker source it names (usually the patient's facility or
    the account doing the work) and, when even that is silent, either refuses
    (facility_unknown) or, for a report that must never be refused (a NABH
    incident), takes facility 1.

Lookups take the caller's connection and run only with multi-facility data.
The SQL that reads a facility off admissions, invoices and payments lives here
on purpose, so the multi-site SQL-site registry classifies one module.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

MAIN_FACILITY_ID = 1

MISMATCH = "facility_mismatch"
UNKNOWN = "facility_unknown"

#: A source's value: a facility id, None (names no facility), or a callable
#: returning either, evaluated only when more than one facility is on record.
Source = int | None | Callable[[], int | None]


class FacilityStampError(ValueError):
    """A write refused because its facility cannot be told, or because two of
    its sources sit at different facilities. Each writer maps it onto its own
    refusal type; `code` is MISMATCH or UNKNOWN and `message` is the words."""

    def __init__(self, code: str, message: str, *, sources: dict | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.sources = dict(sources or {})


def multi_facility(db: Any) -> bool:
    """True once more than one facilities row exists (facility_service's
    cached count; a statement at most every CACHE_SECONDS)."""
    from services import facility_service

    return facility_service.multi_facility_data(db=db)


def row_facility(row: Any) -> int | None:
    """The facility_id of a dict or sqlite3.Row, or None when the row is
    missing, has no such column, or holds NULL."""
    if row is None:
        return None
    try:
        keys = row.keys()
    except AttributeError:
        return None
    if "facility_id" not in keys or row["facility_id"] is None:
        return None
    return int(row["facility_id"])


def facility_label(db: Any, facility_id: int) -> str:
    """'<name> (<code>)' for a refusal's words, or 'facility <id>' when the
    facility has no row or no name."""
    try:
        from services import facility_service

        ident = facility_service.facility_identity(int(facility_id), db=db) or {}
    except Exception:
        ident = {}
    name, code = ident.get("name"), ident.get("code")
    if name and code:
        return f"{name} ({code})"
    return name or code or f"facility {facility_id}"


def _value(source: Source) -> int | None:
    value = source() if callable(source) else source
    return None if value is None else int(value)


def resolve(
    db: Any,
    sources: Sequence[tuple[str, Source]],
    *,
    what: str,
    fallback: Source = None,
    agree: bool = True,
    refuse_unknown: bool = True,
    unknown_hint: str = "",
) -> int:
    """The facility a new `what` belongs to.

    `sources` are (label, value) pairs in priority order; labels are plain
    words ("the counter", "the bill") used in the refusal. With agree=True
    every source that names a facility must name the same one; with
    agree=False the first that names one wins (a report that is never
    refused). With none, `fallback` is asked. With that silent too, the write
    is refused (refuse_unknown) or takes facility 1."""
    if not multi_facility(db):
        return MAIN_FACILITY_ID
    named: dict[str, int] = {}
    for label, source in sources:
        value = _value(source)
        if value is None:
            continue
        if not agree:
            return value
        named[label] = value
    distinct = set(named.values())
    if len(distinct) > 1:
        parts = ", ".join(
            f"{label} is at {facility_label(db, fid)}" for label, fid in named.items()
        )
        raise FacilityStampError(
            MISMATCH,
            f"This {what} cannot be saved: {parts}. A {what} belongs to one facility, "
            "so nothing was saved.",
            sources=named,
        )
    if distinct:
        return distinct.pop()
    value = _value(fallback)
    if value is not None:
        return value
    if not refuse_unknown:
        return MAIN_FACILITY_ID
    raise FacilityStampError(
        UNKNOWN,
        f"Orb cannot tell which facility this {what} belongs to, so nothing was saved."
        + (f" {unknown_hint}" if unknown_hint else ""),
    )


# ── Lookups (caller's connection; run only with multi-facility data) ────────


def patient_facility(
    db: Any, *, patient_id: str | None = None, admission_id: str | None = None
) -> int:
    """The facility of the admission, else of the patient's latest encounter,
    else 1 (facility_identity.document_facility_id, the rule the supplier
    snapshot and the document numbering already use)."""
    from services import facility_identity

    return facility_identity.document_facility_id(
        db, patient_id=patient_id, admission_id=admission_id
    )


def admission_facility(db: Any, admission_id: str | None, *, patient_id: str | None) -> int | None:
    """The facility of this patient's admission; None when the id names no
    admission of theirs."""
    if not admission_id or not patient_id:
        return None
    row = db.execute(
        "SELECT facility_id FROM admissions WHERE id = ? AND patient_id = ?",
        (str(admission_id), str(patient_id)),
    ).fetchone()
    return None if row is None or row[0] is None else int(row[0])


def invoice_facility(db: Any, invoice_id: int | None) -> int | None:
    if invoice_id is None:
        return None
    row = db.execute("SELECT facility_id FROM invoices WHERE id = ?", (int(invoice_id),)).fetchone()
    return None if row is None or row[0] is None else int(row[0])


def payment_facility(db: Any, payment_id: int | None) -> int | None:
    if payment_id is None:
        return None
    row = db.execute("SELECT facility_id FROM payments WHERE id = ?", (int(payment_id),)).fetchone()
    return None if row is None or row[0] is None else int(row[0])


def store_facility(db: Any, store_id: int | None) -> int | None:
    if store_id is None:
        return None
    row = db.execute("SELECT facility_id FROM stores WHERE id = ?", (int(store_id),)).fetchone()
    return None if row is None or row[0] is None else int(row[0])


def department_facility(db: Any, department: str | None) -> int | None:
    """The facility that owns a department in the master (names compared
    ignoring case and outer spaces); None when the master does not list it."""
    from services import facility_service

    return facility_service.department_facility(department, db=db)


def ward_facility(db: Any, *, ward_id: str | None = None, ward: str | None = None) -> int | None:
    """The facility of a ward in the bed master, through its department: by
    id, else by code or name ignoring case. None when the master does not
    know the ward, or knows two wards of that name at different facilities."""
    if ward_id:
        rows = db.execute("SELECT department FROM wards WHERE id = ?", (str(ward_id),)).fetchall()
    elif isinstance(ward, str) and ward.strip():
        key = ward.strip().casefold()
        rows = [
            r
            for r in db.execute("SELECT code, name, department FROM wards").fetchall()
            if key in {str(r[0] or "").strip().casefold(), str(r[1] or "").strip().casefold()}
        ]
        rows = [(r[2],) for r in rows]
    else:
        return None
    found = {department_facility(db, r[0]) for r in rows}
    found.discard(None)
    return found.pop() if len(found) == 1 else None


def account_facility(db: Any, user_id: str | None) -> int | None:
    """The facility a staff account works at (facility_service.user_facility_id:
    its department's, else its home row's, else 1); None for an id that names
    no account, or an account whose department the master does not list."""
    if not user_id:
        return None
    row = db.execute(
        "SELECT id, role, department FROM users WHERE id = ?", (str(user_id),)
    ).fetchone()
    if row is None:
        return None
    from services import facility_service

    return facility_service.user_facility_id(
        {"id": row[0], "role": row[1], "department": row[2]}, db=db
    )
