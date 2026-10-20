"""Orb: the 404 gates for the hospital-system build's opt-in switches.

Every route in routes/adt, routes/lab, routes/pharmacy_store, opd_routes,
registration_routes and schedule_routes sits behind one of these. They read
config at REQUEST time (the dietary_routes pattern), so a test, or a site that
flips a switch and restarts, gets the answer the switch board publishes on
/api/health.

404, not 403, and the same shape as the existing module gates
({"error": "module_disabled", ...}): the screen is not forbidden to this user,
it is not present at this site.

SESSION FIRST. Each gate depends on auth.get_current_user, so a caller with no
session gets 401 before the switch is consulted, on or off
(tests/test_unauthenticated_surface.py requires every non-public route to
refuse an anonymous caller). FastAPI caches the dependency per request, so a
route that also uses require_permission still authenticates once. A route that
authenticates some other way (an analyser's instrument key, a display board's
device key) uses the *_without_session form and must be listed in that test's
PUBLIC_ROUTES with its reason.

Use as a router-level dependency, so a route added later inherits it:

    router = APIRouter(prefix="/api/adt",
                       dependencies=[Depends(require_adt_module)])
"""

from __future__ import annotations

import config
from auth import get_current_user
from fastapi import Depends, HTTPException

# Plain words for the 404 body. No variable names on screen: a client that was
# built before a switch flipped deserves a sentence.
_MODULE_WORDS = {
    "adt": "Beds and admissions is switched off on this site.",
    "pharmacy-store": "The pharmacy store is switched off on this site.",
    "lab": "The laboratory module is switched off on this site.",
}

_FEATURE_WORDS = {
    "registration": "The registration desk is switched off on this site.",
    "opd": "Outpatient consultations are switched off on this site.",
}


def _module_off(module_id: str) -> HTTPException:
    return HTTPException(
        status_code=404,
        detail={
            "error": "module_disabled",
            "module": module_id,
            "message": _MODULE_WORDS.get(module_id, "This module is switched off on this site."),
        },
    )


def _feature_off(flag_id: str) -> HTTPException:
    return HTTPException(
        status_code=404,
        detail={
            "error": "feature_disabled",
            "feature": flag_id,
            "message": _FEATURE_WORDS.get(flag_id, "This feature is switched off on this site."),
        },
    )


def module_gate(module_id: str, *, allow_draining: bool = False, require_session: bool = True):
    """A dependency that 404s unless the opt-in module is 'on' (or, with
    allow_draining, 'on' or 'draining': open work stays reachable while a
    switched-off module drains, ruling LIS-R21)."""
    if module_id not in config.OPT_IN_MODULES:
        raise ValueError(f"{module_id!r} is not an opt-in module")
    allowed = ("on", "draining") if allow_draining else ("on",)

    def _check() -> None:
        if config.module_state(module_id) not in allowed:
            raise _module_off(module_id)

    # async: the check reads config and a cached, DB-free drain check only,
    # so it never needs a worker thread.
    if require_session:
        _GATE_USER_DEFAULT = Depends(get_current_user)

        async def _gate_first(user: dict = _GATE_USER_DEFAULT) -> None:
            _check()

        suffix = ""
    else:

        async def _gate_second() -> None:
            _check()

        suffix = "_without_session"
    _gate = _gate_first if require_session else _gate_second
    _gate.__name__ = f"require_{module_id.replace('-', '_')}_module{suffix}"
    return _gate


def feature_gate(flag_id: str, *, require_session: bool = True):
    """A dependency that 404s unless the feature flag (prerequisites
    included) is on."""
    if flag_id not in config.FEATURE_FLAGS:
        raise ValueError(f"{flag_id!r} is not a feature flag")

    def _check() -> None:
        if not config.feature_enabled(flag_id):
            raise _feature_off(flag_id)

    # async: the check reads config and a cached, DB-free drain check only,
    # so it never needs a worker thread.
    if require_session:
        _GATE_USER_DEFAULT = Depends(get_current_user)

        async def _gate_first(user: dict = _GATE_USER_DEFAULT) -> None:
            _check()

        suffix = ""
    else:

        async def _gate_second() -> None:
            _check()

        suffix = "_without_session"
    _gate = _gate_first if require_session else _gate_second
    _gate.__name__ = f"require_{flag_id}_feature{suffix}"
    return _gate


# The named gates every later increment imports.
require_adt_module = module_gate("adt")
require_pharmacy_store_module = module_gate("pharmacy-store")


def require_lab_module(allow_draining: bool = False, *, require_session: bool = True):
    """The laboratory gate. Every routes/lab sub-router carries
    require_lab_module(allow_draining=True), so every lab route 404s when the
    module is 'off'; a route that STARTS new work (order intake, collection,
    analyser intake) also carries require_lab_module() so it 404s while the
    module is draining. Finishing, cancelling and releasing open work must stay
    reachable while draining (LIS-R21)."""
    return module_gate("lab", allow_draining=allow_draining, require_session=require_session)


_REQUIRE_LAB_CRITICAL_SURFACE_USER_DEFAULT = Depends(get_current_user)


async def require_lab_critical_surface(
    user: dict = _REQUIRE_LAB_CRITICAL_SURFACE_USER_DEFAULT,
) -> None:
    """The gate for the laboratory critical-result routes (routes/lab/
    criticals.py): open while the laboratory is on or draining, AND while
    interfaced criticals are on without the laboratory (that switch works
    without ORB_LAB, ruling LIS-R22). A critical result that was raised must
    never become unreachable because a switch changed."""
    if config.module_state("lab") in ("on", "draining"):
        return
    if config.feature_enabled("lab_critical_interfaced"):
        return
    raise _module_off("lab")


require_registration_feature = feature_gate("registration")
require_opd_feature = feature_gate("opd")
