"""The surgical case lifecycle: a case cannot END before it BEGINS, and the
time between the two is now recorded.

THE DEFECTS (pilot dry run, 2026-08-14), all three in workflow 5:

  1. COMPLETION WAS NOT A TRANSITION. ``POST /{id}/complete`` guarded only
     against completing TWICE. Every other status walked through it, and during
     the dry run an agent marked a case 'completed' that ``/start`` had REFUSED
     twice for want of team confirmations. That is not a cosmetic wrong status:
     completion AUTHORS A POST-OPERATIVE NOTE INTO THE PATIENT'S CHART and files
     a `surgery_completed` row on the tamper-evident audit chain. The record then
     says an operation happened, with a note describing it, for an operation
     that never started.

  2. THE WHO CHECKLIST GATED NOTHING. 19/19 bought a progress bar. A case could
     be closed at 0/19 with no complaint anywhere.

  3. TURNAROUND WAS UNMEASURABLE. `surgeries` recorded no time at all —
     `scheduled_date`/`scheduled_time` are the booking and `created_at` is when
     the row was written. The metric workflow 5 is NAMED after could not be
     computed from the database, at all.

WHAT THESE TESTS HOLD DOWN:

  STATE GATE   completing a case that never started is refused, the case does
               not move, and NO post-op note is written into the chart.
  CHECKLIST    with the gate on (the default) an incomplete checklist refuses
               the completion; with it off the same case closes. Both sides are
               asserted, because a gate that cannot be turned off is a workflow
               ruling imposed on a hospital that has not agreed to it, and a
               gate that is off by default is decoration again.
  TIMING       started_at / completed_at populate on the transitions, survive
               the post-op-note compensation path, and produce a turnaround that
               matches the elapsed time.
  AUDIT        a refusal is on the chain with outcome='denied' — "somebody tried
               to close a case that had not started" is exactly what a safety
               policy exists to surface.
"""
import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from auth import create_token
from database import db_session
from main import app
from services.who_checklist import TEMPLATE_VERSION, WHO_CHECKLIST

client = TestClient(app)

ALL_ITEM_IDS = [item["id"] for phase in WHO_CHECKLIST["phases"] for item in phase["items"]]


@pytest.fixture(autouse=True)
def _default_gate(monkeypatch):
    """Every test in this module starts from the SHIPPED default.

    The gate is read from the environment at call time, so an operator's (or
    another test's) ORB_SURGERY_REQUIRE_CHECKLIST would otherwise decide what
    this file proves. Unset it, and let the one test that cares pin it.
    """
    monkeypatch.delenv("ORB_SURGERY_REQUIRE_CHECKLIST", raising=False)


# ── fixtures ──────────────────────────────────────────────────────────────────

def _mk_user(role: str, prefix: str) -> str:
    uid = f"{prefix}{uuid.uuid4().hex[:6].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT OR IGNORE INTO users (id, name, role, password_hash) VALUES (?, ?, ?, 'x')",
            (uid, f"{role.title()} {prefix}", role),
        )
    return uid


@pytest.fixture()
def doctor():
    return _mk_user("doctor", "SLDOC")


@pytest.fixture()
def nurse():
    return _mk_user("nurse", "SLNUR")


@pytest.fixture()
def patient():
    pid = f"PATSL{uuid.uuid4().hex[:6].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO patients (id, name, mrn, ward, bed, diagnosis) "
            "VALUES (?, ?, ?, 'Ward A', 'A-2', 'Perforated viscus')",
            (pid, "Lifecycle Patient", f"MRN-{uuid.uuid4().hex[:8]}"),
        )
    yield pid
    # These cases are dated TODAY (a lifecycle test is about a case that is
    # happening), so unlike the past-dated rows in the sibling surgical suite
    # they land in `/schedule`'s live window and every other module's schedule
    # assertions would see them. Clean up after ourselves: assignments first
    # (FK to surgeries), then the cases, then the prep rows other modules'
    # setups DELETE around.
    with db_session() as db:
        db.execute(
            "DELETE FROM surgery_team_assignments WHERE surgery_id IN "
            "(SELECT id FROM surgeries WHERE patient_id = ?)", (pid,),
        )
        db.execute("DELETE FROM surgery_prep_requests WHERE patient_id = ?", (pid,))
        db.execute("DELETE FROM surgeries WHERE patient_id = ?", (pid,))


