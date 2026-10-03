"""Thinking mode — the effort switch, and the floor that overrides it.

Four properties are pinned here, in the order they matter:

  1. THE DEFAULT IS ON. Nothing ambiguous routes a clinician to the fast model.
  2. THE FLOOR FIRES on patient-specific and dosing/medication questions even
     when the clinician asked for fast — including the four measured failures
     that motivated it.
  3. THE ENV VAR REALLY DISABLES IT, proved through a fresh interpreter with
     ORB_FAST_MODE_SAFETY_FLOOR=0 in the environment, not by patching a symbol.
     A test that only patches the module constant cannot tell a working env var
     from a typo'd one.
  4. THE ANSWER SAYS WHICH MODEL PRODUCED IT, in the dict the Sage route ships
     to the client as `metadata.provider`.
"""
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from services import thinking_mode as tm

SERVER_DIR = Path(__file__).resolve().parent.parent

# Distinct fake tiers, so a test can never pass because both names happened to
# be the same string on this box.
BIG = "big:27b"
SMALL = "small:4b"


@pytest.fixture(autouse=True)
def _two_tiers(monkeypatch):
    """Give every test a real, distinct fast tier and an armed floor.

    Also RESETS the per-request ContextVar. pytest runs the whole module in one
    context, so a test that calls `begin_turn(False)` would otherwise leave
    thinking OFF for every test after it — and the tests that would then break
    are exactly the ones asserting the default is ON. A leaked flag is the same
    bug in production and in the suite.
    """
    monkeypatch.setattr(tm, "LLM_MODEL", BIG)
    monkeypatch.setattr(tm, "THINKING_FAST_MODEL", SMALL)
    monkeypatch.setattr(tm, "THINKING_FAST_MODE_ENABLED", True)
    monkeypatch.setattr(tm, "THINKING_DEFAULT_ON", True)
    monkeypatch.setattr(tm, "FAST_MODE_SAFETY_FLOOR", True)
    token = tm._thinking_flag.set(None)
    yield
    tm._thinking_flag.reset(token)


# ═════════════════════════════════════════════════════════════════════
#  1. Default is thinking-ON
# ═════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("raw", [None, "", "maybe", "  ", {}, [], "TRUE", "yes", True, 1])
def test_ambiguous_or_true_flag_resolves_to_thinking_on(raw):
    """Only an explicit falsy value buys the fast model."""
    assert tm.resolve_thinking_flag(raw) is True


@pytest.mark.parametrize("raw", [False, 0, "false", "0", "off", "no", "fast", "FAST"])
def test_only_explicit_false_turns_thinking_off(raw):
    assert tm.resolve_thinking_flag(raw) is False


def test_default_turn_with_no_flag_goes_to_the_careful_model():
    """A request body with no `thinking` key at all — today's clients."""
    tm.begin_turn(None)
    d = tm.decide("what is the pathophysiology of ARDS")
    assert d.tier == tm.TIER_CAREFUL
    assert d.model == BIG
    assert d.requested_thinking is True
    assert d.floor_fired is False


def test_caller_that_never_began_a_turn_gets_the_careful_model():
    """Background callers (narrator, summarisers) never touch begin_turn. They
    must not be moved onto the fast model by this feature existing."""
    assert tm.current_thinking_flag() is True
    assert tm.decide("summarise the ward").tier == tm.TIER_CAREFUL


def test_operator_can_flip_the_default_off():
    """ORB_THINKING_DEFAULT=off is honoured — but only for the DEFAULT; the
    floor still stands above it (asserted separately below)."""
    tm.THINKING_DEFAULT_ON = False  # module constant, restored by the fixture
    try:
        assert tm.resolve_thinking_flag(None) is False
    finally:
        tm.THINKING_DEFAULT_ON = True


def test_explicit_fast_on_a_safe_question_actually_gets_the_fast_model():
    """The toggle is not decorative: a general, drug-free, patient-free
    question with thinking OFF really does route to the small model."""
    d = tm.decide("what does hypoxia mean", thinking=False)
    assert d.tier == tm.TIER_FAST
    assert d.model == SMALL
    assert d.floor_fired is False


# ═════════════════════════════════════════════════════════════════════
#  2. The floor fires — including on the four measured failures
# ═════════════════════════════════════════════════════════════════════
PATIENT_QUESTIONS = [
    "what are this patient's latest labs",
    "summarise this chart",
    "what is his creatinine trend",
    "is she improving",
    "what happened to them overnight",
    "obs for bed 4",
]


