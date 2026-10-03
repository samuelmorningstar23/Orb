"""Orb — TIER 3 RELOCATED: the reasoned assessment on the handover and the round.

WHY THIS FILE EXISTS. Sage's full four-section assessment is measured at 42-57 s
end to end on this appliance. At a deteriorating bedside that is useless — 45
seconds is past the point where a second opinion changes anything. The reasoning
itself is good; the SURFACE was wrong. So it is carried where 45 seconds is
invisible because it was already paid: the I-PASS shift handover, composed ahead
of a shift change, and the ward round, walked patient by patient.

The move is only safe if three things hold, and each one is a property here
rather than a feature:

  1. IT IS REUSED, NEVER RE-DERIVED. The sheet says what the ward was actually
     shown when Vigil paged. A handover that re-runs the model produces a
     second, slower, subtly different opinion — and "the machine told the night
     nurse something else" is the exact disagreement a handover exists to
     prevent. Bounded by a staleness rule so an assessment from outside the
     window the sheet claims to cover can never appear on it.
  2. THE MACHINE AUTHORSHIP IS UNMISTAKABLE. `plain_text` is the print/copy
     source of truth for a MEDICO-LEGAL document. A reader must never be able to
     take this section for something a clinician wrote, and must never be able
     to take it for a description of the patient NOW — it is a snapshot, stamped
     with the NEWS2 the engine computed at the time.
  3. NOTHING IS INVENTED TO FILL A HEADING. No assessment on file means the
     sheet says nothing at all: no heading, no "none available", no blank
     section that reads as one somebody forgot to complete.

And the standing rule this file inherits from tests/test_ipass.py: NO MACHINE
IDENTIFIER REACHES THE PRINTED SHEET. That defect (`insufficient_data` printed
next to its own English translation) was fixed in this file's subject yesterday;
the supplement adds five more enums — `state`, `citation_basis`, `confidence`,
`news2_level`, `action_type` — and every one of them is a fresh chance to
reintroduce it.
"""
import asyncio
import json
import os
import re
import sys
import uuid
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from database import db_session  # noqa: E402
from services import ipass_service  # noqa: E402
from services.ipass_service import (  # noqa: E402
    AI_DRAFTED_LABEL,
    AI_SUPPLEMENT_DISCLAIMER,
    AI_SUPPLEMENT_END,
    AI_SUPPLEMENT_HEADING,
    build_ipass,
    get_reasoned_assessment,
    render_assessment_text,
    save_ipass_handoff,
)

DEPARTMENT = "Relocation Medicine"

#: Model prose, exactly as `sage_assessment_service` stores it: already
#: sanitized, already NEWS2-pinned. Deliberately free of machine identifiers so
#: that any snake_case token the sheet-wide assertion finds is ORB'S, which is
#: the half this module controls.
SUMMARY = (
    "Respiratory rate 32 and SpO2 86% on oxygen are the dominant NEWS2 "
    "contributors, with hypotension at 78 systolic across the last four sets. "
    "The picture is consistent with worsening sepsis.")

CONSIDERATIONS = [
    {"key": "c0", "step": "Start IV ceftriaxone for suspected sepsis",
     "why": "Fever with hypotension", "orb_can_act": True,
     "action_id": "act-1", "action_type": "start_medication",
     "proposed_action_type": "start_medication", "proposed_target": "Ceftriaxone",
     "advice_only_reason": None},
    {"key": "c1", "step": "Escalate to the medical registrar now",
     "why": "Score in the critical band", "orb_can_act": False,
     "action_id": None, "action_type": None,
     "proposed_action_type": None, "proposed_target": None,
     "advice_only_reason": None},
]

WATCH_ITEMS = [
    "Whether the patient is protecting their airway",
    "Whether there is a source of infection visible at the bedside",
]

EVIDENCE = [
    {"document_id": "LOC-SEPSIS-001", "title": "Sepsis Six — local policy",
     "source": "Hospital policy", "trust_tier": 1, "attribution": "model_cited"},
]

_CREATED_PATIENTS: list[str] = []
_CREATED_USERS: list[str] = []


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────
def _now() -> datetime:
    return datetime.now(timezone.utc)


def _mk_patient(*, department: str = DEPARTMENT) -> str:
    """An admitted patient with enough vitals for a real I-PASS sheet.

    The department lives on the ADMISSION — `patients.ward` is a bed label and
    is not what ward scoping reads.
    """
    pid = f"RELOCP{uuid.uuid4().hex[:8].upper()}"
    _CREATED_PATIENTS.append(pid)
    now = _now()
    with db_session() as db:
        db.execute("INSERT OR IGNORE INTO users (id, name, role, password_hash) "
                   "VALUES ('RELOCADM', 'Relocation Admitter', 'doctor', 'x')")
        db.execute(
            """INSERT INTO patients (id, name, mrn, bed, ward, age, sex, diagnosis,
                                     status, admission_date)
               VALUES (?, ?, ?, 'R-1', 'RL-1', 71, 'F', 'Community acquired pneumonia',
                       'admitted', ?)""",
            (pid, f"Relocation {pid}", f"MRN-{uuid.uuid4().hex[:8].upper()}",
             now.isoformat()),
        )
        db.execute(
            "INSERT INTO admissions (patient_id, department, ward, bed, status, admitted_by) "
            "VALUES (?, ?, 'RL-1', 'R-1', 'active', 'RELOCADM')",
            (pid, department),
        )
        for i in range(4):
            drift = 3 - i
            db.execute(
                """INSERT INTO vitals (patient_id, timestamp, heart_rate, systolic_bp,
                                       diastolic_bp, spo2, temperature, respiratory_rate,
                                       consciousness, on_oxygen)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'alert', 1)""",
                (pid, (now - timedelta(minutes=20 * drift)).isoformat(),
                 138 - 4 * drift, 78 + 6 * drift, 44, 86 + 2 * drift, 39.4,
                 32 - 2 * drift),
            )
    return pid


