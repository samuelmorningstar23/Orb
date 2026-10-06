import asyncio
import random
import os
import json
import time
import uuid
import logging
from datetime import datetime, timezone

logger = logging.getLogger("orb.bot")
from database import get_db, db_session, run_db
from config import UPLOAD_DIR
from services.vigil_service import (generate_ai_recommendation, news2_reason_line,
                                    score_vitals_row, scoring_enabled)
from services.vital_charting import as_charted, vitals_line
from services.vitals_carry import carry_forward, dump_carried
from services.news2_text import news2_description
from services.clinical_time import utc_iso_now

# The service identities this module writes as, named once. Each must stay in
# database.SERVICE_ACCOUNT_IDS, the set User Management refuses to reset, re-role
# or reactivate (tests/test_bot_service_identities.py pins it): a password reset
# on the account Vigil pages as would make it a hospital-wide doctor login.
VIGIL_BOT_ID = "bot_vigil"
LAB_BOT_ID = "bot_lab"
LENS_BOT_ID = "bot_lens"

#: clinical_documents.source / lens_analyses.source on a row a simulator wrote
#: (migration 46). The one marker every reader checks.
SIMULATED_SOURCE = "simulated"

# ── Pruning config ──
MAX_VITALS_PER_PATIENT = 2000
PRUNE_EVERY_N_CYCLES = 60  # prune once every ~5 minutes (60 × 5s interval)
_cycle_counter = 0

# ── Alert cooldown tracker (patient_id → (last_alert_timestamp, last_level)) ──
# NOTE: process-local state — see deferred multi-worker finding. The level is
# tracked so a worsening trajectory (e.g. elevated→critical) can bypass the
# cooldown rather than being muted for the window where seconds matter.
# (last_alert_timestamp, last_level, last_raw_news2). The NEWS2 aggregate is
# part of the key state, not decoration: it is how "this patient is WORSE than
# when we last paged" is decided, which is the only thing allowed to bypass the
# cooldown for an already-critical patient. See evaluate_and_dispatch_alerts.
_alert_cooldowns: dict[str, tuple[float, str, int | None]] = {}
ALERT_COOLDOWN_SECONDS = 120  # 2 minutes between alerts per patient
# Floor on how often a DETERIORATION may re-page, i.e. how fast the
# escalation/worsening bypass below is allowed to fire. Deterioration must page
# quickly — that is the whole product — but a monitor sampling every 5 seconds
# makes NEWS2 flicker up by a point routinely, and a page per flicker is alert
# fatigue wearing a safety argument. 30s is a quarter of the cooldown: fast
# enough that a real decompensation still reaches the team inside half a minute,
# slow enough that noise cannot ring the bell twelve times a minute.
MIN_REALERT_SECONDS = 30
# Ordinal ranking of NEWS2 bands, used to detect escalation across alerts.
_LEVEL_RANK = {"stable": 0, "elevated": 1, "critical": 2}
# What the simulator's Relay alert asks for, by band (RCP NEWS2: 7+ is an
# emergency response, 5-6 or a single 3 an urgent one).
_ALERT_ASK = {
    "critical": "Immediate review recommended.",
    "elevated": "Urgent review recommended.",
}

# ── Per-patient severity cache (loaded once from DB) ──
_severity_cache: dict[str, str] = {}  # patient_id → "critical" | "elevated" | "stable"

# ── Severity-aware vitals profiles ──
# Each profile defines: targets (what vitals drift toward), reversion strength,
# noise amplitude, clamp ranges, and crisis spike probability
SEVERITY_PROFILES = {
    "critical": {
        "targets": {"hr": 118.0, "sbp": 88.0, "dia": 52.0, "spo2": 90.0, "temp": 38.8, "rr": 26.0},
        "reversion": 0.03,       # weak pull — stays unstable
        "noise": {"hr": 4.0, "sbp": 5.0, "dia": 2.5, "spo2": 0.8, "temp": 0.12, "rr": 1.2},
        "clamp": {"hr": (45, 150), "sbp": (65, 180), "dia": (35, 110), "spo2": (80, 99), "temp": (35.5, 40.5), "rr": (8, 38)},
        "spike_chance": 0.15,    # 15% chance of crisis spike per cycle
    },
    "elevated": {
        "targets": {"hr": 98.0, "sbp": 105.0, "dia": 62.0, "spo2": 93.0, "temp": 37.9, "rr": 22.0},
        "reversion": 0.05,       # moderate pull
        "noise": {"hr": 3.0, "sbp": 4.0, "dia": 2.0, "spo2": 0.5, "temp": 0.08, "rr": 0.8},
        "clamp": {"hr": (50, 130), "sbp": (75, 170), "dia": (42, 105), "spo2": (85, 100), "temp": (36.0, 39.8), "rr": (10, 32)},
        "spike_chance": 0.08,    # 8% chance
    },
    "stable": {
        "targets": {"hr": 78.0, "sbp": 122.0, "dia": 78.0, "spo2": 97.0, "temp": 36.8, "rr": 16.0},
        "reversion": 0.10,       # strong pull toward normal
        "noise": {"hr": 1.5, "sbp": 2.0, "dia": 1.0, "spo2": 0.2, "temp": 0.03, "rr": 0.4},
        "clamp": {"hr": (55, 105), "sbp": (100, 150), "dia": (60, 90), "spo2": (93, 100), "temp": (36.2, 37.8), "rr": (12, 22)},
        "spike_chance": 0.0,     # stable patients don't spike
    },
}

# ── Crisis spike definitions (what happens during a spike event) ──
CRISIS_SPIKES = [
    {"hr": +20, "spo2": -5, "sbp": -15, "rr": +6, "temp": 0, "dia": -8},   # acute decompensation
    {"hr": +15, "spo2": -8, "sbp": 0, "rr": +8, "temp": 0, "dia": 0},      # respiratory distress
    {"hr": +10, "spo2": -3, "sbp": -20, "rr": +4, "temp": +0.8, "dia": -10}, # septic shock pattern
    {"hr": +25, "spo2": -2, "sbp": +20, "rr": +3, "temp": 0, "dia": +12},   # hypertensive crisis
    {"hr": -15, "spo2": -4, "sbp": -12, "rr": -3, "temp": -0.5, "dia": -8}, # bradycardic event
]


def _coalesce_vital(value, fallback):
    """Return ``value`` as a float, falling back when the column is NULL/blank.

    Vitals rows are allowed to be partial — a nurse can chart RR and SpO2
    without a blood pressure, and imported/observation rows routinely leave
    ``diastolic_bp`` NULL. The random-walk arithmetic below cannot subtract
    from ``None``: before this guard a single NULL column raised
    ``unsupported operand type(s) for -: 'float' and 'NoneType'`` out of
    ``_generate_vitals`` and killed the tick for EVERY patient in the batch,
    not just the one with the gap. Falling back to the severity target means
    the walk restarts from a clinically sensible value for that patient's
    acuity instead of crashing.
    """
    if value is None:
        return float(fallback)
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(fallback)


def _get_patient_severity(db, pid: str) -> str:
    """Get the severity profile for a patient (cached)."""
    if pid in _severity_cache:
        return _severity_cache[pid]

    # Determine severity from ward and initial risk score
    row = db.execute(
        """SELECT p.ward, rs.level FROM patients p
           LEFT JOIN risk_scores rs ON rs.patient_id = p.id
           WHERE p.id = ?
           ORDER BY rs.timestamp DESC, rs.id DESC LIMIT 1""",
        (pid,)
    ).fetchone()

    if row:
        ward = (row["ward"] or "").upper()
        risk_level = row["level"] if row["level"] else "stable"

        if ward == "ICU" or risk_level == "critical":
            severity = "critical"
        elif risk_level == "elevated":
            severity = "elevated"
        else:
            severity = "stable"
    else:
        severity = "stable"

    _severity_cache[pid] = severity
    return severity


