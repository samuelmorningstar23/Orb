"""Housekeeping & facilities — room turnaround, cleaning requests, linen.

WHAT THIS FILE IS DEFENDING, in order of how much it would cost to get wrong:

  1. "READY" MUST MEAN SOMEBODY CLEANED IT. The state machine's whole value is
     that `vacant_ready` cannot be reached from `vacant_dirty` — a clean has to be
     STARTED by a named person before it can be finished. If that jump is legal,
     the turnaround report loses its start timestamp, "ready" degrades to
     "somebody pressed a button", and a bed manager offers a bed that has a
     previous patient's linen on it. Both the LEGAL and the ILLEGAL moves are
     asserted, because a transition table that is only ever exercised on its happy
     path is a comment.

  2. THE TURNAROUND CLOCK STARTS WHEN THE PATIENT LEAVES, NOT WHEN SOMEBODY
     ASKS. `vacated_at → ready_at`, never `requested_at → completed_at`. The
     difference IS the finding: a bed that sat dirty for four hours before anyone
     raised a request and was then cleaned in thirty minutes must report four and
     a half hours, not thirty minutes. `test_turnaround_is_measured_from_when_the_
     bed_was_vacated_not_when_someone_asked` pins exactly that, with a two-hour
     gap that a requested_at-based implementation cannot survive.

  3. ONE DISCHARGE, ONE TASK. The sweep is a PULL — it re-reads recent discharges
     on every board load, because the clinical discharge path may not be touched
     to push at it — so the same discharge is offered to the inserter dozens of
     times a day. Idempotency is not a nicety here, it is the correctness
     condition, and it is enforced by a UNIQUE index rather than a
     SELECT-then-INSERT that two browser tabs will interleave. Prove-can-fail
     below drops the index and watches the duplicates appear.

  4. TERMINAL CLEANS CARRY A LONGER EXPECTATION. A ward with a heavy isolation
     load must not read as slow because the board expects a 45-minute standard
     clean of a 2-hour terminal one.

  5. CAPACITY IS NOT SILENTLY REDEFINED. `forecast_service`'s `available` means
     exactly what it meant before this module existed. Asserted directly, because
     "we didn't change it" is the kind of claim that stops being true six weeks
     after somebody writes it in a docstring.

  6. NO PHI ON A CORRIDOR TROLLEY. A cleaning task carries no patient id, name,
     MRN or diagnosis — only the boolean `clean_type`.

PROVEN ABLE TO FAIL. See the prove-can-fail block at the bottom of this file:
`test_PROVE_CAN_FAIL_without_the_unique_index_one_discharge_makes_many_tasks`
removes the real guard from the real database, runs the real sweep twice, and
asserts that the SAME helper the idempotency test uses reports the duplicate —
then restores the index and re-asserts the invariant holds again.
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import config
from auth import create_token
from database import db_session
from main import app
from services import housekeeping_service as hk

client = TestClient(app)

WARD = f"HK Ward {uuid.uuid4().hex[:6].upper()}"
WARD_B = f"HK Ward {uuid.uuid4().hex[:6].upper()}"


# ── fixtures ─────────────────────────────────────────────────────────────────

def _mk_user(role, *, department=None):
    uid = f"HKU{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO users (id, name, role, password_hash, department, is_active) "
            "VALUES (?, ?, ?, 'x', ?, 1)",
            (uid, f"Housekeeping {role}", role, department or WARD),
        )
    return uid


def _auth(user_id, role):
    return {"Authorization": f"Bearer {create_token(user_id, role)}"}


@pytest.fixture(scope="module")
def actors():
    return {
        "admin": (_mk_user("admin"), "admin"),
        "nurse": (_mk_user("nurse"), "nurse"),
        "pharmacist": (_mk_user("pharmacist"), "pharmacist"),
    }


@pytest.fixture(scope="module")
def admin_headers(actors):
    uid, role = actors["admin"]
    return _auth(uid, role)


def _unit(ward=WARD, *, state="vacant_dirty", isolation=False, bed=None, code=None):
    """A registered housekeeping unit. Distinct code per call so no two tests can
    collide in the shared session database."""
    suffix = uuid.uuid4().hex[:6].upper()
    return hk.register_unit(
        unit_code=code or f"HK-{suffix}",
        ward=ward,
        bed_label=bed or f"B{suffix}",
        is_isolation_room=isolation,
        initial_state=state,
        actor_user_id="HKFIXTURE",
    )


def _discharged_patient(*, ward, bed, isolation=None, discharged_at=None):
    """A patient who has been discharged, with the discharge RECORD the sweep
    actually reads (`discharge_summaries`) — not just a status column.

    Returns (patient_id, discharge_summary_id).
    """
    pid = f"HKP{uuid.uuid4().hex[:8].upper()}"
    aid = f"HKA{uuid.uuid4().hex[:8].upper()}"
    when = (discharged_at or datetime.now(timezone.utc)).isoformat()
    with db_session() as db:
        db.execute(
            "INSERT OR IGNORE INTO users (id, name, role, password_hash) "
            "VALUES ('HKADMITTER', 'HK Admitter', 'doctor', 'x')"
        )
        db.execute(
            """INSERT INTO patients (id, name, mrn, bed, ward, age, sex, diagnosis,
                                     status, admission_date)
               VALUES (?, ?, ?, ?, ?, 61, 'M', 'HK fixture', 'discharged', ?)""",
            (pid, f"HK Patient {pid[-4:]}", f"MRN-{uuid.uuid4().hex[:8].upper()}",
             bed, ward, when),
        )
        db.execute(
            """INSERT INTO admissions (id, patient_id, admitted_by, department, ward,
                                       bed, isolation_precautions, status, created_at)
               VALUES (?, ?, 'HKADMITTER', ?, ?, ?, ?, 'discharged', ?)""",
            (aid, pid, ward, ward, bed, isolation, when),
        )
        db.execute(
            """INSERT INTO discharge_summaries (patient_id, author_id, summary, discharged_at)
               VALUES (?, 'HKADMITTER', 'HK fixture summary', ?)""",
            (pid, when),
        )
        ds_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
    return pid, ds_id


def _tasks_for_discharge(ds_id):
    """THE INVARIANT HELPER. Every task the sweep created for one discharge.

    Both the idempotency test and the prove-can-fail test read the count through
    this one function, so the prove-can-fail is demonstrating that THIS
    assertion — not a similar-looking one — goes red.
    """
    with db_session() as db:
        return [
            dict(r) for r in db.execute(
                "SELECT id, unit_id, clean_type, expected_minutes, vacated_at, source_ref "
                "FROM housekeeping_tasks WHERE source_type = 'discharge' AND source_ref = ?",
                (str(ds_id),),
            ).fetchall()
        ]


# ═════════════════════════════════════════════════════════════════════════════
#  1. THE STATE MACHINE — the legal moves, and the illegal ones
# ═════════════════════════════════════════════════════════════════════════════

LEGAL = [
    ("occupied", "vacant_dirty"),
    ("occupied", "out_of_service"),
    ("vacant_dirty", "cleaning_in_progress"),
    ("vacant_dirty", "out_of_service"),
    ("cleaning_in_progress", "vacant_ready"),
    ("cleaning_in_progress", "vacant_dirty"),
    ("vacant_ready", "occupied"),
    ("vacant_ready", "vacant_dirty"),
    ("vacant_ready", "out_of_service"),
    ("out_of_service", "vacant_dirty"),
]

ILLEGAL = [
    # THE ONE THAT MATTERS. A bed cannot become ready without a recorded clean.
    ("vacant_dirty", "vacant_ready"),
    # An uncleaned bed cannot quietly acquire a patient through the ordinary path.
    ("vacant_dirty", "occupied"),
    ("occupied", "cleaning_in_progress"),
    ("occupied", "vacant_ready"),
    ("cleaning_in_progress", "occupied"),
    # Back from maintenance ⇒ needs a clean. An engineer and a toolbox were on it.
    ("out_of_service", "vacant_ready"),
    ("out_of_service", "occupied"),
    ("out_of_service", "cleaning_in_progress"),
]


@pytest.mark.parametrize("start,target", LEGAL)
def test_legal_transitions_are_allowed_and_ledgered(start, target):
    unit = _unit(state=start)
    moved = hk.transition_unit(
        unit["id"], target, actor_user_id="HKACTOR", actor_role="nurse",
        reason="parametrised legal move",
    )
    assert moved["state"] == target

    with db_session() as db:
        event = db.execute(
            "SELECT from_state, to_state, actor_user_id FROM housekeeping_unit_events "
            "WHERE unit_id = ? ORDER BY id DESC LIMIT 1",
            (unit["id"],),
        ).fetchone()
    # The ledger, not the cached column, is the record — a turnaround time is a
    # difference between two of these rows.
    assert (event["from_state"], event["to_state"]) == (start, target)
    assert event["actor_user_id"] == "HKACTOR"


@pytest.mark.parametrize("start,target", ILLEGAL)
def test_illegal_transitions_are_refused_and_change_nothing(start, target):
    unit = _unit(state=start)
    with pytest.raises(hk.HousekeepingError) as exc:
        hk.transition_unit(unit["id"], target, actor_user_id="HKACTOR")
    # Named in the board's words ("waiting to be cleaned"), never the enum.
    assert hk._state_words(target) in str(exc.value)
    assert "_" not in str(exc.value).replace(unit["unit_code"], "")

    # A refusal must be TOTAL: no state change and no ledger row. A machine that
    # refuses the move but records the attempt as if it happened is worse than
    # one that allows it, because the report then disagrees with the bed.
    after = hk.get_unit(unit["id"])
    assert after["state"] == start
    with db_session() as db:
        n = db.execute(
            "SELECT COUNT(*) AS n FROM housekeeping_unit_events "
            "WHERE unit_id = ? AND to_state = ?", (unit["id"], target),
        ).fetchone()["n"]
    assert n == 0


def test_a_dirty_bed_cannot_be_declared_ready_through_the_api_either():
    """The same refusal, through the endpoint a person actually presses."""
    unit = _unit(state="vacant_dirty")
    uid = _mk_user("nurse")
    resp = client.post(
        f"/api/housekeeping/units/{unit['id']}/state",
        json={"state": "vacant_ready", "reason": "looks fine to me"},
        headers=_auth(uid, "nurse"),
    )
    assert resp.status_code == 422, resp.text
    assert hk.get_unit(unit["id"])["state"] == "vacant_dirty"


def test_the_full_turnaround_cycle_walks_the_machine():
    unit = _unit(state="occupied")
    task = hk.raise_cleaning_request(
        unit_id=unit["id"], request_type="discharge_turnaround",
        priority="routine", actor_user_id="HKACTOR",
    )
    assert hk.get_unit(unit["id"])["state"] == "vacant_dirty"
    assert task["status"] == "open"

    hk.start_cleaning(task["id"], actor_user_id="HKCLEANER")
    assert hk.get_unit(unit["id"])["state"] == "cleaning_in_progress"

    done = hk.complete_cleaning(task["id"], actor_user_id="HKCLEANER")
    assert done["status"] == "done"
    assert hk.get_unit(unit["id"])["state"] == "vacant_ready"
    assert done["ready_at"] is not None


def test_a_clean_must_be_started_before_it_can_be_completed():
    """The task-level twin of vacant_dirty → vacant_ready. Both doors are shut,
    because closing only one leaves the other open."""
    unit = _unit(state="vacant_dirty")
    task = hk.raise_cleaning_request(unit_id=unit["id"], actor_user_id="HKACTOR")
    with pytest.raises(hk.HousekeepingError):
        hk.complete_cleaning(task["id"], actor_user_id="HKCLEANER")
    assert hk.get_unit(unit["id"])["state"] == "vacant_dirty"


def test_abandoning_a_clean_reopens_its_task():
    unit = _unit(state="vacant_dirty")
    task = hk.raise_cleaning_request(unit_id=unit["id"], actor_user_id="HKACTOR")
    hk.start_cleaning(task["id"], actor_user_id="HKCLEANER")
    hk.transition_unit(unit["id"], "vacant_dirty", reason="cleaner pulled away")
    reopened = hk.get_task(task["id"])
    assert reopened["status"] == "open" and reopened["started_at"] is None


def test_occupying_an_uncleaned_bed_is_recordable_and_says_so():
    """PILOT SAFETY. Orb does not stand in the doorway; it keeps an honest record.

    A hospital at capacity at 3am will sometimes put a patient into a bed that
    was not cleaned. Refusing to record that does not prevent it — it just means
    the only trace is a bed stuck at 'vacant_dirty' with a patient in it.
    """
    unit = _unit(state="vacant_dirty")
    task = hk.raise_cleaning_request(unit_id=unit["id"], actor_user_id="HKACTOR")

    out = hk.force_occupied(unit["id"], reason="No other bed; ward at capacity",
                            actor_user_id="HKBEDMGR", actor_role="admin")
    assert out["state"] == "occupied" and out["clean_was_skipped"] is True

    with db_session() as db:
        ev = db.execute(
            "SELECT from_state, to_state, reason FROM housekeeping_unit_events "
            "WHERE unit_id = ? ORDER BY id DESC LIMIT 1", (unit["id"],),
        ).fetchone()
    assert ev["from_state"] == "vacant_dirty" and ev["to_state"] == "occupied"
    assert "OCCUPIED WITHOUT COMPLETED CLEAN" in ev["reason"].upper()

    # The orphaned request is closed with the reason, not left to rot in the queue.
    assert hk.get_task(task["id"])["status"] == "cancelled"


def test_occupying_a_ready_bed_is_not_flagged_as_a_skipped_clean():
    unit = _unit(state="vacant_ready")
    out = hk.force_occupied(unit["id"], reason="admission", actor_user_id="HKBEDMGR")
    assert out["state"] == "occupied" and out["clean_was_skipped"] is False


# ═════════════════════════════════════════════════════════════════════════════
#  2. PRIORITY — a spillage and a discharge are not the same job
# ═════════════════════════════════════════════════════════════════════════════

def test_the_queue_puts_an_immediate_spillage_ahead_of_older_routine_work():
    ward = f"HK Queue {uuid.uuid4().hex[:6].upper()}"
    older = []
    for _ in range(3):
        u = _unit(ward, state="occupied")
        older.append(hk.raise_cleaning_request(
            unit_id=u["id"], request_type="discharge_turnaround",
            priority="routine", actor_user_id="HKACTOR")["id"])

    spill_unit = _unit(ward, state="vacant_ready")
    spill = hk.raise_cleaning_request(
        unit_id=spill_unit["id"], request_type="spillage", priority="immediate",
        description="Blood spillage in the bay", actor_user_id="HKACTOR",
    )

    queue = [t["id"] for t in hk.list_tasks(ward=ward, status="open")]
    assert queue[0] == spill["id"], (
        "an immediate spillage raised last must still be worked first — a board "
        "sorted purely by age buries it behind every routine turnaround"
    )
    assert set(older).issubset(set(queue))


def test_a_spillage_on_a_made_bed_dirties_it():
    unit = _unit(state="vacant_ready")
    hk.raise_cleaning_request(unit_id=unit["id"], request_type="spillage",
                              priority="urgent", actor_user_id="HKACTOR")
    assert hk.get_unit(unit["id"])["state"] == "vacant_dirty"


def test_a_bed_back_from_maintenance_needs_a_clean_before_it_is_anyones_bed():
    unit = _unit(state="out_of_service")
    # Anything but a post-maintenance request is refused while it is out of service.
    with pytest.raises(hk.HousekeepingError):
        hk.raise_cleaning_request(unit_id=unit["id"], request_type="routine",
                                  actor_user_id="HKACTOR")
    task = hk.raise_cleaning_request(unit_id=unit["id"], request_type="post_maintenance",
                                     actor_user_id="HKACTOR")
    assert hk.get_unit(unit["id"])["state"] == "vacant_dirty"
    assert task["status"] == "open"


def test_a_request_on_an_already_dirty_bed_keeps_the_clock_it_already_started():
    """The turnaround clock started when the bed became dirty. Raising the
    request an hour later must not restart it — that hour IS the finding."""
    unit = _unit(state="occupied")
    first = hk.raise_cleaning_request(unit_id=unit["id"],
                                      request_type="discharge_turnaround",
                                      actor_user_id="HKACTOR")
    second = hk.raise_cleaning_request(unit_id=unit["id"], request_type="spillage",
                                       priority="urgent", actor_user_id="HKACTOR")
    assert second["vacated_at"] == first["vacated_at"], (
        "a second request on an already-dirty bed must inherit the moment the bed "
        "was vacated, not stamp its own"
    )


def test_only_a_turnaround_may_be_raised_on_an_occupied_unit():
    unit = _unit(state="occupied")
    with pytest.raises(hk.HousekeepingError):
        hk.raise_cleaning_request(unit_id=unit["id"], request_type="deep_clean",
                                  actor_user_id="HKACTOR")
    assert hk.get_unit(unit["id"])["state"] == "occupied"


# ═════════════════════════════════════════════════════════════════════════════
#  3. TURNAROUND TIME — measured between the RIGHT two timestamps
# ═════════════════════════════════════════════════════════════════════════════

def test_turnaround_is_measured_from_when_the_bed_was_vacated_not_when_someone_asked():
    """THE REPORTING TEST THAT MATTERS.

    A bed is vacated at 09:00. Nobody raises a request until 11:00. A cleaner
    starts at 11:05 and finishes at 11:35.

      * requested_at → completed_at  =  35 minutes   ← flattering and useless
      * started_at   → completed_at  =  30 minutes   ← "how fast do cleaners work"
      * vacated_at   → ready_at      = 155 minutes   ← the bed was out of action

    The two-hour queue delay is the entire finding, and it is invisible to the
    first two measurements. This test fixes the timestamps directly so that an
    implementation measuring the wrong pair cannot pass by coincidence.
    """
    ward = f"HK Clock {uuid.uuid4().hex[:6].upper()}"
    unit = _unit(ward, state="vacant_dirty")
    task = hk.raise_cleaning_request(unit_id=unit["id"], actor_user_id="HKACTOR")

    base = datetime.now(timezone.utc) - timedelta(hours=6)
    vacated = base
    requested = base + timedelta(hours=2)
    started = base + timedelta(hours=2, minutes=5)
    completed = base + timedelta(hours=2, minutes=35)

    with db_session() as db:
        db.execute(
            "UPDATE housekeeping_tasks SET vacated_at = ?, requested_at = ?, started_at = ?, "
            "completed_at = ?, ready_at = ?, status = 'done' WHERE id = ?",
            (vacated.isoformat(), requested.isoformat(), started.isoformat(),
             completed.isoformat(), completed.isoformat(), task["id"]),
        )

    loaded = hk.get_task(task["id"])
    assert loaded["turnaround_minutes"] == pytest.approx(155.0, abs=0.2), (
        "turnaround must be ready_at − vacated_at (155 min), not requested_at → "
        f"completed_at (35 min) nor started_at → completed_at (30 min); got "
        f"{loaded['turnaround_minutes']}"
    )
    assert loaded["cleaning_minutes"] == pytest.approx(30.0, abs=0.2)

    report = hk.ward_turnaround_report(ward=ward, days=2)
    row = next(w for w in report["wards"] if w["ward"] == ward)
    assert row["median_turnaround_minutes"] == pytest.approx(155.0, abs=0.2)
    assert row["median_cleaning_minutes"] == pytest.approx(30.0, abs=0.2)
    assert report["measured_between"] == "vacated_at → ready_at"


def test_a_task_whose_timestamps_are_out_of_order_is_reported_not_hidden():
    """A completed task the report cannot measure is COUNTED as unmeasurable.

    Silently dropping the awkward rows is how a median gets to look good.
    """
    ward = f"HK Skew {uuid.uuid4().hex[:6].upper()}"
    unit = _unit(ward, state="vacant_dirty")
    task = hk.raise_cleaning_request(unit_id=unit["id"], actor_user_id="HKACTOR")
    now = datetime.now(timezone.utc)
    with db_session() as db:
        db.execute(
            "UPDATE housekeeping_tasks SET vacated_at = ?, ready_at = ?, status = 'done' "
            "WHERE id = ?",
            (now.isoformat(), (now - timedelta(hours=1)).isoformat(), task["id"]),
        )
    row = next(w for w in hk.ward_turnaround_report(ward=ward, days=2)["wards"]
               if w["ward"] == ward)
    assert row["completed"] == 1 and row["measured"] == 0 and row["unmeasurable"] == 1


def test_an_open_task_reports_elapsed_time_and_goes_overdue():
    ward = f"HK Overdue {uuid.uuid4().hex[:6].upper()}"
    unit = _unit(ward, state="vacant_dirty")
    task = hk.raise_cleaning_request(unit_id=unit["id"], actor_user_id="HKACTOR")
    long_ago = datetime.now(timezone.utc) - timedelta(minutes=300)
    with db_session() as db:
        db.execute("UPDATE housekeeping_tasks SET vacated_at = ? WHERE id = ?",
                   (long_ago.isoformat(), task["id"]))
    loaded = hk.get_task(task["id"])
    assert loaded["elapsed_minutes"] == pytest.approx(300, abs=1)
    assert loaded["is_overdue"] is True
    assert loaded["minutes_over_expected"] == pytest.approx(
        300 - hk.expected_minutes_for("standard"), abs=1)


# ═════════════════════════════════════════════════════════════════════════════
#  4. TERMINAL / ISOLATION CLEANS carry a LONGER expectation
# ═════════════════════════════════════════════════════════════════════════════

def test_a_terminal_clean_carries_the_longer_expectation():
    assert hk.expected_minutes_for("terminal") > hk.expected_minutes_for("standard"), (
        "a terminal clean that is expected to take the same time as wiping down a "
        "bed space makes every isolation room read as late"
    )
    unit = _unit(state="vacant_dirty")
    std = hk.raise_cleaning_request(unit_id=unit["id"], clean_type="standard",
                                    actor_user_id="HKACTOR")
    unit2 = _unit(state="vacant_dirty")
    term = hk.raise_cleaning_request(unit_id=unit2["id"], clean_type="terminal",
                                     actor_user_id="HKACTOR")
    assert std["expected_minutes"] == hk.expected_minutes_for("standard")
    assert term["expected_minutes"] == hk.expected_minutes_for("terminal")
    assert term["is_terminal_clean"] is True and std["is_terminal_clean"] is False


def test_the_expectation_is_frozen_on_the_task_when_it_is_raised():
    """`expected_minutes` is STORED, not recomputed at read time. A site that
    retunes its SLA next month must not retroactively rewrite whether last
    month's cleans were late."""
    unit = _unit(state="vacant_dirty")
    task = hk.raise_cleaning_request(unit_id=unit["id"], clean_type="terminal",
                                     actor_user_id="HKACTOR")
    original = task["expected_minutes"]
    old = config.HOUSEKEEPING_TERMINAL_CLEAN_MINUTES
    try:
        config.HOUSEKEEPING_TERMINAL_CLEAN_MINUTES = old + 999
        assert hk.get_task(task["id"])["expected_minutes"] == original
    finally:
        config.HOUSEKEEPING_TERMINAL_CLEAN_MINUTES = old


