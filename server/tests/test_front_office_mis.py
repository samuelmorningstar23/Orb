"""The front office day summary (opd-18, W8-B).

What this file defends:

  1. FLAG OFF, NOTHING NEW. GET /api/frontdesk/mis/daily is 404
     feature_disabled while the 'opd' feature is off, and 401 with no session.
  2. THE COUNTS, against a fixture day written out by hand: new against
     returning registrations, visits by type, by clinic and by doctor, the
     median and longest wait to be called, left without being seen by its
     cancel code, every other cancel by code, no-shows (recorded and computed
     once the day ended), and conversions to inpatient.
  3. THE DAY IS THE HOSPITAL'S: a record created at 01:30 IST on the 15th is
     new on the 15th even though its UTC date is the 14th; a day after today
     is refused.
  4. NO PHI: the answer carries no patient id, name, UHID or visit id.
  5. SCOPE: a departmented account sees its clinic only; an account with no
     department sees every clinic; the cashier is refused.
"""
from __future__ import annotations

import ast
import json
import re
import uuid
from datetime import timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

import config
from auth import create_token
from database import db_session
from main import app
from services import front_office_mis, hospital_time

client = TestClient(app)

CLINIC_A = "MIS Cardiology"
CLINIC_B = "MIS Orthopaedics"
DAY = "2026-09-15"


# ── fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def ist(monkeypatch):
    """Pin the hospital's zone so the day boundaries are the ones written here."""
    monkeypatch.setattr(hospital_time, "hospital_timezone", lambda: ZoneInfo("Asia/Kolkata"))


@pytest.fixture
def opd_on(monkeypatch):
    monkeypatch.setattr(config, "FRONTDESK_ENABLED", True)
    monkeypatch.setattr(config, "REGISTRATION_ENABLED", True)
    monkeypatch.setattr(config, "OPD_ENABLED", True)
    assert config.feature_enabled("opd")


@pytest.fixture
def opd_off(monkeypatch):
    monkeypatch.setattr(config, "OPD_ENABLED", False)
    assert not config.feature_enabled("opd")


def _mk_user(role, department=None, name=None):
    uid = f"MIS{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO users (id, name, role, password_hash, department, is_active) "
            "VALUES (?, ?, ?, 'x', ?, 1)",
            (uid, name or f"Mis {role} {uid[-4:]}", role, department),
        )
    return uid


def _auth(uid, role):
    return {"Authorization": f"Bearer {create_token(uid, role)}"}


def _patient(name, created_at, *, registered_at=None):
    pid = f"MISP{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO patients (id, name, mrn, age, sex, status, created_at) "
            "VALUES (?, ?, ?, 50, 'M', 'registered', ?)",
            (pid, name, f"UHMIS-{uuid.uuid4().hex[:8].upper()}", created_at),
        )
        if registered_at:
            db.execute(
                "INSERT INTO patient_demographics (patient_id, registered_at, registered_by, "
                "registration_source) VALUES (?, ?, 'X', 'desk')", (pid, registered_at))
    return pid


def _visit(pid, *, day=DAY, clinic=CLINIC_A, doctor=("DOC-MIS-1", "Dr Iyer"), token=1,
           status="completed", visit_type="new", registered="04:00", called=None,
           cancel_code=None, disposition=None):
    vid = f"MISV{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO opd_visits (id, patient_id, department, doctor_id, doctor_name, "
            "visit_date, token_number, visit_type, status, registered_at, called_at, "
            "cancel_reason_code, disposition) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (vid, pid, clinic, doctor[0] if doctor else None, doctor[1] if doctor else None,
             day, token, visit_type, status, f"{day}T{registered}:00+00:00",
             f"{day}T{called}:00+00:00" if called else None, cancel_code, disposition),
        )
    return vid


def _appointment(pid, *, day=DAY, clinic=CLINIC_A, status="booked"):
    aid = f"MISAP{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO appointments (id, patient_id, scheduled_date, scheduled_time, "
            "status, department) VALUES (?, ?, ?, '10:00', ?, ?)",
            (aid, pid, day, status, clinic),
        )
    return aid


