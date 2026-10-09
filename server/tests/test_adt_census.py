"""The daily census from the bed movement ledger (adt-13a, W7-C).

The counting rules are tested on a scratch database holding only the tables
the census reads, with the suite's real DDL copied into it, so every figure
is exact and no other test's wards or patients reach it. The routes are
tested on the suite's own database, through the real admission and move
writers.

What this file defends (the increment's fixture list, then the routes):

  * a transfer across midnight is counted in the receiving ward;
  * a readmission inside the period counts both stays;
  * a bed out of service for 6 h reduces available beds by a quarter of a bed;
  * a temporary extra bed (and a bed of a class not counted) is not a
    licensed bed, though its patient is in the census;
  * ICU, ward, ICU within 40 h is a return to intensive care; 50 h is not;
    a move out under 48 h before now is pending;
  * a window straddling a ward's ledger start reads 'insufficient data' for
    the early days, never zero, and hospital totals need every ward;
  * a discharge recorded before ADT, with only its summary, ends the stay;
  * mixed legacy timestamp spellings compare correctly;
  * a move recorded in error and undone is not a transfer;
  * status 'transferred' from an import counts as transferred out, and deaths
    are listed;
  * occupancy is reported both ways (ADT-P4), and ALOS;
  * the window is whole past days, at most 92;
  * the routes: 404 while the module is off, adt.read only, a scoped caller
    sees its department's wards and no hospital totals, another ward is 404,
    a method on every answer, and the CSV.
"""
import csv
import io
import sqlite3
import uuid
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

import config
import main
from auth import create_token, hash_password
from database import db_session
from services import adt_census_service as census
from services import bed_master_service as bm

TZ = ZoneInfo("Asia/Kolkata")
D1, D2, D3 = date(2026, 9, 10), date(2026, 9, 11), date(2026, 9, 12)
NOW = datetime(2026, 9, 20, 6, 30, tzinfo=timezone.utc)
COMPLETE = datetime(2026, 9, 1, tzinfo=TZ)

_TABLES = ("wards", "housekeeping_units", "housekeeping_unit_events", "bed_classes",
           "admissions", "patient_movements", "discharge_summaries")


# ═════════════════════════════════════════════════════════════════════════════
#  The scratch database
# ═════════════════════════════════════════════════════════════════════════════

@pytest.fixture(scope="module")
def ddl() -> dict:
    with db_session() as db:
        rows = db.execute(
            f"SELECT name, sql FROM sqlite_master WHERE type = 'table' "
            f"AND name IN ({','.join('?' for _ in _TABLES)})", _TABLES).fetchall()
    found = {r["name"]: r["sql"] for r in rows}
    assert set(found) == set(_TABLES), f"missing tables: {set(_TABLES) - set(found)}"
    return found


@pytest.fixture()
def db(ddl):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    for sql in ddl.values():
        conn.execute(sql)
    yield conn
    conn.close()


def at(day: date, hh: int, mm: int = 0) -> datetime:
    """A hospital-local wall-clock time, as an aware UTC instant."""
    return datetime.combine(day, time(hh, mm), tzinfo=TZ).astimezone(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def add_ward(conn, code, *, ward_type="general", dept="General Medicine",
             complete_from=COMPLETE, created=datetime(2026, 1, 1, tzinfo=timezone.utc),
             active=1, name=None):
    wid = f"WRD-{code}"
    conn.execute(
        "INSERT INTO wards (id, code, name, department, ward_type, ledger_complete_from, "
        "is_active, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (wid, code, name or code, dept, ward_type,
         complete_from if isinstance(complete_from, str) or complete_from is None
         else iso(complete_from), active, iso(created)))
    return wid


def add_bed(conn, ward_id, label, *, temporary=0, bed_class=None,
            created=datetime(2026, 1, 1, tzinfo=timezone.utc), retired=None):
    bid = f"{ward_id}/{label}"
    conn.execute(
        "INSERT INTO housekeeping_units (id, unit_code, ward, bed_label, state, ward_id, "
        "is_temporary, bed_class, created_at, retired_at) VALUES (?, ?, ?, ?, 'vacant_ready', ?, ?, ?, ?, ?)",
        (bid, bid, ward_id, label, ward_id, temporary, bed_class, iso(created),
         iso(retired) if retired else None))
    return bid


