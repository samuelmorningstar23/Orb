"""Local staff-recorded feedback and complaints; no messages are transmitted."""
from database import db_session
from permissions import department_scope, FEEDBACK_MANAGE, FEEDBACK_READ, FEEDBACK_WRITE
from services import local_operations_common as c

STATUSES = ("recorded", "investigating", "resolved", "closed", "void")
NOTICE = "Recorded on this appliance only. No message is sent. Omitting a reporter name does not make the authenticated request untraceable."


def _event(db, row, actor, action, note, previous=None):
    db.execute("INSERT INTO feedback_events (id,case_id,patient_id,action,from_status,to_status,note,actor_id,occurred_at) VALUES (?,?,?,?,?,?,?,?,?)",
               (c.new_id("FBE"), row["id"], row["patient_id"], action, previous, row["status"], note, actor["id"], c.now()))


def register(data, *, actor, key, request_meta=None):
    payload = {
        "kind": c.choice(data.get("kind"), ("feedback", "complaint"), "type"),
        "channel": c.choice(data.get("channel"), ("in_person", "telephone", "written", "other"), "channel"),
        "subject": c.text(data.get("subject"), "Subject", 200),
        "statement": c.text(data.get("statement"), "Original statement", 10000),
        "reporter_name": c.text(data.get("reporter_name"), "Reporter name", 200, False),
        "contact_reference": c.text(data.get("contact_reference"), "Contact reference", 300, False),
        "patient_id": c.text(data.get("patient_id"), "Patient ID", 100, False),
        "rating": c.number(data["rating"], "Rating", 1, 5) if data.get("rating") is not None else None,
    }
    key = c.text(key, "Idempotency-Key", 150)
    digest = c.fingerprint(payload)
    with db_session(immediate=True) as db:
        actor = c.actor_on(db, actor, FEEDBACK_WRITE)
        if payload["patient_id"]:
            c.patient_on(db, payload["patient_id"], actor)
        old = c.replay_on(db, "feedback_cases", actor, key, digest)
        if old:
            c.audit_on(db, actor, "feedback", old["id"], "replayed", patient_id=old["patient_id"], meta=request_meta, read=True)
            return old
        ident, timestamp = c.new_id("FB"), c.now()
        db.execute("""INSERT INTO feedback_cases
            (id,department,patient_id,kind,channel,subject,statement,reporter_name,contact_reference,rating,
             created_by,created_at,updated_at,client_key,request_hash) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                   (ident, department_scope(actor), payload["patient_id"], payload["kind"], payload["channel"],
                    payload["subject"], payload["statement"], payload["reporter_name"], payload["contact_reference"],
                    payload["rating"], actor["id"], timestamp, timestamp, key, digest))
        row = c.scoped_on(db, "feedback_cases", ident, actor)
        _event(db, row, actor, "recorded", "Original statement recorded.")
        c.audit_on(db, actor, "feedback", ident, "recorded", patient_id=row["patient_id"], meta=request_meta)
        return row


def list_cases(*, actor, status=None, limit=100, offset=0, request_meta=None):
    c.number(limit, "Limit", 1, 200)
    c.number(offset, "Offset", 0, 100000)
    if status:
        c.choice(status, STATUSES, "status")
    with db_session(immediate=True) as db:
        actor = c.actor_on(db, actor, FEEDBACK_READ)
        sql, args = c.scope_sql(actor)
        linked, linked_args = c.patient_filter(actor)
        sql, args = sql + " AND " + linked, (*args, *linked_args)
        if status:
            sql, args = sql + " AND status = ?", (*args, status)
        rows = [dict(r) for r in db.execute(f"SELECT * FROM feedback_cases WHERE {sql} ORDER BY created_at DESC,id LIMIT ? OFFSET ?", (*args, limit + 1, offset))]
        has_more = len(rows) > limit
        rows = rows[:limit]
        c.audit_on(db, actor, "feedback", "register", "listed", meta=request_meta, read=True)
        return {"cases": rows, "limit": limit, "offset": offset, "notice": NOTICE, "has_more": has_more}


def detail(ident, *, actor, request_meta=None):
    with db_session(immediate=True) as db:
        actor = c.actor_on(db, actor, FEEDBACK_READ)
        row = c.scoped_on(db, "feedback_cases", ident, actor)
        row["events"] = [dict(r) for r in db.execute("SELECT * FROM feedback_events WHERE case_id = ? ORDER BY occurred_at,id", (ident,))]
        c.audit_on(db, actor, "feedback", ident, "viewed", patient_id=row["patient_id"], meta=request_meta, read=True)
        return row


def act(ident, data, *, actor, request_meta=None):
    action = c.choice(data.get("action"), ("note", "assign", "investigate", "resolve", "close", "reopen", "void"), "action")
    note = c.text(data.get("note"), "Response or reason", 10000)
    with db_session(immediate=True) as db:
        actor = c.actor_on(db, actor, FEEDBACK_MANAGE)
        row = c.scoped_on(db, "feedback_cases", ident, actor)
        c.revision(row, data.get("version"))
        previous = row["status"]
        if previous == "void" or (previous == "closed" and action != "reopen"):
            raise c.OperationsError("This case is closed. Reopen it with a reason before adding work.", "invalid_transition", 409)
        transitions = {"investigate": (("recorded",), "investigating"),
                       "resolve": (("recorded", "investigating"), "resolved"),
                       "close": (("resolved",), "closed"),
                       "reopen": (("resolved", "closed"), "investigating"),
                       "void": (("recorded", "investigating", "resolved"), "void")}
        if action in transitions:
            allowed, target = transitions[action]
            if previous not in allowed:
                raise c.OperationsError("That action is not available in this state.", "invalid_transition", 409)
            row["status"] = target
        if action == "resolve":
            row["resolution"] = note
        if action == "reopen":
            row["resolution"] = None
        if action == "assign":
            owner = c.text(data.get("owner_id"), "Owner", 100)
            user = db.execute("SELECT id,department FROM users WHERE id = ? AND is_active = 1", (owner,)).fetchone()
            if not user or (department_scope(actor) is not None and user["department"] != department_scope(actor)):
                raise c.OperationsError("Choose an active owner in scope.", "owner_not_found", 404)
            row["owner_id"] = owner
        db.execute("UPDATE feedback_cases SET status=?,resolution=?,owner_id=?,updated_at=?,version=version+1 WHERE id=?",
                   (row["status"], row["resolution"], row["owner_id"], c.now(), ident))
        _event(db, row, actor, action, note, previous)
        c.audit_on(db, actor, "feedback", ident, action, patient_id=row["patient_id"], meta=request_meta)
        return c.scoped_on(db, "feedback_cases", ident, actor)
