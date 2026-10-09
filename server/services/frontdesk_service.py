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

import json
import logging
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Optional

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
from services.billing_masters import resolve_patient_payer
from services.hospital_time import hospital_timezone, hospital_today

logger = logging.getLogger("orb.frontdesk")

__all__ = [
    "FrontDeskError",
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


class OpdReplayConflict(FrontDeskError):
    """OPD mode only: an idempotency key that already names a visit for a
    DIFFERENT patient or clinic than the request names.

    Raised by register_opd_visit(on_replay=...) before that visit is serialized,
    so nothing about it reaches the caller. The message carries no patient
    detail (a caller that does not know this subclass maps it to a plain 422);
    `visit_id` is for the route's audit row only, never for the response.
    """

    def __init__(self, visit_id: str):
        super().__init__(
            "This registration key already belongs to a different registration."
        )
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
)
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
    return datetime.now(timezone.utc).isoformat()


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


def _clean(value: object, limit: int) -> Optional[str]:
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
    return {k: v for k, v in _row_to_dict(row).items()
            if k not in _VISIT_COLUMNS_NOT_IN_API}


def _movement_api_dict(row) -> dict:
    """A movement row as the API has always returned it: the migration 51
    columns left out, every other key in its original order."""
    return {k: v for k, v in _row_to_dict(row).items()
            if k not in _MOVEMENT_COLUMNS_NOT_IN_API}


# ═════════════════════════════════════════════════════════════════════════════
#  Ward cover — the anti-orphaning list
# ═════════════════════════════════════════════════════════════════════════════

def list_covering_departments() -> list[str]:
    """Every department at least one ACTIVE clinical account covers.

    THIS IS THE ANTI-ORPHANING LIST, and it is derived from exactly the column
    `permissions.department_scope` reads (`users.department`) so the two answers
    cannot drift. A department absent from this list is a department whose
    patients would appear on nobody's board.

    Accounts with NO department are excluded on purpose: by the department_scope
    convention a blank department means "hospital-wide governance account", not
    "covers every ward" — such an account reading across wards is not the same
    thing as a ward having staff on it.
    """
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
    return [r["department"] for r in rows]


def _resolve_covering_department(name: str) -> Optional[str]:
    """Map a typed department onto its CANONICAL covered spelling, or None.

    Case- and whitespace-insensitive, because a clerk typing "cardiology" must
    not create an invisible ward that differs from "Cardiology" by one byte. The
    value STORED is always the canonical string from `users.department`, so the
    equality test in PATIENT_DEPARTMENT_SQL matches exactly.
    """
    wanted = (name or "").strip().casefold()
    if not wanted:
        return None
    for canonical in list_covering_departments():
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