def _mk_assessment(patient_id: str, *, state: str = "ready", age_minutes: int = 45,
                   summary: str = SUMMARY, news2: int | None = 19,
                   news2_level: str = "critical", citation_basis: str = "model_cited",
                   confidence: str = "medium", model: str | None = "medgemma:27b",
                   depth: str = "full", message_id=None,
                   considerations=None, watch_items=None, evidence=None) -> str:
    """A stored Sage assessment, written `age_minutes` ago.

    Inserted directly rather than generated: this file is about what happens to
    a draft that ALREADY EXISTS, and driving the 27B to make one would test the
    generator (which tests/test_sage_assessment.py already owns) instead.

    `depth` defaults to 'full' because that is the artefact this surface carries
    — migration 30's Tier 3. The 'brief' case has its own test.
    """
    aid = str(uuid.uuid4())
    stamp = (_now() - timedelta(minutes=age_minutes)).isoformat()
    with db_session() as db:
        db.execute(
            """INSERT INTO sage_assessments
                   (id, patient_id, channel_id, message_id, episode_id, lane, state,
                    depth, news2, news2_level, news2_drivers, summary, considerations,
                    evidence, watch_items, citation_basis, confidence, model,
                    conversation_id, ttft_ms, total_ms, version, created_at, updated_at)
               VALUES (?, ?, NULL, ?, ?, 'simulator', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                       ?, 4200, 46000, 1, ?, ?)""",
            (aid, patient_id, message_id, f"ep-{aid[:8]}", state, depth, news2, news2_level,
             json.dumps(["Respiratory rate 32", "SpO2 86% on oxygen"]),
             summary,
             json.dumps(CONSIDERATIONS if considerations is None else considerations),
             json.dumps(EVIDENCE if evidence is None else evidence),
             json.dumps(WATCH_ITEMS if watch_items is None else watch_items),
             citation_basis, confidence, model, f"assess-{aid[:12]}", stamp, stamp),
        )
    return aid


def _endorse(assessment_id: str, name: str, role: str = "doctor") -> None:
    with db_session() as db:
        db.execute(
            """INSERT INTO sage_assessment_events
                   (assessment_id, prior_state, new_state, actor_id, actor_name,
                    actor_role, reason, source, created_at)
               VALUES (?, 'ready', 'approved', 'REL-D1', ?, ?, NULL, 'ui', ?)""",
            (assessment_id, name, role, _now().isoformat()),
        )


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    with db_session() as db:
        for pid in _CREATED_PATIENTS:
            db.execute("DELETE FROM sage_assessment_events WHERE assessment_id IN "
                       "(SELECT id FROM sage_assessments WHERE patient_id = ?)", (pid,))
            for tbl in ("sage_assessments", "vitals", "medications", "problem_list",
                        "patient_allergies", "scribe_notes", "risk_scores",
                        "admissions"):
                db.execute(f"DELETE FROM {tbl} WHERE patient_id = ?", (pid,))
            db.execute("DELETE FROM patients WHERE id = ?", (pid,))
        for uid in _CREATED_USERS:
            db.execute("DELETE FROM users WHERE id = ?", (uid,))
    _CREATED_PATIENTS.clear()
    _CREATED_USERS.clear()


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────
#: Same rule as tests/test_ipass.py: a lower-case machine identifier.
_SNAKE_CASE = re.compile(r"\b[a-z0-9]+(?:_[a-z0-9]+)+\b")


def _machine_tokens(text: str) -> list[str]:
    return sorted(set(_SNAKE_CASE.findall(text)))


def _supplement(plain_text: str) -> str:
    """Just the AI block, sliced out of the sheet by its own two markers."""
    if AI_SUPPLEMENT_HEADING not in plain_text:
        return ""
    head = plain_text.index(AI_SUPPLEMENT_HEADING)
    tail = plain_text.index(AI_SUPPLEMENT_END) + len(AI_SUPPLEMENT_END)
    return plain_text[head:tail]


# ═════════════════════════════════════════════════════════════════════════════
#  1. THE ASSESSMENT ARRIVES ON THE HANDOVER
# ═════════════════════════════════════════════════════════════════════════════
def test_a_recent_alert_assessment_is_carried_into_the_handover():
    """The whole point of the relocation: the reasoning the ward saw at 3 a.m.
    is on the sheet the oncoming shift reads, in both forms."""
    patient = _mk_patient()
    _mk_assessment(patient)

    draft = build_ipass(patient)
    block = draft["sections"]["ai_assessment"]

    assert block is not None, "the drafted assessment did not reach the handover"
    assert block["whats_happening"] == SUMMARY
    assert [c["step"] for c in block["considerations"]] == [c["step"] for c in CONSIDERATIONS]
    assert block["watch_items"] == WATCH_ITEMS

    text = draft["plain_text"]
    assert AI_SUPPLEMENT_HEADING in text
    assert SUMMARY in text
    assert "Start IV ceftriaxone for suspected sepsis" in text
    assert "Whether the patient is protecting their airway" in text

    # It sits before the read-back, so the receiver signs off having seen it and
    # the signature block is still the last thing on the page.
    assert text.index(AI_SUPPLEMENT_HEADING) < text.index("S. SYNTHESIS BY RECEIVER")


def test_the_supplement_says_why_the_assessment_exists_and_does_not_invent_an_alert():
    """A draft carrying a `message_id` was written because Vigil paged. One
    without was written because a surface asked for it — printing "when Vigil
    paged" over it would assert a clinical event that never happened, on a
    medico-legal sheet."""
    paged = _mk_patient()
    _mk_assessment(paged, message_id=4242)
    assert get_reasoned_assessment(paged)["reuse_basis"] == "drafted_for_vigil_alert"
    assert "when Vigil paged for this patient" in _supplement(
        build_ipass(paged)["plain_text"])

    asked = _mk_patient()
    _mk_assessment(asked)                       # message_id NULL — a prepare
    block = get_reasoned_assessment(asked)
    assert block["reuse_basis"] == "drafted_for_review"
    supplement = _supplement(build_ipass(asked)["plain_text"])
    assert "when this patient was prepared for review" in supplement
    assert "Vigil paged" not in supplement


def test_the_supplement_carries_the_four_sections_and_nothing_it_did_not_have():
    patient = _mk_patient()
    _mk_assessment(patient)
    block = get_reasoned_assessment(patient)

    assert block["evidence"][0]["title"] == "Sepsis Six — local policy"
    assert block["citation_basis"] == "model_cited"     # raw enum still on the wire
    assert block["confidence"] == "medium"
    assert block["model"] == "medgemma:27b"
    assert block["reused"] is True


# ═════════════════════════════════════════════════════════════════════════════
#  2. NO ASSESSMENT → NO SECTION  (prove-can-fail)
# ═════════════════════════════════════════════════════════════════════════════
def test_a_patient_with_no_assessment_produces_no_empty_section():
    """A heading with nothing under it reads as a section somebody forgot to
    complete. On a medico-legal handover that is worse than silence.

    PROVE-CAN-FAIL: rendering the heading unconditionally fails this.
    """
    patient = _mk_patient()          # no assessment row at all

    draft = build_ipass(patient)
    text = draft["plain_text"]

    # The key is PRESENT and null — the client tests one key rather than
    # inferring a section's existence from absence.
    assert "ai_assessment" in draft["sections"]
    assert draft["sections"]["ai_assessment"] is None

    assert AI_SUPPLEMENT_HEADING not in text
    assert AI_DRAFTED_LABEL not in text
    assert "Orb Sage" not in text
    assert AI_SUPPLEMENT_END not in text
    # Not even the shape of an empty section: nothing between the last I-PASS
    # letter and the read-back but the blank line that was always there.
    between = text[text.index("Contingency planning:"):text.index("S. SYNTHESIS BY RECEIVER")]
    assert "SUPPLEMENT" not in between and "assessment" not in between.lower()

    # And the standalone (ward-round) form renders nothing rather than a header.
    assert render_assessment_text(None) == ""
    assert get_reasoned_assessment(patient) is None


