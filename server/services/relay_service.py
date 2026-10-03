"""Orb Relay — Messaging Service"""
import json
import logging
import sqlite3
from db_backend import DBIntegrityError
from database import get_db, db_session
from roles import ROLE_FEATURE

logger = logging.getLogger("orb.relay")


def get_channels_for_user(user_id: str, user_role: str) -> list:
    """Get channels accessible to this user, enriched with patient risk.

    **THIS IS RELAY'S ACCESS-CONTROL BOUNDARY**, not a sidebar query. Every read
    of message content — REST (routes/relay_routes.py) and websocket
    (ws/realtime.py:_user_can_access_channel) — authorises by asking whether the
    channel appears in this list. `can_access_channel` below is the named
    single-channel form of exactly this rule; there is deliberately no second
    implementation of "can X see channel Y" anywhere in the codebase.

    ONE RULE PER CHANNEL TYPE:
      * department — name matched against the user's department (admin: all).
      * case_room  — `admissions.department` vs the user's department; a room
                     whose department is not derivable stays visible (see the
                     branch comment), and discharged/orphaned rooms are dropped.
      * direct     — explicit rows in `channel_members` (migration 8). Nothing
                     is inferred, and there is no admin bypass.
    """
    # A ROLE FROM AN OPT-IN PART OF ORB WITHOUT relay.read GETS NO CHANNEL
    # (W1-A, 4 October 2026). The roles in roles.ROLE_FEATURE (receptionist,
    # ward clerk and the three lab roles) were ruled out of Relay (OPD-N3,
    # adt-17, LIS-R12) and hold no relay.read. The REST routes refuse them on
    # that permission, but the websocket's join_channel authorises only by
    # asking this function, and the case-room branch below hands every case
    # room in the hospital to an account with no department, which a lab role
    # always is. A ward clerk would also get its own ward's staff room. So the
    # rule sits here, at the one boundary every lane shares. If permissions.py
    # ever grants one of these roles relay.read, it passes with no second edit.
    #
    # Limited to ROLE_FEATURE roles on purpose, so every older role answers
    # exactly as before. cashier and pharmacist are also department-less with
    # no relay.read and still see case rooms here: closing that is a ruling
    # for Samuel, not a side effect of this change.
    if user_role in ROLE_FEATURE and not _relay_capable(user_role):
        return []
    with db_session() as db:
        # Get user department
        user_row = db.execute("SELECT department FROM users WHERE id = ?", (user_id,)).fetchone()
        user_dept = user_row["department"] if (user_row and user_row["department"]) else ""
        
        sql = """
            SELECT c.*, 
               p.mrn,
               p.diagnosis,
               p.ward,
               p.bed,
               p.status as patient_status,
               rs.score as risk_score,
               rs.level as risk_level,
               -- UNREAD = messages this user has not read AND did not write.
               -- `m.sender_id != ?` is the fix for a badge that counted a
               -- clinician's OWN posts against them until something else marked
               -- them read: you cannot have unread your own message. Combined
               -- with the soak-harness volume still in the demo database, that
               -- is how case rooms came to display 5,428 / 5,147 / 5,093
               -- unread — a number nobody believes, on the first Relay screen.
               -- read_by is a JSON array of user ids, so membership is an
               -- exact element match. NOT LIKE '%DOC1%' also matched 'DOC10'
               -- and 'DOC100', so their reads cleared DOC1's badge. Malformed
               -- or NULL read_by counts as unread, as mark_channel_read reads it.
               (SELECT COUNT(*) FROM messages m
                WHERE m.channel_id = c.id
                AND m.sender_id != ?
                AND NOT EXISTS (
                    SELECT 1 FROM json_each(
                        CASE WHEN json_valid(m.read_by) THEN m.read_by END) rb
                    WHERE rb.value = ?)) as unread_count,
               -- datetime() on every messages.timestamp read: the column holds
               -- SQLite's "2026-10-01 08:00:00" and ISO "2026-10-01T08:00:00+00:00"
               -- side by side, and as text 'T' sorts after ' ', so a seeded
               -- welcome line outranked a whole day of real traffic.
               (SELECT content FROM messages m2 
                WHERE m2.channel_id = c.id 
                ORDER BY datetime(m2.timestamp) DESC, m2.id DESC LIMIT 1) as last_message,
               (SELECT COUNT(*) FROM messages m3 
                WHERE m3.channel_id = c.id 
                AND m3.message_type = 'alert'
                AND datetime(m3.timestamp) > datetime('now', '-24 hours')) as alert_count,
               (SELECT MAX(datetime(timestamp)) FROM messages m4 
                WHERE m4.channel_id = c.id) as last_activity,
               (SELECT a.department FROM admissions a
                WHERE a.patient_id = c.patient_id AND a.status = 'active'
                LIMIT 1) as admission_department,
               -- EXPLICIT membership (channel_members, migration 8). This
               -- REPLACED an `is_participant` probe over `messages.sender_id`
               -- that nothing read: "has posted here" is not authorisation, it
               -- is a guess about authorisation, and it silently hides a DM you
               -- were added to but have not replied in.
               EXISTS(SELECT 1 FROM channel_members cm
                      WHERE cm.channel_id = c.id AND cm.user_id = ?) as is_member
        FROM channels c
        LEFT JOIN patients p ON c.patient_id = p.id
        -- The patient's latest risk row: timestamp first, id only to break an
        -- exact tie. MAX(id) alone is insertion order, so a back-charted obs
        -- set (older time, newer row) outranked the current score.
        LEFT JOIN risk_scores rs ON rs.id = (
            SELECT r2.id FROM risk_scores r2 WHERE r2.patient_id = c.patient_id
            ORDER BY r2.timestamp DESC, r2.id DESC LIMIT 1
        )
    """

        rows = db.execute(sql + """ ORDER BY 
            CASE WHEN rs.level = 'critical' THEN 0 
                 WHEN rs.level = 'elevated' THEN 1 
                 ELSE 2 END,
            last_activity DESC NULLS LAST,
            c.name""", (user_id, user_id, user_id)).fetchall()
        
        filtered = []
        for r in rows:
            # Filter orphaned channels: no patient AND no messages
            if r["type"] == "case_room" and not r["patient_id"] and not r["last_message"]:
                continue
            # Close the case room once the patient is discharged — a real workflow
            # archives the room rather than leaving a stale chatroom in Relay forever.
            if r["type"] == "case_room" and r["patient_status"] == "discharged":
                continue
            if r["type"] == "department":
                ch_name = r["name"].lower()
                u_dept = user_dept.lower()
                # ── ADMIN SAW NONE OF THESE — REGRESSION, FIXED 2026-09-07 ──
                # This branch used to read `if type == "department" and
                # user_role != "admin"`, which for an ADMIN made the whole
                # condition False. That was correct while the chain still ended
                # in the bare `else: filtered.append(r)` the leak comment below
                # describes: a department row fell through and was appended, so
                # exempting admins from the filter WAS how admins got every
                # room.
                #
                # The 2026-08-05 PHI fix replaced that bare else with typed
                # branches and a fail-closed default. It closed the case-room and
                # DM leaks correctly, and it silently took the admin's department
                # rooms with it: a department row now matched no later branch and
                # landed in the fail-closed else, which has been logging
                # "Relay channel dept-cardiology has unknown type 'department'
                # — withheld" on every admin channel list ever since. That else
                # calls itself UNREACHABLE; it was reachable, by a VALID type,
                # for a valid role.
                #
                # Department rows are now decided entirely inside this branch and
                # depend on no fall-through at all, which is the actual lesson of
                # the 2026-08-05 fix.
                #
                # KEEP THIS CLAUSE. RULED 2026-09-07 (Samuel), after it was put to
                # him explicitly, because the DM branch ~60 lines below refuses
                # the SAME shape of access in strong terms ("a hospital
                # administrator silently reading staff DMs is the exact abuse
                # this table exists to prevent") and the two read as
                # contradictory at a glance. They are not. The line between them
                # is what the room IS, not who is asking:
                #
                #   department room — the ward's organisational channel. Ward
                #     data. A governance account oversees every ward, which is
                #     the same call permissions.department_scope makes and the
                #     case-room branch below already honours.
                #   direct message  — two named people talking. Not a ward
                #     artefact, so no department scope reaches it and an
                #     administrator must be ADDED to the thread, on the record.
                #
                # So: do not "fix" this to match the DM branch. If the ruling is
                # ever revisited, deleting these two lines is the whole change —
                # ADM001 carries no department and the blank-department rule
                # below would then catch it.
                if user_role == "admin":
                    filtered.append(r)
                # NO DEPARTMENT MEANS NO DEPARTMENT ROOM (2026-09-07).
                # This branch matches by SUBSTRING, and the empty string is a
                # substring of everything: `"" in "cardiology"` is True, so a
                # department-less account was handed EVERY department channel in
                # the hospital. It went unnoticed because the only seeded
                # department-less account was ADM001, which the admin clause
                # above catches before it ever reaches this test. Making PHA001
                # department-less — the fix for the pharmacist being scoped to a
                # ward with no patients — would have been the first account to
                # walk into it.
                #
                # A hospital-wide account (pharmacy, finance, governance) belongs
                # to no ward, so it gets no ward's staff room. That is NOT the
                # same rule as the case-room branch below, which deliberately
                # shows an unscoped account everything: a case room is patient
                # data a hospital-wide function has a reason to see, and a
                # department's staff room is not. An ADMIN is the exception
                # handled above: that is a governance mandate over ward data, not
                # a derived ward scope, and it is the same call the case-room
                # branch below already makes.
                elif not u_dept:
                    pass
                elif u_dept == "surgery" and ch_name == "surgery":
                    filtered.append(r)
                elif u_dept == "anesthesiology" and ch_name == "surgery":
                    filtered.append(r)
                elif u_dept in ch_name or ch_name in u_dept:
                    filtered.append(r)

            # ── PHI LEAK, FIXED 2026-08-05 ──────────────────────────────────
            # Everything that was not a `department` channel fell to a bare
            # `else: filtered.append(r)`, and this query has NO WHERE clause on
            # `channels` — the one bound parameter feeds the unread-count
            # subquery, not access control. So every CASE ROOM and every DIRECT
            # MESSAGE in the hospital was returned to any holder of relay.read.
            #
            # That is not only a list leak. routes/relay_routes.py:82 and :93
            # authorise message reads with
            #     if not any(c["id"] == channel_id for c in get_channels_for_user(...))
            # so this function IS the access-control boundary for Relay message
            # content. Any authenticated clinician could read any patient's case
            # room and any two colleagues' private conversation.
            #
            # Same root cause as the ward-brief leak fixed earlier today: scoping
            # was applied to the surface that looked like it selected something
            # (department channels) and not to the ones that merely listed.
            elif r["type"] == "case_room":
                # Scope on admissions.department — the column every other scoped
                # surface matches (vigil_routes.py:279, nurse_routes.py). NOT
                # patients.ward: scoping on the wrong column is how a scope
                # silently stops scoping.
                if not user_dept or user_role == "admin":
                    # No department on the user row means a genuinely unscoped
                    # governance account — permissions.department_scope's
                    # documented convention. It does NOT mean "scope unknown".
                    filtered.append(r)
                else:
                    # SCOPE WHERE THE DEPARTMENT IS KNOWABLE. A first version of
                    # this dropped every room with no active admission, which is
                    # the wrong blunt instrument: `patients` has no department
                    # column, so a patient between admissions has no derivable
                    # department and would vanish from the ward that is actually
                    # looking after them.
                    #
                    # The leak being closed is cross-department rooms whose
                    # department IS known and DIFFERENT. Where it is genuinely
                    # underivable the room is still listed — that is the
                    # pre-existing behaviour, deliberately not widened and not
                    # narrowed here, and it is exactly why a channel_members
                    # table is the real fix rather than more inference.
                    # NOT `ward` — a first attempt fell back to it and broke 20+
                    # tests, because ward is a BED PREFIX ("GM-1") and department
                    # is a name ("General Medicine"). They never compare equal, so
                    # every room silently vanished. admissions.department is the
                    # only column that carries the scope, which is precisely why
                    # every other scoped surface matches on it.
                    room_dept = r["admission_department"] or ""
                    if not room_dept or room_dept == user_dept:
                        filtered.append(r)

            elif r["type"] == "direct":
                # ── DM ACCESS CONTROL — closed 2026-08-05 (migration 8) ────────
                # A direct channel is visible to its EXPLICIT MEMBERS and to
                # nobody else. Not "whoever has posted" (that hides a DM you were
                # added to but never replied in — inference is not authorisation),
                # and not "whoever shares a department" (a DM is not a ward
                # artefact).
                #
                # NO ADMIN BYPASS, and that is deliberate. `admin` is unscoped for
                # DEPARTMENT scoping — the documented convention in
                # permissions.department_scope, and the case-room branch above
                # honours it — because a governance account legitimately oversees
                # every ward. Membership is not a department scope: two
                # clinicians' private conversation is not ward data, and a
                # hospital administrator silently reading staff DMs is the exact
                # abuse this table exists to prevent. An administrator who needs a
                # thread must be ADDED to it, on the record, like anyone else.
                if r["is_member"]:
                    filtered.append(r)

            else:
                # UNREACHABLE while channels.type keeps its CHECK constraint
                # (database.py:120) — every permitted value is handled above.
                # Fail CLOSED rather than falling through to append: this
                # function is the read boundary for message content, and a
                # default that grants access is how the case-room and DM leaks
                # both happened. A rogue type is a schema bug; it must surface as
                # a missing channel and a warning, never as an unscoped grant.
                logger.warning(
                    "Relay channel %s has unknown type %r — withheld (fail-closed)",
                    r["id"], r["type"],
                )

        return [
            {
                "id": r["id"],
                "name": r["name"],
                "type": r["type"],
                "patient_id": r["patient_id"],
                "mrn": r["mrn"],
                "diagnosis": r["diagnosis"],
                "ward": r["ward"],
                "bed": r["bed"],
                "risk_score": r["risk_score"],
                "risk_level": r["risk_level"],
                "unread_count": r["unread_count"] or 0,
                "last_message": r["last_message"],
                "last_activity": r["last_activity"],
                "alert_count": r["alert_count"] or 0,
                "has_alert": (r["alert_count"] or 0) > 0,
            }
            for r in filtered
        ]


