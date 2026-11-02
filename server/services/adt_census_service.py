"""Orb: the daily census, read from the bed movement ledger (adt-13a, W7-C).

Behind Beds and admissions (ORB_ADT, default off): the only callers are the
routes under /api/adt, which the package gate switches off with the module.

WHAT IT COUNTS, per hospital-local day and per ward (services.hospital_time:
the appliance's zone is the hospital's):

  midnight census     patients present at the stroke of midnight that ends
                      the day, on the ward the ledger places them in at that
                      instant (a transfer at 23:30 counts on the receiving
                      ward). Patients in temporary extra beds are counted.
  admissions          stays that began that day, on the first ward the stay
                      was placed on.
  transfers in / out  a stay leaving one ward of bed setup for another (a bed
                      change inside one ward is not a transfer).
  ends, by type       routine, referred out, transferred out, DAMA, LAMA,
                      absconded, death, as recorded at the end of stay; status
                      'transferred' from a roster or history import counts as
                      transferred out; an end with no type (a discharge before
                      ends carried one, ended only by its summary) reads 'type
                      not recorded'. Deaths are listed on their own as well.
  inpatient days      hour-based: the hours each stay spent on the ward that
                      day, divided by 24 (services/stay_times reads every stay's
                      start and end, so a readmission counts as a second stay).
  licensed beds       beds set up on the ward at midnight, leaving out
                      temporary extra beds and beds whose class is not counted
                      in the census (bed_classes.counts_in_census = 0).
  available beds      the same beds, by the part of the day each was set up and
                      not recorded out of service by housekeeping
                      (housekeeping_unit_events), so one bed out of service for
                      six hours makes a day of 0.75 of that bed.
  occupancy           ruling ADT-P4 (applied): both methods are reported. The
                      NABH figure is the midnight census over available beds;
                      also the midnight census over licensed beds, and
                      inpatient days over available beds.
  ALOS                for stays that ended in the period, from the stay's start
                      to its end, in days.
  return to ICU       moves out of an intensive care ward (ward type icu, nicu
                      or picu) to another ward, and how many of those patients
                      were back on an intensive care ward within 48 hours. A
                      move out less than 48 hours before now is 'pending'.

INSUFFICIENT DATA, NEVER ZERO. A ward's ledger is trustworthy only from
wards.ledger_complete_from, which Bed setup stamps when the bed manager marks
the ward's register complete (every active admission on it linked to a bed).
Any day that does not START on or after that moment reads 'insufficient data'
with the reason, never a count. Hospital totals for a day need every ward that
existed that day complete; a ward that is switched off counts only if a
patient lay on it that day. Window totals add the complete days only and say
how many were left out.

THE LEDGER RULES every reader follows: a movement a correction points at
(corrects_movement_id) never happened; moves are read in the order they
happened (moved_at, which may be back-timed), ties by the order they were
written; a movement's ward is its master ward id, else its typed ward text
matched to a ward code. Encounters a history import filed are left out (the
old system's stays). Every stored time is read through clinical_time, so the
mixed legacy spellings (SQLite's 'YYYY-MM-DD HH:MM:SS', ISO with or without an
offset) all compare correctly.

NO PATIENT DATA leaves this module: counts only, by ward and day. A caller
with a department sees the wards of that department (the bed board's rule)
and no hospital totals; an account with no department sees every ward and the
hospital. Nothing here writes.

Every answer carries `methods`: the words above, for the screen and the CSV.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from typing import Any

from database import db_session

from services.clinical_time import parse_to_utc, utc_iso_now
from services.stay_times import stay_end_sql, stay_start_sql

__all__ = [
    "CensusError",
    "CensusNotFound",
    "ICU_WARD_TYPES",
    "END_TYPES",
    "END_TYPE_WORDS",
    "MAX_DAYS",
    "METHODS",
    "census",
    "census_on",
    "census_csv",
    "parse_window",
]

#: Ward types whose beds are intensive care, for the return-to-ICU count.
ICU_WARD_TYPES: tuple[str, ...] = ("icu", "nicu", "picu")

#: Ends by type, in the order a screen lists them. 'not_recorded' is an end
#: that carries no type (a discharge recorded before ends had one).
END_TYPES: tuple[str, ...] = (
    "routine",
    "referred_out",
    "transferred_out",
    "dama",
    "lama",
    "absconded",
    "death",
    "not_recorded",
)

END_TYPE_WORDS: dict[str, str] = {
    "routine": "Routine discharge",
    "referred_out": "Referred out",
    "transferred_out": "Transferred out",
    "dama": "DAMA",
    "lama": "LAMA",
    "absconded": "Absconded",
    "death": "Death",
    "not_recorded": "Type not recorded",
}

#: The longest window one request may ask for.
MAX_DAYS = 92

#: A return to intensive care counts within this long of leaving it.
RETURN_WINDOW = timedelta(hours=48)

#: Movements that place a stay somewhere (the ward it lies on follows them).
_LOCATING_TYPES: tuple[str, ...] = (
    "admission",
    "bed_assigned",
    "bed_change",
    "transfer",
    "bed_swap",
    "location_linked",
    "correction",
)

METHODS: dict[str, str] = {
    "days": (
        "Days are hospital-local calendar days, midnight to midnight, in the "
        "hospital's own time zone."
    ),
    "insufficient_data": (
        "A ward's figures start on the first full day after its bed register was marked "
        "complete in Bed setup (every patient on the ward linked to a bed). Earlier days read "
        "'insufficient data', never zero. Hospital totals for a day need every ward that "
        "existed that day to be complete; a ward that is switched off counts only if a "
        "patient lay on it that day. Totals for the period add the complete days only."
    ),
    "midnight_census": (
        "Patients present at the stroke of midnight that ends the day, counted on the ward "
        "the bed movement record places them in at that instant, so a transfer just before "
        "midnight counts on the receiving ward. Patients in temporary extra beds are counted. "
        "In the hospital totals, patients not on a ward in Bed setup (waiting for a bed, or "
        "on a place not set up) are counted apart and added to the hospital figure."
    ),
    "admissions": (
        "Stays that began that day (the recorded arrival time, else when the "
        "admission was recorded), on the first ward the stay was placed on."
    ),
    "transfers": (
        "A stay leaving one ward of Bed setup for another: counted out of the ward "
        "it left and into the ward it reached. A bed change inside one ward is not "
        "a transfer. A move recorded in error and undone is not counted."
    ),
    "ends": (
        "Ends of stay by the type recorded at the end. A stay recorded as transferred "
        "by a roster or history import counts as transferred out. An end with no type "
        "(a discharge recorded before ends carried one, or ended only by its discharge "
        "summary) reads 'Type not recorded'. A stay recorded as ended with no end time "
        "anywhere is closed at its last recorded movement, else at its start."
    ),
    "inpatient_days": (
        "Hour-based: the hours each stay spent on the ward during the day, "
        "divided by 24. A readmission is a second stay and counts on its own."
    ),
    "beds": (
        "Licensed beds: beds set up on the ward in Bed setup at midnight, leaving out "
        "temporary extra beds and beds whose class is not counted in the census. "
        "Available beds: the same beds, each by the part of the day it was set up and not "
        "recorded out of service by housekeeping."
    ),
    "occupancy": (
        "NABH occupancy is the midnight census over available beds. Also shown: the "
        "midnight census over licensed beds, and inpatient days over available beds. "
        "Patients in extra beds can take a figure above 100 per cent."
    ),
    "alos": (
        "Average length of stay: for stays that ended in the period, from the start of "
        "the stay to its end, in days, counted on the ward the stay ended on."
    ),
    "return_to_icu": (
        "Moves out of an intensive care ward (ward type ICU, NICU or PICU) to another ward, "
        "counted on the day of the move, and how many of those patients were back on an "
        "intensive care ward within 48 hours. A move out less than 48 hours before now is "
        "counted as pending."
    ),
    "sources": (
        "Read from the bed movement record (patient movements), the admissions, Bed "
        "setup and housekeeping's bed record. Encounters filed by a history import are "
        "left out. No patient is named."
    ),
}


class CensusError(ValueError):
    """A census request that cannot be answered. 422 unless a subclass says
    otherwise."""

    http_status = 422

    def __init__(self, message: str, *, code: str = "census_refused", **detail: Any):
        super().__init__(message)
        self.code = code
        self.detail = detail


class CensusNotFound(CensusError):
    """A ward that is not set up, or not one the caller may see. 404."""

    http_status = 404


# ═════════════════════════════════════════════════════════════════════════════
#  Small helpers
# ═════════════════════════════════════════════════════════════════════════════


def _parse(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    try:
        return parse_to_utc(value)
    except (TypeError, ValueError):
        return None


def _key(value: Any) -> str:
    return " ".join(str(value or "").split()).casefold()


def _day_bounds(day: date, tz: tzinfo) -> tuple[datetime, datetime]:
    """The [start, end) UTC instants of a hospital-local day (wall-clock
    midnight to midnight, so a day of a zone that shifts is 23 or 25 hours)."""
    start = datetime.combine(day, time.min, tzinfo=tz)
    end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=tz)
    return start.astimezone(UTC), end.astimezone(UTC)


def _local_day(instant: datetime, tz: tzinfo) -> date:
    return instant.astimezone(tz).date()


def _overlap(a0: datetime, a1: datetime, b0: datetime, b1: datetime) -> float:
    """Seconds shared by [a0, a1) and [b0, b1)."""
    lo, hi = max(a0, b0), min(a1, b1)
    return max(0.0, (hi - lo).total_seconds())


def _pct(num: float, den: float) -> float | None:
    return None if not den else round(num / den * 100.0, 1)


def parse_window(from_day: Any, to_day: Any, *, today: date) -> tuple[date, date]:
    """The census window as two hospital-local dates, inclusive. Default: the
    seven days ending yesterday. Refused with words: an unreadable date, a
    window that ends today or later (a day is counted once it has ended), a
    start after the end, or more than MAX_DAYS days."""

    def _one(value: Any, what: str) -> date | None:
        text = str(value).strip() if value is not None else ""
        if not text:
            return None
        try:
            return date.fromisoformat(text)
        except ValueError:
            raise CensusError(
                f"The {what} date could not be read. Give it as YYYY-MM-DD.",
                code="bad_date",
                field=what,
            ) from None

    last = _one(to_day, "to")
    first = _one(from_day, "from")
    yesterday = today - timedelta(days=1)
    if last is None:
        last = yesterday if first is None or first <= yesterday else first
    if first is None:
        first = last - timedelta(days=6)
    if last >= today:
        raise CensusError(
            "The census counts whole days, and today has not ended yet. Choose "
            "a period that ends yesterday or earlier.",
            code="day_not_ended",
            last_complete_day=yesterday.isoformat(),
        )
    if first > last:
        raise CensusError("The start of the period is after its end.", code="bad_window")
    if (last - first).days + 1 > MAX_DAYS:
        raise CensusError(
            f"Choose a period of at most {MAX_DAYS} days.",
            code="window_too_long",
            max_days=MAX_DAYS,
        )
    return first, last


# ═════════════════════════════════════════════════════════════════════════════
#  Reading the stays and their places
# ═════════════════════════════════════════════════════════════════════════════


@dataclass
class _Stay:
    id: str
    patient_id: str
    start: datetime
    end: datetime | None
    end_type: str | None
    #: (ward id or None, from, to): where the stay lay, in time order.
    segments: list = field(default_factory=list)


def _table_exists(db, name: str) -> bool:
    return (
        db.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
        ).fetchone()
        is not None
    )


def _columns(db, table: str) -> set:
    return {r[1] for r in db.execute(f"PRAGMA table_info({table})").fetchall()}


def _chunks(values: list, size: int = 400) -> Iterable[list]:
    for i in range(0, len(values), size):
        yield values[i : i + size]


def _end_type(row: dict) -> str:
    """The end of stay's type for the 'ends by type' count."""
    from migrations_his.m0058_adt_admission_location import DISCHARGE_TYPES

    kind = row.get("discharge_type")
    if isinstance(kind, str) and kind in DISCHARGE_TYPES:
        return kind
    if (row.get("status") or "") == "transferred":
        return "transferred_out"
    return "not_recorded"


