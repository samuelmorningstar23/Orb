"""Orb Sage — REPLAYABLE PROVENANCE.

WHAT THIS IS FOR
════════════════
Six months into the pilot a clinician, a hospital committee or a regulator asks
one question about one Sage answer: *why did it say that?*

Every input needed to answer that question is gone by then. The chart has moved
on. The corpus has been re-indexed. The model has been swapped, or the thinking
toggle was off that morning and on this one. A provenance record that stores a
POINTER to the patient answers nothing — replayed today it re-reads today's
chart and tells a story about today. That is worse than no record: it looks
authoritative and it is wrong.

So this module stores the turn's INPUTS BY VALUE, not by reference:

  * the question, verbatim;
  * the resolved scope — which department the asker was confined to, whether
    they were a genuinely unscoped governance account, which patient the turn
    was anchored to, which retrieval scope fired;
  * the patient context AS IT WAS AT THAT MOMENT — the whole assembled chart
    dict, values frozen;
  * the retrieved guideline passages, full text, with their document ids;
  * the model, the served model, the provider, the thinking-mode decision and
    whether the safety floor fired;
  * the answer with its citations, AND the raw model output before the
    guardrail strip rewrote it (an incident review's first question is almost
    always "what did the model actually say?");
  * a sha256 of the exact assembled prompt, as an integrity anchor.

STORAGE IS THE DESIGN PROBLEM
═════════════════════════════
The chart block alone is several KB and it is re-sent, byte for byte, on every
follow-up question about the same patient — and a ward conversation is five or
six of those in a row. Storing each turn's components independently costs
7.6 KB/turn measured; adding the assembled prompt on top (the obvious way to
"just keep everything") roughly doubles that again.

Three mechanisms, in order of how much they save:

  1. CONTENT-ADDRESSED DEDUPLICATION. Every large component (question, chart
     snapshot, each guideline passage, answer, raw output) is stored once, keyed
     by the hash of its own bytes. A follow-up question on an unchanged chart
     re-uses the chart blob for free; the 19-document corpus means guideline
     passages are shared across essentially every turn that retrieves them. This
     is what turns the per-turn cost from "the size of the chart" into "the size
     of what actually changed".
  2. COMPRESSION. Blobs are zlib-compressed before storage. Clinical prose and
     JSON compress 3–5x.
  3. RETENTION. `prune()` drops turns past ORB_SAGE_PROVENANCE_RETENTION_DAYS
     (default 400 — long enough to survive a full year of pilot review plus a
     quarter of slack) and reference-counted-GCs the blobs nothing points at any
     more. It is invoked once a day by main.py's `sage_provenance_prune_sweep`
     lifespan task; without that caller the ~213 MB/year growth never stops.

The prompt itself is deliberately NOT stored: it is the chart block and the
passages concatenated, so storing it would duplicate — and defeat the dedup on
— the two largest components. `prompt_sha256` is stored instead, so a reviewer
can still prove the components on file correspond to the bytes the model saw.

MEASURED, ON REAL PAGES, NOT ESTIMATED
──────────────────────────────────────
400 turns, 12 patients, a 19-document corpus, charts revised every 6th turn
(scratch benchmark; `storage_stats()` reproduces the same numbers live, from
SQLite's own dbstat page counts rather than from a guess):

    on disk, everything included   1,224,704 B  =  3,062 B / turn
    naive (same components, raw)   3,032,154 B  =  7,580 B / turn
    end-to-end saving                                    2.48x

    at 200 turns/day   →   598 KB/day   17.5 MB/month   213 MB/year
    at the 400-day retention horizon, the store settles at ~234 MB and
    STOPS GROWING. That is the number to plan disk against.

That benchmark is deliberately pessimistic about dedup: it samples 3 of 19
documents at random per turn, so guideline manifests almost never repeat. Real
retrieval returns the same top-3 for the same question, so a live appliance
should land under this, not over.

THIS STORE IS PHI
═════════════════
It holds whole chart snapshots. It is therefore scoped and audited exactly like
any other PHI surface: the replay routes hold `audit.read`, resolve scope only
through `permissions.department_scope`, 404 (never 403) on an out-of-department
turn, and write a `phi_access` audit row per read. See routes/sage_routes.py.

Two consequences worth knowing about, neither of them handled here:
  * `sage_provenance_turns` carries a single plain `patient_id`, so
    consent_service._patient_tables discovers it and a DPDP data-principal
    export will include its METADATA rows (no blob content — the blob tables
    are not patient-keyed). That is the correct default; it has not been
    reviewed by anyone who knows what a patient should be shown.
  * DPDP erasure is category-driven (`consent_service._ERASURE_EXECUTORS`) and
    has no category for this store, so an erasure decision does NOT reach it.
    Same posture as `audit_logs`, and for the same defensible reason — but it is
    a decision somebody should make explicitly rather than inherit.

IT MUST NEVER COST A CLINICIAN AN ANSWER
════════════════════════════════════════
`record_turn` is pure sync (call it through `database.run_db`), runs after the
answer has already been streamed, and never raises — every failure is logged and
swallowed. A provenance outage degrades the record, never the care.

WHY THE SCHEMA LIVES HERE AND NOT IN migrations.py
══════════════════════════════════════════════════
`ensure_schema()` is idempotent DDL owned by this module, the same pattern as
services/helix_db.py, services/timeline_service.py and services/corpus_loader.py.
Since 2026-10-03 migration 44 (sage_provenance_schema) runs the same
`_apply_schema` at boot, so a fresh appliance has the tables before its first
answer; `ensure_schema` stays as the guard for any path that runs before it.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import uuid
import zlib
from datetime import UTC, datetime, timedelta

from database import db_session

logger = logging.getLogger("orb.sage_provenance")

#: 2 = safety_floor tri-state (`safety_floor_state`, 2026-08-10). Rows with
#: schema_v 1 predate it and carry NULL there — reported as 'unknown', never
#: retroactively claimed as evaluated.
#: 3 = `safety_floor_armed` (2026-10-03): whether the floor was switched on at
#: all on this appliance, so a turn answered with ORB_FAST_MODE_SAFETY_FLOOR=0
#: reads 'disabled' instead of 'not_fired'. Earlier rows carry NULL.
SCHEMA_V = 3

#: Blob keys are the first 32 hex chars (128 bits) of the content's sha256.
#: 128 bits is collision-free by any practical measure (at 10^9 distinct blobs
#: the birthday probability is ~10^-21) and halves the cost of the reference
#: rows, which are otherwise pure hash text. The FULL digest is kept on the blob
#: row so an export can still carry a standard sha256.
_CID_LEN = 32

LANES = ("chat", "voice")
OUTCOMES = ("answered", "refused", "failed", "cancelled")

#: safety_floor tri-state. The v1 schema stored a single 0/1 and the 0 was a
#: lie by omission: a voice turn (which never runs the floor) and a chat turn
#: the floor examined and let through both read "0". An incident review asking
#: "was the dosing floor even consulted?" needs three answers, not two.
SAFETY_FLOOR_FIRED = "fired"
SAFETY_FLOOR_NOT_FIRED = "not_fired"
SAFETY_FLOOR_NOT_EVALUATED = "not_evaluated"
#: Read-side label ONLY: the operator switched the floor off
#: (ORB_FAST_MODE_SAFETY_FLOOR=0), so it could not have run. Stored as
#: safety_floor_state 'not_evaluated' plus safety_floor_armed = 0, because the
#: state column's CHECK predates it and the table is never rebuilt (see
#: _ADDITIVE_COLUMNS). Recording 'not_fired' here read as "the floor looked and
#: let it through" when nobody was looking.
SAFETY_FLOOR_DISABLED = "disabled"
#: Read-side label ONLY, never written: a row whose safety_floor_state is NULL
#: was recorded before the column existed, and the honest answer about it is
#: "unknown" — the legacy 0 must not be re-badged as an evaluation that passed.
SAFETY_FLOOR_UNKNOWN = "unknown"

#: Component roles, as SINGLE CHARACTERS. There are ~8 reference edges per turn
#: and the edge table plus its index was measured at 32 % of the entire store
#: when the roles were spelled out ("guideline_manifest" is 18 bytes repeated on
#: every row of the busiest table here). The readable names live in
#: ROLE_LABELS and in the replay response; the storage layer spends one byte.
ROLE_QUESTION = "q"
ROLE_CONTEXT = "c"
ROLE_GUIDELINE_MANIFEST = "m"
ROLE_GUIDELINE_BODY = "g"
ROLE_ANSWER = "a"
ROLE_RAW = "r"

ROLE_LABELS = {
    ROLE_QUESTION: "question",
    ROLE_CONTEXT: "patient_context",
    ROLE_GUIDELINE_MANIFEST: "guideline_manifest",
    ROLE_GUIDELINE_BODY: "guideline_body",
    ROLE_ANSWER: "answer",
    ROLE_RAW: "raw_output",
}

_TRUNCATION_MARKER = "\n… [truncated by provenance store]"


# ════════════════════════════════════════════════════════════════════════════
#  Configuration — read at CALL time so ops (and tests) can change it live
# ════════════════════════════════════════════════════════════════════════════
def enabled() -> bool:
    return os.getenv("ORB_SAGE_PROVENANCE_ENABLED", "1").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


def retention_days() -> int:
    try:
        return max(1, int(os.getenv("ORB_SAGE_PROVENANCE_RETENTION_DAYS", "400")))
    except ValueError:
        return 400


def max_blob_bytes() -> int:
    """Per-component ceiling.

    A clinician can paste a 4 MB discharge letter into the box. Without a cap
    that becomes 4 MB of provenance, and the store's whole cost model — a few KB
    per turn — is decided by the worst user of the week rather than by design.
    Truncation is VISIBLE in the replay (`_TRUNCATION_MARKER`), because a
    silently shortened record is a record that lies about being complete."""
    try:
        return max(4096, int(os.getenv("ORB_SAGE_PROVENANCE_MAX_BLOB_BYTES", "131072")))
    except ValueError:
        return 131072


# ════════════════════════════════════════════════════════════════════════════
#  Schema
# ════════════════════════════════════════════════════════════════════════════
# ONE STATEMENT PER ENTRY, executed with execute() — NEVER executescript().
# executescript() issues an implicit COMMIT before it runs, so a first-ever call
# made from inside a caller's open transaction would silently commit their
# half-finished work. That is the exact hazard migrations.run_pending_migrations
# documents, and this module is reachable with a caller-supplied connection.
_SCHEMA_STATEMENTS = (
    """
