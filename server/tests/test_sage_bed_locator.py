"""Sage bed and ward lookup (ruling R026, 3 October 2026; attempt 5).

THE DEFECT. Sage found a bed by squashing the question and every bed label to
letters and digits and testing for a SUBSTRING. Ward "GM-1" sits inside bed
"GM-103", so "Summarise bed GM-103" asked the doctor to pick among all of GM-1;
bed "A1" sits inside "HbA1c", bed "12" inside "after 12 hours". When exactly
one patient matched that way, Sage attached a chart the question was not about.

FOUR ATTEMPTS WERE BACKED OUT (the fourth: "give lasix to 12" read as a
dose, and the which-patient turn was never saved, so "yes" after it went to the
open chart). THREE ATTEMPTS BEFORE THAT: The first matched less, and a turn that
matched nobody fell back to the chart open on screen. The second described
every way a clinician refers to a bed; each gap in that description ("how is
12?", "room 12", "MW/12", a bed typed with an en dash) also fell to the open
chart. The third found beds from the labels on the ward, but dropped "bed 12
HR 140" to the open chart, and its "ask" was a bare list the model ignored.
docs/SAGE_BED_LOCATOR_FINDINGS_2026-10-03.md has the post-mortems.

THE DESIGN THESE TESTS PIN. Beds are found from the labels ON THE DOCTOR'S
WARD, in whatever shape the hospital stores them, as whole labels. Each hit is
certain (answer from that chart), plainly not a bed (a dose, a time, a score),
or unsure (Sage asks). A bed written with a bed word, or as a code this
department uses, that matches nobody is NO MATCHING PATIENT. Never the open
chart.

THE RULINGS. R026 (a); D2 "bed 9" finds "09"; D3 one missing bed in a list is
no match for the whole turn; D4 a bed label that is also a clinical code asks;
Q1 a bare number that is a bed on the ward asks; Q2 a bare bed number only
another department has is no match (bed numbers repeat across wards), while a
lettered code another department has is "outside your department"; Q3 "bay" is
a room of beds, not a bed. A (a): the SERVER writes every which-patient
question; the model is never asked to. E: an answer from a chart the
clinician did not name starts "About <name>, bed <bed>:", and an order on such
a turn asks which patient first. F: another department's code in a transfer
phrase ("transferred from ICU-03") keeps the open chart.

Every behaviour test goes through get_relevant_context WITH AN OPEN CHART
(patient_id set): the open-chart fallback is the path both failed attempts
broke, and a matcher-only test cannot see it. The other direction is pinned
just as hard: ordinary ward phrasing must keep the open chart, because a
chartless turn is the hallucination condition.
"""
import re
import time
import uuid
from datetime import datetime, timezone

import pytest

import services.clinical_knowledge as ck
import services.sage_service as sage
from database import db_session
from services.sage_service import get_relevant_context

NUM = "Locator Numeric Medicine"
NUMW = "Locator Spoken Medicine"
MISS = "Locator Missing Medicine"
GM = "Locator General Medicine"
NINES = "Locator Nines Medicine"
ZERO = "Locator Zero Medicine"
A1 = "Locator A1 Medicine"
CODES = "Locator Codes Medicine"
STOP = "Locator Recovery Medicine"
WARDS = "Locator Ward Medicine"
MINE = "Locator Home Medicine"
OTHER = "Locator Other Surgery"
HIST = "Locator History Medicine"
LABEL = "Locator Label Medicine"
SHAPES = "Locator Shapes Medicine"
HDU = "Locator Hdu Medicine"
HDU2 = "Locator Hdu Two Surgery"
GONE = "Locator Emptied Medicine"
PREFIX = "Locator Prefix Surgery"
WARDS2 = "Locator Ward Pair Medicine"
HOB = "Locator Position Medicine"
GM60 = "Locator Big Ward Medicine"
NAMES = "Locator Namesake Medicine"
SPACED = "Locator Spacing Medicine"
KWARD = "Locator Kay Medicine"
ICUBEDS = "Locator Unit Beds Medicine"
WORDNAMES = "Locator Wordname Medicine"
RELNAMES = "Locator Relative Medicine"


@pytest.fixture(autouse=True)
def _no_live_retrieval(monkeypatch):
    monkeypatch.setattr(ck, "_cross_encoder", None)
    monkeypatch.setattr(ck, "_cross_encoder_checked", True)
    monkeypatch.setattr(ck, "search_guidelines", lambda *a, **k: [])


def _user(dept):
    uid = f"BLU{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute("INSERT OR IGNORE INTO users (id, name, role, password_hash, department) "
                   "VALUES (?, 'Locator Doctor', 'doctor', 'x', ?)", (uid, dept))
    return {"id": uid, "role": "doctor", "department": dept, "name": "Locator Doctor"}


_FIRST = ["Zorawar", "Yashodhara", "Xavierine", "Wenceslas", "Vishwanathan", "Ulupi",
          "Trilochana", "Sudhamshu", "Rukmangada", "Pushpalatha", "Omkarnath", "Nirupama",
          "Mahalingam", "Lalithambika", "Kumudavalli", "Jagadambika", "Indumathi",
          "Hiranmayee", "Gangadharan", "Falguni"]
_LAST = ["Bhattacharjee", "Venkataraman", "Dsouzamani", "Chakravarty", "Iyengarappa",
         "Sengupthana", "Mukhopadhya", "Rajagopalan", "Thirunavukk", "Kandaswamy"]
_NAMES = iter(f"{f} {l}" for l in _LAST for f in _FIRST)


#: Every patient this module seeds. Torn down to neutral labels at the end:
#: the PHI classifier (ai_governance) substring-matches every patient's ward
#: and bed, discharged ones included, so a leftover ward "OR" would mark any
#: later file's question containing "for" or "doctor" as ward-scoped.
_SEEDED: list = []


def _patient(dept, bed, ward=None, status="admitted", name=None):
    """Distinctive names nobody's question contains, so the name matcher stays
    out of every assertion below."""
    pid = f"BLP{uuid.uuid4().hex[:8].upper()}"
    _SEEDED.append(pid)
    name = name or next(_NAMES)
    ward = ward or dept
    with db_session() as db:
        db.execute("INSERT OR IGNORE INTO users (id, name, role, password_hash) "
                   "VALUES ('BLADM', 'Locator Admitter', 'doctor', 'x')")
        db.execute(
            "INSERT INTO patients (id, name, mrn, bed, ward, age, sex, diagnosis, status, admission_date) "
            "VALUES (?, ?, ?, ?, ?, 58, 'F', 'Community-acquired pneumonia', ?, ?)",
            (pid, name, f"MRN-{uuid.uuid4().hex[:8]}", bed, ward, status,
             datetime.now(timezone.utc).isoformat()))
        db.execute("INSERT INTO admissions (patient_id, department, ward, bed, status, admitted_by) "
                   "VALUES (?, ?, ?, ?, ?, 'BLADM')",
                   (pid, dept, ward, bed, "active" if status == "admitted" else "discharged"))
    return {"id": pid, "name": name, "bed": bed, "ward": ward}


@pytest.fixture(scope="module")
def h():
    """The whole synthetic hospital, seeded once, so every bed and ward prefix
    below is on record for every test, exactly as on a real site."""
    s = {}
    s["num"] = {b: _patient(NUM, b) for b in ("7", "12", "14")}
    s["numw"] = {b: _patient(NUMW, b) for b in
                 ("1", "2", "3", "7", "12", "14", "20", "21", "30")}
    s["miss"] = {b: _patient(MISS, b) for b in ("7", "12")}
    s["gm"] = {b: _patient(GM, b, "GM-1") for b in
               ("GM-101", "GM-102", "GM-103", "GM-104", "GM-105")}
    s["gm"].update({b: _patient(GM, b, "CCU") for b in ("CCU-01", "CCU-02")})
    s["gm"].update({b: _patient(GM, b, "RB-1") for b in ("RB-101", "RB-102")})
    s["nines"] = {b: _patient(NINES, b) for b in ("9", "17", "99")}
    s["zero"] = {b: _patient(ZERO, b) for b in ("09", "ICU-03", "ICU-04")}
    s["a1"] = {b: _patient(A1, b) for b in ("A1", "A5")}
    s["codes"] = {b: _patient(CODES, b) for b in ("B12", "B40")}
    s["stop"] = {b: _patient(STOP, b, "RR-1") for b in ("RR-24", "RR-25")}
    s["stop"]["OR-5"] = _patient(STOP, "OR-5", "OR")
    s["wards"] = {
        "5/12": _patient(WARDS, "12", "5"),
        "6/12": _patient(WARDS, "12", "6"),
        "6/3": _patient(WARDS, "3", "6"),
    }
    s["mine"] = {b: _patient(MINE, b) for b in ("41", "42")}
    s["other"] = {b: _patient(OTHER, b) for b in ("9", "17", "3", "SX-17")}
    s["other"]["88"] = _patient(OTHER, "88", status="discharged")
    s["hist"] = {b: _patient(HIST, b) for b in ("31", "32")}
    s["label"] = {b: _patient(LABEL, b) for b in ("Bed 21", "Bed 22")}
    s["shapes"] = {b: _patient(SHAPES, b) for b in
                   ("Room 204", "Room 205", "MW/12", "GW.12", "CARDIO-12", "12A",
                    "Cubicle 3", "ICU Bed 3")}
    s["hdu"] = {b: _patient(HDU, b, "HDU") for b in ("HDU-1", "HDU-2")}
    s["hdu2"] = {"HDU-2-01": _patient(HDU2, "HDU-2-01", "HDU-2")}
    s["gone"] = {b: _patient(GONE, b, "YQ-1", status="discharged") for b in ("YQ-03", "YQ-04")}
    s["gone"]["51"] = _patient(GONE, "51", "YQ-9")
    s["prefix"] = {b: _patient(PREFIX, b) for b in ("D-7", "H1", "ANC-01", "NS-01", "COVID-01",
                                                     "COVID-19")}
    s["hob"] = {b: _patient(HOB, b) for b in ("7", "15", "30", "45")}
    s["gm60"] = {f"GM-{n}": _patient(GM60, f"GM-{n}", "GM-5", name=f"Gmsixty Qqzx{n}")
                 for n in range(501, 561)}
    s["wards2"] = {f"{w}/{b}": _patient(WARDS2, b, w) for w, b in
                   (("5", "12"), ("5", "14"), ("6", "3"), ("6", "20"))}
    s["spaced"] = {b: _patient(SPACED, b) for b in ("C-4", "N-1", "Cabin 5", "Cabin 6")}
    s["kward"] = {f"K-{b}": _patient(KWARD, f"K-{b}") for b in range(1, 13)}
    s["wordnames"] = {
        "7": _patient(WORDNAMES, "7", name="Qorvinda Pellastrix"),
        "12": _patient(WORDNAMES, "12", name="Elvira Short"),
        "14": _patient(WORDNAMES, "14", name="Happy Grewalqu"),
    }
    s["relnames"] = {
        "7": _patient(RELNAMES, "7", name="Qorvathi Pennamuz"),
        "18": _patient(RELNAMES, "18", name="Ravindran Kuppusamy"),
        "19": _patient(RELNAMES, "19", name="Selvaraj K"),
        "20": _patient(RELNAMES, "20", name="Murugesan"),
        "21": _patient(RELNAMES, "21", name="P. Thangavel"),
    }
    s["icubeds"] = {f"ICU Bed {n}": _patient(ICUBEDS, f"ICU Bed {n}", "Unit Beds Ward",
                                             name=f"Icubedsix Qqzx{n}") for n in range(1, 61)}
    s["names"] = {
        "a": _patient(NAMES, "N-1", name="Priyamvada Thekkekara"),
        "b": _patient(NAMES, "N-2", name="Siddharthan Thekkekara"),
        "c": _patient(NAMES, "N-3"),
    }
    s["users"] = {k: _user(d) for k, d in (
        ("num", NUM), ("numw", NUMW), ("miss", MISS), ("gm", GM), ("nines", NINES),
        ("zero", ZERO), ("a1", A1), ("codes", CODES), ("stop", STOP), ("wards", WARDS),
        ("mine", MINE), ("hist", HIST), ("label", LABEL), ("shapes", SHAPES), ("hdu", HDU),
        ("gone", GONE), ("wards2", WARDS2), ("names", NAMES), ("hob", HOB), ("gm60", GM60),
        ("spaced", SPACED), ("kward", KWARD), ("icubeds", ICUBEDS),
        ("wordnames", WORDNAMES), ("relnames", RELNAMES))}
    yield s
    with db_session() as db:
        db.executemany(
            "UPDATE patients SET status = 'discharged', bed = NULL, ward = 'Locator Retired Ward' "
            "WHERE id = ?", [(p,) for p in _SEEDED])
        db.executemany("UPDATE admissions SET status = 'discharged' WHERE patient_id = ?",
                       [(p,) for p in _SEEDED])
    _SEEDED.clear()


def _ask(h, scen, query, open_bed=None, history=None):
    pid = h[scen][open_bed]["id"] if open_bed else None
    context, scope, _src, _conf, _esc, pctx, qscope = get_relevant_context(
        query, h["users"][scen], pid, history)
    return {"context": context, "scope": scope, "pctx": pctx, "qscope": qscope,
            "_h": h, "_scen": scen, "_query": query, "_open_id": pid, "_history": history}


def _chunks(label):
    """The test's OWN reading of "same bed": a leading bed word, case and
    separators ignored, leading zeros ignored. Not imported from the
    implementation."""
    s = re.sub(r"^\s*(?:bed|cot)\s*", "", label.lower())
    return tuple(int(t) if t.isdigit() else t for t in re.findall(r"[a-z]+|\d+", s))


def _lettered_admitted_elsewhere(dept, label):
    """Another department holds this LETTERED label (ruling Q2: only lettered
    codes get the out-of-department answer). Another test file in the same
    session could have left one, so the expectation is read, not assumed."""
    if not any(isinstance(c, str) for c in _chunks(label)):
        return False
    with db_session() as db:
        rows = db.execute(
            "SELECT p.bed, a.department FROM patients p "
            "LEFT JOIN admissions a ON a.patient_id = p.id AND a.status = 'active' "
            "WHERE p.status = 'admitted' AND p.bed IS NOT NULL").fetchall()
    return any(_chunks(r["bed"]) == _chunks(label) and r["department"] != dept for r in rows)


def _assert_chart(r, patient, scope="PATIENT_SPECIFIC"):
    """The bed was read as THIS patient. Since attempt 10 (Samuel, 6 October)
    Sage never opens a bed's chart on its own reading: it asks about exactly
    this patient, with a chart open or with nothing open. Only the open
    patient's own bed, and a reply to the which-patient question, open it."""
    hist = r.get("_history")
    is_reply = bool(hist) and _is_question_turn(hist[-1])
    if is_reply:
        assert r["qscope"] == scope, (r["qscope"], r["context"][:300])
        assert r["pctx"] is not None and r["pctx"]["patient_id"] == patient["id"], (
            "the chart attached is not the patient asked about")
        return
    _assert_confirms(r, patient)
    if r.get("_open_id") and hist is None and r["_open_id"] != patient["id"]:
        _assert_confirms(_ask(r["_h"], r["_scen"], r["_query"]), patient)


def _is_question_turn(msg):
    return (msg.get("role") == "assistant"
            and (msg.get("content") or "").startswith("Which patient do you mean?"))


def _assert_confirms(r, patient):
    """The which-patient question with this patient as its ONLY candidate."""
    assert r["qscope"] == "DISAMBIGUATE", (r.get("_query"), r["qscope"], r["context"][:300])
    assert r["pctx"] is None
    assert r["context"].startswith("Which patient do you mean?"), r["context"][:200]
    assert patient["name"] in r["context"], (r.get("_query"), r["context"][:300])
    assert r["context"].count("\n- Bed ") == 1, (r.get("_query"), r["context"][:300])
    assert 'Reply "yes"' in r["context"], r["context"][:300]


def _assert_anchored(r, patient):
    """The open chart answers: through the sticky anchor, or through the
    pronoun branch when the question says "her" or "the patient"."""
    assert r["qscope"] in ("PATIENT_ANCHORED", "CONTEXTUAL_FOLLOWUP"), (
        r["qscope"], r["context"][:300])
    assert r["pctx"] is not None and r["pctx"]["patient_id"] == patient["id"], (
        "the open chart was dropped or replaced")


def _assert_asked(r, *patients, absent=()):
    assert r["qscope"] == "DISAMBIGUATE", (r["qscope"], r["context"][:300])
    assert r["pctx"] is None
    assert r["context"].startswith("Which patient do you mean?"), r["context"][:200]
    for p in patients:
        assert p["name"] in r["context"], p
    for p in absent:
        assert p["name"] not in r["context"], p


def _assert_missing(r, dept, open_patient, label):
    """The ruled answer to a bed nobody here is in: no chart at all, never the
    open one. "Outside your department" only for a lettered code another
    department holds (Q2)."""
    assert r["pctx"] is None, "a chart was attached for a bed nobody here is in"
    assert open_patient["name"] not in r["context"], (
        "the open chart answered for a bed that does not exist")
    if _lettered_admitted_elsewhere(dept, label):
        assert r["qscope"] == "OUT_OF_SCOPE", r["qscope"]
    else:
        assert r["qscope"] != "OUT_OF_SCOPE", "a bare bed number is not a reach into another department"
        assert "NO MATCHING PATIENT" in r["context"], r["context"][:300]


# ══════════════════════════════════════════════════════════════════════════════
#  1. THE FIRST REVIEWER'S TABLE: numeric beds 7, 12, 14, bed 7's chart open
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("query", [
    "compare beds 12 and 14",
    "bed 12 and 14 potassium?",
    "beds 12, 14 lactate",
    "beds 12 & 14 lactate",
    "beds 12/14 lactate?",
    "bed 12 or 14, whose potassium is lower?",
    "compare 12 and 14",
    "12 and 14 potassium?",
])
def test_two_beds_named_ask_between_those_two_never_the_open_chart(h, query):
    n = h["num"]
    _assert_asked(_ask(h, "num", query, open_bed="7"), n["12"], n["14"], absent=[n["7"]])


@pytest.mark.parametrize("query", [
    "cot 12 lactate?",
    "bed num 12 lactate?",
    "bed no. 12 lactate?",
    "bed number 12 lactate?",
    "bed #12 lactate?",
    "bed:12 lactate?",
    "bed12 lactate?",
    "bed twelve, lactate?",
    "12 number bed lactate?",
    "12th bed lactate?",
    "what is the potassium of the patient in 12?",
    "how is pt in 12 overnight?",
    "bed‑12 lactate?",
    "bed–12 lactate?",
    "bed—12 lactate?",
])
def test_every_bed_phrasing_finds_that_bed_not_the_open_chart(h, query):
    _assert_chart(_ask(h, "num", query, open_bed="7"), h["num"]["12"])


@pytest.mark.parametrize("query", [
    "how is 12?",
    "how's 12 doing?",
    "what about 12?",
    "12's potassium?",
    "who is in 12?",
    "and 12?",
    "12 wala patient potassium?",
    "patient no 12 potassium?",
    "pt 12 potassium?",
    "room 12 potassium?",
    "cubicle 12 potassium?",
    "b/12 potassium?",
    "bed space 12",
    "bedspace 12",
    "bad 12 potassium?",
    "her in 12, potassium?",
    "how is bay 12?",
    "the patient at 12, potassium?",
    "review the pt at 12 tonight?",
])
def test_a_bare_number_that_is_a_bed_here_makes_sage_ask(h, query):
    """Q1. The old code answered these from bed 12 and attempt 2 from the
    OPEN chart. Sage cannot be sure the number is a bed, so it asks."""
    n = h["num"]
    _assert_asked(_ask(h, "num", query, open_bed="7"), n["12"], absent=[n["7"], n["14"]])


@pytest.mark.parametrize("query,label", [
    # "her", not "the patient": another file leaves a discharged test patient
    # NAMED "Patient RBAC Test", whom the older name matcher finds in the word
    # "patient" (a test-data clash outside bed lookup).
    ("what is her potassium in bed 15", "15"),
    ("cot 15 lactate?", "15"),
    ("compare beds 12 and 63", "63"),
])
def test_a_missing_bed_never_reads_the_open_chart(h, query, label):
    """The pronoun branch used to take the open chart without asking whether a
    bed was named. A list with one missing bed is no match for the whole turn
    (D3), so bed 12 is not answered on its own either."""
    r = _ask(h, "num", query, open_bed="7")
    _assert_missing(r, NUM, h["num"]["7"], label)
    assert h["num"]["12"]["name"] not in r["context"]


@pytest.mark.parametrize("query", ["can we shift her to bed 12?", "transfer to bed 12"])
def test_an_occupied_destination_bed_makes_sage_ask(h, query):
    """The question is about the open patient moving, but bed 12's patient is
    in it: Sage must not silently read either chart."""
    n = h["num"]
    _assert_asked(_ask(h, "num", query, open_bed="7"), n["12"], absent=[n["7"]])


def test_an_empty_destination_bed_keeps_the_open_chart(h):
    """An empty bed is the normal place to move someone. That is not "no such
    patient"."""
    _assert_anchored(_ask(h, "num", "can we shift her to bed 40?", open_bed="7"), h["num"]["7"])


# ══════════════════════════════════════════════════════════════════════════════
#  2. WHOLE LABELS: a ward is not a substring of its beds
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("query", [
    "Summarise bed GM-103",
    "Summarise GM-103",
    "gm 103 lactate?",
    "GM103 potassium?",
    "how is the patient in GM-103?",
    "GM–103 lactate?",
])
def test_a_gm_bed_finds_only_that_bed(h, query):
    _assert_chart(_ask(h, "gm", query, open_bed="RB-102"), h["gm"]["GM-103"])


@pytest.mark.parametrize("query,label", [
    ("how is GM-1011", "GM-1011"),
    ("Summarise RB-106", "RB-106"),
    ("GM-1 bed 2 potassium?", "2"),
])
def test_a_code_this_department_uses_that_matches_nobody_is_no_match(h, query, label):
    """RB-1 exists and RB-102 is open; RB-106 does not exist. The old code asked
    between RB-101 and RB-102 (ward substring); attempt 1 answered from
    RB-102. "GM-1 bed 2": ward GM-1 has no bed 2, and another department's
    bed 2 is not this one."""
    r = _ask(h, "gm", query, open_bed="RB-102")
    _assert_missing(r, GM, h["gm"]["RB-102"], label)
    assert h["gm"]["RB-101"]["name"] not in r["context"]


def test_a_ccu_bed_is_one_patient_not_the_whole_unit(h):
    _assert_chart(_ask(h, "gm", "Summarise CCU-01", open_bed="RB-102"), h["gm"]["CCU-01"])


def test_a_ward_question_gets_the_ward_census_not_a_which_patient_question(h):
    """Attempt 4 answered "how is ward GM-1 doing?" with the server's which-
    patient list. A ward on its own is a question about the ward: the census."""
    g = h["gm"]
    r = _ask(h, "gm", "How is ward GM-1 doing?", open_bed="RB-102")
    assert r["qscope"] == "BROAD", r["qscope"]
    for b in ("GM-101", "GM-102", "GM-103", "GM-104", "GM-105"):
        assert g[b]["name"] in r["context"]


# ══════════════════════════════════════════════════════════════════════════════
#  3. SHORT NUMERIC BEDS 9, 17, 99
# ══════════════════════════════════════════════════════════════════════════════

def test_a_number_that_contains_real_beds_finds_nobody(h):
    r = _ask(h, "nines", "Who is in bed 9917", open_bed="99")
    _assert_missing(r, NINES, h["nines"]["99"], "9917")


def test_a_score_of_9_is_not_bed_9(h):
    _assert_anchored(_ask(h, "nines", "Is a NEWS2 of 9 reassuring?", open_bed="99"),
                     h["nines"]["99"])


@pytest.mark.parametrize("query,bed", [("how is bed 9", "9"), ("bed no. 17 please", "17")])
def test_short_beds_resolve_with_the_bed_word(h, query, bed):
    _assert_chart(_ask(h, "nines", query, open_bed="99"), h["nines"][bed])


# ══════════════════════════════════════════════════════════════════════════════
#  4. LEADING ZEROS (D2)
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("query,bed", [
    ("how is bed 9?", "09"),
    ("bed 09 potassium?", "09"),
    ("ICU 3 lactate?", "ICU-03"),
    ("icu-3 lactate?", "ICU-03"),
])
def test_leading_zeros_do_not_matter(h, query, bed):
    _assert_chart(_ask(h, "zero", query, open_bed="ICU-04"), h["zero"][bed])


def test_icu_followed_by_a_duration_is_not_a_bed(h):
    _assert_anchored(_ask(h, "zero", "transferred from ICU 3 days ago, still on oxygen?",
                          open_bed="ICU-04"), h["zero"]["ICU-04"])


