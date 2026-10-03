"""Orb — Executive Command Center aggregation service.

Produces a single, house-wide operational snapshot ("single pane of glass") for a
CMO/COO: live census + bed occupancy vs capacity, an acuity heatmap, active
deteriorations, sepsis-bundle compliance, a short admission/occupancy forecast,
downtime-readiness, and today's OR load.

Design contract:
  * PURE READS. This module never mutates state; it composes read-only views over
    existing services and the DB so it is safe to poll on a short interval.
  * DEGRADE, NEVER CRASH. Every section is wrapped in its own try/except. One
    failing subsystem (e.g. a torch model that won't load) returns that section
    as sensible zeros plus an `error` note, and the rest of the board still
    renders. On a completely empty DB the whole snapshot is all-zeros, no error.
  * CONSISTENT NUMBERS. Ward bed capacities are reused verbatim from
    forecast_service.WARD_CAPACITY (discovered per site at startup), and ward
    normalization uses forecast_service._normalize_ward, so census/occupancy here
    always agree with the Forecast pillar.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from database import db_session

logger = logging.getLogger("orb.command_center")

# Reuse the SAME bed-capacity source of truth the Forecast pillar uses, so
# occupancy math is consistent across the platform.
#
# WARD_CAPACITY is imported by REFERENCE (a dict), which is what makes that
# sharing work: `refresh_ward_capacity()` discovers the site's real wards at
# startup and mutates that dict in place, and this module sees it.
#
# The total must NOT be imported the same way. `TOTAL_CAPACITY` is an int, so
# `from ... import TOTAL_CAPACITY` binds a COPY taken at import time — before
# discovery runs — and the copy never updates. That is exactly what happened:
# the Command Center header read `30/48` (the four-ward fallback) while the
# capacity forecast three tiles below it read `34/72` (the six real wards), from
# the same snapshot. Summing the live dict cannot drift.
from services.forecast_service import (
    WARD_CAPACITY,
    _normalize_ward,
)


def _total_capacity() -> int:
    """Live total bed capacity. Always read through this, never a cached int."""
    return sum(WARD_CAPACITY.values())

# A snapshot is considered "downtime-ready" if it was captured within this window.
DOWNTIME_RECENT_WINDOW_MIN = 24 * 60  # 24 hours


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now().isoformat()


def _parse_ts(ts) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def _admitted_with_acuity() -> list[dict]:
    """All admitted patients with their latest risk level (single query)."""
    with db_session() as db:
        rows = db.execute(
            """SELECT p.id, p.name, p.ward, p.bed,
                      (SELECT rs.level FROM risk_scores rs
                        WHERE rs.patient_id = p.id
                        ORDER BY rs.timestamp DESC, rs.id DESC LIMIT 1) AS level
               FROM patients p
               WHERE p.status = 'admitted'
               ORDER BY p.ward, p.bed"""
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        # No risk score yet → treat as stable so acuity buckets always sum to census.
        d["level"] = d.get("level") or "stable"
        d["norm_ward"] = _normalize_ward(d.get("ward"))
        out.append(d)
    return out


# ══════════════════════════════════════════════════════════════════════════
#  SECTION BUILDERS — each returns a dict and never raises (caller wraps too).
# ══════════════════════════════════════════════════════════════════════════

def _census_and_acuity(patients: list[dict]) -> tuple[dict, dict]:
    """Build both the census (occupancy vs capacity) and acuity (counts + heatmap)
    sections from a single pass over the admitted-patient list."""
    # Per-ward tallies keyed by the canonical WARD_CAPACITY wards.
    ward_occ = {w: 0 for w in WARD_CAPACITY}
    ward_acuity = {w: {"critical": 0, "elevated": 0, "stable": 0} for w in WARD_CAPACITY}
    totals = {"critical": 0, "elevated": 0, "stable": 0}

    for p in patients:
        w = p["norm_ward"]
        lvl = p["level"] if p["level"] in totals else "stable"
        totals[lvl] += 1
        if w in ward_occ:
            ward_occ[w] += 1
            ward_acuity[w][lvl] += 1

    by_ward = []
    heatmap = []
    for w, cap in WARD_CAPACITY.items():
        occ = ward_occ[w]
        by_ward.append({
            "ward": w,
            "occupied": occ,
            "capacity": cap,
            "occupancy_pct": round(occ / cap * 100, 1) if cap else 0.0,
        })
        heatmap.append({
            "ward": w,
            "critical": ward_acuity[w]["critical"],
            "elevated": ward_acuity[w]["elevated"],
            "stable": ward_acuity[w]["stable"],
        })

    total_admitted = len(patients)
    census = {
        "total_admitted": total_admitted,
        "total_capacity": _total_capacity(),
        "occupancy_pct": round(total_admitted / _total_capacity() * 100, 1) if _total_capacity() else 0.0,
        "by_ward": by_ward,
    }
    acuity = {
        "critical": totals["critical"],
        "elevated": totals["elevated"],
        "stable": totals["stable"],
        "heatmap": heatmap,
    }
    return census, acuity


def _deteriorations(patients: list[dict]) -> list[dict]:
    """The active deterioration list: every critical/elevated patient, sorted with
    critical first, annotated with a live NEWS2 from their latest vitals."""
    from services.vigil_service import compute_news2

    watch = [p for p in patients if p["level"] in ("critical", "elevated")]
    if not watch:
        return []

    out = []
    with db_session() as db:
        for p in watch:
            news2_val = None
            try:
                vrow = db.execute(
                    """SELECT heart_rate, systolic_bp, respiratory_rate, temperature,
                              spo2, consciousness, on_oxygen
                       FROM vitals WHERE patient_id = ?
                       -- id DESC: a same-timestamp correction is the later write.
                       ORDER BY timestamp DESC, id DESC LIMIT 1""",
                    (p["id"],),
                ).fetchone()
                prow = db.execute(
                    "SELECT on_scale_2 FROM patients WHERE id = ?", (p["id"],)
                ).fetchone()
                if vrow is not None:
                    v = dict(vrow)
                    news2 = compute_news2(
                        v.get("heart_rate"), v.get("systolic_bp"),
                        v.get("respiratory_rate"), v.get("temperature"),
                        v.get("spo2"),
                        consciousness=v.get("consciousness") or "alert",
                        on_oxygen=bool(v.get("on_oxygen") or 0),
                        on_scale_2=bool((dict(prow).get("on_scale_2") if prow else 0) or 0),
                    )
                    news2_val = news2.get("raw_news2")
            except Exception as e:  # a single bad patient must not blank the list
                logger.warning("deterioration NEWS2 failed for %s: %s", p["id"], e)
            out.append({
                "patient_id": p["id"],
                "name": p.get("name"),
                "ward": p.get("ward"),
                "bed": p.get("bed"),
                "level": p["level"],
                "news2": news2_val,
            })

    # Critical first, then by NEWS2 descending (None sorts last).
    rank = {"critical": 0, "elevated": 1}
    out.sort(key=lambda d: (rank.get(d["level"], 2), -(d["news2"] if d["news2"] is not None else -1)))
    return out


def _bundles() -> dict:
    """Active sepsis/deterioration bundles and their 1-hour compliance."""
    from services.sepsis_bundle_service import get_active_bundles

    active_bundles = get_active_bundles()
    active = len(active_bundles)
    # A bundle is "compliant" when nothing has breached its 1-hour target.
    compliant = sum(1 for b in active_bundles if b.get("on_track"))
    compliance_pct = round(100.0 * compliant / active) if active else 100
    return {
        "active": active,
        "compliant": compliant,
        "compliance_pct": compliance_pct,
    }


def _forecast() -> dict:
    """Next-24h admission + occupancy projection, reusing the Forecast pillar's
    hospital-flow model (which itself uses WARD_CAPACITY)."""
    from services import forecast_service

    flow = forecast_service.predict_hospital_flow(days_ahead=1)
    days = flow.get("daily_forecasts") or []
    first = days[0] if days else {}
    return {
        "projected_admissions_24h": first.get("predicted_admissions", 0),
        "projected_occupancy": first.get(
            "predicted_occupancy", flow.get("current_occupancy", 0)
        ),
        "projected_occupancy_pct": first.get(
            "occupancy_pct", flow.get("current_occupancy_pct", 0.0)
        ),
        "current_occupancy": flow.get("current_occupancy", 0),
        "total_capacity": flow.get("total_capacity", _total_capacity()),
    }


def _downtime_readiness(patients: list[dict]) -> dict:
    """How many admitted patients have a fresh, local downtime snapshot — the
    ward's safety net when the EHR/network/LLM is unavailable."""
    from services.downtime_service import get_latest_snapshot

    total = len(patients)
    now = _now()
    recent = 0
    oldest_age_min = None  # age of the STALEST patient's latest snapshot

    for p in patients:
        try:
            snap = get_latest_snapshot(p["id"])
        except Exception as e:
            logger.warning("downtime snapshot lookup failed for %s: %s", p["id"], e)
            snap = None
        if not snap:
            continue
        dt = _parse_ts(snap.get("created_at"))
        if dt is None:
            continue
        age_min = (now - dt).total_seconds() / 60.0
        if age_min <= DOWNTIME_RECENT_WINDOW_MIN:
            recent += 1
        if oldest_age_min is None or age_min > oldest_age_min:
            oldest_age_min = age_min

    return {
        "patients_with_recent_snapshot": recent,
        "total": total,
        "pct": round(100.0 * recent / total, 1) if total else 0.0,
        "oldest_snapshot_age_min": round(oldest_age_min, 1) if oldest_age_min is not None else None,
    }


