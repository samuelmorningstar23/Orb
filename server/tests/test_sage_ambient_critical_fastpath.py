"""Deterministic critical-value fast-path — the model-independent risk floor.

WHAT THIS FILE IS FOR
---------------------
Measured with the 4B ambient resident: in 1 run of 8 the classifier returned
`risk_detail=None` on BOTH the critical message ("POTASSIUM 6.8, ECG shows
peaked T waves") and the follow-up treatment message, so a lethal electrolyte
produced NO RISK CARD AT ALL — the room had no standing alarm for the rest of
the admission. `_deterministic_risk_extract` charts the card from the raw
message text instead, so the floor no longer depends on a 4B being a reliable
detector of "this is critical".

Every assertion here runs WITHOUT A MODEL. `classification=None` is passed
deliberately in the integration tests: that is the production state this feature
exists for (ambient tier evicted, LLM slot busy, unparseable JSON, Gate 5 miss),
and it is the state in which a missing card is most dangerous.

WHAT IT DOES NOT PROVE
----------------------
That the ward's real phrasings are covered. The corpora below are authored, not
harvested. They lock down the rules, the units, the suppressors and the
interaction with retraction retirement; they say nothing about recall on a real
Relay transcript, which needs the live harness.
"""
import json

import pytest

from services.sage_ambient_service import (
    _CRIT_CONFIDENCE,
    _deterministic_risk_extract,
    _gate_content,
    _retire_retracted_risk_flags,
    _risk_already_charted,
    _update_memory,
)


def _details(text):
    return [f["detail"] for f in _deterministic_risk_extract(text)]


# ─────────────────────────────────────────────────────────────────────
# 1. Every encoded threshold fires
# ─────────────────────────────────────────────────────────────────────
# (message, substring that must appear in the charted card)
THRESHOLD_CASES = [
    # Potassium. UK Renal Association (2020): moderate 6.0-6.4, severe >=6.5.
    ("Potassium 6.8 mmol/L on the gas.", "potassium 6.8"),
    ("K 6.1", "potassium 6.1"),
    ("K+ 7.2 just back from the lab.", "potassium 7.2"),
    ("Serum potassium 2.1 mmol/L after the frusemide.", "potassium 2.1"),
    # Sodium. ESE/ESICM/ERA-EDTA (2014) profound hyponatraemia; lab critical
    # limits for the high side.
    ("Sodium 158 mmol/L, she is confused.", "sodium 158"),
    ("Na 112, had a seizure.", "sodium 112"),
    # Glucose. International Hypoglycaemia Study Group / ADA (2017) Level 2.
    ("Glucose 2.1 mmol/L, clammy and drowsy.", "glucose 2.1 mmol/L"),
    ("RBS 38 mg/dL, gave 25% dextrose.", "glucose 38 mg/dL"),
    ("GRBS 40 mg%, unresponsive.", "glucose 40 mg/dL"),
    # Lactate. Surviving Sepsis Campaign / Sepsis-3.
    ("Lactate 6.2 mmol/L on the VBG.", "lactate 6.2"),
    # pH. ADA/JBDS DKA grading; laboratory critical-value limits.
    ("ABG shows pH 7.05.", "pH 7.05"),
    # Platelets. BSH platelet transfusion guideline (2017).
    ("Platelets 15 this morning.", "platelets 15"),
    ("Platelet count 15,000 on the repeat film.", "platelets 15"),
    ("plt 12k", "platelets 12"),
    # Haemoglobin. AABB (2016) / NICE NG24 restrictive threshold 70 g/L.
    ("Hb 6.2 g/dL and actively bleeding.", "haemoglobin 6.2"),
    ("Haemoglobin 62 g/L on the count.", "haemoglobin 6.2"),
    # Troponin. Fourth Universal Definition of MI (2018).
    ("Troponin is rising, 340 then 1200.", "troponin"),
    ("hs-troponin positive.", "troponin"),
    # ECG. UK Renal Association hyperkalaemia progression; ESC STEMI.
    ("ECG shows peaked T waves.", "peaked T waves"),
    ("T waves are peaked on the strip.", "peaked T waves"),
    ("Wide QRS on the monitor.", "wide QRS"),
    ("Broad complexes on the rhythm strip.", "wide QRS"),
    ("New ST elevation in the inferior leads.", "new ST elevation"),
    # Vitals. BTS oxygen guideline (2017) and RCP NEWS2 (2017).
    ("SpO2 86% on 15L non-rebreathe.", "SpO2 86%"),
    ("Sats 84 on air.", "SpO2 84%"),
    ("BP 82/48, peripherally shut down.", "systolic BP 82"),
    ("Systolic 78, fluids running.", "systolic BP 78"),
    ("RR 34 with accessory muscle use.", "respiratory rate 34"),
    ("GCS 7, not protecting her airway.", "GCS 7"),
]


