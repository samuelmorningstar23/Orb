"""Sage bed and ward lookup (ruling R026, 3 October 2026; attempt 4).

THE DEFECT. Sage found a bed by squashing the question and every bed label to
letters and digits and testing for a SUBSTRING. Ward "GM-1" sits inside bed
"GM-103", so "Summarise bed GM-103" asked the doctor to pick among all of GM-1;
bed "A1" sits inside "HbA1c", bed "12" inside "after 12 hours". When exactly
one patient matched that way, Sage attached a chart the question was not about.

THREE ATTEMPTS WERE BACKED OUT. The first matched less, and a turn that
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
question; the model is never asked to.

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
NAMES = "Locator Namesake Medicine"


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
    s["prefix"] = {b: _patient(PREFIX, b) for b in ("D-7", "H1", "ANC-01", "NS-01", "COVID-01")}
    s["wards2"] = {f"{w}/{b}": _patient(WARDS2, b, w) for w, b in
                   (("5", "12"), ("5", "14"), ("6", "3"), ("6", "20"))}
    s["names"] = {
        "a": _patient(NAMES, "N-1", name="Priyamvada Thekkekara"),
        "b": _patient(NAMES, "N-2", name="Siddharthan Thekkekara"),
        "c": _patient(NAMES, "N-3"),
    }
    s["users"] = {k: _user(d) for k, d in (
        ("num", NUM), ("numw", NUMW), ("miss", MISS), ("gm", GM), ("nines", NINES),
        ("zero", ZERO), ("a1", A1), ("codes", CODES), ("stop", STOP), ("wards", WARDS),
        ("mine", MINE), ("hist", HIST), ("label", LABEL), ("shapes", SHAPES), ("hdu", HDU),
        ("gone", GONE), ("wards2", WARDS2), ("names", NAMES))}
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
    return {"context": context, "scope": scope, "pctx": pctx, "qscope": qscope}


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
    assert r["qscope"] == scope, (r["qscope"], r["context"][:300])
    assert r["pctx"] is not None and r["pctx"]["patient_id"] == patient["id"], (
        "the chart attached is not the patient asked about")


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
    "bed twelve lactate?",
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
    ("what is the potassium of the patient in bed 15", "15"),
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


def test_a_ward_question_still_finds_the_whole_ward(h):
    g = h["gm"]
    r = _ask(h, "gm", "How is ward GM-1 doing?", open_bed="RB-102")
    _assert_asked(r, *(g[b] for b in ("GM-101", "GM-102", "GM-103", "GM-104", "GM-105")),
                  absent=[g["CCU-01"], g["RB-101"]])


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
    ("num", "7", "give 1 gm 8 hourly?"),
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
    ("miss", "7", "start D5W?"),
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
    ("bed twenty one lactate?", "21"),
    ("bed twenty-one lactate?", "21"),
    ("bed thirty lactate?", "30"),
])
def test_spoken_numbers_read_whole(h, query, bed):
    _assert_chart(_ask(h, "numw", query, open_bed="7"), h["numw"][bed])


@pytest.mark.parametrize("query,label", [
    ("bed twenty-two lactate?", "22"),
    ("bed fifty lactate?", "50"),
    ("bed one two lactate?", "one two"),
    ("bed 1 2 lactate?", "1 2"),
    ("beds 1-2 lactate?", "1-2"),
    ("beds 12–14 lactate?", "12-14"),
    ("beds 12 to 14 lactate?", "12 to 14"),
    ("bed hundred and two lactate?", "hundred and two"),
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


@pytest.mark.parametrize("missing", ["Summarise bed 77", "bed‑77 lactate?",
                                     "bed seventy seven lactate?"])
def test_a_missing_bed_in_the_last_turn_stops_the_history_anchor(h, missing):
    """No chart open. The doctor asked about bed 31, then about a bed nobody is
    in, then "and her lactate?". Walking past that turn to bed 31 would
    answer about a patient the doctor had moved on from."""
    r = _ask(h, "hist", "and her lactate?", history=_thread("How is bed 31 doing?", missing))
    assert r["pctx"] is None, "the thread was bound to a patient the doctor moved on from"
    assert h["hist"]["31"]["name"] not in r["context"]


def test_a_resolved_bed_in_history_still_anchors(h):
    r = _ask(h, "hist", "and her lactate?", history=_thread("How is bed 31 doing?"))
    assert r["pctx"] is not None and r["pctx"]["patient_id"] == h["hist"]["31"]["id"]


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
    ("bed twelve lactate?", True),
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
    "bed twelve day 3 of tazo?",
    "bed 12 L sided weakness",
    "bed 12 am bloods back?",
    "bed 12 pm insulin dose?",
    "bed 12 drops sats when flat",
    "bed 12 dose of vanc?",
    "bed 12 +ve blood cultures",
    "bed 12 feet are cold",
    "bed 12 hourly obs",
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
    "is a CCU 1 bed free for her?",
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
    r = _ask(h, "numw", reply, open_bed="7", history=_thread("how is 12?"))
    assert r["qscope"] == "DISAMBIGUATE" and r["pctx"] is None, r["qscope"]
    assert r["context"].startswith("Which patient do you mean?")
    assert h["numw"]["7"]["name"] not in r["context"]


def test_this_patient_after_an_ask_means_the_open_chart(h):
    r = _ask(h, "numw", "this patient please", open_bed="7", history=_thread("how is 12?"))
    assert r["pctx"] is not None and r["pctx"]["patient_id"] == h["numw"]["7"]["id"]


def test_a_bed_after_an_ask_is_that_bed(h):
    _assert_chart(_ask(h, "numw", "bed 12", open_bed="7", history=_thread("how is 12?")),
                  h["numw"]["12"])


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
