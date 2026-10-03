"""Audit-trail attack regressions (executed 2026-08-06).

Every test here failed on the tree as it stood before the fixes in
`services/audit_service.py` and `services/shadow_service.py`, and passes after.
They are written against the behaviour an attacker exercises, not against the
implementation, so a later refactor that reopens the hole still fails them.

WHAT WAS ACTUALLY RUN (not reasoned about):

  * `x-correlation-id: "C"*200000` on an ordinary authenticated POST wrote
    200,000 attacker-chosen characters into `audit_logs.correlation_id`,
    verbatim, newlines included. Measured 260,000 client-controlled bytes per
    audit row and ~92 MB/s of audit-table growth from ONE nurse account.
    `audit_logs` is DELETE-blocked by trigger, so none of it is reclaimable; and
    `phi_access` is fail-closed, so a full disk stops clinicians opening charts.

  * `DROP TRIGGER trg_shadow_no_delete` followed by `init_db()` left the trigger
    permanently gone — migration 4 is one-shot — while the audit_logs triggers,
    which live in `database.SCHEMA`, came straight back.

The attacks that FAILED are recorded here too, as characterisation tests: the
actor cannot be forged and the timestamp cannot be influenced by the client.
Those are the properties most likely to be broken by a well-meaning "let the
client pass its own user id / event time" change, so they are pinned.
"""
import uuid

import pytest
from fastapi.testclient import TestClient

from auth import create_token
from database import db_session
from main import app
from services import shadow_service
from services.audit_service import (
    _MAX_ID_CHARS,
    _MAX_METADATA_BYTES,
    _MAX_SUMMARY_CHARS,
    _MAX_USER_AGENT_CHARS,
    log_audit_event,
)

client = TestClient(app)


def _auth(user_id="AUDATK01", role="nurse"):
    return {"Authorization": f"Bearer {create_token(user_id, role)}"}


@pytest.fixture(autouse=True)
def _nurse_user():
    with db_session() as db:
        db.execute(
            "INSERT OR REPLACE INTO users (id, name, role, department, password_hash) "
            "VALUES ('AUDATK01', 'Audit Attack Nurse', 'nurse', 'Audit Attack Ward', 'x')"
        )
    yield


def _newest_audit_row():
    with db_session() as db:
        row = db.execute(
            "SELECT correlation_id, user_agent, summary_message, metadata_json, "
            "       actor_user_id, actor_role, timestamp, request_path "
            "FROM audit_logs ORDER BY id DESC LIMIT 1"
        ).fetchone()
    keys = ("correlation_id", "user_agent", "summary_message", "metadata_json",
            "actor_user_id", "actor_role", "timestamp", "request_path")
    return dict(zip(keys, tuple(row)))


# ══════════════════════════════════════════════════════════════════════════════
#  1. Unbounded client-controlled fields — the append-only disk-exhaustion path
# ══════════════════════════════════════════════════════════════════════════════

def test_giant_correlation_id_header_is_bounded_before_it_reaches_the_chain():
    """PRE-FIX: 200,000 chars stored verbatim. The middleware takes this header
    straight off the request and nothing between it and the INSERT capped it."""
    client.post(
        "/api/nurse/observations",
        headers={**_auth(), "x-correlation-id": "C" * 200_000},
        json={"patient_id": "NO_SUCH_PATIENT_AUDATK"},
    )
    row = _newest_audit_row()
    assert len(row["correlation_id"]) <= _MAX_ID_CHARS, (
        f"correlation_id stored at {len(row['correlation_id'])} chars — an "
        "authenticated user can grow the append-only audit table without limit, "
        "and the space can never be reclaimed (trg_audit_logs_no_delete)."
    )


def test_giant_user_agent_is_bounded():
    """Same vector, second header. A real user-agent is ~200 chars."""
    client.post(
        "/api/nurse/observations",
        headers={**_auth(), "user-agent": "U" * 60_000},
        json={"patient_id": "NO_SUCH_PATIENT_AUDATK"},
    )
    row = _newest_audit_row()
    assert len(row["user_agent"] or "") <= _MAX_USER_AGENT_CHARS


