"""Local instrument custody and operator-recorded sterilization processing.

No sterilizer commands, recommended cycle parameters, inferred sterility,
clinical-use assertions, order completion or surgical gates are implemented.
"""
import json
from database import db_session
from db_backend import DBIntegrityError
from permissions import department_scope, CSSD_MANAGE, CSSD_READ, CSSD_WRITE, CSSD_RELEASE
from services import local_operations_common as c
from services.surgery_scope import surgery_in_scope

NOTICE = "Local processing and custody record. Release records an authorized operator's SOP review; Orb does not certify sterility or command equipment."


def _event(db, actor, action, note, *, pack_id=None, cycle_id=None, patient_id=None):
    db.execute("INSERT INTO cssd_events (id,pack_id,cycle_id,patient_id,action,note,actor_id,occurred_at) VALUES (?,?,?,?,?,?,?,?)",
               (c.new_id("CSE"), pack_id, cycle_id, patient_id, action, note, actor["id"], c.now()))


def _pack(db, ident, actor):
    row = c.scoped_on(db, "cssd_packs", ident, actor)
    # Custody history must not expose a linked case after a transfer or to a
    # different department. Admin-only initial grants do not replace scope.
    for item in db.execute("SELECT DISTINCT patient_id FROM cssd_issues WHERE pack_id=? AND patient_id IS NOT NULL", (ident,)):
        c.patient_on(db, item["patient_id"], actor)
    row["available_to_issue"] = row["status"] == "released" and bool(row["expires_at"]) and row["expires_at"] > c.now()
    return row


def create_set(data, *, actor, request_meta=None):
    code, name = c.text(data.get("code"), "Set code", 64).upper(), c.text(data.get("name"), "Set name", 200)
    items = data.get("items")
    if not isinstance(items, list) or not 1 <= len(items) <= 100:
        raise c.OperationsError("List 1–100 instrument types with their expected quantities.")
    if any(not isinstance(item, dict) for item in items):
        raise c.OperationsError("Each instrument needs a name and whole-number quantity.")
    clean = [(c.text(x.get("name"), "Instrument name", 200), c.number(x.get("quantity"), "Quantity", 1, 10000)) for x in items]
    if len({name.casefold() for name, _ in clean}) != len(clean):
        raise c.OperationsError("An instrument name occurs more than once.")
    with db_session(immediate=True) as db:
        actor = c.actor_on(db, actor, CSSD_MANAGE)
        ident = c.new_id("CSS")
        try:
            db.execute("INSERT INTO cssd_sets (id,department,code,name,created_by,created_at) VALUES (?,?,?,?,?,?)",
                       (ident, department_scope(actor), code, name, actor["id"], c.now()))
        except DBIntegrityError:
            raise c.OperationsError("This set code already exists.", "duplicate_code", 409) from None
        for item, qty in clean:
            db.execute("INSERT INTO cssd_set_items (id,set_id,name,quantity) VALUES (?,?,?,?)", (c.new_id("CSI"), ident, item, qty))
        c.audit_on(db, actor, "cssd", ident, "set_created", meta=request_meta)
        return c.scoped_on(db, "cssd_sets", ident, actor)


def create_pack(data, *, actor, request_meta=None):
    tag = c.text(data.get("tag"), "Physical pack tag", 100)
    with db_session(immediate=True) as db:
        actor = c.actor_on(db, actor, CSSD_WRITE)
        definition = c.scoped_on(db, "cssd_sets", c.text(data.get("set_id"), "Set", 100), actor)
        if not definition["active"]:
            raise c.OperationsError("This set definition is inactive.", "inactive_set", 409)
        ident, timestamp = c.new_id("CSP"), c.now()
        try:
            db.execute("INSERT INTO cssd_packs (id,department,set_id,tag,created_by,created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
                       (ident, department_scope(actor), definition["id"], tag, actor["id"], timestamp, timestamp))
        except DBIntegrityError:
            raise c.OperationsError("This pack tag already exists.", "duplicate_tag", 409) from None
        _event(db, actor, "received", "Physical pack registered.", pack_id=ident)
        c.audit_on(db, actor, "cssd", ident, "pack_created", meta=request_meta)
        return _pack(db, ident, actor)