def test_an_assessment_with_no_prose_is_not_carried():
    """A row can exist and still have nothing to say — `ready` is written before
    the model runs. Nothing is ever invented to fill the heading."""
    patient = _mk_patient()
    _mk_assessment(patient, summary="   ")

    assert get_reasoned_assessment(patient) is None
    assert AI_SUPPLEMENT_HEADING not in build_ipass(patient)["plain_text"]


# ═════════════════════════════════════════════════════════════════════════════
#  3. THE LABELLING  (prove-can-fail)
# ═════════════════════════════════════════════════════════════════════════════
def test_the_ai_drafted_labelling_is_present_in_both_forms():
    """THE MEDICO-LEGAL PROPERTY. `plain_text` is what gets printed, pasted into
    a message and stapled into the notes. A reader must not be able to take this
    section for something a clinician wrote.

    PROVE-CAN-FAIL: soften the heading or the label and this fails.
    """
    patient = _mk_patient()
    _mk_assessment(patient)
    draft = build_ipass(patient)
    block = draft["sections"]["ai_assessment"]

    # ── structured ──
    assert block["ai_drafted"] is True
    assert block["label"] == AI_DRAFTED_LABEL
    assert block["heading"] == AI_SUPPLEMENT_HEADING
    assert block["authored_by"] == "Orb Sage (artificial intelligence)"
    assert block["disclaimer"] == AI_SUPPLEMENT_DISCLAIMER
    for word in ("AI-drafted", "not written by a clinician"):
        assert word.lower() in block["label"].lower()

    # ── plain text ──
    supplement = _supplement(draft["plain_text"])
    assert supplement, "the supplement is missing from the printed sheet"
    assert supplement.startswith(AI_SUPPLEMENT_HEADING)
    assert "NOT WRITTEN BY A CLINICIAN" in supplement
    assert "Orb Sage (artificial intelligence)" in supplement
    assert AI_SUPPLEMENT_DISCLAIMER in supplement
    assert "has been ordered, charted or administered" in supplement
    assert supplement.rstrip().endswith(AI_SUPPLEMENT_END), (
        "the supplement does not close, so the lines after it read as part of it")

    # The summary is stored as the note's raw_transcript and read on its own.
    assert "AI-drafted" in draft["source_summary"]
    assert "not written by a clinician" in draft["source_summary"]


def test_the_saved_handover_note_keeps_the_labelling():
    """`save_ipass_handoff` stores `plain_text` verbatim as the note. The record
    that survives is the one that has to carry the label."""
    patient = _mk_patient()
    _mk_assessment(patient)
    note_id = save_ipass_handoff(patient, "RELOCADM", build_ipass(patient))

    with db_session() as db:
        row = dict(db.execute("SELECT * FROM scribe_notes WHERE id = ?",
                              (note_id,)).fetchone())
    assert AI_SUPPLEMENT_HEADING in row["structured_note"]
    assert "NOT WRITTEN BY A CLINICIAN" in row["structured_note"]
    assert "AI-drafted" in row["raw_transcript"]


def test_the_supplement_is_stamped_as_a_snapshot_not_as_the_patient_now(monkeypatch):
    """Everything else on the sheet is current; this section is not. It is
    stamped with the NEWS2 the ENGINE computed when it was written — read off
    the row, never recomputed — and with how long ago that was."""
    from zoneinfo import ZoneInfo
    from services.vitals_source import hl7v2
    monkeypatch.setattr(hl7v2, "_default_tz", ZoneInfo("Asia/Kolkata"))
    monkeypatch.setattr(hl7v2, "_default_tz_resolved", True)
    patient = _mk_patient()
    _mk_assessment(patient, age_minutes=125, news2=19, news2_level="critical")

    block = get_reasoned_assessment(patient)
    assert block["news2_at_drafting"] == 19
    assert block["news2_level_at_drafting"] == "critical"
    assert block["age_minutes"] == pytest.approx(125, abs=2)
    assert block["age_display"].startswith("2 hours")

    supplement = _supplement(build_ipass(patient)["plain_text"])
    assert "NEWS2 19 (critical band)" in supplement
    assert "not the patient's state now" in supplement
    assert "Check Orb's current score" in supplement
    assert "Written 2 hours" in supplement
    assert "it has not been re-run" in supplement
    # "for this handover" would be a false statement on the ward round, which
    # is served these same lines standalone.
    assert "for this handover" not in supplement

    # A PRINTED sheet outlives the moment it was printed, so "2 hours ago"
    # alone is not a timestamp. The absolute one travels with it, on the
    # hospital's clock (a UTC stamp read 5.5 hours wrong on an IST ward).
    assert block["drafted_at_display"].endswith(" IST")
    assert block["drafted_at_display"] in supplement


def test_a_reviewed_draft_names_the_clinician_who_endorsed_it():
    """"An AI draft a named doctor endorsed" and "an AI draft nobody has read"
    are different documents, and the sheet must say which one this is."""
    patient = _mk_patient()
    unreviewed = _supplement(build_ipass(_seeded(patient, "ready"))["plain_text"])
    assert "no clinician has reviewed this draft" in unreviewed
    assert "Reviewed and endorsed by" not in unreviewed

    other = _mk_patient()
    aid = _mk_assessment(other, state="approved")
    _endorse(aid, "Dr Anand Rao", "doctor")
    block = get_reasoned_assessment(other)
    assert block["endorsed_by"] == "Dr Anand Rao (doctor)"
    assert "Reviewed and endorsed by Dr Anand Rao (doctor)." in _supplement(
        build_ipass(other)["plain_text"])


def _seeded(patient_id: str, state: str) -> str:
    _mk_assessment(patient_id, state=state)
    return patient_id


