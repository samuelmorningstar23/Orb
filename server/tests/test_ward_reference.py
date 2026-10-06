"""One ward rule for Sage retrieval and the PHI classifier (4 October 2026).

Both lanes squashed the question and the ward label to letters and digits and
tested for a substring. Measured on the seeded wards (CCU, GM-1, NW-1, OW-1,
RCU, SW-1): in a CCU doctor's session "Does atrial fibrillation occur often
after cardiac surgery?" came back as "which patient did you mean?" with every
CCU patient named in the prompt, and the classifier read ward "OR" inside
"for". Ruled by Samuel the same day: whole tokens; a one-token word-like label
("OR", "ER") counts in capitals or right after the word "ward". Also pinned:
the route labels a turn PHI whenever retrieval put patient rows in the prompt
without a chart (DISAMBIGUATE, BROAD).
"""
import json
import uuid
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import database
import services.ai_governance as gov
import services.clinical_knowledge as ck
import services.sage_service as sage
from auth import create_token
from database import db_session
from services.sage_service import get_relevant_context
from services.ward_reference import ward_named_in


@pytest.fixture(autouse=True)
def _no_live_retrieval(monkeypatch):
    monkeypatch.setattr(ck, "_cross_encoder", None)
    monkeypatch.setattr(ck, "_cross_encoder_checked", True)
    monkeypatch.setattr(ck, "search_guidelines", lambda *a, **k: [])


