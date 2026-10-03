"""Orb — versioned schema migrations.

A small, dependency-free migration framework (Alembic-style versioning without
the SQLAlchemy weight the app doesn't use). Each migration has an integer
version, a name, and an ``apply(conn)`` function; the runner applies any that
haven't been recorded in the ``schema_migrations`` ledger, in ascending order,
inside the caller's transaction, and records each on success.

This is the forward mechanism for ALL new schema changes: add a migration to
``MIGRATIONS`` rather than another ad-hoc block in ``database.init_db``. The
historical additive blocks in init_db remain as the proven baseline (they are
idempotent + introspection-guarded); everything new goes here so the schema has
an auditable, ordered, re-runnable history that also works against a Postgres
backend (see docs/DATA_LAYER.md).

Migrations MUST be idempotent (use ``IF NOT EXISTS`` etc.): the ledger prevents
re-application, but idempotency keeps them safe even if the ledger is reset.
"""
from __future__ import annotations

import json
import logging
from typing import Callable, NamedTuple

logger = logging.getLogger("orb.migrations")


class Migration(NamedTuple):
    version: int
    name: str
    apply: Callable[[object], None]


def _ensure_ledger(conn) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )"""
    )


def _applied_versions(conn) -> set[int]:
    _ensure_ledger(conn)
    return {r[0] for r in conn.execute("SELECT version FROM schema_migrations").fetchall()}


# ── Migrations (ascending version; never renumber a shipped one) ──────────────

def _m1_hot_path_indexes(conn) -> None:
    """Composite indexes for the busiest per-patient / queue lookups that the
    single-column indexes don't fully cover — the patient-story merge, the
    pharmacy verify queue, and per-patient audit (PHI-access) review. Pure
    read-path scale; no data change."""
    conn.execute("CREATE INDEX IF NOT EXISTS idx_medications_patient_status ON medications(patient_id, status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_medications_source_verified ON medications(source, verified_by)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_patient ON audit_logs(patient_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_clinical_docs_patient_created ON clinical_documents(patient_id, created_at)")


def _m2_messages_idempotency(conn) -> None:
    """Idempotency key on relay messages so an offline-queued message that is
    replayed (its original POST response was lost in transit) dedupes to the
    first row instead of double-posting. Additive: the column is nullable and
    only enforced-unique when present (partial index), so existing rows and
    ordinary live sends — which pass no key — are unaffected. Mirrors the same
    key handling already on medications / clinical_documents."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(messages)").fetchall()}
    if "idempotency_key" not in cols:
        conn.execute("ALTER TABLE messages ADD COLUMN idempotency_key TEXT")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_idempotency_key "
        "ON messages(idempotency_key) WHERE idempotency_key IS NOT NULL"
    )


def _m3_sage_insights_auto_resolved(conn) -> None:
    """Permit state='auto_resolved' on sage_insights so surveillance can retire
    insights whose condition has cleared (previously only manual dismiss/act
    left 'active', making the ward-brief alert count grow monotonically).
    SQLite cannot alter a CHECK constraint, so rebuild the table in place.
    Idempotent: skips when the stored DDL already permits 'auto_resolved'."""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='sage_insights'"
    ).fetchone()
    if not row or "auto_resolved" in (row[0] or ""):
        return
    conn.execute("ALTER TABLE sage_insights RENAME TO sage_insights_old")
    conn.execute(
        """CREATE TABLE sage_insights (
            id TEXT PRIMARY KEY,
            patient_id TEXT NOT NULL,
            insight_type TEXT NOT NULL,
            severity TEXT NOT NULL DEFAULT 'info' CHECK (severity IN ('critical','warning','info')),
            title TEXT NOT NULL,
            detail TEXT,
            suggested_action_type TEXT,
            suggested_action_target TEXT,
            target_user_id TEXT,
            dedup_key TEXT,
            state TEXT NOT NULL DEFAULT 'active' CHECK (state IN ('active','dismissed','acted','auto_resolved')),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )"""
    )
    conn.execute("INSERT INTO sage_insights SELECT * FROM sage_insights_old")
    conn.execute("DROP TABLE sage_insights_old")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sage_insights_active ON sage_insights(state, target_user_id, created_at DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sage_insights_dedup ON sage_insights(dedup_key, state, created_at DESC)")


def _m4_shadow_events(conn) -> None:
    """The pilot instrument: `shadow_events` (+ one view, two triggers).

    APPEND-ONLY, enforced by the database rather than by convention: one row per
    observation in a clinical episode, threaded by a minted ``episode_id``. A
    correction is a NEW row pointing at the one it replaces via
    ``supersedes_event_id``; nothing is ever UPDATEd.

    This is deliberately NOT the compliance record — ``audit_logs`` is, and stays
    fail-closed. This is the OBSERVATIONAL record that produces the composite
    scorecard: it is fail-OPEN by design (a lost shadow row must never fail a
    clinical action), which is exactly why it cannot be the compliance record.

    Timestamp discipline: both ``occurred_at`` and ``recorded_at`` are written
    explicitly from Python as ISO-8601 UTC at millisecond resolution — never
    ``DEFAULT CURRENT_TIMESTAMP``. That default is what left risk_scores /
    messages / event_log in two incompatible formats at 1-second resolution and
    forced every cross-table comparison through julianday().
    """
    conn.execute(
        """CREATE TABLE IF NOT EXISTS shadow_events (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            episode_id          TEXT NOT NULL,
            kind                TEXT NOT NULL,
            occurred_at         TEXT NOT NULL,
            recorded_at         TEXT NOT NULL,
            lane                TEXT NOT NULL
                CHECK (lane IN ('simulator','ingestion','chat','voice','manual','sweeper')),
            patient_id          TEXT,
            encounter_id        TEXT,
            ward                TEXT,
            actor_user_id       TEXT,
            actor_role          TEXT,
            subject_type        TEXT,
            subject_id          TEXT,
            payload_json        TEXT NOT NULL DEFAULT '{}',
            phi_json            TEXT,
            supersedes_event_id INTEGER REFERENCES shadow_events(id),
            correction_reason   TEXT,
            source_correlation_id TEXT,
            schema_v            INTEGER NOT NULL DEFAULT 1,

            CHECK (kind IN (
                'episode.opened','episode.closed',
                'alert.fired','alert.suppressed','alert.dropped','alert.persisted','alert.delivered',
                'context.compiled','sage.drafted','sage.refused','sage.failed',
                'action.proposed','action.approved','action.declined','action.abandoned',
                'alert.viewed','alert.acknowledged','alert.expired',
                'clinical.action',
                'outcome.candidate','outcome.recorded','outcome.none_confirmed','adjudication.recorded'
            ))
        )"""
    )

    conn.execute("CREATE INDEX IF NOT EXISTS idx_shadow_episode      ON shadow_events(episode_id, id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_shadow_kind_time    ON shadow_events(kind, occurred_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_shadow_patient_time ON shadow_events(patient_id, occurred_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_shadow_subject      ON shadow_events(subject_type, subject_id)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_shadow_supersedes ON shadow_events(supersedes_event_id) "
        "WHERE supersedes_event_id IS NOT NULL"
    )
    # Episode resolution on restart (shadow_service.current_episode's DB fallback):
    # newest episode.opened for a patient. Without this it is a scan of the busiest
    # table in the pilot on every cold-path alert.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_shadow_open_by_patient ON shadow_events(patient_id, id DESC) "
        "WHERE kind = 'episode.opened'"
    )

    # ── Retry/duplicate-emission guard for the kinds that must fire exactly once
    # per episode. NOT global: alert.fired, clinical.action and sage.drafted
    # legitimately repeat.
    #
    # TWO DEVIATIONS FROM THE DESIGN DDL, both of which were live bugs:
    #
    #   1. IFNULL(...) on subject_type/subject_id. SQLite treats NULLs as
    #      DISTINCT in a UNIQUE index, so the design's plain column list would
    #      have deduped NOTHING for 'episode.opened' / 'episode.closed' — whose
    #      subject is NULL by definition. Those are precisely the two kinds a
    #      restart-racing registry is most likely to double-emit, and a second
    #      episode.opened row silently doubles every per-episode denominator in
    #      the scorecard.
    #
    #   2. AND supersedes_event_id IS NULL. Without it the index makes the
    #      correction mechanism unusable for exactly the kinds it guards: a
    #      corrected 'action.approved' carries the same (episode, kind, subject)
    #      as the row it supersedes and would be REFUSED by the unique index.
    #      Append-only + no corrections = no way to fix a wrong row at all.
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_shadow_once "
        "ON shadow_events(episode_id, kind, IFNULL(subject_type,''), IFNULL(subject_id,'')) "
        "WHERE supersedes_event_id IS NULL AND kind IN "
        "('alert.persisted','action.approved','action.declined','action.abandoned',"
        " 'alert.expired','episode.closed','episode.opened')"
    )

    # ── Append-only, enforced by the database, not by convention ──────────────
    conn.execute(
        """CREATE TRIGGER IF NOT EXISTS trg_shadow_no_update
           BEFORE UPDATE ON shadow_events
           BEGIN
               SELECT RAISE(ABORT,
                 'shadow_events is append-only: insert a correcting row with supersedes_event_id');
           END"""
    )

    # DELETE is permitted ONLY for simulator-lane rows, so retention pruning can
    # run while every row of real pilot data is immutable at the storage layer.
    # This is the property that makes the export an evidentiary artefact rather
    # than a report.
    conn.execute(
        """CREATE TRIGGER IF NOT EXISTS trg_shadow_no_delete
           BEFORE DELETE ON shadow_events
           WHEN OLD.lane <> 'simulator'
           BEGIN
               SELECT RAISE(ABORT, 'shadow_events: non-simulator rows are immutable');
           END"""
    )

    # ── Correction resolution, once, in one place ────────────────────────────
    # EVERY scorecard query reads this view, never the base table.
    conn.execute(
        """CREATE VIEW IF NOT EXISTS shadow_events_current AS
           SELECT e.* FROM shadow_events e
           WHERE NOT EXISTS (
               SELECT 1 FROM shadow_events s WHERE s.supersedes_event_id = e.id
           )"""
    )


def _m5_roster_import_runs(conn) -> None:
    """Ledger for HIS roster imports (services/roster_import.py).

    Every APPLIED import writes one row: who ran it, against which file, with
    which column mapping, and what it did. The audit log records that an import
    happened; this records what the import *was*, so a patient row that turns
    out wrong months into the pilot can be traced back to the exact export and
    mapping that produced it. Dry runs write nothing here — only real writes.
    """
    conn.execute(
        """CREATE TABLE IF NOT EXISTS roster_import_runs (
            id TEXT PRIMARY KEY,
            actor_user_id TEXT REFERENCES users(id),
            source_filename TEXT,
            mapping_json TEXT NOT NULL,
            date_order TEXT NOT NULL DEFAULT 'auto',
            update_existing INTEGER NOT NULL DEFAULT 1,
            rows_total INTEGER NOT NULL DEFAULT 0,
            rows_created INTEGER NOT NULL DEFAULT 0,
            rows_updated INTEGER NOT NULL DEFAULT 0,
            rows_unchanged INTEGER NOT NULL DEFAULT 0,
            rows_skipped INTEGER NOT NULL DEFAULT 0,
            issue_count INTEGER NOT NULL DEFAULT 0,
            summary_json TEXT,
            note TEXT,
            started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_roster_import_runs_started "
        "ON roster_import_runs(started_at DESC)"
    )


def _m6_allergy_interlock(conn) -> None:
    """Bedside allergy interlock: the override ledger and the per-dose record of
    what was screened (services/allergy_interlock.py).

    Two additive changes:

      * ``medication_administrations.allergy_check_json`` — what the interlock
        saw and decided for THIS dose. The audit chain already carries it, but
        the clinical record should be able to answer "was this dose screened,
        and against what?" without joining to audit_logs. Nullable: doses
        charted before this migration are honestly NULL ("not screened"), which
        is different from an empty screen.

      * ``medication_allergy_overrides`` — WHO overrode WHAT and WHY, one row
        per explicit override, linked to the administration it authorised.
        ``alert_fingerprint`` is the identity of "this drug against this allergy
        picture"; the interlock carries an override forward for the same order
        only while that fingerprint holds, so a newly-charted allergy re-arms
        the block instead of inheriting a stale acknowledgement. Append-only by
        convention, like medication_administrations itself.
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info(medication_administrations)").fetchall()}
    if "allergy_check_json" not in cols:
        conn.execute("ALTER TABLE medication_administrations ADD COLUMN allergy_check_json TEXT")

    conn.execute(
        """CREATE TABLE IF NOT EXISTS medication_allergy_overrides (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            medication_id INTEGER REFERENCES medications(id),
            patient_id TEXT NOT NULL REFERENCES patients(id),
            administration_id INTEGER REFERENCES medication_administrations(id),
            alert_fingerprint TEXT,
            decision TEXT,
            reason TEXT NOT NULL,
            overridden_by TEXT NOT NULL REFERENCES users(id),
            overridden_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            alerts_json TEXT
        )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_med_allergy_override_lookup "
        "ON medication_allergy_overrides(medication_id, alert_fingerprint)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_med_allergy_override_patient "
        "ON medication_allergy_overrides(patient_id, overridden_at DESC)"
    )


def _m7_message_room_fit_flags(conn) -> None:
    """Wrong-room detection for clinical messages (services/room_fit_service.py).

    One row per message that the detector warned about — never one per message
    checked. The overwhelming majority of traffic clears the deterministic pass
    in microseconds and writes nothing, so this table stays small enough to read
    by eye during the pilot.

    ``resolution`` is the clinically interesting column, not ``band``. A warning
    that was shown and OVERRIDDEN ('confirmed_correct') is the row that says
    whether the ward trusts this feature or merely tolerates it; a warning that
    was acted on ('redirected') is the row that says it caught something real.
    Both are append-only in spirit: ``redirected`` never deletes the original
    message, it points at the copy that was posted into the right room.

    ``UNIQUE(message_id)`` — a message has exactly one flag. The ambient model
    can promote a silent review into a visible flag after the fact, and that is
    an UPDATE of the same row rather than a second warning on the same line.
    """
    conn.execute(
        """CREATE TABLE IF NOT EXISTS message_room_fit_flags (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id INTEGER NOT NULL UNIQUE REFERENCES messages(id),
            channel_id TEXT NOT NULL REFERENCES channels(id),
            room_patient_id TEXT REFERENCES patients(id),
            sender_id TEXT REFERENCES users(id),
            band TEXT NOT NULL,
            stage TEXT NOT NULL,
            score REAL,
            suspected_patient_id TEXT REFERENCES patients(id),
            suspected_patient_name TEXT,
            suggested_channel_id TEXT REFERENCES channels(id),
            suggested_channel_name TEXT,
            reasons_json TEXT,
            markers_json TEXT,
            suppressors_json TEXT,
            matched_surface TEXT,
            ambiguous INTEGER DEFAULT 0,
            resolution TEXT DEFAULT 'open',
            resolved_by TEXT REFERENCES users(id),
            resolved_by_name TEXT,
            resolved_at TIMESTAMP,
            resolve_reason TEXT,
            redirected_message_id INTEGER REFERENCES messages(id),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_room_fit_channel "
        "ON message_room_fit_flags(channel_id, created_at DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_room_fit_open "
        "ON message_room_fit_flags(resolution, created_at DESC)"
    )


def _m8_channel_members(conn) -> None:
    """Explicit membership for Relay DIRECT channels — the missing access model.

    THE HOLE THIS CLOSES. `channels` carried no membership at all, so
    `relay_service.get_channels_for_user` had nothing to filter `direct` rows on
    and listed every DM in the hospital to every holder of `relay.read` — and
    that list is the read boundary (routes/relay_routes.py authorises message
    reads with "is this channel in get_channels_for_user?"). Two colleagues'
    private conversation was readable by any authenticated clinician.

    SCOPE — THIS TABLE GOVERNS `direct` CHANNELS ONLY, and that is a decision,
    not an omission:

      * `case_room` / `department` access stays derived from
        `admissions.department` vs `users.department` (the scoping fixed
        2026-08-05). A case room is a WARD-WIDE artefact: the covering doctor who
        has never posted in it must still see a deteriorating patient's thread.
        Backfilling membership from message history would SHRINK that access —
        the failure mode being "the night doctor cannot see the room", which is
        clinically worse than the leak we are closing.
      * Department is also LIVE where a membership row is a SNAPSHOT: a patient
        transferred to another ward changes department, and every derived surface
        follows. A members table would go stale silently.
      * Enforcing both rules on the same channel type is exactly the "two
        disagreeing rules" trap. One rule per channel type, stated once, in
        `relay_service.get_channels_for_user`.

    BACKFILL — the distinct SENDERS in each direct channel, `added_at` = their
    first message, `source='backfill_sender'`. Message history is the only
    evidence that exists for legacy DMs, and it is evidence of access that was
    ALREADY exercised: every backfilled member could already read the thread, so
    this can only narrow, never widen. Its known gap is stated rather than
    papered over: someone who was in a DM but never replied gets NO row and
    loses visibility. That is deliberate fail-closed behaviour, and it is what
    `relay_service.add_channel_member` (and its endpoint) exists to repair — by
    an explicit, audited grant from someone already in the conversation, not by
    inference.

    IDEMPOTENT: PRIMARY KEY (channel_id, user_id) + INSERT OR IGNORE, so a
    re-run (or a wiped ledger — see tests/test_migrations.py) inserts nothing new
    and cannot duplicate a member.
    """
    conn.execute(
        """CREATE TABLE IF NOT EXISTS channel_members (
            channel_id TEXT NOT NULL REFERENCES channels(id),
            user_id    TEXT NOT NULL REFERENCES users(id),
            added_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            added_by   TEXT REFERENCES users(id),
            -- Provenance of the GRANT itself, so a membership row can always
            -- answer "why does this person have access?". 'backfill_sender' is
            -- inferred from message history and is the only value the system
            -- ever writes without a named human behind it (added_by IS NULL).
            source     TEXT NOT NULL DEFAULT 'added'
                       CHECK (source IN ('added', 'creator', 'backfill_sender')),
            PRIMARY KEY (channel_id, user_id)
        )"""
    )
    # (channel_id, user_id) is served by the PK's implicit index — that is the
    # membership probe in get_channels_for_user. This one is the reverse lookup
    # ("which conversations is this user in"), used by the DM de-duplication in
    # create_direct_channel and by any future "my DMs" surface.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_channel_members_user "
        "ON channel_members(user_id, channel_id)"
    )

    # `messages.sender_id` is declared REFERENCES users(id) and PRAGMA
    # foreign_keys is ON, but the EXISTS guard stays: a bot/system sender id that
    # predates that enforcement would otherwise abort the whole migration — and
    # with it the entire init_db transaction — on a real appliance.
    conn.execute(
        """INSERT OR IGNORE INTO channel_members (channel_id, user_id, added_at, added_by, source)
           SELECT m.channel_id, m.sender_id, MIN(m.timestamp), NULL, 'backfill_sender'
             FROM messages m
             JOIN channels c ON c.id = m.channel_id
            WHERE c.type = 'direct'
              AND m.sender_id IS NOT NULL
              AND EXISTS (SELECT 1 FROM users u WHERE u.id = m.sender_id)
            GROUP BY m.channel_id, m.sender_id"""
    )


def _m9_recover_stranded_sage_insights(conn) -> None:
    """Repair the one state an INTERRUPTED table rebuild can leave behind.

    Migration 3 rebuilds `sage_insights` with the SQLite create-copy-drop-rename
    dance. If that rebuild is ever cut short between the RENAME and the row copy
    — a power cut, an OOM kill — the live table is gone and every row is
    stranded in `sage_insights_old`. The next boot then makes the loss PERMANENT
    and SILENT: `executescript(SCHEMA)` recreates `sage_insights` empty with the
    new DDL, migration 3 inspects that fresh DDL, sees 'auto_resolved' already
    present, returns early as a no-op, and the ledger records v3 as applied.
    Nothing errors, the server boots, and the insights are unreachable forever.
    Reproduced end-to-end 2026-08-06 (500 rows stranded, ledger [1..8], live
    table 0 rows, init_db exit 0).

    `run_pending_migrations` now owns its own transaction, so the runner can no
    longer CREATE that state. This migration exists for databases that were
    damaged BEFORE that guard, and as the standing repair for any future
    interrupted rebuild.

    THE JUDGEMENT CALL, stated rather than buried: recovered rows keep their
    ORIGINAL `state`, so insights that were 'active' at the instant of the crash
    come back active and can reappear on the ward brief. That is deliberate.
    Resurrecting a stale alert is noise the existing auto-resolver retires once
    the condition clears (migration 3 is what gave it 'auto_resolved' to write);
    silently losing 500 critical deterioration insights is a patient-safety
    event with no detector at all. Noise beats amnesia.

    NON-DESTRUCTIVE: `sage_insights_old` is left on disk, not dropped, so an
    operator can still see what happened and diff it. INSERT OR IGNORE on the
    `id` primary key means rows already recovered are skipped, so re-running
    (or a wiped ledger) inserts nothing new. A no-op on every healthy database —
    if `sage_insights_old` does not exist, this returns immediately.
    """
    stranded = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sage_insights_old'"
    ).fetchone()
    if not stranded:
        return

    new_cols = [r[1] for r in conn.execute("PRAGMA table_info(sage_insights)").fetchall()]
    old_cols = {r[1] for r in conn.execute("PRAGMA table_info(sage_insights_old)").fetchall()}
    shared = [c for c in new_cols if c in old_cols]
    if "id" not in shared:
        # No primary key in common: we cannot dedupe, so we must not merge.
        # Loud, and leave the table for a human — a bad merge into the live
        # insight feed is worse than a manual recovery.
        logger.error(
            "sage_insights_old exists but shares no 'id' column with sage_insights "
            "(shared columns: %s). NOT merging — recover it by hand.", shared
        )
        return

    cols = ", ".join(f'"{c}"' for c in shared)
    cur = conn.execute(
        f"INSERT OR IGNORE INTO sage_insights ({cols}) SELECT {cols} FROM sage_insights_old"
    )
    recovered = cur.rowcount or 0
    total = conn.execute("SELECT COUNT(*) FROM sage_insights_old").fetchone()[0]
    logger.warning(
        "Recovered %d of %d sage_insights row(s) stranded in sage_insights_old by an "
        "INTERRUPTED table rebuild. The old table is left in place on purpose — "
        "verify, then drop it by hand.", recovered, total
    )


def _m10_dose_guard(conn) -> None:
    """Bedside dose guard: the override ledger and the per-dose record of what
    was checked (services/drug_safety_service.py + services/emar_service.py).

    Two additive changes, deliberately shaped exactly like migration 6:

      * ``medication_administrations.dose_check_json`` — what the dose guard saw
        and decided for THIS dose. Nullable, and a NULL is meaningful: a dose
        charted before this migration was never dose-checked, which is a
        different statement from "checked and clean". Same reasoning as
        allergy_check_json.

      * ``medication_dose_overrides`` — WHO gave a dose the guard stopped, WHAT
        the dose was, and WHY, one row per explicit override, linked to the
        administration it authorised.

    WHY A SECOND TABLE RATHER THAN REUSING medication_allergy_overrides.
    The mechanism is deliberately identical (typed reason, fingerprint,
    carry-forward, written in the dose's own transaction) — but the allergy
    table is queried by allergy_interlock.find_carried_override with no
    discriminator column, so a dose override living in it would be a row that
    function has to not-pick-up by luck. Two clinically different overrides also
    have to stay separately answerable for a pharmacy review ("show me every
    dose given over a stopped threshold" is not "show me every dose given
    against a documented allergy"). Same pattern, own table.

    ``alert_fingerprint`` is the identity of "this drug at this dose by this
    route on this order"; the guard carries an override forward only while that
    fingerprint holds. Append-only by convention, like
    medication_administrations itself.
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info(medication_administrations)").fetchall()}
    if "dose_check_json" not in cols:
        conn.execute("ALTER TABLE medication_administrations ADD COLUMN dose_check_json TEXT")

    conn.execute(
        """CREATE TABLE IF NOT EXISTS medication_dose_overrides (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            medication_id INTEGER REFERENCES medications(id),
            patient_id TEXT NOT NULL REFERENCES patients(id),
            administration_id INTEGER REFERENCES medication_administrations(id),
            alert_fingerprint TEXT,
            decision TEXT,
            rule TEXT,
            dose_given TEXT,
            route_given TEXT,
            reason TEXT NOT NULL,
            overridden_by TEXT NOT NULL REFERENCES users(id),
            overridden_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            findings_json TEXT
        )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_med_dose_override_lookup "
        "ON medication_dose_overrides(medication_id, alert_fingerprint)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_med_dose_override_patient "
        "ON medication_dose_overrides(patient_id, overridden_at DESC)"
    )


def _m11_escalation_chain(conn) -> None:
    """The escalation chain: a deterioration that reaches a NAMED human, or is
    visibly still waiting for one.

    WHAT WAS MISSING. Vigil could score a NEWS2 19 and the only durable
    consequence was a row in ``vitals`` plus a line in a Relay case room. Nothing
    recorded that a human saw it, nothing happened when no human did, and an
    alert that scrolls off a busy channel is indistinguishable from an alert that
    was answered. Manual entry — the only vitals path that exists when a monitor
    VLAN is down — had no escalation at all.

    TWO TABLES, deliberately.

    ``clinical_escalations`` is the CURRENT STATE of one chain: which patient,
    which tier it has climbed to, who acknowledged it, whether it was resolved
    and why. It is UPDATEd, because "what is outstanding right now" is the
    question a ward board asks a hundred times a shift and deriving it from an
    event fold on every poll is how a board gets slow and then gets wrong.

    ``escalation_events`` is the APPEND-ONLY record of every step — raised, each
    repeat observation, each timed tier advance, the acknowledgement, the
    resolution. The founder's requirement is "each step recorded", and a step
    that is recorded only by mutating a status column is not recorded. Rows are
    never UPDATEd; ``seq`` orders them within a chain.

    THE ONE-ACTIVE-CHAIN-PER-PATIENT RULE IS A DATABASE CONSTRAINT, not a
    service-layer intention. ``idx_escalation_one_active_per_patient`` is a
    partial UNIQUE index over rows that are not yet resolved, so a race between
    two qualifying observations (a monitor push and a nurse's manual set landing
    in the same second — routine on a ward) cannot produce two chains for the
    same patient. Repeated deterioration EXTENDS the open chain; it does not
    start a parallel one that a clinician would have to acknowledge twice.

    WHY ``raised_in_department`` IS NOT A SCOPE SOURCE. It is a SNAPSHOT taken
    when the chain was raised, kept for display and for deriving who to page. The
    ward boundary is derived, on every read, from ``admissions.department``
    through permissions.PATIENT_DEPARTMENT_SQL — the one derivation the whole
    system shares. Scoping on a denormalised copy is how a scope silently stops
    scoping when a patient transfers ward mid-admission.
    """
    conn.execute(
        """CREATE TABLE IF NOT EXISTS clinical_escalations (
            id TEXT PRIMARY KEY,
            patient_id TEXT NOT NULL REFERENCES patients(id),
            -- Display/targeting snapshot ONLY. NEVER scope a query on this;
            -- see the docstring above.
            raised_in_department TEXT,
            status TEXT NOT NULL DEFAULT 'open'
                CHECK (status IN ('open','acknowledged','resolved')),
            trigger TEXT NOT NULL DEFAULT 'news2',
            news2 INTEGER,
            peak_news2 INTEGER,
            level TEXT,
            single_param_3 INTEGER DEFAULT 0,
            drivers_json TEXT,
            vitals_id INTEGER,
            last_vitals_id INTEGER,
            source TEXT,
            current_tier INTEGER NOT NULL DEFAULT 1,
            max_tier INTEGER NOT NULL DEFAULT 1,
            tier_role TEXT,
            tier_label TEXT,
            tier_entered_at TIMESTAMP,
            -- NULL means "no automatic advance from here": the final tier, or an
            -- acknowledged chain whose ladder has stopped.
            tier_due_at TIMESTAMP,
            target_user_id TEXT REFERENCES users(id),
            target_description TEXT,
            raised_at TIMESTAMP NOT NULL,
            acknowledged_at TIMESTAMP,
            acknowledged_by TEXT REFERENCES users(id),
            acknowledged_tier INTEGER,
            resolved_at TIMESTAMP,
            resolved_by TEXT REFERENCES users(id),
            resolution_reason TEXT,
            resolution_category TEXT,
            -- The tier policy in force AT RAISE TIME. A site that edits its
            -- timings must not retroactively rewrite why an old chain climbed
            -- when it did.
            policy_json TEXT,
            updated_at TIMESTAMP
        )"""
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_escalation_one_active_per_patient "
        "ON clinical_escalations(patient_id) WHERE status != 'resolved'"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_escalation_due "
        "ON clinical_escalations(status, tier_due_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_escalation_patient_raised "
        "ON clinical_escalations(patient_id, raised_at DESC)"
    )

    conn.execute(
        """CREATE TABLE IF NOT EXISTS escalation_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            escalation_id TEXT NOT NULL REFERENCES clinical_escalations(id),
            seq INTEGER NOT NULL,
            event_type TEXT NOT NULL,
            tier INTEGER,
            tier_role TEXT,
            target_user_id TEXT,
            target_description TEXT,
            actor_user_id TEXT,
            actor_role TEXT,
            news2 INTEGER,
            detail TEXT,
            metadata_json TEXT,
            occurred_at TIMESTAMP NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_escalation_events_chain "
        "ON escalation_events(escalation_id, seq)"
    )


def _m12_device_pin_identity(conn) -> None:
    """PIN-on-device identity for shared ward tablets (auth.py + auth_routes.py).

    THE PROBLEM. A full password per patient interaction is what makes staff
    share logins on a trolley tablet, and a shared login destroys attribution:
    every audit row says NUR001 when three nurses touched the chart. So the
    tablet is enrolled ONCE and holds a long-lived device credential; a PERSON
    then authenticates on top of it with a short PIN. Two additive changes:

      * ``enrolled_devices`` — one row per tablet. ``secret_hash`` is a SHA-256
        digest of a 256-bit random secret, NOT bcrypt, and that is deliberate:
        bcrypt exists to make LOW-entropy secrets expensive to guess, and this
        secret is not guessable at any cost. Paying ~200 ms of bcrypt on every
        PIN unlock would buy nothing and would sit on the hot path of the one
        interaction the whole feature exists to make fast. The digest is UNIQUE
        so the lookup is a single index probe.

        ``revoked_at`` (not a DELETE) because a revoked tablet must stay
        answerable: the audit rows written from it name a device id, and a
        deleted row makes those rows unreadable. Revocation is also what kills
        the sessions minted on that tablet — auth.get_current_user re-checks the
        device on every request that carries one.

        ``failed_pin_attempts`` / ``pin_locked_until`` throttle the SWEEP that a
        per-account limit cannot see: five guesses each across forty staff is two
        hundred guesses on one tablet, and no single account ever trips.

      * ``users.device_pin_*`` — the tablet PIN, its provisioning timestamp, and
        its OWN failure counter.

        WHY A SEPARATE COLUMN FROM ``users.pin_hash``. pin_hash is the
        CO-SIGNING PIN that authorises agentic medication dispatch. If unlocking
        the tablet used the same secret, anyone who shoulder-surfs an unlock
        typed thirty times a shift on a shared screen could also countersign a
        med order in that nurse's name — two independent controls collapsed into
        one. Separate columns also mean the tablet PIN can be rotated without
        touching signing authority.

        WHY A SEPARATE LOCKOUT FROM ``login_locked_until``. Sharing the counter
        would mean a PIN brute-force also locks the password path, i.e. a
        clinician stranded out of the EHR mid-shift — itself a patient-safety
        event. Separate counters make a PIN lock degrade to "use your password"
        rather than "you are out". They are separate credentials, separately
        throttled; the admin unlock endpoint clears both in one action.

    Additive and idempotent: every column is introspection-guarded and the table
    is IF NOT EXISTS, so a re-run (or a wiped ledger) changes nothing.
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info(users)").fetchall()}
    if "device_pin_hash" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN device_pin_hash TEXT")
    if "device_pin_set_at" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN device_pin_set_at TIMESTAMP")
    if "device_pin_failed_attempts" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN device_pin_failed_attempts INTEGER DEFAULT 0")
    if "device_pin_locked_until" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN device_pin_locked_until REAL")

    conn.execute(
        """CREATE TABLE IF NOT EXISTS enrolled_devices (
            id                  TEXT PRIMARY KEY,
            label               TEXT NOT NULL,
            department          TEXT,
            secret_hash         TEXT NOT NULL UNIQUE,
            enrolled_by         TEXT REFERENCES users(id),
            enrolled_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_seen_at        TIMESTAMP,
            revoked_at          TIMESTAMP,
            revoked_by          TEXT REFERENCES users(id),
            failed_pin_attempts INTEGER NOT NULL DEFAULT 0,
            pin_locked_until    REAL
        )"""
    )
    # The admin device list is "active first, newest first"; a hospital has tens
    # of tablets, not thousands, but the index keeps the revoked ones from being
    # scanned on every page load.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_enrolled_devices_active "
        "ON enrolled_devices(revoked_at, enrolled_at DESC)"
    )


