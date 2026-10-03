"""Orb — THINKING MODE: the clinician-facing effort switch, and the floor under it.

One control a doctor understands without a manual: think harder, or answer fast.

    thinking ON  -> medgemma:27b  (config.LLM_MODEL)          — the careful model
    thinking OFF -> medgemma:4b   (config.THINKING_FAST_MODEL) — the fast model

Three properties this module exists to guarantee, in order of importance:

  1. THE DEFAULT IS ON. An absent flag, a `null`, a string nobody parsed, an old
     client that predates the feature — all of it resolves to the careful model.
     There is exactly one way to get the fast model: ask for it explicitly.

  2. THE FLOOR OVERRIDES THE TOGGLE. Some questions go to the careful model even
     with thinking OFF, because the fast model has been measured getting them
     wrong on this appliance in ways that would hurt a patient. See
     `_FLOOR_RULES` below — that comment is the whole justification, and it is
     what you must read before setting ORB_FAST_MODE_SAFETY_FLOOR=0.

  3. THE CLINICIAN IS NEVER UNSURE WHICH BRAIN ANSWERED. Every decision is
     stamped into `route_info`, which the Sage route already ships to the client
     as `metadata.provider`. A fast answer says so; a floor override says so.
     A silent substitution — in either direction — is the failure mode.

WHERE THE DECISION IS MADE. `select_provider()` is the single entry point, and
it is deliberately a drop-in for the two lines it replaces in
`sage_service.query_ollama` (see the integration note at the bottom of this
file). Everything above it — `resolve_thinking_flag`, `classify_floor`,
`decide` — is a pure function of its arguments, so the policy is testable
without an event loop, a GPU, or a database.
"""
from __future__ import annotations

import logging
import re
from contextvars import ContextVar, Token
from dataclasses import dataclass

from config import (
    FAST_MODE_SAFETY_FLOOR,
    LLM_MODEL,
    THINKING_DEFAULT_ON,
    THINKING_FAST_MODE_ENABLED,
    THINKING_FAST_MODEL,
)

logger = logging.getLogger("orb.thinking_mode")

TIER_CAREFUL = "careful"
TIER_FAST = "fast"

# ═════════════════════════════════════════════════════════════════════════════
#  THE SAFETY FLOOR
# ═════════════════════════════════════════════════════════════════════════════
# WHY THIS EXISTS — the measurements, so nobody removes it blindly.
#
# medgemma:4b is the fast tier. On THIS appliance, on Orb's own prompts, it was
# measured (see docs/perf/ and the model-comparison runs recorded in the project
# memory, 2026-07/2026-08) producing the following:
#
#   * WRONG ON 40% OF SIMPLE DRUG/DOSE RECALL. Not vague — confidently wrong,
#     in the shape a clinician would act on. Two of the recorded failures:
#       - it offered PROTAMINE to reverse WARFARIN. Protamine reverses heparin.
#         Warfarin is reversed with vitamin K and PCC. Following that answer
#         means giving a drug that does nothing while the patient keeps
#         bleeding. Note that this question contains NO dose at all — which is
#         why the floor triggers on any MEDICATION question, not just questions
#         with a number in them.
#       - it recommended a 4 mL/kg fluid bolus for sepsis. The correct initial
#         bolus is 30 mL/kg. That is ONE SEVENTH of the fluid, in the condition
#         where under-resuscitation kills. Note this failure carries a UNIT and
#         a NUMBER but no drug name — which is why the floor also triggers on
#         bare dose quantities.
#
#   * FABRICATED PATIENT DATA ON 3 OF 8 CHART TURNS. Invented vitals, invented
#     history. A 37.5% fabrication rate on the exact task ("tell me about this
#     patient") that a ward round runs dozens of times a day. This is why ANY
#     question with a patient in scope goes to the careful model regardless of
#     the toggle — the failure is not "a worse answer", it is a made-up chart.
#
#   * FABRICATED A NEWS2 TRIGGER THAT CONTRADICTED ORB'S OWN LOGIC. Orb computes
#     NEWS2 deterministically (services/vigil_service). A clinician asking Sage
#     "why did this escalate?" and getting an invented threshold does not just
#     get a wrong answer — they lose trust in the deterministic score that was
#     right, which is the one part of Orb that must never be second-guessed.
#     This is the third rule; it is NOT in the founder's original two-rule spec
#     and is called out separately in `_FLOOR_RULES` so it can be reasoned about
#     (and removed) independently.
#
# THE FLOOR IS DELIBERATELY OVER-INCLUSIVE. A false positive costs the clinician
# seconds. A false negative costs them a wrong dose. The asymmetry is not close.
#
# Disable with ORB_FAST_MODE_SAFETY_FLOOR=0 (config.FAST_MODE_SAFETY_FLOOR).
# Doing that on a ward with real patients means accepting the four failures
# above as reachable through the UI.

