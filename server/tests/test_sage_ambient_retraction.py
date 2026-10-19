"""Gate 5 retraction vocabulary — the deterministic half of the retraction fix.

WHAT THIS DOES AND DOES NOT PROVE
---------------------------------
The retraction defect had two halves:

  1. UPSTREAM (deterministic, tested here). "the lab rang — haemolysed sample,
     repeat is 4.1" matched nothing in `_SIGNAL_PATTERNS`, so Gate 5 dropped it
     and it never reached the classifier at all, while the original "potassium
     6.8, critical" matched `critical` and did. The correction was structurally
     invisible. That asymmetry is pure regex and is locked down below.

  2. DOWNSTREAM (model behaviour + memory merge, NOT tested here). Whether the
     4B then classifies it correctly, and whether `_update_memory` actually
     retires the stale `current_concern` and `risk_flags`, needs a live model
     and is measured by tests/manual/retraction_harness.py.

So a green run of this file means the retraction now REACHES the classifier.
It does not mean the summary is safe. Do not read it as the latter.
"""
import re

import pytest

from services.sage_ambient_service import (
    _CLASSIFY_SYSTEM,
    _build_system,
    _gate_content,
    _gate_signal,
)


# Real phrasings a ward uses to withdraw a result. Every one of these must reach
# the classifier; each was silently dropped before the fix.
RETRACTION_PHRASINGS = [
    "The lab rang — haemolysed sample, repeat is 4.1",
    "Lab called, that sample was hemolysed. Repeat potassium came back 4.1, normal.",
    "Please ignore my last message about the potassium, the tube was mislabelled.",
    "Disregard the 07:00 result — wrong patient's sample went into the analyser.",
    "That potassium was spurious, the repeat shows 4.1.",
    "Amended report from the lab: the earlier potassium is retracted.",
    "The earlier result is superseded by the redraw, which is normal.",
    "Correction: the sodium I quoted was from the contaminated sample.",
    "The 6.8 is not accurate — it was an artefact of the draw. Recollect sent.",
    "Updated result just came through, the first one was not valid.",
]

# Ordinary ward chatter that must NOT be dragged into an LLM call by the new
# vocabulary. The gate is a cost control as well as a filter: every extra match
# is a 4B inference on the single GPU slot.
BENIGN_NON_SIGNAL = [
    "Family visiting this afternoon, they asked for a quiet side room.",
    "Physio came by and she managed to sit out in the chair for an hour.",
    "Notes filed and the whiteboard is updated for the ward round.",
    "The trolley is restocked and I signed the fridge temperature chart.",
    "Handover sheet is printed, I left a copy at the nurses' station.",
]


@pytest.mark.parametrize("text", RETRACTION_PHRASINGS)
def test_retraction_phrasings_reach_the_classifier(text):
    """Gate 5 must let a withdrawal through.

    This is the safety-critical direction. The message that RAISES an alarm
    always carries alarming vocabulary ('critical', 'urgent'); the message that
    WITHDRAWS it often carries none. Dropping the second while keeping the first
    is what made Sage report a retracted potassium of 6.8 as a live emergency.
    """
    assert not _gate_content(text), f"Gate 3 dropped it as short/ack: {text!r}"
    assert _gate_signal(text), f"Gate 5 dropped the retraction: {text!r}"


@pytest.mark.parametrize("text", BENIGN_NON_SIGNAL)
def test_benign_chatter_is_still_filtered(text):
    """The retraction vocabulary must not turn routine chatter into LLM calls."""
    assert not _gate_signal(text), (
        f"Gate 5 now fires on benign chatter, costing a 4B inference: {text!r}"
    )


def test_the_original_failing_pair_is_now_symmetric():
    """The exact asymmetry from the incident: alarm passed, correction did not."""
    alarm = "Potassium 6.8, critical."
    correction = "the lab rang — haemolysed sample, repeat is 4.1"
    assert _gate_signal(alarm), "the alarming message should still pass"
    assert _gate_signal(correction), (
        "the correction must pass too — this asymmetry IS the bug"
    )


