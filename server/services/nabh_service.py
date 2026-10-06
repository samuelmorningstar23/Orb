"""Orb — NABH ACCREDITATION EVIDENCE PACK.

What a hospital shows an NABH assessor: an incident / adverse-event register, a
quality-indicator dashboard computed from data Orb already holds, and a
date-ranged evidence export that bundles both.

NABH accredits the HOSPITAL, not the software. Nothing in this file makes a
hospital compliant. What it does is remove the three weeks of file-pulling
between "show me your medication-error data for last quarter" and an answer, and
make the answer one nobody had to assemble by hand — which is the only kind that
survives a second question.

════════════════════════════════════════════════════════════════════════════════
 THE RULE THIS FILE EXISTS TO ENFORCE
════════════════════════════════════════════════════════════════════════════════

**AN INDICATOR WITH INSUFFICIENT DATA IS NOT ZERO AND IT IS NOT 100 %.**

A pack that flatters the hospital is worse than no pack. It is worse in the
ordinary way (an assessor who catches one made-up number stops believing the
other eight) and worse in the way that matters (a quality committee that reads
"0 medication errors" stops looking for medication errors). So every indicator
below carries a `state`, and only `reported` is allowed to carry a number:

  reported           the number is real and the denominator supports it
  insufficient_data  the source exists but there is not enough of it; the
                     computed value is retained under `detail.withheld_value`
                     for the audit trail and is NOT rendered
  not_collected      Orb holds no source data for this indicator at this site.
                     Distinct from `insufficient_data` because the FIX is
                     different: start recording, versus wait for volume.
  not_computable     a blocker makes the indicator undefined in this window

This is the discipline services/shadow_scorecard_service.py already runs on the
pilot scorecard, deliberately copied rather than reinvented so that the two
honesty surfaces cannot drift apart in behaviour.

THREE PLACES WHERE A ZERO WOULD HAVE BEEN A LIE, and what is done instead:

  1. AN UNUSED REGISTER. "0 medication errors per 1,000 inpatient-days" over a
     month in which the incident register recorded NOTHING AT ALL is a statement
     about the register, not about the hospital. Every register-sourced
     indicator is therefore gated on the register having been used at all in the
     window (`_register_in_use`); with no reports of any kind it reads
     `not_collected`.
  2. AN UNRECORDED DISPOSITION. Orb had no discharge disposition anywhere before
     migration 26 — `patients.status` knows only admitted/discharged/transferred
     and cannot express a death or a LAMA. Mortality and LAMA are therefore
     gated on COVERAGE: dispositions recorded ÷ discharge events observed. Below
     `_coverage_floor()` the indicator reads `insufficient_data` WITH the
     coverage printed, because 1 death out of 3 recorded dispositions on a ward
     that discharged 40 people is 33 %, and 33 % would be a catastrophe on a
     page an assessor reads.
  3. AN UNFINISHED CLOCK. A patient discharged yesterday cannot yet be known not
     to have been re-admitted within 7 days, and an incident reported yesterday
     cannot yet have failed a 30-day closure target. Both indicators EXCLUDE the
     right-censored rows and report how many they excluded, rather than counting
     "not yet" as "no".

WHERE A ZERO IS HONEST: `medication_override_rate` is computed over
machine-generated denominators (administrations recorded by the eMAR), so zero
overrides against a real administration count is a finding, not an absence, and
it reports as 0.

════════════════════════════════════════════════════════════════════════════════
 CONVENTIONS
════════════════════════════════════════════════════════════════════════════════

  * PURE SYNC. Every public function opens its own `db_session()` (or accepts an
    open `db=`). Async callers go through `database.run_db`; the route module
    uses plain `def` handlers so FastAPI threadpools them. Blocking DB work
    inside `async def` is banned and tests/test_event_loop_discipline.py
    enforces it.
  * NEVER `except sqlite3.IntegrityError`. sqlcipher3 raises its OWN hierarchy,
    so a stdlib class is dead code on the shipping encrypted appliance. Import
    `DBIntegrityError` from db_backend.
  * NO FOREIGN KEYS on these tables (migration 26). Referential integrity is
    this module's job; a dangling id yields "unknown", never a failed write, and
    never a row that blocks a DPDP erasure.
  * NOTHING HERE IS A CLINICAL PATH. This module reads the clinical tables and
    writes only to its own five. No function here can create, block, delay or
    annotate an order, an administration, a vital, an alert, an escalation or a
    shadow-mode record.

════════════════════════════════════════════════════════════════════════════════
 ANONYMOUS REPORTING — WHAT IS AND IS NOT GUARANTEED
════════════════════════════════════════════════════════════════════════════════

GUARANTEED. An anonymous report stores no reporter id and no reporter role, in
the incident row or in its transition log. That is a SQLite CHECK constraint
(migration 26), not a code path, so it survives raw SQL and a future column.
`report_incident` never receives the caller's identity for an anonymous report —
the route does not pass it — so there is no variable holding it to leak.

MITIGATED, NOT ELIMINATED. `reported_at` is truncated to the DAY for anonymous
reports (`reported_at_precision = 'day'`), because a second-resolution timestamp
is itself an identifier when something else records who was making requests.

NOT GUARANTEED, AND SAID OUT LOUD ON THE SCREEN. services/audit_middleware.py
records EVERY POST with the caller's user id and the request path. An
administrator holding `audit.read` can correlate "who called POST
/api/nabh/incidents" with an anonymous report's date. The REGISTER is anonymous;
the TRANSPORT is not. A hospital that needs unlinkable reporting needs a channel
that is not this API — a physical box, or a route exempted from actor logging.
`ANONYMITY_STATEMENT` below is the sentence the API and the panel both print,
and it is asserted by tests/test_nabh.py so it cannot be quietly deleted.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, Optional

from database import db_session
from db_backend import DBIntegrityError  # sqlcipher3 raises its OWN hierarchy:
# sqlcipher3.dbapi2.IntegrityError is NOT a subclass of sqlite3.IntegrityError,
# so `except sqlite3.IntegrityError` is DEAD CODE whenever ORB_DB_ENCRYPTION_KEY
# is set — i.e. in the shipping config. Guarded by tests/test_db_exception_aliases.
from services.billing_masters import next_document_number
from services.stay_times import stay_start_sql, summary_less_ends

logger = logging.getLogger("orb.nabh")


# ═════════════════════════════════════════════════════════════════════════════
# Controlled vocabulary
# ═════════════════════════════════════════════════════════════════════════════

#: Incident types. Mirrors migration 26's CHECK exactly — a value that is not in
#: this tuple cannot be inserted, and the API rejects it with a list rather than
#: letting SQLite raise a constraint error at a clerk.
INCIDENT_TYPES: tuple[str, ...] = (
    "medication_error", "adverse_drug_reaction", "patient_fall",
    "hospital_acquired_infection", "blood_transfusion_reaction",
    "wrong_patient_procedure", "wrong_site_surgery", "retained_item",
    "delay_in_care", "diagnostic_error", "equipment_failure",
    "documentation_error", "consent_lapse", "needle_stick",
    "patient_identification", "specimen_error", "security",
    "fire_or_safety", "absconding", "other",
)

#: The harm ladder, lightest first. `near_miss` leads because a register whose
#: lightest category is "minor harm" teaches a ward that near misses are not
#: wanted, and near misses are the only free lesson a hospital gets.
SEVERITIES: tuple[str, ...] = (
    "near_miss", "no_harm", "minor", "moderate", "major", "sentinel",
)

#: Severities NABH expects a hospital to treat as externally reportable /
#: mandatory-RCA events. Used to DEFAULT the flag, never to override an operator.
SENTINEL_SEVERITIES = frozenset({"major", "sentinel"})

INCIDENT_STATUSES: tuple[str, ...] = (
    "reported", "under_investigation", "actions_pending", "closed", "void",
)

#: The lifecycle, as data. A transition not in this map does not happen — there
#: is no second copy of these rules in the route module.
TRANSITIONS: dict[str, tuple[str, ...]] = {
    "reported": ("under_investigation", "void"),
    # NOT straight to 'closed'. `close_incident` requires a recorded
    # investigation, and the only way to record one is `record_investigation`,
    # which lands in `actions_pending`. Listing 'closed' here would advertise a
    # transition that always refuses — an API that lies about what it accepts.
    "under_investigation": ("actions_pending", "void"),
    # An incident whose actions are all done can close; it can also be voided if
    # it turns out to be a duplicate discovered late.
    "actions_pending": ("closed", "void"),
    # Reopening is a real need (an action failed, a second event shows the fix
    # did not hold) and is recorded as a transition with a reason, not as an
    # edit that erases the closure.
    "closed": ("under_investigation",),
    # Terminal. A voided row is never deleted and never returns.
    "void": (),
}

ACTION_TYPES: tuple[str, ...] = (
    "corrective", "preventive", "system_change", "training", "other",
)

DISPOSITIONS: tuple[str, ...] = (
    "routine", "referred_out", "transferred_out", "lama", "dama",
    "absconded", "death", "death_within_48h",
)

#: Dispositions that count in the numerator of the mortality indicator.
DEATH_DISPOSITIONS = frozenset({"death", "death_within_48h"})
#: Dispositions that count as "against medical advice". `absconded` is a
#: DIFFERENT failure (the hospital lost the patient) and is reported beside it,
#: never folded in — folding it in moves the rate in a direction nobody chose.
AMA_DISPOSITIONS = frozenset({"lama", "dama"})

#: The sentence the API and the panel both print above the anonymous checkbox.
#: Asserted by the test suite: deleting it turns the suite red, because a
#: promise of anonymity that the platform cannot keep is the one thing in this
#: module that could cost a person their job.
ANONYMITY_STATEMENT = (
    "An anonymous report stores no reporter name, id or role. That is enforced "
    "by a database constraint, and the reporting time is recorded only to the "
    "day. It is NOT untraceable: Orb's platform audit trail records which "
    "account called which endpoint, so an administrator with audit access could "
    "correlate that with the report. If you need a channel nobody can trace, "
    "use one outside this system."
)

#: Printed on every pack. An evidence pack that claims more than it is, is the
#: failure mode this module was built to avoid.
PACK_ATTESTATION = (
    "This pack is a machine-generated extract of what Orb recorded in the stated "
    "period. Orb is not accredited and does not accredit: NABH accredits the "
    "hospital. Every indicator carries its own state, denominator and caveats. "
    "An indicator marked INSUFFICIENT DATA or NOT COLLECTED has no number, on "
    "purpose. It has not been rounded to zero, and it must not be reported as "
    "zero anywhere downstream."
)


# ═════════════════════════════════════════════════════════════════════════════
# Errors
# ═════════════════════════════════════════════════════════════════════════════

class NabhError(Exception):
    """A refusal with a machine-readable code the UI can branch on."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


