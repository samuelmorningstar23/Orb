"""Orb — BIOMEDICAL ASSET & EQUIPMENT TRACKING.

The equipment register an accreditation assessor asks for, and the thing that
tells a ward its ventilator is overdue for calibration.

════════════════════════════════════════════════════════════════════════════════
 WHY THIS MODULE EXISTS, IN ONE PARAGRAPH
════════════════════════════════════════════════════════════════════════════════

Orb ingests numbers off devices. An infusion pump that has drifted 12% delivers
88 ml where the chart says 100 and Orb records 100. An uncalibrated SpO2 monitor
reads 94% where the truth is 88%, Vigil sees 94%, and no alert fires. The device
is not broken — it is working perfectly and it is wrong, which is the failure
mode no amount of software validation catches, because every layer above the
sensor is doing its job correctly on a bad input.

The only defence is a maintained record of when each measuring device was last
calibrated and when it is next due. That record is this module. It is a
paperwork module in the same way a drug-expiry date is a paperwork field.

════════════════════════════════════════════════════════════════════════════════
 THE ONE DEFINITION EVERYTHING HERE TURNS ON
════════════════════════════════════════════════════════════════════════════════

    next_due_date = last_done_date + interval_days
    A schedule is OVERDUE when  today >= next_due_date + grace_days.

Read that second line twice, because the `>=` is the whole module.

A calibration done on 1 Jan with a 90-day interval is next due on 1 April
(1 Jan + 90 days). The certificate covers 1 Jan through 31 March — ninety days,
counting the day the work was done. On 1 APRIL the cover has run out. Not on
2 April. If this module used `today > next_due_date` it would call that device
compliant for one more day, and "one more day" is a whole day of readings from a
device whose certificate has expired, on the exact day somebody might have
caught it.

The mirror-image rule, stated here so the two can never be confused:

    A CONTRACT is expired when  today > end_date.

`end_date` on an AMC is the LAST COVERED DAY, because it was copied off a piece
of paper a lawyer wrote and that is what such a date means there. A calibration's
`next_due_date` is the FIRST UNCOVERED DAY, because this system computed it. Two
different things that both look like "a date in a date column"; two named
functions — `is_overdue` and `contract_is_expired` — so neither can drift into
the other. Both boundaries are pinned by tests at day−1, day, day+1.

`grace_days` defaults to 0, i.e. no grace at all. It exists because some sites
run a documented tolerance window on non-critical PM; it must never be set on a
calibration schedule for a life-support device, and nothing in this module sets
it for you.

════════════════════════════════════════════════════════════════════════════════
 UNKNOWN IS NOT COMPLIANT
════════════════════════════════════════════════════════════════════════════════

`last_done_date IS NULL` means NEVER DONE. It is not backfilled from the
commissioning date, not defaulted to today, and — this is the part that matters —
it does not exempt the schedule from the overdue register. A machine nobody has
ever calibrated is the most dangerous row this table can hold, so `never_done`
travels on every read model and the register shows it first.

Similarly: an asset with NO calibration schedule at all is invisible to the
overdue view by construction, which is why `coverage_gaps()` exists and why the
overview reports `assets_with_no_schedule`. A register that only reports on the
assets somebody remembered to schedule is a register that grades its own
homework.

════════════════════════════════════════════════════════════════════════════════
 THIS IS NOT A CLINICAL PATH — AND THE PRODUCT QUESTION THAT LEAVES OPEN
════════════════════════════════════════════════════════════════════════════════

NOTHING in this module writes to medications, vitals, alerts, orders,
admissions, shadow capture or the audit chain, and NOTHING here can block a
clinical action. An overdue calibration is a prominent, ward-visible warning on
a screen. A nurse can still use the pump — and must be able to, because the
alternative (software refusing a device at 3am on a ward with one pump) kills
people faster than a 12% drift does.

THE OPEN PRODUCT QUESTION, stated plainly because it is a real one:
*should a reading from an out-of-calibration device be flagged where it is
shown, and should Vigil's alerts on such a reading carry that caveat?*

The honest answer is probably yes, and deliberately NOT tonight. Wiring it would
mean touching the vitals ingest path, which is a clinical write path and out of
scope by ruling. It would also need three things this module cannot supply on its
own: a reliable device-id → asset-tag mapping on the inbound HL7/serial feed
(today's `biomedical_assets.serial_number` is typed by a human and nothing
verifies it matches what the monitor announces); a decision on what a clinician
should DO with a flagged reading, since "this number may be wrong" with no
suggested action is noise that trains people to ignore banners; and a false-
positive budget, because a register with sloppy data would caveat half the
hospital's vitals within a month. `biomedical_assets.produces_clinical_readings`
exists and is populated for exactly this purpose, and is read by nothing.

════════════════════════════════════════════════════════════════════════════════
 CONVENTIONS (inherited from the hospital-admin spine, not reinvented)
════════════════════════════════════════════════════════════════════════════════

  * PURE SYNC. Every public function opens its own `db_session()`. Async callers
    go through `database.run_db`; the route module uses plain `def` handlers so
    FastAPI threadpools them. Blocking DB work inside `async def` is banned and
    tests/test_event_loop_discipline.py enforces it.
  * MONEY IS INTEGER PAISE and all arithmetic goes through services/money.py.
    There is no rounding rule in this file and no REAL column behind it.
  * NOT CONFIGURED IS NOT ZERO. An AMC nobody has valued reads "Not configured",
    never "₹0.00".
  * "TODAY" IS THE HOSPITAL'S DAY, from services/hospital_time.hospital_today().
    `datetime.now(timezone.utc).date()` is yesterday on an IST ward until 05:30,
    and an overdue register that flips a day late for the first five and a half
    hours of every day would be wrong precisely during night shift. Every
    date-comparing function takes an explicit `today` so a test can pin it.
  * NO FOREIGN KEYS exist on these tables (migration 21). Referential integrity
    is this module's job; a dangling vendor id yields "unknown", never a failed
    write.
"""

from __future__ import annotations

import logging

# sqlcipher3.dbapi2.IntegrityError is NOT a subclass of sqlite3.IntegrityError, so
# `except sqlite3.IntegrityError` is DEAD CODE whenever ORB_DB_ENCRYPTION_KEY is
# set — i.e. in the shipping config. Guarded by tests/test_db_exception_aliases.
from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal, overload

from database import db_session
from db_backend import DBIntegrityError  # sqlcipher3 raises its OWN hierarchy:

from services import facility_stamp
from services.hospital_time import hospital_today
from services.money import assert_non_negative_paise, format_inr_or_unset

logger = logging.getLogger("orb.biomedical")

__all__ = [
    "BiomedicalError",
    "UnknownAsset",
    # vocabulary
    "ASSET_CATEGORIES",
    "CRITICALITIES",
    "ASSET_STATUSES",
    "SCHEDULE_TYPES",
    "SCHEDULE_TYPE_LABELS",
    "SERVICE_CALL_STATUSES",
    "OPEN_CALL_STATUSES",
    "SERVICE_CALL_SEVERITIES",
    "SCHEDULE_RESULTS",
    "CONTRACT_TYPES",
    "CONTRACT_STATUSES",
    # the boundary rules — the whole module in four functions
    "compute_next_due",
    "is_overdue",
    "days_overdue",
    "contract_is_expired",
    "contract_days_remaining",
    "contract_in_notice_window",
    # assets
    "upsert_asset",
    "get_asset",
    "list_assets",
    "set_asset_status",
    # schedules
    "upsert_schedule",
    "get_schedule",
    "list_schedules",
    "record_schedule_completion",
    "due_register",
    "coverage_gaps",
    # service calls / downtime
    "open_service_call",
    "update_service_call",
    "close_service_call",
    "get_service_call",
    "list_service_calls",
    "asset_downtime_minutes",
    "downtime_summary",
    # contracts
    "upsert_contract",
    "get_contract",
    "list_contracts",
    "link_contract_assets",
    "expiring_contracts",
    # composite reads
    "biomedical_overview",
    "ward_attention",
]


# ═════════════════════════════════════════════════════════════════════════════
#  Errors
# ═════════════════════════════════════════════════════════════════════════════


class BiomedicalError(ValueError):
    """Anything a caller did wrong. Carries a stable machine-readable `code` so
    the route layer maps it to an HTTP status without string-matching English."""

    code = "biomedical_error"

    def __init__(self, message: str, *, code: str | None = None):
        super().__init__(message)
        if code:
            self.code = code


class UnknownAsset(BiomedicalError):
    """A schedule, call or contract named an asset that is not in the register."""

    code = "unknown_asset"


# ═════════════════════════════════════════════════════════════════════════════
#  Vocabulary
# ═════════════════════════════════════════════════════════════════════════════

#: Suggestions, not a closed set — the DB does not constrain `category`, because
#: no list anybody writes tonight covers what a hospital actually owns, and a
#: CHECK that refuses "cryo-cautery" teaches people to type "other".
ASSET_CATEGORIES: tuple[str, ...] = (
    "ventilator",
    "patient_monitor",
    "infusion_pump",
    "syringe_pump",
    "defibrillator",
    "anaesthesia_machine",
    "dialysis_machine",
    "ecg",
    "ultrasound",
    "xray",
    "ct",
    "mri",
    "c_arm",
    "oxygen_concentrator",
    "suction",
    "warmer",
    "phototherapy",
    "autoclave",
    "lab_analyser",
    "centrifuge",
    "refrigerator",
    "surgical_table",
    "electrosurgical_unit",
    "endoscope",
    "other",
)

#: Enforced by CHECK constraints in migration 21.
CRITICALITIES: tuple[str, ...] = ("life_support", "critical", "routine")