def _flat(text):
    """Collapse whitespace — the instructions are hard-wrapped in the source, so
    "NO\nLONGER TRUE" must still count as the phrase."""
    return re.sub(r"\s+", " ", text).lower()


def test_classifier_prompt_carries_the_retraction_override():
    """The prompt half of the fix must stay present and stay specific.

    A summariser that merely *mentions* a retraction while still asserting the
    old value is the dangerous outcome, so the instruction has to say both
    things: name what was withdrawn, and stop carrying it forward.

    Asserted against the ASSEMBLED prompt for a real retraction, because the
    section is no longer unconditionally part of _CLASSIFY_SYSTEM.
    """
    sys_prompt = _flat(_build_system("Lab rang — haemolysed sample, repeat is 4.1"))
    assert "retraction" in sys_prompt
    for required in ("no longer true", "summary_update", "concern_update"):
        assert required in sys_prompt, f"prompt lost its {required!r} instruction"


def test_retraction_section_is_withheld_from_ordinary_messages():
    """A message that is not a retraction must never see retraction vocabulary.

    Regression for a measured fabrication. The override section used to be in the
    base system prompt for EVERY classification. Live on 2026-08-05, across five
    12-message ward threads, 35 of 35 classifications that ran before the
    retraction message copied the section's worked example out verbatim — so the
    room narrative announced a withdrawn potassium while that potassium was still
    live, still being treated, and still charted as a 0.95-confidence risk.
    """
    ordinary = "Creatinine 2.1 this morning, up from 1.4 on admission."
    assert _build_system(ordinary) == _CLASSIFY_SYSTEM, (
        "an ordinary trend message was handed the RETRACTIONS OVERRIDE section"
    )
    flat = _flat(_build_system(ordinary))
    assert "retractions override" not in flat


def test_prompt_carries_no_transplantable_clinical_values():
    """No prompt the model ever sees may contain a copyable lab value.

    The 4B does not reliably distinguish an illustrative example from a fact
    about this patient. Any concrete analyte+number in the instructions is
    therefore a fabrication waiting to be written into a clinical record, so the
    instructions must contain none — for a retraction message or an ordinary one.
    """
    for content in ("Lab rang — haemolysed sample, repeat is 4.1",
                    "Obs at 0600: temp 38.4, HR 112."):
        flat = _flat(_build_system(content))
        # The exact string that leaked, and the shape of it.
        assert "the repeat is 4.1" not in flat
        assert "potassium 6.8" not in flat
        assert not re.search(r"\b(potassium|sodium|troponin|creatinine|platelets?)\b\s*"
                             r"(is|of|was)?\s*\d", flat), (
            "a concrete analyte+value in the prompt WILL be copied into a summary"
        )


def test_gate_five_has_no_catastrophic_wildcard():
    """A retraction pattern broad enough to match everything is not a filter.

    Guards against someone 'fixing' a future miss by widening the alternation
    until Gate 5 always fires — which would silently put every ward message on
    the single GPU slot.
    """
    from services.sage_ambient_service import _SIGNAL_PATTERNS

    nonsense = "zzz qqq wibble frobnicate"
    assert not _SIGNAL_PATTERNS.search(nonsense), (
        "Gate 5 matches arbitrary text — the alternation has a wildcard"
    )
    # And the benign set above must not all match either (already asserted
    # individually, restated here as a single ratio so a slow drift is visible).
    matched = sum(1 for t in BENIGN_NON_SIGNAL if _SIGNAL_PATTERNS.search(t))
    assert matched == 0, f"{matched}/{len(BENIGN_NON_SIGNAL)} benign messages now match"


def test_gate_five_keeps_its_leading_word_boundary():
    """The alternation must stay anchored at a word start."""
    from services.sage_ambient_service import _SIGNAL_PATTERNS

    assert _SIGNAL_PATTERNS.pattern.startswith("(?:^|\\b)"), \
        "Gate 5 lost its leading word-boundary anchor"


