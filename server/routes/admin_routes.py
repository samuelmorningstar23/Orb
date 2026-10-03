"""Orb Hospital OS — Admin / Governance Routes

Provides pre-aggregated governance metrics for the Admin dashboard.
All endpoints require admin.overview permission.
"""
import json
import logging
import os
import shutil
import sqlite3
import tempfile
from db_backend import DBIntegrityError, DBOperationalError
import uuid
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from datetime import datetime, timezone, timedelta
from typing import Optional

from database import db_session, run_db
from permissions import (require_permission, ADMIN_OVERVIEW, department_scope,
                         log_phi_read, scope_patient_ids_sql)
from services import history_import_service as history_import
from services.audit_service import log_audit_event
from services.consent_service import ERASURE_RETAINED_TABLES
from config import INGESTION_MODE, AUDIT_SIMULATION_EVENTS

logger = logging.getLogger("orb.admin")

router = APIRouter(prefix="/api/admin", tags=["admin"])


# ═════════════════════════════════════════════════════════════════════════════
#  THE LANE PROBLEM, AND WHY THESE TWO TILES HAVE THEIR OWN SOURCES
# ═════════════════════════════════════════════════════════════════════════════
#
# Clinical work reaches Orb down more than one LANE. The two that exist today:
#
#   * the HTTP ingest lane — POST /api/ingest/* → services/ingestion_service.py;
#   * the simulator/bot lane — services/bot_service.py, which is what runs
#     whenever ORB_INGESTION_MODE is `simulation` or `hybrid`.
#
# Until 2026-08-14 the "Alert Activity" and "Records Written" tiles counted
# `audit_logs` rows with action_type IN ('vigil_alert_generated',
# 'relay_alert_generated') and `ingestion_events` respectively. BOTH of those are
# written at call sites that exist ONLY on the HTTP ingest lane
# (ingestion_service._check_ingested_alert; the ingestion_events ledger is the
# ingest endpoint's own receipt). The simulator lane writes neither. So on a box
# running in simulation mode — which is the mode the dry run, every demo and
# every soak test uses — two of the five headline tiles were pinned to 0 while
# the ward showed 43 active / 17 critical and the Flight Recorder streamed
# vigil.alert.critical live. The page was not out of date; it was structurally
# incapable of being right.
#
# THE RULE THIS FILE NOW FOLLOWS: a headline count must come from a table that
# EVERY lane writes, and if a lane's contribution cannot be known, the number is
# reported as unknown (null) — NEVER as 0. Insufficient data is displayed as
# insufficient data. A zero on an admin dashboard is a clinical claim ("nothing
# happened"); we do not make it unless we can support it.

#: The NEWS2 bands an alert can carry (the non-stable half of vigil_service's
#: vocabulary). Pre-seeded into `by_level` so a band with no alerts renders as a
#: KNOWN zero rather than a missing key the UI would have to guess at — the
#: distinction this whole module is about: known-zero and unknown are different
#: answers and must reach the client as different values.
_ALERT_LEVELS = ("critical", "elevated")


def _shadow_off_reason() -> str | None:
    """Why the shadow event log cannot answer, or None when it is recording.

    Shared by the Overview tile and the Alerts list so the two say the same
    thing when the instrument is off.
    """
    try:
        # Read the switch through the module that DEFINES it rather than
        # re-reading ORB_SHADOW_ENABLED here, so the two can never drift apart.
        from services import shadow_service
        instrument_on = bool(shadow_service._enabled())
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("admin overview: shadow instrument probe failed: %s", exc)
        return (
            "Orb could not determine whether the shadow event log is recording, "
            "so the alert count is not knowable."
        )
    if not instrument_on:
        return (
            "The shadow event log is switched off (ORB_SHADOW_ENABLED), so no "
            "lane is recording alerts. Alerts may still be firing on the ward. "
            "This number is unknown, not zero."
        )
    return None


