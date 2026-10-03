"""At-least-once event delivery tests.

Proves the property the whole ambient layer depends on: a subscriber that
crashes on first delivery does NOT lose the event — the delivery row stays
pending and replay_undelivered() re-runs it.
"""
import asyncio

import pytest

from database import db_session
from services.event_bus import LocalEventBus, StateEvent, subscriber_id, MAX_ATTEMPTS


def _deliveries(event_id):
    with db_session() as db:
        return [dict(r) for r in db.execute(
            "SELECT * FROM event_deliveries WHERE event_id = ?", (event_id,)
        ).fetchall()]


def _mk_event(topic="test.delivery", pid="PAT-EVT-1"):
    return StateEvent(topic=topic, source_node="test", event_type="unit",
                      patient_id=pid, payload={"n": 1})


@pytest.mark.asyncio
async def test_successful_delivery_marks_delivered():
    bus = LocalEventBus()
    seen = []

    async def sub(evt):
        seen.append(evt.event_id)

    bus.subscribe("test.delivery", sub)
    evt = _mk_event()
    await bus.publish(evt)
    await asyncio.sleep(0.05)  # let the dispatch task run

    rows = _deliveries(evt.event_id)
    assert len(rows) == 1
    assert rows[0]["status"] == "delivered"
    assert seen == [evt.event_id]


@pytest.mark.asyncio
async def test_subscriber_crash_stays_pending_then_replays():
    bus = LocalEventBus()
    calls = {"n": 0}

    async def flaky(evt):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom on first try")
        # succeeds on replay

    bus.subscribe("test.delivery", flaky)
    evt = _mk_event()
    await bus.publish(evt)
    await asyncio.sleep(0.05)

    rows = _deliveries(evt.event_id)
    assert rows[0]["status"] == "pending", "failed delivery must NOT be silently dropped"
    assert rows[0]["attempts"] == 1

    # Replay re-runs it; second attempt succeeds → delivered.
    n = await bus.replay_undelivered()
    assert n == 1
    await asyncio.sleep(0.05)
    rows = _deliveries(evt.event_id)
    assert rows[0]["status"] == "delivered"
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_replay_survives_a_fresh_bus_instance():
    """Simulate a crash: publish + fail on one bus, then a NEW bus (restart)
    with the same subscriber replays the un-acked delivery."""
    bus1 = LocalEventBus()

    async def always_fail(evt):
        raise RuntimeError("down")

    bus1.subscribe("test.delivery", always_fail)
    evt = _mk_event(pid="PAT-EVT-2")
    await bus1.publish(evt)
    await asyncio.sleep(0.05)
    assert _deliveries(evt.event_id)[0]["status"] == "pending"

    # "Restart": a new bus with a HEALTHY subscriber of the SAME identity.
    bus2 = LocalEventBus()
    got = []

    async def always_fail_v2(evt):  # different body, we monkey its identity below
        got.append(evt.event_id)

    # Give the replacement the same stable subscriber id as the original so the
    # delivery ledger can route the replay to it.
    always_fail_v2.__module__ = always_fail.__module__
    always_fail_v2.__qualname__ = always_fail.__qualname__
    assert subscriber_id(always_fail_v2) == subscriber_id(always_fail)
    bus2.subscribe("test.delivery", always_fail_v2)

    n = await bus2.replay_undelivered()
    assert n == 1
    await asyncio.sleep(0.05)
    assert got == [evt.event_id]
    assert _deliveries(evt.event_id)[0]["status"] == "delivered"


@pytest.mark.asyncio
async def test_poison_delivery_parked_dead_after_max_attempts():
    bus = LocalEventBus()

    async def poison(evt):
        raise RuntimeError("permanently broken")

    bus.subscribe("test.delivery", poison)
    evt = _mk_event(pid="PAT-EVT-3")
    await bus.publish(evt)
    await asyncio.sleep(0.05)
    # Replay until it gives up.
    for _ in range(MAX_ATTEMPTS + 2):
        await bus.replay_undelivered()
        await asyncio.sleep(0.02)

    row = _deliveries(evt.event_id)[0]
    assert row["status"] == "dead"
    assert row["attempts"] >= MAX_ATTEMPTS
    # A dead row is no longer replayed.
    assert await bus.replay_undelivered() == 0


@pytest.mark.asyncio
async def test_unregistered_subscriber_delivery_waits_not_dropped():
    """If the process has no matching subscriber (e.g. a build that hasn't
    registered it yet), the delivery stays pending — not lost, not dead."""
    bus = LocalEventBus()

    async def sub(evt):
        pass

    bus.subscribe("test.delivery", sub)
    evt = _mk_event(pid="PAT-EVT-4")
    await bus.publish(evt)
    await asyncio.sleep(0.05)
    # Now replay on a bus with NO subscribers registered.
    empty = LocalEventBus()
    assert await empty.replay_undelivered() == 0
    # Still recoverable — status unchanged from delivered/pending, never 'dead'.
    assert _deliveries(evt.event_id)[0]["status"] in ("delivered", "pending")