@pytest.fixture(scope="module")
def staff():
    return {
        "recep_a": _mk_user("receptionist", CLINIC_A),
        "recep_all": _mk_user("receptionist", None),
        "doctor_b": _mk_user("doctor", CLINIC_B),
        "admin": _mk_user("admin", None),
        "cashier": _mk_user("cashier", None),
    }


@pytest.fixture(scope="module")
def day_fixture():
    """One hand-written day in two clinics.

    Clinic A, Dr Iyer: four called visits waiting 10, 30, 20 and 5 minutes
    (median 15, longest 30; the 5 is the visit converted to an admission),
    one still waiting, one LWBS, one cancelled as a no-show, one cancelled
    with no code.
    Clinic A, unassigned: one waiting. Clinic B: two completed visits.
    Records: two created that day (one at 01:30 IST, a UTC date earlier),
    the rest earlier.
    """
    old = "2025-01-10 06:00:00"
    p = {
        "new_desk": _patient("Asha New", old, registered_at=f"{DAY}T05:00:00+00:00"),
        "new_night": _patient("Bina Night", old, registered_at="2026-09-14T20:00:00+00:00"),
        "back_1": _patient("Chand Back", old),
        "back_2": _patient("Dev Back", old),
        "back_3": _patient("Esha Back", old),
        "back_4": _patient("Farah Back", old),
        "back_5": _patient("Gita Back", old),
        "back_6": _patient("Hari Back", old),
        "b_1": _patient("Ila Ortho", old),
        "b_2": _patient("Jai Ortho", old, registered_at=f"{DAY}T06:00:00+00:00"),
    }
    visits = {
        "w10": _visit(p["new_desk"], token=1, called="04:10"),
        "w30": _visit(p["back_1"], token=2, called="04:30", visit_type="follow_up"),
        "w20": _visit(p["back_2"], token=3, called="04:20", status="in_consultation"),
        "waiting": _visit(p["back_3"], token=4, status="waiting"),
        "lwbs": _visit(p["back_4"], token=5, status="cancelled",
                       cancel_code="left_without_being_seen"),
        "noshow": _visit(p["back_5"], token=6, status="cancelled", cancel_code="no_show"),
        "uncoded": _visit(p["back_6"], token=7, status="cancelled", visit_type="review"),
        "admitted": _visit(p["new_night"], token=8, called="04:05", disposition="admitted"),
        "unassigned": _visit(p["back_3"], token=1, doctor=None, status="waiting"),
        "b_1": _visit(p["b_1"], clinic=CLINIC_B, doctor=("DOC-MIS-2", "Dr Bose"), called="05:00",
                      registered="04:00", visit_type="procedure"),
        "b_2": _visit(p["b_2"], clinic=CLINIC_B, doctor=("DOC-MIS-2", "Dr Bose"), token=2,
                      called="04:15"),
    }
    appts = {
        "recorded": _appointment(p["back_1"], status="no_show"),
        "computed": _appointment(p["back_2"], status="booked"),
        "checked_in": _appointment(p["back_3"], status="checked_in"),
        "other_clinic": _appointment(p["b_1"], clinic=CLINIC_B, status="booked"),
    }
    return {"patients": p, "visits": visits, "appointments": appts}


def _get(uid, role, day=DAY):
    path = "/api/frontdesk/mis/daily" + (f"?date={day}" if day else "")
    return client.get(path, headers=_auth(uid, role))


# ═════════════════════════════════════════════════════════════════════════════
# 1. Flag off
# ═════════════════════════════════════════════════════════════════════════════

def test_the_summary_is_absent_while_the_feature_is_off(staff, opd_off):
    resp = _get(staff["admin"], "admin")
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "feature_disabled"


def test_no_session_is_401(opd_on):
    assert client.get("/api/frontdesk/mis/daily").status_code == 401


# ═════════════════════════════════════════════════════════════════════════════
# 2. The counts
# ═════════════════════════════════════════════════════════════════════════════

