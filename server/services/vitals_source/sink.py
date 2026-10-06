"""Orb Hospital OS — where readings from a `VitalsSource` actually land.

There is exactly ONE persistence path for clinical vitals in this process:
`services.ingestion_service.ingest_payload`. It already owns raw-payload capture,
fingerprint deduplication, patient resolution, carry-forward, the hash-chained
audit trail, 7-parameter NEWS2, the Vigil→Relay alert bridge, shadow episodes and
the department-scoped Socket.IO emit — and every one of those has a comment on it
explaining a production incident. A second writer for monitor data would drift
from all of it silently.

So this sink is deliberately thin. It does three things the ingestion pipeline
cannot do for itself, because they depend on knowing which source spoke:

  1. **Stamps provenance** — passes the source's `provenance_label` down so
     `vitals.source` says `monitor_hl7`, not a generic feed label.
  2. **Orders the stream** — a monitor that reconnects replays its buffer, and a
     serial gateway can retransmit out of order. Readings the DEVICE ITSELF
     dates before one it already sent are marked `backfill`: stored in full, but
     not allowed to overwrite the live risk score or page a nurse.
  3. **Never raises at the source** — a source's ingest call returns a result or
     a counted failure, so a bad message cannot unwind a socket handler.

WHY ORDERING IS JUDGED IN THE DEVICE'S OWN CLOCK, AND LIVENESS IS NOT
─────────────────────────────────────────────────────────────────────
This file used to answer "is this reading current?" by comparing the device's
timestamp to OUR wall clock and discarding anything more than 15 minutes old.
That is wrong in a way that kills people, and it was proven over a real socket:
a monitor whose clock was 20 minutes slow sent six messages carrying HR 132 /
SpO2 84 / RR 30 — NEWS2 ≈ 10, a crashing patient — and 100 % of them were
classified as backfill and excluded from scoring, alerting and the live emit.
Orb said nothing. The device was not sending old data; its CLOCK was wrong.

So the two questions are now separated, because they have different evidence:

  * **Ordering** (does this reading precede one I already have?) is answered
    device-clock against device-clock, per (source, patient). A constant clock
    offset cancels out of that comparison entirely, which is exactly the
    property we want — a device 20 minutes slow still orders its own stream
    perfectly.
  * **Liveness** (is this happening now?) is answered by RECEIPT TIME. The
    appliance's clock is the one clock we control, and "it arrived just now" is
    a fact, not an inference. Device time is still recorded — it is what the
    clinical record and the ordering guard need — but it never decides whether
    a nurse gets paged.

A device whose clock disagrees with ours is therefore not a muted patient; it
is an OPERATIONAL FAULT, counted in `SourceStats`, exposed in `status()` and
logged at ERROR with the measured skew. The ward gets told its monitor's clock
is wrong, and the patient keeps getting alerted on. That asymmetry is the whole
argument: under-suppressing costs a clinician one redundant reading;
over-suppressing hides a deterioration, and only one of those is survivable.

What we gave up: the wall-clock window was also the restart guard, because the
high-water marks below are in memory. Two things still cover that case, and
neither can silence a live patient. First, a monitor replaying its buffer after
an Orb restart resends the SAME bytes, and `ingestion_service` dedups on the
payload fingerprint, so those never reach scoring twice. Second, anything that
is genuinely new to us is a reading we have never seen — alerting on it once is
the safe direction to fail. Persisting the high-water marks across restarts is
the real fix for the residual case and is noted as follow-up work.
"""
from __future__ import annotations

import logging
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional, Protocol, Sequence

from services.ingestion_adapters import IngestionAdapter, JSONAdapter
from services.ingestion_service import IngestionResult, ingest_payload

from . import hl7v2
from .base import ReadingHint, VitalsReading, VitalsSource

logger = logging.getLogger("orb.vitals_source.sink")

# How far a device's clock may differ from the appliance's before we call it a
# fault. 15 minutes is well past any plausible buffering or network delay and
# comfortably inside the interval at which a ward would notice a missing
# observation, so anything beyond it is a misconfigured clock rather than a slow
# link. NOTE WHAT THIS NUMBER NO LONGER DOES: it does not discard readings, it
# does not suppress alerting, and it does not mark anything backfill. It only
# decides when to raise a visible fault about the DEVICE.
DEFAULT_CLOCK_SKEW_TOLERANCE_SECONDS = 900

