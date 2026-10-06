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
import uuid
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
    # Results that matched no patient and went to the unmatched results queue
    # (ORB_RESULT_QUARANTINE, lis-19). Stored, so a success for the sender.
    quarantined: int = 0

    def __post_init__(self):
        if self.errors is None:
            self.errors = []

    def to_dict(self) -> dict:
        out = {
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
        # Only when the queue took something, so the answer is unchanged with
        # the queue switched off.
        if self.quarantined:
            out["quarantined"] = self.quarantined
        return out


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
            "SELECT * FROM vitals WHERE patient_id = ? AND voided_at IS NULL "
            "ORDER BY timestamp DESC, id DESC LIMIT 1",
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
    executor hops. Returns (inserted_count, errors, quarantined_count) rather
    than raising, because the caller reports partial success. A result the
    unmatched results queue took is counted as quarantined, not inserted.

    The whole message is known here, so a correction that shares its test,
    OBX-4 sub-id, sender and order with another result in the same message is
    marked before anything is written: it supersedes nothing (lis-20,
    supersede_corrected_in_txn), whichever of the two is written first.
    """
    inserted = 0
    quarantined = 0
    errors: list[str] = []
    correcting = _corrections_on()
    for evt in events:
        try:
            sibling = (correcting and is_external_correction(evt)
                       and message_has_sibling_result(evt, events))
            if _insert_observation(evt, event_id, source_label, correlation_id,
                                   sibling_in_message=sibling) == "quarantined":
                quarantined += 1
            else:
                inserted += 1
        except Exception as e:
            errors.append(f"Observation insert error: {e}")
    return inserted, errors, quarantined


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
    vitals_count, obs_count, route_errors, quarantined_count = await _route_events(
        canonical_events, event_id, adapter.source_type, sio, correlation_id,
        source_label=source_label, backfill=backfill,
    )

    result.inserted_vitals = vitals_count
    result.inserted_observations = obs_count
    result.quarantined = quarantined_count
    result.errors.extend(route_errors)

    # ── Step 8: Update event status ──
    # A result held in the unmatched results queue was stored, so it counts as
    # landed: the sender must not retry it, and a retry of a failed event is
    # not deduplicated.
    if route_errors and (vitals_count + obs_count + quarantined_count) > 0:
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
        summary_message=(
            f"Parsed {result.received} events. Inserted {vitals_count} vitals, {obs_count} observations."
            + (f" Held {quarantined_count} unmatched result(s) for review." if quarantined_count else "")
        ),
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
) -> tuple[int, int, list[str], int]:
    """Route canonical events into vitals / observations tables.

    Returns (vitals_inserted, observations_inserted, errors, quarantined): the
    last counts results the unmatched results queue took (lis-19).
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
    quarantined = 0
    if non_vital_events:
        batch_inserted, batch_errors, quarantined = await run_db(
            _insert_observations_batch,
            non_vital_events, event_id, source_label, correlation_id,
        )
        obs_inserted += batch_inserted
        errors.extend(batch_errors)

    return vitals_inserted, obs_inserted, errors, quarantined


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
    *,
    sibling_in_message: bool = False,
) -> str:
    """Insert a non-vital observation into the observations table.

    Returns 'inserted', or 'quarantined' when no patient matched and the
    unmatched results queue is on (ORB_RESULT_QUARANTINE, lis-19): the result
    then waits in result_quarantine for laboratory staff instead of being
    stored with no patient. With ORB_RESULT_CORRECTIONS on, an HL7 OBX-11 'C'
    supersedes the earlier final result it corrects on the same transaction
    (lis-20, see write_observation_in_txn).
    """
    # A correction reads the results it may supersede and then writes; take
    # the write lock first so nothing lands in between (database.db_session).
    correcting = _corrections_on() and is_external_correction(evt)
    quarantine_id = None
    obs_id = None
    with db_session(immediate=correcting) as db:
        # Try to resolve patient
        patient_id = _resolve_patient_id(db, evt.patient_identifier) if evt.patient_identifier else None

        if patient_id is None and _quarantine_on() and _queue_takes(evt, source_label):
            # Held for laboratory staff to attach to the right patient or
            # discard (services/result_quarantine_service.py). Never stored
            # with no patient, where no chart or review would ever see it.
            # Only results from a result feed: a bedside monitor's MAP, ST or
            # PVC count is a device metric and keeps the branch below.
            from services import result_quarantine_service
            quarantine_id = result_quarantine_service.enqueue_in_txn(
                db, evt, event_id=event_id, source_label=source_label)
        else:
            if patient_id is None:
                # PHI-bearing observation that couldn't be linked to a known patient.
                # It is still persisted (with NULL patient_id) so nothing is dropped,
                # but it is now logged so unlinked clinical data is observable rather
                # than silently orphaned. With ORB_RESULT_QUARANTINE on, the branch
                # above sends a result to the unmatched results queue instead
                # (lis-19); a device lane's metrics stay here.
                logger.warning(
                    "Observation '%s' could not be resolved to a patient (raw id=%r); "
                    "inserting unlinked (no review queue: ORB_RESULT_QUARANTINE is off, "
                    "or this is a device lane's metric, lane %r).",
                    evt.observation_name, evt.patient_identifier, source_label,
                )
            obs_id, _superseded = write_observation_in_txn(
                db, evt, patient_id, event_id, source_label, correlation_id=correlation_id,
                sibling_in_message=sibling_in_message)

    if quarantine_id is not None:
        # Fail-soft 'ingestion' category after commit, as below.
        log_audit_event(
            correlation_id=correlation_id,
            event_category="ingestion",
            action_type="observation_quarantined",
            outcome="success",
            encounter_id=evt.encounter_identifier,
            source_type=source_label,
            source_event_id=event_id,
            target_type="result_quarantine",
            target_id=str(quarantine_id),
            summary_message=f"Held unmatched result for review: {evt.observation_name}"
        )
        return "quarantined"

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
    return "inserted"


