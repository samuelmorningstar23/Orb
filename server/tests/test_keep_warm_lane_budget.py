"""The keep-warm heartbeat must not black out the alert lane.

WHAT THIS IS ABOUT. `keep_warm_loop` re-loads an inference tier Ollama has
dropped. It takes the ONE background LLM slot to do it — the same slot Sage's
Tier-2 alert advice needs — and it used to hold that slot for the entire
duration of a 120 s httpx client timeout.

MEASURED ON THE RUNNING WARD, 2026-08-15, 5.5 h. `medgemma:27b` is pinned
(keep_alive=-1) and was serving Tier-2 drafts back to back, so Ollama never had
a window to load `medgemma:4b`. Every warm ping therefore hung the full 120 s
and NONE of them succeeded. Thirty-three of them:

    3939 s held = 20 % of wall clock, 60 % of all lane-occupancy
    Tier-2 loss inside those windows 58.9 %, outside 4.0 %
    53 of the 65 surviving `queue_timeout` rows fell inside one

and the whole thing was invisible: the timeout landed on `logger.debug`.

THE TWO PROPERTIES PINNED HERE, both of which are about the ALERT lane rather
than about warming:

  1. THE BUDGET IS A LANE BUDGET. A warm ping may not hold the slot longer than
     `_warm_budget_seconds()`. 120 s was sized on "a cold 27B load takes ~80 s",
     which is the wrong question — holding Orb's slot does not make Ollama's
     load finish any sooner.

  2. FAILURE BACKS OFF. Retrying a load that cannot complete, every interval,
     forever, turns a transient eviction into a permanent 2-minutes-in-10
     blackout. When the engine has no room, the honest outcome is that the
     ambient tier stays evicted — every ambient caller already checks
     `model_router.ambient_tier_evicted` and skips — not that the alert lane
     keeps paying for the attempt.

PROVEN ABLE TO FAIL: restoring the 120.0 timeout fails the budget tests;
returning a constant from `keep_warm_backoff_ticks` fails the backoff tests.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import main  # noqa: E402


# ── 1. the budget is a lane budget ───────────────────────────────────────────

def test_warm_budget_is_well_under_the_measured_blackout(monkeypatch):
    """Whatever the number is, it must not be the 120 s that was measured.

    Stated as a bound rather than an equality so the value can be tuned without
    editing a test — what must not come back is a budget long enough to lose a
    two-minute window of alert advice to a single ping.
    """
    monkeypatch.delenv("ORB_KEEP_WARM_BUDGET", raising=False)
    budget = main._warm_budget_seconds()
    assert budget <= 60.0, (
        f"keep-warm may hold the one background LLM slot for {budget}s. "
        "33 pings at 120s cost 20% of wall clock of Tier-2 advice on "
        "2026-08-15; the lane is one slot wide, so this number IS the blackout."
    )
    assert budget >= 5.0, "a budget this short cannot load anything"


def test_warm_budget_is_operator_tunable(monkeypatch):
    monkeypatch.setenv("ORB_KEEP_WARM_BUDGET", "20")
    assert main._warm_budget_seconds() == 20.0


def test_warm_budget_survives_a_nonsense_env_value(monkeypatch):
    """A typo in the env must not remove the bound — it falls back to default."""
    monkeypatch.setenv("ORB_KEEP_WARM_BUDGET", "not-a-number")
    assert main._warm_budget_seconds() == 45.0

    # And it can never be set low enough to be a no-op that never warms.
    monkeypatch.setenv("ORB_KEEP_WARM_BUDGET", "0")
    assert main._warm_budget_seconds() >= 5.0


# ── 2. failure backs off ─────────────────────────────────────────────────────

def test_a_success_checks_again_next_interval():
    """Backoff must be zero while things work, or a healthy engine goes stale."""
    assert main.keep_warm_backoff_ticks(0) == 0
    assert main.keep_warm_backoff_ticks(-1) == 0


def test_repeated_failure_backs_off_instead_of_retrying_every_interval():
    ticks = [main.keep_warm_backoff_ticks(f) for f in range(1, 8)]
    assert ticks == sorted(ticks), f"backoff must not shrink under failure: {ticks}"
    assert ticks[0] < ticks[-1], (
        "backoff never grows, so a model Ollama cannot load blacks out the "
        f"alert lane on every interval forever: {ticks}"
    )


def test_backoff_is_capped_so_a_recovered_engine_is_picked_back_up():
    """Unbounded doubling would mean an engine that recovers is never noticed."""
    for failures in (20, 100, 10_000):
        assert (main.keep_warm_backoff_ticks(failures)
                == main.KEEP_WARM_MAX_BACKOFF_TICKS)

    # At the default 300 s interval the cap must still re-probe within an hour.
    assert main.KEEP_WARM_MAX_BACKOFF_TICKS * 300 <= 3600 + 300


@pytest.mark.parametrize("failures,expected", [(1, 1), (2, 2), (3, 4), (4, 8)])
def test_backoff_doubles(failures, expected):
    assert main.keep_warm_backoff_ticks(failures) == expected
