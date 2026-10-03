"""Orb — Biomedical asset & equipment tracking API.

The equipment register, its preventive-maintenance and CALIBRATION schedules,
the breakdown/downtime log, and AMC/CMC contracts.

════════════════════════════════════════════════════════════════════════════════
 FOUR THINGS THIS ROUTER IS CAREFUL ABOUT
════════════════════════════════════════════════════════════════════════════════

1. IT IS NOT A CLINICAL PATH, AND IT MUST NEVER BECOME A GATE. Nothing here
   writes to medications, vitals, alerts, orders, admissions or shadow capture,
   and nothing here may block a clinical action. An overdue calibration is a
   loud warning on a screen. It does not stop a nurse using the pump — see the
   module docstring in services/biomedical_service.py for the reasoning and for
   the product question that leaves open.

2. THE WARD VIEW IS A DIFFERENT PERMISSION FROM THE REGISTER, ON PURPOSE.
   `GET /api/biomedical/wards/{ward}/attention` is readable by anyone who can
   read vitals — doctors, nurses, admins — because the whole point of this
   module is that the ward using the machine finds out its calibration has
   lapsed. Locking that behind an administrative permission would leave the
   warning where only the people who already knew could see it. The payload
   carries NO PHI: asset tags, ward names, dates and counts only.

   The rest of the module (the register, contracts, costs) needs `helix.read`,
   and every write needs `helix.write`, with the three acts that change what the
   hospital has committed to — condemning an asset, and creating or amending a
   contract — needing `admin.overview` on top.

3. IT IS BEHIND A MODULE SWITCH. `config.BIOMEDICAL_ENABLED` (env
   ORB_BIOMEDICAL, DEFAULT ON). Off → every route 404s with a body that says
   why, so a stale client gets an explanation rather than a mystery.

4. PERMISSION DEBT, STATED PLAINLY. Orb has no `biomedical.*` permission and no
   biomedical-engineer role. `permissions.py` is outside this module's scope, so
   these routes borrow `helix.read` / `helix.write` (admin + pharmacist) and
   `admin.overview` (admin only). Two consequences, both wrong for a real
   deployment and both recorded as follow-ups: a PHARMACIST can edit the
   ventilator register, and a hospital cannot give its biomedical engineer
   access without making them a pharmacist or an admin. The ward view is the one
   piece correctly scoped today, because `vitals.read` genuinely is "people who
   look at numbers these machines produce".

All handlers are plain `def`, so FastAPI runs them in a threadpool: everything
below opens SQLite, and blocking the event loop is the failure mode this repo
has shipped three production freezes from.
"""
from __future__ import annotations

import logging
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

import config
from permissions import (
    ADMIN_OVERVIEW,
    HELIX_READ,
    HELIX_WRITE,
    VITALS_READ,
    require_permission,
)
from services import biomedical_service as svc
from services.audit_service import log_audit_event

logger = logging.getLogger("orb.biomedical_routes")

router = APIRouter(prefix="/api/biomedical", tags=["Biomedical Equipment"])


# ── Module switch ────────────────────────────────────────────────────────────

def require_module() -> None:
    """404 when the site has switched biomedical tracking off.

    404 rather than 403: the module is not disallowed for this user, it is not
    present at this deployment. The body says so, because a client built before
    the flag flipped deserves a sentence rather than a blank screen."""
    if not config.BIOMEDICAL_ENABLED:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "module_disabled",
                "module": "biomedical",
                "message": (
                    "The biomedical equipment module is switched off for this "
                    "deployment (ORB_BIOMEDICAL=0). This site keeps its "
                    "equipment and calibration register elsewhere."
                ),
            },
        )


def _fail(exc: svc.BiomedicalError) -> HTTPException:
    """Map a service error to HTTP, carrying the machine-readable `code` through
    so a client can branch on it without matching English."""
    status = {
        "not_found": 404,
        "unknown_asset": 404,
        "duplicate_call_number": 409,
        "already_exists": 409,
        "already_closed": 409,
        "call_closed": 409,
        "use_close": 409,
        "missing_due_basis": 422,
        "future_date": 422,
        "bad_date_order": 422,
    }.get(exc.code, 400)
    return HTTPException(status_code=status,
                         detail={"error": exc.code, "message": str(exc)})


