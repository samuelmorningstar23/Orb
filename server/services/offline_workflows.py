"""Shared boundaries for locally authored hospital workflows.

No transport, model, clinical inference or automatic lifecycle action lives here.
Call these helpers inside a synchronous handler and its own transaction.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import uuid

from permissions import department_scope, patient_in_scope
from services import facility_service
from services.audit_service import log_audit_event


class WorkflowError(ValueError):
    def __init__(self, code: str, message: str, status: int = 422):
        super().__init__(message)
        self.code, self.status = code, status


@dataclass(frozen=True)
class WorkflowScope:
    actor_id: str
    role: str
    department: str | None
    facility_id: int


def scope_for(db, actor: dict) -> WorkflowScope:
    actor_id = actor.get("id")
    if not isinstance(actor_id, str) or not actor_id.strip():
        raise WorkflowError("unauthenticated", "Sign in to continue.", 401)
    row = db.execute("SELECT * FROM users WHERE id=? AND COALESCE(is_active,1)=1", (actor_id,)).fetchone()
    if row is None:
        raise WorkflowError("unauthenticated", "Sign in to continue.", 401)
    actor = dict(row)
    if facility_service.multi_facility_data(db=db):
        raise WorkflowError("facility_fenced", "Local workflows remain unavailable while the multi-facility fence is active.", 503)
    fid = facility_service.user_facility_id(actor, db=db)
    if fid is None:
        raise WorkflowError("facility_required", "A home facility is required.", 409)
    return WorkflowScope(actor_id, str(actor.get("role", "")),
                         department_scope(actor), int(fid))


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def text(value, field: str, *, maximum: int = 4000, required: bool = True) -> str | None:
    if value is None and not required:
        return None
    if not isinstance(value, str):
        raise WorkflowError("invalid_text", f"{field} must be text.")
    cleaned = value.strip()
    if (required and not cleaned) or len(cleaned) > maximum:
        raise WorkflowError("invalid_text", f"{field} must contain 1 to {maximum} characters.")
    if any(ord(c) < 32 and c not in "\n\t" for c in cleaned):
        raise WorkflowError("invalid_text", f"{field} contains an unsupported character.")
    return cleaned or None


def assert_record_scope(scope: WorkflowScope, record) -> None:
    if (int(record["facility_id"]) != scope.facility_id
            or (scope.department is not None and record["department"] != scope.department)):
        raise WorkflowError("not_found", "Record not found.", 404)


def require_patient(db, scope: WorkflowScope, patient_id: str,
                    admission_id: str | None = None) -> dict:
    patient = db.execute("SELECT * FROM patients WHERE id = ?", (patient_id,)).fetchone()
    if patient is None or not patient_in_scope(
            db, patient_id, scope.department, facilities=frozenset({scope.facility_id})):
        raise WorkflowError("not_found", "Patient not found.", 404)
    if admission_id is not None:
        admission = db.execute(
            "SELECT patient_id, facility_id FROM admissions WHERE id = ?", (admission_id,)
        ).fetchone()
        if (admission is None or admission["patient_id"] != patient_id
                or int(admission["facility_id"]) != scope.facility_id):
            raise WorkflowError("not_found", "Admission not found.", 404)
    return dict(patient)


def audit(db, scope: WorkflowScope, action: str, target_type: str,
          target_id: str, *, patient_id: str | None = None,
          metadata: dict | None = None, read: bool = False) -> None:
    log_audit_event(
        db=db, correlation_id=str(uuid.uuid4()),
        event_category="phi_access" if read else "security",
        action_type=action, outcome="success", actor_user_id=scope.actor_id,
        actor_role=scope.role, target_type=target_type, target_id=str(target_id),
        patient_id=patient_id,
        summary_message="Local workflow record read." if read else "Local workflow record changed.",
        metadata={"facility_id": scope.facility_id, **(metadata or {})},
    )
