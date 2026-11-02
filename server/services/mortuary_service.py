"""Local mortuary custody. Records existing authority; never certifies death."""
from database import db_session
from db_backend import DBIntegrityError
from permissions import department_scope, has_permission, MORTUARY_RELEASE, MORTUARY_READ, MORTUARY_WRITE, MORTUARY_MANAGE
from services import local_operations_common as c

NOTICE = "Custody register only. Orb does not pronounce death, certify its cause, clear legal restrictions or notify an authority."


def _event(db, row, actor, action, note, previous_slot=None):
    db.execute("INSERT INTO mortuary_events (id,case_id,patient_id,action,from_slot_id,to_slot_id,note,actor_id,occurred_at) VALUES (?,?,?,?,?,?,?,?,?)",
               (c.new_id("MTE"), row["id"], row["patient_id"], action, previous_slot, row["slot_id"], note, actor["id"], c.now()))


def _slot(db, ident, actor, *, excluding=None):
    slot = c.scoped_on(db, "mortuary_slots", ident, actor)
    if not slot["active"]:
        raise c.OperationsError("This storage position is inactive.", "slot_inactive", 409)
    occupied = db.execute("SELECT id FROM mortuary_cases WHERE slot_id=? AND status <> 'released' AND id <> ?",
                          (ident, excluding or "")).fetchone()
    if occupied:
        raise c.OperationsError("This storage position is occupied.", "slot_occupied", 409)
    return slot


def add_slot(data, *, actor, request_meta=None):
    code = c.text(data.get("code"), "Position code", 64).upper()
    name = c.text(data.get("name"), "Position name", 200)
    with db_session(immediate=True) as db:
        actor = c.actor_on(db, actor, MORTUARY_MANAGE)
        ident = c.new_id("MTS")
        try:
            db.execute("INSERT INTO mortuary_slots (id,department,code,name,created_by,created_at) VALUES (?,?,?,?,?,?)",
                       (ident, department_scope(actor), code, name, actor["id"], c.now()))
        except DBIntegrityError:
            raise c.OperationsError("This position code already exists.", "duplicate_code", 409) from None
        c.audit_on(db, actor, "mortuary", ident, "slot_created", meta=request_meta)
        return c.scoped_on(db, "mortuary_slots", ident, actor)


