"""Direct-message access control — the hole that was declared unfixable.

BEFORE (2026-08-05): `channels` carried no membership at all, so
`relay_service.get_channels_for_user` had nothing to filter `direct` rows on and
listed every DM in the hospital to every holder of `relay.read`. That list is the
READ boundary — routes/relay_routes.py and ws/realtime.py both authorise message
content with "is this channel in get_channels_for_user?" — so any authenticated
clinician could read any two colleagues' private conversation.

The interim idea was to infer participation from "has this user posted in the
thread". It was rejected, and this module is why: `test_a_member_added_who_never
_posted_can_read_it` is exactly the case that heuristic gets wrong, and a
security boundary that guesses is not a boundary.

AFTER: `channel_members` (migration 8) with one explicit row per member.
  * direct           → members only. No admin bypass.
  * case_room/dept   → UNCHANGED, still derived from department. The regression
                       guard for that is at the bottom of this file.
"""
import uuid

import pytest
from fastapi.testclient import TestClient

from auth import hash_password
from database import db_session
from main import app
from services.relay_service import (
    ChannelAccessError,
    add_channel_member,
    can_access_channel,
    create_direct_channel,
    get_channels_for_user,
    list_channel_members,
)

client = TestClient(app)

PASSWORD = "testpass123"


# ── helpers ──────────────────────────────────────────────────────────────────

def _mk_user(prefix: str, role: str = "doctor", department: str = "General Medicine",
             name: str | None = None) -> dict:
    uid = f"{prefix}-{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute(
            """INSERT INTO users (id, name, role, department, password_hash)
               VALUES (?, ?, ?, ?, ?)""",
            (uid, name or f"User {uid}", role, department, hash_password(PASSWORD)),
        )
    return {"id": uid, "role": role, "name": name or f"User {uid}", "department": department}


def _headers(user: dict) -> dict:
    resp = client.post("/api/auth/login",
                       json={"employee_id": user["id"], "password": PASSWORD})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['token']}"}


def _mk_channel(ch_type: str = "direct", name: str = "DM", patient_id: str | None = None) -> str:
    cid = f"DMTEST-{uuid.uuid4().hex[:10].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO channels (id, name, type, patient_id) VALUES (?, ?, ?, ?)",
            (cid, name, ch_type, patient_id),
        )
    return cid


def _add_member_row(channel_id: str, user_id: str, source: str = "added",
                    added_by: str | None = None) -> None:
    with db_session() as db:
        db.execute(
            """INSERT OR IGNORE INTO channel_members
                   (channel_id, user_id, added_by, source) VALUES (?, ?, ?, ?)""",
            (channel_id, user_id, added_by, source),
        )


def _post_row(channel_id: str, sender: dict, content: str, timestamp: str | None = None) -> int:
    """Insert a message directly — bypassing the send route, which enforces the
    very boundary several of these tests are about to probe."""
    with db_session() as db:
        if timestamp:
            cur = db.execute(
                """INSERT INTO messages (channel_id, sender_id, sender_name, sender_role,
                                         content, message_type, timestamp)
                   VALUES (?, ?, ?, ?, ?, 'text', ?)""",
                (channel_id, sender["id"], sender["name"], sender["role"], content, timestamp),
            )
        else:
            cur = db.execute(
                """INSERT INTO messages (channel_id, sender_id, sender_name, sender_role,
                                         content, message_type)
                   VALUES (?, ?, ?, ?, ?, 'text')""",
                (channel_id, sender["id"], sender["name"], sender["role"], content),
            )
        return cur.lastrowid


def _visible_ids(user: dict) -> set:
    return {c["id"] for c in get_channels_for_user(user["id"], user["role"])}


@pytest.fixture(scope="module", autouse=True)
def _the_admitted_patient_does_not_outlive_the_module():
    """THE OTHER HALF of the leak `_channels_do_not_outlive_the_test` closes.

    That fixture snapshot-diffs `channels`, so it takes the case room away — but
    `test_case_room_scoping_is_unchanged_by_the_membership_model` also seeds a
    PATIENT with status 'admitted' and an ACTIVE General Medicine row in
    `admissions`, and neither is a channel. Those two rows are the reason the
    room was department-scoped in the first place, and on their own they are a
    known way to break a LATER module: the shared session database (tests/
    conftest.py::init_test_db) means every surface that asks "who is admitted?"
    — the room_fit census, ward occupancy, any department roll-up — counts this
    module's leftovers as live inpatients for the rest of the run.

    Module-scoped rather than per-test: the rows are cheap, nothing in this file
    reads another test's patient, and the only requirement is that they are gone
    before the next module starts measuring.
    """
    yield
    with db_session() as db:
        db.execute("DELETE FROM admissions WHERE patient_id LIKE 'PAT-DM-%'")
        db.execute("UPDATE patients SET status = 'discharged' WHERE id LIKE 'PAT-DM-%'")


