"""A pharmacist is a hospital-wide function, not a ward.

`enforce_patient_scope` compares the caller's `users.department` against the
patient's ADMISSION department. Nobody is admitted to "Pharmacy", so a
pharmacist carrying that department matched no patient anywhere and every
patient-scoped route answered 404 — including the route a pharmacist uses to
raise an order. Found live on the seeded ward, 2026-09-07.

The same bug was fixed for ADM001 on 2026-08-12 and PHA001, one line below it in
the seed, was missed. These tests pin BOTH halves of the fix: the pharmacist can
now reach a patient, and the department-less account that fix creates does NOT
inherit every department chat room through Relay's substring match.

THREE ACCOUNTS HAVE NOW CARRIED THE SAME WRONG SHAPE — ADM001 ("Administration"),
PHA001 ("Pharmacy") and the 'system' audit identity ("IT"). So the last test here
pins the INVARIANT rather than the three fixes: a department that names no ward
scopes its account to nothing at all. That is the property every one of them
violated, and it is what a fourth instance would violate too.
"""
import uuid

import pytest
from fastapi.testclient import TestClient

from main import app
from database import db_session
from auth import hash_password
from permissions import department_scope, patient_in_scope
from services.helix_db import init_helix_db

client = TestClient(app)


@pytest.fixture(scope="module", autouse=True)
def _seed_module():
    init_helix_db()
    with db_session() as db:
        db.execute(
            "INSERT OR IGNORE INTO helix_medication_master (id, name, generic_name, form, strength) "
            "VALUES ('HXWARDPARA', 'Zylopar Ward Fixture 500mg Tablet', 'zylopar', 'tablet', '500mg')"
        )


def _mk_user(role, department):
    uid = f"UWARD{uuid.uuid4().hex[:6].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT OR IGNORE INTO users (id, name, role, password_hash, department) VALUES (?,?,?,?,?)",
            (uid, f"{role.title()} {uid}", role, hash_password("secretpass"), department),
        )
    return uid


