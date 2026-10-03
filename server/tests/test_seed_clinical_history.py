"""scripts/reset_db_and_seed.py: the clinical-history backfill, the clock and the hospital day.

WHAT THESE TESTS PROTECT. The backfill files labs and progress notes on a fixed
schedule relative to each patient's admission, and randomize_dates() spreads
those admissions over the last three days. Until 2026-09-04 the whole schedule
was filed regardless of how long the patient had actually been on the ward, so
a patient admitted this morning carried a "Progress Note: Day 2" dated the day
after tomorrow: 48 of the 150 documents on a freshly seeded ward were ahead of
the wall clock, and the Patient Story, newest first, opened on them. Until
2026-09-05 the rounds notes were then filed at fixed offsets (+32h as "Day 1",
+56h as "Day 2"), counting elapsed 24-hour blocks from a Day 0, while the
round sheet counts calendar days with the admission day as Day 1: the sheet
always read one day ahead of the newest note, and a patient admitted late in
the evening had no note on their first morning.

The invariants, each pinned below:

  1. NOTHING IS DATED IN THE FUTURE, for any admission age, including one that
     is seconds old.
  2. NOTHING PRECEDES THE ADMISSION. The Story leads with the admission event,
     so a document before it reads as a chart from a previous visit.
  3. THE ORDER OF THE SCHEDULE SURVIVES CLIPPING: what a chart holds now is a
     prefix of what the same admission holds once the clock has run past the
     whole schedule.
  4. "PROGRESS NOTE: DAY N" IS THE ROUND SHEET'S DAY N: calendar days in the
     ward's zone, admission day = 1. One rounds note per hospital day after the
     first, at 08:00, after that morning's 06:00 labs; none on the admission
     day; no day skipped.
  5. THE LAB VALUES DEPEND ON THE WARD POSITION, NOT ON THE CLOCK: clipping
     drops documents, it does not change the numbers on the ones that remain.

The last group runs the real function in a subprocess against its own database,
the way tests/test_seed_modules.py does, so the INSERT path and BOTH timestamp
dialects of admissions.created_at are exercised, not just the planner. The
shared session DB is never touched: the backfill writes to every admitted
patient it can see, which would change what the other modules count.
"""
from __future__ import annotations

import json
import os
import random
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import pytest

from scripts.reset_db_and_seed import (
    MORNING_LABS_AT,
    ROUNDS_AT,
    ROUNDS_DAYS,
    hospital_day,
    plan_clinical_documents,
)

SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The ward's zone for the planner tests. A fixed offset rather than a named
# zone, so the tests need no tzdata; the ward is in India either way.
IST = timezone(timedelta(hours=5, minutes=30))

# 18:00 on the ward on 4 Sept, after that morning's rounds.
NOW = datetime(2026, 9, 4, 18, 0, tzinfo=IST)


def _at(days_ago: int, hh: int, mm: int = 0) -> datetime:
    return datetime(2026, 9, 4 - days_ago, hh, mm, tzinfo=IST)


# Admissions placed on both sides of every step of the schedule AND of the
# day boundaries, with the documents each one holds at NOW worked out by hand.
CASES = {
    "one-second": (NOW - timedelta(seconds=1), []),
    "half-hour": (NOW - timedelta(minutes=30), []),
    "just-past-1h": (NOW - timedelta(hours=1, seconds=1), ["Admission Note"]),
    "3h-today-15:00": (_at(0, 15), ["Admission Note", "Complete Blood Count"]),
    # Admitted this morning at 05:30: 06:00 and 08:00 have passed, but they
    # were day 1's, and rounds notes start on day 2.
    "today-05:30": (_at(0, 5, 30), ["Admission Note", "Complete Blood Count", "Basic Metabolic Panel"]),
    # Admitted last night: a Day 2 note on the first morning, 8.5h in. The old
    # +32h offset gave this patient nothing until tomorrow morning.
    "last-night-23:30": (_at(1, 23, 30), ["Admission Note", "Complete Blood Count", "Basic Metabolic Panel",
                                          "Basic Metabolic Panel", "Progress Note: Day 2"]),
    "yesterday-09:00": (_at(1, 9), ["Admission Note", "Complete Blood Count", "Basic Metabolic Panel",
                                    "Basic Metabolic Panel", "Progress Note: Day 2"]),
    "two-days-ago-01:00": (_at(2, 1), ["Admission Note", "Complete Blood Count", "Basic Metabolic Panel",
                                       "Basic Metabolic Panel", "Progress Note: Day 2",
                                       "Basic Metabolic Panel", "Progress Note: Day 3"]),
    "72h-ago-18:00": (_at(3, 18), ["Admission Note", "Complete Blood Count", "Basic Metabolic Panel",
                                   "Basic Metabolic Panel", "Progress Note: Day 2",
                                   "Basic Metabolic Panel", "Progress Note: Day 3",
                                   "Basic Metabolic Panel", "Progress Note: Day 4"]),
}
IDS = list(CASES)


