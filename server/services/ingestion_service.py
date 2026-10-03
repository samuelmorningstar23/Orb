"""Orb Hospital OS — Ingestion Service

Core orchestration layer for data ingestion. Handles:
  1. Raw event capture (provenance)
  2. Adapter selection & parsing
  3. Deduplication via fingerprint
  4. Routing parsed events into vitals / observations tables
  5. MEWS recomputation on vital ingestion
  6. Socket.IO emission for live frontend updates
  7. Alert checking via shared Vigil logic
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger("orb.ingestion")

from database import db_session, run_db
from services.ingestion_adapters import (
    CanonicalEvent,
    IngestionAdapter,
    ObservationType,
    ParseError,
    compute_fingerprint,
    select_adapter,
    HL7Adapter,
    JSONAdapter,
)
from services.vigil_service import news2_reason_line, score_vitals_row
from services.bot_service import VIGIL_BOT_ID
from services.vital_charting import as_charted, vitals_line
from services.vitals_carry import carry_forward, dump_carried
from services.news2_text import news2_description
from services.audit_service import log_audit_event
from services.clinical_time import device_naive_tz, to_utc_iso, utc_iso_now


# ════════════════════════════════════════════
#  Ingestion Result
# ════════════════════════════════════════════

@dataclass
class IngestionResult:
    """Summary returned to the caller after processing a payload."""
    event_id: int = 0                    # ingestion_events.id
    correlation_id: Optional[str] = None
    parse_status: str = "received"
    received: int = 0                    # canonical events parsed
    inserted_vitals: int = 0
    inserted_observations: int = 0
    duplicates: int = 0
    errors: list[str] = None             # type: ignore
    backfill: bool = False               # stored, but not treated as current state

    def __post_init__(self):
        if self.errors is None:
            self.errors = []

    def to_dict(self) -> dict:
        return {
            "event_id": self.event_id,
            "correlation_id": self.correlation_id,
            "parse_status": self.parse_status,
            "received": self.received,
            "inserted_vitals": self.inserted_vitals,
            "inserted_observations": self.inserted_observations,
            "duplicates": self.duplicates,
            "backfill": self.backfill,
            "errors": self.errors,
        }


# ════════════════════════════════════════════
#  Source Label Mapping
# ════════════════════════════════════════════

_SOURCE_LABELS = {
    "hl7": "ingested_hl7",
    "json": "ingested_json",
    "csv_replay": "csv_replay",
}

# A caller that KNOWS the origin of a payload (services/vitals_source/**: a
# bedside monitor, a nurse's manual entry) passes `source_label=` to
# ingest_payload and overrides the generic feed label above. That string is what
# lands in vitals.source, and it is the only thing that lets the shadow
# scorecard tell a machine reading from a typed one months later. See
# services/vitals_source/base.py:classify_source_label — every label written
# here must be classifiable there.


# ════════════════════════════════════════════
#  Patient Resolution
# ════════════════════════════════════════════

def _resolve_patient_id(db, raw_identifier: str) -> Optional[str]:
    """Try to match a raw patient identifier to an existing patient.

    Checks, in order:
      1. Exact match on patients.id
      2. Exact match on patients.mrn
      3. Case-insensitive match on patients.mrn
    """
    # Direct ID match
    row = db.execute("SELECT id FROM patients WHERE id = ?", (raw_identifier,)).fetchone()
    if row:
        return row["id"]

    # MRN match
    row = db.execute("SELECT id FROM patients WHERE mrn = ?", (raw_identifier,)).fetchone()
    if row:
        return row["id"]

    # Case-insensitive MRN
    row = db.execute(
        "SELECT id FROM patients WHERE LOWER(mrn) = LOWER(?)", (raw_identifier,)
    ).fetchone()
    if row:
        return row["id"]

    return None


# ════════════════════════════════════════════
#  Blocking DB Steps (run via run_db)
# ════════════════════════════════════════════
# Every function below opens its own db_session and MUST stay pure-sync: it is
# handed to database.run_db() and executes on the shared off-loop DB pool.
#
# They live at module scope rather than nested inside their async callers on
# purpose. Nested, they read as though the `with db_session()` runs on the event
# loop — which is the exact defect this file was audited for (the 2026-07-30
# wedge, where synchronous SQLCipher I/O on the uvloop thread stalled the API for
# 15+ minutes) — and every static scan for that defect flags them. At module
# scope the offload is visible at the call site and nowhere else.
#
# Each one also holds a WHOLE read+write sequence, not a fragment of one. That is
# deliberate: splitting a read and its dependent write into two run_db calls puts
# an await between them, and a concurrent ingestion can then change the row in
# the gap. Keeping the pair inside one executor call keeps it atomic AND leaves
# the loop free — do not "simplify" one of these into separate awaits.

def _find_duplicate_event(fingerprint):
    with db_session() as db:
        existing = db.execute(
            "SELECT id FROM ingestion_events WHERE fingerprint = ? AND parse_status != 'failed'",
            (fingerprint,),
        ).fetchone()
        return existing


def _persist_vitals_row(patient_raw_id, vital_events, event_id, source_label):
    # The carry-forward SELECT and the INSERT below are one read-modify-write:
    # the new row inherits whatever the patient's latest row holds for vitals the
    # feed omitted. They stay in a single db_session (and so a single run_db
    # call) because an await between them would let a concurrent ingestion for
    # the same patient land first, and this row would then carry forward values
    # it has already superseded — silently back-dating a vital that NEWS2 is
    # about to be scored on.
    with db_session() as db:
        # Resolve patient
        patient_id = _resolve_patient_id(db, patient_raw_id)
        if not patient_id:
            raise ValueError(f"Patient not found for identifier: {patient_raw_id}")

        # Collect vital values
        vitals_map: dict[str, Optional[float]] = {
            "HR": None, "SBP": None, "DBP": None, "SpO2": None, "Temp": None, "RR": None,
        }
        observed_at = None

        for evt in vital_events:
            if evt.mapped_vital_key and evt.mapped_vital_key in vitals_map:
                vitals_map[evt.mapped_vital_key] = evt.value_numeric
                if evt.observed_at and not observed_at:
                    observed_at = evt.observed_at

        # Stored as the UTC instant the sender named, in the one spelling every
        # vitals writer uses (services/clinical_time.py). The adapters hand over
        # the sender's string verbatim ('Z', '+05:30', or no offset), and text
        # ORDER BY across those spellings put a two-hour-old '+05:30' reading
        # above a five-minute-old 'Z' one. No offset means the device's wall
        # clock, the appliance zone, the rule the sink's ordering guard and the
        # HL7 parser apply. An undateable time is stored at receipt, as the sink
        # already treats it as live; the sender's string stays in the raw event.
        if observed_at:
            try:
                observed_at = to_utc_iso(observed_at, naive_tz=device_naive_tz())
            except ValueError:
                logger.warning("Undateable observed_at %r for %s: stored at receipt time",
                               observed_at, patient_raw_id)
                observed_at = None
        if not observed_at:
            observed_at = utc_iso_now()

        # If we have no actual numeric values, skip (None → async caller returns 0)
        if all(v is None for v in vitals_map.values()):
            return None

        # Fill missing vitals from the patient's most recent row (carry-forward).
        #
        # `id DESC` breaks a timestamp tie, matching vigil_service.get_patient_risk
        # — two readings CAN share an observation time (a corrected set re-charted
        # at the same bedside time, two monitor messages in the same second), and
        # without it "latest" was whichever row SQLite happened to return. Here
        # that decides which values get CARRIED FORWARD onto the new row, so a tie
        # resolved the wrong way copies a superseded observation into the present.
        latest = db.execute(
            "SELECT * FROM vitals WHERE patient_id = ? ORDER BY timestamp DESC, id DESC LIMIT 1",
            (patient_id,),
        ).fetchone()

        # ── Carry forward the two NEWS2 parameters no vital feed carries:
        #    ACVPU consciousness and supplemental O2. Both are HUMAN assessments;
        #    a bedside monitor has no opinion about either. Without the
        #    carry-forward the LIVE ingestion risk path would score a 5-parameter
        #    subset and silently drop the two biggest single escalators on the
        #    RCP table (ACVPU +3, supplemental O2 +2).
        #
        #    None here means NOT KNOWN — no prior row ever recorded it. That is
        #    distinct from a charted 'alert' / 'on air', and compute_news2 now
        #    reports the difference instead of scoring the unknown as normal and
        #    calling the observation set complete. ──
        #
        #    AGE-LIMITED (ruled 2026-10-03, services/vitals_carry.py): a value is
        #    carried only while its MEASUREMENT is younger than its limit (4 h,
        #    temperature 8 h, ACVPU and O2 12 h), and the row records when each
        #    carried value was measured so a stream of HR-only messages cannot
        #    keep re-stamping a 02:00 blood pressure as current. Past the limit
        #    the value is None: not measured, scored 0, NEWS2 marked incomplete.
        consciousness = None
        on_oxygen = None
        carried: dict[str, str] = {}
        if latest:
            columns = {"HR": "heart_rate", "SBP": "systolic_bp", "DBP": "diastolic_bp",
                       "SpO2": "spo2", "Temp": "temperature", "RR": "respiratory_rate"}
            wanted = [col for key, col in columns.items() if vitals_map[key] is None]
            fill, carried = carry_forward(
                latest, observed_at, wanted + ["consciousness", "on_oxygen"])
            for key, col in columns.items():
                if vitals_map[key] is None:
                    vitals_map[key] = fill.get(col)
            consciousness = fill["consciousness"]
            if fill["on_oxygen"] is not None:
                on_oxygen = bool(fill["on_oxygen"])

        # SpO2 SCALE. A COPD / chronic type-2 respiratory failure patient is
        # scored on the Scale 2 table (target 88-92%). Read here, on the session
        # that is already open, because the ingestion lane scored EVERY patient
        # on Scale 1 — so a COPD patient sitting at their normal 90% collected a
        # 3-point single-parameter escalation on every ingested row.
        prow = db.execute(
            "SELECT on_scale_2 FROM patients WHERE id = ?", (patient_id,)
        ).fetchone()
        on_scale_2 = bool(prow["on_scale_2"]) if prow and prow["on_scale_2"] is not None else False

        # Insert.
        #
        # on_oxygen IS WRITTEN (fixed 2026-08-14). It used to be omitted from the
        # column list, so every ingested row took the schema default of 0 — and
        # since the block above reads the LATEST row to score the CURRENT event,
        # an oxygen-dependent patient lost the +2 supplemental-oxygen point on
        # the very next ingested row and every row after it. The value decayed to
        # zero the moment a monitor spoke. It was invisible on the dev box only
        # because ORB_INGESTION_MODE=simulation routes through bot_service, which
        # does write the column; the pilot runs THIS lane, and the population it
        # under-scores is the one most likely to deteriorate.
        db.execute(
            """INSERT INTO vitals
               (patient_id, timestamp, heart_rate, systolic_bp, diastolic_bp,
                spo2, temperature, respiratory_rate, consciousness, on_oxygen,
                source, source_event_id, carried_from)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                patient_id,
                observed_at,
                vitals_map["HR"],
                vitals_map["SBP"],
                vitals_map["DBP"],
                vitals_map["SpO2"],
                vitals_map["Temp"],
                vitals_map["RR"],
                # NULL means NOT ASSESSED, and it is stored as NULL rather than
                # coerced to 'alert'/0 so the carry-forward above keeps saying
                # "nobody has ever charted this" on every subsequent row instead
                # of manufacturing a documented-normal finding on row two. Both
                # columns are nullable, every reader in the codebase already
                # coerces NULL to alert/False for SCORING (unchanged), and the
                # display surfaces already type them nullable and render '—'.
                consciousness,
                None if on_oxygen is None else (1 if on_oxygen else 0),
                source_label,
                event_id,
                dump_carried(carried),
            ),
        )
        return patient_id, vitals_map, observed_at, consciousness, on_oxygen, on_scale_2