#: Human-readable name -> one-line reason, shown to the clinician when it fires.
_FLOOR_RULES: dict[str, str] = {
    "patient_specific":
        "this question is about a specific patient",
    "dosing_or_medication":
        "this question involves a medication or a dose",
    "orb_computed_score":
        "this question is about a score Orb computes itself",
    "work_request_imperative":
        "this asks for hospital work to be arranged",
}


# ── Rule 1 signals: a specific, present patient ──────────────────────────────
# Deliberately NOT "the patient" / "a patient" in the abstract: those appear in
# textbook questions ("what are the signs of sepsis in a patient?") and matching
# them would send the entire general-knowledge lane to the careful model, which
# would make the toggle a placebo. What is matched is DEIXIS — language that only
# resolves against a chart that is open.
_PATIENT_DEIXIS = re.compile(
    r"\b("
    r"this patient|that patient|my patient|the patient in|patient in bed|"
    r"this chart|this admission|this man|this woman|this lady|this gentleman|"
    r"he|him|his|she|her|hers|they|them|their|"
    r"bed\s*\d+|bed\s+(?:one|two|three|four|five|six|seven|eight|nine|ten)|"
    r"mrn\s*\w+|"
    r"summari[sz]e (?:this|the) (?:chart|patient|admission)"
    r")\b",
    re.IGNORECASE,
)

# Markers that the assembled prompt actually carries a chart block. The chart
# builder emits `PATIENT: <name>, <age>y/<sex>, Bed ...` as its first line
# (services/sage_service.py, `context["demographics"]`); the no-match notice is
# the other shape a patient-scoped turn can take. Matching the prompt rather
# than only the request is what catches STICKY ANCHORING — a follow-up like
# "what is driving that?" carries no patient_id, no name and no pronoun, but the
# retrieval layer has attached the chart anyway.
_CONTEXT_PATIENT_MARKERS = ("PATIENT:", "NO MATCHING PATIENT")


# ── Rule 2 signals: dosing / medication / drug quantity ──────────────────────
_DOSE_WORDS = re.compile(
    r"\b("
    r"dose|doses|dosage|dosing|redose|re-dose|"
    r"loading dose|maintenance dose|max(?:imum)? dose|daily dose|stat dose|"
    r"bolus|infusion rate|infusion|titrate|titration|taper|tapering|"
    # QUANTITY-SEEKING QUESTIONS, broadened 2026-08-05 after a measured gap.
    # These were gated to `how much do|should|of` and `how many <unit>`, which
    # requires the asker to already use a unit. So "How much fluid in sepsis?"
    # went to the FAST model — and that is the exact question the 4B answered
    # "4 mL/kg" to, when the correct answer is 30 mL/kg. One-seventh of the dose,
    # from the model, on the question a junior doctor is most likely to ask in a
    # hurry. The `_DOSE_QUANTITY` rule below cannot catch it either: that matches
    # a number, and a question ASKING for a quantity contains none.
    #
    # Deliberately broad. "How much/how many/what rate/what volume" in a clinical
    # assistant is a quantity request essentially always, and quantity is the
    # single thing the small model is measurably worst at. A false floor here
    # costs a few seconds; a false pass costs a dosing error.
    r"how much\b|how many\b|how fast\b|how quickly\b|"
    r"what (?:rate|volume|amount|concentration|strength)\b|"

    r"per kg|/kg|"
    r"bd|tds|qds|nocte|prn|"
    r"prescribe|prescribing|prescription|administer|administration|"
    r"medication|medications|medicine|medicines|drug|drugs|"
    r"antibiotic|antibiotics|analgesia|analgesic|sedation|anticoagulation|"
    r"vasopressor|inotrope|thrombolysis|reversal agent|reverse"
    r")\b",
    re.IGNORECASE,
)

