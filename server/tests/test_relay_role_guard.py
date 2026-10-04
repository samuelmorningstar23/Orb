"""Relay: a role without relay.read gets no channel, on every lane.

relay_service.get_channels_for_user is Relay's one access boundary. The REST
routes also check relay.read, but the websocket's join_channel and typing
handlers authorise ONLY by asking that function, and its case-room branch hands
every case room in the hospital to an account with no department. Wave 1 shut
that for the five hospital-system roles; a cashier and a pharmacist are also
department-less with no relay.read, so a cashier could join a patient's case
room over the websocket and read the clinical thread. The guard now follows the
permission for every role.

The pharmacist was checked before closing it: no pharmacist screen uses Relay,
every Relay REST route needs relay.read, and Sage reads case-room text only for
relay.read holders. Doctor, nurse and admin answer exactly as before.
"""
import asyncio
import uuid

import pytest
from fastapi.testclient import TestClient

import permissions as P
from auth import create_token
from database import db_session
from main import app
from services.relay_service import can_access_channel, get_channels_for_user

client = TestClient(app)

# Not a usable bcrypt hash on purpose: these accounts are driven by token and
# by session stub, never by password login.
_NO_LOGIN_HASH = "x-relay-guard-no-login"


def _mk(role, department=None):
    uid = f"RG{uuid.uuid4().hex[:6].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO users (id, name, role, department, password_hash) VALUES (?, ?, ?, ?, ?)",
            (uid, f"{role} {uid}", role, department, _NO_LOGIN_HASH))
    return uid


@pytest.fixture()
def rooms():
    """A Cardiology case room with a message, a Cardiology staff room and a DM."""
    tag = uuid.uuid4().hex[:6].upper()
    doctor = _mk("doctor", "Cardiology")
    pid = f"RGP-{tag}"
    case_id, dept_id, dm_id = f"case-{pid}", f"dept-rg-{tag}", f"dm-rg-{tag}"
    with db_session() as db:
        db.execute(
            "INSERT INTO patients (id, name, mrn, bed, ward, status) VALUES (?, ?, ?, ?, ?, ?)",
            (pid, "Relay Guard Probe", f"MRN-{pid}", "B7", "CARD-1", "admitted"))
        db.execute(
            "INSERT INTO admissions (id, patient_id, department, status, admitted_by) "
            "VALUES (?, ?, 'Cardiology', 'active', ?)", (f"ADM-{pid}", pid, doctor))
        db.execute("INSERT INTO channels (id, name, type, patient_id) VALUES (?, ?, 'case_room', ?)",
                   (case_id, f"Case {pid}", pid))
        db.execute("INSERT INTO channels (id, name, type) VALUES (?, 'Cardiology', 'department')",
                   (dept_id,))
        db.execute("INSERT INTO channels (id, name, type) VALUES (?, ?, 'direct')",
                   (dm_id, f"Thread {tag}"))
        db.execute(
            "INSERT INTO messages (channel_id, sender_id, sender_name, sender_role, content, "
            "message_type) VALUES (?, ?, 'Dr Probe', 'doctor', 'clinical plan for the probe', 'text')",
            (case_id, doctor))
    return {"case": case_id, "dept": dept_id, "dm": dm_id, "doctor": doctor}


def _ws(monkeypatch, handler, uid, role, department, channel_id, **extra):
    """Drive a real ws.realtime handler with only the socket server stubbed."""
    import ws.realtime as rt
    sid = f"sid-{uid}"
    entered, emitted = [], []

    async def get_session(s):
        return {"user_id": uid, "name": uid, "role": role, "department": department}

    async def enter_room(s, room):
        entered.append((s, room))

    async def emit(event, data=None, **kw):
        emitted.append((event, data, kw))

    monkeypatch.setattr(rt.sio, "get_session", get_session)
    monkeypatch.setattr(rt.sio, "enter_room", enter_room)
    monkeypatch.setattr(rt.sio, "emit", emit)
    asyncio.run(getattr(rt, handler)(sid, {"channel_id": channel_id, **extra}))
    return (sid, channel_id) in entered, emitted


