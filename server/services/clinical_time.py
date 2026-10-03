"""The one spelling Orb stores for `vitals.timestamp` and `risk_scores.timestamp`.

WHY THIS EXISTS (verified 2026-10-02/03, ruled by Samuel 2026-10-03)
───────────────────────────────────────────────────────────────────
Both columns are ordered as TEXT (`ORDER BY timestamp DESC`, `MAX(timestamp)`),
and that is only chronological while every row is spelled the same way. They
were not:

  * `risk_scores` rows from the ingestion and admission lanes took SQLite's
    `CURRENT_TIMESTAMP` default ("2026-10-02 19:26:36"), while the nurse lane
    wrote ISO ("2026-10-02T13:26:35.789391+00:00"). 'T' sorts above ' ', so a
    benign nurse set charted at 13:26 outranked a critical ingestion score
    written at 19:26 on every board that reads "latest", for the rest of the
    UTC day.
  * The JSON, CSV and FHIR adapters stored the device's `observed_at`
    verbatim: 'Z', '+05:30' or no offset at all. A '+05:30' reading from two
    hours ago outranked a 'Z' reading from five minutes ago. A reading with no
    offset is IST wall-clock time, but SQLite, the front end and every Python
    parser read a naive string as UTC, so it was displayed, and sorted, 5.5 h
    in the future. Wrapping readers in julianday() cannot fix that last case;
    only storing the right instant can.

THE CONTRACT
────────────
Every writer of those two columns stores ``YYYY-MM-DDTHH:MM:SS.ffffff+00:00``
(UTC, 'T', microseconds, '+00:00'). One spelling per instant, so text order is
time order, the (patient_id, timestamp) indexes keep working, and equality
compares work across writers. Migration 43 rewrote the rows stored before this
contract existed.

A naive value means a zone, and which zone depends on who wrote it:

  * Orb's own strings (CURRENT_TIMESTAMP, `datetime.utcnow()`) are UTC. That is
    the default here.
  * A sender's string with no offset is the device's local wall clock. Device
    lanes pass ``naive_tz=device_naive_tz()``, the appliance zone, which is the
    same rule `vitals_source/sink.py:_parse_iso` and the HL7 parser apply.

`services/hospital_time.py` answers "what day is it on the ward" and never
formats stored data; this module only formats stored data.
"""
from __future__ import annotations

from datetime import datetime, timezone, tzinfo
from typing import Optional, Union

# The canonical spelling as a SQLite GLOB, for the migration and the tests.
CANONICAL_GLOB = (
    "[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T"
    "[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]+00:00"
)


def format_utc(dt: datetime) -> str:
    """An AWARE datetime in the canonical spelling. A naive one is refused:
    the caller has to say which zone it is in (see `to_utc_iso`)."""
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError("naive datetime: pass it through to_utc_iso with its zone")
    return dt.astimezone(timezone.utc).isoformat(timespec="microseconds")


def utc_iso_now() -> str:
    """Now, in the canonical spelling."""
    return format_utc(datetime.now(timezone.utc))


def parse_to_utc(value: Union[str, datetime], *,
                 naive_tz: Optional[tzinfo] = None) -> datetime:
    """Any stored or received spelling to an aware UTC datetime.

    Accepts a datetime, ISO 8601 with 'Z' or any offset, and SQLite's
    "YYYY-MM-DD HH:MM:SS". A value with no offset is read in ``naive_tz``
    (UTC when not given). Raises ValueError on anything else; it never guesses.
    """
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip() if value is not None else ""
        if not text:
            raise ValueError("empty timestamp")
        if text[-1] in "zZ":
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
    if dt.tzinfo is None or dt.utcoffset() is None:
        dt = dt.replace(tzinfo=naive_tz or timezone.utc)
    return dt.astimezone(timezone.utc)


def to_utc_iso(value: Union[str, datetime], *,
               naive_tz: Optional[tzinfo] = None) -> str:
    """Any accepted spelling (see `parse_to_utc`) in the canonical spelling."""
    return format_utc(parse_to_utc(value, naive_tz=naive_tz))


def device_naive_tz() -> tzinfo:
    """The zone a sender's offset-less timestamp is in: the appliance's.

    Delegates to `hl7v2.appliance_timezone()` (``ORB_HL7_DEFAULT_TZ``, else the
    OS zone) so the stored instant, the sink's ordering guard and the HL7
    parser can never disagree about what a device meant.
    """
    from services.vitals_source.hl7v2 import appliance_timezone

    return appliance_timezone()


# ════════════════════════════════════════════
#  Backfill (migration `canonical_vitals_risk_timestamps`)
# ════════════════════════════════════════════

