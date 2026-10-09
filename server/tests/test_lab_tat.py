"""Laboratory turnaround times (lis-18) and the optional NABH laboratory
indicators.

Pinned here:
  * Intervals come from the laboratory's own records: the order's activation,
    collection, receipt and the FIRST release; an amendment later does not move
    them, and an interval whose end is before its start is left out and counted.
  * Days are hospital-local: a test reported at 23:50 IST and one at 00:10 IST
    the next day fall on two days though they share a UTC date.
  * A test with no target is 'no target', never 0 percent; reported exactly on
    its target is within it.
  * The live overdue list (STAT first), the open exceptions count and the time
    to critical acknowledgement, all inside the caller's department.
  * Simulated results are left out of the figures and counted.
  * The read and the CSV export are audited (phi_access, fail-closed); the
    export carries no patient name or UHID and no cell a spreadsheet would run.
  * With the laboratory off the routes are 404 module_disabled.
  * NABH: with the laboratory off the nine indicators are unchanged; with it on
    two laboratory indicators appear, honest about missing targets and data.
"""
from __future__ import annotations

import csv
import io
import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import config
import main
from auth import create_token, hash_password
from database import db_session
from routes.lab import tat as tat_routes
from services import hospital_time, lab_module
from services import lab_master_service as lm
from services import lab_tat_service as tat
from services import nabh_service
from services import orders_service, terminology_service
from services.audit_service import AuditWriteError
from services.clinical_time import format_utc, utc_iso_now

client = TestClient(main.app)

IST = timezone(timedelta(hours=5, minutes=30))
SUFFIX = uuid.uuid4().hex[:5].upper()
DEPT = f"TAT Ward {SUFFIX}"
WARD_LABEL = f"TAT-{SUFFIX}"


def _code(name):
    return f"W7T{SUFFIX}-{name}"


TESTS = {
    "TGT": {"name": "Turnaround glucose", "short_name": "TAT Glu", "decimal_places": 1,
            "unit": "mmol/L", "result_type": "numeric", "tat_routine_min": 60,
            "tat_stat_min": 30},
    "NOT": {"name": "Turnaround albumin", "short_name": "TAT Alb", "decimal_places": 1,
            "unit": "g/dL", "result_type": "numeric"},
}


def _user(role, dept=None):
    uid = f"TAT{uuid.uuid4().hex[:6].upper()}"
    with db_session() as db:
        db.execute("INSERT INTO users (id, name, role, department, password_hash) "
                   "VALUES (?, ?, ?, ?, ?)",
                   (uid, f"{role.title()} {uid}", role, dept, hash_password("password")))
    return uid, {"Authorization": f"Bearer {create_token(uid, role)}"}


@pytest.fixture(scope="module")
def master():
    uid, _ = _user("pathologist")
    return {"id": uid, "role": "pathologist"}


@pytest.fixture(scope="module", autouse=True)
def _catalogue(master):
    terminology_service.seed_terminology()
    lm.load_starter_catalogue()
    for key, spec in TESTS.items():
        lm.create_test({"code_system": "LOCAL", "code": _code(key), "kind": "analyte",
                        "section_id": "biochemistry", "specimen_type_code": "serum", **spec},
                       actor=master)
    created: list[str] = []
    yield created
    test_ids = [lm.test_id_for("LOCAL", _code(k)) for k in TESTS]
    tmarks = ", ".join("?" for _ in test_ids)
    with db_session() as db:
        if created:
            marks = ", ".join("?" for _ in created)
            ids = tuple(created)
            db.execute(f"DELETE FROM lab_exceptions WHERE order_id IN "
                       f"(SELECT id FROM orders WHERE patient_id IN ({marks}))", ids)
            db.execute(f"DELETE FROM lab_critical_events WHERE patient_id IN ({marks})", ids)
            db.execute(f"DELETE FROM observations WHERE patient_id IN ({marks})", ids)
            db.execute(f"DELETE FROM lab_result_events WHERE patient_id IN ({marks})", ids)
            db.execute(f"DELETE FROM lab_results WHERE patient_id IN ({marks})", ids)
            db.execute(f"DELETE FROM lab_order_items WHERE patient_id IN ({marks})", ids)
            db.execute(f"DELETE FROM lab_specimen_events WHERE patient_id IN ({marks})", ids)
            db.execute(f"DELETE FROM lab_specimens WHERE patient_id IN ({marks})", ids)
            db.execute(f"UPDATE patients SET status = 'discharged' WHERE id IN ({marks})", ids)
            db.execute(f"UPDATE admissions SET status = 'discharged' "
                       f"WHERE patient_id IN ({marks})", ids)
        db.execute(f"DELETE FROM lab_tests WHERE id IN ({tmarks})", tuple(test_ids))
        db.execute(f"DELETE FROM terminology WHERE code_system = 'LOCAL' AND code IN ({tmarks})",
                   tuple(_code(k) for k in TESTS))
    lab_module.refresh_drain_state()


