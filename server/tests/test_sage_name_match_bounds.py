"""Sage's patient-name matchers are bounded on long questions (4 October 2026).

MEASURED: a 50,000-character question that named a bed ("bed 7 bed 7 ...")
held a worker thread for 7.4 s, 22.8 of 22.9 s under the profiler inside
difflib. _name_match ran SequenceMatcher over every window of the question once
per patient (admitted and discharged, in and out of the department, and again
per history turn for the anchor), and the exact tests re-read the whole
question per patient as well. Nothing caps a Sage question below main.py's
512 KB body limit.

Pinned here:
  * the per-text index gives the SAME answers as the matchers as they stood on
    3 October 2026 (frozen below as the oracle) on any text within the cap;
  * past _FUZZY_MAX_TOKENS name tokens the fuzzy window stops and exact
    matching does not, on every lane: in scope, out of scope and the history
    anchor all go through the one matcher;
  * get_relevant_context on a 512 KB question finishes in a few seconds,
    including text built so that every window survives the cheap bounds.
"""
import difflib
import random
import re
import time
import uuid
from datetime import datetime, timezone

import pytest

import services.clinical_knowledge as ck
import services.sage_service as sage
from database import db_session
from services.sage_service import get_relevant_context

CAP = sage._FUZZY_MAX_TOKENS
#: A Sage question just under main.py's 512 KB request cap.
BIG = 500_000
#: Seconds. Measured well under one on the development Mac; the slack is for a
#: loaded machine, not for the matcher.
BUDGET_S = 3.0


@pytest.fixture(autouse=True)
def _no_live_retrieval(monkeypatch):
    monkeypatch.setattr(ck, "_cross_encoder", None)
    monkeypatch.setattr(ck, "_cross_encoder_checked", True)
    monkeypatch.setattr(ck, "search_guidelines", lambda *a, **k: [])


# ── the oracle: the three matchers as they stood on 3 October 2026 ───────────
def _old_tokens(query_lower):
    return re.findall(r'\b[a-z]+\b', sage._name_matching_text(query_lower))


def _old_whole_name_in_query(p_name, query_lower):
    p_name = (p_name or "").strip()
    if not p_name or sage._name_letters(p_name) < 2:
        return False
    return re.search(
        rf"(?<![a-z0-9]){re.escape(p_name)}(?![a-z0-9])",
        sage._name_matching_text(query_lower),
    ) is not None


def _old_name_match(p_name, query_lower, query_words):
    name_parts = [
        part for part in re.split(r'[^a-zA-Z]', p_name)
        if part and part not in ('mr', 'mrs', 'ms', 'dr', 'prof')
    ]
    for part in name_parts:
        if len(part) > 2 and re.search(rf'\b{re.escape(part)}\b', query_lower):
            return True
    letters = sum(len(part) for part in name_parts)
    if not name_parts or letters < 2:
        return False
    full_clean_name = " ".join(name_parts)
    window_size = len(name_parts)
    fuzzy = letters >= sage._FUZZY_MIN_LETTERS
    for i in range(len(query_words) - window_size + 1):
        tokens = query_words[i:i + window_size]
        window = " ".join(tokens)
        if not fuzzy:
            if window == full_clean_name:
                return True
            continue
        if all(sage._is_clinical_vocabulary(t) for t in tokens):
            continue
        if difflib.SequenceMatcher(None, full_clean_name, window).ratio() >= 0.70:
            return True
    return False


def _old_full_name_match(p_name, query_lower):
    if _old_whole_name_in_query(p_name, query_lower):
        return True
    parts = [x for x in re.split(r'[^a-zA-Z]', p_name)
             if x and x not in ('mr', 'mrs', 'ms', 'dr', 'prof') and len(x) > 2]
    if not parts:
        return False
    return all(re.search(rf'\b{re.escape(x)}\b', query_lower) for x in parts)


# ── the corpus ───────────────────────────────────────────────────────────────
NAMES = [
    "", "s", "x", "s.", "om", "ed", "rao", "das", "rama", "bose", "lata", "chris",
    "raman", "anand", "harish", "kavitha", "insulina", "venkatesh", "meenakshi",
    "o'shea", "d'souza", "s. kumar", "li na wu", "suresh reddy", "priya nair",
    "siddharth nair", "venkatesh iyer", "ramesh reddy", "anand reddy",
    "mr suresh reddy", "dr. anand menon", "meenakshi varadarajan",
    "kanchana balasubramanian", "quinteros-achterberg", "anne-marie o'neil",
    "bartholomew quinteros-achterberg",
]

