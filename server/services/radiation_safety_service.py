"""Evidence register only; never issues a licence or clears equipment for use."""
import json
import uuid
from datetime import date

from database import db_session
from db_backend import DBIntegrityError
from permissions import has_permission
from services.hospital_time import hospital_today
from services.offline_workflows import WorkflowError, audit, now, scope_for, text


def _scope(db, actor, write=False):
    scope = scope_for(db, actor)
    if not has_permission(scope.role, "radiation_safety.write" if write else "radiation_safety.read"):
        raise WorkflowError("permission_denied", "Equipment safety register authority is required.", 403)
    return scope


def _day(value, field):
    try:
        parsed = date.fromisoformat(value)
        if parsed.isoformat() != value:
            raise ValueError
        return parsed
    except (TypeError, ValueError) as exc:
        raise WorkflowError("invalid_date", f"{field} requires a YYYY-MM-DD date.") from exc


def _view(row):
    result = dict(row)
    today = hospital_today().isoformat()
    result["date_status"] = ("withdrawn" if row["state"] == "withdrawn" else "not_yet_started" if today < row["valid_from"]
                             else "recorded_expiry_passed" if today > row["valid_to"] else "within_recorded_dates")
    result["asset"] = json.loads(result.pop("asset_snapshot_json"))
    return result


def _event(db, scope, row, action, reason):
    db.execute("INSERT INTO radiation_safety_events VALUES (?,?,?,?,?,?,?)", (str(uuid.uuid4()), row["id"], scope.actor_id,
                action, reason, now(), json.dumps(dict(row), sort_keys=True)))
    audit(db, scope, f"radiation_safety_{action}", "equipment_safety_record", row["id"], metadata={"asset_id": row["asset_id"]})


def _record(db, scope, record_id):
    where, args = "id=? AND facility_id=?", [record_id, scope.facility_id]
    if scope.department is not None:
        where += " AND json_extract(asset_snapshot_json,'$.department')=?"
        args.append(scope.department)
    row = db.execute(f"SELECT * FROM radiation_safety_records WHERE {where}", args).fetchone()
    if row is None:
        raise WorkflowError("not_found", "Safety record not found.", 404)
    return row


def listing(*, actor: dict, asset_id: int | None = None, offset=0, limit=50,
            asset_query="", asset_offset=0, asset_limit=50):
    if (type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100
            or type(asset_offset) is not int or asset_offset < 0 or type(asset_limit) is not int or not 1 <= asset_limit <= 100
            or not isinstance(asset_query, str) or len(asset_query) > 200):
        raise WorkflowError("invalid_query", "Invalid register or equipment page.")
    with db_session() as db:
        scope = _scope(db, actor)
        where, args = "facility_id=?", [scope.facility_id]
        asset_where, asset_args = "is_active=1", []
        if scope.department is not None:
            where += " AND json_extract(asset_snapshot_json,'$.department')=?"
            args.append(scope.department)
            asset_where += " AND department=?"
            asset_args.append(scope.department)
        if asset_id is not None:
            where += " AND asset_id=?"; args.append(asset_id)
        if asset_query.strip():
            escaped = asset_query.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            asset_where += " AND (asset_tag LIKE ? ESCAPE '\\' OR name LIKE ? ESCAPE '\\' OR serial_number LIKE ? ESCAPE '\\')"
            asset_args.extend([f"%{escaped}%"] * 3)
        count = db.execute(f"SELECT COUNT(*) FROM radiation_safety_records WHERE {where}", args).fetchone()[0]
        rows = [_view(row) for row in db.execute(f"SELECT * FROM radiation_safety_records WHERE {where} ORDER BY valid_to,id LIMIT ? OFFSET ?", (*args,limit,offset))]
        asset_total = db.execute(f"SELECT COUNT(*) FROM biomedical_assets WHERE {asset_where}", asset_args).fetchone()[0]
        assets = [dict(row) for row in db.execute(f"SELECT id,asset_tag,name,serial_number FROM biomedical_assets WHERE {asset_where} ORDER BY asset_tag,id LIMIT ? OFFSET ?", (*asset_args,asset_limit,asset_offset))]
        next_offset = asset_offset + asset_limit if asset_offset + len(assets) < asset_total else None
        audit(db, scope, "radiation_safety_list", "equipment_safety_register", "list", read=True, metadata={"returned": len(rows), "assets_returned": len(assets)})
        return {"records": rows, "total": count, "assets": assets, "asset_total": asset_total,
                "asset_offset": asset_offset, "asset_limit": asset_limit, "asset_next_offset": next_offset,
                "asset_catalog_truncated": next_offset is not None}


