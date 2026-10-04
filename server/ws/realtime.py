"""Orb — Real-time WebSocket Events

EVENT LOOP DISCIPLINE
---------------------
Socket.IO handlers on an `AsyncServer` run ON the event loop, and — unlike a
FastAPI route — declaring one `def` does NOT get it threadpooled; python-socketio
calls a sync handler inline on the loop. So the only way blocking work leaves the
loop here is an explicit `database.run_db()` hop.

Every DB touch below therefore follows one shape: a PURE SYNC private helper
(`_authenticate`, `_self_patient_id`, `_patient_department`) that owns its
`db_session`, invoked from the async handler exclusively via `await run_db(...)`.
Keeping the helpers sync is deliberate — they are also called directly from
tests, and run_db() rejects coroutine functions outright.
"""
import asyncio
import logging
import socketio

from http.cookies import SimpleCookie
from urllib.parse import urlsplit

from database import db_session, run_db
from config import CORS_ORIGINS, DEPLOYMENT_PROFILE
from auth import decode_token, SESSION_COOKIE
from permissions import department_scope, has_permission, PATIENT_READ_ALL, VITALS_READ
from services.relay_service import get_channels_for_user

logger = logging.getLogger("orb.ws")

# ── Which browser origins may open the socket ──
# engine.io checks the Origin header on every polling request and on the
# WebSocket upgrade. That check is what stops a page on another origin from
# opening a socket with the clinician's session cookie. SameSite=Lax does not:
# a sibling host (intranet.hospital.local next to orb.hospital.local) is the
# same SITE, so the cookie goes with its requests.
#
# Two kinds of origin pass:
#   1. one listed in ORB_CORS_ORIGINS (Vite on :5173, split-origin installs);
#   2. the page's own address, when the backend serves the built SPA itself.
#      That used to need listing too. When it was missing, the client kept
#      retrying, its abandoned long-polls held every connection the browser
#      allows to the host, and ordinary screens hung for 15 to 25 s
#      (INSTALL_RUNBOOK F11, measured 3 October 2026).
#
# engine.io's own same-origin mode (cors_allowed_origins=None) cannot do (2)
# here: its ASGI driver takes the scheme from X-Forwarded-Proto or assumes
# http, so with ORB_TLS_CERT it compares an https page against http://host and
# rejects it. Passing [] is worse: engine.io reads that as "skip the origin
# check", so an empty ORB_CORS_ORIGINS used to let EVERY origin open a socket.
#
# DNS rebinding: a page on attacker.example whose name now resolves to the
# appliance sends a matching Host and Origin, so (2) alone would let its
# engine.io handshake through. services/host_gate.HostGate (it wraps
# main.socket_app) refuses that Host before engine.io sees the request. Behind
# it, the page still could not authenticate: the browser files cookies by host
# name, so the orb_session cookie for the real name never reaches
# attacker.example, and connect() refuses the socket. The port is compared on
# purpose: cookies are not port-scoped, so another service on the same host
# stays a foreign origin.
_configured_origins = list(CORS_ORIGINS)
_DEFAULT_PORTS = {"http": 80, "https": 443}


def _host_port(netloc: str, scheme: str) -> tuple[str, int] | None:
    """(lowercased host, port) of a bare `host[:port]`, the port defaulted from
    `scheme`. None for anything else (userinfo, a path, a bad port, or a
    repeated header, which engine.io joins with a comma)."""
    if not netloc or "," in netloc:
        return None
    try:
        parts = urlsplit("//" + netloc)
        port = parts.port
    except ValueError:
        return None
    if (not parts.hostname or parts.username is not None or parts.password is not None
            or parts.path or parts.query or parts.fragment):
        return None
    return parts.hostname, port if port is not None else _DEFAULT_PORTS[scheme]


def _request_is_https(environ) -> bool:
    """Did this request arrive over TLS, directly (the ASGI scheme) or as the
    proxy in front reports it?"""
    scope = environ.get("asgi.scope") or {}
    if scope.get("scheme") in ("https", "wss"):
        return True
    forwarded = environ.get("HTTP_X_FORWARDED_PROTO", "")
    return forwarded.split(",")[0].strip().lower() == "https"