FIXED_TEXTS = [
    "how is stress ready doing", "how is venkatesk doing", "is cavitha awake",
    "the parish priest visited", "the frames are loose", "and relay the result",
    "any drama on the ward tonight?", "what is the dose of ceftriaxone?",
    "check the ratio first", "lactate is rising", "start insulin now",
    "how is om doing", "check ed's bp again", "is he on oxygen",
    "send him to the red zone", "what is meenakshi's condition",
    "what is meenakshi’s condition", "how is she doing s/p transfusion",
    "fever x 3 days, what should i do", "k 3.2 this morning",
    "give me meenakshi varadarajan's latest vitals", "how is s. kumar doing",
    "how is o'shea doing", "chris' potassium is low", "whats balasubramanian on right now",
    "the woman in bed 3", "raman2 is here", "2raman is here", "raman_x is here",
    "ramané is here", "x's_y and s. kumar's bp", "o'shea’s chart please",
    "li na wu in bed 4", "lina wu in bed 4", "is quinteros-achterberg stable",
    "mr. suresh reddy, bed 12", "anne-marie o'neil's lactate", "anything at all",
]

_FIRST_SWAPS = {"k": "c", "c": "k", "h": "p", "p": "h", "s": "z", "v": "w", "b": "p", "r": "l"}
_TEMPLATES = ["how is {} doing", "{}'s potassium is 5.9", "is {} deteriorating? bed 12"]
_FILLER = ("the and is on of for with please check review overnight vitals bp hr "
           "spo2 lactate potassium dose ward bed doing chart since morning now").split()


def _garbles(rng, name):
    """Plausible ASR-shaped misses of a name: a swapped first consonant, a
    dropped or doubled letter, two letters transposed, the parts run together."""
    parts = [p for p in re.split(r'[^a-z]', name) if p and p not in ('mr', 'dr')]
    if not parts or sum(map(len, parts)) < sage._FUZZY_MIN_LETTERS:
        return []
    out = []
    first = parts[0]
    out.append(" ".join([_FIRST_SWAPS.get(first[0], "b") + first[1:]] + parts[1:]))
    longest = max(range(len(parts)), key=lambda i: len(parts[i]))
    word = parts[longest]
    if len(word) >= 4:
        i = rng.randrange(1, len(word) - 2)
        for variant in (word[:i] + word[i + 1:], word[:i] + word[i] + word[i:],
                        word[:i] + word[i + 1] + word[i] + word[i + 2:]):
            changed = list(parts)
            changed[longest] = variant
            out.append(" ".join(changed))
    if len(parts) > 1:
        out.append("".join(parts))
    return out


def _corpus():
    rng = random.Random(20261004)
    texts = list(FIXED_TEXTS)
    garbles = []
    for name in NAMES:
        for g in _garbles(rng, name):
            garbles.append(g)
            texts.extend(t.format(g) for t in _TEMPLATES[:2])
    vocab = _FILLER + garbles + [n for n in NAMES if n]
    for _ in range(200):
        words = [rng.choice(vocab) for _ in range(rng.randint(1, 20))]
        text = " ".join(words)
        if rng.random() < 0.3:
            text = text.replace(" ", "'s ", 1)
        texts.append(text)
    return texts


def test_the_index_gives_the_old_answers_within_the_cap():
    """THE equivalence pin. Every (name, text) pair, all three predicates,
    against the frozen 3 October matchers. Texts here are within the cap, so
    the only change allowed is speed."""
    mismatches, fuzzy_only = [], 0
    for text in _corpus():
        q = text.lower()
        old_words, new_words = _old_tokens(q), sage._name_tokens(q)
        assert new_words == old_words
        assert len(new_words) <= CAP, "the equivalence corpus must stay within the cap"
        for name in NAMES:
            old = (_old_whole_name_in_query(name, q), _old_name_match(name, q, old_words),
                   _old_full_name_match(name, q))
            new = (sage._whole_name_in_query(name, q), sage._name_match(name, q, new_words),
                   sage._full_name_match(name, q))
            if old != new:
                mismatches.append((name, text, old, new))
            if old[1] and not old[0] and not old[2]:
                fuzzy_only += 1
    assert not mismatches, mismatches[:10]
    # Not vacuous: the corpus must exercise the fuzzy window, not just the
    # exact paths. Measured 4 October 2026 at several hundred pairs.
    assert fuzzy_only >= 50, fuzzy_only


def test_a_plain_token_list_still_works():
    """Callers pass _name_tokens(query), but the signature takes a list, and a
    plain one must give the same answer."""
    q = "how is stress ready doing"
    assert sage._name_match("suresh reddy", q, list(sage._name_tokens(q)))
    assert sage._name_match("suresh reddy", q, sage._name_tokens(q))


# ── the cap ──────────────────────────────────────────────────────────────────
def _with_filler(n, tail):
    return " ".join(["okay"] * n + [tail])


