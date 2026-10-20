"""Sage's pre-model order gate reads the ways a ward round actually types an order.

`detect_mutation_intent` decides, before any model runs, that a turn is an order:
the turn is then buffered and the action-state override shows a drafted request
or the unsupported-action message instead of the model's prose. Until 9 October
2026 it read only orders whose verb came first, so "bed 13 give lasix 40 iv",
"inj lasix 40 iv stat" and "lasix 40 iv de do" went to the model ungated, and a
model that narrated the order ("Done. Furosemide 40 mg IV has been given and the
order is placed.") was shown as written. The corpus and Samuel's three rulings of
that day are in tests/order_gate_corpus.py.
"""
import json
import time
from unittest import mock

import pytest

from services.sage_service import detect_mutation_intent
from tests import order_gate_corpus as c


def _rows(families):
    return [(fam, q) for fam, rows in families.items() for q in rows]


# ── Orders ────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("family,query", _rows(c.HAND_ORDERS))
def test_hand_built_orders_reach_the_gate(family, query):
    assert detect_mutation_intent(query) is True, f"[{family}] {query!r}"


@pytest.mark.parametrize("query", c.REVIEWER_ORDERS)
def test_the_reviewers_ward_round_orders_reach_the_gate(query):
    assert detect_mutation_intent(query) is True


@pytest.mark.parametrize("query", c.POLITE_QUESTION_MARK_ORDERS)
def test_a_polite_order_with_a_question_mark_is_still_an_order(query):
    """Ruling 3 keeps the request marker ahead of the question mark."""
    assert detect_mutation_intent(query) is True


# ── Reads ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("family,query", _rows(c.HAND_READS))
def test_hand_built_reads_are_not_orders(family, query):
    assert detect_mutation_intent(query) is False, f"[{family}] {query!r}"


@pytest.mark.parametrize("query", c.REVIEWER_QUESTIONS)
def test_the_reviewers_realistic_questions_are_not_orders(query):
    assert detect_mutation_intent(query) is False


@pytest.mark.parametrize("query", c.QUESTION_MARK_PROPOSALS)
def test_a_proposal_ending_in_a_question_mark_is_a_question(query):
    """Ruling 3, 9 October 2026: "start metoprolol 25?" asks for judgement."""
    assert detect_mutation_intent(query) is False


@pytest.mark.parametrize("query", c.NOT_ORDERS_BY_RULING + c.REVIEWER_WORK_REQUESTS)
def test_continue_plans_and_work_requests_stay_outside_the_gate(query):
    assert detect_mutation_intent(query) is False


def test_one_report_word_stops_a_prescription_line():
    """Every verbless order line, wrapped in a report or a question, is a read.

    Ruling 2 makes a bare line an order; this is the bound on it. A status line
    carries a word the line has no use for ("on", "given", "running", "day 3"),
    and that word alone must be enough.
    """
    lines = (c.HAND_ORDERS["drug-first, no verb"] + c.HAND_ORDERS["bed-first, no verb"])
    tripped = [w.format(x=x) for x in lines for w in c.REPORT_WRAPPERS
               if detect_mutation_intent(w.format(x=x))]
    assert tripped == [], f"{len(tripped)} of {len(lines) * len(c.REPORT_WRAPPERS)}: {tripped[:10]}"


# ── The blind sets ────────────────────────────────────────────────────────────
# Rows the gate is known to miss are listed, so a row it used to catch and now
# misses fails here; a known miss it starts catching is reported, not failed.

BLIND_SETS = sorted(int(name[len("HELDOUT")]) for name in dir(c)
                    if name.startswith("HELDOUT") and name.endswith("_ORDERS"))


@pytest.mark.parametrize("n", BLIND_SETS)
def test_blind_orders_caught_stay_caught(n):
    orders, known = getattr(c, f"HELDOUT{n}_ORDERS"), getattr(c, f"KNOWN_MISSES_HELDOUT{n}")
    missed = [q for q in orders if not detect_mutation_intent(q)]
    assert sorted(set(missed) - set(known)) == []


@pytest.mark.parametrize("n", BLIND_SETS)
def test_blind_reads_are_not_orders(n):
    reads, known = getattr(c, f"HELDOUT{n}_READS"), getattr(c, f"KNOWN_TRIPS_HELDOUT{n}")
    tripped = [q for q in reads if detect_mutation_intent(q)]
    assert sorted(set(tripped) - set(known)) == []


