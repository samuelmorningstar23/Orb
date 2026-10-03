"""GET /api/admin/alerts lists the alerts the Overview tile counts.

The Alerts tab read audit_logs category 'ingestion', which only the HTTP ingest
lane writes, so on a simulator box it said "No alert events" while the Overview
counted 68 alerts in the same 24 hours. The list now reads the tile's own source
(shadow_events_current, kind alert.fired), pages on the server, names the lane,
and says "unknown" rather than "none" when the instrument is off.
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from auth import create_token
from database import db_session
from main import app
from services import shadow_service

client = TestClient(app)

DEPT_A = "Admin Alerts Cardiology"
DEPT_B = "Admin Alerts Oncology"


def _auth(uid, role="admin"):
    return {"Authorization": f"Bearer {create_token(uid, role)}"}


def _admin(dept=None):
    uid = f"AAL{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute("INSERT INTO users (id, name, role, password_hash, department) "
                   "VALUES (?, 'Admin Alerts', 'admin', 'x', ?)", (uid, dept))
    return uid


def _patient(dept):
    pid = f"AALP{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute("INSERT OR IGNORE INTO users (id, name, role, password_hash) "
                   "VALUES ('AALADM', 'Admin Alerts Admitter', 'doctor', 'x')")
        db.execute("INSERT INTO patients (id, name, mrn, status) "
                   "VALUES (?, 'Admin Alerts Fixture', ?, 'discharged')", (pid, f"MRN-{pid}"))
        db.execute("INSERT INTO admissions (id, patient_id, department, ward, status, admitted_by) "
                   "VALUES (?, ?, ?, ?, 'discharged', 'AALADM')", (f"ADM-{pid}", pid, dept, dept))
    return pid


def _fire(pid, n, level="critical", lane="simulator"):
    """The simulator lane's own producer, as bot_service calls it."""
    now = datetime.now(timezone.utc)
    episodes = shadow_service.record_batch([{
        "patient_id": pid,
        "kind": "alert.fired",
        "occurred_at": (now - timedelta(seconds=i)).isoformat(timespec="milliseconds"),
        "trigger": "news2_threshold",
        "episode_payload": {"level": level, "raw_news2": 8},
        "payload": {"level": level, "raw_news2": 8, "score": 0.8, "repeat": i > 0},
    } for i in range(n)], lane)
    assert episodes.get(pid), "the simulator lane failed to record its alerts"


def _alerts(uid, **params):
    resp = client.get("/api/admin/alerts", headers=_auth(uid), params=params)
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_simulator_alerts_are_listed_with_lane_level_and_patient():
    admin, pid = _admin(), _patient(DEPT_A)
    _fire(pid, 3)

    body = _alerts(admin, limit=200)

    mine = [a for a in body["alerts"] if a["patient_id"] == pid]
    assert len(mine) == 3
    assert {a["lane"] for a in mine} == {"simulator"}
    assert {a["level"] for a in mine} == {"critical"}
    assert {a["raw_news2"] for a in mine} == {8}
    assert sum(1 for a in mine if a["repeat"]) == 2
    assert body["known"] is True and body["by_lane"].get("simulator", 0) >= 3


def test_the_list_total_matches_the_overview_tile():
    admin, pid = _admin(), _patient(DEPT_A)
    _fire(pid, 2)

    overview = client.get("/api/admin/overview", headers=_auth(admin)).json()["alerts"]
    listed = _alerts(admin)

    assert listed["total"] == overview["total"]
    assert listed["by_lane"] == overview["by_lane"]


def test_paging_and_filters_run_on_the_server():
    admin, pid = _admin(), _patient(DEPT_A)
    _fire(pid, 3, level="elevated")

    page = _alerts(admin, limit=2, offset=0, level="elevated", lane="simulator")
    assert len(page["alerts"]) == 2
    assert page["total"] >= 3
    assert all(a["level"] == "elevated" and a["lane"] == "simulator" for a in page["alerts"])
    nxt = _alerts(admin, limit=2, offset=2, level="elevated", lane="simulator")
    assert {a["id"] for a in page["alerts"]}.isdisjoint({a["id"] for a in nxt["alerts"]})


def test_instrument_off_is_unknown_not_an_empty_list(monkeypatch):
    admin = _admin()
    monkeypatch.setenv("ORB_SHADOW_ENABLED", "0")

    body = _alerts(admin)

    assert body["known"] is False
    assert body["total"] is None
    assert body["alerts"] == []
    assert "unknown, not zero" in body["unknown_reason"]


def test_a_ward_scoped_admin_sees_their_ward_and_the_house_count():
    pid_a, pid_b = _patient(DEPT_A), _patient(DEPT_B)
    _fire(pid_a, 1)
    _fire(pid_b, 2)
    scoped = _admin(DEPT_A)

    body = _alerts(scoped, limit=200)

    ids = {a["patient_id"] for a in body["alerts"]}
    assert pid_a in ids and pid_b not in ids
    assert body["scope"] == DEPT_A
    assert body["house_total"] >= body["total"] + 2


def test_a_page_of_named_alerts_is_audited():
    admin, pid = _admin(), _patient(DEPT_A)
    _fire(pid, 1)

    def _count():
        with db_session() as db:
            return db.execute(
                "SELECT COUNT(*) c FROM audit_logs WHERE event_category = 'phi_access' "
                "AND action_type = 'admin_alerts_viewed' AND actor_user_id = ?",
                (admin,)).fetchone()["c"]

    before = _count()
    _alerts(admin)
    assert _count() == before + 1


@pytest.mark.parametrize("params", [{"hours": 0}, {"hours": 24 * 8}, {"limit": 500}])
def test_out_of_range_windows_are_refused(params):
    resp = client.get("/api/admin/alerts", headers=_auth(_admin()), params=params)
    assert resp.status_code == 422
