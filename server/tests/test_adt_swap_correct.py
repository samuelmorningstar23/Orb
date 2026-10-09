"""Bed swap and a move recorded in error (adt-6b, W7-C).

What this file defends:

  1. FLAG OFF CHANGES NOTHING. With ORB_ADT off both new routes answer 404.
  2. A SWAP. Both patients end in each other's beds, with exactly two
     'bed_swap' movements (keyed '<key>' and '<key>#2') whose from and to are
     the true places, one fail-closed 'beds_swapped' audit per patient, and
     the partial unique index never trips. The text mirrors (admissions and
     patients) are the master's spelling after it.
  3. THE SCREEN'S STATE AND THE KEY. A retried swap with the same key returns
     the first answer and never swaps back; a fresh key with a stale expected
     bed id is 409 state_changed with nothing written; the key, both expected
     bed ids and a reason are required; a key used for another pair is 409.
  4. SCOPE. A swap involving an admission outside the caller's department is
     404 and writes nothing; the attempt is audited.
  5. THE RULES. Two different patients, both in beds of bed setup; a boarder
     needs the acknowledgement and then writes boarder_placed; on a ward whose
     cleaning is tracked the swap needs the reason neither bed is cleaned
     between them, and a bed whose next clean is terminal an authoriser who
     acts (ADT-S2 (c)); an unwritable audit rolls the swap back.
  6. A MOVE RECORDED IN ERROR. A correction restores the earlier place through
     the writer, both rows remain (the new one points at the old), and the
     mirrors follow. Only the latest move, only once, only while the patient
     is still where it put them, and not for a swap. Undoing a transfer gives
     the chart back to the sending department, needs adt.transfer or
     patient.write (the ward clerk is 403) and writes clinical_access. The
     earlier bed taken by someone else is 409. A bed not recorded clean on a
     tracked ward needs its reason. The location read says whether the last
     move can be undone.
"""
import uuid

import pytest
from fastapi.testclient import TestClient

import config
import main
from auth import create_token, hash_password
from database import db_session
from services import bed_master_service as bm
from services import housekeeping_service as hk
from services.adt import transfer as adt_transfer
from services.audit_service import AuditWriteError

client = TestClient(main.app)
DEPT = "General Medicine"   # conftest seeds D001 and N001 in it
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
def adt_on(monkeypatch):
    monkeypatch.setattr(config, "ADT_ENABLED", True)
    yield


@pytest.fixture()
def adt_off(monkeypatch):
    monkeypatch.setattr(config, "ADT_ENABLED", False)
    yield


# ── helpers ──────────────────────────────────────────────────────────────────

def _tag() -> str:
    return uuid.uuid4().hex[:6].upper()


def _user(role: str, department: str | None = DEPT) -> dict:
    uid = f"SW{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO users (id, name, role, department, password_hash, is_active) "
            "VALUES (?, ?, ?, ?, ?, 1)",
            (uid, f"Swap {role} {uid[-4:]}", role, department, hash_password("password")),
        )
    return {"id": uid, "role": role, "department": department, "name": f"Swap {role}"}


_ADMIN: dict | None = None


def _admin() -> dict:
    global _ADMIN
    if _ADMIN is None:
        _ADMIN = _user("admin", None)
    return _ADMIN


def _hdr(user: dict, key: str | None = None) -> dict:
    out = {"Authorization": f"Bearer {create_token(user['id'], user['role'])}"}
    if key:
        out["Idempotency-Key"] = key
    return out


def _ward(department: str = DEPT, labels=("1", "2", "3"), *, tracked: bool = False) -> dict:
    ward = bm.save_ward({"code": f"SW-{_tag()}", "department": department,
                         "cleaning_tracked": tracked}, actor=_admin())
    if labels:
        bm.register_beds({"ward_id": ward["id"], "labels": list(labels)}, actor=_admin())
    return ward