# ══════════════════════════════════════════════════════════════════════════════
#  THE BOUNDARY, NAMED — and channel membership
# ══════════════════════════════════════════════════════════════════════════════
# `get_channels_for_user` was already the de-facto access-control boundary for
# Relay message content, expressed at ~12 call sites as the copy-pasted idiom
#
#     if not any(c["id"] == channel_id for c in get_channels_for_user(...)):
#
# `can_access_channel` gives that idiom a name so the boundary is greppable and
# auditable. It is deliberately DEFINED IN TERMS OF the list query rather than
# reimplemented as a single-channel SELECT: a second implementation is a second
# rule, and two rules that are supposed to agree eventually don't. The cost is
# one list query per authorisation, which is what the call sites already paid.


class ChannelAccessError(RuntimeError):
    """A membership change was refused.

    ``status`` is the HTTP status the route should surface. It exists so the
    service can distinguish "no such channel" from "you are not in it" without
    the routes re-deriving the reason — and without the service importing
    FastAPI.
    """

    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


def can_access_channel(user_id: str, user_role: str, channel_id: str) -> bool:
    """Is this user authorised for this channel? THE single access rule.

    Same answer as `get_channels_for_user` by construction, for every channel
    type, forever. Use this for authorisation; use `get_channels_for_user` when
    the caller genuinely needs the whole list (e.g. the wrong-room detector,
    which reuses the list as the sender's PHI boundary).
    """
    if not user_id or not channel_id:
        return False
    return any(c["id"] == channel_id for c in get_channels_for_user(user_id, user_role))


