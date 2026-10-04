"""The PHI classifier's patient-identifier scan reads the question once (4 October 2026).

ai_governance._match_patient_identifiers ran one `\\bpart\\b` regex over the
whole question per name part per patient row (every status, so the discharged
roster too), and a substring test per ward and bed. A 512 KB question that
named nobody took 13 s per call with 2,000 rows, and classify_query_sensitivity
can make two calls on one chat turn. Same family as the Sage name matcher
(tests/test_sage_name_match_bounds.py), with no fuzzy step, so only the exact
half applies: the question's whole words are read once, and each distinct
ward/bed label is tested once.

Pinned here: the same answers as the old loop (frozen below), including the
known over-labelling the squashed-substring test makes, and a 512 KB question
classified in well under the old time.
"""
import random
import re
import time

import pytest

import database
import services.ai_governance as gov

#: Seconds, per call and per classify. Measured around a tenth of that on the
#: development Mac; the slack is for a loaded machine.
BUDGET_S = 2.0


# ── the oracle: the loop as it stood on 3 October 2026 ───────────────────────
def _old_match(rows, query_lower, include_ward=True):
    query_lower = gov._fold_homoglyphs(query_lower)
    norm_query = re.sub(r"[^a-z0-9]", "", query_lower)
    for r in rows:
        name = str(r["name"] or "").lower()
        name_parts = [p for p in re.split(r"[^a-zA-Z]", name)
                      if p and p not in ("mr", "mrs", "ms", "dr", "prof")]
        for part in name_parts:
            if len(part) > 2 and re.search(rf"\b{re.escape(part)}\b", query_lower):
                return True
        locs = (r["ward"], r["bed"]) if include_ward else (r["bed"],)
        for loc in locs:
            norm_loc = re.sub(r"[^a-z0-9]", "", str(loc or "").lower())
            if norm_loc and norm_loc in norm_query:
                return True
    return False


class _FakeDb:
    def __init__(self, rows):
        self.rows = rows

    def execute(self, *_args):
        return self

    def fetchall(self):
        return self.rows

    def close(self):
        pass


@pytest.fixture()
def table(monkeypatch):
    """Point the classifier at an in-memory patient table."""
    holder = {"rows": []}
    monkeypatch.setattr(database, "get_db", lambda: _FakeDb(holder["rows"]))

    def use(rows):
        holder["rows"] = rows
    return use


def _row(name, ward=None, bed=None):
    return {"name": name, "ward": ward, "bed": bed}


ROWS = [
    _row("Kavya Ramachandran", "ICU", "B-12"),
    _row("Mr. Suresh Reddy", "GM", "GM-103"),
    _row("Dr. Anand Menon", "Cardiology", "C 7"),
    _row("Mrs Lakshmi Iyer", "OR", "7"),
    _row("O'Shea", "HDU", "A1"),
    _row("S. Kumar", "Ortho", "OR-2"),
    _row("Om", "Paeds", None),
    _row("Raman2", "ICU-2", "12"),
    _row("José Álvarez", "Maternity", "M/04"),
    _row("Anne-Marie O'Neil", "", ""),
    _row(None, "ICU", "B-12"),
    _row("Prof Ed Li", None, "D-9"),
]

QUERIES = [
    "what is the empiric antibiotic for community acquired pneumonia",
    "how is kavya doing", "kavya's potassium", "kavyaramachandran", "ramachandran",
    "is reddy stable", "mr suresh reddy", "anand menon", "menon's echo",
    "mrs lakshmi iyer", "iyer", "o'shea in a1", "o'shea", "s. kumar's bp", "kumar",
    "om", "how is om", "raman2", "raman", "jose alvarez", "álvarez", "anne-marie",
    "o'neil", "ed li", "prof", "bed b-12", "b12", "gm103", "c 7", "c7 result",
    "is it okay for her", "the icu census", "icu-2 occupancy", "ward m/04",
    "hba1c is high", "after 12 hours", "dose of 7 mg", "nothing to see here",
    "Кavya Ramachandran", "ѕuresh",  # Cyrillic look-alikes
    "kavya_ramachandran", "kavya2", "2kavya", "kavyas",
    "",
]