def _m13_order_sets(conn) -> None:
    """Order sets / protocols: the application ledger, the link from each order
    back to it, and the local codes the shipped starter protocols order.

    Three additive changes.

      * ``order_set_applications`` — ONE row per "the sepsis bundle was applied
        to this patient by this person at this time, citing this guideline, and
        these are the orders it produced". The per-order `order_events` rows
        already say what each order is; none of them says they were one protocol
        rather than eight independent decisions, and that is exactly the
        question asked after an incident and at pilot review.

        ``config_fingerprint`` is the hash of the set AS IT WAS IN THE FILE at
        the moment it was applied. Protocol files get edited — a new antibiotic,
        a changed fluid volume — and without the fingerprint there is no way to
        know afterwards which version of the bundle a patient actually received.

        ``safety_json`` is the per-item verdict (allergy alerts, interaction
        alerts, dose-guard decision) as of application. Same reasoning as
        ``medication_administrations.allergy_check_json``: what the screen saw
        at the time is evidence, and re-deriving it later from today's allergy
        list answers a different question.

      * ``orders.order_set_application_id`` — nullable, and a NULL is meaningful:
        the order was typed individually, which is a different clinical act from
        the same order arriving inside a protocol. Never backfilled.

      * LOCAL terminology codes for the shipped starter protocols. Medication,
        lab, imaging and procedure orders must carry a code that validates
        against `terminology` (services/orders_service), and Orb's curated
        starter terminology is LOINC/SNOMED/ICD-10 only — it has no drug codes
        at all, so without these rows every medication item in every shipped
        protocol would fail validation on the ward.

        WHY LOCAL CODES AND NOT RxNorm. Inventing plausible-looking RXCUIs for
        an offline hospital appliance is how a fake identifier ends up in a
        discharge summary and then in somebody's national submission. These are
        honestly labelled as this deployment's own codes; a site that has loaded
        RxNorm (scripts/load_rxnorm_formulary.py) should edit
        site_config/order_sets.json to use its real codes, and only the file
        changes.
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info(orders)").fetchall()}
    if "order_set_application_id" not in cols:
        conn.execute("ALTER TABLE orders ADD COLUMN order_set_application_id INTEGER")

    conn.execute(
        """CREATE TABLE IF NOT EXISTS order_set_applications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            patient_id TEXT NOT NULL REFERENCES patients(id),
            set_id TEXT NOT NULL,
            set_name TEXT NOT NULL,
            guideline TEXT,
            config_fingerprint TEXT,
            applied_by TEXT REFERENCES users(id),
            applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            order_count INTEGER NOT NULL DEFAULT 0,
            order_ids_json TEXT,
            safety_json TEXT
        )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_order_set_app_patient "
        "ON order_set_applications(patient_id, id DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_orders_set_application "
        "ON orders(order_set_application_id)"
    )

    conn.executemany(
        "INSERT OR IGNORE INTO terminology (code_system, code, display, is_active) "
        "VALUES (?, ?, ?, 1)",
        _ORDER_SET_LOCAL_CODES,
    )


# Local (this-deployment) codes for the shipped starter protocols. Every display
# ends with "(Orb local code)" so nobody mistakes one for a national identifier.
_ORDER_SET_LOCAL_CODES = [
    ("LOCAL", "ORB-LAB-BLOOD-CULTURE", "Blood culture, two sets (Orb local code)"),
    ("LOCAL", "ORB-LAB-ABG", "Arterial blood gas (Orb local code)"),
    ("LOCAL", "ORB-LAB-VBG", "Venous blood gas (Orb local code)"),
    ("LOCAL", "ORB-LAB-BLOOD-KETONES", "Capillary blood ketones, beta-hydroxybutyrate (Orb local code)"),
    ("LOCAL", "ORB-IMG-CXR", "Chest X-ray, portable (Orb local code)"),
    ("LOCAL", "ORB-PROC-ECG", "12-lead electrocardiogram (Orb local code)"),
    ("LOCAL", "ORB-MED-OXYGEN", "Oxygen (Orb local code)"),
    ("LOCAL", "ORB-MED-CEFTRIAXONE", "Ceftriaxone (Orb local code)"),
    ("LOCAL", "ORB-MED-NACL09", "Sodium chloride 0.9% intravenous infusion (Orb local code)"),
    ("LOCAL", "ORB-MED-ASPIRIN", "Aspirin (Orb local code)"),
    ("LOCAL", "ORB-MED-CLOPIDOGREL", "Clopidogrel (Orb local code)"),
    ("LOCAL", "ORB-MED-GTN", "Glyceryl trinitrate (Orb local code)"),
    ("LOCAL", "ORB-MED-MORPHINE", "Morphine (Orb local code)"),
    ("LOCAL", "ORB-MED-ATORVASTATIN", "Atorvastatin (Orb local code)"),
    ("LOCAL", "ORB-MED-INSULIN-REG", "Insulin, soluble human (Orb local code)"),
    ("LOCAL", "ORB-MED-KCL", "Potassium chloride (Orb local code)"),
    ("LOCAL", "ORB-MED-PARACETAMOL", "Paracetamol (Orb local code)"),
    ("LOCAL", "ORB-MED-ONDANSETRON", "Ondansetron (Orb local code)"),
    ("LOCAL", "ORB-MED-ENOXAPARIN", "Enoxaparin sodium (Orb local code)"),
    ("LOCAL", "ORB-MED-SALBUTAMOL", "Salbutamol (Orb local code)"),
    ("LOCAL", "ORB-MED-IPRATROPIUM", "Ipratropium bromide (Orb local code)"),
    ("LOCAL", "ORB-MED-PREDNISOLONE", "Prednisolone (Orb local code)"),
    ("LOCAL", "ORB-MED-HYDROCORTISONE", "Hydrocortisone sodium succinate (Orb local code)"),
]


def _m14_dpdp_act_machinery(conn) -> None:
    """The Digital Personal Data Protection Act 2023 record layer.

    Seven tables, three of them append-only at the DB layer for the same reason
    ``audit_logs`` is: a consent record and an erasure decision are EVIDENCE, and
    evidence that the application can quietly rewrite is not evidence. The Data
    Protection Board can ask a hospital to show that a consent existed on a given
    date and was withdrawn on another; a mutable row cannot answer that.

      * ``consent_records`` — one immutable row per consent given. WHAT was
        consented to (purpose + scope_detail), WHEN (granted_at), BY WHAT MEANS
        (method), and WHO recorded it (recorded_by/_role). ``notice_version`` and
        ``notice_language`` are here because DPDP §5 makes the NOTICE part of the
        consent: a consent taken against a notice nobody can reproduce, or in a
        language the principal does not read, is not a consent. ``given_by`` +
        ``proxy_*`` carry §9 guardian consent for children and persons with
        disability, which in an Indian ward is the common case, not the edge one.

      * ``consent_withdrawals`` — one immutable row per withdrawal, pointing at
        the consent it retires. THE WITHDRAWAL IS A NEW FACT, NOT AN EDIT. There
        is deliberately no ``status`` column on ``consent_records`` to flip:
        effective status is DERIVED from the presence of a withdrawal row (see
        services/consent_service._effective_status). A status column would be a
        second, writable source of truth for the same question, and the first
        time the two disagreed the hospital would have no way to tell which one
        the auditor should believe. UNIQUE(consent_id) makes double-withdrawal a
        database-level impossibility rather than an application convention.

      * ``retention_policies`` — per-category retention, its lawful basis, the
        rule it comes from, and — the load-bearing column — ``erasable``. This is
        where the erasure/retention collision is ENCODED rather than argued about
        per request; consent_service.adjudicate_erasure_request reads it.
        Seeded below, and editable by a site whose counsel says otherwise (it is
        the one table here that is deliberately mutable: the law changes, and a
        hospital must be able to correct a policy without a code deploy).

      * ``erasure_requests`` / ``erasure_decisions`` — the request has a
        lifecycle (recorded → decided → executed) so it is mutable; the DECISION
        is immutable, one row per category, each carrying the disposition, the
        stated reason and the cited authority. A refusal that cannot be produced
        later is indistinguishable from ignoring the request.

      * ``breach_records`` / ``breach_notifications`` — DPDP §8(6) requires
        notifying the Board AND every affected Data Principal of a personal data
        breach. The header row holds what was exposed and to whom; each
        notification is its own immutable row, because "we told the Board" and
        "we told the patients" are two different obligations discharged at two
        different times, and one timestamp column cannot record both.

    Additive and idempotent throughout: every CREATE is IF NOT EXISTS and the
    seed rows are INSERT OR IGNORE, so a re-run (or a wiped ledger) is a no-op.
    """
    # ── Consent ──────────────────────────────────────────────────────────────
    conn.execute(
        """CREATE TABLE IF NOT EXISTS consent_records (
            id                  TEXT PRIMARY KEY,
            patient_id          TEXT NOT NULL REFERENCES patients(id),
            purpose             TEXT NOT NULL,
            scope_detail        TEXT,
            notice_version      TEXT,
            notice_language     TEXT,
            method              TEXT NOT NULL,
            granted_at          TIMESTAMP NOT NULL,
            expires_at          TIMESTAMP,
            given_by            TEXT NOT NULL DEFAULT 'patient',
            proxy_name          TEXT,
            proxy_relationship  TEXT,
            evidence_ref        TEXT,
            recorded_by         TEXT NOT NULL,
            recorded_by_role    TEXT,
            created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_consent_patient "
        "ON consent_records(patient_id, granted_at DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_consent_patient_purpose "
        "ON consent_records(patient_id, purpose)"
    )
    conn.execute(
        """CREATE TRIGGER IF NOT EXISTS trg_consent_records_no_update
           BEFORE UPDATE ON consent_records
           BEGIN
               SELECT RAISE(ABORT, 'consent_records is append-only: a withdrawal is a new row in consent_withdrawals, never an edit');
           END"""
    )
    conn.execute(
        """CREATE TRIGGER IF NOT EXISTS trg_consent_records_no_delete
           BEFORE DELETE ON consent_records
           BEGIN
               SELECT RAISE(ABORT, 'consent_records is append-only: DELETE is forbidden');
           END"""
    )

    conn.execute(
        """CREATE TABLE IF NOT EXISTS consent_withdrawals (
            id                  TEXT PRIMARY KEY,
            consent_id          TEXT NOT NULL REFERENCES consent_records(id),
            patient_id          TEXT NOT NULL,
            withdrawn_at        TIMESTAMP NOT NULL,
            method              TEXT NOT NULL,
            reason              TEXT,
            recorded_by         TEXT NOT NULL,
            recorded_by_role    TEXT,
            created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )"""
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_consent_withdrawal_unique "
        "ON consent_withdrawals(consent_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_consent_withdrawal_patient "
        "ON consent_withdrawals(patient_id, withdrawn_at DESC)"
    )
    conn.execute(
        """CREATE TRIGGER IF NOT EXISTS trg_consent_withdrawals_no_update
           BEFORE UPDATE ON consent_withdrawals
           BEGIN
               SELECT RAISE(ABORT, 'consent_withdrawals is append-only: UPDATE is forbidden');
           END"""
    )
    conn.execute(
        """CREATE TRIGGER IF NOT EXISTS trg_consent_withdrawals_no_delete
           BEFORE DELETE ON consent_withdrawals
           BEGIN
               SELECT RAISE(ABORT, 'consent_withdrawals is append-only: DELETE is forbidden');
           END"""
    )

    # ── Retention policy ─────────────────────────────────────────────────────
    conn.execute(
        """CREATE TABLE IF NOT EXISTS retention_policies (
            category        TEXT PRIMARY KEY,
            description     TEXT NOT NULL,
            retention_days  INTEGER,
            anchor          TEXT NOT NULL DEFAULT 'record_created',
            legal_basis     TEXT NOT NULL,
            authority       TEXT NOT NULL,
            erasable        INTEGER NOT NULL DEFAULT 0,
            disposition     TEXT NOT NULL,
            updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_by      TEXT
        )"""
    )
    conn.executemany(
        """INSERT OR IGNORE INTO retention_policies
           (category, description, retention_days, anchor, legal_basis, authority,
            erasable, disposition)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        _DPDP_RETENTION_SEED,
    )

    # ── Erasure ──────────────────────────────────────────────────────────────
    conn.execute(
        """CREATE TABLE IF NOT EXISTS erasure_requests (
            id                  TEXT PRIMARY KEY,
            patient_id          TEXT NOT NULL REFERENCES patients(id),
            requested_at        TIMESTAMP NOT NULL,
            requested_by        TEXT NOT NULL,
            requester_identity  TEXT NOT NULL DEFAULT 'patient',
            channel             TEXT,
            categories_json     TEXT,
            note                TEXT,
            status              TEXT NOT NULL DEFAULT 'recorded'
                                CHECK (status IN ('recorded','decided','executed')),
            decided_at          TIMESTAMP,
            decided_by          TEXT,
            executed_at         TIMESTAMP,
            executed_by         TEXT,
            created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_erasure_requests_patient "
        "ON erasure_requests(patient_id, requested_at DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_erasure_requests_status "
        "ON erasure_requests(status, requested_at DESC)"
    )

    conn.execute(
        """CREATE TABLE IF NOT EXISTS erasure_decisions (
            id              TEXT PRIMARY KEY,
            request_id      TEXT NOT NULL REFERENCES erasure_requests(id),
            patient_id      TEXT NOT NULL,
            category        TEXT NOT NULL,
            disposition     TEXT NOT NULL
                            CHECK (disposition IN ('honoured','refused','not_applicable')),
            basis           TEXT NOT NULL,
            reason          TEXT NOT NULL,
            authority       TEXT,
            executable      INTEGER NOT NULL DEFAULT 0,
            rows_erased     INTEGER,
            decided_at      TIMESTAMP NOT NULL,
            decided_by      TEXT NOT NULL,
            executed_at     TIMESTAMP,
            created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )"""
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_erasure_decision_unique "
        "ON erasure_decisions(request_id, category)"
    )
    # `rows_erased` / `executed_at` are filled in by execution, which happens
    # AFTER the decision row is sealed. An immutable table cannot be back-filled,
    # so execution writes its own row into `erasure_executions` instead — the
    # decision says what was allowed, the execution says what was actually done,
    # and the two are separate facts on separate dates.
    conn.execute(
        """CREATE TRIGGER IF NOT EXISTS trg_erasure_decisions_no_update
           BEFORE UPDATE ON erasure_decisions
           BEGIN
               SELECT RAISE(ABORT, 'erasure_decisions is append-only: record a new execution row, never edit a decision');
           END"""
    )
    conn.execute(
        """CREATE TRIGGER IF NOT EXISTS trg_erasure_decisions_no_delete
           BEFORE DELETE ON erasure_decisions
           BEGIN
               SELECT RAISE(ABORT, 'erasure_decisions is append-only: DELETE is forbidden');
           END"""
    )

    conn.execute(
        """CREATE TABLE IF NOT EXISTS erasure_executions (
            id              TEXT PRIMARY KEY,
            request_id      TEXT NOT NULL REFERENCES erasure_requests(id),
            decision_id     TEXT NOT NULL,
            patient_id      TEXT NOT NULL,
            category        TEXT NOT NULL,
            rows_erased     INTEGER NOT NULL DEFAULT 0,
            detail_json     TEXT,
            executed_at     TIMESTAMP NOT NULL,
            executed_by     TEXT NOT NULL,
            created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_erasure_executions_request "
        "ON erasure_executions(request_id)"
    )
    conn.execute(
        """CREATE TRIGGER IF NOT EXISTS trg_erasure_executions_no_update
           BEFORE UPDATE ON erasure_executions
           BEGIN
               SELECT RAISE(ABORT, 'erasure_executions is append-only: UPDATE is forbidden');
           END"""
    )
    conn.execute(
        """CREATE TRIGGER IF NOT EXISTS trg_erasure_executions_no_delete
           BEFORE DELETE ON erasure_executions
           BEGIN
               SELECT RAISE(ABORT, 'erasure_executions is append-only: DELETE is forbidden');
           END"""
    )

    # ── Breach register ──────────────────────────────────────────────────────
    conn.execute(
        """CREATE TABLE IF NOT EXISTS breach_records (
            id                          TEXT PRIMARY KEY,
            nature                      TEXT NOT NULL,
            description                 TEXT NOT NULL,
            occurred_at                 TIMESTAMP,
            discovered_at               TIMESTAMP NOT NULL,
            exposed_to                  TEXT,
            data_categories_json        TEXT,
            records_affected            INTEGER,
            principals_affected         INTEGER,
            affected_patient_ids_json   TEXT,
            containment_at              TIMESTAMP,
            containment_actions         TEXT,
            board_notified_at           TIMESTAMP,
            principals_notified_at      TIMESTAMP,
            status                      TEXT NOT NULL DEFAULT 'open'
                                        CHECK (status IN ('open','contained','notified','closed')),
            reported_by                 TEXT NOT NULL,
            created_at                  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at                  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_breach_records_discovered "
        "ON breach_records(discovered_at DESC)"
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS breach_notifications (
            id              TEXT PRIMARY KEY,
            breach_id       TEXT NOT NULL REFERENCES breach_records(id),
            party_type      TEXT NOT NULL
                            CHECK (party_type IN ('board','data_principal','regulator','internal','other')),
            notified_party  TEXT NOT NULL,
            notified_at     TIMESTAMP NOT NULL,
            method          TEXT,
            reference       TEXT,
            content_summary TEXT,
            recorded_by     TEXT NOT NULL,
            created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_breach_notifications_breach "
        "ON breach_notifications(breach_id, notified_at)"
    )
    conn.execute(
        """CREATE TRIGGER IF NOT EXISTS trg_breach_notifications_no_update
           BEFORE UPDATE ON breach_notifications
           BEGIN
               SELECT RAISE(ABORT, 'breach_notifications is append-only: UPDATE is forbidden');
           END"""
    )
    conn.execute(
        """CREATE TRIGGER IF NOT EXISTS trg_breach_notifications_no_delete
           BEFORE DELETE ON breach_notifications
           BEGIN
               SELECT RAISE(ABORT, 'breach_notifications is append-only: DELETE is forbidden');
           END"""
    )


# The shipped retention matrix. Each row is
#   (category, description, retention_days, anchor, legal_basis, authority,
#    erasable, disposition)
#
# `erasable` IS THE COLLISION, RESOLVED IN DATA. It answers one question: may a
# DPDP §12(3) erasure request reach this category at all? 0 means another law
# compels Orb to keep it and the request is refused WITH the `authority` string
# quoted back to the data principal; 1 means Orb holds it under consent or
# convenience and the request is honoured.
#
# The day counts are the conservative reading of Indian practice, and a site
# whose counsel reads them differently edits this TABLE, not this file — which is
# exactly why `retention_policies` is the one mutable table in this migration.
_DPDP_RETENTION_SEED = [
    (
        "clinical_record",
        "Inpatient and outpatient medical records: notes, discharge summaries, "
        "observations, problem list, admissions.",
        1095, "last_clinical_activity", "legal_obligation",
        "Indian Medical Council (Professional Conduct, Etiquette and Ethics) "
        "Regulations 2002, Reg. 1.3.1: indoor records retained 3 years from the "
        "commencement of treatment. Retention also required to answer a claim "
        "within the Limitation Act 1963 / Consumer Protection Act 2019 window."
        " A patient who was a MINOR is protected for longer: Limitation Act 1963 s.6 starts the limitation period at majority, so the window closes at 18+3=21. Enforced as a floor by consent_service.retention_review, not by this row.",
        0, "retain: review after the limitation window, destroy only on a signed order",
    ),
    (
        "medico_legal_record",
        "Records attached to a medico-legal case, police requisition, or live "
        "litigation.",
        None, "last_clinical_activity", "legal_obligation",
        "Retained until the proceedings conclude and any appeal window closes. "
        "No fixed period; destruction requires the hospital's legal officer.",
        0, "retain indefinitely until legal hold is lifted",
    ),
    (
        "prescription_and_dispensing",
        "Prescriptions, medication orders and administration records.",
        1095, "record_created", "legal_obligation",
        "Drugs and Cosmetics Rules 1945: Schedule H1 register retained 3 years; "
        "dispensing records retained under Rule 65."
        " A patient who was a MINOR is protected for longer: Limitation Act 1963 s.6 starts the limitation period at majority, so the window closes at 18+3=21. Enforced as a floor by consent_service.retention_review, not by this row.",
        0, "retain: destroy only with the pharmacist-in-charge's sign-off",
    ),
    (
        "imaging",
        "Radiology and other clinical images with their reports.",
        1095, "record_created", "legal_obligation",
        "Pre-Conception and Pre-Natal Diagnostic Techniques Act 1994, s.29 sets a "
        "2-year floor for records under that Act; Orb applies the 3-year clinical "
        "record period as the safer single rule."
        " A patient who was a MINOR is protected for longer: Limitation Act 1963 s.6 starts the limitation period at majority, so the window closes at 18+3=21. Enforced as a floor by consent_service.retention_review, not by this row.",
        0, "retain with the clinical record",
    ),
    (
        "audit_trail",
        "The tamper-evident access and mutation log (audit_logs).",
        2920, "record_created", "legal_obligation",
        "DPDP Act 2023 s.8(5) reasonable security safeguards: the trail is the "
        "evidence the safeguards existed. Rows are also chained by SHA-256: "
        "removing one destroys tamper-evidence for every row after it.",
        0, "never erase: the chain is the control",
    ),
    (
        "breach_record",
        "Personal data breach register and the notifications sent for it.",
        2920, "record_created", "legal_obligation",
        "DPDP Act 2023 s.8(6) breach notification to the Data Protection Board "
        "and to affected Data Principals; the register is the proof it was done.",
        0, "never erase",
    ),
    (
        "consent_record",
        "Consent records and their withdrawals.",
        None, "record_created", "legal_obligation",
        "DPDP Act 2023 s.5 and s.6: the Data Fiduciary must be able to "
        "demonstrate that valid consent was given and when it was withdrawn.",
        0, "never erase: this is the proof consent existed",
    ),
    (
        "ai_interaction_transcript",
        "Sage working memory: AI observations, room memory, decision cache, "
        "insight feedback. Derived material, not the signed clinical record.",
        365, "record_created", "consent",
        "Held to make the assistant useful, not because any law requires it. "
        "DPDP s.8(7): erase on withdrawal of consent or when the purpose is served.",
        1, "erase on request; sweep annually",
    ),
    (
        "ambient_audio",
        "Raw Scribe dictation audio kept after the note it produced was signed.",
        30, "record_created", "consent",
        "The SIGNED NOTE is the medical record; the audio is working material. "
        "No Indian rule requires keeping the recording once the note is final.",
        1, "erase 30 days after the note is signed",
    ),
    (
        "patient_portal_account",
        "Bridge portal login, device credentials and discharge access tokens.",
        None, "record_created", "consent",
        "An account is a convenience, not a medical record. Revoking it does not "
        "touch the clinical record it gave access to.",
        1, "erase/revoke on request",
    ),
    (
        "non_clinical_contact",
        "Contact details held for messaging and reminders that are not part of "
        "the medical record proforma.",
        None, "record_created", "consent",
        "DPDP s.8(7). NOTE: name, address and next-of-kin inside the medical "
        "record proforma are NOT this category, those fall under clinical_record.",
        1, "erase on request",
    ),
    (
        "marketing_communications",
        "Any contact list used for outreach, campaigns or feedback drives.",
        None, "record_created", "consent",
        "Pure consent basis. Withdrawal must be honoured in full.",
        1, "erase on request",
    ),
]


def _m15_history_import(conn) -> None:
    """Provenance + ledgers for BULK HISTORICAL RECORD IMPORT
    (services/history_import_service.py).

    Orb REPLACES the incumbent EHR, so on cutover day years of existing clinical
    history have to arrive from the old system. Three things have to be true of
    every row that arrives that way, and this migration is what makes them
    representable in the schema.

    1. PROVENANCE IS CLINICAL — a clinician reading a chart must be able to tell
       "this came from the old system" from "this was written in Orb". A drug
       allergy typed by our own pharmacist and one migrated out of a legacy free
       -text field are not the same evidence, and treating them as the same is
       how a migration launders bad data into a trusted record. So every
       importable table gains:

         import_source_system  the old system's name, e.g. 'MediSys HIS'
         import_external_id    that system's own id for THIS record
         import_run_id         the run that wrote it (→ history_import_runs)

       All three NULL is the honest signal for Orb-native. The columns are
       nullable and additive, so every existing row keeps that meaning for free.

    2. IDEMPOTENCY IS ENFORCED BY THE DATABASE, NOT BY THE IMPORTER'S MEMORY —
       a partial unique index per table on (import_source_system,
       import_external_id) means a re-run, a double-click, or two admins
       committing the same export cannot produce two copies of one historical
       record. Partial (WHERE import_external_id IS NOT NULL) so it constrains
       only imported rows and never Orb-native ones.

    3. IDENTITY ACROSS TWO SYSTEMS IS A MAPPING, NOT A COLUMN —
       ``history_import_patient_xref``. Child records (diagnoses, medications,
       results, documents) reference patients by the OLD system's patient id,
       which is not Orb's. The xref is the authoritative translation, and it is
       written whether the patient was CREATED by an import or MATCHED to a
       patient Orb already held (from roster import, or from an earlier file) —
       which is exactly why the mapping cannot live in patients.import_external_id
       alone: stamping an Orb-native patient row with import provenance would be
       a lie about where that row came from, but the mapping still has to exist.

       UNIQUE is on (source_system, external_patient_id) ONLY. Many external ids
       may point at ONE Orb patient — that is the legitimate representation of
       the same human held twice under two MRNs in the old system, and the
       importer reports it rather than resolving it silently.

    Two ledgers complete the trail:

      * ``history_import_runs`` — one row per APPLIED run: who, which file (with
        its SHA-256, so the exact bytes that produced these records are
        identifiable years later), which mapping, and what it did.

      * ``history_import_rejects`` — EVERY row that did not import, and why.
        This is the whole point of the exercise. A migration that silently drops
        4% of records is a catastrophe discovered months later by a clinician
        looking for a result that is simply not there; a durable, complete,
        queryable reject list is what turns that into a work list. The API caps
        how much reject detail it returns inline, so this table — not the HTTP
        response — is the complete record.

        ``raw_row`` holds the offending line verbatim, which is PHI. It is
        reachable only through admin-gated, audited endpoints, and the table
        carries ``patient_id`` so that when a reject DID resolve to a patient it
        is discovered and purged by the schema-derived erasure plan in
        routes/admin_routes.py. A reject that never resolved to an Orb patient
        is, by construction, not reachable by per-patient erasure.

    Additive and idempotent throughout: every column is introspection-guarded,
    every table/index is IF NOT EXISTS.
    """
    # ── 0: somewhere to put a legacy encounter's END ─────────────────────────
    # A past admission has a discharge date, and Orb had nowhere to record one.
    # Orb-NATIVE discharges are timestamped on ``discharge_summaries.discharged_at``
    # (see services/discharge_service.py) because a native discharge always
    # produces a summary; a MIGRATED encounter has no summary to hang it on, and
    # dropping the date would turn every historical stay into an open-ended one
    # and destroy length-of-stay for the whole imported history.
    #
    # READ THIS BEFORE USING THE COLUMN: it is NOT a general "was this
    # discharged" test and must never be used as one. ``admissions.status`` is
    # that test. This column is NULL for every Orb-native row and populated only
    # where an import knew the date.
    cols = {r[1] for r in conn.execute("PRAGMA table_info(admissions)").fetchall()}
    if cols and "discharged_at" not in cols:
        conn.execute("ALTER TABLE admissions ADD COLUMN discharged_at TIMESTAMP")

    # ── 1 + 2: provenance columns and the idempotency index, per table ────────
    for table in ("patients", "admissions", "problem_list", "medications",
                  "observations", "clinical_documents"):
        cols = {r[1] for r in conn.execute(f'PRAGMA table_info("{table}")').fetchall()}
        if not cols:
            # Table absent on a minimal/older DB — nothing to decorate, and the
            # importer refuses that record type rather than half-writing it.
            continue
        for column in ("import_run_id", "import_source_system", "import_external_id"):
            if column not in cols:
                conn.execute(f'ALTER TABLE "{table}" ADD COLUMN {column} TEXT')
        conn.execute(
            f'CREATE UNIQUE INDEX IF NOT EXISTS idx_{table}_import_external '
            f'ON "{table}"(import_source_system, import_external_id) '
            f'WHERE import_external_id IS NOT NULL'
        )
        # The "show me everything that came from the old system" query, and the
        # per-run rollback/inspection path.
        conn.execute(
            f'CREATE INDEX IF NOT EXISTS idx_{table}_import_run '
            f'ON "{table}"(import_run_id)'
        )

    # ── 3: cross-system patient identity ─────────────────────────────────────
    conn.execute(
        """CREATE TABLE IF NOT EXISTS history_import_patient_xref (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            source_system       TEXT NOT NULL,
            external_patient_id TEXT NOT NULL,
            patient_id          TEXT NOT NULL REFERENCES patients(id),
            mrn_at_import       TEXT,
            created_by_import   INTEGER NOT NULL DEFAULT 0,
            import_run_id       TEXT,
            created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(source_system, external_patient_id)
        )"""
    )
    # Reverse lookup: "which old-system ids does this patient answer to?" — the
    # question asked when two legacy records disagree about one human.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_history_xref_patient "
        "ON history_import_patient_xref(patient_id)"
    )

    # ── Run ledger ───────────────────────────────────────────────────────────
    conn.execute(
        """CREATE TABLE IF NOT EXISTS history_import_runs (
            id              TEXT PRIMARY KEY,
            record_type     TEXT NOT NULL,
            source_system   TEXT NOT NULL,
            actor_user_id   TEXT REFERENCES users(id),
            source_filename TEXT,
            source_sha256   TEXT,
            source_bytes    INTEGER,
            mapping_json    TEXT NOT NULL,
            date_order      TEXT NOT NULL DEFAULT 'auto',
            rows_total      INTEGER NOT NULL DEFAULT 0,
            rows_created    INTEGER NOT NULL DEFAULT 0,
            rows_updated    INTEGER NOT NULL DEFAULT 0,
            rows_unchanged  INTEGER NOT NULL DEFAULT 0,
            rows_failed     INTEGER NOT NULL DEFAULT 0,
            warning_count   INTEGER NOT NULL DEFAULT 0,
            summary_json    TEXT,
            note            TEXT,
            started_at      TIMESTAMP,
            finished_at     TIMESTAMP
        )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_history_import_runs_started "
        "ON history_import_runs(started_at DESC)"
    )

    # ── Reject ledger — the complete "what did NOT come across" record ───────
    conn.execute(
        """CREATE TABLE IF NOT EXISTS history_import_rejects (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id              TEXT NOT NULL REFERENCES history_import_runs(id),
            line                INTEGER,
            severity            TEXT NOT NULL DEFAULT 'error',
            code                TEXT NOT NULL,
            field               TEXT,
            message             TEXT NOT NULL,
            external_id         TEXT,
            external_patient_id TEXT,
            patient_id          TEXT REFERENCES patients(id),
            raw_row             TEXT
        )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_history_import_rejects_run "
        "ON history_import_rejects(run_id, line)"
    )


def _m16_sage_conversation_threads(conn) -> None:
    """Give every Sage turn a CONVERSATION id so "New Chat" actually starts one.

    Before this, ``get_conversation_history(user_id, limit=6)`` was scoped to the
    USER and nothing else — the server unconditionally prepended that clinician's
    last six turns to every prompt. The Sage panel clears its own message array
    on mount and on "New Chat", so the clinician sees an empty thread, but the
    prompt does not: the previous conversation is still in it.

    That is not merely cosmetic. Observed in the browser 2026-08-08: a fresh
    login asked a purely general question ("What is the diagnostic threshold for
    diabetic ketoacidosis?") on the Sage landing screen with no chart open, and
    the answer came back naming a specific admitted patient and quoting a
    glucose value for them — because a previous turn about that patient was
    still in the prefill. On a shared ward workstation that is a PHI-crossing
    problem, and it also explains why Sage got monotonically slower over a
    session: every turn re-paid the prefill cost of the six before it.

    ``conversation_id`` is nullable. NULL means "written before threads existed";
    those rows stay readable in the history list and are simply never selected as
    prompt context, which is the safe reading of an unknown thread.
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info(sage_conversations)").fetchall()}
    if "conversation_id" not in cols:
        conn.execute("ALTER TABLE sage_conversations ADD COLUMN conversation_id TEXT")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_sage_conversation "
        "ON sage_conversations(user_id, conversation_id, timestamp)"
    )


def _m17_patient_review_marks(conn) -> None:
    """Per-clinician "I have read this chart up to here" watermark.

    One row per (user, patient). ``last_reviewed_at`` is the instant that
    clinician last said "I have seen this chart", and the chart timeline uses it
    to draw the *since you last looked* line. It is deliberately PER USER: the
    consultant's watermark must not be advanced by the night nurse opening the
    same chart, or the marker answers the wrong question.

    Three properties this table has on purpose:

    * **It stores no PHI.** Two foreign ids and a timestamp. It is a bookmark,
      not a record — nothing here needs field encryption, and a leak of the whole
      table would disclose which clinician read which chart, which the audit log
      already records anyway.
    * **It is not a read receipt for compliance.** ``phi_access`` rows in
      ``audit_logs`` remain the evidence of who read what; this is a UI
      convenience that a clinician can move forward at will. Nothing safety-
      critical may ever be derived from it.
    * **Absent means "never reviewed", not "everything is new".** A patient with
      no row for this user gets NO new-since badge at all. The alternative —
      treating a missing row as an epoch-zero watermark — would light up every
      patient on the board on a clinician's first login and immediately teach
      them to ignore the marker.

    ``last_reviewed_at`` is an ISO-8601 UTC string, written by the application
    rather than by ``CURRENT_TIMESTAMP``, so that it is comparable with the
    ``created_at`` columns of the tables the timeline reads (which are a mix of
    SQLite ``CURRENT_TIMESTAMP`` and application-written ISO strings). See
    services/patient_timeline_service._normalize_ts for the one place that
    reconciles those two spellings.
    """
    conn.execute(
        """CREATE TABLE IF NOT EXISTS patient_review_marks (
            user_id          TEXT NOT NULL,
            patient_id       TEXT NOT NULL,
            last_reviewed_at TEXT NOT NULL,
            updated_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (user_id, patient_id)
        )"""
    )
    # The ward-board badge asks "for THIS user, across every patient in scope,
    # what is my watermark?" — one lookup per board render, not per patient.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_review_marks_user "
        "ON patient_review_marks(user_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_review_marks_patient "
        "ON patient_review_marks(patient_id)"
    )


def _m18_shadow_none_confirmed_once(conn) -> None:
    """Cover ``outcome.none_confirmed`` with the ``idx_shadow_once`` guard.

    THE DEFECT. The per-shift "no deterioration events" confirmation
    (routes/shadow_routes.post_no_outcome) mints a DETERMINISTIC episode id per
    (ward, date, shift) precisely so a resubmission threads onto the same
    episode — but the kind was missing from idx_shadow_once, so every
    resubmission APPENDED a second identical row. The rows cannot be deleted
    afterwards (trg_shadow_no_delete), so each double-submit permanently
    inflated the confirmation rows for that ward-shift.

    SQLite cannot ALTER an index, so the once-guard is dropped and recreated
    with the extended kind list — the same rebuild-in-place idiom migration 3
    uses for a CHECK constraint. ONE index, not a second one beside it: two
    once-guards with different kind lists is the same two-copies drift that let
    the verdict vocabularies destroy every adjudication (see
    routes/shadow_routes.py, Defect 1).

    PRE-EXISTING DUPLICATES would abort the CREATE UNIQUE INDEX — and with it
    the whole init transaction, i.e. the appliance would not boot. They are
    removed first, keeping the EARLIEST row per ward-shift: the duplicates are
    identical statements of one fact (the episode id is a hash of the payload
    that describes the shift), written through a curl-only surface before any
    capture screen existed. Removing them requires briefly dropping
    trg_shadow_no_delete; a boot failure on a ward appliance is strictly worse
    than deleting a redundant copy of a row we keep. Rows referenced by a
    correction are left alone — nothing ever corrects this kind, and if such a
    row exists the CREATE below fails loudly rather than guessing.

    NOTE the deliberately narrow dedupe: only this kind, only unsuperseded
    rows, and a no-op (no trigger drop at all) on every healthy database.
    """
    dup_groups = conn.execute(
        "SELECT COUNT(*) FROM ("
        " SELECT episode_id FROM shadow_events"
        " WHERE kind = 'outcome.none_confirmed' AND supersedes_event_id IS NULL"
        " GROUP BY episode_id, IFNULL(subject_type,''), IFNULL(subject_id,'')"
        " HAVING COUNT(*) > 1)"
    ).fetchone()[0]
    if dup_groups:
        conn.execute("DROP TRIGGER IF EXISTS trg_shadow_no_delete")
        cur = conn.execute(
            "DELETE FROM shadow_events"
            " WHERE kind = 'outcome.none_confirmed' AND supersedes_event_id IS NULL"
            "   AND id NOT IN ("
            "     SELECT MIN(id) FROM shadow_events"
            "     WHERE kind = 'outcome.none_confirmed' AND supersedes_event_id IS NULL"
            "     GROUP BY episode_id, IFNULL(subject_type,''), IFNULL(subject_id,''))"
            "   AND NOT EXISTS (SELECT 1 FROM shadow_events s"
            "                   WHERE s.supersedes_event_id = shadow_events.id)"
        )
        logger.warning(
            "Removed %d duplicate outcome.none_confirmed row(s) across %d "
            "ward-shift(s) before extending idx_shadow_once (the resubmission "
            "bug this migration closes). The earliest confirmation per "
            "ward-shift was kept.", cur.rowcount or 0, dup_groups,
        )
        # Byte-for-byte the trigger migration 4 installs.
        conn.execute(
            """CREATE TRIGGER IF NOT EXISTS trg_shadow_no_delete
               BEFORE DELETE ON shadow_events
               WHEN OLD.lane <> 'simulator'
               BEGIN
                   SELECT RAISE(ABORT, 'shadow_events: non-simulator rows are immutable');
               END"""
        )

    conn.execute("DROP INDEX IF EXISTS idx_shadow_once")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_shadow_once "
        "ON shadow_events(episode_id, kind, IFNULL(subject_type,''), IFNULL(subject_id,'')) "
        "WHERE supersedes_event_id IS NULL AND kind IN "
        "('alert.persisted','action.approved','action.declined','action.abandoned',"
        " 'alert.expired','episode.closed','episode.opened','outcome.none_confirmed')"
    )


