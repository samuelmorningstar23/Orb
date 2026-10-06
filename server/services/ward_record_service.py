"""Orb Ward Record: the ward's observation interval, and the monthly rounds file.

WHAT THIS IS FOR. The founding hospital's quality coordinator is promised "a
file each month showing which observation rounds were done, late or left
blank" (docs/fundraising/SALES_PLAN_2026-10.md section 3, step 2). The ward
clerk copies each round from the paper observation sheet with back-timed entry,
so every time this module reads is the time written on the sheet. The file is
only as accurate as the paper.

THE RULES, FROM THE SPEC, IN ONE PLACE:

* The due time comes ONLY from the ward's fixed interval, set in writing by the
  nursing superintendent. Never from anything Orb works out about the patient.
  A ward with no setting has no file: `WardRecordError('no_interval')`, which
  the route answers 409. An interval is never guessed.
* The first round is due one interval after the patient arrived on the ward
  (the admission time, or the time of the move onto this ward, or the time
  they came back from leave). Each next round is due one interval after the
  previous DUE time, not after the previous set.
* A round is DONE when a set's sheet time falls in
  [due minus half the grace, due plus grace].
* It is LATE when the first set after the due time comes after due plus grace
  but before the next due time. A set that also falls in the NEXT round's on
  time window goes to the next round unless another set is there for it
  (_kept_for_next_round).
* It is MISSED when no set arrives before the next due time.
* One set satisfies at most one due time.
* Only sets a person copied or charted count: nurse_manual, clerk_transcribed,
  the other labels services/vitals_source/base.py classifies as MANUAL, and
  NULL legacy rows. Never a monitor feed, an ingestion feed, the simulator, or
  values software extracted from a note. A voided set ("entered in error")
  never counts.
* A reading is BLANK when a counted set has nothing for respiratory rate,
  oxygen saturation, systolic blood pressure, heart rate, temperature,
  consciousness or air/oxygen, and that reading is not on the set's illegible
  list. ILLEGIBLE is counted separately.
* The clerk's entry lag (entered_at minus the sheet time) is reported per shift
  as a median and a maximum on its own line and NEVER counts as late.

THE FILE CARRIES NO SCORE, NO BAND, NO RESPONSE TEXT AND NO RISK WORD. Nothing
here imports or calls a scoring function, and the documents are checked for
those words by tests/test_ward_record_rounds.py.

THE VITALS COLUMNS entered_at, illegible and voided_at arrive with migration 77
(the ward record entry work). Every query here checks PRAGMA table_info(vitals)
at run time and uses them when present, so this module works on a database
from before that migration too.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import statistics
import uuid
from datetime import date, datetime, time as dt_time, timedelta, timezone
from typing import Any, Iterable, Optional

from database import db_session
from services.clinical_time import parse_to_utc, utc_iso_now

logger = logging.getLogger("orb.ward_record")

#: The interval and grace limits. The table's CHECKs say the same.
MIN_INTERVAL_MINUTES = 30
MAX_INTERVAL_MINUTES = 1440
MIN_GRACE_MINUTES = 0
MAX_GRACE_MINUTES = 240
DEFAULT_GRACE_MINUTES = 30

#: The longest period one file may cover. A quarter, so a quarterly review can
#: still be one file; anything longer is almost always a typed year.
MAX_PERIOD_DAYS = 92

NO_INTERVAL_MESSAGE = "Set the ward's observation interval first"

#: The closing sentence every document ends with.
CLOSING_LINE = "No score, band or response text appears in this file."


class WardRecordError(Exception):
    """A refusal with a machine-readable code the route maps to HTTP."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


# ═════════════════════════════════════════════════════════════════════════════
#  Wards and settings
# ═════════════════════════════════════════════════════════════════════════════

def _ward_key(value: Any) -> str:
    """Same rule as nabh_service._ward_key: case and outer spaces ignored."""
    return str(value or "").strip().upper()


def hospital_wards(db: Any = None) -> list[str]:
    """Every ward the hospital has, from the one source the NABH panel uses."""
    from services.nabh_service import hospital_wards as _nabh_wards

    return _nabh_wards(db)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _setting_out(row: Any) -> Optional[dict]:
    if row is None:
        return None
    out = dict(row)
    return {
        "ward": out["ward"],
        "interval_minutes": int(out["interval_minutes"]),
        "grace_minutes": int(out["grace_minutes"]),
        "set_by": out["set_by"],
        "set_at": out["set_at"],
        "note": out.get("note"),
    }


def get_setting(ward: str, *, db: Any = None) -> Optional[dict]:
    """The ward's interval and grace, or None when nobody has set them."""
    def _work(conn) -> Optional[dict]:
        return _setting_out(conn.execute(
            "SELECT * FROM ward_observation_settings WHERE UPPER(TRIM(ward)) = ? "
            "ORDER BY set_at DESC LIMIT 1",
            (_ward_key(ward),),
        ).fetchone())

    if db is not None:
        return _work(db)
    with db_session() as conn:
        return _work(conn)


def list_settings(*, db: Any = None) -> dict:
    """Every ward, each with its setting or None. A ward that has a setting
    but no longer appears in the ward list (renamed, closed) is still shown,
    so a setting is never invisible."""
    def _work(conn) -> dict:
        rows = [_setting_out(r) for r in conn.execute(
            "SELECT * FROM ward_observation_settings").fetchall()]
        by_key = {_ward_key(r["ward"]): r for r in rows}
        out: list[dict] = []
        seen: set[str] = set()
        for ward in hospital_wards(conn):
            key = _ward_key(ward)
            seen.add(key)
            out.append({"ward": ward, "setting": by_key.get(key)})
        for key, row in by_key.items():
            if key not in seen:
                out.append({"ward": row["ward"], "setting": row})
        out.sort(key=lambda w: _ward_key(w["ward"]))
        return {
            "wards": out,
            "limits": {
                "interval_minutes": [MIN_INTERVAL_MINUTES, MAX_INTERVAL_MINUTES],
                "grace_minutes": [MIN_GRACE_MINUTES, MAX_GRACE_MINUTES],
                "default_grace_minutes": DEFAULT_GRACE_MINUTES,
            },
        }

    if db is not None:
        return _work(db)
    with db_session() as conn:
        return _work(conn)