def _bed(ward_id: str, label: str) -> dict:
    with db_session() as db:
        return dict(db.execute(
            "SELECT * FROM housekeeping_units WHERE ward_id = ? AND bed_label = ?",
            (ward_id, label)).fetchone())


def _ready(bed_id: str) -> None:
    hk.transition_unit(bed_id, "cleaning_in_progress", actor_user_id="SWHK", reason="test")
    hk.transition_unit(bed_id, "vacant_ready", actor_user_id="SWHK", reason="test")


def _one(sql: str, params: tuple = ()) -> dict | None:
    with db_session() as db:
        row = db.execute(sql, params).fetchone()
        return dict(row) if row else None


def _all(sql: str, params: tuple = ()) -> list[dict]:
    with db_session() as db:
        return [dict(r) for r in db.execute(sql, params).fetchall()]


def _admit(ward: dict, label: str, *, user: dict = DOCTOR, department: str = DEPT,
           isolation: list | None = None, extra: dict | None = None) -> dict:
    """An admission into a master bed through the admission route (module on)."""
    bed = _bed(ward["id"], label)
    body = {"name": f"Swap Patient {_tag()}", "age": 61, "sex": "F",
            "admission_source": "er", "admission_type": "inpatient",
            "department": department, "ward": ward["code"], "bed": label,
            "ward_id": ward["id"], "bed_id": bed["id"],
            "attending_doctor_id": "D001", "chief_complaint": "Fever",
            **({"isolation_precautions": isolation} if isolation else {}), **(extra or {})}
    resp = client.post("/api/admissions/admit", headers=_hdr(user, f"adm-{_tag()}"), json=body)
    assert resp.status_code == 201, resp.text
    out = resp.json()
    _SEEDED.append(out["patient_id"])
    return {"pid": out["patient_id"], "aid": out["admission_id"], "bed_id": bed["id"],
            "label": label}


def _adm(aid: str) -> dict:
    return _one("SELECT * FROM admissions WHERE id = ?", (aid,))


def _movements(admission_id: str) -> list[dict]:
    return _all("SELECT * FROM patient_movements WHERE admission_id = ? ORDER BY rowid",
                (admission_id,))


def _audits(action: str, patient_id: str, category: str | None = None) -> list[dict]:
    sql = "SELECT * FROM audit_logs WHERE action_type = ? AND patient_id = ?"
    params: tuple = (action, patient_id)
    if category:
        sql += " AND event_category = ?"
        params += (category,)
    return _all(sql, params)


def _swap(a: dict, b: dict, *, user: dict = DOCTOR, key: str | None = None, **over):
    body = {"first_admission_id": a["aid"], "second_admission_id": b["aid"],
            "expected_first_bed_id": a["bed_id"], "expected_second_bed_id": b["bed_id"],
            "reason": "Closer to the nursing station", **over}
    return client.post("/api/adt/bed-swaps", headers=_hdr(user, key or f"sw-{_tag()}"), json=body)


def _detail(resp) -> dict:
    return resp.json()["detail"]


def _mirror_ok(aid: str) -> None:
    """admissions.ward/bed are the master's spelling of ward_id/bed_id, and
    the patients mirror carries the current admission's place."""
    row = _one(
        """SELECT a.ward, a.bed, w.code, u.bed_label, p.ward AS p_ward, p.bed AS p_bed
             FROM admissions a JOIN wards w ON w.id = a.ward_id
             JOIN housekeeping_units u ON u.id = a.bed_id
             JOIN patients p ON p.id = a.patient_id WHERE a.id = ?""", (aid,))
    assert row is not None
    assert (row["ward"], row["bed"]) == (row["code"], row["bed_label"]), row
    assert (row["p_ward"], row["p_bed"]) == (row["code"], row["bed_label"]), row


# ═════════════════════════════════════════════════════════════════════════════
#  1. Flag off
# ═════════════════════════════════════════════════════════════════════════════

