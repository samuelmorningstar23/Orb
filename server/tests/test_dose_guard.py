"""eMAR dose validation + the duplicate-dose guard.

Before this, nothing sat between a nurse's keystroke and the legal record of
what went into the patient (services/emar_service.py:74). A decimal slip on
digoxin, insulin or an opioid charted clean, and a repeat on a rounds handoff
charted clean.

The tests are organised the way the guard has to be argued, because the failure
mode of a dose checker is not "it missed one" — it is "the ward switched it
off":

  1. BLOCK the order-of-magnitude error. That is the one that kills and the one
     whose threshold is uncontroversial.
  2. DO NOT block legitimate care. A high-but-real dose, an unknown drug, an
     unparseable dose and a per-kg dose all proceed. Every one of these is a way
     a hard block stops a ward, and a guard that stops a ward gets removed —
     taking the 10x block with it.
  3. DUPLICATE doses warn, on a window derived from the ORDER's frequency; an
     idempotent retry of a lost response is not a second dose.
  4. OVERRIDE is always possible, typed, recorded and audited.
  5. A check that could not RUN says so — it neither passes silently nor blocks
     silently.
"""
import json
import uuid
from datetime import datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from auth import create_token
from database import db_session, verify_audit_chain
from routes.emar_routes import router as emar_router
from services import drug_safety_service as dss
from services import emar_service
from services.clinical_records_service import add_allergy
from services.drug_safety_service import DoseGuardBlocked


# ── fixtures ──────────────────────────────────────────────────────────────────
@pytest.fixture()
def patient():
    """TEST-ISOLATION LAW: patients.status DEFAULTS to 'admitted', so every test
    in this module used to leave one more admitted patient in the shared session
    DB — ~67 by the end of the module. That is not a cosmetic leak. The FHIR
    self-export in services/downtime_service.py iterates
    `patients WHERE status='admitted'` and sleeps 250 ms BETWEEN PATIENTS, so
    each leftover adds a quarter-second to every export call in
    tests/test_downtime_fhir_export.py and to everything downstream of it.
    Discharge on teardown; do not DELETE, because audit/medication rows written
    during the test legitimately reference this id."""
    pid = f"PATDOSE{uuid.uuid4().hex[:6].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO patients (id, name, mrn) VALUES (?, ?, ?)",
            (pid, "Dose Guard Test", f"MRN-{uuid.uuid4().hex[:6]}"),
        )
    yield pid
    with db_session() as db:
        db.execute("UPDATE patients SET status = 'discharged' WHERE id = ?", (pid,))


@pytest.fixture()
def nurse():
    uid = f"NURDG{uuid.uuid4().hex[:6].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT OR IGNORE INTO users (id, name, role, password_hash) "
            "VALUES (?, ?, 'nurse', 'x')",
            (uid, "Nurse Dose"),
        )
    return uid


@pytest.fixture()
def prescriber():
    """For the ONE case in this file that also has to clear an ALLERGY block —
    that half is prescriber-only (permissions.MEDICATION_PRESCRIBE). The dose
    guard has no such restriction and everything else here stays the nurse's."""
    uid = f"DRDG{uuid.uuid4().hex[:6].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT OR IGNORE INTO users (id, name, role, password_hash) "
            "VALUES (?, ?, 'doctor', 'x')",
            (uid, "Dr Dose"),
        )
    return uid


@pytest.fixture(scope="module")
def client():
    app = FastAPI()
    app.include_router(emar_router)
    return TestClient(app)


def _auth(uid, role="nurse"):
    return {"Authorization": f"Bearer {create_token(uid, role)}"}


def _chart_med(patient_id, drug, dosage="500mg", route="Oral", frequency="QDS",
               status="active"):
    with db_session() as db:
        cur = db.execute(
            """INSERT INTO medications (patient_id, drug_name, dosage, route, frequency,
                                        status, prescribed_by, source)
               VALUES (?, ?, ?, ?, ?, ?, 'D001', 'clinician')""",
            (patient_id, drug, dosage, route, frequency, status),
        )
        return cur.lastrowid


