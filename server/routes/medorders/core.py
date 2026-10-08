"""Medication orders, record-mode reads and the core writes: endpoints under
/api/medorders (medorders-4, W5-E).

The package router in routes/medorders/__init__.py adds the /api/medorders
prefix and the ORB_MED_ORDER_STORE gate; this router has neither, so every
endpoint here inherits both (404 feature_disabled while the store is off). The
writes that need the lifecycle carry require_med_order_lifecycle on their own
route (this router stays gate-free, which tests/test_medorder_registries.py
pins). Every handler is a plain def.

Endpoints:
  GET  /patient/{patient_id}       patient.read_all through require_patient_access
                                   (audit med_orders_viewed): the patient's
                                   orders, plus unadopted chart rows shown
                                   through core.legacy_status_for (a GET never
                                   adopts)
  GET  /orders/{id}, /orders/{id}/events
                                   patient.read_all; an order outside the
                                   caller's department is an audited 404
                                   (patient_scope_denied first), then a
                                   phi_access read row
  POST /orders                     create: a draft (order.write or
                                   medication.write) or created and signed
                                   ('prescribed', medication.prescribe);
                                   Idempotency-Key; lifecycle
  POST /orders/{id}/sign           medication.prescribe, expected_version,
                                   safety_ack; the order needs a dose, a
                                   route and a frequency (422
                                   dose_route_and_frequency_required, also
                                   on create as prescribed); lifecycle
  POST /orders/{id}/cancel         a draft, by the person who entered it or
                                   any order.write holder in scope, with a
                                   reason; a signed order is not cancelled
                                   here until ruling MO-R2's step is built;
                                   lifecycle
  GET  /queue/to-sign              medication.prescribe; unsigned drafts in the
                                   caller's department, oldest first, and the
                                   'Entered by a nurse' visibility list
  GET  /admin/parity               admin.overview; the read-only parity report
  POST /admin/adopt                admin.overview; adoption of unlinked chart
                                   rows, the one record-mode write (no
                                   lifecycle gate)

WHY /orders/{id} AND NOT /{id}. A bare /{id} GET would catch every one-segment
path a later module (or the registry test's probe route) adds under this
prefix, and the package cannot carry an empty create path (FastAPI refuses an
empty path under an empty include prefix). The later verify and lifecycle
steps can sit beside these as /orders/{id}/verify, /orders/{id}/hold and so on.

After a write commits, 'medorders:update' goes to the patient's department
room and dept:__all__ through realtime.schedule_patient_vitals, carrying ids,
status and version only: never a drug name.
"""
from __future__ import annotations

import logging
import uuid
from typing import Literal, Mapping, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from database import db_session
from permissions import (
    ADMIN_OVERVIEW,
    MEDICATION_PRESCRIBE,
    MEDICATION_WRITE,
    ORDER_WRITE,
    PATIENT_READ_ALL,
    ROLE_PERMISSIONS,
    department_scope,
    log_phi_read,
    patient_in_scope,
    require_any_permission,
    require_patient_access,
    require_permission,
    scope_patient_ids_sql,
)
from routes.medorders import require_med_order_lifecycle
from services.audit_service import log_audit_event
from services.medorders import adoption, chart_lane, queries
from services.medorders import core as store

logger = logging.getLogger("orb.medorders.routes")

router = APIRouter()

#: The socket event a medication-order change pushes (ids, status, version).
MEDORDERS_UPDATE_EVENT = "medorders:update"

ORDER_NOT_FOUND = "Medication order not found"
PATIENT_NOT_FOUND = "Patient not found"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _request_meta(request: Request) -> dict:
    return {
        "request_path": request.url.path,
        "http_method": request.method,
        "ip_address": request.client.host if request.client else None,
        "user_agent": request.headers.get("user-agent"),
    }


def _holds(user: Mapping, permission: str) -> bool:
    return permission in ROLE_PERMISSIONS.get(user.get("role", ""), set())


def _audit_denied(user: Mapping, request: Request, *, action_type: str, summary: str,
                  metadata: dict, patient_id: Optional[str] = None,
                  target_type: Optional[str] = None, target_id: Optional[str] = None) -> None:
    log_audit_event(
        correlation_id=str(uuid.uuid4()),
        event_category="authorization",
        action_type=action_type,
        outcome="denied",
        summary_message=summary,
        actor_user_id=user.get("id"),
        actor_role=user.get("role"),
        patient_id=patient_id,
        target_type=target_type,
        target_id=target_id,
        request_path=request.url.path,
        http_method=request.method,
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
        metadata=metadata,
    )