def test_client_controlled_bytes_per_audit_row_stay_small_under_abuse():
    """The property that actually matters: bytes-on-disk per request that the
    ATTACKER chooses. Measured at 260,000 pre-fix; the bound puts a hard ceiling
    on it regardless of which header they reach for."""
    with db_session() as db:
        before = db.execute(
            "SELECT COALESCE(SUM(LENGTH(COALESCE(correlation_id,'')) + "
            "LENGTH(COALESCE(user_agent,'')) + LENGTH(COALESCE(request_path,''))), 0), "
            "COUNT(*) FROM audit_logs"
        ).fetchone()
    n = 10
    for _ in range(n):
        client.post(
            "/api/nurse/observations",
            headers={**_auth(),
                     "x-correlation-id": "C" * 200_000,
                     "user-agent": "U" * 60_000},
            json={"patient_id": "NO_SUCH_PATIENT_AUDATK"},
        )
    with db_session() as db:
        after = db.execute(
            "SELECT COALESCE(SUM(LENGTH(COALESCE(correlation_id,'')) + "
            "LENGTH(COALESCE(user_agent,'')) + LENGTH(COALESCE(request_path,''))), 0), "
            "COUNT(*) FROM audit_logs"
        ).fetchone()
    rows_added = after[1] - before[1]
    assert rows_added >= n, "expected one audit row per mutation"
    per_row = (after[0] - before[0]) / rows_added
    assert per_row < 2_000, (
        f"{per_row:,.0f} attacker-chosen bytes per audit row — pre-fix this was "
        "260,000, i.e. ~92 MB/s of unreclaimable audit table from one account."
    )


def test_oversized_metadata_is_replaced_by_a_marker_not_a_broken_fragment():
    """A truncated JSON fragment would not parse; the row must stay readable."""
    import json

    log_audit_event(
        correlation_id=str(uuid.uuid4()),
        event_category="api_mutation",
        action_type="audatk_meta",
        outcome="success",
        metadata={"blob": "M" * (_MAX_METADATA_BYTES * 3)},
    )
    row = _newest_audit_row()
    assert len(row["metadata_json"].encode("utf-8")) <= _MAX_METADATA_BYTES
    parsed = json.loads(row["metadata_json"])  # must still be valid JSON
    assert parsed["_truncated"] is True
    assert parsed["original_bytes"] > _MAX_METADATA_BYTES


def test_summary_message_is_bounded():
    log_audit_event(
        correlation_id=str(uuid.uuid4()),
        event_category="api_mutation",
        action_type="audatk_summary",
        outcome="success",
        summary_message="S" * (_MAX_SUMMARY_CHARS * 4),
    )
    assert len(_newest_audit_row()["summary_message"]) <= _MAX_SUMMARY_CHARS


# ══════════════════════════════════════════════════════════════════════════════
#  2. Control-character injection into the correlation dimension
# ══════════════════════════════════════════════════════════════════════════════

def test_correlation_id_cannot_carry_a_forged_extra_line():
    """`correlation_id` is what forensics GROUPs BY to reconstruct a session.
    PRE-FIX a caller could embed a newline plus a plausible-looking record in
    it, so a rendered or exported trail read as though it held events it did
    not."""
    poison = "SESSION-X\n[row] actor=SOMEONE_ELSE action=chart_opened outcome=success"
    client.post(
        "/api/nurse/observations",
        headers={**_auth(), "x-correlation-id": poison},
        json={"patient_id": "NO_SUCH_PATIENT_AUDATK"},
    )
    stored = _newest_audit_row()["correlation_id"]
    assert "\n" not in stored and "\r" not in stored, (
        "a newline survived into correlation_id — the trail can be made to "
        "render rows that were never written"
    )
    assert "\x00" not in stored


