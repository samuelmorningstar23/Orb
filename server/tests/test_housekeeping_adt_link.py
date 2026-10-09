"""Housekeeping turnaround linked to the bed movement ledger (adt-7, W6-C).

What this file defends:

  1. EXACTLY ONCE. One transfer gives exactly one turnaround task across ten
     sync runs; one discharge gives exactly one task across BOTH sweeps (the
     old discharge sweep and the movement sync) run ten times each, because
     both key a discharge on its summary id (else the movement id); each
     movement side gets one housekeeping_movement_links row.
  2. THE RECORD IS HONEST. A summary-less end (LAMA) raises its task;
     vacated_at is the movement's moved_at, not the sync's clock; a clean is
     terminal when the stay that left carried isolation precautions or the
     bed is an isolation room, and standard for '[]'; a bed recorded clean is
     marked occupied when a patient goes in, and a dirty bed admitted into
     gets housekeeping's force_occupied record naming the movement.
  3. IT CAN NEVER FAIL AN ADT WRITE. An exception in the sync leaves the
     transfer committed and answered 200.
  4. FLAG OFF: nothing synced, nothing written. A ward whose cleaning is not
     tracked is skipped. The housekeeping board load runs the sync.
"""
import json
import time
import uuid

import pytest
from fastapi.testclient import TestClient

import config
import main
from auth import create_token, hash_password
from database import db_session
from services import bed_master_service as bm
from services import discharge_service
from services import housekeeping_service as hk
from services.adt import end_stay as adt_end
from services.adt import transfer as adt_transfer

client = TestClient(main.app)
DEPT = "General Medicine"
DOCTOR = {"id": "D001", "role": "doctor", "department": DEPT, "name": "Dr. Test"}

_SEEDED: list[str] = []


@pytest.fixture(autouse=True)
def _discharge_seeded():
    yield
    if not _SEEDED:
        return
    with db_session() as db:
        db.executemany("UPDATE patients SET status = 'discharged' WHERE id = ?",
                       [(p,) for p in _SEEDED])
        db.executemany("UPDATE admissions SET status = 'discharged' "
                       "WHERE patient_id = ? AND status = 'active'", [(p,) for p in _SEEDED])
    _SEEDED.clear()


@pytest.fixture()
def on(monkeypatch):
    monkeypatch.setattr(config, "ADT_ENABLED", True)
    monkeypatch.setattr(config, "HOUSEKEEPING_ENABLED", True)
    yield


def _tag() -> str:
    return uuid.uuid4().hex[:6].upper()


_ADMIN: dict | None = None


def _admin() -> dict:
    global _ADMIN
    if _ADMIN is None:
        uid = f"HL{uuid.uuid4().hex[:8].upper()}"
        with db_session() as db:
            db.execute("INSERT INTO users (id, name, role, department, password_hash, is_active) "
                       "VALUES (?, 'Link Admin', 'admin', NULL, ?, 1)",
                       (uid, hash_password("password")))
        _ADMIN = {"id": uid, "role": "admin", "department": None}
    return _ADMIN


def _cover(dept: str) -> dict:
    uid = f"HL{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute("INSERT INTO users (id, name, role, department, password_hash, is_active) "
                   "VALUES (?, 'Link Doctor', 'doctor', ?, ?, 1)",
                   (uid, dept, hash_password("password")))
    return {"id": uid, "role": "doctor", "department": dept}


def _hdr(user: dict, key: str | None = None) -> dict:
    out = {"Authorization": f"Bearer {create_token(user['id'], user['role'])}"}
    if key:
        out["Idempotency-Key"] = key
    return out


def _ward(department: str = DEPT, labels=("1", "2", "3"), *, tracked: bool = True,
          ready: bool = True) -> dict:
    ward = bm.save_ward({"code": f"HL-{_tag()}", "department": department,
                         "cleaning_tracked": tracked}, actor=_admin())
    bm.register_beds({"ward_id": ward["id"], "labels": list(labels)}, actor=_admin())
    if ready:
        for label in labels:
            _ready(_bed(ward["id"], label)["id"])
    return ward


