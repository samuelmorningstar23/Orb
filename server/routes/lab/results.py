"""Results: endpoints under /api/lab.

Empty until lis-10, lis-11, lis-12, lis-13 (W5-D, W6-F, W7-H). The package router in routes/lab/__init__.py adds the
/api/lab prefix. This router carries require_lab_module(allow_draining=True): 404 while the
laboratory is off, reachable while it is on or draining. A route that starts
new work adds require_lab_module() as well.

Planned endpoints (docs/his_design/lis.json, section 7):
  GET  /worklists/{section_id}               lab.result
  POST /results, PATCH /results/{result_id}, POST /results/{result_id}/delta-ack, POST /results/tech-validate   lab.result
  POST /results/authorise                    lab.authorise, per specimen
  POST /results/{result_id}/amend, /withdraw   lab.authorise, reason required

Two different people enter and authorise while config.LAB_REQUIRE_DISTINCT_AUTHORISER is on (ruling LIS-R5, held). No autoverification (LIS-R8).

Built in lis-10 (W5-D), all lab.result, all served while the module drains
(entering a result finishes open work, ruling LIS-R21):
  GET   /worklists                  The sections with their counts (no
                                    patient data).
  GET   /worklists/{section_id}     One section's bench: tests on received
                                    samples waiting for or holding an entered
                                    result, STAT first then the oldest
                                    receipt, and the tests cancelled since
                                    receipt with who and when. Hospital-wide
                                    for the laboratory, department-scoped for
                                    an account with a department. Audited as
                                    phi_access 'lab_worklist_viewed'.
  POST  /results                    Enter one result: at the test's reporting
                                    precision, a comparator kept as a bound,
                                    a coded value from the test's list; H and
                                    L only against an approved range.
  PATCH /results/{result_id}        Change it before technical validation;
                                    the value before and after is kept. The
                                    body's `expected` says what the screen
                                    showed (value_text, comment,
                                    abnormal_flag); a result changed since is
                                    refused 409 'result_changed' with the
                                    current row (audited as phi_access
                                    'lab_result_conflict_viewed').
Both writes are one immediate transaction with a clinical_access audit row.
"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from database import db_session
from permissions import LAB_RESULT, department_scope, log_phi_read, require_permission
from routes.lab.orders import audit_unavailable, request_meta
from routes.opt_in_gates import require_lab_module
from services import lab_result_service
from services.audit_service import AuditWriteError

router = APIRouter(dependencies=[Depends(require_lab_module(allow_draining=True))])


class EnterBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    order_item_id: int
    # The value as typed ("5.10", "<0.01", "Positive"): never a parsed float,
    # so the server checks it at the test's reporting precision.
    value: str = Field(..., max_length=lab_result_service.TEXT_RESULT_MAX)
    comment: Optional[str] = Field(None, max_length=1000)


class ExpectedResult(BaseModel):
    """What the editor's screen showed of the result. All three keys are
    required (comment and flag may be null), so an edit always says what it
    saw; a stored result that differs is refused 409 'result_changed'."""
    model_config = ConfigDict(extra="forbid")
    value_text: str = Field(..., max_length=lab_result_service.TEXT_RESULT_MAX)
    comment: Optional[str] = Field(..., max_length=1000)
    abnormal_flag: Optional[str] = Field(..., max_length=4)


class EditBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: str = Field(..., max_length=lab_result_service.TEXT_RESULT_MAX)
    comment: Optional[str] = Field(None, max_length=1000)
    reason: Optional[str] = Field(None, max_length=500)
    expected: ExpectedResult


def _refusal(err: lab_result_service.LabResultError) -> HTTPException:
    return HTTPException(status_code=err.status,
                         detail={"error": err.code, "message": err.message, **err.extra})


@router.get("/worklists")
def list_worklists(user: dict = Depends(require_permission(LAB_RESULT))):
    """The laboratory's sections and how much waits on each bench. Counts
    only, so no audit row."""
    with db_session() as db:
        return {"sections": lab_result_service.list_sections(db)}


@router.get("/worklists/{section_id}")
def get_worklist(section_id: str, request: Request,
                 user: dict = Depends(require_permission(LAB_RESULT))):
    """One section's worklist. The audit row records the patients actually
    returned."""
    dept = department_scope(user)
    try:
        with db_session() as db:
            bench = lab_result_service.worklist(db, section_id, dept=dept)
    except lab_result_service.LabResultError as err:
        raise _refusal(err)
    patient_ids = sorted({i["patient"]["id"] for i in bench["items"]}
                         | {c["patient"]["id"] for c in bench["cancelled"]})
    log_phi_read(
        request=request, user=user,
        action_type="lab_worklist_viewed",
        summary_message=(f"Laboratory worklist viewed: {bench['section']['name']} "
                         f"({len(bench['items'])} tests)"),
        target_type="lab_worklist",
        target_id=section_id,
        metadata={"count": len(bench["items"]), "cancelled": len(bench["cancelled"]),
                  "patient_count": len(patient_ids), "patient_ids": patient_ids[:200]},
    )
    return {**bench, "scope": {"department": dept, "hospital_wide": dept is None}}


@router.post("/results")
def enter_result(body: EnterBody, request: Request,
                 user: dict = Depends(require_permission(LAB_RESULT))):
    try:
        return lab_result_service.enter_result(
            body.order_item_id, value=body.value, comment=body.comment, actor=user,
            dept=department_scope(user), request_meta=request_meta(request))
    except lab_result_service.LabResultError as err:
        raise _refusal(err)
    except AuditWriteError:
        raise audit_unavailable()


@router.patch("/results/{result_id}")
def edit_result(result_id: int, body: EditBody, request: Request,
                user: dict = Depends(require_permission(LAB_RESULT))):
    try:
        return lab_result_service.edit_result(
            result_id, value=body.value, expected=body.expected.model_dump(),
            comment=body.comment, reason=body.reason,
            actor=user, dept=department_scope(user), request_meta=request_meta(request))
    except lab_result_service.LabResultError as err:
        current = err.extra.get("current") if err.code == "result_changed" else None
        if current:
            # The refusal shows the result as it now stands, so it is a PHI
            # read: audited fail-closed before the body leaves.
            try:
                log_phi_read(
                    request=request, user=user,
                    action_type="lab_result_conflict_viewed",
                    summary_message=("Laboratory result shown again: it changed before "
                                     "this edit was saved"),
                    patient_id=current.get("patient_id"),
                    target_type="lab_result",
                    target_id=str(result_id),
                    metadata={"changed_fields": err.extra.get("changed_fields")},
                )
            except AuditWriteError:
                raise audit_unavailable()
        raise _refusal(err)
    except AuditWriteError:
        raise audit_unavailable()