def _insert_observations_batch(events, event_id, source_label, correlation_id):
    """Insert a batch of non-vital observations in one trip to the DB pool.

    Deliberately NOT one shared transaction. Each observation keeps its own
    db_session and its own try/except, so a single malformed OBX still fails
    alone and the rest of the panel lands — the same fail-soft behaviour the
    per-item await loop had. The only thing that changed is the number of
    executor hops. Returns (inserted_count, errors) rather than raising, because
    the caller reports partial success.
    """
    inserted = 0
    errors: list[str] = []
    for evt in events:
        try:
            _insert_observation(evt, event_id, source_label, correlation_id)
            inserted += 1
        except Exception as e:
            errors.append(f"Observation insert error: {e}")
    return inserted, errors


def _persist_risk_score(patient_id, risk):
    # Stamped explicitly with WRITE time (ruling 2026-10-03, R4: not the
    # device's observed time, so a slow device clock cannot sink a fresh score
    # below an older nurse set). The column's CURRENT_TIMESTAMP default wrote
    # "YYYY-MM-DD HH:MM:SS", and the nurse lane's ISO 'T' rows outranked it on
    # every text ORDER BY for the rest of the UTC day.
    with db_session() as db:
        cursor = db.execute(
            """INSERT INTO risk_scores (patient_id, timestamp, score, level, drivers)
               VALUES (?, ?, ?, ?, ?)""",
            (patient_id, utc_iso_now(), risk["score"], risk["level"],
             json.dumps(risk["drivers"])),
        )
        risk_score_id = cursor.lastrowid
        return risk_score_id


