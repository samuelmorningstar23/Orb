"""Reopen a stay whose end was recorded in error (adt-8b, W4-B, 4 October
2026).

What this file defends:

  1. A death recorded on the wrong patient is reopened: admission and
     patient active again, the end's columns cleared, the disposition voided
     (kept, and out of mortality), a 'correction' movement pointing at the
     discharge movement, the discharge movement itself untouched, a
     clinical_access 'stay_reopened' audit, and the portal login the death
     deactivated restored (with a security audit).
  2. THE BED. Taken back when nobody else holds it; when another patient now
     lies in it the reopened patient is awaiting a bed (bed cleared, ward
     kept), and the response says why.
  3. REFUSALS, each before anything is written: a stay with a discharge
     summary (409 summary_on_file, in words); a wrong MRN (409
     read_back_mismatch); no reason (422); a stay still active; a stay not
     ended through Beds and admissions; a patient with another active stay.
  4. WHO. A nurse, a ward clerk or an administrator gets 403; a doctor
     outside the ENDED admission's department gets 404, audited; the route is
     404 with ORB_ADT off.
  5. ONE TRANSACTION. An unwritable stay_reopened audit rolls everything back
     (503). An Idempotency-Key replay writes nothing more. The bundles the end
     cancelled are listed and stay cancelled.
"""
import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import config
import main
from auth import create_token, hash_password
from database import db_session
from services import bed_master_service as bm
from services import discharge_service, nabh_service
from services.adt import core as adt_core
from services.adt import end_stay as adt_end
from services.audit_service import AuditWriteError

client = TestClient(main.app)
DEPT = "General Medicine"   # conftest seeds D001, D002 and N001 in it


# ── helpers ──────────────────────────────────────────────────────────────────

def _tag() -> str:
    return uuid.uuid4().hex[:6].upper()


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _user(role: str, department: str | None = DEPT) -> dict:
    uid = f"RO{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO users (id, name, role, department, password_hash, is_active) "
            "VALUES (?, ?, ?, ?, ?, 1)",
            (uid, f"Reopen {role}", role, department, hash_password("password")),
        )
    return {"id": uid, "role": role, "department": department}


def _hdr(user: dict, key: str | None = None) -> dict:
    headers = {"Authorization": f"Bearer {create_token(user['id'], user['role'])}"}
    if key:
        headers["Idempotency-Key"] = key
    return headers


def _patient(*, admitted_hours_ago: float = 20, ward: str | None = None, bed: str | None = None,
             portal: bool = False, bundle: bool = False) -> dict:
    pid = f"PRO{uuid.uuid4().hex[:8].upper()}"
    aid = f"ADM-RO{uuid.uuid4().hex[:8].upper()}"
    mrn = f"MRN-{pid}"
    start = datetime.now(timezone.utc) - timedelta(hours=admitted_hours_ago)
    user_id = None
    with db_session() as db:
        if portal:
            user_id = f"PT{uuid.uuid4().hex[:8].upper()}"
            db.execute("INSERT INTO users (id, name, role, password_hash, is_active) "
                       "VALUES (?, 'Portal', 'patient', 'x', 1)", (user_id,))
        db.execute(
            "INSERT INTO patients (id, user_id, name, mrn, ward, bed, status, admission_date) "
            "VALUES (?, ?, 'Reopen Test', ?, ?, ?, 'admitted', ?)",
            (pid, user_id, mrn, ward, bed, _iso(start)),
        )
        db.execute(
            "INSERT INTO admissions (id, patient_id, admitted_by, department, ward, bed, status, "
            "created_at, admitted_at) VALUES (?, ?, 'D001', ?, ?, ?, 'active', ?, ?)",
            (aid, pid, DEPT, ward, bed, start.strftime("%Y-%m-%d %H:%M:%S"), _iso(start)),
        )
    if bundle:
        from services.sepsis_bundle_service import start_bundle
        start_bundle(pid, trigger_reason="Manual start")
    return {"pid": pid, "aid": aid, "mrn": mrn, "user_id": user_id}


def _one(sql: str, params: tuple = ()):
    with db_session() as db:
        row = db.execute(sql, params).fetchone()
    return dict(row) if row is not None else None


def _all(sql: str, params: tuple = ()) -> list[dict]:
    with db_session() as db:
        return [dict(r) for r in db.execute(sql, params).fetchall()]


def _admission(aid: str) -> dict:
    return _one("SELECT * FROM admissions WHERE id = ?", (aid,))