@pytest.fixture
def lab_on(monkeypatch):
    monkeypatch.setattr(config, "LAB_ENABLED", True)
    monkeypatch.setattr(config, "LAB_REQUIRE_DISTINCT_AUTHORISER", True)
    with db_session() as db:
        event_id = db.execute(
            "INSERT INTO lab_module_events (event, occurred_at, detail) VALUES ('enabled', ?, ?)",
            (utc_iso_now(), "test cutover")).lastrowid
    yield
    with db_session() as db:
        db.execute("DELETE FROM lab_module_events WHERE id = ?", (event_id,))


@pytest.fixture
def ist(monkeypatch):
    monkeypatch.setattr(hospital_time, "hospital_timezone", lambda: IST)
    return IST


def _patient(created, dept=DEPT):
    pid = f"PTAT{uuid.uuid4().hex[:8].upper()}"
    name = f"Turnaround {uuid.uuid4().hex[:6]}"
    mrn = f"MRN-TAT-{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute("INSERT INTO patients (id, name, mrn, status, ward, bed, date_of_birth, sex) "
                   "VALUES (?, ?, ?, 'admitted', ?, ?, '1970-01-01', 'male')",
                   (pid, name, mrn, WARD_LABEL, f"B{uuid.uuid4().hex[:3]}"))
        db.execute("INSERT INTO admissions (id, patient_id, admitted_by, department, status) "
                   "VALUES (?, ?, 'D001', ?, 'active')",
                   (f"ATAT{uuid.uuid4().hex[:8].upper()}", pid, dept))
    created.append(pid)
    return {"id": pid, "name": name, "mrn": mrn}


@pytest.fixture(scope="module")
def tech():
    return _user("lab_technician")


@pytest.fixture(scope="module")
def phleb():
    return _user("phlebotomist")


@pytest.fixture(scope="module")
def signer():
    return _user("pathologist")


@pytest.fixture(scope="module")
def ward_admin():
    """An account with lab.read scoped to this file's department, so the
    figures below see only this file's patients in the shared database."""
    return _user("admin", dept=DEPT)


def _seen(result):
    return {"value_text": result["value_text"], "comment": result.get("comment"),
            "abnormal_flag": result.get("abnormal_flag")}


def _in_lab(p, tech, phleb, key="TGT", priority="routine"):
    order = orders_service.create_order(
        patient_id=p["id"], order_type="lab", description=key, code=_code(key),
        code_system="LOCAL", priority=priority, ordered_by="D001", status="active")
    with db_session() as db:
        item = dict(db.execute("SELECT id, specimen_id FROM lab_order_items WHERE order_id = ?",
                               (order["id"],)).fetchone())
    identity = {"uhid": p["mrn"], "name": p["name"]}
    sid = item["specimen_id"]
    assert client.post("/api/lab/collection/labels",
                       json={"specimen_ids": [sid], "identity": identity},
                       headers=phleb[1]).status_code == 200
    assert client.post(f"/api/lab/specimens/{sid}/collect", json={"identity": identity},
                       headers=phleb[1]).status_code == 200
    assert client.post(f"/api/lab/specimens/{sid}/receive", json={},
                       headers=tech[1]).status_code == 200
    return order, sid, item["id"]