def _alerts_24h(db, cutoff_iso: str) -> dict:
    """Alerts fired in the window, counted from the SHADOW EVENT LOG.

    SOURCE CHOICE — `shadow_events_current`, kind='alert.fired'. The three
    candidates and why this one:

      * `audit_logs` (vigil_alert_generated / relay_alert_generated) — written at
        exactly two call sites, both inside
        `ingestion_service._check_ingested_alert`. HTTP-ingest lane only. This is
        the source that produced the bug. It is retained below as
        `audit_trail`, explicitly labelled as one lane's view, because it is
        still the right thing to reconcile against for the ingest path.
      * `messages` — a Relay alert row IS written by both lanes
        (`bot_service._persist_alerts`, `ingestion_service._persist_relay_alert`),
        so it does not miss a lane. But it counts DELIVERIES, not detections: an
        alert that fired and was cooled down, dropped, or failed to persist
        leaves no row, and the table carries no level, no lane and no patient_id
        (see shadow_service's module docstring) — so it cannot be broken down and
        cannot be reconciled. It also holds every human chat message, so
        isolating alerts means string-matching the alert body.
      * `shadow_events` — written by the simulator lane
        (bot_service → shadow_service.arecord_batch(..., "simulator")) AND by the
        ingest lane (ingestion_service → shadow_service.aemit("alert.fired", ...,
        "ingestion")). It carries an explicit `lane` column whose vocabulary is a
        CHECK constraint, carries the level, is append-only at the database
        layer, and writes ONE timestamp format by construction (see
        shadow_service.utc_now) so a window comparison is sound.

    The property that decides it: a lane cannot be silently missed here. To write
    an alert row at all a producer must NAME ITSELF in `lane`, so a lane added
    tomorrow appears as a new key in `by_lane` rather than disappearing from the
    total. The audit-log source had the opposite property — a new lane is
    invisible and the total still looks plausible.

    THE ONE THING THIS SOURCE CANNOT DO is report anything when the instrument is
    switched off (ORB_SHADOW_ENABLED=0 — shadow_service._enabled). Alerts keep
    firing; nothing records them. That is reported as `total: None`, i.e. UNKNOWN,
    with the reason. It is never reported as 0.
    """
    def _unknown(reason: str) -> dict:
        return {
            "total": None,
            "known": False,
            "unknown_reason": reason,
            "by_lane": {},
            "by_level": {},
            "source": "shadow_events",
        }

    off_reason = _shadow_off_reason()
    if off_reason:
        return _unknown(off_reason)

    try:
        lane_rows = db.execute(
            """SELECT lane, COUNT(*) AS cnt
                 FROM shadow_events_current
                WHERE kind = 'alert.fired' AND occurred_at >= ?
             GROUP BY lane""",
            (cutoff_iso,),
        ).fetchall()
        level_rows = db.execute(
            """SELECT COALESCE(json_extract(payload_json, '$.level'), 'unspecified') AS level,
                      COUNT(*) AS cnt
                 FROM shadow_events_current
                WHERE kind = 'alert.fired' AND occurred_at >= ?
             GROUP BY level""",
            (cutoff_iso,),
        ).fetchall()
    except DBOperationalError as exc:
        # Missing view/table (migration 4 not applied) is a "cannot know", not a
        # zero. Same rule as the switch being off.
        logger.warning("admin overview: shadow event log unreadable: %s", exc)
        return _unknown(f"The shadow event log could not be read ({exc}).")

    by_lane = {r["lane"]: r["cnt"] for r in lane_rows}
    by_level = {lvl: 0 for lvl in _ALERT_LEVELS}
    by_level.update({r["level"]: r["cnt"] for r in level_rows})
    return {
        "total": sum(by_lane.values()),
        "known": True,
        "unknown_reason": None,
        "by_lane": by_lane,
        "by_level": by_level,
        "source": "shadow_events",
    }


def _records_written_24h(db, cutoff_iso: str) -> dict:
    """Clinical rows written in the window, counted from the CLINICAL TABLES.

    SOURCE CHOICE — `vitals` + `observations`, grouped by their `source` column.
    `ingestion_events.vitals_inserted/observations_inserted` (the old source) is
    the HTTP ingest endpoint's own receipt: the simulator lane, the nurse's
    manual observation set and the bedside-monitor sink all write clinical rows
    without ever creating an ingestion_events row. The destination tables are the
    one place every lane must land, and `vitals.source` / `observations.source`
    name the producer, so a new lane shows up as a new key in `by_source`.

    TIMESTAMPS. This repo has two live formats — Python `.isoformat()` (with a
    'T' and an offset) and SQLite `CURRENT_TIMESTAMP` (a space, no offset) — and
    a naive `col >= ?` string compare silently drops every row of the other
    format on the boundary day. Both sides therefore go through SQLite's
    `datetime()`, which normalises both and converts an offset to UTC.

    `datetime()` cannot use an index, so a cheap lexicographic PRE-FILTER runs
    first: every format begins 'YYYY-MM-DD', and the largest real UTC offset is
    under 24 h, so nothing inside a 24-hour window can sort below the date 48 h
    back. The bulk of the table is rejected by a string compare and only the
    tail pays for the conversion.

    WHY THIS CAN STILL BE UNKNOWN, twice over:

      * `vitals` is not an append-only ledger for the simulator lane. bot_service
        prunes `source='simulated'` rows to MAX_VITALS_PER_PATIENT (2,000) every
        ~5 minutes; at a 5-second tick that is ~2.8 hours of history, so on a box
        that has been simulating for a day the rows retained inside a 24-hour
        window are a FLOOR, not a count.
      * a row whose timestamp `datetime()` cannot parse yields NULL, and
        `NULL >= x` is NULL, so the row is dropped from the count in silence.
        Those rows are counted separately and reported.

    In both cases reporting the floor as though it were the total would be a
    smaller version of exactly the lie this function was written to remove, so
    `total` is None (unknown) and `counted` carries the floor for the UI to
    render as "at least N".
    """
    result: dict = {
        "total": None,
        "counted": 0,
        "vitals_counted": 0,
        "observations_counted": 0,
        "by_source": {},
        "complete": False,
        "incomplete_reason": None,
        "source": "clinical_tables",
    }
    prefilter = _window_prefilter(cutoff_iso)

    try:
        vitals_rows = db.execute(
            """SELECT COALESCE(source, 'unknown') AS src,
                      SUM(CASE WHEN datetime(timestamp) >= datetime(?) THEN 1 ELSE 0 END) AS cnt,
                      SUM(CASE WHEN datetime(timestamp) IS NULL THEN 1 ELSE 0 END) AS bad_ts
                 FROM vitals
                WHERE timestamp >= ?
             GROUP BY src""",
            (cutoff_iso, prefilter),
        ).fetchall()
        # `recorded_at` is WRITE time (what this tile asks about); `observed_at`
        # is the clinical time the result belongs to and can predate the write by
        # hours on a lab backfill.
        obs_rows = db.execute(
            """SELECT COALESCE(source, 'unknown') AS src,
                      SUM(CASE WHEN datetime(COALESCE(recorded_at, observed_at)) >= datetime(?)
                               THEN 1 ELSE 0 END) AS cnt,
                      SUM(CASE WHEN datetime(COALESCE(recorded_at, observed_at)) IS NULL
                               THEN 1 ELSE 0 END) AS bad_ts
                 FROM observations
                WHERE COALESCE(recorded_at, observed_at) >= ?
             GROUP BY src""",
            (cutoff_iso, prefilter),
        ).fetchall()
    except DBOperationalError as exc:  # pragma: no cover - defensive
        logger.warning("admin overview: clinical row counts unreadable: %s", exc)
        result["incomplete_reason"] = f"The clinical tables could not be read ({exc})."
        return result

    by_source: dict[str, int] = {}
    vitals_n = obs_n = unparseable = 0
    for rows, bucket in ((vitals_rows, "vitals"), (obs_rows, "observations")):
        for r in rows:
            cnt = int(r["cnt"] or 0)
            if cnt:
                by_source[r["src"]] = by_source.get(r["src"], 0) + cnt
            unparseable += int(r["bad_ts"] or 0)
            if bucket == "vitals":
                vitals_n += cnt
            else:
                obs_n += cnt

    result.update({
        "counted": vitals_n + obs_n,
        "vitals_counted": vitals_n,
        "observations_counted": obs_n,
        "by_source": by_source,
    })

    truncated, reason = _simulated_history_truncated(db, cutoff_iso)
    if truncated:
        result["incomplete_reason"] = reason
        return result  # total stays None — unknown, not the floor and not zero.
    if unparseable:
        result["incomplete_reason"] = (
            f"{unparseable} clinical row(s) near this window carry a timestamp Orb "
            "cannot parse, so they can be neither counted nor ruled out. The figure "
            "shown is a floor, not a total."
        )
        return result

    result["complete"] = True
    result["total"] = vitals_n + obs_n
    return result