def _bed(ward_id: str, label: str) -> dict:
    with db_session() as db:
        return dict(db.execute(
            "SELECT * FROM housekeeping_units WHERE ward_id = ? AND bed_label = ?",
            (ward_id, label)).fetchone())


def _ready(bed_id: str) -> None:
    hk.transition_unit(bed_id, "cleaning_in_progress", actor_user_id="HLHK", reason="test")
    hk.transition_unit(bed_id, "vacant_ready", actor_user_id="HLHK", reason="test")


def _one(sql: str, params: tuple = ()) -> dict | None:
    with db_session() as db:
        row = db.execute(sql, params).fetchone()
        return dict(row) if row else None


def _all(sql: str, params: tuple = ()) -> list[dict]:
    with db_session() as db:
        return [dict(r) for r in db.execute(sql, params).fetchall()]


def _admit(ward: dict, label: str, **extra) -> dict:
    bed = _bed(ward["id"], label)
    body = {"name": f"Link Patient {_tag()}", "age": 61, "sex": "F",
            "admission_source": "er", "admission_type": "inpatient",
            "department": DEPT, "ward": ward["code"], "bed": label,
            "ward_id": ward["id"], "bed_id": bed["id"],
            "attending_doctor_id": "D001", "chief_complaint": "Fever", **extra}
    resp = client.post("/api/admissions/admit", headers=_hdr(DOCTOR, f"adm-{_tag()}"), json=body)
    assert resp.status_code == 201, resp.text
    out = resp.json()
    _SEEDED.append(out["patient_id"])
    return {"pid": out["patient_id"], "aid": out["admission_id"], "bed_id": bed["id"],
            "movement_id": out["movement_id"]}


def _move(p: dict, ward: dict, label: str, **extra) -> dict:
    return adt_transfer.change_bed(
        p["aid"], actor=DOCTOR, reason="Moved for the link test",
        bed_id=_bed(ward["id"], label)["id"], expected_bed_id=p["bed_id"],
        idempotency_key=f"mv-{_tag()}", **extra)


def _tasks(source_type: str, source_ref: str) -> list[dict]:
    return _all("SELECT * FROM housekeeping_tasks WHERE source_type = ? AND source_ref = ?",
                (source_type, source_ref))


def _links(movement_id: str) -> list[dict]:
    return _all("SELECT * FROM housekeeping_movement_links WHERE movement_id = ? ORDER BY side",
                (movement_id,))


def _state(bed_id: str) -> str:
    return _one("SELECT state FROM housekeeping_units WHERE id = ?", (bed_id,))["state"]


# ═════════════════════════════════════════════════════════════════════════════
#  1. Exactly once
# ═════════════════════════════════════════════════════════════════════════════

def test_one_transfer_gives_exactly_one_task_across_ten_runs(on):
    ward = _ward()
    p = _admit(ward, "1")
    moved = _move(p, ward, "2")   # the mover runs the sync once itself, after its commit
    mid = moved["movement"]["id"]
    for _ in range(10):
        hk.sync_adt_movements()
    tasks = _tasks("transfer", mid)
    assert len(tasks) == 1
    assert tasks[0]["unit_id"] == p["bed_id"]
    assert tasks[0]["request_type"] == "transfer_turnaround"
    links = _links(mid)
    assert [(l["side"], l["effect"]) for l in links] == [("from", "vacated"), ("to", "occupied")]
    assert links[0]["task_id"] == tasks[0]["id"]
    assert _state(p["bed_id"]) == "vacant_dirty"
    assert _state(_bed(ward["id"], "2")["id"]) == "occupied"


def test_one_discharge_gives_one_task_across_both_sweeps_run_ten_times(on):
    ward = _ward()
    p = _admit(ward, "1")
    out = discharge_service.discharge_patient(p["pid"], "D001", "Well, home", [],
                                              discharge_type="routine")
    summary_id = out["summary_id"]
    move = _one("SELECT * FROM patient_movements WHERE admission_id = ? "
                "AND movement_type = 'discharge'", (p["aid"],))
    assert move["discharge_summary_id"] == summary_id
    for _ in range(10):
        hk.sync_discharge_turnarounds()
        hk.sync_adt_movements()
    tasks = _tasks("discharge", str(summary_id))
    assert len(tasks) == 1
    assert _tasks("discharge", move["id"]) == [] and _tasks("transfer", move["id"]) == []
    assert _state(p["bed_id"]) == "vacant_dirty"
    assert [l["side"] for l in _links(move["id"])] == ["from", "to"]


