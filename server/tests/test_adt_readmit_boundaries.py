"""Readmission carry-over boundaries: ruling ADT-S9 (a) (adt-18b, built in W8-A).

A patient readmitted onto the same record keeps every vitals and risk_scores
row of the earlier stay. Until a set is charted in the new stay, every
"latest" reader used to print, and score, the earlier stay's last set as if it
had been taken now. Built as the safety default: the earlier stay's rows are
shown only with their date and a 'Previous stay' label, never as the current
latest, on the board, NEWS2 and Sage.

What this file defends:

  1. THE BOUNDARY. Only a patient on an active admission who also has an
     ended one has a boundary (the current stay's start); a patient with one
     stay has none, and every reader answers exactly as before, even for
     vitals charted before their admission row was written.
  2. EVERY READER, ONE TEST. The board (and the Sage ward brief that shares
     its rows), the NEWS2 entry points (get_patient_risk, is_latest_vitals_row,
     the chart's command bar and risk history, the sparkline, the stored band
     the expected-course screen reads, the trend) and Sage (the fresh risk
     block, the authoritative NEWS2 line, the patient context, the census and
     the hospital risk distribution) show this stay's newest set, with a
     same-timestamp correction winning on id, and never the earlier stay's.
     Each keeps ORDER BY timestamp DESC, id DESC.
  3. DATED AND LABELLED. With nothing charted this stay, the board row and the
     chart carry the earlier stay's last set with its time and the label, and
     no NEWS2 is computed from it; Sage names it as the previous stay's, with
     its time, and not current.
  4. SCORES OFF (the Orb Ward Record). Nothing is scored either way, and the
     earlier stay's stored band is not sent.
  5. END TO END. A readmission through POST /api/admissions/readmit leaves the
     board showing nothing current until a set is charted.
  6. THE STAYS NEVER OVERLAP. With ORB_ADT on, a readmission back-timed to
     before the earlier stay ended is refused 400 before_previous_stay_end
     and writes nothing; a time after it is admitted. For stays that overlap
     anyway (written before the refusal existed), the boundary is the earlier
     stay's recorded end (its discharged_at, or the summary keyed to it), so
     the set charted while it was still open is never the latest. An end
     after now, and a history import's encounter, never move the boundary.
"""
from __future__ import annotations

import inspect
import re
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import config
from auth import create_token
from database import db_session
from main import app
from services import vigil_service
from services.vigil_service import compute_news2

client = TestClient(app)

DEPT = "Readmit Boundary Medicine"

OLD_CRITICAL = dict(heart_rate=138, systolic_bp=80, diastolic_bp=48, spo2=84,
                    temperature=39.4, respiratory_rate=33)
BENIGN = dict(heart_rate=72, systolic_bp=124, diastolic_bp=78, spo2=97,
              temperature=36.8, respiratory_rate=16)
CORRECTION = dict(heart_rate=112, systolic_bp=101, diastolic_bp=60, spo2=93,
                  temperature=38.2, respiratory_rate=23)


def _news2(v: dict) -> int:
    return compute_news2(v["heart_rate"], v["systolic_bp"], v["respiratory_rate"],
                         v["temperature"], v["spo2"], "alert", False)["raw_news2"]


assert len({_news2(OLD_CRITICAL), _news2(BENIGN), _news2(CORRECTION)}) == 3

_PATIENTS: list[str] = []


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _insert_vitals(db, pid, ts, v):
    return db.execute(
        """INSERT INTO vitals (patient_id, timestamp, heart_rate, systolic_bp, diastolic_bp,
                               spo2, temperature, respiratory_rate, consciousness, on_oxygen)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'alert', 0)""",
        (pid, ts, v["heart_rate"], v["systolic_bp"], v["diastolic_bp"], v["spo2"],
         v["temperature"], v["respiratory_rate"])).lastrowid


def _risk(db, pid, ts, level):
    db.execute("INSERT INTO risk_scores (patient_id, timestamp, score, level, drivers) "
               "VALUES (?, ?, 0.5, ?, '[]')", (pid, ts, level))


@pytest.fixture(scope="module")
def doctor():
    uid = f"RBD{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute("INSERT INTO users (id, name, role, password_hash, department, is_active) "
                   "VALUES (?, 'Dr Boundary', 'doctor', 'x', ?, 1)", (uid, DEPT))
    return uid