def test_one_clinics_day_against_the_fixture(staff, opd_on, day_fixture):
    resp = _get(staff["recep_a"], "receptionist")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["date"] == DAY and body["department"] == CLINIC_A

    # Distinct patients in clinic A: 8 (back_3 came twice); two records new that day.
    assert body["registrations"] == {"new": 2, "returning": 6, "not_known": 0, "patients": 8}
    assert body["visits"]["total"] == 9
    assert body["visits"]["by_status"] == {"waiting": 2, "in_consultation": 1,
                                           "completed": 3, "cancelled": 3}
    assert {t["code"]: t["count"] for t in body["visits"]["by_type"]} == {
        "new": 7, "follow_up": 1, "review": 1, "procedure": 0}

    # Waits to be called: 10, 30, 20 and 5 minutes.
    assert body["waits"] == {"measured": 4, "median_minutes": 15.0, "longest_minutes": 30,
                             "still_waiting": 2}
    assert body["left_without_being_seen"] == 1
    codes = {c["code"]: c["count"] for c in body["cancellations"]}
    assert codes["left_without_being_seen"] == 1 and codes["no_show"] == 1
    assert codes["not_coded"] == 1 and codes["patient_request"] == 0
    assert body["conversions"] == 1
    assert body["no_shows"] == {"available": True, "recorded": 1, "computed": 1, "total": 2}

    [clinic] = body["clinics"]
    assert clinic["department"] == CLINIC_A and clinic["total"] == 9
    doctors = {d["doctor_name"]: d for d in clinic["doctors"]}
    assert set(doctors) == {"Dr Iyer", "Unassigned"}
    iyer = doctors["Dr Iyer"]
    assert iyer["total"] == 8 and iyer["completed"] == 3 and iyer["left_without_being_seen"] == 1
    assert iyer["admitted"] == 1
    assert iyer["wait"] == {"measured": 4, "median_minutes": 15.0, "longest_minutes": 30}
    assert doctors["Unassigned"]["doctor_id"] is None
    assert doctors["Unassigned"]["wait"]["median_minutes"] is None


def test_an_account_with_no_department_sees_every_clinic(staff, opd_on, day_fixture):
    body = _get(staff["recep_all"], "receptionist").json()
    clinics = {c["department"]: c for c in body["clinics"]}
    assert CLINIC_A in clinics and CLINIC_B in clinics
    assert clinics[CLINIC_B]["total"] == 2
    assert clinics[CLINIC_B]["wait"] == {"measured": 2, "median_minutes": 37.5,
                                         "longest_minutes": 60}
    assert body["department"] is None
    assert body["no_shows"]["computed"] >= 2


def test_a_departmented_doctor_sees_their_clinic_only(staff, opd_on, day_fixture):
    body = _get(staff["doctor_b"], "doctor").json()
    assert [c["department"] for c in body["clinics"]] == [CLINIC_B]
    assert body["visits"]["total"] == 2
    assert body["registrations"]["new"] == 1
    assert body["no_shows"] == {"available": True, "recorded": 0, "computed": 1, "total": 1}
    assert body["left_without_being_seen"] == 0


def test_an_empty_day_reads_as_empty_not_as_zero_waits(staff, opd_on):
    body = _get(staff["recep_a"], "receptionist", day="2001-02-03").json()
    assert body["visits"]["total"] == 0
    assert body["waits"]["median_minutes"] is None and body["waits"]["longest_minutes"] is None
    assert body["clinics"] == []


# ═════════════════════════════════════════════════════════════════════════════
# 3. The hospital's day
# ═════════════════════════════════════════════════════════════════════════════

def test_a_record_made_at_01_30_ist_is_new_on_that_day(opd_on, day_fixture):
    pid = day_fixture["patients"]["new_night"]
    with db_session() as db:
        stored = db.execute("SELECT registered_at FROM patient_demographics WHERE patient_id = ?",
                            (pid,)).fetchone()[0]
    assert stored.startswith("2026-09-14")
    with db_session() as db:
        split = front_office_mis._registration_split(
            db, front_office_mis.resolve_day(DAY), [pid])
    assert split["new"] == 1 and split["returning"] == 0