def test_flag_off_the_swap_and_correct_routes_answer_404(adt_off):
    swap = client.post("/api/adt/bed-swaps", headers=_hdr(DOCTOR, "k1"),
                       json={"first_admission_id": "A", "second_admission_id": "B"})
    assert swap.status_code == 404
    assert _detail(swap)["error"] == "module_disabled"
    fix = client.post("/api/adt/movements/MOV-X/correct", headers=_hdr(DOCTOR, "k2"),
                      json={"reason": "x", "expected_bed_id": None})
    assert fix.status_code == 404
    assert _detail(fix)["error"] == "module_disabled"


# ═════════════════════════════════════════════════════════════════════════════
#  2. A swap
# ═════════════════════════════════════════════════════════════════════════════

def test_a_swap_puts_each_patient_in_the_others_bed_with_two_rows(adt_on):
    ward = _ward()
    a, b = _admit(ward, "1"), _admit(ward, "2")
    key = f"sw-{_tag()}"
    resp = _swap(a, b, key=key)
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert out["replayed"] is False
    assert out["first"]["bed_id"] == b["bed_id"] and out["second"]["bed_id"] == a["bed_id"]
    assert _adm(a["aid"])["bed_id"] == b["bed_id"]
    assert _adm(b["aid"])["bed_id"] == a["bed_id"]
    assert _adm(a["aid"])["bed"] == "2" and _adm(b["aid"])["bed"] == "1"
    rows_a = [m for m in _movements(a["aid"]) if m["movement_type"] == "bed_swap"]
    rows_b = [m for m in _movements(b["aid"]) if m["movement_type"] == "bed_swap"]
    assert len(rows_a) == 1 and len(rows_b) == 1
    assert rows_a[0]["idempotency_key"] == key
    assert rows_b[0]["idempotency_key"] == f"{key}#2"
    assert (rows_a[0]["from_bed_id"], rows_a[0]["to_bed_id"]) == (a["bed_id"], b["bed_id"])
    assert (rows_b[0]["from_bed_id"], rows_b[0]["to_bed_id"]) == (b["bed_id"], a["bed_id"])
    assert rows_a[0]["reason"] == "Closer to the nursing station"
    # One fail-closed audit per patient, sharing one correlation id.
    audit_a = _audits("beds_swapped", a["pid"], "patient_movement")
    audit_b = _audits("beds_swapped", b["pid"], "patient_movement")
    assert len(audit_a) == 1 and len(audit_b) == 1
    assert audit_a[0]["correlation_id"] == audit_b[0]["correlation_id"]
    # The departments never change in a swap (ruling ADT-S8 held).
    assert _adm(a["aid"])["department"] == DEPT and _adm(b["aid"])["department"] == DEPT
    _mirror_ok(a["aid"])
    _mirror_ok(b["aid"])
    # One active admission per bed: the index holds.
    for bed_id in (a["bed_id"], b["bed_id"]):
        assert _one("SELECT COUNT(*) AS n FROM admissions WHERE bed_id = ? AND status = 'active'",
                    (bed_id,))["n"] == 1


def test_a_swap_across_two_wards_moves_the_ward_too(adt_on):
    one, two = _ward(), _ward()
    a, b = _admit(one, "1"), _admit(two, "3")
    resp = _swap(a, b)
    assert resp.status_code == 200, resp.text
    assert (_adm(a["aid"])["ward_id"], _adm(a["aid"])["bed"]) == (two["id"], "3")
    assert (_adm(b["aid"])["ward_id"], _adm(b["aid"])["bed"]) == (one["id"], "1")
    _mirror_ok(a["aid"])
    _mirror_ok(b["aid"])


# ═════════════════════════════════════════════════════════════════════════════
#  3. The screen's state and the key
# ═════════════════════════════════════════════════════════════════════════════