def _m19_scribe_unverified_ack_columns(conn) -> None:
    """Give "I signed this AI note anyway" its own columns on ``scribe_notes``.

    THE DEFECT. The sign-anyway acknowledgement — who overrode a FAILED
    hallucination check, and when — was persisted as a key *inside*
    ``verification_json`` (``signer_acknowledged_unverified``). That put a
    clinical attestation in a blob that the verification pipeline rewrites:
    routes/scribe_routes.retry_verification and the background verifier both
    replace ``verification_json`` wholesale, so a retry after signing silently
    erases the record of who accepted the risk. It was also COPIED verbatim onto
    every revision draft by scribe_service.create_revision_from_signed, so a
    fresh unsigned draft carried an acknowledgement nobody had made for it.

    Two dedicated columns fix both: they are never touched by the verification
    writers, and the revision INSERT does not list them, so a new draft starts
    with NULL — an acknowledgement now belongs to exactly one note row and
    cannot be inherited.

    BACKFILL. Existing markers are copied into the columns. Deliberately only
    where the marker's own ``note_id`` matches the row it sits on: a marker on
    any other row is a carried-over copy, and promoting THAT into a column would
    manufacture the very false attestation this migration exists to prevent.
    Markers with no ``note_id`` at all are skipped for the same reason —
    unattributable, so not claimed. Nothing is deleted: the original JSON stays
    in ``verification_json`` as the historical record, and skipped rows remain
    fully readable there.
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info(scribe_notes)").fetchall()}
    if "unverified_ack_by" not in cols:
        conn.execute("ALTER TABLE scribe_notes ADD COLUMN unverified_ack_by TEXT")
    if "unverified_ack_at" not in cols:
        conn.execute("ALTER TABLE scribe_notes ADD COLUMN unverified_ack_at TEXT")

    rows = conn.execute(
        "SELECT id, verification_json FROM scribe_notes "
        "WHERE unverified_ack_by IS NULL "
        "  AND verification_json LIKE '%signer_acknowledged_unverified%'"
    ).fetchall()

    migrated = skipped = 0
    for row in rows:
        note_id, raw = row[0], row[1]
        try:
            marker = json.loads(raw).get("signer_acknowledged_unverified")
        except (json.JSONDecodeError, TypeError, AttributeError):
            marker = None
        if not isinstance(marker, dict) or not marker.get("by"):
            skipped += 1
            continue
        if marker.get("note_id") != note_id:
            skipped += 1
            continue
        conn.execute(
            "UPDATE scribe_notes SET unverified_ack_by = ?, unverified_ack_at = ? WHERE id = ?",
            (marker["by"], marker.get("at"), note_id),
        )
        migrated += 1

    if migrated or skipped:
        logger.info(
            "scribe_notes unverified-sign acknowledgements: %d migrated into "
            "columns, %d left in verification_json only (marker unattributable "
            "or carried over from another note)", migrated, skipped,
        )


def _m20_hospital_admin_spine(conn) -> None:
    """The hospital administration spine: billing, payments, payer/claims,
    procurement and the front-desk cash counter — ONE migration for all of it.

    WHY ONE MIGRATION AND NOT FIVE.
    Five module teams writing five migrations against the same release collide on
    version numbers and, worse, each invents its own idea of what an "amount"
    column is. This migration is the contract they all build against: the tables
    exist before any of the modules do, so the modules add ROWS and ROUTES, never
    schema. A module that discovers it needs a column adds migration 21+; it does
    not edit this one.

    THREE RULES THIS SCHEMA IS BUILT ON
    ───────────────────────────────────

    1. NO FOREIGN KEYS. NOT ONE. `PRAGMA foreign_keys=ON` is set on every Orb
       connection (database.py:998), so an FK declared here would be enforced —
       and every one of these tables points at `patients`, `admissions`, `users`
       or `orders`, which are the CLINICAL tables. An FK child row makes the
       parent undeletable and makes a parent rebuild (the `sage_insights`-style
       RENAME/CREATE/INSERT/DROP that migration 3 had to do) fail or silently
       orphan. A hospital's money must never be able to make a clinical write
       or a clinical schema change fail. The linkage is by plain id column plus
       an index; referential integrity is the services' job, and the cost of a
       dangling id is a report that has to say "unknown patient", which is
       recoverable. The cost of the alternative is not.

    2. MONEY IS INTEGER PAISE, AND THE COLUMN NAME SAYS SO. Every money column
       ends in `_paise` (INTEGER), every quantity in `_milli` (INTEGER
       thousandths of a unit), every rate/percentage in `_bp` (INTEGER basis
       points). There is no REAL column anywhere in this migration and there must
       never be one: a naming convention is what makes a float column visible in
       a diff. All arithmetic goes through services/money.py.

    3. NOT CONFIGURED IS NOT ZERO. Where a price may legitimately be unset, the
       table carries a `*_is_configured` flag beside the amount, because a
       `0` in a rate column and "nobody has entered this yet" are indistinguishable
       and mean opposite things at a billing counter.

    ADVISORY, NOT BLOCKING. `discharge_financial_checks` records what the money
    side thought at discharge. It is a RECORD, not a gate: `enforced` defaults 0
    and `config.DISCHARGE_FINANCIAL_ENFORCE` defaults OFF. A patient's discharge
    must never wait on an unpaid bill during a pilot.

    Additive throughout: 29 new tables, no existing table touched, no data moved.
    """
    for statement in _M20_TABLES:
        conn.execute(statement)
    for statement in _M20_INDEXES:
        conn.execute(statement)

    # Structural seed ONLY — no invented prices. A deployment must be able to
    # raise its first bill without a data-entry session, but every rupee in this
    # system has to have been typed by somebody at the hospital.
    conn.executemany(
        "INSERT OR IGNORE INTO tariff_plans (code, name, plan_type, is_default, is_active) "
        "VALUES (?, ?, ?, ?, 1)",
        [("SELFPAY", "Self-pay (cash)", "self_pay", 1)],
    )
    conn.executemany(
        "INSERT OR IGNORE INTO payers (code, name, payer_type, is_active) VALUES (?, ?, ?, 1)",
        [("SELF", "Self-pay / cash patient", "self_pay")],
    )
    conn.executemany(
        "INSERT OR IGNORE INTO stores (code, name, store_type, is_active) VALUES (?, ?, ?, 1)",
        [("CENTRAL", "Central store", "central")],
    )
    # Document series start at 1. The prefix is a placeholder the site edits; it
    # is NOT a real GST invoice series until a site sets it, which is why
    # `is_configured` is 0 here.
    conn.executemany(
        "INSERT OR IGNORE INTO document_number_series "
        "(series_code, document_type, prefix, next_number, is_configured) VALUES (?, ?, ?, 1, 0)",
        [
            ("INV", "invoice", "INV-"),
            ("RCPT", "receipt", "RCPT-"),
            ("REF", "refund", "REF-"),
            ("CLM", "claim", "CLM-"),
            ("IND", "indent", "IND-"),
            ("PO", "purchase_order", "PO-"),
            ("GRN", "goods_receipt", "GRN-"),
        ],
    )


# ── Migration 20 DDL ─────────────────────────────────────────────────────────
# Held as module constants rather than inline so the table list is readable as a
# list, and so tests can assert against the same source the migration runs.

_M20_TABLES: list[str] = [
    # ── Masters: what the hospital sells, and at what price ───────────────────
    """CREATE TABLE IF NOT EXISTS service_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        code TEXT NOT NULL UNIQUE,
        name TEXT NOT NULL,
        category TEXT NOT NULL DEFAULT 'other' CHECK (category IN (
            'consultation','procedure','investigation','imaging','bed','nursing',
            'ot','pharmacy','consumable','implant','ambulance','package','other')),
        department TEXT,
        uom TEXT NOT NULL DEFAULT 'each',
        -- Healthcare services by a clinical establishment are GST-exempt in
        -- India; pharmacy/consumables/implants are not. The DEFAULT is 'exempt'
        -- deliberately: a new clinical service item that nobody classified must
        -- not silently attract tax.
        gst_treatment TEXT NOT NULL DEFAULT 'exempt' CHECK (gst_treatment IN (
            'exempt','taxable','nil_rated','zero_rated')),
        gst_rate_bp INTEGER NOT NULL DEFAULT 0,
        hsn_sac TEXT,
        default_rate_paise INTEGER NOT NULL DEFAULT 0,
        -- 0 means "no price entered", which is NOT the same as a ₹0 item.
        rate_is_configured INTEGER NOT NULL DEFAULT 0,
        is_active INTEGER NOT NULL DEFAULT 1,
        notes TEXT,
        created_by TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""",

    """CREATE TABLE IF NOT EXISTS tariff_plans (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        code TEXT NOT NULL UNIQUE,
        name TEXT NOT NULL,
        plan_type TEXT NOT NULL DEFAULT 'self_pay' CHECK (plan_type IN (
            'self_pay','tpa','insurer','government_scheme','corporate','internal')),
        currency TEXT NOT NULL DEFAULT 'INR',
        effective_from DATE,
        effective_to DATE,
        is_default INTEGER NOT NULL DEFAULT 0,
        is_active INTEGER NOT NULL DEFAULT 1,
        notes TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""",

    """CREATE TABLE IF NOT EXISTS tariff_rates (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tariff_plan_id INTEGER NOT NULL,
        service_item_id INTEGER NOT NULL,
        service_code TEXT NOT NULL,
        -- Indian hospitals price by bed class. NULL = applies to every class.
        ward_class TEXT,
        rate_paise INTEGER NOT NULL,
        effective_from DATE NOT NULL DEFAULT '1970-01-01',
        effective_to DATE,
        is_active INTEGER NOT NULL DEFAULT 1,
        created_by TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""",

    """CREATE TABLE IF NOT EXISTS package_rates (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tariff_plan_id INTEGER NOT NULL,
        package_code TEXT NOT NULL,
        name TEXT NOT NULL,
        ward_class TEXT,
        package_rate_paise INTEGER NOT NULL,
        los_days_included INTEGER,
        inclusions_json TEXT,
        exclusions_json TEXT,
        -- e.g. the PM-JAY Health Benefit Package code this maps to.
        external_scheme_code TEXT,
        effective_from DATE NOT NULL DEFAULT '1970-01-01',
        effective_to DATE,
        is_active INTEGER NOT NULL DEFAULT 1,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""",

    # ── Payer master ──────────────────────────────────────────────────────────
    """CREATE TABLE IF NOT EXISTS payers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        code TEXT NOT NULL UNIQUE,
        name TEXT NOT NULL,
        payer_type TEXT NOT NULL DEFAULT 'self_pay' CHECK (payer_type IN (
            'self_pay','tpa','insurer','pmjay','state_scheme','cghs','esic',
            'corporate','charity')),
        tariff_plan_id INTEGER,
        gstin TEXT,
        state_code TEXT,
        address TEXT,
        contact_name TEXT,
        contact_phone TEXT,
        contact_email TEXT,
        claim_submission_mode TEXT CHECK (claim_submission_mode IN (
            'portal','email','paper','api')),
        portal_url TEXT,
        credit_days INTEGER,
        preauth_required INTEGER NOT NULL DEFAULT 0,
        is_active INTEGER NOT NULL DEFAULT 1,
        notes TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""",

    """CREATE TABLE IF NOT EXISTS patient_payer_policies (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        patient_id TEXT NOT NULL,
        admission_id TEXT,
        payer_id INTEGER NOT NULL,
        policy_number TEXT,
        member_id TEXT,
        scheme_beneficiary_id TEXT,
        subscriber_name TEXT,
        relationship_to_subscriber TEXT,
        valid_from DATE,
        valid_to DATE,
        sum_insured_paise INTEGER,
        balance_paise INTEGER,
        -- The sum insured is frequently unknown at admission. NULL amount +
        -- this flag at 0 means "not known", never "₹0 of cover".
        sum_insured_is_configured INTEGER NOT NULL DEFAULT 0,
        copay_percent_bp INTEGER NOT NULL DEFAULT 0,
        preauth_required INTEGER NOT NULL DEFAULT 0,
        is_primary INTEGER NOT NULL DEFAULT 1,
        status TEXT NOT NULL DEFAULT 'active' CHECK (status IN (
            'active','exhausted','expired','rejected','cancelled')),
        verified_by TEXT,
        verified_at TIMESTAMP,
        created_by TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""",

    # ── Charge capture ────────────────────────────────────────────────────────
    """CREATE TABLE IF NOT EXISTS encounter_charges (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        patient_id TEXT NOT NULL,
        -- NULL for an outpatient/ER visit that never became an admission.
        admission_id TEXT,
        encounter_type TEXT NOT NULL DEFAULT 'ip' CHECK (encounter_type IN (
            'ip','op','er','daycare')),
        service_item_id INTEGER,
        service_code TEXT NOT NULL,
        description TEXT NOT NULL,
        quantity_milli INTEGER NOT NULL DEFAULT 1000,
        unit_rate_paise INTEGER NOT NULL DEFAULT 0,
        rate_is_configured INTEGER NOT NULL DEFAULT 0,
        tariff_plan_id INTEGER,
        ward_class TEXT,
        gross_paise INTEGER NOT NULL DEFAULT 0,
        discount_paise INTEGER NOT NULL DEFAULT 0,
        gst_treatment TEXT NOT NULL DEFAULT 'exempt' CHECK (gst_treatment IN (
            'exempt','taxable','nil_rated','zero_rated')),
        gst_rate_bp INTEGER NOT NULL DEFAULT 0,
        taxable_value_paise INTEGER NOT NULL DEFAULT 0,
        cgst_paise INTEGER NOT NULL DEFAULT 0,
        sgst_paise INTEGER NOT NULL DEFAULT 0,
        igst_paise INTEGER NOT NULL DEFAULT 0,
        total_paise INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN (
            'pending','billed','cancelled','written_off','packaged')),
        invoice_id INTEGER,
        -- Where the charge came from. 'order'/'emar'/'pharmacy' rows are posted
        -- BY a clinical event; they never post back INTO one.
        source TEXT NOT NULL DEFAULT 'manual' CHECK (source IN (
            'manual','order','emar','pharmacy','bed_day','package','ot','import')),
        source_ref TEXT,
        service_date TIMESTAMP,
        charged_by TEXT,
        charged_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        cancelled_by TEXT,
        cancelled_at TIMESTAMP,
        cancel_reason TEXT,
        -- A charge auto-posted from a clinical event is replayed whenever that
        -- event is replayed (offline queue, retry). Without this key the same
        -- dose bills twice.
        idempotency_key TEXT,
        notes TEXT
    )""",

    # ── Invoices ──────────────────────────────────────────────────────────────
    """CREATE TABLE IF NOT EXISTS invoices (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        -- NULL while draft: a draft must not consume a GST invoice number.
        invoice_number TEXT,
        patient_id TEXT NOT NULL,
        admission_id TEXT,
        encounter_type TEXT NOT NULL DEFAULT 'ip' CHECK (encounter_type IN (
            'ip','op','er','daycare')),
        payer_id INTEGER,
        tariff_plan_id INTEGER,
        invoice_type TEXT NOT NULL DEFAULT 'final' CHECK (invoice_type IN (
            'interim','final','advance','credit_note','debit_note')),
        status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN (
            'draft','issued','partially_paid','paid','cancelled','void','written_off')),
        place_of_supply TEXT,
        is_interstate INTEGER NOT NULL DEFAULT 0,
        subtotal_paise INTEGER NOT NULL DEFAULT 0,
        discount_paise INTEGER NOT NULL DEFAULT 0,
        taxable_value_paise INTEGER NOT NULL DEFAULT 0,
        cgst_paise INTEGER NOT NULL DEFAULT 0,
        sgst_paise INTEGER NOT NULL DEFAULT 0,
        igst_paise INTEGER NOT NULL DEFAULT 0,
        tax_paise INTEGER NOT NULL DEFAULT 0,
        total_paise INTEGER NOT NULL DEFAULT 0,
        -- The "Round Off" line every Indian invoice prints, recorded explicitly
        -- rather than smeared into a tax figure.
        rounding_adjustment_paise INTEGER NOT NULL DEFAULT 0,
        payable_paise INTEGER NOT NULL DEFAULT 0,
        amount_paid_paise INTEGER NOT NULL DEFAULT 0,
        balance_paise INTEGER NOT NULL DEFAULT 0,
        patient_responsibility_paise INTEGER NOT NULL DEFAULT 0,
        payer_responsibility_paise INTEGER NOT NULL DEFAULT 0,
        -- A credit note points at the invoice it reverses.
        supersedes_invoice_id INTEGER,
        period_from TIMESTAMP,
        period_to TIMESTAMP,
        issued_at TIMESTAMP,
        issued_by TEXT,
        cancelled_at TIMESTAMP,
        cancelled_by TEXT,
        cancel_reason TEXT,
        idempotency_key TEXT,
        notes TEXT,
        created_by TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""",

    """CREATE TABLE IF NOT EXISTS invoice_lines (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        invoice_id INTEGER NOT NULL,
        line_no INTEGER NOT NULL,
        encounter_charge_id INTEGER,
        service_item_id INTEGER,
        service_code TEXT NOT NULL,
        description TEXT NOT NULL,
        hsn_sac TEXT,
        quantity_milli INTEGER NOT NULL DEFAULT 1000,
        uom TEXT,
        unit_rate_paise INTEGER NOT NULL DEFAULT 0,
        gross_paise INTEGER NOT NULL DEFAULT 0,
        discount_paise INTEGER NOT NULL DEFAULT 0,
        gst_treatment TEXT NOT NULL DEFAULT 'exempt' CHECK (gst_treatment IN (
            'exempt','taxable','nil_rated','zero_rated')),
        gst_rate_bp INTEGER NOT NULL DEFAULT 0,
        taxable_value_paise INTEGER NOT NULL DEFAULT 0,
        cgst_paise INTEGER NOT NULL DEFAULT 0,
        sgst_paise INTEGER NOT NULL DEFAULT 0,
        igst_paise INTEGER NOT NULL DEFAULT 0,
        tax_paise INTEGER NOT NULL DEFAULT 0,
        total_paise INTEGER NOT NULL DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""",

    """CREATE TABLE IF NOT EXISTS document_number_series (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        series_code TEXT NOT NULL UNIQUE,
        document_type TEXT NOT NULL,
        prefix TEXT NOT NULL DEFAULT '',
        suffix TEXT NOT NULL DEFAULT '',
        financial_year TEXT,
        pad_width INTEGER NOT NULL DEFAULT 6,
        next_number INTEGER NOT NULL DEFAULT 1,
        -- 0 until a site sets its real series. A number issued from an
        -- unconfigured series is a placeholder, and the UI must say so.
        is_configured INTEGER NOT NULL DEFAULT 0,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""",

    # ── Payments, deposits, refunds, discounts ────────────────────────────────
    """CREATE TABLE IF NOT EXISTS payments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        receipt_number TEXT,
        patient_id TEXT NOT NULL,
        admission_id TEXT,
        invoice_id INTEGER,
        payer_id INTEGER,
        -- 'in' = money received, 'out' = money returned. The AMOUNT is always
        -- positive; the direction carries the sign. A signed amount plus a
        -- direction is two sources of truth about one thing.
        direction TEXT NOT NULL DEFAULT 'in' CHECK (direction IN ('in','out')),
        amount_paise INTEGER NOT NULL,
        method TEXT NOT NULL DEFAULT 'cash' CHECK (method IN (
            'cash','card','upi','netbanking','cheque','dd','neft','rtgs',
            'wallet','insurance','scheme','adjustment')),
        reference_no TEXT,
        bank_name TEXT,
        instrument_date DATE,
        purpose TEXT NOT NULL DEFAULT 'invoice_payment' CHECK (purpose IN (
            'advance','deposit','invoice_payment','refund','settlement','write_off')),
        status TEXT NOT NULL DEFAULT 'received' CHECK (status IN (
            'received','cleared','bounced','cancelled','reversed')),
        counter_session_id INTEGER,
        refund_id INTEGER,
        reversal_of_payment_id INTEGER,
        received_by TEXT,
        received_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        cancelled_by TEXT,
        cancelled_at TIMESTAMP,
        cancel_reason TEXT,
        idempotency_key TEXT,
        notes TEXT
    )""",

    """CREATE TABLE IF NOT EXISTS payment_allocations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        payment_id INTEGER NOT NULL,
        invoice_id INTEGER NOT NULL,
        amount_paise INTEGER NOT NULL,
        allocated_by TEXT,
        allocated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        reversed_at TIMESTAMP,
        reversed_by TEXT,
        notes TEXT
    )""",

    """CREATE TABLE IF NOT EXISTS patient_deposits (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        patient_id TEXT NOT NULL,
        admission_id TEXT,
        movement TEXT NOT NULL CHECK (movement IN (
            'deposit','apply','refund','forfeit','transfer')),
        -- SIGNED: positive increases the patient's advance balance, negative
        -- reduces it. The running balance is SUM(amount_paise) for the patient,
        -- which is why this one column is signed where `payments.amount_paise`
        -- is not — a ledger's whole job is to be summable.
        amount_paise INTEGER NOT NULL,
        payment_id INTEGER,
        invoice_id INTEGER,
        created_by TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        notes TEXT
    )""",

    """CREATE TABLE IF NOT EXISTS refunds (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        refund_number TEXT,
        patient_id TEXT NOT NULL,
        admission_id TEXT,
        invoice_id INTEGER,
        payment_id INTEGER,
        amount_paise INTEGER NOT NULL,
        reason_code TEXT,
        reason TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'requested' CHECK (status IN (
            'requested','approved','rejected','paid','cancelled')),
        method TEXT CHECK (method IN (
            'cash','card','upi','netbanking','cheque','dd','neft','rtgs','adjustment')),
        reference_no TEXT,
        requested_by TEXT,
        requested_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        decided_by TEXT,
        decided_at TIMESTAMP,
        decision_note TEXT,
        paid_by TEXT,
        paid_at TIMESTAMP,
        idempotency_key TEXT
    )""",

    """CREATE TABLE IF NOT EXISTS discount_approvals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        patient_id TEXT NOT NULL,
        admission_id TEXT,
        invoice_id INTEGER,
        encounter_charge_id INTEGER,
        scope TEXT NOT NULL DEFAULT 'invoice' CHECK (scope IN ('invoice','charge','patient')),
        discount_type TEXT NOT NULL DEFAULT 'amount' CHECK (discount_type IN ('amount','percent')),
        percent_bp INTEGER NOT NULL DEFAULT 0,
        requested_paise INTEGER NOT NULL DEFAULT 0,
        approved_paise INTEGER,
        policy_limit_paise INTEGER,
        exceeded_limit INTEGER NOT NULL DEFAULT 0,
        reason_code TEXT,
        reason TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'requested' CHECK (status IN (
            'requested','approved','rejected','cancelled')),
        requested_by TEXT,
        requested_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        decided_by TEXT,
        decided_at TIMESTAMP,
        decision_note TEXT
    )""",

    # ── Claims ────────────────────────────────────────────────────────────────
    """CREATE TABLE IF NOT EXISTS claims (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        claim_number TEXT,
        patient_id TEXT NOT NULL,
        admission_id TEXT,
        invoice_id INTEGER,
        payer_id INTEGER NOT NULL,
        policy_id INTEGER,
        claim_type TEXT NOT NULL DEFAULT 'cashless' CHECK (claim_type IN (
            'cashless','reimbursement','scheme')),
        package_code TEXT,
        preauth_number TEXT,
        preauth_requested_paise INTEGER,
        preauth_approved_paise INTEGER,
        preauth_status TEXT CHECK (preauth_status IN (
            'not_required','requested','approved','partially_approved','denied','expired')),
        claimed_paise INTEGER NOT NULL DEFAULT 0,
        approved_paise INTEGER,
        disallowed_paise INTEGER,
        tds_paise INTEGER,
        settled_paise INTEGER,
        patient_liability_paise INTEGER,
        status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN (
            'draft','preauth_requested','preauth_approved','preauth_denied',
            'submitted','queried','approved','partially_approved','denied',
            'settled','closed','cancelled','appealed')),
        external_ref TEXT,
        submitted_at TIMESTAMP,
        submitted_by TEXT,
        decided_at TIMESTAMP,
        settled_at TIMESTAMP,
        created_by TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        notes TEXT
    )""",

    """CREATE TABLE IF NOT EXISTS claim_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        claim_id INTEGER NOT NULL,
        event_type TEXT NOT NULL CHECK (event_type IN (
            'created','preauth_requested','preauth_decided','submitted',
            'query_raised','query_answered','approved','partially_approved',
            'denied','settled','appealed','closed','cancelled','note')),
        from_status TEXT,
        to_status TEXT,
        amount_paise INTEGER,
        denial_code TEXT,
        denial_reason TEXT,
        remark TEXT,
        document_ref TEXT,
        actor_id TEXT,
        occurred_at TIMESTAMP,
        recorded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""",

    # ── Procurement (NON-pharmacy stores; Helix owns drug stock) ──────────────
    """CREATE TABLE IF NOT EXISTS vendors (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        code TEXT NOT NULL UNIQUE,
        name TEXT NOT NULL,
        vendor_type TEXT NOT NULL DEFAULT 'supplier' CHECK (vendor_type IN (
            'supplier','service','amc','contractor','distributor')),
        gstin TEXT,
        pan TEXT,
        msme_number TEXT,
        state_code TEXT,
        address TEXT,
        contact_name TEXT,
        contact_phone TEXT,
        contact_email TEXT,
        payment_terms_days INTEGER,
        bank_name TEXT,
        -- Deliberately NOT the full account number: this appliance holds PHI and
        -- has no vendor-payment rail. Enough to reconcile a transfer somebody
        -- else made, not enough to be a payments target.
        account_last4 TEXT,
        ifsc TEXT,
        rating INTEGER,
        is_blacklisted INTEGER NOT NULL DEFAULT 0,
        blacklist_reason TEXT,
        is_active INTEGER NOT NULL DEFAULT 1,
        notes TEXT,
        created_by TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""",

    """CREATE TABLE IF NOT EXISTS stores (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        code TEXT NOT NULL UNIQUE,
        name TEXT NOT NULL,
        store_type TEXT NOT NULL DEFAULT 'central' CHECK (store_type IN (
            'central','sub_store','ot','ward','housekeeping','biomedical','linen')),
        department TEXT,
        ward TEXT,
        incharge_user_id TEXT,
        is_active INTEGER NOT NULL DEFAULT 1,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""",

    """CREATE TABLE IF NOT EXISTS inventory_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        code TEXT NOT NULL UNIQUE,
        name TEXT NOT NULL,
        category TEXT NOT NULL DEFAULT 'consumable' CHECK (category IN (
            'consumable','implant','equipment','instrument','stationery',
            'housekeeping','linen','reagent','ppe','other')),
        uom TEXT NOT NULL DEFAULT 'each',
        pack_size INTEGER NOT NULL DEFAULT 1,
        hsn_sac TEXT,
        gst_treatment TEXT NOT NULL DEFAULT 'taxable' CHECK (gst_treatment IN (
            'exempt','taxable','nil_rated','zero_rated')),
        gst_rate_bp INTEGER NOT NULL DEFAULT 0,
        is_chargeable INTEGER NOT NULL DEFAULT 0,
        service_item_id INTEGER,
        reorder_level_qty INTEGER,
        reorder_qty INTEGER,
        lead_time_days INTEGER,
        last_purchase_rate_paise INTEGER,
        rate_is_configured INTEGER NOT NULL DEFAULT 0,
        default_store_id INTEGER,
        -- HARD BOUNDARY, enforced by the database rather than by convention:
        -- drug stock belongs to Helix (medications/pharmacy), which has batch,
        -- expiry, schedule-H and dispensing rules this module does not model.
        -- A second, dumber stock ledger for the same physical drug is how two
        -- systems disagree about how much of a controlled substance exists.
        is_pharmacy INTEGER NOT NULL DEFAULT 0 CHECK (is_pharmacy = 0),
        is_active INTEGER NOT NULL DEFAULT 1,
        notes TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""",

    """CREATE TABLE IF NOT EXISTS indents (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        indent_number TEXT,
        store_id INTEGER,
        department TEXT,
        ward TEXT,
        priority TEXT NOT NULL DEFAULT 'routine' CHECK (priority IN (
            'routine','urgent','emergency')),
        status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN (
            'draft','submitted','approved','partially_approved','rejected',
            'converted','cancelled','closed')),
        required_by DATE,
        requested_by TEXT,
        requested_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        approved_by TEXT,
        approved_at TIMESTAMP,
        rejected_reason TEXT,
        remarks TEXT
    )""",

    """CREATE TABLE IF NOT EXISTS indent_lines (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        indent_id INTEGER NOT NULL,
        line_no INTEGER NOT NULL,
        inventory_item_id INTEGER,
        item_code TEXT NOT NULL,
        description TEXT NOT NULL,
        uom TEXT,
        quantity_milli INTEGER NOT NULL,
        approved_quantity_milli INTEGER,
        status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN (
            'pending','approved','rejected','ordered','received','cancelled')),
        remarks TEXT
    )""",

    """CREATE TABLE IF NOT EXISTS purchase_orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        po_number TEXT,
        vendor_id INTEGER NOT NULL,
        indent_id INTEGER,
        store_id INTEGER,
        status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN (
            'draft','pending_approval','approved','issued','partially_received',
            'received','closed','cancelled')),
        order_date DATE,
        expected_date DATE,
        place_of_supply TEXT,
        is_interstate INTEGER NOT NULL DEFAULT 0,
        subtotal_paise INTEGER NOT NULL DEFAULT 0,
        discount_paise INTEGER NOT NULL DEFAULT 0,
        taxable_value_paise INTEGER NOT NULL DEFAULT 0,
        cgst_paise INTEGER NOT NULL DEFAULT 0,
        sgst_paise INTEGER NOT NULL DEFAULT 0,
        igst_paise INTEGER NOT NULL DEFAULT 0,
        tax_paise INTEGER NOT NULL DEFAULT 0,
        other_charges_paise INTEGER NOT NULL DEFAULT 0,
        rounding_adjustment_paise INTEGER NOT NULL DEFAULT 0,
        total_paise INTEGER NOT NULL DEFAULT 0,
        terms TEXT,
        created_by TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        approved_by TEXT,
        approved_at TIMESTAMP,
        issued_at TIMESTAMP,
        cancelled_by TEXT,
        cancelled_at TIMESTAMP,
        cancel_reason TEXT,
        remarks TEXT
    )""",

    """CREATE TABLE IF NOT EXISTS purchase_order_lines (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        purchase_order_id INTEGER NOT NULL,
        line_no INTEGER NOT NULL,
        indent_line_id INTEGER,
        inventory_item_id INTEGER,
        item_code TEXT NOT NULL,
        description TEXT NOT NULL,
        hsn_sac TEXT,
        uom TEXT,
        quantity_milli INTEGER NOT NULL,
        received_quantity_milli INTEGER NOT NULL DEFAULT 0,
        unit_rate_paise INTEGER NOT NULL DEFAULT 0,
        gross_paise INTEGER NOT NULL DEFAULT 0,
        discount_paise INTEGER NOT NULL DEFAULT 0,
        gst_treatment TEXT NOT NULL DEFAULT 'taxable' CHECK (gst_treatment IN (
            'exempt','taxable','nil_rated','zero_rated')),
        gst_rate_bp INTEGER NOT NULL DEFAULT 0,
        taxable_value_paise INTEGER NOT NULL DEFAULT 0,
        cgst_paise INTEGER NOT NULL DEFAULT 0,
        sgst_paise INTEGER NOT NULL DEFAULT 0,
        igst_paise INTEGER NOT NULL DEFAULT 0,
        tax_paise INTEGER NOT NULL DEFAULT 0,
        total_paise INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'open' CHECK (status IN (
            'open','partially_received','received','cancelled','short_closed')),
        remarks TEXT
    )""",

    """CREATE TABLE IF NOT EXISTS goods_receipts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        grn_number TEXT,
        purchase_order_id INTEGER,
        vendor_id INTEGER,
        store_id INTEGER,
        vendor_invoice_number TEXT,
        vendor_invoice_date DATE,
        vendor_invoice_total_paise INTEGER,
        status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN (
            'draft','posted','cancelled','returned')),
        qc_status TEXT NOT NULL DEFAULT 'pending' CHECK (qc_status IN (
            'pending','passed','failed','partial')),
        subtotal_paise INTEGER NOT NULL DEFAULT 0,
        taxable_value_paise INTEGER NOT NULL DEFAULT 0,
        cgst_paise INTEGER NOT NULL DEFAULT 0,
        sgst_paise INTEGER NOT NULL DEFAULT 0,
        igst_paise INTEGER NOT NULL DEFAULT 0,
        tax_paise INTEGER NOT NULL DEFAULT 0,
        total_paise INTEGER NOT NULL DEFAULT 0,
        received_by TEXT,
        received_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        posted_by TEXT,
        posted_at TIMESTAMP,
        cancelled_by TEXT,
        cancelled_at TIMESTAMP,
        cancel_reason TEXT,
        idempotency_key TEXT,
        remarks TEXT
    )""",

    """CREATE TABLE IF NOT EXISTS goods_receipt_lines (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        goods_receipt_id INTEGER NOT NULL,
        line_no INTEGER NOT NULL,
        purchase_order_line_id INTEGER,
        inventory_item_id INTEGER,
        item_code TEXT NOT NULL,
        description TEXT NOT NULL,
        batch_no TEXT,
        expiry_date DATE,
        uom TEXT,
        quantity_milli INTEGER NOT NULL,
        accepted_quantity_milli INTEGER NOT NULL DEFAULT 0,
        rejected_quantity_milli INTEGER NOT NULL DEFAULT 0,
        rejection_reason TEXT,
        unit_rate_paise INTEGER NOT NULL DEFAULT 0,
        gross_paise INTEGER NOT NULL DEFAULT 0,
        discount_paise INTEGER NOT NULL DEFAULT 0,
        gst_treatment TEXT NOT NULL DEFAULT 'taxable' CHECK (gst_treatment IN (
            'exempt','taxable','nil_rated','zero_rated')),
        gst_rate_bp INTEGER NOT NULL DEFAULT 0,
        taxable_value_paise INTEGER NOT NULL DEFAULT 0,
        cgst_paise INTEGER NOT NULL DEFAULT 0,
        sgst_paise INTEGER NOT NULL DEFAULT 0,
        igst_paise INTEGER NOT NULL DEFAULT 0,
        tax_paise INTEGER NOT NULL DEFAULT 0,
        total_paise INTEGER NOT NULL DEFAULT 0,
        remarks TEXT
    )""",

    """CREATE TABLE IF NOT EXISTS stock_movements (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        inventory_item_id INTEGER,
        item_code TEXT NOT NULL,
        store_id INTEGER,
        batch_no TEXT,
        expiry_date DATE,
        movement_type TEXT NOT NULL CHECK (movement_type IN (
            'opening','receipt','issue','return','transfer_in','transfer_out',
            'adjustment','consumption','write_off','expiry')),
        -- SIGNED, in thousandths of a unit: positive moves stock INTO the store,
        -- negative moves it OUT. One column, one sign convention, so a store
        -- balance is SUM(quantity_milli) and can never disagree with itself.
        quantity_milli INTEGER NOT NULL,
        balance_after_milli INTEGER,
        unit_rate_paise INTEGER,
        value_paise INTEGER,
        reference_type TEXT CHECK (reference_type IN (
            'grn','indent','purchase_order','manual','patient_charge','transfer','audit')),
        reference_id TEXT,
        -- Present when a consumable was issued to a named patient so it can be
        -- charged. This is a COSTING link, not a clinical record.
        patient_id TEXT,
        moved_by TEXT,
        moved_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        idempotency_key TEXT,
        remarks TEXT
    )""",

    # ── Front desk ────────────────────────────────────────────────────────────
    """CREATE TABLE IF NOT EXISTS cash_counter_sessions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        counter_code TEXT NOT NULL,
        shift_label TEXT,
        opened_by TEXT,
        opened_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        opening_float_paise INTEGER NOT NULL DEFAULT 0,
        closed_by TEXT,
        closed_at TIMESTAMP,
        -- What the cashier counted vs what the system says was taken. The
        -- variance is stored, never silently reconciled away.
        declared_cash_paise INTEGER,
        system_cash_paise INTEGER,
        variance_paise INTEGER,
        status TEXT NOT NULL DEFAULT 'open' CHECK (status IN (
            'open','closed','reconciled','disputed')),
        reconciled_by TEXT,
        reconciled_at TIMESTAMP,
        notes TEXT
    )""",

    """CREATE TABLE IF NOT EXISTS discharge_financial_checks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        patient_id TEXT NOT NULL,
        admission_id TEXT,
        checked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        checked_by TEXT,
        outstanding_paise INTEGER,
        deposit_balance_paise INTEGER,
        unbilled_charge_count INTEGER,
        open_claim_count INTEGER,
        -- 'unknown' is a real answer and must be renderable as one: a site with
        -- no tariffs loaded cannot compute an outstanding, and reporting ₹0
        -- there would tell a clerk the bill is settled.
        advisory_status TEXT NOT NULL DEFAULT 'unknown' CHECK (advisory_status IN (
            'clear','outstanding','unknown')),
        -- ADVISORY BY DEFAULT AND BY DESIGN. 0 means this check only warned.
        -- config.DISCHARGE_FINANCIAL_ENFORCE defaults OFF and must stay OFF for
        -- the pilot: a patient's discharge must never wait on an unpaid bill.
        enforced INTEGER NOT NULL DEFAULT 0,
        blocked_discharge INTEGER NOT NULL DEFAULT 0,
        overridden_by TEXT,
        override_reason TEXT,
        discharge_proceeded INTEGER NOT NULL DEFAULT 1,
        notes TEXT
    )""",
]


_M20_INDEXES: list[str] = [
    # Masters — lookups are by code and by "what is active".
    "CREATE INDEX IF NOT EXISTS idx_service_items_active ON service_items(is_active, category, name)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_tariff_rates_unique ON tariff_rates("
    "tariff_plan_id, service_code, IFNULL(ward_class,''), effective_from)",
    "CREATE INDEX IF NOT EXISTS idx_tariff_rates_lookup ON tariff_rates("
    "service_code, tariff_plan_id, is_active, effective_from DESC)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_package_rates_unique ON package_rates("
    "tariff_plan_id, package_code, IFNULL(ward_class,''), effective_from)",
    "CREATE INDEX IF NOT EXISTS idx_payers_active ON payers(is_active, payer_type, name)",
    "CREATE INDEX IF NOT EXISTS idx_policies_patient ON patient_payer_policies("
    "patient_id, status, is_primary DESC, id DESC)",
    "CREATE INDEX IF NOT EXISTS idx_policies_admission ON patient_payer_policies(admission_id)",

    # Charge capture — the hot read is "everything unbilled for this admission".
    "CREATE INDEX IF NOT EXISTS idx_charges_admission ON encounter_charges("
    "admission_id, status, id DESC)",
    "CREATE INDEX IF NOT EXISTS idx_charges_patient ON encounter_charges("
    "patient_id, status, service_date DESC)",
    "CREATE INDEX IF NOT EXISTS idx_charges_invoice ON encounter_charges(invoice_id)",
    "CREATE INDEX IF NOT EXISTS idx_charges_source ON encounter_charges(source, source_ref)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_charges_idempotency ON encounter_charges("
    "idempotency_key) WHERE idempotency_key IS NOT NULL",

    # Invoices — a draft holds no number, so the uniqueness is partial.
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_invoices_number ON invoices(invoice_number) "
    "WHERE invoice_number IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS idx_invoices_patient ON invoices(patient_id, status, id DESC)",
    "CREATE INDEX IF NOT EXISTS idx_invoices_admission ON invoices(admission_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_invoices_payer ON invoices(payer_id, status, issued_at DESC)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_invoices_idempotency ON invoices(idempotency_key) "
    "WHERE idempotency_key IS NOT NULL",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_invoice_lines_no ON invoice_lines(invoice_id, line_no)",
    "CREATE INDEX IF NOT EXISTS idx_invoice_lines_charge ON invoice_lines(encounter_charge_id)",

    # Payments.
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_payments_receipt ON payments(receipt_number) "
    "WHERE receipt_number IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS idx_payments_patient ON payments(patient_id, status, received_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_payments_invoice ON payments(invoice_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_payments_counter ON payments(counter_session_id, status)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_payments_idempotency ON payments(idempotency_key) "
    "WHERE idempotency_key IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS idx_allocations_invoice ON payment_allocations(invoice_id)",
    "CREATE INDEX IF NOT EXISTS idx_allocations_payment ON payment_allocations(payment_id)",
    "CREATE INDEX IF NOT EXISTS idx_deposits_patient ON patient_deposits(patient_id, id DESC)",
    "CREATE INDEX IF NOT EXISTS idx_deposits_admission ON patient_deposits(admission_id)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_refunds_number ON refunds(refund_number) "
    "WHERE refund_number IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS idx_refunds_patient ON refunds(patient_id, status, id DESC)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_refunds_idempotency ON refunds(idempotency_key) "
    "WHERE idempotency_key IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS idx_discounts_status ON discount_approvals(status, requested_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_discounts_invoice ON discount_approvals(invoice_id)",
    "CREATE INDEX IF NOT EXISTS idx_discounts_patient ON discount_approvals(patient_id, id DESC)",

    # Claims.
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_claims_number ON claims(claim_number) "
    "WHERE claim_number IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS idx_claims_patient ON claims(patient_id, status, id DESC)",
    "CREATE INDEX IF NOT EXISTS idx_claims_payer ON claims(payer_id, status, submitted_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_claims_admission ON claims(admission_id)",
    "CREATE INDEX IF NOT EXISTS idx_claim_events_claim ON claim_events(claim_id, id)",

    # Procurement.
    "CREATE INDEX IF NOT EXISTS idx_vendors_active ON vendors(is_active, is_blacklisted, name)",
    "CREATE INDEX IF NOT EXISTS idx_inventory_active ON inventory_items(is_active, category, name)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_indents_number ON indents(indent_number) "
    "WHERE indent_number IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS idx_indents_status ON indents(status, requested_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_indents_store ON indents(store_id, status)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_indent_lines_no ON indent_lines(indent_id, line_no)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_po_number ON purchase_orders(po_number) "
    "WHERE po_number IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS idx_po_vendor ON purchase_orders(vendor_id, status, order_date DESC)",
    "CREATE INDEX IF NOT EXISTS idx_po_status ON purchase_orders(status, order_date DESC)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_po_lines_no ON purchase_order_lines(purchase_order_id, line_no)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_grn_number ON goods_receipts(grn_number) "
    "WHERE grn_number IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS idx_grn_po ON goods_receipts(purchase_order_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_grn_vendor ON goods_receipts(vendor_id, received_at DESC)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_grn_idempotency ON goods_receipts(idempotency_key) "
    "WHERE idempotency_key IS NOT NULL",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_grn_lines_no ON goods_receipt_lines(goods_receipt_id, line_no)",
    "CREATE INDEX IF NOT EXISTS idx_stock_item_store ON stock_movements("
    "inventory_item_id, store_id, id DESC)",
    "CREATE INDEX IF NOT EXISTS idx_stock_reference ON stock_movements(reference_type, reference_id)",
    "CREATE INDEX IF NOT EXISTS idx_stock_patient ON stock_movements(patient_id, moved_at DESC)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_stock_idempotency ON stock_movements(idempotency_key) "
    "WHERE idempotency_key IS NOT NULL",

    # Front desk.
    "CREATE INDEX IF NOT EXISTS idx_counter_sessions_open ON cash_counter_sessions("
    "status, counter_code, opened_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_discharge_checks_admission ON discharge_financial_checks("
    "admission_id, id DESC)",
    "CREATE INDEX IF NOT EXISTS idx_discharge_checks_patient ON discharge_financial_checks("
    "patient_id, checked_at DESC)",
]


# ── Migration 21: biomedical asset & equipment tracking ──────────────────────

_M21_TABLES = [
    # ── The register ──────────────────────────────────────────────────────────
    #
    # An accreditation assessor (NABH chapter FMS) opens one book: "show me every
    # piece of equipment, where it is, who owns it, and when it was last
    # calibrated." That book is this table joined to biomedical_schedules.
    #
    # `ward` is FREE TEXT, matching `patients.ward` and `admissions.ward`. Orb has
    # no ward master (they are strings everywhere else), and inventing one here
    # would create a second vocabulary for the same word — the failure this
    # codebase already paid for once with two stock ledgers. When a ward master
    # arrives, this column migrates with all the others.
    #
    # `produces_clinical_readings` is recorded and DELIBERATELY UNUSED tonight.
    # It marks the assets whose output Orb ingests (monitors, pumps, ventilators)
    # so that the product question — "should an out-of-calibration device flag its
    # readings?" — can be answered later against real data rather than a guess.
    # Nothing in the vitals pipeline reads it. See the module docstring in
    # services/biomedical_service.py.
    """CREATE TABLE IF NOT EXISTS biomedical_assets (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        asset_tag TEXT NOT NULL,
        name TEXT NOT NULL,
        category TEXT NOT NULL DEFAULT 'other',
        manufacturer TEXT,
        model TEXT,
        serial_number TEXT,
        department TEXT,
        ward TEXT,
        location_detail TEXT,
        criticality TEXT NOT NULL DEFAULT 'routine' CHECK (criticality IN (
            'life_support','critical','routine')),
        status TEXT NOT NULL DEFAULT 'in_service' CHECK (status IN (
            'in_service','standby','under_maintenance','down','condemned')),
        purchase_date DATE,
        commissioned_date DATE,
        purchase_cost_paise INTEGER,
        cost_is_configured INTEGER NOT NULL DEFAULT 0,
        warranty_expiry_date DATE,
        vendor_id INTEGER,
        produces_clinical_readings INTEGER NOT NULL DEFAULT 0,
        notes TEXT,
        is_active INTEGER NOT NULL DEFAULT 1,
        created_by TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_by TEXT,
        updated_at TIMESTAMP
    )""",

    # ── Preventive maintenance / calibration schedules ────────────────────────
    #
    # ONE ROW PER (asset, schedule_type). A ventilator has a PM schedule AND a
    # calibration schedule, and they run on different intervals with different
    # consequences: a missed PM is a reliability problem, a missed CALIBRATION is
    # a measurement problem — the device keeps working and keeps lying.
    #
    # `next_due_date` is STORED, not derived on read, because a schedule may be
    # seeded with a first due date that has no `last_done_date` behind it (an
    # asset commissioned before Orb existed, or one never calibrated at all).
    # A derived-only column would have to invent a last-done date to have
    # anything to derive from, and an invented compliance date is the exact lie
    # this register exists to prevent. `last_done_date IS NULL` therefore MEANS
    # "never done", and reads as that on the screen.
    """CREATE TABLE IF NOT EXISTS biomedical_schedules (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        asset_id INTEGER NOT NULL,
        schedule_type TEXT NOT NULL CHECK (schedule_type IN (
            'preventive_maintenance','calibration','electrical_safety')),
        interval_days INTEGER NOT NULL CHECK (interval_days > 0),
        last_done_date DATE,
        next_due_date DATE NOT NULL,
        grace_days INTEGER NOT NULL DEFAULT 0 CHECK (grace_days >= 0),
        lead_days INTEGER NOT NULL DEFAULT 30 CHECK (lead_days >= 0),
        responsible TEXT,
        vendor_id INTEGER,
        last_certificate_reference TEXT,
        is_active INTEGER NOT NULL DEFAULT 1,
        created_by TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP
    )""",

    # Append-only history of every PM/calibration actually performed. This is the
    # evidence half of the register: the schedule says when it is next due, this
    # says what happened and against which certificate. `was_overdue` /
    # `days_overdue` are stamped AT THE TIME OF THE WORK, because once
    # next_due_date rolls forward the lateness is otherwise unrecoverable — and
    # "how often does this hospital calibrate late" is the question an assessor
    # and a champion both actually ask.
    """CREATE TABLE IF NOT EXISTS biomedical_schedule_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        schedule_id INTEGER NOT NULL,
        asset_id INTEGER NOT NULL,
        schedule_type TEXT NOT NULL,
        performed_date DATE NOT NULL,
        previous_due_date DATE,
        next_due_date DATE NOT NULL,
        was_overdue INTEGER NOT NULL DEFAULT 0,
        days_overdue INTEGER NOT NULL DEFAULT 0,
        result TEXT NOT NULL DEFAULT 'pass' CHECK (result IN (
            'pass','pass_with_adjustment','fail')),
        certificate_reference TEXT,
        performed_by TEXT,
        vendor_id INTEGER,
        cost_paise INTEGER,
        notes TEXT,
        recorded_by TEXT,
        recorded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""",

    # ── Breakdown / service calls, with downtime ──────────────────────────────
    #
    # `down_from` is SEPARATE from `reported_at` on purpose. A pump fails at
    # 02:10 and the call is logged at 07:00 when the day shift arrives; charging
    # the hospital's uptime figure only from 07:00 flatters it by five hours.
    # `down_from` defaults to `reported_at` when nobody knows better, and the
    # service layer says which it used.
    #
    # `downtime_minutes` is written ONLY when the call closes. While a call is
    # open, downtime is still accruing, so a stored number would be stale the
    # second after it was written; the running figure is computed on read.
    """CREATE TABLE IF NOT EXISTS biomedical_service_calls (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        call_number TEXT,
        asset_id INTEGER NOT NULL,
        reported_at TIMESTAMP NOT NULL,
        reported_by TEXT,
        ward TEXT,
        fault_description TEXT NOT NULL,
        severity TEXT NOT NULL DEFAULT 'breakdown' CHECK (severity IN (
            'breakdown','degraded','preventive')),
        took_out_of_service INTEGER NOT NULL DEFAULT 1,
        down_from TIMESTAMP,
        restored_at TIMESTAMP,
        downtime_minutes INTEGER,
        status TEXT NOT NULL DEFAULT 'open' CHECK (status IN (
            'open','in_progress','awaiting_parts','closed','cancelled')),
        resolution TEXT,
        attended_by TEXT,
        vendor_id INTEGER,
        contract_id INTEGER,
        cost_paise INTEGER,
        cost_is_configured INTEGER NOT NULL DEFAULT 0,
        closed_by TEXT,
        closed_at TIMESTAMP,
        created_by TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""",

    # ── AMC / CMC contracts ───────────────────────────────────────────────────
    #
    # A real AMC covers a FLEET ("all 14 infusion pumps"), not one machine, so the
    # coverage is a link table rather than an asset_id column. A contract with no
    # linked assets is legal and means exactly what it says: somebody recorded the
    # paperwork and has not yet said what it covers. That is visible, not silently
    # treated as "covers everything" or "covers nothing".
    #
    # `end_date` IS THE LAST COVERED DAY (see contract_is_expired in
    # services/biomedical_service.py). That is the opposite boundary rule from
    # `biomedical_schedules.next_due_date`, which is the FIRST UNCOVERED day, and
    # the asymmetry is deliberate: a contract's end date is copied off a piece of
    # paper a lawyer wrote, and a calibration's due date is computed by this
    # system. Both rules live in one named function each so neither can drift.
    """CREATE TABLE IF NOT EXISTS biomedical_contracts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        contract_number TEXT NOT NULL,
        contract_type TEXT NOT NULL DEFAULT 'amc' CHECK (contract_type IN (
            'amc','cmc','warranty')),
        vendor_id INTEGER,
        vendor_name TEXT,
        start_date DATE NOT NULL,
        end_date DATE NOT NULL,
        value_paise INTEGER,
        value_is_configured INTEGER NOT NULL DEFAULT 0,
        notice_days INTEGER NOT NULL DEFAULT 60 CHECK (notice_days >= 0),
        covers_calibration INTEGER NOT NULL DEFAULT 0,
        covers_parts INTEGER NOT NULL DEFAULT 0,
        response_hours INTEGER,
        status TEXT NOT NULL DEFAULT 'active' CHECK (status IN (
            'active','expired','terminated','renewed')),
        scope_notes TEXT,
        created_by TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP
    )""",

    """CREATE TABLE IF NOT EXISTS biomedical_contract_assets (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        contract_id INTEGER NOT NULL,
        asset_id INTEGER NOT NULL,
        linked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""",
]

_M21_INDEXES = [
    # The asset tag is the hospital's own barcode/label. Unique, because two rows
    # with the same tag means two people maintaining what they each think is the
    # whole history of one machine.
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_biomed_assets_tag ON biomedical_assets(asset_tag)",
    "CREATE INDEX IF NOT EXISTS idx_biomed_assets_ward ON biomedical_assets(ward, is_active, status)",
    "CREATE INDEX IF NOT EXISTS idx_biomed_assets_dept ON biomedical_assets(department, is_active)",
    "CREATE INDEX IF NOT EXISTS idx_biomed_assets_crit ON biomedical_assets("
    "criticality, is_active, status)",
    "CREATE INDEX IF NOT EXISTS idx_biomed_assets_warranty ON biomedical_assets("
    "warranty_expiry_date) WHERE warranty_expiry_date IS NOT NULL",

    "CREATE UNIQUE INDEX IF NOT EXISTS idx_biomed_sched_asset_type ON biomedical_schedules("
    "asset_id, schedule_type)",
    # The overdue view's index. next_due_date leads because that is what the
    # register is ordered and filtered by.
    "CREATE INDEX IF NOT EXISTS idx_biomed_sched_due ON biomedical_schedules("
    "next_due_date, is_active, schedule_type)",
    "CREATE INDEX IF NOT EXISTS idx_biomed_sched_events_sched ON biomedical_schedule_events("
    "schedule_id, performed_date DESC, id DESC)",
    "CREATE INDEX IF NOT EXISTS idx_biomed_sched_events_asset ON biomedical_schedule_events("
    "asset_id, performed_date DESC)",

    "CREATE UNIQUE INDEX IF NOT EXISTS idx_biomed_calls_number ON biomedical_service_calls("
    "call_number) WHERE call_number IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS idx_biomed_calls_asset ON biomedical_service_calls("
    "asset_id, reported_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_biomed_calls_status ON biomedical_service_calls("
    "status, reported_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_biomed_calls_ward ON biomedical_service_calls(ward, status)",

    "CREATE UNIQUE INDEX IF NOT EXISTS idx_biomed_contracts_number ON biomedical_contracts("
    "contract_number)",
    "CREATE INDEX IF NOT EXISTS idx_biomed_contracts_end ON biomedical_contracts("
    "end_date, status)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_biomed_contract_assets ON biomedical_contract_assets("
    "contract_id, asset_id)",
    "CREATE INDEX IF NOT EXISTS idx_biomed_contract_assets_asset ON biomedical_contract_assets("
    "asset_id)",
]


def _m21_biomedical_assets(conn) -> None:
    """Biomedical asset & equipment tracking: the register, its maintenance and
    CALIBRATION schedules, breakdown/downtime, and AMC/CMC contracts.

    WHY THIS IS ITS OWN MIGRATION AND NOT AN EDIT TO 20.
    Migration 20 is shipped. Its docstring says a module that needs a column adds
    21+; this is that. Six new tables, no existing table touched, no data moved,
    no row rewritten — additive throughout.

    IT INHERITS MIGRATION 20's THREE RULES, and for the same reasons:

      1. NO FOREIGN KEYS. `biomedical_service_calls.ward` and
         `biomedical_assets.ward` point at the same free-text ward vocabulary the
         CLINICAL tables use, and `vendor_id` points at `vendors`. An FK here
         could make a clinical schema change or a vendor cleanup fail. The cost
         of a dangling id is a row that reads "unknown vendor"; the cost of the
         alternative is a failed clinical write.
      2. MONEY IS INTEGER PAISE and the column name says so. `purchase_cost_paise`,
         `cost_paise`, `value_paise`. No REAL column exists here.
      3. NOT CONFIGURED IS NOT ZERO. `cost_is_configured` / `value_is_configured`
         sit beside their amounts, because an AMC recorded as ₹0 and an AMC whose
         value nobody has typed are opposite facts.

    AND IT ADDS A FOURTH, WHICH IS THE POINT OF THE WHOLE MODULE:

      4. UNKNOWN IS NOT COMPLIANT. `biomedical_schedules.last_done_date` is
         nullable and a NULL means NEVER DONE — it is not backfilled from the
         commissioning date, not defaulted to today, and not hidden from the
         overdue register. A machine nobody has ever calibrated is the single
         most dangerous row this table can hold, and every default in this schema
         is chosen so that such a row is LOUD rather than absent.

    NOTHING HERE IS A CLINICAL PATH. No table above is read by medication
    ordering, administration, vitals, alerts, shadow capture or the audit chain,
    and no row in it can block a clinical action. An overdue calibration is a
    prominent warning on a screen. It does not stop a nurse using the pump — see
    the module docstring in services/biomedical_service.py for why that is the
    right call tonight and what the alternative would require.
    """
    for statement in _M21_TABLES:
        conn.execute(statement)
    for statement in _M21_INDEXES:
        conn.execute(statement)
    # NO SEED DATA. Not one asset, not one schedule. An equipment register that
    # ships with example rows is a register somebody has to audit before they can
    # trust it, and a fake ventilator with a fake calibration date is exactly the
    # class of thing that gets believed. The table is empty until a hospital
    # types into it.


# ── Migration 22: housekeeping & facilities ──────────────────────────────────

HOUSEKEEPING_TABLES: list[str] = [
    # ── The unit register: the things housekeeping actually turns around ──────
    #
    # A "unit" is a bed, a side room, a bay or a theatre — the thing that is
    # cleaned as a whole and that is either usable or not.
    #
    # WHY THIS TABLE HAS TO EXIST AT ALL. Orb has no bed registry. `patients.bed`
    # and `admissions.bed` are free-text labels, and ward capacity is a per-ward
    # COUNT (forecast_service.WARD_CAPACITY, a site setting). Nothing in the
    # system knows that bed "GM-1/03" exists as an object with a state. Cleaning
    # state is a property of a physical bed, so the bed has to become a row
    # before it can carry one.
    #
    # `state` DEFAULTS TO 'vacant_dirty', NOT 'vacant_ready'. Registering a bed
    # is an act of inventory, not an act of cleaning. "Ready" is a CLAIM that
    # somebody cleaned this bed, and no import, seed or default may make that
    # claim on a human being's behalf — see the readiness note in
    # services/housekeeping_service.py.
    """CREATE TABLE IF NOT EXISTS housekeeping_units (
        id TEXT PRIMARY KEY,
        unit_code TEXT NOT NULL,
        ward TEXT NOT NULL,
        room TEXT,
        bed_label TEXT,
        unit_type TEXT NOT NULL DEFAULT 'bed'
            CHECK (unit_type IN ('bed', 'room', 'bay', 'theatre')),
        is_isolation_room INTEGER NOT NULL DEFAULT 0,
        state TEXT NOT NULL DEFAULT 'vacant_dirty'
            CHECK (state IN ('occupied', 'vacant_dirty', 'cleaning_in_progress',
                             'vacant_ready', 'out_of_service')),
        state_since TEXT,
        is_active INTEGER NOT NULL DEFAULT 1,
        notes TEXT,
        created_at TEXT,
        updated_at TEXT
    )""",

    # ── The state ledger ──────────────────────────────────────────────────────
    #
    # Every transition, with who moved it and when. `housekeeping_units.state` is
    # the CACHE of the newest row here; this is the record. A turnaround time is
    # a difference between two of these timestamps, so if the ledger is wrong the
    # report is wrong, and a report nobody can audit is a report nobody should
    # act on.
    """CREATE TABLE IF NOT EXISTS housekeeping_unit_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        unit_id TEXT NOT NULL,
        from_state TEXT,
        to_state TEXT NOT NULL,
        task_id TEXT,
        reason TEXT,
        actor_user_id TEXT,
        actor_role TEXT,
        occurred_at TEXT NOT NULL
    )""",

    # ── Cleaning requests ─────────────────────────────────────────────────────
    #
    # NO patient_id, NO patient name, NO diagnosis, and no copy of the chart's
    # isolation text. A housekeeping board is read at a corridor trolley by staff
    # who hold no clinical grant. The ONE clinical fact that crosses into this
    # table is `clean_type = 'terminal'` — a boolean derived from whether the
    # departing admission carried isolation precautions — because a housekeeper
    # sent into an isolation room without knowing it is an infection-control
    # harm, and PPE selection depends on it. The precaution TEXT is never copied;
    # `precaution_note` is a free-text field a human types, not a chart mirror.
    #
    # FOUR TIMESTAMPS, AND THEY ARE NOT INTERCHANGEABLE:
    #   vacated_at   — the unit became vacant_dirty. The turnaround clock STARTS.
    #   requested_at — somebody raised the request. May be much later.
    #   started_at   — a cleaner started. `completed_at` closes the clean.
    #   ready_at     — the unit became vacant_ready. The turnaround clock STOPS.
    # Turnaround = ready_at − vacated_at. Measuring requested_at → completed_at
    # instead would hide the exact delay a bed manager is trying to find: the
    # hour a dirty bed sat before anyone asked for it to be cleaned.
    """CREATE TABLE IF NOT EXISTS housekeeping_tasks (
        id TEXT PRIMARY KEY,
        unit_id TEXT NOT NULL,
        ward TEXT NOT NULL,
        request_type TEXT NOT NULL
            CHECK (request_type IN ('discharge_turnaround', 'transfer_turnaround',
                                    'spillage', 'routine', 'deep_clean',
                                    'post_maintenance')),
        priority TEXT NOT NULL
            CHECK (priority IN ('routine', 'urgent', 'immediate')),
        clean_type TEXT NOT NULL DEFAULT 'standard'
            CHECK (clean_type IN ('standard', 'terminal')),
        expected_minutes INTEGER NOT NULL,
        status TEXT NOT NULL DEFAULT 'open'
            CHECK (status IN ('open', 'in_progress', 'done', 'cancelled')),
        source_type TEXT NOT NULL DEFAULT 'manual'
            CHECK (source_type IN ('manual', 'discharge', 'transfer')),
        source_ref TEXT,
        description TEXT,
        precaution_note TEXT,
        vacated_at TEXT,
        requested_at TEXT NOT NULL,
        requested_by_user_id TEXT,
        started_at TEXT,
        started_by_user_id TEXT,
        completed_at TEXT,
        completed_by_user_id TEXT,
        ready_at TEXT,
        cancelled_at TEXT,
        cancelled_by_user_id TEXT,
        cancel_reason TEXT,
        created_at TEXT
    )""",

    # ── Linen ────────────────────────────────────────────────────────────────
    #
    # A tiny master so the four wards of a pilot site do not each invent their own
    # spelling of "draw sheet". Counts only — linen has no price column here and
    # must not grow one: services/money.py owns every rupee in Orb, and a linen
    # valuation belongs to procurement's item master if a site ever wants it.
    """CREATE TABLE IF NOT EXISTS housekeeping_linen_items (
        code TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        is_active INTEGER NOT NULL DEFAULT 1
    )""",

    # `direction` is four words, not two, because "return" is ambiguous and the
    # ambiguity is where linen goes missing: clean linen ISSUED to a ward comes
    # back either soiled (to the laundry) or unused-clean (to the store), and
    # some of it is condemned. Outstanding = issued − soiled − clean − condemned,
    # which is the number a linen room actually argues about.
    """CREATE TABLE IF NOT EXISTS housekeeping_linen_movements (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ward TEXT NOT NULL,
        item_code TEXT NOT NULL,
        direction TEXT NOT NULL
            CHECK (direction IN ('issue', 'return_soiled', 'return_clean', 'condemned')),
        quantity INTEGER NOT NULL CHECK (quantity > 0),
        task_id TEXT,
        note TEXT,
        actor_user_id TEXT,
        moved_at TEXT NOT NULL,
        idempotency_key TEXT
    )""",
]


HOUSEKEEPING_INDEXES: list[str] = [
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_hk_units_code ON housekeeping_units(unit_code)",
    "CREATE INDEX IF NOT EXISTS idx_hk_units_ward ON housekeeping_units(ward, state, unit_code)",
    "CREATE INDEX IF NOT EXISTS idx_hk_events_unit ON housekeeping_unit_events(unit_id, id DESC)",
    "CREATE INDEX IF NOT EXISTS idx_hk_events_task ON housekeeping_unit_events(task_id, id)",

    # ── THE IDEMPOTENCY GUARD ────────────────────────────────────────────────
    #
    # THIS INDEX IS THE ONLY THING THAT STOPS ONE DISCHARGE PRODUCING TEN
    # CLEANING TASKS. `sync_discharge_turnarounds` is a PULL: it re-reads recent
    # discharges every time the housekeeping board is opened, because the
    # discharge write path may not be touched to push at it. So the same
    # discharge is offered to the inserter on every single board load, and the
    # database — not an application-level "have I seen this?" check, which races
    # two tabs against each other — is what refuses the duplicate.
    #
    # It is a PARTIAL index (`WHERE source_ref IS NOT NULL`) so that the many
    # manually-raised tasks, which have no source_ref, are unconstrained.
    #
    # Deliberately NOT here: a unique index on `unit_id WHERE status IN
    # ('open','in_progress')`. A spillage in an occupied room and that room's
    # later discharge turnaround are two legitimate open tasks on one bed, and a
    # constraint that silently swallowed the second would lose a request.
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_hk_tasks_source ON housekeeping_tasks("
    "source_type, source_ref) WHERE source_ref IS NOT NULL",

    "CREATE INDEX IF NOT EXISTS idx_hk_tasks_queue ON housekeeping_tasks("
    "status, priority, requested_at)",
    "CREATE INDEX IF NOT EXISTS idx_hk_tasks_unit ON housekeeping_tasks(unit_id, id DESC)",
    "CREATE INDEX IF NOT EXISTS idx_hk_tasks_ward ON housekeeping_tasks(ward, ready_at)",

    "CREATE UNIQUE INDEX IF NOT EXISTS idx_hk_linen_idempotency ON "
    "housekeeping_linen_movements(idempotency_key) WHERE idempotency_key IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS idx_hk_linen_ward ON housekeeping_linen_movements("
    "ward, item_code, moved_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_hk_linen_item ON housekeeping_linen_movements("
    "item_code, direction, moved_at DESC)",
]


#: Structural linen vocabulary. Names only — no counts, no prices, no ward
#: allocations. A site renames or deactivates these; it never has to invent the
#: word "pillowcase" before it can record its first issue.
_HOUSEKEEPING_LINEN_SEED: list[tuple[str, str]] = [
    ("BEDSHEET", "Bed sheet"),
    ("DRAWSHEET", "Draw sheet"),
    ("PILLOWCASE", "Pillow cover"),
    ("BLANKET", "Blanket"),
    ("TOWEL", "Towel"),
    ("GOWN", "Patient gown"),
    ("MACKINTOSH", "Mackintosh / rubber sheet"),
]


def _m22_housekeeping(conn) -> None:
    """Housekeeping & facilities: bed/room cleaning state, cleaning requests,
    turnaround reporting and linen movement.

    WHY THIS MODULE EXISTS AT ALL. A discharged bed is not an available bed. It
    is a dirty bed, and between the patient leaving and the next patient arriving
    there is a real, measurable, frequently-hours-long gap that nothing in Orb
    could previously see. Turnaround time is the thing that actually gates
    capacity in an Indian hospital far more often than the number of beds does.

    IT INHERITS MIGRATION 20's RULES:

      1. NO FOREIGN KEYS. `housekeeping_units.ward` speaks the same free-text
         ward vocabulary the CLINICAL tables use, and `housekeeping_tasks
         .source_ref` points at a discharge record. An FK to a clinical table
         would let a housekeeping row block a clinical delete or a clinical
         schema rebuild. A dangling id costs a row that reads "unknown unit".
      2. NO MONEY. There is no `_paise` column here because there is no money in
         housekeeping — and if one is ever needed it goes through
         services/money.py, never a REAL column added here.
      3. NOT CONFIGURED IS NOT ZERO — expressed here as NOT-CLEANED IS NOT READY.
         `state` defaults to 'vacant_dirty'. A bed becomes 'vacant_ready' only
         when a named person records that a clean finished.

    AND THE ONE THAT IS SPECIFIC TO IT:

      4. THIS SCHEMA DOES NOT REDEFINE CAPACITY. Nothing in migration 20's or
         this migration's tables is read by forecast_service, capacity_sim_service
         or command_center_service. `available` on the capacity screen still means
         exactly what it meant yesterday — ward bed count minus admitted patients
         — and this module publishes its own, separately-named readiness figures
         beside a coverage percentage. See the CAPACITY section of
         services/housekeeping_service.py for why they are not merged.

    NOTHING HERE IS A CLINICAL PATH. No table above is read by medication
    ordering, administration, vitals, alerts, shadow capture, the audit chain,
    auth or the offline queue, and no row in it can block a clinical action. A
    bed this module calls 'vacant_dirty' can still be admitted into by
    `POST /api/admissions/admit`, which never consults housekeeping — the
    refusal in `transition_unit` refuses a HOUSEKEEPING RECORD, not a patient.

    Additive throughout: 5 new tables, no existing table touched, no data moved.
    """
    for statement in HOUSEKEEPING_TABLES:
        conn.execute(statement)
    for statement in HOUSEKEEPING_INDEXES:
        conn.execute(statement)

    # Structural seed ONLY, and only the linen vocabulary. NOT ONE UNIT. A bed
    # register that ships with invented beds is a register whose readiness figure
    # describes a building that does not exist — and readiness is a number a bed
    # manager will act on. The unit table stays empty until a hospital types its
    # real beds into it, and every readiness response says how many beds it is
    # actually covering so that "0 of 12 registered" can never read as "all clean".
    conn.executemany(
        "INSERT OR IGNORE INTO housekeeping_linen_items (code, name, is_active) VALUES (?, ?, 1)",
        _HOUSEKEEPING_LINEN_SEED,
    )


# ── Migration 23: ABDM / ABHA rails ──────────────────────────────────────────
#
# READ THIS BEFORE READING THE DDL.
#
# ORB HAS NO CONNECTION TO THE ABDM GATEWAY. None of these tables is a queue, an
# outbox, a sync cursor or a cache of anything the National Health Authority
# holds. Every row is a RECORD OF A STEP A HUMAN PERFORMED SOMEWHERE ELSE — on
# the ABDM/NHA portal, in the ABHA app, or on the HIP console of whatever
# software is actually onboarded. Nothing in this schema is ever transmitted.
#
# That fact is enforced by the schema itself in three ways:
#
#   1. EVERY table that records an ABDM step carries an explicit "where the
#      human did it" column (`performed_where`, `link_method`, `recorded_where`)
#      whose CHECK list contains no value meaning "Orb sent it". There is no
#      column that could hold a gateway response, a request id Orb generated, or
#      a transmission timestamp, because there is nothing to put in one.
#   2. `abha_identities.verification_status` DEFAULTS TO 'unverified' and the
#      column is NOT NULL. An ABHA number keyed at a counter is unverified until
#      a verification row says otherwise. There is no path in the schema by which
#      a row arrives verified.
#   3. Care-context links and consent artefacts are never deleted. Unlinking and
#      revocation are STATUS CHANGES with a mandatory reason plus an `abdm_events`
#      row, so "this was linked and then it wasn't" is always answerable.
#
# NO FOREIGN KEYS — same rule as migration 20, same reason: `PRAGMA
# foreign_keys=ON` is set on every Orb connection, and an FK from an
# administrative table into `patients`/`admissions`/`users` would let a
# compliance row make a clinical write or a clinical schema change fail. Linkage
# is by plain id column plus an index.
#
# NO CLINICAL PATH. Nothing here is read by medication ordering, administration,
# vitals, alerts, shadow capture, the audit chain, auth or the offline queue. A
# patient with no ABHA, an unverified ABHA or a revoked consent is treated
# exactly like any other patient by every clinical surface in Orb.

_M23_TABLES: list[str] = [
    # ── The ABHA identity itself ──────────────────────────────────────────────
    #
    # One row per ABHA a patient presents. A patient may legitimately have more
    # than one over time (a duplicate created at another facility, an address
    # re-created after a lost login), which is why this is not a column on
    # `patients`: overwriting one ABHA with another destroys the record that the
    # first one was ever used to link a care context.
    """CREATE TABLE IF NOT EXISTS abha_identities (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        patient_id TEXT NOT NULL,
        -- Normalised to 14 bare digits. The display form (XX-XXXX-XXXX-XXXX) is
        -- derived, never stored twice, so the two can never disagree.
        abha_number TEXT,
        -- Normalised to lowercase `username@suffix`. NULL is ordinary: plenty of
        -- patients hold a number and no address, or an address and no number.
        abha_address TEXT,
        -- THE DEFAULT IS THE WHOLE POINT. A number keyed at a registration
        -- counter is 'unverified' and must read as unverified everywhere until a
        -- row in `abha_verifications` with outcome='verified' says otherwise.
        -- 'deactivated' means the desk was TOLD (elsewhere) that this ABHA is no
        -- longer valid; it is not something Orb can discover.
        verification_status TEXT NOT NULL DEFAULT 'unverified' CHECK (verification_status IN (
            'unverified','verified','verification_failed','deactivated')),
        verified_at TIMESTAMP,
        verified_by TEXT,
        verification_method TEXT,
        verification_reference TEXT,
        is_primary INTEGER NOT NULL DEFAULT 1,
        status TEXT NOT NULL DEFAULT 'active' CHECK (status IN (
            'active','superseded','withdrawn')),
        -- How the value got into Orb. 'patient_stated' (read out from memory) is
        -- kept distinct from 'abha_card' (a card was in someone's hand) because
        -- they are different levels of evidence and a desk should see which.
        source TEXT NOT NULL DEFAULT 'counter_keyed' CHECK (source IN (
            'counter_keyed','abha_card','abha_app_qr','patient_stated','other')),
        notes TEXT,
        recorded_by TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""",

    # ── The verification record ───────────────────────────────────────────────
    #
    # WHO verified, WHEN, and BY WHAT METHOD — the three facts a NABH assessor or
    # an NHA auditor asks for, and the three facts a `verified` boolean cannot
    # carry. Append-only: a failed verification is kept, because "we tried the
    # Aadhaar OTP twice and it failed both times" is the fact that explains why a
    # number is still unverified three weeks later.
    """CREATE TABLE IF NOT EXISTS abha_verifications (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        abha_identity_id INTEGER NOT NULL,
        patient_id TEXT NOT NULL,
        method TEXT NOT NULL CHECK (method IN (
            'aadhaar_otp','mobile_otp','demographic_match','abha_card_sighted',
            'abha_app_qr','other')),
        outcome TEXT NOT NULL CHECK (outcome IN ('verified','failed','inconclusive')),
        -- WHERE THE HUMAN DID IT. There is deliberately no value on this list
        -- that means "Orb called the gateway", because Orb cannot.
        performed_where TEXT NOT NULL DEFAULT 'outside_orb' CHECK (performed_where IN (
            'abdm_portal','abha_app','partner_software','in_person','outside_orb')),
        performed_by TEXT NOT NULL,
        -- When the human did it, which may be well before Orb heard about it.
        performed_at TIMESTAMP NOT NULL,
        recorded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        recorded_by TEXT,
        -- The transaction id copied off the ABDM screen, if the operator kept
        -- it. Orb never generates one: a reference Orb invented would look
        -- exactly like a gateway reference in an audit and mean nothing.
        reference TEXT,
        failure_reason TEXT,
        notes TEXT
    )""",

    # ── Care-context links ────────────────────────────────────────────────────
    #
    # A care context is one episode (an admission, an OPD visit) that was linked
    # to an ABHA so the patient can pull it into their PHR. The row is the record
    # that a human performed the link on a HIP console elsewhere.
    #
    # NEVER DELETED. Unlinking sets status='unlinked' with a mandatory reason and
    # writes an `abdm_events` row. A DELETE would make "was this episode ever
    # linked?" unanswerable, and that question is exactly what a patient asks
    # when their record vanishes from their app.
    """CREATE TABLE IF NOT EXISTS abdm_care_contexts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        abha_identity_id INTEGER NOT NULL,
        patient_id TEXT NOT NULL,
        care_context_reference TEXT NOT NULL,
        display TEXT,
        hi_type TEXT NOT NULL DEFAULT 'OPConsultation' CHECK (hi_type IN (
            'OPConsultation','DischargeSummary','Prescription','DiagnosticReport',
            'ImmunizationRecord','HealthDocumentRecord','WellnessRecord')),
        encounter_type TEXT NOT NULL DEFAULT 'admission' CHECK (encounter_type IN (
            'admission','opd_visit','other')),
        -- admissions.id when encounter_type='admission'. Plain id, no FK.
        encounter_ref TEXT,
        encounter_date DATE,
        status TEXT NOT NULL DEFAULT 'linked' CHECK (status IN ('linked','unlinked')),
        linked_at TIMESTAMP NOT NULL,
        linked_by TEXT NOT NULL,
        link_method TEXT NOT NULL DEFAULT 'abdm_portal' CHECK (link_method IN (
            'abdm_portal','hip_console','partner_software','other')),
        link_reference TEXT,
        unlinked_at TIMESTAMP,
        unlinked_by TEXT,
        unlink_reason TEXT,
        recorded_by TEXT,
        notes TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""",

    # ── Consent artefacts ─────────────────────────────────────────────────────
    #
    # Under ABDM a Health Information User asks the patient's Consent Manager for
    # data; the artefact is the patient's answer, with a purpose, a scope (which
    # health-information types), a date range over the records, and its own
    # expiry. Orb records that artefact. Orb is not the consent manager, does not
    # evaluate the artefact, and does not release data on the strength of it.
    #
    # `date_range_from/to` (which RECORDS are covered) and `expires_at` (how long
    # the PERMISSION lasts) are different clocks and are stored separately; a
    # single "valid till" column conflates them and is how a desk ends up
    # believing a two-year-old artefact still authorises anything.
    """CREATE TABLE IF NOT EXISTS abdm_consent_artefacts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        -- The consent manager's own id, when the desk has it. NULL while a
        -- request is outstanding — an artefact does not exist until it is granted.
        artefact_id TEXT,
        request_id TEXT,
        patient_id TEXT NOT NULL,
        abha_identity_id INTEGER,
        requester_name TEXT NOT NULL,
        requester_type TEXT NOT NULL DEFAULT 'hiu' CHECK (requester_type IN (
            'hiu','hip','patient','other')),
        -- ABDM's own purpose-of-use codes. 'OTHER' exists so an artefact whose
        -- purpose Orb does not recognise is recorded verbatim in purpose_text
        -- rather than forced into the nearest code.
        purpose_code TEXT NOT NULL DEFAULT 'CAREMGT' CHECK (purpose_code IN (
            'CAREMGT','BTG','PUBHLTH','HPAYMT','DSRCH','PATRQT','SLFRPT','OTHER')),
        purpose_text TEXT,
        -- JSON array of HI types. Empty array means the desk did not record the
        -- scope, which is NOT the same as "no data" and reads as such.
        hi_types_json TEXT NOT NULL DEFAULT '[]',
        date_range_from DATE,
        date_range_to DATE,
        expires_at TIMESTAMP,
        status TEXT NOT NULL DEFAULT 'requested' CHECK (status IN (
            'requested','granted','denied','expired','revoked')),
        requested_at TIMESTAMP,
        granted_at TIMESTAMP,
        denied_at TIMESTAMP,
        denial_reason TEXT,
        revoked_at TIMESTAMP,
        revoked_by TEXT,
        -- NOT NULL is impossible here (the column is empty until revocation) so
        -- the mandatory-reason rule lives in the service and its test. A revoked
        -- artefact with no reason is the row that gets argued about later.
        revocation_reason TEXT,
        recorded_where TEXT NOT NULL DEFAULT 'consent_manager' CHECK (recorded_where IN (
            'consent_manager','abdm_portal','partner_software','in_person','outside_orb')),
        recorded_by TEXT,
        notes TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""",

    # ── The event trail ───────────────────────────────────────────────────────
    #
    # One append-only line per state change across all three record types. This
    # is NOT the audit chain (services/audit_service owns that, and the routes
    # still write to it) — it is the module's own history, so a screen can show
    # "linked 12 Aug by NUR004, unlinked 14 Aug by ADM001 because the episode was
    # merged" without reconstructing it from a hash chain.
    """CREATE TABLE IF NOT EXISTS abdm_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        entity_type TEXT NOT NULL CHECK (entity_type IN (
            'abha_identity','care_context','consent_artefact')),
        entity_id INTEGER NOT NULL,
        patient_id TEXT,
        event_type TEXT NOT NULL,
        from_status TEXT,
        to_status TEXT,
        detail TEXT,
        reason TEXT,
        actor_id TEXT,
        -- When the human did the thing, elsewhere. Distinct from recorded_at,
        -- which is when Orb heard about it.
        occurred_at TIMESTAMP,
        recorded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        metadata_json TEXT
    )""",
]

_M23_INDEXES: list[str] = [
    "CREATE INDEX IF NOT EXISTS idx_abha_patient ON abha_identities(patient_id, status, id DESC)",
    # Partial-unique on the LIVE rows only: the same ABHA number must not be
    # attached to two patients at once (that is a merge error and a PHI hazard),
    # but a superseded/withdrawn row keeps its value so the history survives.
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_abha_number_live ON abha_identities(abha_number) "
    "WHERE abha_number IS NOT NULL AND status = 'active'",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_abha_address_live ON abha_identities(abha_address) "
    "WHERE abha_address IS NOT NULL AND status = 'active'",
    "CREATE INDEX IF NOT EXISTS idx_abha_verification_state ON abha_identities("
    "verification_status, status, id DESC)",

    "CREATE INDEX IF NOT EXISTS idx_abha_verif_identity ON abha_verifications("
    "abha_identity_id, id DESC)",
    "CREATE INDEX IF NOT EXISTS idx_abha_verif_patient ON abha_verifications("
    "patient_id, performed_at DESC)",

    "CREATE INDEX IF NOT EXISTS idx_cc_patient ON abdm_care_contexts(patient_id, status, id DESC)",
    "CREATE INDEX IF NOT EXISTS idx_cc_identity ON abdm_care_contexts(abha_identity_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_cc_encounter ON abdm_care_contexts(encounter_ref, status)",
    # One LIVE link per (ABHA, care context). Re-linking after an unlink is
    # allowed and creates a new row; two simultaneous links are not.
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_cc_live_reference ON abdm_care_contexts("
    "abha_identity_id, care_context_reference) WHERE status = 'linked'",

    "CREATE INDEX IF NOT EXISTS idx_consent_patient ON abdm_consent_artefacts("
    "patient_id, status, id DESC)",
    "CREATE INDEX IF NOT EXISTS idx_consent_status ON abdm_consent_artefacts("
    "status, expires_at)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_consent_artefact_id ON abdm_consent_artefacts("
    "artefact_id) WHERE artefact_id IS NOT NULL",

    "CREATE INDEX IF NOT EXISTS idx_abdm_events_entity ON abdm_events(entity_type, entity_id, id)",
    "CREATE INDEX IF NOT EXISTS idx_abdm_events_patient ON abdm_events(patient_id, id DESC)",
]


def _m23_abdm_abha_rails(conn) -> None:
    """ABDM / ABHA rails: identities, verifications, care-context links, consent
    artefacts, and the event trail that ties them together.

    Five new tables, no existing table touched, no data moved, NO SEED ROWS. A
    seeded ABHA number would be a fabricated national health id sitting in a
    compliance table — the single worst row this schema could ship with.

    See the block comment above for the three ways this schema encodes "Orb does
    not talk to the gateway", and why verification_status defaults to
    'unverified'.
    """
    for statement in _M23_TABLES:
        conn.execute(statement)
    for statement in _M23_INDEXES:
        conn.execute(statement)


def _m24_workforce_attendance(conn) -> None:
    """Staff employment, duty roster, attendance and the payroll EXPORT.

    THE BOUNDARY THIS SCHEMA ENCODES — read before adding a column here.
    ────────────────────────────────────────────────────────────────────
    Orb owns the ROSTER and the ATTENDANCE, because Orb is the only system in
    the building that knows who was actually on the ward at 03:00. Orb does NOT
    own the PAY. There is no salary column, no PF column, no ESI column and no
    tax column in this migration, and adding one would be a decision to take on
    a statutory liability:

      * PF, ESI, Professional Tax and TDS are computed from rules that change
        with every Union and State budget, differ by state, and carry interest
        and penalty when they are wrong. A hospital's payroll bureau is paid to
        track them and is accountable for them.
      * Getting an employee's PF wrong is not a bug report, it is a demand
        notice. Orb cannot accept that exposure to save a CSV round trip.

    So the money a hospital's payroll needs is DELIBERATELY ABSENT from the
    database, not merely left blank in the export — a blank column that exists
    is a column somebody eventually fills in.

    WHAT IS HERE INSTEAD: countable facts. Days present, days absent, leave days
    by type, minutes worked, minutes of overtime, minutes late, and the count of
    allowance-bearing shifts. Every one of them is something a person did or did
    not do, and every one of them is something Orb watched happen.

    RULES CARRIED OVER FROM MIGRATION 20 (see its docstring for the full
    reasoning), because this schema points at the same clinical tables:

      1. NO FOREIGN KEYS. `staff_employment.user_id` points at `users` by plain
         id. An FK would make a clinical-table rebuild fail or orphan rows, and
         a payroll row must never be able to break a clinical write.
      2. NO REAL COLUMNS. Durations are INTEGER MINUTES. There is no money here
         at all, so there is no `_paise` column either — which is itself the
         statement this module is making.
      3. NOT RECORDED IS NOT ABSENT. A day with no `attendance_days` row is
         UNRECORDED, and the summary counts it in its own bucket. Treating a
         missing row as "absent" would dock a nurse's pay for the ward
         in-charge's paperwork; treating it as "present" would pay for a shift
         nobody worked. Both are silent. The third bucket is the honest answer,
         and it is why nothing in this schema back-fills a default day.

    Three CHECK constraints do work the services would otherwise have to be
    trusted with, and they survive somebody writing raw SQL:
      * a `leave` day must name its leave type;
      * only a `present` day may carry worked or overtime minutes;
      * durations cannot be negative.

    Additive: 7 new tables, no existing table touched, no data moved.
    """
    for statement in _M24_TABLES:
        conn.execute(statement)
    for statement in _M24_INDEXES:
        conn.execute(statement)

    # Structural seed only. Shift WINDOWS are the classic Indian three-shift
    # hospital pattern (07–14, 14–21, 21–07) plus a general administrative
    # shift; a site edits them. `allowance_code` marks which shifts a site's own
    # policy typically pays an allowance for — Orb COUNTS those shifts, it does
    # not price them.
    conn.executemany(
        "INSERT OR IGNORE INTO duty_shift_types "
        "(code, name, start_minute, duration_minutes, allowance_code, is_active) "
        "VALUES (?, ?, ?, ?, ?, 1)",
        [
            ("GENERAL", "General (09:00-17:00)", 9 * 60, 8 * 60, None),
            ("MORNING", "Morning (07:00-14:00)", 7 * 60, 7 * 60, None),
            ("EVENING", "Evening (14:00-21:00)", 14 * 60, 7 * 60, "evening"),
            # 21:00 → 07:00 the next day. duration > (1440 - start_minute) is
            # how a shift declares it crosses midnight; nothing stores a flag.
            ("NIGHT", "Night (21:00-07:00)", 21 * 60, 10 * 60, "night"),
        ],
    )
    # `is_paid` is the ONE money-adjacent fact Orb keeps, and it is a
    # CLASSIFICATION, not an amount: the export reports paid and unpaid leave
    # days separately because a bureau needs the split to compute loss of pay,
    # and only the hospital knows which of its leave types are which.
    conn.executemany(
        "INSERT OR IGNORE INTO leave_types (code, name, is_paid, sort_order, is_active) "
        "VALUES (?, ?, ?, ?, 1)",
        [
            ("CL", "Casual leave", 1, 10),
            ("SL", "Sick leave", 1, 20),
            ("EL", "Earned / privilege leave", 1, 30),
            ("CO", "Compensatory off", 1, 40),
            ("ML", "Maternity leave", 1, 50),
            ("LWP", "Leave without pay", 0, 60),
        ],
    )


# ── Migration 24 DDL (workforce) ─────────────────────────────────────────────

_M24_TABLES: list[str] = [
    # WHO IS EMPLOYED. Separate from `users` on purpose: most of a hospital's
    # payroll — housekeeping, security, CSSD, kitchen, drivers — has no Orb
    # login and never will, so an employment record cannot be a login record.
    # `user_id` is the OPTIONAL bridge for the staff who do log in.
    """CREATE TABLE IF NOT EXISTS staff_employment (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        employee_code TEXT NOT NULL UNIQUE,
        user_id TEXT,
        full_name TEXT NOT NULL,
        department TEXT,
        designation TEXT,
        employment_type TEXT NOT NULL DEFAULT 'permanent' CHECK (employment_type IN (
            'permanent','probation','contract','consultant','trainee','outsourced')),
        date_of_joining DATE,
        date_of_exit DATE,
        -- 0 = Monday … 6 = Sunday. NULL means ROTATIONAL: this person's day off
        -- moves with the roster, so no calendar rule can derive it and the
        -- summary must be TOLD which days were off rather than guessing.
        weekly_off_day INTEGER CHECK (weekly_off_day IS NULL OR (weekly_off_day BETWEEN 0 AND 6)),
        -- The payroll bureau's own identifier for this person, when they use
        -- one. Carried through the export untouched so the bureau's import does
        -- not have to match on a name.
        payroll_bureau_ref TEXT,
        is_active INTEGER NOT NULL DEFAULT 1,
        notes TEXT,
        created_by TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""",

    # WHAT A SHIFT IS. Held as start + duration in minutes, never as two clock
    # strings: "21:00-07:00" cannot be compared for overlap without first
    # deciding which day the 07:00 belongs to, and that decision is exactly
    # where a night-shift roster conflict goes undetected.
    """CREATE TABLE IF NOT EXISTS duty_shift_types (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        code TEXT NOT NULL UNIQUE,
        name TEXT NOT NULL,
        start_minute INTEGER NOT NULL CHECK (start_minute BETWEEN 0 AND 1439),
        duration_minutes INTEGER NOT NULL CHECK (duration_minutes > 0 AND duration_minutes <= 1440),
        -- Marks a shift a site's policy typically pays an allowance for. Orb
        -- counts these shifts; the amount is the payroll provider's.
        allowance_code TEXT CHECK (allowance_code IS NULL OR allowance_code IN ('night','evening')),
        is_active INTEGER NOT NULL DEFAULT 1,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""",

    # WHO IS PLANNED TO BE ON. The PLANNED half of "planned vs actual".
    """CREATE TABLE IF NOT EXISTS duty_roster (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        roster_date DATE NOT NULL,
        shift_code TEXT NOT NULL,
        employee_code TEXT NOT NULL,
        department TEXT,
        ward TEXT,
        assigned_role TEXT,
        status TEXT NOT NULL DEFAULT 'planned' CHECK (status IN (
            'planned','published','cancelled')),
        notes TEXT,
        created_by TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""",

    # WHO ACTUALLY CAME. One row per employee per calendar day, and the ABSENCE
    # of a row means UNRECORDED — never "absent". See the migration docstring.
    """CREATE TABLE IF NOT EXISTS attendance_days (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        attendance_date DATE NOT NULL,
        employee_code TEXT NOT NULL,
        -- The shift actually worked, which is not always the shift rostered.
        shift_code TEXT,
        status TEXT NOT NULL CHECK (status IN (
            'present','absent','leave','weekly_off','holiday')),
        leave_type_code TEXT,
        -- Full UTC instants, not clock strings: a night shift's in and out fall
        -- on two different calendar days, and a duration derived from two
        -- times-of-day has to guess which. Nothing here guesses.
        first_in_at TIMESTAMP,
        last_out_at TIMESTAMP,
        worked_minutes INTEGER NOT NULL DEFAULT 0,
        late_minutes INTEGER NOT NULL DEFAULT 0,
        overtime_minutes INTEGER NOT NULL DEFAULT 0,
        source TEXT NOT NULL DEFAULT 'manual' CHECK (source IN (
            'manual','biometric_import','roster_default')),
        remarks TEXT,
        recorded_by TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        -- A leave day that does not say WHICH leave is a day the bureau cannot
        -- price and the employee cannot dispute.
        CHECK (status <> 'leave' OR leave_type_code IS NOT NULL),
        -- Only a day somebody worked can carry worked or overtime minutes.
        -- Without this, a 'leave' row with 480 worked minutes would pass every
        -- service check and quietly inflate a month's hours.
        CHECK (status = 'present' OR (worked_minutes = 0 AND overtime_minutes = 0)),
        CHECK (worked_minutes >= 0 AND late_minutes >= 0 AND overtime_minutes >= 0)
    )""",

    # The hospital's leave vocabulary. `is_paid` is a classification, not a rate.
    """CREATE TABLE IF NOT EXISTS leave_types (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        code TEXT NOT NULL UNIQUE,
        name TEXT NOT NULL,
        is_paid INTEGER NOT NULL DEFAULT 1,
        sort_order INTEGER NOT NULL DEFAULT 100,
        is_active INTEGER NOT NULL DEFAULT 1,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""",

    # Declared non-working days. ONE list, hospital-wide: this is a single-site
    # appliance, and a per-state holiday calendar would be a fiction on a box
    # that sits in exactly one state.
    """CREATE TABLE IF NOT EXISTS hospital_holidays (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        holiday_date DATE NOT NULL UNIQUE,
        name TEXT NOT NULL,
        -- A "restricted"/optional holiday is one an employee may choose to
        -- take. It is recorded for the calendar; it does not by itself remove
        -- the day from anybody's working days.
        is_restricted INTEGER NOT NULL DEFAULT 0,
        created_by TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""",

    # WHAT WE SENT THE BUREAU, AND WHEN. A payroll dispute three months later is
    # answered by the checksum of the exact file that left the building, not by
    # regenerating it from data that has since been corrected.
    """CREATE TABLE IF NOT EXISTS payroll_export_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        period_start DATE NOT NULL,
        period_end DATE NOT NULL,
        department TEXT,
        file_name TEXT NOT NULL,
        row_count INTEGER NOT NULL DEFAULT 0,
        employee_count INTEGER NOT NULL DEFAULT 0,
        -- How many employees in this file had at least one unrecorded day. A
        -- non-zero number here is why a bureau's query gets an answer instead
        -- of an argument.
        incomplete_employee_count INTEGER NOT NULL DEFAULT 0,
        -- The exact header line that was sent. A bureau's importer breaks on a
        -- column-order change, so the order it received is part of the record.
        column_signature TEXT NOT NULL,
        sha256 TEXT NOT NULL,
        generated_by TEXT,
        generated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        notes TEXT
    )""",
]

_M24_INDEXES: list[str] = [
    "CREATE INDEX IF NOT EXISTS idx_staff_employment_active ON staff_employment("
    "is_active, department, full_name)",
    "CREATE INDEX IF NOT EXISTS idx_staff_employment_user ON staff_employment(user_id)",

    # The same person cannot hold the same shift on the same day twice. Partial
    # so that cancelling an assignment frees the slot for a re-assign rather
    # than leaving a tombstone in the way.
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_duty_roster_slot ON duty_roster("
    "roster_date, shift_code, employee_code) WHERE status <> 'cancelled'",
    "CREATE INDEX IF NOT EXISTS idx_duty_roster_day ON duty_roster("
    "roster_date, department, shift_code)",
    "CREATE INDEX IF NOT EXISTS idx_duty_roster_employee ON duty_roster("
    "employee_code, roster_date)",

    # One attendance row per person per day — the identity the monthly summary's
    # arithmetic rests on. Enforced by the DATABASE because a duplicate row
    # would make present + absent + leave exceed the working days with no error
    # raised anywhere.
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_attendance_day ON attendance_days("
    "employee_code, attendance_date)",
    "CREATE INDEX IF NOT EXISTS idx_attendance_date ON attendance_days("
    "attendance_date, status)",

    "CREATE INDEX IF NOT EXISTS idx_holidays_date ON hospital_holidays(holiday_date)",
    "CREATE INDEX IF NOT EXISTS idx_payroll_runs_period ON payroll_export_runs("
    "period_start, generated_at DESC)",
]


# ── Migration 25: diet orders, meal plans, kitchen indents ───────────────────
#
# A diabetic's tray and a renal patient's salt are part of their care, and a
# patient who is NIL BY MOUTH for theatre must not be handed breakfast. So this
# schema is written to make three things structurally impossible rather than
# merely discouraged:
#
#   1. AN ALLERGY CANNOT BE COPIED INTO A DIET ORDER. There is no allergy column
#      on `diet_orders` and there must never be one. `patient_allergies` is the
#      coded source of truth (database.py) and is what order-time allergy
#      checking already reads; a second copy on the diet order would be a list
#      that goes stale the moment a nurse records a new reaction, and a tray card
#      printed from the stale copy is a tray card that harms somebody. The
#      service joins the live table on every read. tests/test_dietary.py asserts
#      the ABSENCE of such a column, so adding one turns the suite red on purpose.
#
#   2. AN NPO ORDER CANNOT BE OPEN-ENDED. `CHECK (diet_type <> 'npo' OR
#      npo_until IS NOT NULL)` is a database constraint, not a validation the
#      service could forget. An NPO with no end time is how a patient stays
#      starved after a cancelled theatre list. The window is bounded and the
#      ORDERING CLINICIAN is on the same row (`ordered_by`), because "who said
#      this patient cannot eat, and until when" is the question the ward asks at
#      07:00 and the one a paper diet sheet answers worst.
#
#   3. A PATIENT CANNOT HOLD TWO ACTIVE DIET ORDERS. A partial unique index on
#      `(patient_id) WHERE status = 'active'` makes "the current diet" a fact the
#      database enforces rather than a query the reader hopes is right. A change
#      of diet SUPERSEDES: the old row moves to 'superseded' and a new row is
#      written, so what this patient was fed, and on whose word, survives.
#
# WHAT IS DELIBERATELY NOT MODELLED. There is no food/ration item master, no
# recipe, no bill of materials and no cost anywhere in these tables. A kitchen
# indent here is a MEAL-COUNT requisition — "cook 14 diabetic soft trays for
# Ward A lunch" — not a rations order priced against a vendor. Inventing a food
# master with invented quantities would produce a screen that looks like
# procurement and is not, and no rupee in this module means there is no second
# money implementation to keep in step with services/money.py. Ration costing is
# a followup, recorded rather than faked.
#
# Inherits migration 20's rules: NO FOREIGN KEYS (a dietary row must never be
# able to make a clinical write or a clinical schema change fail), and no REAL
# column exists. Additive throughout: five new tables, no existing table touched.

_DIETARY_TABLES: list[str] = [
    # ── The clinical order ────────────────────────────────────────────────────
    """CREATE TABLE IF NOT EXISTS diet_orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        patient_id TEXT NOT NULL,
        -- The admission this order was written against, for the record. The
        -- kitchen count does NOT trust it: it re-derives the patient's CURRENT
        -- admission on every read, so a discharge or a transfer is reflected
        -- immediately without anything having to update this column.
        admission_id TEXT,
        diet_type TEXT NOT NULL CHECK (diet_type IN (
            'normal','diabetic','renal','cardiac','soft','liquid','npo')),
        texture TEXT NOT NULL DEFAULT 'regular' CHECK (texture IN (
            'regular','soft','minced','pureed','liquid','thickened')),
        -- DIETARY restrictions only: low salt, vegetarian, no dairy by choice,
        -- fluid restriction. NOT ALLERGIES — see rule 1 above.
        restriction_notes TEXT,
        notes TEXT,
        -- NPO. Bounded by construction (CHECK below); the reason is required by
        -- the service, because "NPO" with no reason cannot be reviewed at 07:00.
        npo_reason TEXT,
        npo_until TIMESTAMP,
        status TEXT NOT NULL DEFAULT 'active' CHECK (status IN (
            'active','superseded','discontinued')),
        ordered_by TEXT NOT NULL,
        ordered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        supersedes_order_id INTEGER,
        superseded_at TIMESTAMP,
        superseded_by_order_id INTEGER,
        discontinued_by TEXT,
        discontinued_at TIMESTAMP,
        discontinue_reason TEXT,
        CHECK (diet_type <> 'npo' OR npo_until IS NOT NULL)
    )""",

    # ── The ward-wise plan for one service on one day ─────────────────────────
    """CREATE TABLE IF NOT EXISTS meal_plans (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        service_date DATE NOT NULL,
        meal_service TEXT NOT NULL CHECK (meal_service IN (
            'breakfast','lunch','dinner')),
        ward TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN (
            'draft','locked','cancelled')),
        -- total_meals is the sum of meal_plan_lines.meal_count, always. The
        -- service computes both from one Counter so they cannot disagree, and
        -- tests/test_dietary.py asserts total == sum(parts) on every plan it
        -- builds. NPO patients are NOT meals and are counted separately.
        total_meals INTEGER NOT NULL DEFAULT 0,
        npo_count INTEGER NOT NULL DEFAULT 0,
        -- Every active diet order this plan considered, meals + NPO alike, so
        -- total_meals + npo_count = order_count is the second invariant.
        order_count INTEGER NOT NULL DEFAULT 0,
        -- The diet_order ids the count was built from, as JSON. Order ids, never
        -- names: this is the audit answer to "what was this count built from"
        -- after a discharged patient dropped out of it, and it is stripped from
        -- every kitchen-facing payload.
        basis_order_ids_json TEXT,
        generated_by TEXT,
        generated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        locked_by TEXT,
        locked_at TIMESTAMP,
        cancelled_by TEXT,
        cancelled_at TIMESTAMP,
        notes TEXT
    )""",

    """CREATE TABLE IF NOT EXISTS meal_plan_lines (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        meal_plan_id INTEGER NOT NULL,
        line_no INTEGER NOT NULL,
        diet_type TEXT NOT NULL,
        texture TEXT NOT NULL DEFAULT 'regular',
        meal_count INTEGER NOT NULL DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""",

    # ── The requisition the kitchen cooks against ─────────────────────────────
    """CREATE TABLE IF NOT EXISTS kitchen_indents (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        indent_number TEXT,
        meal_plan_id INTEGER NOT NULL,
        service_date DATE NOT NULL,
        meal_service TEXT NOT NULL,
        ward TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN (
            'draft','issued','cancelled')),
        total_meals INTEGER NOT NULL DEFAULT 0,
        raised_by TEXT,
        raised_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        issued_by TEXT,
        issued_at TIMESTAMP,
        cancelled_by TEXT,
        cancelled_at TIMESTAMP,
        cancel_reason TEXT,
        notes TEXT
    )""",

    """CREATE TABLE IF NOT EXISTS kitchen_indent_lines (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        kitchen_indent_id INTEGER NOT NULL,
        line_no INTEGER NOT NULL,
        diet_type TEXT NOT NULL,
        texture TEXT NOT NULL DEFAULT 'regular',
        meal_count INTEGER NOT NULL DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""",
]

_DIETARY_INDEXES: list[str] = [
    # ONE active diet order per patient — the database's job, not the reader's.
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_diet_orders_one_active "
    "ON diet_orders(patient_id) WHERE status = 'active'",
    "CREATE INDEX IF NOT EXISTS idx_diet_orders_patient ON diet_orders("
    "patient_id, status, id DESC)",
    # The NPO sweep: "who on this ward cannot eat, and has their window passed".
    "CREATE INDEX IF NOT EXISTS idx_diet_orders_npo ON diet_orders("
    "diet_type, status, npo_until)",
    "CREATE INDEX IF NOT EXISTS idx_diet_orders_admission ON diet_orders(admission_id)",

    # One plan per ward per service per day.
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_meal_plans_unique ON meal_plans("
    "service_date, meal_service, ward)",
    "CREATE INDEX IF NOT EXISTS idx_meal_plans_date ON meal_plans("
    "service_date DESC, meal_service, ward)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_meal_plan_lines_no ON meal_plan_lines("
    "meal_plan_id, line_no)",

    "CREATE UNIQUE INDEX IF NOT EXISTS idx_kitchen_indents_number ON kitchen_indents("
    "indent_number) WHERE indent_number IS NOT NULL",
    # A plan may hold at most one live indent; cancelling one frees the slot.
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_kitchen_indents_live ON kitchen_indents("
    "meal_plan_id) WHERE status <> 'cancelled'",
    "CREATE INDEX IF NOT EXISTS idx_kitchen_indents_date ON kitchen_indents("
    "service_date DESC, meal_service, ward)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_kitchen_indent_lines_no ON kitchen_indent_lines("
    "kitchen_indent_id, line_no)",
]


def _m25_dietary(conn) -> None:
    """Diet orders, ward meal plans and the kitchen indent. The block comment
    above states the three invariants this schema enforces in the DATABASE rather
    than in a service that could forget them.

    NOT A CLINICAL WRITE PATH. Nothing here is read by medication ordering,
    administration, vitals, alerts, shadow capture or the audit chain, and no row
    in it can block a clinical action. A missing diet order does not stop a
    discharge; an unlocked meal plan does not stop anything at all. The one place
    this module touches clinical data is a READ of `patient_allergies`, and that
    read is fail-closed: an allergy list that cannot be read renders as "could not
    be read", never as "no known allergies".

    NO SEED DATA beyond the document-number series. Not one diet order, not one
    plan. A meal plan that ships with example counts is a count somebody cooks.
    """
    for statement in _DIETARY_TABLES:
        conn.execute(statement)
    for statement in _DIETARY_INDEXES:
        conn.execute(statement)
    # The kitchen indent joins the existing document-series machinery rather than
    # inventing a second numbering scheme (services/billing_masters.
    # next_document_number is the ONE allocator). `is_configured` is 0 for the
    # same reason it is 0 for INV/PO: "KIND-" is a placeholder prefix until a site
    # sets its own, and every screen that prints one has to be able to say so.
    conn.executemany(
        "INSERT OR IGNORE INTO document_number_series "
        "(series_code, document_type, prefix, next_number, is_configured) VALUES (?, ?, ?, 1, 0)",
        [("KIND", "kitchen_indent", "KIND-")],
    )


# ── Migration 26: NABH accreditation evidence pack ───────────────────────────

def _m26_nabh_evidence(conn) -> None:
    """The incident / adverse-event register, the discharge-disposition record
    and the evidence-pack log — what a hospital shows an NABH assessor.

    NABH accredits the HOSPITAL, not the software. Orb's job is to be the place
    the evidence already lives, so that "show me your medication-error data for
    the last quarter" is a date range rather than three weeks of file-pulling.

    FIVE THINGS THIS SCHEMA IS BUILT ON
    ───────────────────────────────────

    1. ANONYMITY IS A DATABASE CONSTRAINT, NOT A CODE PATH.
       ``CHECK (is_anonymous = 0 OR (reported_by IS NULL AND reporter_role IS
       NULL))``. A near-miss register a ward believes is attributable is a
       register that records nothing, so the guarantee has to survive somebody
       adding a convenience column later, or writing raw SQL. The ROLE is
       dropped as well as the id: "the night staff nurse on Ward B" is an
       identifier on a ward that has two of them.

       WHAT THIS SCHEMA CANNOT PROMISE, stated here because a promise nobody can
       keep is worse than none: services/audit_middleware.py records EVERY POST
       with the caller's user id and the request path. An administrator holding
       `audit.read` can therefore correlate "who posted to /api/nabh/incidents"
       against an anonymous report's timestamp. The service mitigates by storing
       ``reported_at`` truncated to the DAY for anonymous reports
       (``reported_at_precision = 'day'``), which is the only reason that column
       exists; it narrows the correlation, it does not remove it. The REGISTER
       is anonymous; the TRANSPORT is not yet.

    2. NO FOREIGN KEYS. NOT ONE — migration 20's rule, plus one reason of its
       own. ``PRAGMA foreign_keys=ON`` is set on every Orb connection and these
       tables point at `patients` and `users`. An FK child row makes the parent
       undeletable, and migration 14 shipped DPDP Act erasure: a quality
       register must never be the thing that blocks a lawful erasure request,
       and never the thing that makes a clinical schema rebuild fail. The cost
       of a dangling id here is a report that says "unknown patient".

    3. A VOIDED INCIDENT IS NOT A DELETED ONE. There is no DELETE path in this
       module. A duplicate or mistaken report moves to ``status = 'void'`` with
       a named actor and a reason, stays in the table, and is excluded from
       every indicator. An accreditation register that can lose rows is not a
       register.

    4. THE TRANSITION LOG IS A SEPARATE TABLE. ``nabh_incident_events`` is
       append-only with a per-incident ``seq``; the incident row is current
       state only. An assessor's question is "when was this investigated, and by
       whom" — a mutable status column cannot answer it.

    5. DISPOSITION IS RECORDED, NOT INFERRED. Orb held NO discharge disposition
       anywhere before this migration: `patients.status` knows only
       admitted/discharged/transferred, which cannot express a death or a LAMA.
       Mortality and LAMA are therefore not computable by cleverness over the
       existing tables — they need a recorded fact, and
       ``nabh_discharge_dispositions`` is that fact. It is an administrative
       record made by the records desk AFTER the clinical event; nothing in it
       sits on a clinical write path and nothing in it can block one.

    Additive throughout: 5 new tables, no existing table touched, no data moved.
    Two rows are added to `document_number_series` (migration 20's table) so the
    register and the evidence pack use the ONE document-numbering implementation
    instead of inventing a second.
    """
    for statement in _M26_TABLES:
        conn.execute(statement)
    for statement in _M26_INDEXES:
        conn.execute(statement)

    # Structural seed only — a reference series, no clinical or quality content.
    # `is_configured = 0`: the prefix is a placeholder until a site sets its own,
    # exactly as migration 20's series are.
    conn.executemany(
        "INSERT OR IGNORE INTO document_number_series "
        "(series_code, document_type, prefix, next_number, is_configured) VALUES (?, ?, ?, 1, 0)",
        [
            ("INC", "incident", "INC-"),
            ("NABHPACK", "evidence_pack", "NABH-"),
        ],
    )


# ── Migration 26 DDL ─────────────────────────────────────────────────────────

_M26_TABLES: list[str] = [
    """CREATE TABLE IF NOT EXISTS nabh_incidents (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        reference TEXT NOT NULL UNIQUE,
        incident_type TEXT NOT NULL CHECK (incident_type IN (
            'medication_error','adverse_drug_reaction','patient_fall',
            'hospital_acquired_infection','blood_transfusion_reaction',
            'wrong_patient_procedure','wrong_site_surgery','retained_item',
            'delay_in_care','diagnostic_error','equipment_failure',
            'documentation_error','consent_lapse','needle_stick',
            'patient_identification','specimen_error','security',
            'fire_or_safety','absconding','other')),
        -- NABH's harm ladder. 'near_miss' is FIRST on purpose: a register whose
        -- lightest category is "minor harm" tells a ward that near misses are
        -- not wanted, and near misses are the only free lesson a hospital gets.
        severity TEXT NOT NULL CHECK (severity IN (
            'near_miss','no_harm','minor','moderate','major','sentinel')),
        title TEXT NOT NULL,
        description TEXT NOT NULL,
        occurred_at TIMESTAMP NOT NULL,
        reported_at TIMESTAMP NOT NULL,
        -- 'day' for anonymous reports: a reporting TIMESTAMP is itself an
        -- identifier when the platform audit trail records who called the API.
        reported_at_precision TEXT NOT NULL DEFAULT 'exact'
            CHECK (reported_at_precision IN ('exact','day')),
        ward TEXT,
        department TEXT,
        location TEXT,
        -- OPTIONAL. Most incidents worth reporting are not about a nameable
        -- patient, and demanding one is a reason not to report.
        patient_id TEXT,
        is_anonymous INTEGER NOT NULL DEFAULT 0 CHECK (is_anonymous IN (0,1)),
        reported_by TEXT,
        reporter_role TEXT,
        status TEXT NOT NULL DEFAULT 'reported' CHECK (status IN (
            'reported','under_investigation','actions_pending','closed','void')),
        investigation_summary TEXT,
        root_cause TEXT,
        contributing_factors_json TEXT,
        investigated_by TEXT,
        investigated_at TIMESTAMP,
        -- Set only when an incident is closed with no corrective action. The
        -- column exists so that "we decided nothing was needed" is a recorded
        -- decision with a sentence attached, not an empty action list.
        no_action_reason TEXT,
        closed_by TEXT,
        closed_at TIMESTAMP,
        closure_note TEXT,
        reopened_count INTEGER NOT NULL DEFAULT 0,
        voided_by TEXT,
        voided_at TIMESTAMP,
        void_reason TEXT,
        externally_reportable INTEGER NOT NULL DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        -- THE ANONYMITY GUARANTEE. Enforced by SQLite so it survives raw SQL, a
        -- future column, and anybody who did not read the service.
        CHECK (is_anonymous = 0 OR (reported_by IS NULL AND reporter_role IS NULL))
    )""",

    """CREATE TABLE IF NOT EXISTS nabh_incident_actions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        incident_id INTEGER NOT NULL,
        action_type TEXT NOT NULL DEFAULT 'corrective' CHECK (action_type IN (
            'corrective','preventive','system_change','training','other')),
        description TEXT NOT NULL,
        owner_user_id TEXT,
        owner_description TEXT,
        due_date DATE,
        status TEXT NOT NULL DEFAULT 'open' CHECK (status IN (
            'open','completed','cancelled')),
        completed_at TIMESTAMP,
        completed_by TEXT,
        completion_note TEXT,
        cancelled_reason TEXT,
        created_by TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""",

    """CREATE TABLE IF NOT EXISTS nabh_incident_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        incident_id INTEGER NOT NULL,
        seq INTEGER NOT NULL,
        event_type TEXT NOT NULL,
        from_status TEXT,
        to_status TEXT,
        -- NULL for the 'reported' event of an anonymous report. Same guarantee
        -- as the parent row: there is no second copy of the name anywhere.
        actor_user_id TEXT,
        detail TEXT,
        metadata_json TEXT,
        occurred_at TIMESTAMP NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""",

    """CREATE TABLE IF NOT EXISTS nabh_discharge_dispositions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        patient_id TEXT NOT NULL,
        admission_id TEXT,
        -- 'lama' = the patient left against medical advice; 'dama' = discharged
        -- against medical advice with the paperwork completed. Indian hospitals
        -- distinguish them and NABH counts both, so both exist and the
        -- indicator reports one rate with the split shown.
        disposition TEXT NOT NULL CHECK (disposition IN (
            'routine','referred_out','transferred_out','lama','dama',
            'absconded','death','death_within_48h')),
        occurred_at TIMESTAMP NOT NULL,
        recorded_by TEXT,
        notes TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""",

    """CREATE TABLE IF NOT EXISTS nabh_evidence_exports (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        reference TEXT NOT NULL UNIQUE,
        period_start TEXT NOT NULL,
        period_end TEXT NOT NULL,
        ward TEXT,
        generated_by TEXT,
        generated_at TIMESTAMP NOT NULL,
        include_patient_identifiers INTEGER NOT NULL DEFAULT 0,
        indicator_count INTEGER NOT NULL DEFAULT 0,
        reported_count INTEGER NOT NULL DEFAULT 0,
        insufficient_count INTEGER NOT NULL DEFAULT 0,
        not_collected_count INTEGER NOT NULL DEFAULT 0,
        incident_count INTEGER NOT NULL DEFAULT 0,
        -- SHA-256 over the canonical JSON body. The assessor is handed a file;
        -- the hospital keeps the digest, so "is this the pack you gave us" is a
        -- question with an answer.
        content_sha256 TEXT NOT NULL,
        summary_json TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""",
]


_M26_INDEXES: list[str] = [
    "CREATE INDEX IF NOT EXISTS idx_nabh_incidents_occurred ON nabh_incidents("
    "occurred_at DESC, status)",
    "CREATE INDEX IF NOT EXISTS idx_nabh_incidents_type ON nabh_incidents("
    "incident_type, occurred_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_nabh_incidents_status ON nabh_incidents("
    "status, reported_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_nabh_incidents_ward ON nabh_incidents("
    "ward, occurred_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_nabh_incidents_patient ON nabh_incidents("
    "patient_id, occurred_at DESC)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_nabh_incident_events_seq "
    "ON nabh_incident_events(incident_id, seq)",
    "CREATE INDEX IF NOT EXISTS idx_nabh_incident_actions_incident "
    "ON nabh_incident_actions(incident_id, id)",
    "CREATE INDEX IF NOT EXISTS idx_nabh_incident_actions_open "
    "ON nabh_incident_actions(status, due_date)",
    # ONE disposition per discharge event. Recording the same discharge twice is
    # an UPDATE of this row, never a second row that doubles a mortality
    # denominator.
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_nabh_disposition_event "
    "ON nabh_discharge_dispositions(patient_id, occurred_at)",
    "CREATE INDEX IF NOT EXISTS idx_nabh_disposition_when "
    "ON nabh_discharge_dispositions(occurred_at DESC, disposition)",
    "CREATE INDEX IF NOT EXISTS idx_nabh_exports_generated "
    "ON nabh_evidence_exports(generated_at DESC)",
]


def _m27_surgery_timing(conn) -> None:
    """``surgeries.started_at`` / ``surgeries.completed_at`` — the two columns
    that make surgical turnaround a measurement instead of a claim.

    THE DEFECT (pilot dry run, 2026-08-14). Workflow 5 is named "surgical
    turnaround" and sold on it, and the table it runs against recorded no time
    at all: `scheduled_date` + `scheduled_time` are when the case was BOOKED and
    `created_at` is when the row was written. Nothing anywhere recorded when the
    knife went in or when the patient left the room, so the headline number the
    module exists to produce could not be computed from the database — not
    approximately, not at all.

    ADDITIVE AND NULLABLE. Existing rows get NULL, which reads as "not measured"
    everywhere (services/surgery_service.turnaround_minutes returns None), and
    every write path fills them going forward: POST /{id}/start sets
    ``started_at``, POST /{id}/complete sets ``completed_at``, and the post-op
    note compensation in routes/surgery_routes clears ``completed_at`` again
    when it reverts the status.

    NO BACKFILL, DELIBERATELY. There is no honest source: `created_at` is the
    booking, and `scheduled_time` is the plan, not the event — a case that ran
    two hours late would report a turnaround it never had. A first turnaround
    report assembled from invented timestamps is worse than an empty one,
    because an empty one is visibly empty. Historical cases stay NULL forever
    and the metric starts from the first case started after this migration.

    The index is on ``completed_at`` because every turnaround query is "cases
    that finished in this window" — the completion is the anchor, the start is
    read off the same row.
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info(surgeries)").fetchall()}
    if "started_at" not in cols:
        conn.execute("ALTER TABLE surgeries ADD COLUMN started_at TIMESTAMP")
    if "completed_at" not in cols:
        conn.execute("ALTER TABLE surgeries ADD COLUMN completed_at TIMESTAMP")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_surgeries_completed_at "
        "ON surgeries(completed_at DESC)"
    )


def _m28_patient_appointment_requests_bridge(conn) -> None:
    """``appointments.origin`` / ``appointments.department``, plus the rescue of
    every patient follow-up request that was written into a table nobody reads.

    THE DEFECT (pilot dry run, 2026-08-14). A patient asking for a follow-up
    (POST /api/bridge/appointments) wrote to ``patient_appointment_requests``.
    No staff route, service or screen has ever read that table — verified by
    grep across server/ and src/. The API answered "The care team will confirm a
    time" and nothing downstream of that sentence existed. Meanwhile the only
    appointment surface staff actually look at (the Appointments panel, fed by
    services/forecast_service.get_appointments) reads ``appointments`` for rows
    with status='booked'. Two tables, one of them a dead letter box, and the
    patient was on the wrong side of it.

    THE COLUMNS. ``origin`` ('staff' | 'patient') says who put the row on the
    list, so a patient's ASK is never mistaken for a slot the hospital agreed:
    the clinician list and the patient's own view both word themselves from it.
    ``department`` keeps the specialty the patient asked for, which the request
    carried and the appointment row had nowhere to put. Both additive; existing
    rows read as staff-booked with no department, which is what they are.

    THE BACKFILL, DELIBERATELY. Unlike a metric with no honest source, these
    rows ARE the source: each one is a real request a real patient made and was
    promised an answer to. Copying them across is the only way that promise gets
    kept for anyone who already asked. The request id is reused as the
    appointment id, which makes the copy idempotent (INSERT OR IGNORE) and keeps
    the patient's existing cancel link working. A request whose patient row is
    gone is skipped (the FK would reject it anyway). 'cancelled' requests are
    carried over as cancelled rather than dropped, so the patient's own history
    does not silently lose entries when their list switches tables.

    A request that named no time is listed at the first clinic slot, and the
    reason text SAYS that is what happened — the same wording the patient is
    given. See routes/appointments_routes.FIRST_CLINIC_SLOT.
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info(appointments)").fetchall()}
    if "origin" not in cols:
        conn.execute("ALTER TABLE appointments ADD COLUMN origin TEXT NOT NULL DEFAULT 'staff'")
    if "department" not in cols:
        conn.execute("ALTER TABLE appointments ADD COLUMN department TEXT")

    legacy = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='patient_appointment_requests'"
    ).fetchone()
    if not legacy:
        return

    rows = conn.execute(
        """SELECT r.id, r.patient_id, r.requested_date, r.requested_time,
                  r.department, r.reason, r.status, r.created_at, p.user_id
           FROM patient_appointment_requests r
           JOIN patients p ON p.id = r.patient_id"""
    ).fetchall()

    carried = 0
    for r in rows:
        req_id, patient_id, req_date, req_time, dept, reason, status, created_at, user_id = (
            r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7], r[8]
        )
        if not req_date:
            continue
        slot = (req_time or "").strip()
        note = None
        if not slot:
            slot = "09:00"
            note = "Patient did not choose a time; listed at the first clinic slot."
        parts = ["Patient-requested follow-up."]
        if reason:
            parts.append(str(reason))
        if note:
            parts.append(note)
        cur = conn.execute(
            """INSERT OR IGNORE INTO appointments
                   (id, patient_id, booked_by_user_id, scheduled_date, scheduled_time,
                    appointment_type, reason, status, created_at, origin, department)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'patient', ?)""",
            (
                req_id,
                patient_id,
                user_id,
                req_date,
                slot,
                "Patient request: confirm time",
                " ".join(parts)[:2000],
                "cancelled" if status == "cancelled" else "booked",
                created_at,
                dept,
            ),
        )
        carried += max(cur.rowcount or 0, 0)
    if carried:
        logger.info("Carried %d patient appointment request(s) into appointments", carried)


def _m29_sage_assessments(conn) -> None:
    """``sage_assessments`` + ``sage_assessment_events`` — the DRAFT Sage writes
    when Vigil pages, and the record of what a clinician did with it.

    WHY A TABLE AND NOT A MESSAGE. Until now the only Sage output on a critical
    alert was one sentence of 4B prose appended to the Relay alert TEXT
    (``bot_service.evaluate_and_dispatch_alerts``). Text in a message body has
    no state, so there was nothing to approve, nothing to decline, and no way to
    say "this is still being written" or "this could not be produced" — the
    three things a clinician most needs to know about a machine-authored
    assessment. A row has a state; a sentence does not.

    THE STATE MACHINE, and why `unavailable` is a first-class state rather than
    an absent row:
        generating  → the model is running. The row exists from the instant the
                      alert is on screen, so the surface can say "being written"
                      truthfully instead of guessing from an absence.
        ready       → an assessment was produced.
        unavailable → generation was shed, timed out, was evicted or failed.
                      ``unavailable_reason`` says which, and ``fallback_text``
                      carries the EXISTING deterministic sentence. This is the
                      state that makes a spinner honest: a surface that only
                      knew "no row yet" would spin forever on a night the model
                      was down, implying something is coming when nothing is.
        approved /
        declined    → a clinician read the draft and said so. THE DECLINE IS THE
                      VALUABLE ROW (same argument as sage_actions): everything
                      else in Orb records agreement.

    NOTHING IN THIS TABLE EXECUTES. Approving an assessment records a reading,
    not an order. Anything Sage suggests that Orb could actually DO is written
    as a ``sage_actions`` card, which keeps the existing approve/decline route,
    the per-action confirm lock and the medication safety gate. The join is
    ``sage_actions.source_msg_id`` = this row's ``message_id`` plus
    ``sage_assessment_actions``' explicit link below, so a card can always be
    traced back to the assessment that proposed it.

    ``news2`` / ``news2_drivers`` are stored as Orb's DETERMINISTIC values, not
    the model's. They are written before generation starts and are never
    overwritten from model output — the column is the answer to "what did the
    engine say?", and a model that disagrees is wrong by construction (the
    NEWS2 7-vs-11 incident, one layer further out).

    ``version`` mirrors the optimistic-locking convention ``sage_actions``
    already uses: run_db is a four-thread pool, so a read-then-write is resolved
    in SQL or not at all.
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sage_assessments (
            id                 TEXT PRIMARY KEY,
            patient_id         TEXT NOT NULL,
            channel_id         TEXT,
            message_id         INTEGER,
            episode_id         TEXT,
            lane               TEXT NOT NULL DEFAULT 'simulator',
            state              TEXT NOT NULL DEFAULT 'generating'
                               CHECK (state IN ('generating','ready','unavailable',
                                                'approved','declined')),
            unavailable_reason TEXT,
            news2              INTEGER,
            news2_level        TEXT,
            news2_drivers      TEXT,
            summary            TEXT,
            considerations     TEXT,
            evidence           TEXT,
            watch_items        TEXT,
            citation_basis     TEXT,
            confidence         TEXT,
            model              TEXT,
            conversation_id    TEXT,
            fallback_text      TEXT,
            ttft_ms            INTEGER,
            total_ms           INTEGER,
            version            INTEGER NOT NULL DEFAULT 1,
            created_at         TEXT NOT NULL,
            updated_at         TEXT NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sage_assessments_channel "
                 "ON sage_assessments(channel_id, created_at DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sage_assessments_patient "
                 "ON sage_assessments(patient_id, created_at DESC)")
    # One assessment per Relay alert message. A retry, a restart race or a
    # double publish must not produce two drafts for one alert — a clinician
    # reading two machine assessments of the same deterioration has no way to
    # tell which is current. Partial (message_id IS NOT NULL) because a lane
    # that raised no Relay message still deserves its row.
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_sage_assessments_once "
                 "ON sage_assessments(message_id) WHERE message_id IS NOT NULL")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS sage_assessment_events (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            assessment_id TEXT NOT NULL REFERENCES sage_assessments(id),
            prior_state   TEXT,
            new_state     TEXT NOT NULL,
            actor_id      TEXT,
            actor_name    TEXT,
            actor_role    TEXT,
            reason        TEXT,
            source        TEXT NOT NULL DEFAULT 'ui'
                          CHECK (source IN ('ui','system')),
            created_at    TEXT NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sage_assessment_events_assessment "
                 "ON sage_assessment_events(assessment_id, created_at DESC)")

    # The explicit proposal link. `sage_actions` already carries room_id +
    # source_msg_id, but source_msg_id means "the Relay message this was
    # extracted from" on the ambient lane, and reusing it for "the assessment
    # that proposed this" would give one column two meanings. A join table keeps
    # both truths and lets a card be traced to its assessment even after the
    # message is archived.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sage_assessment_actions (
            assessment_id TEXT NOT NULL REFERENCES sage_assessments(id),
            action_id     TEXT NOT NULL REFERENCES sage_actions(id),
            created_at    TEXT NOT NULL,
            PRIMARY KEY (assessment_id, action_id)
        )
    """)


def _m30_sage_assessment_tiers(conn) -> None:
    """Split the drafted assessment into TIERS, because 45 seconds was useless.

    THE MEASUREMENT THAT FORCED THIS. The four-section prose assessment was
    timed end to end on 2026-08-14 at 42-57 s (median 46 s), first words
    3.3-12.7 s. At a deteriorating bedside that is past the point where a second
    opinion changes anything. The diagnosis was a DESIGN error, not a model one:
    the most clinically useful section — which NEWS2 parameters are scoring and
    which way they are moving — is arithmetic Orb ALREADY performs
    deterministically, and it had been handed to a language model to write out
    in sentences. Length is latency on this appliance (~10-15 tok/s decode), so
    prose the engine could have rendered instantly WAS the 45 seconds.

    ``tier1`` — the deterministic driver/trend block, JSON, computed by
    ``vigil_service.compute_news2``'s own per-parameter breakdown and written in
    the SAME INSERT that creates the row, before any model work starts. It
    cannot fail, cannot hallucinate and cannot be late; a dead model, a shed
    queue and an evicted tier all leave it standing. It is nullable only because
    a patient with no scoreable observation genuinely has no breakdown to show —
    in which case the block itself says so.

    ``depth`` — 'brief' (the alert lane: at most three prioritised next steps,
    hard token cap, streamed) or 'full' (the four-section reasoned assessment,
    which is NOT deleted: it keeps its value on the I-PASS handover and the ward
    round, where a clinician is reviewing rather than reacting and 45 s is
    invisible). Existing rows are backfilled 'full' because that is what they
    are; every new row states its own depth.

    ``first_step_ms`` — time from generation start to the first COMPLETE step
    reaching the surface. ``ttft_ms`` measures the first token, which nobody can
    read; this measures the first thing a clinician can act on, and it is the
    number the pilot's latency claim has to be made of.

    ``advice_note`` — a ward-facing sentence for a draft that is READY but
    incomplete: the generation ran out of time after two of its three steps had
    already landed. Those steps are whole, sanitized and screened, so throwing
    them away would take back something the clinician had already read; showing
    them silently would let a truncated list read as a finished one. The column
    is how the surface can say "these are the ones that arrived".
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info(sage_assessments)").fetchall()}
    if "tier1" not in cols:
        conn.execute("ALTER TABLE sage_assessments ADD COLUMN tier1 TEXT")
    if "depth" not in cols:
        conn.execute("ALTER TABLE sage_assessments ADD COLUMN depth TEXT")
        conn.execute("UPDATE sage_assessments SET depth = 'full' WHERE depth IS NULL")
    if "first_step_ms" not in cols:
        conn.execute("ALTER TABLE sage_assessments ADD COLUMN first_step_ms INTEGER")
    if "advice_note" not in cols:
        conn.execute("ALTER TABLE sage_assessments ADD COLUMN advice_note TEXT")


def _m31_sage_patient_specific(conn) -> None:
    """The advice tier's OWN payload, and the key the repeat-suppressor compares.

    TWO COLUMNS, BOTH ADDITIVE, AND NEITHER OF THEM DERIVABLE FROM WHAT WAS
    ALREADY THERE.

    ``patient_specific`` — the whole output of the patient-specific tier, as
    JSON: the outcome (`flags` / `no_candidates` / `no_material` / `failed`),
    whether the model was called at all, the filter's drop counts, and per flag
    the CAUTION, the CHART FACT behind it, which section of the chart holds that
    fact, when it was charted and which scoring parameter it bears on.

    WHY IT HAD TO BE A COLUMN. The surface has a purpose-built block for this —
    it separates the fact from the judgement, boxes the fact under "From this
    chart", and renders silence as a settled clinical finding rather than an
    empty slot. Measured 2026-08-15 it had NEVER RENDERED IN PRODUCTION: nothing
    emitted the key, so `patientSpecificView` resolved to `absent` on 100 of 100
    rows and the block was unreachable code. What shipped instead was the legacy
    considerations list wearing the new content — the raw chart fact as the bold
    primary line, the judgement dimmer beneath it, no "from this chart"
    labelling, and the prompt's internal "F1." fact id leaking onto a ward
    screen. The information the block needs is not recoverable from
    `considerations`: that column holds the model's sentence and nothing else,
    while the fact, its section, its date and its parameter live in the
    deterministic pre-screen and were being thrown away at the end of the
    generation.

    ``screen_signature`` — WHICH CHART FACTS WERE IN PLAY, from
    `patient_specific_screen.flag_signature`: the rule and the fact, normalised
    and sorted, with the deterioration deliberately left out.

    WHY THAT IS A SECOND COLUMN AND NOT A FIELD INSIDE THE FIRST. It is written
    in the INSERT that OPENS the draft, before any model work, because the thing
    it exists to prevent has to be preventable while a generation is still in
    flight. `patient_specific` cannot be written that early: its outcome is not
    known until the tier resolves, and a `generating` row carrying a provisional
    outcome would render on the ward as a finished answer.

    WHAT IT PREVENTS: Suresh Reddy (PAT-A20FA685) received the identical
    Ceftriaxone-allergy line ELEVEN TIMES IN TWENTY-ONE MINUTES, with thirteen
    unreviewed drafts stacked behind it. The existing redraft debounce was
    working exactly as specified and was powerless — it compares NEWS2 pictures,
    and the score genuinely oscillated 8→14 and back every ninety seconds, so
    "NEWS2 has moved materially" fired over and over on a patient whose CHART had
    not changed at all. An allergy is a permanent fact; it does not become newly
    true because a respiratory rate moved. Comparing signatures is what lets Orb
    tell a repeat from a new finding, and a new drug, a new allergy or a
    first-time renal trigger changes the string and always surfaces.

    NULLABLE, AND THE NULL MEANS SOMETHING. NULL `screen_signature` is "Orb could
    not screen this chart" (or a row written before this migration), and the
    suppressor treats it as not-comparable and drafts. The EMPTY STRING is the
    real, common answer — "Orb looked and there is nothing in play" — and two
    empty strings compare equal. Distinguishing them is the same rule
    `NO_FACTORS_SCREEN_FAILED` draws one file over: a failed check is not a clean
    one.
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info(sage_assessments)").fetchall()}
    if "patient_specific" not in cols:
        conn.execute("ALTER TABLE sage_assessments ADD COLUMN patient_specific TEXT")
    if "screen_signature" not in cols:
        conn.execute("ALTER TABLE sage_assessments ADD COLUMN screen_signature TEXT")
    # The suppressor's read: newest reusable draft for one patient at one depth.
    # Same (patient_id, depth, created_at) shape `_alert_draft_decision` already
    # uses, so this index serves both the debounce and the new gate.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_sage_assessments_patient_depth_created "
        "ON sage_assessments(patient_id, depth, created_at DESC)")


def _m32_allergy_override_attribution(conn) -> None:
    """Make ``medication_allergy_overrides`` answer an incident review on its own.

    The override ledger already recorded WHO (``overridden_by``), WHEN
    (``overridden_at``), WHY (``reason``) and WHICH ALLERGY (``alerts_json``).
    Two things an incident review always asks for were only reachable by JOIN,
    and a JOIN is not a record — it is a re-derivation from rows that keep
    changing.

    ``drug_name`` — WHAT WAS GIVEN. It lived only in ``medications.drug_name``
    via ``medication_id``. A medication row is editable and dischargeable; the
    ward routinely corrects a charted name after the fact. Reviewing an override
    six months later and reading the CURRENT name of the order (or NULL, if the
    row was cleaned up) is how a ledger quietly stops being evidence. The drug
    as it was named ON THE DOSE is now written into the row.

    ``overridden_by_role`` — WITH WHAT AUTHORITY. The override became
    prescriber-only on 2026-08-15 (permissions.MEDICATION_PRESCRIBE). The
    question a review asks is not only "which account" but "was that account
    entitled to", and ``users.role`` is mutable — a doctor who later becomes an
    administrator would rewrite the history of every override they ever
    authorised. Stamped at the time of the decision instead.

    Both columns are additive and nullable. NULL means "written before this
    migration", which is honest; back-filling from today's ``medications`` and
    ``users`` rows would manufacture exactly the false confidence the columns
    exist to remove.
    """
    cols = {r[1] for r in conn.execute(
        "PRAGMA table_info(medication_allergy_overrides)").fetchall()}
    if "drug_name" not in cols:
        conn.execute(
            "ALTER TABLE medication_allergy_overrides ADD COLUMN drug_name TEXT")
    if "overridden_by_role" not in cols:
        conn.execute(
            "ALTER TABLE medication_allergy_overrides ADD COLUMN overridden_by_role TEXT")


def _rebuild_users_role_check(conn, allowed_roles: tuple) -> None:
    """Rebuild ``users`` so its role CHECK permits exactly ``allowed_roles``.

    SQLite cannot ALTER a CHECK constraint, so widening the role list is a
    table rebuild — and ``users`` is the FK parent of ~20 tables, which makes
    this the one rebuild that CANNOT run inside the runner's transaction.
    Both in-transaction shapes were tried and fail (empirically, on stdlib
    sqlite3 and sqlcipher3 both — scratch probe 2026-08-19):

      * create-new → copy → DROP old → rename, under
        ``PRAGMA defer_foreign_keys=ON``: the implicit DELETE inside
        ``DROP TABLE users`` registers one deferred violation per child row,
        and nothing can ever credit them back — the deferred counter is only
        decremented by DML that supplies a missing parent, and the parent
        returns via RENAME, which the counter does not see. COMMIT fails.
      * rename-aside → create final → copy → drop aside, the messages/
        surgeries shape: it needs ``PRAGMA legacy_alter_table=ON`` so the
        rename does NOT rewrite child REFERENCES, and that pragma — like
        ``foreign_keys`` — is silently a NO-OP inside an open transaction.
        The rename follows modern semantics, children get rewritten to the
        aside name, and the aside drop hits an immediate FK violation.

    So this delegates to database._atomic_table_rebuild, which owns an
    autocommit window (both pragmas issued OUTSIDE any transaction) and makes
    the rebuild itself all-or-nothing. The cost, stated honestly: entering
    that window COMMITs the runner's pending work (any migrations applied
    earlier in this batch land early). That is safe here because every
    migration in this file is idempotent by contract — a crash between the
    rebuild's commit and the ledger write means the next boot re-enters this
    function, the DDL guard sees the widened CHECK, and it no-ops while the
    ledger catches up.

    Afterwards it RE-OPENS a transaction if one was open at entry, so the
    runner's invariants hold: the ledger INSERT for this migration (and any
    later pending migrations) run transactionally, and run_pending_migrations'
    own final COMMIT still has a transaction to commit.

    Callers FREEZE their role list (a literal tuple, never
    roles.STORABLE_ROLES): a shipped migration must produce the same schema
    forever. The parity test in tests/test_migrations.py fails the suite when
    STORABLE_ROLES outgrows the newest frozen list — the prompt to add the
    next three-line migration calling this helper.

    Idempotent: no-ops when the stored DDL already permits every requested
    role.
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='users'"
    ).fetchone()
    ddl = (row[0] or "") if row else ""
    if all(f"'{r}'" in ddl for r in allowed_roles):
        return
    # Anything hanging off the table drops with it. users carries no indexes
    # or triggers today (the TEXT PRIMARY KEY's autoindex recreates itself);
    # if one appears later, fail loudly rather than silently drop it, and
    # teach this migration to carry it across.
    assoc = conn.execute(
        "SELECT name, type FROM sqlite_master WHERE tbl_name='users' "
        "AND type IN ('index', 'trigger') AND name NOT LIKE 'sqlite_autoindex%'"
    ).fetchall()
    if assoc:
        raise RuntimeError(
            "users rebuild would drop associated objects it does not know how "
            f"to preserve: {[(r[0], r[1]) for r in assoc]}"
        )
    # Copy the INTERSECTION by name (the surgeries discipline): a canonical
    # column the live table lacks stays at its DEFAULT; a live column this
    # DDL doesn't know aborts rather than silently vanishing.
    canonical = [
        "id", "name", "role", "department", "registration_number",
        "password_hash", "pin_hash", "created_at", "failed_login_attempts",
        "login_locked_until", "totp_secret", "totp_enabled", "token_version",
        "totp_last_step", "is_active", "device_pin_hash", "device_pin_set_at",
        "device_pin_failed_attempts", "device_pin_locked_until",
    ]
    old_cols = [r[1] for r in conn.execute("PRAGMA table_info(users)").fetchall()]
    unknown = [c for c in old_cols if c not in canonical]
    if unknown:
        raise RuntimeError(f"users rebuild would drop live column(s) {unknown}")
    shared = ", ".join(c for c in canonical if c in old_cols)
    role_list = ", ".join(f"'{r}'" for r in allowed_roles)
    was_in_txn = bool(getattr(conn, "in_transaction", False))
    from database import _atomic_table_rebuild  # lazy: mirrors database→migrations

    # The full canonical users shape at the point migrations run: base SCHEMA
    # columns first, then every column the init_db additive blocks and earlier
    # migrations append, in historical order — so a rebuilt table and a
    # fresh-then-altered one agree column-for-column.
    _atomic_table_rebuild(
        conn,
        table="users",
        shared_cols=shared,
        create_new_sql=f"""CREATE TABLE users_new (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            role TEXT NOT NULL CHECK (role IN ({role_list})),
            department TEXT,
            registration_number TEXT,
            password_hash TEXT NOT NULL,
            pin_hash TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            failed_login_attempts INTEGER DEFAULT 0,
            login_locked_until REAL,
            totp_secret TEXT,
            totp_enabled INTEGER DEFAULT 0,
            token_version INTEGER DEFAULT 0,
            totp_last_step INTEGER DEFAULT -1,
            is_active INTEGER DEFAULT 1,
            device_pin_hash TEXT,
            device_pin_set_at TIMESTAMP,
            device_pin_failed_attempts INTEGER DEFAULT 0,
            device_pin_locked_until REAL
        )""",
        post_sql=[],
    )
    # Restore the runner's world: it handed us an open transaction and its
    # ledger INSERT + final COMMIT assume one is still there.
    if was_in_txn and not getattr(conn, "in_transaction", False):
        conn.execute("BEGIN IMMEDIATE")


