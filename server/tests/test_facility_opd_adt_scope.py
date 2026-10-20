"""Facility scope in the outpatient, registration, clinic schedule and bed lanes
(multisite-8h1, W12-F, 20 October 2026).

The module owns its database (isolated_module_database), so the second
facility never reaches the session database. It plants facility 1 and a
second facility (id 9853), each with its own department, ward, bed, clinic,
outpatient visit, inpatient and staff, and checks that an account bound to one
facility neither reads nor changes the other's, that nothing moves between
facilities (ruling MS-R3 (c)), and that with one facility on record every lane
behaves exactly as before. The route groups stay behind the facility fence
until every sweep that reaches them lands, so the HTTP checks open the fence
(FACILITY_BLIND_SURFACES = ()) to show what each lane does once it opens.

Test patients carry invented names (never a word a clinician types). Run with
the test env, never the shipping .env.
"""

from __future__ import annotations

import contextlib
import json
import uuid
from collections.abc import Iterator
from typing import Any

import config
import facility_fence as F
import main
import pytest
from auth import create_token
from database import db_session
from fastapi.testclient import TestClient
from services.adt import core as adt_core
from services.clinical_time import utc_iso_now
from services.frontdesk_service import FrontDeskError, ensure_frontdesk_schema
from services.hospital_time import hospital_today

from services import bed_master_service as bm
from services import facility_lanes
from services import facility_service as FS
from services import opd_queue_service as oq
from services import registration_service as reg

pytestmark = pytest.mark.usefixtures("isolated_module_database")

client = TestClient(main.app)

TAG = uuid.uuid4().hex[:6].upper()
F2 = 9853  # an id no other module writes
F2_CODE = f"OA{TAG[:4]}"
F1_DEPT = f"Quillmoor Medicine {TAG}"
F2_DEPT = f"Vexhale Medicine {TAG}"
F1_WARD = f"QM{TAG}"
F2_WARD = f"VX{TAG}"

F1_NAME = "Orsolya Quandtberry"
F1_PHONE = "8123456701"
F1_UHID = f"OAQ{TAG}"
F1_OUT_NAME = "Thessaly Brumwick"
F2_IN_NAME = "Ilvana Corbisher"


# ── the world ────────────────────────────────────────────────────────────────


#: The accounts of the world: key -> (role, department, home facility).
STAFF = {
    "doc_f1": ("doctor", F1_DEPT, None),
    "doc_f2": ("doctor", F2_DEPT, None),
    "doc_none_f2": ("doctor", None, F2),  # a legacy doctor account with no department
    "nurse_f1": ("nurse", F1_DEPT, None),
    "nurse_f2": ("nurse", F2_DEPT, None),
    "admin_f1": ("admin", None, None),
    "admin_f2": ("admin", None, F2),
    "admin_group": ("admin", None, None),  # holds an enterprise grant below
    "recep_f1": ("receptionist", None, None),
    "recep_f2": ("receptionist", None, F2),
    "recep_lost": ("receptionist", f"Nowhere Clinic {TAG}", None),
}


def _user(db: Any, uid: str, role: str, department: str | None) -> None:
    db.execute(
        "INSERT INTO users (id, name, role, department, password_hash, is_active) "
        "VALUES (?, ?, ?, ?, 'x', 1)",
        (uid, f"Oadt {role} {uid[-4:]}", role, department),
    )


def _home(db: Any, uid: str, facility_id: int) -> None:
    db.execute(
        "INSERT INTO user_facilities (user_id, facility_id, kind, granted_by, granted_at, reason) "
        "VALUES (?, ?, 'home', 'system', CURRENT_TIMESTAMP, 'facility scope test')",
        (uid, facility_id),
    )


def _patient(
    db: Any, pid: str, name: str, *, facility: int, phone: str | None = None, mrn: str | None = None
) -> None:
    db.execute(
        "INSERT INTO patients (id, name, mrn, age, sex, phone, date_of_birth, status, "
        "registered_facility_id, created_at) VALUES (?, ?, ?, 52, 'F', ?, '1974-03-09', "
        "'registered', ?, ?)",
        (pid, name, mrn or f"MRN-{pid}", phone, facility, utc_iso_now()),
    )


def _admit(db: Any, pid: str, aid: str, department: str, facility: int) -> None:
    db.execute("UPDATE patients SET status = 'admitted' WHERE id = ?", (pid,))
    db.execute(
        "INSERT INTO admissions (id, patient_id, admitted_by, department, status, facility_id, "
        "created_at) VALUES (?, ?, 'system', ?, 'active', ?, ?)",
        (aid, pid, department, facility, utc_iso_now()),
    )


