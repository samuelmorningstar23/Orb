"""Every latest-risk_scores reader must break a timestamp tie on id, and never
order by id alone.

The vitals readers got this on 2026-10-03 (tests/test_latest_vitals_tiebreak.py);
risk_scores has the same shape. idx_risk_patient is (patient_id, timestamp DESC),
so on equal timestamps `ORDER BY timestamp DESC LIMIT 1` returns the OLDER row,
and a `MAX(timestamp)` equality join returns BOTH tied rows: a surgery list then
shows the case twice and a ward distribution counts the patient once per row.
risk_scores.id is INTEGER PRIMARY KEY AUTOINCREMENT, so on an exact tie the
higher id is the later write.

Each test writes an earlier 'elevated' row, then a superseded 'stable' row, then
a 'critical' correction at the byte-identical timestamp, and asserts the reader
shows 'critical' exactly once. Relay additionally gets a back-charted row (older
timestamp, higher id), which `MAX(id)` alone got wrong.

Same-format ties only: migration 43 made every stored timestamp one spelling.
"""
import asyncio
import re
import time
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from auth import create_token
from database import db_session
from main import app
from services.clinical_time import format_utc

client = TestClient(app)

DEPT = "TieRisk Medicine"
SURG_DEPT = "TieRisk General Surgery"
ADMITTER = "D001"  # seeded by conftest

EARLIER = ("elevated", 0.4, '["Earlier driver"]')
SUPERSEDED = ("stable", 0.1, '["Superseded driver"]')
CORRECTION = ("critical", 0.9, '["Correction driver"]')


def _insert_risk(db, pid, ts, row) -> int:
    level, score, drivers = row
    return db.execute(
        "INSERT INTO risk_scores (patient_id, timestamp, score, level, drivers) "
        "VALUES (?, ?, ?, ?, ?)",
        (pid, ts, score, level, drivers),
    ).lastrowid


def _insert_vitals(db, pid, ts, hr=72):
    db.execute(
        """INSERT INTO vitals (patient_id, timestamp, heart_rate, systolic_bp,
                               diastolic_bp, spo2, temperature, respiratory_rate,
                               consciousness, on_oxygen)
           VALUES (?, ?, ?, 124, 78, 97, 36.8, 16, 'alert', 0)""",
        (pid, ts, hr),
    )


def _admit(*, name: str | None = None, risks: bool = True, vitals: int = 0) -> str:
    pid = f"TIER{uuid.uuid4().hex[:8].upper()}"
    now = datetime.now(timezone.utc)
    tie = format_utc(now - timedelta(minutes=5))
    with db_session() as db:
        db.execute(
            """INSERT INTO patients (id, name, mrn, bed, ward, diagnosis, status,
                                     age, sex, admission_date)
               VALUES (?, ?, ?, 'TR-1', ?, 'Sepsis', 'admitted', 58, 'F', ?)""",
            (pid, name or f"Tierisk {pid[-6:]}", f"MRN-{pid}", DEPT,
             format_utc(now - timedelta(days=1))),
        )
        db.execute(
            """INSERT INTO admissions (patient_id, department, ward, bed, status, admitted_by)
               VALUES (?, ?, ?, 'TR-1', 'active', ?)""",
            (pid, DEPT, DEPT, ADMITTER),
        )
        for i in range(vitals):
            _insert_vitals(db, pid, format_utc(now - timedelta(minutes=30 * (vitals - i))))
        if risks:
            _insert_risk(db, pid, format_utc(now - timedelta(hours=2)), EARLIER)
            a = _insert_risk(db, pid, tie, SUPERSEDED)
            b = _insert_risk(db, pid, tie, CORRECTION)
            assert b > a
    return pid


def _retire(pid: str) -> None:
    with db_session() as db:
        for tbl in ("vitals", "risk_scores", "patient_snapshots", "appointments",
                    "admissions"):
            db.execute(f"DELETE FROM {tbl} WHERE patient_id = ?", (pid,))
    try:
        with db_session() as db:
            db.execute("DELETE FROM patients WHERE id = ?", (pid,))
    except Exception:
        with db_session() as db:
            db.execute("UPDATE patients SET status = 'discharged' WHERE id = ?", (pid,))


def _mk_user(role: str, department: str | None) -> str:
    uid = f"TIERU{uuid.uuid4().hex[:6].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO users (id, name, role, password_hash, department) "
            "VALUES (?, 'Tierisk User', ?, 'x', ?)",
            (uid, role, department),
        )
    return uid


