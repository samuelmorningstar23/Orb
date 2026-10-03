"""The hospital-system role batch and its grants (his-2, wave 1, 3 October 2026).

Five roles in one users rebuild (migration 48): receptionist, ward_clerk,
lab_technician, pathologist, phlebotomist. Every HIS permission and every v1
grant is registered now, before the routes that use them, so this file pins
each one: a change to who holds a permission is a deliberate edit here and in
src/lib/roles.ts. Every grant is inert while its switch is off, because every
route that checks it answers 404.

User Management refuses to create one of these roles, or to move an account
into one, while its switch is off.
"""
import uuid

import pytest
from fastapi.testclient import TestClient

import config
import migrations
import permissions as P
import roles
from auth import create_token, hash_password
from database import db_session
from main import app

client = TestClient(app)

NEW_ROLES = ("receptionist", "ward_clerk", "lab_technician", "pathologist", "phlebotomist")

HIS_PERMISSIONS = {
    "registration.read", "registration.write", "opd.consult", "schedule.manage",
    "adt.read", "adt.bed", "adt.transfer", "adt.presence", "bed.master",
    "pharmacy_store.read", "pharmacy_store.write", "pharmacy_store.approve",
    "pharmacy_store.ndps", "ward_stock.read", "ward_stock.receive",
    "ward_return.record", "ward_indent.raise",
    "lab.read", "lab.collect", "lab.receive", "lab.result", "lab.authorise",
    "lab.master", "lab.critical_log", "lab.critical_ack", "lab.interface",
    "ingest.reconcile",
}

# The v1 grants, exactly. Mirrored in src/lib/roles.ts (W1-B).
EXPECTED_HIS_GRANTS = {
    "doctor": {"registration.read", "registration.write", "opd.consult", "adt.read",
               "lab.collect", "lab.critical_ack"},
    "nurse": {"ward_stock.read", "ward_stock.receive", "ward_indent.raise",
              "ward_return.record", "lab.collect", "lab.critical_ack"},
    "pharmacist": {"pharmacy_store.read", "pharmacy_store.write", "pharmacy_store.ndps"},
    "admin": {"registration.read", "schedule.manage",
              "adt.read", "adt.bed", "adt.transfer", "adt.presence", "bed.master",
              "pharmacy_store.read", "pharmacy_store.approve",
              "lab.read", "lab.interface"},
    "patient": set(),
    "cashier": set(),
    "receptionist": {"registration.read", "registration.write"},
    "ward_clerk": {"adt.read", "adt.bed"},
    "phlebotomist": {"lab.read", "lab.collect"},
    "lab_technician": {"lab.read", "lab.collect", "lab.receive", "lab.result",
                       "lab.critical_log", "ingest.reconcile"},
    "pathologist": {"lab.read", "lab.collect", "lab.receive", "lab.result",
                    "lab.critical_log", "ingest.reconcile", "lab.authorise", "lab.master"},
}


# ── 1. The role authority ─────────────────────────────────────────────────────

def test_the_five_roles_are_storable_and_frozen_by_migration_48():
    from migrations_his.m0048_users_role_batch import FROZEN_ROLES
    assert set(NEW_ROLES) <= set(roles.STORABLE_ROLES)
    assert tuple(FROZEN_ROLES) == tuple(migrations.LATEST_USERS_ROLE_CHECK) == tuple(
        roles.STORABLE_ROLES)


def test_every_new_role_needs_its_switch():
    assert roles.ROLE_FEATURE == {
        "receptionist": "registration", "ward_clerk": "adt",
        "lab_technician": "lab", "pathologist": "lab", "phlebotomist": "lab"}
    for switch in roles.ROLE_FEATURE.values():
        assert switch in config.FEATURE_FLAGS or switch in config.OPT_IN_MODULES
    from routes.admin_users_routes import _ROLE_SWITCH_OFF_WORDS
    assert set(_ROLE_SWITCH_OFF_WORDS) == set(roles.ROLE_FEATURE)
    for words in _ROLE_SWITCH_OFF_WORDS.values():
        assert "switched off" in words and "_" not in words