def _persist_relay_alert(patient_id, alert_content, episode_id=None, level=None,
                         correlation_id=None):
    # Phase 1 of the alert write — all DB work in a short transaction, NO awaits
    # inside. The INSERTs open sqlite's implicit write transaction; awaiting a
    # socket emit before commit parks the WAL write lock across a suspension
    # point, and any event-loop stall then starves every other writer
    # ("database is locked" storms). Emits happen after commit, in the caller,
    # mirroring nervous_system_triggers.on_scribe_note_signed.
    relay_alert_logged = False
    relay_msg_id = None
    relay_channel_id = None
    msg_data = None
    patient_name = "Unknown"
    with db_session() as db:
        db.execute(
            "INSERT OR IGNORE INTO users (id, name, role, password_hash) VALUES (?, ?, ?, ?)",
            (VIGIL_BOT_ID, 'Orb Vigil', 'doctor', 'system'),
        )
        channel = db.execute(
            "SELECT id FROM channels WHERE type = 'case_room' AND patient_id = ?",
            (patient_id,),
        ).fetchone()

        if channel:
            relay_channel_id = channel["id"]
            cursor = db.execute(
                """INSERT INTO messages
                   (channel_id, sender_id, sender_name, sender_role, content, message_type, is_bot, source)
                   VALUES (?, ?, ?, ?, ?, 'alert', 1, 'bot')""",
                (relay_channel_id, VIGIL_BOT_ID, 'Orb Vigil', 'intelligence', alert_content),
            )
            relay_msg_id = cursor.lastrowid
            row = db.execute("SELECT * FROM messages WHERE id = ?", (relay_msg_id,)).fetchone()
            if row:
                # The serializer the history endpoint and send_message share, so
                # the live alert carries `is_bot` (Relay's AUTO chip) and
                # `source`. A hand-built copy left both out, and every live Vigil
                # alert read as typed by a person until a reload (2026-10-03).
                from services.relay_service import _message_row_to_dict
                msg_data = _message_row_to_dict(row)

            patient = db.execute(
                "SELECT name, bed FROM patients WHERE id = ?", (patient_id,)
            ).fetchone()
            patient_name = patient["name"] if patient else "Unknown"
            relay_alert_logged = True

        # SHADOW on THIS transaction (db=db). A fresh connection from inside this
        # open write transaction would block on the WAL lock this function holds
        # — the same self-deadlock documented on _insert_observation's audit call.
        if episode_id:
            from services import shadow_service
            if relay_alert_logged:
                shadow_service.emit(
                    "alert.persisted", episode_id, "ingestion",
                    patient_id=patient_id, subject_type="messages", subject_id=relay_msg_id,
                    payload={"message_id": relay_msg_id, "channel_id": relay_channel_id},
                    source_correlation_id=correlation_id, db=db,
                )
            else:
                # NEGATIVE: NEWS2 fired, nobody was reachable. Invisible today.
                shadow_service.emit(
                    "alert.dropped", episode_id, "ingestion",
                    patient_id=patient_id,
                    payload={"reason": "no_case_room", "level": level},
                    source_correlation_id=correlation_id, db=db,
                )
        return relay_alert_logged, relay_msg_id, relay_channel_id, msg_data, patient_name


# ════════════════════════════════════════════
#  Core Ingestion Pipeline
# ════════════════════════════════════════════

