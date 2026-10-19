"""Orb: the one door to the local model's inference endpoints.

Every /api/chat and /api/generate request a service makes goes through post()
or stream() below, and each call leaves exactly one row on the audit hash chain
(ai_governance.record_inference): the purpose, the model, the patient when
there is one, the outcome, and a SHA-256 of the prompt (ai_governance.
hash_prompt). The chain never holds the prompt itself.

The exceptions, each named in tests/test_ollama_gateway_guard.py: the two
provider seams (llm_providers for Sage chat and Sage Tier 2, scribe_providers
for Scribe), whose callers write the row because they also serve non-Ollama
engines or own the turn's provenance; and main.py's warm, load and unload
pings, which carry no patient data and manage residency, not answers.

WHY ONE DOOR (2026-10-03). Eleven lanes posted to Ollama directly and only Sage
chat and the ward narrator wrote an audit row, while the collateral promised
that every inference was audited. A rule in a comment had already drifted once,
so tests/test_ollama_gateway_guard.py fails when a module posts to an inference
endpoint itself.

Callers keep their own httpx client (its timeout is their lane budget) and pass
it in; this module only adds the URL, the audit row and, for background work,
the yield below.

YIELDING (F036). A P3 caller that passes yield_to_clinician=True gives the GPU
back when a clinician's turn arrives or its hold reaches the P3 cap
(llm_queue.yield_reason). The request is abandoned, which closes the connection,
and Ollama stops generating for a client that hung up. LaneYielded subclasses
asyncio.TimeoutError, like BackgroundAdmissionRejected, so every caller's
existing timeout handler already takes its deterministic fallback.

Audit failures are logged and swallowed (ai_governance.record_inference_async),
pending the open 2026-09-04 ruling on refusing unaudited inferences.
"""
from __future__ import annotations

import asyncio
import contextlib
import time
from typing import AsyncIterator

from services.ai_governance import (
    QueryClassification,
    hash_prompt,
    record_inference_async,
)
from services.model_switch import ModelsSwitchedOff, refuse_when_models_off  # noqa: F401

# How often a yielding request re-checks the lane. A clinician's turn waits at
# most this long for the background request to let go.
_YIELD_POLL_SECONDS = 0.1


class LaneYielded(asyncio.TimeoutError):
    """A P3 request gave the background lane back before the model answered.

    An asyncio.TimeoutError on purpose: every background caller already treats
    one as "take the deterministic fallback", so yielding needs no new handler.
    """


def _url(path: str) -> str:
    # Read at call time, not import time: tests point config.OLLAMA_BASE_URL at
    # a closed port to reproduce a dead daemon.
    import config
    from offline_policy import local_model_origin
    return f"{local_model_origin(config.OLLAMA_BASE_URL)}{path}"


async def _record(payload: dict, purpose: str, outcome: str, detail: str | None,
                  t0: float, patient_id, user_id, classification) -> None:
    await record_inference_async(
        provider="ollama",
        model=str(payload.get("model") or ""),
        purpose=purpose,
        outcome=outcome,
        classification=classification,
        user_id=user_id,
        patient_id=patient_id,
        timing_ms={"total": round((time.monotonic() - t0) * 1000)},
        detail=detail,
        prompt_sha256=hash_prompt(payload),
    )


async def _post_yielding(client, url: str, payload: dict):
    """client.post, abandoned as soon as the lane asks for the GPU back."""
    from services.llm_queue_service import llm_queue

    reason = llm_queue.yield_reason()
    if reason:
        raise LaneYielded(reason)
    request = asyncio.ensure_future(client.post(url, json=payload))
    try:
        while True:
            done, _ = await asyncio.wait({request}, timeout=_YIELD_POLL_SECONDS)
            if done:
                return request.result()
            reason = llm_queue.yield_reason()
            if reason:
                raise LaneYielded(reason)
    finally:
        if not request.done():
            request.cancel()
            # Wait for the cancel to land so the connection is closed before
            # the caller releases the lane. asyncio.wait never raises the
            # request's own CancelledError, and our own cancel still propagates.
            await asyncio.wait({request})


async def post(client, path: str, payload: dict, *, purpose: str,
               patient_id: str | None = None, user_id: str | None = None,
               classification: QueryClassification | None = None,
               yield_to_clinician: bool = False):
    """One non-streaming inference: client.post(OLLAMA_BASE_URL + path).

    Returns the httpx response untouched, so callers keep their own status and
    parsing. `yield_to_clinician` is for P3 background work only: see the
    module docstring.

    With the models switched off (ORB_MODELS_ENABLED, or the Orb Ward Record)
    it raises ModelsSwitchedOff before any request, and writes no audit row:
    no inference happened.
    """
    refuse_when_models_off(f"ollama_gateway.post {purpose}")
    t0 = time.monotonic()
    outcome, detail = "error", None
    try:
        if yield_to_clinician:
            resp = await _post_yielding(client, _url(path), payload)
        else:
            resp = await client.post(_url(path), json=payload)
        status = getattr(resp, "status_code", 200)
        if status == 200:
            outcome = "served"
        else:
            detail = f"HTTP {status}"
        return resp
    except LaneYielded as e:
        detail = str(e)
        raise
    except asyncio.CancelledError:
        detail = "cancelled"
        raise
    except Exception as e:
        # The class only: an exception message can quote the request.
        detail = type(e).__name__
        raise
    finally:
        await _record(payload, purpose, outcome, detail, t0,
                      patient_id, user_id, classification)


@contextlib.asynccontextmanager
async def stream(client, path: str, payload: dict, *, purpose: str,
                 patient_id: str | None = None, user_id: str | None = None,
                 classification: QueryClassification | None = None,
                 ) -> AsyncIterator:
    """One streaming inference: `async with client.stream("POST", ...)`.

    Yields the httpx response; the caller reads its lines exactly as before.
    The row is written when the block exits. A reader that stops early (a
    client closing the page) still got tokens from the model, so that is
    recorded as served, with the early close noted.

    Refused with ModelsSwitchedOff, and no row written, while the models are
    switched off (see post()).
    """
    refuse_when_models_off(f"ollama_gateway.stream {purpose}")
    t0 = time.monotonic()
    outcome, detail = "error", None
    try:
        async with client.stream("POST", _url(path), json=payload) as response:
            status = getattr(response, "status_code", 200)
            try:
                yield response
            except GeneratorExit:
                outcome, detail = "served", "stream closed by the reader"
                raise
            if status == 200:
                outcome = "served"
            else:
                detail = f"HTTP {status}"
    except asyncio.CancelledError:
        detail = detail or "cancelled"
        raise
    except Exception as e:
        detail = detail or type(e).__name__
        raise
    finally:
        await _record(payload, purpose, outcome, detail, t0,
                      patient_id, user_id, classification)