def test_a_retried_swap_returns_the_first_answer_and_never_swaps_back(adt_on):
    ward = _ward()
    a, b = _admit(ward, "1"), _admit(ward, "2")
    key = f"sw-{_tag()}"
    first = _swap(a, b, key=key)
    assert first.status_code == 200, first.text
    again = _swap(a, b, key=key)
    assert again.status_code == 200, again.text
    assert again.json()["replayed"] is True
    assert again.json()["first"]["movement"]["id"] == first.json()["first"]["movement"]["id"]
    assert _adm(a["aid"])["bed_id"] == b["bed_id"]
    assert _adm(b["aid"])["bed_id"] == a["bed_id"]
    assert len([m for m in _movements(a["aid"]) if m["movement_type"] == "bed_swap"]) == 1
    assert len(_audits("beds_swapped", a["pid"])) == 1


def test_a_fresh_key_with_stale_expected_beds_is_409_and_writes_nothing(adt_on):
    ward = _ward()
    a, b = _admit(ward, "1"), _admit(ward, "2")
    assert _swap(a, b).status_code == 200
    # The screen still shows the old beds: a second swap must not undo the first.
    stale = _swap(a, b)
    assert stale.status_code == 409
    assert _detail(stale)["error"] == "state_changed"
    assert _adm(a["aid"])["bed_id"] == b["bed_id"]
    assert len([m for m in _movements(a["aid"]) if m["movement_type"] == "bed_swap"]) == 1


def test_the_key_both_expected_beds_and_a_reason_are_required(adt_on):
    ward = _ward()
    a, b = _admit(ward, "1"), _admit(ward, "2")
    no_key = client.post("/api/adt/bed-swaps", headers=_hdr(DOCTOR), json={
        "first_admission_id": a["aid"], "second_admission_id": b["aid"],
        "expected_first_bed_id": a["bed_id"], "expected_second_bed_id": b["bed_id"],
        "reason": "x"})
    assert no_key.status_code == 422 and _detail(no_key)["error"] == "idempotency_key_required"
    one_bed = client.post("/api/adt/bed-swaps", headers=_hdr(DOCTOR, f"sw-{_tag()}"), json={
        "first_admission_id": a["aid"], "second_admission_id": b["aid"],
        "expected_first_bed_id": a["bed_id"], "reason": "x"})
    assert one_bed.status_code == 422 and _detail(one_bed)["error"] == "expected_bed_required"
    no_reason = _swap(a, b, reason="  ")
    assert no_reason.status_code == 422 and _detail(no_reason)["error"] == "reason_required"
    assert _adm(a["aid"])["bed_id"] == a["bed_id"]


def test_a_key_used_for_another_pair_is_409(adt_on):
    ward = _ward(labels=("1", "2", "3"))
    a, b, c = _admit(ward, "1"), _admit(ward, "2"), _admit(ward, "3")
    key = f"sw-{_tag()}"
    assert _swap(a, b, key=key).status_code == 200
    reused = _swap(a, c, key=key, expected_first_bed_id=b["bed_id"])
    assert reused.status_code == 409 and _detail(reused)["error"] == "idempotency_key_reused"
    assert _adm(c["aid"])["bed_id"] == c["bed_id"]


# ═════════════════════════════════════════════════════════════════════════════
#  4. Scope and permission
# ═════════════════════════════════════════════════════════════════════════════

def test_a_swap_with_an_admission_outside_scope_is_404_and_writes_nothing(adt_on):
    other_dept = f"Other Dept {_tag()}"
    other_doc = _user("doctor", other_dept)
    mine = _ward()
    theirs = _ward(department=other_dept)
    a = _admit(mine, "1")
    b = _admit(theirs, "1", user=other_doc, department=other_dept)
    resp = _swap(a, b)
    assert resp.status_code == 404
    assert _adm(a["aid"])["bed_id"] == a["bed_id"]
    assert _adm(b["aid"])["bed_id"] == b["bed_id"]
    assert [m for m in _movements(a["aid"]) if m["movement_type"] == "bed_swap"] == []
    assert _audits("patient_scope_denied", b["pid"], "authorization")