class _Wards:
    """Ward lookups: by master id, and by code for a movement written before
    its ward was linked."""

    def __init__(self, wards: list[dict]):
        self.by_id = {w["id"]: w for w in wards}
        self.by_code = {_key(w.get("code")): w for w in wards if _key(w.get("code"))}

    def of(self, ward_id: Any, ward_text: Any) -> str | None:
        if ward_id and ward_id in self.by_id:
            return ward_id
        found = self.by_code.get(_key(ward_text)) if _key(ward_text) else None
        return found["id"] if found else None


def _load_stays(
    db: Any,
    *,
    since: datetime,
    until: datetime,
    wards: _Wards,
    facilities: frozenset[int] | None = None,
) -> tuple[list, int]:
    """Every stay that overlaps [since, until), with the places it lay in.
    Returns (stays, ended stays that carried no end time).

    `facilities` (multisite-8h1): with a facility scope, only the stays of
    admissions at those facilities (admissions.facility_id never changes)."""
    native = "a.import_run_id IS NULL" if "import_run_id" in _columns(db, "admissions") else "1 = 1"
    at_params: tuple = ()
    if facilities is not None:
        from services.facility_lanes import facility_marks

        marks, at_params = facility_marks(facilities)
        native = f"{native} AND a.facility_id IN ({marks})"
    rows = [
        dict(r)
        for r in db.execute(
            f"""SELECT * FROM (
                SELECT a.id, a.patient_id, a.status, a.discharge_type, a.ward, a.ward_id,
                       {stay_start_sql("a")} AS stay_start, {stay_end_sql("a")} AS stay_end
                  FROM admissions a WHERE {native}) s
             WHERE julianday(s.stay_start) < julianday(?)
               AND (s.stay_end IS NULL OR julianday(s.stay_end) >= julianday(?))""",
            (*at_params, until.isoformat(), since.isoformat()),
        ).fetchall()
    ]
    if not rows:
        return [], 0
    moves: dict[str, list[dict]] = {}
    voided: set[str] = set()
    if _table_exists(db, "patient_movements") and {
        "corrects_movement_id",
        "to_ward_id",
    } <= _columns(db, "patient_movements"):
        ids = [r["id"] for r in rows]
        for part in _chunks(ids):
            marks = ",".join("?" for _ in part)
            for m in db.execute(
                f"""SELECT rowid AS row_order, id, admission_id, movement_type, moved_at,
                               from_ward, from_ward_id, to_ward, to_ward_id, corrects_movement_id
                          FROM patient_movements WHERE admission_id IN ({marks})""",
                tuple(part),
            ).fetchall():
                m = dict(m)
                moves.setdefault(m["admission_id"], []).append(m)
                if m.get("corrects_movement_id"):
                    voided.add(m["corrects_movement_id"])
    stays: list[_Stay] = []
    no_end_time = 0
    for r in rows:
        start = _parse(r.get("stay_start"))
        if start is None:
            continue
        own = sorted(
            (m for m in moves.get(r["id"], []) if m["id"] not in voided),
            key=lambda m: (_parse(m.get("moved_at")) or start, m.get("row_order") or 0),
        )
        end = _parse(r.get("stay_end"))
        if end is None and (r.get("status") or "active") != "active":
            # Ended with no time recorded anywhere: closed at its last
            # movement, else at its start (said in METHODS['ends']).
            no_end_time += 1
            times = [t for t in (_parse(m.get("moved_at")) for m in own) if t is not None]
            end = max([start, *times])
        locating = [m for m in own if m.get("movement_type") in _LOCATING_TYPES]
        stay = _Stay(
            id=r["id"],
            patient_id=r["patient_id"],
            start=start,
            end=end,
            end_type=_end_type(r) if (r.get("status") or "active") != "active" else None,
        )
        stay.segments = _segments(stay, r, locating, wards)
        stays.append(stay)
    return stays, no_end_time