def _backdate_last_dose(med_id, hours):
    """Move the most recent administration of a med back in time."""
    when = (datetime.utcnow() - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
    with db_session() as db:
        row = db.execute(
            "SELECT id FROM medication_administrations WHERE medication_id = ? "
            "ORDER BY id DESC LIMIT 1", (med_id,)
        ).fetchone()
        db.execute(
            "UPDATE medication_administrations SET administered_at = ? WHERE id = ?",
            (when, row["id"]),
        )


def _audit_rows(action_type, patient_id):
    with db_session() as db:
        return [dict(r) for r in db.execute(
            "SELECT * FROM audit_logs WHERE action_type = ? AND patient_id = ? ORDER BY id",
            (action_type, patient_id),
        ).fetchall()]


REASON = "Cardiology reviewed at bedside, loading dose intended — Dr Nair."


# ═════════════════════════════════════════════════════════════════════════════
# 1. THE ORDER-OF-MAGNITUDE ERROR IS BLOCKED
# ═════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("drug,order_dose,slip,route", [
    # digoxin 1.25 mg instead of 0.125 mg — the textbook decimal slip.
    ("Digoxin", "125mcg", "1.25mg", "Oral"),
    # insulin 100 units instead of 10 ("10U" read as "100").
    ("Insulin Glargine", "10 units", "100 units", "SC"),
    # paracetamol 5 g instead of 500 mg.
    ("Paracetamol", "500mg", "5g", "Oral"),
    # morphine 100 mg IV instead of 10 mg.
    ("Morphine", "10mg", "100mg", "IV"),
    # fentanyl 1000 mcg instead of 100 mcg.
    ("Fentanyl", "100mcg", "1000mcg", "IV"),
    # heparin 50,000 units instead of 5,000.
    ("Heparin", "5000 units", "50000 units", "IV"),
    # enoxaparin 400 mg instead of 40 mg.
    ("Enoxaparin", "40mg", "400mg", "SC"),
    # warfarin 50 mg instead of 5 mg.
    ("Warfarin", "5mg", "50mg", "Oral"),
    # adrenaline 5 mg IM instead of 0.5 mg.
    ("Adrenaline", "0.5mg", "5mg", "IM"),
    # midazolam 50 mg instead of 5 mg.
    ("Midazolam", "5mg", "50mg", "IV"),
])
def test_ten_x_slip_is_blocked(patient, nurse, drug, order_dose, slip, route):
    med_id = _chart_med(patient, drug, dosage=order_dose, route=route, frequency="OD")
    with pytest.raises(DoseGuardBlocked) as ei:
        emar_service.record_administration(
            med_id, nurse, status="given", dose_given=slip, route_given=route)
    assert ei.value.check["decision"] == dss.DOSE_BLOCK

    # Nothing was charted. A blocked dose must leave no record of a dose given.
    with db_session() as db:
        assert db.execute(
            "SELECT COUNT(*) AS c FROM medication_administrations WHERE medication_id = ?",
            (med_id,),
        ).fetchone()["c"] == 0


def test_concentrated_kcl_iv_push_is_blocked_regardless_of_amount(patient, nurse):
    """Concentrated potassium IV push is never correct — the route IS the error.
    This is a fatal classic, so it is a route rule, not a dose rule."""
    med_id = _chart_med(patient, "Potassium Chloride", dosage="20 mmol",
                        route="IV infusion", frequency="OD")
    with pytest.raises(DoseGuardBlocked) as ei:
        emar_service.record_administration(
            med_id, nurse, status="given", dose_given="20 mmol", route_given="IV push")
    kinds = {f["kind"] for f in ei.value.check["findings"]}
    assert "route_forbidden" in kinds


def test_an_override_names_every_hazard_it_cleared(patient, nurse):
    """A dose can trip a magnitude rule AND a never-correct route rule at once.
    The ledger row has to name BOTH, or 'show me every potassium-push override'
    silently misses the row, and a reason typed for the amount could later be
    carried onto a dose that is also being pushed."""
    med_id = _chart_med(patient, "Potassium Chloride", dosage="200 mmol",
                        route="IV push", frequency="OD")
    emar_service.record_administration(
        med_id, nurse, status="given",
        dose_override_reason="ICU consultant at bedside, central line, cardiac monitored.")
    with db_session() as db:
        row = db.execute(
            "SELECT rule FROM medication_dose_overrides WHERE medication_id = ?",
            (med_id,)).fetchone()
    assert "route_forbidden:kcl_iv_push" in row["rule"]
    assert "dose_magnitude:potassium_chloride" in row["rule"]


def test_vincristine_intrathecal_is_blocked(patient, nurse):
    """Vincristine given intrathecally is universally fatal and universally
    agreed to be never-correct."""
    med_id = _chart_med(patient, "Vincristine", dosage="2mg", route="IV", frequency="Weekly")
    with pytest.raises(DoseGuardBlocked) as ei:
        emar_service.record_administration(
            med_id, nurse, status="given", dose_given="2mg", route_given="Intrathecal")
    assert ei.value.check["decision"] == dss.DOSE_BLOCK


# ═════════════════════════════════════════════════════════════════════════════
# 2. LEGITIMATE CARE IS NOT BLOCKED
# ═════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("drug,dose,route", [
    ("Digoxin", "500mcg", "Oral"),        # rapid digitalisation loading dose
    ("Insulin Glargine", "48 units", "SC"),  # insulin-resistant T2DM, real
    ("Morphine", "60mg", "Oral"),         # palliative oral MR, real
    ("Oxycodone", "80mg", "Oral"),        # opioid-tolerant, real
    ("Enoxaparin", "120mg", "SC"),        # 1.5 mg/kg treatment dose in an 80 kg adult
    ("Warfarin", "10mg", "Oral"),         # loading dose
    ("Paracetamol", "1g", "Oral"),        # standard adult dose
    ("Adrenaline", "1mg", "IV"),          # cardiac arrest dose
    ("Adrenaline", "5mg", "Nebulised"),   # nebulised adrenaline for stridor
    ("Heparin", "5000 units", "SC"),      # prophylaxis
    ("Midazolam", "10mg", "Buccal"),      # status epilepticus
])
def test_high_but_real_doses_are_not_blocked(patient, nurse, drug, dose, route):
    med_id = _chart_med(patient, drug, dosage=dose, route=route, frequency="OD")
    rec = emar_service.record_administration(
        med_id, nurse, status="given", dose_given=dose, route_given=route)
    assert rec["dose_check"]["decision"] != dss.DOSE_BLOCK, rec["dose_check"]["findings"]


