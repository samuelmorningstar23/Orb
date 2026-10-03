"""NEWS2 SpO2 Scale 2 must survive every lane that scores a patient.

WHY THIS FILE EXISTS (found 2026-10-02, NEWS2 render-site sweep). Scale 2 is the
SpO2 table for COPD / chronic type-2 respiratory failure (target 88-92 %), chosen
per patient by `patients.on_scale_2`. The nurse, ingestion, Sage, downtime and
escalation lanes all pass that flag to the scorer. Two lanes did not:

  * the vitals simulator (`bot_service._next_reading`) called compute_news2 with
    no `on_scale_2`, dispatched Relay alerts on the Scale 1 result, and pushed
    that risk on the `vitals:update` socket event. VigilPanel then REPLACES the
    chart's correct Scale 2 breakdown with it, so a COPD patient's SpO2 88 %
    read +3 instead of 0 within one tick of opening the chart;
  * the admission baseline seeded `risk_scores` the same way.

The error runs in BOTH directions, and the second one is the dangerous one:

    COPD, on air, SpO2 88 (on target)   Scale 1: NEWS2 3, elevated, false page
                                        Scale 2: NEWS2 0, stable,   no page
    COPD, on O2, SpO2 97 (over-oxygen.) Scale 1: NEWS2 2, stable,   NO PAGE
                                        Scale 2: NEWS2 5, elevated, page

Nothing in Orb can prescribe Scale 2 yet (gap register X003), so no patient was
mis-scored in the running app. These tests make every lane agree BEFORE the
prescribe route ships, and the last one keeps a new lane from forgetting.

WHAT IS ASSERTED is what leaves the lane: the payload handed to the socket emit,
the Relay message as persisted, the shadow `alert.fired` row, the `risk_scores`
row. Never a return value the lane computed and then threw away.
"""
from __future__ import annotations

import ast
import asyncio
import json
import random
from datetime import datetime, timedelta, timezone
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import services.bot_service as bot_service  # noqa: E402
from database import db_session  # noqa: E402
from services.clinical_time import format_utc  # noqa: E402

SERVER = Path(__file__).resolve().parent.parent

# Normal on every parameter except SpO2, so the SpO2 scale alone decides the
# band. With the random walk pinned (see _pinned_tick) the critical profile moves
# these to HR 81.1, SBP 123.9, RR 16.3, Temp 37.1: all score 0.
_BASE = {"heart_rate": 80.0, "systolic_bp": 125.0, "diastolic_bp": 75.0,
         "temperature": 37.0, "respiratory_rate": 16.0, "consciousness": "alert"}

#: Every patient this module creates, discharged on teardown.
_CREATED: list[str] = []


@pytest.fixture(autouse=True)
def _discharge_created():
    """A leftover admitted patient gets a reading on every later simulator tick
    in the suite and starts counting in ward aggregates (conftest's sweep)."""
    yield
    if not _CREATED:
        return
    with db_session() as db:
        db.executemany("UPDATE patients SET status = 'discharged' WHERE id = ?",
                       [(p,) for p in _CREATED])
        db.executemany("UPDATE admissions SET status = 'discharged' "
                       "WHERE patient_id = ? AND status = 'active'",
                       [(p,) for p in _CREATED])
    _CREATED.clear()


