"""Review of the Orb Ward Record entry lane (6 October 2026).

Two holes the lane's own tests could not show:

1. With ORB_SCORING_ENABLED off the observation write raised nothing, but the
   escalation safety net (escalation_service.scan_new_observations, run by the
   background sweep every minute and on every escalation board read) scored the
   same row a minute later and opened a chain, with its Relay post. Scoring off
   now means the sweep raises nothing either.

2. "Every latest, window and trend reader skips a voided set" was a list kept
   by hand, and three readers added by other changes the same evening
   (Command Center, the surgical day list, appointments) were missing from it
   until each was fixed by hand. The structural test below reads every SQL
   statement in services/ and routes/ that selects from vitals and fails on one
   that neither filters voided_at nor is named in DELIBERATELY_UNFILTERED with
   its reason.
"""
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import config
from auth import create_token
from database import db_session
from routes.nurse_routes import router as nurse_router

DEPT = "Ward Record Review Medicine"

# NEWS2 12: RR 26 (3), SpO2 90 (3), SBP 95 (2), HR 125 (2), temp 39.2 (2).
_NEWS2_12 = {"heart_rate": 125, "systolic_bp": 95, "diastolic_bp": 60,
             "respiratory_rate": 26, "temperature": 39.2, "spo2": 90,
             "consciousness": "alert", "on_oxygen": False}


@pytest.fixture()
def client():
    app = FastAPI()
    app.include_router(nurse_router)
    return TestClient(app)


def _auth(uid, role):
    return {"Authorization": f"Bearer {create_token(uid, role)}"}


def _mk_user(role, department):
    uid = f"WRR{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO users (id, name, role, password_hash, department) "
            "VALUES (?, ?, ?, 'x', ?)",
            (uid, f"Ward Record Review {role}", role, department),
        )
    return uid


def _mk_patient(department):
    pid = f"WRRP{uuid.uuid4().hex[:8].upper()}"
    now = datetime.now(timezone.utc).isoformat()
    with db_session() as db:
        db.execute(
            "INSERT OR IGNORE INTO users (id, name, role, password_hash) "
            "VALUES ('WRRADM', 'Ward Record Review Admitter', 'doctor', 'x')"
        )
        db.execute(
            """INSERT INTO patients (id, name, mrn, bed, ward, age, sex, diagnosis,
                                     status, admission_date)
               VALUES (?, ?, ?, 'B-4', 'WRR-1', 66, 'F', 'Ward record review fixture',
                       'admitted', ?)""",
            (pid, f"WRR {department}", f"MRN-{uuid.uuid4().hex[:8].upper()}", now),
        )
        db.execute(
            "INSERT INTO admissions (patient_id, department, ward, bed, status, admitted_by) "
            "VALUES (?, ?, 'WRR-1', 'B-4', 'active', 'WRRADM')",
            (pid, department),
        )
    return pid


def _chains(pid):
    with db_session() as db:
        return db.execute(
            "SELECT COUNT(*) FROM clinical_escalations WHERE patient_id = ?", (pid,)
        ).fetchone()[0]


@pytest.fixture()
def quiet_relay(monkeypatch):
    """The chain row is what is asserted; the Relay post is not needed for it."""
    from services import escalation_service
    monkeypatch.setattr(escalation_service, "_relay_notify_enabled", lambda: False)


# ── 1. Scoring off: the safety net raises nothing either ─────────────────────

def test_scoring_off_the_escalation_sweep_raises_nothing_from_a_clerk_set(
        client, monkeypatch, quiet_relay):
    from services import escalation_service
    monkeypatch.setattr(config, "SCORING_ENABLED", False)
    dept = f"{DEPT} {uuid.uuid4().hex[:6]}"
    pid = _mk_patient(dept)
    clerk = _mk_user("ward_clerk", dept)
    sheet_time = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
    r = client.post(f"/api/nurse/patients/{pid}/observations",
                    json={**_NEWS2_12, "taken_at": sheet_time},
                    headers=_auth(clerk, "ward_clerk"))
    assert r.status_code == 200, r.text
    assert r.json()["news2"] is None

    assert escalation_service.scan_new_observations(department=dept) == []
    assert escalation_service.sweep(department=dept)["raised"] == 0
    assert _chains(pid) == 0


