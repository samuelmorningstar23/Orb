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

ONE FACILITY AT A TIME (multisite-9b). A machine is registered at one facility
(biomedical_assets.facility_id, migration 75). With more than one facility of a
group on the appliance an engineer or a ward works with its own facility's
machines: every list, count and ward view keeps to the caller's facilities'
assets (and the schedules, calls and downtime of those assets), a machine,
schedule or call of another facility answers 404 exactly like one that does not
exist, and a new machine placed on another facility's department or ward is
refused (403 facility_scope). A maintenance contract is shown to a facility's
reader only when it covers one of that facility's machines, with only those
machines listed and counted. A facility's account changes a contract only when
it covers no other facility's machine (403 facility_scope otherwise, since its
dates, status and coverage list are shared by every machine on it), and a
contract it makes or amends must cover at least one of its own machines. With
one facility on record the caller's facility scope is None and none of this
runs.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterable
from typing import Any

import config
from database import db_session
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from permissions import (
    ADMIN_OVERVIEW,
    HELIX_READ,
    HELIX_WRITE,
    VITALS_READ,
    facility_scope,
    require_permission,
)
from pydantic import BaseModel, Field
from services.audit_service import log_audit_event

from services import biomedical_service as svc
from services import facility_stamp

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
    return HTTPException(status_code=status, detail={"error": exc.code, "message": str(exc)})


# ── Facilities (multisite-9b; see the module docstring) ─────────────────────

#: Fetched for a facility-limited reader before a list's own limit is applied,
#: so the list is cut after the facility filter, never before it.
_SCOPED_FETCH = 1000


def _asset_ids(facilities: frozenset[int]) -> set[int]:
    """The ids of the machines registered at `facilities`."""
    ids = sorted(int(f) for f in facilities)
    if not ids:
        return set()
    marks = ", ".join("?" for _ in ids)
    with db_session() as db:
        rows = db.execute(
            f"SELECT id FROM biomedical_assets WHERE facility_id IN ({marks})", ids
        ).fetchall()
    return {int(r[0]) for r in rows}


def _one_id(sql: str, params: tuple) -> int | None:
    with db_session() as db:
        row = db.execute(sql, params).fetchone()
    return None if row is None or row[0] is None else int(row[0])


def _unknown_asset(what: str) -> HTTPException:
    return HTTPException(
        status_code=404, detail={"error": "unknown_asset", "message": f"no {what}"}
    )


def _require_asset(
    facilities: frozenset[int] | None,
    *,
    asset_id: int | None = None,
    asset_tag: str | None = None,
    must_exist: bool = True,
) -> None:
    """For a facility-limited caller, 404 when the machine named is at another
    facility (or, with must_exist, not on the register at all)."""
    if facilities is None or (asset_id is None and not asset_tag):
        return
    found = (
        _one_id("SELECT id FROM biomedical_assets WHERE id = ?", (int(asset_id),))
        if asset_id is not None
        else _one_id(
            "SELECT id FROM biomedical_assets WHERE asset_tag = ?",
            (str(asset_tag).strip().upper(),),
        )
    )
    if found is None:
        if must_exist:
            raise _unknown_asset(f"asset {asset_id if asset_id is not None else asset_tag}")
        return
    if found not in _asset_ids(facilities):
        raise _unknown_asset(f"asset {asset_id if asset_id is not None else asset_tag}")


def _require_call(facilities: frozenset[int] | None, call_id: int) -> None:
    """For a facility-limited caller, 404 for a service call on another
    facility's machine (or one that does not exist)."""
    if facilities is None:
        return
    owner = _one_id("SELECT asset_id FROM biomedical_service_calls WHERE id = ?", (int(call_id),))
    if owner is None or owner not in _asset_ids(facilities):
        raise HTTPException(
            status_code=404, detail={"error": "not_found", "message": f"no service call {call_id}"}
        )


def _keep_assets(rows: Iterable[dict], ids: set[int], key: str = "id") -> list[dict]:
    return [r for r in rows if r.get(key) is not None and int(r[key]) in ids]


def _scoped_due(due: dict, ids: set[int]) -> dict:
    """A due register kept to these machines, its counts made again the way
    services/biomedical_service.due_register makes them."""
    overdue = _keep_assets(due.get("overdue") or [], ids, "asset_id")
    due_soon = _keep_assets(due.get("due_soon") or [], ids, "asset_id")
    return {
        **due,
        "overdue": overdue,
        "overdue_count": len(overdue),
        "due_soon": due_soon,
        "due_soon_count": len(due_soon),
        "overdue_calibration_count": sum(
            1 for r in overdue if r.get("schedule_type") == "calibration"
        ),
        "overdue_life_support_count": sum(
            1 for r in overdue if r.get("asset_criticality") == "life_support"
        ),
        "never_done_count": sum(1 for r in overdue if r.get("never_done")),
        "measurement_at_risk_count": sum(1 for r in overdue if r.get("measurement_at_risk")),
    }


