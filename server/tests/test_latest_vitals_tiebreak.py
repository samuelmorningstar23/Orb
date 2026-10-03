"""Every latest-vitals reader shown beside a NEWS2 must break a timestamp tie on id.

idx_vitals_patient is (patient_id, timestamp DESC). On equal timestamps SQLite
walks it in rowid ASC, so `ORDER BY timestamp DESC LIMIT 1` without `, id DESC`
deterministically returns the OLDER row: a nurse's benign set over the
peri-arrest correction charted at the same bedside time. vitals.id is INTEGER
PRIMARY KEY AUTOINCREMENT, so on an exact tie the higher id is the later write.
Timestamp stays the primary key everywhere: id order is not observation order
(a back-charted or replayed reading is inserted later with an older time).

Each test charts an earlier set, then a benign set, then the correction at the
byte-identical timestamp string, and asserts the reader shows the correction's
ABSOLUTE values (HR 142, SBP 84, RR 31, SpO2 86) or the NEWS2 computed from them.

Same-format ties only. A tiebreak cannot fix rows stored in mixed timestamp
formats: there the ISO row wins on the primary key before the tiebreak runs.
That is the timestamp-format work, and nothing here claims to cover it.
"""
import asyncio
import re
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from database import db_session
from services.vigil_service import compute_news2

EARLIER = dict(heart_rate=80, systolic_bp=118, diastolic_bp=74, spo2=96,
               temperature=37.1, respiratory_rate=18)
BENIGN = dict(heart_rate=72, systolic_bp=124, diastolic_bp=78, spo2=97,
              temperature=36.8, respiratory_rate=16)
CORRECTION = dict(heart_rate=142, systolic_bp=84, diastolic_bp=48, spo2=86,
                  temperature=35.0, respiratory_rate=31)


def _news2(v: dict) -> int:
    return compute_news2(v["heart_rate"], v["systolic_bp"], v["respiratory_rate"],
                         v["temperature"], v["spo2"], "alert", False)["raw_news2"]


CORRECTION_NEWS2 = _news2(CORRECTION)
BENIGN_NEWS2 = _news2(BENIGN)
assert CORRECTION_NEWS2 != BENIGN_NEWS2, "the two sets must score differently"

DEPT = "Tiebreak Test Medicine"
ADMITTER = "D001"  # seeded by conftest


def _insert_vitals(db, pid: str, ts: str, v: dict) -> int:
    cur = db.execute(
        """INSERT INTO vitals (patient_id, timestamp, heart_rate, systolic_bp,
                               diastolic_bp, spo2, temperature, respiratory_rate,
                               consciousness, on_oxygen)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'alert', 0)""",
        (pid, ts, v["heart_rate"], v["systolic_bp"], v["diastolic_bp"], v["spo2"],
         v["temperature"], v["respiratory_rate"]),
    )
    return cur.lastrowid


def _admit(*, user_id: str | None = None) -> str:
    """An admitted patient (with an admission row, which carries the department)
    whose chart ends in a same-timestamp benign set then correction."""
    pid = f"TIEB{uuid.uuid4().hex[:8].upper()}"
    now = datetime.now(timezone.utc)
    tie = (now - timedelta(minutes=5)).isoformat()
    earlier = (now - timedelta(hours=2)).isoformat()
    with db_session() as db:
        db.execute(
            """INSERT INTO patients (id, name, mrn, bed, ward, diagnosis, status,
                                     age, sex, admission_date, user_id)
               VALUES (?, ?, ?, 'TB-1', ?, 'Sepsis', 'admitted', 64, 'M', ?, ?)""",
            (pid, f"Tiebreak {pid[-4:]}", f"MRN-{pid}", DEPT, earlier, user_id),
        )
        db.execute(
            """INSERT INTO admissions (patient_id, department, ward, bed, status, admitted_by)
               VALUES (?, ?, ?, 'TB-1', 'active', ?)""",
            (pid, DEPT, DEPT, ADMITTER),
        )
        _insert_vitals(db, pid, earlier, EARLIER)
        benign_id = _insert_vitals(db, pid, tie, BENIGN)
        correction_id = _insert_vitals(db, pid, tie, CORRECTION)
    assert correction_id > benign_id
    return pid


def _retire(pid: str) -> None:
    with db_session() as db:
        for tbl in ("vitals", "risk_scores", "patient_snapshots", "admissions"):
            db.execute(f"DELETE FROM {tbl} WHERE patient_id = ?", (pid,))
    try:
        with db_session() as db:
            db.execute("DELETE FROM patients WHERE id = ?", (pid,))
    except Exception:
        # Some reader wrote a child row this cleanup does not know about. Leave
        # the patient out of every admitted census instead.
        with db_session() as db:
            db.execute("UPDATE patients SET status = 'discharged' WHERE id = ?", (pid,))


