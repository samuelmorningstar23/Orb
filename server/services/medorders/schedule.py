"""Orb: the prescriber-authored administration schedule (medorders-19, W8-I).

Ruling MO-R8 (W117), a safety ruling held for Samuel and built as the
architect recommended, option (a): the standard administration times for each
frequency (for example TDS at 08:00, 14:00 and 20:00, or 06:00, 14:00 and
22:00) come from ONE file the hospital signs, and Orb ships NO built-in
default. Until a site signs that file, every board keeps the schedule it
derives today (services/nurse_shift_service.py: the interval read from the
frequency, anchored on the last dose).

THREE SOURCES OF A DUE TIME, in this order (the nurse board asks
board_schedule_for):
  1. times the prescriber set for this order (med_order_schedule_times,
     migration 71), written through set_schedule_in_txn;
  2. the hospital's signed times for the order's frequency
     (server/site_config/dose_times.json, read once at import);
  3. neither: the derived schedule, exactly as before.
Sources 1 and 2 apply only to a ward row LINKED to a medication order and only
while ORB_MED_ORDER_LIFECYCLE is on (the authoritative mode the times were
written in). With the lifecycle off, or for an unlinked row, nothing here
changes a due time.

THE SIGNED FILE (dose_times.json; the annotated template is
dose_times.example.json beside it). A plain file, reviewed like code and read
at server start, like every file in server/site_config (README.md there):
  * a malformed file STOPS THE SERVER at import, naming the file and the
    field (a set of times that silently disappeared would be worse than none);
  * editing it needs a restart: the times a nurse saw at the start of the
    shift are the times applied at the end of it;
  * no file is fine: no site times, the derived schedule continues.
What "signed" means here: the file names the pharmacy lead and the physician
champion who approved it, each with a date. Both are required and neither may
be blank, so the shipped template (blank names, HH:MM placeholders) is refused
if copied unsigned. The file's SHA-256 is recorded with every schedule
defaulted from it.

WHAT A SET OF TIMES MAY SAY. Only for a frequency given a fixed number of
times every day, no more often than every MIN_GAP_MINUTES (two hours): the
board's own parse_frequency reads it as 'scheduled', its interval divides a
day and is at least MIN_GAP_MINUTES (doses_per_day). That is OD, BD, TDS, QDS,
and every 2, 3, 4, 6, 8, 12 or 24 hours; every hour, every 30 or every 90
minutes keep the derived schedule, as do as-needed, stat, continuous and
every-other-day orders. The number of times must equal the doses a day the
frequency means, so set times can move a dose but never add or drop one;
times are distinct, written HH:MM on the hospital's 24-hour clock, and at
least MIN_GAP_MINUTES apart, across midnight too. A frequency is matched to
the file by its exact words (case and spacing aside), never by its interval:
TDS (with meals) and every 8 hours (a strict interval) are different orders,
and only the hospital's file says which spellings share times.

WHEN THE FREQUENCY CHANGES. Times a prescriber set were written for one
frequency, and the same rule holds for them: when an order's frequency
changes to different words (case and spacing aside), even to a frequency with
the same number of doses a day, the times in force are superseded in the same
transaction, with an 'amended' event and its audit row
(supersede_for_frequency_change_in_txn). Today the one writer of an order's
frequency is the pharmacist's correction at verification
(chart_lane._mirror_correction_in_txn); a test holds every writer of that
column to the same call. The board then uses the hospital's times for the new
frequency, or the derived schedule, until a prescriber sets new times.

WHEN A DOSE IS DUE ON SET TIMES (scheduled_due). Set times are wall-clock
times on the hospital's clock (services/hospital_time). One dose counts for ONE
set time, the one it was given for (_covered_by_dose): the set time at or
before it, or the next set time when the dose came up to EARLY_WINDOW_MINUTES
before that time AND nearer to it than to the set time before (a tie counts
for the earlier time, so the later one stays due). So a dose given at 07:50
for 08:00 does not leave 08:00 due, and a dose given on time, or up to an hour
late, never hides the next set time. MIN_GAP_MINUTES is twice the early window
for the same reason (checked at import). With no dose yet, the first set time
at or after the order started is the first one due. Past slots roll forward
exactly as the derived schedule does: the latest slot not yet covered is the
one shown (late after 15 minutes, the board's rule), and the slots before it
are counted as not charted.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time as dt_time, timedelta, timezone
from difflib import get_close_matches
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence

from services.clinical_time import utc_iso_now
from services.medorders import core

logger = logging.getLogger("orb.medorders.schedule")

__all__ = [
    "CONFIG_PATH_ENV", "EARLY_WINDOW_MINUTES", "MIN_GAP_MINUTES", "MAX_LOOKBACK_DAYS",
    "ENTRY_SCHEDULE", "LANE_SCHEDULE", "BASIS_PRESCRIBER", "BASIS_SITE_TIMES",
    "DoseTimesConfigError", "SiteDoseTimes",
    "config_path", "load_dose_times", "site_dose_times", "site_times_for",
    "normalise_frequency", "doses_per_day", "clean_times",
    "current_schedule_in_txn", "current_schedules_in_txn", "set_schedule_in_txn",
    "set_schedule", "supersede_for_frequency_change_in_txn", "FREQUENCY_CHANGED",
    "board_schedule_for", "scheduled_due", "site_times_view",
]

# ── Where the signed file lives ───────────────────────────────────────────────

_DEFAULT_PATH = Path(__file__).resolve().parent.parent.parent / "site_config" / "dose_times.json"

#: An absolute path to read instead, for a site that keeps its configuration in
#: its own repository and for tests.
CONFIG_PATH_ENV = "ORB_DOSE_TIMES_FILE"

#: A dose given up to this long before a set time counts for that time.
EARLY_WINDOW_MINUTES = 60
#: Two set times of one order are at least this far apart (across midnight
#: too), and a frequency more often than this has no set times. It must be at
#: least TWICE the early window: then the window before a set time never
#: reaches back past the middle of the gap, so a dose given on time, or up to
#: an hour late, always counts for its own set time and never for the next.
MIN_GAP_MINUTES = 120
if MIN_GAP_MINUTES < 2 * EARLY_WINDOW_MINUTES:  # a guard on the two numbers above
    raise RuntimeError(
        "MIN_GAP_MINUTES must be at least twice EARLY_WINDOW_MINUTES, or one dose could "
        "count for two set times and hide the next dose from the drug round.")
#: How far back the board looks for set times not charted. Beyond this the
#: count is only a lower bound (the screen shows anything over 12 as "12+").
MAX_LOOKBACK_DAYS = 31

ENTRY_SCHEDULE = "medorder.schedule"
LANE_SCHEDULE = "schedule"
#: Where a set of times came from: typed by the prescriber, or the hospital's
#: signed times for the frequency, accepted unchanged.
BASIS_PRESCRIBER = "prescriber"
BASIS_SITE_TIMES = "site_times"

_TOP_KEYS = ("about", "version", "signed", "times")
_REQUIRED_TOP = ("version", "signed", "times")
_SIGNERS = ("pharmacy", "physician_champion")
_SIGNER_KEYS = ("name", "date")
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


def config_path() -> Path:
    override = os.environ.get(CONFIG_PATH_ENV)
    return Path(override) if override else _DEFAULT_PATH


class DoseTimesConfigError(Exception):
    """The hospital's dose-times file is unusable. Raised at import, so at
    server start: a broken file never reads as 'no site times'."""

    def __init__(self, path: Any, problem: str, *, hint: Optional[str] = None):
        self.path = str(path)
        self.problem = problem
        self.hint = hint
        lines = ["", "Orb will not start: the hospital's dose times file is not valid.", "",
                 f"  File:    {self.path}", f"  Problem: {problem}"]
        if hint:
            lines.append(f"  Hint:    {hint}")
        lines += ["", "Fix the file and restart Orb, or remove it to use the times worked out "
                  "from each frequency (ruling MO-R8).", ""]
        super().__init__("\n".join(lines))


@dataclass(frozen=True)
class SiteDoseTimes:
    """The hospital's signed dose times, as read from the file."""
    path: str
    version: str
    fingerprint: str
    signed: dict
    #: normalised frequency -> times, in clock order
    times: dict
    #: normalised frequency -> the frequency as the file spells it
    labels: dict = field(default_factory=dict)