# Retained under its old name so existing configuration and imports keep
# working. It is deliberately no longer a "window" that anything falls outside
# of — see the module docstring for why that concept was removed.
DEFAULT_LIVE_WINDOW_SECONDS = DEFAULT_CLOCK_SKEW_TOLERANCE_SECONDS

# Cap on tracked (source, patient) high-water marks. Two 60-bed wards is 120
# entries; 4096 is room for a decade of the pilot and a hard ceiling against a
# device that invents a new patient id per message.
_MAX_TRACKED_PATIENTS = 4096

# Cap on tracked clocks. One entry per (source, device); a ward has a handful.
_MAX_TRACKED_CLOCKS = 256


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    """Tolerant ISO-8601 → aware UTC datetime. Never raises.

    A naive timestamp (no offset) is read in the appliance's configured zone,
    NOT as UTC — the same rule `hl7v2.parse_timestamp` applies, for the same
    reason: a wall-clock reading with no offset means local time, and this
    hospital's local time is UTC+05:30. Reading it as UTC put every such reading
    5.5 hours in the future.
    """
    if not value:
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=hl7v2.appliance_timezone())
    return dt.astimezone(timezone.utc)


# ════════════════════════════════════════════
#  Clock skew — a wrong clock is a ward fault, not a muted patient
# ════════════════════════════════════════════

@dataclass
class ClockSkewRecord:
    """What we know about one device's clock. PHI-free by construction.

    Keyed by (source, device) and holding no patient identifier, because this
    record is rendered on the admin surface.
    """
    device_id: str
    readings: int = 0
    readings_out_of_tolerance: int = 0
    faults: int = 0                                  # transitions into "skewed"
    last_skew_seconds: float = 0.0                   # + = device ahead of us
    max_abs_skew_seconds: float = 0.0
    state: str = "ok"                                # ok | skewed
    # Whether the device stated its own UTC offset. False means we supplied the
    # zone, so an operator reading a skew here has to consider "the timezone I
    # configured for this device is wrong" alongside "its clock is wrong".
    device_states_offset: Optional[bool] = None
    first_seen_at: Optional[str] = None
    last_seen_at: Optional[str] = None
    last_fault_at: Optional[str] = None

    def as_dict(self) -> dict:
        return dict(self.__dict__)


@dataclass
class LivenessDecision:
    """The sink's verdict on one payload: ordering and clock, kept separate."""
    backfill: bool = False                  # device dates it before one we hold
    skew_seconds: Optional[float] = None    # device time − receipt time
    skew_state: str = "unknown"             # ok | skewed | unknown
    device_id: Optional[str] = None
    device_states_offset: Optional[bool] = None
    reason: str = ""

    @property
    def clock_skewed(self) -> bool:
        return self.skew_state == "skewed"


class VitalsSink(Protocol):
    """What a `VitalsSource` is allowed to assume about its destination."""

    async def submit_payload(
        self, raw_payload: str, *, source: VitalsSource,
        correlation_id: Optional[str] = None,
        external_message_id: Optional[str] = None,
        adapter: Optional[IngestionAdapter] = None,
        hints: Sequence[ReadingHint] = (),
    ) -> IngestionResult: ...

    async def submit_readings(
        self, readings: Sequence[VitalsReading], *, source: VitalsSource,
        correlation_id: Optional[str] = None,
    ) -> IngestionResult: ...