def _contract_assets(contract_id: int) -> set[int]:
    with db_session() as db:
        rows = db.execute(
            "SELECT asset_id FROM biomedical_contract_assets WHERE contract_id = ?",
            (int(contract_id),),
        ).fetchall()
    return {int(r[0]) for r in rows if r[0] is not None}


def _contract_visible(contract_id: Any, ids: set[int]) -> bool:
    return contract_id is not None and bool(_contract_assets(int(contract_id)) & ids)


def _contracts_seen(rows: Iterable[dict], ids: set[int]) -> list[dict]:
    """The contracts that cover one of these machines, each counting only them."""
    seen: list[dict] = []
    for contract in rows:
        cid = contract.get("id")
        mine = _contract_assets(int(cid)) & ids if cid is not None else set()
        if mine:
            seen.append({**contract, "asset_count": len(mine)})
    return seen


def _refuse_shared_contract(contract_number: str) -> HTTPException:
    return HTTPException(
        status_code=403,
        detail={
            "error": "facility_scope",
            "message": (
                f"Contract {contract_number} also covers another facility's machines, so "
                "only an account that works across the group can change it. Nothing was saved."
            ),
        },
    )


def _refuse_elsewhere(
    facilities: frozenset[int] | None, *, department: str | None, ward: str | None
) -> None:
    """403 facility_scope when a new machine is placed on another facility's
    department or ward."""
    if facilities is None:
        return
    with db_session() as db:
        named = (
            (
                "department",
                facility_stamp.department_facility(db, department) if department else None,
            ),
            ("ward", facility_stamp.ward_facility(db, ward=ward) if ward else None),
        )
    for label, fid in named:
        if fid is not None and fid not in facilities:
            raise HTTPException(
                status_code=403,
                detail={
                    "error": "facility_scope",
                    "message": f"That {label} is at another facility, so nothing was saved.",
                },
            )


def _audit(
    request: Request,
    user: dict,
    action: str,
    *,
    summary: str,
    target_type: str,
    target_id: str | int | None = None,
    metadata: dict | None = None,
) -> None:
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
    manufacturer: str | None = None
    model: str | None = None
    serial_number: str | None = None
    department: str | None = None
    ward: str | None = None
    location_detail: str | None = None
    criticality: str = "routine"
    status: str = "in_service"
    purchase_date: str | None = None
    commissioned_date: str | None = None
    purchase_cost_paise: int | None = None
    warranty_expiry_date: str | None = None
    vendor_id: int | None = None
    produces_clinical_readings: bool = False
    notes: str | None = None
    is_active: bool = True
    #: The Add form sets this: a tag already on the register is refused (409)
    #: rather than overwritten.
    create_only: bool = False


class AssetStatusBody(BaseModel):
    status: str
    reason: str | None = None


class ScheduleBody(BaseModel):
    asset_id: int | None = None
    asset_tag: str | None = None
    schedule_type: str
    interval_days: int = Field(..., gt=0)
    last_done_date: str | None = None
    first_due_date: str | None = None
    grace_days: int = 0
    lead_days: int = svc.DEFAULT_LEAD_DAYS
    responsible: str | None = None
    vendor_id: int | None = None
    certificate_reference: str | None = None
    is_active: bool = True


class CompletionBody(BaseModel):
    schedule_id: int | None = None
    asset_id: int | None = None
    asset_tag: str | None = None
    schedule_type: str | None = None
    performed_date: str | None = None
    result: str = "pass"
    certificate_reference: str | None = None
    performed_by: str | None = None
    vendor_id: int | None = None
    cost_paise: int | None = None
    notes: str | None = None


class ServiceCallBody(BaseModel):
    asset_id: int | None = None
    asset_tag: str | None = None
    fault_description: str = Field(..., min_length=1)
    severity: str = "breakdown"
    reported_at: str | None = None
    reported_by: str | None = None
    ward: str | None = None
    took_out_of_service: bool = True
    down_from: str | None = None
    call_number: str | None = None
    vendor_id: int | None = None


class ServiceCallUpdateBody(BaseModel):
    status: str | None = None
    resolution: str | None = None
    attended_by: str | None = None
    vendor_id: int | None = None
    contract_id: int | None = None


class ServiceCallCloseBody(BaseModel):
    restored_at: str | None = None
    resolution: str | None = None
    attended_by: str | None = None
    vendor_id: int | None = None
    contract_id: int | None = None
    cost_paise: int | None = None
    return_to_service: bool = True


class ContractBody(BaseModel):
    contract_number: str = Field(..., min_length=1, max_length=80)
    contract_type: str = "amc"
    start_date: str
    end_date: str
    vendor_id: int | None = None
    vendor_name: str | None = None
    value_paise: int | None = None
    notice_days: int = svc.DEFAULT_NOTICE_DAYS
    covers_calibration: bool = False
    covers_parts: bool = False
    response_hours: int | None = None
    status: str = "active"
    scope_notes: str | None = None
    asset_ids: list[int] | None = None
    asset_tags: list[str] | None = None


