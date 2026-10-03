"""GET /api/admin/overview — the headline tiles must count what actually
happened, on EVERY lane, and must say "unknown" when they cannot know.

THE DEFECT THESE TESTS PIN (found in the 2026-08-14 pilot dry run). The
"Alert Activity" and "Records Written" tiles both counted rows that are written
at call sites which exist only on the HTTP ingest lane —
`audit_logs.action_type IN ('vigil_alert_generated','relay_alert_generated')`
and `ingestion_events` respectively, both from services/ingestion_service.py.
The simulator/bot lane (services/bot_service.py, i.e. ORB_INGESTION_MODE =
simulation|hybrid) writes neither. So on the very box the page was being shown
on, the ward read 43 active / 17 critical, the Flight Recorder streamed
vigil.alert.critical live, and two of the five headline tiles said 0.

Two invariants, and the second one matters as much as the first:

  1. A headline count comes from a source EVERY lane writes, and a lane that
     produced work appears in the breakdown by name.
  2. A number that cannot be known is reported as null and rendered as unknown.
     NEVER as 0. On an admin dashboard a zero is a clinical claim — "nothing
     happened" — and we do not make it unless we can support it.

WHY THE TESTS ASSERT THE WAY THEY DO. Each lane test also asserts that the OLD
source saw nothing for the same work. That is what makes the test able to fail:
restore the audit-log source and the count goes back to 0 while the assertion
still describes the ward correctly.
"""
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi.testclient import TestClient  # noqa: E402
from main import app  # noqa: E402
from auth import hash_password  # noqa: E402
from database import db_session  # noqa: E402
from db_backend import DBOperationalError  # noqa: E402

client = TestClient(app)

_ALERT_AUDIT_TYPES = ("vigil_alert_generated", "relay_alert_generated")


# ── fixtures / helpers ───────────────────────────────────────────────────────

