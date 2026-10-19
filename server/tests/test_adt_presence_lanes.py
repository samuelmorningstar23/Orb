"""Clinical lanes honour presence (adt-11, W9-B, 10 October 2026).

Ruling ADT-S4, built as option (a) and held for Samuel to confirm: a patient
recorded on leave or absconded stays on every board with a 'Not on ward' chip;
obs and dose clocks pause with the reason; an open escalation is paused, never
closed, and resumes on return; sepsis auto-start opens nothing; the vitals
simulator writes nothing; the downtime ward pack marks them NOT ON WARD. All of
it reads ONE helper (services/adt/clinical_pause.py) and only while ORB_ADT is
on.

What this file defends:

  1. THE HELPER. None with the module off; only patients off the ward; a set
     observed after the patient left is live; the reason sentence.
  2. THE NURSE BOARD. No overdue obs and no overdue dose for a patient on
     leave, with the reason; they stay on the board, after everyone on the
     ward; the clocks run again on return and a set charted after the return
     is live. Module off: presence in the table changes nothing.
  3. THE ESCALATION LADDER. An open chain is paused when the patient leaves
     (never closed), does not climb while they are away, and resumes with the
     minutes it had left when they are back. A timeout owed before the patient
     left still climbs first. A set charted after the patient left makes the
     chain live again; a set from before does not. Switching the module off
     resumes every paused chain. An absconded patient's open chain names the
     treating doctor in the case room, once. Every change is an event and an
     audit row. Module off: the ladder climbs as before.
  4. SEPSIS AUTO-START. Nothing opens for a patient off the ward, unless the
     set behind it was taken after they left; a clinician's own start is never
     refused; module off: as before.
  5. THE SIMULATOR writes no reading for a patient off the ward, and does again
     on return; module off: as before.
  6. THE WARD PACK keeps the page and marks NOT ON WARD on it and in the
     census; module off: not a word.
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest

import config
from database import db_session
from services import bot_service, downtime_service, escalation_service, nurse_shift_service
from services import sepsis_bundle_service
from services.adt import clinical_pause
from services.clinical_time import to_utc_iso, utc_iso_now
from services.vigil_service import compute_news2

# A fixed instant in the past for the ladder, so every expectation is
# arithmetic and no other module's live chain is due before it.
T = datetime(2026, 9, 1, 9, 0, 0, tzinfo=timezone.utc)

# NEWS2 6: over the threshold of 5, under the emergency band, so the chain
# opens at tier 1 (ward nurse, 10 minutes).
DETERIORATING = dict(heart_rate=95, systolic_bp=120, respiratory_rate=22,
                     temperature=38.5, spo2=93)


def _news2(v: dict) -> dict:
    return compute_news2(v["heart_rate"], v["systolic_bp"], v["respiratory_rate"],
                         v["temperature"], v["spo2"], "alert", False)


def _tag() -> str:
    return uuid.uuid4().hex[:8].upper()


_CREATED: list[str] = []


@pytest.fixture(scope="module", autouse=True)
def _cleanup():
    yield
    if not _CREATED:
        return
    marks = ",".join("?" for _ in _CREATED)
    ids = tuple(_CREATED)
    with db_session() as db:
        db.execute(f"DELETE FROM escalation_events WHERE escalation_id IN "
                   f"(SELECT id FROM clinical_escalations WHERE patient_id IN ({marks}))", ids)
        db.execute(f"DELETE FROM clinical_escalations WHERE patient_id IN ({marks})", ids)
        db.execute(f"UPDATE care_bundles SET status = 'cancelled' WHERE patient_id IN ({marks}) "
                   "AND status = 'active'", ids)
        db.execute(f"UPDATE admissions SET status = 'discharged', presence_status = NULL, "
                   f"presence_since = NULL WHERE patient_id IN ({marks})", ids)
        db.execute(f"UPDATE patients SET status = 'discharged' WHERE id IN ({marks})", ids)


@pytest.fixture()
def adt_on(monkeypatch):
    monkeypatch.setattr(config, "ADT_ENABLED", True)
    yield


@pytest.fixture()
def adt_off(monkeypatch):
    monkeypatch.setattr(config, "ADT_ENABLED", False)
    yield


@pytest.fixture()
def relay(monkeypatch):
    """Every case-room post, captured instead of written."""
    sent: list[tuple[str, str]] = []

    def _capture(patient_id, content, *a, **k):
        sent.append((patient_id, content))
        return False, None, None, None, 0

    from services import ingestion_service
    monkeypatch.setattr(ingestion_service, "_persist_relay_alert", _capture)
    return sent


@pytest.fixture()
def dept():
    return f"Lanes Medicine {_tag()}"


def _user(role: str, department: str | None) -> str:
    uid = f"LN{role[:3].upper()}{_tag()}"
    with db_session() as db:
        db.execute("INSERT INTO users (id, name, role, password_hash, department) "
                   "VALUES (?, ?, ?, 'x', ?)", (uid, f"Lanes {role} {uid[-4:]}", role, department))
    return uid


# An invented name: a name holding an ordinary word ("Patient", "Admitted") is read by
# Sage's name matcher in later test files, so "the patient" there would match it.
def _patient(department: str, *, bed: str = "L-1", attending: str | None = None,
             days_ago: int = 3) -> dict:
    pid = f"PLN{_tag()}"
    aid = f"ADM-LN{_tag()}"
    started = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
    with db_session() as db:
        db.execute("INSERT INTO patients (id, name, mrn, bed, ward, status, admission_date, "
                   "attending_doctor_id) VALUES (?, ?, ?, ?, 'LW', 'admitted', ?, ?)",
                   (pid, f"Lanevra {pid[-5:]}", f"MRN-{pid}", bed, started, attending))
        db.execute("INSERT INTO admissions (id, patient_id, admitted_by, department, status, "
                   "attending_doctor_id, admitted_at) VALUES (?, ?, 'D001', ?, 'active', ?, ?)",
                   (aid, pid, department, attending, started))
    _CREATED.append(pid)
    return {"pid": pid, "aid": aid}


def _away(p: dict, status: str | None, since: datetime | None = None) -> None:
    """Record presence straight on the admission (adt-10's writer is tested in
    test_adt_presence.py; here the time must be exact)."""
    with db_session() as db:
        db.execute("UPDATE admissions SET presence_status = ?, presence_since = ? WHERE id = ?",
                   (status, to_utc_iso(since) if since else None, p["aid"]))


def _vitals(pid: str, at: datetime, source: str = nurse_shift_service.OBS_SOURCE,
            **values) -> None:
    v = {"heart_rate": 80, "systolic_bp": 120, "respiratory_rate": 16, "temperature": 36.8,
         "spo2": 97, **values}
    with db_session() as db:
        db.execute("INSERT INTO vitals (patient_id, timestamp, heart_rate, systolic_bp, "
                   "respiratory_rate, temperature, spo2, consciousness, on_oxygen, source, "
                   "carried_from) VALUES (?, ?, ?, ?, ?, ?, ?, 'alert', 0, ?, NULL)",
                   (pid, to_utc_iso(at), v["heart_rate"], v["systolic_bp"],
                    v["respiratory_rate"], v["temperature"], v["spo2"], source))


def _chain(pid: str) -> dict:
    with db_session() as db:
        return dict(db.execute("SELECT * FROM clinical_escalations WHERE patient_id = ? "
                               "AND status != 'resolved'", (pid,)).fetchone())


def _events(eid: str) -> list[dict]:
    with db_session() as db:
        return [dict(r) for r in db.execute(
            "SELECT * FROM escalation_events WHERE escalation_id = ? ORDER BY seq",
            (eid,)).fetchall()]


def _kinds(eid: str) -> list[str]:
    return [e["event_type"] for e in _events(eid)]


def _audits(pid: str, action: str) -> list[dict]:
    with db_session() as db:
        return [dict(r) for r in db.execute(
            "SELECT * FROM audit_logs WHERE patient_id = ? AND action_type = ?",
            (pid, action)).fetchall()]


def _raise(pid: str, at: datetime = T) -> dict:
    chain = escalation_service.evaluate_observation(pid, _news2(DETERIORATING), now=at,
                                                    notify=False)
    assert chain is not None and chain["current_tier"] == 1
    return chain


# ═════════════════════════════════════════════════════════════════════════════
#  1. The helper
# ═════════════════════════════════════════════════════════════════════════════

def test_the_helper_says_nothing_with_the_module_off(adt_off, dept):
    p = _patient(dept)
    _away(p, "on_leave", T)
    assert clinical_pause.away_by_patient([p["pid"]]) is None
    assert clinical_pause.away_for(p["pid"]) is None


def test_the_helper_lists_only_patients_off_the_ward(adt_on, dept):
    here = _patient(dept)
    gone = _patient(dept)
    _away(gone, "absconded", T)
    found = clinical_pause.away_by_patient([here["pid"], gone["pid"]])
    assert set(found) == {gone["pid"]}
    assert found[gone["pid"]]["presence_status"] == "absconded"
    assert clinical_pause.left_ward_at(found[gone["pid"]]) == T


def test_a_set_taken_after_leaving_is_live_and_one_from_before_is_not():
    away = {"presence_status": "on_leave", "presence_since": to_utc_iso(T)}
    assert clinical_pause.observed_after_leaving(away, T + timedelta(minutes=1)) is True
    assert clinical_pause.observed_after_leaving(away, T - timedelta(minutes=1)) is False
    assert clinical_pause.observed_after_leaving(away, None) is False
    assert clinical_pause.observed_after_leaving(None, T) is False
    # No leaving time on record: Orb cannot prove the set is older, so it is live.
    assert clinical_pause.observed_after_leaving(
        {"presence_status": "on_leave", "presence_since": None}, T) is True


def test_the_reason_says_what_is_paused_and_when_it_restarts():
    leave = {"presence_status": "on_leave", "presence_since": None}
    assert clinical_pause.pause_reason(leave) == (
        "Observations and doses are paused while the patient is on leave. They restart "
        "when the return to the ward is recorded.")
    assert clinical_pause.pause_reason(
        {"presence_status": "absconded"}, medication_round=False).startswith(
        "Observations are paused while the patient is absconded.")
    assert clinical_pause.pause_reason(None) is None


# ═════════════════════════════════════════════════════════════════════════════
#  2. The nurse board
# ═════════════════════════════════════════════════════════════════════════════

def _med(pid: str, started: datetime) -> int:
    with db_session() as db:
        return db.execute(
            "INSERT INTO medications (patient_id, drug_name, dosage, route, frequency, status, "
            "prescribed_by, source, started_at) VALUES (?, 'Paracetamol', '1 g', 'Oral', 'Q6H', "
            "'active', 'D001', 'clinician', ?)", (pid, started.isoformat())).lastrowid


def _card(board: dict, pid: str) -> dict:
    return next(c for c in board["patients"] if c["patient_id"] == pid)


def test_no_overdue_obs_or_dose_on_leave_with_the_reason(adt_on, dept, monkeypatch):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    monkeypatch.setattr(nurse_shift_service, "_now", lambda: now)
    here = _patient(dept, bed="L-1")
    gone = _patient(dept, bed="L-2")
    for p in (here, gone):
        # A set five hours ago (4-hourly at NEWS2 0 to 4 reads overdue) and a
        # six-hourly drug started 7 hours ago, so a dose is late.
        _vitals(p["pid"], now - timedelta(hours=5), heart_rate=95)
        _med(p["pid"], now - timedelta(hours=7))
    _away(gone, "on_leave", now - timedelta(hours=1))

    board = nurse_shift_service.build_board(dept)
    on_ward, away = _card(board, here["pid"]), _card(board, gone["pid"])
    assert on_ward["obs_overdue"] is True and on_ward["meds_overdue"] == 1
    assert on_ward["paused"] is None and on_ward["medications"][0]["paused"] is False

    assert away["obs_overdue"] is False and away["obs_due_soon"] is False
    assert away["obs_due_at"] is None and away["obs_minutes_until_due"] is None
    assert away["meds_overdue"] == 0 and away["meds_due_soon"] == 0
    task = away["medications"][0]
    assert task["paused"] is True and task["overdue"] is False and task["due_at"] is None
    assert away["paused"]["presence_status"] == "on_leave"
    assert away["paused"]["presence_since"] == away["presence_since"]
    assert "paused while the patient is on leave" in away["paused"]["reason"]
    # Still on the board, after everyone on the ward.
    order = [c["patient_id"] for c in board["patients"]]
    assert order.index(here["pid"]) < order.index(gone["pid"])
    assert board["totals"]["obs_overdue"] == 1 and board["totals"]["meds_overdue"] == 1
    assert board["totals"]["not_on_ward"] == 1


def test_the_clocks_run_again_on_return_and_a_set_after_it_is_live(adt_on, dept, monkeypatch):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    monkeypatch.setattr(nurse_shift_service, "_now", lambda: now)
    p = _patient(dept)
    _vitals(p["pid"], now - timedelta(hours=30))
    _away(p, "on_leave", now - timedelta(hours=26))
    assert _card(nurse_shift_service.build_board(dept), p["pid"])["obs_overdue"] is False
    # Back on the ward: the last set was before the leave, so obs are due now.
    _away(p, None)
    back = _card(nurse_shift_service.build_board(dept), p["pid"])
    assert back["paused"] is None and back["obs_overdue"] is True
    # A set charted after the return is live: it anchors the clock.
    _vitals(p["pid"], now - timedelta(minutes=5))
    after = _card(nurse_shift_service.build_board(dept), p["pid"])
    assert after["obs_overdue"] is False and after["obs_minutes_until_due"] > 0


def test_on_a_ward_record_computer_only_observations_are_paused(adt_on, dept, monkeypatch):
    """No medication round under the Orb Ward Record: the reason names
    observations only, and the dose counts stay None, never 0."""
    monkeypatch.setattr(config, "WARD_RECORD", True, raising=False)
    p = _patient(dept)
    _away(p, "on_leave", datetime.now(timezone.utc) - timedelta(hours=1))
    card = _card(nurse_shift_service.build_board(dept), p["pid"])
    assert card["paused"]["reason"].startswith("Observations are paused while the patient")
    assert card["obs_overdue"] is False and card["obs_due_at"] is None
    assert card["meds_overdue"] is None and card["medications"] == []


def test_module_off_the_board_ignores_presence_in_the_table(adt_off, dept, monkeypatch):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    monkeypatch.setattr(nurse_shift_service, "_now", lambda: now)
    p = _patient(dept)
    _vitals(p["pid"], now - timedelta(hours=5), heart_rate=95)
    before = nurse_shift_service.build_board(dept)
    _away(p, "absconded", now - timedelta(hours=1))
    during = nurse_shift_service.build_board(dept)
    assert during == before
    card = _card(during, p["pid"])
    assert card["obs_overdue"] is True
    assert "paused" not in card and "presence_status" not in card
    assert "not_on_ward" not in during["totals"]


# ═════════════════════════════════════════════════════════════════════════════
#  3. The escalation ladder
# ═════════════════════════════════════════════════════════════════════════════

def _advance(at: datetime) -> list[dict]:
    return escalation_service.advance_due(now=at)


def test_an_open_chain_pauses_on_leave_and_resumes_with_the_minutes_it_had(
        adt_on, dept, relay):
    p = _patient(dept)
    chain = _raise(p["pid"])
    eid = chain["id"]
    _away(p, "on_leave", T + timedelta(minutes=2))

    _advance(T + timedelta(minutes=3))
    paused = _chain(p["pid"])
    assert paused["status"] == "open" and paused["current_tier"] == 1
    assert paused["tier_due_at"] is None
    ev = next(e for e in _events(eid) if e["event_type"] == "paused")
    assert '"remaining_seconds": 420.0' in ev["metadata_json"]
    assert len(_audits(p["pid"], "escalation_paused")) == 1
    assert any(pid == p["pid"] and "ESCALATION PAUSED" in text for pid, text in relay)

    # Hours later, still away: never climbed, never closed, paused once.
    moved = _advance(T + timedelta(hours=3))
    assert eid not in {m["id"] for m in moved}
    still = _chain(p["pid"])
    assert still["current_tier"] == 1 and still["status"] == "open"
    assert _kinds(eid).count("paused") == 1

    # The board says why the clock is stopped.
    listed = escalation_service.list_escalations(patient_id=p["pid"])[0]
    assert listed["paused"]["presence_status"] == "on_leave"

    # Back on the ward: seven minutes left on tier 1, from the return.
    _away(p, None)
    back_at = T + timedelta(hours=4)
    _advance(back_at)
    resumed = _chain(p["pid"])
    assert resumed["tier_due_at"] == (back_at + timedelta(minutes=7)).isoformat()
    assert _kinds(eid).count("resumed") == 1
    assert '"reason": "returned"' in next(
        e["metadata_json"] for e in _events(eid) if e["event_type"] == "resumed")
    assert len(_audits(p["pid"], "escalation_resumed")) == 1
    assert escalation_service.list_escalations(patient_id=p["pid"])[0]["paused"] is None
    assert any("ESCALATION RESUMED" in text and "in 7 minutes" in text
               for pid, text in relay if pid == p["pid"])

    _advance(back_at + timedelta(minutes=8))
    assert _chain(p["pid"])["current_tier"] == 2


def test_a_timeout_owed_before_the_patient_left_still_climbs_first(adt_on, dept, relay):
    p = _patient(dept)
    eid = _raise(p["pid"])["id"]
    # Tier 1 ran out at T+10; the patient left at T+12; the sweep runs at T+13.
    _away(p, "on_leave", T + timedelta(minutes=12))
    _advance(T + timedelta(minutes=13))
    assert _chain(p["pid"])["current_tier"] == 2
    assert "paused" not in _kinds(eid)
    # The next tick pauses tier 2 with what it has left (due T+25).
    _advance(T + timedelta(minutes=14))
    assert _chain(p["pid"])["tier_due_at"] is None
    paused = [e for e in _events(eid) if e["event_type"] == "paused"]
    assert len(paused) == 1 and '"remaining_seconds": 660.0' in paused[0]["metadata_json"]


def test_a_set_taken_after_leaving_makes_the_chain_live_and_an_older_one_does_not(
        adt_on, dept, relay):
    p = _patient(dept)
    eid = _raise(p["pid"])["id"]
    _away(p, "on_leave", T + timedelta(minutes=2))
    _advance(T + timedelta(minutes=3))
    assert _chain(p["pid"])["tier_due_at"] is None

    # A set from before the patient left, copied in late: still paused.
    escalation_service.evaluate_observation(
        p["pid"], _news2(DETERIORATING), observed_at=T + timedelta(minutes=1),
        now=T + timedelta(minutes=20), notify=False)
    _advance(T + timedelta(minutes=21))
    assert _chain(p["pid"])["tier_due_at"] is None
    assert "resumed" not in _kinds(eid)

    # A set taken at T+30, after the patient left: somebody is with them.
    escalation_service.evaluate_observation(
        p["pid"], _news2(DETERIORATING), observed_at=T + timedelta(minutes=30),
        now=T + timedelta(minutes=30), notify=False)
    _advance(T + timedelta(minutes=31))
    live = _chain(p["pid"])
    assert live["tier_due_at"] == (T + timedelta(minutes=38)).isoformat()
    resumed = [e for e in _events(eid) if e["event_type"] == "resumed"]
    assert len(resumed) == 1 and '"reason": "observed"' in resumed[0]["metadata_json"]
    # Not paused again while the record still says on leave, and it climbs.
    _advance(T + timedelta(minutes=32))
    assert _kinds(eid).count("paused") == 1
    _advance(T + timedelta(minutes=39))
    assert _chain(p["pid"])["current_tier"] == 2


def test_an_absconded_patient_names_the_treating_doctor_once(adt_on, dept, relay):
    doctor = _user("doctor", dept)
    p = _patient(dept, attending=doctor)
    q = _patient(dept, attending=doctor, bed="L-2")
    eid = _raise(p["pid"])["id"]
    acked = _raise(q["pid"])["id"]
    escalation_service.acknowledge(acked, {"id": doctor, "role": "doctor"},
                                   now=T + timedelta(minutes=1))
    _away(p, "absconded", T + timedelta(minutes=2))
    _away(q, "absconded", T + timedelta(minutes=2))

    _advance(T + timedelta(minutes=3))
    notice = [e for e in _events(eid) if e["event_type"] == "absconded_notice"]
    assert len(notice) == 1 and notice[0]["target_user_id"] == doctor
    assert "paused" in _kinds(eid)
    posts = [text for pid, text in relay if pid == p["pid"]]
    assert len(posts) == 1, posts
    assert posts[0].startswith("PATIENT ABSCONDED WITH AN OPEN ESCALATION [TIER 1")
    assert f"Addressed to: Lanes doctor {doctor[-4:]} (Treating doctor, named attending" in posts[0]
    assert "paused until the patient is back on the ward" in posts[0]
    assert len(_audits(p["pid"], "escalation_absconded_notice")) == 1
    # An acknowledged chain is told too; it has no clock to pause.
    assert "absconded_notice" in _kinds(acked) and "paused" not in _kinds(acked)
    assert _chain(q["pid"])["status"] == "acknowledged"

    _advance(T + timedelta(minutes=4))
    _advance(T + timedelta(minutes=5))
    assert _kinds(eid).count("absconded_notice") == 1
    assert len([1 for pid, _t in relay if pid == p["pid"]]) == 1


def test_leave_turning_into_absconding_tells_the_doctor_without_a_second_pause(
        adt_on, dept, relay):
    doctor = _user("doctor", dept)
    p = _patient(dept, attending=doctor)
    eid = _raise(p["pid"])["id"]
    _away(p, "on_leave", T + timedelta(minutes=2))
    _advance(T + timedelta(minutes=3))
    # Not back from leave: recorded absconded, off the ward since the leave.
    _away(p, "absconded", T + timedelta(minutes=2))
    _advance(T + timedelta(minutes=30))
    kinds = _kinds(eid)
    assert kinds.count("paused") == 1 and kinds.count("absconded_notice") == 1
    text = [t for pid, t in relay if pid == p["pid"]][-1]
    assert "paused until the patient is back on the ward" in text


def test_switching_the_module_off_resumes_every_paused_chain(dept, relay, monkeypatch):
    monkeypatch.setattr(config, "ADT_ENABLED", True)
    p = _patient(dept)
    eid = _raise(p["pid"])["id"]
    _away(p, "on_leave", T + timedelta(minutes=2))
    _advance(T + timedelta(minutes=3))
    assert _chain(p["pid"])["tier_due_at"] is None
    monkeypatch.setattr(config, "ADT_ENABLED", False)
    _advance(T + timedelta(minutes=40))
    chain = _chain(p["pid"])
    assert chain["tier_due_at"] == (T + timedelta(minutes=47)).isoformat()
    resumed = [e for e in _events(eid) if e["event_type"] == "resumed"]
    assert '"reason": "module_off"' in resumed[0]["metadata_json"]
    # And the readers add no key with the module off.
    assert "paused" not in escalation_service.list_escalations(patient_id=p["pid"])[0]


def test_an_ended_stay_is_no_longer_paused(adt_on, dept, relay):
    p = _patient(dept)
    eid = _raise(p["pid"])["id"]
    _away(p, "absconded", T + timedelta(minutes=2))
    _advance(T + timedelta(minutes=3))
    with db_session() as db:
        db.execute("UPDATE admissions SET status = 'discharged' WHERE id = ?", (p["aid"],))
        db.execute("UPDATE patients SET status = 'discharged' WHERE id = ?", (p["pid"],))
    _advance(T + timedelta(minutes=10))
    resumed = [e for e in _events(eid) if e["event_type"] == "resumed"]
    assert len(resumed) == 1 and '"reason": "stay_ended"' in resumed[0]["metadata_json"]
    assert _chain(p["pid"])["status"] == "open"


def test_module_off_the_ladder_climbs_as_before(adt_off, dept, relay):
    p = _patient(dept)
    eid = _raise(p["pid"])["id"]
    _away(p, "on_leave", T + timedelta(minutes=2))
    _advance(T + timedelta(minutes=11))
    assert _chain(p["pid"])["current_tier"] == 2
    kinds = _kinds(eid)
    assert not {"paused", "resumed", "absconded_notice"} & set(kinds)


def test_a_critical_set_on_leave_opens_the_chain_and_it_stays_live(adt_on, dept, relay):
    """A set taken while the record says on leave was taken with the patient
    in front of somebody: the chain it raises is live, never paused."""
    p = _patient(dept)
    _away(p, "on_leave", T)
    chain = escalation_service.evaluate_observation(
        p["pid"], _news2(DETERIORATING), observed_at=T + timedelta(minutes=5),
        now=T + timedelta(minutes=5), notify=False)
    _advance(T + timedelta(minutes=6))
    assert "paused" not in _kinds(chain["id"])
    _advance(T + timedelta(minutes=16))
    assert _chain(p["pid"])["current_tier"] == 2


# ═════════════════════════════════════════════════════════════════════════════
#  4. Sepsis auto-start
# ═════════════════════════════════════════════════════════════════════════════

def _bundles(pid: str) -> list[dict]:
    with db_session() as db:
        return [dict(r) for r in db.execute(
            "SELECT * FROM care_bundles WHERE patient_id = ?", (pid,)).fetchall()]


@pytest.fixture()
def autostart(monkeypatch):
    monkeypatch.setattr(config, "SEPSIS_AUTOSTART", True)


def test_auto_start_opens_nothing_off_the_ward(adt_on, dept, autostart):
    p = _patient(dept)
    since = datetime.now(timezone.utc) - timedelta(hours=1)
    _away(p, "on_leave", since)
    assert sepsis_bundle_service.maybe_autostart(p["pid"], {"raw_news2": 9}) is None
    assert sepsis_bundle_service.maybe_autostart(
        p["pid"], {"raw_news2": 9}, observed_at=since - timedelta(minutes=5)) is None
    # The no-score fallback of the critical alert lane goes through start_bundle.
    assert sepsis_bundle_service.start_bundle(
        p["pid"], triggered_by="vigil_auto", trigger_reason="Critical Vigil alert",
        source="vigil_auto") is None
    assert _bundles(p["pid"]) == []
    # A clinician's own start is never refused.
    mine = sepsis_bundle_service.start_bundle(p["pid"], trigger_reason="Manual start")
    assert mine is not None and mine["status"] == "active"


def test_a_set_taken_after_leaving_still_opens_the_bundle(adt_on, dept, autostart):
    p = _patient(dept)
    since = datetime.now(timezone.utc) - timedelta(hours=1)
    _away(p, "absconded", since)
    opened = sepsis_bundle_service.maybe_autostart(
        p["pid"], {"raw_news2": 9}, observed_at=since + timedelta(minutes=30))
    assert opened is not None and opened["source"] == "vigil_auto"


def test_the_bedside_lane_passes_the_observation_time(adt_on, dept, autostart, monkeypatch):
    """A nurse charting a critical set for a patient the record says is on
    leave is with the patient: the set is live and opens the bundle."""
    monkeypatch.setattr(nurse_shift_service, "dispatch_observation_alert",
                        lambda *a, **k: None)
    monkeypatch.setattr(escalation_service, "evaluate_observation", lambda *a, **k: None)
    p = _patient(dept)
    _away(p, "on_leave", datetime.now(timezone.utc) - timedelta(hours=1))
    nurse = _user("nurse", dept)
    nurse_shift_service.record_observation(
        p["pid"], nurse, {"heart_rate": 135, "systolic_bp": 84, "respiratory_rate": 30,
                          "temperature": 35.0, "spo2": 88, "consciousness": "alert",
                          "on_oxygen": False},
        taken_at=utc_iso_now())
    assert [b["source"] for b in _bundles(p["pid"])] == ["vigil_auto"]


def test_module_off_auto_start_is_as_before(adt_off, dept, autostart):
    p = _patient(dept)
    _away(p, "on_leave", datetime.now(timezone.utc) - timedelta(hours=1))
    assert sepsis_bundle_service.maybe_autostart(p["pid"], {"raw_news2": 9}) is not None


# ═════════════════════════════════════════════════════════════════════════════
#  5. The simulator
# ═════════════════════════════════════════════════════════════════════════════

def _tick(monkeypatch, ours: set[str]) -> None:
    """One simulator tick that writes only for this file's patients: every
    other admitted patient in the shared database is skipped, so the suite's
    other modules are not touched."""
    real = bot_service._next_reading

    def _only_ours(db, pid, latest, now_ts, *, on_scale_2):
        if pid not in ours:
            raise RuntimeError("not this file's patient")
        return real(db, pid, latest, now_ts, on_scale_2=on_scale_2)

    monkeypatch.setattr(bot_service, "_next_reading", _only_ours)
    bot_service._generate_vitals(utc_iso_now(), 1)


def _simulated(pid: str) -> int:
    with db_session() as db:
        return db.execute("SELECT COUNT(*) AS n FROM vitals WHERE patient_id = ? "
                          "AND source = 'simulated'", (pid,)).fetchone()["n"]


def test_the_simulator_writes_nothing_for_a_patient_off_the_ward(adt_on, dept, monkeypatch):
    here = _patient(dept)
    gone = _patient(dept, bed="L-2")
    for p in (here, gone):
        _vitals(p["pid"], datetime.now(timezone.utc) - timedelta(minutes=10))
    _away(gone, "on_leave", datetime.now(timezone.utc) - timedelta(minutes=5))
    _tick(monkeypatch, {here["pid"], gone["pid"]})
    assert _simulated(here["pid"]) == 1
    assert _simulated(gone["pid"]) == 0
    # Back on the ward: the next tick writes for them again.
    _away(gone, None)
    _tick(monkeypatch, {here["pid"], gone["pid"]})
    assert _simulated(gone["pid"]) == 1


def test_module_off_the_simulator_ticks_as_before(adt_off, dept, monkeypatch):
    p = _patient(dept)
    _vitals(p["pid"], datetime.now(timezone.utc) - timedelta(minutes=10))
    _away(p, "absconded", datetime.now(timezone.utc) - timedelta(minutes=5))
    _tick(monkeypatch, {p["pid"]})
    assert _simulated(p["pid"]) == 1


# ═════════════════════════════════════════════════════════════════════════════
#  6. The ward pack
# ═════════════════════════════════════════════════════════════════════════════

def _pack_for(ward_patients: list[dict]) -> tuple[str, list[dict]]:
    ids = {p["pid"] for p in ward_patients}
    rows, escalations = downtime_service._admitted_for_packs("LW")
    rows = [r for r in rows if r["id"] in ids]
    for r in rows:
        r["escalation"] = escalations.get(r["id"])
        r["envelope"] = None
    return downtime_service._render_ward_pack("LW", rows, utc_iso_now(), names={}), rows


def test_the_ward_pack_marks_not_on_ward_and_keeps_the_page(adt_on, dept):
    here = _patient(dept, bed="L-1")
    gone = _patient(dept, bed="L-2")
    _away(gone, "absconded", datetime(2026, 10, 9, 8, 35, tzinfo=timezone.utc))
    doc, rows = _pack_for([here, gone])
    assert {r["id"]: r["away"] is not None for r in rows} == {here["pid"]: False,
                                                               gone["pid"]: True}
    assert doc.count("<!-- orb-pack-patient:") == 2
    assert doc.count("NOT ON WARD: absconded") == 2       # census row and the page
    # The ward's clock, never a raw UTC stamp.
    assert "NOT ON WARD: absconded since 9 Oct 2026, 14:05 IST." in doc
    assert "The bed is held for them." in doc
    assert "paused while the patient is absconded" in doc


def test_module_off_the_ward_pack_says_nothing_about_presence(adt_off, dept):
    gone = _patient(dept)
    _away(gone, "on_leave", datetime.now(timezone.utc) - timedelta(hours=1))
    doc, rows = _pack_for([gone])
    assert "away" not in rows[0]
    assert "NOT ON WARD" not in doc