@pytest.fixture(autouse=True)
def _retire():
    yield
    if not _PATIENTS:
        return
    with db_session() as db:
        db.executemany("UPDATE patients SET status = 'discharged' WHERE id = ?",
                       [(p,) for p in _PATIENTS])
        db.executemany("UPDATE admissions SET status = 'discharged' WHERE patient_id = ? "
                       "AND status = 'active'", [(p,) for p in _PATIENTS])
    _PATIENTS.clear()


def _auth(uid, role="doctor"):
    return {"Authorization": f"Bearer {create_token(uid, role)}"}


def _patient(doctor, *, stays: int, ended_hours_ago: float = 72,
             started_hours_ago: float = 2):
    """An admitted patient. With stays=2: an earlier stay ended
    `ended_hours_ago`, with a critical set and a critical risk row at its end,
    and the current stay started `started_hours_ago`. With stays=1: one stay,
    started `started_hours_ago`, with the same 'old' rows charted BEFORE its
    admission row (as seeds and imports do), which must still read as latest."""
    pid = f"RB{uuid.uuid4().hex[:8].upper()}"
    now = datetime.now(timezone.utc)
    start = now - timedelta(hours=started_hours_ago)
    old_at = _iso(now - timedelta(hours=ended_hours_ago + 1))
    with db_session() as db:
        db.execute("INSERT INTO patients (id, name, mrn, ward, bed, diagnosis, status, age, sex, "
                   "attending_doctor_id, admission_date) VALUES (?, ?, ?, 'RB-W1', ?, 'Sepsis', "
                   "'admitted', 70, 'M', ?, ?)",
                   (pid, f"Boundary {pid[-4:]}", f"MRN-{pid}", f"B{pid[-3:]}", doctor,
                    _iso(start)))
        if stays == 2:
            db.execute("INSERT INTO admissions (id, patient_id, admitted_by, department, status, "
                       "created_at, discharged_at) VALUES (?, ?, ?, ?, 'discharged', ?, ?)",
                       (f"ADM-RBO{uuid.uuid4().hex[:6]}", pid, doctor, DEPT,
                        _iso(now - timedelta(hours=ended_hours_ago + 48)),
                        _iso(now - timedelta(hours=ended_hours_ago))))
        db.execute("INSERT INTO admissions (id, patient_id, admitted_by, department, status, "
                   "created_at) VALUES (?, ?, ?, ?, 'active', ?)",
                   (f"ADM-RBN{uuid.uuid4().hex[:6]}", pid, doctor, DEPT, _iso(start)))
        old_id = _insert_vitals(db, pid, old_at, OLD_CRITICAL)
        _risk(db, pid, old_at, "critical")
    _PATIENTS.append(pid)
    return {"pid": pid, "start": start, "old_at": old_at, "old_id": old_id}


def _chart_this_stay(pid, *, minutes_ago=10, tie=True):
    """A benign set then a correction at the byte-identical time, this stay."""
    ts = _iso(datetime.now(timezone.utc) - timedelta(minutes=minutes_ago))
    with db_session() as db:
        benign = _insert_vitals(db, pid, ts, BENIGN)
        correction = _insert_vitals(db, pid, ts, CORRECTION) if tie else None
        _risk(db, pid, ts, "elevated")
    return {"ts": ts, "benign": benign, "correction": correction}


def _board_row(doctor, pid):
    resp = client.get("/api/vigil/patients?limit=500", headers=_auth(doctor))
    assert resp.status_code == 200, resp.text
    return next(r for r in resp.json() if r["id"] == pid)


# ═════════════════════════════════════════════════════════════════════════════
# 1. The boundary
# ═════════════════════════════════════════════════════════════════════════════

def test_only_a_patient_with_an_ended_stay_has_a_boundary(doctor):
    readmitted = _patient(doctor, stays=2)
    single = _patient(doctor, stays=1)
    with db_session() as db:
        starts = vigil_service.readmission_stay_starts_on(db, [readmitted["pid"], single["pid"]])
        assert vigil_service.current_stay_start_on(db, single["pid"]) is None
    assert set(starts) == {readmitted["pid"]}
    assert abs((starts[readmitted["pid"]] - readmitted["start"]).total_seconds()) < 1
    assert vigil_service.from_previous_stay(readmitted["old_at"], starts[readmitted["pid"]])
    assert not vigil_service.from_previous_stay(readmitted["old_at"], None)
    assert vigil_service.from_previous_stay("not a time", starts[readmitted["pid"]])