# A bare quantity with a clinical unit. This is the rule that catches the
# "4 mL/kg" sepsis-bolus failure, which names no drug at all.
_DOSE_QUANTITY = re.compile(
    r"(?:\b\d+(?:\.\d+)?\s*(?:mg|mcg|µg|ug|g|kg|ml|l|units?|iu|mmol|meq)\b"
    r"|\b(?:mg|mcg|µg|ug|g|ml|units?|iu|mmol|meq)\s*/\s*(?:kg|hr|hour|h|min|minute|day|24h)\b)",
    re.IGNORECASE,
)

# Drug NAMES. Free-text drug mention is a medication question even with no dose
# and no dose-word — the protamine/warfarin failure is exactly that shape.
#
# The list is seeded from the pharmacology layer that already exists
# (services/drug_safety_service), so the two never drift apart, and topped up
# with the ward vocabulary that layer does not need but Sage is asked about.
# Kept LOCAL and static: no DB read, no RxNorm lookup — this runs on the hot
# path of every Sage turn and must cost microseconds, not a query.
_EXTRA_DRUG_TERMS: tuple[str, ...] = (
    # the recorded failures
    "protamine", "vitamin k", "phytomenadione", "pcc", "prothrombin complex",
    "octaplex", "beriplex", "idarucizumab", "andexanet", "naloxone", "flumazenil",
    # resuscitation / critical care
    "adrenaline", "epinephrine", "noradrenaline", "norepinephrine", "dopamine",
    "dobutamine", "vasopressin", "metaraminol", "phenylephrine", "atropine",
    "amiodarone", "adenosine", "digoxin", "magnesium", "calcium gluconate",
    "sodium bicarbonate", "hartmann", "hartmann's", "ringer", "normal saline",
    "0.9% saline", "dextrose", "glucose", "colloid", "crystalloid",
    # endocrine / common wards
    "insulin", "actrapid", "novorapid", "lantus", "metformin", "gliclazide",
    "levothyroxine", "hydrocortisone", "prednisolone", "dexamethasone",
    # cardiac / respiratory
    "furosemide", "frusemide", "bisoprolol", "metoprolol", "atenolol",
    "amlodipine", "spironolactone", "salbutamol", "ipratropium", "theophylline",
    "aminophylline", "gtn", "glyceryl trinitrate", "nitrate", "clopidogrel",
    "ticagrelor", "alteplase", "tenecteplase",
    # antimicrobials not in the class table
    "vancomycin", "teicoplanin", "metronidazole", "ciprofloxacin", "levofloxacin",
    "linezolid", "daptomycin", "colistin", "fluconazole", "acyclovir",
    "aciclovir", "oseltamivir", "doxycycline", "clindamycin", "nitrofurantoin",
    "rifampicin", "isoniazid", "pyrazinamide", "ethambutol",
    # neuro / sedation / analgesia
    "paracetamol", "acetaminophen", "propofol", "midazolam", "lorazepam",
    "diazepam", "haloperidol", "olanzapine", "quetiapine", "phenytoin",
    "levetiracetam", "valproate", "keppra", "ketamine", "dexmedetomidine",
    "rocuronium", "suxamethonium", "succinylcholine", "neostigmine", "sugammadex",
    # gastro / other
    "omeprazole", "pantoprazole", "ondansetron", "metoclopramide", "lactulose",
    "terlipressin", "tranexamic", "protamine sulfate", "allopurinol", "colchicine",
    # DRUG CLASSES BY THEIR SPOKEN NAME. "what is the mechanism of action of beta
    # blockers" names no molecule and carries no dose, and is squarely a
    # medication question — the class a clinician says out loud has to be in the
    # vocabulary or rule 2 has a hole the width of pharmacology teaching.
    "beta blocker", "beta-blocker", "ace inhibitor", "ace-inhibitor",
    "calcium channel blocker", "arb", "angiotensin receptor blocker",
    "statin", "diuretic", "loop diuretic", "thiazide", "ppi",
    "proton pump inhibitor", "ssri", "snri", "tricyclic", "opioid", "opiate",
    "benzodiazepine", "steroid", "corticosteroid", "nsaid", "antiemetic",
    "antihypertensive", "antiplatelet", "anticoagulant", "antipsychotic",
    "antidepressant", "antiarrhythmic", "antifungal", "antiviral", "laxative",
    "bronchodilator", "muscle relaxant", "neuromuscular blocker",
)