def _end(p: dict, doctor: dict, end_type: str = "death", **body):
    payload = {"end_type": end_type, "confirm_mrn": p["mrn"], **body}
    return client.post(f"/api/adt/admissions/{p['aid']}/end", json=payload, headers=_hdr(doctor))


def _reopen(aid: str, user: dict, *, confirm_mrn: str, reason: str = "Recorded on the wrong patient",
            key: str | None = None):
    return client.post(f"/api/adt/admissions/{aid}/reopen",
                       json={"reason": reason, "confirm_mrn": confirm_mrn},
                       headers=_hdr(user, key))


def _master_bed() -> tuple[dict, dict]:
    admin = _user("admin", None)
    ward = bm.save_ward({"code": f"RO-{_tag()}", "department": DEPT}, actor=admin)
    bm.register_beds({"ward_id": ward["id"], "labels": ["1"]}, actor=admin)
    bed = _one("SELECT * FROM housekeeping_units WHERE ward_id = ?", (ward["id"],))
    return ward, bed


def _place(aid: str, ward: dict, bed: dict) -> None:
    with db_session(immediate=True) as db:
        adt_core.locate_admission_on(db, admission_id=aid, actor={"id": "system", "role": "admin"},
                                     movement_type="bed_assigned", reason="test",
                                     ward_id=ward["id"], bed_id=bed["id"])


@pytest.fixture()
def adt_on(monkeypatch):
    monkeypatch.setattr(config, "ADT_ENABLED", True)
    yield


@pytest.fixture()
def doctor() -> dict:
    return _user("doctor")


def _mortality_recorded() -> int:
    start = datetime.now(timezone.utc) - timedelta(days=3)
    end = datetime.now(timezone.utc) + timedelta(days=1)
    with db_session() as db:
        base = nabh_service._disposition_base(db, start, end, None)
    return base["recorded"]


# ═════════════════════════════════════════════════════════════════════════════
#  1. A death on the wrong patient
# ═════════════════════════════════════════════════════════════════════════════

def test_a_death_recorded_on_the_wrong_patient_is_reopened(adt_on, doctor):
    p = _patient(portal=True)
    before = _mortality_recorded()
    ended = _end(p, doctor)
    assert ended.status_code == 200, ended.text
    end_move = ended.json()["movement_id"]
    discharge_row = _one("SELECT * FROM patient_movements WHERE id = ?", (end_move,))
    assert _mortality_recorded() == before + 1
    assert _one("SELECT is_active FROM users WHERE id = ?", (p["user_id"],))["is_active"] == 0

    resp = _reopen(p["aid"], doctor, confirm_mrn=p["mrn"])
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert out["status"] == "reopened" and out["corrects_movement_id"] == end_move
    assert out["corrected_end_type"] == "death"

    adm = _admission(p["aid"])
    assert adm["status"] == "active"
    assert adm["discharged_at"] is None and adm["discharge_type"] is None and adm["discharged_by"] is None
    assert _one("SELECT status FROM patients WHERE id = ?", (p["pid"],))["status"] == "admitted"

    # The disposition is voided, kept, and out of mortality.
    rows = _all("SELECT * FROM nabh_discharge_dispositions WHERE patient_id = ?", (p["pid"],))
    assert len(rows) == 1 and rows[0]["voided_at"] and rows[0]["voided_by"] == doctor["id"]
    assert "recorded in error" in rows[0]["void_reason"]
    assert out["dispositions_voided"] == [rows[0]["id"]]
    assert _mortality_recorded() == before

    # The correction row points at the discharge row; the discharge row is intact.
    correction = _one("SELECT * FROM patient_movements WHERE id = ?", (out["movement_id"],))
    assert correction["movement_type"] == "correction"
    assert correction["corrects_movement_id"] == end_move
    assert json.loads(correction["detail_json"])["corrected_end_type"] == "death"
    assert _one("SELECT * FROM patient_movements WHERE id = ?", (end_move,)) == discharge_row

    # The portal login the death deactivated is back, with a security audit.
    assert out["portal_login_restored"] is True
    assert _one("SELECT is_active FROM users WHERE id = ?", (p["user_id"],))["is_active"] == 1
    sec = _one("SELECT event_category FROM audit_logs WHERE action_type = "
               "'patient_portal_login_restored' AND patient_id = ?", (p["pid"],))
    assert sec["event_category"] == "security"
    audit = _one("SELECT event_category, actor_user_id, metadata_json FROM audit_logs "
                 "WHERE action_type = 'stay_reopened' AND patient_id = ?", (p["pid"],))
    assert audit["event_category"] == "clinical_access" and audit["actor_user_id"] == doctor["id"]