def _released(p, tech, phleb, signer, *, key="TGT", priority="routine", ordered=None,
              collected=None, received=None, reported=None, value="5.0"):
    """A test taken through the real flow and released, then its instants set
    to the ones given (aware datetimes)."""
    order, sid, item_id = _in_lab(p, tech, phleb, key, priority)
    resp = client.post("/api/lab/results", json={"order_item_id": item_id, "value": value},
                       headers=tech[1])
    assert resp.status_code == 200, resp.text
    result = resp.json()["result"]
    resp = client.post("/api/lab/results/tech-validate",
                       json={"results": [{"result_id": result["id"], "expected": _seen(result)}]},
                       headers=tech[1])
    assert resp.status_code == 200, resp.text
    result = resp.json()["results"][0]
    resp = client.post("/api/lab/results/authorise",
                       json={"specimen_id": sid,
                             "results": [{"result_id": result["id"], "expected": _seen(result)}]},
                       headers=signer[1])
    assert resp.status_code == 200, resp.text
    with db_session() as db:
        if ordered is not None:
            db.execute("UPDATE order_events SET created_at = ? WHERE order_id = ? "
                       "AND new_status = 'active'", (format_utc(ordered), order["id"]))
        if collected is not None:
            db.execute("UPDATE lab_specimens SET collected_at = ? WHERE id = ?",
                       (format_utc(collected), sid))
        if received is not None:
            db.execute("UPDATE lab_specimens SET received_at = ? WHERE id = ?",
                       (format_utc(received), sid))
        if reported is not None:
            db.execute("UPDATE lab_results SET released_at = ? WHERE id = ?",
                       (format_utc(reported), result["id"]))
    return {"order": order, "specimen_id": sid, "item_id": item_id, "result_id": result["id"]}


def _board(hdr, first, last):
    resp = client.get("/api/lab/tat", params={"from": first, "to": last}, headers=hdr)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _group(board, key="TGT", priority="routine"):
    # This file's tests sit in biochemistry; only this file's patients are
    # visible to ward_admin, so the section's figures are this file's alone.
    return next(g for g in board["by_section_priority"]
                if g["section_id"] == "biochemistry" and g["priority"] == priority)


# ── 1. Intervals and days ───────────────────────────────────────────────────

def test_intervals_come_from_the_records(lab_on, ist, _catalogue, tech, phleb, signer,
                                         ward_admin):
    p = _patient(_catalogue)
    day = hospital_time.hospital_today() - timedelta(days=3)
    base = datetime(day.year, day.month, day.day, 9, 0, tzinfo=IST)
    made = _released(p, tech, phleb, signer, ordered=base, collected=base + timedelta(minutes=20),
                     received=base + timedelta(minutes=50), reported=base + timedelta(minutes=95))
    board = _board(ward_admin[1], day.isoformat(), day.isoformat())
    overall = board["overall"]
    assert overall["count"] == 1
    assert overall["intervals"]["order_to_collection"]["median"] == 20.0
    assert overall["intervals"]["collection_to_receipt"]["median"] == 30.0
    assert overall["intervals"]["receipt_to_report"]["median"] == 45.0
    assert overall["intervals"]["order_to_report"]["median"] == 95.0
    assert overall["target"] == {"with_target": 1, "within": 1, "no_target": 0, "unreadable": 0,
                                 "percent": 100.0, "state": "reported", "text": "100.0%"}
    # A later amendment does not move the turnaround time.
    with db_session() as db:
        old = dict(db.execute("SELECT * FROM lab_results WHERE id = ?",
                              (made["result_id"],)).fetchone())
        db.execute("UPDATE lab_results SET status = 'superseded' WHERE id = ?", (old["id"],))
        db.execute(
            "INSERT INTO lab_results (order_item_id, order_id, specimen_id, patient_id, test_id, "
            "version, value_text, entered_at, released_at, released_by, status, amend_reason) "
            "VALUES (?, ?, ?, ?, ?, 2, '5.5', ?, ?, ?, 'released', 'Recalculated')",
            (old["order_item_id"], old["order_id"], old["specimen_id"], old["patient_id"],
             old["test_id"], format_utc(base + timedelta(hours=5)),
             format_utc(base + timedelta(hours=5)), signer[0]))
    again = _board(ward_admin[1], day.isoformat(), day.isoformat())["overall"]
    assert again["count"] == 1
    assert again["intervals"]["receipt_to_report"]["median"] == 45.0