def add_stay(conn, aid, *, start, end=None, status=None, end_type=None, ward_id=None,
             pid=None, created_at=None, admitted_at=True):
    status = status or ("active" if end is None else "discharged")
    conn.execute(
        "INSERT INTO admissions (id, patient_id, admitted_by, department, ward_id, status, "
        "created_at, admitted_at, discharged_at, discharge_type) "
        "VALUES (?, ?, 'D001', 'General Medicine', ?, ?, ?, ?, ?, ?)",
        (aid, pid or f"P-{aid}", ward_id, status,
         created_at if created_at is not None else iso(start),
         (iso(start) if admitted_at is True else admitted_at),
         iso(end) if isinstance(end, datetime) else end, end_type))
    return aid


def add_move(conn, aid, kind, when, *, from_ward=None, to_ward=None, corrects=None,
             mid=None, pid=None, moved_at=None):
    mid = mid or f"MOV-{uuid.uuid4().hex[:10]}"
    conn.execute(
        "INSERT INTO patient_movements (id, patient_id, admission_id, movement_type, reason, "
        "moved_at, from_ward_id, to_ward_id, corrects_movement_id, recorded_at) "
        "VALUES (?, ?, ?, ?, 'test', ?, ?, ?, ?, ?)",
        (mid, pid or f"P-{aid}", aid, kind, moved_at or iso(when), from_ward, to_ward,
         corrects, iso(when)))
    return mid


def add_event(conn, unit_id, from_state, to_state, when):
    conn.execute(
        "INSERT INTO housekeeping_unit_events (unit_id, from_state, to_state, occurred_at) "
        "VALUES (?, ?, ?, ?)", (unit_id, from_state, to_state, iso(when)))


def run(conn, first=D1, last=D3, *, wards=None, hospital=True, now=NOW):
    every = [dict(r) for r in conn.execute("SELECT * FROM wards ORDER BY code").fetchall()]
    chosen = every if wards is None else [w for w in every if w["code"] in wards]
    return census.census_on(conn, first_day=first, last_day=last, wards=chosen,
                            all_wards=every, hospital=hospital, tz=TZ, now=now)


def ward_out(result, code) -> dict:
    return next(w for w in result["wards"] if w["code"] == code)


def day(result, code, which: date) -> dict:
    return next(d for d in ward_out(result, code)["days"] if d["date"] == which.isoformat())


def hday(result, which: date) -> dict:
    return next(d for d in result["hospital"]["days"] if d["date"] == which.isoformat())


# ═════════════════════════════════════════════════════════════════════════════
#  The counting rules
# ═════════════════════════════════════════════════════════════════════════════

def test_a_transfer_across_midnight_is_counted_in_the_receiving_ward(db):
    a, b = add_ward(db, "A"), add_ward(db, "B")
    add_stay(db, "S1", start=at(D1, 10))
    add_move(db, "S1", "admission", at(D1, 10), to_ward=a)
    add_move(db, "S1", "transfer", at(D2, 23, 30), from_ward=a, to_ward=b)
    out = run(db)
    assert day(out, "A", D1)["midnight_census"] == 1
    assert day(out, "A", D1)["admissions"] == 1
    assert day(out, "A", D2)["midnight_census"] == 0
    assert day(out, "A", D2)["transfers_out"] == 1
    assert day(out, "B", D2)["midnight_census"] == 1
    assert day(out, "B", D2)["transfers_in"] == 1
    assert day(out, "B", D3)["midnight_census"] == 1
    assert day(out, "A", D2)["inpatient_days"] == round(23.5 / 24, 2)
    assert day(out, "B", D2)["inpatient_days"] == round(0.5 / 24, 2)
    # The hospital sees one patient and one transfer, never two.
    assert hday(out, D2)["midnight_census"] == 1
    assert hday(out, D2)["transfers_in"] == hday(out, D2)["transfers_out"] == 1


def test_a_readmission_inside_the_period_counts_both_stays(db):
    a = add_ward(db, "A")
    add_stay(db, "S1", pid="P1", start=at(D1, 8), end=at(D2, 10), end_type="routine")
    add_move(db, "S1", "admission", at(D1, 8), to_ward=a, pid="P1")
    add_move(db, "S1", "discharge", at(D2, 10), from_ward=a, pid="P1")
    add_stay(db, "S2", pid="P1", start=at(D3, 9))
    add_move(db, "S2", "admission", at(D3, 9), to_ward=a, pid="P1")
    out = run(db)
    totals = ward_out(out, "A")["totals"]
    assert totals["admissions"] == 2
    assert day(out, "A", D1)["admissions"] == 1 and day(out, "A", D3)["admissions"] == 1
    assert day(out, "A", D2)["ends"]["routine"] == 1
    assert [day(out, "A", d)["midnight_census"] for d in (D1, D2, D3)] == [1, 0, 1]
    assert totals["stays_ended"] == 1
    assert totals["alos_days"] == round(26 / 24, 2)