def _relay_capable(role: str) -> bool:
    """Can this role use Relay at all? Gate on the PERMISSION, not a role list —
    a role added to ROLE_PERMISSIONS with relay.read should work here without a
    second edit, and `patient` (Bridge-only) must never be addable to a DM.
    """
    try:
        from permissions import has_permission, RELAY_READ
        return has_permission(role or "", RELAY_READ)
    except Exception:  # pragma: no cover — permissions is always importable
        logger.warning("relay: permission lookup failed for role %r", role, exc_info=True)
        return False


def list_channel_members(channel_id: str) -> list:
    """Members of a channel, oldest first. NOT access-controlled on its own —
    callers must check `can_access_channel` first (the route does)."""
    with db_session() as db:
        rows = db.execute(
            """SELECT cm.user_id, cm.added_at, cm.added_by, cm.source,
                      u.name AS user_name, u.role AS user_role
                 FROM channel_members cm
                 LEFT JOIN users u ON u.id = cm.user_id
                WHERE cm.channel_id = ?
                ORDER BY cm.added_at ASC, cm.user_id ASC""",
            (channel_id,),
        ).fetchall()
        return [
            {
                "user_id": r["user_id"],
                "name": r["user_name"],
                "role": r["user_role"],
                "added_at": r["added_at"],
                "added_by": r["added_by"],
                "source": r["source"],
            }
            for r in rows
        ]