def _auth(user_id: str, role: str = "doctor") -> dict:
    return {"Authorization": f"Bearer {create_token(user_id, role)}"}


def _mk_surgery(patient_id, surgeon_id, status="scheduled"):
    sid = f"SURG{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute(
            """INSERT INTO surgeries (id, patient_id, surgeon_id, procedure_name, diagnosis,
                                      scheduled_date, scheduled_time, status)
               VALUES (?, ?, ?, 'Laparotomy', 'Perforation', '2026-08-14', '09:00', ?)""",
            (sid, patient_id, surgeon_id, status),
        )
    return sid


def _assign(surgery_id, user_id, status="confirmed", role_name="Surgeon"):
    with db_session() as db:
        db.execute(
            "INSERT INTO surgery_team_assignments (surgery_id, user_id, role_name, status) "
            "VALUES (?, ?, ?, ?)",
            (surgery_id, user_id, role_name, status),
        )


def _tick_all(surgery_id, actor_id, omit=()):
    """Attest the WHO checklist directly on the row.

    Written as the persisted blob rather than through 19 HTTP calls because most
    tests here are about what the checklist GATES, not about how it is filled;
    test_a_case_ticked_through_the_real_route_can_be_closed does drive the real
    endpoint so the two representations cannot drift apart unnoticed.
    """
    now = datetime.now(timezone.utc).isoformat()
    state = {
        item: {"checked": True, "by": actor_id, "at": now}
        for item in ALL_ITEM_IDS if item not in omit
    }
    with db_session() as db:
        db.execute(
            "UPDATE surgeries SET who_checklist = ? WHERE id = ?",
            (json.dumps({"template": TEMPLATE_VERSION, "state": state}), surgery_id),
        )


def _row(surgery_id):
    with db_session() as db:
        return dict(db.execute(
            "SELECT status, started_at, completed_at FROM surgeries WHERE id = ?",
            (surgery_id,),
        ).fetchone())


def _note_count(patient_id):
    with db_session() as db:
        return db.execute(
            "SELECT COUNT(*) c FROM scribe_notes WHERE patient_id = ?", (patient_id,)
        ).fetchone()["c"]


def _audit(action_type, target_id):
    with db_session() as db:
        return db.execute(
            "SELECT * FROM audit_logs WHERE action_type = ? AND target_id = ? "
            "ORDER BY id DESC LIMIT 1",
            (action_type, target_id),
        ).fetchone()


def _ready_case(patient_id, doctor_id, status="in_progress"):
    """A case that is legally completable except for whatever the test breaks."""
    sid = _mk_surgery(patient_id, doctor_id, status=status)
    _assign(sid, doctor_id)
    _tick_all(sid, doctor_id)
    return sid


# ══════════════════════════════════════════════════════════════════════════
#  GATE 1 — A CASE CANNOT END BEFORE IT BEGINS
# ══════════════════════════════════════════════════════════════════════════

def test_completing_a_case_that_never_started_is_refused(doctor, patient):
    """THE dry-run defect, in one test. The checklist is fully attested here on
    purpose, so the only thing that can refuse this is the state gate."""
    sid = _mk_surgery(patient, doctor, status="scheduled")
    _tick_all(sid, doctor)
    notes_before = _note_count(patient)

    resp = client.post(f"/api/surgery/{sid}/complete", headers=_auth(doctor))

    assert resp.status_code == 409, resp.text
    assert "in progress" in resp.json()["detail"]
    row = _row(sid)
    assert row["status"] == "scheduled", "a refused completion must not move the case"
    assert row["completed_at"] is None
    assert _note_count(patient) == notes_before, \
        "a refused completion must not author a post-op note into the chart"


