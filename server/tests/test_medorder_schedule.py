"""The times a medication order is due (medorders-19, W8-I; ruling MO-R8
(W117), a safety ruling held for Samuel).

Pinned here:
  * migration 71: med_order_schedule_times exists, is in the restore manifest
    and the erase plan, and a set time is superseded once and never rewritten;
  * the hospital's signed dose times (dose_times.json): no file means no site
    times (no built-in default); a file needs both signatures, frequencies
    given a fixed number of times a day no more often than every 2 hours,
    exactly that many distinct times at least 2 hours apart; anything else
    stops the server at import, naming the file; the shipped template is
    refused if copied unsigned;
  * when a dose is due on set times: an early dose covers its slot, one dose
    covers ONE set time (a dose given on time never hides the next one, at
    the 60-minute boundary too), a new order waits for its first set time,
    late slots roll forward and earlier ones count as not charted;
  * the board: with the lifecycle on, a linked TDS drug shows the hospital's
    three times; with no file it falls back to the derived schedule; with the
    lifecycle off, or for an unlinked row, nothing changes; the prescriber's
    times win over the hospital's; overdue is worked out from set times;
  * a changed frequency: the pharmacist's correction at verification (TDS to
    Q8H, the same doses a day) supersedes the times set for TDS in the same
    transaction, with an 'amended' event and its audit row, and the board
    falls back to the hospital's Q8H times or the derived schedule; a
    correction that keeps the frequency keeps them; every writer of an order's
    frequency calls the same supersede (a scan of the source);
  * PUT /orders/{id}/schedule: prescriber only, behind the lifecycle, scoped
    to the department, versioned, with an 'amended' event and its audit row;
    the refusals; GET /patient/{id}/schedules and GET /dose-times.
"""
from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

import config
from auth import create_token
from database import db_session
from main import app
from services import nurse_shift_service
from services import pharmacy_verification_service as pv
from services.medorders import adoption, chart_lane, core
from services.medorders import schedule as med_schedule

SERVER_DIR = Path(__file__).resolve().parent.parent
REPO_DIR = SERVER_DIR.parent
DEPT = "W8I Schedule Ward"
OTHER_DEPT = "W8I Other Ward"
IST = ZoneInfo("Asia/Kolkata")
_PATIENTS: list[str] = []


@pytest.fixture(scope="module", autouse=True)
def _cleanup():
    yield
    if not _PATIENTS:
        return
    marks = ", ".join("?" for _ in _PATIENTS)
    with db_session() as db:
        db.execute(f"DELETE FROM med_order_schedule_times WHERE patient_id IN ({marks})",
                   _PATIENTS)
        db.execute(f"DELETE FROM med_order_events WHERE patient_id IN ({marks})", _PATIENTS)
        db.execute(f"DELETE FROM med_orders WHERE patient_id IN ({marks})", _PATIENTS)
        db.execute(f"UPDATE medications SET med_order_id = NULL WHERE patient_id IN ({marks})",
                   _PATIENTS)
        db.execute(f"UPDATE patients SET status = 'discharged' WHERE id IN ({marks})", _PATIENTS)
        db.execute(f"UPDATE admissions SET status = 'discharged' WHERE patient_id IN ({marks})",
                   _PATIENTS)


# ── Helpers ───────────────────────────────────────────────────────────────────

SIGNED = {"pharmacy": {"name": "A. Pharmacist", "date": "2026-10-01"},
          "physician_champion": {"name": "B. Physician", "date": "2026-10-02"}}


def _write_times(tmp_path, times=None, *, signed=None, extra=None, name="dose_times.json"):
    body = {"version": "2026-10-test",
            "signed": SIGNED if signed is None else signed,
            "times": {"TDS": ["08:00", "14:00", "20:00"], "BD": ["09:00", "21:00"]}
            if times is None else times}
    body.update(extra or {})
    path = tmp_path / name
    path.write_text(json.dumps(body))
    return path


def _refused(path, words):
    with pytest.raises(med_schedule.DoseTimesConfigError) as exc:
        med_schedule.load_dose_times(path)
    assert words in str(exc.value), str(exc.value)
    assert str(path) in str(exc.value)
    return exc.value


def _user(role, department=None):
    uid = f"W8IS{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute("INSERT INTO users (id, name, role, password_hash, department) "
                   "VALUES (?, ?, ?, 'x', ?)", (uid, f"Schedule {role}", role, department))
    return uid


def _hdr(uid, role):
    return {"Authorization": f"Bearer {create_token(uid, role)}"}


def _patient(dept=DEPT):
    pid = f"PW8IS{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute("INSERT INTO patients (id, name, mrn, bed, ward, status) "
                   "VALUES (?, ?, ?, 'S-1', 'SW', 'admitted')",
                   (pid, "Set Times Test", f"MRN-{uuid.uuid4().hex[:8]}"))
        db.execute("INSERT OR IGNORE INTO users (id, name, role, password_hash) "
                   "VALUES ('W8ISADM', 'Schedule Admitter', 'doctor', 'x')")
        db.execute("INSERT INTO admissions (patient_id, department, status, admitted_by) "
                   "VALUES (?, ?, 'active', 'W8ISADM')", (pid, dept))
    _PATIENTS.append(pid)
    return pid


def _chart(pid, *, frequency="TDS", status="active", started_at=None):
    with db_session() as db:
        return db.execute(
            "INSERT INTO medications (patient_id, drug_name, dosage, route, frequency, status, "
            "prescribed_by, source, started_at) "
            "VALUES (?, 'Amoxicillin', '500 mg', 'Oral', ?, ?, NULL, 'clinician', "
            "COALESCE(?, CURRENT_TIMESTAMP))",
            (pid, frequency, status, started_at)).lastrowid


