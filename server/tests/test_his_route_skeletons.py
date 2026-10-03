"""The hospital-system route skeletons (his-1, wave 1, 3 October 2026).

Six route homes are registered once in main.py so later increments add
endpoints inside them and never edit main.py: the packages routes/adt,
routes/lab and routes/pharmacy_store, and the files opd_routes,
registration_routes and schedule_routes. Each sits behind its opt-in switch:

  * a caller with no session gets 401, switch on or off;
  * a signed-in caller gets 404 (module_disabled or feature_disabled, in plain
    words) while the switch is off;
  * with the switch on, the route's own permission decides (200 or 403).

The structural test walks main.app and fails if ANY route under one of these
prefixes lacks its gate, so an endpoint added in a later wave cannot escape it.
"""
import asyncio
import inspect
import uuid

import pytest
from fastapi import HTTPException
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

import config
import main
from auth import create_token, hash_password
from database import db_session

client = TestClient(main.app)

# prefix -> the gate every route under it must carry (by dependency name)
GATES = {
    "/api/adt": ("require_adt_module",),
    "/api/pharmacy-store": ("require_pharmacy_store_module",),
    "/api/lab": ("require_lab_module", "require_lab_critical_surface",
                 "require_lab_module_without_session"),
    "/api/opd": ("require_opd_feature",),
    "/api/registration": ("require_registration_feature",),
    "/api/schedule": ("require_registration_feature",),
}

# meta path -> (the switch constant(s) that open it, a role that holds its
#               permission, a role that does not, the 404 error code)
META = {
    "/api/adt/meta": (("ADT_ENABLED",), "admin", "nurse", "module_disabled"),
    "/api/pharmacy-store/meta": (("PHARMACY_STORE_ENABLED",), "pharmacist", "doctor",
                                 "module_disabled"),
    "/api/lab/meta": (("LAB_ENABLED",), "pathologist", "nurse", "module_disabled"),
    "/api/opd/meta": (("REGISTRATION_ENABLED", "OPD_ENABLED"), "doctor", "admin",
                      "feature_disabled"),
    "/api/registration/meta": (("REGISTRATION_ENABLED",), "receptionist", "nurse",
                               "feature_disabled"),
    "/api/schedule/meta": (("REGISTRATION_ENABLED",), "admin", "pharmacist",
                           "feature_disabled"),
}


def _routes_under(prefix):
    return [r for r in main.app.routes
            if isinstance(r, APIRoute) and (r.path == prefix or r.path.startswith(prefix + "/"))]


def _hdr(role):
    uid = f"SK{uuid.uuid4().hex[:6].upper()}"
    dept = "Cardiology" if role in ("doctor", "nurse", "ward_clerk") else None
    with db_session() as db:
        db.execute(
            "INSERT INTO users (id, name, role, department, password_hash) VALUES (?, ?, ?, ?, ?)",
            (uid, f"{role} {uid}", role, dept, hash_password("password")))
    return {"Authorization": f"Bearer {create_token(uid, role)}"}


# ── 1. Structure ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("prefix", sorted(GATES))
def test_every_route_under_a_his_prefix_carries_its_gate_first(prefix):
    routes = _routes_under(prefix)
    assert routes, f"nothing is mounted under {prefix}: the router dropped out of main.py"
    for route in routes:
        names = [d.call.__name__ for d in route.dependant.dependencies]
        assert names and any(names[0].startswith(g) for g in GATES[prefix]), (
            f"{sorted(route.methods)} {route.path} does not start with its switch gate: {names}")


@pytest.mark.parametrize("prefix", sorted(GATES))
def test_handlers_are_plain_def(prefix):
    """No sync SQLite on the event loop: a handler is a plain def (FastAPI
    threadpools it). An async handler must use run_db; none exists yet."""
    for route in _routes_under(prefix):
        assert not inspect.iscoroutinefunction(route.endpoint), route.path


def test_each_router_is_registered_once():
    paths = [(m, r.path) for r in main.app.routes if isinstance(r, APIRoute)
             for m in r.methods if any(r.path.startswith(p) for p in GATES)]
    assert len(paths) == len(set(paths))
    for meta in META:
        assert any(p == meta for _, p in paths), meta


def test_the_packages_expose_one_router_per_concern():
    from routes import adt, lab, pharmacy_store
    for pkg, subs in (
        (adt, ("master", "board", "moves", "presence", "end_stay", "reservations", "census")),
        (lab, ("master", "orders", "specimens", "results", "criticals", "reports", "tat",
               "analysers")),
        (pharmacy_store, ("masters", "purchasing", "stock", "lots", "transfers", "counts",
                          "ndps", "ward", "returns", "vendor_returns")),
    ):
        for sub in subs:
            assert hasattr(getattr(pkg, sub), "router"), f"{pkg.__name__}.{sub}"