def test_plain_iv_potassium_infusion_is_not_blocked(patient, nurse):
    """The other half of the potassium rule, and the one that decides whether
    the ward keeps it: a route charted as plain 'IV' cannot be distinguished
    from a correct diluted infusion. Blocking every correct potassium infusion
    is how the ward learns to click through the alert that catches the push."""
    med_id = _chart_med(patient, "Potassium Chloride", dosage="20 mmol",
                        route="IV", frequency="OD")
    rec = emar_service.record_administration(med_id, nurse, status="given")
    assert rec["dose_check"]["decision"] == dss.DOSE_ALLOW


def test_noradrenaline_is_not_judged_against_the_adrenaline_ceiling(patient, nurse):
    """'noradrenaline' contains 'adrenaline'. Without the exclusion this rule
    would fire on every correct vasopressor in the ICU — the exact over-firing
    that gets a guard disabled."""
    med_id = _chart_med(patient, "Noradrenaline", dosage="4mg", route="IV",
                        frequency="Continuous")
    rec = emar_service.record_administration(med_id, nurse, status="given")
    assert rec["dose_check"]["decision"] == dss.DOSE_ALLOW
    assert rec["dose_check"]["rule"] is None


def test_basal_and_bolus_insulin_are_not_a_duplicate(patient, nurse):
    """Long-acting and rapid-acting insulin are correctly given together. If the
    duplicate guard cannot tell them apart it fires on every diabetic on the
    ward, twice a day."""
    basal = _chart_med(patient, "Insulin Glargine", dosage="20 units", route="SC",
                       frequency="OD")
    bolus = _chart_med(patient, "Insulin Aspart", dosage="8 units", route="SC",
                       frequency="TDS")
    emar_service.record_administration(basal, nurse, status="given")
    second = emar_service.record_administration(bolus, nurse, status="given")
    assert second["dose_check"]["duplicate"] is None


def test_a_fentanyl_patch_is_declined_not_judged(patient, nurse):
    """A patch strength is a RATE (mcg/hour). Judging it against a bolus ceiling
    would fire on every correct patch."""
    med_id = _chart_med(patient, "Fentanyl", dosage="75 mcg", route="Transdermal",
                        frequency="Every 3 days")
    rec = emar_service.record_administration(med_id, nurse, status="given")
    assert rec["dose_check"]["decision"] == dss.DOSE_ALLOW
    assert rec["dose_check"]["rule"] is None


def test_a_wrong_ORDER_is_caught_not_just_a_wrong_keystroke(patient, nurse):
    """dose_given defaults from the order, so an order written at a lethal dose
    reaches the bedside unchanged. The guard has to stop that too, not only a
    dose the nurse typed."""
    med_id = _chart_med(patient, "Digoxin", dosage="1.25mg", route="Oral", frequency="OD")
    with pytest.raises(DoseGuardBlocked):
        emar_service.record_administration(med_id, nurse, status="given")


def test_a_warning_needs_no_override(patient, nurse):
    """A WARN must never demand a typed reason — that is a block wearing a
    different name, and it is what makes staff stop reading."""
    med_id = _chart_med(patient, "Digoxin", dosage="500mcg", route="Oral", frequency="OD")
    rec = emar_service.record_administration(med_id, nurse, status="given")
    assert rec["dose_check"]["decision"] == dss.DOSE_WARN
    assert rec["dose_check"]["override"] is None
    with db_session() as db:
        assert db.execute(
            "SELECT COUNT(*) AS c FROM medication_dose_overrides WHERE medication_id = ?",
            (med_id,),
        ).fetchone()["c"] == 0


def test_unknown_drug_passes_silently(patient, nurse):
    """Only a fraction of the formulary will ever be curated. Blocking what is
    not in the table stops the ward dead."""
    med_id = _chart_med(patient, "Sitagliptin", dosage="100mg", route="Oral", frequency="OD")
    rec = emar_service.record_administration(med_id, nurse, status="given")
    assert rec["dose_check"]["decision"] == dss.DOSE_ALLOW
    assert rec["dose_check"]["rule"] is None
    assert rec["dose_check"]["findings"] == []


@pytest.mark.parametrize("dose", [
    "1-2 tablets",      # a range is not a dose
    "0.5 mg/kg",        # weight-based — we do not hold a reliable weight
    "10 units/hr",      # a rate, not a dose
    "as directed",      # no number at all
    "2 tablets (1g)",   # two quantities that we cannot reconcile
    "1:1000 0.5mL",     # a ratio product
    "5 puffs",          # a unit with no mass
])
def test_unparseable_dose_never_fires(patient, nurse, dose):
    """An unparseable dose is not evidence of an error."""
    med_id = _chart_med(patient, "Digoxin", dosage=dose, route="Oral", frequency="OD")
    rec = emar_service.record_administration(med_id, nurse, status="given")
    assert rec["dose_check"]["decision"] == dss.DOSE_ALLOW
    assert rec["dose_check"]["parsed_dose"] is None


def test_wrong_unit_family_does_not_fire(patient, nurse):
    """A digoxin rule is written in mg. A dose charted in mL is a different
    statement and the rule must not pretend to understand it."""
    med_id = _chart_med(patient, "Digoxin", dosage="2 mL", route="Oral", frequency="OD")
    rec = emar_service.record_administration(med_id, nurse, status="given")
    assert rec["dose_check"]["decision"] == dss.DOSE_ALLOW