def _drop_user(uid: str) -> None:
    try:
        with db_session() as db:
            db.execute("DELETE FROM users WHERE id = ?", (uid,))
    except Exception:
        pass  # an audit row references it; harmless residue


@pytest.fixture
def pid():
    p = _admit()
    yield p
    _retire(p)


@pytest.fixture
def unordered_reversed(monkeypatch):
    """ORDER-BY-less results come back reversed on every connection the code
    under test opens, so a reader that was right only because SQLite walks the
    index in rowid ASC fails. Pooling off: with ORB_DB_POOL=1 the per-thread
    handle predates this patch and would never see the pragma."""
    import database
    import db_backend

    real = database._build_connection

    def _build(pooled: bool = False):
        conn = real(pooled=pooled)
        conn.execute("PRAGMA reverse_unordered_selects = ON")
        return conn

    monkeypatch.setattr(db_backend, "POOL_ENABLED", False)
    monkeypatch.setattr(database, "_build_connection", _build)


# ── the hazard itself ─────────────────────────────────────────────────────────

def test_the_fixture_reproduces_the_hazard(pid):
    with db_session() as db:
        bare = db.execute(
            "SELECT level FROM risk_scores WHERE patient_id = ? "
            "ORDER BY timestamp DESC LIMIT 1", (pid,)).fetchone()
        fixed = db.execute(
            "SELECT level FROM risk_scores WHERE patient_id = ? "
            "ORDER BY timestamp DESC, id DESC LIMIT 1", (pid,)).fetchone()
        joined = db.execute(
            """SELECT r.level FROM risk_scores r
               JOIN (SELECT patient_id, MAX(timestamp) AS mx FROM risk_scores
                      GROUP BY patient_id) l
                 ON l.patient_id = r.patient_id AND l.mx = r.timestamp
               WHERE r.patient_id = ?""", (pid,)).fetchall()
    assert bare["level"] == "stable"
    assert fixed["level"] == "critical"
    assert len(joined) == 2  # the equality join returns the tie twice


# ── ORDER BY readers ──────────────────────────────────────────────────────────

def test_sage_ambient_context_reads_the_correction(pid):
    from services.sage_ambient_service import _fetch_patient_context

    assert _fetch_patient_context(pid)["risk_level"] == "critical"


def test_discharge_prompt_states_the_correction_band(pid, monkeypatch):
    from services import discharge_service
    from services.llm_queue_service import llm_queue

    sent = []

    class _Resp:
        def json(self):
            return {"message": {"content": "DISCHARGE SUMMARY"}}

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, **k):
            sent.append(json)
            return _Resp()

    async def _acquire(*a, **k):
        return None

    monkeypatch.setattr(discharge_service.httpx, "AsyncClient", _Client)
    monkeypatch.setattr(llm_queue, "acquire", _acquire)
    monkeypatch.setattr(llm_queue, "release", lambda *a, **k: None)

    asyncio.run(discharge_service.generate_discharge_summary(pid))
    prompt = sent[0]["messages"][-1]["content"]
    line = next(ln for ln in prompt.splitlines() if "NEWS2 band" in ln)
    assert line.rstrip().endswith("critical"), line


def test_discharge_timeline_lists_tied_alerts_in_write_order():
    """ASC reader: two critical rows at one timestamp print in write order."""
    from services.discharge_service import build_patient_timeline

    p = _admit(risks=False)
    try:
        tie = format_utc(datetime.now(timezone.utc) - timedelta(minutes=5))
        with db_session() as db:
            _insert_risk(db, p, tie, ("critical", 0.8, '["First write"]'))
            _insert_risk(db, p, tie, ("critical", 0.9, '["Second write"]'))
        alerts = [e["detail"] for e in build_patient_timeline(p) if e["type"] == "alert"]
        assert alerts[-2:] == ["First write", "Second write"], alerts
    finally:
        _retire(p)


def test_sage_fresh_risk_block_escalates_from_the_correction():
    """Benign vitals score stable; the stored band may only raise it, and the
    stored band that counts is the correction's."""
    from services.sage_service import _fresh_risk_block

    p = _admit(vitals=1)
    try:
        with db_session() as db:
            block = _fresh_risk_block(db, p)
        assert block["level"] == "critical", block
    finally:
        _retire(p)