-- One row per Sage turn. Small, fixed-width, and holds NO free text: every
-- large component lives in the blob store and is reached through the refs
-- table. That is what keeps this table scannable for the listing/audit views.
CREATE TABLE IF NOT EXISTS sage_provenance_turns (
    turn_id                  TEXT PRIMARY KEY,
    occurred_at              TEXT NOT NULL,
    lane                     TEXT NOT NULL CHECK (lane IN ('chat','voice')),
    outcome                  TEXT NOT NULL
        CHECK (outcome IN ('answered','refused','failed','cancelled')),
    actor_user_id            TEXT,
    actor_role               TEXT,
    -- The RESOLVED scope, as permissions.department_scope() returned it for the
    -- authenticated user at that moment. NULL + hospital_wide=1 means a
    -- genuinely unscoped governance account; NULL alone is never "unknown".
    department               TEXT,
    hospital_wide            INTEGER NOT NULL DEFAULT 0,
    patient_id               TEXT,
    patient_context_used     INTEGER NOT NULL DEFAULT 0,
    query_scope              TEXT,
    model_requested          TEXT,
    model_served             TEXT,
    provider                 TEXT,
    thinking                 INTEGER,
    thinking_requested       INTEGER,
    thinking_tier            TEXT,
    thinking_reason          TEXT,
    safety_floor             INTEGER NOT NULL DEFAULT 0,
    safety_floor_rules       TEXT,
    safety_floor_unsatisfied INTEGER NOT NULL DEFAULT 0,
    citation_basis           TEXT,
    confidence               TEXT,
    escalation               INTEGER NOT NULL DEFAULT 0,
    retrieved_doc_ids        TEXT,
    cited_doc_ids            TEXT,
    action_status            TEXT,
    refusal_class            TEXT,
    prompt_sha256            TEXT,
    retrieval_ms             INTEGER,
    ttft_ms                  INTEGER,
    total_ms                 INTEGER,
    -- The measurement instrument. bytes_written is the NEW compressed bytes
    -- this turn added after deduplication; bytes_logical is what it would have
    -- cost stored naively. storage_stats() reports both.
    bytes_written            INTEGER NOT NULL DEFAULT 0,
    bytes_logical            INTEGER NOT NULL DEFAULT 0,
    schema_v                 INTEGER NOT NULL DEFAULT 1
)""",
    "CREATE INDEX IF NOT EXISTS idx_sage_prov_time ON sage_provenance_turns(occurred_at)",
    "CREATE INDEX IF NOT EXISTS idx_sage_prov_patient "
    "ON sage_provenance_turns(patient_id, occurred_at)",
    "CREATE INDEX IF NOT EXISTS idx_sage_prov_actor "
    "ON sage_provenance_turns(actor_user_id, occurred_at)",
    """