# ── 2. Behaviour: off, on, anonymous ──────────────────────────────────────────

@pytest.mark.parametrize("path", sorted(META))
def test_no_session_is_401_with_the_switch_off(path):
    assert client.get(path).status_code == 401


@pytest.mark.parametrize("path", sorted(META))
def test_no_session_is_401_with_the_switch_on(monkeypatch, path):
    for const in META[path][0]:
        monkeypatch.setattr(config, const, True)
    assert client.get(path).status_code == 401


@pytest.mark.parametrize("path", sorted(META))
def test_signed_in_holder_gets_404_while_the_switch_is_off(path):
    _, holder, _, error = META[path]
    resp = client.get(path, headers=_hdr(holder))
    assert resp.status_code == 404, resp.text
    detail = resp.json()["detail"]
    assert detail["error"] == error
    assert "switched off on this site" in detail["message"]


@pytest.mark.parametrize("path", sorted(META))
def test_with_the_switch_on_the_permission_decides(monkeypatch, path):
    consts, holder, other, _ = META[path]
    for const in consts:
        monkeypatch.setattr(config, const, True)
    ok = client.get(path, headers=_hdr(holder))
    assert ok.status_code == 200, ok.text
    assert client.get(path, headers=_hdr(other)).status_code == 403


def test_opd_stays_off_without_registration(monkeypatch):
    monkeypatch.setattr(config, "OPD_ENABLED", True)
    resp = client.get("/api/opd/meta", headers=_hdr("doctor"))
    assert resp.status_code == 404
    assert resp.json()["detail"]["feature"] == "opd"


def test_meta_carries_no_patient_data(monkeypatch):
    monkeypatch.setattr(config, "ADT_ENABLED", True)
    body = client.get("/api/adt/meta", headers=_hdr("admin")).json()
    assert body["module"] == "adt" and body["state"] == "on"
    assert set(body) == {"module", "state", "features", "settings"}


# ── 3. The laboratory's draining state ────────────────────────────────────────

def test_lab_meta_is_reachable_while_the_lab_drains(monkeypatch):
    monkeypatch.setattr(config, "_MODULE_DRAIN_CHECKS", {"lab": lambda: True})
    resp = client.get("/api/lab/meta", headers=_hdr("lab_technician"))
    assert resp.status_code == 200, resp.text
    assert resp.json()["state"] == "draining"


def _run(gate, **kwargs):
    return asyncio.run(gate(**kwargs))


def test_a_strict_lab_gate_refuses_new_work_while_draining(monkeypatch):
    from routes.opt_in_gates import require_lab_module
    monkeypatch.setattr(config, "_MODULE_DRAIN_CHECKS", {"lab": lambda: True})
    with pytest.raises(HTTPException) as exc:
        _run(require_lab_module(), user={})
    assert exc.value.status_code == 404
    _run(require_lab_module(allow_draining=True), user={})  # finishing work is allowed
    monkeypatch.setattr(config, "LAB_ENABLED", True)
    _run(require_lab_module(), user={})


def test_the_critical_surface_stays_open_for_interfaced_criticals(monkeypatch):
    from routes.opt_in_gates import require_lab_critical_surface
    monkeypatch.setattr(config, "_MODULE_DRAIN_CHECKS", {})
    with pytest.raises(HTTPException):
        _run(require_lab_critical_surface, user={})
    monkeypatch.setattr(config, "LAB_CRITICAL_INTERFACED_ENABLED", True)
    _run(require_lab_critical_surface, user={})
    monkeypatch.setattr(config, "LAB_CRITICAL_INTERFACED_ENABLED", False)
    monkeypatch.setattr(config, "_MODULE_DRAIN_CHECKS", {"lab": lambda: True})
    _run(require_lab_critical_surface, user={})


def test_a_gate_without_session_exists_for_key_authenticated_intake():
    from routes.opt_in_gates import require_lab_module
    gate = require_lab_module(require_session=False)
    assert gate.__name__ == "require_lab_module_without_session"
    assert "user" not in inspect.signature(gate).parameters


def test_gates_refuse_unknown_ids():
    from routes.opt_in_gates import feature_gate, module_gate
    with pytest.raises(ValueError):
        module_gate("billing")
    with pytest.raises(ValueError):
        feature_gate("no_such_feature")
