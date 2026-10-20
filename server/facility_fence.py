"""Orb: the runtime facility fence (multisite-1, W7-J, 9 October 2026).

Isolation between facilities is keyed on data (Samuel's ruling M2, 3 October
2026): the moment a second facilities row exists, active or not, every screen
must keep each facility's patients apart, whatever ORB_MULTI_SITE says. Until
the scope sweep (multisite-5a to multisite-14) has taught every screen to do
that, a screen that has not been taught must not answer at all. This fence
makes an unfinished screen an outage, never a disclosure:

  * While services.facility_service.multi_facility_data() is False (every
    install today: one facility), the fence does nothing. No query, no audit
    row, no header: a cache hit is a clock read.
  * While it is True, a request to a fenced route gets 503 with the words in
    FENCE_MESSAGE, and an authorization audit row 'facility_fence_blocked' is
    written (at most one per address and route group a minute, with the count
    of the refusals in between, because the audit table is append-only and a
    ward browser polls).

FACILITY_BLIND_SURFACES is the list of route prefixes not yet swept. It
shipped as the sentinel '*' (every patient route fenced). Since multisite-5b
(20 October 2026) it is the list generated from the SQL-site registry
(server/tests/facility_sql_sites/, `python -m tests.facility_sql_sites`),
which tests/test_facility_sql_registry.py pins equal to the registry's blind
route groups, and the list only ever shrinks. FACILITY_CLEARED_SURFACES are
the groups the registry has cleared. While the blind list is not empty, a path
under no blind, cleared or open prefix (a route group added after the registry
was generated) is fenced as well: a screen nobody has classified is an
unfinished one. The create gate (multisite-2) refuses a second facility while
the blind list is not empty.

What is never fenced (OPEN_SURFACES): signing in and the session (/api/auth),
liveness and the switch board (/api/health), facility identity
(/api/facilities) and the operations status page (/api/ops), which explains
the fence to the operator. Paths outside FENCED_ROOTS (the built screens and
their files) carry no patient data and are never fenced, so the 503 reaches a
screen that can show it.

Two places install it (server/main.py): inside the FastAPI app for /api/ and
/uploads/ (FacilityFence, so the CORS and security headers wrap the 503), and
in the Socket.IO app for /socket.io/ (fenced_socketio_app), which answers
before any FastAPI middleware. A socket that connected before the second row
appeared keeps its rooms until multisite-10 gives sockets facility rooms.
"""

from __future__ import annotations

import logging
import posixpath
import time
import uuid

logger = logging.getLogger("orb.facility_fence")

#: The sentinel: every fenced root is blind.
SENTINEL = "*"

#: Route prefixes not yet swept for facility scope (SENTINEL until
#: multisite-5b). Generated from the route group rows of the SQL-site registry
#: (server/tests/facility_sql_sites/<area>.py, SURFACES); each owning sweep
#: removes the groups it clears. Do not edit by hand: change the rows and
#: regenerate (`python -m tests.facility_sql_sites` from server/).
FACILITY_BLIND_SURFACES = (
    "/api/abdm",
    "/api/admin",
    "/api/admissions",
    "/api/adt",
    "/api/appointments",
    "/api/audit",
    "/api/billing",
    "/api/biomedical",
    "/api/bridge",
    "/api/bundles",
    "/api/capacity-sim",
    "/api/chart",
    "/api/command-center",
    "/api/dietary",
    "/api/discharge-access",
    "/api/downtime",
    "/api/emar",
    "/api/fhir",
    "/api/flight-recorder",
    "/api/forecast",
    "/api/frontdesk",
    "/api/helix",
    "/api/housekeeping",
    "/api/ingest",
    "/api/ipass",
    "/api/lab",
    "/api/medorders",
    "/api/nabh",
    "/api/nurse",
    "/api/opd",
    "/api/orders",
    "/api/patients",
    "/api/payer",
    "/api/payments",
    "/api/pharmacy",
    "/api/pharmacy-store",
    "/api/procurement",
    "/api/registration",
    "/api/relay",
    "/api/sage",
    "/api/schedule",
    "/api/scribe",
    "/api/surgery",
    "/api/vigil",
    "/api/ward-record",
    "/api/workforce",
    "/socket.io",
)

#: Route prefixes the registry has cleared: swept (each facility's patients
#: kept apart) or carrying no patient data. Generated like the blind list.
FACILITY_CLEARED_SURFACES = (
    "/api/coding",
    "/api/consent",
    "/api/lens",
    "/api/pulse",
    "/api/search",
    "/uploads",
)

