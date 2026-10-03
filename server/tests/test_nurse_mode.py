"""Nurse mode — shift board, observation scoring, and observation recording.

Covers the pieces a ward nurse's screen depends on being right:
  * the free-text medication frequency parser (the real strings in the DB),
  * the RCP NEWS2 monitoring-escalation table,
  * derived medication due times, including the roll-forward to the current
    dosing slot and the "never show an untimeable med as late" rule,
  * recording an observation set: vitals + risk score + hash-chained audit,
    range/ACVPU validation, and dedupe of a replayed bedside set,
  * route auth — vitals.write is nurse/doctor only, and a pharmacist cannot
    even read the board,
  * the PHI scope boundary — the board's ward comes from the authenticated user
    and NOTHING in the request can widen it, and every read is audited.
"""
import json
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from database import db_session, verify_audit_chain
from services import nurse_shift_service as nurse
from routes.nurse_routes import router as nurse_router
from routes.vigil_routes import router as vigil_router
from auth import create_token


DEPT = "Nurse Test Ward"
# A second ward, used only to prove a leak would be visible: nothing admitted
# here may ever appear on a DEPT nurse's board.
OTHER_DEPT = "Nurse Test Other Ward"


# ── fixtures ──────────────────────────────────────────────────────────────────
def _discharge(patient_id: str) -> None:
    """Retire one patient this module admitted.

    TEST-ISOLATION LAW. These fixtures are function-scoped and seed a patient
    with a FIXED NAME ("Nurse Test", "Other Ward Patient"), so every test that
    used them left another admitted patient behind under the same name. By the
    end of the module the shared session database held a pile of identically
    named admitted patients.

    That is not untidiness. A later module — tests/test_sage_department_scope.py
    — asks Sage to resolve a patient BY NAME for an unscoped governance account,
    and with several admitted "Nurse Test" rows visible it can no longer pick
    one, so the query scope comes back DISAMBIGUATE instead of PATIENT_SPECIFIC.
    Three tests failed for a reason that had nothing to do with the code they
    were testing.

    It was masked in a full alphabetical `pytest tests/` run by a later module
    that happens to clean up — so the suite passed by luck of module ordering,
    which is exactly the accidental green this law exists to prevent.

    Both status columns are set: room_fit_service and the census surfaces filter
    on patients.status, while the department-scoped surfaces join admissions.
    Leaving either one 'active' leaves the residue half-visible — the worst
    outcome, because it looks cleaned up.
    """
    try:
        with db_session() as db:
            db.execute("UPDATE patients SET status = 'discharged' WHERE id = ?", (patient_id,))
            db.execute(
                "UPDATE admissions SET status = 'discharged' WHERE patient_id = ?",
                (patient_id,),
            )
    except Exception:
        # Never let cleanup fail a test — but do not swallow it silently either,
        # since a failure here reintroduces the cross-module bug above.
        import logging
        logging.getLogger(__name__).warning(
            "test_nurse_mode: failed to discharge %s; later modules may see it", patient_id
        )


@pytest.fixture()
def patient():
    """An admitted patient in an isolated department, so board tests only ever
    see rows this test created."""
    pid = f"PATNU{uuid.uuid4().hex[:6].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO patients (id, name, mrn, bed, ward, age, sex, diagnosis, status) "
            "VALUES (?, ?, ?, 'B-1', 'NW', 60, 'M', 'Pneumonia', 'admitted')",
            (pid, "Nurse Test", f"MRN-{uuid.uuid4().hex[:6]}"),
        )
        db.execute(
            "INSERT OR IGNORE INTO users (id, name, role, password_hash) "
            "VALUES ('NURTESTADM', 'Nurse Test Admitter', 'doctor', 'x')"
        )
        db.execute(
            "INSERT INTO admissions (patient_id, department, status, admitted_by) "
            "VALUES (?, ?, 'active', 'NURTESTADM')",
            (pid, DEPT),
        )
    yield pid
    _discharge(pid)


@pytest.fixture()
def nurse_user():
    uid = f"NUR{uuid.uuid4().hex[:6].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT OR IGNORE INTO users (id, name, role, password_hash, department) "
            "VALUES (?, ?, 'nurse', 'x', ?)",
            (uid, "Nurse Obs", DEPT),
        )
    return uid


