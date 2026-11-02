"""Local records custody and explicitly authorised holds; no statutory submission."""
from __future__ import annotations

import json
import uuid

from database import db_session
from db_backend import DBIntegrityError
from permissions import scope_patient_ids_sql, has_permission
from services.offline_workflows import (WorkflowError, assert_record_scope, audit, now,
                                        require_patient, scope_for, text)

def _scope(db, actor, permission):
    scope = scope_for(db, actor)
    if not has_permission(scope.role, permission):
        raise WorkflowError("permission_denied", "Records authority is required.", 403)
    return scope


TABLES = {"hold": "record_legal_holds", "file": "mrd_files", "release": "mrd_release_requests"}


def active_hold(db, patient_id: str, category: str | None = None) -> bool:
    """Used below the erasure decision AND execution boundaries, regardless of flags."""
    exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='record_legal_holds'").fetchone()
    if not exists:
        return False
    for row in db.execute("SELECT categories_json FROM record_legal_holds WHERE patient_id=? AND released_at IS NULL",
                          (patient_id,)):
        try:
            categories = json.loads(row[0])
        except (TypeError, ValueError):
            return True  # Corrupt hold metadata cannot authorise destruction.
        if not isinstance(categories, list) or any(not isinstance(c, str) for c in categories):
            return True
        if not categories or category is None or category in categories:
            return True
    return False


def _event(db, scope, kind, record_id, patient_id, action, reason, version):
    db.execute("INSERT INTO record_governance_events VALUES (?,?,?,?,?,?,?,?,?,?,?)",
               (str(uuid.uuid4()), patient_id, scope.facility_id, scope.department, kind,
                record_id, action, reason, now(), scope.actor_id, version))
    audit(db, scope, f"record_governance_{action}", kind, record_id,
          patient_id=patient_id, metadata={"version": version})


def _get(db, scope, kind, record_id):
    table = TABLES.get(kind)
    if table is None:
        raise WorkflowError("invalid_kind", "Unknown record type.")
    row = db.execute(f"SELECT * FROM {table} WHERE id=?", (record_id,)).fetchone()
    if row is None:
        raise WorkflowError("not_found", "Record not found.", 404)
    assert_record_scope(scope, row)
    require_patient(db, scope, row["patient_id"])
    return dict(row)


def list_records(kind: str, *, actor: dict, patient_id: str | None = None,
                 limit: int = 50, offset: int = 0) -> dict:
    table = TABLES.get(kind)
    if table is None or not 1 <= limit <= 100 or offset < 0:
        raise WorkflowError("invalid_query", "Invalid record query.")
    with db_session() as db:
        scope = _scope(db, actor, "record_governance.read")
        sql, params = scope_patient_ids_sql(scope.department, facilities=frozenset({scope.facility_id}))
        where = f"facility_id=? AND patient_id IN ({sql})"
        args = [scope.facility_id, *params]
        if scope.department is not None:
            where += " AND department=?"; args.append(scope.department)
        if patient_id:
            require_patient(db, scope, patient_id)
            where += " AND patient_id=?"; args.append(patient_id)
        count = db.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}", args).fetchone()[0]
        rows = [dict(r) for r in db.execute(f"SELECT * FROM {table} WHERE {where} ORDER BY created_at DESC,id LIMIT ? OFFSET ?",
                                          (*args, limit, offset))]
        audit(db, scope, "record_governance_list", kind, "list", read=True,
              metadata={"returned": len(rows)})
        return {"records": rows, "total": count}


def detail(kind: str, record_id: str, *, actor: dict) -> dict:
    with db_session() as db:
        scope = _scope(db, actor, "record_governance.read")
        row = _get(db, scope, kind, record_id)
        row["events"] = [dict(r) for r in db.execute(
            "SELECT * FROM record_governance_events WHERE record_type=? AND record_id=? ORDER BY occurred_at,id",
            (kind, record_id))]
        audit(db, scope, "record_governance_detail", kind, record_id, patient_id=row["patient_id"], read=True)
        return row


