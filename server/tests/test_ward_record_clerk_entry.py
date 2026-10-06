"""The Orb Ward Record's clerk lane (6 October 2026).

The ward clerk copies each observation round from the paper sheet
(docs/fundraising/SALES_PLAN_2026-10.md section 3, step 2). She holds
vitals.transcribe, not vitals.write, and reaches the same observations route
with the same patient scope. Her sets are stored with source
'clerk_transcribed', timestamped with the time written on the sheet, and every
keyed set records entered_at, the UTC time of keying.
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import permissions as P
from auth import create_token
from database import db_session
from routes.nurse_routes import router as nurse_router
from services.clinical_time import parse_to_utc
from services.vitals_source.base import SourceKind, classify_source_label

DEPT_A = "Ward Record Clerk Medicine"
DEPT_B = "Ward Record Clerk Surgery"


@pytest.fixture()
def client():
    app = FastAPI()
    app.include_router(nurse_router)
    return TestClient(app)


def _auth(uid, role):
    return {"Authorization": f"Bearer {create_token(uid, role)}"}


def _mk_user(role, department):
    uid = f"WRC{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO users (id, name, role, password_hash, department) "
            "VALUES (?, ?, ?, 'x', ?)",
            (uid, f"Ward Record {role}", role, department),
        )
    return uid


def _mk_patient(department):
    pid = f"WRCP{uuid.uuid4().hex[:8].upper()}"
    now = datetime.now(timezone.utc).isoformat()
    with db_session() as db:
        db.execute(
            "INSERT OR IGNORE INTO users (id, name, role, password_hash) "
            "VALUES ('WRCADM', 'Ward Record Admitter', 'doctor', 'x')"
        )
        db.execute(
            """INSERT INTO patients (id, name, mrn, bed, ward, age, sex, diagnosis,
                                     status, admission_date)
               VALUES (?, ?, ?, 'B-2', 'WR-1', 58, 'M', 'Ward record fixture',
                       'admitted', ?)""",
            (pid, f"WRC {department}", f"MRN-{uuid.uuid4().hex[:8].upper()}", now),
        )
        db.execute(
            "INSERT INTO admissions (patient_id, department, ward, bed, status, admitted_by) "
            "VALUES (?, ?, 'WR-1', 'B-2', 'active', 'WRCADM')",
            (pid, department),
        )
    return pid


_SET = {"heart_rate": 88, "systolic_bp": 128, "diastolic_bp": 80,
        "respiratory_rate": 16, "temperature": 37.1, "spo2": 97,
        "consciousness": "alert", "on_oxygen": False}


def _row(vitals_id):
    with db_session() as db:
        return dict(db.execute("SELECT * FROM vitals WHERE id = ?", (vitals_id,)).fetchone())


def test_the_clerk_holds_transcribe_and_nothing_clinical():
    perms = P.ROLE_PERMISSIONS["ward_clerk"]
    assert P.VITALS_TRANSCRIBE in perms
    assert P.VITALS_WRITE not in perms
    assert P.VITALS_READ not in perms
    assert P.PATIENT_READ_ALL not in perms
    holders = {r for r, p in P.ROLE_PERMISSIONS.items() if P.VITALS_TRANSCRIBE in p}
    assert holders == {"ward_clerk"}


def test_a_clerk_set_is_stored_as_transcribed_with_sheet_time_and_keying_time(client):
    pid = _mk_patient(DEPT_A)
    clerk = _mk_user("ward_clerk", DEPT_A)
    sheet_time = (datetime.now(timezone.utc) - timedelta(days=2, hours=3)).replace(microsecond=0)
    before = datetime.now(timezone.utc)
    r = client.post(f"/api/nurse/patients/{pid}/observations",
                    json={**_SET, "taken_at": sheet_time.isoformat()},
                    headers=_auth(clerk, "ward_clerk"))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["source"] == "clerk_transcribed"
    row = _row(body["vitals_id"])
    assert row["source"] == "clerk_transcribed"
    assert parse_to_utc(row["timestamp"]) == sheet_time
    keyed = parse_to_utc(row["entered_at"])
    assert before - timedelta(seconds=5) <= keyed <= datetime.now(timezone.utc) + timedelta(seconds=5)
    assert row["entered_at"].endswith("+00:00")
    assert classify_source_label(row["source"]) is SourceKind.MANUAL


def test_a_nurse_set_keeps_the_nurse_source_and_records_entered_at(client):
    pid = _mk_patient(DEPT_A)
    nurse = _mk_user("nurse", DEPT_A)
    r = client.post(f"/api/nurse/patients/{pid}/observations", json=_SET,
                    headers=_auth(nurse, "nurse"))
    assert r.status_code == 200, r.text
    row = _row(r.json()["vitals_id"])
    assert row["source"] == "nurse_manual"
    assert row["entered_at"]


def test_the_clerk_is_ward_scoped_with_a_404_and_a_denial_row(client):
    stranger = _mk_patient(DEPT_B)
    clerk = _mk_user("ward_clerk", DEPT_A)
    r = client.post(f"/api/nurse/patients/{stranger}/observations", json=_SET,
                    headers=_auth(clerk, "ward_clerk"))
    assert r.status_code == 404
    with db_session() as db:
        assert db.execute("SELECT COUNT(*) FROM vitals WHERE patient_id = ?",
                          (stranger,)).fetchone()[0] == 0
        denied = db.execute(
            "SELECT COUNT(*) FROM audit_logs WHERE action_type = 'patient_scope_denied' "
            "AND actor_user_id = ? AND patient_id = ?", (clerk, stranger)).fetchone()[0]
    assert denied == 1


def test_a_role_with_neither_permission_is_refused(client):
    pid = _mk_patient(DEPT_A)
    cashier = _mk_user("cashier", None)
    r = client.post(f"/api/nurse/patients/{pid}/observations", json=_SET,
                    headers=_auth(cashier, "cashier"))
    assert r.status_code == 403


def test_the_clerk_gets_no_score_preview(client):
    pid = _mk_patient(DEPT_A)
    clerk = _mk_user("ward_clerk", DEPT_A)
    r = client.post(f"/api/nurse/patients/{pid}/news2-preview", json=_SET,
                    headers=_auth(clerk, "ward_clerk"))
    assert r.status_code == 403


def test_a_clerk_replay_matches_her_own_row_not_the_nurses(client):
    pid = _mk_patient(DEPT_A)
    nurse = _mk_user("nurse", DEPT_A)
    clerk = _mk_user("ward_clerk", DEPT_A)
    sheet_time = (datetime.now(timezone.utc) - timedelta(hours=5)).replace(microsecond=0).isoformat()
    body = {**_SET, "taken_at": sheet_time}
    by_nurse = client.post(f"/api/nurse/patients/{pid}/observations", json=body,
                           headers=_auth(nurse, "nurse")).json()
    first = client.post(f"/api/nurse/patients/{pid}/observations", json=body,
                        headers=_auth(clerk, "ward_clerk")).json()
    assert first["replayed"] is False
    assert first["vitals_id"] != by_nurse["vitals_id"]
    again = client.post(f"/api/nurse/patients/{pid}/observations", json=body,
                        headers=_auth(clerk, "ward_clerk")).json()
    assert again["replayed"] is True
    assert again["vitals_id"] == first["vitals_id"]


def test_the_clerk_cannot_key_a_future_sheet_time(client):
    pid = _mk_patient(DEPT_A)
    clerk = _mk_user("ward_clerk", DEPT_A)
    future = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
    r = client.post(f"/api/nurse/patients/{pid}/observations",
                    json={**_SET, "taken_at": future}, headers=_auth(clerk, "ward_clerk"))
    assert r.status_code == 422


def test_migration_77_adds_the_five_columns():
    with db_session() as db:
        cols = {r[1] for r in db.execute("PRAGMA table_info(vitals)").fetchall()}
    assert {"entered_at", "illegible", "voided_at", "voided_by", "void_reason"} <= cols


def test_migration_77_is_idempotent(tmp_path):
    import sqlite3

    from migrations_his.m0077_vitals_entry_and_void import MIGRATION
    conn = sqlite3.connect(str(tmp_path / "m77.db"))
    conn.execute("CREATE TABLE vitals (id INTEGER PRIMARY KEY, patient_id TEXT, timestamp TEXT)")
    MIGRATION.apply(conn)
    MIGRATION.apply(conn)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(vitals)").fetchall()]
    assert cols.count("entered_at") == 1 and "void_reason" in cols
    conn.close()