# ═════════════════════════════════════════════════════════════════════════════
#  Meta
# ═════════════════════════════════════════════════════════════════════════════


_META__DEFAULT = Depends(require_module)
_META_USER_DEFAULT = Depends(require_permission(HELIX_READ))


@router.get("/meta")
def meta(_: None = _META__DEFAULT, user: dict = _META_USER_DEFAULT) -> dict:
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


_OVERVIEW__DEFAULT = Depends(require_module)
_OVERVIEW_USER_DEFAULT = Depends(require_permission(HELIX_READ))


@router.get("/overview")
def overview(_: None = _OVERVIEW__DEFAULT, user: dict = _OVERVIEW_USER_DEFAULT) -> dict:
    facilities = facility_scope(user)
    out = svc.biomedical_overview()
    if facilities is None:
        return out
    # The caller's facilities' machines only: every list kept to them and
    # every count made again from what is kept.
    ids = _asset_ids(facilities)
    due = _scoped_due(svc.due_register(), ids)
    assets = [
        a
        for a in svc.list_assets(limit=_SCOPED_FETCH)
        if a.get("id") is not None and int(a["id"]) in ids
    ]
    calls = _keep_assets(svc.list_service_calls(open_only=True, limit=500), ids, "asset_id")
    gaps = _keep_assets(svc.coverage_gaps(), ids)
    contracts = svc.expiring_contracts()
    expiring = _contracts_seen(contracts.get("expiring") or [], ids)
    expired = _contracts_seen(contracts.get("expired") or [], ids)
    out.update(
        {
            "assets_total": len(assets),
            "assets_down": sum(1 for a in assets if a.get("status") == "down"),
            "assets_under_maintenance": sum(
                1 for a in assets if a.get("status") == "under_maintenance"
            ),
            "life_support_assets": sum(1 for a in assets if a.get("criticality") == "life_support"),
            "measuring_assets": sum(1 for a in assets if a.get("produces_clinical_readings")),
            "overdue": due["overdue"][:25],
            "overdue_count": due["overdue_count"],
            "overdue_calibration_count": due["overdue_calibration_count"],
            "overdue_life_support_count": due["overdue_life_support_count"],
            "measurement_at_risk_count": due["measurement_at_risk_count"],
            "never_done_count": due["never_done_count"],
            "due_soon": due["due_soon"][:25],
            "due_soon_count": due["due_soon_count"],
            "open_calls": calls[:25],
            "open_call_count": len(calls),
            "contracts_expiring": expiring[:25],
            "contracts_expiring_count": len(expiring),
            "contracts_expired_count": len(expired),
            "assets_with_no_schedule": gaps[:25],
            "assets_with_no_schedule_count": len(gaps),
            "downtime_top": _keep_assets(out.get("downtime_top") or [], ids, "asset_id"),
        }
    )
    return out


_DUE_REGISTER_SCHEDULE_TYPE_DEFAULT = Query(None)
_DUE_REGISTER_WARD_DEFAULT = Query(None)
_DUE_REGISTER_ONLY_OVERDUE_DEFAULT = Query(False)
_DUE_REGISTER__DEFAULT = Depends(require_module)
_DUE_REGISTER_USER_DEFAULT = Depends(require_permission(HELIX_READ))


@router.get("/due")
def due_register(
    schedule_type: str | None = _DUE_REGISTER_SCHEDULE_TYPE_DEFAULT,
    ward: str | None = _DUE_REGISTER_WARD_DEFAULT,
    only_overdue: bool = _DUE_REGISTER_ONLY_OVERDUE_DEFAULT,
    _: None = _DUE_REGISTER__DEFAULT,
    user: dict = _DUE_REGISTER_USER_DEFAULT,
) -> dict:
    """THE OVERDUE VIEW — what is out of calibration or maintenance right now."""
    facilities = facility_scope(user)
    try:
        due = svc.due_register(schedule_type=schedule_type, ward=ward, only_overdue=only_overdue)
    except svc.BiomedicalError as exc:
        raise _fail(exc) from exc
    return due if facilities is None else _scoped_due(due, _asset_ids(facilities))


_COVERAGE_GAPS__DEFAULT = Depends(require_module)
_COVERAGE_GAPS_USER_DEFAULT = Depends(require_permission(HELIX_READ))


@router.get("/coverage-gaps")
def coverage_gaps(
    _: None = _COVERAGE_GAPS__DEFAULT, user: dict = _COVERAGE_GAPS_USER_DEFAULT
) -> dict:
    """Active assets with no maintenance schedule at all — the register's own
    blind spot, reported rather than hidden."""
    facilities = facility_scope(user)
    rows = svc.coverage_gaps()
    if facilities is not None:
        rows = _keep_assets(rows, _asset_ids(facilities))
    return {"assets": rows, "count": len(rows)}


# ═════════════════════════════════════════════════════════════════════════════
#  The ward view — the clinically-scoped slice
# ═════════════════════════════════════════════════════════════════════════════