def _int_in(value: Any, low: int, high: int, what: str) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise WardRecordError(f"The {what} must be a whole number of minutes.",
                              code="bad_value") from None
    if isinstance(value, float) and value != number:
        raise WardRecordError(f"The {what} must be a whole number of minutes.",
                              code="bad_value")
    if not low <= number <= high:
        raise WardRecordError(
            f"The {what} must be between {low} and {high} minutes.", code="bad_value")
    return number


def set_setting(ward: str, *, interval_minutes: Any, grace_minutes: Any = None,
                set_by: Any, note: Any = None, db: Any = None) -> dict:
    """Record the ward's interval and grace as the nursing superintendent set
    them in writing. Returns {"old": setting or None, "new": setting}.

    `set_by` is the person who set it in writing (the name on the order), not
    the account keying it: the route audits the account separately. The ward
    must be one the hospital has, spelled as the hospital spells it."""
    who = str(set_by or "").strip()
    if not who:
        raise WardRecordError(
            "Say who set this in writing (the nursing superintendent's name).",
            code="missing_field")
    if len(who) > 200:
        raise WardRecordError("The name is too long.", code="bad_value")
    clean_note = (str(note).strip() or None) if note is not None else None
    if clean_note and len(clean_note) > 1000:
        raise WardRecordError("The note is too long.", code="bad_value")
    interval = _int_in(interval_minutes, MIN_INTERVAL_MINUTES, MAX_INTERVAL_MINUTES,
                       "interval")
    grace = _int_in(DEFAULT_GRACE_MINUTES if grace_minutes is None else grace_minutes,
                    MIN_GRACE_MINUTES, MAX_GRACE_MINUTES, "grace window")

    def _work(conn) -> dict:
        key = _ward_key(ward)
        known = {_ward_key(w): w for w in hospital_wards(conn)}
        old = get_setting(ward, db=conn)
        if key not in known and old is None:
            raise WardRecordError(f"No ward called {str(ward).strip()!r}.",
                                  code="unknown_ward")
        spelling = known.get(key) or (old or {}).get("ward") or str(ward).strip()
        conn.execute("DELETE FROM ward_observation_settings WHERE UPPER(TRIM(ward)) = ?",
                     (key,))
        conn.execute(
            "INSERT INTO ward_observation_settings (ward, interval_minutes, "
            "grace_minutes, set_by, set_at, note) VALUES (?, ?, ?, ?, ?, ?)",
            (spelling, interval, grace, who, utc_iso_now(), clean_note),
        )
        return {"old": old, "new": get_setting(spelling, db=conn)}

    if db is not None:
        return _work(db)
    with db_session(immediate=True) as conn:
        return _work(conn)


# ═════════════════════════════════════════════════════════════════════════════
#  Period and shifts
# ═════════════════════════════════════════════════════════════════════════════

def _hospital_tz():
    from services.hospital_time import hospital_timezone

    return hospital_timezone()


def resolve_period(start: Any, end: Any) -> dict:
    """Two calendar days on the hospital's clock (end inclusive) as the UTC
    window [start 00:00, the day after end 00:00)."""
    try:
        first = date.fromisoformat(str(start or "").strip()[:10])
        last = date.fromisoformat(str(end or "").strip()[:10])
    except ValueError:
        raise WardRecordError("Give the start and end as dates, YYYY-MM-DD.",
                              code="bad_period") from None
    if last < first:
        raise WardRecordError("The end date is before the start date.", code="bad_period")
    if (last - first).days + 1 > MAX_PERIOD_DAYS:
        raise WardRecordError(
            f"One file covers at most {MAX_PERIOD_DAYS} days.", code="bad_period")
    tz = _hospital_tz()
    lo = datetime.combine(first, dt_time(0, 0), tzinfo=tz).astimezone(timezone.utc)
    hi = datetime.combine(last + timedelta(days=1), dt_time(0, 0),
                          tzinfo=tz).astimezone(timezone.utc)
    return {"start_date": first.isoformat(), "end_date": last.isoformat(),
            "start": lo, "end": hi}


#: Used only when the product's own shift setting cannot be read.
_FALLBACK_SHIFTS = [(dt_time(8, 0), 12.0), (dt_time(20, 0), 12.0)]


def ward_shifts() -> tuple[list[tuple[dt_time, float]], str]:
    """The ward's shifts as (start, length in hours), and where they came from.

    The product already defines ward shifts: ORB_SCORECARD_SHIFTS, read by
    shadow_scorecard_service._shifts (default 07:00 to 15:00, 15:00 to 23:00,
    23:00 to 07:00, hospital local time). The same reader is used here so the
    two can never disagree. Only if it cannot be read do the 08:00 and 20:00
    twelve hour shifts apply, and the file says which was used."""
    try:
        from services.shadow_scorecard_service import _shifts

        shifts = _shifts()
        if shifts:
            return shifts, "ORB_SCORECARD_SHIFTS"
    except Exception:  # noqa: BLE001 - fall back, and say so in the file
        logger.warning("ward shifts unreadable; using 08:00 and 20:00", exc_info=True)
    return list(_FALLBACK_SHIFTS), "fallback"


def _shift_label(start: dt_time, hours: float) -> str:
    end_minutes = int(round(start.hour * 60 + start.minute + hours * 60)) % 1440
    return f"{start.strftime('%H:%M')} to {end_minutes // 60:02d}:{end_minutes % 60:02d}"