def _audit(request: Request, user: dict, action: str, *, summary: str,
           target_type: str, target_id: Optional[str] = None,
           metadata: Optional[dict] = None) -> None:
    """Every biomedical WRITE leaves a trail.

    Category is `security` (fail-closed, awaited) rather than a clinical
    category — an equipment record is an administrative act, and mixing it into
    the clinical categories would dilute the trail a clinical investigation
    reads. `patient_id` is never passed: there is no patient link anywhere in
    this module and an audit row is not the place to invent one.
    """
    log_audit_event(
        correlation_id=str(uuid.uuid4()),
        event_category="security",
        action_type=action,
        outcome="success",
        summary_message=summary,
        actor_user_id=user.get("id"),
        actor_role=user.get("role"),
        target_type=target_type,
        target_id=str(target_id) if target_id is not None else None,
        request_path=request.url.path,
        http_method=request.method,
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
        metadata=metadata or {},
    )


# ═════════════════════════════════════════════════════════════════════════════
#  Request bodies
# ═════════════════════════════════════════════════════════════════════════════

class AssetBody(BaseModel):
    asset_tag: str = Field(..., min_length=1, max_length=64)
    name: str = Field(..., min_length=1, max_length=200)
    category: str = "other"
    manufacturer: Optional[str] = None
    model: Optional[str] = None
    serial_number: Optional[str] = None
    department: Optional[str] = None
    ward: Optional[str] = None
    location_detail: Optional[str] = None
    criticality: str = "routine"
    status: str = "in_service"
    purchase_date: Optional[str] = None
    commissioned_date: Optional[str] = None
    purchase_cost_paise: Optional[int] = None
    warranty_expiry_date: Optional[str] = None
    vendor_id: Optional[int] = None
    produces_clinical_readings: bool = False
    notes: Optional[str] = None
    is_active: bool = True
    #: The Add form sets this: a tag already on the register is refused (409)
    #: rather than overwritten.
    create_only: bool = False


class AssetStatusBody(BaseModel):
    status: str
    reason: Optional[str] = None


class ScheduleBody(BaseModel):
    asset_id: Optional[int] = None
    asset_tag: Optional[str] = None
    schedule_type: str
    interval_days: int = Field(..., gt=0)
    last_done_date: Optional[str] = None
    first_due_date: Optional[str] = None
    grace_days: int = 0
    lead_days: int = svc.DEFAULT_LEAD_DAYS
    responsible: Optional[str] = None
    vendor_id: Optional[int] = None
    certificate_reference: Optional[str] = None
    is_active: bool = True


class CompletionBody(BaseModel):
    schedule_id: Optional[int] = None
    asset_id: Optional[int] = None
    asset_tag: Optional[str] = None
    schedule_type: Optional[str] = None
    performed_date: Optional[str] = None
    result: str = "pass"
    certificate_reference: Optional[str] = None
    performed_by: Optional[str] = None
    vendor_id: Optional[int] = None
    cost_paise: Optional[int] = None
    notes: Optional[str] = None


class ServiceCallBody(BaseModel):
    asset_id: Optional[int] = None
    asset_tag: Optional[str] = None
    fault_description: str = Field(..., min_length=1)
    severity: str = "breakdown"
    reported_at: Optional[str] = None
    reported_by: Optional[str] = None
    ward: Optional[str] = None
    took_out_of_service: bool = True
    down_from: Optional[str] = None
    call_number: Optional[str] = None
    vendor_id: Optional[int] = None


class ServiceCallUpdateBody(BaseModel):
    status: Optional[str] = None
    resolution: Optional[str] = None
    attended_by: Optional[str] = None
    vendor_id: Optional[int] = None
    contract_id: Optional[int] = None


class ServiceCallCloseBody(BaseModel):
    restored_at: Optional[str] = None
    resolution: Optional[str] = None
    attended_by: Optional[str] = None
    vendor_id: Optional[int] = None
    contract_id: Optional[int] = None
    cost_paise: Optional[int] = None
    return_to_service: bool = True