def test_the_exact_dry_run_sequence_start_refused_then_complete_refused(doctor, nurse, patient):
    """Reproduces what the verification agent actually did: /start refused twice
    for outstanding team confirmations, then /complete accepted anyway."""
    sid = _mk_surgery(patient, doctor, status="scheduled")
    _assign(sid, doctor, status="confirmed")
    _assign(sid, nurse, status="alerted", role_name="Scrub Nurse")
    _tick_all(sid, doctor)

    for _ in range(2):
        started = client.post(f"/api/surgery/{sid}/start", headers=_auth(doctor))
        assert started.status_code == 400, started.text
    assert _row(sid)["status"] == "scheduled"

    completed = client.post(f"/api/surgery/{sid}/complete", headers=_auth(doctor))
    assert completed.status_code == 409, completed.text
    assert _row(sid)["status"] == "scheduled"


@pytest.mark.parametrize("status", [
    "scheduled", "cancelled", "emergency_pending", "emergency_ready",
    "emergency_acknowledged", "emergency_staged", "emergency_context_generating",
])
def test_no_status_other_than_in_progress_can_be_completed(doctor, patient, status):
    """The gate is a whitelist, not a blacklist of the two states someone
    happened to think of: every non-running status refuses."""
    sid = _mk_surgery(patient, doctor, status=status)
    _tick_all(sid, doctor)
    resp = client.post(f"/api/surgery/{sid}/complete", headers=_auth(doctor))
    assert resp.status_code == 409, f"{status} -> {resp.status_code} {resp.text}"
    assert _row(sid)["status"] == status


def test_completing_a_running_case_still_works(doctor, patient):
    """The half a security-shaped fix would have destroyed: the normal path."""
    sid = _ready_case(patient, doctor)
    resp = client.post(f"/api/surgery/{sid}/complete", headers=_auth(doctor))
    assert resp.status_code == 200, resp.text
    assert resp.json()["note_id"]
    assert _row(sid)["status"] == "completed"


def test_completion_stays_idempotent(doctor, patient):
    """A repeat completion is still a no-op, not a 409 — the gate must not turn
    a retried request (lost response, double tap) into an error."""
    sid = _ready_case(patient, doctor)
    assert client.post(f"/api/surgery/{sid}/complete", headers=_auth(doctor)).status_code == 200
    notes_after_first = _note_count(patient)

    again = client.post(f"/api/surgery/{sid}/complete", headers=_auth(doctor))
    assert again.status_code == 200
    assert again.json()["status"] == "already_completed"
    assert _note_count(patient) == notes_after_first, "no second post-op note"


def test_a_refused_completion_is_on_the_audit_chain_as_denied(doctor, patient):
    sid = _mk_surgery(patient, doctor, status="scheduled")
    _tick_all(sid, doctor)
    client.post(f"/api/surgery/{sid}/complete", headers=_auth(doctor))

    row = _audit("surgery_completion_refused", sid)
    assert row is not None, "an attempt to close an unstarted case must be provable"
    assert row["outcome"] == "denied"
    assert row["patient_id"] == patient
    meta = json.loads(row["metadata_json"])
    assert meta["refusal_reason"] == "not_started"
    assert meta["from_status"] == "scheduled"


# ══════════════════════════════════════════════════════════════════════════
#  GATE 2 — THE WHO SURGICAL SAFETY CHECKLIST
# ══════════════════════════════════════════════════════════════════════════

