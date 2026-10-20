"""Orb — the FRONT DESK: outpatient visits, the consulting queue, and TRANSFERS.

Two jobs live here because one desk does both of them:

  1. OPD.  A walk-in or booked outpatient is registered against a consulting
     doctor, gets a token, waits, is called, is seen, and the consultation is
     CHARGED through the billing spine (`encounter_charges`) — this module does
     not invent an invoice, a rounding rule or a price.

  2. TRANSFER.  Orb had `admit` and `discharge` and nothing in between, so a
     patient moved from Cardiology to the ICU today leaves NO record of the move
     at all: somebody edits a ward label and the previous location is gone.
     `transfer_patient` makes the move a first-class, audited event with a
     from/to, a timestamp, a reason and a named actor.

════════════════════════════════════════════════════════════════════════════════
 A TRANSFER CHANGES WHO CAN SEE THE PATIENT. READ THIS BEFORE EDITING.
════════════════════════════════════════════════════════════════════════════════
Ward/department is not decoration in Orb. It is the ACCESS BOUNDARY. The one
derivation everything shares is `permissions.PATIENT_DEPARTMENT_SQL`:

    the department of the patient's ACTIVE admission
    (falling back to their newest admission when none is active)

compared against `permissions.department_scope(user)` — the department on the
AUTHENTICATED user's row. Vigil's patient board, the nurse shift board, the Sage
lane, Relay's case rooms, the search surface and every `/{patient_id}` route hang
off that comparison.

So a transfer is a permission change wearing a logistics change's clothes. Three
rules follow, and all three are enforced below and tested:

  A. THE DERIVATION IS NOT DUPLICATED. A transfer UPDATES
     `admissions.department/ward/bed` on the patient's existing active admission.
     It does not add a second location column, a "current_ward" cache or a
     movement-derived scope. The movement row is a LEDGER, never a scope source —
     the same rule `clinical_escalations.raised_in_department` carries.

  B. THE ADMISSION IS PRESERVED. Same admission id, `status` stays `'active'`,
     and `patients.status` stays `'admitted'`. This is the trap: both columns
     have a `'transferred'` value in their CHECK constraint, and setting either
     one means "this patient has LEFT" to every board in the system
     (`WHERE p.status = 'admitted'`, `JOIN admissions a ON a.status = 'active'`).
     A ward transfer that sets status='transferred' deletes the patient from the
     nurse board, from Vigil and from the eMAR. It stays active.

  C. A TRANSFER MAY NEVER ORPHAN A PATIENT. Because scope is derived from the
     department string, a typo ("Cardiolgy") or an invented ward ("Ward 7") does
     not fail loudly — it produces a patient that NO scoped account can see, on a
     ward that does not exist, still admitted, still being scored by Vigil, with
     nobody's board showing them. That is the worst outcome this file can
     produce, so the destination is resolved against
     `list_covering_departments()` — the departments that at least one active
     clinical account actually covers — and an unmatched destination is REFUSED.

VISIBILITY POLICY IMPLEMENTED: STRICT HANDOVER. The instant the transfer commits,
the receiving ward's staff can see the patient and the sending ward's staff
cannot. That is not a separate policy — it is the direct consequence of rule A,
because the scope is derived live from the admission row rather than snapshotted.
A grace window for the sending ward would require a SECOND source of truth for
"who may see this patient", which is precisely the shape of the 2026-08-05 leak.
If a site wants one, it belongs in `permissions`, applied to every surface at
once, and it is a champion decision — recorded as a followup, not smuggled in
here.

CONVENTIONS
  * Every function is PURE SYNC and opens its own `db_session()`. Async callers
    go through `database.run_db` (tests/test_event_loop_discipline.py).
  * All money arithmetic goes through services/money.py; all prices come from
    services/billing_masters.resolve_rate. There is no rounding in this file.
  * A price nobody configured is NOT ₹0: the charge row carries
    `rate_is_configured = 0` and zero totals, and the UI must say so.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

import config
import migrations  # noqa: F401  (loads migrations_his in version order first)
from database import db_session
from migrations_his.m0050_registration_opd_foundation import (
    OPD_VISITS_ADDED_COLUMNS,
    ensure_opd_visits_on,
)
from migrations_his.m0051_adt_movement_ledger import (
    ADDED_COLUMNS as _LEDGER_ADDED_COLUMNS,
)
from migrations_his.m0051_adt_movement_ledger import ensure_ledger_on
from migrations_his.m0074_facility_columns_encounters import ensure_opd_visit_facility_on

from services.billing_masters import resolve_patient_payer
from services.hospital_time import hospital_timezone, hospital_today

logger = logging.getLogger("orb.frontdesk")

__all__ = [
    "FrontDeskError",
    "CrossFacilityMoveRefused",
    "OPD_CONSULTATION_SERVICE_CODE",
    "ensure_frontdesk_schema",
    "list_covering_departments",
    "list_transfer_destinations",
    "register_opd_visit",
    "list_opd_queue",
    "get_opd_visit",
    "start_opd_consultation",
    "complete_opd_visit",
    "cancel_opd_visit",
    "transfer_patient",
    "list_patient_movements",
    "resolve_covering_department",
    "uncovered_department_words",
]


class FrontDeskError(ValueError):
    """A front-desk request that cannot be honoured (bad ward, bad state)."""


class CrossFacilityMoveRefused(FrontDeskError):
    """multisite-3 (W10-B): a move into another facility's department while
    more than one facility is on record. Raised before anything is written,
    after the authorization 'cross_facility_move_refused' row; the transfer
    route answers 409 with `code`."""

    code = "cross_facility_move_refused"


class OpdReplayConflict(FrontDeskError):
    """OPD mode only: an idempotency key that already names a visit for a
    DIFFERENT patient or clinic than the request names.

    Raised by register_opd_visit(on_replay=...) before that visit is serialized,
    so nothing about it reaches the caller. The message carries no patient
    detail (a caller that does not know this subclass maps it to a plain 422);
    `visit_id` is for the route's audit row only, never for the response.
    """

    def __init__(self, visit_id: str):
        super().__init__("This registration key already belongs to a different registration.")
        self.visit_id = visit_id


#: The service item a consultation is charged against. This module REGISTERS
#: the row itself (unpriced) in ensure_frontdesk_schema, so the code the front
#: desk charges is always visible on the billing price list; a site prices it
#: there (or adds `tariff_rates` rows per plan). Until it does, `resolve_rate`
#: reports it unconfigured and the charge is raised UNPRICED rather than at ₹0.
#: Callers may name a different code (a department-specific consultation item,
#: a review vs new-case item) — those are the site's own rows.
OPD_CONSULTATION_SERVICE_CODE = "OPD-CONSULT"

#: Free-text caps. These columns are unbounded TEXT and the front desk is the
#: least-trusted keyboard in the hospital.
_MAX_REASON_LEN = 2000
_MAX_COMPLAINT_LEN = 2000
_MAX_LABEL_LEN = 120

_OPD_STATUSES = ("waiting", "in_consultation", "completed", "cancelled")

#: Roles whose `users.department` constitutes ward COVER. A pharmacist or an
#: administrator having a department string does not mean that ward has clinical
#: staff on it, and admitting a patient into a ward covered only by the pharmacy
#: is the orphaning failure with extra steps.
_COVERING_ROLES = ("doctor", "clinician", "nurse")


# ═════════════════════════════════════════════════════════════════════════════
#  Schema
# ═════════════════════════════════════════════════════════════════════════════
# Both tables now live in numbered migrations: opd_visits in migration 50
# (server/migrations_his/m0050_registration_opd_foundation.py) and
# patient_movements in migration 51 (m0051_adt_movement_ledger.py). Each file
# holds the frozen DDL and indexes; this module imports them, so the lazy path
# below and the migration runner build one shape. They were created here, not
# in a migration, because migration 20 was authored as one migration for five
# modules built in parallel; that followup is now done.
#
# Same rules as migration 20: additive, ZERO foreign keys (an FK from a front-desk
# row to `patients` would make a patient row undeletable and could fail a clinical
# table rebuild), no REAL columns, money in `_paise`.

#: The columns migrations 50 and 51 added. The front desk's API leaves them out
#: of every visit and movement it returns, so those migrations change no
#: existing screen; the increments that write them extend the API under their
#: own feature switches.
_VISIT_COLUMNS_NOT_IN_API: frozenset[str] = frozenset(
    name for name, _ in OPD_VISITS_ADDED_COLUMNS
) | {"facility_id"}  # migration 74 (multisite-4): the visit's facility
_MOVEMENT_COLUMNS_NOT_IN_API: frozenset[str] = frozenset(_LEDGER_ADDED_COLUMNS)

_schema_ready = False
_schema_lock = threading.Lock()


def ensure_frontdesk_schema(force: bool = False) -> None:
    """Create the two front-desk tables if absent, and register this module's
    consultation service item on the billing price list (unpriced, never
    overwriting a row the site has touched). Idempotent, cached, thread-safe.

    An idempotent FALLBACK: init_db's migrations 50 and 51 create both tables
    at boot, so on a migrated database this only ensures they are there. It
    shares their DDL, and it never rebuilds an old-shape movement ledger (only
    migration 51 does that, inside the runner's transaction).

    Lazy rather than at import: `DB_PATH` is bound at import time from config and
    the test harness rewrites it before `database` is first imported — opening a
    connection at module import would race that, and would open a SQLCipher
    handle during interpreter start-up on the event loop's thread.
    """
    global _schema_ready
    if _schema_ready and not force:
        return
    with _schema_lock:
        if _schema_ready and not force:
            return
        with db_session() as db:
            ensure_opd_visits_on(db)
            # Migration 74's opd_visits.facility_id, so the lazy shape is the
            # runner's shape (multisite-4).
            ensure_opd_visit_facility_on(db)
            ensure_ledger_on(db, rebuild=False)
            # Register the code this module charges against as a PRICE-LIST row,
            # unpriced. Without this row the contract is invisible: the billing
            # counter cannot price a code it has never seen, so the site prices
            # a hand-made lookalike and every consultation charge sits at "Not
            # configured" forever. OR IGNORE, keyed on service_items.code UNIQUE:
            # once a site has touched the row (priced it, renamed it, retired
            # it), a restart must never write over that.
            db.execute(
                """INSERT OR IGNORE INTO service_items
                       (code, name, category, uom, gst_treatment, gst_rate_bp,
                        default_rate_paise, rate_is_configured, is_active, notes)
                   VALUES (?, 'OPD consultation', 'consultation', 'each',
                           'exempt', 0, 0, 0, 1, ?)""",
                (
                    OPD_CONSULTATION_SERVICE_CODE,
                    "Raised automatically when an OPD visit is completed. "
                    "Set the rate here; the front desk never invents one.",
                ),
            )
        _schema_ready = True


# ═════════════════════════════════════════════════════════════════════════════
#  Small helpers
# ═════════════════════════════════════════════════════════════════════════════


def _now() -> str:
    """An INSTANT, stored UTC. Never a day key — see `_hospital_day`."""
    return datetime.now(UTC).isoformat()


def _hospital_day(value: object = None) -> str:
    """The HOSPITAL-LOCAL calendar day (`YYYY-MM-DD`) of an instant, or today's.

    THE FRONT DESK DECIDES DATES, AND `str(<utc instant>)[:10]` IS THE WRONG ONE.
    Orb stores every timestamp in UTC; a hospital at UTC+05:30 spends 00:00–05:30
    every day on the PREVIOUS UTC date. Slicing a UTC instant therefore dated a
    2 a.m. consultation YESTERDAY on the patient's bill, and — worse, because it
    is money and it is silent — looked yesterday's tariff up for it: a rate that
    starts today is simply "not in force" at 02:00, so the charge fell back to
    the item default or to `rate_is_configured = 0`.

    One resolver, `services/hospital_time.py`, shared with the surgical Today
    badge and the Forecast day-0 row, so the desk cannot disagree with them about
    what day it is.

    A value that is ALREADY a bare date is returned as-is: it was never an
    instant, and converting it would shift it a day for free. A value that will
    not parse degrades to its first ten characters rather than raising — the
    front desk must never fail to register a patient over a date format.
    """
    if value is None:
        return hospital_today().isoformat()
    text = str(value).strip()
    if not text:
        return hospital_today().isoformat()
    if len(text) <= 10:
        return text[:10]
    parsed = _parse_ts(text.replace(" ", "T", 1))
    if parsed is None:
        return text[:10]
    return parsed.astimezone(hospital_timezone()).date().isoformat()


def _clean(value: object, limit: int) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return text[:limit]


def _row_to_dict(row) -> dict:
    return dict(row) if row is not None else {}


def _visit_api_dict(row) -> dict:
    """A visit row as the API has always returned it: the migration 50 columns
    left out, every other key in its original order."""
    return {k: v for k, v in _row_to_dict(row).items() if k not in _VISIT_COLUMNS_NOT_IN_API}


def _movement_api_dict(row) -> dict:
    """A movement row as the API has always returned it: the migration 51
    columns left out, every other key in its original order."""
    return {k: v for k, v in _row_to_dict(row).items() if k not in _MOVEMENT_COLUMNS_NOT_IN_API}


# ═════════════════════════════════════════════════════════════════════════════
#  Ward cover — the anti-orphaning list
# ═════════════════════════════════════════════════════════════════════════════


def list_covering_departments(
    facility_id: int | None = None, *, facilities: frozenset | None = None
) -> list[str]:
    """Every department at least one ACTIVE clinical account covers.

    THIS IS THE ANTI-ORPHANING LIST, and it is derived from exactly the column
    `permissions.department_scope` reads (`users.department`) so the two answers
    cannot drift. A department absent from this list is a department whose
    patients would appear on nobody's board.

    Accounts with NO department are excluded on purpose: by the department_scope
    convention a blank department means "hospital-wide governance account", not
    "covers every ward" — such an account reading across wards is not the same
    thing as a ward having staff on it.

    FACILITIES (multisite-3, W10-B). With one facility on record (every site
    today) the list is exactly as before, and every department is facility
    1's, so `facility_id` other than 1, or `facilities` without 1, gives [].
    With more than one facility on record (ruling MS-R4 (a): a department name
    is unique across the group and owned by one facility, in the departments
    master), a department the master does not list, or lists as inactive, is
    left out: no facility owns it, so its staff see no patient and a patient
    put there would be on nobody's board. `facility_id` then keeps one
    facility's departments and `facilities` (a facility scope, None for every
    facility) those of a set.
    """
    from services import facility_service

    ensure_frontdesk_schema()
    marks = ",".join("?" for _ in _COVERING_ROLES)
    with db_session() as db:
        rows = db.execute(
            f"""SELECT DISTINCT TRIM(department) AS department
                  FROM users
                 WHERE role IN ({marks})
                   AND department IS NOT NULL
                   AND TRIM(department) != ''
                   AND COALESCE(is_active, 1) = 1
                 ORDER BY 1""",
            _COVERING_ROLES,
        ).fetchall()
        names = [r["department"] for r in rows]
        if not facility_service.multi_facility_data(db=db):
            main = facility_service.MAIN_FACILITY_ID
            if (facility_id is not None and facility_id != main) or (
                facilities is not None and main not in facilities
            ):
                return []
            return names
        master = _department_master_on(db)
    kept = []
    for name in names:
        owner = master.get(name.strip().casefold())
        if owner is None or not owner[1]:
            continue
        if facility_id is not None and owner[0] != facility_id:
            continue
        if facilities is not None and owner[0] not in facilities:
            continue
        kept.append(name)
    return kept


def _department_master_on(db) -> dict:
    """{name ignoring case and outer spaces: (facility id, active)} from the
    departments master (migration 70); {} when the table is absent."""
    if (
        db.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'departments'"
        ).fetchone()
        is None
    ):
        return {}
    return {
        r["name"].strip().casefold(): (int(r["facility_id"]), bool(r["is_active"]))
        for r in db.execute("SELECT name, facility_id, is_active FROM departments").fetchall()
    }


def _resolve_covering_department(
    name: str, *, facility_id: int | None = None, facilities: frozenset | None = None
) -> str | None:
    """Map a typed department onto its CANONICAL covered spelling, or None.

    Case- and whitespace-insensitive, because a clerk typing "cardiology" must
    not create an invisible ward that differs from "Cardiology" by one byte. The
    value STORED is always the canonical string from `users.department`, so the
    equality test in PATIENT_DEPARTMENT_SQL matches exactly.

    `facility_id` and `facilities` narrow the list as in
    list_covering_departments (multisite-3); with neither, a department of any
    facility resolves.
    """
    wanted = (name or "").strip().casefold()
    if not wanted:
        return None
    for canonical in list_covering_departments(facility_id, facilities=facilities):
        if canonical.casefold() == wanted:
            return canonical
    return None


#: Public name for the anti-orphaning resolver (adt-6a): the ADT transfer
#: writer (services/adt/transfer.py) resolves a destination with the same rule,
#: before its immediate session opens, exactly as transfer_patient does.
resolve_covering_department = _resolve_covering_department


def uncovered_department_words(to_department: object) -> str:
    """Rule C's refusal, in the words both transfer lanes give: the front
    desk's own lane (transfer_patient) and the ADT writer it delegates to."""
    return (
        f"{to_department!r} is not a department with clinical cover. "
        "Transferring there would leave the patient admitted on a ward that "
        "appears on nobody's board."
    )


