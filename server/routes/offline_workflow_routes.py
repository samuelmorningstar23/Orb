"""Minimal scoped identity search for local workflow forms."""
from fastapi import APIRouter, Depends, HTTPException, Query, Request

import config
from auth import get_current_user
from database import db_session
from permissions import REGISTRATION_READ, has_permission, log_phi_read, scope_patient_ids_sql
from services.offline_workflows import WorkflowError, scope_for

router = APIRouter(prefix="/api/local-workflows", tags=["Local workflows"])

_READ_PERMISSIONS = (
    "feedback.read", "mortuary.read", "cssd.read", "ris.read", "care_plan.read",
    "emergency_record.read", "perioperative_record.read", "dialysis_record.read",
    "record_governance.read", "finance_tools.read", "reporting.read", "infection_surveillance.read",
)
_MODULES = ("feedback", "mortuary", "cssd", "radiology", "care-plans", "emergency",
            "perioperative", "dialysis", "record-governance", "finance-tools", "reporting", "infection-surveillance")


@router.get("/patients")
def patients(request: Request, q: str = Query(min_length=3, max_length=120),
             include_registered: bool = False, user: dict = Depends(get_current_user)):
    if not any(has_permission(user.get("role", ""), p) for p in _READ_PERMISSIONS):
        raise HTTPException(403, "You cannot access local workflow records.")
    if not any(config.module_state(m) == "on" and has_permission(user.get("role", ""), p)
               for m, p in zip(_MODULES, _READ_PERMISSIONS)):
        raise HTTPException(404, "Local workflows are switched off on this site.")
    if include_registered and not has_permission(user.get("role", ""), REGISTRATION_READ):
        raise HTTPException(403, "Registration access is required.")
    query = q.strip()
    if len(query) < 3:
        raise HTTPException(422, "Enter at least three characters.")
    escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    try:
        with db_session() as db:
            scope = scope_for(db, user)
            sql, params = scope_patient_ids_sql(scope.department,
                                               facilities=frozenset({scope.facility_id}))
            eligible = f"p.id IN ({sql})"
            args = list(params)
            if include_registered:
                eligible = (f"({eligible} OR (p.status = 'registered' AND "
                            "p.registered_facility_id = ? AND NOT EXISTS "
                            "(SELECT 1 FROM admissions a WHERE a.patient_id = p.id)))")
                args.append(scope.facility_id)
            rows = db.execute(
                "SELECT p.id, p.name, p.mrn, p.status FROM patients p WHERE " + eligible +
                " AND (p.name LIKE ? ESCAPE '\\' OR p.mrn LIKE ? ESCAPE '\\' "
                "OR p.id LIKE ? ESCAPE '\\') ORDER BY p.name, p.id LIMIT 20",
                (*args, escaped, escaped, escaped),
            ).fetchall()
        log_phi_read(request=request, user=user, action_type="local_workflow_patient_search",
                     summary_message="Searched local workflow patient identities.",
                     target_type="patient_search", metadata={"matched_ids": [r["id"] for r in rows]})
        return {"patients": [dict(r) for r in rows]}
    except WorkflowError as exc:
        raise HTTPException(exc.status, {"error": exc.code, "message": str(exc)}) from exc