# ═════════════════════════════════════════════════════════════════════════════
# Time / parsing helpers
# ═════════════════════════════════════════════════════════════════════════════

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _dt(value: Any) -> Optional[datetime]:
    """Parse any timestamp Orb writes into an aware UTC datetime.

    This module's own tables are written in one ISO format, but it READS
    `patients`, `discharge_summaries`, `admissions`, `scribe_notes`,
    `clinical_escalations` and the two override tables, which predate that
    discipline and carry `CURRENT_TIMESTAMP` strings at second resolution with
    no zone. Same parser as shadow_scorecard's, for the same reason.
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    if len(text) == 10:
        text += "T00:00:00"
    try:
        parsed = datetime.fromisoformat(text.replace(" ", "T", 1))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _iso(value: Optional[datetime]) -> Optional[str]:
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def resolve_period(period_start: Optional[str], period_end: Optional[str],
                   *, default_days: int = 90) -> tuple[datetime, datetime]:
    """[start, end) as aware UTC datetimes, from two ISO dates.

    HALF-OPEN, always. `period_end` names a DAY the caller wants included, so it
    resolves to the START of the following day: a pack for 1–31 January must
    contain the incident reported at 23:50 on the 31st. An inclusive-end bug
    here silently drops the last day of every quarter, which is exactly the kind
    of error nobody finds because the number still looks plausible.
    """
    end_day = _dt(period_end) if period_end else None
    end = ((end_day + timedelta(days=1)) if end_day else _now())
    start_day = _dt(period_start) if period_start else None
    start = start_day if start_day else (end - timedelta(days=default_days))
    if start >= end:
        raise NabhError(
            "bad_period",
            f"period_start ({period_start}) must be before period_end ({period_end})",
        )
    return start, end


def _in_window(column: str) -> str:
    """SQL for "this timestamp falls in [start, end)", NORMALISED.

    NOT a raw string comparison, and this is not a stylistic preference — it is
    a bug that silently drops a day. Orb's older tables write
    ``CURRENT_TIMESTAMP`` as ``'2025-03-01 10:00:00'`` (space separator, no
    zone); this module's own boundaries are ISO-8601 with an offset,
    ``'2025-03-01T00:00:00+00:00'``. Compared as TEXT, the space (0x20) sorts
    BEFORE the 'T' (0x54), so every row stamped on the first day of a period
    compares as EARLIER than the period start and vanishes from the numerator.
    The row count still looks plausible, which is exactly why nobody finds it.

    `datetime()` parses both spellings and both zones and yields one canonical
    form on each side, so the comparison is between like and like. A timestamp
    SQLite cannot parse yields NULL and the row is excluded — the same outcome a
    Python-side parse would reach, and it is visible because the row appears in
    no bucket rather than in the wrong one.

    Costs the index on that column. Correct and scanned beats indexed and wrong
    at the volumes an appliance holds.
    """
    return f"datetime({column}) >= datetime(?) AND datetime({column}) < datetime(?)"


def _env_int(name: str, default: int, minimum: int = 0) -> int:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        return max(minimum, int(raw))
    except ValueError:
        logger.warning("%s=%r is not an integer; using %d", name, raw, default)
        return default


def _ack_minutes() -> int:
    """Minutes a deterioration escalation has to reach a named human.

    A SINGLE, PRINTED number rather than a per-chain read of `policy_json`. The
    policy a chain was raised under can be edited, and an indicator whose
    definition changes retroactively is not evidence. This threshold appears
    verbatim in the indicator's `definition` string, so the assessor reads the
    rule the number was computed under.
    """
    return _env_int("ORB_NABH_ACK_MINUTES", 15, 1)


def _coverage_floor() -> float:
    """Minimum disposition coverage before a mortality / LAMA rate may print."""
    pct = _env_int("ORB_NABH_DISPOSITION_COVERAGE_PCT", 90, 1)
    return min(pct, 100) / 100.0


def _readmission_days() -> int:
    return _env_int("ORB_NABH_READMISSION_DAYS", 7, 1)


def _closure_days() -> int:
    return _env_int("ORB_NABH_CLOSURE_TARGET_DAYS", 30, 1)


# ═════════════════════════════════════════════════════════════════════════════
# INCIDENT REGISTER
# ═════════════════════════════════════════════════════════════════════════════

def _seq_for(db, incident_id: int) -> int:
    row = db.execute(
        "SELECT COALESCE(MAX(seq), 0) FROM nabh_incident_events WHERE incident_id = ?",
        (incident_id,),
    ).fetchone()
    return int(row[0]) + 1


def _log_event(db, incident_id: int, event_type: str, *, from_status: Optional[str],
               to_status: Optional[str], actor_user_id: Optional[str],
               detail: Optional[str] = None, metadata: Optional[dict] = None) -> None:
    """Append one transition. `actor_user_id` is None for anonymous reporting and
    that is not an omission — see the module docstring."""
    db.execute(
        "INSERT INTO nabh_incident_events (incident_id, seq, event_type, from_status, "
        "to_status, actor_user_id, detail, metadata_json, occurred_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (incident_id, _seq_for(db, incident_id), event_type, from_status, to_status,
         actor_user_id, detail, json.dumps(metadata) if metadata else None,
         _iso(_now())),
    )


def _require(value: Optional[str], field: str) -> str:
    text = (value or "").strip()
    if not text:
        raise NabhError("missing_field", f"{field} is required")
    return text


def _check_vocab(value: str, allowed: Iterable[str], field: str) -> str:
    options = tuple(allowed)
    if value not in options:
        raise NabhError(
            "bad_value",
            f"{field} must be one of {', '.join(options)} (got {value!r})",
        )
    return value


# ── Wards ────────────────────────────────────────────────────────────────────
# A ward is typed by hand in more than one place (the report form, the register
# filter, patients.ward), and 'gm-1' typed into the filter used to match nothing
# and empty the register. Every ward comparison in this module goes through the
# same key: trimmed and upper-cased, in SQL as UPPER(TRIM(col)) = ?.

def _ward_key(value: Any) -> str:
    return str(value or "").strip().upper()


def _ward_match(column: str) -> str:
    return f"UPPER(TRIM({column})) = ?"


def hospital_wards(db: Any = None) -> list[str]:
    """The hospital's wards, for the panel's ward selects: the site's declared
    wards, every ward a patient has been on, and every ward already named on an
    incident, one entry per ward (case and spacing ignored, first spelling wins,
    so a declared or charted 'GM-1' beats a hand-typed 'gm-1')."""
    names: list[str] = []
    try:
        from services.site_config_service import current_site_config
        names.extend(w for w in (current_site_config().wards or []) if w)
    except Exception:  # noqa: BLE001 - no site file is a normal dev state
        pass

    def _work(conn) -> list[str]:
        rows = conn.execute(
            "SELECT ward FROM patients WHERE TRIM(COALESCE(ward, '')) <> '' "
            "UNION ALL "
            "SELECT ward FROM nabh_incidents WHERE TRIM(COALESCE(ward, '')) <> ''"
        ).fetchall()
        return [r[0] for r in rows]

    if db is not None:
        names.extend(_work(db))
    else:
        with db_session() as conn:
            names.extend(_work(conn))

    seen: dict[str, str] = {}
    for name in names:
        key = _ward_key(name)
        if key and key not in seen:
            seen[key] = str(name).strip()
    return sorted(seen.values(), key=str.upper)


def _canonical_ward(db: Any, ward: Optional[str]) -> Optional[str]:
    """Store the hospital's spelling of a ward the reporter typed, so one ward
    never becomes two rows in a by-ward count. An unknown ward is kept as typed
    (trimmed): a report must never be refused over its ward."""
    text = str(ward or "").strip()
    if not text:
        return None
    key = _ward_key(text)
    for known in hospital_wards(db):
        if _ward_key(known) == key:
            return known
    return text


def report_incident(
    *,
    incident_type: str,
    severity: str,
    title: str,
    description: str,
    occurred_at: Optional[str] = None,
    ward: Optional[str] = None,
    department: Optional[str] = None,
    location: Optional[str] = None,
    patient_id: Optional[str] = None,
    is_anonymous: bool = False,
    reported_by: Optional[str] = None,
    reporter_role: Optional[str] = None,
    externally_reportable: Optional[bool] = None,
    db: Any = None,
) -> dict:
    """Record one incident. Returns the stored row.

    ANONYMITY. When `is_anonymous` is true this function does not merely decline
    to store `reported_by` — it OVERWRITES both identity arguments with None
    before the INSERT, so a caller that passes them (a future route, a script, a
    bulk import) cannot store them by accident. The database CHECK is the
    backstop; this is the belt.

    The reporting TIMESTAMP is truncated to the day for anonymous reports.
    Second resolution plus a platform audit trail that records who was calling
    the API is enough to name one person on a night shift.
    """
    incident_type = _check_vocab(_require(incident_type, "incident_type"),
                                INCIDENT_TYPES, "incident_type")
    severity = _check_vocab(_require(severity, "severity"), SEVERITIES, "severity")
    title = _require(title, "title")
    description = _require(description, "description")

    now = _now()
    occurred = _dt(occurred_at) or now
    if occurred > now + timedelta(minutes=5):
        raise NabhError("future_incident",
                        "The time it happened is in the future. An incident is reported "
                        "after it happens.")

    if is_anonymous:
        # Belt. The CHECK constraint is the braces.
        reported_by = None
        reporter_role = None
        reported_at = now.replace(hour=0, minute=0, second=0, microsecond=0)
        precision = "day"
    else:
        reported_at = now
        precision = "exact"

    if externally_reportable is None:
        externally_reportable = severity in SENTINEL_SEVERITIES

    reference = next_document_number("INC")["number"]

    def _work(conn) -> dict:
        cur = conn.execute(
            "INSERT INTO nabh_incidents (reference, incident_type, severity, title, "
            "description, occurred_at, reported_at, reported_at_precision, ward, "
            "department, location, patient_id, is_anonymous, reported_by, reporter_role, "
            "status, externally_reportable, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'reported', ?, ?, ?)",
            (reference, incident_type, severity, title, description, _iso(occurred),
             _iso(reported_at), precision, _canonical_ward(conn, ward), (department or None),
             (location or None), (patient_id or None), 1 if is_anonymous else 0,
             reported_by, reporter_role, 1 if externally_reportable else 0,
             _iso(now), _iso(now)),
        )
        incident_id = int(cur.lastrowid)
        _log_event(conn, incident_id, "reported", from_status=None, to_status="reported",
                   actor_user_id=None if is_anonymous else reported_by,
                   detail="Incident reported"
                          + (" anonymously" if is_anonymous else ""))
        return _load_incident(conn, incident_id)

    try:
        if db is not None:
            return _work(db)
        with db_session() as conn:
            return _work(conn)
    except DBIntegrityError as exc:
        # The anonymity CHECK is the only constraint a caller can realistically
        # trip here, and it means a code path tried to store an identity on an
        # anonymous report. Surface it as a refusal, never as a 500 that a
        # reader could mistake for a transient failure.
        raise NabhError("integrity", f"incident refused by the database: {exc}") from exc


# The reporter's NAME beside the stored id, for the screen: the register printed
# login ids. A subquery rather than a join, so the filters below stay
# unambiguous. An anonymous report stores no id, so this is NULL for it too.
_INCIDENT_SELECT = (
    "SELECT nabh_incidents.*, "
    "(SELECT name FROM users WHERE users.id = nabh_incidents.reported_by) AS reported_by_name "
    "FROM nabh_incidents"
)


def _load_incident(db, incident_id: int) -> dict:
    row = db.execute(_INCIDENT_SELECT + " WHERE id = ?", (incident_id,)).fetchone()
    if row is None:
        raise NabhError("not_found", f"incident {incident_id} does not exist")
    return _incident_dict(row)


def _incident_dict(row) -> dict:
    out = dict(row)
    out["is_anonymous"] = bool(out.get("is_anonymous"))
    out["externally_reportable"] = bool(out.get("externally_reportable"))
    factors = out.pop("contributing_factors_json", None)
    try:
        out["contributing_factors"] = json.loads(factors) if factors else []
    except (TypeError, ValueError):
        out["contributing_factors"] = []
    # Never let a reporter field carry a falsy-but-present value on an anonymous
    # row; the read side states the fact rather than leaving it to be inferred
    # from a null.
    if out["is_anonymous"]:
        out["reported_by"] = None
        out["reporter_role"] = None
        out["reported_by_name"] = None
    out["allowed_transitions"] = list(TRANSITIONS.get(out.get("status") or "", ()))
    return out


def get_incident(incident_id: int, *, db: Any = None) -> dict:
    """One incident with its actions and its full transition log."""
    def _work(conn) -> dict:
        incident = _load_incident(conn, incident_id)
        incident["actions"] = [dict(r) for r in conn.execute(
            "SELECT * FROM nabh_incident_actions WHERE incident_id = ? ORDER BY id",
            (incident_id,),
        ).fetchall()]
        incident["events"] = [dict(r) for r in conn.execute(
            "SELECT * FROM nabh_incident_events WHERE incident_id = ? ORDER BY seq",
            (incident_id,),
        ).fetchall()]
        incident["open_action_count"] = sum(
            1 for a in incident["actions"] if a["status"] == "open")
        return incident

    if db is not None:
        return _work(db)
    with db_session() as conn:
        return _work(conn)


def list_incidents(
    *,
    period_start: Optional[str] = None,
    period_end: Optional[str] = None,
    status: Optional[str] = None,
    incident_type: Optional[str] = None,
    severity: Optional[str] = None,
    ward: Optional[str] = None,
    patient_id: Optional[str] = None,
    include_void: bool = False,
    limit: int = 200,
    db: Any = None,
) -> dict:
    """The register, filtered. Ordered newest incident FIRST by occurrence."""
    start, end = resolve_period(period_start, period_end, default_days=365)
    clauses = [_in_window("occurred_at")]
    params: list[Any] = [_iso(start), _iso(end)]
    if status:
        clauses.append("status = ?")
        params.append(_check_vocab(status, INCIDENT_STATUSES, "status"))
    elif not include_void:
        clauses.append("status != 'void'")
    if incident_type:
        clauses.append("incident_type = ?")
        params.append(_check_vocab(incident_type, INCIDENT_TYPES, "incident_type"))
    if severity:
        clauses.append("severity = ?")
        params.append(_check_vocab(severity, SEVERITIES, "severity"))
    if ward:
        clauses.append(_ward_match("ward"))
        params.append(_ward_key(ward))
    if patient_id:
        clauses.append("patient_id = ?")
        params.append(patient_id)
    sql = (_INCIDENT_SELECT + " WHERE " + " AND ".join(clauses)
           + " ORDER BY occurred_at DESC, id DESC LIMIT ?")
    params.append(max(1, min(int(limit), 1000)))

    def _work(conn) -> dict:
        rows = [_incident_dict(r) for r in conn.execute(sql, params).fetchall()]
        open_counts = {}
        if rows:
            marks = ",".join("?" for _ in rows)
            for r in conn.execute(
                f"SELECT incident_id, COUNT(*) AS n FROM nabh_incident_actions "
                f"WHERE status = 'open' AND incident_id IN ({marks}) GROUP BY incident_id",
                [r["id"] for r in rows],
            ).fetchall():
                open_counts[int(r["incident_id"])] = int(r["n"])
        for r in rows:
            r["open_action_count"] = open_counts.get(int(r["id"]), 0)
        return {
            "period": {"start": _iso(start), "end": _iso(end)},
            "incidents": rows,
            "count": len(rows),
            "truncated": len(rows) >= max(1, min(int(limit), 1000)),
        }

    if db is not None:
        return _work(db)
    with db_session() as conn:
        return _work(conn)


def _transition(db, incident_id: int, to_status: str) -> dict:
    """Load an incident and refuse the move if the lifecycle does not allow it."""
    incident = _load_incident(db, incident_id)
    current = incident["status"]
    if to_status not in TRANSITIONS.get(current, ()):
        raise NabhError(
            "illegal_transition",
            f"an incident that is {current!r} cannot become {to_status!r}; "
            f"allowed from here: {', '.join(TRANSITIONS.get(current, ())) or 'nothing'}",
        )
    return incident


def start_investigation(incident_id: int, *, actor_user_id: str,
                        note: Optional[str] = None, db: Any = None) -> dict:
    def _work(conn) -> dict:
        incident = _transition(conn, incident_id, "under_investigation")
        conn.execute(
            "UPDATE nabh_incidents SET status = 'under_investigation', "
            "reopened_count = reopened_count + ?, updated_at = ? WHERE id = ?",
            (1 if incident["status"] == "closed" else 0, _iso(_now()), incident_id),
        )
        _log_event(conn, incident_id,
                   "reopened" if incident["status"] == "closed" else "investigation_started",
                   from_status=incident["status"], to_status="under_investigation",
                   actor_user_id=actor_user_id, detail=note)
        return _load_incident(conn, incident_id)

    if db is not None:
        return _work(db)
    with db_session() as conn:
        return _work(conn)


def record_investigation(
    incident_id: int, *, actor_user_id: str, summary: str,
    root_cause: Optional[str] = None,
    contributing_factors: Optional[list[str]] = None,
    db: Any = None,
) -> dict:
    """Record the investigation and move to `actions_pending`.

    An investigation SUMMARY is mandatory and a root cause is not. NABH wants an
    RCA for sentinel events; for a near miss on a Tuesday, forcing a "root
    cause" field produces the word "human error", which is the answer that ends
    every investigation that never happened.
    """
    summary = _require(summary, "summary")

    def _work(conn) -> dict:
        incident = _transition(conn, incident_id, "actions_pending")
        conn.execute(
            "UPDATE nabh_incidents SET status = 'actions_pending', investigation_summary = ?, "
            "root_cause = ?, contributing_factors_json = ?, investigated_by = ?, "
            "investigated_at = ?, updated_at = ? WHERE id = ?",
            (summary, (root_cause or None),
             json.dumps(contributing_factors) if contributing_factors else None,
             actor_user_id, _iso(_now()), _iso(_now()), incident_id),
        )
        _log_event(conn, incident_id, "investigation_recorded",
                   from_status=incident["status"], to_status="actions_pending",
                   actor_user_id=actor_user_id, detail=summary[:500])
        return _load_incident(conn, incident_id)

    if db is not None:
        return _work(db)
    with db_session() as conn:
        return _work(conn)


def add_action(
    incident_id: int, *, actor_user_id: str, description: str,
    action_type: str = "corrective", owner_user_id: Optional[str] = None,
    owner_description: Optional[str] = None, due_date: Optional[str] = None,
    db: Any = None,
) -> dict:
    description = _require(description, "description")
    action_type = _check_vocab(action_type, ACTION_TYPES, "action_type")

    def _work(conn) -> dict:
        incident = _load_incident(conn, incident_id)
        if incident["status"] in ("closed", "void"):
            raise NabhError(
                "illegal_transition",
                f"an incident that is {incident['status']!r} does not take new actions; "
                "reopen it first",
            )
        cur = conn.execute(
            "INSERT INTO nabh_incident_actions (incident_id, action_type, description, "
            "owner_user_id, owner_description, due_date, created_by, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (incident_id, action_type, description, (owner_user_id or None),
             (owner_description or None), (due_date or None), actor_user_id,
             _iso(_now()), _iso(_now())),
        )
        _log_event(conn, incident_id, "action_added", from_status=incident["status"],
                   to_status=incident["status"], actor_user_id=actor_user_id,
                   detail=description[:500], metadata={"action_id": int(cur.lastrowid)})
        return get_incident(incident_id, db=conn)

    if db is not None:
        return _work(db)
    with db_session() as conn:
        return _work(conn)


def complete_action(incident_id: int, action_id: int, *, actor_user_id: str,
                    note: Optional[str] = None, db: Any = None) -> dict:
    def _work(conn) -> dict:
        row = conn.execute(
            "SELECT * FROM nabh_incident_actions WHERE id = ? AND incident_id = ?",
            (action_id, incident_id),
        ).fetchone()
        if row is None:
            raise NabhError("not_found", f"action {action_id} is not on incident {incident_id}")
        if row["status"] != "open":
            raise NabhError("illegal_transition",
                            f"action {action_id} is already {row['status']}")
        conn.execute(
            "UPDATE nabh_incident_actions SET status = 'completed', completed_at = ?, "
            "completed_by = ?, completion_note = ?, updated_at = ? WHERE id = ?",
            (_iso(_now()), actor_user_id, (note or None), _iso(_now()), action_id),
        )
        incident = _load_incident(conn, incident_id)
        _log_event(conn, incident_id, "action_completed", from_status=incident["status"],
                   to_status=incident["status"], actor_user_id=actor_user_id,
                   detail=note, metadata={"action_id": action_id})
        return get_incident(incident_id, db=conn)

    if db is not None:
        return _work(db)
    with db_session() as conn:
        return _work(conn)


def close_incident(incident_id: int, *, actor_user_id: str,
                   closure_note: Optional[str] = None,
                   no_action_reason: Optional[str] = None, db: Any = None) -> dict:
    """Close an incident. Refuses on three grounds, each of which is a real
    finding an assessor would otherwise make:

      * no investigation recorded — a closure with no investigation is a
        deletion with extra steps;
      * open actions outstanding — "closed" with a corrective action still open
        is the single most common finding in a paper register;
      * no action AND no reason — deciding that nothing needs to change is a
        legitimate outcome, but it is a DECISION and it gets a sentence.
    """
    def _work(conn) -> dict:
        incident = _transition(conn, incident_id, "closed")
        if not (incident.get("investigation_summary") or "").strip():
            raise NabhError(
                "investigation_required",
                "record the investigation before closing, a closure with no "
                "investigation is a deletion with extra steps",
            )
        actions = conn.execute(
            "SELECT status FROM nabh_incident_actions WHERE incident_id = ?",
            (incident_id,),
        ).fetchall()
        open_actions = [a for a in actions if a["status"] == "open"]
        if open_actions:
            raise NabhError(
                "open_actions",
                f"{len(open_actions)} corrective action(s) are still open; complete or "
                "cancel them before closing",
            )
        effective = [a for a in actions if a["status"] == "completed"]
        reason = (no_action_reason or "").strip()
        if not effective and not reason:
            raise NabhError(
                "no_action_reason_required",
                "this incident is being closed with no corrective action. Record "
                "why nothing needed to change",
            )
        conn.execute(
            "UPDATE nabh_incidents SET status = 'closed', closed_by = ?, closed_at = ?, "
            "closure_note = ?, no_action_reason = ?, updated_at = ? WHERE id = ?",
            (actor_user_id, _iso(_now()), (closure_note or None),
             (reason or None), _iso(_now()), incident_id),
        )
        _log_event(conn, incident_id, "closed", from_status=incident["status"],
                   to_status="closed", actor_user_id=actor_user_id,
                   detail=closure_note or reason or None,
                   metadata={"completed_actions": len(effective)})
        return get_incident(incident_id, db=conn)

    if db is not None:
        return _work(db)
    with db_session() as conn:
        return _work(conn)


def void_incident(incident_id: int, *, actor_user_id: str, reason: str,
                  db: Any = None) -> dict:
    """Mark a report as void — a duplicate, or something that turned out not to
    have happened. THE ROW STAYS. There is no delete in this module."""
    reason = _require(reason, "reason")

    def _work(conn) -> dict:
        incident = _transition(conn, incident_id, "void")
        conn.execute(
            "UPDATE nabh_incidents SET status = 'void', voided_by = ?, voided_at = ?, "
            "void_reason = ?, updated_at = ? WHERE id = ?",
            (actor_user_id, _iso(_now()), reason, _iso(_now()), incident_id),
        )
        _log_event(conn, incident_id, "voided", from_status=incident["status"],
                   to_status="void", actor_user_id=actor_user_id, detail=reason)
        return get_incident(incident_id, db=conn)

    if db is not None:
        return _work(db)
    with db_session() as conn:
        return _work(conn)


# ═════════════════════════════════════════════════════════════════════════════
# DISCHARGE DISPOSITION
# ═════════════════════════════════════════════════════════════════════════════

def _adt_disposition_on(conn, patient_id: str, admission_id: Optional[str],
                        when: datetime) -> Optional[dict]:
    """The live disposition the end of stay (source 'adt', adt-8a) wrote for
    the stay the desk is keying, or None.

    With an admission named, only that admission's row. Without one, the row
    whose stay window holds `when`: from the stay start (the recorded arrival,
    else when the admission was written) to 24 hours after the recorded end,
    nearest the end. The same window the end of stay uses to find a row the
    desk keyed first, so either order gives one row. The window never reaches
    into a later stay of the same patient: a readmission inside the 24 hours
    is its own stay, and its disposition must not overwrite this one. None on
    a database without migration 59's columns."""
    cols = {r[1] for r in conn.execute(
        "PRAGMA table_info(nabh_discharge_dispositions)").fetchall()}
    if not {"source", "voided_at", "needs_reconciliation"} <= cols:
        return None
    rows = [dict(r) for r in conn.execute(
        "SELECT d.id, d.admission_id, d.occurred_at, "
        "COALESCE(a.admitted_at, a.created_at) AS stay_start "
        "FROM nabh_discharge_dispositions d LEFT JOIN admissions a ON a.id = d.admission_id "
        "WHERE d.patient_id = ? AND d.source = 'adt' AND d.voided_at IS NULL "
        "ORDER BY d.id DESC",
        (patient_id,),
    ).fetchall()]
    if admission_id:
        return next((r for r in rows if r["admission_id"] == admission_id), None)
    if not rows:
        return None
    stay_starts = [t for t in (_dt(r[0]) for r in conn.execute(
        "SELECT COALESCE(admitted_at, created_at) FROM admissions WHERE patient_id = ?",
        (patient_id,),
    ).fetchall()) if t is not None]
    best: Optional[tuple[float, dict]] = None
    for row in rows:
        end = _dt(row["occurred_at"])
        if end is None:
            continue
        start = _dt(row.get("stay_start")) or end - timedelta(hours=24)
        upper = end + timedelta(hours=24)
        later = [t for t in stay_starts if t > start and end <= t <= upper]
        if later:
            upper = min(upper, min(later) - timedelta(seconds=1))
        if start <= when <= upper:
            gap = abs((when - end).total_seconds())
            if best is None or gap < best[0]:
                best = (gap, row)
    return best[1] if best else None


