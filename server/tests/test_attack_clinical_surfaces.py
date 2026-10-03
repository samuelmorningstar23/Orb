"""Defects found by ATTACKING the clinical surfaces end to end, not by reading them.

Every test here reproduces something that was executed against a running Orb on
2026-08-06 and came back wrong. They are grouped by the surface that failed, and
each one FAILS on the pre-fix code — that is the only reason any of them exists.

  1. ALLERGY VOCABULARY. The class interlock fires for ibuprofen. It did not fire
     for aceclofenac, nimesulide, etoricoxib, meloxicam, piroxicam or ketoprofen —
     all NSAIDs, the first two among the most-prescribed drugs in an Indian
     hospital. A patient charted "NSAIDs — anaphylaxis" screened CLEAN at order
     time AND at the bedside. Same shape as the 2026-08-02 ibuprofen gap: the
     class TERM was fixed, the class MEMBERSHIP was not.

  2. OBSERVATION DEDUPE. Re-charting the same observation TIME with corrected
     values returned 200 and echoed a NEWS2 of 19 / "critical" computed from the
     new values — while silently keeping the earlier benign row. The nurse was
     told the emergency response had been triggered; the chart said "All vitals
     within normal range".

  3. eMAR AFTER DISCHARGE. Every board correctly drops a discharged patient. The
     medication administration path did not: enoxaparin was administered, 200 OK,
     to a patient discharged minutes earlier.

  4. HANDOVER TREND. A newly admitted patient with ONE observation set (HR 132,
     SBP 88, SpO2 89) was handed over as "improving", because an empty comparison
     window made AVG() return NULL and the delta was computed against zero.

  5. ESCALATION REACH. Identical peri-arrest vitals: the monitor/ingestion path
     raised a Relay alert; the nurse's own bedside entry — the ward's documented
     redundancy layer — raised nothing at all.
"""
import uuid

import pytest

from database import db_session
from services import emar_service, nurse_shift_service, vigil_service
from services.allergy_interlock import BLOCK, evaluate
from services.drug_safety_service import check_medication_safety, classify_drug


# ── fixtures ─────────────────────────────────────────────────────────────────
@pytest.fixture()
def patient():
    """TEST-ISOLATION LAW: this fixture seeds status='admitted' explicitly, so
    every test in this module used to leave one more admitted patient in the
    shared session DB — ~20 by the end of the module. That is not a cosmetic
    leak. The FHIR self-export in services/downtime_service.py iterates
    `patients WHERE status='admitted'` and sleeps _PATIENT_SLEEP_S (250 ms)
    BETWEEN PATIENTS, so each leftover adds ~0.275 s to every export call in
    tests/test_downtime_fhir_export.py and to everything downstream of it.
    Discharge on teardown; do not DELETE, because the audit, allergy and
    medication rows written during the test legitimately reference this id."""
    pid = f"PATATK{uuid.uuid4().hex[:6].upper()}"
    with db_session() as db:
        db.execute("INSERT INTO patients (id, name, mrn, status) VALUES (?, ?, ?, 'admitted')",
                   (pid, "Attack Subject", f"MRN{pid[-5:]}"))
    yield pid
    with db_session() as db:
        db.execute("UPDATE patients SET status = 'discharged' WHERE id = ?", (pid,))


def _chart_allergy(pid, substance, reaction="anaphylaxis", severity="severe"):
    with db_session() as db:
        db.execute(
            "INSERT INTO patient_allergies (patient_id, substance, reaction, severity, status) "
            "VALUES (?, ?, ?, ?, 'active')",
            (pid, substance, reaction, severity),
        )


def _chart_med(pid, drug, dosage="1 tab", route="Oral", freq="BD", status="active"):
    with db_session() as db:
        cur = db.execute(
            "INSERT INTO medications (patient_id, drug_name, dosage, route, frequency, status, source) "
            "VALUES (?, ?, ?, ?, ?, ?, 'clinician')",
            (pid, drug, dosage, route, freq, status),
        )
        return cur.lastrowid


# ═══════════════════════════════════════════════════════════════════════════
# 1. ALLERGY VOCABULARY — class membership, not just the class term
# ═══════════════════════════════════════════════════════════════════════════