def test_admitted_at_is_the_start_when_adt_back_times_the_stay(doctor):
    p = _patient(doctor, stays=2, started_hours_ago=1)
    earlier_start = datetime.now(timezone.utc) - timedelta(hours=5)
    with db_session() as db:
        db.execute("UPDATE admissions SET admitted_at = ? WHERE patient_id = ? "
                   "AND status = 'active'", (_iso(earlier_start), p["pid"]))
        start = vigil_service.current_stay_start_on(db, p["pid"])
    assert abs((start - earlier_start).total_seconds()) < 1


def test_a_patient_with_one_stay_is_read_exactly_as_before(doctor):
    """The 'old' set was charted before the admission row: with one stay it
    is still the latest everywhere (seeded and imported charts look so)."""
    p = _patient(doctor, stays=1)
    row = _board_row(doctor, p["pid"])
    assert row["news2"]["raw_news2"] == _news2(OLD_CRITICAL)
    assert "previous_stay" not in row
    chart = client.get(f"/api/vigil/patients/{p['pid']}", headers=_auth(doctor)).json()
    assert chart["vitals"][0]["heart_rate"] == OLD_CRITICAL["heart_rate"]
    assert "previous_stay" not in chart
    assert vigil_service.get_patient_risk(p["pid"])["raw_news2"] == _news2(OLD_CRITICAL)


# ═════════════════════════════════════════════════════════════════════════════
# 2. Every reader, one test
# ═════════════════════════════════════════════════════════════════════════════

def _readers(doctor, pid):
    """name -> what each latest reader shows for `pid`: the heart rate of the
    set it presents as latest (None for none), or a NEWS2 it computed."""
    from routes import vigil_routes
    from services import sage_service

    out = {}
    row = _board_row(doctor, pid)
    out["board news2"] = row["news2"]["raw_news2"] if row["news2"] else None
    triple = next(t for t in vigil_service.scored_admitted_patients(DEPT) if t[0]["id"] == pid)
    out["scored_admitted_patients"] = triple[1]["heart_rate"] if triple[1] else None
    risk = vigil_service.get_patient_risk(pid)
    out["get_patient_risk"] = risk.get("raw_news2")
    chart = client.get(f"/api/vigil/patients/{pid}", headers=_auth(doctor)).json()
    out["chart vitals[0]"] = chart["vitals"][0]["heart_rate"] if chart["vitals"] else None
    out["chart risk"] = chart["risk"].get("raw_news2")
    out["chart risk history"] = [r["level"] for r in chart["risk_history"]]
    spark = client.get(f"/api/vigil/patients/{pid}/vitals?hours=500",
                       headers=_auth(doctor)).json()
    out["sparkline last"] = spark[-1]["heart_rate"] if spark else None
    stored = vigil_routes._load_latest_risk(pid)
    out["stored band"] = stored["level"] if stored else None
    with db_session() as db:
        block, rows = sage_service._fresh_risk_and_observations(db, pid)
        out["sage fresh risk"] = block["news2"] if block else None
        out["sage fresh rows"] = [r["heart_rate"] for r in rows]
        line = sage_service._authoritative_news2_line(db, pid)
        out["sage news2 line"] = int(re.findall(r"\d+", line.splitlines()[0])[-1]) if line else None
        ctx = sage_service._assemble_patient_context(db, pid, None)
        out["sage context vitals"] = ctx["vitals"][0] if ctx["vitals"] else None
        out["sage context risk"] = ctx["risk"]["level"] if ctx["risk"] else None
    return out


def test_every_latest_reader_shows_this_stays_correction_never_the_earlier_stay(doctor):
    p = _patient(doctor, stays=2)
    chart = _chart_this_stay(p["pid"])
    got = _readers(doctor, p["pid"])
    want_news2 = _news2(CORRECTION)
    assert got["board news2"] == want_news2
    assert got["scored_admitted_patients"] == CORRECTION["heart_rate"]
    assert got["get_patient_risk"] == want_news2
    assert got["chart vitals[0]"] == CORRECTION["heart_rate"]
    assert got["chart risk"] == want_news2
    assert got["chart risk history"] == ["elevated"]
    assert got["sparkline last"] == CORRECTION["heart_rate"]
    assert got["stored band"] == "elevated"
    assert got["sage fresh risk"] == want_news2
    assert got["sage fresh rows"][0] == CORRECTION["heart_rate"]
    assert OLD_CRITICAL["heart_rate"] not in got["sage fresh rows"]
    assert got["sage news2 line"] == want_news2
    assert got["sage context vitals"].startswith(f"HR:{CORRECTION['heart_rate']} ")
    # The band of this stay's set itself (the stored row is this stay's
    # 'elevated'; the earlier stay's 'critical' row is not read).
    assert got["sage context risk"] == compute_news2(
        CORRECTION["heart_rate"], CORRECTION["systolic_bp"], CORRECTION["respiratory_rate"],
        CORRECTION["temperature"], CORRECTION["spo2"], "alert", False)["level"]
    with db_session() as db:
        assert vigil_service.is_latest_vitals_row(db, p["pid"], chart["correction"])
        assert not vigil_service.is_latest_vitals_row(db, p["pid"], chart["benign"])
        assert not vigil_service.is_latest_vitals_row(db, p["pid"], p["old_id"])