class ContractBody(BaseModel):
    contract_number: str = Field(..., min_length=1, max_length=80)
    contract_type: str = "amc"
    start_date: str
    end_date: str
    vendor_id: Optional[int] = None
    vendor_name: Optional[str] = None
    value_paise: Optional[int] = None
    notice_days: int = svc.DEFAULT_NOTICE_DAYS
    covers_calibration: bool = False
    covers_parts: bool = False
    response_hours: Optional[int] = None
    status: str = "active"
    scope_notes: Optional[str] = None
    asset_ids: Optional[list[int]] = None
    asset_tags: Optional[list[str]] = None


# ═════════════════════════════════════════════════════════════════════════════
#  Meta
# ═════════════════════════════════════════════════════════════════════════════

@router.get("/meta")
def meta(_: None = Depends(require_module),
         user: dict = Depends(require_permission(HELIX_READ))) -> dict:
    """The module's vocabulary, so the panel never hard-codes a status list that
    can drift from the CHECK constraints behind it."""
    return {
        "asset_categories": list(svc.ASSET_CATEGORIES),
        "criticalities": list(svc.CRITICALITIES),
        "asset_statuses": list(svc.ASSET_STATUSES),
        "unavailable_statuses": sorted(svc.UNAVAILABLE_STATUSES),
        "schedule_types": list(svc.SCHEDULE_TYPES),
        "schedule_type_labels": dict(svc.SCHEDULE_TYPE_LABELS),
        "schedule_results": list(svc.SCHEDULE_RESULTS),
        "service_call_statuses": list(svc.SERVICE_CALL_STATUSES),
        "open_call_statuses": sorted(svc.OPEN_CALL_STATUSES),
        "service_call_severities": list(svc.SERVICE_CALL_SEVERITIES),
        "contract_types": list(svc.CONTRACT_TYPES),
        "contract_statuses": list(svc.CONTRACT_STATUSES),
        "default_lead_days": svc.DEFAULT_LEAD_DAYS,
        "default_notice_days": svc.DEFAULT_NOTICE_DAYS,
        # The boundary rules, published so a client cannot invent its own.
        "due_rule": (
            "A schedule is OVERDUE when today is ON OR AFTER its next due date "
            "(plus any grace days). The due date is the first day NOT covered by "
            "the last certificate."
        ),
        "contract_rule": (
            "A contract is EXPIRED when today is AFTER its end date. The end date "
            "is the last day the contract covers."
        ),
    }


# ═════════════════════════════════════════════════════════════════════════════
#  Overview and the overdue register
# ═════════════════════════════════════════════════════════════════════════════

@router.get("/overview")
def overview(_: None = Depends(require_module),
             user: dict = Depends(require_permission(HELIX_READ))) -> dict:
    return svc.biomedical_overview()


@router.get("/due")
def due_register(
    schedule_type: Optional[str] = Query(None),
    ward: Optional[str] = Query(None),
    only_overdue: bool = Query(False),
    _: None = Depends(require_module),
    user: dict = Depends(require_permission(HELIX_READ)),
) -> dict:
    """THE OVERDUE VIEW — what is out of calibration or maintenance right now."""
    try:
        return svc.due_register(schedule_type=schedule_type, ward=ward,
                                only_overdue=only_overdue)
    except svc.BiomedicalError as exc:
        raise _fail(exc) from exc


@router.get("/coverage-gaps")
def coverage_gaps(_: None = Depends(require_module),
                  user: dict = Depends(require_permission(HELIX_READ))) -> dict:
    """Active assets with no maintenance schedule at all — the register's own
    blind spot, reported rather than hidden."""
    rows = svc.coverage_gaps()
    return {"assets": rows, "count": len(rows)}


# ═════════════════════════════════════════════════════════════════════════════
#  The ward view — the clinically-scoped slice
# ═════════════════════════════════════════════════════════════════════════════

@router.get("/wards/{ward}/attention")
def ward_attention(
    ward: str,
    _: None = Depends(require_module),
    user: dict = Depends(require_permission(VITALS_READ)),
) -> dict:
    """What a ward needs to know about its own equipment.

    `vitals.read`, NOT `helix.read`: the ward that uses the monitor is the ward
    that has to know its calibration has lapsed, and every clinical role holds
    vitals.read while none of them hold helix.read. There is NO PHI in this
    response — asset tags, ward names, dates and counts — so it needs no patient
    scoping and `enforce_patient_scope` does not apply (no patient_id is
    accepted here, on the query string or anywhere else).
    """
    try:
        return svc.ward_attention(ward)
    except svc.BiomedicalError as exc:
        raise _fail(exc) from exc