def test_a_discharge_from_an_isolation_bed_raises_a_terminal_clean():
    ward = f"HK Iso {uuid.uuid4().hex[:6].upper()}"
    unit = _unit(ward, state="occupied", bed="ISO-1")
    _, ds_id = _discharged_patient(ward=ward, bed="ISO-1",
                                   isolation="Contact precautions — MRSA")
    hk.sync_discharge_turnarounds(lookback_hours=24)

    tasks = _tasks_for_discharge(ds_id)
    assert len(tasks) == 1
    assert tasks[0]["clean_type"] == "terminal"
    assert tasks[0]["expected_minutes"] == hk.expected_minutes_for("terminal")
    assert tasks[0]["unit_id"] == unit["id"]


def test_a_terminal_clean_never_copies_the_precaution_text_out_of_the_chart():
    """THE PHI BOUNDARY. The housekeeper learns 'terminal clean'. They do not
    learn the organism."""
    ward = f"HK IsoText {uuid.uuid4().hex[:6].upper()}"
    _unit(ward, state="occupied", bed="ISO-9")
    _, ds_id = _discharged_patient(ward=ward, bed="ISO-9",
                                   isolation="Airborne precautions — pulmonary TB")
    hk.sync_discharge_turnarounds(lookback_hours=24)

    with db_session() as db:
        row = db.execute(
            "SELECT precaution_note, description FROM housekeeping_tasks "
            "WHERE source_type = 'discharge' AND source_ref = ?", (str(ds_id),),
        ).fetchone()
    blob = f"{row['precaution_note']} {row['description']}".lower()
    assert "tb" not in blob.split() and "tuberculosis" not in blob
    assert "airborne" not in blob
    assert "terminal clean" in blob