def test_a_reopened_stay_can_end_again(adt_on, doctor):
    p = _patient()
    assert _end(p, doctor).status_code == 200
    assert _reopen(p["aid"], doctor, confirm_mrn=p["mrn"]).status_code == 200
    again = _end(p, doctor, end_type="lama", reason="Left after the correction")
    assert again.status_code == 200, again.text
    assert _admission(p["aid"])["discharge_type"] == "lama"
    live = [d for d in _all("SELECT * FROM nabh_discharge_dispositions WHERE patient_id = ?",
                            (p["pid"],)) if not d["voided_at"]]
    assert [d["disposition"] for d in live] == ["lama"]


def test_a_non_death_end_leaves_the_portal_login_alone(adt_on, doctor):
    p = _patient(portal=True)
    assert _end(p, doctor, end_type="lama", reason="Left").status_code == 200
    with db_session() as db:   # deactivated by someone else, for another reason
        db.execute("UPDATE users SET is_active = 0 WHERE id = ?", (p["user_id"],))
    out = _reopen(p["aid"], doctor, confirm_mrn=p["mrn"]).json()
    assert out["portal_login_restored"] is False
    assert _one("SELECT is_active FROM users WHERE id = ?", (p["user_id"],))["is_active"] == 0


# ═════════════════════════════════════════════════════════════════════════════
#  2. The bed
# ═════════════════════════════════════════════════════════════════════════════

def test_the_bed_is_taken_back_when_it_is_free(adt_on, doctor):
    ward, bed = _master_bed()
    p = _patient()
    _place(p["aid"], ward, bed)
    assert _end(p, doctor).status_code == 200
    out = _reopen(p["aid"], doctor, confirm_mrn=p["mrn"]).json()
    assert out["bed_retaken"] is True and out["awaiting_bed"] is False
    assert out["bed_id"] == bed["id"]
    adm = _admission(p["aid"])
    assert adm["bed_id"] == bed["id"] and adm["bed"] == "1" and adm["ward_id"] == ward["id"]
    pat = _one("SELECT ward, bed FROM patients WHERE id = ?", (p["pid"],))
    assert pat == {"ward": ward["code"], "bed": "1"}
    loc = adt_core.current_location(p["pid"])
    assert loc["linked"] is True and loc["bed_id"] == bed["id"]


def test_the_patient_awaits_a_bed_when_another_patient_now_lies_in_it(adt_on, doctor):
    ward, bed = _master_bed()
    p, other = _patient(), _patient()
    _place(p["aid"], ward, bed)
    assert _end(p, doctor).status_code == 200
    _place(other["aid"], ward, bed)
    resp = _reopen(p["aid"], doctor, confirm_mrn=p["mrn"])
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert out["bed_retaken"] is False and out["awaiting_bed"] is True
    assert out["bed_not_retaken"] == "bed_occupied"
    assert out["bed_not_retaken_words"] == "Another patient now lies in that bed"
    adm = _admission(p["aid"])
    assert adm["status"] == "active" and adm["bed_id"] is None and adm["bed"] is None
    assert adm["ward_id"] == ward["id"]
    assert _admission(other["aid"])["bed_id"] == bed["id"]
    loc = adt_core.current_location(p["pid"])
    assert loc["awaiting_bed"] is True
    move = _one("SELECT detail_json FROM patient_movements WHERE id = ?", (out["movement_id"],))
    detail = json.loads(move["detail_json"])
    assert detail["previous_bed_id"] == bed["id"] and detail["awaiting_bed"] is True


def test_a_typed_bed_someone_else_now_holds_is_not_taken_back(adt_on, doctor):
    ward_text = f"RO Typed {_tag()}"
    p = _patient(ward=ward_text, bed="7")
    assert _end(p, doctor).status_code == 200
    _patient(ward=ward_text, bed="7")
    out = _reopen(p["aid"], doctor, confirm_mrn=p["mrn"]).json()
    assert out["awaiting_bed"] is True and out["bed_not_retaken"] == "bed_occupied"
    assert _admission(p["aid"])["bed"] is None


