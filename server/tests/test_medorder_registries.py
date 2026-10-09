"""The medication-order store in every registry that walks the schema, its two
switches, and its route package (medorders-1, W4-E).

Pinned here:
  * retention: both new tables under prescription_and_dispensing, naming
    columns they really have; neither is kept after an erasure (ruling MO-R13
    (a)), and the erase plan deletes from both without meeting a trigger;
  * the development reseed wipes both with their patients and protects neither;
  * the DPDP export reads both as single-patient tables;
  * ORB_MED_ORDER_STORE and ORB_MED_ORDER_LIFECYCLE: both off by default, both
    on /api/health, the lifecycle off whenever the store is, and config refusing
    to load with the lifecycle on and the store off;
  * /api/medorders: registered in main.py, every route behind the store gate
    first and a plain def, the verify, lifecycle and supply routers behind the
    lifecycle gate as well; 401 without a session, 404 while a switch is off,
    then the route's own permission decides.
"""
from __future__ import annotations

import ast
import inspect
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from fastapi import APIRouter, Depends, FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

import config
from database import db_session

SERVER_DIR = Path(__file__).resolve().parent.parent
NEW_TABLES = ("med_orders", "med_order_events")
STORE_GATE = "require_med_order_store_feature"
LIFECYCLE_GATE = "require_med_order_lifecycle_feature"


class _Rollback(Exception):
    pass


# ── 1. Retention and erasure ──────────────────────────────────────────────────

def test_both_tables_are_retained_as_prescription_and_dispensing():
    from services.consent_service import _RETENTION_SOURCES
    rows = {t: (p, c) for t, p, c in _RETENTION_SOURCES["prescription_and_dispensing"]}
    with db_session() as db:
        for table in NEW_TABLES:
            assert rows[table] == ("patient_id", "created_at"), table
            cols = {r[1] for r in db.execute(f"PRAGMA table_info({table})")}
            assert {"patient_id", "created_at"} <= cols, table
    other = {t for cat, entries in _RETENTION_SOURCES.items()
             if cat != "prescription_and_dispensing" for t, _, _ in entries}
    assert not (set(NEW_TABLES) & other), "counted twice in the retention review"


def test_neither_table_is_kept_after_an_erasure():
    from routes.admin_routes import _ERASE_KEEP_TABLES
    from services.consent_service import ERASURE_RETAINED_TABLES
    for table in NEW_TABLES:
        assert table not in ERASURE_RETAINED_TABLES
        assert table not in _ERASE_KEEP_TABLES


def test_the_erase_plan_deletes_the_ledger_and_meets_no_trigger():
    from routes.admin_routes import _ERASE_UNLINKED_DELETES, _build_erase_plan
    pid = f"MOER{uuid.uuid4().hex[:8].upper()}"
    try:
        with db_session() as db:
            planned = {t for t, _, _ in _build_erase_plan(db)}
            assert set(NEW_TABLES) <= planned
            db.execute("INSERT INTO patients (id, name, mrn, status) VALUES (?, 'Erase', ?, "
                       "'discharged')", (pid, f"MRN-{pid}"))
            db.execute(
                "INSERT INTO med_orders (id, patient_id, status, source, drug_name, entered_by, "
                "created_at, updated_at) VALUES (?, ?, 'stopped', 'chart', 'Aspirin', 'D001', "
                "'t', 't')", (f"MO-{pid}", pid))
            db.execute(
                "INSERT INTO med_order_events (med_order_id, patient_id, kind, created_at) "
                "VALUES (?, ?, 'created', 't')", (f"MO-{pid}", pid))
            for _table, sql, n in _ERASE_UNLINKED_DELETES + _build_erase_plan(db):
                db.execute(sql, (pid,) * n)
            for table in NEW_TABLES:
                left = db.execute(f"SELECT COUNT(*) FROM {table} WHERE patient_id = ?",
                                  (pid,)).fetchone()[0]
                assert left == 0, table
            raise _Rollback
    except _Rollback:
        pass


def test_the_reseed_wipes_both_and_protects_neither():
    sys.path.insert(0, str(SERVER_DIR / "scripts"))
    from scripts.reset_db_and_seed import delete_protected_tables, patient_scoped_tables
    with db_session() as db:
        wiped = set(patient_scoped_tables(db))
        protected = set(delete_protected_tables(db))
    assert set(NEW_TABLES) <= wiped
    assert not (set(NEW_TABLES) & protected)


def test_the_dpdp_export_reads_both_as_single_patient_tables():
    from services import consent_service as cs
    with db_session() as db:
        included, refused, _ = cs._patient_tables(db)
    for table in NEW_TABLES:
        assert table in included, table
        assert table not in refused