# ═════════════════════════════════════════════════════════════════════════════
#  4. THE REUSE RULE AND ITS STALENESS BOUND
# ═════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("age_minutes,carried", [
    (5, True),
    (11 * 60, True),
    (12 * 60 - 5, True),
    (12 * 60 + 5, False),     # just outside the sheet's own window
    (36 * 60, False),
])
def test_the_reuse_rule_respects_its_staleness_bound(age_minutes, carried):
    """The bound IS the handover's look-back window. A sheet whose header says
    "last 12h" and whose supplement quotes a 36-hour-old assessment is lying in
    its own header."""
    patient = _mk_patient()
    _mk_assessment(patient, age_minutes=age_minutes)

    draft = build_ipass(patient)
    assert (draft["sections"]["ai_assessment"] is not None) is carried
    assert (AI_SUPPLEMENT_HEADING in draft["plain_text"]) is carried
    if carried:
        assert draft["sections"]["ai_assessment"]["stale_after_hours"] == 12


def test_the_bound_follows_a_narrowed_window():
    """The two are tied together on purpose, so they cannot disagree."""
    patient = _mk_patient()
    _mk_assessment(patient, age_minutes=5 * 60)

    assert build_ipass(patient, window_hours=12)["sections"]["ai_assessment"]
    assert build_ipass(patient, window_hours=2)["sections"]["ai_assessment"] is None
    assert get_reasoned_assessment(patient, 2) is None
    assert get_reasoned_assessment(patient, 12)["stale_after_hours"] == 12


@pytest.mark.parametrize("state", ["declined", "generating", "unavailable"])
def test_only_a_readable_draft_is_carried(state):
    """`declined` is the interesting one: a clinician has already rejected that
    reasoning, and reprinting it on a medico-legal sheet would re-assert it over
    their judgement. `generating` and `unavailable` have no text to carry."""
    patient = _mk_patient()
    _mk_assessment(patient, state=state)

    assert get_reasoned_assessment(patient) is None
    assert AI_SUPPLEMENT_HEADING not in build_ipass(patient)["plain_text"]


def test_the_newest_qualifying_draft_wins_and_only_one_appears():
    """Two machine assessments of one patient on one sheet leaves the reader
    with no way to tell which is current."""
    patient = _mk_patient()
    _mk_assessment(patient, age_minutes=300, summary="The older reading of this patient.")
    _mk_assessment(patient, age_minutes=30, summary="The newer reading of this patient.")

    text = build_ipass(patient)["plain_text"]
    assert "The newer reading of this patient." in text
    assert "The older reading of this patient." not in text
    assert text.count(AI_SUPPLEMENT_HEADING) == 1


def test_a_decline_vetoes_the_older_draft_underneath_it():
    """RULE 2b, and the one I got wrong first time round.

    The clinician declined the machine's current reasoning. Reaching past that
    for an older draft they have effectively superseded is Orb quietly choosing
    the answer it preferred after a human said no — the same move
    `sage_assessment_service._select_cards` already refuses when the drug-safety
    gate blocks a medication ("A BLOCKED SLOT IS NOT REFILLED"). The sheet says
    nothing instead.
    """
    patient = _mk_patient()
    _mk_assessment(patient, age_minutes=300, state="ready",
                   summary="The superseded reading of this patient.")
    _mk_assessment(patient, age_minutes=30, state="declined",
                   summary="The reading the clinician rejected.")

    assert get_reasoned_assessment(patient) is None
    text = build_ipass(patient)["plain_text"]
    assert AI_SUPPLEMENT_HEADING not in text
    assert "The superseded reading of this patient." not in text
    assert "The reading the clinician rejected." not in text


def test_a_failed_generation_does_not_veto_an_older_draft():
    """The other half of rule 2b. `unavailable` is Sage failing to write, not a
    human saying no — it carries no verdict and must not silence the last thing
    Sage actually said inside the window."""
    patient = _mk_patient()
    _mk_assessment(patient, age_minutes=300, state="ready",
                   summary="The last thing Sage actually said.")
    _mk_assessment(patient, age_minutes=30, state="unavailable", summary=None)

    block = get_reasoned_assessment(patient)
    assert block is not None
    assert block["whats_happening"] == "The last thing Sage actually said."
    assert "The last thing Sage actually said." in build_ipass(patient)["plain_text"]


def test_a_decline_outside_the_window_does_not_veto():
    """The veto is a statement about the CURRENT verdict, so it lives inside the
    same window as everything else on the sheet. A decline from three days ago
    is not the last word on a deterioration that happened this morning."""
    patient = _mk_patient()
    _mk_assessment(patient, age_minutes=48 * 60, state="declined",
                   summary="A verdict from two days ago.")
    _mk_assessment(patient, age_minutes=30, state="ready",
                   summary="This morning's reading.")

    block = get_reasoned_assessment(patient)
    assert block is not None
    assert block["whats_happening"] == "This morning's reading."


def test_the_handover_never_generates(monkeypatch):
    """REUSE, NOT RE-DERIVATION. Booby-trap every door to the model: if the
    handover reaches for one, this explodes instead of quietly costing a
    clinician 45 seconds at a shift change."""
    import services.model_router as model_router
    import services.llm_providers as llm_providers

    def _boom(*a, **kw):
        raise AssertionError("the handover reached for a model")

    monkeypatch.setattr(model_router, "select_chat_model", _boom)
    monkeypatch.setattr(llm_providers, "provider_for_model", _boom)

    patient = _mk_patient()
    _mk_assessment(patient)
    draft = build_ipass(patient)
    assert draft["sections"]["ai_assessment"]["reused"] is True
    assert draft["sections"]["ai_assessment"]["whats_happening"] == SUMMARY


def test_an_unreadable_assessment_table_costs_the_supplement_not_the_handover(monkeypatch):
    """FAILS OPEN. The supplement is an extra on a deterministic sheet. Losing
    the whole shift handover because a machine-authored extra could not be read
    is not a trade worth making."""
    patient = _mk_patient()
    _mk_assessment(patient)

    def _explode(*a, **kw):
        raise RuntimeError("no such table: sage_assessments")

    monkeypatch.setattr(ipass_service, "_get_reusable_assessment_rows", _explode)

    draft = build_ipass(patient)
    assert draft["sections"]["ai_assessment"] is None
    assert draft["sections"]["illness_severity"]["text"]      # the sheet survives
    assert "I-PASS SHIFT HANDOFF" in draft["plain_text"]
    assert AI_SUPPLEMENT_HEADING not in draft["plain_text"]


# ═════════════════════════════════════════════════════════════════════════════
#  4b. ONLY TIER 3 IS "THE ASSESSMENT"  (migration 30)
# ═════════════════════════════════════════════════════════════════════════════
def test_a_brief_alert_draft_is_not_carried_as_an_assessment():
    """The alert lane writes `depth='brief'` — Tier 1 plus at most three next
    steps, with NO prose interpretation by design. Putting those three bullets
    on a medico-legal sheet under a heading that says "assessment" would dress
    them as reasoning nobody ever wrote."""
    patient = _mk_patient()
    _mk_assessment(patient, depth="brief", summary=None)

    assert get_reasoned_assessment(patient) is None
    assert AI_SUPPLEMENT_HEADING not in build_ipass(patient)["plain_text"]