_WARD_ATTENTION__DEFAULT = Depends(require_module)
_WARD_ATTENTION_USER_DEFAULT = Depends(require_permission(VITALS_READ))


@router.get("/wards/{ward}/attention")
def ward_attention(
    ward: str,
    _: None = _WARD_ATTENTION__DEFAULT,
    user: dict = _WARD_ATTENTION_USER_DEFAULT,
) -> dict:
    """What a ward needs to know about its own equipment.

    `vitals.read`, NOT `helix.read`: the ward that uses the monitor is the ward
    that has to know its calibration has lapsed, and every clinical role holds
    vitals.read while none of them hold helix.read. There is NO PHI in this
    response — asset tags, ward names, dates and counts — so it needs no patient
    scoping and `enforce_patient_scope` does not apply (no patient_id is
    accepted here, on the query string or anywhere else). With more than one
    facility on record only the caller's facilities' machines on the ward.
    """
    facilities = facility_scope(user)
    try:
        out = svc.ward_attention(ward)
    except svc.BiomedicalError as exc:
        raise _fail(exc) from exc
    if facilities is None:
        return out
    ids = _asset_ids(facilities)
    due = _scoped_due(out, ids)
    unavailable = _keep_assets(out.get("unavailable") or [], ids)
    calls = _keep_assets(out.get("open_calls") or [], ids, "asset_id")
    mine = [a for a in svc.list_assets(ward=ward, limit=_SCOPED_FETCH) if a.get("id") in ids]
    due.update(
        {
            "asset_count": len(mine),
            "unavailable": unavailable,
            "unavailable_count": len(unavailable),
            "open_calls": calls,
            "open_call_count": len(calls),
            "headline": svc._ward_headline(due, unavailable),
        }
    )
    return due


# ═════════════════════════════════════════════════════════════════════════════
#  Assets
# ═════════════════════════════════════════════════════════════════════════════


_LIST_ASSETS_WARD_DEFAULT = Query(None)
_LIST_ASSETS_DEPARTMENT_DEFAULT = Query(None)
_LIST_ASSETS_CATEGORY_DEFAULT = Query(None)
_LIST_ASSETS_CRITICALITY_DEFAULT = Query(None)
_LIST_ASSETS_STATUS_DEFAULT = Query(None)
_LIST_ASSETS_SEARCH_DEFAULT = Query(None, max_length=100)
_LIST_ASSETS_INCLUDE_INACTIVE_DEFAULT = Query(False)
_LIST_ASSETS_LIMIT_DEFAULT = Query(200, ge=1, le=1000)
_LIST_ASSETS__DEFAULT = Depends(require_module)
_LIST_ASSETS_USER_DEFAULT = Depends(require_permission(HELIX_READ))


@router.get("/assets")
def list_assets(
    ward: str | None = _LIST_ASSETS_WARD_DEFAULT,
    department: str | None = _LIST_ASSETS_DEPARTMENT_DEFAULT,
    category: str | None = _LIST_ASSETS_CATEGORY_DEFAULT,
    criticality: str | None = _LIST_ASSETS_CRITICALITY_DEFAULT,
    status: str | None = _LIST_ASSETS_STATUS_DEFAULT,
    search: str | None = _LIST_ASSETS_SEARCH_DEFAULT,
    include_inactive: bool = _LIST_ASSETS_INCLUDE_INACTIVE_DEFAULT,
    limit: int = _LIST_ASSETS_LIMIT_DEFAULT,
    _: None = _LIST_ASSETS__DEFAULT,
    user: dict = _LIST_ASSETS_USER_DEFAULT,
) -> dict:
    facilities = facility_scope(user)
    try:
        rows = svc.list_assets(
            ward=ward,
            department=department,
            category=category,
            criticality=criticality,
            status=status,
            search=search,
            include_inactive=include_inactive,
            limit=limit if facilities is None else _SCOPED_FETCH,
        )
    except svc.BiomedicalError as exc:
        raise _fail(exc) from exc
    if facilities is not None:
        rows = _keep_assets(rows, _asset_ids(facilities))[:limit]
    return {"assets": rows, "count": len(rows)}


_GET_ASSET__DEFAULT = Depends(require_module)
_GET_ASSET_USER_DEFAULT = Depends(require_permission(HELIX_READ))


@router.get("/assets/{asset_id}")
def get_asset(
    asset_id: int,
    _: None = _GET_ASSET__DEFAULT,
    user: dict = _GET_ASSET_USER_DEFAULT,
) -> dict:
    asset = svc.get_asset(asset_id)
    facilities = facility_scope(user)
    if not asset or (facilities is not None and int(asset_id) not in _asset_ids(facilities)):
        raise HTTPException(
            status_code=404, detail={"error": "unknown_asset", "message": f"no asset {asset_id}"}
        )
    return asset


_UPSERT_ASSET__DEFAULT = Depends(require_module)
_UPSERT_ASSET_USER_DEFAULT = Depends(require_permission(HELIX_WRITE))


