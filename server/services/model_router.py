"""Residency-aware local model selection.

Orb pins both inference tiers in memory (ORB_OLLAMA_KEEP_ALIVE=-1), but an
operator can deliberately evict the 27B from the header's AI panel to reclaim
~17 GB. Before this router, the very next Sage/Lens/Scribe request simply asked
Ollama for the 27B anyway, and Ollama transparently re-loaded it — measured at
~80 s to first token, and it silently undid the operator's memory reclaim by
re-pinning the model.

This module makes that case explicit instead: when the preferred tier is not
resident, route the turn to the smaller tier that is (or that loads in seconds),
and tell the caller it happened so the answer can be labelled honestly. The
4B is multimodal and shares the MedGemma family, so it is a usable stand-in for
chat and vision — lower quality, not a different product.

Nothing here loads or unloads a model; it only observes what Ollama currently
holds (`GET /api/ps`) and picks a name.
"""
from __future__ import annotations

import logging
import time
from typing import NamedTuple

import httpx

from config import AMBIENT_LLM_MODEL, LLM_MODEL, MODEL_FALLBACK_ENABLED, OLLAMA_BASE_URL

logger = logging.getLogger("orb.model_router")

# /api/ps is a cheap local call, but a single Sage turn can consult the router
# several times (retrieval, generation, narrator). A short TTL keeps it to one
# round-trip per burst while still reacting to an unload within a second.
_PS_TTL_SECONDS = 1.0
_ps_cache: tuple[float, frozenset[str]] | None = None


def _is_expiring(expires_at) -> bool:
    """True when Ollama's reported expiry has already passed (model is going away).

    A pinned model (keep_alive=-1) reports a far-future date, so this is False
    for anything Orb deliberately keeps warm. An unparsable/absent value is
    treated as NOT expiring — residency is the safer assumption, since the cost
    of a wrong 'expiring' verdict is an unnecessary downgrade.
    """
    if not expires_at:
        return False
    try:
        from datetime import datetime, timezone
        dt = datetime.fromisoformat(str(expires_at))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt <= datetime.now(timezone.utc)
    except Exception:
        return False


class ModelChoice(NamedTuple):
    model: str
    """The Ollama model name to send this request to."""
    downgraded: bool
    """True when `model` is the fallback tier because the preferred one is not resident."""
    preferred: str
    """The tier that would have been used if it were resident."""


async def probe_residency(timeout: float = 2.0) -> frozenset[str] | None:
    """Names Ollama currently holds in memory, or None when Ollama is unreachable.

    None (unknown) and empty-set (reachable, nothing loaded) are DIFFERENT
    answers: ambient services use the distinction to skip optional work when a
    tier was deliberately evicted, without misreading an engine blip as
    "everything is evicted".
    """
    global _ps_cache
    now = time.monotonic()
    if _ps_cache and now - _ps_cache[0] < _PS_TTL_SECONDS:
        return _ps_cache[1]
    try:
        # trust_env=False — THIS LINE IS WHY THE API WEDGED (twice on 2026-08-01,
        # once for 3.5 hours; /api/health stopped answering entirely).
        #
        # httpx.AsyncClient.__init__ is SYNCHRONOUS. `async with httpx.AsyncClient(...)`
        # still runs the whole constructor on the event loop thread, and with
        # trust_env left at its default True that constructor resolves the proxy
        # configuration: _get_proxy_map -> get_environment_proxies -> urllib's
        # getproxies() -> getproxies_macosx_sysconf() -> _scproxy, an IPC
        # round-trip to macOS `configd`. That call takes NO timeout argument and
        # has no async form. Three faulthandler dumps 8 s apart (kill -USR1)
        # caught the main thread in exactly that frame each time.
        #
        # A proxy is meaningless here regardless: OLLAMA_BASE_URL is loopback (or
        # a container hostname), Orb is a local-first appliance, and no hospital
        # proxy has ever been in this path. So skip the lookup entirely rather
        # than hoping configd stays responsive.
        #
        # This site is the hottest one in the process: probe_residency() is
        # consulted by the ward narrator on EVERY event-bus dispatch, plus by
        # every Sage/Vigil/Lens/Scribe turn through select_chat_model().
        #
        # Every other loopback client in server/ carries the same kwarg, and
        # tests/test_httpx_client_config.py fails the build if a new one doesn't.
        # The genuinely-external lane (services/pulse_service.py) deliberately
        # does NOT use trust_env=False — see the note there.
        async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
            resp = await client.get(f"{OLLAMA_BASE_URL}/api/ps")
            resp.raise_for_status()
            # A model being torn down still appears in /api/ps (Ollama's unload
            # is async, the runner shows "Stopping..."), but routing a turn to
            # it triggers a full cold reload. Treat an already-expired
            # expires_at as NOT resident so that window can't cost 80 seconds.
            names = frozenset(
                m.get("name", "")
                for m in resp.json().get("models", [])
                if m.get("name") and not _is_expiring(m.get("expires_at"))
            )
    except Exception as e:
        logger.debug("residency probe failed (%s) — treating as unknown", e)
        return None
    _ps_cache = (now, names)
    return names


async def resident_models(timeout: float = 2.0) -> frozenset[str]:
    """Names Ollama currently holds in memory. Empty set if Ollama is unreachable.

    An unreachable engine deliberately yields an EMPTY set rather than raising:
    the caller then keeps its preferred model, so a transient /api/ps blip can
    never silently downgrade a clinician's answer.
    """
    names = await probe_residency(timeout)
    return names if names is not None else frozenset()


async def ambient_tier_evicted(model: str) -> bool:
    """True when `model` is known to NOT be resident while Ollama is reachable.

    The one case that returns True is an operator eviction (or a not-yet-warmed
    tier): the engine answered /api/ps and the tier is absent. Ambient
    background features consult this before calling Ollama, because their call
    would otherwise transparently RELOAD the tier seconds after an operator
    freed the memory — the header's unload button looked like it didn't work.
    Unknown residency (engine unreachable) returns False: optional work should
    not be dropped on a guess."""
    resident = await probe_residency()
    return resident is not None and model not in resident


def invalidate_residency_cache() -> None:
    """Drop the cached /api/ps snapshot.

    Called right after an operator loads/unloads a model so the next request
    routes on the new reality instead of waiting out the TTL.
    """
    global _ps_cache
    _ps_cache = None


async def select_chat_model(
    preferred: str | None = None,
    fallback: str | None = None,
) -> ModelChoice:
    """Pick the local model for this turn.

    Preferred tier resident  -> use it.
    Preferred tier evicted   -> use the fallback tier (downgraded=True).
    Ollama unreachable, fallback disabled, or the two tiers are the same
                             -> use the preferred tier unchanged.
    """
    preferred = preferred or LLM_MODEL
    fallback = fallback or AMBIENT_LLM_MODEL

    if not MODEL_FALLBACK_ENABLED or fallback == preferred:
        return ModelChoice(preferred, False, preferred)

    resident = await resident_models()
    if not resident:
        # Unknown residency (engine unreachable, or nothing loaded yet at boot).
        # Do not downgrade on a guess.
        return ModelChoice(preferred, False, preferred)
    if preferred in resident:
        return ModelChoice(preferred, False, preferred)

    logger.info(
        "'%s' is not resident — routing this request to '%s' (fallback tier)",
        preferred, fallback,
    )
    return ModelChoice(fallback, True, preferred)
