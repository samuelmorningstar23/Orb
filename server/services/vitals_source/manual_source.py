"""Orb Hospital OS — manual (clinician-typed) vitals as a `VitalsSource` peer.

Manual entry is the redundancy layer, never the plan — and it is also the layer
that will still be carrying most of the ward on go-live day. Treating it as a
first-class source rather than "the thing that happens when there is no monitor"
buys three things:

  * a nurse-typed reading is stamped `manual_entry`, so months later the shadow
    scorecard can separate "Orb saw it on a monitor trace" from "Orb saw it
    because someone wrote it down" — a distinction the pilot's central claim
    depends on;
  * typed readings travel the SAME pipeline as machine ones, so they get
    deduplication, the audit chain, NEWS2, the Vigil→Relay alert and the live
    tile update without a second implementation;
  * when a monitor is connected for a bed, both sources coexist and are
    distinguishable instead of one silently masquerading as the other.

Unlike its peers this source has no loop: it is a PUSH source, driven by whatever
records an observation (the nurse observation route, a tablet, the admission
form). `start()`/`stop()` therefore only arm and disarm it, and `record()` is
the entry point.

NOTE ON WIRING: this source fronts THREE manual paths with different delivery
contracts, and the difference is deliberate:

  * `record()`/`submit()` — label `manual_entry`. Readings travel the shared
    async ingestion sink like every other source's, with its dedup, raw-event
    capture and fail-soft error counting.
  * `record_observation()` — label `nurse_manual`. The nurse charting path
    (`nurse_shift_service.record_observation` delegates here). It is
    synchronous and FAIL-LOUD by recorded design decision: an observation
    feeds an escalation decision, so it is never queued or optimistic and the
    nurse must see the failure. The sink cannot carry that contract — it
    renders readings into the JSON webhook shape (which drops ACVPU and
    supplemental O2, the two strongest NEWS2 escalators), its ordering guard
    would backfill-suppress a late-charted set, and `ingest_payload` counts
    failures instead of raising — so this path wraps the service's own
    transactional write instead of the sink.
  * `record_at_admission()` — label `clinician`. The admission form's initial
    vitals set (`admission_service.admit_patient`). Also synchronous and
    FAIL-LOUD, and unlike the other two it writes INSIDE THE CALLER'S OPEN
    TRANSACTION: an admission is one atomic unit, so the reading must commit
    with the patient, the admission and the case room or not at all. A vitals
    row that outlived a rolled-back admission would reference a patient who was
    never admitted.

All three stamp a label `classify_source_label` maps to MANUAL, leave
`source_event_id` NULL (a typed set has no wire event), and are counted in this
source's stats — so "is manual entry alive?" is one question with one answer.
"""
from __future__ import annotations

import logging
from typing import Optional, Sequence

from services.clinical_time import to_utc_iso, utc_iso_now
from services.ingestion_service import IngestionResult

from .base import (
    LABEL_ADMISSION_VITALS,
    LABEL_MANUAL_ENTRY,
    LABEL_NURSE_OBSERVATION,
    VITAL_KEYS,
    SourceKind,
    VitalsReading,
    VitalsSource,
    utc_now_iso,
)

logger = logging.getLogger("orb.vitals_source.manual")