@router.post("/assets")
def upsert_asset(
    body: AssetBody,
    request: Request,
    _: None = _UPSERT_ASSET__DEFAULT,
    user: dict = _UPSERT_ASSET_USER_DEFAULT,
) -> dict:
    facilities = facility_scope(user)
    _require_asset(facilities, asset_tag=body.asset_tag, must_exist=False)
    _refuse_elsewhere(facilities, department=body.department, ward=body.ward)
    try:
        asset = svc.upsert_asset(**body.model_dump(), actor=user.get("id"))
    except svc.BiomedicalError as exc:
        raise _fail(exc) from exc
    _audit(
        request,
        user,
        "biomedical_asset_saved",
        summary=f"Equipment record saved: {asset.get('asset_tag')} ({asset.get('name')})",
        target_type="biomedical_asset",
        target_id=asset.get("id"),
        metadata={
            "asset_tag": asset.get("asset_tag"),
            "criticality": asset.get("criticality"),
            "ward": asset.get("ward"),
        },
    )
    return asset


_SET_ASSET_STATUS__DEFAULT = Depends(require_module)
_SET_ASSET_STATUS_USER_DEFAULT = Depends(require_permission(HELIX_WRITE))


@router.post("/assets/{asset_id}/status")
def set_asset_status(
    asset_id: int,
    body: AssetStatusBody,
    request: Request,
    _: None = _SET_ASSET_STATUS__DEFAULT,
    user: dict = _SET_ASSET_STATUS_USER_DEFAULT,
) -> dict:
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
                detail={
                    "error": "condemn_requires_admin",
                    "message": (
                        "Condemning an asset retires it from the "
                        "register and needs an administrator."
                    ),
                },
            )
    _require_asset(facility_scope(user), asset_id=asset_id)
    try:
        asset = svc.set_asset_status(
            asset_id=asset_id, status=body.status, reason=body.reason, actor=user.get("id")
        )
    except svc.BiomedicalError as exc:
        raise _fail(exc) from exc
    _audit(
        request,
        user,
        "biomedical_asset_status_changed",
        summary=f"Equipment {asset.get('asset_tag')} set to {body.status}",
        target_type="biomedical_asset",
        target_id=asset_id,
        metadata={"status": body.status, "reason": body.reason},
    )
    return asset


# ═════════════════════════════════════════════════════════════════════════════
#  Schedules
# ═════════════════════════════════════════════════════════════════════════════


_LIST_SCHEDULES_ASSET_ID_DEFAULT = Query(None)
_LIST_SCHEDULES_SCHEDULE_TYPE_DEFAULT = Query(None)
_LIST_SCHEDULES_WARD_DEFAULT = Query(None)
_LIST_SCHEDULES__DEFAULT = Depends(require_module)
_LIST_SCHEDULES_USER_DEFAULT = Depends(require_permission(HELIX_READ))


@router.get("/schedules")
def list_schedules(
    asset_id: int | None = _LIST_SCHEDULES_ASSET_ID_DEFAULT,
    schedule_type: str | None = _LIST_SCHEDULES_SCHEDULE_TYPE_DEFAULT,
    ward: str | None = _LIST_SCHEDULES_WARD_DEFAULT,
    _: None = _LIST_SCHEDULES__DEFAULT,
    user: dict = _LIST_SCHEDULES_USER_DEFAULT,
) -> dict:
    facilities = facility_scope(user)
    try:
        rows = svc.list_schedules(asset_id=asset_id, schedule_type=schedule_type, ward=ward)
    except svc.BiomedicalError as exc:
        raise _fail(exc) from exc
    if facilities is not None:
        rows = _keep_assets(rows, _asset_ids(facilities), "asset_id")
    return {"schedules": rows, "count": len(rows)}


_UPSERT_SCHEDULE__DEFAULT = Depends(require_module)
_UPSERT_SCHEDULE_USER_DEFAULT = Depends(require_permission(HELIX_WRITE))


@router.post("/schedules")
def upsert_schedule(
    body: ScheduleBody,
    request: Request,
    _: None = _UPSERT_SCHEDULE__DEFAULT,
    user: dict = _UPSERT_SCHEDULE_USER_DEFAULT,
) -> dict:
    _require_asset(facility_scope(user), asset_id=body.asset_id, asset_tag=body.asset_tag)
    try:
        sched = svc.upsert_schedule(**body.model_dump(), actor=user.get("id"))
    except svc.BiomedicalError as exc:
        raise _fail(exc) from exc
    _audit(
        request,
        user,
        "biomedical_schedule_saved",
        summary=(
            f"{sched.get('schedule_type_label')} schedule saved for asset "
            f"{sched.get('asset_id')}, next due {sched.get('next_due_date')}"
        ),
        target_type="biomedical_schedule",
        target_id=sched.get("id"),
        metadata={
            "schedule_type": sched.get("schedule_type"),
            "interval_days": sched.get("interval_days"),
            "next_due_date": sched.get("next_due_date"),
        },
    )
    return sched