@pytest.mark.parametrize("q", PATIENT_QUESTIONS)
def test_floor_fires_on_patient_specific_questions(q):
    d = tm.decide(q, thinking=False)
    assert d.floor_fired is True
    assert "patient_specific" in d.floor_rules
    assert d.tier == tm.TIER_CAREFUL
    assert d.model == BIG


def test_floor_fires_when_a_patient_id_is_attached_even_to_neutral_words():
    """The words carry no patient signal at all; the request does."""
    d = tm.decide("what does that mean", patient_id="P001", thinking=False)
    assert "patient_specific" in d.floor_rules
    assert d.tier == tm.TIER_CAREFUL


def test_floor_fires_on_sticky_anchored_followup_via_the_assembled_chart():
    """The killer case: no patient_id, no pronoun, no name — retrieval attached
    the chart anyway (sticky anchoring). The prompt is the only evidence."""
    context = "PATIENT: Test Patient, 61y/M, Bed 4, Ward: ICU, Diagnosis: sepsis"
    d = tm.decide("what is driving that", context=context, thinking=False)
    assert "patient_specific" in d.floor_rules
    assert d.tier == tm.TIER_CAREFUL


def test_floor_fires_when_the_governance_classifier_says_phi():
    class _Classification:
        contains_phi = True
        query_class = "patient_scoped"

    d = tm.decide("what is driving that", classification=_Classification(), thinking=False)
    assert "patient_specific" in d.floor_rules


DOSING_QUESTIONS = [
    # ── the four MEASURED failures of medgemma:4b ──
    "how do you reverse warfarin",           # answered "protamine" (reverses heparin)
    "what is the fluid bolus for sepsis",    # answered 4 mL/kg; correct is 30 mL/kg
    "what dose of amoxicillin for pneumonia",
    "how much paracetamol can I give",
    # ── ordinary medication traffic ──
    "what antibiotic covers pseudomonas",
    "how do statins work",
    "what is the mechanism of action of beta blockers",
    "when would you start an ace inhibitor",
    "gentamicin levels",
    "is 4 mg of ondansetron ok",
    "titrate the noradrenaline",
    "40 mg bd or 20 mg tds",
]


@pytest.mark.parametrize("q", DOSING_QUESTIONS)
def test_floor_fires_on_dosing_and_medication_questions(q):
    d = tm.decide(q, thinking=False)
    assert d.floor_fired is True, f"floor did not fire on: {q!r}"
    assert "dosing_or_medication" in d.floor_rules
    assert d.tier == tm.TIER_CAREFUL
    assert d.model == BIG


def test_floor_fires_on_a_bare_quantity_with_no_drug_named():
    """The sepsis-bolus failure names no drug — a unit and a number is enough."""
    assert "dosing_or_medication" in tm.classify_floor("is 4 ml/kg enough")
    assert "dosing_or_medication" in tm.classify_floor("500 mcg")


def test_floor_fires_on_a_drug_name_with_no_dose_word():
    """The protamine/warfarin failure carries no dose at all."""
    assert "dosing_or_medication" in tm.classify_floor("protamine and heparin")


def test_floor_fires_on_orb_computed_scores():
    """Orb computes NEWS2 deterministically; the 4B fabricated a trigger that
    contradicted it. A wrong answer here discredits the score that was right."""
    d = tm.decide("why did NEWS2 escalate", thinking=False)
    assert "orb_computed_score" in d.floor_rules
    assert d.tier == tm.TIER_CAREFUL


GENERAL_QUESTIONS = [
    "what is sepsis",
    "define tachycardia",
    "what does hypoxia mean",
    "explain the pathophysiology of heart failure",
    "what is the normal range for serum sodium",
    "what is the difference between type 1 and type 2 respiratory failure",
    "hello",
]


@pytest.mark.parametrize("q", GENERAL_QUESTIONS)
def test_floor_does_not_fire_on_general_knowledge(q):
    """The floor must leave a real fast lane behind. If this list starts
    failing, fast mode has become a placebo and the toggle should be removed
    rather than left lying about what it does."""
    assert tm.classify_floor(q) == (), f"floor over-fired on: {q!r}"