@pytest.fixture
def pid():
    p = _admit()
    yield p
    _retire(p)


@pytest.fixture
def unordered_reversed(monkeypatch):
    """Every connection the code under test opens returns ORDER-BY-less results
    in reverse. A MAX(timestamp) equality join yields BOTH tied rows, and a
    per-patient dict that keeps "whichever came last" was correct only because
    SQLite happens to walk the index in rowid ASC. With this on, that luck runs
    the other way, so a reader with no explicit tie rule fails here.

    Pooling is switched off for the test: with ORB_DB_POOL=1 (the shipping .env)
    get_db hands back a per-thread handle opened before this patch, and the
    pragma would never reach it."""
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
    """Without a tiebreak SQLite returns the superseded benign set. If this ever
    stops being true the other tests here prove nothing, so it is asserted."""
    with db_session() as db:
        bare = db.execute(
            "SELECT heart_rate FROM vitals WHERE patient_id = ? ORDER BY timestamp DESC LIMIT 1",
            (pid,),
        ).fetchone()
        fixed = db.execute(
            "SELECT heart_rate FROM vitals WHERE patient_id = ? "
            "ORDER BY timestamp DESC, id DESC LIMIT 1",
            (pid,),
        ).fetchone()
    assert bare["heart_rate"] == BENIGN["heart_rate"]
    assert fixed["heart_rate"] == CORRECTION["heart_rate"]


# ── beside a NEWS2 ────────────────────────────────────────────────────────────

def test_downtime_paper_pack_prints_and_scores_the_correction(pid):
    from services.downtime_service import capture_snapshot

    snap = capture_snapshot(pid, created_by="test")["snapshot"]
    lv = snap["latest_vitals"]
    assert (lv["heart_rate"], lv["systolic_bp"], lv["respiratory_rate"], lv["spo2"]) == (
        142, 84, 31, 86), lv
    assert snap["news2"]["raw_news2"] == CORRECTION_NEWS2


def test_discharge_summary_prompt_carries_the_correction(pid, monkeypatch):
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
    line = next(ln for ln in prompt.splitlines() if ln.startswith("Latest vitals:"))
    assert "HR 142" in line and "BP 84/48" in line and "RR 31" in line, line
    assert "SpO2 86%" in line, line


def test_command_center_deterioration_scores_the_correction(pid):
    from services.command_center_service import _deteriorations

    out = _deteriorations([{"id": pid, "level": "critical", "name": "x",
                            "ward": DEPT, "bed": "TB-1"}])
    assert out[0]["news2"] == CORRECTION_NEWS2


def test_sage_fresh_risk_block_scores_the_correction(pid):
    from services.sage_service import _fresh_risk_block

    with db_session() as db:
        block = _fresh_risk_block(db, pid)
    assert block is not None
    assert block["news2"] == CORRECTION_NEWS2


def test_sage_authoritative_news2_line_states_the_correction(pid):
    from services.sage_service import _authoritative_news2_line

    with db_session() as db:
        line = _authoritative_news2_line(db, pid)
    assert line is not None
    first = line.splitlines()[0]
    assert re.findall(r"\d+", first)[-1] == str(CORRECTION_NEWS2), first


def test_sage_patient_context_lists_the_correction_first(pid):
    from services.sage_service import _assemble_patient_context

    with db_session() as db:
        db.execute(
            "INSERT INTO risk_scores (patient_id, score, level, drivers, timestamp) "
            "VALUES (?, 0.1, 'stable', '[]', ?)",
            (pid, datetime.now(timezone.utc).isoformat()),
        )
        ctx = _assemble_patient_context(db, pid, None)
    assert ctx["vitals"][0].startswith("HR:142 BP:84/48 SpO2:86%"), ctx["vitals"]
    if "risk" in ctx:
        assert ctx["risk"]["news2"] == CORRECTION_NEWS2, ctx["risk"]


def test_sage_census_line_prints_the_correction(unordered_reversed):
    """The broad-query census joins on MAX(timestamp), which returns BOTH tied
    rows; the per-patient dict must keep the later write."""
    from services.sage_service import get_relevant_context

    uid = f"TIEBU{uuid.uuid4().hex[:6].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO users (id, name, role, password_hash, department) "
            "VALUES (?, 'Tiebreak Doctor', 'doctor', 'x', ?)",
            (uid, DEPT),
        )
    p = _admit()
    try:
        user = {"id": uid, "role": "doctor", "department": DEPT, "name": "Tiebreak Doctor"}
        context, *_ = get_relevant_context("give me an overview of all my patients", user)
        line = next((ln for ln in context.splitlines() if f"Tiebreak {p[-4:]}" in ln), "")
        assert "HR:142 BP:84/48 SpO2:86%" in line and "RR:31" in line, line
    finally:
        _retire(p)
        with db_session() as db:
            db.execute("DELETE FROM users WHERE id = ?", (uid,))