#: Of the cleared prefixes, those that carry no patient data at all.
NO_PATIENT_DATA_SURFACES = ("/api/pulse",)

#: The roots the fence looks at. Anything else (the screens and their files)
#: carries no patient data.
HTTP_ROOTS = ("/api", "/uploads")
SOCKET_ROOTS = ("/socket.io",)
FENCED_ROOTS = HTTP_ROOTS + SOCKET_ROOTS

#: Never fenced, whatever the blind list holds.
OPEN_SURFACES = ("/api/auth", "/api/health", "/api/facilities", "/api/ops")

FENCE_MESSAGE = "This screen is not available yet when more than one facility is on record."
FENCE_ERROR = "facility_fence"

#: One audit row per (address, surface) per this many seconds; the refusals
#: in between are counted into the next row.
AUDIT_EVERY_SECONDS = 60.0
_AUDIT_KEYS_MAX = 512
_audit_last: dict[tuple, list] = {}

#: Patched by tests.
_clock = time.monotonic


def _under(path: str, prefix: str) -> bool:
    return path == prefix or path.startswith(prefix + "/")


def _odd(path: str) -> bool:
    """A path a router would never match as written (dot segments, doubled
    slashes). Judged as fenced, so a crafted spelling of an open prefix can
    never step past the fence."""
    if path == "/":
        return False
    return (
        "//" in path
        or "/./" in path
        or "/../" in path
        or path.endswith(("/.", "/.."))
        or posixpath.normpath(path) != path.rstrip("/")
    )


def surface_of(path: str) -> str:
    """The route group a path belongs to: its first two segments, e.g.
    /api/vigil or /uploads. Used as the audit target and the throttle key."""
    parts = [p for p in (path or "").split("/") if p]
    return "/" + "/".join(parts[:2]) if parts else "/"


def is_fenced(
    path: str,
    roots: tuple = FENCED_ROOTS,
    blind: tuple[str, ...] | str | None = None,
    cleared: tuple[str, ...] | None = None,
) -> bool:
    """Would this path be refused while more than one facility is on record?

    Open prefixes never are; blind prefixes always are. With the fence's own
    lists (blind left None), while the blind list is not empty a path under
    no cleared prefix is refused too, so a route group added after the
    registry was generated fails closed (a crafted spelling counts as under
    none); an empty blind list means the sweep is finished and nothing is
    held shut. A caller that passes its own blind list fences only what it
    names, unless it passes a cleared list too."""
    path = path or "/"
    if not any(_under(path, root) for root in roots):
        return False
    if not _odd(path) and any(_under(path, p) for p in OPEN_SURFACES):
        return False
    if blind is None:
        blind = FACILITY_BLIND_SURFACES
        if cleared is None:
            cleared = FACILITY_CLEARED_SURFACES
    if blind == SENTINEL:
        return True
    if any(_under(path, p) for p in blind):
        return True
    if not blind or cleared is None:
        return False
    return _odd(path) or not any(_under(path, p) for p in cleared)


def describe() -> dict:
    """The fence as the operations status page prints it. every_patient_screen
    is true while no route group carrying patient data has been cleared."""
    blind = FACILITY_BLIND_SURFACES
    if blind == SENTINEL:
        return {
            "every_patient_screen": True,
            "blind_surfaces": ["all"],
            "always_open": list(OPEN_SURFACES),
        }
    cleared = sorted(FACILITY_CLEARED_SURFACES)
    return {
        "every_patient_screen": bool(blind) and set(cleared) <= set(NO_PATIENT_DATA_SURFACES),
        "blind_surfaces": sorted(blind),
        "cleared_surfaces": cleared,
        "unlisted_surfaces_fenced": bool(blind),
        "always_open": list(OPEN_SURFACES),
    }


def _actor(scope) -> tuple[str | None, str | None]:
    """(user id, role) from the request's own session token when it verifies,
    else (None, None). CPU only: no database read on the way to a 503."""
    try:
        from auth import _extract_token, decode_token
        from starlette.requests import HTTPConnection

        token, _ = _extract_token(HTTPConnection(scope))
        if not token:
            return None, None
        claims = decode_token(token)
        return claims.get("sub"), claims.get("role")
    except Exception:
        return None, None


