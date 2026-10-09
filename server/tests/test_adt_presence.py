"""Leave and absconded (adt-10, W7-D, 9 October 2026).

What this file defends:

  1. THE RECORD. A leave out and back writes two movements ('leave_start',
     'leave_end') and leaves presence_status and presence_since NULL; the bed
     is held the whole time (ward, bed and both ids untouched, the same place
     on both sides of each movement) and the board says 'Occupied, patient on
     leave'. Absconded and back works the same way; a patient on leave who
     does not come back can be recorded absconded and keeps the time they left
     the ward. Each write has one fail-closed patient_movement audit row in
     its own transaction, and an unwritable one leaves nothing behind (503).
  2. THE STATE MACHINE. An open leave cannot be started twice (409
     already_on_leave); a leave that was never started cannot end; a return is
     only for an absconded patient; a reason is required for a leave and for
     absconding. Nothing is written by a refusal.
  3. WHO (ruling ADT-S7 held, strictest option). Doctors (patient.write) and
     the administrator (adt.presence) record presence; a nurse and a ward
     clerk are refused (403); a doctor of another department gets 404, the
     attempt audited. The module off is 404 module_disabled.
  4. REPLAYS AND TIMES. The Idempotency-Key is required and a replay writes
     nothing; a key reused for another change is 409. Times up to the
     back-time window, never in the future, never before the last leave or
     return.
  5. DISPLAY ONLY. /api/vigil/patients rows and the nurse board's cards carry
     presence_status and presence_since only while ORB_ADT is on; with the
     module off their payloads are unchanged. With and without a leave the
     nurse board's obs and medication due fields, its order and its counts,
     and the escalation chain a critical set opens, are identical (adt-11 is
     where they change).
  6. THE BOARD. The occupant carries presence_since, and a patient absconded
     longer than ORB_ADT_ABSCONDED_CLOSE_HOURS (or on leave longer than
     ORB_ADT_LEAVE_PROMPT_HOURS) gets an advisory line; nothing closes.
"""
import ast
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import config
import main
from auth import create_token, hash_password
from database import db_session
from services import bed_master_service as bm
from services import escalation_service, nurse_shift_service
from services.adt import core as adt_core
from services.adt import presence as adt_presence
from services.audit_service import AuditWriteError

client = TestClient(main.app)
SERVER = Path(__file__).resolve().parents[1]


# ── helpers ──────────────────────────────────────────────────────────────────

def _tag() -> str:
    return uuid.uuid4().hex[:6].upper()


def _user(role: str, department: str | None) -> dict:
    uid = f"PR{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO users (id, name, role, department, password_hash, is_active) "
            "VALUES (?, ?, ?, ?, ?, 1)",
            (uid, f"Presence {role} {uid[-4:]}", role, department, hash_password("password")),
        )
    return {"id": uid, "role": role, "department": department}


def _hdr(user: dict, key: str | None = None) -> dict:
    out = {"Authorization": f"Bearer {create_token(user['id'], user['role'])}"}
    if key:
        out["Idempotency-Key"] = key
    return out


def _ago(**delta) -> str:
    return (datetime.now(timezone.utc) - timedelta(**delta)).isoformat()


def _one(sql: str, params: tuple = ()) -> dict | None:
    with db_session() as db:
        row = db.execute(sql, params).fetchone()
        return dict(row) if row else None


def _all(sql: str, params: tuple = ()) -> list[dict]:
    with db_session() as db:
        return [dict(r) for r in db.execute(sql, params).fetchall()]


@pytest.fixture()
def adt_on(monkeypatch):
    monkeypatch.setattr(config, "ADT_ENABLED", True)
    monkeypatch.setattr(config, "HOUSEKEEPING_ENABLED", True)
    yield


@pytest.fixture()
def site():
    """A department of its own (so the boards list only this file's
    patients), a doctor, a nurse and a ward clerk in it, a hospital-wide
    administrator, and a ward with three beds."""
    dept = f"Presence Medicine {_tag()}"
    doctor = _user("doctor", dept)
    nurse = _user("nurse", dept)
    clerk = _user("ward_clerk", dept)
    admin = _user("admin", None)
    ward = bm.save_ward({"code": f"PR-{_tag()}", "department": dept}, actor=admin)
    bm.register_beds({"ward_id": ward["id"], "labels": ["1", "2", "3"]}, actor=admin)
    out = {"dept": dept, "doctor": doctor, "nurse": nurse, "clerk": clerk, "admin": admin,
           "ward": ward, "patients": []}
    yield out
    with db_session() as db:
        for pid in out["patients"]:
            db.execute("UPDATE admissions SET status = 'discharged' WHERE patient_id = ? "
                       "AND status = 'active'", (pid,))
            db.execute("UPDATE patients SET status = 'discharged' WHERE id = ?", (pid,))