def _link(pid, med_id):
    adoption.adopt_legacy(trigger="script", patient_ids=[pid])
    with db_session() as db:
        order_id = db.execute("SELECT med_order_id FROM medications WHERE id = ?",
                              (med_id,)).fetchone()[0]
    assert order_id
    return order_id


def _order(order_id):
    with db_session() as db:
        return dict(db.execute("SELECT * FROM med_orders WHERE id = ?", (order_id,)).fetchone())


def _rows(order_id):
    with db_session() as db:
        return [dict(r) for r in db.execute(
            "SELECT * FROM med_order_schedule_times WHERE med_order_id = ? ORDER BY id",
            (order_id,)).fetchall()]


def _audits(action, target_id):
    with db_session() as db:
        return [dict(r) for r in db.execute(
            "SELECT * FROM audit_logs WHERE action_type = ? AND target_id = ? ORDER BY rowid",
            (action, target_id)).fetchall()]


@pytest.fixture()
def store(monkeypatch):
    monkeypatch.setattr(config, "MED_ORDER_STORE_ENABLED", True)
    return monkeypatch


@pytest.fixture()
def lifecycle(store):
    store.setattr(config, "MED_ORDER_LIFECYCLE_ENABLED", True)
    return store


@pytest.fixture()
def site(tmp_path, monkeypatch):
    """The hospital's signed times: TDS 08:00, 14:00, 20:00 and BD 09:00, 21:00."""
    loaded = med_schedule.load_dose_times(_write_times(tmp_path))
    monkeypatch.setattr(med_schedule, "_SITE", loaded)
    return loaded


@pytest.fixture()
def no_site(monkeypatch):
    monkeypatch.setattr(med_schedule, "_SITE", None)


@pytest.fixture()
def ist(monkeypatch):
    from services import hospital_time
    monkeypatch.setattr(hospital_time, "hospital_timezone", lambda: IST)
    return IST


def _at(day_offset, hh, mm, tz=IST):
    """An aware UTC instant at hh:mm hospital time, `day_offset` days from a
    fixed day."""
    local = datetime(2026, 10, 10, hh, mm, tzinfo=tz) + timedelta(days=day_offset)
    return local.astimezone(timezone.utc)


# ── 1. Migration 71 ───────────────────────────────────────────────────────────

def test_migration_71_is_collected_and_the_table_exists():
    import migrations_his
    assert {m.version: m.name for m in migrations_his.collect()}[71] == "med_order_schedule_times"
    with db_session() as db:
        cols = {r[1] for r in db.execute("PRAGMA table_info(med_order_schedule_times)")}
        names = {r[0] for r in db.execute(
            "SELECT name FROM sqlite_master WHERE tbl_name = 'med_order_schedule_times'")}
    assert {"med_order_id", "patient_id", "schedule_version", "due_time", "basis",
            "superseded_at", "superseded_by", "authored_by", "authored_at"} <= cols
    assert "trg_med_order_schedule_times_written_once" in names


def test_the_table_is_in_the_restore_manifest_and_the_erase_plan():
    sys.path.insert(0, str(REPO_DIR / "scripts" / "lib"))
    import verify_restore
    assert verify_restore.MODULE_TABLES["medication order times"] == ("med_order_schedule_times",)
    from routes.admin_routes import _build_erase_plan
    with db_session() as db:
        assert "med_order_schedule_times" in {t for t, _, _ in _build_erase_plan(db)}


def test_a_set_time_is_superseded_once_and_never_rewritten():
    oid = f"MO-W8IS-{uuid.uuid4().hex[:8]}"
    with db_session() as db:
        db.execute("INSERT INTO med_order_schedule_times (med_order_id, patient_id, "
                   "schedule_version, due_time, basis, authored_by, authored_at, created_at) "
                   "VALUES (?, 'PNONE', 1, '08:00', 'prescriber', 'D1', 't', 't')", (oid,))
    try:
        with pytest.raises(Exception, match="never rewritten"):
            with db_session() as db:
                db.execute("UPDATE med_order_schedule_times SET due_time = '09:00' "
                           "WHERE med_order_id = ?", (oid,))
        with db_session() as db:
            db.execute("UPDATE med_order_schedule_times SET superseded_at = 't2', "
                       "superseded_by = 'D2' WHERE med_order_id = ?", (oid,))
        with pytest.raises(Exception, match="never rewritten"):
            with db_session() as db:
                db.execute("UPDATE med_order_schedule_times SET superseded_by = 'D3' "
                           "WHERE med_order_id = ?", (oid,))
    finally:
        with db_session() as db:
            db.execute("DELETE FROM med_order_schedule_times WHERE med_order_id = ?", (oid,))


# ── 2. The signed file ────────────────────────────────────────────────────────

def test_no_file_means_no_site_times_and_none_ship(tmp_path):
    assert med_schedule.load_dose_times(tmp_path / "absent.json") is None
    assert not (SERVER_DIR / "site_config" / "dose_times.json").exists(), \
        "Orb ships no dose times (ruling MO-R8 (a)): only the template"


def test_a_signed_file_loads(tmp_path):
    path = _write_times(tmp_path, times={"tds": ["20:00", "08:00", "14:00"],
                                         "Q6H": ["00:00", "06:00", "12:00", "18:00"]})
    site = med_schedule.load_dose_times(path)
    assert site.times["tds"] == ("08:00", "14:00", "20:00")
    assert site.times["q6h"] == ("00:00", "06:00", "12:00", "18:00")
    assert site.signed["pharmacy"]["name"] == "A. Pharmacist"
    assert site.fingerprint.startswith("sha256:") and site.version == "2026-10-test"
    assert med_schedule.site_times_for("  TDS ", site) == ("08:00", "14:00", "20:00")
    # Matched by words, never by interval: every 8 hours is not TDS.
    assert med_schedule.site_times_for("Q8H", site) is None