@pytest.mark.parametrize("text,expected", THRESHOLD_CASES,
                         ids=[t[:38] for t, _ in THRESHOLD_CASES])
def test_each_threshold_fires(text, expected):
    found = _details(text)
    assert found, f"no critical card for {text!r}"
    assert any(expected.lower() in d.lower() for d in found), \
        f"card did not name the finding: {found}"


NORMAL_VALUES = [
    "Potassium 4.2 mmol/L, all within range.",
    "Sodium 138, unremarkable.",
    "Glucose 5.4 mmol/L before lunch.",
    "RBS 110 mg/dL two hours post meal.",
    "Lactate 1.4 on the gas.",
    "pH 7.38, compensated.",
    "Platelets 250 and stable.",
    "Hb 11.2 g/dL.",
    "Sats 96% on room air.",
    "BP 124/78.",
    "RR 18, comfortable at rest.",
    "GCS 15, fully alert.",
]


@pytest.mark.parametrize("text", NORMAL_VALUES, ids=[t[:32] for t in NORMAL_VALUES])
def test_normal_values_are_silent(text):
    """A rule that fires on normal numbers is alert fatigue, and alert fatigue
    is how the feature gets switched off before the real hyperkalaemia lands."""
    assert _deterministic_risk_extract(text) == [], \
        f"false critical alarm on a normal value: {text!r}"


# ─────────────────────────────────────────────────────────────────────
# 2. Units
# ─────────────────────────────────────────────────────────────────────
def test_both_glucose_unit_systems_fire_when_the_unit_is_stated():
    """An Indian ward charts mg/dL; the guideline is written in mmol/L."""
    assert _details("Glucose 2.5 mmol/L, sweaty."), "mmol/L hypoglycaemia missed"
    assert _details("CBG 42 mg/dL, unresponsive."), "mg/dL hypoglycaemia missed"


def test_potassium_is_the_same_value_with_or_without_its_unit():
    """"K 6.8" and "potassium 6.8 mmol/L" are the same result."""
    bare = _details("K 6.8")
    spelt = _details("potassium 6.8 mmol/L")
    assert bare and spelt
    assert "6.8" in bare[0] and "6.8" in spelt[0]


AMBIGUOUS_GLUCOSE = [
    "Glucose 45.",       # 45 mg/dL is critical; 45 mmol/L is hyperglycaemia
    "Sugar 50 on the ward round.",
    "RBS 30.",           # still disagrees: 30 mg/dL critical, 30 mmol/L high
]


@pytest.mark.parametrize("text", AMBIGUOUS_GLUCOSE, ids=[t[:28] for t in AMBIGUOUS_GLUCOSE])
def test_ambiguous_glucose_units_do_not_fire(text):
    """When the two unit readings DISAGREE on criticality, stay silent.

    "glucose 90" is ordinary in mg/dL and impossible in mmol/L; "glucose 45" is
    a treat-now hypo in mg/dL and mere hyperglycaemia in mmol/L. There is no
    safe way to guess, and guessing wrong on the common direction would fire a
    false critical on a normal sugar many times a day.
    """
    assert _deterministic_risk_extract(text) == [], \
        f"fired on an ambiguous unit: {text!r}"


def test_unitless_glucose_fires_only_where_both_scales_agree():
    """2.4 is critical read as mmol/L AND as mg/dL, so it is safe to fire."""
    assert _details("Glucose 2.4, drowsy."), "both scales agree — should fire"
    assert not _details("Glucose 90."), "normal in mg/dL — must not fire"


def test_platelet_notations_all_resolve_to_the_same_count():
    """15, 15,000 and 15k are one number written three ways on one ward."""
    for text in ("Platelets 15.", "Platelets 15,000.", "Platelets 15k."):
        found = _details(text)
        assert found, f"missed {text!r}"
        assert "15" in found[0]
    assert not _details("Platelets 1.5 lakh."), "150 x10^9/L is a normal count"