@pytest.fixture()
def other_dept_patient():
    """An admitted patient on a DIFFERENT ward. Present in every scope test so a
    leak is provable rather than inferred from a count."""
    pid = f"PATOT{uuid.uuid4().hex[:6].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO patients (id, name, mrn, bed, ward, age, sex, diagnosis, status) "
            "VALUES (?, ?, ?, 'C-9', 'OW', 71, 'F', 'Sepsis', 'admitted')",
            (pid, "Other Ward Patient", f"MRN-{uuid.uuid4().hex[:6]}"),
        )
        db.execute(
            "INSERT OR IGNORE INTO users (id, name, role, password_hash) "
            "VALUES ('NURTESTADM', 'Nurse Test Admitter', 'doctor', 'x')"
        )
        db.execute(
            "INSERT INTO admissions (patient_id, department, status, admitted_by) "
            "VALUES (?, ?, 'active', 'NURTESTADM')",
            (pid, OTHER_DEPT),
        )
    yield pid
    _discharge(pid)


@pytest.fixture()
def client():
    app = FastAPI()
    app.include_router(nurse_router)
    return TestClient(app)


@pytest.fixture()
def both_boards_client():
    """Nurse board AND the Vigil patient board on one app.

    The two surfaces must agree about who a user may see — the cross-ward hole
    existed precisely because they disagreed — so the parity assertion has to be
    able to call both with one token.
    """
    app = FastAPI()
    app.include_router(nurse_router)
    app.include_router(vigil_router)
    return TestClient(app)


def _auth(uid, role="nurse"):
    return {"Authorization": f"Bearer {create_token(uid, role)}"}


def _mk_user(role, department=DEPT):
    """Create a real user row and return (id, auth headers) — get_current_user
    resolves the token subject against the users table."""
    uid = f"{role[:3].upper()}{uuid.uuid4().hex[:6].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT OR IGNORE INTO users (id, name, role, password_hash, department) "
            "VALUES (?, ?, ?, 'x', ?)",
            (uid, f"Test {role}", role, department),
        )
    return uid, _auth(uid, role)


def _user(role):
    return _mk_user(role)[1]


def _chart_med(patient_id, drug="Paracetamol", frequency="Q6H", started_at=None,
               status="active", source="clinician", verified_by=None):
    with db_session() as db:
        cur = db.execute(
            """INSERT INTO medications (patient_id, drug_name, dosage, route, frequency,
                                        status, prescribed_by, source, verified_by, started_at)
               VALUES (?, ?, '500mg', 'Oral', ?, ?, 'D001', ?, ?, ?)""",
            (patient_id, drug, frequency, status, source, verified_by,
             (started_at or datetime.now(timezone.utc)).isoformat()),
        )
        return cur.lastrowid


def _give(med_id, patient_id, actor, at):
    with db_session() as db:
        db.execute(
            """INSERT INTO medication_administrations
                   (medication_id, patient_id, administered_by, status, administered_at)
               VALUES (?, ?, ?, 'given', ?)""",
            (med_id, patient_id, actor, at.isoformat()),
        )


def _card(patient_id):
    board = nurse.build_board(DEPT)
    for c in board["patients"]:
        if c["patient_id"] == patient_id:
            return c
    raise AssertionError(f"{patient_id} missing from the board")


# ── frequency parsing ─────────────────────────────────────────────────────────
@pytest.mark.parametrize("freq,kind,minutes", [
    ("Q6H", "scheduled", 360),
    ("Q1H", "scheduled", 60),
    ("Q12H", "scheduled", 720),
    ("q30min", "scheduled", 30),
    ("6 hourly", "scheduled", 360),
    ("BD", "scheduled", 720),
    ("TDS", "scheduled", 480),
    ("QDS", "scheduled", 360),
    ("Daily", "scheduled", 1440),
    ("Nightly", "scheduled", 1440),
    ("Daily x3", "scheduled", 1440),
    ("Daily (24h post-tPA)", "scheduled", 1440),
])
def test_parse_frequency_scheduled(freq, kind, minutes):
    parsed = nurse.parse_frequency(freq)
    assert parsed["kind"] == kind
    assert parsed["interval_minutes"] == minutes


@pytest.mark.parametrize("freq", [
    "PRN", "Q8H PRN", "Q10min PRN", "PRN BP>185/110", "If MAP<65", "as required",
])
def test_prn_beats_any_interval(freq):
    """A PRN order with a minimum interval is NOT a schedule. Showing "Q10min PRN"
    as overdue every ten minutes would train nurses to ignore the overdue colour."""
    parsed = nurse.parse_frequency(freq)
    assert parsed["kind"] == "prn"
    assert parsed["interval_minutes"] is None