# ═════════════════════════════════════════════════════════════════════════════
#  5. THE DISCHARGE SWEEP — exactly one task per discharge
# ═════════════════════════════════════════════════════════════════════════════

def test_a_discharge_creates_a_turnaround_task_exactly_once():
    """Run the sweep five times. One discharge, one task, every time.

    The sweep is a PULL: it re-reads recent discharges on every board load,
    because the clinical discharge path may not be touched to push at it. So this
    is not a "what if someone double-clicks" test — the duplicate insert is
    attempted on every single page view.
    """
    ward = f"HK Once {uuid.uuid4().hex[:6].upper()}"
    unit = _unit(ward, state="occupied", bed="7A")
    _, ds_id = _discharged_patient(ward=ward, bed="7A")

    for _ in range(5):
        hk.sync_discharge_turnarounds(lookback_hours=24)

    tasks = _tasks_for_discharge(ds_id)
    assert len(tasks) == 1, f"one discharge produced {len(tasks)} turnaround tasks"
    assert tasks[0]["unit_id"] == unit["id"]
    # And the bed was vacated exactly once, not five times.
    with db_session() as db:
        vacations = db.execute(
            "SELECT COUNT(*) AS n FROM housekeeping_unit_events "
            "WHERE unit_id = ? AND to_state = 'vacant_dirty'", (unit["id"],),
        ).fetchone()["n"]
    assert vacations == 1