-- Content-addressed component store. `cid` is the content's own hash, so the
-- same chart snapshot / guideline passage written by a thousand turns occupies
-- one row.
--
-- WITHOUT ROWID: this table is only ever reached BY cid. As an ordinary rowid
-- table it pays for two b-trees — the table keyed by rowid and the PK autoindex
-- keyed by cid — and the autoindex alone measured 73 KB per 1,550 blobs.
--
-- NO SEPARATE sha256 COLUMN: `cid` IS sha256(body)[:32]. Storing the other 32
-- hex characters beside it costs 64 bytes on every blob to add nothing an
-- integrity check can use — 128 bits of a cryptographic digest is already
-- collision-free at any scale this appliance will ever see.
-- NO stored_len COLUMN either: it is length(body), and a denormalised copy of a
-- value the row already contains is a value that can disagree with it.
CREATE TABLE IF NOT EXISTS sage_provenance_blobs (
    cid          TEXT PRIMARY KEY,
    kind         TEXT NOT NULL,
    encoding     TEXT NOT NULL DEFAULT 'z',
    logical_len  INTEGER NOT NULL,
    first_seen   INTEGER NOT NULL,
    body         BLOB NOT NULL
) WITHOUT ROWID""",
    """
-- turn -> blob edges. Exists so the retention GC is an exact reference count
-- rather than a reachability walk over every manifest in the store.
--
-- WITHOUT ROWID for the same reason, and it matters more here: this is the
-- highest-cardinality table in the store (~8 rows per turn) and its only access
-- patterns are "the edges of THIS turn" (the PK prefix) and "is this cid still
-- referenced" (a single scan at GC time). There is deliberately NO index on
-- `cid`: it would be a third b-tree over the busiest table, ~370 B per turn, to
-- serve a query that runs once a day during retention pruning — which _prune()
-- therefore writes as a single NOT IN scan instead.
CREATE TABLE IF NOT EXISTS sage_provenance_refs (
    turn_id   TEXT NOT NULL,
    role      TEXT NOT NULL,
    ord       INTEGER NOT NULL DEFAULT 0,
    cid       TEXT NOT NULL,
    PRIMARY KEY (turn_id, role, ord)
) WITHOUT ROWID""",
    # APPEND-ONLY, enforced by the database rather than by convention. The single
    # property this whole feature rests on is that a stored turn describes the
    # past and cannot be quietly revised; "we agreed not to UPDATE it" is not a
    # control an incident review can rely on. DELETE stays permitted — retention
    # pruning is a requirement, and it is the only writer that uses it.
    """CREATE TRIGGER IF NOT EXISTS trg_sage_prov_turn_no_update
       BEFORE UPDATE ON sage_provenance_turns
       BEGIN
           SELECT RAISE(ABORT,
             'sage_provenance_turns is append-only: a recorded turn describes the past');
       END""",
    """CREATE TRIGGER IF NOT EXISTS trg_sage_prov_blob_no_update
       BEFORE UPDATE ON sage_provenance_blobs
       BEGIN
           SELECT RAISE(ABORT,
             'sage_provenance_blobs is append-only: a blob IS its content hash');
       END""",
)

#: Columns added AFTER schema v1, applied as ADDITIVE migrations — ALTER TABLE
#: ADD COLUMN only, never a table rebuild (the append-only triggers make a
#: copy-and-rename rewrite a falsification of the record, not a migration).
#: A fresh install takes exactly the same path as a live pilot store: the v1
#: CREATE above, then these ALTERs — one code path, so the two can never
#: drift apart. Pre-existing rows get NULL, which the read side reports as
#: SAFETY_FLOOR_UNKNOWN; ADD COLUMN fires no UPDATE trigger and validates no
#: old rows, so the migration cannot touch what was already recorded.
_ADDITIVE_COLUMNS = (
    (
        "sage_provenance_turns",
        "safety_floor_state",
        "ALTER TABLE sage_provenance_turns ADD COLUMN safety_floor_state TEXT "
        "CHECK (safety_floor_state IN ('fired','not_fired','not_evaluated'))",
    ),
    (
        "sage_provenance_turns",
        "safety_floor_armed",
        "ALTER TABLE sage_provenance_turns ADD COLUMN safety_floor_armed INTEGER "
        "CHECK (safety_floor_armed IN (0, 1))",
    ),
)

_schema_lock = threading.Lock()
_schema_ready = False


def _apply_schema(db) -> None:
    for stmt in _SCHEMA_STATEMENTS:
        db.execute(stmt)
    for table, column, ddl in _ADDITIVE_COLUMNS:
        # By position (1 = name), not by key: migration 44 runs this on the
        # runner's connection, which need not have a Row factory.
        present = {r[1] for r in db.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in present:
            db.execute(ddl)


def ensure_schema(db=None) -> None:
    """Idempotent DDL. Cheap after the first call in a process."""
    global _schema_ready
    if _schema_ready:
        return
    with _schema_lock:
        if _schema_ready:
            return
        if db is not None:
            _apply_schema(db)
        else:
            with db_session() as conn:
                _apply_schema(conn)
        _schema_ready = True


def reset_schema_cache() -> None:
    """Tests only: forget that the schema was created (they drop and rebuild)."""
    global _schema_ready
    _schema_ready = False


def drop_schema(db) -> None:
    """Tests only."""
    db.execute("DROP TRIGGER IF EXISTS trg_sage_prov_turn_no_update")
    db.execute("DROP TRIGGER IF EXISTS trg_sage_prov_blob_no_update")
    db.execute("DROP TABLE IF EXISTS sage_provenance_refs")
    db.execute("DROP TABLE IF EXISTS sage_provenance_blobs")
    db.execute("DROP TABLE IF EXISTS sage_provenance_turns")


# ════════════════════════════════════════════════════════════════════════════
#  Helpers
# ════════════════════════════════════════════════════════════════════════════
def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def new_turn_id() -> str:
    return uuid.uuid4().hex[:16]


def _canon(obj) -> str:
    """Canonical JSON: sorted keys, no incidental whitespace.

    Determinism is what makes deduplication work at all — two identical charts
    that serialise differently are two blobs."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def _clip(text: str) -> str:
    """Cap one component at max_blob_bytes(), counted in BYTES not characters.

    Characters would be the wrong unit here and not academically so: this
    appliance is going into an Indian hospital, and a paragraph of Devanagari is
    three bytes per character — a character-counted cap would let the same
    "128 KB" limit admit a 384 KB blob. The cut is made on the encoded bytes and
    any half-formed character at the boundary is dropped."""
    if not text:
        return ""
    limit = max_blob_bytes()
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text
    return raw[:limit].decode("utf-8", errors="ignore") + _TRUNCATION_MARKER