# ═════════════════════════════════════════════════════════════════════════════
#  Moves between facilities (multisite-3, W10-B)
# ═════════════════════════════════════════════════════════════════════════════
# Orb works out which facility a chart belongs to from the patient's
# admissions (permissions.FACILITY_PATIENT_SQL), and that is exact only while
# all of a patient's admissions are at one facility. So while more than one
# facility is on record, every lane that admits a patient or changes the
# department of an admission refuses a department of another facility than
# the one the patient's admissions are at (the design's attribution
# invariant; ruling MS-R3 stays at (c), refuse, until it and R009 are ruled).
# A refusal writes an authorization 'cross_facility_move_refused' row and
# changes nothing. With one facility on record none of this reads anything.

CROSS_FACILITY_TRANSFER_WORDS = (
    "That department belongs to another facility of the group. Orb does not move a "
    "patient between facilities yet, so nothing was changed. Choose a department of "
    "the facility the patient is at."
)


def patient_facility_ids_on(db, patient_id: str) -> set:
    """The facilities this patient's admissions, active or not, are at."""
    return {
        int(r[0])
        for r in db.execute(
            "SELECT DISTINCT facility_id FROM admissions WHERE patient_id = ? "
            "AND facility_id IS NOT NULL",
            (patient_id,),
        ).fetchall()
    }


#: register_opd_visit's refusal for a clinic no facility owns (multi-facility
#: data only; the covering list already leaves such a clinic out).
CLINIC_WITHOUT_FACILITY_WORDS = (
    "No facility of the group lists that clinic, so a visit there would be on "
    "nobody's queue. Nothing was saved. Ask an administrator to add it to the "
    "departments list."
)


def opd_visit_facility_on(db: Any, department: str) -> int:
    """The facility an outpatient visit to `department` is at (multisite-4,
    migration 74's opd_visits.facility_id). Facility 1 while one facility is
    on record (nothing beyond the cached count is read). Otherwise the
    facility that owns the clinic in the departments master: under ruling
    MS-R4 (a) a department name is unique across the group and owned by one
    facility, so the clinic decides, never the desk. FrontDeskError when no
    active master row owns it."""
    from services import facility_service

    if not facility_service.multi_facility_data(db=db):
        return facility_service.MAIN_FACILITY_ID
    owner = department_facility_on(db, department)
    if owner is None:
        raise FrontDeskError(CLINIC_WITHOUT_FACILITY_WORDS)
    return owner


def department_facility_on(db: Any, department: str | None) -> int | None:
    """The facility owning an ACTIVE departments master row for `department`
    (ignoring case and outer spaces), read on the caller's connection rather
    than from the scope maps' 30 s cache; None when no active row lists it."""
    owner = _department_master_on(db).get((department or "").strip().casefold())
    if owner is None or not owner[1]:
        return None
    return owner[0]


def audit_cross_facility_refusal(
    *,
    lane: str,
    cause: str,
    patient_id: str | None,
    actor_id: str | None,
    actor_role: str | None,
    to_department: str | None,
    to_facility_id: int | None,
    from_facility_ids,
    admission_id: str | None = None,
    request_meta: dict | None = None,
) -> None:
    """The authorization row for a refused cross-facility move or admission.
    Written on a connection of its own after nothing was written (fail-soft,
    like every authorization row). BLOCKING."""
    from services.audit_service import log_audit_event

    meta = request_meta or {}
    log_audit_event(
        correlation_id=str(uuid.uuid4()),
        event_category="authorization",
        action_type="cross_facility_move_refused",
        outcome="denied",
        summary_message=(
            f"Refused {lane.replace('_', ' ')} of {patient_id or 'a new patient'} "
            f"into {to_department!r}: {cause.replace('_', ' ')}"
        ),
        actor_user_id=actor_id,
        actor_role=actor_role,
        patient_id=patient_id,
        target_type="admission" if admission_id else "patient",
        target_id=admission_id or patient_id,
        request_path=meta.get("request_path"),
        http_method=meta.get("http_method"),
        ip_address=meta.get("ip_address"),
        user_agent=meta.get("user_agent"),
        metadata={
            "lane": lane,
            "cause": cause,
            "to_department": to_department,
            "to_facility_id": to_facility_id,
            "from_facility_ids": sorted(from_facility_ids or ()),
        },
    )