def test_the_sweep_reports_created_versus_already_present():
    ward = f"HK Report {uuid.uuid4().hex[:6].upper()}"
    _unit(ward, state="occupied", bed="3B")
    _discharged_patient(ward=ward, bed="3B")

    first = hk.sync_discharge_turnarounds(lookback_hours=24)
    second = hk.sync_discharge_turnarounds(lookback_hours=24)
    assert first["created"] >= 1
    assert second["created"] == 0 or second["created"] < first["created"]
    assert second["already_present"] >= 1


def test_the_turnaround_clock_starts_when_the_patient_left_not_when_the_sweep_ran():
    """A discharge at 09:00 swept at 14:00 must record vacated_at = 09:00.

    A sweep that stamped its own run time would report a five-hour turnaround as
    zero, and the module's headline number would be a measurement of how often
    somebody opens the board.
    """
    ward = f"HK Late {uuid.uuid4().hex[:6].upper()}"
    _unit(ward, state="occupied", bed="5C")
    left_at = datetime.now(timezone.utc) - timedelta(hours=5)
    _, ds_id = _discharged_patient(ward=ward, bed="5C", discharged_at=left_at)

    hk.sync_discharge_turnarounds(lookback_hours=24)
    task = _tasks_for_discharge(ds_id)[0]
    recorded = datetime.fromisoformat(task["vacated_at"])
    assert abs((recorded - left_at).total_seconds()) < 5