def _put_blob(db, kind: str, text: str) -> tuple[str, int, int]:
    """Store one component. Returns (cid, new_bytes_stored, logical_bytes).

    `new_bytes_stored` is 0 when the content was already present — that zero IS
    the dedup measurement, and it is what `bytes_written` on the turn row sums.
    """
    raw = (text or "").encode("utf-8")
    cid = hashlib.sha256(raw).hexdigest()[:_CID_LEN]
    existing = db.execute("SELECT 1 FROM sage_provenance_blobs WHERE cid = ?", (cid,)).fetchone()
    if existing is not None:
        return cid, 0, len(raw)
    body = zlib.compress(raw, 6)
    db.execute(
        "INSERT OR IGNORE INTO sage_provenance_blobs "
        "(cid, kind, encoding, logical_len, first_seen, body) VALUES (?, ?, 'z', ?, ?, ?)",
        (cid, kind, len(raw), int(datetime.now(UTC).timestamp()), body),
    )
    return cid, len(body), len(raw)


def _get_blob(db, cid: str) -> str | None:
    row = db.execute(
        "SELECT body, encoding FROM sage_provenance_blobs WHERE cid = ?", (cid,)
    ).fetchone()
    if row is None:
        return None
    body = row["body"]
    if row["encoding"] == "z":
        try:
            return zlib.decompress(body).decode("utf-8")
        except Exception:
            logger.exception("provenance blob %s failed to decompress", cid)
            return None
    return body.decode("utf-8") if isinstance(body, (bytes, bytearray)) else str(body)


def _as_bool(v) -> bool | None:
    if v is None:
        return None
    return bool(v)


def _int_or_none(v):
    try:
        return None if v is None else int(v)
    except (TypeError, ValueError):
        return None


def _safety_floor_state(info: dict) -> str:
    """Tri-state, decided by whether the floor RAN, not by what it returned.

    Every path that evaluates the floor writes a `safety_floor` key into
    route_info (True or False — see query_ollama's route_info contract); the
    paths that never reach it (voice lane, pre-LLM refusal, retrieval failure)
    write no such key. So key-absence IS "not evaluated", and a falsy value is
    a genuine "evaluated, did not fire"."""
    if "safety_floor" not in (info or {}):
        return SAFETY_FLOOR_NOT_EVALUATED
    if _safety_floor_armed(info) == 0:
        # Switched off: decide() never consulted it, so it did not "not fire".
        return SAFETY_FLOOR_NOT_EVALUATED
    return SAFETY_FLOOR_FIRED if info.get("safety_floor") else SAFETY_FLOOR_NOT_FIRED


def _safety_floor_armed(info: dict) -> int | None:
    """1 or 0 from thinking_mode.stamp's `safety_floor_enabled`; None when the
    turn never reached the floor's decision (voice, a pre-LLM refusal)."""
    if "safety_floor_enabled" not in (info or {}):
        return None
    return int(bool(info.get("safety_floor_enabled")))


def _floor_state_label(v, armed=None) -> str:
    """NULL (a pre-tri-state row) reads as 'unknown', never as a claimed state.
    A floor the operator switched off reads 'disabled', whatever was stored."""
    if armed is not None and not armed:
        return SAFETY_FLOOR_DISABLED
    return v if v else SAFETY_FLOOR_UNKNOWN