# ── Pure helpers ──────────────────────────────────────────────────────────────

def normalise_frequency(text: Any) -> str:
    """A frequency as the file and the order are matched: lower case, single
    spaces, trimmed."""
    return " ".join(str(text or "").split()).lower()


def doses_per_day(frequency: Any) -> Optional[int]:
    """How many doses a day the frequency means, when it can have set times:
    given a fixed number of times every day, no more often than every
    MIN_GAP_MINUTES. None otherwise (as required, continuous, stat, every other
    day, weekly, more often than every MIN_GAP_MINUTES such as every hour or
    every 90 minutes, or words the board cannot read); such an order keeps the
    derived schedule."""
    # Imported here: nurse_shift_service reads this module inside its own
    # functions, never at its import.
    from services.nurse_shift_service import parse_frequency
    parsed = parse_frequency(str(frequency or ""))
    interval = parsed.get("interval_minutes")
    if parsed.get("kind") != "scheduled" or not interval:
        return None
    if interval > 24 * 60 or (24 * 60) % interval:
        return None
    if interval < MIN_GAP_MINUTES:
        return None
    return (24 * 60) // interval


def _gap_words() -> str:
    """MIN_GAP_MINUTES in the ward's words: '2 hours'."""
    hours, rest = divmod(MIN_GAP_MINUTES, 60)
    if rest:
        return f"{MIN_GAP_MINUTES} minutes"
    return "an hour" if hours == 1 else f"{hours} hours"