def _audit_membership(db, *, action: str, actor: dict, channel_id: str,
                      target_user_id: str, metadata: dict) -> None:
    """Seal a membership change onto the tamper-evident chain.

    event_category='security' is FAIL-CLOSED (audit_service._FAIL_CLOSED_
    CATEGORIES): if the audit row cannot be written, log_audit_event raises and
    — because this runs inside the caller's `db_session` — the membership INSERT
    rolls back with it. An unauditable grant of access to a private conversation
    must not happen at all. This is the same posture as the other access-granting
    paths in the codebase, and the reason the audit shares the transaction rather
    than being fired afterwards.
    """
    from services.audit_service import log_audit_event
    import uuid as _uuid

    log_audit_event(
        correlation_id=str(_uuid.uuid4()),
        event_category="security",
        action_type=action,
        outcome="success",
        db=db,
        actor_user_id=actor.get("id"),
        actor_role=actor.get("role"),
        target_type="channel",
        target_id=channel_id,
        summary_message=f"{action}: user {target_user_id} on channel {channel_id}",
        metadata={"channel_id": channel_id, "member_user_id": target_user_id, **metadata},
    )


def add_channel_member(channel_id: str, actor: dict, new_user_id: str) -> dict:
    """Add a user to a DIRECT channel. Members-only, audited.

    WHO MAY ADD: whoever is already a member of that conversation, and nobody
    else. Not an administrator, not a department colleague, not the holder of any
    permission — a private thread's guest list belongs to the people in it. This
    is the ONLY way `channel_members` grows for an existing DM, which is what
    makes the backfill's fail-closed gap repairable.

    DIRECT ONLY: case rooms and department channels derive access from
    department (see migration 8's docstring). Returning 400 rather than quietly
    inserting a row keeps one rule per channel type — a members row on a case
    room would be a second, disagreeing grant that nothing reads.

    Idempotent: re-adding an existing member is a no-op that reports
    ``already_member`` and writes no second audit row.
    """
    actor_id = (actor or {}).get("id")
    if not actor_id:
        raise ChannelAccessError(403, "Not authorized")
    if not new_user_id:
        raise ChannelAccessError(400, "user_id is required")

    with db_session() as db:
        ch = db.execute(
            "SELECT id, name, type FROM channels WHERE id = ?", (channel_id,)
        ).fetchone()
        if not ch:
            raise ChannelAccessError(404, "Channel not found")
        if ch["type"] != "direct":
            raise ChannelAccessError(
                400,
                "Membership applies to direct messages only; case rooms and "
                "department channels are scoped by department.",
            )

        # THE AUTHORISATION. Read straight from channel_members — deliberately
        # NOT via can_access_channel: that answers "may I read this?", and for a
        # direct channel the two happen to coincide today, but the question here
        # is the narrower "am I in this conversation?" and it must not start
        # inheriting any future visibility rule.
        if not db.execute(
            "SELECT 1 FROM channel_members WHERE channel_id = ? AND user_id = ?",
            (channel_id, actor_id),
        ).fetchone():
            raise ChannelAccessError(
                403, "Only a member of this conversation may add someone to it"
            )

        target = db.execute(
            "SELECT id, name, role FROM users WHERE id = ?", (new_user_id,)
        ).fetchone()
        if not target:
            raise ChannelAccessError(404, "User not found")
        if not _relay_capable(target["role"]):
            raise ChannelAccessError(400, "That user does not have access to Relay")

        if db.execute(
            "SELECT 1 FROM channel_members WHERE channel_id = ? AND user_id = ?",
            (channel_id, new_user_id),
        ).fetchone():
            return {"status": "already_member", "channel_id": channel_id,
                    "user_id": new_user_id}

        try:
            db.execute(
                """INSERT INTO channel_members (channel_id, user_id, added_by, source)
                   VALUES (?, ?, ?, 'added')""",
                (channel_id, new_user_id, actor_id),
            )
        except DBIntegrityError:
            # Two adds of the same person raced past the SELECT above (the four
            # run_db workers execute route bodies in parallel and a SELECT holds
            # no snapshot). The PRIMARY KEY is the compare-and-swap; the loser
            # reports the same outcome as a repeat add rather than a 500.
            return {"status": "already_member", "channel_id": channel_id,
                    "user_id": new_user_id}

        _audit_membership(
            db, action="relay.channel_member_added", actor=actor,
            channel_id=channel_id, target_user_id=new_user_id,
            metadata={"channel_type": "direct", "member_name": target["name"],
                      "member_role": target["role"], "source": "added"},
        )

    return {
        "status": "ok",
        "channel_id": channel_id,
        "user_id": new_user_id,
        "name": target["name"],
        "role": target["role"],
    }