def test_department_rules():
    assert P.DEPARTMENT_REQUIRED_ROLES == frozenset({"doctor", "nurse", "ward_clerk"})
    for role in ("receptionist", "lab_technician", "pathologist", "phlebotomist"):
        assert role not in P.DEPARTMENT_REQUIRED_ROLES


# ── 2. The grants, pinned ─────────────────────────────────────────────────────

def test_every_his_permission_constant_is_defined_once():
    defined = {v for k, v in vars(P).items()
               if k.isupper() and isinstance(v, str) and "." in v}
    assert HIS_PERMISSIONS <= defined


@pytest.mark.parametrize("role", sorted(EXPECTED_HIS_GRANTS))
def test_his_grants_per_role_are_exactly_the_ruled_set(role):
    held = P.ROLE_PERMISSIONS[role] & HIS_PERMISSIONS
    assert held == EXPECTED_HIS_GRANTS[role], role


def test_new_roles_hold_nothing_outside_the_his_set():
    for role in NEW_ROLES:
        assert P.ROLE_PERMISSIONS[role] <= HIS_PERMISSIONS, role


def test_the_clinician_alias_carries_the_doctor_grants():
    assert P.ROLE_PERMISSIONS["clinician"] is P.ROLE_PERMISSIONS["doctor"]


# grn-2: every defined-but-ungranted permission is pinned here as deliberate.
# On 3 October 2026 there are none: each permission in permissions.py has a
# holder. A permission defined ahead of its first holder goes in this set with
# the reason, in the same change.
DELIBERATELY_UNGRANTED: set = set()


def test_every_defined_permission_is_granted_or_pinned_as_deliberate():
    import re
    defined = {v for k, v in vars(P).items()
               if k.isupper() and isinstance(v, str) and re.fullmatch(r"[a-z_]+\.[a-z_]+", v)}
    granted = set().union(*P.ROLE_PERMISSIONS.values())
    assert defined - granted == DELIBERATELY_UNGRANTED
    assert HIS_PERMISSIONS <= granted


def test_lab_roles_reach_no_chart_no_relay_no_sage():
    for role in ("lab_technician", "pathologist", "phlebotomist"):
        perms = P.ROLE_PERMISSIONS[role]
        assert P.PATIENT_READ_ALL not in perms
        assert P.SAGE_QUERY not in perms
        assert not any(p.startswith("relay.") for p in perms)


def test_ingest_reconcile_never_on_admin_or_a_department_scoped_role():
    """LIS-R17: the quarantine queue shows raw identifiers hospital-wide."""
    assert P.INGEST_RECONCILE not in P.ROLE_PERMISSIONS["admin"]
    for role in P.DEPARTMENT_REQUIRED_ROLES:
        assert P.INGEST_RECONCILE not in P.ROLE_PERMISSIONS[role], role
    holders = {r for r, perms in P.ROLE_PERMISSIONS.items() if P.INGEST_RECONCILE in perms}
    assert holders == {"lab_technician", "pathologist"}


def test_only_the_pathologist_authorises():
    holders = {r for r, perms in P.ROLE_PERMISSIONS.items() if P.LAB_AUTHORISE in perms}
    assert holders == {"pathologist"}


def test_strict_adt_defaults_for_held_rulings():
    # ADT-S7 held: nurses get nothing new, presence is the admin's (doctors
    # reach it through patient.write).
    assert not {p for p in P.ROLE_PERMISSIONS["nurse"] if p.startswith("adt.")}
    holders = {r for r, perms in P.ROLE_PERMISSIONS.items() if P.ADT_PRESENCE in perms}
    assert holders == {"admin"}
    # ADT-S8 held: the clerk never changes the treating department.
    assert P.ADT_TRANSFER not in P.ROLE_PERMISSIONS["ward_clerk"]
    # The clerk opens no chart.
    assert P.PATIENT_READ_ALL not in P.ROLE_PERMISSIONS["ward_clerk"]