def test_the_shipped_template_is_refused_if_copied_unsigned(tmp_path):
    template = SERVER_DIR / "site_config" / "dose_times.example.json"
    copy = tmp_path / "dose_times.json"
    copy.write_bytes(template.read_bytes())
    _refused(copy, "is blank")


@pytest.mark.parametrize("change,words", [
    ({"extra": {"signd": {}}}, "Unknown key 'signd'"),
    ({"signed": {"pharmacy": SIGNED["pharmacy"]}}, "'physician_champion' is missing"),
    ({"signed": {**SIGNED, "pharmacy": {"name": " ", "date": "2026-10-01"}}}, "is blank"),
    ({"signed": {**SIGNED, "pharmacy": {"name": "A", "date": "01/10/2026"}}},
     "not a date written YYYY-MM-DD"),
    ({"signed": {**SIGNED, "pharmacy": {"name": "A", "date": "2999-01-01"}}}, "in the future"),
    ({"times": {}}, "at least one frequency"),
    ({"times": {"TDS": ["08:00", "14:00"]}}, "means 3 doses a day"),
    ({"times": {"TDS": ["08:00", "08:00", "20:00"]}}, "same time is given twice"),
    ({"times": {"TDS": ["08:00", "08:30", "20:00"]}}, "less than 2 hours apart"),
    ({"times": {"BD": ["23:30", "00:10"]}}, "less than 2 hours apart"),
    # One hour apart is refused: a dose given on time would cover the next time.
    ({"times": {"BD": ["08:00", "09:00"]}}, "less than 2 hours apart"),
    ({"times": {"BD": ["08:00", "09:59"]}}, "less than 2 hours apart"),
    ({"times": {"Q1H": [f"{h:02d}:00" for h in range(24)]}},
     "no more often than every 2 hours"),
    ({"times": {"Q90min": [f"{(m // 60):02d}:{(m % 60):02d}" for m in range(0, 1440, 90)]}},
     "no more often than every 2 hours"),
    ({"times": {"OD": ["24:00"]}}, "is not a time"),
    ({"times": {"OD": ["8:00"]}}, "is not a time"),
    ({"times": {"PRN": ["08:00"]}}, "fixed number of times every day"),
    ({"times": {"alternate days": ["08:00"]}}, "fixed number of times every day"),
    ({"times": {"TDS": ["08:00", "14:00", "20:00"], "tds": ["06:00", "14:00", "22:00"]}},
     "listed twice"),
])
def test_a_malformed_file_is_refused_naming_the_problem(tmp_path, change, words):
    path = _write_times(tmp_path, times=change.get("times"), signed=change.get("signed"),
                        extra=change.get("extra"))
    _refused(path, words)


def test_a_file_that_is_not_json_is_refused(tmp_path):
    path = tmp_path / "dose_times.json"
    path.write_text("{ not json")
    _refused(path, "not valid JSON")


def _import_schedule(env: dict) -> subprocess.CompletedProcess:
    base = {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", ""),
            "ORB_DEPLOYMENT_PROFILE": "dev",
            "ORB_JWT_SECRET": "probe-only-secret-0123456789abcdef"}
    return subprocess.run(
        [sys.executable, "-c",
         "from services.medorders import schedule as s; "
         "print('SITE:' + str(None if s.site_dose_times() is None else sorted(s.site_dose_times().times)))"],
        cwd=str(SERVER_DIR), env={**base, **env}, capture_output=True, text=True, timeout=180)


def test_a_malformed_file_stops_the_server_at_import(tmp_path):
    bad = _write_times(tmp_path, times={"TDS": ["08:00"]})
    proc = _import_schedule({med_schedule.CONFIG_PATH_ENV: str(bad)})
    assert proc.returncode != 0
    assert "Orb will not start: the hospital's dose times file is not valid" in proc.stderr
    good = _write_times(tmp_path, name="good.json")
    proc = _import_schedule({med_schedule.CONFIG_PATH_ENV: str(good)})
    assert proc.returncode == 0, proc.stderr
    assert "SITE:['bd', 'tds']" in proc.stdout


# ── 3. When a dose is due on set times ────────────────────────────────────────

TDS = ("08:00", "14:00", "20:00")


def test_doses_per_day_reads_only_daily_frequencies():
    assert med_schedule.doses_per_day("TDS") == 3
    assert med_schedule.doses_per_day("bd") == 2
    assert med_schedule.doses_per_day("Q6H") == 4
    assert med_schedule.doses_per_day("OD") == 1
    assert med_schedule.doses_per_day("Q2H") == 12
    assert med_schedule.doses_per_day("Q3H") == 8
    for freq in ("PRN", "STAT", "continuous infusion", "alternate days", "weekly", "", "Q5H"):
        assert med_schedule.doses_per_day(freq) is None, freq


@pytest.mark.parametrize("freq", ["Q1H", "1 hourly", "Q30min", "Q90min"])
def test_a_frequency_more_often_than_every_two_hours_has_no_set_times(freq):
    # Its set times would sit inside one another's early window.
    assert med_schedule.doses_per_day(freq) is None


def test_the_gap_between_set_times_is_more_than_the_early_window():
    assert med_schedule.MIN_GAP_MINUTES > med_schedule.EARLY_WINDOW_MINUTES
    assert med_schedule.MIN_GAP_MINUTES >= 2 * med_schedule.EARLY_WINDOW_MINUTES


def test_set_times_exactly_two_hours_apart_are_accepted():
    every_two = [f"{h:02d}:00" for h in range(0, 24, 2)]
    assert med_schedule.clean_times(every_two, expected=12) == tuple(every_two)
    assert med_schedule.clean_times(["08:00", "10:00", "20:00"], expected=3) == \
        ("08:00", "10:00", "20:00")


