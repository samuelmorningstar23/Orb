"""When a stay began and when it ended: services/stay_times.py and every
summary-keyed reader that now asks it (adt-8c, W4-B, 4 October 2026).

What this file defends:

  1. THE HELPERS. stay_start prefers the recorded arrival (admitted_at) over
     created_at; stay_end prefers admissions.discharged_at, else the first
     summary inside the stay (never a summary written after the patient's
     next admission), and is None while the stay is active. The SQL
     fragments agree with the Python.
  2. SUMMARY-LESS ENDS. An end of stay the beds and admissions module
     recorded with no summary (a death) is found; one the doctor's discharge
     route recorded with its summary is not (it is counted once, as a
     summary); an imported encounter's discharged_at is not (with ORB_ADT off
     nothing changes); a reopened end is not.
  3. FORECAST. A death with no summary counts in 'discharges today'; a
     summary-backed ADT discharge counts once, not twice; a back-timed
     admitted_at moves 'admissions today' to the arrival day.
  4. IMPACT. A death with no summary counts in throughput.
  5. NABH. A summary-less LAMA is a discharge the readmission indicator
     follows; a death is excluded from its denominator by type; both count
     as discharge events for disposition coverage.
  6. HOUSEKEEPING. A readmission inside the lookback raises no turnaround
     task on the new bed and does not flip it to vacant_dirty; the task goes
     to the bed the ended stay was in. A summary-less ADT end raises exactly
     one task, keyed on its movement, on its master bed.
  7. THE /discharged LIST AND THE PORTAL. A summary-less end shows its end
     time on the list; the portal says the stay ended with no summary rather
     than 'not available yet'.
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
from services import discharge_service, forecast_service, housekeeping_service as hk
from services import nabh_service, stay_times
from services.adt import end_stay as adt_end
from services.forecast_service import predict_hospital_flow
from services.impact_service import compute_impact

client = TestClient(main.app)
DEPT = "General Medicine"   # conftest seeds D001, D002 and N001 in it


# ── helpers ──────────────────────────────────────────────────────────────────

def _tag() -> str:
    return uuid.uuid4().hex[:6].upper()


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _user(role: str, department: str | None = DEPT) -> dict:
    uid = f"ST{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO users (id, name, role, department, password_hash, is_active) "
            "VALUES (?, ?, ?, ?, ?, 1)",
            (uid, f"Stay times {role}", role, department, hash_password("password")),
        )
    return {"id": uid, "role": role, "department": department}


def _hdr(user: dict) -> dict:
    return {"Authorization": f"Bearer {create_token(user['id'], user['role'])}"}


def _patient(*, start: datetime | None = None, ward: str | None = None, bed: str | None = None,
             status: str = "admitted", admission_status: str = "active",
             portal: bool = False) -> dict:
    pid = f"PST{uuid.uuid4().hex[:8].upper()}"
    aid = f"ADM-ST{uuid.uuid4().hex[:8].upper()}"
    start = start or (datetime.now(timezone.utc) - timedelta(hours=30))
    user_id = None
    with db_session() as db:
        if portal:
            user_id = f"PT{uuid.uuid4().hex[:8].upper()}"
            db.execute("INSERT INTO users (id, name, role, password_hash, is_active) "
                       "VALUES (?, 'Portal', 'patient', 'x', 1)", (user_id,))
        db.execute(
            "INSERT INTO patients (id, user_id, name, mrn, ward, bed, status, admission_date) "
            "VALUES (?, ?, 'Stay Times Test', ?, ?, ?, ?, ?)",
            (pid, user_id, f"MRN-{pid}", ward, bed, status, _iso(start)),
        )
        db.execute(
            "INSERT INTO admissions (id, patient_id, admitted_by, department, ward, bed, status, "
            "created_at) VALUES (?, ?, 'D001', ?, ?, ?, ?, ?)",
            (aid, pid, DEPT, ward, bed, admission_status, _iso(start)),
        )
    return {"pid": pid, "aid": aid, "mrn": f"MRN-{pid}", "start": start, "user_id": user_id}


def _admission(aid: str) -> dict:
    with db_session() as db:
        return dict(db.execute("SELECT * FROM admissions WHERE id = ?", (aid,)).fetchone())


def _summary(pid: str, when: datetime | str) -> int:
    stamp = when if isinstance(when, str) else when.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    with db_session() as db:
        db.execute(
            "INSERT INTO discharge_summaries (patient_id, author_id, summary, discharged_at) "
            "VALUES (?, 'D001', 'Stay times summary', ?)", (pid, stamp))
        return int(db.execute("SELECT last_insert_rowid()").fetchone()[0])


def _end(aid: str, end_type: str = "death", **kw) -> dict:
    adm = _admission(aid)
    with db_session() as db:
        mrn = db.execute("SELECT mrn FROM patients WHERE id = ?", (adm["patient_id"],)).fetchone()[0]
    return adt_end.end_stay(aid, end_type=end_type, actor={"id": "D001", "role": "doctor"},
                            confirm_mrn=mrn, **kw)


@pytest.fixture()
def adt_on(monkeypatch):
    monkeypatch.setattr(config, "ADT_ENABLED", True)
    yield


def _today() -> dict:
    forecast_service._hospital_flow_cache.clear()
    day0 = predict_hospital_flow(days_ahead=1, include_current_day=True)["daily_forecasts"][0]
    return {"admissions": day0["actual_admissions"], "discharges": day0["actual_discharges"]}


def _throughput() -> int:
    metrics = {m["key"]: m for m in compute_impact()["metrics"]}
    return metrics["throughput"]["evidence_count"]


# ═════════════════════════════════════════════════════════════════════════════
#  1. The helpers
# ═════════════════════════════════════════════════════════════════════════════

def test_stay_start_prefers_the_recorded_arrival():
    created = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)
    arrived = datetime(2026, 9, 30, 22, 0, tzinfo=timezone.utc)
    assert stay_times.stay_start({"created_at": _iso(created)}) == created
    assert stay_times.stay_start({"created_at": _iso(created), "admitted_at": _iso(arrived)}) == arrived
    assert stay_times.stay_start_stored({"created_at": "x", "admitted_at": None}) == "x"
    assert stay_times.stay_start_sql("a") == "COALESCE(a.admitted_at, a.created_at)"


def test_stay_end_is_none_while_active_and_prefers_discharged_at():
    p = _patient()
    with db_session() as db:
        assert stay_times.stay_end(db, _admission(p["aid"])) is None
        db.execute("UPDATE admissions SET status = 'discharged', discharged_at = ? WHERE id = ?",
                   ("2026-10-02T08:00:00.000000+00:00", p["aid"]))
    _summary(p["pid"], datetime(2026, 10, 3, 8, 0, tzinfo=timezone.utc))
    with db_session() as db:
        assert stay_times.stay_end(db, _admission(p["aid"])) == datetime(2026, 10, 2, 8, 0, tzinfo=timezone.utc)


def test_stay_end_takes_the_first_summary_inside_the_stay_and_never_a_later_stays():
    start = datetime.now(timezone.utc) - timedelta(days=5)
    p = _patient(start=start, status="discharged", admission_status="discharged")
    first = start + timedelta(days=1)
    _summary(p["pid"], first)
    # A readmission two days in, with its own summary after it.
    second_aid = f"ADM-ST{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute("INSERT INTO admissions (id, patient_id, admitted_by, department, status, created_at) "
                   "VALUES (?, ?, 'D001', ?, 'discharged', ?)",
                   (second_aid, p["pid"], DEPT, _iso(start + timedelta(days=2))))
    _summary(p["pid"], start + timedelta(days=3))
    with db_session() as db:
        assert stay_times.stay_end(db, _admission(p["aid"])) == first.replace(microsecond=0)
        assert stay_times.stay_end(db, _admission(second_aid)) == (start + timedelta(days=3)).replace(microsecond=0)
        # The SQL fragment says the same.
        rows = {r["id"]: r["e"] for r in db.execute(
            f"SELECT a.id, {stay_times.stay_end_sql('a')} AS e FROM admissions a WHERE a.patient_id = ?",
            (p["pid"],)).fetchall()}
    assert stay_times._parse(rows[p["aid"]]) == first.replace(microsecond=0)
    assert stay_times._parse(rows[second_aid]) == (start + timedelta(days=3)).replace(microsecond=0)


def test_admission_for_summary_finds_the_stay_the_summary_ended():
    start = datetime.now(timezone.utc) - timedelta(days=4)
    p = _patient(start=start, status="discharged", admission_status="discharged")
    sid = _summary(p["pid"], start + timedelta(days=1))
    later = f"ADM-ST{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute("INSERT INTO admissions (id, patient_id, admitted_by, department, status, created_at) "
                   "VALUES (?, ?, 'D001', ?, 'active', ?)",
                   (later, p["pid"], DEPT, _iso(start + timedelta(days=2))))
        row = db.execute("SELECT * FROM discharge_summaries WHERE id = ?", (sid,)).fetchone()
        found = stay_times.admission_for_summary(db, dict(row))
    assert found is not None and found["id"] == p["aid"]


# ═════════════════════════════════════════════════════════════════════════════
#  2. Summary-less ends
# ═════════════════════════════════════════════════════════════════════════════

def test_a_death_with_no_summary_is_a_summary_less_end(adt_on):
    p = _patient()
    result = _end(p["aid"])
    with db_session() as db:
        ends = stay_times.summary_less_ends(db, patient_id=p["pid"])
    assert [(e["admission_id"], e["end_type"], e["movement_id"]) for e in ends] == [
        (p["aid"], "death", result["movement_id"])]


def test_an_imported_discharged_at_is_not_a_summary_less_end():
    """With ORB_ADT off nothing changes: an imported encounter carries
    discharged_at but no discharge movement, so no reader counts it anew."""
    p = _patient(status="discharged", admission_status="discharged")
    with db_session() as db:
        db.execute("UPDATE admissions SET discharged_at = ? WHERE id = ?",
                   (_iso(datetime.now(timezone.utc)), p["aid"]))
        assert stay_times.summary_less_ends(db, patient_id=p["pid"]) == []


def test_a_summary_backed_adt_discharge_is_not_a_summary_less_end(adt_on, monkeypatch):
    async def _draft(_pid):
        return "Drafted summary."

    monkeypatch.setattr(discharge_service, "generate_discharge_summary", _draft)
    p = _patient()
    out = discharge_service.discharge_patient(p["pid"], "D001", "Summary text", [])
    assert out["status"] == "success"
    with db_session() as db:
        assert stay_times.summary_less_ends(db, patient_id=p["pid"]) == []
        moved = db.execute("SELECT discharge_summary_id FROM patient_movements WHERE admission_id = ? "
                           "AND movement_type = 'discharge'", (p["aid"],)).fetchone()
    assert moved["discharge_summary_id"] == out["summary_id"]


# ═════════════════════════════════════════════════════════════════════════════
#  3. Forecast
# ═════════════════════════════════════════════════════════════════════════════

def test_a_death_with_no_summary_counts_in_discharges_today(adt_on):
    p = _patient()
    before = _today()
    _end(p["aid"])
    after = _today()
    assert after["discharges"] == before["discharges"] + 1


def test_a_summary_backed_adt_discharge_counts_once_not_twice(adt_on, monkeypatch):
    async def _draft(_pid):
        return "Drafted summary."

    monkeypatch.setattr(discharge_service, "generate_discharge_summary", _draft)
    p = _patient()
    before = _today()
    discharge_service.discharge_patient(p["pid"], "D001", "Summary text", [])
    assert _today()["discharges"] == before["discharges"] + 1


def test_a_back_timed_arrival_moves_admissions_today_to_the_arrival_day():
    before = _today()
    p = _patient(start=datetime.now(timezone.utc))
    assert _today()["admissions"] == before["admissions"] + 1
    with db_session() as db:
        db.execute("UPDATE admissions SET admitted_at = ? WHERE id = ?",
                   (_iso(datetime.now(timezone.utc) - timedelta(days=3)), p["aid"]))
    assert _today()["admissions"] == before["admissions"]


# ═════════════════════════════════════════════════════════════════════════════
#  4. Impact
# ═════════════════════════════════════════════════════════════════════════════

def test_a_death_with_no_summary_counts_in_impact_throughput(adt_on):
    p = _patient()
    before = _throughput()
    _end(p["aid"])
    assert _throughput() == before + 1


# ═════════════════════════════════════════════════════════════════════════════
#  5. NABH
# ═════════════════════════════════════════════════════════════════════════════

def test_readmission_follows_a_summary_less_lama_and_excludes_a_death_by_type(adt_on, monkeypatch):
    # Ends 10 days ago, so the 7-day follow-up has elapsed (not censored).
    monkeypatch.setattr(adt_end, "_end_time", lambda occurred_at, stay_start, *, enforce_backtime:
                        datetime.now(timezone.utc) - timedelta(days=10))
    window_start = datetime.now(timezone.utc) - timedelta(days=12)
    window_end = datetime.now(timezone.utc) + timedelta(days=1)
    with db_session() as db:
        before = nabh_service._readmission(db, window_start, window_end, None)
        base_before = nabh_service._disposition_base(db, window_start, window_end, None)
    left = _patient(start=datetime.now(timezone.utc) - timedelta(days=11))
    died = _patient(start=datetime.now(timezone.utc) - timedelta(days=11))
    _end(left["aid"], "lama", reason="Wanted to go home")
    _end(died["aid"], "death")
    with db_session() as db:
        after = nabh_service._readmission(db, window_start, window_end, None)
        base_after = nabh_service._disposition_base(db, window_start, window_end, None)
    d_before, d_after = before["detail"], after["detail"]
    assert d_after["discharges_in_period"] == d_before["discharges_in_period"] + 1
    assert d_after["eligible_discharges"] == d_before["eligible_discharges"] + 1
    assert d_after.get("deaths_excluded", 0) == d_before.get("deaths_excluded", 0) + 1
    # Both are discharge events for the disposition coverage.
    assert base_after["discharge_events_observed"] == base_before["discharge_events_observed"] + 2


# ═════════════════════════════════════════════════════════════════════════════
#  6. Housekeeping
# ═════════════════════════════════════════════════════════════════════════════

def _unit(ward: str, bed: str, state: str = "occupied") -> dict:
    return hk.register_unit(unit_code=f"ST-{_tag()}", ward=ward, bed_label=bed,
                            initial_state=state, actor_user_id="STFIXTURE")


def _tasks(unit_id: str) -> list[dict]:
    with db_session() as db:
        return [dict(r) for r in db.execute(
            "SELECT * FROM housekeeping_tasks WHERE unit_id = ?", (unit_id,)).fetchall()]


def _state(unit_id: str) -> str:
    with db_session() as db:
        return db.execute("SELECT state FROM housekeeping_units WHERE id = ?", (unit_id,)).fetchone()[0]


def test_a_readmission_inside_the_lookback_never_cleans_the_new_bed():
    ward = f"ST Readmit {_tag()}"
    old_bed, new_bed = _unit(ward, "OLD-1"), _unit(ward, "NEW-2")
    start = datetime.now(timezone.utc) - timedelta(hours=10)
    p = _patient(start=start, ward=ward, bed="OLD-1", status="discharged",
                 admission_status="discharged")
    sid = _summary(p["pid"], start + timedelta(hours=2))
    # Readmitted four hours later into another bed: the patient's own labels
    # now name the new bed.
    with db_session() as db:
        db.execute("INSERT INTO admissions (id, patient_id, admitted_by, department, ward, bed, "
                   "status, created_at) VALUES (?, ?, 'D001', ?, ?, 'NEW-2', 'active', ?)",
                   (f"ADM-ST{_tag()}", p["pid"], DEPT, ward, _iso(start + timedelta(hours=6))))
        db.execute("UPDATE patients SET status = 'admitted', bed = 'NEW-2' WHERE id = ?", (p["pid"],))
    hk.sync_discharge_turnarounds(lookback_hours=24)
    assert _tasks(new_bed["id"]) == []
    assert _state(new_bed["id"]) == "occupied"
    old_tasks = _tasks(old_bed["id"])
    assert [t["source_ref"] for t in old_tasks] == [str(sid)]
    assert _state(old_bed["id"]) == "vacant_dirty"


def test_a_summary_backed_discharge_sweeps_exactly_as_before():
    ward = f"ST Same {_tag()}"
    unit = _unit(ward, "S-1")
    start = datetime.now(timezone.utc) - timedelta(hours=3)
    p = _patient(start=start, ward=ward, bed="S-1", status="discharged",
                 admission_status="discharged")
    sid = _summary(p["pid"], start + timedelta(hours=1))
    for _ in range(3):
        hk.sync_discharge_turnarounds(lookback_hours=24)
    tasks = _tasks(unit["id"])
    assert [t["source_ref"] for t in tasks] == [str(sid)]
    assert _state(unit["id"]) == "vacant_dirty"


def test_an_ended_stay_with_no_bed_on_its_row_still_reads_the_patient_labels():
    """Older rows carry the bed on the patient only. While that stay is the
    patient's latest, the sweep reads the patient's labels exactly as it
    always did."""
    ward = f"ST Legacy {_tag()}"
    unit = _unit(ward, "L-1")
    start = datetime.now(timezone.utc) - timedelta(hours=3)
    p = _patient(start=start, status="discharged", admission_status="discharged")
    with db_session() as db:
        db.execute("UPDATE patients SET ward = ?, bed = 'L-1' WHERE id = ?", (ward, p["pid"]))
    sid = _summary(p["pid"], start + timedelta(hours=1))
    hk.sync_discharge_turnarounds(lookback_hours=24)
    assert [t["source_ref"] for t in _tasks(unit["id"])] == [str(sid)]


def _master_bed(code: str | None = None) -> tuple[dict, dict]:
    admin = {"id": "system", "role": "admin"}
    ward = bm.save_ward({"code": code or f"ST-{_tag()}", "department": DEPT}, actor=admin)
    bm.register_beds({"ward_id": ward["id"], "labels": ["1"]}, actor=admin)
    with db_session() as db:
        bed = dict(db.execute("SELECT * FROM housekeeping_units WHERE ward_id = ?",
                              (ward["id"],)).fetchone())
    return ward, bed


def test_a_summary_less_adt_end_raises_one_task_on_its_master_bed(adt_on):
    from services.adt import core as adt_core

    ward, bed = _master_bed()
    p = _patient(ward=ward["code"], bed="1")
    with db_session(immediate=True) as db:
        adt_core.locate_admission_on(db, admission_id=p["aid"], actor={"id": "system", "role": "admin"},
                                     movement_type="location_linked", reason="test",
                                     ward_id=ward["id"], bed_id=bed["id"])
        db.execute("UPDATE housekeeping_units SET state = 'occupied' WHERE id = ?", (bed["id"],))
    ended = _end(p["aid"])
    for _ in range(3):
        hk.sync_discharge_turnarounds(lookback_hours=24)
    tasks = _tasks(bed["id"])
    assert [t["source_ref"] for t in tasks] == [ended["movement_id"]]
    assert _state(bed["id"]) == "vacant_dirty"


def test_a_reopened_end_raises_no_task(adt_on):
    p = _patient(ward=f"ST Reopen {_tag()}", bed="R-1")
    unit = _unit(_admission(p["aid"])["ward"], "R-1")
    _end(p["aid"])
    adt_end.reopen_stay(p["aid"], actor={"id": "D001", "role": "doctor"}, confirm_mrn=p["mrn"],
                        reason="Recorded on the wrong patient")
    hk.sync_discharge_turnarounds(lookback_hours=24)
    assert _tasks(unit["id"]) == []
    assert _state(unit["id"]) == "occupied"


# ═════════════════════════════════════════════════════════════════════════════
#  7. The /discharged list and the patient portal
# ═════════════════════════════════════════════════════════════════════════════

def test_the_discharged_list_shows_the_end_time_of_a_summary_less_end(adt_on):
    p = _patient()
    ended = _end(p["aid"], "lama", reason="Left")
    admin = _user("admin", None)
    resp = client.get("/api/vigil/discharged", params={"limit": 500}, headers=_hdr(admin))
    assert resp.status_code == 200
    row = next(r for r in resp.json() if r["id"] == p["pid"])
    assert stay_times._parse(row["discharged_at"]) == stay_times._parse(ended["discharged_at"]).replace(microsecond=0)


def test_the_portal_says_a_summary_less_stay_ended_rather_than_not_yet(adt_on, monkeypatch):
    """The view's own answer. Ruling R029 (services/patient_portal_access)
    decides whether the portal is open at all after a stay ends, and today it
    reads the discharge summary's time, so it closes at once (fail-closed)
    after an end with no summary. That rule is not this file's to change:
    the check is held open here so the view itself is what is tested."""
    from services import patient_portal_access

    monkeypatch.setattr(patient_portal_access, "portal_access",
                        lambda db, user_id: {"open": True, "status": None, "until": None,
                                             "reason": None})
    p = _patient(portal=True)
    resp = client.get("/api/bridge/discharge", headers=_hdr({"id": p["user_id"], "role": "patient"}))
    assert resp.status_code == 200
    assert resp.json()["message"] == "No discharge summary available yet."
    _end(p["aid"], "lama", reason="Left")
    after = client.get("/api/bridge/discharge", headers=_hdr({"id": p["user_id"], "role": "patient"}))
    assert after.status_code == 200, after.text
    body = after.json()
    assert body["discharge_summary"] is None
    assert "has ended" in body["message"] and "yet" not in body["message"]
    assert body["discharged_at"]
    # The end type is never told to the patient.
    assert "lama" not in str(body).lower() and "against" not in str(body).lower()
