"""A partial vitals row must not stop the ward.

WHY THIS FILE EXISTS. `_generate_vitals` does mean-reversion arithmetic against
the patient's most recent row:

    dia_shift = random.gauss(0, noise["dia"]) + reversion * (targets["dia"] - latest["diastolic_bp"])

Partial rows are NORMAL. A nurse can chart RR and SpO2 without a blood pressure,
and observation/import rows routinely leave `diastolic_bp` NULL. Eight admitted
patients in the demo database had exactly that, and the subtraction raised

    unsupported operand type(s) for -: 'float' and 'NoneType'

out of the whole batch — so NO patient got a new reading, not just the eight.
The simulator had been throwing that on every 5-second tick for a week: nothing
on any ward board moved, `RECORDS WRITTEN (24H)` read 0, "obs 144h late" appeared
on the nurse board, and the Sepsis Six timer counted past 226 hours.

The blast radius is the point. This file asserts the two properties that
together contain it: a NULL channel resolves to a clinically sensible value, and
one bad patient cannot cost every other patient their reading.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import services.bot_service as bot_service
from services.bot_service import SEVERITY_PROFILES, _coalesce_vital, _generate_vitals
from database import db_session


# ── The arithmetic guard ──────────────────────────────────────────────────────

def test_null_channel_falls_back_to_the_severity_target():
    targets = SEVERITY_PROFILES["critical"]["targets"]
    assert _coalesce_vital(None, targets["dia"]) == pytest.approx(52.0)
    assert _coalesce_vital("", targets["dia"]) == pytest.approx(52.0)


def test_present_values_pass_through_as_floats():
    assert _coalesce_vital(64, 52.0) == pytest.approx(64.0)
    assert _coalesce_vital("71.5", 52.0) == pytest.approx(71.5)


def test_unparseable_garbage_does_not_raise():
    """An import can put anything in a text column; the tick must survive it."""
    assert _coalesce_vital("n/a", 52.0) == pytest.approx(52.0)
    assert _coalesce_vital(object(), 52.0) == pytest.approx(52.0)


def test_the_subtraction_that_crashed_the_ward_no_longer_raises():
    targets = SEVERITY_PROFILES["critical"]["targets"]
    latest = {"diastolic_bp": None}
    # This is bot_service.py:145 in miniature — the exact expression that threw.
    shift = 0.03 * (targets["dia"] - _coalesce_vital(latest.get("diastolic_bp"), targets["dia"]))
    assert shift == pytest.approx(0.0)


# ── The blast radius ──────────────────────────────────────────────────────────

VICTIM = "PAT-SIMNULL-A"
BYSTANDER = "PAT-SIMNULL-B"


@pytest.fixture
def two_admitted_patients():
    """Own patients, discharged on teardown — a leaked admitted patient shifts
    every census/occupancy assertion in the rest of the suite."""
    with db_session() as db:
        for pid, mrn, name in ((VICTIM, "MRN-SIMNULL-A", "Sim Null A"),
                               (BYSTANDER, "MRN-SIMNULL-B", "Sim Null B")):
            db.execute(
                "INSERT OR REPLACE INTO patients (id, name, mrn, bed, ward, status, age, sex) "
                "VALUES (?, ?, ?, ?, ?, 'admitted', 60, 'Male')",
                (pid, name, mrn, "GM-901", "GM-1"),
            )
    yield VICTIM, BYSTANDER
    with db_session() as db:
        db.execute("DELETE FROM vitals WHERE patient_id IN (?, ?)", (VICTIM, BYSTANDER))
        db.execute("UPDATE patients SET status = 'discharged' WHERE id IN (?, ?)",
                   (VICTIM, BYSTANDER))


def test_a_patient_with_a_null_bp_does_not_stop_every_other_patient(two_admitted_patients):
    """The regression that mattered: one NULL killed the ENTIRE batch."""
    victim, bystander = two_admitted_patients
    ts_before = "2020-01-01T00:00:00+00:00"
    with db_session() as db:
        # A partial row: RR and SpO2 charted, no blood pressure. Exactly what
        # eight admitted patients looked like in the demo database.
        db.execute(
            """INSERT INTO vitals (patient_id, timestamp, heart_rate, systolic_bp,
               diastolic_bp, spo2, temperature, respiratory_rate)
               VALUES (?, ?, ?, ?, NULL, ?, ?, ?)""",
            (victim, ts_before, 96.0, 118.0, 94.0, 37.1, 20.0),
        )
        db.execute(
            """INSERT INTO vitals (patient_id, timestamp, heart_rate, systolic_bp,
               diastolic_bp, spo2, temperature, respiratory_rate)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (bystander, ts_before, 88.0, 124.0, 76.0, 97.0, 36.7, 16.0),
        )

    now_ts = "2026-08-08T12:00:00+00:00"
    _generate_vitals(now_ts, 1)  # must not raise

    with db_session() as db:
        for pid in (victim, bystander):
            row = db.execute(
                "SELECT diastolic_bp FROM vitals WHERE patient_id = ? AND timestamp = ?",
                (pid, now_ts),
            ).fetchone()
            assert row is not None, f"{pid} got no new reading — the batch died again"
            assert row["diastolic_bp"] is not None


def test_any_future_per_patient_failure_costs_one_patient_not_the_ward(
    two_admitted_patients, monkeypatch
):
    """Containment, independent of WHICH bad row shows up next.

    The NULL diastolic is fixed, but the property worth keeping is the one that
    turned a one-patient data gap into a ward-wide outage: an exception on one
    patient must not reach the batch.
    """
    victim, bystander = two_admitted_patients
    ts_before = "2020-01-01T00:00:00+00:00"
    with db_session() as db:
        for pid in (victim, bystander):
            db.execute(
                """INSERT INTO vitals (patient_id, timestamp, heart_rate, systolic_bp,
                   diastolic_bp, spo2, temperature, respiratory_rate)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (pid, ts_before, 90.0, 120.0, 74.0, 96.0, 36.9, 18.0),
            )

    real = bot_service._next_reading

    def explode(db, pid, latest, now_ts, **kwargs):
        if pid == victim:
            raise RuntimeError("simulated bad row")
        return real(db, pid, latest, now_ts, **kwargs)

    monkeypatch.setattr(bot_service, "_next_reading", explode)

    now_ts = "2026-08-08T12:05:00+00:00"
    bot_service._generate_vitals(now_ts, 1)  # must not raise

    with db_session() as db:
        got_bystander = db.execute(
            "SELECT 1 FROM vitals WHERE patient_id = ? AND timestamp = ?",
            (bystander, now_ts),
        ).fetchone()
        got_victim = db.execute(
            "SELECT 1 FROM vitals WHERE patient_id = ? AND timestamp = ?",
            (victim, now_ts),
        ).fetchone()

    assert got_bystander is not None, "one bad patient took the whole ward down"
    assert got_victim is None, "the failing patient should simply be skipped"