def create_direct_channel(creator: dict, participant_ids: list, name: str | None = None) -> dict:
    """Open a direct conversation with the creator as its first member.

    WHY THIS EXISTS: `add_channel_member` requires an existing member, so
    without a creation path that seeds one, no new DM could ever have a member
    and the table could only shrink access. Creation is the bootstrap.

    Re-opening an existing conversation returns it instead of minting a second
    channel with the same people — otherwise every "message Dr X" tap forks a
    new empty thread and the history scatters.
    """
    creator_id = (creator or {}).get("id")
    if not creator_id:
        raise ChannelAccessError(403, "Not authorized")

    wanted = [u for u in dict.fromkeys(participant_ids or []) if u and u != creator_id]
    if not wanted:
        raise ChannelAccessError(400, "At least one other participant is required")

    with db_session() as db:
        rows = db.execute(
            f"SELECT id, name, role FROM users WHERE id IN ({','.join('?' * len(wanted))})",
            tuple(wanted),
        ).fetchall()
        found = {r["id"]: r for r in rows}
        missing = [u for u in wanted if u not in found]
        if missing:
            raise ChannelAccessError(404, f"Unknown user(s): {', '.join(missing)}")
        for uid in wanted:
            if not _relay_capable(found[uid]["role"]):
                raise ChannelAccessError(
                    400, f"{found[uid]['name']} does not have access to Relay")

        members = [creator_id] + wanted

        # Existing conversation with EXACTLY this set of people? A simultaneous
        # double-tap can still mint two threads — SQLite cannot express "unique
        # member set" as a constraint — but that failure is a duplicate empty
        # conversation, visible and harmless. It is NOT an access failure: each
        # thread is created with its own correct member rows.
        existing = db.execute(
            f"""SELECT c.id FROM channels c
                  JOIN channel_members m ON m.channel_id = c.id
                 WHERE c.type = 'direct'
                 GROUP BY c.id
                HAVING COUNT(*) = ?
                   AND SUM(CASE WHEN m.user_id IN ({','.join('?' * len(members))})
                                THEN 1 ELSE 0 END) = ?""",
            (len(members), *members, len(members)),
        ).fetchone()
        if existing:
            return {"status": "existing", "id": existing["id"], "type": "direct"}

        import uuid as _uuid
        channel_id = f"DM-{_uuid.uuid4().hex[:12].upper()}"
        creator_row = db.execute(
            "SELECT name FROM users WHERE id = ?", (creator_id,)
        ).fetchone()
        creator_name = creator_row["name"] if creator_row else creator_id
        channel_name = name or ", ".join(
            [creator_name] + [found[u]["name"] for u in wanted]
        )

        db.execute(
            "INSERT INTO channels (id, name, type) VALUES (?, ?, 'direct')",
            (channel_id, channel_name),
        )
        db.execute(
            "INSERT INTO channel_members (channel_id, user_id, added_by, source) "
            "VALUES (?, ?, ?, 'creator')",
            (channel_id, creator_id, creator_id),
        )
        for uid in wanted:
            db.execute(
                "INSERT INTO channel_members (channel_id, user_id, added_by, source) "
                "VALUES (?, ?, ?, 'added')",
                (channel_id, uid, creator_id),
            )
            _audit_membership(
                db, action="relay.channel_member_added", actor=creator,
                channel_id=channel_id, target_user_id=uid,
                metadata={"channel_type": "direct", "member_name": found[uid]["name"],
                          "member_role": found[uid]["role"], "source": "added",
                          "at_creation": True},
            )

    return {"status": "created", "id": channel_id, "name": channel_name,
            "type": "direct", "members": members}


