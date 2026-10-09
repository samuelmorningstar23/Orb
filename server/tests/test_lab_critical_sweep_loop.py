"""The critical-result clock runs on its own (lis-15).

A critical nobody confirms must climb its ladder with nobody looking at the
Critical results screen. The sweep is started by the laboratory module's own
startup (lab_module.startup, which main.py's lifespan awaits; main.py names
no laboratory loop), registered from services/lab_critical_service.py.
Pinned here, from the parse tree and by running it:

  * main.py's lifespan awaits lab_module.startup, and lab_module imports
    services.lab_critical_service before it starts the loops;
  * the loop 'critical-sweep' is registered for every module state, and runs
    only while a critical surface is open: the laboratory on or draining, or
    interfaced criticals on without it;
  * it is started at boot with the laboratory on and cancelled at shutdown,
    and not started with every surface off;
  * one turn of the loop moves a due critical to its next tier with no
    route call, and pushes the new post live as 'message:new'.
"""
from __future__ import annotations

import ast
import asyncio
import os
import uuid
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

import config
import main
from auth import hash_password
from database import db_session
from services import lab_critical_service as lcs
from services import lab_module, site_config_service
from services.clinical_time import format_utc, parse_to_utc, utc_iso_now
from services.site_config_service import SiteConfig

SERVER_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LADDER = [{"tier": 1, "role": "Ward nurse in charge", "label": "Ward nurse in charge",
           "timeout_minutes": 15},
          {"tier": 2, "role": "Duty doctor", "label": "Duty doctor", "timeout_minutes": None}]


# ── registration, from the parse tree ────────────────────────────────────────

def _lifespan():
    with open(os.path.join(SERVER_ROOT, "main.py"), encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), filename="main.py")
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "lifespan":
            return node
    raise AssertionError("main.py has no lifespan coroutine")


def test_the_lifespan_awaits_the_laboratory_startup():
    awaited = [n for n in ast.walk(_lifespan()) if isinstance(n, ast.Await)
               and isinstance(n.value, ast.Call) and isinstance(n.value.func, ast.Attribute)
               and n.value.func.attr == "startup"]
    names = {getattr(n.value.func.value, "id", "") for n in awaited}
    assert "_lab_module" in names, "main.py's lifespan no longer awaits lab_module.startup"


def test_the_laboratory_imports_the_critical_service_before_it_starts_its_loops():
    assert "services.lab_critical_service" in lab_module._LOOP_MODULES


def test_the_critical_sweep_is_registered_for_every_state():
    entries = {name: states for name, _factory, states in lab_module._LOOPS}
    assert lcs.SWEEP_LOOP_NAME in entries
    assert set(entries[lcs.SWEEP_LOOP_NAME]) == {"on", "draining", "off"}
    factory = next(f for n, f, _s in lab_module._LOOPS if n == lcs.SWEEP_LOOP_NAME)
    assert factory is lcs._sweep_task


# ── started at boot, stopped at shutdown ─────────────────────────────────────

@pytest.fixture
def probe(monkeypatch):
    seen = {"started": 0, "cancelled": 0}

    async def fake_loop(interval_seconds=None):
        seen["started"] += 1
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            seen["cancelled"] += 1
            raise

    monkeypatch.setattr(lcs, "sweep_loop", fake_loop)
    return seen


def test_it_starts_with_the_laboratory_on_and_stops_at_shutdown(probe, monkeypatch):
    monkeypatch.setattr(config, "LAB_ENABLED", True)
    with TestClient(main.app):
        pass
    assert probe == {"started": 1, "cancelled": 1}


def test_it_does_not_run_with_every_critical_surface_off(probe, monkeypatch):
    monkeypatch.setattr(config, "LAB_ENABLED", False)
    monkeypatch.setattr(config, "LAB_CRITICAL_INTERFACED_ENABLED", False)
    monkeypatch.setattr(lab_module, "drain_check", lambda: False)
    monkeypatch.setattr(lab_module, "refresh_drain_state", lambda conn=None: False)
    with TestClient(main.app):
        pass
    assert probe["started"] == 0


def test_it_runs_for_interfaced_criticals_without_the_laboratory(probe, monkeypatch):
    monkeypatch.setattr(config, "LAB_ENABLED", False)
    monkeypatch.setattr(config, "LAB_CRITICAL_INTERFACED_ENABLED", True)
    monkeypatch.setattr(lab_module, "drain_check", lambda: False)
    monkeypatch.setattr(lab_module, "refresh_drain_state", lambda conn=None: False)
    with TestClient(main.app):
        pass
    assert probe == {"started": 1, "cancelled": 1}


# ── one turn of the real loop ────────────────────────────────────────────────