def _deny_missing(user: Mapping, request: Request, permission: str) -> HTTPException:
    """The require_permission refusal, for a permission only some bodies need
    (creating as prescribed): audited, then the same 403 body."""
    _audit_denied(user, request, action_type="permission_denied",
                  summary=f"Missing permissions: {permission}",
                  metadata={"required_permissions": [permission],
                            "missing_permissions": [permission],
                            "user_role": user.get("role")})
    return HTTPException(status_code=403, detail={
        "error": "permission_denied", "message": "Insufficient permissions",
        "required": [permission], "missing": [permission]})


def _refusal(err: store.MedOrderError, user: Mapping, request: Request) -> HTTPException:
    """A store refusal as HTTP: its own status and body (core.http_detail). A
    403 from the store (not the creator of a draft, not a prescriber) is
    audited like any other denial."""
    if isinstance(err, store.NotPermitted):
        order = err.order or {}
        _audit_denied(user, request, action_type="med_order_step_refused",
                      summary=f"Medication order step refused: {err.code}",
                      metadata={"code": err.code, "user_role": user.get("role")},
                      patient_id=order.get("patient_id"), target_type="med_order",
                      target_id=str(order["id"]) if order.get("id") else None)
    return HTTPException(status_code=err.http_status, detail=store.http_detail(err))


def _scope_denied(user: Mapping, request: Request, *, patient_id: str, target_type: str,
                  target_id: str, not_found: str) -> HTTPException:
    """The out-of-department refusal: audited first, then the same 404 an
    unknown id gets, so the answer never confirms the order exists."""
    dept = department_scope(user)
    _audit_denied(user, request, action_type="patient_scope_denied",
                  summary=(f"{target_type} {target_id} belongs to a patient outside the "
                           f"caller's department ({dept})"),
                  metadata={"department": dept, "scope_source": "authenticated_user"},
                  patient_id=patient_id, target_type=target_type, target_id=target_id)
    return HTTPException(status_code=404, detail=not_found)


def _med_order_patient_in_scope_or_404(order_id: str, user: Mapping, request: Request) -> str:
    """The order's patient, when the caller may see it. BLOCKING. Modelled on
    emar_routes._enforce_medication_scope: 404 rather than 403, and a cross-ward
    attempt is audited before the refusal. Returns the patient id."""
    dept = department_scope(user)
    with db_session() as db:
        row = queries.order_scope_row(db, order_id)
        if row is None:
            raise HTTPException(status_code=404, detail=ORDER_NOT_FOUND)
        if dept is None or patient_in_scope(db, row["patient_id"], dept):
            return row["patient_id"]
    raise _scope_denied(user, request, patient_id=row["patient_id"], target_type="med_order",
                        target_id=order_id, not_found=ORDER_NOT_FOUND)


def _patient_in_scope_or_404(patient_id: str, user: Mapping, request: Request) -> None:
    """The same rule for a patient named in a BODY (permissions.enforce_patient_scope
    writes no audit row, so it is not used here)."""
    dept = department_scope(user)
    with db_session() as db:
        if db.execute("SELECT 1 FROM patients WHERE id = ?", (patient_id,)).fetchone() is None:
            raise HTTPException(status_code=404, detail=PATIENT_NOT_FOUND)
        if dept is None or patient_in_scope(db, patient_id, dept):
            return
    raise _scope_denied(user, request, patient_id=patient_id, target_type="patient",
                        target_id=patient_id, not_found=PATIENT_NOT_FOUND)


def update_payload(order: Mapping) -> dict:
    """The 'medorders:update' body: ids, status and version. Never a drug
    name, a dose or a reason: the department room is wider than the chart."""
    return {"patient_id": order.get("patient_id"), "med_order_id": order.get("id"),
            "status": order.get("status"), "version": order.get("version")}


def _emit(order: Mapping) -> None:
    """After commit. Fire and forget: a missed push shows on the next load."""
    try:
        from ws.realtime import schedule_patient_vitals
        schedule_patient_vitals(MEDORDERS_UPDATE_EVENT, update_payload(order))
    except Exception as e:  # noqa: BLE001  (the write has committed; never fail it)
        logger.warning("medorders:update for %s not pushed: %s", order.get("id"), e)


def _step_reply(result: Mapping) -> dict:
    return {
        "order": queries.serialize_order(result["order"]),
        "medication_id": result.get("medication_id"),
        "safety": result.get("safety"),
        "duplicates": result.get("duplicates") or [],
        "replayed": bool(result.get("replayed")),
    }