def test_the_scan_gives_the_old_answers():
    """Every query against every single row and against the whole table, with
    and without wards. The answer is any-of, so the single-row runs are what
    would expose a per-row difference the whole-table run hides."""
    mismatches, answers = [], {True: 0, False: 0}
    rng = random.Random(4)
    queries = list(QUERIES) + [
        " ".join(rng.choice(QUERIES[:-1]) for _ in range(rng.randint(2, 6))) for _ in range(150)
    ]
    for q in queries:
        ql = gov._fold_homoglyphs(q.lower())
        for rows in [[r] for r in ROWS] + [ROWS]:
            for include_ward in (False, True):
                old = _old_match(rows, ql, include_ward)
                new = _new_match(rows, ql, include_ward)
                answers[old] += 1
                if old != new:
                    mismatches.append((q, [r["name"] for r in rows], include_ward, old, new))
    assert not mismatches, mismatches[:10]
    # Not vacuous: both answers occur often (measured 1,247 matches and 3,823
    # misses on 4 October 2026).
    assert answers[True] >= 500 and answers[False] >= 500, answers


def _new_match(rows, query_lower, include_ward):
    original = database.get_db
    database.get_db = lambda: _FakeDb(rows)
    try:
        return gov._match_patient_identifiers(query_lower, include_ward=include_ward)
    finally:
        database.get_db = original


def test_known_over_labelling_is_unchanged(table):
    """Ward "OR" squashed is "or", which sits inside "for": a question naming
    nobody reads as ward-scoped PHI. That errs toward keeping the turn local
    and is a ruling, not part of this change, so it is pinned as it was."""
    table([_row("Nobody Here", "OR", "Z-1")])
    q = "is it okay for her"
    assert gov._match_patient_identifiers(q, include_ward=True) is True
    assert _old_match([_row("Nobody Here", "OR", "Z-1")], q, include_ward=True) is True
    assert gov._match_patient_identifiers(q, include_ward=False) is False


def _hospital(rng, n):
    syllables = "ra me sh pri ya mee na kshi kan cha ve nka te la arj un dee pa an ha ri".split()

    def word():
        return "".join(rng.choice(syllables) for _ in range(3)).capitalize()
    wards = ["ICU", "GM", "Cardiology", "Ortho", "HDU", "Paeds", "Maternity", "Surgery"]
    beds = [f"{w[:2].upper()}-{i}" for w in wards for i in range(1, 26)]   # 200 beds
    return [_row(f"{word()} {word()}", rng.choice(wards), rng.choice(beds)) for _ in range(n)]


#: 512 KB with no pronoun, no bed reference and nothing on record, so both
#: identifier calls run to the end of the table.
NOBODY = ("qzx wvut " * (524_288 // 9)).strip()


def test_a_512kb_question_naming_nobody_is_scanned_in_seconds(table):
    rows = _hospital(random.Random(5), 2_000)
    table(rows)
    for include_ward in (False, True):
        start = time.perf_counter()
        hit = gov._match_patient_identifiers(NOBODY, include_ward=include_ward)
        elapsed = time.perf_counter() - start
        assert hit is False
        assert elapsed < BUDGET_S, f"{elapsed:.2f} s with {len(rows)} rows"


def test_a_512kb_question_is_classified_in_seconds(table):
    table(_hospital(random.Random(6), 2_000))
    start = time.perf_counter()
    c = gov.classify_query_sensitivity(NOBODY)
    elapsed = time.perf_counter() - start
    assert c.contains_phi is False, c.reasons
    assert elapsed < BUDGET_S, f"{elapsed:.2f} s"


def test_a_name_at_the_end_of_a_512kb_question_is_still_found(table):
    rows = _hospital(random.Random(7), 2_000)
    target = rows[1234]["name"].split()[1].lower()
    table(rows)
    assert gov._match_patient_identifiers(f"{NOBODY} {target}", include_ward=False) is True