# Words that only appear in the pattern as WHOLE words (not deliberate stems
# like `wors`/`deteriorat`/`haemolys`), paired with an innocuous longer word
# they are a prefix of.
_WHOLE_WORD_PREFIX_TRAPS = [
    ("add", "The address label was printed."),
    ("add", "Additional blankets brought to the bay."),
    ("home", "She is homesick and wants her own pillow."),
    ("pain", "The painting above her bed fell down."),
    ("start", "Startled by the alarm but settled quickly."),
]


@pytest.mark.parametrize("token,text", _WHOLE_WORD_PREFIX_TRAPS,
                         ids=[f"{t}-in-{x.split()[1]}" for t, x in _WHOLE_WORD_PREFIX_TRAPS])
def test_whole_word_tokens_do_not_match_as_prefixes(token, text):
    from services.sage_ambient_service import _SIGNAL_PATTERNS

    m = _SIGNAL_PATTERNS.search(text)
    assert m is None or m.group(1).lower() != token, (
        f"{token!r} matched as a prefix in {text!r} — a wasted 4B inference"
    )


@pytest.mark.parametrize("text", [
    "Start treatment after the ward round.", "Treatment started this morning.",
    "Starting treatment after the ward round.", "Add treatment after the ward round.",
    "Treatment added this morning.", "Adding treatment after the ward round.",
    "Ready to go home.", "She reports pain.", "The leg is painful.",
    "Painfully limited movement.", "Painless swelling noted.",
    "He is worsening.", "She is deteriorating.", "The sample was haemolysed.",
])
def test_word_boundaries_preserve_clinical_forms_and_intentional_stems(text):
    assert _gate_signal(text)


# ─────────────────────────────────────────────────────────────────────
# The MEMORY layer. No model involved — these feed `_update_memory` the
# ideal classifier output and ask what the clinician-visible surface does
# with it. This isolates "did the model understand?" from "can the data
# model even represent a withdrawal?".
# ─────────────────────────────────────────────────────────────────────
ROOM = "chan-retr-unit"
PATIENT = "PAT-RETR-UNIT"


def _memory_row():
    from database import db_session
    with db_session() as db:
        row = db.execute(
            "SELECT * FROM sage_room_memory WHERE room_id = ?", (ROOM,)).fetchone()
        return dict(row) if row else None


@pytest.fixture
def clean_room():
    from database import db_session
    with db_session() as db:
        db.execute("DELETE FROM sage_room_memory WHERE room_id = ?", (ROOM,))
    yield
    with db_session() as db:
        db.execute("DELETE FROM sage_room_memory WHERE room_id = ?", (ROOM,))


def _msg(mid, content):
    return {"id": mid, "sender_name": "Dr. Meera Sharma",
            "sender_role": "doctor", "content": content}


# The classifier output for the CRITICAL message, shaped exactly as the prompt
# asks for it.
ALARM_AS_RISK = {
    "type": "risk_flag",
    "confidence": 0.9,
    "signal": "Critical hyperkalaemia reported",
    "risk_detail": "Severe hyperkalaemia — potassium 6.8, requires urgent treatment",
    "risk_rationale": "Potassium 6.8 with rising creatinine in AKI",
    "concern_update": "Severe hyperkalaemia (K 6.8) in AKI",
}

# The classifier output for the RETRACTION, shaped EXACTLY as the new
# "RETRACTIONS OVERRIDE" section of _CLASSIFY_SYSTEM demands — including the
# worked example it gives. This is the best case the prompt can produce.
PERFECT_RETRACTION = {
    "type": "summary_update",
    "confidence": 0.9,
    "signal": "Potassium 6.8 retracted as haemolysed; repeat normal",
    "summary_delta": "Potassium 6.8 is RETRACTED — haemolysed sample; the repeat "
                     "is 4.1 (normal). No hyperkalaemia.",
    "concern_update": "AKI on a background of CAP; no hyperkalaemia — the 6.8 was "
                      "a haemolysed sample and the repeat is 4.1",
}