# Frozen copy of roles.STORABLE_ROLES as of the NEWEST users-rebuild migration
# (see _rebuild_users_role_check on why migrations freeze their list).
# Reassigned by each newer rebuild migration; the parity test compares it to
# STORABLE_ROLES so the list cannot grow without a migration to carry it.
LATEST_USERS_ROLE_CHECK: tuple = (
    "doctor", "nurse", "admin", "patient", "pharmacist", "cashier",
)


def _m33_users_role_cashier(conn) -> None:
    """Admit 'cashier' into users.role. The 2026-08-18 payments split minted
    the role in permissions.ROLE_PERMISSIONS, but the CHECK predated it — and
    the ``INSERT OR IGNORE`` seeding idiom swallows a CHECK violation
    silently, so a cashier account never landed and its token 401'd against a
    row that did not exist. roles.py is now the single authority all
    renderings of the list share."""
    _rebuild_users_role_check(
        conn, ("doctor", "nurse", "admin", "patient", "pharmacist", "cashier")
    )


def _m34_minority_retention_authority(conn) -> None:
    """State the minority protection in `retention_policies.authority`.

    THE RULE ITSELF IS ENFORCED IN CODE, not by this row, and the split is
    deliberate. `retention_policies` is keyed by CATEGORY; whether a record
    belongs to a child is a property of the PATIENT, so it cannot be expressed
    as a day count in this table without inventing a second category that maps
    to the same tables and double-counts every paediatric record in the
    retention review. consent_service.retention_review applies it as a floor
    over the top (see config.MINOR_RETENTION_AGE).

    What this migration fixes is that the encoded position did not SAY so. A
    hospital reading the authority for `clinical_record` saw a flat three years
    and no mention that a paediatric record outlives it by up to eighteen. The
    retention register is a document the hospital's counsel reads; a rule
    enforced in code that the register does not state is a rule nobody can
    audit.

    UPDATE, not INSERT OR IGNORE: these rows already exist on every deployment
    that has run migration 14, and the seed's INSERT OR IGNORE would skip them
    silently. Guarded on the text not already being present so a re-run is a
    no-op, and scoped to rows a site has NOT edited away from the seeded
    wording, because retention_policies is deliberately mutable and overwriting
    a site's own counsel-approved text would be exactly the wrong thing to do.
    """
    minor = (
        " A patient who was a MINOR is protected for longer: Limitation Act "
        "1963 s.6 starts the limitation period at majority, so the window "
        "closes at 18+3=21. Enforced as a floor by "
        "consent_service.retention_review, not by this row."
    )
    for category, anchor_text in (
        ("clinical_record", "Consumer Protection Act 2019 window."),
        ("prescription_and_dispensing", "dispensing records retained under Rule 65."),
        ("imaging", "record period as the safer single rule."),
    ):
        conn.execute(
            """UPDATE retention_policies
                  SET authority = authority || ?
                WHERE category = ?
                  AND authority LIKE ?
                  AND authority NOT LIKE '%Limitation Act 1963 s.6%'""",
            (minor, category, f"%{anchor_text}"),
        )


