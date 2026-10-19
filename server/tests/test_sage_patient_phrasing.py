"""Sage must not answer about a named patient with no chart, or with the wrong one (F033).

Two halves of one defect, both found 2026-10-02:
  * the "asks for patient data" probe had a dead `diagnos` stem and missed
    "Summarise X", "Tell me about X", "Update on X", so a name nobody holds
    got a fast, confident answer with no chart and no warning;
  * the 0.70 fuzzy window matched ordinary words to short names ("drama" to
    Rama), and a catalogue token could make a patient with that name
    unnameable.
"""
import random
import string
import uuid
from datetime import datetime, timezone

import pytest

import services.clinical_knowledge as ck
import services.sage_service as sage
from database import db_session
from services.sage_service import _is_clinical_vocabulary, _name_match, _name_tokens, get_relevant_context

DEPT = "Phrasing Medicine"


@pytest.fixture(autouse=True)
def _no_live_retrieval(monkeypatch):
    monkeypatch.setattr(ck, "_cross_encoder", None)
    monkeypatch.setattr(ck, "_cross_encoder_checked", True)
    monkeypatch.setattr(ck, "search_guidelines", lambda *a, **k: [])


def _word(n=7):
    return "".join(random.choice(string.ascii_lowercase) for _ in range(n)).capitalize()


def _user():
    uid = f"PHU{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute("INSERT OR IGNORE INTO users (id, name, role, password_hash, department) "
                   "VALUES (?, 'Phrasing Doctor', 'doctor', 'x', ?)", (uid, DEPT))
    return {"id": uid, "role": "doctor", "department": DEPT, "name": "Phrasing Doctor"}


def _patient(name, bed):
    pid = f"PHP{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute("INSERT OR IGNORE INTO users (id, name, role, password_hash) "
                   "VALUES ('PHADM', 'Phrasing Admitter', 'doctor', 'x')")
        db.execute(
            "INSERT INTO patients (id, name, mrn, bed, ward, age, sex, diagnosis, status, admission_date) "
            "VALUES (?, ?, ?, ?, ?, 61, 'M', 'Pneumonia', 'admitted', ?)",
            (pid, name, f"MRN-{uuid.uuid4().hex[:8]}", bed, DEPT, datetime.now(timezone.utc).isoformat()))
        db.execute("INSERT INTO admissions (patient_id, department, ward, bed, status, admitted_by) "
                   "VALUES (?, ?, ?, ?, 'active', 'PHADM')", (pid, DEPT, DEPT, bed))
    return pid


# ── an invented name, every phrasing ─────────────────────────────────────────
PHRASINGS = [
    "Summarise {n}",
    "Summarize {n} for me",
    "Tell me about {n}",
    "Update on {n}",
    "Any update on {n}?",
    "Brief me on {n}",
    "Catch me up on {n}",
    "What is {n}'s diagnosis?",
    "What are {n}'s vitals?",
]


@pytest.mark.parametrize("phrasing", PHRASINGS)
def test_an_invented_name_gets_no_matching_patient(phrasing):
    user = _user()
    name = f"{_word()} {_word()}"
    context, *_rest, pctx, _qscope = get_relevant_context(phrasing.format(n=name), user)
    assert pctx is None, "a chart was attached for a name nobody holds"
    assert "NO MATCHING PATIENT" in context


def test_a_bed_alone_is_enough_to_warn():
    user = _user()
    context, *_rest, pctx, _q = get_relevant_context("Who is in bed 9917 and what is going on?", user)
    assert pctx is None
    assert "NO MATCHING PATIENT" in context


def test_asking_about_a_colleague_is_not_a_missing_patient():
    user = _user()
    context, *_ = get_relevant_context("Has Dr Ferreira been told about this ward?", user)
    assert "NO MATCHING PATIENT" not in context


def test_summarise_a_real_patient_by_name_and_bed_finds_the_patient():
    """From a fresh chat: no history, no open chart. Since bed locator attempt
    12 (Samuel, 6 October) a bed is always confirmed before its chart opens,
    a full name with it included, so Sage asks about exactly this patient;
    "yes" opens the chart."""
    user = _user()
    first, last = "Rajesh", _word()
    bed = f"GM-{random.randint(100, 999)}"
    pid = _patient(f"{first} {last}", bed)
    context, _scope, _src, _conf, _esc, pctx, qscope = get_relevant_context(
        f"Summarise {first} {last} in bed {bed}", user, history=[])
    assert qscope == "DISAMBIGUATE", qscope
    assert pctx is None
    assert context.startswith("Which patient do you mean?") and f"{first} {last}" in context
    assert "NO MATCHING PATIENT" not in context
    assert pid


# ── the fuzzy matcher ────────────────────────────────────────────────────────
@pytest.mark.parametrize("query", [
    "any drama on the ward tonight?",
    "what is the dose of ceftriaxone?",
    "check the ratio first",
])
def test_a_short_name_is_never_matched_to_an_ordinary_word(query):
    q = query.lower()
    for name in ("rama", "bose", "rao"):
        assert not _name_match(name, q, _name_tokens(q)), (name, query)


def test_a_short_name_still_matches_exactly():
    q = "how is rama this morning?"
    assert _name_match("rama", q, _name_tokens(q))


@pytest.mark.parametrize("name,query,expected", [
    ("om", "how is om doing", True),
    ("ed", "check ed's bp again", True),
    ("om", "is he on oxygen", False),
    ("ed", "send him to the red zone", False),
])
def test_a_two_letter_name_is_matched_exactly_and_only_exactly(name, query, expected):
    """The part loop takes parts of three letters or more, so below the fuzzy
    floor the window is the only path for a two-letter mononymous name. It must
    still find the name typed exactly, and nothing near it."""
    q = query.lower()
    assert _name_match(name, q, _name_tokens(q)) is expected, (name, query)


def test_a_long_name_keeps_its_asr_tolerance():
    q = "how is venkatesk doing"
    assert _name_match("venkatesh", q, _name_tokens(q))


def test_a_window_of_ward_vocabulary_is_never_a_name():
    q = "start insulin now"
    # 0.93 similar, and "insulin" is a word the ward owns.
    assert not _name_match("insulina", q, _name_tokens(q))


# ── catalogue terms do not shadow admitted patients ──────────────────────────
def test_a_patient_named_like_a_catalogue_term_stays_nameable(monkeypatch):
    monkeypatch.setattr(sage, "_FORMULARY_TERMS", frozenset({"kavyamol"}))
    monkeypatch.setattr(sage, "_CENSUS_NAME_TOKENS", frozenset())
    assert _is_clinical_vocabulary("kavyamol")

    _patient(f"Kavyamol {_word()}", f"PH-{random.randint(100, 999)}")
    with db_session() as db:
        sage._refresh_census_name_tokens(db)
    assert not _is_clinical_vocabulary("kavyamol")
    # A catalogue term nobody is named after is still vocabulary.
    monkeypatch.setattr(sage, "_FORMULARY_TERMS", frozenset({"kavyamol", "zzbrandx"}))
    assert _is_clinical_vocabulary("zzbrandx")