def test_memory_can_represent_a_retraction_in_the_narrative(clean_room):
    """discussion_summary must at least carry the correction forward."""
    from services.sage_ambient_service import _update_memory

    _update_memory(ROOM, PATIENT, _msg(1, "Potassium 6.8, critical."),
                   ALARM_AS_RISK, None)
    _update_memory(ROOM, PATIENT,
                   _msg(2, "Lab rang — haemolysed, repeat is 4.1."),
                   PERFECT_RETRACTION, _memory_row())

    row = _memory_row()
    assert "RETRACTED" in (row["discussion_summary"] or ""), \
        "the correction never reached discussion_summary"


def test_concern_is_replaced_when_the_classifier_supplies_concern_update(clean_room):
    """The happy path: model volunteers concern_update, headline gets fixed."""
    from services.sage_ambient_service import _update_memory

    _update_memory(ROOM, PATIENT, _msg(1, "Potassium 6.8, critical."),
                   ALARM_AS_RISK, None)
    assert "6.8" in (_memory_row()["current_concern"] or "")

    _update_memory(ROOM, PATIENT,
                   _msg(2, "Lab rang — haemolysed, repeat is 4.1."),
                   PERFECT_RETRACTION, _memory_row())

    concern = _memory_row()["current_concern"] or ""
    assert "no hyperkalaemia" in concern.lower(), \
        f"current_concern was not corrected: {concern!r}"


# A realistic retraction NAMES what it withdraws. That is what lets the
# deterministic retirement pass connect it to the flag it kills.
REAL_RETRACTION_TEXT = (
    "The lab just rang about the 07:00 sample — it was haemolysed, so ignore "
    "that potassium. The repeat is 4.1, completely normal. There is no "
    "hyperkalaemia."
)


def test_stale_concern_is_cleared_when_classifier_omits_concern_update(clean_room):
    """`concern_update` is optional and nothing enforces it — so don't rely on it.

    sage_ambient_service.py only overwrites `current_concern` when the
    classifier volunteers `concern_update`. A 4B that identifies the retraction
    and writes a perfect `summary_delta` but returns `concern_update: null`
    used to leave the sidebar headline reading "Severe hyperkalaemia (K 6.8)".

    The deterministic retirement pass now clears it instead. Clearing, not
    rewriting: an empty headline is quiet, a wrong headline is dangerous, and
    the existing "no concern yet" fallback re-derives one from the next signal.
    """
    from services.sage_ambient_service import _update_memory

    retraction_without_concern = dict(PERFECT_RETRACTION)
    retraction_without_concern["concern_update"] = None

    _update_memory(ROOM, PATIENT, _msg(1, "Potassium 6.8, critical."),
                   ALARM_AS_RISK, None)
    assert "6.8" in (_memory_row()["current_concern"] or "")

    _update_memory(ROOM, PATIENT, _msg(2, REAL_RETRACTION_TEXT),
                   retraction_without_concern, _memory_row())

    row = _memory_row()
    concern = row["current_concern"] or ""
    assert "6.8" not in concern and "hyperkal" not in concern.lower(), (
        f"headline still asserts the retracted value: {concern!r}"
    )
    assert "RETRACTED" in (row["discussion_summary"] or ""), \
        "the narrative must still carry the correction"


