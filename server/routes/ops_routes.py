"""Orb Hospital OS — the operations status page.

The founder chose NO remote support: nobody at the vendor can dial in and look.
Everything wrong with this appliance therefore has to be legible ON SITE, to a
hospital IT person, in words they understand, without reading Python. That is
what these two endpoints are for.

  GET /api/ops/status       the same answer as JSON, for the admin screen
  GET /api/ops/status.txt   the same answer as plain text, for a terminal or a
                            screenshot when the browser itself is the problem

Both are gated on ``admin.overview`` — the same admin-only permission that
guards user management and backups. A clinician must not be able to read the
appliance's disk headroom, model state or integration status; none of it helps
them treat a patient and all of it helps somebody casing the machine.

WHY THESE ARE PLAIN ``def`` AND NOT ``async def``. services.ops_health_service
.build_status walks the filesystem, opens the database, stats every backup and
(in deep mode) probes the model runtime. Every one of those blocks. A sync
handler is run by FastAPI in the threadpool, which is the codebase's standing
rule — see tests/test_event_loop_discipline.py, which enforces it across every
route module. An ``async def`` here would put a whole-disk scan on the event loop
and freeze the ward's screens for exactly as long as it took.

WHY ONLY THE DEEP READ IS AUDITED. The shallow status is what an admin screen
polls; auditing every poll would bury the audit chain in noise and make the rows
that matter harder to find. The deep read is the one that opens the database and
probes the model, so it is the one that is worth a row.

NO PHI, BY CONSTRUCTION. Nothing here selects patient data — the payload comes
whole from ops_health_service, which is held to that property by three separate
tests in tests/test_ops_health.py (JSON, text, and the offline collector). This
router adds no patient identifier of its own, and must never start to: the
status page is the surface most likely to be screenshotted into a WhatsApp
group. That is also why there is no per-patient or per-ward variant of it and no
scoping parameter — there is nothing here to scope.
"""
from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, Request
from fastapi.responses import PlainTextResponse

from permissions import require_permission
from services import ops_health_service
from services.audit_service import log_audit_event

logger = logging.getLogger("orb.ops_routes")

router = APIRouter(prefix="/api/ops", tags=["ops"])


def _audit_deep_read(request: Request, user: dict, surface: str) -> None:
    """Record that somebody ran the deep appliance check.

    Never raises: a status page that 500s because the audit write failed is a
    status page that is useless in precisely the situation it exists for.
    """
    try:
        log_audit_event(
            correlation_id=str(uuid.uuid4()),
            event_category="system_administration",
            action_type="ops_status_read",
            outcome="success",
            summary_message=f"Deep operations status read ({surface})",
            actor_user_id=user.get("id"),
            actor_role=user.get("role"),
            target_type="appliance",
            target_id="ops_status",
            request_path=request.url.path,
            http_method=request.method,
            ip_address=request.client.host if request.client else None,
        )
    except Exception:
        logger.warning("could not audit deep ops status read", exc_info=True)


@router.get("/status")
def ops_status(request: Request, deep: bool = False,
               user: dict = Depends(require_permission("admin.overview"))):
    """The appliance's own health, in a shape a screen can render.

    ``deep=true`` adds the checks that cost something to run (an integrity read
    of the database, a live probe of the model runtime) and is audited.
    """
    payload = ops_health_service.build_status(deep=deep)
    if deep:
        _audit_deep_read(request, user, "json")
    return payload


@router.get("/status.txt", response_class=PlainTextResponse)
def ops_status_text(request: Request, deep: bool = False,
                    user: dict = Depends(require_permission("admin.overview"))):
    """The same answer as plain text.

    This exists because the moment somebody needs it most is the moment the
    browser, the front end build or the network is the thing that is broken.
    Text survives all three and can be pasted into a message unchanged.
    """
    payload = ops_health_service.build_status(deep=deep)
    if deep:
        _audit_deep_read(request, user, "text")
    return PlainTextResponse(ops_health_service.render_text(payload),
                             media_type="text/plain; charset=utf-8")
