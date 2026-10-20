"""Live socket rooms per facility (multisite-10, wave 12, 20 October 2026).

With one facility on record the rooms are exactly what they were: a
vitals.read socket joins 'dept:<department>', or 'dept:__all__' with none,
and a vitals update goes to both. Once a second facilities row exists
(isolation is keyed on data):

  * the socket handshake reads the facility view through the shared resolver
    (facility_service.facility_scope_for), and a scope that cannot be read
    refuses the socket;
  * a department-less vitals.read socket joins 'fac:<id>:all' for its
    facility, a group-wide account (enterprise grant) joins 'grp:all', a
    ward socket keeps its department room, and an account whose department
    no facility lists joins no vitals room;
  * a vitals update goes to the patient's department room, the room of the
    patient's facility and 'grp:all', in one emit, never to 'dept:__all__',
    so an F2 department-less socket never hears an F1 update and a
    group-wide socket hears both;
  * a socket whose facility view changes (a second facility appears, a home
    facility moves, a group grant is revoked) is dropped on its next
    delivery and rejoins under the new view;
  * the content-free queue nudge reaches the facility rooms as it reached
    'dept:__all__'.

The module owns its database (isolated_module_database); the second facility
row is added per test and removed after, and the facility caches and the
socket module's caches are cleared around every test. No socket transport is
opened: the handlers run with the room calls recorded, and a real
python-socketio manager resolves who hears which rooms.
Run with the test env, never the shipping .env.
"""
from __future__ import annotations

import asyncio
import uuid

import pytest
from socketio import AsyncServer

from auth import create_token
from database import db_session
from permissions import FACILITY_PATIENT_SQL
from services import facility_service as FS
from services.clinical_time import utc_iso_now
from ws import realtime

pytestmark = pytest.mark.usefixtures("isolated_module_database")

TAG = uuid.uuid4().hex[:6].upper()
F2 = 9531                       # an id no other module writes
F2_CODE = f"SR{TAG[:4]}"
F1_DEPT = f"Morvesk Medicine {TAG}"
F2_DEPT = f"Tallowmere Medicine {TAG}"
UNLISTED_DEPT = f"Grindlewick Annex {TAG}"


# ── the world ────────────────────────────────────────────────────────────────

def _user(db, key: str, role: str, dept: str | None, home: int | None = None) -> dict:
    uid = f"SRM{key[:4].upper()}{uuid.uuid4().hex[:5].upper()}"
    db.execute("INSERT INTO users (id, name, role, department, password_hash) "
               "VALUES (?, ?, ?, ?, 'x')", (uid, f"Room {key}", role, dept))
    if home is not None:
        db.execute("INSERT INTO user_facilities (user_id, facility_id, kind, granted_by, "
                   "granted_at, reason) VALUES (?, ?, 'home', 'system', CURRENT_TIMESTAMP, "
                   "'socket rooms test')", (uid, home))
    return {"id": uid, "role": role, "department": dept, "name": f"Room {key}"}


def _patient(db, name: str, dept: str, facility: int, user_id: str | None = None) -> str:
    pid = f"SRMP{uuid.uuid4().hex[:8].upper()}"
    db.execute("INSERT INTO patients (id, name, mrn, status, ward, bed, registered_facility_id, "
               "user_id) VALUES (?, ?, ?, 'admitted', ?, 'R-1', ?, ?)",
               (pid, name, f"SRMMRN{uuid.uuid4().hex[:8]}", dept, facility, user_id))
    db.execute("INSERT INTO admissions (id, patient_id, admitted_by, department, ward, bed, "
               "status, facility_id, created_at) VALUES (?, ?, 'system', ?, ?, 'R-1', "
               "'active', ?, ?)",
               (f"SRMA{uuid.uuid4().hex[:8].upper()}", pid, dept, dept, facility, utc_iso_now()))
    return pid


@pytest.fixture(scope="module")
def world(isolated_module_database):
    with db_session() as db:
        db.execute("INSERT INTO departments (name, facility_id, seeded_from) VALUES (?, 1, 'test')",
                   (F1_DEPT,))
        db.execute("INSERT INTO departments (name, facility_id, seeded_from) VALUES (?, ?, 'test')",
                   (F2_DEPT, F2))
        users = {
            "admin_f1": _user(db, "admin_f1", "admin", None, home=1),
            "admin_f2": _user(db, "admin_f2", "admin", None, home=F2),
            "group": _user(db, "group", "admin", None, home=1),
            "nurse_f1": _user(db, "nurse_f1", "nurse", F1_DEPT),
            "nurse_f2": _user(db, "nurse_f2", "nurse", F2_DEPT),
            "unlisted": _user(db, "unlisted", "nurse", UNLISTED_DEPT),
            "portal": _user(db, "portal", "patient", None),
        }
        db.execute("INSERT INTO enterprise_access_grants (user_id, granted_by, granted_at, "
                   "reason) VALUES (?, 'system', CURRENT_TIMESTAMP, 'socket rooms test')",
                   (users["group"]["id"],))
        p1 = _patient(db, "Wembric Haldorsen", F1_DEPT, 1)
        p2 = _patient(db, "Ysolde Tarrowfield", F2_DEPT, F2, user_id=users["portal"]["id"])
    yield {"users": users, "p1": p1, "p2": p2}
    FS.invalidate_cache()


