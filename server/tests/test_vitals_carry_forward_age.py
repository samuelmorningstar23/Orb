"""Carry-forward is age-limited by the time of the MEASUREMENT (ruled 2026-10-03).

Independent review 2026-10-02, findings F9, G1, G6. Measured before the ruling:
an SBP 78 and RR 30 taken at 02:00 were scored, printed as "Current vitals" and
alerted as "Severe tachypnea (RR 30); Severe hypotension (SBP 78)" on an
HR-only monitor message at 13:00, and one nurse "confused" led every later
simulator alert for as long as the simulator ran.

The rule (services/vitals_carry.py): HR, BP, SpO2 and RR are carried for at most
4 h, temperature 8 h, ACVPU and supplemental O2 12 h, all measured from when the
value was really taken, which each row now records in `vitals.carried_from`.
Past the limit a value is None: not measured, scored 0, NEWS2 incomplete.
"""
from __future__ import annotations

import json
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from database import db_session  # noqa: E402
from services.clinical_time import format_utc  # noqa: E402
from services.ingestion_adapters import CanonicalEvent, ObservationType, ValueType  # noqa: E402
from services.ingestion_service import _insert_vitals, _persist_vitals_row  # noqa: E402
from services.vigil_service import compute_news2  # noqa: E402
from services.vitals_carry import carry_forward, dump_carried  # noqa: E402

NOW = datetime.now(timezone.utc).replace(microsecond=0)


def at(**delta) -> str:
    """NOW plus `delta` (negative for the past), in the stored spelling."""
    return format_utc(NOW + timedelta(**delta))


# ── the helper ───────────────────────────────────────────────────────────────

ROW = {"timestamp": at(hours=0), "heart_rate": 120.0, "systolic_bp": 78.0,
       "diastolic_bp": 50.0, "spo2": 90.0, "respiratory_rate": 30.0,
       "temperature": 38.4, "consciousness": "confusion", "on_oxygen": 1}


@pytest.mark.parametrize("column, limit_hours", [
    ("heart_rate", 4), ("systolic_bp", 4), ("diastolic_bp", 4), ("spo2", 4),
    ("respiratory_rate", 4), ("temperature", 8), ("consciousness", 12), ("on_oxygen", 12),
])
def test_each_value_is_carried_up_to_its_limit_and_not_a_second_past_it(column, limit_hours):
    edge = at(hours=limit_hours)
    past = at(hours=limit_hours, seconds=1)
    assert carry_forward(ROW, edge, [column])[0][column] == ROW[column]
    assert carry_forward(ROW, past, [column])[0][column] is None


def test_age_runs_from_the_measurement_not_from_the_row_that_copied_it():
    """A monitor sending HR every few minutes writes a fresh row each time. If
    age ran from the latest row, a 02:00 BP would never expire."""
    copied = {**ROW, "timestamp": at(hours=3),
              "carried_from": json.dumps({"systolic_bp": at(hours=0)})}
    values, carried = carry_forward(copied, at(hours=4, seconds=1),
                                    ["systolic_bp", "heart_rate"])
    assert values["systolic_bp"] is None, "a 4 h 1 s old BP was carried because its row is 1 h old"
    assert values["heart_rate"] == 120.0          # measured in that row, 1 h ago
    assert carried == {"heart_rate": at(hours=3)}


def test_the_measured_time_travels_with_the_value():
    values, carried = carry_forward(ROW, at(hours=1), ["systolic_bp", "consciousness"])
    assert carried == {"systolic_bp": at(hours=0), "consciousness": at(hours=0)}
    assert json.loads(dump_carried(carried)) == carried
    assert dump_carried({}) is None


@pytest.mark.parametrize("row", [
    {k: v for k, v in ROW.items() if k != "timestamp"},
    {**ROW, "timestamp": "not a time"},
])
def test_a_value_whose_age_cannot_be_shown_is_not_carried(row):
    values, carried = carry_forward(row, at(minutes=1), ["systolic_bp", "consciousness"])
    assert values == {"systolic_bp": None, "consciousness": None} and carried == {}


def test_a_blank_acvpu_is_an_absence_not_a_value():
    values, _ = carry_forward({**ROW, "consciousness": "  "}, at(minutes=1), ["consciousness"])
    assert values["consciousness"] is None


# ── the monitor feed, through the real write ────────────────────────────────