def test_not_given_is_never_dose_checked(patient, nurse):
    """Withholding a dose must always be easy — same principle as the allergy
    interlock."""
    med_id = _chart_med(patient, "Digoxin", dosage="1.25mg", route="Oral", frequency="OD")
    rec = emar_service.record_administration(
        med_id, nurse, status="not_given", not_given_reason="patient refused")
    assert rec["dose_check"] is None


# ═════════════════════════════════════════════════════════════════════════════
# 3. DUPLICATE-DOSE GUARD
# ═════════════════════════════════════════════════════════════════════════════
def test_duplicate_within_window_warns(patient, nurse):
    """Paracetamol QDS given twice inside an hour: the rounds-handoff repeat."""
    med_id = _chart_med(patient, "Paracetamol", dosage="1g", route="Oral", frequency="QDS")
    emar_service.record_administration(med_id, nurse, status="given")
    second = emar_service.record_administration(med_id, nurse, status="given")

    assert second["dose_check"]["decision"] == dss.DOSE_WARN
    dup = second["dose_check"]["duplicate"]
    assert dup is not None
    assert dup["window_hours"] == 3.0          # QDS → 6h interval → half-interval
    assert dup["window_basis"] == "frequency"
    assert dup["minutes_since"] < 60


def test_dose_after_the_window_does_not_warn(patient, nurse):
    """The window comes from the drug's frequency, not a flat constant. A QDS
    drug given 7 hours later is simply the next dose."""
    med_id = _chart_med(patient, "Paracetamol", dosage="1g", route="Oral", frequency="QDS")
    emar_service.record_administration(med_id, nurse, status="given")
    _backdate_last_dose(med_id, hours=7)
    second = emar_service.record_administration(med_id, nurse, status="given")
    assert second["dose_check"]["duplicate"] is None
    assert second["dose_check"]["decision"] == dss.DOSE_ALLOW


def test_window_scales_with_frequency_not_a_constant(patient, nurse):
    """Enoxaparin OD 7 hours after the last dose IS implausible; paracetamol QDS
    at the same 7 hours is not. A flat constant cannot express both."""
    med_id = _chart_med(patient, "Enoxaparin", dosage="40mg", route="SC", frequency="OD")
    emar_service.record_administration(med_id, nurse, status="given")
    _backdate_last_dose(med_id, hours=7)
    second = emar_service.record_administration(med_id, nurse, status="given")
    assert second["dose_check"]["duplicate"] is not None
    assert second["dose_check"]["duplicate"]["window_hours"] == 18.0   # OD → 24h × 0.75


def test_duplicate_spans_two_separate_orders_for_the_same_drug(patient, nurse):
    """The dangerous duplicate is usually two ORDERS (branded + generic), not two
    presses on one order."""
    a = _chart_med(patient, "Paracetamol", dosage="1g", route="Oral", frequency="QDS")
    b = _chart_med(patient, "Paracetamol 650", dosage="650mg", route="Oral", frequency="QDS")
    emar_service.record_administration(a, nurse, status="given")
    second = emar_service.record_administration(b, nurse, status="given")
    assert second["dose_check"]["duplicate"] is not None


def test_unrelated_drug_is_not_a_duplicate(patient, nurse):
    med_a = _chart_med(patient, "Paracetamol", dosage="1g", route="Oral", frequency="QDS")
    med_b = _chart_med(patient, "Amlodipine", dosage="5mg", route="Oral", frequency="OD")
    emar_service.record_administration(med_a, nurse, status="given")
    second = emar_service.record_administration(med_b, nurse, status="given")
    assert second["dose_check"]["duplicate"] is None


def test_unknown_frequency_uses_a_declared_conservative_default(patient, nurse):
    med_id = _chart_med(patient, "Paracetamol", dosage="1g", route="Oral", frequency="PRN")
    emar_service.record_administration(med_id, nurse, status="given")
    second = emar_service.record_administration(med_id, nurse, status="given")
    dup = second["dose_check"]["duplicate"]
    assert dup["window_basis"] == "unknown_frequency_default"
    assert dup["window_hours"] == dss.UNKNOWN_FREQUENCY_WINDOW_HOURS


def test_weekly_methotrexate_given_a_day_later_warns(patient, nurse):
    """The classic fatal methotrexate error is a WEEKLY drug given daily. The
    order's own frequency cannot be trusted here — being charted daily IS the
    error — so oral methotrexate carries a drug-level floor."""
    med_id = _chart_med(patient, "Methotrexate", dosage="15mg", route="Oral", frequency="OD")
    emar_service.record_administration(med_id, nurse, status="given")
    _backdate_last_dose(med_id, hours=24)
    second = emar_service.record_administration(med_id, nurse, status="given")
    dup = second["dose_check"]["duplicate"]
    assert dup is not None
    assert dup["window_basis"] == "drug_floor"