def test_days_split_at_midnight_hospital_time(lab_on, ist, _catalogue, tech, phleb, signer,
                                              ward_admin):
    first = hospital_time.hospital_today() - timedelta(days=5)
    second = first + timedelta(days=1)
    late = datetime(first.year, first.month, first.day, 23, 50, tzinfo=IST)
    early = datetime(second.year, second.month, second.day, 0, 10, tzinfo=IST)
    # The two instants share one UTC date.
    assert late.astimezone(timezone.utc).date() == early.astimezone(timezone.utc).date()
    for at in (late, early):
        _released(_patient(_catalogue), tech, phleb, signer, received=at - timedelta(minutes=30),
                  collected=at - timedelta(minutes=60), reported=at)
    board = _board(ward_admin[1], first.isoformat(), second.isoformat())
    days = {d["day"]: d["count"] for d in board["by_day"]}
    assert days == {first.isoformat(): 1, second.isoformat(): 1}
    assert board["period"]["days"] == 2


def test_an_out_of_order_interval_is_left_out_and_counted(lab_on, ist, _catalogue, tech, phleb,
                                                          signer, ward_admin):
    day = hospital_time.hospital_today() - timedelta(days=8)
    at = datetime(day.year, day.month, day.day, 12, 0, tzinfo=IST)
    # Received before it was collected (a clock fault on a back-dated tube).
    _released(_patient(_catalogue), tech, phleb, signer, collected=at,
              received=at - timedelta(minutes=10), reported=at + timedelta(minutes=40))
    overall = _board(ward_admin[1], day.isoformat(), day.isoformat())["overall"]
    assert overall["intervals"]["collection_to_receipt"]["n"] == 0
    assert overall["intervals"]["collection_to_receipt"]["median"] is None
    assert overall["intervals"]["receipt_to_report"]["median"] == 50.0
    assert overall["out_of_order"] == 1


# ── 2. Targets ──────────────────────────────────────────────────────────────

def test_a_test_without_a_target_is_no_target_never_zero(lab_on, ist, _catalogue, tech, phleb,
                                                         signer, ward_admin):
    day = hospital_time.hospital_today() - timedelta(days=11)
    at = datetime(day.year, day.month, day.day, 10, 0, tzinfo=IST)
    _released(_patient(_catalogue), tech, phleb, signer, key="NOT", received=at,
              collected=at - timedelta(minutes=5), reported=at + timedelta(hours=9))
    board = _board(ward_admin[1], day.isoformat(), day.isoformat())
    target = _group(board)["target"]
    assert target["state"] == "no_target" and target["percent"] is None
    assert target["text"] == "No target" and target["no_target"] == 1
    assert board["overall"]["target"]["percent"] is None
    assert board["by_day"][0]["target"]["text"] == "No target"


def test_exactly_on_target_is_within_and_over_is_not(lab_on, ist, _catalogue, tech, phleb, signer,
                                                     ward_admin):
    day = hospital_time.hospital_today() - timedelta(days=14)
    at = datetime(day.year, day.month, day.day, 8, 0, tzinfo=IST)
    for minutes in (60, 61, 30):
        _released(_patient(_catalogue), tech, phleb, signer, received=at,
                  collected=at - timedelta(minutes=10), reported=at + timedelta(minutes=minutes))
    _released(_patient(_catalogue), tech, phleb, signer, key="NOT", received=at,
              collected=at - timedelta(minutes=10), reported=at + timedelta(minutes=500))
    target = _group(_board(ward_admin[1], day.isoformat(), day.isoformat()))["target"]
    assert target == {"with_target": 3, "within": 2, "no_target": 1, "unreadable": 0,
                      "percent": 66.7, "state": "reported", "text": "66.7%"}


def test_the_stat_target_applies_to_a_stat_test(lab_on, ist, _catalogue, tech, phleb, signer,
                                                ward_admin):
    day = hospital_time.hospital_today() - timedelta(days=17)
    at = datetime(day.year, day.month, day.day, 8, 0, tzinfo=IST)
    _released(_patient(_catalogue), tech, phleb, signer, priority="stat", received=at,
              collected=at - timedelta(minutes=5), reported=at + timedelta(minutes=45))
    board = _board(ward_admin[1], day.isoformat(), day.isoformat())
    stat = _group(board, priority="stat")
    assert stat["priority_text"] == "STAT"
    assert stat["target"]["within"] == 0 and stat["target"]["with_target"] == 1


