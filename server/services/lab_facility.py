"""Facility scope for the laboratory lanes, and the facility a sample is
performed at (multisite-8h2, W13-D, wave 13, 20 October 2026).

Samuel ruled on 3 October 2026 that isolation between the facilities of one
hospital group is keyed on DATA: once the facilities table holds more than one
row, facility scope applies whatever ORB_MULTI_SITE says. With one facility on
record (every install today) permissions.facility_scope(user) answers None
before reading anything, every helper here then asks exactly the question the
laboratory asked before (permissions.patient_in_scope with the department
only), adds no clause and reads no extra row, and a new sample is stamped at
facility 1 without a statement.

THE PERFORMING FACILITY. lab_specimens.facility_id (migration 74) is the
facility whose laboratory performs the sample. The design (multisite.json:
"stamp performing_facility_id, because a central lab serving satellites is the
common pattern; the lab worklist scopes on the performing facility") keeps the
ordering facility derived (the patient's) and stamps the performing one. Until
a site can route a satellite's samples to a central laboratory (a site setting
no wave has built), a new sample is performed where its patient is: the
patient's active admission, else the latest encounter, else the facility the
patient was registered at (facility_identity.document_facility_id, the rule
the money documents use). A recollection keeps the rejected sample's facility,
and a planned tube only ever takes more tests from its own facility.

WHO SEES WHICH ROW (rulings MS-R4 (a) and MS-R5 (a)):
  * a department account (a ward nurse or doctor) sees the laboratory rows of
    its own patients: the department rule it always had, and the patient's
    latest encounter at one of its facilities
    (permissions.scope_patient_ids_sql and patient_in_scope with facilities=);
  * a department-less account (the laboratory roles, the administrator) sees
    the work of its own facility's laboratory: a row about a sample by the
    sample's performing facility, and a row with no sample (an order not yet
    taken in, an interfaced critical) by its patient's facility. That is
    MS-R5 (a)'s "their facility's own queue items by the document's
    facility": the laboratory that holds a tube finishes it, even when its
    patient's latest encounter has moved to another facility since.
  * an empty scope (a department no facility lists) sees nothing.

A RELEASED REPORT IS PART OF THE PATIENT'S CHART. Ruling MS-R2 (a): once a
patient is in scope the whole chart is visible, and the report lists (by UHID,
and the patient's own list) show a patient's reports whole. So a
department-less account opening or printing one report passes when EITHER the
sample's performing facility OR the patient (permissions.patient_in_scope with
facilities=) is in its scope, and is refused only when neither is
(require_in_scope(chart_read=True)). Every laboratory step on the sample keeps
the performing-facility rule above.

Allergies, active medications and problems are never filtered here (MS-R2 (a)).

A REFUSAL BY FACILITY is the same 404 as a row that does not exist, and the
attempt writes an authorization patient_scope_denied row with scope_cause
'facility' (services.facility_lanes.audit_denial, the row
permissions.require_patient_access writes), so a probe across a facility
boundary is always on the record. require_in_scope raises FacilityRefusal
inside the caller's transaction; the service function carries
@audit_refusals, which writes that row on its own connection once the
transaction has rolled back, then raises the service's own not-found error.
A refusal the department causes keeps today's behaviour (the same 404, with
whatever audit the lane wrote before).
"""

from __future__ import annotations

import functools
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from permissions import (
    FACILITY_PATIENT_SQL,
    facility_denial,
    patient_in_scope,
    scope_patient_ids_sql,
)

from services import facility_filter

__all__ = [
    "FacilityRefusal",
    "audit_refusals",
    "copied_facility_on",
    "critical_facility_sql",
    "list_clause",
    "merge_clause",
    "order_specimen_on",
    "performing_facility_on",
    "require_in_scope",
    "result_specimen_on",
    "scope_metadata",
    "scope_of",
    "specimen_facility_on",
    "specimen_facility_sql",
]

MAIN_FACILITY_ID = 1


class FacilityRefusal(Exception):
    """A laboratory row outside the caller's facilities. Raised inside the
    caller's transaction by require_in_scope; @audit_refusals turns it into
    the audit row and the service's own 404 (`not_found`)."""

    def __init__(
        self,
        *,
        patient_id: str | None,
        facility_id: int | None,
        helper: str,
        not_found: Exception,
    ) -> None:
        super().__init__(str(not_found))
        self.patient_id = patient_id
        self.facility_id = facility_id
        self.helper = helper
        self.not_found = not_found


def scope_of(user: Mapping[str, Any] | None) -> frozenset[int] | None:
    """The facilities whose laboratory rows `user` may see, or None for no
    facility limit (one facility on record, a group-wide administrator, a
    service account). BLOCKING on a cache miss: plain-def code only."""
    from permissions import facility_scope

    return facility_scope(user)