def create(*, actor: dict, values: dict):
    with db_session(immediate=True) as db:
        scope = _scope(db, actor, True)
        asset_id = values.get("asset_id")
        if type(asset_id) is not int or asset_id < 1:
            raise WorkflowError("asset_required", "Choose registered equipment.")
        asset = db.execute("SELECT id,asset_tag,name,serial_number,department,is_active FROM biomedical_assets WHERE id=?", (asset_id,)).fetchone()
        if asset is None or not asset["is_active"]:
            raise WorkflowError("not_found", "Active equipment not found.", 404)
        if scope.department is not None and asset["department"] != scope.department:
            raise WorkflowError("not_found", "Equipment not found.", 404)
        kind = values.get("record_kind")
        if kind not in {"licence", "officer"}:
            raise WorkflowError("invalid_kind", "Choose a licence or safety officer appointment.")
        first, last = _day(values.get("valid_from"), "Valid from"), _day(values.get("valid_to"), "Valid to")
        if last < first:
            raise WorkflowError("invalid_date_order", "Valid to must follow valid from.")
        officer = text(values.get("officer_name"), "Officer name", maximum=200) if kind == "officer" else None
        qualification = text(values.get("qualification_reference"), "Qualification reference", maximum=300) if kind == "officer" else None
        rid, stamp = str(uuid.uuid4()), now()
        try:
            db.execute("""INSERT INTO radiation_safety_records
                (id,facility_id,asset_id,asset_snapshot_json,record_kind,reference,authority,valid_from,valid_to,
                 officer_name,qualification_reference,scope_description,created_at,created_by,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (rid, scope.facility_id, asset_id, json.dumps(dict(asset)), kind,
                text(values.get("reference"), "Document reference", maximum=300), text(values.get("authority"), "Issuing authority", maximum=300),
                first.isoformat(), last.isoformat(), officer, qualification, text(values.get("scope_description"), "Recorded scope"), stamp, scope.actor_id, stamp))
        except DBIntegrityError as exc:
            raise WorkflowError("duplicate_reference", "This equipment document is already recorded.", 409) from exc
        row = db.execute("SELECT * FROM radiation_safety_records WHERE id=?", (rid,)).fetchone()
        _event(db, scope, row, "created", "Document evidence recorded")
        return _view(row)


def withdraw(record_id, *, actor, version, reason):
    with db_session(immediate=True) as db:
        scope = _scope(db, actor, True)
        row = _record(db, scope, record_id)
        if row["version"] != version or row["state"] != "recorded":
            raise WorkflowError("version_conflict", "This record changed. Reload before continuing.", 409)
        reason = text(reason, "Withdrawal reason")
        db.execute("UPDATE radiation_safety_records SET state='withdrawn',withdrawal_reason=?,version=version+1,updated_at=? WHERE id=?", (reason,now(),record_id))
        result = db.execute("SELECT * FROM radiation_safety_records WHERE id=?", (record_id,)).fetchone()
        _event(db, scope, result, "withdrawn", reason)
        return _view(result)


def history(record_id, *, actor):
    with db_session() as db:
        scope = _scope(db, actor)
        row = _record(db, scope, record_id)
        audit(db, scope, "radiation_safety_history", "equipment_safety_record", record_id, read=True)
        return {"record": _view(row), "events": [dict(r) for r in db.execute("SELECT action,reason,occurred_at,actor_id FROM radiation_safety_events WHERE record_id=? ORDER BY occurred_at,id", (record_id,))]}