def _audit_due(key: tuple) -> int | None:
    """None when this refusal is only counted; else the refusals counted since
    the last row (0 for the first)."""
    now = _clock()
    entry = _audit_last.get(key)
    if entry is not None and now - entry[0] < AUDIT_EVERY_SECONDS:
        entry[1] += 1
        return None
    suppressed = entry[1] if entry is not None else 0
    if entry is None and len(_audit_last) >= _AUDIT_KEYS_MAX:
        oldest = min(_audit_last, key=lambda k: _audit_last[k][0])
        _audit_last.pop(oldest, None)
    _audit_last[key] = [now, 0]
    return suppressed


def _write_audit(
    *,
    path: str,
    method: str,
    ip: str | None,
    surface: str,
    actor: str | None,
    role: str | None,
    suppressed: int,
) -> None:
    """The authorization row (fail-soft category, so a failed write is logged
    and the 503 still goes out: nothing was disclosed either way)."""
    from services.audit_service import log_audit_event

    log_audit_event(
        correlation_id=f"facility-fence-{uuid.uuid4()}",
        event_category="authorization",
        action_type="facility_fence_blocked",
        outcome="denied",
        summary_message=(
            "Refused while more than one facility is on record: this "
            "screen does not yet keep each facility's patients apart."
        ),
        actor_user_id=actor,
        actor_role=role,
        target_type="route_group",
        target_id=surface,
        request_path=path,
        http_method=method,
        ip_address=ip,
        metadata={
            "surface": surface,
            "refusals_since_last_row": suppressed,
            "blind_surfaces": describe()["blind_surfaces"],
        },
    )


async def _audit_refusal(scope, surface: str) -> None:
    client = scope.get("client") or (None, None)
    ip = client[0] if client else None
    suppressed = _audit_due((ip, surface))
    if suppressed is None:
        return
    actor, role = _actor(scope)
    try:
        from database import run_db

        await run_db(
            _write_audit,
            path=scope.get("path") or "",
            method=scope.get("method") or "WEBSOCKET",
            ip=ip,
            surface=surface,
            actor=actor,
            role=role,
            suppressed=suppressed,
        )
    except Exception as e:  # never let the audit write turn a 503 into a 500
        logger.warning("Could not write the facility fence audit row: %s", e)


_BODY_HEADERS = [
    (b"content-type", b"application/json"),
    (b"cache-control", b"no-store"),
    (b"x-content-type-options", b"nosniff"),
]


async def refused(scope, roots: tuple) -> bool:
    """True when this request must be refused: a fenced path while more than
    one facility is on record. Free (no query) on any path the fence does not
    cover, and a clock read on a cache hit."""
    if scope["type"] not in ("http", "websocket"):
        return False
    if not is_fenced(scope.get("path") or "/", roots):
        return False
    from services import facility_service

    return await facility_service.multi_facility_data_async()


async def refuse(scope, send) -> None:
    """Audit the refusal, then answer 503 (or refuse the socket upgrade)."""
    surface = surface_of(scope.get("path") or "/")
    await _audit_refusal(scope, surface)
    if scope["type"] == "websocket":
        # Closing before accept makes the server answer the upgrade with 403.
        await send({"type": "websocket.close", "code": 1013})
        return
    import json

    body = json.dumps({"detail": FENCE_MESSAGE, "error": FENCE_ERROR, "surface": surface}).encode()
    await send(
        {
            "type": "http.response.start",
            "status": 503,
            "headers": _BODY_HEADERS + [(b"content-length", str(len(body)).encode())],
        }
    )
    await send({"type": "http.response.body", "body": body})


class FacilityFence:
    """Pure ASGI (BaseHTTPMiddleware would buffer Sage's streamed answers), for
    the copy inside the FastAPI app. `roots` limits the fence to some of
    FENCED_ROOTS: main.py passes HTTP_ROOTS."""

    def __init__(self, app, roots: tuple = FENCED_ROOTS):
        self.app = app
        self.roots = tuple(roots)

    async def __call__(self, scope, receive, send):
        if await refused(scope, self.roots):
            await refuse(scope, send)
            return
        await self.app(scope, receive, send)


def fenced_socketio_app(sio, other_app):
    """socketio.ASGIApp(sio, other_app) with the fence in front of /socket.io/.

    A subclass, not a wrapper, so main.socket_app stays HostGate around a
    socketio.ASGIApp (tests/test_host_gate.py pins that shape)."""
    import socketio

    class FencedSocketIOApp(socketio.ASGIApp):
        async def __call__(self, scope, receive, send):
            if await refused(scope, SOCKET_ROOTS):
                await refuse(scope, send)
                return
            await super().__call__(scope, receive, send)

    return FencedSocketIOApp(sio, other_app)