@pytest.mark.parametrize("times", [["08:00", "09:00"], ["08:00", "09:59"], ["23:30", "01:29"]])
def test_set_times_an_hour_apart_are_refused(times):
    with pytest.raises(ValueError, match="less than 2 hours apart"):
        med_schedule.clean_times(times, expected=2)


def test_a_dose_given_on_time_does_not_hide_the_next_set_time():
    # The 60-minute boundary: with times one hour apart (refused now when a
    # set is written or a file is read; the due rule copes with them anyway),
    # a dose given at 08:00 counts for 08:00 only, so 09:00 stays due and shows
    # as late once it passes.
    due = med_schedule.scheduled_due(("08:00", "09:00"), last_given_at=_at(0, 8, 0),
                                     started_at=None, now=_at(0, 9, 30), tz=IST)
    assert due["due_at"] == _at(0, 9, 0) and due["missed_slots"] == 0
    hourly = tuple(f"{h:02d}:00" for h in range(24))
    due = med_schedule.scheduled_due(hourly, last_given_at=_at(0, 8, 0), started_at=None,
                                     now=_at(0, 8, 10), tz=IST)
    assert due["due_at"] == _at(0, 9, 0)
    # Nothing charted since 08:00: at 10:30 the 10:00 dose is due and 09:00
    # counts as not charted, rather than vanishing.
    due = med_schedule.scheduled_due(hourly, last_given_at=_at(0, 8, 0), started_at=None,
                                     now=_at(0, 10, 30), tz=IST)
    assert due["due_at"] == _at(0, 10, 0) and due["missed_slots"] == 1


def test_one_dose_covers_one_set_time_at_the_two_hour_boundary():
    every_two = tuple(f"{h:02d}:00" for h in range(0, 24, 2))
    # Given at 09:00, exactly an hour after 08:00 and an hour before 10:00: a
    # tie counts for 08:00, so 10:00 stays due.
    due = med_schedule.scheduled_due(every_two, last_given_at=_at(0, 9, 0), started_at=None,
                                     now=_at(0, 9, 5), tz=IST)
    assert due["due_at"] == _at(0, 10, 0)
    # Given on time at 08:00: 10:00 is due at 10:00.
    due = med_schedule.scheduled_due(every_two, last_given_at=_at(0, 8, 0), started_at=None,
                                     now=_at(0, 10, 20), tz=IST)
    assert due["due_at"] == _at(0, 10, 0) and due["missed_slots"] == 0
    # Nearer 10:00 and inside its early window: it counts for 10:00 (no second
    # dose shown due ten minutes later), and 12:00 is next.
    due = med_schedule.scheduled_due(every_two, last_given_at=_at(0, 9, 10), started_at=None,
                                     now=_at(0, 9, 15), tz=IST)
    assert due["due_at"] == _at(0, 12, 0)


def test_an_early_dose_covers_its_slot():
    due = med_schedule.scheduled_due(TDS, last_given_at=_at(0, 7, 50), started_at=None,
                                     now=_at(0, 9, 0), tz=IST)
    assert due["due_at"] == _at(0, 14, 0) and due["missed_slots"] == 0
    assert due["due_basis"] == "last_given"


def test_a_dose_more_than_an_hour_early_does_not():
    due = med_schedule.scheduled_due(TDS, last_given_at=_at(0, 6, 50), started_at=None,
                                     now=_at(0, 7, 30), tz=IST)
    assert due["due_at"] == _at(0, 8, 0)


def test_a_new_order_waits_for_its_first_set_time():
    due = med_schedule.scheduled_due(TDS, last_given_at=None, started_at=_at(0, 15, 0),
                                     now=_at(0, 15, 5), tz=IST)
    assert due["due_at"] == _at(0, 20, 0) and due["due_basis"] == "order_start"


def test_late_slots_roll_forward_and_count_as_not_charted():
    due = med_schedule.scheduled_due(TDS, last_given_at=_at(-1, 20, 5), started_at=None,
                                     now=_at(0, 14, 30), tz=IST)
    assert due["due_at"] == _at(0, 14, 0)
    assert due["missed_slots"] == 1  # 08:00 today was never charted


def test_nothing_to_anchor_on_means_no_due_time():
    due = med_schedule.scheduled_due(TDS, last_given_at=None, started_at=None,
                                     now=_at(0, 9, 0), tz=IST)
    assert due["due_at"] is None


# ── 4. The board ──────────────────────────────────────────────────────────────

def _task(pid, med_id):
    board = nurse_shift_service.build_board(DEPT)
    (card,) = [c for c in board["patients"] if c["patient_id"] == pid]
    (task,) = [t for t in card["medications"] if t["medication_id"] == med_id]
    return task


@pytest.fixture()
def clock(monkeypatch, ist):
    now = _at(0, 9, 0)
    monkeypatch.setattr(nurse_shift_service, "_now", lambda: now)
    return now


def _started(at: datetime) -> str:
    return at.strftime("%Y-%m-%d %H:%M:%S")


def test_a_tds_drug_shows_the_hospitals_three_times(lifecycle, site, clock):
    pid = _patient()
    med_id = _chart(pid, started_at=_started(_at(-1, 21, 0)))
    _link(pid, med_id)
    task = _task(pid, med_id)
    assert task["due_source"] == "site_times"
    assert task["set_times"] == ["08:00", "14:00", "20:00"]
    # Started last night, nothing given: 08:00 today is the slot due, an hour late.
    assert task["due_at"] == _at(0, 8, 0).isoformat()
    assert task["overdue"] is True and task["missed_slots"] == 0


def test_with_no_file_the_board_falls_back(lifecycle, no_site, clock):
    pid = _patient()
    med_id = _chart(pid, started_at=_started(_at(-1, 21, 0)))
    _link(pid, med_id)
    task = _task(pid, med_id)
    assert "due_source" not in task and "set_times" not in task
    assert task["due_basis"] == "order_start"
    assert task["due_at"] == (_at(-1, 21, 0) + timedelta(hours=8)).isoformat()


