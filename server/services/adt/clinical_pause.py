"""Orb: clinical lanes honour presence (adt-11, W9-B). The one presence helper.

A patient recorded on leave or absconded (services/adt/presence.py, adt-10) is
not on the ward. Before this module every clinical lane carried on as if they
were: the nurse board said their obs and doses were overdue, the escalation
ladder climbed to the registrar and the consultant about a patient nobody
could see, sepsis auto-start opened a bundle, the vitals simulator wrote
readings for them and the downtime ward pack printed them as if in bed.

RULING ADT-S4, BUILT AS OPTION (a) (held for Samuel to confirm before go-live):

  * the patient stays on every board, with a 'Not on ward' chip (removing them
    recreates the orphaning failure);
  * the nurse board pauses their obs-due and medication-due clocks and says
    why (nurse_shift_service);
  * an open escalation is paused, never closed, and resumes on return; an
    absconded patient with an open escalation names the treating doctor in
    the case room (escalation_service);
  * sepsis auto-start opens nothing for them (sepsis_bundle_service);
  * the vitals simulator writes nothing for them (bot_service);
  * the downtime ward pack marks them NOT ON WARD (downtime_service).

Every one of those lanes asks THIS module, and nothing else, whether a patient
is away. None of them reads admissions.presence_status itself.

WHAT COUNTS AS AWAY. The patient's active admission has presence_status
'on_leave' or 'absconded'. presence_since is when they were last on the ward.

THE SWITCH. Only while ORB_ADT is 'on' (presence_by_patient's own rule).
With the module off, or draining, away_by_patient answers None and every lane
behaves exactly as it did before adt-11: nothing is paused, nothing is
skipped, no key is added to any payload. An escalation paused while the module
was on resumes as soon as it is off, because a paused clock with nobody able
to record the return would be a silently dropped escalation.

A SET TAKEN AFTER THE PATIENT LEFT IS LIVE. A set of observations whose
observation time is after presence_since was taken by somebody with the
patient in front of them (the return may simply not be recorded yet). Such a
set is never suppressed: it scores, raises or extends an escalation (which
then runs live) and, charted at the bedside, opens a sepsis bundle, exactly as
before. Only the AUTOMATIC lanes (the board clocks, the escalation timer,
auto-start from a feed or the simulator, the simulator itself) stand down.
This leans the strict way on purpose: when Orb cannot tell, it keeps alerting.

Vitals charted after the return is recorded are live in every lane: the
return clears presence_status, so the patient is simply on the ward again.
The obs-due clock then runs from the last set a person took, so a patient back
from a long leave reads overdue until a set is taken on return.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from services.adt.core import parse_instant
from services.adt.presence import presence_by_patient

__all__ = [
    "AWAY_STATUSES",
    "AWAY_WORDS",
    "away_by_patient",
    "away_for",
    "left_ward_at",
    "observed_after_leaving",
    "pause_reason",
]

#: The presences that mean the patient is not on the ward.
AWAY_STATUSES = ("on_leave", "absconded")

#: How each presence reads inside a sentence.
AWAY_WORDS: dict[str, str] = {"on_leave": "on leave", "absconded": "absconded"}


def away_by_patient(patient_ids: Any, *, db: Any = None) -> dict | None:
    """patient id -> {presence_status, presence_since} for the patients asked
    about who are recorded off the ward. Patients on the ward are left out.

    None while ORB_ADT is not 'on': the caller then changes nothing at all
    (the flag-off promise). Pass `db` to read on the caller's connection."""
    presence = presence_by_patient(patient_ids, db=db)
    if presence is None:
        return None
    out: dict[str, dict] = {}
    for pid, here in presence.items():
        status = (here or {}).get("presence_status")
        if status in AWAY_STATUSES:
            out[pid] = {
                "presence_status": status,
                "presence_since": (here or {}).get("presence_since"),
            }
    return out


def away_for(patient_id: str | None, *, db: Any = None) -> dict | None:
    """{presence_status, presence_since} when this patient is recorded off the
    ward and the module is on; None otherwise."""
    if not patient_id:
        return None
    found = away_by_patient([patient_id], db=db)
    return (found or {}).get(patient_id)


def left_ward_at(away: dict | None) -> datetime | None:
    """When the patient was last on the ward, as an aware UTC datetime, or
    None when it is not recorded or cannot be read."""
    return parse_instant((away or {}).get("presence_since"))


def observed_after_leaving(away: dict | None, observed_at: Any) -> bool:
    """True when a set observed at `observed_at` was taken after the patient
    left the ward, which makes it live (see the module docstring).

    When the leaving time is not recorded Orb cannot prove the set is older,
    so any readable observation time counts as live: the strict direction."""
    if not away:
        return False
    when = parse_instant(observed_at)
    if when is None:
        return False
    left = left_ward_at(away)
    return left is None or when > left


def pause_reason(away: dict | None, *, medication_round: bool = True) -> str | None:
    """The sentence a board shows beside a paused patient, or None when the
    patient is on the ward. No time in it: a screen prints the time from
    presence_since in its own clock."""
    if not away:
        return None
    words = AWAY_WORDS.get(away.get("presence_status") or "", "off the ward")
    what = "Observations and doses are" if medication_round else "Observations are"
    return (
        f"{what} paused while the patient is {words}. They restart when the "
        "return to the ward is recorded."
    )