# ════════════════════════════════════════════
#  Observation Writer (shared with the unmatched results queue)
# ════════════════════════════════════════════
# One writer for an interfaced result, so a result attached from the unmatched
# results queue lands exactly as it would have at receipt, correction rule
# included. Every function here takes the caller's open transaction and never
# commits.

def _quarantine_on() -> bool:
    import config
    return bool(config.feature_enabled("result_quarantine"))


def _corrections_on() -> bool:
    import config
    return bool(config.feature_enabled("result_corrections"))


def _queue_takes(evt: CanonicalEvent, source_label: str) -> bool:
    """Whether the unmatched results queue takes this unmatched observation:
    a result from a result feed, not a device lane's metric (lis-19)."""
    from services import result_quarantine_service
    return result_quarantine_service.takes_result(source_label, evt.observation_code_system)


def is_external_correction(evt: CanonicalEvent) -> bool:
    """An HL7 OBX-11 'C': this result corrects an earlier final one."""
    return str(evt.status or "").strip().upper() == "C"


def correction_order(placer: Optional[str], filler: Optional[str],
                     other_placer: Optional[str], other_filler: Optional[str]) -> Optional[str]:
    """'filler' or 'placer' when two results belong to the same order, else None.

    The same OBR-3 filler number; or, when either side has no filler number,
    the same OBR-2 placer number the sender supplied. Two different filler
    numbers are two different orders, whatever the placer says. A result with
    neither number belongs to no order a correction can name.
    """
    filler, other_filler = (filler or "").strip(), (other_filler or "").strip()
    placer, other_placer = (placer or "").strip(), (other_placer or "").strip()
    if filler and other_filler:
        return "filler" if filler == other_filler else None
    if placer and other_placer and placer == other_placer:
        return "placer"
    return None


