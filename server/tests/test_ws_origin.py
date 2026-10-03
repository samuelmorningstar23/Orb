"""Socket.IO origin gate (ws.realtime._origin_allowed).

The page the backend serves itself may open the realtime socket without being
listed in ORB_CORS_ORIGINS; every other browser origin must still be listed.

Measured 3 October 2026: with the appliance's own origin missing from the list,
the polling handshake GET passed (Chrome sends no Origin on a same-origin GET),
the POSTs and the WebSocket upgrade got 400, and the client's retries held all
six connections Chrome allows per host, so ordinary REST calls queued for 15 to
25 s. Separately, an EMPTY list was passed to engine.io as [], which engine.io
reads as "skip the origin check", so every origin could open a socket.

These drive the real `realtime.sio` through socketio.ASGIApp, so they exercise
engine.io's own gate on both transports, not only the helper.
"""
import json

import pytest
import socketio
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from auth import SESSION_COOKIE, create_token, hash_password
from database import db_session
from ws import realtime

APPLIANCE = "orb.hospital.local:8000"
OWN = "http://" + APPLIANCE
POLL = "/socket.io/?EIO=4&transport=polling"
WS = "/socket.io/?EIO=4&transport=websocket"
# Starlette's TestClient leaves out the upgrade headers engine.io checks for.
UPGRADE = {"upgrade": "websocket", "connection": "Upgrade"}


@pytest.fixture
def gate(monkeypatch):
    """A client factory on the real sio, with the configured list WITHOUT the
    appliance origin (the measured misconfiguration). engine.io's monitor task
    is kept off so none is left bound to a closed test loop, and engine.io
    sessions and Socket.IO rooms the test opened are dropped afterwards: the
    TestClient closes its loop before socket.io runs the disconnect, so a
    connected sid would otherwise sit in user:/dept: rooms for later tests."""
    monkeypatch.setattr(realtime, "_configured_origins", ["http://localhost:5173"])
    sio, eio = realtime.sio, realtime.sio.eio
    monkeypatch.setattr(eio, "start_service_task", False)

    def connected():
        return set(sio.manager.rooms.get("/", {}).get(None, {}))

    before_eio, before_sio = set(eio.sockets), connected()
    app = socketio.ASGIApp(sio)
    yield lambda base=OWN: TestClient(app, base_url=base)
    for sid in connected() - before_sio:
        sio.manager.basic_disconnect(sid, "/")
    for sid in set(eio.sockets) - before_eio:
        eio.sockets.pop(sid, None)
        sio.environ.pop(sid, None)


def _poll(client, origin=None, **headers):
    if origin is not None:
        headers["origin"] = origin
    return client.get(POLL, headers=headers).status_code


def _ws_url(client):
    # websocket_connect ignores base_url (it joins onto ws://testserver), so
    # the Host the server sees comes only from an absolute URL.
    base = str(client.base_url).rstrip("/")
    return base.replace("http", "ws", 1) + WS


def _ws_opens(client, origin, **headers):
    try:
        with client.websocket_connect(_ws_url(client), headers={"origin": origin, **UPGRADE, **headers}) as ws:
            return ws.receive_text().startswith("0")
    except WebSocketDisconnect:
        return False


def _ws_connect_reply(client, origin, **headers):
    """Open the WebSocket, send a Socket.IO CONNECT for "/", return the reply:
    '40{...}' accepted, '44{...}' refused by connect()."""
    with client.websocket_connect(_ws_url(client), headers={"origin": origin, **UPGRADE, **headers}) as ws:
        assert ws.receive_text().startswith("0")
        ws.send_text("40")
        return ws.receive_text()


def _mk_doctor(uid):
    with db_session() as db:
        db.execute(
            "INSERT OR IGNORE INTO users (id, name, role, password_hash) VALUES (?,?,?,?)",
            (uid, f"User {uid}", "doctor", hash_password("secretpass")),
        )
        db.execute("UPDATE users SET token_version = 0, is_active = 1 WHERE id = ?", (uid,))
    return create_token(uid, "doctor", token_version=0)


