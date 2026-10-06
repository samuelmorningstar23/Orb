"""Orb Forecast — Runtime Inference Service

Provides:
  - Per-patient LoS prediction via LSTM
  - Hospital-level flow forecasting (admissions, discharges, occupancy)
  - Bed capacity projections by ward
  - Discharge readiness scoring
  - Season context from Pulse's built-in calendar (shown beside the numbers,
    never multiplied into them)

Uses cached predictions (refreshed by bot every 60s) for fast API responses.
Falls back to real-time inference if cache is stale.
"""

import json
import logging
import os
import math
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import torch

logger = logging.getLogger("orb.forecast")

from database import get_db, db_session
from models.forecast_lstm import OrbForecastLSTM, load_model, INPUT_DIM
from services.hospital_time import hospital_today, local_day_bounds_utc

# ── Singleton model instance ──
_model: OrbForecastLSTM | None = None

# ── Caches ──
# Keyed by (days_ahead, include_current_day, hospital-local date). A single
# unkeyed slot was three bugs in one: the Command Center's days_ahead=1 call
# served its one-day series to the panel's seven-day request for the next hour
# (and vice versa), an include_current_day=True result leaked to callers that
# index the series positionally, and a result computed at 23:50 local kept
# reporting YESTERDAY as day 0 until 00:50.
_hospital_flow_cache: dict[tuple, tuple[float, dict]] = {}
HOSPITAL_FLOW_CACHE_TTL = 3600  # 1 hour — forecasts are stable for 1 hour


def get_model() -> OrbForecastLSTM:
    """Lazy-load the trained LSTM model."""
    global _model
    if _model is None:
        # Tiny model, millisecond inference — cap torch's intra-op pool so it
        # never spawns a per-core OpenMP gang inside the API process.
        try:
            import torch
            torch.set_num_threads(1)
        except Exception:
            pass
        _model = load_model()
    return _model


# ── Ward capacity (beds per ward) ─────────────────────────────────────────────
# These four names are a FALLBACK, not this hospital's wards. The Command Center
# heatmap read `W1 30/8` — thirty patients in an eight-bed ward — because the
# real wards (GM-1, CCU, RCU, NW-1, OW-1, SW-1) match none of these, and
# `_normalize_ward` funnelled every unrecognised name into "W1". So the whole
# census landed in one fictional ward and blew straight past a fictional cap.
#
# Ward NAMES are now discovered from the site (config if declared, otherwise the
# wards patients are actually admitted to). Bed COUNT is a site setting: how many
# beds a ward has is a fact about the building, not something Orb can infer —
# occupancy would only ever tell us how many are full, which would peg every ward
# at 100%. Set ORB_WARD_BEDS per deployment.
DEFAULT_WARD_BEDS = int(os.getenv("ORB_WARD_BEDS", "12"))

WARD_CAPACITY: dict[str, int] = {
    "ICU": DEFAULT_WARD_BEDS,
    "W1": DEFAULT_WARD_BEDS,
    "W2": DEFAULT_WARD_BEDS,
    "W3": DEFAULT_WARD_BEDS,
}
TOTAL_CAPACITY = sum(WARD_CAPACITY.values())


def refresh_ward_capacity() -> dict[str, int]:
    """Rebuild WARD_CAPACITY from the site's real wards.

    Mutates the module dict IN PLACE and rebinds TOTAL_CAPACITY, so callers that
    did `from forecast_service import WARD_CAPACITY` keep a valid reference —
    command_center_service imports it that way, and its tests assert the API and
    this constant agree.

    Order of truth: declared site config → wards patients are admitted to →
    the fallback above. Never raises; a discovery failure leaves the previous
    map in place, because a Command Center with stale ward names is a smaller
    problem than one that will not load.
    """
    global TOTAL_CAPACITY
    wards: list[str] = []
    try:
        from services.site_config_service import current_site_config
        wards = [w for w in (current_site_config().wards or []) if w]
    except Exception:
        wards = []

    if not wards:
        try:
            from database import db_session
            with db_session() as db:
                wards = [
                    r["ward"] for r in db.execute(
                        "SELECT DISTINCT ward FROM patients "
                        "WHERE status = 'admitted' AND ward IS NOT NULL AND ward <> '' "
                        "ORDER BY ward"
                    ).fetchall()
                ]
        except Exception as e:  # noqa: BLE001
            logger.debug("ward discovery failed, keeping previous capacity map: %s", e)
            return WARD_CAPACITY

    if not wards:
        return WARD_CAPACITY

    WARD_CAPACITY.clear()
    for w in wards:
        WARD_CAPACITY[w] = DEFAULT_WARD_BEDS
    TOTAL_CAPACITY = sum(WARD_CAPACITY.values())
    logger.info(
        "Ward capacity: %d ward(s) x %d beds = %d (%s)",
        len(WARD_CAPACITY), DEFAULT_WARD_BEDS, TOTAL_CAPACITY,
        ", ".join(sorted(WARD_CAPACITY)),
    )
    return WARD_CAPACITY

