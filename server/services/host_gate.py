"""Orb: the Host allow-list, the fence against DNS rebinding.

A web page on attacker.example can re-point its own name at the appliance's LAN
address. The browser then counts Orb as the same origin as that page, so CORS
never applies and the page can call every route through a ward browser,
including POST /api/auth/login (password guessing from inside the hospital). The
one thing the page cannot change is the Host header: it still says
attacker.example. HostGate refuses a request whose Host is a name Orb does not
know, before FastAPI or Socket.IO sees it.

It wraps the outermost ASGI app (main.socket_app), because socketio.ASGIApp
answers /socket.io/ before any FastAPI middleware runs. One gate therefore
covers REST, the SPA, /uploads, Socket.IO polling and the WebSocket upgrade.
Tests that drive `main.app` directly (Host "testserver") never pass through it.

What passes (the names come from config.allowed_host_names()):
  * no Host header at all: HTTP/1.0 clients. A browser always sends one, so a
    rebinding page cannot use this;
  * any IP address, IPv4 or bracketed IPv6. Rebinding needs a name, and a
    browser sends an IP as the Host only when it loaded the page from that IP;
  * a listed name, compared without the port, ignoring case and a trailing dot.

X-Forwarded-Host is never read: a rebinding page is same-origin, so its fetch()
may set that header to anything.
"""
import ipaddress
import logging

import config

logger = logging.getLogger("orb.host_gate")

_BODY = (b"Orb does not answer to this address. Ask the person who installed Orb "
         b"to add it to ORB_ALLOWED_HOSTS.\n")
_HEADERS = [
    (b"content-type", b"text/plain; charset=utf-8"),
    (b"content-length", str(len(_BODY)).encode()),
    (b"cache-control", b"no-store"),
    (b"x-content-type-options", b"nosniff"),
]
# Each distinct refused Host is logged once. A scanner trying thousands of names
# would otherwise fill the log, so past this many the rest are only counted.
_LOG_LIMIT = 100
_BAD_CHARS = set("/\\@?#,; \t\r\n")

# Refusals since the process started, across every gate (production has one).
# The Trust Center prints these: the only sign a rebinding page was ever opened.
refused = {"count": 0, "last_host": None}


def parse_host(raw: str) -> tuple[str, bool] | None:
    """(host, is_ip) for a Host header value, with the port dropped. A name comes
    back lowercased without a trailing dot. None when the value is not a bare
    `host[:port]` (a path, userinfo, a bad port, an unbracketed IPv6)."""
    if not raw or any(c in _BAD_CHARS for c in raw):
        return None
    if raw.startswith("["):
        end = raw.find("]")
        port = raw[end + 1:] if end > 0 else None
        if port is None or (port and not (port[0] == ":" and port[1:].isdigit())):
            return None
        try:
            return str(ipaddress.IPv6Address(raw[1:end])), True
        except ValueError:
            return None
    host, sep, port = raw.partition(":")
    if sep and port and not port.isdigit():
        return None
    try:
        return str(ipaddress.IPv4Address(host)), True
    except ValueError:
        pass
    name = host.lower().rstrip(".")
    return (name, False) if name else None


class HostGate:
    """Pure ASGI wrapper. `names` is read per request, so tests can swap it."""

    def __init__(self, app, names=None):
        self.app = app
        self.names = frozenset(config.allowed_host_names() if names is None else names)
        self._logged: set[str] = set()
        if "*" in self.names:
            logger.warning("Host check is OFF (ORB_ALLOWED_HOSTS=*): Orb answers to any "
                           "name, so a DNS-rebinding web page can reach it.")
        else:
            logger.info("Host check: any IP address, and the names %s. ORB_ALLOWED_HOSTS "
                        "adds more.", ", ".join(sorted(self.names)))

    def allows(self, raw: str | None) -> bool:
        if "*" in self.names or raw is None:
            return True
        parsed = parse_host(raw)
        if parsed is None:
            return False
        host, is_ip = parsed
        return is_ip or host in self.names

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):  # lifespan
            await self.app(scope, receive, send)
            return
        hosts = [v.decode("latin-1") for k, v in scope.get("headers") or () if k == b"host"]
        if len(hosts) > 1:
            shown, ok = ", ".join(hosts), "*" in self.names
        else:
            shown = hosts[0] if hosts else None
            ok = self.allows(shown)
        if ok:
            await self.app(scope, receive, send)
            return

        refused["count"] += 1
        refused["last_host"] = shown
        self._log(shown, scope)
        if scope["type"] == "websocket":
            # Closing before accept makes the server answer the upgrade with 403.
            await send({"type": "websocket.close", "code": 1008})
            return
        await send({"type": "http.response.start", "status": 400, "headers": _HEADERS})
        await send({"type": "http.response.body", "body": _BODY})

    def _log(self, shown, scope) -> None:
        if shown in self._logged:
            return
        if len(self._logged) >= _LOG_LIMIT:
            if len(self._logged) == _LOG_LIMIT:
                self._logged.add("\0more")
                logger.warning("Refused requests for more than %d different Host names; "
                               "the rest are counted on the Trust Center, not logged.",
                               _LOG_LIMIT)
            return
        self._logged.add(shown)
        client = (scope.get("client") or ("an unknown address",))[0]
        logger.warning(
            "Refused a request for Host %r from %s (%r): Orb does not answer to that name. "
            "Either a web page is trying DNS rebinding, or staff or a device reach Orb by a "
            "name it was not told about; if so, add it to ORB_ALLOWED_HOSTS in .env and restart.",
            shown, client, scope.get("path"))
