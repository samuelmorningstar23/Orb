"""Orb — Care Bundle (Sepsis Six / deterioration) timers.

Turns a Vigil deterioration alert into a measurable, time-boxed protocol. The
Sepsis Six bundle (take: bloods/cultures, lactate, urine output; give: oxygen,
IV antibiotics, IV fluids) has a 1-hour target. This service tracks element
completion and computes compliance, converting "we alert" into the
measurable-outcome ROI a pilot needs to demonstrate.

Clinical note: element targets below encode the widely-taught Sepsis Six /
Surviving Sepsis 1-hour bundle. They are configurable and must be confirmed
against the deploying hospital's own protocol.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

from database import db_session

logger = logging.getLogger("orb.sepsis_bundle")

# Bundle element → human label + target minutes from trigger.
_ELEMENTS: dict[str, dict] = {
    "oxygen":       {"label": "Give high-flow oxygen",       "target_min": 60},
    "cultures":     {"label": "Take blood cultures",          "target_min": 60},
    "antibiotics":  {"label": "Give IV antibiotics",          "target_min": 60},
    "fluids":       {"label": "Give IV fluids",               "target_min": 60},
    "lactate":      {"label": "Measure serum lactate",        "target_min": 60},
    "urine_output": {"label": "Monitor urine output",         "target_min": 60},
}
_ELEMENT_COLS = {k: f"{k}_at" for k in _ELEMENTS}

# What each bundle is called on a screen. `bundle_type` is a storage key, and
# title-casing it printed "Sepsis6" on the chart timeline.
BUNDLE_LABELS: dict[str, str] = {
    "sepsis6": "Sepsis Six",
    "deterioration": "Deterioration",
}


def bundle_label(bundle_type: str | None) -> str:
    key = str(bundle_type or "").strip().lower()
    if key in BUNDLE_LABELS:
        return BUNDLE_LABELS[key]
    words = key.replace("_", " ").strip()
    return words[:1].upper() + words[1:] if words else "Care"


_THRESHOLD_MARK = re.compile(r"\s*\(\s*>=\s*(\d+)\s*\)")


def trigger_reason_text(reason: str | None) -> str | None:
    """The auto-start reason as a clinician reads it. The stored criterion is
    "NEWS2 12 (>=7)" (kept as written: it is the record of why the bundle
    opened); every screen reads it as "NEWS2 12, at or above 7"."""
    if not reason:
        return reason
    return _THRESHOLD_MARK.sub(lambda m: f", at or above {m.group(1)}", str(reason))


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now().isoformat()


def start_bundle(patient_id: str, *, bundle_type: str = "sepsis6",
                 triggered_by: str | None = None, trigger_reason: str | None = None,
                 source: str = "clinician") -> dict:
    """Open a bundle for a patient. If an active bundle already exists, return it
    (idempotent — we don't stack concurrent bundles of the same type)."""
    with db_session() as db:
        existing = db.execute(
            "SELECT * FROM care_bundles WHERE patient_id = ? AND bundle_type = ? AND status = 'active' "
            "ORDER BY id DESC LIMIT 1",
            (patient_id, bundle_type),
        ).fetchone()
        if existing:
            return _decorate(dict(existing))
        cur = db.execute(
            """INSERT INTO care_bundles (patient_id, bundle_type, triggered_at, triggered_by, trigger_reason, status, source)
               VALUES (?, ?, ?, ?, ?, 'active', ?)""",
            (patient_id, bundle_type, _now_iso(), triggered_by, trigger_reason, source),
        )
        row = db.execute("SELECT * FROM care_bundles WHERE id = ?", (cur.lastrowid,)).fetchone()
    logger.info("Opened %s bundle for %s (%s)", bundle_type, patient_id, trigger_reason)
    return _decorate(dict(row))


class BundleNotActive(ValueError):
    """The bundle is completed or cancelled. Its own type so the route answers
    409 (the request was well formed, the bundle has moved on) rather than the
    400 an unknown element gets."""


def get_bundle(bundle_id: int) -> dict | None:
    """The raw bundle row, undecorated, or None. For resolving the patient (and
    so the ward scope) before a route writes anything."""
    with db_session() as db:
        row = db.execute("SELECT * FROM care_bundles WHERE id = ?", (bundle_id,)).fetchone()
    return dict(row) if row else None


def complete_element(bundle_id: int, element: str, *, actor_id: str | None = None) -> dict:
    """Mark a bundle element complete (stamps its timestamp). Auto-completes the
    bundle when every element is done.

    Refused on a bundle that is no longer active: stamping an element on a
    cancelled bundle wrote a completion time into a protocol nobody was running,
    and that time then counted toward the compliance figures."""
    if element not in _ELEMENTS:
        raise ValueError(f"Unknown bundle element: {element}")
    col = _ELEMENT_COLS[element]
    with db_session() as db:
        row = db.execute("SELECT * FROM care_bundles WHERE id = ?", (bundle_id,)).fetchone()
        if not row:
            raise ValueError("Bundle not found")
        if row["status"] != "active":
            raise BundleNotActive(
                f"This bundle is {row['status']}; its elements can no longer be marked done."
            )
        if row[col] is None:
            logger.info("Bundle %s element %s completed by %s", bundle_id, element, actor_id)
            db.execute(f"UPDATE care_bundles SET {col} = ? WHERE id = ?", (_now_iso(), bundle_id))
        # Re-read and check for completion
        row = dict(db.execute("SELECT * FROM care_bundles WHERE id = ?", (bundle_id,)).fetchone())
        if all(row[_ELEMENT_COLS[k]] is not None for k in _ELEMENTS) and row["status"] == "active":
            db.execute(
                "UPDATE care_bundles SET status = 'completed', completed_at = ? WHERE id = ?",
                (_now_iso(), bundle_id),
            )
            row = dict(db.execute("SELECT * FROM care_bundles WHERE id = ?", (bundle_id,)).fetchone())
    return _decorate(row)


def cancel_bundle(bundle_id: int, *, actor_id: str | None = None,
                  reason: str | None = None) -> bool:
    """Cancel an active bundle. The route requires the reason and writes the
    audit row (who, why); the table has no column for either."""
    with db_session() as db:
        cur = db.execute(
            "UPDATE care_bundles SET status = 'cancelled' WHERE id = ? AND status = 'active'",
            (bundle_id,),
        )
        cancelled = cur.rowcount > 0
    if cancelled:
        logger.info("Bundle %s cancelled by %s: %s", bundle_id, actor_id, reason)
    return cancelled


def get_active_bundles(department: str | None = None) -> list[dict]:
    """Active bundles, hospital-wide, or for one ADMISSION department when
    `department` is given (permissions.department_scope's value: None means an
    unscoped account, not "scope unknown")."""
    from permissions import scope_patient_ids_sql

    # The same subquery every other patient list scopes through, so a bundle
    # list cannot develop its own idea of which ward a patient is on.
    scope_sql, scope_params = scope_patient_ids_sql(department)
    # Admitted patients only. A bundle left open on a discharged patient (one
    # discharged before discharge_patient closed bundles, or by another path)
    # is not on the ward, and the Command Center counted it in compliance.
    with db_session() as db:
        rows = db.execute(
            f"""SELECT * FROM care_bundles
                 WHERE status = 'active' AND patient_id IN ({scope_sql})
                   AND patient_id IN (SELECT id FROM patients WHERE status = 'admitted')
                 ORDER BY triggered_at""",
            scope_params,
        ).fetchall()
    return _decorate_all(rows)


def get_bundles_for_patient(patient_id: str, active_only: bool = False) -> list[dict]:
    q = "SELECT * FROM care_bundles WHERE patient_id = ?"
    if active_only:
        q += " AND status = 'active'"
    q += " ORDER BY triggered_at DESC"
    with db_session() as db:
        rows = db.execute(q, (patient_id,)).fetchall()
    return _decorate_all(rows)


def _parse(ts) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


# ── Who opened the bundle ────────────────────────────────────────────────────
# The timer said "Trigger: Manual start" with no name, so a nurse arriving at
# the bedside could not tell who had started the clock or whether Orb had. The
# row stores `triggered_by` as a users.id for a clinician start and the marker
# "vigil_auto" for an automatic one; the name is resolved here, at read time,
# so a renamed account reads under its current name.

_AUTO_STARTERS = {"vigil_auto"}
_NEWS2_IN_REASON = re.compile(r"NEWS2\s+(\d+)")


def _scoring_enabled() -> bool:
    """ORB_SCORING_ENABLED through vigil_service, so the Ward Record profile
    (ORB_WARD_RECORD) switches it off here too. Read at call time."""
    from services.vigil_service import scoring_enabled
    return scoring_enabled()


def _is_auto_start(row: dict) -> bool:
    return (row.get("source") in _AUTO_STARTERS
            or row.get("triggered_by") in _AUTO_STARTERS)


def _starter_names(user_ids) -> dict[str, str]:
    """users.id to display name for the ids given, one query. Unknown ids are
    left out, and the caller prints no name rather than an id."""
    ids = sorted({str(u) for u in user_ids if u and str(u) not in _AUTO_STARTERS})
    if not ids:
        return {}
    marks = ",".join("?" for _ in ids)
    with db_session() as db:
        rows = db.execute(
            f"SELECT id, name FROM users WHERE id IN ({marks})", ids,
        ).fetchall()
    return {r["id"]: r["name"] for r in rows if r["name"]}


def _decorate_all(rows) -> list[dict]:
    """`_decorate` for a list of rows, with the starters' names in one query."""
    rows = [dict(r) for r in rows]
    names = _starter_names(r.get("triggered_by") for r in rows)
    return [_decorate(r, names=names) for r in rows]


def _decorate(row: dict, names: dict[str, str] | None = None) -> dict:
    """Attach per-element status + overall compliance to a bundle row."""
    # Read the stored criterion BEFORE it is reworded below: "NEWS2 12 (>=7)".
    raw_reason = row.get("trigger_reason")
    if _is_auto_start(row):
        row["started_by_name"] = "Orb"
        m = _NEWS2_IN_REASON.search(str(raw_reason or ""))
        row["trigger_news2"] = int(m.group(1)) if m else None
    else:
        starter = row.get("triggered_by")
        if names is None:
            names = _starter_names([starter])
        row["started_by_name"] = names.get(str(starter)) if starter else None
        row["trigger_news2"] = None
    started = _parse(row.get("triggered_at")) or _now()
    now = _now()
    elapsed_min = (now - started).total_seconds() / 60.0

    elements = []
    completed = 0
    breached = 0
    for key, meta in _ELEMENTS.items():
        done_at = _parse(row.get(_ELEMENT_COLS[key]))
        target = meta["target_min"]
        if done_at is not None:
            completed += 1
            done_min = (done_at - started).total_seconds() / 60.0
            state = "done_on_time" if done_min <= target else "done_late"
            if done_min > target:
                breached += 1
        else:
            # outstanding — overdue if past target
            state = "overdue" if elapsed_min > target else "pending"
            if state == "overdue":
                breached += 1
        elements.append({
            "key": key, "label": meta["label"], "target_min": target,
            "completed_at": row.get(_ELEMENT_COLS[key]), "state": state,
        })

    total = len(_ELEMENTS)
    on_time = sum(1 for e in elements if e["state"] == "done_on_time")
    row["bundle_label"] = bundle_label(row.get("bundle_type"))
    row["trigger_reason"] = trigger_reason_text(row.get("trigger_reason"))
    # Scoring switched off (ORB_SCORING_ENABLED, the Orb Ward Record): a
    # bundle Orb opened before the switch still shows, but not the stored
    # NEWS2 that opened it. A reason a clinician typed is kept.
    if not _scoring_enabled():
        row["trigger_news2"] = None
        if _is_auto_start(row):
            row["trigger_reason"] = None
    row["elapsed_min"] = round(elapsed_min, 1)
    row["elements"] = elements
    row["completed_count"] = completed
    row["total_count"] = total
    # completion_pct = how much of the bundle is done (regardless of timing).
    # compliance_pct = the headline 1-hour metric: elements done WITHIN target.
    # These differ when elements are completed late, so we never report 100%
    # compliance for a bundle that breached the 1-hour target.
    row["completion_pct"] = round(100.0 * completed / total, 0)
    row["compliance_pct"] = round(100.0 * on_time / total, 0)
    row["on_track"] = breached == 0
    return row


# ── Auto-trigger helper (safe to call from the risk path) ────────────────────
def autostart_enabled() -> bool:
    """ORB_SEPSIS_AUTOSTART, read at call time so a test or an operator flip is
    seen without re-importing. Off, no lane opens a bundle by itself; a
    clinician's own start_bundle call is unaffected."""
    import config
    return bool(getattr(config, "SEPSIS_AUTOSTART", True))


def maybe_autostart(patient_id: str, news2: dict) -> dict | None:
    """Open a sepsis bundle automatically when NEWS2 indicates high risk
    (aggregate >= 7, the RCP 'urgent clinical review' threshold). Idempotent and
    non-raising, so it is safe to call from the monitoring path.

    Returns None and starts nothing while ORB_SEPSIS_AUTOSTART is off."""
    if not autostart_enabled():
        return None
    try:
        raw = news2.get("raw_news2", 0) if isinstance(news2, dict) else 0
        if raw is not None and raw >= 7:
            return start_bundle(
                patient_id, triggered_by="vigil_auto",
                trigger_reason=f"NEWS2 {raw} (>=7)", source="vigil_auto",
            )
    except Exception as e:
        logger.warning("sepsis bundle auto-start failed for %s: %s", patient_id, e)
    return None