# ── The server is built with the callable gate ────────────────────────────────

def test_sio_uses_the_callable_gate_not_a_list():
    # A list (or []) here brings back one of the two measured failures: a list
    # without the appliance origin starves the browser, and [] disables the check.
    assert realtime.sio.eio.cors_allowed_origins is realtime._origin_allowed


# ── The appliance's own page ──────────────────────────────────────────────────

def test_own_origin_opens_both_transports_without_being_listed(gate):
    with gate() as c:
        assert _poll(c, OWN) == 200
        assert _ws_opens(c, OWN)


def test_own_origin_polling_post_is_accepted(gate):
    """The measured shape: the Origin-less GET always passed, the POST that
    follows (which carries Origin) was the one rejected."""
    with gate() as c:
        r = c.get(POLL)  # Chrome sends no Origin on a same-origin GET
        assert r.status_code == 200
        sid = json.loads(r.text[1:])["sid"]
        post = c.post(f"{POLL}&sid={sid}", content="40", headers={"origin": OWN})
        assert post.status_code == 200, post.text
        foreign = c.post(f"{POLL}&sid={sid}", content="40", headers={"origin": "https://evil.example"})
        assert foreign.status_code == 400


def test_listed_origin_still_passes(gate):
    # Vite on :5173 proxies with changeOrigin, so Host is the backend's and the
    # page is only recognised through the list.
    with gate() as c:
        assert _poll(c, "http://localhost:5173") == 200
        assert _ws_opens(c, "http://localhost:5173")


# ── Foreign origins stay out ──────────────────────────────────────────────────

@pytest.mark.parametrize("origin", [
    "https://evil.example",
    # Same SITE as the appliance, so a SameSite=Lax cookie would go with it:
    # the origin check is the only gate for a sibling host.
    "http://intranet.hospital.local:8000",
    # Same host, another port. Cookies are not port-scoped.
    "http://orb.hospital.local:5173",
    "http://orb.hospital.local",
    "null",
])
def test_foreign_origin_is_rejected_on_both_transports(gate, origin):
    with gate() as c:
        assert _poll(c, origin) == 400
        assert not _ws_opens(c, origin)


def test_empty_cors_list_no_longer_admits_every_origin(gate, monkeypatch):
    # Before: [] went to engine.io, which skipped the check, and this was 200.
    monkeypatch.setattr(realtime, "_configured_origins", [])
    with gate() as c:
        assert _poll(c, "https://evil.example") == 400
        assert not _ws_opens(c, "https://evil.example")
        assert _poll(c, OWN) == 200


def test_foreign_origin_with_a_valid_session_cookie_is_rejected(gate):
    """Cross-site WebSocket hijacking: the browser attaches the clinician's
    cookie, and only the origin gate stands in the way."""
    cookie = f"{SESSION_COOKIE}={_mk_doctor('WS-ORIGIN-CSWSH')}"
    with gate() as c:
        assert _poll(c, "http://intranet.hospital.local:8000", cookie=cookie) == 400
        assert not _ws_opens(c, "http://intranet.hospital.local:8000", cookie=cookie)


# ── TLS shapes ────────────────────────────────────────────────────────────────

def test_direct_tls_page_is_its_own_origin(gate):
    # uvicorn serving ORB_TLS_CERT itself. engine.io's built-in same-origin mode
    # rejects this one: its ASGI driver assumes http without X-Forwarded-Proto.
    with gate("https://orb.hospital.local") as c:
        assert _poll(c, "https://orb.hospital.local") == 200
        assert _ws_opens(c, "https://orb.hospital.local")


def test_https_page_behind_a_proxy_that_sends_no_forwarded_proto(gate):
    # The proxy terminates TLS and forwards plain http with Host preserved.
    with gate("http://orb.hospital.local") as c:
        assert _poll(c, "https://orb.hospital.local") == 200
        assert _ws_opens(c, "https://orb.hospital.local")