def record_disposition(
    *, patient_id: str, disposition: str, occurred_at: Optional[str] = None,
    admission_id: Optional[str] = None, recorded_by: Optional[str] = None,
    notes: Optional[str] = None, db: Any = None,
) -> dict:
    """Record how one admission ended.

    THIS IS AN ADMINISTRATIVE RECORD, NOT A CLINICAL ONE. It does not discharge
    anybody, does not touch `patients.status`, does not write to
    `discharge_summaries`, and cannot block a discharge. It is the records desk
    writing down a fact that already happened, and it is the ONLY source in Orb
    from which mortality and LAMA can be computed at all.

    Re-recording the same discharge is an UPDATE, not a second row — the unique
    index on (patient_id, occurred_at) is what stops a correction from doubling
    a mortality denominator.
    """
    patient_id = _require(patient_id, "patient_id")
    disposition = _check_vocab(_require(disposition, "disposition"),
                               DISPOSITIONS, "disposition")
    when = _dt(occurred_at) or _now()
    if when > _now() + timedelta(minutes=5):
        raise NabhError("future_disposition", "occurred_at is in the future")

    def _work(conn) -> dict:
        # Beds and admissions (adt-8a): the end of stay may already have
        # written this stay's disposition (source 'adt'). The desk keying the
        # same stay at another instant would add a second row and double the
        # mortality numerator, so that row is corrected in place instead: the
        # desk's value is kept, as any desk correction is, and a value that
        # differs from what the end of stay recorded is flagged for a person.
        # Only rows the module wrote are matched, so a site that never turned
        # ORB_ADT on writes exactly what it always wrote.
        recorded = _adt_disposition_on(conn, patient_id, admission_id, when)
        if recorded is not None:
            conn.execute(
                "UPDATE nabh_discharge_dispositions SET disposition = ?, "
                "admission_id = COALESCE(admission_id, ?), "
                "recorded_by = COALESCE(?, recorded_by), notes = COALESCE(?, notes), "
                "needs_reconciliation = CASE WHEN disposition <> ? THEN 1 "
                "ELSE needs_reconciliation END, updated_at = ? WHERE id = ?",
                (disposition, (admission_id or None), (recorded_by or None), (notes or None),
                 disposition, _iso(_now()), recorded["id"]),
            )
            return dict(conn.execute(
                "SELECT * FROM nabh_discharge_dispositions WHERE id = ?", (recorded["id"],)
            ).fetchone())
        conn.execute(
            "INSERT INTO nabh_discharge_dispositions (patient_id, admission_id, "
            "disposition, occurred_at, recorded_by, notes, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(patient_id, occurred_at) DO UPDATE SET "
            "disposition = excluded.disposition, admission_id = excluded.admission_id, "
            "recorded_by = excluded.recorded_by, notes = excluded.notes, "
            "updated_at = excluded.updated_at",
            (patient_id, (admission_id or None), disposition, _iso(when),
             (recorded_by or None), (notes or None), _iso(_now()), _iso(_now())),
        )
        row = conn.execute(
            "SELECT * FROM nabh_discharge_dispositions WHERE patient_id = ? AND occurred_at = ?",
            (patient_id, _iso(when)),
        ).fetchone()
        return dict(row)

    if db is not None:
        return _work(db)
    with db_session() as conn:
        return _work(conn)


