"""Sage's trust numbers: confidence and citations must describe EVIDENCE.

THE TWO DEFECTS THESE PIN, measured across 40 eval turns (2026-08-14).

1. CONFIDENCE WAS INVERTED. routes/sage_routes.py carried:

       final_confidence = confidence
       if not valid_cited_sources and sources:
           final_confidence = "low"

   so the label was a function of whether the MODEL closed a <cited_sources>
   tag. Answers that retrieved three guideline documents read "low"; elliptical
   dosing follow-ups that retrieved NOTHING kept the no-retrieval fast path's
   hardcoded "high". The same question, the same three documents and identical
   fusion scores scored "medium" in one eval arm and "low" in the other — the
   only difference being whether that particular generation happened to emit a
   well-formed block.

2. CITATIONS NEVER APPEARED. 0 of 40 turns carried one, while 14 retrieved
   documents sat unattributed. Stock MedGemma honours the citation contract
   inconsistently, so a pipeline that can only attribute what the model NAMES
   will, on this model, attribute nothing at all.

Both are now decided by one measurable question — did the answer use the
retrieved text? — computed by sage_service.assess_answer_grounding and turned
into a label by sage_service.classify_answer_confidence.

The direction of every assertion below matters. Under-attribution costs a
clinician a reference they could have opened; OVER-attribution hands them a
citation that does not support the sentence it is attached to, which is worse:
it looks checkable and is not, and "a clinician can verify what they are told"
is this product's whole trust argument.
"""
import json
import sys
from pathlib import Path
from unittest import mock

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

with mock.patch("config.INGESTION_MODE", "hybrid"):
    from main import app

import routes.sage_routes as sage_routes
from services.sage_service import (assess_answer_grounding,
                                   classify_answer_confidence)

client = TestClient(app)


# ══════════════════════════════════════════════════════════════════════════════
#  THE REAL EVAL SHAPES
#
#  Three retrieved documents with the fusion scores the live corpus produces,
#  one local policy and two external — the exact shape of the turns that read
#  "low" while holding three guidelines.
# ══════════════════════════════════════════════════════════════════════════════

LOCAL_SEPSIS = {
    "document_id": "LOC-SEPSIS-001",
    "title": "Adult sepsis bundle",
    "content": ("Take blood cultures before antimicrobials. Administer intravenous "
                "ceftriaxone 2 g once daily. Measure serum lactate; repeat at 120 "
                "minutes if raised. Begin a 30 mL/kg crystalloid resuscitation and "
                "record hourly urine output."),
    "source": "Orb Demo Hospital",
    "source_type": "policy",
    "trust_tier": 1,
    "is_synthetic": False,
    "category": "Sepsis",
    "fusion_score": 0.0612,
}

EXT_VASOPRESSOR = {
    "document_id": "EXT-SEPSIS-004",
    "title": "Vasopressor initiation",
    "content": ("Where hypotension persists despite fluid resuscitation, commence "
                "noradrenaline peripherally targeting a mean arterial pressure of 65 "
                "mmHg. Insert central access within 6 hours."),
    "source": "Surviving Sepsis",
    "source_type": "guideline",
    "trust_tier": 3,
    "is_synthetic": False,
    "category": "Sepsis",
    "fusion_score": 0.0384,
}

EXT_STEWARDSHIP = {
    "document_id": "EXT-STEW-002",
    "title": "Antimicrobial stewardship review",
    "content": ("Every antimicrobial prescription is reviewed by the stewardship "
                "pharmacist at 72 hours, with de-escalation guided by culture "
                "sensitivities and procalcitonin trajectory."),
    "source": "NCDC AMR programme",
    "source_type": "guideline",
    "trust_tier": 3,
    "is_synthetic": False,
    "category": "Antimicrobials",
    "fusion_score": 0.0271,
}

EVAL_DOCS = [LOCAL_SEPSIS, EXT_VASOPRESSOR, EXT_STEWARDSHIP]

QUESTION = "what should I be doing for this deteriorating admission right now?"

