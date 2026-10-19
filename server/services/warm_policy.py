"""Bounded local-model warming policy, without loading the application."""

import math
import os
from collections.abc import Collection, Iterable

MAX_BACKOFF_TICKS = 12


def warm_budget_seconds() -> float:
    try:
        value = float(os.getenv("ORB_KEEP_WARM_BUDGET", "45"))
    except (TypeError, ValueError):
        return 45.0
    return max(5.0, value) if math.isfinite(value) else 45.0


def keep_warm_backoff_ticks(failures: int) -> int:
    if failures <= 0:
        return 0
    # Cap the exponent before computing it, even after a very long outage.
    return min(1 << min(failures - 1, 4), MAX_BACKOFF_TICKS)


def select_models_to_warm(
    configured: Iterable[str], resident: Collection[str], operator_unloaded: Collection[str]
) -> list[str]:
    return [
        model
        for model in dict.fromkeys(configured)
        if model not in resident and model not in operator_unloaded
    ]