def test_scoring_off_evaluate_observation_opens_no_chain(monkeypatch, quiet_relay):
    from services import escalation_service
    from services.vigil_service import compute_news2
    monkeypatch.setattr(config, "SCORING_ENABLED", False)
    pid = _mk_patient(DEPT)
    news2 = compute_news2(125, 95, 26, 39.2, 90, "alert", False)
    assert escalation_service.evaluate_observation(pid, news2) is None
    assert _chains(pid) == 0


def test_scoring_on_the_same_set_is_still_caught_by_the_sweep(monkeypatch, quiet_relay):
    """The control: with scoring on, a NEWS2 12 row that no write path
    dispatched is raised by the safety net, so the test above is not passing
    only because the sweep finds nothing to do."""
    from services import escalation_service
    assert config.SCORING_ENABLED
    dept = f"{DEPT} {uuid.uuid4().hex[:6]}"
    pid = _mk_patient(dept)
    at = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
    with db_session() as db:
        db.execute(
            """INSERT INTO vitals (patient_id, timestamp, heart_rate, systolic_bp,
                                   diastolic_bp, spo2, temperature, respiratory_rate,
                                   consciousness, on_oxygen, source)
               VALUES (?, ?, 125, 95, 60, 90, 39.2, 26, 'alert', 0, 'clerk_transcribed')""",
            (pid, at),
        )
    assert escalation_service.sweep(department=dept)["raised"] == 1
    assert _chains(pid) == 1


# ── 2. Every vitals reader skips a voided set, or says why not ───────────────

_SERVER = Path(__file__).resolve().parent.parent

# file (relative to server/) -> substrings, each naming one statement left
# unfiltered on purpose, with the reason.
DELIBERATELY_UNFILTERED: dict[str, dict[str, str]] = {
    "services/bot_service.py": {
        "DELETE FROM vitals": "the simulator prune deletes old simulated rows; not a read",
        "FROM vitals WHERE source = 'simulated'": "inner select of the same prune",
    },
    "services/nurse_shift_service.py": {
        "WHERE patient_id = ? AND source = ? AND timestamp = ?":
            "the replay check: a queued resend of a set that was struck out returns "
            "replayed instead of charting the error a second time",
        "SELECT patient_id FROM vitals WHERE id = ?":
            "the void route's scope lookup by id",
        "SELECT id, patient_id, timestamp, source, voided_at FROM vitals WHERE id = ?":
            "the void itself, which must see the row to refuse a second void",
    },
    "services/nervous_system_triggers.py": {
        "SELECT * FROM vitals WHERE id = ?":
            "by id, already gated on is_latest_vitals_row, which skips voided sets",
    },
    "services/ingestion_service.py": {
        "source_event_id = ?": "wire dedupe by event id, not a clinical read",
    },
    "services/impact_service.py": {
        "SELECT v.source FROM vitals v":
            "names the feed that produced a historical alert; history, not latest",
    },
    "services/shadow_scorecard_service.py": {
        "MAX(timestamp) AS last_at FROM vitals":
            "the pilot evidence instrument, left as it was by the lane (see its report)",
    },
    "routes/admin_routes.py": {
        "AS bad_ts": "feed health write counts per source, not a clinical read",
        "WHERE source = 'simulated'": "counts simulated patients for feed health",
    },
}

_SQL_READ = re.compile(r"\b(FROM|JOIN)\s+vitals\b")


def _statements(text: str):
    """(line number, statement text) for each SQL statement that names vitals.

    A statement is approximated as the 3 lines before the match and the 7
    after it: enough to see a WHERE clause written on the lines around FROM,
    and short enough that a neighbouring statement rarely lends it a filter."""
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if _SQL_READ.search(line):
            yield i + 1, "\n".join(lines[max(0, i - 3):i + 8])


def test_every_vitals_reader_skips_voided_sets_or_says_why():
    offenders = []
    for folder in ("services", "routes"):
        for path in sorted((_SERVER / folder).rglob("*.py")):
            rel = path.relative_to(_SERVER).as_posix()
            text = path.read_text(encoding="utf-8")
            allowed = DELIBERATELY_UNFILTERED.get(rel, {})
            lines = text.split("\n")
            for lineno, window in _statements(text):
                line = lines[lineno - 1]
                if "voided_at" in window:
                    continue
                if any(key in line or key in window for key in allowed):
                    continue
                offenders.append(f"{rel}:{lineno}: {line.strip()}")
    assert not offenders, (
        "These read vitals without skipping sets marked entered in error "
        "(add AND voided_at IS NULL, or name them in DELIBERATELY_UNFILTERED "
        "with the reason):\n" + "\n".join(offenders))
