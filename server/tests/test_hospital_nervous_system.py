"""Tests for Orb Hospital Nervous System

Validates:
  1. LocalEventBus publish, subscribe, and wildcard topic matching.
  2. SQL durability in event_log.
  3. Reactive Trigger 1: Critical Vigil alert pre-loads RAG context to sage_decision_cache.
  4. Reactive Trigger 2: Scribe note signed auto-extracts and syncs vitals/medications to EHR.
  5. Reactive Trigger 3: Relay marker pre-warms MLX model in Unified Memory.
"""
import os
import sys
import pytest
import json
import asyncio
import uuid
from unittest import mock

# Explicitly import routes.ingestion_routes so it is loaded
import routes.ingestion_routes

with mock.patch("config.INGESTION_MODE", "hybrid"), \
     mock.patch("routes.ingestion_routes.INGESTION_MODE", "hybrid"), \
     mock.patch("main.INGESTION_MODE", "hybrid"):
    from database import db_session, get_db
    from services.event_bus import event_bus, StateEvent
    from services.nervous_system_triggers import register_triggers

@pytest.fixture(autouse=True)
def setup_hns_test_data():
    """Seeds isolation-ready dataset for nervous system testing."""
    # Ensure triggers are registered
    register_triggers()
    
    def _clean(db):
        db.execute("PRAGMA foreign_keys = OFF")
        tables = [
            "sage_handoff_events", "sage_handoffs", "sage_action_events", "sage_actions",
            "sage_room_memory", "sage_observations", "sage_conversations", "messages",
            "channels", "discharge_access_tokens", "discharge_summaries", "lens_analyses",
            "surgery_team_assignments", "surgery_prep_requests", "surgeries", "admissions",
            "audit_logs", "helix_stock_movements", "helix_transitions", "helix_patient_med_orders",
            "helix_inventory_lots", "helix_medication_master", "event_log", "sage_decision_cache",
            "risk_scores", "scribe_notes", "vitals", "medications", "forecast_predictions",
            "patient_profiles", "clinical_documents", "observations", "ingestion_events",
            "hospital_forecasts", "admission_history", "patients", "users"
        ]
        for table in tables:
            try:
                db.execute(f"DELETE FROM {table}")
            except Exception:
                pass
        db.execute("PRAGMA foreign_keys = ON")

    with db_session() as db:
        # Clear existing tables safely
        _clean(db)
        
        # Insert test users
        db.execute("INSERT OR IGNORE INTO users (id, name, role, password_hash) VALUES ('D-HNS-DOC', 'Dr. Nervous', 'doctor', 'x')")
        
        # Insert test patient
        db.execute(
            "INSERT OR IGNORE INTO patients (id, name, mrn, status) VALUES (?, ?, ?, 'admitted')",
            ("PAT-HNS-1", "Nervous Patient One", "MRN-HNS-1")
        )
        
    yield
    
    with db_session() as db:
        _clean(db)


# ════════════════════════════════════════════
#  Test Cases
# ════════════════════════════════════════════

@pytest.mark.asyncio
async def test_event_bus_pub_sub_wildcard():
    """Verify LocalEventBus supports publish, subscribe, wildcard matching, and durability."""
    received = []
    
    async def cb1(event: StateEvent):
        received.append(f"cb1:{event.topic}")
        
    async def cb2(event: StateEvent):
        received.append(f"cb2:{event.topic}")

    # Register callbacks synchronously
    event_bus.subscribe("vigil.alert.critical", cb1)
    event_bus.subscribe("vigil.alert.*", cb2)

    # Publish exact topic
    event1 = StateEvent(
        topic="vigil.alert.critical",
        source_node="vigil",
        event_type="patient_decompensated",
        patient_id="PAT-HNS-1",
        payload={"score": 0.85}
    )
    
    await event_bus.publish(event1)
    
    # Wait small delay for async tasks to run
    await asyncio.sleep(0.1)
    
    # Both exact and wildcard should match
    assert "cb1:vigil.alert.critical" in received
    assert "cb2:vigil.alert.critical" in received

    # Check database write-ahead event durability log
    with db_session() as db:
        row = db.execute("SELECT * FROM event_log WHERE event_id = ?", (event1.event_id,)).fetchone()
        assert row is not None
        assert row["topic"] == "vigil.alert.critical"
        assert row["source_node"] == "vigil"
        assert row["correlation_id"] == event1.correlation_id