def test_with_nothing_charted_this_stay_no_reader_shows_the_earlier_set(doctor):
    p = _patient(doctor, stays=2)
    got = _readers(doctor, p["pid"])
    assert got == {
        "board news2": None, "scored_admitted_patients": None,
        "get_patient_risk": None, "chart vitals[0]": None, "chart risk": None,
        "chart risk history": [], "sparkline last": None, "stored band": None,
        "sage fresh risk": None, "sage fresh rows": [], "sage news2 line": None,
        "sage context vitals": None, "sage context risk": None,
    }
    assert vigil_service.get_patient_risk(p["pid"])["drivers"] == ["No vitals recorded"]


def test_the_earlier_stays_stored_band_never_raises_this_stays(doctor):
    """Sage's stored band may only ESCALATE a fresh one (the CfC overlay).
    The earlier stay's 'critical' row must not raise this stay's 'stable'."""
    from services import sage_service

    p = _patient(doctor, stays=2)
    with db_session() as db:
        _insert_vitals(db, p["pid"], _iso(datetime.now(timezone.utc) - timedelta(minutes=5)),
                       BENIGN)
        block, _rows = sage_service._fresh_risk_and_observations(db, p["pid"])
    assert block["news2"] == _news2(BENIGN)
    assert block["level"] == "stable"


def test_a_set_back_charted_into_the_earlier_stay_is_never_pushed_as_latest(doctor):
    p = _patient(doctor, stays=2)
    back = _iso(p["start"] - timedelta(hours=1))
    with db_session() as db:
        back_id = _insert_vitals(db, p["pid"], back, BENIGN)
        assert not vigil_service.is_latest_vitals_row(db, p["pid"], back_id)
    assert _board_row(doctor, p["pid"])["news2"] is None


def test_the_trend_never_compares_across_the_stays(doctor):
    """Discharged 8 h ago, back 3 h ago: the earlier stay's set sits in the
    trend's comparison window and must not be its baseline."""
    p = _patient(doctor, stays=2, ended_hours_ago=7, started_hours_ago=3)
    _chart_this_stay(p["pid"], minutes_ago=30, tie=False)
    assert vigil_service.get_vitals_trend(p["pid"], hours=6) == {
        "trend": "insufficient_data", "reason": "no earlier vitals to compare against"}
    single = _patient(doctor, stays=1, ended_hours_ago=7, started_hours_ago=3)
    _chart_this_stay(single["pid"], minutes_ago=30, tie=False)
    assert vigil_service.get_vitals_trend(single["pid"], hours=6)["trend"] != "insufficient_data"


def test_the_sage_census_and_distribution_leave_the_earlier_stay_out(doctor):
    from services.sage_service import build_aggregate_context, get_relevant_context

    def criticals():
        text = build_aggregate_context("how many patients are critical")
        found = re.search(r"Risk distribution: critical (\d+)", text)
        return int(found.group(1)) if found else 0

    before = criticals()
    p = _patient(doctor, stays=2)
    assert criticals() == before        # the earlier stay's critical row is not counted
    single = _patient(doctor, stays=1)
    assert criticals() == before + 1    # a one-stay critical row still counts

    user = {"id": doctor, "role": "doctor", "department": DEPT, "name": "Dr Boundary"}
    context, *_ = get_relevant_context("give me an overview of all my patients", user)
    line = next((ln for ln in context.splitlines() if f"Boundary {p['pid'][-4:]}" in ln), "")
    assert line, context
    assert f"HR:{OLD_CRITICAL['heart_rate']}" not in line and "critical" not in line.lower()
    control = next(ln for ln in context.splitlines() if f"Boundary {single['pid'][-4:]}" in ln)
    assert f"HR:{OLD_CRITICAL['heart_rate']}" in control


