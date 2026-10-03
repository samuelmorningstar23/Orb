"""The escalation clock runs on its own, not only when somebody opens the board.

``escalation_service.sweep`` advances overdue tiers, but until 2026-10-02 its
only callers were GET /api/vigil/escalations and POST /escalations/sweep. On an
idle appliance (nobody has Vigil open at 03:00) an unacknowledged deterioration
therefore sat at tier 1 and the registrar was never told. These tests pin:

  * the loop is SPAWNED from main.py's lifespan and its task is retained
    (asserted from the parse tree, like the shadow sweeper: a docstring claiming
    registration is what let that one go missing);
  * a chain past its tier deadline climbs with no route call at all;
  * the tier post it writes is pushed live as ``message:new``, not left in the
    case room until someone refreshes.
"""
import ast
import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from database import db_session
from services import escalation_service as esc
from services.vigil_service import compute_news2

SERVER_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# NEWS2 6: over the urgent-response threshold, so the chain enters at tier 1.
DETERIORATING = dict(heart_rate=95, systolic_bp=120, respiratory_rate=22,
                     temperature=38.5, spo2=93)


def _news2(v):
    return compute_news2(v["heart_rate"], v["systolic_bp"], v["respiratory_rate"],
                         v["temperature"], v["spo2"], "alert", False)


# ── registration ──────────────────────────────────────────────────────────────
def _lifespan_ast():
    with open(os.path.join(SERVER_ROOT, "main.py"), "r", encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), filename="main.py")
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "lifespan":
            return node
    raise AssertionError("main.py has no `lifespan` coroutine")


def _escalation_spawns(lifespan_node):
    found = []
    for node in ast.walk(lifespan_node):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "_spawn"):
            continue
        for arg in node.args:
            if isinstance(arg, ast.Call) and getattr(arg.func, "id", "") == "_escalation_sweeper":
                found.append(node)
    return found


def test_the_escalation_sweeper_is_spawned_and_retained_in_lifespan():
    lifespan = _lifespan_ast()
    spawns = _escalation_spawns(lifespan)
    assert spawns, "escalation_service.sweep_loop is not spawned from main.py's lifespan"

    imported = any(
        isinstance(n, ast.ImportFrom) and n.module == "services.escalation_service"
        and any(a.name == "sweep_loop" and a.asname == "_escalation_sweeper" for a in n.names)
        for n in ast.walk(lifespan)
    )
    assert imported, "_escalation_sweeper must be escalation_service.sweep_loop"

    retained = any(
        isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        and n.func.attr == "append" and any(a in spawns for a in n.args)
        for n in ast.walk(lifespan)
    )
    assert retained, "the sweeper task is spawned but never appended to lifespan_tasks"


def test_sweep_interval_is_clamped(monkeypatch):
    monkeypatch.setenv("ORB_ESCALATION_SWEEP_INTERVAL_S", "1")
    assert esc.sweep_interval_seconds() == 10
    monkeypatch.setenv("ORB_ESCALATION_SWEEP_INTERVAL_S", "9999")
    assert esc.sweep_interval_seconds() == 120
    monkeypatch.setenv("ORB_ESCALATION_SWEEP_INTERVAL_S", "not a number")
    assert esc.sweep_interval_seconds() == 30
    monkeypatch.delenv("ORB_ESCALATION_SWEEP_INTERVAL_S")
    assert esc.sweep_interval_seconds() == 30


