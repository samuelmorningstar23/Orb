"""The JOINS between Orb's modules — the hops a screen-by-screen check cannot see.

Orb's commercial claim is that the unit of sale is a WORKFLOW, not an agent. A
workflow is only as good as its handoffs, and every defect locked in here was
found by DRIVING a loop end to end on a running appliance (2026-08-13/14), not
by reading a module. Each one passed its own module's tests and broke at a join.

  1. NURSE OBSERVATION → ESCALATION CHAIN. The bedside score never reached the
     chain, and the safety net's watermark then ate it.
  2. FORECAST WARD BREAKDOWN. Two screens off the same server disagreed about
     how many patients are in a ward.
  3. SERVICE-ITEM CATEGORY. The route charge-capture tells a clerk to use
     answered 500 on the obvious input.
"""
import uuid

import pytest

from database import db_session
from services import billing_masters, escalation_service, forecast_service, nurse_shift_service
from services.billing_masters import MasterDataError


# ══════════════════════════════════════════════════════════════════════════════
#  1. NURSE OBSERVATION → ESCALATION CHAIN
# ══════════════════════════════════════════════════════════════════════════════
# MEASURED FAILURE (2026-08-13, live appliance). A nurse charted NEWS2 19 on a
# patient who already had an open tier-2 chain. The Relay case room said
# "NEWS2 19 (critical)". The escalation board — the surface a registrar actually
# triages from — said NEWS2 10, with the drivers of an unrelated, milder monitor
# reading, for the same patient in the same minute.
#
# Two causes, compounding:
#   * NO vitals writer called `escalation_service.evaluate_observation`. The only
#     caller was `scan_new_observations`, the safety net, which runs when someone
#     OPENS the board. So the chain was pull-based: nobody looks, nothing
#     escalates.
#   * When the sweep did run, its "already open, already at least this loud"
#     branch advanced `last_vitals_id` PAST the NEWS2 19 row without recording it
#     — tier 2 is the highest ENTRY tier the policy has, so no score, however
#     high, can out-rank an open tier-2 chain. The observation was consumed and
#     could never be reconsidered.
#
# The fix wires the write path, as escalation_service's own docstrings prescribe.

def _ward_department() -> str:
    """A department nothing else in the suite is measuring."""
    return f"Workflow Join Ward {uuid.uuid4().hex[:8]}"


def _mk_patient(department: str) -> str:
    pid = f"PATWFJ{uuid.uuid4().hex[:6].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO patients (id, name, mrn, bed, ward, age, sex, diagnosis, status) "
            "VALUES (?, 'Workflow Join Patient', ?, 'WFJ-1', 'WFJ', 71, 'F', "
            "'UTI with delirium', 'admitted')",
            (pid, f"MRN-{uuid.uuid4().hex[:8]}"),
        )
        db.execute(
            "INSERT OR IGNORE INTO users (id, name, role, password_hash) "
            "VALUES ('WFJADMITTER', 'Workflow Join Admitter', 'doctor', 'x')"
        )
        db.execute(
            "INSERT INTO admissions (patient_id, department, status, admitted_by) "
            "VALUES (?, ?, 'active', 'WFJADMITTER')",
            (pid, department),
        )
    return pid


def _mk_nurse(department: str) -> str:
    uid = f"WFJN{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO users (id, name, role, password_hash, department) "
            "VALUES (?, 'Workflow Join Nurse', 'nurse', 'x', ?)",
            (uid, department),
        )
    return uid


@pytest.fixture()
def charted_patient():
    department = _ward_department()
    pid = _mk_patient(department)
    nurse = _mk_nurse(department)
    yield pid, nurse
    with db_session() as db:
        db.execute(
            "DELETE FROM escalation_events WHERE escalation_id IN "
            "(SELECT id FROM clinical_escalations WHERE patient_id = ?)", (pid,))
        db.execute("DELETE FROM clinical_escalations WHERE patient_id = ?", (pid,))
        db.execute("DELETE FROM vitals WHERE patient_id = ?", (pid,))
        db.execute("DELETE FROM risk_scores WHERE patient_id = ?", (pid,))
        db.execute("UPDATE admissions SET status = 'discharged' WHERE patient_id = ?", (pid,))
        db.execute("UPDATE patients SET status = 'discharged' WHERE id = ?", (pid,))