# Day-of-week admission multipliers (Indian hospital patterns)
DOW_MULTIPLIERS = {0: 1.35, 1: 1.25, 2: 1.10, 3: 1.00, 4: 0.95, 5: 0.70, 6: 0.65}


# There is no weather, air-quality or flu multiplier. Pulse used to feed live
# internet data in here and admissions were scaled by hand-picked guesses
# (+20% for a thunderstorm, +35% for a flu surge). Orb is strictly offline, and
# ruled 6 October 2026: the season is shown as context only (`season` in
# predict_hospital_flow), with NO numeric seasonal or weather adjustment.


# ══════════════════════════════════════════════════════════════════════
# FEATURE EXTRACTION
# ══════════════════════════════════════════════════════════════════════

def extract_patient_features(patient_id: str, seq_len: int = 48) -> torch.Tensor | None:
    """
    Extract LSTM input features for a patient from the database.

    Returns tensor of shape (1, seq_len, 19) or None if insufficient data.
    """
    with db_session() as db:
        # Patient demographics
        patient = db.execute(
            "SELECT * FROM patients WHERE id = ?", (patient_id,)
        ).fetchone()
        if not patient:
            return None

        # Recent vitals (up to seq_len * 5min intervals ≈ 4 hours)
        vitals = db.execute(
            """SELECT heart_rate, systolic_bp, diastolic_bp, spo2, temperature,
                      respiratory_rate, timestamp
               FROM vitals WHERE patient_id = ?
               ORDER BY timestamp DESC, id DESC LIMIT ?""",
            (patient_id, seq_len),
        ).fetchall()

        if len(vitals) < 3:
            return None

        # Latest risk score
        risk = db.execute(
            "SELECT score, level FROM risk_scores WHERE patient_id = ? ORDER BY timestamp DESC, id DESC LIMIT 1",
            (patient_id,),
        ).fetchone()

        # Medication count
        med_count = db.execute(
            "SELECT COUNT(*) as cnt FROM medications WHERE patient_id = ? AND status = 'active'",
            (patient_id,),
        ).fetchone()

    # Build feature tensor
    features = np.zeros((seq_len, INPUT_DIM))

    # Reverse vitals so oldest is first (LSTM expects chronological order)
    vitals_list = list(reversed(vitals))

    # Pad or truncate to seq_len
    n_vitals = len(vitals_list)

    for t in range(seq_len):
        # Use the closest available vital
        v_idx = min(t, n_vitals - 1) if t < n_vitals else n_vitals - 1
        v = vitals_list[v_idx]

        features[t, 0] = v["heart_rate"] or 80.0
        features[t, 1] = v["systolic_bp"] or 120.0
        features[t, 2] = v["diastolic_bp"] or 75.0
        features[t, 3] = v["spo2"] or 97.0
        features[t, 4] = v["temperature"] or 37.0
        features[t, 5] = v["respiratory_rate"] or 16.0

        # Static features
        features[t, 6] = patient["age"] or 50
        features[t, 7] = 1.0 if patient["sex"] == "M" else 0.0
        features[t, 8] = 1.0 if patient["sex"] == "F" else 0.0

        ward = (patient["ward"] or "").upper()
        features[t, 9]  = 1.0 if ward == "ICU" else 0.0
        features[t, 10] = 1.0 if ward in ("W1", "WARD 1") else 0.0
        features[t, 11] = 1.0 if ward in ("W2", "WARD 2") else 0.0
        features[t, 12] = 1.0 if ward in ("W3", "WARD 3") else 0.0

        # Severity from risk score
        risk_level = risk["level"] if risk else "stable"
        features[t, 13] = 1.0 if risk_level == "critical" else 0.0
        features[t, 14] = 1.0 if risk_level == "elevated" else 0.0
        features[t, 15] = 1.0 if risk_level == "stable" else 0.0

        # Days since admission
        if patient["admission_date"]:
            try:
                adm = datetime.fromisoformat(patient["admission_date"].replace("Z", "+00:00"))
                days = (datetime.now(timezone.utc) - adm).total_seconds() / 86400
            except (ValueError, TypeError):
                days = 3.0
        else:
            days = 3.0
        features[t, 16] = days

        # Medication count
        features[t, 17] = (med_count["cnt"] if med_count else 3)

        # Risk score
        features[t, 18] = risk["score"] if risk else 0.5

    return torch.tensor(features, dtype=torch.float32).unsqueeze(0)  # (1, seq_len, 19)


# ══════════════════════════════════════════════════════════════════════
# PER-PATIENT PREDICTIONS
# ══════════════════════════════════════════════════════════════════════