def _minutes(hhmm: str) -> int:
    hours, minutes = hhmm.split(":")
    return int(hours) * 60 + int(minutes)


class _TimesInvalid(ValueError):
    pass


def clean_times(times: Any, *, expected: int) -> tuple[str, ...]:
    """Check one set of times for a frequency of `expected` doses a day and
    return it in clock order. Raises ValueError with words for the screen."""
    if not isinstance(times, (list, tuple)):
        raise _TimesInvalid("Give the times as a list, for example 08:00, 14:00, 20:00.")
    cleaned = []
    for value in times:
        text = value.strip() if isinstance(value, str) else ""
        if not _TIME_RE.match(text):
            raise _TimesInvalid(
                f"{value!r} is not a time. Write each time as HH:MM on the 24-hour clock, "
                "from 00:00 to 23:59.")
        cleaned.append(text)
    if len(set(cleaned)) != len(cleaned):
        raise _TimesInvalid("The same time is given twice.")
    if len(cleaned) != expected:
        raise _TimesInvalid(
            f"This frequency means {expected} dose{'s' if expected != 1 else ''} a day, so give "
            f"{expected} time{'s' if expected != 1 else ''} ({len(cleaned)} given).")
    ordered = sorted(cleaned, key=_minutes)
    if len(ordered) > 1:
        marks = [_minutes(t) for t in ordered]
        gaps = [b - a for a, b in zip(marks, marks[1:])] + [marks[0] + 24 * 60 - marks[-1]]
        if min(gaps) < MIN_GAP_MINUTES:
            raise _TimesInvalid(
                f"Two times are less than {_gap_words()} apart. Set times for one order must "
                f"be at least {_gap_words()} apart, across midnight too.")
    return tuple(ordered)


# ── The signed file ───────────────────────────────────────────────────────────

def _suggest(name: str, options: Iterable[str]) -> Optional[str]:
    near = get_close_matches(name, list(options), n=1)
    return f"Did you mean '{near[0]}'?" if near else None


def _check_keys(path: Path, where: str, data: Mapping, allowed: Sequence[str],
                required: Sequence[str]) -> None:
    for key in data:
        if key not in allowed:
            raise DoseTimesConfigError(
                path, f"Unknown key '{key}' in {where}. Allowed: {', '.join(allowed)}.",
                hint=_suggest(str(key), allowed))
    for key in required:
        if key not in data:
            raise DoseTimesConfigError(path, f"'{key}' is missing from {where}.")


def _nonblank(path: Path, where: str, value: Any, *, max_len: int = 120) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DoseTimesConfigError(path, f"{where} is blank. Every value must be filled in.")
    text = value.strip()
    if len(text) > max_len:
        raise DoseTimesConfigError(path, f"{where} is longer than {max_len} characters.")
    return text