def test_haemoglobin_gdl_and_gl_both_resolve():
    assert _details("Hb 6.5 g/dL"), "g/dL missed"
    assert _details("Hb 65 g/L"), "g/L missed"
    assert not _details("Hb 105 g/L"), "10.5 g/dL is not critical"


# ─────────────────────────────────────────────────────────────────────
# 3. Negation, hypothesis, targets, history, supersession
# ─────────────────────────────────────────────────────────────────────
SUPPRESSED = [
    # Supersession — the phrasing the brief names explicitly.
    ("potassium was 6.8, now 4.1", "superseded"),
    ("The potassium was 6.8 this morning, repeat is 4.1.", "superseded"),
    ("K 6.8 earlier, repeat potassium 4.1.", "superseded"),
    ("GCS was 7 on arrival, now 14.", "superseded/history"),
    # Negation.
    ("There is no hyperkalaemia and no peaked T waves.", "negation"),
    ("ECG clear, no peaked T waves.", "negation"),
    ("Troponin is not rising.", "negation"),
    ("Troponin negative.", "no positive descriptor"),
    ("No new ST elevation on the repeat ECG.", "negation"),
    # Hypothesis — the brief names this one too.
    ("if the potassium goes above 6 call me", "hypothetical"),
    ("If the potassium is 6.5 or more, give calcium gluconate.", "hypothetical"),
    ("Should she drop below GCS 9 we will intubate.", "hypothetical"),
    ("In case the sodium falls below 120, restrict fluids.", "hypothetical"),
    # Targets and protocol thresholds.
    ("Keep the potassium below 5.5 please.", "target"),
    ("Transfusion threshold Hb 7 as per protocol.", "target"),
    ("Target sats 88-92% in COPD.", "target"),
    ("Monitor for a lactate above 4.", "target"),
    ("Watch for a GCS drop below 9.", "target"),
    ("Aim for a systolic above 90.", "target"),
    # History.
    ("History of hyperkalaemia and wide QRS in 2019.", "history frame"),
    ("h/o troponin rising on the last admission", "history frame"),
    ("Potassium 6.8 on admission, now 4.4 after treatment.", "history"),
    ("Sats 84 yesterday, 95 today.", "history"),
]


@pytest.mark.parametrize("text,why", SUPPRESSED, ids=[t[:38] for t, _ in SUPPRESSED])
def test_negation_history_and_hypotheticals_do_not_fire(text, why):
    assert _deterministic_risk_extract(text) == [], \
        f"fired on a {why} phrasing — this is the alert fatigue case: {text!r}"


# The other half of the suppressor contract. Each of these carries suppressor
# vocabulary that must NOT reach the value, and each is a live emergency: a
# suppressor scoped to the whole message would silence all of them, and that
# miss is worse than the false alarm it prevents.
SUPPRESSOR_MUST_NOT_OVERREACH = [
    "She has no fever and potassium 6.8 with peaked T waves.",
    "No IV access yet. K 6.8 on the gas.",
    "Not on dialysis, potassium 6.8.",
    "Admitted yesterday with pneumonia, potassium 6.8 now.",
    "Known CKD, potassium 6.8 today.",
    "Potassium 6.8 - if it climbs further we will dialyse.",
    "Monitor closely, potassium 6.8 with ECG changes.",
    "K 6.8, now on 10 units of insulin with dextrose.",
]


@pytest.mark.parametrize("text", SUPPRESSOR_MUST_NOT_OVERREACH,
                         ids=[t[:38] for t in SUPPRESSOR_MUST_NOT_OVERREACH])
def test_suppressors_do_not_silence_a_live_critical(text):
    assert _deterministic_risk_extract(text), \
        f"a suppressor reached across and killed a live critical: {text!r}"


def test_vitamin_k_is_not_a_potassium():
    """`K` has to be in the lexicon — a ward writes "K 6.8" — and that makes
    "Vitamin K 10 mg" a potassium of 10 unless it is excluded explicitly."""
    assert not _deterministic_risk_extract("Vitamin K 10 mg IV given.")
    assert not _deterministic_risk_extract("vit k 12 mg stat")


def test_hba1c_is_not_a_haemoglobin():
    assert not _deterministic_risk_extract("HbA1c 6.5%, control is poor.")