def test_each_reader_keeps_order_by_timestamp_desc_id_desc():
    from routes import vigil_routes
    from services import sage_service

    readers = [
        vigil_service.scored_admitted_patients, vigil_service.get_patient_risk,
        vigil_service.is_latest_vitals_row, vigil_service._assemble_feature_sequence,
        vigil_service.previous_stay_on, vigil_routes._load_patient_bundle,
        vigil_routes._load_latest_risk, sage_service._fresh_risk_and_observations,
        sage_service._authoritative_news2_line, sage_service._assemble_patient_context,
        sage_service.build_aggregate_context,
    ]
    for fn in readers:
        src = re.sub(r"\s+", " ", inspect.getsource(fn))
        assert re.search(r"ORDER BY (\w+\.)?timestamp DESC, (\w+\.)?id DESC", src), fn.__name__
        assert not re.search(r"ORDER BY (\w+\.)?timestamp DESC (LIMIT|\")", src), fn.__name__


# ═════════════════════════════════════════════════════════════════════════════
# 3. Dated and labelled
# ═════════════════════════════════════════════════════════════════════════════

def test_the_board_row_and_the_chart_carry_the_earlier_stay_dated_and_labelled(doctor):
    p = _patient(doctor, stays=2)
    row = _board_row(doctor, p["pid"])
    assert row["news2"] is None and row["news2_at"] is None and row["risk_level"] is None
    prev = row["previous_stay"]
    assert prev["label"] == vigil_service.PREVIOUS_STAY_LABEL == "Previous stay"
    assert prev["last_observed_at"] == p["old_at"]
    assert prev["last_vitals"]["previous_stay"] is True
    assert prev["last_vitals"]["heart_rate"] == OLD_CRITICAL["heart_rate"]
    assert "news2" not in prev["last_vitals"] and "level" not in prev["last_vitals"]

    chart = client.get(f"/api/vigil/patients/{p['pid']}", headers=_auth(doctor)).json()
    assert chart["vitals"] == [] and chart["risk_history"] == []
    assert chart["risk"]["drivers"] == ["No vitals recorded"]
    assert chart["previous_stay"]["last_vitals"]["timestamp"] == p["old_at"]
    assert chart["previous_stay"]["last_risk"] == {
        "level": "critical", "timestamp": p["old_at"], "previous_stay": True}
    assert chart["previous_stay"]["this_stay_started_at"] == _iso(p["start"])

    # Once a set is charted this stay, the board is scored from it, and the
    # earlier stay is still there with its date.
    _chart_this_stay(p["pid"], tie=False)
    row = _board_row(doctor, p["pid"])
    assert row["news2"]["raw_news2"] == _news2(BENIGN)
    assert row["previous_stay"]["last_observed_at"] == p["old_at"]


def test_sage_names_the_earlier_stay_with_its_time_and_as_not_current(doctor):
    from services.sage_service import PREVIOUS_STAY_MISSING, _assemble_patient_context

    p = _patient(doctor, stays=2)
    with db_session() as db:
        ctx = _assemble_patient_context(db, p["pid"], None)
    assert ctx["vitals"] == [] and ctx["risk"] is None
    line = next(m for m in ctx["completeness"]["missing"]
                if m.startswith(PREVIOUS_STAY_MISSING))
    assert p["old_at"] in line and "not current" in line and "previous stay" in line


# ═════════════════════════════════════════════════════════════════════════════
# 4. Scores off (the Orb Ward Record)
# ═════════════════════════════════════════════════════════════════════════════

def test_with_scores_off_nothing_is_scored_and_no_earlier_band_is_sent(doctor, monkeypatch):
    monkeypatch.setattr(config, "SCORING_ENABLED", False)
    p = _patient(doctor, stays=2)
    row = _board_row(doctor, p["pid"])
    assert row["news2"] is None
    assert "last_risk" not in row["previous_stay"]
    chart = client.get(f"/api/vigil/patients/{p['pid']}", headers=_auth(doctor)).json()
    assert chart["risk"] is None and "last_risk" not in chart["previous_stay"]
    assert chart["previous_stay"]["last_vitals"]["timestamp"] == p["old_at"]


# ═════════════════════════════════════════════════════════════════════════════
# 5. End to end through the readmission
# ═════════════════════════════════════════════════════════════════════════════