def _validate(path: Path, raw: bytes) -> SiteDoseTimes:
    try:
        data = json.loads(raw.decode("utf-8"))
    except UnicodeDecodeError:
        raise DoseTimesConfigError(path, "The file is not UTF-8 text.") from None
    except json.JSONDecodeError as e:
        raise DoseTimesConfigError(
            path, f"The file is not valid JSON (line {e.lineno}, column {e.colno}: {e.msg}).") \
            from None
    if not isinstance(data, dict):
        raise DoseTimesConfigError(path, "The file must hold one JSON object.")
    _check_keys(path, "the file", data, _TOP_KEYS, _REQUIRED_TOP)
    if "about" in data and not isinstance(data["about"], str):
        raise DoseTimesConfigError(path, "'about' must be text.")
    version = _nonblank(path, "'version'", data["version"], max_len=64)

    signed = data["signed"]
    if not isinstance(signed, dict):
        raise DoseTimesConfigError(path, "'signed' must name the pharmacy and the physician "
                                         "champion who approved these times.")
    _check_keys(path, "'signed'", signed, _SIGNERS, _SIGNERS)
    signatures = {}
    for signer in _SIGNERS:
        entry = signed[signer]
        if not isinstance(entry, dict):
            raise DoseTimesConfigError(path, f"'signed.{signer}' must have a name and a date.")
        _check_keys(path, f"'signed.{signer}'", entry, _SIGNER_KEYS, _SIGNER_KEYS)
        name = _nonblank(path, f"'signed.{signer}.name'", entry["name"])
        when = _nonblank(path, f"'signed.{signer}.date'", entry["date"], max_len=10)
        try:
            signed_on = date.fromisoformat(when)
        except ValueError:
            raise DoseTimesConfigError(
                path, f"'signed.{signer}.date' is {when!r}, not a date written YYYY-MM-DD.") \
                from None
        if signed_on > _today():
            raise DoseTimesConfigError(path, f"'signed.{signer}.date' is in the future.")
        signatures[signer] = {"name": name, "date": signed_on.isoformat()}

    times = data["times"]
    if not isinstance(times, dict) or not times:
        raise DoseTimesConfigError(
            path, "'times' must list at least one frequency with its times, for example "
                  "\"TDS\": [\"08:00\", \"14:00\", \"20:00\"].")
    by_frequency: dict[str, tuple[str, ...]] = {}
    labels: dict[str, str] = {}
    for label, values in times.items():
        key = normalise_frequency(label)
        if not key or len(key) > 40:
            raise DoseTimesConfigError(path, f"Frequency {label!r} is blank or too long.")
        if key in by_frequency:
            raise DoseTimesConfigError(
                path, f"Frequency '{label}' is listed twice ('{labels[key]}' already).")
        expected = doses_per_day(label)
        if expected is None:
            raise DoseTimesConfigError(
                path, f"Times cannot be set for '{label}': set times are only for a "
                      "frequency given a fixed number of times every day, no more often than "
                      f"every {_gap_words()} (for example OD, BD, TDS, QDS or Q6H).")
        try:
            by_frequency[key] = clean_times(values, expected=expected)
        except ValueError as e:
            raise DoseTimesConfigError(path, f"'times.{label}': {e}") from None
        labels[key] = str(label).strip()
    return SiteDoseTimes(
        path=str(path), version=version,
        fingerprint="sha256:" + hashlib.sha256(raw).hexdigest(),
        signed=signatures, times=by_frequency, labels=labels)


def _today() -> date:
    from services.hospital_time import hospital_today
    return hospital_today()


def load_dose_times(path: Optional[Path] = None) -> Optional[SiteDoseTimes]:
    """The signed file, validated; None when there is no file (no site
    times). Raises DoseTimesConfigError for a file Orb will not use."""
    path = Path(path) if path is not None else config_path()
    if not path.exists():
        return None
    try:
        raw = path.read_bytes()
    except OSError as e:
        raise DoseTimesConfigError(path, f"The file cannot be read: {e}.") from None
    return _validate(path, raw)


#: Read once, at import (README rules 1 and 2).
_SITE: Optional[SiteDoseTimes] = load_dose_times()
if _SITE is not None:
    logger.info("Dose times: %d frequencies from %s (version %s, %s)",
                len(_SITE.times), _SITE.path, _SITE.version, _SITE.fingerprint)


def site_dose_times() -> Optional[SiteDoseTimes]:
    """The hospital's signed dose times, or None (no file)."""
    return _SITE


def site_times_for(frequency: Any, site: Optional[SiteDoseTimes] = None
                   ) -> Optional[tuple[str, ...]]:
    """The signed times for this exact frequency, or None."""
    site = site if site is not None else site_dose_times()
    if site is None:
        return None
    return site.times.get(normalise_frequency(frequency))