def _segments(stay: _Stay, row: dict, moves: list[dict], wards: _Wards) -> list:
    """Where the stay lay, as (ward id or None, from, to) in time order, from
    its start to its end (open-ended while active)."""
    far = datetime.max.replace(tzinfo=UTC)
    end = stay.end or far
    if moves:
        first = moves[0]
        here = (
            None
            if first.get("movement_type") == "admission"
            else wards.of(first.get("from_ward_id"), first.get("from_ward"))
        )
    else:
        here = wards.of(row.get("ward_id"), row.get("ward"))
    points: list[tuple[datetime, str | None]] = [(stay.start, here)]
    for m in moves:
        when = _parse(m.get("moved_at")) or stay.start
        when = min(max(when, stay.start), end)
        points.append((when, wards.of(m.get("to_ward_id"), m.get("to_ward"))))
    out: list = []
    for (t0, ward), (t1, _next) in zip(points, points[1:] + [(end, None)], strict=False):
        if t1 > t0:
            if out and out[-1][0] == ward and out[-1][2] == t0:
                out[-1] = (ward, out[-1][1], t1)
            else:
                out.append((ward, t0, t1))
    return out


def _where_at(stay: _Stay, instant: datetime) -> tuple[bool, str | None]:
    """(present, ward id or None) at `instant`."""
    if instant < stay.start or (stay.end is not None and instant >= stay.end):
        return False, None
    for ward, t0, t1 in stay.segments:
        if t0 <= instant < t1:
            return True, ward
    return True, None


