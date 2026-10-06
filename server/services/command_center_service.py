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
    """All admitted patients, each banded by the NEWS2 of their LATEST VITALS
    row, in the Vigil board's order.

    The band came off risk_scores, which the Vigil board stopped reading on 3
    October (the simulator and the Scribe lane write vitals with no risk row,
    so it goes stale), and a patient with no risk row was counted "stable". So
    the Command Center could list a patient as stable whom the board beside it
    showed at NEWS2 9. It now scores the same row with the same entry point
    (vigil_service.score_vitals_row, overlay on, the patient's own SpO2 scale)
    and sorts with vigil_service.board_order, so the two cannot disagree.

    `level` is None when nothing is recorded: "not scored", never "stable".
    Every admitted patient is listed (the census counts them all), including
    one with no active admission row, which the board's own query would drop.
    """
    from services.vigil_service import board_order, score_vitals_row

    with db_session() as db:
        rows = db.execute(
            """SELECT p.id, p.name, p.ward, p.bed, p.on_scale_2
               FROM patients p
               WHERE p.status = 'admitted'"""
        ).fetchall()
        # The latest row per patient, id DESC breaking a timestamp tie: the
        # same moment the board scores.
        latest = {
            v["patient_id"]: dict(v)
            for v in db.execute(
                """SELECT v.* FROM patients p
                   JOIN vitals v ON v.id = (
                       SELECT v2.id FROM vitals v2 WHERE v2.patient_id = p.id
                       ORDER BY v2.timestamp DESC, v2.id DESC LIMIT 1
                   )
                   WHERE p.status = 'admitted'"""
            ).fetchall()
        }

    # Scored after the session closes: the CfC overlay reads the vitals history
    # on its own connection.
    out = []
    for r in rows:
        d = dict(r)
        on_scale_2 = bool(d.pop("on_scale_2", 0) or 0)
        news2 = score_vitals_row(latest.get(d["id"]), patient_id=d["id"], on_scale_2=on_scale_2)
        d["news2_result"] = news2
        d["level"] = news2["level"] if news2 else None
        d["news2"] = news2["raw_news2"] if news2 else None
        d["norm_ward"] = _normalize_ward(d.get("ward"))
        out.append(d)
    out.sort(key=lambda d: board_order(d["id"], d["news2_result"]))
    return out


# ══════════════════════════════════════════════════════════════════════════
#  SECTION BUILDERS — each returns a dict and never raises (caller wraps too).
# ══════════════════════════════════════════════════════════════════════════

def _census_and_acuity(patients: list[dict]) -> tuple[dict, dict]:
    """Build both the census (occupancy vs capacity) and acuity (counts + heatmap)
    sections from a single pass over the admitted-patient list."""
    # Per-ward tallies keyed by the canonical WARD_CAPACITY wards.
    ward_occ = {w: 0 for w in WARD_CAPACITY}
    ward_acuity = {w: {"critical": 0, "elevated": 0, "stable": 0, "not_scored": 0}
                   for w in WARD_CAPACITY}
    totals = {"critical": 0, "elevated": 0, "stable": 0, "not_scored": 0}

    for p in patients:
        w = p["norm_ward"]
        # Nothing recorded is its own bucket. It used to be counted stable so
        # the buckets summed to the census; they still do, honestly.
        lvl = p["level"] if p["level"] in ("critical", "elevated", "stable") else "not_scored"
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
            "not_scored": ward_acuity[w]["not_scored"],
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
        "not_scored": totals["not_scored"],
        "heatmap": heatmap,
    }
    return census, acuity


def _deteriorations(patients: list[dict]) -> list[dict]:
    """The highest-scores list: every critical/elevated patient, in the Vigil
    board's order (critical first, then NEWS2 high to low), each with the NEWS2
    of the latest vitals that banded them.

    It used to band from risk_scores and then re-score the vitals itself with
    unassessed ACVPU taken as alert, so a row could say "elevated" beside a
    NEWS2 from a different moment, and the order was not the board's.
    `patients` arrives in board order from `_admitted_with_acuity`.
    """
    out = []
    for p in patients:
        if p["level"] not in ("critical", "elevated"):
            continue
        news2 = p.get("news2_result") or {}
        out.append({
            "patient_id": p["id"],
            "name": p.get("name"),
            "ward": p.get("ward"),
            "bed": p.get("bed"),
            "level": p["level"],
            "news2": p.get("news2"),
            # False: an incomplete set, so the number is a minimum.
            "news2_complete": news2.get("complete"),
        })
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
        "critical": 0, "elevated": 0, "stable": 0, "not_scored": 0,
        "heatmap": [
            {"ward": w, "critical": 0, "elevated": 0, "stable": 0, "not_scored": 0}
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