def site_times_view(site: Optional[SiteDoseTimes] = None) -> dict:
    """The signed file as a screen shows it: no patient data."""
    site = site if site is not None else site_dose_times()
    if site is None:
        return {"available": False, "version": None, "fingerprint": None, "signed": None,
                "times": []}
    return {
        "available": True, "version": site.version, "fingerprint": site.fingerprint,
        "signed": site.signed,
        "times": [{"frequency": site.labels.get(k, k), "times": list(v)}
                  for k, v in site.times.items()],
    }


# ── Authored times: readers ───────────────────────────────────────────────────

def _view(rows: Sequence[Mapping]) -> Optional[dict]:
    if not rows:
        return None
    first = rows[0]
    return {
        "times": sorted((r["due_time"] for r in rows), key=_minutes),
        "basis": first["basis"],
        "schedule_version": first["schedule_version"],
        "site_times_version": first["site_times_version"],
        "site_times_fingerprint": first["site_times_fingerprint"],
        "authored_by": first["authored_by"],
        "authored_at": first["authored_at"],
    }


def current_schedule_in_txn(conn, order_id: str) -> Optional[dict]:
    """The order's set times now in force, or None."""
    rows = conn.execute(
        "SELECT * FROM med_order_schedule_times WHERE med_order_id = ? "
        "AND superseded_at IS NULL ORDER BY due_time", (order_id,)).fetchall()
    return _view([dict(r) for r in rows])


def current_schedules_in_txn(conn, order_ids: Iterable[str]) -> dict[str, dict]:
    """{order id: set times} for every order in `order_ids` that has some."""
    ids = sorted({str(i) for i in order_ids if i})
    found: dict[str, list[dict]] = {}
    for start in range(0, len(ids), 500):
        chunk = ids[start:start + 500]
        marks = ",".join("?" for _ in chunk)
        for row in conn.execute(
            f"SELECT * FROM med_order_schedule_times WHERE med_order_id IN ({marks}) "
            "AND superseded_at IS NULL ORDER BY med_order_id, due_time", chunk).fetchall():
            found.setdefault(row["med_order_id"], []).append(dict(row))
    return {oid: _view(rows) for oid, rows in found.items()}


# ── Authored times: the writer ────────────────────────────────────────────────

_NOT_LIVE_WORDS = {
    "stopped": "was stopped", "completed": "is complete", "cancelled": "was cancelled",
    "rejected": "was rejected by the pharmacist",
}
STALE_ORDER_MESSAGE = "This medicine's order changed since you opened it. Reload and try again."