def test_a_long_interval_duplicate_survives_a_busy_patient(patient, nurse):
    """REGRESSION. The history read was capped at 400 rows with no time bound,
    so on a busy patient the week-old methotrexate dose fell off the end of the
    page and the repeat charted clean — the exact case the drug floor exists
    for. A row cap can always be exhausted by unrelated drugs; the window has to
    be bounded by TIME."""
    mtx = _chart_med(patient, "Methotrexate", dosage="15mg", route="Oral", frequency="Weekly")
    other = _chart_med(patient, "Pantoprazole", dosage="40mg", route="IV", frequency="BD")
    now = datetime.utcnow()
    with db_session() as db:
        db.execute(
            "INSERT INTO medication_administrations (medication_id, patient_id, "
            "administered_by, status, dose_given, route_given, administered_at) "
            "VALUES (?, ?, ?, 'given', '15mg', 'Oral', ?)",
            (mtx, patient, nurse,
             (now - timedelta(hours=48)).strftime("%Y-%m-%d %H:%M:%S")),
        )
        for i in range(450):     # a genuinely busy patient, all unrelated
            db.execute(
                "INSERT INTO medication_administrations (medication_id, patient_id, "
                "administered_by, status, dose_given, route_given, administered_at) "
                "VALUES (?, ?, ?, 'given', '40mg', 'IV', ?)",
                (other, patient, nurse,
                 (now - timedelta(hours=47 - i * 0.1)).strftime("%Y-%m-%d %H:%M:%S")),
            )

    rec = emar_service.record_administration(mtx, nurse, status="given")
    dup = rec["dose_check"]["duplicate"]
    assert dup is not None
    assert dup["previous_medication_id"] == mtx    # the MTX dose, not a PPI dose
    # 'Weekly' gives a 126h window; oral methotrexate's 144h floor is larger and
    # wins, which is the point of the floor.
    assert dup["window_basis"] == "drug_floor"
    assert dup["window_hours"] == 144.0


def test_not_given_rows_are_not_duplicates(patient, nurse):
    """A withheld dose is not a dose."""
    med_id = _chart_med(patient, "Paracetamol", dosage="1g", route="Oral", frequency="QDS")
    emar_service.record_administration(med_id, nurse, status="not_given",
                                       not_given_reason="NPO for theatre")
    rec = emar_service.record_administration(med_id, nurse, status="given")
    assert rec["dose_check"]["duplicate"] is None


def test_idempotent_retry_is_not_a_duplicate(patient, nurse):
    """A lost response re-sent is not a second dose. The replay short-circuits
    above the guard, so it can never be counted."""
    med_id = _chart_med(patient, "Paracetamol", dosage="1g", route="Oral", frequency="QDS")
    key = f"idem-{uuid.uuid4().hex}"
    first = emar_service.record_administration(med_id, nurse, status="given",
                                               idempotency_key=key)
    replay = emar_service.record_administration(med_id, nurse, status="given",
                                                idempotency_key=key)
    assert replay["replayed"] is True
    assert replay["id"] == first["id"]
    # Same row returned, same screen — no second dose, and no duplicate warning
    # invented for one.
    assert replay["dose_check"]["duplicate"] is None
    with db_session() as db:
        assert db.execute(
            "SELECT COUNT(*) AS c FROM medication_administrations WHERE medication_id = ?",
            (med_id,),
        ).fetchone()["c"] == 1


def test_idempotent_retry_through_the_route_is_not_a_duplicate(patient, nurse, client):
    med_id = _chart_med(patient, "Paracetamol", dosage="1g", route="Oral", frequency="QDS")
    hdr = {**_auth(nurse), "Idempotency-Key": f"idem-{uuid.uuid4().hex}"}
    r1 = client.post(f"/api/emar/medications/{med_id}/administer",
                     json={"status": "given"}, headers=hdr)
    r2 = client.post(f"/api/emar/medications/{med_id}/administer",
                     json={"status": "given"}, headers=hdr)
    assert r1.status_code == 200 and r2.status_code == 200, (r1.text, r2.text)
    assert r2.json()["administration"]["dose_check"]["duplicate"] is None


def test_history_read_excludes_the_incoming_idempotency_key(patient, nurse):
    """The replay short-circuit is what protects a retry today, and it sits far
    above this read. This proves the SECOND, local guarantee: even reached
    directly, the history read will not hand its own retry back as a prior dose.
    Without it, reordering the guards would silently turn a re-sent lost
    response into a 'second dose'."""
    med_id = _chart_med(patient, "Paracetamol", dosage="1g", route="Oral", frequency="QDS")
    key = f"idem-{uuid.uuid4().hex}"
    emar_service.record_administration(med_id, nurse, status="given", idempotency_key=key)

    with db_session() as db:
        with_key = emar_service._read_recent_administrations(
            db, patient, lookback_hours=6, exclude_idempotency_key=key)
        without = emar_service._read_recent_administrations(
            db, patient, lookback_hours=6)
    assert with_key == []
    assert len(without) == 1


def test_a_duplicate_warns_but_never_blocks(patient, nurse):
    """A duplicate is a warning by design: the nurse may know something the
    chart does not (a dropped tablet, a split dose)."""
    med_id = _chart_med(patient, "Paracetamol", dosage="1g", route="Oral", frequency="QDS")
    emar_service.record_administration(med_id, nurse, status="given")
    second = emar_service.record_administration(med_id, nurse, status="given")
    assert second["dose_check"]["decision"] == dss.DOSE_WARN
    assert second["id"] is not None      # it was charted