def _refuse_cross_facility_transfer(patient_id: str, department: str, actor_id: str | None) -> None:
    """transfer_patient's facility check, before its immediate session:
    CrossFacilityMoveRefused (after the audit row) when the destination's
    facility is not the one the patient's admissions are at. The facility of
    an admission never changes and a department in use cannot change
    facility (services/department_master.py), so the answer holds through the
    write."""
    from services import facility_service

    if not facility_service.multi_facility_data():
        return
    to_facility = facility_service.department_facility(department)
    with db_session() as db:
        admission = db.execute(
            """SELECT id FROM admissions WHERE patient_id = ? AND status = 'active'
                ORDER BY created_at DESC, rowid DESC LIMIT 1""",
            (patient_id,),
        ).fetchone()
        if admission is None:
            return  # transfer_patient refuses it in its own words
        held = patient_facility_ids_on(db, patient_id)
        role_row = (
            db.execute("SELECT role FROM users WHERE id = ?", (actor_id,)).fetchone()
            if actor_id
            else None
        )
    if to_facility is not None and held <= {to_facility}:
        return
    audit_cross_facility_refusal(
        lane="frontdesk_transfer",
        cause="department_not_listed" if to_facility is None else "patient_at_another_facility",
        patient_id=patient_id,
        actor_id=actor_id,
        actor_role=role_row["role"] if role_row else None,
        to_department=department,
        to_facility_id=to_facility,
        from_facility_ids=held,
        admission_id=admission["id"],
    )
    raise CrossFacilityMoveRefused(CROSS_FACILITY_TRANSFER_WORDS)


def list_transfer_destinations(*, facilities: frozenset[int] | None = None) -> list[dict]:
    """Where a patient may be transferred TO, with the wards/beds in use there.

    The department list is the covering list (above) — it is the set of legal
    destinations, not a suggestion. `wards` is a convenience for the UI, gathered
    from the ward labels already in use on active admissions in that department;
    it is free text and is NOT validated on transfer (a genuinely new bay in a
    covered department is legitimate). The DEPARTMENT is what is validated,
    because the department is what carries the access boundary.

    `facilities` is the caller's permissions.facility_scope (multisite-9a): a
    set keeps the departments those facilities own and counts only their
    admissions; None (one facility on record) changes nothing.
    """
    from services.facility_documents import facility_clause

    ensure_frontdesk_schema()
    departments = list_covering_departments(facilities=facilities)
    if not departments:
        return []
    facility_sql, facility_params = facility_clause("a.facility_id", facilities)
    with db_session() as db:
        rows = db.execute(
            f"""SELECT a.department AS department, a.ward AS ward, COUNT(*) AS occupied
                 FROM admissions a
                WHERE a.status = 'active' AND a.ward IS NOT NULL AND TRIM(a.ward) != ''{facility_sql}
                GROUP BY a.department, a.ward""",
            facility_params,
        ).fetchall()
        staff = db.execute(
            f"""SELECT TRIM(department) AS department, COUNT(*) AS staff_count
                  FROM users
                 WHERE role IN ({",".join("?" for _ in _COVERING_ROLES)})
                   AND department IS NOT NULL AND TRIM(department) != ''
                   AND COALESCE(is_active, 1) = 1
                 GROUP BY TRIM(department)""",
            _COVERING_ROLES,
        ).fetchall()

    wards_by_dept: dict[str, list[dict]] = {}
    for r in rows:
        wards_by_dept.setdefault(r["department"] or "", []).append(
            {"ward": r["ward"], "occupied": r["occupied"]}
        )
    staff_by_dept = {r["department"]: r["staff_count"] for r in staff}

    return [
        {
            "department": dept,
            "staff_count": staff_by_dept.get(dept, 0),
            "wards": sorted(wards_by_dept.get(dept, []), key=lambda w: str(w["ward"])),
        }
        for dept in departments
    ]


#: Roles that may be named as the CONSULTING doctor on an OPD visit. Narrower
#: than `_COVERING_ROLES` on purpose: a nurse covers a ward (so the ward is not
#: orphaned) but does not hold an outpatient clinic, and 'clinician' is the
#: server's own alias for 'doctor' (permissions.ROLE_PERMISSIONS).
_CONSULTING_ROLES = ("doctor", "clinician")


def list_consulting_doctors(
    department: str | None = None, *, facilities: frozenset[int] | None = None
) -> list[dict]:
    """The doctors a front desk may book an OPD visit with.

    WHY THIS EXISTS. The panel used to derive its doctor list from
    `list_opd_queue` — i.e. from the visits ALREADY registered today. That is
    circular: on an empty morning no doctor has a queue, so the dropdown was
    empty, so the first visit of the day could not be assigned to anybody, so no
    doctor ever got a queue. The roster is a property of the STAFF TABLE, not of
    today's traffic, and is read from there.

    Scoped exactly like every other front-desk read: `department` is what
    `permissions.department_scope` derived from the AUTHENTICATED user and is
    never nameable by the caller. A governance account with no department (the
    scope function returns None) sees every clinic, which is the same answer it
    already gets from the queue.

    Inactive accounts are excluded — a doctor who has left must not be bookable
    — and so are accounts with no department, matching the convention in
    `list_covering_departments`.

    `facilities` is the caller's permissions.facility_scope (multisite-9a): a
    set keeps the doctors of the clinics those facilities own (the departments
    master), so a satellite desk is never offered the main hospital's roster.
    None (one facility on record) changes nothing.
    """
    ensure_frontdesk_schema()
    if facilities is not None:
        clinics = {
            name.strip().casefold() for name in list_covering_departments(facilities=facilities)
        }
        return [
            d
            for d in list_consulting_doctors(department)
            if (d["department"] or "").strip().casefold() in clinics
        ]
    marks = ",".join("?" for _ in _CONSULTING_ROLES)
    sql = f"""SELECT id, name, TRIM(department) AS department
                FROM users
               WHERE role IN ({marks})
                 AND COALESCE(is_active, 1) = 1
                 AND department IS NOT NULL
                 AND TRIM(department) != ''"""
    params: list[object] = list(_CONSULTING_ROLES)
    if department:
        sql += " AND TRIM(department) = ?"
        params.append(department.strip())
    sql += " ORDER BY name"
    with db_session() as db:
        rows = db.execute(sql, params).fetchall()
    return [{"id": r["id"], "name": r["name"], "department": r["department"]} for r in rows]


# ═════════════════════════════════════════════════════════════════════════════
#  OPD — registration, queue, lifecycle
# ═════════════════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class ConsultationCharge:
    """The consultation charge that was (or was not) raised for a visit."""

    charge_id: int | None
    service_code: str
    description: str
    rate_paise: int | None
    configured: bool
    total_paise: int
    gst_treatment: str
    source: str
    # True when THIS call inserted the encounter_charges row, False when the
    # idempotency key found an earlier one. Kept out of as_dict so the response
    # is unchanged; the OPD audit row reads it.
    newly_raised: bool = False

    def as_dict(self) -> dict:
        from services.money import format_inr_or_unset

        return {
            "charge_id": self.charge_id,
            "service_code": self.service_code,
            "description": self.description,
            "rate_paise": self.rate_paise,
            "rate_display": format_inr_or_unset(self.rate_paise),
            "rate_is_configured": self.configured,
            "total_paise": self.total_paise,
            "total_display": format_inr_or_unset(self.total_paise if self.configured else None),
            "gst_treatment": self.gst_treatment,
            "rate_source": self.source,
        }


def _patient_row(db, patient_id: str):
    return db.execute(
        "SELECT id, name, mrn, age, sex, ward, bed, status FROM patients WHERE id = ?",
        (patient_id,),
    ).fetchone()


def _patient_has_admission(db, patient_id: str) -> bool:
    return (
        db.execute(
            "SELECT 1 FROM admissions WHERE patient_id = ? LIMIT 1", (patient_id,)
        ).fetchone()
        is not None
    )


def _patient_has_active_admission(db, patient_id: str) -> bool:
    return (
        db.execute(
            "SELECT 1 FROM admissions WHERE patient_id = ? AND status = 'active' LIMIT 1",
            (patient_id,),
        ).fetchone()
        is not None
    )


def opd_mode() -> bool:
    """Is the 'opd' feature on right now (ORB_OPD with its prerequisites)?

    Read at call time. Everything this module does differently for outpatients
    under that feature is behind this one question; with it off the front desk
    behaves exactly as it did before 3 October 2026.
    """
    return config.feature_enabled("opd")


def opd_registration_allowed(db, patient_id: str, dept: str | None) -> bool:
    """May a caller scoped to `dept` register THIS patient into an OPD clinic?

    Not the same question as `permissions.patient_in_scope`, and the difference
    is the whole point of an outpatient department:

      * `dept is None` (governance account) — yes, as everywhere else.
      * The patient has an admission (current or past) — then a department IS
        derivable for them, and the ordinary ward boundary applies. Without this
        branch the OPD desk would be a scope launder: a Cardiology clerk could
        register an Oncology inpatient into a Cardiology clinic and then read
        their name, MRN and complaint off their own queue.
      * The patient has NO admission at all — a pure outpatient. No ward owns
        them, `patient_in_scope` fail-closes them out of EVERY department, and
        refusing here would make it impossible to register a walk-in. Allowed,
        and the visit row itself becomes the record of which clinic saw them.

    IN OPD MODE (opd-7, the 'opd' feature on) the line moves from "any
    admission ever" to "an ACTIVE admission": a patient discharged from
    Cardiology may be registered into the Ortho clinic, which the rule above
    refuses because their last admission still names Cardiology. A patient with
    an active admission anywhere keeps the ordinary ward boundary, so the desk
    still cannot launder an inpatient of another ward, and the OPD record view
    (permissions.patient_in_opd_scope) closes for anyone actively admitted.
    """
    if dept is None:
        return True
    if not patient_id:
        return False
    if opd_mode():
        if not _patient_has_active_admission(db, patient_id):
            return True
    elif not _patient_has_admission(db, patient_id):
        return True
    from permissions import patient_in_scope

    return patient_in_scope(db, patient_id, dept)


def _next_token(db, *, visit_date: str, department: str, doctor_id: str | None) -> int:
    """The next token for this doctor's clinic today.

    Tokens are per (date, department, doctor) and start at 1 each day. Cancelled
    visits are counted in MAX() on purpose: reusing a cancelled patient's token
    number the same morning means two people were called by the same number, and
    the queue display is the only instruction most OPD patients get.

    The unassigned line (no doctor) also counts the numbers a doctor's take
    moved out of it (OPD mode, opd_queue_service.assign_visit): that patient
    still holds a slip with the number, so it is spent the same way. With no
    take that day, which is always so with the 'opd' feature off, it adds
    nothing and the numbering is exactly as before.
    """
    row = db.execute(
        """SELECT MAX(token_number) AS top FROM opd_visits
            WHERE visit_date = ? AND department = ?
              AND COALESCE(doctor_id, '') = COALESCE(?, '')""",
        (visit_date, department, doctor_id),
    ).fetchone()
    top = int((row["top"] if row else None) or 0)
    if not doctor_id:
        from services.opd_queue_service import taken_unassigned_top

        top = max(top, taken_unassigned_top(db, visit_date=visit_date, department=department))
    return top + 1


