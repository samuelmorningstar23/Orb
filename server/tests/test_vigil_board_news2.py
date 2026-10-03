"""The Vigil ward board row shows NEWS2 from ONE scored moment: the latest vitals row.

GET /api/vigil/patients used to read the row's risk off `risk_scores`, through a
join on `rs.timestamp = MAX(rs2.timestamp)`. Three things were wrong with that,
all verified 2026-10-02 and ruled by Samuel 2026-10-03:

  1. A TIMESTAMP TIE LISTED THE PATIENT TWICE. Two risk rows at the same instant
     both equal the MAX, so the join returned both. The board drew two rows for
     one bed, and LIMIT/OFFSET paging counted the duplicate, so a page boundary
     could drop a different patient off the board entirely.
  2. THE ROW WAS STALE. The vitals simulator and the Scribe lane write vitals
     but never a risk row, so every 30 s poll put back whichever risk row was
     written last (often the admission baseline) and undid the live ticks.
  3. THE NUMBER WAS NOT NEWS2. `risk_scores.score` is a 0-1 index of the
     aggregate, which the board printed as a percentage. The NEWS2 aggregate
     itself was not on the list at all.

The ruling: score each patient's latest vitals row (ORDER BY timestamp DESC,
id DESC) through `vigil_service.score_vitals_row`, WITH the CfC overlay so the
board agrees with the chart and the nurse board, and derive the whole row
(number, band, drivers, sort) from that one result.

Every test below fails on the route as it stood before this file was added: the
tie test on the duplicate row, the rest because the list carried no `news2`.
"""
import uuid

import pytest
from fastapi.testclient import TestClient

from auth import create_token
from database import db_session
from main import app
from services import vigil_service

client = TestClient(app)

TS = "2026-10-03T08:00:00+00:00"
EARLIER = "2026-10-03T06:00:00+00:00"

# NEWS2 on Scale 1, worked by hand from the RCP chart.
# HR 135 (3) + RR 26 (3) + SpO2 91 (3) = 9, critical.
CRITICAL_9 = dict(heart_rate=135.0, systolic_bp=118.0, respiratory_rate=26.0,
                  temperature=37.0, spo2=91.0)
# RR 25 (3) alone = 3, but a single parameter scoring 3 bands it 'elevated'.
SINGLE3_3 = dict(heart_rate=80.0, systolic_bp=120.0, respiratory_rate=25.0,
                 temperature=37.0, spo2=97.0)
# HR 105 (1) + Temp 38.5 (1) + SpO2 95 (1) = 3, no single 3, so 'stable'.
SPREAD_3 = dict(heart_rate=105.0, systolic_bp=120.0, respiratory_rate=16.0,
                temperature=38.5, spo2=95.0)
# Every vital in its 0-point band.
BENIGN_0 = dict(heart_rate=80.0, systolic_bp=120.0, respiratory_rate=16.0,
                temperature=37.0, spo2=97.0)
# RR 22 (2) + SpO2 93 (2) + HR 96 (1) + T 38.5 (1) = 6: below 'critical' on the
# table, so a critical band can only be the overlay's doing.
OVERLAY_6 = dict(heart_rate=96.0, systolic_bp=118.0, respiratory_rate=22.0,
                 temperature=38.5, spo2=93.0)


def _ward():
    """A doctor alone in a fresh department, so no other module's patients (and
    no other test's) can appear on this board."""
    dept = f"Board NEWS2 {uuid.uuid4().hex[:8]}"
    uid = f"BNU{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO users (id, name, role, password_hash, department) "
            "VALUES (?, 'Board News2 Doctor', 'doctor', 'x', ?)",
            (uid, dept),
        )
    return dept, uid, {"Authorization": f"Bearer {create_token(uid, 'doctor')}"}