def register(*, actor, limit=100, offset=0, request_meta=None):
    c.number(limit, "Limit", 1, 200)
    c.number(offset, "Offset", 0, 100000)
    with db_session(immediate=True) as db:
        actor = c.actor_on(db, actor, CSSD_READ)
        sql, args = c.scope_sql(actor)
        sets = [dict(r) for r in db.execute(f"SELECT * FROM cssd_sets WHERE {sql} ORDER BY code LIMIT ? OFFSET ?", (*args, limit + 1, offset))]
        for item in sets:
            item["items"] = [dict(r) for r in db.execute("SELECT * FROM cssd_set_items WHERE set_id=? ORDER BY name", (item["id"],))]
        visible, patient_args = c.patient_filter(actor, "i.patient_id")
        packs = [dict(row) for row in db.execute(
            f"SELECT * FROM cssd_packs WHERE {sql} AND NOT EXISTS "
            f"(SELECT 1 FROM cssd_issues i WHERE i.pack_id=cssd_packs.id AND NOT {visible}) "
            "ORDER BY created_at DESC,id LIMIT ? OFFSET ?", (*args, *patient_args, limit + 1, offset))]
        for row in packs:
            row["available_to_issue"] = row["status"] == "released" and bool(row["expires_at"]) and row["expires_at"] > c.now()
        cycles = [dict(row) for row in db.execute(
            f"SELECT * FROM cssd_cycles WHERE {sql} AND NOT EXISTS "
            "(SELECT 1 FROM cssd_cycle_packs cp JOIN cssd_issues i ON i.pack_id=cp.pack_id "
            f"WHERE cp.cycle_id=cssd_cycles.id AND NOT {visible}) "
            "ORDER BY created_at DESC,id LIMIT ? OFFSET ?", (*args, *patient_args, limit + 1, offset))]
        has_more = any(len(rows) > limit for rows in (sets, packs, cycles))
        sets, packs, cycles = sets[:limit], packs[:limit], cycles[:limit]
        c.audit_on(db, actor, "cssd", "register", "listed", meta=request_meta, read=True)
        return {"sets": sets, "packs": packs, "cycles": cycles, "limit": limit, "offset": offset, "notice": NOTICE, "has_more": has_more}


def pack_detail(ident, *, actor, request_meta=None):
    with db_session(immediate=True) as db:
        actor = c.actor_on(db, actor, CSSD_READ)
        row = _pack(db, ident, actor)
        row["issues"] = [dict(r) for r in db.execute("SELECT * FROM cssd_issues WHERE pack_id=? ORDER BY issued_at,id", (ident,))]
        row["events"] = [dict(r) for r in db.execute("SELECT * FROM cssd_events WHERE pack_id=? ORDER BY occurred_at,id", (ident,))]
        c.audit_on(db, actor, "cssd", ident, "pack_viewed", meta=request_meta, read=True)
        return row


def pack_action(ident, data, *, actor, request_meta=None):
    action = c.choice(data.get("action"), ("clean", "pack", "reprocess", "retire", "issue", "return"), "action")
    note = c.text(data.get("note"), "Processing / custody note", 4000)
    surgery_id = c.text(data.get("surgery_id"), "Surgery ID", 100, False)
    with db_session(immediate=True) as db:
        actor = c.actor_on(db, actor, CSSD_WRITE)
        row = _pack(db, ident, actor)
        c.revision(row, data.get("version"))
        patient_id = None
        allowed = {"clean": ("received",), "pack": ("cleaned",),
                   "reprocess": ("quarantined", "released"),
                   "retire": ("received", "cleaned", "packed", "quarantined", "released"),
                   "issue": ("released",), "return": ("issued", "recall_pending")}
        if row["status"] not in allowed[action]:
            raise c.OperationsError("That action is not available for this pack.", "invalid_transition", 409)
        if action == "issue":
            if not row["available_to_issue"]:
                raise c.OperationsError("The operator-recorded expiry has passed. Reprocess the pack.", "pack_expired", 409)
            destination = c.text(data.get("destination"), "Destination", 300)
            reference = c.text(data.get("reference"), "Custody / case reference", 300)
            if surgery_id:
                if not surgery_in_scope(db, actor, surgery_id):
                    raise c.OperationsError("Surgery not found.", "not_found", 404)
                surgery = db.execute("SELECT patient_id FROM surgeries WHERE id=?", (surgery_id,)).fetchone()
                if not surgery:
                    raise c.OperationsError("Surgery not found.", "not_found", 404)
                patient_id = c.patient_on(db, surgery["patient_id"], actor)
            db.execute("INSERT INTO cssd_issues (id,pack_id,surgery_id,patient_id,destination,reference,issued_by,issued_at) VALUES (?,?,?,?,?,?,?,?)",
                       (c.new_id("CSU"), ident, surgery_id, patient_id, destination, reference, actor["id"], c.now()))
            row["status"] = "issued"
        elif action == "return":
            issue = db.execute("SELECT * FROM cssd_issues WHERE pack_id=? AND returned_at IS NULL", (ident,)).fetchone()
            if not issue:
                raise c.OperationsError("No open custody issue exists.", "no_open_issue", 409)
            patient_id = issue["patient_id"]
            if patient_id:
                c.patient_on(db, patient_id, actor)
            db.execute("UPDATE cssd_issues SET returned_by=?,returned_at=?,return_note=? WHERE id=?",
                       (actor["id"], c.now(), note, issue["id"]))
            row["status"], row["cycle_id"], row["expires_at"] = "received", None, None
        else:
            row["status"] = {"clean": "cleaned", "pack": "packed", "retire": "retired", "reprocess": "received"}[action]
            if action == "reprocess":
                row["cycle_id"], row["expires_at"] = None, None
        old_cycle = db.execute("SELECT cycle_id FROM cssd_packs WHERE id=?", (ident,)).fetchone()[0]
        db.execute("UPDATE cssd_packs SET status=?,cycle_id=?,expires_at=?,updated_at=?,version=version+1 WHERE id=?",
                   (row["status"], row["cycle_id"], row["expires_at"], c.now(), ident))
        _event(db, actor, action, note, pack_id=ident, cycle_id=old_cycle, patient_id=patient_id)
        c.audit_on(db, actor, "cssd", ident, action, patient_id=patient_id, meta=request_meta)
        return _pack(db, ident, actor)