def test_a_bed_out_of_service_for_six_hours_reduces_available_beds(db):
    a = add_ward(db, "A")
    for label in ("1", "2", "3"):
        add_bed(db, a, label)
    add_event(db, f"{a}/2", "vacant_ready", "out_of_service", at(D2, 8))
    add_event(db, f"{a}/2", "out_of_service", "vacant_dirty", at(D2, 14))
    out = run(db)
    assert day(out, "A", D1)["licensed_beds"] == 3
    assert day(out, "A", D1)["available_beds"] == 3.0
    assert day(out, "A", D2)["licensed_beds"] == 3
    assert day(out, "A", D2)["available_beds"] == 2.75


def test_a_temporary_bed_is_not_licensed_but_its_patient_is_counted(db):
    a = add_ward(db, "A")
    add_bed(db, a, "1")
    add_bed(db, a, "2")
    add_bed(db, a, "1A", temporary=1)
    db.execute("INSERT INTO bed_classes (code, name, counts_in_census, created_at) "
               "VALUES ('DAYCARE', 'Day care recliner', 0, ?)", (iso(NOW),))
    add_bed(db, a, "R1", bed_class="DAYCARE")
    for i in range(3):
        add_stay(db, f"S{i}", start=at(D1, 9))
        add_move(db, f"S{i}", "admission", at(D1, 9), to_ward=a)
    out = run(db)
    d = day(out, "A", D1)
    assert d["licensed_beds"] == 2
    assert d["available_beds"] == 2.0
    assert d["midnight_census"] == 3
    # ADT-P4: both occupancy figures, and they may pass 100 with extra beds.
    assert d["occupancy"]["nabh"] == 150.0
    assert d["occupancy"]["licensed"] == 150.0


def test_icu_ward_icu_within_40_hours_is_a_return_and_50_hours_is_not(db):
    icu = add_ward(db, "ICU", ward_type="icu")
    ward = add_ward(db, "A")
    add_stay(db, "BACK", start=at(D1, 6))
    add_move(db, "BACK", "admission", at(D1, 6), to_ward=icu)
    add_move(db, "BACK", "transfer", at(D1, 20), from_ward=icu, to_ward=ward)
    add_move(db, "BACK", "transfer", at(D3, 12), from_ward=ward, to_ward=icu)   # 40 h
    add_stay(db, "LATE", start=at(D1, 7))
    add_move(db, "LATE", "admission", at(D1, 7), to_ward=icu)
    add_move(db, "LATE", "transfer", at(D1, 9), from_ward=icu, to_ward=ward)
    add_move(db, "LATE", "transfer", at(D3, 11), from_ward=ward, to_ward=icu)   # 50 h
    out = run(db)
    d1 = day(out, "ICU", D1)["icu"]
    assert d1 == {"transfers_out": 2, "returns_48h": 1, "pending": 0}
    totals = ward_out(out, "ICU")["totals"]["icu"]
    assert totals["return_rate"] == 50.0
    # A general ward carries no intensive care block.
    assert "icu" not in day(out, "A", D1)


def test_a_move_out_of_icu_under_48_hours_before_now_is_pending(db):
    icu = add_ward(db, "ICU", ward_type="icu")
    ward = add_ward(db, "A")
    add_stay(db, "S1", start=at(D1, 6))
    add_move(db, "S1", "admission", at(D1, 6), to_ward=icu)
    add_move(db, "S1", "transfer", at(D3, 22), from_ward=icu, to_ward=ward)
    out = run(db, now=at(D3, 23) + timedelta(hours=10))
    assert day(out, "ICU", D3)["icu"] == {"transfers_out": 1, "returns_48h": 0, "pending": 1}
    assert ward_out(out, "ICU")["totals"]["icu"]["return_rate"] is None