def test_risk_flag_naming_the_retracted_value_is_retired(clean_room):
    """`risk_flags` was append-only; retirement is what the fix adds.

    `_merge_risk` only appends or updates, and nothing else in the codebase
    removes or resolves a flag (verified: risk_flags is written in
    sage_ambient_service and admission_service, read in relay_routes, deleted
    nowhere). So once the critical potassium was charted, the sidebar rendered a
    "Risk Detected / Active" card naming K 6.8 — confidence 0.9 clears the 0.80
    render threshold at SageAmbientSidebar.tsx:92 — for the rest of the
    admission, and no classifier output could take it down.

    After retirement the flag is still THERE (the audit trail that Sage once
    raised the alarm matters) but zeroed, so it no longer renders as active.
    """
    import json as _json
    from services.sage_ambient_service import _update_memory

    _update_memory(ROOM, PATIENT, _msg(1, "Potassium 6.8, critical."),
                   ALARM_AS_RISK, None)
    _update_memory(ROOM, PATIENT, _msg(2, REAL_RETRACTION_TEXT),
                   PERFECT_RETRACTION, _memory_row())

    flags = _json.loads(_memory_row()["risk_flags"] or "[]")
    live = [f for f in flags
            if "6.8" in _json.dumps(f) and (f.get("confidence") or 0) >= 0.80]
    assert not live, f"a retracted risk flag still renders as ACTIVE: {live}"

    retired = [f for f in flags if f.get("retracted")]
    assert retired, "the flag was dropped instead of retired — audit trail lost"
    assert retired[0]["prior_confidence"] == 0.9, \
        "the original confidence must be preserved for the audit trail"


def test_retirement_works_even_when_the_llm_was_never_called(clean_room):
    """The retirement pass reads the MESSAGE, not the classification.

    This is the case that matters most in production: the ambient tier is
    evicted or the LLM slot is busy, so `_update_memory` is called with
    `classification=None` (sage_ambient_service.py, the not-has_signal and
    sampling paths). Before the fix, a stale critical card would sit on the
    sidebar unchallenged precisely when Sage was least able to reason.
    """
    import json as _json
    from services.sage_ambient_service import _update_memory

    _update_memory(ROOM, PATIENT, _msg(1, "Potassium 6.8, critical."),
                   ALARM_AS_RISK, None)
    _update_memory(ROOM, PATIENT, _msg(2, REAL_RETRACTION_TEXT),
                   None, _memory_row())   # <- no classification at all

    flags = _json.loads(_memory_row()["risk_flags"] or "[]")
    live = [f for f in flags if (f.get("confidence") or 0) >= 0.80]
    assert not live, f"stale card survived an unclassified retraction: {live}"
    assert not (_memory_row()["current_concern"] or ""), \
        "stale headline survived an unclassified retraction"


def test_narrative_records_the_withdrawal_without_the_model(clean_room):
    """The summary must say it was withdrawn even when the LLM never ran.

    `summary_delta` — the only thing that used to write the correction into
    `discussion_summary` — comes from the classifier. When the ambient tier is
    evicted or the slot is busy, `_update_memory` is called with
    `classification=None` and the narrative keeps asserting the old value
    forever. Degraded mode is exactly when that is most dangerous, so the
    retirement pass writes the clinician's own words in verbatim.
    """
    from services.sage_ambient_service import _update_memory

    _update_memory(ROOM, PATIENT, _msg(1, "Potassium 6.8, critical."),
                   ALARM_AS_RISK, None)
    _update_memory(ROOM, PATIENT, _msg(2, REAL_RETRACTION_TEXT),
                   None, _memory_row())

    disc = _memory_row()["discussion_summary"] or ""
    assert "RETRACTED" in disc, f"narrative never records the withdrawal: {disc!r}"
    assert "haemolysed" in disc, "the reason for the withdrawal was dropped"
    assert "4.1" in disc, "the replacing value was dropped"


def test_narrative_note_is_not_duplicated_on_repeat_delivery(clean_room):
    """Relay replays and near-duplicate messages must not stack the note."""
    from services.sage_ambient_service import _update_memory

    _update_memory(ROOM, PATIENT, _msg(1, "Potassium 6.8, critical."),
                   ALARM_AS_RISK, None)
    for mid in (2, 3, 4):
        _update_memory(ROOM, PATIENT, _msg(mid, REAL_RETRACTION_TEXT),
                       None, _memory_row())

    disc = _memory_row()["discussion_summary"] or ""
    assert disc.count("RETRACTED by ward message") == 1, \
        f"the retraction note stacked: {disc!r}"