#: NEWS2 well inside the emergency band (RR 30 = 3, SpO2 84 = 3, on O2 = 2,
#: SBP 78 = 3, HR 138 = 3, temp 39.6 = 2, ACVPU voice = 3).
PERI_ARREST = dict(heart_rate=138, systolic_bp=78, diastolic_bp=46,
                   respiratory_rate=30, temperature=39.6, spo2=84,
                   consciousness="voice", on_oxygen=True)
#: Qualifying, but modest — the score an earlier monitor reading might carry.
MODEST = dict(heart_rate=95, systolic_bp=120, diastolic_bp=75,
              respiratory_rate=22, temperature=38.5, spo2=93,
              consciousness="alert", on_oxygen=False)


def test_charting_a_qualifying_observation_raises_the_chain_itself(charted_patient):
    """THE WRITE PATH ESCALATES. No board read, no sweep, no second actor.

    Before the fix this assertion failed with zero chains: charting produced a
    Relay message and a vitals row and nothing that anyone owned.
    """
    pid, nurse = charted_patient

    assert escalation_service.patient_escalations(pid) == [], "precondition: no chain yet"

    result = nurse_shift_service.record_observation(pid, nurse, dict(PERI_ARREST))
    assert result["news2"]["level"] == "critical"

    chains = escalation_service.patient_escalations(pid)
    assert len(chains) == 1, "charting a peri-arrest set must open exactly one chain"
    assert chains[0]["status"] == "open"
    assert chains[0]["trigger"] == "news2"


def test_the_chain_carries_the_score_that_was_actually_charted(charted_patient):
    """THE NUMBER ON THE BOARD IS THE NUMBER AT THE BEDSIDE.

    This is the disagreement that was measured live: case room 19, board 10. The
    chain's `news2` must be the score `record_observation` computed for THIS set,
    not whatever an unrelated row left behind.
    """
    pid, nurse = charted_patient

    result = nurse_shift_service.record_observation(pid, nurse, dict(PERI_ARREST))
    charted = result["news2"]["raw_news2"]

    chain = escalation_service.patient_escalations(pid)[0]
    assert chain["news2"] == charted
    assert chain["peak_news2"] == charted
    assert chain["last_vitals_id"] == result["vitals_id"], (
        "the chain must be pinned to the observation that raised it")


def test_a_worse_set_on_an_open_chain_is_recorded_not_swallowed(charted_patient):
    """THE REGRESSION THAT MOTIVATED THE FIX.

    A modest qualifying set opens the chain at its entry tier. A peri-arrest set
    lands while that chain is open. Because tier 2 is the highest ENTRY tier the
    policy defines, the safety net's tier comparison classes the worse score as
    'already covered' and only moves the watermark. If the write path does not
    record it, the deterioration is invisible AND unreachable — the watermark has
    already passed it.

    `peak_news2` is the assertion that matters: it is what a reviewer reads to
    learn how sick this patient actually got.
    """
    pid, nurse = charted_patient

    first = nurse_shift_service.record_observation(pid, nurse, dict(MODEST))
    opened = escalation_service.patient_escalations(pid)
    assert len(opened) == 1
    assert opened[0]["peak_news2"] == first["news2"]["raw_news2"]

    worse = nurse_shift_service.record_observation(pid, nurse, dict(PERI_ARREST))
    assert worse["news2"]["raw_news2"] > first["news2"]["raw_news2"], "fixture sanity"

    chains = escalation_service.patient_escalations(pid)
    assert len(chains) == 1, "a deterioration extends the chain, it does not fork one"
    chain = chains[0]
    assert chain["peak_news2"] == worse["news2"]["raw_news2"], (
        "the worse score was swallowed: the board still reports the milder one")
    assert chain["news2"] == worse["news2"]["raw_news2"]

    # And it is on the chain's history, so the escalation can be reconstructed.
    detail = escalation_service.get_escalation(chain["id"], include_events=True)
    scores = [e.get("news2") for e in detail["events"]]
    assert worse["news2"]["raw_news2"] in scores