def _is_own_origin(origin: str, environ) -> bool:
    """True when `origin` is the address this request was sent to, i.e. a page
    this server served itself.

    Host and port must equal the Host header's. The scheme may be the
    request's own or https: behind a TLS proxy that sends no X-Forwarded-Proto
    an https page arrives as plain http. The reverse, an http page on a
    connection known to be https, is a downgrade and is refused.
    """
    try:
        parts = urlsplit(origin)
    except ValueError:
        return False
    if parts.scheme not in _DEFAULT_PORTS or parts.path or parts.query or parts.fragment:
        return False
    page = _host_port(parts.netloc, parts.scheme)
    if page is None or _host_port(environ.get("HTTP_HOST", ""), parts.scheme) != page:
        return False
    return parts.scheme == "https" or not _request_is_https(environ)


def _origin_allowed(origin, environ=None) -> bool:
    """engine.io's origin gate (it accepts a callable taking origin, environ).
    Read at call time, so tests can swap `_configured_origins`.

    "*" in ORB_CORS_ORIGINS keeps the meaning engine.io gave it (any origin),
    the same as the REST middleware: an operator's explicit choice, and the
    Trust Center reports it as a wildcard."""
    if not origin:
        return False
    if "*" in _configured_origins or origin in _configured_origins:
        return True
    if environ is None:
        return False
    try:
        return _is_own_origin(origin, environ)
    except Exception:
        logger.debug("WS origin check failed for %r", origin, exc_info=True)
        return False


# Create Socket.IO server
sio = socketio.AsyncServer(
    async_mode="asgi",
    cors_allowed_origins=_origin_allowed,
    logger=False,
    engineio_logger=False,
)


def _cookie_token(environ) -> str | None:
    """Extract the HttpOnly session-cookie JWT from the handshake environ, for
    cookie-based clients that no longer keep a JS-readable token to put in `auth`."""
    if not environ:
        return None
    raw = environ.get("HTTP_COOKIE")
    if not raw:
        return None
    try:
        jar = SimpleCookie()
        jar.load(raw)
        morsel = jar.get(SESSION_COOKIE)
        return morsel.value if morsel else None
    except Exception:
        return None


def _authenticate(auth, environ=None) -> dict | None:
    """Validate the JWT supplied in the Socket.IO handshake `auth` payload, or —
    for cookie-based clients — the HttpOnly session cookie on the handshake request.

    BLOCKING (reads `users`). Must stay sync and be called via `run_db()` from
    any coroutine — see the module docstring.

    Returns the authoritative user dict (from the DB row) on success, or None
    if the token is missing/invalid/expired or the user no longer exists.

    The role/name/id are ALWAYS taken from the DB row keyed by the token's
    `sub`, never from client-supplied fields.
    """
    token = None
    if isinstance(auth, dict):
        token = auth.get("token") or auth.get("Authorization") or auth.get("authorization")
    # Fall back to the session cookie sent with the handshake (same-origin).
    if not token:
        token = _cookie_token(environ)
    if not token:
        return None
    # Allow "Bearer <token>" form
    if isinstance(token, str) and token.lower().startswith("bearer "):
        token = token[7:].strip()
    try:
        payload = decode_token(token)
    except Exception:
        # decode_token raises HTTPException on expired/invalid — treat as reject
        return None
    user_id = payload.get("sub")
    if not user_id:
        return None
    # Discharge-access grants share the signing key but use sub='discharge_access';
    # they must never be treated as a staff session.
    if user_id == "discharge_access":
        return None
    try:
        with db_session() as db:
            row = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
            # Bridge is for the admission only (ruling R029): the same verdict
            # auth.get_current_user applies to every REST request.
            portal = None
            if row is not None and row["role"] == "patient":
                from services.patient_portal_access import portal_access
                portal = portal_access(db, row["id"])
    except Exception as e:
        logger.warning("WS auth DB lookup failed: %s", e)
        return None
    if not row:
        return None
    if portal is not None and not portal["open"]:
        return None
    user = dict(row)
    # Mirror the REST layer's revocation checks (auth.get_current_user): a socket
    # must not outlive a logout-everywhere / password-reset / deactivation. Without
    # these a revoked or deactivated session kept streaming PHI (vitals, relay,
    # scribe audio) until the 24h JWT expiry.
    if "is_active" in user and user["is_active"] is not None and not user["is_active"]:
        return None
    # Session revocation — mirror auth.get_current_user EXACTLY. It treats an
    # ABSENT tv claim as 0 (``payload.get("tv", 0)``) and an absent/NULL row
    # value as 0, then rejects on any mismatch. The previous
    # ``token_tv is not None and row_tv is not None`` guard SKIPPED the check for
    # a validly-signed token that simply carried no tv claim, so such a token
    # could outlive a logout / password-reset / deactivation on the socket until
    # the 24h JWT expiry — the one revocation the REST layer closes. Real staff
    # tokens always carry tv (auth.create_token), so treating absent-as-0 rejects
    # nothing legitimate; a malformed tv fails closed.
    try:
        token_tv = int(payload.get("tv", 0))
    except (TypeError, ValueError):
        return None
    row_tv = int(user.get("token_version") or 0)
    if token_tv != row_tv:
        return None
    return user