def _event(mrn: str, key: str, value: float, observed_at: str) -> CanonicalEvent:
    return CanonicalEvent(
        patient_identifier=mrn, observation_name=key, value=str(value),
        observation_type=ObservationType.VITAL, value_type=ValueType.NUMERIC,
        value_numeric=value, observed_at=observed_at, mapped_vital_key=key,
        source_type="json",
    )


@pytest.fixture
def ward_patient():
    """An admitted patient with a case room and a full nurse set charted at T0."""
    pid = f"CARRY{uuid.uuid4().hex[:8].upper()}"
    mrn = f"MRN-{pid}"
    with db_session() as db:
        db.execute("INSERT INTO patients (id, name, mrn, status) VALUES (?, ?, ?, 'admitted')",
                   (pid, f"Carry {pid}", mrn))
        db.execute("INSERT OR REPLACE INTO channels (id, name, type, patient_id) "
                   "VALUES (?, ?, 'case_room', ?)", (f"carry-room-{pid}", f"Case {pid}", pid))
        db.execute(
            """INSERT INTO vitals (patient_id, timestamp, heart_rate, systolic_bp, diastolic_bp,
                   spo2, temperature, respiratory_rate, consciousness, on_oxygen, source)
               VALUES (?, ?, 120, 78, 50, 90, 38.4, 30, 'confusion', 1, 'nurse_observation')""",
            (pid, at(hours=-11)),
        )
    yield pid, mrn
    with db_session() as db:
        db.execute("UPDATE patients SET status = 'discharged' WHERE id = ?", (pid,))


def _rows(pid):
    with db_session() as db:
        return [dict(r) for r in db.execute(
            "SELECT * FROM vitals WHERE patient_id = ? ORDER BY timestamp, id", (pid,)).fetchall()]


def _alerts(pid):
    with db_session() as db:
        return [r["content"] for r in db.execute(
            "SELECT m.content FROM messages m JOIN channels c ON c.id = m.channel_id "
            "WHERE c.patient_id = ? AND m.message_type = 'alert'", (pid,)).fetchall()]


async def test_eleven_hour_old_vitals_are_no_longer_alerted_as_current(ward_patient):
    """The review's G1 case, end to end: a nurse set at T0 (NEWS2 critical) and,
    11 h later, a monitor message carrying only HR 80 and SpO2 98. The 11-hour-old
    BP, RR and temperature are not carried. The ACVPU and oxygen assessment is,
    because 11 h is inside its ruled 12 h, so the alert still fires, on that."""
    pid, mrn = ward_patient
    await _insert_vitals(mrn, [_event(mrn, "HR", 80, at(hours=0)),
                               _event(mrn, "SpO2", 98, at(hours=0))],
                         event_id=None, source_label="ingested_json")

    row = _rows(pid)[-1]
    assert (row["systolic_bp"], row["diastolic_bp"], row["respiratory_rate"],
            row["temperature"]) == (None,) * 4, row
    assert (row["consciousness"], row["on_oxygen"]) == ("confusion", 1)
    assert json.loads(row["carried_from"]) == {"consciousness": at(hours=-11),
                                               "on_oxygen": at(hours=-11)}
    alerts = _alerts(pid)
    assert len(alerts) == 1, alerts
    assert "Altered consciousness (ACVPU)" in alerts[0], alerts[0]
    for stale in ("RR 30", "SBP 78", "tachypnea", "hypotension"):
        assert stale not in alerts[0], f"11-hour-old {stale!r} alerted as current:\n{alerts[0]}"


async def test_past_every_limit_the_old_set_raises_nothing(ward_patient):
    """13 h on, the nurse's assessment has expired too: HR 80 and SpO2 98 alone
    score 0, as an incomplete set, and nobody is paged on a T0 picture."""
    pid, mrn = ward_patient
    with db_session() as db:
        db.execute("UPDATE vitals SET timestamp = ? WHERE patient_id = ?", (at(hours=-13), pid))
    await _insert_vitals(mrn, [_event(mrn, "HR", 80, at(hours=0)),
                               _event(mrn, "SpO2", 98, at(hours=0))],
                         event_id=None, source_label="ingested_json")

    row = _rows(pid)[-1]
    assert (row["consciousness"], row["on_oxygen"], row["carried_from"]) == (None, None, None)
    assert _alerts(pid) == []