def test_by_default_an_incomplete_checklist_refuses_the_completion(doctor, patient):
    """Was: 19/19 gated nothing and 0/19 gated nothing. The default ships ON."""
    sid = _mk_surgery(patient, doctor, status="in_progress")
    _tick_all(sid, doctor, omit={"so_counts"})  # 18/19 — sponge count not confirmed
    notes_before = _note_count(patient)

    resp = client.post(f"/api/surgery/{sid}/complete", headers=_auth(doctor))
    assert resp.status_code == 409, resp.text
    assert "18/19" in resp.json()["detail"], resp.text
    assert _row(sid)["status"] == "in_progress"
    assert _note_count(patient) == notes_before


def test_an_untouched_checklist_refuses_the_completion(doctor, patient):
    sid = _mk_surgery(patient, doctor, status="in_progress")
    resp = client.post(f"/api/surgery/{sid}/complete", headers=_auth(doctor))
    assert resp.status_code == 409
    assert "0/19" in resp.json()["detail"]


def test_the_checklist_gate_can_be_turned_off_per_site(doctor, patient, monkeypatch):
    """The hospital, not Orb, decides whether a missed tick blocks closing a
    case mid-list. Same case as the test above; only the ruling changes."""
    monkeypatch.setenv("ORB_SURGERY_REQUIRE_CHECKLIST", "0")
    sid = _mk_surgery(patient, doctor, status="in_progress")

    resp = client.post(f"/api/surgery/{sid}/complete", headers=_auth(doctor))
    assert resp.status_code == 200, resp.text
    assert _row(sid)["status"] == "completed"


def test_turning_the_gate_off_does_not_disable_the_state_gate(doctor, patient, monkeypatch):
    """The two gates are independent. A site that opts out of the checklist
    ruling still cannot complete an operation that never happened."""
    monkeypatch.setenv("ORB_SURGERY_REQUIRE_CHECKLIST", "off")
    sid = _mk_surgery(patient, doctor, status="scheduled")
    resp = client.post(f"/api/surgery/{sid}/complete", headers=_auth(doctor))
    assert resp.status_code == 409
    assert "in progress" in resp.json()["detail"]


def test_a_mistyped_gate_value_fails_safe_and_leaves_the_gate_on(doctor, patient, monkeypatch):
    """Only an explicit off-value turns a clinical safety check off. 'flase'
    must not read as 'false'."""
    monkeypatch.setenv("ORB_SURGERY_REQUIRE_CHECKLIST", "flase")
    sid = _mk_surgery(patient, doctor, status="in_progress")
    assert client.post(f"/api/surgery/{sid}/complete",
                       headers=_auth(doctor)).status_code == 409


def test_a_case_ticked_through_the_real_route_can_be_closed(doctor, patient):
    """Drives the actual POST /{id}/checklist endpoint for all 19 items, so the
    gate and the writer cannot disagree about what 'complete' means."""
    sid = _mk_surgery(patient, doctor, status="in_progress")
    for item_id in ALL_ITEM_IDS:
        resp = client.post(f"/api/surgery/{sid}/checklist",
                           json={"item_id": item_id, "checked": True},
                           headers=_auth(doctor))
        assert resp.status_code == 200, resp.text
    assert resp.json()["completed"] is True

    resp = client.post(f"/api/surgery/{sid}/complete", headers=_auth(doctor))
    assert resp.status_code == 200, resp.text


def test_unticking_an_item_re_closes_the_gate(doctor, patient):
    """set_item deletes the key rather than storing checked=False; the gate must
    read that as outstanding, not as a leftover attestation."""
    sid = _mk_surgery(patient, doctor, status="in_progress")
    _tick_all(sid, doctor)
    client.post(f"/api/surgery/{sid}/checklist",
                json={"item_id": "to_antibiotic", "checked": False},
                headers=_auth(doctor))
    resp = client.post(f"/api/surgery/{sid}/complete", headers=_auth(doctor))
    assert resp.status_code == 409
    assert "18/19" in resp.json()["detail"]