@pytest.mark.parametrize("freq,kind", [
    ("Continuous", "continuous"),
    ("Infusion", "continuous"),
    ("In fluids", "continuous"),
    ("Bolus+infusion", "continuous"),
    ("Transfusion", "continuous"),
    ("Tapering", "continuous"),
    ("Stat", "stat"),
    ("Over 30min", "unscheduled"),
    ("", "unscheduled"),
    (None, "unscheduled"),
])
def test_parse_frequency_non_scheduled(freq, kind):
    assert nurse.parse_frequency(freq)["kind"] == kind


# ── NEWS2 → obs escalation (RCP table) ────────────────────────────────────────
@pytest.mark.parametrize("raw,single3,minutes", [
    (0, False, 720),    # routine, 12-hourly
    (3, False, 240),    # low score, 4-hourly minimum
    (4, False, 240),
    (5, False, 60),     # medium, hourly
    (6, False, 60),
    (7, False, 30),     # high, continuous monitoring
    (12, False, 30),
    (3, True, 60),      # single parameter 3 escalates a low aggregate to hourly
    (6, True, 60),
    (9, True, 30),      # already tighter than the single-param floor
])
def test_obs_schedule_bands(raw, single3, minutes):
    sched = nurse.obs_schedule({"raw_news2": raw, "single_param_3": single3})
    assert sched["interval_minutes"] == minutes
    assert sched["band"] and sched["response"]


def test_obs_schedule_without_a_score_is_not_reassuring():
    """No NEWS2 must fall back to 4-hourly, never to the 12-hourly relaxation —
    an absent score is not a score of zero."""
    assert nurse.obs_schedule(None)["interval_minutes"] == 240
    assert nurse.obs_schedule({})["interval_minutes"] == 240


# ── shift board ───────────────────────────────────────────────────────────────
def test_board_flags_patient_with_no_observations_as_overdue(patient):
    card = _card(patient)
    assert card["last_obs_at"] is None
    assert card["obs_overdue"] is True, "an admitted patient with no obs at all is overdue"
    assert card["news2"] is None


def test_board_scores_latest_vitals_and_sets_obs_due(patient, nurse_user):
    nurse.record_observation(
        patient, nurse_user,
        {"heart_rate": 70, "systolic_bp": 120, "respiratory_rate": 16,
         "temperature": 36.8, "spo2": 98},
    )
    card = _card(patient)
    assert card["news2"]["raw_news2"] == 0
    assert card["obs_interval_minutes"] == 720
    assert card["obs_overdue"] is False
    assert card["obs_due_at"] is not None


def test_board_due_time_anchors_on_last_given_dose(patient, nurse_user):
    med_id = _chart_med(patient, frequency="Q6H",
                        started_at=datetime.now(timezone.utc) - timedelta(days=2))
    given_at = datetime.now(timezone.utc) - timedelta(hours=2)
    _give(med_id, patient, nurse_user, given_at)

    task = next(m for m in _card(patient)["medications"] if m["medication_id"] == med_id)
    assert task["due_basis"] == "last_given"
    assert task["missed_slots"] == 0
    # last dose 2h ago on a 6-hourly order → next dose in ~4h, not overdue.
    assert 220 < task["minutes_until_due"] < 245
    assert task["overdue"] is False


def test_board_rolls_forward_to_the_current_dosing_slot(patient):
    """An order started days ago with nothing charted must report THIS slot's
    lateness plus a missed-slot count — not "three days late", which would paint
    the whole board red and stop meaning anything."""
    _chart_med(patient, frequency="Q6H",
               started_at=datetime.now(timezone.utc) - timedelta(hours=25))
    task = _card(patient)["medications"][0]
    assert task["due_basis"] == "order_start"
    assert task["missed_slots"] == 3          # slots at +6h, +12h, +18h skipped
    assert -360 < task["minutes_until_due"] <= 0   # current slot is within one interval


@pytest.mark.parametrize("freq", ["PRN", "Continuous", "Over 30min"])
def test_untimeable_meds_are_never_overdue(patient, freq):
    """A med we cannot time is a med we must not imply is late."""
    _chart_med(patient, frequency=freq,
               started_at=datetime.now(timezone.utc) - timedelta(days=5))
    task = _card(patient)["medications"][0]
    assert task["due_at"] is None
    assert task["overdue"] is False
    assert task["due_soon"] is False