def _bed(ward_id: str, label: str) -> dict:
    with db_session() as db:
        return dict(db.execute("SELECT * FROM housekeeping_units WHERE ward_id = ? AND bed_label = ?",
                               (ward_id, label)).fetchone())


def _admit(site: dict, label: str, *, admitted_days_ago: int = 4) -> dict:
    """An admitted patient of the site's department in bed `label`, admitted
    some days ago so back-timed events fall inside the stay."""
    pid = f"PPR{uuid.uuid4().hex[:8].upper()}"
    aid = f"ADM-PR{uuid.uuid4().hex[:8].upper()}"
    name = f"Presence Patient {pid[-6:]}"
    started = _ago(days=admitted_days_ago)
    with db_session() as db:
        db.execute(
            "INSERT INTO patients (id, name, mrn, status, admission_date) "
            "VALUES (?, ?, ?, 'admitted', ?)",
            (pid, name, f"MRN-{pid}", started))
        db.execute(
            "INSERT INTO admissions (id, patient_id, admitted_by, department, status, admitted_at) "
            "VALUES (?, ?, 'D001', ?, 'active', ?)",
            (aid, pid, site["dept"], started))
    site["patients"].append(pid)
    bed = _bed(site["ward"]["id"], label)
    with db_session(immediate=True) as db:
        adt_core.locate_admission_on(db, admission_id=aid, actor=site["admin"],
                                     movement_type="bed_assigned", reason="test",
                                     ward_id=site["ward"]["id"], bed_id=bed["id"])
    return {"pid": pid, "aid": aid, "name": name, "bed_id": bed["id"], "label": label}


def _post(user: dict, path: str, body: dict | None = None, key: str | None = "auto"):
    if key == "auto":
        key = f"k-{uuid.uuid4().hex}"
    return client.post(f"/api/adt/admissions/{path}", headers=_hdr(user, key), json=body or {})


def _adm(aid: str) -> dict:
    return _one("SELECT * FROM admissions WHERE id = ?", (aid,))


def _moves(aid: str) -> list[dict]:
    return _all("SELECT * FROM patient_movements WHERE admission_id = ? ORDER BY rowid", (aid,))


def _tile(user: dict, ward_id: str, label: str) -> dict:
    resp = client.get("/api/adt/board", params={"ward_id": ward_id}, headers=_hdr(user))
    assert resp.status_code == 200, resp.text
    ward = next(w for w in resp.json()["wards"] if w["id"] == ward_id)
    return next(t for t in ward["tiles"] if t["bed_label"] == label)


# ═════════════════════════════════════════════════════════════════════════════
#  1. The record
# ═════════════════════════════════════════════════════════════════════════════

def test_leave_out_and_back_writes_two_movements_and_leaves_presence_null(adt_on, site):
    p = _admit(site, "1")
    before = _adm(p["aid"])
    out = _post(site["doctor"], f"{p['aid']}/leave",
                {"reason": "Home for the festival, approved by the consultant",
                 "expected_return_at": (datetime.now(timezone.utc) + timedelta(hours=20)).isoformat()})
    assert out.status_code == 200, out.text
    body = out.json()
    assert body["presence_status"] == "on_leave" and body["presence_since"]
    assert body["replayed"] is False and body["bed_held"]["bed_id"] == p["bed_id"]
    during = _adm(p["aid"])
    assert during["presence_status"] == "on_leave" and during["presence_since"]
    # The bed is held: nothing about where the patient lies changed.
    for col in ("ward_id", "bed_id", "ward", "bed", "department", "status"):
        assert during[col] == before[col], col
    back = _post(site["doctor"], f"{p['aid']}/leave-end", {})
    assert back.status_code == 200, back.text
    after = _adm(p["aid"])
    assert after["presence_status"] is None and after["presence_since"] is None
    assert after["bed_id"] == p["bed_id"]
    kinds = [m["movement_type"] for m in _moves(p["aid"])]
    assert kinds[-2:] == ["leave_start", "leave_end"]
    for m in _moves(p["aid"])[-2:]:
        # The same place on both sides: the bed is the patient's throughout.
        assert m["from_bed_id"] == m["to_bed_id"] == p["bed_id"]
        assert m["moved_by"] == site["doctor"]["id"]
    leave_move = _moves(p["aid"])[-2]
    assert leave_move["reason"].startswith("Home for the festival")
    assert '"expected_return_at"' in leave_move["detail_json"]
    audits = _all("SELECT * FROM audit_logs WHERE patient_id = ? AND action_type IN "
                  "('leave_started', 'leave_ended') ORDER BY id", (p["pid"],))
    assert [a["action_type"] for a in audits] == ["leave_started", "leave_ended"]
    assert all(a["event_category"] == "patient_movement" for a in audits)