def _visit(db: Any, vid: str, pid: str, department: str, facility: int, token: int) -> None:
    db.execute(
        "INSERT INTO opd_visits (id, patient_id, department, visit_date, token_number, status, "
        "registered_by, registered_at, facility_id) VALUES (?, ?, ?, ?, ?, 'waiting', 'system', "
        "?, ?)",
        (vid, pid, department, hospital_today().isoformat(), token, utc_iso_now(), facility),
    )


@pytest.fixture(scope="module")
def world(isolated_module_database: Any) -> Iterator[dict]:
    ensure_frontdesk_schema()
    from services import housekeeping_service as hk

    hk.ensure_housekeeping_schema()
    users = {
        key: {"id": f"OAU{n:02d}{TAG}", "role": role, "department": dept}
        for n, (key, (role, dept, _home_at)) in enumerate(STAFF.items())
    }
    pats = {
        "in_f1": f"OAP1{TAG}",
        "out_f1": f"OAO1{TAG}",
        "in_f2": f"OAP2{TAG}",
    }
    with db_session() as db:
        db.execute(
            "INSERT INTO facilities (id, uuid, code, name) VALUES (?, ?, ?, ?)",
            (F2, str(uuid.uuid4()), F2_CODE, "Vexhale Satellite"),
        )
        db.execute(
            "INSERT INTO departments (name, facility_id, seeded_from) VALUES (?, 1, 'test')",
            (F1_DEPT,),
        )
        db.execute(
            "INSERT INTO departments (name, facility_id, seeded_from) VALUES (?, ?, 'test')",
            (F2_DEPT, F2),
        )
        for key, (role, dept, home_at) in STAFF.items():
            _user(db, users[key]["id"], role, dept)
            if home_at is not None:
                _home(db, users[key]["id"], home_at)
        db.execute(
            "INSERT INTO enterprise_access_grants (user_id, granted_by, granted_at, reason) "
            "VALUES (?, 'system', CURRENT_TIMESTAMP, 'facility scope test')",
            (users["admin_group"]["id"],),
        )
        _patient(db, pats["in_f1"], F1_NAME, facility=1, phone=F1_PHONE, mrn=F1_UHID)
        _admit(db, pats["in_f1"], f"OAAD1{TAG}", F1_DEPT, 1)
        _patient(db, pats["out_f1"], F1_OUT_NAME, facility=1, phone="8123456702")
        _visit(db, f"OAV1{TAG}", pats["out_f1"], F1_DEPT, 1, 41)
        _patient(db, pats["in_f2"], F2_IN_NAME, facility=F2, phone="8123456703")
        _admit(db, pats["in_f2"], f"OAAD2{TAG}", F2_DEPT, F2)
        _visit(db, f"OAV2{TAG}", pats["in_f2"], F2_DEPT, F2, 42)
    FS.invalidate_cache()
    F._audit_last.clear()
    assert FS.multi_facility_data() is True
    # Wards and beds of each facility, set up by that facility's administrator.
    with _switches():
        f1_ward = bm.save_ward({"code": F1_WARD, "department": F1_DEPT}, actor=users["admin_f1"])
        bm.register_beds({"ward_id": f1_ward["id"], "labels": ["1", "2"]}, actor=users["admin_f1"])
        f2_ward = bm.save_ward({"code": F2_WARD, "department": F2_DEPT}, actor=users["admin_f2"])
        bm.register_beds({"ward_id": f2_ward["id"], "labels": ["1", "2"]}, actor=users["admin_f2"])
        with db_session(immediate=True) as db:
            for aid, ward in ((f"OAAD1{TAG}", f1_ward), (f"OAAD2{TAG}", f2_ward)):
                adt_core.locate_admission_on(
                    db,
                    admission_id=aid,
                    actor=users["admin_group"],
                    movement_type="bed_assigned",
                    reason="facility scope test",
                    ward_id=ward["id"],
                    bed_id=_bed(ward["id"], "1"),
                )
    try:
        yield {"users": users, "patients": pats, "f1_ward": f1_ward, "f2_ward": f2_ward}
    finally:
        FS.invalidate_cache()
        F._audit_last.clear()


