"""Admin Users + Data & Backups endpoints (routes/admin_routes.py).

Covers the read-only account posture list, the DB/backup posture snapshot,
the online-backup action (incl. its audit event), and RBAC denial for
non-admin roles.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi.testclient import TestClient  # noqa: E402
from main import app  # noqa: E402
from auth import hash_password  # noqa: E402
from database import get_db  # noqa: E402

client = TestClient(app)


def _ensure_user(user_id: str, role: str):
    db = get_db()
    try:
        db.execute(
            "INSERT OR IGNORE INTO users (id, name, role, department, password_hash) VALUES (?, ?, ?, ?, ?)",
            (user_id, f"{role.title()} OpsTest", role, "Administration", hash_password("password")),
        )
        db.commit()
    finally:
        db.close()


def _login(user_id: str) -> str:
    resp = client.post("/api/auth/login", json={"employee_id": user_id, "password": "password"})
    assert resp.status_code == 200, resp.text
    return resp.json()["token"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_admin_users_shape_and_no_secrets():
    _ensure_user("OPS-ADM", "admin")
    token = _login("OPS-ADM")
    resp = client.get("/api/admin/users", headers=_auth(token))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert isinstance(body.get("users"), list) and body["users"]
    row = next(u for u in body["users"] if u["id"] == "OPS-ADM")
    assert row["role"] == "admin"
    assert isinstance(row["totp_enabled"], bool)
    assert "failed_logins_24h" in row and "last_login" in row
    # hashes/secrets must never be exposed
    joined = str(body)
    assert "password_hash" not in joined and "pin_hash" not in joined and "totp_secret" not in joined


def test_admin_users_last_login_reflects_auth_audit():
    _ensure_user("OPS-ADM", "admin")
    token = _login("OPS-ADM")  # this login writes an auth audit row
    resp = client.get("/api/admin/users", headers=_auth(token))
    row = next(u for u in resp.json()["users"] if u["id"] == "OPS-ADM")
    assert row["last_login"] is not None


def test_admin_users_failed_logins_counted():
    """login_failure rows carry no actor_user_id (attempted id is in metadata),
    so the count must key on metadata.attempted_id — else it is always 0."""
    _ensure_user("OPS-ADM", "admin")
    _ensure_user("OPS-DOC", "doctor")
    # 3 wrong-password attempts against OPS-DOC
    for _ in range(3):
        r = client.post("/api/auth/login", json={"employee_id": "OPS-DOC", "password": "wrong"})
        assert r.status_code == 401
    token = _login("OPS-ADM")
    row = next(u for u in client.get("/api/admin/users", headers=_auth(token)).json()["users"]
               if u["id"] == "OPS-DOC")
    assert row["failed_logins_24h"] == 3, row


def test_backups_posture_shape():
    _ensure_user("OPS-ADM", "admin")
    token = _login("OPS-ADM")
    resp = client.get("/api/admin/backups", headers=_auth(token))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["database"]["filename"].endswith(".db")
    assert body["database"]["size_bytes"] > 0
    assert "users" in body["database"]["tables"]
    assert isinstance(body["backups"], list)
    assert body["backup_count"] == len(body["backups"])


def test_backup_count_reflects_true_total_not_display_cap(tmp_path, monkeypatch):
    """backup_count must be the real on-disk total, not the (≤50) list length."""
    monkeypatch.setenv("ORB_BACKUP_DIR", str(tmp_path))
    for i in range(55):
        (tmp_path / f"orb-backup-20260101-{i:06d}.db").write_bytes(b"x")
    _ensure_user("OPS-ADM", "admin")
    token = _login("OPS-ADM")
    body = client.get("/api/admin/backups", headers=_auth(token)).json()
    assert body["backup_count"] == 55
    assert len(body["backups"]) == 50  # list capped for display
    assert body["backups_shown"] == 50


def test_create_backup_writes_verified_file_and_audits(tmp_path, monkeypatch):
    monkeypatch.setenv("ORB_BACKUP_DIR", str(tmp_path))
    _ensure_user("OPS-ADM", "admin")
    token = _login("OPS-ADM")

    resp = client.post("/api/admin/backups", headers=_auth(token))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "success"
    fname = body["backup"]["filename"]
    assert fname.startswith("orb-backup-") and fname.endswith(".db")
    assert (tmp_path / fname).is_file()
    assert body["backup"]["size_bytes"] > 0

    # listed by GET afterwards
    listed = client.get("/api/admin/backups", headers=_auth(token)).json()
    assert any(b["filename"] == fname for b in listed["backups"])

    # audited
    logs = client.get(
        "/api/audit/logs", headers=_auth(token),
        params={"action_type": "backup_created", "limit": 5},
    ).json()
    assert any(l["target_id"] == fname for l in logs["logs"])


def test_non_admin_denied():
    _ensure_user("OPS-DOC", "doctor")
    token = _login("OPS-DOC")
    for path in ("/api/admin/users", "/api/admin/backups"):
        resp = client.get(path, headers=_auth(token))
        assert resp.status_code == 403, f"{path}: {resp.status_code}"
    resp = client.post("/api/admin/backups", headers=_auth(token))
    assert resp.status_code == 403


def test_admin_staffs_a_cashier_end_to_end():
    """The staffing path the cashier role existed for but could not complete
    until migration 33: ROLE_PERMISSIONS carried 'cashier' while the users
    CHECK rejected the row — and INSERT OR IGNORE swallowed the rejection, so
    the account 401'd against a row that never landed. Now: an admin creates
    the account over the API, the cashier logs in and reads the counter, and
    the same token is refused the admin surface. An unknown role is still
    refused up front with a 400 that names the allowed set (rendered from
    roles.STORABLE_ROLES, the same authority the CHECK renders from)."""
    _ensure_user("OPS-ADM", "admin")
    token = _login("OPS-ADM")

    resp = client.post(
        "/api/admin/users",
        headers=_auth(token),
        json={"id": "CSH-E2E-01", "name": "Counter One", "role": "cashier",
              "department": None, "password": "password"},
    )
    assert resp.status_code == 201, resp.text

    cashier_token = _login("CSH-E2E-01")
    counter = client.get("/api/payments/receipts", headers=_auth(cashier_token))
    assert counter.status_code == 200, counter.text
    assert client.get("/api/admin/users", headers=_auth(cashier_token)).status_code == 403

    bad = client.post(
        "/api/admin/users",
        headers=_auth(token),
        json={"id": "TPA-NOPE", "name": "T", "role": "tpa_clerk",
              "password": "password"},
    )
    assert bad.status_code == 400, bad.text
    assert "cashier" in bad.json()["detail"]