def test_with_the_lifecycle_off_nothing_changes(store, site, clock):
    pid = _patient()
    med_id = _chart(pid, started_at=_started(_at(-1, 21, 0)))
    _link(pid, med_id)
    task = _task(pid, med_id)
    assert "due_source" not in task
    assert task["due_at"] == (_at(-1, 21, 0) + timedelta(hours=8)).isoformat()


def test_an_unlinked_row_keeps_the_derived_time(lifecycle, site, clock):
    pid = _patient()
    med_id = _chart(pid, started_at=_started(_at(-1, 21, 0)))
    assert "due_source" not in _task(pid, med_id)


def test_the_prescribers_times_win_and_overdue_comes_from_them(lifecycle, site, clock):
    pid = _patient()
    med_id = _chart(pid, started_at=_started(_at(-1, 21, 0)))
    order_id = _link(pid, med_id)
    with db_session(immediate=True) as conn:
        med_schedule.set_schedule_in_txn(conn, order_id, ["06:00", "14:00", "22:00"],
                                         actor_id="W8ISDOC", actor_role="doctor")
    task = _task(pid, med_id)
    assert task["due_source"] == "prescriber"
    assert task["set_times"] == ["06:00", "14:00", "22:00"]
    # 22:00 last night and 06:00 today both passed with nothing given: 06:00
    # is the slot due (three hours late) and 22:00 was never charted.
    assert task["due_at"] == _at(0, 6, 0).isoformat()
    assert task["overdue"] is True and task["missed_slots"] == 1


def test_times_for_another_frequency_are_never_used(lifecycle, site, clock):
    pid = _patient()
    med_id = _chart(pid, frequency="Q8H", started_at=_started(_at(-1, 21, 0)))
    _link(pid, med_id)
    assert "due_source" not in _task(pid, med_id)  # the file names TDS, not Q8H


# ── 4b. When the order's frequency changes ────────────────────────────────────

def _ai_chart(pid, *, frequency="TDS", started_at=None):
    """A Sage-charted drug as the Sage lane writes it: llm_extracted, the
    placeholder dose and route, unverified."""
    with db_session() as db:
        return db.execute(
            "INSERT INTO medications (patient_id, drug_name, dosage, route, frequency, status, "
            "prescribed_by, source, started_at) VALUES (?, 'Amoxicillin', 'REVIEW REQUIRED', "
            "'Review', ?, 'active', NULL, 'llm_extracted', COALESCE(?, CURRENT_TIMESTAMP))",
            (pid, frequency, started_at)).lastrowid


def _events(order_id, kind=None):
    sql, args = "SELECT * FROM med_order_events WHERE med_order_id = ?", [order_id]
    if kind:
        sql += " AND kind = ?"
        args.append(kind)
    with db_session() as db:
        return [dict(r) for r in db.execute(sql + " ORDER BY id", args).fetchall()]


def _verify_with(med_id, pharmacist, frequency):
    pv.verify_medication(med_id, pharmacist, dosage="500 mg", route="Oral", frequency=frequency)


def _set_times(order_id, times, actor="W8ISDOC"):
    with db_session(immediate=True) as conn:
        med_schedule.set_schedule_in_txn(conn, order_id, times, actor_id=actor,
                                         actor_role="doctor")


def _in_force(order_id):
    return [r["due_time"] for r in _rows(order_id) if r["superseded_at"] is None]


def test_a_frequency_correction_at_verification_supersedes_the_set_times(
        lifecycle, site, clock):
    """The reviewer's case: TDS times set by the prescriber, then the
    pharmacist corrects the frequency to Q8H while verifying. A strict
    8-hourly drug must never run on times written for TDS."""
    pid = _patient()
    med_id = _ai_chart(pid, started_at=_started(_at(-1, 21, 0)))
    order_id = _link(pid, med_id)
    _set_times(order_id, ["07:00", "13:00", "19:00"])
    assert _task(pid, med_id)["set_times"] == ["07:00", "13:00", "19:00"]
    pharmacist = _user("pharmacist", DEPT)

    _verify_with(med_id, pharmacist, "Q8H")

    order = _order(order_id)
    assert order["frequency"] == "Q8H" and order["status"] == "verified"
    rows = _rows(order_id)
    assert rows and all(r["superseded_at"] for r in rows)
    assert {r["superseded_by"] for r in rows} == {pharmacist}
    with db_session() as db:
        assert med_schedule.current_schedule_in_txn(db, order_id) is None
    # One 'amended' event for the correction, one for the times it ended.
    amended = _events(order_id, "amended")
    superseded = [e for e in amended
                  if json.loads(e["payload_json"]).get("superseded_because")]
    (event,) = superseded
    payload = json.loads(event["payload_json"])
    assert payload["superseded_because"] == med_schedule.FREQUENCY_CHANGED
    assert payload["changes"] == {"schedule_times": {"before": ["07:00", "13:00", "19:00"],
                                                     "after": None}}
    assert payload["frequency"] == {"before": "TDS", "after": "Q8H"}
    assert event["entry_point"] == chart_lane.ENTRY_LEGACY_VERIFY
    assert event["actor_id"] == pharmacist and event["actor_role"] == "pharmacist"
    assert [e["kind"] for e in _events(order_id)][-1] == "verified"
    audits = [json.loads(a["metadata_json"]) for a in _audits("med_order_amended", order_id)]
    (audit,) = [m for m in audits if m.get("superseded_because")]
    assert audit["amended_fields"] == ["schedule_times"]
    assert audit["lane"] == med_schedule.LANE_SCHEDULE
    assert audit["schedule_times_before"] == ["07:00", "13:00", "19:00"]
    assert audit["schedule_times_after"] is None
    assert audit["medication_id"] == str(med_id)

    # The board: no set times for Q8H (the file names TDS and BD only), so the
    # derived 8-hourly schedule, anchored on the order's start.
    task = _task(pid, med_id)
    assert task["frequency"] == "Q8H"
    assert "due_source" not in task and "set_times" not in task
    assert task["due_basis"] == "order_start"
    assert task["due_at"] == (_at(-1, 21, 0) + timedelta(hours=8)).isoformat()
    # The prescriber's screen shows no set times either.
    doctor = _user("doctor", DEPT)
    body = TestClient(app).get(f"/api/medorders/patient/{pid}/schedules",
                               headers=_hdr(doctor, "doctor")).json()
    (entry,) = [o for o in body["orders"] if o["med_order_id"] == order_id]
    assert entry["frequency"] == "Q8H" and entry["schedule"] is None


