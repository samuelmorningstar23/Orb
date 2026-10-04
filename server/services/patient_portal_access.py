"""When a patient's Bridge login works.

RULING R029 (4 October 2026, option (a), end of day): Bridge is for the
admission only. The login works while the patient is admitted, and until the
end of the hospital-local day of their latest discharge. After that every lane
a patient login can use refuses it: password sign-in (403 with the reason),
every authenticated request (401, so the app's session-expiry path returns the
patient to sign-in, where the reason is shown), and the live socket.

Why it ends at all: after discharge the patient's case room is closed in Relay
(relay_service hides it), so a Bridge emergency message reached nobody while
the reply said "Your care team has been notified", and the reply told a patient
at home to press a call button they no longer have. The medicines and status
cards also read the inpatient record as if it were current. And the appliance
never answers from outside the hospital (R027), so the only post-discharge use
was a patient back on site, a few metres from the front desk.

Why it is a RULE, not a switch flipped at discharge: the hospital-system build
plans to readmit a returning patient into the same file (HIS_BUILD_PLAN W6-B).
A login switched off at discharge would stay off on readmission unless someone
remembered to switch it back. Judged from the record each time, a readmitted
file (status 'admitted' again) opens again on its own, and an open session is
cut off at the deadline because the check runs on every request.

Every status other than 'admitted' and 'discharged' ('transferred' out,
'registered' and never admitted) has no open portal. An account with no linked
patient row is left as it was: the portal itself says it cannot find records.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional

PORTAL_CLOSED_MESSAGE = (
    "Your hospital stay has ended, so this portal is closed. Your discharge "
    "summary was given to you before you left. If you need help, contact the hospital."
)


def _end_of_local_day_utc(stamp) -> Optional[datetime]:
    """The UTC instant the hospital-local day containing `stamp` ends, or None."""
    from services.hospital_time import local_day_bounds_utc, to_hospital

    local = to_hospital(stamp)
    if local is None:
        return None
    return local_day_bounds_utc(local.date())[1]


def portal_access(db, user_id: str) -> Dict[str, Any]:
    """Is this patient login's portal open right now? Blocking; pass an open db.

    Returns {"open": bool, "status": str|None, "until": iso str|None,
    "reason": str|None}. `until` is set for a discharged patient (the end of the
    discharge day, UTC). `reason` is the patient-facing sentence when closed.
    """
    from services.hospital_time import _utc_now

    row = db.execute(
        "SELECT id, status FROM patients WHERE user_id = ? LIMIT 1", (user_id,)
    ).fetchone()
    if row is None:
        return {"open": True, "status": None, "until": None, "reason": None}
    status = row["status"]
    if status == "admitted":
        return {"open": True, "status": status, "until": None, "reason": None}
    until = None
    if status == "discharged":
        last = db.execute(
            "SELECT MAX(discharged_at) AS at FROM discharge_summaries WHERE patient_id = ?",
            (row["id"],),
        ).fetchone()
        # No discharge time on record: fail closed rather than open forever.
        until = _end_of_local_day_utc(last["at"]) if last and last["at"] else None
        if until is not None and _utc_now() < until:
            return {"open": True, "status": status, "until": until.isoformat(), "reason": None}
    return {"open": False, "status": status,
            "until": until.isoformat() if until else None,
            "reason": PORTAL_CLOSED_MESSAGE}


def portal_access_for(user_id: str) -> Dict[str, Any]:
    """portal_access with its own session. Blocking; call via run_db from async."""
    from database import db_session

    with db_session() as db:
        return portal_access(db, user_id)
