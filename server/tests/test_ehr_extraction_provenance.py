"""Signed-note auto-extraction provenance (nervous_system_triggers.on_scribe_note_signed).

Stops provenance laundering on the autonomous EHR-sync path:
  - 'start' actions are charted as UNVERIFIED, source='llm_extracted' rows
    (verified_by NULL) so they land in the pharmacist verification queue —
    never as trusted clinician orders with invented doses.
  - A severe safety alert (MedicationSafetyBlocked, non-interactive path) does
    NOT activate the med silently and does NOT drop it silently either: it is
    still charted unverified with a '[SAFETY]' note for the pharmacist.
  - 'stop' actions are never executed autonomously: the trigger records a
    clinical_documents proposal (+ Relay case-room message when a channel
    exists); the existing med row is left untouched for a human to action.

The LLM call is isolated in `_extract_updates`, stubbed here so each test
controls the extraction payload deterministically.
"""
import uuid

import pytest

from database import db_session
from services import nervous_system_triggers as nst
from services import pharmacy_verification_service as pharmacy
from services.clinical_records_service import add_allergy
from services.event_bus import StateEvent


# ── fixtures / helpers ────────────────────────────────────────────────────────
@pytest.fixture()
def patient():
    pid = f"PATEXT{uuid.uuid4().hex[:6].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO patients (id, name, mrn, status) VALUES (?, ?, ?, 'admitted')",
            (pid, "Extraction Test", f"MRN-{uuid.uuid4().hex[:6]}"),
        )
    return pid


def _seed_note(patient_id, author_id="D001"):
    """Insert a signed SOAP note; returns its id. Content is irrelevant — the
    extraction itself is stubbed per test."""
    with db_session() as db:
        cur = db.execute(
            """INSERT INTO scribe_notes (patient_id, author_id, raw_transcript, structured_note, status, note_format)
               VALUES (?, ?, 'Transcript', 'PLAN:\nSee stubbed extraction.', 'signed', 'soap')""",
            (patient_id, author_id),
        )
        return cur.lastrowid


def _stub_extraction(monkeypatch, updates: dict):
    async def _fake_extract(note_id, patient_id, structured_text):
        return updates
    monkeypatch.setattr(nst, "_extract_updates", _fake_extract)


def _event(patient_id, note_id):
    return StateEvent(
        topic="scribe.note.signed",
        source_node="scribe",
        event_type="note_signed",
        patient_id=patient_id,
        payload={"note_id": note_id, "signer_id": "D001"},
    )


def _med(patient_id, drug):
    with db_session() as db:
        return db.execute(
            "SELECT * FROM medications WHERE patient_id = ? AND drug_name = ?",
            (patient_id, drug),
        ).fetchone()


# ── 'start' → unverified llm_extracted row in the pharmacist queue ────────────
async def test_start_charts_unverified_llm_extracted_med(patient, monkeypatch):
    note_id = _seed_note(patient)
    _stub_extraction(monkeypatch, {
        "medications": [
            {"drug_name": "Paracetamol", "action": "start",
             "dosage": "500mg", "frequency": "QDS", "route": "Oral"},
        ],
        "vitals": {}, "problems": [],
    })

    await nst.on_scribe_note_signed(_event(patient, note_id))

    row = _med(patient, "Paracetamol")
    assert row is not None
    assert row["source"] == "llm_extracted"
    assert row["verified_by"] is None
    assert row["dosage"] == "500mg"
    assert row["frequency"] == "QDS"
    assert row["prescribed_by"] == "D001"

    # It enters the pharmacist verification queue.
    assert row["id"] in [p["id"] for p in pharmacy.list_pending_verification()]


async def test_start_without_llm_dose_uses_review_placeholder(patient, monkeypatch):
    note_id = _seed_note(patient)
    _stub_extraction(monkeypatch, {
        "medications": [{"drug_name": "Bisoprolol", "action": "start"}],
        "vitals": {}, "problems": [],
    })

    await nst.on_scribe_note_signed(_event(patient, note_id))

    row = _med(patient, "Bisoprolol")
    assert row is not None
    # No invented dose/frequency — explicitly flagged for pharmacist entry.
    assert row["dosage"] == "REVIEW REQUIRED"
    assert row["frequency"] == "REVIEW REQUIRED"
    # Nor an invented route: it used to default to "Oral", which charted
    # enoxaparin as an oral drug.
    assert row["route"] == "REVIEW REQUIRED"
    assert row["source"] == "llm_extracted"
    assert row["verified_by"] is None