def _build_drug_vocabulary() -> frozenset[str]:
    """Union of the pharmacology layer's drug terms and the ward vocabulary above.

    Reads `drug_safety_service` defensively: that module opens a DB session at
    call time but not at import time, and if its private tables are ever renamed
    this must degrade to "slightly smaller list", never to an import error on
    the Sage hot path.
    """
    terms: set[str] = {t.lower() for t in _EXTRA_DRUG_TERMS}
    try:
        from services import drug_safety_service as _dss
        for table_name in ("_DRUG_CLASS_PATTERNS", "_ALLERGEN_CLASS_TERMS"):
            table = getattr(_dss, table_name, None) or {}
            for values in table.values():
                terms.update(str(v).lower() for v in values)
    except Exception:  # pragma: no cover - defensive
        logger.debug("drug vocabulary: pharmacology layer unavailable, using local list")
    # A one-character term would match inside every word once it is put in an
    # alternation with \b guards. None exist today; this keeps it that way.
    return frozenset(t for t in terms if len(t) >= 3)


_DRUG_VOCABULARY = _build_drug_vocabulary()
# Longest-first so "co-amoxiclav" wins over "amoxicillin"-style prefixes and the
# reported match is the specific one. The trailing `s?` is what makes the class
# names work in the plural a clinician actually uses ("statins", "beta
# blockers", "opioids") — without it `\b` after "blocker" refuses to match
# "blockers", and the commonest phrasing of a medication question walks past
# the floor.
_DRUG_NAMES = re.compile(
    r"\b(" + "|".join(re.escape(t) for t in sorted(_DRUG_VOCABULARY, key=len, reverse=True)) + r")s?\b",
    re.IGNORECASE,
)


# ── Rule 4 signals: an instruction to arrange hospital work ──────────────────
# WHY THIS IS A FLOOR RULE AND NOT A MUTATION. `detect_mutation_intent` asks
# "is this an order to change the CHART", and for these it is right to say no:
# Orb cannot book a scan, cancel a phlebotomy round or send a referral, so
# asserting a chart mutation would be false and the widening that would reach
# them is the one `sage_service` warns against above `MUTATION_PATTERNS` (it
# swallows "how do I book a CT?" with them). This rule asserts something
# weaker and true: the clinician asked for WORK, and the fast model answers
# that badly.
#
# MEASURED 2026-09-07, Orb's own prompts, no patient in scope, on the six
# phrasings of docs/perf/FAST_LANE_MARKER_2026-09-07.md section 7b. The count is
# replies that PROMISED to do the work and reached the clinician:
#
#     medgemma:4b    3 of 48 — "Okay, I understand. I will cancel the morning
#                    bloods.", "Okay, I can schedule physiotherapy for tomorrow.
#                    I will add that to the patient's plan." (A fourth claimed
#                    "I've recorded" and was caught by the older patterns.)
#     medgemma:27b   2 of 30 — "Okay, I will record a blood pressure of 130/80
#                    mmHg in the patient's chart.", twice, on that one phrasing.
#                    The other five phrasings were clean 25 of 25.
#
# A clinician who reads "I will cancel the morning bloods" does not cancel the
# morning bloods, and the bloods are still drawn. That is the same shape as the
# three failures above: not a worse answer, a false one that gets acted on.
#
# READ THE 27B NUMBER HONESTLY. It is better, not clean, so this rule improves
# the odds and does not close the hole. The thing that closes it is
# `routes/sage_routes.FORBIDDEN_PHRASE_PATTERNS`, which deletes the sentence
# whichever model produces it — both of those 27B replies included. Do not
# retire that list on the strength of this rule, or the other way round.
#
# THE ANCHOR IS THE WHOLE SAFETY MARGIN. Only a clause-INITIAL verb counts
# (after an optional politeness preamble), because that is what an English
# imperative looks like and it is what separates the order from the question:
# "book an urgent CT head" fires, "how do I book a CT?", "when do you book a CT
# head in head injury?" and "who cancels the morning bloods if the patient is
# fasting?" all begin with an interrogative and do not. Verbs that also head an
# ordinary question ("is", "does") are not here, and neither is any verb the
# mutation gate already locks on ("add", "start", "prescribe", "order") — that
# turn is buffered and rewritten before this ever matters.
_WORK_REQUEST_IMPERATIVE = re.compile(
    r"^\s*(?:(?:please|kindly|just|now)\s+"
    r"|(?:can|could|will|would)\s+(?:you|u)\s+(?:please\s+)?"
    r"|(?:i|we)\s+(?:want|need|would\s+like)\s+(?:you\s+)?to\s+"
    r"|let'?s\s+|go\s+ahead\s+and\s+){0,3}"
    r"(?:book|schedule|reschedule|cancel|refer|arrange|request|chase|"
    r"raise|put\s+in|call|bleep|page|admit|discharge|transfer|consent|"
    r"record|log|organise|organize|set\s+up|send)\b",
    re.IGNORECASE,
)