def _patient(dept, admitted_by, *, on_scale_2=False):
    pid = f"BNP{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute(
            """INSERT INTO patients (id, name, mrn, bed, ward, age, sex, diagnosis,
                                     status, admission_date, on_scale_2)
               VALUES (?, ?, ?, ?, ?, 60, 'M', 'Board test', 'admitted', ?, ?)""",
            (pid, f"Board {pid}", f"MRN-{pid}", f"B-{pid[-4:]}", dept, TS,
             1 if on_scale_2 else 0),
        )
        db.execute(
            "INSERT INTO admissions (patient_id, department, ward, bed, status, admitted_by) "
            "VALUES (?, ?, ?, ?, 'active', ?)",
            (pid, dept, dept, f"B-{pid[-4:]}", admitted_by),
        )
    return pid


def _vitals(pid, v, *, ts=TS, consciousness="alert", on_oxygen=0):
    with db_session() as db:
        db.execute(
            """INSERT INTO vitals
                   (patient_id, timestamp, heart_rate, systolic_bp, diastolic_bp,
                    spo2, temperature, respiratory_rate, consciousness, on_oxygen,
                    source)
               VALUES (?, ?, ?, ?, 75, ?, ?, ?, ?, ?, 'nurse_observation')""",
            (pid, ts, v["heart_rate"], v["systolic_bp"], v["spo2"],
             v["temperature"], v["respiratory_rate"], consciousness, on_oxygen),
        )


def _risk_row(pid, *, score, level, ts=TS):
    with db_session() as db:
        db.execute(
            "INSERT INTO risk_scores (patient_id, timestamp, score, level, drivers) "
            "VALUES (?, ?, ?, ?, '[\"stored driver\"]')",
            (pid, ts, score, level),
        )


def _board(headers, **params):
    r = client.get("/api/vigil/patients", headers=headers, params=params)
    assert r.status_code == 200, r.text
    return r.json(), int(r.headers["X-Total-Count"])


@pytest.fixture(autouse=True)
def _no_checkpoint(monkeypatch):
    """Today's shipping state, made explicit: no CfC checkpoint loaded."""
    monkeypatch.setattr(vigil_service, "compute_cfc_risk", lambda _pid: None)


def test_a_timestamp_tie_does_not_list_a_patient_twice():
    dept, uid, headers = _ward()
    pid = _patient(dept, uid)
    # Two risk rows at the same instant: both equal MAX(timestamp).
    _risk_row(pid, score=0.1, level="stable")
    _risk_row(pid, score=0.45, level="critical")
    # Two vitals rows at the same instant too: a same-time correction. The
    # higher id is the later write, so it is the one the board must score.
    _vitals(pid, BENIGN_0)
    _vitals(pid, CRITICAL_9)

    rows, total = _board(headers)
    assert [r["id"] for r in rows] == [pid]
    assert total == 1
    assert rows[0]["news2"]["raw_news2"] == 9
    assert rows[0]["risk_level"] == "critical"


def test_the_row_comes_from_the_latest_vitals_not_a_stale_risk_row():
    """The simulator writes vitals and no risk row, so the newest risk row can be
    hours older than the newest observation. The board follows the observation."""
    dept, uid, headers = _ward()
    pid = _patient(dept, uid)
    _risk_row(pid, score=0.9, level="critical", ts=EARLIER)
    _vitals(pid, CRITICAL_9, ts=EARLIER)
    _vitals(pid, BENIGN_0, ts=TS)

    rows, _ = _board(headers)
    (row,) = rows
    assert row["news2"]["raw_news2"] == 0
    assert row["news2"]["level"] == "stable"
    assert row["news2_at"] == TS
    # The legacy fields are the SAME moment, never a second source.
    assert row["risk_level"] == "stable"
    assert row["risk_score"] == row["news2"]["score"]
    assert row["risk_drivers"] == row["news2"]["drivers"]
    assert "stored driver" not in row["risk_drivers"]