def test_median_and_percentile_rules():
    assert tat.median([]) is None and tat.percentile([], 90) is None
    assert tat.median([3, 1, 2]) == 2 and tat.median([1, 2, 3, 4]) == 2.5
    assert tat.percentile(list(range(1, 11)), 90) == 9
    assert tat.percentile(list(range(1, 12)), 90) == 10
    assert tat.percentile([7], 90) == 7


def test_simulated_results_are_left_out_and_counted(lab_on, ist, _catalogue, tech, phleb, signer,
                                                    ward_admin):
    day = hospital_time.hospital_today() - timedelta(days=20)
    at = datetime(day.year, day.month, day.day, 8, 0, tzinfo=IST)
    made = _released(_patient(_catalogue), tech, phleb, signer, received=at,
                     collected=at - timedelta(minutes=5), reported=at + timedelta(minutes=10))
    with db_session() as db:
        db.execute("UPDATE lab_results SET source = 'simulated' WHERE id = ?",
                   (made["result_id"],))
    board = _board(ward_admin[1], day.isoformat(), day.isoformat())
    assert board["overall"]["count"] == 0 and board["simulated_excluded"] == 1


# ── 3. Live lists and criticals ─────────────────────────────────────────────

def test_the_overdue_list(lab_on, _catalogue, tech, phleb, ward_admin):
    now = datetime.now(timezone.utc)
    routine_p, stat_p, none_p = (_patient(_catalogue) for _ in range(3))
    _, routine_sid, routine_item = _in_lab(routine_p, tech, phleb)
    _, stat_sid, stat_item = _in_lab(stat_p, tech, phleb, priority="stat")
    _, none_sid, none_item = _in_lab(none_p, tech, phleb, key="NOT")
    with db_session() as db:
        db.execute("UPDATE lab_specimens SET received_at = ? WHERE id = ?",
                   (format_utc(now - timedelta(minutes=90)), routine_sid))
        db.execute("UPDATE lab_specimens SET received_at = ? WHERE id = ?",
                   (format_utc(now - timedelta(minutes=40)), stat_sid))
        db.execute("UPDATE lab_specimens SET received_at = ? WHERE id = ?",
                   (format_utc(now - timedelta(hours=20)), none_sid))
    today = hospital_time.hospital_today().isoformat()
    overdue = _board(ward_admin[1], today, today)["overdue"]
    items = [o["item_id"] for o in overdue["items"]]
    assert items[:2] == [stat_item, routine_item], "STAT first, then the most overdue"
    assert none_item not in items
    assert overdue["in_lab_without_target"] >= 1
    routine = next(o for o in overdue["items"] if o["item_id"] == routine_item)
    assert routine["target_minutes"] == 60 and 29 <= routine["over_by_minutes"] <= 31
    assert routine["patient"]["mrn"] == routine_p["mrn"]


def test_lists_and_counts_stay_inside_the_department(lab_on, _catalogue, tech, phleb):
    elsewhere = _patient(_catalogue, dept=f"Elsewhere {SUFFIX}")
    _, sid, item = _in_lab(elsewhere, tech, phleb)
    with db_session() as db:
        db.execute("UPDATE lab_specimens SET received_at = ? WHERE id = ?",
                   (format_utc(datetime.now(timezone.utc) - timedelta(hours=3)), sid))
        order_id = db.execute("SELECT order_id FROM lab_order_items WHERE id = ?",
                              (item,)).fetchone()[0]
        db.execute("INSERT INTO lab_exceptions (kind, order_id, detail, created_at) "
                   "VALUES ('order_state_conflict', ?, 'test', ?)", (order_id, utc_iso_now()))
    _, scoped = _user("admin", dept=DEPT)
    today = hospital_time.hospital_today().isoformat()
    board = _board(scoped, today, today)
    assert item not in [o["item_id"] for o in board["overdue"]["items"]]
    assert all(k["kind"] != "order_state_conflict" or k["count"] == 0
               for k in board["exceptions"]["by_kind"])
    hospital_wide = _board(tech[1], today, today)
    assert item in [o["item_id"] for o in hospital_wide["overdue"]["items"]]
    conflict = next(k for k in hospital_wide["exceptions"]["by_kind"]
                    if k["kind"] == "order_state_conflict")
    assert conflict["count"] >= 1 and conflict["label"] == "Order changed outside the laboratory"
    assert board["scope"] == {"department": DEPT, "hospital_wide": False}


