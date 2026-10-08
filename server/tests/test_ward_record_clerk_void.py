"""The ward clerk strikes out her own copies (Orb Ward Record, 9 October 2026).

SALES_PLAN_2026-10.md section 3, step 2: "A wrong value is corrected by marking
it entered in error with a reason; the original stays in the audit chain." The
void route used to need vitals.write, so a set the clerk copied wrong waited
for a nurse or doctor to strike it out. Now an account holding only
vitals.transcribe may strike out a set when it is a copy from the paper sheet
(source clerk_transcribed) AND her own account keyed it, which the set's
observation_recorded audit row records. Same reason rule, same audit row, same
ward scope. Anything else is a 403 in her words. Her Observations screen lists
her own copies for a patient from the last 24 hours.
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from auth import create_token
from database import db_session
from routes.nurse_routes import router as nurse_router
from services import nurse_shift_service

DEPT_A = "Ward Record Clerk Void Medicine"
DEPT_B = "Ward Record Clerk Void Surgery"
NOT_YOURS = "You can only strike out a set you copied yourself."


@pytest.fixture()
def client():
    app = FastAPI()
    app.include_router(nurse_router)
    return TestClient(app)


def _auth(uid, role):
    return {"Authorization": f"Bearer {create_token(uid, role)}"}


def _mk_user(role, department):
    uid = f"WCV{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO users (id, name, role, password_hash, department) "
            "VALUES (?, ?, ?, 'x', ?)",
            (uid, f"Clerk Void {role}", role, department),
        )
    return uid


def _mk_patient(department):
    pid = f"WCVP{uuid.uuid4().hex[:8].upper()}"
    then = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    with db_session() as db:
        db.execute(
            "INSERT OR IGNORE INTO users (id, name, role, password_hash) "
            "VALUES ('WCVADM', 'Clerk Void Admitter', 'doctor', 'x')"
        )
        db.execute(
            """INSERT INTO patients (id, name, mrn, bed, ward, age, sex, diagnosis,
                                     status, admission_date)
               VALUES (?, ?, ?, 'B-3', 'CV-1', 64, 'M', 'Clerk void fixture',
                       'admitted', ?)""",
            (pid, f"WCV {department}", f"MRN-{uuid.uuid4().hex[:8].upper()}", then),
        )
        db.execute(
            "INSERT INTO admissions (patient_id, department, ward, bed, status, admitted_by, "
            "created_at) VALUES (?, ?, 'CV-1', 'B-3', 'active', 'WCVADM', ?)",
            (pid, department, then),
        )
    return pid


_SET = {"heart_rate": 182, "systolic_bp": 126, "diastolic_bp": 78,
        "respiratory_rate": 16, "temperature": 36.9, "spo2": 97,
        "consciousness": "alert", "on_oxygen": False}


def _post(client, pid, uid, role, minutes_ago=30, body=None):
    taken = (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()
    r = client.post(f"/api/nurse/patients/{pid}/observations",
                    json={**(body or _SET), "taken_at": taken}, headers=_auth(uid, role))
    assert r.status_code == 200, r.text
    return r.json()["vitals_id"]


def _void(client, vid, uid, role, reason="Heart rate keyed as 182, sheet says 82"):
    return client.post(f"/api/nurse/observations/{vid}/void",
                       json={"reason": reason}, headers=_auth(uid, role))


def _row(vid):
    with db_session() as db:
        return dict(db.execute("SELECT * FROM vitals WHERE id = ?", (vid,)).fetchone())


def _void_audits(vid):
    with db_session() as db:
        return db.execute(
            "SELECT actor_user_id, metadata_json FROM audit_logs "
            "WHERE action_type = 'observation_voided' AND target_id = ?",
            (str(vid),)).fetchall()


# ── The server rule ──────────────────────────────────────────────────────────

def test_the_clerk_strikes_out_her_own_copy_with_the_same_audit_row(client):
    pid = _mk_patient(DEPT_A)
    clerk = _mk_user("ward_clerk", DEPT_A)
    vid = _post(client, pid, clerk, "ward_clerk")
    r = _void(client, vid, clerk, "ward_clerk")
    assert r.status_code == 200, r.text
    row = _row(vid)
    assert row["heart_rate"] == 182            # the original stays
    assert row["voided_at"] and row["voided_by"] == clerk
    assert row["void_reason"] == "Heart rate keyed as 182, sheet says 82"
    audits = _void_audits(vid)
    assert len(audits) == 1 and audits[0]["actor_user_id"] == clerk


def test_the_clerk_cannot_strike_out_another_clerks_copy(client):
    pid = _mk_patient(DEPT_A)
    first = _mk_user("ward_clerk", DEPT_A)
    second = _mk_user("ward_clerk", DEPT_A)
    vid = _post(client, pid, first, "ward_clerk")
    r = _void(client, vid, second, "ward_clerk")
    assert r.status_code == 403
    assert r.json()["detail"] == NOT_YOURS
    assert _row(vid)["voided_at"] is None
    assert not _void_audits(vid)


def test_the_clerk_cannot_strike_out_a_nurses_set(client):
    pid = _mk_patient(DEPT_A)
    nurse = _mk_user("nurse", DEPT_A)
    clerk = _mk_user("ward_clerk", DEPT_A)
    vid = _post(client, pid, nurse, "nurse")
    r = _void(client, vid, clerk, "ward_clerk")
    assert r.status_code == 403 and r.json()["detail"] == NOT_YOURS
    assert _row(vid)["voided_at"] is None


def test_a_set_she_keyed_that_is_not_a_copy_is_still_not_hers_to_strike(client):
    """The rule is source clerk_transcribed AND keyed by her, both halves."""
    pid = _mk_patient(DEPT_A)
    clerk = _mk_user("ward_clerk", DEPT_A)
    vid = _post(client, pid, clerk, "ward_clerk")
    with db_session() as db:
        db.execute("UPDATE vitals SET source = 'nurse_manual' WHERE id = ?", (vid,))
    r = _void(client, vid, clerk, "ward_clerk")
    assert r.status_code == 403 and r.json()["detail"] == NOT_YOURS


def test_a_nurse_still_strikes_out_a_clerks_copy(client):
    pid = _mk_patient(DEPT_A)
    clerk = _mk_user("ward_clerk", DEPT_A)
    nurse = _mk_user("nurse", DEPT_A)
    vid = _post(client, pid, clerk, "ward_clerk")
    assert _void(client, vid, nurse, "nurse").status_code == 200


def test_the_reason_rule_is_the_same_for_the_clerk(client):
    pid = _mk_patient(DEPT_A)
    clerk = _mk_user("ward_clerk", DEPT_A)
    vid = _post(client, pid, clerk, "ward_clerk")
    assert _void(client, vid, clerk, "ward_clerk", reason="   ").status_code == 422
    assert _void(client, vid, clerk, "ward_clerk", reason="x" * 501).status_code == 422
    assert _row(vid)["voided_at"] is None
    assert _void(client, vid, clerk, "ward_clerk", reason="first").status_code == 200
    assert _void(client, vid, clerk, "ward_clerk", reason="second").status_code == 409
    assert _row(vid)["void_reason"] == "first"


def test_the_clerk_is_ward_scoped_through_the_row(client):
    pid = _mk_patient(DEPT_B)
    clerk_b = _mk_user("ward_clerk", DEPT_B)
    clerk_a = _mk_user("ward_clerk", DEPT_A)
    vid = _post(client, pid, clerk_b, "ward_clerk")
    # Out of her ward reads as no such set, before any question of whose it is.
    assert _void(client, vid, clerk_a, "ward_clerk").status_code == 404


def test_an_account_with_neither_permission_is_refused(client):
    pid = _mk_patient(DEPT_A)
    clerk = _mk_user("ward_clerk", DEPT_A)
    pharmacist = _mk_user("pharmacist", DEPT_A)
    vid = _post(client, pid, clerk, "ward_clerk")
    assert _void(client, vid, pharmacist, "pharmacist").status_code == 403


def test_who_keyed_a_set_is_read_from_its_audit_row(client):
    pid = _mk_patient(DEPT_A)
    clerk = _mk_user("ward_clerk", DEPT_A)
    vid = _post(client, pid, clerk, "ward_clerk")
    with db_session() as db:
        assert nurse_shift_service.observation_keyed_by(db, vid, pid) == clerk
        assert nurse_shift_service.observation_keyed_by(db, 999999999, pid) is None


# ── Her own copies ───────────────────────────────────────────────────────────

def test_her_copies_list_shows_only_her_own_sets_with_the_sheet_time_and_readings(client):
    pid = _mk_patient(DEPT_A)
    clerk = _mk_user("ward_clerk", DEPT_A)
    other = _mk_user("ward_clerk", DEPT_A)
    nurse = _mk_user("nurse", DEPT_A)
    older = _post(client, pid, clerk, "ward_clerk", minutes_ago=90)
    newer = _post(client, pid, clerk, "ward_clerk", minutes_ago=30,
                  body={**_SET, "heart_rate": 82})
    _post(client, pid, other, "ward_clerk", minutes_ago=60)
    _post(client, pid, nurse, "nurse", minutes_ago=10)
    assert _void(client, older, clerk, "ward_clerk").status_code == 200

    r = client.get(f"/api/nurse/transcribe/patients/{pid}/my-copies",
                   headers=_auth(clerk, "ward_clerk"))
    assert r.status_code == 200, r.text
    copies = r.json()["copies"]
    assert [c["vitals_id"] for c in copies] == [newer, older]
    assert set(copies[0]) == {"vitals_id", "taken_at", "readings", "voided"}
    assert copies[0]["readings"]["heart_rate"] == 82
    assert copies[0]["readings"]["on_oxygen"] is False
    assert copies[0]["voided"] is False and copies[1]["voided"] is True
    assert copies[0]["taken_at"] == _row(newer)["timestamp"]


def test_her_copies_list_leaves_out_a_copy_keyed_more_than_a_day_ago(client):
    pid = _mk_patient(DEPT_A)
    clerk = _mk_user("ward_clerk", DEPT_A)
    vid = _post(client, pid, clerk, "ward_clerk")
    with db_session() as db:
        db.execute("UPDATE vitals SET entered_at = ? WHERE id = ?",
                   ("2020-01-01T00:00:00+00:00", vid))
    r = client.get(f"/api/nurse/transcribe/patients/{pid}/my-copies",
                   headers=_auth(clerk, "ward_clerk"))
    assert r.json()["copies"] == []


def test_her_copies_list_is_hers_and_ward_scoped(client):
    pid = _mk_patient(DEPT_B)
    clerk_a = _mk_user("ward_clerk", DEPT_A)
    nurse = _mk_user("nurse", DEPT_B)
    url = f"/api/nurse/transcribe/patients/{pid}/my-copies"
    assert client.get(url, headers=_auth(clerk_a, "ward_clerk")).status_code == 404
    assert client.get(url, headers=_auth(nurse, "nurse")).status_code == 403