@pytest.fixture(autouse=True)
def _channels_do_not_outlive_the_test():
    """Delete every channel this module creates, plus the rows hanging off it.

    THE LEAK THIS CLOSES (observed 2026-08-05): the whole suite shares ONE temp
    database (tests/conftest.py::init_test_db, session-scoped), so a channel row
    left behind here is still in `channels` when a later module reads it. Nothing
    below is a per-test artefact as far as the rest of the suite is concerned —
    `_mk_channel` and the case rooms in particular land in exactly the table that
    Relay's access-control boundary scans with no WHERE clause on `channels`.

    It surfaced through tests/test_room_fit_adversarial.py::
    test_case_room_visibility_is_hospital_wide_and_that_is_load_bearing, which
    asserts a General Medicine reader and an Orthopedics reader see the SAME set
    of case rooms. `test_case_room_scoping_is_unchanged_by_the_membership_model`
    leaves behind a case room whose patient has an ACTIVE General Medicine
    admission, so the GM reader saw one room the Ortho reader did not and the
    tripwire fired — full suite red, that file alone green. That assertion is
    load-bearing (its own docstring explains what has to ship the day it fails),
    so the fix belongs here, at the source, not in an id filter over there.

    SNAPSHOT-DIFF rather than a list of ids collected by `_mk_channel`, because
    `_mk_channel` is not the only door: `create_direct_channel` and
    POST /api/relay/channels/direct mint their own ids, and a future helper would
    silently reopen this. Whatever appeared in `channels` while the test ran, goes.

    DELETION ORDER is child-before-parent, and PRAGMA foreign_keys is ON
    (database.py::get_db) — so if a new table ever gains an FK to `channels`
    without being added here, the final DELETE raises IntegrityError and this
    fails loudly instead of quietly leaking again.
    """
    with db_session() as db:
        before = {r["id"] for r in db.execute("SELECT id FROM channels").fetchall()}
    try:
        yield
    finally:
        with db_session() as db:
            leaked = [r["id"] for r in db.execute("SELECT id FROM channels").fetchall()
                      if r["id"] not in before]
            if not leaked:
                return
            marks = ",".join("?" * len(leaked))
            # message_room_fit_flags points at BOTH channels (channel_id,
            # suggested_channel_id) and messages (message_id,
            # redirected_message_id), so it has to go before either parent.
            db.execute(
                f"""DELETE FROM message_room_fit_flags
                     WHERE channel_id IN ({marks})
                        OR suggested_channel_id IN ({marks})
                        OR message_id IN (
                               SELECT id FROM messages WHERE channel_id IN ({marks}))
                        OR redirected_message_id IN (
                               SELECT id FROM messages WHERE channel_id IN ({marks}))""",
                leaked * 4,
            )
            db.execute(f"DELETE FROM surgery_prep_requests WHERE channel_id IN ({marks})",
                       leaked)
            db.execute(f"DELETE FROM messages WHERE channel_id IN ({marks})", leaked)
            db.execute(f"DELETE FROM channel_members WHERE channel_id IN ({marks})", leaked)
            db.execute(f"DELETE FROM channels WHERE id IN ({marks})", leaked)


@pytest.fixture
def dm():
    """Alice and Bob in a conversation; Mallory outside it."""
    alice = _mk_user("DM-ALICE", name="Dr Alice Menon")
    bob = _mk_user("DM-BOB", role="nurse", name="Nurse Bob Rao")
    mallory = _mk_user("DM-MALLORY", name="Dr Mallory Iyer")
    channel_id = _mk_channel(name="Alice / Bob")
    _add_member_row(channel_id, alice["id"], source="creator")
    _add_member_row(channel_id, bob["id"], added_by=alice["id"])
    _post_row(channel_id, alice, "Can you chase the potassium for bed 12?")
    return {"alice": alice, "bob": bob, "mallory": mallory, "channel": channel_id}


# ══════════════════════════════════════════════════════════════════════════════
#  1. THE BOUNDARY
# ══════════════════════════════════════════════════════════════════════════════