# ═════════════════════════════════════════════════════════════════════════════
#  Assets
# ═════════════════════════════════════════════════════════════════════════════

@router.get("/assets")
def list_assets(
    ward: Optional[str] = Query(None),
    department: Optional[str] = Query(None),
    category: Optional[str] = Query(None),
    criticality: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    search: Optional[str] = Query(None, max_length=100),
    include_inactive: bool = Query(False),
    limit: int = Query(200, ge=1, le=1000),
    _: None = Depends(require_module),
    user: dict = Depends(require_permission(HELIX_READ)),
) -> dict:
    try:
        rows = svc.list_assets(
            ward=ward, department=department, category=category,
            criticality=criticality, status=status, search=search,
            include_inactive=include_inactive, limit=limit)
    except svc.BiomedicalError as exc:
        raise _fail(exc) from exc
    return {"assets": rows, "count": len(rows)}


@router.get("/assets/{asset_id}")
def get_asset(asset_id: int, _: None = Depends(require_module),
              user: dict = Depends(require_permission(HELIX_READ))) -> dict:
    asset = svc.get_asset(asset_id)
    if not asset:
        raise HTTPException(status_code=404,
                            detail={"error": "unknown_asset",
                                    "message": f"no asset {asset_id}"})
    return asset


@router.post("/assets")
def upsert_asset(body: AssetBody, request: Request,
                 _: None = Depends(require_module),
                 user: dict = Depends(require_permission(HELIX_WRITE))) -> dict:
    try:
        asset = svc.upsert_asset(**body.model_dump(), actor=user.get("id"))
    except svc.BiomedicalError as exc:
        raise _fail(exc) from exc
    _audit(request, user, "biomedical_asset_saved",
           summary=f"Equipment record saved: {asset.get('asset_tag')} ({asset.get('name')})",
           target_type="biomedical_asset", target_id=asset.get("id"),
           metadata={"asset_tag": asset.get("asset_tag"),
                     "criticality": asset.get("criticality"),
                     "ward": asset.get("ward")})
    return asset


@router.post("/assets/{asset_id}/status")
def set_asset_status(asset_id: int, body: AssetStatusBody, request: Request,
                     _: None = Depends(require_module),
                     user: dict = Depends(require_permission(HELIX_WRITE))) -> dict:
    """Move an asset between statuses.

    CONDEMNING needs `admin.overview` on top of `helix.write`: it retires a
    capital asset and drops it out of the register a ward can see, which is a
    management decision rather than a workshop one. The extra check is done
    inline rather than as a second dependency because it applies to ONE value of
    one field.
    """
    if (body.status or "").strip().lower() == "condemned":
        from permissions import ROLE_PERMISSIONS
        if ADMIN_OVERVIEW not in ROLE_PERMISSIONS.get(user.get("role", ""), set()):
            raise HTTPException(
                status_code=403,
                detail={"error": "condemn_requires_admin",
                        "message": ("Condemning an asset retires it from the "
                                    "register and needs an administrator.")})
    try:
        asset = svc.set_asset_status(asset_id=asset_id, status=body.status,
                                     reason=body.reason, actor=user.get("id"))
    except svc.BiomedicalError as exc:
        raise _fail(exc) from exc
    _audit(request, user, "biomedical_asset_status_changed",
           summary=f"Equipment {asset.get('asset_tag')} set to {body.status}",
           target_type="biomedical_asset", target_id=asset_id,
           metadata={"status": body.status, "reason": body.reason})
    return asset


# ═════════════════════════════════════════════════════════════════════════════
#  Schedules
# ═════════════════════════════════════════════════════════════════════════════

@router.get("/schedules")
def list_schedules(
    asset_id: Optional[int] = Query(None),
    schedule_type: Optional[str] = Query(None),
    ward: Optional[str] = Query(None),
    _: None = Depends(require_module),
    user: dict = Depends(require_permission(HELIX_READ)),
) -> dict:
    try:
        rows = svc.list_schedules(asset_id=asset_id, schedule_type=schedule_type,
                                  ward=ward)
    except svc.BiomedicalError as exc:
        raise _fail(exc) from exc
    return {"schedules": rows, "count": len(rows)}


