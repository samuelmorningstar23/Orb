"""The front office day summary (opd-18): one hospital-local day, in counts.

GET /api/frontdesk/mis/daily answers, for one hospital-local calendar day and
the caller's own clinic (permissions.department_scope; every clinic for an
account with no department):

  * registrations: patients seen that day whose record was new that day,
    against patients who already had one (returning), and how many visits
    were new, follow-up, review or procedure visits;
  * visits by clinic and by doctor, with what became of them;
  * the wait to be called in: median and longest, over the visits that were
    called, and how many are still waiting;
  * left without being seen (the LWBS cancel code, opd-8) and every other
    cancel by its code;
  * no-shows: appointments of that day nobody checked in, recorded by the
    desk or computed once their clinic session ended (opd-16, the same rule
    the Appointments day view reads by);
  * conversions: visits that ended in an admission (opd-17a).

AGGREGATES ONLY. The answer carries no patient id, name, UHID, visit id,
token or complaint: nothing in it identifies a patient, so a read writes no
phi_access row (the census precedent, adt-13a). Doctor names are staff, and
the desk's own roster already shows them.

THE DAY IS THE HOSPITAL'S. opd_visits.visit_date and appointments'
scheduled_date are already hospital-local days; a record's creation instant
is stored UTC and is converted with services.hospital_time before it is
compared. A day after today is refused (422 'future_day'): it has no data
yet, and a zero would read as a quiet day.

READ-ONLY, no scoring. Nothing here imports a scoring entry point or reads
vitals or risk scores (the OPD no-scoring rule); the waits are clock
arithmetic on the visit's own timestamps.
"""
from __future__ import annotations

import statistics
from datetime import date, datetime, timezone
from typing import Any, Iterable, Optional

from database import db_session
from services.hospital_time import hospital_today, to_hospital

__all__ = [
    "MisError",
    "LWBS_CODE",
    "VISIT_TYPES",
    "VISIT_STATUSES",
    "daily_summary",
    "resolve_day",
]

#: The cancel code that means "left without being seen" (opd_queue_service).
LWBS_CODE = "left_without_being_seen"

#: opd_visits.visit_type values, in the order the summary lists them.
VISIT_TYPES: tuple[tuple[str, str], ...] = (
    ("new", "New"),
    ("follow_up", "Follow-up"),
    ("review", "Review"),
    ("procedure", "Procedure"),
)

#: opd_visits.status values, in the order the summary lists them.
VISIT_STATUSES: tuple[str, ...] = ("waiting", "in_consultation", "completed", "cancelled")

_CHUNK = 400


class MisError(ValueError):
    """A request the summary refuses (422)."""

    def __init__(self, code: str, message: str, status: int = 422):
        super().__init__(message)
        self.code = code
        self.status = status


def resolve_day(value: Optional[str]) -> date:
    """The hospital-local day asked for: today when blank, never after today."""
    today = hospital_today()
    text = (value or "").strip()
    if not text:
        return today
    if len(text) != 10:
        raise MisError("bad_date", "The date must be written YYYY-MM-DD.")
    try:
        day = date.fromisoformat(text)
    except ValueError as exc:
        raise MisError("bad_date", "The date must be written YYYY-MM-DD.") from exc
    if day > today:
        raise MisError("future_day", "That day has not happened yet.")
    return day


def _table_exists(db, name: str) -> bool:
    return db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _columns(db, table: str) -> set[str]:
    return {r[1] for r in db.execute(f"PRAGMA table_info({table})").fetchall()}