def test_http_page_on_an_https_connection_is_a_downgrade(gate):
    with gate("https://orb.hospital.local") as c:
        assert _poll(c, "http://orb.hospital.local") == 400
        assert not _ws_opens(c, "http://orb.hospital.local")
    with gate("http://orb.hospital.local") as c:
        assert _poll(c, "http://orb.hospital.local", **{"x-forwarded-proto": "https"}) == 400


# ── Authentication is still the gate behind the origin ─────────────────────────

def test_own_origin_with_session_cookie_connects(gate):
    uid = "WS-ORIGIN-OK"
    cookie = f"{SESSION_COOKIE}={_mk_doctor(uid)}"
    with gate() as c:
        reply = _ws_connect_reply(c, OWN, cookie=cookie)
    assert reply.startswith("40"), reply
    assert "sid" in json.loads(reply[2:])
    # connect() ran to the end: the authenticated user got their personal room.
    assert realtime.sio.manager.rooms["/"].get(f"user:{uid}")


def test_dns_rebinding_page_passes_the_origin_gate_but_cannot_authenticate(gate):
    """attacker.example now resolves to the appliance, so Host and Origin match
    and the engine.io handshake opens. The browser files cookies by host name,
    so the real orb_session cookie is never sent here, and connect() refuses."""
    with gate("http://attacker.example:8000") as c:
        reply = _ws_connect_reply(c, "http://attacker.example:8000")
    assert reply.startswith("44"), reply


# ── The rule itself ───────────────────────────────────────────────────────────

def _environ(host, scheme="http", **extra):
    return {"HTTP_HOST": host, "asgi.scope": {"scheme": scheme}, **extra}


@pytest.mark.parametrize("origin,environ,allowed", [
    ("http://orb.local:8000", _environ("orb.local:8000"), True),
    ("http://ORB.Local:8000", _environ("orb.local:8000"), True),
    ("https://orb.local", _environ("orb.local:443", "https"), True),
    ("https://orb.local", _environ("orb.local", "wss"), True),
    ("https://orb.local", _environ("orb.local"), True),            # TLS proxy, no XFP
    ("https://orb.local:8443", _environ("orb.local:8443"), True),  # nginx $http_host keeps the port
    ("https://orb.local:8443", _environ("orb.local"), False),      # nginx $host drops it (runbook F11)
    ("http://[::1]:8000", _environ("[::1]:8000"), True),
    ("http://orb.local", _environ("orb.local", "https"), False),    # downgrade
    ("http://orb.local", _environ("orb.local", "ws", HTTP_X_FORWARDED_PROTO="https"), False),
    ("http://orb.local:8001", _environ("orb.local:8000"), False),
    ("http://orb.local", _environ("orb.local:8000"), False),
    ("http://other.local:8000", _environ("orb.local:8000"), False),
    ("http://orb.local:8000/", _environ("orb.local:8000"), False),
    ("http://user@orb.local:8000", _environ("orb.local:8000"), False),
    ("ftp://orb.local:8000", _environ("orb.local:8000"), False),
    ("http://orb.local:8000", _environ("orb.local:8000,evil.example"), False),
    ("http://orb.local:8000", _environ("evil@orb.local:8000"), False),
    ("http://orb.local:8000", _environ("orb.local:notaport"), False),
    ("http://orb.local:8000", {}, False),                            # no Host header
    ("null", _environ("orb.local:8000"), False),
    ("", _environ("orb.local:8000"), False),
    (None, _environ("orb.local:8000"), False),
])
def test_origin_rule(monkeypatch, origin, environ, allowed):
    monkeypatch.setattr(realtime, "_configured_origins", [])
    assert realtime._origin_allowed(origin, environ) is allowed


def test_configured_list_is_exact_and_needs_no_environ(monkeypatch):
    monkeypatch.setattr(realtime, "_configured_origins", ["http://localhost:5173"])
    assert realtime._origin_allowed("http://localhost:5173") is True
    assert realtime._origin_allowed("http://localhost:5174") is False
    # Without an environ there is nothing to compare against: list only.
    assert realtime._origin_allowed("http://orb.local:8000") is False