def _raise_consultation_charge(
    db: Any,
    *,
    visit_id: str,
    patient_id: str,
    department: str,
    service_code: str,
    tariff_plan_id: int | None,
    charged_by: str | None,
    service_day: str,
    charged_at: str,
    interstate: bool = False,
    facility_id: int | None = None,
) -> ConsultationCharge:
    """Write the consultation charge line into the billing spine's table.

    THIS MODULE DOES NOT DO BILLING. It resolves a rate through
    billing_masters.resolve_rate, computes the line through money.compute_line,
    and inserts ONE `encounter_charges` row with `encounter_type='op'`. Invoicing,
    payment and claims are other modules reading that row.

    UNPRICED IS NOT FREE. When no tariff row and no configured default exist, the
    charge is still written — the consultation happened — with
    `rate_is_configured = 0`, a zero rate and zero totals. It sits in `pending`
    for the billing counter to price. The alternative (skipping the row) loses
    the fact that a chargeable event occurred, and the alternative to THAT
    (inventing a rate) is the fabricated-number failure this project forbids.

    `source` is 'manual': `encounter_charges.source` has a fixed CHECK list from
    migration 20 with no OPD member, and widening a shared spine table from a
    module is exactly the collision the one-migration rule exists to prevent.
    `source_ref` carries the visit id, so the provenance is not lost.

    TWO DIFFERENT KINDS OF TIME, and they are not interchangeable.
    `service_day` is the hospital-local CALENDAR DAY (`_hospital_day`) — it is
    what the patient's bill prints and what the tariff is looked up against, and
    it matches the day `billing_service.add_charge` writes for every other line
    in the hospital. `charged_at` is the INSTANT the counter posted it, in UTC.
    Passing the instant for both (which this function used to do) puts a 2 a.m.
    IST consultation on yesterday's bill at yesterday's price.

    The INSERT itself lives in services/opd_charges_service.raise_op_line_on
    (opd-13), the one writer of an outpatient line, so a registration-time
    charge and this completion-time one are the same row shape and share the
    visit's idempotency key: whichever runs first raises the line and the
    other finds it. A line found already raised is answered as STORED (its
    code, description and amount), never re-priced.

    `facility_id` is the visit's own facility (multisite-9a): the line is
    stamped there, so the facility's counter finds it.
    """
    from services.opd_charges_service import raise_op_line_on

    # Idempotent: one consultation charge per visit, whatever replays.
    line = raise_op_line_on(
        db,
        patient_id=patient_id,
        service_code=service_code,
        tariff_plan_id=tariff_plan_id,
        service_day=service_day,  # service_date: the ward's calendar day
        charged_at=charged_at,  # charged_at: the UTC instant
        charged_by=charged_by,
        idempotency_key=f"opd_visit:{visit_id}:consultation",
        source_ref=visit_id,
        description_fallback=f"{department} consultation",
        unpriced_note="Rate not configured at time of consultation",
        interstate=interstate,
        facility_id=facility_id,
    )
    return ConsultationCharge(
        charge_id=line.charge_id,
        service_code=line.service_code,
        description=line.description,
        rate_paise=line.rate_paise,
        configured=line.configured,
        total_paise=line.total_paise,
        gst_treatment=line.gst_treatment,
        source=line.rate_source,
        newly_raised=line.newly_raised,
    )


def register_opd_visit(
    *,
    patient_id: str,
    department: str,
    doctor_id: str | None = None,
    visit_type: str = "new",
    chief_complaint: str | None = None,
    appointment_id: str | None = None,
    notes: str | None = None,
    registered_by: str | None = None,
    idempotency_key: str | None = None,
    visit_date: str | None = None,
    self_registered: bool = False,
    on_registered: Callable[[Any, dict], None] | None = None,
    on_replay: Callable[[Any, dict, dict], None] | None = None,
    identity_check: dict | None = None,
) -> dict:
    """Register an outpatient visit and put it in the queue.

    The department is resolved against the COVERING list for the same reason a
    transfer is: an OPD queue in a department nobody staffs is a patient sitting
    in a corridor on a list nobody opens. Callers must have already checked that
    the CALLER is allowed to register into this department (the route does that,
    against `permissions.department_scope`) — this function validates that the
    department is real.

    Three keywords used only in OPD mode (opd-7), all inert by default:
      * `self_registered` stamps opd_visits.scope_self_registered = 1: the
        registrant holds opd.consult, so this visit opens the OPD record view
        to the person who registered it (ruling OPD-N2: it counts, flagged).
      * `on_registered(db, visit)` runs INSIDE this function's immediate
        transaction, after the visit row is written and before it commits. The
        route writes its audit rows there, so a fail-closed audit that cannot
        be written raises and the visit is rolled back with it. It is not
        called for an idempotent replay.
      * `on_replay(db, visit, facts)` handles an idempotent replay. When given,
        the replay is BOUND to this request: the earlier visit must be for the
        same patient and the same clinic, or OpdReplayConflict is raised before
        that visit is serialized (the idempotency key is global, so without
        this a key reused across clinics returned another clinic's patient).
        A matching replay calls on_replay inside the transaction with facts
        {"replay": True}, so the route audits the disclosure exactly as the
        flag-off path audits every call. Without it, a replay returns the
        earlier visit as it always did.

    One keyword used only with the 'registration' feature on (opd-6a):
      * `identity_check` is the passing stated-identifier check the route ran
        before this transaction (registration_service.check_identity, ruling
        OPD-S1): {result, checked, checked_at, checked_by, purpose, method},
        never a stated or stored value. It is written to migration 50's
        opd_visits.identity_check_json on a NEW visit only (a replay keeps the
        first call's). The API leaves that column out of every visit it
        returns, so the visit shape is unchanged.
    """
    ensure_frontdesk_schema()
    patient_id = (patient_id or "").strip()
    if not patient_id:
        raise FrontDeskError("patient_id is required")
    if visit_type not in ("new", "follow_up", "review", "procedure"):
        raise FrontDeskError(f"unknown visit_type {visit_type!r}")

    canonical_dept = _resolve_covering_department(department)
    if canonical_dept is None:
        raise FrontDeskError(
            f"{department!r} is not a department with clinical cover. An OPD "
            "queue nobody staffs is a patient nobody sees."
        )

    day = _hospital_day(visit_date)
    complaint = _clean(chief_complaint, _MAX_COMPLAINT_LEN)
    note = _clean(notes, _MAX_REASON_LEN)
    idem = _clean(idempotency_key, _MAX_LABEL_LEN)
    doctor = _clean(doctor_id, _MAX_LABEL_LEN)

    # immediate=True: the token allocation is read-then-write and two clerks
    # registering into the same clinic in the same second would otherwise both
    # read the same MAX() and hand out the same token.
    with db_session(immediate=True) as db:
        if idem:
            prior = db.execute(
                "SELECT * FROM opd_visits WHERE idempotency_key = ?", (idem,)
            ).fetchone()
            if prior:
                if on_replay is None:
                    return _serialize_visit(db, prior)
                same_patient = (prior["patient_id"] or "").strip() == patient_id
                same_clinic = (
                    prior["department"] or ""
                ).strip().casefold() == canonical_dept.casefold()
                if not (same_patient and same_clinic):
                    raise OpdReplayConflict(str(prior["id"]))
                visit = _serialize_visit(db, prior)
                on_replay(db, visit, {"replay": True})
                return visit

        patient = _patient_row(db, patient_id)
        if patient is None:
            raise FrontDeskError(f"No such patient: {patient_id}")

        doctor_name = None
        if doctor:
            # The same rule as list_consulting_doctors: an ACTIVE clinician of
            # THIS clinic. The id arrives from the request, and it used to be
            # checked only for existence, so a nurse's id or another clinic's
            # doctor was accepted and printed on the token as the consultant.
            drow = db.execute(
                "SELECT name, role, TRIM(COALESCE(department, '')) AS department, "
                "COALESCE(is_active, 1) AS active FROM users WHERE id = ?",
                (doctor,),
            ).fetchone()
            if (
                drow is None
                or drow["role"] not in _CONSULTING_ROLES
                or not drow["active"]
                or drow["department"].casefold() != canonical_dept.casefold()
            ):
                raise FrontDeskError(
                    f"{doctor} is not a consulting doctor in {canonical_dept}. "
                    "Choose a doctor from that clinic's list."
                )
            doctor_name = drow["name"]

        # multisite-4: the clinic's facility (1 with one facility on record).
        facility_id = opd_visit_facility_on(db, canonical_dept)
        visit_id = f"OPD-{uuid.uuid4().hex[:8].upper()}"
        token = _next_token(db, visit_date=day, department=canonical_dept, doctor_id=doctor)
        now = _now()
        db.execute(
            """INSERT INTO opd_visits (
                    id, patient_id, department, doctor_id, doctor_name, visit_date,
                    token_number, visit_type, chief_complaint, status, appointment_id,
                    registered_by, registered_at, notes, idempotency_key, facility_id
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'waiting', ?, ?, ?, ?, ?, ?)""",
            (
                visit_id,
                patient_id,
                canonical_dept,
                doctor,
                doctor_name,
                day,
                token,
                visit_type,
                complaint,
                _clean(appointment_id, _MAX_LABEL_LEN),
                registered_by,
                now,
                note,
                idem,
                facility_id,
            ),
        )
        if self_registered:
            # Migration 50's column; set only in OPD mode, so the insert above
            # is the one the front desk always ran.
            db.execute(
                "UPDATE opd_visits SET scope_self_registered = 1 WHERE id = ?",
                (visit_id,),
            )
        if identity_check is not None:
            # Migration 50's column; written only with the registration
            # feature on (opd-6a), in the visit's own transaction.
            db.execute(
                "UPDATE opd_visits SET identity_check_json = ? WHERE id = ?",
                (json.dumps(identity_check, sort_keys=True), visit_id),
            )
        prepaid = None
        if opd_mode():
            prepaid = _opd_charges_at_registration(db, visit_id, registered_by)
        row = db.execute("SELECT * FROM opd_visits WHERE id = ?", (visit_id,)).fetchone()
        visit = _serialize_visit(db, row)
        if prepaid is not None:
            visit["prepaid_charge"] = prepaid
        if on_registered is not None:
            on_registered(db, visit)
        return visit


def _opd_charges_at_registration(db, visit_id: str, registered_by: str | None) -> dict | None:
    """OPD mode (opd-13), inside the visit's transaction: keep the payer and
    tariff the visit is registered under (migration 50's payer_id and
    tariff_plan_id), and with ORB_OPD_CHARGE_AT=registration raise the
    consultation line now. Returns the raised line (with the code's basis),
    or None when the consultation is charged at completion."""
    from services import opd_charges_service as charges

    row = db.execute("SELECT * FROM opd_visits WHERE id = ?", (visit_id,)).fetchone()
    payer = charges.payer_snapshot_on(db, row["patient_id"])
    db.execute(
        "UPDATE opd_visits SET payer_id = ?, tariff_plan_id = ? WHERE id = ?",
        (payer.payer_id, payer.tariff_plan_id, visit_id),
    )
    if charges.charge_at() != "registration":
        return None
    visit = {k: row[k] for k in row.keys()}
    visit["tariff_plan_id"] = payer.tariff_plan_id
    line, choice = charges.raise_consultation_at_registration_on(
        db, visit=visit, charged_by=registered_by
    )
    return {**line.as_dict(), **choice.as_facts(), "charged_at": "registration"}