@pytest.mark.parametrize("at,expected", [
    (0, True),
    (CAP - 2, True),       # "stress ready" is tokens 62-63, the last window inside
    (CAP - 1, False),      # tokens 63-64: crosses the cap
    (CAP + 6, False),
])
def test_the_fuzzy_window_stops_at_the_cap(at, expected):
    q = _with_filler(at, "stress ready")
    assert _old_name_match("suresh reddy", q, _old_tokens(q)), (
        "the garble must match under the old matcher, or this pins nothing")
    assert not _old_name_match("suresh reddy", _with_filler(at, "x"), _old_tokens(_with_filler(at, "x")))
    assert sage._name_match("suresh reddy", q, sage._name_tokens(q)) is expected


LONG = " ".join(["okay"] * 20_000)


@pytest.mark.parametrize("name,tail", [
    ("suresh reddy", "suresh"),                  # a name part
    ("suresh reddy", "suresh reddy"),            # the whole name
    ("meenakshi varadarajan", "varadarajan, meenakshi"),
    ("om", "om"),                                # a short name, exact window
    ("li na wu", "li na wu"),                    # six letters, no part of three: the window alone finds it
    ("s. kumar", "s. kumar's"),
    ("o'shea", "o'shea"),
])
def test_exact_matching_reads_the_whole_text(name, tail):
    """The cap is on the NEIGHBOURHOOD. A name typed exactly is found however
    far into the text it sits, by all three predicates, as before."""
    q = f"{LONG} {tail} doing ok?"
    old = (_old_whole_name_in_query(name, q), _old_name_match(name, q, _old_tokens(q)),
           _old_full_name_match(name, q))
    new = (sage._whole_name_in_query(name, q), sage._name_match(name, q, sage._name_tokens(q)),
           sage._full_name_match(name, q))
    assert new == old
    assert new[1], (name, tail)


# ── the cap on every lane, end to end ────────────────────────────────────────
DEPT = "Name Bounds Medicine"
OTHER = "Name Bounds Surgery"


def _user(dept=DEPT):
    uid = f"NBU{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute("INSERT OR IGNORE INTO users (id, name, role, password_hash, department) "
                   "VALUES (?, 'Bounds Doctor', 'doctor', 'x', ?)", (uid, dept))
    return {"id": uid, "role": "doctor", "department": dept, "name": "Bounds Doctor"}


def _patient(name, bed, dept=DEPT):
    pid = f"NBP{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute("INSERT OR IGNORE INTO users (id, name, role, password_hash) "
                   "VALUES ('NBADM', 'Bounds Admitter', 'doctor', 'x')")
        db.execute(
            "INSERT INTO patients (id, name, mrn, bed, ward, age, sex, diagnosis, status, admission_date) "
            "VALUES (?, ?, ?, ?, ?, 61, 'M', 'Pneumonia', 'admitted', ?)",
            (pid, name, f"MRN-{uuid.uuid4().hex[:8]}", bed, dept, datetime.now(timezone.utc).isoformat()))
        db.execute("INSERT INTO admissions (patient_id, department, ward, bed, status, admitted_by) "
                   "VALUES (?, ?, ?, ?, 'active', 'NBADM')", (pid, dept, dept, bed))
    return pid


def _retire(*pids):
    """A name and a bed outlive a discharge, and the matchers read discharged
    patients too, so a seed is renamed and loses its bed rather than being left
    for later tests to trip on. The name has no ordinary word in it: the old
    "Name Bounds Retired Probe" matched "name" in any later question, and the
    kept bare beds "1".."12" matched inside "bed 9917" (an out-of-department
    refusal in test_sage_patient_phrasing when this file ran first)."""
    with db_session() as db:
        for pid in pids:
            db.execute("UPDATE patients SET name = 'Qzxbounds Vvqretired', bed = NULL, "
                       "status = 'discharged' WHERE id = ?", (pid,))


def _oracle_matches(name, text):
    q = text.lower()
    return _old_name_match(name.lower(), q, _old_tokens(q))


def test_in_department_lane_obeys_the_cap():
    user = _user()
    name, garble = "Dharmendra Kulasekaran", "dharmendar kulasekeran"
    pid = _patient(name, "NB-11")
    try:
        inside = "Summarise " + _with_filler(CAP - 10, garble)
        outside = "Summarise " + _with_filler(CAP + 10, garble)
        assert _oracle_matches(name, inside) and _oracle_matches(name, outside)

        *_, pctx, qscope = get_relevant_context(inside, user, history=[])
        assert pctx is not None and pctx["patient_id"] == pid, qscope
        *_, pctx, qscope = get_relevant_context(outside, user, history=[])
        assert pctx is None or pctx["patient_id"] != pid, qscope
    finally:
        _retire(pid)