def _self_patient_id(user_id: str) -> str | None:
    """Patient record owned by this login, or None when the account has none.

    Mirrors bridge_routes._resolve_patient_id — a patient socket may only ever be
    scoped to its OWN record.

    BLOCKING. Call via `run_db()` from a coroutine.
    """
    try:
        with db_session() as db:
            row = db.execute(
                "SELECT id FROM patients WHERE user_id = ?", (user_id,)
            ).fetchone()
    except Exception as e:
        logger.warning("WS self-patient lookup failed for %s: %s", user_id, e)
        return None
    return row["id"] if row else None


@sio.event
async def connect(sid, environ, auth=None):
    """Authenticate the Socket.IO handshake before accepting the client.

    Rejecting (returning False) refuses the connection. On success the
    authenticated user is stored in the per-connection session and used as the
    source of truth for identity/role on every subsequent event.
    """
    # Off the loop: the handshake reads the `users` row on an encrypted DB, and a
    # reconnect storm (ward wifi blip -> every tablet redials at once) would
    # otherwise serialise those reads through the event loop.
    user = await run_db(_authenticate, auth, environ)
    if user is None:
        logger.info("WS connection rejected (unauthenticated): %s", sid)
        return False
    await sio.save_session(sid, {
        "user_id": user["id"],
        "name": user["name"],
        "role": user["role"],
        "department": user.get("department"),
    })
    # Personal room for targeted, live, per-user delivery (e.g. surgical alerts to
    # only the assigned surgeon / nurse / anesthetist).
    await sio.enter_room(sid, f"user:{user['id']}")
    # Vitals rooms are gated on the SAME permission the REST board requires
    # (vitals.read), not on mere authentication. Patient-portal and pharmacist
    # logins are ordinary rows in the same users table and pass the handshake, so
    # authentication alone would hand them the live vitals feed the REST layer
    # denies them. An EMPTY department is therefore not a wildcard either: only a
    # vitals.read holder with no department watches the whole census.
    # THE scoping derivation, shared with /api/vigil/patients and the nurse shift
    # board — see permissions.department_scope. This used to be its own copy of
    # the same two lines; the nurse board's private copy is exactly how a
    # client-supplied `?all_departments=true` got to widen a nurse's PHI reach.
    dept = department_scope(user)
    role = user["role"]
    if has_permission(role, VITALS_READ):
        # Mirrors the REST board filter (get_patients): a user with a department
        # sees that department's patients; one without sees everything.
        await sio.enter_room(sid, f"dept:{dept}" if dept else "dept:__all__")
    elif role == "patient":
        # Self-scoped room only — a patient account carries a department (their
        # ward) purely as a location, never as a care-team scope.
        patient_id = await run_db(_self_patient_id, user["id"])
        if patient_id:
            await sio.enter_room(sid, f"patient-self:{patient_id}")
    logger.info("WS connected: %s (user=%s role=%s dept=%s)", sid, user["id"], role, dept or "-")
    return True