def _next_reading(db, pid: str, latest: dict, now_ts, *, on_scale_2: bool):
    """One patient's next simulated reading.

    Returns ``(insert_tuple, emit_dict)``. Extracted from the batch loop so a
    failure here can be contained to a single patient — before that, an
    exception raised on ONE patient's row escaped `_generate_vitals` and no
    patient in the batch got a reading at all.

    ``on_scale_2`` is the patient's NEWS2 SpO2 scale (patients.on_scale_2).
    Required, with no default, on purpose: the scorer defaults to Scale 1, so a
    caller that forgets it gets a plausible wrong score rather than an error.
    """
    severity = _get_patient_severity(db, pid)
    profile = SEVERITY_PROFILES[severity]
    targets = profile["targets"]
    reversion = profile["reversion"]
    noise = profile["noise"]
    clamps = profile["clamp"]

    # Partial vitals rows are NORMAL (see _coalesce_vital). Resolve every
    # channel to a float BEFORE any arithmetic touches it.
    cur_hr = _coalesce_vital(latest.get("heart_rate"), targets["hr"])
    cur_sys = _coalesce_vital(latest.get("systolic_bp"), targets["sbp"])
    cur_dia = _coalesce_vital(latest.get("diastolic_bp"), targets["dia"])
    cur_spo2 = _coalesce_vital(latest.get("spo2"), targets["spo2"])
    cur_temp = _coalesce_vital(latest.get("temperature"), targets["temp"])
    cur_rr = _coalesce_vital(latest.get("respiratory_rate"), targets["rr"])

    # Random walk + mean-reversion toward severity-appropriate targets
    hr_shift = random.gauss(0, noise["hr"]) + reversion * (targets["hr"] - cur_hr)
    sys_shift = random.gauss(0, noise["sbp"]) + reversion * (targets["sbp"] - cur_sys)
    dia_shift = random.gauss(0, noise["dia"]) + reversion * (targets["dia"] - cur_dia)
    spo2_shift = random.gauss(0, noise["spo2"]) + reversion * (targets["spo2"] - cur_spo2)
    temp_shift = random.gauss(0, noise["temp"]) + reversion * (targets["temp"] - cur_temp)
    rr_shift = random.gauss(0, noise["rr"]) + reversion * (targets["rr"] - cur_rr)

    # ── Periodic crisis spikes for critical/elevated patients ──
    if random.random() < profile["spike_chance"]:
        spike = random.choice(CRISIS_SPIKES)
        hr_shift += spike["hr"]
        spo2_shift += spike["spo2"]
        sys_shift += spike["sbp"]
        rr_shift += spike["rr"]
        temp_shift += spike["temp"]
        dia_shift += spike["dia"]

    # Trajectory modifier for additional variety
    traj_mod = hash(pid) % 3
    if traj_mod == 1 and severity == "stable":  # only stable patients actively improve
        hr_shift -= 0.3
        sys_shift += 0.5
        spo2_shift += 0.1

    # Clamp to severity-appropriate ranges. These add to the RESOLVED cur_*
    # values, not to latest[...] — the same NULL that broke the shift
    # arithmetic above breaks this addition too, and fixing only one of the two
    # pairs leaves the tick still dying.
    new_hr = round(max(clamps["hr"][0], min(clamps["hr"][1], cur_hr + hr_shift)), 1)
    new_sys = round(max(clamps["sbp"][0], min(clamps["sbp"][1], cur_sys + sys_shift)), 1)
    new_dia = round(max(clamps["dia"][0], min(clamps["dia"][1], cur_dia + dia_shift)), 1)
    new_spo2 = round(max(clamps["spo2"][0], min(clamps["spo2"][1], cur_spo2 + spo2_shift)), 1)
    new_temp = round(max(clamps["temp"][0], min(clamps["temp"][1], cur_temp + temp_shift)), 1)
    new_rr = round(max(clamps["rr"][0], min(clamps["rr"][1], cur_rr + rr_shift)), 1)

    # ── Carry forward the two NEWS2 parameters the simulator doesn't
    #    actively model (ACVPU consciousness + supplemental O2) from the
    #    patient's most recent row, so the LIVE risk path scores the full
    #    7-parameter NEWS2 instead of a 5-parameter subset. Both are now
    #    persisted per row (on_oxygen column added in database.py).
    #
    #    AGE-LIMITED (ruled 2026-10-03, services/vitals_carry.py): carried only
    #    for 12 h from the nurse's actual assessment, which every simulated row
    #    now records in `carried_from`. A nurse's one "confused" used to lead
    #    every later simulated alert for as long as the simulator ran. Past the
    #    limit, or when nobody ever charted it, the value is None (not
    #    assessed). It is no longer filled in as "alert" / "on air": that
    #    default would re-invent an expired assessment as fresh on the next
    #    tick, and it presented a finding nobody made. ──
    carried_values, carried = carry_forward(latest, now_ts, ["consciousness", "on_oxygen"])
    consciousness = carried_values["consciousness"]
    on_oxygen = (None if carried_values["on_oxygen"] is None
                 else bool(carried_values["on_oxygen"]))

    # Scored through the SHARED entry point, on THIS patient's SpO2 scale. Until
    # 2026-10-02 this called compute_news2 with no scale, so a COPD patient on
    # Scale 2 was scored on Scale 1 here and nowhere else: SpO2 88 on air paged
    # as +3, SpO2 97 on O2 (over-oxygenation, +3) did not page at all, and the
    # `vitals:update` event below carried that score into the open chart, where
    # VigilPanel replaces the chart's own risk with it.
    # overlay=False keeps this lane NEWS2-only, as it has always been. Putting
    # the CfC overlay on the paging path is a separate decision.
    # Scoring switched off (the Orb Ward Record): the reading is stored and
    # pushed with risk None, and nothing below pages on it.
    risk = score_vitals_row(
        {"heart_rate": new_hr, "systolic_bp": new_sys, "respiratory_rate": new_rr,
         "temperature": new_temp, "spo2": new_spo2,
         "consciousness": consciousness, "on_oxygen": on_oxygen},
        on_scale_2=on_scale_2, overlay=False,
    ) if scoring_enabled() else None

    return (
        (pid, now_ts, new_hr, new_sys, new_dia, new_spo2,
         new_temp, new_rr, consciousness,
         None if on_oxygen is None else (1 if on_oxygen else 0),
         dump_carried(carried)),
        {
            "patient_id": pid,
            "timestamp": now_ts,
            "heart_rate": new_hr,
            "systolic_bp": new_sys,
            "diastolic_bp": new_dia,
            "spo2": new_spo2,
            "temperature": new_temp,
            "respiratory_rate": new_rr,
            # The wire is never null for these two (ws/realtime.vitals_update_
            # payload, the same coercion ingestion applies): whether ACVPU and
            # O2 were assessed travels in risk.missing_parameters. The STORED
            # row keeps NULL, which is what scoring and carry-forward read.
            "consciousness": consciousness or "alert",
            "on_oxygen": bool(on_oxygen),
            "risk": risk,
        },
    )