_RECORD_COMPLETION__DEFAULT = Depends(require_module)
_RECORD_COMPLETION_USER_DEFAULT = Depends(require_permission(HELIX_WRITE))


@router.post("/schedules/complete")
def record_completion(
    body: CompletionBody,
    request: Request,
    _: None = _RECORD_COMPLETION__DEFAULT,
    user: dict = _RECORD_COMPLETION_USER_DEFAULT,
) -> dict:
    """Record that a PM or calibration was done and roll the schedule forward.

    A `result` of `fail` does NOT roll it forward — nothing was certified — and
    the asset stays in the overdue register. See the service docstring.
    """
    facilities = facility_scope(user)
    if facilities is not None and body.schedule_id is not None:
        owner = _one_id(
            "SELECT asset_id FROM biomedical_schedules WHERE id = ?", (int(body.schedule_id),)
        )
        if owner is None or owner not in _asset_ids(facilities):
            raise HTTPException(
                status_code=404,
                detail={"error": "not_found", "message": f"no schedule {body.schedule_id}"},
            )
    _require_asset(facilities, asset_id=body.asset_id, asset_tag=body.asset_tag)
    try:
        sched = svc.record_schedule_completion(**body.model_dump(), actor=user.get("id"))
    except svc.BiomedicalError as exc:
        raise _fail(exc) from exc
    _audit(
        request,
        user,
        "biomedical_maintenance_recorded",
        summary=(
            f"{sched.get('schedule_type_label')} recorded for asset "
            f"{sched.get('asset_id')} (result {sched.get('recorded_result')}); "
            f"next due {sched.get('next_due_date')}"
        ),
        target_type="biomedical_schedule",
        target_id=sched.get("id"),
        metadata={
            "result": sched.get("recorded_result"),
            "was_overdue": sched.get("recorded_was_overdue"),
            "days_overdue": sched.get("recorded_days_overdue"),
            "certificate_reference": body.certificate_reference,
        },
    )
    return sched


# ═════════════════════════════════════════════════════════════════════════════
#  Service calls and downtime
# ═════════════════════════════════════════════════════════════════════════════


_LIST_SERVICE_CALLS_ASSET_ID_DEFAULT = Query(None)
_LIST_SERVICE_CALLS_WARD_DEFAULT = Query(None)
_LIST_SERVICE_CALLS_STATUS_DEFAULT = Query(None)
_LIST_SERVICE_CALLS_OPEN_ONLY_DEFAULT = Query(False)
_LIST_SERVICE_CALLS_LIMIT_DEFAULT = Query(100, ge=1, le=500)
_LIST_SERVICE_CALLS__DEFAULT = Depends(require_module)
_LIST_SERVICE_CALLS_USER_DEFAULT = Depends(require_permission(HELIX_READ))


@router.get("/service-calls")
def list_service_calls(
    asset_id: int | None = _LIST_SERVICE_CALLS_ASSET_ID_DEFAULT,
    ward: str | None = _LIST_SERVICE_CALLS_WARD_DEFAULT,
    status: str | None = _LIST_SERVICE_CALLS_STATUS_DEFAULT,
    open_only: bool = _LIST_SERVICE_CALLS_OPEN_ONLY_DEFAULT,
    limit: int = _LIST_SERVICE_CALLS_LIMIT_DEFAULT,
    _: None = _LIST_SERVICE_CALLS__DEFAULT,
    user: dict = _LIST_SERVICE_CALLS_USER_DEFAULT,
) -> dict:
    facilities = facility_scope(user)
    try:
        rows = svc.list_service_calls(
            asset_id=asset_id,
            ward=ward,
            status=status,
            open_only=open_only,
            limit=limit if facilities is None else 500,
        )
    except svc.BiomedicalError as exc:
        raise _fail(exc) from exc
    if facilities is not None:
        rows = _keep_assets(rows, _asset_ids(facilities), "asset_id")[:limit]
    return {"service_calls": rows, "count": len(rows)}


_OPEN_SERVICE_CALL__DEFAULT = Depends(require_module)
_OPEN_SERVICE_CALL_USER_DEFAULT = Depends(require_permission(HELIX_WRITE))


@router.post("/service-calls")
def open_service_call(
    body: ServiceCallBody,
    request: Request,
    _: None = _OPEN_SERVICE_CALL__DEFAULT,
    user: dict = _OPEN_SERVICE_CALL_USER_DEFAULT,
) -> dict:
    _require_asset(facility_scope(user), asset_id=body.asset_id, asset_tag=body.asset_tag)
    try:
        call = svc.open_service_call(**body.model_dump(), actor=user.get("id"))
    except svc.BiomedicalError as exc:
        raise _fail(exc) from exc
    _audit(
        request,
        user,
        "biomedical_service_call_opened",
        summary=(
            f"Service call opened on asset {call.get('asset_id')}: {call.get('fault_description')}"
        ),
        target_type="biomedical_service_call",
        target_id=call.get("id"),
        metadata={
            "severity": call.get("severity"),
            "took_out_of_service": call.get("took_out_of_service"),
        },
    )
    return call