def test_a_summary_less_end_raises_its_task_keyed_on_the_movement(on):
    ward = _ward()
    p = _admit(ward, "1")
    mrn = _one("SELECT mrn FROM patients WHERE id = ?", (p["pid"],))["mrn"]
    adt_end.end_stay(p["aid"], end_type="lama", actor=DOCTOR, confirm_mrn=mrn,
                     reason="Left against medical advice")
    move = _one("SELECT * FROM patient_movements WHERE admission_id = ? "
                "AND movement_type = 'discharge'", (p["aid"],))
    assert move["discharge_summary_id"] is None
    hk.sync_adt_movements()
    for _ in range(3):
        hk.sync_discharge_turnarounds()
        hk.sync_adt_movements()
    tasks = _tasks("discharge", move["id"])
    assert len(tasks) == 1 and tasks[0]["request_type"] == "discharge_turnaround"
    # vacated_at is when the patient left, as the ledger says.
    assert tasks[0]["vacated_at"] == move["moved_at"]


# ═════════════════════════════════════════════════════════════════════════════
#  2. The record is honest
# ═════════════════════════════════════════════════════════════════════════════

def test_vacated_at_equals_moved_at_for_a_back_timed_move(on):
    from datetime import datetime, timedelta, timezone

    ward = _ward()
    p = _admit(ward, "1", admitted_at=(datetime.now(timezone.utc) - timedelta(hours=2)).isoformat())
    when = (datetime.now(timezone.utc) - timedelta(minutes=40)).isoformat()
    moved = _move(p, ward, "2", moved_at=when)
    hk.sync_adt_movements()
    move = _one("SELECT moved_at FROM patient_movements WHERE id = ?", (moved["movement"]["id"],))
    task = _tasks("transfer", moved["movement"]["id"])[0]
    assert task["vacated_at"] == move["moved_at"]


def test_terminal_cleans_follow_isolation_precautions_and_isolation_rooms(on):
    ward = _ward(labels=("1", "2", "3", "4", "5", "6"))
    # A stay with precautions: terminal.
    iso = _admit(ward, "1")
    with db_session() as db:
        db.execute("UPDATE admissions SET isolation_precautions = ? WHERE id = ?",
                   (json.dumps(["contact"]), iso["aid"]))
    m1 = _move(iso, ward, "2")["movement"]["id"]
    # A stay whose precautions are the empty list: standard.
    plain = _admit(ward, "3")
    with db_session() as db:
        db.execute("UPDATE admissions SET isolation_precautions = '[]' WHERE id = ?",
                   (plain["aid"],))
    m2 = _move(plain, ward, "4")["movement"]["id"]
    # An isolation room with no precautions on the stay: terminal.
    room_stay = _admit(ward, "5")
    with db_session() as db:
        db.execute("UPDATE housekeeping_units SET is_isolation_room = 1 WHERE id = ?",
                   (room_stay["bed_id"],))
    m3 = _move(room_stay, ward, "6")["movement"]["id"]
    hk.sync_adt_movements()
    assert _tasks("transfer", m1)[0]["clean_type"] == "terminal"
    assert _tasks("transfer", m2)[0]["clean_type"] == "standard"
    assert _tasks("transfer", m3)[0]["clean_type"] == "terminal"
    # The boolean crosses, never the text.
    assert "contact" not in (_tasks("transfer", m1)[0]["precaution_note"] or "")