def test_a_discharge_from_an_unregistered_bed_is_counted_not_silently_dropped():
    """A housekeeping module that quietly ignores two thirds of a hospital's
    discharges looks exactly like one that is working."""
    ward = f"HK Unreg {uuid.uuid4().hex[:6].upper()}"
    _discharged_patient(ward=ward, bed="NOT-REGISTERED")
    result = hk.sync_discharge_turnarounds(lookback_hours=24)
    assert result["unmatched"] >= 1
    assert ward in result["unmatched_wards"]


def test_a_readmitted_and_rediscarged_patient_gets_a_second_task():
    """Idempotency keys the DISCHARGE EVENT, not the patient. Keying the patient
    would mean a patient's second stay never got its bed cleaned."""
    ward = f"HK Again {uuid.uuid4().hex[:6].upper()}"
    _unit(ward, state="occupied", bed="9D")
    _, first = _discharged_patient(ward=ward, bed="9D")
    hk.sync_discharge_turnarounds(lookback_hours=24)
    _, second = _discharged_patient(ward=ward, bed="9D")
    hk.sync_discharge_turnarounds(lookback_hours=24)

    assert first != second
    assert len(_tasks_for_discharge(first)) == 1
    assert len(_tasks_for_discharge(second)) == 1


def test_a_discharge_older_than_the_lookback_window_is_not_swept():
    ward = f"HK Old {uuid.uuid4().hex[:6].upper()}"
    _unit(ward, state="occupied", bed="OLD-1")
    _, ds_id = _discharged_patient(
        ward=ward, bed="OLD-1",
        discharged_at=datetime.now(timezone.utc) - timedelta(days=9),
    )
    hk.sync_discharge_turnarounds(lookback_hours=24)
    assert _tasks_for_discharge(ds_id) == []


