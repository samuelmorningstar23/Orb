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
Scan, receive, reject and recollect are lis-9 (W5-D).
"""
from typing import Any, Optional

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field

from database import db_session
from permissions import LAB_COLLECT, department_scope, log_phi_read, require_permission
from routes.lab.orders import audit_unavailable, lab_refusal, request_meta
from routes.opt_in_gates import require_lab_module
from services import lab_order_service
from services.audit_service import AuditWriteError

router = APIRouter(dependencies=[Depends(require_lab_module(allow_draining=True))])

# Starting new work: 404 while the laboratory drains (ruling LIS-R21).
_NEW_WORK = [Depends(require_lab_module())]


class Identity(BaseModel):
    """The two identifiers typed at the bedside, from the wristband or as the
    patient states them. Compared with the record; never stored."""
    model_config = ConfigDict(extra="forbid")
    uhid: Optional[str] = Field(None, max_length=64)
    name: Optional[str] = Field(None, max_length=200)


class LabelsBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    specimen_ids: list[str] = Field(..., max_length=lab_order_service.MAX_LABELS_PER_PRINT)
    identity: Optional[Identity] = None


class CollectBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    identity: Optional[Identity] = None
    collected_at: Optional[str] = Field(None, max_length=64)
    backdate_reason: Optional[str] = Field(None, max_length=500)
    collection_location: Optional[str] = Field(None, max_length=120)


def _identity(value: Optional[Identity]) -> Optional[dict[str, Any]]:
    return value.model_dump() if value is not None else None


@router.get("/collection")
def list_collection(request: Request, user: dict = Depends(require_permission(LAB_COLLECT))):
    """Samples waiting to be collected, STAT first, then by ward and bed, and
    rejected samples waiting for a recollection. Scoped by department_scope,
    never by a request parameter. The audit row records the patients
    actually returned."""
    dept = department_scope(user)
    with db_session() as db:
        rows = lab_order_service.list_collection(db, dept=dept)
    patient_ids = sorted({r["patient"]["id"] for r in rows})
    log_phi_read(
        request=request, user=user,
        action_type="lab_collection_list_viewed",
        summary_message=f"Specimen collection list viewed ({len(rows)} samples)",
        target_type="lab_collection",
        target_id="pending",
        metadata={"count": len(rows), "patient_count": len(patient_ids),
                  "patient_ids": patient_ids[:200]},
    )
    return {"specimens": rows, "scope": {"department": dept, "hospital_wide": dept is None},
            "backdate_minutes": lab_order_service.backdate_limit_minutes()}


@router.post("/collection/labels", dependencies=_NEW_WORK)
def print_collection_labels(body: LabelsBody, request: Request,
                            user: dict = Depends(require_permission(LAB_COLLECT))):
    """The label payloads for one patient's samples, once the two identifiers
    match. The client draws the barcode (src/lib/code128.ts)."""
    try:
        return lab_order_service.print_labels(
            body.specimen_ids, actor=user, identity=_identity(body.identity),
            dept=department_scope(user), request_meta=request_meta(request))
    except lab_order_service.LabOrderError as err:
        raise lab_refusal(err)
    except AuditWriteError:
        raise audit_unavailable()


@router.post("/specimens/{specimen_id}/collect", dependencies=_NEW_WORK)
def collect_specimen(specimen_id: str, body: CollectBody, request: Request,
                     user: dict = Depends(require_permission(LAB_COLLECT))):
    """Record a sample as collected. Unknown and out-of-department are the
    same 404."""
    try:
        return lab_order_service.collect_specimen(
            specimen_id, actor=user, identity=_identity(body.identity),
            dept=department_scope(user), collected_at=body.collected_at,
            backdate_reason=body.backdate_reason,
            collection_location=body.collection_location,
            request_meta=request_meta(request))
    except lab_order_service.LabOrderError as err:
        raise lab_refusal(err)
    except AuditWriteError:
        raise audit_unavailable()