def test_sodium_chloride_is_not_a_sodium_result():
    assert not _deterministic_risk_extract("Started 0.9% NaCl 1L over 4 hours.")


PRODUCT_NOT_PATIENT = [
    "Hb 1 unit transfused overnight.",
    "Platelets 1 pool given.",
    "Platelets 2 packs ordered from the blood bank.",
    "Glucose 50 ml IV push.",
]


@pytest.mark.parametrize("text", PRODUCT_NOT_PATIENT,
                         ids=[t[:32] for t in PRODUCT_NOT_PATIENT])
def test_a_dose_or_a_product_is_not_a_result(text):
    """Every threshold in this file is a small number, so a dose sitting next to
    an analyte name lands squarely inside the critical band. Without this guard
    an ordinary prescribing message charts a severe anaemia."""
    assert _deterministic_risk_extract(text) == [], \
        f"charted a critical value off a product/dose: {text!r}"


def test_a_negated_first_mention_does_not_hide_a_live_second_one():
    """Phrase rules scan every occurrence, not just the first. Otherwise the
    reassuring earlier sentence buries the emergency in the later one."""
    assert _deterministic_risk_extract(
        "No peaked T waves at 10:00. Peaked T waves now on the repeat ECG."), \
        "a negated first mention hid a live second mention"


def test_a_dipstick_is_not_a_serum_result():
    """"Urine sugar 2+" would otherwise chart profound hypoglycaemia."""
    assert not _deterministic_risk_extract("Urine sugar 2+ on the dipstick.")
    assert _deterministic_risk_extract("Urine output 20 ml, potassium 6.8."), \
        "the specimen suppressor reached past its comma and killed a live value"


# ─────────────────────────────────────────────────────────────────────
# 3b. The alert-fatigue corpus
# ─────────────────────────────────────────────────────────────────────
# The way this feature actually fails in a pilot is not a missed potassium — it
# is a card on a routine observation set, three times a shift, until a charge
# nurse asks for the whole thing to be turned off. Then the real hyperkalaemia
# has no card either. So the false-alarm rate on ORDINARY traffic is the number
# that decides whether the feature survives contact with a ward, and it is
# pinned at zero here. Measured 0/40 at the time of writing.
#
# These are deliberately the messages MOST likely to trip a numeric rule: full
# observation sets, prescriptions with doses, transfusion records, ABGs, lakh
# notation, "2 hourly" frequencies, dipsticks, HbA1c, vitamin K.
ORDINARY_WARD_TRAFFIC = [
    "Family visiting this afternoon, they asked for a quiet side room.",
    "Physio came by and she managed to sit out in the chair for an hour.",
    "Notes filed and the whiteboard is updated for the ward round.",
    "Obs at 0600: temp 37.2, HR 88, BP 118/72, RR 16, sats 97% on air.",
    "Creatinine 2.1 this morning, up from 1.4 on admission.",
    "Started IV ceftriaxone 2 g once daily for the pneumonia.",
    "Gave 1 unit of packed cells, Hb was 8.1 pre-transfusion.",
    "Insulin sliding scale running, RBS 180 mg/dL at 6 hourly checks.",
    "Catheter output 40 ml over the last 2 hours.",
    "Transfer the patient to the OR for an emergency laparotomy.",
    "Discharge planned for tomorrow if the sats stay above 94 on air.",
    "Please chart 4 hourly paracetamol 1 g PO.",
    "Doing 2 hourly turns, pressure areas intact.",
    "ECG done, sinus rhythm at 78, no acute changes.",
    "ABG on 2L: pH 7.41, pCO2 5.1, pO2 9.8, lactate 1.2.",
    "Potassium 4.9 and magnesium 0.8, replacing the mag.",
    "Repeat FBC tomorrow: Hb 9.4, platelets 180, WCC 14.",
    "Weaned oxygen from 4L to 2L, sats 95%.",
    "GCS 14 (E4 V4 M6), slightly drowsy but rousable.",
    "Two doses of furosemide 40 mg IV given today.",
    "Pain score 6 out of 10, gave 5 mg oral morphine.",
    "Urine dipstick: protein 2+, blood 1+, nitrites negative.",
    "Started on 5 units of Actrapid with 50 ml of 50% dextrose.",
    "Her sodium is 134 and improving with fluid restriction.",
    "Troponin sent as part of the workup, awaiting result.",
    "Troponin 12, within normal limits.",
    "Blood sugars have been 6 to 9 mmol/L all week.",
    "Platelets 15 lakh on the report, no bleeding.",
    "Vitamin K 10 mg IV for the raised INR.",
    "HbA1c 8.2%, needs diabetic review.",
    "The pH probe study is booked for next Tuesday.",
    "RR 2 hourly obs from now please.",
]


