"""Orb Hospital OS — bedside monitor `VitalsSource` (HL7 v2 over MLLP).

The hospital's monitor make and model is not known yet. What IS known, with high
confidence for this setting, is the protocol: bedside monitors and their central
stations emit HL7 v2 `ORU^R01` observation results over MLLP/TCP, either directly
or through a vendor gateway. That is what this adapter speaks, so the remaining
work when a model is finally named is a mapping table and a bench run — not an
architecture.

Two wiring directions are supported because we do not yet know which the site
uses, and both are common:

    mode="listen"   Orb opens a TCP port; the monitor/gateway connects and
                    pushes ORU messages. (The usual arrangement. Default.)
    mode="connect"  Orb dials out to a gateway that listens. (Some central
                    stations and serial-to-IP converters work this way.)

Both directions run the identical message path, so only the socket setup differs.

VENDOR PROFILES. The hospital's monitors are Philips or GE — not yet narrowed
further. Both speak this same ORU^R01; they differ in the OBX-3 identifiers and
OBX-6 units they put inside it. `profile=` selects the translation table
(`vendor_profiles.py`), `"auto"` guesses it per message from MSH-3/MSH-4, and
`"generic"` turns it off and maps LOINC only. The profile is consulted ONLY for
observations Orb's own LOINC table could not map, so a wrong guess in a vendor
table cannot corrupt a reading that was already understood — and every
identifier nobody could map is counted and NAMED in `status()`, because that
list is the whole point of a bench run before hardware arrives.

DESIGN RULE FOR THIS FILE — bad input from a device is NORMAL, not exceptional.
A monitor that disconnects mid-message, sends garbage, floods, retransmits, or
replays its buffer out of order is a Tuesday. Every one of those is handled
in-band and counted; none of them may raise into the asyncio loop, corrupt data,
or silently swallow a real reading. The three mechanisms:

  * framing      — `MllpFramer` is bounded and resynchronising (see mllp.py).
  * parsing      — `hl7v2.parse` raises only `Hl7ParseError`, caught here; the
                   sender gets a protocol-correct NACK so it knows to stop or
                   retry rather than block on its own ACK timeout.
  * backpressure — accepted messages go on a BOUNDED queue drained by a small
                   worker pool. If a flooding device out-runs the database we
                   answer `AE` (application error), which in HL7 means "I did
                   not take this, send it again" — the sender retries, the
                   reading is not lost, and the appliance does not swallow an
                   unbounded queue of PHI.

Nothing here touches the database. Persistence happens in the sink, which hands
off to `ingestion_service` (fully `run_db`-offloaded), so this module is pure
asyncio I/O and cannot wedge the loop.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections import Counter, deque
from datetime import tzinfo
from typing import Optional, Union

from . import hl7v2
from . import vendor_profiles
from .base import ReadingHint, SourceKind, VitalsSource, LABEL_MONITOR_HL7, utc_now_iso
from .mllp import DEFAULT_MAX_MESSAGE_BYTES, MllpFramer, frame
from .profile_adapter import VendorProfileAdapter

logger = logging.getLogger("orb.vitals_source.monitor")

# IANA-registered port for HL7 MLLP.
DEFAULT_MLLP_PORT = 2575

# Bounded work queue between the sockets and the database. 512 messages is
# roughly 8 minutes of a 60-bed ward at one observation per bed per minute —
# ample burst absorption, and small enough that a stuck device cannot pin
# hundreds of megabytes of PHI in memory.
DEFAULT_QUEUE_SIZE = 512

# Ingestion is I/O-bound on the DB pool (4 workers). Two drainers keep it fed
# without competing with clinical request traffic for the same pool.
DEFAULT_WORKERS = 2

# A ward has a handful of gateways, not hundreds. Refusing beyond this stops a
# misconfigured device (or anything else on the ward VLAN) from exhausting file
# descriptors through this port.
DEFAULT_MAX_CONNECTIONS = 16

# How many DISTINCT unmapped OBX identifiers to remember. These are device
# vocabulary (PHI-free) and the list is the artefact a bench run produces; it is
# bounded because a misconfigured gateway could otherwise stream unique
# identifiers until the process runs out of memory.
MAX_UNMAPPED_IDENTIFIERS = 64


class HL7MonitorSource(VitalsSource):
    """Receives HL7 v2 ORU^R01 vitals from a bedside monitor over MLLP."""

    kind = SourceKind.MONITOR
    provenance_label = LABEL_MONITOR_HL7

    def __init__(
        self,
        sink,
        *,
        source_id: str = "monitor_hl7",
        host: str = "127.0.0.1",
        port: int = DEFAULT_MLLP_PORT,
        mode: str = "listen",
        queue_size: int = DEFAULT_QUEUE_SIZE,
        workers: int = DEFAULT_WORKERS,
        max_connections: int = DEFAULT_MAX_CONNECTIONS,
        max_message_bytes: int = DEFAULT_MAX_MESSAGE_BYTES,
        reconnect_delay_seconds: float = 5.0,
        receiving_app: str = "ORB",
        device_timezone: Union[str, tzinfo, None] = None,
        profile: str = "auto",
        strict_profile: bool = False,
    ):
        super().__init__(source_id)
        if mode not in ("listen", "connect"):
            raise ValueError(f"mode must be 'listen' or 'connect', got {mode!r}")
        self.sink = sink
        self.host = host
        self.port = int(port)
        self.mode = mode
        # THE ZONE THIS PARTICULAR DEVICE'S OFFSET-LESS TIMESTAMPS ARE IN.
        # An HL7 DTM with no offset is legal and is what most bedside monitors
        # emit — they print local wall-clock time. None means "use the
        # appliance's zone", which is right when the monitor and the appliance
        # sit on the same ward. It is NOT right for a gateway someone configured
        # in UTC, or a second ward's feed on a different site, and two devices on
        # one ward disagreeing is exactly how a timezone bug bites twice. So the
        # setting lives per SOURCE, and one Orb can run one source per clock.
        self.device_timezone = hl7v2.resolve_timezone(device_timezone)
        if device_timezone and self.device_timezone is None:
            logger.error(
                "[%s] device_timezone=%r is not a usable timezone; falling back to the "
                "appliance zone (%s). Offset-less timestamps from this device will be "
                "read in that zone.",
                source_id, device_timezone, hl7v2.appliance_timezone(),
            )
        self.max_connections = max(1, int(max_connections))
        self.max_message_bytes = int(max_message_bytes)
        self.reconnect_delay_seconds = float(reconnect_delay_seconds)
        self.receiving_app = receiving_app

        # ── vendor profile ──
        # "auto" is the honest default while the make is unknown: it detects per
        # message and reports what it detected, rather than silently asserting a
        # vendor. Naming the vendor explicitly is strictly better and is what
        # the pilot should run with once the site visit has happened.
        requested = (profile or "auto").strip().lower()
        self.profile_mode = "auto" if requested in ("auto", "detect", "") else "fixed"
        self.configured_profile: Optional[vendor_profiles.VendorProfile] = None
        if self.profile_mode == "fixed":
            if not vendor_profiles.is_known_profile(requested):
                # Loud, not fatal. A typo in `.env` must not cost a ward its
                # monitor feed; it degrades to LOINC-only, which is what Orb did
                # before profiles existed.
                logger.error(
                    "[%s] ORB_MONITOR_PROFILE=%r is not a known vendor profile "
                    "(known: %s). Falling back to 'generic' — LOINC only.",
                    source_id, requested,
                    ", ".join(sorted(vendor_profiles.PROFILES)),
                )
            self.configured_profile = vendor_profiles.profile_for(requested)
        self.strict_profile = bool(strict_profile)
        # PHI-free device vocabulary counters, surfaced in status().
        self.profile_detections: Counter = Counter()
        self.mapping_counts: Counter = Counter()
        self.unmapped_identifiers: Counter = Counter()

        self._queue: asyncio.Queue = asyncio.Queue(maxsize=max(1, int(queue_size)))
        self._worker_count = max(1, int(workers))
        self._workers: list[asyncio.Task] = []
        self._server: Optional[asyncio.AbstractServer] = None
        self._client_task: Optional[asyncio.Task] = None
        self._connections: set[asyncio.Task] = set()
        self._open_connections = 0
        # Reasons only — never payload bytes. The raw message (PHI) already has
        # exactly one home, `ingestion_events`, under its retention policy; a
        # second copy in a process-memory ring buffer read by an admin screen
        # would be an unaudited PHI store.
        self.recent_rejects: deque = deque(maxlen=20)
        self.unsupported_messages = 0

    # ════════════════════════════════════════
    #  Lifecycle
    # ════════════════════════════════════════

    @property
    def bound_port(self) -> Optional[int]:
        """The port actually in use (resolves `port=0` for tests/bench runs)."""
        if self._server is not None and self._server.sockets:
            try:
                return self._server.sockets[0].getsockname()[1]
            except Exception:
                return None
        return self.port

    async def start(self) -> None:
        if self._running:
            return
        self.stats.started_at = time.monotonic()
        self._workers = [
            asyncio.create_task(self._drain_queue(), name=f"{self.source_id}-worker-{i}")
            for i in range(self._worker_count)
        ]
        try:
            if self.mode == "listen":
                self._server = await asyncio.start_server(
                    self._on_connection, self.host, self.port,
                )
                bound = ", ".join(str(s.getsockname()) for s in (self._server.sockets or []))
                logger.info("[%s] MLLP listener up on %s (HL7 v2 ORU^R01)",
                            self.source_id, bound)
            else:
                self._client_task = asyncio.create_task(
                    self._connect_forever(), name=f"{self.source_id}-client")
                logger.info("[%s] MLLP client dialling %s:%s",
                            self.source_id, self.host, self.port)
        except Exception:
            # The workers were spawned BEFORE the socket (so they are draining
            # the moment the first message lands). If binding then fails —
            # "address already in use" is the everyday case on the appliance —
            # the registry logs it and Orb carries on, which would otherwise
            # leave these tasks running forever against a source that never
            # started. Reap them before letting the failure out.
            for task in self._workers:
                task.cancel()
            await asyncio.gather(*self._workers, return_exceptions=True)
            self._workers.clear()
            raise
        self._running = True

    async def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        if self._server is not None:
            self._server.close()
            try:
                await self._server.wait_closed()
            except Exception:
                pass
            self._server = None
        for task in [self._client_task, *self._connections, *self._workers]:
            if task is not None:
                task.cancel()
        pending = [t for t in [self._client_task, *self._connections, *self._workers] if t]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._client_task = None
        self._connections.clear()
        self._workers.clear()
        self.stats.connected = False
        forget = getattr(self.sink, "forget", None)
        if callable(forget):
            forget(self.source_id)
        logger.info("[%s] MLLP source stopped", self.source_id)

    # ════════════════════════════════════════
    #  Connection handling
    # ════════════════════════════════════════

    async def _on_connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """asyncio.start_server callback — one task per inbound connection."""
        if self._open_connections >= self.max_connections:
            logger.warning("[%s] refusing connection: %d already open (cap %d)",
                           self.source_id, self._open_connections, self.max_connections)
            self.stats.note_error(f"connection cap {self.max_connections} reached")
            try:
                writer.close()
            except Exception:
                pass
            return
        task = asyncio.current_task()
        if task is not None:
            self._connections.add(task)
        try:
            await self._handle_stream(reader, writer)
        finally:
            if task is not None:
                self._connections.discard(task)

    async def _connect_forever(self) -> None:
        """Outbound mode: dial the gateway and reconnect with backoff forever.

        A monitor gateway rebooting overnight must not need Orb restarted, so
        the only exit from this loop is cancellation at shutdown.
        """
        delay = self.reconnect_delay_seconds
        while True:
            try:
                reader, writer = await asyncio.open_connection(self.host, self.port)
                delay = self.reconnect_delay_seconds  # reset on a good connect
                await self._handle_stream(reader, writer)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.stats.note_error(f"connect failed: {e}")
                logger.warning("[%s] connect to %s:%s failed: %s",
                               self.source_id, self.host, self.port, e)
            # Capped exponential backoff: a gateway that is down for an hour
            # must not be hammered, but reconnect must still be quick enough
            # that a blip costs a ward seconds, not minutes.
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60.0)

    async def _handle_stream(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """Read MLLP frames off one connection until it closes. Never raises."""
        peer = "unknown"
        try:
            peer = str(writer.get_extra_info("peername"))
        except Exception:
            pass

        framer = MllpFramer(self.max_message_bytes)
        self._open_connections += 1
        self.stats.connections_opened += 1
        self.stats.connected = True
        logger.info("[%s] monitor connected: %s", self.source_id, peer)

        try:
            while True:
                chunk = await reader.read(8192)
                if not chunk:
                    break  # clean EOF — the device hung up
                for raw in framer.feed(chunk):
                    ack = self._accept(raw, peer)
                    if ack is not None:
                        try:
                            writer.write(frame(ack))
                            await writer.drain()
                        except (ConnectionError, OSError) as e:
                            # The device vanished between its message and our
                            # ACK. It will retransmit; dedup makes that a no-op.
                            logger.info("[%s] ACK to %s failed (%s); dropping connection",
                                        self.source_id, peer, e)
                            return
        except asyncio.CancelledError:
            raise
        except (ConnectionError, OSError) as e:
            logger.info("[%s] connection %s ended: %s", self.source_id, peer, e)
        except Exception as e:
            # Defensive: an unexpected type here would otherwise kill the
            # connection task silently and take the feed down with no signal.
            self.stats.note_error(f"stream error: {e}")
            logger.exception("[%s] unexpected error on connection %s", self.source_id, peer)
        finally:
            discarded = framer.stats
            if discarded.bytes_discarded_before_start or discarded.oversize_messages:
                logger.warning(
                    "[%s] connection %s framing losses: %s",
                    self.source_id, peer, discarded.as_dict(),
                )
            framer.reset()
            self._open_connections = max(0, self._open_connections - 1)
            self.stats.connections_closed += 1
            self.stats.connected = self._open_connections > 0
            try:
                writer.close()
            except Exception:
                pass
            logger.info("[%s] monitor disconnected: %s", self.source_id, peer)

    # ════════════════════════════════════════
    #  Message acceptance
    # ════════════════════════════════════════

    def _reject(self, message, code: str, reason: str, *, peer: str,
                control_id: str = "") -> str:
        self.stats.messages_rejected += 1
        self.stats.note_error(reason)
        self.recent_rejects.append({
            "at": utc_now_iso(), "peer": peer, "code": code,
            "reason": reason, "control_id": control_id,
        })
        return hl7v2.build_ack(message, code, reason, receiving_app=self.receiving_app,
                               control_id=control_id or None)

    def _accept(self, raw: bytes, peer: str) -> Optional[str]:
        """Validate one framed message and queue it. Returns the ACK to send.

        Runs on the connection task, so it must stay cheap and synchronous —
        all it does is parse (pure string work) and a non-blocking queue put.
        """
        self.stats.messages_received += 1

        text = raw.decode("utf-8", errors="replace")
        try:
            message = hl7v2.parse(text)
        except hl7v2.Hl7ParseError as e:
            self.stats.parse_failures += 1
            logger.warning("[%s] unparseable message from %s (%d bytes): %s",
                           self.source_id, peer, len(raw), e)
            # AR, not AE: retransmitting the identical unparseable bytes will
            # fail identically. Telling the sender to retry forever would turn
            # one bad message into a permanent flood.
            return self._reject(None, hl7v2.ACK_REJECT, f"unparseable HL7: {e}", peer=peer)

        control_id = message.control_id
        if message.message_type != "ORU":
            # Not a vitals result (ADT, ORM, …). Acknowledged so the sender's
            # queue drains, counted so a monitor sending us the wrong feed is
            # visible on the bench instead of looking like silence.
            self.unsupported_messages += 1
            logger.info("[%s] ignoring %s^%s from %s (this source handles ORU only)",
                        self.source_id, message.message_type or "?",
                        message.trigger_event or "?", peer)
            return hl7v2.build_ack(message, hl7v2.ACK_REJECT,
                                   "ORB monitor feed accepts ORU^R01 only",
                                   receiving_app=self.receiving_app)

        hints = self._extract_hints(message)
        if not hints:
            return self._reject(message, hl7v2.ACK_REJECT,
                                "no patient identifier in PID-3",
                                peer=peer, control_id=control_id)

        item = {
            "raw": text,
            "hints": hints,
            "profile": self._profile_for_message(message),
            "correlation_id": f"{self.source_id}-{uuid.uuid4()}",
            # MSH-10 is the sender's own message id. Threading it through gives
            # the ingestion dedup a sender-scoped key in addition to the payload
            # fingerprint, so a device that retransmits with a fresh timestamp
            # is still traceable to one logical observation.
            "external_message_id": control_id or None,
            "peer": peer,
        }
        try:
            self._queue.put_nowait(item)
        except asyncio.QueueFull:
            self.stats.overflow_rejections += 1
            logger.error(
                "[%s] ingest queue full (%d): NACKing %s from %s so it retries — "
                "the device is out-running persistence.",
                self.source_id, self._queue.maxsize, control_id or "<no id>", peer,
            )
            # AE = application error. In HL7 this is the sender's cue to retry,
            # which is precisely what we want: we did not take the message, and
            # the reading must not be lost just because we were busy.
            return self._reject(message, hl7v2.ACK_ERROR,
                                "receiver busy, retry", peer=peer, control_id=control_id)

        self.stats.messages_accepted += 1
        # ACK on ACCEPT, not on commit. HL7 senders time out in seconds; holding
        # the ACK across a database write would make every DB hiccup look like a
        # dead receiver and stop the monitor sending. Delivery is still
        # at-least-once end-to-end: the payload fingerprint makes a retransmit
        # after a crash idempotent (ingestion_service dedup).
        return hl7v2.build_ack(message, hl7v2.ACK_ACCEPT, receiving_app=self.receiving_app)

    def _profile_for_message(self, message: hl7v2.Hl7Message):
        """Which vendor table reads THIS message.

        A configured profile always wins — an operator who has been to the site
        and named the vendor must not be second-guessed by a string match. In
        "auto" mode the guess is made per message (a shared gateway can carry
        two makes) and every outcome is counted, so the admin surface can show
        "we are guessing, and here is what we guessed" rather than asserting a
        make nobody confirmed.
        """
        if self.configured_profile is not None:
            self.profile_detections[f"configured:{self.configured_profile.key}"] += 1
            return self.configured_profile
        try:
            detection = vendor_profiles.detect_from_message(message)
        except Exception:
            logger.debug("[%s] profile detection failed", self.source_id, exc_info=True)
            return vendor_profiles.COMBINED
        label = detection.profile.key if detection.named_vendor else f"guess:{detection.profile.key}"
        self.profile_detections[label] += 1
        return detection.profile

    def _note_mapping(self, kind: str, detail: str) -> None:
        """Observer for the profile adapter. PHI-free by construction: `detail`
        is an OBX identifier and display name, never a value or a patient."""
        self.mapping_counts[kind] += 1
        if kind != "unmapped":
            return
        # Count a known identifier always; admit a NEW one only while there is
        # room, so the list stays bounded without losing the counts it holds.
        if detail in self.unmapped_identifiers or len(self.unmapped_identifiers) < MAX_UNMAPPED_IDENTIFIERS:
            self.unmapped_identifiers[detail] += 1

    def _device_identity(self, message: hl7v2.Hl7Message) -> str:
        """A PHI-free name for the clock that stamped this message.

        MSH-3/MSH-4 (sending application / facility) is what a monitor or its
        gateway calls itself, and it is the natural key for "whose clock is
        wrong" — one device, one clock, many beds. Deliberately NOT the patient
        id: the skew report is rendered on an admin screen and shipped in the
        heartbeat, and a patient identifier there would be PHI.
        """
        app = (message.sending_application or "").strip()
        facility = (message.sending_facility or "").strip()
        name = "^".join(part for part in (app, facility) if part)
        return name or self.source_id

    def _extract_hints(self, message: hl7v2.Hl7Message) -> list[ReadingHint]:
        """Pull (patient, observed_at, device) out of a parsed ORU.

        Cheap by design: the authoritative parse into observations happens once,
        downstream, in the shared ingestion adapter. All we need here is enough
        to order the stream and to judge the device's clock.

        Timestamps are resolved against THIS SOURCE'S configured zone, so a
        gateway that emits offset-less local time is read in its own zone rather
        than assumed to be at Greenwich.
        """
        pid = message.first("PID")
        if pid is None:
            return []
        # PID-3 is the identifier LIST: repetitions, each `id^^^authority^type`.
        # A monitor commonly sends both the hospital MRN and its own bed-local
        # id; we take the first, which is what the ingestion adapter resolves.
        patient = pid.field(3, component=1) or pid.field(2, component=1)
        if not patient:
            return []

        # Most specific source of truth first. OBX-14 and OBR-7 are both
        # optional, so we fall down the chain rather than lose the date — an
        # undateable reading is treated as live, which is safe but blind.
        best: Optional[hl7v2.Hl7Timestamp] = None
        for candidates in (
            [obx.field(14) for obx in message.all("OBX")],     # per-observation
            [obr.field(7) for obr in message.all("OBR")],      # per-panel
            [message.message_datetime],                        # MSH-7
        ):
            for raw in candidates:
                parsed = hl7v2.parse_timestamp_detail(raw, default_tz=self.device_timezone)
                if parsed and (best is None or parsed.dt > best.dt):
                    best = parsed
            if best is not None:
                break

        return [ReadingHint(
            patient_identifier=patient,
            observed_at=best.iso if best else None,
            device_id=self._device_identity(message),
            device_time_had_offset=best.had_offset if best else None,
        )]

    # ════════════════════════════════════════
    #  Persistence workers
    # ════════════════════════════════════════

    async def _drain_queue(self) -> None:
        """Move accepted messages into the ingestion pipeline, forever."""
        while True:
            item = await self._queue.get()
            try:
                result = await self.sink.submit_payload(
                    item["raw"],
                    source=self,
                    correlation_id=item["correlation_id"],
                    external_message_id=item["external_message_id"],
                    # Same zone the hints were resolved in. If the adapter used
                    # a different assumption, the reading STORED in `vitals` and
                    # the reading we ORDERED and alerted on would disagree about
                    # when it happened — which is the original defect, moved.
                    #
                    # The vendor profile was resolved on the connection task
                    # (where the message was already parsed), so no message is
                    # parsed twice to decide which table reads it.
                    adapter=VendorProfileAdapter(
                        item.get("profile"),
                        default_timezone=self.device_timezone,
                        observer=self._note_mapping,
                        strict=self.strict_profile,
                    ),
                    hints=item["hints"],
                )
                self.stats.readings_ingested += result.inserted_vitals
                self.stats.duplicates += result.duplicates
                if result.backfill:
                    self.stats.backfilled += 1
                if result.inserted_vitals or result.inserted_observations:
                    self.stats.last_reading_at = utc_now_iso()
                if result.errors:
                    self.stats.note_error("; ".join(result.errors)[:300])
                    logger.warning("[%s] ingest reported errors for %s: %s",
                                   self.source_id, item["external_message_id"], result.errors)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                # THE loop-safety line for this workstream: a device message
                # that blows up persistence is counted and logged here and goes
                # no further. It can never unwind into the socket handler or the
                # event loop.
                self.stats.parse_failures += 1
                self.stats.note_error(f"ingest failed: {e}")
                logger.exception("[%s] ingest failed for message from %s",
                                 self.source_id, item.get("peer"))
            finally:
                self._queue.task_done()

    # ════════════════════════════════════════
    #  Observability
    # ════════════════════════════════════════

    @property
    def assumed_timezone(self) -> str:
        """The zone this source reads an offset-less DTM in. Never a mystery."""
        zone = self.device_timezone or hl7v2.appliance_timezone()
        return str(getattr(zone, "key", None) or zone)

    @property
    def profile_name(self) -> str:
        if self.configured_profile is not None:
            return self.configured_profile.key
        return "auto"

    def profile_status(self) -> dict:
        """What this feed is translating with, and how well it is going.

        `verified_against_device` is hard-coded False and there is no code path
        that sets it True. Until a captured message or a vendor point-mapping
        document exists, an operator reading this screen must not be able to
        mistake "the table loaded" for "the mapping is right".
        """
        detected = dict(self.profile_detections)
        unmapped = self.unmapped_identifiers.most_common(20)
        body = {
            "mode": self.profile_mode,
            "configured": self.profile_name,
            "strict": self.strict_profile,
            "available": sorted(vendor_profiles.PROFILES),
            "detected": detected,
            "mapping_counts": dict(self.mapping_counts),
            "unmapped_identifiers": [{"identifier": k, "count": n} for k, n in unmapped],
            "unmapped_distinct": len(self.unmapped_identifiers),
            "verified_against_device": False,
            "verification_note": (
                "Vendor identifier tables are unconfirmed hypotheses (see "
                "services/vitals_source/vendor_profiles.py). A clean bench run proves the "
                "pipeline, not the mapping. Confirm with a captured message from the "
                "hospital's own monitor."
            ),
        }
        if unmapped:
            body["action"] = (
                f"{len(self.unmapped_identifiers)} observation identifier(s) from this device "
                "could not be mapped to a vital. Send this list to the vendor or the biomedical "
                "engineer. It is exactly what the bench test exists to produce."
            )
        return body

    def describe(self) -> dict:
        return {
            **super().describe(),
            "protocol": "hl7v2-mllp",
            "mode": self.mode,
            "endpoint": f"{self.host}:{self.port}",
            "vendor_profile": self.profile_name,
            # Stated out loud on the admin surface: an operator must be able to
            # see what Orb thinks this device's clock zone is without reading
            # the source, because getting it wrong is a 5.5-hour error here.
            "assumed_timezone": self.assumed_timezone,
            "device_timezone_configured": self.device_timezone is not None,
        }

    def status(self) -> dict:
        base = super().status()
        base["queue_depth"] = self._queue.qsize()
        base["queue_capacity"] = self._queue.maxsize
        base["open_connections"] = self._open_connections
        base["unsupported_messages"] = self.unsupported_messages
        base["recent_rejects"] = list(self.recent_rejects)
        base["profile"] = self.profile_status()
        # The clock fault, made visible. A monitor whose clock is wrong is an
        # operational problem the ward has to fix; it is NOT a reason to stop
        # alerting on the patient, so it surfaces here rather than silently
        # changing what gets scored.
        report = getattr(self.sink, "clock_skew_report", None)
        base["clock_skew"] = (report(self.source_id) if callable(report)
                              else {"state": "unknown"})
        return base
