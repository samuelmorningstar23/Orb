"""Every API route refuses a caller with no session, except a named few.

WHY THIS FILE EXISTS. Two routes were probed for a 401 (tests/test_rbac.py,
/api/vigil/patients and /api/ingest/status) out of several hundred. A new
router mounted without its auth dependency, or one route in an existing router
that forgot it, would have passed the whole suite while serving its data to
anyone on the hospital network.

WHAT THIS DOES. It walks main.app.routes, makes a real request to every
method of every API route with no Authorization header and no cookie, and
requires a refusal (401 or 403). Path parameters are filled with a
placeholder, so a route that looks something up before it authenticates shows
up here too. The few routes that are public BY DESIGN are listed in
PUBLIC_ROUTES with the reason, so adding one is a reviewed decision.

THE FLOOR. ROUTE_FLOOR pins the size of the surface: if the walk ever finds
far fewer routes (a router dropped from main, a renamed attribute), a green run
would mean nothing, so it fails instead.
"""
import os
import re
import sys

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import main  # noqa: E402

#: (method, path) pairs that answer without a staff session ON PURPOSE.
PUBLIC_ROUTES: dict[tuple[str, str], str] = {
    ("POST", "/api/auth/login"): "staff sign-in: the route that issues the session",
    ("POST", "/api/auth/pin-login"): "PIN sign-in on an enrolled tablet",
    ("POST", "/api/auth/device/bind"): "tablet enrolment; checks its own one-time code",
    ("GET", "/api/auth/device"): "login screen asks whether THIS browser holds a "
                                 "device credential; discloses only that tablet's label",
    ("POST", "/api/discharge-access/verify"): "patient's discharge link: token plus "
                                              "date of birth, no staff session exists",
    ("GET", "/api/health"): "liveness probe, no clinical content",
    ("GET", "/api/health/loop"): "event-loop watchdog probe, no clinical content",
    ("GET", "/metrics"): "Prometheus scrape, counts only; ORB_METRICS_TOKEN gates it "
                         "(routes/metrics_routes.py)",
    ("GET", "/socket.io/{_path:path}"): "wrong-entrypoint diagnostic: realtime is "
                                        "served by main:socket_app, not FastAPI",
    ("POST", "/socket.io/{_path:path}"): "wrong-entrypoint diagnostic, as above",
}

#: The API surface was 516 method/path pairs on 2026-10-03.
ROUTE_FLOOR = 450


def _api_pairs():
    pairs = []
    for route in main.app.routes:
        if not isinstance(route, APIRoute):
            continue
        for method in sorted(route.methods - {"HEAD", "OPTIONS"}):
            pairs.append((method, route.path))
    return pairs


def _concrete(path: str) -> str:
    # One placeholder for every parameter: an int-typed one accepts it, and a
    # route that resolved the id before checking the caller answers 404/422
    # here, which is exactly what this test exists to catch.
    return re.sub(r"\{[^}]+\}", "1", path)


@pytest.fixture(scope="module")
def anon():
    return TestClient(main.app, raise_server_exceptions=False)


def test_the_walk_covers_the_whole_surface():
    pairs = _api_pairs()
    assert len(pairs) >= ROUTE_FLOOR, (
        f"found {len(pairs)} API method/path pairs, expected at least {ROUTE_FLOOR}: "
        "a router has dropped out of main.app"
    )


def test_every_public_route_still_exists():
    """An allowlist entry for a route that is gone would quietly bless the next
    route to take its path."""
    pairs = set(_api_pairs())
    missing = sorted(set(PUBLIC_ROUTES) - pairs)
    assert not missing, f"remove from PUBLIC_ROUTES: {missing}"


def test_every_other_route_refuses_a_caller_with_no_session(anon):
    open_routes = []
    for method, path in _api_pairs():
        if (method, path) in PUBLIC_ROUTES:
            continue
        resp = anon.request(method, _concrete(path))
        if resp.status_code not in (401, 403):
            open_routes.append(f"{method} {path} -> {resp.status_code}")
    assert not open_routes, (
        "these routes answered a caller with no session instead of refusing:\n  "
        + "\n  ".join(open_routes)
    )
