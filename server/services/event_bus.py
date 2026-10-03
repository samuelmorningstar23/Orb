"""Orb Hospital OS — Local Event Bus (at-least-once delivery)

High-performance, local-first reactive publish-subscribe broker with a durable
write-ahead log AND a per-(event, subscriber) delivery ledger.

Delivery guarantee — the omniscient layer depends on it:
  Previously publish() persisted the event then fire-and-forget dispatched each
  subscriber with NO ack/replay, so a crash between persist and dispatch, or any
  subscriber exception, silently dropped the workflow (an ambient agent that
  misses "patient deteriorated" isn't ambient — it's asleep). Now:

    1. publish() persists the event (event_log WAL) AND records one 'pending'
       delivery row per matched subscriber (event_deliveries), THEN dispatches.
    2. On successful callback the delivery row flips to 'delivered'.
    3. On failure it stays 'pending' with attempts++ and last_error recorded.
    4. replay_undelivered() re-dispatches every still-'pending' row — called on
       startup (catches crash-dropped work) and on a periodic poller (catches
       subscriber exceptions and transient failures). After MAX_ATTEMPTS a row is
       marked 'dead' so a poison event can't loop forever.

Replay is guarded three ways, because a delivery row stays 'pending' for the
whole time its callback runs and heavy subscribers (RAG retrieval, the
single-slot LLM queue) routinely outlive the poll interval:
  * an in-memory in-flight set of (event_id, subscriber) keys, so a poll tick
    never re-dispatches a delivery whose earlier dispatch is still queued or
    running (otherwise each tick multiplies the same work);
  * a replay re-dispatch counts as an attempt, so a delivery that never acks
    (hung, not raising) eventually parks as 'dead' instead of retrying forever;
  * rows whose subscriber is not registered in this process are excluded from
    the query, so they cannot occupy the ORDER BY created_at LIMIT window and
    silently starve newer, replayable deliveries.

Subscribers are identified by a STABLE id (module.qualname), so a replay after
restart can find the same callback. Redelivery is at-least-once, so subscribers
should be idempotent (the existing ones are: they use INSERT OR REPLACE / dedup
keys / idempotent upserts).
"""
import asyncio
import atexit
import json
import uuid
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Callable, Awaitable, Any, Optional
from datetime import datetime, timezone
from database import db_session

logger = logging.getLogger(__name__)

# Every DB write in this module runs here rather than on the event loop, and
# rather than on asyncio's default executor.
#
# One worker, deliberately. SQLite allows exactly one writer, so fanning these
# writes across many threads would not make them any more parallel — it would
# just park N threads on the same lock. Worse, to_thread's default executor is
# shared process-wide (the ward narrator's snapshot builder uses it too), so an
# alert burst could occupy every slot in it and stall unrelated work. A private
# single-thread lane serialises event-bus writes in publish order, bounds the
# cost of a storm to one thread, and leaves the shared pool alone.
_DB_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="orb-eventbus-db")
atexit.register(_DB_EXECUTOR.shutdown, wait=False)


async def _in_db_thread(fn, *args):
    """Run a blocking event-bus DB write off the event loop."""
    return await asyncio.get_running_loop().run_in_executor(_DB_EXECUTOR, fn, *args)

# Upper bound on concurrent subscriber dispatch tasks. Without this, an alert
# burst spawns one unbounded create_task per (event x subscriber), which can
# pile thousands of coroutines into the single-slot LLM queue and starve
# interactive Sage. The semaphore provides simple backpressure.
_MAX_CONCURRENT_DISPATCH = 64
# After this many failed attempts a delivery is parked as 'dead' so a poison
# event / permanently-broken subscriber cannot loop the poller forever.
MAX_ATTEMPTS = 8

# (event_id, subscriber) keys whose dispatch task exists but has not finished —
# including the time it spends waiting on the dispatch semaphore. A delivery row
# is only flipped off 'pending' when its callback returns/raises, so without this
# guard every replay tick re-dispatches work that is still in flight and the
# duplicates multiply faster than they complete. Single-process bus, single event
# loop: a plain set is sufficient (a persisted 'in_flight' status + lease would
# be required only for cross-process replay).
_IN_FLIGHT: set[tuple[str, str]] = set()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def subscriber_id(callback: Callable) -> str:
    """Stable identifier for a subscriber across restarts."""
    mod = getattr(callback, "__module__", "?")
    qual = getattr(callback, "__qualname__", getattr(callback, "__name__", repr(callback)))
    return f"{mod}.{qual}"