def _or_today() -> dict:
    """Today's operating-room load (UTC calendar day)."""
    today = _now().date().isoformat()
    with db_session() as db:
        rows = db.execute(
            """SELECT s.id, s.procedure_name, s.operating_room, s.scheduled_time,
                      s.status, s.estimated_duration_min,
                      p.name AS patient_name, p.ward AS ward
               FROM surgeries s
               LEFT JOIN patients p ON p.id = s.patient_id
               WHERE s.scheduled_date = ?
               ORDER BY s.scheduled_time ASC""",
            (today,),
        ).fetchall()
    cases = [{
        "id": r["id"],
        "procedure_name": r["procedure_name"],
        "operating_room": r["operating_room"],
        "scheduled_time": r["scheduled_time"],
        "status": r["status"],
        "estimated_duration_min": r["estimated_duration_min"],
        "patient_name": r["patient_name"],
        "ward": r["ward"],
    } for r in rows]
    return {"count": len(cases), "cases": cases}


# ══════════════════════════════════════════════════════════════════════════
#  PUBLIC ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════

# Empty-DB / failure fallbacks per section (used when a builder raises).
def _empty_census() -> dict:
    return {
        "total_admitted": 0,
        "total_capacity": _total_capacity(),
        "occupancy_pct": 0.0,
        "by_ward": [
            {"ward": w, "occupied": 0, "capacity": cap, "occupancy_pct": 0.0}
            for w, cap in WARD_CAPACITY.items()
        ],
    }


