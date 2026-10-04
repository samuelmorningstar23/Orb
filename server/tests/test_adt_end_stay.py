"""Ends of stay with a type, a server read-back and one disposition (adt-8a,
W3-B, 4 October 2026).

What this file defends:

  1. MIGRATION 59 adds source, the void columns and needs_reconciliation to
     nabh_discharge_dispositions, keeps every existing row as 'admin', and
     refuses an unknown source.
  2. FLAG OFF. discharge_patient writes exactly the rows it always wrote: the
     status flips, the bundle closure with its audit, one draft summary, and
     no end type, movement or disposition. The response has no end-of-stay
     block, and the discharge route ignores a body.
  3. FLAG ON, POST /api/adt/admissions/{id}/end. Each end type sets the
     admission's columns, writes the 'discharge' movement and the right
     disposition (a death within 48 hours is 'death_within_48h'). A wrong
     confirm_mrn writes nothing. A nurse or an administrator is 403; a doctor
     outside the department is 404, audited. The bed is free at commit. A
     death deactivates the portal login and issues no link. Open escalation
     chains are listed and never closed. An Idempotency-Key replay writes
     nothing more.
  4. DISPOSITIONS ARE NEVER DOUBLED. A desk-keyed row for the same admission,
     or for the patient inside the stay window, is linked; a differing type is
     flagged and the desk's value kept. Mortality and LAMA compute from them;
     voided rows never count.
  5. THE DISCHARGE ROUTE with the flag on records routine, referred out,
     transferred out and DAMA (read-back and reason), and points LAMA,
     absconded and death to End of stay before drafting anything.
  6. THE ROSTER CLOSE goes through end_stay when on, keeping 'transferred'.
  7. discharge_preconditions returns [] and every door asks it (AST-pinned,
     and proven by making it refuse); it reads no NEWS2 and no bundle.
  8. NABH inpatient days count per stay once ADT has recorded stays.
"""
import ast
import importlib
import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import config
import main
from auth import create_token, hash_password
from database import db_session
from db_backend import DBIntegrityError
from services import bed_master_service as bm
from services import discharge_service, nabh_service, roster_import
from services.adt import core as adt_core
from services.adt import end_stay as adt_end

client = TestClient(main.app)

M59 = importlib.import_module("migrations_his.m0059_adt_discharge_dispositions")
SERVER = Path(__file__).resolve().parents[1]
DEPT = "General Medicine"   # conftest seeds D001, D002 and N001 in it


# ── helpers ──────────────────────────────────────────────────────────────────

def _tag() -> str:
    return uuid.uuid4().hex[:6].upper()


def _user(role: str, department: str | None = DEPT) -> dict:
    uid = f"ES{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO users (id, name, role, department, password_hash, is_active) "
            "VALUES (?, ?, ?, ?, ?, 1)",
            (uid, f"End stay {role}", role, department, hash_password("password")),
        )
    return {"id": uid, "role": role, "department": department}


def _hdr(user: dict) -> dict:
    return {"Authorization": f"Bearer {create_token(user['id'], user['role'])}"}


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _patient(*, admitted_hours_ago: float = 72, department: str = DEPT, ward: str | None = None,
             bed: str | None = None, portal: bool = False, bundle: bool = False) -> dict:
    """An admitted patient with one active admission whose stay began
    `admitted_hours_ago`. Returns ids and the MRN."""
    pid = f"PES{uuid.uuid4().hex[:8].upper()}"
    aid = f"ADM-ES{uuid.uuid4().hex[:8].upper()}"
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
            "VALUES (?, ?, 'End Stay Test', ?, ?, ?, 'admitted', ?)",
            (pid, user_id, mrn, ward, bed, _iso(start)),
        )
        db.execute(
            "INSERT INTO admissions (id, patient_id, admitted_by, department, ward, bed, status, "
            "created_at) VALUES (?, ?, 'D001', ?, ?, ?, 'active', ?)",
            (aid, pid, department, ward, bed, start.strftime("%Y-%m-%d %H:%M:%S")),
        )
        db.execute("UPDATE admissions SET admitted_at = ? WHERE id = ?", (_iso(start), aid))
    if bundle:
        from services.sepsis_bundle_service import start_bundle
        start_bundle(pid, trigger_reason="Manual start")
    return {"pid": pid, "aid": aid, "mrn": mrn, "user_id": user_id, "start": start}


def _admission(aid: str) -> dict:
    with db_session() as db:
        return dict(db.execute("SELECT * FROM admissions WHERE id = ?", (aid,)).fetchone())


def _one(sql: str, params: tuple = ()):
    with db_session() as db:
        row = db.execute(sql, params).fetchone()
    return dict(row) if row is not None else None


def _all(sql: str, params: tuple = ()) -> list[dict]:
    with db_session() as db:
        return [dict(r) for r in db.execute(sql, params).fetchall()]


def _dispositions(pid: str) -> list[dict]:
    return _all("SELECT * FROM nabh_discharge_dispositions WHERE patient_id = ? ORDER BY id", (pid,))


def _end(aid: str, user: dict, **body):
    payload = {"end_type": "routine", "confirm_mrn": None, **body}
    headers = _hdr(user)
    key = payload.pop("idempotency_key", None)
    if key:
        headers["Idempotency-Key"] = key
    return client.post(f"/api/adt/admissions/{aid}/end", json=payload, headers=headers)


@pytest.fixture()
def adt_on(monkeypatch):
    monkeypatch.setattr(config, "ADT_ENABLED", True)
    yield