def _multi_facility(conn: Any) -> bool:
    from services import facility_service

    return facility_service.multi_facility_data(db=conn)


# ── The performing facility ──────────────────────────────────────────────────


def performing_facility_on(conn: Any, patient_id: str | None) -> int:
    """The facility a new sample for this patient is performed at: 1 while
    one facility is on record (no statement beyond the cached count);
    otherwise the patient's active admission, else latest encounter, else
    registration facility, else 1."""
    from services import facility_identity

    return int(facility_identity.document_facility_id(conn, patient_id=patient_id))


def specimen_facility_on(conn: Any, specimen_id: str | None) -> int | None:
    """The performing facility of one sample, or None when no sample has
    that id. A sample written before migration 74 holds the column's
    default, facility 1."""
    if not specimen_id:
        return None
    row = conn.execute(
        "SELECT facility_id FROM lab_specimens WHERE id = ?", (str(specimen_id),)
    ).fetchone()
    if row is None or row[0] is None:
        return None
    return int(row[0])


def copied_facility_on(conn: Any, specimen_id: str) -> int:
    """The facility a recollection of `specimen_id` is performed at: the
    rejected sample's own (the laboratory that rejected it redraws it); 1
    while one facility is on record, without a statement."""
    if not _multi_facility(conn):
        return MAIN_FACILITY_ID
    found = specimen_facility_on(conn, specimen_id)
    return MAIN_FACILITY_ID if found is None else found


def merge_clause(conn: Any, facility_id: int) -> tuple[str, tuple[int, ...]]:
    """(' AND s.facility_id = ?', (id,)) so a planned tube takes tests only
    from its own facility; ('', ()) while one facility is on record, so the
    merge statement is the one it always was."""
    if not _multi_facility(conn):
        return "", ()
    return " AND s.facility_id = ?", (int(facility_id),)


def result_specimen_on(conn: Any, result_id: Any) -> str | None:
    """The sample a laboratory result was entered on, or None (no such
    result, or an interfaced critical, which names no result)."""
    if result_id is None:
        return None
    row = conn.execute(
        "SELECT specimen_id FROM lab_results WHERE id = ?", (int(result_id),)
    ).fetchone()
    return None if row is None or row[0] is None else str(row[0])


def order_specimen_on(conn: Any, order_id: str | None) -> str | None:
    """The sample of a laboratory order's first test that has one, or None
    (an order not taken in yet). Every sample of one order is planned for
    the same patient at the same time, so they share a facility."""
    if not order_id:
        return None
    row = conn.execute(
        "SELECT specimen_id FROM lab_order_items WHERE order_id = ? AND specimen_id IS NOT NULL "
        "ORDER BY id LIMIT 1",
        (str(order_id),),
    ).fetchone()
    return None if row is None or row[0] is None else str(row[0])


# ── Lists ────────────────────────────────────────────────────────────────────


def specimen_facility_sql(specimen_col: str, patient_col: str) -> str:
    """An SQL expression for the facility a laboratory row belongs to: the
    performing facility of the sample `specimen_col` names, else the
    facility of the patient `patient_col` names (FACILITY_PATIENT_SQL).
    Both arguments are code, never request text."""
    return (
        f"COALESCE((SELECT lf_s.facility_id FROM lab_specimens lf_s "
        f"WHERE lf_s.id = {specimen_col}), "
        f"(SELECT {FACILITY_PATIENT_SQL} FROM patients p WHERE p.id = {patient_col}))"
    )


def critical_facility_sql(result_col: str, patient_col: str) -> str:
    """The facility a critical result belongs to: the performing facility of
    the sample its laboratory result was entered on, else (an interfaced
    critical, which has no sample) the facility of its patient. Arguments are
    code, never request text."""
    return (
        f"COALESCE((SELECT lf_s.facility_id FROM lab_results lf_r "
        f"JOIN lab_specimens lf_s ON lf_s.id = lf_r.specimen_id "
        f"WHERE lf_r.id = {result_col}), "
        f"(SELECT {FACILITY_PATIENT_SQL} FROM patients p WHERE p.id = {patient_col}))"
    )


def list_clause(
    *,
    dept: str | None,
    facilities: Iterable[int] | None,
    patient_col: str,
    facility_sql: str | None = None,
) -> tuple[str, tuple[Any, ...]]:
    """The ' AND ...' a laboratory list adds for the caller's facilities,
    after (and beside) its own department clause, which it leaves alone.

    ('', ()) with no facility limit, so the list's statement is unchanged.
    A department account (or a row with no facility expression) keeps the
    patients whose latest encounter is at one of the facilities; a
    department-less account keeps the rows whose `facility_sql` is one of
    them. The empty scope gives a predicate that is never true."""
    if facilities is None:
        return "", ()
    scope = frozenset(int(f) for f in facilities)
    if dept is not None or facility_sql is None:
        sql, params = scope_patient_ids_sql(None, facilities=scope)
        return f" AND {patient_col} IN ({sql})", tuple(params)
    return facility_filter.column_clause(facility_sql, scope)