def test_a_window_straddling_the_ledger_start_reads_insufficient_data(db):
    a = add_ward(db, "A", complete_from=at(D2, 10))
    add_ward(db, "B")
    add_stay(db, "S1", start=at(D1, 9))
    add_move(db, "S1", "admission", at(D1, 9), to_ward=a)
    out = run(db)
    early = [day(out, "A", d) for d in (D1, D2)]
    for d in early:
        assert d["status"] == "insufficient_data"
        assert "midnight_census" not in d, "an insufficient day must never carry a count"
        assert "marked complete" in d["reason"]
    assert day(out, "A", D3)["status"] == "complete"
    assert day(out, "A", D3)["midnight_census"] == 1
    assert ward_out(out, "A")["first_complete_day"] == D3.isoformat()
    totals = ward_out(out, "A")["totals"]
    assert (totals["status"], totals["complete_days"], totals["insufficient_days"]) == ("partial", 1, 2)
    # The hospital needs every ward.
    assert hday(out, D1)["status"] == "insufficient_data"
    assert "A" in hday(out, D1)["reason"]
    assert hday(out, D3)["status"] == "complete"


def test_a_ledger_start_at_local_midnight_counts_that_day(db):
    add_ward(db, "A", complete_from=datetime.combine(D2, time.min, tzinfo=TZ))
    out = run(db)
    assert day(out, "A", D1)["status"] == "insufficient_data"
    assert day(out, "A", D2)["status"] == "complete"


def test_a_ward_never_marked_complete_is_never_counted(db):
    add_ward(db, "A", complete_from=None)
    out = run(db)
    assert all(d["status"] == "insufficient_data" for d in ward_out(out, "A")["days"])
    assert ward_out(out, "A")["totals"]["status"] == "insufficient_data"
    assert out["hospital"]["totals"]["status"] == "insufficient_data"


def test_a_discharge_before_adt_with_only_a_summary_ends_the_stay(db):
    a = add_ward(db, "A")
    add_stay(db, "OLD", pid="P-OLD", start=at(D1, 9), status="discharged", ward_id=a)
    db.execute("INSERT INTO discharge_summaries (patient_id, author_id, summary, discharged_at) "
               "VALUES ('P-OLD', 'D001', 'Home', ?)", (iso(at(D2, 15)),))
    out = run(db)
    assert day(out, "A", D1)["midnight_census"] == 1
    assert day(out, "A", D2)["midnight_census"] == 0
    assert day(out, "A", D2)["ends"]["not_recorded"] == 1
    assert day(out, "A", D3)["midnight_census"] == 0


def test_mixed_legacy_timestamp_spellings_compare_correctly(db):
    a = add_ward(db, "A", complete_from="2026-08-31T18:30:00Z")   # 1 Sep 00:00 IST
    b = add_ward(db, "B")
    # created_at as SQLite's CURRENT_TIMESTAMP writes it (naive UTC), no
    # admitted_at; a move with an IST offset; another with 'Z'.
    start = at(D1, 9)
    add_stay(db, "S1", start=start, admitted_at=None,
             created_at=start.strftime("%Y-%m-%d %H:%M:%S"))
    add_move(db, "S1", "admission", start, to_ward=a, moved_at=start.strftime("%Y-%m-%d %H:%M:%S"))
    move_at = at(D2, 23, 45)
    add_move(db, "S1", "transfer", move_at, from_ward=a, to_ward=b,
             moved_at=move_at.astimezone(TZ).isoformat())
    out = run(db)
    assert day(out, "A", D1)["admissions"] == 1
    assert day(out, "A", D1)["midnight_census"] == 1
    assert day(out, "B", D2)["midnight_census"] == 1
    assert day(out, "A", D2)["midnight_census"] == 0
    assert day(out, "A", D1)["status"] == "complete"


def test_a_move_recorded_in_error_and_undone_is_not_a_transfer(db):
    a, b = add_ward(db, "A"), add_ward(db, "B")
    add_stay(db, "S1", start=at(D1, 9))
    add_move(db, "S1", "admission", at(D1, 9), to_ward=a)
    wrong = add_move(db, "S1", "transfer", at(D2, 12), from_ward=a, to_ward=b)
    add_move(db, "S1", "correction", at(D2, 13), from_ward=b, to_ward=a, corrects=wrong)
    out = run(db)
    assert day(out, "A", D2)["transfers_out"] == 0
    assert day(out, "B", D2)["transfers_in"] == 0
    assert day(out, "A", D2)["midnight_census"] == 1
    assert day(out, "B", D2)["inpatient_days"] == 0.0


