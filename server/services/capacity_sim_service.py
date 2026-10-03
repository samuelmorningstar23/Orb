"""Orb Forecast — What-If Capacity & Surge Simulator

A deterministic scenario engine that projects hospital occupancy forward under
operator-defined "what-if" knobs (extra admissions, opened/closed beds, an
admission-rate surge multiplier, longer/shorter stays, aggressive discharge).

DESIGN — built ON the existing forecast models, not a parallel one
------------------------------------------------------------------
The baseline (current census, total bed capacity, expected admission rate) is
taken directly from ``services.forecast_service`` so the simulator can never
contradict the numbers the Forecast pillar already shows the clinician:

  * ``forecast_service.predict_hospital_flow()`` — supplies the current census,
    total capacity, and the Poisson-fit expected admission rate (its
    ``summary.total_predicted_admissions_7d`` is the expected admissions over a
    full 7-day window; dividing by 7 recovers the daily base rate, since the
    day-of-week multipliers ``forecast_service.DOW_MULTIPLIERS`` average to 1.0
    across a complete week).
  * ``forecast_service.TOTAL_CAPACITY`` / ``WARD_CAPACITY`` — bed capacity.

We import these read-only and never mutate forecast state.

DETERMINISM
-----------
The projection math contains no ``random`` and no wall-clock reads. Given the
same baseline + knobs it always returns the same numbers (the only clock use is
an OPTIONAL ``now`` used purely to stamp human-readable timestamps on each step;
it never changes occupancy/overflow/staffing outputs). Callers/tests may also
inject the baseline directly via ``baseline_*`` overrides to be fully hermetic.
Degrades gracefully on an empty DB (baseline census 0, default admission rate).
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("orb.capacity_sim")

from services import forecast_service


# ══════════════════════════════════════════════════════════════════════
#  ASSUMPTIONS (cited, editable) — the honest, transparent basis of the sim
# ══════════════════════════════════════════════════════════════════════

# Average length-of-stay used to derive the baseline discharge rate.
# Mirrors the per-ward base LOS heuristic in
# ``forecast_service._heuristic_prediction`` ({ICU:7, W1:4, W2:3, W3:3.5} days);
# the unweighted mean of those four wards is 4.375 days. Kept here as a named
# constant so the simulator's discharge assumption is explicit and stays in step
# with the forecast heuristic rather than inventing a new LOS.
BASE_AVG_LOS_DAYS = 4.375

# Nurse-to-patient staffing ratio (patients per nurse). 1 nurse : 4 patients is
# the common med-surg safe-staffing benchmark (e.g. California Title 22 §70217
# mandates 1:4 on medical/surgical units; ICUs are stricter at 1:2). Editable
# per scenario via the ``nurse_patient_ratio`` knob so ops leaders can model
# their own unit's ratio.
DEFAULT_NURSE_PATIENT_RATIO = 4.0

# Fallback daily admission rate when forecast_service cannot supply one (empty
# DB / no admission history). Matches forecast_service's own default
# ("Default Indian small hospital") so the two never disagree.
FALLBACK_ADMISSION_RATE_DAILY = 4.5

ASSUMPTIONS_META = {
    "base_avg_los_days": {
        "value": BASE_AVG_LOS_DAYS,
        "source": "forecast_service._heuristic_prediction ward base-LOS mean "
                  "(ICU 7 / W1 4 / W2 3 / W3 3.5 days → mean 4.375)",
    },
    "nurse_patient_ratio": {
        "value": DEFAULT_NURSE_PATIENT_RATIO,
        "source": "Med-surg safe-staffing benchmark 1:4 (California Title 22 "
                  "§70217; ICU 1:2). Editable per scenario.",
    },
    "admission_rate_source": {
        "value": "forecast_service.predict_hospital_flow",
        "source": "Poisson mean fit over 30-day admission_history "
                  "(day-of-week multipliers average to 1.0 across a full week)",
    },
    "capacity_source": {
        "value": forecast_service.TOTAL_CAPACITY,
        "source": "forecast_service.WARD_CAPACITY (ICU+W1+W2+W3)",
    },
    "fallback_admission_rate_daily": {
        "value": FALLBACK_ADMISSION_RATE_DAILY,
        "source": "forecast_service default admission rate for empty history",
    },
}


# ══════════════════════════════════════════════════════════════════════
#  BASELINE — pulled from the live forecast models
# ══════════════════════════════════════════════════════════════════════

def _load_baseline() -> dict:
    """Read the current census, capacity, and expected daily admission rate
    from forecast_service. Degrades to zero-census / default rate on any error
    or an empty DB — never raises."""
    census = 0
    capacity = forecast_service.TOTAL_CAPACITY
    admission_rate_daily = FALLBACK_ADMISSION_RATE_DAILY

    try:
        flow = forecast_service.predict_hospital_flow(days_ahead=7)
        census = int(flow.get("current_occupancy", 0) or 0)
        capacity = int(flow.get("total_capacity", capacity) or capacity)
        total_7d = flow.get("summary", {}).get("total_predicted_admissions_7d")
        if total_7d:
            admission_rate_daily = float(total_7d) / 7.0
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("Baseline load fell back to defaults: %s", e)

    return {
        "census": max(0, census),
        "capacity": max(0, capacity),
        "admission_rate": round(admission_rate_daily, 2),
    }


# ══════════════════════════════════════════════════════════════════════
#  SIMULATION
# ══════════════════════════════════════════════════════════════════════

def _f(scenario: dict, key: str, default: float) -> float:
    """Coerce a scenario knob to float, tolerating None/blank/bad input."""
    try:
        v = scenario.get(key, default)
        if v is None or v == "":
            return float(default)
        return float(v)
    except (TypeError, ValueError):
        return float(default)


def simulate(scenario: dict | None = None, horizon_hours: int = 24) -> dict:
    """Project the next ``horizon_hours`` of occupancy under a what-if scenario.

    Scenario knobs (all optional):
      - extra_admissions          : one-off surge of N admissions spread evenly
                                    over the horizon ("20 admissions tonight")
      - beds_delta                : open (+) / close (-) beds vs current capacity
      - admission_rate_multiplier : scale the expected admission rate (1.5 = flu)
      - los_multiplier            : scale length-of-stay (>1 = sicker/slower)
      - discharge_boost           : fractional increase in discharge rate
                                    (0.3 = 30% more aggressive discharging)
      - nurse_patient_ratio       : patients per nurse (editable staffing model)

    Baseline overrides (hermetic testing / manual planning):
      - baseline_census, baseline_capacity, admission_rate_daily
      - now : ISO timestamp used ONLY to label steps (never affects math)

    Returns the full contract: baseline, scenario (resolved), projection,
    summary, assumptions.
    """
    scenario = dict(scenario or {})

    # ── Resolve baseline (overrides win, else pull from forecast models) ──
    base = _load_baseline()
    if "baseline_census" in scenario and scenario["baseline_census"] is not None:
        base["census"] = max(0, int(_f(scenario, "baseline_census", base["census"])))
    if "baseline_capacity" in scenario and scenario["baseline_capacity"] is not None:
        base["capacity"] = max(0, int(_f(scenario, "baseline_capacity", base["capacity"])))
    if "admission_rate_daily" in scenario and scenario["admission_rate_daily"] is not None:
        base["admission_rate"] = round(_f(scenario, "admission_rate_daily", base["admission_rate"]), 2)

    # ── Resolve knobs ──
    horizon_hours = max(1, min(int(horizon_hours), 24 * 30))  # cap at 30 days
    extra_admissions = max(0.0, _f(scenario, "extra_admissions", 0.0))
    beds_delta = int(_f(scenario, "beds_delta", 0.0))
    adm_mult = max(0.0, _f(scenario, "admission_rate_multiplier", 1.0))
    los_mult = max(0.05, _f(scenario, "los_multiplier", 1.0))
    discharge_boost = max(-0.99, _f(scenario, "discharge_boost", 0.0))
    nurse_ratio = _f(scenario, "nurse_patient_ratio", DEFAULT_NURSE_PATIENT_RATIO)
    if nurse_ratio <= 0:
        nurse_ratio = DEFAULT_NURSE_PATIENT_RATIO

    census0 = base["census"]
    capacity = max(0, base["capacity"] + beds_delta)

    # ── Rates (per hour) ──
    # Admissions: expected daily rate → hourly, scaled by surge multiplier,
    # plus the one-off surge spread evenly across the horizon.
    base_hourly_adm = (base["admission_rate"] / 24.0) * adm_mult
    extra_hourly_adm = extra_admissions / horizon_hours

    # Discharges: a census fraction leaves each hour, governed by LOS.
    # fraction/hour = 1 / (avg_los_hours), lengthened by los_mult,
    # accelerated by discharge_boost.
    eff_los_hours = BASE_AVG_LOS_DAYS * 24.0 * los_mult
    discharge_rate_hourly = (1.0 / eff_los_hours) * (1.0 + discharge_boost)
    discharge_rate_hourly = min(discharge_rate_hourly, 1.0)  # can't discharge >100%/hr

    # ── Optional step labelling clock (does NOT affect any number) ──
    now = None
    raw_now = scenario.get("now")
    if raw_now:
        try:
            now = datetime.fromisoformat(str(raw_now).replace("Z", "+00:00"))
        except (ValueError, TypeError):
            now = None

    # ── Step the model forward ──
    projection = []
    occupancy = float(census0)
    peak_occupancy = occupancy
    peak_step = 0
    max_overflow = 0.0
    hours_to_overflow = None
    total_admissions = 0.0
    total_discharges = 0.0

    def _record(step: int, hour_offset: int, occ: float):
        entry = {
            "step": step,
            "hour_offset": hour_offset,
            "projected_occupancy": round(occ, 1),
            "capacity": capacity,
            "overflow": round(max(0.0, occ - capacity), 1),
            "occupancy_pct": round((occ / capacity * 100.0), 1) if capacity > 0 else None,
        }
        if now is not None:
            entry["timestamp"] = (now + timedelta(hours=hour_offset)).isoformat()
        projection.append(entry)

    # t0 = baseline (hour 0)
    _record(0, 0, occupancy)
    if capacity > 0 and occupancy > capacity and hours_to_overflow is None:
        hours_to_overflow = 0

    for h in range(1, horizon_hours + 1):
        admissions_h = base_hourly_adm + extra_hourly_adm
        discharges_h = occupancy * discharge_rate_hourly
        occupancy = max(0.0, occupancy + admissions_h - discharges_h)
        total_admissions += admissions_h
        total_discharges += discharges_h

        _record(h, h, occupancy)

        if occupancy > peak_occupancy:
            peak_occupancy = occupancy
            peak_step = h
        overflow = max(0.0, occupancy - capacity)
        if overflow > max_overflow:
            max_overflow = overflow
        if capacity > 0 and occupancy > capacity and hours_to_overflow is None:
            hours_to_overflow = h

    # ── Summary metrics ──
    peak_pct = round(peak_occupancy / capacity * 100.0, 1) if capacity > 0 else None
    beds_short_at_peak = int(math.ceil(max(0.0, peak_occupancy - capacity)))
    # Suggested discharges = extra discharges needed to keep the peak at/under
    # capacity (i.e. the worst-case overflow rounded up to whole patients).
    suggested_discharges = int(math.ceil(max_overflow))

    # Staffing gap = nurses needed at peak occupancy minus nurses the baseline
    # census is already staffed for, at the (editable) nurse:patient ratio.
    nurses_at_peak = math.ceil(peak_occupancy / nurse_ratio) if peak_occupancy > 0 else 0
    nurses_at_baseline = math.ceil(census0 / nurse_ratio) if census0 > 0 else 0
    staffing_gap_nurses = max(0, nurses_at_peak - nurses_at_baseline)

    return {
        "baseline": {
            "census": census0,
            "capacity": base["capacity"],
            "admission_rate": base["admission_rate"],  # per day
        },
        "scenario": {
            "extra_admissions": extra_admissions,
            "beds_delta": beds_delta,
            "admission_rate_multiplier": adm_mult,
            "los_multiplier": los_mult,
            "discharge_boost": discharge_boost,
            "nurse_patient_ratio": nurse_ratio,
            "horizon_hours": horizon_hours,
            "effective_capacity": capacity,
        },
        "projection": projection,
        "summary": {
            "peak_occupancy": round(peak_occupancy, 1),
            "peak_occupancy_pct": peak_pct,
            "peak_hour_offset": peak_step,
            "hours_to_overflow": hours_to_overflow,
            "beds_short_at_peak": beds_short_at_peak,
            "suggested_discharges": suggested_discharges,
            "staffing_gap_nurses": staffing_gap_nurses,
            "nurses_required_at_peak": nurses_at_peak,
            "projected_admissions": round(total_admissions, 1),
            "projected_discharges": round(total_discharges, 1),
        },
        "assumptions": ASSUMPTIONS_META,
    }