@pytest.mark.parametrize("role", ["cashier", "pharmacist"])
def test_a_role_without_relay_read_gets_no_case_room_on_any_lane(monkeypatch, rooms, role):
    uid = _mk(role, None)
    # Even a membership row written straight into the table opens nothing.
    with db_session() as db:
        db.execute("INSERT INTO channel_members (channel_id, user_id) VALUES (?, ?)",
                   (rooms["dm"], uid))

    assert not P.has_permission(role, P.RELAY_READ)
    assert get_channels_for_user(uid, role) == []
    for channel_id in (rooms["case"], rooms["dept"], rooms["dm"]):
        assert not can_access_channel(uid, role, channel_id)
        joined, emitted = _ws(monkeypatch, "join_channel", uid, role, None, channel_id)
        assert not joined, f"{role} joined {channel_id} over the websocket"
        assert [(e, d) for e, d, _ in emitted] == [("error", {"error": "not authorized for channel"})]

    # Typing into the room is refused by the same boundary: nothing is broadcast.
    _, emitted = _ws(monkeypatch, "typing", uid, role, None, rooms["case"])
    assert emitted == []

    # The REST lane is shut on the permission itself.
    hdr = {"Authorization": f"Bearer {create_token(uid, role)}"}
    assert client.get("/api/relay/channels", headers=hdr).status_code == 403
    resp = client.get(f"/api/relay/channels/{rooms['case']}/messages", headers=hdr)
    assert resp.status_code == 403
    assert "clinical plan for the probe" not in resp.text


def test_patient_and_unknown_roles_get_no_channel(rooms):
    pt = _mk("patient", None)
    assert get_channels_for_user(pt, "patient") == []
    assert get_channels_for_user(pt, "") == []
    assert get_channels_for_user(pt, "not_a_role") == []


def test_doctor_nurse_and_admin_are_unchanged(monkeypatch, rooms):
    doc_here = _mk("doctor", "Cardiology")
    nurse_here = _mk("nurse", "Cardiology")
    doc_away = _mk("doctor", "Oncology")
    nurse_away = _mk("nurse", "Oncology")
    admin = _mk("admin", None)

    for uid, role in ((doc_here, "doctor"), (nurse_here, "nurse")):
        visible = {c["id"] for c in get_channels_for_user(uid, role)}
        assert {rooms["case"], rooms["dept"]} <= visible, role
        assert rooms["dm"] not in visible, f"{role} saw a DM they are not a member of"
        assert _ws(monkeypatch, "join_channel", uid, role, "Cardiology", rooms["case"])[0]
        assert _ws(monkeypatch, "join_channel", uid, role, "Cardiology", rooms["dept"])[0]
        hdr = {"Authorization": f"Bearer {create_token(uid, role)}"}
        resp = client.get(f"/api/relay/channels/{rooms['case']}/messages", headers=hdr)
        assert resp.status_code == 200, resp.text

    for uid, role in ((doc_away, "doctor"), (nurse_away, "nurse")):
        visible = {c["id"] for c in get_channels_for_user(uid, role)}
        assert rooms["case"] not in visible and rooms["dept"] not in visible, role
        assert not _ws(monkeypatch, "join_channel", uid, role, "Oncology", rooms["case"])[0]

    visible = {c["id"] for c in get_channels_for_user(admin, "admin")}
    assert {rooms["case"], rooms["dept"]} <= visible
    assert rooms["dm"] not in visible


def test_clinician_alias_is_unchanged(rooms):
    uid = _mk("doctor", "Cardiology")
    assert {c["id"] for c in get_channels_for_user(uid, "clinician")} == {
        c["id"] for c in get_channels_for_user(uid, "doctor")}


def test_blank_department_rule_still_holds_for_a_relay_role(rooms):
    """The department-less substring rule (no department, no staff room) used
    to be pinned through a pharmacist in test_pharmacy_not_a_ward.py. The
    pharmacist now stops at the permission guard first, so pin the rule here
    through a relay.read role: a doctor row with no department gets case rooms
    (unscoped, as department_scope documents) but no department staff room."""
    uid = _mk("doctor", None)
    visible = {c["id"]: c for c in get_channels_for_user(uid, "doctor")}
    assert rooms["case"] in visible
    assert rooms["dept"] not in visible


def test_granting_relay_read_opens_relay_with_no_second_edit(monkeypatch, rooms):
    uid = _mk("cashier", None)
    assert get_channels_for_user(uid, "cashier") == []
    monkeypatch.setitem(P.ROLE_PERMISSIONS, "cashier",
                        P.ROLE_PERMISSIONS["cashier"] | {P.RELAY_READ})
    visible = {c["id"] for c in get_channels_for_user(uid, "cashier")}
    assert rooms["case"] in visible
    assert rooms["dept"] not in visible