def test_ends_by_type_imports_transferred_and_deaths(db):
    a = add_ward(db, "A")
    add_stay(db, "DIED", start=at(D1, 9), end=at(D2, 3), end_type="death")
    add_move(db, "DIED", "admission", at(D1, 9), to_ward=a)
    add_stay(db, "MOVED", start=at(D1, 9), end=at(D2, 4), status="transferred")
    add_move(db, "MOVED", "admission", at(D1, 9), to_ward=a)
    add_stay(db, "LAMA", start=at(D1, 9), end=at(D3, 4), end_type="lama")
    add_move(db, "LAMA", "admission", at(D1, 9), to_ward=a)
    out = run(db)
    d2 = day(out, "A", D2)
    assert d2["ends"]["death"] == 1 and d2["deaths"] == 1
    assert d2["ends"]["transferred_out"] == 1
    assert day(out, "A", D3)["ends"]["lama"] == 1
    assert ward_out(out, "A")["totals"]["deaths"] == 1


def test_an_encounter_a_history_import_filed_is_left_out(db):
    a = add_ward(db, "A")
    add_stay(db, "IMP", start=at(D1, 9), ward_id=a)
    db.execute("UPDATE admissions SET import_run_id = 'RUN-1' WHERE id = 'IMP'")
    out = run(db)
    assert day(out, "A", D1)["midnight_census"] == 0
    assert day(out, "A", D1)["admissions"] == 0


def test_hospital_totals_add_patients_not_on_a_ward(db):
    a = add_ward(db, "A")
    add_ward(db, "OFF", active=0, complete_from=None)   # switched off, empty
    add_ward(db, "LATER", complete_from=None, created=at(D3, 12) + timedelta(days=2))
    add_bed(db, a, "1")
    add_stay(db, "PLACED", start=at(D1, 9))
    add_move(db, "PLACED", "admission", at(D1, 9), to_ward=a)
    add_stay(db, "WAITING", start=at(D1, 10))
    add_move(db, "WAITING", "admission", at(D1, 10))          # no ward yet
    out = run(db)
    h = hday(out, D1)
    assert h["status"] == "complete"
    assert h["midnight_census"] == 2
    assert h["not_on_a_ward"] == 1
    assert h["admissions"] == 2
    assert day(out, "A", D1)["midnight_census"] == 1
    assert h["licensed_beds"] == 1


def test_a_scoped_view_has_no_hospital_block(db):
    add_ward(db, "A")
    out = run(db, hospital=False)
    assert out["hospital"] is None


def test_every_answer_carries_the_methods(db):
    add_ward(db, "A")
    out = run(db)
    for key in ("midnight_census", "insufficient_data", "occupancy", "beds", "alos",
                "return_to_icu", "ends", "transfers", "inpatient_days", "days"):
        assert out["methods"][key]
    assert "available beds" in out["methods"]["occupancy"]


# ═════════════════════════════════════════════════════════════════════════════
#  The window and the CSV
# ═════════════════════════════════════════════════════════════════════════════

def test_the_window_is_whole_past_days_and_at_most_92():
    today = date(2026, 10, 9)
    assert census.parse_window(None, None, today=today) == (date(2026, 10, 2), date(2026, 10, 8))
    assert census.parse_window("2026-10-01", None, today=today) == (date(2026, 10, 1),
                                                                     date(2026, 10, 8))
    with pytest.raises(census.CensusError) as e:
        census.parse_window(None, "2026-10-09", today=today)
    assert e.value.code == "day_not_ended"
    with pytest.raises(census.CensusError) as e:
        census.parse_window("2026-10-05", "2026-10-01", today=today)
    assert e.value.code == "bad_window"
    with pytest.raises(census.CensusError) as e:
        census.parse_window("2026-01-01", "2026-10-01", today=today)
    assert e.value.code == "window_too_long"
    with pytest.raises(census.CensusError) as e:
        census.parse_window("10/01/2026", None, today=today)
    assert e.value.code == "bad_date"