def test_every_brand_the_name_matcher_knows_is_a_drug_to_the_gate():
    """sage_service._BRAND_FLOOR lists brands that are never a patient's name.
    Each is a drug, so the gate must read "stop the glycomet" as an order."""
    from services import sage_service
    unknown = sorted(b for b in sage_service._BRAND_FLOOR if not sage_service._is_gate_drug(b))
    assert unknown == []
    for brand in sorted(sage_service._BRAND_FLOOR):
        assert detect_mutation_intent(f"stop the {brand}") is True, brand


@pytest.mark.parametrize("query,order", [
    # The informational "give" (adopted from the 12 October pass, tested in
    # tests/test_sage_scope_budget.py) read with the ward wording: a summary is
    # still a read after a bed reference, and an order after the summary is
    # still an order in every shape the order gate reads.
    ("bed 12 give a concise overview", False),
    ("bed 12: give a brief summary of her meds", False),
    ("give a summary of lasix 40 iv bd", False),
    ("bed 12 give a summary and give lasix 40 iv", True),
    ("give a summary, then lasix 40 iv stat for bed 12", True),
    ("give an overview, inj lasix 40 iv stat", True),
    ("give a concise overview, bed 12 lasix 40 iv de do", True),
])
def test_an_informational_give_beside_ward_wording(query, order):
    assert detect_mutation_intent(query) is order


@pytest.mark.parametrize("query,order", [
    # Decided 20 October 2026: a "Plan:" or "Rx:"
    # line that names a medicine action is an order, as it would be without the
    # header. On a ward the plan is the day's order list, and the costs are
    # lopsided: gated, Sage says no chart change was made or drafts a request
    # for approval; ungated, the model can say the drug was started. A plan with
    # no medicine action, and a plan asked as a question, stay reads.
    ("Plan: start inj ceftriaxone 2 g iv od", True),
    ("Plan: inj ceftriaxone 2 g iv od", True),
    ("plan: lasix 40 iv bd", True),
    ("plan - inj pan 40 iv od, tab doxy 100 bd", True),
    ("Plan: stop metformin", True),
    ("plan: switch to oral antibiotics tomorrow", True),
    ("Plan: bed 12 lasix 40 iv stat", True),
    ("Rx: inj monocef 1 g iv bd", True),
    ("rx: tab dolo 650 sos", True),
    ("orders: ns 100 ml/hr", True),
    ("Plan: continue same", False),
    ("plan: continue current meds", True),
    ("Plan: review in the evening", False),
    ("plan: discharge tomorrow", False),
    ("plan: repeat cbc and rft", False),
    ("Plan: ct head today", False),
    ("rx: as before", False),
    ("Plan: start inj ceftriaxone 2 g iv od?", False),
    ("plan to start insulin if sugars stay high", False),
    ("plan for her?", False),
    ("home meds: metformin 500 bd, amlodipine 5 od", False),
])
def test_a_plan_line_is_read_like_the_order_under_it(query, order):
    assert detect_mutation_intent(query) is order