@router.post("/schedules")
def upsert_schedule(body: ScheduleBody, request: Request,
                    _: None = Depends(require_module),
                    user: dict = Depends(require_permission(HELIX_WRITE))) -> dict:
    try:
        sched = svc.upsert_schedule(**body.model_dump(), actor=user.get("id"))
    except svc.BiomedicalError as exc:
        raise _fail(exc) from exc
    _audit(request, user, "biomedical_schedule_saved",
           summary=(f"{sched.get('schedule_type_label')} schedule saved for asset "
                    f"{sched.get('asset_id')}, next due {sched.get('next_due_date')}"),
           target_type="biomedical_schedule", target_id=sched.get("id"),
           metadata={"schedule_type": sched.get("schedule_type"),
                     "interval_days": sched.get("interval_days"),
                     "next_due_date": sched.get("next_due_date")})
    return sched


@router.post("/schedules/complete")
def record_completion(body: CompletionBody, request: Request,
                      _: None = Depends(require_module),
                      user: dict = Depends(require_permission(HELIX_WRITE))) -> dict:
    """Record that a PM or calibration was done and roll the schedule forward.

    A `result` of `fail` does NOT roll it forward — nothing was certified — and
    the asset stays in the overdue register. See the service docstring.
    """
    try:
        sched = svc.record_schedule_completion(**body.model_dump(), actor=user.get("id"))
    except svc.BiomedicalError as exc:
        raise _fail(exc) from exc
    _audit(request, user, "biomedical_maintenance_recorded",
           summary=(f"{sched.get('schedule_type_label')} recorded for asset "
                    f"{sched.get('asset_id')} (result {sched.get('recorded_result')}); "
                    f"next due {sched.get('next_due_date')}"),
           target_type="biomedical_schedule", target_id=sched.get("id"),
           metadata={"result": sched.get("recorded_result"),
                     "was_overdue": sched.get("recorded_was_overdue"),
                     "days_overdue": sched.get("recorded_days_overdue"),
                     "certificate_reference": body.certificate_reference})
    return sched


# ═════════════════════════════════════════════════════════════════════════════
#  Service calls and downtime
# ═════════════════════════════════════════════════════════════════════════════

@router.get("/service-calls")
def list_service_calls(
    asset_id: Optional[int] = Query(None),
    ward: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    open_only: bool = Query(False),
    limit: int = Query(100, ge=1, le=500),
    _: None = Depends(require_module),
    user: dict = Depends(require_permission(HELIX_READ)),
) -> dict:
    try:
        rows = svc.list_service_calls(asset_id=asset_id, ward=ward, status=status,
                                      open_only=open_only, limit=limit)
    except svc.BiomedicalError as exc:
        raise _fail(exc) from exc
    return {"service_calls": rows, "count": len(rows)}


@router.post("/service-calls")
def open_service_call(body: ServiceCallBody, request: Request,
                      _: None = Depends(require_module),
                      user: dict = Depends(require_permission(HELIX_WRITE))) -> dict:
    try:
        call = svc.open_service_call(**body.model_dump(), actor=user.get("id"))
    except svc.BiomedicalError as exc:
        raise _fail(exc) from exc
    _audit(request, user, "biomedical_service_call_opened",
           summary=(f"Service call opened on asset {call.get('asset_id')}: "
                    f"{call.get('fault_description')}"),
           target_type="biomedical_service_call", target_id=call.get("id"),
           metadata={"severity": call.get("severity"),
                     "took_out_of_service": call.get("took_out_of_service")})
    return call


@router.post("/service-calls/{call_id}")
def update_service_call(call_id: int, body: ServiceCallUpdateBody, request: Request,
                        _: None = Depends(require_module),
                        user: dict = Depends(require_permission(HELIX_WRITE))) -> dict:
    try:
        call = svc.update_service_call(call_id=call_id, **body.model_dump(),
                                       actor=user.get("id"))
    except svc.BiomedicalError as exc:
        raise _fail(exc) from exc
    _audit(request, user, "biomedical_service_call_updated",
           summary=f"Service call {call_id} updated to {call.get('status')}",
           target_type="biomedical_service_call", target_id=call_id,
           metadata={"status": call.get("status")})
    return call


