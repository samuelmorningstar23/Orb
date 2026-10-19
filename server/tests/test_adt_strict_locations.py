"""Strict locations (adt-16, HIS wave 9, W9-A; ORB_ADT_STRICT_LOCATIONS, ruling F104).

F104 as ruled: a ward or bed not in bed setup is admitted unlinked and said so
until the bed manager has set the hospital up; then the site turns strict on.
With strict on, a ward bed setup does not hold is refused (400, with words) on
every lane that names a place, and a bed bed setup does not hold is refused on
a ward whose register is complete (a ward still being set up keeps the typed
bed unlinked, with the warning). The place an admission is already recorded at
is never refused.

What this file defends:

  1. STRICT OFF KEEPS LINK-OR-UNLINKED. With the module on and strict off, a
     typed ward not in bed setup is admitted unlinked with the warning, as
     before; the admission form's options say strict_locations False.
  2. EACH LANE REFUSES AN UNLISTED WARD AND WRITES NOTHING: the admission
     form, Scribe's quick admission, a bed assignment, a transfer, the front
     desk's transfer (it delegates to the ADT writer) and a roster file (the
     row is rejected in the dry run, and Apply writes nothing for it).
  3. THE PER-WARD HALF OF F104: a typed bed on a ward still being set up is
     kept unlinked with the warning; on a ward whose register is complete it
     is refused; a bed label with no ward is refused.
  4. THE PLACE ALREADY RECORDED IS NEVER REFUSED: a transfer that keeps a
     patient admitted before strict where they lie goes through.
  5. ROSTER MOVES: a move to an unlisted ward is refused on its own
     (update_refused, the patients mirror untouched), listed in the dry run.
  6. THE SWITCH NEEDS THE MODULE: with ORB_ADT off, strict is off whatever
     its own variable says, and the module-off lanes are unchanged.
"""
import uuid

import pytest
from fastapi.testclient import TestClient

import config
import main
from auth import create_token, hash_password
from database import db_session
from services import bed_master_service as bm
from services import roster_import
from services.adt import core as adt_core

client = TestClient(main.app)
DEPT = "General Medicine"   # conftest seeds D001 and N001 in it
DOCTOR = {"id": "D001", "role": "doctor", "department": DEPT}

_SEEDED: list[str] = []


@pytest.fixture(autouse=True)
def _discharge_seeded():
    """Every patient admitted here is discharged again, so no bed stays held."""
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
    monkeypatch.setattr(config, "ADT_STRICT_LOCATIONS_ENABLED", False)
    yield


@pytest.fixture()
def strict_on(monkeypatch):
    monkeypatch.setattr(config, "ADT_ENABLED", True)
    monkeypatch.setattr(config, "ADT_STRICT_LOCATIONS_ENABLED", True)
    assert adt_core.strict_locations_on()
    yield


def _tag() -> str:
    return uuid.uuid4().hex[:6].upper()


def _user(role: str, department: str | None = DEPT) -> dict:
    uid = f"SL{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO users (id, name, role, department, password_hash, is_active) "
            "VALUES (?, ?, ?, ?, ?, 1)",
            (uid, f"Strict {role} {uid[-4:]}", role, department, hash_password("password")),
        )
    return {"id": uid, "role": role, "department": department}


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


def _ward(department: str = DEPT, labels=("1", "2", "3"), *, complete: bool = False) -> dict:
    ward = bm.save_ward({"code": f"SL-{_tag()}", "department": department,
                         "cleaning_tracked": False}, actor=_admin())
    if labels:
        bm.register_beds({"ward_id": ward["id"], "labels": list(labels)}, actor=_admin())
    if complete:
        bm.mark_register_complete(ward["id"], {}, actor=_admin())
    return ward


def _bed(ward_id: str, label: str) -> dict:
    with db_session() as db:
        return dict(db.execute(
            "SELECT * FROM housekeeping_units WHERE ward_id = ? AND bed_label = ?",
            (ward_id, label)).fetchone())


def _one(sql: str, params: tuple = ()) -> dict | None:
    with db_session() as db:
        row = db.execute(sql, params).fetchone()
        return dict(row) if row else None


def _all(sql: str, params: tuple = ()) -> list[dict]:
    with db_session() as db:
        return [dict(r) for r in db.execute(sql, params).fetchall()]