def test_stat_dose_is_due_until_given_then_settles(patient, nurse_user):
    med_id = _chart_med(patient, frequency="Stat",
                        started_at=datetime.now(timezone.utc) - timedelta(hours=3))
    task = _card(patient)["medications"][0]
    assert task["due_basis"] == "stat_pending"
    assert task["overdue"] is True

    _give(med_id, patient, nurse_user, datetime.now(timezone.utc))
    task = _card(patient)["medications"][0]
    assert task["due_basis"] == "stat_given"
    assert task["overdue"] is False


def test_board_flags_unverified_ai_med_as_not_administrable(patient):
    _chart_med(patient, drug="Vancomycin", source="llm_extracted", verified_by=None)
    task = _card(patient)["medications"][0]
    assert task["unverified"] is True, (
        "an AI-extracted med with no pharmacist verification must be flagged before "
        "the nurse walks to the bedside — emar_service rejects it at the write"
    )


def test_withheld_dose_does_not_reset_the_dosing_clock(patient, nurse_user):
    """A 'not_given' row records that a dose was withheld; it must not look like
    the dose went in, or the next one would be scheduled off a dose never given."""
    med_id = _chart_med(patient, frequency="Q6H",
                        started_at=datetime.now(timezone.utc) - timedelta(hours=7))
    with db_session() as db:
        db.execute(
            """INSERT INTO medication_administrations
                   (medication_id, patient_id, administered_by, status,
                    not_given_reason, administered_at)
               VALUES (?, ?, ?, 'not_given', 'Refused', ?)""",
            (med_id, patient, nurse_user, datetime.now(timezone.utc).isoformat()),
        )
    task = _card(patient)["medications"][0]
    assert task["last_given_at"] is None
    assert task["due_basis"] == "order_start"
    assert task["overdue"] is True


def test_board_totals_and_sickest_first(patient, nurse_user):
    board = nurse.build_board(DEPT)
    assert board["department"] == DEPT
    assert board["totals"]["patients"] == len(board["patients"])
    levels = [(c["news2"] or {}).get("level", "stable") for c in board["patients"]]
    rank = {"critical": 0, "elevated": 1, "stable": 2}
    ranks = [rank.get(lv, 3) for lv in levels]
    assert ranks == sorted(ranks), "board must lead with the sickest patients"


# ── scoring preview ───────────────────────────────────────────────────────────
def test_preview_scores_without_persisting(patient):
    before = _vitals_count(patient)
    res = nurse.score_observation(patient, {"respiratory_rate": 26, "spo2": 89,
                                            "on_oxygen": True})
    assert res["news2"]["raw_news2"] == 8   # RR 3 + SpO2 3 + O2 2
    assert res["news2"]["level"] == "critical"
    assert res["schedule"]["interval_minutes"] == 30
    assert _vitals_count(patient) == before, "a preview must never write a reading"


def test_preview_and_record_agree(patient, nurse_user):
    """The previewed score and the recorded score come from one implementation;
    a divergence here is the bug class that makes a ward stop trusting the number."""
    obs = {"heart_rate": 124, "systolic_bp": 96, "diastolic_bp": 62,
           "respiratory_rate": 26, "temperature": 38.6, "spo2": 89, "on_oxygen": True}
    preview = nurse.score_observation(patient, obs)
    recorded = nurse.record_observation(patient, nurse_user, obs)
    assert preview["news2"]["raw_news2"] == recorded["news2"]["raw_news2"] == 13
    assert preview["schedule"] == recorded["schedule"]


def test_scale_2_patient_scored_on_scale_2(patient, nurse_user):
    with db_session() as db:
        db.execute("UPDATE patients SET on_scale_2 = 1 WHERE id = ?", (patient,))
    res = nurse.score_observation(patient, {"spo2": 90})
    assert res["news2"]["spo2_scale"] == 2
    # 90% is inside the Scale 2 target band (88-92) and must score 0, where
    # Scale 1 would have scored it 3.
    assert res["news2"]["raw_news2"] == 0


def _vitals_count(patient_id):
    with db_session() as db:
        return db.execute(
            "SELECT COUNT(*) c FROM vitals WHERE patient_id = ?", (patient_id,)
        ).fetchone()["c"]