# ── Rule 3 signals: a score Orb computes deterministically ───────────────────
_ORB_COMPUTED = re.compile(
    r"\b("
    r"news\s*2|news2|national early warning|early warning score|\bews\b|"
    r"escalation (?:trigger|threshold|criteria)|trigger threshold|"
    r"sepsis (?:bundle|six|6)|deterioration score|acuity score|risk score|"
    r"why did (?:orb|vigil|this) (?:escalate|alert|flag|trigger)|"
    r"vigil (?:alert|score|flag)"
    r")\b",
    re.IGNORECASE,
)


# ═════════════════════════════════════════════════════════════════════════════
#  Per-request flag transport
# ═════════════════════════════════════════════════════════════════════════════
# The `thinking` flag arrives on the Sage request body; the model is chosen deep
# inside sage_service.query_ollama, which never sees the body. A ContextVar
# carries it across that gap without threading a new parameter through a file
# this change does not own.
#
# SAFETY OF THE TRANSPORT. Starlette handles each request in its own task, and a
# task copies the context at creation, so a value set in one request cannot be
# read by another. But the failure mode if that ever stopped being true is a
# LEAKED "thinking OFF" — a downgrade nobody asked for. Two guards make that
# harmless: the flag carries ONLY the boolean (never the classification), and
# `select_provider` re-derives the floor from the live prompt in flight, so even
# a leaked OFF still cannot route a patient or dosing question to the 4B.
_thinking_flag: ContextVar[bool | None] = ContextVar("orb_thinking_flag", default=None)

_TRUTHY = {"1", "true", "yes", "on", "think", "thinking", "careful"}
_FALSY = {"0", "false", "no", "off", "fast", "quick"}


def resolve_thinking_flag(raw: object) -> bool:
    """Normalise whatever the client sent into a boolean. UNRECOGNISED -> DEFAULT.

    The default is `config.THINKING_DEFAULT_ON` (ON unless an operator changed
    it). Everything ambiguous lands there: `None`, a missing key, `"maybe"`, a
    number that is not 0/1, a dict. There is no input that accidentally means
    "use the fast model" — that requires an explicit false/0/"off"/"fast".
    """
    if raw is None:
        return THINKING_DEFAULT_ON
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return bool(raw)
    if isinstance(raw, str):
        token = raw.strip().lower()
        if token in _TRUTHY:
            return True
        if token in _FALSY:
            return False
    return THINKING_DEFAULT_ON


def begin_turn(raw: object) -> Token:
    """Record this request's thinking flag. Returns the ContextVar reset token.

    Called once, at the top of the Sage query route, with the raw body value.
    Callers may ignore the token — the context dies with the request task.
    """
    return _thinking_flag.set(resolve_thinking_flag(raw))


def end_turn(token: Token) -> None:
    """Restore the previous flag. Optional; used by tests to keep contexts clean."""
    try:
        _thinking_flag.reset(token)
    except ValueError:  # pragma: no cover - token from another context
        pass


def current_thinking_flag() -> bool:
    """This request's thinking flag, or the configured default if none was set.

    "None was set" is the un-integrated case (a caller that never went through
    `begin_turn`, e.g. the ward narrator or a background summariser). It
    resolves to the default — ON — so nothing outside the Sage chat path is
    silently moved onto the fast model by this feature existing.
    """
    value = _thinking_flag.get()
    return THINKING_DEFAULT_ON if value is None else value