def test_after_a_readmission_the_board_shows_nothing_current_until_a_set_is_charted(
        doctor, monkeypatch):
    from services import admission_service as adm

    monkeypatch.setattr(config, "REGISTRATION_ENABLED", True)
    monkeypatch.setattr(config, "READMISSION_ENABLED", True)
    monkeypatch.setattr(config, "ADT_ENABLED", False)
    p = _patient(doctor, stays=1)
    with db_session() as db:
        db.execute("UPDATE patients SET status = 'discharged', date_of_birth = '1955-01-01' "
                   "WHERE id = ?", (p["pid"],))
        db.execute("UPDATE admissions SET status = 'discharged' WHERE patient_id = ?",
                   (p["pid"],))
    mrn = f"MRN-{p['pid']}"
    resp = client.post(
        "/api/admissions/readmit",
        json={"patient_id": p["pid"],
              "identity_check": {"name": f"Boundary {p['pid'][-4:]}",
                                 "date_of_birth": "1955-01-01", "uhid": mrn},
              "department": DEPT, "ward": "RB-W2", "bed": "B77", "attending_doctor_id": doctor,
              "chief_complaint": "Fever again"},
        headers={**_auth(doctor), "Idempotency-Key": uuid.uuid4().hex})
    assert resp.status_code == 201, resp.text
    assert resp.json()["readmission"] is True
    row = _board_row(doctor, p["pid"])
    assert row["news2"] is None
    assert row["previous_stay"]["last_observed_at"] == p["old_at"]
    assert adm.READMISSION_AUDIT_ACTION
    _chart_this_stay(p["pid"], minutes_ago=0, tie=False)
    assert _board_row(doctor, p["pid"])["news2"]["raw_news2"] == _news2(BENIGN)


# ═════════════════════════════════════════════════════════════════════════════
# 6. The stays never overlap
# ═════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def adt_readmission(monkeypatch):
    monkeypatch.setattr(config, "REGISTRATION_ENABLED", True)
    monkeypatch.setattr(config, "READMISSION_ENABLED", True)
    monkeypatch.setattr(config, "ADT_ENABLED", True)
    yield


def _discharged_an_hour_ago(doctor):
    """A patient whose one stay began two days ago and ended an hour ago, with
    a critical set charted two hours ago, inside that stay."""
    p = _patient(doctor, stays=1, ended_hours_ago=1, started_hours_ago=48)
    ended = datetime.now(timezone.utc) - timedelta(hours=1)
    with db_session() as db:
        db.execute("UPDATE patients SET status = 'discharged', date_of_birth = '1955-01-01' "
                   "WHERE id = ?", (p["pid"],))
        db.execute("UPDATE admissions SET status = 'discharged', discharged_at = ? "
                   "WHERE patient_id = ?", (_iso(ended), p["pid"]))
    return {**p, "ended": ended}


def _readmit_adt(doctor, p, admitted_at):
    return client.post(
        "/api/admissions/readmit",
        json={"patient_id": p["pid"],
              "identity_check": {"name": f"Boundary {p['pid'][-4:]}",
                                 "date_of_birth": "1955-01-01", "uhid": f"MRN-{p['pid']}"},
              "department": DEPT, "ward": "", "bed": "", "awaiting_bed": True,
              "attending_doctor_id": doctor, "chief_complaint": "Fever again",
              "admitted_at": _iso(admitted_at)},
        headers={**_auth(doctor), "Idempotency-Key": uuid.uuid4().hex})


def _stay_rows(pid):
    with db_session() as db:
        return {
            "status": db.execute("SELECT status FROM patients WHERE id = ?",
                                 (pid,)).fetchone()["status"],
            "admissions": db.execute("SELECT COUNT(*) FROM admissions WHERE patient_id = ?",
                                     (pid,)).fetchone()[0],
            "movements": db.execute("SELECT COUNT(*) FROM patient_movements "
                                    "WHERE patient_id = ?", (pid,)).fetchone()[0],
        }