# ── Vitals scoping ────────────────────────────────────────────────────────────
# Emit per-patient vitals/status only to the patient's department room (+ the
# __all__ room for no-department users who see the whole census), instead of a
# global broadcast. A small TTL cache avoids a DB hit per emit (a patient's
# department changes only on admission/transfer).
import time as _time

_patient_dept_cache: dict[str, tuple[str | None, float]] = {}
_PATIENT_DEPT_TTL = 60.0


_CACHE_MISS = object()


def _cached_department(patient_id: str):
    """Pure in-memory cache probe — a dict lookup, safe on the event loop.

    Returns the cached department (possibly None) or the private _CACHE_MISS
    sentinel. `None` is a real, cacheable answer ("no active admission"), so it
    cannot double as the miss marker.
    """
    if not patient_id:
        return None
    cached = _patient_dept_cache.get(patient_id)
    if cached and cached[1] > _time.monotonic():
        return cached[0]
    return _CACHE_MISS


def _patient_department(patient_id: str) -> str | None:
    """Active-admission department for a patient (cached), or None if unknown.

    BLOCKING on a cache miss. Coroutines must reach it through
    `_department_for_emit()`, never directly.
    """
    if not patient_id:
        return None
    cached = _cached_department(patient_id)
    if cached is not _CACHE_MISS:
        return cached
    dept = None
    try:
        with db_session() as db:
            row = db.execute(
                "SELECT department FROM admissions WHERE patient_id = ? AND status = 'active' "
                "ORDER BY created_at DESC LIMIT 1",
                (patient_id,),
            ).fetchone()
            if row:
                dept = (row["department"] or "").strip() or None
    except Exception as e:
        logger.warning("vitals scoping: dept lookup failed for %s: %s", patient_id, e)
    _patient_dept_cache[patient_id] = (dept, _time.monotonic() + _PATIENT_DEPT_TTL)
    return dept


async def _department_for_emit(patient_id: str) -> str | None:
    """Loop-safe department lookup for the emit path.

    Vitals emits are the hottest event in the appliance (one per bed per
    interval), so a blanket `run_db` hop would add a thread round-trip to every
    one of them. The TTL cache serves the overwhelming majority from memory with
    no hop at all; only an actual miss — an admission or transfer — pays the
    encrypted `admissions` read, and that read happens off the loop.
    """
    cached = _cached_department(patient_id)
    if cached is not _CACHE_MISS:
        return cached
    return await run_db(_patient_department, patient_id)


# ── The patient's name on a vitals update ─────────────────────────────────────
# A deterioration alert (src/services/criticalAlerts.ts) names the patient from
# `vital.patient_name`, and no lane ever sent one, so the notification read the
# raw patient id. The name is added once, in emit_patient_vitals, for every
# lane. The same TTL cache pattern as the department: a name changes only on a
# registration correction, and a minute of the old spelling is harmless. The
# rooms are not widened: they mirror the REST board's scope, so everyone who
# receives the update can already see this patient's name there.
_patient_name_cache: dict[str, tuple[str | None, float]] = {}


def _cached_name(patient_id: str):
    """Pure in-memory cache probe, as `_cached_department`."""
    if not patient_id:
        return None
    cached = _patient_name_cache.get(patient_id)
    if cached and cached[1] > _time.monotonic():
        return cached[0]
    return _CACHE_MISS


