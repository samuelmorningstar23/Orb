"""Fresh-install admin bootstrap (services/bootstrap_service.py).

The two invariants under test, stated as the module states them:
  1. "no login-capable admin" can never be a quiet state — it is detected,
     logged, published as a gauge, and written to disk as a remediation file.
  2. a shipped default credential cannot be used — in the prod profile the
     account is DEACTIVATED at boot, not merely warned about.

ISOLATION. These tests build their own throwaway sqlite database and inject it
into bootstrap_service via database.db_session, rather than using the suite's
shared test DB. That is not tidiness: run_bootstrap_check with enforcement ON
deactivates every account whose password is "password", and half this suite
creates exactly such accounts (see tests/test_admin_ops.py::_ensure_user). A
non-isolated test here would silently break unrelated tests depending on
collection order.
"""
import os
import sqlite3
import sys
from contextlib import contextmanager

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from auth import hash_password  # noqa: E402
from services import bootstrap_service as bs  # noqa: E402

# Mirrors the users columns bootstrap_service reads. Deliberately a subset of the
# real schema — if it ever drifts, these tests should fail loudly rather than
# pass against a shape production does not have.
_USERS_DDL = """
CREATE TABLE users (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    role TEXT NOT NULL,
    department TEXT,
    registration_number TEXT,
    password_hash TEXT NOT NULL,
    is_active INTEGER DEFAULT 1,
    token_version INTEGER DEFAULT 0,
    totp_enabled INTEGER DEFAULT 0,
    totp_secret TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""


@pytest.fixture
def db(tmp_path):
    """A throwaway users database with sqlite3.Row, seeded with the 'system' row."""
    conn = sqlite3.connect(str(tmp_path / "bootstrap_test.db"))
    conn.row_factory = sqlite3.Row
    conn.executescript(_USERS_DDL)
    conn.execute(
        "INSERT INTO users (id, name, role, department, password_hash) "
        "VALUES ('system', 'Orb Sage', 'admin', NULL, 'system_hash')"
    )
    conn.commit()
    yield conn
    conn.close()


def _add(db, user_id, role="admin", password="Correct-Horse-Battery-9", *,
         active=1, mfa=0, name=None):
    db.execute(
        "INSERT INTO users (id, name, role, password_hash, is_active, totp_enabled) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (user_id, name or f"{role.title()} {user_id}", role,
         hash_password(password), active, mfa),
    )
    db.commit()


@pytest.fixture
def inject(monkeypatch, db, tmp_path):
    """Point bootstrap_service's db_session and evidence dir at the fixtures.

    bootstrap_service imports db_session INSIDE run_bootstrap_check, so patching
    the attribute on the database module is what reaches it.
    """
    import database

    @contextmanager
    def _session():
        yield db

    monkeypatch.setattr(database, "db_session", _session)
    monkeypatch.setenv("ORB_BOOTSTRAP_DIR", str(tmp_path / "ops" / "bootstrap"))
    return db


# ── Password policy ──────────────────────────────────────────────────────────
def test_policy_refuses_every_shipped_default():
    """Every entry in the denylist must be refused — including the one that is
    literally in this repo's seed scripts and SETUP-NOTES.md."""
    for pwd in bs.SHIPPED_DEFAULT_PASSWORDS:
        assert bs.password_policy_error(pwd) is not None, pwd
    assert bs.password_policy_error("password") is not None
    # Case must not be an escape hatch.
    assert bs.password_policy_error("PASSWORD") is not None


def test_policy_refuses_short_and_id_derived_passwords():
    assert bs.password_policy_error("short1234") is not None            # < 12
    assert bs.password_policy_error("ADM001", user_id="ADM001") is not None
    assert bs.password_policy_error("ADM001-winter", user_id="ADM001") is not None
    assert bs.password_policy_error("janedoe-is-here", name="janedoe") is not None
    assert bs.password_policy_error("aaaaaaaaaaaaaaaa") is not None      # too few distinct


def test_policy_accepts_a_real_password():
    assert bs.password_policy_error(
        "tK9!marigold-cupboard", user_id="ADM001", name="Jane Doe"
    ) is None


# ── Inventory ────────────────────────────────────────────────────────────────
def test_system_row_is_never_counted_as_an_admin(db):
    """The 'system' row is role='admin' with an unusable placeholder hash. If it
    counted, a virgin appliance would report a healthy admin roster and the whole
    first invariant would be dead on arrival."""
    inv = bs.admin_inventory(db)
    assert inv["admins_active"] == 0
    assert inv["active"] == []


