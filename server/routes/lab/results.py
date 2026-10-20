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

Built in lis-11 and lis-12 (W6-F), all served while the module drains
(validating and releasing finish open work, ruling LIS-R21):
  GET   /validation                 lab.result. The Validate screen: results
                                    waiting for technical validation and
                                    results waiting for a signatory, by
                                    sample. Audited as phi_access
                                    'lab_validation_viewed'.
  POST  /results/{result_id}/delta-ack
                                    lab.result. The technologist's reason
                                    for a delta flag (ruling LIS-R9 (a)).
  POST  /results/tech-validate      lab.result. Technically validate one or
                                    more entered results, all or none; a
                                    delta flag with no reason is refused.
  POST  /results/authorise          lab.authorise. Authorise results on ONE
                                    sample and release them into the chart,
                                    all or none; while two different people
                                    are required (LIS-R5) a signatory who
                                    took part in a result is refused 403
                                    'second_person_required'.
  POST  /results/{result_id}/return lab.authorise. Send a validated result
                                    back to the bench with a reason.
Each write is one immediate transaction with a clinical_access audit row per
result, and each says what its screen showed (`expected`); a result changed
since is refused 409 'result_changed' without its current value (the screen
reloads the audited queue instead).

lis-15 (W7-H): technical validation of a result flagged critical against an
approved limit raises its critical result (preliminary, ruling LIS-R4 (a)),
and the answer's `criticals` says whether the ward was told in the case room
(services/lab_critical_service.py; the Critical results screen is
routes/lab/criticals.py).

Built in lis-13 (W7-H), served while the module drains (correcting the
record is never new work), ruling LIS-R10 (a) applied as the build default:
  GET   /results/reported/{specimen_id}
                                    lab.result. One sample's reported results
                                    with their earlier versions, withdrawals
                                    and any amendment waiting for a second
                                    signatory. Audited as phi_access
                                    'lab_reported_results_viewed'.
  POST  /results/{result_id}/amend  lab.authorise. Amend a reported result
                                    with a reason. While two different people
                                    are required (LIS-R5) this proposes it;
                                    otherwise it is applied at once.
  POST  /results/{result_id}/amend/confirm
                                    lab.authorise. A second signatory applies
                                    a proposed amendment.
  POST  /results/{result_id}/amend/cancel
                                    lab.authorise. Cancel a proposal.
  POST  /results/{result_id}/withdraw
                                    lab.authorise. Withdraw a result entered
                                    in error, with a reason.
