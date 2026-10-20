"""Specimens: endpoints under /api/lab.

Empty until lis-8, lis-9 (W4-D, W5-D). The package router in routes/lab/__init__.py adds the
/api/lab prefix. This router carries require_lab_module(allow_draining=True): 404 while the
laboratory is off, reachable while it is on or draining. A route that starts
new work adds require_lab_module() as well.

Planned endpoints (docs/his_design/lis.json, section 7):
  GET  /collection, /collection/labels       lab.collect (ward-scoped for department accounts)
  POST /specimens/{specimen_id}/collect      lab.collect, two-identifier check; add require_lab_module() (new work)
  GET  /specimens/scan/{code}, /specimens/{specimen_id}
  POST /specimens/{specimen_id}/receive, /reject, /recollect   lab.receive

Back-dating a collection is refused until ruling LIS-R6 is confirmed (config.LAB_COLLECTION_BACKDATE_MINUTES is 0).

Built in lis-8 (W4-D):
  GET  /collection                  lab.collect. The ward Samples view and the
                                    laboratory's phlebotomy list: a department
                                    account sees its own department only, an
                                    account with none (the laboratory roles)
                                    every ward. Readable while draining, so
                                    what is left can be seen. Audited as
                                    phi_access 'lab_collection_list_viewed'.
  POST /collection/labels           lab.collect, strict gate (new work). One
                                    patient's planned samples, after the UHID
                                    and name typed at the bedside both match
                                    (LIS-R6, strictest option). Each print is
                                    counted on the specimen and audited as
                                    phi_access 'lab_labels_printed' in the same
                                    transaction. A POST, not the GET the design
                                    listed: printing changes the specimen
                                    (label_print_count closes it to merges).
  POST /specimens/{specimen_id}/collect   lab.collect, strict gate. The same
                                    two identifiers, a printed label, and no
                                    back-dating while the site limit is 0; one
                                    immediate transaction with its
                                    clinical_access audit row.

Built in lis-9 (W5-D). Every accession number in a path is checked by its
check digit first (422 bad_accession, nothing looked up). All need
lab.receive; the laboratory roles are hospital-wide and a department account
is scoped to its department (404, never 403):
  GET  /receiving                   Samples in transit (timed from the
                                    collection event) and rejected samples
                                    waiting for a decision, plus the
                                    rejection reasons. Readable while
                                    draining. Audited as phi_access
                                    'lab_receiving_list_viewed'.
  GET  /specimens/scan/{code}       One scanned or typed sample, with the
                                    steps it offers and its stability.
                                    Audited as phi_access 'lab_specimen_viewed'.
  POST /specimens/{specimen_id}/receive    Only a collected sample; past its
                                    stability time only with a reason.
  POST /specimens/{specimen_id}/reject     A reason code (and a note for
                                    'other'); a plain case-room notice after
                                    the commit; a specimen error incident
                                    prompt with the NABH module on.
  POST /specimens/{specimen_id}/recollect  Strict gate (new work): a new
                                    planned tube linked by recollect_of.
  POST /specimens/{specimen_id}/cancel-rejected   The other decision: the
                                    rejected tests are cancelled with a reason.
Receive, reject and the cancel decision finish open work, so they are served
while the module drains (ruling LIS-R21); recollecting starts new work and is
not.
"""

from typing import Any

from database import db_session
from fastapi import APIRouter, Depends, Request
from permissions import (
    LAB_COLLECT,
    LAB_RECEIVE,
    department_scope,
    facility_scope,
    log_phi_read,
    require_permission,
)
from pydantic import BaseModel, ConfigDict, Field
from services.audit_service import AuditWriteError

from routes.lab.orders import audit_unavailable, lab_refusal, request_meta
from routes.opt_in_gates import require_lab_module
from services import lab_order_service

router = APIRouter(dependencies=[Depends(require_lab_module(allow_draining=True))])

# Starting new work: 404 while the laboratory drains (ruling LIS-R21).
_NEW_WORK = [Depends(require_lab_module())]


class Identity(BaseModel):
    """The two identifiers typed at the bedside, from the wristband or as the
    patient states them. Compared with the record; never stored."""

    model_config = ConfigDict(extra="forbid")
    uhid: str | None = Field(None, max_length=64)
    name: str | None = Field(None, max_length=200)


class LabelsBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    specimen_ids: list[str] = Field(..., max_length=lab_order_service.MAX_LABELS_PER_PRINT)
    identity: Identity | None = None


class CollectBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    identity: Identity | None = None
    collected_at: str | None = Field(None, max_length=64)
    backdate_reason: str | None = Field(None, max_length=500)
    collection_location: str | None = Field(None, max_length=120)


def _identity(value: Identity | None) -> dict[str, Any] | None:
    return value.model_dump() if value is not None else None