def test_floor_is_irrelevant_when_thinking_is_on():
    """Thinking ON already gets the careful model; the floor should not claim
    credit for it — a banner saying 'this needed the careful model' on a turn
    the clinician explicitly asked to be careful is noise."""
    d = tm.decide("what dose of amoxicillin", thinking=True)
    assert d.tier == tm.TIER_CAREFUL
    assert d.floor_fired is False


def test_no_fast_tier_means_no_toggle_and_an_honest_reason():
    """A box where medgemma:4b was never pulled resolves the fast tier to the
    careful model. Say so rather than pretending the toggle worked."""
    tm.THINKING_FAST_MODEL = BIG
    try:
        assert tm.fast_mode_available() is False
        d = tm.decide("what is sepsis", thinking=False)
        assert d.tier == tm.TIER_CAREFUL
        assert d.fast_mode_available is False
        assert "not available" in d.reason
    finally:
        tm.THINKING_FAST_MODEL = SMALL


def test_policy_switch_pins_every_turn_to_the_careful_model():
    tm.THINKING_FAST_MODE_ENABLED = False
    try:
        assert tm.fast_mode_available() is False
        assert tm.decide("what is sepsis", thinking=False).tier == tm.TIER_CAREFUL
    finally:
        tm.THINKING_FAST_MODE_ENABLED = True


# ═════════════════════════════════════════════════════════════════════
#  3. The env var disables the floor — proved through the environment
# ═════════════════════════════════════════════════════════════════════
def test_floor_can_be_disabled_in_process(monkeypatch):
    monkeypatch.setattr(tm, "FAST_MODE_SAFETY_FLOOR", False)
    d = tm.decide("what dose of amoxicillin for a patient in bed 4", thinking=False)
    assert d.floor_fired is False
    assert d.tier == tm.TIER_FAST
    assert d.model == SMALL


def _run_with_env(env_extra: dict) -> str:
    """Import config+thinking_mode in a FRESH interpreter under `env_extra`.

    This is the only way to test an env var that is read at import time. The
    in-process test above proves the branch; this proves the WIRE from
    ORB_FAST_MODE_SAFETY_FLOOR to that branch — a typo in the variable name
    would pass the first test and fail this one.
    """
    script = textwrap.dedent(
        """
        from services import thinking_mode as tm
        # Force a distinct fast tier regardless of what is pulled on this box,
        # so the assertion is about the FLOOR, not about model availability.
        tm.THINKING_FAST_MODEL = "small:4b"
        tm.LLM_MODEL = "big:27b"
        tm.THINKING_FAST_MODE_ENABLED = True
        d = tm.decide("what dose of amoxicillin for the patient in bed 4", thinking=False)
        print(f"floor_enabled={tm.FAST_MODE_SAFETY_FLOOR} "
              f"fired={d.floor_fired} tier={d.tier} model={d.model}")
        """
    )
    env = {
        **os.environ,
        "ORB_DEPLOYMENT_PROFILE": "dev",
        # Do not let a stray ORB_FAST_MODEL from the shell change the tiers.
        "ORB_FAST_MODEL": "small:4b",
        **env_extra,
    }
    out = subprocess.run(
        [sys.executable, "-c", script], cwd=SERVER_DIR, env=env,
        capture_output=True, text=True, timeout=120,
    )
    assert out.returncode == 0, out.stderr
    return out.stdout.strip().splitlines()[-1]


def test_env_var_zero_really_disables_the_floor():
    line = _run_with_env({"ORB_FAST_MODE_SAFETY_FLOOR": "0"})
    assert "floor_enabled=False" in line
    assert "fired=False" in line
    assert "tier=fast" in line


def test_floor_is_armed_by_default_with_no_env_var():
    env = {k: v for k, v in os.environ.items() if k != "ORB_FAST_MODE_SAFETY_FLOOR"}
    out = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(
            """
            from services import thinking_mode as tm
            tm.THINKING_FAST_MODEL = "small:4b"
            tm.LLM_MODEL = "big:27b"
            tm.THINKING_FAST_MODE_ENABLED = True
            d = tm.decide("what dose of amoxicillin", thinking=False)
            print(f"floor_enabled={tm.FAST_MODE_SAFETY_FLOOR} fired={d.floor_fired} tier={d.tier}")
            """
        )],
        cwd=SERVER_DIR,
        env={**env, "ORB_DEPLOYMENT_PROFILE": "dev", "ORB_FAST_MODEL": "small:4b"},
        capture_output=True, text=True, timeout=120,
    )
    assert out.returncode == 0, out.stderr
    line = out.stdout.strip().splitlines()[-1]
    assert "floor_enabled=True" in line
    assert "fired=True" in line
    assert "tier=careful" in line