def test_a_brief_draft_with_prose_is_still_not_carried():
    """Belt and braces: the depth filter is the rule, not the emptiness of
    `summary`. A brief row that somehow carried prose is still not Tier 3."""
    patient = _mk_patient()
    _mk_assessment(patient, depth="brief", summary="Three quick steps, not an assessment.")

    assert get_reasoned_assessment(patient) is None
    assert "Three quick steps" not in build_ipass(patient)["plain_text"]


def test_a_declined_brief_draft_does_not_veto_the_full_assessment():
    """The veto is scoped to Tier 3 too. Declining the alert's three next steps
    is not a verdict on a reasoned assessment the clinician has never read."""
    patient = _mk_patient()
    _mk_assessment(patient, depth="brief", state="declined", age_minutes=10,
                   summary="Steps the clinician waved away at the bedside.")
    _mk_assessment(patient, depth="full", state="ready", age_minutes=60)

    block = get_reasoned_assessment(patient)
    assert block is not None
    assert block["whats_happening"] == SUMMARY


# ═════════════════════════════════════════════════════════════════════════════
#  4c. ASKING EARLY — the "nobody is waiting" half
# ═════════════════════════════════════════════════════════════════════════════
class _FakeGenerator:
    """Stands in for `sage_assessment_service.generate_full_assessment`.

    Records its calls and BLOCKS until released, so a test can assert that the
    caller did not wait for it — which is the whole property.
    """

    def __init__(self):
        self.calls: list[tuple] = []
        self.release = None

    async def __call__(self, patient_id, *, lane="handover", message_id=None):
        self.calls.append((patient_id, lane))
        if self.release is not None:
            await self.release.wait()
        return "fake-assessment-id"


async def test_preparing_reuses_before_it_generates(monkeypatch):
    """RULE 1, at the seam. A draft that already exists is never re-derived —
    it would be slower AND could disagree with what the ward was shown."""
    from services import sage_assessment_service as sas
    fake = _FakeGenerator()
    monkeypatch.setattr(sas, "generate_full_assessment", fake)

    patient = _mk_patient()
    _mk_assessment(patient)

    out = await ipass_service.prepare_reasoned_assessment(patient)
    assert out["status"] == "ready"
    assert out["assessment"]["whats_happening"] == SUMMARY
    assert fake.calls == [], "a fresh generation was started over a usable draft"
    # One payload shape whatever the outcome, so a client reads the same keys.
    assert set(out) == {"status", "assessment", "stale_after_hours", "reason", "message"}


async def test_preparing_starts_one_when_there_is_nothing_to_reuse(monkeypatch):
    from services import sage_assessment_service as sas
    fake = _FakeGenerator()
    monkeypatch.setattr(sas, "generate_full_assessment", fake)

    patient = _mk_patient()
    out = await ipass_service.prepare_reasoned_assessment(patient)

    assert out["status"] == "preparing"
    assert out["assessment"] is None
    assert out["message"]
    # Let the scheduled task actually run before the test ends.
    for _ in range(20):
        if fake.calls:
            break
        await asyncio.sleep(0.01)
    assert fake.calls == [(patient, ipass_service._shadow_safe_lane("handover"))]


async def test_a_prepared_assessment_is_recorded_under_a_lane_the_instrument_accepts(
        monkeypatch):
    """MEASURED 2026-08-14, and it is the reason this surface does not simply
    take `generate_full_assessment`'s default lane.

    `shadow_events` constrains `lane` to six values and `handover` is not one of
    them. `open_episode` fails OPEN — so it returns a non-null episode id having
    written nothing, and caches that id in its process-local registry. Two
    things then break silently: every Tier 3 draft prepared here is invisible to
    the pilot's only evidence instrument, and the NEXT REAL VIGIL ALERT for that
    patient resolves onto a phantom episode whose `episode.opened` row was never
    written.
    """
    from services import sage_assessment_service as sas
    from services import shadow_service

    fake = _FakeGenerator()
    monkeypatch.setattr(sas, "generate_full_assessment", fake)

    patient = _mk_patient()
    await ipass_service.prepare_reasoned_assessment(patient)
    for _ in range(20):
        if fake.calls:
            break
        await asyncio.sleep(0.01)

    lane = fake.calls[0][1]
    assert lane in shadow_service.LANES, (
        f"lane {lane!r} would be dropped by the shadow instrument, and "
        f"open_episode would cache a phantom episode id for this patient")

    # The property that actually matters: an episode opened on this lane leaves
    # a row behind, so a later alert joins something real.
    other = _mk_patient()
    episode_id, _ = shadow_service.open_episode(other, lane, trigger="handover_prepare")
    with db_session() as db:
        rows = db.execute(
            "SELECT COUNT(*) c FROM shadow_events WHERE episode_id = ?",
            (episode_id,)).fetchone()["c"]
    assert episode_id and rows > 0, (
        "the episode was minted and cached but nothing was written — a later "
        "alert for this patient would hang off an episode that does not exist")


def test_the_lane_guard_self_heals_when_the_vocabulary_grows():
    """Written as a lookup, not a constant: the day `handover` is added to
    LANES and the CHECK, this surface starts using it with no edit here."""
    from services import shadow_service

    assert ipass_service._shadow_safe_lane("handover") == ipass_service.PREPARE_LANE_FALLBACK
    monkey = frozenset(shadow_service.LANES | {"handover"})
    original = shadow_service.LANES
    try:
        shadow_service.LANES = monkey
        assert ipass_service._shadow_safe_lane("handover") == "handover"
    finally:
        shadow_service.LANES = original


async def test_preparing_returns_immediately_and_never_awaits_the_model(monkeypatch):
    """THE PROPERTY THE WHOLE RELOCATION RESTS ON. 46 seconds is acceptable
    here only because nobody is holding still for it. If this call ever awaited
    the generation, the handover would have inherited the exact defect that made
    the bedside version useless."""
    from services import sage_assessment_service as sas
    fake = _FakeGenerator()
    fake.release = asyncio.Event()          # the generation never finishes
    monkeypatch.setattr(sas, "generate_full_assessment", fake)

    patient = _mk_patient()
    out = await asyncio.wait_for(
        ipass_service.prepare_reasoned_assessment(patient), timeout=2.0)
    assert out["status"] == "preparing"

    # And the handover itself renders while the generation is still hanging.
    draft = build_ipass(patient)
    assert draft["sections"]["ai_assessment"] is None
    assert "I-PASS SHIFT HANDOFF" in draft["plain_text"]

    fake.release.set()
    await asyncio.sleep(0)
    for task in list(ipass_service._PREPARE_INFLIGHT):
        task.cancel()