# Every one of these was administered clean, through the real API, against a
# patient charted "NSAIDs / anaphylaxis / severe".
NSAID_MEMBERS = [
    "Aceclofenac", "Nimesulide", "Etoricoxib", "Meloxicam",
    "Piroxicam", "Ketoprofen", "Etodolac", "Flurbiprofen", "Lornoxicam",
]
CEPHALOSPORIN_MEMBERS = ["Cefixime", "Cefpodoxime", "Cefadroxil", "Cefepime", "Cefoperazone"]


@pytest.mark.parametrize("drug", NSAID_MEMBERS)
def test_nsaid_class_allergy_blocks_every_nsaid(patient, drug):
    """'NSAIDs' charted as a class must stop EVERY NSAID, not just the six the
    map happened to list. Aceclofenac and nimesulide are the two an Indian ward
    reaches for first."""
    _chart_allergy(patient, "NSAIDs")
    screen = evaluate([{"id": 1, "substance": "NSAIDs", "reaction": "anaphylaxis",
                        "severity": "severe"}], drug)
    assert screen["decision"] == BLOCK, (
        f"{drug} is an NSAID and screened {screen['decision']} against an "
        f"NSAID-class anaphylaxis allergy — this is the ibuprofen gap again"
    )


@pytest.mark.parametrize("drug", CEPHALOSPORIN_MEMBERS)
def test_cephalosporin_class_allergy_blocks_every_cephalosporin(drug):
    screen = evaluate([{"id": 1, "substance": "Cephalosporins", "reaction": "anaphylaxis",
                        "severity": "severe"}], drug)
    assert screen["decision"] == BLOCK, f"{drug} screened {screen['decision']}"


@pytest.mark.parametrize("drug", CEPHALOSPORIN_MEMBERS)
def test_penicillin_cross_reactivity_reaches_every_cephalosporin(drug):
    """Penicillin↔cephalosporin cross-reactivity is only as good as the
    cephalosporin list. Cefixime is the commonest oral cephalosporin in India and
    was not in it, so the cross-reactivity alert never fired for it."""
    assert "cephalosporins" in classify_drug(drug), (
        f"{drug} does not classify as a cephalosporin, so no penicillin "
        f"cross-reactivity alert can ever fire for it"
    )


@pytest.mark.parametrize("brand,ingredient_class", [
    ("Ecosprin 75", "nsaids"),      # India's dominant aspirin brand
    ("Brufen 400", "nsaids"),       # ibuprofen
    ("Combiflam", "nsaids"),        # ibuprofen + paracetamol
    ("Voveran", "nsaids"),          # diclofenac
    ("Zerodol", "nsaids"),          # aceclofenac
])
def test_common_indian_brand_names_classify(brand, ingredient_class):
    """Brand resolution via helix_medication_master is an enrichment that is
    empty on a fresh appliance. The class map is the floor, and these brands are
    what is actually written on an Indian drug chart."""
    assert ingredient_class in classify_drug(brand), (
        f"{brand} classifies as {classify_drug(brand)} — an NSAID-allergic "
        f"patient would be screened clean for it"
    )


def test_paracetamol_is_not_an_nsaid(patient):
    """The other half of the contract. Widening the class map must not start
    firing on paracetamol — an interlock that over-fires is the documented
    alert-fatigue hazard and is worse than none."""
    _chart_allergy(patient, "NSAIDs")
    safety = check_medication_safety(patient, "Paracetamol")
    assert not safety["allergy_alerts"], (
        f"Paracetamol alerted against an NSAID allergy: {safety['allergy_alerts']}"
    )
    assert not safety["blocking"]


@pytest.mark.parametrize("drug", ["Pantoprazole", "Metformin", "Amlodipine",
                                  "Levothyroxine", "Ondansetron", "Salbutamol"])
def test_unrelated_drugs_stay_quiet_against_an_nsaid_allergy(drug):
    screen = evaluate([{"id": 1, "substance": "NSAIDs", "reaction": "anaphylaxis",
                        "severity": "severe"}], drug)
    assert screen["decision"] == "allow", (
        f"{drug} alerted against an NSAID allergy — over-firing trains staff to "
        f"click through the real alert"
    )