# An answer that DEMONSTRABLY used the local policy: it carries that passage's
# antibiotic, its lactate timing, its fluid volume and its monitoring — none of
# which are in the question.
ANSWER_USING_LOCAL = (
    "Take blood cultures first, then give intravenous ceftriaxone 2 g. "
    "Check serum lactate now and repeat it at 120 minutes. Start 30 mL/kg of "
    "crystalloid and record hourly urine output."
)

# An answer that used NOTHING retrieved — generic advice, no passage-specific
# term in it at all.
ANSWER_USING_NOTHING = (
    "I would escalate to the senior clinician on duty and reassess the "
    "observations at the bedside before deciding anything further."
)


def _prompt_context(docs) -> str:
    """The rendered guideline block, as _get_guideline_context builds it."""
    lines = ["CHANNEL B (LOCAL POLICY/FORMULARY):"]
    for d in docs:
        lines.append(f"  [{d['category']}] {d['title']} (DOC: {d['document_id']})\n  {d['content']}")
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════════
#  (a) CONFIDENCE — a function of evidence, table-driven over the eval shapes
# ══════════════════════════════════════════════════════════════════════════════

#  (name, kwargs, expected_label, expected_basis)
CONFIDENCE_TABLE = [
    # THE MEASURED DEFECT. Three documents, good fusion scores, the answer used
    # them, and the model emitted no citation block. This read "low".
    ("retrieved_grounded_model_cited_nothing",
     dict(retrieval_confidence="high", source_count=3, grounded=True,
          query_scope="GENERAL_MEDICAL"),
     "high", "grounded_in_retrieval"),
    ("retrieved_grounded_medium_signals",
     dict(retrieval_confidence="medium", source_count=3, grounded=True,
          query_scope="PATIENT_ANCHORED"),
     "medium", "grounded_in_retrieval"),
    # THE OTHER HALF OF THE INVERSION: the elliptical dosing follow-up that
    # retrieved nothing and came back in 1.7 s reading "high".
    ("nothing_retrieved_clinical_followup",
     dict(retrieval_confidence="high", source_count=0, grounded=False,
          query_scope="GENERAL_MEDICAL"),
     "low", "no_evidence_retrieved"),
    ("nothing_retrieved_anchored_followup",
     dict(retrieval_confidence="high", source_count=0, grounded=False,
          query_scope="PATIENT_ANCHORED"),
     "low", "no_evidence_retrieved"),
    # Retrieval fired but the answer ignored it — model-only, and labelled so.
    ("retrieved_but_unused",
     dict(retrieval_confidence="high", source_count=3, grounded=False,
          query_scope="GENERAL_MEDICAL"),
     "low", "retrieved_evidence_unused"),
    # Weak signals stay weak even when the answer used them.
    ("weak_scores_grounded",
     dict(retrieval_confidence="low", source_count=3, grounded=True,
          query_scope="GENERAL_MEDICAL"),
     "low", "weak_retrieval_scores"),
    # HONEST LABELLING: a degraded answer stays degraded however good retrieval was.
    ("degraded_answer_with_perfect_retrieval",
     dict(retrieval_confidence="high", source_count=3, grounded=True,
          query_scope="GENERAL_MEDICAL", degraded=True),
     "low", "degraded_answer"),
    # The one part of the old formatting check worth keeping: the model named
    # documents and NOT ONE of them was retrieved. That is invention, not a
    # formatting slip.
    ("fabricated_citation",
     dict(retrieval_confidence="high", source_count=3, grounded=True,
          query_scope="GENERAL_MEDICAL", fabricated_citation=True),
     "low", "citation_not_in_retrieval"),
    # An incomplete chart still pulls high down to medium.
    ("grounded_but_chart_incomplete",
     dict(retrieval_confidence="high", source_count=3, grounded=True,
          query_scope="PATIENT_SPECIFIC", missing_critical_context=True),
     "medium", "grounded_in_retrieval"),
    # Census/aggregate answers come from the RECORD, not from retrieval.
    ("census_scope",
     dict(retrieval_confidence="high", source_count=0, grounded=False,
          query_scope="BROAD"),
     "high", "deterministic_scope"),
    ("cross_department_denial",
     dict(retrieval_confidence="high", source_count=0, grounded=False,
          query_scope="OUT_OF_SCOPE"),
     "high", "deterministic_scope"),
]


