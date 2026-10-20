"""Pulse: season and conditions, from a calendar built into Orb.

WHAT CHANGED, AND WHY
─────────────────────
Pulse used to pull four live internet feeds (open-meteo weather and air
quality, FDA drug recalls, CDC flu figures), and Forecast multiplied tomorrow's
admissions by hand-picked weather guesses. Orb is strictly offline: nothing
leaves the appliance, ever. Ruled 6 October 2026: all of that is gone. Pulse
now answers from a seasonal calendar shipped with Orb and read from disk, for
the Indian state the hospital is in (site.toml, `[site] state`).

What it can say is deliberately modest: "it is the monsoon in this part of
India, and these illnesses typically rise in it". It never says how many
patients that means. Forecast shows the season beside its numbers as context
and never multiplies by it.

THE FILE
────────
server/site_config/seasonal_calendar.json, or the path in
ORB_SEASONAL_CALENDAR_FILE (tests, and sites that keep configuration in their
own repository). Schema version 1:

  zones    groups of states. Every one of the 36 codes in
           services/india_states.py sits in exactly one zone.
  entries  a season, an environmental condition or a disease season: the zones
           it applies to, a "MM-DD" start and end (both inclusive; a start
           later than the end runs over the new year), an optional peak inside
           that range, a summary, what typically rises, and at least one source.

A file that breaks any rule is refused whole, and the refusal lists every
problem, with a line number where one can be found. Keys starting with "_" are
comments and are ignored; any other key Orb does not know is an error, because
a misspelt key is a value someone believes is in use.

A missing or refused file does NOT stop Orb. A seasonal note is reference
material, not a safety control, and a hospital's server must not go down over
it. The problem is logged once, at startup, and GET /api/pulse/season answers
503 with a plain sentence until the file is fixed and Orb restarted. Like every
file in site_config/, it is read once: an edit needs a restart.

DATES
─────
"Today" is always the HOSPITAL's calendar day (services/hospital_time.py), so
at 00:10 IST on 1 November the November entries are active, although the UTC
date is still 31 October.

29 February is a valid start or end. In a year without one it reads as
28 February, for starts and ends alike, so "until the end of February" still
means that, and no range can turn into one that wraps the year by accident.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from calendar import isleap
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, TypeGuard

from services import hospital_time
from services.india_states import STATES, resolve_state, state_name

logger = logging.getLogger("orb.pulse.season")

_DEFAULT_PATH = Path(__file__).resolve().parent.parent / "site_config" / "seasonal_calendar.json"

#: Env override for the file's location. An absolute path.
CALENDAR_PATH_ENV = "ORB_SEASONAL_CALENDAR_FILE"

SCHEMA_VERSION = 1
STATUSES = ("draft", "reviewed")
#: Every kind an entry may have, in the order active entries are listed.
KINDS = ("season", "environment", "disease")
TITLE_MAX = 40
#: An entry that is not active today is "upcoming" when it next starts within
#: this many days (inclusive).
UPCOMING_DAYS = 60

# Ward staff read these, so they say who can fix it, not which file. The
# technical detail (site.toml, [site] state) is in the install runbook and the
# startup log.
SETUP_MESSAGE = (
    "This hospital's state has not been set up, so Pulse cannot show its "
    "seasonal calendar. Ask the person who installed Orb to set it."
)

_KIND_ORDER = {kind: i for i, kind in enumerate(KINDS)}
_ZONE_ID_RE = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$")
_ENTRY_ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_MMDD_RE = re.compile(r"^(\d{2})-(\d{2})$")
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

_ROOT_KEYS = {"version", "status", "reviewed_by", "reviewed_on", "compiled_on", "zones", "entries"}
_ZONE_KEYS = {"id", "name", "states"}
_ENTRY_KEYS = {
    "id",
    "kind",
    "title",
    "zones",
    "start",
    "end",
    "peak",
    "summary",
    "typically_rises",
    "sources",
}
_PEAK_KEYS = {"start", "end"}
_SOURCE_KEYS = {"name", "url", "accessed"}


# ═════════════════════════════════════════════════════════════════════════════
#  Errors
# ═════════════════════════════════════════════════════════════════════════════
class CalendarError(Exception):
    """The calendar file is missing, unreadable, or breaks a rule.

    `str()` is the whole operator-facing message, every problem on its own
    line. `public_message` is the one sentence a ward screen gets: no path, no
    detail, just what happened and who can fix it.
    """

    def __init__(self, path: Path | str, problems: list[str], *, missing: bool = False):
        self.path = str(path)
        self.problems = list(problems)
        self.missing = missing
        super().__init__(self._render())

    def _render(self) -> str:
        if self.missing:
            head = f"Seasonal calendar not found: {self.path}"
        else:
            head = (
                f"Seasonal calendar {self.path} was refused, so Pulse will not "
                f"show it. {len(self.problems)} problem(s):"
            )
        return "\n".join([head, *(f"  - {p}" for p in self.problems)])

    @property
    def public_message(self) -> str:
        if self.missing:
            return (
                "Pulse's seasonal calendar is not installed on this Orb, so there "
                "is nothing to show. Ask the person who installed Orb to check it."
            )
        return (
            "Pulse's seasonal calendar has errors, so Orb is not showing it. "
            "Ask the person who installed Orb to check it."
        )

    def copy(self) -> CalendarError:
        """A fresh instance to raise. Raising one cached instance on every
        request would grow its traceback without end."""
        return CalendarError(self.path, self.problems, missing=self.missing)


# ═════════════════════════════════════════════════════════════════════════════
#  Day arithmetic
# ═════════════════════════════════════════════════════════════════════════════
def _parse_mmdd(value: Any) -> tuple[int, int] | None:
    """(month, day) for a valid "MM-DD", else None. 02-29 is valid."""
    if not isinstance(value, str):
        return None
    m = _MMDD_RE.match(value)
    if not m:
        return None
    month, day = int(m.group(1)), int(m.group(2))
    try:
        date(2000, month, day)  # a leap year, so 02-29 is a real day
    except ValueError:
        return None
    return month, day


def _on(year: int, md: tuple[int, int]) -> date:
    """MM-DD in a given year. 02-29 reads as 02-28 when the year has none."""
    month, day = md
    if (month, day) == (2, 29) and not isleap(year):
        day = 28
    return date(year, month, day)


@dataclass(frozen=True)
class Span:
    """A yearly period, both ends inclusive. start > end wraps the new year."""

    start: tuple[int, int]
    end: tuple[int, int]

    @property
    def wraps(self) -> bool:
        return self.start > self.end

    def contains(self, day: date) -> bool:
        first, last = _on(day.year, self.start), _on(day.year, self.end)
        if self.wraps:
            return day >= first or day <= last
        return first <= day <= last

    def ends_on(self, day: date) -> date:
        """The last day of the period that contains `day`."""
        if self.wraps and day >= _on(day.year, self.start):
            return _on(day.year + 1, self.end)
        return _on(day.year, self.end)

    def next_start(self, day: date) -> date:
        """The first start date strictly after `day`."""
        first = _on(day.year, self.start)
        return first if first > day else _on(day.year + 1, self.start)

    def reference_days(self) -> set[int]:
        """Day-of-year numbers in a leap reference year, for containment checks."""
        first = date(2000, *self.start).timetuple().tm_yday
        last = date(2000, *self.end).timetuple().tm_yday
        if first <= last:
            return set(range(first, last + 1))
        return set(range(first, 367)) | set(range(1, last + 1))


def _mmdd_text(md: tuple[int, int]) -> str:
    return f"{md[0]:02d}-{md[1]:02d}"


# ═════════════════════════════════════════════════════════════════════════════
#  The loaded calendar
# ═════════════════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class Entry:
    id: str
    kind: str
    title: str
    zones: tuple[str, ...]
    start: str
    end: str
    span: Span
    peak: Span | None
    summary: str
    typically_rises: tuple[str, ...]
    sources: tuple[dict, ...]


@dataclass(frozen=True)
class SeasonalCalendar:
    path: str
    version: int
    status: str
    reviewed_by: str | None
    reviewed_on: str | None
    compiled_on: str
    zones: tuple[dict, ...]
    entries: tuple[Entry, ...]

    def header(self) -> dict:
        return {
            "version": self.version,
            "status": self.status,
            "reviewed_by": self.reviewed_by,
            "reviewed_on": self.reviewed_on,
            "compiled_on": self.compiled_on,
        }

    def zone_for_state(self, code: str) -> dict | None:
        for zone in self.zones:
            if code in zone["states"]:
                return zone
        return None


# ═════════════════════════════════════════════════════════════════════════════
#  Validation
# ═════════════════════════════════════════════════════════════════════════════
class _Check:
    """Collects every problem, so one refusal lists them all.

    json gives no positions for a parsed value, so a problem finds its line by
    searching the raw text for a pattern (a key, or `"id": "..."`). A needle is
    a pattern, or (pattern, n) for its n-th match, so the second of two entries
    sharing an id points at its own line. A miss is harmless: the problem is
    still reported, without a line number.
    """

    def __init__(self, raw: str):
        self.lines = raw.splitlines()
        self.problems: list[str] = []
        # Entry ids are searched for below this line, so an entry and a zone
        # that share a name cannot be confused.
        self.entries_from = self.line_of(r'"entries"\s*:') or 1

    def line_of(self, pattern: str, start: int = 1, nth: int = 1) -> int | None:
        rx = re.compile(pattern)
        seen = 0
        for n in range(max(1, start), len(self.lines) + 1):
            if rx.search(self.lines[n - 1]):
                seen += 1
                if seen == nth:
                    return n
        return None

    def fail(self, where: str, message: str, needle: Any = None) -> None:
        start = self.entries_from if where.startswith("entries[") else 1
        pattern, nth = needle if isinstance(needle, tuple) else (needle, 1)
        line = self.line_of(pattern, start, nth) if pattern else None
        at = f" (line {line})" if line else ""
        self.problems.append(f"{where}{at}: {message}")


def _id_pattern(value: str) -> str:
    return rf'"id"\s*:\s*"{re.escape(value)}"'


def _key_pattern(key: str) -> str:
    return rf'"{re.escape(key)}"\s*:'


def _is_text(value: Any) -> TypeGuard[str]:
    return isinstance(value, str) and bool(value.strip())


def _iso_date(value: Any) -> bool:
    if not isinstance(value, str) or not _ISO_DATE_RE.match(value):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _unknown_keys(check: _Check, where: str, obj: dict, allowed: set[str], needle: Any) -> None:
    extra = sorted(k for k in obj if k not in allowed and not str(k).startswith("_"))
    if extra:
        check.fail(
            where,
            f"unknown key(s) {extra}. Allowed: {sorted(allowed)}. "
            f"Keys starting with '_' are comments.",
            needle,
        )


def _check_header(check: _Check, doc: dict) -> None:
    version = doc.get("version")
    if isinstance(version, bool) or version != SCHEMA_VERSION:
        check.fail(
            "version",
            f"must be {SCHEMA_VERSION} (the version this build reads); got {version!r}",
            _key_pattern("version"),
        )
    status = doc.get("status")
    if status not in STATUSES:
        check.fail(
            "status", f"must be one of {list(STATUSES)}; got {status!r}", _key_pattern("status")
        )
    reviewed_by = doc.get("reviewed_by")
    reviewed_on = doc.get("reviewed_on")
    if reviewed_by is not None and not _is_text(reviewed_by):
        check.fail("reviewed_by", "must be null or a name", _key_pattern("reviewed_by"))
    if reviewed_on is not None and not _iso_date(reviewed_on):
        check.fail(
            "reviewed_on",
            f"must be null or a date written YYYY-MM-DD; got {reviewed_on!r}",
            _key_pattern("reviewed_on"),
        )
    if status == "reviewed" and (reviewed_by is None or reviewed_on is None):
        check.fail(
            "status",
            "is 'reviewed', so reviewed_by and reviewed_on must both say who reviewed it and when",
            _key_pattern("status"),
        )
    if status == "draft" and (reviewed_by is not None or reviewed_on is not None):
        check.fail(
            "status",
            "is 'draft', so reviewed_by and reviewed_on must both be "
            "null. A draft with a reviewer's name on it reads as "
            "reviewed.",
            _key_pattern("status"),
        )
    if not _iso_date(doc.get("compiled_on")):
        check.fail(
            "compiled_on",
            f"must be a date written YYYY-MM-DD; got {doc.get('compiled_on')!r}",
            _key_pattern("compiled_on"),
        )


def _check_zones(check: _Check, zones_raw: Any) -> list[dict]:
    if not isinstance(zones_raw, list) or not zones_raw:
        check.fail("zones", "must be a non-empty list", _key_pattern("zones"))
        return []
    zones: list[dict] = []
    seen_ids: set[str] = set()
    occurrences: dict[str, int] = {}
    owner: dict[str, str] = {}
    for i, zone in enumerate(zones_raw):
        if not isinstance(zone, dict):
            check.fail(f"zones[{i}]", "must be an object")
            continue
        zid = zone.get("id")
        where = f"zones[{i}] {zid!r}" if isinstance(zid, str) else f"zones[{i}]"
        needle = None
        if isinstance(zid, str):
            occurrences[zid] = occurrences.get(zid, 0) + 1
            needle = (_id_pattern(zid), occurrences[zid])
        _unknown_keys(check, where, zone, _ZONE_KEYS, needle)
        if not isinstance(zid, str) or not _ZONE_ID_RE.match(zid):
            check.fail(where, 'id must be snake_case, e.g. "west_coast"', needle)
        elif zid in seen_ids:
            check.fail(where, "id is used by an earlier zone", needle)
        else:
            seen_ids.add(zid)
        if not _is_text(zone.get("name")):
            check.fail(where, "name must be some text", needle)
        states = zone.get("states")
        if not isinstance(states, list) or not states:
            check.fail(where, "states must be a non-empty list of state codes", needle)
            states = []
        clean: list[str] = []
        for code in states:
            if not isinstance(code, str) or code not in STATES:
                guess = resolve_state(code) if isinstance(code, str) else None
                hint = f" Did you mean {guess!r}?" if guess else ""
                check.fail(where, f"{code!r} is not one of the 36 state codes.{hint}", needle)
                continue
            if code in owner:
                check.fail(
                    where,
                    f"{code} ({STATES[code]}) is already in zone "
                    f"{owner[code]!r}. A state belongs to exactly one zone.",
                    needle,
                )
                continue
            owner[code] = zid if isinstance(zid, str) else f"zones[{i}]"
            clean.append(code)
        zones.append({"id": zid, "name": zone.get("name"), "states": tuple(clean)})
    missing = [code for code in STATES if code not in owner]
    if missing:
        check.fail(
            "zones",
            "these state codes are in no zone, so a hospital there "
            "would see nothing: " + ", ".join(f"{c} ({STATES[c]})" for c in missing),
            _key_pattern("zones"),
        )
    return zones


def _check_span(check: _Check, where: str, obj: dict, needle: Any, label: str = "") -> Span | None:
    start, end = _parse_mmdd(obj.get("start")), _parse_mmdd(obj.get("end"))
    if start is None:
        check.fail(
            where,
            f"{label}start must be a real date written MM-DD; got {obj.get('start')!r}",
            needle,
        )
    if end is None:
        check.fail(
            where, f"{label}end must be a real date written MM-DD; got {obj.get('end')!r}", needle
        )
    if start is None or end is None:
        return None
    return Span(start, end)


def _check_sources(check: _Check, where: str, sources: Any, needle: Any) -> tuple:
    if not isinstance(sources, list) or not sources:
        check.fail(where, "sources must list at least one source", needle)
        return ()
    out = []
    for j, src in enumerate(sources):
        here = f"{where} sources[{j}]"
        if not isinstance(src, dict):
            check.fail(here, "must be an object with name, url and accessed", needle)
            continue
        _unknown_keys(check, here, src, _SOURCE_KEYS, needle)
        if not _is_text(src.get("name")):
            check.fail(here, "name must be some text", needle)
        url = src.get("url")
        if not isinstance(url, str) or not re.match(r"^https?://\S+$", url):
            check.fail(here, f"url must be an http(s) address; got {url!r}", needle)
        if not _iso_date(src.get("accessed")):
            check.fail(
                here,
                f"accessed must be a date written YYYY-MM-DD; got {src.get('accessed')!r}",
                needle,
            )
        out.append({"name": src.get("name"), "url": url, "accessed": src.get("accessed")})
    return tuple(out)


def _check_entries(check: _Check, entries_raw: Any, zone_ids: set[str]) -> list[Entry]:
    if not isinstance(entries_raw, list) or not entries_raw:
        check.fail("entries", "must be a non-empty list", _key_pattern("entries"))
        return []
    entries: list[Entry] = []
    seen_ids: set[str] = set()
    occurrences: dict[str, int] = {}
    for i, raw in enumerate(entries_raw):
        if not isinstance(raw, dict):
            check.fail(f"entries[{i}]", "must be an object")
            continue
        eid = raw.get("id")
        where = f"entries[{i}] {eid!r}" if isinstance(eid, str) else f"entries[{i}]"
        needle = None
        if isinstance(eid, str):
            occurrences[eid] = occurrences.get(eid, 0) + 1
            needle = (_id_pattern(eid), occurrences[eid])
        before = len(check.problems)

        _unknown_keys(check, where, raw, _ENTRY_KEYS, needle)
        if not isinstance(eid, str) or not _ENTRY_ID_RE.match(eid):
            check.fail(where, 'id must be kebab-case, e.g. "southwest-monsoon"', needle)
        elif eid in seen_ids:
            check.fail(where, "id is used by an earlier entry", needle)
        else:
            seen_ids.add(eid)

        kind = raw.get("kind")
        if kind not in KINDS:
            check.fail(where, f"kind must be one of {list(KINDS)}; got {kind!r}", needle)

        title = raw.get("title")
        if not _is_text(title):
            check.fail(where, "title must be some text", needle)
        elif len(title) > TITLE_MAX:
            check.fail(
                where,
                f"title is {len(title)} characters; the most a Pulse card shows is {TITLE_MAX}",
                needle,
            )

        zones = raw.get("zones")
        if not isinstance(zones, list) or not zones:
            check.fail(where, "zones must list at least one zone id", needle)
            zones = []
        for z in zones:
            if not isinstance(z, str) or z not in zone_ids:
                check.fail(where, f"zone {z!r} is not defined in zones", needle)
        if len(set(map(str, zones))) != len(zones):
            check.fail(where, "zones lists the same zone twice", needle)

        span = _check_span(check, where, raw, needle)

        peak = None
        if raw.get("peak") is not None:  # absent or null: no peak
            peak_raw = raw["peak"]
            if not isinstance(peak_raw, dict):
                check.fail(where, "peak must be an object with start and end", needle)
            else:
                _unknown_keys(check, f"{where} peak", peak_raw, _PEAK_KEYS, needle)
                peak = _check_span(check, where, peak_raw, needle, label="peak ")
                if (
                    peak is not None
                    and span is not None
                    and not peak.reference_days() <= span.reference_days()
                ):
                    check.fail(
                        where,
                        f"peak {_mmdd_text(peak.start)} to "
                        f"{_mmdd_text(peak.end)} is not inside the range "
                        f"{_mmdd_text(span.start)} to {_mmdd_text(span.end)}",
                        needle,
                    )

        if not _is_text(raw.get("summary")):
            check.fail(where, "summary must be some text", needle)

        rises = raw.get("typically_rises")
        if not isinstance(rises, list) or not all(_is_text(r) for r in rises):
            check.fail(
                where, "typically_rises must be a list of text items (it may be empty)", needle
            )
            rises = []

        sources = _check_sources(check, where, raw.get("sources"), needle)

        if len(check.problems) == before and span is not None:
            assert isinstance(eid, str) and isinstance(kind, str) and isinstance(title, str)
            entries.append(
                Entry(
                    id=eid,
                    kind=kind,
                    title=title.strip(),
                    zones=tuple(zones),
                    start=raw["start"],
                    end=raw["end"],
                    span=span,
                    peak=peak,
                    summary=raw["summary"].strip(),
                    typically_rises=tuple(r.strip() for r in rises),
                    sources=sources,
                )
            )
    return entries


def load_calendar(path: Path | str | None = None) -> SeasonalCalendar:
    """Read and fully validate the calendar. PURE: no cache, no logging.

    Raises CalendarError listing every problem found, all in one message.
    """
    target = Path(path) if path is not None else calendar_path()
    if not target.is_file():
        raise CalendarError(
            target,
            [
                f"there is no file at this path. Orb ships one at "
                f"server/site_config/seasonal_calendar.json; set {CALENDAR_PATH_ENV} to "
                f"use another."
            ],
            missing=True,
        )
    try:
        raw = target.read_text(encoding="utf-8")
    except OSError as e:
        raise CalendarError(target, [f"the file could not be read ({e.strerror or e})"]) from None
    try:
        doc = json.loads(raw)
    except json.JSONDecodeError as e:
        raise CalendarError(
            target, [f"line {e.lineno}, column {e.colno}: not valid JSON: {e.msg}"]
        ) from None
    if not isinstance(doc, dict):
        raise CalendarError(target, ["the file must hold one JSON object"])

    check = _Check(raw)
    _unknown_keys(check, "<root>", doc, _ROOT_KEYS, None)
    _check_header(check, doc)
    zones = _check_zones(check, doc.get("zones"))
    zone_ids = {z["id"] for z in zones if isinstance(z["id"], str)}
    entries = _check_entries(check, doc.get("entries"), zone_ids)
    if check.problems:
        raise CalendarError(target, check.problems)

    return SeasonalCalendar(
        path=str(target),
        version=doc["version"],
        status=doc["status"],
        reviewed_by=doc.get("reviewed_by"),
        reviewed_on=doc.get("reviewed_on"),
        compiled_on=doc["compiled_on"],
        zones=tuple(zones),
        entries=tuple(entries),
    )


# ═════════════════════════════════════════════════════════════════════════════
#  Process cache
# ═════════════════════════════════════════════════════════════════════════════
# Keyed by path, so a test that points ORB_SEASONAL_CALENDAR_FILE elsewhere gets
# its own file. A failure is cached too: it is logged once, not on every request.
_cache: dict[str, SeasonalCalendar | CalendarError] = {}
_cache_lock = threading.Lock()


def calendar_path() -> Path:
    override = os.environ.get(CALENDAR_PATH_ENV)
    return Path(override) if override else _DEFAULT_PATH


def get_calendar() -> SeasonalCalendar:
    """The calendar for this process. Raises CalendarError when it is unusable."""
    key = str(calendar_path())
    with _cache_lock:
        hit = _cache.get(key)
        if hit is None:
            try:
                hit = load_calendar(key)
                logger.info(
                    "Seasonal calendar loaded from %s: %d zone(s), %d entr%s, status %s",
                    key,
                    len(hit.zones),
                    len(hit.entries),
                    "y" if len(hit.entries) == 1 else "ies",
                    hit.status,
                )
            except CalendarError as e:
                hit = e
                logger.error("%s\nPulse answers 503 until this is fixed and Orb restarted.", e)
            _cache[key] = hit
    if isinstance(hit, CalendarError):
        raise hit.copy()
    return hit


def reload_calendar() -> None:
    """Forget every loaded calendar. Tests, and nothing else: an operator restarts."""
    with _cache_lock:
        _cache.clear()


def preload() -> None:
    """Load at startup so a bad file is in the boot log, not found on first click.
    Never raises."""
    try:
        get_calendar()
    except CalendarError:
        pass  # already logged


# ═════════════════════════════════════════════════════════════════════════════
#  What Pulse and Forecast show
# ═════════════════════════════════════════════════════════════════════════════
def _site_state() -> str | None:
    from services import site_config_service

    raw = getattr(site_config_service.current_site_config(), "site_state", None)
    return resolve_state(raw)


def _card(entry: Entry, in_peak: bool) -> dict:
    return {
        "id": entry.id,
        "kind": entry.kind,
        "title": entry.title,
        "start": entry.start,
        "end": entry.end,
        "in_peak": in_peak,
        "summary": entry.summary,
        "typically_rises": list(entry.typically_rises),
        "sources": [dict(s) for s in entry.sources],
    }


def _sort_key(card: dict) -> tuple:
    return (_KIND_ORDER[card["kind"]], card["title"].casefold(), card["id"])


def season_report(today: date | None = None) -> dict:
    """The body of GET /api/pulse/season. Raises CalendarError when the
    calendar is unusable (the route turns that into a 503)."""
    cal = get_calendar()
    today = today or hospital_time.hospital_today()
    code = _site_state()
    body: dict = {
        "date": today.isoformat(),
        "state": None,
        "zone": None,
        "setup_needed": code is None,
        "setup_message": SETUP_MESSAGE if code is None else None,
        "calendar": cal.header(),
        "active": [],
        "upcoming": [],
    }
    if code is None:
        return body
    zone = cal.zone_for_state(code)
    body["state"] = {"code": code, "name": state_name(code)}
    if zone is None:  # pragma: no cover  (validation puts every code in a zone)
        return body
    body["zone"] = {"id": zone["id"], "name": zone["name"]}

    active, upcoming = [], []
    for entry in cal.entries:
        if zone["id"] not in entry.zones:
            continue
        if entry.span.contains(today):
            card = _card(entry, bool(entry.peak and entry.peak.contains(today)))
            card["ends_on"] = entry.span.ends_on(today).isoformat()
            active.append(card)
            continue
        starts_on = entry.span.next_start(today)
        days_away = (starts_on - today).days
        if days_away <= UPCOMING_DAYS:
            card = _card(entry, False)
            card["starts_on"] = starts_on.isoformat()
            card["days_away"] = days_away
            upcoming.append(card)

    body["active"] = sorted(active, key=_sort_key)
    body["upcoming"] = sorted(upcoming, key=lambda c: (c["days_away"], *_sort_key(c)))
    return body


def season_context(today: date | None = None) -> list[dict]:
    """Today's active entries as Forecast shows them: context beside the
    numbers, never a multiplier on them. Empty when the state is not set, the
    calendar is unusable, or nothing is active. Never raises: a seasonal note
    must not take the bed forecast down with it."""
    try:
        report = season_report(today)
    except CalendarError:
        return []
    except Exception:  # noqa: BLE001
        logger.exception("Seasonal context for Forecast failed; showing none")
        return []
    return [
        {"id": c["id"], "title": c["title"], "kind": c["kind"], "in_peak": c["in_peak"]}
        for c in report["active"]
    ]
