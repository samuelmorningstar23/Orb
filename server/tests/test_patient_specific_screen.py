"""Orb — STEP A: the deterministic patient-specific pre-screen, and its filters.

WHY THIS FILE EXISTS. The advice tier it feeds was measured on the dry-run ward
at 22% delivery overall and 0% across a quiet control window, and when it did
arrive two of its three steps restated the alert they were attached to
("Escalate immediately", "Critical NEWS2"). The verdict was that it was
decoration. The re-shape makes it say ONLY what depends on THIS patient's own
chart — and the module under test is the half that decides, with no model
involved, whether there is anything of that kind to say at all.

THE THREE RULES THE BUILD IS HELD TO, and where each is pinned here:

  1. SILENCE IS A VALID AND EXPECTED ANSWER. `test_a_bare_chart_produces_no
     _candidates_at_all` and the whole "IT MISSES ON PURPOSE" section. A screen
     that always finds something is padding, which is what killed the last one.
  2. NEVER RESTATE THE ALERT. `test_the_generic_filter_catches_every_phrase
     _measured_on_screen` is table-driven over the EXACT strings a clinician saw,
     and `names_a_fact` is the stronger, positive half of the same rule.
  3. EVERY FLAG NAMES ITS SOURCE FACT. Every rule test asserts the drug, the lab
     value with its date, or the allergy as charted appears in the rendered fact,
     because a checkable claim beats a confident one.

WHAT IS ASSERTED is the candidate list, the rendered fact string, or a filter
verdict — never a log line. Where a rule's value is that it does NOT fire, the
test constructs the patient it must stay quiet about and asserts the empty list,
because a false flag is how a clinician learns to ignore the tier.

THE FABRICATION GUARD'S FALSE-POSITIVE RATE, MEASURED 2026-08-15. The guard now
treats a clinically-shaped word it cannot account for as SUSPECT rather than
safe (`patient_specific_screen.is_prose`), which is the only way to catch a drug
Orb has never heard of — and the only rule in the file that can plausibly delete
a TRUE sentence. So it was measured rather than argued about: medgemma:27b, the
production prompt and options, every admitted patient on the seeded ward that
the screen fires on, 258 generated lines over eight runs at temperatures 0.2
(production) and 0.8-0.9 (stress).

  * The closed-vocabulary rule deleted 5 of 258 lines. Every one was an ordinary
    English word missing from the table — "unsafe", "drops", "toxicity",
    "thereof", "thrombolysis" — not an invention. Each was added to the table.
  * On the final two runs after those five fixes, 66 fresh lines at 0.2 and 0.9:
    it deleted NONE. That is the number this file's `MEASURED_ON_THE_WARD`
    tables defend.

RE-MEASURED 2026-08-15, AND THE 0/66 ABOVE IS NOT THE RATE TO QUOTE. Those 66
lines are the ones the table was GROWN FROM, so measuring the table against them
measures it against itself. Measured instead on thirty flags written fresh for
these same patients — three samples of ten, the table frozen after the second —
the deletion rate is 0/10 on the sample used to grow the table, 3/10 held out
and 3/10 on a final untouched sample. Every deletion was an ordinary English
word the table did not hold ("penalty", "steer", "mount", "concealed",
"flatten", "table"); none was an invention. Growing the table from ~1,100 to
~2,000 entries between the samples did not move the held-out rate, because
English is open and the table is not.

The guard is therefore still costing roughly ONE TRUE LINE IN THREE, and that is
a product ruling rather than a bug to tune away — the alternative that removes
the cost is the shape test in `patient_specific_screen._DRUG_SUFFIXES`'s note,
which was tried and let `noradrenaline` onto the alert screen. `DROP_UNKNOWN_TERM`
is separate precisely so the ruling can be made on week-one pilot data.
  * The other drops in the same runs were the OLDER rules doing their job: 14
    lines named no chart fact at all (`unsourced`), and 5 named a drug or a
    number the screen never supplied (`fabricated`, e.g. a ceftriaxone the model
    reached for on a penicillin-allergic patient).

`DROP_UNKNOWN_TERM` is a separate drop reason precisely so this rate keeps being
measured in the pilot instead of being taken on trust from this docstring.
"""
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from database import db_session  # noqa: E402
from services import patient_specific_screen as pss  # noqa: E402
from services import sage_assessment_service as sas  # noqa: E402
from services.drug_safety_service import classify_drug  # noqa: E402

_CREATED: list[str] = []

#: A picture that is deteriorating by BP and respiratory rate and by nothing
#: else. Chosen so the heart rate and the temperature are MEASURED AND NOT
#: SCORING, which is the precondition both masking rules exist for: HR 78 is
#: 0pt (51-90) and 37.0 °C is 0pt (36.1-38.0), while SBP 88 scores 2 and RR 22
#: scores 2. Every rule test starts here and changes one thing.
QUIET_PULSE = {
    "heart_rate": 78, "systolic_bp": 88, "diastolic_bp": 60,
    "respiratory_rate": 22, "temperature": 37.0, "spo2": 97,
    "consciousness": "alert", "on_oxygen": False,
}


@pytest.fixture(autouse=True)
def _formulary(isolated_module_database):
    """The hospital's own catalogue — the thing brand resolution reads.

    METOLAR is the load-bearing row: an Indian ward charts the brand, the
    beta-blocker stem lives in the generic, and `_active_medications` resolves
    one to the other through `allergy_interlock.resolve_ingredients_bulk` before
    `classify_drug` ever sees it.

    Use this module's catalogue: stock-rule fixtures deliberately put strength
    and unit words in brand names, which would contaminate the drug-name-only
    lexicon invariant below when the files share a session database.
    """
    from services import interaction_db
    from services.helix_db import HELIX_SCHEMA
    with db_session() as db:
        db.executescript(HELIX_SCHEMA)
        for row in (
            ("PSS-MTL", "Metolar 50", "Metoprolol", "Tablet", "50 mg"),
            ("PSS-WAR", "Warfarin", "Warfarin sodium", "Tablet", "5 mg"),
            ("PSS-CEF", "Ceftriaxone", "Ceftriaxone sodium", "Injection", "1 g"),
        ):
            db.execute(
                "INSERT OR IGNORE INTO helix_medication_master (id, name, "
                "generic_name, form, strength) VALUES (?, ?, ?, ?, ?)", row)
    # The interaction knowledge base is the fabrication guard's third vocabulary
    # and the one that recognises a named ingredient with no drug class. Seeded
    # here for the same reason the formulary is: the guard's coverage is a
    # property of Orb's own data, so a test that leaves the table empty measures
    # a weaker guard than the one that ships.
    interaction_db.ensure_seeded()
    yield


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    if not _CREATED:
        return
    ids = list(_CREATED)
    _CREATED.clear()
    marks = ",".join("?" * len(ids))
    with db_session() as db:
        for table in ("vitals", "medications", "patient_allergies", "observations",
                      "clinical_documents", "problem_list", "admissions",
                      "risk_scores", "world_model_state"):
            try:
                col = "entity_id" if table == "world_model_state" else "patient_id"
                db.execute(f"DELETE FROM {table} WHERE {col} IN ({marks})", ids)
            except Exception:
                pass
        db.execute(f"DELETE FROM patients WHERE id IN ({marks})", ids)