class IngestionSink:
    """Production sink: everything goes through `ingestion_service`."""

    def __init__(self, *, sio=None,
                 skew_tolerance_seconds: Optional[int] = None,
                 live_window_seconds: Optional[int] = None):
        """`live_window_seconds` is the pre-fix name for `skew_tolerance_seconds`.

        Kept accepted so existing configuration keeps loading, but it no longer
        names a window that readings fall out of — see the module docstring.
        """
        self._sio = sio
        tolerance = skew_tolerance_seconds
        if tolerance is None:
            tolerance = (live_window_seconds if live_window_seconds is not None
                         else DEFAULT_CLOCK_SKEW_TOLERANCE_SECONDS)
        self._skew_tolerance = timedelta(seconds=max(0, int(tolerance)))
        # (source_id, patient_identifier) -> newest DEVICE time seen so far.
        self._high_water: "OrderedDict[tuple[str, str], datetime]" = OrderedDict()
        # (source_id, device_id) -> what we know about that device's clock.
        self._clocks: "OrderedDict[tuple[str, str], ClockSkewRecord]" = OrderedDict()

    # ── ordering + clock ──

    def classify(self, source_id: str, hints: Iterable[ReadingHint], *,
                 stats=None, now: Optional[datetime] = None) -> LivenessDecision:
        """Judge one payload's ordering and its device's clock, separately.

        Ordering (the `backfill` flag) compares DEVICE time to DEVICE time and
        is the only thing that can suppress live scoring/alerting. A message is
        backfill only when EVERY reading in it is one the device itself dates
        before something we already hold: under-suppressing shows a clinician
        one redundant reading, over-suppressing hides a deterioration, and only
        one of those is survivable.

        Clock skew compares device time to RECEIPT time. It never suppresses
        anything. It raises a counted, visible fault about the device.
        """
        receipt = now or datetime.now(timezone.utc)
        decision = LivenessDecision()
        saw_any = False
        all_stale = True
        worst_skew: Optional[float] = None

        for hint in hints:
            if not hint.patient_identifier:
                continue
            observed = _parse_iso(hint.observed_at)
            if observed is None:
                # No usable timestamp ⇒ treat as current. A source that cannot
                # date its readings must not have them silently muted.
                decision.reason = "undateable reading treated as live"
                decision.skew_state = "unknown"
                return decision
            saw_any = True

            # ── ordering: device clock vs device clock ──
            key = (source_id, hint.patient_identifier)
            previous = self._high_water.get(key)
            if previous is None or observed >= previous:
                all_stale = False
            if previous is None or observed > previous:
                self._high_water[key] = observed
                self._high_water.move_to_end(key)
                while len(self._high_water) > _MAX_TRACKED_PATIENTS:
                    self._high_water.popitem(last=False)

            # ── clock: device clock vs OUR clock ──
            skew = (observed - receipt).total_seconds()
            if worst_skew is None or abs(skew) > abs(worst_skew):
                worst_skew = skew
                decision.device_id = hint.device_id
                decision.device_states_offset = hint.device_time_had_offset

        if not saw_any:
            decision.reason = "no dateable readings"
            decision.skew_state = "unknown"
            return decision

        decision.backfill = all_stale
        if decision.backfill:
            decision.reason = "device dates every reading before one already held"

        if worst_skew is not None:
            in_tolerance = abs(worst_skew) <= self._skew_tolerance.total_seconds()
            decision.skew_seconds = round(worst_skew, 3)
            decision.skew_state = "ok" if in_tolerance else "skewed"
            self._record_clock(source_id, decision, in_tolerance=in_tolerance)
            if stats is not None:
                stats.note_clock_skew(worst_skew, in_tolerance=in_tolerance)
        return decision

    def is_backfill(self, source_id: str, hints: Iterable[ReadingHint]) -> bool:
        """True when the DEVICE dates every reading before one we already hold.

        Wall-clock distance from now is deliberately NOT part of this answer.
        """
        return self.classify(source_id, hints).backfill

    def _record_clock(self, source_id: str, decision: LivenessDecision, *,
                      in_tolerance: bool) -> None:
        """Update the per-device clock record and shout once when it goes bad."""
        device_id = decision.device_id or "unidentified-device"
        key = (source_id, device_id)
        record = self._clocks.get(key)
        if record is None:
            record = ClockSkewRecord(device_id=device_id,
                                     first_seen_at=datetime.now(timezone.utc).isoformat())
            self._clocks[key] = record
            while len(self._clocks) > _MAX_TRACKED_CLOCKS:
                self._clocks.popitem(last=False)
        self._clocks.move_to_end(key)

        skew = decision.skew_seconds or 0.0
        record.readings += 1
        record.last_skew_seconds = skew
        record.max_abs_skew_seconds = max(record.max_abs_skew_seconds, round(abs(skew), 3))
        record.last_seen_at = datetime.now(timezone.utc).isoformat()
        record.device_states_offset = decision.device_states_offset

        if in_tolerance:
            if record.state == "skewed":
                logger.warning(
                    "[%s] device %s clock is back within tolerance (skew %+.1fs)",
                    source_id, device_id, skew)
            record.state = "ok"
            return

        record.readings_out_of_tolerance += 1
        record.last_fault_at = record.last_seen_at
        if record.state != "skewed":
            # Log the TRANSITION, not every message: a device 20 minutes out
            # sends one of these every few seconds and would drown the log it is
            # supposed to be visible in.
            record.faults += 1
            record.state = "skewed"
            logger.error(
                "[%s] CLOCK SKEW: device %s is %+.1f minutes from appliance time "
                "(tolerance %.0f min, device %s its UTC offset). Readings are still "
                "being scored and alerted on using RECEIPT time — but this device's "
                "recorded observation times are wrong and its clock needs setting.",
                source_id, device_id, skew / 60.0,
                self._skew_tolerance.total_seconds() / 60.0,
                "stated" if decision.device_states_offset else "did NOT state",
            )

    def clock_skew_report(self, source_id: Optional[str] = None) -> dict:
        """PHI-free clock health, for `status()` and the admin surface."""
        records = [r for (sid, _), r in self._clocks.items()
                   if source_id is None or sid == source_id]
        skewed = [r for r in records if r.state == "skewed"]
        return {
            "state": "skewed" if skewed else "ok",
            "tolerance_seconds": int(self._skew_tolerance.total_seconds()),
            # The appliance-wide default. A source with a per-device override
            # reports its own zone as `assumed_timezone` in `describe()`; these
            # are deliberately named differently so a mismatch between them is
            # readable rather than hidden behind one ambiguous key.
            "appliance_timezone": str(getattr(hl7v2.appliance_timezone(), "key", None)
                                      or hl7v2.appliance_timezone()),
            "devices_tracked": len(records),
            "devices_skewed": len(skewed),
            "devices": [r.as_dict() for r in records],
        }

    def forget(self, source_id: str) -> None:
        """Drop ordering state for a source (used when it is stopped).

        Clock records are KEPT: "the monitor in bay 3 has a wrong clock" is an
        operational finding that survives a feed restart, and losing it on every
        reconnect is how a fault becomes invisible.
        """
        for key in [k for k in self._high_water if k[0] == source_id]:
            del self._high_water[key]

    # ── submission ──

    async def submit_payload(
        self, raw_payload: str, *, source: VitalsSource,
        correlation_id: Optional[str] = None,
        external_message_id: Optional[str] = None,
        adapter: Optional[IngestionAdapter] = None,
        hints: Sequence[ReadingHint] = (),
    ) -> IngestionResult:
        """Ingest a raw wire payload (e.g. the HL7 message a monitor sent).

        The RAW message is what gets handed on, not a re-serialised version of
        our parse of it: `ingestion_events.raw_payload` is the evidence trail a
        clinical review reads when it wants to know what the device actually
        said, and it has to be the device's bytes.
        """
        # `stats=` is what makes the clock fault countable: the sink measures the
        # skew, the source's own counters carry it, and `status()` shows it.
        decision = self.classify(source.source_id, hints, stats=source.stats)
        return await ingest_payload(
            raw_payload=raw_payload,
            adapter=adapter,
            source_name=source.source_id,
            correlation_id=correlation_id,
            external_message_id=external_message_id,
            sio=self._sio,
            source_label=source.provenance_label,
            backfill=decision.backfill,
        )

    async def submit_readings(
        self, readings: Sequence[VitalsReading], *, source: VitalsSource,
        correlation_id: Optional[str] = None,
    ) -> IngestionResult:
        """Ingest structured readings (manual entry, or any non-wire source).

        Rendered into the documented JSON webhook shape so it travels the exact
        same pipeline as everything else rather than a private fast path.
        """
        import json

        usable = [r for r in readings if r and not r.is_empty and r.patient_identifier]
        if not usable:
            return IngestionResult(correlation_id=correlation_id, parse_status="empty")

        hints = [ReadingHint(r.patient_identifier, r.observed_at,
                             device_id=r.device_id or source.source_id)
                 for r in usable]
        decision = self.classify(source.source_id, hints, stats=source.stats)
        backfill = decision.backfill

        # One payload per patient: ingest_payload's dedup fingerprint and its
        # ingestion_events row are both per-message, and a mixed-patient blob
        # would make a single duplicate poison the whole batch.
        result = IngestionResult(correlation_id=correlation_id, parse_status="parsed",
                                 backfill=backfill)
        for reading in usable:
            payload = json.dumps(reading.to_json_payload(), sort_keys=True)
            single = await ingest_payload(
                raw_payload=payload,
                adapter=JSONAdapter(),
                source_name=reading.device_id or source.source_id,
                content_type="application/json",
                correlation_id=correlation_id,
                external_message_id=reading.external_message_id,
                sio=self._sio,
                source_label=source.provenance_label,
                backfill=backfill,
            )
            result.event_id = single.event_id or result.event_id
            result.received += single.received
            result.inserted_vitals += single.inserted_vitals
            result.inserted_observations += single.inserted_observations
            result.duplicates += single.duplicates
            result.errors.extend(single.errors)
            if single.parse_status not in ("parsed", "duplicate"):
                result.parse_status = single.parse_status
        return result