def _plan(admit: datetime, now: datetime = NOW, seed: int = 42):
    return plan_clinical_documents(admit, now, "Seed Clock Patient", "COPD exacerbation",
                                   random.Random(seed), tz=IST)


def _whole_schedule(admit: datetime, seed: int = 42):
    """What the admission holds once the clock has run past the entire schedule."""
    return _plan(admit, admit + timedelta(days=30), seed)


def _sheet_day(now: datetime, admit: datetime) -> int:
    """admissionDay() from src/components/round/roundSummary.ts, transcribed:
    the difference between the two local calendar days, plus one."""
    start_of_day = lambda d: d.astimezone(IST).replace(hour=0, minute=0, second=0, microsecond=0)
    return round((start_of_day(now) - start_of_day(admit)).total_seconds() / 86_400) + 1


# ── 1 + 2 + 3 · The clock, the admission, and the order ─────────────────────

@pytest.mark.parametrize("case", IDS)
def test_every_document_sits_between_the_admission_and_now(case):
    admit, _expected = CASES[case]
    for _doc_type, title, _content, ts in _plan(admit):
        assert ts < NOW, f"{title} is dated {ts.isoformat()}, after now ({NOW.isoformat()})"
        assert ts >= admit, f"{title} is dated {ts.isoformat()}, before the admission ({admit.isoformat()})"


@pytest.mark.parametrize("case", IDS)
def test_a_chart_holds_a_prefix_of_its_admissions_whole_schedule_in_order(case):
    admit, _expected = CASES[case]
    plan = _plan(admit)
    titles = [title for _, title, _, _ in plan]
    whole = [title for _, title, _, _ in _whole_schedule(admit)]
    assert titles == whole[:len(titles)], (
        "clipping must drop documents from the END of the schedule only; "
        f"got {titles} against {whole}"
    )
    stamps = [ts for _, _, _, ts in plan]
    assert stamps == sorted(stamps)
    assert len(set(stamps)) == len(stamps), "two documents share a timestamp"


@pytest.mark.parametrize("case", IDS)
def test_the_documents_each_stay_holds_are_the_ones_worked_out_by_hand(case):
    admit, expected = CASES[case]
    assert [title for _, title, _, _ in _plan(admit)] == expected


def test_a_stay_of_seconds_holds_nothing_rather_than_something_future():
    assert _plan(NOW - timedelta(seconds=1)) == []


def test_the_returned_timestamps_are_utc():
    for _d, _t, _c, ts in _whole_schedule(_at(3, 18)):
        assert ts.tzinfo is not None and ts.utcoffset() == timedelta(0)


# ── 4 · The note's Day N is the round sheet's Day N ──────────────────────────

@pytest.mark.parametrize("case", IDS)
def test_progress_note_day_is_the_hospital_day_the_sheet_would_show(case):
    admit, _expected = CASES[case]
    notes = [(title, ts) for _, title, _, ts in _whole_schedule(admit) if title.startswith("Progress Note: Day ")]
    assert notes, "every admission's whole schedule holds rounds notes"
    for title, ts in notes:
        n = int(title.rsplit(" ", 1)[1])
        assert n == hospital_day(ts, admit, IST), f"{title} is filed on hospital day {hospital_day(ts, admit, IST)}"
        assert n == _sheet_day(ts, admit), f"{title} would show as Day {_sheet_day(ts, admit)} on the round sheet"


@pytest.mark.parametrize("case", IDS)
def test_rounds_notes_start_on_day_two_and_skip_no_day(case):
    admit, _expected = CASES[case]
    days = [int(title.rsplit(" ", 1)[1]) for _, title, _, _ in _whole_schedule(admit)
            if title.startswith("Progress Note: Day ")]
    assert days == list(ROUNDS_DAYS)
    assert days[0] == 2, "the admission note covers day 1; rounds notes begin the next morning"


