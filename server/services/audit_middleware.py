"""Orb — PHI access/mutation audit middleware.

HIPAA §164.312(b) requires audit controls over PHI access. Rather than hand-wire
an audit call into every route (the prior state: only ~4 of 18 route modules
logged anything), this middleware records EVERY state-changing API request
(POST/PUT/PATCH/DELETE) generically: who (actor from the JWT), what (method +
path), and the outcome (HTTP status). Route-level audit events (with richer
domain metadata) still fire where they exist; this is the safety net that
guarantees no mutation goes unlogged.

Read-only GETs are intentionally not logged here (too high-volume, and reads are
better audited at the sensitive-endpoint level); the emphasis is a complete,
tamper-evident trail of every change to clinical data.

EVENT LOOP: this is the highest-traffic DB write in the codebase — every
POST/PUT/PATCH/DELETE pays one, and since encryption-at-rest every page it
touches costs HMAC-SHA512 + AES. It therefore MUST NOT run on the loop (three
production freezes came from exactly this shape — docs/WORKLOG.md §3). The write
goes through `database.run_db`, and the middleware still AWAITS it: the response
is not returned until the audit row is durable, exactly as before. Off-loop is
not the same as fire-and-forget.
"""
from __future__ import annotations

import asyncio
import logging
import uuid

logger = logging.getLogger("orb.audit_mw")

_AUDIT_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

# Paths that already emit a richer, purpose-built audit event OR are noise.
# Login is audited in auth_routes; skipping avoids double-logging the actor's
# credential (and login has no bearer token yet anyway).
_SKIP_PATHS = {"/api/auth/login"}

# The summary is what an administrator reads in the audit log, and it used to be
# "POST /api/emar/P001/administer -> 200". Method, path and status keep their
# own columns and metadata, so filtering and tracing lose nothing; the summary
# names the area in words. Only segments that do not read well humanised are
# listed; anything else falls back to the segment itself.
_AREA_NAMES = {
    "abdm": "ABDM", "admin": "administration", "audit": "the audit log",
    "auth": "sign-in", "bridge": "the patient portal", "bundles": "care bundles",
    "capacity-sim": "capacity planning", "chart": "the chart",
    "command-center": "the command centre", "consent": "the consent register",
    "discharge-access": "discharge access", "downtime": "downtime records",
    "emar": "medication administration", "fhir": "FHIR export",
    "flight-recorder": "the flight recorder", "frontdesk": "the front desk",
    "helix": "pharmacy", "ingest": "data ingestion", "ipass": "I-PASS handoffs",
    "lens": "Lens imaging", "nabh": "NABH", "nurse": "nursing", "ops": "operations",
    "patients": "patient records", "payer": "insurance claims", "pulse": "Pulse",
    "relay": "Relay", "sage": "Sage", "scribe": "Scribe", "vigil": "Vigil",
}
_METHOD_WORDS = {"POST": "Submitted to", "PUT": "Updated in",
                 "PATCH": "Updated in", "DELETE": "Deleted from"}


def _readable_summary(method: str, path: str, status: int) -> str:
    """'Submitted to medication administration: completed', not 'POST … -> 200'."""
    parts = [p for p in path.split("/") if p]
    seg = parts[1] if len(parts) > 1 and parts[0] == "api" else ""
    area = _AREA_NAMES.get(seg) or seg.replace("-", " ").replace("_", " ") or "the system"
    if status < 400:
        result = "completed"
    elif status in (401, 403):
        result = "refused, not permitted"
    elif status == 404:
        result = "not found"
    elif status < 500:
        result = "rejected"
    else:
        result = "failed with a server error"
    return f"{_METHOD_WORDS.get(method, method)} {area}: {result}"


def _actor_from_request(request) -> tuple[str | None, str | None]:
    """Best-effort decode of the session JWT to (user_id, role). Never raises.

    EXTRACTION IS DELEGATED TO auth._extract_token, DELIBERATELY — the same
    function `get_current_user` authenticates with. This middleware used to
    re-implement the lookup as "read the Authorization header", and that copy
    went stale the moment Deferred A moved the SPA onto the HttpOnly
    `orb_session` cookie (auth.py:26). The browser stopped sending an
    Authorization header, this function stopped finding a token, and EVERY
    mutation made from the app — every order, every appointment, every receipt —
    landed on the tamper-evident chain with actor_user_id NULL. The rows kept
    being written, the chain kept verifying, and the trail said nobody did any
    of it.

    That is not a cosmetic gap. auth.py:31-44 rests the whole PIN-on-device
    design on the claim that the ACTOR is the person, recorded per request; a
    chain of unattributed mutations cannot support that claim to a hospital's
    compliance officer, and the loss is unrecoverable after the fact because
    audit_logs is append-only.

    So there is now exactly ONE token-extraction rule in the process. A future
    credential channel (another cookie name, a header rename) reaches the audit
    trail for free, and cannot silently un-attribute it again.
    """
    try:
        from auth import _extract_token, decode_token
        token, _source = _extract_token(request)
        if not token:
            return None, None
        # Raises HTTPException on an expired/invalid token; an unauthenticated
        # attempt is still audited, just without an actor it cannot prove.
        payload = decode_token(token)
        return payload.get("sub") or payload.get("id"), payload.get("role")
    except Exception:
        return None, None


async def audit_mutations(request, call_next):
    """Starlette HTTP middleware: audit-log every state-changing API call."""
    path = request.url.path
    method = request.method

    should_audit = (
        method in _AUDIT_METHODS
        and path.startswith("/api/")
        and path not in _SKIP_PATHS
    )

    if not should_audit:
        return await call_next(request)

    correlation_id = request.headers.get("x-correlation-id") or str(uuid.uuid4())
    actor_id, actor_role = _actor_from_request(request)

    # Log in a finally so a mutation that raises an UNHANDLED exception (→ 500)
    # is still audited, not just the ones that return a response object. The
    # status defaults to 500 and is overwritten once a response is produced.
    status = 500
    try:
        response = await call_next(request)
        status = response.status_code
        return response
    finally:
        from database import run_db
        from services.audit_service import log_audit_event
        outcome = "success" if status < 400 else ("denied" if status in (401, 403) else "failure")
        # shield(): the write is submitted to the DB executor and allowed to
        # finish even if THIS request is cancelled (client disconnect, timeout).
        # Without it a disconnect mid-flight would cancel the queued write and
        # silently drop the audit row for a mutation the server already applied —
        # the sync version could not lose it, so neither may this one.
        write = asyncio.shield(
            run_db(
                log_audit_event,
                correlation_id=correlation_id,
                event_category="api_mutation",
                action_type=f"{method.lower()}_request",
                outcome=outcome,
                summary_message=_readable_summary(method, path, status),
                actor_user_id=actor_id,
                actor_role=actor_role,
                request_path=path,
                http_method=method,
                ip_address=request.client.host if request.client else None,
                user_agent=request.headers.get("user-agent"),
                metadata={"status_code": status, "method": method, "path": path},
            )
        )
        try:
            await write
        except asyncio.CancelledError:
            # Our await was cancelled, not the write — the shielded task keeps
            # running to completion. Propagate cancellation as asyncio requires.
            raise
        except Exception as e:  # audit must never break the request
            # api_mutation is a fail-SOFT category (see audit_service); a failed
            # safety-net row must not turn a successful clinical write into a 500.
            logger.debug("audit middleware could not log %s %s: %s", method, path, e)
