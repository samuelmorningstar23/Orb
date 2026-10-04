"""Laboratory catalogue and master: endpoints under /api/lab.

Empty until lis-3, lis-4 (W2-D, W3-D). The package router in routes/lab/__init__.py adds the
/api/lab prefix. This router carries require_lab_module(allow_draining=True): 404 while the
laboratory is off, reachable while it is on or draining. A route that starts
new work adds require_lab_module() as well.

Planned endpoints (docs/his_design/lis.json, section 7):
  GET  /catalogue?q=                         order.read or lab.read
  /master/tests, /panels, /ranges, /critical-limits, /specimen-types, /rejection-reasons   GET lab.read, writes lab.master

No reference range and no critical limit ships active (rulings LIS-R1, LIS-R2, held); the pathologist approves each. Writes audit as clinical_config.

Built in lis-3 (W2-D). Reads are open while the module is on or draining, so
laboratory staff finishing open work can still see the catalogue. Every write
also carries the strict gate (require_lab_module()), so the catalogue cannot
change while the module drains, and needs lab.master. The service checks
lab.master again and writes its clinical_config audit row inside the same
transaction, so a write whose audit fails commits nothing. No route here
returns patient data.
"""
import uuid
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from auth import get_current_user
from database import db_session, run_db
from permissions import LAB_MASTER, LAB_READ, ORDER_READ, has_permission, require_permission
from routes.opt_in_gates import require_lab_module
from services import lab_master_service as master
from services.audit_service import log_audit_event

router = APIRouter(dependencies=[Depends(require_lab_module(allow_draining=True))])

# A write starts new configuration, so it 404s while the laboratory drains.
_WRITE_GATE = [Depends(require_lab_module())]


def _require_any_permission(*permissions: str):
    """Like require_permission, but ANY one of `permissions` will do (the
    catalogue serves both the order screen and the laboratory). A denial is
    audited exactly as require_permission audits one."""
    async def _checker(request: Request, user: dict = Depends(get_current_user)) -> dict:
        role = user.get("role", "")
        if any(has_permission(role, p) for p in permissions):
            return user
        await run_db(
            log_audit_event,
            correlation_id=str(uuid.uuid4()),
            event_category="authorization",
            action_type="permission_denied",
            outcome="denied",
            summary_message=f"Missing any of: {', '.join(permissions)}",
            actor_user_id=user.get("id"),
            actor_role=role,
            request_path=request.url.path,
            http_method=request.method,
            ip_address=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
            metadata={"required_any": list(permissions), "user_role": role},
        )
        raise HTTPException(status_code=403, detail={
            "error": "permission_denied", "message": "Insufficient permissions",
            "required_any": list(permissions)})
    _checker.__name__ = "require_any_permission"
    return _checker


def _refusal(err: master.LabMasterError) -> HTTPException:
    return HTTPException(status_code=err.status,
                         detail={"error": err.code, "message": err.message})


def _read(fn, *args, **kwargs):
    try:
        with db_session() as db:
            return fn(db, *args, **kwargs)
    except master.LabMasterError as err:
        raise _refusal(err)