def _shift_of(instant: datetime, shifts: list[tuple[dt_time, float]], tz) -> tuple[str, str]:
    """(shift day, shift label) for an instant: the hospital-local day the
    shift STARTED on, so a night shift's 02:00 round sits under the evening it
    began."""
    local = instant.astimezone(tz)
    for day in (local.date(), local.date() - timedelta(days=1)):
        for start, hours in shifts:
            w_start = datetime.combine(day, start, tzinfo=tz)
            if w_start <= local < w_start + timedelta(hours=hours):
                return day.isoformat(), _shift_label(start, hours)
    return local.date().isoformat(), "outside the ward's shifts"


# ═════════════════════════════════════════════════════════════════════════════
#  Stays on the ward
# ═════════════════════════════════════════════════════════════════════════════

def _dt(value: Any) -> Optional[datetime]:
    if value in (None, ""):
        return None
    try:
        return parse_to_utc(value)
    except (TypeError, ValueError):
        return None


def _table_columns(conn, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f'PRAGMA table_info("{table}")').fetchall()}


#: Movement types that put the patient somewhere (when they carry a ward).
_PLACING_MOVES = frozenset({"admission", "bed_assigned", "transfer", "bed_change",
                            "bed_swap", "location_linked", "correction"})
_LEAVE_MOVES = frozenset({"leave_start", "absconded"})
_RETURN_MOVES = frozenset({"leave_end", "absconded_return"})


def _ward_names(ward_rows: dict, text: Any, ward_id: Any) -> set[str]:
    keys = set()
    if _ward_key(text):
        keys.add(_ward_key(text))
    if ward_id and ward_id in ward_rows:
        keys |= ward_rows[ward_id]
    return keys


def _ward_lookup(conn) -> dict:
    if not _table_columns(conn, "wards"):
        return {}
    out: dict[str, set[str]] = {}
    for row in conn.execute("SELECT id, code, name FROM wards").fetchall():
        out[row["id"]] = {k for k in (_ward_key(row["code"]), _ward_key(row["name"])) if k}
    return out


def _stay_segments(conn, ward: str, period_start: datetime, period_end: datetime,
                   now: datetime) -> tuple[list[dict], dict]:
    """Every spell a patient spent on `ward`, from arrival to leaving.

    A stay is an admission (admissions table) or, for a patient written before
    admissions existed, the patient row. Its moves come from patient_movements:
    a move onto the ward opens a spell, a move off it, a leave, an absconding
    or the end of the stay closes it, and a return from leave opens a new one.
    The end of a stay is the recorded discharge time, else the discharge move,
    else, for a stay still open, now; else the first discharge summary after it
    began; else it is UNKNOWN and the spell is closed at its last counted
    set, which the file reports."""
    key = _ward_key(ward)
    ward_rows = _ward_lookup(conn)
    adm_cols = _table_columns(conn, "admissions")
    has_moves = bool(_table_columns(conn, "patient_movements"))
    notes = {"stays_with_unknown_end": 0}

    summaries: dict[str, list[datetime]] = {}
    if _table_columns(conn, "discharge_summaries"):
        for row in conn.execute(
                "SELECT patient_id, discharged_at FROM discharge_summaries").fetchall():
            when = _dt(row["discharged_at"])
            if when:
                summaries.setdefault(row["patient_id"], []).append(when)
    for times in summaries.values():
        times.sort()

    patients = {r["id"]: dict(r) for r in conn.execute(
        "SELECT id, mrn, ward, bed, admission_date, status FROM patients").fetchall()}

    def _cols(*names: str) -> str:
        return ", ".join(n if n in adm_cols else f"NULL AS {n}" for n in names)

    stays: list[dict] = []
    with_admission: set[str] = set()
    if adm_cols:
        for row in conn.execute(
                "SELECT id, patient_id, status, created_at, "
                + _cols("ward", "bed", "ward_id", "admitted_at", "discharged_at")
                + " FROM admissions").fetchall():
            row = dict(row)
            with_admission.add(row["patient_id"])
            start = _dt(row.get("admitted_at")) or _dt(row.get("created_at"))
            if start is None:
                continue
            stays.append({
                "patient_id": row["patient_id"], "admission_id": row["id"],
                "start": start, "end": _dt(row.get("discharged_at")),
                "open": (row.get("status") or "active") == "active",
                "ward": row.get("ward"), "ward_id": row.get("ward_id"),
                "bed": row.get("bed"),
            })
    for pid, p in patients.items():
        if pid in with_admission or (p.get("status") or "admitted") == "registered":
            continue
        start = _dt(p.get("admission_date"))
        if start is None:
            continue
        stays.append({
            "patient_id": pid, "admission_id": None, "start": start, "end": None,
            "open": (p.get("status") or "admitted") == "admitted",
            "ward": p.get("ward"), "ward_id": None, "bed": p.get("bed"),
        })

    segments: list[dict] = []
    for stay in stays:
        # Only stays that can touch the period. A stay that ended before it
        # began, or began after it ended, has no round in it.
        if stay["start"] >= period_end or (stay["end"] is not None and stay["end"] <= period_start):
            continue
        patient = patients.get(stay["patient_id"]) or {}
        moves: list[dict] = []
        if has_moves:
            if stay["admission_id"]:
                rows = conn.execute(
                    "SELECT rowid AS _rid, * FROM patient_movements "
                    "WHERE admission_id = ? OR (admission_id IS NULL AND patient_id = ?)",
                    (stay["admission_id"], stay["patient_id"])).fetchall()
            else:
                rows = conn.execute(
                    "SELECT rowid AS _rid, * FROM patient_movements WHERE patient_id = ?",
                    (stay["patient_id"],)).fetchall()
            moves = [dict(r) for r in rows]
            corrected = {m.get("corrects_movement_id") for m in moves if m.get("corrects_movement_id")}
            moves = [m for m in moves if m.get("id") not in corrected]
            for m in moves:
                m["_at"] = _dt(m.get("moved_at"))
            moves = [m for m in moves if m["_at"] is not None and m["_at"] >= stay["start"]]
            moves.sort(key=lambda m: (m["_at"], m.get("_rid") or 0))

        end = stay["end"]
        if end is None:
            leaving = [m["_at"] for m in moves if m.get("movement_type") == "discharge"]
            if leaving:
                end = leaving[0]
        end_known = True
        if end is None:
            if stay["open"]:
                end = now
            else:
                later = [t for t in summaries.get(stay["patient_id"], []) if t >= stay["start"]]
                if later:
                    end = later[0]
                else:
                    end_known = False
        if end is not None:
            moves = [m for m in moves if m["_at"] < end]

        placing = [m for m in moves if m.get("movement_type") in _PLACING_MOVES
                   and _ward_names(ward_rows, m.get("to_ward"), m.get("to_ward_id"))]
        first_from = next((m for m in placing
                           if _ward_names(ward_rows, m.get("from_ward"), m.get("from_ward_id"))),
                          None)
        if first_from is not None and (not placing or placing[0] is first_from):
            here = _ward_names(ward_rows, first_from.get("from_ward"), first_from.get("from_ward_id"))
            bed = first_from.get("from_bed")
        else:
            here = _ward_names(ward_rows, stay["ward"], stay["ward_id"]) or \
                _ward_names(ward_rows, patient.get("ward"), None)
            # admissions.ward follows the patient, so after a move it names the
            # LATEST ward; an admission move at the start corrects it below.
            bed = stay["bed"] or patient.get("bed")

        on_ward = key in here
        away = False
        spell_start = stay["start"] if on_ward else None
        spell_bed = bed if on_ward else None

        def _close(at: datetime) -> None:
            if spell_start is not None:
                segments.append({"patient_id": stay["patient_id"],
                                 "mrn": patient.get("mrn"), "bed": spell_bed,
                                 "start": spell_start, "end": at,
                                 "end_known": True, "open": False})

        for m in moves:
            kind = m.get("movement_type")
            at = m["_at"]
            if kind in _LEAVE_MOVES:
                if on_ward and not away:
                    _close(at)
                    spell_start = None
                away = True
            elif kind in _RETURN_MOVES:
                if away and on_ward:
                    spell_start = at
                away = False
            elif kind in _PLACING_MOVES:
                target = _ward_names(ward_rows, m.get("to_ward"), m.get("to_ward_id"))
                if not target:
                    continue
                now_here = key in target
                if now_here and not on_ward:
                    on_ward = True
                    spell_start = None if away else at
                    spell_bed = m.get("to_bed")
                elif not now_here and on_ward:
                    if not away:
                        _close(at)
                    spell_start = None
                    on_ward = False
                elif now_here and on_ward and m.get("to_bed"):
                    spell_bed = m.get("to_bed")
        if on_ward and not away and spell_start is not None:
            segments.append({"patient_id": stay["patient_id"], "mrn": patient.get("mrn"),
                             "bed": spell_bed, "start": spell_start, "end": end,
                             "end_known": end_known, "open": stay["open"] and end is now})

    out = [s for s in segments if s["end"] is None or s["end"] > s["start"]]
    for s in out:
        if not s["end_known"]:
            notes["stays_with_unknown_end"] += 1
    return out, notes