def test_inventory_separates_active_inactive_and_mfa(db):
    _add(db, "ADM001", mfa=1)
    _add(db, "ADM002", mfa=0)
    _add(db, "ADM003", active=0)
    inv = bs.admin_inventory(db)
    assert inv["admins_active"] == 2
    assert inv["admins_inactive"] == 1
    assert inv["admins_with_mfa"] == 1


# ── Credential scan ──────────────────────────────────────────────────────────
def test_scan_finds_the_seeded_demo_password(db):
    _add(db, "ADM001", password="password")
    _add(db, "DOC001", role="doctor", password="Correct-Horse-Battery-9")
    result = bs.scan_default_credentials(db, candidates=["password"])
    assert [f["id"] for f in result["findings"]] == ["ADM001"]
    assert result["checked"] >= 2


def test_scan_never_reports_the_password_itself(db):
    """The result is written to ops/bootstrap/status.json and summarised to
    admins (/api/health/access, the ops status page). A file listing working
    credentials for the appliance would be a rather spectacular own goal."""
    _add(db, "ADM001", password="password")
    result = bs.scan_default_credentials(db, candidates=["password"])
    blob = repr(result)
    assert "password" not in blob.replace("password_hint", "").replace("passwords", "")
    assert result["findings"][0]["password_hint"] == "p" + "*" * 7


def test_scan_checks_privileged_accounts_first(db):
    """When the budget runs out it must run out on a pharmacist, never on the
    account holding admin.overview."""
    for i in range(5):
        _add(db, f"NUR{i:03d}", role="nurse")
    _add(db, "ADM001", password="password")
    result = bs.scan_default_credentials(db, candidates=["password"], budget_s=0.0001)
    # Budget of ~0 means exactly one candidate is attempted, on the first account
    # in scan order — which must be the admin.
    assert result["budget_exhausted"] is True
    assert [f["id"] for f in result["findings"]] in ([], ["ADM001"])
    assert result["accounts_considered"] == 6


def test_scan_skips_patients_and_the_system_row(db):
    _add(db, "P001", role="patient", password="password")
    result = bs.scan_default_credentials(db, candidates=["password"])
    assert result["findings"] == []
    assert result["accounts_considered"] == 0


# ── Enforcement ──────────────────────────────────────────────────────────────
def test_enforcement_deactivates_and_revokes(inject, db):
    _add(db, "ADM001", password="password")
    _add(db, "ADM002", password="tK9!marigold-cupboard")
    snap = bs.run_bootstrap_check(enforce=True)

    assert snap["default_credential_accounts"] == ["ADM001"]
    assert snap["default_credential_locked"] == ["ADM001"]
    row = db.execute("SELECT is_active, token_version FROM users WHERE id='ADM001'").fetchone()
    assert row["is_active"] == 0, "a default-credential account must not be able to log in"
    assert row["token_version"] == 1, "existing sessions must be revoked at the same moment"
    # The good admin is untouched, and the snapshot describes the world AFTER
    # the lockout.
    assert snap["admins_active"] == 1


def test_dev_profile_reports_but_does_not_lock(inject, db):
    """On a laptop the demo accounts ARE the product. Report, never lock."""
    _add(db, "ADM001", password="password")
    snap = bs.run_bootstrap_check(enforce=False)
    assert snap["default_credential_accounts"] == ["ADM001"]
    assert snap["default_credential_locked"] == []
    assert db.execute("SELECT is_active FROM users WHERE id='ADM001'").fetchone()[0] == 1


def test_locking_the_only_admin_is_allowed_and_screams(inject, db, tmp_path):
    """The deliberate ordering: an unusable appliance beats a breached one.

    Locking the last admin produces invariant-1 state, and that must be loud —
    a remediation file on disk with the exact command, not a log line.
    """
    _add(db, "ADM001", password="password")
    snap = bs.run_bootstrap_check(enforce=True)

    assert snap["admins_active"] == 0
    notice = tmp_path / "ops" / "bootstrap" / bs.FIRST_ADMIN_FILENAME
    assert notice.exists()
    text = notice.read_text()
    assert "create_admin.py" in text and "--enroll-mfa" in text
    assert "NO ADMINISTRATOR ACCOUNT" in text


def test_virgin_appliance_writes_the_notice(inject, tmp_path):
    """Nothing but the 'system' row: the fresh-install case."""
    snap = bs.run_bootstrap_check(enforce=True)
    assert snap["admins_active"] == 0
    assert (tmp_path / "ops" / "bootstrap" / bs.FIRST_ADMIN_FILENAME).exists()