def test_sage_patient_context_seeds_the_correction(pid):
    """No vitals at all: the stored row is the whole risk block."""
    from services.sage_service import _assemble_patient_context

    with db_session() as db:
        ctx = _assemble_patient_context(db, pid, None)
    assert ctx["risk"]["level"] == "critical", ctx["risk"]


def test_forecast_features_carry_the_correction_score():
    from services.forecast_service import extract_patient_features

    p = _admit(vitals=3)
    try:
        features = extract_patient_features(p, seq_len=48)
        assert features is not None
        assert float(features[0, -1, 18]) == pytest.approx(0.9)
    finally:
        _retire(p)


def test_forecast_appointment_acuity_is_the_correction(pid):
    """Since 2026-10-06 the appointment badge is the NEWS2 of the latest VITALS
    row (the Vigil board's rule), not risk_scores, so the tie this file is about
    is now a vitals tie: a benign set and a peri-arrest correction charted at
    the same time, the correction the higher id. The risk rows the fixture
    wrote are ignored."""
    from services.forecast_service import get_appointments
    from services.hospital_time import hospital_today

    ts = format_utc(datetime.now(timezone.utc) - timedelta(minutes=5))
    with db_session() as db:
        _insert_vitals(db, pid, ts)
        db.execute(
            """INSERT INTO vitals (patient_id, timestamp, heart_rate, systolic_bp,
                                   diastolic_bp, spo2, temperature, respiratory_rate,
                                   consciousness, on_oxygen)
               VALUES (?, ?, 135, 85, 50, 88, 39.5, 30, 'alert', 0)""",
            (pid, ts),
        )
        db.execute(
            """INSERT INTO appointments (id, patient_id, booked_by_user_id, scheduled_date,
                   scheduled_time, appointment_type, reason, status, origin, department)
               VALUES (?, ?, ?, ?, '10:00', 'Follow-up', 'Review', 'booked', 'staff', ?)""",
            (str(uuid.uuid4()), pid, ADMITTER,
             (hospital_today() + timedelta(days=1)).isoformat(), DEPT),
        )
    mine = [a for a in get_appointments(days_ahead=3) if a["patient_id"] == pid]
    assert [a["acuity"] for a in mine] == ["critical"]


def test_world_model_current_level_is_the_correction(pid):
    from services.world_model_service import compute_patient_state

    state = compute_patient_state(pid)
    assert state["current_risk_level"] == "critical"
    assert state["risk_level_history"][:2] == ["critical", "stable"]


def test_downtime_pack_lists_the_correction_first(pid):
    from services.downtime_service import capture_snapshot

    snap = capture_snapshot(pid, created_by="test")["snapshot"]
    assert snap["recent_risk_scores"][0]["level"] == "critical"


def test_voice_fallback_risk_line_is_the_correction(pid):
    """No vitals, so voice falls back to the stored row."""
    from services.voice_agent_service import build_patient_context

    ctx = build_patient_context(pid, {"id": ADMITTER, "role": "doctor"})
    assert "critical" in ctx.lower(), ctx
    assert "Superseded driver" not in ctx, ctx


def test_vigil_latest_risk_and_history_start_on_the_correction(pid):
    from routes.vigil_routes import _load_latest_risk, _load_patient_bundle

    assert _load_latest_risk(pid)["level"] == "critical"
    risk_history = _load_patient_bundle(pid)[4]
    assert risk_history[0]["level"] == "critical"


def test_bot_severity_profile_reads_the_correction(pid):
    from services import bot_service

    bot_service._severity_cache.pop(pid, None)
    with db_session() as db:
        assert bot_service._get_patient_severity(db, pid) == "critical"
    bot_service._severity_cache.pop(pid, None)


def test_bot_lab_picker_sees_each_patient_once_at_the_correction(pid, monkeypatch):
    """The LEFT JOIN returned one row per risk row, so a patient's lab odds grew
    with their history length and the weight came from an arbitrary row."""
    from services import bot_service

    seen = {}

    class _Stop(Exception):
        pass

    def _choices(population, weights=None, k=1):
        seen["rows"] = [(p["id"], w) for p, w in zip(population, weights)]
        raise _Stop

    monkeypatch.setattr(bot_service.random, "choices", _choices)
    bot_service._lab_cooldown.pop(pid, None)
    with pytest.raises(_Stop):
        bot_service._generate_lab_result(time.time())
    assert [w for p, w in seen["rows"] if p == pid] == [3], seen["rows"]