# ═════════════════════════════════════════════════════════════════════════════
#  Observation sets
# ═════════════════════════════════════════════════════════════════════════════

#: The readings the Ward Record copies from the paper sheet and the file checks
#: for blanks, in the order the file lists them. Diastolic pressure is copied
#: but is not one of the readings a blank is counted for (spec, step 2).
READINGS: tuple[tuple[str, str], ...] = (
    ("respiratory_rate", "Respiratory rate"),
    ("spo2", "Oxygen saturation"),
    ("systolic_bp", "Systolic blood pressure"),
    ("heart_rate", "Heart rate"),
    ("temperature", "Temperature"),
    ("consciousness", "Consciousness"),
    ("on_oxygen", "Air or oxygen"),
)
_READING_KEYS = tuple(k for k, _ in READINGS)


def _counted_source(label: Any) -> bool:
    """True for a set a person charted or copied from the sheet.

    NULL (a legacy row) counts. Otherwise the label must be one the product
    classifies as MANUAL (services/vitals_source/base.py: nurse_manual,
    manual_entry, clinician) or the clerk's clerk_transcribed. A monitor feed,
    an ingestion feed, the simulator and values extracted from a note never
    count."""
    if label is None or str(label).strip() == "":
        return True
    text = str(label).strip().lower()
    if text == "clerk_transcribed":
        return True
    from services.vitals_source.base import SourceKind, classify_source_label

    return classify_source_label(text) is SourceKind.MANUAL


def _illegible_set(raw: Any) -> set[str]:
    """vitals.illegible: comma separated column names (migration 77). A JSON
    list is read too, so a differently keyed row is not silently dropped."""
    if raw in (None, ""):
        return set()
    text = str(raw).strip()
    names: Iterable[Any]
    if text.startswith("["):
        try:
            names = json.loads(text)
        except ValueError:
            names = text.strip("[]").split(",")
    else:
        names = text.split(",")
    return {str(n).strip().strip('"').strip("'").lower() for n in names if str(n).strip()}


def _load_sets(conn, patient_id: str, lo: datetime, hi: datetime, cols: set[str]) -> list[dict]:
    """Counted sets for one patient with a sheet time in [lo, hi), oldest first."""
    wanted = ["id", "timestamp"] + [c for c in _READING_KEYS if c in cols]
    for optional in ("source", "entered_at", "illegible", "voided_at"):
        if optional in cols:
            wanted.append(optional)
    sql = (f"SELECT {', '.join(wanted)} FROM vitals WHERE patient_id = ? "
           "AND julianday(timestamp) >= julianday(?) - 1 "
           "AND julianday(timestamp) < julianday(?) + 1")
    params: list[Any] = [patient_id, lo.isoformat(), hi.isoformat()]
    if "voided_at" in cols:
        sql += " AND voided_at IS NULL"
    out: list[dict] = []
    for row in conn.execute(sql, params).fetchall():
        row = dict(row)
        at = _dt(row.get("timestamp"))
        if at is None or not lo <= at < hi:
            continue
        if not _counted_source(row.get("source")):
            continue
        row["_at"] = at
        row["_entered"] = _dt(row.get("entered_at"))
        row["_illegible"] = _illegible_set(row.get("illegible"))
        out.append(row)
    out.sort(key=lambda r: (r["_at"], r["id"]))
    return out