def _clear_socket_caches() -> None:
    realtime._patient_dept_cache.clear()
    realtime._patient_name_cache.clear()
    realtime._patient_facility_cache.clear()
    realtime._FACILITY_ROOMS.clear()


@pytest.fixture(autouse=True)
def _fresh_caches():
    FS.invalidate_cache()
    _clear_socket_caches()
    yield
    FS.invalidate_cache()
    _clear_socket_caches()


@pytest.fixture()
def two_facilities(world):
    with db_session() as db:
        db.execute("INSERT INTO facilities (id, uuid, code, name) VALUES (?, ?, ?, ?)",
                   (F2, str(uuid.uuid4()), F2_CODE, "Tallowmere Satellite"))
    FS.invalidate_cache()
    assert FS.multi_facility_data() is True
    try:
        yield
    finally:
        with db_session() as db:
            db.execute("DELETE FROM facilities WHERE id = ?", (F2,))
        FS.invalidate_cache()


def _token(user: dict) -> str:
    return create_token(user["id"], user["role"], token_version=0)


def _connect(monkeypatch, user: dict) -> set:
    """Run the real handshake (real token, real _authenticate) and return the
    rooms the socket joined."""
    rooms: set = set()
    sid = "sid-" + user["id"]

    async def save_session(_sid, session):
        return None

    async def enter_room(_sid, room):
        rooms.add(room)

    monkeypatch.setattr(realtime.sio, "save_session", save_session)
    monkeypatch.setattr(realtime.sio, "enter_room", enter_room)
    try:
        assert asyncio.run(realtime.connect(sid, {}, {"token": _token(user)})) is True
    finally:
        realtime.sio.session_principals.pop(sid, None)
    return rooms


def _emits(monkeypatch, patient_id: str) -> list:
    sent: list = []

    async def fake_emit(event, payload, room=None, **kwargs):
        sent.append((event, room))

    monkeypatch.setattr(realtime.sio, "emit", fake_emit)
    asyncio.run(realtime.emit_patient_vitals("vitals:update", {"patient_id": patient_id}))
    return sent


# ═══════════════════════════════════════════════════════════════════════════
# 1. One facility on record: the rooms are what they were
# ═══════════════════════════════════════════════════════════════════════════

def test_with_one_facility_the_handshake_and_rooms_are_unchanged(world, monkeypatch):
    u = world["users"]
    sock = realtime._authenticate({"token": _token(u["admin_f2"])})
    assert sock is not None
    assert sock[realtime.MULTI_FACILITY_KEY] is False
    assert sock[realtime.FACILITY_SCOPE_KEY] is None
    assert _connect(monkeypatch, u["admin_f2"]) == {f"user:{u['admin_f2']['id']}", "dept:__all__"}
    assert _connect(monkeypatch, u["group"]) == {f"user:{u['group']['id']}", "dept:__all__"}
    assert _connect(monkeypatch, u["nurse_f1"]) == {f"user:{u['nurse_f1']['id']}",
                                                     f"dept:{F1_DEPT}"}
    assert _connect(monkeypatch, u["unlisted"]) == {f"user:{u['unlisted']['id']}",
                                                     f"dept:{UNLISTED_DEPT}"}


def test_with_one_facility_an_update_goes_to_the_department_and_whole_census_rooms(
        world, monkeypatch):
    sent = _emits(monkeypatch, world["p1"])
    assert sent == [("vitals:update", "dept:__all__"), ("vitals:update", f"dept:{F1_DEPT}")]
    assert realtime._patient_facility_cache == {}, "the facility was looked up with one facility"


def test_with_one_facility_the_queue_nudge_rooms_are_unchanged(world):
    assert realtime.queue_nudge_rooms(F1_DEPT) == [
        f"dept:{F1_DEPT}", "dept:__all__", f"opd:{F1_DEPT}", "opd:__all__"]