async def test_the_same_message_three_hours_on_still_carries_and_alerts(ward_patient):
    """Inside the limits nothing changes: the BP, RR and ACVPU charted 3 h ago
    are carried, scored and named, with their measured time on the row."""
    pid, mrn = ward_patient
    with db_session() as db:
        db.execute("UPDATE vitals SET timestamp = ? WHERE patient_id = ?", (at(hours=-3), pid))
    await _insert_vitals(mrn, [_event(mrn, "HR", 80, at(hours=0)),
                               _event(mrn, "SpO2", 98, at(hours=0))],
                         event_id=None, source_label="ingested_json")

    row = _rows(pid)[-1]
    assert (row["systolic_bp"], row["respiratory_rate"], row["consciousness"]) == (78, 30, "confusion")
    assert json.loads(row["carried_from"])["systolic_bp"] == at(hours=-3)
    alerts = _alerts(pid)
    assert len(alerts) == 1 and "Severe tachypnea (RR 30)" in alerts[0], alerts


def test_a_run_of_hr_only_messages_cannot_keep_an_old_bp_alive(ward_patient):
    """HR every 30 min after the T0 set. BP is carried through T0 + 4 h and is
    gone at T0 + 4.5 h, though every row before it is only 30 min old."""
    pid, mrn = ward_patient
    t0 = -11
    for half_hours in range(1, 10):                       # T0 + 0.5 h ... T0 + 4.5 h
        _persist_vitals_row(mrn, [_event(mrn, "HR", 96, at(hours=t0, minutes=30 * half_hours))],
                            event_id=None, source_label="ingested_json")
    bp = [r["systolic_bp"] for r in _rows(pid)[1:]]
    assert bp == [78] * 8 + [None], bp
    measured = [json.loads(r["carried_from"] or "{}").get("systolic_bp") for r in _rows(pid)[1:]]
    assert measured == [at(hours=t0)] * 8 + [None], "the carried BP lost its measured time"


def test_temperature_lasts_8_h_and_acvpu_and_oxygen_12_h(ward_patient):
    pid, mrn = ward_patient
    for hours_after in (5, 9, 13):
        _persist_vitals_row(mrn, [_event(mrn, "HR", 96, at(hours=-11 + hours_after))],
                            event_id=None, source_label="ingested_json")
    got = [(r["systolic_bp"], r["temperature"], r["consciousness"], r["on_oxygen"])
           for r in _rows(pid)[1:]]
    assert got == [
        (None, 38.4, "confusion", 1),     # +5 h: BP gone, temperature and ACVPU/O2 stay
        (None, None, "confusion", 1),     # +9 h: temperature gone too
        (None, None, None, None),         # +13 h: nurse assessments expired
    ], got
    scored = compute_news2(96, None, None, None, None, consciousness=None, on_oxygen=None)
    assert scored["complete"] is False   # what an expired set scores as: a minimum


# ── the simulator ────────────────────────────────────────────────────────────

def _sim_tick(latest: dict):
    from services import bot_service
    with db_session() as db:
        insert, emit = bot_service._next_reading(
            db, f"SIMCARRY{uuid.uuid4().hex[:6]}", latest, at(hours=0), on_scale_2=False)
    return insert, emit


SIM_ROW = {"heart_rate": 80.0, "systolic_bp": 120.0, "diastolic_bp": 75.0, "spo2": 97.0,
           "temperature": 37.0, "respiratory_rate": 16.0}


def test_the_simulator_stops_carrying_a_nurse_assessment_after_12_h():
    insert, emit = _sim_tick({**SIM_ROW, "timestamp": at(hours=-13),
                              "consciousness": "confusion", "on_oxygen": 1})
    assert (insert[8], insert[9], insert[10]) == (None, None, None), insert
    assert emit["risk"]["complete"] is False
    assert "Altered consciousness (ACVPU)" not in emit["risk"]["drivers"]


def test_the_simulator_carries_it_within_12_h_and_keeps_the_assessment_time():
    """The latest row is 5 s old (a previous tick) but the assessment it carries
    was charted 11 h ago; the new row must still say 11 h ago."""
    insert, emit = _sim_tick({**SIM_ROW, "timestamp": at(seconds=-5),
                              "consciousness": "confusion", "on_oxygen": 1,
                              "carried_from": json.dumps({"consciousness": at(hours=-11),
                                                          "on_oxygen": at(hours=-11)})})
    assert (insert[8], insert[9]) == ("confusion", 1)
    assert json.loads(insert[10]) == {"consciousness": at(hours=-11), "on_oxygen": at(hours=-11)}
    assert "Altered consciousness (ACVPU)" in emit["risk"]["drivers"]


