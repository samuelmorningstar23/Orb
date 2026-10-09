"""Tests for Sage's deterministic action-mutation guardrail.

detect_mutation_intent (services/sage_service.py) is the hard, regex-based gate
that classifies whether a user is trying to make Sage mutate the clinical chart
(prescribe, change a dose, stop a drug, place an order, write a note). Sage is a
read-only decision-support assistant; this gate is what enforces the "no action
hallucination" rule before any LLM call. It is a pure function, so it is cheap to
pin against regressions — a loosened pattern that stops catching "prescribe ..."
would let Sage appear to take clinical actions it must never take.

The gate is deliberately two-sided, and BOTH sides are safety-relevant:

  UNDER-TRIGGER is a clinical safety failure. Until 2026-07-31 the stop/change
  pattern required a GENERIC noun after the verb
  ("stop|discontinue|dc … dose|medication|med|drug|therapy"), so a stop order
  that named the drug — "stop the heparin for this patient" — did not match. The
  deterministic lock never engaged, and the only remaining gate was the 27B model
  choosing to emit ACT|. A safety net must not depend on model compliance.

  OVER-TRIGGER degrades the product and is not free either: a hit makes
  routes/sage_routes.py buffer the whole turn (`mutation_locked` → nothing
  streams) and the action-state override REPLACES the visible answer with
  "…direct modifications are not currently supported…". Trip that on
  "How do you dose IV alteplase?" and the clinician gets a slow non-answer to a
  legitimate question.

So the tables below are the contract in both directions.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services import sage_service
from services.sage_service import detect_mutation_intent, register_formulary_terms


# ══════════════════════════════════════════════════════════════════════
# MUST TRIP — orders. Missing one of these is a clinical safety failure.
# ══════════════════════════════════════════════════════════════════════

# Pre-2026-07-31 behaviour. Pinned so the widening never costs the old recall.
LEGACY_ORDERS = [
    "Add paracetamol 500mg to Ajay's chart",
    "prescribe amoxicillin for the patient in bed 3",
    "start him on antibiotics",
    "increase the dose of his medication",
    "stop the therapy",
    "d/c the drug",
    "place order for a CBC",
    "update chart with these vitals",
    "write note: patient stable",
    "give her another dose of acetaminophen",
]

# THE FIX: a stop/hold order that names the DRUG instead of a generic noun.
# Every one of these returned False before 2026-07-31.
DRUG_NAMED_STOP_ORDERS = [
    "stop the heparin for this patient",
    "discontinue the metoprolol",
    "hold the warfarin",
    "stop heparin",
    "stop the amlodipine",
    "hold the atorvastatin",
    "discontinue ciprofloxacin",
    "d/c the vancomycin",
    "dc the enoxaparin",
    "withhold the metoprolol this morning",
    "take him off the heparin",
    "take her off the noradrenaline",
    "hold his warfarin until the INR is back",
    "stop the heparin infusion",
    "pause the insulin drip",
    "stop the anticoagulation",
    "cancel the ceftriaxone",
    "omit the morning metoprolol",
]

# Dose/therapy CHANGES that name the drug — same blind spot, same fix.
DRUG_NAMED_CHANGE_ORDERS = [
    "increase the metoprolol to 50mg",
    "double his metoprolol",
    "reduce the enoxaparin dose",
    "switch him to oral metoprolol",
    "wean the noradrenaline",
    "titrate the noradrenaline to a MAP of 65",
    "restart the heparin at 1000 units an hour",
    "convert the heparin to enoxaparin",
]

# Drug-named ADD orders. "start him on heparin" also missed before the fix:
# the old add-pattern's object list was medication|med|tylenol|paracetamol|
# acetaminophen|antibiotic|dose|drug — no heparin, no ceftriaxone.
DRUG_NAMED_ADD_ORDERS = [
    "start him on heparin",
    "put him on ceftriaxone",
    "put her on ceftriaxone 1g",
    "commence heparin",
]

# Imperative form that is not sentence-initial, or is wrapped in a polite
# request. Still an instruction addressed to Sage.
POLITE_AND_EMBEDDED_ORDERS = [
    "please stop the heparin",
    "kindly discontinue the metoprolol",
    "can you stop the heparin",
    "can you stop the heparin?",
    "could you please hold the warfarin",
    "would you please stop the warfarin",
    "i want you to stop the heparin",
    "i need you to stop the heparin",
    "go ahead and stop the heparin",
    "let's stop the heparin",
    "ok stop the heparin",
    "just stop the heparin",
    "for this patient stop the heparin",
    "for Mr Sharma in bed 3, stop the heparin",
    "he is bleeding, stop the heparin",
    "The patient is bleeding. Stop the heparin.",
]

# Passive / deontic orders scoped to THIS patient.
DEONTIC_ORDERS = [
    "his heparin needs to be stopped",
    "the patient's warfarin should be held",
    "his metoprolol needs to be reduced",
]

# Verbs with no non-clinical reading — an order even with no object.
BARE_DISCONTINUATION_ORDERS = [
    "discontinue",
    "d/c",
    "deprescribe",
]

MUST_TRIP = (
    LEGACY_ORDERS
    + DRUG_NAMED_STOP_ORDERS
    + DRUG_NAMED_CHANGE_ORDERS
    + DRUG_NAMED_ADD_ORDERS
    + POLITE_AND_EMBEDDED_ORDERS
    + DEONTIC_ORDERS
    + BARE_DISCONTINUATION_ORDERS
)


@pytest.mark.parametrize("query", MUST_TRIP)
def test_mutation_intent_detected(query):
    assert detect_mutation_intent(query) is True


# ══════════════════════════════════════════════════════════════════════
# MUST NOT TRIP — questions. Tripping one of these blanks a real answer.
# ══════════════════════════════════════════════════════════════════════

# Pre-2026-07-31 behaviour, pinned.
LEGACY_INFORMATIONAL = [
    "What is the recommended dose of paracetamol for an adult?",
    "Summarize this patient's overnight vitals",
    "Why might this patient be tachycardic?",
    "What are the side effects of amoxicillin?",
    "How is sepsis diagnosed?",
    "hello",
    # Pinned by tests/test_sage_streaming.py: the deterministic gate is meant to
    # miss this phrasing, so the model's ACT| marker is what locks the stream.
    "he really needs paracetamol for the fever",
]

# THE HARD CASES named in the brief. Each contains a mutation verb AND a drug
# name and must still be answered as a question. What separates them from the
# orders above is grammatical FORM, not vocabulary: an interrogative lead
# (what/when/how/should I/is it/do I) instead of a verb at the clause head.
QUESTIONS_ABOUT_STOPPING = [
    "When should you stop heparin?",
    "when to stop heparin",
    "when should heparin be stopped",
    "should I stop the heparin?",
    "should we hold the warfarin before surgery?",
    "is it safe to stop the heparin?",
    "do I stop the heparin before theatre?",
    "why should heparin be stopped before surgery?",
    "what happens if we stop the heparin",
    "how long before surgery do we hold the warfarin",
    "would you stop heparin here?",
    "when do you discontinue the metoprolol",
    # Was a "terse order" from 31 July. Samuel's ruling of 9 October 2026: a
    # trailing "?" asks unless the message is polite (tests/order_gate_corpus.py).
    "stop the heparin?",
]

# Dosing / pharmacology questions — the brief's other two named cases.
DOSING_AND_PHARMACOLOGY_QUESTIONS = [
    "How do you dose IV alteplase?",
    "What causes a drop in haemoglobin?",
    "what does metoprolol do",
    "what are the side effects of metoprolol",
    "what is the half-life of enoxaparin",
    "how much heparin should I give?",
    "what is the loading dose of amiodarone",
]

# The verb is buried inside a NOUN PHRASE, not at a clause head. These are the
# cases clause-head anchoring exists to protect — an unanchored
# "<verb>.*<drug>" regex flags every one of them.
NOUN_PHRASE_QUERIES = [
    "reasons to stop metoprolol",
    "indications to hold the warfarin",
    "criteria for stopping heparin",
    "thoughts on stopping the heparin",
    "advice on holding the warfarin",
    "any reason to stop the heparin?",
    "the standing order for heparin",
    "what is the standing order for heparin?",
]

# A polite marker ("can you", "please") makes it a REQUEST, but the thing
# requested is still information — the mutation verb never reaches a clause head.
POLITE_INFORMATION_REQUESTS = [
    "can you tell me when to stop the heparin?",
    "can you explain how to stop heparin?",
    "please explain the heparin protocol",
    "please summarise the chart",
    "give me the census overview",
    "give me the metoprolol dose",
]

# Reports of what already happened, and third-party statements. Past tense and
# passive-without-a-patient are not instructions.
DECLARATIVE_NON_ORDERS = [
    "I stopped the heparin this morning",
    "he was started on heparin yesterday",
    "she was given metoprolol at 8am",
    "why was the metoprolol discontinued",
    "anticoagulation should be stopped 24 hours before surgery",
    "we should stop the heparin soon",
    "chart shows heparin was given",
]

# Ordinary conversation and non-medication instructions.
CHATTER_AND_NON_MED = [
    "hold on, what were the vitals",
    "hold on",
    "stop",
    "list the patients on heparin",
    "which patients are on warfarin?",
    "tell me about stopping heparin",
]

MUST_NOT_TRIP = (
    LEGACY_INFORMATIONAL
    + QUESTIONS_ABOUT_STOPPING
    + DOSING_AND_PHARMACOLOGY_QUESTIONS
    + NOUN_PHRASE_QUERIES
    + POLITE_INFORMATION_REQUESTS
    + DECLARATIVE_NON_ORDERS
    + CHATTER_AND_NON_MED
)


@pytest.mark.parametrize("query", MUST_NOT_TRIP)
def test_informational_queries_not_flagged(query):
    assert detect_mutation_intent(query) is False


# ══════════════════════════════════════════════════════════════════════
# Form separation, stated as its own property
# ══════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize(
    "order,question",
    [
        ("stop the heparin", "when should you stop heparin?"),
        ("discontinue the metoprolol", "when do you discontinue the metoprolol"),
        ("hold the warfarin", "should we hold the warfarin before surgery?"),
        ("increase the metoprolol", "how much do you increase the metoprolol by"),
        ("start him on heparin", "when do you start heparin"),
    ],
)
def test_same_verb_and_drug_split_by_sentence_form(order, question):
    """Identical verb + identical drug. Only the FORM differs, and the form is
    what decides — imperative at a clause head vs interrogative lead."""
    assert detect_mutation_intent(order) is True
    assert detect_mutation_intent(question) is False


def test_detection_is_case_insensitive():
    assert detect_mutation_intent("PRESCRIBE ANTIBIOTICS NOW") is True
    assert detect_mutation_intent("STOP THE HEPARIN") is True
    assert detect_mutation_intent("Stop The Heparin For This Patient") is True


@pytest.mark.parametrize("query", ["", "   ", None])
def test_empty_input_is_safe(query):
    assert detect_mutation_intent(query) is False


def test_whitespace_and_newlines_do_not_defeat_the_gate():
    assert detect_mutation_intent("stop   the\n heparin") is True


def test_gate_is_pure_and_repeatable():
    """It runs inline on the event loop for every Sage turn: no DB, no state."""
    q = "stop the heparin for this patient"
    assert [detect_mutation_intent(q) for _ in range(3)] == [True, True, True]


# ══════════════════════════════════════════════════════════════════════
# Formulary widening (helix_medication_master → gate lexicon)
# ══════════════════════════════════════════════════════════════════════

@pytest.fixture
def clean_formulary():
    """Snapshot/restore the module-level lexicon — register_formulary_terms is
    additive and global (it is loaded once at startup in production)."""
    saved_terms, saved_re = sage_service._FORMULARY_TERMS, sage_service._FORMULARY_RE
    sage_service._FORMULARY_TERMS, sage_service._FORMULARY_RE = frozenset(), None
    try:
        yield
    finally:
        sage_service._FORMULARY_TERMS, sage_service._FORMULARY_RE = saved_terms, saved_re


def test_brand_names_are_missed_until_the_formulary_is_registered(clean_formulary):
    """The built-in lexicon is international generics plus the common ward brands
    (services/order_wording.WARD_DRUG_NAMES, which since 9 October 2026 has Dolo
    and Monocef). Any other brand (Zinetac = ranitidine, Sporlac = a probiotic)
    is only reachable through the hospital's own helix_medication_master
    catalogue."""
    assert detect_mutation_intent("stop the Zinetac") is False
    assert detect_mutation_intent("hold the Sporlac") is False

    register_formulary_terms(["Zinetac 150mg Tablet", "Sporlac Tablet"])

    assert detect_mutation_intent("stop the Zinetac") is True
    assert detect_mutation_intent("hold the Sporlac") is True


def test_formulary_terms_do_not_break_the_question_side(clean_formulary):
    register_formulary_terms(["Dolo 650mg Tablet", "Monocef 1g Injection"])
    assert detect_mutation_intent("what is Dolo used for?") is False
    assert detect_mutation_intent("when should you stop Dolo?") is False
    assert detect_mutation_intent("reasons to stop the Monocef") is False


def test_formulary_loader_drops_strength_and_form_tokens(clean_formulary):
    """'stop the tablet'/'stop the 650mg' must not become mutation triggers just
    because a catalogue row spells its packaging out."""
    register_formulary_terms(["Dolo 650mg Tablet", "Sterile Water for Injection"])
    assert "tablet" not in sage_service._FORMULARY_TERMS
    assert "water" not in sage_service._FORMULARY_TERMS
    assert "650mg" not in sage_service._FORMULARY_TERMS
    assert "dolo" in sage_service._FORMULARY_TERMS


def test_formulary_registration_is_additive_and_idempotent(clean_formulary):
    n1 = register_formulary_terms(["Dolo 650mg Tablet"])
    n2 = register_formulary_terms(["Dolo 650mg Tablet"])
    assert n1 == n2
    n3 = register_formulary_terms(["Monocef 1g Injection"])
    assert n3 > n2
    # The built-in lexicon is the floor: registering nothing weakens nothing.
    register_formulary_terms([])
    assert detect_mutation_intent("stop the heparin") is True


def test_empty_formulary_leaves_the_built_in_gate_intact(clean_formulary):
    register_formulary_terms(None)
    assert detect_mutation_intent("stop the heparin for this patient") is True
    assert detect_mutation_intent("How do you dose IV alteplase?") is False