def test_a_non_member_cannot_list_the_direct_channel(dm):
    assert dm["channel"] not in _visible_ids(dm["mallory"])
    assert not can_access_channel(dm["mallory"]["id"], dm["mallory"]["role"], dm["channel"])


def test_a_non_member_cannot_read_the_messages(dm):
    """The list is the read boundary — prove it over HTTP, not just in-process."""
    resp = client.get(f"/api/relay/channels/{dm['channel']}/messages",
                      headers=_headers(dm["mallory"]))
    assert resp.status_code == 403, resp.text
    assert "potassium" not in resp.text


def test_a_member_added_who_never_posted_can_read_it(dm):
    """THE CASE THE 'has posted here' HEURISTIC GETS WRONG.

    Bob was added by Alice and has never written a word. Under participation-
    inference he is invisible to himself; under explicit membership he is a
    member, which is the whole reason the table exists.
    """
    with db_session() as db:
        assert db.execute(
            "SELECT COUNT(*) FROM messages WHERE channel_id = ? AND sender_id = ?",
            (dm["channel"], dm["bob"]["id"]),
        ).fetchone()[0] == 0, "fixture drifted — Bob must NOT have posted"

    assert dm["channel"] in _visible_ids(dm["bob"])
    resp = client.get(f"/api/relay/channels/{dm['channel']}/messages",
                      headers=_headers(dm["bob"]))
    assert resp.status_code == 200, resp.text
    assert "potassium" in resp.text


def test_an_admin_has_no_standing_in_a_private_conversation(dm):
    """`admin` is unscoped for DEPARTMENT scoping — a governance account oversees
    every ward. Membership is not a department scope, and a hospital
    administrator silently reading staff DMs is the abuse this closes. An admin
    who needs the thread must be added to it, on the record."""
    admin = _mk_user("DM-ADMIN", role="admin", department="")
    assert dm["channel"] not in _visible_ids(admin)
    resp = client.get(f"/api/relay/channels/{dm['channel']}/messages",
                      headers=_headers(admin))
    assert resp.status_code == 403, resp.text


def test_a_non_member_cannot_post_into_it(dm):
    resp = client.post(f"/api/relay/channels/{dm['channel']}/messages",
                       json={"content": "hello", "message_type": "text"},
                       headers=_headers(dm["mallory"]))
    assert resp.status_code == 403, resp.text


def test_a_non_member_cannot_mark_it_read(dm):
    """mark_read writes to the thread's rows — same boundary as reading it."""
    resp = client.post(f"/api/relay/channels/{dm['channel']}/read",
                       headers=_headers(dm["mallory"]))
    assert resp.status_code == 403, resp.text


def test_an_alert_posted_in_a_dm_does_not_leak_through_the_alerts_endpoint(dm):
    """THE SECOND DOOR. `/api/relay/alerts` does not go through the channel
    list: it scans every alert in the hospital and selects on the alert's own
    targeting payload — and for an admin it selects unconditionally. Since
    `message_type` comes from the request body, a DM can contain an alert.
    """
    import json as _json
    alert = _json.dumps({
        "is_structured": True,
        "title": "DM alert canary borborygmus",
        "target_roles": ["doctor", "nurse"],
        "target_departments": ["General Medicine"],
        "target_users": [dm["mallory"]["id"]],
    })
    with db_session() as db:
        db.execute(
            """INSERT INTO messages (channel_id, sender_id, sender_name, sender_role,
                                     content, message_type)
               VALUES (?, ?, ?, 'doctor', ?, 'alert')""",
            (dm["channel"], dm["alice"]["id"], dm["alice"]["name"], alert),
        )

    admin = _mk_user("DM-ALERTADM", role="admin", department="")
    for outsider in (dm["mallory"], admin):
        resp = client.get("/api/relay/alerts", headers=_headers(outsider))
        assert resp.status_code == 200, resp.text
        assert "borborygmus" not in resp.text, (
            f"{outsider['role']} read a DM's contents through the alerts endpoint"
        )

    seen = client.get("/api/relay/alerts", headers=_headers(dm["bob"]))
    assert "borborygmus" in seen.text, "a member must still receive it"