def test_the_csv_has_a_row_per_ward_and_day_and_the_methods(db):
    add_ward(db, "A", name="=HYPERLINK(1)")
    add_ward(db, "B", complete_from=None)
    out = run(db)
    text = census.census_csv(out)
    rows = list(csv.reader(io.StringIO(text)))
    assert rows[0][:5] == ["level", "ward", "ward_name", "department", "date"]
    ward_rows = [r for r in rows[1:] if r and r[0] == "ward"]
    assert len(ward_rows) == 6
    hospital_rows = [r for r in rows[1:] if r and r[0] == "hospital"]
    assert len(hospital_rows) == 3
    # A typed name never runs as a formula in a spreadsheet.
    assert all(not r[2].startswith("=") for r in ward_rows)
    b_rows = [r for r in ward_rows if r[1] == "B"]
    assert all(r[5] == "insufficient data" and r[7] == "" for r in b_rows)
    assert any(r and r[0] == "occupancy" for r in rows), "the methods follow the table"


# ═════════════════════════════════════════════════════════════════════════════
#  The routes, on the suite's database
# ═════════════════════════════════════════════════════════════════════════════

client = TestClient(main.app)
DEPT = "General Medicine"
DOCTOR = {"id": "D001", "role": "doctor", "department": DEPT}
_SEEDED: list[str] = []


@pytest.fixture(autouse=True)
def _discharge_seeded():
    yield
    if not _SEEDED:
        return
    with db_session() as conn:
        conn.executemany("UPDATE patients SET status = 'discharged' WHERE id = ?",
                         [(p,) for p in _SEEDED])
        conn.executemany("UPDATE admissions SET status = 'discharged' "
                         "WHERE patient_id = ? AND status = 'active'", [(p,) for p in _SEEDED])
    _SEEDED.clear()


@pytest.fixture()
def adt_on(monkeypatch):
    monkeypatch.setattr(config, "ADT_ENABLED", True)
    yield


def _user(role: str, department: str | None = DEPT) -> dict:
    uid = f"CN{uuid.uuid4().hex[:8].upper()}"
    with db_session() as conn:
        conn.execute(
            "INSERT INTO users (id, name, role, department, password_hash, is_active) "
            "VALUES (?, ?, ?, ?, ?, 1)",
            (uid, f"Census {role}", role, department, hash_password("password")))
    return {"id": uid, "role": role, "department": department}


_ADMIN: dict | None = None


def _admin() -> dict:
    global _ADMIN
    if _ADMIN is None:
        _ADMIN = _user("admin", None)
    return _ADMIN


def _hdr(user: dict, key: str | None = None) -> dict:
    out = {"Authorization": f"Bearer {create_token(user['id'], user['role'])}"}
    if key:
        out["Idempotency-Key"] = key
    return out


def _route_ward(department: str = DEPT) -> dict:
    ward = bm.save_ward({"code": f"CN-{uuid.uuid4().hex[:6].upper()}", "department": department,
                         "cleaning_tracked": False}, actor=_admin())
    bm.register_beds({"ward_id": ward["id"], "labels": ["1", "2"]}, actor=_admin())
    return ward


def test_the_census_routes_answer_404_while_the_module_is_off(monkeypatch):
    monkeypatch.setattr(config, "ADT_ENABLED", False)
    for path in ("/api/adt/census", "/api/adt/census.csv"):
        resp = client.get(path, headers=_hdr(_admin()))
        assert resp.status_code == 404
        assert resp.json()["detail"]["error"] == "module_disabled"


def test_only_adt_read_holders_read_the_census(adt_on):
    for role in ("nurse", "pharmacist"):
        assert client.get("/api/adt/census", headers=_hdr(_user(role))).status_code == 403
    assert client.get("/api/adt/census", headers=_hdr(_user("ward_clerk"))).status_code == 200


def test_an_account_with_no_department_gets_every_ward_and_the_hospital(adt_on):
    ward = _route_ward()
    resp = client.get("/api/adt/census", headers=_hdr(_admin()))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["hospital"] is not None
    assert body["methods"]["midnight_census"]
    mine = next(w for w in body["wards"] if w["id"] == ward["id"])
    # Just set up and never marked complete: insufficient data, never zero.
    assert all(d["status"] == "insufficient_data" for d in mine["days"])
    assert len(mine["days"]) == 7