def test_a_patient_with_no_vitals_is_not_recorded_and_sorts_last():
    dept, uid, headers = _ward()
    none = _patient(dept, uid)
    # A risk row with no vitals behind it is not a score the board may show.
    _risk_row(none, score=0.9, level="critical")
    scored = _patient(dept, uid)
    _vitals(scored, BENIGN_0)

    rows, total = _board(headers)
    assert [r["id"] for r in rows] == [scored, none]
    assert total == 2
    last = rows[-1]
    assert last["news2"] is None
    assert last["news2_at"] is None
    assert last["risk_score"] is None
    assert last["risk_level"] is None
    assert last["risk_drivers"] == []


def test_board_order_is_monotonic_and_paging_visits_each_patient_once():
    """Critical first, then NEWS2 high to low; at an equal NEWS2 the single-3
    'elevated' patient above the 'stable' one; not recorded last. Paging one row
    at a time walks exactly that order with no repeat and no gap."""
    dept, uid, headers = _ward()
    none = _patient(dept, uid)
    spread = _patient(dept, uid)
    _vitals(spread, SPREAD_3)
    single3 = _patient(dept, uid)
    _vitals(single3, SINGLE3_3)
    crit = _patient(dept, uid)
    _vitals(crit, CRITICAL_9)
    benign = _patient(dept, uid)
    _vitals(benign, BENIGN_0)

    rows, total = _board(headers)
    assert [r["id"] for r in rows] == [crit, single3, spread, benign, none]
    assert [(r["news2"] or {}).get("raw_news2") for r in rows] == [9, 3, 3, 0, None]
    assert [r["risk_level"] for r in rows] == ["critical", "elevated", "stable", "stable", None]
    assert total == 5

    paged = []
    for offset in range(total):
        page, page_total = _board(headers, limit=1, offset=offset)
        assert page_total == total
        paged += [r["id"] for r in page]
    assert paged == [r["id"] for r in rows]


def test_the_board_scores_on_the_patients_own_spo2_scale():
    """SpO2 89 on air: 0 points on Scale 2 (target 88-92), 3 points on Scale 1."""
    dept, uid, headers = _ward()
    pid = _patient(dept, uid, on_scale_2=True)
    _vitals(pid, dict(BENIGN_0, spo2=89.0))

    (row,), _ = _board(headers)
    assert row["news2"]["spo2_scale"] == 2
    assert row["news2"]["raw_news2"] == 0


def test_the_board_and_the_chart_agree_with_the_overlay_active(monkeypatch):
    """The CfC overlay may escalate the band. The board must apply it exactly as
    the chart (get_patient_risk) does, or the two disagree about who is critical
    the day a checkpoint is loaded."""
    def _high(_pid):
        return {"outcomes": {"deterioration": {"risk_by_horizon": {"6": 0.92},
                                               "peak_risk": 0.92}},
                "primary_risk": 0.92, "seq_len": 12, "model": "CfC-LNN"}
    monkeypatch.setattr(vigil_service, "compute_cfc_risk", _high)

    dept, uid, headers = _ward()
    pid = _patient(dept, uid)
    _vitals(pid, OVERLAY_6)

    (row,), _ = _board(headers)
    chart = vigil_service.get_patient_risk(pid)
    assert row["news2"]["raw_news2"] == chart["raw_news2"] == 6
    assert row["news2"]["level"] == chart["level"] == "critical"
    assert row["risk_level"] == "critical"


def test_an_incomplete_set_is_reported_as_incomplete():
    """A set with no ACVPU recorded scores 0 for it, so the aggregate is a floor.
    The board gets `complete: false` to say so, not a silent full score."""
    dept, uid, headers = _ward()
    pid = _patient(dept, uid)
    _vitals(pid, SPREAD_3, consciousness=None)

    (row,), _ = _board(headers)
    assert row["news2"]["raw_news2"] == 3
    assert row["news2"]["complete"] is False
    assert row["news2"]["missing_parameters"] == ["Consciousness (ACVPU)"]
