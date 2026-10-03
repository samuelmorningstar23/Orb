"""The agentic (no-human-at-the-other-end) order path must FAIL CLOSED.

Regression guard for the fail-open defect verified 2026-08-06: the Sage handoff
adapter used to call ``check_medication_safety`` directly and swallow any
exception into ``safety = {"blocking": False}``. An unreadable allergy table —
a schema/migration fault, a decryption failure, a corrupt page — therefore
looked identical to a clean screen, and amoxicillin was charted ACTIVE for a
patient with documented penicillin anaphylaxis.

Reproduced for real (not mocked) by renaming ``patient_allergies`` away on a
throwaway DB: the archived pre-fix module charted the drug; the current module
blocks. These tests inject the same fault class at the one DB read the screen
depends on, and also pin the legitimate cases so the fix cannot be "hardened"
into blocking real orders.
"""
import uuid

import pytest

from database import db_session
from db_backend import DBOperationalError
from services.clinical_records_service import add_allergy
from services.sage_handoff_service import HandoffBlocked, _dispatch_to_helix


@pytest.fixture()
def patient():
    """TEST-ISOLATION LAW: patients.status DEFAULTS to 'admitted', so every test
    in this module used to leave one more admitted patient in the shared session
    DB — ~6 by the end of the module. That is not a cosmetic leak. The FHIR
    self-export in services/downtime_service.py iterates
    `patients WHERE status='admitted'` and sleeps _PATIENT_SLEEP_S (250 ms)
    BETWEEN PATIENTS, so each leftover adds ~0.275 s to every export call in
    tests/test_downtime_fhir_export.py and to everything downstream of it.
    Discharge on teardown; do not DELETE, because the allergy and medication
    rows written during the test legitimately reference this id."""
    pid = f"PATFO{uuid.uuid4().hex[:6].upper()}"
    with db_session() as db:
        db.execute("INSERT INTO patients (id, name, mrn) VALUES (?, ?, ?)",
                   (pid, "Fail-open Test", f"MRN-{uuid.uuid4().hex[:6]}"))
    yield pid
    with db_session() as db:
        db.execute("UPDATE patients SET status = 'discharged' WHERE id = ?", (pid,))


def _charted(pid, drug):
    with db_session() as db:
        return db.execute(
            "SELECT count(*) FROM medications WHERE patient_id = ? AND lower(drug_name) = ?",
            (pid, drug.lower()),
        ).fetchone()[0]


@pytest.fixture()
def broken_allergy_read(monkeypatch):
    """The allergy table cannot be read — the exact fault class the pre-fix code
    treated as 'no alerts'. Injected at the real DB read inside the screen, so
    ``check_medication_safety`` raises for real rather than being replaced."""
    def _boom(db, patient_id):
        raise DBOperationalError("no such table: patient_allergies")

    import services.drug_safety_service as dss
    monkeypatch.setattr(dss, "_active_allergies", _boom)
    return _boom


# ── the defect ───────────────────────────────────────────────────────────────
def test_agentic_order_fails_closed_when_allergy_screen_errors(patient, broken_allergy_read):
    """Pre-fix: charted amoxicillin against a penicillin anaphylaxis. Must block."""
    add_allergy(patient, "Penicillin", reaction="anaphylaxis", severity="severe")

    with pytest.raises(HandoffBlocked) as blocked:
        _dispatch_to_helix(
            {"patient_id": patient, "target": "Amoxicillin", "rationale": "start iv abx"},
            actor_id="D001",
        )

    assert blocked.value.safety["screen_failed"] is True, (
        "a failed screen must be reported as a failed screen, not as an allergy match"
    )
    assert _charted(patient, "Amoxicillin") == 0, (
        "a contraindicated drug reached the chart while the safety screen was failing"
    )


def test_failed_screen_leaves_an_audit_trail(patient, broken_allergy_read):
    """A failed screen must be recorded, not just logged — the pre-fix path left
    nothing on the audit chain to show the order was never checked."""
    with db_session() as db:
        before = db.execute(
            "SELECT count(*) FROM audit_logs WHERE event_category = 'clinical_safety'"
        ).fetchone()[0]

    with pytest.raises(HandoffBlocked):
        _dispatch_to_helix(
            {"patient_id": patient, "target": "Amoxicillin", "rationale": "start abx"},
            actor_id="D001",
        )

    with db_session() as db:
        rows = db.execute(
            "SELECT outcome, action_type FROM audit_logs WHERE event_category = 'clinical_safety' "
            "ORDER BY id DESC LIMIT 5"
        ).fetchall()
        after = db.execute(
            "SELECT count(*) FROM audit_logs WHERE event_category = 'clinical_safety'"
        ).fetchone()[0]

    assert after > before, "failed drug-safety screen was not audited"
    assert any(r["outcome"] == "failure" and r["action_type"] == "drug_safety_screen"
               for r in rows)


# ── the legitimate cases the fix must NOT break ──────────────────────────────
def test_screen_working_no_conflict_still_charts(patient):
    _dispatch_to_helix(
        {"patient_id": patient, "target": "Paracetamol", "rationale": "give iv paracetamol"},
        actor_id="D001",
    )
    assert _charted(patient, "Paracetamol") == 1


def test_moderate_cross_reactivity_is_not_over_blocked(patient):
    """Penicillin allergy + a cephalosporin is a MODERATE cross-reactivity alert.
    Blocking it would be the alert-fatigue/over-tightening failure, not safety."""
    add_allergy(patient, "Penicillin", reaction="rash", severity="moderate")
    _ref, safety, _note = _dispatch_to_helix(  # raises HandoffBlocked if over-blocked
        {"patient_id": patient, "target": "Cefuroxime", "rationale": "start abx"},
        actor_id="D001",
    )
    assert _charted(patient, "Cefuroxime") == 1
    # Not blocked, but not silent either: the alert travels with the handoff.
    assert any(a["match"] == "cross_reactivity" for a in safety["alerts"])


def test_non_formulary_drug_still_charts(patient):
    """A drug outside the interaction knowledge base screens clean-but-unscreened.
    'Not in the KB' must not be treated as 'screen failed'."""
    _ref, safety, _note = _dispatch_to_helix(  # raises HandoffBlocked if treated as failed
        {"patient_id": patient, "target": "Some-Random-Herbal", "rationale": "continue"},
        actor_id="D001",
    )
    assert safety["screen_failed"] is False
    assert _charted(patient, "Some-Random-Herbal") == 1


def test_severe_allergy_still_blocks_with_a_working_screen(patient):
    add_allergy(patient, "Penicillin", reaction="anaphylaxis", severity="severe")
    with pytest.raises(HandoffBlocked) as blocked:
        _dispatch_to_helix(
            {"patient_id": patient, "target": "Amoxicillin", "rationale": "start abx"},
            actor_id="D001",
        )
    assert blocked.value.safety["screen_failed"] is False
    assert "penicillin" in blocked.value.reason.lower()
    assert _charted(patient, "Amoxicillin") == 0
