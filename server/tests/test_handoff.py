"""Orb Sage Ambient — Stage 4 Downstream Handoff Unit Tests

Verifies that confirmed Sage actions inside Relay are properly orchestrated into
structured downstream handoffs (Helix, Scribe, Admissions/Surgeries), handling
success, failure, idempotency, and API route transitions safely.
"""
import pytest
import json
import uuid
from unittest import mock

# Explicitly import routes.ingestion_routes so it is loaded before mock.patch checks attributes
import routes.ingestion_routes

# Mock config before importing main or anything that imports config
with mock.patch("config.INGESTION_MODE", "hybrid"), \
     mock.patch("routes.ingestion_routes.INGESTION_MODE", "hybrid"), \
     mock.patch("main.INGESTION_MODE", "hybrid"):
    from fastapi.testclient import TestClient
    from main import app
    from database import db_session, get_db
    from services.sage_handoff_service import create_and_dispatch_handoff

client = TestClient(app)


def _create_user(user_id: str, name: str, role: str, password: str = "testpass123"):
    """Helper to insert a user for testing."""
    from auth import hash_password
    pwd = hash_password(password)
    with db_session() as db:
        db.execute(
            "INSERT OR IGNORE INTO users (id, name, role, password_hash) VALUES (?, ?, ?, ?)",
            (user_id, name, role, pwd),
        )
    return user_id


def _login(user_id: str, password: str = "testpass123") -> str:
    """Helper to log in and get JWT token."""
    resp = client.post("/api/auth/login", json={"employee_id": user_id, "password": password})
    assert resp.status_code == 200, f"Login failed: {resp.text}"
    return resp.json()["token"]


