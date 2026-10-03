"""Sage alert assessment copy: what the clinician reads on the panel.

Three defects in the words, found reading real drafts on the ward:

  * the antimicrobial-allergy hook "an infective picture: temperature is
    scoring" read as a line cut off mid-sentence once the model echoed it;
  * the prompt asks the model to cite guidelines by corpus id, and it wrote
    those ids into its prose ("give fluids per EXT-SEPSIS-001"), a code nobody
    on the ward can look up;
  * evidence was built per retrieved CHUNK, so two chunks of one guideline
    listed the same title twice (and gave the client two rows with one key).
"""
import json

from services import patient_specific_screen as pss
from services import sage_assessment_service as sas

SOURCES = [
    {"document_id": "EXT-SEPSIS-001", "title": "Surviving Sepsis Campaign",
     "source": "External guideline", "trust_tier": 3,
     "content": "Give 30 mL/kg crystalloid for hypotension or lactate of 4 or more."},
    {"document_id": "EXT-SEPSIS-001", "title": "Surviving Sepsis Campaign",
     "source": "External guideline", "trust_tier": 3,
     "content": "Take blood cultures before antibiotics where this causes no delay."},
    {"document_id": "LOC-SEPSIS-001", "title": "Sepsis Six, local policy",
     "source": "Hospital policy", "trust_tier": 1,
     "content": "Administer antibiotics within one hour of recognising sepsis."},
]

_IDS = ("EXT-SEPSIS-001", "LOC-SEPSIS-001", "DOC:")


def _identity(text):
    return text


# ── 1. The hook is a whole clause ────────────────────────────────────────────
def _allergy_hook(febrile: bool) -> str:
    tier1 = {"parameters": [{"parameter": pss.P_TEMP, "display": "39.2", "points": 2}]
             if febrile else []}
    allergy = {"substance": "Penicillin", "reaction": "anaphylaxis",
               "severity": "severe", "classes": frozenset({"penicillins"}),
               "recorded": None}
    problems = [] if febrile else [{"text": "Community acquired pneumonia"}]
    out = pss._rule_antimicrobial_allergy(pss._picture(tier1), [], [allergy], problems, None)
    assert len(out) == 1, "the fixture no longer fires the rule"
    return out[0]["hook"]


def test_the_febrile_allergy_hook_reads_as_a_complete_clause():
    assert _allergy_hook(febrile=True) == (
        "Infective picture: temperature is in the scoring range")


def test_the_diagnosis_allergy_hook_reads_as_a_complete_clause():
    assert _allergy_hook(febrile=False) == (
        "Infective picture: the working diagnosis is infective")


# ── 2. No corpus id reaches the prose ────────────────────────────────────────
def test_a_retrieved_id_becomes_its_title():
    assert sas._ids_to_titles(
        "Give fluids per EXT-SEPSIS-001 now.", SOURCES) == (
        "Give fluids per Surviving Sepsis Campaign now.")
    assert sas._ids_to_titles(
        "Antibiotics within the hour (DOC: LOC-SEPSIS-001).", SOURCES) == (
        "Antibiotics within the hour (Sepsis Six, local policy).")
    assert sas._ids_to_titles(
        "Cultures first [EXT-SEPSIS-001].", SOURCES) == (
        "Cultures first (Surviving Sepsis Campaign).")


def test_an_id_that_was_never_retrieved_is_dropped_not_shown():
    assert sas._ids_to_titles(
        "Escalate early (DOC: NICE_NG51).", SOURCES) == "Escalate early."


def test_prose_without_an_id_is_left_exactly_as_written():
    text = "NEWS2 7 : review within the hour ,  as charted."
    assert sas._ids_to_titles(text, SOURCES) == text
    assert sas._ids_to_titles(text, []) == text


def test_the_parser_writes_titles_into_every_prose_field():
    raw = json.dumps({
        "whats_happening": "Hypotension and fever, consistent with sepsis per EXT-SEPSIS-001.",
        "considerations": [{
            "step": "Complete the Sepsis Six within the hour (DOC: LOC-SEPSIS-001).",
            "why": "EXT-SEPSIS-001 puts antibiotics inside one hour.",
            "orb_can_act": False, "action_type": None, "target": None,
        }],
        "cited_sources": ["EXT-SEPSIS-001", "LOC-SEPSIS-001"],
        "watch_items": ["Lactate, which LOC-SEPSIS-001 asks for, is not on file."],
    })
    parsed = sas._parse_assessment(raw, {"retrieval_confidence": "high"}, SOURCES, _identity)
    assert parsed is not None

    prose = [parsed["summary"], *parsed["watch_items"]]
    for c in parsed["considerations"]:
        prose += [c["step"], c["why"] or ""]
    for line in prose:
        assert not any(i in line for i in _IDS), line
    assert "Surviving Sepsis Campaign" in parsed["summary"]
    assert "(Sepsis Six, local policy)" in parsed["considerations"][0]["step"]
    # The citations themselves still key on ids: that is what they are for.
    assert set(parsed["cited_doc_ids"]) == {"EXT-SEPSIS-001", "LOC-SEPSIS-001"}


# ── 3. One evidence row per document ─────────────────────────────────────────
def test_evidence_lists_each_document_once():
    out = sas._attribute("sepsis antibiotics within one hour", ["EXT-SEPSIS-001"],
                         {"retrieval_confidence": "high"}, SOURCES)
    ids = [e["document_id"] for e in out["evidence"]]
    assert ids == ["EXT-SEPSIS-001", "LOC-SEPSIS-001"]
    assert out["evidence"][0]["attribution"] == "model_cited"