def test_control_characters_are_stripped_from_identifier_columns():
    log_audit_event(
        correlation_id="a\nb\r\tc\x00d e",
        event_category="api_mutation",
        action_type="audatk\ncontrol",
        outcome="success",
        actor_user_id="U\n001",
    )
    row = _newest_audit_row()
    for ch in ("\n", "\r", "\t", "\x00", " "):
        assert ch not in row["correlation_id"]
        assert ch not in (row["actor_user_id"] or "")


def test_bounding_does_not_break_the_hash_chain():
    """The bound is applied BEFORE hashing, so the stored bytes and the hashed
    bytes are the same bytes. If that ordering is ever inverted, every clipped
    row verifies as tampered."""
    from database import verify_audit_chain

    client.post(
        "/api/nurse/observations",
        headers={**_auth(), "x-correlation-id": "C" * 100_000, "user-agent": "U" * 9_000},
        json={"patient_id": "NO_SUCH_PATIENT_AUDATK"},
    )
    result = verify_audit_chain()
    assert result["ok"], f"hash chain broken after clipping: {result.get('broken_row_ids')}"


# ══════════════════════════════════════════════════════════════════════════════
#  3. Characterisation: the attacks that FAILED, pinned so they keep failing
# ══════════════════════════════════════════════════════════════════════════════

def test_actor_cannot_be_forged_from_headers_or_body():
    """Executed: X-Actor-User-Id / body actor_user_id / a tampered JWT payload /
    alg=none. None moved the actor. The actor comes from the verified JWT."""
    client.post(
        "/api/nurse/observations",
        headers={**_auth("AUDATK01", "nurse"),
                 "X-Actor-User-Id": "VICTIM_DOCTOR",
                 "X-User-Id": "VICTIM_DOCTOR",
                 "X-Forwarded-User": "VICTIM_DOCTOR"},
        json={"patient_id": "NO_SUCH_PATIENT_AUDATK",
              "actor_user_id": "VICTIM_DOCTOR",
              "user_id": "VICTIM_DOCTOR",
              "recorded_by": "VICTIM_DOCTOR"},
    )
    row = _newest_audit_row()
    assert row["actor_user_id"] == "AUDATK01"
    assert row["actor_role"] == "nurse"


def test_client_cannot_influence_the_audit_timestamp():
    """Executed with Date / X-Timestamp / x-occurred-at headers and matching
    body fields. The timestamp is `datetime.now(timezone.utc)`, server-side."""
    client.post(
        "/api/nurse/observations",
        headers={**_auth(), "Date": "1999-01-01T00:00:00Z",
                 "X-Timestamp": "1999-01-01T00:00:00Z",
                 "x-occurred-at": "2099-01-01T00:00:00Z"},
        json={"patient_id": "NO_SUCH_PATIENT_AUDATK",
              "timestamp": "1999-01-01T00:00:00Z",
              "occurred_at": "1999-01-01T00:00:00Z"},
    )
    ts = _newest_audit_row()["timestamp"]
    assert not ts.startswith("1999") and not ts.startswith("2099")
    assert ts.endswith("+00:00") or ts.endswith("Z"), "audit timestamps must be UTC"


# ══════════════════════════════════════════════════════════════════════════════
#  4. shadow_events append-only: watch the triggers actually fire
# ══════════════════════════════════════════════════════════════════════════════

def test_shadow_append_only_triggers_actually_fire():
    """Not "the trigger exists in sqlite_master" — it is made to ABORT a real
    UPDATE and a real DELETE, inside a rolled-back savepoint."""
    result = shadow_service.verify_append_only()
    assert result["ok"], result
    assert result["update_blocked"], "an UPDATE on shadow_events was NOT aborted"
    assert result["delete_blocked"], "a DELETE of a non-simulator row was NOT aborted"
    assert result["simulator_delete_allowed"], (
        "simulator-lane pruning is blocked — retention can no longer run"
    )
    assert result["triggers_missing"] == []


