"""The bed board, the awaiting and unlinked lists, and available beds
(adt-4a, W4-B, 4 October 2026).

What this file defends:

  1. THE PRECEDENCE TABLE (design section 4). An occupied bed housekeeping
     marks out of service shows the occupant and the marker; a bed vacated
     while housekeeping still says vacant_ready from before the departure
     needs cleaning, with no sync run; housekeeping 'occupied' with nobody in
     the bed needs cleaning; available only when vacant_ready and no newer
     vacating movement; reserved sits above out of service.
  2. CLEANING NOT TRACKED. With ORB_HOUSEKEEPING off, or the ward's
     cleaning_tracked 0, a vacant bed reads 'Vacant, cleaning not tracked'
     and no bed reads 'Needs cleaning'.
  3. IDENTITY. An occupant outside the caller's department is never named:
     the tile says 'Occupied' or 'Boarder' and carries no patient field. A
     read that names anybody writes exactly one phi_access row with the
     patient ids; a read that names nobody, and the available-beds read,
     write none. An unwritable audit refuses the read (503).
  4. LISTS. A typed-bed admission is under Unlinked, never Awaiting; an
     admission with no bed is under Awaiting; other departments' patients are
     on neither.
  5. WHO AND WHEN. 404 with ORB_ADT off; 403 for a nurse (no adt.read); 404
     for a ward the caller may not see.
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import config
import main
from auth import create_token, hash_password
from database import db_session
from services import bed_master_service as bm
from services import housekeeping_service as hk
from services.adt import core as adt_core
from services.adt import end_stay as adt_end
from services.audit_service import AuditWriteError

client = TestClient(main.app)
DEPT = "General Medicine"   # conftest seeds doctors and a nurse in it


# ── helpers ──────────────────────────────────────────────────────────────────

def _tag() -> str:
    return uuid.uuid4().hex[:6].upper()


def _user(role: str, department: str | None = DEPT) -> dict:
    uid = f"BB{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO users (id, name, role, department, password_hash, is_active) "
            "VALUES (?, ?, ?, ?, ?, 1)",
            (uid, f"Board {role}", role, department, hash_password("password")),
        )
    return {"id": uid, "role": role, "department": department}


def _hdr(user: dict) -> dict:
    return {"Authorization": f"Bearer {create_token(user['id'], user['role'])}"}


_ADMIN: dict | None = None


def _admin() -> dict:
    global _ADMIN
    if _ADMIN is None:
        _ADMIN = _user("admin", None)
    return _ADMIN


def _ward(department: str = DEPT, labels=("1", "2", "3"), **extra) -> dict:
    ward = bm.save_ward({"code": f"BB-{_tag()}", "department": department, **extra}, actor=_admin())
    if labels:
        bm.register_beds({"ward_id": ward["id"], "labels": list(labels)}, actor=_admin())
    return ward


def _bed(ward_id: str, label: str) -> dict:
    with db_session() as db:
        return dict(db.execute("SELECT * FROM housekeeping_units WHERE ward_id = ? AND bed_label = ?",
                               (ward_id, label)).fetchone())


def _ready(bed_id: str) -> None:
    """Housekeeping cleans a registered (vacant_dirty) bed through to ready."""
    hk.transition_unit(bed_id, "cleaning_in_progress", actor_user_id="BBHK", reason="test")
    hk.transition_unit(bed_id, "vacant_ready", actor_user_id="BBHK", reason="test")


def _admit(*, department: str = DEPT, ward_text: str | None = None,
           bed_text: str | None = None, name: str | None = None) -> dict:
    pid = f"PBB{uuid.uuid4().hex[:8].upper()}"
    aid = f"ADM-BB{uuid.uuid4().hex[:8].upper()}"
    name = name or f"Board Patient {pid[-6:]}"
    with db_session() as db:
        db.execute(
            "INSERT INTO patients (id, name, mrn, ward, bed, status, admission_date) "
            "VALUES (?, ?, ?, ?, ?, 'admitted', CURRENT_TIMESTAMP)",
            (pid, name, f"MRN-{pid}", ward_text, bed_text),
        )
        db.execute(
            "INSERT INTO admissions (id, patient_id, admitted_by, department, ward, bed, status) "
            "VALUES (?, ?, 'D001', ?, ?, ?, 'active')",
            (aid, pid, department, ward_text, bed_text),
        )
    return {"pid": pid, "aid": aid, "mrn": f"MRN-{pid}", "name": name}


def _place(p: dict, ward: dict, label: str, **kw) -> None:
    with db_session(immediate=True) as db:
        adt_core.locate_admission_on(db, admission_id=p["aid"], actor=_admin(),
                                     movement_type="bed_assigned", reason="test",
                                     ward_id=ward["id"], bed_id=_bed(ward["id"], label)["id"], **kw)


def _board(user: dict, ward_id: str | None = None) -> dict:
    resp = client.get("/api/adt/board", params={"ward_id": ward_id} if ward_id else None,
                      headers=_hdr(user))
    assert resp.status_code == 200, resp.text
    return resp.json()


def _tiles(board: dict, ward_id: str) -> dict:
    ward = next(w for w in board["wards"] if w["id"] == ward_id)
    return {t["bed_label"]: t for t in ward["tiles"]}


def _ago(**delta) -> str:
    return (datetime.now(timezone.utc) - timedelta(**delta)).isoformat()


def _audits(action: str, actor_id: str) -> list[dict]:
    with db_session() as db:
        return [dict(r) for r in db.execute(
            "SELECT * FROM audit_logs WHERE action_type = ? AND actor_user_id = ?",
            (action, actor_id)).fetchall()]


@pytest.fixture()
def adt_on(monkeypatch):
    monkeypatch.setattr(config, "ADT_ENABLED", True)
    monkeypatch.setattr(config, "HOUSEKEEPING_ENABLED", True)
    yield


@pytest.fixture()
def clerk() -> dict:
    return _user("ward_clerk")


# ═════════════════════════════════════════════════════════════════════════════
#  1. The precedence table
# ═════════════════════════════════════════════════════════════════════════════

_WARD = {"cleaning_tracked": 1}


@pytest.mark.parametrize("kw, expected", [
    # occupancy outranks out of service, with the marker
    ({"state": "out_of_service", "occupied": True}, ("occupied", ["Bed reported out of service"])),
    ({"state": "out_of_service", "occupied": True, "presence": "on_leave"},
     ("occupied_on_leave", ["Bed reported out of service"])),
    # reservation outranks out of service, with the marker
    ({"state": "out_of_service", "reserved": True}, ("reserved", ["Bed reported out of service"])),
    ({"state": "out_of_service"}, ("out_of_service", [])),
    ({"state": "vacant_dirty"}, ("needs_cleaning", [])),
    ({"state": "occupied"}, ("needs_cleaning", [])),
    ({"state": "cleaning_in_progress"}, ("cleaning", [])),
    ({"state": "vacant_ready", "ready_jd": 10.0}, ("available", [])),
    ({"state": "vacant_ready", "ready_jd": 10.0, "vacated_jd": 9.0}, ("available", [])),
    ({"state": "vacant_ready", "ready_jd": 10.0, "vacated_jd": 11.0}, ("needs_cleaning", [])),
    ({"state": "vacant_ready", "state_jd": 10.0, "vacated_jd": 11.0}, ("needs_cleaning", [])),
    # Order unknowable (the same millisecond): never offered as clean.
    ({"state": "vacant_ready", "ready_jd": 10.0, "vacated_jd": 10.0}, ("needs_cleaning", [])),
])
def test_the_precedence_table(monkeypatch, kw, expected):
    monkeypatch.setattr(config, "HOUSEKEEPING_ENABLED", True)
    bed = {"state": kw.pop("state"), "is_active": 1}
    status, markers, _since = bm._derive_status(
        bed, _WARD, occupied=kw.get("occupied", False), presence=kw.get("presence"),
        reserved=kw.get("reserved", False), ready_jd=kw.get("ready_jd"),
        vacated_jd=kw.get("vacated_jd"), state_jd=kw.get("state_jd"))
    assert (status, markers) == expected


def test_an_occupied_bed_marked_out_of_service_shows_the_occupant_and_the_marker(adt_on):
    ward = _ward()
    p = _admit()
    _place(p, ward, "1")
    hk.transition_unit(_bed(ward["id"], "1")["id"], "out_of_service", actor_user_id="BBHK",
                       reason="Broken rail")
    admin = _admin()
    tile = _tiles(_board(admin, ward["id"]), ward["id"])["1"]
    assert tile["status"] == "occupied"
    assert tile["status_words"] == "Occupied, bed reported out of service"
    assert tile["markers"] == ["Bed reported out of service"]
    assert tile["occupant"]["name"] == p["name"] and tile["occupant"]["mrn"] == p["mrn"]


def test_a_bed_vacated_before_housekeeping_caught_up_needs_cleaning(adt_on):
    ward = _ward()
    bed = _bed(ward["id"], "1")
    _ready(bed["id"])
    # The clean finished a minute ago (this database works faster than a
    # millisecond, and julianday() keeps milliseconds).
    with db_session() as db:
        db.execute("UPDATE housekeeping_unit_events SET occurred_at = ? WHERE unit_id = ? "
                   "AND to_state = 'vacant_ready'",
                   (_ago(minutes=1), bed["id"]))
    p = _admit()
    _place(p, ward, "1")
    # Housekeeping never heard: it still says vacant_ready from before.
    assert _bed(ward["id"], "1")["state"] == "vacant_ready"
    ended = adt_end.end_stay(p["aid"], end_type="routine", actor={"id": "D001", "role": "doctor"},
                             confirm_mrn=p["mrn"])
    tile = _tiles(_board(_admin(), ward["id"]), ward["id"])["1"]
    assert tile["status"] == "needs_cleaning" and tile["status_words"] == "Needs cleaning"
    assert tile["free"] is False and tile["occupant"] is None
    # Cleaned after the departure (the patient left thirty seconds ago):
    # available again.
    with db_session() as db:
        db.execute("UPDATE patient_movements SET moved_at = ? WHERE id = ?",
                   (_ago(seconds=30), ended["movement_id"]))
    hk.transition_unit(bed["id"], "vacant_dirty", actor_user_id="BBHK", reason="test")
    _ready(bed["id"])
    tile = _tiles(_board(_admin(), ward["id"]), ward["id"])["1"]
    assert tile["status"] == "available" and tile["free"] is True


def test_housekeeping_occupied_with_nobody_in_the_bed_needs_cleaning(adt_on):
    ward = _ward()
    bed = _bed(ward["id"], "2")
    _ready(bed["id"])
    hk.transition_unit(bed["id"], "occupied", actor_user_id="BBHK", reason="test")
    tiles = _tiles(_board(_admin(), ward["id"]), ward["id"])
    assert tiles["2"]["status"] == "needs_cleaning"
    assert tiles["1"]["status"] == "needs_cleaning"   # registered beds start dirty
    assert tiles["3"]["status"] == "needs_cleaning"


def test_a_typed_occupant_holds_the_bed_and_is_marked_not_linked(adt_on):
    ward = _ward()
    p = _admit(ward_text=ward["code"], bed_text="3")
    tile = _tiles(_board(_admin(), ward["id"]), ward["id"])["3"]
    assert tile["status"] == "occupied" and "Not linked to bed setup" in tile["markers"]
    assert tile["occupant"]["patient_id"] == p["pid"] and tile["occupant"]["linked"] is False


# ═════════════════════════════════════════════════════════════════════════════
#  2. Cleaning not tracked
# ═════════════════════════════════════════════════════════════════════════════

def test_housekeeping_off_reads_cleaning_not_tracked_and_warns_nothing(adt_on, monkeypatch):
    ward = _ward()
    _ready(_bed(ward["id"], "2")["id"])
    monkeypatch.setattr(config, "HOUSEKEEPING_ENABLED", False)
    board = _board(_admin(), ward["id"])
    tiles = _tiles(board, ward["id"])
    assert {t["status"] for t in tiles.values()} == {"vacant_untracked"}
    assert all(t["status_words"] == "Vacant, cleaning not tracked" for t in tiles.values())
    assert all(t["housekeeping_state"] is None and t["free"] for t in tiles.values())
    assert board["wards"][0]["cleaning_tracked"] is False
    assert "needs_cleaning" not in board["wards"][0]["counts"]


def test_a_ward_that_does_not_track_cleaning_warns_nothing(adt_on):
    ward = _ward(cleaning_tracked=False)
    tiles = _tiles(_board(_admin(), ward["id"]), ward["id"])
    assert {t["status"] for t in tiles.values()} == {"vacant_untracked"}
    out = client.get("/api/adt/beds/available", params={"ward_id": ward["id"]},
                     headers=_hdr(_admin())).json()
    assert {b["bed_label"] for b in out["beds"]} == {"1", "2", "3"} and out["not_ready"] == []


# ═════════════════════════════════════════════════════════════════════════════
#  3. Identity and its audit
# ═════════════════════════════════════════════════════════════════════════════

def _cover(department: str) -> None:
    """A nurse in `department`, so a ward can be set up there."""
    _user("nurse", department=department)


def test_an_out_of_scope_occupant_is_never_named(adt_on, clerk):
    other = f"Cardiology {_tag()}"
    ward = _ward()                                  # a General Medicine ward
    boarder = _admit(department=other, name="Boarder Outsider")
    _place(boarder, ward, "1", boarder_acknowledged=True)
    own = _admit(name="Own Inpatient")
    _place(own, ward, "2")
    before = len(_audits("bed_board_viewed", clerk["id"]))
    board = _board(clerk, ward["id"])
    tiles = _tiles(board, ward["id"])
    hidden = tiles["1"]
    assert hidden["status"] == "boarder" and hidden["status_words"] == "Boarder"
    assert hidden["occupant"] is None and hidden["occupant_hidden"] is True
    assert hidden["since"] is None and hidden["minutes_in_state"] is None
    blob = str(hidden)
    for secret in (boarder["pid"], boarder["aid"], boarder["mrn"], "Boarder Outsider", other):
        assert secret not in blob
    assert tiles["2"]["occupant"]["name"] == "Own Inpatient"
    audits = _audits("bed_board_viewed", clerk["id"])
    assert len(audits) == before + 1
    assert audits[-1]["event_category"] == "phi_access"
    assert own["pid"] in audits[-1]["metadata_json"] and boarder["pid"] not in audits[-1]["metadata_json"]


def test_an_own_patient_boarding_elsewhere_is_named_and_that_ward_is_visible(adt_on, clerk):
    other = f"Ortho {_tag()}"
    _cover(other)
    host = _ward(department=other)
    mine = _admit(name="Mine Boarding")
    _place(mine, host, "1", boarder_acknowledged=True)
    theirs = _admit(department=other, name="Their Patient")
    _place(theirs, host, "2")
    board = _board(clerk, host["id"])
    tiles = _tiles(board, host["id"])
    assert tiles["1"]["occupant"]["name"] == "Mine Boarding"
    assert tiles["1"]["occupant"]["boarder_from"] == DEPT
    assert tiles["2"]["status"] == "occupied" and tiles["2"]["occupant"] is None
    assert "Their Patient" not in str(board)


def test_a_read_that_names_nobody_writes_no_audit(adt_on, clerk):
    ward = _ward()
    before = len(_audits("bed_board_viewed", clerk["id"]))
    _board(clerk, ward["id"])
    client.get("/api/adt/beds/available", params={"ward_id": ward["id"]}, headers=_hdr(clerk))
    assert len(_audits("bed_board_viewed", clerk["id"])) == before


def test_an_unwritable_audit_refuses_the_board(adt_on, monkeypatch):
    ward = _ward()
    _place(_admit(), ward, "1")
    real = bm.log_audit_event

    def broken(*args, **kwargs):
        if kwargs.get("action_type") == "bed_board_viewed":
            raise AuditWriteError("audit table unwritable")
        return real(*args, **kwargs)

    monkeypatch.setattr(bm, "log_audit_event", broken)
    resp = client.get("/api/adt/board", params={"ward_id": ward["id"]}, headers=_hdr(_admin()))
    assert resp.status_code == 503 and resp.json()["detail"]["error"] == "audit_unavailable"


# ═════════════════════════════════════════════════════════════════════════════
#  4. Awaiting a bed, and unlinked
# ═════════════════════════════════════════════════════════════════════════════

def test_typed_beds_are_unlinked_never_awaiting(adt_on, clerk):
    ward = _ward()
    waiting = _admit(ward_text=ward["code"])
    typed_unknown = _admit(ward_text=ward["code"], bed_text="99")
    typed_match = _admit(ward_text=ward["code"], bed_text="2")
    outsider = _admit(department=f"Elsewhere {_tag()}", ward_text=ward["code"])
    resp = client.get("/api/adt/awaiting-bed", params={"ward_id": ward["id"]}, headers=_hdr(clerk))
    assert resp.status_code == 200, resp.text
    out = resp.json()
    awaiting = {e["admission_id"]: e for e in out["awaiting"]}
    unlinked = {e["admission_id"]: e for e in out["unlinked"]}
    assert waiting["aid"] in awaiting and waiting["aid"] not in unlinked
    assert awaiting[waiting["aid"]]["name"] == waiting["name"]
    for typed in (typed_unknown, typed_match):
        assert typed["aid"] in unlinked and typed["aid"] not in awaiting
    assert unlinked[typed_unknown["aid"]]["reason"] == "bed_not_in_setup"
    assert unlinked[typed_match["aid"]]["reason"] == "matches_bed"
    assert unlinked[typed_match["aid"]]["reason_words"]
    assert outsider["aid"] not in awaiting and outsider["aid"] not in unlinked
    audit = _audits("awaiting_bed_viewed", clerk["id"])[-1]
    assert audit["event_category"] == "phi_access" and outsider["pid"] not in audit["metadata_json"]


def test_available_beds_lists_free_beds_and_says_why_others_are_not_ready(adt_on):
    ward = _ward()
    _ready(_bed(ward["id"], "1")["id"])
    hk.transition_unit(_bed(ward["id"], "2")["id"], "cleaning_in_progress", actor_user_id="BBHK",
                       reason="test")
    _place(_admit(), ward, "3")
    out = client.get("/api/adt/beds/available", params={"ward_id": ward["id"]},
                     headers=_hdr(_admin())).json()
    assert [b["bed_label"] for b in out["beds"]] == ["1"]
    assert {b["bed_label"]: b["status"] for b in out["not_ready"]} == {"2": "cleaning"}
    assert all("patient" not in str(k) for b in out["beds"] + out["not_ready"] for k in b)


# ═════════════════════════════════════════════════════════════════════════════
#  5. Who and when
# ═════════════════════════════════════════════════════════════════════════════

def test_the_board_is_404_with_adt_off(monkeypatch):
    monkeypatch.setattr(config, "ADT_ENABLED", False)
    for path in ("/api/adt/board", "/api/adt/awaiting-bed", "/api/adt/beds/available"):
        resp = client.get(path, headers=_hdr(_admin()))
        assert resp.status_code == 404 and resp.json()["detail"]["error"] == "module_disabled"


def test_a_nurse_holds_no_board(adt_on):
    resp = client.get("/api/adt/board", headers=_hdr(_user("nurse")))
    assert resp.status_code == 403


def test_another_departments_ward_is_404_for_a_scoped_account(adt_on, clerk):
    other = f"Neuro {_tag()}"
    _cover(other)
    theirs = _ward(department=other)
    assert client.get("/api/adt/board", params={"ward_id": theirs["id"]},
                      headers=_hdr(clerk)).status_code == 404
    # An unscoped administrator sees it.
    assert _board(_admin(), theirs["id"])["wards"][0]["id"] == theirs["id"]
    # And the clerk's own wards are their default board.
    mine = _ward()
    board = _board(clerk)
    assert mine["id"] in {w["id"] for w in board["wards"]}
    assert theirs["id"] not in {w["id"] for w in board["wards"]}
    assert theirs["id"] not in {w["id"] for w in board["ward_choices"]}


def test_no_board_word_carries_an_em_dash():
    import inspect

    from routes.adt import board as route

    words = [w for w, _tone in bm.BOARD_STATUSES.values()]
    sources = [inspect.getsource(fn) for fn in (
        bm.bed_board, bm.available_beds, bm.awaiting_bed, bm._board_on, bm._derive_status)]
    sources.append(inspect.getsource(route))
    for text in words + sources:
        assert "\u2014" not in text
    assert all(w[:1].isupper() and w[1:] == w[1:].lower() or "," in w for w in words)