def test_a_stable_set_still_escalates_nothing(charted_patient):
    """The interlock must not fire on everything — that is the alert-fatigue
    hazard `dispatch_observation_alert` is written around, and the escalation
    call inherits it."""
    pid, nurse = charted_patient

    result = nurse_shift_service.record_observation(
        pid, nurse,
        dict(heart_rate=72, systolic_bp=120, diastolic_bp=78, respiratory_rate=16,
             temperature=36.8, spo2=98, consciousness="alert", on_oxygen=False),
    )
    assert result["news2"]["level"] == "stable"
    assert escalation_service.patient_escalations(pid) == []


# ══════════════════════════════════════════════════════════════════════════════
#  2. FORECAST WARD BREAKDOWN
# ══════════════════════════════════════════════════════════════════════════════
# MEASURED FAILURE (2026-08-13). /api/forecast/hospital reported GM-1, NW-1,
# OW-1 and SW-1 as holding ZERO patients for all seven forecast days, while
# /api/forecast/capacity — off the same census, on the same server, one request
# later — correctly reported 5 in each.
#
# `predict_hospital_flow` inlined the PRE-FIX ward heuristic (anything whose
# label contains "1" becomes "W1"). `_normalize_ward` had already been corrected
# to pass a known ward through unchanged; this copy was missed. The breakdown
# loop reads `ward_counts` with the REAL WARD_CAPACITY keys, so four of six wards
# matched nothing and printed 0 — silently, because the ratios were never
# required to sum to 1.

def test_ward_breakdown_names_the_real_wards(monkeypatch):
    """Every ward in the capacity map must be reachable from a patient's ward
    label. A breakdown key that no admitted patient can ever land in is a
    permanent zero on a bed manager's screen."""
    monkeypatch.setitem(forecast_service.WARD_CAPACITY, "GM-1", 15)
    monkeypatch.setitem(forecast_service.WARD_CAPACITY, "CCU", 15)

    assert forecast_service._normalize_ward("GM-1") == "GM-1"
    assert forecast_service._normalize_ward("CCU") == "CCU"
    # The digit heuristic survives ONLY as a fallback for an unknown ward.
    assert forecast_service._normalize_ward("GM-1") != "W1"


@pytest.fixture()
def patient_in_a_digit_named_ward():
    """A patient in a ward whose real label contains a digit — "GM-1", exactly
    the naming this hospital uses.

    That digit is the trigger: the pre-fix heuristic rewrote any such label to
    the fictional key "W1", so the ward the patient is REALLY in matched nothing
    in the capacity map. A ward named "ICU" or "W1" would not expose the bug,
    which is why the suite's usual fixture ward cannot be reused here.
    """
    ward = "GM-1"
    pid = f"PATWFJ{uuid.uuid4().hex[:6].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO patients (id, name, mrn, ward, bed, diagnosis, status) "
            "VALUES (?, 'Ward Keying Fixture', ?, ?, 'GM-1-9', 'Pneumonia', 'admitted')",
            (pid, f"MRN-{uuid.uuid4().hex[:8]}", ward),
        )
    forecast_service.WARD_CAPACITY.setdefault(ward, forecast_service.DEFAULT_WARD_BEDS)
    forecast_service._hospital_flow_cache.clear()
    yield pid, ward
    with db_session() as db:
        db.execute("DELETE FROM forecast_predictions WHERE patient_id = ?", (pid,))
        db.execute("DELETE FROM patients WHERE id = ?", (pid,))
    forecast_service._hospital_flow_cache.clear()