def _patient_name(patient_id: str) -> str | None:
    """The patient's name (cached), or None if unknown. BLOCKING on a cache
    miss: coroutines go through `_name_for_emit()`."""
    if not patient_id:
        return None
    cached = _cached_name(patient_id)
    if cached is not _CACHE_MISS:
        return cached
    name = None
    try:
        with db_session() as db:
            row = db.execute("SELECT name FROM patients WHERE id = ?", (patient_id,)).fetchone()
            if row:
                name = (row["name"] or "").strip() or None
    except Exception as e:
        logger.warning("vitals emit: name lookup failed for %s: %s", patient_id, e)
    _patient_name_cache[patient_id] = (name, _time.monotonic() + _PATIENT_DEPT_TTL)
    return name


async def _name_for_emit(patient_id: str) -> str | None:
    """Loop-safe name lookup for the emit path, as `_department_for_emit`."""
    cached = _cached_name(patient_id)
    if cached is not _CACHE_MISS:
        return cached
    return await run_db(_patient_name, patient_id)


async def emit_patient_vitals(event: str, payload: dict) -> None:
    """Emit a per-patient vitals/status event scoped to its department room
    (+ __all__ for no-department viewers). Replaces the global sio.emit.

    A `vitals:update` also gets the patient's `patient_name` (see above), unless
    the lane already set it; an unknown patient gets none rather than a guess."""
    dept = await _department_for_emit(payload.get("patient_id"))
    if event == "vitals:update" and not payload.get("patient_name"):
        name = await _name_for_emit(payload.get("patient_id"))
        if name:
            payload = {**payload, "patient_name": name}
    # __all__ always: no-department clinicians (whole-census board) must still see it.
    await sio.emit(event, payload, room="dept:__all__")
    if dept:
        await sio.emit(event, payload, room=f"dept:{dept}")


def vitals_update_payload(row, risk: dict, *, source: str) -> dict:
    """The `vitals:update` body for ONE stored vitals row.

    The wire shape bot_service and ingestion_service send (ingestion's keys,
    which are bot_service's plus `source`), for the lanes that build it from a
    row: the nurse observation route and Scribe extraction. Ingestion's two
    coercions apply, so `consciousness` and `on_oxygen` are never null on the
    wire; whether ACVPU was actually assessed travels in
    `risk.missing_parameters`. tests/test_vitals_emit_lanes.py holds all four
    lanes to the same keys.

    `risk` is required, not optional: the open chart ignores a tick without one
    and keeps showing the older set.
    """
    def _get(key):
        try:
            return row[key]
        except (KeyError, IndexError):
            return None

    return {
        "patient_id": _get("patient_id"),
        "timestamp": _get("timestamp"),
        "heart_rate": _get("heart_rate"),
        "systolic_bp": _get("systolic_bp"),
        "diastolic_bp": _get("diastolic_bp"),
        "spo2": _get("spo2"),
        "temperature": _get("temperature"),
        "respiratory_rate": _get("respiratory_rate"),
        "consciousness": _get("consciousness") or "alert",
        "on_oxygen": bool(_get("on_oxygen")),
        "risk": risk,
        "source": source,
    }


#: Strong references to emits scheduled from sync code (the loop holds tasks
#: only weakly, so an unreferenced one can be collected before it runs).
_THREAD_EMITS: set = set()


def schedule_on_loop(make_coro, what: str) -> bool:
    """Run ``make_coro()`` on the app's event loop from SYNC code. Never raises.

    For writers that run in a FastAPI ``def`` route's anyio worker thread (the
    nurse observation route) and have to push a socket event after their
    transaction commits. Two ways back to the loop: already on it (create a
    task), or an anyio worker thread (its portal). With neither, as in a script
    or a unit test calling a service directly, the push is skipped: the row is
    already stored and shows on the next load. Same mechanism as
    bot_service.publish_vigil_critical.

    Fire and forget, so the caller's response does not wait on the socket
    layer. Returns True when the emit was scheduled.
    """
    def _start() -> bool:
        task = asyncio.get_running_loop().create_task(make_coro())
        _THREAD_EMITS.add(task)
        task.add_done_callback(_THREAD_EMITS.discard)
        return True

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        try:
            return _start()
        except Exception as e:
            logger.warning("%s not pushed live: %s", what, e)
            return False

    try:
        from anyio.from_thread import run_sync as _portal_run_sync
        return bool(_portal_run_sync(_start))
    except RuntimeError as e:
        # "not called from an AnyIO worker thread": no portal back to the loop.
        logger.info("%s not pushed live: no event loop reachable from this thread (%s)",
                    what, e)
        return False
    except Exception as e:
        logger.warning("%s not pushed live: %s", what, e)
        return False