class StateEvent:
    def __init__(self, topic: str, source_node: str, event_type: str,
                 patient_id: Optional[str], payload: Dict[str, Any],
                 correlation_id: Optional[str] = None, event_id: Optional[str] = None,
                 timestamp: Optional[str] = None):
        self.event_id = event_id or str(uuid.uuid4())
        self.topic = topic
        self.source_node = source_node
        self.event_type = event_type
        self.patient_id = patient_id
        self.payload = payload
        self.timestamp = timestamp or _now()
        self.correlation_id = correlation_id or f"corr-{uuid.uuid4().hex[:12]}"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "event_id": self.event_id,
            "topic": self.topic,
            "source_node": self.source_node,
            "event_type": self.event_type,
            "patient_id": self.patient_id,
            "payload": self.payload,
            "timestamp": self.timestamp,
            "correlation_id": self.correlation_id,
        }

    @classmethod
    def from_row(cls, row) -> "StateEvent":
        """Reconstruct a StateEvent from a persisted event_log row (for replay),
        preserving the original event_id/timestamp/correlation_id."""
        try:
            payload = json.loads(row["payload"]) if row["payload"] else {}
        except (TypeError, ValueError, json.JSONDecodeError):
            payload = {}
        return cls(
            topic=row["topic"], source_node=row["source_node"],
            event_type=row["event_type"], patient_id=row["patient_id"],
            payload=payload, correlation_id=row["correlation_id"],
            event_id=row["event_id"], timestamp=row["timestamp"],
        )