def test_voice_agent_context_carries_the_correction(pid):
    from services.voice_agent_service import build_patient_context

    ctx = build_patient_context(pid, {"id": ADMITTER, "role": "doctor"})
    assert re.search(r"HR\D{0,3}142\b", ctx), ctx
    assert not re.search(r"HR\D{0,3}72\b", ctx), ctx


def test_ipass_prints_and_scores_the_same_correction(pid):
    """I-PASS prints vitals[-1] of an ASC window and must score that same row."""
    from services.ipass_service import build_ipass

    draft = build_ipass(pid)
    lv = draft["sections"]["situation_awareness"]["latest_vitals"]
    assert (lv["heart_rate"], lv["systolic_bp"], lv["respiratory_rate"], lv["spo2"]) == (
        142, 84, 31, 86), lv
    assert draft["sections"]["illness_severity"]["news2"] == CORRECTION_NEWS2


def test_ipass_scores_the_row_it_prints_not_a_second_read(pid, monkeypatch):
    """A tick landing between two reads used to give a severity for a set the
    handoff did not show. The second read is made to fail outright: the score
    must come from the printed row (vigil_service.risk_for_row), not a re-read."""
    from services import ipass_service

    def _stale_second_read(patient_id):
        raise AssertionError("I-PASS must score the row it prints, not re-read")

    monkeypatch.setattr(ipass_service, "get_patient_risk", _stale_second_read,
                        raising=False)
    draft = ipass_service.build_ipass(pid)
    assert draft["sections"]["illness_severity"]["news2"] == CORRECTION_NEWS2


def test_vigil_recent_vitals_end_on_the_correction(pid):
    from routes.vigil_routes import _load_recent_vitals

    rows = _load_recent_vitals(pid, 24)
    assert rows[-1]["heart_rate"] == 142


def test_bot_carries_forward_from_the_correction(pid, monkeypatch, unordered_reversed):
    """The simulator's next reading drifts from the patient's latest row. Drifting
    from the superseded benign set erases the correction within one tick."""
    from services import bot_service

    seen = {}

    def _spy(db, p, latest, now_ts, *, on_scale_2):
        if p == pid:
            seen["latest"] = dict(latest)
        raise RuntimeError("spy: no reading this tick")

    monkeypatch.setattr(bot_service, "_next_reading", _spy)
    bot_service._generate_vitals(datetime.now(timezone.utc).isoformat(), 1)
    assert seen["latest"]["heart_rate"] == 142, seen


def test_bot_dynamic_finding_quotes_the_correction(pid):
    from services.bot_service import get_dynamic_finding

    with db_session() as db:
        text, _urgency = get_dynamic_finding(db, pid, "ECG")
    assert "142 BPM" in text, text


def test_patient_portal_status_shows_the_correction():
    from services.bridge_service import get_patient_status_simple

    uid = f"TIEBP{uuid.uuid4().hex[:6].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO users (id, name, role, password_hash) "
            "VALUES (?, 'Tiebreak Patient', 'patient', 'x')",
            (uid,),
        )
    p = _admit(user_id=uid)
    try:
        status = get_patient_status_simple(uid)
        assert status["vitals"]["heart_rate"] == "142 bpm", status["vitals"]
    finally:
        _retire(p)
        with db_session() as db:
            db.execute("DELETE FROM users WHERE id = ?", (uid,))


# ── not beside a NEWS2 (trend and export readers) ─────────────────────────────

def test_world_model_trend_ends_on_the_correction(pid):
    from services.world_model_service import compute_patient_state

    state = compute_patient_state(pid)
    assert state["vital_trends"]["heart_rate"]["latest"] == 142.0


def test_forecast_features_end_on_the_correction(pid):
    from services.forecast_service import extract_patient_features

    features = extract_patient_features(pid, seq_len=48)
    assert features is not None
    assert float(features[0, -1, 0]) == 142.0


def test_fhir_export_lists_the_correction_first(pid):
    from services.fhir_export_service import observations_to_fhir

    obs = observations_to_fhir(pid)
    hr = [o for o in obs if o["code"]["coding"][0]["code"] == "8867-4"]
    assert hr[0]["valueQuantity"]["value"] == 142, hr[0]