# ── the rule ─────────────────────────────────────────────────────────────────
WARD_TABLE = [
    # (ward label, text as typed, names the ward?)
    ("CCU", "Does atrial fibrillation occur often after cardiac surgery?", False),
    ("CCU", "What is the accuracy of troponin for ruling out MI?", False),
    ("CCU", "any concerns in ccu tonight", True),
    ("CCU", "CCU census please", True),
    ("RCU", "Explain the circulation in the fetal heart", False),
    ("OW-1", "Is a heart rate below 100 normal?", False),
    ("OW-1", "follow 1 litre with another", False),
    ("OW-1", "how is ow 1 tonight", True),
    ("GM-1", "how is gm 1", True),
    ("GM-1", "how is gm-1", True),
    ("GM-1", "how is GM1", True),
    ("GM-1", "how is gm 01", True),             # leading zeros ignored
    ("GM-1", "how is gm 101", False),           # a different number
    ("GM-1", "how is gm", False),
    ("ICU-02", "who is in icu 2", True),
    ("NW-1", "nw1 handover", True),
    ("A1", "is the hba1c high", False),
    ("General Medicine", "general practice and medicine", False),
    # a code of several ordinary words needs a sign too (2026-10-06)
    ("General Medicine", "Is this a general medicine or a surgical problem?", False),
    ("General Medicine", "How is hyponatraemia managed in general medicine?", False),
    ("General Medicine", "General medicine textbooks you would recommend?", False),
    ("General Medicine", "who is on general medicine today", False),     # lower case, no "ward"
    ("General Medicine", "General Medicine handover at 8?", False),      # opens the sentence
    ("General Medicine", "Any concerns on General Medicine tonight?", True),
    ("General Medicine", "who is on the general medicine ward", True),
    ("General Medicine", "GENERAL MEDICINE census please", True),
    ("Care of the Elderly", "What does care of the elderly involve in India?", False),
    ("Care of the Elderly", "Anyone on Care of the Elderly deteriorating?", True),
    ("High Dependency", "When is high dependency care indicated?", False),
    ("High Dependency", "Who is in High Dependency tonight?", True),
    ("Step Down", "Step down the steroid dose after a week?", False),
    ("Step Down", "Anyone on Step Down tonight?", True),
    # a place word or an abbreviation in the code is sign enough
    ("Burns Unit", "who is in the burns unit", True),
    ("Labour Room", "anyone in labour room tonight", True),
    ("Neuro ICU", "neuro icu census", True),
    # the words of a code sit in one sentence
    ("Labour Room", "Is she in labour? Room 4 is free.", False),
    # one-token word-like labels: capitals or after "ward"
    ("OR", "is it okay for her", False),
    ("OR", "Should I give amoxicillin or cefuroxime?", False),
    ("OR", "Or should I wait?", False),
    ("OR", "patient back from OR, antibiotics due?", True),
    ("OR", "who is on ward or today", True),
    ("OR", "WHO IS IN OR", True),
    ("ER", "er, what is the dose of ceftriaxone", False),
    ("ER", "came up via ER last night", True),
    ("IN", "who is in the ward", False),
    ("IN", "who is in ward in", True),
    ("OR", "Should I give A, Or B?", True),      # a capital mid-sentence
    ("OR", "OR ward handover", True),
    ("OR", "who is in or ward", False),         # a short English word before "ward"
    # one-word codes that are ordinary words: a capital, mid-sentence capital,
    # or "ward" beside them (ruled 2026-10-04)
    ("Surgery", "Is surgery indicated for appendicitis?", False),
    ("Surgery", "Pain after cardiac surgery, what analgesia?", False),
    ("Surgery", "Surgery is indicated when pain persists", False),
    ("Surgery", "Any beds free on Surgery?", True),
    ("Surgery", "who is on the surgery ward", True),
    ("Surgery", "ward surgery handover please", True),
    ("Surgery", "SURGERY census", True),
    ("Recovery", "Recovery after MI takes weeks", False),
    ("Recovery", "how is recovery going after the op", False),
    ("Recovery", "Is she stable? Recovery says yes.", False),
    ("Recovery", "anyone still in Recovery?", True),
    ("OBS", "repeat obs in an hour", False),
    ("OBS", "who is on OBS tonight", True),
    ("Male", "male, 45, chest pain", False),
    ("Male", "Male, 45, chest pain", False),
    ("Male", "any beds on male ward", True),
    ("Maternity", "maternity leave policy", False),
    ("Maternity", "any deliveries on Maternity tonight", True),
    ("Lotus", "Is Lotus full tonight?", True),
    ("East", "the east side of the building", False),
    ("East", "any beds on East", True),
    # digits only: beside "ward"
    ("6", "give 6 units of insulin", False),
    ("6", "who is on ward 6", True),
    ("06", "who is on ward 6", True),
    ("6", "the 6 ward round is late", True),
    # abbreviations stay case-blind
    ("ICU", "icu census", True),
    ("NICU", "nicu handover", True),
    # a generic mention names no ward (ruled 2026-10-06): a listed noun after
    # the code, or a need or destination before it
    ("ICU", "What are the ICU admission criteria for DKA?", False),
    ("ICU", "What is ICU delirium and how do we prevent it?", False),
    ("ICU", "Explain ICU-acquired weakness", False),
    ("ICU", "Should we call the ICU team for a NEWS2 of 7?", False),
    ("ICU", "Is high-flow oxygen an ICU-level intervention?", False),
    ("ICU", "Is there ICU bed availability tonight?", False),
    ("ICU", "When does acute pancreatitis need ICU?", False),
    ("ICU", "Is he for ICU if he deteriorates?", False),
    ("CCU", "Should she go to CCU?", False),
    ("Surgery", "Call the Surgery team about her wound", False),
    # ...but someone who already got there is on the ward now
    ("ICU", "Who was admitted to ICU today?", True),
    ("CCU", "Any new admissions to CCU overnight?", True),
    ("ICU", "Who came back to ICU overnight?", True),
    ("ICU", "transferred into ICU this morning, how is he", True),
    # ...and an ordinary reference still names it
    ("ICU", "Any concerns in ICU tonight?", True),
    ("ICU", "Which ICU patients are on vasopressors?", True),
    ("ICU", "How is ICU doing?", True),
    ("ICU", "ICU handover please", True),
    ("ICU", "What do we need? ICU census.", True),      # the cue is in another sentence
    # a phrasing the lists do not cover still names the ward (known residual)
    ("ICU", "What is the mortality of septic shock in ICU?", True),
    # nothing names an empty label
    ("", "anything at all", False),
    (None, "anything at all", False),
]


@pytest.mark.parametrize("label,text,expected", WARD_TABLE)
def test_ward_rule_table(label, text, expected):
    assert ward_named_in(label, text) is expected, (label, text)