# An invented name: a name holding an ordinary word ("Patient", "Admitted") is read by
# Sage's name matcher in later test files, so "the patient" there would match it.
def _payload(**over) -> dict:
    body = {"name": f"Strivane {_tag()}", "age": 51, "sex": "F",
            "admission_source": "er", "admission_type": "inpatient",
            "department": DEPT, "ward": "", "bed": "",
            "attending_doctor_id": "D001", "chief_complaint": "Fever"}
    body.update(over)
    return body


def _admit(body: dict, *, key: str | None = None):
    resp = client.post("/api/admissions/admit", headers=_hdr(DOCTOR, key or f"sl-{_tag()}"),
                       json=body)
    if resp.status_code in (200, 201):
        _SEEDED.append(resp.json()["patient_id"])
    return resp


def _named(name: str) -> int:
    return _one("SELECT COUNT(*) AS n FROM patients WHERE name = ?", (name,))["n"]


def _detail(resp) -> dict:
    return resp.json()["detail"]


def _admit_into(ward: dict, label: str) -> dict:
    bed = _bed(ward["id"], label)
    resp = _admit(_payload(ward=ward["code"], bed=label, ward_id=ward["id"], bed_id=bed["id"]))
    assert resp.status_code == 201, resp.text
    out = resp.json()
    return {"pid": out["patient_id"], "aid": out["admission_id"], "bed_id": bed["id"]}


def _receiving() -> dict:
    dept = f"Strict Recv {_tag()}"
    _user("doctor", dept)
    _user("nurse", dept)
    return {"dept": dept, "ward": _ward(department=dept)}


# ═════════════════════════════════════════════════════════════════════════════
#  1. Strict off keeps link-or-unlinked
# ═════════════════════════════════════════════════════════════════════════════

def test_strict_off_admits_an_unlisted_ward_unlinked_with_the_warning(adt_on):
    assert not adt_core.strict_locations_on()
    typed = f"Typed Bay {_tag()}"
    resp = _admit(_payload(ward=typed, bed="4"))
    assert resp.status_code == 201, resp.text
    out = resp.json()
    assert out["location"]["linked"] is False and out["location"]["ward_id"] is None
    assert "not in bed setup" in out["location_warning"]
    adm = _one("SELECT ward, bed, ward_id FROM admissions WHERE id = ?", (out["admission_id"],))
    assert adm == {"ward": typed, "bed": "4", "ward_id": None}


def test_the_options_say_whether_strict_is_on(adt_on, monkeypatch):
    resp = client.get("/api/admissions/options", headers=_hdr(DOCTOR))
    assert resp.status_code == 200 and resp.json()["master"]["strict_locations"] is False
    monkeypatch.setattr(config, "ADT_STRICT_LOCATIONS_ENABLED", True)
    resp = client.get("/api/admissions/options", headers=_hdr(DOCTOR))
    assert resp.json()["master"]["strict_locations"] is True


# ═════════════════════════════════════════════════════════════════════════════
#  2. Each lane refuses an unlisted ward and writes nothing
# ═════════════════════════════════════════════════════════════════════════════

def test_the_admission_form_refuses_an_unlisted_ward_and_writes_nothing(strict_on):
    body = _payload(ward=f"Nowhere {_tag()}", bed="1")
    moves_before = _one("SELECT COUNT(*) AS n FROM patient_movements")["n"]
    resp = _admit(body)
    assert resp.status_code == 400, resp.text
    detail = _detail(resp)
    assert detail["error"] == "ward_not_in_setup"
    assert "not in bed setup" in detail["message"] and "Nothing was saved" in detail["message"]
    assert _named(body["name"]) == 0
    assert _one("SELECT COUNT(*) AS n FROM patient_movements")["n"] == moves_before


def test_awaiting_a_bed_on_an_unlisted_ward_is_refused_too(strict_on):
    body = _payload(ward=f"Nowhere {_tag()}", bed="", awaiting_bed=True)
    resp = _admit(body)
    assert resp.status_code == 400 and _detail(resp)["error"] == "ward_not_in_setup"
    assert _named(body["name"]) == 0


def test_a_listed_place_is_admitted_and_linked_under_strict(strict_on):
    ward = _ward(complete=True)
    resp = _admit(_payload(ward=ward["code"].lower(), bed="2"))
    assert resp.status_code == 201, resp.text
    loc = resp.json()["location"]
    assert loc["linked"] is True and loc["ward_id"] == ward["id"]
    assert loc["bed_id"] == _bed(ward["id"], "2")["id"]