def test_a_checklist_refusal_records_the_progress_it_refused_on(doctor, patient):
    sid = _mk_surgery(patient, doctor, status="in_progress")
    _tick_all(sid, doctor, omit={"so_counts", "so_specimen"})
    client.post(f"/api/surgery/{sid}/complete", headers=_auth(doctor))

    meta = json.loads(_audit("surgery_completion_refused", sid)["metadata_json"])
    assert meta["refusal_reason"] == "checklist_incomplete"
    assert meta["checklist_progress"] == {"checked": 17, "total": 19}


# ══════════════════════════════════════════════════════════════════════════
#  TIMING — TURNAROUND BECOMES MEASURABLE
# ══════════════════════════════════════════════════════════════════════════

def test_the_surgeries_table_has_the_timing_columns(doctor, patient):
    """Migration 27. Asserted against the live schema, not the migration list."""
    with db_session() as db:
        cols = {r[1] for r in db.execute("PRAGMA table_info(surgeries)").fetchall()}
    assert {"started_at", "completed_at"} <= cols


def test_start_stamps_started_at(doctor, patient):
    sid = _mk_surgery(patient, doctor, status="scheduled")
    _assign(sid, doctor)
    before = datetime.now(timezone.utc)

    resp = client.post(f"/api/surgery/{sid}/start", headers=_auth(doctor))
    assert resp.status_code == 200, resp.text

    row = _row(sid)
    assert row["started_at"], "a started case with no start time is unmeasurable"
    assert row["completed_at"] is None
    stamped = datetime.fromisoformat(row["started_at"])
    assert before - timedelta(seconds=5) <= stamped <= datetime.now(timezone.utc) + timedelta(seconds=5)
    assert resp.json()["started_at"] == row["started_at"]


def test_an_idempotent_second_start_does_not_move_the_start_time(doctor, patient):
    """'already_in_progress' must keep the ORIGINAL knife-in time — a start time
    that the last button press can overwrite measures the button, not the case."""
    sid = _mk_surgery(patient, doctor, status="scheduled")
    _assign(sid, doctor)
    client.post(f"/api/surgery/{sid}/start", headers=_auth(doctor))
    first = _row(sid)["started_at"]

    again = client.post(f"/api/surgery/{sid}/start", headers=_auth(doctor))
    assert again.json()["status"] == "already_in_progress"
    assert _row(sid)["started_at"] == first


def test_complete_stamps_completed_at_and_reports_a_sane_turnaround(doctor, patient):
    """The number workflow 5 is sold on. A case that started 95 minutes ago and
    is closed now must report ~95 minutes, not None and not zero."""
    sid = _ready_case(patient, doctor)
    started = datetime.now(timezone.utc) - timedelta(minutes=95)
    with db_session() as db:
        db.execute("UPDATE surgeries SET started_at = ? WHERE id = ?",
                   (started.isoformat(), sid))

    resp = client.post(f"/api/surgery/{sid}/complete", headers=_auth(doctor))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert 94.5 <= body["turnaround_min"] <= 96.0, body

    row = _row(sid)
    assert row["completed_at"]
    assert datetime.fromisoformat(row["completed_at"]) > started

    # And the same figure is on the audit chain and on the detail read.
    meta = json.loads(_audit("surgery_completed", sid)["metadata_json"])
    assert meta["turnaround_min"] == body["turnaround_min"]
    detail = client.get(f"/api/surgery/{sid}", headers=_auth(doctor)).json()
    assert detail["turnaround_min"] == body["turnaround_min"]
    assert detail["started_at"] == row["started_at"]


def test_a_full_start_to_complete_round_trip_is_measurable(doctor, patient):
    """No hand-written timestamps anywhere: both ends come from the routes."""
    sid = _mk_surgery(patient, doctor, status="scheduled")
    _assign(sid, doctor)
    _tick_all(sid, doctor)

    assert client.post(f"/api/surgery/{sid}/start", headers=_auth(doctor)).status_code == 200
    resp = client.post(f"/api/surgery/{sid}/complete", headers=_auth(doctor))
    assert resp.status_code == 200, resp.text
    turnaround = resp.json()["turnaround_min"]
    assert turnaround is not None and turnaround >= 0