def intake(data, *, actor, key, request_meta=None):
    payload = {field: c.text(data.get(field), label, size) for field, label, size in (
        ("body_tag", "Body tag", 100), ("identity_label", "Recorded identity / unidentified label", 300),
        ("authority_reference", "Existing death / custody authority reference", 500), ("slot_id", "Storage position", 100))}
    payload["patient_id"] = c.text(data.get("patient_id"), "Patient ID", 100, False)
    payload["received_at"] = c.instant(data["received_at"], "Received time") if data.get("received_at") else None
    key = c.text(key, "Idempotency-Key", 150)
    digest = c.fingerprint(payload)
    with db_session(immediate=True) as db:
        actor = c.actor_on(db, actor, MORTUARY_WRITE)
        if payload["patient_id"]:
            c.patient_on(db, payload["patient_id"], actor)
        old = c.replay_on(db, "mortuary_cases", actor, key, digest)
        if old:
            c.audit_on(db, actor, "mortuary", old["id"], "replayed", patient_id=old["patient_id"], meta=request_meta, read=True)
            return old
        _slot(db, payload["slot_id"], actor)
        ident, timestamp = c.new_id("MT"), c.now()
        try:
            db.execute("""INSERT INTO mortuary_cases (id,department,patient_id,body_tag,identity_label,authority_reference,
                received_at,slot_id,created_by,created_at,updated_at,client_key,request_hash) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                       (ident, department_scope(actor), payload["patient_id"], payload["body_tag"], payload["identity_label"],
                        payload["authority_reference"], payload["received_at"] or timestamp, payload["slot_id"],
                        actor["id"], timestamp, timestamp, key, digest))
        except DBIntegrityError:
            raise c.OperationsError("This body tag or storage position is already recorded.", "allocation_conflict", 409) from None
        row = c.scoped_on(db, "mortuary_cases", ident, actor)
        _event(db, row, actor, "received", "Received against the recorded authority reference.")
        c.audit_on(db, actor, "mortuary", ident, "received", patient_id=row["patient_id"], meta=request_meta)
        return row


def register(*, actor, limit=100, offset=0, request_meta=None):
    c.number(limit, "Limit", 1, 200)
    c.number(offset, "Offset", 0, 100000)
    with db_session(immediate=True) as db:
        actor = c.actor_on(db, actor, MORTUARY_READ)
        sql, args = c.scope_sql(actor)
        linked, linked_args = c.patient_filter(actor)
        cases = [dict(row) for row in db.execute(
            f"SELECT * FROM mortuary_cases WHERE {sql} AND {linked} ORDER BY received_at DESC,id LIMIT ? OFFSET ?",
            (*args, *linked_args, limit + 1, offset))]
        has_more = len(cases) > limit
        cases = cases[:limit]
        slots = [dict(r) for r in db.execute(f"SELECT * FROM mortuary_slots WHERE {sql} ORDER BY code", args)]
        # Occupancy exposes no identity outside the bounded register.
        occupied = {r[0] for r in db.execute("SELECT slot_id FROM mortuary_cases WHERE status <> 'released'")}
        for slot in slots:
            slot["occupied"] = slot["id"] in occupied
        c.audit_on(db, actor, "mortuary", "register", "listed", meta=request_meta, read=True)
        return {"cases": cases, "slots": slots, "limit": limit, "offset": offset, "notice": NOTICE, "has_more": has_more}


def detail(ident, *, actor, request_meta=None):
    with db_session(immediate=True) as db:
        actor = c.actor_on(db, actor, MORTUARY_READ)
        row = c.scoped_on(db, "mortuary_cases", ident, actor)
        row["events"] = [dict(r) for r in db.execute("SELECT * FROM mortuary_events WHERE case_id=? ORDER BY occurred_at,id", (ident,))]
        c.audit_on(db, actor, "mortuary", ident, "viewed", patient_id=row["patient_id"], meta=request_meta, read=True)
        return row


def act(ident, data, *, actor, request_meta=None):
    action = c.choice(data.get("action"), ("move", "hold", "clear_hold", "release"), "action")
    note = c.text(data.get("note"), "Reason", 4000)
    with db_session(immediate=True) as db:
        actor = c.actor_on(db, actor, MORTUARY_WRITE)
        if action in ("release", "clear_hold"):
            c.permission_on(actor, MORTUARY_RELEASE)
        row = c.scoped_on(db, "mortuary_cases", ident, actor)
        c.revision(row, data.get("version"))
        if row["status"] == "released":
            raise c.OperationsError("Handover is final. This custody record cannot be changed.", "invalid_transition", 409)
        previous_slot = row["slot_id"]
        if action == "move":
            target = c.text(data.get("slot_id"), "Storage position", 100)
            _slot(db, target, actor, excluding=ident)
            if target == previous_slot:
                raise c.OperationsError("Choose a different storage position.", "same_slot", 409)
            row["slot_id"] = target
        elif action == "hold":
            if row["status"] != "stored":
                raise c.OperationsError("This custody record is already held.", "invalid_transition", 409)
            row["status"], row["hold_reason"] = "held", note
        elif action == "clear_hold":
            if row["status"] != "held":
                raise c.OperationsError("There is no recorded hold to clear.", "invalid_transition", 409)
            reference = c.text(data.get("authority_reference"), "Hold clearance authority reference", 500)
            note += f"\nClearance reference: {reference}"
            row["status"], row["hold_reason"] = "stored", None
        else:
            if row["status"] == "held":
                raise c.OperationsError("Clear the recorded hold against an authority reference before handover.", "custody_held", 409)
            fields = {name: c.text(data.get(name), label, maximum) for name, label, maximum in (
                ("release_authority_reference", "Release authority reference", 500),
                ("recipient_name", "Recipient", 200), ("recipient_relationship", "Recipient relationship / authority", 200),
                ("identity_check_reference", "Identity check reference (not a full identity document)", 300),
                ("witness_id", "Witness account ID", 100))}
            witness = db.execute("SELECT id,role FROM users WHERE id=? AND is_active=1", (fields["witness_id"],)).fetchone()
            if not witness or witness["id"] == actor["id"] or not has_permission(witness["role"], MORTUARY_RELEASE):
                raise c.OperationsError("A different active account with mortuary release authority must witness handover.", "witness_required", 422)
            row["status"], row["slot_id"] = "released", None
        # Release content and lifecycle are one statement so the table CHECK
        # never sees a released timestamp on a still-occupied row.
        if action == "release":
            db.execute("""UPDATE mortuary_cases SET status='released',slot_id=NULL,hold_reason=NULL,
                release_authority_reference=?,recipient_name=?,recipient_relationship=?,identity_check_reference=?,
                witness_id=?,released_at=?,released_by=?,updated_at=?,version=version+1 WHERE id=?""",
                       (fields["release_authority_reference"], fields["recipient_name"], fields["recipient_relationship"],
                        fields["identity_check_reference"], fields["witness_id"], c.now(), actor["id"], c.now(), ident))
        else:
            db.execute("UPDATE mortuary_cases SET status=?,slot_id=?,hold_reason=?,updated_at=?,version=version+1 WHERE id=?",
                       (row["status"], row["slot_id"], row["hold_reason"], c.now(), ident))
        _event(db, row, actor, action, note, previous_slot)
        c.audit_on(db, actor, "mortuary", ident, action, patient_id=row["patient_id"], meta=request_meta)
        return c.scoped_on(db, "mortuary_cases", ident, actor)