def test_no_bed_at_all_is_still_admitted_under_strict(strict_on):
    resp = _admit(_payload(ward="", bed="", awaiting_bed=True))
    assert resp.status_code == 201, resp.text
    assert resp.json()["location"]["awaiting_bed"] is True


def test_scribe_quick_admit_refuses_an_unlisted_ward_and_writes_nothing(strict_on):
    name = f"Scribe Strict {_tag()}"
    resp = client.post("/api/scribe/quick-admit", headers=_hdr(DOCTOR), json={
        "name": name, "age": 60, "sex": "M", "department": DEPT,
        "ward": f"Nowhere {_tag()}", "bed": "3", "chief_complaint": "Chest pain",
        "attending_doctor_id": "D001"})
    assert resp.status_code == 400, resp.text
    assert _detail(resp)["error"] == "ward_not_in_setup"
    assert _named(name) == 0


def test_a_bed_assignment_to_an_unlisted_ward_is_refused(strict_on):
    resp = _admit(_payload(ward="", bed="", awaiting_bed=True))
    aid = resp.json()["admission_id"]
    refused = client.post(f"/api/adt/admissions/{aid}/bed",
                          headers=_hdr(DOCTOR, f"bed-{_tag()}"),
                          json={"ward": f"Nowhere {_tag()}", "bed": "9"})
    assert refused.status_code == 400, refused.text
    assert _detail(refused)["error"] == "ward_not_in_setup"
    adm = _one("SELECT ward, bed, ward_id, bed_id FROM admissions WHERE id = ?", (aid,))
    assert adm == {"ward": None, "bed": None, "ward_id": None, "bed_id": None}
    assert _all("SELECT * FROM patient_movements WHERE admission_id = ? "
                "AND movement_type = 'bed_assigned'", (aid,)) == []


def test_a_transfer_to_an_unlisted_ward_is_refused_and_writes_nothing(strict_on):
    home = _ward()
    p = _admit_into(home, "1")
    recv = _receiving()
    resp = client.post("/api/adt/transfers", headers=_hdr(DOCTOR, f"tr-{_tag()}"), json={
        "admission_id": p["aid"], "to_department": recv["dept"], "ward": f"Nowhere {_tag()}",
        "bed": "2", "expected_bed_id": p["bed_id"], "reason": "Escalation",
        "boarder_acknowledged": True})
    assert resp.status_code == 400, resp.text
    assert _detail(resp)["error"] == "ward_not_in_setup"
    adm = _one("SELECT department, bed_id FROM admissions WHERE id = ?", (p["aid"],))
    assert adm == {"department": DEPT, "bed_id": p["bed_id"]}
    assert [m["movement_type"] for m in _all(
        "SELECT movement_type FROM patient_movements WHERE admission_id = ? ORDER BY rowid",
        (p["aid"],))] == ["admission"]


def test_the_front_desk_transfer_refuses_an_unlisted_ward_and_writes_nothing(strict_on):
    home = _ward()
    p = _admit_into(home, "1")
    recv = _receiving()
    resp = client.post("/api/frontdesk/transfers", headers=_hdr(DOCTOR), json={
        "patient_id": p["pid"], "to_department": recv["dept"], "to_ward": f"Nowhere {_tag()}",
        "to_bed": "5", "reason": "Escalation of care", "boarder_acknowledged": True})
    assert resp.status_code == 400, resp.text
    assert _detail(resp)["error"] == "ward_not_in_setup"
    assert _one("SELECT department FROM admissions WHERE id = ?",
                (p["aid"],))["department"] == DEPT


