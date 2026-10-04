"""Host allow-list: services/host_gate.HostGate around main.socket_app.

A DNS-rebinding page re-points its own name at the appliance, so the browser
counts Orb as the same origin and CORS never applies. Its requests still carry
Host: attacker.example, and that is what the gate refuses, on REST and on the
socket, before any route runs. Measured 3 October 2026 before the gate existed:
Host attacker.example:8000 got 200 from /api/health and reached the login route.

These drive the real main.socket_app (FastAPI behind socketio.ASGIApp), so the
gate is proven in front of both, not only as a helper.
"""
import asyncio
import logging
from contextlib import asynccontextmanager

import pytest
import socketio
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import config
import main
from services import host_gate
from services.compliance_service import compliance_report
from services.host_gate import HostGate
from ws import realtime

LISTED = "orb.hospital.local"
ATTACKER = "attacker.example:8000"
POLL = "/socket.io/?EIO=4&transport=polling"
WS = "/socket.io/?EIO=4&transport=websocket"
# Starlette's TestClient leaves out the upgrade headers engine.io checks for.
UPGRADE = {"upgrade": "websocket", "connection": "Upgrade"}


@pytest.fixture
def gate(monkeypatch):
    """main.socket_app with a known name list and fresh counters. engine.io's
    monitor task stays off and sessions a test opened are dropped afterwards, as
    in test_ws_origin.gate."""
    monkeypatch.setattr(main.socket_app, "names", frozenset({"localhost", LISTED}))
    monkeypatch.setattr(main.socket_app, "_logged", set())
    monkeypatch.setitem(host_gate.refused, "count", 0)
    monkeypatch.setitem(host_gate.refused, "last_host", None)
    eio = realtime.sio.eio
    monkeypatch.setattr(eio, "start_service_task", False)
    before = set(eio.sockets)
    yield main.socket_app
    for sid in set(eio.sockets) - before:
        eio.sockets.pop(sid, None)
        realtime.sio.environ.pop(sid, None)


def _get(app, host, path="/api/health", **headers):
    return TestClient(app).get(path, headers={"host": host, **headers})


async def _ok_app(scope, receive, send):
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})


def _status(app, headers):
    """Status for a raw ASGI request, for Host shapes an HTTP client will not send
    (none at all, two of them)."""
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
             "method": "GET", "scheme": "http", "path": "/", "raw_path": b"/",
             "query_string": b"", "root_path": "", "headers": headers,
             "client": ("10.0.0.9", 50000), "server": ("10.0.0.1", 8000)}
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    asyncio.run(app(scope, receive, send))
    return next(m["status"] for m in sent if m["type"] == "http.response.start")


# ── It is installed where it covers everything ──────────────────────────────

def test_socket_app_is_the_gate_in_front_of_socketio():
    # A FastAPI middleware would miss /socket.io/, which socketio.ASGIApp answers first.
    assert isinstance(main.socket_app, HostGate)
    assert isinstance(main.socket_app.app, socketio.ASGIApp)


def test_main_installs_the_names_the_trust_center_reports():
    assert main.socket_app.names == config.allowed_host_names()


# ── A rebinding name is refused on every path ────────────────────────────────

def test_rebinding_host_is_refused_on_rest(gate):
    c = TestClient(gate, base_url="http://" + ATTACKER)
    r = c.get("/api/health")
    assert r.status_code == 400
    assert "ORB_ALLOWED_HOSTS" in r.text
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["cache-control"] == "no-store"
    login = c.post("/api/auth/login", json={"username": "DOC001", "password": "password"})
    assert login.status_code == 400
    assert c.get("/").status_code == 400


def test_rebinding_host_is_refused_on_the_socket(gate):
    c = TestClient(gate, base_url="http://" + ATTACKER)
    origin = {"origin": "http://" + ATTACKER}
    # Origin equals Host here, which the socket's own-origin rule would accept;
    # test_star_turns_the_check_off shows this exact handshake opens without the gate.
    assert c.get(POLL, headers=origin).status_code == 400
    with pytest.raises(WebSocketDisconnect):
        with c.websocket_connect("ws://" + ATTACKER + WS, headers={**origin, **UPGRADE}) as ws:
            ws.receive_text()


@pytest.mark.parametrize("host", [
    "attacker.example",
    "192.168.1.10.attacker.example",
    f"{LISTED}.attacker.example",
    "localhost.attacker.example",
    "testserver",
])
def test_lookalike_names_are_refused(gate, host):
    assert _get(gate, host).status_code == 400