# ── recording ─────────────────────────────────────────────────────────────────
def test_record_writes_vitals_risk_and_audit(patient, nurse_user):
    res = nurse.record_observation(
        patient, nurse_user,
        {"heart_rate": 124, "systolic_bp": 96, "diastolic_bp": 62,
         "respiratory_rate": 26, "temperature": 38.6, "spo2": 89,
         "consciousness": "confusion", "on_oxygen": True},
    )
    assert res["replayed"] is False
    assert res["news2"]["level"] == "critical"

    with db_session() as db:
        row = db.execute("SELECT * FROM vitals WHERE id = ?", (res["vitals_id"],)).fetchone()
        risk = db.execute(
            "SELECT * FROM risk_scores WHERE patient_id = ? ORDER BY id DESC LIMIT 1",
            (patient,),
        ).fetchone()
        audit = db.execute(
            """SELECT * FROM audit_logs WHERE action_type = 'observation_recorded'
               AND patient_id = ? ORDER BY id DESC LIMIT 1""",
            (patient,),
        ).fetchone()

    # every NEWS2 parameter round-trips, including the two the device feeds drop
    assert row["source"] == nurse.OBS_SOURCE
    assert row["consciousness"] == "confusion"
    assert row["on_oxygen"] == 1
    assert row["respiratory_rate"] == 26

    assert risk["level"] == "critical"
    assert json.loads(risk["drivers"])

    assert audit is not None, "an observation must never commit unaudited"
    assert audit["event_category"] == "clinical_access"
    assert audit["actor_user_id"] == nurse_user
    assert audit["target_id"] == str(res["vitals_id"])
    meta = json.loads(audit["metadata_json"])
    assert meta["news2"] == res["news2"]["raw_news2"]
    assert meta["source"] == nurse.OBS_SOURCE

    chain = verify_audit_chain()
    assert chain["ok"], f"audit chain broken: {chain['broken_row_ids']}"


def test_replayed_bedside_set_is_not_charted_twice(patient, nurse_user):
    """The offline queue replays the body verbatim, so the same taken_at arrives
    again. A duplicate would look like a real repeat measurement."""
    taken_at = (datetime.now(timezone.utc) - timedelta(minutes=3)).isoformat()
    obs = {"heart_rate": 88, "spo2": 96}
    first = nurse.record_observation(patient, nurse_user, obs, taken_at=taken_at)
    replay = nurse.record_observation(patient, nurse_user, obs, taken_at=taken_at)

    assert replay["replayed"] is True
    assert replay["vitals_id"] == first["vitals_id"]
    assert _vitals_count(patient) == 1


def test_distinct_readings_are_both_kept(patient, nurse_user):
    now = datetime.now(timezone.utc)
    nurse.record_observation(patient, nurse_user, {"heart_rate": 88},
                             taken_at=(now - timedelta(minutes=30)).isoformat())
    nurse.record_observation(patient, nurse_user, {"heart_rate": 92},
                             taken_at=(now - timedelta(minutes=10)).isoformat())
    assert _vitals_count(patient) == 2


def test_partial_set_is_allowed(patient, nurse_user):
    """A nurse may legitimately record a partial set (no BP on an agitated
    patient); NEWS2 scores what is present."""
    res = nurse.record_observation(patient, nurse_user, {"spo2": 91})
    assert res["news2"]["raw_news2"] == 3   # SpO2 <92 on Scale 1
    with db_session() as db:
        row = db.execute("SELECT * FROM vitals WHERE id = ?", (res["vitals_id"],)).fetchone()
    assert row["systolic_bp"] is None


@pytest.mark.parametrize("obs,fragment", [
    ({"heart_rate": 900}, "heart_rate"),
    ({"heart_rate": -5}, "heart_rate"),
    ({"temperature": 100}, "temperature"),
    ({"spo2": 150}, "spo2"),
    ({"respiratory_rate": "abc"}, "respiratory_rate"),
    ({}, "at least one reading"),
    ({"heart_rate": 80, "consciousness": "asleep"}, "consciousness"),
])
def test_entry_guards_reject_bad_sets(patient, nurse_user, obs, fragment):
    with pytest.raises(ValueError) as exc:
        nurse.record_observation(patient, nurse_user, obs)
    assert fragment in str(exc.value)
    assert _vitals_count(patient) == 0


def test_future_observation_time_rejected(patient, nurse_user):
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    with pytest.raises(ValueError, match="future"):
        nurse.record_observation(patient, nurse_user, {"heart_rate": 80}, taken_at=future)


def test_unknown_patient_rejected(nurse_user):
    with pytest.raises(ValueError, match="not found"):
        nurse.record_observation("NO-SUCH-PATIENT", nurse_user, {"heart_rate": 80})