@pytest.fixture
def due_critical(monkeypatch):
    """An open critical at tier 1 of a two-tier ladder, posted, and past due."""
    monkeypatch.setattr(config, "LAB_ENABLED", True)
    monkeypatch.setattr(site_config_service, "_loaded",
                        SiteConfig(present=True, lab_critical={"tiers": LADDER}))
    suffix = uuid.uuid4().hex[:8].upper()
    pid, room, uid = f"PLSL{suffix}", f"lsl-{suffix.lower()}", f"LSLTECH{suffix[:5]}"
    nid = f"LC-sweep-{suffix.lower()}"
    raised = format_utc(parse_to_utc(utc_iso_now()) - timedelta(minutes=30))
    due = format_utc(parse_to_utc(raised) + timedelta(minutes=15))
    policy = '{"configured": true, "source": "site.toml", "tiers": ' + \
        __import__("json").dumps(LADDER) + "}"
    with db_session() as db:
        db.execute("INSERT INTO users (id, name, role, password_hash) VALUES (?, ?, ?, ?)",
                   (uid, f"Tech {uid}", "lab_technician", hash_password("password")))
        db.execute("INSERT INTO patients (id, name, mrn, status) VALUES (?, ?, ?, 'admitted')",
                   (pid, f"Sweep {suffix}", f"MRN-LSL-{suffix}"))
        db.execute("INSERT INTO channels (id, name, type, patient_id) "
                   "VALUES (?, ?, 'case_room', ?)", (room, f"Case {pid}", pid))
        rid = db.execute(
            "INSERT INTO lab_results (order_item_id, order_id, patient_id, test_id, entered_at, "
            "status, value_text, unit, is_critical) VALUES (?, ?, ?, 'LOCAL:SWEEP-K', ?, "
            "'tech_validated', '7.2', 'mmol/L', 1)",
            (900000 + int(suffix[:4], 16), f"ORD-{suffix}", pid, raised)).lastrowid
        db.execute(
            "INSERT INTO lab_critical_notifications (id, source_lane, result_id, patient_id, "
            "test_name, value_display, preliminary, raised_at, raised_by, current_tier, "
            "tier_due_at, delivery_status, policy_json, status) VALUES (?, 'lis', ?, ?, "
            "'Potassium', '7.2 mmol/L', 1, ?, ?, 1, ?, 'posted', ?, 'open')",
            (nid, rid, pid, raised, uid, due, policy))
    yield {"id": nid, "patient_id": pid, "room": room}
    with db_session() as db:
        db.execute("DELETE FROM lab_critical_events WHERE notification_id = ?", (nid,))
        db.execute("DELETE FROM lab_critical_notifications WHERE id = ?", (nid,))
        db.execute("DELETE FROM lab_results WHERE id = ?", (rid,))
        db.execute("DELETE FROM messages WHERE channel_id = ?", (room,))
        db.execute("DELETE FROM channels WHERE id = ?", (room,))
        db.execute("UPDATE patients SET status = 'discharged' WHERE id = ?", (pid,))
    lab_module.refresh_drain_state()


def test_one_turn_climbs_a_due_critical_and_pushes_the_post_live(due_critical, monkeypatch):
    from ws import realtime

    emitted = []

    async def emit(event, data=None, room=None, **kwargs):
        emitted.append((event, data, room))

    monkeypatch.setattr(realtime.sio, "emit", emit)
    real_sleep = asyncio.sleep
    turns = {"n": 0}

    async def quick_sleep(seconds):
        turns["n"] += 1
        if turns["n"] > 1:
            raise asyncio.CancelledError
        await real_sleep(0)

    monkeypatch.setattr(lcs, "_sleep", quick_sleep)

    async def run():
        with pytest.raises(asyncio.CancelledError):
            await lcs.sweep_loop(interval_seconds=60)

    asyncio.run(run())
    with db_session() as db:
        row = dict(db.execute("SELECT * FROM lab_critical_notifications WHERE id = ?",
                              (due_critical["id"],)).fetchone())
        posts = [dict(r) for r in db.execute("SELECT * FROM messages WHERE channel_id = ?",
                                             (due_critical["room"],)).fetchall()]
    assert row["current_tier"] == 2 and row["status"] == "open"
    # The last tier never hands on: it is due from the moment it was reached.
    assert row["delivery_status"] == "posted" and row["tier_due_at"] is not None
    assert parse_to_utc(row["tier_due_at"]) <= parse_to_utc(utc_iso_now())
    assert len(posts) == 1 and "The laboratory is now calling: Duty doctor" in posts[0]["content"]
    assert ("message:new", emitted[0][1], due_critical["room"]) == emitted[0]
    assert emitted[0][1]["id"] == posts[0]["id"]


def test_the_loop_stops_itself_once_every_surface_is_off(monkeypatch):
    monkeypatch.setattr(config, "LAB_ENABLED", False)
    monkeypatch.setattr(config, "LAB_CRITICAL_INTERFACED_ENABLED", False)
    monkeypatch.setattr(config, "_MODULE_DRAIN_CHECKS", {"lab": lambda: False})
    real_sleep = asyncio.sleep

    async def quick_sleep(seconds):
        await real_sleep(0)

    monkeypatch.setattr(lcs, "_sleep", quick_sleep)
    asyncio.run(asyncio.wait_for(lcs.sweep_loop(interval_seconds=60), timeout=5))