# ═════════════════════════════════════════════════════════════════════════════
#  3. Refusals, before anything is written
# ═════════════════════════════════════════════════════════════════════════════

def _nothing_reopened(p: dict) -> None:
    adm = _admission(p["aid"])
    assert adm["status"] != "active"
    assert _all("SELECT id FROM patient_movements WHERE admission_id = ? AND movement_type = "
                "'correction'", (p["aid"],)) == []
    assert all(d["voided_at"] is None for d in _all(
        "SELECT voided_at FROM nabh_discharge_dispositions WHERE patient_id = ?", (p["pid"],)))


def test_a_stay_with_a_discharge_summary_is_refused_in_words(adt_on, doctor, monkeypatch):
    async def _draft(_pid):
        return "Drafted summary."

    monkeypatch.setattr(discharge_service, "generate_discharge_summary", _draft)
    p = _patient()
    out = discharge_service.discharge_patient(p["pid"], doctor["id"], "Summary", [])
    assert out["status"] == "success"
    resp = _reopen(p["aid"], doctor, confirm_mrn=p["mrn"])
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["error"] == "summary_on_file"
    assert "corrected by a new admission" in detail["message"]
    _nothing_reopened(p)


def test_a_wrong_mrn_writes_nothing(adt_on, doctor):
    p = _patient(portal=True)
    assert _end(p, doctor).status_code == 200
    resp = _reopen(p["aid"], doctor, confirm_mrn="MRN-SOMEONE-ELSE")
    assert resp.status_code == 409 and resp.json()["detail"]["error"] == "read_back_mismatch"
    _nothing_reopened(p)
    assert _one("SELECT is_active FROM users WHERE id = ?", (p["user_id"],))["is_active"] == 0
    assert _one("SELECT 1 AS x FROM audit_logs WHERE action_type = 'stay_reopened' AND patient_id = ?",
                (p["pid"],)) is None


def test_a_reason_is_required(adt_on, doctor):
    p = _patient()
    assert _end(p, doctor).status_code == 200
    resp = _reopen(p["aid"], doctor, confirm_mrn=p["mrn"], reason="   ")
    assert resp.status_code == 422 and resp.json()["detail"]["error"] == "reason_required"
    _nothing_reopened(p)


def test_an_active_stay_has_nothing_to_reopen(adt_on, doctor):
    p = _patient()
    resp = _reopen(p["aid"], doctor, confirm_mrn=p["mrn"])
    assert resp.status_code == 409 and resp.json()["detail"]["error"] == "admission_active"


def test_a_stay_ended_without_the_module_cannot_be_reopened_here(adt_on, doctor):
    p = _patient()
    with db_session() as db:   # a roster-file close before ORB_ADT: no movement
        db.execute("UPDATE admissions SET status = 'discharged' WHERE id = ?", (p["aid"],))
        db.execute("UPDATE patients SET status = 'discharged' WHERE id = ?", (p["pid"],))
    resp = _reopen(p["aid"], doctor, confirm_mrn=p["mrn"])
    assert resp.status_code == 409 and resp.json()["detail"]["error"] == "no_recorded_end"
    _nothing_reopened(p)


def test_a_patient_with_another_active_stay_is_refused(adt_on, doctor):
    p = _patient()
    assert _end(p, doctor).status_code == 200
    with db_session() as db:
        db.execute("INSERT INTO admissions (id, patient_id, admitted_by, department, status, created_at) "
                   "VALUES (?, ?, 'D001', ?, 'active', ?)",
                   (f"ADM-RO{_tag()}", p["pid"], DEPT, _iso(datetime.now(timezone.utc))))
    resp = _reopen(p["aid"], doctor, confirm_mrn=p["mrn"])
    assert resp.status_code == 409 and resp.json()["detail"]["error"] == "patient_has_active_admission"
    _nothing_reopened(p)


# ═════════════════════════════════════════════════════════════════════════════
#  4. Who may reopen
# ═════════════════════════════════════════════════════════════════════════════

def test_a_nurse_a_ward_clerk_or_an_administrator_gets_403(adt_on, doctor):
    p = _patient()
    assert _end(p, doctor).status_code == 200
    for role in ("nurse", "ward_clerk", "admin"):
        resp = _reopen(p["aid"], _user(role), confirm_mrn=p["mrn"])
        assert resp.status_code == 403, (role, resp.text)
    _nothing_reopened(p)