def test_a_readmission_back_timed_into_the_earlier_stay_is_refused(doctor, adt_readmission):
    """The reviewer's probe: discharged an hour ago with a critical set two
    hours ago; a readmission dated three hours ago would have made that set
    this stay's latest (NEWS2 from the earlier stay on the board)."""
    from services import admission_service as adm

    p = _discharged_an_hour_ago(doctor)
    before = _stay_rows(p["pid"])
    for when in (datetime.now(timezone.utc) - timedelta(hours=3),
                 p["ended"] - timedelta(minutes=1)):
        resp = _readmit_adt(doctor, p, when)
        assert resp.status_code == 400, resp.text
        detail = resp.json()["detail"]
        assert detail == {"error": "before_previous_stay_end",
                          "message": adm.READMISSION_BEFORE_PREVIOUS_END}
        assert _stay_rows(p["pid"]) == before == {"status": "discharged", "admissions": 1,
                                                  "movements": 0}

    resp = _readmit_adt(doctor, p, datetime.now(timezone.utc) - timedelta(minutes=30))
    assert resp.status_code == 201, resp.text
    assert resp.json()["readmission"] is True
    assert vigil_service.get_patient_risk(p["pid"]).get("raw_news2") is None
    with db_session() as db:
        start = vigil_service.current_stay_start_on(db, p["pid"])
        prev = vigil_service.previous_stay_on(db, p["pid"], start)
    assert vigil_service.from_previous_stay(p["old_at"], start)
    assert prev["last_vitals"]["heart_rate"] == OLD_CRITICAL["heart_rate"]
    assert prev["last_observed_at"] == p["old_at"]
    chart = client.get(f"/api/vigil/patients/{p['pid']}", headers=_auth(doctor)).json()
    assert chart["vitals"] == [] and chart["risk"]["drivers"] == ["No vitals recorded"]
    assert chart["previous_stay"]["last_vitals"]["heart_rate"] == OLD_CRITICAL["heart_rate"]


def test_with_adt_off_the_time_check_does_not_run(doctor):
    from services import admission_service as adm

    p = _discharged_an_hour_ago(doctor)
    with db_session() as db:
        assert adm._check_readmission_time_on(db, p["pid"], None) is None
        prev_end = adm._previous_stay_end_on(db, p["pid"])
    assert abs((prev_end - p["ended"]).total_seconds()) < 1


def test_the_previous_end_is_read_from_the_summary_else_the_stay_start(doctor):
    """An Orb discharge with ORB_ADT off leaves admissions.discharged_at NULL
    and dates its discharge summary; a stay with no end anywhere counts from
    its start."""
    from services import admission_service as adm

    p = _discharged_an_hour_ago(doctor)
    summary_at = datetime.now(timezone.utc) - timedelta(minutes=50)
    with db_session() as db:
        adm_id = db.execute("SELECT id, created_at FROM admissions WHERE patient_id = ?",
                            (p["pid"],)).fetchone()
        db.execute("UPDATE admissions SET discharged_at = NULL WHERE id = ?", (adm_id["id"],))
        assert abs((adm._previous_stay_end_on(db, p["pid"]) - p["start"]).total_seconds()) < 1
        db.execute("INSERT INTO discharge_summaries (patient_id, author_id, summary, "
                   "discharged_at) VALUES (?, ?, 'Earlier stay', ?)",
                   (p["pid"], doctor, summary_at.strftime("%Y-%m-%d %H:%M:%S")))
        got = adm._previous_stay_end_on(db, p["pid"])
    assert abs((got - summary_at).total_seconds()) < 1


def test_an_end_inside_the_clock_slack_refuses_a_readmission_now(doctor, adt_readmission):
    """An end recorded up to 5 minutes ahead (the event-time slack) is
    honoured: a readmission at now, blank time, is refused until it passes."""
    p = _discharged_an_hour_ago(doctor)
    with db_session() as db:
        db.execute("UPDATE admissions SET discharged_at = ? WHERE patient_id = ?",
                   (_iso(datetime.now(timezone.utc) + timedelta(minutes=2)), p["pid"]))
    resp = client.post(
        "/api/admissions/readmit",
        json={"patient_id": p["pid"],
              "identity_check": {"name": f"Boundary {p['pid'][-4:]}",
                                 "date_of_birth": "1955-01-01", "uhid": f"MRN-{p['pid']}"},
              "department": DEPT, "ward": "", "bed": "", "awaiting_bed": True,
              "attending_doctor_id": doctor, "chief_complaint": "Fever again"},
        headers={**_auth(doctor), "Idempotency-Key": uuid.uuid4().hex})
    assert resp.status_code == 400, resp.text
    assert resp.json()["detail"]["error"] == "before_previous_stay_end"
    assert _stay_rows(p["pid"])["admissions"] == 1