def _m35_ward_receipt(conn) -> None:
    """Give the pharmacy chain a real last step: the ward says the drug arrived.

    TWO DEFECTS, ONE ROW SHAPE.

    1. NOBODY RECEIVED ANYTHING. ``dispensed -> delivered`` was gated on
       ``helix.write``, which only the pharmacist and an administrator hold. The
       same desk that sent the drug recorded that the ward got it, and the row
       kept no trace of who took delivery or when. A custody trail whose last
       hop is attested by the sender is not a custody trail. ``received_by`` and
       ``received_at`` are the ward's own attestation, and the route that writes
       them (``POST /helix/orders/{id}/receive``) is gated on the new
       ``helix.receive``, which the pharmacist deliberately does not hold.

    2. NO DISPENSED DRUG WAS EVER BILLABLE. ``helix_service`` set
       ``is_mock_event = 1`` on EVERY transition into ``dispensed`` and
       ``delivered`` — unconditionally, since the column was added for a
       demonstration script that no longer exists. ``billing_service`` then
       excluded exactly those rows (``IFNULL(o.is_mock_event, 0) = 0``). So the
       pharmacy revenue line was structurally zero: every real dispense flagged
       itself as simulator output on the way out of the door, and the charge was
       dropped in silence. Nothing else in the codebase ever wrote the column.

       It is dropped rather than left at 0. A column named ``is_mock_event``
       sitting in a clinical schema invites exactly one future bug, which is the
       one it already caused: something starts setting it again.

    Both columns are additive and nullable. NULL ``received_by`` on a
    ``delivered`` row means "delivered before this migration", which is honest —
    back-filling the dispensing pharmacist as the receiver would manufacture the
    very attestation the column exists to record.

    DROP COLUMN is safe here: ``is_mock_event`` carries no index, appears in no
    CHECK, and is not part of any FK. The three tables that reference
    ``helix_patient_med_orders`` reference ``id``, so no rebuild is involved and
    the FK-parent rebuild trap (see ``_rebuild_users_role_check``) does not
    apply. Guarded anyway: an appliance on an older SQLite keeps the column, and
    the code no longer writes or reads it either way.
    """
    # The Helix tables are created lazily by services/helix_db.init_helix_db(),
    # which runs OUTSIDE this runner — so on a fresh database this migration
    # executes before they exist. That is not a problem to work around: the
    # fresh schema in helix_db.py already carries `received_by`/`received_at`
    # and already lacks both mock columns, so there is genuinely nothing to do.
    # PRAGMA table_info on a missing table returns an empty list rather than
    # raising, which would make every check below look like "column missing" and
    # send an ALTER at a table that is not there. Ask sqlite_master instead.
    def _table(name: str) -> bool:
        return conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone() is not None

    if not _table("helix_patient_med_orders"):
        return

    cols = {r[1] for r in conn.execute(
        "PRAGMA table_info(helix_patient_med_orders)").fetchall()}

    if "received_by" not in cols:
        conn.execute(
            "ALTER TABLE helix_patient_med_orders ADD COLUMN received_by TEXT "
            "REFERENCES users(id)")
    if "received_at" not in cols:
        conn.execute(
            "ALTER TABLE helix_patient_med_orders ADD COLUMN received_at TIMESTAMP")

    if "is_mock_event" in cols:
        try:
            conn.execute(
                "ALTER TABLE helix_patient_med_orders DROP COLUMN is_mock_event")
        except Exception:
            # Pre-3.35 SQLite. The column stays and stays at its 0 default:
            # helix_service no longer writes it and billing_service no longer
            # reads it, so it is inert rather than load-bearing.
            logger.info(
                "migration 35: DROP COLUMN unsupported, leaving is_mock_event "
                "in place (inert: nothing reads or writes it)")

    # Same story one table over: `helix_stock_movements.is_mock` DEFAULTED TO 1,
    # so every real FIFO decrement recorded itself as simulator output. Nothing
    # ever read it, which is the only reason it caused no second billing defect.
    if not _table("helix_stock_movements"):
        return

    mv_cols = {r[1] for r in conn.execute(
        "PRAGMA table_info(helix_stock_movements)").fetchall()}
    if "is_mock" in mv_cols:
        try:
            conn.execute(
                "ALTER TABLE helix_stock_movements DROP COLUMN is_mock")
        except Exception:
            logger.info(
                "migration 35: DROP COLUMN unsupported, leaving is_mock in "
                "place (inert: nothing reads or writes it)")

    # Historical rows written by the old code name themselves "mock_dispense".
    # They ARE real dispenses — the string described the demo script the code was
    # written for, not the event — so they are renamed rather than deleted.
    conn.execute(
        "UPDATE helix_stock_movements SET reason = 'dispense' "
        "WHERE reason = 'mock_dispense'")