# ═════════════════════════════════════════════════════════════════════════════
#  Beds: licensed and available
# ═════════════════════════════════════════════════════════════════════════════


def _beds_on(db, ward_ids: list[str]) -> dict[str, list[dict]]:
    """ward id -> its census beds: not temporary, in a class counted in the
    census, each with when it existed and when housekeeping recorded it out
    of service."""
    if not ward_ids or not _table_exists(db, "housekeeping_units"):
        return {}
    cols = _columns(db, "housekeeping_units")
    if "ward_id" not in cols:
        return {}
    uncounted: set[str] = set()
    if _table_exists(db, "bed_classes") and "counts_in_census" in _columns(db, "bed_classes"):
        uncounted = {
            _key(r[0])
            for r in db.execute(
                "SELECT code FROM bed_classes WHERE COALESCE(counts_in_census, 1) = 0"
            ).fetchall()
        }
    temporary = "COALESCE(is_temporary, 0) = 0" if "is_temporary" in cols else "1 = 1"
    retired = "retired_at" if "retired_at" in cols else "NULL"
    bed_class = "bed_class" if "bed_class" in cols else "NULL"
    beds: dict[str, list[dict]] = {}
    unit_ids: list[str] = []
    for part in _chunks(ward_ids):
        marks = ",".join("?" for _ in part)
        for r in db.execute(
            f"""SELECT id, ward_id, created_at, {retired} AS retired_at,
                           {bed_class} AS bed_class, state, state_since
                      FROM housekeeping_units
                     WHERE ward_id IN ({marks}) AND {temporary}""",
            tuple(part),
        ).fetchall():
            r = dict(r)
            if r.get("bed_class") and _key(r["bed_class"]) in uncounted:
                continue
            r["exists_from"] = _parse(r.get("created_at"))
            r["exists_to"] = _parse(r.get("retired_at"))
            r["oos"] = []
            beds.setdefault(r["ward_id"], []).append(r)
            unit_ids.append(r["id"])
    _out_of_service_on(db, beds, unit_ids)
    return beds