# ═════════════════════════════════════════════════════════════════════════════
# 4. OVERRIDE
# ═════════════════════════════════════════════════════════════════════════════
def test_override_lets_the_dose_through_and_is_recorded(patient, nurse):
    med_id = _chart_med(patient, "Digoxin", dosage="125mcg", route="Oral", frequency="OD")
    with pytest.raises(DoseGuardBlocked):
        emar_service.record_administration(med_id, nurse, status="given", dose_given="1.25mg")

    rec = emar_service.record_administration(
        med_id, nurse, status="given", dose_given="1.25mg", dose_override_reason=REASON)
    assert rec["dose_check"]["decision"] == dss.DOSE_BLOCK
    assert rec["dose_check"]["override"]["reason"] == REASON
    assert rec["dose_check"]["override"]["carried"] is False

    with db_session() as db:
        rows = db.execute(
            "SELECT * FROM medication_dose_overrides WHERE medication_id = ?", (med_id,)
        ).fetchall()
    assert len(rows) == 1
    assert rows[0]["reason"] == REASON
    assert rows[0]["overridden_by"] == nurse
    assert rows[0]["administration_id"] == rec["id"]


def test_override_is_audited_on_the_tamper_evident_chain(patient, nurse):
    med_id = _chart_med(patient, "Digoxin", dosage="125mcg", route="Oral", frequency="OD")
    emar_service.record_administration(
        med_id, nurse, status="given", dose_given="1.25mg", dose_override_reason=REASON)

    audits = _audit_rows("medication_dose_override", patient)
    assert audits, "expected a medication_dose_override audit row"
    meta = json.loads(audits[-1]["metadata_json"])
    assert meta["reason"] == REASON
    assert meta["medication_id"] == med_id
    assert meta["dose_given"] == "1.25mg"
    assert audits[-1]["event_category"] == "clinical_access"   # fail-closed
    assert verify_audit_chain()["ok"]


@pytest.mark.parametrize("junk", ["ok", "y", "....", "as per doctor", "urgent"])
def test_junk_override_reasons_are_refused(patient, nurse, junk):
    med_id = _chart_med(patient, "Digoxin", dosage="125mcg", route="Oral", frequency="OD")
    with pytest.raises(DoseGuardBlocked):
        emar_service.record_administration(
            med_id, nurse, status="given", dose_given="1.25mg", dose_override_reason=junk)
    with db_session() as db:
        assert db.execute(
            "SELECT COUNT(*) AS c FROM medication_dose_overrides WHERE medication_id = ?",
            (med_id,),
        ).fetchone()["c"] == 0


def test_override_carries_forward_when_the_ORDER_itself_is_the_high_dose(patient, nurse):
    """An order genuinely written at a high dose is a standing clinical decision.
    Re-prompting every dose of it is how a guard gets clicked through."""
    med_id = _chart_med(patient, "Insulin Glargine", dosage="100 units", route="SC",
                        frequency="OD")
    reason = "Severe insulin resistance, U-500 regimen — endocrinology 04/08."
    emar_service.record_administration(med_id, nurse, status="given",
                                       dose_override_reason=reason)
    _backdate_last_dose(med_id, hours=24)
    second = emar_service.record_administration(med_id, nurse, status="given")
    assert second["dose_check"]["override"]["carried"] is True
    assert second["dose_check"]["override"]["reason"] == reason
    with db_session() as db:
        assert db.execute(
            "SELECT COUNT(*) AS c FROM medication_dose_overrides WHERE medication_id = ?",
            (med_id,),
        ).fetchone()["c"] == 1


def test_a_bedside_deviation_is_re_armed_every_time(patient, nurse):
    """A dose that does NOT match the order is a per-dose deviation — most often
    a keystroke. Carrying that forward would let tomorrow's slip through on
    yesterday's reason."""
    med_id = _chart_med(patient, "Digoxin", dosage="125mcg", route="Oral", frequency="OD")
    emar_service.record_administration(med_id, nurse, status="given",
                                       dose_given="1.25mg", dose_override_reason=REASON)
    _backdate_last_dose(med_id, hours=24)
    with pytest.raises(DoseGuardBlocked):
        emar_service.record_administration(med_id, nurse, status="given", dose_given="1.25mg")


# ═════════════════════════════════════════════════════════════════════════════
# 5. A CHECK THAT COULD NOT RUN SAYS SO
# ═════════════════════════════════════════════════════════════════════════════
def test_internal_failure_is_surfaced_not_swallowed(patient, nurse, monkeypatch):
    """sage_handoff_service treated an unreadable allergy screen as a clean one.
    For dose checking, an error means "I could not check" — it must reach the
    clinician, the record and the audit trail. It must not silently pass and it
    must not silently block a ward."""
    def _boom(db, patient_id, **kw):
        raise RuntimeError("administration history unreadable")

    med_id = _chart_med(patient, "Paracetamol", dosage="1g", route="Oral", frequency="QDS")
    monkeypatch.setattr(emar_service, "_read_recent_administrations", _boom)
    rec = emar_service.record_administration(med_id, nurse, status="given")

    assert rec["dose_check"]["unavailable"] is True
    assert rec["dose_check"]["decision"] == dss.DOSE_WARN     # surfaced, not silent
    assert "duplicate" in (rec["dose_check"]["unavailable_reason"] or "")

    meta = json.loads(_audit_rows("medication_administered", patient)[-1]["metadata_json"])
    assert meta["dose_check"]["unavailable"] is True
    assert verify_audit_chain()["ok"]


