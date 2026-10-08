"""Orb Sage — Persistent, versioned world model (temporal state).

Today Sage reassembles context from raw SQL on every turn, which gives a
snapshot but no MEMORY: it can't say "this patient's SpO2 has been sliding for
six hours" or "risk has escalated twice today" without re-deriving trends each
time, and nothing persists across turns. This service maintains an evolving,
versioned per-patient and per-ward state computed from the time-series tables,
so Sage reasons over a MAINTAINED model instead of a fresh snapshot — the
difference between "reads the chart fast" and "understands the trajectory".

What it adds over _assemble_patient_context:
  * TREND signals: linear slope + direction (rising/falling/flat) for HR, SpO2,
    RR, temperature over the recent window, plus a derived overall trajectory
    label (improving / stable / worsening) from the risk-score history.
  * VERSIONING: each update writes a new row (monotonic version per entity), so
    the evolution is auditable and a diff between versions is possible.

PHI posture: state is PHI-derived, so state_json is application-layer AES-GCM
encrypted at rest (services/field_crypto) when a key is configured — mitigating
the deferred whole-DB encryption gap for this NEW store. Reads decrypt
transparently; with no key the value is cleartext, exactly as the rest of the DB
today (see database.py encryption note).
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone

from database import db_session
from services.field_crypto import decrypt_field, encrypt_field, is_field_encryption_enabled

logger = logging.getLogger("orb.world_model")

HOSPITAL_ID = "__hospital__"
_VITAL_FIELDS = ("heart_rate", "spo2", "respiratory_rate", "temperature")


def _int_env(name: str, default: int, minimum: int) -> int:
    """Read a positive int from the environment, falling back to `default` on
    anything unparseable. Never returns below `minimum` — a retention bound of
    0 would delete the row we just wrote."""
    try:
        return max(minimum, int(os.getenv(name, "") or default))
    except (TypeError, ValueError):
        return default


# ── Retention ──
# world_model_state is written on every risk/vitals transition and read ONLY as
# `ORDER BY version DESC LIMIT 1` (_load_latest below is the sole production
# reader). Unbounded, it reached 58,188 rows / 60 MB of state_json in its first
# week on ~30 patients, and every one of those bytes is copied by
# consistent_backup, walked by integrity_check and re-decrypted by SQLCipher on
# any full scan. `_persist` therefore prunes its own entity's tail on every
# write.
#
# KEEP is per (entity_type, entity_id), so the table's ceiling is
# entities x KEEP rows rather than time x write-rate. 20 versions is a real
# history window for "how did this state evolve" debugging while capping the
# busiest patient observed (3,786 versions) at ~20 KB.
_KEEP_VERSIONS = _int_env("ORB_WORLD_MODEL_KEEP_VERSIONS", 20, 1)

# The prune is deliberately BATCHED. It runs inside the same write transaction
# as the INSERT — cheap, no second WAL lock acquisition — but that means its
# lock hold is on a clinical write path. An unbatched first-run delete against
# an existing backlog would be thousands of rows in one statement; every other
# writer in the appliance (vitals ingestion, eMAR, the audit chain) queues
# behind it on SQLite's single write lock. Capping the batch makes the worst
# case constant instead of proportional to however large the backlog grew, and
# the backlog simply drains over successive writes to that entity.
_PRUNE_BATCH = _int_env("ORB_WORLD_MODEL_PRUNE_BATCH", 200, 1)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _slope(values: list[float]) -> float:
    """Least-squares slope of evenly-spaced values (oldest→newest). Positive =
    rising. Returns 0 for <2 points."""
    n = len(values)
    if n < 2:
        return 0.0
    xs = list(range(n))
    mean_x = sum(xs) / n
    mean_y = sum(values) / n
    denom = sum((x - mean_x) ** 2 for x in xs)
    if denom == 0:
        return 0.0
    return sum((xs[i] - mean_x) * (values[i] - mean_y) for i in range(n)) / denom


def _direction(slope: float, eps: float) -> str:
    if slope > eps:
        return "rising"
    if slope < -eps:
        return "falling"
    return "flat"


# Per-vital "meaningful change per reading" epsilon for direction labelling.
_TREND_EPS = {"heart_rate": 1.0, "spo2": 0.5, "respiratory_rate": 0.5, "temperature": 0.1}


def compute_patient_state(patient_id: str) -> dict | None:
    """Derive the current temporal state for one patient from the time-series
    tables. Read-only; returns None if the patient doesn't exist."""
    with db_session() as db:
        pat = db.execute(
            "SELECT id, name, ward, bed, diagnosis, status FROM patients WHERE id = ?",
            (patient_id,),
        ).fetchone()
        if not pat:
            return None
        # Oldest→newest window of the last 8 vitals.
        vit_rows = db.execute(
            """SELECT heart_rate, spo2, respiratory_rate, temperature, timestamp
               FROM vitals WHERE patient_id = ? AND voided_at IS NULL
               ORDER BY timestamp DESC, id DESC LIMIT 8""",
            (patient_id,),
        ).fetchall()
        # Scoring switched off (ORB_SCORING_ENABLED, the Orb Ward Record): no
        # band stored before the switch is read, so the state names no risk
        # level and no level history.
        from services.vigil_service import scoring_enabled
        risk_rows = db.execute(
            """SELECT score, level, timestamp FROM risk_scores
               WHERE patient_id = ? ORDER BY timestamp DESC, id DESC LIMIT 6""",
            (patient_id,),
        ).fetchall() if scoring_enabled() else []

    vit_rows = list(reversed(vit_rows))  # oldest→newest
    trends = {}
    for f in _VITAL_FIELDS:
        series = [float(r[f]) for r in vit_rows if r[f] is not None]
        if len(series) >= 2:
            s = _slope(series)
            trends[f] = {
                "direction": _direction(s, _TREND_EPS[f]),
                "slope_per_reading": round(s, 3),
                "first": round(series[0], 1),
                "latest": round(series[-1], 1),
                "n": len(series),
            }

    # Risk trajectory from the level history (newest first in risk_rows).
    rank = {"stable": 0, "elevated": 1, "critical": 2}
    levels = [(r["level"] or "stable").lower() for r in risk_rows]
    current_level = levels[0] if levels else "unknown"
    trajectory = "stable"
    if len(levels) >= 2:
        newest, older = rank.get(levels[0], 0), rank.get(levels[-1], 0)
        if newest > older:
            trajectory = "worsening"
        elif newest < older:
            trajectory = "improving"
    # A physiologic worsening signal reinforces the label even without a risk flip.
    worsening_signals = sum(
        1 for f, t in trends.items()
        if (f == "spo2" and t["direction"] == "falling")
        or (f in ("heart_rate", "respiratory_rate", "temperature") and t["direction"] == "rising")
    )
    if worsening_signals >= 2 and trajectory == "stable":
        trajectory = "worsening"

    return {
        "patient_id": patient_id,
        "name": pat["name"],
        "ward": pat["ward"],
        "bed": pat["bed"],
        "diagnosis": pat["diagnosis"],
        "current_risk_level": current_level,
        "risk_level_history": levels,
        "vital_trends": trends,
        "trajectory": trajectory,
        "worsening_signals": worsening_signals,
        "computed_at": _now(),
    }