def start_cycle(data, *, actor, key, request_meta=None):
    payload = {field: c.text(data.get(field), label, 300) for field, label in (
        ("reference", "Cycle reference"), ("sterilizer_reference", "Sterilizer reference"),
        ("sop_reference", "Hospital SOP reference"), ("method", "Recorded method"))}
    packs = data.get("pack_ids")
    if not isinstance(packs, list) or not 1 <= len(packs) <= 100:
        raise c.OperationsError("Choose 1–100 packed sets.")
    payload["pack_ids"] = sorted(c.text(x, "Pack", 100) for x in packs)
    if len(set(payload["pack_ids"])) != len(packs):
        raise c.OperationsError("A pack occurs more than once.")
    key = c.text(key, "Idempotency-Key", 150)
    digest = c.fingerprint(payload)
    with db_session(immediate=True) as db:
        actor = c.actor_on(db, actor, CSSD_WRITE)
        old = c.replay_on(db, "cssd_cycles", actor, key, digest)
        if old:
            c.audit_on(db, actor, "cssd", old["id"], "cycle_replayed", meta=request_meta, read=True)
            return old
        for ident in payload["pack_ids"]:
            if _pack(db, ident, actor)["status"] != "packed":
                raise c.OperationsError("Every pack must be packed and outside another cycle.", "pack_unavailable", 409)
        ident, timestamp = c.new_id("CSC"), c.now()
        try:
            db.execute("""INSERT INTO cssd_cycles (id,department,reference,sterilizer_reference,sop_reference,method,
                started_at,created_by,created_at,updated_at,client_key,request_hash) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                       (ident, department_scope(actor), payload["reference"], payload["sterilizer_reference"], payload["sop_reference"],
                        payload["method"], timestamp, actor["id"], timestamp, timestamp, key, digest))
        except DBIntegrityError:
            raise c.OperationsError("This cycle reference already exists.", "duplicate_reference", 409) from None
        for pack in payload["pack_ids"]:
            db.execute("INSERT INTO cssd_cycle_packs (cycle_id,pack_id) VALUES (?,?)", (ident, pack))
            db.execute("UPDATE cssd_packs SET status='in_cycle',cycle_id=?,expires_at=NULL,updated_at=?,version=version+1 WHERE id=?",
                       (ident, timestamp, pack))
            _event(db, actor, "cycle_started", "Loaded against the recorded SOP.", pack_id=pack, cycle_id=ident)
        c.audit_on(db, actor, "cssd", ident, "cycle_started", meta=request_meta)
        return c.scoped_on(db, "cssd_cycles", ident, actor)


def cycle_detail(ident, *, actor, request_meta=None):
    with db_session(immediate=True) as db:
        actor = c.actor_on(db, actor, CSSD_READ)
        row = c.scoped_on(db, "cssd_cycles", ident, actor)
        row["packs"] = [_pack(db, item[0], actor) for item in db.execute("SELECT pack_id FROM cssd_cycle_packs WHERE cycle_id=?", (ident,))]
        row["events"] = [dict(r) for r in db.execute("SELECT * FROM cssd_events WHERE cycle_id=? ORDER BY occurred_at,id", (ident,))]
        for event in row["events"]:
            if event["patient_id"]:
                c.patient_on(db, event["patient_id"], actor)
        c.audit_on(db, actor, "cssd", ident, "cycle_viewed", meta=request_meta, read=True)
        return row


def cycle_action(ident, data, *, actor, request_meta=None):
    action = c.choice(data.get("action"), ("complete", "record_checks", "release", "recall"), "action")
    note = c.text(data.get("note"), "Operator's recorded review / reason", 4000)
    with db_session(immediate=True) as db:
        actor = c.actor_on(db, actor, CSSD_WRITE)
        if action in ("release", "recall"):
            c.permission_on(actor, CSSD_RELEASE)
        row = c.scoped_on(db, "cssd_cycles", ident, actor)
        c.revision(row, data.get("version"))
        packs = [_pack(db, item[0], actor) for item in db.execute("SELECT pack_id FROM cssd_cycle_packs WHERE cycle_id=?", (ident,))]
        if action in ("complete", "record_checks"):
            if row["status"] != ("running" if action == "complete" else "completed"):
                raise c.OperationsError("Checks can be recorded only on a running or completed cycle, before release.", "invalid_transition", 409)
            physical = c.choice(data.get("physical_result"), ("pass", "fail", "pending"), "physical result")
            chemical = c.choice(data.get("chemical_result"), ("pass", "fail", "pending"), "chemical result")
            biological = c.choice(data.get("biological_result"), ("pass", "fail", "pending", "not_required"), "biological result")
            if action == "record_checks":
                for field, value in (("physical_result", physical), ("chemical_result", chemical), ("biological_result", biological)):
                    if row[field] not in (None, "pending", value):
                        raise c.OperationsError("A final check cannot be silently replaced. Recall and reprocess the load.", "final_check", 409)
            basis = c.text(data.get("biological_basis"), "Documented biological-check basis", 1000, biological == "not_required")
            measurements = c.text(data.get("measurements"), "Recorded cycle measurements / log reference", 4000)
            status = "failed" if "fail" in (physical, chemical, biological) else "completed"
            db.execute("""UPDATE cssd_cycles SET status=?,completed_at=?,physical_result=?,chemical_result=?,biological_result=?,
                biological_basis=?,measurements=?,completion_note=?,updated_at=?,version=version+1 WHERE id=?""",
                       (status, row["completed_at"] or c.now(), physical, chemical, biological, basis, measurements, note, c.now(), ident))
            db.execute("UPDATE cssd_packs SET status='quarantined',updated_at=?,version=version+1 WHERE cycle_id=? AND status='in_cycle'", (c.now(), ident))
            note += "\nRecorded checks: " + json.dumps({"physical": physical, "chemical": chemical, "biological": biological,
                                                        "biological_basis": basis, "measurements": measurements}, ensure_ascii=False)
        elif action == "release":
            if row["status"] != "completed" or row["physical_result"] != "pass" or row["chemical_result"] != "pass" or row["biological_result"] not in ("pass", "not_required"):
                raise c.OperationsError("Release needs completed documented checks with no failed or pending result.", "checks_incomplete", 409)
            if row["biological_result"] == "not_required" and not row["biological_basis"]:
                raise c.OperationsError("Record the hospital SOP's biological-check basis.")
            expiry = c.instant(data.get("expires_at"), "Operator-recorded expiry", future=True)
            if expiry <= c.now():
                raise c.OperationsError("The operator-recorded expiry must be in the future.", "expired", 422)
            reference = c.text(data.get("release_reference"), "Release authorization / SOP review reference", 500)
            if not packs or any(p["status"] != "quarantined" or p["cycle_id"] != ident for p in packs):
                raise c.OperationsError("Every loaded pack must still be held in this cycle.", "pack_unavailable", 409)
            db.execute("UPDATE cssd_cycles SET status='released',released_by=?,released_at=?,release_reference=?,updated_at=?,version=version+1 WHERE id=?",
                       (actor["id"], c.now(), reference, c.now(), ident))
            db.execute("UPDATE cssd_packs SET status='released',expires_at=?,updated_at=?,version=version+1 WHERE cycle_id=?", (expiry, c.now(), ident))
        else:
            if row["status"] not in ("completed", "released", "failed"):
                raise c.OperationsError("This cycle cannot be recalled in its current state.", "invalid_transition", 409)
            db.execute("UPDATE cssd_cycles SET status='recalled',updated_at=?,version=version+1 WHERE id=?", (c.now(), ident))
            for pack in packs:
                if pack["cycle_id"] == ident and pack["status"] != "retired":
                    target = "recall_pending" if pack["status"] == "issued" else "quarantined"
                    db.execute("UPDATE cssd_packs SET status=?,updated_at=?,version=version+1 WHERE id=?", (target, c.now(), pack["id"]))
        _event(db, actor, action, note, cycle_id=ident)
        c.audit_on(db, actor, "cssd", ident, f"cycle_{action}", meta=request_meta)
        return c.scoped_on(db, "cssd_cycles", ident, actor)