@pytest.mark.parametrize("text", ORDINARY_WARD_TRAFFIC,
                         ids=[t[:40] for t in ORDINARY_WARD_TRAFFIC])
def test_ordinary_ward_traffic_charts_nothing(text):
    assert _deterministic_risk_extract(text) == [], \
        (f"false critical alarm on routine traffic — this is how the feature "
         f"gets switched off at the ward: {text!r}")


# ─────────────────────────────────────────────────────────────────────
# 4. The card renders, and it retires
# ─────────────────────────────────────────────────────────────────────
def test_confidence_clears_the_sidebar_render_threshold():
    """SageAmbientSidebar.tsx:92 filters risk_flags at confidence >= 0.80.
    A card charted below it exists in the database and is invisible on the ward,
    which is indistinguishable from not charting it at all."""
    assert _CRIT_CONFIDENCE >= 0.80


@pytest.mark.parametrize("retraction", [
    # The ward writes British English...
    "The lab rang about that sample — haemolysed. The repeat potassium is 4.1. "
    "There is no hyperkalaemia.",
    # ...and medgemma writes American. This mismatch already broke retirement
    # once (see test_sage_ambient_retraction.py), so the new card must not
    # reintroduce it from the other side.
    "Lab called back: that potassium was a haemolysed sample, repeat is 4.1. "
    "No hyperkalemia.",
])
def test_a_later_retraction_retires_the_fast_path_card(retraction):
    findings = _deterministic_risk_extract(
        "POTASSIUM 6.8 mmol/L. ECG shows peaked T waves.")
    assert findings, "precondition: the fast-path must chart something"

    flags = [{"detail": f["detail"], "rationale": f["rationale"],
              "confidence": _CRIT_CONFIDENCE} for f in findings]
    flags, retired = _retire_retracted_risk_flags(flags, retraction)

    assert retired == len(flags), (
        f"a fast-path card survived a retraction naming its subject: "
        f"{[f for f in flags if not f.get('retracted')]}"
    )
    live = [f for f in flags if (f.get("confidence") or 0) >= 0.80]
    assert not live, f"retracted card still renders as ACTIVE: {live}"


def test_retiring_a_fast_path_card_does_not_touch_an_unrelated_one():
    """The fast-path is the first thing in this file to put UNITS and RULE
    vocabulary into flag text ("mmol/L", "critical threshold"). Every card it
    writes shares those words, so if they were allowed to identify a retraction
    subject, withdrawing any one result would silently zero all the others."""
    flags = [dict(f, confidence=_CRIT_CONFIDENCE) for f in [
        {"detail": d["detail"], "rationale": d["rationale"]}
        for d in _deterministic_risk_extract(
            "Potassium 6.8 mmol/L. Platelets 15. Lactate 6.2 mmol/L.")
    ]]
    assert len(flags) == 3, f"precondition failed: {flags}"

    flags, retired = _retire_retracted_risk_flags(
        flags, "That platelet count of 15 was a clotted sample — the repeat is "
               "210, so disregard it.")
    assert retired == 1, f"a platelet retraction retired {retired} cards"
    survivors = [f for f in flags if not f.get("retracted")]
    assert len(survivors) == 2
    assert all("platelet" not in f["detail"].lower() for f in survivors)


def test_a_retraction_message_never_charts_a_card_of_its_own():
    """"the potassium of 6.8 was haemolysed" states a critical value in order to
    WITHDRAW it. Charting off that sentence would re-raise the alarm the message
    exists to cancel."""
    ROOM, PATIENT = "chan-crit-retr", "PAT-CRIT-RETR"
    _clear(ROOM)
    try:
        _update_memory(ROOM, PATIENT, _msg(1, "Routine review, nothing new."),
                       None, None)
        _update_memory(
            ROOM, PATIENT,
            _msg(2, "The potassium of 6.8 was a haemolysed sample — ignore it, "
                    "the repeat is 4.1."),
            None, _row(ROOM))
        flags = json.loads(_row(ROOM)["risk_flags"] or "[]")
        assert not [f for f in flags if (f.get("confidence") or 0) >= 0.80], \
            f"a retraction message charted its own critical card: {flags}"
    finally:
        _clear(ROOM)


