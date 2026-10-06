"""Orb Hospital OS — the `VitalsSource` contract.

Where a vital sign came from is a clinical fact, not a implementation detail. A
NEWS2 of 7 built from a monitor's SpO2 trace and a NEWS2 of 7 a nurse typed at
the bedside are different evidence, and the shadow scorecard has to be able to
tell them apart after the fact — otherwise "Orb detected deterioration 40 minutes
early" is an unfalsifiable claim.

So this module defines three things:

  1. `SourceKind`      — what KIND of thing produced a reading. Monitor, manual
                          and simulator are PEERS: none is the special case.
  2. `VitalsReading`    — one patient, one instant, N vitals, plus the identity
                          of the source and device that produced it.
  3. `VitalsSource`     — a named, supervised producer with a lifecycle
                          (`start`/`stop`) and observable health (`status`).

The hospital's actual monitor make and model is not known yet — that is the #1
deliverable of a site visit that has not happened. The point of this interface is
that when a model IS named, the remaining work is ONE `VitalsSource` subclass,
not an architecture: everything downstream (provenance labelling, persistence,
NEWS2, alerting, shadow logging, health reporting) is already wired to the
contract rather than to HL7.
"""
from __future__ import annotations

import asyncio
import logging
import math
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

logger = logging.getLogger("orb.vitals_source")


# ════════════════════════════════════════════
#  Source kinds and provenance labels
# ════════════════════════════════════════════

class SourceKind(str, Enum):
    """How a reading physically came to exist.

    MONITOR / MANUAL / SIMULATOR are the three peers named in the roadmap.
    DERIVED and UNKNOWN exist because the database already contains rows that
    honestly are neither — pretending otherwise would make the classifier lie.
    """
    MONITOR = "monitor"        # a machine measured it
    MANUAL = "manual"          # a human read a device and typed it
    SIMULATOR = "simulator"    # synthetic; must never be mistaken for either
    DERIVED = "derived"        # extracted from a document by software (e.g. Scribe)
    UNKNOWN = "unknown"        # arrived over a generic feed that asserted no kind


# The string written into `vitals.source` for each source in this package.
# These are STABLE WIRE VALUES: the shadow scorecard, exports and any clinical
# review read them back. Adding a label means adding it to _LABEL_KIND below.
LABEL_MONITOR_HL7 = "monitor_hl7"
LABEL_MANUAL_ENTRY = "manual_entry"
LABEL_SIMULATED = "simulated"
# The nurse observation path (nurse_shift_service.record_observation, wrapped
# by ManualEntrySource.record_observation). The literal predates this package
# and rows already exist under it, so it is registered as-is rather than
# renamed to LABEL_MANUAL_ENTRY — the two stay distinguishable: `manual_entry`
# travelled the ingestion pipeline, `nurse_manual` is the charting path's
# direct transactional write.
LABEL_NURSE_OBSERVATION = "nurse_manual"
# The admission-time vitals set (admission_service, wrapped by
# ManualEntrySource.record_at_admission). Same reasoning as above: the literal
# predates this package and rows already exist under it, so it is registered
# as-is rather than renamed. It also must never be the 'simulated' default —
# the bot prune DELETES simulated rows, and a real admission set is not the
# bot's to delete.
LABEL_ADMISSION_VITALS = "clinician"
# The Orb Ward Record's clerk lane (6 October 2026): a set the ward clerk copied
# from the paper observation sheet, through the same nurse observation route
# under vitals.transcribe. A person took it at the bedside, so it is MANUAL; its
# timestamp is the time written on the sheet and vitals.entered_at is when it
# was keyed.
LABEL_CLERK_TRANSCRIBED = "clerk_transcribed"