def _out_of_service_on(db, beds: dict[str, list[dict]], unit_ids: list[str]) -> None:
    """Fill each bed's out-of-service intervals from housekeeping's state
    ledger (housekeeping_unit_events): from an event into out_of_service to
    the next event out of it. A bed with no events uses its cached state."""
    far = datetime.max.replace(tzinfo=UTC)
    near = datetime.min.replace(tzinfo=UTC)
    events: dict[str, list[dict]] = {}
    if unit_ids and _table_exists(db, "housekeeping_unit_events"):
        for part in _chunks(unit_ids):
            marks = ",".join("?" for _ in part)
            for e in db.execute(
                f"""SELECT id, unit_id, from_state, to_state, occurred_at
                          FROM housekeeping_unit_events WHERE unit_id IN ({marks})""",
                tuple(part),
            ).fetchall():
                events.setdefault(e["unit_id"], []).append(dict(e))
    for ward_beds in beds.values():
        for bed in ward_beds:
            own = sorted(
                events.get(bed["id"], []),
                key=lambda e: (_parse(e.get("occurred_at")) or near, e.get("id") or 0),
            )
            if not own:
                if bed.get("state") == "out_of_service":
                    bed["oos"].append((_parse(bed.get("state_since")) or near, far))
                continue
            state = own[0].get("from_state")
            since = near
            for e in own:
                when = _parse(e.get("occurred_at")) or since
                if state == "out_of_service":
                    bed["oos"].append((since, when))
                state, since = e.get("to_state"), when
            if state == "out_of_service":
                bed["oos"].append((since, far))


def _bed_day(beds: list[dict], start: datetime, end: datetime) -> tuple[int, float]:
    """(licensed beds at midnight, available bed-days) for one ward and day."""
    far = datetime.max.replace(tzinfo=UTC)
    near = datetime.min.replace(tzinfo=UTC)
    length = (end - start).total_seconds()
    licensed = 0
    available = 0.0
    for bed in beds:
        b0 = bed.get("exists_from") or near
        b1 = bed.get("exists_to") or far
        if b0 <= end < b1:
            licensed += 1
        up = _overlap(b0, b1, start, end)
        if not up:
            continue
        down = sum(_overlap(max(o0, b0), min(o1, b1), start, end) for o0, o1 in bed["oos"])
        available += max(0.0, up - down) / length
    return licensed, available


# ═════════════════════════════════════════════════════════════════════════════
#  The count
# ═════════════════════════════════════════════════════════════════════════════


def _complete_from(ward: dict, tz: tzinfo) -> tuple[datetime | None, date | None]:
    """(the ledger start, the first full local day on or after it)."""
    start = _parse(ward.get("ledger_complete_from"))
    if start is None:
        return None, None
    local = start.astimezone(tz)
    first = local.date()
    if local.timetz().replace(tzinfo=None) != time.min:
        first = first + timedelta(days=1)
    return start, first


def _insufficient_words(ward: dict, first_full: date | None) -> str:
    """Why a ward's day is not counted. The date itself travels beside it as
    first_complete_day, for the screen to print in its own date format."""
    code = ward.get("code") or "This ward"
    if first_full is None:
        return (
            f"{code}: the bed register has not been marked complete in Bed setup, so the "
            "ward is not counted yet."
        )
    return (
        f"{code}: figures start on the first full day after its bed register was marked "
        "complete in Bed setup."
    )


def _empty_counts() -> dict:
    return {
        "midnight_census": 0,
        "admissions": 0,
        "transfers_in": 0,
        "transfers_out": 0,
        "ends": {t: 0 for t in END_TYPES},
        "deaths": 0,
        "inpatient_hours": 0.0,
        "stays": set(),
        "icu_out": 0,
        "icu_returns": 0,
        "icu_pending": 0,
    }