def _mk_patient(*, scale_2: bool, spo2: float, on_oxygen: bool) -> str:
    """An admitted patient with a Relay case room and ONE prior vitals row."""
    pid = f"S2LANE{uuid.uuid4().hex[:8].upper()}"
    _CREATED.append(pid)
    with db_session() as db:
        db.execute(
            "INSERT INTO patients (id, name, mrn, bed, ward, age, sex, status, on_scale_2) "
            "VALUES (?, ?, ?, 'S2-1', 'S2-WARD', 71, 'M', 'admitted', ?)",
            (pid, f"Scale Lane {pid}", f"MRN-{pid}", 1 if scale_2 else 0),
        )
        db.execute(
            "INSERT OR REPLACE INTO channels (id, name, type, patient_id) "
            "VALUES (?, ?, 'case_room', ?)",
            (f"s2-room-{pid}", f"Case: {pid}", pid),
        )
        db.execute(
            """INSERT INTO vitals (patient_id, timestamp, heart_rate, systolic_bp,
               diastolic_bp, spo2, temperature, respiratory_rate, consciousness, on_oxygen)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            # Five minutes ago, not 2020: since 2026-10-03 carry-forward is
            # age-limited (services/vitals_carry.py), and a six-year-old oxygen
            # flag is rightly not carried into the tick.
            (pid, format_utc(datetime.now(timezone.utc) - timedelta(minutes=5)),
             _BASE["heart_rate"], _BASE["systolic_bp"], _BASE["diastolic_bp"],
             spo2, _BASE["temperature"], _BASE["respiratory_rate"],
             _BASE["consciousness"], 1 if on_oxygen else 0),
        )
    return pid


class _NullSio:
    async def emit(self, *a, **kw):
        return None


async def _pinned_tick(monkeypatch, patients: list[str]) -> dict[str, dict]:
    """One REAL `run_vitals_bot` tick, with only the randomness pinned.

    Returns {patient_id: payload} as it was handed to the socket emit. The
    simulator still ticks every admitted patient in the session DB (that is the
    production shape); only the emit and the alert dispatch are narrowed to the
    patients under test, so this test cannot page anyone else's case room.
    """
    # No noise, no crisis spike: the next reading is cur + reversion * (target - cur).
    monkeypatch.setattr(random, "gauss", lambda *a, **k: 0.0)
    monkeypatch.setattr(random, "random", lambda: 0.99)
    for pid in patients:
        # Critical profile: target SpO2 90, clamp 80-99, so SpO2 97 stays 97
        # (96.8) and SpO2 88 stays 88 (88.1) after one tick.
        monkeypatch.setitem(bot_service._severity_cache, pid, "critical")
    # Cycle 1: not a multiple of 120 (forecast refresh) or 60 (prune).
    monkeypatch.setattr(bot_service, "_cycle_counter", 0)

    ours = set(patients)
    real_generate = bot_service._generate_vitals

    def _only_ours(now_ts, cycle):
        queue = real_generate(now_ts, cycle)
        return [p for p in (queue or []) if p["patient_id"] in ours]

    monkeypatch.setattr(bot_service, "_generate_vitals", _only_ours)

    emitted: dict[str, dict] = {}

    async def _record_emit(event, payload):
        assert event == "vitals:update"
        emitted[payload["patient_id"]] = payload

    import ws.realtime
    monkeypatch.setattr(ws.realtime, "emit_patient_vitals", _record_emit)
    monkeypatch.setattr(bot_service, "publish_vigil_critical", lambda *a, **k: True)

    async def _no_model(*a, **k):
        return "Escalate to the registrar."
    monkeypatch.setattr(bot_service, "generate_ai_recommendation", _no_model)
    monkeypatch.setenv("ORB_SHADOW_ENABLED", "true")

    await bot_service.run_vitals_bot(_NullSio())

    # `alert.delivered` is written by a background task. Let it land before
    # pytest-asyncio closes this test's loop, or it is destroyed mid-write.
    # Only THIS loop's tasks: one left by an earlier module on a closed loop
    # can never finish and would cost every test here the full timeout.
    from services import shadow_service
    loop = asyncio.get_running_loop()
    pending = [t for t in list(shadow_service._inflight)
               if not t.done() and t.get_loop() is loop]
    if pending:
        await asyncio.wait(pending, timeout=10)
    return emitted


def _spo2_row(risk: dict) -> dict:
    rows = [b for b in risk["breakdown"] if str(b["parameter"]).startswith("SpO2")]
    assert len(rows) == 1, risk["breakdown"]
    return rows[0]


def _alerts(pid: str) -> list[str]:
    with db_session() as db:
        return [r["content"] for r in db.execute(
            "SELECT m.content FROM messages m JOIN channels c ON c.id = m.channel_id "
            "WHERE c.patient_id = ? AND m.message_type = 'alert' ORDER BY m.id",
            (pid,)).fetchall()]


def _fired(pid: str) -> list[dict]:
    with db_session() as db:
        return [json.loads(r["payload_json"] or "{}") for r in db.execute(
            "SELECT payload_json FROM shadow_events_current "
            "WHERE patient_id = ? AND kind = 'alert.fired' ORDER BY id",
            (pid,)).fetchall()]


# ═════════════════════════════════════════════════════════════════════════════
#  The simulator lane: socket risk, Relay alert, shadow row
# ═════════════════════════════════════════════════════════════════════════════

async def test_an_over_oxygenated_scale_2_patient_is_paged_with_scale_2_points(monkeypatch):
    """The dangerous direction. On Scale 1 a COPD patient on O2 at SpO2 97 scores
    0 for SpO2 (NEWS2 2, stable) and nobody is paged. On Scale 2 it is
    over-oxygenation, +3, NEWS2 5, elevated."""
    pid = _mk_patient(scale_2=True, spo2=97.0, on_oxygen=True)

    emitted = await _pinned_tick(monkeypatch, [pid])

    # The socket payload VigilPanel merges into the open chart.
    risk = emitted[pid]["risk"]
    spo2 = _spo2_row(risk)
    assert spo2["parameter"] == "SpO2 (Scale 2)", risk["breakdown"]
    assert spo2["points"] == 3, spo2
    assert risk["spo2_scale"] == 2
    assert risk["raw_news2"] == 5 and risk["level"] == "elevated", risk

    # The Relay alert as persisted.
    alerts = _alerts(pid)
    assert len(alerts) == 1, f"expected one page for over-oxygenation, got {alerts}"
    assert "NEWS2 5 (elevated)" in alerts[0], alerts[0]

    # The shadow instrument records which table scored it.
    fired = _fired(pid)
    assert fired and fired[-1]["spo2_scale"] == 2, fired


async def test_an_on_target_scale_2_patient_on_air_is_not_paged(monkeypatch):
    """The noisy direction. SpO2 88 on air is this patient's target, not hypoxia.
    Scale 1 called it +3 (single parameter 3, elevated) and paged the ward."""
    pid = _mk_patient(scale_2=True, spo2=88.0, on_oxygen=False)

    emitted = await _pinned_tick(monkeypatch, [pid])

    risk = emitted[pid]["risk"]
    spo2 = _spo2_row(risk)
    assert spo2["parameter"] == "SpO2 (Scale 2)", risk["breakdown"]
    assert spo2["points"] == 0, spo2
    assert risk["spo2_scale"] == 2
    assert risk["raw_news2"] == 0 and risk["level"] == "stable", risk
    assert _alerts(pid) == [], "an on-target COPD saturation paged the ward"
    assert _fired(pid) == []


async def test_a_scale_1_patient_on_the_same_tick_is_still_scored_on_scale_1(monkeypatch):
    """The control. The fix reads the flag per patient; it must not flip the
    ward. Ticked together with a Scale 2 patient so a fix that leaks one
    patient's scale onto the next is caught too."""
    copd = _mk_patient(scale_2=True, spo2=88.0, on_oxygen=False)
    other = _mk_patient(scale_2=False, spo2=88.0, on_oxygen=False)

    emitted = await _pinned_tick(monkeypatch, [copd, other])

    risk = emitted[other]["risk"]
    spo2 = _spo2_row(risk)
    assert spo2["parameter"] == "SpO2" and spo2["points"] == 3, spo2
    assert risk["spo2_scale"] == 1
    assert risk["level"] == "elevated"
    assert len(_alerts(other)) == 1
    assert _spo2_row(emitted[copd]["risk"])["points"] == 0


# ═════════════════════════════════════════════════════════════════════════════
#  The admission baseline
# ═════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def _admission_records_scale_2():
    """Stands in for the admission form that does not exist yet (X003).

    `admit_patient` has no Scale 2 input, so today every new patient row has
    on_scale_2 = 0 and the baseline is right by accident. A trigger sets the
    flag on the row admit_patient inserts, the way a future form would, so the
    test shows whether the baseline READS the flag rather than assuming it.
    """
    with db_session() as db:
        db.execute(
            "CREATE TRIGGER IF NOT EXISTS _test_admit_scale_2 AFTER INSERT ON patients "
            "WHEN NEW.name = 'Scale Two Admission' "
            "BEGIN UPDATE patients SET on_scale_2 = 1 WHERE id = NEW.id; END"
        )
    try:
        yield
    finally:
        with db_session() as db:
            db.execute("DROP TRIGGER IF EXISTS _test_admit_scale_2")


def test_the_admission_baseline_scores_the_patients_recorded_scale(_admission_records_scale_2):
    from auth import hash_password
    from services.admission_service import admit_patient

    with db_session() as db:
        db.execute(
            "INSERT OR IGNORE INTO users (id, name, role, password_hash) VALUES (?,?,?,?)",
            ("S2-DOC", "Dr Scale", "doctor", hash_password("x")),
        )
    res = admit_patient(
        {"name": "Scale Two Admission", "ward": "General",
         "vitals": {"hr": 80, "bp_sys": 125, "bp_dia": 75, "spo2": 88,
                    "temperature": 37.0, "rr": 16, "on_oxygen": False}},
        {"id": "S2-DOC", "name": "Dr Scale", "role": "doctor"},
    )
    pid = res["patient_id"]
    _CREATED.append(pid)

    with db_session() as db:
        assert db.execute("SELECT on_scale_2 FROM patients WHERE id = ?",
                          (pid,)).fetchone()["on_scale_2"] == 1
        row = db.execute(
            "SELECT level, drivers FROM risk_scores WHERE patient_id = ? ORDER BY id DESC LIMIT 1",
            (pid,)).fetchone()
    assert row is not None, "admission seeded no baseline risk"
    drivers = json.loads(row["drivers"] or "[]")
    assert row["level"] == "stable", (row["level"], drivers)
    assert not any("hypoxia" in d.lower() for d in drivers), drivers


# ═════════════════════════════════════════════════════════════════════════════
#  The drift guard: a lane cannot forget the scale again
# ═════════════════════════════════════════════════════════════════════════════

#: Where patient scoring lives. scripts/ is out: wedge_bench scores one fixed
#: benchmark vital set, not a patient.
_SCANNED = ("services", "routes", "ws")
#: The scorer's own module defines compute_news2 and its deprecated alias.
_EXEMPT = {"services/vigil_service.py"}
_SCORERS = {"compute_news2", "score_vitals_row"}
#: Scale 1 only, with no on_scale_2 parameter at all: no lane may call it.
_SCALE_1_ONLY = {"compute_mews"}


def _calls_missing_the_scale() -> list[str]:
    misses = []
    files = [p for d in _SCANNED for p in sorted((SERVER / d).rglob("*.py"))]
    files += sorted(SERVER.glob("*.py"))
    for path in files:
        rel = path.relative_to(SERVER).as_posix()
        if rel in _EXEMPT:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
        # `from services.vigil_service import compute_news2 as news2` must not
        # be a way around the guard.
        alias = {a.asname: a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)
                 for a in n.names if a.asname and a.name in _SCORERS | _SCALE_1_ONLY}
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = fn.id if isinstance(fn, ast.Name) else fn.attr if isinstance(fn, ast.Attribute) else None
            name = alias.get(name, name)
            if name in _SCALE_1_ONLY:
                misses.append(f"{rel}:{node.lineno} {name}(...) can only score Scale 1")
            elif name in _SCORERS and not any(kw.arg == "on_scale_2" for kw in node.keywords):
                misses.append(f"{rel}:{node.lineno} {name}(...) without on_scale_2")
    return misses


def test_every_patient_scoring_call_passes_the_patients_scale():
    """Both scorers DEFAULT to Scale 1, so forgetting the flag is silent: the
    lane works, the tests built on Scale 1 fixtures pass, and a COPD patient is
    scored on the wrong table. This is how the simulator and the admission
    baseline drifted while five other lanes were right. An explicit keyword at
    every call is the cheapest thing that makes the omission visible."""
    misses = _calls_missing_the_scale()
    assert not misses, "\n".join(misses)
