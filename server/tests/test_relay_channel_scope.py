"""Regression: Relay's channel list was the access-control boundary, and it did not scope.

Found 2026-08-05. `get_channels_for_user` built its SQL with NO WHERE clause on
`channels` — the single bound parameter fed the unread-count subquery, not access
control. Only `type == 'department'` rows were filtered; case rooms and direct
messages fell through to a bare `else: filtered.append(r)`.

That is not merely a noisy sidebar. routes/relay_routes.py authorises message reads with

    if not any(c["id"] == channel_id for c in get_channels_for_user(...)):
        raise HTTPException(403)

so this function IS the boundary for Relay message CONTENT. Every clinician could
read every patient's case room in the hospital.

Same root cause as the ward-brief leak fixed the same day: scoping was applied to the
surface that looked like it *selected* something, and not to the ones that merely *listed*.
"""
import uuid
from datetime import datetime, timezone

import pytest

from database import db_session
from services.relay_service import get_channels_for_user


def _clinical_users():
    with db_session() as db:
        return db.execute(
            """SELECT id, name, role, department FROM users
               WHERE department IS NOT NULL AND department != '' AND role != 'admin'"""
        ).fetchall()


def _admission_department(patient_id):
    if not patient_id:
        return None
    with db_session() as db:
        row = db.execute(
            "SELECT department FROM admissions WHERE patient_id = ? AND status = 'active' LIMIT 1",
            (patient_id,),
        ).fetchone()
    return row["department"] if row else None


def test_a_ward_scoped_clinician_sees_no_other_departments_case_rooms():
    users = _clinical_users()
    if not users:
        pytest.skip("no department-scoped clinical users seeded")

    offenders = []
    for user in users:
        rooms = [
            c for c in get_channels_for_user(user["id"], user["role"])
            if c["type"] == "case_room"
        ]
        for room in rooms:
            dept = _admission_department(room.get("patient_id"))
            # Only assert where the department is KNOWABLE. A patient with no
            # active admission has no derivable department (`patients` has no
            # department column) and is deliberately still listed — see the
            # comment at the fix. The leak being closed is rooms whose
            # department is known and DIFFERENT.
            if dept and dept != user["department"]:
                offenders.append(
                    f"{user['name']} ({user['department']}) can see "
                    f"{room['name']!r} which belongs to {dept}"
                )

    assert not offenders, (
        "Relay channel list leaked case rooms across departments:\n  "
        + "\n  ".join(offenders)
    )


def test_the_scoping_is_driven_by_the_user_row_not_by_an_argument():
    """department_scope's contract: a scope the caller can name is not a scope.

    get_channels_for_user takes only (user_id, user_role) and must derive the
    department from the USER ROW. If a department ever becomes a parameter, this
    test's premise is broken and the signature check should fail loudly.
    """
    import inspect
    params = list(inspect.signature(get_channels_for_user).parameters)
    assert params == ["user_id", "user_role"], (
        "get_channels_for_user grew a parameter. If a department/scope selector was "
        f"added it must NOT be client-supplied. Signature is now: {params}"
    )