# ═════════════════════════════════════════════════════════════════════════════
#  6. CAPACITY — separate, and honestly labelled
# ═════════════════════════════════════════════════════════════════════════════

def test_housekeeping_never_reports_a_number_called_available():
    """`available` is the capacity screen's word and it means something specific
    there. Housekeeping uses its own vocabulary so the two cannot be confused by
    a reader skimming a dashboard."""
    readiness = hk.ward_readiness()
    for w in readiness["wards"]:
        assert "available" not in w or w.get("capacity_view_available") is not None
        assert set(["registered_units", "ward_capacity", "coverage_pct",
                    "vacant_ready", "not_ready_registered"]).issubset(w.keys())


def test_readiness_always_publishes_its_own_coverage():
    """`ready = 3` on a twelve-bed ward with three registered beds is not "three
    beds free". The coverage figure is what stops it being read that way, so it
    is never optional."""
    ward = f"HK Cover {uuid.uuid4().hex[:6].upper()}"
    _unit(ward, state="vacant_ready")
    _unit(ward, state="vacant_dirty")
    _unit(ward, state="cleaning_in_progress")
    _unit(ward, state="out_of_service")

    row = next(w for w in hk.ward_readiness(ward=ward)["wards"] if w["ward"] == ward)
    assert row["registered_units"] == 4
    assert row["vacant_ready"] == 1
    assert row["not_ready_registered"] == 3
    assert "coverage_pct" in row and "ward_capacity" in row
    assert "capacity_view_available" in row


def test_housekeeping_does_not_change_what_the_capacity_screen_reports():
    """THE ONE THAT PROTECTS SOMEBODY ELSE'S NUMBER.

    Registering four beds and marking three of them unusable must leave
    forecast_service's bed-capacity answer for that ward exactly where it was.
    That number is on a screen a bed manager already acts on; changing what it
    means from a facilities module would be a silent redefinition.
    """
    from services import forecast_service as fc

    before = fc.get_bed_capacity_forecast()
    ward = f"HK Sep {uuid.uuid4().hex[:6].upper()}"
    for state in ("vacant_dirty", "cleaning_in_progress", "out_of_service", "vacant_ready"):
        _unit(ward, state=state)
    after = fc.get_bed_capacity_forecast()

    assert after["total"]["capacity"] == before["total"]["capacity"]
    assert after["total"]["available"] == before["total"]["available"]
    assert set(after["current"]) == set(before["current"])
    for w in before["current"]:
        assert after["current"][w] == before["current"][w]


def test_the_readiness_payload_states_the_capacity_relationship_in_words():
    r = hk.ward_readiness()
    assert "do NOT subtract beds awaiting cleaning" in r["capacity_basis"]
    # In words a bed manager reads: it used to name its own JSON fields.
    assert "registered here" in r["reconciliation_note"]
    assert "_" not in r["reconciliation_note"]


# ═════════════════════════════════════════════════════════════════════════════
#  7. LINEN
# ═════════════════════════════════════════════════════════════════════════════

def test_linen_outstanding_is_issued_minus_every_kind_of_return():
    ward = f"HK Linen {uuid.uuid4().hex[:6].upper()}"
    hk.record_linen_movement(ward=ward, item_code="BEDSHEET", direction="issue", quantity=100)
    hk.record_linen_movement(ward=ward, item_code="BEDSHEET", direction="return_soiled", quantity=70)
    hk.record_linen_movement(ward=ward, item_code="BEDSHEET", direction="return_clean", quantity=10)
    hk.record_linen_movement(ward=ward, item_code="BEDSHEET", direction="condemned", quantity=5)

    line = next(l for l in hk.linen_summary(ward=ward)["lines"] if l["item_code"] == "BEDSHEET")
    assert (line["issued"], line["returned_soiled"], line["returned_clean"],
            line["condemned"]) == (100, 70, 10, 5)
    assert line["outstanding"] == 15, (
        "outstanding must be issued − soiled − clean − condemned; collapsing the "
        "three return kinds into one loses the number a linen room argues about"
    )


def test_a_negative_linen_balance_is_reported_not_clamped():
    """A ward returning stock issued before the window opened. Clamping it to
    zero turns 'your window is too short' into 'your linen reconciles'."""
    ward = f"HK LinenNeg {uuid.uuid4().hex[:6].upper()}"
    hk.record_linen_movement(ward=ward, item_code="TOWEL", direction="return_soiled", quantity=12)
    line = next(l for l in hk.linen_summary(ward=ward)["lines"] if l["item_code"] == "TOWEL")
    assert line["outstanding"] == -12


