"""Orb: the models switch (ORB_MODELS_ENABLED, and the Orb Ward Record profile).

With the models switched off no local model runs on this computer: no request
reaches Ollama from any lane, Whisper does not transcribe, the knowledge
workers do not start, nothing is warmed at boot, and the routes of the model
features (Sage, Scribe drafting and its second pass, Lens, Relay's ambient
cards, Sage insights, the alert assessment) answer 404 in SWITCHED_OFF_DETAIL.

ORB_WARD_RECORD forces the switch off at boot (config.py). Read at call time,
so a test or an operator flip is seen at once.

Two ways to refuse, for two kinds of caller:
  * refuse_when_models_off(what) raises ModelsSwitchedOff. The inference
    gateway (services/ollama_gateway) and the provider seams call it before any
    request leaves this process, so a lane nobody gated still cannot reach a
    model. ModelsSwitchedOff is a RuntimeError; every lane already turns a
    failed inference into its deterministic fallback or an error it reports.
  * require_models_on is a FastAPI dependency that answers 404 before a route
    reads anything, for the routes of the model features themselves.
"""

from __future__ import annotations

import logging

from fastapi import HTTPException

logger = logging.getLogger("orb.models")

#: What a model feature's route answers while the models are switched off.
SWITCHED_OFF_DETAIL = "Switched off on this computer"


class ModelsSwitchedOff(RuntimeError):
    """A model was asked for while the models are switched off."""


def models_enabled() -> bool:
    """ORB_MODELS_ENABLED, and never on under the Orb Ward Record profile."""
    import config

    return bool(getattr(config, "MODELS_ENABLED", True)) and not bool(
        getattr(config, "WARD_RECORD", False)
    )


def background_models_enabled() -> bool:
    """Automatic inference jobs; independent of interactive permission/routing."""
    import config

    return models_enabled() and bool(getattr(config, "BACKGROUND_MODELS_ENABLED", True))


def refuse_when_models_off(what: str) -> None:
    """Raise ModelsSwitchedOff when the models are switched off."""
    if models_enabled():
        return
    logger.info("model request refused, models switched off: %s", what)
    raise ModelsSwitchedOff(
        f"{what}: the models are switched off on this computer "
        "(ORB_MODELS_ENABLED is off or ORB_WARD_RECORD is on)"
    )


def require_models_on() -> None:
    """FastAPI dependency: 404 SWITCHED_OFF_DETAIL while the models are off."""
    if not models_enabled():
        raise HTTPException(status_code=404, detail=SWITCHED_OFF_DETAIL)