def test_a_failed_check_is_never_carried_forward(patient, nurse, monkeypatch):
    """An unreadable check is not a decision anyone made about a known risk."""
    def _boom(db, patient_id, **kw):
        raise RuntimeError("down")

    med_id = _chart_med(patient, "Paracetamol", dosage="1g", route="Oral", frequency="QDS")
    monkeypatch.setattr(emar_service, "_read_recent_administrations", _boom)
    rec = emar_service.record_administration(med_id, nurse, status="given")
    assert rec["dose_check"]["fingerprint"] is None


# ═════════════════════════════════════════════════════════════════════════════
# 6. THE HTTP CONTRACT
# ═════════════════════════════════════════════════════════════════════════════
def test_route_returns_428_then_accepts_the_reason(patient, nurse, client):
    med_id = _chart_med(patient, "Digoxin", dosage="125mcg", route="Oral", frequency="OD")
    hdr = _auth(nurse)

    resp = client.post(f"/api/emar/medications/{med_id}/administer",
                       json={"status": "given", "dose_given": "1.25mg"}, headers=hdr)
    assert resp.status_code == 428, resp.text
    detail = resp.json()["detail"]
    assert detail["error"] == "dose_acknowledgement_required"
    assert detail["dose"]["decision"] == dss.DOSE_BLOCK
    assert detail["dose"]["findings"][0]["kind"] == "dose_magnitude"

    resp = client.post(
        f"/api/emar/medications/{med_id}/administer",
        json={"status": "given", "dose_given": "1.25mg", "dose_override_reason": REASON},
        headers=hdr,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["administration"]["dose_check"]["override"]["carried"] is False


def test_both_guards_armed_are_cleared_one_at_a_time_in_severity_order(patient, prescriber, client):
    """The dose guard shares the 428 with the allergy interlock, and only one
    can be reported at a time. The allergy is the more serious finding so it
    keeps precedence, and clearing it must NOT clear the dose block with it —
    one acknowledgement must never silently cover a hazard nobody was shown.

    Driven by a PRESCRIBER because the allergy half of this sequence is
    prescriber-only since 2026-08-15 (permissions.MEDICATION_PRESCRIBE); the
    dose guard itself is not, and every other case in this file is still the
    nurse's.
    """
    add_allergy(patient, "opioids", reaction="anaphylaxis", severity="severe")
    med_id = _chart_med(patient, "Morphine", dosage="10mg", route="IV", frequency="Q4H")
    hdr = _auth(prescriber, "doctor")
    body = {"status": "given", "dose_given": "100mg", "route_given": "IV"}

    r = client.post(f"/api/emar/medications/{med_id}/administer", json=body, headers=hdr)
    assert r.status_code == 428
    assert r.json()["detail"]["error"] == "allergy_acknowledgement_required"

    body["allergy_override_reason"] = "Label is a nausea intolerance, de-labelled by pain team 05/08."
    r = client.post(f"/api/emar/medications/{med_id}/administer", json=body, headers=hdr)
    assert r.status_code == 428, "clearing the allergy must not clear the dose block"
    assert r.json()["detail"]["error"] == "dose_acknowledgement_required"

    body["dose_override_reason"] = "Opioid-tolerant, PCA transition per pain team, 1:1 observation."
    r = client.post(f"/api/emar/medications/{med_id}/administer", json=body, headers=hdr)
    assert r.status_code == 200, r.text

    with db_session() as db:
        assert db.execute(
            "SELECT COUNT(*) AS c FROM medication_allergy_overrides WHERE medication_id = ?",
            (med_id,)).fetchone()["c"] == 1
        assert db.execute(
            "SELECT COUNT(*) AS c FROM medication_dose_overrides WHERE medication_id = ?",
            (med_id,)).fetchone()["c"] == 1
    # Two nothing-charted 428s then one dose: the blocked attempts wrote no row.
    assert len(emar_service.list_administrations(patient)) == 1
    assert verify_audit_chain()["ok"]


def test_history_carries_the_dose_check(patient, nurse, client):
    med_id = _chart_med(patient, "Paracetamol", dosage="1g", route="Oral", frequency="QDS")
    emar_service.record_administration(med_id, nurse, status="given")
    emar_service.record_administration(med_id, nurse, status="given")
    resp = client.get(f"/api/emar/patients/{patient}/administrations", headers=_auth(nurse))
    assert resp.status_code == 200
    assert resp.json()["administrations"][0]["dose_check"]["decision"] == dss.DOSE_WARN


# ═════════════════════════════════════════════════════════════════════════════
# 7. THE PURE LAYER (no DB) — parsing is where a dose checker actually fails
# ═════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("text,value,unit", [
    ("500mg", 500.0, "mg"),
    ("500 mg", 500.0, "mg"),
    ("1g", 1000.0, "mg"),
    ("0.125mg", 0.125, "mg"),
    ("125mcg", 0.125, "mg"),
    ("125 micrograms", 0.125, "mg"),
    ("10 units", 10.0, "units"),
    ("10U", 10.0, "units"),
    ("5000 IU", 5000.0, "units"),
    ("10,000 units", 10000.0, "units"),   # Indian/UK thousands separator
    ("20 mmol", 20.0, "mmol"),
    ("20 mEq", 20.0, "mmol"),
    ("2 mL", 2.0, "ml"),
    # A route abbreviated inside the dose box. A blanket "any slash is a rate"
    # refusal would let a 10x insulin dose through on this alone.
    ("100 units s/c", 100.0, "units"),
    ("40mg p/o", 40.0, "mg"),
])
def test_parse_dose(text, value, unit):
    p = dss.parse_dose(text)
    assert p is not None, text
    assert p["unit"] == unit
    assert p["value"] == pytest.approx(value)