def test_time_to_critical_acknowledgement(lab_on, _catalogue, ward_admin):
    """As lab_critical_service writes them: a callback with a read-back is a
    'callback_logged' plus an 'acknowledged' (closure laboratory_callback); a
    call that reached nobody is a 'callback_logged' alone and acknowledges
    nothing."""
    now = datetime.now(timezone.utc)
    phone = json.dumps({"closure": "laboratory_callback"})
    rows = []
    for minutes, closure in ((10, None), (25, phone), (None, None)):
        p = _patient(_catalogue)
        nid = f"crit-{uuid.uuid4().hex[:8]}"
        raised = now - timedelta(hours=2)
        rows.append((nid, p["id"], "raised", raised, None))
        # An unanswered call first: never an acknowledgement.
        rows.append((nid, p["id"], "callback_logged", raised + timedelta(minutes=3),
                     json.dumps({"reached": False})))
        if minutes is not None:
            if closure:
                rows.append((nid, p["id"], "callback_logged", raised + timedelta(minutes=minutes),
                             json.dumps({"reached": True})))
            rows.append((nid, p["id"], "acknowledged", raised + timedelta(minutes=minutes),
                         closure))
            # A later event never counts as the first acknowledgement.
            rows.append((nid, p["id"], "acknowledged", raised + timedelta(minutes=90), None))
    with db_session() as db:
        for nid, pid, event, at, meta in rows:
            db.execute("INSERT INTO lab_critical_events (notification_id, patient_id, event_type, "
                       "occurred_at, metadata_json) VALUES (?, ?, ?, ?, ?)",
                       (nid, pid, event, format_utc(at), meta))
    today = hospital_time.hospital_today().isoformat()
    yesterday = (hospital_time.hospital_today() - timedelta(days=1)).isoformat()
    crit = _board(ward_admin[1], yesterday, today)["critical"]
    assert crit["raised"] == 3 and crit["acknowledged"] == 2 and crit["open"] == 1
    assert crit["median_minutes"] == 17.5 and crit["p90_minutes"] == 25.0
    assert crit["by_phone_log"] == 1 and crit["by_ward_read_back"] == 1
    assert crit["oldest_open_minutes"] >= 119


# ── 4. Audit, export and the switch ─────────────────────────────────────────

def test_the_read_is_audited(lab_on, tech):
    today = hospital_time.hospital_today().isoformat()
    _board(tech[1], today, today)
    with db_session() as db:
        row = db.execute("SELECT event_category, target_type FROM audit_logs "
                         "WHERE action_type = 'lab_tat_viewed' AND actor_user_id = ? "
                         "ORDER BY id DESC LIMIT 1", (tech[0],)).fetchone()
    assert row is not None and row["event_category"] == "phi_access"


