"""Orb Hospital OS — the synthetic vitals simulator, as a `VitalsSource` peer.

This is a REFACTOR, not a rewrite. `bot_service.run_vitals_bot` is unchanged and
still owns generation, batched persistence, pruning and the Vigil→Relay alert
bridge; what changes is that the loop around it now lives behind the same
lifecycle and health contract as a real monitor. That is the whole point of the
interface: simulator, manual entry and monitor are peers, so the ward's data
provenance is a property of the source rather than of which code path happened to
write the row.

Two behaviours are preserved deliberately and must stay preserved:

  * **Tick composition.** The reports and lab bots historically shared this one
    5-second tick and ran AFTER the vitals bot, sequentially, inside the same
    try/except. Splitting them into their own task would change their ordering
    and their failure coupling, so they are passed in as `companion_tick` and
    still run in the same place. They are not vitals; they are here because the
    tick is.
  * **Provenance.** Rows written by `bot_service` carry `source='simulated'`
    (the column default plus the explicit prune filter). `provenance_label` here
    is that same literal, so `classify_source_label` reports SIMULATOR for
    every row this source has ever produced, including historical ones.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

from .base import (
    LABEL_SIMULATED,
    STOP_TIMEOUT_SECONDS,
    SourceKind,
    VitalsSource,
    stop_task,
    utc_now_iso,
)

logger = logging.getLogger("orb.vitals_source.simulator")


class SimulatorSource(VitalsSource):
    """Runs the synthetic vitals bot on a fixed interval."""

    kind = SourceKind.SIMULATOR
    provenance_label = LABEL_SIMULATED

    def __init__(
        self,
        *,
        sio=None,
        interval_seconds: float = 5.0,
        source_id: str = "simulator",
        tick: Callable[[], Awaitable[None]] | None = None,
        companion_tick: Callable[[], Awaitable[None]] | None = None,
        stop_timeout: float = STOP_TIMEOUT_SECONDS,
    ):
        super().__init__(source_id)
        self._sio = sio
        self.interval_seconds = float(interval_seconds)
        #: Longest `stop()` will wait for the tick loop before abandoning it.
        self.stop_timeout = float(stop_timeout)
        self._tick = tick
        self._companion_tick = companion_tick
        self._task: asyncio.Task | None = None
        # Loops that `stop()` had to abandon (see below). Kept only so a
        # restart can say out loud that a second writer is still alive.
        self._abandoned: set[asyncio.Task] = set()

    async def _default_tick(self) -> None:
        # Imported inside the tick, not at module import: bot_service pulls in
        # forecast_service → torch, and this package is imported by the
        # ingestion layer, which must stay light.
        from services.bot_service import run_vitals_bot

        await run_vitals_bot(self._sio)

    async def start(self) -> None:
        if self._running:
            return
        if self._abandoned:
            # A previous stop() gave up on a loop that would not cancel. It is
            # still ticking, so this start makes TWO writers of synthetic
            # vitals in one process — the hazard, said out loud, rather than
            # silently doubling the ward's row rate.
            logger.error(
                "[%s] starting a new tick loop while %d abandoned loop(s) are "
                "still alive — synthetic vitals will be written twice until "
                "this process restarts",
                self.source_id,
                len(self._abandoned),
            )
        self.stats.started_at = time.monotonic()
        # Set BEFORE the task exists: `_loop` uses this flag as its non-
        # cancellation exit, so it must never observe a False that start()
        # simply has not written yet.
        self._running = True
        self._task = asyncio.create_task(self._loop(), name=f"{self.source_id}-loop")
        logger.info("[%s] synthetic vitals every %ss", self.source_id, self.interval_seconds)

    async def stop(self) -> None:
        if not self._running:
            return
        # Clear the flag first: it is what lets `_loop` exit on its own even if
        # something below it swallowed our cancellation.
        self._running = False
        task, self._task = self._task, None
        if task is not None:
            # BOUNDED. `await task` here used to be unbounded, and the except
            # arm below it (`except (asyncio.CancelledError, Exception)`)
            # swallowed cancellation aimed at US as well — so a tick that had
            # itself swallowed a CancelledError anywhere downstream left this
            # await hanging forever, and the whole appliance with it.
            if not await stop_task(
                task, label=self.source_id, timeout=self.stop_timeout, log=logger
            ):
                # Visible on the admin surface, not just in a log line nobody
                # greps at 03:00: an abandoned loop is still writing rows.
                self.stats.note_error(
                    f"stop() abandoned the tick loop after {self.stop_timeout}s; "
                    "it may still be writing vitals"
                )
                self._abandoned.add(task)
                task.add_done_callback(self._abandoned.discard)
        logger.info("[%s] stopped", self.source_id)

    async def _loop(self) -> None:
        tick = self._tick or self._default_tick
        while self._running:
            try:
                await tick()
                self.stats.messages_accepted += 1
                self.stats.last_reading_at = utc_now_iso()
                if self._companion_tick is not None:
                    await self._companion_tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                # Identical to the pre-refactor handler in main.py: a bot
                # failure is logged and the loop keeps its cadence.
                self.stats.note_error(str(e))
                logger.error("Simulation bot error: %s", e)
            # A tick that SWALLOWED the cancellation (some helper below it
            # catching CancelledError and returning) would otherwise leave this
            # loop running after stop() cleared the flag, holding shutdown open.
            # Re-check here so the loop has an exit that does not depend on
            # cancellation working all the way down.
            if not self._running:
                break
            await asyncio.sleep(self.interval_seconds)

    def describe(self) -> dict:
        return {**super().describe(), "interval_seconds": self.interval_seconds}