@pytest.mark.parametrize("query,order", [
    # Ruled 20 October 2026 (Samuel): "we'll add" is read like a plan line. A
    # first-person statement of what the team will do to a medicine is an order;
    # the singular ("I'll start ...") and Indian English "we shall" carry the
    # same risk. Advice, a hypothetical, someone else's plan, the past, a
    # question and anything without a medicine action stay reads.
    ("we'll add spiro 25 od", True),
    ("we will add spironolactone 25 mg od", True),
    ("we shall start inj ceftriaxone 1 g iv od", True),
    ("we'll stop the heparin before surgery", True),
    ("we'll stop the metformin since her creatinine is rising", True),
    ("we'll increase lasix to 40 bd", True),
    ("we're going to start pip-taz 4.5 g q8h", True),
    ("we are going to switch to oral antibiotics tomorrow", True),
    ("we'll also add pantoprazole 40 iv od", True),
    ("we'll start her on insulin sliding scale", True),
    ("we will transfuse 2 units prbc", True),
    ("we’ll hold the ramipril tonight", True),
    ("I'll start ceftriaxone 1 g iv od", True),
    ("i will add pcm 1 g iv q6h", True),
    ("we should stop the heparin soon", False),
    ("we would add spiro if k is low", False),
    ("they'll start ceftriaxone in the icu", False),
    ("we added spiro yesterday", False),
    ("we've started ceftriaxone", False),
    ("we have started ceftriaxone this morning", False),
    ("we'll add spiro?", False),
    ("should we add spiro?", False),
    ("will we need to stop metformin", False),
    ("we'll see how she does", False),
    ("we'll review in the evening", False),
    ("we'll discuss adding spiro with the consultant", False),
    ("we'll start the round from bed 1", False),
    ("we'll update the family after the round", False),
    ("we'll give the family an update", False),
    ("we'll give a summary at handover", False),
    ("we'll repeat the potassium at 6 pm", False),
    ("we'll continue lasix 40 bd", True),
    ("we'll check the potassium", False),
    ("i'll check the drug chart", False),
    # Found by blind set 5 (20 October): a conditional with a hypothetical or
    # someone else's plan is still a read, and a first-person one is an order.
    ("if k drops below 3.5 we would start kcl", False),
    ("if map drops again we would add vasopressin", False),
    ("if she spikes again they will add vanco", False),
    ("if bp is high they'll add amlodipine", False),
    ("if sugars stay high we should start insulin", False),
    ("if k drops below 3.5 we will start kcl", True),
    ("if k drops below 3.5 start kcl 20 mmol iv", True),
    ("well, the lasix seems to be working", False),
])
def test_a_we_will_line_is_read_like_a_plan_line(query, order):
    assert detect_mutation_intent(query) is order


@pytest.mark.parametrize("query,order", [
    # Ruled 20 October 2026 (Samuel): "continue lasix" is read like a plan line.
    # Continuing a named medicine, or the medicines as a group ("continue home
    # meds"), is an order; "continue" with no medicine straight after it, a
    # question and the past stay reads.
    ("continue lasix 40 bd", True),
    ("continue inj ceftriaxone 1 g iv bd", True),
    ("cont iv fluids at 100 ml/hr", True),
    ("cont. tab ecosprin 75 od", True),
    ("continue home meds", True),
    ("continue all meds", True),
    ("continue the antibiotics", True),
    ("cont same antibiotics", True),
    ("cont nebs", True),
    ("continue with pantop 40 iv od", True),
    ("metformin: continue", True),
    ("lasix 40 iv bd, continue", True),
    ("bed 12 continue lasix 40 bd", True),
    ("Plan: continue inj meronem 1 g iv tds", True),
    ("continue lasix 40 bd for now", True),
    ("continue same", False),
    ("continue monitoring", False),
    ("continue to monitor urine output after lasix", False),
    ("continue lasix?", False),
    ("should we continue lasix", False),
    ("how long to continue ceftriaxone for pyelo", False),
    ("continued on lasix", False),
    ("lasix 40 iv bd continued", False),
    ("she continues to need oxygen", False),
    ("contd", False),
    # Found by blind set 5 (20 October): monitoring is not a medicine, even on a
    # line with a frequency that would pass for a dosing signature.
    ("continue vitals 2 hourly", False),
    ("continue obs 4 hourly", False),
    ("cont grbs 6 hourly", False),
    ("continue i/o charting", False),
])
def test_a_continue_line_is_read_like_a_plan_line(query, order):
    assert detect_mutation_intent(query) is order