# ── provenance: the nurse write is a ManualEntrySource path ───────────────────
def test_nurse_write_lands_with_the_pipeline_provenance_contract(patient, nurse_user):
    """The row a nurse write produces carries the same provenance columns, with
    the same semantics, as a pipeline-sourced row: `source` is a label
    `classify_source_label` maps to a SourceKind — MANUAL, a human typed it —
    and `source_event_id` links a row to its raw wire event, NULL here because
    a typed set has no wire payload (its evidence row is the fail-closed audit
    entry). REGRESSION: until 2026-08-10 'nurse_manual' was absent from
    _LABEL_KIND, so the ward's own observations classified UNKNOWN and the
    shadow scorecard could not attribute the pilot's most common vitals path."""
    from services.vitals_source import SourceKind, classify_source_label, is_machine_measured

    res = nurse.record_observation(patient, nurse_user, {"heart_rate": 88, "spo2": 96})
    with db_session() as db:
        row = db.execute(
            "SELECT source, source_event_id, timestamp FROM vitals WHERE id = ?",
            (res["vitals_id"],),
        ).fetchone()
    assert row["source"] == nurse.OBS_SOURCE == "nurse_manual"
    assert classify_source_label(row["source"]) is SourceKind.MANUAL
    assert not is_machine_measured(row["source"])
    assert row["source_event_id"] is None
    assert row["timestamp"] == res["recorded_at"]


def test_nurse_write_flows_through_the_registered_manual_source(patient, nurse_user):
    """ManualEntrySource is the wrapper for the nurse charting path: the write
    is counted in the same source stats every other vitals feed reports, so
    'is the ward's manual entry alive?' is answerable from the admin surface
    instead of by grepping logs."""
    from services.vitals_source import ManualEntrySource
    from services.vitals_source.registry import registry

    displaced = registry.get("manual")
    if displaced is not None:
        registry.unregister("manual")
    source = registry.register(ManualEntrySource(None))
    try:
        taken_at = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
        nurse.record_observation(patient, nurse_user, {"heart_rate": 88},
                                 taken_at=taken_at)
        nurse.record_observation(patient, nurse_user, {"heart_rate": 88},
                                 taken_at=taken_at)   # verbatim replay
        assert source.stats.messages_received == 2
        assert source.stats.messages_accepted == 2
        assert source.stats.readings_ingested == 1
        assert source.stats.duplicates == 1
        assert source.stats.last_reading_at is not None
    finally:
        registry.unregister("manual")
        if displaced is not None:
            registry.register(displaced)


def test_audit_failure_rolls_back_the_reading_and_is_loud(patient, nurse_user, monkeypatch):
    """Fail-loud through the whole path, on a REAL failure mode: clinical_access
    audit is fail-closed, so a refused audit write must surface to the caller —
    never a queued or optimistic ack — and the vitals row must roll back with
    it, because an observation that cannot be audited must not commit."""
    def _refuse(*args, **kwargs):
        raise RuntimeError("audit store refused")

    monkeypatch.setattr(nurse, "log_audit_event", _refuse)
    with pytest.raises(RuntimeError, match="audit store refused"):
        nurse.record_observation(patient, nurse_user, {"heart_rate": 88})
    assert _vitals_count(patient) == 0, \
        "a reading whose audit failed must not stay charted"


# ── HTTP routes ───────────────────────────────────────────────────────────────
def test_route_board_scopes_to_the_users_department(patient, nurse_user, client):
    resp = client.get("/api/nurse/board", headers=_auth(nurse_user))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["department"] == DEPT
    assert any(c["patient_id"] == patient for c in body["patients"])


def test_route_preview_then_record(patient, nurse_user, client):
    hdr = _auth(nurse_user)
    obs = {"respiratory_rate": 26, "spo2": 89, "on_oxygen": True}

    preview = client.post(f"/api/nurse/patients/{patient}/news2-preview", json=obs, headers=hdr)
    assert preview.status_code == 200, preview.text
    assert preview.json()["news2"]["raw_news2"] == 8
    assert _vitals_count(patient) == 0

    rec = client.post(f"/api/nurse/patients/{patient}/observations", json=obs, headers=hdr)
    assert rec.status_code == 200, rec.text
    assert rec.json()["news2"]["raw_news2"] == 8
    assert _vitals_count(patient) == 1