# ═══════════════════════════════════════════════════════════════════════════
# 2. OBSERVATION DEDUPE — a correction must not be swallowed, and the response
#    must never report a score that was not persisted
# ═══════════════════════════════════════════════════════════════════════════

_BENIGN = dict(heart_rate=72, systolic_bp=124, respiratory_rate=16, spo2=98,
               temperature=36.7, consciousness="alert", on_oxygen=False)
_PERI_ARREST = dict(heart_rate=142, systolic_bp=78, respiratory_rate=32, spo2=84,
                    temperature=39.4, consciousness="voice", on_oxygen=True)


def test_identical_replay_at_the_same_time_is_still_deduped(patient):
    """The behaviour that must NOT regress: an offline queue replaying the same
    body verbatim is one reading, not two."""
    ts = "2026-08-06T09:00:00+00:00"
    first = nurse_shift_service.record_observation(patient, "N001", dict(_BENIGN), taken_at=ts)
    again = nurse_shift_service.record_observation(patient, "N001", dict(_BENIGN), taken_at=ts)
    assert again["replayed"] is True
    assert again["vitals_id"] == first["vitals_id"]
    with db_session() as db:
        n = db.execute("SELECT COUNT(*) c FROM vitals WHERE patient_id = ?", (patient,)).fetchone()["c"]
    assert n == 1


def test_corrected_values_at_the_same_time_are_not_silently_discarded(patient):
    """THE BUG. A nurse mis-keys an obs set, then re-submits the same observation
    TIME with the real, peri-arrest values.

    Pre-fix: 200, ``replayed: True``, and a NEWS2 of 19 / 'critical' echoed back
    from values that were never written. The chart kept HR 72 and the board read
    'All vitals within normal range'.
    """
    ts = "2026-08-06T09:00:00+00:00"
    nurse_shift_service.record_observation(patient, "N001", dict(_BENIGN), taken_at=ts)
    corrected = nurse_shift_service.record_observation(
        patient, "N001", dict(_PERI_ARREST), taken_at=ts
    )

    # Whatever the service decides to do with a conflicting correction, the one
    # thing it may not do is report a score it did not persist.
    with db_session() as db:
        row = db.execute(
            "SELECT heart_rate, spo2 FROM vitals WHERE patient_id = ? "
            "ORDER BY id DESC LIMIT 1", (patient,),
        ).fetchone()
        stored_hr = row["heart_rate"]
        risk = db.execute(
            "SELECT level FROM risk_scores WHERE patient_id = ? ORDER BY id DESC LIMIT 1",
            (patient,),
        ).fetchone()

    reported_level = corrected["news2"]["level"]
    if reported_level == "critical":
        assert stored_hr == 142, (
            "record_observation reported NEWS2 'critical' to the caller but the "
            f"chart still holds heart_rate={stored_hr} — the deterioration was "
            "silently discarded while the nurse was told it had been recorded"
        )
        assert risk is not None and risk["level"] == "critical", (
            "the caller was told 'critical' but no critical risk_scores row exists"
        )
    else:
        # A refusal is an acceptable resolution; a silent swallow is not.
        assert corrected.get("conflict") or corrected.get("superseded_by"), (
            "a conflicting correction was neither applied nor flagged to the caller"
        )


# ═══════════════════════════════════════════════════════════════════════════
# 3. eMAR AFTER DISCHARGE
# ═══════════════════════════════════════════════════════════════════════════

def test_cannot_administer_a_medication_to_a_discharged_patient(patient):
    """Executed against the live API: patient discharged, then enoxaparin 40mg SC
    administered, 200 OK. The boards had already dropped him."""
    med_id = _chart_med(patient, "Enoxaparin", dosage="40mg", route="SC", freq="OD")
    with db_session() as db:
        db.execute("UPDATE patients SET status = 'discharged' WHERE id = ?", (patient,))

    with pytest.raises(ValueError, match="discharg"):
        emar_service.record_administration(med_id, "N001", status="given")


def test_withholding_a_dose_for_a_discharged_patient_is_still_allowed(patient):
    """Withholding must always be easy — including tidying up the round after a
    discharge. Only 'given' is stopped."""
    med_id = _chart_med(patient, "Enoxaparin")
    with db_session() as db:
        db.execute("UPDATE patients SET status = 'discharged' WHERE id = ?", (patient,))

    rec = emar_service.record_administration(
        med_id, "N001", status="not_given", not_given_reason="patient discharged before the round"
    )
    assert rec["status"] == "not_given"