def test_the_bed_stays_occupied_on_leave_on_the_board_and_is_never_freed(adt_on, site):
    p = _admit(site, "2")
    assert _post(site["doctor"], f"{p['aid']}/leave", {"reason": "Day leave"}).status_code == 200
    tile = _tile(site["doctor"], site["ward"]["id"], "2")
    assert tile["status"] == "occupied_on_leave"
    assert tile["status_words"] == "Occupied, patient on leave"
    assert tile["free"] is False
    assert tile["occupant"]["presence_status"] == "on_leave"
    assert tile["occupant"]["presence_since"] == _adm(p["aid"])["presence_since"]
    # No other patient can be put in the held bed.
    other = _admit(site, "3")
    with pytest.raises(adt_core.AdtConflict) as refused:
        with db_session(immediate=True) as db:
            adt_core.locate_admission_on(db, admission_id=other["aid"], actor=site["admin"],
                                         movement_type="bed_change", reason="test",
                                         bed_id=p["bed_id"])
    assert refused.value.code == "bed_occupied"
    assert _post(site["doctor"], f"{p['aid']}/leave-end", {}).status_code == 200
    assert _tile(site["doctor"], site["ward"]["id"], "2")["status"] == "occupied"


def test_absconded_and_back_and_a_leave_that_turns_into_absconding(adt_on, site):
    p = _admit(site, "1")
    gone = _post(site["admin"], f"{p['aid']}/absconded",
                 {"reason": "Not in bed at the 06:00 round; security informed"})
    assert gone.status_code == 200, gone.text
    assert _adm(p["aid"])["presence_status"] == "absconded"
    tile = _tile(site["admin"], site["ward"]["id"], "1")
    assert tile["status"] == "occupied_absconded"
    assert tile["status_words"] == "Occupied, patient absconded"
    assert _post(site["admin"], f"{p['aid']}/returned", {}).status_code == 200
    assert _adm(p["aid"])["presence_status"] is None

    # A back-timed leave before that return is refused: the times stay in order.
    early = _post(site["doctor"], f"{p['aid']}/leave",
                  {"reason": "Home overnight", "occurred_at": _ago(hours=5)})
    assert early.status_code == 400 and early.json()["detail"]["error"] == "before_last_change"
    assert _post(site["doctor"], f"{p['aid']}/leave", {"reason": "Home overnight"}
                 ).status_code == 200
    since = _adm(p["aid"])["presence_since"]
    out = _post(site["doctor"], f"{p['aid']}/absconded",
                {"reason": "Did not come back from leave; family not answering"})
    assert out.status_code == 200, out.text
    adm = _adm(p["aid"])
    # Off the ward since the leave began, not since it was noticed.
    assert adm["presence_status"] == "absconded" and adm["presence_since"] == since
    assert out.json()["presence_since"] == since
    # A return from leave is not a return from absconding.
    wrong = _post(site["doctor"], f"{p['aid']}/leave-end", {})
    assert wrong.status_code == 409 and wrong.json()["detail"]["error"] == "patient_absconded"
    assert _post(site["doctor"], f"{p['aid']}/returned", {}).status_code == 200
    kinds = [m["movement_type"] for m in _moves(p["aid"])]
    assert kinds == ["bed_assigned", "absconded", "absconded_return", "leave_start",
                     "absconded", "absconded_return"]