def test_route_validation_and_missing_patient(patient, nurse_user, client):
    hdr = _auth(nurse_user)
    bad = client.post(f"/api/nurse/patients/{patient}/observations",
                      json={"heart_rate": 900}, headers=hdr)
    assert bad.status_code == 422
    assert "heart_rate" in bad.json()["detail"]

    missing = client.post("/api/nurse/patients/NOPE/observations",
                          json={"heart_rate": 80}, headers=hdr)
    assert missing.status_code == 404


# ── PHI scope boundary ────────────────────────────────────────────────────────
# A nurse's ward is decided by the SERVER from the authenticated user. The board
# once took `?department=` and `?all_departments=true` off the query string, and
# NUR001 — five patients on /api/vigil/patients — pulled thirty across six
# departments through it, unaudited. These tests pin that shut.

# Table-driven ON PURPOSE. The defect was a parameter, so the guarantee under
# test is "no parameter", not "not these two parameters" — a future query flag
# lands here as a new row rather than as a new hole. Every entry is a query
# string a client could send; none of them may change what comes back.
SCOPE_WIDENING_ATTEMPTS = [
    "?all_departments=true",
    "?all_departments=True",
    "?all_departments=1",
    "?all_departments=false",           # the "off" form must be just as inert
    f"?department={quote(OTHER_DEPT)}",
    "?department=",                     # empty → must not mean "unscoped"
    "?department=%25",                  # SQL wildcard, in case scope ever reaches LIKE
    "?department=Cardiology&all_departments=true",
    f"?department={quote(DEPT)}&department={quote(OTHER_DEPT)}",   # repeated param
    "?dept=Cardiology",
    "?ward=OW",
    "?departments=Cardiology,ICU",
    "?scope=all",
    "?all=1",
    "?limit=1000&offset=0",
]


@pytest.mark.parametrize("query", SCOPE_WIDENING_ATTEMPTS)
def test_nurse_cannot_widen_their_own_phi_scope(patient, other_dept_patient,
                                                nurse_user, client, query):
    """No query parameter may widen a nurse's ward. Server decides, full stop.

    Also checks the trail records the scope the user GOT, never the one they
    asked for — an audit row that echoed the request would be worse than none.
    """
    resp = client.get(f"/api/nurse/board{query}", headers=_auth(nurse_user))
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["department"] == DEPT, (
        f"{query!r} changed the board's scope — the ward must come from the user"
    )
    ids = {c["patient_id"] for c in body["patients"]}
    assert patient in ids, "positive control: the nurse's own patient must be on the board"
    assert other_dept_patient not in ids, f"{query!r} leaked a patient from {OTHER_DEPT}"
    assert {c["department"] for c in body["patients"]} == {DEPT}

    meta = json.loads(_latest_board_audit(nurse_user)["metadata_json"])
    assert meta["department"] == DEPT
    assert meta["hospital_wide"] is False
    assert meta["scope_source"] == "authenticated_user"


def _latest_board_audit(actor_id):
    with db_session() as db:
        return db.execute(
            """SELECT * FROM audit_logs
               WHERE action_type = 'nurse_board_viewed' AND actor_user_id = ?
               ORDER BY id DESC LIMIT 1""",
            (actor_id,),
        ).fetchone()


def test_board_read_is_audited_as_phi_access(patient, nurse_user, client):
    """A screen that returns every patient's clinical state is a PHI disclosure,
    and the write middleware only covers POST/PUT/PATCH/DELETE."""
    resp = client.get("/api/nurse/board", headers=_auth(nurse_user))
    assert resp.status_code == 200

    row = _latest_board_audit(nurse_user)
    assert row is not None, "a bulk PHI read must never happen untraced"
    assert row["event_category"] == "phi_access", "must be a fail-closed PHI category"
    assert row["actor_user_id"] == nurse_user
    assert row["actor_role"] == "nurse"
    assert row["outcome"] == "success"
    assert row["request_path"] == "/api/nurse/board"
    assert row["http_method"] == "GET"
    assert row["target_type"] == "department"
    assert row["target_id"] == DEPT

    chain = verify_audit_chain()
    assert chain["ok"], f"audit chain broken: {chain['broken_row_ids']}"