def set_schedule_in_txn(conn, order_id: str, times: Sequence[str], *, actor_id: str,
                        actor_role: Optional[str], expected_version: Optional[int] = None,
                        request_meta: Optional[Mapping] = None,
                        now: Optional[str] = None) -> dict:
    """Set (or, with an empty list, clear) the times an order is due, in the
    caller's immediate transaction. Returns {order, schedule, event_id,
    replayed}.

    The same guarded UPDATE as every store write (a new order version), an
    'amended' event with the times before and after, and its clinical_access
    audit row (fail-closed), together. The rows in force are superseded, never
    deleted, so who set which times stays readable. Refused for an order that
    has ended, an as-needed order, a frequency not given a fixed number of times
    a day, and a set of times that does not fit the frequency."""
    if not getattr(conn, "in_transaction", False):
        raise core.StoreUsageError("set_schedule_in_txn runs inside the caller's write "
                                   "transaction: open db_session(immediate=True) first")
    order = core.get_order_in_txn(conn, order_id)
    if order is None:
        raise core.MedOrderNotFound("This medication order no longer exists")
    if expected_version is not None and order["version"] != expected_version:
        raise core.StaleOrderVersion(STALE_ORDER_MESSAGE, order=order)
    status = order["status"]
    if status in core.TERMINAL_STATUSES:
        raise core.TransitionNotAllowed(
            f"This medicine's order {_NOT_LIVE_WORDS.get(status, 'has ended')}, so its times "
            "cannot be changed.", code="order_not_live", order=order)
    if order.get("is_prn"):
        raise core.MedOrderInvalid(
            "An as-needed medicine has no set times: it is given when it is needed.",
            code="prn_has_no_set_times", order=order)
    if not isinstance(times, (list, tuple)):
        raise core.MedOrderInvalid("Give the times as a list.", code="invalid_times", order=order)

    current = current_schedule_in_txn(conn, order["id"])
    if not times:
        new_times: tuple[str, ...] = ()
        basis = None
    else:
        expected = doses_per_day(order.get("frequency"))
        if expected is None:
            raise core.MedOrderInvalid(
                "Set times can be given only for a medicine given a fixed number of times a "
                f"day, no more often than every {_gap_words()} (for example OD, BD, TDS, QDS or "
                "every 6 hours). This one keeps the times worked out from its frequency.",
                code="frequency_not_daily", order=order)
        try:
            new_times = clean_times(list(times), expected=expected)
        except ValueError as e:
            raise core.MedOrderInvalid(str(e), code="invalid_times", order=order) from None
        site = site_dose_times()
        site_times = site_times_for(order.get("frequency"), site)
        basis = BASIS_SITE_TIMES if site_times == new_times else BASIS_PRESCRIBER

    before = list(current["times"]) if current else []
    if list(new_times) == before and (current or {}).get("basis") == basis:
        return {"order": order, "schedule": current, "event_id": None, "replayed": True}

    now = now or utc_iso_now()
    cur = conn.execute(
        "UPDATE med_orders SET version = ?, updated_at = ? "
        "WHERE id = ? AND version = ? AND status = ?",
        (order["version"] + 1, now, order["id"], order["version"], status))
    if cur.rowcount != 1:
        raise core.StaleOrderVersion(STALE_ORDER_MESSAGE,
                                     order=core.get_order_in_txn(conn, order["id"]) or order)
    if current:
        conn.execute(
            "UPDATE med_order_schedule_times SET superseded_at = ?, superseded_by = ? "
            "WHERE med_order_id = ? AND superseded_at IS NULL", (now, actor_id, order["id"]))
    site_version = site_fingerprint = None
    if basis == BASIS_SITE_TIMES:
        site = site_dose_times()
        site_version, site_fingerprint = site.version, site.fingerprint
    if new_times:
        last = conn.execute(
            "SELECT COALESCE(MAX(schedule_version), 0) FROM med_order_schedule_times "
            "WHERE med_order_id = ?", (order["id"],)).fetchone()[0]
        for due_time in new_times:
            conn.execute(
                "INSERT INTO med_order_schedule_times (med_order_id, patient_id, "
                "schedule_version, due_time, basis, site_times_version, site_times_fingerprint, "
                "authored_by, authored_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (order["id"], order["patient_id"], int(last) + 1, due_time, basis, site_version,
                 site_fingerprint, actor_id, now, now))
    after = core.get_order_in_txn(conn, order["id"])
    schedule = current_schedule_in_txn(conn, order["id"])
    changes = {"schedule_times": {"before": before or None, "after": list(new_times) or None}}
    event_id = core.record_event_in_txn(
        conn, order=after, kind="amended", from_status=status, to_status=status,
        actor_id=actor_id, actor_role=actor_role, entry_point=ENTRY_SCHEDULE,
        payload={"changes": changes, "basis": basis, "site_times_version": site_version,
                 "site_times_fingerprint": site_fingerprint},
        created_at=now)
    core.audit_in_txn(
        conn, order=after, action="med_order_amended", actor_id=actor_id,
        actor_role=actor_role, entry_point=ENTRY_SCHEDULE, from_status=status, to_status=status,
        lane=LANE_SCHEDULE, event_id=event_id,
        extra={"amended_fields": ["schedule_times"], "schedule_times_before": before or None,
               "schedule_times_after": list(new_times) or None, "schedule_basis": basis,
               "site_times_version": site_version},
        request_meta=request_meta)
    return {"order": after, "schedule": schedule, "event_id": event_id, "replayed": False}


def set_schedule(order_id: str, times: Sequence[str], *, actor_id: str,
                 actor_role: Optional[str], expected_version: Optional[int] = None,
                 request_meta: Optional[Mapping] = None) -> dict:
    """PUT /api/medorders/orders/{id}/schedule. BLOCKING: one immediate
    transaction."""
    from database import db_session
    with db_session(immediate=True) as conn:
        return set_schedule_in_txn(conn, order_id, times, actor_id=actor_id,
                                   actor_role=actor_role, expected_version=expected_version,
                                   request_meta=request_meta)


# ── Authored times: when the order's frequency changes ────────────────────────

