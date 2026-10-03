"""Orb Hospital OS — Audit Service

Provides a healthcare-grade, append-only logging facility.

Append-only / tamper-evidence is enforced at TWO layers:
  1. DB layer: BEFORE UPDATE/DELETE triggers on audit_logs RAISE(ABORT), so the
     app DB user cannot mutate or delete audit rows (see database.py SCHEMA).
  2. Per-row SHA-256 hash chain: each row stores prev_hash + row_hash, so any
     tampering (even at the file level) is detectable via verify_audit_chain().

Failure handling is fail-closed for security/clinical_access categories: if the
audit write cannot be persisted, log_audit_event raises AuditWriteError so the
caller can refuse to proceed with an unaudited PHI/security action. For purely
operational/simulation categories it stays fail-soft (logged, non-fatal).
"""
from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from db_backend import DBIntegrityError
from datetime import datetime, timezone
from typing import Optional, Any

from database import db_session
from config import AUDIT_SIMULATION_EVENTS

logger = logging.getLogger("orb.audit")

# Categories where an audit write is a control we must not silently lose. A failed
# write here raises rather than print-and-continue, so the caller can fail closed.
#
# The names MUST match what call sites actually emit. Until 2026-09-04 this set
# said "auth" and "ai_egress"; no site ever wrote either string, so a failed audit
# write for a login or an AI inference was fail-open in practice. Logins, MFA and
# lockout events are written as "authentication" (routes/auth_routes.py) and the
# per-inference AI record as "ai_inference" (services/ai_governance.py). The
# hosted AI lane that "ai_egress" described was removed in July 2026; the local
# lane serves PHI-bearing queries, so its record is held to the same rule as a
# PHI access. tests/test_audit_fail_closed_categories.py checks every name here
# against the source tree, so a renamed category cannot silently go dead again.
#
# The last three were registered on 3 October 2026, before their first writer,
# so every hospital-system increment finds its category already fail-closed:
#   patient_identity  registration, demographic and UHID corrections, duplicate
#                     overrides and resolutions, failed identity checks (OPD)
#   patient_movement  bed assignment, transfer, swap, leave, absconded,
#                     movement corrections (ADT)
#   clinical_config   laboratory test master, reference ranges, critical
#                     limits, analyser mapping and credentials (LIS)
# The test file lists them as pre-registered with the increment that will emit
# each; that list must shrink, never grow, as the writers land.
_FAIL_CLOSED_CATEGORIES = frozenset({
    "security", "clinical_access", "phi_access", "authentication", "ai_inference",
    "patient_identity", "patient_movement", "clinical_config",
})


class AuditWriteError(RuntimeError):
    """Raised when a fail-closed audit event could not be durably persisted."""


# ── Field bounds: the audit table is a DoS surface, because it is append-only ──
#
# EXECUTED 2026-08-06. `audit_middleware` takes `correlation_id` straight off the
# client's `x-correlation-id` header and `user_agent` off `user-agent`, and
# neither was bounded anywhere on the way to the INSERT. One ordinary logged-in
# nurse, POSTing to any /api/ route with a 200 KB header, wrote 260 KB of
# attacker-chosen bytes PER ROW — measured at ~92 MB/s of audit-table growth
# from a single account.
#
# On an appliance that is not a nuisance, it is a clinical outage with three
# steps and no way back:
#   1. `audit_logs` is DELETE-blocked by `trg_audit_logs_no_delete`, so the
#      space cannot be reclaimed — not by an admin, not by a cleanup job. The
#      append-only guarantee that makes the trail evidence also makes this
#      damage permanent short of a restore.
#   2. When the disk fills, the audit INSERT starts failing.
#   3. `phi_access` / `clinical_access` are FAIL-CLOSED (verified by execution:
#      17 of 18 PHI surfaces refuse the read when the audit write cannot
#      persist). So a full disk means clinicians cannot open charts.
#
# The bound belongs HERE and not in the middleware: this function is the single
# choke point every audit row in the codebase passes through, so a route added
# tomorrow inherits the limit instead of having to remember it.
#
# Limits are generous against real traffic and ruinous against abuse: a real
# user-agent is ~200 chars, a UUID correlation id is 36, and the longest
# summary any call site in this repo builds is well under 4 KB.
_MAX_ID_CHARS = 200          # correlation/actor/target/patient/session/ip/method…
_MAX_PATH_CHARS = 512
_MAX_USER_AGENT_CHARS = 512
_MAX_SUMMARY_CHARS = 4000
_MAX_METADATA_BYTES = 8192

