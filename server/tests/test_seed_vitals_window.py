"""scripts/reset_db_and_seed.py: the seeded vitals and the admission.

WHAT THESE TESTS PROTECT. The clinical seeder stamps every patient a current
reading plus 24 hourly readings for the day before it, all relative to its own
clock, and randomize_dates() then spreads the admissions over the three days
before that clock. Until 2026-09-05 the readings were left where they were, so
a patient admitted six hours ago carried a trend that began 18 hours before
they arrived: 130 of the 750 seeded readings on that day's reseed preceded
their admission. The fix is the mirror image of the documents rule pinned in
test_seed_clinical_history.py: the trend stays anchored at the clock and is
clipped at the admission.

The invariants, each pinned below:

  1. NO READING PRECEDES THE ADMISSION, in either timestamp dialect.
  2. THE CURRENT READING ALWAYS SURVIVES. randomize_dates() draws the admission
     strictly before patients.created_at, the instant the seeder stamps on the
     current reading, so even a stay of one second keeps one set of vitals.
  3. THE TREND IS CLIPPED FROM ITS START ONLY: what a short stay keeps is the
     most recent part of what a long stay keeps, and a stay longer than the
     trend keeps all of it.
  4. THE VALUES ARE UNTOUCHED: clipping drops rows, it never rewrites one.

Both functions run in a subprocess against their own database, the way
tests/test_seed_clinical_history.py does, so the real DELETE path is exercised.
The shared session DB is never touched.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import pytest

SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The ward's clock: what the seeder stamps on patients.created_at and on the
# current reading. Fixed, because neither function under test reads the wall
# clock once that stamp is present.
T0 = datetime(2026, 9, 5, 6, 0, tzinfo=timezone.utc)

# Stays that straddle the 24-reading trend. The 23.5h one is written in
# SQLite's bare dialect on BOTH the admission and the readings, since a bot
# writes that shape and a string compare would misjudge it.
_AGES = {
    "PAT-VW-1S": ("1s", 1 / 3600),
    "PAT-VW-30M": ("30m", 0.5),
    "PAT-VW-3H": ("3h", 3),
    "PAT-VW-23H30-BARE": ("23h30m bare", 23.5),
    "PAT-VW-72H": ("72h", 72),
}
_RAND = [f"PAT-VW-RAND-{i}" for i in range(12)]

_SCRIPT = """
import json
from datetime import datetime, timedelta, timezone
import database
database.init_db()
from database import db_session

T0 = datetime.fromisoformat(%r)
AGES = %s
RAND = %s

def patient(db, pid, bed, admit_stamp):
    db.execute(
        "INSERT INTO patients (id, name, mrn, age, sex, ward, bed, attending_doctor_id, "
        "diagnosis, admission_date, status, created_at) "
        "VALUES (?, ?, ?, 61, 'Female', 'GM-1', ?, 'DOC001', 'Community-acquired pneumonia', ?, 'admitted', ?)",
        (pid, "Vitals Window " + pid[7:], "MRN-" + pid, bed, admit_stamp, T0.isoformat()))
    db.execute(
        "INSERT INTO admissions (id, patient_id, admitted_by, department, ward, bed, "
        "attending_doctor_id, status, created_at) "
        "VALUES (?, ?, 'DOC001', 'General Medicine', 'GM-1', ?, 'DOC001', 'active', ?)",
        ("ADM-" + pid, pid, bed, admit_stamp))

with db_session() as db:
    db.execute(
        "INSERT OR REPLACE INTO users (id, name, role, department, password_hash, created_at) "
        "VALUES ('DOC001', 'Dr. Meera Sharma', 'doctor', 'General Medicine', 'x', CURRENT_TIMESTAMP)")
    # Phase A: records stamped at T0 with the admission still at T0, the shape
    # the clinical seeder leaves behind for randomize_dates() to spread.
    for i, pid in enumerate(RAND):
        patient(db, pid, "GM-2%%02d" %% i, T0.isoformat())

from scripts.reset_db_and_seed import randomize_dates, clip_vitals_at_admission
randomize_dates()

with db_session() as db:
    drawn = {r["patient_id"]: r["created_at"] for r in db.execute(
        "SELECT patient_id, created_at FROM admissions").fetchall()}
    # Phase B: exact stays, each with the seeder's 25 readings at T0 - k hours
    # carrying heart_rate 60 + k so a rewritten value would show.
    for i, (pid, (_label, hours)) in enumerate(AGES.items()):
        admit = T0 - timedelta(hours=hours)
        bare = pid.endswith("BARE")
        stamp = admit.replace(microsecond=0).strftime("%%Y-%%m-%%d %%H:%%M:%%S") if bare else admit.isoformat()
        patient(db, pid, "GM-3%%02d" %% i, stamp)
        for k in range(24, -1, -1):
            ts = T0 - timedelta(hours=k)
            db.execute(
                "INSERT INTO vitals (patient_id, timestamp, heart_rate, systolic_bp, diastolic_bp, "
                "spo2, temperature, respiratory_rate) VALUES (?, ?, ?, 118, 76, 96.0, 37.1, 18)",
                (pid, ts.strftime("%%Y-%%m-%%d %%H:%%M:%%S") if bare else ts.isoformat(), 60 + k))

