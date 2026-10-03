"""User Management guards: service identities, patient accounts, departments.

bot_vigil, bot_lab and friends are users rows with role 'doctor', no department
and a sentinel hash. One admin password reset turned such a row into a working
hospital-wide doctor login, and the staff list offered exactly that button on
every bot, patient and the admin's own row. A doctor or nurse created with a
blank department read every ward (permissions.department_scope).
"""
import uuid

import pytest
from fastapi.testclient import TestClient

from auth import create_token, hash_password
from database import SERVICE_ACCOUNT_IDS, db_session
from main import app

client = TestClient(app)


def _mk(role, department=None, uid=None):
    uid = uid or f"UG{uuid.uuid4().hex[:6].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT OR IGNORE INTO users (id, name, role, department, password_hash) "
            "VALUES (?, ?, ?, ?, ?)",
            (uid, f"{role.title()} {uid}", role, department, hash_password("password")),
        )
    return uid


def _ensure_bots():
    with db_session() as db:
        for uid in ("bot_vigil", "bot_lab"):
            db.execute("INSERT OR IGNORE INTO users (id, name, role, password_hash) "
                       "VALUES (?, ?, 'doctor', 'system')", (uid, uid))


@pytest.fixture()
def admin():
    uid = _mk("admin")
    return uid, {"Authorization": f"Bearer {create_token(uid, 'admin')}"}


def _ids(resp):
    assert resp.status_code == 200, resp.text
    return {u["id"] for u in resp.json()["users"]}


# ── the staff list ────────────────────────────────────────────────────────────
def test_staff_list_leaves_out_service_accounts_and_patients(admin):
    _, hdr = admin
    _ensure_bots()
    patient = _mk("patient", "General Medicine")
    nurse = _mk("nurse", "Cardiology")
    ids = _ids(client.get("/api/admin/users", headers=hdr))
    assert nurse in ids
    assert patient not in ids
    assert not ids & SERVICE_ACCOUNT_IDS

    with_patients = _ids(client.get("/api/admin/users?include_patients=true", headers=hdr))
    assert patient in with_patients
    assert not with_patients & SERVICE_ACCOUNT_IDS


def test_staff_list_marks_the_admins_own_row_and_offers_departments(admin):
    uid, hdr = admin
    with db_session() as db:
        db.execute("INSERT OR IGNORE INTO channels (id, name, type) "
                   "VALUES ('dept-guards-test', 'Guards Test Ward', 'department')")
    body = client.get("/api/admin/users", headers=hdr).json()
    own = next(u for u in body["users"] if u["id"] == uid)
    assert own["is_self"] is True
    assert all(u["is_self"] is False for u in body["users"] if u["id"] != uid)
    assert "Guards Test Ward" in body["departments"]


# ── service identities cannot be changed ──────────────────────────────────────
@pytest.mark.parametrize("target", ["system", "bot_vigil", "bot_lab"])
def test_reset_password_on_a_service_account_is_refused(admin, target):
    admin_id, hdr = admin
    _ensure_bots()
    with db_session() as db:
        before = db.execute("SELECT password_hash, token_version FROM users WHERE id = ?",
                            (target,)).fetchone()
    resp = client.post(f"/api/admin/users/{target}/reset-password",
                       json={"new_password": "takeover-123"}, headers=hdr)
    assert resp.status_code == 409, resp.text
    with db_session() as db:
        after = db.execute("SELECT password_hash, token_version FROM users WHERE id = ?",
                           (target,)).fetchone()
        audit = db.execute(
            "SELECT outcome FROM audit_logs WHERE action_type = "
            "'user_service_account_change_denied' AND target_id = ? AND actor_user_id = ?",
            (target, admin_id)).fetchone()
    assert dict(after) == dict(before)
    assert audit is not None and audit["outcome"] == "denied"
    # and it still cannot log in
    login = client.post("/api/auth/login",
                        json={"employee_id": target, "password": "takeover-123"})
    assert login.status_code != 200