def test_an_admitted_patient_is_unaffected(patient):
    """Regression guard on the fix: the ordinary round must not change."""
    med_id = _chart_med(patient, "Paracetamol", dosage="1g")
    rec = emar_service.record_administration(med_id, "N001", status="given")
    assert rec["status"] == "given"
    assert rec["replayed"] is False


def test_offline_replay_of_a_dose_given_before_discharge_still_returns_the_record():
    """Over-tightening guard on the discharge fix.

    The nurse gives the dose at 08:00 while the patient is an inpatient; the
    tablet is offline, so the write sits in the localStorage queue with its
    Idempotency-Key. The patient goes home at 10:00. The tablet reconnects at
    11:00 and replays THAT key.

    The dose is already charted, and a replay writes no new row — so the honest
    answer is the existing record. Sited above the replay lookup the guard
    answered 422 instead, and api/client.ts:isPermanentReplayFailure treats a
    4xx replay as permanent: it drops the queued item and tells the nurse the
    sync failed, for a dose that IS on the chart.
    """
    pid = f"PATRPL{uuid.uuid4().hex[:6].upper()}"
    with db_session() as db:
        db.execute("INSERT INTO patients (id, name, mrn, status) VALUES (?, ?, ?, 'admitted')",
                   (pid, "Replay Patient", f"MRN-{uuid.uuid4().hex[:6]}"))
    med_id = _chart_med(pid, "Paracetamol", dosage="500mg")
    key = f"offline-{uuid.uuid4().hex[:8]}"

    first = emar_service.record_administration(
        med_id, "N001", status="given", idempotency_key=key
    )
    assert first["replayed"] is False

    with db_session() as db:
        db.execute("UPDATE patients SET status = 'discharged' WHERE id = ?", (pid,))

    replay = emar_service.record_administration(
        med_id, "N001", status="given", idempotency_key=key
    )
    assert replay["replayed"] is True, "the offline replay was refused after discharge"
    assert replay["id"] == first["id"], "the replay charted a second dose"

    # ...and the guard it must not weaken: a FRESH key after discharge is still
    # refused, so nothing new can be charted against a patient who has left.
    with pytest.raises(ValueError, match="discharg"):
        emar_service.record_administration(
            med_id, "N001", status="given", idempotency_key=f"fresh-{uuid.uuid4().hex[:8]}"
        )


# ═══════════════════════════════════════════════════════════════════════════
# 4. HANDOVER TREND — no comparison window is not "improving"
# ═══════════════════════════════════════════════════════════════════════════

def test_single_observation_set_is_not_reported_as_improving(patient):
    """A patient admitted this shift has exactly one obs set, so the previous
    window is empty. AVG() over zero rows returns NULL, `None or 0` turned the
    absolute values into deltas (+132, +88, +89), and the I-PASS handover told
    the oncoming nurse a peri-arrest patient was 'improving'."""
    nurse_shift_service.record_observation(
        patient, "N001",
        dict(heart_rate=132, systolic_bp=88, respiratory_rate=28, spo2=89,
             temperature=39.2, consciousness="alert", on_oxygen=False),
    )
    trend = vigil_service.get_vitals_trend(patient, hours=6)
    assert trend["trend"] != "improving", (
        f"a single peri-arrest observation set was handed over as 'improving': {trend}"
    )
    assert trend["trend"] == "insufficient_data", (
        f"with no comparison window the honest answer is insufficient_data, got {trend}"
    )


def test_a_real_improvement_is_still_reported_as_improving(patient):
    """The fix must not flatten the trend into 'insufficient_data' forever —
    with both windows populated the comparison still has to work."""
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    with db_session() as db:
        # previous window (6-12h ago): hypoxic and hypotensive
        db.execute(
            "INSERT INTO vitals (patient_id, timestamp, heart_rate, systolic_bp, spo2, "
            "respiratory_rate) VALUES (?, ?, 120, 90, 88, 26)",
            (patient, (now - timedelta(hours=9)).isoformat()),
        )
        # recent window (<6h): recovered
        db.execute(
            "INSERT INTO vitals (patient_id, timestamp, heart_rate, systolic_bp, spo2, "
            "respiratory_rate) VALUES (?, ?, 118, 118, 97, 18)",
            (patient, (now - timedelta(hours=1)).isoformat()),
        )
    trend = vigil_service.get_vitals_trend(patient, hours=6)
    assert trend["trend"] == "improving", trend


