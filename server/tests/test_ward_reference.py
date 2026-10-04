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
    ("General Medicine", "who is on general medicine today", True),
    ("General Medicine", "general practice and medicine", False),
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
    # nothing names an empty label
    ("", "anything at all", False),
    (None, "anything at all", False),
]


@pytest.mark.parametrize("label,text,expected", WARD_TABLE)
def test_ward_rule_table(label, text, expected):
    assert ward_named_in(label, text) is expected, (label, text)


def test_lower_cased_text_still_finds_every_ward_but_capitals():
    """A caller holding only the lower-cased question still finds every ward,
    and "ward or"; only the capitals half needs the text as typed."""
    assert ward_named_in("CCU", "ccu census") is True
    assert ward_named_in("OR", "who is on ward or today") is True
    assert ward_named_in("OR", "patient back from or") is False


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