def test_the_ward_clerk_may_swap_and_a_nurse_may_not(adt_on):
    ward = _ward()
    a, b = _admit(ward, "1"), _admit(ward, "2")
    nurse = _user("nurse")
    assert _swap(a, b, user=nurse).status_code == 403
    clerk = _user("ward_clerk")
    resp = _swap(a, b, user=clerk)
    assert resp.status_code == 200, resp.text


# ═════════════════════════════════════════════════════════════════════════════
#  5. The rules
# ═════════════════════════════════════════════════════════════════════════════

def test_two_different_patients_both_in_beds_of_bed_setup(adt_on):
    ward = _ward()
    a = _admit(ward, "1")
    same = _swap(a, a)
    assert same.status_code == 422 and _detail(same)["error"] == "swap_needs_two"
    # A patient awaiting a bed has no bed to give.
    body = {"name": f"Awaiting {_tag()}", "age": 40, "sex": "M", "admission_source": "er",
            "admission_type": "inpatient", "department": DEPT, "ward": ward["code"], "bed": "",
            "ward_id": ward["id"], "awaiting_bed": True, "attending_doctor_id": "D001",
            "chief_complaint": "Cough"}
    resp = client.post("/api/admissions/admit", headers=_hdr(DOCTOR, f"adm-{_tag()}"), json=body)
    assert resp.status_code == 201, resp.text
    _SEEDED.append(resp.json()["patient_id"])
    waiting = {"aid": resp.json()["admission_id"], "bed_id": None}
    refused = _swap(a, waiting)
    assert refused.status_code == 409 and _detail(refused)["error"] == "swap_needs_beds"
    assert _adm(a["aid"])["bed_id"] == a["bed_id"]


def test_a_swap_onto_another_departments_ward_needs_the_boarder_acknowledgement(adt_on):
    other_dept = f"Board Dept {_tag()}"
    _user("doctor", other_dept)
    mine = _ward()
    host = _ward(department=other_dept)
    a = _admit(mine, "1")
    # A General Medicine patient already boarding on the other department's ward.
    boarder_body = {"boarder_acknowledged": True}
    b = _admit(host, "1", extra=boarder_body)
    refused = _swap(a, b)
    assert refused.status_code == 409
    detail = _detail(refused)
    assert detail["error"] == "boarder_acknowledgement_required"
    assert "will not be able to open" in detail["message"]
    assert _adm(a["aid"])["bed_id"] == a["bed_id"]
    resp = _swap(a, b, boarder_acknowledged=True)
    assert resp.status_code == 200, resp.text
    assert resp.json()["first"]["boarder"] is True
    placed = [r for r in _audits("boarder_placed", a["pid"], "clinical_access")
              if "bed swap" in (r.get("summary_message") or "")]
    assert len(placed) == 1
    room = _one("SELECT id FROM channels WHERE patient_id = ? AND type = 'case_room'", (a["pid"],))
    if room:
        lines = _all("SELECT content FROM messages WHERE channel_id = ? AND content LIKE ?",
                     (room["id"], "%in a bed swap%"))
        assert len(lines) == 1


def test_on_a_tracked_ward_the_swap_needs_the_not_cleaned_reason(adt_on, monkeypatch):
    monkeypatch.setattr(config, "HOUSEKEEPING_ENABLED", True)
    ward = _ward(labels=("1", "2"), tracked=True)
    for label in ("1", "2"):
        _ready(_bed(ward["id"], label)["id"])
    a, b = _admit(ward, "1"), _admit(ward, "2")
    refused = _swap(a, b)
    assert refused.status_code == 409
    assert _detail(refused)["error"] == "bed_not_clean_reason_required"
    assert "Neither bed is cleaned" in _detail(refused)["message"]
    resp = _swap(a, b, not_clean_reason="Linen changed at the bedside")
    assert resp.status_code == 200, resp.text
    row = [m for m in _movements(a["aid"]) if m["movement_type"] == "bed_swap"][0]
    assert '"not_clean_reason": "Linen changed at the bedside"' in row["detail_json"]