# ═════════════════════════════════════════════════════════════════════════════
#  The rounds
# ═════════════════════════════════════════════════════════════════════════════

def _kept_for_next_round(sets: list[dict], i: int, next_due: datetime,
                         half: timedelta, grace: timedelta, end: datetime) -> bool:
    """True when sets[i] is the only set in the NEXT round's on time window.

    The late window of one round (up to the next due time) and the on time
    window of the next (from half the grace before it) overlap. A set in that
    overlap is a late round only when another set is there for the next round;
    otherwise it is the next round done on time and this round is missed. Each
    way one round is short; this way the file does not call an on time round
    missed."""
    if next_due >= end:
        return False  # the patient left before the next round fell due
    opens = next_due - half
    if sets[i]["_at"] < opens:
        return False
    closes = next_due + grace
    return not any(opens <= s["_at"] <= closes and s["_at"] < end
                   for s in sets[i + 1:])


def _rounds_for_segment(seg: dict, sets: list[dict], *, interval: timedelta,
                        grace: timedelta, period_start: datetime,
                        period_end: datetime, now: datetime) -> list[dict]:
    """Every due time of one spell inside the period, each with its outcome.

    The chain starts at the spell's start and runs on the DUE times, so a due
    time before the period still uses up the set that satisfied it. Outcomes:
    done, late, missed, and 'open' for a round still inside its window now.
    A due time whose grace window the patient left the ward inside, with no
    set, is not a round (they were not on the ward to be observed late)."""
    half = grace / 2
    end = seg["end"]
    rounds: list[dict] = []
    i = 0
    k = 1
    while True:
        due = seg["start"] + interval * k
        k += 1
        if due >= end or due >= period_end:
            break
        cutoff = min(due + interval, end)
        while i < len(sets) and sets[i]["_at"] < due - half:
            i += 1  # an extra set between rounds: satisfies nothing
        outcome: Optional[str] = None
        used: Optional[dict] = None
        if i < len(sets) and sets[i]["_at"] <= due + grace:
            outcome, used = "done", sets[i]
        elif i < len(sets) and sets[i]["_at"] < cutoff and \
                not _kept_for_next_round(sets, i, due + interval, half, grace, end):
            outcome, used = "late", sets[i]
        elif not seg.get("open") and end <= due + grace:
            outcome = None  # left the ward inside the grace window
        elif (due + interval if seg.get("open") else cutoff) > now:
            outcome = "open"
        else:
            outcome = "missed"
        if used is not None:
            i += 1
        if outcome is None or due < period_start:
            continue
        rounds.append({
            "due": due, "outcome": outcome,
            "set_at": used["_at"] if used is not None else None,
            "minutes_after_due": (round((used["_at"] - due).total_seconds() / 60)
                                  if used is not None else None),
        })
    return rounds


def _empty_acvpu_oxygen_filled_in() -> bool:
    """True when the observation entry path stores an EMPTY consciousness box
    as alert, or an empty oxygen box as air. Then a blank in those two can
    never reach the database, and the file says so instead of printing a 0
    that reads as "never left blank". Asked of the entry code itself (the
    request body's defaults, then the service's cleaning), so the sentence
    goes away by itself the day the entry path stores an empty box as empty.
    When the entry code cannot be asked, the file claims nothing."""
    try:
        from routes.nurse_routes import ObservationRequest
        from services.nurse_shift_service import _clean_obs

        body = ObservationRequest(heart_rate=80).model_dump()
        cleaned = _clean_obs(body)
    except Exception:  # noqa: BLE001 - the file then makes no claim either way
        logger.debug("could not ask the entry path about empty boxes", exc_info=True)
        return False
    return cleaned.get("consciousness") is not None or cleaned.get("on_oxygen") is not None


def _median(values: list[float]) -> Optional[float]:
    return round(statistics.median(values), 1) if values else None


def due_now(ward: str, *, now: Optional[datetime] = None, db: Any = None) -> Optional[dict]:
    """When each patient on `ward` is next due a round, by the rounds file's rule.

    The nurse shift board's due time while scoring is switched off (the Orb
    Ward Record): the ward's fixed interval only, chained from the patient's
    arrival on the ward exactly as compute_rounds chains it, never anything
    worked out about the patient. None when the ward has no setting, so the
    caller says "Set the ward's observation interval" and shows no due time.

    Returns {"ward", "interval_minutes", "grace_minutes", "patients": {id:
    {"due_at", "late"}}} for every patient on the ward now. `due_at` is the
    round still open (no set yet, its window not closed), else the next due
    time after the last round; `late` is True when that open round is past its
    due time plus the grace window, the same "late" the file counts."""
    moment = now or _now()

    def _work(conn) -> Optional[dict]:
        setting = get_setting(ward, db=conn)
        if setting is None:
            return None
        interval = timedelta(minutes=setting["interval_minutes"])
        grace = timedelta(minutes=setting["grace_minutes"])
        cols = _table_columns(conn, "vitals")
        segments, _notes = _stay_segments(conn, setting["ward"], moment - interval,
                                          moment, moment)
        patients: dict[str, dict] = {}
        for seg in segments:
            if not seg.get("open") or seg["start"] > moment:
                continue
            sets = _load_sets(conn, seg["patient_id"], seg["start"],
                              moment + timedelta(seconds=1), cols)
            rounds = _rounds_for_segment(seg, sets, interval=interval, grace=grace,
                                         period_start=seg["start"],
                                         period_end=moment + interval, now=moment)
            if not rounds:
                due, late = seg["start"] + interval, False
            elif rounds[-1]["outcome"] == "open":
                due = rounds[-1]["due"]
                late = moment > due + grace
            else:
                due, late = rounds[-1]["due"] + interval, False
            patients[seg["patient_id"]] = {"due_at": due, "late": late}
        return {"ward": setting["ward"], "interval_minutes": setting["interval_minutes"],
                "grace_minutes": setting["grace_minutes"], "patients": patients}

    if db is not None:
        return _work(db)
    with db_session() as conn:
        return _work(conn)