def _m36_pharmacy_is_not_a_ward(conn) -> None:
    """Un-scope the pharmacist. `users.department = 'Pharmacy'` -> NULL.

    THE DEFECT. `permissions.enforce_patient_scope` compares the caller's
    `users.department` against the patient's ADMISSION department. Nobody is ever
    admitted to "Pharmacy", so a pharmacist seeded with that department matched
    no patient in the hospital and every patient-scoped route answered 404
    "Patient not found" — including `POST /api/helix/orders/approve`, the route a
    pharmacist uses to raise an order at all. Demonstrated live 2026-09-07
    against the seeded ward.

    THIS EXACT BUG WAS ALREADY FIXED ONCE. ADM001 was changed from
    "Administration" to NULL on 2026-08-12, with a comment in
    scripts/seed_clinical_data.py explaining that a governance account scoped to
    a ward with no patients "is not a safe default, it is an invisible one".
    PHA001 sat one line below it and was missed. Both `routes/helix_routes.py`
    and `routes/payments_routes.py` already state in prose that a hospital-wide
    account is seeded with NO department; the seed was the only thing that
    disagreed with them.

    SCOPED NARROWLY, on purpose. Only rows that are BOTH `role = 'pharmacist'`
    AND `department = 'Pharmacy'` — the exact broken combination. A site that has
    deliberately attached a pharmacist to a real clinical ward keeps that
    department, because there the scope is meaningful and a widening would be a
    silent grant. "Pharmacy" is safe to clear because it can never match an
    admission: `AdmissionRequest.department` is a clinical department and no
    admission path writes "Pharmacy".

    NOT A WIDENING ANYWHERE ELSE. The one surface where a blank department used
    to mean "see everything" was Relay's department-channel filter, which matched
    by substring and so let `""` match every room. That is closed in the same
    change (services/relay_service.py): no department now means no department
    room.
    """
    if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='users'"
    ).fetchone() is None:
        return

    cur = conn.execute(
        "UPDATE users SET department = NULL "
        " WHERE role = 'pharmacist' AND department = 'Pharmacy'"
    )
    if getattr(cur, "rowcount", 0) > 0:
        logger.info(
            "migration 36: un-scoped %s pharmacist account(s) that were "
            "department='Pharmacy' and could therefore reach no patient",
            cur.rowcount,
        )