def test_linen_movements_are_idempotent_on_a_key():
    ward = f"HK LinenIdem {uuid.uuid4().hex[:6].upper()}"
    key = f"KEY-{uuid.uuid4().hex[:10]}"
    hk.record_linen_movement(ward=ward, item_code="BLANKET", direction="issue",
                             quantity=20, idempotency_key=key)
    again = hk.record_linen_movement(ward=ward, item_code="BLANKET", direction="issue",
                                     quantity=20, idempotency_key=key)
    assert again.get("duplicate_ignored") is True
    line = next(l for l in hk.linen_summary(ward=ward)["lines"] if l["item_code"] == "BLANKET")
    assert line["issued"] == 20


def test_an_unknown_linen_item_is_refused():
    with pytest.raises(hk.HousekeepingError):
        hk.record_linen_movement(ward=WARD, item_code="INVENTED-THING",
                                 direction="issue", quantity=1)


@pytest.mark.parametrize("qty", [0, -5])
def test_a_non_positive_linen_quantity_is_refused(qty):
    with pytest.raises(hk.HousekeepingError):
        hk.record_linen_movement(ward=WARD, item_code="TOWEL", direction="issue", quantity=qty)


# ═════════════════════════════════════════════════════════════════════════════
#  8. THE API SURFACE — permissions, the module switch, and PHI
# ═════════════════════════════════════════════════════════════════════════════