# ── Bodies ────────────────────────────────────────────────────────────────────

class CreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    patient_id: str
    drug_name: str
    #: 'draft' (reaches nothing until a prescriber signs it) or 'prescribed'
    #: (created and signed in one step: medication.prescribe).
    status: Literal["draft", "prescribed"] = "draft"
    dose: Optional[str] = None
    route: Optional[str] = None
    frequency: Optional[str] = None
    is_prn: bool = False
    prn_indication: Optional[str] = None
    priority: Optional[Literal["routine", "urgent", "stat"]] = None
    indication: Optional[str] = None
    notes: Optional[str] = None
    duration_text: Optional[str] = None
    #: {acknowledged: true, reason: '...'} after a 428 from the safety gate.
    safety_ack: Optional[dict] = None


class SignRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int
    safety_ack: Optional[dict] = None


class CancelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str
    expected_version: Optional[int] = None


class AdoptRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: Limit adoption to these patients (one ward first, say); omitted adopts
    #: every unlinked chart row.
    patient_ids: Optional[list[str]] = None


# ── Queues and administration (literal paths first) ───────────────────────────

@router.get("/queue/to-sign")
def to_sign_queue(request: Request,
                  user: dict = Depends(require_permission(MEDICATION_PRESCRIBE))):
    """Unsigned drafts in the caller's department, oldest first (a draft
    waiting over 24 hours is marked, ruling MO-R10 (a)), and the live orders a
    nurse charted, listed for visibility only (held ruling R010)."""
    scope_sql, scope_params = scope_patient_ids_sql(department_scope(user))
    with db_session() as db:
        queue = queries.to_sign_queue(db, scope_sql, scope_params)
    items = queue["drafts"] + queue["entered_by_a_nurse"]
    if items:
        log_phi_read(
            request=request, user=user, action_type="med_orders_to_sign_viewed",
            summary_message=f"Medication orders to sign viewed ({len(items)})",
            target_type="med_order",
            metadata={"med_order_ids": [i["id"] for i in items],
                      "patient_ids": sorted({i["patient_id"] for i in items})},
        )
    return queue


@router.get("/admin/parity")
def parity_report(request: Request, user: dict = Depends(require_permission(ADMIN_OVERVIEW))):
    """Where the store and the ward disagree, and what is not adopted yet.
    Read-only; ids only, no drug names. Audited like the parity CLI."""
    report = adoption.parity_report()
    log_phi_read(
        request=request, user=user, action_type="med_order_parity_report",
        summary_message="Medication order parity report viewed",
        target_type="med_orders",
        metadata={"clean": report["clean"], "orders": report["orders"],
                  "events": report["events"]},
    )
    return report


@router.post("/admin/adopt")
def adopt(request: Request, body: Optional[AdoptRequest] = None,
          user: dict = Depends(require_permission(ADMIN_OVERVIEW))):
    """Adopt unlinked chart rows into the store (the one record-mode write, so
    no lifecycle gate). Idempotent; one clinical_access audit row per batch."""
    try:
        return adoption.adopt_legacy(
            trigger="admin", actor_id=user["id"], actor_role=user.get("role"),
            patient_ids=body.patient_ids if body else None,
            request_meta=_request_meta(request))
    except adoption.AdoptionRefused as e:
        raise HTTPException(status_code=404, detail={
            "error": "feature_disabled", "feature": "med_order_store", "message": str(e)})


# ── Reads ─────────────────────────────────────────────────────────────────────

@router.get("/patient/{patient_id}")
def list_patient_orders(
    patient_id: str,
    user: dict = Depends(require_patient_access(PATIENT_READ_ALL,
                                                audit_action="med_orders_viewed")),
):
    """The patient's orders, newest first, and the chart rows not adopted yet
    (each shown as the order it would become, marked legacy, with no id)."""
    with db_session() as db:
        if db.execute("SELECT 1 FROM patients WHERE id = ?", (patient_id,)).fetchone() is None:
            raise HTTPException(status_code=404, detail=PATIENT_NOT_FOUND)
        return queries.list_for_patient(db, patient_id)


@router.get("/orders/{order_id}")
def get_order(order_id: str, request: Request,
              user: dict = Depends(require_permission(PATIENT_READ_ALL))):
    oid = order_id
    patient_id = _med_order_patient_in_scope_or_404(oid, user, request)
    with db_session() as db:
        order = store.get_order_in_txn(db, oid)
    if order is None:
        raise HTTPException(status_code=404, detail=ORDER_NOT_FOUND)
    log_phi_read(request=request, user=user, action_type="med_order_viewed",
                 summary_message=f"Medication order {oid} viewed", patient_id=patient_id,
                 target_type="med_order", target_id=oid)
    return {"order": queries.serialize_order(order)}


