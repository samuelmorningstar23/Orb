"""Orb: how a medicine on hold reads wherever a drug list is shown or handed
on (medorders-6b, W7-E; design section 7).

A held order keeps its ward row's status ('active' or 'prn': the interaction
screen and the regimen still see the drug) and sets medications.held_at and
hold_reason (core.projection_for). Those two columns are the marker every
reader checks. They are NULL on every row of a database that never held an
order, so every reader using this module prints exactly what it printed before.

One wording, so the handover, the discharge reconciliation, the export and
Sage cannot describe the same hold three ways:

  hold_phrase(row)    'on hold since 9 Oct 2026, 14:20 IST: Nil by mouth'
                      (the clinical readers: I-PASS, discharge, Sage)
  PORTAL_STATUS       'On hold', and nothing else: the patient portal shows
                      no colour, no time and no reason (the portal rule)

tests/test_medorder_reader_registry.py lists every module that reads FROM
medications and what a hold does to it.
"""
from __future__ import annotations

from typing import Any, Mapping, Optional

from services.hospital_time import hospital_stamp

__all__ = ["HELD_COLUMNS", "PORTAL_STATUS", "is_held", "hold_phrase"]

#: The two projection columns a reader adds to its SELECT.
HELD_COLUMNS = ("held_at", "hold_reason")

#: The portal's plain status for a held medicine.
PORTAL_STATUS = "On hold"


def _field(row: Any, name: str) -> Optional[Any]:
    if row is None:
        return None
    if isinstance(row, Mapping):
        return row.get(name)
    try:
        return row[name]
    except (IndexError, KeyError):
        return None


def is_held(row: Any) -> bool:
    """Is this ward row's order on hold? held_at is set only by a hold."""
    return bool(_field(row, "held_at"))


def hold_phrase(row: Any) -> str:
    """'on hold since <ward time>: <reason>', for the clinical readers. Says
    only 'on hold' when the time cannot be read and leaves the reason off when
    none was recorded. '' for a row that is not held."""
    if not is_held(row):
        return ""
    when = hospital_stamp(_field(row, "held_at"))
    reason = " ".join(str(_field(row, "hold_reason") or "").split())
    phrase = f"on hold since {when}" if when else "on hold"
    return f"{phrase}: {reason}" if reason else phrase