# ══════════════════════════════════════════════════════════════════════════════
#  5. A ONE-LETTER BED: "A1"
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("query", [
    "what is a normal HbA1c?",
    "is a 1 point rise significant",
    "what is her A1c?",
    "A1c 9.2, is that high?",
])
def test_a1_inside_ordinary_words_keeps_the_open_chart(h, query):
    _assert_anchored(_ask(h, "a1", query, open_bed="A5"), h["a1"]["A5"])


@pytest.mark.parametrize("query", ["summarise A1", "summarise bed A-1", "how is a-1?"])
def test_a1_written_as_a_bed_finds_it(h, query):
    _assert_chart(_ask(h, "a1", query, open_bed="A5"), h["a1"]["A1"])


# ══════════════════════════════════════════════════════════════════════════════
#  6. A BED LABEL THAT IS ALSO A CLINICAL CODE (D4)
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("query", ["is vitamin b12 low?", "summarise B12"])
def test_a_code_that_is_a_bed_on_this_ward_makes_sage_ask(h, query):
    c = h["codes"]
    _assert_asked(_ask(h, "codes", query, open_bed="B40"), c["B12"], absent=[c["B40"]])


def test_a_code_with_the_bed_word_is_simply_that_bed(h):
    _assert_chart(_ask(h, "codes", "bed B12 lactate?", open_bed="B40"), h["codes"]["B12"])


@pytest.mark.parametrize("query", ["is the T4 low?", "is vitamin d3 needed?"])
def test_a_code_that_is_not_a_bed_here_is_clinical_text(h, query):
    _assert_anchored(_ask(h, "codes", query, open_bed="B40"), h["codes"]["B40"])


# ══════════════════════════════════════════════════════════════════════════════
#  7. THE OTHER DIRECTION: ordinary ward phrasing keeps the open chart
#
#  Each of these would drop the chart (the hallucination condition) if a
#  number in it were read as a bed. All the bed prefixes and numbers in them
#  are on record somewhere in the hospital.
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("scen,open_bed,query", [
    ("num", "7", "potassium after 12 hours?"),
    ("num", "7", "repeat the lactate in 12 hours"),
    ("num", "7", "500 mg 12 hourly, is that right?"),
    ("num", "7", "is 1 gm 8 hourly enough?"),
    ("num", "7", "keep on bed rest?"),
    ("num", "7", "moved to this bed 2 days ago, any change?"),
    ("num", "7", "compare ceftriaxone and cefepime"),
    ("num", "7", "is 14 breaths a minute normal?"),
    ("num", "7", "potassium 12, is that dangerous?"),
    ("num", "7", "is a GCS of 14 acceptable?"),
    ("num", "7", "seen on 12/10, any change?"),
    ("num", "7", "review at 12:30?"),
    ("numw", "7", "keep head of bed 30 degrees?"),
    ("numw", "7", "head of bed 30° ok?"),
    ("numw", "7", "is the patient in 2:1 block?"),
    ("numw", "7", "is this patient in 1:1 nursing?"),
    ("numw", "7", "the patient in 2 litres O2, wean?"),
    ("numw", "7", "12 number beds free in ICU?"),
    ("numw", "7", "2 no. beds free?"),
    ("numw", "7", "she fell out of bed. 2 nurses lifted her, any injury?"),
    ("numw", "7", "no ICU bed. 3 patients waiting, who goes first?"),
    ("miss", "7", "is the patient in 2nd degree heart block?"),
    ("miss", "7", "is the patient in 1st degree block?"),
    ("miss", "7", "is the patient in 3rd trimester?"),
    ("miss", "7", "was the patient in 2019 on warfarin?"),
    ("miss", "7", "is the patient in 4+ oedema?"),
    ("miss", "7", "what is her A1c?"),
    ("miss", "7", "is D5W the right fluid?"),
    ("miss", "7", "PEEP 8 cm H2O ok?"),
    ("miss", "7", "is an H3 receptor blocker useful?"),
    ("miss", "7", "ANC 500, is she neutropenic?"),
    ("miss", "7", "ANC 1200?"),
    ("miss", "7", "NS 100 cc/hr ok?"),
    ("miss", "7", "COVID 19 swab sent?"),
    ("miss", "7", "covid-19 swab sent?"),
    ("codes", "B40", "is vitamin d3 needed?"),
    ("stop", "RR-25", "RR 24, is that high?"),
    ("stop", "RR-25", "is the potassium 5 or 6?"),
    ("stop", "RR-25", "HR 120 BP 90/60, what next?"),
    ("gm", "RB-102", "2 gm 12 hourly ceftriaxone?"),
    ("gm", "RB-102", "ceftriaxone gm 12 hourly?"),
])
def test_ordinary_ward_phrasing_keeps_the_open_chart(h, scen, open_bed, query):
    _assert_anchored(_ask(h, scen, query, open_bed=open_bed), h[scen][open_bed])


def test_a_recovery_ward_bed_written_as_a_bed_still_resolves(h):
    _assert_chart(_ask(h, "stop", "summarise RR-24", open_bed="RR-25"), h["stop"]["RR-24"])


def test_a_recovery_ward_code_written_joined_makes_sage_ask(h):
    """'rr24' is a respiratory rate or bed RR-24. Both are plausible."""
    s = h["stop"]
    _assert_asked(_ask(h, "stop", "rr24 potassium?", open_bed="RR-25"), s["RR-24"], absent=[s["RR-25"]])


def test_a_missing_recovery_ward_bed_is_no_match(h):
    r = _ask(h, "stop", "summarise RR-26", open_bed="RR-25")
    _assert_missing(r, STOP, h["stop"]["RR-25"], "RR-26")


# ══════════════════════════════════════════════════════════════════════════════
#  8. SPOKEN NUMBERS, RANGES AND LISTS
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("query,bed", [
    ("bed twenty one, lactate?", "21"),
    ("bed twenty-one?", "21"),
    ("bed thirty?", "30"),
    ("how is bed twelve now?", "12"),
])
def test_spoken_numbers_read_whole(h, query, bed):
    """A number word is a bed when it is the whole reference (attempt 7)."""
    _assert_chart(_ask(h, "numw", query, open_bed="7"), h["numw"][bed])


@pytest.mark.parametrize("query,label", [
    ("bed twenty-two, lactate?", "22"),
    ("bed fifty?", "50"),
    ("bed one two?", "one two"),
    ("beds 1-2 lactate?", "1-2"),
    ("beds 12–14 lactate?", "12-14"),
    ("beds 12 to 14 lactate?", "12 to 14"),
    ("bed hundred and two?", "hundred and two"),
])
def test_numbers_that_cannot_be_read_as_one_bed_are_no_match(h, query, label):
    """Never a guess: "bed twenty one" used to read as bed 20, "beds 1-2" as
    bed 12, and "beds 12 to 14" dropped bed 14 silently."""
    r = _ask(h, "numw", query, open_bed="7")
    assert r["pctx"] is None and h["numw"]["7"]["name"] not in r["context"]
    assert "NO MATCHING PATIENT" in r["context"], (query, r["qscope"])
    for b in ("1", "2", "12", "14", "20"):
        assert h["numw"][b]["name"] not in r["context"], (query, b)


# ══════════════════════════════════════════════════════════════════════════════
#  9. WARD AND BED TOGETHER
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("query,key", [
    ("ward 5 bed 12 potassium?", "5/12"),
    ("bed 12 on ward 6 potassium?", "6/12"),
    ("ward 6 - bed 12 potassium?", "6/12"),
    ("bed 12 (ward 6) potassium?", "6/12"),
    ("bed 12 from ward 6 potassium?", "6/12"),
    ("ward-6/bed-12 potassium?", "6/12"),
    ("6th ward bed 12 potassium?", "6/12"),
    ("bed 12, ward six, potassium?", "6/12"),
    ("w6 b12 potassium?", "6/12"),
])
def test_ward_and_bed_written_together_pick_the_intersection(h, query, key):
    _assert_chart(_ask(h, "wards", query, open_bed="6/3"), h["wards"][key])


@pytest.mark.parametrize("query", [
    "ward 9 bed 12 potassium?",
    "bed 12 on ward 9 potassium?",
    "ward 5 bed 3 potassium?",
])
def test_a_ward_and_bed_that_do_not_meet_are_no_match_never_out_of_department(h, query):
    r = _ask(h, "wards", query, open_bed="6/3")
    w = h["wards"]
    assert r["qscope"] != "OUT_OF_SCOPE" and r["pctx"] is None
    assert "NO MATCHING PATIENT" in r["context"]
    for p in w.values():
        assert p["name"] not in r["context"]


def test_a_bed_number_on_two_wards_asks(h):
    w = h["wards"]
    _assert_asked(_ask(h, "wards", "bed 12 potassium?", open_bed="6/3"),
                  w["5/12"], w["6/12"], absent=[w["6/3"]])


def test_a_ward_elsewhere_in_the_sentence_does_not_filter_the_bed(h):
    w = h["wards"]
    _assert_asked(_ask(h, "wards", "transfer bed 12 to ward 6?", open_bed="6/3"),
                  w["5/12"], w["6/12"], absent=[w["6/3"]])


# ══════════════════════════════════════════════════════════════════════════════
#  10. ANOTHER DEPARTMENT'S BEDS (Q2)
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("query,label", [
    ("who is in bed 9917", "9917"),
    ("who is in bed 9?", "9"),
    ("compare beds 41 and 17", "17"),
    ("who is in bed 88?", "88"),
])
def test_a_bare_number_another_department_has_is_no_match(h, query, label):
    """Bed numbers repeat across wards, so a bare number is not a reach into
    another department, and it must not file a security event against the
    doctor. Bed 88's patient has also gone home."""
    r = _ask(h, "mine", query, open_bed="41")
    _assert_missing(r, MINE, h["mine"]["41"], label)
    assert h["mine"]["42"]["name"] not in r["context"]


@pytest.mark.parametrize("query", [
    "summarise SX-17",
    "bed SX-17 lactate?",
    "compare beds 41 and SX-17",
])
def test_a_lettered_code_another_department_holds_is_out_of_department(h, query):
    r = _ask(h, "mine", query, open_bed="42")
    assert r["qscope"] == "OUT_OF_SCOPE" and r["pctx"] is None
    assert h["mine"]["41"]["name"] not in r["context"]


# ══════════════════════════════════════════════════════════════════════════════
#  11. THE CONVERSATION HISTORY
# ══════════════════════════════════════════════════════════════════════════════

def _thread(*user_turns):
    out = []
    for t in user_turns:
        out += [{"role": "user", "content": t}, {"role": "assistant", "content": "NOACT|Noted."}]
    return out


def _asked(*user_turns):
    """A thread whose LAST answer was the server's which-patient question, as
    the chat route saves it (attempt 6 reads the question from the saved
    turn, not from the clinician's message). This one lists nobody, like a
    re-ask whose patient has left their bed: no bed in reply answers it
    (attempt 13). Until attempt 12 it read "- Bed 12: someone", which never
    parsed as an offer, and these replies passed only because a question
    listing nobody accepted ANY bed (review 12, B-A)."""
    out = _thread(*user_turns[:-1])
    out += [{"role": "user", "content": user_turns[-1]},
            {"role": "assistant", "content": "Which patient do you mean?\n\n"
                                             "Reply with the bed or the patient's name."}]
    return out


def _asked_about(patient, *user_turns, chart_open=True):
    """The same thread, with the question the server writes when it offers
    `patient`."""
    from services.sage_service import _which_patient_question
    out = _thread(*user_turns[:-1])
    out += [{"role": "user", "content": user_turns[-1]},
            {"role": "assistant", "content": _which_patient_question([patient], chart_open)}]
    return out


@pytest.mark.parametrize("missing", ["Summarise bed 77", "bed‑77 lactate?",
                                     "bed seventy seven lactate?"])
def test_a_missing_bed_in_the_last_turn_stops_the_history_anchor(h, missing):
    """No chart open. The doctor asked about bed 31, then about a bed nobody is
    in, then "and her lactate?". Walking past that turn to bed 31 would
    answer about a patient the doctor had moved on from."""
    r = _ask(h, "hist", "and her lactate?", history=_thread("How is bed 31 doing?", missing))
    assert r["pctx"] is None, "the thread was bound to a patient the doctor moved on from"
    assert h["hist"]["31"]["name"] not in r["context"]


def test_a_resolved_bed_in_history_is_offered_again(h):
    """Attempt 13: a bed in an earlier turn is never carried forward on its
    own. Sage offers that patient again and "yes" settles it (attempt 12
    answered from bed 12 after an aggregate question about it)."""
    _assert_confirms(_ask(h, "hist", "and her lactate?", history=_thread("How is bed 31 doing?")),
                     h["hist"]["31"])


# ══════════════════════════════════════════════════════════════════════════════
#  12. STORED LABEL SHAPES
# ══════════════════════════════════════════════════════════════════════════════

def test_a_bed_stored_as_bed_21_is_found_by_bed_21(h):
    _assert_chart(_ask(h, "label", "how is bed 21?", open_bed="Bed 22"), h["label"]["Bed 21"])


@pytest.mark.parametrize("query,bed", [
    ("MW/12 potassium?", "MW/12"),
    ("MW-12 potassium?", "MW/12"),
    ("bed MW/12 potassium?", "MW/12"),
    ("GW.12 potassium?", "GW.12"),
    ("bed GW.12 potassium?", "GW.12"),
    ("CARDIO-12 potassium?", "CARDIO-12"),
    ("room 204 potassium?", "Room 204"),
    ("cubicle 3 potassium?", "Cubicle 3"),
    ("ICU bed 3 potassium?", "ICU Bed 3"),
])
def test_every_stored_label_shape_is_found(h, query, bed):
    """Attempt 2 could not express "/", ".", six-letter prefixes or labels
    with "bed" in the middle, and fell to the open chart. The shape now comes
    from the stored label."""
    _assert_chart(_ask(h, "shapes", query, open_bed="Room 205"), h["shapes"][bed])


def test_a_bare_number_with_a_letter_asks(h):
    s = h["shapes"]
    _assert_asked(_ask(h, "shapes", "how is 12A?", open_bed="Room 205"), s["12A"],
                  absent=[s["Room 205"], s["MW/12"]])


def test_a_missing_room_is_no_match(h):
    r = _ask(h, "shapes", "room 206 potassium?", open_bed="Room 205")
    _assert_missing(r, SHAPES, h["shapes"]["Room 205"], "Room 206")


@pytest.mark.parametrize("query", ["HDU-2 lactate?", "summarise HDU-2", "hdu 2 lactate?"])
def test_a_bed_named_like_another_departments_ward_is_still_this_bed(h, query):
    _assert_chart(_ask(h, "hdu", query, open_bed="HDU-1"), h["hdu"]["HDU-2"])


@pytest.mark.parametrize("query", ["YQ-03 potassium?", "summarise YQ-03"])
def test_a_ward_emptied_today_still_counts_as_this_departments_code(h, query):
    r = _ask(h, "gone", query, open_bed="51")
    _assert_missing(r, GONE, h["gone"]["51"], "YQ-03")


# ══════════════════════════════════════════════════════════════════════════════
#  13. THE GATE, SPEED AND ROBUSTNESS
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("query,names", [
    ("cot 12 lactate?", True),
    ("bed num 12 lactate?", True),
    ("compare beds 12 and 14", True),
    ("what is the potassium of the patient in 12?", True),
    ("12 number bed lactate?", True),
    ("bed twelve, lactate?", True),
    ("can she stay in bed one more day?", False),
    ("is the patient in two minds about surgery?", False),
    ("raise bed 15 cm for her", False),
    ("potassium after 12 hours?", False),
    ("keep on bed rest", False),
    ("moved to this bed 2 days ago", False),
    ("keep head of bed 30 degrees", False),
    ("she fell out of bed. 2 nurses lifted her", False),
    ("can we shift her to bed 12?", False),
])
def test_the_name_gate_reads_bed_words_like_the_matcher(query, names):
    """The gate suppresses the open chart. It must count a bed written with a
    bed word, and must not count a dose, a duration, a new sentence or a
    destination, or the open patient's own question loses its chart."""
    assert sage._names_a_specific_patient(query, query.lower()) is names


def test_a_long_run_of_whitespace_cannot_stall_the_gate():
    q = "bed" + " " * 50_000 + "x"
    t0 = time.perf_counter()
    sage._names_a_specific_patient(q, q.lower())
    assert time.perf_counter() - t0 < 0.2


_LONG_UNITS = [
    ("num", "bed-a-"), ("num", "bed 7 "), ("num", "beds 7, "), ("a1", "a1 "),
    ("gm", "gm-1-"), ("num", "1-"), ("num", "12 "), ("num", "the patient in 12 and "),
    ("gm", "GM-103 "), ("num", "beds 7 to "),
]


def _rows(dept):
    sql, params = sage._scope_sql(
        "SELECT p.id, p.name, p.ward, p.bed, p.status FROM patients p WHERE p.status = 'admitted'",
        dept)
    with db_session() as db:
        return db.execute(sql, params).fetchall()