# ═════════════════════════════════════════════════════════════════════════════
#  The decision
# ═════════════════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class ThinkingDecision:
    """What was asked for, what was decided, and why — in words a doctor reads."""

    requested_thinking: bool
    """What the clinician's toggle said, after defaulting."""
    tier: str
    """TIER_CAREFUL or TIER_FAST, which brain this turn is going to."""
    model: str
    """The tier's model name, BEFORE residency routing may substitute it."""
    floor_fired: bool
    """True when the floor overrode a thinking-OFF request."""
    floor_rules: tuple[str, ...] = ()
    """Which rules matched, in `_FLOOR_RULES` order."""
    floor_enabled: bool = True
    """Whether the floor was armed at all (ORB_FAST_MODE_SAFETY_FLOOR)."""
    fast_mode_available: bool = True
    """False when there is no distinct fast tier to route to, or policy pins the
    careful one. The toggle should not be offered when this is False."""
    reason: str = ""
    """One sentence, written for the clinician, not the log."""

    @property
    def thinking(self) -> bool:
        """Did this turn actually get the careful model's effort?"""
        return self.tier == TIER_CAREFUL


def fast_mode_available() -> bool:
    """Is there a real fast lane on this appliance?

    False when an operator pinned the careful model (ORB_FAST_MODE_ENABLED=0) or
    when the fast tier resolved to the same model as the careful one — which is
    what `config._resolve_fast_model` does on a box where medgemma:4b was never
    pulled. Offering a toggle that changes nothing is worse than not offering it.
    """
    return THINKING_FAST_MODE_ENABLED and THINKING_FAST_MODEL != LLM_MODEL


def classify_floor(
    query: str,
    patient_id: str | None = None,
    classification: object | None = None,
    context: str | None = None,
) -> tuple[str, ...]:
    """Which floor rules this turn trips. Pure; safe to call anywhere.

    Every argument is optional and each one only ever ADDS signal, so a caller
    that has less information gets a floor that fires at least as often — never
    less often. The text rules alone are enough to catch the measured failures.
    """
    q = query or ""
    matched: list[str] = []

    # ── Rule 1: a specific patient is in scope ──
    patient_signal = bool(patient_id)
    if not patient_signal and classification is not None:
        # ai_governance.QueryClassification — duck-typed so this module never
        # hard-depends on that class's shape.
        if getattr(classification, "contains_phi", False):
            patient_signal = True
        elif str(getattr(classification, "query_class", "")) in (
            "patient_scoped", "patient_enumeration",
        ):
            patient_signal = True
    if not patient_signal and context:
        patient_signal = any(m in context for m in _CONTEXT_PATIENT_MARKERS)
    if not patient_signal:
        patient_signal = bool(_PATIENT_DEIXIS.search(q))
    if patient_signal:
        matched.append("patient_specific")

    # ── Rule 2: dosing / medication / drug quantity ──
    if _DOSE_WORDS.search(q) or _DOSE_QUANTITY.search(q) or _DRUG_NAMES.search(q):
        matched.append("dosing_or_medication")

    # ── Rule 3: a score Orb computes deterministically ──
    if _ORB_COMPUTED.search(q):
        matched.append("orb_computed_score")

    # ── Rule 4: an instruction to arrange hospital work ──
    if _WORK_REQUEST_IMPERATIVE.match(q.strip()):
        matched.append("work_request_imperative")

    return tuple(matched)


def safety_setting_warnings() -> list[str]:
    """One operator sentence for each Sage safety switch set away from its safe
    default, read now.

    Each of these silences a check with no sign on the ward: the floor off
    sends dosing and patient questions to the small model, and the two history
    switches take away the marker that keeps the model's second opinion on a
    chart request (sage_service._history_mutation_marker). Provenance then
    recorded 'not_fired' as if the floor had looked.
    """
    import os

    out: list[str] = []
    if not FAST_MODE_SAFETY_FLOOR:
        out.append(
            "ORB_FAST_MODE_SAFETY_FLOOR is off: fast mode answers medication, dose "
            "and patient questions on the small model. Read services/thinking_mode.py "
            "before running a ward like this.")
    # Lazy: sage_service is heavy, and these readers are its own, so the
    # warning cannot disagree with what the replay actually does.
    from services.sage_service import (
        SAGE_FAST_MARKER_ACT, _history_mutation_marker, _history_replay_enabled,
    )
    if _history_mutation_marker() != SAGE_FAST_MARKER_ACT:
        out.append(
            "ORB_SAGE_HISTORY_MUTATION_MARKER is %r: a replayed chart request no "
            "longer carries ACT|, which silenced the model's second opinion on a "
            "real chart request 10 times in 10 (docs/perf/FAST_LANE_MARKER_2026-09-07.md)."
            % os.getenv("ORB_SAGE_HISTORY_MUTATION_MARKER", ""))
    if not _history_replay_enabled():
        out.append(
            "ORB_SAGE_HISTORY_MARKER is off: a chart request is replayed as the "
            "model's own narration instead of the safe message the clinician read.")
    return out