def test_case_room_alerts_are_not_narrowed_by_the_dm_rule(dm):
    """The alert clause must touch DIRECT channels only. An alert in a case room
    still reaches whoever it targets, including across departments — narrowing
    clinical alert delivery would be a far worse bug than the leak above."""
    import json as _json
    patient_free_room = _mk_channel(ch_type="case_room", name="Case: alert scope")
    alert = _json.dumps({
        "is_structured": True,
        "title": "case room alert canary rhinotillexis",
        "target_users": [dm["mallory"]["id"]],
    })
    with db_session() as db:
        db.execute(
            """INSERT INTO messages (channel_id, sender_id, sender_name, sender_role,
                                     content, message_type)
               VALUES (?, ?, ?, 'doctor', ?, 'alert')""",
            (patient_free_room, dm["alice"]["id"], dm["alice"]["name"], alert),
        )
    resp = client.get("/api/relay/alerts", headers=_headers(dm["mallory"]))
    assert "rhinotillexis" in resp.text


def test_the_alerts_endpoint_never_disagrees_with_the_channel_list(dm):
    """THE ONE RESIDUAL COUPLING, pinned rather than assumed.

    Every other authorisation surface is DEFINED IN TERMS OF
    `get_channels_for_user` — `can_access_channel`, and through it the REST
    routes and ws/realtime.py. `get_user_alerts` cannot be: it is not
    channel-scoped, it scans every alert in the hospital and selects on the
    alert's own targeting payload. So it carries a SECOND expression of the DM
    membership rule, in its own SQL (relay_service.py, the
    ``c.type != 'direct' OR EXISTS(channel_members …)`` clause).

    Two expressions of one rule is the trap this work was told to avoid. It is
    accepted here only because the alternative — filtering alerts through the
    channel list — would NARROW clinical alert delivery, which is worse than the
    leak being closed (see test_case_room_alerts_are_not_narrowed_by_the_dm_rule).

    The mitigation is this test: the two rules must AGREE, for every seeded user,
    over the live database. Change one and not the other and this fails loudly
    instead of quietly reopening the door.
    """
    import json as _json
    from services.relay_service import get_user_alerts

    alert = _json.dumps({
        "is_structured": True,
        "title": "coupling canary pneumonoultramicroscopic",
        # Target as widely as the payload allows, so the ONLY thing that can
        # withhold this row from anyone is the DM membership clause itself.
        "target_roles": ["doctor", "nurse", "admin"],
        "target_departments": ["General Medicine", "Cardiology", ""],
        "target_users": [],
    })
    with db_session() as db:
        db.execute(
            """INSERT INTO messages (channel_id, sender_id, sender_name, sender_role,
                                     content, message_type)
               VALUES (?, ?, ?, 'doctor', ?, 'alert')""",
            (dm["channel"], dm["alice"]["id"], dm["alice"]["name"], alert),
        )
        users = db.execute(
            "SELECT id, name, role FROM users WHERE role != 'patient'"
        ).fetchall()
        direct_ids = {
            r["id"] for r in db.execute(
                "SELECT id FROM channels WHERE type = 'direct'"
            ).fetchall()
        }

    offenders = []
    reached_a_member = False
    for user in users:
        visible = _visible_ids({"id": user["id"], "role": user["role"]})
        for row in get_user_alerts(user["id"], user["role"]):
            cid = row.get("channel_id")
            if cid != dm["channel"]:
                continue
            if cid not in visible:
                offenders.append(
                    f"{user['name']} ({user['role']}) received an alert from DM "
                    f"{cid}, which the channel list withholds from them"
                )
            elif user["id"] in (dm["alice"]["id"], dm["bob"]["id"]):
                reached_a_member = True

    assert not offenders, (
        "the alerts endpoint and the channel list disagree about DM membership:\n  "
        + "\n  ".join(offenders)
    )
    # POSITIVE CONTROL. Without this the test passes when the canary reaches
    # NOBODY — a targeting change, a payload typo or a broken alerts endpoint
    # would all read as "no disagreement found" and the invariant would stop
    # being tested while still going green.
    assert reached_a_member, (
        "the canary alert reached no member at all — this test proved nothing"
    )
    assert direct_ids, "no direct channels to check"


