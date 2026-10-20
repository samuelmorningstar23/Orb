"""Facility scope for the outpatient, registration and bed lanes (multisite-8h1,
W12-F, 20 October 2026).

Samuel ruled on 3 October 2026 that isolation between the facilities of one
hospital group is keyed on DATA: once the facilities table holds more than one
row, facility scope applies whatever ORB_MULTI_SITE says. With one facility on
record (every install today) every helper here answers "no facility limit"
before it reads anything, so the lanes that call it behave exactly as before.

The lanes that use this module (routes/opd_routes.py, registration_routes.py,
schedule_routes.py, the routes/adt package, services/opd_queue_service.py,
services/registration_service.py, services/adt, the bed master and the census)
ask four questions of it:

  * which facilities the signed-in account may see (scope_of, the same
    resolver every other lane uses: permissions.facility_scope);
  * which facilities an account named only by its id may see
    (account_scope_on, for a service function that is handed an actor id);
  * which facility a visit, an admission or a ward is at (visit_facility_on,
    admission_facility_on, ward_facility_on), read off the row itself
    (migrations 70 and 74 gave every one of them a facility_id);
  * whether that facility is outside the scope (outside).

A visit, an admission or a ward outside the caller's facilities answers 404,
exactly like one that does not exist, and the attempt writes an authorization
patient_scope_denied row with scope_cause 'facility' (audit_denial, the same
row permissions.require_patient_access writes), so a probe across a facility
boundary is always on the record.

Rulings applied here (multi-site epic): MS-R4 (a), a department, and so a ward
and a clinic, belongs to one facility; MS-R5 (a), a department-less account
sees what is at its own facility; MS-R3 (c), nothing moves between facilities.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

__all__ = [
    "account_scope_on",
    "admission_facility_on",
    "audit_denial",
    "facility_marks",
    "multi_facility",
    "outside",
    "scope_of",
    "visit_facility_on",
    "ward_facility_on",
    "wards_in_scope",
]


def multi_facility(db: Any = None) -> bool:
    """True once more than one facilities row exists (facility_service's
    cached count; a statement at most every CACHE_SECONDS)."""
    from services import facility_service

    return facility_service.multi_facility_data(db=db)


def scope_of(user: Any) -> frozenset[int] | None:
    """The facilities whose records `user` may see, or None for no facility
    limit (one facility on record, a group-wide administrator, a service
    account). BLOCKING on a cache miss: plain-def code only."""
    from permissions import facility_scope

    return facility_scope(user)


def account_scope_on(db: Any, user_id: Any) -> frozenset[int] | None:
    """scope_of for an account named by its id, read on the caller's
    connection. None while one facility is on record (nothing is read). With
    more than one, an id that names no account gets the empty set: a scope
    that cannot be told is never widened."""
    from services import facility_service

    if not facility_service.multi_facility_data(db=db):
        return None
    uid = str(user_id or "").strip()
    if not uid:
        return frozenset()
    row = db.execute("SELECT id, role, department FROM users WHERE id = ?", (uid,)).fetchone()
    if row is None:
        return frozenset()
    return facility_service.facility_scope_for(
        {"id": row[0], "role": row[1], "department": row[2]}, db=db
    )


def outside(facility_id: Any, facilities: frozenset[int] | None) -> bool:
    """Is a row at `facility_id` outside `facilities`? Never with no facility
    limit (None); always for a row whose facility cannot be read."""
    if facilities is None:
        return False
    if facility_id is None:
        return True
    try:
        return int(facility_id) not in facilities
    except (TypeError, ValueError):
        return True


def facility_marks(facilities: Iterable[int]) -> tuple[str, tuple[int, ...]]:
    """('?, ?', ids) for an IN (...) clause; ('NULL', ()) for none, which
    matches nothing."""
    ids = tuple(sorted({int(f) for f in facilities}))
    return (", ".join("?" for _ in ids) or "NULL"), ids


def _facility_of(row: Any) -> int | None:
    if row is None or row[0] is None:
        return None
    return int(row[0])


def visit_facility_on(db: Any, visit_id: Any) -> int | None:
    """The facility an outpatient visit is at (migration 74), or None when no
    visit has that id."""
    return _facility_of(
        db.execute("SELECT facility_id FROM opd_visits WHERE id = ?", (str(visit_id),)).fetchone()
    )


def admission_facility_on(db: Any, admission_id: Any) -> int | None:
    """The facility an admission is at (migration 70), or None when no
    admission has that id. An admission's facility never changes."""
    return _facility_of(
        db.execute(
            "SELECT facility_id FROM admissions WHERE id = ?", (str(admission_id),)
        ).fetchone()
    )


def ward_facility_on(db: Any, ward_id: Any) -> int | None:
    """The facility a bed-setup ward is at (migration 74), or None when no
    ward has that id."""
    if not ward_id:
        return None
    return _facility_of(
        db.execute("SELECT facility_id FROM wards WHERE id = ?", (str(ward_id),)).fetchone()
    )


def wards_in_scope(wards: Iterable[dict], facilities: frozenset[int] | None) -> list[dict]:
    """The wards (bed-setup rows as dicts) at one of `facilities`; every ward
    with no facility limit. A ward row read before migration 74 has no
    facility_id and counts as facility 1, the column's default."""
    rows = list(wards)
    if facilities is None:
        return rows
    return [w for w in rows if not outside(w.get("facility_id", 1), facilities)]


def audit_denial(
    *,
    user: dict,
    patient_id: str | None,
    dept: str | None,
    facilities: frozenset[int],
    facility_id: int | None,
    helper: str,
    request_meta: dict | None = None,
) -> None:
    """The authorization patient_scope_denied row for a facility-caused
    refusal, in the shape permissions.require_patient_access writes it
    (scope_cause 'facility', facility_scope, patient_facility_id,
    scope_helper). BLOCKING, on a connection of its own: call it after any
    write session has closed. Fail-soft like every authorization row."""
    from permissions import _audit_facility_denial

    meta = request_meta or {}
    _audit_facility_denial(
        user=user,
        patient_id=patient_id,
        dept=dept,
        facilities=facilities,
        patient_facility_id=facility_id,
        helper=helper,
        request_path=meta.get("request_path"),
        http_method=meta.get("http_method"),
        ip_address=meta.get("ip_address"),
        user_agent=meta.get("user_agent"),
    )