def _serialize_visit(db, row, *, now: datetime | None = None) -> dict:
    """API shape for one visit, including its live waiting time.

    In OPD mode (opd-8) the visit also carries its queue fields (priority,
    room, skipped, call count, cancel code, disposition) from
    opd_queue_service.queue_fields. With the feature off the keys are exactly
    the ones the desk always returned."""
    if row is None:
        return {}
    data = _visit_api_dict(row)
    patient = _patient_row(db, data.get("patient_id") or "")
    data["patient_name"] = patient["name"] if patient else None
    data["patient_mrn"] = patient["mrn"] if patient else None
    data["patient_age"] = patient["age"] if patient else None
    data["patient_sex"] = patient["sex"] if patient else None
    data["waiting_minutes"] = _waiting_minutes(data, now=now)
    data["consultation_minutes"] = _consultation_minutes(data, now=now)
    if opd_mode():
        from services.opd_queue_service import queue_fields, reissued_tokens

        data.update(queue_fields(row))
        # The token on the patient's slip when a doctor's take changed it
        # (opd_queue_service.assign_visit); None when it never changed.
        visit_id = str(data.get("id") or "")
        data["previous_token_number"] = (
            reissued_tokens(db, [visit_id]).get(visit_id) if data.get("doctor_id") else None
        )
    return data