def test_a_replayed_idempotency_key_cannot_read_a_dm_from_outside(dm):
    """THE THIRD DOOR — found 2026-08-05 while auditing the membership work.

    `send_message`'s replay lookup was ``WHERE idempotency_key = ?`` with NO
    channel predicate, and ``Idempotency-Key`` is a plain request header the
    caller chooses. So the boundary check on the send route authorised the
    channel in the URL while the SERVICE answered from whichever channel owned
    that key — anywhere in the hospital.

    Mallory posts to a room she is legitimately in, quoting a key used by a
    message inside Alice and Bob's DM. Before the fix the 200 response carried
    that DM's content, sender and channel_id straight back to her: a message-read
    path that never consulted `get_channels_for_user` at all.

    The key is unguessable in normal use (a client UUID), which is mitigation,
    not a boundary — keys reach logs, proxies and offline queues, and an insider
    who has seen one keeps a permanent read primitive into a private thread.
    """
    stolen_key = f"stolen-{uuid.uuid4()}"
    secret = "DM idempotency canary quinquagenarian"
    resp = client.post(
        f"/api/relay/channels/{dm['channel']}/messages",
        headers={**_headers(dm["alice"]), "Idempotency-Key": stolen_key},
        json={"content": secret, "message_type": "text"},
    )
    assert resp.status_code == 200, resp.text

    # A room Mallory is genuinely entitled to post in.
    her_room = _mk_channel(ch_type="department", name="General Medicine")
    replay = client.post(
        f"/api/relay/channels/{her_room}/messages",
        headers={**_headers(dm["mallory"]), "Idempotency-Key": stolen_key},
        json={"content": "unrelated", "message_type": "text"},
    )

    assert secret not in replay.text, (
        "a replayed idempotency key returned a DM's contents to a non-member"
    )
    assert dm["channel"] not in replay.text, (
        "a replayed idempotency key disclosed the DM's channel id to a non-member"
    )
    assert replay.status_code == 409, (
        "a key already owned by another conversation must be refused, not "
        f"silently answered from it (got {replay.status_code}: {replay.text})"
    )

    # And the legitimate owner's replay still dedupes, in its own channel.
    again = client.post(
        f"/api/relay/channels/{dm['channel']}/messages",
        headers={**_headers(dm["alice"]), "Idempotency-Key": stolen_key},
        json={"content": secret, "message_type": "text"},
    )
    assert again.status_code == 200, again.text
    assert again.json()["id"] == resp.json()["id"]


def test_can_access_channel_agrees_with_the_list_for_everyone(dm):
    """One rule, not two. `can_access_channel` is the named single-channel form
    of the list query and must never diverge from it — including for the
    websocket layer, which authorises through the same list."""
    for user in (dm["alice"], dm["bob"], dm["mallory"]):
        visible = _visible_ids(user)
        for channel_id in (dm["channel"], "definitely-not-a-channel"):
            assert can_access_channel(user["id"], user["role"], channel_id) == (
                channel_id in visible
            ), f"{user['id']} / {channel_id}"


# ══════════════════════════════════════════════════════════════════════════════
#  2. GROWING THE TABLE — otherwise DMs are unusable
# ══════════════════════════════════════════════════════════════════════════════

def test_a_member_can_add_someone_and_they_see_it_immediately(dm):
    assert dm["channel"] not in _visible_ids(dm["mallory"])
    resp = client.post(f"/api/relay/channels/{dm['channel']}/members",
                       json={"user_id": dm["mallory"]["id"]},
                       headers=_headers(dm["bob"]))
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "ok"
    assert dm["channel"] in _visible_ids(dm["mallory"])


def test_a_non_member_cannot_add_anyone(dm):
    outsider = _mk_user("DM-OUT")
    resp = client.post(f"/api/relay/channels/{dm['channel']}/members",
                       json={"user_id": outsider["id"]},
                       headers=_headers(dm["mallory"]))
    assert resp.status_code == 403, resp.text
    assert dm["channel"] not in _visible_ids(outsider)


def test_an_admin_cannot_add_themselves(dm):
    """The obvious escalation: hold every permission, add yourself, read the
    thread. `relay.send` on the route is a floor; membership is the check."""
    admin = _mk_user("DM-ADMIN2", role="admin", department="")
    resp = client.post(f"/api/relay/channels/{dm['channel']}/members",
                       json={"user_id": admin["id"]}, headers=_headers(admin))
    assert resp.status_code == 403, resp.text
    assert dm["channel"] not in _visible_ids(admin)


def test_adding_the_same_person_twice_is_a_no_op(dm):
    first = add_channel_member(dm["channel"], dm["alice"], dm["mallory"]["id"])
    second = add_channel_member(dm["channel"], dm["alice"], dm["mallory"]["id"])
    assert first["status"] == "ok"
    assert second["status"] == "already_member"
    with db_session() as db:
        assert db.execute(
            "SELECT COUNT(*) FROM channel_members WHERE channel_id = ? AND user_id = ?",
            (dm["channel"], dm["mallory"]["id"]),
        ).fetchone()[0] == 1