def _admin_token() -> str:
    with db_session() as db:
        db.execute(
            "INSERT OR IGNORE INTO users (id, name, role, department, password_hash) "
            "VALUES (?,?,?,?,?)",
            ("OVW-ADM", "Overview Admin", "admin", "Administration",
             hash_password("password")),
        )
    r = client.post("/api/auth/login",
                    json={"employee_id": "OVW-ADM", "password": "password"})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def _overview(token: str) -> dict:
    r = client.get("/api/admin/overview",
                   headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
    return r.json()


@pytest.fixture
def patient():
    """A patient nobody else in the suite touches, discharged on teardown.

    Not DELETEd: shadow_events is append-only for every lane and audit_logs is
    hash-chained, so rows referencing this id legitimately outlive the test.
    """
    pid = f"OVW{uuid.uuid4().hex[:6].upper()}"
    with db_session() as db:
        db.execute("INSERT INTO patients (id, name, mrn) VALUES (?,?,?)",
                   (pid, "Overview Testpatient", f"MRN-{uuid.uuid4().hex[:8]}"))
    yield pid
    with db_session() as db:
        # vitals ARE removed: this module seeds thousands of them to exercise the
        # retention-cap branch, and leaving them behind would make every later
        # overview call in the session report an incomplete window.
        db.execute("DELETE FROM vitals WHERE patient_id = ?", (pid,))
        db.execute("DELETE FROM observations WHERE patient_id = ?", (pid,))
        db.execute("UPDATE patients SET status = 'discharged' WHERE id = ?", (pid,))


def _fire_simulator_alerts(pid: str, n: int, level: str = "critical") -> None:
    """Produce n Vigil alerts the way the SIMULATOR LANE produces them.

    Byte-for-byte the call bot_service.evaluate_and_dispatch_alerts makes
    (`shadow_service.arecord_batch(shadow_obs, "simulator")` — the async wrapper
    over this exact function), so the test exercises the real producer rather
    than a hand-rolled INSERT that could drift from it.
    """
    from services import shadow_service
    now = datetime.now(timezone.utc)
    observations = [{
        "patient_id": pid,
        "kind": "alert.fired",
        "occurred_at": (now - timedelta(seconds=i)).isoformat(timespec="milliseconds"),
        "trigger": "news2_threshold",
        "episode_payload": {"level": level, "raw_news2": 9},
        "payload": {"level": level, "raw_news2": 9, "score": 0.8,
                    "drivers": ["SpO2 88%"], "repeat": i > 0},
    } for i in range(n)]
    episodes = shadow_service.record_batch(observations, "simulator")
    assert episodes.get(pid), "simulator lane failed to record its alerts"


def _audit_alert_rows(pid: str) -> int:
    with db_session() as db:
        row = db.execute(
            "SELECT COUNT(*) AS c FROM audit_logs WHERE patient_id = ? "
            "AND action_type IN (?, ?)",
            (pid, *_ALERT_AUDIT_TYPES),
        ).fetchone()
    return int(row["c"])


# ═════════════════════════════════════════════════════════════════════════════
#  1. The alert tile counts the simulator/bot lane
# ═════════════════════════════════════════════════════════════════════════════

def test_alert_tile_counts_alerts_from_the_simulator_lane(patient):
    """The lane that produced the alerts is the lane the old source could not see."""
    token = _admin_token()
    before = _overview(token)["alerts"]
    assert before["known"] is True, before

    _fire_simulator_alerts(patient, 3)

    after = _overview(token)["alerts"]
    delta_total = after["total"] - before["total"]
    delta_sim = after["by_lane"].get("simulator", 0) - before["by_lane"].get("simulator", 0)

    assert delta_total == 3, (before, after)
    assert delta_sim == 3, after["by_lane"]
    assert "simulator" in after["by_lane"], "the producing lane must be named"

    # THE PROVE-CAN-FAIL ANCHOR. The pre-fix source was audit_logs
    # vigil_alert_generated/relay_alert_generated, written ONLY by
    # ingestion_service._check_ingested_alert. Three alerts just fired for this
    # patient and that source holds zero rows about them — so restoring it
    # returns the tile to 0 and delta_total above becomes 0.
    assert _audit_alert_rows(patient) == 0, (
        "the audit-log source is expected to be blind to the simulator lane; if "
        "this ever becomes non-zero, re-derive which source this tile should use"
    )
    assert after["audit_trail"]["lane"] == "ingestion"


def test_alert_tile_breaks_down_by_level(patient):
    token = _admin_token()
    before = _overview(token)["alerts"]["by_level"]
    _fire_simulator_alerts(patient, 2, level="elevated")
    after = _overview(token)["alerts"]["by_level"]
    assert after.get("elevated", 0) - before.get("elevated", 0) == 2, (before, after)


def test_alert_tile_counts_the_ingestion_lane_too(patient):
    """Fixing one lane must not lose the other. Both write shadow alert.fired."""
    from services import shadow_service
    token = _admin_token()
    before = _overview(token)["alerts"]

    episode_id, _ = shadow_service.open_episode(
        patient, "ingestion", trigger="news2_threshold",
        payload={"level": "critical", "raw_news2": 8},
    )
    assert episode_id
    shadow_service.emit("alert.fired", episode_id, "ingestion",
                        patient_id=patient,
                        payload={"level": "critical", "raw_news2": 8})

    after = _overview(token)["alerts"]
    assert after["by_lane"].get("ingestion", 0) - before["by_lane"].get("ingestion", 0) == 1
    assert after["total"] - before["total"] == 1


# ═════════════════════════════════════════════════════════════════════════════
#  2. Unknown is reported as unknown, never as zero
# ═════════════════════════════════════════════════════════════════════════════

def test_alert_tile_is_unknown_not_zero_when_the_instrument_is_off(monkeypatch, patient):
    """ORB_SHADOW_ENABLED=0 stops the recording, not the alerting.

    The ward keeps deteriorating and Vigil keeps firing; nothing writes it down.
    A 0 here would be a false clinical claim, so the contract is null + a reason.
    """
    token = _admin_token()
    _fire_simulator_alerts(patient, 2)          # real alerts exist in the window
    monkeypatch.setenv("ORB_SHADOW_ENABLED", "0")

    alerts = _overview(token)["alerts"]
    assert alerts["total"] is None, alerts
    assert alerts["known"] is False
    assert alerts["unknown_reason"], "an unknown number must carry its reason"
    assert "ORB_SHADOW_ENABLED" in alerts["unknown_reason"]
    # Belt and braces: the thing this repo rule forbids.
    assert alerts["total"] != 0
    assert alerts["by_lane"] == {}, "no lane may be claimed as zero either"


def test_alert_tile_is_unknown_when_the_shadow_log_cannot_be_read():
    """A missing view/table (migration not applied) is a cannot-know, not a zero."""
    from routes.admin_routes import _alerts_24h

    # DBOperationalError is a TUPLE of every installed driver's OperationalError
    # (db_backend._alias) — except-able, not raisable. Raise the first member;
    # that is the class the stdlib/sqlcipher connection would actually throw.
    class _Unreadable:
        def execute(self, *_a, **_k):
            raise DBOperationalError[0]("no such table: shadow_events_current")

    out = _alerts_24h(_Unreadable(), "2026-08-13T00:00:00.000+00:00")
    assert out["total"] is None and out["known"] is False
    assert out["unknown_reason"]


def test_unknown_alert_total_is_null_in_json_not_absent(monkeypatch, patient):
    """The UI branches on `total === null`; an absent key would read as undefined
    and a 0 would read as truth. The key must be present and explicitly null."""
    token = _admin_token()
    monkeypatch.setenv("ORB_SHADOW_ENABLED", "off")
    body = _overview(token)
    assert "total" in body["alerts"]
    assert body["alerts"]["total"] is None


# ═════════════════════════════════════════════════════════════════════════════
#  3. Records written — same lane problem, same contract
# ═════════════════════════════════════════════════════════════════════════════

def _seed_vitals(pid: str, n: int, source: str, minutes_ago_start: int = 1) -> None:
    now = datetime.now(timezone.utc)
    rows = [
        (pid, (now - timedelta(minutes=minutes_ago_start, seconds=i)).isoformat(),
         80.0, 120.0, 80.0, 97.0, 36.8, 16.0, "alert", source)
        for i in range(n)
    ]
    with db_session() as db:
        db.executemany(
            """INSERT INTO vitals (patient_id, timestamp, heart_rate, systolic_bp,
                   diastolic_bp, spo2, temperature, respiratory_rate, consciousness, source)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            rows,
        )


def test_records_written_counts_the_simulator_lane(patient):
    """Simulated vitals never create an ingestion_events row, which is why the
    old source reported 0 for them."""
    token = _admin_token()
    before = _overview(token)

    _seed_vitals(patient, 5, "simulated")

    after = _overview(token)
    assert after["records_written"]["complete"] is True, after["records_written"]
    delta = after["records_written"]["total"] - before["records_written"]["total"]
    assert delta == 5, (before["records_written"], after["records_written"])
    assert after["records_written"]["by_source"].get("simulated", 0) >= 5

    # PROVE-CAN-FAIL ANCHOR: the pre-fix source. `ingestion.total_vitals` is
    # summed from ingestion_events, which the simulator lane never writes — so it
    # did not move at all while five clinical rows landed in the chart.
    assert after["ingestion"]["total_vitals"] == before["ingestion"]["total_vitals"]


def test_records_written_counts_both_timestamp_formats(patient):
    """This repo stores two live timestamp formats and they do not sort together.

    Python `.isoformat()` writes 'YYYY-MM-DDTHH:MM:SS+00:00'; SQLite's
    CURRENT_TIMESTAMP default writes 'YYYY-MM-DD HH:MM:SS'. At string position 10
    that is ' ' (0x20) against 'T' (0x54), so under a plain `col >= ?` compare
    EVERY space-format row dated the same day as the cutoff sorts below it and
    silently vanishes from the count.

    The window is pinned here rather than derived from the wall clock on
    purpose: with a `now - 24h` cutoff the rows usually land on the following
    calendar date, where the broken compare happens to give the right answer, and
    the test would pass against the defect roughly 23 hours out of 24.
    """
    from routes.admin_routes import _records_written_24h

    base = (datetime.now(timezone.utc).replace(hour=6, minute=0, second=0, microsecond=0)
            - timedelta(days=3))
    cutoff = base.isoformat()                                       # ...T06:00:00+00:00
    iso_row = (base + timedelta(hours=2)).isoformat()               # ...T08:00:00+00:00
    sqlite_row = (base + timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S")   # ... 09:00:00
    assert sqlite_row < cutoff, "the premise: this row sorts BELOW the cutoff as a string"
    assert iso_row > cutoff

    with db_session() as db:
        before = _records_written_24h(db, cutoff)["counted"]
        db.execute(
            "INSERT INTO vitals (patient_id, timestamp, heart_rate, source) VALUES (?,?,?,?)",
            (patient, iso_row, 80.0, "monitor"),
        )
        db.execute(
            "INSERT INTO observations (patient_id, source, observation_name, "
            "value_text, observed_at, recorded_at) VALUES (?,?,?,?,?,?)",
            (patient, "ingested_hl7", "Sodium", "138", sqlite_row, sqlite_row),
        )
        after = _records_written_24h(db, cutoff)

    assert after["counted"] - before == 2, after
    assert after["by_source"].get("ingested_hl7", 0) >= 1, (
        "the SQLite CURRENT_TIMESTAMP row must be counted, not sorted away"
    )


def test_records_written_counts_recent_rows_through_the_route(patient):
    """The same property once more end to end, so the route's own cutoff and
    prefilter are exercised and not just the helper."""
    token = _admin_token()
    before = _overview(token)["records_written"]["counted"]
    _seed_vitals(patient, 2, "monitor")
    after = _overview(token)["records_written"]
    assert after["counted"] - before == 2, after


def test_records_written_is_unknown_not_a_floor_when_retention_pruned(patient):
    """bot_service caps retained `source='simulated'` vitals per patient, so on a
    box that has been simulating for hours the rows still present inside a 24h
    window are a FLOOR. Presenting a floor as a total is a smaller version of the
    same lie, so the total is null and the floor is carried separately."""
    from services.bot_service import MAX_VITALS_PER_PATIENT as cap

    token = _admin_token()
    # Every row inside the last hour => the retained history does not reach back
    # 24h => the prune demonstrably removed in-window rows.
    _seed_vitals(patient, cap, "simulated")

    rec = _overview(token)["records_written"]
    assert rec["total"] is None, rec
    assert rec["complete"] is False
    assert rec["counted"] >= cap, rec
    assert rec["incomplete_reason"] and str(cap) in rec["incomplete_reason"]
    assert rec["total"] != 0


def test_records_written_is_unknown_when_a_timestamp_cannot_be_parsed(patient):
    """`datetime('garbage')` is NULL and `NULL >= x` is NULL, so an unparseable
    row drops out of the count in total silence. A silently-short count is the
    same class of falsehood as a structurally-zero one — it must be declared."""
    token = _admin_token()
    _seed_vitals(patient, 2, "monitor")
    with db_session() as db:
        db.execute(
            "INSERT INTO vitals (patient_id, timestamp, heart_rate, source) VALUES (?,?,?,?)",
            # Sorts inside the prefilter window, parses as nothing.
            (patient, f"{datetime.now(timezone.utc):%Y-%m-%d}T25:99:99 not-a-time",
             80.0, "monitor"),
        )

    rec = _overview(token)["records_written"]
    assert rec["total"] is None, rec
    assert rec["complete"] is False
    assert "cannot parse" in (rec["incomplete_reason"] or "")
    assert rec["counted"] >= 2


def test_records_written_is_complete_when_history_spans_the_window(patient):
    """The unknown branch must not be sticky: a box whose retained history covers
    the window reports an exact number, not a permanent shrug."""
    token = _admin_token()
    from services.bot_service import MAX_VITALS_PER_PATIENT as cap
    # At the cap, but the oldest retained row predates the window start.
    _seed_vitals(patient, cap - 1, "simulated")
    old = (datetime.now(timezone.utc) - timedelta(hours=30)).isoformat()
    with db_session() as db:
        db.execute(
            "INSERT INTO vitals (patient_id, timestamp, heart_rate, source) VALUES (?,?,?,?)",
            (patient, old, 80.0, "simulated"),
        )

    rec = _overview(token)["records_written"]
    assert rec["complete"] is True, rec
    assert rec["total"] == rec["counted"]


# ═════════════════════════════════════════════════════════════════════════════
#  4. Contract guards
# ═════════════════════════════════════════════════════════════════════════════

def test_no_headline_number_is_zero_while_its_source_is_unknown(monkeypatch, patient):
    """The repo rule, asserted as a rule rather than per-tile: every headline
    total is either a real integer or explicitly null — never 0-standing-in-for-
    unknown."""
    token = _admin_token()
    _fire_simulator_alerts(patient, 1)
    monkeypatch.setenv("ORB_SHADOW_ENABLED", "0")
    body = _overview(token)

    for tile in ("alerts", "records_written"):
        total = body[tile]["total"]
        assert total is None or isinstance(total, int), (tile, total)
        if total is None:
            reason = body[tile].get("unknown_reason") or body[tile].get("incomplete_reason")
            assert reason, f"{tile}: an unknown total must say why"


def test_overview_requires_admin_overview_permission():
    with db_session() as db:
        db.execute(
            "INSERT OR IGNORE INTO users (id, name, role, password_hash) VALUES (?,?,?,?)",
            ("OVW-NUR", "Overview Nurse", "nurse", hash_password("password")),
        )
    r = client.post("/api/auth/login",
                    json={"employee_id": "OVW-NUR", "password": "password"})
    token = r.json()["token"]
    resp = client.get("/api/admin/overview",
                      headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 403, resp.text