def test_an_unwritable_audit_leaves_nothing_behind(adt_on, site, monkeypatch):
    p = _admit(site, "1")
    moves_before = len(_moves(p["aid"]))
    real = adt_core.log_audit_event

    def broken(*args, **kwargs):
        if kwargs.get("event_category") == "patient_movement":
            raise AuditWriteError("audit table unwritable")
        return real(*args, **kwargs)

    monkeypatch.setattr(adt_core, "log_audit_event", broken)
    out = _post(site["doctor"], f"{p['aid']}/leave", {"reason": "Day leave"})
    assert out.status_code == 503 and out.json()["detail"]["error"] == "audit_unavailable"
    assert _adm(p["aid"])["presence_status"] is None
    assert len(_moves(p["aid"])) == moves_before


def test_current_location_says_off_the_ward(adt_on, site):
    p = _admit(site, "1")
    assert adt_core.current_location(p["pid"])["on_ward"] is True
    assert _post(site["doctor"], f"{p['aid']}/leave", {"reason": "Scan at the city hospital"}
                 ).status_code == 200
    loc = adt_core.current_location(p["pid"])
    assert loc["on_ward"] is False and loc["presence_status"] == "on_leave"
    assert loc["bed_id"] == p["bed_id"]


# ═════════════════════════════════════════════════════════════════════════════
#  2. The state machine
# ═════════════════════════════════════════════════════════════════════════════

def test_an_open_leave_cannot_be_started_twice_and_nothing_is_written(adt_on, site):
    p = _admit(site, "1")
    assert _post(site["doctor"], f"{p['aid']}/leave", {"reason": "Day leave"}).status_code == 200
    count = len(_moves(p["aid"]))
    again = _post(site["doctor"], f"{p['aid']}/leave", {"reason": "Day leave again"})
    assert again.status_code == 409 and again.json()["detail"]["error"] == "already_on_leave"
    assert len(_moves(p["aid"])) == count


@pytest.mark.parametrize("path, code", [
    ("leave-end", "not_on_leave"),
    ("returned", "not_absconded"),
])
def test_a_change_that_cannot_start_from_on_the_ward_is_refused(adt_on, site, path, code):
    p = _admit(site, "1")
    count = len(_moves(p["aid"]))
    out = _post(site["doctor"], f"{p['aid']}/{path}", {})
    assert out.status_code == 409 and out.json()["detail"]["error"] == code
    assert len(_moves(p["aid"])) == count


def test_a_leave_while_absconded_and_a_second_absconding_are_refused(adt_on, site):
    p = _admit(site, "1")
    assert _post(site["doctor"], f"{p['aid']}/absconded", {"reason": "Missing at the round"}
                 ).status_code == 200
    leave = _post(site["doctor"], f"{p['aid']}/leave", {"reason": "Day leave"})
    assert leave.status_code == 409 and leave.json()["detail"]["error"] == "patient_absconded"
    twice = _post(site["doctor"], f"{p['aid']}/absconded", {"reason": "Still missing"})
    assert twice.status_code == 409 and twice.json()["detail"]["error"] == "already_absconded"


@pytest.mark.parametrize("path", ["leave", "absconded"])
def test_a_leave_and_absconding_need_a_reason(adt_on, site, path):
    p = _admit(site, "1")
    out = _post(site["doctor"], f"{p['aid']}/{path}", {"reason": "   "})
    assert out.status_code == 422 and out.json()["detail"]["error"] == "reason_required"
    assert _adm(p["aid"])["presence_status"] is None


def test_an_ended_stay_takes_no_presence(adt_on, site):
    p = _admit(site, "1")
    with db_session() as db:
        db.execute("UPDATE admissions SET status = 'discharged' WHERE id = ?", (p["aid"],))
    with pytest.raises(adt_core.AdtConflict) as refused:
        adt_presence.start_leave(p["aid"], actor=site["doctor"], reason="Day leave",
                                 idempotency_key=f"k-{uuid.uuid4().hex}")
    assert refused.value.code == "admission_not_active"


def test_an_unknown_body_field_is_refused(adt_on, site):
    p = _admit(site, "1")
    out = _post(site["doctor"], f"{p['aid']}/leave", {"reason": "Day leave", "bed_id": "X"})
    assert out.status_code == 422


