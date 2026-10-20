"""Orb Hospital OS — the guideline-retrieval QUERY.

WHAT THIS FIXES (DEF-4, measured on the ward during the 2026-08-14 dry run):
"Ventilator Weaning Protocol" was cited as evidence on EVERY drafted
assessment — a DKA patient, a dengue patient, a CAP-with-sepsis patient and a
UTI patient alike.

THE CAUSE WAS NOT THE RETRIEVER. IT WAS THE QUERY. Both lanes built it from the
NEWS2 drivers and nothing else:

    sage_assessment_service._generate      " ".join(prep["news2_drivers"])
    nervous_system_triggers.on_critical…   " ".join(drivers)

and NEWS2 drivers are PURE PHYSIOLOGY — "Tachypnea (RR 24)", "Tachycardia
(HR 122)", "Low SpO2 (94%)", "Pyrexia (Temp 39.1)". They say how sick the
patient is and not one word about what the patient HAS. Four unrelated
conditions presenting with similar vitals therefore sent the SAME query text
and retrieved the SAME documents; and because "SpO2" appears in exactly one of
the built-in corpus documents — the ventilator weaning protocol — the lexical
lane matched that one for essentially every vitals-shaped query.

The relevance floor (clinical_knowledge.apply_relevance_floor) stopped most of
the wrong citations, which is the honest failure mode. But "no guideline in
Orb's corpus matched this alert" for a patient whose guideline is sitting IN
the corpus is still a miss, and no floor can rescue a query that never said
what the patient has. Measured on the seeded corpus with the cross-encoder:

    "Tachypnea (RR 28) Tachycardia (HR 118)"                       no evidence
    "Diabetic ketoacidosis. Tachypnea (RR 28), Tachycardia (…)"    EXT-METAB-001  0.584
    "Tachypnea (RR 28) Low SpO2 (91%) Hypotension (SBP 85) …"      no evidence
    "CAP with early sepsis. Tachypnea (RR 28), Low SpO2 (91%) …"   EXT-ID-001     0.864

So the CLINICAL PICTURE leads — the working diagnosis, then the active problem
list — and the drivers follow as SECONDARY terms. They are kept rather than
dropped because they measurably help: on the CAP case they moved the top
guideline from 0.658 to 0.864, and on the UTI case they lifted that patient's
antibiotic guideline from 0.078 (under the floor, discarded) to 0.244 (cited).

WHAT IS DELIBERATELY NOT IN THE QUERY: the demographics line. It is the one
field that reads like a summary of the patient, and it is what
`nervous_system_triggers._compile_context` used to fall back to — but it is
`PATIENT: <name>, <age>y/<sex>, Bed …, Ward: …, Diagnosis: <dx>`, so using it
sends a named human being's identity into the knowledge child (and into
whatever the retrieval lane logs) to retrieve a guideline that only the last
field could ever have matched. The diagnosis is read structurally instead.

Pure and dependency-free on purpose: both callers reach it from inside a thread
(the retrieval executor, the warm executor), and it must never be the thing
that drags the knowledge stack into an import.
"""

import re
from collections.abc import Iterable
from typing import Any

# The query a patient with NO stated condition and no drivers gets. Kept
# deliberately generic: it is what the alert lane sent before this module
# existed, and a query that names no condition should retrieve nothing rather
# than guess one. (The prefetch lane's old default was "sepsis protocol
# hemodynamic instability", which silently biased every driverless warm pool
# toward sepsis for patients who had no sepsis.)
FALLBACK_QUERY = "acute clinical deterioration"

# BOUNDS, not tuning knobs. The cross-encoder scores the query against each
# passage in one forward pass, so a long query dilutes every term in it; and
# the lexical lane ORs each token, so a long query matches more documents at
# lower precision. A working diagnosis plus a few active problems plus the
# drivers that fired the alert is the whole clinical picture a retrieval query
# needs — the rest of the chart is already in the prompt.
_MAX_PROBLEMS = 4
_MAX_DRIVERS = 6
_MAX_SEGMENT_CHARS = 120
_MAX_QUERY_CHARS = 320

# Only the words that would make two different conditions look like the same
# one to the dedup below. This is NOT the retrieval tokenizer — retrieval has
# its own (clinical_knowledge._rerank_terms), and importing it here would pull
# the knowledge stack into two hot import paths for four lines of regex.
_DEDUP_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "the",
        "with",
        "of",
        "on",
        "in",
        "for",
        "to",
    }
)