# How many deteriorating patients the ward state NAMES. `worsening_count` is
# always the true total; this only bounds the list that carries PHI (name, bed)
# into a hospital-wide summary.
_WARD_WORSENING_LIMIT = _int_env("ORB_WARD_WORSENING_LIMIT", 10, 1)

# Severity ordering for that list. Higher = more urgent.
_LEVEL_RANK = {"critical": 3, "elevated": 2, "stable": 1, "unknown": 0}


def compute_ward_state() -> dict:
    """Derive hospital-level temporal state (counts + how many are worsening).

    The `worsening` list is ranked MOST URGENT FIRST before it is truncated.
    That ordering is load-bearing, not cosmetic: the scan below walks patients
    in SQLite's rowid order for `status='admitted'` — i.e. roughly admission
    order, which correlates with nothing clinical. Truncating that raw order
    means the ward's "who is deteriorating" list is an arbitrary sample of the
    deteriorating, so a `critical` patient can be omitted while ten patients
    with no risk score at all are shown, with `worsening_count` reporting a
    larger number than the list explains. Ranking first makes the patients
    dropped by the cut the LEAST urgent ones, and makes the list deterministic
    for a given clinical picture rather than dependent on row order.

    Ties break on worsening_signals, then patient_id — so the output is stable
    across runs instead of varying with how the rows happen to be laid out.
    """
    with db_session() as db:
        admitted = [r["id"] for r in db.execute(
            "SELECT id FROM patients WHERE status = 'admitted'"
        ).fetchall()]
    dist = {"critical": 0, "elevated": 0, "stable": 0, "unknown": 0}
    ranked = []
    for pid in admitted:
        st = compute_patient_state(pid)
        if not st:
            continue
        level = st["current_risk_level"]
        dist[level] = dist.get(level, 0) + 1
        if st["trajectory"] == "worsening":
            ranked.append((
                -_LEVEL_RANK.get(level, 0),
                -st["worsening_signals"],
                pid,
                {"patient_id": pid, "name": st["name"], "bed": st["bed"], "level": level},
            ))
    ranked.sort(key=lambda r: (r[0], r[1], r[2]))
    worsening = [r[3] for r in ranked]
    return {
        "census": len(admitted),
        "risk_distribution": dist,
        "worsening_count": len(worsening),
        "worsening": worsening[:_WARD_WORSENING_LIMIT],
        "computed_at": _now(),
    }


# ── Persistence (versioned + optionally field-encrypted) ──