def test_the_two_capacity_surfaces_agree_on_where_patients_are(
    patient_in_a_digit_named_ward,
):
    """THE JOIN. `/forecast/hospital`'s day-0 ward breakdown and
    `/forecast/capacity`'s `current` occupancy are two renderings of ONE census.
    They must not disagree.

    Day 0 is the honest comparison: it is MEASURED on both sides, so any
    difference is a keying bug and not a modelling choice. Before the fix this
    failed exactly as it did on the live appliance — capacity said the ward held
    patients, the forecast breakdown said it was empty.
    """
    _pid, ward = patient_in_a_digit_named_ward

    forecast_service._hospital_flow_cache.clear()
    flow = forecast_service.predict_hospital_flow(days_ahead=1, include_current_day=True)
    capacity = forecast_service.get_bed_capacity_forecast()

    day0 = flow["daily_forecasts"][0]
    assert day0.get("is_current") is True

    breakdown = day0["ward_breakdown"]
    occupied = {w: c["occupied"] for w, c in capacity["current"].items()}
    assert set(breakdown) == set(occupied), "the two surfaces name different wards"

    assert occupied[ward] >= 1, "fixture sanity: capacity must see the patient"
    assert breakdown[ward] >= 1, (
        f"{ward} holds {occupied[ward]} patient(s) on the capacity screen but the "
        f"forecast breakdown shows {breakdown[ward]} — the breakdown is keyed on "
        f"labels no patient can match")

    # And no census is lost on the way: every ward capacity sees, the breakdown
    # sees too.
    for w, count in occupied.items():
        if count:
            assert breakdown[w] > 0, f"{w} vanished from the forecast breakdown"


# ══════════════════════════════════════════════════════════════════════════════
#  3. SERVICE-ITEM CATEGORY
# ══════════════════════════════════════════════════════════════════════════════
# MEASURED FAILURE (2026-08-13). `capture_clinical_charges` skipped a completed
# lab order with reason `no_service_item` and the hint "create a service item
# with code '58410-2' to price this". Following that hint with the natural
# category for a pathology test — "lab" — returned HTTP 500 Internal Server
# Error: the value hit the table's CHECK constraint (whose word is
# "investigation") and the raw IntegrityError escaped `_handle`, which only
# translates BillingError / MasterDataError / ValueError.
#
# A 500 on the remediation route means the encounter→charge→invoice→claim chain
# dead-ends at a step the system itself told the clerk to take.

def test_an_unknown_category_is_a_stated_refusal_not_a_crash():
    """The clerk must be told which field was wrong and what is allowed."""
    with pytest.raises(MasterDataError) as excinfo:
        billing_masters.upsert_service_item(
            code=f"WFJ-{uuid.uuid4().hex[:8]}",
            name="CBC panel",
            category="lab",              # the natural word; the schema's is 'investigation'
            default_rate_paise=35000,
        )
    message = str(excinfo.value)
    assert "lab" in message
    assert "investigation" in message, "the refusal must name the accepted vocabulary"


def test_the_declared_vocabulary_matches_the_table_constraint():
    """`SERVICE_CATEGORIES` is a mirror of the CHECK constraint. If it drifts,
    a value this module accepts becomes a 500 again — the exact failure. Proven
    against the live schema rather than a copy of it: every declared category is
    inserted for real."""
    made: list[str] = []
    try:
        for category in sorted(billing_masters.SERVICE_CATEGORIES):
            code = f"WFJ-CAT-{uuid.uuid4().hex[:8]}"
            made.append(code)
            item = billing_masters.upsert_service_item(
                code=code, name=f"Vocabulary probe ({category})", category=category
            )
            assert item["category"] == category
    finally:
        if made:
            with db_session() as db:
                db.execute(
                    f"DELETE FROM service_items WHERE code IN ({','.join('?' * len(made))})",
                    tuple(made),
                )


def test_a_valid_category_still_creates_a_priceable_item():
    """The fix must not narrow what worked: an 'investigation' item is created
    and carries a CONFIGURED price, which is what lets charge capture post it
    rather than skipping it as `rate_not_configured`."""
    code = f"WFJ-{uuid.uuid4().hex[:8]}"
    try:
        item = billing_masters.upsert_service_item(
            code=code, name="CBC panel", category="investigation",
            default_rate_paise=35000,
        )
        assert item["category"] == "investigation"
        assert item["rate_is_configured"] in (1, True)
        assert item["default_rate_paise"] == 35000
    finally:
        with db_session() as db:
            db.execute("DELETE FROM service_items WHERE code = ?", (code,))