def list_dispositions(*, period_start: Optional[str] = None,
                      period_end: Optional[str] = None,
                      limit: int = 500, db: Any = None,
                      include_voided: bool = False) -> dict:
    """The dispositions recorded in the period. A disposition voided because
    the stay it described was reopened (migration 59, adt-8b) is left out
    unless `include_voided`: it is kept as evidence, never counted."""
    start, end = resolve_period(period_start, period_end, default_days=90)

    def _work(conn) -> dict:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM nabh_discharge_dispositions WHERE "
            + _in_window("occurred_at")
            + ("" if include_voided else " AND voided_at IS NULL")
            + " ORDER BY occurred_at DESC LIMIT ?",
            (_iso(start), _iso(end), max(1, min(int(limit), 2000))),
        ).fetchall()]
        return {"period": {"start": _iso(start), "end": _iso(end)},
                "dispositions": rows, "count": len(rows)}

    if db is not None:
        return _work(db)
    with db_session() as conn:
        return _work(conn)


def void_stay_dispositions_on(conn, *, admission_id: str, disposition_id: Optional[int] = None,
                              voided_by: Optional[str], reason: str) -> list[int]:
    """Void, never delete, the live dispositions that describe the end of one
    stay, on the caller's connection (adt-8b: a stay reopened because its end
    was recorded in error). Returns the ids voided.

    Voided: every live row keyed to this admission, and the row the end of
    stay linked (`disposition_id`) when it carries no other admission. A
    voided row keeps its disposition, time and author as evidence; every
    indicator and list skips it (voided_at IS NOT NULL). Writes no audit: the
    reopen audits the whole correction in the same transaction. Returns []
    on a database without migration 59's columns."""
    cols = {r[1] for r in conn.execute(
        "PRAGMA table_info(nabh_discharge_dispositions)").fetchall()}
    if not {"voided_at", "voided_by", "void_reason"} <= cols:
        return []
    why = (" ".join(str(reason or "").split()) or "stay reopened")[:2000]
    rows = conn.execute(
        "SELECT id FROM nabh_discharge_dispositions WHERE voided_at IS NULL "
        "AND (admission_id = ? OR (id = ? AND admission_id IS NULL)) ORDER BY id",
        (admission_id, disposition_id if disposition_id is not None else -1),
    ).fetchall()
    now = _iso(_now())
    voided: list[int] = []
    for row in rows:
        done = conn.execute(
            "UPDATE nabh_discharge_dispositions SET voided_at = ?, voided_by = ?, "
            "void_reason = ?, updated_at = ? WHERE id = ? AND voided_at IS NULL",
            (now, voided_by, why, now, row["id"]),
        )
        if done.rowcount:
            voided.append(int(row["id"]))
    return voided


# ═════════════════════════════════════════════════════════════════════════════
# INDICATOR SCAFFOLDING
# ═════════════════════════════════════════════════════════════════════════════

#: id → static description. `n_required` is the FLOOR below which the indicator
#: refuses to print, and each one is a judgement about how small a denominator
#: can be before the number is noise dressed as a finding.
INDICATOR_SPEC: dict[str, dict] = {
    "medication_error_rate": {
        "code": "QI-MED-01",
        "label": "Reported medication errors per 1,000 inpatient-days",
        "unit": "per 1,000 inpatient-days",
        "definition": (
            "Incidents recorded in the register as a medication error, divided "
            "by inpatient-days in the period, × 1,000. Voided reports excluded."),
        "denominator_label": "inpatient-days",
        "numerator_label": "reported medication errors",
        "direction": "lower_is_better",
        "n_required": 100,
        "is_proxy": False,
        "sources": ["nabh_incidents", "patients", "discharge_summaries"],
        "caveat": (
            "THIS MEASURES REPORTING, NOT SAFETY. A low number is produced both "
            "by a safe hospital and by a ward that has learned not to report. "
            "Read it beside the near-miss share and the total report volume: a "
            "register with no near misses in it is not being used."),
    },
    "medication_override_rate": {
        "code": "QI-MED-02",
        "label": "Medication safety-alert overrides per 1,000 administrations",
        "unit": "per 1,000 administrations",
        "definition": (
            "Dose-check and allergy-check overrides recorded at the bedside, "
            "divided by medication doses recorded as given in the period, "
            "× 1,000."),
        "denominator_label": "administrations given",
        "numerator_label": "safety-alert overrides",
        "direction": "lower_is_better",
        "n_required": 200,
        "is_proxy": True,
        "sources": ["medication_dose_overrides", "medication_allergy_overrides",
                    "medication_administrations"],
        "caveat": (
            "A PROXY, AND NOT A MEDICATION-ERROR RATE. An override is a "
            "clinician deliberately proceeding past a dose or allergy warning; "
            "most are clinically correct and the interlock is the thing that is "
            "wrong. It is here because it is the one medication-safety signal "
            "Orb generates without anyone choosing to report, so it does not "
            "share the reported rate's under-reporting bias."),
    },
    "patient_fall_rate": {
        "code": "QI-NUR-01",
        "label": "Reported patient falls per 1,000 inpatient-days",
        "unit": "per 1,000 inpatient-days",
        "definition": (
            "Incidents recorded in the register as a patient fall, divided by "
            "inpatient-days in the period, × 1,000. Voided reports excluded."),
        "denominator_label": "inpatient-days",
        "numerator_label": "reported falls",
        "direction": "lower_is_better",
        "n_required": 100,
        "is_proxy": False,
        "sources": ["nabh_incidents", "patients", "discharge_summaries"],
        "caveat": (
            "Reporting-dependent, exactly as the medication-error rate is. Orb "
            "has no independent fall detection, so an unreported fall is "
            "invisible here and everywhere else in this system."),
    },
    "handoff_completion": {
        "code": "QI-CONT-01",
        "label": "Structured I-PASS handoffs completed",
        "unit": "%",
        "definition": (
            "I-PASS handoff notes created in the period that reached a signed "
            "state, divided by I-PASS handoff notes created in the period."),
        "denominator_label": "handoffs drafted",
        "numerator_label": "handoffs signed",
        "direction": "higher_is_better",
        "n_required": 20,
        "is_proxy": False,
        "sources": ["scribe_notes"],
        "caveat": (
            "MEASURES DOCUMENTATION, NOT THE CONVERSATION. The 'S. Synthesis by "
            "receiver' read-back that makes I-PASS work is spoken at the bedside "
            "and is not captured anywhere in Orb. The denominator is handoffs "
            "DRAFTED, not shifts that occurred: a shift where nobody opened the "
            "handoff tool at all is invisible to this indicator."),
    },
    "escalation_ack_compliance": {
        "code": "QI-CARE-01",
        "label": "Deterioration escalations acknowledged within the response window",
        "unit": "%",
        "definition": (
            "Escalation chains raised in the period that a named clinician "
            "acknowledged within the response window, divided by chains whose "
            "outcome is decidable. Chains still inside the window at computation "
            "time are excluded and counted separately."),
        "denominator_label": "decidable escalations",
        "numerator_label": "acknowledged in time",
        "direction": "higher_is_better",
        "n_required": 10,
        "is_proxy": False,
        "sources": ["clinical_escalations"],
        "caveat": (
            "Acknowledgement is a click, not a bedside review. This says a named "
            "human took the alert, not that the patient was seen."),
    },
    "inpatient_mortality": {
        "code": "QI-OUT-01",
        "label": "Inpatient mortality",
        "unit": "%",
        "definition": (
            "Discharges recorded as a death (including a death within 48 hours), "
            "divided by all discharge dispositions recorded in the period."),
        "denominator_label": "recorded dispositions",
        "numerator_label": "deaths",
        "direction": "lower_is_better",
        "n_required": 20,
        "is_proxy": False,
        "sources": ["nabh_discharge_dispositions", "discharge_summaries"],
        "caveat": (
            "COVERAGE-GATED. The denominator is dispositions somebody RECORDED, "
            "not discharges that happened, so a partly-kept register inflates "
            "this number without limit. It refuses to print below the coverage "
            "floor and prints the coverage beside the value when it does."),
    },
    "against_medical_advice_rate": {
        "code": "QI-OUT-02",
        "label": "Discharges against medical advice (LAMA / DAMA)",
        "unit": "%",
        "definition": (
            "Discharges recorded as LAMA or DAMA, divided by all discharge "
            "dispositions recorded in the period. Absconding is counted and "
            "reported separately, never folded in."),
        "denominator_label": "recorded dispositions",
        "numerator_label": "LAMA + DAMA",
        "direction": "lower_is_better",
        "n_required": 20,
        "is_proxy": False,
        "sources": ["nabh_discharge_dispositions", "discharge_summaries"],
        "caveat": (
            "Coverage-gated on the same denominator as mortality. A LAMA rate is "
            "also a billing and a communication signal, not only a clinical one."),
    },
    "readmission_7_day": {
        "code": "QI-OUT-03",
        "label": "Unplanned re-admission within 7 days",
        "unit": "%",
        "definition": (
            "Discharges followed by a re-admission of the same patient within "
            "the follow-up window, divided by discharges whose follow-up window "
            "has fully elapsed. Discharges still inside their window are "
            "excluded and counted separately."),
        "denominator_label": "discharges with elapsed follow-up",
        "numerator_label": "re-admitted within window",
        "direction": "lower_is_better",
        "n_required": 20,
        "is_proxy": False,
        "sources": ["discharge_summaries", "admissions", "patients"],
        "caveat": (
            "ONLY RE-ADMISSIONS TO THIS HOSPITAL, and only those Orb recorded. A "
            "patient who deteriorates and presents somewhere else is invisible, "
            "which biases this number DOWNWARD, i.e. in the hospital's favour. "
            "Nor does it distinguish planned from unplanned returns: a staged "
            "procedure counts as a re-admission here."),
    },
    "incident_closure_30d": {
        "code": "QI-QI-01",
        "label": "Incidents closed within the closure target",
        "unit": "%",
        "definition": (
            "Incidents reported in the period and closed within the closure "
            "target, divided by incidents reported in the period whose target "
            "has fully elapsed. Voided reports excluded; incidents still inside "
            "their target are excluded and counted separately."),
        "denominator_label": "incidents past their target date",
        "numerator_label": "closed in time",
        "direction": "higher_is_better",
        "n_required": 5,
        "is_proxy": False,
        "sources": ["nabh_incidents"],
        "caveat": (
            "Measures the quality committee's throughput, not the quality of the "
            "investigations. A register closed quickly and shallowly scores 100 % "
            "here."),
    },
}

INDICATOR_IDS: tuple[str, ...] = tuple(INDICATOR_SPEC)