def create(kind: str, values: dict, *, actor: dict) -> dict:
    with db_session(immediate=True) as db:
        scope = _scope(db, actor, "record_governance.write")
        patient_id = text(values.get("patient_id"), "Patient", maximum=120)
        require_patient(db, scope, patient_id, values.get("admission_id"))
        record_id, stamp = str(uuid.uuid4()), now()
        common = (record_id, patient_id, scope.facility_id, scope.department)
        if kind == "hold":
            categories = values.get("categories", [])
            known = {r[0] for r in db.execute("SELECT category FROM retention_policies")}
            if (not isinstance(categories, list) or len(categories) > 30
                    or any(not isinstance(c, str) or c not in known for c in categories)):
                raise WorkflowError("invalid_categories", "Choose existing retention categories.")
            reason = text(values.get("reason"), "Hold reason")
            reference = text(values.get("authority_reference"), "Authority reference", maximum=300)
            db.execute("""INSERT INTO record_legal_holds
                (id,patient_id,facility_id,department,categories_json,reason,authority_reference,created_at,created_by)
                VALUES (?,?,?,?,?,?,?,?,?)""", (*common, json.dumps(sorted(set(categories))), reason, reference, stamp, scope.actor_id))
        elif kind == "file":
            medium = values.get("medium")
            if medium not in {"paper", "digital_reference"}:
                raise WorkflowError("invalid_medium", "Choose paper or a digital reference.")
            reason = "File registered"
            try:
                db.execute("""INSERT INTO mrd_files
                    (id,patient_id,facility_id,department,admission_id,file_number,medium,location,custodian,state,created_at,created_by,updated_at)
                    VALUES (?,?,?,?,?,?,?,?,?,'archived',?,?,?)""", (*common, values.get("admission_id"),
                    text(values.get("file_number"), "File number", maximum=120), medium,
                    text(values.get("location"), "Location", maximum=200),
                    text(values.get("custodian"), "Custodian", maximum=200), stamp, scope.actor_id, stamp))
            except DBIntegrityError as exc:
                raise WorkflowError("duplicate_file", "This file number is already registered.", 409) from exc
        elif kind == "release":
            documents = values.get("documents")
            if not isinstance(documents, list) or not 1 <= len(documents) <= 50:
                raise WorkflowError("documents_required", "List 1 to 50 document references.")
            documents = [text(d, "Document reference", maximum=200) for d in documents]
            reason = text(values.get("purpose"), "Purpose")
            db.execute("""INSERT INTO mrd_release_requests
                (id,patient_id,facility_id,department,purpose,recipient,documents_json,authority_reference,status,created_at,created_by,updated_at)
                VALUES (?,?,?,?,?,?,?,?,'requested',?,?,?)""", (*common, reason,
                text(values.get("recipient"), "Recipient", maximum=300), json.dumps(documents),
                text(values.get("authority_reference"), "Consent or authority reference", maximum=300), stamp, scope.actor_id, stamp))
        else:
            raise WorkflowError("invalid_kind", "Unknown record type.")
        _event(db, scope, kind, record_id, patient_id, "created", reason, 1)
        return _get(db, scope, kind, record_id)


def transition(kind: str, record_id: str, *, actor: dict, version: int,
               action: str, reason: str, location: str | None = None,
               custodian: str | None = None, reference: str | None = None) -> dict:
    reason = text(reason, "Reason")
    with db_session(immediate=True) as db:
        scope = _scope(db, actor, "record_governance.write")
        if kind in {"hold", "release"} and not has_permission(scope.role, "record_governance.release"):
            raise WorkflowError("permission_denied", "Records release authority is required.", 403)
        row = _get(db, scope, kind, record_id)
        if row["version"] != version:
            raise WorkflowError("version_conflict", "This record changed. Reload before continuing.", 409)
        stamp, revision = now(), version + 1
        if kind == "hold":
            if action != "release" or row["released_at"]:
                raise WorkflowError("invalid_transition", "This hold cannot make that transition.", 409)
            db.execute("UPDATE record_legal_holds SET released_at=?,released_by=?,release_reason=?,release_reference=?,version=? WHERE id=?",
                       (stamp, scope.actor_id, reason, text(reference, "Release authority reference", maximum=300), revision, record_id))
        elif kind == "file":
            allowed = {"archived": {"checked_out", "in_transit", "missing"}, "checked_out": {"archived", "in_transit", "missing"},
                       "in_transit": {"archived", "checked_out", "missing"}, "missing": {"archived"}}
            if action not in allowed[row["state"]]:
                raise WorkflowError("invalid_transition", "This file cannot make that transition.", 409)
            db.execute("UPDATE mrd_files SET state=?,location=?,custodian=?,updated_at=?,version=? WHERE id=?",
                       (action, text(location, "Location", maximum=200), text(custodian, "Custodian", maximum=200), stamp, revision, record_id))
        elif kind == "release":
            allowed = {"requested": {"approved", "denied", "cancelled"}, "approved": {"released", "cancelled"}}
            if action not in allowed.get(row["status"], set()):
                raise WorkflowError("invalid_transition", "This request cannot make that transition.", 409)
            if action == "released":
                reference = text(reference, "Recipient verification and handover reference", maximum=300)
            db.execute("""UPDATE mrd_release_requests SET status=?,decision_reason=?,handover_reference=?,
                decided_by=CASE WHEN ? IN ('approved','denied') THEN ? ELSE decided_by END,
                released_by=CASE WHEN ?='released' THEN ? ELSE released_by END,updated_at=?,version=? WHERE id=?""",
                (action, reason, reference, action, scope.actor_id, action, scope.actor_id, stamp, revision, record_id))
        else:
            raise WorkflowError("invalid_kind", "Unknown record type.")
        _event(db, scope, kind, record_id, row["patient_id"], action, reason, revision)
        return _get(db, scope, kind, record_id)