async def ingest_payload(
    raw_payload: str,
    adapter: Optional[IngestionAdapter] = None,
    source_name: Optional[str] = None,
    content_type: Optional[str] = None,
    correlation_id: Optional[str] = None,
    external_message_id: Optional[str] = None,
    sio=None,
    source_label: Optional[str] = None,
    backfill: bool = False,
) -> IngestionResult:
    """Full ingestion pipeline: capture → parse → deduplicate → route → emit.

    Args:
        raw_payload:          The raw incoming payload string.
        adapter:              Pre-selected adapter (optional; auto-detected if None).
        source_name:          Human-readable origin label.
        content_type:         HTTP Content-Type header for adapter selection hints.
        correlation_id:       Caller-provided trace ID.
        external_message_id:  Sender-provided dedup key.
        sio:                  Socket.IO server instance for real-time emission.
        source_label:         Explicit provenance for vitals.source (see
                              _SOURCE_LABELS). Defaults to the adapter's generic
                              feed label.
        backfill:             The payload is known to be OLDER than data already
                              held for these patients (a monitor replaying its
                              buffer after a reconnect, a late retransmit). The
                              readings are still persisted in full — dropping a
                              real reading is never acceptable — but they do NOT
                              recompute the live risk score, do NOT raise a Vigil
                              alert and are NOT emitted to the live board, because
                              a stale value must not present itself as current.

    Returns:
        IngestionResult with counts and any errors.
    """
    result = IngestionResult(correlation_id=correlation_id, backfill=backfill)

    # ── Step 1: Select adapter ──
    if adapter is None:
        adapter = select_adapter(raw_payload, content_type)

    # log_audit_event is fully synchronous DB work — and the heaviest kind: it
    # takes BEGIN IMMEDIATE (the write lock) before reading the hash-chain head,
    # so it serialises against every other writer in the process for the whole
    # SELECT + INSERT. Called bare from an async def it ran that on the event
    # loop, which is the same defect as a bare db_session and, on the encrypted
    # DB, several times more expensive. Every audit call inside an `async def` in
    # this module therefore goes through run_db. The one in _insert_observation
    # stays bare: that function is already executing in a DB worker.
    await run_db(
        log_audit_event,
        correlation_id=correlation_id,
        event_category="ingestion",
        action_type="ingestion_request_received",
        outcome="success",
        source_type=adapter.source_type if adapter else "unknown",
        metadata={"source_name": source_name, "external_message_id": external_message_id}
    )

    if adapter is None:
        result.parse_status = "failed"
        result.errors.append("No adapter could handle this payload format")
        # Still record the raw event
        await run_db(
            _record_raw_event,
            raw_payload=raw_payload,
            source_type="unknown",
            source_name=source_name,
            correlation_id=correlation_id,
            external_message_id=external_message_id,
            parse_status="failed",
            parse_error="No suitable adapter found",
            adapter_name=None,
        )
        return result

    # ── Step 2: Compute fingerprint for dedup ──
    fingerprint = compute_fingerprint(raw_payload)

    # ── Step 3: Check deduplication ──
    existing = await run_db(_find_duplicate_event, fingerprint)

    if existing:
        result.parse_status = "duplicate"
        result.duplicates = 1
        
        await run_db(
            log_audit_event,
            correlation_id=correlation_id,
            event_category="ingestion",
            action_type="ingestion_duplicate_detected",
            outcome="success",
            source_type=adapter.source_type,
            summary_message=f"Duplicate of event {existing['id']}",
            metadata={"existing_event_id": existing["id"]}
        )
        
        # Record the duplicate event
        result.event_id = await run_db(
            _record_raw_event,
            raw_payload=raw_payload,
            source_type=adapter.source_type,
            source_name=source_name,
            correlation_id=correlation_id,
            external_message_id=external_message_id,
            fingerprint=fingerprint,
            parse_status="duplicate",
            parse_error=f"Duplicate of event {existing['id']}",
            adapter_name=adapter.adapter_name,
        )
        return result

    # ── Step 4: Record raw event ──
    event_id = await run_db(
        _record_raw_event,
        raw_payload=raw_payload,
        source_type=adapter.source_type,
        source_name=source_name,
        correlation_id=correlation_id,
        external_message_id=external_message_id,
        fingerprint=fingerprint,
        parse_status="received",
        adapter_name=adapter.adapter_name,
    )
    result.event_id = event_id

    # ── Step 5: Parse ──
    try:
        canonical_events = adapter.parse(
            raw_payload,
            source_name=source_name,
            correlation_id=correlation_id,
        )
    except ParseError as e:
        result.parse_status = "failed"
        result.errors.append(str(e))
        await run_db(_update_event_status, event_id, "failed", parse_error=str(e))
        await run_db(
            log_audit_event,
            correlation_id=correlation_id,
            event_category="ingestion",
            action_type="ingestion_parse_failure",
            outcome="failure",
            source_type=adapter.source_type,
            source_event_id=event_id,
            summary_message=f"ParseError: {e}"
        )
        return result
    except Exception as e:
        result.parse_status = "failed"
        result.errors.append(f"Unexpected parse error: {e}")
        await run_db(_update_event_status, event_id, "failed", parse_error=str(e))
        await run_db(
            log_audit_event,
            correlation_id=correlation_id,
            event_category="ingestion",
            action_type="ingestion_parse_failure",
            outcome="failure",
            source_type=adapter.source_type,
            source_event_id=event_id,
            summary_message=f"Unexpected error: {e}"
        )
        return result

    result.received = len(canonical_events)

    # ── Step 6: Set provenance on all events ──
    for evt in canonical_events:
        evt.raw_event_id = event_id
        evt.source_type = adapter.source_type

    # ── Step 7: Route into tables ──
    vitals_count, obs_count, route_errors = await _route_events(
        canonical_events, event_id, adapter.source_type, sio, correlation_id,
        source_label=source_label, backfill=backfill,
    )

    result.inserted_vitals = vitals_count
    result.inserted_observations = obs_count
    result.errors.extend(route_errors)

    # ── Step 8: Update event status ──
    if route_errors and (vitals_count + obs_count) > 0:
        final_status = "partially_parsed"
        audit_action = "ingestion_parse_partial_success"
        audit_outcome = "partial_success"
    elif route_errors:
        final_status = "failed"
        audit_action = "ingestion_parse_failure"
        audit_outcome = "failure"
    else:
        final_status = "parsed"
        audit_action = "ingestion_parse_success"
        audit_outcome = "success"

    result.parse_status = final_status
    await run_db(
        _update_event_status,
        event_id, final_status,
        vitals_inserted=vitals_count,
        observations_inserted=obs_count,
        parse_error="; ".join(route_errors) if route_errors else None,
    )

    await run_db(
        log_audit_event,
        correlation_id=correlation_id,
        event_category="ingestion",
        action_type=audit_action,
        outcome=audit_outcome,
        source_type=adapter.source_type,
        source_event_id=event_id,
        summary_message=f"Parsed {result.received} events. Inserted {vitals_count} vitals, {obs_count} observations.",
        metadata={"route_errors": route_errors} if route_errors else None
    )

    return result