@pytest.mark.parametrize("raw", [
    f"user@{LISTED}",
    f"{LISTED}/x",
    f"{LISTED}:http",
    f"{LISTED}:8000:9000",
    "::1",
    "[::1]x",
    "[::1",
    "",
])
def test_malformed_hosts_are_refused(raw):
    gate = HostGate(_ok_app, names={"localhost", LISTED})
    assert _status(gate, [(b"host", raw.encode())]) == 400


def test_two_host_headers_are_refused():
    gate = HostGate(_ok_app, names={"localhost", LISTED})
    assert _status(gate, [(b"host", LISTED.encode()), (b"host", b"attacker.example")]) == 400


def test_forwarded_host_is_never_trusted(gate):
    # A rebinding page is same-origin, so its fetch() can set this header freely.
    assert _get(gate, ATTACKER, **{"x-forwarded-host": LISTED}).status_code == 400
    assert _get(gate, LISTED, **{"x-forwarded-host": "attacker.example"}).status_code == 200


# ── Every way a working install is reached still passes ──────────────────────

@pytest.mark.parametrize("host", [
    "localhost", "localhost:8000", "LOCALHOST:5173", "localhost:",
    "127.0.0.1:8000", "[::1]:8000", "[::1]",
    "192.168.1.10", "10.20.30.40:443", "[fe80::1]:8000",
    LISTED, f"{LISTED}:8000", "ORB.Hospital.Local:8000", f"{LISTED}.", f"{LISTED}.:8000",
])
def test_allowed_forms_pass(gate, host):
    assert _get(gate, host).status_code == 200


def test_a_request_without_a_host_header_passes():
    # HTTP/1.0 clients; a browser always sends one, so rebinding cannot use this.
    assert _status(HostGate(_ok_app, names={"localhost"}), []) == 200


def test_listed_name_still_opens_the_socket(gate):
    own = f"{LISTED}:8000"
    c = TestClient(gate, base_url="http://" + own)
    origin = {"origin": "http://" + own}
    assert c.get(POLL, headers=origin).status_code == 200
    with c.websocket_connect("ws://" + own + WS, headers={**origin, **UPGRADE}) as ws:
        assert ws.receive_text().startswith("0")


def test_star_turns_the_check_off(gate, monkeypatch):
    monkeypatch.setattr(gate, "names", frozenset({"*"}))
    c = TestClient(gate, base_url="http://" + ATTACKER)
    assert c.get("/api/health").status_code == 200
    origin = {"origin": "http://" + ATTACKER}
    with c.websocket_connect("ws://" + ATTACKER + WS, headers={**origin, **UPGRADE}) as ws:
        assert ws.receive_text().startswith("0")


def test_vite_proxy_and_local_tools_pass_with_the_derived_names():
    """Vite proxies /api and /socket.io with changeOrigin, so the backend sees Host
    localhost:8000; Playwright, scripts and run-orb.sh use localhost or 127.0.0.1.
    Built from the real test-env config, not a hand-made list."""
    gate = HostGate(main.socket_app.app)
    for host in ("localhost:8000", "127.0.0.1:8000", "localhost:5173"):
        assert _get(gate, host).status_code == 200, host


def test_tests_on_main_app_are_not_gated():
    # ~130 test modules drive main.app with Host "testserver"; the gate sits outside it.
    assert TestClient(main.app).get("/api/health").status_code == 200


def test_lifespan_reaches_the_app_behind_the_gate():
    started = []

    @asynccontextmanager
    async def lifespan(app):
        started.append(True)
        yield

    inner = Starlette(routes=[Route("/", lambda r: PlainTextResponse("ok"))], lifespan=lifespan)
    with TestClient(HostGate(inner, names={"localhost"}), base_url="http://localhost") as c:
        assert c.get("/").status_code == 200
    assert started == [True]


# ── The names are derived from settings an install already has ───────────────

def _derive(monkeypatch, *, cors=(), public="http://localhost:5173", host="127.0.0.1",
            extra=(), own="orb-box"):
    monkeypatch.setattr(config, "CORS_ORIGINS", list(cors))
    monkeypatch.setattr(config, "PUBLIC_APP_BASE_URL", public)
    monkeypatch.setattr(config, "HOST", host)
    monkeypatch.setattr(config, "ALLOWED_HOSTS_EXTRA", list(extra))
    monkeypatch.setattr("socket.gethostname", lambda: own)
    return config.allowed_host_names()