def _result_identity(evt: CanonicalEvent) -> tuple:
    """(code, OBX-4 sub-id, sender, placer, filler) of one parsed result. The
    placer is None when the adapter invented the group id."""
    placer = None if evt.group_id_generated else evt.obr_placer
    return ((evt.observation_code or "").strip(), (evt.obx_sub_id or "").strip(),
            (evt.hl7_sender or "").strip(), placer, evt.obr_filler)


def message_has_sibling_result(evt: CanonicalEvent, message_events) -> bool:
    """Whether another result in the same message has the same test code,
    OBX-4 sub-id, sender and order as `evt`, whatever its status.

    Two such results (two isolates under one culture code, say) mean the test
    and order do not name one result, so a correction among them cannot be
    pinned to one earlier result and supersedes nothing (lis-20).
    """
    code, sub_id, sender, placer, filler = _result_identity(evt)
    if not code:
        return False
    for other in message_events:
        if other is evt:
            continue
        o_code, o_sub_id, o_sender, o_placer, o_filler = _result_identity(other)
        if (o_code, o_sub_id, o_sender) != (code, sub_id, sender):
            continue
        if correction_order(placer, filler, o_placer, o_filler):
            return True
    return False


def write_observation_in_txn(
    db,
    evt: CanonicalEvent,
    patient_id: Optional[str],
    event_id: Optional[int],
    source_label: str,
    *,
    correlation_id: Optional[str] = None,
    actor: Optional[dict] = None,
    sibling_in_message: bool = False,
    later_correction: Optional[dict] = None,
) -> tuple[int, list[int]]:
    """INSERT one observations row on the caller's transaction.

    Returns (observation id, ids it superseded). The order identity columns
    (migration 60) are always written: obr_placer only when the sender sent
    OBR-2, never the random group id. With ORB_RESULT_CORRECTIONS on and a
    patient known, an OBX-11 'C' then supersedes the prior final it corrects,
    when exactly one matches. `sibling_in_message` says the caller knows the
    same message holds another result for the same test and order (one not
    written yet): the correction then supersedes nothing.

    `later_correction` is the unmatched results queue's answer from
    later_correction_for_attach: with outcome 'supersede' the row is stored
    already superseded by the correction that was sent after it, and replaces
    nothing itself.
    """
    placer = None if evt.group_id_generated else evt.obr_placer
    cursor = db.execute(
        """INSERT INTO observations
           (patient_id, encounter_id, source, source_event_id,
            observation_code, observation_code_system, observation_name,
            value_text, value_numeric, unit, reference_range, abnormal_flag,
            status, observed_at, panel_name, obr_group_id, raw_segment_ref,
            obr_placer, obr_filler, obx_sub_id, hl7_sender)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
            placer,
            evt.obr_filler,
            evt.obx_sub_id,
            evt.hl7_sender,
        ),
    )
    obs_id = int(cursor.lastrowid)
    superseded: list[int] = []
    if patient_id and later_correction and later_correction.get("outcome") == "supersede":
        _store_superseded_on_attach_in_txn(
            db, observation_id=obs_id, decision=later_correction, patient_id=patient_id,
            source=source_label, previous_status=evt.status, code=evt.observation_code,
            correlation_id=correlation_id, actor=actor)
        return obs_id, superseded
    if patient_id and _corrections_on() and is_external_correction(evt):
        superseded = supersede_corrected_in_txn(
            db, new_id=obs_id, patient_id=patient_id, code=evt.observation_code,
            sub_id=evt.obx_sub_id, placer=placer, filler=evt.obr_filler,
            sender=evt.hl7_sender, source=source_label, event_id=event_id,
            sibling_in_message=sibling_in_message,
            correlation_id=correlation_id, actor=actor)
    return obs_id, superseded


def supersede_corrected_in_txn(
    db,
    *,
    new_id: int,
    patient_id: str,
    code: Optional[str],
    sub_id: Optional[str],
    placer: Optional[str],
    filler: Optional[str],
    sender: Optional[str],
    source: Optional[str],
    event_id: Optional[int] = None,
    sibling_in_message: bool = False,
    correlation_id: Optional[str] = None,
    actor: Optional[dict] = None,
) -> list[int]:
    """Mark the ONE live final result a correction replaces as superseded (lis-20).

    A prior result is a candidate only when it is the SAME patient, the same
    test code, the same OBX-4 sub-id, the same sender (MSH-3 and MSH-4) and the
    same lane, is live and final (or an earlier correction), was not released
    by the laboratory module (that lane amends its own results), arrived in an
    earlier message than the correction (never its own, `event_id`), and
    belongs to the same order (correction_order): the same OBR-3 filler
    number, or, when either side has no filler number, the same OBR-2 placer
    number the sender supplied. The random group id the adapter invents for a
    missing OBR-2 is never stored as a placer, so it can never match.

    Strictest rule (held for Samuel to confirm before go-live): a correction
    supersedes a result only when exactly one candidate matches AND its own
    message holds no other result for the same test and order (two isolates
    under one culture code, say). Otherwise the test and order do not name one
    result, and hiding the wrong one would take a valid result off the chart,
    the ward pack and Tier 1. So:
      * no candidate: nothing changes, the correction is stored beside, as
        before;
      * more than one candidate, or a sibling in the same message (already
        written, or `sibling_in_message` from a caller that knows the whole
        message): nothing is superseded, the correction is stored beside the
        others, and a fail-closed clinical_access row
        (observation_correction_not_applied) records why;
      * exactly one: that row gets status 'superseded' and superseded_by,
        which every reader in services/lab_observations.py leaves out, and a
        fail-closed clinical_access audit row on this transaction.
    If an audit row cannot be written, the correction is rolled back with it.
    """
    from services import lab_observations
    code = (code or "").strip()
    placer = (placer or "").strip()
    filler = (filler or "").strip()
    if not patient_id or not code or not (placer or filler):
        return []
    rows = lab_observations.correction_candidates(
        db, patient_id=patient_id, code=code, sub_id=sub_id, sender=sender,
        source=source, exclude_id=int(new_id))
    matched: list[tuple] = []
    for row in rows:
        if (event_id is not None and row["source_event_id"] is not None
                and int(row["source_event_id"]) == int(event_id)):
            continue  # never a result from the correction's own message
        matched_on = correction_order(placer, filler, row["obr_placer"], row["obr_filler"])
        if matched_on:
            matched.append((row, matched_on))
    if not matched:
        return []
    siblings = [
        r["id"] for r in lab_observations.same_message_results(
            db, event_id=event_id, code=code, sub_id=sub_id, sender=sender,
            source=source, exclude_id=int(new_id))
        if correction_order(placer, filler, r["obr_placer"], r["obr_filler"])
    ]
    actor = actor or {}
    if len(matched) > 1 or siblings or sibling_in_message:
        why = ("several_earlier_results" if len(matched) > 1
               else "several_results_in_message")
        logger.warning(
            "Corrected result %s supersedes nothing: %s for test %s on this order (%d "
            "earlier, %d more in its message); stored beside them.",
            new_id, why, code, len(matched), len(siblings) + int(bool(sibling_in_message)))
        log_audit_event(
            correlation_id=correlation_id or str(uuid.uuid4()),
            event_category="clinical_access",
            action_type="observation_correction_not_applied",
            outcome="success",
            db=db,
            actor_user_id=actor.get("user_id"),
            actor_role=actor.get("role"),
            patient_id=patient_id,
            source_type=source,
            source_event_id=event_id,
            target_type="observations",
            target_id=str(new_id),
            summary_message=(f"Corrected result {new_id} stored beside the earlier results: "
                             "the test and order do not name exactly one result"),
            metadata={"reason": why, "candidates": [row["id"] for row, _ in matched],
                      "same_message": siblings,
                      "sibling_in_message": bool(sibling_in_message),
                      "observation_code": code},
        )
        return []
    row, matched_on = matched[0]
    cur = db.execute(
        "UPDATE observations SET status = 'superseded', superseded_by = ? "
        "WHERE id = ? AND superseded_by IS NULL",
        (int(new_id), row["id"]),
    )
    if cur.rowcount != 1:  # pragma: no cover - the caller holds the write lock
        return []
    log_audit_event(
        correlation_id=correlation_id or str(uuid.uuid4()),
        event_category="clinical_access",
        action_type="observation_superseded",
        outcome="success",
        db=db,
        actor_user_id=actor.get("user_id"),
        actor_role=actor.get("role"),
        patient_id=patient_id,
        source_type=source,
        target_type="observations",
        target_id=str(row["id"]),
        summary_message=f"Result {row['id']} superseded by corrected result {new_id}",
        metadata={"superseded_by": int(new_id), "matched_on": matched_on,
                  "previous_status": row["status"], "observation_code": code},
    )
    return [row["id"]]


def attach_unlinked_observation_in_txn(
    db,
    observation_id: int,
    patient_id: str,
    *,
    correlation_id: Optional[str] = None,
    actor: Optional[dict] = None,
    sibling_in_message: bool = False,
    later_correction: Optional[dict] = None,
) -> tuple[int, list[int]]:
    """Give a result stored with no patient its patient (an attach from the
    unmatched results queue of a row the sweep copied). The row keeps its id
    and everything else. Returns (observation id, ids it superseded). A
    correction supersedes exactly as at receipt (supersede_corrected_in_txn),
    its own message being the raw message the row came from.
    `later_correction` as for write_observation_in_txn."""
    from services import lab_observations
    row = lab_observations.unlinked_result(db, int(observation_id))
    if row is None:
        raise LookupError(f"observation {observation_id} is not a result with no patient")
    cur = db.execute(
        "UPDATE observations SET patient_id = ? WHERE id = ? AND patient_id IS NULL",
        (patient_id, int(observation_id)),
    )
    if cur.rowcount != 1:  # pragma: no cover - checked just above on this transaction
        raise LookupError(f"observation {observation_id} already has a patient")
    superseded: list[int] = []
    if later_correction and later_correction.get("outcome") == "supersede":
        _store_superseded_on_attach_in_txn(
            db, observation_id=int(observation_id), decision=later_correction,
            patient_id=patient_id, source=row["source"], previous_status=row["status"],
            code=row["observation_code"], correlation_id=correlation_id, actor=actor)
        return int(observation_id), superseded
    if _corrections_on() and str(row["status"] or "").strip().upper() == "C":
        superseded = supersede_corrected_in_txn(
            db, new_id=int(observation_id), patient_id=patient_id,
            code=row["observation_code"], sub_id=row["obx_sub_id"], placer=row["obr_placer"],
            filler=row["obr_filler"], sender=row["hl7_sender"], source=row["source"],
            event_id=row["source_event_id"], sibling_in_message=sibling_in_message,
            correlation_id=correlation_id, actor=actor)
    return int(observation_id), superseded


# ── A result attached late, after a correction of it ────────────────────────
# The unmatched results queue can attach a result long after the laboratory
# sent a correction of it: the final waited in the queue while its correction
# was attached first, or reached the chart at receipt. Written as it stands,
# the stale final would be the newest row (the same observed time, a higher
# id), so the ward pack would show only the stale value and Tier 1 would read
# it first. later_correction_for_attach says what to do before anything is
# written.

#: Why a late attach is refused, as later_correction_for_attach reports it.
LATE_ORDER_UNKNOWN = "message_order_unknown"
LATE_CORRECTIONS_OFF = "corrections_switched_off"
LATE_SEVERAL_CORRECTIONS = "several_later_corrections"
LATE_NOT_A_FINAL = "not_a_final_result"
LATE_CORRECTION_REPLACED_ANOTHER = "correction_already_replaced_a_result"
LATE_OTHER_RESULTS = "other_results_on_this_order"
LATE_SIBLINGS = "several_results_in_message"


def later_correction_for_attach(
    db,
    *,
    patient_id: str,
    code: Optional[str],
    sub_id: Optional[str],
    placer: Optional[str],
    filler: Optional[str],
    sender: Optional[str],
    source: Optional[str],
    status: Optional[str],
    event_id: Optional[int],
    own_id: Optional[int] = None,
    sibling_in_message: bool = False,
) -> dict:
    """Whether a correction sent AFTER this result already stands on the
    patient's chart, for a result the unmatched results queue is about to
    attach (lis-19 with lis-20). Reads only; the caller decides and writes.

    A correction counts when it is live, an external OBX-11 'C' not released
    by the laboratory module, for the same patient, test code, OBX-4 sub-id,
    sender, lane and order (correction_order) as this result, and came in a
    LATER message (a higher ingestion event id) than this one. Returns:
      {"outcome": "none"} when there is no such correction: attach as usual;
      {"outcome": "supersede", "correction_id", "matched_on"} when the picture
        is exactly what receipt in order would have made of it: one such
        correction, ORB_RESULT_CORRECTIONS on, this result a final (or an
        earlier correction), the correction replacing nothing yet, no other
        final on the chart for this test and order, and nothing else under
        this test and order in this result's own message. The result is then
        stored already superseded by it;
      {"outcome": "refuse", "reason", "candidates"} otherwise (strictest
        option, held for Samuel): the result would land in front of a later
        correction and Orb cannot say which result that correction replaces,
        or which message came first. Nothing is written; the queue says why,
        and laboratory staff discard it or ask the laboratory.
    """
    from services import lab_observations
    code = (code or "").strip()
    if not patient_id or not code or not ((placer or "").strip() or (filler or "").strip()):
        return {"outcome": "none"}
    rows = lab_observations.live_results_for_test(
        db, patient_id=patient_id, code=code, sub_id=sub_id, sender=sender, source=source,
        exclude_id=own_id)
    same_order = []
    for row in rows:
        matched_on = correction_order(placer, filler, row["obr_placer"], row["obr_filler"])
        if matched_on:
            same_order.append((row, matched_on))
    corrections = [(r, m) for r, m in same_order
                   if r["is_correction"] and r["lab_result_id"] is None]
    if not corrections:
        return {"outcome": "none"}
    ids = [int(r["id"]) for r, _ in corrections]
    if event_id is None or any(r["source_event_id"] is None for r, _ in corrections):
        return {"outcome": "refuse", "reason": LATE_ORDER_UNKNOWN, "candidates": ids}
    later = [(r, m) for r, m in corrections if int(r["source_event_id"]) > int(event_id)]
    if not later:
        return {"outcome": "none"}
    later_ids = [int(r["id"]) for r, _ in later]

    def refuse(reason: str) -> dict:
        return {"outcome": "refuse", "reason": reason, "candidates": later_ids}

    if not _corrections_on():
        return refuse(LATE_CORRECTIONS_OFF)
    if len(later) > 1:
        return refuse(LATE_SEVERAL_CORRECTIONS)
    if str(status or "final").strip().lower() not in lab_observations.CORRECTABLE_STATUSES:
        return refuse(LATE_NOT_A_FINAL)
    correction, matched_on = later[0]
    if correction["replaced_another"]:
        return refuse(LATE_CORRECTION_REPLACED_ANOTHER)
    others = [r for r, _ in same_order if int(r["id"]) != int(correction["id"])
              and str(r["status"] or "final").strip().lower()
              in lab_observations.CORRECTABLE_STATUSES]
    if others:
        return refuse(LATE_OTHER_RESULTS)
    siblings = [
        r for r in lab_observations.same_message_results(
            db, event_id=event_id, code=code, sub_id=sub_id, sender=sender, source=source,
            exclude_id=-1 if own_id is None else int(own_id))
        if correction_order(placer, filler, r["obr_placer"], r["obr_filler"])
    ]
    if sibling_in_message or siblings:
        return refuse(LATE_SIBLINGS)
    return {"outcome": "supersede", "correction_id": int(correction["id"]),
            "matched_on": matched_on}


def later_correction_for_event(db, evt: CanonicalEvent, patient_id: str, *,
                               event_id: Optional[int], source_label: str,
                               sibling_in_message: bool = False) -> dict:
    """later_correction_for_attach for a parsed result not written yet."""
    placer = None if evt.group_id_generated else evt.obr_placer
    return later_correction_for_attach(
        db, patient_id=patient_id, code=evt.observation_code, sub_id=evt.obx_sub_id,
        placer=placer, filler=evt.obr_filler, sender=evt.hl7_sender, source=source_label,
        status=evt.status, event_id=event_id, sibling_in_message=sibling_in_message)


def later_correction_for_unlinked(db, observation_id: int, patient_id: str, *,
                                  sibling_in_message: bool = False) -> dict:
    """later_correction_for_attach for a result stored with no patient (a row
    the sweep copied). {"outcome": "none"} when the id names no such row;
    the attach itself then refuses it."""
    from services import lab_observations
    row = lab_observations.unlinked_result(db, int(observation_id))
    if row is None:
        return {"outcome": "none"}
    return later_correction_for_attach(
        db, patient_id=patient_id, code=row["observation_code"], sub_id=row["obx_sub_id"],
        placer=row["obr_placer"], filler=row["obr_filler"], sender=row["hl7_sender"],
        source=row["source"], status=row["status"], event_id=row["source_event_id"],
        own_id=int(row["id"]), sibling_in_message=sibling_in_message)


def _store_superseded_on_attach_in_txn(
    db,
    *,
    observation_id: int,
    decision: dict,
    patient_id: str,
    source: Optional[str],
    previous_status: Optional[str],
    code: Optional[str],
    correlation_id: Optional[str],
    actor: Optional[dict],
) -> None:
    """Mark a result just attached from the unmatched results queue as
    superseded by the correction that was sent after it, with a fail-closed
    clinical_access audit row on the caller's transaction: if the row cannot
    be written, the attach rolls back with it."""
    correction_id = int(decision["correction_id"])
    cur = db.execute(
        "UPDATE observations SET status = 'superseded', superseded_by = ? "
        "WHERE id = ? AND superseded_by IS NULL",
        (correction_id, int(observation_id)),
    )
    if cur.rowcount != 1:  # pragma: no cover - the row was written on this transaction
        raise LookupError(f"observation {observation_id} could not be marked superseded")
    actor = actor or {}
    log_audit_event(
        correlation_id=correlation_id or str(uuid.uuid4()),
        event_category="clinical_access",
        action_type="observation_superseded",
        outcome="success",
        db=db,
        actor_user_id=actor.get("user_id"),
        actor_role=actor.get("role"),
        patient_id=patient_id,
        source_type=source,
        target_type="observations",
        target_id=str(observation_id),
        summary_message=(f"Result {observation_id} attached from the unmatched results queue "
                         f"already superseded by corrected result {correction_id}, which the "
                         "laboratory sent after it"),
        metadata={"superseded_by": correction_id, "matched_on": decision.get("matched_on"),
                  "previous_status": previous_status, "observation_code": code,
                  "attached_after_correction": True},
    )


def unlinked_observations_not_queued(db, *, limit: int = 500, exclude_sources=(),
                                     exclude_code_systems=()) -> list:
    """Results stored with no patient that the unmatched results queue does
    not hold yet, oldest first: the queue's one-time sweep (lis-19). Read
    through services/lab_observations.py like every other read. The two
    exclusions leave out device lanes' metrics, which the queue never takes."""
    from services import lab_observations
    return lab_observations.unlinked_results_not_queued(
        db, limit=limit, exclude_sources=exclude_sources,
        exclude_code_systems=exclude_code_systems)