def test_a_patient_under_isolation_makes_the_others_new_bed_terminal(adt_on, monkeypatch):
    monkeypatch.setattr(config, "HOUSEKEEPING_ENABLED", True)
    ward = _ward(labels=("1", "2"), tracked=True)
    for label in ("1", "2"):
        _ready(_bed(ward["id"], label)["id"])
    a = _admit(ward, "1", isolation=["contact"])
    b = _admit(ward, "2")
    refused = _swap(a, b, not_clean_reason="Linen changed")
    assert refused.status_code == 409
    assert _detail(refused)["error"] == "terminal_clean_authorisation_required"
    # A name alone is not enough; the sender may authorise it themself.
    named = _swap(a, b, not_clean_reason="Linen changed", terminal_clean_authorised_by="N001",
                  terminal_clean_reason="Cohorted, both contact precautions")
    assert named.status_code == 409 and _detail(named)["error"] == "terminal_clean_pin_required"
    resp = _swap(a, b, not_clean_reason="Linen changed", terminal_clean_authorised_by="D001",
                 terminal_clean_reason="Cohorted, both contact precautions")
    assert resp.status_code == 200, resp.text
    row_b = [m for m in _movements(b["aid"]) if m["movement_type"] == "bed_swap"][0]
    assert '"confirmed_by": "own_session"' in row_b["detail_json"]


def test_an_unwritable_audit_rolls_the_swap_back(adt_on, monkeypatch):
    ward = _ward()
    a, b = _admit(ward, "1"), _admit(ward, "2")
    real = adt_transfer.audit_on

    def _refuse(db, **kwargs):
        if kwargs.get("action") == "beds_swapped":
            raise AuditWriteError("audit store unavailable")
        return real(db, **kwargs)

    monkeypatch.setattr(adt_transfer, "audit_on", _refuse)
    resp = _swap(a, b)
    assert resp.status_code == 503
    assert _detail(resp)["error"] == "audit_unavailable"
    assert _adm(a["aid"])["bed_id"] == a["bed_id"]
    assert _adm(b["aid"])["bed_id"] == b["bed_id"]
    assert [m for m in _movements(a["aid"]) if m["movement_type"] == "bed_swap"] == []


def test_after_a_swap_no_turnaround_is_left_open_on_either_bed(adt_on, monkeypatch):
    monkeypatch.setattr(config, "HOUSEKEEPING_ENABLED", True)
    ward = _ward(labels=("1", "2"), tracked=True)
    for label in ("1", "2"):
        _ready(_bed(ward["id"], label)["id"])
    a, b = _admit(ward, "1"), _admit(ward, "2")
    hk.sync_adt_movements()
    assert _swap(a, b, not_clean_reason="Linen changed").status_code == 200
    hk.sync_adt_movements()
    for bed_id in (a["bed_id"], b["bed_id"]):
        open_tasks = _all("SELECT id FROM housekeeping_tasks WHERE unit_id = ? "
                          "AND status IN ('open', 'in_progress')", (bed_id,))
        assert open_tasks == [], f"a turnaround is open on a bed a patient lies in: {bed_id}"


# ═════════════════════════════════════════════════════════════════════════════
#  6. A move recorded in error
# ═════════════════════════════════════════════════════════════════════════════

def _change_bed(p: dict, to_bed: dict, *, user: dict = DOCTOR, key: str | None = None):
    return client.post(f"/api/adt/admissions/{p['aid']}/bed-change",
                       headers=_hdr(user, key or f"bc-{_tag()}"),
                       json={"bed_id": to_bed["id"], "expected_bed_id": _adm(p["aid"])["bed_id"],
                             "reason": "Nearer the window"})


def _correct(movement_id: str, p: dict, *, user: dict = DOCTOR, key: str | None = None, **over):
    body = {"expected_bed_id": _adm(p["aid"])["bed_id"], "reason": "Wrong patient chosen", **over}
    return client.post(f"/api/adt/movements/{movement_id}/correct",
                       headers=_hdr(user, key or f"fix-{_tag()}"), json=body)


