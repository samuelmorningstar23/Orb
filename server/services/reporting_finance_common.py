"""Bounded local reporting/finance primitives; no inference or network access."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any

import config
from permissions import department_scope, has_all_permissions, patient_in_scope, scope_patient_ids_sql
from services import facility_service
from services.audit_service import log_audit_event
from services.hospital_time import hospital_today, local_day_bounds_utc


class WorkbenchError(ValueError):
    def __init__(self, code: str, message: str, status: int = 422):
        super().__init__(message)
        self.code, self.status = code, status


def stamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def packed(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest(value: Any) -> str:
    return hashlib.sha256(packed(value).encode()).hexdigest()


def text(value: Any, name: str, *, maximum: int = 200, blank: bool = False) -> str:
    if not isinstance(value, str) or len(value) > maximum or (not blank and not value.strip()):
        raise WorkbenchError("invalid_input", f"{name} must be {'non-empty ' if not blank else ''}text of at most {maximum} characters.")
    return value.strip()


def integer(value: Any, name: str, *, minimum: int = 0, maximum: int = 9_000_000_000_000_000) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise WorkbenchError("invalid_input", f"{name} must be an integer between {minimum} and {maximum}.")
    return value


def identity(db, user: dict, *permissions: str) -> dict:
    """Reload the active staff identity; callers cannot forge role or scope."""
    uid = user.get("id") if isinstance(user, dict) else None
    row = db.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone() if uid else None
    if row is None:
        raise WorkbenchError("authentication_required", "An active staff session is required.", 401)
    actor = dict(row)
    from services.offline_workflows import WorkflowError, scope_for
    try:
        scope_for(db, actor)
    except WorkflowError as exc:
        raise WorkbenchError(exc.code, str(exc), exc.status) from exc
    if actor.get("is_active") is not None and not actor["is_active"]:
        raise WorkbenchError("authentication_required", "An active staff session is required.", 401)
    if not has_all_permissions(actor.get("role", ""), *permissions):
        raise WorkbenchError("permission_denied", "This account cannot perform that action.", 403)
    return actor


def module_on(attribute: str, module: str) -> None:
    if not getattr(config, attribute, False):
        raise WorkbenchError("module_disabled", f"The {module} module is not enabled.", 404)


def scope(db, actor: dict) -> tuple[str | None, frozenset[int] | None]:
    return department_scope(actor), facility_service.facility_scope_for(actor, db=db)


def patients_sql(db, actor: dict, *, department: bool = True) -> tuple[str, tuple]:
    dept, facilities = scope(db, actor)
    return scope_patient_ids_sql(dept if department else None, facilities=facilities)


def require_patient(db, actor: dict, patient_id: str) -> None:
    dept, facilities = scope(db, actor)
    exists = db.execute("SELECT 1 FROM patients WHERE id = ?", (patient_id,)).fetchone()
    if exists is None or not patient_in_scope(db, patient_id, dept, facilities=facilities):
        raise WorkbenchError("not_found", "Record not found.", 404)


def facility(db, actor: dict, facility_id: Any) -> int:
    fid = integer(facility_id, "facility_id", minimum=1, maximum=2_147_483_647)
    permitted = facility_service.facility_scope_for(actor, db=db)
    if permitted is not None and fid not in permitted:
        raise WorkbenchError("not_found", "Facility not found.", 404)
    if db.execute("SELECT 1 FROM facilities WHERE id = ? AND is_active = 1", (fid,)).fetchone() is None:
        raise WorkbenchError("not_found", "Facility not found.", 404)
    return fid


def window(from_day: Any, to_day: Any, *, default: bool = False) -> tuple[str, str, str, str]:
    if default and from_day is None and to_day is None:
        to_day = hospital_today().isoformat()
        from_day = (hospital_today() - timedelta(days=29)).isoformat()
    try:
        first = date.fromisoformat(from_day) if isinstance(from_day, str) and len(from_day) == 10 else None
        last = date.fromisoformat(to_day) if isinstance(to_day, str) and len(to_day) == 10 else None
    except ValueError as exc:
        raise WorkbenchError("invalid_period", "Dates must use YYYY-MM-DD.") from exc
    if first is None or last is None or first > last or (last - first).days >= 366 or last > hospital_today():
        raise WorkbenchError("invalid_period", "Choose a completed or current period of at most 366 days.")
    start, _ = local_day_bounds_utc(first)
    _, end = local_day_bounds_utc(last)
    return first.isoformat(), last.isoformat(), start.isoformat(), end.isoformat()


def audit(db, actor: dict, action: str, *, target: str | None = None,
          metadata: dict | None = None, read: bool = False) -> None:
    """Fail-closed, parent and audit share the caller's one transaction."""
    dept, facilities = scope(db, actor)
    log_audit_event(db=db, correlation_id=str(uuid.uuid4()),
                    event_category="phi_access" if read else "security",
                    action_type=action, outcome="success", summary_message=action.replace("_", " "),
                    actor_user_id=actor["id"], actor_role=actor["role"],
                    target_type="local_workbench", target_id=target,
                    metadata={**(metadata or {}), "department": dept,
                              "facility_scope": None if facilities is None else sorted(facilities)})


def csv_text(columns: list[str], rows: list[dict]) -> str:
    """Escape spreadsheet formulas in text; numeric paise keep numeric signs."""
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(columns)
    for row in rows:
        values = []
        for column in columns:
            value = row.get(column)
            if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@", "\t", "\r")):
                value = "'" + value
            values.append(value)
        writer.writerow(values)
    return output.getvalue()