def schedule_patient_vitals(event: str, payload: dict) -> bool:
    """`emit_patient_vitals` from sync code. See schedule_on_loop."""
    return schedule_on_loop(lambda: emit_patient_vitals(event, payload),
                            f"{event} for {payload.get('patient_id')}")


# ── Presence: who is currently viewing each patient ──────────────────────────
# In-process roster keyed by patient_id -> { sid: {user_id, name, role} }. A
# single process backs the demo/pilot; a multi-process deployment would move this
# to Redis (socket.io's own adapter) — the event contract below stays the same.
_presence: dict[str, dict[str, dict]] = {}


def _roster(patient_id: str) -> list[dict]:
    """Distinct viewers for a patient (deduped by user_id, so one clinician with
    two tabs shows once)."""
    seen: dict[str, dict] = {}
    for viewer in _presence.get(patient_id, {}).values():
        seen[viewer["user_id"]] = {
            "user_id": viewer["user_id"],
            "name": viewer["name"],
            "role": viewer["role"],
        }
    return list(seen.values())


async def _broadcast_presence(patient_id: str) -> None:
    await sio.emit(
        "presence:update",
        {"patient_id": patient_id, "viewers": _roster(patient_id)},
        room=f"patient-presence:{patient_id}",
    )


@sio.event
async def enter_patient(sid, data):
    """Client opened a patient view — join its presence room and announce."""
    session = await sio.get_session(sid)
    if not session:
        return
    patient_id = (data or {}).get("patient_id")
    if not patient_id:
        return
    # The roster names the clinicians currently reading a chart, so joining is
    # gated on the same permission that opens the chart over REST. Without this a
    # patient/pharmacist socket could subscribe to any patient's viewer list.
    if not has_permission(session["role"], PATIENT_READ_ALL):
        logger.info(
            "WS presence denied: user=%s patient=%s", session["user_id"], patient_id
        )
        return
    await sio.enter_room(sid, f"patient-presence:{patient_id}")
    _presence.setdefault(patient_id, {})[sid] = {
        "user_id": session["user_id"],
        "name": session["name"],
        "role": session["role"],
    }
    await _broadcast_presence(patient_id)


@sio.event
async def leave_patient(sid, data):
    """Client closed a patient view — leave the presence room and announce."""
    patient_id = (data or {}).get("patient_id")
    if not patient_id:
        return
    await sio.leave_room(sid, f"patient-presence:{patient_id}")
    room = _presence.get(patient_id)
    if room and sid in room:
        del room[sid]
        if not room:
            del _presence[patient_id]
    await _broadcast_presence(patient_id)


@sio.event
async def disconnect(sid):
    logger.info("WS disconnected: %s", sid)
    # Drop this sid from any patient it was viewing and refresh those rosters so a
    # closed tab / dropped connection doesn't leave a ghost presence chip behind.
    affected = [pid for pid, room in _presence.items() if sid in room]
    for pid in affected:
        del _presence[pid][sid]
        if not _presence[pid]:
            del _presence[pid]
        await _broadcast_presence(pid)


async def _user_can_access_channel(user_id: str, user_role: str, channel_id: str) -> bool:
    """Return True if the authenticated user is authorized for this channel.

    Reuses the same visibility logic as the REST layer (get_channels_for_user),
    so WS room membership matches what the user can see over HTTP.
    """
    try:
        # get_channels_for_user is a multi-table join over the encrypted DB and
        # runs on EVERY join_channel / typing / new_message event — typing
        # indicators alone would hammer the loop with it.
        channels = await run_db(get_channels_for_user, user_id, user_role)
    except Exception as e:
        logger.warning("WS channel access check failed: %s", e)
        return False
    return any(c.get("id") == channel_id for c in channels)