def _m37_system_account_has_no_ward(conn) -> None:
    """`users.department` on the 'system' audit identity: 'IT' -> NULL.

    THIRD ACCOUNT, SAME TRAP. No ward is called "IT", so the value could only
    ever mean "scoped to a department nobody is admitted to" — exactly what made
    ADM001 blind to every governance surface (fixed 2026-08-12) and PHA001 unable
    to reach a single patient (migration 36, hours ago). 'system' is Orb Sage's
    AUDIT IDENTITY: its placeholder hash `system_hash` can never verify against
    any password, so no request ever authenticates as it and no scope is ever
    derived for it. That is why this one was never a live defect and is corrected
    anyway — a column that is wrong the moment anything reads it is a trap left
    armed, and two of the three accounts carrying it turned out to be live bugs.

    BOTH WRITERS USE `INSERT OR IGNORE`, so neither database.py's seed nor
    services/sage_assessment_service.py's re-create touches a row that already
    exists. Without this migration every database created before today keeps
    'IT' forever while new ones get NULL — the two writers agreeing while the
    estate diverges, which is the shape of the billing defect in migration 35.

    Matched on id AND the exact value, so a deployment that has deliberately put
    something else there is left alone.
    """
    if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='users'"
    ).fetchone() is None:
        return

    cur = conn.execute(
        "UPDATE users SET department = NULL WHERE id = 'system' AND department = 'IT'"
    )
    if getattr(cur, "rowcount", 0) > 0:
        logger.info(
            "migration 37: cleared the 'system' audit identity's department "
            "('IT' was never a ward)"
        )