def test_no_narrative_note_when_nothing_was_actually_retired(clean_room):
    """A marker match that retires nothing must stay silent.

    Otherwise every message containing 'amended' or 'repeat is' would append
    noise to the summary of a room it has nothing to do with.
    """
    from services.sage_ambient_service import _update_memory

    _update_memory(ROOM, PATIENT, _msg(1, "Lactate 2.1, febrile."),
                   {"type": "risk_flag", "confidence": 0.9,
                    "signal": "Rising lactate",
                    "risk_detail": "Possible evolving sepsis — lactate 2.1"}, None)
    _update_memory(ROOM, PATIENT,
                   _msg(2, "Amended radiology report: the knee film is now reported."),
                   None, _memory_row())

    disc = _memory_row()["discussion_summary"] or ""
    assert "RETRACTED" not in disc, f"noise appended to the narrative: {disc!r}"


def test_retirement_does_not_touch_unrelated_risk_flags(clean_room):
    """Over-retirement would be worse than the bug.

    Silently clearing a live risk card is a safety hazard in its own right, so
    the pass only retires flags that share a distinguishing token with the
    retraction. A potassium retraction must leave a sepsis flag standing.
    """
    import json as _json
    from services.sage_ambient_service import _update_memory

    sepsis = {
        "type": "risk_flag", "confidence": 0.9,
        "signal": "Rising lactate",
        "risk_detail": "Possible evolving sepsis — lactate 2.1 and febrile",
        "risk_rationale": "Lactate rising with fever in pneumonia",
    }
    _update_memory(ROOM, PATIENT, _msg(1, "Potassium 6.8, critical."),
                   ALARM_AS_RISK, None)
    _update_memory(ROOM, PATIENT, _msg(2, "Lactate 2.1, febrile."),
                   sepsis, _memory_row())
    _update_memory(ROOM, PATIENT, _msg(3, REAL_RETRACTION_TEXT),
                   PERFECT_RETRACTION, _memory_row())

    flags = _json.loads(_memory_row()["risk_flags"] or "[]")
    sepsis_flags = [f for f in flags if "sepsis" in _json.dumps(f).lower()]
    assert sepsis_flags, "the sepsis flag vanished"
    assert not sepsis_flags[0].get("retracted"), \
        "a potassium retraction wrongly retired the sepsis flag"
    assert (sepsis_flags[0].get("confidence") or 0) >= 0.80, \
        "the sepsis flag was silently down-weighted"


def test_a_retraction_that_never_names_what_it_withdraws_cannot_be_matched():
    """HONEST LIMITATION, pinned so nobody assumes more than is true.

    Retirement links a retraction to a flag by a shared distinguishing token.
    A terse "Lab rang — haemolysed, repeat is 4.1" names neither the analyte nor
    the old value, so there is nothing to match against "potassium 6.8" and the
    flag is correctly left alone rather than guessed at.

    Matching this would mean retiring flags on generic lab vocabulary, which
    would silently clear unrelated live cards. Leaving it is the safer error,
    but it IS an error: the narrative carries the correction while the card does
    not. Closing it properly needs the classifier to name the retracted subject
    in a structured field.
    """
    from services.sage_ambient_service import (
        _retire_retracted_risk_flags, _RETRACTION_MARKER,
    )

    terse = "Lab rang — haemolysed, repeat is 4.1."
    assert _RETRACTION_MARKER.search(terse), "it is still detected AS a retraction"

    flags = [{"detail": "Severe hyperkalaemia — potassium 6.8", "confidence": 0.9}]
    flags, retired = _retire_retracted_risk_flags(flags, terse)
    assert retired == 0, (
        "the terse retraction matched a flag it never named — check that the "
        "stopword list has not been narrowed into a wildcard"
    )