def test_out_of_department_lane_obeys_the_cap():
    """The deny path reads the same matcher as the show path; if the cap drifted
    between them, a garble could be shown on one lane and unknown on the other."""
    user = _user()
    name, garble = "Gurucharan Thiruvenkadam", "Gurucharen Thiruvenkadem"
    pid = _patient(name, "NS-21", dept=OTHER)
    try:
        inside = "Summarise " + _with_filler(CAP - 10, garble)
        outside = "Summarise " + _with_filler(CAP + 10, garble)
        assert _oracle_matches(name, inside) and _oracle_matches(name, outside)

        *_, pctx, qscope = get_relevant_context(inside, user, history=[])
        assert qscope == "OUT_OF_SCOPE"
        *_, pctx, qscope = get_relevant_context(outside, user, history=[])
        assert qscope != "OUT_OF_SCOPE" and pctx is None
    finally:
        _retire(pid)


def test_history_anchor_obeys_the_cap():
    user = _user()
    name, garble = "Thirunavukkarasu Venugopal", "thirunavukarasu venugopaal"
    pid = _patient(name, "NB-12")
    try:
        inside = _with_filler(CAP - 10, garble)
        outside = _with_filler(CAP + 10, garble)
        assert _oracle_matches(name, inside) and _oracle_matches(name, outside)

        *_, pctx, _q = get_relevant_context(
            "what are his vitals?", user, history=[{"role": "user", "content": inside}])
        assert pctx is not None and pctx["patient_id"] == pid, _q
        *_, pctx, _q = get_relevant_context(
            "what are his vitals?", user, history=[{"role": "user", "content": outside}])
        assert pctx is None or pctx["patient_id"] != pid, _q
    finally:
        _retire(pid)


# ── timing: a 512 KB question ────────────────────────────────────────────────
def _anagram_ward(rng, n=30):
    """Thirty names built from one pool of letters. Any shuffle of that pool is
    as long as each name and holds the same letters, so it survives BOTH cheap
    bounds against every patient and forces SequenceMatcher: the text that
    only the cap can bound (measured 23 s on the matcher alone without it)."""
    pool = list("aaeeiihhkmnrrsstv")
    names = []
    for _ in range(n):
        rng.shuffle(pool)
        word = "".join(pool)
        names.append(f"{word[:8].capitalize()} {word[8:].capitalize()}")
    return names


def _anagram_text(rng, size):
    pool = list("aaeeiihhkmnrrsstv")
    words, length = [], 0
    while length < size:
        rng.shuffle(pool)
        word = "".join(pool)
        cut = rng.randint(3, len(word) - 3)
        words += [word[:cut], word[cut:]]
        length += len(word) + 2
    return " ".join(words)


def _timed(query, user, history=None):
    start = time.perf_counter()
    result = get_relevant_context(query, user, history=history or [])
    return time.perf_counter() - start, result


def test_a_512kb_bed_question_is_answered_in_seconds():
    """The measured case, ten times longer: 7.4 s at 50,000 characters before."""
    user = _user(dept="Name Bounds Bed Ward")
    pids = [_patient(f"Qzxbed{chr(97 + i)} Kumaraswamy", str(i + 1), dept="Name Bounds Bed Ward")
            for i in range(12)]
    try:
        query = ("bed 7 " * (BIG // 6)).strip()
        elapsed, _result = _timed(query, user)
        assert elapsed < BUDGET_S, f"{elapsed:.2f} s for a {len(query)}-character question"
    finally:
        _retire(*pids)


def test_a_512kb_question_built_to_defeat_the_bounds_is_answered_in_seconds():
    rng = random.Random(7)
    dept = "Name Bounds Anagram Ward"
    user = _user(dept=dept)
    pids = [_patient(n, f"AN-{i}", dept=dept) for i, n in enumerate(_anagram_ward(rng))]
    try:
        # "Summarise <Capitalised>" makes the question name-shaped, so the
        # out-of-department lane scans the hospital roster as well.
        query = "Summarise Qorvath " + _anagram_text(rng, BIG)
        elapsed, _result = _timed(query, user)
        assert elapsed < BUDGET_S, f"{elapsed:.2f} s for a {len(query)}-character question"
    finally:
        _retire(*pids)


def test_three_512kb_history_turns_are_read_in_seconds():
    """The anchor re-reads up to three earlier user turns on every follow-up;
    a pasted note used to cost its full scan again on each of them."""
    rng = random.Random(8)
    dept = "Name Bounds History Ward"
    user = _user(dept=dept)
    pids = [_patient(n, f"HI-{i}", dept=dept) for i, n in enumerate(_anagram_ward(rng))]
    try:
        history = []
        for _ in range(3):
            history += [{"role": "user", "content": _anagram_text(rng, BIG)},
                        {"role": "assistant", "content": "Noted."}]
        elapsed, _result = _timed("what are his vitals?", user, history=history)
        assert elapsed < BUDGET_S, f"{elapsed:.2f} s with three {BIG}-character turns"
    finally:
        _retire(*pids)