# ═════════════════════════════════════════════════════════════════════
#  4. The answer says which model produced it
# ═════════════════════════════════════════════════════════════════════
def test_fast_turn_is_labelled_as_fast_in_route_info():
    info = {}
    tm.stamp(info, tm.decide("what is sepsis", thinking=False))
    assert info["thinking"] is False
    assert info["thinking_tier"] == tm.TIER_FAST
    assert info["thinking_model"] == SMALL
    assert info["safety_floor"] is False
    assert "small model" in info["thinking_reason"]


def test_floor_override_is_labelled_and_names_its_reason():
    info = {}
    d = tm.decide("what dose of amoxicillin", thinking=False)
    tm.stamp(info, d, served_model=BIG)
    assert info["thinking_requested"] is False   # the clinician asked for fast
    assert info["thinking"] is True              # they got careful
    assert info["safety_floor"] is True
    assert info["safety_floor_rules"] == ["dosing_or_medication"]
    assert info["safety_floor_unsatisfied"] is False
    assert "careful model" in info["thinking_reason"]
    assert "medication or a dose" in info["thinking_reason"]


def test_floor_that_could_not_be_honoured_is_flagged():
    """The floor demanded the careful model and residency could not supply it
    (the 27B was evicted). The clinician has been told 'this needed the careful
    model' and did not get it — that must be visible, not silent."""
    info = {}
    d = tm.decide("what dose of amoxicillin", thinking=False)
    tm.stamp(info, d, served_model=SMALL)
    assert info["safety_floor"] is True
    assert info["safety_floor_unsatisfied"] is True


def test_thinking_on_turn_is_labelled_as_careful():
    info = {}
    tm.stamp(info, tm.decide("what is sepsis", thinking=True))
    assert info["thinking"] is True
    assert info["thinking_tier"] == tm.TIER_CAREFUL
    assert info["safety_floor"] is False


def test_stamp_keys_cannot_collide_with_the_route_layer():
    """`query_ollama` calls `info.update(lane=, provider=, model=, downgraded=,
    preferred_model=)` immediately after the hook. If a key ever collided, the
    clinician's label would be silently overwritten by the routing label."""
    info = {}
    tm.stamp(info, tm.decide("what is sepsis", thinking=False))
    assert not (set(info) & {"lane", "provider", "model", "downgraded", "preferred_model"})


# ═════════════════════════════════════════════════════════════════════
#  The integration hook itself
# ═════════════════════════════════════════════════════════════════════
async def test_select_provider_returns_a_provider_bound_to_the_chosen_model(monkeypatch):
    """The hook is a drop-in for the two lines it replaces: it must hand back a
    provider that actually speaks to the model it just chose. The bug it
    prevents is `local_provider` (27B) being used for a fast-tier turn."""
    from services import llm_providers

    monkeypatch.setattr(tm, "THINKING_FAST_MODEL", "medgemma:4b")
    monkeypatch.setattr(tm, "LLM_MODEL", llm_providers.local_provider.model)

    info = {}
    choice, provider = await tm.select_provider(
        "what is sepsis", thinking=False, route_info=info)
    assert choice.model == "medgemma:4b"
    assert provider.model == "medgemma:4b"
    assert provider is not llm_providers.local_provider
    assert info["thinking_tier"] == tm.TIER_FAST


async def test_select_provider_uses_the_residency_router_for_careful_turns(monkeypatch):
    """Thinking ON must go through model_router unchanged, so the existing
    "27B evicted -> answer on the ambient tier and say so" behaviour survives."""
    from services import llm_providers, model_router

    called = {}

    async def _fake_select():
        called["yes"] = True
        return model_router.ModelChoice("ambient:4b", True, "big:27b")

    # `select_provider` imports select_chat_model INSIDE the call, so the module
    # attribute is the only patch point that actually intercepts it.
    monkeypatch.setattr(model_router, "select_chat_model", _fake_select)

    info = {}
    choice, provider = await tm.select_provider(
        "what is sepsis", thinking=True, route_info=info)
    assert called == {"yes": True}
    assert choice.downgraded is True
    assert provider.model == "ambient:4b"
    assert info["thinking_tier"] == tm.TIER_CAREFUL
    assert provider is not llm_providers.local_provider