def test_a_bed_change_to_a_listed_bed_still_works_under_strict(strict_on):
    ward = _ward(complete=True)
    p = _admit_into(ward, "1")
    resp = client.post(f"/api/adt/admissions/{p['aid']}/bed-change",
                       headers=_hdr(DOCTOR, f"bc-{_tag()}"),
                       json={"bed_id": _bed(ward["id"], "3")["id"], "expected_bed_id": p["bed_id"],
                             "reason": "Nearer the station"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["current_bed"] == "3"


# ═════════════════════════════════════════════════════════════════════════════
#  3. The per-ward half of F104
# ═════════════════════════════════════════════════════════════════════════════

def test_a_typed_bed_on_a_ward_still_being_set_up_is_kept_unlinked(strict_on):
    ward = _ward(complete=False)
    resp = _admit(_payload(ward=ward["code"], bed="Corridor 7"))
    assert resp.status_code == 201, resp.text
    out = resp.json()
    assert out["location"]["ward_id"] == ward["id"] and out["location"]["bed_id"] is None
    assert out["location"]["bed"] == "Corridor 7"
    assert "not in bed setup" in out["location_warning"]


def test_a_typed_bed_on_a_complete_ward_is_refused(strict_on):
    ward = _ward(complete=True)
    body = _payload(ward=ward["code"], bed="Corridor 7")
    resp = _admit(body)
    assert resp.status_code == 400, resp.text
    detail = _detail(resp)
    assert detail["error"] == "bed_not_in_setup" and detail["ward_id"] == ward["id"]
    assert ward["code"] in detail["message"] and "complete" in detail["message"]
    assert _named(body["name"]) == 0


def test_a_bed_label_with_no_ward_names_no_place(strict_on):
    resp = _admit(_payload(ward="", bed="12"))
    assert resp.status_code == 400, resp.text
    assert _detail(resp)["error"] == "bed_not_in_setup"


def test_strict_refusal_is_none_while_off(adt_on):
    target = {"ward": None, "bed": None, "ward_text": "Nowhere", "bed_text": "1"}
    assert adt_core.strict_place_refusal(target) is None


# ═════════════════════════════════════════════════════════════════════════════
#  4. The place already recorded is never refused
# ═════════════════════════════════════════════════════════════════════════════

def test_a_transfer_keeping_a_pre_strict_unlisted_place_goes_through(adt_on, monkeypatch):
    typed = f"Old Bay {_tag()}"
    resp = _admit(_payload(ward=typed, bed="2"))
    assert resp.status_code == 201, resp.text
    aid = resp.json()["admission_id"]
    monkeypatch.setattr(config, "ADT_STRICT_LOCATIONS_ENABLED", True)
    recv = _receiving()
    moved = client.post("/api/adt/transfers", headers=_hdr(DOCTOR, f"tr-{_tag()}"), json={
        "admission_id": aid, "to_department": recv["dept"], "keep_location": True,
        "expected_bed_id": None, "reason": "Taken over by the receiving team"})
    assert moved.status_code == 200, moved.text
    adm = _one("SELECT department, ward, bed FROM admissions WHERE id = ?", (aid,))
    assert adm == {"department": recv["dept"], "ward": typed, "bed": "2"}


def test_moving_from_a_pre_strict_unlisted_place_to_another_is_refused(adt_on, monkeypatch):
    typed = f"Old Bay {_tag()}"
    resp = _admit(_payload(ward=typed, bed="2"))
    aid = resp.json()["admission_id"]
    monkeypatch.setattr(config, "ADT_STRICT_LOCATIONS_ENABLED", True)
    refused = client.post(f"/api/adt/admissions/{aid}/bed-change",
                          headers=_hdr(DOCTOR, f"bc-{_tag()}"),
                          json={"bed": "3", "expected_bed_id": None, "reason": "Moved bay"})
    assert refused.status_code == 400, refused.text
    assert _detail(refused)["error"] == "ward_not_in_setup"
    assert _one("SELECT bed FROM admissions WHERE id = ?", (aid,))["bed"] == "2"


# ═════════════════════════════════════════════════════════════════════════════
#  5. Roster files
# ═════════════════════════════════════════════════════════════════════════════

_HEADER = "MRN,Name,Age,Sex,Ward,Bed,Diagnosis,Department"
_MAPPING = {"mrn": "MRN", "name": "Name", "age": "Age", "sex": "Sex", "ward": "Ward",
            "bed": "Bed", "diagnosis": "Diagnosis", "department": "Department"}


def _roster(*rows: tuple[str, str, str]) -> bytes:
    lines = [_HEADER] + [f"{mrn},Roster Iyer,44,F,{ward},{bed},Sepsis,{DEPT}"
                         for mrn, ward, bed in rows]
    return ("\n".join(lines) + "\n").encode()


def _seed_roster_patient(mrn: str) -> None:
    with db_session() as db:
        row = db.execute("SELECT id FROM patients WHERE mrn = ?", (mrn,)).fetchone()
    if row is not None:
        _SEEDED.append(row["id"])


def test_the_roster_dry_run_lists_an_unlisted_ward_and_apply_writes_nothing_for_it(strict_on):
    ward = _ward(complete=True)
    bad, good = f"SLR{_tag()}", f"SLR{_tag()}"
    nowhere = f"Nowhere {_tag()}"
    content = _roster((bad, nowhere, "1"), (good, ward["code"], "2"))
    dry = roster_import.plan(content, _MAPPING)
    assert dry["counts"]["location_rejects"] == 1
    reject = dry["location_rejects"][0]
    assert reject["mrn"] == bad and reject["code"] == "ward_not_in_setup"
    assert reject["ward"] == nowhere and "not in bed setup" in reject["message"]
    row = next(r for r in dry["rows"] if r["mrn"] == bad)
    assert row["action"] == "skip" and row["reason"] == "ward_not_in_setup"
    assert any(i["code"] == "ward_not_in_setup" and i["severity"] == "error"
               for i in row["issues"])
    assert next(r for r in dry["rows"] if r["mrn"] == good)["action"] == "create"

    applied = roster_import.apply_import(content, _MAPPING, actor_user_id=_admin()["id"])
    _seed_roster_patient(good)
    assert _one("SELECT 1 AS x FROM patients WHERE mrn = ?", (bad,)) is None
    assert _one("SELECT 1 AS x FROM patients WHERE mrn = ?", (good,)) is not None
    assert applied["counts"]["location_rejects"] == 1


def test_a_roster_move_to_an_unlisted_ward_is_refused_and_listed(adt_on, monkeypatch):
    mrn = f"SLR{_tag()}"
    typed = f"Roster Bay {_tag()}"
    roster_import.apply_import(_roster((mrn, typed, "1")), _MAPPING,
                               actor_user_id=_admin()["id"])
    _seed_roster_patient(mrn)
    pid = _one("SELECT id FROM patients WHERE mrn = ?", (mrn,))["id"]
    aid = _one("SELECT id FROM admissions WHERE patient_id = ? AND status = 'active'",
               (pid,))["id"]
    monkeypatch.setattr(config, "ADT_STRICT_LOCATIONS_ENABLED", True)
    nowhere = f"Nowhere {_tag()}"
    content = _roster((mrn, nowhere, "4"))
    dry = roster_import.plan(content, _MAPPING)
    row = next(r for r in dry["rows"] if r["mrn"] == mrn)
    assert row["admission"]["action"] == "update_refused"
    assert row["admission"]["refusal"]["code"] == "ward_not_in_setup"
    assert dry["location_rejects"][0]["admission_id"] == aid

    applied = roster_import.apply_import(content, _MAPPING, actor_user_id=_admin()["id"])
    assert applied["admissions"]["update_refused"] == 1
    assert applied["admissions"]["updated"] == 0
    assert _one("SELECT ward, bed FROM admissions WHERE id = ?", (aid,)) == \
        {"ward": typed, "bed": "1"}
    # The patients mirror is the ADT writer's: the refused move left it as well.
    assert _one("SELECT ward, bed FROM patients WHERE id = ?", (pid,)) == \
        {"ward": typed, "bed": "1"}


def test_a_roster_plan_with_strict_off_carries_no_location_rejects(adt_on):
    dry = roster_import.plan(_roster((f"SLR{_tag()}", f"Nowhere {_tag()}", "1")), _MAPPING)
    assert "location_rejects" not in dry and "location_rejects" not in dry["counts"]
    assert dry["rows"][0]["action"] == "create"


# ═════════════════════════════════════════════════════════════════════════════
#  6. The switch needs the module
# ═════════════════════════════════════════════════════════════════════════════

def test_strict_without_the_module_is_off_and_the_form_is_unchanged(monkeypatch):
    monkeypatch.setattr(config, "ADT_ENABLED", False)
    monkeypatch.setattr(config, "ADT_STRICT_LOCATIONS_ENABLED", True)
    assert not config.feature_enabled("adt_strict_locations")
    assert not adt_core.strict_locations_on()
    typed = f"Typed Off {_tag()}"
    resp = client.post("/api/admissions/admit", headers=_hdr(DOCTOR),
                       json=_payload(ward=typed, bed="2"))
    assert resp.status_code == 200, resp.text
    _SEEDED.append(resp.json()["patient_id"])
    assert "location" not in resp.json() and "movement_id" not in resp.json()
    assert _one("SELECT ward FROM admissions WHERE id = ?",
                (resp.json()["admission_id"],))["ward"] == typed