#: NABH indicators a hospital WILL be asked for and Orb cannot compute today.
#: This is the gap list, shipped as data rather than left in a slide deck: the
#: honest half of an evidence pack is the half that says what is missing.
NOT_COLLECTED_CATALOGUE: tuple[dict[str, str], ...] = (
    {"code": "QI-INF-01", "label": "Hand-hygiene compliance",
     "reason": "Orb captures no hand-hygiene observation audit.",
     "needed": "An observation round: observer, opportunity, moment, compliance."},
    {"code": "QI-INF-02",
     "label": "Device-associated infection rates (CAUTI / CLABSI / VAP)",
     "reason": "Orb records no device insertion, removal or device-days, so "
               "neither numerator nor denominator exists.",
     "needed": "Device line/catheter/ventilator start and stop per patient, plus "
               "a surveillance definition applied by the infection-control nurse."},
    {"code": "QI-INF-03", "label": "Surgical site infection rate",
     "reason": "Surgeries are scheduled in Orb but post-operative infection "
               "surveillance and the 30/90-day follow-up window are not.",
     "needed": "Post-discharge SSI surveillance capture linked to the surgery."},
    {"code": "QI-ASS-01", "label": "Time to initial assessment",
     "reason": "No timestamped initial-assessment record exists; an admission "
               "row's creation time is a clerical event, not a clinical one.",
     "needed": "An initial-assessment note with its own completion timestamp."},
    {"code": "QI-OUT-04", "label": "Return to ICU within 48 hours",
     "reason": "Orb has no ICU admission/discharge record distinct from a ward "
               "move, so a return cannot be detected.",
     "needed": "Bed-level transfers with unit type, or an explicit ICU episode."},
    {"code": "QI-UTL-01", "label": "Bed occupancy rate",
     "reason": "Inpatient-days are computable, but the hospital's licensed bed "
               "count is not configured anywhere in Orb, so the denominator is "
               "missing. A guessed bed count would make this indicator "
               "arbitrary in either direction.",
     "needed": "A configured bed count per ward."},
    {"code": "QI-BLD-01", "label": "Blood transfusion reactions per unit transfused",
     "reason": "Reactions can be reported in the register, but Orb records no "
               "transfusion events, so there is no denominator.",
     "needed": "Transfusion issue and administration records."},
)


#: What a quality committee calls each record an indicator is computed from.
#: The spec lists table names because they document the query; the screen and
#: the evidence pack showed them verbatim ("nabh_discharge_dispositions").
SOURCE_LABELS: dict[str, str] = {
    "nabh_incidents": "Incident register",
    "patients": "Admission records",
    "admissions": "Admission records",
    "discharge_summaries": "Discharge summaries",
    "medication_dose_overrides": "Dose-check overrides",
    "medication_allergy_overrides": "Allergy-check overrides",
    "medication_administrations": "Medication administration record",
    "scribe_notes": "I-PASS handoff notes",
    "clinical_escalations": "Deterioration escalations",
    "nabh_discharge_dispositions": "Discharge dispositions",
}


def _source_labels(sources) -> list[str]:
    """Plain names, each once, in the spec's order."""
    out: list[str] = []
    for source in sources:
        label = SOURCE_LABELS.get(source, source.replace("_", " ").capitalize())
        if label not in out:
            out.append(label)
    return out


def _new_indicator(indicator_id: str) -> dict:
    spec = INDICATOR_SPEC[indicator_id]
    return {
        "id": indicator_id,
        "code": spec["code"],
        "label": spec["label"],
        "definition": spec["definition"],
        "unit": spec["unit"],
        "state": "not_computable",
        "value": None,
        "display": None,
        "numerator": None,
        "denominator": None,
        "numerator_label": spec["numerator_label"],
        "denominator_label": spec["denominator_label"],
        "n_observed": 0,
        "n_required": _env_int(f"ORB_NABH_MIN_N_{indicator_id.upper()}",
                               spec["n_required"], 0),
        "direction": spec["direction"],
        "is_proxy": spec["is_proxy"],
        "caveat": spec["caveat"],
        "sources": _source_labels(spec["sources"]),
        "blockers": [],
        "excluded": {},
        "detail": {},
    }


def _finalise(indicator: dict) -> dict:
    """The last gate before a number leaves this module.

    A `reported` indicator must have something to print, a positive denominator,
    n at or above the floor, and no blockers. Anything else keeps its arithmetic
    under `detail.withheld_*` for the audit trail and hands the view NOTHING —
    because a view given a number will render it, and a rendered number is a
    claim. This is the one function that stops "0 %" on morning one.
    """
    if indicator["blockers"]:
        if indicator["state"] not in ("not_collected",):
            indicator["state"] = "not_computable"
    elif indicator["state"] == "reported":
        if indicator["value"] is None and indicator["display"] is None:
            indicator["state"] = "not_computable"
        elif not indicator["denominator"] or indicator["denominator"] <= 0:
            indicator["state"] = "insufficient_data"
        elif indicator["n_required"] > 0 and indicator["n_observed"] < indicator["n_required"]:
            indicator["state"] = "insufficient_data"
    if indicator["state"] != "reported":
        indicator["detail"]["withheld_value"] = indicator["value"]
        indicator["detail"]["withheld_display"] = indicator["display"]
        indicator["value"] = None
        indicator["display"] = None
    return indicator


def _pct(numerator: int, denominator: int) -> float:
    return round(100.0 * numerator / denominator, 2)


def _rate_per_1000(numerator: int, denominator: float) -> float:
    return round(1000.0 * numerator / denominator, 3)


# ═════════════════════════════════════════════════════════════════════════════
# Shared denominators
# ═════════════════════════════════════════════════════════════════════════════

def _ward_filter(rows: list[dict], ward: Optional[str], key: str = "ward") -> list[dict]:
    if not ward:
        return rows
    wanted = _ward_key(ward)
    return [r for r in rows if _ward_key(r.get(key)) == wanted]


def _overlap_hours(a_start: datetime, a_end: datetime,
                   b_start: datetime, b_end: datetime) -> float:
    lo = max(a_start, b_start)
    hi = min(a_end, b_end)
    return max(0.0, (hi - lo).total_seconds() / 3600.0)


def _per_stay_counting_on(db) -> bool:
    """True when the beds and admissions module is not off AND at least one
    admission carries what only it writes (an arrival time or an end type,
    migration 58). Then each stay is visible and inpatient days count per
    stay. With the module off nothing changes, whatever the data says."""
    import config

    if config.module_state("adt") == "off":
        return False
    cols = {r[1] for r in db.execute("PRAGMA table_info(admissions)").fetchall()}
    if not {"admitted_at", "discharge_type", "discharged_at"} <= cols:
        return False
    return db.execute(
        "SELECT 1 FROM admissions WHERE discharge_type IS NOT NULL OR admitted_at IS NOT NULL LIMIT 1"
    ).fetchone() is not None


def _inpatient_days_per_stay(db, start: datetime, horizon: datetime,
                             ward: Optional[str]) -> dict:
    """Inpatient-days inside [start, horizon), one stay at a time (adt-8a).

    Each admission counts from its start (the recorded arrival time, else when
    the admission was written) to its end: the end of stay the module recorded
    (admissions.discharged_at), else, for a stay still active, the end of the
    window, else the first discharge summary at or after its start, else the
    patient's last vital signs after its start. A ward filter takes the ward
    each stay was recorded on. A patient with no admission row at all (records
    written before admissions existed) is counted from patients.admission_date
    as the older method did, so nobody counted before drops out."""
    stays = [dict(r) for r in db.execute(
        "SELECT a.id, a.patient_id, a.ward, a.status, a.admitted_at, a.created_at, "
        "a.discharged_at FROM admissions a"
    ).fetchall()]
    with_stays = {s["patient_id"] for s in stays}
    stays = _ward_filter(stays, ward)

    summaries: dict[str, list[datetime]] = {}
    for row in db.execute("SELECT patient_id, discharged_at FROM discharge_summaries").fetchall():
        when = _dt(row["discharged_at"])
        if when:
            summaries.setdefault(row["patient_id"], []).append(when)
    for times in summaries.values():
        times.sort()
    last_vital: dict[str, datetime] = {}
    for row in db.execute(
        "SELECT patient_id, MAX(timestamp) AS t FROM vitals WHERE voided_at IS NULL "
        "GROUP BY patient_id"
    ).fetchall():
        when = _dt(row["t"])
        if when:
            last_vital[row["patient_id"]] = when

    hours = 0.0
    counted: set[str] = set()
    open_stays = assumed_ends = stay_count = 0
    for stay in stays:
        stay_start = _dt(stay.get("admitted_at")) or _dt(stay.get("created_at"))
        if stay_start is None:
            continue
        stay_end = _dt(stay.get("discharged_at"))
        if stay_end is None or stay_end < stay_start:
            if (stay.get("status") or "active") == "active":
                stay_end = horizon
                open_stays += 1
            else:
                later = [t for t in summaries.get(stay["patient_id"], []) if t >= stay_start]
                if later:
                    stay_end = later[0]
                else:
                    vital = last_vital.get(stay["patient_id"])
                    stay_end = vital if vital and vital >= stay_start else stay_start
                    assumed_ends += 1
        overlap = _overlap_hours(stay_start, stay_end, start, horizon)
        if overlap > 0:
            hours += overlap
            counted.add(stay["patient_id"])
            stay_count += 1

    # Patients with no admission row: the older, per-patient method.
    legacy = [dict(r) for r in db.execute(
        "SELECT id, ward, admission_date, status FROM patients "
        "WHERE COALESCE(status, 'admitted') <> 'registered'"
    ).fetchall()]
    legacy = _ward_filter([p for p in legacy if p["id"] not in with_stays], ward)
    for patient in legacy:
        stay_start = _dt(patient.get("admission_date"))
        if stay_start is None:
            continue
        times = summaries.get(patient["id"], [])
        stay_end = times[-1] if times else None
        if stay_end is None or stay_end <= stay_start:
            if (patient.get("status") or "admitted").strip() == "admitted":
                stay_end = horizon
                open_stays += 1
            else:
                stay_end = last_vital.get(patient["id"]) or stay_start
                assumed_ends += 1
        overlap = _overlap_hours(stay_start, stay_end, start, horizon)
        if overlap > 0:
            hours += overlap
            counted.add(patient["id"])
            stay_count += 1

    return {
        "inpatient_days": round(hours / 24.0, 1),
        "patients": len(counted),
        "stays": stay_count,
        "open_stays": open_stays,
        "assumed_ends": assumed_ends,
        "method": (
            "Counted per stay: each admission from its start (the recorded arrival "
            "time, else when the admission was recorded) to its recorded end of stay, "
            "within the period and up to now. A stay still open counts to the end of "
            "the period; an ended stay with no recorded end is closed at the first "
            "discharge summary after it began, else at the patient's last recorded "
            "vital signs. Repeat stays inside the period are each counted. A patient "
            "with no admission record is counted from their admission date."),
    }


def _inpatient_days(db, start: datetime, end: datetime, ward: Optional[str]) -> dict:
    """Inpatient-days inside [start, end), and an honest account of how.

    KNOWN UNDER-COUNT, AND ITS DIRECTION. `patients` holds ONE row per patient
    carrying the CURRENT admission, so a patient admitted twice inside the
    window contributes only their latest stay. That UNDERSTATES the denominator,
    which OVERSTATES every per-1,000-inpatient-day rate computed from it — i.e.
    it makes the hospital look worse, never better. That is the only direction
    an accreditation pack may be wrong in, which is why this is shipped with the
    bias stated rather than held back for a stay table that does not exist yet.

    `patients.ward` is likewise the CURRENT ward, not the ward the patient
    occupied on each day, so a ward filter attributes the whole stay to where
    the patient is now.
    """
    horizon = min(end, _now())
    if horizon <= start:
        return {"inpatient_days": 0.0, "patients": 0, "open_stays": 0,
                "assumed_ends": 0, "method": "window has not started"}

    # Beds and admissions (adt-8a): once the module is on and has recorded
    # stays, count every stay, not only each patient's current one.
    if _per_stay_counting_on(db):
        return _inpatient_days_per_stay(db, start, horizon, ward)

    # A 'registered' row (migration 52) is a person on the patient index who
    # has never been admitted: they have no stay, so they add no inpatient-day.
    # Excluded at the source so a registration desk can never feed this
    # denominator; until registration is switched on no such row exists.
    patients = [dict(r) for r in db.execute(
        "SELECT id, ward, admission_date, status FROM patients "
        "WHERE COALESCE(status, 'admitted') <> 'registered'"
    ).fetchall()]
    patients = _ward_filter(patients, ward)

    discharges: dict[str, datetime] = {}
    for row in db.execute(
        "SELECT patient_id, MAX(discharged_at) AS d FROM discharge_summaries "
        "GROUP BY patient_id"
    ).fetchall():
        when = _dt(row["d"])
        if when:
            discharges[row["patient_id"]] = when

    last_vital: dict[str, datetime] = {}
    for row in db.execute(
        "SELECT patient_id, MAX(timestamp) AS t FROM vitals WHERE voided_at IS NULL "
        "GROUP BY patient_id"
    ).fetchall():
        when = _dt(row["t"])
        if when:
            last_vital[row["patient_id"]] = when

    hours = 0.0
    counted: set[str] = set()
    open_stays = 0
    assumed_ends = 0
    for patient in patients:
        stay_start = _dt(patient.get("admission_date"))
        if stay_start is None:
            continue
        status = (patient.get("status") or "admitted").strip()
        stay_end = discharges.get(patient["id"])
        if stay_end is None or stay_end <= stay_start:
            if status == "admitted":
                stay_end = horizon
                open_stays += 1
            else:
                stay_end = last_vital.get(patient["id"]) or stay_start
                assumed_ends += 1
        overlap = _overlap_hours(stay_start, stay_end, start, horizon)
        if overlap > 0:
            hours += overlap
            counted.add(patient["id"])

    return {
        # One decimal: the screen printed "199.9583". The rates are computed
        # from this same figure, so the printed numbers reproduce each other.
        "inpatient_days": round(hours / 24.0, 1),
        "patients": len(counted),
        "open_stays": open_stays,
        "assumed_ends": assumed_ends,
        "method": (
            "Counted from each patient's admission date to their latest discharge, "
            "within the period and up to now. A patient still admitted counts to the "
            "end of the period; a discharged patient with no discharge summary is "
            "closed at their last recorded vital signs. Only each patient's current "
            "admission is visible, so repeat stays inside the period are under-counted, "
            "which raises every rate computed on this denominator."),
    }