def get_user_alerts(user_id: str, user_role: str) -> list:
    """Get active alerts specifically targeted to this user.

    THE SECOND DOOR OUT OF A DM, closed 2026-08-05. This is a message-read path
    that does NOT go through `get_channels_for_user`: it scans every alert row in
    the hospital and selects on the alert's own targeting payload, and for
    `role == 'admin'` it selects unconditionally. Nothing stops a clinician
    posting `message_type: 'alert'` with a structured body into a DM
    (models/message.py takes message_type from the request), so a private
    conversation could have been read out through this endpoint even after the
    channel list was fixed.

    The DIRECT-channel clause below applies the SAME membership rule as
    `get_channels_for_user` — it is not a second rule, and deliberately not a
    broader one: case rooms and department channels are untouched, so no
    clinical alert delivery narrows. An alert whose channel row is missing
    entirely (`c.type IS NULL`) also stays, for the same reason.
    """
    with db_session() as db:
        user_row = db.execute("SELECT department FROM users WHERE id = ?", (user_id,)).fetchone()
        user_dept = user_row["department"] if (user_row and user_row["department"]) else ""

        # Get all recent alert messages (last 48 hours)
        rows = db.execute(
            """SELECT m.* FROM messages m
               LEFT JOIN channels c ON c.id = m.channel_id
               WHERE m.message_type = 'alert'
               AND datetime(m.timestamp) > datetime('now', '-48 hours')
               AND (c.type IS NULL OR c.type != 'direct'
                    OR EXISTS (SELECT 1 FROM channel_members cm
                               WHERE cm.channel_id = m.channel_id AND cm.user_id = ?))
               ORDER BY datetime(m.timestamp) DESC, m.id DESC""",
            (user_id,),
        ).fetchall()
        
        user_alerts = []
        for row in rows:
            try:
                payload = json.loads(row["content"])
                if not payload.get("is_structured"):
                    continue
                if payload.get("status") in ["resolved", "escalated"]:
                    continue
                    
                is_target = False
                
                # Check explicit assignment
                if user_id in payload.get("target_users", []):
                    is_target = True
                # Check role / department fallback
                elif user_role in payload.get("target_roles", []) and user_dept in payload.get("target_departments", []):
                    is_target = True
                elif user_role == "admin":
                    is_target = True
                    
                if is_target:
                    user_alerts.append(_message_row_to_dict(row))
            except Exception as e:
                # Skip non-JSON / malformed alert rows, but log so a systematic
                # parsing problem doesn't silently hide alerts from a clinician.
                logger.debug("Skipping unparseable alert row %s: %s", row["id"], e)

        return user_alerts


def get_channel_messages(channel_id: str, limit: int = 500, offset: int = 0) -> list:
    """Get messages for a channel."""
    with db_session() as db:
        # Instant order, not text order (see get_channels_for_user), with the
        # id to settle messages stamped in the same second.
        rows = db.execute(
            """SELECT * FROM (
                   SELECT * FROM messages 
                   WHERE channel_id = ? 
                   ORDER BY datetime(timestamp) DESC, id DESC 
                   LIMIT ? OFFSET ?
               ) sub ORDER BY datetime(timestamp) ASC, id ASC""",
            (channel_id, limit, offset),
        ).fetchall()

        # The one serializer every Relay message body goes through, so history
        # and live sends carry the same fields (is_bot and source included;
        # older rows that predate those columns get the safe defaults).
        return [_message_row_to_dict(r) for r in rows]