def _parse_ts(value: object) -> datetime | None:
    if not value:
        return None
    text = str(value).replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _waiting_minutes(visit: dict, *, now: datetime | None = None) -> int | None:
    """Minutes between registration and being called — or, while still waiting,
    minutes elapsed so far. None when the timestamp is unreadable, NEVER 0: a
    waiting time that cannot be computed must not display as "seen instantly"."""
    start = _parse_ts(visit.get("registered_at"))
    if start is None:
        return None
    if visit.get("status") == "cancelled":
        end = _parse_ts(visit.get("cancelled_at"))
    else:
        end = _parse_ts(visit.get("called_at"))
    if end is None:
        if visit.get("status") in ("completed", "cancelled"):
            end = _parse_ts(visit.get("completed_at")) or _parse_ts(visit.get("cancelled_at"))
        if end is None:
            end = now or datetime.now(UTC)
    return max(0, int((end - start).total_seconds() // 60))


def _consultation_minutes(visit: dict, *, now: datetime | None = None) -> int | None:
    called = _parse_ts(visit.get("called_at"))
    if called is None:
        return None
    end = _parse_ts(visit.get("completed_at")) or (now or datetime.now(UTC))
    return max(0, int((end - called).total_seconds() // 60))


def list_opd_queue(
    *,
    department: str | None = None,
    visit_date: str | None = None,
    doctor_id: str | None = None,
    statuses: tuple[str, ...] | None = None,
    facilities: frozenset[int] | None = None,
) -> dict:
    """The queue, grouped by consulting doctor, ordered by token.

    `department` is the CALLER'S scope as derived by
    `permissions.department_scope` — None means an unscoped governance account
    and returns every clinic. It is never a department the request named; the
    route enforces that.

    `facilities` is the caller's permissions.facility_scope (multisite-9a): a
    queue is document-keyed, so a set keeps the visits whose own facility
    (migration 74) is one of them. None (one facility on record) changes
    nothing.
    """
    from services.facility_documents import facility_condition

    ensure_frontdesk_schema()
    day = _hospital_day(visit_date)
    wanted = tuple(statuses or ("waiting", "in_consultation"))
    for s in wanted:
        if s not in _OPD_STATUSES:
            raise FrontDeskError(f"unknown status {s!r}")

    clauses = ["v.visit_date = ?"]
    params: list[Any] = [day]
    if department is not None:
        clauses.append("v.department = ?")
        params.append(department)
    if doctor_id:
        clauses.append("v.doctor_id = ?")
        params.append(doctor_id)
    clauses.append(f"v.status IN ({','.join('?' for _ in wanted)})")
    params.extend(wanted)
    facility_sql, facility_params = facility_condition("v.facility_id", facilities)
    if facility_sql is not None:
        clauses.append(facility_sql)
        params.extend(facility_params)

    now = datetime.now(UTC)
    with db_session() as db:
        rows = db.execute(
            f"""SELECT v.*, p.name AS patient_name, p.mrn AS patient_mrn,
                       p.age AS patient_age, p.sex AS patient_sex
                  FROM opd_visits v
                  LEFT JOIN patients p ON p.id = v.patient_id
                 WHERE {" AND ".join(clauses)}
                 ORDER BY v.department, COALESCE(v.doctor_name, ''), v.token_number""",
            params,
        ).fetchall()

        # OPD mode (opd-8): each visit carries its queue fields, and within one
        # doctor's queue the order is the seeing order (in the room, then waiting
        # by priority then token, skipped after them). Off, nothing changes.
        queue_mode = opd_mode()
        reissued: dict = {}
        if queue_mode:
            from services import opd_queue_service as oq

            # The visits a doctor took under a new token: the desk must see the
            # number on the patient's slip is no longer theirs.
            reissued = oq.reissued_tokens(db, [r["id"] for r in rows if r["doctor_id"]])

    visits: list[dict] = []
    for r in rows:
        data = _visit_api_dict(r)
        data["waiting_minutes"] = _waiting_minutes(data, now=now)
        data["consultation_minutes"] = _consultation_minutes(data, now=now)
        if queue_mode:
            data.update(oq.queue_fields(r))
            data["previous_token_number"] = reissued.get(str(r["id"]))
        visits.append(data)
    if queue_mode:
        visits.sort(
            key=lambda v: (v["department"], v.get("doctor_name") or "", *oq.queue_sort_key(v))
        )

    queues: list[dict] = []
    index: dict[tuple, int] = {}
    for v in visits:
        key = (v["department"], v.get("doctor_id") or "")
        if key not in index:
            index[key] = len(queues)
            queues.append(
                {
                    "department": v["department"],
                    "doctor_id": v.get("doctor_id"),
                    "doctor_name": v.get("doctor_name") or "Unassigned",
                    "visits": [],
                }
            )
        queues[index[key]]["visits"].append(v)

    for q in queues:
        waits = [
            v["waiting_minutes"]
            for v in q["visits"]
            if v["status"] == "waiting" and v["waiting_minutes"] is not None
        ]
        q["waiting_count"] = sum(1 for v in q["visits"] if v["status"] == "waiting")
        q["in_consultation_count"] = sum(1 for v in q["visits"] if v["status"] == "in_consultation")
        # None, not 0, when nothing is waiting — "no queue" and "no wait" are
        # different facts and a 0 would read as the second.
        q["longest_wait_minutes"] = max(waits) if waits else None

    result = {
        "visit_date": day,
        "department": department,
        "queues": queues,
        "total": len(visits),
    }
    if queue_mode:
        # What the desk's queue screen needs to offer the OPD-mode actions,
        # and how often it re-reads until live desk nudges land (opd-9b).
        result.update(
            {
                "opd_mode": True,
                "poll_seconds": int(getattr(config, "OPD_QUEUE_POLL_SECONDS", 30) or 30),
                "priorities": [{"code": c, "label": lab} for c, lab in oq.priority_categories()],
                "cancel_reasons": [{"code": c, "label": lab} for c, lab in oq.CANCEL_REASON_CODES],
            }
        )
    return result


def get_opd_visit(visit_id: str) -> dict | None:
    ensure_frontdesk_schema()
    with db_session() as db:
        row = db.execute("SELECT * FROM opd_visits WHERE id = ?", (visit_id,)).fetchone()
        if row is None:
            return None
        return _serialize_visit(db, row)


def opd_visit_facility_id(visit_id: str) -> int | None:
    """The facility one outpatient visit belongs to (migration 74), or None
    when no visit has that id. The front desk routes check it against the
    caller's facility scope before showing or changing the visit
    (multisite-9a); the visit API itself leaves the column out."""
    ensure_frontdesk_schema()
    with db_session() as db:
        row = db.execute("SELECT facility_id FROM opd_visits WHERE id = ?", (visit_id,)).fetchone()
    return None if row is None or row[0] is None else int(row[0])


def start_opd_consultation(
    visit_id: str,
    *,
    called_by: str | None = None,
    on_done: Callable[[Any, dict, dict], None] | None = None,
) -> dict:
    """Call the next patient in: 'waiting' to 'in_consultation'. Idempotent.

    `on_done(db, visit, facts)` (OPD mode only) runs inside the transaction on
    EVERY successful call, so the route's audit row commits with the change.
    A double tap is called too, with facts {"replay": True}: the flag-off path
    audits every call, and OPD mode must never write fewer rows than it.
    """
    ensure_frontdesk_schema()
    with db_session(immediate=True) as db:
        row = db.execute("SELECT * FROM opd_visits WHERE id = ?", (visit_id,)).fetchone()
        if row is None:
            raise FrontDeskError(f"No such OPD visit: {visit_id}")
        if row["status"] == "in_consultation":
            visit = _serialize_visit(db, row)
            if on_done is not None:
                on_done(db, visit, {"replay": True})
            return visit
        if row["status"] != "waiting":
            raise FrontDeskError(f"Visit {visit_id} is {row['status']} and cannot be called in")
        db.execute(
            "UPDATE opd_visits SET status = 'in_consultation', called_at = ? WHERE id = ?",
            (_now(), visit_id),
        )
        if opd_mode():
            # opd-8: the call is counted, and calling a skipped patient in
            # ends the skip. Migration 50's columns; untouched with OPD off.
            db.execute(
                "UPDATE opd_visits SET call_count = COALESCE(call_count, 0) + 1, "
                "skipped_at = NULL WHERE id = ?",
                (visit_id,),
            )
        updated = db.execute("SELECT * FROM opd_visits WHERE id = ?", (visit_id,)).fetchone()
        logger.info("OPD visit %s called in by %s", visit_id, called_by)
        visit = _serialize_visit(db, updated)
        if on_done is not None:
            on_done(db, visit, {"replay": False})
        return visit


def complete_opd_visit(
    visit_id: str,
    *,
    completed_by: str | None = None,
    service_code: str | None = None,
    raise_charge: bool = True,
    notes: str | None = None,
    on_done: Callable[[Any, dict, dict], None] | None = None,
    disposition: str | None = None,
    before_complete: Callable[[Any, Any], None] | None = None,
) -> dict:
    """Finish the consultation and raise the consultation charge.

    Two keywords used only by the OPD consultation route (opd-10), both inert
    by default:
      * `disposition` is written to opd_visits.disposition and disposition_at
        (migration 50) when THIS call completes the visit; a replay never
        rewrites it. The route validates the vocabulary.
      * `before_complete(db, row)` runs inside the immediate transaction before
        anything is written, and only when the visit is not yet completed. It
        raises to refuse (the OPD-N8 rule: a signed note, or a reason).

    The charge is raised HERE and not at registration because a patient who
    registers and leaves before being seen has not been consulted (a site may
    choose otherwise in OPD mode: ORB_OPD_CHARGE_AT=registration, opd-13, when
    this call finds the line already raised under the visit's key). In OPD
    mode the code comes from the site's charge rules (opd_charges_service)
    unless the caller names one, the line is priced on the tariff the visit
    was registered under, `on_done`'s facts carry the code and its basis, and
    a checked-in appointment naming this visit is marked completed. Idempotent on
    both halves: a replay returns the completed visit and does not double-charge
    (the `encounter_charges` idempotency key is derived from the visit id).

    `on_done(db, visit, facts)` (OPD mode only) runs inside the transaction on
    EVERY successful call, with `consultation_charge` already on the dict, so
    the route's audit row commits with the completion and its charge. A replay
    is called too, because a replay is not always a no-op: completing with
    raise_charge false and then again with raise_charge true inserts the charge
    on the second call, and that billing write must have a domain audit row
    naming it (the flag-off path audits every call). `facts` says which:
      * "replay": the visit was already completed before this call;
      * "charge_raised_now": this call inserted the encounter_charges row.
    """
    ensure_frontdesk_schema()
    with db_session(immediate=True) as db:
        row = db.execute("SELECT * FROM opd_visits WHERE id = ?", (visit_id,)).fetchone()
        if row is None:
            raise FrontDeskError(f"No such OPD visit: {visit_id}")
        if row["status"] == "cancelled":
            raise FrontDeskError(f"Visit {visit_id} was cancelled and cannot be completed")

        now = _now()
        already_complete = row["status"] == "completed"
        if not already_complete and before_complete is not None:
            before_complete(db, row)
        if not already_complete:
            db.execute(
                """UPDATE opd_visits
                      SET status = 'completed',
                          completed_at = ?,
                          called_at = COALESCE(called_at, ?),
                          notes = COALESCE(?, notes)
                    WHERE id = ?""",
                (now, now, _clean(notes, _MAX_REASON_LEN), visit_id),
            )
            if disposition:
                db.execute(
                    "UPDATE opd_visits SET disposition = ?, disposition_at = ? WHERE id = ?",
                    (disposition, now, visit_id),
                )

        charge: ConsultationCharge | None = None
        charge_facts: dict = {}
        queue_mode = opd_mode()
        if raise_charge and queue_mode:
            # opd-13: the site's charge rules choose the code (the caller's own
            # service_code still wins), and the visit is priced on the tariff
            # it was registered under. With ORB_OPD_CHARGE_AT=registration the
            # line already exists under this visit's key and is returned as
            # stored, so nothing is charged twice.
            from services import opd_charges_service as charges

            choice = charges.resolve_consultation_code_on(
                db, {k: row[k] for k in row.keys()}, requested_code=service_code
            )
            plan_id = row["tariff_plan_id"]
            if plan_id is None:
                plan_id = charges.payer_snapshot_on(db, row["patient_id"]).tariff_plan_id
            charge = _raise_consultation_charge(
                db,
                visit_id=visit_id,
                patient_id=row["patient_id"],
                department=row["department"],
                service_code=choice.service_code,
                tariff_plan_id=plan_id,
                charged_by=completed_by,
                service_day=_hospital_day(now),
                charged_at=now,
                facility_id=row["facility_id"],
            )
            charge_facts = choice.as_facts()
            if charge.service_code != choice.service_code:
                # Raised at registration under the code chosen then.
                charge_facts["charge_code"] = charge.service_code
                charge_facts["charge_code_basis"] = "raised_at_registration"
            db.execute(
                "UPDATE opd_visits SET consultation_charge_id = ? WHERE id = ?",
                (charge.charge_id, visit_id),
            )
        elif raise_charge:
            payer = resolve_patient_payer(row["patient_id"])
            charge = _raise_consultation_charge(
                db,
                visit_id=visit_id,
                patient_id=row["patient_id"],
                department=row["department"],
                service_code=(service_code or OPD_CONSULTATION_SERVICE_CODE),
                tariff_plan_id=payer.tariff_plan_id,
                charged_by=completed_by,
                # The bill's DATE is the ward's day; the posting TIME is UTC.
                service_day=_hospital_day(now),
                charged_at=now,
                facility_id=row["facility_id"],
            )
            db.execute(
                "UPDATE opd_visits SET consultation_charge_id = ? WHERE id = ?",
                (charge.charge_id, visit_id),
            )
        if queue_mode and not already_complete and row["appointment_id"]:
            # opd-16: the appointment this visit was checked in from is now
            # attended. Only a checked-in appointment that names THIS visit.
            db.execute(
                "UPDATE appointments SET status = 'completed', completed_at = ?, "
                "completed_by = ? WHERE id = ? AND status = 'checked_in' "
                "AND opd_visit_id = ?",
                (now, completed_by, row["appointment_id"], visit_id),
            )

        updated = db.execute("SELECT * FROM opd_visits WHERE id = ?", (visit_id,)).fetchone()
        result = _serialize_visit(db, updated)
        if on_done is not None:
            result["consultation_charge"] = charge.as_dict() if charge else None
            on_done(
                db,
                result,
                {
                    "replay": already_complete,
                    "charge_raised_now": bool(charge and charge.newly_raised),
                    **charge_facts,
                },
            )

    result["consultation_charge"] = charge.as_dict() if charge else None
    return result


def cancel_opd_visit(
    visit_id: str,
    *,
    reason: str,
    cancelled_by: str | None = None,
    on_done: Callable[[Any, dict, dict], None] | None = None,
    reason_code: str | None = None,
) -> dict:
    """Cancel a visit (no-show, left without being seen). Soft: the row stays.

    `on_done(db, visit, facts)` (OPD mode only) runs inside the transaction on
    EVERY successful call, so the route's audit row commits with the cancel. A
    repeat cancel of an already cancelled visit is called too, with facts
    {"replay": True}: the flag-off path audits every call.

    `reason_code` (OPD mode, opd-8) is one of
    opd_queue_service.CANCEL_REASON_CODES and is stored in
    opd_visits.cancel_reason_code; 'left_without_being_seen' is the LWBS code.
    A cancel never raises a charge, coded or not. In OPD mode with
    ORB_OPD_CHARGE_AT=registration (opd-13) the line raised at registration
    is voided while still pending and left for a refund once invoiced; the
    facts handed to `on_done` say which (prepaid_charge voided, invoiced,
    already_cancelled or missing), and an appointment checked in to the visit
    is cancelled with it (opd-16).
    """
    ensure_frontdesk_schema()
    clean_reason = _clean(reason, _MAX_REASON_LEN)
    if not clean_reason:
        raise FrontDeskError("A cancellation reason is required")
    code = (reason_code or "").strip().lower() or None
    if code is not None:
        from services.opd_queue_service import CANCEL_REASON_CODES

        if code not in dict(CANCEL_REASON_CODES):
            raise FrontDeskError(f"{reason_code!r} is not a cancellation reason code")
    with db_session(immediate=True) as db:
        row = db.execute("SELECT * FROM opd_visits WHERE id = ?", (visit_id,)).fetchone()
        if row is None:
            raise FrontDeskError(f"No such OPD visit: {visit_id}")
        if row["status"] == "cancelled":
            visit = _serialize_visit(db, row)
            if on_done is not None:
                on_done(db, visit, {"replay": True})
            return visit
        if row["status"] == "completed":
            # The consultation happened. Cancelling it would erase a clinical
            # event and orphan its charge.
            raise FrontDeskError(f"Visit {visit_id} is completed and cannot be cancelled")
        db.execute(
            "UPDATE opd_visits SET status = 'cancelled', cancelled_at = ?, "
            "cancel_reason = ? WHERE id = ?",
            (_now(), clean_reason, visit_id),
        )
        if code is not None:
            db.execute(
                "UPDATE opd_visits SET cancel_reason_code = ? WHERE id = ?",
                (code, visit_id),
            )
        facts: dict = {"replay": False}
        if opd_mode():
            # opd-13: a line raised at registration (ORB_OPD_CHARGE_AT=
            # registration) is voided while still pending, a 'left without
            # being seen' included; one already on an invoice is left for a
            # refund at the payments desk, and the facts say which.
            from services.opd_charges_service import void_prepaid_consultation_on

            facts.update(
                void_prepaid_consultation_on(
                    db, visit_row=row, actor=cancelled_by, reason=clean_reason
                )
            )
            if row["appointment_id"]:
                # opd-16: the appointment checked in to this visit is closed
                # with it, saying why.
                db.execute(
                    "UPDATE appointments SET status = 'cancelled', cancel_reason = ?, "
                    "cancelled_at = ?, cancelled_by = ? WHERE id = ? "
                    "AND status = 'checked_in' AND opd_visit_id = ?",
                    (
                        f"The outpatient visit was cancelled: {clean_reason}"[:_MAX_REASON_LEN],
                        _now(),
                        cancelled_by,
                        row["appointment_id"],
                        visit_id,
                    ),
                )
        updated = db.execute("SELECT * FROM opd_visits WHERE id = ?", (visit_id,)).fetchone()
        logger.info("OPD visit %s cancelled by %s: %s", visit_id, cancelled_by, clean_reason)
        visit = _serialize_visit(db, updated)
        if "prepaid_charge" in facts:
            visit["prepaid_charge"] = {
                "charge_id": facts.get("prepaid_charge_id"),
                "outcome": facts["prepaid_charge"],
                "invoice_id": facts.get("prepaid_invoice_id"),
            }
        if on_done is not None:
            on_done(db, visit, facts)
        return visit


# ═════════════════════════════════════════════════════════════════════════════
#  Transfers
# ═════════════════════════════════════════════════════════════════════════════


def transfer_patient(
    *,
    patient_id: str,
    to_department: str,
    to_ward: str | None = None,
    to_bed: str | None = None,
    reason: str,
    moved_by: str | None = None,
    moved_by_name: str | None = None,
    notes: str | None = None,
    idempotency_key: str | None = None,
) -> dict:
    """Move an admitted patient to another ward. ONE transaction, ONE admission.

    What this writes, and why each write is necessary:

      1. `patient_movements` — the LEDGER row. from/to department, ward and bed,
         a UTC timestamp, a mandatory reason and the named actor. This is the
         record that did not exist before: previously a ward change was a column
         edit with no history, so "which ward was she on when she deteriorated"
         had no answer.

      2. `admissions` (the SAME row) — department/ward/bed updated in place. This
         is the ACCESS BOUNDARY moving, because `permissions.PATIENT_DEPARTMENT_
         SQL` derives scope from this column on every read. `status` is NOT
         touched: it stays 'active'.

      3. `patients` — ward/bed mirrored. Vigil, Sage, the nurse board and the
         discharge summary read the location off `patients`, not `admissions`
         (services/admission_service.admit_patient establishes that mirror). If
         only the admission moved, the patient would be scoped to the ICU while
         every screen in the hospital still printed "Cardiology, Bed 4".
         `patients.status` is NOT touched either: it stays 'admitted'.

    REFUSALS (all of them are the anti-orphaning rule):
      * unknown patient, or a patient with no ACTIVE admission — there is no
        location to move;
      * a destination department with no clinical cover — see
        `list_covering_departments`;
      * a transfer to the department the patient is already in AND the same bed —
        returned unchanged rather than writing a movement to nowhere.
    """
    ensure_frontdesk_schema()
    # With Beds and admissions (ORB_ADT) on, the bed master's single location
    # writer owns admissions.ward/bed and patients.ward/bed, and the route
    # delegates to services/adt/transfer.py (adt-6a). This lane writes those
    # columns directly, so it refuses rather than become a second writer.
    from services.adt.core import adt_on

    if adt_on():
        raise FrontDeskError(
            "Beds and admissions is switched on, so a transfer is recorded through it: "
            "use the Transfer screen, which checks the bed and records the move."
        )
    patient_id = (patient_id or "").strip()
    if not patient_id:
        raise FrontDeskError("patient_id is required")
    clean_reason = _clean(reason, _MAX_REASON_LEN)
    if not clean_reason:
        raise FrontDeskError("A transfer reason is required. A move with no reason is not a record")

    canonical_dept = _resolve_covering_department(to_department)
    if canonical_dept is None:
        raise FrontDeskError(uncovered_department_words(to_department))
    # multisite-3: never into another facility's department while more than
    # one facility is on record (CrossFacilityMoveRefused, audited).
    _refuse_cross_facility_transfer(patient_id, canonical_dept, moved_by)

    ward = _clean(to_ward, _MAX_LABEL_LEN)
    bed = _clean(to_bed, _MAX_LABEL_LEN)
    note = _clean(notes, _MAX_REASON_LEN)
    idem = _clean(idempotency_key, _MAX_LABEL_LEN)

    with db_session(immediate=True) as db:
        if idem:
            prior = db.execute(
                "SELECT * FROM patient_movements WHERE idempotency_key = ?", (idem,)
            ).fetchone()
            if prior:
                return _transfer_result(db, patient_id, _movement_api_dict(prior))

        patient = _patient_row(db, patient_id)
        if patient is None:
            raise FrontDeskError(f"No such patient: {patient_id}")

        admission = db.execute(
            """SELECT * FROM admissions
                WHERE patient_id = ? AND status = 'active'
                ORDER BY created_at DESC, rowid DESC LIMIT 1""",
            (patient_id,),
        ).fetchone()
        if admission is None:
            raise FrontDeskError(
                f"{patient_id} has no active admission: an outpatient cannot be "
                "transferred between wards"
            )

        from_department = admission["department"]
        from_ward = admission["ward"]
        from_bed = admission["bed"]

        # A "transfer" that changes nothing writes a movement row asserting a
        # move that did not happen. The ledger has to stay truthful.
        if (
            (from_department or "") == canonical_dept
            and (ward is None or (from_ward or "") == ward)
            and (bed is None or (from_bed or "") == bed)
        ):
            raise FrontDeskError(f"{patient_id} is already in {canonical_dept} at that location")

        now = _now()
        movement_id = f"MOV-{uuid.uuid4().hex[:8].upper()}"
        db.execute(
            """INSERT INTO patient_movements (
                    id, patient_id, admission_id, movement_type,
                    from_department, from_ward, from_bed,
                    to_department, to_ward, to_bed,
                    reason, moved_at, moved_by, moved_by_name, notes, idempotency_key
               ) VALUES (?, ?, ?, 'transfer', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                movement_id,
                patient_id,
                admission["id"],
                from_department,
                from_ward,
                from_bed,
                canonical_dept,
                ward if ward is not None else from_ward,
                bed if bed is not None else from_bed,
                clean_reason,
                now,
                moved_by,
                moved_by_name,
                note,
                idem,
            ),
        )

        # THE ACCESS BOUNDARY MOVES HERE. Same admission id, status untouched.
        db.execute(
            """UPDATE admissions
                  SET department = ?,
                      ward = COALESCE(?, ward),
                      bed = COALESCE(?, bed)
                WHERE id = ?""",
            (canonical_dept, ward, bed, admission["id"]),
        )

        # The display mirror. Vigil/Sage/nurse board/discharge read location from
        # `patients`; `patients.status` is deliberately NOT written — a
        # 'transferred' status here removes the patient from every board.
        db.execute(
            "UPDATE patients SET ward = COALESCE(?, ward), bed = COALESCE(?, bed) WHERE id = ?",
            (ward, bed, patient_id),
        )

        movement = db.execute(
            "SELECT * FROM patient_movements WHERE id = ?", (movement_id,)
        ).fetchone()
        return _transfer_result(db, patient_id, _movement_api_dict(movement))


def _transfer_result(db, patient_id: str, movement: dict) -> dict:
    """The response shape: the movement plus the patient's location AFTER it."""
    admission = db.execute(
        """SELECT id, department, ward, bed, status FROM admissions
            WHERE patient_id = ? AND status = 'active'
            ORDER BY created_at DESC, rowid DESC LIMIT 1""",
        (patient_id,),
    ).fetchone()
    patient = _patient_row(db, patient_id)
    return {
        "movement": movement,
        "patient_id": patient_id,
        "patient_name": patient["name"] if patient else None,
        "admission_id": admission["id"] if admission else None,
        # Proof, in the response, that the admission survived the move.
        "admission_status": admission["status"] if admission else None,
        "patient_status": patient["status"] if patient else None,
        "current_department": admission["department"] if admission else None,
        "current_ward": (admission["ward"] if admission else None),
        "current_bed": (admission["bed"] if admission else None),
    }


def list_patient_movements(patient_id: str, *, limit: int = 50) -> list[dict]:
    """This patient's movement history, newest first."""
    ensure_frontdesk_schema()
    limit = max(1, min(int(limit), 500))
    with db_session() as db:
        rows = db.execute(
            """SELECT * FROM patient_movements
                WHERE patient_id = ?
                ORDER BY moved_at DESC, rowid DESC
                LIMIT ?""",
            (patient_id, limit),
        ).fetchall()
    return [_movement_api_dict(r) for r in rows]


# ═════════════════════════════════════════════════════════════════════════════
#  Group identity match at registration (multisite-17, W12-E)
# ═════════════════════════════════════════════════════════════════════════════
# A satellite desk sees only its own facility's patients, so a patient whose
# record sits at another facility of the group would be registered a second
# time, and their allergy list split in two (ruling MS-R11 (a): one group
# record per person). This lane finds that patient across the whole group by
# four things the patient states (name, age or date of birth, sex and phone)
# and answers who they are and nothing clinical: the record id, name, age,
# sex, group record number (UHID) and the code of the facility the record was
# registered at.
#
# Built strict (multisite-17; match quality, merge and encounter attribution
# belong to the OPD/MPI epic):
#   * all four must match: the phone's ten digits exactly, the sex exactly,
#     every word of the name the patient says is a word of the record's name
#     (patient_matching.stated_name_matches, the identity check's own rule),
#     and the date of birth exactly, or the age within AGE_CLOSE_YEARS;
#   * every search is audited phi_access (fail-closed, inside the search's own
#     transaction) with the desk's reason and the ids it showed, a search that
#     finds nobody included; nothing is answered if the row cannot be written;
#   * GROUP_MATCH_LIMIT searches per account per GROUP_MATCH_WINDOW, counted
#     from those rows, so a restart does not reset the count;
#   * each match carries a ticket bound to the searching account and that one
#     patient for GROUP_MATCH_TICKET_SECONDS. Registering a visit with it is
#     the one way a desk registers a patient whose latest encounter is at
#     another facility; the visit then brings the chart into this facility's
#     scope (ruling MS-R5 (a)), and that is audited clinical_access in the
#     visit's own transaction.
# The lane is offered only with ORB_MULTI_SITE on and more than one facility
# on record (the route's gates); with one facility every patient is already
# in the desk's own list.

GROUP_MATCH_ACTION = "group_identity_match_searched"
GROUP_MATCH_LIMITED_ACTION = "group_identity_match_rate_limited"
GROUP_MATCH_VISIT_ACTION = "group_match_visit_registered"
GROUP_MATCH_LIMIT = 20
GROUP_MATCH_WINDOW = timedelta(hours=1)
GROUP_MATCH_MAX_RESULTS = 10
GROUP_MATCH_SCAN_CAP = 500
GROUP_MATCH_TICKET_SECONDS = 15 * 60
GROUP_MATCH_REASON_MIN = 8
GROUP_MATCH_REASON_MAX = 500
GROUP_MATCH_AGE_MAX = 130

#: The refusal for a matched patient who is admitted at another facility now.
GROUP_MATCH_INPATIENT_WORDS = (
    "This patient is admitted at another facility of the group right now, so a visit "
    "cannot be registered here. Nothing was saved. Ask that facility to discharge or "
    "transfer them first."
)


class GroupMatchError(FrontDeskError):
    """A group identity search that cannot run: missing or unusable facts (422),
    no reason (422), or too many searches (429). `detail()` is the route's
    answer; it never carries a stated or stored value."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: int = 422,
        fields: list[str] | None = None,
        retry_after_seconds: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.fields = list(fields or [])
        self.retry_after_seconds = retry_after_seconds

    def detail(self) -> dict:
        out: dict[str, Any] = {"error": self.code, "message": self.message}
        if self.fields:
            out["fields"] = self.fields
        if self.retry_after_seconds is not None:
            out["retry_after_seconds"] = self.retry_after_seconds
        return out


def _ticket_key() -> bytes:
    """The ticket key, derived from the session secret for this one purpose
    (never the session secret itself)."""
    secret = str(getattr(config, "JWT_SECRET", "") or "").encode("utf-8")
    return hmac.new(secret, b"orb-group-identity-match-ticket-v1", hashlib.sha256).digest()


def _ticket_mac(user_id: str, patient_id: str, expires: int) -> str:
    message = f"{user_id}|{patient_id}|{expires}".encode()
    return hmac.new(_ticket_key(), message, hashlib.sha256).hexdigest()


def group_match_ticket(user_id: str, patient_id: str, *, now: float | None = None) -> str:
    """A ticket for registering a visit for `patient_id`, valid for
    GROUP_MATCH_TICKET_SECONDS and only for the account `user_id`."""
    import time

    expires = int((time.time() if now is None else now) + GROUP_MATCH_TICKET_SECONDS)
    return f"{expires}.{_ticket_mac(str(user_id), str(patient_id), expires)}"


def group_match_ticket_valid(
    ticket: str | None, *, user_id: str | None, patient_id: str | None, now: float | None = None
) -> bool:
    """Was this ticket issued by a group search of THIS account for THIS
    patient, and is it still in date? Never raises."""
    import time

    if not ticket or not user_id or not patient_id:
        return False
    try:
        expires_text, mac = str(ticket).split(".", 1)
        expires = int(expires_text)
    except (TypeError, ValueError):
        return False
    current = time.time() if now is None else now
    if expires < current or expires > current + GROUP_MATCH_TICKET_SECONDS + 5:
        return False
    return hmac.compare_digest(_ticket_mac(str(user_id), str(patient_id), expires), mac)


def _stated_group_facts(stated: dict) -> dict:
    """The four stated facts in their compared form, or GroupMatchError
    naming every field that is missing or unusable (never echoing a value)."""
    from services import patient_matching as pm

    missing: list[str] = []
    name = str(stated.get("name") or "").strip()
    if not pm.name_key(name) or len(name) < 2:
        missing.append("name")
    sex = pm.sex_key(stated.get("sex"))
    if sex is None:
        missing.append("sex")
    phone = pm.phone_key(stated.get("phone"))
    if phone is None:
        missing.append("phone")
    dob = None
    raw_dob = stated.get("date_of_birth")
    if raw_dob not in (None, ""):
        dob = pm.dob_key(raw_dob)
        if dob is None:
            missing.append("date_of_birth")
    age = None
    raw_age = stated.get("age_years")
    if raw_age not in (None, ""):
        try:
            age = int(raw_age)
        except (TypeError, ValueError):
            age = None
        if age is None or isinstance(raw_age, bool) or not 0 <= age <= GROUP_MATCH_AGE_MAX:
            missing.append("age_years")
            age = None
    if dob is None and age is None and not {"date_of_birth", "age_years"} & set(missing):
        missing.append("age_or_date_of_birth")
    if missing:
        raise GroupMatchError(
            "group_match_incomplete",
            "Ask the patient for their full name, their age or date of birth, their sex and "
            "their own 10-digit phone number. All four are needed to search the group.",
            fields=missing,
        )
    return {"name": name, "sex": sex, "phone": phone, "dob": dob, "age": age}


def _clean_reason(reason: str | None) -> str:
    text = " ".join(str(reason or "").split())
    if len(text) < GROUP_MATCH_REASON_MIN:
        raise GroupMatchError(
            "reason_required",
            "Say why you are searching the whole group, for example "
            "'Patient says they were treated at the main hospital'.",
            fields=["reason"],
        )
    return text[:GROUP_MATCH_REASON_MAX]


def group_match_searches_since(db: Any, user_id: str | None, since: str) -> int:
    """How many group searches this account has made since `since` (a UTC ISO
    instant), counted from the audit trail. Names no patient."""
    return int(
        db.execute(
            "SELECT COUNT(*) FROM audit_logs WHERE actor_user_id = ? AND action_type = ? "
            "AND timestamp >= ?",
            (user_id, GROUP_MATCH_ACTION, since),
        ).fetchone()[0]
    )


def _age_fits(facts: dict, row: Any, today: date) -> bool:
    """The stated date of birth or age against the record's."""
    from services import patient_matching as pm

    stored_dob = pm.dob_key(row["date_of_birth"])
    if facts["dob"] is not None and stored_dob is not None:
        return bool(facts["dob"] == stored_dob)
    stated_age = facts["age"]
    if stated_age is None:
        born = date.fromisoformat(facts["dob"])
        stated_age = pm.full_years(born, today)
    stored_age, _stated = pm.current_age(
        date_of_birth=row["date_of_birth"],
        stated_age=row["age"],
        age_recorded_on=None,
        created_at=row["created_at"],
        today=today,
    )
    return stored_age is not None and abs(stored_age - stated_age) <= pm.AGE_CLOSE_YEARS


def find_group_matches_on(db: Any, facts: dict, *, today: date | None = None) -> tuple:
    """(matches, truncated) for the stated facts, across every facility. Each
    match is {id, name, age, sex, group_mrn, registered_facility_id}; nothing
    clinical is read. Candidates are found by the phone's last four digits
    and checked here on all four facts."""
    from services import patient_matching as pm

    today = today or hospital_today()
    rows = db.execute(
        "SELECT id, name, mrn, age, date_of_birth, sex, phone, created_at, "
        "registered_facility_id FROM patients WHERE phone LIKE ? ORDER BY id LIMIT ?",
        (f"%{facts['phone'][-4:]}", GROUP_MATCH_SCAN_CAP),
    ).fetchall()
    scanned_all = len(rows) < GROUP_MATCH_SCAN_CAP
    matches = []
    for row in rows:
        if pm.phone_key(row["phone"]) != facts["phone"]:
            continue
        if pm.sex_key(row["sex"]) != facts["sex"]:
            continue
        if not pm.stated_name_matches(facts["name"], row["name"]):
            continue
        if not _age_fits(facts, row, today):
            continue
        age, _stated = pm.current_age(
            date_of_birth=row["date_of_birth"],
            stated_age=row["age"],
            age_recorded_on=None,
            created_at=row["created_at"],
            today=today,
        )
        matches.append(
            {
                "id": row["id"],
                "name": row["name"],
                "age": age,
                "sex": pm.sex_key(row["sex"]),
                "group_mrn": row["mrn"],
                "registered_facility_id": int(row["registered_facility_id"] or 1),
            }
        )
    matches.sort(key=lambda m: ((m["name"] or "").casefold(), m["id"]))
    truncated = (not scanned_all) or len(matches) > GROUP_MATCH_MAX_RESULTS
    return matches[:GROUP_MATCH_MAX_RESULTS], truncated


def group_identity_match(
    stated: dict,
    *,
    user: dict,
    reason: str | None,
    request_meta: dict | None = None,
    today: date | None = None,
) -> dict:
    """Search the whole group for the patient the desk is talking to.

    Returns {matches: [{id, name, age, sex, group_mrn, registered_facility,
    ticket}], truncated, searched_at, searches_left, ticket_seconds}.
    Raises GroupMatchError (422 incomplete or no reason, 429 too many
    searches) and AuditWriteError (the route's 503) when the phi_access row
    cannot be written, in which case nothing is answered."""
    from services import facility_service
    from services.audit_service import log_audit_event
    from services.clinical_time import utc_iso_now

    facts = _stated_group_facts(stated)
    why = _clean_reason(reason)
    meta = dict(request_meta or {})
    user_id = user.get("id")
    since = (datetime.now(UTC) - GROUP_MATCH_WINDOW).isoformat()
    criteria = ["name", "date_of_birth" if facts["dob"] else "age_years", "sex", "phone"]
    limited = False
    used = 0
    matches: list[dict] = []
    truncated = False
    with db_session(immediate=True) as db:
        used = group_match_searches_since(db, user_id, since)
        if used >= GROUP_MATCH_LIMIT:
            limited = True
        else:
            matches, truncated = find_group_matches_on(db, facts, today=today)
            log_audit_event(
                correlation_id=str(uuid.uuid4()),
                event_category="phi_access",
                action_type=GROUP_MATCH_ACTION,
                outcome="success",
                db=db,
                summary_message=(
                    f"Group identity search at the front desk: {len(matches)} match(es)"
                ),
                actor_user_id=user_id,
                actor_role=user.get("role"),
                target_type="patient_list",
                target_id="group-identity-match",
                request_path=meta.get("request_path"),
                http_method=meta.get("http_method"),
                ip_address=meta.get("ip_address"),
                user_agent=meta.get("user_agent"),
                metadata={
                    "reason": why,
                    "criteria": criteria,
                    "matched_patient_ids": [m["id"] for m in matches],
                    "result_count": len(matches),
                    "truncated": truncated,
                    "searches_in_window": used + 1,
                    "window_seconds": int(GROUP_MATCH_WINDOW.total_seconds()),
                },
            )
    if limited:
        log_audit_event(
            correlation_id=str(uuid.uuid4()),
            event_category="authorization",
            action_type=GROUP_MATCH_LIMITED_ACTION,
            outcome="denied",
            summary_message="Group identity search refused: too many searches in the last hour",
            actor_user_id=user_id,
            actor_role=user.get("role"),
            request_path=meta.get("request_path"),
            http_method=meta.get("http_method"),
            ip_address=meta.get("ip_address"),
            user_agent=meta.get("user_agent"),
            metadata={"searches_in_window": used, "limit": GROUP_MATCH_LIMIT, "reason": why},
        )
        raise GroupMatchError(
            "group_match_rate_limited",
            f"You have searched the whole group {GROUP_MATCH_LIMIT} times in the last hour. "
            "Ask a colleague, or try again later.",
            status=429,
            retry_after_seconds=int(GROUP_MATCH_WINDOW.total_seconds()),
        )
    out = []
    for m in matches:
        ident = facility_service.facility_identity(m["registered_facility_id"]) or {}
        out.append(
            {
                "id": m["id"],
                "name": m["name"],
                "age": m["age"],
                "sex": m["sex"],
                "group_mrn": m["group_mrn"],
                "registered_facility": {"id": ident.get("id"), "code": ident.get("code")},
                "ticket": group_match_ticket(str(user_id), m["id"]),
            }
        )
    return {
        "matches": out,
        "truncated": truncated,
        "searched_at": utc_iso_now(),
        "searches_left": max(0, GROUP_MATCH_LIMIT - used - 1),
        "ticket_seconds": GROUP_MATCH_TICKET_SECONDS,
    }


def audit_group_match_visit_on(
    db: Any,
    *,
    user: dict,
    visit: dict,
    from_facility_id: int | None,
    to_facility_id: int | None,
    request_meta: dict | None = None,
) -> None:
    """The clinical_access row (fail-closed) for a visit registered through a
    group match: the chart of a patient whose latest encounter was at another
    facility now belongs to this facility's scope. Inside the visit's own
    transaction, so the visit and its record land together or not at all."""
    from services.audit_service import log_audit_event

    meta = dict(request_meta or {})
    log_audit_event(
        correlation_id=f"group-match-visit-{visit.get('id')}",
        event_category="clinical_access",
        action_type=GROUP_MATCH_VISIT_ACTION,
        outcome="success",
        db=db,
        summary_message=(
            f"OPD visit {visit.get('id')} registered for {visit.get('patient_id')} through a "
            "group identity match; the record now opens at this facility"
        ),
        actor_user_id=user.get("id"),
        actor_role=user.get("role"),
        patient_id=visit.get("patient_id"),
        target_type="opd_visit",
        target_id=str(visit.get("id")),
        request_path=meta.get("request_path"),
        http_method=meta.get("http_method"),
        ip_address=meta.get("ip_address"),
        user_agent=meta.get("user_agent"),
        metadata={
            "department": visit.get("department"),
            "from_facility_id": from_facility_id,
            "to_facility_id": to_facility_id,
        },
    )
