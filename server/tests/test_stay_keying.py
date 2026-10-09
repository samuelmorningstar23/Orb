"""Stay keying for repeat admissions (opd-17b, W6-B; migration 65 absorbs
adt-18a's one-active-admission index).

Until now a patient was one stay: every patients row came from a fresh
admission. Readmission onto the same record (opd-17a, opd-17c, the Ward
Record's returning patient) breaks that, so this increment keys what must be
keyed on the admission, once, for every database, whatever the switches say:

  * migration 65: discharge_summaries.admission_id (backfilled only where the
    stay is certain) and idx_admissions_one_active, one active admission per
    patient, built only after a pre-check that refuses and names violators;
  * discharge_service writes admission_id on every summary;
  * /api/vigil/discharged lists a patient once (their latest summary);
  * NABH inpatient days and the shadow scorecard census count a patient's
    stays one by one, and are unchanged where each patient has one stay;
  * one case room per patient is the rule, pinned here;
  * the written audit of every stay-keyed reader is STAY_KEYED_READER_AUDIT
    below: each reader is fixed here, correct as it is, or listed for
    opd-17c, and the test keeps every entry pointing at code that exists.
"""
from __future__ import annotations

import importlib
import json
import re
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import config
from auth import create_token
from database import SCHEMA, db_session
from db_backend import DBError, DBIntegrityError
from main import app
from services import discharge_service, nabh_service
from services import shadow_scorecard_service as sc
from services.admission_service import admit_patient
from services.clinical_time import format_utc

client = TestClient(app)
SERVER = Path(__file__).resolve().parent.parent
M65 = importlib.import_module("migrations_his.m0065_stay_keying")
DEPT = "General Medicine"
_SEEDED: list[str] = []


@pytest.fixture(autouse=True)
def _discharge_seeded():
    yield
    if not _SEEDED:
        return
    with db_session() as db:
        db.executemany("UPDATE patients SET status = 'discharged' WHERE id = ?",
                       [(p,) for p in _SEEDED])
        db.executemany("UPDATE admissions SET status = 'discharged' "
                       "WHERE patient_id = ? AND status = 'active'", [(p,) for p in _SEEDED])
    _SEEDED.clear()


def _tag() -> str:
    return uuid.uuid4().hex[:8].upper()


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _sql_time(dt: datetime) -> str:
    """The shape discharge_summaries.discharged_at is stored in."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _patient(*, status="discharged", ward=None, admission_date=None, user_id=None) -> str:
    pid = f"PSK{_tag()}"
    with db_session() as db:
        db.execute("INSERT INTO patients (id, user_id, name, mrn, ward, status, admission_date) "
                   "VALUES (?, ?, ?, ?, ?, ?, ?)",
                   (pid, user_id, f"Stay Key {pid[-4:]}", f"MRN-{pid}", ward, status,
                    admission_date))
    return pid


def _stay(pid, *, status, created, ward=None, discharged_at=None, department=DEPT) -> str:
    aid = f"ADM-SK{_tag()}"
    with db_session() as db:
        db.execute("INSERT INTO admissions (id, patient_id, admitted_by, department, ward, "
                   "status, created_at, discharged_at) VALUES (?, ?, 'D001', ?, ?, ?, ?, ?)",
                   (aid, pid, department, ward, status, _iso(created),
                    _iso(discharged_at) if discharged_at else None))
    return aid


def _summary(pid, when: datetime, *, admission_id=None, text="Summary") -> int:
    with db_session() as db:
        db.execute("INSERT INTO discharge_summaries (patient_id, author_id, summary, "
                   "discharged_at, admission_id) VALUES (?, 'D001', ?, ?, ?)",
                   (pid, text, _sql_time(when), admission_id))
        return db.execute("SELECT last_insert_rowid()").fetchone()[0]


def _scratch_db() -> sqlite3.Connection:
    """An older schema: database.SCHEMA only, no migrations, plus the ledger
    columns migration 65's second backfill rule reads."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    for statement in (s for s in SCHEMA.split(";") if s.strip()):
        try:
            conn.execute(statement)
        except DBError:
            pass
    conn.execute("CREATE TABLE IF NOT EXISTS patient_movements (id TEXT, patient_id TEXT, "
                 "admission_id TEXT, movement_type TEXT, discharge_summary_id INTEGER)")
    conn.execute("INSERT INTO users (id, name, role, password_hash) VALUES ('D9', 'D', 'doctor', 'x')")
    return conn


# ═════════════════════════════════════════════════════════════════════════════
#  1. Migration 65
# ═════════════════════════════════════════════════════════════════════════════