def test_a_bed_change_recorded_in_error_is_undone_and_both_rows_remain(adt_on):
    ward = _ward()
    p = _admit(ward, "1")
    moved = _change_bed(p, _bed(ward["id"], "2"))
    assert moved.status_code == 200, moved.text
    mov_id = moved.json()["movement"]["id"]
    resp = _correct(mov_id, p)
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert out["corrects_movement_id"] == mov_id
    assert _adm(p["aid"])["bed_id"] == p["bed_id"]
    _mirror_ok(p["aid"])
    rows = _movements(p["aid"])
    kinds = [m["movement_type"] for m in rows]
    assert kinds[-2:] == ["bed_change", "correction"]
    assert rows[-1]["corrects_movement_id"] == mov_id
    assert rows[-1]["reason"].startswith("Recorded in error: ")
    assert rows[-2]["to_bed_id"] == _bed(ward["id"], "2")["id"], "the old row was changed"
    assert _audits("movement_corrected", p["pid"], "patient_movement")


def test_only_the_latest_move_only_once_and_never_a_swap(adt_on):
    ward = _ward(labels=("1", "2", "3"))
    p = _admit(ward, "1")
    first = _change_bed(p, _bed(ward["id"], "2")).json()["movement"]["id"]
    second = _change_bed(p, _bed(ward["id"], "3")).json()["movement"]["id"]
    not_latest = _correct(first, p)
    assert not_latest.status_code == 409 and _detail(not_latest)["error"] == "not_latest_movement"
    assert _correct(second, p).status_code == 200
    twice = _correct(second, p)
    assert twice.status_code == 409 and _detail(twice)["error"] == "already_corrected"
    q, r = _admit(ward, "3"), None
    r = _admit(_ward(), "1")
    swapped = _swap(q, r)
    assert swapped.status_code == 200, swapped.text
    swap_mov = swapped.json()["first"]["movement"]["id"]
    refused = _correct(swap_mov, q)
    assert refused.status_code == 409 and _detail(refused)["error"] == "not_correctable"
    assert "swapping the two patients back" in _detail(refused)["message"]


def test_a_retried_correction_returns_the_first_and_writes_one_row(adt_on):
    ward = _ward()
    p = _admit(ward, "1")
    mov_id = _change_bed(p, _bed(ward["id"], "2")).json()["movement"]["id"]
    key = f"fix-{_tag()}"
    first = _correct(mov_id, p, key=key)
    assert first.status_code == 200, first.text
    again = client.post(f"/api/adt/movements/{mov_id}/correct", headers=_hdr(DOCTOR, key),
                        json={"expected_bed_id": _bed(ward["id"], "2")["id"],
                              "reason": "Wrong patient chosen"})
    assert again.status_code == 200, again.text
    assert again.json()["replayed"] is True
    assert len([m for m in _movements(p["aid"]) if m["movement_type"] == "correction"]) == 1


def test_the_earlier_bed_taken_since_is_409(adt_on):
    ward = _ward(labels=("1", "2"))
    p = _admit(ward, "1")
    mov_id = _change_bed(p, _bed(ward["id"], "2")).json()["movement"]["id"]
    _admit(ward, "1")   # somebody else is put in bed 1
    resp = _correct(mov_id, p)
    assert resp.status_code == 409
    assert _detail(resp)["error"] == "bed_occupied"
    assert "cannot be undone" in _detail(resp)["message"]
    assert _adm(p["aid"])["bed_id"] == _bed(ward["id"], "2")["id"]