#: Single-line identifier columns. Besides the length cap these are stripped of
#: control characters: `correlation_id` is the field forensics GROUPS BY when
#: reconstructing "everything that happened in session X", and a caller who can
#: embed a newline plus a plausible-looking row into it can make an exported or
#: rendered trail read as though it contains events it does not.
_ID_FIELDS = (
    "correlation_id", "event_category", "actor_user_id", "actor_role",
    "target_type", "target_id", "patient_id", "encounter_id", "source_type",
    "http_method", "ip_address", "session_id", "outcome", "action_type",
)

# Chars that must never survive into a single-line audit column: C0 controls
# (incl. NUL/CR/LF/TAB), DEL, and the Unicode line/paragraph separators — the
# last two because JSON and several log/CSV renderers treat them as breaks.
_CONTROL_CHARS = {c: None for c in list(range(0x00, 0x20)) + [0x7F, 0x2028, 0x2029]}


def _clip(value: Any, limit: int, *, strip_controls: bool = False) -> Any:
    """Bound one free-text audit field. Non-strings pass through untouched."""
    if not isinstance(value, str):
        return value
    if strip_controls:
        value = value.translate(_CONTROL_CHARS)
    return value if len(value) <= limit else value[:limit]


def bound_audit_fields(row: dict[str, Any]) -> dict[str, Any]:
    """Apply the field bounds to a row dict, IN PLACE, before hashing.

    Ordering is load-bearing: this runs BEFORE `compute_audit_row_hash` and
    before the INSERT, so the hash is taken over exactly the bytes that get
    stored and `database.verify_audit_chain()` still reconciles.
    """
    for field in _ID_FIELDS:
        if row.get(field) is not None:
            row[field] = _clip(row[field], _MAX_ID_CHARS, strip_controls=True)
    if row.get("request_path") is not None:
        row["request_path"] = _clip(row["request_path"], _MAX_PATH_CHARS, strip_controls=True)
    if row.get("user_agent") is not None:
        row["user_agent"] = _clip(row["user_agent"], _MAX_USER_AGENT_CHARS, strip_controls=True)
    if row.get("summary_message") is not None:
        row["summary_message"] = _clip(row["summary_message"], _MAX_SUMMARY_CHARS)
    meta = row.get("metadata_json")
    if isinstance(meta, str) and len(meta.encode("utf-8")) > _MAX_METADATA_BYTES:
        # Same call as shadow_service.sanitize_payload: keep the ROW (a missing
        # row silently changes what the trail says happened) and lose only the
        # oversized blob, saying so explicitly rather than storing a truncated
        # fragment that would not parse as JSON.
        row["metadata_json"] = json.dumps(
            {"_truncated": True, "original_bytes": len(meta.encode("utf-8"))},
            separators=(",", ":"),
        )
    return row


# Fields (in fixed order) that participate in the per-row hash chain. Order and
# membership are part of the chain contract — do NOT reorder without a schema bump.
_HASH_FIELDS = (
    "timestamp", "correlation_id", "event_category", "actor_user_id", "actor_role",
    "target_type", "target_id", "patient_id", "encounter_id", "source_type",
    "source_event_id", "request_path", "http_method", "ip_address", "user_agent",
    "session_id", "outcome", "action_type", "summary_message", "metadata_json",
)