# Every value any part of Orb writes into `vitals.source`, mapped to its kind.
# tests/test_vitals_source.py asserts this covers every literal the repo writes,
# so a new writer cannot silently create an unclassifiable reading.
_LABEL_KIND: dict[str, SourceKind] = {
    # ── this package ──
    LABEL_MONITOR_HL7: SourceKind.MONITOR,
    LABEL_MANUAL_ENTRY: SourceKind.MANUAL,
    LABEL_NURSE_OBSERVATION: SourceKind.MANUAL,
    LABEL_ADMISSION_VITALS: SourceKind.MANUAL,
    LABEL_CLERK_TRANSCRIBED: SourceKind.MANUAL,
    LABEL_SIMULATED: SourceKind.SIMULATOR,
    # ── pre-existing writers ──
    "llm_extracted": SourceKind.DERIVED,     # nervous_system_triggers: parsed from a signed note
    # Generic inbound feeds. These are deliberately UNKNOWN rather than MONITOR:
    # /api/ingest/hl7 carries both monitor vitals AND lab panels from whatever
    # gateway holds the API key, and asserting "machine-measured at the bedside"
    # for all of it would be a fabricated provenance claim. A monitor wired
    # through this package gets LABEL_MONITOR_HL7 instead.
    "ingested_hl7": SourceKind.UNKNOWN,
    "ingested_json": SourceKind.UNKNOWN,
    "ingested_fhir": SourceKind.UNKNOWN,
    "csv_replay": SourceKind.UNKNOWN,
}


def classify_source_label(label: Optional[str]) -> SourceKind:
    """Map a `vitals.source` value to its `SourceKind`.

    Unrecognised labels return UNKNOWN — never a guess. A reading whose origin
    we cannot name must not be counted as machine-measured evidence.
    """
    if not label:
        return SourceKind.UNKNOWN
    return _LABEL_KIND.get(label.strip().lower(), SourceKind.UNKNOWN)


def is_machine_measured(label: Optional[str]) -> bool:
    """True only for readings a device produced. The scorecard's honesty test."""
    return classify_source_label(label) is SourceKind.MONITOR


# ════════════════════════════════════════════
#  Readings
# ════════════════════════════════════════════

# The six vitals Orb's `vitals` table and NEWS2 scoring understand. Adapters map
# their own codes onto these keys (see ingestion_adapters.map_to_vital_key).
VITAL_KEYS = ("HR", "SBP", "DBP", "SpO2", "Temp", "RR")