def test_the_room_rules_as_a_table():
    fs2 = frozenset({F2})
    assert realtime.vitals_rooms_for("Ward A", None, False) == ["dept:Ward A"]
    assert realtime.vitals_rooms_for(None, None, False) == ["dept:__all__"]
    assert realtime.vitals_rooms_for(None, fs2, True) == [f"fac:{F2}:all"]
    assert realtime.vitals_rooms_for(None, frozenset({1, F2}), True) == ["fac:1:all",
                                                                         f"fac:{F2}:all"]
    assert realtime.vitals_rooms_for(None, None, True) == ["grp:all"]
    assert realtime.vitals_rooms_for("Ward A", fs2, True) == ["dept:Ward A"]
    assert realtime.vitals_rooms_for("Ward A", frozenset(), True) == []
    assert realtime.vitals_rooms_for(None, frozenset(), True) == []
    assert realtime.patient_vitals_rooms("Ward A", F2) == ["dept:Ward A", f"fac:{F2}:all",
                                                           "grp:all"]
    assert realtime.patient_vitals_rooms(None, None) == ["grp:all"]


# ═══════════════════════════════════════════════════════════════════════════
# 2. Two facilities: who joins what
# ═══════════════════════════════════════════════════════════════════════════

def test_the_handshake_carries_the_shared_resolvers_scope(world, two_facilities):
    u = world["users"]
    for key, expected in (("admin_f2", frozenset({F2})), ("admin_f1", frozenset({1})),
                          ("group", None), ("nurse_f1", frozenset({1})),
                          ("nurse_f2", frozenset({F2})), ("unlisted", frozenset()),
                          ("portal", None)):
        sock = realtime._authenticate({"token": _token(u[key])})
        assert sock is not None, key
        assert sock[realtime.MULTI_FACILITY_KEY] is True, key
        assert sock[realtime.FACILITY_SCOPE_KEY] == expected == FS.facility_scope_for(u[key]), key


def test_each_socket_joins_the_rooms_of_its_facility(world, two_facilities, monkeypatch):
    u = world["users"]

    def mine(key):
        return {r for r in _connect(monkeypatch, u[key]) if r != f"user:{u[key]['id']}"}

    assert mine("admin_f2") == {f"fac:{F2}:all"}
    assert mine("admin_f1") == {"fac:1:all"}
    assert mine("group") == {"grp:all"}
    assert mine("nurse_f1") == {f"dept:{F1_DEPT}"}
    assert mine("nurse_f2") == {f"dept:{F2_DEPT}"}
    assert mine("unlisted") == set(), "a department no facility lists joined a vitals room"
    assert mine("portal") == {f"patient-self:{world['p2']}"}
    assert realtime._FACILITY_ROOMS == {"fac:1:all", f"fac:{F2}:all"}


def test_an_update_goes_to_the_patients_own_rooms_in_one_emit(world, two_facilities,
                                                              monkeypatch):
    assert _emits(monkeypatch, world["p1"]) == [
        ("vitals:update", [f"dept:{F1_DEPT}", "fac:1:all", "grp:all"])]
    assert _emits(monkeypatch, world["p2"]) == [
        ("vitals:update", [f"dept:{F2_DEPT}", f"fac:{F2}:all", "grp:all"])]


def test_an_f2_socket_never_hears_an_f1_update_and_the_group_hears_both(
        world, two_facilities, monkeypatch):
    u = world["users"]
    mgr = AsyncServer(async_mode="asgi").manager
    mgr.initialize()
    sids = {}
    for key in ("admin_f1", "admin_f2", "group", "nurse_f1", "nurse_f2", "unlisted", "portal"):
        rooms = _connect(monkeypatch, u[key])
        sid = asyncio.run(mgr.connect(f"eio-{key}", "/"))
        for room in rooms:
            mgr.basic_enter_room(sid, "/", room)
        sids[key] = sid
    # A socket that joined before the second facility appeared, still in the
    # whole-census room: it must not hear an update either.
    stale = asyncio.run(mgr.connect("eio-stale", "/"))
    mgr.basic_enter_room(stale, "/", "dept:__all__")

    def audience(patient_id):
        rooms = _emits(monkeypatch, patient_id)[0][1]
        heard = {s for s, _ in mgr.get_participants("/", rooms)}
        return {k for k, s in sids.items() if s in heard} | ({"stale"} if stale in heard else set())

    assert audience(world["p1"]) == {"admin_f1", "group", "nurse_f1"}
    assert audience(world["p2"]) == {"admin_f2", "group", "nurse_f2"}


def test_the_patient_facility_is_the_rest_scope_derivation(world, two_facilities):
    with db_session() as db:
        for pid in (world["p1"], world["p2"]):
            row = db.execute(f"SELECT {FACILITY_PATIENT_SQL} FROM patients p WHERE p.id = ?",
                             (pid,)).fetchone()
            assert realtime._patient_facility(pid) == int(row[0])
    assert realtime._patient_facility(world["p2"]) == F2
    assert realtime._patient_facility("NO-SUCH-PATIENT") is None
    assert realtime._cached_facility(world["p2"]) == (True, F2)