# `vitals.source` labels Orb writes itself. A naive string under one of these is
# Orb's own, so UTC. Every other label (the ingestion feeds: ingested_json,
# ingested_hl7, ingested_fhir, csv_replay, monitor_hl7, manual_entry) went
# through the ingestion pipeline, the only lane that stored a SENDER's string
# verbatim, so a naive string there is the device's wall clock.
_ORB_WRITTEN_VITALS_SOURCES = frozenset({"simulated", "nurse_manual", "clinician", "llm_extracted"})

_BACKFILL_CHUNK = 5000


def _zone_name(tz: tzinfo) -> str:
    return str(getattr(tz, "key", None) or tz)


def backfill_canonical_timestamps(conn) -> dict:
    """Rewrite every `vitals` / `risk_scores` timestamp not already canonical.

    Runs inside the caller's transaction (the boot migration's) and never
    commits. Ruled by Samuel 2026-10-03 (R2, R3):

      * SQLite's "YYYY-MM-DD HH:MM:SS" in risk_scores is CURRENT_TIMESTAMP, so
        UTC. 'Z' and explicit offsets are converted to the same instant.
      * A naive vitals string is read by who wrote it: Orb's own labels as UTC,
        an ingestion feed's as the appliance zone, the rule the sink applied
        when it ingested the row. This is the one case that MOVES an instant
        (5.5 h earlier, to when the reading was taken) rather than respelling it.
      * Anything unparseable is left exactly as it is, counted and logged.

    Writes one PHI-free `data_management` audit row (counts only) when it
    changed or skipped anything. Idempotent: a second run finds nothing to do.
    Returns the counts, per table.
    """
    import logging

    log = logging.getLogger("orb.clinical_time")
    device_tz = device_naive_tz()
    counts: dict = {}
    for table in ("risk_scores", "vitals"):
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
        ).fetchone()
        if not exists:
            continue
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        source_sql = ", source" if (table == "vitals" and "source" in cols) else ", NULL"
        c = {"rewritten": 0, "unparseable": 0, "naive_read_as_utc": 0,
             "naive_read_in_device_zone": 0}
        last_id = 0
        while True:
            rows = conn.execute(
                f"""SELECT id, timestamp{source_sql} FROM {table}
                     WHERE id > ? AND timestamp IS NOT NULL
                       AND (typeof(timestamp) != 'text' OR timestamp NOT GLOB ?)
                     ORDER BY id LIMIT ?""",
                (last_id, CANONICAL_GLOB, _BACKFILL_CHUNK),
            ).fetchall()
            if not rows:
                break
            last_id = rows[-1][0]
            updates = []
            for row_id, raw, source in ((r[0], r[1], r[2]) for r in rows):
                try:
                    probe = parse_to_utc(raw, naive_tz=timezone.utc)
                    naive = not _has_offset(raw)
                    device = (naive and table == "vitals"
                              and (source or "").strip().lower() not in _ORB_WRITTEN_VITALS_SOURCES)
                    new = to_utc_iso(raw, naive_tz=device_tz) if device else format_utc(probe)
                except (TypeError, ValueError):
                    c["unparseable"] += 1
                    log.warning("timestamp backfill: %s id %s left as %r (unparseable)",
                                table, row_id, raw)
                    continue
                if naive:
                    c["naive_read_in_device_zone" if device else "naive_read_as_utc"] += 1
                if new != raw:
                    updates.append((new, row_id))
            if updates:
                conn.executemany(f"UPDATE {table} SET timestamp = ? WHERE id = ?", updates)
                c["rewritten"] += len(updates)
        counts[table] = c

    changed = sum(c["rewritten"] + c["unparseable"] for c in counts.values())
    if changed:
        from services.audit_service import log_audit_event

        log_audit_event(
            correlation_id="migration-canonical_vitals_risk_timestamps",
            event_category="data_management",
            action_type="timestamp_spelling_normalised",
            outcome="success",
            db=conn,
            summary_message=(
                "vitals/risk_scores timestamps rewritten to one UTC spelling: "
                + ", ".join(f"{t} {c['rewritten']} rewritten, {c['unparseable']} unparseable"
                            for t, c in counts.items())
            ),
            actor_user_id="system",
            metadata={"counts": counts, "device_zone": _zone_name(device_tz)},
        )
    log.info("timestamp backfill: %s (device zone %s)", counts, _zone_name(device_tz))
    return counts


def _has_offset(raw) -> bool:
    """True when the stored value names its own zone ('Z' or an offset)."""
    if isinstance(raw, datetime):
        return raw.tzinfo is not None
    text = str(raw).strip()
    if text[-1:] in ("z", "Z"):
        return True
    try:
        return datetime.fromisoformat(text).tzinfo is not None
    except ValueError:
        return False
