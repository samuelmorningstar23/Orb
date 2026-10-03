"""The hospital's own calendar day.

`datetime.now(timezone.utc).date()` is NOT "today" on a ward. In a UTC+05:30
hospital every reading between 00:00 and 05:30 IST returns YESTERDAY's date, so
a server-computed day key is wrong for the first five and a half hours of every
day — which is night shift, exactly when a "Today" list is read hardest. Two
surfaces were built on that mistake:

  * `GET /api/surgery/schedule` started its week at the UTC date, so before
    05:30 the first card was yesterday's list.
  * the Forecast day-0 row is stamped with a date key the browser compares
    against ITS local date (src/lib/datetime.ts:localDateKey) — a UTC key never
    matches during that window.

There is deliberately ONE resolver, so the surgical Today badge and the forecast
day-0 row cannot disagree about what day it is.

The zone is the appliance's, from
`services.vitals_source.hl7v2.appliance_timezone()`: `ORB_HL7_DEFAULT_TZ` (which
site config writes from the site's declared `timezone`), else the appliance's OS
zone, else UTC. The appliance sits in the hospital, so its zone IS the
hospital's — the same reasoning that makes it the right default for an
offset-less HL7 timestamp.

Nothing here formats or parses stored data: every timestamp Orb persists stays
UTC. This module only answers "which calendar day is it HERE, now", and converts
such a day back into the UTC window it occupies.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone, tzinfo


def hospital_timezone() -> tzinfo:
    """The appliance's (= the hospital's) timezone."""
    # Imported lazily: hl7v2 is pure protocol code with no Orb dependencies and
    # must stay importable on its own (see services/vitals_source/__init__.py).
    from services.vitals_source.hl7v2 import appliance_timezone

    return appliance_timezone()


def _utc_now() -> datetime:
    """The current instant, aware, UTC.

    Its own function so a test can pin an instant by patching this one name —
    the suite has no freezegun, and the 00:00–05:30 IST window this module
    exists for cannot be exercised by waiting for it.
    """
    return datetime.now(timezone.utc)


def hospital_now() -> datetime:
    """Now, as hospital-local wall-clock time (aware)."""
    return _utc_now().astimezone(hospital_timezone())


def hospital_today() -> date:
    """The hospital's current calendar day."""
    return hospital_now().date()


def to_hospital(value) -> datetime | None:
    """A stored instant as hospital wall-clock time (aware), or None.

    Takes an aware datetime, an ISO string with an offset, or the bare
    'YYYY-MM-DD HH:MM:SS' that SQLite's CURRENT_TIMESTAMP writes, which is UTC.
    A naive value is therefore read as UTC, never as local time.
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        stamp = value
    else:
        try:
            stamp = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.astimezone(hospital_timezone())


def hospital_stamp(value) -> str:
    """'2 Oct 2026, 13:19 IST': a stored instant as the ward reads it.

    For printed and spoken text only (sheets, packs, prompts). A UTC clock on a
    handover sheet in an IST hospital is 5.5 hours wrong to everyone who reads
    it. Returns "" when the value cannot be read, so the caller decides what an
    unreadable time looks like.
    """
    local = to_hospital(value)
    if local is None:
        return ""
    return f"{local.day} {local.strftime('%b %Y, %H:%M')} {local.tzname() or ''}".rstrip()


def local_day_bounds_utc(day: date | None = None) -> tuple[datetime, datetime]:
    """The `[start, end)` UTC instants spanned by a hospital-local calendar day.

    Use this — never a `date(...)` comparison against a stored column — to count
    what happened "today": stored timestamps are UTC, and a hospital-local day
    is a UTC window that straddles two UTC dates.
    """
    tz = hospital_timezone()
    day = day or hospital_today()
    start_local = datetime.combine(day, time.min, tzinfo=tz)
    # Midnight to midnight by WALL CLOCK, then converted. On a zone that shifts
    # (India does not, but this module is not India-only) that is a 23- or
    # 25-hour UTC window — which is the correct length of that calendar day.
    end_local = start_local + timedelta(days=1)
    return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)