# ─────────────────────────────────────────────────────────────────────
# Findings from the LIVE measurement (6 real 4B runs, 2026-08-05). Each of
# these was observed against the running appliance, not imagined.
# ─────────────────────────────────────────────────────────────────────

def test_retirement_survives_british_american_spelling():
    """The ward writes "hyperkalaemia"; medgemma writes "Hyperkalemia".

    Observed in all six live runs: the model's `risk_detail` uses US spelling
    while the ward message uses UK spelling. Before canonicalisation a flag
    reading "Hyperkalemia with ECG changes" was NOT retired by "...there is no
    hyperkalaemia" — it stayed at 0.95 and kept rendering as an ACTIVE risk.

    Retirement appeared to work in the live runs only because the model also
    happened to write "potassium" into the rationale. That is a coincidence, so
    this test removes it: neither the analyte name nor the number appears in the
    flag.
    """
    from services.sage_ambient_service import _retire_retracted_risk_flags

    retraction = ("The lab called back - that potassium of 6.8 was a HAEMOLYSED "
                  "SAMPLE. The repeat potassium is 4.1, which is normal. There is "
                  "no hyperkalaemia.")
    flags = [{"detail": "Hyperkalemia with ECG changes (peaked T waves).",
              "rationale": "Peaked T waves indicate cardiac risk.",
              "confidence": 0.95}]
    flags, retired = _retire_retracted_risk_flags(flags, retraction)
    assert retired == 1, "US-spelled flag not retired by a UK-spelled retraction"
    assert flags[0]["confidence"] == 0.0
    assert flags[0]["retracted"] is True


@pytest.mark.parametrize("detail,rationale", [
    ("Thrombocytopenia, platelets 42,000.", "Bleeding risk in dengue."),
    ("Rising creatinine, AKI.", "Renal impairment worsening."),
    ("Febrile 38.4 with tachycardia.", "Possible sepsis."),
])
def test_spelling_canonicalisation_does_not_widen_what_gets_retired(detail, rationale):
    """Canonicalising ae/oe -> e must not turn retirement into a wildcard.

    Silently clearing a live risk card is worse than leaving a stale one, so the
    potassium retraction must still leave every unrelated flag untouched.
    """
    from services.sage_ambient_service import _retire_retracted_risk_flags

    retraction = ("The lab called back - that potassium of 6.8 was a HAEMOLYSED "
                  "SAMPLE. The repeat potassium is 4.1, which is normal. There is "
                  "no hyperkalaemia.")
    flags = [{"detail": detail, "rationale": rationale, "confidence": 0.9}]
    flags, retired = _retire_retracted_risk_flags(flags, retraction)
    assert retired == 0, f"unrelated flag {detail!r} was wrongly retired"
    assert flags[0]["confidence"] == 0.9


def test_escalation_signal_with_risk_detail_charts_a_risk_card(clean_room):
    """The message announcing the emergency is often typed `escalation_signal`.

    Live, the critical "POTASSIUM 6.8, peaked T waves" message was classified
    escalation_signal WITH a populated risk_detail in five of six runs. The
    memory layer accepted `risk_flag` only, so the detail was discarded every
    time; in the one run where no later message happened to be classified
    risk_flag, a critical potassium produced NO risk card at all.
    """
    import json

    from services.sage_ambient_service import _update_memory

    escalation = {
        "type": "escalation_signal",
        "confidence": 0.95,
        "signal": "Critical potassium with ECG changes",
        "risk_detail": "Hyperkalemia (Potassium 6.8 mmol/L) with peaked T waves.",
        "risk_rationale": "Life-threatening arrhythmia risk.",
    }
    _update_memory(ROOM, PATIENT,
                   _msg(1, "Lab rang - POTASSIUM 6.8. ECG shows peaked T waves."),
                   escalation, None)

    flags = json.loads(_memory_row()["risk_flags"])
    assert flags, "an escalation_signal carrying a risk_detail charted no risk card"
    assert any("potassium" in f.get("detail", "").lower() for f in flags)
