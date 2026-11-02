"""Demo housekeeping must not delete real or mixed-origin clinical history."""
from datetime import datetime, timedelta, timezone

import pytest

import database
from database import db_session
from services import bot_service


@pytest.fixture
def isolated_simulator_db(monkeypatch, tmp_path):
    # Pruning is database-wide: use a fresh store so testing a tiny simulated
    # row cap cannot truncate fixtures owned by any other test module.
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "simulator.db")
    database.init_db()
    return "PAT-RETENTION"


def test_cleanup_keeps_risk_history_and_real_vitals_while_pruning_demo_rows(
        isolated_simulator_db, monkeypatch):
    pid = isolated_simulator_db
    monkeypatch.setattr(bot_service, "MAX_VITALS_PER_PATIENT", 3)
    monkeypatch.setattr(bot_service, "PRUNE_EVERY_N_CYCLES", 1)
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with db_session() as db:
        db.execute("INSERT INTO patients (id, name, mrn, ward, status) "
                   "VALUES (?, 'Retention Test', 'MRN-RETENTION', 'WARD', 'admitted')",
                   (pid,))
        db.executemany(
            "INSERT INTO risk_scores (patient_id, timestamp, score, level) VALUES (?, ?, 0, 'stable')",
            [(pid, (start + timedelta(minutes=i)).isoformat()) for i in range(150)],
        )
        for source in ("manual", "ingested_hl7", "simulated"):
            db.executemany(
                "INSERT INTO vitals (patient_id, timestamp, source, heart_rate, systolic_bp, "
                "diastolic_bp, spo2, temperature, respiratory_rate, consciousness, on_oxygen) "
                "VALUES (?, ?, ?, 80, 120, 70, 98, 37, 16, 'alert', 0)",
                [(pid, (start + timedelta(minutes=i)).isoformat(), source) for i in range(6)],
            )
        risk_ids = {row["id"] for row in db.execute(
            "SELECT id FROM risk_scores WHERE patient_id = ?", (pid,))}
        real_vital_ids = {row["id"] for row in db.execute(
            "SELECT id FROM vitals WHERE patient_id = ? AND source != 'simulated'", (pid,))}

    bot_service._generate_vitals((start + timedelta(days=1)).isoformat(), 1)

    with db_session() as db:
        assert {row["id"] for row in db.execute(
            "SELECT id FROM risk_scores WHERE patient_id = ?", (pid,))} == risk_ids
        assert len(risk_ids) == 150, "exercise history beyond the retired 100-row cap"
        assert {row["id"] for row in db.execute(
            "SELECT id FROM vitals WHERE patient_id = ? AND source != 'simulated'", (pid,))} == real_vital_ids
        assert db.execute("SELECT COUNT(*) FROM vitals WHERE patient_id = ? AND source = 'simulated'",
                          (pid,)).fetchone()[0] == 3