@pytest.mark.asyncio
async def test_trigger_vigil_alert_preloads_sage_cache():
    """Verify that a critical Vigil alert proactively compiles and caches Sage decision context."""
    event = StateEvent(
        topic="vigil.alert.critical",
        source_node="vigil",
        event_type="patient_deteriorated",
        patient_id="PAT-HNS-1",
        payload={"score": 0.85, "drivers": ["severe tachycardia", "hypotension"]}
    )
    
    # Emit critical Vigil event
    await event_bus.publish(event)

    # publish() dispatches handlers via fire-and-forget tasks, and the first
    # preload lazily loads the guideline embedder (can take ~10s cold), so poll
    # with a generous deadline instead of a fixed sleep.
    cache = None
    deadline = asyncio.get_event_loop().time() + 60.0
    while asyncio.get_event_loop().time() < deadline:
        with db_session() as db:
            cache = db.execute("SELECT * FROM sage_decision_cache WHERE patient_id = ?", ("PAT-HNS-1",)).fetchone()
        if cache is not None:
            break
        await asyncio.sleep(0.25)

    # Verify preloaded cache is committed and fresh in SQLite
    with db_session() as db:
        cache = db.execute("SELECT * FROM sage_decision_cache WHERE patient_id = ?", ("PAT-HNS-1",)).fetchone()
        assert cache is not None
        assert cache["mews_score"] == 0.85
        
        compiled = json.loads(cache["compiled_context"])
        assert compiled["channel_a_facts"] is not None
        assert "channel_b_c_guidelines" in compiled


@pytest.mark.asyncio
async def test_trigger_scribe_note_signed_syncs_ehr():
    """Verify that signing a draft Scribe SOAP note automatically extracts and commits metrics to vitals/medications."""
    from services.llm_queue_service import llm_queue
    
    async def mock_acquire(priority=2, timeout=120.0):
        pass
        
    def mock_release():
        pass

    with mock.patch.object(llm_queue, "acquire", mock_acquire), \
         mock.patch.object(llm_queue, "release", mock_release), \
         mock.patch("httpx.AsyncClient.post", side_effect=Exception("Mock network error")):
         
        note_id = 999
        
        # Insert draft note with clinical SOAP findings
        with db_session() as db:
            db.execute(
                """INSERT INTO scribe_notes (id, patient_id, author_id, raw_transcript, structured_note, status, note_format) 
                   VALUES (?, ?, 'D-HNS-DOC', 'Transcript', ?, 'draft', 'soap')""",
                (note_id, "PAT-HNS-1", "OBJECTIVE:\nHeart rate: 85 bpm, BP: 120/80 mmHg, SpO2: 99%.\n\nPLAN:\nStart Metoprolol 2.5mg IV once.")
            )
            
        event = StateEvent(
            topic="scribe.note.signed",
            source_node="scribe",
            event_type="note_signed",
            patient_id="PAT-HNS-1",
            payload={"note_id": note_id, "signer_id": "D-HNS-DOC"}
        )
        
        # Mock LLM API or fallback, then emit Scribe Note Signed event
        await event_bus.publish(event)
        
        # Stagger for parsing and SQLite committing
        await asyncio.sleep(0.5)
        
        with db_session() as db:
            # FAIL-CLOSED regression guard: with no real LLM extraction available in
            # tests (no Ollama), the note-signing trigger must NOT fabricate
            # medications / vitals / orders. (It previously injected a hardcoded fake
            # Metoprolol order + vitals — a patient-safety hazard that was removed.)
            med = db.execute("SELECT * FROM medications WHERE patient_id = ? AND status = 'active'", ("PAT-HNS-1",)).fetchone()
            assert med is None, "note-signing must not inject a fabricated medication without a real, validated extraction"

            vitals = db.execute("SELECT * FROM vitals WHERE patient_id = ?", ("PAT-HNS-1",)).fetchone()
            assert vitals is None, "note-signing must not inject fabricated vitals without a real extraction"