async def test_preparing_twice_does_not_start_two_generations(monkeypatch):
    """THE DEDUPE. A handover generation carries no `message_id`, so the unique
    index that stops two drafts per alert does not apply — nothing but this
    check stops a double tap costing two 46-second generations."""
    from services import sage_assessment_service as sas
    fake = _FakeGenerator()
    monkeypatch.setattr(sas, "generate_full_assessment", fake)

    patient = _mk_patient()
    _mk_assessment(patient, state="generating", summary=None, age_minutes=0)

    first = await ipass_service.prepare_reasoned_assessment(patient)
    second = await ipass_service.prepare_reasoned_assessment(patient)

    assert first["status"] == second["status"] == "preparing"
    assert fake.calls == [], "a second generation was started for the same patient"


async def test_an_abandoned_generation_does_not_block_a_new_one(monkeypatch):
    """A row stuck in `generating` past the stale window is a process that died
    mid-generation. Treating it as in-flight forever would mean a killed
    appliance could never prepare that patient again."""
    from services import sage_assessment_service as sas
    fake = _FakeGenerator()
    monkeypatch.setattr(sas, "generate_full_assessment", fake)

    patient = _mk_patient()
    _mk_assessment(patient, state="generating", summary=None, age_minutes=120)

    out = await ipass_service.prepare_reasoned_assessment(patient)
    assert out["status"] == "preparing"
    for _ in range(20):
        if fake.calls:
            break
        await asyncio.sleep(0.01)
    assert [c[0] for c in fake.calls] == [patient]


async def test_preparing_is_honest_when_it_cannot_start(monkeypatch):
    """No silent no-op. Every refusal names its reason and carries ward-facing
    wording, the same contract `sage_assessment_service.UNAVAILABLE_MESSAGES`
    holds for the alert lane."""
    from services import sage_assessment_service as sas
    monkeypatch.setattr(sas, "_enabled", lambda: False)

    patient = _mk_patient()
    out = await ipass_service.prepare_reasoned_assessment(patient)
    assert out["status"] == "unavailable"
    assert out["reason"] == "disabled"
    assert out["message"] == ipass_service.PREPARE_MESSAGES["disabled"]
    assert out["assessment"] is None


async def test_backpressure_refuses_rather_than_queues_without_bound(monkeypatch):
    """A "prepare the whole ward" client must not turn this into an unbounded
    coroutine factory."""
    from services import sage_assessment_service as sas
    fake = _FakeGenerator()
    monkeypatch.setattr(sas, "generate_full_assessment", fake)
    monkeypatch.setattr(ipass_service, "_MAX_PREPARE_INFLIGHT", 0)

    patient = _mk_patient()
    out = await ipass_service.prepare_reasoned_assessment(patient)
    assert out["status"] == "unavailable"
    assert out["reason"] == "busy"
    assert fake.calls == []


# ═════════════════════════════════════════════════════════════════════════════
#  5. THE PRINTED SHEET STILL SPEAKS ENGLISH
# ═════════════════════════════════════════════════════════════════════════════
#
# The supplement adds five enums to a document that had already shipped one of
# them raw. Each parametrisation below includes a value that does not exist
# today: a translation TABLE alone would let the next addition leak straight
# onto the sheet, so the fallback is the generic humaniser.
@pytest.mark.parametrize("basis,expected", [
    ("model_cited", "Sage cited the guideline(s) listed below"),
    ("system_attributed", "Orb matched its wording to the guideline(s) listed below"),
    ("retrieved", "the wording could not be tied to any of them"),
    ("none", "no guideline in Orb's corpus matched this alert"),
    ("corpus_disabled", "corpus disabled"),
])
def test_no_machine_enum_reaches_the_printed_supplement_evidence(basis, expected):
    patient = _mk_patient()
    _mk_assessment(patient, citation_basis=basis)

    draft = build_ipass(patient)
    supplement = _supplement(draft["plain_text"])
    assert expected in supplement, supplement
    assert not _machine_tokens(supplement), (
        f"machine identifiers on a medico-legal handover: {_machine_tokens(supplement)}")
    # Raw value still on the wire for anything that matches on it.
    assert draft["sections"]["ai_assessment"]["citation_basis"] == basis


@pytest.mark.parametrize("confidence,expected", [
    ("high", "high confidence"),
    ("medium", "medium confidence"),
    ("low", "low confidence"),
    ("not_assessed", "not assessed"),
])
def test_no_machine_enum_reaches_the_printed_supplement_confidence(confidence, expected):
    patient = _mk_patient()
    _mk_assessment(patient, confidence=confidence)

    supplement = _supplement(build_ipass(patient)["plain_text"])
    assert f"Sage's own confidence: {expected}." in supplement
    assert not _machine_tokens(supplement), _machine_tokens(supplement)


@pytest.mark.parametrize("level,expected", [
    ("critical", "NEWS2 19 (critical band)"),
    ("elevated", "NEWS2 19 (elevated band)"),
    ("stable", "NEWS2 19 (stable band)"),
    ("not_scored", "NEWS2 19 (not scored)"),
])
def test_no_machine_enum_reaches_the_printed_supplement_severity(level, expected):
    patient = _mk_patient()
    _mk_assessment(patient, news2_level=level)

    supplement = _supplement(build_ipass(patient)["plain_text"])
    assert expected in supplement, supplement
    assert not _machine_tokens(supplement), _machine_tokens(supplement)


def test_no_none_and_no_action_enum_reaches_the_printed_supplement():
    """The `None` half of the same defect. A draft with no model recorded, no
    NEWS2, no `why` on its steps and no retrieved evidence is the sparsest row
    the table can hold — and every one of those gaps is a `None` waiting to be
    interpolated into a clinical sentence.

    `action_type` is the fifth enum: the sheet says what Orb can DO about a
    step, in English, rather than printing `start_medication` at a nurse.
    """
    patient = _mk_patient()
    _mk_assessment(
        patient, model=None, news2=None, news2_level=None, confidence=None,
        citation_basis=None, evidence=[],
        considerations=[{"key": "c0", "step": "Escalate to the medical registrar now",
                         "why": None, "orb_can_act": True, "action_id": "act-9",
                         "action_type": "start_medication"}],
        watch_items=[])

    draft = build_ipass(patient)
    supplement = _supplement(draft["plain_text"])

    assert supplement, "the sparsest possible row still has prose and must render"
    assert "None" not in supplement, supplement
    assert "null" not in supplement
    assert not _machine_tokens(supplement), _machine_tokens(supplement)
    assert "start_medication" not in supplement
    assert "one-click card" in supplement
    assert "Orb had no NEWS2 on file when this was written" in supplement
    # No model recorded → the author line simply omits it rather than saying None.
    assert "Author: Orb Sage (artificial intelligence)." in supplement
    assert "model" not in supplement.split("\n")[1]