def test_a_real_deterioration_is_still_reported_as_deteriorating(patient):
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    with db_session() as db:
        db.execute(
            "INSERT INTO vitals (patient_id, timestamp, heart_rate, systolic_bp, spo2, "
            "respiratory_rate) VALUES (?, ?, 80, 125, 98, 16)",
            (patient, (now - timedelta(hours=9)).isoformat()),
        )
        db.execute(
            "INSERT INTO vitals (patient_id, timestamp, heart_rate, systolic_bp, spo2, "
            "respiratory_rate) VALUES (?, ?, 130, 85, 88, 30)",
            (patient, (now - timedelta(hours=1)).isoformat()),
        )
    trend = vigil_service.get_vitals_trend(patient, hours=6)
    assert trend["trend"] == "deteriorating", trend


# ═══════════════════════════════════════════════════════════════════════════
# 5. ESCALATION REACH — the nurse's own entry must escalate like the monitor's
# ═══════════════════════════════════════════════════════════════════════════

@pytest.fixture()
def patient_with_case_room(patient):
    """A patient with the Relay case room every admission path creates — the
    channel an alert is actually delivered into."""
    cid = f"CHATK{uuid.uuid4().hex[:6].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO channels (id, name, type, patient_id) VALUES (?, ?, 'case_room', ?)",
            (cid, f"Case: {patient}", patient),
        )
    return patient, cid


def _alerts_in(channel_id):
    with db_session() as db:
        return db.execute(
            "SELECT COUNT(*) c FROM messages WHERE channel_id = ? AND message_type = 'alert'",
            (channel_id,),
        ).fetchone()["c"]


def test_nurse_charted_deterioration_reaches_the_case_room(patient_with_case_room):
    """Executed on a live server: identical peri-arrest vitals produced
    "⚠️ VIGIL ALERT [INGESTED_JSON] …" in the case room via /api/ingest/json and
    NOTHING via /api/nurse/.../observations. Manual entry is the ward's
    redundancy layer — it is the path that must not be silent.

    Asserted on the delivered MESSAGE, not on a call to the dispatcher: "did the
    function run" is not the same claim as "did it reach anyone".
    """
    pid, cid = patient_with_case_room
    assert _alerts_in(cid) == 0

    result = nurse_shift_service.record_observation(pid, "N001", dict(_PERI_ARREST))
    assert result["news2"]["level"] == "critical"

    assert _alerts_in(cid) == 1, (
        f"a NEWS2 {result['news2']['raw_news2']} / critical observation charted by "
        f"a nurse delivered no alert to the patient's case room"
    )
    with db_session() as db:
        content = db.execute(
            "SELECT content FROM messages WHERE channel_id = ? AND message_type = 'alert' "
            "ORDER BY id DESC LIMIT 1", (cid,),
        ).fetchone()["content"]
    assert "VIGIL ALERT" in content and "NEWS2" in content, content


def test_a_stable_observation_delivers_no_alert(patient_with_case_room):
    """The other half. An interlock that fires on everything trains staff to
    ignore it, which is how the real alert dies."""
    pid, cid = patient_with_case_room
    nurse_shift_service.record_observation(pid, "N001", dict(_BENIGN))
    assert _alerts_in(cid) == 0, "a stable observation set alerted"


def test_alert_delivery_failure_never_loses_the_reading(patient, monkeypatch):
    """Escalation is best-effort ON TOP of a committed reading. A patient with no
    case room, or a Relay write that throws, must not roll back the observation —
    losing the chart entry would be strictly worse than losing the alert."""
    def _boom(*a, **k):
        raise RuntimeError("relay down")
    monkeypatch.setattr(nurse_shift_service, "dispatch_observation_alert", _boom)

    result = nurse_shift_service.record_observation(patient, "N001", dict(_PERI_ARREST))
    assert result["news2"]["level"] == "critical"
    with db_session() as db:
        n = db.execute("SELECT COUNT(*) c FROM vitals WHERE patient_id = ?",
                       (patient,)).fetchone()["c"]
    assert n == 1, "the observation was lost when alert delivery failed"