def test_admin_never_registers_and_never_writes_the_pharmacy_store():
    assert P.REGISTRATION_WRITE not in P.ROLE_PERMISSIONS["admin"]
    assert P.OPD_CONSULT not in P.ROLE_PERMISSIONS["admin"]
    assert P.PHARMACY_STORE_WRITE not in P.ROLE_PERMISSIONS["admin"]


def test_the_receptionist_holds_no_money_consent_or_transfer():
    perms = P.ROLE_PERMISSIONS["receptionist"]
    for p in (P.PAYMENTS_RECORD, P.CONSENT_RECORD, P.FRONTDESK_WRITE, P.PATIENT_WRITE,
              P.PATIENT_READ_ALL):
        assert p not in perms


# ── 3. User Management ────────────────────────────────────────────────────────

def _mk(role, department=None):
    uid = f"RB{uuid.uuid4().hex[:6].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO users (id, name, role, department, password_hash) VALUES (?, ?, ?, ?, ?)",
            (uid, f"{role} {uid}", role, department, hash_password("password")))
    return uid


@pytest.fixture()
def admin_hdr():
    uid = _mk("admin")
    return {"Authorization": f"Bearer {create_token(uid, 'admin')}"}


def _create(hdr, role, department=None):
    uid = f"RB{uuid.uuid4().hex[:6].upper()}"
    resp = client.post("/api/admin/users", headers=hdr, json={
        "id": uid, "name": f"New {role}", "role": role, "department": department,
        "password": "password1"})
    return uid, resp


@pytest.mark.parametrize("role,department", [
    ("receptionist", None), ("ward_clerk", "Cardiology"), ("lab_technician", None),
    ("pathologist", None), ("phlebotomist", None)])
def test_a_role_is_refused_while_its_switch_is_off(admin_hdr, role, department):
    uid, resp = _create(admin_hdr, role, department)
    assert resp.status_code == 400, resp.text
    assert "switched off" in resp.json()["detail"]
    with db_session() as db:
        assert db.execute("SELECT 1 FROM users WHERE id = ?", (uid,)).fetchone() is None


@pytest.mark.parametrize("role,department,switch", [
    ("receptionist", None, "REGISTRATION_ENABLED"),
    ("ward_clerk", "Cardiology", "ADT_ENABLED"),
    ("lab_technician", None, "LAB_ENABLED"),
    ("pathologist", None, "LAB_ENABLED"),
    ("phlebotomist", None, "LAB_ENABLED")])
def test_a_role_is_created_once_its_switch_is_on(monkeypatch, admin_hdr, role, department, switch):
    monkeypatch.setattr(config, switch, True)
    uid, resp = _create(admin_hdr, role, department)
    assert resp.status_code == 201, resp.text
    with db_session() as db:
        row = db.execute("SELECT role, department FROM users WHERE id = ?", (uid,)).fetchone()
    assert row["role"] == role and row["department"] == department


def test_a_ward_clerk_needs_a_department(monkeypatch, admin_hdr):
    monkeypatch.setattr(config, "ADT_ENABLED", True)
    _, resp = _create(admin_hdr, "ward_clerk", None)
    assert resp.status_code == 400
    assert resp.json()["detail"] == "Department is required for a ward clerk"


def test_a_draining_laboratory_still_lets_lab_staff_be_created(monkeypatch, admin_hdr):
    """While open lab work drains, the lab may need a technician to finish it."""
    monkeypatch.setattr(config, "_MODULE_DRAIN_CHECKS", {"lab": lambda: True})
    _, resp = _create(admin_hdr, "lab_technician")
    assert resp.status_code == 201, resp.text


