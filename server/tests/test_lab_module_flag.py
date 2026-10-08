"""The laboratory module switch and its three states (lis-1).

ORB_LAB is off unless a site turns it on. services/lab_module.py computes the
state at startup, off the event loop, and answers /api/health from a cached,
DB-free drain check:

  on        ORB_LAB set;
  draining  ORB_LAB off with laboratory work still open (ruling LIS-R21,
            strictest option: open work is never hidden, new work stops, and
            the compliance report fails until the work is done);
  off       nothing open: every /api/lab route answers 404.

Also pinned: one cutover event per transition (never one per boot), the
starter catalogue and its terminology codes only with ORB_LAB on, the lifespan
hook registering and unregistering the drain check, and the loops later LIS
increments register.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import textwrap
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import config
import main
from auth import create_token, hash_password
from database import db_session
from services import lab_module
from services.clinical_time import utc_iso_now

SERVER = Path(__file__).resolve().parents[1]


def _hdr(role):
    uid = f"LF{uuid.uuid4().hex[:6].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO users (id, name, role, department, password_hash) VALUES (?, ?, ?, ?, ?)",
            (uid, f"{role} {uid}", role, None, hash_password("password")))
    return {"Authorization": f"Bearer {create_token(uid, role)}"}


@pytest.fixture(autouse=True)
def _clean_module_state():
    """No drain check or cached answer leaks between tests."""
    saved = dict(lab_module._drain)
    yield
    config.register_module_drain_check("lab", None)
    lab_module._drain.update(saved)


@pytest.fixture
def open_item():
    """One laboratory test not yet released: open work."""
    order_id = f"LFORD-{uuid.uuid4().hex[:8]}"
    with db_session() as db:
        item_id = db.execute(
            "INSERT INTO lab_order_items (order_id, patient_id, test_id, specimen_id, status, "
            "created_at) VALUES (?, 'PLFOPEN', 'LOINC:2823-3', 'S-LF-1', 'received', ?)",
            (order_id, utc_iso_now())).lastrowid
    yield item_id
    with db_session() as db:
        db.execute("DELETE FROM lab_order_items WHERE id = ?", (item_id,))
    lab_module.refresh_drain_state()


# ── 1. Off by default ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw", [None, "", "  ", "0", "false", "off", "no", "maybe"])
def test_orb_lab_unset_blank_or_anything_else_is_off(monkeypatch, raw):
    if raw is None:
        monkeypatch.delenv("ORB_LAB", raising=False)
    else:
        monkeypatch.setenv("ORB_LAB", raw)
    assert config._opt_in("ORB_LAB") is False


def test_with_the_switch_off_and_nothing_open_the_module_is_off():
    with TestClient(main.app) as client:
        assert "lab" in config._MODULE_DRAIN_CHECKS, "startup registers the drain check"
        body = client.get("/api/health").json()
        assert body["modules"]["lab"] is False
        assert body["module_states"]["lab"] == "off"
        resp = client.get("/api/lab/meta", headers=_hdr("pathologist"))
        assert resp.status_code == 404
        assert resp.json()["detail"]["error"] == "module_disabled"
    assert "lab" not in config._MODULE_DRAIN_CHECKS, "shutdown unregisters it"


def test_with_the_switch_on_the_module_answers(monkeypatch):
    monkeypatch.setattr(config, "LAB_ENABLED", True)
    with TestClient(main.app) as client:
        body = client.get("/api/health").json()
        assert body["modules"]["lab"] is True and body["module_states"]["lab"] == "on"
        meta = client.get("/api/lab/meta", headers=_hdr("pathologist"))
        assert meta.status_code == 200 and meta.json()["state"] == "on"


# ── 2. Draining ───────────────────────────────────────────────────────────────

def test_open_work_counts_tolerate_empty_tables():
    with db_session() as db:
        counts = lab_module.open_work_counts(db)
    assert set(counts) == {"specimens", "tests", "results", "critical_results"}


def test_open_work_makes_a_switched_off_module_drain(open_item):
    assert lab_module.refresh_drain_state() is True
    counts = lab_module.cached_open_work()
    assert counts["tests"] >= 1 and counts["specimens"] >= 1
    config.register_module_drain_check("lab", lab_module.drain_check)
    assert config.module_state("lab") == "draining"
    assert config.enabled_modules()["lab"] is True, "lab staff keep the tab to finish the work"
    with db_session() as db:
        db.execute("UPDATE lab_order_items SET status = 'released' WHERE id = ?", (open_item,))
    assert lab_module.refresh_drain_state() is False
    assert config.module_state("lab") == "off"


def test_unreleased_results_and_open_criticals_are_open_work():
    nid = f"LFCRIT-{uuid.uuid4().hex[:8]}"
    with db_session() as db:
        rid = db.execute(
            "INSERT INTO lab_results (order_item_id, order_id, patient_id, test_id, entered_at, "
            "status) VALUES (987654, 'LFORD-R', 'PLFOPEN', 'LOINC:2823-3', ?, 'tech_validated')",
            (utc_iso_now(),)).lastrowid
        db.execute(
            "INSERT INTO lab_critical_notifications (id, source_lane, result_id, patient_id, "
            "test_name, value_display, raised_at) VALUES (?, 'lis', ?, 'PLFOPEN', 'Potassium', "
            "'7.0 mmol/L', ?)", (nid, rid, utc_iso_now()))
    try:
        with db_session() as db:
            counts = lab_module.open_work_counts(db)
        assert counts["results"] >= 1 and counts["critical_results"] >= 1
        assert lab_module.refresh_drain_state() is True
        with db_session() as db:
            db.execute("UPDATE lab_results SET status = 'released' WHERE id = ?", (rid,))
            db.execute("UPDATE lab_critical_notifications SET status = 'acknowledged' WHERE id = ?",
                       (nid,))
            counts = lab_module.open_work_counts(db)
        assert counts["results"] == 0 and counts["critical_results"] == 0
    finally:
        with db_session() as db:
            db.execute("DELETE FROM lab_critical_notifications WHERE id = ?", (nid,))
            db.execute("DELETE FROM lab_results WHERE id = ?", (rid,))
        lab_module.refresh_drain_state()


def test_a_failed_count_reads_as_draining_never_as_off(monkeypatch):
    def boom(conn):
        raise RuntimeError("database unavailable")
    monkeypatch.setattr(lab_module, "open_work_counts", boom)
    assert lab_module.refresh_drain_state() is True
    assert lab_module.drain_check() is True
    config.register_module_drain_check("lab", lab_module.drain_check)
    assert config.module_state("lab") == "draining"


def test_draining_lab_meta_answers_and_a_strict_route_refuses(open_item):
    lab_module.refresh_drain_state()
    config.register_module_drain_check("lab", lab_module.drain_check)
    client = TestClient(main.app)
    meta = client.get("/api/lab/meta", headers=_hdr("lab_technician"))
    assert meta.status_code == 200 and meta.json()["state"] == "draining"
    write = client.put("/api/lab/master/sections/haematology", headers=_hdr("pathologist"),
                       json={"name": "Haematology"})
    assert write.status_code == 404 and write.json()["detail"]["error"] == "module_disabled"


def test_health_never_opens_a_connection_even_while_draining(monkeypatch, open_item):
    import database
    with TestClient(main.app) as client:
        lab_module.refresh_drain_state()
        assert client.get("/api/health").json()["module_states"]["lab"] == "draining"

        def refuse(*a, **k):
            raise AssertionError("/api/health opened a database connection")
        monkeypatch.setattr(database, "db_session", refuse)
        monkeypatch.setattr(database, "get_db", refuse)
        resp = client.get("/api/health")
        assert resp.status_code == 200
        assert resp.json()["module_states"]["lab"] == "draining"
        monkeypatch.undo()


def test_the_compliance_report_fails_while_draining_and_is_silent_otherwise(open_item):
    from services.compliance_service import compliance_report
    report = compliance_report()
    control = next(c for c in report["controls"] if c["id"] == "lab_module_draining")
    assert control["status"] == "fail"
    assert "specimen(s) with tests not yet released" in control["evidence"]
    assert "critical result(s) not yet closed" in control["evidence"]
    assert chr(0x2014) not in control["evidence"] + control["remediation"], "no em dashes"
    with db_session() as db:
        db.execute("UPDATE lab_order_items SET status = 'cancelled' WHERE id = ?", (open_item,))
    report = compliance_report()
    assert "lab_module_draining" not in {c["id"] for c in report["controls"]}


def test_the_compliance_report_says_nothing_about_the_lab_when_it_is_on(monkeypatch, open_item):
    from services.compliance_service import compliance_report
    monkeypatch.setattr(config, "LAB_ENABLED", True)
    assert "lab_module_draining" not in {c["id"] for c in compliance_report()["controls"]}


def test_the_drain_refresh_loop_stops_once_the_work_is_finished(open_item):
    lab_module.refresh_drain_state()

    async def scenario():
        task = asyncio.create_task(lab_module._drain_refresh_loop(0))
        await asyncio.sleep(0.2)
        assert not task.done(), "keeps watching while work is open"
        await asyncio.to_thread(_release, open_item)
        await asyncio.wait_for(task, timeout=5)
    asyncio.run(scenario())
    assert lab_module.drain_check() is False


def _release(item_id):
    with db_session() as db:
        db.execute("UPDATE lab_order_items SET status = 'released' WHERE id = ?", (item_id,))


# ── 3. The cutover ────────────────────────────────────────────────────────────

def _events():
    with db_session() as db:
        return [r["event"] for r in db.execute(
            "SELECT event FROM lab_module_events ORDER BY julianday(occurred_at), id").fetchall()]


def test_one_cutover_event_per_transition_never_one_per_boot():
    with db_session(immediate=True) as db:
        lab_module.record_cutover(db, lab_on=False)  # settle whatever earlier files left
    start = len(_events())
    steps = [(False, None), (True, "enabled"), (True, None), (True, None),
             (False, "disabled"), (False, None), (True, "enabled"), (False, "disabled")]
    for lab_on, expected in steps:
        with db_session(immediate=True) as db:
            assert lab_module.record_cutover(db, lab_on=lab_on) == expected, (lab_on, expected)
    assert _events()[start:] == ["enabled", "disabled", "enabled", "disabled"]
    with db_session() as db:
        audits = db.execute(
            "SELECT action_type, event_category FROM audit_logs WHERE action_type IN "
            "('lab_module_enabled', 'lab_module_disabled') ORDER BY id DESC LIMIT 4").fetchall()
    assert {a["event_category"] for a in audits} == {"clinical_config"}


def test_the_enable_instant_is_the_latest_enabled_event_and_none_when_off():
    with db_session(immediate=True) as db:
        lab_module.record_cutover(db, lab_on=True)
        instant = lab_module.current_enable_instant(db)
        assert instant and instant.endswith("+00:00")
    with db_session(immediate=True) as db:
        lab_module.record_cutover(db, lab_on=False)
        assert lab_module.current_enable_instant(db) is None


def test_the_cutover_and_its_audit_commit_together(monkeypatch):
    from services import audit_service
    from services.audit_service import AuditWriteError
    with db_session(immediate=True) as db:
        lab_module.record_cutover(db, lab_on=False)
    before = _events()

    def refuse(*a, **k):
        raise AuditWriteError("audit store unavailable")
    monkeypatch.setattr(audit_service, "log_audit_event", refuse)
    with pytest.raises(AuditWriteError):
        with db_session(immediate=True) as db:
            lab_module.record_cutover(db, lab_on=True)
    assert _events() == before


# ── 4. Startup and shutdown through the real lifespan ─────────────────────────

def _terminology_count():
    with db_session() as db:
        return db.execute("SELECT COUNT(*) FROM terminology").fetchone()[0]


def test_switched_off_startup_leaves_terminology_and_the_catalogue_alone():
    with db_session() as db:
        tests_before = db.execute("SELECT COUNT(*) FROM lab_tests").fetchone()[0]
    with TestClient(main.app):
        pass
    terms = _terminology_count()
    with TestClient(main.app):
        pass
    assert _terminology_count() == terms
    with db_session() as db:
        assert db.execute("SELECT COUNT(*) FROM lab_tests").fetchone()[0] == tests_before
    assert _events()[-1:] in ([], ["disabled"])


def test_switched_on_startup_writes_enabled_and_loads_the_catalogue(monkeypatch):
    monkeypatch.setattr(config, "LAB_ENABLED", True)
    with TestClient(main.app):
        pass
    assert _events()[-1] == "enabled"
    from services.terminology_service import validate_code
    assert validate_code("LOINC", "24325-3"), "a starter code new to terminology is registered"
    with db_session() as db:
        assert db.execute("SELECT COUNT(*) FROM lab_tests").fetchone()[0] >= 40
    monkeypatch.setattr(config, "LAB_ENABLED", False)
    with TestClient(main.app):
        pass
    assert _events()[-1] == "disabled"


def test_a_registered_loop_runs_in_its_states_and_stops_at_shutdown(monkeypatch):
    seen = {"started": 0, "cancelled": 0}

    async def loop():
        seen["started"] += 1
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            seen["cancelled"] += 1
            raise
    monkeypatch.setattr(lab_module, "_LOOPS", [])
    lab_module.register_loop("probe", loop, states=("on",))
    with TestClient(main.app):
        pass
    assert seen == {"started": 0, "cancelled": 0}, "not started while the module is off"
    monkeypatch.setattr(config, "LAB_ENABLED", True)
    with TestClient(main.app):
        pass
    assert seen == {"started": 1, "cancelled": 1}
    with pytest.raises(ValueError):
        lab_module.register_loop("bad", loop, states=("sideways",))


# ── 5. A fresh database, end to end ───────────────────────────────────────────

_FRESH = textwrap.dedent("""
    import config
    from database import init_db, db_session
    from services import lab_module
    from services.terminology_service import seed_terminology
    init_db()
    seed_terminology()

    def count(sql):
        with db_session() as db:
            return db.execute(sql).fetchone()[0]

    lab_tables = [r[0] for r in __import__('database').get_db().execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'lab_%'").fetchall()]
    terms = count("SELECT COUNT(*) FROM terminology")
    s = lab_module._startup_sync()
    assert s["cutover"] is None and s["catalogue"] is None and s["open"] is False, s
    assert count("SELECT COUNT(*) FROM terminology") == terms
    for t in lab_tables:
        assert count(f"SELECT COUNT(*) FROM {t}") == 0, t

    config.LAB_ENABLED = True
    s = lab_module._startup_sync()
    assert s["cutover"] == "enabled" and s["catalogue"]["loaded"] is True, s
    added = count("SELECT COUNT(*) FROM terminology") - terms
    assert added == 4, added   # the four starter LOINC codes the seed lacks
    assert count("SELECT COUNT(*) FROM lab_reference_ranges") == 0
    assert count("SELECT COUNT(*) FROM lab_critical_limits") == 0
    s = lab_module._startup_sync()
    assert s["cutover"] is None and s["catalogue"]["loaded"] is False, s
    assert s["catalogue"]["terminology_added"] == 0

    config.LAB_ENABLED = False
    assert lab_module._startup_sync()["cutover"] == "disabled"
    assert lab_module._startup_sync()["cutover"] is None
    assert count("SELECT COUNT(*) FROM lab_module_events WHERE event='enabled'") == 1
    assert count("SELECT COUNT(*) FROM lab_module_events WHERE event='disabled'") == 1
    print("FRESH-OK", len(lab_tables))
""")


def test_a_fresh_database_stays_empty_off_and_loads_once_on(tmp_path):
    env = {k: v for k, v in os.environ.items() if not k.startswith("ORB_LAB")}
    env["ORB_DB_PATH"] = str(tmp_path / "orb_test_lab_fresh.db")
    env.pop("ORB_DB_ENCRYPTION_KEY", None)
    out = subprocess.run([sys.executable, "-c", _FRESH], cwd=str(SERVER), env=env,
                         capture_output=True, text=True, timeout=240)
    assert out.returncode == 0, out.stderr[-3000:]
    # 17 lab_ tables from migration 55, plus lab_instruments and
    # lab_instrument_test_map from migration 64 (lis-23), all empty while off.
    assert "FRESH-OK 19" in out.stdout


def test_a_loop_module_registers_itself_at_import_and_a_broken_one_is_skipped(monkeypatch, tmp_path):
    pkg = tmp_path / "lab_loop_probe_pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "good.py").write_text(textwrap.dedent("""
        import asyncio
        from services import lab_module
        STARTED = []

        async def _loop():
            STARTED.append(1)
            await asyncio.sleep(3600)

        lab_module.register_loop("good-probe", _loop, states=("on",))
    """))
    (pkg / "broken.py").write_text("raise RuntimeError('broken on purpose')\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(lab_module, "_LOOPS", [])
    monkeypatch.setattr(lab_module, "_LOOP_MODULES",
                        ("lab_loop_probe_pkg.broken", "lab_loop_probe_pkg.good"))
    monkeypatch.setattr(config, "LAB_ENABLED", True)
    with TestClient(main.app):
        pass
    import lab_loop_probe_pkg.good as good
    assert good.STARTED == [1]