def _m38_retire_unknown_models(conn) -> None:
    """Drop `model_registry` rows for AI models Orb no longer ships.

    THE DEFECT. `model_governance_service.seed_default_models()` ends with a
    DELETE written for exactly this purpose, and its own comment says why: "a
    lingering row would over-claim in the governance UI and in the compliance
    export, which is the exact failure this registry exists to prevent." But the
    only caller on a normal read path is `list_models()`, and it invokes the
    seeder ONLY when the registry is empty (`if n == 0`). On any database that
    already holds rows the DELETE never runs, so a retired model is served
    forever and the safeguard never fires on the estate it was written for.

    MEASURED, not theorised. `sage_llm_hosted`, a HOSTED off-appliance lane
    inside a product whose entire claim is that inference never leaves the
    building, left `_default_models()` on 2026-08-09 and was still being served
    by this box on 2026-09-07, a month later. It also survived a full
    `scripts/reset_db_and_seed.py`: that wipe walks patient-scoped tables, and
    `model_registry` has no `patient_id` and no foreign key into one, so a reseed
    does not reach it. Only an administrator knowing to call
    `POST /api/admin/models/reseed` ever cleared it.

    WHY THIS IS MORE THAN A SCREEN. `services/compliance_service._model_posture`
    also calls `list_models()` and reports `total = len(models)`, so the stale row
    put "total 5 / unvalidated 4" into the compliance posture export when the
    truth was 4 / 3. Every other probe in that report degrades to the WEAKER
    claim when it fails; this one never failed. It faithfully reported a registry
    that was wrong upstream, which is why `_safe()` could not catch it and why
    the fix has to be here rather than in the report.

    FROZEN KEY SET, on purpose. The tuple below is what `_default_models()`
    shipped the day this migration was written, not an import of it: a migration
    is a historical record and must not change meaning when the model line-up
    does. Over-deletion is self-correcting regardless, because `main.py`'s
    lifespan calls `seed_default_models()` immediately after `init_db()` returns
    and re-upserts the current set. This migration repairs databases that are
    ALREADY wrong; the lifespan reconcile is what stops the NEXT retirement
    drifting.

    SAFE TO GENERALISE HERE, unlike migrations 36 and 37. Those matched one exact
    value because an operator might legitimately have set something else. This
    table has no such case: it is written from `_default_models()` and nowhere
    else, so a key outside that set cannot be a deliberate local choice.
    """
    if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='model_registry'"
    ).fetchone() is None:
        return

    known = ("vigil_news2", "vigil_cfc", "forecast_lstm", "sage_llm")
    cur = conn.execute(
        "DELETE FROM model_registry WHERE model_key NOT IN "
        f"({','.join('?' * len(known))})",
        known,
    )
    if getattr(cur, "rowcount", 0) > 0:
        logger.info(
            "migration 38: removed %s model_registry row(s) for models Orb no "
            "longer ships; they were being shown as live in the governance UI "
            "and counted in the compliance posture export",
            cur.rowcount,
        )


def _rebuild_sage_handoffs_blocked_status(conn) -> None:
    """Admit 'blocked' into sage_handoffs.status and add safety_json.

    When the drug-safety screen stopped an agentic order, the handoff was
    stored 'accepted' with downstream_ref_id 'blocked-allergy', and the Relay
    card read "Sent to Helix" for an order that never left (found 2026-10-02:
    a doctor confirming ceftriaxone for a ceftriaxone-anaphylaxis patient was
    told it was sent). 'blocked' is its own outcome, and safety_json carries the
    screen's alerts, the non-blocking ones included, to the card.

    SQLite cannot ALTER a CHECK constraint, and sage_handoffs is an FK parent
    (sage_handoff_events.handoff_id), so this is the users-rebuild shape from
    migration 33: database._atomic_table_rebuild owns an autocommit window,
    then the runner's transaction is re-opened. See _rebuild_users_role_check
    for why neither in-transaction recipe works on an FK parent.

    Rows written before this keep their old values ('accepted' +
    'blocked-allergy'): rewriting them would put a status in the record that
    nothing decided at the time.

    Idempotent: no-ops when the stored DDL already admits 'blocked' and the
    column exists.
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='sage_handoffs'"
    ).fetchone()
    if not row:
        return
    old_cols = [r[1] for r in conn.execute("PRAGMA table_info(sage_handoffs)").fetchall()]
    if "'blocked'" in (row[0] or "") and "safety_json" in old_cols:
        return
    # The one index the table carries is recreated below; anything else hanging
    # off it would drop with the rebuild, so refuse rather than lose it.
    assoc = conn.execute(
        "SELECT name, type FROM sqlite_master WHERE tbl_name='sage_handoffs' "
        "AND type IN ('index', 'trigger') AND name NOT LIKE 'sqlite_autoindex%' "
        "AND name != 'idx_sage_handoffs_action'"
    ).fetchall()
    if assoc:
        raise RuntimeError(
            "sage_handoffs rebuild would drop associated objects it does not know how "
            f"to preserve: {[(r[0], r[1]) for r in assoc]}"
        )
    canonical = [
        "id", "source_action_id", "room_id", "patient_id", "target_module",
        "handoff_type", "payload_json", "status", "downstream_ref_id",
        "error_summary", "created_by", "last_attempted_at", "created_at",
        "updated_at", "safety_json",
    ]
    unknown = [c for c in old_cols if c not in canonical]
    if unknown:
        raise RuntimeError(f"sage_handoffs rebuild would drop live column(s) {unknown}")
    shared = ", ".join(c for c in canonical if c in old_cols)
    was_in_txn = bool(getattr(conn, "in_transaction", False))
    from database import _atomic_table_rebuild  # lazy: mirrors database→migrations

    _atomic_table_rebuild(
        conn,
        table="sage_handoffs",
        shared_cols=shared,
        create_new_sql="""CREATE TABLE sage_handoffs_new (
            id TEXT PRIMARY KEY,
            source_action_id TEXT NOT NULL REFERENCES sage_actions(id),
            room_id TEXT NOT NULL,
            patient_id TEXT NOT NULL,
            target_module TEXT NOT NULL,
            handoff_type TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            status TEXT DEFAULT 'pending' CHECK (status IN ('pending', 'accepted', 'failed', 'unsupported', 'blocked')),
            downstream_ref_id TEXT,
            error_summary TEXT,
            created_by TEXT NOT NULL REFERENCES users(id),
            last_attempted_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            safety_json TEXT
        )""",
        post_sql=[
            "CREATE INDEX IF NOT EXISTS idx_sage_handoffs_action "
            "ON sage_handoffs(source_action_id, created_at DESC)",
        ],
    )
    if was_in_txn and not getattr(conn, "in_transaction", False):
        conn.execute("BEGIN IMMEDIATE")


def _m39_handoff_blocked_status(conn) -> None:
    """A blocked agentic order is stored 'blocked', not 'accepted'. The rebuild
    lives in _rebuild_sage_handoffs_blocked_status, the same split as m33 and
    _rebuild_users_role_check, so its scratch table (sage_handoffs_new) is not
    read as a new table by the warm-standby manifest check."""
    _rebuild_sage_handoffs_blocked_status(conn)


def _m40_admission_allergies_charted(conn) -> None:
    """Chart the allergies typed at admission that never reached the interlock.

    Until 2026-10-02 admit_patient kept the allergy chips ONLY in the
    patient_profiles.allergies narrative. patient_allergies, which the
    order-time and bedside interlocks screen against, stayed empty, so every
    patient admitted through the form screened as allergy-free. Admission now
    writes both; this charts the ones already admitted.

    Add-only and idempotent (services/clinical_records_service.
    backfill_profile_allergies): an allergy already on the chart, active or
    deactivated by a clinician, is never touched, and free-text narratives are
    left for a clinician rather than split into guesses.
    """
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='patient_profiles'"
    ).fetchone()
    if not exists:
        return
    from services.clinical_records_service import backfill_profile_allergies  # lazy

    backfill_profile_allergies(conn)


def _m41_discharge_summary_review(conn) -> None:
    """Review state on discharge_summaries, so a model's draft cannot reach the
    patient before a clinician has read it.

    Every discharge summary was the raw 27B output, stored as final and shown on
    the patient portal and the discharge link; the discharging doctor never saw
    it (found 2026-10-01: an invented discharge date and a literal
    "[Doctor name]" in the attending line). review_status is 'draft' until a
    prescriber marks it reviewed (services/discharge_service.
    mark_discharge_summary_reviewed); the patient-facing readers show the text
    only when it says 'reviewed'.

    Existing rows default to 'draft' as well. None of them was reviewed, and a
    record must not claim a review nobody did; a clinician can review them now.

    Idempotent: each column is added only when absent.
    """
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='discharge_summaries'"
    ).fetchone()
    if not exists:
        return
    cols = {r[1] for r in conn.execute("PRAGMA table_info(discharge_summaries)").fetchall()}
    if "review_status" not in cols:
        conn.execute(
            "ALTER TABLE discharge_summaries ADD COLUMN review_status TEXT NOT NULL DEFAULT 'draft'"
        )
    if "reviewed_by" not in cols:
        conn.execute("ALTER TABLE discharge_summaries ADD COLUMN reviewed_by TEXT")
    if "reviewed_at" not in cols:
        conn.execute("ALTER TABLE discharge_summaries ADD COLUMN reviewed_at TIMESTAMP")


def _m42_claims_idempotency(conn) -> None:
    """``claims.idempotency_key``, unique when present, so opening a claim file
    is safe to repeat.

    The Insurance desk's "Open claim" had no busy guard and the route no key, so
    a double click opened two claim files for the same admission, each with its
    own CLM number (found 2026-10-01). services/payer_service.create_claim now
    returns the existing claim when the same key arrives again.

    Idempotent: the column is added only when absent; the index is IF NOT EXISTS.
    """
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='claims'"
    ).fetchone()
    if not exists:
        return
    cols = {r[1] for r in conn.execute("PRAGMA table_info(claims)").fetchall()}
    if "idempotency_key" not in cols:
        conn.execute("ALTER TABLE claims ADD COLUMN idempotency_key TEXT")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_claims_idempotency ON claims(idempotency_key) "
        "WHERE idempotency_key IS NOT NULL"
    )


def _m43_canonical_vitals_risk_timestamps(conn) -> None:
    """One stored spelling for ``vitals.timestamp`` and ``risk_scores.timestamp``.

    Both are ordered as text, and they held up to five spellings (SQLite's
    CURRENT_TIMESTAMP, ISO '+00:00', 'Z', '+05:30', naive), so a benign nurse
    set could outrank a later critical ingestion score on every "latest" read
    (verified 2026-10-02/03). Writers now store
    ``YYYY-MM-DDTHH:MM:SS.ffffff+00:00`` (services/clinical_time.py); this
    rewrites the rows stored before that. Ruled by Samuel 2026-10-03, including
    reading a naive ingestion-feed string in the appliance zone.

    Delegates to services/clinical_time.backfill_canonical_timestamps, which
    runs in this transaction, leaves unparseable values untouched, writes one
    counts-only audit row, and is idempotent.
    """
    from services.clinical_time import backfill_canonical_timestamps  # lazy

    backfill_canonical_timestamps(conn)


def _m44_sage_provenance_schema(conn) -> None:
    """The Sage provenance store, created at boot like every other table.

    sage_provenance_turns/blobs/refs were created by module DDL on the first
    recorded answer (services/sage_provenance_service.ensure_schema), so a fresh
    appliance had no provenance tables until somebody asked Sage something, and
    nothing in the migration ledger said they belonged to the schema. This runs
    that same DDL, one statement per execute() (never executescript, which would
    commit the runner's transaction), including the additive columns
    (safety_floor_state, and safety_floor_armed added 2026-10-03). The module
    keeps calling ensure_schema, so an older database that skipped this still
    gets its tables.

    Idempotent: CREATE ... IF NOT EXISTS, and each column is added only when
    absent. No row is touched; the store is append-only.
    """
    from services.sage_provenance_service import _apply_schema  # lazy

    _apply_schema(conn)


def _m45_lens_review(conn) -> None:
    """Who reviewed a Lens read, when, and the text they confirmed.

    The assign route sent the clinician's reviewed text to the case room only;
    the row kept the raw model read, and the chart, discharge timeline, Sage and
    the patient portal all showed that (found 2026-10-01). A row is now filed
    only with reviewed_text, and those readers use reviewed rows and the
    reviewed text alone (routes/lens_routes._assign_analysis).

    Existing rows stay unreviewed (all three columns NULL). Nobody confirmed
    them, and a record must not claim a review nobody did.

    Idempotent: each column is added only when absent.
    """
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='lens_analyses'"
    ).fetchone()
    if not exists:
        return
    cols = {r[1] for r in conn.execute("PRAGMA table_info(lens_analyses)").fetchall()}
    if "reviewed_by" not in cols:
        conn.execute("ALTER TABLE lens_analyses ADD COLUMN reviewed_by TEXT")
    if "reviewed_at" not in cols:
        conn.execute("ALTER TABLE lens_analyses ADD COLUMN reviewed_at TIMESTAMP")
    if "reviewed_text" not in cols:
        conn.execute("ALTER TABLE lens_analyses ADD COLUMN reviewed_text TEXT")


def _m46_simulated_result_source(conn) -> None:
    """``source`` on clinical_documents and lens_analyses, so a simulator's
    result is marked as one.

    The simulation lab and imaging bots (services/bot_service) wrote rows that
    looked exactly like genuine results: Sage read them as chart facts and the
    Tier 1 screen raised renal flags off a fabricated creatinine. They now write
    source = 'simulated'; the chart labels those rows, Sage prefixes them, and
    Tier 1 does not flag off them.

    Existing rows keep NULL (a genuine or unknown origin). Rows the lab and
    imaging bots wrote before this are recognised by their author account
    (bot_service.LAB_BOT_ID / LENS_BOT_ID, the only writers under those ids)
    and marked too, so the old fabricated results stop counting as genuine.

    Idempotent: each column is added only when absent; the backfill touches
    only rows still NULL.
    """
    for table in ("clinical_documents", "lens_analyses"):
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        if not exists:
            continue
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if "source" not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN source TEXT")
        conn.execute(
            f"UPDATE {table} SET source = 'simulated' "
            "WHERE source IS NULL AND author_id IN ('bot_lab', 'bot_lens')"
        )


def _m47_vitals_carried_from(conn) -> None:
    """When each carried-forward vital in a row was really measured.

    The feed lane fills vitals a monitor message did not carry from the
    patient's latest row, and the simulator does the same for ACVPU and O2. A
    copied value looked exactly like a measured one, so it had no age, and a
    02:00 SBP 78 was scored and alerted as current at 13:00 (review 2026-10-02,
    G1). Ruled by Samuel 2026-10-03: carry-forward is age-limited by the time
    of the MEASUREMENT (services/vitals_carry.py). This column holds it, as JSON
    {column: measured_at UTC} for every value the row copied; NULL means every
    value in the row was measured at its own timestamp.

    Existing rows stay NULL. Their copied values are indistinguishable from
    measured ones and are treated as measured at the row's time, so the limit
    starts from there once; nothing is backfilled because nothing records the
    truth to backfill from.

    Idempotent: the column is added only when absent.
    """
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='vitals'"
    ).fetchone()
    if not exists:
        return
    cols = {r[1] for r in conn.execute("PRAGMA table_info(vitals)").fetchall()}
    if "carried_from" not in cols:
        conn.execute("ALTER TABLE vitals ADD COLUMN carried_from TEXT")


MIGRATIONS: list[Migration] = [
    Migration(1, "hot_path_indexes", _m1_hot_path_indexes),
    Migration(2, "messages_idempotency", _m2_messages_idempotency),
    Migration(3, "sage_insights_auto_resolved", _m3_sage_insights_auto_resolved),
    Migration(4, "shadow_events", _m4_shadow_events),
    Migration(5, "roster_import_runs", _m5_roster_import_runs),
    Migration(6, "allergy_interlock", _m6_allergy_interlock),
    Migration(7, "message_room_fit_flags", _m7_message_room_fit_flags),
    Migration(8, "channel_members", _m8_channel_members),
    Migration(9, "recover_stranded_sage_insights", _m9_recover_stranded_sage_insights),
    Migration(10, "dose_guard", _m10_dose_guard),
    Migration(11, "escalation_chain", _m11_escalation_chain),
    Migration(12, "device_pin_identity", _m12_device_pin_identity),
    Migration(13, "order_sets", _m13_order_sets),
    Migration(14, "dpdp_act_machinery", _m14_dpdp_act_machinery),
    Migration(15, "history_import", _m15_history_import),
    Migration(16, "sage_conversation_threads", _m16_sage_conversation_threads),
    Migration(17, "patient_review_marks", _m17_patient_review_marks),
    Migration(18, "shadow_none_confirmed_once", _m18_shadow_none_confirmed_once),
    Migration(19, "scribe_unverified_ack_columns", _m19_scribe_unverified_ack_columns),
    Migration(20, "hospital_admin_spine", _m20_hospital_admin_spine),
    Migration(21, "biomedical_assets", _m21_biomedical_assets),
    Migration(22, "housekeeping", _m22_housekeeping),
    Migration(23, "abdm_abha_rails", _m23_abdm_abha_rails),
    Migration(24, "workforce_attendance", _m24_workforce_attendance),
    Migration(25, "dietary", _m25_dietary),
    Migration(26, "nabh_evidence", _m26_nabh_evidence),
    Migration(27, "surgery_timing", _m27_surgery_timing),
    Migration(28, "patient_appointment_requests_bridge",
              _m28_patient_appointment_requests_bridge),
    Migration(29, "sage_assessments", _m29_sage_assessments),
    Migration(30, "sage_assessment_tiers", _m30_sage_assessment_tiers),
    Migration(31, "sage_patient_specific", _m31_sage_patient_specific),
    Migration(32, "allergy_override_attribution", _m32_allergy_override_attribution),
    Migration(33, "users_role_cashier", _m33_users_role_cashier),
    Migration(34, "minority_retention_authority", _m34_minority_retention_authority),
    Migration(35, "ward_receipt", _m35_ward_receipt),
    Migration(36, "pharmacy_is_not_a_ward", _m36_pharmacy_is_not_a_ward),
    Migration(37, "system_account_has_no_ward", _m37_system_account_has_no_ward),
    Migration(38, "retire_unknown_models", _m38_retire_unknown_models),
    Migration(39, "handoff_blocked_status", _m39_handoff_blocked_status),
    Migration(40, "admission_allergies_charted", _m40_admission_allergies_charted),
    Migration(41, "discharge_summary_review", _m41_discharge_summary_review),
    Migration(42, "claims_idempotency", _m42_claims_idempotency),
    Migration(43, "canonical_vitals_risk_timestamps", _m43_canonical_vitals_risk_timestamps),
    Migration(44, "sage_provenance_schema", _m44_sage_provenance_schema),
    Migration(45, "lens_review", _m45_lens_review),
    Migration(46, "simulated_result_source", _m46_simulated_result_source),
    Migration(47, "vitals_carried_from", _m47_vitals_carried_from),
]


def _apply_pending(conn) -> list[int]:
    """The apply loop itself. Assumes a transaction is already open — see
    run_pending_migrations, which is what guarantees that."""
    applied = _applied_versions(conn)
    ran: list[int] = []
    for mig in sorted(MIGRATIONS, key=lambda m: m.version):
        if mig.version in applied:
            continue
        logger.info("Applying migration %d (%s)", mig.version, mig.name)
        mig.apply(conn)
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations (version, name) VALUES (?, ?)",
            (mig.version, mig.name),
        )
        ran.append(mig.version)
    if ran:
        logger.info("Applied %d migration(s): %s", len(ran), ran)
    return ran


def run_pending_migrations(conn) -> list[int]:
    """Apply every migration whose version isn't in the ledger, in order.

    ALL-OR-NOTHING. Either every pending migration is applied and ledgered, or
    the database is exactly as it was. Returns the versions applied this call
    (empty if the schema was already current).

    WHY THIS OWNS ITS TRANSACTION INSTEAD OF ASSUMING ONE.
    This function used to document that it "runs inside the caller's open
    transaction (the caller commits), so a failure rolls the whole init back" —
    and that was true of init_db by luck, not by construction. Python's sqlite3
    opens an implicit transaction only before DML (INSERT/UPDATE/DELETE), NEVER
    before DDL. The only thing keeping init_db's migration run atomic was an
    unrelated `INSERT OR IGNORE INTO users ('system', ...)` a few dozen lines
    earlier, which happened to leave a transaction open. Delete or condition
    that one line — or call this function from an ops/repair script, which the
    module docstring positively invites — and every CREATE/ALTER/DROP below
    commits on its own.

    That is not a theoretical hazard. Migration 3 is a four-statement rebuild
    (RENAME → CREATE → INSERT SELECT → DROP). Executed with nothing pending and
    SIGKILLed after the RENAME, it leaves `sage_insights` gone and all its rows
    in `sage_insights_old`; the next boot recreates the table empty, migration 3
    reads the fresh DDL and no-ops, and the ledger records v3 — the migration is
    skipped forever over a half-built schema, with no error anywhere.
    Reproduced end-to-end 2026-08-06 with 500 rows. See
    tests/test_migration_atomicity.py.

    So: if the caller already holds a transaction (init_db's db_session, the
    tests), JOIN it and let the caller commit — that preserves the existing
    contract exactly, including nested/pooled SAVEPOINT frames, where
    `in_transaction` is necessarily True because a SAVEPOINT starts one.
    Otherwise take an explicit BEGIN IMMEDIATE and commit or roll back here.
    `in_transaction` False means the caller has nothing pending, so committing
    our own work can never publish someone else's half-finished writes.
    """
    # Absent attribute (an exotic wrapper/mock) → keep the historical
    # join-the-caller behaviour rather than issue BEGIN on something that may
    # not support it.
    in_txn = getattr(conn, "in_transaction", True)
    if in_txn:
        return _apply_pending(conn)

    prev_isolation = conn.isolation_level
    conn.isolation_level = None  # we own BEGIN/COMMIT from here
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            ran = _apply_pending(conn)
            conn.execute("COMMIT")
            return ran
        except BaseException:
            try:
                conn.execute("ROLLBACK")
            except Exception:  # pragma: no cover — nothing left to roll back
                logger.debug("migration rollback failed", exc_info=True)
            raise
    finally:
        # Restore before returning: on a POOLED handle this connection is shared
        # thread-wide, and a leaked isolation_level=None puts the whole thread in
        # autocommit — which turns every later db_session rollback into a silent
        # no-op (the failure mode db_backend.py documents).
        conn.isolation_level = prev_isolation


def current_version(conn) -> int:
    """Highest applied migration version (0 if none) — for health/diagnostics."""
    _ensure_ledger(conn)
    row = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
    return int(row[0]) if row and row[0] is not None else 0