@pytest.mark.parametrize("name,kwargs,expected,basis",
                         CONFIDENCE_TABLE, ids=[r[0] for r in CONFIDENCE_TABLE])
def test_confidence_reflects_evidence_not_formatting(name, kwargs, expected, basis):
    label, why = classify_answer_confidence(**kwargs)
    assert (label, why) == (expected, basis), (
        f"{name}: evidence {kwargs} should read {expected!r} ({basis}), got "
        f"{label!r} ({why})")


def test_the_citation_tag_is_not_an_input_to_confidence():
    """The signature itself is the guarantee: there is no parameter through
    which 'did the model close a tag' could reach the label. The only
    citation-shaped input is `fabricated_citation`, which is about the model
    naming documents that do not exist — a content failure, not a format one."""
    import inspect
    params = set(inspect.signature(classify_answer_confidence).parameters)
    assert params == {
        "retrieval_confidence", "source_count", "grounded", "query_scope",
        "degraded", "fabricated_citation", "missing_critical_context",
    }


@pytest.mark.parametrize("retrieval_confidence", ["high", "medium", "low"])
@pytest.mark.parametrize("query_scope", ["GENERAL_MEDICAL", "PATIENT_ANCHORED",
                                         "PATIENT_SPECIFIC", "CONTEXTUAL_FOLLOWUP",
                                         "AMBIGUOUS"])
def test_nothing_retrieved_on_a_clinical_question_never_reads_high(
        retrieval_confidence, query_scope):
    """Whatever retrieval's own signal said, an answer with no documents behind
    it is not a high-confidence clinical answer."""
    label, _ = classify_answer_confidence(
        retrieval_confidence=retrieval_confidence, source_count=0,
        grounded=False, query_scope=query_scope)
    assert label != "high"


@pytest.mark.parametrize("retrieval_confidence", ["high", "medium"])
def test_a_grounded_answer_with_good_scores_is_at_least_medium(retrieval_confidence):
    """The founder's floor: retrieval fired, the scores cleared the calibrated
    bar, the answer used the passages — that is at least medium no matter what
    the model did with its tags."""
    label, basis = classify_answer_confidence(
        retrieval_confidence=retrieval_confidence, source_count=3,
        grounded=True, query_scope="GENERAL_MEDICAL")
    assert label in ("medium", "high"), f"{retrieval_confidence} → {label}"
    assert basis == "grounded_in_retrieval"


# ══════════════════════════════════════════════════════════════════════════════
#  (b) ATTRIBUTION — never for a passage the answer did not use
# ══════════════════════════════════════════════════════════════════════════════

def test_a_used_passage_is_attributed_and_an_unused_one_is_not():
    """The whole point, in one assertion. Three documents in the prompt, an
    answer built from ONE of them: that one is attributed and the other two are
    not."""
    g = assess_answer_grounding(
        answer=ANSWER_USING_LOCAL, sources=EVAL_DOCS, query=QUESTION,
        prompt_context=_prompt_context(EVAL_DOCS))
    assert g["attributed"] == ["LOC-SEPSIS-001"], g["per_source"]
    assert g["grounded"] is True
    assert g["per_source"]["EXT-SEPSIS-004"]["attributed"] is False
    assert g["per_source"]["EXT-STEW-002"]["attributed"] is False


def test_an_answer_that_used_nothing_attributes_nothing():
    g = assess_answer_grounding(
        answer=ANSWER_USING_NOTHING, sources=EVAL_DOCS, query=QUESTION,
        prompt_context=_prompt_context(EVAL_DOCS))
    assert g["attributed"] == []
    assert g["grounded"] is False


def test_echoing_the_question_is_not_evidence_that_a_passage_was_used():
    """An answer that only repeats the clinician's own words has demonstrated
    nothing about the documents sitting next to it."""
    question = ("should I give ceftriaxone and check the lactate and start "
                "crystalloid for this admission")
    echo = ("You should give ceftriaxone, check the lactate and start "
            "crystalloid for this admission.")
    g = assess_answer_grounding(
        answer=echo, sources=EVAL_DOCS, query=question,
        prompt_context=_prompt_context(EVAL_DOCS))
    assert g["attributed"] == [], g["per_source"]