def _register_in_use(db, start: datetime, end: datetime, ward: Optional[str]) -> dict:
    """Did anybody use the incident register in this window at all?

    THE GATE THAT STOPS THE WORST NUMBER IN THE PACK. Zero medication errors over
    a quarter in which the register recorded nothing whatsoever is a fact about
    the register. It is reported as NOT COLLECTED, and the difference between
    that and "0.0 per 1,000 inpatient-days" is the difference between a quality
    committee that goes and looks and one that files the page.
    """
    clauses = [_in_window("occurred_at"), "status != 'void'"]
    params: list[Any] = [_iso(start), _iso(end)]
    if ward:
        clauses.append(_ward_match("ward"))
        params.append(_ward_key(ward))
    rows = db.execute(
        "SELECT incident_type, severity FROM nabh_incidents WHERE " + " AND ".join(clauses),
        params,
    ).fetchall()
    by_type = Counter(r["incident_type"] for r in rows)
    by_severity = Counter(r["severity"] for r in rows)
    return {
        "total": len(rows),
        "in_use": len(rows) > 0,
        "by_type": dict(by_type),
        "by_severity": dict(by_severity),
        "near_miss_share": (round(by_severity.get("near_miss", 0) / len(rows), 4)
                            if rows else None),
    }


_REGISTER_UNUSED_BLOCKER = (
    "The incident register recorded nothing in this period, so this indicator "
    "has no numerator AND no evidence that reporting was happening at all. A "
    "zero here would be a statement about the register, not about the hospital."
)


# ═════════════════════════════════════════════════════════════════════════════
# INDICATORS
# ═════════════════════════════════════════════════════════════════════════════

def _register_rate_indicator(indicator_id: str, incident_type: str, db,
                             start: datetime, end: datetime, ward: Optional[str],
                             usage: dict, bed_days: dict) -> dict:
    """Shared shape for 'reported X per 1,000 inpatient-days'."""
    indicator = _new_indicator(indicator_id)
    days = float(bed_days["inpatient_days"])
    count = int(usage["by_type"].get(incident_type, 0))

    indicator["numerator"] = count
    indicator["denominator"] = round(days, 1)
    indicator["n_observed"] = int(days)
    indicator["detail"] = {
        "inpatient_days": bed_days,
        "register_reports_all_types": usage["total"],
        "near_miss_share": usage["near_miss_share"],
    }

    if not usage["in_use"]:
        indicator["state"] = "not_collected"
        indicator["blockers"] = [_REGISTER_UNUSED_BLOCKER]
        return _finalise(indicator)
    if days <= 0:
        indicator["state"] = "not_collected"
        indicator["blockers"] = [
            "No inpatient-days fall inside this period. There is no ward "
            "activity to normalise against."
        ]
        return _finalise(indicator)

    indicator["value"] = _rate_per_1000(count, days)
    indicator["display"] = f"{indicator['value']:.2f} per 1,000 inpatient-days"
    indicator["state"] = "reported"
    return _finalise(indicator)


def _medication_override_rate(db, start: datetime, end: datetime,
                              ward: Optional[str]) -> dict:
    indicator = _new_indicator("medication_override_rate")
    ward_join = " JOIN patients p ON p.id = t.patient_id AND UPPER(TRIM(p.ward)) = ?" if ward else ""
    ward_params: list[Any] = [_ward_key(ward)] if ward else []

    dose = db.execute(
        "SELECT COUNT(*) FROM medication_dose_overrides t" + ward_join
        + " WHERE " + _in_window("t.overridden_at"),
        (*ward_params, _iso(start), _iso(end)),
    ).fetchone()[0]
    allergy = db.execute(
        "SELECT COUNT(*) FROM medication_allergy_overrides t" + ward_join
        + " WHERE " + _in_window("t.overridden_at"),
        (*ward_params, _iso(start), _iso(end)),
    ).fetchone()[0]
    given = db.execute(
        "SELECT COUNT(*) FROM medication_administrations t" + ward_join
        + " WHERE t.status = 'given' AND " + _in_window("t.administered_at"),
        (*ward_params, _iso(start), _iso(end)),
    ).fetchone()[0]

    overrides = int(dose) + int(allergy)
    indicator["numerator"] = overrides
    indicator["denominator"] = int(given)
    indicator["n_observed"] = int(given)
    indicator["detail"] = {
        "dose_guard_overrides": int(dose),
        "allergy_interlock_overrides": int(allergy),
        "administrations_given": int(given),
    }

    if given <= 0:
        indicator["state"] = "not_collected"
        indicator["blockers"] = [
            "No medication administrations were recorded in this period, so "
            "there is nothing to compute an override rate against. If the ward "
            "is administering medicines, the eMAR is not being used."
        ]
        return _finalise(indicator)

    # A genuine 0 IS reportable here, and this is the one indicator where that
    # is true: the denominator is machine-generated by the eMAR, so "no
    # overrides against 4,000 administrations" is a finding rather than an
    # absence of reporting.
    indicator["value"] = _rate_per_1000(overrides, float(given))
    indicator["display"] = f"{indicator['value']:.2f} per 1,000 administrations"
    indicator["state"] = "reported"
    return _finalise(indicator)


def _handoff_completion(db, start: datetime, end: datetime, ward: Optional[str]) -> dict:
    indicator = _new_indicator("handoff_completion")
    ward_join = " JOIN patients p ON p.id = n.patient_id AND UPPER(TRIM(p.ward)) = ?" if ward else ""
    ward_params: list[Any] = [_ward_key(ward)] if ward else []
    rows = db.execute(
        "SELECT n.status AS status FROM scribe_notes n" + ward_join
        + " WHERE n.note_format = 'handoff' AND " + _in_window("n.created_at"),
        (*ward_params, _iso(start), _iso(end)),
    ).fetchall()

    statuses = Counter((r["status"] or "draft") for r in rows)
    # 'superseded' is a note that WAS signed and then replaced by a newer signed
    # version. Counting it as incomplete would penalise a ward for correcting a
    # handoff, which is the behaviour the indicator should encourage.
    completed = statuses.get("signed", 0) + statuses.get("superseded", 0)
    total = len(rows)

    indicator["numerator"] = completed
    indicator["denominator"] = total
    indicator["n_observed"] = total
    indicator["detail"] = {"by_status": dict(statuses)}

    if total == 0:
        indicator["state"] = "not_collected"
        indicator["blockers"] = [
            "No I-PASS handoff was drafted in this period. The indicator has no "
            "denominator, which is itself the finding: the handoff tool was "
            "not used, so nothing can be said about handoff quality."
        ]
        return _finalise(indicator)

    indicator["value"] = _pct(completed, total)
    indicator["display"] = f"{indicator['value']:.1f}%"
    indicator["state"] = "reported"
    return _finalise(indicator)


def _escalation_ack_compliance(db, start: datetime, end: datetime,
                               ward: Optional[str]) -> dict:
    indicator = _new_indicator("escalation_ack_compliance")
    minutes = _ack_minutes()
    window = timedelta(minutes=minutes)
    indicator["definition"] += f" The response window is {minutes} minutes."

    ward_join = " JOIN patients p ON p.id = e.patient_id AND UPPER(TRIM(p.ward)) = ?" if ward else ""
    ward_params: list[Any] = [_ward_key(ward)] if ward else []
    rows = [dict(r) for r in db.execute(
        "SELECT e.id AS id, e.trigger AS trigger, e.raised_at AS raised_at, "
        "e.acknowledged_at AS acknowledged_at, e.acknowledged_by AS acknowledged_by, "
        "e.status AS status FROM clinical_escalations e" + ward_join
        + " WHERE " + _in_window("e.raised_at"),
        (*ward_params, _iso(start), _iso(end)),
    ).fetchall()]

    now = _now()
    decidable = 0
    in_time = 0
    censored = 0
    unnamed_acks = 0
    late_minutes: list[float] = []
    by_trigger: dict[str, dict[str, int]] = {}
    for row in rows:
        raised = _dt(row["raised_at"])
        if raised is None:
            continue
        acked = _dt(row["acknowledged_at"])
        # An acknowledgement with no `acknowledged_by` is still an
        # acknowledgement, but the indicator's label says "a NAMED clinician",
        # so the unnamed ones are counted and surfaced rather than quietly
        # promoted or quietly dropped.
        if acked is not None and not row["acknowledged_by"]:
            unnamed_acks += 1
        trigger = (row["trigger"] or "unspecified")
        bucket = by_trigger.setdefault(trigger, {"decidable": 0, "in_time": 0, "censored": 0})
        if acked is not None:
            delay = (acked - raised).total_seconds() / 60.0
            decidable += 1
            bucket["decidable"] += 1
            if delay <= minutes:
                in_time += 1
                bucket["in_time"] += 1
            else:
                late_minutes.append(round(delay, 2))
        elif now >= raised + window:
            # Unacknowledged and the window has closed: a decidable failure.
            decidable += 1
            bucket["decidable"] += 1
        else:
            # RIGHT-CENSORED. Raised four minutes ago with a fifteen-minute
            # window is not a miss; counting it as one would make this number a
            # function of when the report was run.
            censored += 1
            bucket["censored"] += 1

    indicator["numerator"] = in_time
    indicator["denominator"] = decidable
    indicator["n_observed"] = decidable
    indicator["excluded"] = {
        "still_inside_response_window": censored,
        "why": ("An escalation raised moments before this pack was generated "
                "cannot yet have missed its window. Counting it as a miss would "
                "make the indicator depend on when the report was run."),
    }
    indicator["detail"] = {
        "response_window_minutes": minutes,
        "escalations_raised": len(rows),
        "by_trigger": by_trigger,
        "late_acknowledgement_minutes": sorted(late_minutes),
        "acknowledged_without_a_named_clinician": unnamed_acks,
    }

    if not rows:
        indicator["state"] = "not_collected"
        indicator["blockers"] = [
            "No deterioration escalation was raised in this period. With no "
            "escalations there is nothing to be compliant with; a 100% here "
            "would be a claim about a ward that never called for help."
        ]
        return _finalise(indicator)
    if decidable == 0:
        indicator["state"] = "insufficient_data"
        return _finalise(indicator)

    indicator["value"] = _pct(in_time, decidable)
    indicator["display"] = f"{indicator['value']:.1f}%"
    indicator["state"] = "reported"
    return _finalise(indicator)


def _summary_less_ends_in(db, start: datetime, end: datetime,
                          ward: Optional[str]) -> list[dict]:
    """Ends of stay inside [start, end) that the beds and admissions module
    recorded with no discharge summary (services/stay_times.py, adt-8c),
    reopened ones excluded. A ward filter takes the ward the ended stay was
    recorded on. Empty with ORB_ADT off: no such end exists."""
    rows = summary_less_ends(db, since=_iso(start), until=_iso(end))
    return _ward_filter(rows, ward)


def _disposition_base(db, start: datetime, end: datetime, ward: Optional[str]) -> dict:
    """The shared, coverage-gated denominator for mortality and LAMA.

    Voided dispositions (migration 59: a stay reopened as recorded in error)
    are never counted. The end of stay (adt-8a) writes at most one live row
    per admission, so an automatic row and a records-desk row for one death
    are one row here, not two."""
    ward_join = " JOIN patients p ON p.id = d.patient_id AND UPPER(TRIM(p.ward)) = ?" if ward else ""
    ward_params: list[Any] = [_ward_key(ward)] if ward else []
    rows = [dict(r) for r in db.execute(
        "SELECT d.disposition AS disposition FROM nabh_discharge_dispositions d" + ward_join
        + " WHERE " + _in_window("d.occurred_at") + " AND d.voided_at IS NULL",
        (*ward_params, _iso(start), _iso(end)),
    ).fetchall()]

    summary_join = " JOIN patients p ON p.id = s.patient_id AND UPPER(TRIM(p.ward)) = ?" if ward else ""
    observed = int(db.execute(
        "SELECT COUNT(*) FROM discharge_summaries s" + summary_join
        + " WHERE " + _in_window("s.discharged_at"),
        (*ward_params, _iso(start), _iso(end)),
    ).fetchone()[0])
    # Ends the beds and admissions module recorded with no summary are
    # discharge events too (adt-8c); a ward filter takes the ward the ended
    # stay was on. None exist with ORB_ADT off.
    observed += len(_summary_less_ends_in(db, start, end, ward))

    recorded = len(rows)
    pool = max(recorded, observed)
    coverage = (recorded / pool) if pool else 0.0
    return {
        "rows": Counter(r["disposition"] for r in rows),
        "recorded": recorded,
        "discharge_events_observed": observed,
        "coverage": round(coverage, 4),
        "coverage_floor": _coverage_floor(),
    }