@pytest.mark.parametrize("scen,unit", _LONG_UNITS)
def test_the_bed_locator_is_linear_at_the_request_cap(h, scen, unit):
    """Attempt 2 took 57 s on "bed-a-" x 50,000 characters (the old code
    0.56 s), because each bed word re-read the rest of the question. The
    request cap is 512 KB. Names are left out here (empty word list): the
    fuzzy name matcher's own cost is older than this change."""
    dept = {"num": NUM, "a1": A1, "gm": GM}[scen]
    q = (unit * (512_000 // len(unit) + 1))[:512_000].lower()
    rows = _rows(dept)
    sage._refresh_census_name_tokens(sage.get_db())
    t0 = time.perf_counter()
    sage._has_bed_reference(q)
    sage._locate_patients(rows, [], q, [])
    assert time.perf_counter() - t0 < 4.0, unit


#: Questions that name a bed with a bed word also run the out-of-department
#: NAME check, and on 50 KB its fuzzy matcher alone takes 5 to 7 s ("bed 7 "
#: took 7.4 s on the code before this change, measured 4 October). That cost
#: is older than this change and is not touched by it; the bed locator's own
#: cost on these inputs is pinned at 512 KB above.
_REACHES_THE_NAME_CHECK = {"bed 7 ", "beds 7, ", "the patient in 12 and ", "beds 7 to "}


@pytest.mark.parametrize("scen,unit", [u for u in _LONG_UNITS if u[1] not in _REACHES_THE_NAME_CHECK])
def test_a_50k_question_is_answered_in_seconds_not_minutes(h, scen, unit):
    """End to end, for long questions that do not reach the name check."""
    q = (unit * (50_000 // len(unit) + 1))[:50_000]
    t0 = time.perf_counter()
    _ask(h, scen, q, open_bed=next(iter(h[scen])))
    assert time.perf_counter() - t0 < 3.0, unit


def test_a_huge_number_after_bed_does_not_crash_the_turn_or_the_thread(h):
    """int() refuses more than 4,300 digits. In attempt 2 this raised, and the
    history anchor re-read the turn, so every later follow-up raised too."""
    q = "bed " + "9" * 4400
    _ask(h, "num", q, open_bed="7")
    _ask(h, "num", "and her lactate?", open_bed="7", history=_thread(q))


# ══════════════════════════════════════════════════════════════════════════════
#  14. THROUGH THE ROUTE: the open patient's name stays off a no-match turn
# ══════════════════════════════════════════════════════════════════════════════

@pytest.fixture(scope="module")
def client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routes.sage_routes import router as sage_router
    app = FastAPI()
    app.include_router(sage_router)
    return TestClient(app)


def _prompt_for(client, monkeypatch, user, query, open_patient):
    from auth import create_token
    seen = {}

    def _capture(query, context, history, scope, **kw):
        seen["context"] = context

        async def _gen():
            route_info = kw.get("route_info")
            if route_info is not None:
                route_info.update({"provider": "stub", "model": "stub"})
            yield "ok"
        return _gen()

    monkeypatch.setattr("routes.sage_routes.query_ollama", _capture)
    headers = {"Authorization": f"Bearer {create_token(user['id'], 'doctor')}"}
    client.post("/api/sage/query", headers=headers, json={
        "query": query, "patient_id": open_patient["id"],
        "view_context": {"tab": "chart", "patient_id": open_patient["id"],
                         "patient_name": open_patient["name"]},
    })
    return seen.get("context", "")


def test_a_missing_bed_turn_does_not_name_the_open_patient_to_the_model(h, client, monkeypatch):
    """The early exit attaches no chart, but the screen line used to tell the
    model "viewing patient <open name>": the one patient it could drift to."""
    seven = h["num"]["7"]
    context = _prompt_for(client, monkeypatch, h["users"]["num"], "cot 15 lactate?", seven)
    assert "NO MATCHING PATIENT" in context
    assert seven["name"] not in context


def test_an_ordinary_turn_still_names_the_open_patient(h, client, monkeypatch):
    seven = h["num"]["7"]
    context = _prompt_for(client, monkeypatch, h["users"]["num"],
                          "what is the sepsis bundle?", seven)
    assert seven["name"] in context


# ══════════════════════════════════════════════════════════════════════════════
#  15. THE SECOND REVIEW (attempt 3, 4 October): every finding as a test
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("query", [
    "bed 12 day 3 of ceftriaxone, continue?",
    "bed 12 HR 140, give metoprolol?",
    "bed 12 h/o CKD, potassium ok?",
    "bed 12 d/c today?",
    "bed 12 d3 of tazo",
    "bed 12 L sided weakness",
    "bed 12 dose of vanc?",
    "bed 12 +ve blood cultures",
    "bed 12 g/c poor",
    "the patient in 12 h/o asthma, nebulise?",
    "Bed 12, 65/M, k/c/o DM, potassium 3.1, replace?",
    "Bed 12, 65M, k/c/o DM, potassium 3.1, replace?",
    "Bed 12, 65 M, k/c/o DM, potassium 3.1, replace?",
    "bed 12, 65/F, fever, cultures?",
    "bed 12, 140/90, start amlodipine?",
    "bed 12, 2/5 power both legs",
    "bed 12, 3 sets of cultures sent",
    "bed 12, 2 pints PRBC given",
    "bed 12th lactate?",
    "bed １２ lactate?",
])
def test_handover_phrasing_after_a_bed_word_is_that_bed(h, query):
    """Attempt 3 dropped the bed when a unit-like word followed it, and the
    open chart answered: "bed 12 HR 140" is ordinary handover phrasing. A list
    never runs on into "65/M" (the standard case-presentation opening)."""
    _assert_chart(_ask(h, "numw", query, open_bed="7"), h["numw"]["12"])


@pytest.mark.parametrize("query", [
    "bed 45 HR 140, give metoprolol?",
    "bed 45 day 3 of tazo",
    "bed 45 h/o CKD?",
    "bed 45 d/c today?",
    "what happened to bed 45 overnight?",
    "ABG from bed 45, interpret?",
    "talk to bed 45's family about prognosis?",
    "I went to bed 45 and she was hypotensive, what now?",
    "the labs from bed 45 are back?",
    "compare bed 12 to bed 45",
    "bed nos 45", "bed nr 45", "bed nr. 45", "bed n° 45", "bed nº 45",
    "bed (45) lactate?", "bed, 45 lactate?", "bed = 45", "bed\n45 lactate?",
    "bed 45th lactate?", "bed fourty five", "bed ninty", "bed a hundred and two",
    "bed 45 lactate?", "bed　45 lactate?", "bed​45 lactate?",
    "bed ４５ lactate?", "bed 0000045 lactate?",
])
def test_a_missing_bed_in_every_spelling_is_no_match(h, query):
    """R026 for every spelling the second reviewer found falling through to the
    open chart. A bed after "to" or "from" is a destination only after a
    movement verb ("shift her to bed 12")."""
    r = _ask(h, "numw", query, open_bed="7")
    assert r["pctx"] is None and h["numw"]["7"]["name"] not in r["context"], (query, r["qscope"])
    assert "NO MATCHING PATIENT" in r["context"], (query, r["qscope"])
    assert h["numw"]["12"]["name"] not in r["context"]


def test_a_number_too_long_to_be_a_bed_after_a_bed_word_is_no_match(h):
    r = _ask(h, "numw", "bed " + "9" * 4400, open_bed="7")
    assert r["pctx"] is None and "NO MATCHING PATIENT" in r["context"]


@pytest.mark.parametrize("query", [
    "bed 12 ward 6 potassium?",
    "bed 3 ward 5 potassium?",
    "bed 20 ward 5 potassium?",
])
def test_a_ward_written_after_the_bed_with_no_connector_still_narrows(h, query):
    """"12 ward" was read as ward 12, the real ward was never read, and the
    bed was not narrowed: ward 5's bed 12 answered "bed 12 ward 6"."""
    r = _ask(h, "wards2", query, open_bed="6/20")
    assert r["pctx"] is None and r["qscope"] != "OUT_OF_SCOPE"
    assert "NO MATCHING PATIENT" in r["context"]
    for p in h["wards2"].values():
        assert p["name"] not in r["context"]


@pytest.mark.parametrize("query", [
    "should I see the patient at 3?",
    "the patient at 3, potassium?",
])
def test_the_patient_at_n_is_a_time_or_a_bed_so_sage_asks(h, query):
    n = h["numw"]
    _assert_asked(_ask(h, "numw", query, open_bed="7"), n["3"], absent=[n["7"]])


@pytest.mark.parametrize("query", [
    "is the patient on 2 pressors?",
    "the patient on 2 antibiotics, de-escalate?",
    "pt on 2 inotropes, wean?",
    "is the patient on 3 antihypertensives enough?",
])
def test_the_patient_on_n_drugs_is_a_count(h, query):
    _assert_anchored(_ask(h, "numw", query, open_bed="7"), h["numw"]["7"])


@pytest.mark.parametrize("query", [
    "is there an ICU 3 bed free for her?",
    "ICU 3 nurse to 2 patients ok?",
    "she was in ICU 3 nights ago, any notes?",
    "is the patient in ICU 3 now?",
    "she needs ICU 3 nurse ratio?",
])
def test_a_unit_named_in_passing_is_not_a_reach_into_another_department(h, query):
    """Each false denial writes a security audit row against the doctor. ICU
    is another department's prefix here (ZERO holds ICU-03); CCU is this
    department's, and "a CCU 1 bed" is a free bed, not CCU-01's patient."""
    r = _ask(h, "gm", query, open_bed="RB-102")
    assert r["qscope"] != "OUT_OF_SCOPE", query
    _assert_anchored(r, h["gm"]["RB-102"])


def test_another_departments_code_written_as_a_code_is_out_of_department(h):
    r = _ask(h, "gm", "summarise ICU-03", open_bed="RB-102")
    assert r["qscope"] == "OUT_OF_SCOPE" and r["pctx"] is None


# ══════════════════════════════════════════════════════════════════════════════
#  16. AFTER SAGE ASKS: the next turn must not fall back to the open chart
# ══════════════════════════════════════════════════════════════════════════════

def test_an_ask_turn_stops_the_history_walk(h):
    """No chart open. "how is 12?" was only ASKED about; binding the thread to
    bed 12 (attempt 3) answered for a patient nobody confirmed."""
    r = _ask(h, "numw", "what next?", history=_thread("How is bed 21 doing?", "how is 12?"))
    assert r["pctx"] is None
    assert h["numw"]["12"]["name"] not in r["context"] or r["qscope"] == "DISAMBIGUATE"
    assert h["numw"]["21"]["name"] not in r["context"]


@pytest.mark.parametrize("reply", ["yes", "ok, and her potassium?", "the first one"])
def test_a_reply_that_names_nobody_after_an_ask_is_asked_again(h, reply):
    """With a chart open, "yes" after "did you mean bed 12?" names nobody, and
    the sticky anchor would answer it from the OPEN chart: the wrong patient."""
    r = _ask(h, "numw", reply, open_bed="7", history=_asked("how is 12?"))
    assert r["qscope"] == "DISAMBIGUATE" and r["pctx"] is None, r["qscope"]
    assert r["context"].startswith("Which patient do you mean?")
    assert h["numw"]["7"]["name"] not in r["context"]


def test_this_patient_after_an_ask_carries_nothing_that_named_a_bed(h):
    """Attempt 13f (Samuel, certain side only): "how is 12?" read bed 12, so
    "this patient" does not carry it to the open chart; the notice asks for
    the question again without the bed. Attempt 13e let "this patient"
    disown an unsure reading, and "lasix to 12 and kcl to 13" went to the
    open chart (review 13e, G2)."""
    from services.sage_service import OTHERS_NOT_CARRIED_NOTICE
    n = h["numw"]
    r = _ask(h, "numw", "this patient please", open_bed="7", history=_asked("how is 12?"))
    assert r["qscope"] == "NOT_CARRIED" and r["pctx"] is None, r["qscope"]
    assert r["context"].startswith(f"About {n['7']['name']}") and OTHERS_NOT_CARRIED_NOTICE in r["context"]


def test_a_bed_after_an_ask_is_that_bed(h):
    """A reply to the which-patient question is the confirmation itself."""
    n = h["numw"]
    _assert_chart(_ask(h, "numw", "bed 12", open_bed="7", history=_asked_about(n["12"], "how is 12?")),
                  n["12"])


@pytest.mark.parametrize("reply", ["12", "bed 12", "yes bed 12", "Bed 12.", "correct, bed 12"])
@pytest.mark.parametrize("open_bed", ["7", None])
def test_a_bed_after_a_question_that_offered_nobody_is_a_new_question(h, reply, open_bed):
    """Review 12, B-A: a re-ask listing nobody accepted any bed, and "12"
    opened the newcomer in bed 12 unlabelled. Now the bed is read as a new
    question: Sage asks about whoever is in bed 12 now."""
    n = h["numw"]
    r = _ask(h, "numw", reply, open_bed=open_bed, history=_asked("how is 12?"))
    _assert_confirms(r, n["12"])


def test_a_bed_with_another_chart_open_and_no_question_is_confirmed(h):
    n = h["numw"]
    _assert_confirms(_ask(h, "numw", "bed 12", open_bed="7", history=_thread("how is 12?")), n["12"])


# ══════════════════════════════════════════════════════════════════════════════
#  17. THE SERVER WRITES THE QUESTION (ruling A), names and beds alike
# ══════════════════════════════════════════════════════════════════════════════

def _sse(response):
    import json as _json
    return [_json.loads(line[6:]) for line in response.text.splitlines()
            if line.startswith("data: ")]


@pytest.mark.parametrize("scen,query,open_bed,expect", [
    ("numw", "how is 12?", "7", ("12",)),
    ("names", "how is Thekkekara doing?", "c", ("a", "b")),
])
def test_the_which_patient_question_never_reaches_the_model(h, client, monkeypatch, scen,
                                                            query, open_bed, expect):
    """Attempt 3 sent the model a bare list under the general prompt; in a
    replay it answered generically with invented citations. Today's name
    ambiguity went the same way."""
    from auth import create_token

    async def _must_not_run(*a, **kw):
        raise AssertionError("the model was asked to resolve which patient")
        yield ""  # pragma: no cover

    monkeypatch.setattr("routes.sage_routes.query_ollama", _must_not_run)
    user, open_p = h["users"][scen], h[scen][open_bed]
    r = client.post("/api/sage/query",
                    headers={"Authorization": f"Bearer {create_token(user['id'], 'doctor')}"},
                    json={"query": query, "patient_id": open_p["id"]})
    assert r.status_code == 200, r.text
    events = _sse(r)
    body = "".join(e.get("content", "") for e in events)
    assert body.startswith("Which patient do you mean?"), body[:200]
    for k in expect:
        assert h[scen][k]["name"] in body
    assert open_p["name"] not in body.split("\n\n")[1], "the open patient listed as a candidate"
    meta = next(e["metadata"] for e in events if "metadata" in e)
    assert meta["query_scope"] == "DISAMBIGUATE" and meta["patient_context_used"] is False


# ══════════════════════════════════════════════════════════════════════════════
#  18. THE ASK RATE: everyday numbers that are not beds keep the open chart
#
#  The second reviewer measured 61% asks on 54 number-bearing questions on a
#  ward numbered 1 to 30. These are the families behind those asks; every
#  number in them is a bed on this ward.
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("query", [
    "12 lead ECG done?",
    "repeat a 12 lead ECG in the morning?",
    "12-lead ECG shows ST depression?",
    "lactate came back 3, worried?",
    "potassium has been 3 for two days",
    "vanc trough 20, adjust?",
    "trop I 21, repeat?",
    "procalcitonin 2, stop antibiotics?",
    "vitamin D 12, replace?",
    "GCS dropped from 14 to 12",
    "CKD 3, adjust the dose?",
    "CHA2DS2-VASc 3, anticoagulate?",
    "HAS-BLED 2, ok?",
    "Mobitz 2, pace her?",
    "ECOG 2, fit for chemo?",
    "lasix 20 iv, enough?",
    "metoprolol 12 bd ok?",
    "dexamethasone 12 iv daily?",
    "insulin 12 before dinner?",
    "2 sets of blood cultures sent?",
    "3 episodes of vomiting, ondansetron?",
    "2 pints PRBC, check hb after?",
    "1 more dose of IV iron?",
    "3 more days of antibiotics?",
    "review at 3?",
    "temp spiked to 39 at 2, cultures?",
    "repeat CBC at 7?",
    "scan on 3 oct, any result?",
    "her son is 30, can he consent?",
])
def test_everyday_numbers_keep_the_open_chart(h, query):
    """Lower case where a capitalised word ("Hb", "Oct") would trip the older
    capitalised-name gate, which drops the chart for reasons outside beds."""
    _assert_anchored(_ask(h, "numw", query, open_bed="7"), h["numw"]["7"])


@pytest.mark.parametrize("scen,unit", [("wards2", "ward 6 bed 12 "), ("wards2", "bed 12 ward 6 "),
                                       ("wards2", "ward 5 bed 12 (ward 6) ")])
def test_ward_and_bed_pairs_are_linear_at_the_request_cap(h, scen, unit):
    """Attempt 3 compared every bed with every ward mention: 4.2 s at 50 KB,
    about seven minutes at 512 KB."""
    q = (unit * (512_000 // len(unit) + 1))[:512_000].lower()
    rows = _rows(WARDS2)
    sage._refresh_census_name_tokens(sage.get_db())
    t0 = time.perf_counter()
    sage._locate_patients(rows, [], q, [])
    assert time.perf_counter() - t0 < 4.0, unit


# ══════════════════════════════════════════════════════════════════════════════
#  19. THE THIRD REVIEW (attempt 4, 4 October): every finding as a test
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("query,beds", [
    ("give lasix to 12", ("12",)),
    ("hold metformin for 12", ("12",)),
    ("start ceftriaxone for 14", ("14",)),
    ("increase insulin for 12 to 10 units", ("12",)),
    ("what is the potassium of 12?", ("12",)),
    ("send potassium for 12", ("12",)),
    ("repeat lactate for 12 and 14", ("12", "14")),
    ("insulin for 12?", ("12",)),
    ("dose for 12?", ("12",)),
    ("trop for 12?", ("12",)),
    ("the sats of 12 are dropping", ("12",)),
    ("BP of 12 and 14?", ("12", "14")),
    ("temp of 12?", ("12",)),
    ("paracetamol for 12 and 14", ("12", "14")),
    ("can you look at 12?", ("12",)),
    ("have a look at 14 before rounds", ("14",)),
])
def test_a_bed_number_after_to_for_of_at_is_a_patient_not_a_value(h, query, beds):
    """Attempt 4 read these as values after a drug or lab word and answered
    from the OPEN chart; the four orders were drafted on bed 7. "for 12",
    "to 12", "of 12" and "at 12" introduce a patient as often as a value."""
    n = h["numw"]
    _assert_asked(_ask(h, "numw", query, open_bed="7"), *(n[b] for b in beds), absent=[n["7"]])


def test_a_score_written_with_an_article_is_still_a_value(h):
    _assert_anchored(_ask(h, "numw", "is a GCS of 12 acceptable?", open_bed="7"), h["numw"]["7"])


def test_a_middle_turn_sage_asked_about_stops_the_history_walk(h):
    r = _ask(h, "numw", "and her lactate?",
             history=_thread("how is bed 12?", "potassium of 14?"))
    assert r["pctx"] is None or r["pctx"]["patient_id"] != h["numw"]["12"]["id"]


@pytest.mark.parametrize("query", ["bed sx 17 potassium?", "bed SX 17", "cot sx 17?"])
def test_another_departments_letters_after_a_bed_word_are_that_bed(h, query):
    r = _ask(h, "mine", query, open_bed="41")
    assert r["qscope"] == "OUT_OF_SCOPE" and r["pctx"] is None, r["qscope"]


@pytest.mark.parametrize("query", [
    "bed १२ potassium?",
    "bed ١٢ potassium?",
    "bed 12 65M fever 3 days",
    "bed 12 65F k/c/o CKD",
    "bed 12 65 y M",
    "bed 12 2nd day of fever, plan?",
    "bed 12 3rd dose of tazo due?",
    "bed 12 1st prbc running",
    "bed 12 2 PRBC given",
    "bed 12 1 drain in situ",
    "bed 12 3 drains",
    "bed 12 2 sachets ors",
    "bed 12 2 attenders",
    "bed 12 with 3 drains, remove one?",
])
def test_more_handover_phrasing_is_that_bed(h, query):
    _assert_chart(_ask(h, "numw", query, open_bed="7"), h["numw"]["12"])


@pytest.mark.parametrize("query", [
    "out of bed 2nd day post-op, ok?",
    "mobilised out of bed 1st time today",
    "she has been in bed 3rd day now",
    "confined to bed 2nd week",
    "side room bed: 2nd episode of c diff?",
])
def test_an_ordinal_before_a_day_or_an_episode_is_not_a_bed(h, query):
    _assert_anchored(_ask(h, "numw", query, open_bed="7"), h["numw"]["7"])


@pytest.mark.parametrize("query", [
    "compare bed 12 with 45",
    "bed 12 then 45",
    "bed 12 and also 45",
])
def test_with_then_and_also_continue_a_list(h, query):
    r = _ask(h, "numw", query, open_bed="7")
    assert r["pctx"] is None and "NO MATCHING PATIENT" in r["context"], (query, r["qscope"])
    assert h["numw"]["12"]["name"] not in r["context"]


@pytest.mark.parametrize("query", [
    "transferred from ICU-03 yesterday, plan?",
    "she was in HDU-2 for 2 days",
    "step down from ICU-03 today",
    "can she go to ICU-03?",
    "is ICU-03 free?",
    "transferred from GM-103 yesterday, plan?",
])
def test_a_code_in_a_transfer_phrase_keeps_the_open_chart(h, query):
    """Ruling F: a place in the open patient's story, not a request for
    another patient's chart, and never a security event."""
    r = _ask(h, "gm", query, open_bed="RB-102")
    assert r["qscope"] != "OUT_OF_SCOPE", query
    _assert_anchored(r, h["gm"]["RB-102"])


def test_who_is_in_a_code_is_still_that_bed(h):
    _assert_chart(_ask(h, "gm", "who is in GM-103?", open_bed="RB-102"), h["gm"]["GM-103"])


@pytest.mark.parametrize("query", [
    "she had 2 seizures overnight",
    "she has only 1 kidney",
    "she has 3 children at home",
    "her 2 sons want to talk to you",
    "she smokes 12 a day",
    "3 options for her uti?",
])
def test_more_everyday_counts_keep_the_open_chart(h, query):
    _assert_anchored(_ask(h, "numw", query, open_bed="7"), h["numw"]["7"])


def test_a_destination_ward_keeps_the_open_chart(h):
    """The shared ward rule (6 October): "should she go to CCU?" does not name
    the ward; the open chart answers, labelled."""
    _assert_anchored(_ask(h, "gm", "should she go to CCU?", open_bed="RB-102"), h["gm"]["RB-102"])


@pytest.mark.parametrize("query", ["is a CCU 1 bed free for her?"])
def test_a_ward_of_ours_named_on_its_own_is_the_ward_census(h, query):
    """Since the shared ward rule (services/ward_reference, 4 October) a ward
    named anywhere is named, as in the old code; the census answers it, never
    one CCU patient's chart in place of the open one, never another
    department."""
    r = _ask(h, "gm", query, open_bed="RB-102")
    assert r["qscope"] == "BROAD", (query, r["qscope"])
    assert r["pctx"] is None


def test_a_ward_question_on_numbered_wards_gets_the_census(h):
    r = _ask(h, "wards2", "how is ward 5 doing?", open_bed="6/20")
    assert r["qscope"] == "BROAD", r["qscope"]


# ══════════════════════════════════════════════════════════════════════════════
#  20. AFTER SAGE ASKS, THROUGH THE REAL ROUTE WITH A SAVED CONVERSATION
#
#  Attempt 4's route returned before the code that saves the turn, so in
#  production the next turn never saw the question and "yes" went to the open
#  chart. Its unit tests supplied the history by hand and could not see it.
# ══════════════════════════════════════════════════════════════════════════════

def _turns(client, monkeypatch, user, open_patient, queries, view=False):
    """Post `queries` in ONE saved conversation; return [(body, meta, model_called)]."""
    from auth import create_token
    calls = []

    def _model(query, context, history, scope, **kw):
        calls.append(context)

        async def _gen():
            route_info = kw.get("route_info")
            if route_info is not None:
                route_info.update({"provider": "stub", "model": "stub"})
            yield "NOACT|stub answer."
        return _gen()

    monkeypatch.setattr("routes.sage_routes.query_ollama", _model)
    headers = {"Authorization": f"Bearer {create_token(user['id'], 'doctor')}"}
    conv = f"locator-{uuid.uuid4().hex[:10]}"
    out = []
    for q in queries:
        before = len(calls)
        body = {"query": q, "patient_id": open_patient["id"], "conversation_id": conv}
        if view:
            body["view_context"] = {"tab": "chart", "patient_id": open_patient["id"],
                                    "patient_name": open_patient["name"]}
        r = client.post("/api/sage/query", headers=headers, json=body)
        assert r.status_code == 200, r.text
        events = _sse(r)
        content = "".join(e.get("content", "") for e in events)
        meta = next((e["metadata"] for e in events if "metadata" in e), {})
        out.append((content, meta, len(calls) > before))
    return out


@pytest.mark.parametrize("reply", [
    "ok and her potassium?", "the first one", "no", "neither", "the other one",
    "both", "is she septic?", "potassium?", "and the lactate?", "creatinine?", "K?",
    "not this patient",
])
def test_any_reply_that_names_nobody_after_the_question_is_asked_again(h, client, monkeypatch, reply):
    n = h["numw"]
    (q1, m1, called1), (q2, m2, called2) = _turns(
        client, monkeypatch, h["users"]["numw"], n["7"], ["how is 12?", reply])
    assert m1["query_scope"] == "DISAMBIGUATE" and not called1
    assert m2["query_scope"] == "DISAMBIGUATE" and not called2, (reply, m2.get("query_scope"))
    assert q2.startswith("Which patient do you mean?")
    assert n["7"]["name"] not in q2


def test_a_name_question_is_asked_again_too(h, client, monkeypatch):
    nm = h["names"]
    (_q1, m1, _c1), (_q2, m2, called2) = _turns(
        client, monkeypatch, h["users"]["names"], nm["c"], ["how is Thekkekara doing?", "yes"])
    assert m1["query_scope"] == "DISAMBIGUATE"
    assert m2["query_scope"] == "DISAMBIGUATE" and not called2


@pytest.mark.parametrize("reply", ["bed 12", "12", "yes bed 12", "bed 12 please"])
def test_a_bed_after_the_question_is_that_bed(h, client, monkeypatch, reply):
    n = h["numw"]
    (_q1, _m1, _c1), (_q2, m2, called2) = _turns(
        client, monkeypatch, h["users"]["numw"], n["7"], ["how is 12?", reply])
    assert called2 and m2["query_scope"] == "PATIENT_SPECIFIC", (reply, m2.get("query_scope"))
    assert m2["patient_id"] == n["12"]["id"]


def test_this_patient_after_the_question_names_the_open_chart_in_the_notice(h, client, monkeypatch):
    n = h["numw"]
    (_q1, _m1, _c1), (q2, m2, called2) = _turns(
        client, monkeypatch, h["users"]["numw"], n["7"], ["how is 12?", "this patient"])
    assert not called2 and m2["query_scope"] == "NOT_CARRIED"
    assert q2.startswith(f"About {n['7']['name']}, bed 7:"), q2[:120]


def test_sage_keeps_asking_until_the_patient_is_named(h, client, monkeypatch):
    """Attempt 5 answered from the open chart after one re-ask, and "yes" after
    the order question drafted there. Attempt 6 asks again every time a reply
    names nobody; "this patient" (as the question invites) ends it."""
    n = h["numw"]
    turns = _turns(client, monkeypatch, h["users"]["numw"], n["7"],
                   ["how is 12?", "neither", "potassium?", "this patient", "potassium?"])
    for q, m, called in turns[:3]:
        assert m["query_scope"] == "DISAMBIGUATE" and not called, m.get("query_scope")
    q4, m4, called4 = turns[3]
    # The exchange read bed 12, so nothing of it is carried (attempt 13f) ...
    assert not called4 and m4["query_scope"] == "NOT_CARRIED"
    assert q4.startswith(f"About {n['7']['name']}, bed 7:"), q4[:120]
    # ... and asked again without the bed, the open chart answers.
    q5, m5, called5 = turns[4]
    assert called5 and m5["patient_id"] == n["7"]["id"], m5.get("query_scope")


def test_the_question_never_carries_the_screen_line(h, client, monkeypatch):
    """The route appended "ACTIVE VIEW ... viewing patient <name>" to every
    scope but OUT_OF_SCOPE, so the clinician saw it inside the question."""
    n = h["numw"]
    [(q1, m1, _c1)] = _turns(client, monkeypatch, h["users"]["numw"], n["7"], ["how is 12?"],
                             view=True)
    assert m1["query_scope"] == "DISAMBIGUATE"
    assert "ACTIVE VIEW" not in q1 and n["7"]["name"] not in q1


# ══════════════════════════════════════════════════════════════════════════════
#  21. RULING E: name the patient on every answer the clinician did not name
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("query", ["what is driving that?", "is she improving?"])
def test_an_unnamed_answer_starts_with_the_patients_name_and_bed(h, client, monkeypatch, query):
    n = h["numw"]
    [(body, meta, called)] = _turns(client, monkeypatch, h["users"]["numw"], n["7"], [query])
    assert called and meta["patient_id"] == n["7"]["id"]
    assert body.startswith(f"About {n['7']['name']}, bed 7:"), body[:120]


def test_a_named_answer_has_no_label(h, client, monkeypatch):
    """Named by NAME: not confirmed, not labelled."""
    n = h["numw"]
    [(body, meta, called)] = _turns(client, monkeypatch, h["users"]["numw"], n["7"],
                                    [f"how is {n['12']['name'].split()[0]} doing?"])
    assert called and meta["patient_id"] == n["12"]["id"]
    assert not body.startswith("About ")


@pytest.mark.parametrize("reply", ["yes", "go ahead", "yes please", "ok", "correct"])
def test_yes_confirms_a_question_with_one_candidate(h, client, monkeypatch, reply):
    """Attempt 9 (Samuel, 6 October): with a chart open another bed is
    confirmed first, and "yes" confirms it. The answer is labelled, since the
    clinician did not type the name."""
    n = h["numw"]
    (q1, m1, c1), (q2, m2, c2) = _turns(client, monkeypatch, h["users"]["numw"], n["7"],
                                        ["how is bed 12 doing?", reply])
    assert m1["query_scope"] == "DISAMBIGUATE" and not c1 and 'Reply "yes" for bed 12' in q1
    assert c2 and m2["patient_id"] == n["12"]["id"], (reply, m2.get("query_scope"))
    assert q2.startswith(f"About {n['12']['name']}, bed 12:"), q2[:120]


def test_yes_never_confirms_a_question_with_two_candidates(h, client, monkeypatch):
    w = h["wards"]
    (q1, m1, _c1), (q2, m2, c2) = _turns(client, monkeypatch, h["users"]["wards"], w["6/3"],
                                         ["how is 12?", "yes"])
    assert m1["query_scope"] == "DISAMBIGUATE" and q1.count("\n- Bed ") == 2, q1[:300]
    assert m2["query_scope"] == "DISAMBIGUATE" and not c2


@pytest.mark.parametrize("order", [
    "give him lasix 40 mg iv",
    "start her on ceftriaxone 1 g iv od",
    "stop the heparin",
])
def test_a_pronoun_order_on_the_open_chart_drafts_there_labelled(h, client, monkeypatch, order):
    """Ruling H: no question before a pronoun order on the open chart (attempt
    5 asked on 70% of realistic ward-round orders). The answer names the
    patient first, and the action card needs approval."""
    n = h["numw"]
    [(body, meta, called)] = _turns(client, monkeypatch, h["users"]["numw"], n["7"], [order])
    assert called and meta["patient_id"] == n["7"]["id"]
    assert body.startswith(f"About {n['7']['name']}, bed 7:"), body[:120]


@pytest.mark.parametrize("order", ["give lasix to 12", "hold metformin for 12",
                                   "give lasix 40 iv but not for this patient"])
def test_an_order_with_a_bed_number_or_a_negated_open_chart_asks_first(h, client, monkeypatch, order):
    n = h["numw"]
    [(body, meta, called)] = _turns(client, monkeypatch, h["users"]["numw"], n["7"], [order])
    assert not called and meta["query_scope"] == "DISAMBIGUATE", meta.get("query_scope")
    assert body.startswith("Which patient do you mean?")


def test_yes_after_the_question_never_drafts_an_order(h, client, monkeypatch):
    """Attempt 5: "give him lasix", "12", "yes", "yes" drafted on bed 7."""
    n = h["numw"]
    turns = _turns(client, monkeypatch, h["users"]["numw"], n["7"],
                   ["give lasix 40 iv to 12", "yes"])
    (q1, m1, c1), (q2, m2, c2) = turns
    assert not c1 and m1["query_scope"] == "DISAMBIGUATE", m1.get("query_scope")
    # "yes" to the one-candidate question is bed 12, never the open bed 7.
    assert c2 and m2["patient_id"] == n["12"]["id"], m2.get("query_scope")


@pytest.mark.parametrize("order,bed", [
    ("give lasix 40 mg iv to this patient", "7"),
])
def test_an_order_that_names_the_patient_goes_through(h, client, monkeypatch, order, bed):
    n = h["numw"]
    [(_body, meta, called)] = _turns(client, monkeypatch, h["users"]["numw"], n["7"], [order])
    assert called and meta["patient_id"] == n[bed]["id"]


def test_an_order_to_another_bed_is_confirmed_then_goes_through(h, client, monkeypatch):
    n = h["numw"]
    (_q1, m1, c1), (_q2, m2, c2) = _turns(client, monkeypatch, h["users"]["numw"], n["7"],
                                          ["give lasix 40 mg iv to bed 12", "yes"])
    assert not c1 and m1["query_scope"] == "DISAMBIGUATE"
    assert c2 and m2["patient_id"] == n["12"]["id"]


# ══════════════════════════════════════════════════════════════════════════════
#  22. THE VOICE LANE (ruling A and E there too)
# ══════════════════════════════════════════════════════════════════════════════

def _voice(query, user, patient_id, history=None):
    import asyncio
    from services.voice_agent_service import VoiceAgent
    agent = VoiceAgent(model_path="/nonexistent")

    def _no_model():
        raise AssertionError("the voice model was loaded for a which-patient turn")
    agent.load = _no_model

    async def _collect():
        return "".join([t async for t in agent.generate_stream(
            query, user, patient_id=patient_id, conversation_history=history)])
    return asyncio.run(_collect())


def test_voice_speaks_the_server_question_without_the_model(h):
    n = h["numw"]
    said = _voice("how is 12?", h["users"]["numw"], n["7"]["id"])
    assert said.startswith("Which patient do you mean?") and n["12"]["name"] in said


def test_voice_asks_again_after_its_own_question(h):
    n = h["numw"]
    said = _voice("yes", h["users"]["numw"], n["7"]["id"],
                  history=[{"query": "how is 12?", "response": "Which patient do you mean?"}])
    assert said.startswith("Which patient do you mean?")


# ══════════════════════════════════════════════════════════════════════════════
#  23. MEMORY: a long question is not kept in the cache
# ══════════════════════════════════════════════════════════════════════════════

def test_a_long_question_is_not_cached(h):
    """Attempt 4's cache pinned 457 MB with eight 512 KB questions. A long
    question is cached for its own turn only, and the turn empties it."""
    sage._tokens_and_index_cached.cache_clear()
    sage._bed_word_refs_cached.cache_clear()
    q = ("12 " * 200_000)[:512_000]
    _ask(h, "numw", q, open_bed="7")
    assert sage._tokens_and_index_cached.cache_info().currsize == 0
    assert sage._bed_word_refs_cached.cache_info().currsize == 0
    assert sage._tokens_and_index_long.cache_info().currsize == 0
    assert sage._bed_word_refs_long.cache_info().currsize == 0


# ══════════════════════════════════════════════════════════════════════════════
#  24. THE FIFTH REVIEW (attempt 5, 4 October) and the shipping bar (ruling G)
#
#  The bar: no wrong-patient path the old code got right, fewer wrong charts
#  than the old code on realistic questions, and every remaining miss labelled.
# ══════════════════════════════════════════════════════════════════════════════

def _never_a_certain_wrong_chart(r, h, scen, open_bed, wrong):
    """Allowed: the open chart (labelled by the route), or a question. Never
    the chart of `wrong` as if it had been asked for."""
    assert not (r["pctx"] is not None and r["pctx"]["patient_id"] == h[scen][wrong]["id"]), r["qscope"]
    if r["pctx"] is not None:
        assert r["pctx"]["patient_id"] == h[scen][open_bed]["id"], r["qscope"]


@pytest.mark.parametrize("query,wrong", [
    ("head end of bed 30-45 degrees, aspiration precautions?", "30"),
    ("keep head end of bed 30-45\u00b0", "30"),
    ("head-of-bed 30-45 degrees?", "30"),
    ("head end of the bed 30-45 degrees", "30"),
    ("head end elevation of bed 30-45 degrees?", "30"),
    ("bed 30-45\u00b0 up", "30"),
    ("head end of bed 30", "30"),
    ("raise the foot end of bed 15 cm", "15"),
    ("head end of the bed 30 cm up", "30"),
])
def test_a_bed_position_never_opens_that_beds_chart(h, query, wrong):
    """Attempt 5 opened bed 30's chart for "head end of bed 30-45 degrees",
    with no label. Asking about bed 30 is allowed; answering as bed 30 is not."""
    _never_a_certain_wrong_chart(_ask(h, "hob", query, open_bed="7"), h, "hob", "7", wrong)


@pytest.mark.parametrize("query", [
    "is the patient in bed 12 ready for discharge?",
    "bed 12 is ready for discharge, any pending reports?",
    "is bed 12 ready for the CT?",
    "bed 12 free of pain now?",
    "bed 12 empty stomach since midnight?",
    "going back to bed 12, what was the plan?",
    "we go to bed 12 after this, trop?",
])
def test_ready_free_going_and_was_in_do_not_turn_a_bed_into_a_place(h, query):
    """Attempt 5's place rule answered all of these from the open chart."""
    _assert_chart(_ask(h, "numw", query, open_bed="7"), h["numw"]["12"])


def test_a_bare_number_ready_for_discharge_asks(h):
    n = h["numw"]
    _assert_asked(_ask(h, "numw", "is 12 ready for discharge?", open_bed="7"), n["12"], absent=[n["7"]])


def test_this_departments_missing_code_with_ready_is_no_match(h):
    r = _ask(h, "gm", "is GM-145 ready for discharge?", open_bed="RB-102")
    _assert_missing(r, GM, h["gm"]["RB-102"], "GM-145")


@pytest.mark.parametrize("query", [
    "bed 12 2 vomits overnight", "bed 12 2 spikes", "bed 12 2 falls", "bed 12 2 fits",
    "bed 12 2 hypos", "bed 12 2 injections due", "bed 12 2 cultures sent",
    "bed 12 2 transfusions so far", "bed 12 2 boluses given",
])
def test_a_second_number_after_a_bed_never_says_no_such_patient(h, query):
    """Attempt 5 told the clinician bed 12 did not exist. The bed stands, or
    Sage asks; never "no matching patient", never the open chart."""
    r = _ask(h, "numw", query, open_bed="7")
    assert "NO MATCHING PATIENT" not in r["context"], query
    assert r["qscope"] in ("PATIENT_SPECIFIC", "DISAMBIGUATE"), r["qscope"]
    assert h["numw"]["12"]["name"] in r["context"] or (
        r["pctx"] is not None and r["pctx"]["patient_id"] == h["numw"]["12"]["id"])
    assert h["numw"]["7"]["name"] not in r["context"]


@pytest.mark.parametrize("query", [
    "Bed 12, 65 male, fever",
    "bed 12 65 Male DM HTN",
    "Bed 12, 65, M",
    "Bed 12, 72 old man, confused",
])
def test_case_presentation_openings_are_that_bed(h, query):
    _assert_chart(_ask(h, "numw", query, open_bed="7"), h["numw"]["12"])


@pytest.mark.parametrize("query", ["out of bed 3x today, ok?", "is the patient in 20s?"])
def test_counts_and_decades_are_not_beds(h, query):
    _assert_anchored(_ask(h, "numw", query, open_bed="7"), h["numw"]["7"])


@pytest.mark.parametrize("query", [
    "came out of ICU-03 yesterday, plan?",
    "back from ICU-03, continue antibiotics?",
    "returned from ICU-03 today",
    "send her back to ICU-03?",
    "refer her to ICU-03?",
    "escalate to ICU-03?",
    "upgrade to ICU-03 tonight?",
    "can we get ICU-03 for her?",
    "book ICU-03 for her",
    "post ICU-03 stay, how is she?",
    "after her ICU-03 stay, any concerns?",
])
def test_another_departments_code_in_passing_is_never_refused(h, query):
    """Attempt 5 refused 11 of these as "outside your department", each with
    a security audit row. The old code kept the chart, and so does this."""
    r = _ask(h, "gm", query, open_bed="RB-102")
    assert r["qscope"] != "OUT_OF_SCOPE", query
    _assert_anchored(r, h["gm"]["RB-102"])


@pytest.mark.parametrize("query", [
    "covid-19 rtpcr negative, de-isolate?",
    "COVID-19 vaccine given?",
    "h/o covid-19 last year",
])
def test_covid_19_is_never_a_bed_refusal(h, query):
    r = _ask(h, "miss", query, open_bed="7")
    assert r["qscope"] != "OUT_OF_SCOPE", query
    _assert_anchored(r, h["miss"]["7"])


def test_reaching_for_another_departments_code_is_refused(h):
    r = _ask(h, "mine", "how is MW/12?", open_bed="41")
    assert r["qscope"] == "OUT_OF_SCOPE" and r["pctx"] is None


@pytest.mark.parametrize("middle", [
    "is bed 14 ready for discharge?", "going to bed 14 now", "how is 45?", "and twenty?",
    "who is in 45?", "the old man in 45?", "patient no. 45?", "14 hr 140, af?",
    "send a nurse to bed 45", "bed 45 days 3 of tazo", "bed XII potassium?",
])
def test_the_history_walk_never_steps_back_past_a_bed_or_a_number(h, middle):
    """No chart open. Attempt 5 answered "and her lactate?" from bed 12, two
    turns back, past a turn about another bed."""
    r = _ask(h, "numw", "and her lactate?", history=_thread("bed 12 HR 140", middle))
    assert r["pctx"] is None or r["pctx"]["patient_id"] != h["numw"]["12"]["id"], middle


def test_a_bare_bed_in_answer_to_the_question_is_offered_again(h):
    """No chart open: "12" answered the question, so "and her lactate?" offers
    bed 12 again (attempt 13: a patient chosen by bed is never carried
    forward on their own; attempt 5 lost the thread, attempts 10 to 12
    followed it)."""
    n = h["numw"]
    hist = _asked_about(n["12"], "how is 12?", chart_open=False) + [
        {"role": "user", "content": "12"},
        {"role": "assistant", "content": "NOACT|Bed 12 is stable."}]
    r = _ask(h, "numw", "and her lactate?", history=hist)
    _assert_confirms(r, n["12"])
    assert "this patient" not in r["context"]


def test_a_census_answer_is_never_taken_for_the_which_patient_question(h):
    r = _ask(h, "wards2", "any of them septic?", open_bed="6/20",
             history=_thread("how is ward 5 doing?"))
    assert r["qscope"] != "DISAMBIGUATE", r["qscope"]


@pytest.mark.parametrize("reply", ["I don't mean this patient", "not for this patient",
                                   "no, not the current patient", "never this patient",
                                   "not the patient whose chart is open"])
def test_a_negated_this_patient_after_the_question_is_asked_again(h, reply):
    r = _ask(h, "numw", reply, open_bed="7", history=_asked("how is 12?"))
    assert r["qscope"] == "DISAMBIGUATE" and r["pctx"] is None, (reply, r["qscope"])


@pytest.mark.parametrize("query", [
    "bed 12 aur 45", "bed 12 n 45", "bed 12 + 45", "bed 12 as well as 45",
    "bed 12 along with 45", "bed 12 & then 45", "bed 12; 45", "bed 12 or else 45",
])
def test_more_list_words_never_drop_a_missing_bed(h, query):
    r = _ask(h, "numw", query, open_bed="7")
    assert r["pctx"] is None and "NO MATCHING PATIENT" in r["context"], (query, r["qscope"])


def test_gm_codes_on_a_60_bed_ward_are_linear(h):
    """Attempt 5: 4.5 s on 480 KB of "gm 512" on a 60-bed ward."""
    q = ("gm 512 " * 70_000)[:480_000]
    rows = _rows(GM60)
    sage._refresh_census_name_tokens(sage.get_db())
    t0 = time.perf_counter()
    sage._has_bed_reference(q)
    sage._locate_patients(rows, [], q, [])
    assert time.perf_counter() - t0 < 3.0


def test_the_label_is_in_the_finished_answer_and_the_saved_turn(h, client, monkeypatch):
    """Attempt 5 streamed the label as its own event; the screen replaces the
    streamed text with structured_response.concise_answer, which lacked it,
    and so did the saved turn."""
    from auth import create_token
    from services.sage_service import get_conversation_history

    def _model(query, context, history, scope, **kw):
        async def _gen():
            route_info = kw.get("route_info")
            if route_info is not None:
                route_info.update({"provider": "stub", "model": "stub"})
            yield ("<action_intent>None</action_intent>"
                   "<concise_answer>Her lactate is 2.1 mmol/L.</concise_answer>")
        return _gen()

    monkeypatch.setattr("routes.sage_routes.query_ollama", _model)
    n = h["numw"]
    user = h["users"]["numw"]
    conv = f"label-{uuid.uuid4().hex[:8]}"
    r = client.post("/api/sage/query",
                    headers={"Authorization": f"Bearer {create_token(user['id'], 'doctor')}"},
                    json={"query": "and her lactate?", "patient_id": n["7"]["id"],
                          "conversation_id": conv})
    events = _sse(r)
    structured = next((e["structured_response"] for e in events if "structured_response" in e), None)
    label = f"About {n['7']['name']}, bed 7:"
    assert structured is not None and structured.get("concise_answer", "").startswith(label), structured
    saved = get_conversation_history(user["id"], limit=4, conversation_id=conv)
    assert any(label in (m.get("content") or "") for m in saved if m.get("role") == "assistant"), saved



# ══════════════════════════════════════════════════════════════════════════════
#  25. THE SIXTH REVIEW (attempt 6, 4 October): every finding as a test
#
#  Attempt 6 met the bar on realistic traffic and failed it on two classes:
#  number words read as certain beds (unlabelled, where the old code kept the
#  open chart), and a leading bed number or code thrown away as a value, so an
#  order drafted on the open patient where the old code asked.
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("query", [
    "keep her flat in bed one hour after the LP",
    "can she stay in bed one more day?",
    "she has been in bed two nights now, DVT prophylaxis?",
    "lying in bed one week, any pressure sores?",
    "is the patient in two minds about surgery?",
    "is the patient in one of the side rooms MRSA positive?",
    "the patient in three way foley, irrigate?",
    "the lady in one of the side rooms, MRSA?",
    "the man in two minds about the stent",
    "is the patient in one piece after the fall?",
    "the patient in four point restraints, review?",
    "the patient in one go can have all meds?",
    "can she sit in bed one more hour?",
    "bed two pillows, orthopnoea?",
    "in bed one month after the stroke, rehab?",
    "bed one week then mobilise?",
    "keep in bed twelve hours after the procedure",
    "keep in bed twelve hrs after the procedure",
    "bed ten pm onwards, sedation?",
    "she stayed in bed eleven days at home",
    "bed twelve day 3 of tazo?",
])
def test_number_words_inside_a_phrase_are_ordinary_words(h, query):
    """Attempt 6 opened bed 1 for "can she stay in bed one more day?"; the old
    code never read number words. A number word is a bed only when it is the
    whole reference ("bed twelve?"); otherwise the open chart answers, labelled."""
    _assert_anchored(_ask(h, "numw", query, open_bed="7"), h["numw"]["7"])


@pytest.mark.parametrize("query,bed", [
    ("raise bed 12 cm for her", "12"),
    ("elevate the bed 30 cm at the head", "30"),
    ("keep her in bed 30 min after the procedure", "30"),
    ("lie in bed 20 min after the injection", "20"),
    ("flat in bed 12 hour post angio?", "12"),
    ("in bed 1 hour post LP, then mobilise?", "1"),
    ("in bed 2 nights in a row she was agitated", "2"),
    ("bed 2 pillows, still orthopnoeic?", "2"),
    ("turn her in bed 2 hourly", "2"),
    ("bed 12 am bloods back?", "12"),
    ("bed 12 pm insulin dose?", "12"),
    ("bed 12 hourly obs", "12"),
    ("bed 21 inch waist?", "21"),
])
def test_a_bed_word_then_a_length_or_a_time_is_asked_never_certain(h, query, bed):
    """Attempt 6 read "raise bed 15 cm" and "keep her in bed 30 min" as certain
    beds; the old code asked. Sage asks when that bed is occupied."""
    n = h["numw"]
    _assert_asked(_ask(h, "numw", query, open_bed="7"), n[bed], absent=[n["7"]])


@pytest.mark.parametrize("query", [
    "turn her in bed 2 hourly",
    "flat in bed 1 hour post LP, then mobilise?",
    "raise bed 15 cm for her",
    "in bed 2 nights in a row she was agitated",
    "bed 2 pillows, still orthopnoeic?",
    "she sleeps in bed 4 pillows high",
    "back to bed 1 hour after chair, ok?",
    "in bed 10 pm onwards, sleeping tablet?",
])
def test_the_same_phrases_on_a_ward_without_those_beds_keep_the_open_chart(h, query):
    """On Orb's own lettered naming there is no bed 2: the phrase is text, not
    a missing bed (attempt 6 said NO MATCHING PATIENT)."""
    _assert_anchored(_ask(h, "gm60", query, open_bed="GM-507"), h["gm60"]["GM-507"])


@pytest.mark.parametrize("query,bed", [
    ("12 iv fluids continue?", "12"),
    ("12 iv access lost", "12"),
    ("12 iv abx day 5", "12"),
    ("12 nebs given?", "12"),
    ("12 drain output?", "12"),
    ("12 drains removed?", "12"),
    ("12 mg 1.6, replace?", "12"),
    ("12 po intake poor", "12"),
    ("12 sos ondansetron given?", "12"),
    ("12 stat ecg please", "12"),
    ("12 hr 140 af?", "12"),
    ("12 bd insulin dose?", "12"),
    ("14 day 5 of meropenem, stop?", "14"),
    ("14 days of meropenem done?", "14"),
    ("12 iv lasix 40 stat", "12"),
    ("give 12 iv lasix 40", "12"),
    ("12 stat lasix 40 iv", "12"),
    ("12 nebs duolin q6h", "12"),
    ("12 iv ns 100 ml/hr", "12"),
    ("14 stat paracetamol 1 g iv", "14"),
    ("- 12 iv fluids continue?", "12"),
    ("ok thanks\n12 iv fluids continue?", "12"),
])
def test_a_number_that_opens_a_handover_line_is_asked_about(h, query, bed):
    """Attempt 6 threw these away as values and the order was drafted on the
    open patient. The old code asked; so does Sage."""
    n = h["numw"]
    _assert_asked(_ask(h, "numw", query, open_bed="7"), n[bed], absent=[n["7"]])


@pytest.mark.parametrize("query", [
    "2 sets of blood cultures sent?",
    "3 episodes of vomiting since morning, what next?",
    "4 loose stools today, send stool culture?",
    "1 more dose of IV iron?",
    "12 lead ECG done?",
    "2 units ffp before the ascitic tap?",
    "3 options for her uti?",
    "2 no. beds free?",
    "12 number beds free in ICU?",
    "lasix 20 iv now?",
    "ondansetron 4 iv ok?",
    "amlodipine 5 od enough?",
    "metoprolol 12 bd, increase?",
])
def test_everyday_counts_and_doses_still_keep_the_open_chart(h, query):
    _assert_anchored(_ask(h, "numw", query, open_bed="7"), h["numw"]["7"])


@pytest.mark.parametrize("query,both", [
    ("12/14 potassium?", True),
    ("ceftriaxone 12 and 14?", True),
    ("potassium of 12 or 14", True),
])
def test_two_numbers_joined_as_a_list_are_both_asked_about(h, query, both):
    n = h["numw"]
    _assert_asked(_ask(h, "numw", query, open_bed="7"), n["12"], n["14"], absent=[n["7"]])


@pytest.mark.parametrize("query", [
    "sats between 88 and 92 on 2 L?",
    "follow up in 2 weeks or 4?",
])
def test_a_range_is_not_a_list_of_beds(h, query):
    _assert_anchored(_ask(h, "numw", query, open_bed="7"), h["numw"]["7"])


@pytest.mark.parametrize("query", [
    "GM-512 HR 140, metoprolol?", "GM-512 hr 140", "GM-512 day 3 of tazo, stop?",
    "GM-512 d3 tazo", "GM-512 iv fluids?", "GM-512 F 65 fever", "GM-512 M/65 chest pain",
    "GM-512 am bloods back?", "GM-512 stat ecg", "GM-512 nebs given?", "GM-512 po intake poor",
    "GM-512 drain output?", "GM-512 bd insulin?", "GM-512 sos ondansetron?",
    "GM-512 mg 1.6 replace?", "GM-512 iv lasix 40 stat", "gm512 hr 140",
    "GM-514 stat paracetamol 1 g iv",
])
def test_this_departments_code_before_a_unit_or_route_is_that_bed(h, query):
    """Attempt 6 dropped the code as a value and drafted "GM-512 iv lasix 40
    stat" on the open patient; the old code asked."""
    want = "GM-514" if "514" in query else "GM-512"
    _assert_chart(_ask(h, "gm60", query, open_bed="GM-507"), h["gm60"][want])


@pytest.mark.parametrize("query", ["GM 512 HR 140", "gm 512 iv fluids?"])
def test_a_spaced_code_before_a_unit_is_asked_about(h, query):
    g = h["gm60"]
    _assert_asked(_ask(h, "gm60", query, open_bed="GM-507"), g["GM-512"], absent=[g["GM-507"]])


@pytest.mark.parametrize("query", ["GM-575 hr 140", "GM-575 iv fluids?", "GM-575 iv lasix 40 stat"])
def test_a_missing_code_before_a_unit_is_no_match(h, query):
    r = _ask(h, "gm60", query, open_bed="GM-507")
    _assert_missing(r, GM60, h["gm60"]["GM-507"], "GM-575")


@pytest.mark.parametrize("query", ["ceftriaxone gm 12 hourly?", "2 gm 12 hourly ceftriaxone?"])
def test_a_dose_in_grams_is_still_a_dose(h, query):
    _assert_anchored(_ask(h, "gm", query, open_bed="RB-102"), h["gm"]["RB-102"])


@pytest.mark.parametrize("query,bed", [
    ("c 4 potassium?", "C-4"),
    ("n 1 potassium?", "N-1"),
    ("how is c 4?", "C-4"),
    ("cabin 5 iv fluids?", "Cabin 5"),
])
def test_a_code_typed_with_a_space_is_asked_about(h, query, bed):
    """The old code found these (squashed); attempt 6 answered from the open chart."""
    sp = h["spaced"]
    _assert_asked(_ask(h, "spaced", query, open_bed="Cabin 6"), sp[bed], absent=[sp["Cabin 6"]])


@pytest.mark.parametrize("query", ["is a 1 point rise significant?", "x 3 today", "c 9 potassium?"])
def test_a_spaced_letter_that_is_a_word_or_no_bed_keeps_the_open_chart(h, query):
    _assert_anchored(_ask(h, "spaced", query, open_bed="Cabin 6"), h["spaced"]["Cabin 6"])


def test_twelve_a_typed_with_a_space_is_asked_about_as_bed_12a(h):
    sp = h["shapes"]
    for q in ("12 A potassium?", "bed 12 A potassium?"):
        _assert_asked(_ask(h, "shapes", q, open_bed="Room 204"), sp["12A"], absent=[sp["Room 204"]])


@pytest.mark.parametrize("query", [
    "bed 12 a bit better today?",
    "bed 12 drops sats when flat",
    "bed 12 feet are cold",
    "she was in bed 12 last night, how is she now?",
])
def test_a_bed_followed_by_words_sage_does_not_recognise_is_asked_about(h, query):
    """The price of attempt 8 (closed list on the CERTAIN side): a bed whose
    surrounding words are not recognised is asked about when occupied, never
    read as certain. Attempt 7 answered these from bed 12; it also answered
    "keep her flat in bed 12 hr after the angio" from bed 12."""
    n = h["numw"]
    _assert_asked(_ask(h, "numw", query, open_bed="7"), n["12"], absent=[n["7"]])


@pytest.mark.parametrize("query", [
    "show me the ICU-03 notes for her",
    "details of ICU-03 admission for her?",
    "update on ICU-03 cultures for her?",
    "review the ICU-03 notes for her",
    "what about ICU-03 course, any complications?",
    "check on ICU-03 culture reports for her",
])
def test_a_reach_verb_before_another_departments_code_mid_sentence_is_not_a_reach(h, query):
    """Attempt 6 refused these, each with a security audit row."""
    r = _ask(h, "gm", query, open_bed="RB-102")
    assert r["qscope"] != "OUT_OF_SCOPE", query
    _assert_anchored(r, h["gm"]["RB-102"])


def test_summary_of_a_stay_elsewhere_is_not_a_reach(h):
    """The census lane answers "summary of" (a BROAD word), as in the old code."""
    assert _ask(h, "gm", "summary of ICU-03 stay?", open_bed="RB-102")["qscope"] != "OUT_OF_SCOPE"


@pytest.mark.parametrize("query", ["how is ICU-03?", "how is ICU-03 doing?", "summarise ICU-03",
                                   "update on ICU-03 please"])
def test_a_reach_verb_then_the_code_alone_is_out_of_department(h, query):
    assert _ask(h, "gm", query, open_bed="RB-102")["qscope"] == "OUT_OF_SCOPE", query


@pytest.mark.parametrize("query", [
    "bed 12, 45 potassium?",
    "bed 12, bed 45 potassium?",
    "bed 12, 45 and 14",
])
def test_a_missing_bed_after_a_comma_is_no_match(h, query):
    """D3. Attempt 6 made a comma item after a singular "bed" unsure and
    answered from bed 12 alone."""
    r = _ask(h, "numw", query, open_bed="7")
    assert r["pctx"] is None and "NO MATCHING PATIENT" in r["context"], (query, r["qscope"])


@pytest.mark.parametrize("query", [
    "Bed 12, 65, M, fever",
    "bed 12, 65 male, fever",
    "Bed 12, 65 yrs, fever",
    "bed 12, 2 nurses needed",
])
def test_an_age_sex_or_count_after_a_comma_is_not_a_bed(h, query):
    _assert_chart(_ask(h, "numw", query, open_bed="7"), h["numw"]["12"])


def test_the_label_never_goes_inside_an_act_reply():
    """parse_fast_response needs the action JSON right after ACT|; attempt 6
    put the label there and the turn broke on reload."""
    from routes.sage_routes import _labelled, parse_fast_response
    label = "About Somebody, bed 7:"
    act = 'ACT|{"action_type": "medication_order", "drug": "furosemide"} Drafting it.'
    assert _labelled(act, label) == act
    assert parse_fast_response(_labelled(act, label))["action_intent"].startswith("{")
    noact = "NOACT| Her lactate is 2.1."
    out = _labelled(noact, label)
    assert out.startswith("NOACT|") and parse_fast_response(out)["answer"].startswith(label), out
    assert _labelled("  NOACT|Fine.", label).lstrip().startswith("NOACT|" + label)


def test_the_history_memo_is_per_thread(h):
    """Two turns at once must never share one memo (attempt 6 used a module
    dict keyed on id())."""
    import threading
    rows = _rows(NUMW)
    sage._forget_long_question()
    sage._history_located(rows, [], "bed 12 potassium?")
    seen = {}

    def _other():
        seen["memo"] = dict(getattr(sage._HISTORY_LOCATED, "memo", None) or {})

    t = threading.Thread(target=_other)
    t.start()
    t.join()
    assert seen["memo"] == {}
    assert sage._HISTORY_LOCATED.memo
    sage._forget_long_question()
    assert sage._HISTORY_LOCATED.memo == {}


def test_a_memo_entry_is_never_served_for_another_census_list(h):
    rows = list(_rows(NUMW))
    sage._forget_long_question()
    first = sage._history_located(rows, [], "bed 12 potassium?")
    other = [r for r in rows if str(r["bed"]) != "12"]
    again = sage._history_located(other, [], "bed 12 potassium?")
    assert first.matched and not again.matched
    sage._forget_long_question()


@pytest.mark.parametrize("query", ["bed 45 2 vomits", "bed 45 2 vomits overnight, plan?"])
def test_a_missing_bed_then_a_second_number_is_no_match(h, query):
    """Unsure only between bed 45 and bed 452; neither is here (R026)."""
    r = _ask(h, "numw", query, open_bed="7")
    assert r["pctx"] is None and "NO MATCHING PATIENT" in r["context"], (query, r["qscope"])


@pytest.mark.parametrize("query,bed", [
    ("keep bed 30 45 degrees", "30"),
    ("bed 12 2 days post-op, drain out?", "12"),
])
def test_a_second_number_with_a_time_or_angle_makes_the_bed_unsure(h, query, bed):
    n = h["numw"]
    _assert_asked(_ask(h, "numw", query, open_bed="7"), n[bed], absent=[n["7"]])


# ══════════════════════════════════════════════════════════════════════════════
#  26. THE SEVENTH REVIEW (attempt 7, 4 October) and attempt 8: the closed list
#      sits on the CERTAIN side
#
#  Attempts 3 to 7 listed what makes a bed word NOT a bed; every unlisted word
#  stayed a certain bed. "transferred from ICU bed 12" opened OUR bed 12 and an
#  order was drafted there; "flat in bed 12 hr" opened bed 12. Now a bed word is
#  certain only after a recognised lead-in, or at the start of a clause with a
#  recognised continuation; anything else is asked about when occupied and is
#  text when not.
# ══════════════════════════════════════════════════════════════════════════════

_B1 = [
    ("transferred from ward 6 bed 12 yesterday, continue meropenem?", "12"),
    ("shifted from ward 6 bed 12 last night, anything pending?", "12"),
    ("received from ward 6 bed 14, sugar charting?", "14"),
    ("came from ICU bed 12 this morning, how is she?", "12"),
    ("transferred from ICU bed 12, still on noradrenaline?", "12"),
    ("ex ICU bed 12, wean the oxygen?", "12"),
    ("she was in ICU bed 12 for a week, any old cultures?", "12"),
    ("she was in ER bed 12 overnight, troponin repeated?", "12"),
    ("in casualty bed 14 she got 2 L of fluids, more?", "14"),
    ("back from cath lab bed 12, groin ok?", "12"),
    ("received from OT recovery bed 12 post lap chole, analgesia?", "12"),
    ("moved out of HDU bed 12 today, step down obs?", "12"),
    ("is ICU bed 12 free for her?", "12"),
    ("transferred from ICU bed 3 yesterday, continue meropenem?", "3"),
    ("bed 12 in ICU is septic, can we take her?", "12"),
    ("ex-MICU bed 14, on room air now, discharge?", "14"),
    ("from ICU, bed 12, to our ward today, plan?", "12"),
    ("the ICU bed 12 patient is coming to us, what to prepare?", "12"),
    ("transferred to ICU bed 12 last night, any update?", "12"),
    ("transferred from ICU bed 12, start lasix 40 iv", "12"),
    ("came from ICU bed 12 this morning, give lasix 40 iv stat", "12"),
    ("ICU beds 12, any free tonight?", "12"),
    ("total no. of beds 30, how many discharges today?", "30"),
    ("our ward has beds 30, all full, where to admit?", "30"),
]


@pytest.mark.parametrize("query,bed", _B1)
def test_another_units_bed_is_never_our_certain_bed(h, query, bed):
    """B1: our bed with that number is asked about (it is occupied); never
    read as certain, so an order in the turn is never drafted on it."""
    n = h["numw"]
    _assert_asked(_ask(h, "numw", query, open_bed="7"), n[bed], absent=[n["7"]])


@pytest.mark.parametrize("query", [q for q, _b in _B1])
def test_another_units_bed_on_a_ward_without_that_number_keeps_the_chart(h, query):
    """On Orb's GM naming there is no bed 12: the phrase is text, not a missing
    bed (attempt 7 said NO MATCHING PATIENT), and never another department."""
    r = _ask(h, "gm60", query, open_bed="GM-507")
    assert r["qscope"] != "OUT_OF_SCOPE", query
    _assert_anchored(r, h["gm60"]["GM-507"])


@pytest.mark.parametrize("query,bed", [
    ("keep her flat in bed 12 hr after the angio", "12"),
    ("keep her flat in bed 12 hr after the angio, give lasix 40 iv", "12"),
    ("lying in bed 20 hr a day, pressure care?", "20"),
    ("raise the bed 14 cms", "14"),
    ("raise the bed 12 centimetres for her", "12"),
    ("lie in bed 20 secs then stand", "20"),
    ("her bed 12 metres from the nursing station, move her closer?", "12"),
    ("bed 12 noon onwards nil by mouth?", "12"),
    ("bed 2 o clock sedation?", "2"),
    ("bed 3 per cent incline?", "3"),
    ("bed 12 hr ago she was fine, now drowsy", "12"),
    ("in bed 2 week now, LMWH?", "2"),
    ("the 2nd bed from the window is hers, she is agitated", "2"),
])
def test_a_bed_with_a_unit_sage_does_not_know_is_asked_about(h, query, bed):
    """B2: attempt 7 read these as certain beds where the old code asked."""
    n = h["numw"]
    _assert_asked(_ask(h, "numw", query, open_bed="7"), n[bed], absent=[n["7"]])


@pytest.mark.parametrize("query,wrong", [
    ("pillows in bed: two", "2"),
    ("how many hours in bed - twelve?", "12"),
    ("Days in bed: three.", "3"),
])
def test_a_number_word_after_a_separator_is_not_a_bed(h, query, wrong):
    """B3: attempt 7 opened bed 2 for "pillows in bed: two"."""
    _never_a_certain_wrong_chart(_ask(h, "numw", query, open_bed="7"), h, "numw", "7", wrong)


@pytest.mark.parametrize("query,bed", [
    ("bed 12 start ceftriaxone 1 g iv od", "12"),
    ("bed 14 hold the enoxaparin", "14"),
    ("bed 12 HR 140, metoprolol?", "12"),
    ("bed 12 day 3 of tazo", "12"),
    ("what about bed 12 drops sats when flat", "12"),
    ("how is bed 12 noon onwards?", "12"),
    ("Bed 12 - 60, female, CAP day 3", "12"),
])
def test_recognised_shapes_stay_certain(h, query, bed):
    _assert_chart(_ask(h, "numw", query, open_bed="7"), h["numw"][bed])


def test_a_number_after_a_comma_in_an_unknown_phrase_is_asked_about(h):
    """D3 kept (Samuel, 4 October): "bed 12, 140 systolic" is not certain
    about bed 12; with no bed 140 Sage asks about bed 12 (attempt 7 said no
    match)."""
    n = h["numw"]
    _assert_asked(_ask(h, "numw", "bed 12, 140 systolic, labetalol?", open_bed="7"), n["12"],
                  absent=[n["7"]])


def test_our_own_label_written_around_the_bed_word_is_certain(h):
    """"ICU Bed 3" is a label on this ward: the unit word is ours."""
    _assert_chart(_ask(h, "shapes", "summarise icu bed 3 drops sats", open_bed="Room 204"),
                  h["shapes"]["ICU Bed 3"])


def test_a_ward_of_ours_beside_the_bed_makes_it_certain(h):
    _assert_chart(_ask(h, "wards2", "ward 6 bed 3 drops sats when flat", open_bed="5/12"),
                  h["wards2"]["6/3"])


@pytest.mark.parametrize("query", ["K3.2 replace?", "K5.8, give calcium gluconate?"])
def test_a_one_letter_code_with_a_decimal_is_a_value(h, query):
    _assert_anchored(_ask(h, "kward", query, open_bed="K-7"), h["kward"]["K-7"])


def test_a_one_letter_code_in_an_unknown_phrase_is_asked_about(h):
    k = h["kward"]
    _assert_asked(_ask(h, "kward", "K4 in the morning?", open_bed="K-7"), k["K-4"], absent=[k["K-7"]])
    _assert_chart(_ask(h, "kward", "how is K4?", open_bed="K-7"), k["K-4"])


@pytest.mark.parametrize("query,limit", [
    ("how is 12" + " " * 40_000 + "?", 1.0),
    ("12" + " " * 100_000, 1.0),
    (("12" + " " * 2_000) * 250, 3.0),
    (("a 1" + " " * 2_000) * 250, 3.0),
])
def test_long_runs_of_spaces_are_linear(h, query, limit):
    """Attempt 7: 15 s for "how is 12" plus 40,000 spaces, 46 s for 100,000
    (optional characters between two whitespace runs backtracked)."""
    t0 = time.perf_counter()
    _ask(h, "numw", query, open_bed="7")
    assert time.perf_counter() - t0 < limit


def test_a_reloaded_order_turn_keeps_the_label(h, client, monkeypatch):
    """Attempt 7 reloaded "Drafted request for ..." with no label: /history
    shows the stored safe answer, which never carried it."""
    from auth import create_token

    def _model(query, context, history, scope, **kw):
        async def _gen():
            route_info = kw.get("route_info")
            if route_info is not None:
                route_info.update({"provider": "stub", "model": "stub"})
            yield ('<action_intent>{"action_type": "add_medication", "medication": "furosemide", '
                   '"dose": "40 mg", "route": "iv"}</action_intent>'
                   '<concise_answer>Drafting furosemide.</concise_answer>')
        return _gen()

    monkeypatch.setattr("routes.sage_routes.query_ollama", _model)
    n = h["numw"]
    user = h["users"]["numw"]
    headers = {"Authorization": f"Bearer {create_token(user['id'], 'doctor')}"}
    r = client.post("/api/sage/query", headers=headers,
                    json={"query": "give her lasix 40 iv", "patient_id": n["7"]["id"],
                          "conversation_id": f"reload-{uuid.uuid4().hex[:8]}"})
    events = _sse(r)
    structured = next((e["structured_response"] for e in events if "structured_response" in e), None)
    label = f"About {n['7']['name']}, bed 7:"
    assert structured is not None and structured.get("concise_answer", "").startswith(label), structured
    rows = client.get("/api/sage/history", headers=headers).json()["messages"]
    last = [m for m in rows if m.get("role") == "assistant"][-1]
    assert (last.get("content") or "").startswith(label), last.get("content")


# ══════════════════════════════════════════════════════════════════════════════
#  27. THE EIGHTH REVIEW (attempt 8, 6 October) and attempt 9: CONFIRM ANOTHER
#      BED WHILE A CHART IS OPEN (Samuel, 6 October)
#
#  Attempt 8 still read "her husband in bed 12 has covid" and "insulin meant
#  for bed 12" as bed 12 and opened that chart unlabelled, orders included:
#  eight attempts at reading every phrasing each left a tail. Now, while a
#  chart is open or the conversation is about one patient, a bed that is not
#  that patient is never opened on Sage's own reading: Sage asks, and "yes" or
#  "12" confirms. Named patients and the open patient's own bed are not asked.
# ══════════════════════════════════════════════════════════════════════════════

_REVIEW8_ROWS = [
    ("she was given the insulin meant for bed 12, check her sugar now", "12"),
    ("blood for bed 12 was hung on her by mistake, transfusion reaction?", "12"),
    ("her sample was labelled for bed 12, repeat her bloods", "12"),
    ("the visitor of bed 12 gave her food, aspiration risk?", "12"),
    ("she had bed 12 last admission, any old cultures?", "12"),
    ("when bed 12 is free, shift her there", "12"),
    ("hold bed 12 for her, she's back from OT at 4", "12"),
    ("book bed 12 for her after the scan", "12"),
    ("ICU has bed 12 free, can we shift her?", "12"),
    ("is bed 12 in ICU free for her?", "12"),
    ("has bed 12's MRSA spread to her?", "12"),
    ("the ECG of bed 12 was put in her file by mistake", "12"),
    ("she was in contact with bed 12, TB positive, prophylaxis for her?", "12"),
    ("she was on bed 12 last night before shifting", "12"),
    ("did she catch the flu from bed 12?", "12"),
    ("she fell at bed 12 last night, xray?", "12"),
    ("Bed 12 was her bed before transfer, old notes?", "12"),
    ("bed 12 was shifted to ICU last night, how is she doing there?", "12"),
    ("ICU called, bed 12 is free for her", "12"),
    ("Came from ICU. Bed 12, on norad, wean now?", "12"),
    ("her husband in bed 12 has covid, should she be tested?", "12"),
    ("her son in bed 12 is febrile, should she be isolated?", "12"),
    ("her sugar in bed 12 was 300 before transfer", "12"),
    ("her BP on bed 12 was fine, why low now?", "12"),
    ("the man in bed 12 has open TB, does she need prophylaxis?", "12"),
    ("transferred from ICU (bed 12) yesterday, continue meropenem?", "12"),
    ("ICU referral: bed 12, 60M, septic shock, can we accept?", "12"),
    ("ICU, bed 12, still on noradrenaline?", "12"),
    ("will review the patient in 20, what should I check first?", "20"),
    ("I'll see the patient in 14, anything urgent?", "14"),
    ("the patient in 12 noon slot for echo, prep?", "12"),
    ("is the patient in 2 ltr O2 ok?", "2"),
]


@pytest.mark.parametrize("query,bed", _REVIEW8_ROWS)
def test_with_a_chart_open_another_bed_is_never_opened_on_sages_reading(h, query, bed):
    """Attempt 8 opened that chart, unlabelled. Allowed: the question (with
    that bed among the candidates), or the open chart (labelled by the route)."""
    n = h["numw"]
    r = _ask(h, "numw", query, open_bed="7")
    _never_a_certain_wrong_chart(r, h, "numw", "7", bed)
    if r["qscope"] == "DISAMBIGUATE":
        assert n[bed]["name"] in r["context"], (query, r["context"][:200])


@pytest.mark.parametrize("order", [
    "transferred from ICU (bed 12) yesterday, start lasix 40 iv",
    "her husband in bed 12 has covid, start her on oseltamivir 75 bd",
    "she was given the insulin meant for bed 12, give her 50 ml of 25% dextrose",
    "will review the patient in 20, give lasix 40 iv stat meanwhile",
])
def test_an_order_in_those_shapes_is_never_drafted_on_another_bed(h, client, monkeypatch, order):
    n = h["numw"]
    [(_body, meta, called)] = _turns(client, monkeypatch, h["users"]["numw"], n["7"], [order])
    assert meta.get("patient_id") in (None, n["7"]["id"]), (order, meta.get("patient_id"))
    if meta.get("query_scope") == "DISAMBIGUATE":
        assert not called


def test_the_conversations_patient_counts_as_open(h):
    """No chart on screen, but the conversation is about bed 3's patient:
    another bed is confirmed first."""
    n = h["numw"]
    first = n["3"]["name"].split()[0]
    hist = _thread(f"how is {first} today?")
    r = _ask(h, "numw", "her husband in bed 12 has covid, should she be tested?", history=hist)
    _never_a_certain_wrong_chart(r, h, "numw", "3", "12")
    r = _ask(h, "numw", "how is bed 12 doing?", history=hist)
    _assert_confirms(r, n["12"])


def test_with_nothing_open_a_bed_is_confirmed_too(h):
    """Attempt 9 opened it; attempt 9's review found nouns instead of pronouns
    ("the husband in bed 12 has covid") doing the same with nothing open."""
    _assert_confirms(_ask(h, "numw", "how is bed 12 doing?"), h["numw"]["12"])


def test_the_open_patients_own_bed_is_asked_about_too(h):
    """Attempt 12: no exceptions. Attempt 11 answered the own bed directly,
    and "ER bed 12 referral" with bed 12 open was answered from bed 12."""
    _assert_confirms(_ask(h, "numw", "bed 7 potassium?", open_bed="7"), h["numw"]["7"])


@pytest.mark.parametrize("query", ["G.M.-103 lactate?", "G.M. 103 lactate?", "g.m-103 lactate?",
                                   "GM -103 lactate?", "GM - 103 lactate?"])
def test_our_code_written_with_dots_or_a_spaced_dash_is_that_bed(h, query):
    """The old code found these; attempts 7 and 8 answered from the open chart."""
    _assert_chart(_ask(h, "gm", query, open_bed="RB-102"), h["gm"]["GM-103"])


def test_sixty_beds_stored_as_icu_bed_n_are_linear(h):
    """Attempt 8's review: 4 to 7 s at 512 KB, every "icu bed" tried all 60."""
    q = ("ICU Bed 3 " * 52_000)[:512_000].lower()
    rows = _rows(ICUBEDS)
    sage._refresh_census_name_tokens(sage.get_db())
    t0 = time.perf_counter()
    sage._has_bed_reference(q)
    sage._locate_patients(rows, [], q, [])
    assert time.perf_counter() - t0 < 3.0



@pytest.mark.parametrize("reply", ["no, she is next to bed 12", "not her, the one near bed 12",
                                   "bed 12 is her neighbour, not her"])
def test_a_longer_reply_naming_a_bed_after_the_question_is_asked_again(h, reply):
    """Only a reply that is nothing but a bed ("12", "bed 12") confirms; the
    attempt 8 reviewer's "no, she is next to bed 12" opened bed 12."""
    n = h["numw"]
    r = _ask(h, "numw", reply, open_bed="7", history=_asked("how is 12?"))
    assert r["qscope"] == "DISAMBIGUATE" and r["pctx"] is None, (reply, r["qscope"])


@pytest.mark.parametrize("query,bed", [
    ("her husband in bed 12 has covid, should she be tested?", "12"),
    ("she was given the insulin meant for bed 12, check her sugar now", "12"),
    ("did he catch the flu from bed 14?", "14"),
])
def test_with_nothing_open_a_question_about_her_still_confirms_another_bed(h, query, bed):
    """No chart and no conversation: "her" is still not the patient in bed 12."""
    n = h["numw"]
    r = _ask(h, "numw", query)
    assert r["qscope"] == "DISAMBIGUATE" and r["pctx"] is None, (query, r["qscope"])
    assert n[bed]["name"] in r["context"]


@pytest.mark.parametrize("query", ["I'm going to bed 12 now, what's her potassium?",
                                   "her potassium in bed 12?"])
def test_her_with_a_bed_is_confirmed_with_or_without_a_chart(h, query):
    """"her" may be the bed's own patient or the one the clinician has in mind;
    either way one reply settles it, and no chart is opened on a guess."""
    n = h["numw"]
    _assert_confirms(_ask(h, "numw", query, open_bed="7"), n["12"])
    _assert_confirms(_ask(h, "numw", query), n["12"])


# ══════════════════════════════════════════════════════════════════════════════
#  28. THE NINTH REVIEW (attempt 9, 6 October) and attempt 10: EVERY BED IS
#      CONFIRMED (Samuel, 6 October)
#
#  Attempt 9 asked only while a chart was open; with nothing open the same
#  shapes came back with nouns ("the husband in bed 12 has covid"), the
#  conversation's patient lapsed after a turn holding a number, "not 12" was
#  read as the confirmation, and one name word skipped it. Now no bed is ever
#  opened on Sage's own reading; the open patient's own bed, a full name and
#  the confirming reply ("yes", or exactly that bed's label) are the only ways
#  through.
# ══════════════════════════════════════════════════════════════════════════════

_REVIEW9_NOTHING_OPEN = [
    ("transferred from ICU (bed 12) yesterday, continue meropenem?", "12"),
    ("ICU referral: bed 12, 60M, septic shock, can we accept?", "12"),
    ("ICU, bed 12, still on noradrenaline?", "12"),
    ("came from ICU. Bed 12, on norad, wean now?", "12"),
    ("ICU has bed 12 free, can we shift the sick one?", "12"),
    ("swapped from bed 12 yesterday, settling?", "12"),
    ("the patient in 12 noon slot for echo, prep?", "12"),
    ("insulin meant for bed 12 was given to the wrong patient, what to monitor?", "12"),
    ("the husband in bed 12 has covid, should the wife be tested?", "12"),
    ("pt's son in bed 12 is febrile, isolate pt?", "12"),
    ("the visitor of bed 12 has chickenpox, who needs VZIG?", "12"),
    ("sample of bed 12 was sent under the wrong name, what to do?", "12"),
    ("same plan as for bed 12 for the new admission?", "12"),
    ("new admission next to bed 12, MRSA screen needed?", "12"),
    ("will review the patient in 20, what should I check first?", "20"),
]


@pytest.mark.parametrize("query,bed", _REVIEW9_NOTHING_OPEN)
def test_with_nothing_open_no_bed_is_opened_on_sages_reading(h, query, bed):
    n = h["numw"]
    r = _ask(h, "numw", query)
    assert r["pctx"] is None or r["pctx"]["patient_id"] != n[bed]["id"], (query, r["qscope"])


@pytest.mark.parametrize("query,bed", _REVIEW9_NOTHING_OPEN[:6])
def test_after_a_turn_holding_a_number_another_bed_is_still_confirmed(h, query, bed):
    """B-B: the conversation's patient lapses after "lasix 40 iv bd, continue?";
    attempt 9's rule then had no referent."""
    n = h["numw"]
    first = n["3"]["name"].split()[0]
    hist = _thread(f"how is {first} today?", "lasix 40 iv bd, continue?")
    r = _ask(h, "numw", query, history=hist)
    assert r["pctx"] is None or r["pctx"]["patient_id"] != n[bed]["id"], (query, r["qscope"])


@pytest.mark.parametrize("reply", ["not 12", "not 12?", "except 12", "nahi 12", "never 12",
                                   "wrong 12", "no 12"])
def test_a_negated_number_after_the_question_is_not_the_confirmation(h, reply):
    """B-C: attempt 9 read these as the bare-bed reply and opened bed 12."""
    n = h["numw"]
    r = _ask(h, "numw", reply, open_bed="7", history=_asked("how is 12?"))
    assert r["pctx"] is None or r["pctx"]["patient_id"] not in (n["12"]["id"],), (reply, r["qscope"])
    assert r["qscope"] == "DISAMBIGUATE", (reply, r["qscope"])


@pytest.mark.parametrize("reply", ["12", "bed 12", "yes bed 12", "bed 12 please", "Bed 12.",
                                   "bed no. 12", "correct, bed 12"])
def test_a_reply_that_is_exactly_the_bed_confirms_it(h, reply):
    n = h["numw"]
    _assert_chart(_ask(h, "numw", reply, open_bed="7", history=_asked_about(n["12"], "how is 12?")),
                  n["12"])


@pytest.mark.parametrize("query,bed", [
    ("she is short of breath, her husband in bed 12 had flu, swab her?", "12"),
    ("is she happy to go home? her son in bed 14 is still febrile", "14"),
])
@pytest.mark.parametrize("open_bed", ["7", None])
def test_one_name_word_does_not_skip_the_confirmation(h, query, bed, open_bed):
    """B-D: "Elvira Short" matched on "short"; only a FULL name skips it."""
    w = h["wordnames"]
    r = _ask(h, "wordnames", query, open_bed=open_bed)
    assert r["pctx"] is None or r["pctx"]["patient_id"] != w[bed]["id"], (query, r["qscope"])


def test_a_full_name_with_the_bed_is_confirmed_too(h):
    w = h["wordnames"]
    _assert_confirms(_ask(h, "wordnames", "how is Elvira Short in bed 12?", open_bed="7"), w["12"])


def test_after_yes_a_follow_up_order_offers_the_confirmed_patient_again(h, client, monkeypatch):
    """Attempt 9 drafted "add kcl 20 mmol" on the open bed 7 after the
    clinician had confirmed bed 12; attempts 10 to 12 followed bed 12, and
    the follow survived chart visits it could not see (review 12, B-C).
    Attempt 13 asks again, offering bed 12, and "yes" drafts it there."""
    n = h["numw"]
    turns = _turns(client, monkeypatch, h["users"]["numw"], n["7"],
                   ["how is bed 12 doing?", "yes", "add kcl 20 mmol", "yes"])
    (_q2, m2, c2), (q3, m3, c3), (_q4, m4, c4) = turns[1:]
    assert c2 and m2["patient_id"] == n["12"]["id"]
    assert m3["query_scope"] == "DISAMBIGUATE" and not c3, m3.get("query_scope")
    assert n["12"]["name"] in q3 and n["7"]["name"] not in q3
    assert c4 and m4["patient_id"] == n["12"]["id"], m4.get("query_scope")


def test_after_naming_another_patient_an_unnamed_follow_up_asks(h, client, monkeypatch):
    n = h["numw"]
    first = n["12"]["name"].split()[0]
    turns = _turns(client, monkeypatch, h["users"]["numw"], n["7"],
                   [f"how is {first} doing?", "add kcl 20 mmol", "this patient"])
    (_q1, m1, c1), (q2, m2, c2), (_q3, m3, c3) = turns
    assert c1 and m1["patient_id"] == n["12"]["id"]
    assert m2["query_scope"] == "DISAMBIGUATE" and not c2 and n["12"]["name"] in q2
    assert c3 and m3["patient_id"] == n["7"]["id"]


def test_the_walk_never_anchors_on_a_bed_it_only_asked_about(h):
    """Attempt 9: thread about bed 3, a question about bed 12, then "this
    patient" with nothing open bound the conversation to bed 12."""
    n = h["numw"]
    first = n["3"]["name"].split()[0]
    hist = _thread(f"how is {first} today?") + [
        {"role": "user", "content": "how is bed 12 doing?"},
        {"role": "assistant", "content": "Which patient do you mean?\n\n- Bed 12: x, ward y"},
        {"role": "user", "content": "this patient"},
        {"role": "assistant", "content": "NOACT|Noted."},
    ]
    rows = _rows(NUMW)
    assert sage._anchor_patient_from_history(rows, [], hist) != n["12"]["id"]


def test_y_alone_confirms(h, client, monkeypatch):
    n = h["numw"]
    (_q1, m1, _c1), (_q2, m2, c2) = _turns(client, monkeypatch, h["users"]["numw"], n["7"],
                                          ["how is bed 12 doing?", "y"])
    assert c2 and m2["patient_id"] == n["12"]["id"]


def test_a_long_reply_after_the_question_is_linear(h):
    """Attempt 9's bare-bed reply pattern: 8.8 s for 40,000 spaces."""
    t0 = time.perf_counter()
    _ask(h, "numw", "12" + " " * 40_000 + "x", open_bed="7", history=_asked("how is 12?"))
    _ask(h, "numw", "12" + " " * 40_000 + "x", history=_asked("how is 12?") + [
        {"role": "user", "content": "12" + " " * 40_000 + "x"},
        {"role": "assistant", "content": "NOACT|Noted."}])
    assert time.perf_counter() - t0 < 2.0



@pytest.mark.parametrize("unit", ["bed 7 ", "12 ", "GM-103 ", "the patient in 12, "])
def test_a_question_with_more_than_a_thousand_beds_is_asked_about(h, unit):
    """A paste, not a question: Sage stops reading at 1,000 bed references and
    asks which patient, never a chart. Reading 87,000 cost 3 s at 512 KB."""
    scen, open_bed = ("gm", "RB-102") if unit.startswith("GM") else ("numw", "7")
    q = unit * 1_200
    t0 = time.perf_counter()
    r = _ask(h, scen, q, open_bed=open_bed)
    assert r["qscope"] == "DISAMBIGUATE" and r["pctx"] is None, r["qscope"]
    assert time.perf_counter() - t0 < 2.0


def test_a_ward_handover_naming_every_bed_is_still_read(h):
    """Well under the cap: "beds 1, 2, 3, 7, 12, 14, 20, 21, 30" lists them all."""
    n = h["numw"]
    r = _ask(h, "numw", "handover for beds 1, 2, 3, 7, 12, 14, 20, 21 and 30 please", open_bed="7")
    assert r["pctx"] is None or r["pctx"]["patient_id"] == n["7"]["id"]


# ══════════════════════════════════════════════════════════════════════════════
#  29. THE TENTH REVIEW (attempt 10, 6 October) and attempt 11
#
#  The rule held wherever it was applied; two holes in applying it blocked:
#  a bed Sage was unsure of plus ONE word of a name skipped the question, and a
#  choice made in reply survived the clinician opening another chart.
# ══════════════════════════════════════════════════════════════════════════════

_RELATIVE_ROWS = [
    ("her husband Ravindran in bed 18 has covid, should she be tested?", "18"),
    ("Ravindran in bed 18 has open TB, does she need INH prophylaxis?", "18"),
    ("the insulin meant for Ravindran in bed 18 was given to her by mistake, check her sugar", "18"),
    ("Mr Ravindran in bed 18 is her husband, does she need a swab?", "18"),
    ("her husband Ravindran in 18 has flu, swab her?", "18"),
    ("her son Selvaraj in bed 19 is febrile, isolate her?", "19"),
    ("Selvaraj K in 19 is her brother, any risk to her?", "19"),
    ("Murugesan in bed 20 has TB, prophylaxis for her?", "20"),
    ("her father Thangavel in 21 had chickenpox, VZIG for her?", "21"),
]


@pytest.mark.parametrize("query,bed", _RELATIVE_ROWS)
@pytest.mark.parametrize("open_bed", ["7", None])
def test_one_name_word_with_a_bed_never_opens_that_chart(h, query, bed, open_bed):
    """B1: attempt 10 opened bed 18 for "her husband Ravindran in bed 18"."""
    r = _ask(h, "relnames", query, open_bed=open_bed)
    rel = h["relnames"]
    assert r["pctx"] is None or r["pctx"]["patient_id"] != rel[bed]["id"], (query, r["qscope"])


@pytest.mark.parametrize("query", [
    "she is short of breath, her husband in 12 had flu, swab her?",
    "is she happy to go home? her son in 14 is still febrile",
])
@pytest.mark.parametrize("open_bed", ["7", None])
def test_a_name_word_with_an_unsure_bed_is_still_asked(h, query, open_bed):
    w = h["wordnames"]
    r = _ask(h, "wordnames", query, open_bed=open_bed)
    assert r["pctx"] is None or r["pctx"]["patient_id"] not in (w["12"]["id"], w["14"]["id"])


@pytest.mark.parametrize("query,bed", [
    ("how is Ravindran Kuppusamy in bed 18?", "18"),
    ("Selvaraj K in bed 19, potassium?", "19"),
    ("the husband Ravindran Kuppusamy in bed 18 has covid, should the wife be tested?", "18"),
    ("start this patient on the same antibiotics as Ravindran Kuppusamy in bed 18", "18"),
    ("uske pati Ravindran Kuppusamy bed 18 mein hai, flu hai, swab karein?", "18"),
    ("pt's husband Ravindran Kuppusamy bed 18 has flu, swab pt?", "18"),
])
def test_a_full_name_with_its_bed_is_asked_too(h, query, bed):
    """Attempt 12: a full name with its bed is confirmed like any bed
    (attempt 11's exception opened "the husband Ravindran Kuppusamy in bed
    18 ... should the wife be tested?")."""
    _assert_confirms(_ask(h, "relnames", query, open_bed="7"), h["relnames"][bed])


@pytest.mark.parametrize("query,bed", [("how is Murugesan in bed 20?", "20"),
                                       ("Selvaraj in bed 19, potassium?", "19")])
def test_one_typed_word_is_not_a_full_name(h, query, bed):
    """"Murugesan" alone, or "Selvaraj" for "Selvaraj K": attempt 10 treated
    these as full names and skipped the question."""
    _assert_confirms(_ask(h, "relnames", query, open_bed="7"), h["relnames"][bed])


def test_an_order_with_a_name_word_and_a_bed_is_never_drafted_there(h, client, monkeypatch):
    rel = h["relnames"]
    [(_b, meta, called)] = _turns(client, monkeypatch, h["users"]["relnames"], rel["7"],
                                  ["her husband Ravindran in bed 18 has flu, start lasix 40 iv for her"])
    assert meta.get("patient_id") != rel["18"]["id"]
    assert meta.get("query_scope") == "DISAMBIGUATE" and not called


def _turns_switching(client, monkeypatch, user, steps):
    """Like _turns, but each step names the chart open for it (or None)."""
    from auth import create_token
    calls = []

    def _model(query, context, history, scope, **kw):
        calls.append(context)

        async def _gen():
            route_info = kw.get("route_info")
            if route_info is not None:
                route_info.update({"provider": "stub", "model": "stub"})
            yield ("<action_intent>None</action_intent>"
                   "<concise_answer>Noted.</concise_answer>")
        return _gen()

    monkeypatch.setattr("routes.sage_routes.query_ollama", _model)
    headers = {"Authorization": f"Bearer {create_token(user['id'], 'doctor')}"}
    conv = f"switch-{uuid.uuid4().hex[:8]}"
    out = []
    for q, open_p in steps:
        before = len(calls)
        body = {"query": q, "conversation_id": conv}
        if open_p is not None:
            body["patient_id"] = open_p["id"]
        events = _sse(client.post("/api/sage/query", headers=headers, json=body))
        meta = next((e["metadata"] for e in events if "metadata" in e), {})
        out.append((meta, len(calls) > before))
    return out


def test_a_choice_made_in_reply_does_not_follow_into_another_chart(h, client, monkeypatch):
    """B2: confirm bed 12 with bed 7 open, then open bed 3's chart: an unnamed
    follow-up asks, never bed 12 (attempt 10 drafted orders there)."""
    n = h["numw"]
    out = _turns_switching(client, monkeypatch, h["users"]["numw"], [
        ("how is bed 12 doing?", n["7"]), ("yes", n["7"]),
        ("and her potassium?", n["7"]),
        ("give her lasix 40 iv", n["3"]),
    ])
    assert out[1][0].get("patient_id") == n["12"]["id"]
    # Same chart: offered again, not followed (attempt 13).
    assert out[2][0].get("query_scope") == "DISAMBIGUATE" and not out[2][1], out[2][0]
    assert out[3][0].get("query_scope") == "DISAMBIGUATE" and not out[3][1], out[3][0]


def test_a_choice_made_with_nothing_open_asks_once_a_chart_is_opened(h, client, monkeypatch):
    n = h["numw"]
    out = _turns_switching(client, monkeypatch, h["users"]["numw"], [
        ("how is bed 12 doing?", None), ("yes", None), ("what is her potassium?", n["3"]),
    ])
    assert out[1][0].get("patient_id") == n["12"]["id"]
    assert out[2][0].get("query_scope") == "DISAMBIGUATE" and not out[2][1]


@pytest.mark.parametrize("query", ["how is 7?", "7 potassium?", "give lasix 40 iv to 7",
                                   "bed 7 2 days post op, drain out?",
                                   "transferred from ICU bed 7 yesterday, still on norad?"])
def test_the_open_patients_own_bed_is_asked_like_any_bed(h, query):
    r = _ask(h, "numw", query, open_bed="7")
    assert r["qscope"] == "DISAMBIGUATE" and r["pctx"] is None, (query, r["qscope"])
    assert h["numw"]["7"]["name"] in r["context"]


def test_a_repeated_question_keeps_its_candidate_so_yes_settles_it(h, client, monkeypatch):
    n = h["numw"]
    out = _turns_switching(client, monkeypatch, h["users"]["numw"], [
        ("how is bed 12 doing?", n["7"]), ("potassium?", n["7"]), ("yes", n["7"]),
    ])
    assert out[1][0].get("query_scope") == "DISAMBIGUATE"
    assert out[2][0].get("patient_id") == n["12"]["id"], out[2][0]


def test_a_number_reply_counts_only_for_the_beds_offered(h, client, monkeypatch):
    """An option number ("2") after a question offering beds 12 and 14 is
    not bed 2."""
    n = h["numw"]
    out = _turns_switching(client, monkeypatch, h["users"]["numw"], [
        ("compare bed 12 and bed 14", n["7"]), ("2", n["7"]), ("14", n["7"]), ("yes", n["7"]),
    ])
    # "2" is not one of the beds offered: a new question about bed 2, never its chart
    assert out[1][0].get("query_scope") == "DISAMBIGUATE" and not out[1][1]
    assert out[2][0].get("query_scope") == "DISAMBIGUATE" and not out[2][1]
    assert out[3][0].get("patient_id") == n["14"]["id"]


@pytest.mark.parametrize("query", ["bed GM-103 was in ICU-03 before, any old notes?",
                                   "how is GM-103 after ICU-03?",
                                   "bed GM-103 is going to ICU-03 tonight, handover points?"])
def test_another_departments_code_as_a_place_beside_our_bed_is_not_refused(h, query):
    assert _ask(h, "gm", query, open_bed="RB-102")["qscope"] != "OUT_OF_SCOPE", query


def test_the_paste_cap_still_refuses_another_departments_patient(h):
    """Attempt 10: the 1,000-reference cap skipped the out-of-department
    refusal and its audit row."""
    other = h["zero"]["ICU-03"]["name"]
    r = _ask(h, "numw", "bed 7 " * 1_001 + f"how is {other}?", open_bed="7")
    assert r["qscope"] == "OUT_OF_SCOPE", r["qscope"]


# ══════════════════════════════════════════════════════════════════════════════
#  30. THE ELEVENTH REVIEW (attempt 11, 6 October) and attempt 12: NO EXCEPTIONS
#
#  Attempts 10 and 11 held wherever the rule applied; every failure came
#  through an exception (a name word, a full name, the open patient's own bed,
#  a reply remembered across charts, a reply matched against the whole ward).
#  Now a chart opens on a bed only through a reply to the question, made on
#  the chart the clinician is still on.
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("query", [
    "ER bed 12 referral: 60M, septic shock, can we accept?",
    "ICU bed 12 called, can they send their patient to us tonight?",
    "the patient in ICU bed 12 is crashing, should we accept him?",
    "triage bed 12, 30F, dengue, platelets 20k, admit?",
])
def test_an_unsure_own_bed_is_never_answered_from_the_open_chart(h, query):
    """F2: attempt 11 answered these from bed 12's chart with bed 12 open."""
    r = _ask(h, "numw", query, open_bed="12")
    assert r["pctx"] is None, (query, r["qscope"])


def test_yes_to_the_own_bed_opens_it(h, client, monkeypatch):
    n = h["numw"]
    out = _turns_switching(client, monkeypatch, h["users"]["numw"], [
        ("bed 7 potassium?", n["7"]), ("yes", n["7"])])
    assert out[0][0].get("query_scope") == "DISAMBIGUATE" and not out[0][1]
    assert out[1][1] and out[1][0].get("patient_id") == n["7"]["id"]


@pytest.mark.parametrize("between", ["who are the sickest patients on the ward?",
                                     "give me a handover summary of the ward",
                                     "how many patients are admitted?"])
def test_a_visit_to_another_chart_ends_the_reply_choice(h, client, monkeypatch, between):
    """B3: confirm bed 13 on bed 7's chart, ask a census question on bed 9's
    chart, come back to bed 7: the order is asked about, never bed 13's."""
    n = h["numw"]
    out = _turns_switching(client, monkeypatch, h["users"]["numw"], [
        ("how is bed 12 doing?", n["7"]), ("yes", n["7"]),
        (between, n["3"]),
        ("give her lasix 40 iv", n["7"]),
    ])
    assert out[1][0].get("patient_id") == n["12"]["id"]
    assert out[3][0].get("patient_id") != n["12"]["id"], out[3][0]
    assert out[3][0].get("query_scope") == "DISAMBIGUATE" and not out[3][1]


def test_a_reply_never_opens_a_newcomer_in_the_offered_bed(h, client, monkeypatch):
    """B4: the question offered bed 12's patient; she moved to bed 31 and
    someone new took bed 12. "12" must not open the newcomer's chart."""
    from database import db_session
    n = h["numw"]
    offered = _patient(NUMW, "33", name="Qzvimbert Lomandrix")
    out1 = _turns_switching(client, monkeypatch, h["users"]["numw"], [("how is bed 33 doing?", n["7"])])
    assert out1[0][0].get("query_scope") == "DISAMBIGUATE"
    with db_session() as db:
        db.execute("UPDATE patients SET bed = '34' WHERE id = ?", (offered["id"],))
        db.execute("UPDATE admissions SET bed = '34' WHERE patient_id = ?", (offered["id"],))
    newcomer = _patient(NUMW, "33", name="Xorvadine Quellbrook")
    from auth import create_token
    # continue the SAME conversation: reuse the helper with the saved question
    hist = [{"role": "user", "content": "how is bed 33 doing?"},
            {"role": "assistant",
             "content": f"Which patient do you mean?\n\n- Bed 33: {offered['name']}, ward {NUMW}\n\n"
                        'Reply "yes" for bed 33, or with another bed or name.'}]
    r = _ask(h, "numw", "33", open_bed="7", history=hist)
    assert r["pctx"] is None or r["pctx"]["patient_id"] != newcomer["id"], r["qscope"]
    with db_session() as db:
        for pid in (offered["id"], newcomer["id"]):
            db.execute("UPDATE patients SET status = 'discharged', bed = NULL WHERE id = ?", (pid,))
            db.execute("UPDATE admissions SET status = 'discharged' WHERE patient_id = ?", (pid,))


def test_another_bed_in_reply_starts_a_new_question(h, client, monkeypatch):
    """A bare "21" to a question about bed 20 asks about bed 21; "yes" opens it."""
    n = h["numw"]
    out = _turns_switching(client, monkeypatch, h["users"]["numw"], [
        ("how is bed 20 doing?", n["7"]), ("21", n["7"]), ("yes", n["7"])])
    assert out[1][0].get("query_scope") == "DISAMBIGUATE" and not out[1][1]
    assert out[2][1] and out[2][0].get("patient_id") == n["21"]["id"]


@pytest.mark.parametrize("order", [
    "the husband Ravindran Kuppusamy in bed 18 has flu, start oseltamivir 75 bd for the wife",
    "give this patient the same dose of lasix as Ravindran Kuppusamy in bed 18",
])
def test_an_order_naming_a_full_name_with_its_bed_is_asked_first(h, client, monkeypatch, order):
    rel = h["relnames"]
    [(_b, meta, called)] = _turns(client, monkeypatch, h["users"]["relnames"], rel["7"], [order])
    assert meta.get("patient_id") != rel["18"]["id"] and not called



# ══════════════════════════════════════════════════════════════════════════════
#  ATTEMPT 13: nothing is rebuilt from the conversation (review 12)
# ══════════════════════════════════════════════════════════════════════════════

def _steps(client, monkeypatch, user, steps):
    """One saved conversation. Each step is (query, open patient or None), or
    a callable run between turns (a bed move). Returns [(body, meta,
    model_called)] for the queries."""
    from auth import create_token
    calls = []

    def _model(query, context, history, scope, **kw):
        calls.append(context)

        async def _gen():
            route_info = kw.get("route_info")
            if route_info is not None:
                route_info.update({"provider": "stub", "model": "stub"})
            yield "NOACT|stub answer."
        return _gen()

    monkeypatch.setattr("routes.sage_routes.query_ollama", _model)
    headers = {"Authorization": f"Bearer {create_token(user['id'], 'doctor')}"}
    conv = f"a13-{uuid.uuid4().hex[:10]}"
    out = []
    for step in steps:
        if callable(step):
            step()
            continue
        q, open_p = step
        before = len(calls)
        body = {"query": q, "conversation_id": conv}
        if open_p is not None:
            body["patient_id"] = open_p["id"]
        r = client.post("/api/sage/query", headers=headers, json=body)
        assert r.status_code == 200, r.text
        events = _sse(r)
        content = "".join(e.get("content", "") for e in events)
        meta = next((e["metadata"] for e in events if "metadata" in e), {})
        out.append((content, meta, len(calls) > before))
    return out


def _move(patient, bed):
    def _do():
        with db_session() as db:
            db.execute("UPDATE patients SET bed = ? WHERE id = ?", (bed, patient["id"]))
            db.execute("UPDATE admissions SET bed = ? WHERE patient_id = ?", (bed, patient["id"]))
    return _do


def _retire(*patients):
    with db_session() as db:
        for p in patients:
            db.execute("UPDATE patients SET status = 'discharged', bed = NULL WHERE id = ?", (p["id"],))
            db.execute("UPDATE admissions SET status = 'discharged' WHERE patient_id = ?", (p["id"],))


@pytest.mark.parametrize("reply", ["35", "bed 35"])
@pytest.mark.parametrize("chart", [True, False])
def test_a_re_ask_that_lost_its_patient_never_opens_the_newcomer(h, client, monkeypatch, reply, chart):
    """Review 12, B-A: the question offered bed 35's patient; she moved to bed
    36 and a newcomer took bed 35; "yes" got a re-ask listing nobody, and
    "35" opened the newcomer's chart unlabelled. Now "35" asks about the
    newcomer by name, and only "yes" to that opens her chart."""
    n = h["numw"]
    open_p = n["7"] if chart else None
    offered = _patient(NUMW, "35", name="Pervokinde Orrvandelix")
    holder = {}

    def _admit():
        holder["new"] = _patient(NUMW, "35", name="Thraxquimel Luxquerand")
    try:
        out = _steps(client, monkeypatch, h["users"]["numw"], [
            ("how is bed 35?", open_p), _move(offered, "36"), _admit,
            ("yes", open_p), (reply, open_p), ("yes", open_p)])
        (b1, m1, c1), (b2, m2, c2), (b3, m3, c3), (_b4, m4, c4) = out
        assert m1["query_scope"] == "DISAMBIGUATE" and offered["name"] in b1 and not c1
        assert m2["query_scope"] == "DISAMBIGUATE" and not c2, m2.get("query_scope")
        assert m3["query_scope"] == "DISAMBIGUATE" and not c3, (reply, m3.get("query_scope"))
        assert holder["new"]["name"] in b3 and offered["name"] not in b3, b3[:300]
        assert m3.get("patient_id") is None
        assert c4 and m4.get("patient_id") == holder["new"]["id"], m4.get("query_scope")
    finally:
        _retire(offered, *holder.values())


def test_an_order_after_a_lost_offer_is_not_drafted_on_the_newcomer(h, client, monkeypatch):
    n = h["numw"]
    offered = _patient(NUMW, "37", name="Qweldrisant Vorpaxmund")
    holder = {}

    def _admit():
        holder["new"] = _patient(NUMW, "37", name="Zimbrolette Akkavurin")
    try:
        out = _steps(client, monkeypatch, h["users"]["numw"], [
            ("give lasix 40 iv to bed 37", n["7"]), _move(offered, "38"), _admit,
            ("go ahead", n["7"]), ("37", n["7"])])
        for body, meta, called in out:
            assert meta["query_scope"] == "DISAMBIGUATE" and not called, meta.get("query_scope")
            assert meta.get("patient_id") is None
        assert holder["new"]["name"] in out[2][0]
    finally:
        _retire(offered, *holder.values())


@pytest.mark.parametrize("between", [
    [("what is the antidote for paracetamol overdose?", "3")],
    [("who is deteriorating on the ward?", "3")],
    [],
])
def test_a_choice_made_in_reply_is_offered_again_after_any_turns(h, client, monkeypatch, between):
    """Review 12, B-C: after "yes" to bed 13 with bed 7 open (bed 12 here), a turn on
    bed 9's chart (bed 3 here) that Sage never saved (a safety refusal, a retrieval failure)
    left the follow intact, and "give her furosemide iv stat" back on bed 7
    was drafted on bed 13. Nothing is followed now: the order asks."""
    n = h["numw"]
    steps = [("how is bed 12?", n["7"]), ("yes", n["7"])]
    steps += [(q, n[b]) for q, b in between]
    steps += [("give her furosemide iv stat", n["7"])]
    out = _steps(client, monkeypatch, h["users"]["numw"], steps)
    assert out[1][2] and out[1][1].get("patient_id") == n["12"]["id"]
    body, meta, called = out[-1]
    assert meta["query_scope"] == "DISAMBIGUATE" and not called, meta.get("query_scope")
    assert n["12"]["name"] in body and meta.get("patient_id") is None


def test_a_choice_made_with_a_chart_open_is_offered_again_once_it_closes(h, client, monkeypatch):
    """Review 12, variant H: with no chart open the follow ran whatever chart
    had been open when the choice was made."""
    n = h["numw"]
    out = _steps(client, monkeypatch, h["users"]["numw"], [
        ("how is bed 12?", n["7"]), ("yes", n["7"]),
        ("who is deteriorating on the ward?", n["3"]),
        ("give her furosemide iv stat", None), ("yes", None)])
    body, meta, called = out[3]
    assert meta["query_scope"] == "DISAMBIGUATE" and not called, meta.get("query_scope")
    assert n["12"]["name"] in body and "this patient" not in body
    assert out[4][2] and out[4][1].get("patient_id") == n["12"]["id"]


@pytest.mark.parametrize("first", ["what is the length of stay of the patient in 12 so far?",
                                   "length of stay of cot 12?",
                                   "how many in the ward have fever apart from cot 12?"])
@pytest.mark.parametrize("chart", [True, False])
def test_a_bed_in_an_aggregate_question_is_never_carried_forward(h, client, monkeypatch, first, chart):
    """Review 12, D: the aggregate lane answers these before the locator, and
    with nothing open "give her lasix 40 iv" was then drafted on bed 12."""
    n = h["numw"]
    open_p = n["7"] if chart else None
    out = _steps(client, monkeypatch, h["users"]["numw"], [
        (first, open_p), ("give her lasix 40 iv", open_p)])
    body, meta, called = out[1]
    assert meta.get("patient_id") != n["12"]["id"], (first, meta.get("query_scope"))
    if meta.get("query_scope") == "DISAMBIGUATE":
        assert not called


def test_the_history_endpoint_carries_no_open_chart_field(h, client, monkeypatch):
    """Attempts 11 and 12 saved the open chart with every turn for the follow
    chain. Nothing reads it now, so it is not written."""
    from services.sage_service import get_conversation_history
    n = h["numw"]
    conv = f"a13h-{uuid.uuid4().hex[:8]}"
    from auth import create_token
    monkeypatch.setattr("routes.sage_routes.query_ollama",
                        lambda *a, **k: (lambda: (x for x in ()))())
    headers = {"Authorization": f"Bearer {create_token(h['users']['numw']['id'], 'doctor')}"}
    client.post("/api/sage/query", headers=headers,
                json={"query": "how is bed 13?", "patient_id": n["7"]["id"], "conversation_id": conv})
    rows = get_conversation_history(h["users"]["numw"]["id"], conversation_id=conv)
    assert rows and all("open_patient_id" not in r for r in rows)


# ══════════════════════════════════════════════════════════════════════════════
#  ATTEMPT 13b: the reply decides WHO, the earlier message decides WHAT (S1)
# ══════════════════════════════════════════════════════════════════════════════

_NARRATED = "NOACT|Done. Furosemide 40 mg IV has been given and the order is placed."


def _steps_told(client, monkeypatch, user, steps, reply=_NARRATED):
    """One saved conversation with a model that narrates a completed order.
    Returns [(screen text, meta, the query the model was given or None)]."""
    from auth import create_token
    asked = []

    def _model(query, context, history, scope, **kw):
        asked.append(query)

        async def _gen():
            route_info = kw.get("route_info")
            if route_info is not None:
                route_info.update({"provider": "stub", "model": "stub"})
            yield reply
        return _gen()

    monkeypatch.setattr("routes.sage_routes.query_ollama", _model)
    headers = {"Authorization": f"Bearer {create_token(user['id'], 'doctor')}"}
    conv = f"a13b-{uuid.uuid4().hex[:10]}"
    out = []
    for q, open_p in steps:
        before = len(asked)
        body = {"query": q, "conversation_id": conv}
        if open_p is not None:
            body["patient_id"] = open_p["id"]
        r = client.post("/api/sage/query", headers=headers, json=body)
        assert r.status_code == 200, r.text
        events = _sse(r)
        content = "".join(e.get("content", "") for e in events)
        meta = next((e["metadata"] for e in events if "metadata" in e), {})
        out.append((content, meta, asked[-1] if len(asked) > before else None))
    return out


def _assert_gated(turn, patient, what):
    """The order was gated: the narration never reached the screen, an action
    state was set for this patient, and the model was given the order."""
    content, meta, model_q = turn
    assert "has been given" not in content and "order is placed" not in content, content[:300]
    assert meta.get("action_state"), (meta.get("query_scope"), content[:200])
    assert meta.get("patient_id") == patient["id"], meta.get("patient_id")
    assert model_q is not None and what in model_q, model_q


@pytest.mark.parametrize("reply", ["yes", "12", "bed 12", "yes, bed 12 please", "Yes.", "go ahead"])
@pytest.mark.parametrize("chart", [True, False])
def test_an_order_settled_by_a_reply_is_gated(h, client, monkeypatch, reply, chart):
    """Review 13, S1: "give her lasix 40 iv", the which-patient question, then
    "yes": the gate read only "yes" and the model's "the order is placed"
    reached the screen with nothing drafted. The certain case (attempt 13d):
    one patient offered, the reply exactly yes or exactly that bed."""
    n = h["numw"]
    open_p = n["7"] if chart else None
    out = _steps_told(client, monkeypatch, h["users"]["numw"], [
        ("give lasix 40 iv to bed 12", open_p), (reply, open_p)])
    assert out[0][1]["query_scope"] == "DISAMBIGUATE" and out[0][2] is None
    _assert_gated(out[1], n["12"], "give lasix 40 iv")


def test_an_unnamed_follow_up_order_settled_by_yes_is_gated(h, client, monkeypatch):
    n = h["numw"]
    out = _steps_told(client, monkeypatch, h["users"]["numw"], [
        ("how is bed 12?", n["7"]), ("yes", n["7"]),
        ("give her lasix 40 iv", n["7"]), ("yes", n["7"])], reply="NOACT|Stable.")
    assert out[2][1]["query_scope"] == "DISAMBIGUATE"
    assert out[1][2] == "how is bed 12?"
    content, meta, model_q = out[3]
    assert meta.get("action_state") and meta.get("patient_id") == n["12"]["id"]
    assert model_q == "give her lasix 40 iv", model_q


@pytest.mark.parametrize("reply", ["this patient", "this patient please", "yes, this patient"])
def test_this_patient_in_reply_to_an_order_question_is_gated_on_the_open_chart(h, client, monkeypatch, reply):
    """The question invites exactly these words for the open chart, so they are
    a certain reply (attempt 13d) when the order names nobody else: here
    "give her lasix 40 iv" was offered again because the conversation was
    about bed 12. "this patient, stat" is not a certain reply."""
    n = h["numw"]
    first = n["12"]["name"].split()[0]
    out = _steps_told(client, monkeypatch, h["users"]["numw"], [
        (f"how is {first} doing?", n["7"]), ("give her lasix 40 iv", n["7"]), (reply, n["7"])],
        reply="NOACT|Stable.")
    assert out[1][1]["query_scope"] == "DISAMBIGUATE"
    _assert_gated(out[2], n["7"], "give her lasix 40 iv")


@pytest.mark.parametrize("order", ["give lasix 40 iv to bed 12",
                                   "give lasix 40 iv to bed 12 and kcl 20 mmol iv to bed 14",
                                   "give lasix 40 iv to bed 12 and kcl 20 mmol iv to this patient"])
def test_this_patient_never_takes_an_order_that_names_another_bed(h, client, monkeypatch, order):
    """Review 13d, G-A: "this patient" carried a message naming other beds to
    the open chart, and the lasix meant for bed 12 was drafted on bed 7."""
    n = h["numw"]
    out = _steps_told(client, monkeypatch, h["users"]["numw"], [(order, n["7"]), ("this patient", n["7"])])
    assert out[0][1]["query_scope"] == "DISAMBIGUATE"
    _assert_not_carried(out[1], n["7"])


def test_a_read_after_an_order_question_is_answered_without_the_order(h, client, monkeypatch):
    """"give lasix 40 iv to bed 12", the question, "potassium?" (asked again),
    "yes": the read is answered and the order is in front of nobody
    (review 13c drafted it with the read)."""
    n = h["numw"]
    out = _steps_seen(client, monkeypatch, h["users"]["numw"], [
        ("give lasix 40 iv to bed 12", n["7"]), ("potassium?", n["7"]), ("yes", n["7"])],
        reply=_acting)
    assert out[1][1]["query_scope"] == "DISAMBIGUATE"
    content, meta, model_q, model_h = out[2]
    assert model_q == "potassium?", model_q
    assert _drafted_for(meta) is None and not meta.get("action_state")
    assert not any("lasix" in (m.get("content") or "") for m in model_h)


def test_a_new_message_after_the_question_keeps_its_own_words(h, client, monkeypatch):
    """A reply with words of its own is a new message: the earlier order is
    not carried onto whoever it names (that would draft it on the wrong
    patient), and it is not falsely blocked as an order."""
    n = h["numw"]
    p = _patient(NUMW, "29", name="Quorvantine Elspethwyck")
    try:
        out = _steps_told(client, monkeypatch, h["users"]["numw"], [
            ("give lasix 40 iv to bed 12", n["7"]), ("how is Quorvantine Elspethwyck doing?", n["7"])],
            reply="NOACT|Stable overnight.")
        content, meta, model_q = out[1]
        assert model_q == "how is Quorvantine Elspethwyck doing?", (meta.get("query_scope"), content[:300])
        assert not meta.get("action_state"), meta.get("action_state")
        assert meta.get("patient_id") == p["id"]
    finally:
        _retire(p)


def test_a_read_question_settled_by_yes_is_asked_of_the_model_in_its_own_words(h, client, monkeypatch):
    n = h["numw"]
    out = _steps_told(client, monkeypatch, h["users"]["numw"], [
        ("how is bed 12 doing?", n["7"]), ("yes", n["7"])], reply="NOACT|Stable overnight.")
    content, meta, model_q = out[1]
    assert model_q == "how is bed 12 doing?"
    assert not meta.get("action_state")
    assert "Stable overnight." in content


def test_guidelines_are_searched_with_the_earlier_words(h, monkeypatch):
    import services.sage_service as svc
    seen = []
    real = svc._get_guideline_context

    def _spy(query, *a, **k):
        seen.append(query)
        return real(query, *a, **k)
    monkeypatch.setattr(svc, "_get_guideline_context", _spy)
    n = h["numw"]
    r = _ask(h, "numw", "yes", open_bed="7",
             history=_asked_about(n["12"], "is bed 12 fit for discharge on warfarin?"))
    assert r["pctx"] is not None and r["pctx"]["patient_id"] == n["12"]["id"]
    assert seen and seen[-1] == "is bed 12 fit for discharge on warfarin?", seen


@pytest.mark.parametrize("text,ok", [
    ("yes", True), ("Yes please.", True), ("y", True), ("go ahead", True), ("12", True),
    ("bed 12", True), ("bed no. 12", True), ("this patient", True), ("the lady in bed 12", True),
    ("FIRST", True), ("FIRST LAST", True), ("yes FIRST in 12", True), ("it's FIRST", True),
    ("14", False), ("not 12", False), ("how is she?", False), ("yes, and her potassium?", False),
    ("give lasix", False), ("FIRST potassium?", False), ("", False),
])
def test_a_reply_is_only_who(h, text, ok):
    from services.sage_service import _reply_is_only_who
    n = h["numw"]
    first, last = n["12"]["name"].split()
    row = {"name": n["12"]["name"], "bed": "12", "ward": n["12"]["ward"]}
    t = text.replace("FIRST", first).replace("LAST", last)
    assert _reply_is_only_who(t, row) is ok, t


def test_the_exchange_walks_back_through_re_asks():
    from services.sage_service import _read_what, _question_exchange, WHICH_PATIENT_QUESTION
    q = WHICH_PATIENT_QUESTION + "\\n\\nReply with the bed or the patient's name."
    hist = [{"role": "user", "content": "how is bed 3?"}, {"role": "assistant", "content": "NOACT|Fine."},
            {"role": "user", "content": "give lasix 40 iv to bed 37"}, {"role": "assistant", "content": q},
            {"role": "user", "content": "go ahead"}, {"role": "assistant", "content": q},
            {"role": "user", "content": "37"}, {"role": "assistant", "content": q}]
    ex = _question_exchange(hist)
    assert ex == ["37", "go ahead", "give lasix 40 iv to bed 37"]
    assert _read_what(ex) == ""          # an order never rides along with a read
    assert _question_exchange(hist[:2]) == []


# ══════════════════════════════════════════════════════════════════════════════
#  ATTEMPT 13c: the model only ever sees the settled request (review 13b)
# ══════════════════════════════════════════════════════════════════════════════

_DECLARES = 'ACT|{"action_type": "add_medication", "medication": "furosemide", "dose": "40 mg iv"} Drafting.'


def _acting(query, history):
    """A model that declares the furosemide order only when the order is in
    front of it (its message or its history), and narrates nothing else. A
    stub that declares whatever it is given tests the stub, not the route."""
    seen = " ".join([query or ""] + [(m.get("content") or "") for m in (history or [])]).lower()
    return _DECLARES if ("lasix" in seen or "furosemide" in seen) else "NOACT|Noted."


def _steps_seen(client, monkeypatch, user, steps, reply=_NARRATED):
    """Like _steps_told, and also what the model was shown: [(screen text,
    meta, model query or None, model history or None)]."""
    from auth import create_token
    seen = []

    def _model(query, context, history, scope, **kw):
        seen.append((query, [dict(m) for m in (history or [])]))
        r = reply(query, history) if callable(reply) else reply

        async def _gen():
            route_info = kw.get("route_info")
            if route_info is not None:
                route_info.update({"provider": "stub", "model": "stub"})
            yield r
        return _gen()

    monkeypatch.setattr("routes.sage_routes.query_ollama", _model)
    headers = {"Authorization": f"Bearer {create_token(user['id'], 'doctor')}"}
    conv = f"a13c-{uuid.uuid4().hex[:10]}"
    out = []
    for q, open_p in steps:
        before = len(seen)
        body = {"query": q, "conversation_id": conv}
        if open_p is not None:
            body["patient_id"] = open_p["id"]
        r = client.post("/api/sage/query", headers=headers, json=body)
        assert r.status_code == 200, r.text
        events = _sse(r)
        content = "".join(e.get("content", "") for e in events)
        meta = next((e["metadata"] for e in events if "metadata" in e), {})
        got = seen[-1] if len(seen) > before else (None, None)
        out.append((content, meta, got[0], got[1]))
    return out


def _drafted_for(meta):
    """The patient a model-declared order landed on, drafted or not. The test
    DB has no active furosemide, so a declared order ends as
    "not_in_helix_database"; checking only "request_drafted" made every
    "nothing drafted" assertion pass whatever the code did (found building
    attempt 13d)."""
    a = meta.get("action_state") or {}
    return a.get("target_patient") if a.get("action_type") == "add_medication" else None


def test_the_declaring_stub_puts_an_order_on_the_patient(h, client, monkeypatch):
    """The control that keeps the assertions above honest."""
    n = h["numw"]
    out = _steps_seen(client, monkeypatch, h["users"]["numw"], [("give her lasix 40 iv", n["12"])],
                      reply=_DECLARES)
    assert _drafted_for(out[0][1]) == n["12"]["name"], out[0][1].get("action_state")


@pytest.mark.parametrize("middle,second", [("how is bed 14?", "14"), ("is bed 14 febrile?", "14"),
                                           ("what is the potassium of bed 14?", "14")])
@pytest.mark.parametrize("chart", [True, False])
def test_a_new_question_about_another_bed_ends_the_exchange(h, client, monkeypatch, middle, second, chart):
    """Review 13b, R1: "give lasix 40 iv to bed 12", then a question about bed
    14, then "yes" drafted the lasix on bed 14's patient."""
    n = h["numw"]
    open_p = n["7"] if chart else None
    out = _steps_seen(client, monkeypatch, h["users"]["numw"], [
        ("give lasix 40 iv to bed 12", open_p), (middle, open_p), ("yes", open_p)], reply=_acting)
    content, meta, model_q, model_h = out[2]
    assert meta.get("patient_id") == n[second]["id"]
    assert _drafted_for(meta) is None, meta.get("action_state")
    assert model_q == middle, model_q
    assert not any("lasix" in (m.get("content") or "") for m in model_h), model_h


def test_another_bed_in_reply_to_an_order_question_loses_the_order(h, client, monkeypatch):
    """"14" to the question about bed 12's order, then "yes" to bed 14: the
    order is not moved to bed 14 (a lost order, never a moved one)."""
    n = h["numw"]
    out = _steps_seen(client, monkeypatch, h["users"]["numw"], [
        ("give lasix 40 iv to bed 12", n["7"]), ("14", n["7"]), ("yes", n["7"])], reply=_acting)
    content, meta, model_q, model_h = out[2]
    assert meta.get("patient_id") == n["14"]["id"]
    assert _drafted_for(meta) is None, meta.get("action_state")
    assert not any("lasix" in (m.get("content") or "") for m in model_h)


@pytest.mark.parametrize("stop", ["cancel that", "no, don't give it", "wait", "hold it"])
def test_a_cancelled_order_is_not_drafted_after_yes(h, client, monkeypatch, stop):
    n = h["numw"]
    out = _steps_seen(client, monkeypatch, h["users"]["numw"], [
        ("give lasix 40 iv to bed 12", n["7"]), (stop, n["7"]), ("yes", n["7"])], reply=_acting)
    content, meta, model_q, model_h = out[-1]
    assert _drafted_for(meta) is None, (stop, meta.get("action_state"))
    assert model_q is None or "lasix" not in model_q, model_q


def _assert_not_carried(turn, patient):
    """The server's notice for an order a reply did not carry: no model, no
    order, the patient named first."""
    from services.sage_service import (ORDER_NOT_CARRIED_NOTICE, READ_NOT_CARRIED_NOTICE,
                                       OTHERS_NOT_CARRIED_NOTICE)
    content, meta, model_q = turn[0], turn[1], turn[2]
    assert meta.get("query_scope") == "NOT_CARRIED", (meta.get("query_scope"), content[:200])
    assert model_q is None and not meta.get("action_state")
    assert content.startswith(f"About {patient['name']}"), content[:120]
    assert any(n in content for n in (ORDER_NOT_CARRIED_NOTICE, READ_NOT_CARRIED_NOTICE,
                                      OTHERS_NOT_CARRIED_NOTICE))


@pytest.mark.parametrize("reply,who", [
    ("this patient, stat", "7"), ("this patient asap", "7"), ("yes, this patient now", "7"),
    ("this patient, give it", "7"), ("for this patient instead", "7"),
    ("FIRST, stat", "12"), ("FIRST only", "12"), ("FIRST please start it", "12"),
    ("yes FIRST, go ahead and give", "12"), ("FIRST - give it", "12"), ("FIRST", "12"),
    ("FIRST, no thanks", "12"), ("no thank you, FIRST", "12"), ("this patient, no thanks", "7"),
    ("FIRST - no", "12"), ("FIRST no no", "12"), ("FIRST, no lasix", "12"),
    ("this patient, no lasix", "7"), ("FIRST? no to lasix", "12"), ("FIRST no stat", "12"),
])
def test_a_reply_that_is_not_the_certain_case_gets_the_notice(h, client, monkeypatch, reply, who):
    """Review 13b G1 and review 13c G1: replies that name the patient with
    go-ahead words, or decline, drafted the order or showed its narration.
    Attempt 13d: only "yes" or the offered bed carries an order; these get
    the server's notice and nothing is drafted."""
    n = h["numw"]
    first, last = n["12"]["name"].split()
    q = reply.replace("FIRST", first).replace("LAST", last)
    out = _steps_told(client, monkeypatch, h["users"]["numw"], [
        ("give lasix 40 iv to bed 12", n["7"]), (q, n["7"])])
    _assert_not_carried(out[1], n[who])


@pytest.mark.parametrize("reply", ["FIRST, is she febrile?", "FIRST, what is her potassium?",
                                   "this patient, how is the urine output?"])
def test_a_reply_with_words_of_its_own_never_shows_the_model_the_order(h, client, monkeypatch, reply):
    """A reply that is a message of its own keeps its own words, and the
    order Sage asked about is not in the model's history, so it cannot be
    narrated (review 13b, G1)."""
    n = h["numw"]
    q = reply.replace("FIRST", n["12"]["name"].split()[0])
    out = _steps_seen(client, monkeypatch, h["users"]["numw"], [
        ("give lasix 40 iv to bed 12", n["7"]), (q, n["7"])], reply="NOACT|Afebrile.")
    content, meta, model_q, model_h = out[1]
    assert model_q == q, model_q
    assert not any("lasix" in (m.get("content") or "") for m in model_h), model_h
    assert not any((m.get("content") or "").lstrip().startswith("Which patient") for m in model_h)


def test_the_model_history_keeps_settled_turns(h, client, monkeypatch):
    """Ordinary turns stay; only the requests Sage asked about, and the
    questions, are left out."""
    n = h["numw"]
    out = _steps_seen(client, monkeypatch, h["users"]["numw"], [
        ("what are her overnight obs?", n["7"]), ("how is bed 12?", n["7"]), ("yes", n["7"])],
        reply="NOACT|Stable.")
    _c, _m, model_q, model_h = out[-1]
    assert model_q == "how is bed 12?"
    texts = [(m.get("role"), m.get("content")) for m in model_h]
    assert texts[0] == ("user", "what are her overnight obs?"), texts
    assert ("user", "how is bed 12?") not in texts
    assert not any((t or "").lstrip().startswith("Which patient") for _r, t in texts)


def test_history_for_model_drops_asked_requests_and_questions():
    from services.sage_service import history_for_model, WHICH_PATIENT_QUESTION
    q = WHICH_PATIENT_QUESTION + "\n\n- Bed 12: A B, ward W\n\nReply \"yes\" for bed 12."
    hist = [{"role": "user", "content": "how is she?"}, {"role": "assistant", "content": "NOACT|Fine."},
            {"role": "user", "content": "give lasix to bed 12"}, {"role": "assistant", "content": q},
            {"role": "user", "content": "yes"}, {"role": "assistant", "content": "Drafted."},
            {"role": "user", "content": "potassium of 14?"}, {"role": "assistant", "content": q}]
    assert [m["content"] for m in history_for_model(hist)] == [
        "how is she?", "NOACT|Fine.", "yes", "Drafted."]


def test_the_exchange_stops_at_a_new_patient_and_at_a_cancel():
    from services.sage_service import _question_exchange, WHICH_PATIENT_QUESTION
    q12 = WHICH_PATIENT_QUESTION + "\n\n- Bed 12: A B, ward W\n\nReply \"yes\" for bed 12."
    q14 = WHICH_PATIENT_QUESTION + "\n\n- Bed 14: C D, ward W\n\nReply \"yes\" for bed 14."
    nobody = WHICH_PATIENT_QUESTION + "\n\nReply with the bed or the patient's name."
    order = {"role": "user", "content": "give lasix 40 iv to bed 12"}
    assert _question_exchange([order, {"role": "assistant", "content": q12},
                               {"role": "user", "content": "how is bed 14?"},
                               {"role": "assistant", "content": q14}]) == ["how is bed 14?"]
    assert _question_exchange([order, {"role": "assistant", "content": q12},
                               {"role": "user", "content": "potassium?"},
                               {"role": "assistant", "content": q12}]) == [
        "potassium?", "give lasix 40 iv to bed 12"]
    assert _question_exchange([order, {"role": "assistant", "content": q12},
                               {"role": "user", "content": "go ahead"},
                               {"role": "assistant", "content": nobody}]) == [
        "go ahead", "give lasix 40 iv to bed 12"]
    assert _question_exchange([order, {"role": "assistant", "content": q12},
                               {"role": "user", "content": "cancel that"},
                               {"role": "assistant", "content": q12}]) == ["cancel that"]


def test_the_request_given_to_the_model_is_capped():
    from services.sage_service import _read_what, _EXCHANGE_WHAT_MAX
    big = "potassium " * 200_000
    assert len(_read_what([big, big, big])) == _EXCHANGE_WHAT_MAX


# ══════════════════════════════════════════════════════════════════════════════
#  ATTEMPT 13d: an order crosses the question only in the certain case (review 13c)
# ══════════════════════════════════════════════════════════════════════════════

def test_a_new_question_inside_a_two_patient_offer_answers_the_read(h, client, monkeypatch):
    """Review 13c, G2: "give lasix 40 iv to Mr <surname>" offered two
    patients; "how is bed 34?" then "yes" drafted the lasix on bed 34."""
    n = h["numw"]
    a = _patient(NUMW, "33", name="Vexquorin Brogolvexith")
    b = _patient(NUMW, "34", name="Taddrimel Brogolvexith")
    try:
        out = _steps_seen(client, monkeypatch, h["users"]["numw"], [
            ("give lasix 40 iv to Mr Brogolvexith", n["7"]), ("how is bed 34?", n["7"]),
            ("yes", n["7"])], reply=_acting)
        assert out[0][1]["query_scope"] == "DISAMBIGUATE"
        content, meta, model_q, model_h = out[2]
        assert meta.get("patient_id") == b["id"]
        assert model_q == "how is bed 34?", model_q
        assert _drafted_for(meta) is None
        assert not any("lasix" in (m.get("content") or "") for m in model_h)
        out2 = _steps_told(client, monkeypatch, h["users"]["numw"], [
            ("give lasix 40 iv to Mr Brogolvexith", n["7"]), ("34", n["7"])])
        _assert_not_carried(out2[1], b)          # two were offered: not the certain case
    finally:
        _retire(a, b)


def test_a_two_patient_order_is_never_handed_over_whole(h, client, monkeypatch):
    """Review 13c, G3."""
    n = h["numw"]
    out = _steps_told(client, monkeypatch, h["users"]["numw"], [
        ("give lasix 40 iv to bed 12 and kcl 20 mmol iv to bed 14", n["7"]), ("14", n["7"])])
    assert out[0][1]["query_scope"] == "DISAMBIGUATE"
    _assert_not_carried(out[1], n["14"])


@pytest.mark.parametrize("reply", ["and FIRST14?", "now FIRST14", "FIRST14"])
def test_another_patient_named_in_reply_to_an_order_question_gets_the_notice(h, client, monkeypatch, reply):
    """Review 13c, G4: "and <name of another patient>?" moved the order."""
    n = h["numw"]
    q = reply.replace("FIRST14", n["14"]["name"].split()[0])
    out = _steps_told(client, monkeypatch, h["users"]["numw"], [
        ("give lasix 40 iv to bed 12", n["7"]), (q, n["7"])])
    _assert_not_carried(out[1], n["14"])


def test_a_retyped_order_completes_after_the_notice(h, client, monkeypatch):
    n = h["numw"]
    first = n["12"]["name"].split()[0]
    out = _steps_told(client, monkeypatch, h["users"]["numw"], [
        ("give lasix 40 iv to bed 12", n["7"]), (f"{first}, stat", n["7"]),
        ("give lasix 40 iv to bed 12", n["7"]), ("yes", n["7"])])
    _assert_not_carried(out[1], n["12"])
    assert out[2][1]["query_scope"] == "DISAMBIGUATE"
    _assert_gated(out[3], n["12"], "give lasix 40 iv to bed 12")


def test_a_settled_read_keeps_its_context_for_the_next_turn(h, client, monkeypatch):
    """Review 13c (F): "is bed 12 on heparin?", "yes", "should she be?", "yes"
    gave the model "should she be?" with history ["yes"] only."""
    n = h["numw"]
    out = _steps_seen(client, monkeypatch, h["users"]["numw"], [
        ("is bed 12 on heparin?", n["7"]), ("yes", n["7"]), ("should she be?", n["7"]),
        ("yes", n["7"])], reply="NOACT|On prophylactic dose.")
    _c, _m, model_q, model_h = out[3]
    assert model_q == "should she be?", model_q
    texts = [m.get("content") for m in model_h if m.get("role") == "user"]
    assert "is bed 12 on heparin?" in texts, texts
    assert "yes" not in texts, texts


def test_history_for_model_never_puts_an_order_back():
    from services.sage_service import history_for_model, WHICH_PATIENT_QUESTION
    q = WHICH_PATIENT_QUESTION + "\n\n- Bed 12: A B, ward W\n\nReply \"yes\" for bed 12."
    hist = [{"role": "user", "content": "give lasix 40 iv to bed 12"}, {"role": "assistant", "content": q},
            {"role": "user", "content": "yes"}, {"role": "assistant", "content": "Drafted request."},
            {"role": "user", "content": "is bed 12 on heparin?"}, {"role": "assistant", "content": q},
            {"role": "user", "content": "yes"}, {"role": "assistant", "content": "NOACT|Yes."}]
    assert [m["content"] for m in history_for_model(hist)] == [
        "yes", "Drafted request.", "is bed 12 on heparin?", "NOACT|Yes."]


@pytest.mark.parametrize("query", ["bed 713 give lasix 40 iv", "bed 712: lasix 40 iv stat"])
def test_another_departments_short_bed_code_is_not_found_inside_a_number(h, query):
    """Reported by the order-gate session (9 October): on main the
    out-of-department matcher squashes the question and finds another
    department's bed "D7" inside "bed713", so the turn is refused as outside
    the department. The locator reads whole labels."""
    from services.sage_service import OUT_OF_SCOPE_NOTICE
    other = _patient("Locator Dseven Medicine", "D7", name="Quillvarent Ostrakomb")
    try:
        r = _ask(h, "gm", query, open_bed="GM-101")
        assert r["qscope"] != "OUT_OF_SCOPE" and OUT_OF_SCOPE_NOTICE not in r["context"], (
            query, r["qscope"])
    finally:
        _retire(other)


@pytest.mark.parametrize("wording", ["GM-102 needs lasix 40 iv", "GM-102 would benefit from lasix 40 iv"])
@pytest.mark.parametrize("reply", ["GM-103", "GM-102"])
def test_a_message_about_two_beds_is_never_handed_over_for_one(h, client, monkeypatch, reply, wording):
    """Review 13c, W23: the order detector did not read "GM-102 needs lasix 40
    iv" as an order, so the message went to the model whole on GM-103's
    chart and the lasix was drafted there. Read or not, nothing is carried."""
    g = h["gm"]
    out = _steps_seen(client, monkeypatch, h["users"]["gm"], [
        (f"{wording}, and check GM-103 potassium", g["GM-101"]),
        (reply, g["GM-101"])], reply=_acting)
    assert out[0][1]["query_scope"] == "DISAMBIGUATE"
    _assert_not_carried(out[1][:3], g[reply])
    assert _drafted_for(out[1][1]) is None


def test_a_name_shared_by_two_patients_is_asked_again_without_the_name(h, client, monkeypatch):
    """Attempt 13f (certain side only): a name two patients share is two
    patients, so the read is not carried; asked again, it is answered.
    Attempt 13e carried it, and "Rajesh Kumar ... Suresh Kumar" drafted
    Rajesh's lasix on Suresh (review 13e, G1)."""
    from services.sage_service import OTHERS_NOT_CARRIED_NOTICE
    n = h["numw"]
    a = _patient(NUMW, "33", name="Vexquorin Drallomethic")
    b = _patient(NUMW, "34", name="Taddrimel Drallomethic")
    try:
        out = _steps_seen(client, monkeypatch, h["users"]["numw"], [
            ("how is Drallomethic doing?", n["7"]), ("34", n["7"])], reply="NOACT|Stable.")
        assert out[0][1]["query_scope"] == "DISAMBIGUATE"
        content, meta, model_q, model_h = out[1]
        assert meta.get("query_scope") == "NOT_CARRIED" and model_q is None, meta.get("query_scope")
        assert content.startswith(f"About {b['name']}") and OTHERS_NOT_CARRIED_NOTICE in content
    finally:
        _retire(a, b)


# ══════════════════════════════════════════════════════════════════════════════
#  ATTEMPT 13e: one carry rule for both branches (review 13d)
# ══════════════════════════════════════════════════════════════════════════════

#: Orders as written on a round, read and unread by the order detector: the
#: first three were unread before 324e754 widened it; the last three are
#: still unread after it (indirect wording, number words), so they take the
#: READ branch, where review 13d found the holes.
_UNREAD_ORDERS = ["lasix 40 iv for bed 12", "bed 12 needs lasix 40 iv", "push lasix 40 iv to bed 12",
                  "bed 12 would benefit from lasix 40 iv", "bed 12 could do with lasix forty iv",
                  "i think bed 12 needs some lasix"]


@pytest.mark.parametrize("order", _UNREAD_ORDERS)
@pytest.mark.parametrize("reply", ["this patient, no", "this patient, no thanks", "FIRST, no",
                                   "no thank you, FIRST"])
def test_a_decline_after_any_order_gets_the_notice(h, client, monkeypatch, order, reply):
    """Review 13d, G-B: an order the detector may not read took the read branch,
    where "this patient, no" was carried and the order moved to the open
    chart. Whatever the detector says, a decline carries nothing."""
    n = h["numw"]
    q = reply.replace("FIRST", n["12"]["name"].split()[0])
    out = _steps_seen(client, monkeypatch, h["users"]["numw"], [(order, n["7"]), (q, n["7"])],
                      reply=_acting)
    if out[0][1]["query_scope"] != "DISAMBIGUATE":
        pytest.skip("this wording is not asked about on this build")
    who = n["7"] if q.startswith("this patient") else n["12"]
    content, meta, model_q, model_h = out[1]
    assert meta.get("query_scope") == "NOT_CARRIED" and model_q is None, (q, meta.get("query_scope"))
    assert content.startswith(f"About {who['name']}")
    assert _drafted_for(meta) is None


@pytest.mark.parametrize("wording", ["GM-102 needs lasix 40 iv", "GM-102 would benefit from lasix 40 iv"])
def test_a_bed_plus_a_name_is_two_patients(h, client, monkeypatch, wording):
    """Review 13d, G-B: "GM-102 needs lasix 40 iv, and check <name of the
    GM-103 patient>'s potassium", then that name: the bed check counted one
    bed and the whole message went to GM-103's chart."""
    g = h["gm"]
    p = _patient(GM, "GM-106", "GM-1", name="Prillvaxen Odrunquist")
    try:
        out = _steps_seen(client, monkeypatch, h["users"]["gm"], [
            (f"{wording}, and check Prillvaxen Odrunquist's potassium", g["GM-101"]),
            ("Prillvaxen Odrunquist", g["GM-101"])], reply=_acting)
        assert out[0][1]["query_scope"] == "DISAMBIGUATE", out[0][1].get("query_scope")
        content, meta, model_q, model_h = out[1]
        assert meta.get("query_scope") == "NOT_CARRIED" and model_q is None, (meta.get("query_scope"), model_q)
        assert _drafted_for(meta) is None
    finally:
        _retire(p)


@pytest.mark.parametrize("order", _UNREAD_ORDERS)
def test_another_name_after_any_order_is_not_carried(h, client, monkeypatch, order):
    """Review 13d (B8/B9): an undetected order, then the bare name of another
    patient, drafted on that patient; a detected order got the notice."""
    n = h["numw"]
    first14 = n["14"]["name"].split()[0]
    out = _steps_seen(client, monkeypatch, h["users"]["numw"], [(order, n["7"]), (first14, n["7"])],
                      reply=_acting)
    if out[0][1]["query_scope"] != "DISAMBIGUATE":
        pytest.skip("this wording is not asked about on this build")
    content, meta, model_q, model_h = out[1]
    assert _drafted_for(meta) is None, (order, meta.get("action_state"))
    assert model_q is None or "lasix" not in model_q, model_q


def test_a_notice_is_never_undone_by_the_history():
    """Review 13d, G-C: the history restore put a message a notice had
    refused back in front of the model."""
    from services.sage_service import history_for_model, WHICH_PATIENT_QUESTION, READ_NOT_CARRIED_NOTICE
    q = WHICH_PATIENT_QUESTION + "\n\n- Bed 13: A B, ward W\n\nReply \"yes\" for bed 13."
    hist = [{"role": "user", "content": "bed 12 needs lasix 40 iv, check bed 13 potassium"},
            {"role": "assistant", "content": q},
            {"role": "user", "content": "13"},
            {"role": "assistant", "content": "About A B, bed 13:\n\n" + READ_NOT_CARRIED_NOTICE}]
    shown = [m["content"] for m in history_for_model(hist)]
    assert not any("lasix" in t for t in shown), shown


def test_voice_speaks_the_notice_without_the_model(h):
    """Review 13d: voice handed the notice to its model as patient context."""
    from services.sage_service import _which_patient_question, ORDER_NOT_CARRIED_NOTICE
    n = h["numw"]
    q = _which_patient_question([n["12"]], True)
    said = _voice(f"{n['12']['name'].split()[0]}, stat", h["users"]["numw"], n["7"]["id"],
                  history=[{"query": "give lasix 40 iv to bed 12", "response": q}])
    assert ORDER_NOT_CARRIED_NOTICE in said and said.startswith(f"About {n['12']['name']}"), said[:200]


def test_a_message_too_long_to_read_whole_is_never_carried(h):
    from services.sage_service import _refers_only_to, _LOCATOR_CACHE_MAX
    n = h["numw"]
    row = {"id": n["12"]["id"], "name": n["12"]["name"], "bed": "12", "ward": n["12"]["ward"]}
    assert not _refers_only_to([], [], "how is she? " + "x " * _LOCATOR_CACHE_MAX, row)


@pytest.mark.parametrize("order", ["give lasix 40 iv, but not for this patient",
                                   "lasix 40 iv stat, not this patient"])
def test_this_patient_never_takes_an_order_that_rules_it_out(h, client, monkeypatch, order):
    """Review 13d (D2): the message says not this patient, then "this patient"
    drafted it on the open chart."""
    n = h["numw"]
    first = n["12"]["name"].split()[0]
    out = _steps_told(client, monkeypatch, h["users"]["numw"], [
        (f"how is {first} doing?", n["7"]), (order, n["7"]), ("this patient", n["7"])],
        reply="NOACT|Stable.")
    if out[1][1].get("query_scope") != "DISAMBIGUATE":
        pytest.skip("this wording is not asked about on this build")
    content, meta, model_q = out[2]
    assert not meta.get("action_state"), (order, meta.get("action_state"))


# ══════════════════════════════════════════════════════════════════════════════
#  ATTEMPT 13f: the certain side only (review 13e)
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("message,reply_kind", [
    ("give lasix 40 iv to Rajvexan Kumbrelt and kcl 20 mmol iv to Surovant Kumbrelt", "this"),
    ("Rajvexan Kumbrelt would benefit from lasix 40 iv, and how is Surovant Kumbrelt?", "bed"),
    ("Rajvexan Kumbrelt could do with lasix forty iv, and how is Surovant Kumbrelt?", "name"),
])
def test_two_patients_who_share_a_name_word_are_two_patients(h, client, monkeypatch, message, reply_kind):
    """Review 13e, G1: a name word two patients share counted as one
    ambiguous reference, and Rajvexan's lasix was drafted on Surovant."""
    n = h["numw"]
    raj = _patient(NUMW, "35", name="Rajvexan Kumbrelt")
    sur = _patient(NUMW, "36", name="Surovant Kumbrelt")
    try:
        reply = {"this": "this patient", "bed": "36", "name": "Surovant Kumbrelt"}[reply_kind]
        out = _steps_seen(client, monkeypatch, h["users"]["numw"], [(message, sur), (reply, sur)],
                          reply=_acting)
        assert out[0][1]["query_scope"] == "DISAMBIGUATE", out[0][1].get("query_scope")
        if out[1][1].get("query_scope") == "DISAMBIGUATE":
            # The full name also matched by surname, so Sage asked again: safe.
            assert out[1][2] is None
        else:
            _assert_not_carried(out[1][:3], sur)
        assert _drafted_for(out[1][1]) is None
    finally:
        _retire(raj, sur)


@pytest.mark.parametrize("message", [
    "give lasix 40 iv to 12 and kcl 20 mmol iv to 14",
    "lasix 40 iv to 12 and kcl 20 to 14",
    "12 would benefit from lasix 40 iv and 14 could do with kcl 20 mmol iv",
    "give lasix 40 iv to 12 and kcl 20 mmol iv to this patient",
    "she needs lasix 40 iv, and 14 needs kcl 20 mmol iv",
    "give lasix 40 iv to bed 12 stat and kcl 20 mmol iv to bed 14 stat",
])
def test_this_patient_never_disowns_a_bed(h, client, monkeypatch, message):
    """Review 13e, G2: "this patient" disowned readings Sage was unsure of, and
    an order for 12 or 14 went to the open chart."""
    n = h["numw"]
    out = _steps_seen(client, monkeypatch, h["users"]["numw"], [(message, n["7"]), ("this patient", n["7"])],
                      reply=_acting)
    if out[0][1]["query_scope"] != "DISAMBIGUATE":
        pytest.skip("this wording is not asked about on this build")
    _assert_not_carried(out[1][:3], n["7"])
    assert _drafted_for(out[1][1]) is None


@pytest.mark.parametrize("message", [
    "give lasix 40 iv to bed 12 instead of this patient",
    "give lasix 40 iv to bed 12 rather than this patient",
    "give lasix 40 iv to bed 12, unlike this patient",
    "give lasix 40 iv to everyone excluding this patient, start with bed 12",
    "give lasix 40 iv to bed 12 and leave this patient alone",
    "bed 12 needs lasix 40 iv, this patient already had it",
])
def test_a_message_that_mentions_the_open_chart_is_never_carried(h, client, monkeypatch, message):
    """Review 13e, G3: the negation list missed "instead of", "rather than" and
    the rest, and the lasix was drafted on the patient the message excluded.
    Attempt 13f needs no list: any mention of the open chart is enough."""
    n = h["numw"]
    out = _steps_seen(client, monkeypatch, h["users"]["numw"], [(message, n["7"]), ("this patient", n["7"])],
                      reply=_acting)
    if out[0][1]["query_scope"] != "DISAMBIGUATE":
        pytest.skip("this wording is not asked about on this build")
    _assert_not_carried(out[1][:3], n["7"])
    assert _drafted_for(out[1][1]) is None


@pytest.mark.parametrize("message", [
    "bed 31 would benefit from lasix 40 iv, and bed 31 could do with kcl 20 mmol iv",
    "give kcl 20 mmol iv to bed 31 and lasix 40 iv to bed 31",
])
def test_one_bed_label_on_two_wards_is_two_patients(h, client, monkeypatch, message):
    """Review 13e, G4: one bed label on two wards, written as two references,
    counted as one, and DWB's KCl landed on DWA's bed."""
    a = _patient(NUMW, "31", ward="Locator Ward Alpha", name="Hesquorin Tavelmund")
    b = _patient(NUMW, "31", ward="Locator Ward Beta", name="Joravique Pelstrand")
    try:
        out = _steps_seen(client, monkeypatch, h["users"]["numw"], [
            (message, h["numw"]["7"]), ("Hesquorin Tavelmund", h["numw"]["7"])], reply=_acting)
        assert out[0][1]["query_scope"] == "DISAMBIGUATE", out[0][1].get("query_scope")
        _assert_not_carried(out[1][:3], a)
        assert _drafted_for(out[1][1]) is None
    finally:
        _retire(a, b)


def test_a_one_patient_order_still_crosses_in_the_certain_case(h, client, monkeypatch):
    """The certain case is untouched: "give lasix 40 iv to bed 12", then "yes"."""
    n = h["numw"]
    out = _steps_told(client, monkeypatch, h["users"]["numw"], [
        ("give lasix 40 iv to bed 12", n["7"]), ("yes", n["7"])])
    _assert_gated(out[1], n["12"], "give lasix 40 iv to bed 12")


@pytest.mark.parametrize("reply", ["this patient", "FIRST12"])
def test_a_paste_past_the_bed_cap_is_never_carried(h, client, monkeypatch, reply):
    """Review 13f, G-CAP: past _BED_REF_CAP the locator stops reading and
    reports no bed, so the carry rule saw "read nobody" and "this patient"
    carried a paste about other beds to the open chart."""
    n = h["numw"]
    paste = "give lasix 40 iv to " + ", ".join(["bed 1", "bed 2", "bed 3"] * 367)
    q = reply.replace("FIRST12", n["12"]["name"])
    out = _steps_seen(client, monkeypatch, h["users"]["numw"], [(paste, n["7"]), (q, n["7"])],
                      reply=_acting)
    assert out[0][1]["query_scope"] == "DISAMBIGUATE", out[0][1].get("query_scope")
    content, meta, model_q, model_h = out[1]
    assert _drafted_for(meta) is None, (reply, meta.get("action_state"))
    assert model_q is None or "lasix" not in model_q


# ══════════════════════════════════════════════════════════════════════════════
#  ATTEMPT 13f.2: another department's bed code is someone else (review 13f.1, F-1)
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("second", ["kcl 20 mmol iv to OXQ-3", "start kcl 20 mmol iv for OXQ-3",
                                    "OXQ-3 needs kcl 20 mmol iv",
                                    "OXQ-3 would benefit from kcl 20 mmol iv"])
@pytest.mark.parametrize("reply", ["yes", "GM-102", "this patient"])
def test_a_message_naming_another_departments_bed_is_never_carried(h, client, monkeypatch, second, reply):
    """Review 13f.1, F-1: the locator reads another department's code (into
    `foreign`), and the carry rule never looked at it, so a re-asked "kcl ...
    to OX-3" was drafted on the offered GM-102, or on the open chart."""
    g = h["gm"]
    other = _patient("Locator Oxq Medicine", "OXQ-3", name="Wrennquist Talvorandine")
    try:
        out = _steps_seen(client, monkeypatch, h["users"]["gm"], [
            ("give lasix 40 iv to GM-102", g["GM-101"]), (second, g["GM-101"]), (reply, g["GM-101"])],
            reply=_acting)
        assert out[0][1]["query_scope"] == "DISAMBIGUATE", out[0][1].get("query_scope")
        content, meta, model_q, model_h = out[-1]
        assert _drafted_for(meta) is None, (second, reply, meta.get("action_state"))
        assert model_q is None or "kcl" not in model_q, model_q
    finally:
        _retire(other)


def test_an_unread_paste_past_the_cap_is_not_carried_by_a_name_reply(h, client, monkeypatch):
    """Review 13f.1: the cap test's name reply also passed on 13f, so the
    read-branch path 13f carried (an order the detector does not read, then a
    name) needs its own test."""
    p = _patient(NUMW, "29", name="Bralstimor Quenneville")
    try:
        paste = ", ".join(["bed 1", "bed 2", "bed 3"] * 367) + " would benefit from lasix 40 iv"
        out = _steps_seen(client, monkeypatch, h["users"]["numw"], [
            (paste, None), ("Bralstimor Quenneville", None)], reply=_acting)
        assert out[0][1]["query_scope"] == "DISAMBIGUATE", out[0][1].get("query_scope")
        content, meta, model_q, model_h = out[1]
        assert meta.get("query_scope") != "DISAMBIGUATE", "the name reply must settle the question"
        assert _drafted_for(meta) is None, meta.get("action_state")
        assert model_q is None or "lasix" not in model_q
    finally:
        _retire(p)