def test_membership_does_not_apply_to_case_rooms(dm):
    """One rule per channel type. A members row on a case room would be a
    second, disagreeing grant that the scoping logic never reads — so the
    service refuses rather than writing a lie into the table."""
    case_room = _mk_channel(ch_type="case_room", name="Case: someone")
    _add_member_row(case_room, dm["alice"]["id"], source="creator")
    with pytest.raises(ChannelAccessError) as err:
        add_channel_member(case_room, dm["alice"], dm["bob"]["id"])
    assert err.value.status == 400
    assert "direct" in err.value.detail.lower()


def test_a_patient_account_cannot_be_added(dm):
    """`patient` is a Bridge-only role with no relay.read. Gated on the
    PERMISSION rather than a role name so a future role inherits the rule."""
    patient = _mk_user("DM-PT", role="patient", department="")
    with pytest.raises(ChannelAccessError) as err:
        add_channel_member(dm["channel"], dm["alice"], patient["id"])
    assert err.value.status == 400
    assert dm["channel"] not in _visible_ids(patient)


def test_unknown_channel_and_unknown_user_are_404(dm):
    with pytest.raises(ChannelAccessError) as no_channel:
        add_channel_member("no-such-channel", dm["alice"], dm["bob"]["id"])
    assert no_channel.value.status == 404
    with pytest.raises(ChannelAccessError) as no_user:
        add_channel_member(dm["channel"], dm["alice"], "NOBODY-AT-ALL")
    assert no_user.value.status == 404


def test_every_addition_lands_on_the_audit_chain(dm):
    add_channel_member(dm["channel"], dm["alice"], dm["mallory"]["id"])
    with db_session() as db:
        row = db.execute(
            """SELECT * FROM audit_logs
                WHERE action_type = 'relay.channel_member_added'
                  AND target_id = ?
                ORDER BY id DESC LIMIT 1""",
            (dm["channel"],),
        ).fetchone()
    assert row is not None, "granting access to a private thread must be audited"
    assert row["actor_user_id"] == dm["alice"]["id"]
    assert row["event_category"] == "security"   # fail-closed category
    assert row["row_hash"], "the grant must be sealed onto the hash chain"
    assert dm["mallory"]["id"] in (row["metadata_json"] or "")


def test_a_grant_that_cannot_be_audited_does_not_happen(dm, monkeypatch):
    """FAIL-CLOSED, proven rather than asserted in a comment.

    `_audit_membership` writes with event_category='security', which
    audit_service treats as fail-closed — the write RAISES instead of returning,
    and because it runs inside `add_channel_member`'s own `db_session`, the
    membership INSERT rolls back with it. The docstring claims that; this proves
    it, because the failure mode it prevents is silent: an access grant to a
    private conversation that exists in `channel_members` and nowhere on the
    tamper-evident chain.

    Asserts the ROW IS ABSENT, not merely that an exception surfaced — an
    exception after a committed INSERT would be the exact bug.
    """
    import services.audit_service as audit

    def _explode(*args, **kwargs):
        raise audit.AuditWriteError("audit store unavailable")

    monkeypatch.setattr(audit, "log_audit_event", _explode)

    with pytest.raises(audit.AuditWriteError):
        add_channel_member(dm["channel"], dm["alice"], dm["mallory"]["id"])

    monkeypatch.undo()
    with db_session() as db:
        assert db.execute(
            "SELECT COUNT(*) FROM channel_members WHERE channel_id = ? AND user_id = ?",
            (dm["channel"], dm["mallory"]["id"]),
        ).fetchone()[0] == 0, "an unauditable grant of DM access was persisted"
    assert dm["channel"] not in _visible_ids(dm["mallory"])


def test_the_members_list_is_gated_on_the_same_boundary(dm):
    ok = client.get(f"/api/relay/channels/{dm['channel']}/members",
                    headers=_headers(dm["bob"]))
    assert ok.status_code == 200, ok.text
    assert {m["user_id"] for m in ok.json()} == {dm["alice"]["id"], dm["bob"]["id"]}

    denied = client.get(f"/api/relay/channels/{dm['channel']}/members",
                        headers=_headers(dm["mallory"]))
    assert denied.status_code == 403, denied.text


# ══════════════════════════════════════════════════════════════════════════════
#  3. CREATION — the bootstrap, without which the table can only shrink access
# ══════════════════════════════════════════════════════════════════════════════

