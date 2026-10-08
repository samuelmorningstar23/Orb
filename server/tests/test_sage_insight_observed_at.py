"""A Sage insight says when the reading it describes was taken (9 October 2026).

The card printed "At <time>: <detail>" from updated_at, which the sweep stamps
with its own clock every time it raises, rewrites or keeps a card. A set the
ward clerk copied in at 01:51 that the paper sheet timed 22:50 therefore read
"At 01:51 AM". Each insight now carries observed_at, the timestamp of the
vitals row its numbers come from, and the screen prints that; updated_at keeps
ordering and ageing the feed. Migration 80 adds the column.
"""
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone

import pytest

import config
from database import db_session
from migrations_his import m0080_sage_insight_observed_at as m80
from services.clinical_time import format_utc
from services.sage_proactive_service import evaluate_patient, get_active_insights


@pytest.fixture
def patient():
    pid = f"PAT-OBSAT-{uuid.uuid4().hex[:10]}"
    with db_session() as db:
        db.execute("DELETE FROM insight_feedback WHERE pattern IN "
                   "('trajectory:warning', 'sepsis:warning', 'stewardship:info')")
        db.execute(
            "INSERT INTO patients (id, name, mrn, diagnosis, status, ward, bed) "
            "VALUES (?, 'Observed At Patient', ?, 'Pneumonia', 'admitted', 'GM-1', 'GM-105')",
            (pid, f"MRN-{pid}"),
        )
    yield pid
    with db_session() as db:
        db.execute("DELETE FROM sage_insights WHERE patient_id = ?", (pid,))
        db.execute("DELETE FROM vitals WHERE patient_id = ?", (pid,))
        db.execute("DELETE FROM medications WHERE patient_id = ?", (pid,))
        db.execute("UPDATE patients SET status = 'discharged' WHERE id = ?", (pid,))


def _chart(pid, minutes_ago, spo2=97, hr=80, temp=37.0, rr=16):
    """A set taken `minutes_ago`, stored in the one UTC spelling every writer uses."""
    ts = format_utc(datetime.now(timezone.utc) - timedelta(minutes=minutes_ago))
    with db_session() as db:
        db.execute(
            "INSERT INTO vitals (patient_id, timestamp, heart_rate, spo2, temperature, respiratory_rate) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (pid, ts, hr, spo2, temp, rr),
        )
    return ts


def _card(pid, key_tail):
    with db_session() as db:
        row = db.execute(
            "SELECT * FROM sage_insights WHERE patient_id = ? AND dedup_key = ?",
            (pid, f"{pid}:{key_tail}"),
        ).fetchone()
    return dict(row) if row else None


def test_a_card_carries_the_time_its_reading_was_taken_not_the_sweep_time(patient):
    # A round copied in now from a sheet timed three hours ago.
    _chart(patient, 300, spo2=97)
    _chart(patient, 240, spo2=95)
    taken = _chart(patient, 180, spo2=93)
    evaluate_patient(patient)
    card = _card(patient, "trajectory:spo2")
    assert card is not None
    assert card["observed_at"] == taken
    # updated_at is the sweep's clock, hours after the reading.
    assert card["updated_at"] > taken


def test_a_refire_moves_the_time_to_the_newer_reading(patient):
    _chart(patient, 300, spo2=97)
    _chart(patient, 240, spo2=95)
    first = _chart(patient, 180, spo2=93)
    evaluate_patient(patient)
    assert _card(patient, "trajectory:spo2")["observed_at"] == first
    newer = _chart(patient, 60, spo2=90)
    evaluate_patient(patient)
    card = _card(patient, "trajectory:spo2")
    assert card["observed_at"] == newer
    assert "90%" in card["detail"]


def test_a_sepsis_card_carries_its_readings_time(patient):
    taken = _chart(patient, 150, hr=118, temp=38.6, rr=26)
    evaluate_patient(patient)
    assert _card(patient, "sepsis")["observed_at"] == taken


def test_a_deterioration_card_carries_its_readings_time(monkeypatch, patient):
    monkeypatch.setattr(config, "SCORING_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "WARD_RECORD", False, raising=False)
    taken = _chart(patient, 120, spo2=84, hr=135, temp=39.4, rr=32)
    evaluate_patient(patient)
    card = _card(patient, "deterioration:critical")
    assert card is not None
    assert card["observed_at"] == taken


def test_a_stewardship_card_describes_no_reading(patient):
    started = (datetime.now(timezone.utc) - timedelta(hours=96)).isoformat()
    with db_session() as db:
        db.execute(
            "INSERT INTO medications (patient_id, drug_name, dosage, route, frequency, status, "
            "prescribed_by, started_at) VALUES (?, 'Ceftriaxone', '2g', 'IV', 'OD', 'active', "
            "'D001', ?)", (patient, started))
    evaluate_patient(patient)
    with db_session() as db:
        row = db.execute(
            "SELECT observed_at FROM sage_insights WHERE patient_id = ? AND insight_type = 'stewardship'",
            (patient,)).fetchone()
    assert row is not None and row["observed_at"] is None


def test_the_feed_sends_observed_at(patient):
    _chart(patient, 300, spo2=97)
    _chart(patient, 240, spo2=95)
    taken = _chart(patient, 180, spo2=93)
    evaluate_patient(patient)
    feed = [i for i in get_active_insights("ADMIN-OBSAT", "admin") if i["patient_id"] == patient]
    assert feed and all("observed_at" in i for i in feed)
    assert any(i["observed_at"] == taken for i in feed)


def test_migration_80_adds_the_column_once():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE sage_insights (id TEXT PRIMARY KEY, updated_at TIMESTAMP)")
    m80.apply(conn)
    m80.apply(conn)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(sage_insights)").fetchall()]
    assert cols.count("observed_at") == 1
    empty = sqlite3.connect(":memory:")
    m80.apply(empty)  # no table: nothing to extend, no error
    assert m80.MIGRATION.version == 80