def log_safety_setting_warnings() -> list[str]:
    """Log each of `safety_setting_warnings()` at WARNING. Called once at boot
    (routes.sage_routes import). Never raises: a warning must not stop Sage."""
    try:
        warnings = safety_setting_warnings()
    except Exception:  # noqa: BLE001
        logger.exception("could not read the Sage safety settings")
        return []
    for line in warnings:
        logger.warning("SAGE SAFETY SETTING NOT DEFAULT: %s", line)
    return warnings


def _floor_sentence(rules: tuple[str, ...]) -> str:
    reasons = [_FLOOR_RULES[r] for r in rules if r in _FLOOR_RULES]
    if not reasons:
        return "this one needed the careful model"
    if len(reasons) == 1:
        return reasons[0]
    return ", ".join(reasons[:-1]) + " and " + reasons[-1]


def decide(
    query: str,
    patient_id: str | None = None,
    classification: object | None = None,
    context: str | None = None,
    thinking: bool | None = None,
) -> ThinkingDecision:
    """Choose the tier for one turn. Pure — no I/O, no event loop, no GPU.

    `thinking=None` reads this request's flag from the ContextVar (which itself
    falls back to the configured default), so the normal call site passes
    nothing and tests can pass an explicit value.
    """
    requested = current_thinking_flag() if thinking is None else bool(thinking)
    available = fast_mode_available()

    if requested:
        return ThinkingDecision(
            requested_thinking=True, tier=TIER_CAREFUL, model=LLM_MODEL,
            floor_fired=False, floor_enabled=FAST_MODE_SAFETY_FLOOR,
            fast_mode_available=available,
            reason="Thinking mode on, answered by the careful model.",
        )

    if not available:
        # The clinician asked for fast and there is nothing faster to give them.
        # Say so rather than pretending the toggle worked.
        return ThinkingDecision(
            requested_thinking=False, tier=TIER_CAREFUL, model=LLM_MODEL,
            floor_fired=False, floor_enabled=FAST_MODE_SAFETY_FLOOR,
            fast_mode_available=False,
            reason="Fast mode is not available on this appliance, answered by "
                   "the careful model.",
        )

    rules = classify_floor(query, patient_id, classification, context) if FAST_MODE_SAFETY_FLOOR else ()
    if rules:
        return ThinkingDecision(
            requested_thinking=False, tier=TIER_CAREFUL, model=LLM_MODEL,
            floor_fired=True, floor_rules=rules, floor_enabled=True,
            fast_mode_available=True,
            reason=f"Fast mode was on, but {_floor_sentence(rules)}, so this one "
                   f"was answered by the careful model.",
        )

    return ThinkingDecision(
        requested_thinking=False, tier=TIER_FAST, model=THINKING_FAST_MODEL,
        floor_fired=False, floor_enabled=FAST_MODE_SAFETY_FLOOR,
        fast_mode_available=True,
        reason="Fast mode, answered by the small model. Lower accuracy; verify "
               "anything you would act on.",
    )