def test_creating_a_dm_makes_the_creator_and_invitee_members():
    alice = _mk_user("DM-CA", name="Dr Create Alice")
    bob = _mk_user("DM-CB", role="nurse", name="Nurse Create Bob")
    carol = _mk_user("DM-CC", name="Dr Create Carol")

    resp = client.post("/api/relay/channels/direct",
                       json={"user_ids": [bob["id"]]}, headers=_headers(alice))
    assert resp.status_code == 200, resp.text
    created = resp.json()
    assert created["status"] == "created"

    channel_id = created["id"]
    assert channel_id in _visible_ids(alice)
    assert channel_id in _visible_ids(bob)
    assert channel_id not in _visible_ids(carol)

    sources = {m["user_id"]: m["source"] for m in list_channel_members(channel_id)}
    assert sources == {alice["id"]: "creator", bob["id"]: "added"}


def test_reopening_the_same_conversation_returns_the_same_channel():
    """Otherwise every "message Dr X" tap forks a new empty thread and the
    history scatters across duplicates."""
    alice = _mk_user("DM-RA")
    bob = _mk_user("DM-RB", role="nurse")
    first = create_direct_channel(alice, [bob["id"]])
    second = create_direct_channel(alice, [bob["id"]])
    third = create_direct_channel(bob, [alice["id"]])
    assert first["status"] == "created"
    assert second == {"status": "existing", "id": first["id"], "type": "direct"}
    assert third["id"] == first["id"], "the same two people, from either side"

    # A DIFFERENT set of people is a different conversation.
    carol = _mk_user("DM-RC")
    group = create_direct_channel(alice, [bob["id"], carol["id"]])
    assert group["status"] == "created" and group["id"] != first["id"]


def test_creating_a_dm_with_only_yourself_is_refused():
    alice = _mk_user("DM-SA")
    with pytest.raises(ChannelAccessError) as err:
        create_direct_channel(alice, [alice["id"]])
    assert err.value.status == 400


# ══════════════════════════════════════════════════════════════════════════════
#  4. THE BACKFILL
# ══════════════════════════════════════════════════════════════════════════════

def _run_backfill():
    from migrations import _m8_channel_members
    with db_session() as db:
        _m8_channel_members(db)


def _members(channel_id: str) -> dict:
    with db_session() as db:
        return {
            r["user_id"]: {"source": r["source"], "added_by": r["added_by"],
                           "added_at": r["added_at"]}
            for r in db.execute(
                "SELECT user_id, source, added_by, added_at FROM channel_members "
                "WHERE channel_id = ?", (channel_id,)
            ).fetchall()
        }


def test_backfill_makes_the_distinct_senders_of_a_legacy_dm_its_members():
    """Message history is the only evidence a legacy DM has — and it is evidence
    of access ALREADY exercised, so the backfill can only narrow, never widen."""
    alice = _mk_user("DM-BFA", name="Dr Backfill Alice")
    bob = _mk_user("DM-BFB", role="nurse", name="Nurse Backfill Bob")
    outsider = _mk_user("DM-BFO", name="Dr Backfill Outsider")
    channel_id = _mk_channel(name="legacy thread")          # NO member rows
    _post_row(channel_id, alice, "first", timestamp="2026-01-01T08:00:00")
    _post_row(channel_id, alice, "second", timestamp="2026-01-02T08:00:00")
    _post_row(channel_id, bob, "reply", timestamp="2026-01-03T08:00:00")

    assert channel_id not in _visible_ids(alice), "no member rows yet — fail closed"

    _run_backfill()

    rows = _members(channel_id)
    assert set(rows) == {alice["id"], bob["id"]}
    assert rows[alice["id"]]["source"] == "backfill_sender"
    assert rows[alice["id"]]["added_by"] is None, (
        "an inferred grant has no human behind it and must not name one"
    )
    assert rows[alice["id"]]["added_at"] == "2026-01-01T08:00:00", "earliest message"
    assert channel_id in _visible_ids(alice)
    assert channel_id in _visible_ids(bob)
    assert channel_id not in _visible_ids(outsider)