ASSET_STATUSES: tuple[str, ...] = (
    "in_service",
    "standby",
    "under_maintenance",
    "down",
    "condemned",
)

#: The statuses in which an asset is NOT available to a ward. Used by the ward
#: view and the overview; kept as a set so a new status has to be classified.
UNAVAILABLE_STATUSES: frozenset[str] = frozenset({"under_maintenance", "down", "condemned"})

SCHEDULE_TYPES: tuple[str, ...] = (
    "preventive_maintenance",
    "calibration",
    "electrical_safety",
)

SCHEDULE_TYPE_LABELS: dict[str, str] = {
    "preventive_maintenance": "Preventive maintenance",
    "calibration": "Calibration",
    "electrical_safety": "Electrical safety test",
}

SCHEDULE_RESULTS: tuple[str, ...] = ("pass", "pass_with_adjustment", "fail")

SERVICE_CALL_STATUSES: tuple[str, ...] = (
    "open",
    "in_progress",
    "awaiting_parts",
    "closed",
    "cancelled",
)

#: A call in one of these is still accruing downtime.
OPEN_CALL_STATUSES: frozenset[str] = frozenset({"open", "in_progress", "awaiting_parts"})

SERVICE_CALL_SEVERITIES: tuple[str, ...] = ("breakdown", "degraded", "preventive")

CONTRACT_TYPES: tuple[str, ...] = ("amc", "cmc", "warranty")

CONTRACT_STATUSES: tuple[str, ...] = ("active", "expired", "terminated", "renewed")

#: How far ahead the "due soon" bucket looks when a schedule does not say.
DEFAULT_LEAD_DAYS = 30

#: How far ahead the contract warning looks when a contract does not say.
DEFAULT_NOTICE_DAYS = 60


# ═════════════════════════════════════════════════════════════════════════════
#  THE BOUNDARY RULES
#
#  Four functions. Every date comparison in this module — service, routes,
#  reports, ward view — goes through one of them. Nothing below reimplements a
#  `<=` on a date, and nothing outside this block is allowed to either, because
#  the entire class of defect this module can have is an off-by-one on a day.
# ═════════════════════════════════════════════════════════════════════════════


def compute_next_due(last_done: date, interval_days: int) -> date:
    """The next due date for work completed on `last_done`.

    Plain calendar addition: 1 Jan + 90 days = 1 April. Deliberately NOT months —
    "every 3 months" is ambiguous (is 30 Nov + 3 months the 28th or the 30th of
    February?) and a maintenance interval that changes length depending on which
    month you are in is a maintenance interval nobody can plan staff around.
    Sites that think in months set 30/90/180/365.
    """
    if not isinstance(last_done, date) or isinstance(last_done, datetime):
        raise BiomedicalError(f"last_done must be a date (got {last_done!r})", code="bad_date")
    interval = _positive_int(interval_days, "interval_days")
    return last_done + timedelta(days=interval)


def is_overdue(next_due: date, today: date, grace_days: int = 0) -> bool:
    """True when the work is now late.

    ``today >= next_due + grace_days``. THE `>=` IS THE POINT: `next_due` is the
    first day NOT covered by the last certificate, so a device whose calibration
    is due on 1 April is out of calibration ON 1 April, not on the 2nd. See the
    module docstring. Pinned at day−1/day/day+1 by
    tests/test_biomedical.py::test_overdue_fires_on_the_due_date_not_the_day_after.
    """
    if grace_days:
        next_due = next_due + timedelta(days=_non_negative_int(grace_days, "grace_days"))
    return today >= next_due


def days_overdue(next_due: date, today: date, grace_days: int = 0) -> int:
    """How many days late, 0 on the first day it is late, 0 when not late.

    0 on the FIRST overdue day is arithmetic, not a bug: the effective date and
    today are the same day, so nothing has elapsed. The screen says "Overdue
    today" for 0 and "Overdue by N days" above it, rather than "Overdue by 0
    days", which reads like a rounding error and gets ignored.
    """
    if grace_days:
        next_due = next_due + timedelta(days=_non_negative_int(grace_days, "grace_days"))
    if today < next_due:
        return 0
    return (today - next_due).days


def contract_is_expired(end_date: date, today: date) -> bool:
    """True when an AMC/CMC no longer covers today.

    ``today > end_date`` — the OPPOSITE boundary from `is_overdue`, and
    deliberately so. `end_date` is the last day the contract covers, because that
    is what "valid to 31 March 2027" means on the contract a hospital signed. On
    31 March the vendor still has to come out; on 1 April they do not.
    """
    return today > end_date


def contract_days_remaining(end_date: date, today: date) -> int:
    """Days of cover left INCLUDING today. 0 on the day after it expires and
    below zero after that, so a caller can show "expired 12 days ago"."""
    return (end_date - today).days


def contract_in_notice_window(end_date: date, notice_days: int, today: date) -> bool:
    """True when a live contract is close enough to its end to act on.

    Live, i.e. NOT already expired — an expired contract is a different and
    louder state than one in its notice window, and folding the two together
    would let "renew this" and "you are uncovered right now" share a colour.
    Fires ON the first day of the window: with notice_days=60 and an end date of
    1 June, the warning is up on 2 April (60 days remaining), not on 3 April.
    """
    if contract_is_expired(end_date, today):
        return False
    return contract_days_remaining(end_date, today) <= _non_negative_int(notice_days, "notice_days")


# ═════════════════════════════════════════════════════════════════════════════
#  Small helpers
# ═════════════════════════════════════════════════════════════════════════════


def _row(row) -> dict:
    return {k: row[k] for k in row.keys()} if row is not None else {}


def _now() -> str:
    """UTC, second resolution, matching the CURRENT_TIMESTAMP format SQLite
    writes for the columns that default. Mixed formats in one column are how a
    date range silently drops rows."""
    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")


@overload
def _clean(value: str | None, *, field_name: str, required: Literal[True]) -> str: ...


@overload
def _clean(
    value: str | None, *, field_name: str, required: Literal[False] = False
) -> str | None: ...


@overload
def _clean(value: str | None, *, field_name: str, required: bool = False) -> str | None: ...


def _clean(value: str | None, *, field_name: str, required: bool = False) -> str | None:
    value = (value or "").strip() if value is not None else None
    if not value:
        if required:
            raise BiomedicalError(f"{field_name} is required", code="missing_field")
        return None
    return value


def _positive_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise BiomedicalError(
            f"{name} must be a whole number of days (got {value!r})", code="bad_interval"
        )
    if value <= 0:
        raise BiomedicalError(
            f"{name} must be greater than zero (got {value})", code="bad_interval"
        )
    return value


def _non_negative_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise BiomedicalError(f"{name} must be a whole number (got {value!r})", code="bad_number")
    if value < 0:
        raise BiomedicalError(f"{name} cannot be negative (got {value})", code="bad_number")
    return value


