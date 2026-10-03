"""Carry-forward of vitals a new reading did not measure, with an age limit.

A monitor message usually carries only some vitals (HR and SpO2 every few
seconds, a cuff pressure every 15 to 60 minutes), and no monitor ever carries
ACVPU or supplemental O2, which are nurse assessments. The feed lane fills the
gaps from the patient's latest row so NEWS2 is scored on a full set, and the
simulator does the same for ACVPU and O2.

That carry had no age limit (independent review 2026-10-02, findings F9, G1,
G6). Measured end to end: an SBP 78 and RR 30 from 02:00 were scored, printed
as "Current vitals" and alerted as "Severe tachypnea (RR 30); Severe
hypotension (SBP 78)" on an HR-only message at 13:00. A nurse's one "confused"
led every later simulator alert for as long as the simulator ran.

RULED by Samuel 2026-10-03:
  * A carried value is only as fresh as its MEASUREMENT. Each row stores, in
    `vitals.carried_from` (migration 47), when every value it copied was really
    measured, and the next reading measures age from there. Without that, a
    monitor sending HR every minute re-stamps a 02:00 BP as one minute old on
    every row, and no limit ever bites.
  * The limits are CARRY_LIMITS below. Past its limit a value is not carried:
    it becomes None, meaning not measured, which scores 0 and leaves the NEWS2
    marked incomplete instead of presenting an old reading as current.
  * The simulator follows the same rule for ACVPU and O2.

A value whose age cannot be established (no timestamp, or one that does not
parse) is not carried either: the rule is that a value must be shown to be
fresh, not that it must be shown to be stale.

A value measured AFTER the new reading (an out-of-order message) is carried as
before; changing that is a separate decision (see the review's G-items).
"""
from __future__ import annotations

import json
import logging
from datetime import timedelta
from typing import Any, Iterable, Mapping, Optional

from services.clinical_time import format_utc, parse_to_utc

logger = logging.getLogger("orb.vitals_carry")

#: How long a value may stand in for a measurement nobody took. Monitor vitals:
#: the caps in the 2026-10-01 validation protocol (research_2026-10-01/R5,
#: section 5.1), so the live feed and the PhysioNet replay score the same way.
#: ACVPU and supplemental O2: NEWS2's minimum observation frequency (12-hourly),
#: so a nurse's assessment expires only once the next obs round is overdue.
CARRY_LIMITS: dict[str, timedelta] = {
    "heart_rate": timedelta(hours=4),
    "systolic_bp": timedelta(hours=4),
    "diastolic_bp": timedelta(hours=4),
    "spo2": timedelta(hours=4),
    "respiratory_rate": timedelta(hours=4),
    "temperature": timedelta(hours=8),
    "consciousness": timedelta(hours=12),
    "on_oxygen": timedelta(hours=12),
}


def _field(row: Any, name: str) -> Any:
    """`row[name]` for a dict or a sqlite3.Row, None when the column is absent."""
    try:
        keys = row.keys()
    except AttributeError:
        return None
    return row[name] if name in keys else None


def _has_value(column: str, value: Any) -> bool:
    if column == "consciousness":
        # A blank ACVPU is an absence, exactly as compute_news2 treats it.
        return bool(value and str(value).strip())
    return value is not None


def carry_forward(latest: Optional[Mapping], observed_at: str,
                  columns: Iterable[str]) -> tuple[dict, dict]:
    """The values in `latest` still fresh at `observed_at`, and when each was measured.

    Returns ``(values, carried)``. ``values[column]`` is the latest row's value,
    or None when it is absent or past its CARRY_LIMITS entry. ``carried`` maps
    every column actually carried to the UTC time it was really measured; store
    it on the new row with `dump_carried` so the next reading ages it correctly.
    """
    values: dict = {c: None for c in columns}
    carried: dict[str, str] = {}
    if not latest:
        return values, carried

    try:
        prior = json.loads(_field(latest, "carried_from") or "{}")
        if not isinstance(prior, dict):
            prior = {}
    except (TypeError, ValueError):
        prior = {}
    row_time = _field(latest, "timestamp")

    try:
        now = parse_to_utc(observed_at)
    except (TypeError, ValueError):
        logger.warning("carry-forward: unreadable observation time %r; carrying nothing",
                       observed_at)
        return values, carried

    for column in values:
        value = _field(latest, column)
        if not _has_value(column, value):
            continue
        measured = prior.get(column) or row_time
        try:
            measured_at = parse_to_utc(measured)
        except (TypeError, ValueError):
            logger.warning("carry-forward: %s has no readable measurement time (%r); "
                           "not carried", column, measured)
            continue
        if now - measured_at > CARRY_LIMITS[column]:
            continue
        values[column] = value
        carried[column] = format_utc(measured_at)
    return values, carried


def dump_carried(carried: Mapping[str, str]) -> Optional[str]:
    """The `vitals.carried_from` value for a row: JSON, or NULL when nothing was carried."""
    return json.dumps(dict(sorted(carried.items()))) if carried else None