def test_verify_append_only_leaves_no_probe_rows_behind():
    with db_session() as db:
        before = db.execute("SELECT COUNT(*) FROM shadow_events").fetchone()[0]
    shadow_service.verify_append_only()
    with db_session() as db:
        after = db.execute("SELECT COUNT(*) FROM shadow_events").fetchone()[0]
        leaked = db.execute(
            "SELECT COUNT(*) FROM shadow_events WHERE episode_id LIKE '_verify%'"
        ).fetchone()[0]
    assert after == before, "verification probe rows were committed to the instrument"
    assert leaked == 0


def test_verify_append_only_reports_a_dropped_trigger_instead_of_passing():
    """PRE-FIX there was no verification at all, so a dropped trigger was
    invisible. The instrument must not report healthy while it is mutable."""
    with db_session() as db:
        db.execute("DROP TRIGGER IF EXISTS trg_shadow_no_delete")
    try:
        result = shadow_service.verify_append_only()
        assert result["ok"] is False
        assert "trg_shadow_no_delete" in result["triggers_missing"]
        assert result["delete_blocked"] is False, (
            "a non-simulator row was deletable and verification still passed"
        )
    finally:
        shadow_service.ensure_append_only()


def test_ensure_append_only_restores_what_the_one_shot_migration_cannot():
    """EXECUTED PRE-FIX: `DROP TRIGGER trg_shadow_no_delete` then `init_db()`
    left it gone for good — migration 4 has already recorded its version in
    `schema_migrations` and never runs again. The audit_logs triggers survive
    the same treatment only because they live in `database.SCHEMA`, which
    replays every boot. This gives shadow_events the same property."""
    from database import init_db

    with db_session() as db:
        db.execute("DROP TRIGGER IF EXISTS trg_shadow_no_delete")
        db.execute("DROP TRIGGER IF EXISTS trg_shadow_no_update")

    init_db()  # the one-shot migration does NOT bring them back
    with db_session() as db:
        after_init = {
            r[0] for r in db.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' "
                "AND tbl_name='shadow_events'"
            ).fetchall()
        }
    assert "trg_shadow_no_delete" not in after_init, (
        "migration 4 unexpectedly re-ran; this test's premise needs revisiting"
    )

    repaired = shadow_service.ensure_append_only()
    assert "trg_shadow_no_delete" in repaired["repaired"]
    assert repaired["verified"]["ok"] is True

    # and the guarantee is real again, proven by firing it
    with db_session() as db:
        db.execute(
            "INSERT INTO shadow_events (episode_id, kind, occurred_at, recorded_at, lane, payload_json) "
            "VALUES ('audatk_ep', 'alert.fired', '2026-08-06T00:00:00.000Z', "
            "'2026-08-06T00:00:00.000Z', 'manual', '{}')"
        )
    with pytest.raises(Exception):
        with db_session() as db:
            db.execute("DELETE FROM shadow_events WHERE episode_id = 'audatk_ep'")


def test_audit_logs_triggers_block_update_and_delete():
    """The compliance record's own append-only guarantee, watched firing."""
    log_audit_event(
        correlation_id="audatk-immutable",
        event_category="phi_access",
        action_type="audatk_immutable",
        outcome="success",
        actor_user_id="AUDATK01",
        patient_id="NO_SUCH_PATIENT_AUDATK",
    )
    with pytest.raises(Exception):
        with db_session() as db:
            db.execute(
                "UPDATE audit_logs SET actor_user_id = 'SOMEONE_ELSE' "
                "WHERE correlation_id = 'audatk-immutable'"
            )
    with pytest.raises(Exception):
        with db_session() as db:
            db.execute("DELETE FROM audit_logs WHERE correlation_id = 'audatk-immutable'")
    with db_session() as db:
        row = db.execute(
            "SELECT actor_user_id FROM audit_logs WHERE correlation_id = 'audatk-immutable'"
        ).fetchone()
    assert row is not None and row[0] == "AUDATK01"