def compute_audit_row_hash(row: dict[str, Any], prev_hash: Optional[str]) -> str:
    """Compute the tamper-evidence hash for an audit row.

    row_hash = SHA256( prev_hash || canonical_json(selected fields) ).
    Used both when writing a new row and when verifying the chain.
    """
    canonical = json.dumps(
        {k: row.get(k) for k in _HASH_FIELDS},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    h = hashlib.sha256()
    h.update((prev_hash or "").encode("utf-8"))
    h.update(canonical.encode("utf-8"))
    return h.hexdigest()


def log_audit_event(
    correlation_id: str,
    event_category: str,
    action_type: str,
    outcome: str,
    db: Any = None,
    summary_message: Optional[str] = None,
    actor_user_id: Optional[str] = None,
    actor_role: Optional[str] = None,
    target_type: Optional[str] = None,
    target_id: Optional[str] = None,
    patient_id: Optional[str] = None,
    encounter_id: Optional[str] = None,
    source_type: Optional[str] = None,
    source_event_id: Optional[int] = None,
    request_path: Optional[str] = None,
    http_method: Optional[str] = None,
    ip_address: Optional[str] = None,
    user_agent: Optional[str] = None,
    session_id: Optional[str] = None,
    metadata: Optional[dict[str, Any]] = None,
):
    """Log an append-only, tamper-evident audit event.

    Persists the event with a SHA-256 hash chain (prev_hash/row_hash) so any later
    tampering is detectable via database.verify_audit_chain().

    Failure handling:
      - For fail-closed categories (security, clinical_access, phi_access,
        authentication, ai_inference; see _FAIL_CLOSED_CATEGORIES) a
        persistence failure raises AuditWriteError so the caller can refuse to
        proceed with an unaudited action.
      - For other categories it stays fail-soft (logged, non-fatal) to avoid
        crashing operational/ingestion paths.
    """
    # Suppress ONLY genuinely synthetic demo data — never suppress on actor_role
    # alone. The old `actor_role == "system"` clause was a latent footgun: the day a
    # system/automated actor mutates real PHI, its audit row would silently vanish.
    if not AUDIT_SIMULATION_EVENTS and (source_type == "simulation" or event_category == "simulation"):
        return

    fail_closed = event_category in _FAIL_CLOSED_CATEGORIES

    try:
        timestamp = datetime.now(timezone.utc).isoformat()
        metadata_json = json.dumps(metadata) if metadata else None

        row = {
            "timestamp": timestamp,
            "correlation_id": correlation_id,
            "event_category": event_category,
            "actor_user_id": actor_user_id,
            "actor_role": actor_role,
            "target_type": target_type,
            "target_id": target_id,
            "patient_id": patient_id,
            "encounter_id": encounter_id,
            "source_type": source_type,
            "source_event_id": source_event_id,
            "request_path": request_path,
            "http_method": http_method,
            "ip_address": ip_address,
            "user_agent": user_agent,
            "session_id": session_id,
            "outcome": outcome,
            "action_type": action_type,
            "summary_message": summary_message,
            "metadata_json": metadata_json,
        }

        # Bound the caller-supplied free text BEFORE the row is hashed or
        # inserted, so the hash covers exactly what is stored. See the
        # _MAX_* block above: unbounded fields here are an append-only
        # disk-exhaustion path that ends in fail-closed PHI reads refusing.
        bound_audit_fields(row)

        # The INSERT reads from `row`, NOT from the argument locals. Those
        # locals still hold the pre-bound values, and binding the hash to one
        # copy and the INSERT to the other is how a clipped row would store one
        # thing and attest to another — every such row would then verify as
        # tampered. One dict, used by both, cannot drift.
        _COLUMNS = (
            "timestamp", "correlation_id", "event_category", "actor_user_id", "actor_role",
            "target_type", "target_id", "patient_id", "encounter_id", "source_type",
            "source_event_id", "request_path", "http_method", "ip_address", "user_agent",
            "session_id", "outcome", "action_type", "summary_message", "metadata_json",
        )

        def _do_insert(conn):
            # Link to the previous row's hash to form the chain. Read the most
            # recent hashed row under the same connection/transaction.
            prev = conn.execute(
                "SELECT row_hash FROM audit_logs WHERE row_hash IS NOT NULL ORDER BY id DESC LIMIT 1"
            ).fetchone()
            prev_hash = prev[0] if prev else None
            row_hash = compute_audit_row_hash(row, prev_hash)
            conn.execute(
                """INSERT INTO audit_logs (
                    timestamp, correlation_id, event_category, actor_user_id, actor_role,
                    target_type, target_id, patient_id, encounter_id, source_type,
                    source_event_id, request_path, http_method, ip_address, user_agent,
                    session_id, outcome, action_type, summary_message, metadata_json,
                    prev_hash, row_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                tuple(row[c] for c in _COLUMNS) + (prev_hash, row_hash),
            )

        def _is_integrity_error(exc: Exception) -> bool:
            # The partial UNIQUE index on audit_logs(prev_hash) rejects a second
            # writer chaining off a stale head.
            #
            # This used to also match on `type(exc).__name__ == "IntegrityError"`,
            # a local workaround for the SQLCipher drivers raising their own
            # dbapi2.IntegrityError that does not subclass the stdlib's. That
            # workaround is why this one call site kept working on encrypted
            # appliances while every other `except sqlite3.IntegrityError` in the
            # codebase was silently dead. db_backend.DBIntegrityError now covers
            # every installed driver, so the name check is gone — matching on a
            # bare class name would also swallow an unrelated library's
            # IntegrityError, which is not what this guard means.
            return isinstance(exc, DBIntegrityError)

        if db:
            # Caller owns the transaction (and should hold the write lock); their
            # context serializes the prev-hash read + insert.
            try:
                _do_insert(db)
            except Exception as exc:
                if not _is_integrity_error(exc):
                    raise
                # Unique-index collision on prev_hash: another writer sealed a row
                # first. Re-read the (new) chain head and retry ONCE.
                logger.warning("Audit prev_hash collision (caller txn); retrying once")
                _do_insert(db)
        else:
            # Standalone audit write (bots, background sweeps, most routes). The
            # prev-hash SELECT + INSERT MUST be atomic, or two concurrent writers
            # both chain off the same prev_hash and the linkage breaks (observed:
            # dozens of spurious "chain broken" links on a busy DB — not tampering,
            # a read-then-write race). BEGIN IMMEDIATE takes the write lock before
            # the SELECT so sealers serialize. The partial UNIQUE index on
            # prev_hash is the belt-and-braces backstop; on a collision we retry
            # the whole read-prev + insert once against the fresh chain head.
            from database import get_db
            from db_backend import is_pooled
            for attempt in range(2):
                conn = get_db()
                if is_pooled(conn) and conn.in_transaction:
                    # Pooled handle already inside another frame's transaction
                    # on this thread: BEGIN IMMEDIATE would raise, and our
                    # except-ROLLBACK would destroy the OUTER frame's work.
                    # Write within the open transaction instead — identical to
                    # the caller-owned (db=) path; the partial UNIQUE index on
                    # prev_hash remains the concurrency backstop.
                    try:
                        _do_insert(conn)
                        break
                    except Exception as exc:
                        if attempt == 0 and _is_integrity_error(exc):
                            logger.warning("Audit prev_hash collision (pooled txn); retrying once")
                            continue
                        raise
                prev_isolation = conn.isolation_level
                conn.isolation_level = None
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    _do_insert(conn)
                    conn.execute("COMMIT")
                    break
                except Exception as exc:
                    try:
                        conn.execute("ROLLBACK")
                    except Exception:
                        pass
                    if attempt == 0 and _is_integrity_error(exc):
                        logger.warning("Audit prev_hash collision; retrying once")
                        continue
                    raise
                finally:
                    # MUST restore before close: on a POOLED handle close() is a
                    # neutered no-op and the connection is shared thread-wide —
                    # leaving isolation_level=None put the whole thread in
                    # autocommit, turning every later db_session rollback into
                    # a silent no-op (an eMAR administration survived its
                    # failed audit write exactly this way).
                    conn.isolation_level = prev_isolation
                    conn.close()
    except Exception as e:
        logger.critical(
            "[Audit Log Failure] Failed to write audit event (%s, category=%s): %s",
            action_type, event_category, e,
        )
        if fail_closed:
            # Fail closed: a security/clinical-access action MUST NOT proceed
            # unaudited. Surface the failure so the caller can abort.
            raise AuditWriteError(
                f"audit write failed for {event_category}/{action_type}: {e}"
            ) from e
        # Non-security categories: fail soft so operational paths are not crashed.