# Names used by the JSON webhook shape, which is the canonical way this package
# hands structured readings to the ingestion pipeline.
_VITAL_JSON_NAME = {
    "HR": ("8867-4", "Heart Rate", "bpm"),
    "SBP": ("8480-6", "Systolic Blood Pressure", "mmHg"),
    "DBP": ("8462-4", "Diastolic Blood Pressure", "mmHg"),
    "SpO2": ("2708-6", "Oxygen Saturation", "%"),
    "Temp": ("8310-5", "Body Temperature", "Cel"),
    "RR": ("9279-1", "Respiratory Rate", "/min"),
}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class VitalsReading:
    """One patient, one instant, one or more vitals, with its origin attached.

    `values` holds only the vitals the source actually measured — a missing key
    means "not measured", never "zero". The ingestion path carries forward the
    patient's last known value for anything absent, so inventing a 0 here would
    fabricate a clinical observation.
    """
    patient_identifier: str
    observed_at: str                                   # ISO 8601
    values: dict[str, float] = field(default_factory=dict)
    source_id: str = ""                                # which VitalsSource instance
    kind: SourceKind = SourceKind.UNKNOWN
    device_id: Optional[str] = None                    # bed / monitor identity
    encounter_identifier: Optional[str] = None
    consciousness: Optional[str] = None                # ACVPU, when the source knows it
    on_oxygen: Optional[bool] = None
    entered_by: Optional[str] = None                   # user id, for MANUAL
    external_message_id: Optional[str] = None          # sender's own dedup key
    notes: Optional[str] = None

    def __post_init__(self):
        # Drop anything that is not a real, finite number for a known vital, at
        # the boundary, rather than letting a None, a string or a NaN reach
        # NEWS2. `float("nan")` succeeds and then passes every range check,
        # which is how a disconnected lead scores as a normal vital sign.
        cleaned: dict[str, float] = {}
        for key, raw in (self.values or {}).items():
            if key not in VITAL_KEYS or raw is None:
                continue
            try:
                value = float(raw)
            except (TypeError, ValueError):
                continue
            if not math.isfinite(value):
                continue
            cleaned[key] = value
        self.values = cleaned
        if not self.observed_at:
            self.observed_at = utc_now_iso()

    @property
    def is_empty(self) -> bool:
        return not self.values

    def to_json_payload(self) -> dict:
        """Render as the documented `/api/ingest/json` webhook shape.

        Structured sources hand readings to the ingestion pipeline through this
        shape so there is exactly ONE persistence path in the process — dedup,
        raw-event capture, audit, NEWS2, Vigil alerting, shadow episodes and the
        Socket.IO emit all come from `ingestion_service`, not from a second
        implementation that will drift from it.
        """
        observations = []
        for key, value in self.values.items():
            code, name, unit = _VITAL_JSON_NAME[key]
            observations.append({
                "code": code,
                "name": name,
                "value": value,
                "unit": unit,
                "status": "final",
                "observed_at": self.observed_at,
            })
        payload = {
            "patient_id": self.patient_identifier,
            "observed_at": self.observed_at,
            "source_name": self.device_id or self.source_id,
            "observations": observations,
        }
        if self.encounter_identifier:
            payload["encounter_id"] = self.encounter_identifier
        if self.external_message_id:
            payload["message_id"] = self.external_message_id
        return payload


@dataclass
class ReadingHint:
    """The minimum a sink needs to order a message against what it already has.

    Used for the out-of-order / backfill guard without re-parsing the payload or
    hitting the database.

    `device_id` names the CLOCK the timestamp came off — one monitor, one clock.
    Clock skew is tracked per device rather than per patient, both because that
    is where the fault actually lives (a wrong clock is wrong for every bed it
    serves) and because the skew report is shown on an admin screen, where a
    patient identifier would be PHI.

    `device_time_had_offset` records whether the device stated its UTC offset or
    we supplied one. A skew computed against an ASSUMED zone is a weaker claim
    than one computed against a stated offset, and an operator reading the fault
    needs to be able to tell "your clock is wrong" from "your timezone is".
    """
    patient_identifier: str
    observed_at: Optional[str]
    device_id: Optional[str] = None
    device_time_had_offset: Optional[bool] = None


# ════════════════════════════════════════════
#  Source health
# ════════════════════════════════════════════