def test_lower_cased_text_still_finds_every_ward_but_capitals():
    """A caller holding only the lower-cased question still finds every
    abbreviation and multi-part code, and a word code beside "ward"; only the
    capital signs need the text as typed."""
    assert ward_named_in("CCU", "ccu census") is True
    assert ward_named_in("GM-1", "how is gm-1") is True
    assert ward_named_in("OR", "who is on ward or today") is True
    assert ward_named_in("Surgery", "who is on the surgery ward") is True
    assert ward_named_in("OR", "patient back from or") is False
    assert ward_named_in("Surgery", "any beds free on surgery") is False


ICU_GENERIC = [
    "What are the ICU admission criteria for DKA?",
    "What is ICU delirium and how do we prevent it?",
    "Explain ICU-acquired weakness",
    "Should we call the ICU team for a NEWS2 of 7?",
    "When does acute pancreatitis need ICU?",
]
ICU_REFERENCE = [
    "Any concerns in ICU tonight?",
    "Which ICU patients are on vasopressors?",
    "Who was admitted to ICU today?",
]


def test_a_generic_icu_question_lists_no_icu_patient():
    """Measured 2026-10-06 with bed locator attempt 8 live: every one of these
    put both ICU patients in the prompt as the ICU census. A destination ("is
    she for ICU?") now goes to the patient being discussed instead."""
    dept = f"Ward Rule Intensive {uuid.uuid4().hex[:4]}"
    icu = ["Qzxicuone Pallavaraman", "Qzxicutwo Thirukkovilur"]
    pids = [_patient(n, "ICU", f"ICU-0{i + 1}", dept) for i, n in enumerate(icu)]
    own = "Bhairavi Kandasamyputhur"
    pids.append(_patient(own, "GM-9", "GM-901", dept))
    user = _user(dept)
    try:
        for question in ICU_GENERIC:
            context, *_rest, pctx, qscope = get_relevant_context(question, user, history=[])
            assert not any(n in context for n in icu), (question, qscope)
        for question in ICU_REFERENCE:
            context, *_rest, pctx, qscope = get_relevant_context(question, user, history=[])
            assert all(n in context for n in icu), (question, qscope)
        history = [{"role": "user", "content": f"How is {own} doing?"},
                   {"role": "assistant", "content": "Stable overnight."}]
        context, *_rest, pctx, qscope = get_relevant_context(
            "Is she for ICU if she deteriorates?", user, history=history)
        assert pctx is not None and pctx["patient_id"] == pids[-1], qscope
        assert not any(n in context for n in icu), qscope
    finally:
        _retire(*pids)


# ── the classifier ───────────────────────────────────────────────────────────
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
    holder = {"rows": []}
    monkeypatch.setattr(database, "get_db", lambda: _FakeDb(holder["rows"]))

    def use(rows):
        holder["rows"] = rows
    return use


def _row(name, ward=None, bed=None):
    return {"name": name, "ward": ward, "bed": bed}


@pytest.mark.parametrize("question", [
    "What is the maximum dose for paracetamol in adults?",
    "Does atrial fibrillation occur often after cardiac surgery?",
    "What is the accuracy of troponin for ruling out MI?",
])
def test_a_general_question_is_no_longer_ward_scoped_phi(table, question):
    table([_row("Nobody Onrecord", "OR", "Z-1"), _row("Noone Elsewhere", "CCU", "Z-2")])
    c = gov.classify_query_sensitivity(question)
    assert "query references a ward on record" not in c.reasons, (question, c.reasons)


@pytest.mark.parametrize("question", [
    "Antibiotics due for anyone back from OR?",
    "Anything to hand over on ward or?",
    "Any new admissions to CCU overnight?",
])
def test_a_real_ward_reference_is_still_phi(table, question):
    table([_row("Nobody Onrecord", "OR", "Z-1"), _row("Noone Elsewhere", "CCU", "Z-2")])
    c = gov.classify_query_sensitivity(question)
    assert c.contains_phi is True, (question, c.reasons)


# ── retrieval, end to end ────────────────────────────────────────────────────
def _user(dept):
    uid = f"WRU{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute("INSERT OR IGNORE INTO users (id, name, role, password_hash, department) "
                   "VALUES (?, 'Ward Rule Doctor', 'doctor', 'x', ?)", (uid, dept))
    return {"id": uid, "role": "doctor", "department": dept, "name": "Ward Rule Doctor"}