def census_on(
    db: Any,
    *,
    first_day: date,
    last_day: date,
    wards: list[dict],
    all_wards: list[dict],
    hospital: bool,
    tz: tzinfo,
    now: datetime,
    facilities: frozenset[int] | None = None,
) -> dict:
    """The census for `wards` (the caller may see them) over [first_day,
    last_day], hospital-local, and the hospital totals when `hospital`.
    `all_wards` is every ward of bed setup: a move to a ward the caller does
    not see still counts as a transfer out, and the hospital is complete for a
    day only when every ward that existed that day is.

    `facilities` (multisite-8h1) limits the stays counted to admissions at
    those facilities; the caller passes that facility's wards as `all_wards`,
    so the hospital totals are the facility's."""
    days = [first_day + timedelta(days=i) for i in range((last_day - first_day).days + 1)]
    bounds = {d: _day_bounds(d, tz) for d in days}
    window_start, window_end = bounds[days[0]][0], bounds[days[-1]][1]
    horizon = min(now, window_end + RETURN_WINDOW)
    lookup = _Wards(all_wards)
    stays, no_end_time = _load_stays(
        db, since=window_start, until=horizon, wards=lookup, facilities=facilities
    )
    icu = {w["id"] for w in all_wards if (w.get("ward_type") or "") in ICU_WARD_TYPES}

    # ward id (None: not on a ward in bed setup) -> day -> counts.
    counts: dict[str | None, dict[date, dict]] = {}

    def cell(ward: str | None, day: date) -> dict:
        return counts.setdefault(ward, {}).setdefault(day, _empty_counts())

    in_window = set(days)
    ended_on: dict[str | None, dict[date, list[float]]] = {}
    for stay in stays:
        # Midnight census and inpatient hours.
        for d in days:
            d0, d1 = bounds[d]
            present, ward = _where_at(stay, d1)
            if present:
                cell(ward, d)["midnight_census"] += 1
            for ward_seg, s0, s1 in stay.segments:
                shared = _overlap(s0, s1, d0, d1)
                if shared:
                    c: dict | None
                    c = cell(ward_seg, d)
                    c["inpatient_hours"] += shared / 3600.0
                    c["stays"].add(stay.id)
        known = [(w, s0, s1) for w, s0, s1 in stay.segments if w is not None]
        # Admission, on the first ward the stay was placed on.
        start_day = _local_day(stay.start, tz)
        if start_day in in_window:
            cell(known[0][0] if known else None, start_day)["admissions"] += 1
        # Transfers between wards, and moves out of intensive care.
        for (w_prev, _p0, p1), (w_next, n0, _n1) in zip(known, known[1:], strict=False):
            if w_prev == w_next:
                continue
            out_day, in_day = _local_day(p1, tz), _local_day(n0, tz)
            if out_day in in_window:
                cell(w_prev, out_day)["transfers_out"] += 1
            if in_day in in_window:
                cell(w_next, in_day)["transfers_in"] += 1
            if w_prev in icu and w_next not in icu and out_day in in_window:
                c = cell(w_prev, out_day)
                c["icu_out"] += 1
                back = next((s0 for w, s0, _s1 in known if w in icu and s0 >= n0), None)
                if back is not None and back - p1 <= RETURN_WINDOW:
                    c["icu_returns"] += 1
                elif now - p1 < RETURN_WINDOW:
                    c["icu_pending"] += 1
        # The end of stay, on the ward it ended on.
        if stay.end is not None:
            end_day = _local_day(stay.end, tz)
            if end_day in in_window:
                last_ward = stay.segments[-1][0] if stay.segments else None
                c = cell(last_ward, end_day)
                kind = stay.end_type or "not_recorded"
                c["ends"][kind] = c["ends"].get(kind, 0) + 1
                if kind == "death":
                    c["deaths"] += 1
                ended_on.setdefault(last_ward, {}).setdefault(end_day, []).append(
                    (stay.end - stay.start).total_seconds() / 86400.0
                )

    beds = _beds_on(db, [w["id"] for w in all_wards])
    starts = {w["id"]: _complete_from(w, tz) for w in all_wards}

    def ward_complete(ward: dict, day: date) -> bool:
        start, _first = starts[ward["id"]]
        return start is not None and start <= bounds[day][0]

    def day_figures(c: dict, licensed: int, available: float) -> dict:
        return {
            "midnight_census": c["midnight_census"],
            "admissions": c["admissions"],
            "transfers_in": c["transfers_in"],
            "transfers_out": c["transfers_out"],
            "ends": dict(c["ends"]),
            "deaths": c["deaths"],
            "inpatient_days": round(c["inpatient_hours"] / 24.0, 2),
            "stays": len(c["stays"]),
            "licensed_beds": licensed,
            "available_beds": round(available, 2),
            "occupancy": {
                "nabh": _pct(c["midnight_census"], available),
                "licensed": _pct(c["midnight_census"], licensed),
                "hours": _pct(c["inpatient_hours"] / 24.0, available),
            },
        }

    def totals_of(day_rows: list[dict], lengths: list[float], *, is_icu: bool) -> dict:
        done = [r for r in day_rows if r["status"] == "complete"]
        status = (
            "insufficient_data"
            if not done
            else "complete"
            if len(done) == len(day_rows)
            else "partial"
        )
        out: dict[str, Any] = {
            "status": status,
            "complete_days": len(done),
            "insufficient_days": len(day_rows) - len(done),
        }
        if not done:
            return out
        census_sum = sum(r["midnight_census"] for r in done)
        licensed_sum = sum(r["licensed_beds"] for r in done)
        available_sum = sum(r["available_beds"] for r in done)
        inpatient = sum(r["inpatient_days"] for r in done)
        ends = {t: sum(r["ends"].get(t, 0) for r in done) for t in END_TYPES}
        out.update(
            {
                "midnight_census_total": census_sum,
                "average_midnight_census": round(census_sum / len(done), 2),
                "admissions": sum(r["admissions"] for r in done),
                "transfers_in": sum(r["transfers_in"] for r in done),
                "transfers_out": sum(r["transfers_out"] for r in done),
                "ends": ends,
                "deaths": ends.get("death", 0),
                "inpatient_days": round(inpatient, 2),
                "licensed_bed_days": licensed_sum,
                "available_bed_days": round(available_sum, 2),
                "occupancy": {
                    "nabh": _pct(census_sum, available_sum),
                    "licensed": _pct(census_sum, licensed_sum),
                    "hours": _pct(inpatient, available_sum),
                },
                "stays_ended": len(lengths),
                "alos_days": round(sum(lengths) / len(lengths), 2) if lengths else None,
            }
        )
        if is_icu:
            moved = sum((r.get("icu") or {}).get("transfers_out", 0) for r in done)
            back = sum((r.get("icu") or {}).get("returns_48h", 0) for r in done)
            pending = sum((r.get("icu") or {}).get("pending", 0) for r in done)
            decided = moved - pending
            out["icu"] = {
                "transfers_out": moved,
                "returns_48h": back,
                "pending": pending,
                "return_rate": _pct(back, decided) if decided > 0 else None,
            }
        return out

    ward_out: list[dict] = []
    for ward_record in wards:
        start, first_full = starts[ward_record["id"]]
        is_icu = ward_record["id"] in icu
        rows: list[dict] = []
        lengths: list[float] = []
        for d in days:
            if not ward_complete(ward_record, d):
                rows.append(
                    {
                        "date": d.isoformat(),
                        "status": "insufficient_data",
                        "reason": _insufficient_words(ward_record, first_full),
                    }
                )
                continue
            c = counts.get(ward_record["id"], {}).get(d) or _empty_counts()
            licensed, available = _bed_day(beds.get(ward_record["id"], []), *bounds[d])
            row = {
                "date": d.isoformat(),
                "status": "complete",
                **day_figures(c, licensed, available),
            }
            if is_icu:
                row["icu"] = {
                    "transfers_out": c["icu_out"],
                    "returns_48h": c["icu_returns"],
                    "pending": c["icu_pending"],
                }
            rows.append(row)
            lengths.extend(ended_on.get(ward_record["id"], {}).get(d, []))
        ward_out.append(
            {
                "id": ward_record["id"],
                "code": ward_record.get("code"),
                "name": ward_record.get("name"),
                "department": ward_record.get("department"),
                "ward_type": ward_record.get("ward_type"),
                "is_active": bool(ward_record.get("is_active")),
                "icu": is_icu,
                "ledger_complete_from": ward_record.get("ledger_complete_from"),
                "first_complete_day": first_full.isoformat() if first_full else None,
                "days": rows,
                "totals": totals_of(rows, lengths, is_icu=is_icu),
            }
        )

    hospital_out: dict | None = None
    if hospital:
        rows = []
        lengths = []
        for d in days:
            d0, d1 = bounds[d]
            needed = [
                w
                for w in all_wards
                if (_parse(w.get("created_at")) or d0) < d1
                and (w.get("is_active") or (counts.get(w["id"], {}).get(d) or {}).get("stays"))
            ]
            missing = [w for w in needed if not ward_complete(w, d)]
            if not needed or missing:
                reason = (
                    "No ward is set up in Bed setup yet."
                    if not needed
                    else "Hospital totals need every ward complete for the day. Not complete: "
                    + ", ".join(sorted(str(w.get("code")) for w in missing))
                    + "."
                )
                rows.append(
                    {"date": d.isoformat(), "status": "insufficient_data", "reason": reason}
                )
                continue
            total = _empty_counts()
            for ward_id in [w["id"] for w in all_wards] + [None]:
                c = counts.get(ward_id, {}).get(d)
                if not c:
                    continue
                for k in (
                    "midnight_census",
                    "admissions",
                    "transfers_in",
                    "transfers_out",
                    "deaths",
                    "icu_out",
                    "icu_returns",
                    "icu_pending",
                ):
                    total[k] += c[k]
                total["inpatient_hours"] += c["inpatient_hours"]
                total["stays"] |= c["stays"]
                for t, n in c["ends"].items():
                    total["ends"][t] = total["ends"].get(t, 0) + n
            licensed = 0
            available = 0.0
            for w in all_wards:
                lic, avail = _bed_day(beds.get(w["id"], []), d0, d1)
                licensed += lic
                available += avail
            unplaced = (counts.get(None, {}).get(d) or {}).get("midnight_census", 0)
            rows.append(
                {
                    "date": d.isoformat(),
                    "status": "complete",
                    **day_figures(total, licensed, available),
                    "not_on_a_ward": unplaced,
                    "icu": {
                        "transfers_out": total["icu_out"],
                        "returns_48h": total["icu_returns"],
                        "pending": total["icu_pending"],
                    },
                }
            )
            for ward_id in [w["id"] for w in all_wards] + [None]:
                lengths.extend(ended_on.get(ward_id, {}).get(d, []))
        hospital_out = {
            "days": rows,
            "totals": totals_of(rows, lengths, is_icu=True),
            "ended_without_time": no_end_time,
        }

    return {
        "generated_at": utc_iso_now(),
        "from": first_day.isoformat(),
        "to": last_day.isoformat(),
        "timezone": str(getattr(tz, "key", None) or tz),
        "wards": ward_out,
        "hospital": hospital_out,
        "end_type_words": dict(END_TYPE_WORDS),
        "methods": dict(METHODS),
    }