#: Why a set of times stopped applying without a prescriber clearing it.
FREQUENCY_CHANGED = "frequency_changed"
_FREQUENCY_CHANGED_WORDS = ("The frequency changed, so the times set for the old frequency "
                            "no longer apply.")


def supersede_for_frequency_change_in_txn(
        conn, order: Mapping, *, frequency_before: Any, actor_id: Optional[str],
        actor_role: Optional[str], entry_point: str, extra: Optional[Mapping] = None,
        request_meta: Optional[Mapping] = None, now: Optional[str] = None) -> Optional[int]:
    """The order's frequency has just changed, in the caller's transaction:
    the times set for the old frequency stop applying. Returns the id of the
    'amended' event that records it, or None when nothing changes (the same
    frequency written another way, or no times in force).

    Set times are written for one frequency. A different frequency, even one
    with the same number of doses a day (TDS with meals and every 8 hours, a
    strict interval), is a different order, so its times are superseded here
    and never carried over: the board falls back to the hospital's times for
    the new frequency, or to the times worked out from it, until a prescriber
    sets new ones. Every writer that changes med_orders.frequency calls this in
    the same transaction (today chart_lane._mirror_correction_in_txn, the
    pharmacist's correction at verification); tests/test_medorder_schedule.py
    holds every writer of that column to it.

    `order` is the order as it now stands, with the new frequency. The caller's
    guarded UPDATE has already moved the order's version in this transaction,
    so this writes no second version: it supersedes the rows in force, then
    writes an 'amended' event (the caller's entry point, the times before and
    after) and its clinical_access audit row (fail-closed), beside the
    caller's own."""
    if not getattr(conn, "in_transaction", False):
        raise core.StoreUsageError("supersede_for_frequency_change_in_txn runs inside the "
                                   "caller's write transaction")
    frequency_after = order.get("frequency")
    if normalise_frequency(frequency_before) == normalise_frequency(frequency_after):
        return None
    current = current_schedule_in_txn(conn, order["id"])
    if current is None:
        return None
    if not str(actor_id or "").strip():
        raise core.MedOrderInvalid("A change to an order's times names the person who made it",
                                   code="actor_required", order=order)
    now = now or utc_iso_now()
    conn.execute(
        "UPDATE med_order_schedule_times SET superseded_at = ?, superseded_by = ? "
        "WHERE med_order_id = ? AND superseded_at IS NULL", (now, actor_id, order["id"]))
    before = list(current["times"])
    status = order["status"]
    event_id = core.record_event_in_txn(
        conn, order=order, kind="amended", from_status=status, to_status=status,
        actor_id=actor_id, actor_role=actor_role, entry_point=entry_point,
        reason=_FREQUENCY_CHANGED_WORDS,
        payload={"changes": {"schedule_times": {"before": before, "after": None}},
                 "superseded_because": FREQUENCY_CHANGED,
                 "frequency": {"before": frequency_before, "after": frequency_after},
                 "basis_before": current["basis"],
                 "schedule_version_before": current["schedule_version"]},
        created_at=now)
    core.audit_in_txn(
        conn, order=order, action="med_order_amended", actor_id=actor_id,
        actor_role=actor_role, entry_point=entry_point, from_status=status, to_status=status,
        reason=_FREQUENCY_CHANGED_WORDS, lane=LANE_SCHEDULE, event_id=event_id,
        extra={**dict(extra or {}), "amended_fields": ["schedule_times"],
               "schedule_times_before": before, "schedule_times_after": None,
               "schedule_basis": None, "superseded_because": FREQUENCY_CHANGED,
               "frequency_before": frequency_before, "frequency_after": frequency_after},
        request_meta=request_meta)
    return event_id


# ── The board ─────────────────────────────────────────────────────────────────