@pytest.fixture()
def _open_case_room():
    """One ADMITTED patient with a case room, owned by this test.

    This used to ride on whatever admitted patients other modules happened to
    leave in the shared DB. That premise died when conftest's
    `_sweep_admitted_leftovers` started discharging leftovers at each module
    boundary: `get_channels_for_user` deliberately hides the case room of a
    DISCHARGED patient (relay_service — a room is archived when the patient
    leaves), so an admin correctly saw zero rooms and the assertion below read
    that as over-scoping. The skip guard could not catch it either, because it
    counted case-room ROWS without asking whether any patient was still
    admitted.

    Owning the room makes the test prove its own premise instead of inheriting
    it, so it now fails only for the reason it names.
    """
    pid = f"RCSP{uuid.uuid4().hex[:8].upper()}"
    now = datetime.now(timezone.utc).isoformat()
    with db_session() as db:
        db.execute(
            "INSERT INTO patients (id, name, mrn, bed, ward, age, sex, diagnosis, "
            "status, admission_date) VALUES (?, 'Relay Scope Patient', ?, 'RCS-1', "
            "'RCS-1', 58, 'F', 'Relay channel scope fixture', 'admitted', ?)",
            (pid, f"MRN-{uuid.uuid4().hex[:8].upper()}", now),
        )
        db.execute(
            "INSERT OR IGNORE INTO users (id, name, role, password_hash) "
            "VALUES ('RCSADM', 'Relay Scope Admitter', 'doctor', 'x')"
        )
        db.execute(
            "INSERT INTO admissions (patient_id, department, ward, bed, status, admitted_by) "
            "VALUES (?, 'Relay Scope Medicine', 'RCS-1', 'RCS-1', 'active', 'RCSADM')",
            (pid,),
        )
        db.execute(
            "INSERT OR REPLACE INTO channels (id, name, type, patient_id) "
            "VALUES (?, ?, 'case_room', ?)",
            (f"rcs-room-{pid}", f"Relay Scope Case Room {pid}", pid),
        )
    yield pid
    with db_session() as db:
        db.execute("DELETE FROM channels WHERE patient_id = ?", (pid,))
        db.execute("DELETE FROM admissions WHERE patient_id = ?", (pid,))
        db.execute("DELETE FROM patients WHERE id = ?", (pid,))


def test_an_unscoped_admin_still_sees_across_departments(_open_case_room):
    """None/blank department means a genuinely unscoped governance account — the
    documented convention in permissions.department_scope. It must not be read as
    'scope unknown, show nothing', or admin dashboards silently empty."""
    with db_session() as db:
        admin = db.execute("SELECT id, role FROM users WHERE role = 'admin' LIMIT 1").fetchone()
    if not admin:
        pytest.skip("no admin user seeded")

    rooms = [c for c in get_channels_for_user(admin["id"], admin["role"]) if c["type"] == "case_room"]
    assert rooms, "an admin lost all case-room visibility — scoping over-applied"
    assert any(c["patient_id"] == _open_case_room for c in rooms), (
        "the admin did not see the open case room seeded for this test"
    )


def test_direct_messages_are_membership_scoped():
    """The replacement for `test_direct_messages_remain_a_known_open_hole`.

    That test asserted the ABSENCE of a membership model and was to be replaced
    the day one landed. It has (migration 8, `channel_members`), so the assertion
    is now the real property: a direct channel is listed to its members and to
    NOBODY else — including an administrator, who is unscoped for *department*
    but has no standing in a private conversation.

    Deliberately written against the live schema and every seeded user rather
    than a purpose-built fixture: this is the hospital-wide invariant, and a
    fixture-only version would pass while a seeded DM leaked. The per-case
    behaviour (added-but-never-posted, add/remove authorisation, the backfill)
    is in tests/test_relay_dm_membership.py.
    """
    with db_session() as db:
        assert db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='channel_members'"
        ).fetchone(), "channel_members table missing — DM access control is not installed"

        dms = db.execute("SELECT id FROM channels WHERE type = 'direct'").fetchall()
        if not dms:
            pytest.skip("no direct channels in this database")
        members = {
            dm["id"]: {
                r["user_id"] for r in db.execute(
                    "SELECT user_id FROM channel_members WHERE channel_id = ?", (dm["id"],)
                ).fetchall()
            }
            for dm in dms
        }
        users = db.execute("SELECT id, name, role FROM users WHERE role != 'patient'").fetchall()

    offenders = []
    for user in users:
        visible = {c["id"] for c in get_channels_for_user(user["id"], user["role"])
                   if c["type"] == "direct"}
        for dm_id, member_ids in members.items():
            if dm_id in visible and user["id"] not in member_ids:
                offenders.append(f"{user['name']} ({user['role']}) can see DM {dm_id} "
                                 f"without being a member")
            if dm_id not in visible and user["id"] in member_ids:
                offenders.append(f"{user['name']} ({user['role']}) is a member of DM "
                                 f"{dm_id} but cannot see it")

    assert not offenders, "DM membership scoping is wrong:\n  " + "\n  ".join(offenders)
