"""Bed reservations (adt-12, W7-D, 9 October 2026).

What this file defends:

  1. THE TABLE (migration 69). One active reservation per bed is refused
     underneath the writer too (partial unique index); a closed reservation
     never changes again (trigger); a closed one needs its closed_at; an
     admission always names its patient; nothing stops the erase plan's
     DELETE.
  2. RESERVE AND RELEASE. A free master bed is held with a purpose and an
     expiry from ruling ADT-P9 (120 minutes, 24 hours for elective and
     post-operative); a second reservation on one bed is 409; an occupied, out
     of service or retired bed cannot be held; a transfer names its admission;
     one patient holds one bed at a time; the Idempotency-Key is required and a
     replay writes nothing. Release needs a reason and happens once.
  3. PLACING A PATIENT. Admitting or moving anybody else into a held bed is
     409 bed_reserved on every lane (the admission form, the bed route, a bed
     change, a link) and the words never name who it is held for; the patient
     it is held for is placed and the reservation fulfilled in the same
     transaction, still meeting the cleaning rule (ADT-S2 (c)).
  4. EXPIRY. A reservation past its time holds nothing: the board frees the
     bed at once, another patient can be placed, and the next reservation
     write records it as expired.
  5. RETIRE. Retiring a held bed is refused; after the release it retires.
  6. WHO AND WHAT IS SHOWN. adt.bed or patient.write reserve (the clerk, the
     doctor, the administrator); a nurse is 403; a patient outside the
     caller's department is 404, audited, whichever way the body names them
     (admission, patient_id, or both, each checked on its own), and checked
     again inside the service's transaction, so a race between the check and
     the write holds nothing; the caller-named patient guard sees the
     handler derive the department itself. The tile says 'Reserved for
     transfer' with its expiry and names the patient only inside scope; the
     list does the same and audits a read that names anybody. The list's
     free-text note goes with the name: none outside scope, and on a hold for
     nobody named only to who may release it. The module off is 404.
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import config
import main
from auth import create_token, hash_password
from database import db_session
from db_backend import DBIntegrityError
from services import bed_master_service as bm
from services import housekeeping_service as hk
from services.adt import core as adt_core
from services.adt import reservations as adt_res
from services.adt import transfer as adt_transfer
from services.audit_service import AuditWriteError

client = TestClient(main.app)


# ── helpers ──────────────────────────────────────────────────────────────────

def _tag() -> str:
    return uuid.uuid4().hex[:6].upper()


def _user(role: str, department: str | None) -> dict:
    uid = f"RS{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO users (id, name, role, department, password_hash, is_active) "
            "VALUES (?, ?, ?, ?, ?, 1)",
            (uid, f"Reserve {role} {uid[-4:]}", role, department, hash_password("password")),
        )
    return {"id": uid, "role": role, "department": department}


def _hdr(user: dict, key: str | None = None) -> dict:
    out = {"Authorization": f"Bearer {create_token(user['id'], user['role'])}"}
    if key:
        out["Idempotency-Key"] = key
    return out


def _key() -> str:
    return f"k-{uuid.uuid4().hex}"


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
    dept = f"Reserve Medicine {_tag()}"
    doctor = _user("doctor", dept)
    nurse = _user("nurse", dept)
    clerk = _user("ward_clerk", dept)
    admin = _user("admin", None)
    # Cleaning not tracked unless a test says so: the cleaning rule has its own test.
    ward = bm.save_ward({"code": f"RS-{_tag()}", "department": dept, "cleaning_tracked": False},
                        actor=admin)
    bm.register_beds({"ward_id": ward["id"], "labels": ["1", "2", "3", "4"]}, actor=admin)
    out = {"dept": dept, "doctor": doctor, "nurse": nurse, "clerk": clerk, "admin": admin,
           "ward": ward, "patients": []}
    yield out
    with db_session() as db:
        for pid in out["patients"]:
            db.execute("UPDATE admissions SET status = 'discharged' WHERE patient_id = ? "
                       "AND status = 'active'", (pid,))
            db.execute("UPDATE patients SET status = 'discharged' WHERE id = ?", (pid,))
        db.execute("UPDATE bed_reservations SET status = 'released', closed_at = ?, "
                   "close_reason = 'test over' WHERE ward_id = ? AND status = 'active'",
                   (datetime.now(timezone.utc).isoformat(), ward["id"]))


def _bed(ward_id: str, label: str) -> dict:
    with db_session() as db:
        return dict(db.execute("SELECT * FROM housekeeping_units WHERE ward_id = ? AND bed_label = ?",
                               (ward_id, label)).fetchone())


def _admit(site: dict, label: str | None = None, *, department: str | None = None) -> dict:
    """An admitted patient, in bed `label` or awaiting a bed."""
    pid = f"PRS{uuid.uuid4().hex[:8].upper()}"
    aid = f"ADM-RS{uuid.uuid4().hex[:8].upper()}"
    name = f"Reserve Patient {pid[-6:]}"
    started = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    with db_session() as db:
        db.execute("INSERT INTO patients (id, name, mrn, status, admission_date) "
                   "VALUES (?, ?, ?, 'admitted', ?)", (pid, name, f"MRN-{pid}", started))
        db.execute("INSERT INTO admissions (id, patient_id, admitted_by, department, status, "
                   "admitted_at) VALUES (?, ?, 'D001', ?, 'active', ?)",
                   (aid, pid, department or site["dept"], started))
    site["patients"].append(pid)
    out = {"pid": pid, "aid": aid, "name": name, "mrn": f"MRN-{pid}", "bed_id": None}
    if label:
        bed = _bed(site["ward"]["id"], label)
        with db_session(immediate=True) as db:
            adt_core.locate_admission_on(db, admission_id=aid, actor=site["admin"],
                                         movement_type="bed_assigned", reason="test",
                                         ward_id=site["ward"]["id"], bed_id=bed["id"])
        out["bed_id"] = bed["id"]
    return out


def _reserve(user: dict, body: dict, key: str | None = "auto"):
    return client.post("/api/adt/reservations",
                       headers=_hdr(user, _key() if key == "auto" else key), json=body)


def _release(user: dict, rid: str, reason: str = "No longer needed"):
    return client.post(f"/api/adt/reservations/{rid}/release", headers=_hdr(user),
                       json={"reason": reason})


def _res(rid: str) -> dict:
    return _one("SELECT * FROM bed_reservations WHERE id = ?", (rid,))


def _tile(user: dict, ward_id: str, label: str) -> dict:
    resp = client.get("/api/adt/board", params={"ward_id": ward_id}, headers=_hdr(user))
    assert resp.status_code == 200, resp.text
    ward = next(w for w in resp.json()["wards"] if w["id"] == ward_id)
    return next(t for t in ward["tiles"] if t["bed_label"] == label)


def _expire(rid: str, minutes_ago: int = 1) -> None:
    when = (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()
    with db_session() as db:
        db.execute("UPDATE bed_reservations SET expires_at = ? WHERE id = ?", (when, rid))


def _detail(resp) -> dict:
    return resp.json()["detail"]


# ═════════════════════════════════════════════════════════════════════════════
#  1. The table
# ═════════════════════════════════════════════════════════════════════════════

_INS = ("INSERT INTO bed_reservations (id, bed_id, ward_id, patient_id, admission_id, purpose, "
        "status, reserved_at, expires_at, closed_at) VALUES (?, ?, 'W', ?, ?, ?, ?, 'a', 'b', ?)")


def test_the_table_refuses_a_second_active_reservation_on_one_bed():
    bed = f"BED-{_tag()}"
    with db_session() as db:
        db.execute(_INS, (f"R-{_tag()}", bed, None, None, "other", "active", None))
    with pytest.raises(DBIntegrityError):
        with db_session() as db:
            db.execute(_INS, (f"R-{_tag()}", bed, None, None, "other", "active", None))
    with db_session() as db:
        # A closed one beside it is fine.
        db.execute(_INS, (f"R-{_tag()}", bed, None, None, "other", "released", "c"))


def test_a_closed_reservation_never_changes_and_can_still_be_erased():
    rid = f"R-{_tag()}"
    with db_session() as db:
        db.execute(_INS, (rid, f"BED-{_tag()}", "PX", None, "other", "released", "c"))
    with pytest.raises(Exception) as refused:
        with db_session() as db:
            db.execute("UPDATE bed_reservations SET status = 'active', closed_at = NULL "
                       "WHERE id = ?", (rid,))
    assert "cannot be changed" in str(refused.value)
    with db_session() as db:
        db.execute("DELETE FROM bed_reservations WHERE id = ?", (rid,))
    assert _res(rid) is None


@pytest.mark.parametrize("values", [
    ("other", "released", None, None, None),      # closed with no closed_at
    ("bogus", "active", None, None, None),        # unknown purpose
    ("transfer", "active", None, "ADM-X", None),  # an admission with no patient
    ("other", "pending", None, None, "c"),        # unknown status
])
def test_the_table_refuses_rows_that_break_its_rules(values):
    purpose, status, patient, admission, closed = values
    with pytest.raises(DBIntegrityError):
        with db_session() as db:
            db.execute(_INS, (f"R-{_tag()}", f"BED-{_tag()}", patient, admission, purpose,
                              status, closed))


# ═════════════════════════════════════════════════════════════════════════════
#  2. Reserve and release
# ═════════════════════════════════════════════════════════════════════════════

def test_reserving_a_free_bed_for_a_transfer_shows_on_the_board(adt_on, site):
    p = _admit(site, "1")
    bed = _bed(site["ward"]["id"], "3")
    out = _reserve(site["clerk"], {"bed_id": bed["id"], "purpose": "transfer",
                                   "admission_id": p["aid"], "note": "From ICU after review"})
    assert out.status_code == 201, out.text
    res = out.json()["reservation"]
    assert res["status"] == "active" and res["purpose_words"] == "Reserved for transfer"
    assert res["patient_id"] == p["pid"] and res["admission_id"] == p["aid"]
    reserved_at = datetime.fromisoformat(res["reserved_at"])
    expires_at = datetime.fromisoformat(res["expires_at"])
    assert timedelta(minutes=119) < expires_at - reserved_at <= timedelta(minutes=120)
    tile = _tile(site["clerk"], site["ward"]["id"], "3")
    assert tile["status"] == "reserved" and tile["tone"] == "held"
    assert tile["status_words"] == "Reserved for transfer"
    assert tile["free"] is False
    assert tile["reservation"]["id"] == res["id"]
    assert tile["reservation"]["expires_at"] == res["expires_at"]
    assert tile["reservation"]["patient"]["name"] == p["name"]
    audits = _all("SELECT * FROM audit_logs WHERE action_type = 'bed_reserved' AND target_id = ?",
                  (res["id"],))
    assert len(audits) == 1 and audits[0]["event_category"] == "patient_movement"
    assert audits[0]["patient_id"] == p["pid"]


@pytest.mark.parametrize("purpose, minutes", [
    ("admission", 120), ("transfer", 120), ("other", 120), ("elective", 1440), ("post_op", 1440)])
def test_the_hold_follows_the_site_setting_for_its_purpose(adt_on, site, purpose, minutes):
    assert adt_res.expiry_minutes(purpose) == minutes
    p = _admit(site, "1")
    body = {"bed_id": _bed(site["ward"]["id"], "2")["id"], "purpose": purpose}
    if purpose in adt_res.NEEDS_ADMISSION:
        body["admission_id"] = p["aid"]
    out = _reserve(site["admin"], body)
    assert out.status_code == 201, out.text
    res = out.json()["reservation"]
    span = datetime.fromisoformat(res["expires_at"]) - datetime.fromisoformat(res["reserved_at"])
    assert span == timedelta(minutes=minutes)


def test_a_shorter_hold_may_be_asked_for_never_a_longer_one(adt_on, site):
    bed = _bed(site["ward"]["id"], "2")
    longer = _reserve(site["admin"], {"bed_id": bed["id"], "purpose": "admission",
                                      "expires_in_minutes": 121})
    assert longer.status_code == 422 and _detail(longer)["error"] == "bad_expiry"
    shorter = _reserve(site["admin"], {"bed_id": bed["id"], "purpose": "admission",
                                       "expires_in_minutes": 30})
    assert shorter.status_code == 201
    res = shorter.json()["reservation"]
    span = datetime.fromisoformat(res["expires_at"]) - datetime.fromisoformat(res["reserved_at"])
    assert span == timedelta(minutes=30)


def test_a_second_active_reservation_on_one_bed_is_409(adt_on, site):
    bed = _bed(site["ward"]["id"], "2")
    assert _reserve(site["admin"], {"bed_id": bed["id"], "purpose": "admission"}).status_code == 201
    again = _reserve(site["clerk"], {"bed_id": bed["id"], "purpose": "elective"})
    assert again.status_code == 409 and _detail(again)["error"] == "bed_reserved"
    assert len(_all("SELECT id FROM bed_reservations WHERE bed_id = ? AND status = 'active'",
                    (bed["id"],))) == 1


def test_an_occupied_out_of_service_or_retired_bed_cannot_be_held(adt_on, site):
    _admit(site, "1")
    occupied = _reserve(site["admin"], {"bed_id": _bed(site["ward"]["id"], "1")["id"],
                                        "purpose": "admission"})
    assert occupied.status_code == 409 and _detail(occupied)["error"] == "bed_occupied"
    broken = _bed(site["ward"]["id"], "2")
    hk.transition_unit(broken["id"], "out_of_service", actor_user_id="RSHK", reason="Rail broken")
    oos = _reserve(site["admin"], {"bed_id": broken["id"], "purpose": "admission"})
    assert oos.status_code == 409 and _detail(oos)["error"] == "bed_out_of_service"
    gone = _bed(site["ward"]["id"], "4")
    bm.retire_bed(gone["id"], {"reason": "Removed"}, actor=site["admin"])
    retired = _reserve(site["admin"], {"bed_id": gone["id"], "purpose": "admission"})
    assert retired.status_code == 409 and _detail(retired)["error"] == "bed_retired"


def test_a_transfer_names_its_admission_and_one_patient_holds_one_bed(adt_on, site):
    p = _admit(site, "1")
    nameless = _reserve(site["admin"], {"bed_id": _bed(site["ward"]["id"], "2")["id"],
                                        "purpose": "transfer"})
    assert nameless.status_code == 422 and _detail(nameless)["error"] == "admission_required"
    first = _reserve(site["admin"], {"bed_id": _bed(site["ward"]["id"], "2")["id"],
                                     "purpose": "transfer", "admission_id": p["aid"]})
    assert first.status_code == 201
    # By patient id too: an admitted patient's hold is their admission's.
    second = _reserve(site["admin"], {"bed_id": _bed(site["ward"]["id"], "3")["id"],
                                      "purpose": "other", "patient_id": p["pid"]})
    assert second.status_code == 409 and _detail(second)["error"] == "patient_already_reserved"


def test_the_key_is_required_and_a_replay_writes_nothing(adt_on, site):
    bed = _bed(site["ward"]["id"], "2")
    missing = _reserve(site["admin"], {"bed_id": bed["id"], "purpose": "admission"}, key=None)
    assert missing.status_code == 422 and _detail(missing)["error"] == "idempotency_key_required"
    key = _key()
    first = _reserve(site["admin"], {"bed_id": bed["id"], "purpose": "admission"}, key=key)
    again = _reserve(site["admin"], {"bed_id": bed["id"], "purpose": "admission"}, key=key)
    assert first.status_code == 201 and again.status_code == 201
    assert again.json()["replayed"] is True
    assert again.json()["reservation"]["id"] == first.json()["reservation"]["id"]
    other_bed = _reserve(site["admin"], {"bed_id": _bed(site["ward"]["id"], "3")["id"],
                                         "purpose": "admission"}, key=key)
    assert other_bed.status_code == 409 and _detail(other_bed)["error"] == "idempotency_key_reused"


def test_release_needs_a_reason_and_happens_once(adt_on, site):
    bed = _bed(site["ward"]["id"], "2")
    rid = _reserve(site["clerk"], {"bed_id": bed["id"], "purpose": "admission"}
                   ).json()["reservation"]["id"]
    blank = _release(site["clerk"], rid, "  ")
    assert blank.status_code == 422 and _detail(blank)["error"] == "reason_required"
    out = _release(site["clerk"], rid, "Patient went to another hospital")
    assert out.status_code == 200, out.text
    row = _res(rid)
    assert row["status"] == "released" and row["closed_by"] == site["clerk"]["id"]
    assert row["close_reason"] == "Patient went to another hospital" and row["closed_at"]
    twice = _release(site["clerk"], rid)
    assert twice.status_code == 409 and _detail(twice)["error"] == "reservation_not_active"
    assert _tile(site["clerk"], site["ward"]["id"], "2")["status"] != "reserved"
    assert len(_all("SELECT * FROM audit_logs WHERE action_type = 'bed_reservation_released' "
                    "AND target_id = ?", (rid,))) == 1


def test_an_unwritable_audit_reserves_nothing(adt_on, site, monkeypatch):
    real = adt_core.log_audit_event

    def broken(*args, **kwargs):
        if kwargs.get("event_category") == "patient_movement":
            raise AuditWriteError("audit table unwritable")
        return real(*args, **kwargs)

    monkeypatch.setattr(adt_core, "log_audit_event", broken)
    bed = _bed(site["ward"]["id"], "2")
    out = _reserve(site["admin"], {"bed_id": bed["id"], "purpose": "admission"})
    assert out.status_code == 503 and _detail(out)["error"] == "audit_unavailable"
    assert _all("SELECT id FROM bed_reservations WHERE bed_id = ?", (bed["id"],)) == []


# ═════════════════════════════════════════════════════════════════════════════
#  3. Placing a patient
# ═════════════════════════════════════════════════════════════════════════════

def test_admitting_someone_else_into_a_reserved_bed_is_409_on_every_lane(adt_on, site):
    held_for = _admit(site, "1")
    bed = _bed(site["ward"]["id"], "3")
    assert _reserve(site["admin"], {"bed_id": bed["id"], "purpose": "transfer",
                                    "admission_id": held_for["aid"]}).status_code == 201
    # The admission form.
    form = client.post("/api/admissions/admit", headers=_hdr(site["doctor"], _key()), json={
        "name": f"Walk In {_tag()}", "age": 50, "sex": "M", "admission_source": "er",
        "admission_type": "inpatient", "department": site["dept"], "ward": site["ward"]["code"],
        "bed": "3", "bed_id": bed["id"], "attending_doctor_id": site["doctor"]["id"],
        "chief_complaint": "Chest pain"})
    assert form.status_code == 409, form.text
    assert _detail(form)["error"] == "bed_reserved"
    assert held_for["name"] not in form.text and held_for["mrn"] not in form.text
    # The bed route, for an admission awaiting a bed.
    waiting = _admit(site)
    route = client.post(f"/api/adt/admissions/{waiting['aid']}/bed",
                        headers=_hdr(site["doctor"], _key()), json={"bed_id": bed["id"]})
    assert route.status_code == 409 and _detail(route)["error"] == "bed_reserved"
    assert held_for["name"] not in route.text
    # A bed change.
    lying = _admit(site, "2")
    with pytest.raises(adt_core.AdtConflict) as moved:
        adt_transfer.change_bed(lying["aid"], actor=site["admin"], reason="Closer to the station",
                                bed_id=bed["id"], expected_bed_id=lying["bed_id"],
                                idempotency_key=_key())
    assert moved.value.code == "bed_reserved"
    # The single location writer itself (a link or a correction).
    with pytest.raises(adt_core.AdtConflict) as linked:
        with db_session(immediate=True) as db:
            adt_core.locate_admission_on(db, admission_id=waiting["aid"], actor=site["admin"],
                                         movement_type="location_linked", reason="test",
                                         bed_id=bed["id"])
    assert linked.value.code == "bed_reserved"
    assert _one("SELECT bed_id FROM admissions WHERE id = ?", (waiting["aid"],))["bed_id"] is None
    assert _one("SELECT status FROM bed_reservations WHERE bed_id = ? AND status = 'active'",
                (bed["id"],)) is not None


def test_moving_the_reserved_patient_in_fulfils_the_reservation(adt_on, site):
    p = _admit(site, "1")
    bed = _bed(site["ward"]["id"], "3")
    rid = _reserve(site["doctor"], {"bed_id": bed["id"], "purpose": "transfer",
                                    "admission_id": p["aid"]}).json()["reservation"]["id"]
    moved = adt_transfer.change_bed(p["aid"], actor=site["doctor"], reason="Reserved bed ready",
                                    bed_id=bed["id"], expected_bed_id=p["bed_id"],
                                    idempotency_key=_key())
    assert moved["bed_id"] == bed["id"]
    row = _res(rid)
    assert row["status"] == "fulfilled" and row["movement_id"] == moved["movement"]["id"]
    assert row["closed_by"] == site["doctor"]["id"] and row["close_reason"] == adt_res.FULFILLED_REASON
    audits = _all("SELECT * FROM audit_logs WHERE action_type = 'bed_reservation_fulfilled' "
                  "AND target_id = ?", (rid,))
    assert len(audits) == 1 and audits[0]["patient_id"] == p["pid"]
    assert _tile(site["doctor"], site["ward"]["id"], "3")["status"] == "occupied"


def test_assigning_the_reserved_patient_a_bed_fulfils_it(adt_on, site):
    waiting = _admit(site)
    bed = _bed(site["ward"]["id"], "4")
    rid = _reserve(site["clerk"], {"bed_id": bed["id"], "purpose": "admission",
                                   "patient_id": waiting["pid"]}).json()["reservation"]["id"]
    # An admitted patient's hold is kept for their admission.
    assert _res(rid)["admission_id"] == waiting["aid"]
    out = client.post(f"/api/adt/admissions/{waiting['aid']}/bed",
                      headers=_hdr(site["clerk"], _key()), json={"bed_id": bed["id"]})
    assert out.status_code == 200, out.text
    assert _res(rid)["status"] == "fulfilled"


def test_the_reserved_patient_still_meets_the_cleaning_rule(adt_on, site):
    with db_session() as db:
        db.execute("UPDATE wards SET cleaning_tracked = 1 WHERE id = ?", (site["ward"]["id"],))
    waiting = _admit(site)
    bed = _bed(site["ward"]["id"], "2")   # registered beds start needing a clean
    rid = _reserve(site["admin"], {"bed_id": bed["id"], "purpose": "admission",
                                   "admission_id": waiting["aid"]}).json()["reservation"]["id"]
    refused = client.post(f"/api/adt/admissions/{waiting['aid']}/bed",
                          headers=_hdr(site["doctor"], _key()), json={"bed_id": bed["id"]})
    assert refused.status_code == 409
    assert _detail(refused)["error"] == "bed_not_clean_reason_required"
    assert _res(rid)["status"] == "active"
    ok = client.post(f"/api/adt/admissions/{waiting['aid']}/bed",
                     headers=_hdr(site["doctor"], _key()),
                     json={"bed_id": bed["id"], "not_clean_reason": "Patient arriving now"})
    assert ok.status_code == 200, ok.text
    assert _res(rid)["status"] == "fulfilled"


def test_a_bed_held_for_nobody_named_is_placeable_by_nobody_until_released(adt_on, site):
    waiting = _admit(site)
    bed = _bed(site["ward"]["id"], "3")
    rid = _reserve(site["admin"], {"bed_id": bed["id"], "purpose": "admission",
                                   "note": "ER patient on the way"}).json()["reservation"]["id"]
    refused = client.post(f"/api/adt/admissions/{waiting['aid']}/bed",
                          headers=_hdr(site["doctor"], _key()), json={"bed_id": bed["id"]})
    assert refused.status_code == 409 and _detail(refused)["error"] == "bed_reserved"
    assert _release(site["admin"], rid, "The ER patient arrived").status_code == 200
    ok = client.post(f"/api/adt/admissions/{waiting['aid']}/bed",
                     headers=_hdr(site["doctor"], _key()), json={"bed_id": bed["id"]})
    assert ok.status_code == 200, ok.text


def test_a_held_bed_still_says_it_needs_cleaning(adt_on, site):
    with db_session() as db:
        db.execute("UPDATE wards SET cleaning_tracked = 1 WHERE id = ?", (site["ward"]["id"],))
    bed = _bed(site["ward"]["id"], "2")   # registered beds start needing a clean
    assert _reserve(site["admin"], {"bed_id": bed["id"], "purpose": "admission"}).status_code == 201
    tile = _tile(site["admin"], site["ward"]["id"], "2")
    assert tile["status"] == "reserved" and "Needs cleaning" in tile["markers"]


def test_the_admission_form_shows_a_held_bed_as_not_selectable(adt_on, site):
    bed = _bed(site["ward"]["id"], "3")
    assert _reserve(site["admin"], {"bed_id": bed["id"], "purpose": "elective"}).status_code == 201
    with db_session() as db:
        wards = adt_core.bed_choices_on(db)
    ward = next(w for w in wards if w["id"] == site["ward"]["id"])
    entry = next(b for b in ward["beds"] if b["bed_id"] == bed["id"])
    assert entry["status"] == "reserved" and entry["selectable"] is False
    assert entry["status_words"] == "Reserved for a planned admission"


# ═════════════════════════════════════════════════════════════════════════════
#  4. Expiry
# ═════════════════════════════════════════════════════════════════════════════

def test_an_expired_reservation_frees_the_bed(adt_on, site):
    held_for = _admit(site, "1")
    bed = _bed(site["ward"]["id"], "3")
    rid = _reserve(site["admin"], {"bed_id": bed["id"], "purpose": "transfer",
                                   "admission_id": held_for["aid"]}).json()["reservation"]["id"]
    _expire(rid)
    tile = _tile(site["admin"], site["ward"]["id"], "3")
    assert tile["status"] != "reserved" and tile["reservation"] is None
    # Another patient can be placed at once; the old hold is not fulfilled by it.
    waiting = _admit(site)
    ok = client.post(f"/api/adt/admissions/{waiting['aid']}/bed",
                     headers=_hdr(site["doctor"], _key()), json={"bed_id": bed["id"]})
    assert ok.status_code == 200, ok.text
    assert _res(rid)["status"] == "active"   # recorded at the next reservation write
    other = _reserve(site["admin"], {"bed_id": _bed(site["ward"]["id"], "4")["id"],
                                     "purpose": "admission"})
    assert other.status_code == 201
    row = _res(rid)
    assert row["status"] == "expired" and row["close_reason"] == adt_res.EXPIRED_REASON
    assert row["closed_at"] and row["closed_by"] is None
    assert len(_all("SELECT * FROM audit_logs WHERE action_type = 'bed_reservation_expired' "
                    "AND target_id = ?", (rid,))) == 1


def test_an_expired_reservation_does_not_block_a_new_one_on_the_bed(adt_on, site):
    bed = _bed(site["ward"]["id"], "2")
    rid = _reserve(site["admin"], {"bed_id": bed["id"], "purpose": "admission"}
                   ).json()["reservation"]["id"]
    _expire(rid)
    again = _reserve(site["admin"], {"bed_id": bed["id"], "purpose": "elective"})
    assert again.status_code == 201, again.text
    assert _res(rid)["status"] == "expired"
    late = _release(site["admin"], rid)
    assert late.status_code == 409 and _detail(late)["status"] == "expired"


# ═════════════════════════════════════════════════════════════════════════════
#  5. Retire
# ═════════════════════════════════════════════════════════════════════════════

def test_retiring_a_reserved_bed_is_refused_until_it_is_released(adt_on, site):
    bed = _bed(site["ward"]["id"], "2")
    rid = _reserve(site["admin"], {"bed_id": bed["id"], "purpose": "admission"}
                   ).json()["reservation"]["id"]
    with pytest.raises(bm.BedMasterConflict) as refused:
        bm.retire_bed(bed["id"], {"reason": "Ward refit"}, actor=site["admin"])
    assert refused.value.code == "bed_reserved"
    resp = client.post(f"/api/adt/master/beds/{bed['id']}/retire", headers=_hdr(site["admin"]),
                       json={"reason": "Ward refit"})
    assert resp.status_code == 409
    assert _release(site["admin"], rid, "Ward refit").status_code == 200
    assert bm.retire_bed(bed["id"], {"reason": "Ward refit"}, actor=site["admin"])["retired_at"]


def test_an_expired_hold_does_not_stop_a_retire(adt_on, site):
    bed = _bed(site["ward"]["id"], "2")
    rid = _reserve(site["admin"], {"bed_id": bed["id"], "purpose": "admission"}
                   ).json()["reservation"]["id"]
    _expire(rid)
    assert bm.retire_bed(bed["id"], {"reason": "Ward refit"}, actor=site["admin"])["retired_at"]


# ═════════════════════════════════════════════════════════════════════════════
#  6. Who, and what is shown
# ═════════════════════════════════════════════════════════════════════════════

def test_a_nurse_cannot_reserve_or_release(adt_on, site):
    bed = _bed(site["ward"]["id"], "2")
    assert _reserve(site["nurse"], {"bed_id": bed["id"], "purpose": "admission"}
                    ).status_code == 403
    rid = _reserve(site["admin"], {"bed_id": bed["id"], "purpose": "admission"}
                   ).json()["reservation"]["id"]
    assert _release(site["nurse"], rid).status_code == 403
    assert _res(rid)["status"] == "active"


def test_a_patient_outside_the_callers_department_is_404_and_audited(adt_on, site):
    elsewhere = _admit(site, department=f"Elsewhere {_tag()}")
    bed = _bed(site["ward"]["id"], "2")
    out = _reserve(site["clerk"], {"bed_id": bed["id"], "purpose": "transfer",
                                   "admission_id": elsewhere["aid"]})
    assert out.status_code == 404
    by_patient = _reserve(site["clerk"], {"bed_id": bed["id"], "purpose": "admission",
                                          "patient_id": elsewhere["pid"]})
    assert by_patient.status_code == 404
    denied = _all("SELECT * FROM audit_logs WHERE action_type = 'patient_scope_denied' "
                  "AND actor_user_id = ?", (site["clerk"]["id"],))
    assert len(denied) == 2
    assert _all("SELECT id FROM bed_reservations WHERE bed_id = ?", (bed["id"],)) == []
    # Held by the administrator for that patient: the clerk cannot release it.
    rid = _reserve(site["admin"], {"bed_id": bed["id"], "purpose": "transfer",
                                   "admission_id": elsewhere["aid"]}).json()["reservation"]["id"]
    assert _release(site["clerk"], rid).status_code == 404
    assert _res(rid)["status"] == "active"


def _scope_denials(user: dict) -> list[dict]:
    return _all("SELECT patient_id, target_type FROM audit_logs "
                "WHERE action_type = 'patient_scope_denied' AND actor_user_id = ?",
                (user["id"],))


_OUT_OF_SCOPE = {"error": "not_found", "message": adt_res.OUT_OF_SCOPE_WORDS}


def test_the_caller_named_patient_guard_sees_the_reserve_route_scoped():
    """POST /api/adt/reservations takes a patient_id in its body, so the
    caller-named patient guard (tests/test_caller_named_patient_scope.py)
    enumerates it. The handler itself must derive the department from the
    authenticated user, where the guard can read it, not only inside helpers."""
    from tests.test_caller_named_patient_scope import _caller_named_patient_mutations

    bare, seen = _caller_named_patient_mutations()
    route = ("POST", "/api/adt/reservations")
    assert route in seen, "the guard no longer sees the reserve route at all"
    assert route not in bare


def test_a_patient_id_beside_an_admission_is_scoped_on_its_own(adt_on, site):
    """Both named: the admission is the clerk's own patient's, the patient_id
    another department's. Each is checked, so the answer is 404 with the
    attempt audited and nothing held, never a 422 that records nothing."""
    mine = _admit(site)
    elsewhere = _admit(site, department=f"Elsewhere {_tag()}")
    bed = _bed(site["ward"]["id"], "2")
    out = _reserve(site["clerk"], {"bed_id": bed["id"], "purpose": "transfer",
                                   "admission_id": mine["aid"],
                                   "patient_id": elsewhere["pid"]})
    assert out.status_code == 404, out.text
    assert _detail(out) == _OUT_OF_SCOPE
    assert _scope_denials(site["clerk"]) == [{"patient_id": elsewhere["pid"],
                                              "target_type": "patient"}]
    assert _all("SELECT id FROM bed_reservations WHERE bed_id = ?", (bed["id"],)) == []
    # The same body naming the clerk's own patient both ways holds the bed.
    ok = _reserve(site["clerk"], {"bed_id": bed["id"], "purpose": "transfer",
                                  "admission_id": mine["aid"], "patient_id": mine["pid"]})
    assert ok.status_code == 201, ok.text
    held = _res(ok.json()["reservation"]["id"])
    assert (held["patient_id"], held["admission_id"]) == (mine["pid"], mine["aid"])


def test_the_service_checks_scope_again_inside_its_own_transaction(adt_on, site):
    """The route's check runs in a session of its own. reserve_bed refuses a
    patient outside the caller's department by itself, inside the write's
    transaction, whichever way the patient is named, and writes nothing. The
    refusal never carries the patient id in its answer."""
    elsewhere_dept = f"Elsewhere {_tag()}"
    elsewhere = _admit(site, department=elsewhere_dept)
    bed = _bed(site["ward"]["id"], "2")
    for named in ({"purpose": "transfer", "admission_id": elsewhere["aid"]},
                  {"purpose": "admission", "patient_id": elsewhere["pid"]}):
        with pytest.raises(adt_res.ReservationOutOfScope) as refused:
            adt_res.reserve_bed({"bed_id": bed["id"], **named}, actor=site["clerk"],
                                idempotency_key=_key())
        assert refused.value.patient_id == elsewhere["pid"]
        assert refused.value.http_status == 404
        assert adt_core.http_detail(refused.value) == _OUT_OF_SCOPE
    assert _all("SELECT id FROM bed_reservations WHERE bed_id = ?", (bed["id"],)) == []
    # The patient's own department holds the bed: the check is not refusing everyone.
    theirs = _user("doctor", elsewhere_dept)
    held = adt_res.reserve_bed({"bed_id": bed["id"], "purpose": "transfer",
                                "admission_id": elsewhere["aid"]},
                               actor=theirs, idempotency_key=_key())
    assert held["reservation"]["patient_id"] == elsewhere["pid"]


def test_a_scope_change_between_the_check_and_the_write_is_404_and_audited(
        adt_on, site, monkeypatch):
    """The race made certain: the handler's own checks are switched off, so
    only the check inside the service's transaction stands. The route answers
    with the same 404, records the attempt, and nothing is held."""
    import routes.adt.reservations as reservation_routes

    monkeypatch.setattr(reservation_routes, "_scoped_admission", lambda *a, **k: None)
    monkeypatch.setattr(reservation_routes, "_scoped_patient", lambda *a, **k: None)
    elsewhere = _admit(site, department=f"Elsewhere {_tag()}")
    bed = _bed(site["ward"]["id"], "2")
    out = _reserve(site["clerk"], {"bed_id": bed["id"], "purpose": "transfer",
                                   "admission_id": elsewhere["aid"]})
    assert out.status_code == 404, out.text
    assert _detail(out) == _OUT_OF_SCOPE
    assert _scope_denials(site["clerk"]) == [{"patient_id": elsewhere["pid"],
                                              "target_type": "patient"}]
    assert _all("SELECT id FROM bed_reservations WHERE bed_id = ?", (bed["id"],)) == []
    assert _all("SELECT id FROM audit_logs WHERE action_type = 'bed_reserved' "
                "AND actor_user_id = ?", (site["clerk"]["id"],)) == []


def test_a_bed_held_for_nobody_is_held_only_on_the_callers_own_wards(adt_on, site):
    other_dept = f"Surgery {_tag()}"
    surgeon = _user("doctor", other_dept)
    theirs = bm.save_ward({"code": f"RS-{_tag()}", "department": other_dept,
                           "cleaning_tracked": False}, actor=site["admin"])
    bm.register_beds({"ward_id": theirs["id"], "labels": ["1", "2"]}, actor=site["admin"])
    bed = _bed(theirs["id"], "1")
    nobody = _reserve(site["clerk"], {"bed_id": bed["id"], "purpose": "admission"})
    assert nobody.status_code == 404 and _detail(nobody)["error"] == "ward_not_found"
    # Named, for a patient of the caller's own: allowed on another team's ward.
    p = _admit(site, "1")
    named = _reserve(site["clerk"], {"bed_id": bed["id"], "purpose": "transfer",
                                     "admission_id": p["aid"]})
    assert named.status_code == 201, named.text
    # That team's own doctor and the administrator may hold their beds for nobody named.
    assert _reserve(surgeon, {"bed_id": _bed(theirs["id"], "2")["id"],
                              "purpose": "elective"}).status_code == 201
    with db_session() as db:
        db.execute("UPDATE bed_reservations SET status = 'released', closed_at = ?, "
                   "close_reason = 'test over' WHERE ward_id = ? AND status = 'active'",
                   (datetime.now(timezone.utc).isoformat(), theirs["id"]))


def test_the_tile_names_the_patient_only_inside_scope(adt_on, site):
    elsewhere_dept = f"Elsewhere {_tag()}"
    elsewhere = _admit(site, department=elsewhere_dept)
    bed = _bed(site["ward"]["id"], "2")
    assert _reserve(site["admin"], {"bed_id": bed["id"], "purpose": "transfer",
                                    "admission_id": elsewhere["aid"]}).status_code == 201
    tile = _tile(site["clerk"], site["ward"]["id"], "2")
    assert tile["status"] == "reserved" and tile["status_words"] == "Reserved for transfer"
    assert tile["reservation"]["patient"] is None
    assert tile["reservation"]["patient_hidden"] is True
    resp = client.get("/api/adt/board", params={"ward_id": site["ward"]["id"]},
                      headers=_hdr(site["clerk"]))
    assert elsewhere["name"] not in resp.text and elsewhere["pid"] not in resp.text
    listed = client.get("/api/adt/reservations", headers=_hdr(site["clerk"]))
    assert listed.status_code == 200
    assert elsewhere["name"] not in listed.text and elsewhere["pid"] not in listed.text
    # Hospital-wide, the administrator sees who it is for, and that read is audited.
    admin_tile = _tile(site["admin"], site["ward"]["id"], "2")
    assert admin_tile["reservation"]["patient"]["name"] == elsewhere["name"]


def test_the_list_hides_the_note_with_a_patient_outside_scope(adt_on, site):
    """The note is free text and often names the patient: outside scope the
    list shows the hold, never the note (round 1 review finding)."""
    elsewhere = _admit(site, department=f"Surgery {_tag()}")
    bed = _bed(site["ward"]["id"], "2")
    note = f"For {elsewhere['name']} coming from surgery"
    made = _reserve(site["admin"], {"bed_id": bed["id"], "purpose": "transfer",
                                    "admission_id": elsewhere["aid"], "note": note})
    assert made.status_code == 201, made.text
    rid = made.json()["reservation"]["id"]
    for params in ({}, {"ward_id": site["ward"]["id"]}):
        listed = client.get("/api/adt/reservations", params=params, headers=_hdr(site["clerk"]))
        assert listed.status_code == 200, listed.text
        assert elsewhere["name"] not in listed.text and elsewhere["pid"] not in listed.text
        entry = next(r for r in listed.json()["reservations"] if r["id"] == rid)
        assert entry["note"] is None and entry["patient_hidden"] is True
    # Nobody was named to the clerk, so the read wrote no phi_access row.
    assert _all("SELECT id FROM audit_logs WHERE action_type = 'bed_reservations_viewed' "
                "AND actor_user_id = ?", (site["clerk"]["id"],)) == []
    # Inside scope (the administrator, hospital-wide) the note is shown with the name.
    admin_list = client.get("/api/adt/reservations", params={"ward_id": site["ward"]["id"]},
                            headers=_hdr(site["admin"]))
    assert admin_list.status_code == 200, admin_list.text
    entry = next(r for r in admin_list.json()["reservations"] if r["id"] == rid)
    assert entry["note"] == note and entry["patient"]["name"] == elsewhere["name"]


def test_the_note_on_a_hold_for_nobody_goes_only_to_who_may_release_it(adt_on, site):
    """A team that sees another team's ward because its own patient lies
    there sees that ward's holds for nobody named, but not their notes."""
    other_dept = f"Surgery {_tag()}"
    surgeon = _user("doctor", other_dept)
    surgery_clerk = _user("ward_clerk", other_dept)
    theirs = bm.save_ward({"code": f"RS-{_tag()}", "department": other_dept,
                           "cleaning_tracked": False}, actor=site["admin"])
    bm.register_beds({"ward_id": theirs["id"], "labels": ["1", "2"]}, actor=site["admin"])
    # One of the site's own patients lies on the surgery ward, so the site
    # clerk's board and list show that ward.
    p = _admit(site)
    with db_session(immediate=True) as db:
        adt_core.locate_admission_on(db, admission_id=p["aid"], actor=site["admin"],
                                     movement_type="bed_assigned", reason="test",
                                     ward_id=theirs["id"], bed_id=_bed(theirs["id"], "1")["id"],
                                     boarder_acknowledged=True)
    note = "Held for the patient the theatre list names at two"
    made = _reserve(surgeon, {"bed_id": _bed(theirs["id"], "2")["id"], "purpose": "elective",
                              "note": note})
    assert made.status_code == 201, made.text
    rid = made.json()["reservation"]["id"]
    try:
        def entry_for(user: dict) -> dict:
            resp = client.get("/api/adt/reservations", params={"ward_id": theirs["id"]},
                              headers=_hdr(user))
            assert resp.status_code == 200, resp.text
            return next(r for r in resp.json()["reservations"] if r["id"] == rid)

        seen = entry_for(site["clerk"])
        assert seen["note"] is None and seen["patient_hidden"] is False
        assert seen["purpose"] == "elective"
        # Who made it, the ward's own department, and an account with no
        # department read the note.
        assert entry_for(surgeon)["note"] == note
        assert entry_for(surgery_clerk)["note"] == note
        assert entry_for(site["admin"])["note"] == note
    finally:
        with db_session() as db:
            db.execute("UPDATE bed_reservations SET status = 'released', closed_at = ?, "
                       "close_reason = 'test over' WHERE ward_id = ? AND status = 'active'",
                       (datetime.now(timezone.utc).isoformat(), theirs["id"]))