# Module level, not a closure inside the async caller: this body runs on a
# run_db() worker thread, so every value it touches must arrive as an
# argument or a module global. Nesting it in the coroutine made it one
# edit away from silently capturing a loop-side local and reading it from
# another thread.
def _generate_vitals(now_ts, cycle_counter):
    with db_session() as db:
        # on_scale_2 rides along on the query the tick already runs, and is
        # re-read every tick rather than cached like severity: a scale charted
        # mid-shift applies from the next reading.
        patients = db.execute(
            "SELECT id, on_scale_2 FROM patients WHERE status = 'admitted'"
        ).fetchall()

        if not patients:
            return

        # Fetch latest vitals for ALL patients in one query
        latest_map = {}
        for row in db.execute(
            """SELECT v.* FROM vitals v
               INNER JOIN (
                   SELECT patient_id, MAX(timestamp) as max_ts
                   FROM vitals WHERE voided_at IS NULL GROUP BY patient_id
               ) latest ON v.patient_id = latest.patient_id AND v.timestamp = latest.max_ts
               WHERE v.voided_at IS NULL"""
        ).fetchall():
            # MAX(timestamp) matches every tied row, in no promised order; carry
            # forward from the later write (higher id), i.e. a same-time correction.
            prev = latest_map.get(row["patient_id"])
            if prev is None or row["id"] > prev["id"]:
                latest_map[row["patient_id"]] = dict(row)

        # Generate new vitals for all patients. One patient's bad row must
        # never cost the whole ward its tick — see the except below.
        inserts = []
        emit_queue = []

        for p in patients:
            pid = p["id"]
            latest = latest_map.get(pid)
            if not latest:
                continue

            try:
                produced = _next_reading(db, pid, latest, now_ts,
                                         on_scale_2=bool(p["on_scale_2"]))
            except Exception:
                # DEFENCE IN DEPTH, and the real lesson of the NULL-BP crash:
                # the BUG was one patient's missing diastolic, but the DAMAGE
                # was that the exception escaped this loop, so every other
                # patient on the ward silently lost their reading too — every
                # 5 seconds, for a week, with nothing on any board moving.
                # Whatever the next bad row turns out to be, it now costs one
                # patient one tick.
                logger.exception("Simulation: skipping patient %s this tick", pid)
                continue

            inserts.append(produced[0])
            emit_queue.append(produced[1])

        # Batch INSERT all vitals in one transaction
        if inserts:
            db.executemany(
                """INSERT INTO vitals (patient_id, timestamp, heart_rate, systolic_bp,
                   diastolic_bp, spo2, temperature, respiratory_rate, consciousness, on_oxygen,
                   carried_from)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                inserts,
            )


        # Periodic pruning — demo housekeeping only.
        # IMPORTANT: prune ONLY simulator-generated rows (source='simulated').
        # Real ingested clinical vitals (HL7/JSON/CSV feeds) are append-only and
        # must NEVER be row-capped — Forecast/Vigil trend logic depend on the
        # longitudinal history, and clinical/medico-legal records can't be shredded.
        # TODO (data, cross-file): replace row-capping with a time-windowed,
        # index-friendly archive policy for a real EHR.
        if cycle_counter % PRUNE_EVERY_N_CYCLES == 0:
            db.execute(f"""
                DELETE FROM vitals WHERE source = 'simulated' AND id NOT IN (
                    SELECT id FROM (
                        SELECT id, ROW_NUMBER() OVER (PARTITION BY patient_id ORDER BY timestamp DESC) as rn
                        FROM vitals WHERE source = 'simulated'
                    ) WHERE rn <= {MAX_VITALS_PER_PATIENT}
                )
            """)
            db.execute("""
                DELETE FROM risk_scores WHERE id NOT IN (
                    SELECT id FROM (
                        SELECT id, ROW_NUMBER() OVER (PARTITION BY patient_id ORDER BY timestamp DESC) as rn
                        FROM risk_scores
                    ) WHERE rn <= 100
                )
            """)
        return emit_queue


async def run_vitals_bot(sio):
    """
    Batched vitals simulator — processes all patients in a single DB
    connection without per-patient sleeps, then emits updates.
    Severity-aware: critical patients stay unstable and fire Vigil alerts.
    """
    global _cycle_counter
    _cycle_counter += 1

    # ── Refresh LSTM forecast predictions every ~10 min (120 × 5s) ──
    # Off the loop like everything else here. This is the heaviest single call
    # in the bot tick — one query plus an LSTM forward pass and a cache write
    # PER admitted patient — and it ran inline, so every 120th tick froze the
    # whole API for its duration. refresh_all_predictions is pure sync
    # (forecast_service imports no asyncio; vram_guard is a sync
    # contextmanager), which is what makes it safe to hand to a worker.
    # The LSTM is a model: not run while the models are switched off
    # (ORB_MODELS_ENABLED, or the Orb Ward Record).
    from services.model_switch import models_enabled
    if _cycle_counter % 120 == 0 and models_enabled():
        try:
            from services.forecast_service import refresh_all_predictions
            await run_db(refresh_all_predictions)
        except Exception as e:
            logger.error("[Forecast] Refresh error: %s", e)

    # The one stored vitals spelling (services/clinical_time.py).
    now_ts = utc_iso_now()
    # The cycle number is snapshotted HERE, on the loop, and passed in. The
    # prune schedule is derived from it inside the worker thread, so reading the
    # global over there would sample whatever the counter happens to be when the
    # thread gets scheduled rather than the tick that actually queued this work.
    emit_queue = await run_db(_generate_vitals, now_ts, _cycle_counter)
    if emit_queue is None:
        return

    # Emit all updates (outside the DB session), scoped to each patient's
    # department room instead of a global broadcast (PHI over-broadcast fix).
    from ws.realtime import emit_patient_vitals
    for payload in emit_queue:
        await emit_patient_vitals("vitals:update", payload)

    # ── VIGIL → RELAY ALERT BRIDGE ──
    # Check each patient's computed risk and send alerts to case rooms
    await evaluate_and_dispatch_alerts(sio, emit_queue)


# Module level, not a closure inside the async caller: this body runs on a
# run_db() worker thread, so every value it touches must arrive as an
# argument or a module global. Nesting it in the coroutine made it one
# edit away from silently capturing a loop-side local and reading it from
# another thread.
def _persist_alerts(alerts, episodes=None):
    """Persist the batch of Vigil alerts as Relay messages.

    ``episodes`` maps patient_id -> shadow episode_id (minted upstream, before
    this write, so `alert.fired.occurred_at` is the moment NEWS2 crossed the
    threshold rather than the moment the row hit the disk). The shadow rows are
    written with ``db=db`` on THIS transaction — a second connection from inside
    this open write transaction would block on the WAL lock this function holds.
    """
    from services import shadow_service
    from services.relay_service import _message_row_to_dict
    episodes = episodes or {}
    out = []
    with db_session() as db:
        # Ensure Orb Vigil bot user exists
        db.execute(
            "INSERT OR IGNORE INTO users (id, name, role, password_hash) VALUES (?, ?, ?, ?)",
            (VIGIL_BOT_ID, 'Orb Vigil', 'doctor', 'system')
        )
        shadow_batch = []
        for alert in alerts:
            episode_id = episodes.get(alert["pid"])
            channel = db.execute(
                "SELECT id FROM channels WHERE type = 'case_room' AND patient_id = ?",
                (alert["pid"],)
            ).fetchone()
            if not channel:
                # A NEGATIVE, and one that is invisible everywhere else in Orb:
                # Vigil fired and the alert reached nobody because the patient
                # has no case room. Silently `continue`-ing here is exactly how
                # a scorecard ends up measuring only the alerts that worked.
                if episode_id:
                    shadow_batch.append(shadow_service.build(
                        "alert.dropped", episode_id, "simulator",
                        occurred_at=alert.get("fired_at"), patient_id=alert["pid"],
                        payload={"reason": "no_case_room", "level": alert["risk_level"]},
                    ))
                continue
            cursor = db.execute(
                """INSERT INTO messages (channel_id, sender_id, sender_name, sender_role, content, message_type, is_bot, source)
                   VALUES (?, ?, ?, ?, ?, 'alert', 1, 'bot')""",
                (channel["id"], VIGIL_BOT_ID, 'Orb Vigil', 'intelligence', alert["content"])
            )
            msg_id = cursor.lastrowid
            row = db.execute("SELECT * FROM messages WHERE id = ?", (msg_id,)).fetchone()
            patient = db.execute("SELECT name FROM patients WHERE id = ?", (alert["pid"],)).fetchone()
            out.append({
                "alert": alert,
                "channel_id": channel["id"],
                "msg_id": msg_id,
                "episode_id": episode_id,
                # The history endpoint's serializer, so the live alert carries
                # `is_bot` (Relay's AUTO chip) and `source` (2026-10-03).
                "msg_data": _message_row_to_dict(row) if row else None,
                "pname": patient["name"] if patient else alert["pid"],
            })
            if episode_id:
                shadow_batch.append(shadow_service.build(
                    "alert.persisted", episode_id, "simulator",
                    patient_id=alert["pid"], subject_type="messages", subject_id=msg_id,
                    payload={"message_id": msg_id, "channel_id": channel["id"]},
                ))
        shadow_service.emit_many(shadow_batch, db=db)
    return out


#: The degraded-mode disclosure, WORD FOR WORD as SageAlertAssessment.tsx
#: renders it beside `fallback_text`. One wording per statement, so the ward
#: cannot be told two stories about the same sentence depending on whether the
#: nurse read it in Relay or on the alert card. If the UI copy changes, this
#: changes with it (test_wedge_loop.py pins the pairing).
RELAY_DEGRADED_MODE_DISCLAIMER = (
    "This is Orb's fixed degraded-mode line, the same sentence for every alert, "
    "not an assessment of this patient."
)


def _fallback_recommendation() -> str:
    """vigil_service's static degraded-mode sentence, or a sentinel.

    Read through a function, not imported at module scope, so that a rename in
    vigil_service degrades to "we emitted one sage.drafted too many" rather than
    an ImportError on the alert path. The sentinel can never equal a model's
    output, so the safe direction on failure is over-counting by one, which the
    log line makes visible.
    """
    try:
        from services.vigil_service import _FALLBACK_RECOMMENDATION
        return _FALLBACK_RECOMMENDATION
    except Exception:
        logger.warning("[Vigil Alert] vigil_service._FALLBACK_RECOMMENDATION is gone — "
                       "sage.drafted can no longer exclude the static fallback")
        return "\x00__orb_no_fallback_sentinel__"


# ═════════════════════════════════════════════════════════════════════════════
#  vigil.alert.critical — ONE publisher, for EVERY lane
# ═════════════════════════════════════════════════════════════════════════════
# The event loop keeps only a WEAK reference to a task (same hazard main.py
# documents for its lifespan loops): a publish task nobody holds can be
# collected mid-flight, and the prewarm would then stop firing with no
# exception and no log line. Bounded so an alert storm cannot turn the nervous
# system into an unbounded coroutine factory — same shape, and same reason, as
# shadow_service._inflight.
_bus_inflight: set = set()
_MAX_BUS_INFLIGHT = 256


def _schedule_assessment(patient_id, *, level, score, drivers,
                         message_id=None, raw_news2=None, lane="simulator") -> bool:
    """Ask Sage for a drafted assessment of an alert that is ALREADY delivered.

    A thin, never-raising wrapper so the alert path has ONE line to read and one
    symbol to patch in a test. All the policy — critical-only, feature flag,
    backpressure, the careful tier, the honest unavailable states — lives in
    services/sage_assessment_service, not here.

    Deliberately not awaited and deliberately not checked: the alert is on a
    clinician's screen before this runs, and 51 ms vitals-to-alert is the
    product. If the draft cannot even be scheduled, the alert stands alone and
    the surface says so.

    WHAT IT SCHEDULES IS NOW THE FAST SHAPE, and the reason is worth stating at
    the call site because this is where someone will look. The first version
    asked for four prose sections and was measured at a median 46 s, which is
    past the point where a second opinion changes anything at a deteriorating
    bedside. What is scheduled here is Tier 1 (the NEWS2 drivers and trend,
    deterministic, no model, on the row within one DB dispatch of this line) plus
    a capped, streamed list of at most three next steps. The four-section draft
    still exists — `sage_assessment_service.generate_full_assessment` — for the
    handover and the ward round, where the wait is invisible.

    THIS LINE'S OWN COST IS UNCHANGED: one `loop.create_task`, nothing awaited.
    """
    try:
        from services.sage_assessment_service import schedule_for_alert
        return schedule_for_alert(
            patient_id, level=level, score=score, drivers=drivers,
            message_id=message_id, raw_news2=raw_news2, lane=lane)
    except Exception as e:
        logger.warning("[Vigil Alert] Sage assessment not scheduled for %s: %s", patient_id, e)
        return False


def build_vigil_critical_event(patient_id, *, level, score, drivers,
                               message_id=None, raw_news2=None, lane="simulator"):
    """THE `vigil.alert.critical` StateEvent. Built in one place, by every lane.

    `nervous_system_triggers.on_critical_vigil_alert` is the hospital's prewarm:
    it opens a deterioration care bundle and pre-assembles the RAG context Sage
    will need, so that when a clinician opens the chart the assessment is warm
    rather than a 20-second wait. Until 2026-08-14 the ONLY publisher was the
    simulator loop below — so the prewarm fired for demo patients and for
    nobody the pilot will actually run on. Manual bedside entry (the ward's
    redundancy layer, and the lane the shadow pilot goes live on) and the
    HL7/FHIR/JSON feed both raised a Relay alert and left the nervous system
    asleep.

    `raw_news2` is threaded DELIBERATELY and is a behaviour change. The
    subscriber's bundle branch reads it and falls back to `start_bundle()`
    unconditionally when it is absent — so an *elevated* (NEWS2 5-6) alert was
    opening a Sepsis Six bundle, bypassing `sepsis_bundle_service`'s own stated
    NEWS2>=7 policy. With the score in the payload the subscriber takes
    `maybe_autostart`, which is that policy. Critical is NEWS2>=7 by
    construction (vigil_service.compute_news2's banding), so no bundle that
    should open stops opening.
    """
    from services.event_bus import StateEvent
    return StateEvent(
        topic="vigil.alert.critical",
        source_node="vigil",
        event_type="patient_deteriorated",
        patient_id=patient_id,
        payload={
            "score": score,
            "level": level,
            "drivers": drivers,
            "message_id": message_id,
            "raw_news2": raw_news2,
            # Which vitals path raised this. Not read by any subscriber today;
            # it is here so "the prewarm never fires for nurse-entered vitals"
            # is answerable from event_log instead of by reading three modules.
            "lane": lane,
        },
    )


def publish_vigil_critical(patient_id, *, level, score, drivers,
                           message_id=None, raw_news2=None, lane="simulator") -> bool:
    """Publish the alert event from ANY lane, on or off the event loop.

    ALSO SCHEDULES SAGE'S DRAFT ASSESSMENT. This is the seam because it is the
    one place every lane already reaches — simulator loop, nurse bedside entry,
    and the HL7/FHIR feed once it is wired (see the note at the end of this
    docstring). Hooking the assessment anywhere lane-specific is how the wedge
    ended up with a Sage output that existed for demo patients and for nobody
    the pilot will run on.

    THE ALERT NEVER WAITS FOR IT. By the time this function is called the Relay
    message row is already written (`message_id` is its id) and the socket emit
    is the caller's next statement. Scheduling costs one `loop.create_task`; the
    draft's first act is an off-loop DB dispatch, so it cannot occupy the loop
    ahead of delivery. Generation itself takes tens of seconds on the 27B and is
    never awaited by anything on the alert path.

    Returns True when the publish was SCHEDULED (not when it completed — the bus
    persists and dispatches asynchronously). Never raises: the nervous system is
    an accelerator, and a clinical alert that has already been written must not
    be lost because a preloader could not be woken. The same applies to the
    draft: a failure to schedule one is logged, never raised, and the alert
    stands alone.

    THREE WAYS IN, because the lanes do not live in the same place:
      * ON THE LOOP — the simulator loop below, and the ingestion path whenever
        it is wired up — `create_task`;
      * IN AN ANYIO WORKER THREAD — a FastAPI `def` endpoint, which is exactly
        what `POST /api/nurse/patients/{id}/observations` is and therefore how
        every nurse-charted observation arrives. anyio's portal hands the
        coroutine back to the loop; the request does not wait on the bus;
      * NO LOOP REACHABLE AT ALL — a script, a unit test, a bare `run_db`
        worker. Logged and skipped. Callers must not depend on the publish
        having happened; nothing clinical does.

    WIRED FROM THE FEED SINCE 2026-10-03: `ingestion_service._check_ingested_alert`
    (the HL7/FHIR/JSON lane) calls this after its Relay write, with
    lane="ingestion", so a device-fed deterioration wakes the prewarm and gets
    a drafted assessment like the other two lanes.
    """
    try:
        evt = build_vigil_critical_event(
            patient_id, level=level, score=score, drivers=drivers,
            message_id=message_id, raw_news2=raw_news2, lane=lane,
        )
    except Exception as build_err:
        logger.warning("[Vigil Alert] Event Bus event build failed: %s", build_err)
        return False

    def _schedule_on_loop() -> bool:
        from services.event_bus import event_bus
        loop = asyncio.get_running_loop()
        # SAGE'S DRAFT, FIRST AND INDEPENDENTLY. First because it is the thing a
        # clinician is about to look for; independently because the two have
        # different failure modes and neither may take the other down —
        # event-bus backpressure must not silently cancel the assessment, and a
        # draft that cannot be scheduled must not stop the prewarm being woken.
        # It has its own bounded in-flight set inside the service. Only critical
        # alerts get one; the service enforces that too, so the rule lives in
        # one place rather than in every call site.
        _schedule_assessment(patient_id, level=level, score=score, drivers=drivers,
                             message_id=message_id, raw_news2=raw_news2, lane=lane)
        if len(_bus_inflight) >= _MAX_BUS_INFLIGHT:
            logger.warning("[Vigil Alert] Event Bus backpressure — publish dropped for %s",
                           patient_id)
            return False
        task = loop.create_task(event_bus.publish(evt))
        _bus_inflight.add(task)
        task.add_done_callback(_bus_inflight.discard)
        return True

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        try:
            return _schedule_on_loop()
        except Exception as bus_err:
            logger.warning("[Vigil Alert] Event Bus publish failed: %s", bus_err)
            return False

    try:
        from anyio.from_thread import run_sync as _portal_run_sync
        return bool(_portal_run_sync(_schedule_on_loop))
    except Exception as bus_err:
        # RuntimeError here means "this thread was not started by anyio", i.e.
        # there is no portal back to the loop. Expected off-request (scripts,
        # unit tests); worth a line, never worth an exception.
        logger.warning(
            "[Vigil Alert] no event loop reachable from this thread — "
            "vigil.alert.critical NOT published for %s (%s)", patient_id, bus_err)
        return False


# ── THE ALERT COOLDOWN, SHARED BY THE TWO MACHINE LANES (2026-10-03) ─────────
# The simulator and the device/HL7 feed both page from machine readings every
# few seconds, so both need the cooldown. Only the simulator had it: a real
# monitor feed paged on every reading. One rule for both, one dict per patient.
# The nurse lane stays outside it on purpose: a nurse charting by hand is not
# a 5-second sampler, and every set she charts above threshold must page.
#
# Still process-local (one worker), but no longer reset by a restart: the first
# check after start-up seeds it from the shadow log's `alert.fired` rows inside
# the window, so a restart does not re-page every elevated patient at once.
COOLDOWN_LANES = ("simulator", "ingestion")
_cooldowns_reconciled = False


def alert_cooldown_check(pid: str, risk_level: str, raw_news2, now: float | None = None):
    """(suppressed, seconds_since_last) for one machine-lane alert.

    The ruled rule, unchanged: ALERT_COOLDOWN_SECONDS between pages, bypassed
    by a band escalation or a rise in NEWS2, but never faster than
    MIN_REALERT_SECONDS. Pure: reads the dict, writes nothing.
    """
    now = time.time() if now is None else now
    last_ts, last_level, last_news2 = _alert_cooldowns.get(pid, (0.0, "stable", None))
    escalated = _LEVEL_RANK.get(risk_level, 0) > _LEVEL_RANK.get(last_level, 0)
    worsened = (
        raw_news2 is not None
        and last_news2 is not None
        and raw_news2 > last_news2
    )
    bypass = (escalated or worsened) and now - last_ts >= MIN_REALERT_SECONDS
    suppressed = not bypass and now - last_ts < ALERT_COOLDOWN_SECONDS
    return suppressed, (round(now - last_ts, 1) if last_ts else None)


def record_alert_dispatched(pid: str, risk_level: str, raw_news2, now: float | None = None) -> bool:
    """Stamp a page for this patient. True when it is a repeat (one was on
    record already), the meaning `repeat` carries on the shadow row."""
    repeat = pid in _alert_cooldowns
    _alert_cooldowns[pid] = (time.time() if now is None else now, risk_level, raw_news2)
    return repeat


def reconcile_alert_cooldowns(now: float | None = None) -> int:
    """Seed the cooldown from `alert.fired` rows inside the window. SYNC (DB):
    call through run_db. Never overwrites a newer in-memory entry. Returns how
    many patients were seeded. With the shadow log switched off it finds
    nothing, which is the old behaviour, not a new failure."""
    now = time.time() if now is None else now
    cutoff = datetime.fromtimestamp(now - ALERT_COOLDOWN_SECONDS, tz=timezone.utc) \
        .isoformat(timespec="milliseconds")
    with db_session() as db:
        rows = db.execute(
            f"""SELECT patient_id, occurred_at, payload_json FROM shadow_events_current
                WHERE kind = 'alert.fired' AND occurred_at >= ?
                  AND lane IN ({",".join("?" * len(COOLDOWN_LANES))})
                ORDER BY occurred_at ASC, id ASC""",
            (cutoff, *COOLDOWN_LANES),
        ).fetchall()
    seeded = 0
    for r in rows:
        try:
            ts = datetime.fromisoformat(r["occurred_at"]).timestamp()
            payload = json.loads(r["payload_json"] or "{}")
        except (TypeError, ValueError):
            continue
        pid = r["patient_id"]
        if not pid or _alert_cooldowns.get(pid, (0.0,))[0] >= ts:
            continue
        _alert_cooldowns[pid] = (ts, payload.get("level") or "stable", payload.get("raw_news2"))
        seeded += 1
    return seeded


async def ensure_cooldowns_reconciled() -> None:
    """Run the start-up reconcile once per process, off the loop. Marked done
    BEFORE it runs: a failed read is logged, not retried on every tick."""
    global _cooldowns_reconciled
    if _cooldowns_reconciled:
        return
    _cooldowns_reconciled = True
    try:
        n = await run_db(reconcile_alert_cooldowns)
        if n:
            logger.info("[Vigil Alert] cooldown seeded for %s patient(s) from the shadow log", n)
    except Exception as e:
        logger.warning("[Vigil Alert] cooldown reconcile failed (starting empty): %s", e)


async def evaluate_and_dispatch_alerts(sio, emit_queue: list):
    """Check all emitted vitals for critical thresholds and post alerts to Relay case rooms.

    This is the single source of truth for Vigil → Relay alert dispatch.
    Called by both the normal bot loop and the deterministic demo trigger.
    """
    from services import shadow_service
    # Scoring switched off: there is no level to alert on, so nothing is sent.
    if not scoring_enabled():
        return
    # Once per process: a restart must not re-page every elevated patient.
    await ensure_cooldowns_reconciled()
    now = time.time()
    alerts_to_send = []
    # Shadow observations for this cycle. Collected on the loop (cheap, no DB)
    # and written in ONE off-loop transaction below.
    shadow_obs: list[dict] = []
    # Sage drafts produced for this cycle's critical alerts. Held until the
    # episodes are resolved below — `sage.drafted` has to land on the SAME
    # episode as the alert it was drafted for, or the loop is three unrelated
    # rows instead of one measurable chain.
    sage_drafts: list[dict] = []

    for payload in emit_queue:
        pid = payload["patient_id"]
        risk = payload.get("risk") or {}
        risk_level = risk.get("level", "stable")
        risk_score = risk.get("score", 0)
        drivers = risk.get("drivers", [])

        # Only alert on non-stable patients (NEWS2 level is source of truth).
        if risk_level == "stable":
            continue

        # ── SHADOW: detection time is stamped HERE ──
        # The instant NEWS2 is known to be above threshold — before the cooldown
        # gate, before the messages INSERT, before the event-bus publish. The
        # closest thing that exists today (event_log.timestamp) is stamped after
        # the DB write, which builds the DB's own write latency into the pilot's
        # headline lead-time number.
        fired_at = shadow_service.utc_now()
        raw_news2 = risk.get("raw_news2", risk.get("raw_mews"))

        # ── Cooldown, with escalation override ──
        # The cooldown fights alert fatigue, but a WORSENING patient must not be
        # muted. Two things bypass it:
        #   * a band escalation (elevated→critical), and
        #   * a critical patient whose NEWS2 has RISEN since their last alert —
        #     SpO2 88→80, new hypotension — which pages the team immediately
        #     instead of waiting out the 2-minute window.
        #
        # The second clause used to read `risk_level != "critical"`, i.e. a
        # critical patient was exempt from the cooldown ENTIRELY. That was
        # invisible while the vitals simulator was crashing (bot_service.py:145
        # — nothing was ever generated, so nothing ever alerted). The moment the
        # simulator was fixed it became a storm: a steady-state critical patient
        # re-alerted on EVERY 5-second tick, four identical auto-alerts landed
        # in their Relay case room per minute, and the scripted clinical thread
        # the ward relies on scrolled out of reach within a couple of minutes.
        # Alert fatigue is the failure mode this cooldown exists to prevent, and
        # a re-alert that says nothing new is the purest form of it.
        #
        # The clinical intent is preserved exactly: deterioration always pages.
        # `_alert_cooldowns` now carries the NEWS2 aggregate that was alerted on,
        # so "worse than last time" is a comparison rather than an assumption.
        # The rule itself lives in alert_cooldown_check, shared with the
        # device/HL7 lane.
        suppressed, seconds_since_last = alert_cooldown_check(pid, risk_level, raw_news2, now)
        if suppressed:
            # A NEGATIVE. The suppressed alert still gets an episode: if it did
            # not, "how many deteriorations did Vigil see but not surface?" would
            # be unanswerable, and the suppression rate would look like zero.
            shadow_obs.append({
                "patient_id": pid,
                "kind": "alert.suppressed",
                "occurred_at": fired_at,
                "trigger": "news2_threshold",
                "episode_payload": {"level": risk_level, "raw_news2": raw_news2},
                "payload": {
                    "level": risk_level,
                    "raw_news2": raw_news2,
                    "suppressed_by": "cooldown",
                    "seconds_since_last": seconds_since_last,
                },
            })
            continue

        # AS CHARTED (services/vital_charting.py): the numbers NEWS2 scored and
        # its drivers print. These feed the vitals line only; the words come
        # from NEWS2 itself (below), so the line cannot read "RR 20" beside a
        # reason reading "Tachypnea (RR 21)".
        hr = as_charted(payload["heart_rate"])
        sbp = as_charted(payload["systolic_bp"])
        spo2 = as_charted(payload["spo2"])
        temp = as_charted(payload["temperature"], 1)
        rr = as_charted(payload["respiratory_rate"])
        dbp = as_charted(payload["diastolic_bp"])

        # ── The reason line IS the NEWS2 breakdown (ruled 2026-10-02, see
        #    `news2_reason_line` in vigil_service). It is wording only and must
        #    NEVER gate dispatch: the NEWS2 level (risk_level != 'stable',
        #    including any single-parameter-3 escalation) is the single source
        #    of truth for whether a patient is alerted. Ad-hoc cutoffs here once
        #    silently swallowed genuinely 'elevated' patients (new confusion,
        #    mild bradypnea, borderline hypotension).
        #
        #    This lane used to compose its own phrases. D5 (2026-08-15) pulled
        #    its "severe" bar into line with `_SEVERE_THRESHOLDS`, but the rest
        #    still disagreed with the driver strip a nurse reads within a minute
        #    of this line: it ignored Scale 2, called HR 101-110 "Tachycardia",
        #    dropped SBP 85-90 and RR 25 whenever another phrase fired, and
        #    printed a 0-point "Hypertension" that could push a 3-point
        #    parameter off the line. ──
        # Wording only, so it can never cost the alert (ruled 2026-10-03, G2): a
        # raise here used to drop the whole alert and every
        # later patient's on the same tick. No real trigger is
        # known; the fallback makes "wording never gates dispatch" true anyway.
        try:
            reason_text = news2_reason_line(risk)
        except Exception:
            logger.exception("reason line failed for %s; sending the band alone", pid)
            reason_text = f"NEWS2 {risk_level}"
        vitals_summary = vitals_line(hr=hr, sbp=sbp, dbp=dbp, spo2=spo2, temp=temp, rr=rr)

        # ── NEWS2 is an ordinal escalation trigger, NOT a calibrated
        #    probability. Report the raw aggregate + clinical band rather than a
        #    fabricated percentage (the old "{score:.0%}" implied a deterioration
        #    probability the rule does not provide and capped the sickest
        #    patients at 100%). ──
        raw_news2 = risk.get("raw_news2", risk.get("raw_mews"))
        # One label for every surface (news2_text): it adds the single-
        # parameter-3 and incomplete-set clauses this line used to drop.
        news2_text = news2_description(risk) if raw_news2 is not None else f"NEWS2 {risk_level}"

        # ONE WORDING PER NEWS2 BAND: same band, same words. This used to be
        # random.choice over three templates, so two identical deteriorations
        # read differently and "Consider bedside evaluation" could land on a
        # critical page. Same opening as the feed and nurse lanes.
        base_alert = (
            f"⚠️ VIGIL ALERT: {reason_text}\nCurrent vitals: {vitals_summary}\n"
            f"{news2_text}. {_ALERT_ASK.get(risk_level, _ALERT_ASK['elevated'])}"
        )

        # Add MedGemma AI Recommendation for critical alerts
        if risk_level == "critical":
            ai_rec = await generate_ai_recommendation(vitals_summary, risk_level, drivers)

            # ── LANE PARITY: ONE test, TWO consumers ─────────────────────────
            # `model_drafted` decides both what the Relay message SAYS and what
            # the shadow instrument RECORDS. It used to decide only the second:
            # the message attributed the sentence to MedGemma unconditionally,
            # so every degraded alert went out reading
            # "✦ MedGemma AI: Ensure continuous monitoring and prepare for
            # possible intervention" — the static fallback, credited to a model
            # that never ran, while the alert surface next door showed the same
            # sentence under "Standing guidance" WITH the degraded-mode
            # disclaimer. Same content, honest in one lane and dishonest in the
            # other. Computing it once means the message and the scorecard can
            # never disagree about whether Sage drafted.
            model_drafted = bool(ai_rec) and ai_rec != _fallback_recommendation()

            if model_drafted:
                base_alert += f"\n\n**✦ MedGemma AI**: {ai_rec}"
            elif ai_rec:
                # VERBATIM the wording SageAlertAssessment.tsx renders, because a
                # phrase any lane strips, every lane must strip — and the same
                # disclosure any lane makes, every lane must make. No ✦, no model
                # name: nothing here was assessed by a model.
                base_alert += (f"\n\n**Standing guidance**: {ai_rec}"
                               f"\n_{RELAY_DEGRADED_MODE_DISCLAIMER}_")
            # ── SHADOW: this is the ONLY Sage output anywhere in the wedge ──
            # Recorded so "what did Sage contribute to this alert?" is a query
            # rather than an opinion. Two deliberate restrictions, both about
            # not overstating it:
            #
            #   * only when a MODEL produced the sentence. On the static
            #     fallback (breaker open, ambient tier evicted, queue timeout)
            #     nothing was drafted, and counting it would make the instrument
            #     report Sage participation on a night Sage was down.
            #   * NO ttft_ms / total_ms. shadow_scorecard_service._time_saved
            #     multiplies every timed `sage.drafted` by an assumed manual-
            #     lookup baseline — the figure its own METRIC_SPEC calls "the
            #     one most likely to be quoted back in a fundraising context".
            #     A one-sentence garnish nobody asked for did not replace a
            #     clinician's lookup, so it must not enter that denominator.
            #     The latency IS worth measuring; adding it needs `_time_saved`
            #     narrowed by `query_class` first, which is a read-side change.
            if model_drafted:
                sage_drafts.append({
                    "pid": pid,
                    "occurred_at": shadow_service.utc_now(),
                    "conversation_id": f"vigil-{uuid.uuid4().hex[:16]}",
                })

        alerts_to_send.append({
            "pid": pid,
            "content": base_alert,
            "risk_level": risk_level,
            # Captured PER ALERT: the send loop below runs after this loop
            # finishes, so reading the loop variables there gave every alert
            # in a batch the LAST patient's score/drivers.
            "score": risk_score,
            "drivers": drivers,
            "raw_news2": raw_news2,
            "fired_at": fired_at,
        })
        # A RE-ALERT JOINS THE OPEN EPISODE (shadow_service.open_episode resolves
        # before it mints), carrying repeat=True. Minting per alert instead would
        # turn a storm into thousands of one-row episodes and make the
        # false-alert denominator a measurement of ALERT_COOLDOWN_SECONDS.
        shadow_obs.append({
            "patient_id": pid,
            "kind": "alert.fired",
            "occurred_at": fired_at,
            "trigger": "news2_threshold",
            "episode_payload": {"level": risk_level, "raw_news2": raw_news2},
            "payload": {
                "level": risk_level,
                "raw_news2": raw_news2,
                "score": risk_score,
                "drivers": drivers[:6],
                "single_param_3": bool(risk.get("single_param_3")),
                "spo2_scale": risk.get("spo2_scale"),
                "repeat": pid in _alert_cooldowns,
            },
        })
        record_alert_dispatched(pid, risk_level, raw_news2, now)

    # SHADOW: one off-loop transaction for the whole cycle, BEFORE the alert
    # persist so the episode ids exist to thread into it. Fail-open — a failure
    # here returns {} and the alert path below is untouched.
    episodes = await shadow_service.arecord_batch(shadow_obs, "simulator")

    # SHADOW: Sage's contribution to this cycle's alerts, threaded onto the same
    # episodes. After arecord_batch because that is what mints them.
    if sage_drafts:
        await shadow_service.aemit_many([
            shadow_service.build(
                "sage.drafted", episodes[d["pid"]], "simulator",
                occurred_at=d["occurred_at"], patient_id=d["pid"],
                payload={
                    "conversation_id": d["conversation_id"],
                    # No retrieval ran and nothing was cited. Said explicitly —
                    # the same value the voice lane records — rather than left
                    # null, which reads as "not captured" instead of "none".
                    "citation_basis": "none",
                    "retrieved_doc_ids": [],
                    "cited_doc_ids": [],
                    "trust_tiers": [],
                    "query_class": "VIGIL_ALERT_RECOMMENDATION",
                    "cancelled": False,
                },
            )
            for d in sage_drafts if episodes.get(d["pid"])
        ])

    # Send alerts to Relay case rooms. All DB work happens off-loop in one
    # short transaction; socket emits and event-bus publishes happen back on
    # the loop afterwards (create_task/sio need the loop, and the old shape
    # held this write transaction open across an await per alert).
    if alerts_to_send:
        persisted = await run_db(_persist_alerts, alerts_to_send, episodes)

        for item in persisted:
            alert = item["alert"]
            # Publish event to LocalEventBus for reactive nervous system preloading.
            # Through `publish_vigil_critical` — the ONE publisher every lane
            # shares — so a nurse-entered deterioration wakes the same nervous
            # system this one does.
            publish_vigil_critical(
                alert["pid"],
                level=alert["risk_level"],
                score=alert["score"],
                drivers=alert["drivers"],
                message_id=item["msg_id"],
                raw_news2=alert.get("raw_news2"),
                lane="simulator",
            )

            if item["msg_data"]:
                await sio.emit("message:new", item["msg_data"], room=item["channel_id"])
                if item.get("episode_id"):
                    shadow_service.emit_bg(
                        "alert.delivered", item["episode_id"], "simulator",
                        patient_id=alert["pid"], subject_type="messages", subject_id=item["msg_id"],
                        payload={"channel_id": item["channel_id"], "transport": "socket"},
                    )
            logger.info("[Vigil Alert] %s: %s — alert sent to case room", item["pname"], alert['risk_level'].upper())


def get_dynamic_finding(db, pid, scan_type):
    """
    Generates a randomized clinical interpretation based on scan type.
    Returns (analysis_text, urgency_level)
    """
    latest_hr = 75
    vital = db.execute(
        "SELECT heart_rate FROM vitals WHERE patient_id = ? AND voided_at IS NULL "
        "ORDER BY timestamp DESC, id DESC LIMIT 1",
        (pid,)
    ).fetchone()
    if vital:
        latest_hr = int(vital["heart_rate"])

    templates = {
        "ECG": [
            (f"Sinus rhythm identified at {latest_hr} BPM. Normal axis. No ST segment deviation detected.", "info"),
            (f"Borderline Sinus Rhythm @ {latest_hr} BPM. Rare PVCs noted. Suggest clinical correlation.", "alert"),
            (f"Orb Vision detects Sinus Tachycardia ({latest_hr} BPM). No acute ischemic changes visible.", "alert" if latest_hr > 110 else "info"),
            (f"Baseline Normal ECG @ {latest_hr} BPM. Stable conduction intervals. No significant Q waves.", "info"),
            (f"Orb AI detects Sinus Rhythm ({latest_hr} BPM) with minor non-specific T-wave flattening.", "info")
        ],
        "XRAY": [
            ("AI analysis highlights clear lung fields. No focal consolidations or pneumothorax visible.", "info"),
            ("Evidence of mild interstitial prominence. No acute infiltrate. Pleural spaces are clear.", "info"),
            ("Orb Vision identifies normal pulmonary vascularity. Lungs are well-expanded.", "info"),
            ("Minimal bibasilar atelectasis noted. No pneumonia-like consolidations. Mediastinum is centered.", "info"),
            ("Orb Lens detects opacity in right lower lobe. Clinical correlation for atypical pneumonia suggested.", "alert")
        ],
        "MRI": [
            ("Orb Vision detects normal grey-white matter differentiation. No intracranial masses observed.", "info"),
            ("Stable MRI findings. No evidence of acute infarction or demyelination.", "info"),
            ("Slight non-specific white matter hyperintensities. No focal mass effect detected.", "info"),
            ("Normal intracranial flow voids. No acute intracranial process identified.", "info"),
            ("Orb AI detects small area of restricted diffusion in left parietal lobe. Suspected acute ischemic event.", "alert")
        ],
        "CT": [
            ("Analysis of abdominal slices reveals normal organ sizing. Liver and spleen unremarkable.", "info"),
            ("No evidence of bowel wall thickening. Appendix is seen and is normal.", "info"),
            ("Stable CT findings. No retroperitoneal lymphadenopathy. Bladder is normally distended.", "info"),
            ("Orb AI identifies fat stranding around the cecum. Findings highly suspicious for acute appendicitis.", "alert"),
            ("Minimal dependent atelectasis in lung bases. Upper abdominal organs are within normal limits.", "info")
        ]
    }

    options = templates.get(scan_type, [("No abnormal findings detected.", "info")])
    return random.choice(options)


def _synthetic_content_allowed() -> bool:
    """True when this deployment permits fabricated clinical content
    (ORB_DEMO_MODE). Read at call time so a test or an operator flip applies."""
    import config
    return bool(getattr(config, "DEMO_MODE", False))


# Module level, not a closure inside the async caller: this body runs on a
# run_db() worker thread, so every value it touches must arrive as an
# argument or a module global. Nesting it in the coroutine made it one
# edit away from silently capturing a loop-side local and reading it from
# another thread.
def _create_report(available):
    with db_session() as db:
        patients = db.execute("SELECT id FROM patients WHERE status = 'admitted'").fetchall()
        if not patients:
            return

        pid = random.choice(patients)["id"]
        scan = random.choice(available)
        analysis, urgency = get_dynamic_finding(db, pid, scan["type"])

        db.execute(
            "INSERT OR IGNORE INTO users (id, name, role, password_hash) VALUES (?, ?, ?, ?)",
            (LENS_BOT_ID, 'Orb AI Lens', 'doctor', 'system')
        )

        # source = 'simulated' (migration 46): a template finding is not a
        # read of any image of this patient, and every reader labels it so.
        db.execute(
            """INSERT INTO lens_analyses (
                patient_id, image_path, image_type, clinical_context, analysis, created_at, author_id,
                source
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                pid,
                f"/uploads/{scan['file']}",
                scan["type"],
                scan["context"],
                analysis,
                datetime.now(timezone.utc).isoformat(),
                LENS_BOT_ID,
                SIMULATED_SOURCE,
            )
        )

        channel = db.execute("SELECT id FROM channels WHERE type = 'case_room' AND patient_id = ?", (pid,)).fetchone()
        return pid, scan, analysis, urgency, channel


async def run_reports_bot():
    """
    Randomly generates a diagnostic report every ~few mins.
    """
    # Fabricated clinical CONTENT, so it runs only where ORB_DEMO_MODE permits
    # synthetic content (config.DEMO_MODE), as config says it does. It used to
    # be gated on ORB_INGESTION_MODE alone, so a hybrid real deployment would
    # have written template findings onto real charts.
    if not _synthetic_content_allowed():
        return
    mock_scans = [
        {"file": "mock_xray.png", "type": "XRAY", "context": "Routine Chest check"},
        {"file": "mock_mri.png", "type": "MRI", "context": "Brain scan due to neurological symptoms"},
        {"file": "mock_ct.png", "type": "CT", "context": "Abdominal CT for suspected appendicitis"},
        {"file": "mock_ecg.png", "type": "ECG", "context": "12-lead ECG for palpitations"},
    ]

    upload_dir = str(UPLOAD_DIR)
    if not os.path.exists(upload_dir):
        return

    available = [s for s in mock_scans if os.path.exists(os.path.join(upload_dir, s["file"]))]
    if not available:
        return

    # 0.2% chance per call to massively reduce frequency
    if random.random() > 0.002:
        return

    result = await run_db(_create_report, available)
    if result is None:
        return
    pid, scan, analysis, urgency, channel = result

    if channel:
        from services.relay_service import send_message
        from ws.realtime import sio

        # send_message opens its own db_session, so calling it inline put a
        # SQLite INSERT back on the loop right after we had just moved the
        # report write off it. It is pure sync (no asyncio), so run_db owns it.
        msg = await run_db(
            send_message,
            channel_id=channel["id"],
            sender_id=LENS_BOT_ID,
            sender_name="Orb Lens AI",
            sender_role="intelligence",
            content=f"Diagnostic Update: New {scan['type'].upper()} scan processed.\nFindings: {analysis}",
            message_type=urgency if urgency in ("alert", "system") else "text",
            # 'simulated', not 'bot': Relay badges the post as a simulator's.
            is_bot=True, source=SIMULATED_SOURCE,
        )
        await sio.emit("message:new", msg, room=channel["id"])

    logger.info("[Reports Bot] Generated %s for patient %s", scan['type'], pid)


# ── Lab Report Bot ──
_lab_cooldown: dict[str, float] = {}
LAB_COOLDOWN_SECONDS = 180  # 3 minutes between lab reports per patient

LAB_PANELS = {
    "critical": [
        {
            "panel": "CBC (STAT)",
            "generate": lambda: {
                "WBC": (random.uniform(14, 28), "x10³/μL", "[H]", "4.5-11.0"),
                "Hemoglobin": (random.uniform(7.5, 10.5), "g/dL", "[L]", "12.0-17.5"),
                "Platelets": (random.randint(45, 130), "x10³/μL", "[L]", "150-400"),
                "Neutrophils": (random.uniform(78, 92), "%", "[H]", "40-70"),
                "Bands": (random.randint(6, 18), "%", "[H] LEFT SHIFT", "0-5"),
            }
        },
        {
            "panel": "BMP (STAT)",
            "generate": lambda: {
                "Sodium": (random.randint(128, 148), "mEq/L", "", "136-145"),
                "Potassium": (random.uniform(5.2, 6.8), "mEq/L", "[H] ⚠️ CRITICAL", "3.5-5.0"),
                "BUN": (random.randint(35, 68), "mg/dL", "[H]", "7-20"),
                "Creatinine": (random.uniform(2.2, 5.5), "mg/dL", "[H]", "0.7-1.3"),
                "Lactate": (random.uniform(3.5, 8.2), "mmol/L", "[H] ⚠️ CRITICAL", "0.5-2.0"),
            }
        },
        {
            "panel": "ABG (STAT)",
            "generate": lambda: {
                "pH": (random.uniform(7.18, 7.32), "", "[L]", "7.35-7.45"),
                "pCO₂": (random.uniform(28, 48), "mmHg", "", "35-45"),
                "pO₂": (random.uniform(52, 75), "mmHg", "[L]", "80-100"),
                "HCO₃": (random.uniform(14, 20), "mEq/L", "[L]", "22-26"),
                "Lactate": (random.uniform(3.0, 7.0), "mmol/L", "[H]", "0.5-2.0"),
            }
        },
        {
            "panel": "Coagulation (STAT)",
            "generate": lambda: {
                "PT": (random.uniform(16, 24), "sec", "[H]", "11-13.5"),
                "INR": (random.uniform(1.5, 2.8), "", "[H]", "0.8-1.1"),
                "aPTT": (random.uniform(38, 65), "sec", "[H]", "25-35"),
                "D-Dimer": (random.uniform(2.5, 8.0), "μg/mL", "[H]", "<0.5"),
            }
        },
    ],
    "elevated": [
        {
            "panel": "CBC",
            "generate": lambda: {
                "WBC": (random.uniform(11, 16), "x10³/μL", "[H]", "4.5-11.0"),
                "Hemoglobin": (random.uniform(10.5, 13), "g/dL", "", "12.0-17.5"),
                "Platelets": (random.randint(140, 220), "x10³/μL", "", "150-400"),
                "CRP": (random.uniform(25, 85), "mg/L", "[H]", "<5"),
            }
        },
        {
            "panel": "BMP",
            "generate": lambda: {
                "Sodium": (random.randint(132, 143), "mEq/L", "", "136-145"),
                "Potassium": (random.uniform(3.6, 5.2), "mEq/L", "", "3.5-5.0"),
                "Creatinine": (random.uniform(1.0, 2.0), "mg/dL", "[H]", "0.7-1.3"),
                "Glucose": (random.randint(85, 180), "mg/dL", "", "70-100"),
            }
        },
    ],
    "stable": [
        {
            "panel": "CBC (Routine)",
            "generate": lambda: {
                "WBC": (random.uniform(5.5, 10), "x10³/μL", "", "4.5-11.0"),
                "Hemoglobin": (random.uniform(12, 15.5), "g/dL", "", "12.0-17.5"),
                "Platelets": (random.randint(180, 350), "x10³/μL", "", "150-400"),
            }
        },
    ],
}


# Module level, not a closure inside the async caller: this body runs on a
# run_db() worker thread, so every value it touches must arrive as an
# argument or a module global. Nesting it in the coroutine made it one
# edit away from silently capturing a loop-side local and reading it from
# another thread.
def _generate_lab_result(now):
    with db_session() as db:
        patients = db.execute(
            """SELECT p.id, p.name, p.ward, p.diagnosis, rs.level
               FROM patients p
               -- The LATEST risk row only. A plain join on patient_id returned
               -- one row per stored score, so a patient's lab odds grew with
               -- their history and the weight came from an arbitrary row.
               LEFT JOIN risk_scores rs ON rs.id = (
                   SELECT rs2.id FROM risk_scores rs2 WHERE rs2.patient_id = p.id
                   ORDER BY rs2.timestamp DESC, rs2.id DESC LIMIT 1)
               WHERE p.status = 'admitted'
               ORDER BY rs.timestamp DESC""",
        ).fetchall()

        if not patients:
            return

        # Filter out patients with recent lab results
        eligible = [p for p in patients if now - _lab_cooldown.get(p["id"], 0) > LAB_COOLDOWN_SECONDS]
        if not eligible:
            return

        # Weighted selection — critical patients get labs more often
        weights = []
        for p in eligible:
            level = p["level"] or "stable"
            weights.append(3 if level == "critical" else 2 if level == "elevated" else 1)

        patient = random.choices(eligible, weights=weights, k=1)[0]
        pid = patient["id"]
        pname = patient["name"]
        severity = _get_patient_severity(db, pid)
        diag = patient["diagnosis"] or ""

        # Try condition-specific panels from seed_labs first
        panel_cfg = None
        try:
            from seed_labs import DIAGNOSIS_LAB_SCHEDULE
            diag_lower = diag.lower()
            for keyword, schedule in DIAGNOSIS_LAB_SCHEDULE.items():
                if keyword.lower() in diag_lower:
                    # Pick a random panel from the schedule
                    _, panel_gen = random.choice(schedule)
                    panel_data = panel_gen()
                    panel_cfg = {"panel": panel_data["panel"], "generate": lambda pd=panel_data: pd["results"]}
                    break
        except Exception:
            pass

        # Fallback: severity-based panels
        if not panel_cfg:
            panels = LAB_PANELS.get(severity, LAB_PANELS["stable"])
            panel_cfg = random.choice(panels)
        results = panel_cfg["generate"]()

        # Format the lab result
        lines = [f"🔬 LAB RESULT: {panel_cfg['panel']}", f"Patient: {pname}", ""]
        has_critical = False
        for name, (val, unit, flag, ref) in results.items():
            val_str = f"{val:.1f}" if isinstance(val, float) else str(val)
            line = f"  {name}: {val_str} {unit}  {flag}  (ref: {ref})"
            lines.append(line)
            if "CRITICAL" in flag:
                has_critical = True

        if has_critical:
            lines.append("")
            lines.append("⚠️ CRITICAL VALUES: Immediate physician notification required")

        content = "\n".join(lines)

        # Store as clinical document
        db.execute(
            "INSERT OR IGNORE INTO users (id, name, role, password_hash) VALUES (?, ?, ?, ?)",
            (LAB_BOT_ID, 'Orb Lab System', 'doctor', 'system')
        )
        # source = 'simulated' (migration 46) on both copies: the chart labels
        # it, Sage prefixes it, and the Tier 1 renal rules never flag off it.
        db.execute(
            """INSERT INTO clinical_documents (patient_id, doc_type, title, content, author_id, source)
               VALUES (?, 'lab_result', ?, ?, ?, ?)""",
            (pid, panel_cfg["panel"], content, LAB_BOT_ID, SIMULATED_SOURCE)
        )

        # Also store in lens_analyses so the patient timeline picks it up
        db.execute(
            """INSERT INTO lens_analyses (patient_id, image_path, image_type, clinical_context, analysis, author_id, created_at,
                                          source)
               VALUES (?, '', 'lab', ?, ?, ?, ?, ?)""",
            (pid, f"Lab: {panel_cfg['panel']}", content, LAB_BOT_ID,
             datetime.now(timezone.utc).isoformat(), SIMULATED_SOURCE)
        )

        # Post to Relay case room
        channel = db.execute(
            "SELECT id FROM channels WHERE type = 'case_room' AND patient_id = ?", (pid,)
        ).fetchone()
        return pid, pname, panel_cfg, has_critical, content, channel


async def run_lab_bot():
    """
    Simulates automated lab result delivery for admitted patients.
    # Runs every 5s tick, massively reduced frequency
    """
    # Fabricated results: ORB_DEMO_MODE only (see run_reports_bot).
    if not _synthetic_content_allowed():
        return
    if random.random() > 0.002:
        return

    now = time.time()

    result = await run_db(_generate_lab_result, now)
    if result is None:
        return
    pid, pname, panel_cfg, has_critical, content, channel = result

    # _generate_lab_result READ _lab_cooldown to pick an eligible patient and we
    # WRITE it here, after an await — a read-modify-write straddling a
    # suspension point. It is safe only because main.py drives the three bots
    # from one serial `while True` task, so a second lab tick cannot be in
    # flight while this one is parked. Keep it that way: if the bots are ever
    # dispatched concurrently, two ticks could select the same patient before
    # either stamped the cooldown, and this write must move inside
    # _generate_lab_result (one executor call = atomic) instead.
    _lab_cooldown[pid] = now

    if channel:
        from services.relay_service import send_message
        from ws.realtime import sio

        # Off the loop for the same reason as the reports bot: send_message
        # opens its own db_session.
        msg = await run_db(
            send_message,
            channel_id=channel["id"],
            sender_id=LAB_BOT_ID,
            sender_name="Orb Lab System",
            sender_role="intelligence",
            content=content,
            message_type="alert" if has_critical else "system",
            # 'simulated', not 'bot': Relay badges the post as a simulator's.
            is_bot=True, source=SIMULATED_SOURCE,
        )
        await sio.emit("message:new", msg, room=channel["id"])

    logger.info("[Lab Bot] %s for %s %s", panel_cfg['panel'], pname, '⚠️ CRITICAL' if has_critical else '✓')