class LocalEventBus:
    def __init__(self):
        self._subscribers: Dict[str, List[Callable[[StateEvent], Awaitable[None]]]] = {}
        self._lock = asyncio.Lock()
        self._dispatch_sem: Optional[asyncio.Semaphore] = None
        self._poller_task: Optional[asyncio.Task] = None
        # One-shot warning about pending rows whose subscriber this build does
        # not register (they are excluded from replay, so they'd be silent).
        self._orphans_logged = False

    @property
    def dispatch_sem(self) -> asyncio.Semaphore:
        """Lazy-init the dispatch semaphore in the owning event loop."""
        if self._dispatch_sem is None:
            self._dispatch_sem = asyncio.Semaphore(_MAX_CONCURRENT_DISPATCH)
        return self._dispatch_sem

    def subscribe(self, topic: str, callback: Callable[[StateEvent], Awaitable[None]]):
        """Subscribe to events on a topic or pattern (e.g. 'vigil.*' or '*')."""
        if topic not in self._subscribers:
            self._subscribers[topic] = []
        if callback not in self._subscribers[topic]:
            self._subscribers[topic].append(callback)
            logger.info(
                "[EventBus] Registered subscription: %s (%s) for topic '%s'",
                getattr(callback, "__name__", repr(callback)), subscriber_id(callback), topic,
            )

    def _matched_callbacks(self, topic: str) -> list:
        matched = []
        for pattern, callbacks in self._subscribers.items():
            if self._match_topic(pattern, topic):
                matched.extend(callbacks)
        return matched

    def _callback_by_id(self, sub_id: str):
        for callbacks in self._subscribers.values():
            for cb in callbacks:
                if subscriber_id(cb) == sub_id:
                    return cb
        return None

    def _registered_subscriber_ids(self) -> List[str]:
        """Every subscriber id registered in THIS process (deduped)."""
        seen: Dict[str, None] = {}
        for callbacks in self._subscribers.values():
            for cb in callbacks:
                seen.setdefault(subscriber_id(cb), None)
        return list(seen)

    def _spawn_dispatch(self, callback: Callable, event: StateEvent, sid: str,
                        attempt_recorded: bool = False) -> bool:
        """Create the dispatch task unless this (event, subscriber) is already in
        flight. Returns True when a task was actually created."""
        key = (event.event_id, sid)
        if key in _IN_FLIGHT:
            return False
        _IN_FLIGHT.add(key)
        task = asyncio.create_task(self._safe_dispatch(callback, event, attempt_recorded))
        # A task cancelled before its first step never runs the coroutine body,
        # so its finally-clause release would never fire and the key would block
        # this delivery from ever being replayed again.
        task.add_done_callback(lambda _t, k=key: _IN_FLIGHT.discard(k))
        return True

    async def publish(self, event: StateEvent):
        """Publish an event: persist to the WAL, record a pending delivery per
        matched subscriber, then dispatch. Delivery is now tracked, so a crash
        or subscriber exception is recoverable via replay_undelivered()."""
        # 1. Persist Event to the write-ahead log.
        #
        # OFF THE LOOP. Every write in this class used to run inline on the event
        # loop thread, and publish() alone did three of them (this insert, the
        # delivery intents below, and the ack in _safe_dispatch). Each one blocks
        # on SQLite's single writer lock, which the vitals simulator and the audit
        # hash-chain are also competing for. Under real load that serialised the
        # entire API behind the event bus: /api/health stopped answering for
        # MINUTES, and because a blocked loop cannot run signal handlers the
        # worker ignored SIGTERM too. Captured live 2026-07-30 with
        # `kill -USR1 <worker>`: the main thread sat in db_session.__exit__ under
        # event_bus._mark_delivered.
        #
        # Handing them to a thread is the right tool: sqlite3 RELEASES the GIL around
        # execute/commit, so a thread parked on the writer lock costs the loop
        # nothing, while the ordering guarantees here are unchanged (each await
        # still completes before the next step).
        await _in_db_thread(self._persist_event, event)

        # 2. Match subscribers and record a pending delivery row for each.
        async with self._lock:
            matched = self._matched_callbacks(event.topic)

        sub_ids = [subscriber_id(cb) for cb in matched]
        if sub_ids:
            await _in_db_thread(
                self._record_delivery_intents, event.event_id, event.topic, sub_ids
            )

        # 3. Dispatch (bounded). Each dispatch acks/nacks its delivery row.
        for cb, sid in zip(matched, sub_ids):
            self._spawn_dispatch(cb, event, sid)

    def _persist_event(self, event: StateEvent):
        """Blocking WAL write. Always call via _in_db_thread from the loop."""
        try:
            with db_session() as db:
                db.execute(
                    """INSERT OR IGNORE INTO event_log (event_id, topic, source_node, event_type, patient_id, payload, timestamp, correlation_id)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (event.event_id, event.topic, event.source_node, event.event_type,
                     event.patient_id, json.dumps(event.payload), event.timestamp, event.correlation_id)
                )
        except Exception:
            logger.error(
                "[EventBus] Failed to persist event %s (topic=%s, corr=%s) to event_log; "
                "WAL durability NOT guaranteed for this event",
                event.event_id, event.topic, event.correlation_id, exc_info=True,
            )

    def _record_delivery_intents(self, event_id: str, topic: str, sub_ids: list):
        try:
            with db_session() as db:
                for sid in sub_ids:
                    db.execute(
                        """INSERT OR IGNORE INTO event_deliveries
                           (event_id, subscriber, topic, status, attempts, created_at, updated_at)
                           VALUES (?, ?, ?, 'pending', 0, ?, ?)""",
                        (event_id, sid, topic, _now(), _now()),
                    )
        except Exception:
            logger.error("[EventBus] Failed to record delivery intents for %s", event_id, exc_info=True)

    def _mark_delivered(self, event_id: str, sub_id: str):
        try:
            with db_session() as db:
                db.execute(
                    "UPDATE event_deliveries SET status='delivered', updated_at=? WHERE event_id=? AND subscriber=?",
                    (_now(), event_id, sub_id),
                )
        except Exception:
            logger.error("[EventBus] Failed to mark delivered %s/%s", event_id, sub_id, exc_info=True)

    def _mark_attempt(self, event_id: str, sub_id: str) -> int:
        """Count a replay re-dispatch as an attempt and return the new count.

        attempts used to be incremented only when a callback RAISED, so a
        delivery that hangs (or is simply slower than the poll interval) stayed
        'pending' with attempts=0 forever and was re-dispatched by every tick.
        Counting the re-dispatch itself lets the existing MAX_ATTEMPTS check
        park such a delivery as 'dead'. A late ack still wins: _mark_delivered
        flips the row to 'delivered' regardless of status.

        The UPDATE deliberately excludes rows already marked 'delivered'. The
        caller used to reach this line with no await between create_task and
        here, so an ack could not land in between; now that both the ack and
        this call go through the DB thread, they genuinely interleave, and
        without the guard a dispatch that finished quickly would have its
        'delivered' overwritten back to 'pending' — and then be replayed
        forever. Filtering in SQL keeps it correct regardless of who wins.
        """
        try:
            with db_session() as db:
                # Increment IN SQL, never read-modify-write in Python. This and
                # _mark_failed both touch the same row and now genuinely
                # interleave; the old SELECT-then-UPDATE pair meant whichever
                # wrote last stamped the attempts value it had read earlier,
                # silently undoing the other's increment. The counter then never
                # reached MAX_ATTEMPTS and a permanently-failing delivery was
                # replayed forever instead of being parked as dead.
                db.execute(
                    f"""UPDATE event_deliveries
                           SET attempts = attempts + 1,
                               status = CASE WHEN attempts + 1 >= {MAX_ATTEMPTS}
                                             THEN 'dead' ELSE 'pending' END,
                               updated_at = ?
                         WHERE event_id = ? AND subscriber = ? AND status != 'delivered'""",
                    (_now(), event_id, sub_id),
                )
                row = db.execute(
                    "SELECT attempts, status FROM event_deliveries WHERE event_id=? AND subscriber=?",
                    (event_id, sub_id),
                ).fetchone()
                if row is None:
                    return 0
                if row["status"] == "dead":
                    logger.error(
                        "[EventBus] Delivery %s/%s parked as DEAD after %s attempts "
                        "(never acked — subscriber hung or persistently failing)",
                        event_id, sub_id, row["attempts"],
                    )
                return row["attempts"]
        except Exception:
            logger.error("[EventBus] Failed to count attempt %s/%s", event_id, sub_id, exc_info=True)
            return 0

    def _mark_failed(self, event_id: str, sub_id: str, error: str, count_attempt: bool = True):
        # count_attempt=False when the replay path already counted this attempt,
        # so a replayed-then-failed delivery burns one attempt, not two.
        inc = 1 if count_attempt else 0
        try:
            with db_session() as db:
                # Relative increment for the same reason as _mark_attempt: these
                # two writes race, and a read-modify-write here would roll the
                # counter back. With inc=0 the attempts column is left exactly
                # as the replay path set it.
                db.execute(
                    f"""UPDATE event_deliveries
                           SET attempts = attempts + {inc},
                               status = CASE WHEN attempts + {inc} >= {MAX_ATTEMPTS}
                                             THEN 'dead' ELSE 'pending' END,
                               last_error = ?, updated_at = ?
                         WHERE event_id = ? AND subscriber = ? AND status != 'delivered'""",
                    ((error or "")[:500], _now(), event_id, sub_id),
                )
                row = db.execute(
                    "SELECT attempts, status FROM event_deliveries WHERE event_id=? AND subscriber=?",
                    (event_id, sub_id),
                ).fetchone()
                if row is not None and row["status"] == "dead":
                    logger.error(
                        "[EventBus] Delivery %s/%s parked as DEAD after %s attempts: %s",
                        event_id, sub_id, row["attempts"], error,
                    )
        except Exception:
            logger.error("[EventBus] Failed to mark failed %s/%s", event_id, sub_id, exc_info=True)

    def _match_topic(self, pattern: str, topic: str) -> bool:
        if pattern == "*" or pattern == topic:
            return True
        if pattern.endswith(".*"):
            prefix = pattern[:-2]
            return topic.startswith(prefix)
        return False

    async def _safe_dispatch(self, callback: Callable[[StateEvent], Awaitable[None]], event: StateEvent,
                             attempt_recorded: bool = False):
        cb_name = getattr(callback, "__name__", repr(callback))
        sid = subscriber_id(callback)
        try:
            async with self.dispatch_sem:
                try:
                    await callback(event)
                    # Off-loop for the same reason as publish(): this ack is a
                    # SQLite write, and it ran on the loop for every delivery.
                    await _in_db_thread(self._mark_delivered, event.event_id, sid)
                except Exception as e:
                    # No longer a silent drop: the delivery row stays 'pending' and
                    # replay_undelivered() will re-run it (bounded by MAX_ATTEMPTS).
                    logger.error(
                        "[EventBus] Error in subscriber %s processing '%s' (corr=%s); "
                        "delivery marked for replay",
                        cb_name, event.topic, event.correlation_id, exc_info=True,
                    )
                    await _in_db_thread(
                        self._mark_failed, event.event_id, sid, str(e),
                        not attempt_recorded,
                    )
        finally:
            # Released only once the callback has fully settled — holding it
            # across the semaphore wait is the point: queued duplicates are what
            # multiplied the work.
            _IN_FLIGHT.discard((event.event_id, sid))

    def _query_replayable(self, placeholders: str, known_ids: list, limit: int) -> list:
        """Blocking replay scan. Always call via _in_db_thread."""
        try:
            with db_session() as db:
                rows = db.execute(
                    f"""SELECT d.event_id, d.subscriber,
                               e.topic, e.source_node, e.event_type, e.patient_id,
                               e.payload, e.timestamp, e.correlation_id
                        FROM event_deliveries d
                        JOIN event_log e ON e.event_id = d.event_id
                        WHERE d.status = 'pending' AND d.attempts < ?
                          AND d.subscriber IN ({placeholders})
                        ORDER BY d.created_at ASC LIMIT ?""",
                    (MAX_ATTEMPTS, *known_ids, limit),
                ).fetchall()
                if not self._orphans_logged:
                    self._orphans_logged = True
                    orphans = db.execute(
                        f"""SELECT COUNT(*) AS n FROM event_deliveries
                            WHERE status = 'pending' AND subscriber NOT IN ({placeholders})""",
                        tuple(known_ids),
                    ).fetchone()
                    if orphans and orphans["n"]:
                        logger.warning(
                            "[EventBus] %s pending delivery row(s) reference subscribers not "
                            "registered in this process; excluded from replay",
                            orphans["n"],
                        )
        except Exception:
            logger.error("[EventBus] replay_undelivered query failed", exc_info=True)
            return []
        return rows

    async def replay_undelivered(self, limit: int = 200) -> int:
        """Re-dispatch every still-'pending' delivery whose event and subscriber
        are both known. Returns the number re-dispatched. Idempotent-safe: it
        only re-runs deliveries that never acked, and subscribers are idempotent.

        Deliveries already in flight are skipped — a 'pending' row does not mean
        nobody is working on it, it means nobody has acked yet.
        """
        known_ids = self._registered_subscriber_ids()
        if not known_ids:
            return 0

        # Rows for subscribers this build does not register can never be
        # replayed, but they are the OLDEST rows, so leaving them in the
        # ORDER BY created_at LIMIT window would silently starve every newer
        # replayable delivery behind them. Filter them out in SQL.
        placeholders = ",".join("?" for _ in known_ids)

        # Same rule as publish(): the replay scan is a multi-table join over
        # event_deliveries + event_log and it runs on a timer, so leaving it on
        # the loop meant a periodic stall proportional to the backlog.
        rows = await _in_db_thread(self._query_replayable, placeholders, known_ids, limit)

        redispatched = 0
        skipped_in_flight = 0
        for row in rows:
            sid = row["subscriber"]
            cb = self._callback_by_id(sid)
            if cb is None:
                # Raced with an unregister between query and lookup — leave
                # pending; a future build that registers it will pick it up.
                continue
            event = StateEvent.from_row(row)
            if not self._spawn_dispatch(cb, event, sid, attempt_recorded=True):
                skipped_in_flight += 1
                continue
            # Counts the re-dispatch itself, so a delivery that never acks
            # (rather than raising) still walks towards 'dead'. This await lets
            # the dispatch task run, so it CAN ack before we get here —
            # _mark_attempt's UPDATE excludes 'delivered' rows for exactly that
            # reason, and the two are safe in either order.
            await _in_db_thread(self._mark_attempt, event.event_id, sid)
            redispatched += 1
        if redispatched or skipped_in_flight:
            logger.info(
                "[EventBus] Replayed %s undelivered event(s) (%s still in flight, skipped)",
                redispatched, skipped_in_flight,
            )
        return redispatched

    def start_replay_poller(self, interval_seconds: int = 45):
        """Launch a background loop that periodically replays pending deliveries.
        Idempotent — a second call is a no-op if the poller is already running."""
        if self._poller_task and not self._poller_task.done():
            return

        async def _loop():
            # Initial catch-up (crash recovery) then steady-state polling.
            try:
                await self.replay_undelivered()
            except Exception:
                logger.error("[EventBus] initial replay failed", exc_info=True)
            while True:
                await asyncio.sleep(interval_seconds)
                try:
                    await self.replay_undelivered()
                except Exception:
                    logger.error("[EventBus] periodic replay failed", exc_info=True)

        self._poller_task = asyncio.create_task(_loop())
        logger.info("[EventBus] delivery replay poller started (every %ss)", interval_seconds)


# Global Event Bus Singleton
event_bus = LocalEventBus()
