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
  * ONE FACILITY AT A TIME (multisite-12). With more than one facility of a
    hospital group on the appliance, house_snapshot(facility_ids) keeps every
    section to those facilities: the patients whose latest encounter is there
    (an admitted patient's is their active admission), the beds of the wards
    placed there (forecast_service.capacity_for, from Bed setup), their bundles,
    forecast and operating lists. None is the whole appliance, as before, and
    group_tiles=True adds one tile per facility for a group-wide reader, built
    from the same patient list so the tiles add up to the group's figures.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from database import db_session

from services import hospital_time
from services.facility_filter import column_clause, patient_clause
from services.forecast_service import (
    WARD_CAPACITY,
    _normalize_ward,
    capacity_for,
    capacity_source_summary,
    ward_facilities,
)

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


def _total_capacity(capacity: dict[str, int] | None = None) -> int:
    """Live total bed capacity. Always read through this, never a cached int.
    `capacity` is one facility's wards (capacity_for); None is every ward."""
    return sum((WARD_CAPACITY if capacity is None else capacity).values())


# A snapshot is considered "downtime-ready" if it was captured within this window.
DOWNTIME_RECENT_WINDOW_MIN = 24 * 60  # 24 hours


def _now() -> datetime:
    return datetime.now(UTC)


def _now_iso() -> str:
    return _now().isoformat()


def _parse_ts(ts) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt
    except (ValueError, TypeError):
        return None


def _admitted_with_acuity(
    facility_ids: frozenset[int] | None = None, *, with_facility: bool = False
) -> list[dict]:
    """All admitted patients, each banded by the NEWS2 of their LATEST VITALS
    row, in the Vigil board's order.

    `facility_ids` keeps the list to the patients whose latest encounter is at
    those facilities (services.facility_filter.patient_clause, the rule a chart
    is opened by); None lists every admitted patient. `with_facility` adds each
    patient's facility_id (that same rule) for the group tiles.

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
    from permissions import FACILITY_PATIENT_SQL

    from services.vigil_service import (
        board_order,
        score_vitals_row,
        scoring_enabled,
        ward_bed_order,
    )

    fac_sql, fac_args = patient_clause(facility_ids)
    facility_col = f", {FACILITY_PATIENT_SQL} AS facility_id" if with_facility else ""
    with db_session() as db:
        rows = db.execute(
            f"""SELECT p.id, p.name, p.ward, p.bed, p.on_scale_2{facility_col}
               FROM patients p
               WHERE p.status = 'admitted'{fac_sql}""",
            fac_args,
        ).fetchall()
        # The latest row per patient, id DESC breaking a timestamp tie: the
        # same moment the board scores.
        latest = {
            v["patient_id"]: dict(v)
            for v in db.execute(
                f"""SELECT v.* FROM patients p
                   JOIN vitals v ON v.id = (
                       SELECT v2.id FROM vitals v2 WHERE v2.patient_id = p.id
                         AND v2.voided_at IS NULL
                       ORDER BY v2.timestamp DESC, v2.id DESC LIMIT 1
                   )
                   WHERE p.status = 'admitted'{fac_sql}""",
                fac_args,
            ).fetchall()
        }

    # Scored after the session closes: the CfC overlay reads the vitals history
    # on its own connection.
    # With scoring switched off (ORB_SCORING_ENABLED, the Orb Ward Record) no
    # NEWS2 is computed: every patient is "not scored", the highest-scores list
    # is empty and the list is in ward, then bed order, as on the Vigil board.
    scoring = scoring_enabled()
    out = []
    for r in rows:
        d = dict(r)
        on_scale_2 = bool(d.pop("on_scale_2", 0) or 0)
        if not scoring:
            d["news2_result"] = None
            d["level"] = None
            d["news2"] = None
            d["norm_ward"] = _normalize_ward(d.get("ward") or "")
            out.append(d)
            continue
        # One patient whose row cannot be scored must not blank the house: the
        # whole list used to fall to zeros with it. That patient is counted
        # "not scored", which the Critical card says out loud.
        try:
            news2 = score_vitals_row(latest.get(d["id"]), patient_id=d["id"], on_scale_2=on_scale_2)
        except Exception as e:
            logger.warning("command-center: NEWS2 failed for %s: %s", d["id"], e)
            news2 = None
        d["news2_result"] = news2
        d["level"] = news2["level"] if news2 else None
        d["news2"] = news2["raw_news2"] if news2 else None
        d["norm_ward"] = _normalize_ward(d.get("ward") or "")
        out.append(d)
    if scoring:
        out.sort(key=lambda d: board_order(d["id"], d["news2_result"]))
    else:
        out.sort(key=ward_bed_order)
    return out


# ══════════════════════════════════════════════════════════════════════════
#  SECTION BUILDERS — each returns a dict and never raises (caller wraps too).
# ══════════════════════════════════════════════════════════════════════════


def _census_and_acuity(
    patients: list[dict], capacity: dict[str, int] | None = None
) -> tuple[dict, dict]:
    """Build both the census (occupancy vs capacity) and acuity (counts + heatmap)
    sections from a single pass over the admitted-patient list. `capacity` is
    one facility's wards (capacity_for); None is every ward of WARD_CAPACITY."""
    wards = WARD_CAPACITY if capacity is None else capacity
    # Per-ward tallies keyed by the canonical WARD_CAPACITY wards.
    ward_occ = {w: 0 for w in wards}
    ward_acuity = {w: {"critical": 0, "elevated": 0, "stable": 0, "not_scored": 0} for w in wards}
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
    for w, cap in wards.items():
        occ = ward_occ[w]
        by_ward.append(
            {
                "ward": w,
                "occupied": occ,
                "capacity": cap,
                "occupancy_pct": round(occ / cap * 100, 1) if cap else 0.0,
            }
        )
        heatmap.append(
            {
                "ward": w,
                "critical": ward_acuity[w]["critical"],
                "elevated": ward_acuity[w]["elevated"],
                "stable": ward_acuity[w]["stable"],
                "not_scored": ward_acuity[w]["not_scored"],
            }
        )

    total_admitted = len(patients)
    census = {
        "total_admitted": total_admitted,
        "total_capacity": _total_capacity(capacity),
        "occupancy_pct": round(total_admitted / _total_capacity(capacity) * 100, 1)
        if _total_capacity(capacity)
        else 0.0,
        "by_ward": by_ward,
    }
    # Where the bed counts came from (adt-14): 'from bed register' or 'site
    # setting', only while Beds and admissions is on (None, nothing added, off).
    source = capacity_source_summary(wards=None if capacity is None else list(capacity))
    if source is not None:
        census["capacity_source"] = {k: source[k] for k in ("source", "words")}
        for row in by_ward:
            row["capacity_source"] = source["by_ward"].get(
                row["ward"], {"source": "site_setting", "words": "site setting"}
            )
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
        out.append(
            {
                "patient_id": p["id"],
                "name": p.get("name"),
                "ward": p.get("ward"),
                "bed": p.get("bed"),
                "level": p["level"],
                "news2": p.get("news2"),
                # False: an incomplete set, so the number is a minimum.
                "news2_complete": news2.get("complete"),
            }
        )
    return out


def _bundles(facility_ids: frozenset[int] | None = None) -> dict:
    """Active sepsis/deterioration bundles and their 1-hour compliance, at
    `facility_ids` (None: every facility)."""
    from services.sepsis_bundle_service import get_active_bundles

    active_bundles = (
        get_active_bundles()
        if facility_ids is None
        else get_active_bundles(facilities=facility_ids)
    )
    active = len(active_bundles)
    # A bundle is "compliant" when nothing has breached its 1-hour target.
    compliant = sum(1 for b in active_bundles if b.get("on_track"))
    compliance_pct = round(100.0 * compliant / active) if active else 100
    return {
        "active": active,
        "compliant": compliant,
        "compliance_pct": compliance_pct,
    }


def _forecast(facility_ids: frozenset[int] | None = None) -> dict:
    """Next-24h admission + occupancy projection, reusing the Forecast pillar's
    hospital-flow model (which itself uses WARD_CAPACITY), at `facility_ids`
    (None: every facility)."""
    from services import forecast_service

    flow = (
        forecast_service.predict_hospital_flow(days_ahead=1)
        if facility_ids is None
        else forecast_service.predict_hospital_flow(days_ahead=1, facilities=facility_ids)
    )
    days = flow.get("daily_forecasts") or []
    first = days[0] if days else {}
    return {
        "projected_admissions_24h": first.get("predicted_admissions", 0),
        "projected_occupancy": first.get("predicted_occupancy", flow.get("current_occupancy", 0)),
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


def _or_today(facility_ids: frozenset[int] | None = None) -> dict:
    """Today's operating-room load, at `facility_ids` (the case's own
    surgeries.facility_id; None: every facility).

    Today is the hospital's calendar day (services.hospital_time), because
    surgeries.scheduled_date is a ward calendar date. The UTC date is still
    yesterday between 00:00 and 05:30 IST, so the board showed yesterday's
    theatre list to the night team."""
    today = hospital_time.hospital_today().isoformat()
    fac_sql, fac_args = column_clause("s.facility_id", facility_ids)
    with db_session() as db:
        rows = db.execute(
            f"""SELECT s.id, s.procedure_name, s.operating_room, s.scheduled_time,
                      s.status, s.estimated_duration_min,
                      p.name AS patient_name, p.ward AS ward
               FROM surgeries s
               LEFT JOIN patients p ON p.id = s.patient_id
               WHERE s.scheduled_date = ?{fac_sql}
               ORDER BY s.scheduled_time ASC""",
            (today, *fac_args),
        ).fetchall()
    cases = [
        {
            "id": r["id"],
            "procedure_name": r["procedure_name"],
            "operating_room": r["operating_room"],
            "scheduled_time": r["scheduled_time"],
            "status": r["status"],
            "estimated_duration_min": r["estimated_duration_min"],
            "patient_name": r["patient_name"],
            "ward": r["ward"],
        }
        for r in rows
    ]
    return {"count": len(cases), "cases": cases}


# ══════════════════════════════════════════════════════════════════════════
#  PUBLIC ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════


# Empty-DB / failure fallbacks per section (used when a builder raises).
def _empty_census(capacity: dict[str, int] | None = None) -> dict:
    wards = WARD_CAPACITY if capacity is None else capacity
    return {
        "total_admitted": 0,
        "total_capacity": _total_capacity(capacity),
        "occupancy_pct": 0.0,
        "by_ward": [
            {"ward": w, "occupied": 0, "capacity": cap, "occupancy_pct": 0.0}
            for w, cap in wards.items()
        ],
    }


def _empty_acuity(capacity: dict[str, int] | None = None) -> dict:
    wards = WARD_CAPACITY if capacity is None else capacity
    return {
        "critical": 0,
        "elevated": 0,
        "stable": 0,
        "not_scored": 0,
        "heatmap": [
            {"ward": w, "critical": 0, "elevated": 0, "stable": 0, "not_scored": 0} for w in wards
        ],
    }


def _facility_tiles(patients: list[dict]) -> dict:
    """One tile per facility on record, for a reader who sees the whole group:
    its census, beds and bands, from the same patient list and ward map as the
    group's own figures, so the tiles add up to them. The beds of a ward no
    facility can be told for are counted apart (unplaced_capacity), never
    given to one facility."""
    from services import facility_service

    placed = ward_facilities()
    facility_ids = sorted(int(f["id"]) for f in facility_service.list_facilities())
    tiles: list[dict[str, Any]] = []
    for fid in facility_ids:
        own = [p for p in patients if p.get("facility_id") is not None and p["facility_id"] == fid]
        beds = sum(cap for w, cap in list(WARD_CAPACITY.items()) if placed.get(w) == fid)
        bands = {"critical": 0, "elevated": 0, "stable": 0, "not_scored": 0}
        for p in own:
            level = p["level"] if p["level"] in ("critical", "elevated", "stable") else "not_scored"
            bands[level] += 1
        tiles.append(
            {
                "facility": facility_service.facility_identity(fid),
                "total_admitted": len(own),
                "total_capacity": beds,
                "occupancy_pct": round(len(own) / beds * 100, 1) if beds else 0.0,
                **bands,
            }
        )
    unplaced = sum(cap for w, cap in list(WARD_CAPACITY.items()) if placed.get(w) is None)
    return {"tiles": tiles, "unplaced_capacity": unplaced}


def house_snapshot(
    facility_ids: frozenset[int] | None = None, *, group_tiles: bool = False
) -> dict:
    """Assemble the full house-wide command-center snapshot.

    Each section is independently guarded: a failure in one subsystem yields that
    section's empty/zero shape plus an `error` note, while every other section
    still renders. Never raises.

    `facility_ids` keeps every section to those facilities (multisite-12);
    None is the whole appliance. `group_tiles` (a group-wide reader, with
    more than one facility on record) adds `facility_tiles`, one per
    facility, and `unplaced_capacity`.
    """
    snapshot: dict = {"generated_at": _now_iso()}
    # The screen hides the acuity bands and the highest-scores list when this
    # is False (ORB_SCORING_ENABLED off, the Orb Ward Record).
    from services.vigil_service import scoring_enabled

    snapshot["scoring_enabled"] = scoring_enabled()

    # Admitted-patient list feeds census, acuity, deteriorations and downtime.
    # If even this base read fails, everything patient-derived degrades to zeros.
    try:
        if facility_ids is None and not group_tiles:
            patients = _admitted_with_acuity()
        else:
            patients = _admitted_with_acuity(facility_ids, with_facility=group_tiles)
    except Exception as e:
        logger.error("command-center: base patient read failed: %s", e)
        patients = []
        snapshot["patients_error"] = str(e)

    # One facility's wards (read after the patients, whose wards may have
    # joined the map); None is every ward.
    capacity: dict[str, int] | None = None
    if facility_ids is not None:
        try:
            capacity = capacity_for(facility_ids)
        except Exception as e:
            # A facility whose wards cannot be told shows no beds, never the
            # whole group's.
            logger.error("command-center: facility capacity read failed: %s", e)
            capacity = {}
            snapshot["capacity_error"] = str(e)

    # ── census + acuity (single pass) ──
    try:
        census, acuity = _census_and_acuity(patients, capacity)
    except Exception as e:
        logger.error("command-center census/acuity failed: %s", e)
        census, acuity = _empty_census(capacity), _empty_acuity(capacity)
        census["error"] = acuity["error"] = str(e)
    snapshot["census"] = census
    snapshot["acuity"] = acuity

    # ── one tile per facility (a group-wide reader only) ──
    if group_tiles:
        try:
            tiles = _facility_tiles(patients)
            snapshot["facility_tiles"] = tiles["tiles"]
            snapshot["unplaced_capacity"] = tiles["unplaced_capacity"]
        except Exception as e:
            logger.error("command-center facility tiles failed: %s", e)
            snapshot["facility_tiles"] = []
            snapshot["facility_tiles_error"] = str(e)

    # ── deteriorations ──
    try:
        snapshot["deteriorations"] = _deteriorations(patients)
    except Exception as e:
        logger.error("command-center deteriorations failed: %s", e)
        snapshot["deteriorations"] = []
        snapshot["deteriorations_error"] = str(e)

    # ── bundles ──
    try:
        snapshot["bundles"] = _bundles() if facility_ids is None else _bundles(facility_ids)
    except Exception as e:
        logger.error("command-center bundles failed: %s", e)
        snapshot["bundles"] = {"active": 0, "compliant": 0, "compliance_pct": 100, "error": str(e)}

    # ── forecast ──
    try:
        snapshot["forecast"] = _forecast() if facility_ids is None else _forecast(facility_ids)
    except Exception as e:
        logger.error("command-center forecast failed: %s", e)
        snapshot["forecast"] = {
            "projected_admissions_24h": 0,
            "projected_occupancy": 0,
            "projected_occupancy_pct": 0.0,
            "current_occupancy": census.get("total_admitted", 0),
            "total_capacity": _total_capacity(capacity),
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
        snapshot["or_today"] = _or_today() if facility_ids is None else _or_today(facility_ids)
    except Exception as e:
        logger.error("command-center OR-today failed: %s", e)
        snapshot["or_today"] = {"count": 0, "cases": [], "error": str(e)}

    return snapshot