def compute_rounds(ward: str, start: Any, end: Any, *, now: Optional[datetime] = None,
                   include_patient_identifiers: bool = False, db: Any = None) -> dict:
    """The rounds file's content for one ward and period, as a dict.

    Refuses with code 'no_interval' when the ward has no setting. Reads only."""
    period = resolve_period(start, end)
    moment = now or _now()

    def _work(conn) -> dict:
        setting = get_setting(ward, db=conn)
        if setting is None:
            raise WardRecordError(NO_INTERVAL_MESSAGE, code="no_interval")
        interval = timedelta(minutes=setting["interval_minutes"])
        grace = timedelta(minutes=setting["grace_minutes"])
        tz = _hospital_tz()
        shifts, shift_source = ward_shifts()
        cols = _table_columns(conn, "vitals")
        segments, notes = _stay_segments(conn, setting["ward"], period["start"],
                                         period["end"], moment)

        labels: dict[str, str] = {}
        rows: dict[tuple, dict] = {}
        shift_totals: dict[tuple, dict] = {}
        blank = {k: 0 for k in _READING_KEYS}
        illegible = {k: 0 for k in _READING_KEYS}
        lag_by_shift: dict[tuple, list[float]] = {}
        sets_counted = 0
        sets_without_entry_time = 0
        unknown_ends = 0

        def _blank_counts() -> dict:
            return {"due": 0, "done": 0, "late": 0, "missed": 0, "open": 0,
                    "sets": 0, "blank_readings": 0, "illegible_readings": 0}

        segments.sort(key=lambda s: (s["start"], str(s["patient_id"])))
        for seg in segments:
            hi = seg["end"] if seg["end"] is not None else period["end"] + interval
            sets = _load_sets(conn, seg["patient_id"], seg["start"],
                              min(hi, period["end"] + interval), cols)
            unknown_end = seg["end"] is None
            if unknown_end:
                # End of stay never recorded: close the spell just after its
                # last set, so no round is invented after the patient may have
                # gone. The file says how many stays this happened to.
                seg["end"] = (sets[-1]["_at"] + timedelta(seconds=1)) if sets else seg["start"]
            if seg["end"] <= period["start"] or seg["end"] <= seg["start"]:
                continue
            if unknown_end:
                unknown_ends += 1
            pid = seg["patient_id"]
            if pid not in labels:
                labels[pid] = (str(seg.get("mrn") or pid) if include_patient_identifiers
                               else f"Patient {len(labels) + 1}")
            bed = seg.get("bed") or ""

            for r in _rounds_for_segment(seg, sets, interval=interval, grace=grace,
                                         period_start=period["start"],
                                         period_end=period["end"], now=moment):
                day, label = _shift_of(r["due"], shifts, tz)
                key = (day, label, pid, bed)
                row = rows.setdefault(key, _blank_counts())
                tot = shift_totals.setdefault((day, label), _blank_counts())
                for target in (row, tot):
                    target["due"] += 1
                    target[r["outcome"]] += 1

            for s in sets:
                if not period["start"] <= s["_at"] < period["end"] or s["_at"] >= seg["end"]:
                    continue
                day, label = _shift_of(s["_at"], shifts, tz)
                key = (day, label, pid, bed)
                row = rows.setdefault(key, _blank_counts())
                tot = shift_totals.setdefault((day, label), _blank_counts())
                sets_counted += 1
                row["sets"] += 1
                tot["sets"] += 1
                for field in _READING_KEYS:
                    if field in s["_illegible"]:
                        illegible[field] += 1
                        row["illegible_readings"] += 1
                        tot["illegible_readings"] += 1
                    elif s.get(field) is None:
                        blank[field] += 1
                        row["blank_readings"] += 1
                        tot["blank_readings"] += 1
                if s["_entered"] is not None:
                    lag = (s["_entered"] - s["_at"]).total_seconds() / 60.0
                    lag_by_shift.setdefault((day, label), []).append(lag)
                else:
                    sets_without_entry_time += 1

        shift_order = sorted(shift_totals, key=lambda k: (k[0], k[1]))
        # Patients in the order they arrived, so Patient 2 comes before Patient 10.
        order = {pid: n for n, pid in enumerate(labels)}
        lag_rows = []
        for key in shift_order:
            lags = lag_by_shift.get(key, [])
            lag_rows.append({
                "shift_date": key[0], "shift": key[1], "sets_keyed": len(lags),
                "median_minutes": _median(lags),
                "max_minutes": round(max(lags), 1) if lags else None,
            })
        all_lags = [v for vs in lag_by_shift.values() for v in vs]
        overall = _blank_counts()
        for tot in shift_totals.values():
            for f in overall:
                overall[f] += tot[f]

        return {
            "ward": setting["ward"],
            "period": {"start_date": period["start_date"], "end_date": period["end_date"],
                       "start": period["start"].isoformat(),
                       "end": period["end"].isoformat()},
            "setting": setting,
            "setting_entered_during_period": bool(
                _dt(setting["set_at"]) and _dt(setting["set_at"]) > period["start"]),
            "shifts": {"source": shift_source,
                       "labels": [_shift_label(s, h) for s, h in shifts],
                       "timezone": str(tz)},
            "include_patient_identifiers": bool(include_patient_identifiers),
            "totals": overall,
            "shift_totals": [{"shift_date": k[0], "shift": k[1], **shift_totals[k]}
                             for k in shift_order],
            "rows": [{"shift_date": k[0], "shift": k[1], "patient": labels[k[2]],
                      "bed": k[3], **rows[k]}
                     for k in sorted(rows, key=lambda k: (k[0], k[1], order[k[2]], k[3]))],
            "readings": [{"reading": name, "field": field, "blank": blank[field],
                          "illegible": illegible[field]} for field, name in READINGS],
            "entry_lag": {
                "by_shift": lag_rows,
                "median_minutes": _median(all_lags),
                "max_minutes": round(max(all_lags), 1) if all_lags else None,
                "sets_keyed": len(all_lags),
                "sets_without_entry_time": sets_without_entry_time,
            },
            "patients": len(labels),
            "sets_counted": sets_counted,
            "stays_with_unknown_end": unknown_ends,
            "vitals_columns": {c: c in cols for c in ("entered_at", "illegible", "voided_at")},
            "empty_acvpu_oxygen_stored_as_alert_air": _empty_acvpu_oxygen_filled_in(),
        }

    if db is not None:
        return _work(db)
    with db_session() as conn:
        return _work(conn)