def test_the_board_loads_and_carries_the_capacity_caveat(admin_headers):
    resp = client.get("/api/housekeeping/overview", headers=admin_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "readiness" in body and "capacity_basis" in body["readiness"]
    assert set(body["counts"]) >= {"open", "in_progress", "overdue", "terminal_open"}


def test_the_transition_table_is_published_rather_than_reimplemented(admin_headers):
    """The panel reads the legal moves from the server. A UI that hard-codes them
    grows a second copy of the state machine, and the copy that is wrong is
    always the one the user can see."""
    meta = client.get("/api/housekeeping/meta", headers=admin_headers).json()
    assert meta["legal_transitions"]["vacant_dirty"] == ["cleaning_in_progress", "out_of_service"]
    assert "vacant_ready" not in meta["legal_transitions"]["vacant_dirty"]
    assert meta["expected_minutes"]["terminal"] > meta["expected_minutes"]["standard"]


def test_a_pharmacist_can_neither_read_nor_write_housekeeping(actors):
    uid, role = actors["pharmacist"]
    headers = _auth(uid, role)
    assert client.get("/api/housekeeping/overview", headers=headers).status_code == 403
    assert client.post("/api/housekeeping/units", headers=headers,
                       json={"unit_code": "X", "ward": WARD}).status_code == 403


def test_a_nurse_may_mark_a_bed_dirty(actors):
    uid, role = actors["nurse"]
    unit = _unit(state="occupied")
    resp = client.post(
        "/api/housekeeping/tasks",
        json={"unit_id": unit["id"], "request_type": "discharge_turnaround",
              "priority": "urgent", "description": "Patient has gone home"},
        headers=_auth(uid, role),
    )
    assert resp.status_code == 200, resp.text
    assert hk.get_unit(unit["id"])["state"] == "vacant_dirty"


def test_every_route_404s_when_the_module_is_switched_off(admin_headers):
    original = config.HOUSEKEEPING_ENABLED
    try:
        config.HOUSEKEEPING_ENABLED = False
        for path in ("/api/housekeeping/overview", "/api/housekeeping/meta",
                     "/api/housekeeping/units", "/api/housekeeping/tasks",
                     "/api/housekeeping/turnaround", "/api/housekeeping/readiness",
                     "/api/housekeeping/linen"):
            resp = client.get(path, headers=admin_headers)
            assert resp.status_code == 404, f"{path} → {resp.status_code}"
        resp = client.post("/api/housekeeping/units", headers=admin_headers,
                           json={"unit_code": "X", "ward": WARD})
        assert resp.status_code == 404
    finally:
        config.HOUSEKEEPING_ENABLED = original


def test_health_publishes_the_housekeeping_module_flag():
    modules = client.get("/api/health").json().get("modules", {})
    assert "housekeeping" in modules, (
        "the shell decides whether to render the tab from this flag; a module the "
        "server runs but does not publish is a tab that never appears"
    )


def test_no_cleaning_task_payload_carries_a_patient_identifier(admin_headers):
    """A housekeeping board is read on a corridor trolley by staff with no
    clinical grant. The task rows must not become a chart nobody is auditing."""
    ward = f"HK Phi {uuid.uuid4().hex[:6].upper()}"
    _unit(ward, state="occupied", bed="PHI-1")
    pid, ds_id = _discharged_patient(ward=ward, bed="PHI-1",
                                     isolation="Contact precautions — CRE")
    hk.sync_discharge_turnarounds(lookback_hours=24)

    tasks = client.get(f"/api/housekeeping/tasks?ward={ward}", headers=admin_headers).json()["tasks"]
    assert tasks, "fixture did not produce a task"
    blob = repr(tasks)
    assert pid not in blob
    assert "source_ref" not in blob
    assert "CRE" not in blob
    for t in tasks:
        assert "patient_id" not in t and "mrn" not in t and "diagnosis" not in t

    detail = client.get(f"/api/housekeeping/tasks/{tasks[0]['id']}", headers=admin_headers).json()
    assert pid not in repr(detail) and "source_ref" not in repr(detail)


def test_the_api_walks_a_whole_turnaround(admin_headers):
    ward = f"HK E2E {uuid.uuid4().hex[:6].upper()}"
    unit = client.post("/api/housekeeping/units", headers=admin_headers, json={
        "unit_code": f"E2E-{uuid.uuid4().hex[:6].upper()}", "ward": ward,
        "bed_label": "1", "initial_state": "occupied",
    }).json()

    task = client.post("/api/housekeeping/tasks", headers=admin_headers, json={
        "unit_id": unit["id"], "request_type": "discharge_turnaround", "priority": "routine",
    }).json()
    assert client.post(f"/api/housekeeping/tasks/{task['id']}/start",
                       headers=admin_headers).status_code == 200
    done = client.post(f"/api/housekeeping/tasks/{task['id']}/complete",
                       headers=admin_headers, json={"note": "terminal wipe done"}).json()
    assert done["status"] == "done"

    readiness = client.get(f"/api/housekeeping/readiness?ward={ward}",
                           headers=admin_headers).json()
    row = next(w for w in readiness["wards"] if w["ward"] == ward)
    assert row["vacant_ready"] == 1 and row["not_ready_registered"] == 0


def test_registering_an_existing_unit_again_does_not_reset_its_state():
    """A ward re-import must not silently mark every bed dirty and orphan the
    open tasks."""
    code = f"HK-REREG-{uuid.uuid4().hex[:6].upper()}"
    unit = _unit(code=code, state="vacant_ready")
    again = hk.register_unit(unit_code=code, ward=WARD_B, bed_label="renamed")
    assert again["id"] == unit["id"]
    assert again["state"] == "vacant_ready"
    assert again["ward"] == WARD_B


def test_a_newly_registered_unit_is_not_claimed_to_be_clean():
    """NOT-CLEANED IS NOT READY. Registering a bed is inventory, not cleaning.

    If registration defaulted to 'vacant_ready', importing a bed list on a
    Tuesday afternoon would publish "48 beds ready" without one person having
    walked a ward — and the first thing anybody would do with that number is
    offer a bed.
    """
    unit = hk.register_unit(unit_code=f"HK-DEF-{uuid.uuid4().hex[:6].upper()}", ward=WARD_B)
    assert unit["state"] == "vacant_dirty"


# ═════════════════════════════════════════════════════════════════════════════
#  PROVE-CAN-FAIL
# ═════════════════════════════════════════════════════════════════════════════

def test_PROVE_CAN_FAIL_without_the_unique_index_one_discharge_makes_many_tasks():
    """Proof that `test_a_discharge_creates_a_turnaround_task_exactly_once` is
    load-bearing rather than decorative.

    The idempotency guard is ONE THING: the partial unique index
    `idx_hk_tasks_source ON housekeeping_tasks(source_type, source_ref)`, with
    `sync_discharge_turnarounds` catching `DBIntegrityError` as "already done".
    There is no application-level "have I seen this discharge?" check, on purpose
    — a SELECT-then-INSERT is exactly what two browser tabs interleave.

    So this test removes the real index from the real database, runs the real
    sweep, and asserts that `_tasks_for_discharge` — the SAME helper the
    idempotency test asserts on — now reports duplicates. Then it restores the
    index and re-asserts that the invariant holds again, so the file cannot leave
    the schema damaged for the tests that follow.

    WATCHED GO RED, 2026-08-12. `test_a_discharge_creates_a_turnaround_task_
    exactly_once` was run verbatim with `idx_hk_tasks_source` dropped and failed
    with `one discharge produced 5 turnaround tasks` — one per sweep, exactly as
    predicted. Restored, it passes.

    NOTE ON `ensure_housekeeping_schema()` BELOW, which is not decoration: the
    service rebuilds its indexes lazily on first use per process, so a DROP
    issued before any housekeeping call is silently undone by the next one, and
    this test would then "pass" while proving nothing. Priming the schema cache
    first is what makes the drop stick.
    """
    hk.ensure_housekeeping_schema()
    ward = f"HK Prove {uuid.uuid4().hex[:6].upper()}"
    _unit(ward, state="occupied", bed="P-1")
    _, ds_id = _discharged_patient(ward=ward, bed="P-1")

    try:
        with db_session() as db:
            db.execute("DROP INDEX IF EXISTS idx_hk_tasks_source")

        for _ in range(3):
            hk.sync_discharge_turnarounds(lookback_hours=24)

        duplicated = _tasks_for_discharge(ds_id)
        assert len(duplicated) > 1, (
            "with the unique index dropped the sweep should have produced a task "
            f"per run, but produced {len(duplicated)} — which would mean the "
            "idempotency test passes for some OTHER reason and is not actually "
            "guarding the index"
        )
    finally:
        # Clear the mess this test deliberately made, THEN restore the guard.
        #
        # The mess is wider than this one discharge, and that is itself worth
        # knowing: with the index gone the sweep duplicated EVERY discharge in
        # the lookback window, including the fixtures other tests in this module
        # created. So the cleanup dedupes globally — keep the lowest rowid per
        # (source_type, source_ref) — because a partial cleanup would leave
        # CREATE UNIQUE INDEX failing and take the rest of the suite with it.
        with db_session() as db:
            db.execute(
                """DELETE FROM housekeeping_tasks
                    WHERE source_ref IS NOT NULL
                      AND rowid NOT IN (
                          SELECT MIN(rowid) FROM housekeeping_tasks
                           WHERE source_ref IS NOT NULL
                           GROUP BY source_type, source_ref)"""
            )
            for statement in hk.HOUSEKEEPING_INDEXES:
                db.execute(statement)

    # And with the guard back, the invariant holds again — including for this
    # very discharge, which is now swept from scratch.
    for _ in range(3):
        hk.sync_discharge_turnarounds(lookback_hours=24)
    assert len(_tasks_for_discharge(ds_id)) == 1