def as_date(value: date | datetime | str | None, field_name: str) -> date | None:
    """Anything a caller can plausibly send → a `date`, or a refusal that names
    the field. Never guesses a format: an unparseable date is a data-entry error
    and silently coercing it is how 01/02 becomes the wrong month."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    try:
        return date.fromisoformat(text[:10])
    except ValueError as exc:
        raise BiomedicalError(
            f"{field_name} is not a date in YYYY-MM-DD form: {value!r}", code="bad_date"
        ) from exc


def _require_date(value, field_name: str) -> date:
    parsed = as_date(value, field_name)
    if parsed is None:
        raise BiomedicalError(f"{field_name} is required", code="missing_field")
    return parsed


def _as_dt(value: datetime | str | None, field_name: str) -> datetime | None:
    """A timestamp → aware UTC datetime. Accepts the `YYYY-MM-DD HH:MM:SS` form
    SQLite writes and ISO-8601 with or without an offset. A NAIVE value is read
    as UTC, because every timestamp Orb persists is UTC."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip().replace(" ", "T", 1)
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
        except ValueError as exc:
            raise BiomedicalError(
                f"{field_name} is not a timestamp: {value!r}", code="bad_timestamp"
            ) from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _dt_str(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")


def _today(today: date | None) -> date:
    return today or hospital_today()


def _optional_paise(value: object, name: str) -> tuple[int | None, int]:
    """`(paise, is_configured)`. A cost nobody typed is (None, 0) — NOT (0, 1).
    Zero is a price; absence is not."""
    if value is None:
        return None, 0
    return assert_non_negative_paise(value, name), 1


def _one_of(
    value: str | None, allowed: tuple[str, ...], name: str, default: str | None = None
) -> str:
    text = (value or default or "").strip().lower()
    if text not in allowed:
        raise BiomedicalError(
            f"unknown {name} {value!r}; expected one of {list(allowed)}", code=f"bad_{name}"
        )
    return text


# ═════════════════════════════════════════════════════════════════════════════
#  Assets — the register
# ═════════════════════════════════════════════════════════════════════════════


def upsert_asset(
    *,
    asset_tag: str,
    name: str,
    category: str = "other",
    manufacturer: str | None = None,
    model: str | None = None,
    serial_number: str | None = None,
    department: str | None = None,
    ward: str | None = None,
    location_detail: str | None = None,
    criticality: str = "routine",
    status: str = "in_service",
    purchase_date: date | str | None = None,
    commissioned_date: date | str | None = None,
    purchase_cost_paise: int | None = None,
    warranty_expiry_date: date | str | None = None,
    vendor_id: int | None = None,
    produces_clinical_readings: bool = False,
    notes: str | None = None,
    is_active: bool = True,
    actor: str | None = None,
    create_only: bool = False,
) -> dict:
    """Create or update one asset, keyed on `asset_tag`.

    The tag is the hospital's own label — the sticker on the machine. It is
    upper-cased and unique, because two rows for one physical device means two
    people each maintaining what they believe is its whole history.

    `create_only` is the Add form: a tag already on the register is refused
    (code `already_exists`, naming the machine) instead of overwriting it. On an
    update, a blank optional field keeps what is on file; the Add form sends
    nulls for every field nobody typed in.
    """
    asset_tag = (_clean(asset_tag, field_name="asset tag", required=True) or "").upper()
    name = _clean(name, field_name="asset name", required=True)
    category = (category or "other").strip().lower() or "other"
    criticality = _one_of(criticality, CRITICALITIES, "criticality", "routine")
    status = _one_of(status, ASSET_STATUSES, "status", "in_service")
    cost_paise, cost_configured = _optional_paise(purchase_cost_paise, "purchase_cost_paise")

    purchase = as_date(purchase_date, "purchase_date")
    commissioned = as_date(commissioned_date, "commissioned_date")
    warranty = as_date(warranty_expiry_date, "warranty_expiry_date")
    if purchase and commissioned and commissioned < purchase:
        raise BiomedicalError(
            "commissioned_date is before purchase_date. A machine cannot be put "
            "into service before it was bought",
            code="bad_date_order",
        )

    with db_session() as db:
        if create_only:
            existing = db.execute(
                "SELECT name, ward FROM biomedical_assets WHERE asset_tag = ?", (asset_tag,)
            ).fetchone()
            if existing:
                where = f" on ward {existing['ward']}" if existing["ward"] else ""
                raise BiomedicalError(
                    f"{asset_tag} already exists as {existing['name']}{where}. "
                    "Nothing was changed. Use a different tag, or open that record "
                    "to edit it.",
                    code="already_exists",
                )
        facility_id = _asset_facility(
            db, asset_tag=asset_tag, department=department, ward=ward, actor=actor
        )
        # facility_id is written once, when the machine is registered, and is
        # never in the update list below (multisite-7b).
        db.execute(
            """INSERT INTO biomedical_assets (
                   asset_tag, name, category, manufacturer, model, serial_number,
                   department, ward, location_detail, criticality, status,
                   purchase_date, commissioned_date, purchase_cost_paise,
                   cost_is_configured, warranty_expiry_date, vendor_id,
                   produces_clinical_readings, notes, is_active, created_by,
                   facility_id)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(asset_tag) DO UPDATE SET
                   name = excluded.name,
                   category = excluded.category,
                   manufacturer = COALESCE(excluded.manufacturer, manufacturer),
                   model = COALESCE(excluded.model, model),
                   serial_number = COALESCE(excluded.serial_number, serial_number),
                   department = COALESCE(excluded.department, department),
                   ward = COALESCE(excluded.ward, ward),
                   location_detail = COALESCE(excluded.location_detail, location_detail),
                   criticality = excluded.criticality,
                   status = excluded.status,
                   purchase_date = COALESCE(excluded.purchase_date, purchase_date),
                   commissioned_date = COALESCE(excluded.commissioned_date, commissioned_date),
                   purchase_cost_paise = CASE WHEN excluded.cost_is_configured = 1
                       THEN excluded.purchase_cost_paise ELSE purchase_cost_paise END,
                   cost_is_configured = MAX(excluded.cost_is_configured, cost_is_configured),
                   warranty_expiry_date = COALESCE(excluded.warranty_expiry_date, warranty_expiry_date),
                   vendor_id = COALESCE(excluded.vendor_id, vendor_id),
                   produces_clinical_readings = excluded.produces_clinical_readings,
                   notes = COALESCE(excluded.notes, notes),
                   is_active = excluded.is_active,
                   updated_by = excluded.created_by,
                   updated_at = CURRENT_TIMESTAMP""",
            (
                asset_tag,
                name,
                category,
                _clean(manufacturer, field_name="manufacturer"),
                _clean(model, field_name="model"),
                _clean(serial_number, field_name="serial_number"),
                _clean(department, field_name="department"),
                _clean(ward, field_name="ward"),
                _clean(location_detail, field_name="location_detail"),
                criticality,
                status,
                purchase.isoformat() if purchase else None,
                commissioned.isoformat() if commissioned else None,
                cost_paise,
                cost_configured,
                warranty.isoformat() if warranty else None,
                int(vendor_id) if vendor_id is not None else None,
                1 if produces_clinical_readings else 0,
                _clean(notes, field_name="notes"),
                1 if is_active else 0,
                actor,
                facility_id,
            ),
        )
        row = db.execute(
            "SELECT * FROM biomedical_assets WHERE asset_tag = ?", (asset_tag,)
        ).fetchone()
    return _asset_view(_row(row))


def _asset_facility(
    db: Any, *, asset_tag: str, department: str | None, ward: str | None, actor: str | None
) -> int:
    """The facility a machine is registered at (multisite-7b, migration 75):
    its department's in the departments master, else its ward's in Bed
    setup, else the facility of the account registering it. A machine on the
    register keeps its facility: a department or ward at another facility is
    refused, because moving equipment between facilities is not supported
    yet. Facility 1, with nothing read, while only one facility is on record."""

    def registered() -> int | None:
        prior = db.execute(
            "SELECT facility_id FROM biomedical_assets WHERE asset_tag = ?", (asset_tag,)
        ).fetchone()
        return None if prior is None or prior[0] is None else int(prior[0])

    dept = _clean(department, field_name="department")
    ward_text = _clean(ward, field_name="ward")
    try:
        return facility_stamp.resolve(
            db,
            (
                ("the machine as registered", registered),
                ("the department", lambda: facility_stamp.department_facility(db, dept)),
                ("the ward", lambda: facility_stamp.ward_facility(db, ward=ward_text)),
            ),
            what="machine",
            fallback=lambda: facility_stamp.account_facility(db, actor),
            unknown_hint="Name its department.",
        )
    except facility_stamp.FacilityStampError as exc:
        raise BiomedicalError(exc.message, code=exc.code) from exc


def _asset_view(row: dict, today: date | None = None) -> dict:
    """One asset as a screen wants it. Adds only DERIVED fields — never invents
    a stored one."""
    if not row:
        return {}
    day = _today(today)
    out = dict(row)
    out["criticality_label"] = {
        "life_support": "Life support",
        "critical": "Critical",
        "routine": "Routine",
    }.get(row.get("criticality") or "", row.get("criticality"))
    out["cost_display"] = format_inr_or_unset(
        row.get("purchase_cost_paise") if row.get("cost_is_configured") else None
    )
    warranty = as_date(row.get("warranty_expiry_date"), "warranty_expiry_date")
    # Warranty uses the CONTRACT rule (end_date is the last covered day), not the
    # schedule rule — a warranty is a contract, whatever column it lives in.
    out["warranty_active"] = warranty is not None and not contract_is_expired(warranty, day)
    out["warranty_days_remaining"] = contract_days_remaining(warranty, day) if warranty else None
    out["is_available"] = bool(row.get("is_active")) and (
        row.get("status") not in UNAVAILABLE_STATUSES
    )
    return out


def get_asset(
    asset_id: int | None = None, *, asset_tag: str | None = None, today: date | None = None
) -> dict:
    """One asset with its schedules, open calls and contracts. Empty dict if it
    does not exist — callers that need a refusal use `_resolve_asset`."""
    day = _today(today)
    with db_session() as db:
        row = _fetch_asset(db, asset_id=asset_id, asset_tag=asset_tag)
        if row is None:
            return {}
        asset = _asset_view(_row(row), day)
        aid = asset["id"]
        asset["schedules"] = [
            _schedule_view(_row(r), day)
            for r in db.execute(
                "SELECT * FROM biomedical_schedules WHERE asset_id = ? "
                "ORDER BY next_due_date, schedule_type",
                (aid,),
            ).fetchall()
        ]
        asset["service_calls"] = [
            _call_view(_row(r))
            for r in db.execute(
                "SELECT * FROM biomedical_service_calls WHERE asset_id = ? "
                "ORDER BY reported_at DESC, id DESC LIMIT 50",
                (aid,),
            ).fetchall()
        ]
        asset["contracts"] = [
            _contract_view(_row(r), day)
            for r in db.execute(
                "SELECT c.* FROM biomedical_contracts c "
                "JOIN biomedical_contract_assets ca ON ca.contract_id = c.id "
                "WHERE ca.asset_id = ? ORDER BY c.end_date DESC",
                (aid,),
            ).fetchall()
        ]
        asset["history"] = [
            _row(r)
            for r in db.execute(
                "SELECT * FROM biomedical_schedule_events WHERE asset_id = ? "
                "ORDER BY performed_date DESC, id DESC LIMIT 50",
                (aid,),
            ).fetchall()
        ]
    asset["downtime_minutes_90d"] = asset_downtime_minutes(
        asset["id"], since=day - timedelta(days=90)
    )
    return asset


def _fetch_asset(db, *, asset_id: int | None = None, asset_tag: str | None = None):
    if asset_id is not None:
        return db.execute(
            "SELECT * FROM biomedical_assets WHERE id = ?", (int(asset_id),)
        ).fetchone()
    if asset_tag:
        return db.execute(
            "SELECT * FROM biomedical_assets WHERE asset_tag = ?", (str(asset_tag).strip().upper(),)
        ).fetchone()
    raise BiomedicalError("an asset id or asset tag is required", code="missing_field")


def _resolve_asset_id(db, *, asset_id: int | None = None, asset_tag: str | None = None) -> int:
    row = _fetch_asset(db, asset_id=asset_id, asset_tag=asset_tag)
    if row is None:
        raise UnknownAsset(
            f"no asset with {'id ' + str(asset_id) if asset_id is not None else 'tag ' + repr(asset_tag)} "
            "in the equipment register"
        )
    return int(row["id"])


def list_assets(
    *,
    ward: str | None = None,
    department: str | None = None,
    category: str | None = None,
    criticality: str | None = None,
    status: str | None = None,
    search: str | None = None,
    include_inactive: bool = False,
    limit: int = 200,
    today: date | None = None,
) -> list[dict]:
    """The register, filtered. Ordered by criticality then tag so the machines
    that matter most are at the top of the page an assessor is handed."""
    day = _today(today)
    clauses: list[str] = []
    params: list[Any] = []
    if not include_inactive:
        clauses.append("is_active = 1")
    if ward:
        clauses.append("ward = ?")
        params.append(ward.strip())
    if department:
        clauses.append("department = ?")
        params.append(department.strip())
    if category:
        clauses.append("category = ?")
        params.append(category.strip().lower())
    if criticality:
        clauses.append("criticality = ?")
        params.append(_one_of(criticality, CRITICALITIES, "criticality"))
    if status:
        clauses.append("status = ?")
        params.append(_one_of(status, ASSET_STATUSES, "status"))
    if search:
        needle = f"%{search.strip()}%"
        clauses.append(
            "(asset_tag LIKE ? OR name LIKE ? OR model LIKE ? "
            "OR serial_number LIKE ? OR manufacturer LIKE ?)"
        )
        params.extend([needle] * 5)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    # CASE, not a lookup table, because SQLite has no enum ordering and the three
    # criticalities are a fixed vocabulary enforced by a CHECK.
    sql = (
        f"SELECT * FROM biomedical_assets {where} "
        "ORDER BY CASE criticality WHEN 'life_support' THEN 0 WHEN 'critical' THEN 1 "
        "ELSE 2 END, asset_tag LIMIT ?"
    )
    params.append(max(1, min(int(limit), 1000)))
    with db_session() as db:
        rows = db.execute(sql, params).fetchall()
    return [_asset_view(_row(r), day) for r in rows]


def set_asset_status(
    *,
    asset_id: int | None = None,
    asset_tag: str | None = None,
    status: str,
    reason: str | None = None,
    actor: str | None = None,
) -> dict:
    """Move an asset between in_service / standby / under_maintenance / down /
    condemned.

    CONDEMNED IS NOT A DELETE. The row stays, its history stays, and it drops out
    of the default register by `is_active = 0` — because an assessor asking "what
    happened to pump 14" must get an answer, and because a deleted row is how a
    device with a bad history quietly returns to a ward.
    """
    status = _one_of(status, ASSET_STATUSES, "status")
    if status == "condemned" and not (reason or "").strip():
        raise BiomedicalError("condemning an asset needs a reason", code="missing_field")
    with db_session() as db:
        aid = _resolve_asset_id(db, asset_id=asset_id, asset_tag=asset_tag)
        note = _clean(reason, field_name="reason")
        db.execute(
            "UPDATE biomedical_assets SET status = ?, "
            "is_active = CASE WHEN ? = 'condemned' THEN 0 ELSE is_active END, "
            "notes = COALESCE(?, notes), updated_by = ?, updated_at = CURRENT_TIMESTAMP "
            "WHERE id = ?",
            (status, status, note, actor, aid),
        )
        row = db.execute("SELECT * FROM biomedical_assets WHERE id = ?", (aid,)).fetchone()
    logger.info("biomedical asset %s status=%s by %s", aid, status, actor)
    return _asset_view(_row(row))


# ═════════════════════════════════════════════════════════════════════════════
#  Schedules — preventive maintenance and CALIBRATION
# ═════════════════════════════════════════════════════════════════════════════


def upsert_schedule(
    *,
    asset_id: int | None = None,
    asset_tag: str | None = None,
    schedule_type: str,
    interval_days: int,
    last_done_date: date | str | None = None,
    first_due_date: date | str | None = None,
    grace_days: int = 0,
    lead_days: int = DEFAULT_LEAD_DAYS,
    responsible: str | None = None,
    vendor_id: int | None = None,
    certificate_reference: str | None = None,
    is_active: bool = True,
    actor: str | None = None,
) -> dict:
    """Create or replace the PM / calibration / electrical-safety schedule for one
    asset. One schedule per (asset, type) — enforced by a unique index.

    WHERE `next_due_date` COMES FROM, in order:

      1. `last_done_date` + `interval_days`, when the site knows when the work was
         last done. This is the normal case and the one the module is built on.
      2. `first_due_date`, when it does not. An asset commissioned in 2019 whose
         paperwork is in a cupboard gets a date the biomedical engineer decides;
         `last_done_date` stays NULL and the schedule reads "never recorded in
         Orb", which is the truth.
      3. Neither → REFUSED. There is deliberately no fallback to "today +
         interval". That default would silently mark every legacy machine
         compliant for a full cycle on the day it was entered, which is the exact
         lie an equipment register exists to prevent, and it would be invisible
         because the resulting screen looks perfect.
    """
    schedule_type = _one_of(schedule_type, SCHEDULE_TYPES, "schedule_type")
    interval = _positive_int(interval_days, "interval_days")
    grace = _non_negative_int(grace_days, "grace_days")
    lead = _non_negative_int(lead_days, "lead_days")
    last_done = as_date(last_done_date, "last_done_date")
    first_due = as_date(first_due_date, "first_due_date")

    if last_done is not None:
        next_due = compute_next_due(last_done, interval)
    elif first_due is not None:
        next_due = first_due
    else:
        raise BiomedicalError(
            "a schedule needs either last_done_date (the due date is computed "
            "from it) or first_due_date (for equipment whose last service Orb "
            "has no record of). Orb will not assume a machine was serviced "
            "today just because it was entered today.",
            code="missing_due_basis",
        )

    with db_session() as db:
        aid = _resolve_asset_id(db, asset_id=asset_id, asset_tag=asset_tag)
        db.execute(
            """INSERT INTO biomedical_schedules (
                   asset_id, schedule_type, interval_days, last_done_date,
                   next_due_date, grace_days, lead_days, responsible, vendor_id,
                   last_certificate_reference, is_active, created_by)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(asset_id, schedule_type) DO UPDATE SET
                   interval_days = excluded.interval_days,
                   last_done_date = excluded.last_done_date,
                   next_due_date = excluded.next_due_date,
                   grace_days = excluded.grace_days,
                   lead_days = excluded.lead_days,
                   responsible = excluded.responsible,
                   vendor_id = excluded.vendor_id,
                   last_certificate_reference = excluded.last_certificate_reference,
                   is_active = excluded.is_active,
                   updated_at = CURRENT_TIMESTAMP""",
            (
                aid,
                schedule_type,
                interval,
                last_done.isoformat() if last_done else None,
                next_due.isoformat(),
                grace,
                lead,
                _clean(responsible, field_name="responsible"),
                int(vendor_id) if vendor_id is not None else None,
                _clean(certificate_reference, field_name="certificate_reference"),
                1 if is_active else 0,
                actor,
            ),
        )
        row = db.execute(
            "SELECT * FROM biomedical_schedules WHERE asset_id = ? AND schedule_type = ?",
            (aid, schedule_type),
        ).fetchone()
    return _schedule_view(_row(row))


def _schedule_view(row: dict, today: date | None = None, asset: dict | None = None) -> dict:
    """One schedule with its compliance state resolved. THE ONLY PLACE the panel
    and the API learn whether something is overdue."""
    if not row:
        return {}
    day = _today(today)
    next_due = as_date(row.get("next_due_date"), "next_due_date")
    grace = int(row.get("grace_days") or 0)
    lead = int(row.get("lead_days") or DEFAULT_LEAD_DAYS)
    active = bool(row.get("is_active", 1))

    out = dict(row)
    out["schedule_type_label"] = SCHEDULE_TYPE_LABELS.get(
        row.get("schedule_type") or "", row.get("schedule_type")
    )
    out["never_done"] = row.get("last_done_date") in (None, "")
    out["effective_due_date"] = (
        (next_due + timedelta(days=grace)).isoformat()
        if next_due and grace
        else (next_due.isoformat() if next_due else None)
    )

    overdue = next_due is not None and active and is_overdue(next_due, day, grace)
    out["is_overdue"] = overdue
    out["days_overdue"] = days_overdue(next_due, day, grace) if next_due else 0
    out["days_until_due"] = (next_due + timedelta(days=grace) - day).days if next_due else None
    out["due_soon"] = bool(
        next_due
        and active
        and not overdue
        and (next_due + timedelta(days=grace) - day).days <= lead
    )
    if not active:
        out["compliance"] = "inactive"
    elif overdue:
        out["compliance"] = "overdue"
    elif out["due_soon"]:
        out["compliance"] = "due_soon"
    else:
        out["compliance"] = "ok"
    # An overdue CALIBRATION on a device Orb reads numbers from is the specific
    # thing this module was built to surface. Flagged here so no caller has to
    # rediscover the combination.
    out["measurement_at_risk"] = bool(
        overdue
        and row.get("schedule_type") == "calibration"
        and (asset or {}).get("produces_clinical_readings")
    )
    if asset:
        for key in (
            "asset_tag",
            "name",
            "ward",
            "department",
            "criticality",
            "status",
            "produces_clinical_readings",
        ):
            out[f"asset_{key}" if key != "name" else "asset_name"] = asset.get(key)
    return out


def get_schedule(schedule_id: int, today: date | None = None) -> dict:
    with db_session() as db:
        row = db.execute(
            "SELECT * FROM biomedical_schedules WHERE id = ?", (int(schedule_id),)
        ).fetchone()
        if row is None:
            return {}
        asset = _row(
            db.execute(
                "SELECT * FROM biomedical_assets WHERE id = ?", (row["asset_id"],)
            ).fetchone()
        )
    return _schedule_view(_row(row), _today(today), asset)


def list_schedules(
    *,
    asset_id: int | None = None,
    schedule_type: str | None = None,
    ward: str | None = None,
    today: date | None = None,
) -> list[dict]:
    clauses = ["1=1"]
    params: list[Any] = []
    if asset_id is not None:
        clauses.append("s.asset_id = ?")
        params.append(int(asset_id))
    if schedule_type:
        clauses.append("s.schedule_type = ?")
        params.append(_one_of(schedule_type, SCHEDULE_TYPES, "schedule_type"))
    if ward:
        clauses.append("a.ward = ?")
        params.append(ward.strip())
    with db_session() as db:
        rows = db.execute(
            f"SELECT s.*, a.asset_tag, a.name AS asset_name, a.ward, a.department, "
            f"a.criticality, a.status AS asset_status, a.produces_clinical_readings "
            f"FROM biomedical_schedules s "
            f"JOIN biomedical_assets a ON a.id = s.asset_id "
            f"WHERE {' AND '.join(clauses)} ORDER BY s.next_due_date, a.asset_tag",
            params,
        ).fetchall()
    day = _today(today)
    return [_schedule_view(_row(r), day, _asset_from_join(_row(r))) for r in rows]


def _asset_from_join(row: dict) -> dict:
    """The asset columns carried along a schedule JOIN, in asset shape."""
    return {
        "asset_tag": row.get("asset_tag"),
        "name": row.get("asset_name"),
        "ward": row.get("ward"),
        "department": row.get("department"),
        "criticality": row.get("criticality"),
        "status": row.get("asset_status"),
        "produces_clinical_readings": row.get("produces_clinical_readings"),
    }


def record_schedule_completion(
    *,
    schedule_id: int | None = None,
    asset_id: int | None = None,
    asset_tag: str | None = None,
    schedule_type: str | None = None,
    performed_date: date | str | None = None,
    result: str = "pass",
    certificate_reference: str | None = None,
    performed_by: str | None = None,
    vendor_id: int | None = None,
    cost_paise: int | None = None,
    notes: str | None = None,
    actor: str | None = None,
    today: date | None = None,
) -> dict:
    """Record that a PM / calibration was done, and roll the schedule forward.

    `next_due_date` moves to `performed_date + interval_days` — from the date the
    work was ACTUALLY DONE, not from the date it was due. A device calibrated 20
    days late gets its next 365-day certificate from the day of the calibration,
    which is what the certificate itself says; anchoring to the old due date
    would claim cover for 20 days nobody tested.

    A FAILED calibration does NOT roll the schedule forward. The device did not
    pass, so nothing has been certified, and marking it due in another year would
    convert a failure into a year of false compliance. `result='fail'` leaves
    `next_due_date` where it was (so the asset stays in the overdue register) and
    records the attempt.
    """
    day = _today(today)
    performed = as_date(performed_date, "performed_date") or day
    if performed > day:
        raise BiomedicalError(
            f"performed_date {performed.isoformat()} is in the future (today is {day.isoformat()})",
            code="future_date",
        )
    result = _one_of(result, SCHEDULE_RESULTS, "result", "pass")

    with db_session() as db:
        if schedule_id is not None:
            row = db.execute(
                "SELECT * FROM biomedical_schedules WHERE id = ?", (int(schedule_id),)
            ).fetchone()
        else:
            aid = _resolve_asset_id(db, asset_id=asset_id, asset_tag=asset_tag)
            stype = _one_of(schedule_type, SCHEDULE_TYPES, "schedule_type")
            row = db.execute(
                "SELECT * FROM biomedical_schedules WHERE asset_id = ? AND schedule_type = ?",
                (aid, stype),
            ).fetchone()
        if row is None:
            raise BiomedicalError("no such schedule", code="not_found")

        sched = _row(row)
        previous_due = as_date(sched.get("next_due_date"), "next_due_date")
        grace = int(sched.get("grace_days") or 0)
        was_late = previous_due is not None and is_overdue(previous_due, performed, grace)
        late_by = days_overdue(previous_due, performed, grace) if previous_due else 0

        if result == "fail":
            new_due = previous_due
            last_done_for_schedule = sched.get("last_done_date")
            certificate_for_schedule = sched.get("last_certificate_reference")
        else:
            new_due = compute_next_due(performed, int(sched["interval_days"]))
            last_done_for_schedule = performed.isoformat()
            certificate_for_schedule = _clean(
                certificate_reference, field_name="certificate_reference"
            )

        db.execute(
            "UPDATE biomedical_schedules SET last_done_date = ?, next_due_date = ?, "
            "last_certificate_reference = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (
                last_done_for_schedule,
                new_due.isoformat() if new_due else sched.get("next_due_date"),
                certificate_for_schedule,
                sched["id"],
            ),
        )
        db.execute(
            """INSERT INTO biomedical_schedule_events (
                   schedule_id, asset_id, schedule_type, performed_date,
                   previous_due_date, next_due_date, was_overdue, days_overdue,
                   result, certificate_reference, performed_by, vendor_id,
                   cost_paise, notes, recorded_by)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                sched["id"],
                sched["asset_id"],
                sched["schedule_type"],
                performed.isoformat(),
                previous_due.isoformat() if previous_due else None,
                effective_due.isoformat()
                if (effective_due := new_due or previous_due) is not None
                else None,
                1 if was_late else 0,
                late_by,
                result,
                _clean(certificate_reference, field_name="certificate_reference"),
                _clean(performed_by, field_name="performed_by"),
                int(vendor_id) if vendor_id is not None else None,
                _optional_paise(cost_paise, "cost_paise")[0],
                _clean(notes, field_name="notes"),
                actor,
            ),
        )
        fresh = db.execute(
            "SELECT * FROM biomedical_schedules WHERE id = ?", (sched["id"],)
        ).fetchone()
        asset = _row(
            db.execute(
                "SELECT * FROM biomedical_assets WHERE id = ?", (sched["asset_id"],)
            ).fetchone()
        )
    logger.info(
        "biomedical %s recorded for asset %s (result=%s, late=%s)",
        sched["schedule_type"],
        sched["asset_id"],
        result,
        was_late,
    )
    view = _schedule_view(_row(fresh), day, asset)
    view["recorded_result"] = result
    view["recorded_was_overdue"] = was_late
    view["recorded_days_overdue"] = late_by
    return view


def due_register(
    *,
    today: date | None = None,
    schedule_type: str | None = None,
    ward: str | None = None,
    include_due_soon: bool = True,
    only_overdue: bool = False,
) -> dict:
    """THE OVERDUE VIEW. The screen that answers "what is out of calibration
    right now", and the one an assessor is shown.

    Returns `overdue` and `due_soon` as separate lists rather than one list with
    a flag, because they are different actions: overdue is "this is happening
    now", due soon is "book the engineer". Ordered by how late, worst first, with
    life-support equipment ahead of routine at equal lateness.
    """
    day = _today(today)
    rows = list_schedules(schedule_type=schedule_type, ward=ward, today=day)
    overdue = [r for r in rows if r["compliance"] == "overdue"]
    due_soon = [r for r in rows if r["compliance"] == "due_soon"]

    crit_rank = {"life_support": 0, "critical": 1, "routine": 2}
    overdue.sort(
        key=lambda r: (
            -r["days_overdue"],
            crit_rank.get((r.get("asset_criticality") or ""), 3),
            r.get("asset_asset_tag") or "",
        )
    )
    due_soon.sort(
        key=lambda r: (
            r["days_until_due"] if r["days_until_due"] is not None else 9999,
            crit_rank.get((r.get("asset_criticality") or ""), 3),
        )
    )

    return {
        "as_of": day.isoformat(),
        "overdue": overdue,
        "overdue_count": len(overdue),
        "due_soon": [] if (only_overdue or not include_due_soon) else due_soon,
        "due_soon_count": len(due_soon),
        "overdue_calibration_count": sum(
            1 for r in overdue if r.get("schedule_type") == "calibration"
        ),
        "overdue_life_support_count": sum(
            1 for r in overdue if r.get("asset_criticality") == "life_support"
        ),
        "never_done_count": sum(1 for r in overdue if r.get("never_done")),
        "measurement_at_risk_count": sum(1 for r in overdue if r.get("measurement_at_risk")),
    }


def coverage_gaps(*, today: date | None = None) -> list[dict]:
    """Active assets with NO schedule of a given type.

    The register's blind spot, made explicit. An overdue view can only ever
    report on schedules that exist, so the machine nobody set a calibration
    schedule for is perfectly compliant according to every other read in this
    module. That is the failure this function exists to prevent, and the reason
    the overview reports its count next to the overdue count rather than in a
    drill-down.
    """
    with db_session() as db:
        rows = db.execute(
            "SELECT a.* FROM biomedical_assets a "
            "WHERE a.is_active = 1 AND a.status != 'condemned' "
            "AND NOT EXISTS (SELECT 1 FROM biomedical_schedules s "
            "                WHERE s.asset_id = a.id AND s.is_active = 1) "
            "ORDER BY CASE a.criticality WHEN 'life_support' THEN 0 "
            "         WHEN 'critical' THEN 1 ELSE 2 END, a.asset_tag"
        ).fetchall()
    return [_asset_view(_row(r), _today(today)) for r in rows]


# ═════════════════════════════════════════════════════════════════════════════
#  Service calls and downtime
# ═════════════════════════════════════════════════════════════════════════════


def open_service_call(
    *,
    asset_id: int | None = None,
    asset_tag: str | None = None,
    fault_description: str,
    severity: str = "breakdown",
    reported_at: datetime | str | None = None,
    reported_by: str | None = None,
    ward: str | None = None,
    took_out_of_service: bool = True,
    down_from: datetime | str | None = None,
    call_number: str | None = None,
    vendor_id: int | None = None,
    actor: str | None = None,
) -> dict:
    """Log a breakdown or service call.

    `down_from` vs `reported_at`: a pump fails at 02:10 and the call is logged at
    07:00 when the day shift arrives. Charging downtime only from 07:00 flatters
    the uptime figure by five hours, so `down_from` is a separate field. It
    defaults to `reported_at` when nobody knows better, and the returned record
    says which was used (`down_from_assumed`).

    A call with `took_out_of_service=False` — a noisy fan, a cracked casing —
    accrues NO downtime. The machine is still working. Conflating the two would
    make the availability figure meaningless in the direction that flatters
    nobody: it would make a well-run department look bad and hide the real
    outages inside the noise.
    """
    severity = _one_of(severity, SERVICE_CALL_SEVERITIES, "severity", "breakdown")
    fault = _clean(fault_description, field_name="fault_description", required=True)
    reported = _as_dt(reported_at, "reported_at") or datetime.now(UTC)
    down = _as_dt(down_from, "down_from")
    assumed = down is None
    if took_out_of_service:
        down = down or reported
        if down > reported:
            raise BiomedicalError(
                "down_from is after reported_at. A machine cannot go out of "
                "service after the fault was reported",
                code="bad_date_order",
            )
    else:
        down = None

    with db_session() as db:
        aid = _resolve_asset_id(db, asset_id=asset_id, asset_tag=asset_tag)
        asset = _row(db.execute("SELECT * FROM biomedical_assets WHERE id = ?", (aid,)).fetchone())
        try:
            cur = db.execute(
                """INSERT INTO biomedical_service_calls (
                       call_number, asset_id, reported_at, reported_by, ward,
                       fault_description, severity, took_out_of_service, down_from,
                       status, vendor_id, created_by)
                   VALUES (?,?,?,?,?,?,?,?,?,'open',?,?)""",
                (
                    _clean(call_number, field_name="call_number"),
                    aid,
                    _dt_str(reported),
                    _clean(reported_by, field_name="reported_by"),
                    _clean(ward, field_name="ward") or asset.get("ward"),
                    fault,
                    severity,
                    1 if took_out_of_service else 0,
                    _dt_str(down),
                    int(vendor_id) if vendor_id is not None else None,
                    actor,
                ),
            )
        except DBIntegrityError as exc:
            raise BiomedicalError(
                f"a service call numbered {call_number!r} already exists",
                code="duplicate_call_number",
            ) from exc
        call_id = cur.lastrowid
        # A breakdown that took the machine out of service moves the ASSET to
        # 'down' as well, so the register and the ward view agree without anybody
        # remembering to update two places. A condemned asset is not resurrected.
        if took_out_of_service and asset.get("status") != "condemned":
            db.execute(
                "UPDATE biomedical_assets SET status = 'down', "
                "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (aid,),
            )
        row = db.execute(
            "SELECT * FROM biomedical_service_calls WHERE id = ?", (call_id,)
        ).fetchone()
    view = _call_view(_row(row))
    view["down_from_assumed"] = assumed and took_out_of_service
    logger.info("biomedical service call %s opened on asset %s (%s)", call_id, aid, severity)
    return view


def update_service_call(
    *,
    call_id: int,
    status: str | None = None,
    resolution: str | None = None,
    attended_by: str | None = None,
    vendor_id: int | None = None,
    contract_id: int | None = None,
    actor: str | None = None,
) -> dict:
    """Progress an open call without closing it (parts ordered, engineer on site).

    Closing is `close_service_call` and nothing else, because closing is the act
    that computes downtime and needs a restoration timestamp — allowing
    `status='closed'` through here would produce a closed call with no downtime,
    silently subtracting a real outage from the hospital's figures.
    """
    if status is not None:
        status = _one_of(status, SERVICE_CALL_STATUSES, "status")
        if status == "closed":
            raise BiomedicalError(
                "close a service call with close_service_call, which records when "
                "the machine was back in service and computes the downtime",
                code="use_close",
            )
    with db_session() as db:
        row = db.execute(
            "SELECT * FROM biomedical_service_calls WHERE id = ?", (int(call_id),)
        ).fetchone()
        if row is None:
            raise BiomedicalError(f"no service call {call_id}", code="not_found")
        current = _row(row)
        if current["status"] in ("closed", "cancelled") and status not in (None, current["status"]):
            raise BiomedicalError(
                f"service call {call_id} is {current['status']} and cannot be reopened",
                code="call_closed",
            )
        db.execute(
            "UPDATE biomedical_service_calls SET status = COALESCE(?, status), "
            "resolution = COALESCE(?, resolution), attended_by = COALESCE(?, attended_by), "
            "vendor_id = COALESCE(?, vendor_id), contract_id = COALESCE(?, contract_id) "
            "WHERE id = ?",
            (
                status,
                _clean(resolution, field_name="resolution"),
                _clean(attended_by, field_name="attended_by"),
                int(vendor_id) if vendor_id is not None else None,
                int(contract_id) if contract_id is not None else None,
                int(call_id),
            ),
        )
        fresh = db.execute(
            "SELECT * FROM biomedical_service_calls WHERE id = ?", (int(call_id),)
        ).fetchone()
    return _call_view(_row(fresh))


def close_service_call(
    *,
    call_id: int,
    restored_at: datetime | str | None = None,
    resolution: str | None = None,
    attended_by: str | None = None,
    vendor_id: int | None = None,
    contract_id: int | None = None,
    cost_paise: int | None = None,
    return_to_service: bool = True,
    actor: str | None = None,
) -> dict:
    """Close a call, stamp the restoration time, and freeze the downtime.

    `downtime_minutes` is written HERE and only here, as whole minutes between
    `down_from` and `restored_at`. Truncated, not rounded: a 90-second outage is
    1 minute, not 2. It is stored rather than always derived so that a later edit
    to the asset's ward or an ops backfill cannot silently change a figure the
    hospital has already reported.

    A call that never took the machine out of service closes with
    `downtime_minutes = 0`, not NULL — zero downtime is a measured fact here,
    unlike a cost nobody typed.
    """
    restored = _as_dt(restored_at, "restored_at") or datetime.now(UTC)
    with db_session() as db:
        row = db.execute(
            "SELECT * FROM biomedical_service_calls WHERE id = ?", (int(call_id),)
        ).fetchone()
        if row is None:
            raise BiomedicalError(f"no service call {call_id}", code="not_found")
        call = _row(row)
        if call["status"] == "closed":
            raise BiomedicalError(
                f"service call {call_id} is already closed", code="already_closed"
            )
        if call["status"] == "cancelled":
            raise BiomedicalError(f"service call {call_id} was cancelled", code="call_closed")

        down = _as_dt(call.get("down_from"), "down_from")
        if down is None:
            minutes = 0
        else:
            if restored < down:
                raise BiomedicalError(
                    "restored_at is before the machine went out of service", code="bad_date_order"
                )
            minutes = int((restored - down).total_seconds() // 60)

        cost, cost_configured = _optional_paise(cost_paise, "cost_paise")
        db.execute(
            "UPDATE biomedical_service_calls SET status = 'closed', restored_at = ?, "
            "downtime_minutes = ?, resolution = COALESCE(?, resolution), "
            "attended_by = COALESCE(?, attended_by), vendor_id = COALESCE(?, vendor_id), "
            "contract_id = COALESCE(?, contract_id), cost_paise = COALESCE(?, cost_paise), "
            "cost_is_configured = CASE WHEN ? = 1 THEN 1 ELSE cost_is_configured END, "
            "closed_by = ?, closed_at = ? WHERE id = ?",
            (
                _dt_str(restored),
                minutes,
                _clean(resolution, field_name="resolution"),
                _clean(attended_by, field_name="attended_by"),
                int(vendor_id) if vendor_id is not None else None,
                int(contract_id) if contract_id is not None else None,
                cost,
                cost_configured,
                actor,
                _now(),
                int(call_id),
            ),
        )
        # Back in service only when the caller says so AND nothing else is still
        # holding the machine down. A second open call on the same asset means it
        # is still broken for a different reason.
        if return_to_service:
            still_down = db.execute(
                "SELECT COUNT(*) FROM biomedical_service_calls "
                "WHERE asset_id = ? AND id != ? AND took_out_of_service = 1 "
                f"AND status IN ({','.join('?' * len(OPEN_CALL_STATUSES))})",
                (call["asset_id"], int(call_id), *sorted(OPEN_CALL_STATUSES)),
            ).fetchone()[0]
            if not still_down:
                db.execute(
                    "UPDATE biomedical_assets SET status = 'in_service', "
                    "updated_at = CURRENT_TIMESTAMP WHERE id = ? AND status = 'down'",
                    (call["asset_id"],),
                )
        fresh = db.execute(
            "SELECT * FROM biomedical_service_calls WHERE id = ?", (int(call_id),)
        ).fetchone()
    logger.info("biomedical service call %s closed, downtime %s min", call_id, minutes)
    return _call_view(_row(fresh))


def _call_view(row: dict, now: datetime | None = None) -> dict:
    """One service call, with downtime resolved.

    A call that is still OPEN has no stored downtime — it is still accruing — so
    `downtime_minutes_effective` is computed here from `down_from` to now. That
    is the number a ward wants ("this has been down for 6 hours"), and it is
    labelled `is_running` so nobody stores it back.
    """
    if not row:
        return {}
    out = dict(row)
    stored = row.get("downtime_minutes")
    down = _as_dt(row.get("down_from"), "down_from")
    running = row.get("status") in OPEN_CALL_STATUSES and down is not None
    if running:
        assert down is not None
        current = now or datetime.now(UTC)
        out["downtime_minutes_effective"] = max(0, int((current - down).total_seconds() // 60))
    else:
        out["downtime_minutes_effective"] = int(stored or 0)
    out["downtime_is_running"] = running
    out["is_open"] = row.get("status") in OPEN_CALL_STATUSES
    out["cost_display"] = format_inr_or_unset(
        row.get("cost_paise") if row.get("cost_is_configured") else None
    )
    return out


def asset_downtime_minutes(
    asset_id: int,
    *,
    since: date | None = None,
    until: date | None = None,
    include_open: bool = True,
    now: datetime | None = None,
) -> int:
    """Total downtime for one asset, ACCUMULATED ACROSS EVERY SERVICE CALL.

    This is the figure a hospital argues with a vendor about, so the accumulation
    is the point: three separate two-hour outages are six hours down, not two.
    Open calls contribute their running downtime when `include_open` (the
    default), because a machine that has been down for a week and is still down
    is not zero downtime.

    Filtered by when the outage STARTED (`down_from`), not when it ended, so a
    January outage repaired in February is counted in January — the month the
    ward lost the machine.
    """
    clauses = ["asset_id = ?", "down_from IS NOT NULL", "took_out_of_service = 1"]
    params: list[Any] = [int(asset_id)]
    if since is not None:
        clauses.append("down_from >= ?")
        params.append(f"{since.isoformat()} 00:00:00")
    if until is not None:
        clauses.append("down_from < ?")
        params.append(f"{(until + timedelta(days=1)).isoformat()} 00:00:00")
    with db_session() as db:
        rows = db.execute(
            f"SELECT status, down_from, downtime_minutes FROM biomedical_service_calls "
            f"WHERE {' AND '.join(clauses)}",
            params,
        ).fetchall()
    current = now or datetime.now(UTC)
    total = 0
    for r in rows:
        row = _row(r)
        if row["status"] in OPEN_CALL_STATUSES:
            if not include_open:
                continue
            down = _as_dt(row["down_from"], "down_from")
            assert down is not None
            total += max(0, int((current - down).total_seconds() // 60))
        elif row["status"] == "cancelled":
            # A cancelled call is one that should never have been raised. It did
            # not describe a real outage, so it contributes nothing.
            continue
        else:
            total += int(row["downtime_minutes"] or 0)
    return total


def downtime_summary(
    *,
    since: date | None = None,
    until: date | None = None,
    ward: str | None = None,
    limit: int = 25,
    now: datetime | None = None,
) -> list[dict]:
    """Per-asset downtime over a window, worst first. The list a biomedical
    engineer takes into a vendor review."""
    clauses = ["a.is_active = 1"]
    params: list[Any] = []
    if ward:
        clauses.append("a.ward = ?")
        params.append(ward.strip())
    with db_session() as db:
        assets = db.execute(
            f"SELECT * FROM biomedical_assets a WHERE {' AND '.join(clauses)}", params
        ).fetchall()
    out = []
    for a in assets:
        row = _row(a)
        minutes = asset_downtime_minutes(row["id"], since=since, until=until, now=now)
        if minutes <= 0:
            continue
        out.append(
            {
                "asset_id": row["id"],
                "asset_tag": row["asset_tag"],
                "asset_name": row["name"],
                "ward": row["ward"],
                "department": row["department"],
                "criticality": row["criticality"],
                "status": row["status"],
                "downtime_minutes": minutes,
                "downtime_hours": round(minutes / 60, 1),
            }
        )
    out.sort(key=lambda r: -r["downtime_minutes"])
    return out[: max(1, min(int(limit), 200))]


def get_service_call(call_id: int) -> dict:
    with db_session() as db:
        row = db.execute(
            "SELECT * FROM biomedical_service_calls WHERE id = ?", (int(call_id),)
        ).fetchone()
    return _call_view(_row(row))


def list_service_calls(
    *,
    asset_id: int | None = None,
    ward: str | None = None,
    status: str | None = None,
    open_only: bool = False,
    limit: int = 100,
) -> list[dict]:
    clauses = ["1=1"]
    params: list[Any] = []
    if asset_id is not None:
        clauses.append("c.asset_id = ?")
        params.append(int(asset_id))
    if ward:
        clauses.append("(c.ward = ? OR a.ward = ?)")
        params.extend([ward.strip(), ward.strip()])
    if status:
        clauses.append("c.status = ?")
        params.append(_one_of(status, SERVICE_CALL_STATUSES, "status"))
    if open_only:
        clauses.append(f"c.status IN ({','.join('?' * len(OPEN_CALL_STATUSES))})")
        params.extend(sorted(OPEN_CALL_STATUSES))
    with db_session() as db:
        rows = db.execute(
            f"SELECT c.*, a.asset_tag, a.name AS asset_name, a.criticality "
            f"FROM biomedical_service_calls c "
            f"JOIN biomedical_assets a ON a.id = c.asset_id "
            f"WHERE {' AND '.join(clauses)} ORDER BY c.reported_at DESC, c.id DESC LIMIT ?",
            [*params, max(1, min(int(limit), 500))],
        ).fetchall()
    return [_call_view(_row(r)) for r in rows]


# ═════════════════════════════════════════════════════════════════════════════
#  AMC / CMC contracts
# ═════════════════════════════════════════════════════════════════════════════


def upsert_contract(
    *,
    contract_number: str,
    contract_type: str = "amc",
    start_date: date | str,
    end_date: date | str,
    vendor_id: int | None = None,
    vendor_name: str | None = None,
    value_paise: int | None = None,
    notice_days: int = DEFAULT_NOTICE_DAYS,
    covers_calibration: bool = False,
    covers_parts: bool = False,
    response_hours: int | None = None,
    status: str = "active",
    scope_notes: str | None = None,
    asset_ids: list[int] | None = None,
    asset_tags: list[str] | None = None,
    actor: str | None = None,
) -> dict:
    """Create or update an AMC/CMC/warranty contract, keyed on `contract_number`.

    `asset_ids` / `asset_tags` REPLACE the coverage list when supplied and leave
    it alone when omitted (None) — so "update the value" does not silently
    uncover a fleet. An explicit empty list clears the coverage, which is a
    different and deliberate act.
    """
    number = _clean(contract_number, field_name="contract_number", required=True)
    ctype = _one_of(contract_type, CONTRACT_TYPES, "contract_type", "amc")
    cstatus = _one_of(status, CONTRACT_STATUSES, "status", "active")
    start = _require_date(start_date, "start_date")
    end = _require_date(end_date, "end_date")
    if end < start:
        raise BiomedicalError("end_date is before start_date", code="bad_date_order")
    notice = _non_negative_int(notice_days, "notice_days")
    value, value_configured = _optional_paise(value_paise, "value_paise")

    with db_session() as db:
        db.execute(
            """INSERT INTO biomedical_contracts (
                   contract_number, contract_type, vendor_id, vendor_name,
                   start_date, end_date, value_paise, value_is_configured,
                   notice_days, covers_calibration, covers_parts, response_hours,
                   status, scope_notes, created_by)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(contract_number) DO UPDATE SET
                   contract_type = excluded.contract_type,
                   vendor_id = excluded.vendor_id,
                   vendor_name = excluded.vendor_name,
                   start_date = excluded.start_date,
                   end_date = excluded.end_date,
                   value_paise = excluded.value_paise,
                   value_is_configured = excluded.value_is_configured,
                   notice_days = excluded.notice_days,
                   covers_calibration = excluded.covers_calibration,
                   covers_parts = excluded.covers_parts,
                   response_hours = excluded.response_hours,
                   status = excluded.status,
                   scope_notes = excluded.scope_notes,
                   updated_at = CURRENT_TIMESTAMP""",
            (
                number,
                ctype,
                int(vendor_id) if vendor_id is not None else None,
                _clean(vendor_name, field_name="vendor_name"),
                start.isoformat(),
                end.isoformat(),
                value,
                value_configured,
                notice,
                1 if covers_calibration else 0,
                1 if covers_parts else 0,
                int(response_hours) if response_hours is not None else None,
                cstatus,
                _clean(scope_notes, field_name="scope_notes"),
                actor,
            ),
        )
        row = db.execute(
            "SELECT * FROM biomedical_contracts WHERE contract_number = ?", (number,)
        ).fetchone()
        contract_id = int(row["id"])

    if asset_ids is not None or asset_tags is not None:
        link_contract_assets(
            contract_id=contract_id,
            asset_ids=asset_ids or [],
            asset_tags=asset_tags or [],
            replace=True,
        )
    return get_contract(contract_id)


def link_contract_assets(
    *,
    contract_id: int,
    asset_ids: list[int] | None = None,
    asset_tags: list[str] | None = None,
    replace: bool = False,
) -> dict:
    """Attach assets to a contract. Every id/tag must resolve — a coverage list
    that silently dropped the one machine somebody mistyped is worse than a
    refusal, because the gap only surfaces when the vendor declines a call."""
    with db_session() as db:
        exists = db.execute(
            "SELECT id FROM biomedical_contracts WHERE id = ?", (int(contract_id),)
        ).fetchone()
        if exists is None:
            raise BiomedicalError(f"no contract {contract_id}", code="not_found")
        resolved: list[int] = []
        for aid in asset_ids or []:
            resolved.append(_resolve_asset_id(db, asset_id=int(aid)))
        for tag in asset_tags or []:
            resolved.append(_resolve_asset_id(db, asset_tag=tag))
        if replace:
            db.execute(
                "DELETE FROM biomedical_contract_assets WHERE contract_id = ?", (int(contract_id),)
            )
        for aid in dict.fromkeys(resolved):
            db.execute(
                "INSERT OR IGNORE INTO biomedical_contract_assets (contract_id, asset_id) "
                "VALUES (?,?)",
                (int(contract_id), aid),
            )
    return get_contract(contract_id)


def _contract_view(row: dict, today: date | None = None) -> dict:
    if not row:
        return {}
    day = _today(today)
    end = as_date(row.get("end_date"), "end_date")
    notice = int(row.get("notice_days") or DEFAULT_NOTICE_DAYS)
    out = dict(row)
    out["value_display"] = format_inr_or_unset(
        row.get("value_paise") if row.get("value_is_configured") else None
    )
    expired = end is not None and contract_is_expired(end, day)
    out["is_expired"] = expired
    out["days_remaining"] = contract_days_remaining(end, day) if end else None
    out["in_notice_window"] = end is not None and contract_in_notice_window(end, notice, day)
    # A contract someone TERMINATED is not "expiring soon", whatever its dates
    # say. Status wins over arithmetic here because a human made a decision.
    if row.get("status") in ("terminated", "renewed"):
        out["cover_state"] = row["status"]
    elif expired:
        out["cover_state"] = "expired"
    elif out["in_notice_window"]:
        out["cover_state"] = "expiring"
    else:
        out["cover_state"] = "active"
    return out


def get_contract(contract_id: int, today: date | None = None) -> dict:
    with db_session() as db:
        row = db.execute(
            "SELECT * FROM biomedical_contracts WHERE id = ?", (int(contract_id),)
        ).fetchone()
        if row is None:
            return {}
        assets = db.execute(
            "SELECT a.id, a.asset_tag, a.name, a.ward, a.criticality "
            "FROM biomedical_contract_assets ca "
            "JOIN biomedical_assets a ON a.id = ca.asset_id "
            "WHERE ca.contract_id = ? ORDER BY a.asset_tag",
            (int(contract_id),),
        ).fetchall()
    view = _contract_view(_row(row), _today(today))
    view["assets"] = [_row(a) for a in assets]
    view["asset_count"] = len(view["assets"])
    return view


def list_contracts(
    *, status: str | None = None, contract_type: str | None = None, today: date | None = None
) -> list[dict]:
    clauses = ["1=1"]
    params: list[Any] = []
    if status:
        clauses.append("status = ?")
        params.append(_one_of(status, CONTRACT_STATUSES, "status"))
    if contract_type:
        clauses.append("contract_type = ?")
        params.append(_one_of(contract_type, CONTRACT_TYPES, "contract_type"))
    with db_session() as db:
        rows = db.execute(
            f"SELECT c.*, (SELECT COUNT(*) FROM biomedical_contract_assets ca "
            f"              WHERE ca.contract_id = c.id) AS asset_count "
            f"FROM biomedical_contracts c WHERE {' AND '.join(clauses)} "
            f"ORDER BY c.end_date",
            params,
        ).fetchall()
    day = _today(today)
    return [_contract_view(_row(r), day) for r in rows]


def expiring_contracts(*, today: date | None = None, within_days: int | None = None) -> dict:
    """Contracts inside their own notice window, plus the ones already expired.

    Each contract carries its OWN `notice_days`, because a 90-day CT scanner AMC
    and a 30-day autoclave AMC need different lead times and a single hospital
    setting would be wrong for both. `within_days` overrides it for a caller who
    wants a fixed horizon ("what is up in the next 30 days").
    """
    day = _today(today)
    rows = list_contracts(today=day)
    live = [r for r in rows if r.get("status") not in ("terminated", "renewed")]
    if within_days is None:
        expiring = [r for r in live if r["cover_state"] == "expiring"]
    else:
        horizon = _non_negative_int(within_days, "within_days")
        expiring = [
            r for r in live if not r["is_expired"] and (r["days_remaining"] or 0) <= horizon
        ]
    expired = [r for r in live if r["is_expired"]]
    expiring.sort(key=lambda r: r["days_remaining"] if r["days_remaining"] is not None else 9999)
    expired.sort(key=lambda r: r["days_remaining"] if r["days_remaining"] is not None else 0)
    return {
        "as_of": day.isoformat(),
        "expiring": expiring,
        "expiring_count": len(expiring),
        "expired": expired,
        "expired_count": len(expired),
    }


# ═════════════════════════════════════════════════════════════════════════════
#  Composite reads
# ═════════════════════════════════════════════════════════════════════════════


def biomedical_overview(*, today: date | None = None) -> dict:
    """The department's first screen, ordered by what would hurt.

    Overdue calibration first, then equipment that is down, then contracts, then
    the coverage gap — which is last on the screen but is the one that can make
    every number above it look better than the truth.
    """
    day = _today(today)
    due = due_register(today=day)
    contracts = expiring_contracts(today=day)
    gaps = coverage_gaps(today=day)
    open_calls = list_service_calls(open_only=True, limit=100)
    with db_session() as db:
        totals = _row(
            db.execute(
                "SELECT COUNT(*) AS total, "
                "SUM(CASE WHEN status = 'down' THEN 1 ELSE 0 END) AS down, "
                "SUM(CASE WHEN status = 'under_maintenance' THEN 1 ELSE 0 END) AS maintenance, "
                "SUM(CASE WHEN criticality = 'life_support' THEN 1 ELSE 0 END) AS life_support, "
                "SUM(CASE WHEN produces_clinical_readings = 1 THEN 1 ELSE 0 END) AS measuring "
                "FROM biomedical_assets WHERE is_active = 1"
            ).fetchone()
        )
    return {
        "as_of": day.isoformat(),
        "assets_total": int(totals.get("total") or 0),
        "assets_down": int(totals.get("down") or 0),
        "assets_under_maintenance": int(totals.get("maintenance") or 0),
        "life_support_assets": int(totals.get("life_support") or 0),
        "measuring_assets": int(totals.get("measuring") or 0),
        "overdue": due["overdue"][:25],
        "overdue_count": due["overdue_count"],
        "overdue_calibration_count": due["overdue_calibration_count"],
        "overdue_life_support_count": due["overdue_life_support_count"],
        "measurement_at_risk_count": due["measurement_at_risk_count"],
        "never_done_count": due["never_done_count"],
        "due_soon": due["due_soon"][:25],
        "due_soon_count": due["due_soon_count"],
        "open_calls": open_calls[:25],
        "open_call_count": len(open_calls),
        "contracts_expiring": contracts["expiring"][:25],
        "contracts_expiring_count": contracts["expiring_count"],
        "contracts_expired_count": contracts["expired_count"],
        "assets_with_no_schedule": gaps[:25],
        "assets_with_no_schedule_count": len(gaps),
        "downtime_top": downtime_summary(since=day - timedelta(days=30), limit=10),
    }


def ward_attention(ward: str, *, today: date | None = None) -> dict:
    """What a WARD needs to know about its own equipment.

    Deliberately narrow. A ward does not want the register; it wants three
    sentences: which of our machines is out of calibration, which is down, and
    which is about to fall due. Anything more and the screen gets skimmed, which
    is the same as not having it.

    NO PHI ANYWHERE IN THIS PAYLOAD — asset tags, ward names and dates only. That
    is what lets it be readable by every clinical role rather than only by the
    people who can see the whole equipment register.
    """
    day = _today(today)
    ward = _clean(ward, field_name="ward", required=True)
    due = due_register(ward=ward, today=day)
    assets = list_assets(ward=ward, today=day)
    down = [a for a in assets if a.get("status") in UNAVAILABLE_STATUSES]
    open_calls = list_service_calls(ward=ward, open_only=True, limit=50)
    return {
        "ward": ward,
        "as_of": day.isoformat(),
        "asset_count": len(assets),
        "overdue": due["overdue"],
        "overdue_count": due["overdue_count"],
        "overdue_calibration_count": due["overdue_calibration_count"],
        "measurement_at_risk_count": due["measurement_at_risk_count"],
        "due_soon": due["due_soon"],
        "due_soon_count": due["due_soon_count"],
        "unavailable": down,
        "unavailable_count": len(down),
        "open_calls": open_calls,
        "open_call_count": len(open_calls),
        # The one sentence the ward is actually being told. Computed here so the
        # panel, a future Relay card and any report all say the same thing.
        "headline": _ward_headline(due, down),
    }


def _ward_headline(due: dict, unavailable: list[dict]) -> str:
    parts = []
    if due["overdue_calibration_count"]:
        parts.append(
            f"{due['overdue_calibration_count']} device"
            f"{'s' if due['overdue_calibration_count'] != 1 else ''} overdue for calibration"
        )
    other_overdue = due["overdue_count"] - due["overdue_calibration_count"]
    if other_overdue > 0:
        parts.append(f"{other_overdue} overdue for maintenance")
    if unavailable:
        parts.append(f"{len(unavailable)} out of service")
    if not parts:
        return "No equipment on this ward is overdue or out of service."
    return "; ".join(parts).capitalize() + "."