# ═════════════════════════════════════════════════════════════════════════════
#  The harness
# ═════════════════════════════════════════════════════════════════════════════
def _patient(*, vitals=None, meds=(), allergies=(), problems=(),
             diagnosis="Community acquired pneumonia", labs=(), reports=()) -> str:
    """One admitted patient with exactly the chart a test needs and no more.

    `meds` are (name, dose) pairs; `allergies` (substance, reaction, severity);
    `labs` (name, value, unit, flag, age_days). Everything defaults to ABSENT,
    because the screen's most important behaviour is what it does with a chart
    that has nothing in it.

    `reports` are (title, content, age_days) — THE LANE ORB ACTUALLY HAS. `labs`
    writes structured `observations` rows, i.e. what an HL7 analyser feed
    produces, and on this appliance that table holds ZERO ROWS for every patient
    on the ward. Every lab Orb really holds is a rendered `clinical_documents`
    report, and until 2026-08-15 nothing read one — so both renal rules were
    unreachable code and this whole fixture proved a screen that could not fire.
    """
    pid = f"PSS{uuid.uuid4().hex[:9].upper()}"
    _CREATED.append(pid)
    now = datetime.now(timezone.utc)
    obs = {**QUIET_PULSE, **(vitals or {})}
    with db_session() as db:
        db.execute("INSERT OR IGNORE INTO users (id, name, role, password_hash) "
                   "VALUES ('PSSADM', 'Screen Admitter', 'doctor', 'x')")
        db.execute(
            """INSERT INTO patients (id, name, mrn, bed, ward, age, sex, diagnosis,
                                     status, admission_date)
               VALUES (?, ?, ?, 'S-1', 'PS-1', 68, 'M', ?, 'admitted', ?)""",
            (pid, f"Screen {pid}", f"MRN-{uuid.uuid4().hex[:8].upper()}",
             diagnosis, now.isoformat()))
        db.execute(
            "INSERT INTO admissions (patient_id, department, ward, bed, status, "
            "admitted_by) VALUES (?, 'Screen Medicine', 'PS-1', 'S-1', 'active', "
            "'PSSADM')", (pid,))
        db.execute(
            """INSERT INTO vitals (patient_id, timestamp, heart_rate, systolic_bp,
                                   diastolic_bp, spo2, temperature, respiratory_rate,
                                   consciousness, on_oxygen)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (pid, now.isoformat(), obs["heart_rate"], obs["systolic_bp"],
             obs["diastolic_bp"], obs["spo2"], obs["temperature"],
             obs["respiratory_rate"], obs["consciousness"],
             1 if obs["on_oxygen"] else 0))
        for name, dose in meds:
            db.execute(
                "INSERT INTO medications (patient_id, drug_name, dosage, route, "
                "frequency, status, prescribed_by) "
                "VALUES (?, ?, ?, 'oral', 'OD', 'active', 'PSSADM')",
                (pid, name, dose))
        for substance, reaction, severity in allergies:
            db.execute(
                "INSERT INTO patient_allergies (patient_id, substance, reaction, "
                "severity, status, recorded_by) VALUES (?, ?, ?, ?, 'active', 'PSSADM')",
                (pid, substance, reaction, severity))
        for problem in problems:
            db.execute(
                "INSERT INTO problem_list (patient_id, problem, status, recorded_by, "
                "recorded_at) VALUES (?, ?, 'active', 'PSSADM', ?)",
                (pid, problem, now.isoformat()))
        for name, value, unit, flag, age_days in labs:
            db.execute(
                "INSERT INTO observations (patient_id, source, observation_name, "
                "value_numeric, unit, abnormal_flag, observed_at, recorded_at) "
                "VALUES (?, 'ingested_hl7', ?, ?, ?, ?, ?, ?)",
                (pid, name, value, unit, flag,
                 (now - timedelta(days=age_days)).isoformat(),
                 (now - timedelta(days=age_days)).isoformat()))
        for title, content, age_days in reports:
            db.execute("INSERT OR IGNORE INTO users (id, name, role, password_hash) "
                       "VALUES ('bot_lab', 'Orb Lab System', 'doctor', 'system')")
            db.execute(
                "INSERT INTO clinical_documents (patient_id, doc_type, title, "
                "content, author_id, created_at) "
                "VALUES (?, 'lab_result', ?, ?, 'bot_lab', ?)",
                (pid, title, content, (now - timedelta(days=age_days)).isoformat()))
    return pid


def _report(creatinine="2.5", unit="mg/dL", flag="[H]", ref="0.7-1.3") -> str:
    """Orb's OWN rendered lab report, byte for byte the shape
    `bot_service._generate_lab_result` writes: two spaces of indent, the analyte,
    the value with its unit, the flag column, the reference range in
    parentheses."""
    return (f"🔬 LAB RESULT — BMP — STAT\nPatient: Test\n\n"
            f"  Sodium: 143 mEq/L    (ref: 136-145)\n"
            f"  Creatinine: {creatinine} {unit}  {flag}  (ref: {ref})\n")


def _run(pid: str, *, facts=None) -> dict:
    """The screen, on a real connection, against Orb's own Tier 1 block.

    Tier 1 is COMPUTED rather than hand-written: every rule below keys off which
    NEWS2 parameters are scoring, and a hand-built block would let this file
    agree with itself about a decomposition the engine does not produce.
    """
    with db_session() as db:
        tier1 = sas._compute_tier1(db, pid)
        return pss.screen(db, pid, tier1, facts=facts)


def _facts(result: dict) -> str:
    return " || ".join(c["fact"] for c in result["candidates"])


# ═════════════════════════════════════════════════════════════════════════════
#  1. SILENCE IS THE COMMON ANSWER, AND IT COSTS NOTHING
#
#  This is the first rule the build is held to and the one the whole throughput
#  argument rests on: an empty candidate list is what lets the caller answer
#  without a queue slot, a prompt or a token.
# ═════════════════════════════════════════════════════════════════════════════
def test_a_bare_chart_produces_no_candidates_at_all():
    """No drugs, no allergies, no labs, no problem list — and a deteriorating
    patient. The screen looked and there was nothing to say."""
    result = _run(_patient())

    assert result["screened"] is True, "the screen ran"
    assert result["screen_error"] is None
    assert result["candidates"] == []
    assert result["rules_fired"] == []


def test_an_empty_screen_still_reports_that_it_ran():
    """"Orb looked and found nothing" and "Orb could not look" are different
    clinical claims and the caller renders them differently — one is a confident
    silence, the other is a degraded one."""
    clean = _run(_patient())
    failed = pss.screen(_ExplodingDB(), "PSSNOBODY", {"available": True, "parameters": []})

    assert clean["screened"] is True and clean["screen_error"] is None
    assert failed["screened"] is False and failed["screen_error"] == "screen_failed"
    assert failed["candidates"] == []


def test_a_patient_with_no_scoreable_observation_is_not_screened():
    """Every rule is "this chart fact AND this thing the patient is doing right
    now". With no deterioration to hook to, firing on the drug chart alone would
    produce a flag about a medication list rather than about an alert."""
    pid = _patient(meds=[("Warfarin", "5 mg")])
    with db_session() as db:
        db.execute("DELETE FROM vitals WHERE patient_id = ?", (pid,))
        tier1 = sas._compute_tier1(db, pid)
        result = pss.screen(db, pid, tier1)

    assert tier1.get("available") is not True
    assert result["screened"] is False
    assert result["screen_error"] == "no_scoreable_observation"
    assert result["candidates"] == []


# ═════════════════════════════════════════════════════════════════════════════
#  2. THE RULES FIRE, AND EVERY FLAG NAMES ITS SOURCE FACT
# ═════════════════════════════════════════════════════════════════════════════
def test_a_rate_controlled_patient_with_a_quiet_pulse_is_flagged_by_name():
    """THE CLEAREST CASE IN THE WHOLE DESIGN. NEWS2 scores the compensatory
    tachycardia of shock; a beta-blocker is precisely what stops a patient
    mounting it. A heart rate of 78 in a hypotensive, tachypnoeic patient on
    metoprolol is a blunted signal, and the score cannot say so."""
    result = _run(_patient(meds=[("Metoprolol", "50 mg")]))

    assert result["rules_fired"] == [pss.RULE_RATE_CONTROL]
    fact = result["candidates"][0]
    assert "Metoprolol" in fact["fact"], "the flag must name the drug, not the class"
    assert "50 mg" in fact["fact"], "and the dose, so it is checkable in one glance"
    assert fact["evidence"]["drug"] == "Metoprolol"
    # The hook names what the patient is doing RIGHT NOW that makes the drug
    # matter — the conjunction is what makes this patient-specific.
    assert "Heart rate" in fact["hook"] and "78" in fact["hook"]


def test_a_charted_brand_name_is_resolved_through_the_formulary_before_classing():
    """"Metolar 50" contains no beta-blocker stem. Indian wards chart brands, so
    without the formulary resolution this patient is invisible to the rule that
    matters most — and the reuse is `allergy_interlock.resolve_ingredients_bulk`,
    the same one query the bedside interlock already makes."""
    result = _run(_patient(meds=[("Metolar 50", "50 mg")]))

    assert result["rules_fired"] == [pss.RULE_RATE_CONTROL]
    # NAMED AS CHARTED. The generic is matching material; the clinician has to be
    # able to find this string on their own drug chart.
    assert "Metolar 50" in result["candidates"][0]["fact"]
    assert "Metoprolol" not in result["candidates"][0]["fact"]


def test_an_anticoagulated_hypotensive_patient_is_flagged_by_name():
    """Hypotension on an anticoagulant raises occult haemorrhage as a CAUSE
    rather than a complication, and changes the risk of every procedure the next
    hour might need. The class comes from `classify_drug`, not from any
    vocabulary of this module's."""
    result = _run(_patient(meds=[("Warfarin", "5 mg")]))

    assert pss.RULE_ANTICOAGULATION in result["rules_fired"]
    fact = next(c for c in result["candidates"]
                if c["rule"] == pss.RULE_ANTICOAGULATION)
    assert "Warfarin" in fact["fact"] and "5 mg" in fact["fact"]
    assert "Systolic BP" in fact["hook"] and "88" in fact["hook"]


def test_an_antimicrobial_allergy_in_an_infective_picture_names_it_as_charted():
    """The order-time gate and the bedside interlock both catch this — but only
    once somebody has ALREADY CHOSEN the drug. At 3am the useful moment is before
    the choice, and that is the only thing this flag adds."""
    result = _run(_patient(allergies=[("Penicillin", "anaphylaxis", "severe")]))

    assert pss.RULE_ANTIMICROBIAL_ALLERGY in result["rules_fired"]
    fact = next(c for c in result["candidates"]
                if c["rule"] == pss.RULE_ANTIMICROBIAL_ALLERGY)
    # THE REACTION IS PART OF THE FACT. "Penicillin allergy" changes what you
    # avoid; "Penicillin — anaphylaxis" changes how hard you avoid it.
    assert "Penicillin" in fact["fact"] and "anaphylaxis" in fact["fact"]
    assert fact["evidence"]["severity"] == "severe"
    assert "penicillins" in fact["evidence"]["classes"]


def test_an_allergy_charted_as_a_class_is_read_the_way_the_clinician_wrote_it():
    """"Sulfa" is not a drug name. It is how a sulfonamide allergy is actually
    charted, and `classify_allergen` — the interlock's own reader — is what makes
    it a class rather than an unknown word."""
    result = _run(_patient(allergies=[("Sulfa drugs", "rash", "moderate")]))

    fact = next(c for c in result["candidates"]
                if c["rule"] == pss.RULE_ANTIMICROBIAL_ALLERGY)
    assert "Sulfa drugs" in fact["fact"]
    assert fact["evidence"]["classes"] == ["sulfonamides"]


def test_a_raised_creatinine_facing_a_volume_decision_states_the_value_and_the_date():
    """RULE 3 IN ITS SHARPEST FORM. Not "consider renal function" — that is
    advice, and it is true of anyone. "Creatinine 180 µmol/L, 6 days ago" is a
    fact a clinician can check, and the AGE IS ALWAYS RENDERED so they weigh the
    staleness themselves rather than trusting our window."""
    result = _run(_patient(labs=[("Creatinine", 180.0, "umol/L", "H", 6)]))

    assert pss.RULE_RENAL_VOLUME in result["rules_fired"]
    fact = next(c for c in result["candidates"]
                if c["rule"] == pss.RULE_RENAL_VOLUME)
    assert "180" in fact["fact"]
    assert "umol/L" in fact["fact"]
    assert "6 days ago" in fact["fact"], "a lab with no date is not checkable"
    assert fact["evidence"]["age_days"] == 6


def test_a_renally_handled_drug_and_a_raised_creatinine_name_both_halves():
    """"On ramipril; creatinine 180 six days ago" is the sentence the ruling
    named as the target. The ACE-inhibitor class comes from `classify_drug` — its
    "-pril" stem — and not from this module."""
    result = _run(_patient(meds=[("Ramipril", "5 mg")],
                           labs=[("Creatinine", 180.0, "umol/L", "H", 6)]))

    fact = next(c for c in result["candidates"] if c["rule"] == pss.RULE_RENAL_DRUG)
    assert "Ramipril" in fact["fact"] and "180" in fact["fact"]
    assert fact["evidence"]["drug"] == "Ramipril"
    assert fact["evidence"]["value"] == 180.0


def test_charted_heart_failure_facing_a_volume_question_is_named_as_charted():
    """The mirror of the renal rule from the other organ. The problem is quoted
    exactly as the problem list holds it, because "documented HFrEF on the
    problem list" is a fact and "consider heart failure" is advice."""
    result = _run(_patient(problems=["Chronic heart failure (HFrEF), EF 30%"]))

    fact = next(c for c in result["candidates"] if c["rule"] == pss.RULE_HEART_FAILURE)
    assert "Chronic heart failure (HFrEF), EF 30%" in fact["fact"]


def test_a_steroid_on_an_afebrile_deteriorating_patient_is_flagged():
    """A NORMAL TEMPERATURE ON A STEROID IS NOT EVIDENCE OF NO INFECTION. The
    mirror of the rate-control trap: the thermometer reads 37.0 and the patient
    may still be septic."""
    result = _run(_patient(meds=[("Prednisolone", "20 mg")]))

    assert pss.RULE_BLUNTED_FEVER in result["rules_fired"]
    fact = next(c for c in result["candidates"] if c["rule"] == pss.RULE_BLUNTED_FEVER)
    assert "Prednisolone" in fact["fact"] and "20 mg" in fact["fact"]
    assert "Temperature" in fact["hook"]


def test_the_working_diagnosis_can_be_read_off_an_already_assembled_chart():
    """The alert path has ALREADY built the chart block. Reading the problem list
    off it rather than querying a second time is why this screen adds no round
    trip to the lane it runs on."""
    pid = _patient(diagnosis="", problems=[])
    supplied = {"diagnosis": "Severe sepsis, urinary source", "problems": []}

    with_facts = _run(pid, facts=supplied)
    without = _run(pid, facts=None)

    # Same patient, same vitals, same allergy list — the infective picture came
    # only from the caller's dict.
    assert without["rules_fired"] == []
    assert with_facts["rules_fired"] == []
    febrile = _patient(diagnosis="", allergies=[("Penicillin", "rash", "moderate")])
    assert _run(febrile, facts=supplied)["rules_fired"] == [
        pss.RULE_ANTIMICROBIAL_ALLERGY]
    assert _run(febrile, facts=None)["rules_fired"] == [], (
        "with no diagnosis and no fever there is no infective picture to hook to")


# ═════════════════════════════════════════════════════════════════════════════
#  3. IT MISSES ON PURPOSE
#
#  A false flag is not a small cost. It is how a clinician learns to ignore the
#  tier, which is precisely what killed the version this replaces. Every test
#  here constructs the patient the rule must stay QUIET about.
# ═════════════════════════════════════════════════════════════════════════════
def test_rate_control_is_silent_when_the_tachycardia_is_itself_the_driver():
    """A flag reading "this pulse may be blunted" over a heart rate of 124 is the
    exact false positive that teaches a ward to skip the panel. The rate control
    is plainly not hiding anything — the clinician can see the tachycardia and
    NEWS2 is already scoring it."""
    result = _run(_patient(vitals={"heart_rate": 124},
                           meds=[("Metoprolol", "50 mg")]))

    assert pss.RULE_RATE_CONTROL not in result["rules_fired"]


def test_rate_control_is_silent_when_nothing_else_is_driving_the_score():
    """A quiet pulse on a patient who is not deteriorating by anything else is a
    quiet pulse. The rule is about a MASKED signal, and there is nothing here for
    it to be masking."""
    calm = {"systolic_bp": 124, "respiratory_rate": 16, "spo2": 98,
            "temperature": 37.0, "heart_rate": 78}
    result = _run(_patient(vitals=calm, meds=[("Metoprolol", "50 mg")]))

    assert result["candidates"] == []


def test_anticoagulation_is_silent_on_a_patient_who_is_merely_tachypnoeic():
    """An anticoagulated patient who is breathing fast is an anticoagulated
    patient, not a finding. The rule fires on hypotension or altered
    consciousness — the two pictures where the anticoagulant changes what this
    could BE."""
    breathing = {"systolic_bp": 124, "respiratory_rate": 26, "heart_rate": 78}
    result = _run(_patient(vitals=breathing, meds=[("Warfarin", "5 mg")]))

    assert pss.RULE_ANTICOAGULATION not in result["rules_fired"]


def test_an_allergy_that_is_not_antimicrobial_is_not_an_antibiotic_problem():
    """A latex or a peanut allergy does not rule out the first-line agent for a
    pneumonia. Only the classes an infective deterioration is actually treated
    with can do that, and they come from `classify_allergen`'s own vocabulary."""
    result = _run(_patient(allergies=[("Latex", "contact dermatitis", "mild")]))

    assert result["candidates"] == []


def test_an_antimicrobial_allergy_with_no_infective_picture_is_silent():
    """Not febrile, and the working diagnosis is a fracture. The allergy is real
    and the interlock still holds it; it just does not bear on THIS
    deterioration, which is the only thing this tier is allowed to talk about."""
    result = _run(_patient(diagnosis="Fractured neck of femur",
                           allergies=[("Penicillin", "rash", "moderate")]))

    assert result["candidates"] == []


def test_a_stale_creatinine_is_history_rather_than_a_finding():
    """A creatinine from a month ago does not describe this deterioration.
    `LAB_WINDOW_DAYS` is 14 and the boundary is asserted from BOTH sides so a
    widened window is a decision somebody makes rather than a drift."""
    fresh = _run(_patient(labs=[("Creatinine", 180.0, "umol/L", "H",
                                 pss.LAB_WINDOW_DAYS - 1)]))
    stale = _run(_patient(labs=[("Creatinine", 180.0, "umol/L", "H",
                                 pss.LAB_WINDOW_DAYS + 1)]))

    assert pss.RULE_RENAL_VOLUME in fresh["rules_fired"]
    assert stale["candidates"] == []


def test_a_normal_creatinine_is_not_a_renal_flag():
    result = _run(_patient(labs=[("Creatinine", 74.0, "umol/L", "N", 1)]))

    assert result["candidates"] == []


@pytest.mark.parametrize("value,unit,raised", [
    # THE UNIT MATTERS MORE THAN THE NUMBER. 180 is markedly raised in µmol/L and
    # an impossible value in mg/dL; 1.8 is the reverse. A screen that read the
    # figure alone would flag every normal Indian mg/dL report and miss every
    # abnormal µmol/L one.
    (180.0, "umol/L", True),
    (74.0, "umol/L", False),
    (1.8, "mg/dL", True),
    (0.9, "mg/dL", False),
    # No unit at all: magnitude decides, the same heuristic a clinician applies
    # reading a foreign report.
    (180.0, "", True),
    (1.8, "", True),
    (0.9, "", False),
])
def test_the_creatinine_threshold_reads_the_unit_not_just_the_number(value, unit, raised):
    result = _run(_patient(labs=[("Creatinine", value, unit, "", 1)]))

    assert (pss.RULE_RENAL_VOLUME in result["rules_fired"]) is raised


def test_the_lab_flag_beats_our_threshold_because_the_lab_knows_its_own_assay():
    """A value inside our fallback threshold that the analyser itself marked high
    is high — its reference range knows this population and this assay, and ours
    is a fallback for a report that carries no flag at all."""
    result = _run(_patient(labs=[("Creatinine", 105.0, "umol/L", "H", 1)]))

    assert pss.RULE_RENAL_VOLUME in result["rules_fired"]


# ═════════════════════════════════════════════════════════════════════════════
#  THE LAB LANE ORB ACTUALLY HAS
#
#  MEASURED 2026-08-15: the `observations` table — the HL7/FHIR analyser feed —
#  holds ZERO ROWS for every patient on this appliance, because the only thing
#  that writes it is `ingestion_service` and no feed is connected. Every lab Orb
#  holds is a rendered `clinical_documents` row. So `_latest_creatinine` returned
#  None for every patient, both renal rules were unreachable code, and Lakshmi
#  Devi (PAT-BA80FFC5) — creatinine 2.5 mg/dL flagged [H] against a printed
#  reference range of 0.7-1.3, with a systolic of 78 — was screened SILENT on
#  three of three alerts. Every test above passed throughout, because the fixture
#  wrote structured rows the ward has never had.
#
#  A SCREEN WHOSE RULES CANNOT FIRE MEASURES NOTHING, and it fails in the
#  direction that looks like good news.
#
#  READING A DOCUMENT IS NARROW AND STAYS NARROW. These tests pin the four
#  conditions that make a line a RESULT rather than a sentence, because the whole
#  safety of the lane is that a clinician's free text can never become a flag.
# ═════════════════════════════════════════════════════════════════════════════
def test_the_renal_rule_fires_on_the_lab_lane_orb_actually_has():
    """PROVE-CAN-FAIL TARGET: the defect itself, as the ward saw it.

    Revert `_latest_creatinine` to the structured lane alone and this fails —
    which is exactly what the whole suite did while the rule was dead.
    """
    result = _run(_patient(vitals={"systolic_bp": 78},
                           reports=[("BMP — STAT", _report(), 0)]))

    assert pss.RULE_RENAL_VOLUME in result["rules_fired"], (
        "the renal rule is still blind to the only lab lane Orb has")
    fact = next(c for c in result["candidates"] if c["rule"] == pss.RULE_RENAL_VOLUME)
    assert "2.5" in fact["fact"] and "mg/dL" in fact["fact"]
    assert "today" in fact["fact"], "a lab with no date is not checkable"
    assert fact["evidence"]["value"] == 2.5
    # WHICH REPORT TO OPEN. A parsed number is only as checkable as its source.
    assert "BMP — STAT" in fact["source"]


def test_a_renally_handled_drug_pairs_with_a_reported_creatinine_too():
    """The second dead rule, on the same lane. "On metformin; creatinine 2.5
    mg/dL today" is the sentence, and metformin is one of the four ingredients
    `classify_drug` has no class for."""
    result = _run(_patient(meds=[("Metformin", "1 g")],
                           reports=[("BMP", _report(), 0)]))

    fact = next(c for c in result["candidates"] if c["rule"] == pss.RULE_RENAL_DRUG)
    assert "Metformin" in fact["fact"] and "2.5" in fact["fact"]


def test_a_structured_result_still_wins_over_a_rendered_report():
    """The analyser feed stays the PREFERRED source: it carries the analyser's
    own abnormal flag and needs no parsing at all. Nothing about it needed
    fixing, and a hospital that connects one must get it rather than our
    text scan."""
    result = _run(_patient(labs=[("Creatinine", 180.0, "umol/L", "H", 6)],
                           reports=[("BMP", _report(), 0)]))

    fact = next(c for c in result["candidates"] if c["rule"] == pss.RULE_RENAL_VOLUME)
    assert "180" in fact["fact"] and "umol/L" in fact["fact"]
    assert "2.5" not in fact["fact"]


@pytest.mark.parametrize("line", [
    # THE REFERENCE RANGE IS WHAT MAKES A LINE A RESULT. Every one of these
    # contains the word and the number and none of them is a reported value.
    "Creatinine has been climbing since admission — now 2.5",
    "Plan: hold nephrotoxics if creatinine > 2.5 mg/dL",
    "Creatinine: 2.5 mg/dL on the outside report",
    "Discussed with renal — creatinine 2.5, for review tomorrow",
])
def test_prose_about_a_creatinine_is_never_read_as_a_result(line):
    """A clinician's sentence must never become a flag. The four structured
    fields — analyte, value, unit, printed reference range — are the gate, and
    prose cannot satisfy them however many numbers it contains."""
    content = f"🔬 LAB RESULT — Note\nPatient: Test\n\n  {line}\n"
    result = _run(_patient(vitals={"systolic_bp": 78},
                           reports=[("Ward round", content, 0)]))

    assert result["candidates"] == [], f"prose became a clinical flag: {line!r}"


def test_two_different_creatinines_in_one_report_are_refused_rather_than_guessed():
    """A wrong lab value on an alert screen is the worst outcome available here,
    and there is no version of guessing that beats silence. Refuses the PATIENT,
    not just the report — falling through to an older result would answer a
    question this data cannot answer."""
    content = (f"🔬 LAB RESULT — BMP\nPatient: Test\n\n"
               f"  Creatinine: 2.5 mg/dL  [H]  (ref: 0.7-1.3)\n"
               f"  Creatinine: 4.1 mg/dL  [H]  (ref: 0.7-1.3)\n")
    result = _run(_patient(vitals={"systolic_bp": 78}, reports=[
        ("BMP — STAT", content, 0),
        ("BMP", _report(), 1),
    ]))

    assert result["candidates"] == []


def test_the_reports_own_reference_range_decides_before_our_threshold():
    """The printed range knows this assay and this population. Ours is a fallback
    for a report that prints no range at all, and it must not overrule one that
    does."""
    inside = _run(_patient(vitals={"systolic_bp": 78},
                           reports=[("BMP", _report("1.2", flag="", ref="0.7-1.3"), 0)]))
    above = _run(_patient(vitals={"systolic_bp": 78},
                          reports=[("BMP", _report("1.4", flag="", ref="0.7-1.3"), 0)]))

    assert inside["candidates"] == []
    assert pss.RULE_RENAL_VOLUME in above["rules_fired"]


def test_a_reported_creatinine_obeys_the_same_staleness_window():
    """One rule about how old a lab may be, applied to both lanes. A creatinine
    from three months ago is history whichever table it arrived in."""
    fresh = _run(_patient(vitals={"systolic_bp": 78},
                          reports=[("BMP", _report(), pss.LAB_WINDOW_DAYS - 1)]))
    stale = _run(_patient(vitals={"systolic_bp": 78},
                          reports=[("BMP", _report(), pss.LAB_WINDOW_DAYS + 1)]))

    assert pss.RULE_RENAL_VOLUME in fresh["rules_fired"]
    assert stale["candidates"] == []


def test_the_newest_report_wins_whichever_timestamp_shape_it_was_written_in():
    """`clinical_documents.created_at` holds TWO shapes — SQLite's own
    "2026-08-14 17:24:15" from `CURRENT_TIMESTAMP` and "2026-08-14T17:24:15+00:00"
    from every explicit writer. In a text sort the space precedes the 'T', so on
    a day carrying both, a naive DESC hands back the isoformat row whatever the
    clock says. This function stops at the first report holding a creatinine, so
    that is a SILENT MISS: today's STAT 5.0 passed over for this morning's normal
    1.1, on exactly the patient the rule exists for.
    """
    pid = _patient(vitals={"systolic_bp": 78})
    # BOTH ON THE SAME CALENDAR DAY, which is the only way the separator can
    # decide anything — on different dates the date prefix dominates and the bug
    # is invisible. Anchored to today's date rather than to the clock so the test
    # cannot pass or fail depending on what time it runs.
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with db_session() as db:
        db.execute("INSERT OR IGNORE INTO users (id, name, role, password_hash) "
                   "VALUES ('bot_lab', 'Orb Lab System', 'doctor', 'system')")
        # The older, NORMAL result — written the way an explicit writer does.
        db.execute(
            "INSERT INTO clinical_documents (patient_id, doc_type, title, content, "
            "author_id, created_at) VALUES (?, 'lab_result', 'BMP', ?, 'bot_lab', ?)",
            (pid, _report("1.1", flag="", ref="0.6-1.3"), f"{day}T00:05:00+00:00"))
        # The newer, RAISED one — written the way `CURRENT_TIMESTAMP` does.
        db.execute(
            "INSERT INTO clinical_documents (patient_id, doc_type, title, content, "
            "author_id, created_at) VALUES (?, 'lab_result', 'BMP — STAT', ?, "
            "'bot_lab', ?)",
            (pid, _report("5.0"), f"{day} 00:06:00"))

    result = _run(pid)

    assert pss.RULE_RENAL_VOLUME in result["rules_fired"], (
        "the newest result was sorted below an older one by its separator alone")
    fact = next(c for c in result["candidates"] if c["rule"] == pss.RULE_RENAL_VOLUME)
    assert "5" in fact["fact"]


def test_a_normal_reported_creatinine_is_still_not_a_flag():
    """Silence stays the common answer on this lane too. The seeded ward's own
    normal report — 1.1 against a range of 0.6-1.3, no flag printed — must read
    as nothing to say."""
    result = _run(_patient(vitals={"systolic_bp": 78},
                           reports=[("BMP", _report("1.1", flag="", ref="0.6-1.3"), 0)]))

    assert result["candidates"] == []


# ═════════════════════════════════════════════════════════════════════════════
#  WHERE TO CHECK IT, AND WHEN IT WAS CHARTED
#
#  The rendered block on the clinician's screen boxes the chart fact under "From
#  this chart" and puts the section, the date and the scoring parameter beneath
#  it. Those three fields are what turn a confident claim into a checkable one,
#  and they come from here — the model is never shown them and could not supply
#  them if it were.
# ═════════════════════════════════════════════════════════════════════════════
def test_every_candidate_says_which_section_of_the_chart_holds_its_fact():
    """A flag a clinician cannot navigate to is a flag they have to take on
    trust, which is the one thing this tier is not asking for."""
    result = _run(_patient(
        vitals={"systolic_bp": 78},
        meds=[("Warfarin", "5 mg")],
        allergies=[("Penicillin", "rash", "moderate")],
        problems=["Chronic heart failure (HFrEF), EF 30%"],
        reports=[("BMP", _report(), 0)]))

    sources = {c["rule"]: c["source"] for c in result["candidates"]}
    assert sources[pss.RULE_ANTICOAGULATION] == pss.SOURCE_MEDICATIONS
    assert sources[pss.RULE_ANTIMICROBIAL_ALLERGY] == pss.SOURCE_ALLERGIES
    assert pss.SOURCE_RESULTS in sources[pss.RULE_RENAL_VOLUME]
    assert all(c["source"] for c in result["candidates"])


def test_a_charted_fact_carries_the_date_it_was_charted():
    """Phrased on the server, never composed in a client — the same rule
    `unavailable_message` follows, and for the same reason: two renderers of one
    clinical date will eventually phrase it two ways."""
    result = _run(_patient(vitals={"systolic_bp": 78}, meds=[("Warfarin", "5 mg")]))

    fact = next(c for c in result["candidates"]
                if c["rule"] == pss.RULE_ANTICOAGULATION)
    assert fact["recorded"] == "charted today"


def test_a_flag_names_the_scoring_parameter_it_bears_on_or_says_nothing():
    """`relates_to` is the number on the panel above the flag. The renal-DRUG
    rule deliberately does not need the picture at all — both halves are on the
    chart — so it must not appear to point at one."""
    result = _run(_patient(vitals={"systolic_bp": 78},
                           meds=[("Warfarin", "5 mg"), ("Metformin", "1 g")],
                           reports=[("BMP", _report(), 0)]))
    by_rule = {c["rule"]: c for c in result["candidates"]}

    assert "Systolic BP" in by_rule[pss.RULE_ANTICOAGULATION]["relates_to"]
    assert "78" in by_rule[pss.RULE_ANTICOAGULATION]["relates_to"]
    assert by_rule[pss.RULE_RENAL_DRUG]["relates_to"] is None


# ═════════════════════════════════════════════════════════════════════════════
#  THE SIGNATURE — WHICH CHART FACTS ARE IN PLAY
#
#  Suresh Reddy (PAT-A20FA685) was shown the identical Ceftriaxone-allergy line
#  ELEVEN TIMES IN TWENTY-ONE MINUTES. The redraft debounce compares NEWS2
#  pictures and his NEWS2 genuinely oscillated 8→14 and back every ninety
#  seconds, so it was powerless: an allergy charted in 2023 does not become newly
#  true because a respiratory rate moved. This is the string that lets a caller
#  tell a repeat from a new finding.
# ═════════════════════════════════════════════════════════════════════════════
def test_the_signature_is_the_same_for_the_same_chart_facts_at_a_different_score():
    """THE WHOLE POINT. Two observations of one patient, four NEWS2 points apart,
    with the same charted allergy. One signature."""
    pid = _patient(allergies=[("Ceftriaxone", "anaphylaxis", "severe")],
                   vitals={"temperature": 39.4})
    with db_session() as db:
        mild = pss.flag_signature(pss.screen(db, pid, sas._compute_tier1(db, pid)))
        db.execute(
            "INSERT INTO vitals (patient_id, timestamp, heart_rate, systolic_bp, "
            "diastolic_bp, spo2, temperature, respiratory_rate, consciousness, "
            "on_oxygen) VALUES (?, ?, 132, 74, 44, 88, 39.6, 30, 'voice', 1)",
            (pid, datetime.now(timezone.utc).isoformat()))
        worse = pss.flag_signature(pss.screen(db, pid, sas._compute_tier1(db, pid)))

    assert mild == worse, "the deterioration leaked into the signature"
    assert "ceftriaxone" in mild, "the signature does not identify the fact in play"


def test_a_new_chart_fact_changes_the_signature():
    """The counterweight, and the reason suppressing on this string is safe at
    all: a new drug, a new allergy or a first-time raised creatinine cannot be
    caught by it."""
    pid = _patient(vitals={"systolic_bp": 78},
                   allergies=[("Ceftriaxone", "anaphylaxis", "severe")])
    with db_session() as db:
        before = pss.flag_signature(pss.screen(db, pid, sas._compute_tier1(db, pid)))
        db.execute(
            "INSERT INTO medications (patient_id, drug_name, dosage, route, "
            "frequency, status, prescribed_by) "
            "VALUES (?, 'Warfarin', '5 mg', 'oral', 'OD', 'active', 'PSSADM')", (pid,))
        after = pss.flag_signature(pss.screen(db, pid, sas._compute_tier1(db, pid)))

    assert before != after
    assert "warfarin" in after.lower()


def test_a_clean_screen_and_a_failed_screen_have_different_signatures():
    """"Orb looked and there is nothing in play" is a real answer that compares
    equal to another one. "Orb could not look" is not comparable to anything, and
    a caller that treated the two alike would silence a patient Orb never
    actually screened."""
    with db_session() as db:
        clean = pss.flag_signature(pss.screen(
            db, _patient(), sas._compute_tier1(db, _patient())))
    failed = pss.flag_signature(pss.screen(
        _ExplodingDB(), "PSSNOBODY", {"available": True, "parameters": []}))

    assert clean == ""
    assert failed is None


def test_a_blunted_fever_flag_is_not_raised_on_a_patient_who_is_already_febrile():
    """A patient with a temperature of 39.4 does not need to be told their fever
    might be blunted. It is not."""
    result = _run(_patient(vitals={"temperature": 39.4},
                           meds=[("Prednisolone", "20 mg")]))

    assert pss.RULE_BLUNTED_FEVER not in result["rules_fired"]


def test_a_discontinued_drug_is_not_on_this_patient():
    """The screen reads what the patient is ACTUALLY on. A stopped beta-blocker
    is not blunting anything, and naming it would send a clinician to a drug
    chart entry that says "discontinued"."""
    pid = _patient(meds=[("Metoprolol", "50 mg")])
    with db_session() as db:
        db.execute("UPDATE medications SET status = 'discontinued' "
                   "WHERE patient_id = ?", (pid,))

    assert _run(pid)["candidates"] == []


def test_a_resolved_problem_is_not_a_current_problem():
    pid = _patient(problems=["Chronic heart failure (HFrEF)"])
    with db_session() as db:
        db.execute("UPDATE problem_list SET status = 'resolved' WHERE patient_id = ?",
                   (pid,))

    assert _run(pid)["candidates"] == []


# ═════════════════════════════════════════════════════════════════════════════
#  4. THE SHORT LIST STAYS SHORT, AND THE SCREEN NEVER TAKES THE ROW WITH IT
# ═════════════════════════════════════════════════════════════════════════════
def test_the_candidate_list_is_capped_and_numbered():
    """A screen that hands over eight facts has stopped pre-screening and is
    asking the model to trawl. The cap is CLINICAL, not a latency bound."""
    result = _run(_patient(
        meds=[("Metoprolol", "50 mg"), ("Warfarin", "5 mg"),
              ("Ramipril", "5 mg"), ("Prednisolone", "20 mg")],
        allergies=[("Penicillin", "rash", "moderate")],
        problems=["Chronic heart failure (HFrEF)"],
        labs=[("Creatinine", 180.0, "umol/L", "H", 2)],
        vitals={"temperature": 38.6}))

    assert len(result["candidates"]) == pss.MAX_CANDIDATES
    assert [c["id"] for c in result["candidates"]] == ["F1", "F2", "F3", "F4"]
    # Ordered by the rule sequence, so the cap keeps the highest-consequence
    # facts rather than whichever the drug chart happened to list first.
    assert result["rules_fired"][0] == pss.RULE_RATE_CONTROL


def test_one_rate_control_flag_per_patient_not_a_drug_list():
    """Two beta-blockers is a prescribing question, not two findings."""
    result = _run(_patient(meds=[("Metoprolol", "50 mg"), ("Bisoprolol", "2.5 mg")]))

    assert result["rules_fired"] == [pss.RULE_RATE_CONTROL]


def test_a_broken_rule_costs_its_own_flag_and_not_every_flag(monkeypatch):
    """One flag lost is better than every flag lost — and better still than an
    exception inside the transaction that creates the assessment row."""
    def _boom(*a, **kw):
        raise RuntimeError("rule exploded")

    monkeypatch.setattr(pss, "_rule_rate_control", _boom)
    result = _run(_patient(meds=[("Metoprolol", "50 mg"), ("Warfarin", "5 mg")]))

    assert result["screened"] is True
    assert result["rules_fired"] == [pss.RULE_ANTICOAGULATION]


def test_the_screen_never_raises_into_the_transaction_that_writes_the_row():
    """It runs inside the transaction that creates the assessment row. An
    exception here would leave a clinician looking at an alert with no row at
    all, which is the silent-absence failure this design forbids."""
    result = pss.screen(_ExplodingDB(), "PSSNOBODY",
                        {"available": True, "parameters": [], "news2": 7})

    assert result == pss._empty(screened=False, error="screen_failed")


class _ExplodingDB:
    """Every read fails — a disk, decryption or schema fault, injected where the
    real one would land."""

    def execute(self, *a, **kw):
        raise RuntimeError("database is gone")


# ═════════════════════════════════════════════════════════════════════════════
#  5. RULE 2 — NEVER RESTATE THE ALERT
#
#  Table-driven over the EXACT strings that were on the ward screen, because the
#  prompt forbidding them is not a control: a 27B ignores a prompt often enough
#  to matter, and this is what happens when it does.
# ═════════════════════════════════════════════════════════════════════════════
MEASURED_ON_SCREEN = [
    # ── verbatim from the tier this replaces ──
    "Escalate immediately",
    "Repeat observations",
    "Assess response to initial interventions",
    # ── the same padding, as a model actually writes it into a flag line ──
    "Escalate immediately | Critical NEWS2",
    "Repeat observations in 15 minutes | Track the trend",
    "Assess response to initial interventions | Guides the next step",
    "Consider sepsis given the fever | Common cause",
    "Urgent senior review | Critical band",
    "Continuous monitoring required | Patient is unstable",
    "Obtain IV access and send blood cultures | Standard for sepsis",
]


@pytest.mark.parametrize("line", MEASURED_ON_SCREEN)
def test_the_generic_filter_catches_every_phrase_measured_on_screen(line):
    assert pss.is_generic(line) is True, f"generic line survived the filter: {line!r}"


@pytest.mark.parametrize("line", [
    "Warfarin 5 mg charted — hypotension here may be occult bleeding",
    "Penicillin allergy (anaphylaxis) rules out the first-line beta-lactam",
    "Metoprolol 50 mg is blunting the pulse; 78 is not reassurance here",
    "Creatinine 180 umol/L six days ago changes the fluid calculus",
])
def test_a_patient_specific_line_is_not_caught_by_the_generic_filter(line):
    """The filter must not eat the output the tier exists to produce. A filter
    that deletes true sentences is worse than the padding it removes."""
    assert pss.is_generic(line) is False


def test_a_line_that_names_no_fact_is_dropped_even_when_it_is_not_on_the_list():
    """RULE 3 IS THE STRONGER HALF, and it generalises: a line naming no drug, no
    lab and no allergy is a line about nothing in particular, whatever words it
    used to get there. `_GENERIC_PHRASES` is belt to this braces."""
    result = _run(_patient(meds=[("Warfarin", "5 mg")]))

    assert pss.names_a_fact("Warfarin 5 mg raises occult bleeding here", result) is True
    assert pss.names_a_fact("Keep a close eye on this gentleman", result) is False
    assert pss.verify_line("Keep a close eye on this gentleman", result) == pss.DROP_UNSOURCED


# ═════════════════════════════════════════════════════════════════════════════
#  6. THE FABRICATION GUARD
#
#  A model naming a drug, a dose or a comorbidity the chart does not contain, on
#  the busiest screen in the hospital, is the worst outcome available here.
# ═════════════════════════════════════════════════════════════════════════════
def test_a_line_quoting_the_facts_it_was_given_survives_intact():
    result = _run(_patient(meds=[("Warfarin", "5 mg")]))

    assert pss.verify_line(
        "Warfarin 5 mg charted — hypotension here may be occult bleeding", result) is None


def test_a_drug_the_screen_never_supplied_is_dropped():
    """The model is not shown the chart, so a second drug name came from its own
    memory. Orb recognises it as a drug and that recognition is what makes the
    deletion safe rather than a guess — and it takes all three of Orb's existing
    vocabularies to be worth relying on:

      * CEFTRIAXONE is on the hospital's formulary;
      * AMIODARONE has no drug class and is not stocked in this fixture, but is a
        named ingredient in the interaction knowledge base the order-time gate
        screens against — which is exactly the consequential drug that fell
        through the other two before that vocabulary was added;
      * IBUPROFEN is caught by `classify_drug` alone (nsaids).
    """
    result = _run(_patient(meds=[("Warfarin", "5 mg")]))

    assert pss.verify_line(
        "Warfarin 5 mg — hold it and cover with Ceftriaxone", result) == pss.DROP_FABRICATED
    assert pss.verify_line(
        "Warfarin 5 mg interacts with the charted amiodarone", result) == pss.DROP_FABRICATED
    assert pss.verify_line(
        "Warfarin 5 mg and the ibuprofen together raise bleeding risk",
        result) == pss.DROP_FABRICATED


def test_a_drug_named_inside_the_fact_the_model_was_shown_is_not_an_invention():
    """THE GUARD MUST NOT DELETE THE TIER'S OWN TRUE SENTENCES.

    An allergy is charted as prose, and the prose routinely names a drug that is
    not the allergen: "Penicillin — widespread rash 48h into a course of
    AMOXICILLIN". That whole string is what `render_candidates` puts in the
    prompt, so a model writing "penicillin allergy may preclude amoxicillin" has
    quoted the chart back exactly as instructed.

    MEASURED ON THE SEEDED WARD, 2026-08-15: the guard deleted that line as a
    fabrication, which was the only flag on one of the six patients that reach
    the model at all. A tier that deletes its own checkable output is the
    decoration this design replaces, so "the screen printed it" now counts as
    "the chart supplied it" — the same rule numbers have always followed.
    """
    result = _run(_patient(
        allergies=[("Penicillin",
                    "Widespread maculopapular rash 48h into a course of amoxicillin",
                    "moderate")]))
    assert result["rules_fired"] == [pss.RULE_ANTIMICROBIAL_ALLERGY]
    assert "amoxicillin" in _facts(result).lower(), "the fixture lost its point"

    assert pss.verify_line(
        "Penicillin allergy may preclude amoxicillin here", result) is None, (
        "a drug the screen printed in the model's own prompt was deleted as an "
        "invention")

    # THE LICENCE IS EXACTLY AS WIDE AS THE PROMPT AND NO WIDER. A drug the fact
    # did NOT name is still an invention.
    assert pss.verify_line(
        "Penicillin allergy — give ceftriaxone instead", result) == pss.DROP_FABRICATED

    # AND IT DOES NOT WEAKEN RULE 3. Exonerating a word for the fabrication check
    # is not the same as letting it stand in for naming the fact: an incidental
    # word from the reaction text still cannot buy a line its ticket.
    assert pss.verify_line("Widespread rash is a concern", result) == pss.DROP_UNSOURCED


def test_a_drug_no_vocabulary_of_orbs_recognises_is_still_dropped():
    """THE HOLE THIS ROUND CLOSED, and the two names that proved it existed.

    MEASURED ON THIS APPLIANCE 2026-08-15: `classify_drug` gives VANCOMYCIN and
    NORADRENALINE no class, the formulary (14 rows here) does not stock them, and
    the interaction knowledge base does not name them. Every drug vocabulary Orb
    has is blind to both — so "Penicillin allergy — avoid vancomycin" used to
    SURVIVE the fabrication guard. Not because the guard judged the word safe:
    because it could not tell the word was a drug name at all.

    The first two assertions are the point. They check Orb genuinely cannot
    recognise these two, so a future formulary load that happens to catch them
    turns this into a test of the wrong thing and says so.

    NORADRENALINE IS THE ONE THAT PINS THE RULE'S SHAPE, and it is why the
    closed-vocabulary check may never be gated on "does this word LOOK like a
    drug?". That gate was written on 2026-08-15 to stop the guard deleting
    ordinary English, and it is a reasonable-sounding idea: vancomycin ends in a
    WHO class marker, so morphology catches it for free. Noradrenaline has no
    class marker and is written in lower case, so the gated guard passed it
    straight through and this file went green while "noradrenaline may be
    needed" reached the alert screen on a patient with none charted. Morphology
    is a SUFFICIENT signal and never a necessary one. The third assertion says
    the same thing about a word that is not a drug at all: unaccountable is
    unaccountable.
    """
    pid = _patient(allergies=[("Penicillin", "rash", "moderate")])
    result = _run(pid)
    with db_session() as db:
        vocabulary = pss._known_drug_tokens(db, [])
    for invisible in ("vancomycin", "noradrenaline"):
        assert not classify_drug(invisible), (
            f"{invisible} now has a drug class — this test no longer proves the "
            "closed-vocabulary rule catches what the three vocabularies miss")
        assert invisible not in vocabulary, (
            f"{invisible} is now in the formulary or the interaction table; pick "
            "a name that is still invisible or this test has stopped testing")
    # The half of the fixture that keeps the previous sentence honest: this one
    # carries no class marker either, so a morphology-shaped guard cannot be
    # what makes the assertion below pass.
    assert not pss.has_drug_morphology("noradrenaline"), (
        "noradrenaline now has drug morphology — pick a name that still has "
        "none, or this test stops proving the rule needs no shape test")

    assert pss.verify_line(
        "Penicillin allergy — avoid vancomycin", result) == pss.DROP_UNKNOWN_TERM
    assert pss.verify_line(
        "Penicillin allergy charted — noradrenaline may be needed",
        result) == pss.DROP_UNKNOWN_TERM
    # An entirely invented word is the same class of thing and goes the same way.
    assert pss.verify_line(
        "Penicillin allergy may interact with zephyrixin",
        result) == pss.DROP_UNKNOWN_TERM


def test_the_guard_states_its_boundary_rather_than_pretending_to_have_none():
    """WHAT THE GUARD STILL CANNOT DO, pinned so nobody discovers it in a pilot.

    Three things by design rather than by accident:

      * A TOKEN SHORTER THAN FOUR CHARACTERS is not examined (`_MIN_DRUG_TOKEN`),
        the same floor the drug matcher uses, because a short string matches an
        arbitrary slice of anything. "TXA" is the shape that gets through.
      * A DRUG CLASS is prose, not a name. "Cover with a carbapenem" is a
        judgement about a class and cannot send a clinician to a specific wrong
        drug; deleting it would delete most true sentences this tier can write.
      * A DRUG NAMED INSIDE A FACT THE MODEL WAS SHOWN may be repeated — that is
        the amoxicillin case below, and it is the chart speaking, not the model.

    Everything else — every word of four characters or more that is neither in
    the facts nor ordinary clinical prose — is deleted, whether or not Orb has
    ever heard of it.
    """
    result = _run(_patient(meds=[("Warfarin", "5 mg")]))

    assert pss.verify_line("Warfarin 5 mg — TXA may be needed", result) is None, (
        "documented boundary — change this test deliberately, not by accident")
    assert pss.verify_line(
        "Warfarin 5 mg charted — an antibiotic choice is not the issue here",
        result) is None
    # And the number check still runs first-class beside the word check.
    assert pss.verify_line("Warfarin 5 mg with zephyrixin 250 mg",
                           result) == pss.DROP_UNKNOWN_TERM
    assert pss.verify_line("Warfarin 250 mg is the usual dose",
                           result) == pss.DROP_FABRICATED


def test_the_prose_lexicon_contains_no_drug_name():
    """THE GUARD ON THE GUARD, and the reason this is not a fourth drug database.

    The closed vocabulary works only if it holds no drug name — one leak and the
    guard silently stops covering that drug. It is checked against ORB'S OWN
    THREE VOCABULARIES rather than against a reviewer's memory: `classify_drug`'s
    classes, the hospital formulary and the interaction ingredient list. Every
    name any of them knows must be treated as a NAME by `is_prose`, including
    after the inflection folding — the folding is where a leak would hide.
    """
    with db_session() as db:
        vocabulary = pss._known_drug_tokens(db, [])
    assert len(vocabulary) > 20, "the drug vocabularies are empty — nothing tested"

    leaks = sorted(token for token in vocabulary if pss.is_prose(token))
    assert leaks == [], (
        f"the prose lexicon treats {leaks} as ordinary English, so the guard no "
        "longer covers those drugs")
    classified = sorted(w for w in pss._PROSE if classify_drug(w))
    assert classified == [], (
        f"{classified} are drug names with a class and must not be in the lexicon")

    # THE THIRD CHECK, AND THE ONLY ONE THAT NEEDS NO DATABASE: SHAPE. Orb's
    # three vocabularies are finite and this appliance's are tiny, so "no
    # vocabulary of ours knows this word" is weak evidence that a lexicon entry
    # is not a medicine. Drug morphology needs no vocabulary at all — a word
    # ending in a WHO/USAN class marker is a drug name whoever has heard of it —
    # so it catches a leak the other two would wave through, which is exactly
    # the class of miss (vancomycin, unstocked and unclassified) that motivated
    # the closed vocabulary in the first place.
    #
    # `_PROSE_CLASSES` is exempt and must be: a class term legitimately carries
    # the marker of the class it names ("statin", "lactam", "sulfa"), and a
    # class cannot send a clinician to a specific wrong drug.
    non_class = frozenset(
        pss._fold(w)
        for chunk in (pss._PROSE_ORDINARY, pss._PROSE_ORDINARY_2,
                      pss._PROSE_CLINICAL, pss._PROSE_ORB)
        for w in chunk.split())
    shaped = sorted(w for w in non_class if pss.has_drug_morphology(w))
    assert shaped == [], (
        f"{shaped} are shaped like generic drug names and must not sit in the "
        "ordinary-prose tables — either they are medicines, or they belong in "
        "_PROSE_CLASSES with the reason written down")


def test_a_drug_class_the_chart_itself_uses_is_not_read_as_an_invention():
    """SULFA IS A CLASS, NOT A MEDICINE — and Orb already says so out of the
    other side of its mouth.

    `drug_safety_service._ALLERGEN_CLASS_TERMS` lists "sulfa" and "sulpha" as
    how a clinician CHARTS a sulfonamide allergy, which is the whole reason the
    allergy rule fires on this patient at all. The word being absent from
    `_PROSE_CLASSES` meant the guard read the flag's own class term as an
    unaccountable name and deleted a TRUE line that named the charted allergy
    and no medicine whatsoever — measured 2026-08-15, and it took eleven tests
    in tests/test_sage_assessment.py down with it, none of them about text.

    The `names_a_fact` assertion is the half that keeps this narrow: a class
    term is prose, but a line still cannot be written about a patient whose
    chart never supplied the fact.
    """
    result = _run(_patient(allergies=[("Sulfonamides", "Rash", "severe")],
                           vitals={"temperature": 38.6}))
    assert result["rules_fired"] == [pss.RULE_ANTIMICROBIAL_ALLERGY]

    assert pss.verify_line(
        "Sulfonamide allergy is charted — the sulfa-based first-line option is "
        "ruled out for this patient\ndocumented sulfa allergy with an infective "
        "picture", result) is None, (
        "the guard deleted a flag that named the charted allergy and no drug")

    # AND THE LICENCE IS EXACTLY A CLASS WIDE. A specific medicine in the same
    # sentence is still an invention.
    assert pss.verify_line(
        "Sulfonamide allergy charted — give co-trimoxazole instead",
        result) == pss.DROP_FABRICATED
    # A patient with no such allergy cannot write the line at all.
    bare = _run(_patient(meds=[("Warfarin", "5 mg")]))
    assert pss.verify_line(
        "The sulfa-based first-line option is ruled out", bare) == pss.DROP_UNSOURCED


def test_a_prefix_does_not_make_an_ordinary_word_a_name():
    """PRODUCTIVE PREFIXES, and why stripping them costs the guard nothing.

    "rechecked" is "checked", "nephrotoxic" is "toxic" — one ordinary word with
    a prefix English does not bother to hyphenate. Each deleted a true line on
    the held-out sample, and each is the same shape as the "unsafe" case the
    negation rule already covers.

    THE REMAINDER MUST STILL BE PROSE IN ITS OWN RIGHT, which is what makes this
    safe: a coined name does not decompose into English, so no prefix rescues
    one. The second block is that half.
    """
    result = _run(_patient(meds=[("Warfarin", "5 mg")],
                           labs=[("Creatinine", 180.0, "umol/L", "H", 6)]))

    assert pss.is_prose("rechecked") and pss.is_prose("nephrotoxic")
    assert pss.verify_line(
        "Warfarin with creatinine 180 — nephrotoxic choices need rechecking "
        "here", result) is None

    # A prefix on something that is not a word is not a word.
    for coined in ("prevancomycin", "subzephyrixin", "renoradrenaline"):
        assert not pss.is_prose(coined), (
            f"{coined} decomposed into ordinary English — the prefix rule has "
            "become a way to smuggle a name past the guard")


#: LINES THE MODEL ACTUALLY WROTE, on the seeded ward, through the production
#: prompt at the production temperature. Kept verbatim except for the patient's
#: own drug and dose, which are swapped for this file's fixture.
#:
#: THE MEASUREMENT THEY COME FROM (medgemma:27b, `_build_prompt_brief`,
#: temperature 0.2, num_predict 96 — the shipping settings — over every admitted
#: patient the screen fires on, 2026-08-15): 258 generated lines across eight
#: runs. The closed-vocabulary rule deleted FIVE of them, and every one was an
#: ordinary word missing from the table rather than an invention: "unsafe",
#: "drops", "toxicity", "thereof", "thrombolysis". Each was added; on the final
#: two runs (66 fresh lines at temperature 0.2 and 0.9) it deleted NONE.
#:
#: This table is what stops that regressing. It is not a style guide for the
#: model — it is the shape of sentence this ward measurably produces, and a
#: change to the lexicon that starts deleting one of them is a change that would
#: have cost a clinician a true flag.
MEASURED_ON_THE_WARD = [
    "Warfarin 5 mg (anticoagulant, on the chart) — hypotension may be occult bleeding",
    "Anticoagulation may increase bleeding risk if intervention is needed",
    "Anticoagulation may be a contraindication to invasive procedures if needed",
    "Anticoagulation might be a relative contraindication to invasive procedures "
    "or thrombolysis if needed",
    "Anticoagulation may be related to bleeding or risk thereof",
    "Anticoagulation increases bleeding risk if BP drops further",
    "Warfarin may worsen hypotension; this anticoagulation may be unsafe here",
    "Warfarin 5 mg is on the chart — bleeding risk assessment needed",
    "Anticoagulation status affects the bleeding risk of any procedure tonight",
    # The two the sibling suite (tests/test_assessment_tiers.GOOD_STEPS) streams
    # through the real lane. If the guard ever eats one of these, that file goes
    # red for a reason nobody would look for in this one.
    "Warfarin 5 mg charted — hypotension here may be occult bleeding\n"
    "Changes the differential",
    "Penicillin allergy charted (rash) — first-line cover must avoid it\n"
    "Rules out the obvious agent",
    # And the wedge end-to-end fixture (tests/test_wedge_end_to_end.TWO_CARD_STEPS),
    # which is how "reverse" and "theatre" were found missing: without them the
    # guard deleted this line and took three whole-loop tests with it. THE WORDS A
    # CLINICIAN ACTUALLY USES ABOUT ANTICOAGULATION ARE PART OF THE GUARD.
    "Warfarin charted — reverse before theatre\nanticoagulated",
    "Warfarin charted — consider occult bleeding\nanticoagulated and hypotensive",
    "Penicillin allergy charted — first-line cover is out\n"
    "documented allergy, infective picture",
    # And the assessment suite's own fixture (tests/test_sage_assessment
    # .BRIEF_STEPS), which was NOT pinned here and so was not protected: a
    # lexicon change deleted it on 2026-08-15 and took eleven tests down, none
    # of them about text — the medication gate, the cards, the episode, the HTTP
    # contract. The systolic is this file's fixture's rather than that one's,
    # per the note above. Its allergy line needs a sulfonamide-allergic patient
    # and lives in `test_a_drug_class_the_chart_itself_uses_is_not_read_as_an
    # _invention`.
    "Warfarin on the chart with a systolic of 88 — occult bleeding belongs in "
    "the differential\nanticoagulated and hypotensive",
]

MEASURED_ON_THE_WARD_ALLERGY = [
    "Penicillin allergy may preclude amoxicillin use",
    "This allergy rules out penicillin-class antibiotics",
    "Penicillin allergy — first-line agent choice is affected",
    "This allergy contraindicates the use of the first-line agent",
    "Penicillin allergy limits the empiric antibiotic choice",
    "Allergy is charted as moderate — antibiotic choice must avoid it",
]


@pytest.mark.parametrize("line", MEASURED_ON_THE_WARD)
def test_a_line_this_ward_really_produced_is_not_deleted(line):
    result = _run(_patient(meds=[("Warfarin", "5 mg")],
                           allergies=[("Penicillin", "rash", "moderate")],
                           vitals={"temperature": 38.6}))

    assert pss.verify_line(line, result) is None, (
        "the guard deleted a line the ward actually produced — this is the "
        "false-positive failure that killed the previous version of this tier")


@pytest.mark.parametrize("line", MEASURED_ON_THE_WARD_ALLERGY)
def test_an_allergy_line_this_ward_really_produced_is_not_deleted(line):
    result = _run(_patient(
        allergies=[("Penicillin",
                    "Widespread maculopapular rash 48h into a course of amoxicillin",
                    "moderate")]))

    assert pss.verify_line(line, result) is None


def test_orbs_own_scoring_words_are_not_treated_as_inventions():
    """The model is shown Orb's deterministic block as well as the facts, so
    quoting "Systolic BP" or "consciousness" back is reading, not remembering.

    Measured on the ward: the model echoes the whole hook — "Enoxaparin 40mg SC
    OD — Systolic BP 94mmHg scoring 2" — far more often than it writes prose, so
    a guard that did not count Orb's own block as supplied would delete the most
    common shape of true output this tier produces.
    """
    pid = _patient(meds=[("Warfarin", "5 mg")])
    result = _run(pid)

    for word in ("systolic", "respiratory", "temperature"):
        assert word in result["supplied_tokens"], (
            f"{word} is on the clinician's panel and in the model's prompt, but "
            "the guard would treat it as remembered")
    # A parameter this patient's block does NOT carry (ACVPU is omitted while
    # the patient is alert) is still safe, because Orb's own vocabulary is in
    # the prose table as a floor. Belt and braces, and the braces are per-patient.
    assert "consciousness" not in result["supplied_tokens"]
    assert pss.is_prose("consciousness") and pss.is_prose("acvpu")


def test_an_invented_dose_or_lab_value_is_dropped():
    """THE STRICTER HALF, and it has no gap: every figure must be one the screen
    supplied or one of Orb's own pinned scoring values. An invented dose is
    deleted whatever word it sits beside."""
    result = _run(_patient(meds=[("Warfarin", "5 mg")],
                           labs=[("Creatinine", 180.0, "umol/L", "H", 6)]))

    assert pss.verify_line("Warfarin 5 mg with creatinine 180", result) is None
    assert pss.verify_line("Warfarin 9 mg is above the usual dose",
                           result) == pss.DROP_FABRICATED
    assert pss.verify_line("Warfarin charted; creatinine 320 is dangerous",
                           result) == pss.DROP_FABRICATED


def test_a_flag_naming_the_drug_the_way_a_clinician_shortens_it_survives():
    """A DROPPED TRUE FLAG IS A SILENT LOSS, and this is the shape one takes.

    The chart holds "Metolar 50"; a model handed that fact writes "Metolar",
    because that is how the drug is spoken about. `names_a_fact` matches the
    allowed term as a substring, so without the charted name being expanded into
    the words a clinician would shorten it to, a correct and checkable flag is
    deleted as unsourced — the tier failing silently in the direction nobody
    would notice.
    """
    result = _run(_patient(meds=[("Metolar 50", "50 mg")]))

    assert pss.verify_line(
        "Metolar is blunting this pulse — 78 is not reassurance", result) is None
    # The full charted string still works, and so does the allergy equivalent.
    assert pss.verify_line(
        "Metolar 50 is blunting this pulse", result) is None

    allergic = _run(_patient(allergies=[("Amoxicillin 500mg", "rash", "moderate")]))
    assert pss.verify_line(
        "Amoxicillin allergy rules out the first-line agent", allergic) is None


def test_orbs_own_deterministic_scoring_figures_are_quotable():
    """The numbers already on the clinician's screen are pinned and cannot be
    wrong, so a flag hooking itself to one is not fabricating."""
    result = _run(_patient(meds=[("Metoprolol", "50 mg")]))

    assert pss.verify_line(
        "Metoprolol 50 mg — heart rate 78 is not reassurance at this systolic 88",
        result) is None


def test_the_filters_are_applied_in_priority_order():
    """A line that is BOTH generic and fabricated is reported as generic, because
    that is the one the prompt is meant to prevent and the rate anyone tunes."""
    result = _run(_patient(meds=[("Warfarin", "5 mg")]))

    assert pss.verify_line("Escalate immediately and start Ceftriaxone 2 g",
                           result) == pss.DROP_GENERIC


def test_the_rendered_prompt_lines_are_the_only_clinical_content_and_are_traceable():
    """The model is never shown the chart. It is shown these lines, numbered, so
    a flag can be traced back to the fact that produced it — and so there is
    nothing else in the context window to fabricate FROM."""
    result = _run(_patient(meds=[("Warfarin", "5 mg")],
                           allergies=[("Penicillin", "rash", "moderate")],
                           vitals={"temperature": 38.6}))
    lines = pss.render_candidates(result)

    assert len(lines) == len(result["candidates"]) >= 2
    assert lines[0].startswith("F1. ") and lines[1].startswith("F2. ")
    assert "Warfarin" in lines[0] and "Penicillin" in lines[1]
    # Each carries the question the model is being asked about that one fact —
    # not an invitation to write management.
    assert all(line.rstrip().endswith("]") for line in lines)