clip_vitals_at_admission()

with db_session() as db:
    kept = {}
    for pid in AGES:
        kept[pid] = [[r["timestamp"], r["heart_rate"]] for r in db.execute(
            "SELECT timestamp, heart_rate FROM vitals WHERE patient_id = ? ORDER BY id", (pid,)).fetchall()]
    untouched = db.execute("SELECT COUNT(*) FROM vitals WHERE patient_id NOT IN (%%s)" %%
                           ",".join("?" * len(AGES)), tuple(AGES)).fetchone()[0]
print("RESULT" + json.dumps({"drawn": drawn, "kept": kept, "untouched": untouched}))
""" % (T0.isoformat(), json.dumps(_AGES), json.dumps(_RAND))


def _run(db_path) -> dict:
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
        f"seed subprocess produced no result\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
    )


@pytest.fixture(scope="module")
def clipped(tmp_path_factory) -> dict:
    return _run(tmp_path_factory.mktemp("seed_vitals_window") / "orb.db")


def _ts(raw: str) -> datetime:
    dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _admit(pid: str) -> datetime:
    admit = T0 - timedelta(hours=_AGES[pid][1])
    return admit.replace(microsecond=0) if pid.endswith("BARE") else admit


def _hours_kept(pid: str, clipped: dict) -> list[int]:
    """The k of each surviving reading (reading k sits at T0 - k hours), oldest first."""
    return sorted(
        (round((T0 - _ts(ts)).total_seconds() / 3600) for ts, _hr in clipped["kept"][pid]),
        reverse=True,
    )


# ── 2 · The window ends before the clock the current reading carries ─────────

def test_randomize_dates_draws_every_admission_in_the_three_days_before_the_records_clock(clipped):
    for pid in _RAND:
        admit = _ts(clipped["drawn"][pid])
        assert T0 - timedelta(days=3) <= admit, f"{pid} admitted {admit.isoformat()}, more than 3 days before its record"
        assert admit <= T0 - timedelta(seconds=1), (
            f"{pid} admitted {admit.isoformat()}, not strictly before the clock {T0.isoformat()} "
            "that its current reading is stamped with"
        )


def test_the_draws_use_the_window_not_one_corner_of_it(clipped):
    stamps = {clipped["drawn"][pid] for pid in _RAND}
    assert len(stamps) > 1, "every admission landed on the same instant"


# ── 1 · Nothing precedes the admission ───────────────────────────────────────

@pytest.mark.parametrize("pid", list(_AGES), ids=[v[0] for v in _AGES.values()])
def test_no_surviving_reading_precedes_the_admission(pid, clipped):
    admit = _admit(pid)
    for ts, _hr in clipped["kept"][pid]:
        assert _ts(ts) >= admit, f"{pid}: reading at {ts} precedes the admission {admit.isoformat()}"


# ── 2 · The current reading survives ─────────────────────────────────────────

@pytest.mark.parametrize("pid", list(_AGES), ids=[v[0] for v in _AGES.values()])
def test_the_current_reading_survives_every_stay(pid, clipped):
    assert 0 in _hours_kept(pid, clipped), f"{pid} lost the reading stamped at the clock"


def test_a_stay_of_seconds_keeps_exactly_the_current_reading(clipped):
    assert _hours_kept("PAT-VW-1S", clipped) == [0]
    assert _hours_kept("PAT-VW-30M", clipped) == [0]


# ── 3 · Clipped from the start only ──────────────────────────────────────────

def test_each_stay_keeps_the_readings_taken_since_it_began(clipped):
    assert _hours_kept("PAT-VW-3H", clipped) == [3, 2, 1, 0]
    assert _hours_kept("PAT-VW-23H30-BARE", clipped) == list(range(23, -1, -1)), (
        "the bare dialect must be parsed, not string-compared: the 24h-old reading "
        "precedes a 23.5h-old admission and nothing else does"
    )
    assert _hours_kept("PAT-VW-72H", clipped) == list(range(24, -1, -1))


def test_a_short_stay_holds_the_recent_end_of_a_long_one(clipped):
    long = _hours_kept("PAT-VW-72H", clipped)
    for pid in _AGES:
        short = _hours_kept(pid, clipped)
        assert short == long[len(long) - len(short):], f"{pid} keeps {short}, not a tail of {long}"


# ── 4 · Values untouched ─────────────────────────────────────────────────────

@pytest.mark.parametrize("pid", list(_AGES), ids=[v[0] for v in _AGES.values()])
def test_surviving_readings_keep_their_values(pid, clipped):
    for ts, hr in clipped["kept"][pid]:
        k = round((T0 - _ts(ts)).total_seconds() / 3600)
        assert hr == 60 + k, f"{pid}: reading {k}h before the clock reads {hr}, seeded as {60 + k}"


def test_charts_without_readings_are_simply_left_alone(clipped):
    assert clipped["untouched"] == 0