def _auth_header(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(autouse=True)
def setup_handoff_data():
    """Seeds isolation-ready dataset for handoff testing."""
    from services.helix_db import init_helix_db
    init_helix_db()
    
    # Ensure test user exists
    _create_user("D-HANDOFF-DOC", "Dr. Handoff Test", "doctor")
    
    with db_session() as db:
        # Clear existing test handoff records to guarantee test isolation
        db.execute("DELETE FROM sage_handoff_events")
        db.execute("DELETE FROM sage_handoffs")
        db.execute("DELETE FROM sage_action_events")
        db.execute("DELETE FROM sage_actions")
        db.execute("DELETE FROM scribe_notes")
        db.execute("DELETE FROM admissions")
        db.execute("DELETE FROM surgery_team_assignments")
        db.execute("DELETE FROM surgeries")
        
        # Insert test patients
        db.execute(
            "INSERT OR IGNORE INTO patients (id, name, mrn, diagnosis, status) VALUES (?, ?, ?, ?, 'admitted')",
            ("PAT-HANDOFF-1", "Handoff Patient One", "MRN-HANDOFF-1", "Acute Renal Failure")
        )
        db.execute(
            "INSERT OR IGNORE INTO patients (id, name, mrn, diagnosis, status) VALUES (?, ?, ?, ?, 'admitted')",
            ("PAT007", "Alex Patient", "MRN-PAT007", "Severe Sepsis")
        )
        
        # Insert test channels/rooms
        db.execute(
            "INSERT OR IGNORE INTO channels (id, name, type, patient_id) VALUES (?, ?, 'case_room', ?)",
            ("case-PAT-HANDOFF-1", "Handoff 1 Case Room", "PAT-HANDOFF-1")
        )
        db.execute(
            "INSERT OR IGNORE INTO channels (id, name, type, patient_id) VALUES (?, ?, 'case_room', ?)",
            ("case-PAT007", "PAT007 Case Room", "PAT007")
        )
        
        # Insert medication to helix catalog
        db.execute(
            "INSERT OR IGNORE INTO helix_medication_master (id, name, generic_name, form, strength) VALUES (?, ?, ?, ?, ?)",
            ("MED-ASPIRIN", "Aspirin", "Aspirin", "Tablet", "81mg")
        )
        
        # Insert stock for helix dispense checks
        db.execute(
            "INSERT OR IGNORE INTO helix_inventory_lots (id, medication_id, lot_number, quantity, expiration_date) VALUES (?, ?, ?, ?, ?)",
            ("LOT-ASPIRIN", "MED-ASPIRIN", "LOT12345", 100, "2028-12-31")
        )

        # Insert pre-seeded surgery for PAT007 (needed by the special transfer flow)
        db.execute(
            """INSERT OR IGNORE INTO surgeries 
               (id, patient_id, surgeon_id, procedure_name, diagnosis, scheduled_date, scheduled_time, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            ("SURG-ALEX-EM", "PAT007", "D-HANDOFF-DOC", "Emergency Surgery", "Severe Sepsis", "2026-05-22", "12:00", "emergency_pending")
        )
        
        # Insert active admissions
        db.execute(
            "INSERT OR IGNORE INTO admissions (id, patient_id, admitted_by, department, status) VALUES (?, ?, ?, ?, 'active')",
            ("ADM-HANDOFF-1", "PAT-HANDOFF-1", "D-HANDOFF-DOC", "Internal Medicine")
        )
        db.execute(
            "INSERT OR IGNORE INTO admissions (id, patient_id, admitted_by, department, status) VALUES (?, ?, ?, ?, 'active')",
            ("ADM-PAT007", "PAT007", "D-HANDOFF-DOC", "Internal Medicine")
        )
        
    yield
    
    with db_session() as db:
        db.execute("DELETE FROM sage_handoff_events")
        db.execute("DELETE FROM sage_handoffs")
        db.execute("DELETE FROM sage_action_events")
        db.execute("DELETE FROM sage_actions")
        db.execute("DELETE FROM scribe_notes")
        db.execute("DELETE FROM admissions")
        db.execute("DELETE FROM surgery_team_assignments")
        db.execute("DELETE FROM surgeries")


# ════════════════════════════════════════════
#  Test Cases
# ════════════════════════════════════════════

def test_create_and_dispatch_handoff_prescribe_success():
    """Verify that transition of a prescribe action creates a helix order and sets handoff status to accepted."""
    action_id = str(uuid.uuid4())
    action_dict = {
        "id": action_id,
        "room_id": "case-PAT-HANDOFF-1",
        "patient_id": "PAT-HANDOFF-1",
        "action_type": "prescribe",
        "target": "Aspirin",
        "rationale": "Chest pain, suspected acute coronary syndrome. Plan route IV.",
        "confidence": 0.98,
        "state": "confirmed",
        "version": 1
    }
    
    with db_session() as db:
        db.execute(
            """INSERT INTO sage_actions (id, room_id, patient_id, action_type, target, rationale, confidence, state, version)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (action_dict["id"], action_dict["room_id"], action_dict["patient_id"], action_dict["action_type"],
             action_dict["target"], action_dict["rationale"], action_dict["confidence"], action_dict["state"], action_dict["version"])
        )
        
    # Execute dispatch
    handoff = create_and_dispatch_handoff(action_dict, "D-HANDOFF-DOC")
    
    assert handoff["status"] == "accepted"
    assert handoff["target_module"] == "helix"
    assert handoff["downstream_ref_id"] is not None
    
    # Authoritatively assert Helix state
    with db_session() as db:
        order = db.execute("SELECT * FROM helix_patient_med_orders WHERE id = ?", (handoff["downstream_ref_id"],)).fetchone()
        assert order is not None
        assert order["patient_id"] == "PAT-HANDOFF-1"
        # Human-in-the-loop dosing: agentic orders no longer fabricate a "TBD"
        # placeholder dose — they flag it for clinician entry.
        assert order["dosage"] == "REVIEW REQUIRED"
        assert order["route"] == "IV"  # Parsed from IV in rationale
        assert order["status"] == "pending"

    # The med charted to the patient's chart carries llm_extracted provenance.
    with db_session() as db:
        med = db.execute(
            "SELECT source, dosage FROM medications WHERE patient_id = 'PAT-HANDOFF-1' "
            "AND drug_name = 'Aspirin' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert med is not None
        assert med["source"] == "llm_extracted"
        
        # Verify event logging
        events = db.execute("SELECT * FROM sage_handoff_events WHERE handoff_id = ? ORDER BY id ASC", (handoff["id"],)).fetchall()
        assert len(events) >= 2
        assert events[0]["new_status"] == "pending"
        assert events[1]["new_status"] == "accepted"


def test_create_and_dispatch_handoff_document_success():
    """Verify that transition of a document action creates a Scribe note and sets status to accepted."""
    action_id = str(uuid.uuid4())
    action_dict = {
        "id": action_id,
        "room_id": "case-PAT-HANDOFF-1",
        "patient_id": "PAT-HANDOFF-1",
        "action_type": "document",
        "target": "Cardiology Consultation soap notes",
        "rationale": "Patient presented with heart failure flare up. Adjusted beta blocker dosing.",
        "confidence": 0.95,
        "state": "confirmed",
        "version": 1
    }
    
    with db_session() as db:
        db.execute(
            """INSERT INTO sage_actions (id, room_id, patient_id, action_type, target, rationale, confidence, state, version)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (action_dict["id"], action_dict["room_id"], action_dict["patient_id"], action_dict["action_type"],
             action_dict["target"], action_dict["rationale"], action_dict["confidence"], action_dict["state"], action_dict["version"])
        )
        
    handoff = create_and_dispatch_handoff(action_dict, "D-HANDOFF-DOC")
    
    assert handoff["status"] == "accepted"
    assert handoff["target_module"] == "scribe"
    assert handoff["downstream_ref_id"] is not None
    
    with db_session() as db:
        note = db.execute("SELECT * FROM scribe_notes WHERE id = ?", (handoff["downstream_ref_id"],)).fetchone()
        assert note is not None
        assert note["patient_id"] == "PAT-HANDOFF-1"
        assert note["author_id"] == "D-HANDOFF-DOC"
        assert "Cardiology Consultation" in note["raw_transcript"]
        assert note["status"] == "draft"


def test_create_and_dispatch_handoff_transfer_success():
    """Verify that transition of a transfer action updates active admission and schedules surgery."""
    action_id = str(uuid.uuid4())
    action_dict = {
        "id": action_id,
        "room_id": "case-PAT-HANDOFF-1",
        "patient_id": "PAT-HANDOFF-1",
        "action_type": "transfer_to_surgery",
        "target": "General Surgery",
        "rationale": "Suspected bowel perforation. Needs laparotomy.",
        "confidence": 0.99,
        "state": "confirmed",
        "version": 1
    }
    
    with db_session() as db:
        db.execute(
            """INSERT INTO sage_actions (id, room_id, patient_id, action_type, target, rationale, confidence, state, version)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (action_dict["id"], action_dict["room_id"], action_dict["patient_id"], action_dict["action_type"],
             action_dict["target"], action_dict["rationale"], action_dict["confidence"], action_dict["state"], action_dict["version"])
        )
        
    handoff = create_and_dispatch_handoff(action_dict, "D-HANDOFF-DOC")
    
    assert handoff["status"] == "accepted"
    assert handoff["target_module"] == "admissions"
    assert handoff["downstream_ref_id"] is not None
    
    with db_session() as db:
        # Check active admission is updated
        adm = db.execute("SELECT * FROM admissions WHERE patient_id = ? AND status = 'active'", ("PAT-HANDOFF-1",)).fetchone()
        assert adm["department"] == "General Surgery"
        
        # Check surgical case is scheduled
        surg = db.execute("SELECT * FROM surgeries WHERE id = ?", (handoff["downstream_ref_id"],)).fetchone()
        assert surg is not None
        # The card's target names a department, not an operation, so the
        # procedure is left for the surgeon rather than guessed from the
        # rationale (F017).
        assert surg["procedure_name"] == "To be confirmed"
        assert surg["status"] == "emergency_pending"
        
        # Check system message in room
        msg = db.execute("SELECT * FROM messages WHERE channel_id = ? ORDER BY timestamp DESC LIMIT 1", ("case-PAT-HANDOFF-1",)).fetchone()
        assert msg is not None
        assert "confirmed and initiated" in msg["content"]


def test_create_and_dispatch_handoff_transfer_special_patient_007():
    """Verify that emergency transfer for patient PAT007 transitions the pre-seeded surgery to emergency_ready."""
    action_id = str(uuid.uuid4())
    action_dict = {
        "id": action_id,
        "room_id": "case-PAT007",
        "patient_id": "PAT007",
        "action_type": "transfer_to_surgery",
        "target": "General Surgery",
        "rationale": "Escalated for emergency laparotomy and hartmann's procedure.",
        "confidence": 0.99,
        "state": "confirmed",
        "version": 1
    }
    
    with db_session() as db:
        db.execute(
            """INSERT INTO sage_actions (id, room_id, patient_id, action_type, target, rationale, confidence, state, version)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (action_dict["id"], action_dict["room_id"], action_dict["patient_id"], action_dict["action_type"],
             action_dict["target"], action_dict["rationale"], action_dict["confidence"], action_dict["state"], action_dict["version"])
        )
        
    handoff = create_and_dispatch_handoff(action_dict, "D-HANDOFF-DOC")
    
    assert handoff["status"] == "accepted"
    assert handoff["downstream_ref_id"] == "SURG-ALEX-EM"
    
    with db_session() as db:
        surg = db.execute("SELECT * FROM surgeries WHERE id = ?", ("SURG-ALEX-EM",)).fetchone()
        assert surg["status"] == "emergency_ready"


def test_create_and_dispatch_handoff_prescribe_noncatalog_still_charts():
    """v2 contract: a confirmed med order for a drug NOT in the Helix formulary is
    still CHARTED to the patient's active meds (so the clinician's order is never
    lost) — only the Helix pharmacy order is skipped for pharmacy to reconcile.
    The handoff is therefore 'accepted', and the action stays 'confirmed'."""
    action_id = str(uuid.uuid4())
    action_dict = {
        "id": action_id,
        "room_id": "case-PAT-HANDOFF-1",
        "patient_id": "PAT-HANDOFF-1",
        "action_type": "prescribe",
        "target": "NonExistentMedication 500mg",
        "rationale": "Uncatalogued drug.",
        "confidence": 0.90,
        "state": "confirmed",
        "version": 1
    }
    
    with db_session() as db:
        db.execute(
            """INSERT INTO sage_actions (id, room_id, patient_id, action_type, target, rationale, confidence, state, version)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (action_dict["id"], action_dict["room_id"], action_dict["patient_id"], action_dict["action_type"],
             action_dict["target"], action_dict["rationale"], action_dict["confidence"], action_dict["state"], action_dict["version"])
        )
        
    handoff = create_and_dispatch_handoff(action_dict, "D-HANDOFF-DOC")

    # Charted (accepted), Helix order skipped (best-effort) -> ref marks it.
    assert handoff["status"] == "accepted"
    assert handoff["downstream_ref_id"] == "charted-only"

    with db_session() as db:
        # The med IS now on the patient's chart (visible in Vigil).
        med = db.execute(
            "SELECT * FROM medications WHERE patient_id = ? AND drug_name = ?",
            ("PAT-HANDOFF-1", "NonExistentMedication 500mg"),
        ).fetchone()
        assert med is not None and med["status"] == "active"
        # Action state remains "confirmed" and is NOT reverted/disrupted.
        action = db.execute("SELECT * FROM sage_actions WHERE id = ?", (action_id,)).fetchone()
        assert action["state"] == "confirmed"


def test_create_and_dispatch_handoff_unsupported_action():
    """Verify that an unsupported action type falls back to status unsupported gracefully."""
    action_id = str(uuid.uuid4())
    # 'escalate' is in CHECK constraint but not processed by the handoff service
    action_dict = {
        "id": action_id,
        "room_id": "case-PAT-HANDOFF-1",
        "patient_id": "PAT-HANDOFF-1",
        "action_type": "escalate",
        "target": "Chief Medical Officer",
        "rationale": "High-level review.",
        "confidence": 0.92,
        "state": "confirmed",
        "version": 1
    }
    
    with db_session() as db:
        db.execute(
            """INSERT INTO sage_actions (id, room_id, patient_id, action_type, target, rationale, confidence, state, version)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (action_dict["id"], action_dict["room_id"], action_dict["patient_id"], action_dict["action_type"],
             action_dict["target"], action_dict["rationale"], action_dict["confidence"], action_dict["state"], action_dict["version"])
        )
        
    handoff = create_and_dispatch_handoff(action_dict, "D-HANDOFF-DOC")

    assert handoff["status"] == "unsupported"
    assert handoff["error_summary"].startswith("Needs manual dispatch")


def _confirm_card(action_type: str, target: str, rationale: str = "Ward round.") -> dict:
    """A confirmed sage_actions card for PAT-HANDOFF-1, dispatched as Relay would."""
    action_dict = {
        "id": str(uuid.uuid4()),
        "room_id": "case-PAT-HANDOFF-1",
        "patient_id": "PAT-HANDOFF-1",
        "action_type": action_type,
        "target": target,
        "rationale": rationale,
        "confidence": 0.6,
        "state": "confirmed",
        "version": 1,
    }
    with db_session() as db:
        db.execute(
            """INSERT INTO sage_actions (id, room_id, patient_id, action_type, target, rationale, confidence, state, version)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (action_dict["id"], action_dict["room_id"], action_dict["patient_id"], action_type,
             target, rationale, 0.6, "confirmed", 1),
        )
    return create_and_dispatch_handoff(action_dict, "D-HANDOFF-DOC")


def _med_rows(drug: str) -> tuple:
    """(rows charted for `drug`, Helix orders) for PAT-HANDOFF-1."""
    with db_session() as db:
        charted = db.execute(
            "SELECT COUNT(*) FROM medications WHERE patient_id = 'PAT-HANDOFF-1' AND drug_name = ?",
            (drug,)).fetchone()[0]
        ordered = db.execute(
            "SELECT COUNT(*) FROM helix_patient_med_orders WHERE patient_id = 'PAT-HANDOFF-1'"
        ).fetchone()[0]
    return charted, ordered


@pytest.mark.parametrize("verb,target", [
    ("start monitoring", "Fluid balance"),      # "start" made it a pharmacy order
    ("address concerns", "Family meeting"),     # "add" inside "address"
    ("hold the line", "Bed manager"),           # "hold"
])
def test_a_verb_that_only_contains_a_medication_word_reaches_no_module(verb, target):
    """The substring router sent these to Helix: a 'start monitoring' card charted
    'Fluid balance' as an active medication with a REVIEW REQUIRED dose."""
    before = _med_rows(target)
    handoff = _confirm_card(verb, target)

    assert handoff["status"] == "unsupported"
    assert handoff["target_module"] == "unsupported"
    assert handoff["error_summary"].startswith("Needs manual dispatch")
    assert _med_rows(target) == before, "nothing may be charted or ordered"


def test_a_stop_card_never_starts_the_drug():
    """`_dispatch_to_helix` only STARTS a drug. Routed there by "stop", confirming
    "stop warfarin" charted warfarin as a NEW active medication and ordered it."""
    before = _med_rows("Warfarin")
    handoff = _confirm_card("stop_medication", "Warfarin", "INR 6.2, hold anticoagulation.")

    assert handoff["status"] == "unsupported"
    assert "stop medication" in handoff["error_summary"]
    assert _med_rows("Warfarin") == before


def test_a_bare_transfer_books_no_surgery():
    """"transfer" alone could mean the ICU; only transfer_to_surgery books theatre."""
    handoff = _confirm_card("transfer", "ICU")

    assert handoff["status"] == "unsupported"
    with db_session() as db:
        assert db.execute(
            "SELECT COUNT(*) FROM surgeries WHERE patient_id = 'PAT-HANDOFF-1'").fetchone()[0] == 0


def test_a_spelling_variant_of_a_table_verb_still_routes():
    """'Start medication' and 'start_medication' are the same verb."""
    handoff = _confirm_card("Start Medication", "NonExistentMedication 250mg")
    assert handoff["target_module"] == "helix"
    assert handoff["status"] == "accepted"


def test_create_and_dispatch_handoff_idempotency():
    """Verify that repeated handoff dispatch requests return the existing record and do not duplicate downstreams."""
    action_id = str(uuid.uuid4())
    action_dict = {
        "id": action_id,
        "room_id": "case-PAT-HANDOFF-1",
        "patient_id": "PAT-HANDOFF-1",
        "action_type": "document",
        "target": "Consultation SOAP",
        "rationale": "Routine visit notes.",
        "confidence": 0.95,
        "state": "confirmed",
        "version": 1
    }
    
    with db_session() as db:
        db.execute(
            """INSERT INTO sage_actions (id, room_id, patient_id, action_type, target, rationale, confidence, state, version)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (action_dict["id"], action_dict["room_id"], action_dict["patient_id"], action_dict["action_type"],
             action_dict["target"], action_dict["rationale"], action_dict["confidence"], action_dict["state"], action_dict["version"])
        )
        
    handoff1 = create_and_dispatch_handoff(action_dict, "D-HANDOFF-DOC")
    assert handoff1["status"] == "accepted"
    
    handoff2 = create_and_dispatch_handoff(action_dict, "D-HANDOFF-DOC")
    
    # Assert that the exact same record is returned
    assert handoff1["id"] == handoff2["id"]
    assert handoff1["downstream_ref_id"] == handoff2["downstream_ref_id"]
    
    # Assert only 1 scribe note was created
    with db_session() as db:
        notes = db.execute("SELECT * FROM scribe_notes WHERE patient_id = ?", ("PAT-HANDOFF-1",)).fetchall()
        assert len(notes) == 1


def test_relay_routes_transition_endpoint():
    """Verify that the transition API endpoint successfully triggers downstream handoffs on confirmation."""
    token = _login("D-HANDOFF-DOC")
    
    action_id = str(uuid.uuid4())
    with db_session() as db:
        db.execute(
            """INSERT INTO sage_actions (id, room_id, patient_id, action_type, target, rationale, confidence, state, version)
               VALUES (?, 'case-PAT-HANDOFF-1', 'PAT-HANDOFF-1', 'document', 'Consult SOAP', 'Dosing check', 0.95, 'suggested', 1)"""
        , (action_id,))
        
    resp = client.post(
        f"/api/relay/channels/case-PAT-HANDOFF-1/sage_actions/{action_id}/transition",
        headers=_auth_header(token),
        json={
            "state": "confirmed",
            "version": 1,
            "reason": "Doctor confirmed clinical documentation need"
        }
    )
    
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["action"]["state"] == "confirmed"
    assert data["handoff"]["status"] == "accepted"
    assert data["handoff"]["target_module"] == "scribe"
    
    with db_session() as db:
        note_id = data["handoff"]["downstream_ref_id"]
        note = db.execute("SELECT * FROM scribe_notes WHERE id = ?", (note_id,)).fetchone()
        assert note is not None
        assert note["raw_transcript"] == "Sage Ambient Observation: Consult SOAP. Dosing check"