@router.get("/orders/{order_id}/events")
def get_order_events(order_id: str, request: Request,
                     user: dict = Depends(require_permission(PATIENT_READ_ALL))):
    """The order's ledger, oldest first."""
    oid = order_id
    patient_id = _med_order_patient_in_scope_or_404(oid, user, request)
    with db_session() as db:
        events = store.events_in_txn(db, oid)
    log_phi_read(request=request, user=user, action_type="med_order_events_viewed",
                 summary_message=f"Medication order {oid} history viewed",
                 patient_id=patient_id, target_type="med_order", target_id=oid)
    return {"med_order_id": oid, "events": [queries.serialize_event(e) for e in events]}


# ── Writes (lifecycle) ────────────────────────────────────────────────────────

@router.post("/orders", dependencies=[Depends(require_med_order_lifecycle)])
def create_order(
    body: CreateRequest,
    request: Request,
    user: dict = Depends(require_any_permission(ORDER_WRITE, MEDICATION_WRITE)),
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
):
    """Create a draft, or create and sign in one step. A draft needs
    order.write or medication.write; 'prescribed' needs medication.prescribe
    and is screened now (428 to acknowledge, 409 screen_stale when an allergy
    or a drug was charted while it was being screened)."""
    if body.status == "prescribed" and not _holds(user, MEDICATION_PRESCRIBE):
        raise _deny_missing(user, request, MEDICATION_PRESCRIBE)
    _patient_in_scope_or_404(body.patient_id, user, request)
    try:
        result = chart_lane.create_order(
            patient_id=body.patient_id, drug_name=body.drug_name, status=body.status,
            actor_id=user["id"], actor_role=user.get("role"), dose=body.dose,
            route=body.route, frequency=body.frequency, is_prn=body.is_prn,
            prn_indication=body.prn_indication, priority=body.priority,
            indication=body.indication, notes=body.notes, duration_text=body.duration_text,
            idempotency_key=idempotency_key, safety_ack=body.safety_ack,
            request_meta=_request_meta(request))
    except store.MedOrderError as e:
        raise _refusal(e, user, request)
    if not result["replayed"]:
        _emit(result["order"])
    return _step_reply(result)


@router.post("/orders/{order_id}/sign", dependencies=[Depends(require_med_order_lifecycle)])
def sign_order(order_id: str, body: SignRequest, request: Request,
               user: dict = Depends(require_permission(MEDICATION_PRESCRIBE))):
    """The prescriber's signature: the draft becomes prescribed and reaches the
    ward. 428 on an alert to acknowledge (a severe allergy clears only for a
    prescriber), 409 stale_version or screen_stale."""
    oid = order_id
    _med_order_patient_in_scope_or_404(oid, user, request)
    try:
        result = chart_lane.sign_order(
            oid, actor_id=user["id"], actor_role=user.get("role"),
            expected_version=body.expected_version, safety_ack=body.safety_ack,
            request_meta=_request_meta(request))
    except store.MedOrderError as e:
        raise _refusal(e, user, request)
    _emit(result["order"])
    return _step_reply(result)


@router.post("/orders/{order_id}/cancel", dependencies=[Depends(require_med_order_lifecycle)])
def cancel_order(order_id: str, body: CancelRequest, request: Request,
                 user: dict = Depends(require_any_permission(
                     ORDER_WRITE, MEDICATION_WRITE, MEDICATION_PRESCRIBE))):
    """Cancel an unsigned order, with a reason: its creator, or anyone in scope
    who may write orders. A signed order is refused (403 for a non-prescriber,
    409 cancel_not_offered for a prescriber until ruling MO-R2's step is
    built)."""
    oid = order_id
    _med_order_patient_in_scope_or_404(oid, user, request)
    try:
        result = chart_lane.cancel_draft(
            oid, actor_id=user["id"], actor_role=user.get("role"),
            expected_version=body.expected_version, reason=body.reason,
            may_cancel_any_draft=_holds(user, ORDER_WRITE),
            may_prescribe=_holds(user, MEDICATION_PRESCRIBE),
            request_meta=_request_meta(request))
    except store.MedOrderError as e:
        raise _refusal(e, user, request)
    _emit(result["order"])
    return {"order": queries.serialize_order(result["order"])}