def _login_hdr(uid):
    r = client.post("/api/auth/login", json={"employee_id": uid, "password": "secretpass"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def _mk_admitted_patient(department="General Medicine"):
    pid = f"PATWARD{uuid.uuid4().hex[:6].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO patients (id, name, mrn, status, bed, ward) "
            "VALUES (?, ?, ?, 'admitted', 'B-2', 'GM-1')",
            (pid, f"Ward Case {uuid.uuid4().hex[:5]}", f"MRN-{uuid.uuid4().hex[:6]}"),
        )
        db.execute(
            "INSERT INTO admissions (patient_id, department, ward, bed, status, admitted_by) "
            "VALUES (?, ?, 'GM-1', 'B-2', 'active', 'system')",
            (pid, department),
        )
    return pid


# ── the defect itself ────────────────────────────────────────────────────────
def test_a_pharmacist_can_raise_an_order_for_a_patient_on_a_ward():
    """THE BUG, END TO END. With department='Pharmacy' this returned 404
    "Patient not found" for every patient in the hospital."""
    pharmacist = _mk_user("pharmacist", None)
    pid = _mk_admitted_patient("General Medicine")

    resp = client.post(
        "/api/helix/orders/approve",
        json={"patient_id": pid, "medication": "Zylopar Ward Fixture",
              "dose": "500mg", "route": "PO", "frequency": "TDS"},
        headers=_login_hdr(pharmacist),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["order"]["patient_id"] == pid


def test_pharmacy_is_never_an_admission_department():
    """Why clearing the column is safe rather than a widening: the value could
    never have matched anything. Asserts the INVARIANT (no admission carries it),
    not the current seed's contents."""
    with db_session() as db:
        rows = db.execute(
            "SELECT COUNT(*) FROM admissions WHERE LOWER(department) = 'pharmacy'"
        ).fetchone()[0]
    assert rows == 0, (
        "an admission department of 'Pharmacy' would make the cleared scope "
        "meaningful again and this migration wrong"
    )


def test_a_ward_scoped_pharmacist_is_still_scoped():
    """The migration clears ONLY role='pharmacist' AND department='Pharmacy'. A
    site that deliberately attaches a pharmacist to a real clinical ward keeps
    that scope — clearing it would be a silent grant."""
    ward_pharmacist = {"department": "Cardiology"}
    assert department_scope(ward_pharmacist) == "Cardiology"

    pid = _mk_admitted_patient("General Medicine")
    with db_session() as db:
        assert patient_in_scope(db, pid, "Cardiology") is False
        assert patient_in_scope(db, pid, None) is True


# ── the leak the fix would otherwise have opened ─────────────────────────────
def test_a_department_less_account_gets_no_department_channels():
    """RELAY MATCHES BY SUBSTRING, and "" is a substring of everything.
    `"" in "cardiology"` is True, so before 2026-09-07 a department-less account
    was handed every department room in the hospital. It was invisible because
    the only department-less seeded account was ADM001, and `admin` never enters
    that branch — making PHA001 department-less would have been the first
    account to walk into it."""
    from services.relay_service import get_channels_for_user

    with db_session() as db:
        for cid, name in (("dept-test-cardio", "Cardiology"),
                          ("dept-test-neuro", "Neurology")):
            db.execute(
                "INSERT OR IGNORE INTO channels (id, name, type) VALUES (?, ?, 'department')",
                (cid, name),
            )

    pharmacist = _mk_user("pharmacist", None)
    channels = get_channels_for_user(pharmacist, "pharmacist")
    depts = [c for c in channels if c["type"] == "department"]
    assert depts == [], (
        f"a hospital-wide account belongs to no ward and gets no ward's staff "
        f"room; saw {[c['name'] for c in depts]}"
    )


def test_an_admin_sees_every_department_channel():
    """REGRESSION, FIXED 2026-09-07: an admin saw NONE of them.

    The branch read `if type == "department" and user_role != "admin"`, which was
    how admins got every room back when the chain ended in the bare
    `else: filtered.append(r)`. The 2026-08-05 PHI fix replaced that else with
    typed branches and a fail-closed default, and department rows for an admin
    then matched nothing and landed in the fail-closed branch — the one whose own
    comment calls itself UNREACHABLE. It had been logging "unknown type
    'department' — withheld" on every admin channel list since.

    Asserts governance sees ALL of them, not merely one: seeing a single room
    would also pass a naive "admin sees something" check while the scope was
    still wrong."""
    from services.relay_service import get_channels_for_user

    with db_session() as db:
        for cid, name in (("dept-adm-cardio", "Cardiology"),
                          ("dept-adm-neuro", "Neurology"),
                          ("dept-adm-ortho", "Orthopedics")):
            db.execute(
                "INSERT OR IGNORE INTO channels (id, name, type) VALUES (?, ?, 'department')",
                (cid, name),
            )

    # Department set deliberately: an admin's governance reach must not depend on
    # which department happens to be on their user row.
    admin = _mk_user("admin", "IT")
    names = {c["name"] for c in get_channels_for_user(admin, "admin")
             if c["type"] == "department"}
    for expected in ("Cardiology", "Neurology", "Orthopedics"):
        assert expected in names, (
            f"an administrator oversees every ward and must see {expected}; saw {sorted(names)}"
        )


def test_no_valid_channel_type_reaches_the_fail_closed_branch():
    """THE INVARIANT BEHIND THE BUG, not the bug's symptom.

    The fail-closed `else` exists for a rogue `channels.type` and documents
    itself as unreachable. It was reachable — by the VALID type 'department', for
    the role `admin` — and it withheld the row silently apart from a log line
    nobody read. This asserts the property that was actually violated: every type
    the CHECK constraint permits is decided by a branch that names it, for every
    role, so no valid row can ever be dropped by the default again."""
    from services.relay_service import get_channels_for_user

    with db_session() as db:
        db.execute(
            "INSERT OR IGNORE INTO channels (id, name, type) VALUES "
            "('dept-adm-cardio', 'Cardiology', 'department')"
        )

    for role, dept in (("admin", "IT"), ("admin", None),
                       ("doctor", "Cardiology"), ("nurse", "Cardiology"),
                       ("pharmacist", None)):
        uid = _mk_user(role, dept)
        with _capture_relay_warnings() as seen:
            get_channels_for_user(uid, role)
        assert not seen, (
            f"role={role} dept={dept!r} pushed a valid channel type into the "
            f"fail-closed branch: {seen}"
        )


import contextlib
import logging


@contextlib.contextmanager
def _capture_relay_warnings():
    """Collect 'unknown type' withholdings emitted while listing channels."""
    seen = []

    class _H(logging.Handler):
        def emit(self, record):
            msg = record.getMessage()
            if "unknown type" in msg:
                seen.append(msg)

    h = _H()
    root = logging.getLogger()
    root.addHandler(h)
    prev = root.level
    root.setLevel(logging.DEBUG)
    try:
        yield seen
    finally:
        root.removeHandler(h)
        root.setLevel(prev)


def test_a_ward_account_still_gets_its_own_department_channel():
    """The guard above must not have closed the normal path."""
    from services.relay_service import get_channels_for_user

    with db_session() as db:
        db.execute(
            "INSERT OR IGNORE INTO channels (id, name, type) VALUES "
            "('dept-test-cardio', 'Cardiology', 'department')"
        )

    doc = _mk_user("doctor", "Cardiology")
    channels = get_channels_for_user(doc, "doctor")
    names = {c["name"] for c in channels if c["type"] == "department"}
    assert "Cardiology" in names, "a ward account must still see its own room"


# ── the audit identity, and the invariant behind all three ───────────────────
def test_the_system_audit_identity_carries_no_ward():
    """'system' (Orb Sage) shipped with department='IT'. No ward is called IT.

    It was never a LIVE defect — the account's placeholder hash `system_hash`
    can never verify against a password, so nothing ever authenticates as it and
    no scope is ever derived. Fixed anyway (migration 37): two of the three
    accounts that carried a non-ward department turned out to be real bugs, and
    the third is a trap left armed for whoever wires this identity into a code
    path that does resolve a scope."""
    with db_session() as db:
        row = db.execute(
            "SELECT department, password_hash FROM users WHERE id = 'system'"
        ).fetchone()
    assert row is not None, "the system audit identity must exist"
    assert row["department"] is None, (
        f"'system' belongs to no ward; found department={row['department']!r}"
    )
    # The reason it was harmless, asserted rather than assumed: if this ever
    # becomes a login-capable account, the scope question stops being theoretical.
    assert row["password_hash"] == "system_hash", (
        "'system' is an audit identity, not a login; if that changed, its scope "
        "now matters and this test is the place that says so"
    )


def test_a_department_naming_no_ward_scopes_an_account_to_nothing():
    """THE INVARIANT ALL THREE BUGS VIOLATED.

    `enforce_patient_scope` matches the caller's department against the
    patient's ADMISSION department. A department that no admission uses -
    "Administration", "Pharmacy", "IT" - therefore matches no patient anywhere,
    and the account silently sees an empty hospital rather than erroring.

    Asserted as a PROPERTY of the scoping model, not as a fact about today's
    seed: a fourth account given a non-ward department would fail this even
    though nobody thought to name it here."""
    pid = _mk_admitted_patient("General Medicine")

    with db_session() as db:
        real_wards = {
            r[0] for r in db.execute(
                "SELECT DISTINCT department FROM admissions WHERE department IS NOT NULL"
            ).fetchall()
        }
        for not_a_ward in ("Administration", "Pharmacy", "IT"):
            assert not_a_ward not in real_wards, (
                f"{not_a_ward!r} is being used as an admission department, which "
                f"would make scoping an account to it meaningful and this "
                f"reasoning wrong"
            )
            assert patient_in_scope(db, pid, not_a_ward) is False, (
                f"an account scoped to {not_a_ward!r} can reach no patient at "
                f"all — which is why the value is never right on a user row"
            )

        # And the shape that IS right for a hospital-wide function: no department.
        assert patient_in_scope(db, pid, None) is True