# ── equality joins: one row per patient, the correction's ─────────────────────

def _dist(text: str) -> dict:
    """The briefing omits the line when no admitted patient has a risk row."""
    m = re.search(r"Risk distribution: critical (\d+), elevated (\d+), stable (\d+)", text)
    if not m:
        assert "Risk distribution" not in text, text
        return {"critical": 0, "elevated": 0, "stable": 0}
    return {"critical": int(m[1]), "elevated": int(m[2]), "stable": int(m[3])}


def test_sage_aggregate_distribution_counts_the_patient_once():
    from services.sage_service import build_aggregate_context

    before = _dist(build_aggregate_context("how many critical patients"))
    p = _admit()
    try:
        after = _dist(build_aggregate_context("how many critical patients"))
    finally:
        _retire(p)
    assert after["critical"] - before["critical"] == 1, (before, after)
    assert after["stable"] == before["stable"], (before, after)
    assert after["elevated"] == before["elevated"], (before, after)


def test_sage_narrator_snapshot_counts_the_patient_once():
    """Retargeted 2026-10-03: the ward brief bands from the latest VITALS row,
    the board's moment (test_ward_brief_latest_vitals.py), not risk_scores. A
    critical set and a same-timestamp correction to a benign one: the patient
    lands once, in the correction's band, whatever the tied risk rows say (their
    correction is 'critical' here, so a brief still reading them goes red)."""
    from services.sage_narrator_service import WardNarrator

    before = WardNarrator._build_ward_snapshot()["risk_distribution"]
    p = _admit()
    try:
        tie = format_utc(datetime.now(timezone.utc) - timedelta(minutes=1))
        with db_session() as db:
            # HR 135 (3) + RR 26 (3) + SpO2 91 (3) = NEWS2 9, critical.
            db.execute(
                """INSERT INTO vitals (patient_id, timestamp, heart_rate, systolic_bp,
                                       diastolic_bp, spo2, temperature, respiratory_rate,
                                       consciousness, on_oxygen)
                   VALUES (?, ?, 135, 118, 75, 91, 37.0, 26, 'alert', 0)""",
                (p, tie),
            )
            _insert_vitals(db, p, tie)  # the correction: NEWS2 0, the higher id
        after = WardNarrator._build_ward_snapshot()["risk_distribution"]
    finally:
        _retire(p)
    assert after["stable"] - before["stable"] == 1, (before, after)
    assert after["critical"] == before["critical"], (before, after)
    assert after["elevated"] == before["elevated"], (before, after)


def test_sage_census_line_shows_the_correction_band(unordered_reversed):
    from services.sage_service import get_relevant_context

    uid = _mk_user("doctor", DEPT)
    p = _admit()
    try:
        user = {"id": uid, "role": "doctor", "department": DEPT, "name": "Tierisk User"}
        context, *_ = get_relevant_context("give me an overview of all my patients", user)
        lines = [ln for ln in context.splitlines() if f"Tierisk {p[-6:]}" in ln]
        assert len(lines) == 1, lines
        assert "CRITICAL" in lines[0] and "STABLE" not in lines[0], lines[0]
    finally:
        _retire(p)
        _drop_user(uid)


@pytest.fixture
def surgery_world():
    surgeon = _mk_user("doctor", SURG_DEPT)
    p = _admit()
    sid = f"TIERS{uuid.uuid4().hex[:8].upper()}"
    from services.hospital_time import hospital_today
    with db_session() as db:
        db.execute(
            """INSERT INTO surgeries (id, patient_id, surgeon_id, procedure_name, diagnosis,
                                      scheduled_date, scheduled_time, status)
               VALUES (?, ?, ?, 'Laparotomy', 'Perforation', ?, '09:00', 'scheduled')""",
            (sid, p, surgeon, hospital_today().isoformat()),
        )
    yield {"surgeon": surgeon, "pid": p, "sid": sid}
    with db_session() as db:
        db.execute("DELETE FROM surgery_team_assignments WHERE surgery_id = ?", (sid,))
        db.execute("DELETE FROM surgeries WHERE id = ?", (sid,))
    _retire(p)
    _drop_user(surgeon)