def _window_prefilter(cutoff_iso: str) -> str:
    """A date-only lower bound safe to compare against ANY stored format.

    Every timestamp this repo writes starts 'YYYY-MM-DD', whatever separator or
    offset follows, so a plain string compare against a date 24 h below the
    window start can never exclude a row that belongs in the window (the largest
    real UTC offset is +14:00 / −12:00, both under 24 h). It exists purely to
    keep the `datetime()` conversion off the bulk of the table.
    """
    try:
        return (datetime.fromisoformat(cutoff_iso) - timedelta(hours=24)).strftime("%Y-%m-%d")
    except ValueError:  # pragma: no cover - defensive
        return "0000-01-01"


def _simulated_history_truncated(db, cutoff_iso: str) -> tuple[bool, str | None]:
    """Has the simulator's row-cap prune removed vitals from inside the window?

    True only when BOTH hold for at least one patient: the retained
    `source='simulated'` row count is at the cap (so the prune has run on them),
    AND the oldest retained row is NEWER than the window start (so what it
    removed was inside the window). A freshly seeded box, or one where the cap
    still spans more than the window, is reported as complete and gets an exact
    number.
    """
    try:
        from services.bot_service import MAX_VITALS_PER_PATIENT as cap
    except Exception:  # pragma: no cover - defensive
        return False, None
    try:
        row = db.execute(
            """SELECT COUNT(*) AS n FROM (
                   SELECT patient_id
                     FROM vitals
                    WHERE source = 'simulated'
                 GROUP BY patient_id
                   HAVING COUNT(*) >= ?
                      AND MIN(datetime(timestamp)) > datetime(?)
               )""",
            (int(cap), cutoff_iso),
        ).fetchone()
    except DBOperationalError:  # pragma: no cover - defensive
        return False, None
    affected = int(row["n"] or 0) if row else 0
    if not affected:
        return False, None
    return True, (
        f"{affected} simulated patient(s) are at the {cap}-row retention cap and "
        "hold no history reaching back 24h, so bot_service has pruned rows from "
        "inside this window. The figure shown is a floor, not a total."
    )