# ════════════════════════════════════════════════════════════════════════════
#  WRITE — pure sync, never raises. Call through database.run_db().
# ════════════════════════════════════════════════════════════════════════════
def record_turn(
    *,
    turn_id: str | None = None,
    lane: str = "chat",
    outcome: str = "answered",
    user: dict | None = None,
    question: str = "",
    patient_id: str | None = None,
    patient_context: dict | None = None,
    prompt_context: str | None = None,
    sources: list | None = None,
    route_info: dict | None = None,
    answer: str = "",
    raw_output: str = "",
    structured: dict | None = None,
    cited_doc_ids: list | None = None,
    citation_basis: str | None = None,
    confidence: str | None = None,
    escalation: bool = False,
    query_scope: str | None = None,
    action_status: str | None = None,
    refusal_class: str | None = None,
    timings: dict | None = None,
    occurred_at: str | None = None,
    db=None,
) -> str | None:
    """Persist everything needed to reconstruct one Sage turn.

    Returns the turn id, or None if provenance is disabled or the write failed.
    NEVER RAISES — a provenance failure must not be able to reach the clinician.

    `user` is the AUTHENTICATED user dict. The department is derived from it
    through permissions.department_scope() and never taken from the request; a
    scope the caller can name is not a scope.
    """
    if not enabled():
        return None
    try:
        return _record_turn_inner(
            turn_id=turn_id,
            lane=lane,
            outcome=outcome,
            user=user,
            question=question,
            patient_id=patient_id,
            patient_context=patient_context,
            prompt_context=prompt_context,
            sources=sources,
            route_info=route_info,
            answer=answer,
            raw_output=raw_output,
            structured=structured,
            cited_doc_ids=cited_doc_ids,
            citation_basis=citation_basis,
            confidence=confidence,
            escalation=escalation,
            query_scope=query_scope,
            action_status=action_status,
            refusal_class=refusal_class,
            timings=timings,
            occurred_at=occurred_at,
            db=db,
        )
    except Exception:
        # Rule: the record degrades, the care does not.
        logger.exception("Sage provenance capture failed (turn dropped)")
        return None


def _record_turn_inner(
    *,
    turn_id,
    lane,
    outcome,
    user,
    question,
    patient_id,
    patient_context,
    prompt_context,
    sources,
    route_info,
    answer,
    raw_output,
    structured,
    cited_doc_ids,
    citation_basis,
    confidence,
    escalation,
    query_scope,
    action_status,
    refusal_class,
    timings,
    occurred_at,
    db,
):
    from permissions import department_scope

    user = user or {}
    ns = {
        "turn_id": turn_id or new_turn_id(),
        "occurred_at": occurred_at or utc_now(),
        "lane": lane if lane in LANES else "chat",
        "outcome": outcome if outcome in OUTCOMES else "answered",
        "user": user,
        # THE ONE SCOPE SOURCE. Derived from the authenticated user, never from
        # anything the request could name. None here means a genuinely unscoped
        # governance account (hospital_wide=1), never "unknown".
        "dept": department_scope(user),
        "question": question or "",
        "patient_id": patient_id,
        "patient_context": patient_context,
        "prompt_context": prompt_context,
        "sources": [s for s in (sources or []) if isinstance(s, dict)],
        "info": route_info or {},
        "answer": answer or "",
        "raw_output": raw_output or "",
        "structured": structured,
        "cited_doc_ids": list(cited_doc_ids or []),
        "citation_basis": citation_basis,
        "confidence": confidence,
        "escalation": escalation,
        "query_scope": query_scope,
        "action_status": action_status,
        "refusal_class": refusal_class,
        "timings": timings or {},
    }

    if db is not None:
        return _write(db, ns)
    with db_session() as conn:
        return _write(conn, ns)