@pytest.mark.parametrize("case", IDS)
def test_each_rounds_note_follows_that_mornings_labs(case):
    admit, _expected = CASES[case]
    whole = _whole_schedule(admit)
    for i, (doc_type, title, _c, ts) in enumerate(whole):
        if not title.startswith("Progress Note: Day "):
            continue
        local = ts.astimezone(IST)
        assert (local.hour, local.minute) == (ROUNDS_AT.hour, ROUNDS_AT.minute), f"{title} filed at {local.time()}"
        prev_type, prev_title, _pc, prev_ts = whole[i - 1]
        prev_local = prev_ts.astimezone(IST)
        assert prev_type == "lab_result" and prev_local.date() == local.date(), f"{title} is not preceded by that morning's labs"
        assert (prev_local.hour, prev_local.minute) == (MORNING_LABS_AT.hour, MORNING_LABS_AT.minute)


def test_a_late_evening_admission_gets_its_first_note_the_next_morning():
    """The case the old fixed offsets got wrong: admitted 23:30, rounds at 08:00
    the next morning are 8.5 hours in and belong to Day 2, not to nothing."""
    admit = _at(1, 23, 30)
    plan = _plan(admit)
    note = next((ts for _, title, _, ts in plan if title == "Progress Note: Day 2"), None)
    assert note is not None
    assert note.astimezone(IST) == _at(0, 8)
    assert note - admit == timedelta(hours=8, minutes=30)


def test_the_ward_zone_is_the_default():
    """Without an explicit zone the planner counts days in the server's own zone,
    which is the browsers' zone on an appliance, so the seed and the sheet agree."""
    admit = datetime(2026, 9, 1, 12, 30, tzinfo=timezone.utc)
    server_zone = datetime.now().astimezone().tzinfo
    by_default = plan_clinical_documents(admit, admit + timedelta(days=30), "P", "dx", random.Random(1))
    explicit = plan_clinical_documents(admit, admit + timedelta(days=30), "P", "dx", random.Random(1), tz=server_zone)
    assert by_default == explicit


# ── 5 · The numbers do not depend on the clock ───────────────────────────────

def test_lab_values_depend_on_the_ward_position_not_on_the_clock():
    admit = _at(3, 18)
    short = _plan(admit, admit + timedelta(hours=6), seed=7)
    long = _whole_schedule(admit, seed=7)
    short_labs = [(t, c) for d, t, c, _ in short if d == "lab_result"]
    long_labs = [(t, c) for d, t, c, _ in long if d == "lab_result"]
    assert len(short_labs) == 2 and len(long_labs) == 2 + len(ROUNDS_DAYS)
    assert short_labs == long_labs[:2], (
        "the CBC and BMP a six-hour admission holds must be the same draws the "
        "same ward position would hold three days in"
    )


def test_the_content_names_the_patient_and_the_diagnosis():
    by_title = {t: c for _, t, c, _ in _whole_schedule(_at(3, 18))}
    assert "Seed Clock Patient" in by_title["Complete Blood Count"]
    assert "COPD exacerbation" in by_title["Admission Note"]
    assert "COPD exacerbation" in by_title["Progress Note: Day 2"]
    assert "COPD exacerbation" in by_title["Progress Note: Day 3"]


# ── The real function, against its own database ─────────────────────────────

# Five admitted patients whose admissions straddle the schedule, one of them
# stamped in SQLite's bare dialect (the shape CURRENT_TIMESTAMP writes) and the
# rest in the application's isoformat, since admissions.created_at holds both.
_AGES_H = {
    "PAT-SCH-FRESH": 0.0003,   # about one second
    "PAT-SCH-3H": 3,
    "PAT-SCH-30H": 30,
    "PAT-SCH-40H": 40,         # bare dialect
    "PAT-SCH-60H": 60,
}