_UPDATE_SERVICE_CALL__DEFAULT = Depends(require_module)
_UPDATE_SERVICE_CALL_USER_DEFAULT = Depends(require_permission(HELIX_WRITE))


@router.post("/service-calls/{call_id}")
def update_service_call(
    call_id: int,
    body: ServiceCallUpdateBody,
    request: Request,
    _: None = _UPDATE_SERVICE_CALL__DEFAULT,
    user: dict = _UPDATE_SERVICE_CALL_USER_DEFAULT,
) -> dict:
    _require_call(facility_scope(user), call_id)
    try:
        call = svc.update_service_call(call_id=call_id, **body.model_dump(), actor=user.get("id"))
    except svc.BiomedicalError as exc:
        raise _fail(exc) from exc
    _audit(
        request,
        user,
        "biomedical_service_call_updated",
        summary=f"Service call {call_id} updated to {call.get('status')}",
        target_type="biomedical_service_call",
        target_id=call_id,
        metadata={"status": call.get("status")},
    )
    return call


_CLOSE_SERVICE_CALL__DEFAULT = Depends(require_module)
_CLOSE_SERVICE_CALL_USER_DEFAULT = Depends(require_permission(HELIX_WRITE))


@router.post("/service-calls/{call_id}/close")
def close_service_call(
    call_id: int,
    body: ServiceCallCloseBody,
    request: Request,
    _: None = _CLOSE_SERVICE_CALL__DEFAULT,
    user: dict = _CLOSE_SERVICE_CALL_USER_DEFAULT,
) -> dict:
    _require_call(facility_scope(user), call_id)
    try:
        call = svc.close_service_call(call_id=call_id, **body.model_dump(), actor=user.get("id"))
    except svc.BiomedicalError as exc:
        raise _fail(exc) from exc
    _audit(
        request,
        user,
        "biomedical_service_call_closed",
        summary=(f"Service call {call_id} closed; {call.get('downtime_minutes')} minutes downtime"),
        target_type="biomedical_service_call",
        target_id=call_id,
        metadata={"downtime_minutes": call.get("downtime_minutes")},
    )
    return call


_DOWNTIME_WARD_DEFAULT = Query(None)
_DOWNTIME_DAYS_DEFAULT = Query(30, ge=1, le=730)
_DOWNTIME_LIMIT_DEFAULT = Query(25, ge=1, le=200)
_DOWNTIME__DEFAULT = Depends(require_module)
_DOWNTIME_USER_DEFAULT = Depends(require_permission(HELIX_READ))


@router.get("/downtime")
def downtime(
    ward: str | None = _DOWNTIME_WARD_DEFAULT,
    days: int = _DOWNTIME_DAYS_DEFAULT,
    limit: int = _DOWNTIME_LIMIT_DEFAULT,
    _: None = _DOWNTIME__DEFAULT,
    user: dict = _DOWNTIME_USER_DEFAULT,
) -> dict:
    """Downtime per asset over a window, accumulated across every service call."""
    from datetime import timedelta

    from services.hospital_time import hospital_today

    today = hospital_today()
    facilities = facility_scope(user)
    rows = svc.downtime_summary(
        since=today - timedelta(days=days), limit=limit if facilities is None else 200
    )
    if facilities is not None:
        rows = _keep_assets(rows, _asset_ids(facilities), "asset_id")[:limit]
    return {
        "as_of": today.isoformat(),
        "window_days": days,
        "assets": rows,
        "count": len(rows),
        "total_minutes": sum(r["downtime_minutes"] for r in rows),
    }


# ═════════════════════════════════════════════════════════════════════════════
#  Contracts
# ═════════════════════════════════════════════════════════════════════════════


_LIST_CONTRACTS_STATUS_DEFAULT = Query(None)
_LIST_CONTRACTS_CONTRACT_TYPE_DEFAULT = Query(None)
_LIST_CONTRACTS__DEFAULT = Depends(require_module)
_LIST_CONTRACTS_USER_DEFAULT = Depends(require_permission(HELIX_READ))


@router.get("/contracts")
def list_contracts(
    status: str | None = _LIST_CONTRACTS_STATUS_DEFAULT,
    contract_type: str | None = _LIST_CONTRACTS_CONTRACT_TYPE_DEFAULT,
    _: None = _LIST_CONTRACTS__DEFAULT,
    user: dict = _LIST_CONTRACTS_USER_DEFAULT,
) -> dict:
    facilities = facility_scope(user)
    try:
        rows = svc.list_contracts(status=status, contract_type=contract_type)
    except svc.BiomedicalError as exc:
        raise _fail(exc) from exc
    if facilities is not None:
        rows = _contracts_seen(rows, _asset_ids(facilities))
    return {"contracts": rows, "count": len(rows)}