def board_schedule_for(order: Optional[Mapping], authored: Optional[Mapping],
                       site: Optional[SiteDoseTimes]) -> Optional[dict]:
    """Which set times the board uses for one LINKED ward row: {times,
    source: 'prescriber' | 'site_times', ...} or None (the derived schedule).
    The caller has already checked the lifecycle is on. The order's own
    frequency decides: no set times for a frequency the board cannot time as
    a fixed number of doses a day, and none whose count differs from the doses
    a day it means. Times written for a different frequency never reach here:
    the write that changes an order's frequency supersedes its set times in
    the same transaction (supersede_for_frequency_change_in_txn), so the order
    falls back to the hospital's times for the new frequency, or to the
    derived schedule, until a prescriber sets new ones."""
    if order is None or order.get("is_prn"):
        return None
    if order.get("status") not in core.LIVE_STATUSES:
        return None
    expected = doses_per_day(order.get("frequency"))
    if expected is None:
        return None
    if authored and len(authored.get("times") or ()) == expected:
        return {"times": list(authored["times"]),
                "source": authored.get("basis") or BASIS_PRESCRIBER,
                "authored": True}
    site_times = site_times_for(order.get("frequency"), site)
    if site_times and len(site_times) == expected:
        return {"times": list(site_times), "source": BASIS_SITE_TIMES, "authored": False,
                "site_times_version": site.version if site else None}
    return None


def _slots(times: Sequence[str], tz, start: datetime, end: datetime) -> list[datetime]:
    """Every set time from `start` to `end` inclusive, as aware UTC instants,
    in order. Both bounds aware."""
    clock = sorted((_minutes(t) for t in times))
    first_day = start.astimezone(tz).date() - timedelta(days=1)
    last_day = end.astimezone(tz).date() + timedelta(days=1)
    out: list[datetime] = []
    day = first_day
    while day <= last_day:
        for minute in clock:
            local = datetime.combine(day, dt_time(minute // 60, minute % 60), tzinfo=tz)
            instant = local.astimezone(timezone.utc)
            if start <= instant <= end:
                out.append(instant)
        day += timedelta(days=1)
    return out


def _covered_by_dose(times: Sequence[str], tz, given_at: datetime) -> datetime:
    """The last set time a dose given at `given_at` accounts for, or
    `given_at` itself when it accounts for none after it. One dose counts for
    ONE set time, the one it was given for: the set time at or before it, or
    the next set time when the dose came up to EARLY_WINDOW_MINUTES before
    that time AND strictly nearer to it than to the set time before. A tie
    counts for the earlier time, so the later one stays due. Set times before
    the one it counts for are never counted as not charted, as the derived
    schedule never counts slots before its anchor.

    So a dose cannot hide a second set time, whatever times it is handed
    (both the file reader and the prescriber's write refuse times less than
    MIN_GAP_MINUTES apart; this rule does not rely on that): with set times
    one hour apart, a dose given at 08:00 counts for 08:00 and 09:00 stays
    due."""
    window = timedelta(minutes=EARLY_WINDOW_MINUTES)
    after = _slots(times, tz, given_at + timedelta(microseconds=1), given_at + window)
    if not after:
        return given_at
    following = after[0]
    before = _slots(times, tz, given_at - timedelta(days=1), given_at)
    if before and (given_at - before[-1]) <= (following - given_at):
        return given_at
    return following


def scheduled_due(times: Sequence[str], *, last_given_at: Optional[datetime],
                  started_at: Optional[datetime], now: datetime, tz=None) -> dict:
    """The due time on set times: {due_at, due_basis, missed_slots}, the same
    three facts the derived schedule gives. Aware datetimes in, aware UTC out.
    due_at None when there is nothing to anchor on (no dose and no start)."""
    if tz is None:
        from services.hospital_time import hospital_timezone
        tz = hospital_timezone()
    if last_given_at is not None:
        # The next set time due is the first one after the set time this dose
        # counted for (_covered_by_dose): at most one, never two.
        floor = _covered_by_dose(times, tz, last_given_at) + timedelta(microseconds=1)
        basis = "last_given"
    elif started_at is not None:
        floor = started_at
        basis = "order_start"
    else:
        return {"due_at": None, "due_basis": "unscheduled", "missed_slots": 0}
    lookback = now - timedelta(days=MAX_LOOKBACK_DAYS)
    window_start = max(floor, lookback) if floor < now else floor
    if window_start > now:
        upcoming = _slots(times, tz, window_start, window_start + timedelta(days=2))
        return {"due_at": upcoming[0] if upcoming else None, "due_basis": basis,
                "missed_slots": 0}
    past = _slots(times, tz, window_start, now)
    if not past:
        upcoming = _slots(times, tz, now, now + timedelta(days=2))
        return {"due_at": upcoming[0] if upcoming else None, "due_basis": basis,
                "missed_slots": 0}
    return {"due_at": past[-1], "due_basis": basis, "missed_slots": len(past) - 1}