def predict_patient_los(patient_id: str) -> dict:
    """
    Predict length-of-stay and discharge probability for a single patient.
    Uses LSTM inference with features extracted from the database.
    """
    model = get_model()

    # FAIL CLOSED on the untrained network: until a validated checkpoint is
    # loaded the LSTM is at random init and model.predict() would emit/persist
    # meaningless LOS/discharge/acuity numbers that the UI presents as neural
    # predictions. Route to the transparent heuristic instead (it is clearly
    # labelled lower-confidence and rule-based), and tag the source so callers /
    # UI never attribute these to a "trained neural network".
    if not getattr(model, "trained", False):
        result = _heuristic_prediction(patient_id)
        result["model_trained"] = False
        result["prediction_source"] = "heuristic"
        _cache_prediction(patient_id, result)
        return result

    features = extract_patient_features(patient_id)

    if features is None:
        # Fallback: heuristic prediction based on ward and days admitted
        result = _heuristic_prediction(patient_id)
        result["model_trained"] = True
        result["prediction_source"] = "heuristic"
        _cache_prediction(patient_id, result)
        return result

    from services.gpu_coordinator import vram_guard

    with vram_guard("Forecast LSTM"):
        result = model.predict(features)
    result["prediction_source"] = "lstm"

    # Add estimated discharge date
    now = datetime.now(timezone.utc)
    est_discharge = now + timedelta(days=result["predicted_los_days"])
    result["estimated_discharge_date"] = est_discharge.strftime("%Y-%m-%d")

    # Cache the prediction
    _cache_prediction(patient_id, result)

    return result


def _heuristic_prediction(patient_id: str) -> dict:
    """Fallback prediction when LSTM features are insufficient."""
    with db_session() as db:
        patient = db.execute(
            "SELECT ward, admission_date FROM patients WHERE id = ?",
            (patient_id,),
        ).fetchone()

    if not patient:
        return {
            "predicted_los_days": 3.0,
            "discharge_probability": 0.15,
            "acuity_trajectory": "stable",
            "confidence": 0.3,
            "estimated_discharge_date": (datetime.now(timezone.utc) + timedelta(days=3)).strftime("%Y-%m-%d"),
        }

    ward = (patient["ward"] or "").upper()
    base_los = {"ICU": 7.0, "W1": 4.0, "W2": 3.0, "W3": 3.5}.get(ward, 4.0)

    # Adjust for days already admitted
    if patient["admission_date"]:
        try:
            adm = datetime.fromisoformat(patient["admission_date"].replace("Z", "+00:00"))
            days_in = (datetime.now(timezone.utc) - adm).total_seconds() / 86400
        except (ValueError, TypeError):
            days_in = 2.0
    else:
        days_in = 2.0

    remaining = max(0.5, base_los - days_in)
    discharge_prob = min(0.9, 1.0 / (remaining + 0.5))

    return {
        "predicted_los_days": round(remaining, 1),
        "discharge_probability": round(discharge_prob, 3),
        "acuity_trajectory": "stable",
        "confidence": 0.45,
        "estimated_discharge_date": (datetime.now(timezone.utc) + timedelta(days=remaining)).strftime("%Y-%m-%d"),
    }


