"""Pulse's record of external pulls is durable (F152).

Every call Pulse makes to the internet (weather, air quality, FDA, CDC) was
logged to an in-memory list capped at 500: gone at every restart, and after
500 calls. It is now an audit row on the hash chain, written on the DB lane
when the fetcher runs on the event loop, and the audit-log route reads it back.
"""
import asyncio
import uuid

from database import db_session, verify_audit_chain
from services import pulse_service


def _rows_for(source: str):
    with db_session() as db:
        return db.execute(
            "SELECT outcome, target_id, metadata_json FROM audit_logs "
            "WHERE action_type = ? AND target_id = ? ORDER BY id",
            (pulse_service.EGRESS_AUDIT_ACTION, source),
        ).fetchall()


def test_a_pull_logged_off_the_loop_is_an_audit_row():
    source = f"test/feed-{uuid.uuid4().hex[:6]}"
    pulse_service._log_ingestion(source, "https://example.invalid/x", "OK", "abc123")
    rows = _rows_for(source)
    assert len(rows) == 1
    assert rows[0]["outcome"] == "success"
    assert "abc123" in rows[0]["metadata_json"]


def test_a_pull_logged_on_the_loop_is_written_through_the_db_lane():
    source = f"test/feed-{uuid.uuid4().hex[:6]}"

    async def fetcher():
        pulse_service._log_ingestion(source, "https://example.invalid/y", "ERROR: timeout")
        # Let the scheduled run_db write finish before the loop closes.
        await asyncio.gather(*list(pulse_service._PENDING_EGRESS_AUDITS))

    asyncio.run(fetcher())
    rows = _rows_for(source)
    assert len(rows) == 1 and rows[0]["outcome"] == "failure"


def test_the_audit_log_route_reads_the_durable_record():
    source = f"test/feed-{uuid.uuid4().hex[:6]}"
    pulse_service._log_ingestion(source, "https://example.invalid/z", "OK", "def456")
    # A restart empties the in-memory list; the record survives.
    pulse_service._pulse_log.clear()
    entries = pulse_service.get_ingestion_audit_log(limit=200)
    mine = [e for e in entries if e.get("source") == source]
    assert mine and mine[-1]["data_hash"] == "def456"
    assert mine[-1]["direction"] == "INBOUND_ONLY"


def test_the_chain_still_verifies():
    pulse_service._log_ingestion(f"test/feed-{uuid.uuid4().hex[:6]}", "https://example.invalid", "OK")
    assert verify_audit_chain()["ok"] is True