_SCRIPT = """
import json
from datetime import datetime, timedelta, timezone
import database
database.init_db()
from database import db_session

AGES_H = %s
now = datetime.now(timezone.utc)
admits = {}
with db_session() as db:
    db.execute(
        "INSERT OR REPLACE INTO users (id, name, role, department, password_hash, created_at) "
        "VALUES ('DOC001', 'Dr. Meera Sharma', 'doctor', 'General Medicine', 'x', CURRENT_TIMESTAMP)")
    for i, (pid, age) in enumerate(AGES_H.items()):
        admit = now - timedelta(hours=age)
        if pid.endswith("40H"):
            admit = admit.replace(microsecond=0)
            stamp = admit.strftime("%%Y-%%m-%%d %%H:%%M:%%S")
        else:
            stamp = admit.isoformat()
        admits[pid] = admit.isoformat()
        db.execute(
            "INSERT INTO patients (id, name, mrn, age, sex, ward, bed, attending_doctor_id, "
            "diagnosis, admission_date, status, created_at) "
            "VALUES (?, ?, ?, 55, 'Male', 'GM-1', ?, 'DOC001', 'COPD exacerbation', ?, 'admitted', ?)",
            (pid, "Seed Clock " + pid[-3:], "MRN-" + pid, "GM-10" + str(i), stamp, now.isoformat()))
        db.execute(
            "INSERT INTO admissions (id, patient_id, admitted_by, department, ward, bed, "
            "attending_doctor_id, status, created_at) "
            "VALUES (?, ?, 'DOC001', 'General Medicine', 'GM-1', ?, 'DOC001', 'active', ?)",
            ("ADM-" + pid, pid, "GM-10" + str(i), stamp))

from scripts.reset_db_and_seed import seed_clinical_history
seed_clinical_history()

with db_session() as db:
    rows = db.execute(
        "SELECT patient_id, doc_type, title, created_at FROM clinical_documents "
        "ORDER BY patient_id, created_at").fetchall()
print("RESULT" + json.dumps({"now": now.isoformat(), "admits": admits,
                             "docs": [dict(r) for r in rows]}))
""" % json.dumps(_AGES_H)


def _run_backfill(db_path) -> dict:
    env = {k: v for k, v in os.environ.items() if k != "ORB_DEPLOYMENT_PROFILE"}
    env["ORB_DB_PATH"] = str(db_path)
    env["ORB_DEPLOYMENT_PROFILE"] = "dev"
    env["PYTHONPATH"] = SERVER_DIR
    proc = subprocess.run(
        [sys.executable, "-c", _SCRIPT], capture_output=True, text=True,
        env=env, cwd=SERVER_DIR, timeout=300,
    )
    for line in proc.stdout.splitlines():
        if line.startswith("RESULT"):
            return json.loads(line[len("RESULT"):])
    raise AssertionError(
        f"backfill subprocess produced no result\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
    )


@pytest.fixture(scope="module")
def backfilled(tmp_path_factory) -> dict:
    return _run_backfill(tmp_path_factory.mktemp("seed_clinical_history") / "orb.db")


def _ts(raw: str) -> datetime:
    dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _titles(backfilled: dict) -> dict[str, list[str]]:
    titles: dict[str, list[str]] = {pid: [] for pid in _AGES_H}
    for d in backfilled["docs"]:
        titles[d["patient_id"]].append(d["title"])
    return titles


def test_backfill_writes_nothing_dated_after_the_seed_ran(backfilled):
    now = _ts(backfilled["now"])
    late = [d for d in backfilled["docs"] if _ts(d["created_at"]) >= now]
    assert not late, f"future-dated documents on a fresh seed: {late}"


def test_backfill_writes_nothing_before_the_admission(backfilled):
    for d in backfilled["docs"]:
        admit = _ts(backfilled["admits"][d["patient_id"]])
        assert _ts(d["created_at"]) >= admit, f"{d} precedes its admission {admit.isoformat()}"


def test_backfill_files_exactly_what_the_planner_plans_for_each_stay(backfilled):
    """The INSERT path stores the planner's documents, in the ward's zone, from
    either timestamp dialect. The planner is run here with the subprocess's
    clock and anchors, so the comparison is exact rather than shape-only."""
    now = _ts(backfilled["now"])
    titles = _titles(backfilled)
    for pid in _AGES_H:
        admit = _ts(backfilled["admits"][pid])
        planned = [t for _, t, _, _ in plan_clinical_documents(admit, now, "x", "COPD exacerbation", random.Random(0))]
        assert titles[pid] == planned, f"{pid}: filed {titles[pid]}, planned {planned}"


def test_backfill_gives_each_stay_the_part_of_the_schedule_that_has_happened(backfilled):
    titles = _titles(backfilled)
    assert titles["PAT-SCH-FRESH"] == []
    assert titles["PAT-SCH-3H"][:2] == ["Admission Note", "Complete Blood Count"]
    # Day 2's 08:00 rounds are at most 32h after any admission, so both of
    # these stays have passed at least one morning round, whatever the clock.
    assert "Progress Note: Day 2" in titles["PAT-SCH-40H"], "the bare timestamp dialect must anchor the chart too"
    assert "Progress Note: Day 2" in titles["PAT-SCH-60H"]