_EXPIRING_CONTRACTS_WITHIN_DAYS_DEFAULT = Query(None, ge=0, le=1095)
_EXPIRING_CONTRACTS__DEFAULT = Depends(require_module)
_EXPIRING_CONTRACTS_USER_DEFAULT = Depends(require_permission(HELIX_READ))


@router.get("/contracts/expiring")
def expiring_contracts(
    within_days: int | None = _EXPIRING_CONTRACTS_WITHIN_DAYS_DEFAULT,
    _: None = _EXPIRING_CONTRACTS__DEFAULT,
    user: dict = _EXPIRING_CONTRACTS_USER_DEFAULT,
) -> dict:
    """Contracts inside their notice window, plus the ones already expired."""
    facilities = facility_scope(user)
    try:
        out = svc.expiring_contracts(within_days=within_days)
    except svc.BiomedicalError as exc:
        raise _fail(exc) from exc
    if facilities is None:
        return out
    ids = _asset_ids(facilities)
    expiring = _contracts_seen(out.get("expiring") or [], ids)
    expired = _contracts_seen(out.get("expired") or [], ids)
    out.update(
        {
            "expiring": expiring,
            "expiring_count": len(expiring),
            "expired": expired,
            "expired_count": len(expired),
        }
    )
    return out


_GET_CONTRACT__DEFAULT = Depends(require_module)
_GET_CONTRACT_USER_DEFAULT = Depends(require_permission(HELIX_READ))


@router.get("/contracts/{contract_id}")
def get_contract(
    contract_id: int,
    _: None = _GET_CONTRACT__DEFAULT,
    user: dict = _GET_CONTRACT_USER_DEFAULT,
) -> dict:
    contract = svc.get_contract(contract_id)
    facilities = facility_scope(user)
    ids = _asset_ids(facilities) if facilities is not None else set()
    if not contract or (facilities is not None and not _contract_visible(contract_id, ids)):
        raise HTTPException(
            status_code=404, detail={"error": "not_found", "message": f"no contract {contract_id}"}
        )
    if facilities is not None:
        # Only this facility's machines on a contract that covers several.
        contract["assets"] = _keep_assets(contract.get("assets") or [], ids)
        contract["asset_count"] = len(contract["assets"])
    return contract


_UPSERT_CONTRACT__DEFAULT = Depends(require_module)
_UPSERT_CONTRACT_USER_DEFAULT = Depends(require_permission(ADMIN_OVERVIEW))


@router.post("/contracts")
def upsert_contract(
    body: ContractBody,
    request: Request,
    _: None = _UPSERT_CONTRACT__DEFAULT,
    user: dict = _UPSERT_CONTRACT_USER_DEFAULT,
) -> dict:
    """Create or amend an AMC/CMC. `admin.overview` because a maintenance
    contract is money the hospital has committed, not a workshop record.

    A facility-limited caller amends only a contract that covers one of their
    facility's machines and no other facility's: the dates, status, value and
    the coverage list (which `asset_ids` / `asset_tags` REPLACE) belong to every
    machine on it, so a contract shared with another facility is a group
    account's to change (403 facility_scope). They name only their own machines
    on it, and a contract they make or amend covers at least one of them."""
    facilities = facility_scope(user)
    if facilities is not None:
        ids = _asset_ids(facilities)
        existing = _one_id(
            "SELECT id FROM biomedical_contracts WHERE contract_number = ?",
            (body.contract_number.strip(),),
        )
        if existing is not None:
            covered = _contract_assets(existing)
            if not covered & ids:
                raise HTTPException(
                    status_code=404,
                    detail={"error": "not_found", "message": f"no contract {body.contract_number}"},
                )
            if covered - ids:
                raise _refuse_shared_contract(body.contract_number.strip())
        for asset_id in body.asset_ids or []:
            _require_asset(facilities, asset_id=asset_id)
        for tag in body.asset_tags or []:
            _require_asset(facilities, asset_tag=tag)
        replacing = body.asset_ids is not None or body.asset_tags is not None
        if not (body.asset_ids or body.asset_tags) and (existing is None or replacing):
            raise HTTPException(
                status_code=422,
                detail={
                    "error": "assets_required",
                    "message": (
                        "Name at least one of this facility's machines the contract covers. "
                        "Nothing was saved."
                    ),
                },
            )
    try:
        contract = svc.upsert_contract(**body.model_dump(), actor=user.get("id"))
    except svc.BiomedicalError as exc:
        raise _fail(exc) from exc
    _audit(
        request,
        user,
        "biomedical_contract_saved",
        summary=(
            f"{contract.get('contract_type', '').upper()} "
            f"{contract.get('contract_number')} saved, ends "
            f"{contract.get('end_date')}, covering "
            f"{contract.get('asset_count')} asset(s)"
        ),
        target_type="biomedical_contract",
        target_id=contract.get("id"),
        metadata={
            "contract_number": contract.get("contract_number"),
            "end_date": contract.get("end_date"),
            "asset_count": contract.get("asset_count"),
        },
    )
    return contract