# ═════════════════════════════════════════════════════════════════════════════
#  The public read, and the CSV
# ═════════════════════════════════════════════════════════════════════════════


def census(
    *,
    actor: dict,
    from_day: Any = None,
    to_day: Any = None,
    ward_id: str | None = None,
    now: datetime | None = None,
) -> dict:
    """GET /api/adt/census. The caller's wards (all of them for an account
    with no department, else its department's), or one of them; hospital
    totals for an account with no department and no ward chosen. A ward the
    caller may not see is 404. With more than one facility on record
    (multisite-8h1) everything is the caller's facility's: its wards, the
    stays of its admissions, and its own totals as the hospital's."""
    from permissions import department_scope

    from services import facility_lanes
    from services import housekeeping_service as hk
    from services.hospital_time import hospital_timezone

    tz = hospital_timezone()
    now = now or datetime.now(UTC)
    first, last = parse_window(from_day, to_day, today=now.astimezone(tz).date())
    dept = department_scope(actor)
    facilities = facility_lanes.scope_of(actor)
    hk.ensure_housekeeping_schema()
    with db_session() as db:
        if not _table_exists(db, "wards"):
            all_wards: list[dict] = []
        else:
            all_wards = [
                dict(r) for r in db.execute("SELECT * FROM wards ORDER BY LOWER(code)").fetchall()
            ]
        all_wards = facility_lanes.wards_in_scope(all_wards, facilities)
        visible = (
            all_wards
            if dept is None
            else [w for w in all_wards if _key(w.get("department")) == _key(dept)]
        )
        if ward_id:
            chosen = [w for w in visible if w["id"] == ward_id]
            if not chosen:
                raise CensusNotFound(
                    "No ward with that id is on your census.", code="ward_not_found"
                )
        else:
            chosen = visible
        out = census_on(
            db,
            first_day=first,
            last_day=last,
            wards=chosen,
            all_wards=all_wards,
            hospital=dept is None and not ward_id,
            tz=tz,
            now=now,
            facilities=facilities,
        )
    out["department"] = dept
    out["ward_id"] = ward_id
    out["ward_choices"] = [
        {
            "id": w["id"],
            "code": w.get("code"),
            "name": w.get("name"),
            "department": w.get("department"),
        }
        for w in visible
    ]
    if dept is not None:
        out["hospital_hidden"] = (
            "Hospital totals are shown to accounts that are not tied to one department."
        )
    return out