# ═════════════════════════════════════════════════════════════════════════════
#  The documents
# ═════════════════════════════════════════════════════════════════════════════

def _local_text(value: Any) -> str:
    when = _dt(value)
    if when is None:
        return str(value or "")
    return when.astimezone(_hospital_tz()).strftime("%d %b %Y %H:%M")


def _minutes(value: Optional[float]) -> str:
    if value is None:
        return "none keyed"
    return f"{value:g}"


def header_lines(data: dict, *, reference: str, generated_at: str,
                 generated_by: str) -> list[tuple[str, str]]:
    """The header, as (label, value) pairs, shared by both formats."""
    s = data["setting"]
    shifts = data["shifts"]
    shift_words = ", ".join(shifts["labels"])
    if shifts["source"] == "fallback":
        shift_words += " (the ward's shift setting could not be read; 08:00 and 20:00 used)"
    else:
        shift_words += " (the hospital's ward shifts)"
    lines = [
        ("Ward", data["ward"]),
        ("Period", f"{data['period']['start_date']} to {data['period']['end_date']}, "
                   "hospital local time"),
        ("Observation interval", f"every {s['interval_minutes']} minutes"),
        ("Grace window", f"{s['grace_minutes']} minutes"),
        ("Set in writing by", s["set_by"]),
        ("Setting entered on", _local_text(s["set_at"])),
    ]
    if s.get("note"):
        lines.append(("Setting note", s["note"]))
    if data.get("setting_entered_during_period"):
        lines.append(("Setting caution",
                      "This interval was entered after the period began; the whole "
                      "period is measured against it."))
    t = data["totals"]
    lines += [
        ("Shifts", shift_words),
        ("Rounds due", str(t["due"])),
        ("Done", str(t["done"])),
        ("Late", str(t["late"])),
        ("Missed", str(t["missed"])),
        ("Still open", str(t["open"])),
        ("Patients", str(data["patients"])),
        ("Observation sets counted", str(data["sets_counted"])),
        ("Reference", reference),
        ("Produced", f"{_local_text(generated_at)} by {generated_by}"),
        ("How rounds are counted",
         "The first round is due one interval after the patient arrived on the ward, "
         "each next one an interval after the previous due time. Done: a set timed "
         "from half the grace window before the due time to the grace window after "
         "it. Late: the first set after that, before the next due time, unless it is "
         "the only set on time for the next round. Missed: no set before the next "
         "due time. One set counts for one round. Times are the times written on "
         "the sheet."),
        ("Patients shown as",
         "hospital patient number and bed" if data["include_patient_identifiers"]
         else "a number within this file and the bed (patient numbers left out)"),
    ]
    if data.get("stays_with_unknown_end"):
        lines.append(("Stays with no recorded end",
                      f"{data['stays_with_unknown_end']}; counted up to their last set"))
    if data.get("empty_acvpu_oxygen_stored_as_alert_air"):
        lines.append(("Consciousness and air or oxygen",
                      "the entry screen stores an empty box as alert and on air, so "
                      "this file cannot count those two readings as blank"))
    if not data["vitals_columns"].get("illegible"):
        lines.append(("Illegible readings",
                      "this system does not record illegible readings yet"))
    return lines


_SHIFT_COLUMNS = ("due", "done", "late", "missed", "open")
_SHIFT_HEADINGS = ("Due", "Done", "Late", "Missed", "Still open")


def render_csv(data: dict, *, reference: str, generated_at: str, generated_by: str) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["Ward Record rounds file"])
    for label, value in header_lines(data, reference=reference,
                                     generated_at=generated_at, generated_by=generated_by):
        w.writerow([label, value])
    w.writerow([])
    w.writerow(["Totals by shift"])
    w.writerow(["Shift date", "Shift", *_SHIFT_HEADINGS, "Sets", "Blank readings",
                "Illegible readings"])
    for row in data["shift_totals"]:
        w.writerow([row["shift_date"], row["shift"], *[row[c] for c in _SHIFT_COLUMNS],
                    row["sets"], row["blank_readings"], row["illegible_readings"]])
    w.writerow([])
    w.writerow(["Rounds by patient and shift"])
    w.writerow(["Shift date", "Shift", "Patient", "Bed", *_SHIFT_HEADINGS, "Sets",
                "Blank readings", "Illegible readings"])
    for row in data["rows"]:
        w.writerow([row["shift_date"], row["shift"], row["patient"], row["bed"],
                    *[row[c] for c in _SHIFT_COLUMNS], row["sets"],
                    row["blank_readings"], row["illegible_readings"]])
    w.writerow([])
    w.writerow(["Blank and illegible readings"])
    w.writerow(["Reading", "Blank", "Illegible"])
    for row in data["readings"]:
        w.writerow([row["reading"], row["blank"], row["illegible"]])
    w.writerow([])
    lag = data["entry_lag"]
    w.writerow(["Clerk entry lag (minutes from the time on the sheet to the time keyed; "
                "never counted as late)"])
    w.writerow(["Overall", f"median {_minutes(lag['median_minutes'])}",
                f"maximum {_minutes(lag['max_minutes'])}",
                f"{lag['sets_keyed']} sets with an entry time",
                f"{lag['sets_without_entry_time']} without"])
    w.writerow(["Shift date", "Shift", "Sets keyed", "Median minutes", "Maximum minutes"])
    for row in lag["by_shift"]:
        w.writerow([row["shift_date"], row["shift"], row["sets_keyed"],
                    "" if row["median_minutes"] is None else row["median_minutes"],
                    "" if row["max_minutes"] is None else row["max_minutes"]])
    w.writerow([])
    w.writerow([CLOSING_LINE])
    return buf.getvalue()