def test_the_queue_nudge_reaches_the_facility_rooms(world, two_facilities, monkeypatch):
    _connect(monkeypatch, world["users"]["admin_f2"])
    rooms = realtime.queue_nudge_rooms(F2_DEPT)
    assert rooms[:2] == [f"dept:{F2_DEPT}", "dept:__all__"]
    assert "grp:all" in rooms and f"fac:{F2}:all" in rooms
    assert rooms[-2:] == [f"opd:{F2_DEPT}", "opd:__all__"]


def test_an_unreadable_facility_scope_refuses_the_socket(world, two_facilities, monkeypatch):
    def boom(user, *, db=None):
        raise FS.FacilityScopeError("maps unreadable")

    monkeypatch.setattr(FS, "facility_scope_for", boom)
    assert realtime._authenticate({"token": _token(world["users"]["admin_f2"])}) is None


# ═══════════════════════════════════════════════════════════════════════════
# 3. A changed facility view drops the socket on its next delivery
# ═══════════════════════════════════════════════════════════════════════════

def _fresh(monkeypatch, original: dict) -> tuple:
    dropped: list = []

    async def disconnect(sid, *a, **k):
        dropped.append(sid)

    monkeypatch.setattr(realtime.sio, "disconnect", disconnect)
    sid = f"sid-fresh-{uuid.uuid4().hex[:6]}"
    realtime.sio.session_principals[sid] = original
    try:
        user = asyncio.run(realtime.sio._fresh(sid))
    finally:
        realtime.sio.session_principals.pop(sid, None)
    return user, dropped


def test_an_unchanged_view_keeps_the_socket(world, two_facilities, monkeypatch):
    original = realtime._authenticate({"token": _token(world["users"]["admin_f2"])})
    user, dropped = _fresh(monkeypatch, original)
    assert user is not None and dropped == []


def test_a_socket_from_before_the_second_facility_is_dropped(world, monkeypatch):
    original = realtime._authenticate({"token": _token(world["users"]["group"])})
    assert original[realtime.MULTI_FACILITY_KEY] is False
    with db_session() as db:
        db.execute("INSERT INTO facilities (id, uuid, code, name) VALUES (?, ?, ?, ?)",
                   (F2, str(uuid.uuid4()), F2_CODE, "Tallowmere Satellite"))
    FS.invalidate_cache()
    try:
        # The group account's scope is None both times; the flag alone drops it,
        # because its room changes from 'dept:__all__' to 'grp:all'.
        user, dropped = _fresh(monkeypatch, original)
        assert user is None and len(dropped) == 1
    finally:
        with db_session() as db:
            db.execute("DELETE FROM facilities WHERE id = ?", (F2,))
        FS.invalidate_cache()


def test_a_home_facility_move_drops_the_socket(world, two_facilities, monkeypatch):
    admin = world["users"]["admin_f2"]
    original = realtime._authenticate({"token": _token(admin)})
    assert original[realtime.FACILITY_SCOPE_KEY] == frozenset({F2})
    with db_session() as db:
        db.execute("UPDATE user_facilities SET facility_id = 1 WHERE user_id = ? AND kind = 'home'",
                   (admin["id"],))
    FS.invalidate_cache()
    try:
        user, dropped = _fresh(monkeypatch, original)
        assert user is None and len(dropped) == 1
    finally:
        with db_session() as db:
            db.execute("UPDATE user_facilities SET facility_id = ? WHERE user_id = ? "
                       "AND kind = 'home'", (F2, admin["id"]))
        FS.invalidate_cache()


def test_a_revoked_group_grant_drops_the_socket(world, two_facilities, monkeypatch):
    group = world["users"]["group"]
    original = realtime._authenticate({"token": _token(group)})
    assert original[realtime.FACILITY_SCOPE_KEY] is None
    with db_session() as db:
        db.execute("UPDATE enterprise_access_grants SET revoked_at = CURRENT_TIMESTAMP, "
                   "revoked_by = 'system', revoke_reason = 'socket rooms test' "
                   "WHERE user_id = ?", (group["id"],))
    FS.invalidate_cache()
    try:
        user, dropped = _fresh(monkeypatch, original)
        assert user is None and len(dropped) == 1
    finally:
        with db_session() as db:
            db.execute("UPDATE enterprise_access_grants SET revoked_at = NULL, revoked_by = NULL, "
                       "revoke_reason = NULL WHERE user_id = ?", (group["id"],))
        FS.invalidate_cache()