def test_a_legacy_case_with_no_start_time_reports_no_turnaround_not_zero(doctor, patient):
    """Rows that predate migration 27 have NULL started_at. The report must say
    'not measured' — a zero would quietly drag a ward's mean down."""
    sid = _ready_case(patient, doctor)
    with db_session() as db:
        db.execute("UPDATE surgeries SET started_at = NULL WHERE id = ?", (sid,))

    resp = client.post(f"/api/surgery/{sid}/complete", headers=_auth(doctor))
    assert resp.status_code == 200, resp.text
    assert resp.json()["turnaround_min"] is None


def test_the_post_op_note_compensation_also_clears_completed_at(doctor, patient, monkeypatch):
    """When the note write fails the status is reverted — the completion time has
    to go with it, or the case sits in a turnaround report with a completed_at
    and a status that disagree."""
    import routes.surgery_routes as sr

    sid = _ready_case(patient, doctor)
    real_save = sr.save_scribe_note

    def _boom(**kwargs):
        raise RuntimeError("scribe unavailable")

    monkeypatch.setattr(sr, "save_scribe_note", _boom)
    lenient = TestClient(app, raise_server_exceptions=False)
    resp = lenient.post(f"/api/surgery/{sid}/complete", headers=_auth(doctor))
    assert resp.status_code >= 500

    row = _row(sid)
    assert row["status"] == "in_progress", "the failed completion must roll back"
    assert row["completed_at"] is None, \
        "a case that is not completed must not carry a completion time"

    # And the retry that the compensation exists to make possible still works.
    # Restored by name rather than monkeypatch.undo(), which shares this test's
    # monkeypatch instance with the autouse env fixture and would put the
    # checklist gate back to whatever the ambient environment says.
    monkeypatch.setattr(sr, "save_scribe_note", real_save)
    assert client.post(f"/api/surgery/{sid}/complete",
                       headers=_auth(doctor)).status_code == 200


# ══════════════════════════════════════════════════════════════════════════
#  turnaround_minutes — the one derivation
# ══════════════════════════════════════════════════════════════════════════

def test_turnaround_minutes_is_none_when_it_is_not_known():
    from services.surgery_service import turnaround_minutes as tm
    now = datetime.now(timezone.utc).isoformat()
    assert tm(None, now) is None
    assert tm(now, None) is None
    assert tm("", "") is None
    assert tm("not a timestamp", now) is None
    # Naive vs aware: comparing them raises, and guessing a timezone would
    # invent a turnaround.
    assert tm("2026-08-14 09:00:00", now) is None


def test_turnaround_minutes_refuses_a_negative_interval():
    """completed before started is a corrupt pair, not a fast operation."""
    from services.surgery_service import turnaround_minutes as tm
    later = datetime(2026, 8, 14, 11, 0, tzinfo=timezone.utc).isoformat()
    earlier = datetime(2026, 8, 14, 9, 0, tzinfo=timezone.utc).isoformat()
    assert tm(later, earlier) is None


def test_turnaround_minutes_reads_the_shapes_this_column_actually_holds():
    from services.surgery_service import turnaround_minutes as tm
    start = datetime(2026, 8, 14, 9, 0, tzinfo=timezone.utc)
    end = start + timedelta(minutes=142, seconds=30)
    assert tm(start.isoformat(), end.isoformat()) == 142.5
    # 'Z' suffix, and the bare SQL TIMESTAMP shape a restore or a DEFAULT
    # CURRENT_TIMESTAMP would write.
    assert tm("2026-08-14T09:00:00Z", "2026-08-14T10:30:00Z") == 90.0
    assert tm("2026-08-14 09:00:00", "2026-08-14 09:45:00") == 45.0
    assert tm(start, end) == 142.5