def test_backfill_is_idempotent_and_never_overwrites_an_explicit_grant():
    alice = _mk_user("DM-BIA")
    bob = _mk_user("DM-BIB", role="nurse")
    channel_id = _mk_channel(name="idempotence thread")
    _post_row(channel_id, alice, "hello", timestamp="2026-02-01T08:00:00")
    _add_member_row(channel_id, bob["id"], source="added", added_by=alice["id"])

    _run_backfill()
    once = _members(channel_id)
    with db_session() as db:
        total_once = db.execute("SELECT COUNT(*) FROM channel_members").fetchone()[0]

    _run_backfill()
    _run_backfill()
    twice = _members(channel_id)
    with db_session() as db:
        total_twice = db.execute("SELECT COUNT(*) FROM channel_members").fetchone()[0]

    assert once == twice, "re-running the backfill changed membership"
    assert total_once == total_twice, "re-running the backfill duplicated rows"
    # Bob posted nothing but was explicitly added; the backfill must not
    # downgrade that row's provenance to an inference.
    assert twice[bob["id"]] == {"source": "added", "added_by": alice["id"],
                                "added_at": twice[bob["id"]]["added_at"]}


def test_backfill_leaves_case_rooms_and_department_channels_alone():
    """They are scoped by DEPARTMENT — live, and following the patient. A
    membership snapshot taken from message history would both go stale and
    shrink a ward-wide room to whoever happened to type in it."""
    doctor = _mk_user("DM-BFD")
    case_room = _mk_channel(ch_type="case_room", name="Case: backfill")
    dept = _mk_channel(ch_type="department", name="General Medicine")
    _post_row(case_room, doctor, "ward round done")
    _post_row(dept, doctor, "handover at 8")

    _run_backfill()

    assert _members(case_room) == {}
    assert _members(dept) == {}


# ══════════════════════════════════════════════════════════════════════════════
#  5. NO REGRESSION IN THE DEPARTMENT SCOPING FIXED THE SAME DAY
# ══════════════════════════════════════════════════════════════════════════════

def test_case_room_scoping_is_unchanged_by_the_membership_model():
    """A case room is visible to the department caring for the patient — to a
    clinician who has NEVER posted in it, and with no membership row anywhere.
    A clinician in another department still cannot see it."""
    suffix = uuid.uuid4().hex[:6].upper()
    ours = _mk_user("DM-GM", department="General Medicine")
    theirs = _mk_user("DM-CARD", department="Cardiology")
    patient_id = f"PAT-DM-{suffix}"
    with db_session() as db:
        db.execute(
            """INSERT INTO patients (id, name, mrn, ward, bed, status)
               VALUES (?, 'Membership Regression', ?, 'GM-1', 'GM-9', 'admitted')""",
            (patient_id, f"MRN-DM-{suffix}"),
        )
        db.execute(
            """INSERT INTO admissions (id, patient_id, admitted_by, department, status)
               VALUES (?, ?, ?, 'General Medicine', 'active')""",
            (f"ADM-DM-{suffix}", patient_id, ours["id"]),
        )
    case_room = _mk_channel(ch_type="case_room", name="Case: Membership Regression",
                            patient_id=patient_id)
    _post_row(case_room, ours, "admitted overnight, plan as per notes")

    with db_session() as db:
        assert db.execute(
            "SELECT COUNT(*) FROM channel_members WHERE channel_id = ?", (case_room,)
        ).fetchone()[0] == 0, "case rooms must not need membership rows"

    assert case_room in _visible_ids(ours)
    assert case_room not in _visible_ids(theirs)


def test_an_unrecognised_channel_type_fails_closed():
    """The `else` branch of the type switch is unreachable while channels.type
    keeps its CHECK constraint — but it is the read boundary's DEFAULT, and a
    default that GRANTS is how both the case-room leak and the DM leak happened.

    Reached here by suspending the CHECK for one UPDATE, which is the only way
    to produce a row the schema forbids. Restored in `finally`: a rogue row left
    behind would be a landmine for every other module that reads `channels`.
    """
    doctor = _mk_user("DM-ODD")
    channel_id = _mk_channel(name="rogue")
    _add_member_row(channel_id, doctor["id"], source="creator")
    assert channel_id in _visible_ids(doctor), "member sees it as an ordinary DM"

    try:
        with db_session() as db:
            db.execute("PRAGMA ignore_check_constraints = ON")
            try:
                db.execute("UPDATE channels SET type = 'wormhole' WHERE id = ?",
                           (channel_id,))
            finally:
                db.execute("PRAGMA ignore_check_constraints = OFF")

        with db_session() as db:
            assert db.execute(
                "SELECT type FROM channels WHERE id = ?", (channel_id,)
            ).fetchone()["type"] == "wormhole", "could not stage the rogue row"

        assert channel_id not in _visible_ids(doctor), (
            "an unrecognised channel type must be WITHHELD, not granted"
        )
    finally:
        with db_session() as db:
            db.execute("UPDATE channels SET type = 'direct' WHERE id = ?", (channel_id,))
