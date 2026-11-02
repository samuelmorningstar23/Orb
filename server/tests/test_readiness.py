"""Readiness reports only database usability; liveness remains independent."""
from contextlib import contextmanager
import asyncio
import sqlite3
import threading

from fastapi.testclient import TestClient
import pytest

import database
import main
from db_backend import DBOperationalError
from migrations import MIGRATIONS


@pytest.fixture
def probe_db(monkeypatch):
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.execute("CREATE TABLE users (id TEXT)")
    conn.execute("CREATE TABLE patients (id TEXT, status TEXT)")
    conn.execute("CREATE TABLE schema_migrations (version INTEGER)")
    conn.executemany("INSERT INTO schema_migrations VALUES (?)",
                     [(migration.version,) for migration in MIGRATIONS])
    conn.commit()

    @contextmanager
    def session():
        yield conn

    monkeypatch.setattr(database, "db_session", session)
    yield conn
    conn.close()


def test_empty_database_is_ready_without_models_or_a_session(probe_db, monkeypatch):
    monkeypatch.setattr("config.MODELS_ENABLED", False)
    response = TestClient(main.app).get("/api/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ready"}


@pytest.mark.parametrize("fault", ["missing_table", "missing_column", "missing_ledger", "missing_migration"])
def test_required_schema_failures_make_the_appliance_not_ready(probe_db, fault):
    if fault == "missing_table":
        probe_db.execute("DROP TABLE patients")
    elif fault == "missing_column":
        probe_db.execute("ALTER TABLE patients RENAME COLUMN status TO broken_status")
    elif fault == "missing_ledger":
        probe_db.execute("DROP TABLE schema_migrations")
    else:
        probe_db.execute("DELETE FROM schema_migrations WHERE version = ?",
                         (MIGRATIONS[-1].version,))
    response = TestClient(main.app).get("/api/ready")
    assert response.status_code == 503
    assert response.json() == {"status": "not_ready", "error": "database_unavailable"}


def test_database_failure_does_not_disclose_paths_errors_or_phi(monkeypatch):
    private = "/private/hospital/records.db patient Jane Confidential MRN-SECRET"

    @contextmanager
    def unavailable():
        raise DBOperationalError[0](private)
        yield

    monkeypatch.setattr(database, "db_session", unavailable)
    response = TestClient(main.app).get("/api/ready")
    assert response.status_code == 503
    assert response.json() == {"status": "not_ready", "error": "database_unavailable"}
    assert "private" not in response.text
    assert "Confidential" not in response.text
    assert "MRN-SECRET" not in response.text


def test_readiness_never_returns_record_contents_or_writes(probe_db):
    secret = "Jane Confidential MRN-SECRET"
    probe_db.execute("INSERT INTO users VALUES (?)", (secret,))
    probe_db.execute("INSERT INTO patients VALUES (?, 'admitted')", (secret,))
    statements = []
    probe_db.set_trace_callback(statements.append)
    response = TestClient(main.app).get("/api/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ready"}
    assert secret not in response.text
    assert statements and all(sql.lstrip().upper().startswith("SELECT") for sql in statements)


def test_readiness_probe_runs_off_the_event_loop_and_liveness_does_not_probe(monkeypatch):
    test_thread = threading.get_ident()
    worker_threads = []
    worker_has_loop = []

    def probe():
        worker_threads.append(threading.get_ident())
        try:
            asyncio.get_running_loop()
            worker_has_loop.append(True)
        except RuntimeError:
            worker_has_loop.append(False)

    monkeypatch.setattr(main, "_check_database_readiness", probe)
    client = TestClient(main.app)
    assert client.get("/api/health").status_code == 200
    assert worker_threads == []
    assert client.get("/api/ready").status_code == 200
    assert len(worker_threads) == 1 and worker_threads[0] != test_thread
    assert worker_has_loop == [False]


def test_readiness_is_not_a_patient_surface_under_the_facility_fence(probe_db, monkeypatch):
    async def multiple_facilities():
        return True

    monkeypatch.setattr("services.facility_service.multi_facility_data_async", multiple_facilities)
    response = TestClient(main.app).get("/api/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ready"}