def test_returning_to_a_bed_not_recorded_clean_needs_its_reason(adt_on, monkeypatch):
    monkeypatch.setattr(config, "HOUSEKEEPING_ENABLED", True)
    ward = _ward(labels=("1", "2"), tracked=True)
    for label in ("1", "2"):
        _ready(_bed(ward["id"], label)["id"])
    p = _admit(ward, "1")
    moved = client.post(f"/api/adt/admissions/{p['aid']}/bed-change", headers=_hdr(DOCTOR, f"bc-{_tag()}"),
                        json={"bed_id": _bed(ward["id"], "2")["id"], "expected_bed_id": p["bed_id"],
                              "reason": "Nearer the window"})
    assert moved.status_code == 200, moved.text
    mov_id = moved.json()["movement"]["id"]
    refused = _correct(mov_id, p)
    assert refused.status_code == 409
    assert _detail(refused)["error"] == "bed_not_clean_reason_required"
    resp = _correct(mov_id, p, not_clean_reason="The patient never left the bed")
    assert resp.status_code == 200, resp.text


def test_undoing_a_transfer_hands_the_chart_back_and_needs_the_transfer_permission(adt_on):
    recv_dept = f"Recv Dept {_tag()}"
    recv_doc = _user("doctor", recv_dept)
    _user("nurse", recv_dept)
    home = _ward()
    recv_ward = _ward(department=recv_dept)
    p = _admit(home, "1")
    moved = client.post("/api/adt/transfers", headers=_hdr(DOCTOR, f"tr-{_tag()}"), json={
        "admission_id": p["aid"], "to_department": recv_dept,
        "bed_id": _bed(recv_ward["id"], "1")["id"], "expected_bed_id": p["bed_id"],
        "reason": "Surgical review"})
    assert moved.status_code == 200, moved.text
    mov_id = moved.json()["movement"]["id"]
    assert _adm(p["aid"])["department"] == recv_dept
    # The receiving ward's clerk holds adt.bed only: no department change.
    clerk = _user("ward_clerk", recv_dept)
    clerk_try = _correct(mov_id, p, user=clerk)
    assert clerk_try.status_code == 403
    assert _detail(clerk_try)["error"] == "transfer_permission_required"
    resp = _correct(mov_id, p, user=recv_doc)
    assert resp.status_code == 200, resp.text
    assert resp.json()["department_changed"] is True
    assert _adm(p["aid"])["department"] == DEPT
    assert _adm(p["aid"])["bed_id"] == p["bed_id"]
    assert len(_audits("patient_transfer_corrected", p["pid"], "clinical_access")) == 1
    # Strict handover back: the receiving department no longer sees the chart.
    loc = client.get(f"/api/adt/admissions/{p['aid']}/location", headers=_hdr(recv_doc))
    assert loc.status_code == 404
    mine = client.get(f"/api/adt/admissions/{p['aid']}/location", headers=_hdr(DOCTOR))
    assert mine.status_code == 200


def test_the_location_read_says_whether_the_last_move_can_be_undone(adt_on):
    ward = _ward(labels=("1", "2"))
    p = _admit(ward, "1")
    before = client.get(f"/api/adt/admissions/{p['aid']}/location", headers=_hdr(DOCTOR)).json()
    assert before["last_move"]["movement_type"] == "admission"
    assert before["last_move"]["correctable"] is False
    mov_id = _change_bed(p, _bed(ward["id"], "2")).json()["movement"]["id"]
    after = client.get(f"/api/adt/admissions/{p['aid']}/location", headers=_hdr(DOCTOR)).json()
    assert after["last_move"]["id"] == mov_id
    assert after["last_move"]["correctable"] is True
    assert after["last_move"]["from"]["bed"] == "1" and after["last_move"]["to"]["bed"] == "2"
    assert after["last_move"]["changes_department"] is False


def test_a_correction_outside_scope_is_404(adt_on):
    other_dept = f"Scope Dept {_tag()}"
    other_doc = _user("doctor", other_dept)
    ward = _ward(labels=("1", "2"))
    p = _admit(ward, "1")
    mov_id = _change_bed(p, _bed(ward["id"], "2")).json()["movement"]["id"]
    resp = _correct(mov_id, p, user=other_doc)
    assert resp.status_code == 404
    assert _adm(p["aid"])["bed_id"] == _bed(ward["id"], "2")["id"]
    unknown = _correct("MOV-NOT-THERE", p)
    assert unknown.status_code == 404