@router.post("/service-calls/{call_id}/close")
def close_service_call(call_id: int, body: ServiceCallCloseBody, request: Request,
                       _: None = Depends(require_module),
                       user: dict = Depends(require_permission(HELIX_WRITE))) -> dict:
    try:
        call = svc.close_service_call(call_id=call_id, **body.model_dump(),
                                      actor=user.get("id"))
    except svc.BiomedicalError as exc:
        raise _fail(exc) from exc
    _audit(request, user, "biomedical_service_call_closed",
           summary=(f"Service call {call_id} closed; "
                    f"{call.get('downtime_minutes')} minutes downtime"),
           target_type="biomedical_service_call", target_id=call_id,
           metadata={"downtime_minutes": call.get("downtime_minutes")})
    return call


@router.get("/downtime")
def downtime(
    ward: Optional[str] = Query(None),
    days: int = Query(30, ge=1, le=730),
    limit: int = Query(25, ge=1, le=200),
    _: None = Depends(require_module),
    user: dict = Depends(require_permission(HELIX_READ)),
) -> dict:
    """Downtime per asset over a window, accumulated across every service call."""
    from datetime import timedelta

    from services.hospital_time import hospital_today

    today = hospital_today()
    rows = svc.downtime_summary(since=today - timedelta(days=days), limit=limit)
    return {
        "as_of": today.isoformat(), "window_days": days,
        "assets": rows, "count": len(rows),
        "total_minutes": sum(r["downtime_minutes"] for r in rows),
    }


# ═════════════════════════════════════════════════════════════════════════════
#  Contracts
# ═════════════════════════════════════════════════════════════════════════════

@router.get("/contracts")
def list_contracts(
    status: Optional[str] = Query(None),
    contract_type: Optional[str] = Query(None),
    _: None = Depends(require_module),
    user: dict = Depends(require_permission(HELIX_READ)),
) -> dict:
    try:
        rows = svc.list_contracts(status=status, contract_type=contract_type)
    except svc.BiomedicalError as exc:
        raise _fail(exc) from exc
    return {"contracts": rows, "count": len(rows)}


@router.get("/contracts/expiring")
def expiring_contracts(
    within_days: Optional[int] = Query(None, ge=0, le=1095),
    _: None = Depends(require_module),
    user: dict = Depends(require_permission(HELIX_READ)),
) -> dict:
    """Contracts inside their notice window, plus the ones already expired."""
    try:
        return svc.expiring_contracts(within_days=within_days)
    except svc.BiomedicalError as exc:
        raise _fail(exc) from exc


@router.get("/contracts/{contract_id}")
def get_contract(contract_id: int, _: None = Depends(require_module),
                 user: dict = Depends(require_permission(HELIX_READ))) -> dict:
    contract = svc.get_contract(contract_id)
    if not contract:
        raise HTTPException(status_code=404,
                            detail={"error": "not_found",
                                    "message": f"no contract {contract_id}"})
    return contract


@router.post("/contracts")
def upsert_contract(body: ContractBody, request: Request,
                    _: None = Depends(require_module),
                    user: dict = Depends(require_permission(ADMIN_OVERVIEW))) -> dict:
    """Create or amend an AMC/CMC. `admin.overview` because a maintenance
    contract is money the hospital has committed, not a workshop record."""
    try:
        contract = svc.upsert_contract(**body.model_dump(), actor=user.get("id"))
    except svc.BiomedicalError as exc:
        raise _fail(exc) from exc
    _audit(request, user, "biomedical_contract_saved",
           summary=(f"{contract.get('contract_type', '').upper()} "
                    f"{contract.get('contract_number')} saved, ends "
                    f"{contract.get('end_date')}, covering "
                    f"{contract.get('asset_count')} asset(s)"),
           target_type="biomedical_contract", target_id=contract.get("id"),
           metadata={"contract_number": contract.get("contract_number"),
                     "end_date": contract.get("end_date"),
                     "asset_count": contract.get("asset_count")})
    return contract