def test_the_whole_sheet_is_still_free_of_machine_identifiers():
    """The sheet-wide invariant from tests/test_ipass.py, re-asserted WITH the
    supplement on it — that file's own copy passes vacuously because its patient
    has no assessment."""
    patient = _mk_patient()
    aid = _mk_assessment(patient, state="approved")
    _endorse(aid, "Dr Anand Rao", "doctor")

    draft = build_ipass(patient)
    assert AI_SUPPLEMENT_HEADING in draft["plain_text"]
    assert not _machine_tokens(draft["plain_text"]), _machine_tokens(draft["plain_text"])
    assert not _machine_tokens(draft["source_summary"]), _machine_tokens(draft["source_summary"])


# ═════════════════════════════════════════════════════════════════════════════
#  6. THE WARD ROUND'S HTTP CONTRACT
# ═════════════════════════════════════════════════════════════════════════════
def _app():
    """ONLY the I-PASS router — `main.app`'s lifespan would start the simulator
    and the model warmups, and every assertion here depends on the request being
    the only thing that ran."""
    from fastapi import FastAPI
    from routes.ipass_routes import router

    app = FastAPI()
    app.include_router(router)
    return app


def _auth(uid, role="doctor"):
    from auth import create_token
    return {"Authorization": f"Bearer {create_token(uid, role)}"}


def _mk_user(department: str, role: str = "doctor") -> str:
    uid = f"RELOCU{uuid.uuid4().hex[:8].upper()}"
    _CREATED_USERS.append(uid)
    with db_session() as db:
        db.execute("INSERT INTO users (id, name, role, password_hash, department) "
                   "VALUES (?, 'Round Doctor', ?, 'x', ?)", (uid, role, department))
    return uid


def test_the_round_endpoint_serves_the_same_block_as_the_handover():
    """The round walks patient by patient and asks for one patient's reasoning.
    It is a pure DB read — no model, so it costs milliseconds however slow the
    appliance is — and it must not be able to word it differently."""
    from fastapi.testclient import TestClient

    patient = _mk_patient()
    _mk_assessment(patient)
    doctor = _mk_user(DEPARTMENT)

    with TestClient(_app()) as client:
        res = client.get(f"/api/ipass/{patient}/reasoned-assessment",
                         headers=_auth(doctor))
    assert res.status_code == 200, res.text
    body = res.json()

    assert body["patient_id"] == patient
    assert body["stale_after_hours"] == 12
    assert body["assessment"]["ai_drafted"] is True
    assert body["assessment"]["label"] == AI_DRAFTED_LABEL
    assert body["assessment"]["whats_happening"] == SUMMARY
    assert AI_SUPPLEMENT_HEADING in body["plain_text"]
    assert "NOT WRITTEN BY A CLINICIAN" in body["plain_text"]

    # Byte-identical to the block the handover sheet prints.
    assert body["plain_text"] == _supplement(build_ipass(patient)["plain_text"])


def test_the_round_endpoint_says_nothing_when_there_is_nothing_to_say():
    from fastapi.testclient import TestClient

    patient = _mk_patient()
    doctor = _mk_user(DEPARTMENT)

    with TestClient(_app()) as client:
        body = client.get(f"/api/ipass/{patient}/reasoned-assessment",
                          headers=_auth(doctor)).json()
    assert body["assessment"] is None
    assert body["plain_text"] == ""
    assert AI_SUPPLEMENT_HEADING not in body["plain_text"]


def test_the_round_can_ask_for_an_assessment_ahead_of_reading_it(monkeypatch):
    """The prepare call the round makes as it moves to the next patient. It
    returns at once; the 46 seconds are spent while the clinician is still at
    the previous bed."""
    from fastapi.testclient import TestClient
    from services import sage_assessment_service as sas

    fake = _FakeGenerator()
    monkeypatch.setattr(sas, "generate_full_assessment", fake)

    patient = _mk_patient()
    doctor = _mk_user(DEPARTMENT)

    with TestClient(_app()) as client:
        res = client.post(f"/api/ipass/{patient}/reasoned-assessment/prepare",
                          headers=_auth(doctor))
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] == "preparing"
    assert body["assessment"] is None
    assert body["message"]
    assert fake.calls and fake.calls[0][0] == patient


def test_preparing_an_assessment_needs_permission_to_use_sage(monkeypatch):
    """Reading a draft Sage already wrote is part of seeing the chart. Spending
    the appliance's GPU to write a new one is USING Sage, and the gate says so —
    strictly more than the GET beside it requires."""
    from fastapi.testclient import TestClient
    import permissions

    patient = _mk_patient()
    _mk_assessment(patient)
    doctor = _mk_user(DEPARTMENT)

    without_sage = set(permissions.ROLE_PERMISSIONS["doctor"]) - {"sage.query"}
    monkeypatch.setitem(permissions.ROLE_PERMISSIONS, "doctor", without_sage)

    with TestClient(_app()) as client:
        blocked = client.post(f"/api/ipass/{patient}/reasoned-assessment/prepare",
                              headers=_auth(doctor))
        readable = client.get(f"/api/ipass/{patient}/reasoned-assessment",
                              headers=_auth(doctor))
    assert blocked.status_code == 403, blocked.text
    assert readable.status_code == 200, "reading an existing draft is not using Sage"
    assert readable.json()["assessment"] is not None


def test_a_clinician_outside_the_ward_cannot_ask_for_an_assessment():
    """A cross-ward caller must not be able to spend this appliance's GPU on a
    patient they cannot see. 404, never 403 — a 403 confirms the patient
    exists."""
    from fastapi.testclient import TestClient

    patient = _mk_patient()
    outsider = _mk_user("Cardiology")

    with TestClient(_app()) as client:
        res = client.post(f"/api/ipass/{patient}/reasoned-assessment/prepare",
                          headers=_auth(outsider))
    assert res.status_code == 404, res.text

    with db_session() as db:
        started = db.execute(
            "SELECT COUNT(*) c FROM sage_assessments WHERE patient_id = ?",
            (patient,)).fetchone()["c"]
    assert started == 0, "a refused caller still started a generation"