An applied amendment or a withdrawal posts a plain notice to the patient's
case room after it commits; a failed notice never undoes it.
"""

from database import db_session
from fastapi import APIRouter, Depends, HTTPException, Request
from permissions import (
    LAB_AUTHORISE,
    LAB_RESULT,
    department_scope,
    facility_scope,
    log_phi_read,
    require_permission,
)
from pydantic import BaseModel, ConfigDict, Field
from services.audit_service import AuditWriteError

from routes.lab.orders import audit_unavailable, request_meta
from routes.opt_in_gates import require_lab_module
from services import lab_result_service

router = APIRouter(dependencies=[Depends(require_lab_module(allow_draining=True))])


class EnterBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    order_item_id: int
    # The value as typed ("5.10", "<0.01", "Positive"): never a parsed float,
    # so the server checks it at the test's reporting precision.
    value: str = Field(..., max_length=lab_result_service.TEXT_RESULT_MAX)
    comment: str | None = Field(None, max_length=1000)


class ExpectedResult(BaseModel):
    """What the editor's screen showed of the result. All three keys are
    required (comment and flag may be null), so an edit always says what it
    saw; a stored result that differs is refused 409 'result_changed'."""

    model_config = ConfigDict(extra="forbid")
    value_text: str = Field(..., max_length=lab_result_service.TEXT_RESULT_MAX)
    comment: str | None = Field(..., max_length=1000)
    abnormal_flag: str | None = Field(..., max_length=4)


class EditBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: str = Field(..., max_length=lab_result_service.TEXT_RESULT_MAX)
    comment: str | None = Field(None, max_length=1000)
    reason: str | None = Field(None, max_length=500)
    expected: ExpectedResult


def _refusal(err: lab_result_service.LabResultError) -> HTTPException:
    return HTTPException(
        status_code=err.status, detail={"error": err.code, "message": err.message, **err.extra}
    )


_LIST_WORKLISTS_USER_DEFAULT = Depends(require_permission(LAB_RESULT))


@router.get("/worklists", response_model=None)
def list_worklists(user: dict = _LIST_WORKLISTS_USER_DEFAULT) -> dict:
    """The laboratory's sections and how much waits on each bench, counting
    only the samples the caller's facilities perform. Counts only, so no
    audit row."""
    with db_session() as db:
        return {"sections": lab_result_service.list_sections(db, facilities=facility_scope(user))}


_GET_WORKLIST_USER_DEFAULT = Depends(require_permission(LAB_RESULT))


@router.get("/worklists/{section_id}", response_model=None)
def get_worklist(
    section_id: str, request: Request, user: dict = _GET_WORKLIST_USER_DEFAULT
) -> dict:
    """One section's worklist, inside the caller's department and
    facilities. The audit row records the patients actually returned."""
    dept = department_scope(user)
    try:
        with db_session() as db:
            bench = lab_result_service.worklist(
                db, section_id, dept=dept, facilities=facility_scope(user)
            )
    except lab_result_service.LabResultError as err:
        raise _refusal(err) from err
    patient_ids = sorted(
        {i["patient"]["id"] for i in bench["items"]}
        | {c["patient"]["id"] for c in bench["cancelled"]}
    )
    log_phi_read(
        request=request,
        user=user,
        action_type="lab_worklist_viewed",
        summary_message=(
            f"Laboratory worklist viewed: {bench['section']['name']} ({len(bench['items'])} tests)"
        ),
        target_type="lab_worklist",
        target_id=section_id,
        metadata={
            "count": len(bench["items"]),
            "cancelled": len(bench["cancelled"]),
            "patient_count": len(patient_ids),
            "patient_ids": patient_ids[:200],
        },
    )
    return {**bench, "scope": {"department": dept, "hospital_wide": dept is None}}


_ENTER_RESULT_USER_DEFAULT = Depends(require_permission(LAB_RESULT))


@router.post("/results", response_model=None)
def enter_result(
    body: EnterBody, request: Request, user: dict = _ENTER_RESULT_USER_DEFAULT
) -> dict:
    try:
        return lab_result_service.enter_result(
            body.order_item_id,
            value=body.value,
            comment=body.comment,
            actor=user,
            dept=department_scope(user),
            request_meta=request_meta(request),
            facilities=facility_scope(user),
        )
    except lab_result_service.LabResultError as err:
        raise _refusal(err) from err
    except AuditWriteError:
        raise audit_unavailable() from None


_EDIT_RESULT_USER_DEFAULT = Depends(require_permission(LAB_RESULT))


@router.patch("/results/{result_id}", response_model=None)
def edit_result(
    result_id: int,
    body: EditBody,
    request: Request,
    user: dict = _EDIT_RESULT_USER_DEFAULT,
) -> dict:
    try:
        return lab_result_service.edit_result(
            result_id,
            value=body.value,
            expected=body.expected.model_dump(),
            comment=body.comment,
            reason=body.reason,
            actor=user,
            dept=department_scope(user),
            request_meta=request_meta(request),
            facilities=facility_scope(user),
        )
    except lab_result_service.LabResultError as err:
        current = err.extra.get("current") if err.code == "result_changed" else None
        if current:
            # The refusal shows the result as it now stands, so it is a PHI
            # read: audited fail-closed before the body leaves.
            try:
                log_phi_read(
                    request=request,
                    user=user,
                    action_type="lab_result_conflict_viewed",
                    summary_message=(
                        "Laboratory result shown again: it changed before this edit was saved"
                    ),
                    patient_id=current.get("patient_id"),
                    target_type="lab_result",
                    target_id=str(result_id),
                    metadata={"changed_fields": err.extra.get("changed_fields")},
                )
            except AuditWriteError:
                raise audit_unavailable() from None
        raise _refusal(err) from err
    except AuditWriteError:
        raise audit_unavailable() from None


# ── Validation and release (lis-11, lis-12) ─────────────────────────────────


class ResultSeen(BaseModel):
    """One result a validation or authorisation names, with what the screen
    showed of it."""

    model_config = ConfigDict(extra="forbid")
    result_id: int
    expected: ExpectedResult


class TechValidateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    results: list[ResultSeen] = Field(
        ..., min_length=1, max_length=lab_result_service.VALIDATION_BATCH_MAX
    )


class AuthoriseBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    specimen_id: str = Field(..., min_length=1, max_length=40)
    results: list[ResultSeen] = Field(
        ..., min_length=1, max_length=lab_result_service.VALIDATION_BATCH_MAX
    )


class ReasonBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(..., max_length=lab_result_service.REASON_MAX)
    expected: ExpectedResult


def _seen_list(results: list[ResultSeen]) -> list[dict]:
    return [{"result_id": r.result_id, "expected": r.expected.model_dump()} for r in results]


def _write(call):
    """Run one validation write, turning its refusals into HTTP answers."""
    try:
        return call()
    except lab_result_service.LabResultError as err:
        raise _refusal(err) from err
    except AuditWriteError:
        raise audit_unavailable() from None


_GET_VALIDATION_USER_DEFAULT = Depends(require_permission(LAB_RESULT))


@router.get("/validation", response_model=None)
def get_validation(request: Request, user: dict = _GET_VALIDATION_USER_DEFAULT) -> dict:
    """Results waiting for technical validation and for a signatory, inside
    the caller's department and facilities. The audit row records the
    patients actually returned."""
    dept = department_scope(user)
    with db_session() as db:
        queues = lab_result_service.validation_queues(
            db, actor=user, dept=dept, facilities=facility_scope(user)
        )
    groups = queues["technical"] + queues["authorisation"]
    patient_ids = sorted({g["patient"]["id"] for g in groups})
    count = sum(len(g["results"]) for g in groups)
    log_phi_read(
        request=request,
        user=user,
        action_type="lab_validation_viewed",
        summary_message=f"Laboratory validation queues viewed ({count} results)",
        target_type="lab_validation",
        target_id="queues",
        metadata={
            "count": count,
            "technical": sum(len(g["results"]) for g in queues["technical"]),
            "authorisation": sum(len(g["results"]) for g in queues["authorisation"]),
            "patient_count": len(patient_ids),
            "patient_ids": patient_ids[:200],
        },
    )
    return {**queues, "scope": {"department": dept, "hospital_wide": dept is None}}


_ACKNOWLEDGE_DELTA_USER_DEFAULT = Depends(require_permission(LAB_RESULT))


@router.post("/results/{result_id}/delta-ack", response_model=None)
def acknowledge_delta(
    result_id: int,
    body: ReasonBody,
    request: Request,
    user: dict = _ACKNOWLEDGE_DELTA_USER_DEFAULT,
) -> dict:
    return _write(
        lambda: lab_result_service.acknowledge_delta(
            result_id,
            reason=body.reason,
            expected=body.expected.model_dump(),
            actor=user,
            dept=department_scope(user),
            request_meta=request_meta(request),
            facilities=facility_scope(user),
        )
    )


_TECH_VALIDATE_USER_DEFAULT = Depends(require_permission(LAB_RESULT))


@router.post("/results/tech-validate", response_model=None)
def tech_validate(
    body: TechValidateBody, request: Request, user: dict = _TECH_VALIDATE_USER_DEFAULT
) -> dict:
    return _write(
        lambda: lab_result_service.technically_validate(
            _seen_list(body.results),
            actor=user,
            dept=department_scope(user),
            request_meta=request_meta(request),
            facilities=facility_scope(user),
        )
    )


_AUTHORISE_USER_DEFAULT = Depends(require_permission(LAB_AUTHORISE))


@router.post("/results/authorise", response_model=None)
def authorise(body: AuthoriseBody, request: Request, user: dict = _AUTHORISE_USER_DEFAULT) -> dict:
    return _write(
        lambda: lab_result_service.authorise_and_release(
            body.specimen_id,
            _seen_list(body.results),
            actor=user,
            dept=department_scope(user),
            request_meta=request_meta(request),
            facilities=facility_scope(user),
        )
    )


_RETURN_TO_BENCH_USER_DEFAULT = Depends(require_permission(LAB_AUTHORISE))


@router.post("/results/{result_id}/return", response_model=None)
def return_to_bench(
    result_id: int,
    body: ReasonBody,
    request: Request,
    user: dict = _RETURN_TO_BENCH_USER_DEFAULT,
) -> dict:
    return _write(
        lambda: lab_result_service.return_to_bench(
            result_id,
            reason=body.reason,
            expected=body.expected.model_dump(),
            actor=user,
            dept=department_scope(user),
            request_meta=request_meta(request),
            facilities=facility_scope(user),
        )
    )


# ── Amended and withdrawn results (lis-13) ──────────────────────────────────


class AmendBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # The amended value as typed, checked at the test's reporting precision.
    value: str = Field(..., max_length=lab_result_service.TEXT_RESULT_MAX)
    comment: str | None = Field(None, max_length=1000)
    reason: str = Field(..., max_length=lab_result_service.REASON_MAX)
    delta_reason: str | None = Field(None, max_length=lab_result_service.REASON_MAX)
    expected: ExpectedResult


class ProposedSeen(BaseModel):
    """What the confirming signatory's screen showed of the proposal."""

    model_config = ConfigDict(extra="forbid")
    value_text: str = Field(..., max_length=lab_result_service.TEXT_RESULT_MAX)
    comment: str | None = Field(..., max_length=1000)


class ConfirmAmendBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    proposal_id: int
    expected: ExpectedResult
    proposed: ProposedSeen


class CancelAmendBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    proposal_id: int
    reason: str = Field(..., max_length=lab_result_service.REASON_MAX)


class WithdrawBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(..., max_length=lab_result_service.REASON_MAX)
    wrong_patient: bool = False
    expected: ExpectedResult


_GET_REPORTED_RESULTS_USER_DEFAULT = Depends(require_permission(LAB_RESULT))


@router.get("/results/reported/{specimen_id}", response_model=None)
def get_reported_results(
    specimen_id: str, request: Request, user: dict = _GET_REPORTED_RESULTS_USER_DEFAULT
) -> dict:
    """One sample's reported results. The audit row names the patient."""
    dept = department_scope(user)
    try:
        with db_session() as db:
            body = lab_result_service.reported_results(
                db,
                specimen_id,
                actor=user,
                dept=dept,
                facilities=facility_scope(user),
                request_meta=request_meta(request),
            )
    except lab_result_service.LabResultError as err:
        raise _refusal(err) from err
    log_phi_read(
        request=request,
        user=user,
        action_type="lab_reported_results_viewed",
        summary_message=(
            f"Reported laboratory results viewed for sample "
            f"{body['specimen']['id']} ({len(body['tests'])} tests)"
        ),
        patient_id=body["specimen"]["patient"]["id"],
        target_type="lab_specimen",
        target_id=body["specimen"]["id"],
        metadata={"tests": len(body["tests"])},
    )
    return body