# ─────────────────────────────────────────────────────────────────────
# 5. Fallback only, and idempotent
# ─────────────────────────────────────────────────────────────────────
def test_no_second_card_when_the_classifier_already_charted_the_value():
    """The exact shape medgemma emitted live, US spelling and all."""
    existing = [{
        "detail": "Hyperkalemia (Potassium 6.8 mmol/L) with peaked T waves.",
        "rationale": "Life-threatening arrhythmia risk.",
        "confidence": 0.95,
    }]
    for finding in _deterministic_risk_extract(
            "POTASSIUM 6.8. ECG shows peaked T waves."):
        assert _risk_already_charted(existing, finding), (
            f"would have duplicated a card the classifier already charted: "
            f"{finding['detail']!r}"
        )


def test_a_retracted_card_does_not_block_a_genuinely_new_value():
    """Getting this backwards is fatal in the quiet direction.

    A retracted "potassium 6.8" flag must not suppress a later, real
    "potassium 7.4" — but replaying the retracted message itself must not
    resurrect the zeroed card at 0.85 either.
    """
    retracted = [{
        "detail": "Hyperkalaemia — potassium 6.8 mmol/L",
        "rationale": "Critical-value rule.",
        "confidence": 0.0, "retracted": True,
    }]
    new_value = _deterministic_risk_extract("Repeat potassium 7.4 mmol/L.")[0]
    assert not _risk_already_charted(retracted, new_value), \
        "a retracted 6.8 silenced a live 7.4"

    same_value = _deterministic_risk_extract("Potassium 6.8 mmol/L.")[0]
    assert _risk_already_charted(retracted, same_value), \
        "replaying the retracted message would resurrect the card at 0.85"


ROOM = "chan-crit-fastpath"
PATIENT = "PAT-CRIT-FASTPATH"


def _clear(room=ROOM):
    from database import db_session
    with db_session() as db:
        db.execute("DELETE FROM sage_room_memory WHERE room_id = ?", (room,))


def _row(room=ROOM):
    from database import db_session
    with db_session() as db:
        r = db.execute("SELECT * FROM sage_room_memory WHERE room_id = ?",
                       (room,)).fetchone()
        return dict(r) if r else None


def _msg(mid, content):
    return {"id": mid, "sender_name": "Nurse Priya Reddy",
            "sender_role": "nurse", "content": content}


@pytest.fixture
def clean_room():
    _clear()
    yield
    _clear()


def _live(room=ROOM):
    return [f for f in json.loads((_row(room) or {}).get("risk_flags") or "[]")
            if (f.get("confidence") or 0) >= 0.80]


def test_message_replay_does_not_stack_duplicate_cards(clean_room):
    """Relay redelivery is real — tests/test_sage_ambient_retraction.py already
    pins the same property for the narrative note."""
    text = "POTASSIUM 6.8 mmol/L. ECG shows peaked T waves."
    for mid in (1, 2, 3, 4):
        _update_memory(ROOM, PATIENT, _msg(mid, text), None, _row())
    flags = json.loads(_row()["risk_flags"] or "[]")
    potassium = [f for f in flags if "potassium" in f["detail"].lower()]
    assert len(potassium) == 1, f"the card stacked on replay: {flags}"


# ─────────────────────────────────────────────────────────────────────
# 6. End to end through _update_memory — the measured failure itself
# ─────────────────────────────────────────────────────────────────────
def test_the_measured_failure_now_produces_a_standing_alarm(clean_room):
    """THE REGRESSION THIS FEATURE EXISTS FOR.

    1 run in 8: the 4B returned risk_detail=None on the critical message AND on
    the treatment message, so a potassium of 6.8 with peaked T waves charted no
    risk card at all and the room had no standing alarm. Both messages below are
    passed with `classification=None` — the exact classifier output that failed.
    """
    _update_memory(ROOM, PATIENT,
                   _msg(1, "Lab rang - POTASSIUM 6.8 mmol/L. ECG shows peaked "
                           "T waves."), None, None)
    _update_memory(ROOM, PATIENT,
                   _msg(2, "Giving calcium gluconate and an insulin-dextrose "
                           "infusion now."), None, _row())

    live = _live()
    assert live, "still no risk card for a potassium of 6.8 with peaked T waves"
    assert any("6.8" in f["detail"] for f in live), \
        f"the card does not name the value: {live}"
    assert any("potassium" in f["detail"].lower() for f in live), \
        f"the card does not name the analyte: {live}"