def test_a_fresh_database_carries_the_column_and_both_indexes():
    with db_session() as db:
        cols = {r[1] for r in db.execute("PRAGMA table_info(discharge_summaries)").fetchall()}
        indexes = {r[0]: r[1] for r in db.execute(
            "SELECT name, sql FROM sqlite_master WHERE type = 'index'").fetchall()}
        applied = {r[0] for r in db.execute("SELECT version FROM schema_migrations").fetchall()}
    assert "admission_id" in cols and 65 in applied
    assert M65.SUMMARY_ADMISSION_INDEX_NAME in indexes
    one_active = indexes[M65.ONE_ACTIVE_INDEX_NAME]
    assert "UNIQUE" in one_active.upper() and "status = 'active'" in one_active


def test_backfill_keys_a_single_stay_and_leaves_the_uncertain_null():
    conn = _scratch_db()
    conn.execute("INSERT INTO patients (id, name, mrn) VALUES ('PA', 'A', 'MA')")
    conn.execute("INSERT INTO admissions (id, patient_id, admitted_by, status) "
                 "VALUES ('ADM-A', 'PA', 'D9', 'discharged')")
    conn.execute("INSERT INTO patients (id, name, mrn) VALUES ('PB', 'B', 'MB')")
    conn.execute("INSERT INTO admissions (id, patient_id, admitted_by, status) "
                 "VALUES ('ADM-B1', 'PB', 'D9', 'discharged')")
    conn.execute("INSERT INTO admissions (id, patient_id, admitted_by, status) "
                 "VALUES ('ADM-B2', 'PB', 'D9', 'active')")
    conn.execute("INSERT INTO patients (id, name, mrn) VALUES ('PC', 'C', 'MC')")
    conn.execute("INSERT INTO admissions (id, patient_id, admitted_by, status) "
                 "VALUES ('ADM-C1', 'PC', 'D9', 'discharged')")
    conn.execute("INSERT INTO admissions (id, patient_id, admitted_by, status) "
                 "VALUES ('ADM-C2', 'PC', 'D9', 'discharged')")
    conn.execute("INSERT INTO patients (id, name, mrn) VALUES ('PD', 'D', 'MD')")
    for pid in ("PA", "PB", "PC", "PD"):
        conn.execute("INSERT INTO discharge_summaries (patient_id, author_id, summary) "
                     "VALUES (?, 'D9', 's')", (pid,))
    sid_c = conn.execute("SELECT id FROM discharge_summaries WHERE patient_id = 'PC'").fetchone()[0]
    conn.execute("INSERT INTO patient_movements (id, patient_id, admission_id, movement_type, "
                 "discharge_summary_id) VALUES ('MOV-1', 'PC', 'ADM-C1', 'discharge', ?)", (sid_c,))
    M65.apply(conn)
    M65.apply(conn)     # idempotent
    got = {r[0]: r[1] for r in conn.execute(
        "SELECT patient_id, admission_id FROM discharge_summaries").fetchall()}
    assert got == {"PA": "ADM-A", "PB": None, "PC": "ADM-C1", "PD": None}
    with pytest.raises(DBIntegrityError):
        conn.execute("INSERT INTO admissions (id, patient_id, admitted_by, status) "
                     "VALUES ('ADM-B3', 'PB', 'D9', 'active')")