def test_a_doctor_outside_the_ended_admissions_department_gets_404_audited(adt_on, doctor):
    p = _patient()
    assert _end(p, doctor).status_code == 200
    outsider = _user("doctor", department=f"Elsewhere {_tag()}")
    resp = _reopen(p["aid"], outsider, confirm_mrn=p["mrn"])
    assert resp.status_code == 404
    _nothing_reopened(p)
    denied = _one("SELECT event_category, metadata_json FROM audit_logs WHERE action_type = "
                  "'patient_scope_denied' AND patient_id = ? AND target_id = ?", (p["pid"], p["aid"]))
    assert denied["event_category"] == "authorization"
    assert "ended_admission" in denied["metadata_json"]
    assert _reopen("ADM-NO-SUCH", doctor, confirm_mrn="x").status_code == 404


def test_the_route_is_404_with_adt_off(monkeypatch, doctor):
    monkeypatch.setattr(config, "ADT_ENABLED", True)
    p = _patient()
    assert _end(p, doctor).status_code == 200
    monkeypatch.setattr(config, "ADT_ENABLED", False)
    resp = _reopen(p["aid"], doctor, confirm_mrn=p["mrn"])
    assert resp.status_code == 404 and resp.json()["detail"]["error"] == "module_disabled"
    _nothing_reopened(p)


# ═════════════════════════════════════════════════════════════════════════════
#  5. One transaction
# ═════════════════════════════════════════════════════════════════════════════

def test_an_unwritable_reopen_audit_rolls_everything_back(adt_on, doctor, monkeypatch):
    p = _patient(portal=True)
    assert _end(p, doctor).status_code == 200
    real = adt_core.log_audit_event

    def broken(*args, **kwargs):
        if kwargs.get("action_type") == "stay_reopened":
            raise AuditWriteError("audit table unwritable")
        return real(*args, **kwargs)

    monkeypatch.setattr(adt_core, "log_audit_event", broken)
    resp = _reopen(p["aid"], doctor, confirm_mrn=p["mrn"])
    assert resp.status_code == 503 and resp.json()["detail"]["error"] == "audit_unavailable"
    _nothing_reopened(p)
    assert _one("SELECT status FROM patients WHERE id = ?", (p["pid"],))["status"] == "discharged"
    assert _one("SELECT is_active FROM users WHERE id = ?", (p["user_id"],))["is_active"] == 0


def test_an_idempotency_key_replay_writes_nothing_more(adt_on, doctor):
    p = _patient()
    assert _end(p, doctor).status_code == 200
    key = f"reopen-{uuid.uuid4()}"
    first = _reopen(p["aid"], doctor, confirm_mrn=p["mrn"], key=key)
    again = _reopen(p["aid"], doctor, confirm_mrn=p["mrn"], key=key)
    assert first.status_code == 200 and again.status_code == 200
    assert again.json()["replayed"] is True
    assert again.json()["movement_id"] == first.json()["movement_id"]
    assert len(_all("SELECT id FROM patient_movements WHERE admission_id = ? AND movement_type = "
                    "'correction'", (p["aid"],))) == 1
    assert len(_all("SELECT id FROM audit_logs WHERE action_type = 'stay_reopened' AND patient_id = ?",
                    (p["pid"],))) == 1


def test_cancelled_bundles_are_listed_and_stay_cancelled(adt_on, doctor):
    p = _patient(bundle=True)
    ended = _end(p, doctor)
    assert ended.status_code == 200 and ended.json()["bundles_cancelled"]
    out = _reopen(p["aid"], doctor, confirm_mrn=p["mrn"]).json()
    assert out["bundles_not_restored"]
    assert {b["id"] for b in out["bundles_not_restored"]} == {
        b["id"] for b in ended.json()["bundles_cancelled"]}
    assert all(b["status"] == "cancelled" for b in out["bundles_not_restored"])
    assert all(r["status"] == "cancelled" for r in _all(
        "SELECT status FROM care_bundles WHERE patient_id = ?", (p["pid"],)))


def test_no_string_in_the_reopen_lane_carries_an_em_dash():
    import inspect

    from routes.adt import end_stay as route

    for source in (inspect.getsource(adt_end.reopen_stay_on), inspect.getsource(adt_end.reopen_stay),
                   inspect.getsource(adt_end._bed_for_reopen_on), str(adt_end._REOPEN_BED_WORDS),
                   inspect.getsource(route.reopen_admission),
                   inspect.getsource(route._scoped_ended_admission)):
        assert "\u2014" not in source