# ═════════════════════════════════════════════════════════════════════════════
#  3. Who
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("role", ["nurse", "clerk"])
def test_a_nurse_and_a_ward_clerk_are_refused(adt_on, site, role):
    p = _admit(site, "1")
    for path in ("leave", "leave-end", "absconded", "returned"):
        out = _post(site[role], f"{p['aid']}/{path}", {"reason": "Day leave"})
        assert out.status_code == 403, (path, out.text)
    assert _adm(p["aid"])["presence_status"] is None


def test_a_doctor_of_another_department_gets_404_and_the_attempt_is_audited(adt_on, site):
    p = _admit(site, "1")
    stranger = _user("doctor", f"Elsewhere {_tag()}")
    out = _post(stranger, f"{p['aid']}/leave", {"reason": "Day leave"})
    assert out.status_code == 404
    assert _adm(p["aid"])["presence_status"] is None
    denied = _all("SELECT * FROM audit_logs WHERE action_type = 'patient_scope_denied' "
                  "AND actor_user_id = ?", (stranger["id"],))
    assert len(denied) == 1 and denied[0]["patient_id"] == p["pid"]


def test_the_routes_are_404_with_the_module_off(monkeypatch, site):
    monkeypatch.setattr(config, "ADT_ENABLED", False)
    out = _post(site["doctor"], "ADM-NOPE/leave", {"reason": "Day leave"})
    assert out.status_code == 404 and out.json()["detail"]["error"] == "module_disabled"


# ═════════════════════════════════════════════════════════════════════════════
#  4. Replays and times
# ═════════════════════════════════════════════════════════════════════════════

def test_the_key_is_required_replayed_and_never_reused(adt_on, site):
    p = _admit(site, "1")
    missing = _post(site["doctor"], f"{p['aid']}/leave", {"reason": "Day leave"}, key=None)
    assert missing.status_code == 422
    assert missing.json()["detail"]["error"] == "idempotency_key_required"
    key = f"k-{uuid.uuid4().hex}"
    first = _post(site["doctor"], f"{p['aid']}/leave", {"reason": "Day leave"}, key=key)
    assert first.status_code == 200
    count = len(_moves(p["aid"]))
    again = _post(site["doctor"], f"{p['aid']}/leave", {"reason": "Day leave"}, key=key)
    assert again.status_code == 200 and again.json()["replayed"] is True
    assert again.json()["movement"]["id"] == first.json()["movement"]["id"]
    assert len(_moves(p["aid"])) == count
    # The same key for another change, or by another person, writes nothing.
    other = _post(site["doctor"], f"{p['aid']}/leave-end", {}, key=key)
    assert other.status_code == 409
    assert other.json()["detail"]["error"] == "idempotency_key_reused"
    assert _adm(p["aid"])["presence_status"] == "on_leave"