_LIST_COLLECTION_USER_DEFAULT = Depends(require_permission(LAB_COLLECT))


@router.get("/collection", response_model=None)
def list_collection(request: Request, user: dict = _LIST_COLLECTION_USER_DEFAULT) -> dict:
    """Samples waiting to be collected, STAT first, then by ward and bed, and
    rejected samples waiting for a recollection. Scoped by department_scope
    and facility_scope, never by a request parameter. The audit row records
    the patients actually returned."""
    dept = department_scope(user)
    with db_session() as db:
        rows = lab_order_service.list_collection(db, dept=dept, facilities=facility_scope(user))
    patient_ids = sorted({r["patient"]["id"] for r in rows})
    log_phi_read(
        request=request,
        user=user,
        action_type="lab_collection_list_viewed",
        summary_message=f"Specimen collection list viewed ({len(rows)} samples)",
        target_type="lab_collection",
        target_id="pending",
        metadata={
            "count": len(rows),
            "patient_count": len(patient_ids),
            "patient_ids": patient_ids[:200],
        },
    )
    return {
        "specimens": rows,
        "scope": {"department": dept, "hospital_wide": dept is None},
        "backdate_minutes": lab_order_service.backdate_limit_minutes(),
    }


_PRINT_COLLECTION_LABELS_USER_DEFAULT = Depends(require_permission(LAB_COLLECT))


@router.post("/collection/labels", dependencies=_NEW_WORK, response_model=None)
def print_collection_labels(
    body: LabelsBody, request: Request, user: dict = _PRINT_COLLECTION_LABELS_USER_DEFAULT
) -> dict:
    """The label payloads for one patient's samples, once the two identifiers
    match. The client draws the barcode (src/lib/code128.ts)."""
    try:
        return lab_order_service.print_labels(
            body.specimen_ids,
            actor=user,
            identity=_identity(body.identity),
            dept=department_scope(user),
            request_meta=request_meta(request),
            facilities=facility_scope(user),
        )
    except lab_order_service.LabOrderError as err:
        raise lab_refusal(err) from err
    except AuditWriteError:
        raise audit_unavailable() from None


_COLLECT_SPECIMEN_USER_DEFAULT = Depends(require_permission(LAB_COLLECT))


@router.post("/specimens/{specimen_id}/collect", dependencies=_NEW_WORK, response_model=None)
def collect_specimen(
    specimen_id: str,
    body: CollectBody,
    request: Request,
    user: dict = _COLLECT_SPECIMEN_USER_DEFAULT,
) -> dict:
    """Record a sample as collected. Unknown, out-of-department and
    out-of-facility are the same 404."""
    try:
        return lab_order_service.collect_specimen(
            specimen_id,
            actor=user,
            identity=_identity(body.identity),
            dept=department_scope(user),
            collected_at=body.collected_at,
            backdate_reason=body.backdate_reason,
            collection_location=body.collection_location,
            request_meta=request_meta(request),
            facilities=facility_scope(user),
        )
    except lab_order_service.LabOrderError as err:
        raise lab_refusal(err) from err
    except AuditWriteError:
        raise audit_unavailable() from None


# ── lis-9: receipt, rejection and the recollect or cancel decision ───────────


class ReceiveBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    receipt_note: str | None = Field(None, max_length=500)
    # A sample past its stability time is received only with this set AND a
    # note saying why it is being processed.
    accept_past_stability: bool = False


class RejectBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason_code: str | None = Field(None, max_length=64)
    note: str | None = Field(None, max_length=500)


class RecollectBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    note: str | None = Field(None, max_length=500)


class CancelRejectedBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str | None = Field(None, max_length=500)


def _patient_ids(rows: list[dict]) -> list[str]:
    return sorted({r["patient"]["id"] for r in rows if r.get("patient")})


_LIST_RECEIVING_USER_DEFAULT = Depends(require_permission(LAB_RECEIVE))


@router.get("/receiving", response_model=None)
def list_receiving(request: Request, user: dict = _LIST_RECEIVING_USER_DEFAULT) -> dict:
    """Samples in transit and rejected samples waiting for a decision, inside
    the caller's department and facilities. The audit row records the
    patients actually returned."""
    dept = department_scope(user)
    with db_session() as db:
        lists = lab_order_service.list_receiving(db, dept=dept, facilities=facility_scope(user))
    patient_ids = sorted(
        set(_patient_ids(lists["in_transit"])) | set(_patient_ids(lists["rejected"]))
    )
    log_phi_read(
        request=request,
        user=user,
        action_type="lab_receiving_list_viewed",
        summary_message=(
            f"Laboratory receiving lists viewed ({len(lists['in_transit'])} in "
            f"transit, {len(lists['rejected'])} rejected)"
        ),
        target_type="lab_receiving",
        target_id="open",
        metadata={
            "in_transit": len(lists["in_transit"]),
            "rejected": len(lists["rejected"]),
            "patient_count": len(patient_ids),
            "patient_ids": patient_ids[:200],
        },
    )
    return {**lists, "scope": {"department": dept, "hospital_wide": dept is None}}