# ── 2. The switches ───────────────────────────────────────────────────────────

def test_both_switches_are_registered_and_off_by_default():
    assert config.FEATURE_FLAGS["med_order_store"] == "ORB_MED_ORDER_STORE"
    assert config.FEATURE_FLAGS["med_order_lifecycle"] == "ORB_MED_ORDER_LIFECYCLE"
    assert config.MED_ORDER_STORE_ENABLED is False
    assert config.MED_ORDER_LIFECYCLE_ENABLED is False
    flags = config.feature_flags()
    assert flags["med_order_store"] is False and flags["med_order_lifecycle"] is False


def test_the_lifecycle_is_off_whenever_the_store_is(monkeypatch):
    monkeypatch.setattr(config, "MED_ORDER_LIFECYCLE_ENABLED", True)
    assert config.feature_enabled("med_order_lifecycle") is False
    monkeypatch.setattr(config, "MED_ORDER_STORE_ENABLED", True)
    assert config.feature_enabled("med_order_lifecycle") is True
    assert config.feature_enabled("med_order_store") is True


def _import_config(env: dict) -> subprocess.CompletedProcess:
    base = {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", ""),
            "ORB_DEPLOYMENT_PROFILE": "dev",
            "ORB_JWT_SECRET": "probe-only-secret-0123456789abcdef"}
    return subprocess.run(
        [sys.executable, "-c",
         "import json, config; f = config.feature_flags(); "
         "print('FLAGS:' + json.dumps({k: f[k] for k in ('med_order_store', 'med_order_lifecycle')}))"],
        cwd=str(SERVER_DIR), env={**base, **env}, capture_output=True, text=True, timeout=120)


def test_config_refuses_to_load_with_the_lifecycle_on_and_the_store_off():
    proc = _import_config({"ORB_MED_ORDER_LIFECYCLE": "1"})
    assert proc.returncode != 0
    assert "ORB_MED_ORDER_LIFECYCLE is on but ORB_MED_ORDER_STORE is off" in proc.stderr


@pytest.mark.parametrize("env,expected", [
    ({}, '{"med_order_store": false, "med_order_lifecycle": false}'),
    ({"ORB_MED_ORDER_STORE": "yes"}, '{"med_order_store": true, "med_order_lifecycle": false}'),
    ({"ORB_MED_ORDER_STORE": "1", "ORB_MED_ORDER_LIFECYCLE": "on"},
     '{"med_order_store": true, "med_order_lifecycle": true}'),
    ({"ORB_MED_ORDER_STORE": "maybe", "ORB_MED_ORDER_LIFECYCLE": ""},
     '{"med_order_store": false, "med_order_lifecycle": false}'),
])
def test_config_reads_the_switches_from_the_environment(env, expected):
    proc = _import_config(env)
    assert proc.returncode == 0, proc.stderr
    assert "FLAGS:" + expected in proc.stdout


def test_health_publishes_both_switches_off():
    import main
    with TestClient(main.app) as client:
        body = client.get("/api/health").json()
    assert body["features"]["med_order_store"] is False
    assert body["features"]["med_order_lifecycle"] is False


def test_health_follows_the_store_switch(monkeypatch):
    import main
    monkeypatch.setattr(config, "MED_ORDER_STORE_ENABLED", True)
    with TestClient(main.app) as client:
        body = client.get("/api/health").json()
    assert body["features"]["med_order_store"] is True
    assert body["features"]["med_order_lifecycle"] is False


# ── 3. The route package ──────────────────────────────────────────────────────

def _routes_under(app, prefix="/api/medorders"):
    return [r for r in app.routes
            if isinstance(r, APIRoute) and (r.path == prefix or r.path.startswith(prefix + "/"))]


def test_main_registers_the_package_once():
    tree = ast.parse((SERVER_DIR / "main.py").read_text())
    imports = [n for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)
               and n.module == "routes.medorders"
               and any(a.name == "router" and a.asname == "medorders_router" for a in n.names)]
    includes = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
                and getattr(n.func, "attr", None) == "include_router"
                and any(isinstance(a, ast.Name) and a.id == "medorders_router" for a in n.args)]
    assert len(imports) == 1 and len(includes) == 1


def test_every_mounted_route_starts_with_the_store_gate_and_is_a_plain_def():
    import main
    for route in _routes_under(main.app):
        names = [d.call.__name__ for d in route.dependant.dependencies]
        # The Orb Ward Record refusal comes first, declared at include_router
        # in main.py (services/drug_chart, 9 October 2026); then the gates.
        assert names[:1] == ["require_drug_chart"], (route.path, names)
        names = names[1:]
        assert names and names[0] == STORE_GATE, (sorted(route.methods), route.path, names)
        assert not inspect.iscoroutinefunction(route.endpoint), route.path
        module = route.endpoint.__module__
        if module.split(".")[-1] in ("verify", "lifecycle", "supply"):
            assert names[1] == LIFECYCLE_GATE, (route.path, names)