def test_a_terminal_clean_skipped_once_is_still_owed_at_the_next_departure(on):
    ward = _ward(labels=("1", "2", "3"))
    first = _admit(ward, "1")
    with db_session() as db:
        db.execute("UPDATE admissions SET isolation_precautions = ? WHERE id = ?",
                   (json.dumps(["droplet"]), first["aid"]))
    m1 = _move(first, ward, "2")["movement"]["id"]
    hk.sync_adt_movements()
    assert _tasks("transfer", m1)[0]["clean_type"] == "terminal"
    # A second patient is put in bed 1 before the terminal clean, authorised.
    second = _admit(ward, "1", terminal_clean_authorised_by="D001",
                    terminal_clean_reason="No other bed tonight")
    hk.sync_adt_movements()
    assert _tasks("transfer", m1)[0]["status"] == "cancelled"
    # The second patient carries no precautions, yet the clean owed is terminal.
    m2 = _move(second, ward, "3")["movement"]["id"]
    hk.sync_adt_movements()
    assert _tasks("transfer", m2)[0]["clean_type"] == "terminal"


def test_a_dirty_bed_admitted_into_is_force_occupied_naming_the_movement(on):
    ward = _ward(labels=("1",), ready=False)
    bed = _bed(ward["id"], "1")
    assert bed["state"] == "vacant_dirty"
    p = _admit(ward, "1", not_clean_reason="Only bed free")
    hk.sync_adt_movements()
    assert _state(bed["id"]) == "occupied"
    event = _one("SELECT * FROM housekeeping_unit_events WHERE unit_id = ? AND to_state = 'occupied' "
                 "ORDER BY id DESC LIMIT 1", (bed["id"],))
    assert "OCCUPIED WITHOUT COMPLETED CLEAN" in event["reason"]
    assert p["movement_id"] in event["reason"]
    assert [l["effect"] for l in _links(p["movement_id"]) if l["side"] == "to"] == ["occupied_unclean"]


def test_a_bed_recorded_clean_is_marked_occupied_when_a_patient_goes_in(on):
    ward = _ward(labels=("1",))
    p = _admit(ward, "1")
    hk.sync_adt_movements()
    assert _state(p["bed_id"]) == "occupied"
    assert [(l["side"], l["effect"]) for l in _links(p["movement_id"])] == \
        [("from", "skipped"), ("to", "occupied")]


def test_a_department_only_transfer_touches_no_bed(on, monkeypatch):
    ward = _ward(labels=("1",))
    p = _admit(ward, "1")
    hk.sync_adt_movements()
    recv = f"Link Recv {_tag()}"
    _cover(recv)
    moved = adt_transfer.transfer(
        p["aid"], actor=DOCTOR, to_department=recv, reason="Accepted, no bed yet",
        keep_location=True, boarder_acknowledged=True, expected_bed_id=p["bed_id"],
        idempotency_key=f"keep-{_tag()}")
    mid = moved["movement"]["id"]
    hk.sync_adt_movements()
    assert _tasks("transfer", mid) == []
    assert {l["note"] for l in _links(mid)} == {"same_bed"}
    assert _state(p["bed_id"]) == "occupied"


def test_a_bed_cleaned_after_the_patient_left_is_not_made_dirty_by_a_late_sync(on, monkeypatch):
    ward = _ward(labels=("1", "2"))
    p = _admit(ward, "1")
    # The sync after the move fails, so housekeeping hears nothing at first.
    monkeypatch.setattr(hk, "sync_adt_movements", lambda **_k: (_ for _ in ()).throw(RuntimeError()))
    moved = _move(p, ward, "2")
    monkeypatch.undo()
    monkeypatch.setattr(config, "ADT_ENABLED", True)
    monkeypatch.setattr(config, "HOUSEKEEPING_ENABLED", True)
    # Meanwhile a cleaner records bed 1 clean: it was cleaned AFTER the move
    # (kept clear of the move's millisecond, which would read as a tie).
    time.sleep(0.01)
    hk.transition_unit(p["bed_id"], "vacant_dirty", actor_user_id="HLHK", reason="spotted")
    _ready(p["bed_id"])
    hk.sync_adt_movements()
    assert _state(p["bed_id"]) == "vacant_ready"
    link = next(l for l in _links(moved["movement"]["id"]) if l["side"] == "from")
    assert link["effect"] == "task_raised" and link["note"] == "cleaned_since"
    # The clean it owed is recorded, so its task is not left open: a ready bed's
    # task can never be started (ready to being cleaned is not a legal move).
    task = _tasks("transfer", moved["movement"]["id"])[0]
    assert task["status"] == "cancelled"
    assert "recorded clean after the patient left" in task["cancel_reason"]
    assert _open_tasks(p["bed_id"]) == []