def test_names_come_from_the_settings_an_install_already_has(monkeypatch):
    names = _derive(
        monkeypatch,
        cors=["http://localhost:5173", "https://ORB.Hospital.Local:8000",
              "http://192.168.1.10:8000", "*"],
        public="https://discharge.hospital.org/", host="0.0.0.0", own="Orb-Box.local")
    assert names == {"localhost", "orb.hospital.local", "discharge.hospital.org",
                     "orb-box.local", "orb-box"}


def test_a_bare_machine_name_gains_its_mdns_form(monkeypatch):
    assert {"orbbox", "orbbox.local"} <= _derive(monkeypatch, own="orbbox")


def test_orb_host_counts_only_when_it_is_a_name(monkeypatch):
    assert "orb.hospital.local" in _derive(monkeypatch, host="orb.hospital.local")
    assert _derive(monkeypatch, host="0.0.0.0") == _derive(monkeypatch, host="127.0.0.1")


def test_allowed_hosts_adds_names_in_any_spelling(monkeypatch):
    names = _derive(monkeypatch, extra=["Extra.Example:8443", "https://other.example/",
                                        "bare", "not a name!", "10.0.0.5"])
    assert {"extra.example", "other.example", "bare"} <= names
    assert not any(" " in n or "!" in n for n in names)
    assert "10.0.0.5" not in names  # every IP passes anyway


def test_star_in_allowed_hosts_turns_the_check_off_but_star_in_cors_does_not(monkeypatch):
    assert "*" in _derive(monkeypatch, extra=["*"])
    assert "*" not in _derive(monkeypatch, cors=["*"])


# ── Refusals are logged once per name and counted ───────────────────────────

def test_each_refused_name_is_logged_once_naming_the_setting(gate, caplog):
    caplog.set_level(logging.WARNING, logger="orb.host_gate")
    for _ in range(3):
        _get(gate, ATTACKER)
    _get(gate, "other.example")
    messages = [r.getMessage() for r in caplog.records if r.name == "orb.host_gate"]
    assert len(messages) == 2
    assert "'attacker.example:8000'" in messages[0]
    assert "ORB_ALLOWED_HOSTS" in messages[0]
    assert host_gate.refused == {"count": 4, "last_host": "other.example"}


def test_the_log_stops_naming_hosts_after_the_limit(monkeypatch, caplog):
    caplog.set_level(logging.WARNING, logger="orb.host_gate")
    monkeypatch.setattr(host_gate, "_LOG_LIMIT", 3)
    monkeypatch.setitem(host_gate.refused, "count", 0)
    gate = HostGate(_ok_app, names={"localhost"})
    for i in range(6):
        assert _status(gate, [(b"host", f"n{i}.example".encode())]) == 400
    messages = [r.getMessage() for r in caplog.records if r.name == "orb.host_gate"]
    assert len(messages) == 4
    assert "more than 3" in messages[-1]
    assert host_gate.refused["count"] == 6


# ── The Trust Center describes the installed check ──────────────────────────

def _row():
    return next(c for c in compliance_report()["controls"] if c["id"] == "host_allow_list")


def test_trust_center_lists_the_names_and_the_refusals(gate, monkeypatch):
    monkeypatch.setattr(config, "ALLOWED_HOSTS_EXTRA", [LISTED])
    _get(gate, ATTACKER)
    row = _row()
    assert row["status"] == "pass"
    assert LISTED in row["evidence"]
    assert "turned away 1 request(s)" in row["evidence"]
    assert "attacker.example:8000" in row["evidence"]
    assert "ORB_ALLOWED_HOSTS" in row["settings"]


@pytest.mark.parametrize("profile,status", [("prod", "fail"), ("dev", "warn")])
def test_trust_center_flags_the_check_turned_off(monkeypatch, profile, status):
    monkeypatch.setattr(config, "ALLOWED_HOSTS_EXTRA", ["*"])
    monkeypatch.setattr(config, "DEPLOYMENT_PROFILE", profile)
    assert _row()["status"] == status


def test_a_hostile_refused_name_cannot_put_settings_or_paths_on_the_screen(gate):
    assert _status(gate, [(b"host", "x/api/ORB_JWT_SECRET\u2014".encode())]) == 400
    evidence = _row()["evidence"]
    assert "/api/" not in evidence
    assert "ORB_" not in evidence
    assert "\u2014" not in evidence