def _persist(entity_type: str, entity_id: str, state: dict, reason: str, summary: str) -> int:
    payload = json.dumps(state)
    enc = is_field_encryption_enabled()
    stored = encrypt_field(payload) if enc else payload
    with db_session() as db:
        row = db.execute(
            "SELECT MAX(version) AS v FROM world_model_state WHERE entity_type=? AND entity_id=?",
            (entity_type, entity_id),
        ).fetchone()
        version = (row["v"] or 0) + 1
        db.execute(
            """INSERT INTO world_model_state
               (entity_type, entity_id, version, state_json, encrypted, summary, updated_by, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (entity_type, entity_id, version, stored, 1 if enc else 0, summary[:200], reason, _now()),
        )
        _prune_versions(db, entity_type, entity_id, version)
    return version


def _prune_versions(db, entity_type: str, entity_id: str, newest_version: int) -> int:
    """Drop this entity's oldest versions beyond _KEEP_VERSIONS. Returns the
    number of rows deleted.

    Scoped to ONE entity and driven off the version we just assigned, so the
    whole statement is an index range scan on idx_world_model_latest
    (entity_type, entity_id, version DESC) — no table scan, no window function
    over the full table. Batched: see _PRUNE_BATCH.

    Runs on the caller's open transaction (`db`), never its own session — a
    second db_session() here would take a second WAL lock while the INSERT's is
    still held.

    Never raises: retention is housekeeping and must not fail a state write.
    """
    cutoff = newest_version - _KEEP_VERSIONS
    if cutoff < 1:
        return 0
    try:
        cur = db.execute(
            """DELETE FROM world_model_state
               WHERE id IN (
                   SELECT id FROM world_model_state
                   WHERE entity_type = ? AND entity_id = ? AND version <= ?
                   ORDER BY version ASC LIMIT ?
               )""",
            (entity_type, entity_id, cutoff, _PRUNE_BATCH),
        )
        return cur.rowcount or 0
    except Exception as e:
        logger.warning(
            "world_model prune failed for %s/%s: %s", entity_type, entity_id, e
        )
        return 0


def _load_latest(entity_type: str, entity_id: str) -> dict | None:
    with db_session() as db:
        row = db.execute(
            """SELECT state_json, encrypted, version, updated_by, created_at
               FROM world_model_state WHERE entity_type=? AND entity_id=?
               ORDER BY version DESC LIMIT 1""",
            (entity_type, entity_id),
        ).fetchone()
    if not row:
        return None
    try:
        raw = decrypt_field(row["state_json"]) if row["encrypted"] else row["state_json"]
        state = json.loads(raw)
        state["_version"] = row["version"]
        state["_updated_by"] = row["updated_by"]
        return state
    except Exception as e:
        logger.warning("world_model load/decrypt failed for %s/%s: %s", entity_type, entity_id, e)
        return None


def update_patient_state(patient_id: str, reason: str = "manual") -> dict | None:
    """Recompute and persist a new versioned patient state. Returns the state."""
    state = compute_patient_state(patient_id)
    if state is None:
        return None
    summary = f"{state['name']} {state['current_risk_level']} trajectory={state['trajectory']}"
    state["_version"] = _persist("patient", patient_id, state, reason, summary)
    return state


def update_ward_state(reason: str = "manual") -> dict:
    state = compute_ward_state()
    summary = f"census={state['census']} worsening={state['worsening_count']}"
    state["_version"] = _persist("ward", HOSPITAL_ID, state, reason, summary)
    return state


def get_patient_state(patient_id: str, recompute_if_missing: bool = True) -> dict | None:
    """Latest persisted patient state; computes+persists one if none exists.

    Scoring switched off (ORB_SCORING_ENABLED, the Orb Ward Record): computed
    afresh and not persisted, because a state persisted before the switch names
    the band stored then."""
    from services.vigil_service import scoring_enabled
    if not scoring_enabled():
        return compute_patient_state(patient_id)
    st = _load_latest("patient", patient_id)
    if st is None and recompute_if_missing:
        return update_patient_state(patient_id, reason="on_demand")
    return st


def get_ward_state(recompute_if_missing: bool = True) -> dict | None:
    # Scoring switched off: computed afresh, as get_patient_state above.
    from services.vigil_service import scoring_enabled
    if not scoring_enabled():
        return compute_ward_state()
    st = _load_latest("ward", HOSPITAL_ID)
    if st is None and recompute_if_missing:
        return update_ward_state(reason="on_demand")
    return st


def trajectory_line(patient_id: str) -> str:
    """A one-line PHI-scoped trajectory summary suitable for Channel A context
    (only surfaced on already-patient-scoped turns, so it adds no new egress)."""
    st = get_patient_state(patient_id)
    if not st:
        return ""
    parts = [f"TRAJECTORY: {st['trajectory']} (risk {st['current_risk_level']})"]
    notable = []
    for f, t in (st.get("vital_trends") or {}).items():
        if t["direction"] != "flat":
            notable.append(f"{f} {t['direction']} {t['first']}→{t['latest']}")
    if notable:
        parts.append("; ".join(notable))
    return " | ".join(parts)