@router.get("/overview")
def admin_overview(user: dict = Depends(require_permission("admin.overview"))):
    """Pre-aggregated governance snapshot for the Admin dashboard."""

    now = datetime.now(timezone.utc)
    cutoff_24h = (now - timedelta(hours=24)).isoformat()
    # shadow_events writes ONE format — ISO-8601 UTC at millisecond resolution
    # (shadow_service.utc_now) — and sorts lexicographically, so its window
    # bound is generated at the same precision and compared as a plain string.
    cutoff_24h_ms = (now - timedelta(hours=24)).isoformat(timespec="milliseconds")

    with db_session() as db:
        # ── Audit counts (24h) by category ──
        audit_by_category = db.execute(
            """SELECT event_category, COUNT(*) as cnt
               FROM audit_logs WHERE timestamp >= ?
               GROUP BY event_category""",
            (cutoff_24h,),
        ).fetchall()

        audit_total_24h = sum(r["cnt"] for r in audit_by_category)
        audit_categories = {r["event_category"]: r["cnt"] for r in audit_by_category}

        # ── Audit counts by outcome (24h) ──
        audit_by_outcome = db.execute(
            """SELECT outcome, COUNT(*) as cnt
               FROM audit_logs WHERE timestamp >= ?
               GROUP BY outcome""",
            (cutoff_24h,),
        ).fetchall()
        audit_outcomes = {r["outcome"]: r["cnt"] for r in audit_by_outcome}

        # ── Ingestion summary (24h) ──
        ingestion_summary = db.execute(
            """SELECT
                 COUNT(*) as total,
                 SUM(CASE WHEN parse_status = 'parsed' THEN 1 ELSE 0 END) as parsed,
                 SUM(CASE WHEN parse_status = 'failed' THEN 1 ELSE 0 END) as failed,
                 SUM(CASE WHEN parse_status = 'duplicate' THEN 1 ELSE 0 END) as duplicates,
                 SUM(COALESCE(vitals_inserted, 0)) as total_vitals,
                 SUM(COALESCE(observations_inserted, 0)) as total_observations
               FROM ingestion_events
               WHERE received_at >= ?""",
            (cutoff_24h,),
        ).fetchone()

        # ── Source mix (24h) ──
        source_mix = db.execute(
            """SELECT source_type, COUNT(*) as cnt
               FROM ingestion_events WHERE received_at >= ?
               GROUP BY source_type""",
            (cutoff_24h,),
        ).fetchall()
        sources = {r["source_type"]: r["cnt"] for r in source_mix}

        # ── Alerts (24h) — LANE-AGNOSTIC. See _alerts_24h for the source choice. ──
        alerts = _alerts_24h(db, cutoff_24h_ms)

        # ── Records written (24h) — LANE-AGNOSTIC. See _records_written_24h. ──
        records_written = _records_written_24h(db, cutoff_24h)

        # The audit trail's own view of alerting. KEPT, but scoped honestly: these
        # two action_types are written only by ingestion_service, so they describe
        # the HTTP ingest lane and nothing else. Useful to reconcile that lane
        # against the instrument; useless as a headline.
        alert_audit_rows = db.execute(
            """SELECT action_type, COUNT(*) as cnt
               FROM audit_logs
               WHERE timestamp >= ?
                 AND action_type IN ('vigil_alert_generated', 'relay_alert_generated')
               GROUP BY action_type""",
            (cutoff_24h,),
        ).fetchall()
        alert_audit = {r["action_type"]: r["cnt"] for r in alert_audit_rows}

        # ── Recent parse failures (last 5) ──
        recent_failures = db.execute(
            """SELECT id, received_at, source_type, parse_error, correlation_id
               FROM ingestion_events
               WHERE parse_status = 'failed'
               ORDER BY received_at DESC LIMIT 5"""
        ).fetchall()

        # ── Auth events (24h) ──
        # auth events are written with event_category='authentication' /
        # 'authorization' (see routes/auth_routes.py, permissions.py) — the
        # previous 'auth' filter matched nothing, so this summary was always [].
        auth_summary = db.execute(
            """SELECT action_type, outcome, COUNT(*) as cnt
               FROM audit_logs
               WHERE timestamp >= ?
                 AND event_category IN ('authentication', 'authorization')
               GROUP BY action_type, outcome""",
            (cutoff_24h,),
        ).fetchall()
        auth_events = [{"action": r["action_type"], "outcome": r["outcome"], "count": r["cnt"]} for r in auth_summary]

    return {
        "ingestion_mode": INGESTION_MODE,
        "audit_simulation_enabled": AUDIT_SIMULATION_EVENTS,
        "audit_total_24h": audit_total_24h,
        "audit_categories": audit_categories,
        "audit_outcomes": audit_outcomes,
        "ingestion": {
            "total": ingestion_summary["total"] or 0,
            "parsed": ingestion_summary["parsed"] or 0,
            "failed": ingestion_summary["failed"] or 0,
            "duplicates": ingestion_summary["duplicates"] or 0,
            "total_vitals": ingestion_summary["total_vitals"] or 0,
            "total_observations": ingestion_summary["total_observations"] or 0,
        },
        "source_mix": sources,
        # `total: null` means UNKNOWN and must render as unknown — never as 0.
        # `by_lane` names every lane that produced an alert, so a lane that was
        # added without telling this endpoint shows up instead of vanishing.
        "alerts": {
            **alerts,
            "audit_trail": {
                "vigil": alert_audit.get("vigil_alert_generated", 0),
                "relay": alert_audit.get("relay_alert_generated", 0),
                "lane": "ingestion",
                "note": (
                    "These two audit action_types are written only by the HTTP "
                    "ingest lane; they are not a hospital-wide alert count."
                ),
            },
        },
        # Same contract: `total: null` = unknown, `counted` is a floor the UI
        # renders as "at least N".
        "records_written": records_written,
        "recent_failures": [dict(r) for r in recent_failures],
        "auth_events": auth_events,
    }


# ════════════════════════════════════════════════════════════════
#  Alerts list: the rows behind the Overview's "Alert Activity" tile
# ════════════════════════════════════════════════════════════════
# The Alerts tab used to read audit_logs category 'ingestion' (first 200 rows,
# filtered in the browser). Those rows are written by the HTTP ingest lane
# only, so on a simulator or manual-lane box the tab said "No alert events"
# while the Overview counted 68 in the same 24 hours. This lists the SAME
# source the tile counts (see _alerts_24h), paged on the server.

_ALERT_LIST_MAX_HOURS = 24 * 7