@dataclass
class SourceStats:
    """Counters every source reports. A feed that has gone quiet is a clinical
    event, so `last_reading_at` matters as much as the totals."""
    started_at: Optional[float] = None
    connected: bool = False
    connections_opened: int = 0
    connections_closed: int = 0
    messages_received: int = 0
    messages_accepted: int = 0
    messages_rejected: int = 0
    readings_ingested: int = 0
    duplicates: int = 0
    backfilled: int = 0            # accepted, but too old to drive a live alert
    parse_failures: int = 0
    overflow_rejections: int = 0   # sender out-ran us; NACKed so it can retry
    last_reading_at: Optional[str] = None
    last_error: Optional[str] = None
    last_error_at: Optional[str] = None

    # ── clock skew ──
    # A device whose clock disagrees with the appliance's is an OPERATIONAL
    # FAULT the ward has to be told about, not a reason to stop alerting on the
    # patient. These counters exist so the fault is visible and countable on the
    # admin surface instead of being a log line nobody greps at 03:00.
    #
    # These are a SOURCE-level summary. When one source carries several devices,
    # `clock_skew_state` and `last_clock_skew_seconds` describe the most recent
    # reading's device and will move between devices. The authoritative
    # per-device view — which device, how far out, since when — is the sink's
    # `clock_skew_report()`, surfaced as `status()["clock_skew"]`.
    clock_skew_readings: int = 0        # readings arriving outside the tolerance
    clock_skew_faults: int = 0          # times a device CROSSED into fault state
    last_clock_skew_seconds: Optional[float] = None   # + = device ahead of us
    max_abs_clock_skew_seconds: float = 0.0
    clock_skew_state: str = "ok"        # ok | skewed (most recent reading)

    def note_error(self, message: str) -> None:
        self.last_error = str(message)[:300]
        self.last_error_at = utc_now_iso()

    def note_clock_skew(self, skew_seconds: float, *, in_tolerance: bool) -> None:
        """Record one reading's device-vs-receipt clock delta.

        Called for EVERY dateable reading, in tolerance or not, so
        `last_clock_skew_seconds` is a live measurement rather than a high-water
        mark that never comes back down after one bad message.
        """
        self.last_clock_skew_seconds = round(float(skew_seconds), 3)
        self.max_abs_clock_skew_seconds = max(
            self.max_abs_clock_skew_seconds, round(abs(float(skew_seconds)), 3))
        if in_tolerance:
            self.clock_skew_state = "ok"
            return
        self.clock_skew_readings += 1
        if self.clock_skew_state != "skewed":
            # Count TRANSITIONS, not readings: a monitor 20 minutes out sends
            # one fault every few seconds, and a counter that ticks per message
            # tells an operator how chatty the device is, not how many devices
            # are broken.
            self.clock_skew_faults += 1
        self.clock_skew_state = "skewed"

    def as_dict(self) -> dict:
        data = dict(self.__dict__)
        if self.started_at:
            data["uptime_seconds"] = round(time.monotonic() - self.started_at, 1)
        return data


# ════════════════════════════════════════════
#  Bounded shutdown
# ════════════════════════════════════════════

#: How long a source's `stop()` waits for its own task to actually die before
#: abandoning it. Shutdown LIVENESS beats a tidy cleanup: this is an appliance
#: that restarts for upgrades, power events and launchd, and a shutdown path
#: that can block forever is an appliance that does not come back up. (Observed:
#: the process sat at "Waiting for application shutdown." indefinitely while the
#: reloader held the listen socket, so every HTTP request — /api/health
#: included — hung with no error, and the still-running worker kept writing to
#: the encrypted DB. A second worker had already been orphaned the same way,
#: which is the multi-writer hazard this repo has been burned by before.)
STOP_TIMEOUT_SECONDS = 5.0