@contextlib.contextmanager
def _switches() -> Iterator[None]:
    """The modules these lanes sit behind, on, for the length of the block."""
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(config, "ADT_ENABLED", True)
        mp.setattr(config, "HOUSEKEEPING_ENABLED", True)
        mp.setattr(config, "FRONTDESK_ENABLED", True)
        mp.setattr(config, "REGISTRATION_ENABLED", True)
        mp.setattr(config, "OPD_ENABLED", True)
        mp.setattr(config, "UHID_SOURCE", "hospital")
        mp.setattr(config, "UHID_PATTERN", None)
        yield


@pytest.fixture()
def lanes_on(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Every switch on, and the fence open as it will be once the sweeps land."""
    from ws import realtime

    monkeypatch.setattr(realtime, "schedule_on_loop", lambda make_coro, what: True)
    monkeypatch.setattr(F, "FACILITY_BLIND_SURFACES", ())
    monkeypatch.setattr(config, "DUPLICATE_MODE", "block")
    with _switches():
        yield


@pytest.fixture(autouse=True)
def _fresh_cache() -> Iterator[None]:
    FS.invalidate_cache()
    yield
    FS.invalidate_cache()


def _hdr(user: dict) -> dict:
    return {"Authorization": f"Bearer {create_token(user['id'], user['role'])}"}


def _bed(ward_id: str, label: str) -> str:
    with db_session() as db:
        return db.execute(
            "SELECT id FROM housekeeping_units WHERE ward_id = ? AND bed_label = ?",
            (ward_id, label),
        ).fetchone()[0]


def _audits(action: str, actor_id: str) -> list[dict]:
    with db_session() as db:
        rows = db.execute(
            "SELECT * FROM audit_logs WHERE action_type = ? AND actor_user_id = ? ORDER BY id",
            (action, actor_id),
        ).fetchall()
    out = []
    for r in rows:
        row = dict(r)
        row["meta"] = json.loads(row.get("metadata_json") or "{}")
        out.append(row)
    return out


@contextlib.contextmanager
def _one_facility() -> Iterator[None]:
    """Facility 2 taken off the record for the block (no row refers to it by
    key), so the lanes see one facility, as every install does today."""
    with db_session() as db:
        row = dict(db.execute("SELECT * FROM facilities WHERE id = ?", (F2,)).fetchone())
        db.execute("DELETE FROM facilities WHERE id = ?", (F2,))
    FS.invalidate_cache()
    try:
        yield
    finally:
        cols = ", ".join(row)
        marks = ", ".join("?" for _ in row)
        with db_session() as db:
            db.execute(f"INSERT INTO facilities ({cols}) VALUES ({marks})", tuple(row.values()))
        FS.invalidate_cache()


# ═════════════════════════════════════════════════════════════════════════════
#  1. The scope helpers, and the one-facility path
# ═════════════════════════════════════════════════════════════════════════════


def test_the_world_is_what_it_says(world: dict) -> None:
    users = world["users"]
    assert facility_lanes.scope_of(users["admin_f1"]) == frozenset({1})
    assert facility_lanes.scope_of(users["admin_f2"]) == frozenset({F2})
    assert facility_lanes.scope_of(users["admin_group"]) is None
    assert facility_lanes.scope_of(users["doc_f2"]) == frozenset({F2})
    assert facility_lanes.scope_of(users["recep_lost"]) == frozenset()
    with db_session() as db:
        assert facility_lanes.account_scope_on(db, users["recep_f2"]["id"]) == frozenset({F2})
        assert facility_lanes.account_scope_on(db, "OA-NOBODY") == frozenset()
        assert facility_lanes.visit_facility_on(db, f"OAV1{TAG}") == 1
        assert facility_lanes.admission_facility_on(db, f"OAAD2{TAG}") == F2
        assert facility_lanes.ward_facility_on(db, world["f2_ward"]["id"]) == F2
    assert facility_lanes.outside(None, frozenset({1}))
    assert not facility_lanes.outside(None, None)


def test_wards_are_stamped_with_their_departments_facility(world: dict) -> None:
    with db_session() as db:
        f1 = dict(
            db.execute("SELECT * FROM wards WHERE id = ?", (world["f1_ward"]["id"],)).fetchone()
        )
        f2 = dict(
            db.execute("SELECT * FROM wards WHERE id = ?", (world["f2_ward"]["id"],)).fetchone()
        )
    assert (f1["facility_id"], f1["facility_code"]) == (1, "MAIN")
    assert (f2["facility_id"], f2["facility_code"]) == (F2, F2_CODE)


def test_with_one_facility_every_lane_reads_as_before(world: dict) -> None:
    users = world["users"]
    with _one_facility():
        assert facility_lanes.scope_of(users["admin_f2"]) is None
        with db_session() as db:
            assert facility_lanes.account_scope_on(db, users["recep_f2"]["id"]) is None
        found = reg.search_index(F1_UHID, facilities=facility_lanes.scope_of(users["recep_f2"]))
        assert [c["patient_id"] for c in found["results"]] == [world["patients"]["in_f1"]]
        with _switches():
            board = bm.bed_board(actor=users["admin_f2"])
        codes = {w["code"] for w in board["wards"]}
        assert {F1_WARD, F2_WARD} <= codes


# ═════════════════════════════════════════════════════════════════════════════
#  2. Outpatients
# ═════════════════════════════════════════════════════════════════════════════


def test_a_visit_at_another_facility_is_404_and_audited(world: dict, lanes_on: None) -> None:
    doc = world["users"]["doc_none_f2"]  # a legacy doctor account with no department
    own = client.get(f"/api/opd/visits/OAV2{TAG}/consultation", headers=_hdr(doc))
    assert own.status_code == 200, own.text
    other = client.get(f"/api/opd/visits/OAV1{TAG}/consultation", headers=_hdr(doc))
    assert other.status_code == 404, other.text
    rows = _audits("patient_scope_denied", doc["id"])
    assert rows, "the refusal must be on the record"
    meta = rows[-1]["meta"]
    assert meta["scope_cause"] == "facility"
    assert meta["scope_helper"] == "opd_visit_in_clinic"
    assert meta["patient_facility_id"] == 1
    assert meta["facility_scope"] == [F2]
    assert rows[-1]["patient_id"] == world["patients"]["out_f1"]


def test_a_doctors_queue_holds_only_their_facilitys_clinics(world: dict, lanes_on: None) -> None:
    doc = world["users"]["doc_none_f2"]
    resp = client.get("/api/opd/my-queue", headers=_hdr(doc))
    assert resp.status_code == 200, resp.text
    unassigned = {v["id"] for v in resp.json()["unassigned"]}
    assert f"OAV2{TAG}" in unassigned
    assert f"OAV1{TAG}" not in unassigned
    # Service level: the empty set holds nothing, None holds both.
    none = oq.list_my_queue(doctor_id=doc["id"], department=None, facilities=frozenset())
    assert none["total"] == 0
    both = oq.list_my_queue(doctor_id=doc["id"], department=None, facilities=None)
    assert {f"OAV1{TAG}", f"OAV2{TAG}"} <= {v["id"] for v in both["unassigned"]}


def test_the_queue_never_acts_on_another_facilitys_visit(world: dict, lanes_on: None) -> None:
    with pytest.raises(FrontDeskError):
        oq.skip_visit(f"OAV1{TAG}", actor_id=world["users"]["recep_f2"]["id"])
    with pytest.raises(FrontDeskError):
        oq.update_queue_fields(f"OAV1{TAG}", room="7", actor_id=world["users"]["recep_f2"]["id"])
    # An account nobody can place is refused too: a scope never widens.
    with pytest.raises(FrontDeskError):
        oq.recall_visit(f"OAV1{TAG}", actor_id=None)
    moved = oq.update_queue_fields(
        f"OAV2{TAG}", room="9", actor_id=world["users"]["recep_f2"]["id"]
    )
    assert moved["room"] == "9"


def test_a_token_board_is_made_and_switched_off_only_for_the_callers_facility(
    world: dict, lanes_on: None
) -> None:
    users = world["users"]
    with pytest.raises(oq.BoardError) as refused:
        oq.create_display_board(
            department=F1_DEPT, label=None, actor=users["admin_f2"], scope_department=None
        )
    assert refused.value.code == "department_not_covered"
    made = oq.create_display_board(
        department=F1_DEPT, label=None, actor=users["admin_f1"], scope_department=None
    )
    with pytest.raises(oq.BoardError) as hidden:
        oq.revoke_display_board(made["board"]["id"], actor=users["admin_f2"], scope_department=None)
    assert hidden.value.code == "board_not_found"
    own = oq.create_display_board(
        department=F2_DEPT, label=None, actor=users["admin_f2"], scope_department=None
    )
    assert (
        oq.revoke_display_board(own["board"]["id"], actor=users["admin_f2"], scope_department=None)[
            "active"
        ]
        is False
    )


# ═════════════════════════════════════════════════════════════════════════════
#  3. Registration
# ═════════════════════════════════════════════════════════════════════════════


def _search(user: dict, q: str) -> list[str]:
    resp = client.get("/api/registration/search", params={"q": q}, headers=_hdr(user))
    assert resp.status_code == 200, resp.text
    return [c["patient_id"] for c in resp.json()["results"]]


def test_the_index_search_stays_inside_the_desks_facility(world: dict, lanes_on: None) -> None:
    pid = world["patients"]["in_f1"]
    assert pid in _search(world["users"]["recep_f1"], F1_UHID)
    assert pid in _search(world["users"]["recep_f1"], F1_PHONE)
    assert pid not in _search(world["users"]["recep_f2"], F1_UHID)
    assert pid not in _search(world["users"]["recep_f2"], F1_PHONE)
    assert pid not in _search(world["users"]["recep_f2"], "Quandtberry")
    assert _search(world["users"]["recep_lost"], F1_UHID) == []
    assert pid in _search(world["users"]["admin_group"], F1_UHID)


def test_a_hard_match_at_another_facility_is_counted_but_never_shown(
    world: dict, lanes_on: None
) -> None:
    desk = world["users"]["recep_f2"]
    match = client.post(
        "/api/registration/match", json={"name": F1_NAME, "uhid": F1_UHID}, headers=_hdr(desk)
    )
    assert match.status_code == 200, match.text
    body = match.json()
    assert body["candidates"] == []
    assert body["held_elsewhere"] is True
    assert body["blocked"] is True
    assert F1_NAME not in match.text and world["patients"]["in_f1"] not in match.text
    resp = client.post(
        "/api/registration/patients",
        json={"name": "Merriwyn Oatesley", "sex": "F", "age_years": 40, "uhid": F1_UHID},
        headers=_hdr(desk),
    )
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["error"] == "existing_record"
    assert detail["candidates"] == []
    assert detail["held_elsewhere"] is True
    assert detail["message"] == reg.HARD_MATCH_ELSEWHERE_WORDS
    assert world["patients"]["in_f1"] not in resp.text
    refused = _audits("registration_refused", desk["id"])[-1]["meta"]
    assert refused["held_elsewhere_patient_ids"] == [world["patients"]["in_f1"]]


def test_a_probable_duplicate_elsewhere_blocks_and_a_possible_one_needs_a_reason(
    world: dict, lanes_on: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    desk = world["users"]["recep_f2"]
    probable = {
        "name": F1_NAME,
        "sex": "F",
        "age_years": 52,
        "phone": F1_PHONE,
        "uhid": f"OAN{TAG}",
    }
    resp = client.post("/api/registration/patients", json=probable, headers=_hdr(desk))
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["error"] == "duplicate_check_incomplete"
    assert resp.json()["detail"]["message"] == reg.PROBABLE_ELSEWHERE_WORDS
    assert world["patients"]["in_f1"] not in resp.text
    # 'warn': the same record needs a reviewed reason, and the override opens
    # a worklist pair with the record the desk was never shown.
    monkeypatch.setattr(config, "DUPLICATE_MODE", "warn")
    resp = client.post("/api/registration/patients", json=probable, headers=_hdr(desk))
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["error"] == "duplicate_review_required"
    assert detail["message"] == reg.REVIEW_ELSEWHERE_WORDS
    assert detail["candidates"] == []
    review = {
        "decision": "register_new",
        "candidate_ids": [],
        "reason": "The patient says this is a first visit to the group",
    }
    resp = client.post(
        "/api/registration/patients",
        json={**probable, "duplicate_review": review},
        headers=_hdr(desk),
    )
    assert resp.status_code == 201, resp.text
    new_id = resp.json()["patient"]["patient_id"]
    with db_session() as db:
        at = db.execute(
            "SELECT registered_facility_id FROM patients WHERE id = ?", (new_id,)
        ).fetchone()[0]
        pair = db.execute(
            "SELECT 1 FROM patient_duplicate_candidates WHERE patient_id IN (?, ?) "
            "AND candidate_patient_id IN (?, ?)",
            (new_id, world["patients"]["in_f1"], new_id, world["patients"]["in_f1"]),
        ).fetchone()
    assert at == F2
    assert pair is not None
    # The pair spans two facilities: neither desk's worklist lists it, the
    # group-wide administrator's does, and a desk cannot resolve it.
    listed = client.get("/api/registration/duplicates", headers=_hdr(desk))
    assert listed.status_code == 200, listed.text
    assert world["patients"]["in_f1"] not in listed.text
    group = client.get("/api/registration/duplicates", headers=_hdr(world["users"]["admin_group"]))
    assert group.status_code == 200, group.text
    item = next(
        i
        for i in group.json()["items"]
        if {new_id, world["patients"]["in_f1"]}
        == {(i["patient"] or {}).get("patient_id"), (i["candidate"] or {}).get("patient_id")}
    )
    with pytest.raises(reg.RegistrationNotFound):
        reg.resolve_duplicate(
            item["id"],
            resolution="not_duplicate",
            reason="Different people, checked by phone",
            user=desk,
            facilities=frozenset({F2}),
        )


def test_a_registrar_whose_facility_cannot_be_told_registers_nobody(
    world: dict, lanes_on: None
) -> None:
    lost = world["users"]["recep_lost"]
    resp = client.post(
        "/api/registration/patients",
        json={"name": "Pellandra Wexcombe", "sex": "F", "age_years": 33, "uhid": f"OAX{TAG}"},
        headers=_hdr(lost),
    )
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"]["error"] == "facility_scope"
    assert _audits("registration_facility_unknown", lost["id"])
    with db_session() as db:
        assert db.execute("SELECT 1 FROM patients WHERE mrn = ?", (f"OAX{TAG}",)).fetchone() is None


def test_the_identity_check_refuses_another_facilitys_inpatient(
    world: dict, lanes_on: None
) -> None:
    desk = world["users"]["recep_f2"]
    stated = {"name": F1_NAME, "age_years": 52, "phone_last4": F1_PHONE[-4:]}
    resp = client.post(
        "/api/registration/identity-check",
        json={"patient_id": world["patients"]["in_f1"], **stated},
        headers=_hdr(desk),
    )
    assert resp.status_code == 404, resp.text
    meta = _audits("patient_scope_denied", desk["id"])[-1]["meta"]
    assert meta["scope_cause"] == "facility" and meta["identity_check"] is True
    # A patient with no active admission may still be checked from another
    # facility (match or no match only): the group identity search finds them.
    resp = client.post(
        "/api/registration/identity-check",
        json={
            "patient_id": world["patients"]["out_f1"],
            "name": F1_OUT_NAME,
            "age_years": 52,
            "phone_last4": "6702",
        },
        headers=_hdr(desk),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["result"] == "match"
    # The facility 1 desk checks its own inpatient.
    resp = client.post(
        "/api/registration/identity-check",
        json={"patient_id": world["patients"]["in_f1"], **stated},
        headers=_hdr(world["users"]["recep_f1"]),
    )
    assert resp.status_code == 200, resp.text


def test_the_sweep_pairs_only_inside_the_callers_facilities(world: dict, lanes_on: None) -> None:
    with db_session() as db:
        _patient(db, f"OATW{TAG}", F2_IN_NAME, facility=F2, phone="8123456703")
        _patient(db, f"OATX{TAG}", F1_OUT_NAME, facility=F2, phone="8123456702")
    result = reg.sweep_duplicates(user=world["users"]["admin_f2"], max_pairs=50)
    assert result["pairs_opened"] >= 1
    with db_session() as db:
        pairs = {
            frozenset((r[0], r[1]))
            for r in db.execute(
                "SELECT patient_id, candidate_patient_id FROM patient_duplicate_candidates "
                "WHERE source = 'sweep'"
            ).fetchall()
        }
    assert frozenset((f"OATW{TAG}", world["patients"]["in_f2"])) in pairs
    # F1's outpatient and its F2 namesake are at two facilities: not this sweep's.
    assert frozenset((f"OATX{TAG}", world["patients"]["out_f1"])) not in pairs


# ═════════════════════════════════════════════════════════════════════════════
#  4. Clinic timetables
# ═════════════════════════════════════════════════════════════════════════════


def test_a_timetable_is_its_facilitys_own(world: dict, lanes_on: None) -> None:
    from services import clinic_schedule_service as schedule

    users = world["users"]
    for doc, dept in (("doc_f1", F1_DEPT), ("doc_f2", F2_DEPT)):
        schedule.create_session(
            {
                "doctor_id": users[doc]["id"],
                "department": dept,
                "weekday": 2,
                "start_time": "09:00",
                "end_time": "11:00",
                "slot_minutes": 15,
            },
            actor=users["admin_group"],
        )
    listed = client.get("/api/schedule/sessions", headers=_hdr(users["admin_f2"]))
    assert listed.status_code == 200, listed.text
    depts = {s["department"] for s in listed.json()["sessions"]}
    assert F2_DEPT in depts and F1_DEPT not in depts
    doctors = client.get("/api/schedule/doctors", headers=_hdr(users["recep_f2"]))
    assert doctors.status_code == 200, doctors.text
    assert users["doc_f1"]["id"] not in doctors.text
    refused = client.post(
        "/api/schedule/sessions",
        json={
            "doctor_id": users["doc_f1"]["id"],
            "weekday": 4,
            "start_time": "14:00",
            "end_time": "15:00",
            "slot_minutes": 15,
        },
        headers=_hdr(users["admin_f2"]),
    )
    assert refused.status_code == 404, refused.text
    f1_session = next(
        s for s in schedule.list_sessions(doctor_id=users["doc_f1"]["id"]) if s["is_active"]
    )
    assert (
        client.get(
            f"/api/schedule/sessions/{f1_session['id']}", headers=_hdr(users["admin_f2"])
        ).status_code
        == 404
    )
    retire = client.post(
        f"/api/schedule/sessions/{f1_session['id']}/retire",
        json={"reason": "Not this facility's clinic"},
        headers=_hdr(users["admin_f2"]),
    )
    assert retire.status_code == 404, retire.text


# ═════════════════════════════════════════════════════════════════════════════
#  5. Beds and admissions
# ═════════════════════════════════════════════════════════════════════════════


def test_the_bed_board_and_setup_show_the_callers_facility_only(
    world: dict, lanes_on: None
) -> None:
    users = world["users"]
    board = client.get("/api/adt/board", headers=_hdr(users["admin_f2"]))
    assert board.status_code == 200, board.text
    codes = {w["code"] for w in board.json()["wards"]}
    assert F2_WARD in codes and F1_WARD not in codes
    assert F1_NAME not in board.text
    assert F2_IN_NAME in board.text
    other = client.get(
        "/api/adt/board",
        params={"ward_id": world["f1_ward"]["id"]},
        headers=_hdr(users["admin_f2"]),
    )
    assert other.status_code == 404, other.text
    group = client.get("/api/adt/board", headers=_hdr(users["admin_group"]))
    assert {F1_WARD, F2_WARD} <= {w["code"] for w in group.json()["wards"]}
    master = client.get("/api/adt/master", headers=_hdr(users["admin_f2"]))
    assert master.status_code == 200, master.text
    master_codes = {w["code"] for w in master.json()["wards"]}
    assert F2_WARD in master_codes and F1_WARD not in master_codes
    assert F1_DEPT not in master.json()["departments"]["covering"]
    assert F2_DEPT in master.json()["departments"]["covering"]
    beds = client.get("/api/adt/beds/available", headers=_hdr(users["admin_f2"]))
    assert beds.status_code == 200, beds.text
    assert all(b["ward_code"] != F1_WARD for b in beds.json()["beds"])
    census = client.get("/api/adt/census", headers=_hdr(users["admin_f2"]))
    assert census.status_code == 200, census.text
    assert F1_WARD not in {w["code"] for w in census.json()["ward_choices"]}


def test_an_admission_at_another_facility_is_404_and_audited(world: dict, lanes_on: None) -> None:
    admin = world["users"]["admin_f2"]
    resp = client.get(f"/api/adt/admissions/OAAD1{TAG}/location", headers=_hdr(admin))
    assert resp.status_code == 404, resp.text
    meta = _audits("patient_scope_denied", admin["id"])[-1]["meta"]
    assert meta["scope_cause"] == "facility"
    assert meta["scope_helper"] == "adt_scoped_admission"
    assert meta["patient_facility_id"] == 1
    own = client.get(f"/api/adt/admissions/OAAD2{TAG}/location", headers=_hdr(admin))
    assert own.status_code == 200, own.text
    leave = client.post(
        f"/api/adt/admissions/OAAD1{TAG}/leave",
        json={"reason": "Home for a family event"},
        headers={**_hdr(admin), "Idempotency-Key": f"oa-leave-{TAG}"},
    )
    assert leave.status_code == 404, leave.text


def test_nothing_moves_between_facilities(world: dict, lanes_on: None) -> None:
    group = world["users"]["admin_group"]
    f2_bed = _bed(world["f2_ward"]["id"], "2")
    with db_session() as db:
        current = db.execute(
            "SELECT bed_id FROM admissions WHERE id = ?", (f"OAAD1{TAG}",)
        ).fetchone()[0]
    moved = client.post(
        f"/api/adt/admissions/OAAD1{TAG}/bed-change",
        json={
            "bed_id": f2_bed,
            "expected_bed_id": current,
            "reason": "Closer to the window",
            "not_clean_reason": "Cleaned this morning",
        },
        headers={**_hdr(group), "Idempotency-Key": f"oa-move-{TAG}"},
    )
    assert moved.status_code == 409, moved.text
    assert moved.json()["detail"]["error"] == adt_core.CROSS_FACILITY_CODE
    transfer = client.post(
        "/api/adt/transfers",
        json={
            "admission_id": f"OAAD1{TAG}",
            "to_department": F2_DEPT,
            "keep_location": True,
            "expected_bed_id": current,
            "reason": "Care taken over by the satellite",
            "boarder_acknowledged": True,
        },
        headers={**_hdr(group), "Idempotency-Key": f"oa-transfer-{TAG}"},
    )
    assert transfer.status_code == 409, transfer.text
    assert transfer.json()["detail"]["error"] == adt_core.CROSS_FACILITY_CODE
    rows = _audits("cross_facility_move_refused", group["id"])
    lanes = [r["meta"]["lane"] for r in rows]
    assert lanes.count("adt_bed_change") == 1
    assert lanes.count("adt_transfer") == 1  # written once, by the writer, not again
    causes = {r["meta"]["lane"]: r["meta"]["cause"] for r in rows}
    assert causes["adt_bed_change"] == "ward_at_another_facility"
    assert causes["adt_transfer"] == "patient_at_another_facility"
    with db_session() as db:
        adm = dict(
            db.execute(
                "SELECT department, bed_id, facility_id FROM admissions WHERE id = ?",
                (f"OAAD1{TAG}",),
            ).fetchone()
        )
    assert adm == {"department": F1_DEPT, "bed_id": current, "facility_id": 1}


def test_a_bed_of_another_facility_is_never_held(world: dict, lanes_on: None) -> None:
    admin = world["users"]["admin_f2"]
    resp = client.post(
        "/api/adt/reservations",
        json={"bed_id": _bed(world["f1_ward"]["id"], "2"), "purpose": "other"},
        headers={**_hdr(admin), "Idempotency-Key": f"oa-hold-{TAG}"},
    )
    assert resp.status_code == 404, resp.text
    resp = client.post(
        "/api/adt/reservations",
        json={
            "bed_id": _bed(world["f2_ward"]["id"], "2"),
            "purpose": "admission",
            "patient_id": world["patients"]["out_f1"],
        },
        headers={**_hdr(admin), "Idempotency-Key": f"oa-hold2-{TAG}"},
    )
    assert resp.status_code == 404, resp.text
    assert _audits("patient_scope_denied", admin["id"])[-1]["meta"]["scope_helper"] == (
        "adt_reservation_patient"
    )


def test_ward_setup_stays_at_its_facility(world: dict, lanes_on: None) -> None:
    users = world["users"]
    made = client.post(
        "/api/adt/master/wards",
        json={"code": f"VY{TAG}", "department": F2_DEPT},
        headers=_hdr(users["admin_f2"]),
    )
    assert made.status_code == 201, made.text
    with db_session() as db:
        assert (
            db.execute(
                "SELECT facility_id FROM wards WHERE id = ?", (made.json()["id"],)
            ).fetchone()[0]
            == F2
        )
    other = client.post(
        "/api/adt/master/wards",
        json={"code": f"QY{TAG}", "department": F1_DEPT},
        headers=_hdr(users["admin_f2"]),
    )
    assert other.status_code == 422, other.text
    patch = client.patch(
        f"/api/adt/master/wards/{world['f1_ward']['id']}",
        json={"name": "Renamed from the satellite"},
        headers=_hdr(users["admin_f2"]),
    )
    assert patch.status_code == 404, patch.text
    # A ward in use never moves between facilities, even for the group.
    moved = client.patch(
        f"/api/adt/master/wards/{world['f1_ward']['id']}",
        json={"department": F2_DEPT},
        headers=_hdr(users["admin_group"]),
    )
    assert moved.status_code == 409, moved.text
    assert moved.json()["detail"]["error"] == "ward_facility_fixed"
    retire = client.post(
        f"/api/adt/master/beds/{_bed(world['f1_ward']['id'], '2')}/retire",
        json={"reason": "Broken frame"},
        headers=_hdr(users["admin_f2"]),
    )
    assert retire.status_code == 404, retire.text