@sio.event
async def join_channel(sid, data):
    """User joins a Relay channel room (only if authorized for it)."""
    session = await sio.get_session(sid)
    if not session:
        return
    channel_id = (data or {}).get("channel_id")
    if not channel_id:
        return
    if not await _user_can_access_channel(session["user_id"], session["role"], channel_id):
        logger.info(
            "WS join denied: user=%s channel=%s", session["user_id"], channel_id
        )
        await sio.emit("error", {"error": "not authorized for channel"}, to=sid)
        return
    await sio.enter_room(sid, channel_id)
    logger.debug("%s joined channel %s", session["user_id"], channel_id)


@sio.event
async def leave_channel(sid, data):
    """User leaves a Relay channel room."""
    channel_id = (data or {}).get("channel_id")
    if channel_id:
        await sio.leave_room(sid, channel_id)


@sio.event
async def typing(sid, data):
    """Broadcast typing indicator (only from authenticated, authorized members)."""
    session = await sio.get_session(sid)
    if not session:
        return
    channel_id = (data or {}).get("channel_id")
    if not channel_id:
        return
    if not await _user_can_access_channel(session["user_id"], session["role"], channel_id):
        return
    # Re-derive sender identity from the session; do not trust client fields.
    payload = {
        "channel_id": channel_id,
        "sender_id": session["user_id"],
        "sender_name": session["name"],
    }
    await sio.emit("typing", payload, room=channel_id, skip_sid=sid)


@sio.event
async def new_message(sid, data):
    """Handle new message — authenticate, authorize, save and broadcast.

    Identity (sender_id/name/role) is derived from the authenticated session,
    NEVER from client-supplied data. relay.send permission and channel
    membership are enforced server-side, mirroring the REST RBAC.
    """
    session = await sio.get_session(sid)
    if not session:
        await sio.emit("error", {"error": "unauthenticated"}, to=sid)
        return

    data = data or {}
    channel_id = data.get("channel_id")
    if not channel_id:
        return

    if not has_permission(session["role"], "relay.send"):
        logger.info("WS message denied (no relay.send): user=%s", session["user_id"])
        await sio.emit("error", {"error": "insufficient permissions"}, to=sid)
        return

    if not await _user_can_access_channel(session["user_id"], session["role"], channel_id):
        logger.info(
            "WS message denied (channel): user=%s channel=%s",
            session["user_id"], channel_id,
        )
        await sio.emit("error", {"error": "not authorized for channel"}, to=sid)
        return

    from models.message import MAX_MESSAGE_CHARS
    from services.relay_service import send_message

    # The same content rule as the REST lane (models/message.MessageCreate):
    # stripped, 1 to MAX_MESSAGE_CHARS. This lane defaulted a missing body to
    # '' and stored it, and had no ceiling at all.
    content = data.get("content")
    content = content.strip() if isinstance(content, str) else ""
    if not content or len(content) > MAX_MESSAGE_CHARS:
        await sio.emit(
            "error",
            {"error": f"A message must be 1 to {MAX_MESSAGE_CHARS} characters."},
            to=sid,
        )
        return

    # A DB INSERT + read-back on the encrypted store. Awaited (not fired and
    # forgotten) so the broadcast still happens only after the message is
    # durable — a Relay message every clinician saw but the DB never kept
    # would be worse than a slow one.
    msg = await run_db(
        send_message,
        channel_id=channel_id,
        sender_id=session["user_id"],
        sender_name=session["name"],
        sender_role=session["role"],
        content=content,
        # Never from the client: alert, system and ambient_action rows are
        # written by the server, and a forged one reads as the system speaking.
        message_type="text",
    )

    await sio.emit("message:new", msg, room=channel_id)


# Register streaming transcription events on this sio instance
import ws.scribe_stream  # noqa: F401, E402