async def test_select_provider_never_sends_a_dosing_question_to_the_fast_model(monkeypatch):
    """End of the whole feature, in one assertion."""
    from services import model_router

    async def _fake_select():
        return model_router.ModelChoice(BIG, False, BIG)

    monkeypatch.setattr(model_router, "select_chat_model", _fake_select)
    tm.begin_turn(False)  # clinician turned thinking OFF
    info = {}
    choice, _provider = await tm.select_provider(
        "how do you reverse warfarin", route_info=info)
    assert choice.model == BIG
    assert info["safety_floor"] is True


def test_provider_for_model_returns_the_singleton_for_the_primary_tier():
    """Patching `local_provider.astream_chat` is how most of this suite fakes
    inference. If the primary tier ever stopped resolving to that object, those
    tests would go on passing while covering nothing."""
    from services import llm_providers

    assert llm_providers.provider_for_model(llm_providers.local_provider.model) is llm_providers.local_provider
    assert llm_providers.provider_for_model(None) is llm_providers.local_provider


def test_provider_for_model_caches_one_handle_per_tier():
    from services import llm_providers

    a = llm_providers.provider_for_model("some-other:4b")
    b = llm_providers.provider_for_model("some-other:4b")
    assert a is b
    assert a.model == "some-other:4b"
    assert a is not llm_providers.local_provider


# ═════════════════════════════════════════════════════════════════════
#  Contract with the two files this change does not own
# ═════════════════════════════════════════════════════════════════════
def test_documented_call_site_still_exists_in_sage_service():
    """The integration note in services/thinking_mode.py tells the integrator to
    replace two exact lines in sage_service.query_ollama. If those lines change
    (or the hook lands), this test is the thing that notices the note is stale.

    It passes in BOTH states — un-integrated (the old lines are there) and
    integrated (the hook is there) — and fails only when neither is true, which
    is precisely when the documentation has rotted.
    """
    src = (SERVER_DIR / "services" / "sage_service.py").read_text()
    pre_integration = "choice = await select_chat_model()" in src
    post_integration = "thinking_mode.select_provider(" in src
    assert pre_integration or post_integration, (
        "Neither the documented pre-integration lines nor the thinking_mode hook "
        "are present in sage_service.query_ollama. Update the INTEGRATION note at "
        "the bottom of services/thinking_mode.py."
    )


# ── Quantity-seeking questions must never reach the fast model ───────────────
# Measured gap, 2026-08-05: "How much fluid in sepsis?" reached the 4B, which
# answers 4 mL/kg where the correct answer is 30 mL/kg. The dose-word rule was
# gated to "how much do/should/of" and the quantity rule matches a NUMBER — but
# a question ASKING for a quantity contains none, so both missed it.

import pytest
from services import thinking_mode as _tm


@pytest.mark.parametrize("query", [
    "How much fluid in sepsis?",
    "How much adrenaline in anaphylaxis?",
    "How many units of insulin?",
    "How fast do I run the noradrenaline?",
    "How quickly should I correct the sodium?",
    "What rate for the infusion?",
    "What volume of Hartmann's?",
    "What concentration should I use?",
])
def test_a_question_asking_for_a_quantity_never_reaches_the_fast_model(query):
    decision = _tm.decide(query, None, None, None, False)
    assert decision.floor_fired, (
        f"{query!r} reached the fast model. Quantity is the single thing the 4B "
        f"is measurably worst at — it answered 4 mL/kg for the sepsis bolus."
    )
    assert decision.model != _tm.THINKING_FAST_MODEL


@pytest.mark.parametrize("query", [
    "Define septic shock.",
    "What is an anion gap?",
    "What causes hyponatraemia?",
    "What is the Glasgow Coma Scale?",
    "How is sepsis diagnosed?",
    "What is atrial fibrillation?",
    "Explain type 1 vs type 2 respiratory failure.",
])
def test_the_broadened_rule_did_not_swallow_ordinary_definitions(query):
    """If the floor catches everything, thinking-OFF is a toggle that does
    nothing — which is worse than not shipping it, because it implies a speed
    choice the clinician is not actually getting."""
    decision = _tm.decide(query, None, None, None, False)
    assert not decision.floor_fired, f"{query!r} was over-blocked by the floor"