def _open_tasks(bed_id: str) -> list[dict]:
    return _all("SELECT * FROM housekeeping_tasks WHERE unit_id = ? "
                "AND status IN ('open', 'in_progress')", (bed_id,))


def _events(bed_id: str) -> list[dict]:
    return _all("SELECT * FROM housekeeping_unit_events WHERE unit_id = ? ORDER BY id", (bed_id,))


def _discharge(p: dict) -> dict:
    out = discharge_service.discharge_patient(p["pid"], "D001", "Well, home", [],
                                              discharge_type="routine")
    move = _one("SELECT * FROM patient_movements WHERE admission_id = ? "
                "AND movement_type = 'discharge'", (p["aid"],))
    return {"summary_id": out["summary_id"], "movement_id": move["id"]}


def _assert_readmitted_unclean(bed_id: str, discharge: dict, second: dict) -> None:
    """The bed the next patient was put in before it was recorded clean ends
    occupied with housekeeping's force_occupied record naming that patient's
    movement, the departure's turnaround cancelled, and nothing left open."""
    assert _state(bed_id) == "occupied"
    events = _events(bed_id)
    last = events[-1]
    assert last["to_state"] == "occupied"
    assert "OCCUPIED WITHOUT COMPLETED CLEAN" in last["reason"]
    assert second["movement_id"] in last["reason"]
    # The departure was recorded first: the bed was marked needing cleaning
    # when the first patient left.
    assert events[-2]["to_state"] == "vacant_dirty"
    assert events[-2]["reason"] == "patient discharged"
    tasks = _tasks("discharge", str(discharge["summary_id"]))
    assert len(tasks) == 1 and tasks[0]["status"] == "cancelled"
    assert second["movement_id"] in tasks[0]["cancel_reason"]
    assert _open_tasks(bed_id) == []
    assert [(l["side"], l["effect"]) for l in _links(discharge["movement_id"])] == \
        [("from", "vacated"), ("to", "skipped")]
    assert [l["effect"] for l in _links(second["movement_id"]) if l["side"] == "to"] == \
        ["occupied_unclean"]


def test_a_discharge_and_the_next_admission_synced_together_record_the_skipped_clean(on):
    # The usual case: neither the admission form nor the doctor's discharge
    # runs the sync, so the first stay, its discharge and the next admission
    # into the same bed all wait for one run.
    ward = _ward(labels=("1",))
    first = _admit(ward, "1")
    ended = _discharge(first)
    second = _admit(ward, "1", not_clean_reason="Only bed")
    out = hk.sync_adt_movements()
    assert out["occupied_unclean"] == 1 and out["vacated"] == 1
    _assert_readmitted_unclean(first["bed_id"], ended, second)
    # Running it again acts once.
    written = len(_events(first["bed_id"]))
    for _ in range(3):
        hk.sync_adt_movements()
    assert len(_events(first["bed_id"])) == written
    _assert_readmitted_unclean(first["bed_id"], ended, second)


def test_a_discharge_and_readmission_after_the_first_stay_was_recorded(on):
    ward = _ward(labels=("1",))
    first = _admit(ward, "1")
    hk.sync_adt_movements()
    assert _state(first["bed_id"]) == "occupied"
    ended = _discharge(first)
    second = _admit(ward, "1", not_clean_reason="Only bed")
    hk.sync_adt_movements()
    _assert_readmitted_unclean(first["bed_id"], ended, second)