def scope_metadata(dept: str | None, facilities: Iterable[int] | None) -> dict[str, Any]:
    """What a laboratory read audit row records about the reader's scope:
    the department rule as before, plus the facility scope when one
    applies (never with one facility on record)."""
    out: dict[str, Any] = {
        "department": dept,
        "hospital_wide": dept is None,
        "scope_source": "authenticated_user",
    }
    if facilities is not None:
        out["facility_scope"] = sorted(int(f) for f in facilities)
    return out


# ── One row ──────────────────────────────────────────────────────────────────


def require_in_scope(
    conn: Any,
    *,
    patient_id: str | None,
    dept: str | None,
    facilities: frozenset[int] | None,
    helper: str,
    not_found: Exception,
    specimen_id: str | None = None,
    facility_not_found: Exception | None = None,
    chart_read: bool = False,
) -> None:
    """Is this laboratory row inside the caller's scope? Raises `not_found`
    when the department refuses it (exactly as patient_in_scope did before),
    and FacilityRefusal carrying `facility_not_found` (else `not_found`) when
    the facility limit does.

    With no facility limit it is patient_in_scope(conn, patient_id, dept).
    Under one, the facility is checked first: a department account by its
    patient's latest encounter (permissions.facility_denial), then its
    department as before; a department-less account by the row's sample's
    performing facility, or its patient's facility when it names no
    sample.

    `chart_read` is for reading a released report, which is part of the
    patient's chart (MS-R2 (a)): a department-less account whose scope does
    not hold the sample's performing facility still passes when the patient
    is in its facilities, and is refused only when neither is. A department
    account is checked by its patient exactly as without it."""
    if facilities is None:
        if not patient_in_scope(conn, patient_id or "", dept):
            raise not_found
        return
    refused = facility_not_found if facility_not_found is not None else not_found
    if dept is None and specimen_id:
        fid = specimen_facility_on(conn, specimen_id)
        if facility_filter.in_scope(fid, facilities):
            return
        if chart_read and patient_in_scope(conn, patient_id or "", None, facilities=facilities):
            return
        raise FacilityRefusal(
            patient_id=patient_id, facility_id=fid, helper=helper, not_found=refused
        )
    denied, fid = facility_denial(conn, patient_id, facilities)
    if denied:
        raise FacilityRefusal(
            patient_id=patient_id, facility_id=fid, helper=helper, not_found=refused
        )
    # The facility passed (or the id names no patient, which the facility
    # check never records as a refusal): the department decides as before,
    # and with no department an unknown patient is still not found.
    if dept is None:
        if not patient_in_scope(conn, patient_id or "", None, facilities=facilities):
            raise not_found
    elif not patient_in_scope(conn, patient_id or "", dept):
        raise not_found


def _write_refusal(
    refusal: FacilityRefusal,
    *,
    actor: Mapping[str, Any] | None,
    dept: str | None,
    facilities: frozenset[int] | None,
    request_meta: Mapping[str, Any] | None,
) -> None:
    from services import facility_lanes

    facility_lanes.audit_denial(
        user=dict(actor or {}),
        patient_id=refusal.patient_id,
        dept=dept,
        facilities=frozenset(facilities or ()),
        facility_id=refusal.facility_id,
        helper=refusal.helper,
        request_meta=dict(request_meta or {}),
    )


def audit_refusals[**P, R](fn: Callable[P, R]) -> Callable[P, R]:
    """For a laboratory service function whose scope check may raise
    FacilityRefusal: once the function's transaction has rolled back, write
    the authorization patient_scope_denied row (fail-soft, on its own
    connection) and raise the function's own not-found error instead. Reads
    actor, dept, facilities and request_meta from the call's keywords, which
    is how every route calls these functions."""

    @functools.wraps(fn)
    def inner(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return fn(*args, **kwargs)
        except FacilityRefusal as refusal:
            scope = kwargs.get("facilities")
            meta = kwargs.get("request_meta")
            actor = kwargs.get("actor")
            dept = kwargs.get("dept")
            _write_refusal(
                refusal,
                actor=actor if isinstance(actor, Mapping) else None,
                dept=dept if isinstance(dept, str) else None,
                facilities=frozenset(scope) if isinstance(scope, frozenset) else frozenset(),
                request_meta=meta if isinstance(meta, Mapping) else None,
            )
            raise refusal.not_found from None

    return inner