# ══════════════════════════════════════════════════════════════════════════════
#  SHADOW INSTRUMENT — the alert-engagement producers
# ══════════════════════════════════════════════════════════════════════════════
# WHY THIS EXISTS. `shadow_service.sweep_once` marks an `alert.persisted` as
# `alert.expired` unless an `alert.viewed` / `alert.acknowledged` row exists for
# the same (episode, message). Nothing wrote those two kinds, so the moment the
# sweeper was registered it would have marked 100% of alerts expired and the
# engagement figure would have read a STRUCTURAL zero — a number produced by
# missing code rather than by clinician behaviour, which is worse than no number.
#
# The lookup below is the join Orb otherwise does not have: `messages` carries no
# episode_id, so the ONLY way back from a message to its episode is the
# `alert.persisted` row that Vigil/ingestion wrote when the alert was delivered.
# An alert with no such row (posted before the instrument existed, or dropped by
# a fail-open emit) resolves to nothing and is simply not counted — engagement
# under-counts rather than inventing an episode to hang the row on.


def shadow_alert_episodes(db, message_ids) -> dict:
    """message_id -> {episode_id, lane, patient_id, ward, persisted_at} for the
    ids that are known alerts in the shadow log. Never raises: an unavailable
    instrument returns {} and the caller carries on.

    The LANE IS INHERITED from the `alert.persisted` row, never assumed to be a
    clinician lane. A simulated alert that someone happens to open in the demo
    ward must stay lane='simulator' end to end — the scorecard excludes that lane
    from every metric, and a chat-lane view row hanging off a simulator-lane
    alert would smuggle synthetic traffic into the clinical-event count on the
    face of the instrument-health panel.
    """
    ids = [int(m) for m in message_ids if m is not None]
    if not ids:
        return {}
    out: dict = {}
    try:
        placeholders = ",".join("?" for _ in ids)
        rows = db.execute(
            f"""SELECT episode_id, subject_id, lane, patient_id, ward, occurred_at
                  FROM shadow_events_current
                 WHERE kind = 'alert.persisted' AND subject_type = 'messages'
                   AND subject_id IN ({placeholders})""",
            tuple(str(i) for i in ids),
        ).fetchall()
        for row in rows:
            try:
                key = int(row["subject_id"])
            except (TypeError, ValueError):
                continue
            out[key] = {
                "episode_id": row["episode_id"],
                "lane": row["lane"],
                "patient_id": row["patient_id"],
                "ward": row["ward"],
                "persisted_at": row["occurred_at"],
            }
    except Exception:
        logger.debug("shadow: alert episode lookup failed", exc_info=True)
        return {}
    return out


def _seconds_since(persisted_at, now_iso) -> float | None:
    from datetime import datetime
    try:
        start = datetime.fromisoformat(persisted_at)
        end = datetime.fromisoformat(now_iso)
        return round((end - start).total_seconds(), 1)
    except (TypeError, ValueError):
        return None


def mark_channel_read(channel_id: str, user_id: str) -> int:
    """Mark all messages in a channel as read by this user.

    ``messages.read_by`` is a JSON array of user ids. The unread badge in
    ``get_channels_for_user`` counts messages whose ``read_by`` does not contain
    the requesting user's id — but nothing ever wrote to ``read_by``, so the
    badge never cleared. This appends the user id to every message in the channel
    that doesn't already include it. Returns the number of messages updated.

    SHADOW: the messages this call transitions from unread to read are the ONLY
    evidence Orb has that a human actually opened an alert. Emitted for the
    NEWLY-read alerts only (the loop already skips ones this user had read), so a
    second call is a no-op rather than a second viewer.
    """
    updated = 0
    newly_read: list[int] = []
    with db_session() as db:
        rows = db.execute(
            "SELECT id, read_by FROM messages WHERE channel_id = ?",
            (channel_id,),
        ).fetchall()
        for r in rows:
            try:
                read_by = json.loads(r["read_by"]) if r["read_by"] else []
            except (TypeError, ValueError):
                read_by = []
            if user_id in read_by:
                continue
            read_by.append(user_id)
            db.execute(
                "UPDATE messages SET read_by = ? WHERE id = ?",
                (json.dumps(read_by), r["id"]),
            )
            newly_read.append(r["id"])
            updated += 1

        # SHADOW on THIS transaction (db=db). This function holds an open write
        # transaction by the time it gets here; a fresh connection would block on
        # the WAL lock it holds until busy_timeout — the self-deadlock documented
        # at the top of shadow_service.
        _emit_alert_viewed(db, newly_read, channel_id, user_id)
    return updated