# ═══════════════════════════════════════════════════════════════════════════
# 6. NEWS2 COVERAGE HONESTY — a partial set must not read like a full one
# ═══════════════════════════════════════════════════════════════════════════

def test_news2_arithmetic_is_exact_at_the_bands(patient):
    """The clean result worth keeping. 33 hand-computed RCP cases were driven
    through /api/nurse/.../news2-preview on a live server and every one matched,
    including the exact 5 / 6 / 7 band edges. These are the ones the escalation
    decision turns on."""
    # RR22(2) + SpO2 95(1) + HR 95(1) + T 38.5(1) = 5
    assert vigil_service.compute_news2(95, 120, 22, 38.5, 95)["raw_news2"] == 5
    # RR22(2) + SpO2 93(2) + HR 95(1) + T 38.5(1) = 6
    assert vigil_service.compute_news2(95, 120, 22, 38.5, 93)["raw_news2"] == 6
    # RR22(2) + SpO2 93(2) + HR 115(2) + T 38.5(1) = 7
    seven = vigil_service.compute_news2(115, 120, 22, 38.5, 93)
    assert seven["raw_news2"] == 7 and seven["level"] == "critical"
    # RR22(2) + SpO2 95(1) + HR 95(1) = 4 — must stay below the band
    four = vigil_service.compute_news2(95, 120, 22, 36.8, 95)
    assert four["raw_news2"] == 4 and four["level"] == "stable"


def test_a_partial_observation_set_is_labelled_as_partial():
    """Probed on the live server: a lone heart rate of 70 returned raw_news2 0,
    level 'stable', and NOTHING in the payload distinguished it from a full,
    genuinely clean set. An unmeasured parameter scores 0, so a partial set can
    only under-score — the direction that matters."""
    partial = vigil_service.compute_news2(70, None, None, None, None)
    assert partial["raw_news2"] == 0
    assert partial["complete"] is False
    # SIX missing, not four: ACVPU and supplemental O2 are NEWS2 parameters too,
    # and a caller that passed neither did not assess either. Widened 2026-08-14
    # — see test_news2.py's coverage-honesty block for why the old four-item
    # expectation was itself the bug.
    assert set(partial["missing_parameters"]) == {
        "Respiratory rate", "SpO2", "Systolic BP", "Temperature",
        "Consciousness (ACVPU)", "Supplemental O2",
    }
    assert partial["coverage_note"] and "INCOMPLETE" in partial["coverage_note"]
    assert "All vitals within normal range" not in partial["drivers"], (
        "a one-parameter observation set claimed all vitals were normal"
    )


def test_a_full_normal_set_is_marked_complete():
    # A FULL set is seven parameters. The five numeric vitals plus an actual
    # ACVPU assessment and an actual oxygen-delivery statement — which is what a
    # nurse's obs round produces and what a bedside monitor alone never does.
    full = vigil_service.compute_news2(70, 120, 16, 36.8, 98,
                                       consciousness="alert", on_oxygen=False)
    assert full["raw_news2"] == 0
    assert full["complete"] is True
    assert full["missing_parameters"] == []
    assert full["coverage_note"] is None
    assert full["drivers"] == ["All vitals within normal range"]


def test_five_numeric_vitals_without_acvpu_or_oxygen_is_not_complete():
    """The monitor-feed case, which is the pilot's NORMAL case.

    Five perfect numbers off a bedside monitor used to come back
    `complete: true` — a 5-of-7 NEWS2 presented as a full one, hiding the two
    parameters worth the most points on the RCP table (ACVPU +3, O2 +2). A
    machine cannot assess consciousness or read an oxygen mask.
    """
    monitor_only = vigil_service.compute_news2(70, 120, 16, 36.8, 98)
    assert monitor_only["raw_news2"] == 0
    assert monitor_only["complete"] is False
    assert set(monitor_only["missing_parameters"]) == {
        "Consciousness (ACVPU)", "Supplemental O2"
    }
    assert "All vitals within normal range" not in monitor_only["drivers"]