def _disposition_indicator(indicator_id: str, numerator_set: frozenset[str],
                           base: dict, extra_detail: dict) -> dict:
    indicator = _new_indicator(indicator_id)
    counts = base["rows"]
    recorded = base["recorded"]
    numerator = sum(counts.get(k, 0) for k in numerator_set)

    indicator["numerator"] = numerator
    indicator["denominator"] = recorded
    indicator["n_observed"] = recorded
    indicator["detail"] = {
        "by_disposition": dict(counts),
        "dispositions_recorded": recorded,
        "discharge_events_observed": base["discharge_events_observed"],
        "coverage": base["coverage"],
        "coverage_floor": base["coverage_floor"],
        **extra_detail,
    }

    if recorded == 0:
        indicator["state"] = "not_collected"
        indicator["blockers"] = [
            "No discharge disposition was recorded in this period. Orb cannot "
            "work out a death or a LAMA from any other record (a patient's status "
            "says only admitted, discharged or transferred), so this indicator has "
            "nothing to count until the records desk records one."
        ]
        return _finalise(indicator)
    if base["coverage"] < base["coverage_floor"]:
        indicator["state"] = "insufficient_data"
        indicator["blockers"] = []
        indicator["detail"]["coverage_shortfall"] = (
            f"Dispositions were recorded for {base['coverage'] * 100:.0f}% of the "
            f"{base['discharge_events_observed']} discharge events Orb observed in "
            f"this period. Below {base['coverage_floor'] * 100:.0f}% this rate is "
            "arithmetic on a biased sample: the discharges somebody bothered to "
            "record are not a random sample of discharges."
        )
        # Keep the arithmetic for the audit trail; _finalise moves it to
        # detail.withheld_value and hands the view nothing.
        indicator["value"] = _pct(numerator, recorded)
        indicator["display"] = f"{indicator['value']:.1f}%"
        return _finalise(indicator)

    indicator["value"] = _pct(numerator, recorded)
    indicator["display"] = f"{indicator['value']:.1f}%"
    indicator["state"] = "reported"
    return _finalise(indicator)


def _readmission(db, start: datetime, end: datetime, ward: Optional[str]) -> dict:
    indicator = _new_indicator("readmission_7_day")
    days = _readmission_days()
    follow_up = timedelta(days=days)
    indicator["label"] = f"Unplanned re-admission within {days} days"
    indicator["definition"] += f" The follow-up window is {days} days."

    ward_join = " JOIN patients p ON p.id = s.patient_id AND UPPER(TRIM(p.ward)) = ?" if ward else ""
    ward_params: list[Any] = [_ward_key(ward)] if ward else []
    discharges = [dict(r) for r in db.execute(
        "SELECT s.patient_id AS patient_id, s.discharged_at AS discharged_at "
        "FROM discharge_summaries s" + ward_join
        + " WHERE " + _in_window("s.discharged_at"),
        (*ward_params, _iso(start), _iso(end)),
    ).fetchall()]
    # Ends the beds and admissions module recorded with no summary (adt-8c)
    # are discharges this indicator follows up too, except a death: a patient
    # who died cannot be re-admitted, and counting them as 'not re-admitted'
    # would flatter the rate. Excluded by the recorded end type, never by
    # guesswork. None exist with ORB_ADT off.
    deaths_excluded = 0
    for end_row in _summary_less_ends_in(db, start, end, ward):
        if end_row.get("end_type") == "death":
            deaths_excluded += 1
            continue
        discharges.append({"patient_id": end_row["patient_id"],
                           "discharged_at": end_row["ended_at"]})

    # Admission events, from BOTH places Orb records one. `admissions` is the
    # modern path; `patients.admission_date` is the legacy one and is still
    # written by parts of the system. Using only the first would silently miss
    # re-admissions — a miss that biases this rate DOWNWARD, i.e. in the
    # hospital's favour, which is the direction a pack must never be wrong in.
    # De-duplicated to the minute so a re-admission recorded in both tables is
    # one event, not two. A stay begins at its recorded arrival when the beds
    # and admissions module recorded one (services/stay_times.py), else when
    # the admission was written; with ORB_ADT off that is always created_at.
    events: dict[str, set[datetime]] = {}
    for row in db.execute(
        f"SELECT a.patient_id, {stay_start_sql('a')} AS created_at FROM admissions a"
    ).fetchall():
        when = _dt(row["created_at"])
        if when:
            events.setdefault(row["patient_id"], set()).add(when.replace(second=0, microsecond=0))
    for row in db.execute(
        "SELECT id, admission_date FROM patients WHERE admission_date IS NOT NULL"
    ).fetchall():
        when = _dt(row["admission_date"])
        if when:
            events.setdefault(row["id"], set()).add(when.replace(second=0, microsecond=0))

    now = _now()
    eligible = 0
    readmitted = 0
    censored = 0
    for row in discharges:
        left = _dt(row["discharged_at"])
        if left is None:
            continue
        if now < left + follow_up:
            # RIGHT-CENSORED: the follow-up clock has not finished running.
            censored += 1
            continue
        eligible += 1
        for when in events.get(row["patient_id"], ()):  # noqa: SIM110 — readability
            if left < when <= left + follow_up:
                readmitted += 1
                break

    indicator["numerator"] = readmitted
    indicator["denominator"] = eligible
    indicator["n_observed"] = eligible
    indicator["excluded"] = {
        "follow_up_not_elapsed": censored,
        "why": (f"A patient discharged less than {days} days ago cannot yet be "
                "known not to have come back. Counting them as 'not re-admitted' "
                "is how this indicator gets quietly optimistic near the end of "
                "every reporting period."),
    }
    indicator["detail"] = {
        "follow_up_days": days,
        "discharges_in_period": len(discharges),
        "eligible_discharges": eligible,
        "censored_discharges": censored,
    }
    if deaths_excluded:
        indicator["detail"]["deaths_excluded"] = deaths_excluded

    if not discharges:
        indicator["state"] = "not_collected"
        indicator["blockers"] = [
            "No discharge was recorded in this period, so there is nothing to "
            "follow up."
        ]
        return _finalise(indicator)
    if eligible == 0:
        indicator["state"] = "insufficient_data"
        indicator["blockers"] = []
        return _finalise(indicator)

    indicator["value"] = _pct(readmitted, eligible)
    indicator["display"] = f"{indicator['value']:.1f}%"
    indicator["state"] = "reported"
    return _finalise(indicator)


def _incident_closure(db, start: datetime, end: datetime, ward: Optional[str],
                      usage: dict) -> dict:
    indicator = _new_indicator("incident_closure_30d")
    target_days = _closure_days()
    target = timedelta(days=target_days)
    indicator["label"] = f"Incidents closed within {target_days} days"
    indicator["definition"] += f" The closure target is {target_days} days."

    clauses = [_in_window("reported_at"), "status != 'void'"]
    params: list[Any] = [_iso(start), _iso(end)]
    if ward:
        clauses.append(_ward_match("ward"))
        params.append(_ward_key(ward))
    rows = [dict(r) for r in db.execute(
        "SELECT reported_at, status, closed_at FROM nabh_incidents WHERE "
        + " AND ".join(clauses), params,
    ).fetchall()]

    now = _now()
    eligible = 0
    in_time = 0
    censored = 0
    still_open_past_target = 0
    for row in rows:
        reported = _dt(row["reported_at"])
        if reported is None:
            continue
        closed = _dt(row["closed_at"]) if row["status"] == "closed" else None
        if closed is not None:
            eligible += 1
            if closed <= reported + target:
                in_time += 1
        elif now >= reported + target:
            eligible += 1
            still_open_past_target += 1
        else:
            censored += 1

    indicator["numerator"] = in_time
    indicator["denominator"] = eligible
    indicator["n_observed"] = eligible
    indicator["excluded"] = {
        "target_not_elapsed": censored,
        "why": (f"An incident reported less than {target_days} days ago has not "
                "yet missed its closure target."),
    }
    indicator["detail"] = {
        "closure_target_days": target_days,
        "incidents_reported": len(rows),
        "open_past_target": still_open_past_target,
        "register_reports_all_types": usage["total"],
    }

    if not usage["in_use"]:
        indicator["state"] = "not_collected"
        indicator["blockers"] = [_REGISTER_UNUSED_BLOCKER]
        return _finalise(indicator)
    if eligible == 0:
        indicator["state"] = "insufficient_data"
        indicator["blockers"] = []
        return _finalise(indicator)

    indicator["value"] = _pct(in_time, eligible)
    indicator["display"] = f"{indicator['value']:.1f}%"
    indicator["state"] = "reported"
    return _finalise(indicator)


# ═════════════════════════════════════════════════════════════════════════════
# THE DASHBOARD
# ═════════════════════════════════════════════════════════════════════════════

def indicators(
    *, period_start: Optional[str] = None, period_end: Optional[str] = None,
    ward: Optional[str] = None, db: Any = None,
) -> dict:
    """Every indicator for one period, each with its own honest state.

    NEVER RAISES ON AN EMPTY DATABASE. A hospital that switched Orb on this
    morning must get a COMPLETE, explicitly-empty dashboard — nine indicators,
    every one saying what it is missing — rather than an exception or a page of
    zeroes. That property is asserted directly in tests/test_nabh.py.
    """
    start, end = resolve_period(period_start, period_end, default_days=90)

    def _work(conn) -> dict:
        usage = _register_in_use(conn, start, end, ward)
        bed_days = _inpatient_days(conn, start, end, ward)
        disposition_base = _disposition_base(conn, start, end, ward)

        computed = [
            _register_rate_indicator("medication_error_rate", "medication_error",
                                     conn, start, end, ward, usage, bed_days),
            _medication_override_rate(conn, start, end, ward),
            _register_rate_indicator("patient_fall_rate", "patient_fall",
                                     conn, start, end, ward, usage, bed_days),
            _handoff_completion(conn, start, end, ward),
            _escalation_ack_compliance(conn, start, end, ward),
            _disposition_indicator("inpatient_mortality", DEATH_DISPOSITIONS,
                                   disposition_base, {}),
            _disposition_indicator(
                "against_medical_advice_rate", AMA_DISPOSITIONS, disposition_base,
                {"absconded_reported_separately":
                 disposition_base["rows"].get("absconded", 0)}),
            _readmission(conn, start, end, ward),
            _incident_closure(conn, start, end, ward, usage),
        ]

        states = Counter(i["state"] for i in computed)
        return {
            "period": {"start": _iso(start), "end": _iso(end),
                       "start_date": start.date().isoformat(),
                       "end_date": (end - timedelta(days=1)).date().isoformat()},
            "ward": ward,
            "generated_at": _iso(_now()),
            "indicators": computed,
            "summary": {
                "total": len(computed),
                "reported": states.get("reported", 0),
                "insufficient_data": states.get("insufficient_data", 0),
                "not_collected": states.get("not_collected", 0),
                "not_computable": states.get("not_computable", 0),
            },
            "denominators": {
                "inpatient_days": bed_days,
                "dispositions": {
                    "recorded": disposition_base["recorded"],
                    "discharge_events_observed": disposition_base["discharge_events_observed"],
                    "coverage": disposition_base["coverage"],
                    "coverage_floor": disposition_base["coverage_floor"],
                },
            },
            "register_usage": usage,
            "not_collected_catalogue": [dict(g) for g in NOT_COLLECTED_CATALOGUE],
            "attestation": PACK_ATTESTATION,
        }

    if db is not None:
        return _work(db)
    with db_session() as conn:
        return _work(conn)


# ═════════════════════════════════════════════════════════════════════════════
# THE EVIDENCE PACK
# ═════════════════════════════════════════════════════════════════════════════

def _deidentify(incident: dict) -> dict:
    """Strip the direct patient identifier from a register row.

    `patient_id` is an Orb id, not a name — but an assessor does not need it and
    a pack that leaves the building is the wrong place to find out that it did.
    The free-text `description` is NOT machine-redactable and is left intact
    with a warning on the pack, because silently mangling an investigator's
    words would be worse than telling the truth about what the file contains.
    """
    out = dict(incident)
    out["patient_id"] = None
    out["patient_identifier_withheld"] = bool(incident.get("patient_id"))
    return out