@pytest.fixture()
def doctor() -> dict:
    return _user("doctor")


# ═════════════════════════════════════════════════════════════════════════════
#  1. Migration 59
# ═════════════════════════════════════════════════════════════════════════════

def test_migration_59_is_ledgered_with_its_columns():
    with db_session() as db:
        ledger = {r[0] for r in db.execute("SELECT version FROM schema_migrations")}
        cols = [r[1] for r in db.execute("PRAGMA table_info(nabh_discharge_dispositions)")]
        index = db.execute("SELECT 1 FROM sqlite_master WHERE name = 'idx_nabh_disposition_admission'"
                           ).fetchone()
    assert 59 in ledger
    assert cols[-5:] == ["source", "voided_at", "voided_by", "void_reason", "needs_reconciliation"]
    assert index is not None


def test_migration_59_keeps_existing_rows_as_admin_and_refuses_an_unknown_source(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "dispo.db"))
    conn.execute("""CREATE TABLE nabh_discharge_dispositions (
        id INTEGER PRIMARY KEY AUTOINCREMENT, patient_id TEXT NOT NULL, admission_id TEXT,
        disposition TEXT NOT NULL, occurred_at TIMESTAMP NOT NULL, recorded_by TEXT, notes TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
    conn.execute("INSERT INTO nabh_discharge_dispositions (patient_id, disposition, occurred_at) "
                 "VALUES ('P1', 'death', '2026-09-01T00:00:00+00:00')")
    conn.commit()
    M59.apply(conn)
    M59.apply(conn)
    conn.commit()
    row = conn.execute("SELECT source, voided_at, needs_reconciliation FROM "
                       "nabh_discharge_dispositions").fetchone()
    assert row == ("admin", None, 0)
    with pytest.raises(DBIntegrityError):
        conn.execute("INSERT INTO nabh_discharge_dispositions (patient_id, disposition, occurred_at, "
                     "source) VALUES ('P2', 'death', '2026-09-02T00:00:00+00:00', 'robot')")
    conn.close()


# ═════════════════════════════════════════════════════════════════════════════
#  2. Flag off: the discharge is what it always was
# ═════════════════════════════════════════════════════════════════════════════

def test_flag_off_discharge_writes_the_same_rows_as_before(monkeypatch):
    monkeypatch.setattr(config, "ADT_ENABLED", False)
    p = _patient(bundle=True)
    # A second (wrongly) active admission is closed too, as it always was.
    second = f"ADM-ES2{_tag()}"
    with db_session() as db:
        db.execute("INSERT INTO admissions (id, patient_id, admitted_by, department, status) "
                   "VALUES (?, ?, 'D001', ?, 'active')", (second, p["pid"], DEPT))
    result = discharge_service.discharge_patient(
        p["pid"], "D001", "Summary text", [], discharge_type="death", confirm_mrn="wrong")
    assert result["status"] == "success"
    assert set(result) == {"status", "summary_id", "discharged_at", "review_status", "summary_url",
                           "qr_code_url", "secure_link_issued", "secure_link_reason"}
    assert _one("SELECT status FROM patients WHERE id = ?", (p["pid"],))["status"] == "discharged"
    for aid in (p["aid"], second):
        adm = _admission(aid)
        assert adm["status"] == "discharged"
        assert (adm["discharged_at"], adm["discharge_type"], adm["discharged_by"]) == (None, None, None)
    assert _all("SELECT * FROM patient_movements WHERE patient_id = ?", (p["pid"],)) == []
    assert _dispositions(p["pid"]) == []
    bundles = _all("SELECT status FROM care_bundles WHERE patient_id = ?", (p["pid"],))
    assert bundles and all(b["status"] == "cancelled" for b in bundles)
    audit = _one("SELECT metadata_json FROM audit_logs WHERE action_type = 'bundle_cancelled' "
                 "AND patient_id = ?", (p["pid"],))
    assert '"reason": "patient discharged"' in audit["metadata_json"]
    summaries = _all("SELECT review_status FROM discharge_summaries WHERE patient_id = ?", (p["pid"],))
    assert summaries == [{"review_status": "draft"}]
    assert _all("SELECT action_type FROM audit_logs WHERE patient_id = ? AND action_type IN "
                "('stay_ended', 'death_recorded')", (p["pid"],)) == []
    # A second discharge writes nothing.
    assert discharge_service.discharge_patient(p["pid"], "D001", "again", [])["status"] == "already_discharged"
    assert len(_all("SELECT id FROM discharge_summaries WHERE patient_id = ?", (p["pid"],))) == 1


def test_flag_off_the_discharge_route_ignores_a_body(monkeypatch, doctor):
    monkeypatch.setattr(config, "ADT_ENABLED", False)

    async def _draft(_pid):
        return "Drafted summary."

    monkeypatch.setattr(discharge_service, "generate_discharge_summary", _draft)
    p = _patient()
    resp = client.post(f"/api/vigil/patients/{p['pid']}/discharge",
                       json={"discharge_type": "death"}, headers=_hdr(doctor))
    assert resp.status_code == 200, resp.text
    assert "end_of_stay" not in resp.json()
    assert _admission(p["aid"])["discharge_type"] is None


# ═════════════════════════════════════════════════════════════════════════════
#  3. POST /api/adt/admissions/{id}/end
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("end_type", ["routine", "referred_out", "transferred_out", "dama",
                                      "lama", "absconded", "death"])
def test_each_end_type_sets_columns_movement_and_disposition(adt_on, doctor, end_type):
    p = _patient(admitted_hours_ago=72)
    body = {"end_type": end_type, "confirm_mrn": f"  {p['mrn'].lower()} "}
    if end_type in adt_end.REASON_REQUIRED_TYPES:
        body["reason"] = "Family took the patient home"
    resp = _end(p["aid"], doctor, **body)
    assert resp.status_code == 200, resp.text
    out = resp.json()
    adm = _admission(p["aid"])
    assert adm["status"] == "discharged"
    assert adm["discharge_type"] == end_type and adm["discharged_by"] == doctor["id"]
    assert adm["discharged_at"] and adm["discharged_at"].endswith("+00:00")
    assert _one("SELECT status FROM patients WHERE id = ?", (p["pid"],))["status"] == "discharged"
    [move] = _all("SELECT * FROM patient_movements WHERE admission_id = ?", (p["aid"],))
    assert move["movement_type"] == "discharge" and move["end_type"] == end_type
    assert move["id"] == out["movement_id"] and move["recorded_at"]
    [dispo] = _dispositions(p["pid"])
    assert dispo["disposition"] == end_type   # 72 h: a death is not within 48 h
    assert dispo["source"] == "adt" and dispo["admission_id"] == p["aid"]
    assert out["discharge_link_issued"] is False and out["preconditions"] == []
    action = "death_recorded" if end_type == "death" else "stay_ended"
    audit = _one("SELECT event_category FROM audit_logs WHERE action_type = ? AND patient_id = ?",
                 (action, p["pid"]))
    assert audit["event_category"] == "clinical_access"


def test_a_death_within_48_hours_is_death_within_48h(adt_on, doctor):
    p = _patient(admitted_hours_ago=20)
    resp = _end(p["aid"], doctor, end_type="death", confirm_mrn=p["mrn"])
    assert resp.status_code == 200, resp.text
    assert resp.json()["disposition"]["disposition"] == "death_within_48h"
    assert _dispositions(p["pid"])[0]["disposition"] == "death_within_48h"


def test_a_wrong_confirm_mrn_writes_nothing(adt_on, doctor):
    p = _patient()
    for bad in ("MRN-SOMEONE-ELSE", "", None):
        resp = _end(p["aid"], doctor, end_type="death", confirm_mrn=bad)
        assert resp.status_code in (409, 422), resp.text
    resp = _end(p["aid"], doctor, end_type="death", confirm_mrn="MRN-SOMEONE-ELSE")
    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "read_back_mismatch"
    adm = _admission(p["aid"])
    assert adm["status"] == "active" and adm["discharge_type"] is None
    assert _all("SELECT id FROM patient_movements WHERE admission_id = ?", (p["aid"],)) == []
    assert _dispositions(p["pid"]) == []
    assert _all("SELECT id FROM audit_logs WHERE action_type = 'death_recorded' AND patient_id = ?",
                (p["pid"],)) == []


def test_lama_dama_and_absconded_need_a_reason(adt_on, doctor):
    p = _patient()
    for end_type in ("lama", "dama", "absconded"):
        resp = _end(p["aid"], doctor, end_type=end_type, confirm_mrn=p["mrn"])
        assert resp.status_code == 422 and resp.json()["detail"]["error"] == "reason_required"
    assert _admission(p["aid"])["status"] == "active"


def test_future_and_too_old_end_times_are_refused(adt_on, doctor):
    p = _patient(admitted_hours_ago=200)
    future = _iso(datetime.now(timezone.utc) + timedelta(hours=2))
    old = _iso(datetime.now(timezone.utc) - timedelta(hours=config.ADT_BACKTIME_HOURS + 5))
    assert _end(p["aid"], doctor, confirm_mrn=p["mrn"], occurred_at=future).json()["detail"]["error"] == "future_time"
    assert _end(p["aid"], doctor, confirm_mrn=p["mrn"], occurred_at=old).json()["detail"]["error"] == "beyond_backtime"
    within = _iso(datetime.now(timezone.utc) - timedelta(hours=3))
    resp = _end(p["aid"], doctor, confirm_mrn=p["mrn"], occurred_at=within)
    assert resp.status_code == 200, resp.text


def test_a_nurse_or_an_administrator_posting_a_death_gets_403(adt_on):
    p = _patient()
    for role in ("nurse", "admin", "ward_clerk"):
        resp = _end(p["aid"], _user(role), end_type="death", confirm_mrn=p["mrn"])
        assert resp.status_code == 403, (role, resp.text)
    assert _admission(p["aid"])["status"] == "active"


def test_a_doctor_outside_the_department_gets_404_and_the_attempt_is_audited(adt_on):
    p = _patient()
    outsider = _user("doctor", department=f"Elsewhere {_tag()}")
    resp = _end(p["aid"], outsider, end_type="death", confirm_mrn=p["mrn"])
    assert resp.status_code == 404
    assert _admission(p["aid"])["status"] == "active"
    denied = _one("SELECT event_category FROM audit_logs WHERE action_type = 'patient_scope_denied' "
                  "AND patient_id = ? AND target_id = ?", (p["pid"], p["aid"]))
    assert denied["event_category"] == "authorization"
    assert _end("ADM-NO-SUCH", _user("doctor"), confirm_mrn="x").status_code == 404


def test_the_route_is_404_with_adt_off(monkeypatch, doctor):
    monkeypatch.setattr(config, "ADT_ENABLED", False)
    p = _patient()
    resp = _end(p["aid"], doctor, confirm_mrn=p["mrn"])
    assert resp.status_code == 404 and resp.json()["detail"]["error"] == "module_disabled"


def test_the_bed_is_free_at_commit(adt_on, doctor):
    admin = _user("admin", department=None)
    ward = bm.save_ward({"code": f"ES-{_tag()}", "department": DEPT}, actor=admin)
    bm.register_beds({"ward_id": ward["id"], "labels": ["1"]}, actor=admin)
    bed = _one("SELECT id FROM housekeeping_units WHERE ward_id = ?", (ward["id"],))
    first, second = _patient(), _patient()
    with db_session(immediate=True) as db:
        adt_core.locate_admission_on(db, admission_id=first["aid"], actor=admin,
                                     movement_type="bed_assigned", reason="test",
                                     ward_id=ward["id"], bed_id=bed["id"])
    with pytest.raises(adt_core.AdtConflict):
        with db_session(immediate=True) as db:
            adt_core.locate_admission_on(db, admission_id=second["aid"], actor=admin,
                                         movement_type="bed_assigned", reason="test",
                                         ward_id=ward["id"], bed_id=bed["id"])
    resp = _end(first["aid"], doctor, confirm_mrn=first["mrn"])
    assert resp.status_code == 200 and resp.json()["bed_freed"] == bed["id"]
    move = _one("SELECT from_bed_id, from_ward_id FROM patient_movements WHERE admission_id = ?",
                (first["aid"],))
    assert move == {"from_bed_id": bed["id"], "from_ward_id": ward["id"]}
    with db_session(immediate=True) as db:
        placed = adt_core.locate_admission_on(db, admission_id=second["aid"], actor=admin,
                                              movement_type="bed_assigned", reason="test",
                                              ward_id=ward["id"], bed_id=bed["id"])
    assert placed["bed_id"] == bed["id"]
    assert _admission(first["aid"])["bed_id"] == bed["id"]   # kept as the record of the stay


def test_a_death_deactivates_the_portal_login_and_issues_no_link(adt_on, doctor):
    p = _patient(portal=True)
    resp = _end(p["aid"], doctor, end_type="death", confirm_mrn=p["mrn"])
    assert resp.status_code == 200, resp.text
    assert resp.json()["portal_login_deactivated"] is True
    assert _one("SELECT is_active FROM users WHERE id = ?", (p["user_id"],))["is_active"] == 0
    move = _one("SELECT detail_json FROM patient_movements WHERE admission_id = ?", (p["aid"],))
    assert json.loads(move["detail_json"])["portal_login_deactivated"] == p["user_id"]
    assert _all("SELECT token_id FROM discharge_access_tokens WHERE patient_id = ?", (p["pid"],)) == []
    sec = _one("SELECT event_category FROM audit_logs WHERE action_type = "
               "'patient_portal_login_deactivated' AND patient_id = ?", (p["pid"],))
    assert sec["event_category"] == "security"
    # A non-death end leaves the login alone.
    q = _patient(portal=True)
    assert _end(q["aid"], doctor, confirm_mrn=q["mrn"]).json()["portal_login_deactivated"] is False
    assert _one("SELECT is_active FROM users WHERE id = ?", (q["user_id"],))["is_active"] == 1


def test_open_escalations_are_listed_and_never_closed_and_bundles_close(adt_on, doctor):
    p = _patient(bundle=True)
    esc_id = f"ESC-{_tag()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO clinical_escalations (id, patient_id, status, news2, peak_news2, raised_at) "
            "VALUES (?, ?, 'open', 7, 7, ?)", (esc_id, p["pid"], _iso(datetime.now(timezone.utc))))
    resp = _end(p["aid"], doctor, end_type="death", confirm_mrn=p["mrn"])
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert [e["id"] for e in out["open_escalations"]] == [esc_id]
    assert _one("SELECT status FROM clinical_escalations WHERE id = ?", (esc_id,))["status"] == "open"
    assert out["bundles_cancelled"] and all(
        b["status"] == "cancelled" for b in _all("SELECT status FROM care_bundles WHERE patient_id = ?",
                                                 (p["pid"],)))


def test_an_idempotency_key_replay_writes_nothing_more(adt_on, doctor):
    p = _patient()
    key = f"end-{uuid.uuid4()}"
    first = _end(p["aid"], doctor, confirm_mrn=p["mrn"], idempotency_key=key)
    again = _end(p["aid"], doctor, confirm_mrn=p["mrn"], idempotency_key=key)
    assert first.status_code == 200 and again.status_code == 200
    assert again.json()["replayed"] is True
    assert again.json()["movement_id"] == first.json()["movement_id"]
    assert len(_all("SELECT id FROM patient_movements WHERE admission_id = ?", (p["aid"],))) == 1
    assert len(_dispositions(p["pid"])) == 1
    # Without the key, a second end of the same stay is refused.
    assert _end(p["aid"], doctor, confirm_mrn=p["mrn"]).json()["detail"]["error"] == "admission_not_active"


# ═════════════════════════════════════════════════════════════════════════════
#  4. Dispositions are never doubled
# ═════════════════════════════════════════════════════════════════════════════

def test_a_desk_disposition_for_the_same_admission_is_kept_and_a_differing_type_flagged(adt_on, doctor):
    p = _patient()
    keyed = nabh_service.record_disposition(
        patient_id=p["pid"], admission_id=p["aid"], disposition="routine",
        occurred_at=_iso(datetime.now(timezone.utc) - timedelta(hours=1)), recorded_by="ADMIN")
    resp = _end(p["aid"], doctor, end_type="death", confirm_mrn=p["mrn"])
    assert resp.status_code == 200, resp.text
    [row] = _dispositions(p["pid"])
    assert row["id"] == keyed["id"] and row["disposition"] == "routine"
    assert row["needs_reconciliation"] == 1 and row["source"] == "admin"
    assert resp.json()["disposition"]["action"] == "matched_admission_flagged"
    flagged = _one("SELECT event_category FROM audit_logs WHERE action_type = "
                   "'disposition_reconcile_flagged' AND patient_id = ?", (p["pid"],))
    assert flagged["event_category"] == "clinical_access"


def test_a_matching_desk_disposition_is_linked_not_duplicated(adt_on, doctor):
    p = _patient()
    nabh_service.record_disposition(
        patient_id=p["pid"], disposition="lama",
        occurred_at=_iso(datetime.now(timezone.utc) - timedelta(minutes=30)))
    resp = _end(p["aid"], doctor, end_type="lama", confirm_mrn=p["mrn"], reason="Left at night")
    assert resp.status_code == 200, resp.text
    [row] = _dispositions(p["pid"])
    assert row["admission_id"] == p["aid"] and row["needs_reconciliation"] == 0
    assert resp.json()["disposition"]["action"] == "linked_stay_window"


def test_a_desk_disposition_outside_the_stay_window_is_not_linked(adt_on, doctor):
    p = _patient(admitted_hours_ago=24)
    nabh_service.record_disposition(
        patient_id=p["pid"], disposition="routine",
        occurred_at=_iso(datetime.now(timezone.utc) - timedelta(days=20)))
    resp = _end(p["aid"], doctor, end_type="routine", confirm_mrn=p["mrn"])
    assert resp.status_code == 200
    rows = _dispositions(p["pid"])
    assert len(rows) == 2 and {r["source"] for r in rows} == {"admin", "adt"}


def test_a_desk_disposition_keyed_after_the_end_of_stay_corrects_the_same_row(adt_on, doctor):
    """The other order: the end of stay wrote the row first, then the desk
    keys the same stay at another instant. Still one row; the desk's value is
    kept and a differing type is flagged."""
    p = _patient()
    assert _end(p["aid"], doctor, end_type="routine", confirm_mrn=p["mrn"]).status_code == 200
    [auto] = _dispositions(p["pid"])
    later = _iso(datetime.now(timezone.utc) - timedelta(minutes=3))
    same = nabh_service.record_disposition(patient_id=p["pid"], disposition="routine",
                                           occurred_at=later, recorded_by="ADMIN")
    [row] = _dispositions(p["pid"])
    assert same["id"] == row["id"] == auto["id"]
    assert row["needs_reconciliation"] == 0 and row["recorded_by"] == "ADMIN"
    nabh_service.record_disposition(patient_id=p["pid"], admission_id=p["aid"],
                                    disposition="lama", occurred_at=later)
    [row] = _dispositions(p["pid"])
    assert row["disposition"] == "lama" and row["needs_reconciliation"] == 1
    assert row["source"] == "adt" and row["admission_id"] == p["aid"]


def test_the_desk_still_writes_its_own_rows_when_the_module_wrote_none(adt_on, doctor):
    """Only rows the end of stay wrote are corrected in place: two desk
    keyings at different instants stay two rows, as they always were, and a
    desk row naming another admission is not merged into an ADT row."""
    pid = f"PDK{uuid.uuid4().hex[:8].upper()}"
    now = datetime.now(timezone.utc)
    for hours in (30, 2):
        nabh_service.record_disposition(patient_id=pid, disposition="routine",
                                        occurred_at=_iso(now - timedelta(hours=hours)))
    assert len(_dispositions(pid)) == 2
    p = _patient()
    assert _end(p["aid"], doctor, end_type="routine", confirm_mrn=p["mrn"]).status_code == 200
    nabh_service.record_disposition(patient_id=p["pid"], admission_id=f"ADM-OTHER{_tag()}",
                                    disposition="routine",
                                    occurred_at=_iso(now - timedelta(minutes=10)))
    rows = _dispositions(p["pid"])
    assert len(rows) == 2 and {r["source"] for r in rows} == {"admin", "adt"}


def test_a_desk_disposition_for_a_readmission_never_overwrites_the_earlier_stay(adt_on, doctor):
    """Readmitted inside the 24 hours after an end of stay: the desk keying
    the new stay's death is a row of its own, and the earlier stay's routine
    disposition is untouched."""
    p = _patient(admitted_hours_ago=72)
    now = datetime.now(timezone.utc)
    resp = _end(p["aid"], doctor, end_type="routine", confirm_mrn=p["mrn"],
                occurred_at=_iso(now - timedelta(hours=3)))
    assert resp.status_code == 200, resp.text
    with db_session() as db:
        db.execute("INSERT INTO admissions (id, patient_id, admitted_by, department, status, "
                   "created_at, admitted_at) VALUES (?, ?, 'D001', ?, 'active', ?, ?)",
                   (f"ADM-RE{_tag()}", p["pid"], DEPT, _iso(now - timedelta(hours=2)),
                    _iso(now - timedelta(hours=2))))
    nabh_service.record_disposition(patient_id=p["pid"], disposition="death",
                                    occurred_at=_iso(now - timedelta(minutes=30)))
    rows = _dispositions(p["pid"])
    assert [(r["source"], r["disposition"], r["needs_reconciliation"]) for r in rows] == [
        ("adt", "routine", 0), ("admin", "death", 0)]


def test_mortality_and_lama_compute_from_the_dispositions_and_voided_rows_never_count(adt_on, doctor):
    start = datetime.now(timezone.utc) - timedelta(days=1)
    end = datetime.now(timezone.utc) + timedelta(days=1)
    with db_session() as db:
        before = nabh_service._disposition_base(db, start, end, None)
    dead, left = _patient(admitted_hours_ago=10), _patient()
    assert _end(dead["aid"], doctor, end_type="death", confirm_mrn=dead["mrn"]).status_code == 200
    assert _end(left["aid"], doctor, end_type="lama", confirm_mrn=left["mrn"],
                reason="Wanted to go home").status_code == 200
    with db_session() as db:
        after = nabh_service._disposition_base(db, start, end, None)
    assert after["recorded"] - before["recorded"] == 2
    assert after["rows"]["death_within_48h"] - before["rows"].get("death_within_48h", 0) == 1
    assert after["rows"]["lama"] - before["rows"].get("lama", 0) == 1
    # A voided disposition (a stay reopened in error, adt-8b) is never counted.
    with db_session() as db:
        db.execute("UPDATE nabh_discharge_dispositions SET voided_at = ?, voided_by = 'X', "
                   "void_reason = 'wrong patient' WHERE patient_id = ?",
                   (_iso(datetime.now(timezone.utc)), dead["pid"]))
        voided = nabh_service._disposition_base(db, start, end, None)
    assert voided["recorded"] == after["recorded"] - 1
    listed = nabh_service.list_dispositions(period_start=start.date().isoformat(),
                                            period_end=end.date().isoformat(), limit=2000)
    assert dead["pid"] not in {d["patient_id"] for d in listed["dispositions"]}
    every = nabh_service.list_dispositions(period_start=start.date().isoformat(),
                                           period_end=end.date().isoformat(), limit=2000,
                                           include_voided=True)
    assert dead["pid"] in {d["patient_id"] for d in every["dispositions"]}


# ═════════════════════════════════════════════════════════════════════════════
#  5. The doctor's discharge route with the flag on
# ═════════════════════════════════════════════════════════════════════════════

@pytest.fixture()
def drafted(monkeypatch):
    async def _draft(_pid):
        return "Drafted summary."

    monkeypatch.setattr(discharge_service, "generate_discharge_summary", _draft)


@pytest.mark.parametrize("end_type", ["lama", "absconded", "death"])
def test_lama_absconded_and_death_are_pointed_to_end_of_stay(adt_on, drafted, doctor, end_type):
    p = _patient()
    resp = client.post(f"/api/vigil/patients/{p['pid']}/discharge",
                       json={"discharge_type": end_type}, headers=_hdr(doctor))
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["error"] == "use_end_of_stay"
    assert detail["end_of_stay_route"] == f"/api/adt/admissions/{p['aid']}/end"
    assert _admission(p["aid"])["status"] == "active"
    assert _all("SELECT id FROM discharge_summaries WHERE patient_id = ?", (p["pid"],)) == []


def test_a_routine_discharge_records_the_end_with_the_summary(adt_on, drafted, doctor):
    p = _patient(bundle=True)
    resp = client.post(f"/api/vigil/patients/{p['pid']}/discharge", headers=_hdr(doctor))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    end = body["end_of_stay"]
    assert end["end_type"] == "routine" and end["bundles_cancelled"]
    adm = _admission(p["aid"])
    assert adm["discharge_type"] == "routine" and adm["discharged_by"] == doctor["id"]
    move = _one("SELECT discharge_summary_id, end_type FROM patient_movements WHERE admission_id = ?",
                (p["aid"],))
    assert move == {"discharge_summary_id": body["summary_id"], "end_type": "routine"}
    assert _dispositions(p["pid"])[0]["disposition"] == "routine"


def test_a_dama_needs_the_mrn_typed_back_and_a_reason(adt_on, drafted, doctor):
    p = _patient()
    url = f"/api/vigil/patients/{p['pid']}/discharge"
    resp = client.post(url, json={"discharge_type": "dama", "reason": "Family decision"},
                       headers=_hdr(doctor))
    assert resp.status_code == 409 and resp.json()["detail"]["error"] == "read_back_mismatch"
    resp = client.post(url, json={"discharge_type": "dama", "confirm_mrn": p["mrn"]},
                       headers=_hdr(doctor))
    assert resp.status_code == 422 and resp.json()["detail"]["error"] == "reason_required"
    assert _admission(p["aid"])["status"] == "active"
    resp = client.post(url, json={"discharge_type": "dama", "confirm_mrn": p["mrn"],
                                  "reason": "Family decision"}, headers=_hdr(doctor))
    assert resp.status_code == 200, resp.text
    assert resp.json()["end_of_stay"]["end_type"] == "dama"
    assert _dispositions(p["pid"])[0]["disposition"] == "dama"


def test_discharge_patient_itself_rechecks_the_read_back_inside_its_transaction(adt_on):
    p = _patient()
    with pytest.raises(adt_core.AdtConflict) as exc:
        discharge_service.discharge_patient(p["pid"], "D001", "Summary", [], discharge_type="dama",
                                            confirm_mrn="WRONG", reason="x")
    assert exc.value.code == "read_back_mismatch"
    assert _admission(p["aid"])["status"] == "active"
    assert _all("SELECT id FROM discharge_summaries WHERE patient_id = ?", (p["pid"],)) == []
    with pytest.raises(adt_core.AdtConflict):
        discharge_service.discharge_patient(p["pid"], "D001", "Summary", [], discharge_type="death")


# ═════════════════════════════════════════════════════════════════════════════
#  6. The roster close
# ═════════════════════════════════════════════════════════════════════════════

_ROSTER_DEPT = DEPT
_HEADER = "MRN,Name,Age,Sex,Ward,Bed,Diagnosis,Department,Status"
_MAPPING = {"mrn": "MRN", "name": "Name", "age": "Age", "sex": "Sex", "ward": "Ward",
            "bed": "Bed", "diagnosis": "Diagnosis", "department": "Department", "status": "Status"}


def _roster(mrn: str, status: str) -> bytes:
    return (f"{_HEADER}\n{mrn},Asha Rao,44,F,ES-RW,A-1,Sepsis,{_ROSTER_DEPT},{status}\n").encode()


def _roster_admit(admin: dict) -> tuple[str, str, str]:
    mrn = f"ESR{uuid.uuid4().hex[:6].upper()}"
    res = roster_import.apply_import(_roster(mrn, "Admitted"), _MAPPING, actor_user_id=admin["id"])
    pid = res["created_patient_ids"][0]
    aid = _one("SELECT id FROM admissions WHERE patient_id = ? AND status = 'active'", (pid,))["id"]
    with db_session() as db:
        # The roster stamps the admission at IST midnight today; move it an
        # hour into the past so an end 'now' is never before it.
        db.execute("UPDATE admissions SET created_at = ? WHERE id = ?",
                   (_iso(datetime.now(timezone.utc) - timedelta(hours=1)), aid))
    return mrn, pid, aid


def test_a_roster_transfer_close_goes_through_end_stay_and_keeps_transferred(adt_on):
    admin = _user("admin", department=None)
    mrn, pid, aid = _roster_admit(admin)
    res = roster_import.apply_import(_roster(mrn, "Transferred"), _MAPPING, actor_user_id=admin["id"])
    assert res["admissions"]["closed"] == 1 and res["admissions"]["close_refused"] == 0
    adm = _admission(aid)
    assert adm["status"] == "transferred" and adm["discharge_type"] == "transferred_out"
    move = _one("SELECT movement_type, end_type, detail_json FROM patient_movements WHERE admission_id = ?",
                (aid,))
    assert move["movement_type"] == "discharge" and move["end_type"] == "transferred_out"
    assert json.loads(move["detail_json"])["door"] == "roster_import"
    assert _dispositions(pid)[0]["disposition"] == "transferred_out"


def test_a_roster_discharged_close_records_no_guessed_type(adt_on):
    admin = _user("admin", department=None)
    mrn, pid, aid = _roster_admit(admin)
    res = roster_import.apply_import(_roster(mrn, "Discharged"), _MAPPING, actor_user_id=admin["id"])
    assert res["admissions"]["closed"] == 1
    adm = _admission(aid)
    assert adm["status"] == "discharged" and adm["discharge_type"] is None
    assert adm["discharged_at"] is not None
    move = _one("SELECT end_type, detail_json FROM patient_movements WHERE admission_id = ?", (aid,))
    assert move["end_type"] is None and json.loads(move["detail_json"])["end_type_not_stated"] is True
    assert _dispositions(pid) == []


def test_a_roster_close_with_adt_off_is_unchanged(monkeypatch):
    monkeypatch.setattr(config, "ADT_ENABLED", False)
    admin = _user("admin", department=None)
    mrn, pid, aid = _roster_admit(admin)
    res = roster_import.apply_import(_roster(mrn, "Transferred"), _MAPPING, actor_user_id=admin["id"])
    assert res["admissions"] == {"created": 0, "updated": 0, "closed": 1}
    adm = _admission(aid)
    assert adm["status"] == "transferred" and adm["discharge_type"] is None
    assert _all("SELECT id FROM patient_movements WHERE admission_id = ?", (aid,)) == []


# ═════════════════════════════════════════════════════════════════════════════
#  7. discharge_preconditions: every door asks it, and it gates nothing clinical
# ═════════════════════════════════════════════════════════════════════════════

def test_discharge_preconditions_returns_an_empty_list_for_every_type():
    with db_session() as db:
        for end_type in (*adt_end.END_TYPES, None):
            assert adt_end.discharge_preconditions(db, "ANY", end_type) == []


def _functions(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node


def _calls(node) -> set[str]:
    names = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            fn = sub.func
            if isinstance(fn, ast.Name):
                names.add(fn.id)
            elif isinstance(fn, ast.Attribute):
                names.add(fn.attr)
    return names


def test_every_end_of_stay_door_goes_through_discharge_preconditions():
    """AST pin. Any function in server/ (tests excluded) that closes a stay
    (close_admission_on) must ask discharge_preconditions in the same body,
    and every caller of end_stay_on reaches it because end_stay_on asks it."""
    closers, enders = [], []
    for path in sorted(SERVER.rglob("*.py")):
        rel = path.relative_to(SERVER)
        if {"tests", ".venv", "venv", "__pycache__"} & set(rel.parts):
            continue
        for fn in _functions(path):
            calls = _calls(fn)
            if "close_admission_on" in calls and fn.name != "close_admission_on":
                closers.append((str(rel), fn.name, "discharge_preconditions" in calls))
            if "end_stay_on" in calls:
                enders.append((str(rel), fn.name))
    names = {(r, n) for r, n, _ in closers}
    assert ("services/discharge_service.py", "discharge_patient") in names
    assert ("services/adt/end_stay.py", "end_stay_on") in names
    missing = [(r, n) for r, n, asks in closers if not asks]
    assert not missing, f"these close a stay without asking discharge_preconditions: {missing}"
    assert ("services/roster_import.py", "apply_import") in enders
    assert ("services/adt/end_stay.py", "end_stay") in enders


def test_discharge_preconditions_reads_no_news2_and_no_bundle():
    fn = next(f for f in _functions(SERVER / "services" / "adt" / "end_stay.py")
              if f.name == "discharge_preconditions")
    body = [n for n in fn.body if not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant))]
    words = {n.id.lower() for n in ast.walk(ast.Module(body=body, type_ignores=[]))
             if isinstance(n, ast.Name)}
    words |= {n.attr.lower() for n in ast.walk(ast.Module(body=body, type_ignores=[]))
              if isinstance(n, ast.Attribute)}
    strings = " ".join(n.value.lower() for n in ast.walk(ast.Module(body=body, type_ignores=[]))
                       if isinstance(n, ast.Constant) and isinstance(n.value, str))
    for word in ("news2", "bundle", "care_bundles", "vitals", "risk"):
        assert not any(word in w for w in words) and word not in strings, word
    assert len(body) == 1 and isinstance(body[0], ast.Return)
    assert isinstance(body[0].value, ast.List) and body[0].value.elts == []


def test_a_precondition_refusal_stops_every_door(adt_on, monkeypatch, doctor, drafted):
    problem = [{"code": "r009", "message": "test precondition"}]
    monkeypatch.setattr(adt_end, "discharge_preconditions", lambda db, pid, t: problem)
    # The end route.
    p = _patient()
    resp = _end(p["aid"], doctor, confirm_mrn=p["mrn"])
    assert resp.status_code == 409 and resp.json()["detail"]["error"] == "discharge_preconditions"
    assert resp.json()["detail"]["preconditions"] == problem
    # The discharge route.
    resp = client.post(f"/api/vigil/patients/{p['pid']}/discharge", headers=_hdr(doctor))
    assert resp.status_code == 409 and resp.json()["detail"]["error"] == "discharge_preconditions"
    assert _admission(p["aid"])["status"] == "active"
    # The roster close.
    monkeypatch.setattr(adt_end, "discharge_preconditions", lambda db, pid, t: [])
    admin = _user("admin", department=None)
    mrn, pid, aid = _roster_admit(admin)
    monkeypatch.setattr(adt_end, "discharge_preconditions", lambda db, pid, t: problem)
    res = roster_import.apply_import(_roster(mrn, "Transferred"), _MAPPING, actor_user_id=admin["id"])
    assert res["admissions"]["close_refused"] == 1 and res["admissions"]["closed"] == 0
    assert _admission(aid)["status"] == "active"


# ═════════════════════════════════════════════════════════════════════════════
#  8. NABH inpatient days per stay
# ═════════════════════════════════════════════════════════════════════════════

def _two_stays(ward: str) -> str:
    """One patient, two stays on `ward` inside the last week: the first
    ended (an ADT end with a type), the second still active."""
    pid = f"PNB{uuid.uuid4().hex[:8].upper()}"
    now = datetime.now(timezone.utc)
    s1, e1, s2 = now - timedelta(days=6), now - timedelta(days=4), now - timedelta(days=1)
    with db_session() as db:
        db.execute("INSERT INTO patients (id, name, mrn, ward, status, admission_date) "
                   "VALUES (?, 'Two Stays', ?, ?, 'admitted', ?)", (pid, f"MRN-{pid}", ward, _iso(s2)))
        db.execute("INSERT INTO admissions (id, patient_id, admitted_by, department, ward, status, "
                   "created_at, admitted_at, discharged_at, discharge_type) VALUES "
                   "(?, ?, 'D001', ?, ?, 'discharged', ?, ?, ?, 'routine')",
                   (f"ADM-NB1{pid}", pid, DEPT, ward, _iso(s1), _iso(s1), _iso(e1)))
        db.execute("INSERT INTO admissions (id, patient_id, admitted_by, department, ward, status, "
                   "created_at, admitted_at) VALUES (?, ?, 'D001', ?, ?, 'active', ?, ?)",
                   (f"ADM-NB2{pid}", pid, DEPT, ward, _iso(s2), _iso(s2)))
    return pid


def test_inpatient_days_count_per_stay_only_with_adt_on(monkeypatch):
    ward = f"NB-{_tag()}"
    _two_stays(ward)
    start = datetime.now(timezone.utc) - timedelta(days=7)
    end = datetime.now(timezone.utc) + timedelta(days=1)
    monkeypatch.setattr(config, "ADT_ENABLED", False)
    with db_session() as db:
        off = nabh_service._inpatient_days(db, start, end, ward)
    assert "stays" not in off and 0.9 <= off["inpatient_days"] <= 1.1   # current stay only
    monkeypatch.setattr(config, "ADT_ENABLED", True)
    with db_session() as db:
        on = nabh_service._inpatient_days(db, start, end, ward)
    assert on["stays"] == 2 and on["patients"] == 1
    assert 2.9 <= on["inpatient_days"] <= 3.1   # 2 days ended + 1 day open
    assert on["method"].startswith("Counted per stay")
