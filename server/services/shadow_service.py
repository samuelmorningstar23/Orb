"""Orb — Shadow event log (the pilot instrument).

THE WRITE PATH for `shadow_events`: episode correlation + the emit API that the
Vigil / Sage / Relay call sites use. The aggregation API and the UI live
elsewhere (`shadow_scorecard_service`, routes) and read the
`shadow_events_current` VIEW, never the base table.

Three rules govern every line in this file, in this order:

 1. **A shadow write must never fail a clinical action.** This log is the
    observational record, not the compliance record (`audit_logs` is that, and
    stays fail-closed). Every public entry point swallows its own exceptions and
    returns None. If the instrument breaks, the hospital keeps working and the
    breakage is counted in `STATS`.
 2. **Never on the event loop.** Since encryption-at-rest every page I/O pays
    HMAC-SHA512 + AES; a `db_session()` on the loop stalls the whole API. Async
    callers use `aemit`/`aemit_many`/`aopen_episode`, which go through
    `run_db()`. The sync forms are for code that is ALREADY on a worker thread.
 3. **Bounded.** The episode registry is a capped LRU, payloads are key-
    whitelisted per kind and byte-capped, and the fire-and-forget background
    emitter refuses to accumulate more than `_MAX_INFLIGHT` tasks.

── The self-deadlock rule (read this before adding a call site) ───────────────
If the caller ALREADY holds an open write transaction on this thread, it MUST
pass `db=<that connection>`. Opening a second connection from inside an open
write transaction blocks on the WAL write lock the caller itself holds — a
same-thread self-deadlock that can only end at busy_timeout (30 s). Same rule
that already governs `log_audit_event` (see ingestion_service._insert_observation).

── Correlation ───────────────────────────────────────────────────────────────
Episodes are THREADED, not reconstructed by join. `messages` has no patient_id,
no level, no FK to the vital that fired it; `correlation_id` overlap between
audit_logs and event_log is literally zero rows; bot_service writes no audit
rows and the ingestion path never publishes to the event bus. There is no table
in which both halves of an episode appear, so an `episode_id` is minted once at
the semantic birth of the alert and carried.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import threading
from collections import OrderedDict
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import Any

from database import db_session
from db_backend import DBIntegrityError

logger = logging.getLogger("orb.shadow")


# ── Configuration ────────────────────────────────────────────────────────────
#: Master switch. Off => every emit is a no-op that still returns cleanly, so a
#: broken instrument can be disabled in the field without a redeploy.
def _enabled() -> bool:
    return os.getenv("ORB_SHADOW_ENABLED", "true").strip().lower() in ("1", "true", "yes", "on")


def _episode_window_min() -> int:
    """Long enough that a 4 a.m. flag and a 7 a.m. ICU transfer are ONE episode;
    short enough that two unrelated deteriorations three days apart are not."""
    try:
        return max(1, int(os.getenv("ORB_SHADOW_EPISODE_WINDOW_MIN", "240")))
    except ValueError:
        return 240


EPISODE_REGISTRY_MAX = 512
MAX_PAYLOAD_BYTES = 4096
MAX_PHI_BYTES = 8192
_MAX_INFLIGHT = 256

# ── Controlled vocabulary (mirrors the CHECK constraint in migration 4) ──────
LANES = frozenset({"simulator", "ingestion", "chat", "voice", "manual", "sweeper"})

#: payload_json is STRUCTURED ONLY and key-whitelisted per kind. Free clinical
#: prose goes to phi_json or — better — is not copied at all: subject_id points
#: at the row that already holds it. The shadow log stores *facts about*
#: clinical content, never clinical content.
PAYLOAD_KEYS: dict[str, frozenset[str]] = {
    "episode.opened": frozenset({"trigger", "level", "raw_news2", "reopened"}),
    "episode.closed": frozenset({"window_min", "events_n", "reason"}),
    "alert.fired": frozenset(
        {
            "level",
            "raw_news2",
            "score",
            "drivers",
            "single_param_3",
            "spo2_scale",
            "vital_id",
            "repeat",
        }
    ),
    "alert.suppressed": frozenset({"level", "raw_news2", "suppressed_by", "seconds_since_last"}),
    "alert.dropped": frozenset({"reason", "level"}),
    "alert.persisted": frozenset({"message_id", "channel_id"}),
    "alert.delivered": frozenset({"channel_id", "recipients", "transport"}),
    "context.compiled": frozenset({"facts_n", "guidelines_n", "compile_ms", "trigger"}),
    "sage.drafted": frozenset(
        {
            "conversation_id",
            "citation_basis",
            "confidence",
            "retrieved_doc_ids",
            "cited_doc_ids",
            "trust_tiers",
            "ttft_ms",
            "retrieval_ms",
            "total_ms",
            "query_class",
            "cancelled",
            # THE TWO TIMING FACTS THE PILOT'S LATENCY CLAIM IS MADE OF, added
            # 2026-08-14 with the alert assessment's tier split. `ttft_ms` is when
            # the first TOKEN existed, which nobody can read; `first_step_ms` is when
            # the first complete, sanitized suggestion reached the surface. `depth`
            # separates a capped three-step alert draft from the four-section
            # handover assessment — without it the scorecard's time-saved figure
            # averages two workloads that never happen together.
            "first_step_ms",
            "depth",
            # ── THE PATIENT-SPECIFIC ADVICE TIER, added 2026-08-15 ──────────────
            # The tier now says only what depends on THIS patient's chart, so its
            # commonest correct output is silence — and the pilot has to be able to
            # tell TWO KINDS OF SILENCE APART. "Orb checked and there was nothing to
            # flag" and "the check never ran" are OPPOSITE facts about a
            # deteriorating patient, and an empty flag list reports them as the same
            # number. `patient_specific` is therefore the outcome WORD, not a count:
            #   flags | no_candidates | no_material | failed
            # Without it a scorecard reading "advice delivered on 12% of alerts"
            # cannot say whether the tier is working exactly as designed or is
            # broken on 88% of the ward, which are opposite conclusions from one
            # figure — the same trap the old 22%/0% delivery number sprang.
            "patient_specific",
            # How many cautions survived to the screen, and how many candidate facts
            # the DETERMINISTIC pre-screen produced before any model saw them. The
            # ratio is how we learn whether the pre-screen is too tight or too loose
            # without reading a single patient's chart.
            "patient_specific_n",
            "patient_specific_candidates",
            # THE THROUGHPUT FACT THE RULING PREDICTED. On most patients the
            # pre-screen finds nothing and the model is never called, which is what
            # dissolves the old 22% delivery problem as a side effect. This boolean
            # is the only honest way to report "what fraction of a realistic ward
            # actually triggers a model call" — inferring it from timings would make
            # a fast draft and a skipped one indistinguishable.
            "patient_specific_model_called",
            # What the post-filters dropped, and why. `dropped_generic` is a line
            # that restated the alert; `dropped_unsourced` is the model naming a
            # drug or a value the pre-screen never supplied — i.e. reaching into its
            # own memory, which is the failure mode this tier's whole design exists
            # to make impossible. Both are counted rather than merely suppressed:
            # a filter nobody measures is a filter nobody notices going quiet.
            "patient_specific_dropped_generic",
            "patient_specific_dropped_unsourced",
            # Cost of the deterministic pre-screen alone (no model in the path).
            "patient_specific_ms",
        }
    ),
    "sage.refused": frozenset({"refusal_class", "stage"}),
    "sage.failed": frozenset({"failure"}),
    "action.proposed": frozenset(
        {
            "action_class",
            "status_code",
            "supported",
            "gate",
            "target_patient_present",
            "drug_rxnorm",
            "dose_present",
            "conversation_id",
        }
    ),
    "action.approved": frozenset({"action_class", "order_id", "decline_reason"}),
    "action.declined": frozenset({"action_class", "order_id", "decline_reason"}),
    "action.abandoned": frozenset({"action_class", "window_min"}),
    "alert.viewed": frozenset({"message_id", "channel_id", "seconds_since_persisted"}),
    "alert.acknowledged": frozenset({"message_id", "disposition"}),
    "alert.expired": frozenset({"message_id", "window_min", "viewers"}),
    "clinical.action": frozenset({"action_class", "source_table", "minutes_after_alert"}),
    "outcome.candidate": frozenset({"event_type", "source", "detected_by"}),
    "outcome.recorded": frozenset({"event_type", "recognised_by", "source", "confirmed_by_role"}),
    "outcome.none_confirmed": frozenset({"ward", "period_start", "period_end", "shift"}),
    "adjudication.recorded": frozenset(
        {"verdict", "adjudicator_role", "blinded", "review_seconds"}
    ),
}

KINDS = frozenset(PAYLOAD_KEYS)

#: Kinds guarded by idx_shadow_once — a duplicate is EXPECTED (retry, restart
#: race) and is silently deduped rather than counted as an instrument failure.
#: MUST mirror the kind list in migrations._m4_shadow_events + every later
#: migration that extends idx_shadow_once (_m18 added outcome.none_confirmed);
#: a kind in the index but not here makes the losing side of a concurrent
#: double-submit count as dropped_error and false-alarms instrument.healthy.
ONCE_KINDS = frozenset(
    {
        "alert.persisted",
        "action.approved",
        "action.declined",
        "action.abandoned",
        "alert.expired",
        "episode.closed",
        "episode.opened",
        "outcome.none_confirmed",
    }
)

# ── Instrument self-observability ────────────────────────────────────────────
#: The instrument's own failures are themselves data. A scorecard built on a
#: silently-lossy log is worse than no scorecard, so drops are counted and the
#: aggregation layer can refuse to publish when this is non-zero.
STATS: dict[str, int] = {
    "emitted": 0,
    "deduped": 0,
    "dropped_error": 0,
    "dropped_backpressure": 0,
    "dropped_invalid": 0,
    "episodes_opened": 0,
    # ── The sweeper's own liveness. See `sweeper_health`. ───────────────────
    # These ride on STATS deliberately: `shadow_scorecard_service._instrument`
    # and `routes/shadow_routes._attach_stats` both copy this dict wholesale
    # onto the instrument block, so a sweeper that has stopped producing the
    # negatives becomes visible on the scorecard an operator already reads,
    # with no second plumbing to keep in sync.
    "sweep_cycles": 0,
    "sweep_failures": 0,
    "sweep_consecutive_failures": 0,
}
_stats_lock = threading.Lock()


def _bump(key: str, n: int = 1) -> None:
    with _stats_lock:
        STATS[key] = STATS.get(key, 0) + n


def _set_stat(key: str, value: int) -> None:
    with _stats_lock:
        STATS[key] = int(value)


# ── Time ─────────────────────────────────────────────────────────────────────
def utc_now() -> str:
    """ISO-8601 UTC at MILLISECOND resolution, written explicitly from Python.

    Never `DEFAULT CURRENT_TIMESTAMP` — that default is what left risk_scores /
    messages / event_log in two incompatible formats at 1-second resolution, so
    every cross-table comparison has to go through julianday(). Inside
    shadow_events there is exactly one format, and it sorts lexicographically.
    """
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def _parse(ts: str) -> datetime | None:
    try:
        return datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return None


# ── Episode registry (tier 2 of correlation; tier 3 is the DB fallback) ──────
_episodes: OrderedDict[str, tuple[str, float]] = (
    OrderedDict()
)  # patient_id -> (episode_id, opened_epoch)
_episode_lock = threading.RLock()

# patient_id -> ward, bounded. Ward is a quasi-identifier (2 wards) and is PHI at
# export; it is cached only to keep the emit path from re-reading `patients`.
_ward_cache: OrderedDict[str, str | None] = OrderedDict()
_WARD_CACHE_MAX = 512


def new_episode_id() -> str:
    return "ep_" + secrets.token_hex(8)


def _remember(patient_id: str, episode_id: str, opened_epoch: float) -> None:
    with _episode_lock:
        _episodes[patient_id] = (episode_id, opened_epoch)
        _episodes.move_to_end(patient_id)
        while len(_episodes) > EPISODE_REGISTRY_MAX:
            _episodes.popitem(last=False)


def _registry_lookup(patient_id: str) -> str | None:
    window = _episode_window_min() * 60
    now = datetime.now(UTC).timestamp()
    with _episode_lock:
        hit = _episodes.get(patient_id)
        if not hit:
            return None
        episode_id, opened = hit
        if now - opened > window:
            _episodes.pop(patient_id, None)
            return None
        _episodes.move_to_end(patient_id)
        return episode_id


def current_episode_cached(patient_id: str) -> str | None:
    """Registry-only lookup. Safe on the event loop — touches no DB."""
    if not patient_id:
        return None
    return _registry_lookup(patient_id)


def current_episode(patient_id: str, db=None) -> str | None:
    """Resolve the open episode for a patient. SYNC — never call on the loop.

    Tier 2 (in-process registry) then tier 3 (DB fallback). The registry dies
    with the process — the same flaw that makes `_alert_cooldowns` unrecoverable
    today — so the fallback makes episode resolution restart-safe by
    construction, and re-caches the result.
    """
    if not patient_id or not _enabled():
        return None
    hit = _registry_lookup(patient_id)
    if hit:
        return hit
    try:
        cutoff = (datetime.now(UTC) - timedelta(minutes=_episode_window_min())).isoformat(
            timespec="milliseconds"
        )
        if db is not None:
            row = _fetch_open_episode(db, patient_id, cutoff)
        else:
            with db_session() as conn:
                row = _fetch_open_episode(conn, patient_id, cutoff)
        if row:
            episode_id = row[0] if not hasattr(row, "keys") else row["episode_id"]
            occurred = row[1] if not hasattr(row, "keys") else row["occurred_at"]
            parsed = _parse(occurred)
            _remember(
                patient_id,
                episode_id,
                parsed.timestamp() if parsed else datetime.now(UTC).timestamp(),
            )
            return episode_id
    except Exception:
        logger.debug("shadow: episode DB fallback failed for %s", patient_id, exc_info=True)
    return None


def _fetch_open_episode(conn, patient_id: str, cutoff: str):
    return conn.execute(
        "SELECT episode_id, occurred_at FROM shadow_events_current "
        "WHERE patient_id = ? AND kind = 'episode.opened' AND occurred_at >= ? "
        "ORDER BY id DESC LIMIT 1",
        (patient_id, cutoff),
    ).fetchone()


def _lookup_ward(conn, patient_id: str | None) -> str | None:
    if not patient_id:
        return None
    with _episode_lock:
        if patient_id in _ward_cache:
            _ward_cache.move_to_end(patient_id)
            return _ward_cache[patient_id]
    ward = None
    try:
        row = conn.execute("SELECT ward FROM patients WHERE id = ?", (patient_id,)).fetchone()
        if row is not None:
            ward = row["ward"] if hasattr(row, "keys") else row[0]
    except Exception:
        ward = None
    with _episode_lock:
        _ward_cache[patient_id] = ward
        _ward_cache.move_to_end(patient_id)
        while len(_ward_cache) > _WARD_CACHE_MAX:
            _ward_cache.popitem(last=False)
    return ward


def reset_registry() -> None:
    """Test hook — drop all in-process correlation state."""
    with _episode_lock:
        _episodes.clear()
        _ward_cache.clear()


# ── Payload discipline ───────────────────────────────────────────────────────
def sanitize_payload(kind: str, payload: dict | None) -> dict:
    """Key-whitelist + JSON-safety + byte cap. Unknown keys are DROPPED, not
    stored: the whitelist is the contract the scorecard queries are written
    against, and an un-whitelisted key is how free clinical prose leaks into a
    column that is exported."""
    allowed = PAYLOAD_KEYS.get(kind, frozenset())
    out: dict[str, Any] = {}
    for key, value in (payload or {}).items():
        if key not in allowed:
            logger.debug("shadow: dropped un-whitelisted payload key %r for kind %s", key, kind)
            continue
        if value is None or isinstance(value, (bool, int, float, str)):
            out[key] = value
        elif isinstance(value, (list, tuple)):
            out[key] = [v for v in list(value)[:32] if isinstance(v, (bool, int, float, str))]
        else:
            out[key] = str(value)[:200]
    encoded = json.dumps(out, separators=(",", ":"), default=str)
    if len(encoded.encode("utf-8")) > MAX_PAYLOAD_BYTES:
        # Truncate rather than drop the row: a partial payload still anchors the
        # episode timeline; a missing row silently changes a denominator.
        out = {"_truncated": True}
        encoded = json.dumps(out, separators=(",", ":"))
    return out


# ── The emit API ─────────────────────────────────────────────────────────────
def _insert(conn, ev: dict) -> int | None:
    payload = sanitize_payload(ev["kind"], ev.get("payload"))
    phi = ev.get("phi")
    phi_json = None
    if phi:
        phi_json = json.dumps(phi, separators=(",", ":"), default=str)[:MAX_PHI_BYTES]
    ward = ev.get("ward")
    if ward is None:
        ward = _lookup_ward(conn, ev.get("patient_id"))
    cur = conn.execute(
        """INSERT INTO shadow_events
               (episode_id, kind, occurred_at, recorded_at, lane, patient_id, encounter_id,
                ward, actor_user_id, actor_role, subject_type, subject_id, payload_json,
                phi_json, supersedes_event_id, correction_reason, source_correlation_id, schema_v)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)""",
        (
            ev["episode_id"],
            ev["kind"],
            ev["occurred_at"],
            utc_now(),
            ev["lane"],
            ev.get("patient_id"),
            ev.get("encounter_id"),
            ward,
            ev.get("actor_user_id"),
            ev.get("actor_role"),
            ev.get("subject_type"),
            str(ev["subject_id"]) if ev.get("subject_id") is not None else None,
            json.dumps(payload, separators=(",", ":"), default=str),
            phi_json,
            ev.get("supersedes_event_id"),
            ev.get("correction_reason"),
            ev.get("source_correlation_id"),
        ),
    )
    return cur.lastrowid


def _validate(ev: dict) -> str | None:
    if ev.get("kind") not in KINDS:
        return f"unknown kind {ev.get('kind')!r}"
    if ev.get("lane") not in LANES:
        return f"unknown lane {ev.get('lane')!r}"
    if not ev.get("episode_id"):
        return "missing episode_id"
    if not ev.get("occurred_at"):
        return "missing occurred_at"
    return None


def build(
    kind: str,
    episode_id: str,
    lane: str,
    *,
    occurred_at: str | None = None,
    patient_id: str | None = None,
    encounter_id: str | None = None,
    ward: str | None = None,
    actor_user_id: str | None = None,
    actor_role: str | None = None,
    subject_type: str | None = None,
    subject_id: Any | None = None,
    payload: dict | None = None,
    phi: dict | None = None,
    supersedes_event_id: int | None = None,
    correction_reason: str | None = None,
    source_correlation_id: str | None = None,
) -> dict:
    """Build one event dict. Cheap and loop-safe — no DB, no clock beyond
    `occurred_at`. Stamp `occurred_at` at the SEMANTIC moment of the thing (for
    an alert: the instant compute_news2 returns above threshold, BEFORE the
    messages INSERT and before the event-bus publish); `recorded_at` is stamped
    at insert time, and `recorded_at - occurred_at` is the instrument's own lag.
    """
    return {
        "kind": kind,
        "episode_id": episode_id,
        "lane": lane,
        "occurred_at": occurred_at or utc_now(),
        "patient_id": patient_id,
        "encounter_id": encounter_id,
        "ward": ward,
        "actor_user_id": actor_user_id,
        "actor_role": actor_role,
        "subject_type": subject_type,
        "subject_id": subject_id,
        "payload": payload,
        "phi": phi,
        "supersedes_event_id": supersedes_event_id,
        "correction_reason": correction_reason,
        "source_correlation_id": source_correlation_id,
    }


def emit_many(events: Iterable[dict], db=None) -> list[int | None]:
    """Insert a batch of built events in ONE transaction. SYNC — worker thread only.

    NEVER raises. Pass `db=` when the caller already holds a write transaction
    on this thread (see the self-deadlock rule at the top of this module).
    """
    events = [e for e in events if e]
    if not events or not _enabled():
        return [None] * len(events)
    try:
        if db is not None:
            return [_emit_one(db, ev) for ev in events]
        with db_session() as conn:
            return [_emit_one(conn, ev) for ev in events]
    except Exception:
        # The whole transaction failed (table missing, disk full, locked out).
        # Count it and carry on — a clinical action must never fail because the
        # observational log did.
        _bump("dropped_error", len(events))
        logger.warning("shadow: batch emit failed (%d events dropped)", len(events), exc_info=True)
        return [None] * len(events)


def _emit_one(conn, ev: dict) -> int | None:
    problem = _validate(ev)
    if problem:
        _bump("dropped_invalid")
        logger.warning("shadow: refusing invalid event — %s", problem)
        return None
    try:
        row_id = _insert(conn, ev)
        _bump("emitted")
        return row_id
    except DBIntegrityError:
        # idx_shadow_once did its job: a retry / restart race re-emitted a
        # once-per-episode kind. Expected, not a failure. A constraint ABORT
        # rolls back the STATEMENT only, so a caller-supplied transaction stays
        # usable and the clinical write it wraps still commits.
        if ev["kind"] in ONCE_KINDS:
            _bump("deduped")
        else:
            _bump("dropped_error")
            logger.warning("shadow: integrity error on %s", ev["kind"], exc_info=True)
        return None
    except Exception:
        _bump("dropped_error")
        logger.warning("shadow: emit failed for %s", ev.get("kind"), exc_info=True)
        return None


def emit(kind: str, episode_id: str, lane: str, *, db=None, **kwargs) -> int | None:
    """Single-event convenience over `emit_many`. SYNC — worker thread only."""
    return emit_many([build(kind, episode_id, lane, **kwargs)], db=db)[0]


# ── Async entry points (everything on the loop goes through these) ───────────
async def aemit_many(events: Iterable[dict]) -> list[int | None]:
    from database import run_db

    events = [e for e in events if e]
    if not events or not _enabled():
        return [None] * len(events)
    try:
        return await run_db(emit_many, events)
    except Exception:
        _bump("dropped_error", len(events))
        logger.warning("shadow: aemit_many dispatch failed", exc_info=True)
        return [None] * len(events)


async def aemit(kind: str, episode_id: str, lane: str, **kwargs) -> int | None:
    return (await aemit_many([build(kind, episode_id, lane, **kwargs)]))[0]


# ── Fire-and-forget, bounded ─────────────────────────────────────────────────
_inflight: set = set()


def emit_bg(kind: str, episode_id: str, lane: str, **kwargs) -> None:
    """Schedule an emit without awaiting it. Use ONLY where the clinical path
    must not pay even the executor hop (socket-delivery confirmations).

    Bounded: past `_MAX_INFLIGHT` outstanding tasks the emit is DROPPED and
    counted. An unbounded fire-and-forget logger is how a storm turns an
    observability feature into an outage.
    """
    import asyncio

    if not _enabled():
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    if len(_inflight) >= _MAX_INFLIGHT:
        _bump("dropped_backpressure")
        return
    task = loop.create_task(aemit(kind, episode_id, lane, **kwargs))
    _inflight.add(task)
    task.add_done_callback(_inflight.discard)


# ── Episode lifecycle ────────────────────────────────────────────────────────
def open_episode(
    patient_id: str,
    lane: str,
    *,
    occurred_at: str | None = None,
    source_correlation_id: str | None = None,
    trigger: str | None = None,
    payload: dict | None = None,
    db=None,
) -> tuple[str | None, bool]:
    """Resolve-or-mint the episode for a patient. SYNC — worker thread only.

    Returns ``(episode_id, opened_now)``. A RE-ALERT JOINS THE EXISTING EPISODE:
    without that, an alert storm produces thousands of one-row episodes and the
    false-alert denominator measures the cooldown config rather than clinical
    validity.
    """
    if not patient_id or not _enabled():
        return (None, False)
    try:
        # Fast path outside the lock: the storm case is a re-alert on a patient
        # whose episode is already in the registry, and that must not serialize.
        cached = _registry_lookup(patient_id)
        if cached:
            return (cached, False)
        # The mint is serialized: two worker threads racing on the same patient
        # would otherwise mint two episode_ids, and idx_shadow_once cannot dedupe
        # that (the ids differ). Process-local only — a multi-worker deployment
        # needs the DB fallback to arbitrate, which it does within one window.
        with _episode_lock:
            existing = current_episode(patient_id, db=db)
            if existing:
                return (existing, False)
            episode_id = new_episode_id()
            _remember(patient_id, episode_id, datetime.now(UTC).timestamp())
        ev = build(
            "episode.opened",
            episode_id,
            lane,
            occurred_at=occurred_at,
            patient_id=patient_id,
            payload={**(payload or {}), **({"trigger": trigger} if trigger else {})},
            source_correlation_id=source_correlation_id,
        )
        emit_many([ev], db=db)
        _bump("episodes_opened")
        return (episode_id, True)
    except Exception:
        _bump("dropped_error")
        logger.warning("shadow: open_episode failed for %s", patient_id, exc_info=True)
        return (None, False)


async def aopen_episode(patient_id: str, lane: str, **kwargs) -> tuple[str | None, bool]:
    from database import run_db

    if not patient_id or not _enabled():
        return (None, False)
    try:
        return await run_db(open_episode, patient_id, lane, **kwargs)
    except Exception:
        _bump("dropped_error")
        logger.warning("shadow: aopen_episode dispatch failed", exc_info=True)
        return (None, False)


def record_batch(observations: list[dict], lane: str) -> dict[str, str]:
    """Resolve-or-open an episode per patient and emit a batch of observations
    in ONE transaction. SYNC — worker thread only (call via `run_db`).

    Each observation is ``{"patient_id", "kind", "occurred_at", "payload",
    "trigger", "source_correlation_id", "subject_type", "subject_id"}``.
    Returns ``{patient_id: episode_id}`` so the CALLER can thread the episode
    into the writes that follow (message persist, socket emit) without a second
    resolution round-trip.

    One transaction per Vigil cycle, not one per patient: during the alert storm
    this path runs on every simulator tick, and a per-patient transaction there
    is a per-patient WAL write lock acquisition.
    """
    episodes: dict[str, str] = {}
    if not observations or not _enabled():
        return episodes
    try:
        with db_session() as conn:
            batch: list[dict] = []
            for obs in observations:
                pid = obs.get("patient_id")
                if not pid:
                    continue
                episode_id = episodes.get(pid)
                if not episode_id:
                    episode_id, _ = open_episode(
                        pid,
                        lane,
                        occurred_at=obs.get("occurred_at"),
                        source_correlation_id=obs.get("source_correlation_id"),
                        trigger=obs.get("trigger"),
                        payload=obs.get("episode_payload"),
                        db=conn,
                    )
                    if not episode_id:
                        continue
                    episodes[pid] = episode_id
                batch.append(
                    build(
                        obs["kind"],
                        episode_id,
                        lane,
                        occurred_at=obs.get("occurred_at"),
                        patient_id=pid,
                        subject_type=obs.get("subject_type"),
                        subject_id=obs.get("subject_id"),
                        payload=obs.get("payload"),
                        source_correlation_id=obs.get("source_correlation_id"),
                    )
                )
            emit_many(batch, db=conn)
    except Exception:
        _bump("dropped_error", len(observations))
        logger.warning(
            "shadow: record_batch failed (%d observations)", len(observations), exc_info=True
        )
    return episodes


async def arecord_batch(observations: list[dict], lane: str) -> dict[str, str]:
    from database import run_db

    if not observations or not _enabled():
        return {}
    try:
        return await run_db(record_batch, observations, lane)
    except Exception:
        _bump("dropped_error", len(observations))
        logger.warning("shadow: arecord_batch dispatch failed", exc_info=True)
        return {}


def record_correction(
    event_id: int,
    reason: str,
    *,
    payload: dict | None = None,
    db=None,
) -> int | None:
    """Correct an earlier row by APPENDING a superseding one. There is no UPDATE
    path — `trg_shadow_no_update` aborts it at the storage layer. The superseded
    row vanishes from `shadow_events_current` (and therefore from every metric)
    while surviving in the base table for audit."""
    if not _enabled():
        return None

    def _work(conn):
        row = conn.execute("SELECT * FROM shadow_events WHERE id = ?", (event_id,)).fetchone()
        if row is None:
            return None
        keys = row.keys() if hasattr(row, "keys") else None
        get = (lambda k: row[k]) if keys else (lambda k: None)
        base_payload = {}
        try:
            base_payload = json.loads(get("payload_json") or "{}")
        except (TypeError, ValueError):
            base_payload = {}
        kind, episode_id, lane = get("kind"), get("episode_id"), get("lane")
        assert isinstance(kind, str) and isinstance(episode_id, str) and isinstance(lane, str)
        ev = build(
            kind,
            episode_id,
            lane,
            occurred_at=get("occurred_at"),
            patient_id=get("patient_id"),
            encounter_id=get("encounter_id"),
            ward=get("ward"),
            actor_user_id=get("actor_user_id"),
            actor_role=get("actor_role"),
            subject_type=get("subject_type"),
            subject_id=get("subject_id"),
            payload={**base_payload, **(payload or {})},
            supersedes_event_id=event_id,
            correction_reason=reason,
            source_correlation_id=get("source_correlation_id"),
        )
        return _emit_one(conn, ev)

    try:
        if db is not None:
            return _work(db)
        with db_session() as conn:
            return _work(conn)
    except Exception:
        _bump("dropped_error")
        logger.warning("shadow: correction of %s failed", event_id, exc_info=True)
        return None


# ── The sweeper: absence is never evidence; it is turned into a row ──────────
#
# THE INSTRUMENT GOING DEAD MUST NEVER BE QUIET.
#
# `sweep_once` is the ONLY producer of `alert.expired`, `action.abandoned` and
# `episode.closed` (main.py's lifespan says so at the registration site, and
# tests/test_shadow_producers.py asserts it). Every other rule in this file says
# "swallow the error, the hospital keeps working" — and that is right for an
# emit, where the loss is one row. It is WRONG for the sweep, because the loss
# is a whole CLASS of row: the negatives. A scorecard missing its negatives does
# not look broken, it looks GOOD — every alert appears engaged with, every
# proposal appears actioned — which is the most flattering failure mode the
# instrument has and the one a pilot readout would never question.
#
# So the sweep still never raises (a background loop that dies stops sweeping
# forever), but a failed cycle is LOUD: logged at error, counted, and left in a
# state an operator can read back through `sweeper_health()` and through STATS
# on the scorecard's instrument block.
_sweeper_lock = threading.Lock()
_SWEEPER: dict[str, Any] = {
    "started": False,  # the lifespan actually registered the loop
    "cycles": 0,
    "failures": 0,
    "consecutive_failures": 0,
    "last_attempt_at": None,
    "last_success_at": None,
    "last_error": None,  # exception CLASS + message, never a row
}


def _record_sweep(ok: bool, error: BaseException | None = None) -> None:
    """Fold one sweep cycle's outcome into the instrument's self-report."""
    stamp = utc_now()
    with _sweeper_lock:
        _SWEEPER["cycles"] += 1
        _SWEEPER["last_attempt_at"] = stamp
        _SWEEPER["started"] = True
        if ok:
            _SWEEPER["consecutive_failures"] = 0
            _SWEEPER["last_success_at"] = stamp
            _SWEEPER["last_error"] = None
        else:
            _SWEEPER["failures"] += 1
            _SWEEPER["consecutive_failures"] += 1
            _SWEEPER["last_error"] = (
                f"{type(error).__name__}: {error}" if error is not None else "unknown"
            )
        consecutive = _SWEEPER["consecutive_failures"]
        failures = _SWEEPER["failures"]
        cycles = _SWEEPER["cycles"]
    _set_stat("sweep_cycles", cycles)
    _set_stat("sweep_failures", failures)
    _set_stat("sweep_consecutive_failures", consecutive)


def note_sweeper_unavailable(reason: str) -> None:
    """The loop could not be STARTED at all (called from main.py's lifespan).

    "Never started" and "started and failing" produce the same missing negatives,
    so they are reported through the same counter rather than through a boot log
    line nobody re-reads three weeks into a pilot.
    """
    with _sweeper_lock:
        _SWEEPER["started"] = False
        _SWEEPER["failures"] += 1
        _SWEEPER["consecutive_failures"] += 1
        _SWEEPER["last_error"] = f"sweeper not started: {reason}"
        consecutive = _SWEEPER["consecutive_failures"]
        failures = _SWEEPER["failures"]
    _set_stat("sweep_failures", failures)
    _set_stat("sweep_consecutive_failures", consecutive)
    logger.error(
        "shadow: the negatives sweeper is NOT running (%s). Nothing will write "
        "alert.expired, action.abandoned or episode.closed, so the scorecard "
        "will score the successes only.",
        reason,
    )


def sweeper_health() -> dict:
    """The sweeper's self-report: is the negatives producer alive?

    `healthy` is False the moment a cycle fails, not after some threshold: this
    loop runs every five minutes and has nothing to be flaky about — a failure
    is a broken instrument, not noise.
    """
    with _sweeper_lock:
        state = dict(_SWEEPER)
    state["healthy"] = state["consecutive_failures"] == 0 and state["cycles"] > 0
    if state["consecutive_failures"]:
        state["warning"] = (
            f"The shadow sweeper has failed {state['consecutive_failures']} "
            f"consecutive cycle(s) (last error: {state['last_error']}). It is the "
            "only producer of alert.expired, action.abandoned and episode.closed, "
            "while it is down, the scorecard's negatives are missing and every "
            "engagement/agreement figure reads HIGH."
        )
    elif state["cycles"] == 0:
        state["warning"] = (
            "The shadow sweeper has not completed a cycle yet. Until it does, no "
            "negative event exists."
        )
    return state


def sweep_once(now: datetime | None = None) -> dict:
    """Close expired episodes and MATERIALISE THE NEGATIVES. SYNC — worker only.

    The negatives are the point of the instrument. An alert nobody opened, a
    draft nobody accepted, a proposal nobody actioned — none of those write a
    row anywhere in Orb today, so the scorecard would silently score only the
    successes. This pass turns each absence into an explicit row:

      * `episode.closed`    — window expired.
      * `alert.expired`     — an `alert.persisted` with no `alert.viewed` /
                              `alert.acknowledged` for that message.
      * `action.abandoned`  — an `action.proposed` with neither `action.approved`
                              nor `action.declined` in the episode.

    Idempotent by construction: all three kinds are covered by idx_shadow_once,
    so a re-run dedupes instead of double-counting.

    Returns the three counts plus `ok`. `ok=False` means THIS CYCLE FAILED and
    the counts are not evidence of a quiet ward — a caller that cannot tell those
    two apart is the reason the failure was invisible in the first place.
    """
    counts = {"closed": 0, "expired": 0, "abandoned": 0, "ok": True}
    if not _enabled():
        return counts
    window = _episode_window_min()
    now = now or datetime.now(UTC)
    cutoff = (now - timedelta(minutes=window)).isoformat(timespec="milliseconds")
    stamp = now.isoformat(timespec="milliseconds")
    try:
        with db_session() as conn:
            stale = conn.execute(
                """SELECT episode_id, MAX(patient_id) AS patient_id, MAX(ward) AS ward,
                          COUNT(*) AS events_n
                     FROM shadow_events_current
                    GROUP BY episode_id
                   HAVING MAX(occurred_at) < ?
                      AND SUM(CASE WHEN kind = 'episode.closed' THEN 1 ELSE 0 END) = 0
                    LIMIT 500""",
                (cutoff,),
            ).fetchall()

            for ep in stale:
                episode_id = ep["episode_id"]
                patient_id = ep["patient_id"]
                ward = ep["ward"]
                batch: list[dict] = []

                unviewed = conn.execute(
                    """SELECT subject_id, json_extract(payload_json,'$.message_id') AS message_id
                         FROM shadow_events_current p
                        WHERE p.episode_id = ? AND p.kind = 'alert.persisted'
                          AND NOT EXISTS (
                              SELECT 1 FROM shadow_events_current v
                               WHERE v.episode_id = p.episode_id
                                 AND v.kind IN ('alert.viewed','alert.acknowledged')
                                 AND v.subject_id = p.subject_id)""",
                    (episode_id,),
                ).fetchall()
                for row in unviewed:
                    batch.append(
                        build(
                            "alert.expired",
                            episode_id,
                            "sweeper",
                            occurred_at=stamp,
                            patient_id=patient_id,
                            ward=ward,
                            subject_type="messages",
                            subject_id=row["subject_id"],
                            payload={
                                "message_id": row["message_id"],
                                "window_min": window,
                                "viewers": 0,
                            },
                        )
                    )

                proposals = conn.execute(
                    """SELECT json_extract(payload_json,'$.action_class') AS action_class
                         FROM shadow_events_current
                        WHERE episode_id = ? AND kind = 'action.proposed'
                        LIMIT 1""",
                    (episode_id,),
                ).fetchall()
                if proposals:
                    settled = conn.execute(
                        "SELECT 1 FROM shadow_events_current WHERE episode_id = ? "
                        "AND kind IN ('action.approved','action.declined') LIMIT 1",
                        (episode_id,),
                    ).fetchone()
                    if not settled:
                        batch.append(
                            build(
                                "action.abandoned",
                                episode_id,
                                "sweeper",
                                occurred_at=stamp,
                                patient_id=patient_id,
                                ward=ward,
                                payload={
                                    "action_class": proposals[0]["action_class"],
                                    "window_min": window,
                                },
                            )
                        )

                batch.append(
                    build(
                        "episode.closed",
                        episode_id,
                        "sweeper",
                        occurred_at=stamp,
                        patient_id=patient_id,
                        ward=ward,
                        payload={
                            "window_min": window,
                            "events_n": ep["events_n"],
                            "reason": "window_expired",
                        },
                    )
                )

                ids = emit_many(batch, db=conn)
                for ev, row_id in zip(batch, ids, strict=False):
                    if row_id is None:
                        continue
                    if ev["kind"] == "alert.expired":
                        counts["expired"] += 1
                    elif ev["kind"] == "action.abandoned":
                        counts["abandoned"] += 1
                    else:
                        counts["closed"] += 1

                if patient_id:
                    with _episode_lock:
                        cached = _episodes.get(patient_id)
                        if cached and cached[0] == episode_id:
                            _episodes.pop(patient_id, None)
    except Exception as exc:
        counts["ok"] = False
        _record_sweep(False, exc)
        # ERROR, not warning: this is the sole producer of the negatives, and a
        # cycle that fails produces a scorecard that flatters the product.
        logger.error(
            "shadow: sweep cycle FAILED (%d consecutive) — no negatives were "
            "materialised this cycle; alert.expired / action.abandoned / "
            "episode.closed are the scorecard's only counter-evidence",
            STATS.get("sweep_consecutive_failures", 1),
            exc_info=True,
        )
        return counts
    _record_sweep(True)
    return counts


async def sweep_loop(interval_seconds: int = 300) -> None:
    """Background closer. Registered from main.py's lifespan.

    Never raises out: an exception escaping here kills the task and the negatives
    stop forever, silently, which is exactly the failure this whole block exists
    to prevent. It is loud instead — see `sweep_once` and `sweeper_health`.
    """
    import asyncio

    from database import run_db

    while True:
        await asyncio.sleep(interval_seconds)
        try:
            counts = await run_db(sweep_once)
        except Exception as exc:
            # The dispatch itself failed (thread pool, DB handle, cancellation of
            # the worker). `sweep_once` never ran, so it could not count this.
            _record_sweep(False, exc)
            logger.error(
                "shadow: sweep cycle could not run (%d consecutive) — the "
                "negatives producer is down",
                STATS.get("sweep_consecutive_failures", 1),
                exc_info=True,
            )
            continue
        if not counts.get("ok", True):
            continue  # already logged at error, and counted, in sweep_once
        if any(counts.get(k) for k in ("closed", "expired", "abandoned")):
            logger.info("shadow sweep: %s", counts)


# ── Ground-truth capture (no call sites yet — see docs/PILOT_ROADMAP.md) ─────
def record_outcome(
    patient_id: str,
    event_type: str,
    *,
    occurred_at: str | None = None,
    recognised_by: str | None = None,
    source: str = "manual",
    confirmed_by_role: str | None = None,
    actor_user_id: str | None = None,
    note: str | None = None,
    lane: str = "manual",
    db=None,
) -> int | None:
    """Write the ground-truth term of the detection-lead-time metric.

    THERE IS NO GROUND-TRUTH OUTCOME RECORD ANYWHERE IN ORB TODAY —
    `patients.status` is CHECK-constrained to admitted/discharged/transferred and
    cannot represent death, ICU transfer or arrest. Without capture, detection
    lead time is not weak, it is UNDEFINED, and the pilot produces no clinical
    validation. This helper is the write path; the capture UI is the nurse
    view's (`src/pages/panels/nurse/OutcomeSheet.tsx`).

    `note` is the recorder's own words. It goes to `phi_json`, NEVER to
    `payload_json`: payload_json is structured-only, key-whitelisted, and is
    what the de-identified export reads. A note is prose a human typed about a
    patient — it stays on the appliance, it is not a metric input, and it exists
    so the weekly adjudication can see what the nurse meant.
    """
    episode_id = current_episode(patient_id, db=db)
    if not episode_id:
        episode_id, _ = open_episode(
            patient_id, lane, occurred_at=occurred_at, trigger="outcome_capture", db=db
        )
    if not episode_id:
        return None
    text = (note or "").strip()
    return emit(
        "outcome.recorded",
        episode_id,
        lane,
        occurred_at=occurred_at,
        patient_id=patient_id,
        actor_user_id=actor_user_id,
        payload={
            "event_type": event_type,
            "recognised_by": recognised_by,
            "source": source,
            "confirmed_by_role": confirmed_by_role,
        },
        phi={"note": text[:1000]} if text else None,
        db=db,
    )


def record_adjudication(
    episode_id: str,
    verdict: str,
    *,
    adjudicator_role: str | None = None,
    blinded: bool = False,
    review_seconds: int | None = None,
    actor_user_id: str | None = None,
    patient_id: str | None = None,
    lane: str = "manual",
    db=None,
) -> int | None:
    """Record a blinded clinician verdict on an episode (the false-alert term)."""
    return emit(
        "adjudication.recorded",
        episode_id,
        lane,
        patient_id=patient_id,
        actor_user_id=actor_user_id,
        actor_role=adjudicator_role,
        payload={
            "verdict": verdict,
            "adjudicator_role": adjudicator_role,
            "blinded": bool(blinded),
            "review_seconds": review_seconds,
        },
        db=db,
    )


# ── Is the append-only guarantee actually in force? ──────────────────────────
#
# "This is the property that makes the export an evidentiary artefact rather
# than a report" (migration 4). That claim rests entirely on two triggers, and
# until now nothing in the running system ever checked they were there — a
# trigger nobody has watched fire is indistinguishable from one that cannot.
#
# EXECUTED 2026-08-06, and the asymmetry is real: `trg_audit_logs_no_update` /
# `_no_delete` live in `database.SCHEMA`, which replays `CREATE TRIGGER IF NOT
# EXISTS` on EVERY boot — drop one and the next start puts it back. The
# shadow_events triggers live in migration 4, which is one-shot: once its
# version is recorded in `schema_migrations` it never runs again. Dropping
# `trg_shadow_no_delete` and re-running `init_db()` left it gone, permanently,
# with no error and no log line. The same hole opens without an attacker — a
# DB restored from a backup taken mid-migration, or any path that copies rows
# into a fresh table — and the instrument would go on looking append-only
# while being freely mutable.
#
# So: two functions, both cheap enough to call at startup or from a health
# probe. `verify_append_only` does not read `sqlite_master` and believe it — it
# makes the triggers FIRE, inside a SAVEPOINT that is always rolled back.

_APPEND_ONLY_TRIGGERS = {
    "trg_shadow_no_update": (
        """CREATE TRIGGER IF NOT EXISTS trg_shadow_no_update
           BEFORE UPDATE ON shadow_events
           BEGIN
               SELECT RAISE(ABORT,
                 'shadow_events is append-only: insert a correcting row with supersedes_event_id');
           END"""
    ),
    "trg_shadow_no_delete": (
        """CREATE TRIGGER IF NOT EXISTS trg_shadow_no_delete
           BEFORE DELETE ON shadow_events
           WHEN OLD.lane <> 'simulator'
           BEGIN
               SELECT RAISE(ABORT, 'shadow_events: non-simulator rows are immutable');
           END"""
    ),
}


def _existing_triggers(conn) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'trigger' AND tbl_name = 'shadow_events'"
    ).fetchall()
    return {r[0] for r in rows}


def verify_append_only(db=None) -> dict:
    """Prove, by making them fire, that shadow_events cannot be mutated.

    Inserts a probe row on a NON-simulator lane inside a SAVEPOINT, attempts an
    UPDATE and a DELETE against it, and records whether each was aborted. The
    savepoint is rolled back unconditionally, so the probe row never becomes
    part of the instrument — and because rollback is not a DELETE, unwinding it
    does not itself need the trigger's permission.

    SYNC — never call on the event loop. Returns a dict rather than raising:
    this is a diagnostic, and a caller deciding what to do about a broken
    guarantee is a different question from discovering that it is broken.
    """

    def _work(conn) -> dict:
        present = _existing_triggers(conn)
        out = {
            "ok": False,
            "triggers_present": sorted(present & set(_APPEND_ONLY_TRIGGERS)),
            "triggers_missing": sorted(set(_APPEND_ONLY_TRIGGERS) - present),
            "update_blocked": False,
            "delete_blocked": False,
            "simulator_delete_allowed": False,
        }
        probe_ep = f"_verify_{secrets.token_hex(8)}"
        now = utc_now()
        conn.execute("SAVEPOINT shadow_verify")
        try:
            # 'alert.fired' is deliberately NOT covered by idx_shadow_once, so a
            # probe can never collide with a real row's uniqueness constraint.
            conn.execute(
                "INSERT INTO shadow_events (episode_id, kind, occurred_at, recorded_at, lane, payload_json) "
                "VALUES (?, 'alert.fired', ?, ?, 'manual', '{}')",
                (probe_ep, now, now),
            )
            try:
                conn.execute(
                    "UPDATE shadow_events SET payload_json = '{\"tampered\":1}' WHERE episode_id = ?",
                    (probe_ep,),
                )
            except Exception:
                out["update_blocked"] = True
            try:
                conn.execute("DELETE FROM shadow_events WHERE episode_id = ?", (probe_ep,))
            except Exception:
                out["delete_blocked"] = True
            # The permitted case, asserted too: retention pruning of simulator
            # rows must still work, or a "safe" repair could quietly freeze it.
            sim_ep = f"_verify_sim_{secrets.token_hex(8)}"
            conn.execute(
                "INSERT INTO shadow_events (episode_id, kind, occurred_at, recorded_at, lane, payload_json) "
                "VALUES (?, 'alert.fired', ?, ?, 'simulator', '{}')",
                (sim_ep, now, now),
            )
            try:
                conn.execute("DELETE FROM shadow_events WHERE episode_id = ?", (sim_ep,))
                out["simulator_delete_allowed"] = True
            except Exception:
                out["simulator_delete_allowed"] = False
        finally:
            # ALWAYS. The probe rows must not survive into the instrument, and a
            # ROLLBACK TO leaves the caller's outer transaction (if any) intact.
            conn.execute("ROLLBACK TO shadow_verify")
            conn.execute("RELEASE shadow_verify")

        out["ok"] = (
            not out["triggers_missing"]
            and out["update_blocked"]
            and out["delete_blocked"]
            and out["simulator_delete_allowed"]
        )
        return out

    try:
        if db is not None:
            return _work(db)
        with db_session() as conn:
            return _work(conn)
    except Exception as exc:
        logger.error("shadow: append-only verification could not run", exc_info=True)
        return {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "triggers_present": [],
            "triggers_missing": sorted(_APPEND_ONLY_TRIGGERS),
            "update_blocked": False,
            "delete_blocked": False,
            "simulator_delete_allowed": False,
        }


def ensure_append_only(db=None) -> dict:
    """Re-create any missing append-only trigger, then verify by firing them.

    Idempotent (`CREATE TRIGGER IF NOT EXISTS`) and safe to call on every boot —
    which is the point: it gives `shadow_events` the same self-healing property
    `audit_logs` already gets free from `database.SCHEMA`, instead of depending
    on a one-shot migration that will never run again.

    Deliberately does NOT run inside `verify_append_only`: verification that
    silently repairs what it is verifying cannot report a breach.
    """

    def _work(conn) -> dict:
        missing = sorted(set(_APPEND_ONLY_TRIGGERS) - _existing_triggers(conn))
        for name in missing:
            logger.warning(
                "shadow: append-only trigger %s was MISSING — recreating. The instrument "
                "was mutable while it was absent; treat rows in that window with suspicion.",
                name,
            )
            conn.execute(_APPEND_ONLY_TRIGGERS[name])
        return {"repaired": missing}

    try:
        if db is not None:
            result = _work(db)
        else:
            with db_session() as conn:
                result = _work(conn)
    except Exception as exc:
        logger.error("shadow: could not ensure append-only triggers", exc_info=True)
        return {"repaired": [], "error": f"{type(exc).__name__}: {exc}", "verified": {"ok": False}}
    result["verified"] = verify_append_only(db=db)
    return result