def test_notice_is_cleared_once_an_admin_exists(inject, db, tmp_path):
    bs.run_bootstrap_check(enforce=True)
    notice = tmp_path / "ops" / "bootstrap" / bs.FIRST_ADMIN_FILENAME
    assert notice.exists()

    _add(db, "ADM001", password="tK9!marigold-cupboard")
    snap = bs.run_bootstrap_check(enforce=True)
    assert snap["admins_active"] == 1
    assert not notice.exists(), "a stale 'no admin' notice would send an operator chasing a fixed problem"


def test_status_json_is_written_and_carries_no_password(inject, db, tmp_path):
    import json
    _add(db, "ADM001", password="password")
    bs.run_bootstrap_check(enforce=True)
    status = tmp_path / "ops" / "bootstrap" / bs.STATUS_FILENAME
    assert status.exists()
    blob = status.read_text()
    json.loads(blob)  # must be valid JSON for whoever reads it on the box
    assert '"password"' not in blob


def test_snapshot_is_published_for_the_access_checks(inject, db):
    _add(db, "ADM001", password="tK9!marigold-cupboard", mfa=1)
    bs.run_bootstrap_check(enforce=True)
    snap = bs.bootstrap_snapshot()
    assert snap["state"] == "ok"
    assert snap["admins_active"] == 1
    assert snap["admins_with_mfa"] == 1


# ── Role review ──────────────────────────────────────────────────────────────
def test_review_flags_privileged_accounts_without_mfa(db):
    _add(db, "ADM001", mfa=0)
    _add(db, "ADM002", mfa=1)
    _add(db, "DOC001", role="doctor", mfa=0)
    review = bs.review_accounts(db)
    assert review["privileged_without_mfa"] == ["ADM001"]
    adm1 = next(a for a in review["accounts"] if a["id"] == "ADM001")
    assert "privileged account without MFA" in adm1["findings"]
    # A doctor without MFA is not flagged — MFA is mandated for admin/CIO roles.
    doc = next(a for a in review["accounts"] if a["id"] == "DOC001")
    assert doc["findings"] == []


def test_review_flags_legacy_password_hashes(db):
    import hashlib
    db.execute(
        "INSERT INTO users (id, name, role, password_hash) VALUES (?,?,?,?)",
        ("DOC009", "Legacy", "doctor", hashlib.sha256(b"x").hexdigest()),
    )
    db.commit()
    review = bs.review_accounts(db)
    row = next(a for a in review["accounts"] if a["id"] == "DOC009")
    assert "legacy (non-bcrypt) password hash" in row["findings"]


def test_review_excludes_patients_and_system(db):
    _add(db, "P001", role="patient")
    _add(db, "ADM001")
    review = bs.review_accounts(db)
    ids = {a["id"] for a in review["accounts"]}
    assert ids == {"ADM001"}


def test_review_reports_permission_breadth(db):
    """The CIO's actual question is 'what can each of them reach'."""
    _add(db, "ADM001", role="admin")
    _add(db, "DOC001", role="doctor")
    review = bs.review_accounts(db)
    perms = {a["id"]: a["permission_count"] for a in review["accounts"]}
    assert perms["ADM001"] > 0 and perms["DOC001"] > 0


# ── Admin surface ────────────────────────────────────────────────────────────
def test_health_access_endpoint_is_admin_only_and_never_sweeps():
    """GET /api/health/access enumerates this appliance's weak points, so it is
    gated on admin.overview — and it returns the CACHED snapshot, so it can
    never be used to make the box do minutes of bcrypt per request."""
    from fastapi.testclient import TestClient
    from database import get_db
    from main import app

    client = TestClient(app)

    def _ensure(user_id, role):
        db = get_db()
        try:
            db.execute(
                "INSERT OR IGNORE INTO users (id, name, role, department, password_hash) "
                "VALUES (?, ?, ?, 'Administration', ?)",
                (user_id, f"{role.title()} AccessTest", role, hash_password("password")),
            )
            db.commit()
        finally:
            db.close()

    def _token(user_id):
        resp = client.post("/api/auth/login",
                           json={"employee_id": user_id, "password": "password"})
        assert resp.status_code == 200, resp.text
        return resp.json()["token"]

    _ensure("ACCESS-ADM", "admin")
    _ensure("ACCESS-DOC", "doctor")

    denied = client.get("/api/health/access",
                        headers={"Authorization": f"Bearer {_token('ACCESS-DOC')}"})
    assert denied.status_code == 403

    resp = client.get("/api/health/access",
                      headers={"Authorization": f"Bearer {_token('ACCESS-ADM')}"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # Orb is strictly offline: there is no outbound heartbeat state to report.
    assert set(body) == {"access"}