def _emit_alert_viewed(db, message_ids, channel_id: str, user_id: str) -> None:
    if not message_ids:
        return
    try:
        from services import shadow_service
        known = shadow_alert_episodes(db, message_ids)
        if not known:
            return
        now = shadow_service.utc_now()
        events = [
            shadow_service.build(
                "alert.viewed", info["episode_id"], info["lane"],
                occurred_at=now, patient_id=info["patient_id"], ward=info["ward"],
                actor_user_id=user_id,
                subject_type="messages", subject_id=message_id,
                payload={
                    "message_id": message_id,
                    "channel_id": channel_id,
                    # The instrument's headline engagement number: how long an
                    # alert sat in the room before a human opened it.
                    "seconds_since_persisted": _seconds_since(info["persisted_at"], now),
                },
            )
            for message_id, info in known.items()
        ]
        shadow_service.emit_many(events, db=db)
    except Exception:
        # Rule 1: reading a message must never fail because the log did.
        logger.debug("shadow: alert.viewed emit failed", exc_info=True)


def _message_row_to_dict(row, *, is_bot: bool = False, source: str = "clinician") -> dict:
    return {
        "id": row["id"],
        "channel_id": row["channel_id"],
        "sender_id": row["sender_id"],
        "sender_name": row["sender_name"],
        "sender_role": row["sender_role"],
        "content": row["content"],
        "message_type": row["message_type"],
        "timestamp": row["timestamp"],
        "is_bot": bool(row["is_bot"]) if "is_bot" in row.keys() else is_bot,
        "source": row["source"] if "source" in row.keys() else source,
    }


def send_message(channel_id: str, sender_id: str, sender_name: str,
                 sender_role: str, content: str, message_type: str = "text",
                 is_bot: bool = False, source: str = "clinician",
                 idempotency_key: str | None = None) -> dict:
    """Send a message to a channel.

    ``is_bot``/``source`` tag automated posts (simulation bots, AI agents) so the
    Relay UI can badge them distinctly from real clinician messages — the same
    provenance principle applied to vitals and medications.

    ``idempotency_key`` (set by offline replays) dedupes: if a message with the
    same key already exists IN THIS CHANNEL it is returned verbatim, marked
    ``idempotent_replay`` so the caller can skip re-broadcasting / re-running
    ambient analysis. Live sends pass no key and are never deduped.

    THE THIRD DOOR OUT OF A DM, closed 2026-08-05. This lookup used to be
    ``WHERE idempotency_key = ?`` with no channel predicate — and the key is a
    request HEADER the caller chooses (routes/relay_routes.py, ``Idempotency-
    Key``). The send route authorises the channel in the URL; the service then
    answered from whichever channel owned that key, ANYWHERE in the hospital. So
    posting to a room you are entitled to, quoting a key from a private
    conversation, returned that conversation's content, sender and channel_id in
    a 200 — a message-read path that never consulted `get_channels_for_user`.
    Sibling of the alerts leak: the boundary was checked on the surface that
    looked like it selected the channel, and not on the one that merely matched.

    A key already owned by a DIFFERENT channel is now REFUSED (409) rather than
    answered from. It cannot simply fall through to the INSERT: the unique index
    on ``idempotency_key`` is global, so that would be a 500 instead — and it
    must not be silently ignored either, because "dedupe requested, dedupe not
    performed" is how the double-post this key exists to prevent comes back. The
    409 does confirm that SOME message holds the key, which is a far smaller
    disclosure than the message itself and is forced by the global index.
    """
    with db_session() as db:
        if idempotency_key:
            existing = db.execute(
                "SELECT * FROM messages WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if existing and existing["channel_id"] != channel_id:
                raise ChannelAccessError(
                    409, "That idempotency key belongs to a different conversation")
            if existing:
                return {**_message_row_to_dict(existing, is_bot=is_bot, source=source),
                        "idempotent_replay": True}
        try:
            cursor = db.execute(
                """INSERT INTO messages (channel_id, sender_id, sender_name, sender_role, content, message_type, is_bot, source, idempotency_key)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (channel_id, sender_id, sender_name, sender_role, content, message_type,
                 1 if is_bot else 0, source, idempotency_key),
            )
            msg_id = cursor.lastrowid
        except DBIntegrityError:
            # Concurrent replay raced past the pre-INSERT check — return the
            # existing row for this key rather than a duplicate message.
            existing = db.execute(
                "SELECT * FROM messages WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone() if idempotency_key else None
            # Same channel check as the pre-INSERT path. A racing writer in
            # ANOTHER channel is the cross-channel case again, arriving through
            # the unique index instead of the SELECT; answering it here would
            # reopen the door the check above closes.
            if existing and existing["channel_id"] != channel_id:
                raise ChannelAccessError(
                    409, "That idempotency key belongs to a different conversation")
            if not existing:
                raise
            return {**_message_row_to_dict(existing, is_bot=is_bot, source=source),
                    "idempotent_replay": True}

        # Fetch the inserted message
        row = db.execute("SELECT * FROM messages WHERE id = ?", (msg_id,)).fetchone()
        return _message_row_to_dict(row, is_bot=is_bot, source=source)