def test_a_clinician_outside_the_ward_cannot_read_the_reasoned_assessment():
    """Same gate as the handover it is embedded in: 404, never 403 — a 403
    confirms the patient exists."""
    from fastapi.testclient import TestClient

    patient = _mk_patient()
    _mk_assessment(patient)
    outsider = _mk_user("Cardiology")

    with TestClient(_app()) as client:
        res = client.get(f"/api/ipass/{patient}/reasoned-assessment",
                         headers=_auth(outsider))
    assert res.status_code == 404, res.text
    assert SUMMARY not in res.text


# ═════════════════════════════════════════════════════════════════════════════
#  8. THE SCREEN AND THE PRINTOUT SAY THE SAME THING
#
#  D3, 2026-08-15: the server side of this shipped complete and was never
#  rendered. `GET /api/ipass/{id}/reasoned-assessment` returned 200 with
#  available:true and four sections, and the handover page contained the string
#  "Sage" ZERO times. It is now rendered by
#  src/components/handoff/ReasonedAssessment.tsx, on the handover sheet and the
#  ward round.
#
#  That component has to WRITE OUT the same connective sentences this module
#  prints — a React tree cannot render `_render_assessment_lines`' text — which
#  creates exactly the drift this module's own docstrings warn about: two
#  surfaces inventing two stories about one fact. So the client keeps its
#  wording in one exported constant and this test reads it, character for
#  character, and asserts every phrase is one this module actually prints.
#
#  Reword the printed sheet and the screen fails a test instead of silently
#  disagreeing with the document a clinician signed.
# ═════════════════════════════════════════════════════════════════════════════
_CLIENT_DIR = os.path.join(os.path.dirname(__file__), "..", "..",
                           "src", "components", "handoff")
#: The renderer, and the module holding the wording it prints.
_CLIENT_COMPONENT = os.path.join(_CLIENT_DIR, "ReasonedAssessment.tsx")
_CLIENT_COPY = os.path.join(_CLIENT_DIR, "reasonedAssessmentCopy.ts")

#: `key: 'value',` inside the ASSESSMENT_COPY object. Single-quoted with escaped
#: apostrophes, which is how the client writes the ASCII apostrophes this module
#: prints (a curly quote would read identically on screen and compare unequal).
_COPY_ENTRY = re.compile(r"^\s{2}(\w+):\s*\n?\s*'((?:[^'\\]|\\.)*)',\s*$", re.MULTILINE)


def _client_copy() -> dict[str, str]:
    with open(_CLIENT_COPY, encoding="utf-8") as fh:
        source = fh.read()
    start = source.index("export const ASSESSMENT_COPY")
    block = source[start:source.index("} as const;", start)]
    found = {k: v.replace("\\'", "'") for k, v in _COPY_ENTRY.findall(block)}
    assert found, "ASSESSMENT_COPY could not be read out of the client component"
    return found


def test_the_client_component_exists_and_declares_its_wording():
    """The defect itself, pinned: the endpoint was served and nothing rendered
    it. If this file disappears, Tier 3 has been relocated to nowhere again."""
    assert os.path.exists(_CLIENT_COMPONENT), (
        "the reasoned assessment has no renderer — it is served and unread, "
        "which is the whole of D3")
    assert os.path.exists(_CLIENT_COPY)
    copy = _client_copy()
    # Every phrase the component prints outside the server's own fields.
    assert set(copy) >= {
        "reproduced", "snapshotLead", "snapshotTail", "whatsHappening",
        "steps", "watch", "orbCanAct", "confidenceLead"}


def test_every_sentence_the_screen_prints_is_a_sentence_this_module_prints():
    patient = _mk_patient()
    _mk_assessment(patient)
    supplement = _supplement(build_ipass(patient)["plain_text"])
    assert supplement, "no supplement was carried, so this proves nothing"

    missing = [f"{key}: {phrase!r}" for key, phrase in _client_copy().items()
               if phrase not in supplement]
    assert not missing, (
        "the handover SCREEN prints wording the handover SHEET does not:\n  "
        + "\n  ".join(missing)
        + "\n(one of the two has been reworded — they are the same document)")


def test_the_screen_never_invents_a_second_disclaimer():
    """The three labelling constants are the safety property. The client is not
    allowed to carry its own copy of any of them — it must print the server's,
    so there is exactly one wording in the product and one place to change it."""
    source = ""
    for path in (_CLIENT_COMPONENT, _CLIENT_COPY):
        with open(path, encoding="utf-8") as fh:
            source += fh.read()
    for constant in (AI_SUPPLEMENT_HEADING, AI_DRAFTED_LABEL, AI_SUPPLEMENT_DISCLAIMER):
        assert constant not in source, (
            f"the client hard-codes {constant!r} instead of printing the "
            "server's — two copies of a medico-legal label is one copy too many")


#: `src/api/ipass.ts`, where the client declares the SHAPE it renders. The
#: wording test above catches a reworded sentence; this catches a renamed field,
#: which fails more quietly — TypeScript is perfectly happy with a payload that
#: never arrives, and the screen simply renders `undefined` or nothing at all.
_CLIENT_API = os.path.join(os.path.dirname(__file__), "..", "..",
                           "src", "api", "ipass.ts")
_TS_FIELD = re.compile(r"^\s{2}(\w+)[?]?:\s", re.MULTILINE)


def _client_interface(name: str) -> set[str]:
    with open(_CLIENT_API, encoding="utf-8") as fh:
        source = fh.read()
    start = source.index(f"export interface {name} {{")
    body = source[start:source.index("\n}", start)]
    return set(_TS_FIELD.findall(body))


def test_the_client_declares_every_field_this_module_actually_sends():
    """A field this module renames or drops does not break the build — the
    screen just renders nothing where a clinician expected the machine's
    reasoning, which is the exact failure mode D3 was."""
    patient = _mk_patient()
    _mk_assessment(patient)
    block = get_reasoned_assessment(patient)
    assert block, "no block was produced, so this proves nothing"

    declared = _client_interface("IpassReasonedAssessment")
    missing = sorted(set(block) - declared)
    assert not missing, (
        "the server sends fields the client has never heard of: "
        f"{missing} — src/api/ipass.ts is out of date with this module")
    invented = sorted(declared - set(block))
    assert not invented, (
        "the client declares fields the server does not send: "
        f"{invented} — the screen will render `undefined` for each of them")


def test_the_handover_payload_always_carries_the_assessment_key():
    """The client tests ONE key rather than inferring from absence. Dropping
    `ai_assessment` from the sections would make "no assessment" and "an older
    build of the server" indistinguishable on screen."""
    patient = _mk_patient()
    assert "ai_assessment" in build_ipass(patient)["sections"]
    assert build_ipass(patient)["sections"]["ai_assessment"] is None
    _mk_assessment(patient)
    assert build_ipass(patient)["sections"]["ai_assessment"] is not None