def test_the_export_is_audited_and_carries_no_patient_identity(lab_on, ist, _catalogue, tech,
                                                               phleb, signer, ward_admin):
    p = _patient(_catalogue)
    day = hospital_time.hospital_today() - timedelta(days=23)
    at = datetime(day.year, day.month, day.day, 8, 0, tzinfo=IST)
    made = _released(p, tech, phleb, signer, received=at, collected=at - timedelta(minutes=15),
                     reported=at + timedelta(minutes=50))
    resp = client.get("/api/lab/tat/export", params={"from": day.isoformat(),
                                                     "to": day.isoformat()},
                      headers=ward_admin[1])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["filename"].endswith(".csv") and body["rows"] == 1
    assert p["name"] not in body["csv"] and p["mrn"] not in body["csv"]
    rows = list(csv.reader(io.StringIO(body["csv"])))
    assert rows[0] == list(tat.EXPORT_COLUMNS)
    line = dict(zip(rows[0], rows[1]))
    assert line["Accession"] == made["specimen_id"]
    assert line["Receipt to report (min)"] == "50.0"
    assert line["Collection to receipt (min)"] == "15.0"
    assert line["Within target"] == "Yes" and line["Simulated"] == "No"
    assert line["Reported (hospital time)"] == (at + timedelta(minutes=50)).strftime(
        "%Y-%m-%d %H:%M")
    with db_session() as db:
        row = db.execute("SELECT event_category, metadata_json FROM audit_logs "
                         "WHERE action_type = 'lab_tat_exported' AND actor_user_id = ? "
                         "ORDER BY id DESC LIMIT 1", (ward_admin[0],)).fetchone()
    assert row["event_category"] == "phi_access"
    assert p["id"] in json.loads(row["metadata_json"])["patient_ids"]


def test_a_cell_is_never_a_formula():
    assert tat._cell("=HYPERLINK(1)") == "'=HYPERLINK(1)"
    assert tat._cell("+1") == "'+1" and tat._cell("@x") == "'@x"
    assert tat._cell("Glucose") == "Glucose" and tat._cell(None) == ""


def test_an_unauditable_read_or_export_shows_nothing(lab_on, tech, monkeypatch):
    def refuse(**_kw):
        raise AuditWriteError("audit store unavailable")

    monkeypatch.setattr(tat_routes, "log_phi_read", refuse)
    for path in ("/api/lab/tat", "/api/lab/tat/export"):
        resp = client.get(path, headers=tech[1])
        assert resp.status_code == 503 and resp.json()["detail"]["error"] == "audit_unavailable"
        assert "csv" not in resp.json() and "overdue" not in resp.json()


def test_bad_periods_are_refused(lab_on, tech):
    today = hospital_time.hospital_today()
    for params in ({"from": "yesterday"},
                   {"from": today.isoformat(), "to": (today - timedelta(days=1)).isoformat()},
                   {"to": (today + timedelta(days=1)).isoformat()},
                   {"from": (today - timedelta(days=tat.MAX_PERIOD_DAYS)).isoformat(),
                    "to": today.isoformat()}):
        resp = client.get("/api/lab/tat", params=params, headers=tech[1])
        assert resp.status_code == 422, (params, resp.text)
        assert resp.json()["detail"]["error"] == "bad_period"


def test_who_may_read_the_turnaround_times(lab_on):
    for role, dept in (("doctor", "General Medicine"), ("nurse", "General Medicine"),
                       ("pharmacist", None), ("receptionist", None)):
        _, hdr = _user(role, dept)
        assert client.get("/api/lab/tat", headers=hdr).status_code == 403, role
    _, phleb_hdr = _user("phlebotomist")
    assert client.get("/api/lab/tat", headers=phleb_hdr).status_code == 200


def test_the_routes_are_404_with_the_module_off(tech, monkeypatch):
    monkeypatch.setattr(config, "LAB_ENABLED", False)
    monkeypatch.setattr(config, "_MODULE_DRAIN_CHECKS", {"lab": lambda: False})
    for path in ("/api/lab/tat", "/api/lab/tat/export"):
        resp = client.get(path, headers=tech[1])
        assert resp.status_code == 404 and resp.json()["detail"]["error"] == "module_disabled"


# ── 5. The optional NABH indicators ─────────────────────────────────────────

def test_nabh_is_unchanged_with_the_laboratory_off(monkeypatch):
    monkeypatch.setattr(config, "LAB_ENABLED", False)
    monkeypatch.setattr(config, "_MODULE_DRAIN_CHECKS", {"lab": lambda: False})
    dash = nabh_service.indicators()
    ids = [i["id"] for i in dash["indicators"]]
    assert ids == list(nabh_service.INDICATOR_IDS) and len(ids) == 9
    assert [i["id"] for i in nabh_service.meta()["indicators"]] == ids