_AMEND_RESULT_USER_DEFAULT = Depends(require_permission(LAB_AUTHORISE))


@router.post("/results/{result_id}/amend", response_model=None)
def amend_result(
    result_id: int,
    body: AmendBody,
    request: Request,
    user: dict = _AMEND_RESULT_USER_DEFAULT,
) -> dict:
    return _write(
        lambda: lab_result_service.amend_result(
            result_id,
            value=body.value,
            comment=body.comment,
            reason=body.reason,
            delta_reason=body.delta_reason,
            expected=body.expected.model_dump(),
            actor=user,
            dept=department_scope(user),
            request_meta=request_meta(request),
            facilities=facility_scope(user),
        )
    )


_CONFIRM_AMENDMENT_USER_DEFAULT = Depends(require_permission(LAB_AUTHORISE))


@router.post("/results/{result_id}/amend/confirm", response_model=None)
def confirm_amendment(
    result_id: int,
    body: ConfirmAmendBody,
    request: Request,
    user: dict = _CONFIRM_AMENDMENT_USER_DEFAULT,
) -> dict:
    return _write(
        lambda: lab_result_service.confirm_amendment(
            result_id,
            proposal_id=body.proposal_id,
            expected=body.expected.model_dump(),
            proposed=body.proposed.model_dump(),
            actor=user,
            dept=department_scope(user),
            request_meta=request_meta(request),
            facilities=facility_scope(user),
        )
    )


_CANCEL_AMENDMENT_USER_DEFAULT = Depends(require_permission(LAB_AUTHORISE))


@router.post("/results/{result_id}/amend/cancel", response_model=None)
def cancel_amendment(
    result_id: int,
    body: CancelAmendBody,
    request: Request,
    user: dict = _CANCEL_AMENDMENT_USER_DEFAULT,
) -> dict:
    return _write(
        lambda: lab_result_service.cancel_amendment(
            result_id,
            proposal_id=body.proposal_id,
            reason=body.reason,
            actor=user,
            dept=department_scope(user),
            request_meta=request_meta(request),
            facilities=facility_scope(user),
        )
    )


_WITHDRAW_RESULT_USER_DEFAULT = Depends(require_permission(LAB_AUTHORISE))


@router.post("/results/{result_id}/withdraw", response_model=None)
def withdraw_result(
    result_id: int,
    body: WithdrawBody,
    request: Request,
    user: dict = _WITHDRAW_RESULT_USER_DEFAULT,
) -> dict:
    return _write(
        lambda: lab_result_service.withdraw_result(
            result_id,
            reason=body.reason,
            wrong_patient=body.wrong_patient,
            expected=body.expected.model_dump(),
            actor=user,
            dept=department_scope(user),
            request_meta=request_meta(request),
            facilities=facility_scope(user),
        )
    )