def _terms(text: str) -> set[str]:
    """Content tokens of `text`, for deciding whether one segment is already
    said by another. Single characters and digits are KEPT — "Type 1 DM" and
    "Type 2 DM" are different problems and must not collapse into one."""
    return {
        w for w in re.split(r"[^a-z0-9]+", (text or "").lower()) if w and w not in _DEDUP_STOPWORDS
    }


def _clean(value: Any) -> str:
    """One line of query text, or "". Free-text clinical fields arrive with
    newlines, doubled spaces and the occasional pasted paragraph."""
    text = " ".join(str(value or "").split())
    return text[:_MAX_SEGMENT_CHARS].strip()


def clinical_picture(facts: dict | None) -> list[str]:
    """What this patient HAS, most-specific first — the working diagnosis, then
    the active problem list.

    `facts` is `sage_service._assemble_patient_context`'s dict. Both fields are
    read structurally (`diagnosis`, `problems`) rather than parsed back out of
    the rendered demographics line, so a change to how the chart block READS
    cannot silently change what Orb RETRIEVES.

    Returns [] for a patient with no stated condition — a real state (an
    unlabelled emergency admission), and the caller falls back to the drivers.
    """
    if not isinstance(facts, dict):
        return []
    problems = facts.get("problems") or []
    if not isinstance(problems, (list, tuple)):
        problems = []
    out: list[str] = []
    seen: set[str] = set()
    for raw in [facts.get("diagnosis")] + list(problems)[:_MAX_PROBLEMS]:
        text = _clean(raw)
        if not text:
            continue
        terms = _terms(text)
        # A problem-list entry that repeats the working diagnosis ("CAP with
        # early sepsis" / "Community acquired pneumonia") adds no retrieval
        # signal and spends the query budget twice.
        if not terms or terms <= seen:
            continue
        seen |= terms
        out.append(text)
    return out


def _render(primary: list[str], secondary: list[str]) -> str:
    """Sentence-shaped, because the cross-encoder is a language model and reads
    the query as text. The punctuation is invisible to the lexical lane, which
    strips it before building the FTS MATCH."""
    head = ". ".join(primary)
    tail = ", ".join(secondary)
    return ". ".join(part for part in (head, tail) if part)


def build_guideline_query(
    facts: dict | None = None,
    drivers: Iterable[Any] | None = None,
    *,
    fallback: str = FALLBACK_QUERY,
) -> str:
    """The retrieval query for one patient: clinical picture first, drivers second.

    `drivers` is the NEWS2 driver list on both alert lanes. The prefetch warmer
    also passes a caller-supplied hint through it (a clinician's typed hint from
    the manual warm route), which is the same thing structurally: secondary
    terms that sharpen a query the clinical picture has already anchored.

    Never returns "" — a caller that got an empty query would send it straight
    to `search_guidelines`, and an empty FTS MATCH is a different failure than
    "this patient has nothing stated". Degrades in stated order: picture +
    drivers → picture → drivers → `fallback`.
    """
    primary = clinical_picture(facts)
    seen: set[str] = set()
    for seg in primary:
        seen |= _terms(seg)

    secondary: list[str] = []
    for raw in list(drivers or [])[:_MAX_DRIVERS]:
        text = _clean(raw)
        if not text:
            continue
        terms = _terms(text)
        # A driver already said by the picture ("Pyrexia" beside a diagnosis of
        # "Pyrexia of unknown origin") is dropped for the same reason as above.
        if not terms or terms <= seen:
            continue
        seen |= terms
        secondary.append(text)

    # Truncation drops WHOLE segments from the least important end, so the
    # query can never be cut mid-condition: drivers go first, then the tail of
    # the problem list. The working diagnosis is the last thing left standing.
    while len(_render(primary, secondary)) > _MAX_QUERY_CHARS and (secondary or len(primary) > 1):
        (secondary if secondary else primary).pop()

    # The slice is unreachable while _MAX_SEGMENT_CHARS <= _MAX_QUERY_CHARS (one
    # segment always fits); it is here so raising one bound without the other
    # cannot hand the retriever an unbounded query.
    return _render(primary, secondary)[:_MAX_QUERY_CHARS].strip() or fallback