def test_terms_the_answer_could_have_taken_from_the_chart_are_not_evidence():
    """The chart sits in the SAME prompt as the guidelines. A drug, a lab or a
    volume that appears in both is not proof the guideline was read — the answer
    could have got every word of it off the chart."""
    chart = {
        "demographics": "PATIENT: Ramaswamy Krishnamurthy, 64M",
        "medications": ["ceftriaxone 2 g IV once daily"],
        "labs": ["serum lactate 4.1", "urine output falling"],
        "notes": "crystalloid 30 mL/kg given in casualty",
    }
    answer = ("Continue the ceftriaxone 2 g and keep watching the serum lactate. "
              "The 30 mL/kg crystalloid is already running, so reassess her "
              "perfusion at the bedside.")

    # CONTROL: with no chart in the prompt, those same words ARE evidence — the
    # only place the answer could have found them is the guideline.
    without_chart = assess_answer_grounding(
        answer=answer, sources=EVAL_DOCS, query=QUESTION,
        prompt_context=_prompt_context(EVAL_DOCS))
    assert without_chart["attributed"] == ["LOC-SEPSIS-001"]

    with_chart = assess_answer_grounding(
        answer=answer, sources=EVAL_DOCS, query=QUESTION,
        prompt_context=_prompt_context(EVAL_DOCS), patient_context=chart)
    assert with_chart["attributed"] == [], (
        "every passage term in this answer was already on the chart — nothing "
        f"here shows the guideline was used: {with_chart['per_source']}")


def test_a_passage_clipped_out_of_the_prompt_is_never_attributed():
    """Passage bodies are clipped to a character budget before they reach the
    model (_get_guideline_context). Attributing a sentence the model was never
    shown would be a fabrication with a document id on it."""
    clipped_prompt = ("CHANNEL B (LOCAL POLICY/FORMULARY):\n"
                      "  [Sepsis] Adult sepsis bundle (DOC: LOC-SEPSIS-001)\n"
                      "  [passage omitted for length: cite by DOC_ID if relevant]")
    g = assess_answer_grounding(
        answer=ANSWER_USING_LOCAL, sources=[LOCAL_SEPSIS], query=QUESTION,
        prompt_context=clipped_prompt)
    assert g["attributed"] == []
    assert g["grounded"] is False


def test_a_short_decline_attributes_nothing():
    g = assess_answer_grounding(
        answer="I am not sure.", sources=EVAL_DOCS, query=QUESTION,
        prompt_context=_prompt_context(EVAL_DOCS))
    assert g["attributed"] == []


def test_one_coincidental_term_is_not_enough_to_attribute():
    """Two independent distinctive hits is the bar; one is a coincidence. This is
    the over-attribution guard — an over-attributed answer is worse than an
    unattributed one."""
    g = assess_answer_grounding(
        answer=("Noradrenaline would be the next step if she stays hypotensive, "
                "but I would reassess at the bedside before committing to it."),
        sources=EVAL_DOCS, query=QUESTION,
        prompt_context=_prompt_context(EVAL_DOCS))
    assert g["attributed"] == [], g["per_source"]


def test_grounding_never_raises_on_malformed_sources():
    """A grounding failure must not be able to take out a clinical answer."""
    g = assess_answer_grounding(answer=ANSWER_USING_LOCAL,
                                sources=[None, {}, {"document_id": "X"}, "junk"],
                                query=QUESTION)
    assert g["attributed"] == []
    assert g["grounded"] is False


# ══════════════════════════════════════════════════════════════════════════════
#  (c) END TO END — the same evidence must score the same on the wire
# ══════════════════════════════════════════════════════════════════════════════

def _login() -> str:
    r = client.post("/api/auth/login",
                    json={"employee_id": "D001", "password": "password"})
    assert r.status_code == 200
    return r.json()["token"]