def test_the_package_routers_carry_their_gates():
    from routes import medorders
    from routes.medorders import core, lifecycle, supply, verify
    assert medorders.router.prefix == "/api/medorders"
    assert [d.dependency.__name__ for d in medorders.router.dependencies] == [STORE_GATE]
    assert core.router.dependencies == []
    for sub in (verify, lifecycle, supply):
        assert [d.dependency.__name__ for d in sub.router.dependencies] == [LIFECYCLE_GATE], sub


def _hdr(role):
    from auth import create_token, hash_password
    uid = f"MO{uuid.uuid4().hex[:6].upper()}"
    dept = "Cardiology" if role in ("doctor", "nurse") else None
    with db_session() as db:
        db.execute("INSERT INTO users (id, name, role, department, password_hash) "
                   "VALUES (?, ?, ?, ?, ?)", (uid, f"{role} {uid}", role, dept,
                                              hash_password("password")))
    return {"Authorization": f"Bearer {create_token(uid, role)}"}


@pytest.fixture
def probe_client():
    """One probe endpoint in core and one in lifecycle, added the way a later
    wave adds endpoints, mounted under a package router built exactly like
    routes/medorders/__init__.py on a scratch app. Removed afterwards."""
    from permissions import MEDICATION_PRESCRIBE, PATIENT_READ_ALL, require_permission
    from routes import medorders
    from routes.medorders import core, lifecycle

    @core.router.get("/__gate_probe_read")
    def _read(user: dict = Depends(require_permission(PATIENT_READ_ALL))):
        return {"ok": True}

    @lifecycle.router.post("/__gate_probe_write")
    def _write(user: dict = Depends(require_permission(MEDICATION_PRESCRIBE))):
        return {"ok": True}

    added = [(core.router, core.router.routes[-1]), (lifecycle.router, lifecycle.router.routes[-1])]
    try:
        pkg = APIRouter(prefix=medorders.router.prefix,
                        dependencies=list(medorders.router.dependencies))
        pkg.include_router(core.router)
        pkg.include_router(lifecycle.router)
        app = FastAPI()
        app.include_router(pkg)
        for route in _routes_under(app):
            names = [d.call.__name__ for d in route.dependant.dependencies]
            assert names[0] == STORE_GATE, names
        yield TestClient(app)
    finally:
        for router, route in added:
            router.routes.remove(route)


READ, WRITE = "/api/medorders/__gate_probe_read", "/api/medorders/__gate_probe_write"


def test_no_session_is_401_whatever_the_switches(probe_client, monkeypatch):
    assert probe_client.get(READ).status_code == 401
    monkeypatch.setattr(config, "MED_ORDER_STORE_ENABLED", True)
    monkeypatch.setattr(config, "MED_ORDER_LIFECYCLE_ENABLED", True)
    assert probe_client.get(READ).status_code == 401
    assert probe_client.post(WRITE).status_code == 401


def test_everything_is_404_while_the_store_is_off(probe_client, monkeypatch):
    monkeypatch.setattr(config, "MED_ORDER_LIFECYCLE_ENABLED", True)  # the store gate wins
    for method, path in (("get", READ), ("post", WRITE)):
        resp = getattr(probe_client, method)(path, headers=_hdr("doctor"))
        assert resp.status_code == 404, resp.text
        detail = resp.json()["detail"]
        assert (detail["error"], detail["feature"]) == ("feature_disabled", "med_order_store")
        assert "switched off on this site" in detail["message"]


def test_record_mode_opens_reads_but_not_lifecycle_writes(probe_client, monkeypatch):
    monkeypatch.setattr(config, "MED_ORDER_STORE_ENABLED", True)
    assert probe_client.get(READ, headers=_hdr("doctor")).status_code == 200
    resp = probe_client.post(WRITE, headers=_hdr("doctor"))
    assert resp.status_code == 404
    assert resp.json()["detail"]["feature"] == "med_order_lifecycle"


def test_with_both_switches_on_the_permission_decides(probe_client, monkeypatch):
    monkeypatch.setattr(config, "MED_ORDER_STORE_ENABLED", True)
    monkeypatch.setattr(config, "MED_ORDER_LIFECYCLE_ENABLED", True)
    assert probe_client.post(WRITE, headers=_hdr("doctor")).status_code == 200
    assert probe_client.post(WRITE, headers=_hdr("nurse")).status_code == 403
    assert probe_client.get(READ, headers=_hdr("pharmacist")).status_code == 403