def _md_cell(value: Any) -> str:
    return str(value if value is not None else "").replace("|", "/").replace("\n", " ")


def render_markdown(data: dict, *, reference: str, generated_at: str,
                    generated_by: str) -> str:
    out: list[str] = [f"# Ward Record rounds file: {_md_cell(data['ward'])}", ""]
    for label, value in header_lines(data, reference=reference,
                                     generated_at=generated_at, generated_by=generated_by):
        out.append(f"- **{label}:** {_md_cell(value)}")
    out += ["", "## Totals by shift", "",
            "| Shift date | Shift | " + " | ".join(_SHIFT_HEADINGS)
            + " | Sets | Blank readings | Illegible readings |",
            "|" + "---|" * (len(_SHIFT_HEADINGS) + 5)]
    for row in data["shift_totals"]:
        cells = [row["shift_date"], row["shift"], *[row[c] for c in _SHIFT_COLUMNS],
                 row["sets"], row["blank_readings"], row["illegible_readings"]]
        out.append("| " + " | ".join(_md_cell(c) for c in cells) + " |")
    if not data["shift_totals"]:
        out.append("")
        out.append("No rounds were due on this ward in this period.")
    out += ["", "## Rounds by patient and shift", "",
            "| Shift date | Shift | Patient | Bed | " + " | ".join(_SHIFT_HEADINGS)
            + " | Sets | Blank readings | Illegible readings |",
            "|" + "---|" * (len(_SHIFT_HEADINGS) + 7)]
    for row in data["rows"]:
        cells = [row["shift_date"], row["shift"], row["patient"], row["bed"],
                 *[row[c] for c in _SHIFT_COLUMNS], row["sets"],
                 row["blank_readings"], row["illegible_readings"]]
        out.append("| " + " | ".join(_md_cell(c) for c in cells) + " |")
    out += ["", "## Blank and illegible readings", "",
            "| Reading | Blank | Illegible |", "|---|---|---|"]
    for row in data["readings"]:
        out.append(f"| {row['reading']} | {row['blank']} | {row['illegible']} |")
    lag = data["entry_lag"]
    out += ["", "## Clerk entry lag", "",
            ("Minutes from the time written on the sheet to the time the set was keyed. "
             "Entry lag is never counted as late."), "",
            (f"Overall: median {_minutes(lag['median_minutes'])}, maximum "
             f"{_minutes(lag['max_minutes'])}, over {lag['sets_keyed']} sets with an entry "
             f"time ({lag['sets_without_entry_time']} without)."), "",
            "| Shift date | Shift | Sets keyed | Median minutes | Maximum minutes |",
            "|---|---|---|---|---|"]
    for row in lag["by_shift"]:
        out.append(f"| {row['shift_date']} | {row['shift']} | {row['sets_keyed']} | "
                   f"{_minutes(row['median_minutes'])} | {_minutes(row['max_minutes'])} |")
    out += ["", CLOSING_LINE, ""]
    return "\n".join(out)


def _canonical_json(data: dict) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), default=str)


FORMATS = ("json", "csv", "md")


def produce_file(ward: str, start: Any, end: Any, *, fmt: str, generated_by: str,
                 include_patient_identifiers: bool = False,
                 now: Optional[datetime] = None) -> dict:
    """Compute, render, hash and log one rounds file.

    Returns {"reference", "sha256", "format", "content" (str for csv and md,
    the dict for json), "data"}. The SHA-256 covers the exact bytes returned
    (for json: the canonical JSON of the data with its reference)."""
    if fmt not in FORMATS:
        raise WardRecordError(f"Unknown format {fmt!r}.", code="bad_value")
    data = compute_rounds(ward, start, end, now=now,
                          include_patient_identifiers=include_patient_identifiers)
    reference = f"WR-{(now or _now()).strftime('%Y%m%d')}-{uuid.uuid4().hex[:8].upper()}"
    generated_at = utc_iso_now()
    if fmt == "csv":
        content: Any = render_csv(data, reference=reference, generated_at=generated_at,
                                  generated_by=generated_by)
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    elif fmt == "md":
        content = render_markdown(data, reference=reference, generated_at=generated_at,
                                  generated_by=generated_by)
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    else:
        content = dict(data, reference=reference, generated_at=generated_at,
                       generated_by=generated_by, closing_line=CLOSING_LINE)
        digest = hashlib.sha256(_canonical_json(content).encode("utf-8")).hexdigest()
    with db_session() as conn:
        conn.execute(
            "INSERT INTO ward_record_exports (reference, ward, period_start, period_end, "
            "generated_by, generated_at, format, sha256) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (reference, data["ward"], data["period"]["start_date"],
             data["period"]["end_date"], generated_by, generated_at, fmt, digest),
        )
    return {"reference": reference, "sha256": digest, "format": fmt,
            "content": content, "data": data}