_SCAN_SPECIMEN_USER_DEFAULT = Depends(require_permission(LAB_RECEIVE))


@router.get("/specimens/scan/{code}", response_model=None)
def scan_specimen(code: str, request: Request, user: dict = _SCAN_SPECIMEN_USER_DEFAULT) -> dict:
    """One scanned or typed sample. A bad check digit is 422 and nothing is
    looked up; unknown, out-of-department and out-of-facility are the same
    404 (a facility refusal is audited)."""
    try:
        with db_session() as db:
            detail = lab_order_service.scan_specimen(
                db,
                code,
                dept=department_scope(user),
                facilities=facility_scope(user),
                actor=user,
                request_meta=request_meta(request),
            )
    except lab_order_service.LabOrderError as err:
        raise lab_refusal(err) from err
    log_phi_read(
        request=request,
        user=user,
        action_type="lab_specimen_viewed",
        summary_message="Laboratory specimen viewed at receipt",
        patient_id=detail["patient"]["id"],
        target_type="lab_specimen",
        target_id=detail["id"],
        metadata={"status": detail["status"]},
    )
    return {"specimen": detail}


_RECEIVE_SPECIMEN_USER_DEFAULT = Depends(require_permission(LAB_RECEIVE))


@router.post("/specimens/{specimen_id}/receive", response_model=None)
def receive_specimen(
    specimen_id: str,
    body: ReceiveBody,
    request: Request,
    user: dict = _RECEIVE_SPECIMEN_USER_DEFAULT,
) -> dict:
    try:
        return lab_order_service.receive_specimen(
            specimen_id,
            actor=user,
            dept=department_scope(user),
            receipt_note=body.receipt_note,
            accept_past_stability=body.accept_past_stability,
            request_meta=request_meta(request),
            facilities=facility_scope(user),
        )
    except lab_order_service.LabOrderError as err:
        raise lab_refusal(err) from err
    except AuditWriteError:
        raise audit_unavailable() from None


_REJECT_SPECIMEN_USER_DEFAULT = Depends(require_permission(LAB_RECEIVE))


@router.post("/specimens/{specimen_id}/reject", response_model=None)
def reject_specimen(
    specimen_id: str,
    body: RejectBody,
    request: Request,
    user: dict = _REJECT_SPECIMEN_USER_DEFAULT,
) -> dict:
    try:
        return lab_order_service.reject_specimen(
            specimen_id,
            actor=user,
            dept=department_scope(user),
            reason_code=body.reason_code,
            note=body.note,
            request_meta=request_meta(request),
            facilities=facility_scope(user),
        )
    except lab_order_service.LabOrderError as err:
        raise lab_refusal(err) from err
    except AuditWriteError:
        raise audit_unavailable() from None


_RECOLLECT_SPECIMEN_USER_DEFAULT = Depends(require_permission(LAB_RECEIVE))


@router.post("/specimens/{specimen_id}/recollect", dependencies=_NEW_WORK, response_model=None)
def recollect_specimen(
    specimen_id: str,
    body: RecollectBody,
    request: Request,
    user: dict = _RECOLLECT_SPECIMEN_USER_DEFAULT,
) -> dict:
    try:
        return lab_order_service.recollect_specimen(
            specimen_id,
            actor=user,
            dept=department_scope(user),
            note=body.note,
            request_meta=request_meta(request),
            facilities=facility_scope(user),
        )
    except lab_order_service.LabOrderError as err:
        raise lab_refusal(err) from err
    except AuditWriteError:
        raise audit_unavailable() from None


_CANCEL_REJECTED_TESTS_USER_DEFAULT = Depends(require_permission(LAB_RECEIVE))


@router.post("/specimens/{specimen_id}/cancel-rejected", response_model=None)
def cancel_rejected_tests(
    specimen_id: str,
    body: CancelRejectedBody,
    request: Request,
    user: dict = _CANCEL_REJECTED_TESTS_USER_DEFAULT,
) -> dict:
    try:
        return lab_order_service.cancel_rejected_tests(
            specimen_id,
            actor=user,
            dept=department_scope(user),
            reason=body.reason,
            request_meta=request_meta(request),
            facilities=facility_scope(user),
        )
    except lab_order_service.LabOrderError as err:
        raise lab_refusal(err) from err
    except AuditWriteError:
        raise audit_unavailable() from None