def test_the_list_names_in_scope_patients_and_audits_the_read(adt_on, site):
    p = _admit(site, "1")
    bed = _bed(site["ward"]["id"], "3")
    rid = _reserve(site["doctor"], {"bed_id": bed["id"], "purpose": "transfer",
                                    "admission_id": p["aid"]}).json()["reservation"]["id"]
    resp = client.get("/api/adt/reservations", params={"ward_id": site["ward"]["id"]},
                      headers=_hdr(site["doctor"]))
    assert resp.status_code == 200, resp.text
    entry = next(r for r in resp.json()["reservations"] if r["id"] == rid)
    assert entry["patient"]["name"] == p["name"] and entry["bed_label"] == "3"
    reads = _all("SELECT * FROM audit_logs WHERE action_type = 'bed_reservations_viewed' "
                 "AND actor_user_id = ?", (site["doctor"]["id"],))
    assert len(reads) == 1 and reads[0]["event_category"] == "phi_access"
    other = client.get("/api/adt/reservations", params={"ward_id": "WARD-NOPE"},
                       headers=_hdr(site["doctor"]))
    assert other.status_code == 404
    assert client.get("/api/adt/reservations", headers=_hdr(site["nurse"])).status_code == 403


def test_the_routes_are_404_with_the_module_off(monkeypatch, site):
    monkeypatch.setattr(config, "ADT_ENABLED", False)
    for resp in (
        client.get("/api/adt/reservations", headers=_hdr(site["admin"])),
        _reserve(site["admin"], {"bed_id": "X", "purpose": "admission"}),
        _release(site["admin"], "RES-NOPE"),
    ):
        assert resp.status_code == 404 and _detail(resp)["error"] == "module_disabled"