# ═════════════════════════════════════════════════════════════════════════════
#  route_info stamping — how the clinician finds out
# ═════════════════════════════════════════════════════════════════════════════
def stamp(route_info: dict | None, decision: ThinkingDecision, served_model: str | None = None) -> dict:
    """Write the decision into the dict the Sage route ships as `metadata.provider`.

    `served_model` is the model that will ACTUALLY run the turn, after the
    residency router has had its say. When the floor demanded the careful model
    and residency could not supply it, `safety_floor_unsatisfied` goes True —
    that combination is the one case where the clinician has been told "this
    needed the careful model" and did not get it, and it must not be silent.

    Keys are namespaced under `thinking_*` / `safety_floor_*` so they cannot
    collide with the lane/provider/model keys `query_ollama` writes next.
    """
    info = route_info if route_info is not None else {}
    served = served_model or decision.model
    info.update(
        thinking=decision.thinking,
        thinking_requested=decision.requested_thinking,
        thinking_tier=decision.tier,
        thinking_model=decision.model,
        thinking_available=decision.fast_mode_available,
        thinking_reason=decision.reason,
        safety_floor=decision.floor_fired,
        safety_floor_rules=list(decision.floor_rules),
        safety_floor_enabled=decision.floor_enabled,
        safety_floor_unsatisfied=bool(decision.floor_fired and served != LLM_MODEL),
    )
    return info


# ═════════════════════════════════════════════════════════════════════════════
#  INTEGRATION HOOK
# ═════════════════════════════════════════════════════════════════════════════
async def select_provider(
    query: str,
    context: str | None = None,
    patient_id: str | None = None,
    classification: object | None = None,
    route_info: dict | None = None,
    thinking: bool | None = None,
):
    """Pick the model AND the provider for one Sage turn. The one call site.

    Returns `(ModelChoice, ChatProvider)` — exactly the two values
    `sage_service.query_ollama` binds today, so it is a two-line-for-one-line
    replacement there (see the note at the bottom of this file).

    Residency, by tier:
      * careful tier — delegates to `model_router.select_chat_model()` unchanged,
        so the existing "27B was evicted, answer on the ambient tier and say so"
        behaviour is preserved bit for bit, including `downgraded`.
      * fast tier — uses the fast model directly and never downgrades. It IS the
        small tier; there is nothing smaller to fall back to, and a 3 GB load is
        seconds, not the ~80 s that motivated the residency router.
    """
    from services.llm_providers import provider_for_model
    from services.model_router import ModelChoice, select_chat_model

    decision = decide(query, patient_id, classification, context, thinking)

    if decision.tier == TIER_FAST:
        choice = ModelChoice(decision.model, False, decision.model)
    else:
        choice = await select_chat_model()

    stamp(route_info, decision, served_model=choice.model)
    if decision.floor_fired:
        # Log the RULES, never the query — this line would otherwise carry PHI.
        logger.info(
            "thinking-mode floor fired (%s) — routing to %s instead of %s",
            ",".join(decision.floor_rules), choice.model, THINKING_FAST_MODEL,
        )
    return choice, provider_for_model(choice.model)


# ─────────────────────────────────────────────────────────────────────────────
# INTEGRATION — the two edits this module needs, in files it does not own.
#
# 1) server/routes/sage_routes.py, in `sage_query`, immediately after
#    `effective_patient_id = patient_id or _vc.get(...)` (line 1308 as of
#    2026-08-05). ONE LINE:
#
#        thinking_mode.begin_turn(body.get("thinking"))
#
#    (plus `from services import thinking_mode` at the top of the module.)
#
#    NOT in the /sage/voice handler (its own effective_patient_id is at line
#    2062). That is deliberate: a spoken turn has no visible toggle, so it takes
#    the default — the careful model — until voice grows a control of its own.
#
# 2) server/services/sage_service.py, in `query_ollama`. Replace THESE TWO LINES
#    (1328-1329 as of 2026-08-05):
#
#        choice = await select_chat_model()
#        provider = local_provider if not choice.downgraded else OllamaChatProvider(model=choice.model)
#
#    with THIS ONE:
#
#        choice, provider = await thinking_mode.select_provider(
#            prompt, context, patient_id, classification, route_info=info)
#
#    Every name in that call is already bound at that point in the function. The
#    `info.update(lane=..., provider=..., model=..., downgraded=...,
#    preferred_model=...)` immediately below keeps working unchanged — its keys
#    do not collide with the ones `stamp()` writes.
#
# Until (1) lands, `current_thinking_flag()` returns the configured default (ON)
# and every turn is served by the careful model — i.e. today's behaviour exactly.
# Until (2) lands, this module is inert. Neither edit can fail open.
# ─────────────────────────────────────────────────────────────────────────────