@pytest.mark.parametrize("query,order", [
    # 20 October 2026: the drug classes and wordings blind set 5 missed, each
    # added as a family rather than as the one sentence (so set 5 is no longer
    # blind). Typos stay unread on purpose.
    ("hold the beta blocker, pt in shock", True),
    ("stop the ace inhibitor, creatinine rising", True),
    ("hold calcium channel blockers today", True),
    ("hold all oral hypoglycemics till she is eating", True),
    ("stop the ppi", True),
    ("skip tonight's statin", True),
    ("continue antiepileptics", True),
    ("continue ATT", True),
    ("we shall start akt-4 tomorrow morning", True),
    ("start anti tb treatment", True),
    ("stop ivf, taking orally well", True),
    ("inj tranexamic acid 1 g iv tds", True),
    ("inj polymyxin b 25 lakh units loading then 12.5 lakh units bd", True),
    ("inj ampicillin 50 mg/kg iv q8h and genta 4 mg/kg od", True),
    ("inj iron sucrose 200 mg in 100 ml ns alternate days x 5 doses", True),
    ("ffp 2 units before the cvc insertion", True),
    ("after the culture is sent start meropenem 1 g q8h", True),
    ("no metformin from today", True),
    ("tab telma 40 subah ek", True),
    ("Rx: tab azee 500 od x3d, tab pcm 650 sos", True),
    ("plan for tonight: inj lasix 40 iv bd", True),
    ("will start inj meropenem from now", True),
    ("sister can you put a neb duolin for bed 2?", True),
    # Reads beside the new words.
    ("is she on a beta blocker", False),
    ("beta blockers in heart failure", False),
    ("statin intolerance", False),
    ("on att for 2 months", False),
    ("att completed last year", False),
    ("after the transfusion her hb rose", False),
    ("no metformin since yesterday", False),
    ("plan for tonight: review the echo", False),
    ("will review in the evening", False),
    ("subah bp 90 tha", False),
    ("alternate day dressing", False),
    ("ivf conception, 32 weeks", False),
    ("polymyxin b resistant klebsiella", False),
    ("if we give genta will her creatinine rise", False),
    ("pan 40 dose", False),
])
def test_the_classes_and_wordings_set_5_missed(query, order):
    assert detect_mutation_intent(query) is order


# ── Cost ──────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "lasix 40 iv bd " * 35000,
    "the patient is stable and comfortable today " * 12000,
    "bed 13 " + "give lasix 40 iv and " * 25000,
])
def test_a_512_kb_message_costs_no_more_than_the_old_gate_did(text):
    """The gate runs on the event loop. The old gate took up to ~0.2 s on 512 KB
    here; the new checks are length-capped, so they add little on top of it."""
    t = time.perf_counter()
    detect_mutation_intent(text)
    assert time.perf_counter() - t < 1.0


# ── End to end: the narration no longer reaches the screen ───────────────────

NARRATION = "NOACT|Done. Furosemide 40 mg IV has been given and the order is placed."


def _narrating_model(prompt, context="", conversation_history=None, scope="simple", **kwargs):
    """A model that ignores the order contract and says it gave the drug."""
    async def gen():
        info = kwargs.get("route_info")
        if info is not None:
            info.update(lane="local", provider="ollama", model="test")
        yield NARRATION
    return gen()


def _shown(resp_text):
    events = []
    for frame in resp_text.split("\n\n"):
        frame = frame.strip()
        if frame.startswith("data: "):
            try:
                events.append(json.loads(frame[6:]))
            except json.JSONDecodeError:
                pass
    return [e["content"] for e in events if "content" in e]


def _no_patient_found(query, user, patient_id=None, history=None, *a, **kw):
    """Retrieval with nobody matched. The route refuses a bed that another test
    left in another department before any model runs ("That patient is outside
    your department"), which is right, but is not what this test is about."""
    return ("", "simple", [], "high", False, None, "GENERAL_MEDICAL")


@pytest.mark.parametrize("query", [
    "bed 13 give lasix 40 iv",
    "13 give lasix 40 iv",
    "bed 12: lasix 40 iv stat",
    "lasix 40 iv bd for bed 12",
    "inj lasix 40 iv stat",
    "lasix 40 iv de do",
])
def test_a_narrated_order_is_replaced_by_the_safe_message(query):
    from fastapi.testclient import TestClient
    with mock.patch("config.INGESTION_MODE", "hybrid"):
        from main import app
    import routes.sage_routes as sage_routes
    from services.helix_db import init_helix_db

    init_helix_db()
    client = TestClient(app)
    token = client.post("/api/auth/login",
                        json={"employee_id": "D001", "password": "password"}).json()["token"]
    with mock.patch.object(sage_routes, "query_ollama", _narrating_model), \
         mock.patch.object(sage_routes, "get_relevant_context", _no_patient_found):
        r = client.post("/api/sage/query", headers={"Authorization": f"Bearer {token}"},
                        json={"query": query})
    assert r.status_code == 200, r.text
    shown = _shown(r.text)
    assert shown == [sage_routes.CHART_MUTATION_SAFE_MESSAGE], shown