@router.get("/alerts")
def admin_alerts(
    request: Request,
    hours: int = Query(24, ge=1, le=_ALERT_LIST_MAX_HOURS),
    lane: Optional[str] = Query(None),
    level: Optional[str] = Query(None),
    limit: int = Query(25, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: dict = Depends(require_permission("admin.overview")),
):
    """Every alert.fired row in the window, newest first, with lane and level.

    Same contract as the tile: when the shadow log is off the answer is
    `total: null` with the reason, never an empty list read as "no alerts".
    The rows name patients, so a ward-scoped account sees its own ward only
    (with the house-wide count beside it) and every page of names is audited.
    """
    base = {
        "hours": hours,
        "limit": limit,
        "offset": offset,
        "lane": lane,
        "level": level,
        "source": "shadow_events",
    }
    off_reason = _shadow_off_reason()
    if off_reason:
        return {**base, "known": False, "unknown_reason": off_reason, "total": None,
                "alerts": [], "by_lane": {}, "scope": None, "house_total": None}

    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(
        timespec="milliseconds")
    where = ["kind = 'alert.fired'", "occurred_at >= ?"]
    params: list = [cutoff]
    if lane:
        where.append("lane = ?")
        params.append(lane)
    if level:
        where.append("json_extract(payload_json, '$.level') = ?")
        params.append(level)

    dept = department_scope(user)
    scoped_where, scoped_params = list(where), list(params)
    if dept is not None:
        scope_sql, scope_args = scope_patient_ids_sql(dept)
        scoped_where.append(f"patient_id IN ({scope_sql})")
        scoped_params.extend(scope_args)

    try:
        with db_session() as db:
            clause = " AND ".join(scoped_where)
            total = db.execute(
                f"SELECT COUNT(*) AS n FROM shadow_events_current WHERE {clause}",
                scoped_params,
            ).fetchone()["n"]
            lane_rows = db.execute(
                f"SELECT lane, COUNT(*) AS n FROM shadow_events_current WHERE {clause} "
                "GROUP BY lane",
                scoped_params,
            ).fetchall()
            rows = db.execute(
                f"""SELECT id, episode_id, occurred_at, lane, patient_id, ward,
                           json_extract(payload_json, '$.level') AS level,
                           json_extract(payload_json, '$.raw_news2') AS raw_news2,
                           json_extract(payload_json, '$.repeat') AS repeat
                      FROM shadow_events_current
                     WHERE {clause}
                  ORDER BY occurred_at DESC, id DESC
                     LIMIT ? OFFSET ?""",
                [*scoped_params, limit, offset],
            ).fetchall()
            house_total = None
            if dept is not None:
                # A count names nobody; it stops a quiet ward reading as a quiet house.
                house_total = db.execute(
                    f"SELECT COUNT(*) AS n FROM shadow_events_current "
                    f"WHERE {' AND '.join(where)}",
                    params,
                ).fetchone()["n"]
    except DBOperationalError as exc:
        logger.warning("admin alerts: shadow event log unreadable: %s", exc)
        return {**base, "known": False,
                "unknown_reason": f"The shadow event log could not be read ({exc}).",
                "total": None, "alerts": [], "by_lane": {}, "scope": dept,
                "house_total": None}

    alerts = [
        {
            "id": r["id"],
            "episode_id": r["episode_id"],
            "occurred_at": r["occurred_at"],
            "lane": r["lane"],
            "level": r["level"],
            "raw_news2": r["raw_news2"],
            "repeat": bool(r["repeat"]),
            "patient_id": r["patient_id"],
            "ward": r["ward"],
        }
        for r in rows
    ]
    if alerts:
        patient_ids = sorted({a["patient_id"] for a in alerts if a["patient_id"]})
        log_phi_read(
            request=request,
            user=user,
            action_type="admin_alerts_viewed",
            summary_message=(
                f"Admin alert list viewed ({len(alerts)} alerts, "
                f"{len(patient_ids)} patients) for {dept or 'all departments'}"
            ),
            target_type="department",
            target_id=dept,
            metadata={"result_count": len(alerts), "patient_ids": patient_ids,
                      "hours": hours, "offset": offset},
        )

    return {
        **base,
        "known": True,
        "unknown_reason": None,
        "total": total,
        "alerts": alerts,
        "by_lane": {r["lane"]: r["n"] for r in lane_rows},
        "scope": dept,
        "house_total": house_total,
    }


# ════════════════════════════════════════════════════════════════
#  Patient erasure: the legacy one-click route is RETIRED
# ════════════════════════════════════════════════════════════════
# POST /patients/{id}/erase used to delete every patient table except
# audit_logs in one call: no retention check, no legal hold, no minor floor and
# no erasure_requests row. That is the outcome services/consent_service.py
# calls illegal under Indian medical-record law (THE COLLISION). DPDP erasure
# now goes through /api/admin/compliance/erasure only: record the request,
# adjudicate it against retention_policies (refusals carry their citation),
# then execute the honoured categories as a separate act.
#
# _build_erase_plan stays because a destruction on a signed order, once the
# retention period has lapsed, will need exactly this schema-derived plan. No
# route reaches it today. It is DERIVED FROM THE LIVE SCHEMA rather than
# hand-maintained: the hard-coded list it replaced fell a dozen migrations
# behind and left full PHI in patient_snapshots, observations, sage_insights,
# orders and care_bundles while reporting success.

# Tables that DO carry a patient_id column (or reach one through a foreign key)
# but are deliberately kept, each with the reason a destruction order must be
# told. Every one except audit_logs is append-only at the storage layer (a
# BEFORE DELETE trigger that RAISEs ABORT), so a plan that included it aborted
# the whole transaction on the first patient with a row there: any patient the
# ingestion lane had ever written a shadow row for could not be erased at all.
# Kept is not the same as harmless: shadow_events.phi_json can carry a
# recorder's own words about this patient, so a destruction route must report
# these tables as retained rather than claim the patient is gone.
#
# ONE list, owned by services/consent_service.py, whose DPDP erasure already
# reports it in every execution result ("retained"). A second copy here would
# drift the way the hand-kept delete list did.
_ERASE_RETAINED: dict[str, str] = ERASURE_RETAINED_TABLES
_ERASE_KEEP_TABLES: frozenset[str] = frozenset(_ERASE_RETAINED)

# Patient-scoped rows the schema walk cannot discover: they have no patient_id
# column AND no foreign key into a patient-scoped table, so neither pass of
# _build_erase_plan() can see them. Runs BEFORE the derived plan — event_log is
# event_deliveries' parent in fact but not in schema (no FK is declared), so the
# ordering has to be asserted here.
_ERASE_UNLINKED_DELETES: list[tuple[str, str, int]] = [
    ("event_deliveries",
     "DELETE FROM event_deliveries WHERE event_id IN (SELECT event_id FROM event_log WHERE patient_id = ?)", 1),
    # world_model_state is keyed generically (entity_type/entity_id) rather than
    # by a patient_id column; state_json is PHI-derived (ciphertext when field
    # encryption is on, which is retention of personal data either way).
    ("world_model_state",
     "DELETE FROM world_model_state WHERE entity_type = 'patient' AND entity_id = ?", 1),
]


def _build_erase_plan(db: sqlite3.Connection) -> list[tuple[str, str, int]]:
    """Derive the ordered erasure DELETE plan from the live schema.

    Two passes over sqlite_master:

    1. every table with a ``patient_id`` column (minus _ERASE_KEEP_TABLES) is
       purged directly. Aggregate tables (hospital_forecasts, admission_history)
       have no such column and so are excluded by construction, as intended;
    2. the transitive closure of tables reaching one of those through a foreign
       key — messages→channels, order_events→orders,
       helix_transitions→helix_patient_med_orders, … — is purged through a
       subquery on the parent's own predicate.

    Statements are ordered children-before-parents so FK enforcement
    (PRAGMA foreign_keys=ON) never trips, and the child subqueries still see
    their parent rows intact when they run.

    Returns ``(table, sql, param_count)``; the SQL takes the patient id repeated
    ``param_count`` times (>1 only when a child reaches the patient through more
    than one patient-scoped parent).
    """
    tables = [
        r[0] for r in db.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    ]

    columns: dict[str, list[str]] = {}
    fks: dict[str, list[tuple[str, str, str]]] = {}  # child -> [(parent, from_col, to_col)]
    for table in tables:
        try:
            info = db.execute(f'PRAGMA table_info("{table}")').fetchall()
            fk_rows = db.execute(f'PRAGMA foreign_key_list("{table}")').fetchall()
        except DBOperationalError:
            # A virtual table whose module is not loaded (sqlite-vec shadow
            # tables) cannot be introspected; it holds no patient rows.
            continue
        columns[table] = [c[1] for c in info]
        fks[table] = [(r[2], r[3], r[4] or "id") for r in fk_rows]

    predicates: dict[str, tuple[str, int]] = {
        table: ("patient_id = ?", 1)
        for table, cols in columns.items()
        if "patient_id" in cols and table not in _ERASE_KEEP_TABLES
    }

    # Transitive closure. Bounded rather than run to a true fixpoint so an
    # accidental FK cycle terminates instead of growing predicates forever; the
    # schema is only two hops deep (helix_transitions→orders→patient).
    derived: dict[str, tuple[str, int]] = {}
    for _ in range(8):
        changed = False
        known = {**predicates, **derived}
        for table in sorted(columns):
            if table in predicates or table in _ERASE_KEEP_TABLES:
                continue
            clauses: list[str] = []
            params = 0
            for parent, from_col, to_col in fks.get(table, []):
                parent_pred = known.get(parent)
                if parent_pred is None or parent == table:
                    continue
                clauses.append(
                    f'"{from_col}" IN (SELECT "{to_col}" FROM "{parent}" WHERE {parent_pred[0]})'
                )
                params += parent_pred[1]
            if clauses:
                candidate = (" OR ".join(clauses), params)
                if derived.get(table) != candidate:
                    derived[table] = candidate
                    changed = True
        if not changed:
            break
    predicates.update(derived)

    # Topological order: emit every child before the parent it references.
    ordered: list[str] = []
    visited: set[str] = set()

    def visit(table: str) -> None:
        if table in visited:
            return
        visited.add(table)  # marked on entry, so an FK cycle cannot recurse forever
        for child, child_fks in fks.items():
            if child in predicates and any(parent == table for parent, _, _ in child_fks):
                visit(child)
        ordered.append(table)

    for table in sorted(predicates):
        visit(table)

    return [(t, f'DELETE FROM "{t}" WHERE {predicates[t][0]}', predicates[t][1]) for t in ordered]


@router.post("/patients/{patient_id}/erase")
def erase_patient(
    patient_id: str,
    request: Request,
    user: dict = Depends(require_permission("admin.overview")),
):
    """RETIRED: always 410 Gone, and deletes nothing.

    Kept as a route rather than removed so a stale tab or an old script gets a
    plain answer instead of a bare 404, and so the attempt lands on the audit
    trail. `security` is fail-closed; a refusal that cannot be recorded fails
    loudly, and either way nothing is erased.
    """
    log_audit_event(
        correlation_id=str(uuid.uuid4()),
        event_category="security",
        action_type="patient_erase_refused",
        outcome="denied",
        summary_message=(
            f"Retired one-click erasure refused for patient {patient_id}; DPDP "
            "erasure goes through an adjudicated erasure request"
        ),
        actor_user_id=user.get("id"),
        actor_role=user.get("role"),
        target_type="patient",
        target_id=patient_id,
        patient_id=patient_id,
        request_path=request.url.path,
        http_method=request.method,
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )
    raise HTTPException(
        status_code=410,
        detail=(
            "This way of erasing a patient has been retired and nothing was "
            "erased. Open an erasure request instead: it is checked against the "
            "records the hospital must keep by law before anything is removed."
        ),
    )


# ════════════════════════════════════════════════════════════════
#  Bulk historical record import (legacy EHR → Orb)
# ════════════════════════════════════════════════════════════════
# Orb REPLACES the incumbent EHR, so cutover means migrating years of clinical
# history out of a system nobody here designed. services/history_import_service.py
# holds the reasoning; these endpoints are the four-step flow that makes it safe:
#
#   GET  /history-import/record-types   what can be imported, and into what
#   GET  /history-import/fields         the target fields for one record type
#   POST /history-import/preview        upload → delimiter, headers, SUGGESTED
#                                       mapping, sample rows, header candidates
#   POST /history-import/dry-run        upload + confirmed mapping → the exact
#                                       create/update/skip plan and EVERY failed
#                                       row with its reason. Touches nothing.
#   POST /history-import/commit         same inputs → executes in ONE transaction,
#                                       writes the run ledger and the complete
#                                       reject list
#   GET  /history-import/runs           import history
#   GET  /history-import/runs/{id}/rejects   the durable, paged work list of rows
#                                            that did NOT come across
#
# DRY RUN IS NOT OPTIONAL IN PRACTICE. The commit path recomputes the plan
# server-side from the uploaded bytes rather than trusting a plan posted back by
# the client, so commit can never write something a dry run of the same file and
# mapping did not show — but an admin who skips the dry run has skipped their
# only chance to read the failure list before the write.
#
# Auth: every endpoint is gated on ``admin.overview``, the permission held only
# by the ``admin`` role. A doctor cannot bulk-write clinical history from a
# spreadsheet. Every endpoint that touches PHI writes an audit row.
#
# Event loop: handlers are ``async def`` because reading a multipart upload is
# async, but ALL parsing and DB work goes through ``database.run_db`` onto the
# private DB pool. A 200 MB import must not stall the ward's screens.

# Where uploads are spooled. Deliberately NOT the system temp dir by default:
# the upload is PHI in clear text, and the operator has secured the volume the
# database lives on (encryption at rest, backup policy, disk disposal), not
# /tmp. Overridable for operators who mount something better.
def _spool_dir() -> str:
    from config import DB_PATH
    configured = os.environ.get("ORB_IMPORT_SPOOL_DIR")
    path = configured or os.path.join(os.path.dirname(os.path.abspath(str(DB_PATH))),
                                      ".import-spool")
    os.makedirs(path, mode=0o700, exist_ok=True)
    return path


async def _spool_upload(file: UploadFile) -> tuple[str, int]:
    """Stream an upload to a private temp file and return (path, bytes).

    Chunked rather than ``await file.read()`` because a historical export can be
    hundreds of megabytes and reading it whole would hold all of it in the API
    process — the failure mode the service layer streams specifically to avoid.
    ``mkstemp`` creates the file 0600, so it is not world-readable while it
    exists; the caller MUST delete it in a finally, PHI does not linger.
    """
    fd, path = tempfile.mkstemp(prefix="orb_history_import_", suffix=".csv",
                                dir=_spool_dir())
    total = 0
    try:
        with os.fdopen(fd, "wb") as out:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > history_import.MAX_BYTES:
                    raise HTTPException(
                        status_code=413,
                        detail=f"File exceeds the "
                               f"{history_import.MAX_BYTES // (1024 * 1024)} MB limit.",
                    )
                out.write(chunk)
    except BaseException:
        _discard(path)
        raise
    if total == 0:
        _discard(path)
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")
    return path, total


def _discard(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:  # pragma: no cover — already gone
        logger.debug("history import spool file already removed: %s", path)


def _parse_mapping(mapping: str) -> dict[str, str]:
    """Parse the JSON mapping form field into {orb_field: csv_header}."""
    try:
        parsed = json.loads(mapping or "{}")
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=422, detail=f"mapping must be valid JSON: {e}")
    if not isinstance(parsed, dict):
        raise HTTPException(
            status_code=422,
            detail="mapping must be a JSON object of {orb_field: csv_column}")
    out: dict[str, str] = {}
    for k, v in parsed.items():
        if v is None or v == "":
            continue
        if not isinstance(k, str) or not isinstance(v, str):
            raise HTTPException(status_code=422,
                                detail="mapping keys and values must both be strings")
        out[k] = v
    return out


def _history_audit(request: Request, user: dict, action_type: str, summary: str,
                   metadata: dict, target_id: str | None = None) -> None:
    log_audit_event(
        correlation_id=f"history-import-{target_id or 'preview'}-{id(request):x}",
        event_category="data_management",
        action_type=action_type,
        outcome="success",
        summary_message=summary,
        actor_user_id=user.get("id"),
        actor_role=user.get("role"),
        target_type="history_import",
        target_id=target_id,
        request_path=request.url.path,
        http_method=request.method,
        ip_address=request.client.host if request.client else None,
        metadata=metadata,
    )


@router.get("/history-import/record-types")
def history_import_record_types(user: dict = Depends(require_permission(ADMIN_OVERVIEW))):
    """What can be imported, and the limits that apply."""
    return {
        "record_types": history_import.record_type_catalogue(),
        "date_orders": ["auto", "dmy", "mdy"],
        "max_bytes": history_import.MAX_BYTES,
        "max_rows": history_import.MAX_ROWS,
    }


@router.get("/history-import/fields")
def history_import_fields(record_type: str,
                          user: dict = Depends(require_permission(ADMIN_OVERVIEW))):
    """Target fields one record type can populate, for the mapping UI."""
    try:
        return {"record_type": record_type,
                "fields": history_import.field_catalogue(record_type)}
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))