def test_the_simulator_no_longer_invents_alert_and_on_air():
    insert, _ = _sim_tick({**SIM_ROW, "timestamp": at(seconds=-5)})
    assert (insert[8], insert[9]) == (None, None), (
        "a never-charted ACVPU was filled in as 'alert' / on air")


# ── writers that used to leave ACVPU and O2 to the column defaults ───────────
# vitals.consciousness defaults to 'alert' and on_oxygen to 0, so an INSERT that
# omits them stores "assessed: alert, on air". Ruled 2026-10-03: Scribe
# note vitals carry them like the feed; admission vitals store what the form
# supplied and NULL otherwise.

def _nurse_set(pid: str, hours_ago: float) -> None:
    with db_session() as db:
        db.execute("INSERT INTO patients (id, name, mrn, status) VALUES (?, ?, ?, 'admitted')",
                   (pid, f"Writer {pid}", f"MRN-{pid}"))
        db.execute("INSERT OR IGNORE INTO users (id, name, role, password_hash) "
                   "VALUES ('CARRYDOC', 'Carry Doctor', 'doctor', 'x')")
        db.execute(
            """INSERT INTO vitals (patient_id, timestamp, heart_rate, systolic_bp, spo2,
                   respiratory_rate, temperature, consciousness, on_oxygen, source)
               VALUES (?, ?, 88, 118, 94, 18, 37.0, 'confusion', 1, 'nurse_observation')""",
            (pid, at(hours=-hours_ago)),
        )


def _scribe_writes_vitals(pid: str) -> dict:
    from services import nervous_system_triggers
    nervous_system_triggers._persist_extraction(
        note_id=0, patient_id=pid, author_id="CARRYDOC",
        updates={"vitals": {"heart_rate": 92, "systolic_bp": 124}},
        med_starts=[], stop_proposals=[])
    return _rows(pid)[-1]


def test_a_scribe_note_no_longer_erases_a_nurses_recent_acvpu_and_oxygen():
    pid = f"SCRIBE{uuid.uuid4().hex[:8].upper()}"
    _nurse_set(pid, hours_ago=1)
    row = _scribe_writes_vitals(pid)
    assert row["source"] == "llm_extracted"
    assert (row["consciousness"], row["on_oxygen"]) == ("confusion", 1), (
        "the note's row reset the nurse's assessment to the column defaults")
    assert json.loads(row["carried_from"]) == {"consciousness": at(hours=-1),
                                               "on_oxygen": at(hours=-1)}


def test_a_scribe_note_stores_not_assessed_once_the_assessment_is_12_h_old():
    pid = f"SCRIBE{uuid.uuid4().hex[:8].upper()}"
    _nurse_set(pid, hours_ago=13)
    row = _scribe_writes_vitals(pid)
    assert (row["consciousness"], row["on_oxygen"], row["carried_from"]) == (None, None, None)


def _admit_row(**kwargs) -> dict:
    from services.vitals_source.manual_source import ManualEntrySource
    pid = f"ADMIT{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute("INSERT INTO patients (id, name, mrn, status) VALUES (?, ?, ?, 'admitted')",
                   (pid, f"Admit {pid}", f"MRN-{pid}"))
        ManualEntrySource(None).record_at_admission(
            db, pid, {"HR": 96, "SBP": 118, "SpO2": 95}, observed_at=at(hours=0), **kwargs)
    return _rows(pid)[-1]


def test_admission_stores_the_acvpu_and_oxygen_the_clinician_entered():
    row = _admit_row(consciousness="confusion", on_oxygen=True)
    assert (row["consciousness"], row["on_oxygen"]) == ("confusion", 1), (
        "the admission form's ACVPU was replaced by the column default 'alert'")


def test_admission_without_them_stores_not_assessed():
    row = _admit_row()
    assert (row["consciousness"], row["on_oxygen"]) == (None, None)
    scored = compute_news2(row["heart_rate"], row["systolic_bp"], None, None, row["spo2"],
                           consciousness=row["consciousness"], on_oxygen=row["on_oxygen"])
    assert scored["complete"] is False