def test_a_scoped_caller_sees_its_department_and_no_hospital_totals(adt_on):
    other = f"Census Dept {uuid.uuid4().hex[:6]}"
    _user("doctor", other)   # a ward needs a department somebody clinical covers
    theirs = _route_ward(department=other)
    mine = _route_ward()
    resp = client.get("/api/adt/census", headers=_hdr(DOCTOR))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    ids = {w["id"] for w in body["wards"]}
    assert mine["id"] in ids and theirs["id"] not in ids
    assert body["hospital"] is None and body["hospital_hidden"]
    assert all((w["department"] or "") == DEPT for w in body["wards"])
    one = client.get("/api/adt/census", params={"ward_id": theirs["id"]}, headers=_hdr(DOCTOR))
    assert one.status_code == 404 and one.json()["detail"]["error"] == "ward_not_found"


def test_the_window_is_checked_by_the_route(adt_on):
    today = datetime.now(timezone.utc).astimezone(TZ).date()
    resp = client.get("/api/adt/census", params={"to": today.isoformat()}, headers=_hdr(_admin()))
    assert resp.status_code == 422 and resp.json()["detail"]["error"] in ("day_not_ended",)
    resp = client.get("/api/adt/census", params={"from": "yesterday"}, headers=_hdr(_admin()))
    assert resp.status_code == 422 and resp.json()["detail"]["error"] == "bad_date"


def test_the_csv_route_downloads_the_census(adt_on):
    _route_ward()
    resp = client.get("/api/adt/census.csv", headers=_hdr(_admin()))
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/csv")
    assert "attachment" in resp.headers["content-disposition"]
    rows = list(csv.reader(io.StringIO(resp.text)))
    assert rows[0][0] == "level"
    assert any(r and r[0] == "midnight_census" for r in rows)


def test_a_real_admission_and_bed_change_are_counted_from_the_ledger(adt_on):
    """End to end through the real writers: an admission back-timed two days,
    a bed change yesterday, the ward's ledger start set before both."""
    from services.hospital_time import hospital_timezone

    tz = hospital_timezone()
    today = datetime.now(timezone.utc).astimezone(tz).date()
    yesterday, before = today - timedelta(days=1), today - timedelta(days=2)
    ward = _route_ward()
    with db_session() as conn:
        conn.execute("UPDATE wards SET ledger_complete_from = ? WHERE id = ?",
                     (datetime.combine(before, time.min, tzinfo=tz).astimezone(timezone.utc)
                      .isoformat(), ward["id"]))
        beds = {r["bed_label"]: r["id"] for r in conn.execute(
            "SELECT id, bed_label FROM housekeeping_units WHERE ward_id = ?", (ward["id"],))}
    admitted = datetime.combine(before, time(10), tzinfo=tz)
    body = {"name": f"Census Patient {uuid.uuid4().hex[:4]}", "age": 70, "sex": "F",
            "admission_source": "er", "admission_type": "inpatient", "department": DEPT,
            "ward": ward["code"], "bed": "1", "ward_id": ward["id"], "bed_id": beds["1"],
            "attending_doctor_id": "D001", "chief_complaint": "Fall",
            "admitted_at": admitted.isoformat()}
    resp = client.post("/api/admissions/admit", headers=_hdr(DOCTOR, f"adm-{uuid.uuid4().hex}"),
                       json=body)
    assert resp.status_code == 201, resp.text
    _SEEDED.append(resp.json()["patient_id"])
    aid = resp.json()["admission_id"]
    moved = client.post(f"/api/adt/admissions/{aid}/bed-change",
                        headers=_hdr(DOCTOR, f"bc-{uuid.uuid4().hex}"),
                        json={"bed_id": beds["2"], "expected_bed_id": beds["1"],
                              "reason": "Quieter bed",
                              "moved_at": datetime.combine(yesterday, time(9), tzinfo=tz)
                              .isoformat()})
    assert moved.status_code == 200, moved.text
    resp = client.get("/api/adt/census", headers=_hdr(_admin()),
                      params={"from": before.isoformat(), "to": yesterday.isoformat(),
                              "ward_id": ward["id"]})
    assert resp.status_code == 200, resp.text
    days = {d["date"]: d for d in resp.json()["wards"][0]["days"]}
    assert days[before.isoformat()]["status"] == "complete"
    assert days[before.isoformat()]["admissions"] == 1
    assert days[before.isoformat()]["midnight_census"] == 1
    # A bed change inside one ward is not a transfer.
    assert days[yesterday.isoformat()]["transfers_in"] == 0
    assert days[yesterday.isoformat()]["midnight_census"] == 1
    # A ward filter shows no hospital block.
    assert resp.json()["hospital"] is None