@router.post("/history-import/preview")
async def history_import_preview(
    request: Request,
    file: UploadFile = File(...),
    record_type: str = Form(...),
    header_line: int | None = Form(None),
    user: dict = Depends(require_permission(ADMIN_OVERVIEW)),
):
    """Sniff an export: delimiter, headers, suggested mapping, sample rows.

    Reads nothing from the clinical tables and writes nothing anywhere. Answers
    even when the header row is ambiguous — ``header_confident: false`` plus
    ``header_candidates`` is how the admin is asked which line holds the column
    names, because reading a report title as the header shifts every column.
    """
    path, size = await _spool_upload(file)
    try:
        result = await run_db(history_import.sniff, path, record_type,
                              header_line=header_line)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    finally:
        _discard(path)
    result["filename"] = file.filename
    _history_audit(request, user, "history_import_preview",
                   f"History import preview of '{file.filename}' as {record_type}",
                   {"filename": file.filename, "record_type": record_type,
                    "bytes": size, "sha256": result.get("source_sha256")})
    return result


@router.post("/history-import/dry-run")
async def history_import_dry_run(
    request: Request,
    file: UploadFile = File(...),
    record_type: str = Form(...),
    source_system: str = Form(...),
    mapping: str = Form(...),
    date_order: str = Form("auto"),
    header_line: int | None = Form(None),
    user: dict = Depends(require_permission(ADMIN_OVERVIEW)),
):
    """Report exactly what an import WOULD create, update and skip. Writes nothing.

    ``counts`` and ``issue_summary`` are exact over every row read. ``rows`` and
    ``failures`` are capped for transport and say so — the commit step persists
    the complete reject list regardless, so nothing is ever only visible here.
    """
    parsed_mapping = _parse_mapping(mapping)
    path, size = await _spool_upload(file)
    try:
        result = await run_db(
            history_import.plan, path, record_type, parsed_mapping,
            source_system=source_system, date_order=date_order, header_line=header_line,
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    finally:
        _discard(path)

    result["filename"] = file.filename
    _history_audit(request, user, "history_import_dry_run",
                   f"History import dry run of '{file.filename}' as {record_type} "
                   f"from '{source_system}': {result['counts']}",
                   {"filename": file.filename, "record_type": record_type,
                    "source_system": source_system, "counts": result["counts"],
                    "issue_summary": result["issue_summary"],
                    "mapping": parsed_mapping, "date_order": date_order,
                    "bytes": size, "sha256": result.get("source_sha256")})
    return result


@router.post("/history-import/commit")
async def history_import_commit(
    request: Request,
    file: UploadFile = File(...),
    record_type: str = Form(...),
    source_system: str = Form(...),
    mapping: str = Form(...),
    date_order: str = Form("auto"),
    header_line: int | None = Form(None),
    note: str | None = Form(None),
    user: dict = Depends(require_permission(ADMIN_OVERVIEW)),
):
    """Apply the import in ONE transaction. Idempotent on the source's own ids.

    The plan is recomputed server-side from the uploaded bytes rather than taken
    from the client, so a commit can never write something the dry run did not
    show for the same file and mapping. Every rejected row is written to the
    durable reject ledger, which is the work list the hospital gets back.
    """
    parsed_mapping = _parse_mapping(mapping)
    path, size = await _spool_upload(file)
    try:
        result = await run_db(
            history_import.apply_import, path, record_type, parsed_mapping,
            source_system=source_system, actor_user_id=user["id"],
            source_filename=file.filename, date_order=date_order,
            header_line=header_line, note=note,
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except DBIntegrityError as e:
        # The whole import is one transaction, so NOTHING was written. Report the
        # constraint rather than a bare 500 — it is almost always a duplicate
        # external id or MRN that appeared between the dry run and the commit.
        logger.warning("History import rejected by a DB constraint: %s", e)
        raise HTTPException(
            status_code=409,
            detail=f"Import rejected by a database constraint and NOTHING was written: {e}",
        )
    finally:
        _discard(path)

    _history_audit(request, user, "history_import_committed",
                   f"History import '{file.filename}' as {record_type} from "
                   f"'{source_system}' applied: {result['summary']}",
                   {"filename": file.filename, "record_type": record_type,
                    "source_system": source_system, "summary": result["summary"],
                    "issue_summary": result["issue_summary"],
                    "mapping": parsed_mapping, "date_order": date_order,
                    "bytes": size, "sha256": result.get("source_sha256"),
                    "note": note},
                   target_id=result["run_id"])
    return result


@router.get("/history-import/runs")
async def history_import_runs(
    limit: int = 50,
    record_type: str | None = None,
    user: dict = Depends(require_permission(ADMIN_OVERVIEW)),
):
    """Applied-import history, newest first. Dry runs are not recorded here."""
    return {"runs": await run_db(history_import.list_runs, limit, record_type)}


@router.get("/history-import/runs/{run_id}/rejects")
async def history_import_rejects(
    run_id: str,
    request: Request,
    limit: int = 500,
    offset: int = 0,
    user: dict = Depends(require_permission(ADMIN_OVERVIEW)),
):
    """Every row a run did NOT import, paged, with its line number and reason.

    This is the deliverable of a migration as much as the imported rows are: a
    migration that silently drops records is a catastrophe found months later by
    a clinician looking for something that is simply not there. Paged rather
    than capped, so nothing is unreachable however large the file was.

    The rows quote the offending source line verbatim, so this is PHI — hence
    admin-only and audited on every read.
    """
    result = await run_db(history_import.list_rejects, run_id, limit, offset)
    if not result:
        raise HTTPException(status_code=404, detail="Import run not found")
    _history_audit(request, user, "history_import_rejects_read",
                   f"Read {len(result['rejects'])} reject row(s) for import run {run_id}",
                   {"run_id": run_id, "count": result["count"],
                    "limit": limit, "offset": offset},
                   target_id=run_id)
    return result