def test_times_inside_the_window_are_kept_and_outside_it_refused(adt_on, site):
    p = _admit(site, "1", admitted_days_ago=6)
    far = _post(site["doctor"], f"{p['aid']}/leave",
                {"reason": "Day leave", "occurred_at": _ago(hours=80)})
    assert far.status_code == 400 and far.json()["detail"]["error"] == "beyond_backtime"
    ahead = _post(site["doctor"], f"{p['aid']}/leave",
                  {"reason": "Day leave",
                   "occurred_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()})
    assert ahead.status_code == 400 and ahead.json()["detail"]["error"] == "future_time"
    left = _ago(hours=10)
    ok = _post(site["doctor"], f"{p['aid']}/leave", {"reason": "Day leave", "occurred_at": left})
    assert ok.status_code == 200
    assert _moves(p["aid"])[-1]["moved_at"].startswith(left[:16])
    early = _post(site["doctor"], f"{p['aid']}/leave-end", {"occurred_at": _ago(hours=11)})
    assert early.status_code == 400 and early.json()["detail"]["error"] == "before_last_change"


def test_an_expected_return_before_the_leave_is_refused(adt_on, site):
    p = _admit(site, "1")
    out = _post(site["doctor"], f"{p['aid']}/leave",
                {"reason": "Day leave", "expected_return_at": _ago(hours=1)})
    assert out.status_code == 422 and out.json()["detail"]["error"] == "bad_expected_return"


def test_a_return_before_the_admission_is_refused(adt_on, site):
    p = _admit(site, "1", admitted_days_ago=1)
    out = _post(site["doctor"], f"{p['aid']}/absconded",
                {"reason": "Missing", "occurred_at": _ago(hours=30)})
    assert out.status_code == 400 and out.json()["detail"]["error"] == "before_admission"


# ═════════════════════════════════════════════════════════════════════════════
#  5. Display only
# ═════════════════════════════════════════════════════════════════════════════

def _vigil_rows(user: dict) -> dict:
    resp = client.get("/api/vigil/patients", headers=_hdr(user), params={"limit": 500})
    assert resp.status_code == 200, resp.text
    return {r["id"]: r for r in resp.json()}


def test_the_vigil_and_nurse_rows_carry_presence_with_the_module_on(adt_on, site):
    p = _admit(site, "1")
    q = _admit(site, "2")
    assert _post(site["doctor"], f"{p['aid']}/leave", {"reason": "Day leave"}).status_code == 200
    rows = _vigil_rows(site["doctor"])
    assert rows[p["pid"]]["presence_status"] == "on_leave"
    assert rows[p["pid"]]["presence_since"] == _adm(p["aid"])["presence_since"]
    assert rows[q["pid"]]["presence_status"] is None and rows[q["pid"]]["presence_since"] is None
    resp = client.get("/api/nurse/board", headers=_hdr(site["nurse"]))
    assert resp.status_code == 200, resp.text
    cards = {c["patient_id"]: c for c in resp.json()["patients"]}
    assert cards[p["pid"]]["presence_status"] == "on_leave"
    assert cards[q["pid"]]["presence_status"] is None


def test_the_payloads_are_unchanged_with_the_module_off(monkeypatch, site):
    monkeypatch.setattr(config, "ADT_ENABLED", True)
    p = _admit(site, "1")
    # Presence written while the module was on, then the module switched off.
    with db_session() as db:
        db.execute("UPDATE admissions SET presence_status = 'on_leave', presence_since = ? "
                   "WHERE id = ?", (_ago(hours=1), p["aid"]))
    monkeypatch.setattr(config, "ADT_ENABLED", False)
    assert adt_presence.presence_by_patient([p["pid"]]) is None
    row = _vigil_rows(site["doctor"])[p["pid"]]
    assert "presence_status" not in row and "presence_since" not in row
    board = nurse_shift_service.build_board(site["dept"])
    card = next(c for c in board["patients"] if c["patient_id"] == p["pid"])
    assert "presence_status" not in card and "presence_since" not in card


_PRESENCE_KEYS = {"presence_status", "presence_since"}


def _without_presence(board: dict) -> dict:
    out = dict(board)
    out["patients"] = [{k: v for k, v in c.items() if k not in _PRESENCE_KEYS}
                       for c in board["patients"]]
    return out


def test_obs_due_and_the_board_order_are_identical_with_and_without_a_leave(
        adt_on, site, monkeypatch):
    p = _admit(site, "1")
    q = _admit(site, "2")
    now = datetime.now(timezone.utc)
    with db_session() as db:
        # A set two hours ago for one patient, none for the other.
        db.execute("INSERT INTO vitals (patient_id, heart_rate, respiratory_rate, spo2, "
                   "systolic_bp, temperature, consciousness, on_oxygen, timestamp, source, "
                   "carried_from) VALUES (?, 118, 24, 93, 98, 38.4, 'alert', 0, ?, 'manual', NULL)",
                   (p["pid"], (now - timedelta(hours=2)).isoformat()))
    monkeypatch.setattr(nurse_shift_service, "_now", lambda: now)
    before = nurse_shift_service.build_board(site["dept"])
    assert _post(site["doctor"], f"{p['aid']}/leave", {"reason": "Day leave"}).status_code == 200
    assert _post(site["doctor"], f"{q['aid']}/absconded", {"reason": "Missing"}).status_code == 200
    during = nurse_shift_service.build_board(site["dept"])
    assert _without_presence(during) == _without_presence(before)
    card = next(c for c in during["patients"] if c["patient_id"] == p["pid"])
    assert card["presence_status"] == "on_leave"
    # Overdue obs still read overdue: pausing them is adt-11, not this.
    absent = next(c for c in during["patients"] if c["patient_id"] == q["pid"])
    assert absent["obs_overdue"] is True


def _chain_shape(chain: dict) -> dict:
    """An escalation chain without what names its patient or itself."""
    drop = {"id", "escalation_id", "patient_id", "patient_name", "bed", "ward", "mrn",
            "raised_at", "updated_at", "next_due_at", "events", "tier_started_at",
            "last_observation_at", "observed_at", "vitals_id", "channel_id"}
    return {k: v for k, v in chain.items() if k not in drop}


def test_the_escalation_a_critical_set_opens_is_identical_on_leave(adt_on, site):
    on_ward = _admit(site, "1")
    away = _admit(site, "2")
    assert _post(site["doctor"], f"{away['aid']}/leave", {"reason": "Day leave"}).status_code == 200
    news2 = {"raw_news2": 9, "level": "critical", "drivers": ["Respiration rate 30"],
             "single_param_3": True}
    at = datetime.now(timezone.utc)
    first = escalation_service.evaluate_observation(on_ward["pid"], dict(news2), now=at,
                                                    notify=False)
    second = escalation_service.evaluate_observation(away["pid"], dict(news2), now=at,
                                                     notify=False)
    assert first is not None and second is not None
    assert _chain_shape(first) == _chain_shape(second)


def test_no_clinical_lane_reads_presence_yet():
    """adt-11 (ruling ADT-S4) is where obs, medication, escalation, sepsis and
    the simulator learn about presence. Until then none of them reads it."""
    for name in ("escalation_service.py", "sepsis_bundle_service.py", "bot_service.py",
                 "downtime_service.py"):
        source = (SERVER / "services" / name).read_text()
        tree = ast.parse(source)
        imported = {getattr(n, "module", None) for n in ast.walk(tree)
                    if isinstance(n, ast.ImportFrom)}
        assert "services.adt.presence" not in imported, name
        assert "presence_status" not in source, name


# ═════════════════════════════════════════════════════════════════════════════
#  6. The board's advisory lines
# ═════════════════════════════════════════════════════════════════════════════

def test_absconded_past_the_close_hours_gets_an_advisory_line_and_nothing_closes(
        adt_on, site, monkeypatch):
    p = _admit(site, "1")
    q = _admit(site, "2")
    monkeypatch.setattr(config, "ADT_ABSCONDED_CLOSE_HOURS", 24)
    assert _post(site["doctor"], f"{p['aid']}/absconded",
                 {"reason": "Missing", "occurred_at": _ago(hours=30)}).status_code == 200
    assert _post(site["doctor"], f"{q['aid']}/absconded",
                 {"reason": "Missing", "occurred_at": _ago(hours=2)}).status_code == 200
    late = _tile(site["doctor"], site["ward"]["id"], "1")
    fresh = _tile(site["doctor"], site["ward"]["id"], "2")
    assert any("Absconded for over 24 hours" in m for m in late["markers"])
    assert not any("Absconded for over" in m for m in fresh["markers"])
    assert _adm(p["aid"])["status"] == "active"


def test_a_long_leave_gets_the_doctor_prompt(adt_on, site, monkeypatch):
    p = _admit(site, "1")
    monkeypatch.setattr(config, "ADT_LEAVE_PROMPT_HOURS", 48)
    assert _post(site["doctor"], f"{p['aid']}/leave",
                 {"reason": "Home", "occurred_at": _ago(hours=50)}).status_code == 200
    tile = _tile(site["doctor"], site["ward"]["id"], "1")
    assert any("On leave for over 48 hours" in m for m in tile["markers"])


def test_an_out_of_scope_tile_says_nothing_about_presence(adt_on, site):
    p = _admit(site, "1")
    assert _post(site["doctor"], f"{p['aid']}/leave", {"reason": "Home"}).status_code == 200
    outsider = _user("ward_clerk", f"Elsewhere {_tag()}")
    # A clerk of another department cannot open this ward at all.
    resp = client.get("/api/adt/board", params={"ward_id": site["ward"]["id"]},
                      headers=_hdr(outsider))
    assert resp.status_code == 404
    # The hospital-wide administrator sees it, with presence.
    tile = _tile(site["admin"], site["ward"]["id"], "1")
    assert tile["occupant"]["presence_status"] == "on_leave"
