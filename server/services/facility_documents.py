"""Facility scope for money documents and work queues (multisite-9a, W12-E,
wave 12, 20 October 2026).

Orb keeps the facilities of one hospital group apart by row-level facility
ids (ruling MS-R1 (a)). Charts are scoped through the patient: the facility
of the patient's latest encounter (permissions.FACILITY_PATIENT_SQL). Money
and work queues are scoped through the DOCUMENT instead (ruling MS-R5 (a)):
an invoice, a receipt, a refund, a cash counter, a claim or an outpatient
visit belongs to the facility its own facility_id names (migrations 74 and
75), and a facility-bound account works on its own facility's documents.

This module is the one place the money and queue lanes ask that question:

  * facility_clause / patient_facility_clause add the facility predicate to a
    list statement, so a LIMIT can never hide an in-scope row;
  * document_denied answers "is this one document outside the caller's
    facilities?", and audit_document_denial writes the authorization row the
    lanes write before their 404;
  * the *_facility lookups read one document's facility (mostly tables outside
    the multi-site SQL-site registry's list; invoices and payments are read
    through services.facility_stamp, and admission_row_facility is registered
    in the registry's money area).

ONE FACILITY ON RECORD (every install today). The caller's facility scope is
None (permissions.facility_scope answers None before reading anything), so
every clause here is empty, nothing is denied and nothing is audited: the
lanes run exactly the statements they always ran.

A document no row holds is never a facility refusal (the lane then answers
exactly as it did before, as permissions.facility_denial does for a missing
patient). An account whose department no facility owns has the empty scope
and is refused every document.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any

from services.audit_service import log_audit_event

#: Facility 1, the main facility of every install.
MAIN_FACILITY_ID = 1

#: The action every lane writes for a document outside the caller's scope.
DENIAL_ACTION = "facility_document_denied"


def _ids(facilities: frozenset[int]) -> tuple[int, ...]:
    return tuple(sorted(int(f) for f in facilities))


def facility_clause(column: str, facilities: frozenset[int] | None) -> tuple[str, tuple]:
    """(' AND <column> IN (?, ...)', ids) keeping the rows whose own facility is
    one of `facilities`. ('', ()) for None (no facility limit) and ' AND 0'
    for the empty set (no row). `column` is a column name the caller wrote,
    never request text."""
    if facilities is None:
        return "", ()
    ids = _ids(facilities)
    if not ids:
        return " AND 0", ()
    return f" AND {column} IN ({', '.join('?' for _ in ids)})", ids


def facility_condition(column: str, facilities: frozenset[int] | None) -> tuple[str | None, tuple]:
    """facility_clause without the leading ' AND ', for a lane that joins its
    conditions itself: (None, ()) for None, ('0', ()) for the empty set."""
    sql, params = facility_clause(column, facilities)
    return (sql[len(" AND ") :] if sql else None), params


def patient_facility_clause(column: str, facilities: frozenset[int] | None) -> tuple[str, tuple]:
    """(' AND <column> IN (<patients whose latest encounter is at one of
    `facilities`>)', params): the chart rule (permissions.scope_patient_ids_sql
    with no department) for a row that has no facility of its own, such as a
    medication or a Helix supply order. ('', ()) for None."""
    if facilities is None:
        return "", ()
    from permissions import scope_patient_ids_sql

    sql, params = scope_patient_ids_sql(None, facilities=facilities)
    return f" AND {column} IN ({sql})", tuple(params)


def discount_facility_condition(facilities: frozenset[int] | None) -> tuple[str | None, tuple]:
    """The condition keeping discount requests (discount_approvals, which has
    no facility of its own) at one of `facilities`: the bill's facility, else
    the facility of the patient's latest encounter. (None, ()) for None."""
    if facilities is None:
        return None, ()
    ids = _ids(facilities)
    if not ids:
        return "0", ()
    from permissions import FACILITY_PATIENT_SQL

    marks = ", ".join("?" for _ in ids)
    return (
        "COALESCE((SELECT i.facility_id FROM invoices i "
        "WHERE i.id = discount_approvals.invoice_id), "
        f"(SELECT {FACILITY_PATIENT_SQL} FROM patients p "
        f"WHERE p.id = discount_approvals.patient_id)) IN ({marks})",
        ids,
    )


def document_denied(document_facility_id: int | None, facilities: frozenset[int] | None) -> bool:
    """Is a document at `document_facility_id` outside `facilities`?

    Never for None facilities (no limit) or a document no row holds (None).
    Always for the empty set."""
    if facilities is None or document_facility_id is None:
        return False
    if not facilities:
        return True
    return int(document_facility_id) not in facilities


def request_meta(request: Any) -> dict:
    """The request fields an audit row carries, from a Starlette request (or
    {} when there is none)."""
    if request is None:
        return {}
    client = getattr(request, "client", None)
    headers = getattr(request, "headers", None) or {}
    return {
        "request_path": request.url.path,
        "http_method": request.method,
        "ip_address": client.host if client else None,
        "user_agent": headers.get("user-agent"),
    }


