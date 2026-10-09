"""Orb Hospital OS — Flight Recorder Service (A8 v1)

A replay/forensics layer over Orb's existing tamper-evident audit log and the
event bus write-ahead log. It answers three questions for an investigator:

  1. verify_chain()      — is the audit hash chain intact, and if not, WHERE did
                           it first break? (tamper detection)
  2. get_timeline()      — a single, time-ordered stream merging audit_logs,
                           event_log and order_events, so an admin can scrub
                           through everything that happened (to a patient, or
                           hospital-wide) and follow the causal chain.
  3. get_event_detail()  — the full raw row behind one timeline entry.

IMPORTANT — this module is READ-ONLY over the audit/event tables. The hashing
algorithm is NOT reimplemented here: verify_chain imports the exact
`compute_audit_row_hash` used by audit_service when writing rows, so a match here
is a real match against how the row was sealed. The hash chain is GLOBAL (each
row links to the immediately preceding audit row by id, regardless of patient),
so integrity is always verified over the full chain — a tampered row anywhere
invalidates provenance for everyone. The patient_id argument scopes the reported
row count/timeline, not the correctness of the chain walk.
"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime
from typing import Any, Optional

from config import DB_PATH
from database import db_session
# Reuse the EXACT sealing algorithm — do not reinvent it, or verification would
# silently diverge from how rows were written.
from services.audit_service import compute_audit_row_hash
from services.drug_chart import drug_chart_enabled as _drug_chart_enabled


# The columns (and their order) that audit_service persists and hashes. Selected
# explicitly so the dict handed to compute_audit_row_hash carries every hashed
# field under the same keys the writer used.
_AUDIT_SELECT = (
    "id, timestamp, correlation_id, event_category, actor_user_id, actor_role, "
    "target_type, target_id, patient_id, encounter_id, source_type, "
    "source_event_id, request_path, http_method, ip_address, user_agent, "
    "session_id, outcome, action_type, summary_message, metadata_json, "
    "prev_hash, row_hash"
)


def _parse_ts(value: Any) -> datetime:
    """Best-effort parse of a timestamp into a comparable datetime.

    Audit/event rows store ISO-8601 with a 'T' separator (and often a +00:00
    offset); SQLite CURRENT_TIMESTAMP rows (order_events) store
    'YYYY-MM-DD HH:MM:SS' with a space and no offset. Naive string sorting would
    interleave the two formats incorrectly, so we normalize to datetime for the
    merge sort. Unparseable/missing values sort to the epoch (very old).
    """
    if not value:
        return datetime.min
    s = str(value).strip().replace("Z", "+00:00")
    if " " in s and "T" not in s:
        s = s.replace(" ", "T", 1)
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return datetime.min
    # Drop tz-awareness so aware and naive rows are mutually comparable.
    return dt.replace(tzinfo=None)


# ════════════════════════════════════════════════════════════════
#  Chain integrity
# ════════════════════════════════════════════════════════════════

# Verifying re-hashes every audit row (about 100k on a pilot box), and the
# timeline, the badge and every event the scrubber lands on each asked for it,
# so a page load hashed the whole table several times and play mode re-hashed
# it every 900 ms. The walk is now cached and extended INCREMENTALLY over rows
# appended since the last one. Three things keep that from weakening the check:
#   * the cached prefix is re-fingerprinted on every call (row count up to the
#     cached tip, and the tip's own row_hash), so a deleted, inserted or
#     rewritten tip forces a full walk at once;
#   * the cache is only trusted for _CHAIN_CACHE_MAX_AGE_S, so an in-place edit
#     of an older row (the one move a fingerprint cannot see) still surfaces in
#     the timeline flags within that bound;
#   * verify_chain(), which drives the "Chain verified / Tampered at #N" badge,
#     always walks the full chain. Only the per-row flags read the cache.
_CHAIN_CACHE_MAX_AGE_S = 60.0

_chain_lock = threading.Lock()
_chain_cache: dict[str, Any] = {}


def _walk_audit_rows(rows, state: dict[str, Any]) -> None:
    """Extend a chain walk over `rows` (ascending id), updating `state` in place.

    Mirrors database.verify_audit_chain: legacy rows with NULL row_hash restart
    the chain (they predate hash sealing). Recomputes each row's hash from its
    stored prev_hash and confirms it matches row_hash and links to the prior row.
    """
    for r in rows:
        d = dict(r)
        state["tip_id"] = d["id"]
        state["tip_hash"] = d["row_hash"]
        state["rows_seen"] += 1
        if d["row_hash"] is None:
            # A NULL row_hash is only legitimate as a contiguous PREFIX of
            # pre-sealing legacy rows. Once ANY sealed (hashed) row has appeared,
            # a subsequent NULL hash means a sealed row's hash was erased — the
            # exact move used to hide a tampered row while letting the chain
            # "restart". Treat it as a break, and do NOT reset last_hash, so the
            # following row's prev_hash link mismatch is also caught.
            if state["sealing_began"]:
                state["broken"].append(d["id"])
                state["checked"] += 1
            else:
                state["last_hash"] = None  # legitimate pre-sealing legacy prefix
            continue
        state["sealing_began"] = True
        expected = compute_audit_row_hash(d, d.get("prev_hash"))
        last_hash = state["last_hash"]
        link_ok = (last_hash is None) or (d.get("prev_hash") == last_hash)
        if expected != d["row_hash"] or not link_ok:
            state["broken"].append(d["id"])
        state["checked"] += 1
        state["last_hash"] = d["row_hash"]


def _fresh_state() -> dict[str, Any]:
    return {"tip_id": 0, "tip_hash": None, "rows_seen": 0, "last_hash": None,
            "sealing_began": False, "broken": [], "checked": 0, "walked_at": None}


def _prefix_unchanged(conn, state: dict[str, Any]) -> bool:
    """Is the already-verified prefix still exactly the rows we walked?"""
    if not state["tip_id"]:
        return state["rows_seen"] == 0
    n = conn.execute("SELECT COUNT(id) AS n FROM audit_logs WHERE id <= ?",
                     (state["tip_id"],)).fetchone()["n"]
    tip = conn.execute("SELECT row_hash FROM audit_logs WHERE id = ?",
                       (state["tip_id"],)).fetchone()
    return n == state["rows_seen"] and tip is not None and tip["row_hash"] == state["tip_hash"]


def _chain_state(conn, *, max_age_s: Optional[float] = None) -> dict[str, Any]:
    """The chain walk, reusing the cached prefix when it is provably unchanged
    and younger than `max_age_s` (default _CHAIN_CACHE_MAX_AGE_S); otherwise a
    full walk. Returns a copy."""
    if max_age_s is None:
        max_age_s = _CHAIN_CACHE_MAX_AGE_S
    key = str(DB_PATH)
    with _chain_lock:
        state = _chain_cache.get(key)
        now = time.monotonic()
        if (state is None or state["walked_at"] is None
                or now - state["walked_at"] >= max_age_s
                or not _prefix_unchanged(conn, state)):
            state = _fresh_state()
            state["walked_at"] = now
        # Walk a copy and publish it only when complete, so a walk that fails
        # part-way never leaves rows marked as seen that were not checked.
        work = {**state, "broken": list(state["broken"])}
        rows = conn.execute(
            f"SELECT {_AUDIT_SELECT} FROM audit_logs WHERE id > ? ORDER BY id ASC",
            (work["tip_id"],),
        ).fetchall()
        _walk_audit_rows(rows, work)
        _chain_cache[key] = work
        return {**work, "broken": list(work["broken"])}


def _broken_audit_ids(conn, *, max_age_s: Optional[float] = None) -> tuple[list[int], int]:
    """(broken_ids, checked) over the FULL audit chain, in id order."""
    state = _chain_state(conn, max_age_s=max_age_s)
    return state["broken"], state["checked"]


def verify_chain(patient_id: Optional[str] = None) -> dict[str, Any]:
    """Verify the audit hash chain for tamper-evidence.

    Returns {ok, broken_at, count} where broken_at is the id of the FIRST row
    whose recomputed hash (or prev-link) fails, or None if the chain is intact.
    The chain is always walked globally (see module docstring); `count` reports
    the number of hashed rows checked — scoped to `patient_id` when supplied so
    the UI can show "N of this patient's events sit under a verified chain".

    Always a FULL walk (max_age_s=0): this is the tamper-evidence claim itself,
    so it never answers from the cache. The walk it does refreshes the cache the
    timeline and event detail then read.
    """
    with db_session() as conn:
        broken, checked = _broken_audit_ids(conn, max_age_s=0)
        if patient_id:
            count = conn.execute(
                "SELECT COUNT(id) AS c FROM audit_logs "
                "WHERE patient_id = ? AND row_hash IS NOT NULL",
                (patient_id,),
            ).fetchone()["c"]
        else:
            count = checked
    return {
        "ok": len(broken) == 0,
        "broken_at": broken[0] if broken else None,
        "count": count,
    }


# ════════════════════════════════════════════════════════════════
#  Unified timeline
# ════════════════════════════════════════════════════════════════

def _table_exists(conn, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?", (name,)
    ).fetchone()
    return row is not None


def get_timeline(
    patient_id: Optional[str] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
    limit: int = 500,
) -> list[dict[str, Any]]:
    """Return a time-ordered, unified stream of everything that happened.

    Merges three sources, each normalized to a common shape:
        {id, ts, source, actor, action, target, detail, chain_ok,
         patient_id, correlation_id}
      - source='audit' : audit_logs (chain_ok reflects this row's integrity)
      - source='event' : event_bus write-ahead log (event_log)
      - source='order' : order_events state transitions (chain_ok=None — these
                         are not part of the hash chain)

    Ascending by timestamp. When more than `limit` rows match, the most recent
    `limit` are returned (still ascending) so the scrubber shows the latest
    window. `since`/`until` are ISO-8601 bounds compared against each source's
    native timestamp column.
    """
    limit = max(1, min(int(limit), 5000))
    events: list[dict[str, Any]] = []

    with db_session() as conn:
        broken_ids = set(_broken_audit_ids(conn)[0])

        # ── audit_logs ──
        q = f"SELECT {_AUDIT_SELECT} FROM audit_logs WHERE 1=1"
        params: list[Any] = []
        if patient_id:
            q += " AND patient_id = ?"
            params.append(patient_id)
        if since:
            q += " AND timestamp >= ?"
            params.append(since)
        if until:
            q += " AND timestamp <= ?"
            params.append(until)
        q += " ORDER BY timestamp DESC, id DESC LIMIT ?"
        params.append(limit)
        for r in conn.execute(q, params).fetchall():
            d = dict(r)
            target = None
            if d.get("target_type") or d.get("target_id"):
                target = f"{d.get('target_type') or ''}:{d.get('target_id') or ''}".strip(":")
            elif d.get("request_path"):
                target = d.get("request_path")
            events.append({
                "id": d["id"],
                "ts": d["timestamp"],
                "source": "audit",
                "actor": d.get("actor_user_id") or d.get("actor_role"),
                "action": d.get("action_type"),
                "target": target,
                "detail": d.get("summary_message"),
                "chain_ok": d["row_hash"] is not None and d["id"] not in broken_ids,
                "patient_id": d.get("patient_id"),
                "correlation_id": d.get("correlation_id"),
            })

        # ── event_log (event bus WAL) ──
        q = ("SELECT event_id, topic, source_node, event_type, patient_id, "
             "payload, timestamp, correlation_id FROM event_log WHERE 1=1")
        params = []
        if patient_id:
            q += " AND patient_id = ?"
            params.append(patient_id)
        if since:
            q += " AND timestamp >= ?"
            params.append(since)
        if until:
            q += " AND timestamp <= ?"
            params.append(until)
        q += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)
        for r in conn.execute(q, params).fetchall():
            d = dict(r)
            events.append({
                "id": d["event_id"],
                "ts": d["timestamp"],
                "source": "event",
                "actor": d.get("source_node"),
                "action": d.get("event_type"),
                "target": d.get("topic"),
                "detail": _summarize_payload(d.get("payload")),
                "chain_ok": None,  # event_log is not hash-chained
                "patient_id": d.get("patient_id"),
                "correlation_id": d.get("correlation_id"),
            })

        # ── order_events (optional table) ──
        if _table_exists(conn, "order_events"):
            q = ("SELECT oe.id AS id, oe.order_id AS order_id, oe.prior_status AS prior_status, "
                 "oe.new_status AS new_status, oe.actor_id AS actor_id, oe.reason AS reason, "
                 "oe.created_at AS created_at, o.patient_id AS patient_id, "
                 "o.description AS description "
                 "FROM order_events oe LEFT JOIN orders o ON o.id = oe.order_id WHERE 1=1")
            params = []
            if not _drug_chart_enabled():
                # A medication order is the drug chart, which an Orb Ward
                # Record computer keeps on paper (services/drug_chart): its
                # status changes are left out, as the chart timeline leaves
                # them out. Every other order stays.
                q += " AND COALESCE(o.order_type, '') != 'medication'"
            if patient_id:
                q += " AND o.patient_id = ?"
                params.append(patient_id)
            if since:
                q += " AND oe.created_at >= ?"
                params.append(since)
            if until:
                q += " AND oe.created_at <= ?"
                params.append(until)
            q += " ORDER BY oe.created_at DESC, oe.id DESC LIMIT ?"
            params.append(limit)
            for r in conn.execute(q, params).fetchall():
                d = dict(r)
                transition = f"{d.get('prior_status') or 'new'} → {d.get('new_status')}"
                events.append({
                    "id": d["id"],
                    "ts": d["created_at"],
                    "source": "order",
                    "actor": d.get("actor_id"),
                    "action": f"order_{d.get('new_status')}",
                    "target": d.get("order_id"),
                    "detail": d.get("reason") or f"{d.get('description') or 'order'}: {transition}",
                    "chain_ok": None,  # order_events is not hash-chained
                    "patient_id": d.get("patient_id"),
                    "correlation_id": None,
                })

    # Merge + time-order ascending, then keep the most recent `limit`.
    events.sort(key=lambda e: (_parse_ts(e["ts"]), str(e["source"]), str(e["id"])))
    if len(events) > limit:
        events = events[-limit:]
    return events


def _summarize_payload(payload: Any) -> Optional[str]:
    """Compact one-line summary of an event_log JSON payload for the timeline."""
    if not payload:
        return None
    try:
        data = json.loads(payload) if isinstance(payload, str) else payload
    except (ValueError, TypeError):
        return str(payload)[:200]
    if isinstance(data, dict):
        parts = [f"{k}={v}" for k, v in list(data.items())[:4]]
        return ", ".join(parts)
    return str(data)[:200]


# ════════════════════════════════════════════════════════════════
#  Single-event detail
# ════════════════════════════════════════════════════════════════

def get_event_detail(event_id: Any, source: str) -> Optional[dict[str, Any]]:
    """Return the full raw row behind one timeline entry, plus chain status.

    `source` selects the backing table ('audit' | 'event' | 'order'). Returns
    None if no such row exists. For audit rows the returned dict includes a
    `chain_ok` flag so the detail pane can flag a tampered record.
    """
    with db_session() as conn:
        if source == "audit":
            row = conn.execute(
                f"SELECT {_AUDIT_SELECT} FROM audit_logs WHERE id = ?", (event_id,)
            ).fetchone()
            if row is None:
                return None
            d = dict(row)
            broken = set(_broken_audit_ids(conn)[0])
            d["chain_ok"] = d["row_hash"] is not None and d["id"] not in broken
            if d.get("metadata_json"):
                try:
                    d["metadata"] = json.loads(d["metadata_json"])
                except (ValueError, TypeError):
                    pass
            return {"source": "audit", "event": d}

        if source == "event":
            row = conn.execute(
                "SELECT * FROM event_log WHERE event_id = ?", (event_id,)
            ).fetchone()
            if row is None:
                return None
            d = dict(row)
            if d.get("payload"):
                try:
                    d["payload_parsed"] = json.loads(d["payload"])
                except (ValueError, TypeError):
                    pass
            return {"source": "event", "event": d}

        if source == "order":
            if not _table_exists(conn, "order_events"):
                return None
            row = conn.execute(
                "SELECT oe.*, o.patient_id AS patient_id, o.description AS order_description, "
                "o.order_type AS order_type "
                "FROM order_events oe LEFT JOIN orders o ON o.id = oe.order_id "
                "WHERE oe.id = ?",
                (event_id,),
            ).fetchone()
            if row is None:
                return None
            if row["order_type"] == "medication" and not _drug_chart_enabled():
                # Not on the timeline under the Orb Ward Record, so not here.
                return None
            return {"source": "order", "event": dict(row)}

    return None