# ── behaviour ─────────────────────────────────────────────────────────────────
@pytest.fixture()
def ward(monkeypatch):
    """One admitted patient with a case room, and a hospital-wide scan disabled.

    The scan half of ``sweep`` looks at every ward's recent vitals; left on, it
    would raise chains for other modules' leftover readings. This file is about
    the clock, so only ``advance_due`` runs for real.
    """
    monkeypatch.setattr(esc, "scan_new_observations", lambda **kw: [])
    dept = f"Sweep Loop Ward {uuid.uuid4().hex[:8]}"
    pid = f"PATSWL{uuid.uuid4().hex[:6].upper()}"
    channel = f"CH-SWL-{uuid.uuid4().hex[:8]}"
    with db_session() as db:
        db.execute(
            "INSERT OR IGNORE INTO users (id, name, role, password_hash) "
            "VALUES ('SWLADMITTER', 'Sweep Admitter', 'doctor', 'x')")
        db.execute(
            "INSERT INTO patients (id, name, mrn, bed, ward, age, sex, diagnosis, status) "
            "VALUES (?, 'Sweep Loop Patient', ?, 'S-1', 'SW', 70, 'F', 'Pneumonia', 'admitted')",
            (pid, f"MRN-{uuid.uuid4().hex[:8]}"))
        db.execute(
            "INSERT INTO admissions (patient_id, department, status, admitted_by) "
            "VALUES (?, ?, 'active', 'SWLADMITTER')", (pid, dept))
        db.execute(
            "INSERT INTO channels (id, name, type, patient_id) VALUES (?, ?, 'case_room', ?)",
            (channel, "Case: Sweep Loop Patient", pid))
    yield {"patient": pid, "channel": channel}
    esc._EMIT_LOOP = None
    with db_session() as db:
        db.execute("DELETE FROM escalation_events WHERE escalation_id IN "
                   "(SELECT id FROM clinical_escalations WHERE patient_id = ?)", (pid,))
        db.execute("DELETE FROM clinical_escalations WHERE patient_id = ?", (pid,))
        db.execute("DELETE FROM messages WHERE channel_id = ?", (channel,))
        db.execute("DELETE FROM channels WHERE id = ?", (channel,))
        db.execute("UPDATE admissions SET status = 'discharged' WHERE patient_id = ?", (pid,))
        db.execute("UPDATE patients SET status = 'discharged' WHERE id = ?", (pid,))


async def _run_loop_until(predicate, timeout=5.0):
    task = asyncio.create_task(esc.sweep_loop(0.01))
    try:
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            if predicate():
                return True
            await asyncio.sleep(0.02)
        return False
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


def test_an_overdue_chain_climbs_with_no_route_call(ward, monkeypatch):
    monkeypatch.setenv("ORB_ESCALATION_RELAY_NOTIFY", "0")
    raised = esc.evaluate_observation(
        ward["patient"], _news2(DETERIORATING),
        now=datetime.now(timezone.utc) - timedelta(minutes=45), notify=False,
    )
    assert raised["current_tier"] == 1

    climbed = asyncio.run(_run_loop_until(
        lambda: esc.get_escalation(raised["id"], include_events=False)["current_tier"] == 3))
    assert climbed, "the background loop did not advance an overdue chain"


def test_the_loop_keeps_running_after_a_failed_cycle(ward, monkeypatch):
    calls = {"n": 0}

    def flaky(**kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("database is locked")
        return {"advanced": 0, "raised": 0}

    monkeypatch.setattr(esc, "sweep", flaky)
    assert asyncio.run(_run_loop_until(lambda: calls["n"] >= 3)), \
        "one failed sweep stopped the escalation clock"


def test_a_tier_post_is_pushed_live_as_message_new(ward, monkeypatch):
    monkeypatch.setenv("ORB_ESCALATION_RELAY_NOTIFY", "1")
    sent = []

    async def fake_emit(event, data, room=None, **kw):
        sent.append((event, data, room))

    import ws.realtime as realtime
    monkeypatch.setattr(realtime.sio, "emit", fake_emit)

    raised = esc.evaluate_observation(
        ward["patient"], _news2(DETERIORATING),
        now=datetime.now(timezone.utc) - timedelta(minutes=45), notify=False,
    )
    assert asyncio.run(_run_loop_until(lambda: any(
        e == "message:new" and r == ward["channel"] for e, _d, r in sent))), \
        "the tier post was stored but never pushed to the case room"
    pushed = [d for e, d, r in sent if e == "message:new" and r == ward["channel"]]
    assert raised["id"] in pushed[0]["content"]
    assert pushed[0]["channel_id"] == ward["channel"]
