"""The CIO account review and the default-password scan cover every staff role.

bootstrap_service.STAFF_ROLES used to be a hand-written tuple that stopped at
pharmacist. Cashier and the five hospital-system roles (receptionist, ward
clerk, lab technician, pathologist, phlebotomist) therefore escaped both the
go-live staff review a CIO signs and the shipped-default-password scan that
locks such accounts in the prod profile. The list is now derived from
roles.STORABLE_ROLES, so these tests pin both the derivation and the
behaviour it buys.

Isolation follows tests/test_bootstrap_admin.py: a throwaway sqlite database
per test, never the suite's shared one, because enforcement deactivates any
account whose password is "password".
"""
import os
import sqlite3
import sys
from contextlib import contextmanager

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import roles  # noqa: E402
from auth import hash_password  # noqa: E402
from services import bootstrap_service as bs  # noqa: E402

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

HIS_ROLES = ("receptionist", "ward_clerk", "lab_technician", "pathologist", "phlebotomist")


@pytest.fixture
def db(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "bootstrap_staff_roles.db"))
    conn.row_factory = sqlite3.Row
    conn.executescript(_USERS_DDL)
    conn.execute(
        "INSERT INTO users (id, name, role, department, password_hash) "
        "VALUES ('system', 'Orb Sage', 'admin', NULL, 'system_hash')"
    )
    conn.commit()
    yield conn
    conn.close()


@pytest.fixture
def inject(monkeypatch, db, tmp_path):
    import database

    @contextmanager
    def _session():
        yield db

    monkeypatch.setattr(database, "db_session", _session)
    monkeypatch.setenv("ORB_BOOTSTRAP_DIR", str(tmp_path / "ops" / "bootstrap"))
    return db


def _add(db, user_id, role, password="Correct-Horse-Battery-9", *, active=1):
    db.execute(
        "INSERT INTO users (id, name, role, password_hash, is_active) VALUES (?, ?, ?, ?, ?)",
        (user_id, f"{role} {user_id}", role, hash_password(password), active),
    )
    db.commit()


def test_staff_roles_are_every_storable_role_except_patient():
    """Equality, not a subset: a role added to roles.py that bootstrap does not
    cover fails here, which is the regression this file exists to stop."""
    assert set(bs.STAFF_ROLES) == set(roles.STORABLE_ROLES) - {"patient"}
    assert "patient" not in bs.STAFF_ROLES
    for role in ("admin", "doctor", "nurse", "pharmacist", "cashier", *HIS_ROLES):
        assert role in bs.STAFF_ROLES, role


def test_receptionist_with_a_shipped_default_is_reported_by_review_and_scan(db):
    _add(db, "REC001", "receptionist", password="password")
    _add(db, "DOC001", "doctor")

    review = bs.review_accounts(db)
    reviewed = {a["id"]: a for a in review["accounts"]}
    assert "REC001" in reviewed, "the CIO review must list a receptionist login"
    assert reviewed["REC001"]["role"] == "receptionist"
    assert review["staff_accounts"] == 2

    scan = bs.scan_default_credentials(db, candidates=["password"])
    assert [f["id"] for f in scan["findings"]] == ["REC001"]
    assert scan["findings"][0]["role"] == "receptionist"
    assert scan["accounts_considered"] == 2


@pytest.mark.parametrize("role", ("cashier", *HIS_ROLES))
def test_every_new_staff_role_is_scanned(db, role):
    _add(db, "U001", role, password="password")
    scan = bs.scan_default_credentials(db, candidates=["password"])
    assert [f["id"] for f in scan["findings"]] == ["U001"], role
    assert [a["id"] for a in bs.review_accounts(db)["accounts"]] == ["U001"], role


def test_patients_and_the_system_row_still_escape(db):
    _add(db, "P001", "patient", password="password")
    scan = bs.scan_default_credentials(db, candidates=["password"])
    assert scan["findings"] == []
    assert scan["accounts_considered"] == 0
    assert bs.review_accounts(db)["accounts"] == []


def test_prod_enforcement_locks_a_receptionist_on_a_shipped_default(inject, db):
    _add(db, "ADM001", "admin", password="tK9!marigold-cupboard")
    _add(db, "REC001", "receptionist", password="password")
    snap = bs.run_bootstrap_check(enforce=True)

    assert snap["default_credential_accounts"] == ["REC001"]
    assert snap["default_credential_locked"] == ["REC001"]
    row = db.execute("SELECT is_active, token_version FROM users WHERE id='REC001'").fetchone()
    assert row["is_active"] == 0
    assert row["token_version"] == 1
    # The admin with a real password is untouched.
    assert db.execute("SELECT is_active FROM users WHERE id='ADM001'").fetchone()[0] == 1