def list_transfer_destinations() -> list[dict]:
    """Where a patient may be transferred TO, with the wards/beds in use there.

    The department list is the covering list (above) — it is the set of legal
    destinations, not a suggestion. `wards` is a convenience for the UI, gathered
    from the ward labels already in use on active admissions in that department;
    it is free text and is NOT validated on transfer (a genuinely new bay in a
    covered department is legitimate). The DEPARTMENT is what is validated,
    because the department is what carries the access boundary.
    """
    ensure_frontdesk_schema()
    departments = list_covering_departments()
    if not departments:
        return []
    with db_session() as db:
        rows = db.execute(
            """SELECT a.department AS department, a.ward AS ward, COUNT(*) AS occupied
                 FROM admissions a
                WHERE a.status = 'active' AND a.ward IS NOT NULL AND TRIM(a.ward) != ''
                GROUP BY a.department, a.ward""",
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


def list_consulting_doctors(department: Optional[str] = None) -> list[dict]:
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
    """
    ensure_frontdesk_schema()
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
    return [
        {"id": r["id"], "name": r["name"], "department": r["department"]}
        for r in rows
    ]


# ═════════════════════════════════════════════════════════════════════════════
#  OPD — registration, queue, lifecycle
# ═════════════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class ConsultationCharge:
    """The consultation charge that was (or was not) raised for a visit."""
    charge_id: Optional[int]
    service_code: str
    description: str
    rate_paise: Optional[int]
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
            "total_display": format_inr_or_unset(
                self.total_paise if self.configured else None
            ),
            "gst_treatment": self.gst_treatment,
            "rate_source": self.source,
        }


def _patient_row(db, patient_id: str):
    return db.execute(
        "SELECT id, name, mrn, age, sex, ward, bed, status FROM patients WHERE id = ?",
        (patient_id,),
    ).fetchone()


def _patient_has_admission(db, patient_id: str) -> bool:
    return db.execute(
        "SELECT 1 FROM admissions WHERE patient_id = ? LIMIT 1", (patient_id,)
    ).fetchone() is not None


def _patient_has_active_admission(db, patient_id: str) -> bool:
    return db.execute(
        "SELECT 1 FROM admissions WHERE patient_id = ? AND status = 'active' LIMIT 1",
        (patient_id,),
    ).fetchone() is not None


def opd_mode() -> bool:
    """Is the 'opd' feature on right now (ORB_OPD with its prerequisites)?

    Read at call time. Everything this module does differently for outpatients
    under that feature is behind this one question; with it off the front desk
    behaves exactly as it did before 3 October 2026.
    """
    return config.feature_enabled("opd")


def opd_registration_allowed(db, patient_id: str, dept: Optional[str]) -> bool:
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


def _next_token(db, *, visit_date: str, department: str, doctor_id: Optional[str]) -> int:
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
    db,
    *,
    visit_id: str,
    patient_id: str,
    department: str,
    service_code: str,
    tariff_plan_id: Optional[int],
    charged_by: Optional[str],
    service_day: str,
    charged_at: str,
    interstate: bool = False,
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
    """
    from services.opd_charges_service import raise_op_line_on

    # Idempotent: one consultation charge per visit, whatever replays.
    line = raise_op_line_on(
        db,
        patient_id=patient_id,
        service_code=service_code,
        tariff_plan_id=tariff_plan_id,
        service_day=service_day,      # service_date: the ward's calendar day
        charged_at=charged_at,        # charged_at: the UTC instant
        charged_by=charged_by,
        idempotency_key=f"opd_visit:{visit_id}:consultation",
        source_ref=visit_id,
        description_fallback=f"{department} consultation",
        unpriced_note="Rate not configured at time of consultation",
        interstate=interstate,
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
    doctor_id: Optional[str] = None,
    visit_type: str = "new",
    chief_complaint: Optional[str] = None,
    appointment_id: Optional[str] = None,
    notes: Optional[str] = None,
    registered_by: Optional[str] = None,
    idempotency_key: Optional[str] = None,
    visit_date: Optional[str] = None,
    self_registered: bool = False,
    on_registered: Optional[Callable[[Any, dict], None]] = None,
    on_replay: Optional[Callable[[Any, dict, dict], None]] = None,
    identity_check: Optional[dict] = None,
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
                    (prior["department"] or "").strip().casefold()
                    == canonical_dept.casefold()
                )
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

        visit_id = f"OPD-{uuid.uuid4().hex[:8].upper()}"
        token = _next_token(
            db, visit_date=day, department=canonical_dept, doctor_id=doctor
        )
        now = _now()
        db.execute(
            """INSERT INTO opd_visits (
                    id, patient_id, department, doctor_id, doctor_name, visit_date,
                    token_number, visit_type, chief_complaint, status, appointment_id,
                    registered_by, registered_at, notes, idempotency_key
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'waiting', ?, ?, ?, ?, ?)""",
            (
                visit_id, patient_id, canonical_dept, doctor, doctor_name, day,
                token, visit_type, complaint, _clean(appointment_id, _MAX_LABEL_LEN),
                registered_by, now, note, idem,
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


def _opd_charges_at_registration(db, visit_id: str,
                                 registered_by: Optional[str]) -> Optional[dict]:
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
        db, visit=visit, charged_by=registered_by)
    return {**line.as_dict(), **choice.as_facts(), "charged_at": "registration"}


def _serialize_visit(db, row, *, now: Optional[datetime] = None) -> dict:
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
            reissued_tokens(db, [visit_id]).get(visit_id) if data.get("doctor_id") else None)
    return data


def _parse_ts(value: object) -> Optional[datetime]:
    if not value:
        return None
    text = str(value).replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _waiting_minutes(visit: dict, *, now: Optional[datetime] = None) -> Optional[int]:
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
            end = now or datetime.now(timezone.utc)
    return max(0, int((end - start).total_seconds() // 60))


def _consultation_minutes(visit: dict, *, now: Optional[datetime] = None) -> Optional[int]:
    called = _parse_ts(visit.get("called_at"))
    if called is None:
        return None
    end = _parse_ts(visit.get("completed_at")) or (now or datetime.now(timezone.utc))
    return max(0, int((end - called).total_seconds() // 60))


def list_opd_queue(
    *,
    department: Optional[str] = None,
    visit_date: Optional[str] = None,
    doctor_id: Optional[str] = None,
    statuses: Optional[tuple[str, ...]] = None,
) -> dict:
    """The queue, grouped by consulting doctor, ordered by token.

    `department` is the CALLER'S scope as derived by
    `permissions.department_scope` — None means an unscoped governance account
    and returns every clinic. It is never a department the request named; the
    route enforces that.
    """
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

    now = datetime.now(timezone.utc)
    with db_session() as db:
        rows = db.execute(
            f"""SELECT v.*, p.name AS patient_name, p.mrn AS patient_mrn,
                       p.age AS patient_age, p.sex AS patient_sex
                  FROM opd_visits v
                  LEFT JOIN patients p ON p.id = v.patient_id
                 WHERE {' AND '.join(clauses)}
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
        visits.sort(key=lambda v: (v["department"], v.get("doctor_name") or "",
                                   *oq.queue_sort_key(v)))

    queues: list[dict] = []
    index: dict[tuple, int] = {}
    for v in visits:
        key = (v["department"], v.get("doctor_id") or "")
        if key not in index:
            index[key] = len(queues)
            queues.append({
                "department": v["department"],
                "doctor_id": v.get("doctor_id"),
                "doctor_name": v.get("doctor_name") or "Unassigned",
                "visits": [],
            })
        queues[index[key]]["visits"].append(v)

    for q in queues:
        waits = [v["waiting_minutes"] for v in q["visits"]
                 if v["status"] == "waiting" and v["waiting_minutes"] is not None]
        q["waiting_count"] = sum(1 for v in q["visits"] if v["status"] == "waiting")
        q["in_consultation_count"] = sum(
            1 for v in q["visits"] if v["status"] == "in_consultation")
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
        result.update({
            "opd_mode": True,
            "poll_seconds": int(getattr(config, "OPD_QUEUE_POLL_SECONDS", 30) or 30),
            "priorities": [{"code": c, "label": lab} for c, lab in oq.priority_categories()],
            "cancel_reasons": [{"code": c, "label": lab} for c, lab in oq.CANCEL_REASON_CODES],
        })
    return result


def get_opd_visit(visit_id: str) -> Optional[dict]:
    ensure_frontdesk_schema()
    with db_session() as db:
        row = db.execute("SELECT * FROM opd_visits WHERE id = ?", (visit_id,)).fetchone()
        if row is None:
            return None
        return _serialize_visit(db, row)


def start_opd_consultation(
    visit_id: str,
    *,
    called_by: Optional[str] = None,
    on_done: Optional[Callable[[Any, dict, dict], None]] = None,
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
            raise FrontDeskError(
                f"Visit {visit_id} is {row['status']} and cannot be called in"
            )
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
        updated = db.execute(
            "SELECT * FROM opd_visits WHERE id = ?", (visit_id,)
        ).fetchone()
        logger.info("OPD visit %s called in by %s", visit_id, called_by)
        visit = _serialize_visit(db, updated)
        if on_done is not None:
            on_done(db, visit, {"replay": False})
        return visit


def complete_opd_visit(
    visit_id: str,
    *,
    completed_by: Optional[str] = None,
    service_code: Optional[str] = None,
    raise_charge: bool = True,
    notes: Optional[str] = None,
    on_done: Optional[Callable[[Any, dict, dict], None]] = None,
    disposition: Optional[str] = None,
    before_complete: Optional[Callable[[Any, Any], None]] = None,
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
            raise FrontDeskError(
                f"Visit {visit_id} was cancelled and cannot be completed"
            )

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

        charge: Optional[ConsultationCharge] = None
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
                db, {k: row[k] for k in row.keys()}, requested_code=service_code)
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

        updated = db.execute(
            "SELECT * FROM opd_visits WHERE id = ?", (visit_id,)
        ).fetchone()
        result = _serialize_visit(db, updated)
        if on_done is not None:
            result["consultation_charge"] = charge.as_dict() if charge else None
            on_done(db, result, {
                "replay": already_complete,
                "charge_raised_now": bool(charge and charge.newly_raised),
                **charge_facts,
            })

    result["consultation_charge"] = charge.as_dict() if charge else None
    return result


def cancel_opd_visit(
    visit_id: str,
    *,
    reason: str,
    cancelled_by: Optional[str] = None,
    on_done: Optional[Callable[[Any, dict, dict], None]] = None,
    reason_code: Optional[str] = None,
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
            raise FrontDeskError(
                f"Visit {visit_id} is completed and cannot be cancelled"
            )
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
            facts.update(void_prepaid_consultation_on(
                db, visit_row=row, actor=cancelled_by, reason=clean_reason))
            if row["appointment_id"]:
                # opd-16: the appointment checked in to this visit is closed
                # with it, saying why.
                db.execute(
                    "UPDATE appointments SET status = 'cancelled', cancel_reason = ?, "
                    "cancelled_at = ?, cancelled_by = ? WHERE id = ? "
                    "AND status = 'checked_in' AND opd_visit_id = ?",
                    (f"The outpatient visit was cancelled: {clean_reason}"[:_MAX_REASON_LEN],
                     _now(), cancelled_by, row["appointment_id"], visit_id),
                )
        updated = db.execute(
            "SELECT * FROM opd_visits WHERE id = ?", (visit_id,)
        ).fetchone()
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
    to_ward: Optional[str] = None,
    to_bed: Optional[str] = None,
    reason: str,
    moved_by: Optional[str] = None,
    moved_by_name: Optional[str] = None,
    notes: Optional[str] = None,
    idempotency_key: Optional[str] = None,
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
        raise FrontDeskError(
            "A transfer reason is required. A move with no reason is not a record"
        )

    canonical_dept = _resolve_covering_department(to_department)
    if canonical_dept is None:
        raise FrontDeskError(uncovered_department_words(to_department))

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
            raise FrontDeskError(
                f"{patient_id} is already in {canonical_dept} at that location"
            )

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
                movement_id, patient_id, admission["id"],
                from_department, from_ward, from_bed,
                canonical_dept, ward if ward is not None else from_ward,
                bed if bed is not None else from_bed,
                clean_reason, now, moved_by, moved_by_name, note, idem,
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
            "UPDATE patients SET ward = COALESCE(?, ward), bed = COALESCE(?, bed) "
            "WHERE id = ?",
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
