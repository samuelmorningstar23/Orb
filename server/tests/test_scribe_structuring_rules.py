"""Scribe structuring must not invent a conversation or change clinical values (F028).

The old templates opened with "CRITICAL: You must extract ... a DIALOGUE
section", so a one-voice dictation came back with a doctor-patient exchange the
model made up, and nothing told it not to convert units or expand
abbreviations. With verification off by default the doctor could sign that.

The 4B obeys a format rule only as the LAST thing in the prompt (2026-09-07), so
the rules are pinned to come after the fenced dictation.
"""
import asyncio
import json
from pathlib import Path

import pytest

from services import scribe_service
from services.scribe_service import (
    STRUCTURING_PROMPTS,
    build_structuring_prompt,
    has_speaker_turns,
)

MONOLOGUE = (
    "67 year old male, day three COPD exacerbation. c/o SOB overnight. "
    "Temp 101 F, SpO2 89 percent on 2 litres. Plan: neb salbutamol QID, "
    "prednisolone 40 mg OD, review in the morning."
)
FIXTURES = Path(__file__).resolve().parent.parent / "scripts" / "fixtures" / "scribe"


def _fixture_transcript(name: str) -> str:
    return json.loads((FIXTURES / name).read_text())["transcript"]


@pytest.mark.parametrize("fmt", sorted(STRUCTURING_PROMPTS))
def test_a_monologue_is_never_asked_for_a_dialogue(fmt):
    prompt = build_structuring_prompt(MONOLOGUE, fmt)
    assert "DIALOGUE:\n" not in prompt
    assert "Doctor: [" not in prompt
    assert "Do not write a DIALOGUE section and do not invent a conversation" in prompt


@pytest.mark.parametrize("fmt", sorted(STRUCTURING_PROMPTS))
def test_a_labelled_conversation_keeps_its_dialogue(fmt):
    prompt = build_structuring_prompt(_fixture_transcript("case_01_chest_pain.json"), fmt)
    assert "DIALOGUE:\nDoctor: [" in prompt
    assert "Never invent a line, a question or a speaker" in prompt


@pytest.mark.parametrize("fmt", sorted(STRUCTURING_PROMPTS))
def test_the_rules_come_last_after_the_dictation(fmt):
    prompt = build_structuring_prompt(MONOLOGUE, fmt)
    fence_end = prompt.index("<<<END_DICTATION>>>")
    for rule in ("Never add a symptom", "Never convert units", "Never expand them",
                 "Do not write a DIALOGUE section"):
        assert prompt.index(rule) > fence_end, rule
    assert prompt.rstrip().endswith("with no preamble.")
    # Nothing ahead of the dictation still demands a dialogue.
    assert "CRITICAL" not in prompt[:fence_end]


def test_the_discharge_date_is_never_made_up():
    assert "Today's date" not in build_structuring_prompt(MONOLOGUE, "discharge")


@pytest.mark.parametrize("text, expected", [
    (MONOLOGUE, False),
    ("Patient: Ravi Kumar, bed 4. Known diabetic, sugars high overnight.", False),
    ("", False),
    ("Doctor: How is the pain? Patient: Worse at night.", True),
    ("Dr: Any fever?\nPt: No.", True),
    ("Speaker 1: Any fever? Speaker 2: No.", True),
])
def test_speaker_turn_detection(text, expected):
    assert has_speaker_turns(text) is expected


def test_every_fixture_conversation_is_detected():
    for path in FIXTURES.glob("*.json"):
        assert has_speaker_turns(json.loads(path.read_text())["transcript"]), path.name


def test_structure_transcript_sends_the_monologue_prompt(monkeypatch):
    """The live path uses the builder, not the bare template."""
    seen = {}

    class _Provider:
        hosted = True

        async def agenerate(self, prompt, **kw):
            seen["prompt"] = prompt
            return "SUBJECTIVE:\nSOB overnight."

    monkeypatch.setattr(scribe_service, "get_provider", lambda name: _Provider())
    out = asyncio.run(scribe_service.structure_transcript(MONOLOGUE, "soap"))
    assert out.startswith("SUBJECTIVE")
    assert "DIALOGUE:\n" not in seen["prompt"]
    assert seen["prompt"].rstrip().endswith("with no preamble.")