def _patient(name, ward, bed, dept):
    pid = f"WRP{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute("INSERT OR IGNORE INTO users (id, name, role, password_hash) "
                   "VALUES ('WRADM', 'Ward Rule Admitter', 'doctor', 'x')")
        db.execute(
            "INSERT INTO patients (id, name, mrn, bed, ward, age, sex, diagnosis, status, admission_date) "
            "VALUES (?, ?, ?, ?, ?, 64, 'M', 'NSTEMI', 'admitted', ?)",
            (pid, name, f"MRN-{uuid.uuid4().hex[:8]}", bed, ward, datetime.now(timezone.utc).isoformat()))
        db.execute("INSERT INTO admissions (patient_id, department, ward, bed, status, admitted_by) "
                   "VALUES (?, ?, ?, ?, 'active', 'WRADM')", (pid, dept, ward, bed))
    return pid


def _retire(*pids):
    with db_session() as db:
        for pid in pids:
            db.execute("UPDATE patients SET name = 'Ward Rule Retired Probe', status = 'discharged' "
                       "WHERE id = ?", (pid,))


@pytest.fixture()
def ccu_ward():
    dept = f"Ward Rule Coronary {uuid.uuid4().hex[:4]}"
    names = ["Zorawar Kulandaivelu", "Yashodhara Ponnambalam"]
    pids = [_patient(n, "CCU", f"CCU-0{i + 1}", dept) for i, n in enumerate(names)]
    yield {"user": _user(dept), "names": names, "pids": pids}
    _retire(*pids)


@pytest.mark.parametrize("question", [
    "Does atrial fibrillation occur often after cardiac surgery?",
    "What is the accuracy of troponin for ruling out MI?",
])
def test_a_general_question_names_no_ccu_patient(ccu_ward, question):
    context, *_rest, pctx, qscope = get_relevant_context(question, ccu_ward["user"], history=[])
    assert qscope != "DISAMBIGUATE", qscope
    assert pctx is None
    for name in ccu_ward["names"]:
        assert name not in context


def test_a_real_ccu_reference_still_locates_the_ward(ccu_ward):
    context, *_rest, pctx, qscope = get_relevant_context(
        "Any concerns in CCU tonight?", ccu_ward["user"], history=[])
    assert qscope == "DISAMBIGUATE"
    assert all(name in context for name in ccu_ward["names"])


def test_or_ward_general_question_attaches_no_chart():
    """A ward literally called OR, one patient in it: the squashed test read
    "or" inside "amoxicillin or cefuroxime" and attached that patient's chart
    to a general question."""
    dept = f"Ward Rule Theatre {uuid.uuid4().hex[:4]}"
    pid = _patient("Thangavelu Ramanathapuram", "OR", "OR-1", dept)
    user = _user(dept)
    try:
        *_, pctx, qscope = get_relevant_context(
            "Should I give amoxicillin or cefuroxime for cellulitis?", user, history=[])
        assert pctx is None or pctx["patient_id"] != pid, qscope
        *_, pctx, qscope = get_relevant_context(
            "Is the patient back from OR stable?", user, history=[])
        assert pctx is not None and pctx["patient_id"] == pid, qscope
    finally:
        _retire(pid)


@pytest.mark.parametrize("ward,general,reference", [
    ("Surgery", "Pain after cardiac surgery, what analgesia is best?",
     "Who is on Surgery tonight?"),
    ("Recovery", "How long is recovery after a hip replacement?",
     "Anyone still in Recovery?"),
    ("General Medicine", "Is this a general medicine or a surgical problem?",
     "Any concerns on General Medicine tonight?"),
    ("Care of the Elderly", "What does care of the elderly involve in India?",
     "Anyone on Care of the Elderly deteriorating?"),
    ("6", "Should I give 6 units of insulin for a glucose of 18?",
     "Who is on ward 6 tonight?"),
])
def test_a_word_coded_ward_needs_a_sign(ward, general, reference):
    """Ward codes are free text in the bed master. A hospital that codes a ward
    "Surgery" used to get that ward's patient for any question about surgery."""
    dept = f"Ward Rule Word {uuid.uuid4().hex[:4]}"
    name = "Kulasekarapandian Muthuswamy"
    pid = _patient(name, ward, f"WW-{uuid.uuid4().hex[:3]}", dept)
    user = _user(dept)
    try:
        context, *_rest, pctx, qscope = get_relevant_context(general, user, history=[])
        assert pctx is None or pctx["patient_id"] != pid, qscope
        assert name not in context, qscope
        context, *_rest, pctx, qscope = get_relevant_context(reference, user, history=[])
        assert name in context, qscope
        assert pctx is None or pctx["patient_id"] == pid, qscope
    finally:
        _retire(pid)