class RecordingSink:
    """In-memory sink for tests and bench runs — records, never touches the DB.

    Mirrors `IngestionSink`'s ordering logic exactly (it reuses it) so a test
    that asserts backfill behaviour is asserting the production rule.
    """

    def __init__(self, *, skew_tolerance_seconds: Optional[int] = None,
                 live_window_seconds: Optional[int] = None):
        self._order = IngestionSink(skew_tolerance_seconds=skew_tolerance_seconds,
                                    live_window_seconds=live_window_seconds)
        self.payloads: list[dict] = []
        self.readings: list[VitalsReading] = []

    async def submit_payload(self, raw_payload, *, source, correlation_id=None,
                             external_message_id=None, adapter=None, hints=()):
        decision = self._order.classify(source.source_id, hints, stats=source.stats)
        self.payloads.append({
            "raw": raw_payload,
            "source_id": source.source_id,
            "provenance_label": source.provenance_label,
            "correlation_id": correlation_id,
            "external_message_id": external_message_id,
            "backfill": decision.backfill,
            # Recorded alongside so a bench run can show what the device's clock
            # was doing without the skew having changed the clinical outcome.
            "skew_seconds": decision.skew_seconds,
            "skew_state": decision.skew_state,
        })
        return IngestionResult(correlation_id=correlation_id, parse_status="parsed",
                               received=1, inserted_vitals=1, backfill=decision.backfill)

    async def submit_readings(self, readings, *, source, correlation_id=None):
        usable = [r for r in readings if r and not r.is_empty and r.patient_identifier]
        hints = [ReadingHint(r.patient_identifier, r.observed_at,
                             device_id=r.device_id or source.source_id)
                 for r in usable]
        decision = self._order.classify(source.source_id, hints, stats=source.stats)
        for reading in usable:
            reading.notes = "backfill" if decision.backfill else reading.notes
            self.readings.append(reading)
        return IngestionResult(correlation_id=correlation_id, parse_status="parsed",
                               received=len(usable), inserted_vitals=len(usable),
                               backfill=decision.backfill)

    def clock_skew_report(self, source_id: Optional[str] = None) -> dict:
        return self._order.clock_skew_report(source_id)

    def forget(self, source_id: str) -> None:
        self._order.forget(source_id)


__all__ = [
    "VitalsSink", "IngestionSink", "RecordingSink",
    "ClockSkewRecord", "LivenessDecision",
    "DEFAULT_CLOCK_SKEW_TOLERANCE_SECONDS", "DEFAULT_LIVE_WINDOW_SECONDS",
]