def _write(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except master.LabMasterError as err:
        raise _refusal(err)


# ── Request bodies (unknown fields are refused, never ignored) ───────────────

class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LabTestCreate(_Body):
    code_system: str = Field(..., max_length=8)
    code: str = Field(..., min_length=1, max_length=64)
    kind: str = "analyte"
    name: str = Field(..., min_length=1, max_length=200)
    short_name: str = Field(..., min_length=1, max_length=16)
    performed_at: str = "lab"
    section_id: Optional[str] = None
    specimen_type_code: str
    result_type: str = "numeric"
    unit: Optional[str] = None
    decimal_places: int = 0
    coded_values_json: Optional[Any] = None
    orderable: bool = True
    tat_routine_min: Optional[int] = None
    tat_urgent_min: Optional[int] = None
    tat_stat_min: Optional[int] = None


class LabTestUpdate(_Body):
    name: Optional[str] = None
    short_name: Optional[str] = None
    performed_at: Optional[str] = None
    section_id: Optional[str] = None
    specimen_type_code: Optional[str] = None
    result_type: Optional[str] = None
    unit: Optional[str] = None
    decimal_places: Optional[int] = None
    coded_values_json: Optional[Any] = None
    orderable: Optional[bool] = None
    tat_routine_min: Optional[int] = None
    tat_urgent_min: Optional[int] = None
    tat_stat_min: Optional[int] = None
    delta_kind: Optional[str] = None
    delta_threshold_milli: Optional[int] = None
    delta_window_hours: Optional[int] = None
    active: Optional[bool] = None


class PanelComponents(_Body):
    component_test_ids: list[str]


class RangeApproval(_Body):
    test_id: str
    sex: str = "any"
    age_min_days: int = 0
    age_max_days: Optional[int] = None
    # Decimal strings keep the precision a person typed ("5.10"); numbers
    # are accepted too. Either must be exact at the test's decimal places.
    low: Optional[str | int | float] = None
    high: Optional[str | int | float] = None
    method_note: str = Field(..., max_length=500)


class LimitApproval(_Body):
    test_id: str
    sex: str = "any"
    age_min_days: int = 0
    age_max_days: Optional[int] = None
    critical_low: Optional[str | int | float] = None
    critical_high: Optional[str | int | float] = None
    agreed_with: str = Field(..., max_length=500)
    method_note: str = Field(..., max_length=500)


class Retirement(_Body):
    reason: str = Field(..., max_length=500)


class SpecimenTypeBody(_Body):
    display: str
    container: Optional[str] = None
    container_colour_name: Optional[str] = None
    min_volume_ul: Optional[int] = None
    stability_minutes: Optional[int] = None
    handling_note: Optional[str] = None
    active: bool = True


class SectionBody(_Body):
    name: str
    sort_order: int = 0
    active: bool = True


class RejectionReasonBody(_Body):
    display: str
    incident_prompt: bool = False
    active: bool = True


# ── The catalogue (order screen and laboratory) ──────────────────────────────

@router.get("/catalogue")
def lab_catalogue(q: str = Query("", max_length=100), limit: int = Query(25, ge=1, le=100),
                  user: dict = Depends(_require_any_permission(ORDER_READ, LAB_READ))):
    """Active orderable tests matching q, with the specimen and container to
    collect. No patient data."""
    return {"tests": _read(master.search_catalogue, q, limit=limit)}


# ── Tests and panels ─────────────────────────────────────────────────────────

@router.get("/master/tests")
def list_lab_tests(include_inactive: bool = False, kind: Optional[str] = None,
                   user: dict = Depends(require_permission(LAB_READ))):
    return {"tests": _read(master.list_tests, include_inactive=include_inactive, kind=kind),
            "gaps": _read(master.configuration_gaps)}


@router.get("/master/tests/{test_id}")
def get_lab_test(test_id: str, user: dict = Depends(require_permission(LAB_READ))):
    test = _read(master.get_test, test_id)
    if test is None:
        raise HTTPException(status_code=404, detail={"error": "not_found",
                                                     "message": "No such laboratory test."})
    return test


@router.post("/master/tests", dependencies=_WRITE_GATE, status_code=201)
def create_lab_test(body: LabTestCreate, user: dict = Depends(require_permission(LAB_MASTER))):
    return _write(master.create_test, body.model_dump(exclude_unset=False), actor=user)


@router.patch("/master/tests/{test_id}", dependencies=_WRITE_GATE)
def update_lab_test(test_id: str, body: LabTestUpdate,
                    user: dict = Depends(require_permission(LAB_MASTER))):
    return _write(master.update_test, test_id, body.model_dump(exclude_unset=True), actor=user)


@router.get("/master/panels")
def list_lab_panels(user: dict = Depends(require_permission(LAB_READ))):
    return {"panels": _read(master.list_panels)}


@router.put("/master/panels/{panel_id}/components", dependencies=_WRITE_GATE)
def set_lab_panel_components(panel_id: str, body: PanelComponents,
                             user: dict = Depends(require_permission(LAB_MASTER))):
    return _write(master.set_panel_components, panel_id, body.component_test_ids, actor=user)


# ── Reference ranges and critical limits ─────────────────────────────────────

@router.get("/master/ranges")
def list_lab_ranges(test_id: Optional[str] = None, include_retired: bool = False,
                    user: dict = Depends(require_permission(LAB_READ))):
    return {"ranges": _read(master.list_reference_ranges, test_id=test_id,
                            include_retired=include_retired)}


@router.post("/master/ranges", dependencies=_WRITE_GATE, status_code=201)
def approve_lab_range(body: RangeApproval, user: dict = Depends(require_permission(LAB_MASTER))):
    data = body.model_dump()
    return _write(master.approve_reference_range, data.pop("test_id"), data, actor=user)


@router.post("/master/ranges/{range_id}/retire", dependencies=_WRITE_GATE)
def retire_lab_range(range_id: int, body: Retirement,
                     user: dict = Depends(require_permission(LAB_MASTER))):
    return _write(master.retire_reference_range, range_id, body.reason, actor=user)


@router.get("/master/critical-limits")
def list_lab_critical_limits(test_id: Optional[str] = None, include_retired: bool = False,
                             user: dict = Depends(require_permission(LAB_READ))):
    return {"critical_limits": _read(master.list_critical_limits, test_id=test_id,
                                     include_retired=include_retired)}


@router.post("/master/critical-limits", dependencies=_WRITE_GATE, status_code=201)
def approve_lab_critical_limit(body: LimitApproval,
                               user: dict = Depends(require_permission(LAB_MASTER))):
    data = body.model_dump()
    return _write(master.approve_critical_limit, data.pop("test_id"), data, actor=user)


@router.post("/master/critical-limits/{limit_id}/retire", dependencies=_WRITE_GATE)
def retire_lab_critical_limit(limit_id: int, body: Retirement,
                              user: dict = Depends(require_permission(LAB_MASTER))):
    return _write(master.retire_critical_limit, limit_id, body.reason, actor=user)


# ── Specimen types, sections and rejection reasons ───────────────────────────

@router.get("/master/specimen-types")
def list_lab_specimen_types(include_inactive: bool = False,
                            user: dict = Depends(require_permission(LAB_READ))):
    return {"specimen_types": _read(master.list_specimen_types,
                                    include_inactive=include_inactive)}


@router.put("/master/specimen-types/{code}", dependencies=_WRITE_GATE)
def save_lab_specimen_type(code: str, body: SpecimenTypeBody,
                           user: dict = Depends(require_permission(LAB_MASTER))):
    return _write(master.upsert_specimen_type, code, body.model_dump(), actor=user)


@router.get("/master/sections")
def list_lab_sections(include_inactive: bool = False,
                      user: dict = Depends(require_permission(LAB_READ))):
    return {"sections": _read(master.list_sections, include_inactive=include_inactive)}


@router.put("/master/sections/{section_id}", dependencies=_WRITE_GATE)
def save_lab_section(section_id: str, body: SectionBody,
                     user: dict = Depends(require_permission(LAB_MASTER))):
    return _write(master.upsert_section, section_id, body.model_dump(), actor=user)


@router.get("/master/rejection-reasons")
def list_lab_rejection_reasons(include_inactive: bool = False,
                               user: dict = Depends(require_permission(LAB_READ))):
    return {"rejection_reasons": _read(master.list_rejection_reasons,
                                       include_inactive=include_inactive)}


@router.put("/master/rejection-reasons/{code}", dependencies=_WRITE_GATE)
def save_lab_rejection_reason(code: str, body: RejectionReasonBody,
                              user: dict = Depends(require_permission(LAB_MASTER))):
    return _write(master.upsert_rejection_reason, code, body.model_dump(), actor=user)