async def stop_task(
    task: Optional[asyncio.Task],
    *,
    label: str,
    timeout: float = STOP_TIMEOUT_SECONDS,
    log: Optional[logging.Logger] = None,
) -> bool:
    """Cancel `task` and wait for it to finish, BOUNDED. True if it really died.

    Three properties, each of them a bug this repo has actually shipped:

      1. **Bounded.** A task can be made uncancellable by any `except` arm
         anywhere below it that catches `CancelledError` and returns instead of
         re-raising (in 3.12 it is a `BaseException`, so a bare
         `except Exception` does NOT catch it — but `except (TimeoutError,
         CancelledError)` does, and that shape is common). `await task` on such
         a task never returns. This waits out a deadline and then gives up.

      2. **`asyncio.wait`, not `asyncio.wait_for`.** `wait_for` implements its
         timeout by cancelling the AWAITING task; when the awaited object is a
         Task, that cancellation is forwarded straight into the uncancellable
         task and the awaiter stays blocked — i.e. `wait_for` does not bound
         this at all. `asyncio.wait({task}, timeout=...)` returns when the clock
         runs out and leaves the task alone, which is the only real bound.

      3. **Our own cancellation still propagates.** If the shutdown that called
         us is itself cancelled, that `CancelledError` is re-raised, never
         swallowed — swallowing it here would make `stop()` the next
         uncancellable thing in the chain.

    The task is cancelled a second time part-way through the budget: a task that
    swallowed the first `CancelledError` usually dies on the next one, because
    it lands at a different await.

    `timeout` is the maximum TOTAL wait and should be positive; a task that is
    still alive when it expires is abandoned, logged at ERROR, and reported by
    the False return so the caller can surface it (the sources put it in
    `stats.last_error`, where the admin surface shows it).
    """
    log = log or logger
    if task is None:
        return True

    if not task.done():
        task.cancel()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.0, float(timeout))
        rounds = 0
        while not task.done():
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            rounds += 1
            if rounds == 1:
                slice_seconds = remaining * 0.6
            else:
                task.cancel()          # second landing site for the cancel
                slice_seconds = remaining
            try:
                await asyncio.wait({task}, timeout=slice_seconds)
            except asyncio.CancelledError:
                # OUR wait was cancelled, not the task. Never swallow it.
                raise

    if not task.done():
        log.error(
            "[%s] task %r did NOT stop within %.1fs and is being ABANDONED so "
            "shutdown can finish. It may keep running (and writing) until the "
            "process exits — that is a cancellation bug in the task, not here.",
            label, task.get_name(), timeout,
        )
        return False

    if not task.cancelled():
        # Harvest the result so a task that died of a real error is reported
        # once, here, instead of surfacing later as "exception was never
        # retrieved" — but a failing task must never block or break shutdown.
        exc = task.exception()
        if exc is not None:
            log.warning("[%s] task %r ended with %s: %s",
                        label, task.get_name(), type(exc).__name__, exc)
    return True


# ════════════════════════════════════════════
#  The interface
# ════════════════════════════════════════════

class VitalsSource(ABC):
    """A named, supervised producer of vital signs.

    Implementations must obey three rules, all of them learned the hard way in
    this repo:

      1. **Nothing blocking on the event loop.** A source's `start`/`stop` and
         any loop it owns are asyncio; all database work goes through
         `database.run_db` (which the shared sink already does).
      2. **Bad input is normal.** `start()` must not let a parse error, a
         disconnect, a flood or a garbage byte propagate. Failures are counted
         in `stats` and logged; they never reach the caller's loop.
      3. **Provenance is not optional.** Everything a source persists carries
         `provenance_label`, so a machine reading is distinguishable from a
         typed one forever after.
    """

    #: SourceKind this source produces.
    kind: SourceKind = SourceKind.UNKNOWN
    #: Value written into `vitals.source` for readings from this source.
    provenance_label: str = "unknown"

    def __init__(self, source_id: str):
        self.source_id = source_id
        self.stats = SourceStats()
        self._running = False

    # ── lifecycle ──

    @abstractmethod
    async def start(self) -> None:
        """Begin producing. Must return once the source is armed (not when it
        finishes) — long-lived work belongs on tasks the source owns."""

    @abstractmethod
    async def stop(self) -> None:
        """Stop producing and release sockets/tasks. Must be idempotent.

        Must also RETURN — in bounded time, whatever the tasks it owns do. Wait
        for them with `stop_task` (or an equivalent bounded wait) and abandon
        anything that will not die, loudly. A source that blocks here holds the
        whole appliance in shutdown; see `stop_task`'s docstring.
        """

    @property
    def running(self) -> bool:
        return self._running

    # ── observability ──

    def describe(self) -> dict:
        """Static identity — what this source IS, for the admin surface."""
        return {
            "source_id": self.source_id,
            "kind": self.kind.value,
            "provenance_label": self.provenance_label,
            "class": type(self).__name__,
        }

    def status(self) -> dict:
        """Identity + live health. Safe to call from a route handler."""
        return {**self.describe(), "running": self._running, "stats": self.stats.as_dict()}