@pytest.mark.parametrize("method,path,body", [
    ("patch", "/api/admin/users/bot_vigil", {"role": "admin"}),
    ("post", "/api/admin/users/bot_vigil/activate", None),
    ("post", "/api/admin/users/bot_vigil/deactivate", None),
    ("post", "/api/admin/users/bot_vigil/pin", {"pin": "4321"}),
])
def test_every_change_to_a_service_account_is_refused(admin, method, path, body):
    _, hdr = admin
    _ensure_bots()
    kwargs = {"headers": hdr}
    if body is not None:
        kwargs["json"] = body
    resp = getattr(client, method)(path, **kwargs)
    assert resp.status_code == 409, resp.text
    with db_session() as db:
        row = db.execute("SELECT role, pin_hash, COALESCE(is_active, 1) AS active "
                         "FROM users WHERE id = 'bot_vigil'").fetchone()
    assert row["role"] == "doctor" and row["pin_hash"] is None and row["active"] == 1


@pytest.mark.parametrize("reserved", ["system", "bot_vigil", "bot_newthing", "BOT_LAB"])
def test_service_account_ids_cannot_be_created(admin, reserved):
    _, hdr = admin
    resp = client.post("/api/admin/users", headers=hdr, json={
        "id": reserved, "name": "Not a bot", "role": "admin", "password": "password1"})
    assert resp.status_code == 409, resp.text


# ── patient portal accounts ───────────────────────────────────────────────────
def test_patient_accounts_are_not_created_here(admin):
    _, hdr = admin
    resp = client.post("/api/admin/users", headers=hdr, json={
        "id": f"PX{uuid.uuid4().hex[:6]}", "name": "Portal", "role": "patient",
        "department": "General Medicine", "password": "password1"})
    assert resp.status_code == 400, resp.text


def test_patient_and_staff_roles_do_not_cross(admin):
    _, hdr = admin
    patient = _mk("patient", "General Medicine")
    nurse = _mk("nurse", "Cardiology")
    r1 = client.patch(f"/api/admin/users/{patient}", headers=hdr,
                      json={"role": "doctor", "department": "Cardiology"})
    r2 = client.patch(f"/api/admin/users/{nurse}", headers=hdr, json={"role": "patient"})
    assert r1.status_code == 400 and r2.status_code == 400
    with db_session() as db:
        roles = {r["id"]: r["role"] for r in db.execute(
            "SELECT id, role FROM users WHERE id IN (?, ?)", (patient, nurse)).fetchall()}
    assert roles == {patient: "patient", nurse: "nurse"}


# ── clinical accounts need a department ───────────────────────────────────────
@pytest.mark.parametrize("role", ["nurse", "doctor"])
@pytest.mark.parametrize("department", [None, "", "   "])
def test_clinical_account_without_department_is_refused(admin, role, department):
    _, hdr = admin
    uid = f"UG{uuid.uuid4().hex[:6].upper()}"
    resp = client.post("/api/admin/users", headers=hdr, json={
        "id": uid, "name": "No Ward", "role": role, "department": department,
        "password": "password1"})
    assert resp.status_code == 400, resp.text
    assert resp.json()["detail"] == "Department is required for clinical roles"
    with db_session() as db:
        assert db.execute("SELECT 1 FROM users WHERE id = ?", (uid,)).fetchone() is None


def test_clinical_account_with_department_is_created(admin):
    _, hdr = admin
    uid = f"UG{uuid.uuid4().hex[:6].upper()}"
    resp = client.post("/api/admin/users", headers=hdr, json={
        "id": uid, "name": "Ward Nurse", "role": "nurse", "department": " Cardiology ",
        "password": "password1"})
    assert resp.status_code == 201, resp.text
    assert resp.json()["department"] == "Cardiology"


def test_pharmacist_stays_hospital_wide(admin):
    """Pharmacy is not a ward (migration 36): no department is required."""
    _, hdr = admin
    resp = client.post("/api/admin/users", headers=hdr, json={
        "id": f"UG{uuid.uuid4().hex[:6].upper()}", "name": "Rx", "role": "pharmacist",
        "password": "password1"})
    assert resp.status_code == 201, resp.text


def test_patch_cannot_blank_a_clinical_department(admin):
    _, hdr = admin
    nurse = _mk("nurse", "Cardiology")
    resp = client.patch(f"/api/admin/users/{nurse}", headers=hdr, json={"department": "  "})
    assert resp.status_code == 400, resp.text
    admin_role = client.patch(f"/api/admin/users/{_mk('admin')}", headers=hdr,
                              json={"role": "doctor"})
    assert admin_role.status_code == 400, admin_role.text
    with db_session() as db:
        assert db.execute("SELECT department FROM users WHERE id = ?",
                          (nurse,)).fetchone()["department"] == "Cardiology"
