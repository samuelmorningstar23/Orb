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

import asyncio
from dataclasses import dataclass
import logging
import math
import threading
import time
from datetime import UTC
from typing import NamedTuple
from weakref import WeakKeyDictionary

import httpx

from config import AMBIENT_LLM_MODEL, LLM_MODEL, MODEL_FALLBACK_ENABLED, OLLAMA_BASE_URL
from services.local_model_http import local_model_ssl_context

logger = logging.getLogger("orb.model_router")

# /api/ps is a cheap local call, but a single Sage turn can consult the router
# several times (retrieval, generation, narrator). A short TTL keeps it to one
# round-trip per burst while still reacting to an unload within a second.
_PS_TTL_SECONDS = 1.0
_PS_PROBE_TIMEOUT_SECONDS = 2.0


@dataclass
class _ResidencyState:
    origin: str
    epoch: int
    cache: tuple[float, frozenset[str]] | None = None
    pending: asyncio.Task[frozenset[str] | None] | None = None


# A task/lock from one asyncio.run() or request-worker loop must never be awaited
# by another loop. Completed states hold no loop/task references, allowing old
# loops to disappear from the weak index. The registry lock is held only for
# tiny synchronous state changes, never network I/O or an await.
_ps_states: WeakKeyDictionary[asyncio.AbstractEventLoop, _ResidencyState] = WeakKeyDictionary()
_ps_state_lock = threading.Lock()
_ps_epoch = 0


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
        from datetime import datetime

        dt = datetime.fromisoformat(str(expires_at))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt <= datetime.now(UTC)
    except Exception:
        return False


class ModelChoice(NamedTuple):
    model: str
    """The Ollama model name to send this request to."""
    downgraded: bool
    """True when `model` is the fallback tier because the preferred one is not resident."""
    preferred: str
    """The tier that would have been used if it were resident."""


async def _fetch_residency(state: _ResidencyState) -> frozenset[str] | None:
    """One bounded engine read; cancelling a waiter cannot cancel this owner."""
    from services.model_switch import models_enabled

    try:
        if not models_enabled():
            return None
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
        # There is no external lane any more: Orb is strictly offline, Pulse's
        # internet feeds were removed on 6 October 2026, and that same test
        # now fails the build if any server module calls a host that is not
        # loopback.
        async with asyncio.timeout(_PS_PROBE_TIMEOUT_SECONDS):
            async with httpx.AsyncClient(verify=local_model_ssl_context(), timeout=_PS_PROBE_TIMEOUT_SECONDS, trust_env=False) as client:
                resp = await client.get(f"{state.origin}/api/ps")
                resp.raise_for_status()
                # A model being torn down still appears in /api/ps (Ollama's
                # unload is async), but routing to it triggers a cold reload.
                names = frozenset(
                    m.get("name", "")
                    for m in resp.json().get("models", [])
                    if m.get("name") and not _is_expiring(m.get("expires_at"))
                )
        with _ps_state_lock:
            if state.epoch != _ps_epoch or state.origin != OLLAMA_BASE_URL or not models_enabled():
                return None
            state.cache = (time.monotonic(), names)
        return names
    except Exception as e:
        logger.debug("residency probe failed (%s) — treating as unknown", e)
        return None
    finally:
        with _ps_state_lock:
            if state.pending is asyncio.current_task():
                state.pending = None


async def probe_residency(timeout: float = 2.0) -> frozenset[str] | None:
    """Names held by the current local engine; None means unknown, not empty.

    Concurrent cold callers on the same loop/origin/configuration share one
    bounded read. Each caller keeps its own wait budget; a short timeout or
    cancellation does not cancel the shared read or another caller. The shared
    network read is capped at two seconds and failures are never cached.
    Invalidation/origin changes fence an in-flight result from every waiter.
    """
    from services.model_switch import models_enabled

    if not models_enabled() or not math.isfinite(timeout) or timeout <= 0:
        return None
    loop = asyncio.get_running_loop()
    origin = OLLAMA_BASE_URL
    with _ps_state_lock:
        state = _ps_states.get(loop)
        if state is None or state.epoch != _ps_epoch or state.origin != origin:
            state = _ResidencyState(origin, _ps_epoch)
            _ps_states[loop] = state
        if state.cache is not None and time.monotonic() - state.cache[0] < _PS_TTL_SECONDS:
            return state.cache[1]
        if state.pending is None:
            state.pending = loop.create_task(_fetch_residency(state), name="orb-residency-probe")
        pending = state.pending
    try:
        names = await asyncio.wait_for(asyncio.shield(pending), timeout)
    except TimeoutError:
        return None
    with _ps_state_lock:
        if (state.epoch != _ps_epoch or state.origin != OLLAMA_BASE_URL
                or state is not _ps_states.get(loop) or not models_enabled()):
            return None
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
    global _ps_epoch
    with _ps_state_lock:
        _ps_epoch += 1
        # Do not cancel another caller's task from this possibly different
        # thread/loop. Its bounded completion is fenced by the epoch instead.
        _ps_states.clear()


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
        preferred,
        fallback,
    )
    return ModelChoice(fallback, True, preferred)