def test_a_discharge_and_readmission_seen_first_by_the_board_load(on):
    # The board runs the old discharge sweep first, which raises the
    # discharge's task without moving a bed another admission holds; the
    # movement sync after it still records the skipped clean.
    ward = _ward(labels=("1",))
    first = _admit(ward, "1")
    hk.sync_adt_movements()
    ended = _discharge(first)
    second = _admit(ward, "1", not_clean_reason="Only bed")
    hk.overview()
    _assert_readmitted_unclean(first["bed_id"], ended, second)


def test_a_transfer_out_and_an_admission_in_synced_together(on, monkeypatch):
    ward = _ward(labels=("1", "2"))
    first = _admit(ward, "1")
    hk.sync_adt_movements()
    # The mover's own sync fails, so the move waits for the next run too.
    monkeypatch.setattr(hk, "sync_adt_movements", lambda **_k: (_ for _ in ()).throw(RuntimeError()))
    moved = _move(first, ward, "2")
    monkeypatch.undo()
    monkeypatch.setattr(config, "ADT_ENABLED", True)
    monkeypatch.setattr(config, "HOUSEKEEPING_ENABLED", True)
    second = _admit(ward, "1", not_clean_reason="Only bed")
    hk.sync_adt_movements()
    mid = moved["movement"]["id"]
    assert _state(first["bed_id"]) == "occupied"
    last = _events(first["bed_id"])[-1]
    assert "OCCUPIED WITHOUT COMPLETED CLEAN" in last["reason"]
    assert second["movement_id"] in last["reason"]
    assert _tasks("transfer", mid)[0]["status"] == "cancelled"
    assert _open_tasks(first["bed_id"]) == []
    assert _state(_bed(ward["id"], "2")["id"]) == "occupied"


def test_a_bed_cleaned_between_discharge_and_readmission_is_occupied_with_no_task_left(on):
    ward = _ward(labels=("1",))
    first = _admit(ward, "1")
    hk.sync_adt_movements()
    ended = _discharge(first)
    # A cleaner records bed 1 clean before any sync has seen the discharge.
    # julianday() reads time to the millisecond and the bed board reads a tie
    # as not cleaned, so the clean is kept clear of the discharge's moment.
    time.sleep(0.01)
    hk.transition_unit(first["bed_id"], "vacant_dirty", actor_user_id="HLHK", reason="spotted")
    _ready(first["bed_id"])
    second = _admit(ward, "1")
    hk.sync_adt_movements()
    assert _state(first["bed_id"]) == "occupied"
    last = _events(first["bed_id"])[-1]
    assert last["from_state"] == "vacant_ready" and last["to_state"] == "occupied"
    assert "OCCUPIED WITHOUT COMPLETED CLEAN" not in (last["reason"] or "")
    assert [l["effect"] for l in _links(second["movement_id"]) if l["side"] == "to"] == ["occupied"]
    tasks = _tasks("discharge", str(ended["summary_id"]))
    assert len(tasks) == 1 and tasks[0]["status"] == "cancelled"
    assert _open_tasks(first["bed_id"]) == []