def test_the_standing_alarm_is_taken_down_by_a_ward_retraction(clean_room):
    """Charting is only half of it — an alarm that cannot be withdrawn becomes
    the previous bug in this file (a dead value that still looks alive)."""
    _update_memory(ROOM, PATIENT,
                   _msg(1, "Lab rang - POTASSIUM 6.8 mmol/L. ECG shows peaked "
                           "T waves."), None, None)
    assert _live(), "precondition: the card must be up first"

    _update_memory(ROOM, PATIENT,
                   _msg(2, "The lab called back about that potassium — the "
                           "sample was haemolysed. The repeat potassium is 4.1. "
                           "There is no hyperkalaemia."),
                   None, _row())

    assert not _live(), f"the fast-path card survived the retraction: {_live()}"
    assert "RETRACTED" in (_row()["discussion_summary"] or ""), \
        "the narrative lost the withdrawal"


def test_the_classifier_keeps_priority_end_to_end(clean_room):
    """When the model DOES chart the risk, the fast-path must add nothing."""
    charted = {
        "type": "escalation_signal", "confidence": 0.95,
        "signal": "Critical potassium with ECG changes",
        "risk_detail": "Hyperkalemia (Potassium 6.8 mmol/L) with peaked T waves.",
        "risk_rationale": "Life-threatening arrhythmia risk.",
    }
    _update_memory(ROOM, PATIENT,
                   _msg(1, "Lab rang - POTASSIUM 6.8 mmol/L. ECG shows peaked "
                           "T waves."), charted, None)
    flags = json.loads(_row()["risk_flags"] or "[]")
    assert len(flags) == 1, f"the fast-path duplicated the classifier: {flags}"
    assert flags[0]["confidence"] == 0.95, "the classifier's card was overwritten"


def test_a_charted_card_is_marked_as_rule_derived(clean_room):
    """Provenance. A clinician triaging AI cards, and anyone reading the audit
    trail later, must be able to tell a regex from the model."""
    _update_memory(ROOM, PATIENT, _msg(1, "GCS 6, snoring respiration."),
                   None, None)
    flags = json.loads(_row()["risk_flags"] or "[]")
    assert flags and flags[0].get("source") == "deterministic_critical_value"


def test_an_empty_headline_is_filled_but_a_live_one_is_never_overwritten(clean_room):
    _update_memory(ROOM, PATIENT, _msg(1, "Sats 84 on air."), None, None)
    assert "84" in (_row()["current_concern"] or ""), \
        "an empty headline was left empty next to a live critical card"

    before = _row()["current_concern"]
    _update_memory(ROOM, PATIENT, _msg(2, "BP 82/48 as well."), None, _row())
    assert _row()["current_concern"] == before, \
        "a regex overwrote an existing headline"


def test_at_most_three_cards_from_one_message(clean_room):
    """_merge_risk keeps only the LAST 5 flags, so an unbounded burst from one
    pasted lab dump would silently evict live cards charted earlier."""
    _update_memory(
        ROOM, PATIENT,
        _msg(1, "Potassium 6.8 mmol/L. Sodium 158 mmol/L. Lactate 6.2 mmol/L. "
                "pH 7.05. Platelets 15. Hb 6.1 g/dL. GCS 7. RR 36."),
        None, None)
    flags = json.loads(_row()["risk_flags"] or "[]")
    assert 0 < len(flags) <= 3, f"charted {len(flags)} cards from one message"


# ─────────────────────────────────────────────────────────────────────
# 7. Gate 3 and the kill switch
# ─────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("text", ["K 6.8", "pH 7.0", "GCS 6", "Na 118"])
def test_ward_shorthand_under_eight_characters_still_reaches_memory(text):
    """Gate 3 returns BEFORE `_update_memory`, and the values that kill fastest
    have the shortest shorthand. Dropping these here meant the fast-path never
    saw the message at all."""
    assert not _gate_content(text), f"Gate 3 dropped a critical value: {text!r}"