def test_the_migration_refuses_while_a_patient_holds_two_active_stays_and_changes_nothing():
    conn = _scratch_db()
    conn.execute("INSERT INTO patients (id, name, mrn) VALUES ('PY', 'Y', 'MY')")
    for aid in ("A1", "A2"):
        conn.execute("INSERT INTO admissions (id, patient_id, admitted_by, status) "
                     "VALUES (?, 'PY', 'D9', 'active')", (aid,))
    with pytest.raises(M65.OneActiveAdmissionRefused) as caught:
        M65.apply(conn)
    assert caught.value.patient_ids == ["PY"] and "PY" in str(caught.value)
    assert "admission_id" not in {r[1] for r in conn.execute(
        "PRAGMA table_info(discharge_summaries)").fetchall()}
    assert M65.ONE_ACTIVE_INDEX_NAME not in {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'index'").fetchall()}


def test_one_active_admission_per_patient_whatever_the_switches_say():
    pid = _patient(status="admitted")
    first = _stay(pid, status="active", created=datetime.now(timezone.utc))
    with pytest.raises(DBIntegrityError):
        _stay(pid, status="active", created=datetime.now(timezone.utc))
    # An ended stay beside it is fine, and once the first ends a new one opens.
    _stay(pid, status="discharged", created=datetime.now(timezone.utc) - timedelta(days=9))
    with db_session() as db:
        db.execute("UPDATE admissions SET status = 'discharged' WHERE id = ?", (first,))
    _stay(pid, status="active", created=datetime.now(timezone.utc))
    _SEEDED.append(pid)


# ═════════════════════════════════════════════════════════════════════════════
#  2. discharge_service writes admission_id
# ═════════════════════════════════════════════════════════════════════════════

def _admit(**over) -> dict:
    body = {"name": f"Stay Keyed {_tag()}", "age": 50, "sex": "M", "department": DEPT,
            "ward": "SK-W", "bed": f"{_tag()[:3]}", "attending_doctor_id": "D001",
            "admission_source": "er", "admission_type": "emergency",
            "chief_complaint": "Breathless"}
    body.update(over)
    out = admit_patient(body, {"id": "D001", "name": "Dr. Test", "role": "doctor",
                               "department": DEPT})
    _SEEDED.append(out["patient_id"])
    return out


def test_a_discharge_keys_its_summary_to_the_stay_it_ends():
    out = _admit()
    result = discharge_service.discharge_patient(out["patient_id"], "D001", "Summary", [])
    assert result["status"] == "success"
    with db_session() as db:
        row = db.execute("SELECT admission_id FROM discharge_summaries WHERE id = ?",
                         (result["summary_id"],)).fetchone()
    assert row["admission_id"] == out["admission_id"]


def test_with_beds_and_admissions_on_the_summary_is_keyed_too(monkeypatch):
    monkeypatch.setattr(config, "ADT_ENABLED", True)
    out = _admit(ward="", bed="", awaiting_bed=True)
    result = discharge_service.discharge_patient(out["patient_id"], "D001", "Summary", [])
    assert result["status"] == "success" and "end_of_stay" in result
    with db_session() as db:
        row = db.execute("SELECT admission_id FROM discharge_summaries WHERE id = ?",
                         (result["summary_id"],)).fetchone()
    assert row["admission_id"] == out["admission_id"]


def test_a_patient_with_no_admission_row_gets_a_null_key():
    pid = _patient(status="admitted")
    result = discharge_service.discharge_patient(pid, "D001", "Summary", [])
    assert result["status"] == "success"
    with db_session() as db:
        row = db.execute("SELECT admission_id FROM discharge_summaries WHERE id = ?",
                         (result["summary_id"],)).fetchone()
    assert row["admission_id"] is None


# ═════════════════════════════════════════════════════════════════════════════
#  3. /api/vigil/discharged: one row per patient
# ═════════════════════════════════════════════════════════════════════════════

def _reader(department: str) -> dict:
    uid = f"SKD{_tag()}"
    with db_session() as db:
        db.execute("INSERT INTO users (id, name, role, password_hash, department, is_active) "
                   "VALUES (?, ?, 'doctor', 'x', ?, 1)", (uid, f"Dr {uid}", department))
    return {"Authorization": f"Bearer {create_token(uid, 'doctor')}"}


def _discharged_rows(headers) -> list[dict]:
    resp = client.get("/api/vigil/discharged?limit=500", headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_a_twice_discharged_patient_is_listed_once_with_the_latest_summary():
    dept = f"SK Dept {_tag()}"
    now = datetime.now(timezone.utc)
    pid = _patient()
    a1 = _stay(pid, status="discharged", created=now - timedelta(days=10), department=dept)
    a2 = _stay(pid, status="discharged", created=now - timedelta(days=3), department=dept)
    _summary(pid, now - timedelta(days=8), admission_id=a1, text="First stay")
    _summary(pid, now - timedelta(days=1), admission_id=a2, text="Second stay")
    once = _patient()
    _stay(once, status="discharged", created=now - timedelta(days=5), department=dept)
    single_at = now - timedelta(days=2)
    _summary(once, single_at, text="Only stay")

    hdr = _reader(dept)
    resp = client.get("/api/vigil/discharged?limit=500", headers=hdr)
    rows = resp.json()
    mine = [r for r in rows if r["id"] == pid]
    assert len(mine) == 1
    assert mine[0]["summary"] == "Second stay" and mine[0]["admission_id"] == a2
    assert mine[0]["discharged_at"] == _sql_time(now - timedelta(days=1))
    assert len(rows) == int(resp.headers["X-Total-Count"])
    single = [r for r in rows if r["id"] == once]
    assert len(single) == 1 and single[0]["discharged_at"] == _sql_time(single_at)
    assert [r["id"] for r in rows] == [pid, once]   # latest end first


def test_a_later_end_with_no_summary_dates_the_row():
    dept = f"SK Dept {_tag()}"
    now = datetime.now(timezone.utc)
    pid = _patient()
    a1 = _stay(pid, status="discharged", created=now - timedelta(days=10), department=dept)
    a2 = _stay(pid, status="discharged", created=now - timedelta(days=3), department=dept,
               discharged_at=now - timedelta(hours=5))
    _summary(pid, now - timedelta(days=8), admission_id=a1, text="First stay")
    with db_session() as db:
        db.execute("INSERT INTO patient_movements (id, patient_id, admission_id, movement_type, "
                   "reason, moved_at, recorded_at) VALUES (?, ?, ?, 'discharge', 'Left', ?, ?)",
                   (f"MOV-SK{_tag()}", pid, a2, _iso(now - timedelta(hours=5)),
                    _iso(now - timedelta(hours=5))))
    rows = [r for r in _discharged_rows(_reader(dept)) if r["id"] == pid]
    assert len(rows) == 1
    assert rows[0]["discharged_at"] == _sql_time(now - timedelta(hours=5))


# ═════════════════════════════════════════════════════════════════════════════
#  4. NABH inpatient days and the shadow scorecard, per stay
# ═════════════════════════════════════════════════════════════════════════════

def _two_stay_patient(ward: str, *, keyed=True) -> str:
    """Stay 1: six to four days ago, ended by its summary (ORB_ADT off, so the
    admission records no end). Stay 2: since a day ago, still open."""
    now = datetime.now(timezone.utc)
    pid = _patient(status="admitted", ward=ward, admission_date=_iso(now - timedelta(days=1)))
    a1 = _stay(pid, status="discharged", created=now - timedelta(days=6), ward=ward)
    _stay(pid, status="active", created=now - timedelta(days=1), ward=ward)
    _summary(pid, now - timedelta(days=4), admission_id=a1 if keyed else None)
    _SEEDED.append(pid)
    return pid


def _window():
    now = datetime.now(timezone.utc)
    return now - timedelta(days=7), now + timedelta(days=1)


def test_nabh_counts_two_stays_separately(monkeypatch):
    monkeypatch.setattr(config, "ADT_ENABLED", False)
    ward = f"SK-NB-{_tag()}"
    _two_stay_patient(ward)
    start, end = _window()
    with db_session() as db:
        days = nabh_service._inpatient_days(db, start, end, ward)
    assert days["stays"] == 2 and days["patients"] == 1 and days["repeat_stay_patients"] == 1
    assert 2.9 <= days["inpatient_days"] <= 3.1      # 2 days ended + 1 day open
    assert days["open_stays"] == 1


def _vital(pid, when: datetime, *, voided=False) -> None:
    """One heart rate, in the stored spelling, nothing carried forward;
    `voided` marks it entered in error."""
    with db_session() as db:
        db.execute("INSERT INTO vitals (patient_id, timestamp, heart_rate, carried_from, "
                   "voided_at) VALUES (?, ?, 80, NULL, ?)",
                   (pid, format_utc(when), format_utc(when) if voided else None))


def _summary_less_then_open(ward: str, *, vitals_ago: tuple = ()) -> str:
    """Stay 1: from six days ago, ended with no summary and no end recorded
    (exactly what roster_import's close writes with ORB_ADT off). Stay 2: from
    two days ago, still open, one vital an hour ago, and the vitals named in
    `vitals_ago` (timedeltas before now). Stay 2 carries an arrival time, so
    with the beds and admissions module on the pack counts per stay."""
    now = datetime.now(timezone.utc)
    pid = _patient(status="admitted", ward=ward, admission_date=_iso(now - timedelta(days=2)))
    _stay(pid, status="discharged", created=now - timedelta(days=6), ward=ward)
    second = _stay(pid, status="active", created=now - timedelta(days=2), ward=ward)
    with db_session() as db:
        db.execute("UPDATE admissions SET admitted_at = created_at WHERE id = ?", (second,))
    for ago in (timedelta(hours=1), *vitals_ago):
        _vital(pid, now - ago)
    _SEEDED.append(pid)
    return pid


def test_nabh_never_counts_the_gap_or_overlap_between_stays(monkeypatch):
    """Stay 1 has no summary and no vital of its own; the patient's latest
    vital is in stay 2. Stay 1 is closed at its own start (the one-stay rule:
    its last vital, else its start), never stretched to stay 2's start by a
    reading from stay 2, so the days at home are never inpatient days and no
    hour is counted twice. The same in both module states."""
    ward = f"SK-NB-{_tag()}"
    _summary_less_then_open(ward)
    start, end = _window()
    monkeypatch.setattr(config, "ADT_ENABLED", False)
    with db_session() as db:
        off = nabh_service._inpatient_days(db, start, end, ward)
    assert 1.9 <= off["inpatient_days"] <= 2.1       # stay 2 only, never 6
    assert off["assumed_ends"] == 1 and off["open_stays"] == 1
    assert off["patients"] == 1 and off["repeat_stay_patients"] == 1
    monkeypatch.setattr(config, "ADT_ENABLED", True)
    with db_session() as db:
        on = nabh_service._inpatient_days(db, start, end, ward)
    assert on["method"].startswith("Counted per stay")
    assert 1.9 <= on["inpatient_days"] <= 2.1
    assert on["assumed_ends"] == 1 and on["open_stays"] == 1


def test_nabh_closes_a_summary_less_stay_at_its_own_last_vital(monkeypatch):
    """A vital inside stay 1 (five days ago) closes it there; one taken at the
    very instant stay 2 began belongs to stay 2 and does not stretch stay 1,
    and nor does a set inside stay 1 marked entered in error."""
    ward = f"SK-NB-{_tag()}"
    pid = _summary_less_then_open(ward, vitals_ago=(timedelta(days=5),))
    with db_session() as db:
        second_start = db.execute(
            "SELECT created_at FROM admissions WHERE patient_id = ? AND status = 'active'",
            (pid,)).fetchone()[0]
    _vital(pid, datetime.fromisoformat(second_start))
    _vital(pid, datetime.now(timezone.utc) - timedelta(days=3), voided=True)
    start, end = _window()
    for adt in (False, True):
        monkeypatch.setattr(config, "ADT_ENABLED", adt)
        with db_session() as db:
            days = nabh_service._inpatient_days(db, start, end, ward)
        assert 2.9 <= days["inpatient_days"] <= 3.1, adt   # 1 day + 2 days
        assert days["stays"] == 2 and days["assumed_ends"] == 1, adt


def test_nabh_single_stay_data_is_counted_exactly_as_before(monkeypatch):
    monkeypatch.setattr(config, "ADT_ENABLED", False)
    ward = f"SK-NB-{_tag()}"
    now = datetime.now(timezone.utc)
    pid = _patient(status="discharged", ward=ward, admission_date=_iso(now - timedelta(days=3)))
    aid = _stay(pid, status="discharged", created=now - timedelta(days=3), ward=ward)
    _summary(pid, now - timedelta(days=1), admission_id=aid)
    start, end = _window()
    with db_session() as db:
        days = nabh_service._inpatient_days(db, start, end, ward)
    assert "stays" not in days and "repeat_stay_patients" not in days
    assert days["patients"] == 1 and 1.9 <= days["inpatient_days"] <= 2.1
    assert days["method"].startswith("Counted from each patient's admission date")


def test_nabh_leaves_history_imported_encounters_out_of_the_repeat_count(monkeypatch):
    monkeypatch.setattr(config, "ADT_ENABLED", False)
    ward = f"SK-NB-{_tag()}"
    now = datetime.now(timezone.utc)
    pid = _patient(status="admitted", ward=ward, admission_date=_iso(now - timedelta(days=1)))
    old = _stay(pid, status="discharged", created=now - timedelta(days=6), ward=ward,
                discharged_at=now - timedelta(days=4))
    with db_session() as db:
        db.execute("UPDATE admissions SET import_run_id = 'RUN-SK' WHERE id = ?", (old,))
    _stay(pid, status="active", created=now - timedelta(days=1), ward=ward)
    _SEEDED.append(pid)
    start, end = _window()
    with db_session() as db:
        days = nabh_service._inpatient_days(db, start, end, ward)
    assert "stays" not in days and 0.9 <= days["inpatient_days"] <= 1.1


def test_scorecard_bed_days_and_census_count_two_stays_separately():
    ward = f"SK-SC-{_tag()}"
    _two_stay_patient(ward)
    start, end = _window()
    now = datetime.now(timezone.utc)
    windows = [{"ward": ward, "start": start, "end": now}]
    with db_session() as db:
        beds = sc._occupied_bed_days(db, windows, ward)
        wards, diagnostics = sc._census_wards(db, start, now, ward)
    assert beds["stays"] == 2 and beds["patients"] == 1 and beds["repeat_stay_patients"] == 1
    assert 2.9 <= beds["bed_days"] <= 3.1
    assert wards == {ward}
    assert diagnostics["repeat_stays_in_window"] == 2
    assert diagnostics["patients_in_window"] == 1 and diagnostics["patients_scanned"] == 1


def test_scorecard_never_counts_the_days_at_home_after_a_summary_less_stay():
    """Stay 1 ended with no summary and no vital of its own; the patient's
    latest vital is in stay 2. Bed-days count stay 2 only (stay 1 closes at
    its own start, as one stay does), never stay 1 stretched to stay 2's
    start, which would OVER-state bed-days and deflate the false-alert rate.
    The census still places both stays in the window."""
    ward = f"SK-SC-{_tag()}"
    _summary_less_then_open(ward)
    start, _end = _window()
    now = datetime.now(timezone.utc)
    windows = [{"ward": ward, "start": start, "end": now}]
    with db_session() as db:
        beds = sc._occupied_bed_days(db, windows, ward)
        wards, diagnostics = sc._census_wards(db, start, now, ward)
    assert 1.99 <= beds["bed_days"] <= 2.01           # never 6
    assert beds["assumed_ends"] == 1 and beds["open_stays"] == 1
    assert beds["stays"] == 1 and beds["repeat_stay_patients"] == 1
    assert wards == {ward}
    assert diagnostics["repeat_stays_in_window"] == 2


def test_scorecard_closes_a_summary_less_stay_at_its_own_last_vital():
    """A vital five days ago is stay 1's own: it closes there (1 day), and
    neither the vital an hour ago in stay 2 nor a set three days ago marked
    entered in error moves it."""
    ward = f"SK-SC-{_tag()}"
    pid = _summary_less_then_open(ward, vitals_ago=(timedelta(days=5),))
    _vital(pid, datetime.now(timezone.utc) - timedelta(days=3), voided=True)
    start, _end = _window()
    now = datetime.now(timezone.utc)
    windows = [{"ward": ward, "start": start, "end": now}]
    with db_session() as db:
        beds = sc._occupied_bed_days(db, windows, ward)
        stays = sc._repeat_stays(db)[pid]
    assert 2.99 <= beds["bed_days"] <= 3.01           # 1 day + 2 days
    assert beds["stays"] == 2 and beds["assumed_ends"] == 1
    assert stays[0]["end"] is None
    assert abs((stays[0]["last_vital"] - (now - timedelta(days=5))).total_seconds()) < 60
    assert stays[1]["last_vital"] is None              # still open: read to the window's end


def test_scorecard_single_stay_data_is_unchanged():
    ward = f"SK-SC-{_tag()}"
    now = datetime.now(timezone.utc)
    pid = _patient(status="discharged", ward=ward, admission_date=_iso(now - timedelta(days=3)))
    aid = _stay(pid, status="discharged", created=now - timedelta(days=3), ward=ward)
    _summary(pid, now - timedelta(days=1), admission_id=aid)
    start, _end = _window()
    windows = [{"ward": ward, "start": start, "end": now}]
    with db_session() as db:
        beds = sc._occupied_bed_days(db, windows, ward)
        _wards, diagnostics = sc._census_wards(db, start, now, ward)
    assert "stays" not in beds and beds["patients"] == 1
    assert 1.99 <= beds["bed_days"] <= 2.01
    assert "repeat_stays_in_window" not in diagnostics
    assert diagnostics["patients_in_window"] == 1


# ═════════════════════════════════════════════════════════════════════════════
#  5. One case room per patient
# ═════════════════════════════════════════════════════════════════════════════

def _rooms(pid) -> int:
    with db_session() as db:
        return db.execute("SELECT COUNT(*) FROM channels WHERE patient_id = ? "
                          "AND type = 'case_room'", (pid,)).fetchone()[0]


def test_an_admission_opens_exactly_one_case_room_and_a_discharge_keeps_it():
    out = _admit()
    assert _rooms(out["patient_id"]) == 1
    discharge_service.discharge_patient(out["patient_id"], "D001", "Summary", [])
    assert _rooms(out["patient_id"]) == 1


#: The only server code that may create a case room. Readers fetch a
#: patient's room by patient_id with no ORDER BY (bot_service, bridge_service,
#: helix_service, ingestion_service, sage_handoff_service, surgery_service and
#: others), so a second room per patient would split the conversation at
#: random. Readmission (opd-17c) must REUSE the room, never add one here.
CASE_ROOM_WRITERS = {"services/admission_service.py": 1}
_CHANNEL_INSERT = re.compile(r"INSERT\s+(?:OR\s+\w+\s+)?INTO\s+channels\b", re.IGNORECASE)


def test_one_case_room_per_patient_is_the_rule_in_code():
    found: dict[str, int] = {}
    for folder in ("services", "routes", "ws", "adapters"):
        for path in (SERVER / folder).rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            for match in _CHANNEL_INSERT.finditer(text):
                statement = text[match.start():match.start() + 400]
                rel = path.relative_to(SERVER).as_posix()
                typed = re.search(r"'(case_room|department|direct)'", statement)
                assert typed is not None, (
                    f"{rel} inserts a channel whose type is not written in the SQL; "
                    "say which type it is so this rule can be checked")
                if typed.group(1) == "case_room":
                    found[rel] = found.get(rel, 0) + 1
    for name in ("main.py", "database.py"):
        assert "'case_room'" not in "".join(
            m.group(0) for m in _CHANNEL_INSERT.finditer((SERVER / name).read_text()))
    assert found == CASE_ROOM_WRITERS


# ═════════════════════════════════════════════════════════════════════════════
#  6. Bridge after a second discharge (R029 (3))
# ═════════════════════════════════════════════════════════════════════════════

def test_the_portal_judges_from_the_stay_that_just_ended():
    from services.patient_portal_access import _end_of_local_day_utc, portal_access

    uid = f"PSKU{_tag()[:4]}"
    with db_session() as db:
        db.execute("INSERT INTO users (id, name, role, password_hash) "
                   "VALUES (?, 'Bridge', 'patient', 'x')", (uid,))
    now = datetime.now(timezone.utc)
    pid = _patient(user_id=uid)
    a1 = _stay(pid, status="discharged", created=now - timedelta(days=10))
    a2 = _stay(pid, status="discharged", created=now - timedelta(days=2))
    _summary(pid, now - timedelta(days=8), admission_id=a1)
    latest = now - timedelta(seconds=5)
    _summary(pid, latest, admission_id=a2)
    with db_session() as db:
        answer = portal_access(db, uid)
    assert answer["open"] is True
    assert answer["until"] == _end_of_local_day_utc(_sql_time(latest)).isoformat()


# ═════════════════════════════════════════════════════════════════════════════
#  7. The written audit of stay-keyed readers
# ═════════════════════════════════════════════════════════════════════════════

#: Every reader that keys on the patient where a stay is meant, as of
#: 9 October 2026. status: 'fixed_here' (this increment, tested above),
#: 'correct_as_is' (already right for repeat stays, say why), or 'opd-17c'
#: (must be handled before a discharged patient can be readmitted onto the
#: same record; readmission is not built, so none of these can misfire yet).
STAY_KEYED_READER_AUDIT = (
    {"reader": "discharge summary written", "file": "services/discharge_service.py",
     "symbol": "def discharge_patient", "status": "fixed_here",
     "note": "writes discharge_summaries.admission_id (migration 65)."},
    {"reader": "discharged patient list", "file": "routes/vigil_routes.py",
     "symbol": "def list_discharged", "status": "fixed_here",
     "note": "one row per patient, latest summary, later of summary and summary-less end."},
    {"reader": "discharge QR code", "file": "routes/vigil_routes.py",
     "symbol": "def _load_qr_path", "status": "fixed_here",
     "note": "latest summary with an id tiebreak."},
    {"reader": "NABH inpatient days", "file": "services/nabh_service.py",
     "symbol": "def _per_stay_totals", "status": "fixed_here",
     "note": "repeat stays counted one by one in every module state; a summary-less stay "
             "closes at its own last vital, never one from the next stay."},
    {"reader": "shadow scorecard bed days and ward census",
     "file": "services/shadow_scorecard_service.py", "symbol": "def _repeat_stays",
     "status": "fixed_here", "note": "repeat stays read from admissions, keyed summaries; "
     "a summary-less stay closes at its own last vital (_last_vital_in_stay)."},
    {"reader": "two open stays for one patient", "file": "migrations_his/m0065_stay_keying.py",
     "symbol": "ONE_ACTIVE_INDEX", "status": "fixed_here",
     "note": "every 'status = active LIMIT 1' reader now has one row to find."},
    {"reader": "Bridge portal window", "file": "services/patient_portal_access.py",
     "symbol": "MAX(discharged_at)", "status": "correct_as_is",
     "note": "the latest summary is the stay that just ended (R029 (3)), pinned above."},
    {"reader": "care bundles at discharge", "file": "services/adt/end_stay.py",
     "symbol": "def close_admission_on", "status": "correct_as_is",
     "note": "active bundles are cancelled at every discharge, so none carry over."},
    {"reader": "sepsis bundle start", "file": "services/sepsis_bundle_service.py",
     "symbol": "status = 'active'", "status": "correct_as_is",
     "note": "deduplicates on an ACTIVE bundle only, so a new stay can start one."},
    {"reader": "case rooms", "file": "services/admission_service.py",
     "symbol": "'case_room'", "status": "fixed_opd_17c",
     "note": "one room per patient (pinned above); a readmission reuses it with a "
             "READMISSION dividing line (READMISSION_DIVIDER), never opens a second "
             "(tests/test_readmission_same_record.py)."},
    {"reader": "open escalation chains", "file": "services/escalation_service.py",
     "symbol": "def patient_escalations", "status": "opd-17c",
     "note": "chains are never closed by a discharge (R009 held), so a readmitted "
             "patient would inherit the earlier stay's open chains."},
    {"reader": "medication chart and eMAR", "file": "services/emar_service.py",
     "symbol": "def record_administration", "status": "fixed_opd_17c",
     "note": "discharge leaves medications 'active'; ruling OPD-S8 (a) closes them in "
             "the readmission transaction (admission_service."
             "_close_previous_stay_medicines_on), so the eMAR refuses them."},
    {"reader": "Bridge medications", "file": "routes/bridge_routes.py",
     "symbol": "def patient_medications", "status": "opd-17c",
     "note": "reads every stay's medicines (R029 (1), still open); the fresh one-time "
             "Bridge password at readmission (R029 (2)) is built in opd-17c."},
    {"reader": "orders", "file": "services/orders_service.py",
     "symbol": "def list_orders", "status": "opd-17c",
     "note": "orders are keyed on the patient and not closed at discharge."},
    {"reader": "I-PASS handoff", "file": "services/ipass_service.py",
     "symbol": "def build_ipass", "status": "opd-17c",
     "note": "latest vitals, risk, medications and notes by patient (ruling ADT-S9)."},
    {"reader": "board, NEWS2 and Sage latest vitals", "file": "services/vigil_service.py",
     "symbol": "def scored_admitted_patients", "status": "fixed_opd_17c",
     "note": "the previous stay's rows are never the latest on the Vigil board, the "
             "NEWS2 entry points, the chart and Sage (ruling ADT-S9 (a), "
             "readmission_stay_starts_on); ORDER BY timestamp DESC, id DESC stays "
             "(tests/test_adt_readmit_boundaries.py). Other lanes are in the W8-A report."},
    {"reader": "discharge summary timeline and reconciliation",
     "file": "services/discharge_service.py", "symbol": "def build_patient_timeline",
     "status": "opd-17c", "note": "reads the whole record, not this stay; "
     "build_medication_reconciliation likewise."},
    {"reader": "stay end and summary to stay", "file": "services/stay_times.py",
     "symbol": "def admission_for_summary", "status": "opd-17c",
     "note": "matches a summary to its stay by time; should prefer "
             "discharge_summaries.admission_id (owner: adt)."},
    {"reader": "final bill flags on the discharged list", "file": "routes/vigil_routes.py",
     "symbol": "final_bill_raised", "status": "opd-17c",
     "note": "keyed on the patient, so a second stay reads the first stay's final bill "
             "(per-stay billing is adt-15)."},
    {"reader": "discharge links", "file": "services/discharge_service.py",
     "symbol": "INSERT INTO discharge_access_tokens", "status": "opd-17c",
     "note": "an earlier stay's link stays valid until its expiry; the link is "
             "in-hospital handover only (R027)."},
    {"reader": "roster import second admission", "file": "services/roster_import.py",
     "symbol": "an open admission already existed at write time", "status": "correct_as_is",
     "note": "re-checks for an active stay inside its transaction; the index now "
             "backs that check (its comment that no index exists is stale; W6-C file)."},
)
AUDIT_STATUSES = {"fixed_here", "correct_as_is", "opd-17c", "fixed_opd_17c"}


def test_every_audited_reader_points_at_code_that_exists():
    for entry in STAY_KEYED_READER_AUDIT:
        assert entry["status"] in AUDIT_STATUSES, entry
        assert len(entry["note"]) >= 20, entry
        path = SERVER / entry["file"]
        assert path.is_file(), entry["file"]
        assert entry["symbol"] in path.read_text(encoding="utf-8"), (
            f"{entry['reader']}: {entry['symbol']!r} is no longer in {entry['file']}; "
            "re-audit the reader and update STAY_KEYED_READER_AUDIT")
    assert sum(e["status"] == "fixed_here" for e in STAY_KEYED_READER_AUDIT) == 6
    assert any(e["status"] == "opd-17c" for e in STAY_KEYED_READER_AUDIT)
    json.dumps(STAY_KEYED_READER_AUDIT)   # plain data, readable outside the suite