_CSV_COLUMNS: tuple[str, ...] = (
    "level",
    "ward",
    "ward_name",
    "department",
    "date",
    "status",
    "reason",
    "midnight_census",
    "admissions",
    "transfers_in",
    "transfers_out",
    *(f"ends_{t}" for t in END_TYPES),
    "deaths",
    "inpatient_days",
    "stays",
    "licensed_beds",
    "available_beds",
    "occupancy_nabh_percent",
    "occupancy_licensed_percent",
    "occupancy_hours_percent",
    "not_on_a_ward",
    "icu_transfers_out",
    "icu_returns_48h",
    "icu_pending",
)


def _cell(value: Any) -> Any:
    """A spreadsheet never runs a cell as a formula: text that starts with
    = + - @ (a typed ward name, a reason) is prefixed with a quote."""
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value
    return "" if value is None else value


def _csv_row(level: str, ward: dict | None, day: dict) -> list:
    occ = day.get("occupancy") or {}
    icu = day.get("icu") or {}
    ends = day.get("ends") or {}
    complete = day.get("status") == "complete"
    values = {
        "level": level,
        "ward": (ward or {}).get("code") if ward else "",
        "ward_name": (ward or {}).get("name") if ward else "",
        "department": (ward or {}).get("department") if ward else "",
        "date": day.get("date"),
        "status": "complete" if complete else "insufficient data",
        "reason": day.get("reason"),
        "midnight_census": day.get("midnight_census"),
        "admissions": day.get("admissions"),
        "transfers_in": day.get("transfers_in"),
        "transfers_out": day.get("transfers_out"),
        **{f"ends_{t}": (ends.get(t, 0) if complete else None) for t in END_TYPES},
        "deaths": day.get("deaths"),
        "inpatient_days": day.get("inpatient_days"),
        "stays": day.get("stays"),
        "licensed_beds": day.get("licensed_beds"),
        "available_beds": day.get("available_beds"),
        "occupancy_nabh_percent": occ.get("nabh"),
        "occupancy_licensed_percent": occ.get("licensed"),
        "occupancy_hours_percent": occ.get("hours"),
        "not_on_a_ward": day.get("not_on_a_ward"),
        "icu_transfers_out": icu.get("transfers_out"),
        "icu_returns_48h": icu.get("returns_48h"),
        "icu_pending": icu.get("pending"),
    }
    return [_cell(values[c]) for c in _CSV_COLUMNS]


def census_csv(result: dict) -> str:
    """The census answer as CSV: one row per ward and day, then the hospital's
    rows when present, then a blank line and the method of every figure."""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(_CSV_COLUMNS)
    for ward in result.get("wards") or []:
        for day in ward.get("days") or []:
            writer.writerow(_csv_row("ward", ward, day))
    hospital = result.get("hospital")
    if hospital:
        for day in hospital.get("days") or []:
            writer.writerow(_csv_row("hospital", None, day))
    writer.writerow([])
    writer.writerow(["method", "how it is counted"])
    writer.writerow(
        [
            "period",
            _cell(
                f"{result.get('from')} to {result.get('to')}, time zone {result.get('timezone')}"
            ),
        ]
    )
    for name, words in (result.get("methods") or {}).items():
        writer.writerow([name, _cell(words)])
    return buf.getvalue()
