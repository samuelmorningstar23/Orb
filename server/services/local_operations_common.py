"""Small shared guards for the three local operational registers only."""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone

from permissions import department_scope, has_permission, scope_patient_ids_sql
from services.audit_service import log_audit_event
from services.clinical_time import format_utc, parse_to_utc, utc_iso_now
from services.facility_service import multi_facility_data
from services import offline_workflows as workflows


class OperationsError(ValueError):
    def __init__(self, message, code="invalid", status=422):
        super().__init__(message)
        self.code, self.status = code, status


def text(value, field, maximum=2000, required=True):
    if value is None and not required:
        return None
    if not isinstance(value, str) or len(value.strip()) > maximum or (required and not value.strip()):
        raise OperationsError(f"{field} must contain 1–{maximum} characters.")
    if any(ord(char) < 32 and char not in "\n\t" for char in value):
        raise OperationsError(f"{field} contains an unsupported character.")
    return value.strip() or None


def choice(value, values, field):
    if value not in values:
        raise OperationsError(f"Choose a valid {field}.")
    return value


def number(value, field, low=1, high=100000):
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise OperationsError(f"{field} must be a whole number between {low} and {high}.")
    return value


def instant(value, field, *, future=False):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError()
        parsed = parse_to_utc(parsed)
    except (ValueError, TypeError, AttributeError):
        raise OperationsError(f"{field} needs a date and time including its time zone.") from None
    if not future and parsed > datetime.now(timezone.utc):
        raise OperationsError(f"{field} cannot be in the future.")
    return format_utc(parsed)


def actor_on(db, actor, permission):
    # Do not defeat the existing fence or imply that these modules completed
    # the held multi-facility sweep. Also protect direct service callers.
    if multi_facility_data(db=db):
        raise OperationsError("This register is not available with multiple facilities on record.", "facility_fence", 503)
    row = db.execute("SELECT id,role,department FROM users WHERE id = ? AND is_active = 1",
                     ((actor or {}).get("id"),)).fetchone()
    if not row:
        raise OperationsError("An active signed-in account is required.", "unauthorized", 401)
    result = dict(row)
    permission_on(result, permission)
    if workflows.scope_for(db, result).facility_id != 1:
        raise OperationsError("This register requires the primary facility.", "facility_fence", 503)
    return result


def permission_on(actor, permission):
    if not has_permission(actor["role"], permission):
        raise OperationsError("This account does not have permission for this operation.", "forbidden", 403)


def patient_filter(actor, column="patient_id"):
    query, args = scope_patient_ids_sql(department_scope(actor), facilities=frozenset({1}))
    return f"({column} IS NULL OR {column} IN ({query}))", args


def scoped_on(db, table, record_id, actor):
    # Table names are private module constants, never request values.
    row = db.execute(f"SELECT * FROM {table} WHERE id = ? AND facility_id = 1", (record_id,)).fetchone()
    if row is None:
        raise OperationsError("Record not found.", "not_found", 404)
    result = dict(row)
    dept = department_scope(actor)
    if dept is not None and result.get("department") != dept:
        raise OperationsError("Record not found.", "not_found", 404)
    if result.get("patient_id"):
        patient_on(db, result["patient_id"], actor)
    return result


def patient_on(db, patient_id, actor):
    pid = text(patient_id, "Patient ID", 100)
    try:
        workflows.require_patient(db, workflows.scope_for(db, actor), pid)
    except workflows.WorkflowError as exc:
        raise OperationsError(str(exc), exc.code, exc.status) from exc
    return pid


def scope_sql(actor, alias=""):
    prefix = alias + "." if alias else ""
    dept = department_scope(actor)
    return (f"{prefix}facility_id = 1" + (f" AND {prefix}department = ?" if dept is not None else ""),
            (dept,) if dept is not None else ())


def revision(row, expected):
    if number(expected, "Revision") != row["version"]:
        raise OperationsError("This record changed. Reload it before trying again.", "version_conflict", 409)


def fingerprint(data):
    return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def replay_on(db, table, actor, key, digest):
    key = text(key, "Idempotency-Key", 150)
    row = db.execute(f"SELECT id,request_hash FROM {table} WHERE facility_id = 1 AND created_by = ? AND client_key = ?",
                     (actor["id"], key)).fetchone()
    if not row:
        return None
    if row["request_hash"] != digest:
        raise OperationsError("This request key was already used for different information.", "idempotency_conflict", 409)
    return scoped_on(db, table, row["id"], actor)


def audit_on(db, actor, domain, record_id, action, *, patient_id=None, meta=None, read=False):
    context = meta or {}
    log_audit_event(db=db, correlation_id=str(uuid.uuid4()),
                    event_category="phi_access" if read else "security",
                    action_type=f"{domain}_{action}", outcome="success",
                    summary_message=f"{domain} {action}", actor_user_id=actor["id"],
                    actor_role=actor["role"], target_type=domain, target_id=str(record_id),
                    patient_id=patient_id, request_path=context.get("request_path"),
                    http_method=context.get("http_method"), ip_address=context.get("ip_address"),
                    user_agent=context.get("user_agent"))


def new_id(prefix):
    return f"{prefix}-{uuid.uuid4().hex}"


def now():
    return utc_iso_now()