@pytest.mark.parametrize("text", [
    "", None, "as directed", "1-2 tablets", "1 to 2 tablets", "0.5 mg/kg",
    "10 units/hr", "2 tablets (1g)", "1:1000", "50%", "5 puffs", "PRN",
    "500mg/125mg",      # a combination product, not one dose
    "75 mcg/hr",        # a patch rate
    "1g in 100mL",      # a bag: two quantities, neither of them "the dose"
])
def test_parse_dose_refuses_what_it_cannot_read(text):
    assert dss.parse_dose(text) is None


@pytest.mark.parametrize("route,expected", [
    ("S/C", "sc"), ("s/c", "sc"), ("I/V", "iv"), ("I.V.", "iv"), ("P/O", "oral"),
    ("IV push", "push"), ("IV bolus stat", "push"), ("NG tube", "oral"),
    ("Intrathecal", "intrathecal"), ("Nebulised", "nebulised"),
])
def test_route_abbreviations_are_understood(route, expected):
    """A route-specific ceiling that silently never applies is worse than no
    rule: it reads like a working safety check."""
    assert expected in dss.classify_route(route)


def test_a_punctuated_route_still_reaches_its_own_ceiling(patient, nurse):
    """Regression: 'S/C' used to classify as nothing, so an insulin dose charted
    that way fell through to the weakest applicable rule."""
    med_id = _chart_med(patient, "Adrenaline", dosage="5mg", route="I/M", frequency="STAT")
    with pytest.raises(DoseGuardBlocked):
        emar_service.record_administration(med_id, nurse, status="given")


@pytest.mark.parametrize("text,hours", [
    ("QDS", 6.0), ("QID", 6.0), ("q6h", 6.0), ("6 hourly", 6.0),
    ("TDS", 8.0), ("TID", 8.0), ("BD", 12.0), ("BID", 12.0), ("twice daily", 12.0),
    ("OD", 24.0), ("Daily", 24.0), ("Nocte", 24.0), ("HS", 24.0),
    ("Q12H", 12.0), ("every 4 hours", 4.0),
    ("Weekly", 168.0), ("once weekly", 168.0),
    ("alternate days", 48.0),
])
def test_parse_frequency(text, hours):
    assert dss.parse_frequency_hours(text) == pytest.approx(hours)


@pytest.mark.parametrize("text", ["", None, "PRN", "SOS", "as required", "STAT", "??"])
def test_parse_frequency_admits_ignorance(text):
    assert dss.parse_frequency_hours(text) is None


def test_every_curated_rule_cites_a_source():
    """A threshold with no provenance is deleted by the first person it annoys."""
    for rule in [*dss.DOSE_RULES, *dss.ROUTE_RULES]:
        assert rule.get("source"), f"{rule['id']} has no cited source"
        assert rule.get("basis"), f"{rule['id']} does not say what the normal dose is"


def test_a_rule_id_never_claims_a_route_it_does_not_enforce():
    """A rule id is written into the record, the override ledger and the audit
    trail. One named '_oral' that in fact judges an IV dose misreports itself in
    all three."""
    for rule in dss.DOSE_RULES:
        for tag in ("oral", "parenteral", "nebulised", "transdermal"):
            if rule["id"].endswith(f"_{tag}"):
                assert rule.get("routes") and tag in rule["routes"], rule["id"]


def test_route_specific_rules_are_not_shadowed_by_a_catch_all():
    """Order in DOSE_RULES is load-bearing: a route-agnostic rule listed before
    its route-specific sibling would win and the specific ceiling would be dead
    code that still reads like a working check."""
    catch_alls: list = []
    for rule in dss.DOSE_RULES:
        patterns = set(rule["match"])
        if rule.get("routes") is None:
            catch_alls.append((rule["id"], patterns))
        else:
            # Overlap, not equality: 'oxycodone_parenteral' and the 'oxycodone'
            # catch-all list different brand sets but still collide on the
            # generic name, which is what actually decides the match.
            for earlier_id, earlier in catch_alls:
                assert not (patterns & earlier), f"{rule['id']} shadowed by {earlier_id}"


def test_no_rule_is_written_in_a_unit_the_parser_cannot_produce():
    """A rule in a unit family parse_dose never emits would be dead code that
    reads like a working safety check."""
    families = {canonical for canonical, _ in dss._UNIT_ALIASES.values()}
    for rule in dss.DOSE_RULES:
        if rule.get("skip"):
            continue
        assert rule["unit"] in families, rule["id"]


def test_no_rule_blocks_below_its_own_warn_threshold():
    """Internal consistency: block_at must always sit above warn_above, or the
    graded response collapses into a hard block."""
    for rule in dss.DOSE_RULES:
        if rule.get("block_at") is not None and rule.get("warn_above") is not None:
            assert rule["block_at"] > rule["warn_above"], rule["id"]