def test_a_day_after_today_is_refused(staff, opd_on):
    tomorrow = (hospital_time.hospital_today() + timedelta(days=1)).isoformat()
    resp = _get(staff["admin"], "admin", day=tomorrow)
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"] == "future_day"
    bad = _get(staff["admin"], "admin", day="15-09-2026")
    assert bad.status_code == 422 and bad.json()["detail"]["error"] == "bad_date"


def test_today_is_the_default(staff, opd_on):
    body = _get(staff["admin"], "admin", day=None).json()
    assert body["date"] == hospital_time.hospital_today().isoformat()


# ═════════════════════════════════════════════════════════════════════════════
# 4. No PHI
# ═════════════════════════════════════════════════════════════════════════════

def test_the_answer_carries_no_patient_identifier(staff, opd_on, day_fixture):
    body = _get(staff["admin"], "admin").json()
    text = json.dumps(body)
    with db_session() as db:
        for pid in day_fixture["patients"].values():
            row = db.execute("SELECT name, mrn FROM patients WHERE id = ?", (pid,)).fetchone()
            assert pid not in text and row["name"] not in text and row["mrn"] not in text
    for vid in day_fixture["visits"].values():
        assert vid not in text
    for aid in day_fixture["appointments"].values():
        assert aid not in text
    banned = {"patient_id", "patient_name", "patient_mrn", "uhid", "name", "visit_id",
              "chief_complaint", "token_number"}

    def keys(node):
        if isinstance(node, dict):
            for k, v in node.items():
                yield k
                yield from keys(v)
        elif isinstance(node, list):
            for v in node:
                yield from keys(v)

    assert not (set(keys(body)) & banned)


def test_a_read_writes_no_phi_access_row(staff, opd_on, day_fixture):
    with db_session() as db:
        before = db.execute("SELECT COUNT(*) FROM audit_logs WHERE event_category = 'phi_access' "
                            "AND actor_user_id = ?", (staff["recep_a"],)).fetchone()[0]
    assert _get(staff["recep_a"], "receptionist").status_code == 200
    with db_session() as db:
        after = db.execute("SELECT COUNT(*) FROM audit_logs WHERE event_category = 'phi_access' "
                           "AND actor_user_id = ?", (staff["recep_a"],)).fetchone()[0]
    assert after == before


def test_the_summary_reads_no_scoring_and_writes_nothing():
    source = Path(front_office_mis.__file__).read_text()
    tree = ast.parse(source)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
            imported.update(f"{node.module}.{a.name}" for a in node.names)
        elif isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
    for banned in ("services.vigil_service", "services.nurse_shift_service",
                   "services.world_model_service", "services.news2_text"):
        assert not any(i == banned or i.startswith(banned + ".") for i in imported), banned
    # The code's own strings and names, docstrings left out.
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef)):
            doc = ast.get_docstring(node, clean=False)
            if doc:
                docstrings.add(doc)
    strings = [n.value for n in ast.walk(tree)
               if isinstance(n, ast.Constant) and isinstance(n.value, str)
               and n.value not in docstrings]
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | {
        n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    sql = " ".join(strings).upper()
    for verb in ("INSERT ", "UPDATE ", "DELETE ", "REPLACE "):
        assert verb not in sql, verb
    for table in ("VITALS", "RISK_SCORES"):
        assert not re.search(rf"\b(FROM|JOIN)\s+{table}\b", sql), table
    for word in ("compute_news2", "score_vitals_row", "compute_mews", "news2_reason_line",
                 "scored_admitted_patients"):
        assert word not in names, word


# ═════════════════════════════════════════════════════════════════════════════
# 5. Who may read it
# ═════════════════════════════════════════════════════════════════════════════

def test_the_cashier_is_refused(staff, opd_on):
    assert _get(staff["cashier"], "cashier").status_code == 403