def test_nabh_shows_the_laboratory_indicators_with_the_laboratory_on(lab_on):
    dash = nabh_service.indicators(ward=WARD_LABEL)
    ids = [i["id"] for i in dash["indicators"]]
    assert ids[-2:] == ["lab_tat_within_target", "lab_critical_ack_in_window"]
    assert dash["summary"]["total"] == 11
    assert [i["id"] for i in nabh_service.meta()["indicators"]][-2:] == ids[-2:]


def test_nabh_laboratory_indicators_are_honest(lab_on, ist, _catalogue, tech, phleb, signer,
                                               monkeypatch):
    ward = f"NABH-{uuid.uuid4().hex[:6]}"
    end = datetime.now(timezone.utc) + timedelta(minutes=1)
    start = end - timedelta(days=30)
    with db_session() as db:
        empty = nabh_service._lab_tat_within_target(db, start, end, ward)
    assert empty["state"] == "not_collected" and empty["value"] is None
    p = _patient(_catalogue)
    with db_session() as db:
        db.execute("UPDATE patients SET ward = ? WHERE id = ?", (ward, p["id"]))
    at = datetime.now(timezone.utc) - timedelta(days=2)
    _released(p, tech, phleb, signer, key="NOT", received=at, collected=at - timedelta(minutes=5),
              reported=at + timedelta(minutes=30))
    with db_session() as db:
        no_target = nabh_service._lab_tat_within_target(db, start, end, ward)
    assert no_target["state"] == "not_computable" and no_target["value"] is None
    assert no_target["excluded"]["no_target"] == 1
    assert "turnaround target" in no_target["blockers"][0]
    _released(p, tech, phleb, signer, received=at, collected=at - timedelta(minutes=5),
              reported=at + timedelta(minutes=30))
    with db_session() as db:
        few = nabh_service._lab_tat_within_target(db, start, end, ward)
    assert few["state"] == "insufficient_data" and few["value"] is None
    monkeypatch.setenv("ORB_NABH_MIN_N_LAB_TAT_WITHIN_TARGET", "1")
    with db_session() as db:
        reported = nabh_service._lab_tat_within_target(db, start, end, ward)
    assert reported["state"] == "reported" and reported["display"] == "100.0%"
    # The critical indicator: one acknowledged in 10 minutes, one late, one
    # still inside its window.
    now = datetime.now(timezone.utc)
    with db_session() as db:
        for minutes, ago in ((10, 120), (40, 120), (None, 5)):
            nid = f"nabh-{uuid.uuid4().hex[:8]}"
            raised = now - timedelta(minutes=ago)
            db.execute("INSERT INTO lab_critical_events (notification_id, patient_id, event_type, "
                       "occurred_at) VALUES (?, ?, 'raised', ?)", (nid, p["id"], format_utc(raised)))
            # An unanswered call never counts.
            db.execute("INSERT INTO lab_critical_events (notification_id, patient_id, "
                       "event_type, occurred_at) VALUES (?, ?, 'callback_logged', ?)",
                       (nid, p["id"], format_utc(raised + timedelta(minutes=1))))
            if minutes is not None:
                db.execute("INSERT INTO lab_critical_events (notification_id, patient_id, "
                           "event_type, occurred_at, metadata_json) "
                           "VALUES (?, ?, 'acknowledged', ?, ?)",
                           (nid, p["id"], format_utc(raised + timedelta(minutes=minutes)),
                            json.dumps({"closure": "laboratory_callback"})))
    monkeypatch.setenv("ORB_NABH_MIN_N_LAB_CRITICAL_ACK_IN_WINDOW", "1")
    with db_session() as db:
        crit = nabh_service._lab_critical_ack_in_window(db, start, end, ward)
    assert crit["numerator"] == 1 and crit["denominator"] == 2
    assert crit["excluded"]["still_inside_callback_window"] == 1
    assert crit["display"] == "50.0%" and "15 minutes" in crit["definition"]


def test_the_evidence_pack_carries_the_laboratory_indicators_when_on(lab_on):
    pack = nabh_service.build_evidence_pack(ward=WARD_LABEL)
    text = nabh_service.render_pack_markdown(pack)
    assert "QI-LAB-01" in text and "QI-LAB-02" in text
    assert chr(0x2014) not in "".join(
        str(v) for spec in nabh_service.LAB_INDICATOR_SPEC.values() for v in spec.values())