def test_after_a_frequency_change_the_hospitals_times_for_the_new_one_apply(
        lifecycle, clock, tmp_path, monkeypatch):
    path = _write_times(tmp_path, {"TDS": ["08:00", "14:00", "20:00"],
                                   "Q8H": ["06:00", "14:00", "22:00"]})
    monkeypatch.setattr(med_schedule, "_SITE", med_schedule.load_dose_times(path))
    pid = _patient()
    med_id = _ai_chart(pid, started_at=_started(_at(-1, 21, 0)))
    order_id = _link(pid, med_id)
    _set_times(order_id, ["07:00", "13:00", "19:00"])
    _verify_with(med_id, _user("pharmacist", DEPT), "Q8H")
    task = _task(pid, med_id)
    assert task["due_source"] == "site_times"
    assert task["set_times"] == ["06:00", "14:00", "22:00"]
    assert _in_force(order_id) == []


def test_a_correction_that_keeps_the_frequency_keeps_the_set_times(lifecycle, site, clock):
    pid = _patient()
    med_id = _ai_chart(pid, started_at=_started(_at(-1, 21, 0)))
    order_id = _link(pid, med_id)
    _set_times(order_id, ["07:00", "13:00", "19:00"])
    _verify_with(med_id, _user("pharmacist", DEPT), "TDS")  # dose and route corrected only
    assert _in_force(order_id) == ["07:00", "13:00", "19:00"]
    assert not [e for e in _events(order_id, "amended")
                if json.loads(e["payload_json"]).get("superseded_because")]
    task = _task(pid, med_id)
    assert task["due_source"] == "prescriber"
    assert task["set_times"] == ["07:00", "13:00", "19:00"]


def test_with_no_set_times_a_frequency_correction_writes_nothing_more(store, site, clock):
    """Flags as shipped (the lifecycle off): no set times exist, so the
    correction writes exactly what it wrote before this change."""
    pid = _patient()
    med_id = _ai_chart(pid)
    order_id = _link(pid, med_id)
    _verify_with(med_id, _user("pharmacist", DEPT), "Q8H")
    assert _order(order_id)["frequency"] == "Q8H"
    assert [e["kind"] for e in _events(order_id)] == ["adopted", "amended", "verified"]
    (audit,) = _audits("med_order_amended", order_id)
    assert json.loads(audit["metadata_json"])["amended_fields"] == ["dose", "frequency", "route"]
    assert _rows(order_id) == []


def test_times_left_from_a_lifecycle_period_are_superseded_with_the_lifecycle_off(
        lifecycle, site, clock):
    """Strict: times set while the lifecycle was on never come back for a
    different frequency if it is switched off and on again."""
    pid = _patient()
    med_id = _ai_chart(pid)
    order_id = _link(pid, med_id)
    _set_times(order_id, ["07:00", "13:00", "19:00"])
    lifecycle.setattr(config, "MED_ORDER_LIFECYCLE_ENABLED", False)
    _verify_with(med_id, _user("pharmacist", DEPT), "Q8H")
    assert _in_force(order_id) == []


def test_the_supersede_helper_on_its_own(lifecycle, site):
    pid = _patient()
    med_id = _chart(pid)
    order_id = _link(pid, med_id)
    _set_times(order_id, ["07:00", "13:00", "19:00"])
    with db_session() as db:
        order = dict(db.execute("SELECT * FROM med_orders WHERE id = ?",
                                (order_id,)).fetchone())
    # Outside a write transaction: refused.
    with db_session() as db:
        with pytest.raises(core.StoreUsageError):
            med_schedule.supersede_for_frequency_change_in_txn(
                db, order, frequency_before="Q8H", actor_id="W8ISPHA", actor_role="pharmacist",
                entry_point="test.frequency")
    # The same frequency written another way is not a change.
    with db_session(immediate=True) as conn:
        assert med_schedule.supersede_for_frequency_change_in_txn(
            conn, order, frequency_before="  tds ", actor_id="W8ISPHA",
            actor_role="pharmacist", entry_point="test.frequency") is None
    assert _in_force(order_id) == ["07:00", "13:00", "19:00"]
    # A change with times in force names the person who made it.
    with db_session(immediate=True) as conn:
        with pytest.raises(core.MedOrderInvalid) as exc:
            med_schedule.supersede_for_frequency_change_in_txn(
                conn, order, frequency_before="Q8H", actor_id=None, actor_role=None,
                entry_point="test.frequency")
    assert exc.value.code == "actor_required"
    assert _in_force(order_id) == ["07:00", "13:00", "19:00"]
    # No times in force: nothing to write.
    other = _link(pid, _chart(pid))
    with db_session(immediate=True) as conn:
        other_order = dict(conn.execute("SELECT * FROM med_orders WHERE id = ?",
                                        (other,)).fetchone())
        assert med_schedule.supersede_for_frequency_change_in_txn(
            conn, other_order, frequency_before="Q8H", actor_id="W8ISPHA",
            actor_role="pharmacist", entry_point="test.frequency") is None
    assert _events(other, "amended") == []