def _write(db, ns: dict) -> str:
    """The transactional body."""
    ensure_schema(db)

    turn_id = ns["turn_id"]
    written = 0
    logical = 0
    refs: list[tuple[str, int, str]] = []

    def add(role: str, kind: str, text: str, ord_: int = 0):
        nonlocal written, logical
        cid, new_bytes, logical_bytes = _put_blob(db, kind, _clip(text))
        written += new_bytes
        logical += logical_bytes
        refs.append((role, ord_, cid))

    # ── question, verbatim ──
    add(ROLE_QUESTION, ROLE_QUESTION, ns["question"] or "")

    # ── the chart AS IT WAS. Stored BY VALUE. This is the whole point. ──
    if ns["patient_context"] is not None:
        add(ROLE_CONTEXT, ROLE_CONTEXT, _canon(ns["patient_context"]))

    # ── retrieved passages: a manifest of metadata, plus each body as its own
    #    blob so a passage shared by a thousand turns is stored once. ──
    manifest = []
    for i, s in enumerate(ns["sources"]):
        body = s.get("content") or s.get("passage") or ""
        cid, new_bytes, logical_bytes = _put_blob(db, ROLE_GUIDELINE_BODY, _clip(body))
        written += new_bytes
        logical += logical_bytes
        refs.append((ROLE_GUIDELINE_BODY, i, cid))
        manifest.append(
            {
                "document_id": s.get("document_id"),
                "title": s.get("title"),
                "source": s.get("source"),
                "source_type": s.get("source_type"),
                "trust_tier": s.get("trust_tier"),
                "is_synthetic": bool(s.get("is_synthetic", False)),
                "category": s.get("category"),
                "fusion_score": s.get("fusion_score"),
                "passage_cid": cid,
            }
        )
    if manifest:
        add(ROLE_GUIDELINE_MANIFEST, ROLE_GUIDELINE_MANIFEST, _canon(manifest))

    # ── the answer as delivered, and what the model actually emitted first ──
    add(
        ROLE_ANSWER,
        ROLE_ANSWER,
        _canon(
            {
                "text": ns["answer"] or "",
                "structured": ns["structured"] or {},
                "cited_document_ids": ns["cited_doc_ids"],
            }
        ),
    )
    if ns["raw_output"]:
        add(ROLE_RAW, ROLE_RAW, ns["raw_output"])

    info = ns["info"]
    prompt_sha = (
        hashlib.sha256(ns["prompt_context"].encode("utf-8")).hexdigest()
        if ns["prompt_context"]
        else None
    )
    retrieved_ids = [s.get("document_id") for s in ns["sources"]]
    timings = ns["timings"]

    # OR IGNORE, never OR REPLACE: first write wins. Combined with the
    # append-only trigger, that means a recorded turn cannot be revised by any
    # path at all — including a retry that thinks it is being helpful.
    db.execute(
        """INSERT OR IGNORE INTO sage_provenance_turns (
               turn_id, occurred_at, lane, outcome, actor_user_id, actor_role,
               department, hospital_wide, patient_id, patient_context_used, query_scope,
               model_requested, model_served, provider,
               thinking, thinking_requested, thinking_tier, thinking_reason,
               safety_floor, safety_floor_rules, safety_floor_unsatisfied,
               safety_floor_state, safety_floor_armed,
               citation_basis, confidence, escalation,
               retrieved_doc_ids, cited_doc_ids, action_status, refusal_class,
               prompt_sha256, retrieval_ms, ttft_ms, total_ms,
               bytes_written, bytes_logical, schema_v
           ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            turn_id,
            ns["occurred_at"],
            ns["lane"],
            ns["outcome"],
            (ns["user"] or {}).get("id"),
            (ns["user"] or {}).get("role"),
            ns["dept"],
            1 if ns["dept"] is None else 0,
            ns["patient_id"],
            1 if ns["patient_context"] is not None else 0,
            ns["query_scope"],
            info.get("thinking_model") or info.get("model"),
            info.get("model"),
            info.get("provider"),
            None if info.get("thinking") is None else int(bool(info.get("thinking"))),
            None
            if info.get("thinking_requested") is None
            else int(bool(info.get("thinking_requested"))),
            info.get("thinking_tier"),
            info.get("thinking_reason"),
            int(bool(info.get("safety_floor"))),
            _canon(list(info.get("safety_floor_rules") or [])),
            int(bool(info.get("safety_floor_unsatisfied"))),
            _safety_floor_state(info),
            _safety_floor_armed(info),
            ns["citation_basis"],
            ns["confidence"],
            int(bool(ns["escalation"])),
            _canon(retrieved_ids),
            _canon(ns["cited_doc_ids"]),
            ns["action_status"],
            ns["refusal_class"],
            prompt_sha,
            _int_or_none(timings.get("retrieval_ms")),
            _int_or_none(timings.get("ttft_ms")),
            _int_or_none(timings.get("total_ms")),
            written,
            logical,
            SCHEMA_V,
        ),
    )
    db.executemany(
        "INSERT OR IGNORE INTO sage_provenance_refs (turn_id, role, ord, cid) VALUES (?, ?, ?, ?)",
        [(turn_id, role, ord_, cid) for role, ord_, cid in refs],
    )
    return turn_id


# ════════════════════════════════════════════════════════════════════════════
#  READ — replay + listing. BLOCKING; call from a `def` route or via run_db.
# ════════════════════════════════════════════════════════════════════════════
def get_turn_row(turn_id: str, db=None) -> dict | None:
    """The metadata row only — used by the route to resolve `patient_id` BEFORE
    it decides whether the caller may see anything at all."""
    if db is not None:
        return _get_turn_row(db, turn_id)
    with db_session() as conn:
        return _get_turn_row(conn, turn_id)


def _get_turn_row(db, turn_id: str) -> dict | None:
    ensure_schema(db)
    row = db.execute("SELECT * FROM sage_provenance_turns WHERE turn_id = ?", (turn_id,)).fetchone()
    return dict(row) if row is not None else None


def replay(turn_id: str, db=None) -> dict | None:
    """Reconstruct one turn from stored components. None if unknown."""
    if db is not None:
        return _replay(db, turn_id)
    with db_session() as conn:
        return _replay(conn, turn_id)


def _replay(db, turn_id: str) -> dict | None:
    row = _get_turn_row(db, turn_id)
    if row is None:
        return None

    refs = db.execute(
        "SELECT role, ord, cid FROM sage_provenance_refs WHERE turn_id = ? ORDER BY role, ord",
        (turn_id,),
    ).fetchall()
    by_role: dict[str, list[tuple[int, str]]] = {}
    for r in refs:
        by_role.setdefault(r["role"], []).append((r["ord"], r["cid"]))

    missing: list[str] = []

    def one(role: str) -> str | None:
        entries = by_role.get(role) or []
        if not entries:
            return None
        text = _get_blob(db, entries[0][1])
        if text is None:
            # Reported by READABLE name. `missing_blobs` is the one field an
            # auditor reads when a reconstruction looks thin, and "q" is not an
            # answer to "what is missing from this record?".
            missing.append(ROLE_LABELS.get(role, role))
        return text

    def load_json(role: str, default):
        text = one(role)
        if not text:
            return default
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            missing.append(f"{ROLE_LABELS.get(role, role)} (corrupt)")
            return default

    question = one(ROLE_QUESTION) or ""
    patient_context = load_json(ROLE_CONTEXT, None) if ROLE_CONTEXT in by_role else None
    answer_blob = load_json(ROLE_ANSWER, {}) or {}
    manifest = load_json(ROLE_GUIDELINE_MANIFEST, []) or []

    guidelines = []
    for entry in manifest:
        cid = entry.get("passage_cid")
        passage = _get_blob(db, cid) if cid else None
        if cid and passage is None:
            missing.append(f"{ROLE_LABELS[ROLE_GUIDELINE_BODY]}:{cid}")
        g = {k: v for k, v in entry.items() if k != "passage_cid"}
        g["passage"] = passage
        guidelines.append(g)

    def jload(col, default):
        try:
            return json.loads(row.get(col) or "null") or default
        except (json.JSONDecodeError, TypeError):
            return default

    return {
        "turn_id": row["turn_id"],
        "occurred_at": row["occurred_at"],
        "lane": row["lane"],
        "outcome": row["outcome"],
        "actor": {"user_id": row["actor_user_id"], "role": row["actor_role"]},
        "scope": {
            "department": row["department"],
            "hospital_wide": bool(row["hospital_wide"]),
            "patient_id": row["patient_id"],
            "patient_context_used": bool(row["patient_context_used"]),
            "query_scope": row["query_scope"],
        },
        "question": question,
        "patient_context": patient_context,
        "guidelines": guidelines,
        "model": {
            "requested": row["model_requested"],
            "served": row["model_served"],
            "provider": row["provider"],
            "thinking": _as_bool(row["thinking"]),
            "thinking_requested": _as_bool(row["thinking_requested"]),
            "thinking_tier": row["thinking_tier"],
            "thinking_reason": row["thinking_reason"],
            "safety_floor": bool(row["safety_floor"]),
            "safety_floor_rules": jload("safety_floor_rules", []),
            "safety_floor_unsatisfied": bool(row["safety_floor_unsatisfied"]),
            "safety_floor_state": _floor_state_label(
                row.get("safety_floor_state"), row.get("safety_floor_armed")
            ),
        },
        "answer": {
            "text": answer_blob.get("text", ""),
            "structured": answer_blob.get("structured", {}),
            "cited_document_ids": answer_blob.get("cited_document_ids", []),
            "citation_basis": row["citation_basis"],
            "confidence": row["confidence"],
            "escalation": bool(row["escalation"]),
            "action_status": row["action_status"],
            "refusal_class": row["refusal_class"],
        },
        "raw_model_output": one(ROLE_RAW) or "",
        "retrieved_document_ids": jload("retrieved_doc_ids", []),
        "timing": {
            "retrieval_ms": row["retrieval_ms"],
            "ttft_ms": row["ttft_ms"],
            "total_ms": row["total_ms"],
        },
        "storage": {
            "bytes_written": row["bytes_written"],
            "bytes_logical": row["bytes_logical"],
        },
        "integrity": {
            "prompt_sha256": row["prompt_sha256"],
            "schema_v": row["schema_v"],
            "missing_blobs": missing,
        },
    }


def list_turns(
    *,
    patient_ids_sql: tuple | None = None,
    scope_department: str | None = None,
    patient_id: str | None = None,
    actor_user_id: str | None = None,
    since: str | None = None,
    limit: int = 50,
    offset: int = 0,
    db=None,
) -> list[dict]:
    """Metadata only — no question text, no chart, no answer.

    `patient_ids_sql` is (sql, params) from permissions.scope_patient_ids_sql; when
    given, only in-scope turns are returned. It is the CALLER'S job to derive it
    from department_scope(user) — this function will not invent a scope.

    PATIENT-LESS TURNS are scoped by `scope_department`, which must be the same
    department the id set was derived from. A turn with no patient anchor is
    still a turn a ward asked — it was attributed to the asking user's
    department at write time (the `department` column, recorded from
    permissions.department_scope since schema v1) and it is read back under
    exactly that scope. Turns whose recorded department is NULL were asked by an
    unscoped governance account and are visible ONLY to unscoped readers: for a
    scoped caller `department = ?` can never match NULL, so they fall out of
    every scoped listing — the fail-closed direction, and the reason no backfill
    is needed (the append-only trigger forbids one anyway)."""
    if db is not None:
        return _list_turns(
            db, patient_ids_sql, scope_department, patient_id, actor_user_id, since, limit, offset
        )
    with db_session() as conn:
        return _list_turns(
            conn, patient_ids_sql, scope_department, patient_id, actor_user_id, since, limit, offset
        )


def _list_turns(
    db, patient_ids_sql, scope_department, patient_id, actor_user_id, since, limit, offset
):
    ensure_schema(db)
    where = ["1=1"]
    params: list = []
    if patient_ids_sql is not None:
        sql, sql_params = patient_ids_sql
        where.append(
            f"((patient_id IS NOT NULL AND patient_id IN ({sql})) "
            "OR (patient_id IS NULL AND department = ?))"
        )
        params.extend(sql_params)
        params.append(scope_department)
    if patient_id:
        where.append("patient_id = ?")
        params.append(patient_id)
    if actor_user_id:
        where.append("actor_user_id = ?")
        params.append(actor_user_id)
    if since:
        where.append("occurred_at >= ?")
        params.append(since)
    limit = max(1, min(int(limit or 50), 500))
    rows = db.execute(
        "SELECT turn_id, occurred_at, lane, outcome, actor_user_id, actor_role, "
        "       department, hospital_wide, patient_id, patient_context_used, query_scope, "
        "       model_served, provider, thinking, safety_floor, safety_floor_unsatisfied, "
        "       safety_floor_state, safety_floor_armed, retrieved_doc_ids, "
        "       citation_basis, confidence, escalation, refusal_class, "
        "       retrieval_ms, ttft_ms, total_ms, bytes_written "
        f"FROM sage_provenance_turns WHERE {' AND '.join(where)} "
        "ORDER BY occurred_at DESC, rowid DESC LIMIT ? OFFSET ?",
        (*params, limit, max(0, int(offset or 0))),
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["hospital_wide"] = bool(d["hospital_wide"])
        d["patient_context_used"] = bool(d["patient_context_used"])
        d["thinking"] = _as_bool(d["thinking"])
        d["safety_floor"] = bool(d["safety_floor"])
        d["safety_floor_unsatisfied"] = bool(d["safety_floor_unsatisfied"])
        d["safety_floor_state"] = _floor_state_label(
            d["safety_floor_state"], d.pop("safety_floor_armed", None)
        )
        d["escalation"] = bool(d["escalation"])
        # A COUNT of retrieved documents, not their ids: the listing stays the
        # thin metadata surface, and "did retrieval inform this answer?" is the
        # incident-review question the count already answers.
        try:
            d["retrieved_doc_count"] = len(json.loads(d.pop("retrieved_doc_ids") or "[]") or [])
        except (json.JSONDecodeError, TypeError):
            d["retrieved_doc_count"] = 0
        out.append(d)
    return out


# ════════════════════════════════════════════════════════════════════════════
#  Retention + measurement
# ════════════════════════════════════════════════════════════════════════════
def prune(*, now: datetime | None = None, days: int | None = None, db=None) -> dict:
    """Drop turns past the retention horizon and GC the blobs nothing references.

    Reference-counted, not "delete what the old turn touched": a chart snapshot
    is exactly the blob two turns share, and deleting it with the older turn
    would silently gut the newer one's replay."""
    if db is not None:
        return _prune(db, now, days)
    with db_session() as conn:
        return _prune(conn, now, days)


def _prune(db, now, days) -> dict:
    ensure_schema(db)
    now = now or datetime.now(UTC)
    cutoff = (now - timedelta(days=days if days is not None else retention_days())).isoformat()

    doomed = [
        r["turn_id"]
        for r in db.execute(
            "SELECT turn_id FROM sage_provenance_turns WHERE occurred_at < ?", (cutoff,)
        ).fetchall()
    ]
    if not doomed:
        return {"cutoff": cutoff, "turns_deleted": 0, "blobs_deleted": 0, "bytes_reclaimed": 0}

    db.executemany("DELETE FROM sage_provenance_refs WHERE turn_id = ?", [(t,) for t in doomed])
    db.executemany("DELETE FROM sage_provenance_turns WHERE turn_id = ?", [(t,) for t in doomed])

    # ONE scan of the edge table, not a correlated lookup per blob: `refs` has no
    # cid index (see the schema note) because this is the only query that wants
    # one and it runs at most once a day.
    orphans = db.execute(
        "SELECT cid, LENGTH(body) sz FROM sage_provenance_blobs "
        "WHERE cid NOT IN (SELECT cid FROM sage_provenance_refs)"
    ).fetchall()
    reclaimed = sum(int(o["sz"] or 0) for o in orphans)
    db.executemany(
        "DELETE FROM sage_provenance_blobs WHERE cid = ?", [(o["cid"],) for o in orphans]
    )
    return {
        "cutoff": cutoff,
        "turns_deleted": len(doomed),
        "blobs_deleted": len(orphans),
        "bytes_reclaimed": reclaimed,
    }


def storage_stats(*, turns_per_day: int = 200, db=None) -> dict:
    """MEASURE, then project. This is the answer to "what does this cost?".

    Returns the real on-disk (compressed, deduplicated) size of the store, the
    naive size it would have been, and the straight-line growth at
    `turns_per_day`. No estimate is hard-coded: every number is computed from
    rows that exist."""
    if db is not None:
        return _storage_stats(db, turns_per_day)
    with db_session() as conn:
        return _storage_stats(conn, turns_per_day)


def _on_disk_bytes(db) -> int | None:
    """REAL pages occupied by this store — tables, indexes and autoindexes.

    Summing the compressed blob lengths is NOT the answer an operator needs: it
    ignores the turn rows, the reference edges, four explicit indexes, three
    PK autoindexes and SQLite's page granularity, and it undershot the truth by
    ~25 % when the two were compared on a 400-turn corpus. The names come from
    sqlite_master rather than a hand-written list so a later index cannot be
    silently left out of the bill.

    Returns None when dbstat is not compiled into this SQLite build, in which
    case the caller falls back to a clearly-labelled estimate."""
    try:
        row = db.execute(
            "SELECT COALESCE(SUM(pgsize),0) FROM dbstat WHERE name IN "
            "(SELECT name FROM sqlite_master WHERE tbl_name LIKE 'sage_provenance%')"
        ).fetchone()
        return int(row[0])
    except Exception:
        return None


def _storage_stats(db, turns_per_day: int) -> dict:
    ensure_schema(db)
    t = db.execute(
        "SELECT COUNT(*) c, COALESCE(SUM(bytes_logical),0) logical FROM sage_provenance_turns"
    ).fetchone()
    b = db.execute(
        "SELECT COUNT(*) c, COALESCE(SUM(LENGTH(body)),0) stored FROM sage_provenance_blobs"
    ).fetchone()
    refs = db.execute("SELECT COUNT(*) c FROM sage_provenance_refs").fetchone()["c"]

    turns = int(t["c"])
    content_bytes = int(b["stored"])
    measured = _on_disk_bytes(db)
    if measured is not None:
        stored, basis = measured, "dbstat"
    else:
        # Fallback only. Deliberately generous rather than flattering: 580 B of
        # turn row + its three indexes and 60 B per reference edge, both rounded
        # UP from the dbstat-measured figures on a 400-turn corpus, plus 30 %
        # page/row slack on the blob payload.
        stored = int(content_bytes * 1.30 + turns * 580 + refs * 60)
        basis = "estimated"

    logical = int(t["logical"])
    per_turn = (stored / turns) if turns else 0.0
    return {
        "turns": turns,
        "blobs": int(b["c"]),
        "refs": refs,
        "content_bytes": content_bytes,
        "stored_bytes": stored,
        "measurement_basis": basis,
        # What a naive store — every component of every turn, uncompressed —
        # would have written.
        "logical_bytes": logical,
        "bytes_per_turn": per_turn,
        # End to end: >1 means dedup + compression are paying for themselves
        # AFTER row, index and page overhead, not before.
        "dedup_ratio": (logical / stored) if stored else 0.0,
        "retention_days": retention_days(),
        "projection": {
            "turns_per_day": turns_per_day,
            "bytes_per_day": per_turn * turns_per_day,
            "bytes_per_year": per_turn * turns_per_day * 365,
            "bytes_at_retention_horizon": per_turn * turns_per_day * retention_days(),
        },
    }