def test_moving_an_account_into_a_switched_off_role_is_refused(admin_hdr):
    nurse = _mk("nurse", "Cardiology")
    resp = client.patch(f"/api/admin/users/{nurse}", headers=admin_hdr,
                        json={"role": "ward_clerk"})
    assert resp.status_code == 400, resp.text
    assert "switched off" in resp.json()["detail"]
    with db_session() as db:
        assert db.execute("SELECT role FROM users WHERE id = ?", (nurse,)).fetchone()["role"] == "nurse"


def test_an_existing_account_keeps_its_role_when_the_switch_goes_off(admin_hdr):
    """Editing an existing clerk's department is not creating a clerk."""
    clerk = _mk("ward_clerk", "Cardiology")
    resp = client.patch(f"/api/admin/users/{clerk}", headers=admin_hdr,
                        json={"department": "Oncology"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["role"] == "ward_clerk"


def test_existing_roles_are_unaffected_by_the_switches(admin_hdr):
    _, resp = _create(admin_hdr, "cashier")
    assert resp.status_code == 201, resp.text
    _, resp = _create(admin_hdr, "nurse", "Cardiology")
    assert resp.status_code == 201, resp.text


# ── 4. A new role holds nothing that opens a chart or the old desks ──────────

@pytest.mark.parametrize("role,department", [
    ("receptionist", None), ("ward_clerk", "Cardiology"), ("lab_technician", None),
    ("pathologist", None), ("phlebotomist", None)])
def test_a_new_role_cannot_reach_the_ward_list_or_a_chart(role, department):
    uid = _mk(role, department)
    hdr = {"Authorization": f"Bearer {create_token(uid, role)}"}
    assert client.get("/api/vigil/patients", headers=hdr).status_code == 403
    # A chart: refused on the permission, before any patient is looked up.
    assert client.get("/api/vigil/patients/ANY-PATIENT", headers=hdr).status_code == 403
    assert client.get("/api/payments/receipts", headers=hdr).status_code in (403, 404)
    assert client.get("/api/admin/users", headers=hdr).status_code == 403


# ── 5. No Relay for the new roles, on the websocket lane too ─────────────────
# relay_service.get_channels_for_user is Relay's one access boundary, and the
# websocket's join_channel authorises only through it (it never checks
# relay.read). Its case-room branch gives a department-less account every case
# room, which a lab role always is. So the new roles are refused there, and
# join_channel is driven here for real with only the socket server stubbed.

def _relay_fixture():
    """A Cardiology case room, a Cardiology staff room and a direct thread."""
    tag = uuid.uuid4().hex[:6].upper()
    doctor = _mk("doctor", "Cardiology")
    pid = f"RBP-{tag}"
    case_id, dept_id, dm_id = f"case-{pid}", f"dept-rb-{tag}", f"dm-rb-{tag}"
    with db_session() as db:
        db.execute(
            "INSERT INTO patients (id, name, mrn, bed, ward, status) VALUES (?, ?, ?, ?, ?, ?)",
            (pid, "Relay Probe", f"MRN-{pid}", "B9", "CARD-1", "admitted"))
        db.execute(
            "INSERT INTO admissions (id, patient_id, department, status, admitted_by) "
            "VALUES (?, ?, 'Cardiology', 'active', ?)", (f"ADM-{pid}", pid, doctor))
        db.execute("INSERT INTO channels (id, name, type, patient_id) VALUES (?, ?, 'case_room', ?)",
                   (case_id, f"Case {pid}", pid))
        db.execute("INSERT INTO channels (id, name, type) VALUES (?, 'Cardiology', 'department')",
                   (dept_id,))
        db.execute("INSERT INTO channels (id, name, type) VALUES (?, ?, 'direct')",
                   (dm_id, f"Thread {tag}"))
    return case_id, dept_id, dm_id


def _join(monkeypatch, uid, role, department, channel_id):
    import asyncio
    import ws.realtime as rt
    sid = f"sid-{uid}"
    entered, errors = [], []

    async def get_session(s):
        return {"user_id": uid, "name": uid, "role": role, "department": department}

    async def enter_room(s, room):
        entered.append((s, room))

    async def emit(event, data=None, **kw):
        errors.append((event, data))

    monkeypatch.setattr(rt.sio, "get_session", get_session)
    monkeypatch.setattr(rt.sio, "enter_room", enter_room)
    monkeypatch.setattr(rt.sio, "emit", emit)
    asyncio.run(rt.join_channel(sid, {"channel_id": channel_id}))
    return (sid, channel_id) in entered, errors


@pytest.mark.parametrize("role,department", [
    ("receptionist", None), ("receptionist", "Cardiology"), ("ward_clerk", "Cardiology"),
    ("lab_technician", None), ("pathologist", None), ("phlebotomist", None)])
def test_a_new_role_cannot_join_a_relay_room_over_the_websocket(monkeypatch, role, department):
    # The switches on, as they must be for such an account to exist at all.
    for switch in ("REGISTRATION_ENABLED", "ADT_ENABLED", "LAB_ENABLED"):
        monkeypatch.setattr(config, switch, True)
    case_id, dept_id, dm_id = _relay_fixture()
    uid = _mk(role, department)
    # Even a membership row written straight into the table opens nothing.
    with db_session() as db:
        db.execute("INSERT INTO channel_members (channel_id, user_id) VALUES (?, ?)", (dm_id, uid))
    for channel_id in (case_id, dept_id, dm_id):
        joined, errors = _join(monkeypatch, uid, role, department, channel_id)
        assert not joined, f"{role}/{department} joined {channel_id}"
        assert errors == [("error", {"error": "not authorized for channel"})]
    from services.relay_service import can_access_channel, get_channels_for_user
    assert get_channels_for_user(uid, role) == []
    assert not can_access_channel(uid, role, case_id)
    # The REST lane is shut on the permission itself.
    hdr = {"Authorization": f"Bearer {create_token(uid, role)}"}
    assert client.get("/api/relay/channels", headers=hdr).status_code == 403
    assert client.get(f"/api/relay/channels/{case_id}/messages", headers=hdr).status_code == 403


def test_older_roles_still_join_relay_rooms_as_before(monkeypatch):
    """The refusal is for the new roles only: a ward nurse, a nurse on another
    ward and an administrator answer exactly as they did."""
    case_id, dept_id, _ = _relay_fixture()
    nurse_here = _mk("nurse", "Cardiology")
    nurse_away = _mk("nurse", "Oncology")
    admin = _mk("admin")
    assert _join(monkeypatch, nurse_here, "nurse", "Cardiology", case_id)[0]
    assert _join(monkeypatch, nurse_here, "nurse", "Cardiology", dept_id)[0]
    assert not _join(monkeypatch, nurse_away, "nurse", "Oncology", case_id)[0]
    assert not _join(monkeypatch, nurse_away, "nurse", "Oncology", dept_id)[0]
    assert _join(monkeypatch, admin, "admin", None, case_id)[0]
    assert _join(monkeypatch, admin, "admin", None, dept_id)[0]


def test_the_relay_refusal_follows_the_permission_not_a_role_list(monkeypatch):
    """Granting relay.read to a new role later opens Relay with no second edit."""
    case_id, _, _ = _relay_fixture()
    uid = _mk("ward_clerk", "Cardiology")
    assert not _join(monkeypatch, uid, "ward_clerk", "Cardiology", case_id)[0]
    monkeypatch.setitem(P.ROLE_PERMISSIONS, "ward_clerk",
                        P.ROLE_PERMISSIONS["ward_clerk"] | {"relay.read"})
    assert _join(monkeypatch, uid, "ward_clerk", "Cardiology", case_id)[0]