# ── severe alert → still charted unverified, flagged for the pharmacist ───────
async def test_severe_allergy_start_still_charted_with_safety_note(patient, monkeypatch):
    add_allergy(patient, "amoxicillin", reaction="anaphylaxis", severity="severe")
    note_id = _seed_note(patient)
    _stub_extraction(monkeypatch, {
        "medications": [{"drug_name": "Amoxicillin", "action": "start",
                         "dosage": "500mg", "frequency": "TDS"}],
        "vitals": {}, "problems": [],
    })

    await nst.on_scribe_note_signed(_event(patient, note_id))

    row = _med(patient, "Amoxicillin")
    assert row is not None, "a blocked med must be charted for review, not dropped silently"
    assert row["source"] == "llm_extracted"
    assert row["verified_by"] is None
    assert "[SAFETY]" in (row["notes"] or "")
    assert "allergic" in row["notes"].lower() or "amoxicillin" in row["notes"].lower()

    # The pharmacist sees it in the queue, flagged severe.
    pending = next(p for p in pharmacy.list_pending_verification() if p["id"] == row["id"])
    assert "[SAFETY]" in (pending["notes"] or "")
    assert pending["max_severity"] == "severe"


# ── 'stop' → proposal only, never an autonomous discontinuation ───────────────
async def test_stop_is_proposed_not_executed(patient, monkeypatch):
    chan_id = f"case-{patient}"
    with db_session() as db:
        cur = db.execute(
            """INSERT INTO medications (patient_id, drug_name, dosage, route, frequency, status, source)
               VALUES (?, 'Warfarin', '5mg', 'Oral', 'OD', 'active', 'clinician')""",
            (patient,),
        )
        med_id = cur.lastrowid
        db.execute(
            "INSERT INTO channels (id, name, type, patient_id) VALUES (?, ?, 'case_room', ?)",
            (chan_id, "Case: Extraction Test", patient),
        )

    note_id = _seed_note(patient)
    _stub_extraction(monkeypatch, {
        "medications": [{"drug_name": "Warfarin", "action": "stop"}],
        "vitals": {}, "problems": [],
    })

    await nst.on_scribe_note_signed(_event(patient, note_id))

    with db_session() as db:
        med = db.execute("SELECT * FROM medications WHERE id = ?", (med_id,)).fetchone()
        doc = db.execute(
            "SELECT * FROM clinical_documents WHERE patient_id = ? AND title = 'Proposed medication stop (from signed note)'",
            (patient,),
        ).fetchone()
        msg = db.execute(
            "SELECT * FROM messages WHERE channel_id = ? ORDER BY id DESC", (chan_id,)
        ).fetchone()

    # The med row is untouched — a human executes the stop via the chart.
    assert med["status"] == "active"

    # The proposal is documented...
    assert doc is not None
    assert "Warfarin" in doc["content"]
    assert str(note_id) in doc["content"]

    # ...and posted to the case room as a bot-provenance system message.
    assert msg is not None
    assert "Warfarin" in msg["content"]
    assert msg["message_type"] == "system"
    assert msg["is_bot"] == 1


async def test_non_object_extraction_payload_writes_nothing(patient, monkeypatch):
    """json.loads can yield a bare array/string; the handler must fail closed
    (no crash, no writes) instead of AttributeError-ing on .get()."""
    note_id = _seed_note(patient)
    _stub_extraction(monkeypatch, ["Paracetamol 500mg"])  # truthy non-dict

    await nst.on_scribe_note_signed(_event(patient, note_id))

    with db_session() as db:
        meds = db.execute(
            "SELECT COUNT(*) AS n FROM medications WHERE patient_id = ?", (patient,)
        ).fetchone()["n"]
        docs = db.execute(
            "SELECT COUNT(*) AS n FROM clinical_documents WHERE patient_id = ?", (patient,)
        ).fetchone()["n"]
    assert meds == 0
    assert docs == 0


async def test_stop_without_case_room_still_documents_proposal(patient, monkeypatch):
    with db_session() as db:
        db.execute(
            """INSERT INTO medications (patient_id, drug_name, dosage, route, frequency, status, source)
               VALUES (?, 'Ramipril', '2.5mg', 'Oral', 'OD', 'active', 'clinician')""",
            (patient,),
        )

    note_id = _seed_note(patient)
    _stub_extraction(monkeypatch, {
        "medications": [{"drug_name": "Ramipril", "action": "stop"}],
        "vitals": {}, "problems": [],
    })

    await nst.on_scribe_note_signed(_event(patient, note_id))

    with db_session() as db:
        med = db.execute(
            "SELECT * FROM medications WHERE patient_id = ? AND drug_name = 'Ramipril'",
            (patient,),
        ).fetchone()
        doc = db.execute(
            "SELECT * FROM clinical_documents WHERE patient_id = ? AND title = 'Proposed medication stop (from signed note)'",
            (patient,),
        ).fetchone()

    assert med["status"] == "active"
    assert doc is not None and "Ramipril" in doc["content"]