# ════════════════════════════════════════════
#  Raw Event Recording
# ════════════════════════════════════════════

def _record_raw_event(
    *,
    raw_payload: str,
    source_type: str,
    source_name: Optional[str] = None,
    correlation_id: Optional[str] = None,
    external_message_id: Optional[str] = None,
    fingerprint: Optional[str] = None,
    parse_status: str = "received",
    parse_error: Optional[str] = None,
    adapter_name: Optional[str] = None,
    patient_identifier_raw: Optional[str] = None,
) -> int:
    """Insert a row into ingestion_events and return its ID.

    DEFERRED (regulatory / cross-cluster — needs a scheduled job + schema policy):
    raw_payload stores the full inbound message (PID/PHI) capped at 50k chars,
    with no TTL or purge, even for failed/duplicate events. This is an indefinite
    second copy of PHI in the unencrypted DB and violates GDPR storage-limitation /
    data-minimization. Recommended approach: (1) add a retention/purge job that
    nulls raw_payload after successful processing or after N days, and (2) store
    only a redacted/hashed form for failed/duplicate events. Both require a
    background scheduler + schema/retention config owned outside this service.
    """
    with db_session() as db:
        cursor = db.execute(
            """INSERT INTO ingestion_events
               (source_type, source_name, external_message_id,
                patient_identifier_raw, raw_payload, payload_format,
                parse_status, parse_error, fingerprint, correlation_id, adapter_name)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                source_type,
                source_name,
                external_message_id,
                patient_identifier_raw,
                raw_payload[:50000],  # cap raw payload size
                adapter_name,
                parse_status,
                parse_error,
                fingerprint,
                correlation_id,
                adapter_name,
            ),
        )
        return cursor.lastrowid


def _update_event_status(
    event_id: int,
    status: str,
    *,
    parse_error: Optional[str] = None,
    vitals_inserted: int = 0,
    observations_inserted: int = 0,
):
    """Update parse status on an existing ingestion_events row."""
    with db_session() as db:
        db.execute(
            """UPDATE ingestion_events
               SET parse_status = ?,
                   parse_error = ?,
                   vitals_inserted = ?,
                   observations_inserted = ?,
                   processed_at = CURRENT_TIMESTAMP
               WHERE id = ?""",
            (status, parse_error, vitals_inserted, observations_inserted, event_id),
        )


# ════════════════════════════════════════════
#  Event Routing
# ════════════════════════════════════════════

async def _route_events(
    events: list[CanonicalEvent],
    event_id: int,
    source_type: str,
    sio=None,
    correlation_id: str = None,
    source_label: Optional[str] = None,
    backfill: bool = False,
) -> tuple[int, int, list[str]]:
    """Route canonical events into vitals / observations tables.

    Returns (vitals_inserted, observations_inserted, errors).
    """
    vitals_inserted = 0
    obs_inserted = 0
    errors: list[str] = []

    source_label = source_label or _SOURCE_LABELS.get(source_type, f"ingested_{source_type}")

    # Group vitals by patient for batch processing
    vitals_by_patient: dict[str, list[CanonicalEvent]] = {}
    non_vital_events: list[CanonicalEvent] = []

    for evt in events:
        if evt.mapped_vital_key and evt.observation_type == ObservationType.VITAL:
            vitals_by_patient.setdefault(evt.patient_identifier, []).append(evt)
        else:
            non_vital_events.append(evt)

    # ── Process vital signs ──
    for patient_raw_id, vital_events in vitals_by_patient.items():
        try:
            count = await _insert_vitals(
                patient_raw_id, vital_events, event_id, source_label, sio, correlation_id,
                backfill=backfill,
            )
            vitals_inserted += count
        except Exception as e:
            errors.append(f"Vitals insert error for {patient_raw_id}: {e}")

    # ── Process non-vital observations (labs, etc.) ──
    # ONE executor hop for the whole batch, not one per observation. A single HL7
    # lab panel routinely carries dozens of OBX segments, and each hop paid a
    # loop round-trip plus a fresh connection open — openDatabase/findReusableFd
    # contends on the same process-global unix-VFS mutex that produced the
    # 2026-07-30 stall, so the per-item version was both slower and a direct
    # contributor to it. Per-observation transactions and per-observation error
    # capture are preserved inside the batch (see _insert_observations_batch).
    if non_vital_events:
        batch_inserted, batch_errors = await run_db(
            _insert_observations_batch,
            non_vital_events, event_id, source_label, correlation_id,
        )
        obs_inserted += batch_inserted
        errors.extend(batch_errors)

    return vitals_inserted, obs_inserted, errors


async def _insert_vitals(
    patient_raw_id: str,
    vital_events: list[CanonicalEvent],
    event_id: int,
    source_label: str,
    sio=None,
    correlation_id: str = None,
    backfill: bool = False,
) -> int:
    """Map vital events into a single vitals table row and trigger downstream logic.

    The existing vitals table expects one row per timestamp with all vital fields.
    We collect the individual vital observations and merge them into one row.

    `backfill` marks a reading that is older than data we already hold (a monitor
    replaying its buffer after a reconnect). It is persisted identically — the
    history must be complete — but it skips the live-state effects. See the
    guard below for exactly why each one is skipped.
    """
    db_result = await run_db(
        _persist_vitals_row, patient_raw_id, vital_events, event_id, source_label
    )
    if db_result is None:
        return 0
    (patient_id, vitals_map, observed_at, consciousness, on_oxygen,
     on_scale_2) = db_result

    await run_db(
        log_audit_event,
        correlation_id=correlation_id,
        event_category="ingestion",
        action_type="vital_inserted",
        outcome="success",
        patient_id=patient_id,
        source_type=source_label,
        source_event_id=event_id,
        target_type="vitals",
        summary_message=f"Inserted vital signs row for patient {patient_id}"
    )

    # ── Downstream: NEWS2 + Socket.IO + Alerts ──
    hr = vitals_map.get("HR")
    sbp = vitals_map.get("SBP")
    rr = vitals_map.get("RR")
    temp = vitals_map.get("Temp")
    spo2 = vitals_map.get("SpO2")
    dbp = vitals_map.get("DBP")

    # ── Backfill stop line ──
    # A late/replayed reading is now safely in the vitals history (above) and
    # audited. Everything past this point publishes CURRENT patient state:
    #   * risk_scores rows are stamped CURRENT_TIMESTAMP, so scoring a
    #     30-minutes-stale reading would make it the patient's newest risk score
    #     and overwrite a fresher, worse one on every board that reads "latest";
    #   * a Vigil alert fired from it would page a nurse about a vital sign that
    #     has already been superseded;
    #   * the Socket.IO emit would paint the stale numbers onto the live tile.
    # None of that is a judgement call about the data — the reading is real and
    # kept. It just is not news.
    if backfill:
        logger.info(
            "Backfilled vitals row for patient %s (observed_at=%s, source=%s): "
            "persisted, but live risk/alert/emit skipped.",
            patient_id, observed_at, source_label,
        )
        await run_db(
            log_audit_event,
            correlation_id=correlation_id,
            event_category="ingestion",
            action_type="vital_backfilled",
            outcome="success",
            patient_id=patient_id,
            source_type=source_label,
            source_event_id=event_id,
            target_type="vitals",
            summary_message=(
                f"Out-of-order vital for patient {patient_id} observed at {observed_at} "
                f"stored without recomputing live risk"
            ),
        )
        return 1

    # Full 7-parameter NEWS2 (consciousness + supplemental O2 included), on the
    # patient's own SpO2 scale, so the ingestion risk path matches the displayed
    # score and never silently drops the ACVPU / on-oxygen escalators.
    #
    # Through the shared entry point (`score_vitals_row`) so the parameter
    # marshalling cannot drift from the chart and the ward board. `overlay=False`
    # is deliberate and is the ONE place the ingestion lane differs: the CfC
    # overlay is a display-time escalation over stored history, and this call
    # writes a `risk_scores` row — the persisted clinical record has to stay the
    # deterministic, auditable RCP aggregate. It also runs per ingested vitals
    # row in the live path, where a torch forward pass per message does not
    # belong. Every surface that DISPLAYS this patient re-scores through
    # score_vitals_row and gets the overlay there.
    risk = score_vitals_row(
        {
            "heart_rate": hr, "systolic_bp": sbp, "respiratory_rate": rr,
            "temperature": temp, "spo2": spo2,
            "consciousness": consciousness, "on_oxygen": on_oxygen,
        },
        on_scale_2=on_scale_2,
        overlay=False,
    )

    # Store risk score
    risk_score_id = await run_db(_persist_risk_score, patient_id, risk)
        
    await run_db(
        log_audit_event,
        correlation_id=correlation_id,
        event_category="ingestion",
        action_type="news2_recomputed",
        outcome="success",
        patient_id=patient_id,
        target_type="risk_scores",
        target_id=str(risk_score_id),
        source_type=source_label,
        source_event_id=event_id,
        summary_message=f"NEWS2 recomputed: {news2_description(risk)}"
    )

    # Emit Socket.IO update (identical shape to bot_service)
    payload = {
        "patient_id": patient_id,
        "timestamp": observed_at,
        "heart_rate": hr,
        "systolic_bp": sbp,
        "diastolic_bp": dbp,
        "spo2": spo2,
        "temperature": temp,
        "respiratory_rate": rr,
        # Coerced to bot_service's wire shape (str / bool, never null) so the
        # live-tile contract stays byte-compatible across the two vitals writers.
        # The honest "was this ever assessed" statement travels in
        # risk.missing_parameters / risk.coverage_note, which is the field the UI
        # must read before presenting this as a complete NEWS2.
        "consciousness": consciousness or "alert",
        "on_oxygen": bool(on_oxygen),
        "risk": risk,
        "source": source_label,
    }

    if sio:
        try:
            # Scoped to the patient's department room, not a global broadcast.
            from ws.realtime import emit_patient_vitals
            await emit_patient_vitals("vitals:update", payload)
        except Exception as e:
            logger.warning("Socket.IO emit error: %s", e)

    # The row this reading became, so the alert and the escalation chain can
    # name it (read here because _persist_vitals_row's return is unchanged).
    vitals_id = await run_db(_vitals_row_id, patient_id, event_id)

    # ── Vigil Alert Check ──
    await _check_ingested_alert(patient_id, payload, risk, sio, correlation_id,
                                vitals_id=vitals_id)

    # ── The ESCALATION CHAIN, from the feed (2026-10-03) ──
    # The nurse lane has called this post-commit since 2026-08-14; the feed did
    # not, so a device-fed deterioration reached the chain only when the
    # safety-net sweep next ran. Every live row, cooldown or not: the chain is
    # idempotent and records the observation on an open escalation instead of
    # re-raising it. notify=False for the nurse lane's reason: the Relay alert
    # above is this reading's message. Never raises; the reading is stored.
    try:
        from services import escalation_service
        await run_db(
            escalation_service.evaluate_observation,
            patient_id, risk, vitals_id=vitals_id, source=source_label,
            observed_at=observed_at, notify=False,
        )
    except Exception as e:
        logger.error("ingestion escalation failed for %s (reading %s is stored): %s",
                     patient_id, vitals_id, e)

    return 1


def _vitals_row_id(patient_id, event_id):
    """The vitals row an ingestion event wrote for this patient, or None."""
    with db_session() as db:
        row = db.execute(
            "SELECT id FROM vitals WHERE patient_id = ? AND source_event_id = ? "
            "ORDER BY id DESC LIMIT 1",
            (patient_id, event_id),
        ).fetchone()
        return row["id"] if row else None


async def _check_ingested_alert(
    patient_id: str,
    vitals_payload: dict,
    risk: dict,
    sio=None,
    correlation_id: str = None,
    vitals_id: int | None = None,
):
    """Check if ingested vitals should trigger a Relay alert.

    The NEWS2 result (risk_level) is the single source of truth for dispatch —
    any non-stable patient is alerted (mirrors bot_service.evaluate_and_dispatch_alerts).
    The reason line comes from `news2_reason_line` (shared by all three lanes)
    and is PURELY human-readable wording: it must never gate the alert,
    otherwise NEWS2-'elevated' patients whose trigger is consciousness / mild
    bradypnea / borderline hypotension would be silently dropped despite the
    displayed score. If it raises, the alert goes out with the band alone.
    """
    risk_level = risk.get("level", "stable")
    drivers = risk.get("drivers", [])

    if risk_level == "stable":
        return

    # ── SHADOW: detection time, stamped the instant NEWS2 is known to be above
    #    threshold — before any DB write. `correlation_id` is threaded into
    #    source_correlation_id: it is the ONE genuine correlation spine that
    #    exists today (HTTP ingest → NEWS2 → relay_alert_generated). The
    #    episode_id supersedes it as the join key, but recording it lets the
    #    shadow log be tied back to the ingestion audit trail.
    from services import bot_service, shadow_service
    fired_at = shadow_service.utc_now()
    raw_news2_shadow = risk.get("raw_news2", risk.get("raw_mews"))

    # ── THE COOLDOWN, SHARED WITH THE SIMULATOR (2026-10-03) ──
    # A real monitor feed sends a reading every few seconds and this lane had
    # no cooldown at all, so it paged on every one. Same rule and same dict as
    # bot_service (120 s, bypassed by a worse NEWS2 or a band escalation, never
    # faster than 30 s). Check and stamp with no await between them, so two
    # readings arriving together cannot both page.
    await bot_service.ensure_cooldowns_reconciled()
    suppressed, seconds_since_last = bot_service.alert_cooldown_check(
        patient_id, risk_level, raw_news2_shadow)
    repeat = False
    if not suppressed:
        repeat = bot_service.record_alert_dispatched(patient_id, risk_level, raw_news2_shadow)

    episode_id, _ = await shadow_service.aopen_episode(
        patient_id, "ingestion",
        occurred_at=fired_at, source_correlation_id=correlation_id,
        trigger="news2_threshold",
        payload={"level": risk_level, "raw_news2": raw_news2_shadow},
    )
    if suppressed:
        # A NEGATIVE, recorded as the simulator records it, so the suppression
        # rate of a real feed is a query and not a guess.
        if episode_id:
            await shadow_service.aemit(
                "alert.suppressed", episode_id, "ingestion",
                occurred_at=fired_at, patient_id=patient_id,
                source_correlation_id=correlation_id,
                payload={
                    "level": risk_level,
                    "raw_news2": raw_news2_shadow,
                    "suppressed_by": "cooldown",
                    "seconds_since_last": seconds_since_last,
                },
            )
        await run_db(
            log_audit_event,
            correlation_id=correlation_id,
            event_category="ingestion",
            action_type="vigil_alert_suppressed",
            outcome="success",
            patient_id=patient_id,
            summary_message=(
                f"Vigil alert held by the cooldown: {risk_level.upper()}, "
                f"{seconds_since_last}s since the last page"
            ),
        )
        return

    if episode_id:
        await shadow_service.aemit(
            "alert.fired", episode_id, "ingestion",
            occurred_at=fired_at, patient_id=patient_id,
            source_correlation_id=correlation_id,
            subject_type="vitals" if vitals_id else None, subject_id=vitals_id,
            payload={
                "level": risk_level,
                "raw_news2": raw_news2_shadow,
                "score": risk.get("score"),
                "drivers": drivers[:6],
                "single_param_3": bool(risk.get("single_param_3")),
                "spo2_scale": risk.get("spo2_scale"),
                "vital_id": vitals_id,
                "repeat": repeat,
            },
        )

    # AS CHARTED (services/vital_charting.py): the numbers NEWS2 scored and
    # its drivers print. A vital the feed did not carry stays None and prints
    # as not recorded. It used to be defaulted to a stand-in (SpO2 100, Temp
    # 37, RR 16, HR 0), which both printed a reading nobody took and, for HR
    # and SBP, read as "Bradycardia: HR 0 bpm" / "Hypotension: BP 0/0 mmHg".
    hr, sbp, dbp, spo2, rr = (as_charted(vitals_payload.get(k)) for k in (
        "heart_rate", "systolic_bp", "diastolic_bp", "spo2", "respiratory_rate"))
    temp = as_charted(vitals_payload.get("temperature"), 1)

    # The reason line IS the NEWS2 breakdown (ruled 2026-10-02, see
    # `news2_reason_line`). Wording only: every non-stable NEWS2 patient was
    # already dispatched above. This lane used to word it from its own cutoffs:
    # "Severe tachycardia" from HR 121, "SpO2 critically low" from 89 %, "SpO2
    # dropping" from one reading, Scale 2 ignored, and a 3-point SBP 85-90 or
    # RR 25 dropped whenever any other phrase fired.
    # Wording only, so it can never cost the alert (ruled 2026-10-03, G2): a
    # raise here used to drop the whole alert, and the
    # ingest reported a failed parse for a row it had written. No real trigger is
    # known; the fallback makes "wording never gates dispatch" true anyway.
    try:
        reason_text = news2_reason_line(risk)
    except Exception:
        logger.exception("reason line failed for %s; sending the band alone", patient_id)
        reason_text = f"NEWS2 {risk_level}"
    vitals_summary = vitals_line(hr=hr, sbp=sbp, dbp=dbp, spo2=spo2, temp=temp, rr=rr)
    source = vitals_payload.get("source", "ingested")

    # Report raw NEWS2 aggregate + band, not a fabricated probability percentage.
    raw_news2 = risk.get("raw_news2", risk.get("raw_mews"))
    # One label for every surface (news2_text). A monitor feed never carries
    # ACVPU or supplemental O2, so this lane is where "incomplete set,
    # minimum" is the normal case and most needs saying.
    news2_text = news2_description(risk) if raw_news2 is not None else f"NEWS2 {risk_level}"

    alert_content = (
        f"⚠️ VIGIL ALERT [{source.upper()}]: {reason_text}\n"
        f"Current vitals: {vitals_summary}\n"
        f"{news2_text}. Immediate review recommended."
    )

    # Post to Relay case room
    relay_alert_logged = False
    relay_msg_id = None
    relay_channel_id = None

    try:
        # Phase 1 — the whole alert write, off the loop and with no awaits inside
        # (see _persist_relay_alert). Phase 2 emits only after it has committed.
        # The episode rides along, so `alert.persisted` / `alert.dropped` land
        # on it (it was opened above and then never passed in).
        (relay_alert_logged, relay_msg_id, relay_channel_id,
         msg_data, patient_name) = await run_db(
            _persist_relay_alert, patient_id, alert_content,
            episode_id, risk_level, correlation_id)

        # Phase 2 — socket emits AFTER the transaction committed.
        if relay_alert_logged and sio:
            if msg_data:
                await sio.emit("message:new", msg_data, room=relay_channel_id)
            # Patient status update — scoped to the patient's department room
            # (nurses' station for that ward), not a global broadcast.
            from ws.realtime import emit_patient_vitals
            await emit_patient_vitals("patient_status_update", {
                "patient_id": patient_id,
                "name": patient_name,
                "status": "critical_alert",
                "timestamp": datetime.now(timezone.utc).isoformat()
            })

    except Exception as e:
        logger.error("Failed to generate alert message: %s", e)

    # ── THE NERVOUS SYSTEM AND SAGE'S DRAFT, from the feed (2026-10-03) ──
    # Through the one publisher the simulator and nurse lanes share, post-
    # commit, never raising. Until now a device-fed deterioration woke no
    # prewarm and got no drafted assessment.
    if relay_alert_logged:
        try:
            bot_service.publish_vigil_critical(
                patient_id,
                level=risk_level,
                score=risk.get("score"),
                drivers=drivers,
                message_id=relay_msg_id,
                raw_news2=raw_news2,
                lane="ingestion",
            )
        except Exception as e:
            logger.warning("ingestion alert: event bus publish failed for %s: %s", patient_id, e)

    if relay_alert_logged:
        await run_db(
            log_audit_event,
            correlation_id=correlation_id,
            event_category="ingestion",
            action_type="relay_alert_generated",
            outcome="success",
            patient_id=patient_id,
            target_type="message",
            # A string, as every other writer passes it: the hash is computed
            # over the int and verified over the stored TEXT, so an int here
            # broke the audit chain on every feed alert that reached a room.
            target_id=str(relay_msg_id) if relay_msg_id is not None else None,
            summary_message=f"Ingestion triggered Relay alert for patient {patient_id}",
            metadata={"reason": reason_text, "risk_level": risk_level, "channel_id": relay_channel_id}
        )

    await run_db(
        log_audit_event,
        correlation_id=correlation_id,
        event_category="ingestion",
        action_type="vigil_alert_generated",
        outcome="success",
        patient_id=patient_id,
        summary_message=f"Vigil alert checked and triggered: {risk_level.upper()}"
    )


def _insert_observation(
    evt: CanonicalEvent,
    event_id: int,
    source_label: str,
    correlation_id: str = None,
):
    """Insert a non-vital observation into the observations table."""
    with db_session() as db:
        # Try to resolve patient
        patient_id = _resolve_patient_id(db, evt.patient_identifier) if evt.patient_identifier else None

        if patient_id is None:
            # PHI-bearing observation that couldn't be linked to a known patient.
            # It is still persisted (with NULL patient_id) so nothing is dropped,
            # but it is now logged so unlinked clinical data is observable rather
            # than silently orphaned.
            # DEFERRED (cross-cluster — needs a new quarantine table/schema):
            # route unresolved-patient observations into an explicit review queue
            # instead of inserting NULL-patient rows that may never be reviewed.
            logger.warning(
                "Observation '%s' could not be resolved to a patient (raw id=%r); "
                "inserting unlinked — needs review queue.",
                evt.observation_name, evt.patient_identifier,
            )

        cursor = db.execute(
            """INSERT INTO observations
               (patient_id, encounter_id, source, source_event_id,
                observation_code, observation_code_system, observation_name,
                value_text, value_numeric, unit, reference_range, abnormal_flag,
                status, observed_at, panel_name, obr_group_id, raw_segment_ref)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                patient_id,
                evt.encounter_identifier,
                source_label,
                event_id,
                evt.observation_code,
                evt.observation_code_system,
                evt.observation_name,
                evt.value if evt.value_numeric is None else None,
                evt.value_numeric,
                evt.unit,
                evt.reference_range,
                evt.abnormal_flag,
                evt.status,
                evt.observed_at,
                evt.panel_name,
                evt.obr_group_id,
                evt.raw_segment_ref,
            ),
        )
        obs_id = cursor.lastrowid

    # Audit AFTER the transaction commits. The standalone audit path opens its
    # own connection and runs BEGIN IMMEDIATE, which would block on the WAL
    # write lock this function's own INSERT still holds — a same-thread
    # self-deadlock that can only end at busy_timeout (30s), after which the
    # fail-soft 'ingestion' write is silently dropped. Same rule as
    # _insert_vitals above.
    log_audit_event(
        correlation_id=correlation_id,
        event_category="ingestion",
        action_type="observation_inserted",
        outcome="success",
        patient_id=patient_id,
        encounter_id=evt.encounter_identifier,
        source_type=source_label,
        source_event_id=event_id,
        target_type="observations",
        target_id=str(obs_id),
        summary_message=f"Inserted observation: {evt.observation_name}"
    )