def test_surgery_service_lists_the_case_once_at_the_correction(surgery_world):
    from services.surgery_service import get_surgery_detail, get_weekly_schedule

    w = surgery_world
    rows = get_weekly_schedule(w["surgeon"], days=2)
    flat = [s for day in rows for s in (day.get("surgeries", [day]) if isinstance(day, dict) else [day])]
    mine = [s for s in flat if s.get("id") == w["sid"]]
    assert [s["risk_level"] for s in mine] == ["critical"], mine
    assert get_surgery_detail(w["sid"])["risk_level"] == "critical"


def test_surgery_routes_list_the_case_once_at_the_correction(surgery_world):
    """The schedule's band is the NEWS2 of the latest VITALS row since
    2026-10-06 (the Vigil board's rule), so its tie is a vitals tie: a benign
    set and a peri-arrest correction at the same time, the correction the
    higher id. The detail route below still reads the risk rows."""
    w = surgery_world
    tie = format_utc(datetime.now(timezone.utc) - timedelta(minutes=5))
    with db_session() as db:
        _insert_vitals(db, w["pid"], tie)
        db.execute(
            """INSERT INTO vitals (patient_id, timestamp, heart_rate, systolic_bp,
                                   diastolic_bp, spo2, temperature, respiratory_rate,
                                   consciousness, on_oxygen)
               VALUES (?, ?, 135, 85, 50, 88, 39.5, 30, 'alert', 0)""",
            (w["pid"], tie),
        )
    headers = {"Authorization": f"Bearer {create_token(w['surgeon'], 'doctor')}"}
    resp = client.get("/api/surgery/schedule?days=2", headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    days = body.get("schedule", body) if isinstance(body, dict) else body
    mine = [s for d in days for s in d["surgeries"] if s["id"] == w["sid"]]
    assert [s["risk_level"] for s in mine] == ["critical"], mine

    detail = client.get(f"/api/surgery/{w['sid']}", headers=headers)
    assert detail.status_code == 200, detail.text
    d = detail.json()
    d = d.get("surgery", d)
    assert d["risk_level"] == "critical", d


def test_patient_search_returns_the_patient_once_at_the_correction():
    uid = _mk_user("doctor", DEPT)
    name = f"Tiesearch {uuid.uuid4().hex[:6]}"
    p = _admit(name=name)
    try:
        headers = {"Authorization": f"Bearer {create_token(uid, 'doctor')}"}
        resp = client.get("/api/search/patients", params={"q": name}, headers=headers)
        assert resp.status_code == 200, resp.text
        mine = [r for r in resp.json() if r["id"] == p]
        assert [r["risk_level"] for r in mine] == ["critical"], mine
    finally:
        _retire(p)
        _drop_user(uid)


# ── Relay: MAX(id) alone was the wrong key ────────────────────────────────────

def _relay_level(p: str) -> list:
    from services.relay_service import get_channels_for_user

    uid = _mk_user("doctor", DEPT)
    cid = f"chan_tier_{uuid.uuid4().hex[:10]}"
    try:
        with db_session() as db:
            db.execute(
                "INSERT INTO channels (id, name, type, patient_id) "
                "VALUES (?, 'Tierisk Case Room', 'case_room', ?)",
                (cid, p),
            )
            db.execute(
                "INSERT INTO channel_members (channel_id, user_id) VALUES (?, ?)",
                (cid, uid),
            )
        return [c["risk_level"] for c in get_channels_for_user(uid, "doctor")
                if c["id"] == cid]
    finally:
        with db_session() as db:
            db.execute("DELETE FROM channel_members WHERE channel_id = ?", (cid,))
            db.execute("DELETE FROM channels WHERE id = ?", (cid,))
        _drop_user(uid)


def test_relay_case_room_shows_the_correction(pid):
    assert _relay_level(pid) == ["critical"]


def test_relay_case_room_ignores_a_back_charted_row():
    """A back-charted obs set writes a risk row with an OLDER timestamp and a
    HIGHER id. It is history, not the current score."""
    p = _admit(risks=False)
    try:
        now = datetime.now(timezone.utc)
        with db_session() as db:
            _insert_risk(db, p, format_utc(now - timedelta(minutes=5)), CORRECTION)
            _insert_risk(db, p, format_utc(now - timedelta(hours=1)), SUPERSEDED)
        assert _relay_level(p) == ["critical"]
    finally:
        _retire(p)
