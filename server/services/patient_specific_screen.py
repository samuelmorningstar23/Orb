"""Orb — STEP A: the deterministic patient-specific pre-screen.

WHAT THIS REPLACES, AND WHY IT HAD TO BE REPLACED
-------------------------------------------------
Tier 2 of the drafted assessment used to produce GENERIC NEXT STEPS. Measured on
the dry-run ward it delivered on 22% of alerts and 0% of a quiet control window,
and when it did arrive two of its three steps restated the alert it was attached
to ("Escalate immediately / Critical NEWS2"). A tier whose output is true of any
patient with those numbers is decoration, and decoration on the busiest screen in
the hospital is worse than nothing: it teaches a clinician to skip the panel, and
they then skip the one line that mattered.

THE RULING: the advice tier says ONLY what depends on THIS patient's own chart —
the things arithmetic over vitals can never surface and a tired clinician might
not cross-reference at 3am. A rate-controlling drug making a reassuring heart
rate misleading. A creatinine that changes the calculus on a standard fluid
bolus. An allergy that rules out the obvious first-line agent. Anticoagulation
before a procedure. A steroid blunting a fever.

THIS MODULE IS THE HALF THAT NEEDS NO MODEL. It pulls the CANDIDATE FACTS —
deterministically, from data Orb already holds — and hands a short list to the
model, which does nothing but judge which of them matter (services/
sage_assessment_service.py, `_build_prompt_facts`). Two consequences follow, and
both are the point:

  * THE MODEL CAN NEVER INVENT A FACT, because it is never shown the chart. It
    is shown a numbered list of facts this screen extracted, and every drug name,
    dose and lab value in its output is checked back against that list
    (:func:`verify_line`). A model naming a comorbidity nobody charted is the
    worst outcome available on this surface.
  * MOST ALERTS COST NO GPU AT ALL. On most patients there is nothing to flag,
    the candidate list is empty, and the caller returns "no patient-specific
    factors identified" instantly without a queue slot, a prompt or a token. That
    is also the throughput fix: the 22%/0% delivery problem was one background
    model slot being asked for more generations than it can physically turn over,
    and the cheapest generation is the one that never happens.

SILENCE IS A FIRST-CLASS ANSWER HERE. Every rule below is written to fire rarely
and specifically. A false flag is not a small cost: it is how a clinician learns
to ignore the tier, which is exactly what killed the last version. Where a rule
had to choose between missing something and firing on a patient it does not
apply to, it misses.

WHAT IS REUSED RATHER THAN REBUILT
----------------------------------
  * ``drug_safety_service.classify_drug`` — the ONE drug→class vocabulary. Every
    class this screen reasons about that already exists there (anticoagulants,
    ace_inhibitors, nsaids, aminoglycosides) comes from it.
  * ``drug_safety_service.classify_allergen`` — an allergy charted as a CLASS
    ("NSAIDs", "Penicillin") read the way a clinician wrote it, substance AND
    class term, word-boundary matched.
  * ``allergy_interlock.read_active_allergies`` — the same active-allergy read
    the bedside interlock screens against, so the flag and the interlock cannot
    describe different allergy lists.
  * ``allergy_interlock.resolve_ingredients_bulk`` — the hospital's OWN formulary
    (``helix_medication_master``), so a charted brand name ("Monocef") is
    classified by its ingredient rather than as an unknown word.
  * ``drug_interactions`` (``interaction_db``) — the SAME table the order-time
    medication safety gate screens against, read here only as a VOCABULARY: it
    is how the fabrication guard recognises a named ingredient that has no drug
    class and is not stocked locally.

WHAT IS NEW, AND WHY IT IS NOT A SECOND DRUG KNOWLEDGE BASE
-----------------------------------------------------------
:data:`_SCREEN_LEXICON` covers exactly the four concepts ``classify_drug`` has no
class for and this screen cannot ask its questions without: rate control, systemic
corticosteroids/immunosuppression, the narrow-therapeutic-index drugs whose
handling changes with renal function, and heart failure as a charted problem.
Each entry names why it is there. Nothing in it duplicates a class
``classify_drug`` already knows — where the two overlap, ``classify_drug`` wins
and this table is not consulted. If Orb ever gains a licensed drug-class source,
this table is the only thing to delete.

:data:`_PROSE_ORDINARY` and its three companions are the OTHER new table, and it
is the inverse of a drug knowledge base: it holds no drug name at all (a test
asserts that against all three vocabularies above) and exists so the fabrication
guard can ask "is this word ordinary clinical prose?" instead of "is this word a
drug Orb happens to know?". The second question fails open on every drug Orb has
never heard of — which, on a 14-row dev formulary, is vancomycin. See the long
comment above :func:`is_prose`.

HOUSE RULES
-----------
Everything here is BLOCKING and PURE-SYNC: it is called from inside the caller's
existing ``run_db`` dispatch on the caller's open connection, so it opens no
session of its own and never touches the event loop. :func:`screen` NEVER RAISES —
it runs inside the transaction that creates the assessment row, and a bug in a
convenience must not take a clinical record with it (same argument as
``sage_assessment_service._compute_tier1``).
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from typing import Any

from services.drug_safety_service import _norm, classify_allergen, classify_drug

logger = logging.getLogger("orb.patient_specific_screen")


# ═════════════════════════════════════════════════════════════════════════════
#  THE RULES, BY NAME
#
#  Each constant is one clinical question. The reason each exists is written at
#  its implementation, not here, so the rule and its justification cannot drift
#  apart.
# ═════════════════════════════════════════════════════════════════════════════
RULE_RATE_CONTROL = "rate_control_masks_the_pulse"
RULE_ANTICOAGULATION = "anticoagulated_in_this_picture"
RULE_ANTIMICROBIAL_ALLERGY = "antimicrobial_allergy_in_an_infective_picture"
RULE_RENAL_VOLUME = "renal_impairment_and_a_volume_decision"
RULE_RENAL_DRUG = "renally_handled_drug_with_a_raised_creatinine"
RULE_HEART_FAILURE = "heart_failure_and_a_volume_decision"
RULE_BLUNTED_FEVER = "steroid_or_immunosuppression_may_blunt_the_fever"

#: Ceiling on how many facts reach the model. Not a latency bound — the list is
#: already tiny — but a CLINICAL one: a screen that hands over eight facts has
#: stopped pre-screening and is asking the model to trawl. If a patient really
#: has more than this many, the highest-consequence rules are ordered first.
MAX_CANDIDATES = 4

#: How stale a lab may be and still describe this deterioration. A creatinine
#: from three months ago is history, not a finding. Fourteen days is the window
#: an inpatient's renal function is meaningfully bounded by, and the AGE IS
#: ALWAYS RENDERED with the value ("creatinine 180, 6 days ago") so a clinician
#: weighs it themselves rather than trusting our window.
LAB_WINDOW_DAYS = 14


# ── NEWS2 parameter names, exactly as vigil_service's breakdown spells them ──
# Copied from the engine's own `add(... parameter=...)` calls. Spelled out here
# so a rename there fails a test rather than silently switching a rule off.
P_RR = "Respiratory rate"
P_SPO2 = "SpO2"
P_SPO2_SCALE2 = "SpO2 (Scale 2)"
P_O2 = "Supplemental O2"
P_SBP = "Systolic BP"
P_HR = "Heart rate"
P_TEMP = "Temperature"
P_ACVPU = "Consciousness (ACVPU)"

_SPO2_PARAMS = frozenset({P_SPO2, P_SPO2_SCALE2})

#: The classes an infective deterioration is likely to be treated with, and
#: therefore the allergies that can rule out the obvious first-line agent. Taken
#: from `classify_allergen`'s own vocabulary — no new terms.
ANTIMICROBIAL_CLASSES = frozenset(
    {
        "penicillins",
        "cephalosporins",
        "carbapenems",
        "sulfonamides",
        "macrolides",
        "aminoglycosides",
    }
)

#: An infective picture, read off the working diagnosis / problem list. Word
#: fragments rather than whole words on purpose: charts write "pneumonia",
#: "bronchopneumonia" and "HAP/pneumonia" for the same thing.
_INFECTIVE_RE = re.compile(
    r"sepsis|septic|infect|pneumon|\bhap\b|\bcap\b|\bvap\b|bacteraem|bacterem|"
    r"cellulit|abscess|empyema|meningit|endocardit|pyelonephrit|\buti\b|"
    r"urinary tract infection|cholangit|cholecystit|peritonit|osteomyelit|"
    r"febrile neutropen|covid|dengue|malaria|typhoid|tubercul|\btb\b",
    re.IGNORECASE,
)

#: Heart failure as a charted problem. Deliberately narrow — "cardiac" and
#: "CCF-like" wording that could mean anything is not in here, because a
#: fluid-caution flag on a patient who does not have heart failure is the exact
#: false positive that trains the tier out of existence.
_HEART_FAILURE_RE = re.compile(
    r"heart failure|cardiac failure|\bccf\b|\bchf\b|\bhfref\b|\bhfpef\b|"
    r"congestive cardiac|left ventricular (?:systolic )?(?:dys)?function|"
    r"\blvsd\b|cardiomyopath|pulmonary oedema|pulmonary edema",
    re.IGNORECASE,
)

#: THE FOUR CONCEPTS `classify_drug` HAS NO CLASS FOR. See the module docstring:
#: this is not a second drug knowledge base, it is the short list of things this
#: screen must be able to ask about and the shared classifier cannot answer.
#: Matched as SUBSTRINGS of a normalised drug name, exactly as `classify_drug`
#: matches its own patterns, so a strength suffix ("Metoprolol 50") still hits.
#: A BRAND NAME DOES NOT HIT THIS TABLE DIRECTLY and is not meant to — "Metolar"
#: contains no beta-blocker stem. It is the formulary resolution in
#: `_active_medications` that turns it into "metoprolol" first, which is why that
#: reuse is load-bearing on an Indian ward rather than a nicety.
_SCREEN_LEXICON: dict[str, list[str]] = {
    # RATE CONTROL. The reason the whole rule exists: NEWS2 scores the
    # compensatory tachycardia of shock, and these drugs are precisely what stops
    # a patient mounting it. "-olol" is the beta-blocker stem (metoprolol,
    # atenolol, bisoprolol, carvedilol, propranolol, labetalol, nebivolol,
    # sotalol, esmolol); the rate-limiting calcium blockers and the two
    # rate-control agents with no stem are enumerated.
    "rate_control": ["olol", "ivabradine", "digoxin", "verapamil", "diltiazem"],
    # SYSTEMIC CORTICOSTEROIDS + IMMUNOSUPPRESSION. Both blunt the febrile
    # response and the inflammatory markers a clinician reads temperature
    # against. Enumerated rather than stemmed: "-sone"/"-olone" would catch
    # unrelated agents, and over-firing here is worse than missing one.
    "immunosuppression": [
        "prednisolone",
        "prednisone",
        "methylprednisolone",
        "dexamethasone",
        "hydrocortisone",
        "betamethasone",
        "deflazacort",
        "budesonide",
        "azathioprine",
        "ciclosporin",
        "cyclosporine",
        "tacrolimus",
        "mycophenolate",
        "methotrexate",
        "cyclophosphamide",
        "rituximab",
        "infliximab",
        "adalimumab",
        "etanercept",
        "tocilizumab",
    ],
    # RENALLY HANDLED / NARROW THERAPEUTIC INDEX. `classify_drug` already covers
    # ace_inhibitors, nsaids and aminoglycosides — those come from IT, and only
    # these four (which it has no class for) are listed here.
    "renally_handled": ["metformin", "digoxin", "lithium", "spironolactone"],
}


def _lexicon_hit(med: dict, key: str) -> bool:
    """True when ANY name this medication is known by hits the lexicon.

    Checked across the charted name AND the formulary generics it resolves to,
    for the same reason `allergy_interlock` resolves before it matches: a ward
    charts "Metolar 50", and the beta-blocker stem lives in "metoprolol".
    """
    terms = _SCREEN_LEXICON[key]
    return any(term in n for n in med.get("names", ()) for term in terms)


# ═════════════════════════════════════════════════════════════════════════════
#  THE SCREEN
# ═════════════════════════════════════════════════════════════════════════════
def screen(db, patient_id: str, tier1: dict, *, facts: dict | None = None) -> dict:
    """Candidate patient-specific facts for THIS deterioration. NEVER RAISES.

    Blocking — the caller is already inside a ``run_db`` dispatch and this runs
    on their open connection, inside the transaction that creates the assessment
    row. An exception here would leave no row at all, which is the silent-absence
    failure the assessment design forbids on the one path where a clinician is
    already looking at an alert.

    ``tier1`` is ``sage_assessment_service._compute_tier1``'s block — the
    deterministic NEWS2 decomposition. It is the ONLY description of the
    deterioration this module reads, so the facts a flag is hooked to and the
    numbers on the clinician's screen are one computation.

    ``facts`` is ``sage_service._assemble_patient_context``'s chart dict when the
    caller already has it (the diagnosis and problem list are read from it rather
    than queried a second time). Optional so this is testable against a bare db.

    Returns::

        {"screened": bool, "candidates": [...], "rules_fired": [...],
         "allowed_terms": {...}, "allowed_numbers": {...},
         "supplied_tokens": {...}, "known_drug_tokens": {...},
         "picture": {...}, "screen_error": str|None}
    """
    try:
        return _screen(db, patient_id, tier1, facts)
    except Exception:
        logger.exception("[PatientScreen] pre-screen failed for %s", patient_id)
        # A FAILED SCREEN IS NOT A CLEAN ONE — the same distinction
        # `check_medication_safety.interaction_screened` draws. No candidates
        # means no model call either way, but the caller must be able to tell
        # "Orb looked and found nothing" from "Orb could not look".
        return _empty(screened=False, error="screen_failed")


def _empty(*, screened: bool, error: str | None = None) -> dict:
    return {
        "screened": screened,
        "candidates": [],
        "rules_fired": [],
        "allowed_terms": frozenset(),
        "allowed_numbers": frozenset(),
        "supplied_tokens": frozenset(),
        "known_drug_tokens": frozenset(),
        "picture": {},
        "screen_error": error,
    }


def _screen(db, patient_id: str, tier1: dict, facts: dict | None) -> dict:
    if not tier1 or not tier1.get("available"):
        # No scoreable observation, so there is no deterioration to hook a fact
        # to. Every rule below is of the form "this chart fact AND this thing the
        # patient is doing right now"; with the second half missing, firing on
        # the first half alone would be a flag about a drug list, not about a
        # deterioration.
        return _empty(screened=False, error="no_scoreable_observation")

    picture = _picture(tier1)
    meds = _active_medications(db, patient_id)
    allergies = _allergies(db, patient_id)
    problems = _problems(db, patient_id, facts)
    creatinine = _latest_creatinine(db, patient_id)

    candidates: list[dict] = []
    for rule in (
        _rule_rate_control,
        _rule_anticoagulation,
        _rule_antimicrobial_allergy,
        _rule_renal_volume,
        _rule_renal_drug,
        _rule_heart_failure,
        _rule_blunted_fever,
    ):
        try:
            found = rule(picture, meds, allergies, problems, creatinine) or []
        except Exception:
            # ONE BROKEN RULE IS NOT A BROKEN SCREEN. The others still describe
            # the patient, and a flag lost is better than every flag lost.
            logger.exception(
                "[PatientScreen] rule %s failed for %s", getattr(rule, "__name__", rule), patient_id
            )
            found = []
        candidates.extend(found)

    candidates = candidates[:MAX_CANDIDATES]
    for i, cand in enumerate(candidates, start=1):
        cand["id"] = f"F{i}"

    return {
        "screened": True,
        "candidates": candidates,
        "rules_fired": [c["rule"] for c in candidates],
        # ── The vocabulary the output filters are made of ──
        "allowed_terms": _allowed_terms(candidates),
        "allowed_numbers": _allowed_numbers(candidates, tier1),
        "supplied_tokens": _supplied_tokens(candidates, tier1),
        "known_drug_tokens": _known_drug_tokens(db, meds),
        "picture": picture,
        "screen_error": None,
    }


# ── What the deterioration looks like, read off Tier 1 ───────────────────────
def _picture(tier1: dict) -> dict:
    """The scored observation, indexed by parameter, plus the two derived facts
    every rule below asks about."""
    scoring = {str(p.get("parameter")): p for p in (tier1.get("parameters") or [])}
    steady = {str(p.get("parameter")): p for p in (tier1.get("non_scoring") or [])}
    measured = {**steady, **scoring}
    return {
        "scoring": scoring,
        "measured": measured,
        "news2": tier1.get("news2"),
        # "Something other than X is driving this" — the precondition on the two
        # masking rules, which are about a parameter that looks REASSURING while
        # the patient is deteriorating by other means.
        "scoring_names": frozenset(scoring),
        "hypotensive": P_SBP in scoring,
        "hypoxic": bool(_SPO2_PARAMS & frozenset(scoring)) or P_O2 in scoring,
        "tachypnoeic": P_RR in scoring,
        "febrile": P_TEMP in scoring,
        "altered": P_ACVPU in scoring,
    }


def _other_drivers(picture: dict, *exclude: str) -> list[str]:
    return sorted(n for n in picture["scoring_names"] if n not in exclude)


def _shown(param: dict | None) -> str:
    """A parameter's value the way the clinician's own panel renders it. Falls
    back to the raw value so a rule can still name the fact if Tier 1's display
    string is ever absent."""
    if not param:
        return "—"
    return str(param.get("display") or param.get("value") or "—")


# ── The chart reads ──────────────────────────────────────────────────────────
def _active_medications(db, patient_id: str) -> list[dict]:
    """What the patient is actually on, with the dose, so a flag can name it.

    ``status != 'discontinued'`` matches `_assemble_patient_context` rather than
    the stricter ``status = 'active'``: a PRN opioid or steroid is still in this
    patient and still blunting what it blunts.

    EVERY NAME IS RESOLVED THROUGH THE HOSPITAL'S OWN FORMULARY BEFORE IT IS
    CLASSIFIED, via `allergy_interlock.resolve_ingredients_bulk` — the same one
    query the bedside interlock uses, so the two cannot disagree about what a
    charted brand name denotes. Without it "Clexane" classifies as nothing at
    all and an anticoagulated patient's flag never fires; with it, it resolves to
    enoxaparin and `classify_drug` puts it in `anticoagulants`. Indian wards
    chart brands, so this is the common case rather than the exotic one.

    `name` — what the FLAG says — stays exactly as charted. The generics are
    matching material only: a clinician checking a flag in one glance needs to
    see the string that is on their own drug chart.
    """
    rows = db.execute(
        "SELECT id, drug_name, dosage, route, frequency, status, started_at "
        "FROM medications WHERE patient_id = ? AND status != 'discontinued' "
        "ORDER BY id",
        (patient_id,),
    ).fetchall()
    charted = [(r, (r["drug_name"] or "").strip()) for r in rows]
    charted = [(r, n) for r, n in charted if n]

    from services.allergy_interlock import resolve_ingredients_bulk

    try:
        # ONE query for the whole drug list — the helper's own N+1 guarantee.
        resolved = resolve_ingredients_bulk(db, [n for _, n in charted])
    except Exception:
        # The formulary is an ENRICHMENT. Losing it costs brand resolution and
        # nothing else; the charted name is still classified. Same posture the
        # interlock takes, and the same one `_known_drug_tokens` takes below.
        logger.debug(
            "[PatientScreen] formulary resolution unavailable for %s", patient_id, exc_info=True
        )
        resolved = {}

    meds = []
    for r, name in charted:
        generics = [g for g in (resolved.get(_norm(name)) or []) if g]
        classes: set[str] = set()
        for candidate in [name, *generics]:
            classes.update(classify_drug(candidate))
        meds.append(
            {
                "name": name,
                "dose": " ".join(
                    x
                    for x in [
                        (r["dosage"] or "").strip(),
                        (r["route"] or "").strip(),
                        (r["frequency"] or "").strip(),
                    ]
                    if x
                ),
                "classes": classes,
                # WHEN IT WAS STARTED, for the rendered block's "where to check it"
                # line. Phrased here; never composed in a client.
                "recorded": _recorded_phrase(_row_value(r, "started_at")),
                # Normalised, deduplicated, charted name first. `_lexicon_hit`
                # matches over all of them.
                "names": list(dict.fromkeys(n for n in (_norm(x) for x in [name, *generics]) if n)),
            }
        )
    return meds


def _row_value(row, column: str):
    """One column off a DB row, tolerating a row that does not carry it.

    Rows reach this module from three drivers (stdlib sqlite3, sqlcipher3, and a
    plain dict in the tests) and only some of them raise IndexError rather than
    returning None for an absent column. A missing timestamp must cost the
    `recorded` phrase and nothing else — never a candidate, and certainly never
    the screen.
    """
    try:
        return row[column]
    except Exception:
        return None


def _allergies(db, patient_id: str) -> list[dict]:
    """THE SAME active-allergy read the bedside interlock screens against."""
    from services.allergy_interlock import read_active_allergies

    # WHEN EACH ONE WAS CHARTED, read here rather than widened into the
    # interlock's own SELECT. That function is the fail-closed read a bedside
    # block depends on; adding a display field to it would put a rendering
    # concern inside a safety path. One indexed lookup on the same index
    # (idx_allergies_patient) is the cheaper mistake.
    stamps: dict = {}
    try:
        stamps = {
            r["id"]: r["recorded_at"]
            for r in db.execute(
                "SELECT id, recorded_at FROM patient_allergies "
                "WHERE patient_id = ? AND status = 'active'",
                (patient_id,),
            ).fetchall()
        }
    except Exception:
        logger.debug(
            "[PatientScreen] allergy timestamps unavailable for %s", patient_id, exc_info=True
        )
    out = []
    for a in read_active_allergies(db, patient_id):
        substance = (a.get("substance") or "").strip()
        if not substance:
            continue
        classes = set(classify_allergen(substance))
        if a.get("substance_class"):
            classes.add(str(a["substance_class"]))
        out.append(
            {
                "substance": substance,
                "reaction": (a.get("reaction") or "").strip(),
                "severity": (a.get("severity") or "unknown").strip(),
                "recorded": _recorded_phrase(stamps.get(a.get("id"))),
                "classes": classes,
            }
        )
    return out


def _problems(db, patient_id: str, facts: dict | None) -> list[dict]:
    """The working diagnosis and the active problem list, as charted.

    Read off `facts` when the caller already assembled the chart — the alert path
    always has — so this costs no second query there.

    RETURNS DICTS, NOT STRINGS, since 2026-08-15: `{"text": ..., "recorded":
    ...}`. The rendered block names where a fact came from and when it was
    charted, and a problem that arrives as a bare string has thrown its date
    away before this module can phrase it. `recorded` is None off the `facts`
    path — the assembled chart carries the working diagnosis without a
    timestamp, and inventing one would be worse than saying nothing.
    """
    if facts:
        items = [str(facts.get("diagnosis") or "")] + [
            str(p) for p in (facts.get("problems") or [])
        ]
        return [{"text": i.strip(), "recorded": None} for i in items if i and i.strip()]
    out: list[dict] = []
    row = db.execute("SELECT diagnosis FROM patients WHERE id = ?", (patient_id,)).fetchone()
    if row and (row["diagnosis"] or "").strip():
        out.append({"text": row["diagnosis"].strip(), "recorded": None})
    try:
        for r in db.execute(
            "SELECT problem, recorded_at FROM problem_list WHERE patient_id = ? "
            "AND status = 'active' ORDER BY recorded_at DESC LIMIT 8",
            (patient_id,),
        ).fetchall():
            text = (r["problem"] or "").strip()
            if text:
                out.append(
                    {"text": text, "recorded": _recorded_phrase(_row_value(r, "recorded_at"))}
                )
    except Exception:
        # `problem_list` is a table migrations treats as possibly absent.
        logger.debug("[PatientScreen] problem list unavailable for %s", patient_id, exc_info=True)
    return out


#: Creatinine as a lab is written a dozen ways. Matched on the observation NAME
#: — never on free prose. See :func:`_creatinine_from_lab_report` for the one
#: place a result outside the `observations` table is read, and the four
#: conditions that make it a RESULT LINE rather than a sentence.
_CREATININE_RE = re.compile(r"creatinin|\bcreat\b|\bs\.?\s?cr\b", re.IGNORECASE)

#: Flags an analyser sets for "above range". Preferred over any threshold of
#: ours: the lab's own reference range knows this population and this assay.
_HIGH_FLAGS = frozenset({"h", "hh", "high", "a", "ah", "abnormal", "*", "+"})


def _latest_creatinine(db, patient_id: str) -> dict | None:
    """The most recent creatinine inside the lab window, and whether it is raised.

    Returns None when there is no creatinine, when it is older than
    :data:`LAB_WINDOW_DAYS`, or when it is normal — all three of which mean the
    renal rules have nothing to say, which is the common and correct case.

    TWO SOURCES, IN PREFERENCE ORDER, AND THE SECOND ONE IS WHY THIS DOCSTRING
    CHANGED. Until 2026-08-15 this read the ``observations`` table alone —
    structured OBX results from the HL7/FHIR ingestion lane — and refused, in
    writing, to look anywhere else. Measured on this appliance the same day:
    ``observations`` holds ZERO ROWS for every patient on the ward, because the
    only thing that writes it is ``ingestion_service`` and no HL7 feed is
    connected. Every lab Orb actually holds is a ``clinical_documents`` row of
    ``doc_type='lab_result'``. So both renal rules were unreachable code: Lakshmi
    Devi (PAT-BA80FFC5), creatinine 2.5 mg/dL flagged [H] against a reference
    range of 0.7-1.3 with a systolic of 78, was screened silent on three of three
    alerts. A rule that cannot fire measures nothing, and it fails in the
    direction that looks like good news.

    The structured lane stays FIRST and unchanged: when an analyser feed exists
    it is the better source, it carries the analyser's own abnormal flag, and
    nothing about it needed fixing.
    """
    return _creatinine_from_observations(db, patient_id) or _creatinine_from_lab_report(
        db, patient_id
    )


def _creatinine_from_observations(db, patient_id: str) -> dict | None:
    """The structured lane: OBX results the ingestion adapters wrote.

    Read through services/lab_observations.py, the one reader the chart's lab
    card and the ward pack share, so this lane follows their rules:

      * A voided or superseded creatinine is never read. Before, a result the
        lab withdrew stayed "the latest" here and could raise a renal flag the
        chart no longer showed.
      * Newest by julianday(COALESCE(observed_at, recorded_at)), not by the
        stored string ('T' or space, offset or none), and equal instants go
        to the later write (id DESC).
      * A simulated creatinine is left out only while the opt-in lab module
        (ORB_LAB) is on. With it off, as at every site today, the lane reads
        it as it always has: a demo ward's results are simulated, and its
        renal flags must not vanish silently.
    """
    from services import lab_observations

    try:
        rows = lab_observations.recent_observations(
            db, patient_id, limit=60, exclude_simulated=lab_observations.tier1_excludes_simulated()
        )
    except Exception:
        logger.debug("[PatientScreen] observations unavailable for %s", patient_id, exc_info=True)
        return None

    for r in rows:
        name = (r["observation_name"] or "").strip()
        if not _CREATININE_RE.search(name):
            continue
        value = r["value_numeric"]
        if value is None:
            continue
        stamp = r["observed_at"] or r["recorded_at"]
        age_days = _age_days(stamp)
        if age_days is None or age_days > LAB_WINDOW_DAYS:
            return None  # the latest one is stale; an older one is worse
        unit = (r["unit"] or "").strip()
        if not _is_raised_creatinine(float(value), unit, r["abnormal_flag"]):
            return None  # the latest one is normal — nothing to flag
        return {
            "name": name or "Creatinine",
            "value": float(value),
            "unit": unit,
            "age_days": age_days,
            "flag": (r["abnormal_flag"] or "").strip(),
            "display": _lab_display(name or "Creatinine", float(value), unit, age_days),
            # Which report to open to check it. The structured lane has a panel
            # name only sometimes; None is fine — the section is what a clinician
            # navigates by.
            "panel": None,
        }
    return None


#: ONE RENDERED RESULT LINE, and nothing that is merely a sentence containing a
#: number. Orb's own lab reports (`bot_service._generate_lab_result`,
#: `seed_labs`, the ward's manual entry route) render every analyte as
#:
#:     "  Creatinine: 2.5 mg/dL  [H]  (ref: 0.7-1.3)"
#:
#: and THE REFERENCE RANGE IS MANDATORY IN THIS PATTERN. That is the whole
#: safety argument for reading a document at all, and it is worth being explicit
#: about: this is not "find a number near the word creatinine". A line qualifies
#: only when it is `analyte : number unit [flag] (ref: range)` — four structured
#: fields in one line, which is a rendered table row and not prose. "Creatinine
#: has been climbing", "hold nephrotoxics if creatinine > 2" and "creatinine 2.5
#: yesterday" in a ward-round note all fail it, because none of them carries a
#: reference range in parentheses. A clinician's free text can therefore never
#: become a flag; only a result Orb itself laid out as a result can.
_LAB_LINE_RE = re.compile(
    r"^\s*(?P<name>[A-Za-z][A-Za-z0-9 .\-'/]{1,40}?)\s*:\s*"
    r"(?P<value>\d{1,4}(?:\.\d{1,3})?)\s*"
    r"(?P<unit>[A-Za-zµμ%][A-Za-zµμ%/\d]{0,11})\s+"
    r"(?P<flag>.*?)\s*"
    r"\(\s*ref\s*:\s*(?P<ref>[^)]{1,40})\)\s*$",
    re.IGNORECASE | re.MULTILINE,
)

#: The reference range as those reports write it: "0.7-1.3", "70-110".
_REF_RANGE_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*[-–]\s*(\d+(?:\.\d+)?)\s*$")

#: How many lab reports back to look for a creatinine. THREE, matching the
#: structured lane's posture rather than a number chosen for feel: that path
#: reads a window of recent results and stops at the FIRST creatinine it finds,
#: because the latest one is the one that describes this patient. Three reports
#: is the same statement in the document lane — enough that a creatinine sitting
#: under yesterday's blood gas is still found, few enough that an old result
#: cannot climb over a newer normal one.
_LAB_REPORTS_SCANNED = 3


def _creatinine_from_lab_report(db, patient_id: str) -> dict | None:
    """The document lane: a creatinine off Orb's OWN rendered lab report.

    THIS IS THE LANE THAT ACTUALLY HAS DATA (see :func:`_latest_creatinine`), and
    it is deliberately the narrower of the two:

      * ``doc_type = 'lab_result'`` only. A discharge summary, a ward-round note
        and a scribe transcript are never read, whatever numbers they contain.
      * :data:`_LAB_LINE_RE` only — a line with an analyte, a value, a unit AND a
        reference range. Prose cannot satisfy it.
      * ONE creatinine per report. Two different creatinine values inside one
        document is an ambiguity, and this refuses rather than picks; a wrong lab
        value on an alert screen is the worst outcome available here and there is
        no version of guessing that is better than silence.
      * The report's OWN reference range decides "raised" before any threshold of
        ours — the same preference order the structured lane gives the analyser's
        abnormal flag, and for the same reason: that range knows this assay.

    WHAT THIS STILL CANNOT DO, stated so nobody assumes otherwise: it reads the
    format Orb renders. A PDF from an outside lab, a photographed report, or an
    HL7 feed writing free text are all invisible to it, and they are invisible
    SAFELY — the screen stays quiet rather than guessing. The permanent fix is a
    structured result on the `observations` table, which is the lane above.
    """
    try:
        # THE SEPARATOR IS NORMALISED BEFORE THE SORT, and it is a correctness
        # detail rather than tidiness. `clinical_documents.created_at` holds two
        # shapes: SQLite's own `CURRENT_TIMESTAMP` writes "2026-08-14 17:24:15"
        # and every explicit writer uses isoformat, "2026-08-14T17:24:15+00:00".
        # In a text sort ' ' (0x20) precedes 'T' (0x54), so on a day carrying
        # both, a plain `ORDER BY created_at DESC` puts the isoformat row FIRST
        # regardless of the clock — and this function stops at the first report
        # holding a creatinine. That is the shape of a silent miss: today's STAT
        # result showing 5.0 passed over for this morning's normal 1.1, and the
        # renal rules going quiet on exactly the patient they exist for.
        # A SIMULATOR'S RESULT IS NEVER READ (source = 'simulated', migration
        # 46). The lab bot writes this exact format with random values, and a
        # renal flag raised off a fabricated creatinine is a false alarm with a
        # real patient's name on it. NULL source (seeded, manual, imported)
        # stays in.
        rows = db.execute(
            "SELECT title, content, created_at FROM clinical_documents "
            "WHERE patient_id = ? AND doc_type = 'lab_result' "
            "AND COALESCE(source, '') != 'simulated' "
            "ORDER BY REPLACE(created_at, ' ', 'T') DESC LIMIT ?",
            (patient_id, _LAB_REPORTS_SCANNED),
        ).fetchall()
    except Exception:
        logger.debug("[PatientScreen] lab reports unavailable for %s", patient_id, exc_info=True)
        return None

    for r in rows:
        found = _creatinine_lines(r["content"] or "")
        if found is None:
            # Ambiguous report — two different creatinine values in one document.
            # Refuse the whole patient rather than fall through to an older
            # report, which would answer a question this data cannot answer.
            logger.warning(
                "[PatientScreen] ambiguous creatinine in lab report for %s — no renal flag",
                patient_id,
            )
            return None
        if not found:
            continue
        name, value, unit, flag, ref = found
        age_days = _age_days(r["created_at"])
        if age_days is None or age_days > LAB_WINDOW_DAYS:
            return None  # the latest one is stale; an older one is worse
        # THE REPORT'S OWN RANGE IS CONSULTED BEFORE ANY THRESHOLD OF OURS, and
        # only where the printed flag column did not already answer. Same
        # preference order the structured lane gives the analyser's flag.
        verdict = flag if str(flag).strip().lower() in _KNOWN_FLAGS else _ref_verdict(value, ref)
        if not _is_raised_creatinine(value, unit, verdict):
            return None  # the latest one is normal — nothing to flag
        return {
            "name": name,
            "value": value,
            "unit": unit,
            "age_days": age_days,
            "flag": flag,
            "display": _lab_display(name, value, unit, age_days),
            # WHICH REPORT TO OPEN. The rendered block puts this in the "where to
            # check it" line, which is the only thing that makes a parsed number
            # as checkable as a structured one.
            "panel": (r["title"] or "").strip() or None,
        }
    return None


def _creatinine_lines(content: str):
    """The single creatinine result line in one report.

    Returns ``(name, value, unit, flag, ref)``, ``()`` for a report with no
    creatinine in it, or ``None`` for an AMBIGUOUS one — two lines naming
    creatinine with different values. The three-way return is the point: "not
    here" and "here twice and I cannot tell which" are different answers and only
    the first is safe to walk past.
    """
    hits = []
    for m in _LAB_LINE_RE.finditer(content or ""):
        name = m.group("name").strip()
        if not _CREATININE_RE.search(name):
            continue
        # `or ""` on every optional-shaped group: a future edit that makes one of
        # them optional would otherwise raise AttributeError here, and `screen`'s
        # blanket guard would turn that into a "screen_failed" — a silent, total
        # loss of the renal rules that looks exactly like a chart with no labs.
        hits.append(
            (
                name,
                float(m.group("value")),
                (m.group("unit") or "").strip(),
                _flag_letter(m.group("flag")),
                (m.group("ref") or "").strip(),
            )
        )
    if not hits:
        return ()
    if len({h[1] for h in hits}) > 1:
        return None
    return hits[0]


#: The flag column of Orb's own reports carries "[H]", "HIGH", "[H] ⚠️ CRITICAL",
#: "N" or nothing. This is the first alphabetic run in it.
_FLAG_LETTER_RE = re.compile(r"[A-Za-z]+")

#: Everything `_is_raised_creatinine` can actually decide from. Anything else in
#: the flag column ("CRITICAL" on its own, a footnote marker) is treated as "the
#: report did not say", so the printed reference range gets its turn instead of
#: being skipped by a word this file does not understand.
_KNOWN_FLAGS = _HIGH_FLAGS | frozenset({"n", "normal", "l", "low"})


def _flag_letter(flag: str | None) -> str:
    """The bare flag letter the structured lane already understands, so ONE
    function decides "raised" for both lanes."""
    m = _FLAG_LETTER_RE.search(flag or "")
    return m.group(0) if m else ""


def _ref_verdict(value: float, ref: str) -> str:
    """ "h"/"n" from the report's OWN reference range, or "" when it cannot be read.

    Fed to :func:`_is_raised_creatinine` in the flag position so the range is
    consulted BEFORE any threshold this file holds. An empty string means "the
    range told us nothing", which lets the unit-aware fallback run exactly as it
    does for a structured result with no analyser flag.
    """
    m = _REF_RANGE_RE.match(ref or "")
    if not m:
        return ""
    try:
        return "h" if value > float(m.group(2)) else "n"
    except (TypeError, ValueError):  # pragma: no cover — the regex guarantees floats
        return ""


def _is_raised_creatinine(value: float, unit: str, flag) -> bool:
    """The lab's own flag first; a unit-aware threshold only as a fallback.

    THE UNIT MATTERS MORE THAN THE NUMBER. 180 is a markedly raised creatinine in
    µmol/L and an impossible one in mg/dL; 1.8 is the reverse. Where the unit is
    absent the magnitude decides (an Indian lab reporting mg/dL never reports 180,
    and a µmol/L lab never reports 1.8), which is the same heuristic a clinician
    applies reading a foreign report.
    """
    f = str(flag or "").strip().lower()
    if f in _HIGH_FLAGS:
        return True
    if f in ("n", "normal", "l", "low"):
        return False
    u = unit.lower().replace(" ", "")
    if "mg" in u:  # mg/dL
        return value > 1.3
    if "mol" in u:  # µmol/L, umol/l, mcmol/L
        return value > 110.0
    return value > 1.3 if value <= 20 else value > 110.0


def _lab_display(name: str, value: float, unit: str, age_days: int) -> str:
    figure = f"{value:g}"
    when = "today" if age_days <= 0 else "yesterday" if age_days == 1 else f"{age_days} days ago"
    return f"{name} {figure}{(' ' + unit) if unit else ''}, {when}"


def _age_days(stamp) -> int | None:
    if not stamp:
        return None
    try:
        parsed = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return max(0, int((datetime.now(UTC) - parsed).total_seconds() // 86400))


def _recorded_phrase(stamp) -> str | None:
    """WHEN THIS WAS CHARTED, as the sentence the ward will read.

    Composed here rather than in the client for the same reason
    ``unavailable_message`` is: a clinical date phrased by two different
    renderers is a date that will eventually be phrased two different ways, and
    "charted 6 days ago" versus "charted today" is exactly the difference
    between a fact worth acting on and one worth re-checking.

    Relative inside the fortnight a deterioration is bounded by, absolute
    outside it — "charted 214 days ago" is arithmetic nobody performs, and a
    long-standing allergy reads correctly as a date. None when the chart has no
    timestamp, which is a gap to be silent about rather than to guess at.
    """
    days = _age_days(stamp)
    if days is None:
        return None
    if days <= 0:
        return "charted today"
    if days == 1:
        return "charted yesterday"
    if days <= LAB_WINDOW_DAYS:
        return f"charted {days} days ago"
    try:
        parsed = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except (TypeError, ValueError):  # pragma: no cover — _age_days already parsed it
        return f"charted {days} days ago"
    return f"charted {parsed.day} {parsed.strftime('%b')}"


# ═════════════════════════════════════════════════════════════════════════════
#  THE RULES
#
#  Every one has the same shape: A CHART FACT **AND** SOMETHING THIS PATIENT IS
#  DOING RIGHT NOW. The conjunction is what makes a flag patient-specific rather
#  than a restatement of the drug chart, and it is what keeps the firing rate
#  honest — the drug alone is not a finding.
#
#  Each candidate carries the evidence that produced it, so the rendered line can
#  name it and a clinician can check it in one glance. A checkable claim beats a
#  confident one.
# ═════════════════════════════════════════════════════════════════════════════
#: WHERE ON THE CHART A FACT LIVES, in the words the ward's own navigation uses.
#: This is not decoration: the flag's whole claim to a clinician's attention is
#: that it can be CHECKED in one glance, and "Allergies" is the difference
#: between checking it and taking Orb's word for it.
SOURCE_MEDICATIONS = "Medications"
SOURCE_ALLERGIES = "Allergies"
SOURCE_RESULTS = "Results"
SOURCE_PROBLEMS = "Problems"
SOURCE_MEDICATIONS_AND_RESULTS = "Medications and results"


def _candidate(
    rule: str,
    *,
    fact: str,
    hook: str,
    question: str,
    terms: list[str],
    numbers: list[Any],
    evidence: dict,
    source: str,
    recorded: str | None = None,
    relates_to: str | None = None,
) -> dict:
    # EVERY NUMBER THE MODEL IS SHOWN IS A NUMBER IT MAY QUOTE. The explicit
    # `numbers` list carries the ones a rule reasons about; these two lines
    # harvest the rest out of the rendered text — the dose, the lab figure, the
    # days since — because a flag that echoes "metoprolol 50 mg" back at us is
    # doing exactly what it was asked to do and must not be deleted as a
    # fabrication for it.
    keys = {_number_key(n) for n in numbers}
    keys.update(_NUMBER_RE.findall(f"{fact} {hook}"))
    return {
        "rule": rule,
        # The chart evidence, rendered — this is what the model must name.
        "fact": fact,
        # ── THE THREE FIELDS THE RENDERED BLOCK NEEDS AND THE PROMPT DOES NOT ──
        # They exist so the surface can separate THE CHART FACT from SAGE'S
        # JUDGEMENT and still tell a clinician where to go and look. None of them
        # reaches the model (`render_candidates` renders `fact`/`hook`/`question`
        # only) — adding them to the prompt would be paying prefill to tell a
        # model something it is not being asked about.
        #
        # `source` — which section of the chart holds it.
        "source": source,
        # `recorded` — when it was charted, phrased HERE so the client never
        # composes a clinical date. None where the fact string already states it
        # (the labs do: "creatinine 2.5 mg/dL, today"), because a meta line that
        # repeats the fact above it is noise on the busiest screen in the
        # hospital.
        "recorded": recorded,
        # `relates_to` — the scoring parameter this fact bears on, when the rule
        # matched one. None where the rule deliberately does not need the picture
        # (`_rule_renal_drug`), so the surface cannot imply a link Orb never made.
        "relates_to": relates_to,
        # What in THIS deterioration the fact bears on.
        "hook": hook,
        # The clinical question, one clause, for the model to answer yes/no to.
        "question": question,
        "terms": [t for t in (_norm(t) for t in terms) if t],
        "numbers": sorted(k for k in (_number_key(k) for k in keys) if k),
        "evidence": evidence,
    }


def _name_terms(name: str) -> list[str]:
    """A charted name, plus the words a clinician would shorten it to.

    A DROPPED TRUE FLAG IS A SILENT LOSS, and this is where one would happen.
    Charts hold "Metolar 50", "Warfarin 5mg tab", "Lasix inj" — and a model
    handed that fact writes "Metolar". `names_a_fact` matches the allowed term as
    a SUBSTRING of the line, so the full charted string would not be found and a
    correct, checkable flag would be deleted as unsourced.

    Only words of four characters or more, the same floor the drug matcher uses
    (`drug_safety_service._MIN_SUBSTRING_MATCH`) and for the same reason: a short
    fragment matches an arbitrary slice of anything, and this list is what a
    line is allowed to be validated BY.

    DELIBERATELY NOT APPLIED TO THE HOOK TEXT or to the generic backup terms — it
    expands what counts as naming THIS drug or THIS allergen, and nothing else.
    """
    normalised = _norm(name)
    if not normalised:
        return []
    words = [w for w in _TOKEN_RE.findall(normalised) if len(w) >= _MIN_DRUG_TOKEN]
    return [normalised, *words]


def _rule_rate_control(picture, meds, allergies, problems, creatinine) -> list[dict]:
    """A REASSURING PULSE ON A RATE-CONTROLLED PATIENT IS NOT REASSURANCE.

    NEWS2 scores the compensatory tachycardia of shock. A beta-blocker,
    ivabradine, digoxin or a rate-limiting calcium blocker is precisely what stops
    a patient mounting it, so a heart rate of 88 in a hypotensive, tachypnoeic
    patient on metoprolol is a BLUNTED SIGNAL rather than a normal one — and the
    score, by construction, cannot say so. This is the single clearest example of
    a fact that needs the chart and can never be arithmetic over vitals.

    FIRES ONLY when the patient is deteriorating by something OTHER than the
    heart rate. On a patient whose tachycardia is itself the driver the rate
    control is a footnote, not a warning, and firing there is the noise that
    trains the tier out of existence.
    """
    hr = picture["measured"].get(P_HR)
    # THE HEART RATE MUST ACTUALLY BE REASSURING FOR THIS TO BE A WARNING.
    # `P_HR in scoring_names` means the patient IS mounting a rate response and
    # NEWS2 is already scoring it — the clinician can see the tachycardia, the
    # rate control plainly is not hiding it, and a flag saying "this pulse may
    # be blunted" over a heart rate of 138 is precisely the false positive that
    # teaches a ward to skip the panel. Only a heart rate scoring ZERO is the
    # "88 in a shocked patient" case this rule exists for. (Structurally the
    # same guard `_rule_blunted_fever` puts on `picture["febrile"]`: the
    # parameter must be MEASURED, NOT SCORING, while something else is.)
    if hr is None or P_HR in picture["scoring_names"]:
        return []
    others = _other_drivers(picture, P_HR)
    if not others:
        return []
    out = []
    for med in meds:
        if not _lexicon_hit(med, "rate_control"):
            continue
        out.append(
            _candidate(
                RULE_RATE_CONTROL,
                fact=f"{med['name']} {med['dose']}".strip() + " (rate-controlling, on the chart)",
                hook=f"{P_HR} {_shown(hr)} scoring {hr.get('points')} while "
                f"{', '.join(others)} {'is' if len(others) == 1 else 'are'} driving the score",
                question="does the rate control make this heart rate unreliable as reassurance?",
                terms=[*_name_terms(med["name"]), "heart rate", "pulse"],
                numbers=[hr.get("value"), hr.get("points")],
                evidence={
                    "drug": med["name"],
                    "dose": med["dose"],
                    "parameter": P_HR,
                    "value": hr.get("value"),
                },
                source=SOURCE_MEDICATIONS,
                recorded=med.get("recorded"),
                relates_to=f"{P_HR} {_shown(hr)}",
            )
        )
        break  # one rate-control flag per patient; two is a drug list
    return out


def _rule_anticoagulation(picture, meds, allergies, problems, creatinine) -> list[dict]:
    """ANTICOAGULATION CHANGES WHAT THIS DETERIORATION MIGHT BE, AND WHAT MAY BE DONE.

    Hypotension on an anticoagulant raises occult haemorrhage as a cause rather
    than a complication; new confusion on one raises intracranial bleeding. It
    also changes the risk of every procedure the next hour might need — a line, a
    tap, a drain. None of that is visible in a NEWS2, and the class comes from
    `classify_drug` (warfarin, heparins, the -xabans, dabigatran) rather than any
    vocabulary of ours.

    FIRES ONLY on hypotension or altered consciousness. An anticoagulated patient
    who is merely tachypnoeic is an anticoagulated patient, not a finding.
    """
    if not (picture["hypotensive"] or picture["altered"]):
        return []
    driver = P_SBP if picture["hypotensive"] else P_ACVPU
    param = picture["scoring"].get(driver)
    out = []
    for med in meds:
        if "anticoagulants" not in med["classes"]:
            continue
        out.append(
            _candidate(
                RULE_ANTICOAGULATION,
                fact=f"{med['name']} {med['dose']}".strip() + " (anticoagulant, on the chart)",
                hook=f"{driver} {_shown(param)} scoring {param.get('points') if param else '—'}",
                question="does anticoagulation change what this could be, or what may safely be done?",
                terms=[*_name_terms(med["name"]), "anticoagul"],
                numbers=[
                    param.get("value") if param else None,
                    param.get("points") if param else None,
                ],
                evidence={
                    "drug": med["name"],
                    "dose": med["dose"],
                    "parameter": driver,
                    "value": param.get("value") if param else None,
                },
                source=SOURCE_MEDICATIONS,
                recorded=med.get("recorded"),
                relates_to=f"{driver} {_shown(param)}",
            )
        )
        break
    return out


def _rule_antimicrobial_allergy(picture, meds, allergies, problems, creatinine) -> list[dict]:
    """THE OBVIOUS FIRST-LINE AGENT MAY BE THE ONE DRUG THIS PATIENT CANNOT HAVE.

    An infective deterioration has a standard answer, and a documented
    beta-lactam or sulfonamide allergy removes it. The order-time gate and the
    bedside interlock both catch this — but only ONCE SOMEBODY HAS ALREADY
    CHOSEN THE DRUG. At 3am the useful moment is before the choice, and that is
    the only thing this flag adds.

    The allergy is read through `classify_allergen`, so an allergy charted as a
    CLASS ("Penicillin", "Sulfa") is read the way the clinician wrote it, and the
    substance is named back EXACTLY AS CHARTED with its reaction so it can be
    checked in one glance.
    """
    infective = picture["febrile"] or any(_INFECTIVE_RE.search(p["text"]) for p in problems)
    if not infective:
        return []
    out = []
    for allergy in allergies:
        shared = allergy["classes"] & ANTIMICROBIAL_CLASSES
        if not shared:
            continue
        reaction = allergy["reaction"] or "reaction not recorded"
        # A whole clause: "an infective picture: temperature is scoring" read
        # as a line cut off mid-sentence once it was echoed back on the panel.
        why = (
            "temperature is in the scoring range"
            if picture["febrile"]
            else "the working diagnosis is infective"
        )
        out.append(
            _candidate(
                RULE_ANTIMICROBIAL_ALLERGY,
                fact=f"Allergy: {allergy['substance']} ({reaction}) "
                f"(severity {allergy['severity']}, charted; class "
                f"{', '.join(sorted(shared))})",
                hook=f"Infective picture: {why}",
                question="does this allergy rule out the obvious first-line agent here?",
                terms=[*_name_terms(allergy["substance"]), "allerg", *sorted(shared)],
                numbers=[],
                evidence={
                    "allergy": allergy["substance"],
                    "reaction": allergy["reaction"],
                    "severity": allergy["severity"],
                    "classes": sorted(shared),
                },
                source=SOURCE_ALLERGIES,
                recorded=allergy.get("recorded"),
                # ONLY WHEN A SCORING PARAMETER IS WHAT MATCHED. Fired off the
                # working diagnosis instead, this flag bears on no number on the
                # panel above it, and pointing at one would be a link Orb did not
                # make.
                relates_to=(
                    f"{P_TEMP} {_shown(picture['scoring'].get(P_TEMP))}"
                    if picture["febrile"]
                    else None
                ),
            )
        )
        break  # the first antimicrobial allergy is the point; a list is not
    return out


def _rule_renal_volume(picture, meds, allergies, problems, creatinine) -> list[dict]:
    """A RAISED CREATININE CHANGES THE CALCULUS ON A STANDARD FLUID BOLUS.

    The reflex in front of a hypotensive, tachycardic patient is volume. That is
    usually right and it is sometimes exactly wrong, and which one it is depends
    on a number that is not on this screen. Orb states the number and its date and
    lets the clinician decide — it does not tell them what to give.

    FIRES ONLY when the picture actually poses the volume question (hypotension
    or tachycardia). A raised creatinine on a patient who is neither is a lab
    result, not a flag.
    """
    if not creatinine:
        return []
    if not (picture["hypotensive"] or P_HR in picture["scoring_names"]):
        return []
    driver = P_SBP if picture["hypotensive"] else P_HR
    param = picture["scoring"].get(driver)
    return [
        _candidate(
            RULE_RENAL_VOLUME,
            fact=creatinine["display"] + " (raised)",
            hook=f"{driver} {_shown(param)} scoring {param.get('points') if param else '—'}",
            question="does this renal function change the calculus on volume here?",
            terms=[creatinine["name"], "creatinine", "renal"],
            numbers=[
                creatinine["value"],
                param.get("value") if param else None,
                param.get("points") if param else None,
            ],
            evidence={
                "lab": creatinine["name"],
                "value": creatinine["value"],
                "unit": creatinine["unit"],
                "age_days": creatinine["age_days"],
                "parameter": driver,
                "panel": creatinine.get("panel"),
            },
            source=(
                f"{SOURCE_RESULTS}: {creatinine['panel']}"
                if creatinine.get("panel")
                else SOURCE_RESULTS
            ),
            # `recorded` is deliberately None on both renal rules: `_lab_display`
            # already puts the age INSIDE the fact ("creatinine 2.5 mg/dL, today"),
            # and a meta line repeating the sentence above it is noise.
            relates_to=f"{driver} {_shown(param)}",
        )
    ]


def _rule_renal_drug(picture, meds, allergies, problems, creatinine) -> list[dict]:
    """A DRUG WHOSE HANDLING CHANGED WHEN THE KIDNEY DID.

    "On ramipril; creatinine 180 six days ago" is a sentence a clinician acts on
    and a score can never produce. The classes come from `classify_drug`
    (ace_inhibitors, nsaids, aminoglycosides); the four ingredients it has no
    class for — metformin, digoxin, lithium, spironolactone — are in
    :data:`_SCREEN_LEXICON` and are there because each is a drug whose
    accumulation in renal impairment is itself a medical emergency.

    Deliberately does NOT need the volume question: this fact stands on its own
    the moment both halves are true.
    """
    if not creatinine:
        return []
    renal_classes = {"ace_inhibitors", "nsaids", "aminoglycosides"}
    out = []
    for med in meds:
        shared = med["classes"] & renal_classes
        if not (shared or _lexicon_hit(med, "renally_handled")):
            continue
        out.append(
            _candidate(
                RULE_RENAL_DRUG,
                fact=f"{med['name']} {med['dose']}".strip()
                + " (on the chart) with "
                + creatinine["display"]
                + " (raised)",
                hook="both are on this patient's own chart",
                question="does this drug need holding, changing or re-dosing at this renal function?",
                terms=[*_name_terms(med["name"]), creatinine["name"], "creatinine"],
                numbers=[creatinine["value"]],
                evidence={
                    "drug": med["name"],
                    "dose": med["dose"],
                    "lab": creatinine["name"],
                    "value": creatinine["value"],
                    "unit": creatinine["unit"],
                    "age_days": creatinine["age_days"],
                    "panel": creatinine.get("panel"),
                },
                source=SOURCE_MEDICATIONS_AND_RESULTS,
                recorded=med.get("recorded"),
                # None, and that is the rule's own docstring in a field: this flag
                # deliberately does NOT need the picture, so it bears on no scoring
                # parameter and must not appear to.
                relates_to=None,
            )
        )
        break
    return out


def _rule_heart_failure(picture, meds, allergies, problems, creatinine) -> list[dict]:
    """HEART FAILURE CHANGES WHAT A FLUID BOLUS DOES.

    Same argument as the renal rule, from the other organ: the reflex volume in
    front of a hypotensive or hypoxic patient is the one thing that makes a
    decompensating ventricle worse. The problem is named back exactly as charted,
    because "consider heart failure" would be advice and "documented HFrEF on the
    problem list" is a fact.

    FIRES ONLY on hypotension, hypoxia or tachypnoea — the pictures where volume
    or oxygenation is actually the question.
    """
    if not (picture["hypotensive"] or picture["hypoxic"] or picture["tachypnoeic"]):
        return []
    for problem in problems:
        text = problem["text"]
        if not _HEART_FAILURE_RE.search(text):
            continue
        drivers = _other_drivers(picture)
        # WHICH ONE OF THE THREE GATES OPENED. `drivers` is every scoring
        # parameter and belongs in the hook; `relates_to` names the single
        # parameter that made this rule fire, because that is the number on the
        # panel a clinician should read the flag against.
        gate = (
            P_SBP
            if picture["hypotensive"]
            else next(iter(sorted(_SPO2_PARAMS & picture["scoring_names"])), P_O2)
            if picture["hypoxic"]
            else P_RR
        )
        return [
            _candidate(
                RULE_HEART_FAILURE,
                fact=f"Charted problem: {text}",
                hook=f"{', '.join(drivers) or 'the current picture'} scoring",
                question="does this change the volume or oxygenation decision here?",
                terms=[text, "heart failure", "cardiac"],
                numbers=[],
                evidence={"problem": text},
                source=SOURCE_PROBLEMS,
                recorded=problem.get("recorded"),
                relates_to=f"{gate} {_shown(picture['scoring'].get(gate))}",
            )
        ]
    return []


def _rule_blunted_fever(picture, meds, allergies, problems, creatinine) -> list[dict]:
    """A NORMAL TEMPERATURE ON A STEROID IS NOT EVIDENCE OF NO INFECTION.

    The mirror of the rate-control rule, and the same class of trap: NEWS2 scores
    a temperature, corticosteroids and immunosuppressants blunt the febrile
    response, so an afebrile patient on prednisolone who is deteriorating by every
    other parameter may be septic with nothing on the thermometer to show for it.

    FIRES ONLY when the temperature was MEASURED, is NOT scoring, and something
    else is. All three matter: an unmeasured temperature is a gap for the coverage
    note to report, and a patient who is already febrile does not need to be told
    their fever might be blunted.
    """
    temp = picture["measured"].get(P_TEMP)
    if temp is None or picture["febrile"]:
        return []
    others = _other_drivers(picture, P_TEMP)
    if not others:
        return []
    out = []
    for med in meds:
        if not _lexicon_hit(med, "immunosuppression"):
            continue
        out.append(
            _candidate(
                RULE_BLUNTED_FEVER,
                fact=f"{med['name']} {med['dose']}".strip()
                + " (immunosuppressant/steroid, on the chart)",
                hook=f"{P_TEMP} {_shown(temp)} is not scoring while "
                f"{', '.join(others)} {'is' if len(others) == 1 else 'are'}",
                question="could this be blunting the febrile response?",
                terms=[*_name_terms(med["name"]), "temperature", "fever", "afebrile"],
                numbers=[temp.get("value")],
                evidence={
                    "drug": med["name"],
                    "dose": med["dose"],
                    "parameter": P_TEMP,
                    "value": temp.get("value"),
                },
                source=SOURCE_MEDICATIONS,
                recorded=med.get("recorded"),
                relates_to=f"{P_TEMP} {_shown(temp)}",
            )
        )
        break
    return out


# ═════════════════════════════════════════════════════════════════════════════
#  RENDERING — the short list the model judges
# ═════════════════════════════════════════════════════════════════════════════
def render_candidates(result: dict) -> list[str]:
    """The candidate facts as prompt lines. Numbered, so a flag can be traced
    back to the fact that produced it, and DELIBERATELY the only clinical content
    in the prompt: the model is never shown the chart, so it cannot name anything
    the chart did not hand it."""
    lines = []
    for cand in result.get("candidates") or []:
        lines.append(f"{cand['id']}. {cand['fact']}: {cand['hook']}. [{cand['question']}]")
    return lines


def flag_signature(result: dict) -> str | None:
    """WHICH CHART FACTS ARE IN PLAY FOR THIS PATIENT, as one comparable string.

    THE PROBLEM IT EXISTS FOR, measured on the ward 2026-08-15: Suresh Reddy
    (PAT-A20FA685) was shown the IDENTICAL Ceftriaxone-allergy line eleven times
    in twenty-one minutes, and thirteen unreviewed drafts stacked up behind it.
    An allergy is a PERMANENT FACT — it does not become newly true because the
    NEWS2 moved from 12 to 14 — and eleven copies of a permanent fact is the
    textbook alert-fatigue failure: the ward learns to swipe the panel away, and
    the one flag that mattered goes with it.

    WHAT IS IN IT, AND WHAT IS DELIBERATELY NOT:

      * THE RULE AND THE FACT. `RULE_ANTIMICROBIAL_ALLERGY` plus "Allergy:
        Ceftriaxone — Anaphylaxis…" — the chart evidence, exactly as the flag
        will state it. A new drug, a new allergy or a first-time renal trigger
        changes this string, which is what makes "a NEW fact must always
        surface" a structural guarantee rather than an intention.
      * NOT THE HOOK. "Systolic BP 65mmHg scoring 3" is the deterioration, and
        it moves every observation cycle. Including it would make every
        signature unique, which is the same as having no signature at all.
      * NOT THE MODEL'S OUTPUT. This is Orb's own deterministic screen, so two
        alerts with the same signature are two alerts that would ask the model
        the identical question — and the answer is already on the record.

    Returns None when the screen COULD NOT RUN, which is not the same as finding
    nothing: an unreadable chart must never be compared equal to a read one, so
    the caller fails open and drafts. The empty string is a real answer — "Orb
    looked and there is nothing in play" — and compares equal to another one.
    """
    if not isinstance(result, dict) or result.get("screen_error"):
        return None
    return "\n".join(
        sorted(
            f"{c.get('rule')}|{_norm(str(c.get('fact') or ''))}"
            for c in (result.get("candidates") or [])
        )
    )


# ═════════════════════════════════════════════════════════════════════════════
#  THE OUTPUT FILTERS
#
#  All three are PURE and live here rather than in the caller, because all three
#  are statements about the FACTS. They run before a line is shown to anybody:
#  the fast lane pushes each complete step to the case room the moment it parses,
#  so a line filtered later would be a line retracted from under someone reading
#  it.
# ═════════════════════════════════════════════════════════════════════════════
DROP_GENERIC = "generic"
DROP_UNSOURCED = "unsourced"
DROP_FABRICATED = "fabricated"
#: A WORD ORB CANNOT ACCOUNT FOR, reported separately from `fabricated` on
#: purpose. "Orb recognised a drug the screen did not supply" and "Orb could not
#: place this word at all" are different confidences and a pilot has to be able
#: to tell them apart: the first is a certainty, the second is the new closed-
#: vocabulary rule and the only one that can plausibly delete a true sentence.
#: `sage_assessment_service._count_drops` labels the counter with whatever
#: string comes back, so this is one more series on the same metric and the rate
#: is visible from day one rather than after somebody thinks to look.
DROP_UNKNOWN_TERM = "unknown_term"

#: RULE 2, MADE DETERMINISTIC. Every one of these was observed on the ward screen
#: from the tier this replaces, and each is true of ANY patient with those
#: numbers. The prompt forbids them; this table is what happens when the prompt
#: is ignored, which on a 27B is often enough to matter.
#:
#: Matched case-insensitively against the WHOLE line (flag + reason), because the
#: same sentence in the WHY field is the same padding it is in the FLAG field.
_GENERIC_PHRASES: tuple[str, ...] = (
    # ── measured verbatim on screen, 2026-08-15 ──
    "escalate immediately",
    "repeat observations",
    "assess response to initial interventions",
    "assess response to intervention",
    "critical news2",
    # ── the rest of the generic-management vocabulary, same class ──
    "escalate to",
    "escalate urgently",
    "urgent senior review",
    "senior review",
    "call the registrar",
    "call the medical registrar",
    "inform the doctor",
    "repeat obs",
    "repeat the observations",
    "recheck observations",
    "continuous monitoring",
    "monitor closely",
    "close monitoring",
    "reassess in",
    "consider sepsis",
    "sepsis six",
    "abcde",
    "a to e assessment",
    "obtain iv access",
    "secure iv access",
    "give supplemental oxygen",
    "administer oxygen",
    "titrate oxygen",
    "send blood cultures",
    "take blood cultures",
    "check lactate",
    "arterial blood gas",
    "consider icu",
    "consider critical care",
    "review the patient",
)

_GENERIC_RE = re.compile("|".join(re.escape(p) for p in _GENERIC_PHRASES), re.IGNORECASE)

#: Tokens for the fabrication guard. Words shorter than this are not checked as
#: drug names — the same floor `drug_safety_service._MIN_SUBSTRING_MATCH` uses,
#: and for the same reason: short strings match an arbitrary slice of anything.
#: It is also the guard's own stated blind spot: a three-letter invention
#: ("give TXA") is not examined, which is why the floor is written down here
#: rather than buried in a regex.
_MIN_DRUG_TOKEN = 4
_TOKEN_RE = re.compile(r"[a-z][a-z0-9'\-]{2,}")
_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")


# ═════════════════════════════════════════════════════════════════════════════
#  THE CLOSED VOCABULARY — WHAT CATCHES A DRUG ORB HAS NEVER HEARD OF
#
#  THE HOLE THIS FILLS, MEASURED ON THIS APPLIANCE 2026-08-15. The guard used to
#  recognise a drug through three existing vocabularies — `classify_drug`'s
#  classes, the hospital formulary, the `drug_interactions` ingredient list — and
#  drop it if the screen had not supplied it. On this dev database the formulary
#  holds FOURTEEN ROWS, and VANCOMYCIN and NORADRENALINE are invisible to all
#  three (no class, not stocked, not an interaction ingredient). So the line
#  "Penicillin allergy — avoid vancomycin" SURVIVED: not because the guard judged
#  the word safe, but because it could not tell the word was a drug name at all.
#  Amiodarone and metronidazole were caught by ONE of the three and nothing else.
#
#  IT GETS WORSE WITH A REAL HOSPITAL, NOT BETTER. A loaded formulary means more
#  drugs the model knows and Orb has never heard of, sitting beside ones it has —
#  and a guard whose coverage nobody can state is a guard nobody should trust.
#
#  THE FIX IS TO INVERT THE QUESTION. "Is this token a known drug?" FAILS OPEN on
#  every word Orb has not heard of, and the words Orb has not heard of are
#  exactly the dangerous ones. "Does every clinically-shaped word in this line
#  come from what the model was actually shown?" FAILS CLOSED: a word this file
#  cannot account for is SUSPECT by default, and an unrecognised drug name is
#  precisely a word this file cannot account for.
#
#  WHAT MAKES THAT SAFE TO DO IS THE SHAPE OF THIS TIER, and it would not be safe
#  anywhere else in Orb. The model is shown at most four fact strings plus Orb's
#  own scoring block, and is asked for at most three lines of at most 24 words
#  each. Its vocabulary OUTSIDE the facts is therefore small, closed and
#  observable: 3,852 real lines from this lane on this ward use 847 distinct
#  words of four characters or more, and that is the whole distribution this
#  table has to cover. It is not a general English dictionary and could not be.
#
#  THE PREVIOUS VERSION OF THIS GUARD WAS TOO EAGER AND DELETED A TRUE FLAG on
#  one of the six patients that reach the model (see `verify_line`'s note about
#  amoxicillin charted inside an allergy reaction). That is the failure this
#  table is most likely to repeat, so three things hold it back: the words below
#  came from what this lane has actually written rather than from imagination,
#  inflections and British/American spellings fold onto one entry so the guard is
#  not a spelling test, and the measured false-positive rate is reported in the
#  test file beside the tests that pin it.
#
#  WHAT TO TELL A CIO, in the words this has to be defended in:
#
#    Sage's advice tier is only allowed to talk about facts Orb read out of this
#    patient's own chart. Before any flag reaches a screen, Orb checks every word
#    in it. A number must be one Orb itself calculated or read from the chart. A
#    word must be either one Orb put in front of the model, or ordinary clinical
#    English from a fixed list that contains no medicine names. Anything else —
#    including the name of a drug nobody has charted for this patient — is
#    deleted before a clinician sees it, whether or not Orb's own drug catalogue
#    happens to contain that medicine. What Orb does NOT promise: it cannot tell
#    a clinician the flag is clinically right, only that every name and number in
#    it came from this patient's record; a drug mentioned inside a charted note
#    can be repeated, because the chart said it; a family of drugs can be named
#    ("penicillins", "an antibiotic") where a specific medicine cannot; and a
#    very short abbreviation is not examined. The cost of the strictness is that
#    an unusual English word can cost a true flag — always a deletion rather than
#    a wrong drug on a screen, and counted continuously in production under its
#    own reason code.
#
#  THAT LAST COST IS NOT SMALL, AND THE HONEST NUMBER IS NOT THE ONE THIS COMMENT
#  USED TO QUOTE. "Zero deletions in the last 66 real lines this ward produced"
#  was measured on the sentences the table was BUILT FROM, which is circular: it
#  measures the table against itself. Re-measured 2026-08-15 on thirty flags
#  written fresh for these same patients, in three samples of ten, with the table
#  frozen between the second and third:
#
#      sample A (used to grow the table) ....... 0/10 deleted
#      sample B (held out) .................... 3/10 deleted
#      sample C (fresh, nothing tuned after) ... 3/10 deleted
#
#  Every single deletion was an ordinary English word the table did not hold —
#  "penalty", "steer", "mount", "concealed", "flatten", "table" — and not one was
#  an invention. So the rate on genuinely fresh true output is of the order of
#  THIRTY PERCENT OF LINES, not zero, and it does not converge by adding words:
#  the table grew from ~1,100 entries to ~2,000 between samples A and B and the
#  held-out rate did not move. English is open and this table is finite.
#
#  THE ALTERNATIVE THAT LOOKS LIKE A FIX IS WORSE, and it was tried and measured
#  on 2026-08-15 before being taken back out: gating this rule on "does the word
#  at least LOOK like a drug name?" drops the false-positive rate to near zero
#  and simultaneously lets `noradrenaline` and an invented `zephyrixin` through,
#  because neither carries a class marker. See :data:`_DRUG_SUFFIXES`. Given the
#  choice between a tier that sometimes says less than it knows and a tier that
#  sometimes names a drug nobody charted, this file keeps choosing the first.
#
#  WHAT THAT MEANS FOR THE PILOT, stated so nobody discovers it on a ward: this
#  tier currently delivers roughly two lines in three of what it correctly works
#  out. That is a PRODUCT decision, not a bug — it needs a ruling on whether a
#  quieter, always-checkable tier is the right trade, and the `unknown_term`
#  counter is what turns the question into a measurement in week one.
#
#  THIS IS NOT A FOURTH DRUG KNOWLEDGE BASE. It is the opposite of one: it
#  contains NO drug name, and `test_the_prose_lexicon_contains_no_drug_name`
#  asserts that against Orb's own three vocabularies. Nothing here says what a
#  drug is, does or interacts with; it says only "this word is ordinary clinical
#  prose, not a name". `rxnorm_corrector._ENGLISH_STOPWORDS` solves a similar
#  problem for phonetic matching and is deliberately NOT reused: it is 239 words
#  chosen for a different job, it already contains a drug name ("insulin"), and
#  coupling a safety filter to a list maintained for spell-correction would mean
#  a well-meaning edit there silently widens the hole here.
# ═════════════════════════════════════════════════════════════════════════════

#: ORDINARY ENGLISH — the connective tissue of a terse clinical sentence. Every
#: one of these has been observed in this lane's own output or is the plain
#: English a 16-word flag is written in.
_PROSE_ORDINARY = """
about above accord accumulate accuracy accurate across acid action active
actively activate activity add addition additional address adequate adjust
adjustment advance advice advise affect after again against aggressive ahead
aim alike allow alone along
already also although alter alternative always among amount another answer
anticipate anything apart appear appears apply applies appropriate area arise
around aside assist assistance associate assume attention attribute available avoidable
avoid away aren't
baseline benefit
balance base because become been before beforehand begin behind being below
beside besides best better between beyond blunt borderline both brief briefly
bring broad build
calculus call cannot can't capacity care careful carry case catch caught cause
causative caution cautious certain challenge chance change character check
choice
choose chosen clear close combine come coming common compare compensate
complete completely complex complicate component concern confirm confirmation
consequence consider consideration consistent constellation contain context
continue contribute contribution control correct correction correlate
correlation could count course cover cross crucial current
damage danger dangerous data date day decide decision decrease delay deliver
delivery depend describe deserve despite detail detect determine differ
difference different difficult direct discuss discussion distinguish document
does doesn't doing done double doubt down driver drive drop duration during
didn't don't
each earlier early easily effect effective effectiveness effort either element
else elsewhere emphasise empirical enable end enough ensure enter entire equal
escalate especially essential evaluate evaluation even evening event ever every
everything evidence evolve exact example except exchange exclude exhibit
existing expect experience expert explain explanation explicit expose extra
face fact factor fail failing fall false family favour feature feel find
finding fire first fit five fixed flow follow force form found four frequency
frequent from full fully further future
gain gave general generally give given goal gone good greater guide guideline
half hand happen hard harm have having help here hide high highest history hold
hour however
hasn't haven't identify ignore immediate immediately impair impact imply
important improve improvement inadequate include increase indicate induce
ineffective influence inform information initial initially initiate input
insight instead insufficient interact interaction interval intervene interpret
into involve irregular isn't issue item itself it's
judge just justify keep knew know known
lack large last late later latest lead leak leakage least leave left less level
life light like likely limit line list little local long look loss lost low
lower lowest
made main maintain major make manage management many mask massive matter
maximal mean measure measurement meet meeting mention method might minute
mislead mismatch miss mistake month more morning most move much multi multiple
must
name narrow nature near nearly necessary need never next night nine non none
normal normally note nothing notice number
objective obscure obtain obvious occult occur offer offset often omit once
onset overdose
ongoing only open optimise optimisation option order other others otherwise
outcome outreach output outweigh over overall overload overlook
part particular particularly past patch pattern penetrate perhaps period
persist persistent picture place plan plausible point poor possibility possible
possibly post potential potentially practice precede preclude predict prefer
prepare
present presentation prevent previous previously primary prior probable
probably problem procedure process produce profile progression prolong promote
prompt promptly prone proper prioritise priority protocol provide provoke
purpose
question quick quiet
raise range rapid rare rate rather ratio reach read reading ready real
react reactivity reassess reassurance reassure reason receive recent recently
recognise recognition recommend reconcile reconciliation reconsider record
recurrence reduce reflect reflex
regard regarding regular related relative relatively relevant reliable remain
remove render repair repeat replace report represent require requirement
resolve respond rest restrict result retain return reveal reversible review
right rise risk risky room rule
safe safely safer safety same save second secondary seek seem seen senior sense
separate series setting settle seven several share short shortly should show
shown side sign significant similar simple simply since single site situation
six size slight slow small some soon source spare specialist specific
specifically stand standard start state status stay step still stop
straightforward strain strategy strong strongly studies study sub subtle such
suggest suggestion suggestive suitable support sure suspect suspicion system
take taken target team tell temp tend term than that their them then there
therefore thereof these they thing think third this thorough those though
thought threat threaten three threshold through throughout time timely timing
today
together told tolerate tomorrow tonight took total toward towards track
trajectory transfer trend trigger true trust turn twelve twice type typical
uncertain unclear under underlying understand unless unlikely unmask
unnecessary unplanned unreliable until unusual upon urgency urgent urgently
usable used useful using usual usually
value various very view visible
wait want warn warrant watch weigh well were what when where whether which
while white whole whose will window with within without withhold work worse
worsen worth would write written wrong
year yesterday yield your week weekly overnight
"""

#: THE ENGLISH THE TABLE ABOVE WAS MISSING, ADDED 2026-08-15.
#:
#: WHY THIS BLOCK IS BIG. The closed-vocabulary rule deletes a WHOLE LINE for one
#: word it cannot place, so this table's completeness IS the tier's
#: false-positive rate. It had been grown only from lines this lane had already
#: produced, which is circular — it measures the table against the sentences the
#: table was built from. Measured instead against ten FRESH flags of the kind a
#: good model writes for these patients, four died: "belongs", "whilst",
#: "understates", "alongside". A 40% deletion rate on true, checkable output,
#: and every one of them ordinary English rather than an invention.
#:
#: THE FIX COSTS THE GUARD NOTHING, which is the argument for doing it this way
#: rather than by softening the rule. The promise is "every word is either from
#: this patient's chart or from a fixed list that contains no medicine names".
#: Adding words that are not medicines keeps that sentence exactly true: drug
#: coverage is decided by what is ABSENT from these tables, never by how few
#: words are present. A shape test on the model's output was the cheap
#: alternative and it silently surrenders every drug with no class marker — see
#: :data:`_DRUG_SUFFIXES`.
#:
#: NO WORD HERE IS A SUBSTANCE. No medicine, no lab analyte, no electrolyte —
#: the same rule the blocks above follow, asserted three ways by
#: `test_the_prose_lexicon_contains_no_drug_name`: against `classify_drug`,
#: against the formulary and interaction vocabularies, and against drug
#: morphology (:func:`has_drug_morphology`).
_PROSE_ORDINARY_2 = """
alongside amid amidst anyone anywhere apparent apparently approach argue
argument arrange arrival arrive aspect attempt attend await aware awareness
backup bear beat began behalf belong beneath benign bias bind blind board
bottom boundary break broken
capable capture central certainly chain characteristic claim clarify clarity
classic clue cluster collect combination comfort comment commit commonly
communicate communication compatible conclude conclusion confidence confident
conflict connect consequently considerable considerably consist constant
constantly constrain constraint contact continual continuous contrary
convert convince core correspond cost create criterion cue curve cycle
deal deep deeply defer define definite definitely definition degree demand
demonstrate denote deny depth derive design desirable desire deviate deviation
difficulty diminish disagree disappear discard disclose discover dismiss
disorder displace disproportionate disrupt distant distinct distinctly diverge
dominant dominate downward dramatic draw duplicate dynamic
ease easier easy edge elapse eligible eliminate emerge emphasis employ empty
encounter encourage endanger endure engage enhance enlarge entail entry equally
equivalent exceed excess excessive exclusive exert exhaust exist expand
expansion explore express extend extension extensive extent external extreme
extremely
facilitate fair fairly familiar far fast faster fatal favourable favorable fear
feasible feed few fewer figure final finally flag flat flexible fluctuate
fluctuation focus fold forget former formerly forward fragile free frequently
fresh front fundamental furthermore
gap gather gentle genuine glance govern gradual gradually grade grave great
greatly ground group grow growth guard guess
halt handle harmful harmless head heavy hence herself hesitate hidden himself
hint huge human hundred
idea ideal identical illustrate impede imperative implement implication impose
impossible impression improbable inability inaccurate inappropriate incidence
incident incidental incomplete inconsistent incorrect indeed independent index
indirect individual inevitable infer inference inherent injure inner insert
insist inspect instance instant instruct instruction intact integrate intend
intent intention interfere interference intermediate intermittent internal
intolerance intolerant introduce invalid inverse invert involvement irrelevant
irreversible isolate
join joint judgement judgment junior
key kind knowledge
label lag largely latter learn legitimate length lengthy lesser lethal liable
likelihood likewise limitation link load locate location logic logical longer
loose lose
magnitude mainly maintenance majority mandatory manner manual mark marked
markedly match material mature meaningful means meanwhile measurable mechanism
member merely merit middle mind minimal minimise minimum minor misinterpret
misread modest modify moment momentary monthly moreover mostly multiply
namely nearby necessarily negative neglect negligible neither neutral
nevertheless newly nonetheless notable notably noted notify notwithstanding
novel nowhere
object objection oblige obstacle obviously occasion occasional occasionally odd
omission onward opposite oppose optimal optimum ordinary organise origin
original originally ought ourselves outset outside outstanding overcome
overestimate overt overtly owing own
pace pair parallel partial partially partly passive path pathway pause peak
pending people perceive percent percentage perfect perform performance
permanent permit persistently personal personnel perspective phase pick pitfall
pivotal plainly plus policy poorly population portion pose position positive
postpone power practical practically precaution precise precisely precision
predictable prediction predominant preference preferable preferably preliminary
premature prerequisite presence press pressing presume prevalence prevention
principal principle probability profound progressive progressively prohibit
prominent pronounced proof proportion proportional propose proposal prospect
prospective protect protection prove proven provision proximal pull purely
pursue push
qualify quality quantify quantity quarter query quickly quite
random randomly rapidly rarely rationale readily readiness realise reality
really reasonable reasonably reassessment rebound recall recommendation recur
recurrent reduction refer reference referral refine refuse regardless region
regularly regulate reject relation relationship release reliably reliance
relief relieve rely remainder remark remarkable remind reminder repeatedly
replacement request resemble reserve residual resolution resource respect
respective respectively restore restriction resume retention revert revise
revision roughly round route routine routinely run
satisfy scenario schedule scope search secure seemingly seldom select selection
self send sensible sensitive sensitivity sequence serial serious seriously
serve service set shape sharp sharply shift shorten shortness significance
significantly silent silently similarly simultaneous simultaneously skip
slightly slower slowly smaller solely solve somewhat somewhere sooner span
special speed split spot spread stage stated statement steady steadily steep
stem stopping store straight strength stress strict strictly subject subsequent
subsequently substantial substantially substitute subtly succeed success
successful successfully sudden suddenly sufficient sufficiently summarise
summary superior supply suppose supposed suppress surprise surround survival
survive suspend sustain sustained switch symptomatic
tailor task technique temporary temporarily test themselves thereafter thereby
thin thoroughly thus tight tighten tolerance tolerable top trace traditional
transient transition translate transport trial trivial trouble troubling truly
try twenty two typically
unable unaware uncommon undergo understate understatement undertake undesirable
undetected undue unexpected unexplained unfavourable unhelpful unique unit
unrecognised unrelated unresolved unresponsive upper upward urge use
vague valid validate validity vary variable variation verify via vigilance
vigilant virtually visit vulnerable
warning wary weak weaken weakness wean weight whatever whenever whereas whereby
wherever whilst whom wide widely widespread willing withdraw withdrawal witness
worry worrying worthwhile
yet yourself
"""

#: CLINICAL PROSE THAT IS NOT A SUBSTANCE — physiology, signs, syndromes,
#: organs, investigations, care process. DELIBERATELY EXCLUDES every orderable
#: or measurable substance: no drug, and no lab analyte either (creatinine,
#: potassium, lactate, glucose). An unsupplied analyte name is an invented
#: measurement, which is the same class of invention as an unsupplied drug and
#: is caught here by its absence rather than exonerated by its presence.
_PROSE_CLINICAL = """
abdominal abnormal abnormality abscess absorption accessory accumulation
acidosis acidotic acuity acute administer administration admission admit
afebrile agent airway alert altered anaemia anaphylaxis angio-oedema anxiety
apnoea ards arrest arrhythmia arterial aspirate aspiration assess assessment
asthma atelectasis auscultate
bacteraemia bedside biopsy bleed bleeding blood bolus bowel bradycardia
bradycardic breath breathing bronchospasm bundle
cannula cannulate cannulation capillary cardiac cardiogenic cardioversion
cardiovascular catheter catheterisation cellulitis cerebral chart charted chest
chronic circulation circulatory
clinical clinically clinician clot coagulate coagulation coagulopathy collapse coma
comorbid comorbidity compensatory complication compromise condition confuse
confusion conscious consciousness consult consultant consultation
contraindicate contraindicated contraindication coverage criteria critical
critically culture
cell clearance cumulative decline decompensate decompensated decompensation
decortication dehydration delirium dengue dependent deplete depletion depress
depression derangement deteriorate deteriorating deterioration diabetic
diagnose diagnosis diagnosed dialysis diastolic differential disability
discharge disease distress distribute distributive doctor dosage dose dosing
drain drainage drug duodenal duodenum dysfunction
echo echocardiogram efficacy effusion electrolyte elevate elevated embolism
emergency emergent empiric empyema endocarditis endoscopy epilepticus
escalation exacerbate exacerbation exam examination examine exposure excretion
failure fatigue febrile fever flare fluid function
gastrointestinal
haemodynamic haemodynamics haemorrhage haemorrhagic handling heart heart-rate
hepatic hfpef hfref hydration
hypercapnia hyperglycaemia hyperkalaemia hypertension hypertensive
hypoglycaemia hypokalaemia hypoperfuse hypoperfusion hypotension hypotensive
hypothermia
hypoventilation hypovolaemia hypovolaemic hypoxaemia hypoxia hypoxic
ictal illness imaging imbalance immune immunosuppress immunosuppressed
impairment impending infarct infarction infect infection infectious inflame
inflammation inflammatory infuse infusion injury instability insufficiency
intervention intracranial intravascular intravenous intubate intubation
invasive investigate investigation ischaemia ischaemic
jaundice ketoacidosis ketosis kidney
lab laparoscopy laparotomy leukocytosis liver lung
malaria margin marker mechanical medical medication meningitis mental mentation
metabolic metabolism monitor monitoring mortality muscle myocardial
nasal neurological neutropenia nurse
observe obstruct obstruction oedema oliguria organ oximetry oxygen oxygenate
oxygenation
pain pancreatitis parameter pathology patient perforate perforation perfuse
perfusion peripheral peritonitis pharmacological physical physiological
physiology pleural pneumonia pneumothorax polymicrobial postoperative
post-operative pressure prescribe prescribed procedural prognosis progress
prophylaxis pulmonary pulse puncture pyelonephritis
radiograph reaction rebreather recovery refill regimen registrar relate renal
reperfusion resist resistance respiration respiratory response responsiveness
resuscitate resuscitation reversal reverse rhythm
operate operation saturation scan score screen screening sedate sedation
seizure sepsis septic severe surgeon
severity shock shunt skin sound spectrum stability stable stridor sub-massive
submassive superimposed supplemental surgery surgical symptom syndrome systemic
systolic
tachycardia tachycardic tachypnoea tachypnoeic telemetry temperature theatre
theater therapeutic therapy therapies thrombocytopenia thrombosis tissue titrate
titration toxic toxicity transfuse transfusion trauma treat treatment
thrombolysis thrombolytic trough tube
ulcer ultrasound undifferentiated unstable unwell urine urinary
vascular ventilate ventilation ventilator ventilatory ventricle ventricular
viral vital volume vomit vomiting
ward workup wound x-ray
"""

#: DRUG CLASSES, NOT DRUG NAMES — and the distinction is the whole point. A class
#: term cannot send a clinician to a specific wrong drug, and `_known_drug_tokens`
#: already excludes the interaction table's class rows for exactly this reason.
#: Anything `classify_drug` recognises is dropped BEFORE this table is consulted
#: (see `verify_line`'s ordering), so a class name here can never exonerate a
#: drug name.
#:
#: SULFA IS A CLASS TERM AND NOT A MEDICINE, exactly as "lactam" is, and Orb
#: already says so in its own vocabulary: `drug_safety_service
#: ._ALLERGEN_CLASS_TERMS["sulfonamides"]` lists "sulfa" and "sulpha" as the way
#: a clinician CHARTS the class. Its absence here was measured deleting the true
#: line "Sulfonamide allergy is charted — the sulfa-based first-line option is
#: ruled out for this patient" on a patient whose chart holds exactly that
#: allergy: the flag was reading the chart back correctly and naming no medicine
#: at all, and the guard ate it. A patient with no sulfonamide allergy cannot
#: write that sentence anyway — `names_a_fact` still requires the line to name
#: the subject of a supplied fact.
_PROSE_CLASSES = """
analgesia analgesic anaesthesia anaesthetic antibiotic antiemetic antimicrobial
anticoagulant anticoagulate anticoagulated anticoagulation antiplatelet
antihypertensive
beta blockade blocker bronchodilation bronchodilator corticosteroid diuretic
immunosuppressant immunosuppression inhibitor inotrope lactam opioid sedative
statin steroid sulfa sulpha vasodilator vasopressor
"""

#: ORB'S OWN WORDS — the vocabulary of the panel the clinician is looking at and
#: of the fact strings this file writes. Most reach the model per-patient
#: through `_supplied_tokens`; these are the floor, so a flag quoting the screen
#: back is never deleted for it on a patient whose facts happen not to say it.
_PROSE_ORB = """
acvpu allerg allergen allergic allergy band breaths celsius class daily
driving hourly mild minutes mmhg mmol moderate news2 observation observations
parameters pinned points reading recorded scale scoring severe spo2 stat
today umol unknown value worsening
bipap cmh2o cpap fio2 hco3 paco2 pao2 pco2 peep qsofa
"""


def _fold(word: str) -> str:
    """ONE SPELLING PER WORD, so the lexicon is not a spelling test.

    British and American clinical English differ on the words this tier uses
    most (haemorrhage/hemorrhage, oedema/edema, anaemia/anemia), and a guard that
    deleted a true flag because the model wrote the American spelling would be
    the "too eager" failure all over again. Folded the SAME way on both sides —
    the lexicon at import and the token at check time — so the mapping only has
    to be consistent, not linguistically pure.
    """
    w = word.strip().lower().replace("ae", "e").replace("oe", "e")
    return w.replace("iz", "is").replace("yz", "ys")


#: Inflections folded onto one entry. Same purpose as `_fold`: the lexicon holds
#: "bleed" and the model may write "bleeding", "bleeds" or "bled" — and a guard
#: that deleted the line for the difference would be deleting true output over
#: grammar.
_SUFFIX_RULES: tuple[tuple[str, str], ...] = (
    ("'s", ""),
    ("s'", ""),
    ("ies", "y"),
    ("ied", "y"),
    ("iest", "y"),
    ("ier", "y"),
    ("ily", "y"),
    ("es", ""),
    ("s", ""),
    ("ing", ""),
    ("ing", "e"),
    ("ed", ""),
    ("ed", "e"),
    ("est", ""),
    ("er", ""),
    ("er", "e"),
    ("ically", "ic"),
    ("ally", "al"),
    ("ly", ""),
    ("ion", ""),
    ("ion", "e"),
    ("ive", ""),
    ("ive", "e"),
    ("ness", ""),
    ("able", ""),
    ("able", "e"),
    ("ible", ""),
)


#: PRODUCTIVE PREFIXES — the front half of a word an ordinary clinical sentence
#: builds on the spot. Stripped ONLY when what is left is itself prose (see
#: `_one_stem`), so this can never turn a coined name into English: it is the
#: same decomposition rule `is_prose` applies to hyphens, applied to the
#: prefixes English does not bother to hyphenate.
#:
#: "un" leads, because negation is the commonest of them and was measured
#: costing a true flag on its own.
_PROSE_PREFIXES: tuple[str, ...] = (
    "un",
    "re",
    "non",
    "pre",
    "post",
    "over",
    "under",
    "anti",
    "inter",
    "intra",
    "sub",
    "multi",
    "semi",
    "self",
    "mis",
    "dis",
    "hyper",
    "hypo",
    "ultra",
    # Clinical combining forms. Same rule: "nephro" + "toxic" survives because
    # "toxic" is prose; "nephro" + anything that is not prose does not.
    "nephro",
    "cardio",
    "hepato",
    "neuro",
    "gastro",
    "broncho",
    "pulmo",
    "thrombo",
    "vaso",
    "angio",
    "cerebro",
    "immuno",
    "electro",
    "myo",
)


def _stem_forms(word: str):
    """The word, then the base forms it could be an inflection of.

    TWO PASSES, because clinical English stacks its endings: "accordingly" is
    "according" is "accord", "administering" is "administer". One pass would
    leave the outer layer on and delete the line over a suffix.
    """
    seen = {word}
    yield word
    for _ in range(2):
        for base in list(seen):
            for form in _one_stem(base):
                if form not in seen:
                    seen.add(form)
                    yield form


def _one_stem(word: str):
    # NEGATION IS NOT A NEW WORD. "unsafe", "uncontrolled", "untreated" and
    # "unexplained" are the sentences this tier writes about a chart fact, and
    # MEASURED on the ward 2026-08-15 the missing "unsafe" alone deleted a true
    # anticoagulation flag. A drug name is not rescued by it: "Unasyn" without
    # its prefix is "asyn", which is not prose either.
    #
    # THE SAME ARGUMENT FOR THE OTHER PRODUCTIVE PREFIXES. "rechecked",
    # "nephrotoxic", "postoperatively", "intravascularly" are one ordinary word
    # with a productive prefix on the front, and each of them deleted a true
    # line (measured on the held-out sample, 2026-08-15). This is SAFETY-NEUTRAL
    # in the only way that matters: the prefix is stripped and the REMAINDER
    # still has to be ordinary prose in its own right, and a drug name does not
    # decompose that way — "cefepime" minus a prefix is not English, which is
    # the same argument `is_prose` already makes about hyphens.
    for prefix in _PROSE_PREFIXES:
        if word.startswith(prefix) and len(word) - len(prefix) >= 4:
            yield word[len(prefix) :]
    for suffix, replacement in _SUFFIX_RULES:
        if not word.endswith(suffix) or len(word) - len(suffix) < 3:
            continue
        base = word[: -len(suffix)] + replacement
        yield base
        # "stopped" → "stop", "occurring" → "occur".
        if not replacement and len(base) >= 4 and base[-1] == base[-2] and base[-1] not in "aeiou":
            yield base[:-1]


_PROSE = frozenset(
    _fold(w)
    for chunk in (_PROSE_ORDINARY, _PROSE_ORDINARY_2, _PROSE_CLINICAL, _PROSE_CLASSES, _PROSE_ORB)
    for w in chunk.split()
)

#: The class block on its own. `_known_drug_tokens` subtracts it, because the
#: interaction seed types a few class names ("diuretics", "statins",
#: "inhibitors") as INGREDIENT rows, and a class name read as a drug name makes
#: "the ACE inhibitor may need holding" a fabricated medication. The module's
#: rule is stated once, here, and both directions obey it.
_PROSE_CLASS_TERMS = frozenset(_fold(w) for w in _PROSE_CLASSES.split())


def _is_class_term(token: str) -> bool:
    """True for a DRUG CLASS word ("diuretics", "inhibitors", "statins").

    Not a drug name, and the distinction is load-bearing in both directions: a
    class cannot send a clinician to a specific wrong drug, so it is prose here
    and it is subtracted from the drug DETECTOR in `_known_drug_tokens`.
    """
    return any(form in _PROSE_CLASS_TERMS for form in _stem_forms(_fold(token)))


#: Drug morphology. A generic drug name is a manufactured word and it shows: the
#: stem is arbitrary but the ENDING is a WHO/USAN class marker, which is why
#: -cillin, -mycin and -olol read as drugs to a clinician who has never met the
#: specific molecule. This is not a drug list — it is what makes a word
#: drug-SHAPED.
#:
#: WHAT IT IS FOR, AND WHAT IT IS DELIBERATELY NOT FOR (2026-08-15). It is used
#: by :func:`has_drug_morphology` to police THIS FILE'S OWN PROSE TABLES — the
#: guard on the guard, `test_the_prose_lexicon_contains_no_drug_name`. It is NOT
#: used to decide whether an unaccounted-for word in model output survives.
#:
#: That second use was tried and measured, and it fails in the one direction
#: this tier cannot afford. Gating the closed-vocabulary rule on "does this look
#: like a name?" keeps ordinary English alive, but a drug name is only sometimes
#: shaped like one: NORADRENALINE and an invented ZEPHYRIXIN carry no class
#: marker at all and are written in lower case, so both walked straight through
#: and "Penicillin allergy charted — noradrenaline may be needed" reached the
#: alert screen on a patient with no noradrenaline anywhere. Morphology is a
#: sufficient signal, never a necessary one, and a guard built on it fails OPEN
#: on exactly the words nobody has heard of — which is the hole this whole
#: section exists to close. The false positives that motivated the gate are real
#: and are fixed where they belong: in :data:`_PROSE_ORDINARY`.
_DRUG_SUFFIXES: tuple[str, ...] = (
    "cillin",
    "mycin",
    "micin",
    "cycline",
    "azole",
    "oxacin",
    "floxacin",
    "penem",
    "cephem",
    "ceph",
    "cef",
    "sulfa",
    "pril",
    "sartan",
    "olol",
    "dipine",
    "statin",
    "parin",
    "xaban",
    "gatran",
    "azepam",
    "azolam",
    "barbital",
    "caine",
    "profen",
    "coxib",
    "tinib",
    "ximab",
    "zumab",
    "mab",
    "vir",
    "navir",
    "ovir",
    "prazole",
    "tidine",
    "setron",
    "triptan",
    "glitazone",
    "gliptin",
    "gliflozin",
    "formin",
    "phylline",
    # Specific -ide class endings only. A bare "ide" matches "provide",
    # "guide" and "outside" — measured, and it deleted a true line.
    "emide",
    "izide",
    "amide",
    "onide",
    "oxide",
    "apride",
)


def has_drug_morphology(token: str) -> bool:
    """True when a word carries a WHO/USAN class marker — i.e. it is shaped like
    a generic drug name, whatever it happens to mean.

    THE GUARD ON THE GUARD, and the only thing this is for. The closed
    vocabulary below works only if the prose tables hold no drug name; this is
    how a leak is caught by SHAPE rather than by a reviewer's memory, alongside
    the check against Orb's three real drug vocabularies. See
    `test_the_prose_lexicon_contains_no_drug_name`.

    NOT USED ON MODEL OUTPUT — see the note on :data:`_DRUG_SUFFIXES`. A word
    with this shape is certainly a name; a word without it is not certainly
    anything, and `noradrenaline` is the counter-example that costs a clinician.

    `_PROSE_CLASSES` is exempt from the check by design: a class term legitimately
    carries the class marker it names ("statin", "lactam", "sulfa"), and a class
    cannot send a clinician to a specific wrong drug.
    """
    folded = _fold(token)
    return any(folded.endswith(suffix) for suffix in _DRUG_SUFFIXES)


def is_prose(token: str) -> bool:
    """True when this word is ordinary clinical prose rather than a name.

    Public because it is the boundary of the guard's promise: what this returns
    False for is what gets deleted, and a reviewer should be able to ask it.
    """
    folded = _fold(token)
    if any(form in _PROSE for form in _stem_forms(folded)):
        return True
    # A COMPOUND IS PROSE ONLY IF EVERY PART IS. "sepsis-induced" and
    # "first-line" survive; "piperacillin-tazobactam" does not, which is the
    # case this rule exists for — a hyphen is the commonest way a drug name
    # arrives looking like two ordinary words.
    parts = [p for p in re.split(r"[-']", folded) if len(p) >= 3]
    if not parts or parts == [folded]:
        return False
    return all(any(form in _PROSE for form in _stem_forms(p)) for p in parts)


def is_generic(line: str) -> bool:
    """True when the line would be true of ANY patient with those numbers."""
    return bool(line) and bool(_GENERIC_RE.search(line))


def names_a_fact(line: str, result: dict) -> bool:
    """RULE 3, ENFORCED: every flag names its source fact.

    This is the positive half of the filter and it is the stronger one — a line
    that names no drug, no lab and no allergy is a line about nothing in
    particular, which is the definition of the padding this tier is not allowed
    to produce. It also happens to catch every generic phrase on its own, with
    :data:`_GENERIC_PHRASES` as belt to its braces.
    """
    text = _norm(line)
    if not text:
        return False
    terms = result.get("allowed_terms") or ()
    if any(term in text for term in terms):
        return True
    # INFLECTION TOLERANCE. A chart and a sentence do not agree on plurals: the
    # allergy is charted "Sulfonamides" and the flag reads "Sulfonamide allergy
    # is charted". A literal substring test calls that line unsourced and
    # deletes it — measured 2026-08-15 on exactly the shape a GOOD flag takes.
    # Matching stem-to-stem keeps the rule ("the line must name the subject of a
    # fact") while letting the two spellings of one fact meet. Bounded by
    # _MIN_DRUG_TOKEN so a short fragment cannot buy a ticket.
    token_stems = {
        form
        for token in _TOKEN_RE.findall(text)
        if len(token) >= _MIN_DRUG_TOKEN
        for form in _stem_forms(token)
    }
    for term in terms:
        for word in _TOKEN_RE.findall(_norm(term)):
            if len(word) < _MIN_DRUG_TOKEN:
                continue
            if any(form in token_stems for form in _stem_forms(word)):
                return True
    return False


def verify_line(line: str, result: dict) -> str | None:
    """THE FABRICATION GUARD. Returns the drop reason, or None to keep the line.

    Every drug name and every number in the output must have come from the
    pre-screen. The model is not shown the chart, so anything else came from its
    own memory — and a model inventing a comorbidity, a dose or a lab value on
    the busiest screen in the hospital is the worst outcome available here.

    IT ASKS TWO QUESTIONS OF EVERY WORD, IN THIS ORDER, and the order is the
    design:

      1. DOES ORB RECOGNISE THIS AS A DRUG? `classify_drug`'s classes, the
         hospital formulary, the `drug_interactions` ingredient list and this
         patient's own chart — three existing vocabularies, no fourth knowledge
         base. A recognised drug the screen did not supply is a CERTAIN
         invention and is dropped as `fabricated`.
      2. CAN THIS FILE ACCOUNT FOR THE WORD AT ALL? Question 1 fails OPEN on
         everything Orb has never heard of, which on a 14-row dev formulary is
         vancomycin and noradrenaline and on a real ward is a much longer list.
         So a word that is neither in the facts the model was shown nor ordinary
         clinical prose (:func:`is_prose`) is SUSPECT BY DEFAULT and dropped as
         `unknown_term`. This is the closed-vocabulary half, and it is what makes
         "avoid vancomycin" impossible to write on a chart that never said it.

    THE EXACT GUARANTEE, STATED HONESTLY, because a guard nobody can bound is a
    guard nobody should trust. A line survives only if EVERY word of four
    characters or more in it is one of: a term the pre-screen's rules are about,
    a word the model was actually shown (the fact strings and Orb's own scoring
    block), or ordinary clinical prose from a fixed table that contains no drug
    name. A drug name is none of those three, whether or not Orb has heard of it.
    What that does NOT promise: three-letter inventions are not examined
    (`_MIN_DRUG_TOKEN`), a drug named INSIDE a charted fact may be repeated
    because the chart said it first, and a class term ("antibiotic",
    "beta-blocker") is prose rather than a name — deliberately, since a class
    cannot send a clinician to a specific wrong drug.

    Numbers are the STRICTER half and have no gap at all: every figure in the
    line must be one the screen supplied or one of Orb's own pinned scoring
    values, so an invented dose or lab result is dropped whatever word it sits
    next to.
    """
    if is_generic(line):
        return DROP_GENERIC
    if not names_a_fact(line, result):
        return DROP_UNSOURCED

    allowed_terms = result.get("allowed_terms") or frozenset()
    supplied = result.get("supplied_tokens") or frozenset()
    known = result.get("known_drug_tokens") or frozenset()
    text = _norm(line)

    for token in _TOKEN_RE.findall(text):
        if len(token) < _MIN_DRUG_TOKEN:
            continue
        if any(token in term or term in token for term in allowed_terms):
            continue
        # ANYTHING THE SCREEN ACTUALLY PRINTED IS SOURCED, WHATEVER RULE PUT IT
        # THERE. `terms` is the rule's own idea of what it is about — the drug it
        # matched, the allergen, the lab — but the FACT STRING handed to the
        # model routinely carries more than that, because it is charted prose:
        # "Allergy: Penicillin — widespread rash 48h into a course of
        # AMOXICILLIN". A model that then writes "penicillin allergy may
        # preclude amoxicillin" has quoted the chart back at us exactly as
        # asked, and deleting that line as an invention is the tier destroying
        # its own true, checkable output — the failure this whole design says is
        # worse than the padding it replaces. Measured on the seeded ward
        # 2026-08-15: it deleted the only flag on 1 of the 6 patients that reach
        # the model.
        #
        # NUMBERS HAVE WORKED THIS WAY SINCE THE FIRST VERSION (`_candidate`
        # harvests every figure out of the rendered fact and hook), so this is
        # the drug half catching up with the number half rather than a new
        # licence. The rule stays exactly as strict where it matters: the model
        # is shown ONLY these fact strings, so "the screen printed it" and "the
        # chart supplied it" are the same statement.
        #
        # DELIBERATELY NOT ADDED TO `allowed_terms`, which is what
        # `names_a_fact` tests. That set stays the rule's own drug/allergen/lab
        # vocabulary, so a line still has to name the SUBJECT of a fact to
        # survive and cannot buy its ticket with an incidental word like "rash".
        # STEM-AWARE, because a chart and a sentence inflect differently. The
        # allergy is charted "Sulfonamides" and the flag reads "Sulfonamide
        # allergy is charted" — the same fact, one letter apart, and a literal
        # comparison deleted the line for it (measured 2026-08-15). `supplied`
        # is only ever the text the model was actually shown, so widening it to
        # inflections cannot admit anything the chart did not supply.
        if token in supplied or any(form in supplied for form in _stem_forms(token)):
            continue
        if token in known or classify_drug(token):
            return DROP_FABRICATED
        # THE INVERSION, AND THE ONLY RULE HERE THAT CAN DELETE A TRUE SENTENCE.
        # Everything above asks "is this a drug?" and lets the word through when
        # it cannot tell — which is precisely how "avoid vancomycin" survived on
        # a formulary that does not stock vancomycin. This asks the opposite
        # question, of a table that holds no drug names: is this word ordinary
        # clinical prose? If it is not, then it is a NAME the model produced from
        # its own memory rather than from the four facts it was shown, and on
        # this surface a confident, checkable-looking name is the most damaging
        # thing it can invent.
        #
        # The cost is stated rather than hidden: an uncommon but legitimate
        # English word deletes its line. That is measured (see the test file's
        # false-positive figure), reported under its OWN drop reason so it can
        # keep being measured in the pilot, and bounded by `_MIN_DRUG_TOKEN` and
        # by everything above it — a word already in the facts, in the rules'
        # terms, or in Orb's own scoring block never reaches this line.
        #
        # NO SHAPE TEST GUARDS THIS LINE, and that was tried. Asking "does the
        # unaccounted-for word at least LOOK like a name?" first is the obvious
        # way to stop deleting true sentences over ordinary English, and it
        # halves the whole rule: `noradrenaline` carries no class marker and an
        # invented `zephyrixin` carries none either, so both survive and the
        # measured hole reopens under a different name. The fix for the false
        # positives is `_PROSE_ORDINARY` — a bigger table of words that are not
        # medicines costs the guard nothing, while a shape test costs it the
        # drugs it cannot recognise. See :data:`_DRUG_SUFFIXES`.
        if not is_prose(token):
            return DROP_UNKNOWN_TERM

    allowed_numbers = result.get("allowed_numbers") or frozenset()
    for raw in _NUMBER_RE.findall(text):
        if _number_key(raw) not in allowed_numbers:
            return DROP_FABRICATED
    return None


def _number_key(value) -> str:
    """One spelling for a number, so "50", "50.0" and 50 compare equal."""
    if value is None:
        return ""
    try:
        return f"{float(value):g}"
    except (TypeError, ValueError):
        return ""


def _allowed_terms(candidates: list[dict]) -> frozenset:
    terms: set[str] = set()
    for cand in candidates:
        for term in cand.get("terms") or []:
            if len(term) >= 3:
                terms.add(term)
    return frozenset(terms)


def _supplied_tokens(candidates: list[dict], tier1: dict | None = None) -> frozenset:
    """Every word the model was actually SHOWN — the facts, and Orb's own block.

    The fabrication guard's question is "did this come from the chart, or from
    the model's memory?", and because the model is handed the fact strings and
    nothing else, the honest test is whether the word appears in what it was
    handed. `render_candidates` is the exact text that reaches the prompt, so
    this reads the same two fields it does.

    TIER 1'S BLOCK COUNTS TOO, and it is the same argument rather than a
    concession. `sage_assessment_service._tier1_prompt_block` puts Orb's own
    deterministic scoring at the top of the prompt — the parameter names, their
    displayed values, the band, the trend, the coverage note — so a flag saying
    "Systolic BP" or "consciousness" is quoting the prompt, not remembering
    something. `_allowed_numbers` has always treated those figures this way; this
    is their words following their numbers. It cannot widen the drug hole: that
    block is NEWS2 arithmetic and contains no drug name, and anything Orb
    recognises as a drug is dropped by the check ABOVE this set is consulted for.

    Used ONLY to exonerate a token in :func:`verify_line` — never to satisfy
    :func:`names_a_fact`, which is the positive "this flag names its source
    fact" rule and must keep its narrower vocabulary.
    """
    tokens: set[str] = set()

    def _add(text: str) -> None:
        tokens.update(t for t in _TOKEN_RE.findall(_norm(text or "")) if len(t) >= _MIN_DRUG_TOKEN)

    for cand in candidates:
        _add(f"{cand.get('fact') or ''} {cand.get('hook') or ''}")
    for key in ("news2_band", "trend", "coverage_note", "movement"):
        _add(str((tier1 or {}).get(key) or ""))
    for param in list((tier1 or {}).get("parameters") or []) + list(
        (tier1 or {}).get("non_scoring") or []
    ):
        _add(
            f"{param.get('parameter') or ''} {param.get('display') or ''} "
            f"{param.get('movement') or ''}"
        )
    return frozenset(tokens)


def _allowed_numbers(candidates: list[dict], tier1: dict) -> frozenset:
    """Numbers a flag may state: the ones the facts carry, plus Orb's own
    deterministic scoring figures (which are already on the clinician's screen
    and are pinned, so a flag quoting one cannot be wrong)."""
    numbers: set[str] = set()
    for cand in candidates:
        numbers.update(cand.get("numbers") or [])
    for key in ("news2", "news2_delta", "observations_used", "spo2_scale"):
        numbers.add(_number_key(tier1.get(key)))
    for param in list(tier1.get("parameters") or []) + list(tier1.get("non_scoring") or []):
        for key in ("value", "points", "first_value", "first_points", "previous_value"):
            numbers.add(_number_key(param.get(key)))
    numbers.discard("")
    return frozenset(numbers)


#: Words that appear in a formulary entry without being a drug name. Excluded
#: from the DETECTOR so an ordinary English sentence ("give it by injection") is
#: not deleted as a fabricated medication.
_FORMULARY_NOISE = frozenset(
    {
        "injection",
        "tablet",
        "tablets",
        "capsule",
        "capsules",
        "syrup",
        "solution",
        "suspension",
        "infusion",
        "sodium",
        "chloride",
        "sulphate",
        "sulfate",
        "hydrochloride",
        "phosphate",
        "acetate",
        "citrate",
        "succinate",
        "tartrate",
        "maleate",
        "oral",
        "intravenous",
        "cream",
        "ointment",
        "drops",
        "spray",
        "inhaler",
        "nebuliser",
        "nebulizer",
        "vial",
        "ampoule",
        "gram",
        "grams",
    }
)


def _known_drug_tokens(db, meds: list[dict]) -> frozenset:
    """What a drug NAME looks like in this hospital: the formulary plus whatever
    is on this patient's chart. Used only as a DETECTOR by the fabrication guard —
    being on this list does not make a name allowed, it makes an unexplained one
    suspicious."""

    def _add(into: set, text: str) -> None:
        into.update(
            t
            for t in _TOKEN_RE.findall(_norm(text or ""))
            if len(t) >= _MIN_DRUG_TOKEN and t not in _FORMULARY_NOISE and not _is_class_term(t)
        )

    tokens: set[str] = set()
    for med in meds:
        _add(tokens, med["name"])
    try:
        rows = db.execute("SELECT name, generic_name FROM helix_medication_master").fetchall()
    except Exception:
        # The formulary is an enrichment; `classify_drug` still detects the
        # families it knows. Same posture allergy_interlock takes.
        logger.debug("[PatientScreen] formulary lookup failed", exc_info=True)
        rows = []
    for r in rows:
        _add(tokens, r["name"])
        _add(tokens, r["generic_name"])

    # THE INTERACTION KNOWLEDGE BASE IS THE THIRD VOCABULARY, and it is the one
    # that closes the gap the other two leave. `classify_drug` knows FAMILIES, so
    # a drug with no class pattern is invisible to it; the formulary knows what
    # THIS hospital stocks, so a well-known drug it has not loaded is invisible
    # too. `drug_interactions` carries named ingredients — amiodarone,
    # metronidazole, clopidogrel, trimethoprim — precisely the consequential
    # drugs that fall through both. It is the SAME table the order-time safety
    # gate screens against, so recognition here and blocking there rest on one
    # source rather than two that can drift.
    #
    # Ingredient rows only: the `class` rows are class NAMES ("anticoagulants"),
    # which are not drug names and would make an ordinary clinical sentence
    # mentioning a class read as a fabricated medication.
    try:
        for r in db.execute(
            "SELECT term_a, term_b FROM drug_interactions WHERE match_type = 'ingredient'"
        ).fetchall():
            _add(tokens, r["term_a"])
            _add(tokens, r["term_b"])
    except Exception:
        logger.debug("[PatientScreen] interaction vocabulary unavailable", exc_info=True)
    return frozenset(tokens)