@pytest.mark.parametrize("text", ["ok", "thanks", "👍", "yes", "done"])
def test_gate_three_still_drops_ordinary_short_messages(text):
    assert _gate_content(text), f"Gate 3 now admits chatter: {text!r}"


@pytest.mark.parametrize("raw,expected_on", [
    (None, True),          # unset
    ("", True),            # SET BUT EMPTY — the case that used to fail off
    ("   ", True),
    ("true", True), ("TRUE", True), ("1", True), ("yes", True), ("on", True),
    ("false", False), ("FALSE", False), ("0", False), ("no", False), ("off", False),
])
def test_a_safety_switch_only_turns_off_when_told_to(monkeypatch, raw, expected_on):
    """`os.getenv(name, "true").lower() == "true"` reads a variable that is SET
    BUT EMPTY as "not true".

    A `.env` line of `ORB_SAGE_AMBIENT_CRITICAL_FASTPATH=`, or a deploy script
    doing `export FOO=$UNSET_VAR`, therefore silently disabled the switch — no
    error, no log line, and no visible difference until a lethal value produced
    no card. For a default-ON safety net, an unreadable config must resolve to
    ON; only a deliberate 0/false/no/off may disable it.
    """
    from services.sage_ambient_service import _safety_flag_on

    name = "ORB_TEST_SAFETY_FLAG"
    monkeypatch.delenv(name, raising=False)
    if raw is not None:
        monkeypatch.setenv(name, raw)
    assert _safety_flag_on(name) is expected_on, \
        f"a safety switch resolved wrongly for {raw!r}"


def test_kill_switch_disables_the_whole_path(monkeypatch, clean_room):
    """Style matches ORB_SAGE_AMBIENT_RETIRE_RETRACTIONS: default ON, one env
    var, off means completely inert."""
    import services.sage_ambient_service as svc

    monkeypatch.setattr(svc, "_CRIT_FASTPATH", False)
    svc._update_memory(ROOM, PATIENT,
                       _msg(1, "POTASSIUM 6.8 mmol/L, peaked T waves."),
                       None, None)
    assert json.loads(_row()["risk_flags"] or "[]") == [], \
        "the kill switch did not stop the fast-path"
    assert svc._gate_content("K 6.8"), \
        "the kill switch did not restore Gate 3's original behaviour"


# ─────────────────────────────────────────────────────────────────────
# 8. Honest limitations, pinned so nobody assumes more than is true
# ─────────────────────────────────────────────────────────────────────
def test_retirement_matches_whole_words_only():
    """HONEST LIMITATION, and the reason the platelet card names the count twice.

    `_clinical_tokens` matches WHOLE words, so "platelet" and "platelets" are
    different subjects. A card that used only one surface form would survive a
    ward retraction that used the other. Folding plurals inside `_canon` would
    fix it globally but would also WIDEN what every retraction retires, and
    silently zeroing a live card is the failure this file guards hardest
    against — so the fix is local to the card text.
    """
    from services.sage_ambient_service import _retraction_subjects

    subjects = _retraction_subjects("That platelet count was clotted.")
    assert "platelet" in subjects and "platelets" not in subjects


def test_a_value_detached_from_its_analyte_is_not_found():
    """The extractor requires the number to sit next to the analyte name, with
    only a closed set of connectors between them. "Sats were 96 an hour ago but
    are 84 now" therefore charts nothing: the 84 is never associated with
    `sats`.

    Widening this means letting an analyte claim any number later in the
    sentence, which is exactly how "if the potassium goes above 6" and "now on
    10 units of insulin" start firing. The miss is the safer error, but it IS a
    miss — closing it properly needs a parsed observation, not a regex.
    """
    assert _deterministic_risk_extract(
        "Sats were 96 an hour ago but are 84 now.") == []


def test_a_bare_glucose_in_mg_dl_is_not_found():
    """The documented cost of the units tie-break. An Indian ward that types
    "GRBS 45" with no unit gets no card, because 45 mmol/L is not a hypo and
    firing anyway would put a false critical on every normal mg/dL sugar. A
    site-level default glucose unit would close this properly."""
    assert _deterministic_risk_extract("GRBS 45") == []
    assert _deterministic_risk_extract("GRBS 45 mg/dL"), \
        "an explicit unit must still resolve it"