def _parse_utc(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        stamp = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00").replace(" ", "T", 1))
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp


def _wait_minutes(visit: dict) -> Optional[int]:
    """Registered to called in, in whole minutes; None when either time is
    missing or unreadable (never 0, which would read as 'seen at once')."""
    start = _parse_utc(visit.get("registered_at"))
    called = _parse_utc(visit.get("called_at"))
    if start is None or called is None:
        return None
    return max(0, int((called - start).total_seconds() // 60))


def _median(values: list[int]) -> Optional[float]:
    if not values:
        return None
    return round(float(statistics.median(values)), 1)


def _wait_block(visits: Iterable[dict]) -> dict:
    waits = [w for w in (_wait_minutes(v) for v in visits) if w is not None]
    return {
        "measured": len(waits),
        "median_minutes": _median(waits),
        "longest_minutes": max(waits) if waits else None,
    }


def _outcome_counts(visits: list[dict]) -> dict:
    return {
        "total": len(visits),
        "waiting": sum(1 for v in visits if v["status"] == "waiting"),
        "in_consultation": sum(1 for v in visits if v["status"] == "in_consultation"),
        "completed": sum(1 for v in visits if v["status"] == "completed"),
        "cancelled": sum(1 for v in visits if v["status"] == "cancelled"),
        "left_without_being_seen": sum(
            1 for v in visits
            if v["status"] == "cancelled" and v.get("cancel_reason_code") == LWBS_CODE),
        "admitted": sum(1 for v in visits if _converted(v)),
    }


def _converted(visit: dict) -> bool:
    return (visit.get("disposition") or "") == "admitted" or bool(visit.get("admission_id"))


def _visits_on(db, day: str, department: Optional[str]) -> list[dict]:
    if not _table_exists(db, "opd_visits"):
        return []
    have = _columns(db, "opd_visits")
    wanted = ["id", "patient_id", "department", "doctor_id", "doctor_name", "visit_type",
              "status", "registered_at", "called_at", "cancel_reason_code",
              "disposition", "admission_id"]
    select = ", ".join(c if c in have else f"NULL AS {c}" for c in wanted)
    sql = f"SELECT {select} FROM opd_visits WHERE visit_date = ?"
    params: list[Any] = [day]
    if department is not None:
        sql += " AND department = ?"
        params.append(department)
    sql += " ORDER BY department, COALESCE(doctor_name, ''), id"
    return [dict(r) for r in db.execute(sql, params).fetchall()]


def _registration_split(db, day: date, patient_ids: list[str]) -> dict:
    """New (record created on `day`), returning (created before it) and not
    known (no readable creation time), over the day's distinct patients. The
    creation instant is the registration desk's (patient_demographics) when
    there is one, else the patients row's own."""
    new = returning = unknown = 0
    has_demo = _table_exists(db, "patient_demographics")
    for start in range(0, len(patient_ids), _CHUNK):
        chunk = patient_ids[start:start + _CHUNK]
        marks = ",".join("?" for _ in chunk)
        if has_demo:
            sql = (f"SELECT p.id, COALESCE(d.registered_at, p.created_at) AS created "
                   f"FROM patients p LEFT JOIN patient_demographics d ON d.patient_id = p.id "
                   f"WHERE p.id IN ({marks})")
        else:
            sql = f"SELECT p.id, p.created_at AS created FROM patients p WHERE p.id IN ({marks})"
        found = {r["id"]: r["created"] for r in db.execute(sql, chunk).fetchall()}
        for pid in chunk:
            local = to_hospital(found.get(pid))
            if local is None or local.date() > day:
                unknown += 1
            elif local.date() == day:
                new += 1
            else:
                returning += 1
    return {"new": new, "returning": returning, "not_known": unknown,
            "patients": len(patient_ids)}


def _no_shows(db, day: date, department: Optional[str]) -> dict:
    """Appointments of the day nobody checked in: recorded by the desk, or
    computed on read once their clinic session ended (the Appointments day
    view's rule, appointments_routes._effective_status). Available once the
    appointments table has the slot shape (migration 68)."""
    empty = {"available": False, "recorded": 0, "computed": 0, "total": 0}
    if not _table_exists(db, "appointments"):
        return empty
    cols = _columns(db, "appointments")
    if "no_show_at" not in cols:
        return empty
    from services.hospital_time import hospital_now
    from routes.appointments_routes import _effective_status

    sql = "SELECT * FROM appointments WHERE scheduled_date = ? AND status IN ('booked', 'no_show')"
    params: list[Any] = [day.isoformat()]
    rows = db.execute(sql, params).fetchall()
    if department is not None:
        clinic = department.strip().casefold()
        rows = [r for r in rows
                if ((r["department"] if "department" in cols else None) or "").strip().casefold()
                == clinic]
    now = hospital_now()
    cache: dict = {}
    recorded = computed = 0
    for row in rows:
        if row["status"] == "no_show":
            recorded += 1
            continue
        status, was_computed = _effective_status(db, row, now, cache)
        if status == "no_show" and was_computed:
            computed += 1
    return {"available": True, "recorded": recorded, "computed": computed,
            "total": recorded + computed}


def daily_summary(day: Optional[str] = None, *, department: Optional[str] = None) -> dict:
    """The day summary for `day` (hospital-local, today when None) and the
    caller's clinic. `department` is permissions.department_scope(user): None
    reads every clinic, never a value the request named."""
    when = resolve_day(day)
    key = when.isoformat()
    with db_session() as db:
        visits = _visits_on(db, key, department)
        patient_ids = sorted({str(v["patient_id"]) for v in visits if v.get("patient_id")})
        registrations = _registration_split(db, when, patient_ids)
        no_shows = _no_shows(db, when, department)

    by_type = {code: 0 for code, _ in VISIT_TYPES}
    for v in visits:
        code = v.get("visit_type") or "new"
        by_type[code] = by_type.get(code, 0) + 1

    clinics: list[dict] = []
    for clinic in sorted({v["department"] for v in visits}, key=lambda d: (d or "").casefold()):
        clinic_visits = [v for v in visits if v["department"] == clinic]
        doctors: list[dict] = []
        keys = sorted({(v.get("doctor_id") or "", v.get("doctor_name") or "") for v in clinic_visits},
                      key=lambda k: (k[0] == "", k[1].casefold(), k[0]))
        for doctor_id, doctor_name in keys:
            mine = [v for v in clinic_visits
                    if (v.get("doctor_id") or "") == doctor_id
                    and (v.get("doctor_name") or "") == doctor_name]
            doctors.append({
                "doctor_id": doctor_id or None,
                "doctor_name": doctor_name or ("Unassigned" if not doctor_id
                                               else "Name not recorded"),
                **_outcome_counts(mine),
                "wait": _wait_block(mine),
            })
        clinics.append({
            "department": clinic,
            **_outcome_counts(clinic_visits),
            "wait": _wait_block(clinic_visits),
            "doctors": doctors,
        })

    from services.opd_queue_service import CANCEL_REASON_CODES
    cancelled = [v for v in visits if v["status"] == "cancelled"]
    cancellations = [
        {"code": code, "label": label,
         "count": sum(1 for v in cancelled if v.get("cancel_reason_code") == code)}
        for code, label in CANCEL_REASON_CODES
    ]
    known_codes = {code for code, _ in CANCEL_REASON_CODES}
    cancellations.append({
        "code": "not_coded", "label": "No reason code",
        "count": sum(1 for v in cancelled if v.get("cancel_reason_code") not in known_codes),
    })

    totals = _outcome_counts(visits)
    return {
        "date": key,
        "department": department,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "registrations": registrations,
        "visits": {
            "total": totals["total"],
            "by_status": {s: totals[s] for s in VISIT_STATUSES},
            "by_type": [{"code": code, "label": label, "count": by_type.get(code, 0)}
                        for code, label in VISIT_TYPES],
        },
        "waits": {**_wait_block(visits), "still_waiting": totals["waiting"]},
        "left_without_being_seen": totals["left_without_being_seen"],
        "cancellations": cancellations,
        "no_shows": no_shows,
        "conversions": totals["admitted"],
        "clinics": clinics,
    }