def test_a_mistyped_future_end_counts_from_the_stays_start(doctor, adt_readmission):
    """A history import keeps a mistyped discharge date as given; one a year
    ahead must not refuse every readmission of the patient. The stay then
    counts from its start, which still refuses a time before it."""
    from services import admission_service as adm

    p = _discharged_an_hour_ago(doctor)
    with db_session() as db:
        db.execute("UPDATE admissions SET discharged_at = ? WHERE patient_id = ?",
                   (_iso(datetime.now(timezone.utc) + timedelta(days=365)), p["pid"]))
        assert abs((adm._previous_stay_end_on(db, p["pid"]) - p["start"]).total_seconds()) < 1
    resp = _readmit_adt(doctor, p, datetime.now(timezone.utc) - timedelta(minutes=30))
    assert resp.status_code == 201, resp.text


def test_overlapping_stays_are_bounded_at_the_earlier_stays_end(doctor):
    """Stays written before the refusal existed: the current admission says it
    began 3 h ago, the earlier stay ended 1 h ago, and the critical set charted
    2 h ago, while the earlier stay was still open, is never the latest."""
    p = _patient(doctor, stays=2, ended_hours_ago=1, started_hours_ago=3)
    with db_session() as db:
        ended = db.execute("SELECT discharged_at FROM admissions WHERE patient_id = ? "
                           "AND status <> 'active'", (p["pid"],)).fetchone()["discharged_at"]
        start = vigil_service.current_stay_start_on(db, p["pid"])
    assert start == datetime.fromisoformat(ended) and start > p["start"]
    assert vigil_service.from_previous_stay(p["old_at"], start)
    got = _readers(doctor, p["pid"])
    assert got["get_patient_risk"] is None and got["chart vitals[0]"] is None
    assert got["scored_admitted_patients"] is None and got["sage fresh rows"] == []
    row = _board_row(doctor, p["pid"])
    assert row["news2"] is None
    assert row["previous_stay"]["last_vitals"]["heart_rate"] == OLD_CRITICAL["heart_rate"]
    # A set charted after the earlier stay ended is this stay's.
    _chart_this_stay(p["pid"], minutes_ago=10, tie=False)
    assert _board_row(doctor, p["pid"])["news2"]["raw_news2"] == _news2(BENIGN)


def test_a_summary_keyed_to_the_earlier_stay_bounds_it_too(doctor):
    p = _patient(doctor, stays=2, ended_hours_ago=1, started_hours_ago=3)
    summary_at = datetime.now(timezone.utc) - timedelta(minutes=70)
    with db_session() as db:
        earlier = db.execute("SELECT id FROM admissions WHERE patient_id = ? "
                             "AND status <> 'active'", (p["pid"],)).fetchone()["id"]
        db.execute("UPDATE admissions SET discharged_at = NULL WHERE id = ?", (earlier,))
        db.execute("INSERT INTO discharge_summaries (patient_id, author_id, summary, "
                   "discharged_at, admission_id) VALUES (?, ?, 'Earlier stay', ?, ?)",
                   (p["pid"], doctor, summary_at.strftime("%Y-%m-%d %H:%M:%S"), earlier))
        start = vigil_service.current_stay_start_on(db, p["pid"])
    assert abs((start - summary_at).total_seconds()) < 1
    assert vigil_service.get_patient_risk(p["pid"]).get("raw_news2") is None


def test_an_end_after_now_or_from_a_history_import_never_moves_the_boundary(doctor):
    """Neither may hide this stay's own sets."""
    p = _patient(doctor, stays=2)
    now = datetime.now(timezone.utc)
    with db_session() as db:
        for adm_id, ended, run in ((f"ADM-RBF{uuid.uuid4().hex[:6]}", now + timedelta(days=1),
                                    None),
                                   (f"ADM-RBI{uuid.uuid4().hex[:6]}", now - timedelta(minutes=5),
                                    f"RUN-{uuid.uuid4().hex[:6]}")):
            db.execute("INSERT INTO admissions (id, patient_id, admitted_by, department, status, "
                       "created_at, discharged_at, import_run_id) "
                       "VALUES (?, ?, ?, ?, 'discharged', ?, ?, ?)",
                       (adm_id, p["pid"], doctor, DEPT, _iso(now - timedelta(days=9)),
                        _iso(ended), run))
        start = vigil_service.current_stay_start_on(db, p["pid"])
    assert abs((start - p["start"]).total_seconds()) < 1
    _chart_this_stay(p["pid"], minutes_ago=30, tie=False)
    assert _board_row(doctor, p["pid"])["news2"]["raw_news2"] == _news2(BENIGN)