def test_history_anchor_reads_the_same_ward_rule():
    """The anchor walks earlier user turns through _match_patients, so it had
    the same hole: one CCU patient, an earlier general question containing
    "accuracy", and "what are his vitals?" was answered from that chart."""
    dept = f"Ward Rule Anchor {uuid.uuid4().hex[:4]}"
    pid = _patient("Venugopalan Chidambaranathan", "CCU", "CCU-07", dept)
    user = _user(dept)
    try:
        history = [{"role": "user", "content": "What is the accuracy of troponin for ruling out MI?"},
                   {"role": "assistant", "content": "High-sensitivity troponin is very accurate."}]
        *_, pctx, qscope = get_relevant_context("what are his vitals?", user, history=history)
        assert pctx is None or pctx["patient_id"] != pid, qscope
    finally:
        _retire(pid)


def test_retrieval_and_classifier_agree_on_wards(table):
    """Parity: the two lanes read one function, with the text as typed. A
    patient whose name and bed cannot match isolates the ward decision."""
    mismatches = []
    for label, text, _expected in WARD_TABLE:
        if not label:
            continue
        row = {"id": "WRPAR", "name": "Qqqxzv Wwwxzv", "ward": label, "bed": None}
        tl = text.lower()
        retrieval = bool(sage._match_patients(
            [row], [], tl, sage._name_tokens(tl), "", query=text))
        table([_row(row["name"], label, None)])
        classifier = gov._match_patient_identifiers(gov._fold_homoglyphs(tl), include_ward=True, query=text)
        if retrieval != classifier:
            mismatches.append((label, text, retrieval, classifier))
    assert not mismatches, mismatches


# ── the route's label follows the prompt ─────────────────────────────────────
@pytest.fixture(scope="module")
def client():
    from routes.sage_routes import router as sage_router
    app = FastAPI()
    app.include_router(sage_router)
    return TestClient(app)


def test_a_which_patient_turn_is_labelled_phi(client, monkeypatch):
    """A misspelt name that fuzzily matches two patients: retrieval lists both
    (DISAMBIGUATE) with no chart, and the classifier, reading exact words, said
    general_knowledge. The label must describe what the model was given."""
    dept = f"Ward Rule Label {uuid.uuid4().hex[:4]}"
    pids = [_patient("Thiruvengadam Iyer", "WL-1", "WL-11", dept),
            _patient("Thiruvengadam Rao", "WL-1", "WL-12", dept)]
    user = _user(dept)
    seen = {}

    def _capture(query, context, history, scope, **kw):
        seen["context"] = context
        seen["classification"] = kw.get("classification")

        async def _gen():
            route_info = kw.get("route_info")
            if route_info is not None:
                route_info.update({"provider": "stub", "model": "stub"})
            yield "ok"
        return _gen()

    monkeypatch.setattr("routes.sage_routes.query_ollama", _capture)
    try:
        headers = {"Authorization": f"Bearer {create_token(user['id'], 'doctor')}"}
        r = client.post("/api/sage/query", headers=headers,
                        json={"query": "Any update on thiruvengadem today"})
        assert r.status_code == 200, r.text
        assert "Thiruvengadam Iyer" in seen["context"] and "Thiruvengadam Rao" in seen["context"]
        c = seen["classification"]
        assert c is not None and c.contains_phi is True, c and c.reasons
        assert any("patient rows in the prompt" in r for r in c.reasons), c.reasons
    finally:
        _retire(*pids)