def audit_document_denial(
    *,
    user: Mapping,
    lane: str,
    target_type: str,
    target_id: str | int | None,
    document_facility_id: int | None,
    facilities: frozenset[int],
    patient_id: str | None = None,
    meta: Mapping | None = None,
) -> None:
    """The authorization row for a document outside the caller's facilities,
    written before the lane's 404 (fail-soft, like every authorization row).
    BLOCKING. The summary names the document, never the patient."""
    if facilities:
        summary = f"{target_type.replace('_', ' ').capitalize()} {target_id} is outside the caller's facility scope"
    else:
        summary = (
            f"{target_type.replace('_', ' ').capitalize()} {target_id} refused: the caller's "
            "department belongs to no facility on record"
        )
    m = dict(meta or {})
    log_audit_event(
        correlation_id=str(uuid.uuid4()),
        event_category="authorization",
        action_type=DENIAL_ACTION,
        outcome="denied",
        summary_message=summary,
        actor_user_id=(user or {}).get("id"),
        actor_role=(user or {}).get("role"),
        target_type=target_type,
        target_id=None if target_id is None else str(target_id),
        patient_id=patient_id,
        request_path=m.get("request_path"),
        http_method=m.get("http_method"),
        ip_address=m.get("ip_address"),
        user_agent=m.get("user_agent"),
        metadata={
            "lane": lane,
            "scope_source": "authenticated_user",
            "scope_cause": "facility",
            "facility_scope": sorted(int(f) for f in facilities),
            "document_facility_id": document_facility_id,
        },
    )


def deny_if_outside(
    user: Mapping,
    *,
    document_facility_id: int | None,
    lane: str,
    target_type: str,
    target_id: str | int | None,
    patient_id: str | None = None,
    meta: Mapping | None = None,
    facilities: Any = None,
) -> bool:
    """True (after the audit row) when the document is outside the caller's
    facility scope; False otherwise. `facilities` defaults to the caller's
    permissions.facility_scope (pass it when already known). BLOCKING on a
    cache miss: async lanes call it through run_db."""
    if facilities is None:
        from permissions import facility_scope

        facilities = facility_scope(user)
    if not document_denied(document_facility_id, facilities):
        return False
    assert facilities is not None
    audit_document_denial(
        user=user,
        lane=lane,
        target_type=target_type,
        target_id=target_id,
        document_facility_id=document_facility_id,
        facilities=facilities,
        patient_id=patient_id,
        meta=meta,
    )
    return True


# ── One document's facility ──────────────────────────────────────────────────
#
# Each reads facility_id only. A missing row gives None, and the lane then
# answers exactly as it did before (its own not-found).


def _one(db: Any, sql: str, doc_id: Any) -> int | None:
    row = db.execute(sql, (doc_id,)).fetchone()
    if row is None or row[0] is None:
        return None
    return int(row[0])


def refund_facility(db: Any, refund_id: int) -> int | None:
    """The facility a refund request belongs to (migration 75)."""
    return _one(db, "SELECT facility_id FROM refunds WHERE id = ?", int(refund_id))


def counter_facility(db: Any, session_id: int) -> int | None:
    """The facility a cash counter session belongs to (migration 75)."""
    return _one(db, "SELECT facility_id FROM cash_counter_sessions WHERE id = ?", int(session_id))


def claim_facility(db: Any, claim_id: int) -> int | None:
    """The facility an insurance claim belongs to (migration 75)."""
    return _one(db, "SELECT facility_id FROM claims WHERE id = ?", int(claim_id))


def charge_facility(db: Any, charge_id: int) -> int | None:
    """The facility a captured charge line belongs to (migration 75)."""
    return _one(db, "SELECT facility_id FROM encounter_charges WHERE id = ?", int(charge_id))


def admission_ref(value: Any) -> str | None:
    """An admission id named in a request body, as text, or None when the body
    names none (absent, null or blank)."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def admission_row_facility(db: Any, admission_id: Any) -> int | None:
    """The facility of the admission this id names, whichever patient it
    belongs to (migration 70's admissions.facility_id), or None when no
    admission has the id.

    Not bound to the patient on purpose: the money services stamp a receipt,
    refund, claim or charge with the named admission's facility
    (facility_identity.document_facility_id) and not every one of them first
    checks whose admission it is, so the gate reads the same row the stamp
    would. Another patient's admission at the caller's own facility passes
    here and is the service's own refusal, as before."""
    ref = admission_ref(admission_id)
    if ref is None:
        return None
    return _one(db, "SELECT facility_id FROM admissions WHERE id = ?", ref)


def policy_facility(db: Any, policy_id: int) -> int | None:
    """The facility an insurance policy on file belongs to: its admission's,
    else its patient's latest encounter's (patient_payer_policies has no
    facility of its own)."""
    from services import facility_stamp

    row = db.execute(
        "SELECT patient_id, admission_id FROM patient_payer_policies WHERE id = ?",
        (int(policy_id),),
    ).fetchone()
    if row is None or not row["patient_id"]:
        return None
    return facility_stamp.patient_facility(
        db, patient_id=row["patient_id"], admission_id=row["admission_id"]
    )


def discount_facility(db: Any, discount_id: int) -> int | None:
    """The facility a discount request belongs to: its bill's, else its
    patient's (discount_approvals has no facility of its own)."""
    from services import facility_stamp

    row = db.execute(
        "SELECT invoice_id, patient_id FROM discount_approvals WHERE id = ?",
        (int(discount_id),),
    ).fetchone()
    if row is None:
        return None
    if row["invoice_id"] is not None:
        fid = facility_stamp.invoice_facility(db, int(row["invoice_id"]))
        if fid is not None:
            return fid
    if row["patient_id"]:
        return facility_stamp.patient_facility(db, patient_id=row["patient_id"])
    return None