def _collect(resp_text: str) -> list:
    events = []
    for frame in resp_text.split("\n\n"):
        frame = frame.strip()
        if frame.startswith("data: "):
            try:
                events.append(json.loads(frame[6:]))
            except json.JSONDecodeError:
                pass
    return events


def _fake_stream(payload: str):
    async def fake(prompt, context="", conversation_history=None, scope="simple", **kwargs):
        info = kwargs.get("route_info")
        if info is not None:
            info.update(lane="local", provider="ollama", model="test")
        for i in range(0, len(payload), 40):
            yield payload[i:i + 40]
    return fake


def _fake_retrieval(docs, confidence="high", query_scope="GENERAL_MEDICAL",
                    scope="simple"):
    """Stand in for get_relevant_context with a KNOWN evidence state."""
    def fake(query, user, patient_id=None, history=None):
        return (_prompt_context(docs) if docs else "", scope, list(docs),
                confidence, False, None, query_scope)
    return fake


def _answer_payload(answer: str, cited: list | None = None) -> str:
    """The fast (answer-first) contract this build actually emits."""
    block = json.dumps(cited) if cited is not None else None
    details = "<display_mode>concise</display_mode>"
    if block is not None:
        details += f"<cited_sources>{block}</cited_sources>"
    return f"NOACT|{answer}\n<details>\n{details}\n</details>"


def _run(answer, docs, cited=None, confidence="high",
         query_scope="GENERAL_MEDICAL", question=QUESTION):
    token = _login()
    with mock.patch.object(sage_routes, "query_ollama",
                           _fake_stream(_answer_payload(answer, cited))), \
         mock.patch.object(sage_routes, "get_relevant_context",
                           _fake_retrieval(docs, confidence, query_scope)):
        r = client.post("/api/sage/query",
                        headers={"Authorization": f"Bearer {token}"},
                        json={"query": question})
    assert r.status_code == 200
    metas = [e["metadata"] for e in _collect(r.text) if "metadata" in e]
    assert metas, r.text[:400]
    return metas[-1]


def test_a_grounded_answer_reads_medium_or_better_with_no_citation_block():
    """THE MEASURED DEFECT, end to end: three documents, good fusion scores, an
    answer built from them, and a model that emitted no <cited_sources> at all.
    This shipped "low"."""
    meta = _run(ANSWER_USING_LOCAL, EVAL_DOCS, cited=None)
    assert meta["confidence"] in ("medium", "high"), meta
    assert meta["confidence_basis"] == "grounded_in_retrieval"


@pytest.mark.parametrize("cited", [None, [], ["LOC-SEPSIS-001"]])
def test_identical_evidence_scores_identically_whatever_the_model_formatted(cited):
    """The eval's sharpest finding: the SAME question with the SAME three
    documents and identical fusion scores scored differently across arms purely
    because one generation closed a tag. All three formatting outcomes must now
    produce one label."""
    meta = _run(ANSWER_USING_LOCAL, EVAL_DOCS, cited=cited)
    assert meta["confidence"] == "high"
    assert meta["confidence_basis"] == "grounded_in_retrieval"


def test_nothing_retrieved_on_a_clinical_question_never_reads_high_on_the_wire():
    """The elliptical follow-up: no documents, fast, and it used to read
    "high" because the no-retrieval path hardcodes that label."""
    meta = _run("Give 1 g intravenously every 24 hours.", [], confidence="high")
    assert meta["confidence"] == "low"
    assert meta["confidence_basis"] == "no_evidence_retrieved"


def test_system_attributed_citations_appear_and_are_marked_as_such():
    """0 of 40 turns carried a citation while 14 documents sat unattributed. The
    deterministic fallback attributes what the answer demonstrably used — and
    labels it as ORB'S attribution, not the model's."""
    meta = _run(ANSWER_USING_LOCAL, EVAL_DOCS, cited=None)
    assert meta["citation_basis"] == "system_attributed"
    assert meta["attributed_sources"] == ["LOC-SEPSIS-001"]
    assert meta["attributed_count"] == 1

    by_id = {s["document_id"]: s for s in meta["sources"]}
    used = by_id["LOC-SEPSIS-001"]
    assert used["attribution"] == "system_attributed"
    # The source name and trust tier ride with it — that is what makes an
    # attribution checkable.
    assert used["source"] == "Orb Demo Hospital"
    assert used["trust_tier"] == 1
    # `cited` still means "the MODEL named it". Orb's attribution must never be
    # rendered under the badge that claims the model's.
    assert used["cited"] is False
    assert meta["cited_count"] == 0
    # The passages the answer did not use are shipped as context, not citation.
    assert by_id["EXT-SEPSIS-004"]["attribution"] == "context_only"
    assert by_id["EXT-STEW-002"]["attribution"] == "context_only"