def build_evidence_pack(
    *, period_start: Optional[str] = None, period_end: Optional[str] = None,
    ward: Optional[str] = None, generated_by: Optional[str] = None,
    include_patient_identifiers: bool = False, db: Any = None,
) -> dict:
    """The date-ranged pack, as one JSON document. Reads only; writes nothing."""
    start, end = resolve_period(period_start, period_end, default_days=90)

    def _work(conn) -> dict:
        dash = indicators(period_start=period_start, period_end=period_end,
                          ward=ward, db=conn)
        register = list_incidents(period_start=period_start, period_end=period_end,
                                  ward=ward, include_void=True, limit=1000, db=conn)
        rows = register["incidents"]
        if not include_patient_identifiers:
            rows = [_deidentify(r) for r in rows]

        dispositions = _disposition_base(conn, start, end, ward)
        anonymous = sum(1 for r in register["incidents"] if r["is_anonymous"])

        pack = {
            "pack_type": "nabh_evidence_pack",
            "pack_version": 1,
            "attestation": PACK_ATTESTATION,
            "site": _site_header(),
            "period": dash["period"],
            "ward": ward,
            "generated_at": _iso(_now()),
            "generated_by": generated_by,
            "include_patient_identifiers": bool(include_patient_identifiers),
            "indicators": dash["indicators"],
            "indicator_summary": dash["summary"],
            "denominators": dash["denominators"],
            "register": {
                "usage": dash["register_usage"],
                "count": len(rows),
                "anonymous_reports": anonymous,
                "by_status": dict(Counter(r["status"] for r in register["incidents"])),
                "incidents": rows,
                "truncated": register["truncated"],
            },
            "dispositions": {
                "by_disposition": dict(dispositions["rows"]),
                "recorded": dispositions["recorded"],
                "discharge_events_observed": dispositions["discharge_events_observed"],
                "coverage": dispositions["coverage"],
            },
            "not_collected": [dict(g) for g in NOT_COLLECTED_CATALOGUE],
            "anonymity_statement": ANONYMITY_STATEMENT,
            "limitations": _limitations(dash, include_patient_identifiers),
        }
        return pack

    if db is not None:
        return _work(db)
    with db_session() as conn:
        return _work(conn)


def _site_header() -> dict:
    """Hospital identity, from the site config if one is loaded. NEVER invented:
    a pack with a made-up hospital name on it is a forgery."""
    try:
        from services.site_config_service import current_site_config
        cfg = current_site_config()
        return {"site_name": cfg.site_name, "configured": bool(cfg.present)}
    except Exception:  # pragma: no cover — config is optional by design
        return {"site_name": None, "configured": False}


def _limitations(dash: dict, include_patient_identifiers: bool) -> list[str]:
    """The sentences a hospital should be able to say out loud before an
    assessor says them first. Assembled from what this particular pack actually
    contains, not from a fixed disclaimer."""
    out = [
        "Orb is not accredited and does not accredit. NABH accredits the hospital; "
        "this pack is evidence a hospital assembles, not a certificate.",
        "Every indicator here is computed only from what Orb recorded. Care "
        "delivered, or errors made, outside Orb are invisible to all of them.",
    ]
    summary = dash["summary"]
    if summary["not_collected"]:
        out.append(
            f"{summary['not_collected']} of {summary['total']} indicators have NO "
            "source data in this period and are marked NOT COLLECTED. They are not "
            "zero and must not be reported as zero.")
    if summary["insufficient_data"]:
        out.append(
            f"{summary['insufficient_data']} of {summary['total']} indicators have "
            "a source but too little of it, and are marked INSUFFICIENT DATA. The "
            "arithmetic is retained in the machine-readable detail for audit and "
            "is deliberately not displayed.")
    bed_days = dash["denominators"]["inpatient_days"]
    if bed_days.get("open_stays"):
        out.append(
            f"{bed_days['open_stays']} patient(s) were still admitted at the end of "
            "the period; their stay is counted to the period end, not to a "
            "discharge that has not happened.")
    if bed_days.get("assumed_ends"):
        out.append(
            f"{bed_days['assumed_ends']} discharged patient(s) had no discharge "
            "record, so their stay was closed at their last recorded vital. That "
            "understates inpatient-days and therefore overstates every rate "
            "computed per 1,000 inpatient-days.")
    coverage = dash["denominators"]["dispositions"]
    if coverage["discharge_events_observed"] and coverage["coverage"] < 1.0:
        out.append(
            f"Discharge dispositions were recorded for {coverage['coverage'] * 100:.0f}% "
            f"of the {coverage['discharge_events_observed']} discharge events Orb "
            "observed. Mortality and LAMA are computed on recorded dispositions only.")
    out.append(
        "Ward attribution uses each patient's CURRENT ward, not the ward they "
        "occupied on each day, so a ward-filtered pack attributes a whole stay to "
        "where the patient is now.")
    if include_patient_identifiers:
        out.append(
            "THIS PACK CONTAINS PATIENT IDENTIFIERS. It is PHI: handle, transmit "
            "and destroy it accordingly.")
    else:
        out.append(
            "Patient identifiers have been removed from the incident rows. "
            "Free-text incident descriptions are NOT redacted and may still name "
            "a person. Read them before this pack leaves the hospital.")
    return out


def _canonical_json(pack: dict) -> str:
    """Stable serialisation for hashing: sorted keys, no incidental whitespace.
    The digest has to be reproducible from the same pack by anyone."""
    return json.dumps(pack, sort_keys=True, separators=(",", ":"), default=str)


def record_evidence_export(
    *, period_start: Optional[str] = None, period_end: Optional[str] = None,
    ward: Optional[str] = None, generated_by: Optional[str] = None,
    include_patient_identifiers: bool = False, db: Any = None,
) -> dict:
    """Build the pack, log that it was produced, and return it with its digest.

    The log row is the point. "Which pack did we hand the assessor in March, and
    was it this file?" is a question a hospital gets asked, and `content_sha256`
    over the canonical JSON is what answers it. The digest is computed over the
    pack WITHOUT its own reference and digest fields, so it is reproducible from
    the returned document.
    """
    pack = build_evidence_pack(
        period_start=period_start, period_end=period_end, ward=ward,
        generated_by=generated_by,
        include_patient_identifiers=include_patient_identifiers, db=db)
    digest = hashlib.sha256(_canonical_json(pack).encode("utf-8")).hexdigest()
    reference = next_document_number("NABHPACK")["number"]
    summary = pack["indicator_summary"]

    def _work(conn) -> dict:
        conn.execute(
            "INSERT INTO nabh_evidence_exports (reference, period_start, period_end, "
            "ward, generated_by, generated_at, include_patient_identifiers, "
            "indicator_count, reported_count, insufficient_count, not_collected_count, "
            "incident_count, content_sha256, summary_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (reference, pack["period"]["start"], pack["period"]["end"], ward,
             generated_by, pack["generated_at"],
             1 if include_patient_identifiers else 0, summary["total"],
             summary["reported"], summary["insufficient_data"],
             summary["not_collected"], pack["register"]["count"], digest,
             json.dumps(summary), _iso(_now())),
        )
        return pack

    if db is not None:
        _work(db)
    else:
        with db_session() as conn:
            _work(conn)

    pack["reference"] = reference
    pack["content_sha256"] = digest
    pack["digest_note"] = (
        "SHA-256 over the canonical JSON of this pack EXCLUDING the 'reference', "
        "'content_sha256' and 'digest_note' fields. Recompute it by removing those "
        "three keys, serialising with sorted keys and no whitespace, and hashing.")
    return pack


def list_evidence_exports(*, limit: int = 50, db: Any = None) -> dict:
    def _work(conn) -> dict:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM nabh_evidence_exports ORDER BY generated_at DESC, id DESC "
            "LIMIT ?", (max(1, min(int(limit), 500)),),
        ).fetchall()]
        return {"exports": rows, "count": len(rows)}

    if db is not None:
        return _work(db)
    with db_session() as conn:
        return _work(conn)


# ═════════════════════════════════════════════════════════════════════════════
# Markdown rendering — what a person actually reads
# ═════════════════════════════════════════════════════════════════════════════

_STATE_LABEL = {
    "reported": "",
    "insufficient_data": "INSUFFICIENT DATA",
    "not_collected": "NOT COLLECTED",
    "not_computable": "NOT COMPUTABLE",
}


def render_pack_markdown(pack: dict) -> str:
    """The pack as a document. Same rule as the JSON: a non-reported indicator
    prints the WORDS, never a number, and never a dash that could be read as a
    zero."""
    site = pack.get("site") or {}
    name = site.get("site_name") or "(hospital name not configured in Orb)"
    period = pack.get("period") or {}
    lines: list[str] = [
        "# NABH accreditation evidence pack",
        "",
        f"**Hospital:** {name}  ",
        f"**Period:** {period.get('start_date')} to {period.get('end_date')} "
        f"(inclusive)  ",
        f"**Ward:** {pack.get('ward') or 'All wards'}  ",
        f"**Generated:** {pack.get('generated_at')} by {pack.get('generated_by') or 'unknown'}  ",
    ]
    if pack.get("reference"):
        lines.append(f"**Reference:** {pack['reference']}  ")
    if pack.get("content_sha256"):
        lines.append(f"**SHA-256:** `{pack['content_sha256']}`  ")
    lines += ["", "> " + pack.get("attestation", ""), "", "## Quality indicators", ""]

    summary = pack.get("indicator_summary") or {}
    lines.append(
        f"{summary.get('reported', 0)} reported · "
        f"{summary.get('insufficient_data', 0)} insufficient data · "
        f"{summary.get('not_collected', 0)} not collected · "
        f"{summary.get('not_computable', 0)} not computable")
    lines += ["", "| Code | Indicator | Result | Numerator | Denominator |",
              "| --- | --- | --- | --- | --- |"]
    for ind in pack.get("indicators", []):
        result = ind["display"] if ind["state"] == "reported" else _STATE_LABEL.get(
            ind["state"], ind["state"].upper())
        num = ind["numerator"] if ind["numerator"] is not None else "—"
        den = (f"{ind['denominator']} {ind['denominator_label']}"
               if ind["denominator"] is not None else "—")
        lines.append(f"| {ind['code']} | {ind['label']} | **{result}** | {num} | {den} |")

    lines += ["", "### Indicator notes", ""]
    for ind in pack.get("indicators", []):
        lines.append(f"**{ind['code']}: {ind['label']}**")
        lines.append("")
        lines.append(f"- Definition: {ind['definition']}")
        if ind["is_proxy"]:
            lines.append("- **This is a proxy measure, not the NABH indicator itself.**")
        lines.append(f"- Caveat: {ind['caveat']}")
        for blocker in ind.get("blockers", []):
            lines.append(f"- **Why there is no number:** {blocker}")
        excluded = ind.get("excluded") or {}
        for key, value in excluded.items():
            if key != "why" and value:
                lines.append(f"- Excluded ({key.replace('_', ' ')}): {value}. {excluded.get('why', '')}")
        lines.append("")

    register = pack.get("register") or {}
    lines += ["## Incident / adverse-event register", "",
              f"{register.get('count', 0)} incident(s) in the period, of which "
              f"{register.get('anonymous_reports', 0)} were reported anonymously.", ""]
    by_status = register.get("by_status") or {}
    if by_status:
        lines.append("| Status | Count |")
        lines.append("| --- | --- |")
        for status in INCIDENT_STATUSES:
            if status in by_status:
                lines.append(f"| {status.replace('_', ' ')} | {by_status[status]} |")
        lines.append("")

    lines += ["## What Orb does NOT collect", "",
              "Indicators an assessor may ask for that this system cannot produce, "
              "and what each would need:", ""]
    for gap in pack.get("not_collected", []):
        lines.append(f"- **{gap['code']} {gap['label']}**: {gap['reason']} "
                     f"*Needed:* {gap['needed']}")
    lines += ["", "## Limitations of this pack", ""]
    for note in pack.get("limitations", []):
        lines.append(f"- {note}")
    lines += ["", "## Anonymous reporting", "", pack.get("anonymity_statement", ""), ""]
    return "\n".join(lines)


# ═════════════════════════════════════════════════════════════════════════════
# Meta — the vocabulary the UI builds its forms from
# ═════════════════════════════════════════════════════════════════════════════

def meta() -> dict:
    """Everything the client needs to render forms without hard-coding a
    vocabulary that could drift from the database CHECK constraints."""
    return {
        "incident_types": list(INCIDENT_TYPES),
        "severities": list(SEVERITIES),
        "sentinel_severities": sorted(SENTINEL_SEVERITIES),
        "statuses": list(INCIDENT_STATUSES),
        "transitions": {k: list(v) for k, v in TRANSITIONS.items()},
        "action_types": list(ACTION_TYPES),
        "dispositions": list(DISPOSITIONS),
        "death_dispositions": sorted(DEATH_DISPOSITIONS),
        "ama_dispositions": sorted(AMA_DISPOSITIONS),
        "indicators": [
            {"id": k, "code": v["code"], "label": v["label"], "unit": v["unit"],
             "definition": v["definition"], "direction": v["direction"],
             "is_proxy": v["is_proxy"], "sources": _source_labels(v["sources"])}
            for k, v in INDICATOR_SPEC.items()
        ],
        "not_collected_catalogue": [dict(g) for g in NOT_COLLECTED_CATALOGUE],
        "anonymity_statement": ANONYMITY_STATEMENT,
        "attestation": PACK_ATTESTATION,
        # Ward names only, no patient data: what the ward selects offer, so the
        # register filter and the report form never take free text.
        "wards": hospital_wards(),
        "thresholds": {
            "escalation_ack_minutes": _ack_minutes(),
            "readmission_days": _readmission_days(),
            "closure_target_days": _closure_days(),
            "disposition_coverage_floor": _coverage_floor(),
        },
    }