def test_an_admission_timed_before_the_last_departure_still_records_the_skipped_clean(on):
    from datetime import datetime, timedelta, timezone

    ward = _ward(labels=("1",))
    first = _admit(ward, "1",
                   admitted_at=(datetime.now(timezone.utc) - timedelta(hours=3)).isoformat())
    hk.sync_adt_movements()
    ended = _discharge(first)
    # The next admission is timed before the discharge was recorded, so its
    # arrival is read first, while the bed still reads occupied.
    second = _admit(ward, "1", not_clean_reason="Only bed",
                    admitted_at=(datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat())
    out = hk.sync_adt_movements()
    assert [l["note"] for l in _links(second["movement_id"]) if l["side"] == "to"] == \
        ["already_occupied"]
    assert out["occupied_unclean"] == 1
    assert _state(first["bed_id"]) == "occupied"
    events = _events(first["bed_id"])
    assert events[-2]["to_state"] == "vacant_dirty"
    assert "OCCUPIED WITHOUT COMPLETED CLEAN" in events[-1]["reason"]
    assert second["movement_id"] in events[-1]["reason"]
    link = next(l for l in _links(ended["movement_id"]) if l["side"] == "from")
    assert (link["effect"], link["note"]) == ("vacated", "occupied_without_clean")
    assert _tasks("discharge", str(ended["summary_id"]))[0]["status"] == "cancelled"
    assert _open_tasks(first["bed_id"]) == []


def test_a_ward_whose_cleaning_is_not_tracked_is_skipped(on):
    ward = _ward(labels=("1", "2"), tracked=False, ready=False)
    p = _admit(ward, "1")
    moved = _move(p, ward, "2")
    hk.sync_adt_movements()
    assert _tasks("transfer", moved["movement"]["id"]) == []
    assert {l["note"] for l in _links(moved["movement"]["id"])} == {"cleaning_not_tracked"}


# ═════════════════════════════════════════════════════════════════════════════
#  3. Never fails an ADT write
# ═════════════════════════════════════════════════════════════════════════════

def test_an_exception_in_the_sync_leaves_the_transfer_committed(on, monkeypatch):
    ward = _ward()
    p = _admit(ward, "1")
    recv = f"Link Recv {_tag()}"
    _cover(recv)
    recv_ward = _ward(department=recv)

    def _boom(**_kwargs):
        raise RuntimeError("housekeeping table locked")

    monkeypatch.setattr(hk, "sync_adt_movements", _boom)
    resp = client.post("/api/adt/transfers", headers=_hdr(DOCTOR, f"tr-{_tag()}"), json={
        "admission_id": p["aid"], "to_department": recv,
        "bed_id": _bed(recv_ward["id"], "1")["id"], "expected_bed_id": p["bed_id"],
        "reason": "Escalation"})
    assert resp.status_code == 200, resp.text
    adm = _one("SELECT department, bed_id FROM admissions WHERE id = ?", (p["aid"],))
    assert adm == {"department": recv, "bed_id": _bed(recv_ward["id"], "1")["id"]}
    assert _tasks("transfer", resp.json()["movement"]["id"]) == []


# ═════════════════════════════════════════════════════════════════════════════
#  4. Flag off, and the board load
# ═════════════════════════════════════════════════════════════════════════════

def test_flag_off_nothing_is_synced(on, monkeypatch):
    ward = _ward()
    p = _admit(ward, "1")
    moved = _move(p, ward, "2")
    mid = moved["movement"]["id"]
    with db_session() as db:   # undo what the mover's own sync did
        db.execute("DELETE FROM housekeeping_movement_links WHERE movement_id = ?", (mid,))
        db.execute("DELETE FROM housekeeping_tasks WHERE source_type = 'transfer' AND source_ref = ?",
                   (mid,))
    monkeypatch.setattr(config, "ADT_ENABLED", False)
    out = hk.sync_adt_movements()
    assert out["off"] == "adt" and out["processed"] == 0
    assert _links(mid) == [] and _tasks("transfer", mid) == []
    # Housekeeping off too: nothing.
    monkeypatch.setattr(config, "ADT_ENABLED", True)
    monkeypatch.setattr(config, "HOUSEKEEPING_ENABLED", False)
    assert hk.sync_adt_movements()["off"] == "housekeeping"
    assert _links(mid) == []


def test_the_housekeeping_board_load_runs_the_sync(on):
    ward = _ward()
    p = _admit(ward, "1")
    moved = _move(p, ward, "2")
    mid = moved["movement"]["id"]
    with db_session() as db:
        db.execute("DELETE FROM housekeeping_movement_links WHERE movement_id = ?", (mid,))
        db.execute("DELETE FROM housekeeping_tasks WHERE source_type = 'transfer' AND source_ref = ?",
                   (mid,))
    board = hk.overview()
    assert "movement_sync" in board and board["movement_sync"].get("processed", 0) >= 1
    assert len(_tasks("transfer", mid)) == 1


def test_the_manual_sync_route_answers_counts_only(on):
    admin = _admin()
    resp = client.post("/api/housekeeping/sync-movements", headers=_hdr(admin))
    assert resp.status_code == 200, resp.text
    assert set(resp.json()) >= {"processed", "vacated", "tasks_raised", "occupied",
                                "occupied_unclean", "skipped", "lookback_hours"}