def test_no_citation_is_attributed_for_a_passage_the_answer_did_not_use():
    """An answer that used none of the three retrieved documents attributes
    none of them, and says so: "retrieved", not "cited"."""
    meta = _run(ANSWER_USING_NOTHING, EVAL_DOCS, cited=None)
    assert meta["citation_basis"] == "retrieved"
    assert meta["attributed_count"] == 0
    assert meta["attributed_sources"] == []
    assert all(s["attribution"] == "context_only" for s in meta["sources"])
    # …and an answer that used nothing is not a confident answer.
    assert meta["confidence"] == "low"
    assert meta["confidence_basis"] == "retrieved_evidence_unused"


def test_a_real_model_citation_still_wins():
    """The fallback is a fallback. When the model names a retrieved document,
    that is the basis and Orb adds nothing of its own."""
    meta = _run(ANSWER_USING_LOCAL, EVAL_DOCS, cited=["LOC-SEPSIS-001"])
    assert meta["citation_basis"] == "model_cited"
    assert meta["cited_count"] == 1
    assert meta["attributed_count"] == 0
    by_id = {s["document_id"]: s for s in meta["sources"]}
    assert by_id["LOC-SEPSIS-001"]["attribution"] == "model_cited"
    assert by_id["LOC-SEPSIS-001"]["cited"] is True


@pytest.mark.parametrize("answer,docs,expected,basis,cite_basis", [
    (ANSWER_USING_LOCAL, EVAL_DOCS, "high", "grounded_in_retrieval",
     "system_attributed"),
    # Retrieval said "high"; the answer used none of it. The prose lane emitted
    # that raw "high" straight onto the wire.
    (ANSWER_USING_NOTHING, EVAL_DOCS, "low", "retrieved_evidence_unused",
     "retrieved"),
    # Nothing retrieved, and retrieval's own label is still "high".
    ("Give 1 g intravenously every 24 hours, adjusted for renal function.",
     [], "low", "no_evidence_retrieved", "none"),
])
def test_the_prose_lane_scores_the_same_evidence_the_same_way(
        answer, docs, expected, basis, cite_basis):
    """`quick` is the prose lane: it retrieves guidelines but returns before the
    XML parse, and it emitted retrieval's RAW confidence while the XML lane
    formatting-downgraded the identical turn. Two lanes must not disagree about
    the same evidence — that disagreement is half of what the eval measured."""
    token = _login()
    with mock.patch.object(sage_routes, "query_ollama", _fake_stream(answer)), \
         mock.patch.object(sage_routes, "get_relevant_context",
                           _fake_retrieval(docs, "high", "GENERAL_MEDICAL",
                                           scope="quick")):
        r = client.post("/api/sage/query",
                        headers={"Authorization": f"Bearer {token}"},
                        json={"query": QUESTION})
    assert r.status_code == 200
    meta = [e["metadata"] for e in _collect(r.text) if "metadata" in e][-1]
    assert meta["confidence"] == expected, meta
    assert meta["confidence_basis"] == basis
    assert meta["citation_basis"] == cite_basis


def test_a_fabricated_citation_is_dropped_and_labelled_honestly():
    """Measured 2026-08-03: 3 of 4 citations the model produced were invented
    identifiers. A citation that points at nothing never leaves the server, and
    the turn that produced it is not a confident turn."""
    meta = _run(ANSWER_USING_LOCAL, EVAL_DOCS, cited=["SSC23", "EXT"])
    assert meta["cited_count"] == 0
    assert meta["confidence"] == "low"
    assert meta["confidence_basis"] == "citation_not_in_retrieval"