def _empty_acuity() -> dict:
    return {
        "critical": 0, "elevated": 0, "stable": 0,
        "heatmap": [
            {"ward": w, "critical": 0, "elevated": 0, "stable": 0}
            for w in WARD_CAPACITY
        ],
    }


def house_snapshot() -> dict:
    """Assemble the full house-wide command-center snapshot.

    Each section is independently guarded: a failure in one subsystem yields that
    section's empty/zero shape plus an `error` note, while every other section
    still renders. Never raises.
    """
    snapshot: dict = {"generated_at": _now_iso()}

    # Admitted-patient list feeds census, acuity, deteriorations and downtime.
    # If even this base read fails, everything patient-derived degrades to zeros.
    try:
        patients = _admitted_with_acuity()
    except Exception as e:
        logger.error("command-center: base patient read failed: %s", e)
        patients = []
        snapshot["patients_error"] = str(e)

    # ── census + acuity (single pass) ──
    try:
        census, acuity = _census_and_acuity(patients)
    except Exception as e:
        logger.error("command-center census/acuity failed: %s", e)
        census, acuity = _empty_census(), _empty_acuity()
        census["error"] = acuity["error"] = str(e)
    snapshot["census"] = census
    snapshot["acuity"] = acuity

    # ── deteriorations ──
    try:
        snapshot["deteriorations"] = _deteriorations(patients)
    except Exception as e:
        logger.error("command-center deteriorations failed: %s", e)
        snapshot["deteriorations"] = []
        snapshot["deteriorations_error"] = str(e)

    # ── bundles ──
    try:
        snapshot["bundles"] = _bundles()
    except Exception as e:
        logger.error("command-center bundles failed: %s", e)
        snapshot["bundles"] = {"active": 0, "compliant": 0, "compliance_pct": 100, "error": str(e)}

    # ── forecast ──
    try:
        snapshot["forecast"] = _forecast()
    except Exception as e:
        logger.error("command-center forecast failed: %s", e)
        snapshot["forecast"] = {
            "projected_admissions_24h": 0,
            "projected_occupancy": 0,
            "projected_occupancy_pct": 0.0,
            "current_occupancy": census.get("total_admitted", 0),
            "total_capacity": _total_capacity(),
            "error": str(e),
        }

    # ── downtime readiness ──
    try:
        snapshot["downtime_readiness"] = _downtime_readiness(patients)
    except Exception as e:
        logger.error("command-center downtime readiness failed: %s", e)
        snapshot["downtime_readiness"] = {
            "patients_with_recent_snapshot": 0,
            "total": len(patients),
            "pct": 0.0,
            "oldest_snapshot_age_min": None,
            "error": str(e),
        }

    # ── OR today ──
    try:
        snapshot["or_today"] = _or_today()
    except Exception as e:
        logger.error("command-center OR-today failed: %s", e)
        snapshot["or_today"] = {"count": 0, "cases": [], "error": str(e)}

    return snapshot
