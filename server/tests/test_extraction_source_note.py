"""A medicine charted from a signed note keeps which note and which sentence.

Migration 79 adds medications.source_note_id and source_sentence. Signed-note
extraction (nervous_system_triggers) stores them for each 'start' it charts,
and the pharmacist's Verify Queue payload carries them with the note's signer
and signing time, so the queue can say "From the note signed by Dr X at 16:49"
and show the sentence the doctor signed.
"""
import json
import uuid

import pytest

from database import db_session
from services import nervous_system_triggers as nst
from services import pharmacy_verification_service as pharmacy
from services.event_bus import StateEvent

PLAN = ("Start enoxaparin 40 mg SC once daily for VTE prophylaxis. "
        "Review renal function in 48 hours.")


@pytest.fixture()
def patient():
    pid = f"PATSRC{uuid.uuid4().hex[:6].upper()}"
    with db_session() as db:
        db.execute("INSERT INTO patients (id, name, mrn, status) VALUES (?, ?, ?, 'admitted')",
                   (pid, "Source Note Test", f"MRN-{pid}"))
    yield pid
    with db_session() as db:
        db.execute("UPDATE patients SET status = 'discharged' WHERE id = ?", (pid,))


@pytest.fixture()
def doctor():
    uid = f"DSRC{uuid.uuid4().hex[:5].upper()}"
    with db_session() as db:
        db.execute("INSERT INTO users (id, name, role, password_hash) VALUES (?, ?, 'doctor', 'x')",
                   (uid, "Dr Rohan Pillai"))
    return uid


def _signed_note(patient_id, author_id):
    note = json.dumps({"subjective": "Day 2 after hip fracture repair.",
                       "assessment": "Immobile, at risk of VTE.",
                       "plan": PLAN})
    with db_session() as db:
        return db.execute(
            """INSERT INTO scribe_notes (patient_id, author_id, raw_transcript, structured_note,
                                         status, note_format, signed_by, signed_at)
               VALUES (?, ?, 'Transcript', ?, 'signed', 'soap', ?, '2026-10-06T11:19:00+00:00')""",
            (patient_id, author_id, note, author_id),
        ).lastrowid


def _extract(monkeypatch, drugs):
    async def _fake(note_id, patient_id, structured_text):
        return {"medications": [{"drug_name": d, "action": "start"} for d in drugs],
                "vitals": {}, "problems": []}
    monkeypatch.setattr(nst, "_extract_updates", _fake)


def _event(patient_id, note_id, signer):
    return StateEvent(topic="scribe.note.signed", source_node="scribe", event_type="note_signed",
                      patient_id=patient_id, payload={"note_id": note_id, "signer_id": signer})


def _row(patient_id, drug):
    with db_session() as db:
        return db.execute("SELECT * FROM medications WHERE patient_id = ? AND drug_name = ?",
                          (patient_id, drug)).fetchone()


async def test_extraction_stores_the_note_id_and_the_sentence(patient, doctor, monkeypatch):
    note_id = _signed_note(patient, doctor)
    _extract(monkeypatch, ["Enoxaparin 40mg", "Vancomycin"])
    await nst.on_scribe_note_signed(_event(patient, note_id, doctor))

    row = _row(patient, "Enoxaparin 40mg")
    assert row["source_note_id"] == note_id
    assert row["source_sentence"] == "Start enoxaparin 40 mg SC once daily for VTE prophylaxis."
    # A drug the note never names keeps the note, and no invented sentence.
    other = _row(patient, "Vancomycin")
    assert other["source_note_id"] == note_id
    assert other["source_sentence"] is None


async def test_the_queue_payload_carries_the_note_and_its_signer(patient, doctor, monkeypatch):
    note_id = _signed_note(patient, doctor)
    _extract(monkeypatch, ["Enoxaparin"])
    await nst.on_scribe_note_signed(_event(patient, note_id, doctor))

    med_id = _row(patient, "Enoxaparin")["id"]
    item = next(p for p in pharmacy.list_pending_verification() if p["id"] == med_id)
    assert item["source_note_id"] == note_id
    assert item["source_sentence"] == "Start enoxaparin 40 mg SC once daily for VTE prophylaxis."
    assert item["source_note_signed_by"] == "Dr Rohan Pillai"
    assert item["source_note_signed_at"] == "2026-10-06T11:19:00+00:00"


def test_a_plain_text_note_finds_the_sentence_too():
    text = "Chest clear. Commence amoxicillin 500 mg TDS for five days.\nReview tomorrow."
    assert nst._drug_sentence(text, "Amoxicillin 500mg") == "Commence amoxicillin 500 mg TDS for five days."
    assert nst._drug_sentence(text, "Ceftriaxone") is None


async def test_the_pharmacist_can_open_the_note_from_the_queue(patient, doctor, monkeypatch):
    from fastapi.testclient import TestClient
    from auth import create_token
    from main import app

    note_id = _signed_note(patient, doctor)
    _extract(monkeypatch, ["Enoxaparin"])
    await nst.on_scribe_note_signed(_event(patient, note_id, doctor))
    med_id = _row(patient, "Enoxaparin")["id"]

    pharmacist = f"PHSRC{uuid.uuid4().hex[:5].upper()}"
    with db_session() as db:
        db.execute("INSERT INTO users (id, name, role, password_hash) VALUES (?, 'Priya Nair', 'pharmacist', 'x')",
                   (pharmacist,))
    client = TestClient(app)
    hdr = {"Authorization": f"Bearer {create_token(pharmacist, 'pharmacist')}"}
    resp = client.get(f"/api/pharmacy/{med_id}/source-note", headers=hdr)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["note_id"] == note_id and body["signed_by"] == "Dr Rohan Pillai"
    assert PLAN in body["text"] and "{" not in body["text"]
    with db_session() as db:
        read = db.execute(
            "SELECT count(*) FROM audit_logs WHERE action_type = 'scribe_note_viewed' "
            "AND target_id = ? AND actor_user_id = ?", (str(note_id), pharmacist)).fetchone()[0]
    assert read == 1, "the read is on the audit chain"

    # A medicine not from a note has nothing to open.
    with db_session() as db:
        plain = db.execute(
            "INSERT INTO medications (patient_id, drug_name, dosage, route, frequency, status, source) "
            "VALUES (?, 'Paracetamol', '1g', 'Oral', 'QDS', 'active', 'agentic')", (patient,)).lastrowid
    assert client.get(f"/api/pharmacy/{plain}/source-note", headers=hdr).status_code == 404