def _cache_prediction(patient_id: str, result: dict):
    """Write prediction to forecast_predictions table."""
    try:
        with db_session() as db:
            db.execute(
                """INSERT INTO forecast_predictions 
                   (patient_id, predicted_los_days, discharge_probability, 
                    acuity_trajectory, confidence, feature_snapshot)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    patient_id,
                    result["predicted_los_days"],
                    result["discharge_probability"],
                    result["acuity_trajectory"],
                    result["confidence"],
                    json.dumps({
                        "estimated_discharge_date": result.get("estimated_discharge_date"),
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    }),
                ),
            )
    except Exception as e:
        logger.error("Cache write failed: %s", e)


# ══════════════════════════════════════════════════════════════════════
# HOSPITAL-LEVEL FORECASTING
# ══════════════════════════════════════════════════════════════════════

def _season_context(today) -> list[dict]:
    """[{id, title, kind, in_peak}] for today's active seasonal entries."""
    # Imported here so the forecast never depends on the calendar loading.
    from services.seasonal_calendar import season_context

    return season_context(today)


def _day_label(d) -> str:
    """"Thu 1 Oct": day before month, unpadded, as the rest of Orb writes a date.
    "%a %b %d" gave the US "Thu Oct 01". `date` beside it stays ISO for code."""
    return f"{d.strftime('%a')} {d.day} {d.strftime('%b')}"


def predict_hospital_flow(days_ahead: int = 7, include_current_day: bool = False) -> dict:
    """
    Aggregate per-patient predictions into hospital-level flow forecasts.

    Returns a `days_ahead`-day forecast with admissions, discharges, and
    occupancy per day. Results are cached for 1 hour to prevent constant UI
    flickering.

    `include_current_day` prepends a MEASURED day-0 entry (`is_current: True`):
    today's census and the admissions/discharges that have actually happened so
    far today. Without it the series starts at TOMORROW, so the panel's first
    chart point silently contradicted the Current Census card next to it.

    It is opt-in because existing callers index `daily_forecasts` POSITIONALLY —
    command_center_service reads `[0]` as "the next 24 hours" — and a measured
    day 0 in that slot would relabel today's actuals as a 24h projection.

    All day keys are HOSPITAL-LOCAL (services/hospital_time.py). They were UTC,
    which in a UTC+05:30 hospital names the wrong day until 05:30 every morning
    and never matches the date key the browser computes for itself.
    """
    today_local = hospital_today()
    cache_key = (days_ahead, include_current_day, today_local.strftime("%Y-%m-%d"))

    # Return cached result if still fresh
    hit = _hospital_flow_cache.get(cache_key)
    if hit is not None and (time.time() - hit[0]) < HOSPITAL_FLOW_CACHE_TTL:
        return hit[1]

    # Get all admitted patients
    with db_session() as db:
        patients = db.execute(
            "SELECT id, ward FROM patients WHERE status = 'admitted'"
        ).fetchall()

        # Current occupancy by ward, keyed the SAME WAY the breakdown below reads
        # it — through `_normalize_ward`, which is the one derivation.
        #
        # THIS BLOCK USED TO INLINE THE OLD HEURISTIC (2026-08-13). When
        # `_normalize_ward` was fixed to pass a known ward through unchanged, this
        # copy of the pre-fix logic was missed, so it kept folding every ward whose
        # label contains a digit into the fictional key "W1". The breakdown loop
        # (`ward_counts.get(w)` over the REAL WARD_CAPACITY keys) then found
        # nothing for GM-1, NW-1, OW-1 and SW-1 and printed 0 for four of six
        # wards, for all seven days — while /api/forecast/capacity, built on
        # `_normalize_ward`, correctly reported 5 in each of them. Two screens off
        # the same server disagreeing about how many patients are in a ward is the
        # failure the row-identity comment further down was written about; this is
        # the same defect one function earlier.
        ward_counts: dict[str, int] = {}
        for p in patients:
            w = _normalize_ward(p["ward"])
            ward_counts[w] = ward_counts.get(w, 0) + 1

        # Get admission history for Poisson rate estimation
        history = db.execute(
            """SELECT admission_date, COUNT(*) as cnt
               FROM admission_history
               WHERE admission_date >= date('now', '-30 days')
               GROUP BY admission_date"""
        ).fetchall()

        # What has actually happened so far TODAY — the day-0 row reports
        # observations, never model output.
        admissions_today = discharges_today = 0
        if include_current_day:
            day_start, day_end = local_day_bounds_utc(today_local)
            start_s = day_start.strftime("%Y-%m-%d %H:%M:%S")
            end_s = day_end.strftime("%Y-%m-%d %H:%M:%S")
            # Both columns hold UTC in two different textual shapes
            # (`admissions.created_at` is an ISO string carrying +00:00,
            # `discharge_summaries.discharged_at` is SQLite CURRENT_TIMESTAMP);
            # datetime() normalises both before the comparison. The bounds are a
            # UTC WINDOW, not a date: a hospital-local day straddles two UTC
            # dates everywhere east or west of Greenwich.
            admissions_today = db.execute(
                "SELECT COUNT(*) AS c FROM admissions "
                "WHERE datetime(created_at) >= ? AND datetime(created_at) < ?",
                (start_s, end_s),
            ).fetchone()["c"]
            # discharge_summaries is the Orb-native discharge EVENT record —
            # patients.status carries no timestamp, and admissions.discharged_at
            # is populated only for imported history (see migrations _m15).
            discharges_today = db.execute(
                "SELECT COUNT(*) AS c FROM discharge_summaries "
                "WHERE datetime(discharged_at) >= ? AND datetime(discharged_at) < ?",
                (start_s, end_s),
            ).fetchone()["c"]

    n_patients = len(patients)

    # Fit admission rate from history
    if history:
        daily_rates = [h["cnt"] for h in history]
        avg_admission_rate = sum(daily_rates) / len(daily_rates)
    else:
        avg_admission_rate = 4.5  # Default Indian small hospital

    # Get per-patient LoS predictions (use cache or compute)
    patient_predictions = {}
    for p in patients:
        pred = _get_cached_or_predict(p["id"])
        patient_predictions[p["id"]] = pred

    # Build daily forecasts
    daily_forecasts = []
    running_occupancy = n_patients

    for day in range(days_ahead):
        future_date = today_local + timedelta(days=day + 1)
        dow = future_date.weekday()

        # Predicted admissions: base_rate x day_of_week. Nothing seasonal or
        # weather-driven multiplies it (see the note above FEATURE EXTRACTION).
        adjusted_rate = avg_admission_rate * DOW_MULTIPLIERS.get(dow, 1.0)
        # A forecast should report the EXPECTED value, not a single random Poisson
        # draw — drawing once made the number jump discontinuously each time the
        # 1h cache expired (no clinical/operational meaning). The Poisson mean IS
        # the expected admission count for the day.
        predicted_admissions = round(adjusted_rate, 1)

        # Predicted discharges (from LoS predictions)
        predicted_discharges = 0
        for pid, pred in patient_predictions.items():
            remaining = pred.get("predicted_los_days", 5.0)
            if remaining <= (day + 1) and remaining > day:
                predicted_discharges += 1

        # DEMO PLACEHOLDER bounds: the n_patients//3 cap and ~8% base discharge
        # floor are heuristic smoothing, NOT model outputs. They keep the demo
        # curve plausible when LoS predictions are sparse; replace with real
        # discharge-event modelling once a validated forecast checkpoint exists.
        predicted_discharges = max(0, min(predicted_discharges, n_patients // 3))
        if predicted_discharges == 0 and day > 0:
            predicted_discharges = max(1, int(n_patients * 0.08))  # Base discharge rate ~8%

        # Occupancy.
        #
        # ROUND THE ADMISSIONS BEFORE ACCUMULATING (2026-08-12). This loop used to
        # carry `running_occupancy` as a FLOAT and round it and `predicted_admissions`
        # INDEPENDENTLY at render time, so the three numbers printed on a row did not
        # reconcile: 3 of 7 rows failed `previous + admissions - discharges` and the
        # occupancy percentage never equalled census/capacity. Found on screen, not by
        # a test — a bed manager reading a row that does not add up stops trusting the
        # whole board, and the same figures resurface on the Command Center.
        #
        # The number we DISPLAY is now the number we CARRY, so the identity holds
        # exactly, except where the capacity clamp below deliberately bites.
        predicted_admissions = int(round(predicted_admissions))
        running_occupancy = int(running_occupancy) - predicted_discharges + predicted_admissions
        running_occupancy = max(0, min(running_occupancy, TOTAL_CAPACITY + 5))

        # Ward breakdown (proportional to current distribution)
        ward_breakdown = {}
        for w, cap in WARD_CAPACITY.items():
            current = ward_counts.get(w, 0)
            ratio = current / max(1, n_patients)
            projected = round(running_occupancy * ratio)
            ward_breakdown[w] = min(projected, cap + 1)

        daily_forecasts.append({
            "date": future_date.strftime("%Y-%m-%d"),
            "day_label": _day_label(future_date),
            "predicted_admissions": predicted_admissions,
            "predicted_discharges": predicted_discharges,
            "predicted_occupancy": running_occupancy,
            "occupancy_pct": round(running_occupancy / TOTAL_CAPACITY * 100, 1),
            "ward_breakdown": ward_breakdown,
        })

    # ── Day 0: measured, not predicted ────────────────────────────────────────
    # Today's census and today's actual admissions/discharges so far. It carries
    # `is_current` so a client can recognise it WITHOUT comparing date strings:
    # the client's own key comes from the browser's local date, and a viewer in
    # a different zone from the appliance (or an appliance whose zone is not the
    # ward's) would silently fail the equality test and render two "today"s.
    series = daily_forecasts
    if include_current_day:
        series = [{
            "date": today_local.strftime("%Y-%m-%d"),
            "day_label": _day_label(today_local),
            "is_current": True,
            # The honest names. These are observations.
            "actual_admissions": admissions_today,
            "actual_discharges": discharges_today,
            "measured_occupancy": n_patients,
            # The same three numbers under the series' own keys, so every
            # consumer that walks daily_forecasts plots a continuous curve
            # instead of a hole. `is_current` is what says they are measured.
            "predicted_admissions": admissions_today,
            "predicted_discharges": discharges_today,
            "predicted_occupancy": n_patients,
            "occupancy_pct": round(n_patients / TOTAL_CAPACITY * 100, 1),
            "ward_breakdown": {w: ward_counts.get(w, 0) for w in WARD_CAPACITY},
        }, *daily_forecasts]

    # Summary statistics — over the PREDICTED days only. A measured day 0 must
    # not move the peak, the averages or the 7-day totals, and
    # `beds_available_in_48h` has to keep indexing the second PREDICTED day.
    total_discharges = sum(d["predicted_discharges"] for d in daily_forecasts)
    peak_day = max(daily_forecasts, key=lambda d: d["predicted_occupancy"])
    avg_occupancy = sum(d["predicted_occupancy"] for d in daily_forecasts) / len(daily_forecasts)

    result = {
        "current_occupancy": n_patients,
        "total_capacity": TOTAL_CAPACITY,
        "current_occupancy_pct": round(n_patients / TOTAL_CAPACITY * 100, 1),
        "daily_forecasts": series,
        # Today's entries from Pulse's seasonal calendar for this hospital's
        # state: CONTEXT for the reader, never an input to any number above.
        # Empty when no state is set, the calendar is unusable, or none apply.
        "season": _season_context(today_local),
        "summary": {
            "avg_occupancy": round(avg_occupancy),
            "peak_day": peak_day["day_label"],
            "peak_occupancy": peak_day["predicted_occupancy"],
            "total_predicted_discharges_7d": total_discharges,
            "total_predicted_admissions_7d": sum(d["predicted_admissions"] for d in daily_forecasts),
            "beds_available_in_48h": max(0, TOTAL_CAPACITY - daily_forecasts[1]["predicted_occupancy"]) if len(daily_forecasts) > 1 else 0,
        },
    }

    # Cache the result. Entries computed for a different hospital-local day are
    # dropped rather than left to expire: a day-0 row is a census with a date on
    # it, and serving yesterday's for another hour is a wrong number on a
    # clinical surface, not a stale one.
    for stale in [k for k in _hospital_flow_cache if k[2] != cache_key[2]]:
        del _hospital_flow_cache[stale]
    _hospital_flow_cache[cache_key] = (time.time(), result)

    return result


def get_bed_capacity_forecast() -> dict:
    """Current + projected bed utilization by ward."""
    with db_session() as db:
        patients = db.execute(
            "SELECT id, ward FROM patients WHERE status = 'admitted'"
        ).fetchall()

    # Current occupancy
    current = {}
    for w, cap in WARD_CAPACITY.items():
        count = sum(
            1 for p in patients
            if _normalize_ward(p["ward"]) == w
        )
        current[w] = {
            "total": cap,
            "occupied": count,
            "available": max(0, cap - count),
            "utilization_pct": round(count / cap * 100, 1),
        }

    # 48h projection (using LoS predictions)
    projected_48h = {}
    discharge_count_by_ward = {}

    for p in patients:
        w = _normalize_ward(p["ward"])
        pred = _get_cached_or_predict(p["id"])
        if pred.get("predicted_los_days", 10) <= 2.0:
            discharge_count_by_ward[w] = discharge_count_by_ward.get(w, 0) + 1

    for w, cap in WARD_CAPACITY.items():
        curr = current[w]["occupied"]
        discharges = discharge_count_by_ward.get(w, 0)
        projected = max(0, curr - discharges)
        projected_48h[w] = {
            "projected_occupied": projected,
            "projected_available": max(0, cap - projected),
            "projected_utilization_pct": round(projected / cap * 100, 1),
        }

    return {
        "current": current,
        "projected_48h": projected_48h,
        "total": {
            "capacity": TOTAL_CAPACITY,
            "occupied": sum(c["occupied"] for c in current.values()),
            "available": sum(c["available"] for c in current.values()),
        },
    }


def get_discharge_board() -> list:
    """
    Get patients sorted by discharge likelihood (next 72 hours).
    For the Discharge Readiness Board.
    """
    with db_session() as db:
        patients = db.execute(
            """SELECT p.id, p.name, p.ward, p.bed, p.diagnosis, p.age, p.sex,
                      p.admission_date
               FROM patients p WHERE p.status = 'admitted'"""
        ).fetchall()

    board = []
    now = datetime.now(timezone.utc)

    for p in patients:
        pred = _get_cached_or_predict(p["id"])

        # Calculate days in hospital
        if p["admission_date"]:
            try:
                adm = datetime.fromisoformat(str(p["admission_date"]).replace("Z", "+00:00"))
                days_in = (now - adm).total_seconds() / 86400
            except (ValueError, TypeError):
                days_in = 3.0
        else:
            days_in = 3.0

        board.append({
            "patient_id": p["id"],
            "name": p["name"],
            "ward": p["ward"],
            "bed": p["bed"],
            "diagnosis": p["diagnosis"],
            "age": p["age"],
            "sex": p["sex"],
            "days_admitted": round(days_in, 1),
            "predicted_remaining_days": pred["predicted_los_days"],
            "discharge_probability": pred["discharge_probability"],
            "acuity_trajectory": pred["acuity_trajectory"],
            "confidence": pred["confidence"],
            "estimated_discharge_date": pred.get("estimated_discharge_date", ""),
        })

    # Sort by discharge probability (highest first)
    board.sort(key=lambda x: x["discharge_probability"], reverse=True)

    return board


def get_admission_patterns() -> dict:
    """
    Analyze admission history for weekly patterns.
    Returns 7×24 heatmap data + daily summary.
    """
    with db_session() as db:
        history = db.execute(
            """SELECT admission_date, patient_name, ward, severity, 
                      length_of_stay_days, discharged
               FROM admission_history 
               ORDER BY admission_date DESC"""
        ).fetchall()

    if not history:
        return {
            "daily_summary": [{"day": d, "avg_admissions": 0, "total": 0} for d in ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]],
            "total_30d": 0,
            "avg_daily": 0,
            "avg_los": 0,
            "severity_distribution": {"critical": 0, "elevated": 0, "stable": 0},
            "ward_distribution": {"ICU": 0, "W1": 0, "W2": 0, "W3": 0},
            "discharge_rate": 0,
        }

    # Daily admission counts
    daily_counts = {}
    severity_counts = {"critical": 0, "elevated": 0, "stable": 0}
    ward_counts = {"ICU": 0, "W1": 0, "W2": 0, "W3": 0}
    total_los = 0
    los_count = 0

    for h in history:
        date_str = h["admission_date"]
        daily_counts[date_str] = daily_counts.get(date_str, 0) + 1

        sev = h["severity"] or "stable"
        if sev in severity_counts:
            severity_counts[sev] += 1

        w = _normalize_ward(h["ward"])
        if w in ward_counts:
            ward_counts[w] += 1

        if h["length_of_stay_days"]:
            total_los += h["length_of_stay_days"]
            los_count += 1

    # Build day-of-week summary
    dow_totals = {i: 0 for i in range(7)}
    dow_counts_n = {i: 0 for i in range(7)}
    for date_str, cnt in daily_counts.items():
        try:
            d = datetime.strptime(date_str, "%Y-%m-%d")
            dow = d.weekday()
            dow_totals[dow] += cnt
            dow_counts_n[dow] += 1
        except ValueError:
            pass

    dow_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    daily_summary = []
    for i in range(7):
        avg = dow_totals[i] / max(1, dow_counts_n[i])
        daily_summary.append({
            "day": dow_names[i],
            "avg_admissions": round(avg, 1),
            "total": dow_totals[i],
        })

    return {
        "daily_summary": daily_summary,
        "total_30d": len(history),
        "avg_daily": round(len(history) / 30, 1),
        "avg_los": round(total_los / max(1, los_count), 1),
        "severity_distribution": severity_counts,
        "ward_distribution": ward_counts,
        "discharge_rate": round(sum(1 for h in history if h["discharged"]) / max(1, len(history)) * 100, 1),
    }


# ══════════════════════════════════════════════════════════════════════
# REFRESH ALL (called by bot every 60s)
# ══════════════════════════════════════════════════════════════════════

def refresh_all_predictions():
    """
    Refresh LSTM predictions for all admitted patients.
    Called periodically by the bot service.
    """
    try:
        with db_session() as db:
            patients = db.execute(
                "SELECT id FROM patients WHERE status = 'admitted'"
            ).fetchall()

        count = 0
        for p in patients:
            try:
                predict_patient_los(p["id"])
                count += 1
            except Exception as e:
                logger.error("Prediction failed for %s: %s", p["id"], e)

        # Prune old predictions (keep latest 5 per patient)
        with db_session() as db:
            db.execute("""
                DELETE FROM forecast_predictions 
                WHERE id NOT IN (
                    SELECT id FROM (
                        SELECT id, ROW_NUMBER() OVER (PARTITION BY patient_id ORDER BY created_at DESC) as rn
                        FROM forecast_predictions
                    ) WHERE rn <= 5
                )
            """)

    except Exception as e:
        logger.error("Refresh failed: %s", e)


# ══════════════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════════════

def _normalize_ward(ward: str) -> str:
    """Map a patient's ward onto a key in WARD_CAPACITY.

    A ward Orb actually knows about passes through UNCHANGED. That is the whole
    fix: the old version recognised only ICU/W1/W2/W3 and sent everything else
    to "W1" via its final `return`, so six real wards (GM-1, CCU, RCU, NW-1,
    OW-1, SW-1) collapsed into one and the heatmap reported `W1 30/8`.

    The digit heuristic survives only as a fallback for a ward that is genuinely
    not in the capacity map, so an un-discovered ward still lands somewhere
    rather than vanishing from the census.
    """
    raw = (ward or "").strip()
    if raw in WARD_CAPACITY:
        return raw

    w = raw.upper()
    for known in WARD_CAPACITY:
        if known.upper() == w:
            return known

    if w == "ICU" and "ICU" in WARD_CAPACITY:
        return "ICU"
    for digit, name in (("1", "W1"), ("2", "W2"), ("3", "W3")):
        if digit in w and name in WARD_CAPACITY:
            return name
    # Unknown ward and no legacy bucket to use: keep the real name so the census
    # stays truthful, and give it the standard bed count rather than dropping it.
    if raw:
        WARD_CAPACITY.setdefault(raw, DEFAULT_WARD_BEDS)
        return raw
    return next(iter(WARD_CAPACITY), "W1")


def _get_cached_or_predict(patient_id: str) -> dict:
    """Get cached prediction or compute fresh one."""
    try:
        with db_session() as db:
            cached = db.execute(
                """SELECT predicted_los_days, discharge_probability, acuity_trajectory,
                          confidence, feature_snapshot
                   FROM forecast_predictions 
                   WHERE patient_id = ? 
                   ORDER BY created_at DESC LIMIT 1""",
                (patient_id,),
            ).fetchone()

        if cached:
            snapshot = json.loads(cached["feature_snapshot"]) if cached["feature_snapshot"] else {}
            return {
                "predicted_los_days": cached["predicted_los_days"],
                "discharge_probability": cached["discharge_probability"],
                "acuity_trajectory": cached["acuity_trajectory"],
                "confidence": cached["confidence"],
                "estimated_discharge_date": snapshot.get("estimated_discharge_date", ""),
            }
    except Exception:
        pass

    # Compute fresh
    return predict_patient_los(patient_id)


# ══════════════════════════════════════════════════════════════════════
# APPOINTMENTS SCHEDULE
# ══════════════════════════════════════════════════════════════════════

def get_appointments(days_ahead: int = 7, department: str | None = None) -> list:
    """Booked appointments for the next `days_ahead` hospital-local days.

    ONLY rows from the `appointments` table. This used to synthesise three to
    five "appointments" a day by sampling admitted patients with a date-seeded
    RNG: random type, random reason, random clock time, "Post-Discharge Review"
    for patients still in a bed. The Appointments panel rendered them as
    bookings and counted them in every tile. A schedule nobody booked is
    fabricated clinical data, so it is gone. If projected follow-up demand is
    ever wanted it belongs on Forecast, labelled as projected, with no clock
    time on it.

    `department` is permissions.department_scope(user), never a request value;
    None is the unscoped governance convention. A row is in scope when the
    patient's admission department is the caller's (the ordinary ward
    boundary), or, for a patient with NO admission at all, when the appointment
    names the caller's department. That second branch is the same rule as
    frontdesk_service.opd_registration_allowed: no ward owns a pure outpatient,
    so the row itself records which clinic they are coming to.
    """
    from permissions import scope_patient_ids_sql

    today = hospital_today()
    first = today.isoformat()
    # Exclusive end: "the next 7 days" is today plus six, not eight dates.
    end = (today + timedelta(days=max(1, days_ahead))).isoformat()

    sql = """SELECT ap.id, ap.patient_id, ap.scheduled_date, ap.scheduled_time,
                    ap.appointment_type, ap.reason, ap.origin, ap.department,
                    p.name, p.age, p.sex, p.ward, p.bed, p.diagnosis
               FROM appointments ap
               JOIN patients p ON p.id = ap.patient_id
              WHERE ap.status = 'booked'
                AND ap.scheduled_date >= ? AND ap.scheduled_date < ?"""
    params: list = [first, end]
    if department is not None:
        scope_sql, scope_params = scope_patient_ids_sql(department)
        sql += f"""
                AND (p.id IN ({scope_sql})
                     OR (NOT EXISTS (SELECT 1 FROM admissions a WHERE a.patient_id = p.id)
                         AND LOWER(TRIM(COALESCE(ap.department, ''))) = LOWER(?)))"""
        params.extend(scope_params)
        params.append(department.strip())
    sql += " ORDER BY ap.scheduled_date, ap.scheduled_time, p.name"

    with db_session() as db:
        booked = db.execute(sql, params).fetchall()
        appointments = []
        for b in booked:
            r = db.execute(
                "SELECT level FROM risk_scores WHERE patient_id = ? ORDER BY timestamp DESC, id DESC LIMIT 1",
                (b["patient_id"],),
            ).fetchone()
            appointments.append({
                "id": b["id"],
                "date": b["scheduled_date"],
                "time": b["scheduled_time"],
                "patient_id": b["patient_id"],
                "patient_name": b["name"],
                "age": b["age"],
                "sex": b["sex"],
                "ward": b["ward"],
                "bed": b["bed"],
                "diagnosis": b["diagnosis"],
                "type": b["appointment_type"] or "Follow-up",
                "reason": b["reason"],
                "origin": b["origin"] or "staff",
                "department": b["department"],
                # The latest real risk score, or None. Never defaulted to
                # "stable": a patient nobody has scored is not a stable patient.
                "acuity": r["level"] if r else None,
                "source": "booked",
            })
    return appointments