def _med_order_update_sql(node):
    """('literal', SET clause) or ('built', None) for an AST string node that
    is an UPDATE of med_orders, else None."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        text = node.value
        if "UPDATE med_orders" not in text:
            return None
        clause = text.split(" SET ", 1)[1] if " SET " in text else text
        return ("literal", clause.split(" WHERE ", 1)[0])
    if isinstance(node, ast.JoinedStr):
        pieces = [v.value for v in node.values
                  if isinstance(v, ast.Constant) and isinstance(v.value, str)]
        if not any("UPDATE med_orders" in p for p in pieces):
            return None
        return ("built", None)
    return None


def test_every_writer_of_an_orders_frequency_supersedes_its_set_times():
    """A write that can change med_orders.frequency (its SET clause names the
    column, or is built at run time in a function that handles a frequency)
    calls supersede_for_frequency_change_in_txn in the same function, so times
    written for one frequency are never applied to another."""
    import re
    writers, checked = [], []
    for path in sorted((SERVER_DIR / "services").rglob("*.py")) + \
            sorted((SERVER_DIR / "routes").rglob("*.py")):
        source = path.read_text()
        if "UPDATE med_orders" not in source:
            continue
        tree = ast.parse(source)
        module_names = {}
        for stmt in tree.body:
            if isinstance(stmt, (ast.Assign, ast.AnnAssign)):
                targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
                for target in targets:
                    if isinstance(target, ast.Name):
                        module_names[target.id] = ast.get_source_segment(source, stmt) or ""
        for func in ast.walk(tree):
            if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            body = ast.get_source_segment(source, func) or ""
            names = {n.id for n in ast.walk(func) if isinstance(n, ast.Name)}
            handles_frequency = "frequency" in body or any(
                "frequency" in module_names.get(n, "") for n in names)
            for node in ast.walk(func):
                found = _med_order_update_sql(node)
                if found is None:
                    continue
                kind, clause = found
                checked.append(f"{path.name}:{func.name}")
                if (kind == "literal" and re.search(r"\bfrequency\b", clause)) or (
                        kind == "built" and handles_frequency):
                    writers.append((path, func.name, body))
    assert checked, "the scan found no UPDATE of med_orders: it is looking in the wrong place"
    assert writers, "the pharmacist's correction writes the frequency: the scan missed it"
    for path, name, body in writers:
        assert "supersede_for_frequency_change_in_txn" in body, (
            f"{path.relative_to(SERVER_DIR)}:{name} can change an order's frequency but does "
            "not supersede the times set for the old one (services/medorders/schedule.py, "
            "supersede_for_frequency_change_in_txn)")


# ── 5. Setting the times: the route ───────────────────────────────────────────

def _put(order_id, uid, role, times, **extra):
    return TestClient(app).put(f"/api/medorders/orders/{order_id}/schedule",
                               headers=_hdr(uid, role), json={"times": times, **extra})


@pytest.fixture()
def doctor():
    return _user("doctor", DEPT)


def test_a_prescriber_sets_the_times(lifecycle, site, doctor):
    pid = _patient()
    med_id = _chart(pid)
    order_id = _link(pid, med_id)
    version = _order(order_id)["version"]
    resp = _put(order_id, doctor, "doctor", ["22:00", "06:00", "14:00"],
                expected_version=version)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["replayed"] is False
    assert body["schedule"]["times"] == ["06:00", "14:00", "22:00"]
    assert body["schedule"]["basis"] == "prescriber"
    assert body["order"]["version"] == version + 1
    rows = _rows(order_id)
    assert [r["due_time"] for r in rows] == ["06:00", "14:00", "22:00"]
    assert {r["authored_by"] for r in rows} == {doctor}
    with db_session() as db:
        (event,) = [dict(r) for r in db.execute(
            "SELECT * FROM med_order_events WHERE med_order_id = ? AND kind = 'amended'",
            (order_id,)).fetchall()]
    payload = json.loads(event["payload_json"])
    assert payload["changes"]["schedule_times"] == {"before": None,
                                                    "after": ["06:00", "14:00", "22:00"]}
    (audit,) = _audits("med_order_amended", order_id)
    meta = json.loads(audit["metadata_json"])
    assert meta["amended_fields"] == ["schedule_times"]
    assert audit["event_category"] == "clinical_access"


def test_the_hospitals_times_are_recorded_as_such_and_a_replay_writes_nothing(
        lifecycle, site, doctor):
    pid = _patient()
    med_id = _chart(pid)
    order_id = _link(pid, med_id)
    first = _put(order_id, doctor, "doctor", ["08:00", "14:00", "20:00"]).json()
    assert first["schedule"]["basis"] == "site_times"
    assert first["schedule"]["site_times_version"] == site.version
    assert first["schedule"]["site_times_fingerprint"] == site.fingerprint
    again = _put(order_id, doctor, "doctor", ["08:00", "14:00", "20:00"]).json()
    assert again["replayed"] is True
    assert len(_rows(order_id)) == 3


def test_new_times_supersede_the_old_and_an_empty_list_clears(lifecycle, site, doctor):
    pid = _patient()
    med_id = _chart(pid)
    order_id = _link(pid, med_id)
    _put(order_id, doctor, "doctor", ["08:00", "14:00", "20:00"])
    _put(order_id, doctor, "doctor", ["06:00", "14:00", "22:00"])
    rows = _rows(order_id)
    assert [r["due_time"] for r in rows if r["superseded_at"] is None] == \
        ["06:00", "14:00", "22:00"]
    assert {r["superseded_by"] for r in rows if r["schedule_version"] == 1} == {doctor}
    cleared = _put(order_id, doctor, "doctor", []).json()
    assert cleared["schedule"] is None
    assert all(r["superseded_at"] for r in _rows(order_id))


@pytest.mark.parametrize("times,code", [
    (["08:00", "14:00"], "invalid_times"),
    (["08:00", "08:20", "20:00"], "invalid_times"),
    (["8am", "14:00", "20:00"], "invalid_times"),
    (["08:00", "09:00", "20:00"], "invalid_times"),
])
def test_times_that_do_not_fit_are_refused(lifecycle, no_site, doctor, times, code):
    pid = _patient()
    med_id = _chart(pid)
    order_id = _link(pid, med_id)
    resp = _put(order_id, doctor, "doctor", times)
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"]["error"] == code
    assert _rows(order_id) == []


def test_an_as_needed_or_irregular_order_has_no_set_times(lifecycle, no_site, doctor):
    pid = _patient()
    prn = _chart(pid, frequency="Q6H PRN", status="prn")
    alternate = _chart(pid, frequency="alternate days")
    prn_order, alt_order = _link(pid, prn), _link(pid, alternate)
    resp = _put(prn_order, doctor, "doctor", ["08:00"])
    assert resp.status_code == 422 and resp.json()["detail"]["error"] == "prn_has_no_set_times"
    resp = _put(alt_order, doctor, "doctor", ["08:00"])
    assert resp.status_code == 422 and resp.json()["detail"]["error"] == "frequency_not_daily"


def test_an_hourly_order_has_no_set_times(lifecycle, no_site, doctor):
    pid = _patient()
    hourly = _chart(pid, frequency="Q1H")
    order_id = _link(pid, hourly)
    resp = _put(order_id, doctor, "doctor", [f"{h:02d}:00" for h in range(24)])
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"]["error"] == "frequency_not_daily"
    assert "every 2 hours" in resp.json()["detail"]["message"]
    assert _rows(order_id) == []
    body = TestClient(app).get(f"/api/medorders/patient/{pid}/schedules",
                               headers=_hdr(doctor, "doctor")).json()
    (entry,) = [o for o in body["orders"] if o["med_order_id"] == order_id]
    assert entry["doses_per_day"] is None  # the screen offers no set times


def test_an_ended_order_and_a_stale_version_are_refused(lifecycle, no_site, doctor):
    pid = _patient()
    med_id = _chart(pid)
    order_id = _link(pid, med_id)
    resp = _put(order_id, doctor, "doctor", ["08:00", "14:00", "20:00"],
                expected_version=_order(order_id)["version"] - 1)
    assert resp.status_code == 409 and resp.json()["detail"]["error"] == "stale_version"
    with db_session() as db:
        db.execute("UPDATE med_orders SET status = 'stopped', projection_status = NULL "
                   "WHERE id = ?", (order_id,))
    resp = _put(order_id, doctor, "doctor", ["08:00", "14:00", "20:00"])
    assert resp.status_code == 409 and resp.json()["detail"]["error"] == "order_not_live"


def test_only_a_prescriber_may_set_times(lifecycle, no_site):
    pid = _patient()
    order_id = _link(pid, _chart(pid))
    nurse = _user("nurse", DEPT)
    assert _put(order_id, nurse, "nurse", ["08:00", "14:00", "20:00"]).status_code == 403


def test_another_departments_order_is_an_audited_404(lifecycle, no_site, doctor):
    pid = _patient(OTHER_DEPT)
    order_id = _link(pid, _chart(pid))
    resp = _put(order_id, doctor, "doctor", ["08:00", "14:00", "20:00"])
    assert resp.status_code == 404
    assert _audits("patient_scope_denied", order_id)
    assert _rows(order_id) == []


def test_the_write_needs_the_lifecycle_and_everything_needs_the_store(store, doctor):
    pid = _patient()
    order_id = _link(pid, _chart(pid))
    resp = _put(order_id, doctor, "doctor", ["08:00", "14:00", "20:00"])
    assert resp.status_code == 404
    assert resp.json()["detail"]["feature"] == "med_order_lifecycle"
    store.setattr(config, "MED_ORDER_STORE_ENABLED", False)
    resp = TestClient(app).get("/api/medorders/dose-times", headers=_hdr(doctor, "doctor"))
    assert resp.status_code == 404
    assert resp.json()["detail"]["feature"] == "med_order_store"


# ── 6. Reading the times ──────────────────────────────────────────────────────

def test_the_patient_read_lists_each_orders_times(lifecycle, site, doctor):
    pid = _patient()
    tds = _chart(pid)
    order_id = _link(pid, tds)
    _put(order_id, doctor, "doctor", ["06:00", "14:00", "22:00"])
    resp = TestClient(app).get(f"/api/medorders/patient/{pid}/schedules",
                               headers=_hdr(doctor, "doctor"))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    (entry,) = [o for o in body["orders"] if o["med_order_id"] == order_id]
    assert entry["schedule"]["times"] == ["06:00", "14:00", "22:00"]
    assert entry["site_times"] == ["08:00", "14:00", "20:00"]
    assert entry["doses_per_day"] == 3
    assert body["site_times"]["available"] is True
    with db_session() as db:
        assert db.execute("SELECT COUNT(*) FROM audit_logs WHERE action_type = "
                          "'med_order_schedules_viewed' AND patient_id = ?",
                          (pid,)).fetchone()[0] >= 1


def test_the_dose_times_read_carries_no_patient_data(store, site, doctor):
    body = TestClient(app).get("/api/medorders/dose-times", headers=_hdr(doctor, "doctor")).json()
    assert body["available"] is True and body["version"] == site.version
    assert {"frequency": "TDS", "times": ["08:00", "14:00", "20:00"]} in body["times"]
    assert set(body) == {"available", "version", "fingerprint", "signed", "times"}


def test_with_no_file_the_dose_times_read_says_so(store, no_site, doctor):
    body = TestClient(app).get("/api/medorders/dose-times", headers=_hdr(doctor, "doctor")).json()
    assert body["available"] is False and body["times"] == []