def test_board_read_audit_is_fail_closed(patient, nurse_user, client, monkeypatch):
    """phi_access is a fail-closed category: if the trail cannot be written, the
    PHI must not be served. Both halves are asserted — the category is in the
    fail-closed set, and the route audits BEFORE it builds the board."""
    from services.audit_service import _FAIL_CLOSED_CATEGORIES, AuditWriteError

    assert "phi_access" in _FAIL_CLOSED_CATEGORIES

    def _boom(*a, **kw):
        raise AuditWriteError("audit storage unavailable")

    monkeypatch.setattr("routes.nurse_routes.log_audit_event", _boom)
    monkeypatch.setattr(
        nurse, "build_board",
        lambda *a, **kw: pytest.fail("PHI was assembled after the audit write failed"),
    )
    with pytest.raises(AuditWriteError):
        client.get("/api/nurse/board", headers=_auth(nurse_user))


def test_nurse_board_and_vigil_board_agree_on_who_the_user_may_see(
    patient, other_dept_patient, nurse_user, both_boards_client
):
    """The two patient surfaces must resolve the SAME cohort for the same token.

    This is the assertion the original defect failed: /api/vigil/patients gave
    NUR001 five patients while /api/nurse/board gave thirty. Both now go through
    permissions.department_scope, and this test fails the moment one of them
    grows its own idea of scope again.
    """
    hdr = _auth(nurse_user)
    vigil = both_boards_client.get("/api/vigil/patients?limit=500", headers=hdr)
    board = both_boards_client.get("/api/nurse/board", headers=hdr)
    assert vigil.status_code == 200, vigil.text
    assert board.status_code == 200, board.text

    vigil_ids = {p["id"] for p in vigil.json()}
    board_ids = {c["patient_id"] for c in board.json()["patients"]}
    assert board_ids == vigil_ids
    assert patient in board_ids
    assert other_dept_patient not in board_ids


def test_user_without_a_department_reads_hospital_wide_and_it_is_flagged(
    patient, other_dept_patient, client
):
    """A departmentless account (a governance admin) is unscoped on BOTH boards —
    the pre-existing convention. It is a property of the user ROW that only an
    administrator can set, so the audit row must say so out loud."""
    uid, hdr = _mk_user("admin", department=None)
    resp = client.get("/api/nurse/board", headers=hdr)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["department"] is None
    ids = {c["patient_id"] for c in body["patients"]}
    assert {patient, other_dept_patient} <= ids

    row = _latest_board_audit(uid)
    assert row is not None
    assert row["event_category"] == "phi_access"
    meta = json.loads(row["metadata_json"])
    assert meta["hospital_wide"] is True
    assert meta["department"] is None


def test_department_scope_normalises_a_blank_department_to_absent():
    """A whitespace department must mean "none on record", not a ward literally
    named " ".

    Untrimmed, `"  "` is truthy: it would scope a board to a department no
    patient is admitted to, and the nurse would be shown an EMPTY ward with no
    error — worse than showing too much. Normalising in the one helper makes
    every surface agree on what a blank department means instead of each one
    deciding with its own `if dept:`.
    """
    from permissions import department_scope

    assert department_scope({"department": DEPT}) == DEPT
    assert department_scope({"department": "  " + DEPT + " "}) == DEPT
    assert department_scope({"department": ""}) is None
    assert department_scope({"department": "   "}) is None
    assert department_scope({"department": None}) is None
    assert department_scope({}) is None


def test_build_board_will_not_default_to_the_whole_hospital():
    """The service takes no default scope. Forgetting to pass one is a TypeError,
    not a hospital-wide query."""
    with pytest.raises(TypeError):
        nurse.build_board()


def test_route_auth_matrix(patient, nurse_user, client):
    """vitals.write is a nurse/doctor permission. An admin may read the ward but
    must not be able to chart a reading, and a pharmacist sees none of it."""
    obs = {"heart_rate": 80}

    for role in ("nurse", "doctor"):
        hdr = _user(role)
        assert client.get("/api/nurse/board", headers=hdr).status_code == 200
        resp = client.post(f"/api/nurse/patients/{patient}/observations",
                           json={**obs, "taken_at": datetime.now(timezone.utc).isoformat()},
                           headers=hdr)
        assert resp.status_code == 200, f"{role} must be able to chart obs: {resp.text}"

    admin = _user("admin")
    assert client.get("/api/nurse/board", headers=admin).status_code == 200
    assert client.post(f"/api/nurse/patients/{patient}/observations",
                       json=obs, headers=admin).status_code == 403

    pharm = _user("pharmacist")
    assert client.get("/api/nurse/board", headers=pharm).status_code == 403
    assert client.post(f"/api/nurse/patients/{patient}/observations",
                       json=obs, headers=pharm).status_code == 403