class ManualEntrySource(VitalsSource):
    """Vitals a human read off a device and typed in."""

    kind = SourceKind.MANUAL
    provenance_label = LABEL_MANUAL_ENTRY

    def __init__(self, sink, *, source_id: str = "manual"):
        super().__init__(source_id)
        self.sink = sink

    async def start(self) -> None:
        self._running = True

    async def stop(self) -> None:
        self._running = False

    async def record(
        self,
        patient_identifier: str,
        values: dict,
        *,
        entered_by: Optional[str] = None,
        observed_at: Optional[str] = None,
        device_id: Optional[str] = None,
        encounter_identifier: Optional[str] = None,
        consciousness: Optional[str] = None,
        on_oxygen: Optional[bool] = None,
        correlation_id: Optional[str] = None,
    ) -> IngestionResult:
        """Record one set of typed vitals.

        `values` uses the canonical keys (HR/SBP/DBP/SpO2/Temp/RR); anything
        absent is left absent so the ingestion carry-forward applies, rather
        than being written as a fabricated zero.
        """
        reading = VitalsReading(
            patient_identifier=str(patient_identifier),
            observed_at=observed_at or utc_now_iso(),
            values=values or {},
            source_id=self.source_id,
            kind=self.kind,
            device_id=device_id,
            encounter_identifier=encounter_identifier,
            consciousness=consciousness,
            on_oxygen=on_oxygen,
            entered_by=entered_by,
        )
        return await self.submit([reading], correlation_id=correlation_id)

    async def submit(self, readings: Sequence[VitalsReading], *,
                     correlation_id: Optional[str] = None) -> IngestionResult:
        """Submit prepared readings. Counts, never raises into the caller's route."""
        self.stats.messages_received += len(readings)
        try:
            result = await self.sink.submit_readings(
                readings, source=self, correlation_id=correlation_id)
        except Exception as e:
            self.stats.messages_rejected += len(readings)
            self.stats.note_error(str(e))
            logger.exception("[%s] manual vitals submission failed", self.source_id)
            raise
        self.stats.messages_accepted += len(readings)
        self.stats.readings_ingested += result.inserted_vitals
        self.stats.duplicates += result.duplicates
        if result.backfill:
            self.stats.backfilled += 1
        if result.inserted_vitals:
            self.stats.last_reading_at = utc_now_iso()
        if result.errors:
            self.stats.note_error("; ".join(result.errors)[:300])
        return result

    #: `vitals.source` written by the nurse charting path below. Distinct from
    #: `provenance_label` above; both classify as MANUAL.
    observation_label = LABEL_NURSE_OBSERVATION

    def record_observation(
        self,
        patient_id: str,
        actor_id: str,
        obs: dict,
        *,
        idempotency_key: Optional[str] = None,
        taken_at: Optional[str] = None,
        source: Optional[str] = None,
        illegible: Optional[list] = None,
    ) -> dict:
        """Chart a nurse-entered observation set. Synchronous and FAIL-LOUD.

        `source` is None for the nurse's own set (`observation_label`) and
        LABEL_CLERK_TRANSCRIBED for a set the ward clerk copied from the paper
        sheet; `illegible` names the readings the sheet showed but nobody could
        read. Both pass straight through to the transaction.

        Wraps `nurse_shift_service._record_observation_txn` — one transaction
        holding the vitals row (source=`observation_label`), the risk_scores
        row (NEWS2 via `vigil_service.compute_news2`, the single scoring
        implementation) and the fail-closed audit entry — and RE-RAISES
        anything it raises, because the recorded design decision for this path
        is that the nurse sees the failure rather than an optimistic ack. What
        the wrapper adds is uniformity: the write is counted in this source's
        stats, so the ward's typed observations are visible on the same health
        surface as every other vitals feed.
        """
        from services import nurse_shift_service

        self.stats.messages_received += 1
        try:
            result = nurse_shift_service._record_observation_txn(
                patient_id, actor_id, obs,
                idempotency_key=idempotency_key, taken_at=taken_at,
                source=source, illegible=illegible)
        except Exception as e:
            self.stats.messages_rejected += 1
            self.stats.note_error(str(e))
            raise
        self.stats.messages_accepted += 1
        if result.get("replayed"):
            self.stats.duplicates += 1
        else:
            self.stats.readings_ingested += 1
            self.stats.last_reading_at = utc_now_iso()
        return result

    #: `vitals.source` written by the admission path below. Distinct again from
    #: the two labels above; all three classify as MANUAL.
    admission_label = LABEL_ADMISSION_VITALS

    def record_at_admission(
        self,
        db,
        patient_id: str,
        values: dict,
        *,
        observed_at: str,
        consciousness: Optional[str] = None,
        on_oxygen: Optional[bool] = None,
    ) -> int:
        """Write the admission form's initial vitals set. Returns the row id.

        Runs INSIDE `db`, the caller's already-open admission transaction, and
        raises into it — both deliberate. `admission_service.admit_patient` is
        one atomic unit (patient, admission, profile, vitals, case room, Sage
        memory), so this reading commits with the admission or rolls back with
        it, and a failure to record the vitals the clinician typed fails the
        admission loudly rather than admitting a patient whose observations
        silently went nowhere.

        `values` uses the canonical keys (`VITAL_KEYS`); a key outside them is
        rejected rather than dropped, because a silently ignored vital would
        leave the row disagreeing with the NEWS2 the caller computed from the
        same form. Absent keys are written NULL — "not measured", never 0.

        ACVPU and supplemental O2 follow the same rule (ruled 2026-10-03): stored
        as the form supplied them, NULL ("not assessed") when it did not. The
        INSERT used to omit consciousness, so the column default 'alert' replaced
        whatever the clinician chose, and coerced an unknown O2 to on air.

        NEWS2 is NOT computed here: `vigil_service.compute_news2` is the single
        scoring implementation and the caller owns that call.
        """
        values = values or {}
        self.stats.messages_received += 1
        try:
            unknown = sorted(k for k in values if k not in VITAL_KEYS)
            if unknown:
                raise ValueError(
                    f"unknown vital key(s) {unknown}; expected {list(VITAL_KEYS)}")
            # Provenance contract shared with the nurse charting path and the
            # ingestion sink: `source` is a label _LABEL_KIND classifies (here
            # `admission_label` → MANUAL) and `source_event_id` stays NULL
            # because a typed set has no wire event behind it.
            cur = db.execute(
                """INSERT INTO vitals
                       (patient_id, timestamp, heart_rate, systolic_bp, diastolic_bp,
                        spo2, temperature, respiratory_rate, consciousness, on_oxygen, source,
                        entered_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                # The one stored spelling (services/clinical_time.py). The
                # caller is Orb itself, so a naive value is UTC.
                (patient_id, to_utc_iso(observed_at), values.get("HR"), values.get("SBP"),
                 values.get("DBP"), values.get("SpO2"), values.get("Temp"),
                 values.get("RR"),
                 (str(consciousness).strip() or None) if consciousness is not None else None,
                 None if on_oxygen is None else (1 if on_oxygen else 0),
                 self.admission_label,
                 # Migration 77: every keyed set records when it was keyed.
                 utc_iso_now()),
            )
        except Exception as e:
            self.stats.messages_rejected += 1
            self.stats.note_error(str(e))
            raise
        # Counted at write time, not at commit: this source cannot see the
        # caller's COMMIT, so an admission that fails AFTER this point leaves
        # the count one high. That imprecision is confined to a health counter —
        # the reading itself rolls back with the transaction.
        self.stats.messages_accepted += 1
        self.stats.readings_ingested += 1
        self.stats.last_reading_at = utc_now_iso()
        return cur.lastrowid
