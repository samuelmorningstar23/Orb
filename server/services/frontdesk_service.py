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

import logging
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from database import db_session
from services.billing_masters import resolve_patient_payer, resolve_rate
from services.hospital_time import hospital_timezone, hospital_today
from services.money import compute_line

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
]


class FrontDeskError(ValueError):
    """A front-desk request that cannot be honoured (bad ward, bad state)."""


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
# NOTE ON PLACEMENT (deliberate, and a followup): the house rule is that schema
# lives in a numbered migration in server/migrations.py. These two tables are
# created here instead because migration 20 (the hospital-admin spine) was
# authored as ONE migration for five modules built in parallel, explicitly so
# that no module agent picks a migration version — two agents choosing version 21
# in the same night produces a duplicate PRIMARY KEY in `schema_migrations` and a
# server that will not boot. `CREATE TABLE IF NOT EXISTS` cannot collide. Folding
# these into a numbered migration once the spine's version numbers are settled is
# a mechanical change and is recorded as a followup.
#
# Same rules as migration 20: additive, ZERO foreign keys (an FK from a front-desk
# row to `patients` would make a patient row undeletable and could fail a clinical
# table rebuild), no REAL columns, money in `_paise`.

_FRONTDESK_TABLES: list[str] = [
    """CREATE TABLE IF NOT EXISTS opd_visits (
        id TEXT PRIMARY KEY,
        patient_id TEXT NOT NULL,
        -- The CLINIC this visit belongs to. This is the scope key for the queue:
        -- an outpatient has no admission, so permissions.PATIENT_DEPARTMENT_SQL
        -- cannot derive a department for them. It is compared against
        -- permissions.department_scope(user) — the same authenticated-user
        -- derivation, never a department named by the request.
        department TEXT NOT NULL,
        doctor_id TEXT,
        -- Display snapshot of the consulting doctor's name at registration.
        -- NEVER a scope source; the id is the identity.
        doctor_name TEXT,
        visit_date TEXT NOT NULL,           -- YYYY-MM-DD, hospital-local
        token_number INTEGER NOT NULL,
        visit_type TEXT NOT NULL DEFAULT 'new' CHECK (visit_type IN (
            'new','follow_up','review','procedure')),
        chief_complaint TEXT,
        status TEXT NOT NULL DEFAULT 'waiting' CHECK (status IN (
            'waiting','in_consultation','completed','cancelled')),
        appointment_id TEXT,
        registered_by TEXT,
        registered_at TIMESTAMP NOT NULL,
        called_at TIMESTAMP,
        completed_at TIMESTAMP,
        cancelled_at TIMESTAMP,
        cancel_reason TEXT,
        -- encounter_charges.id of the consultation charge, once raised.
        consultation_charge_id INTEGER,
        notes TEXT,
        idempotency_key TEXT
    )""",

    """CREATE TABLE IF NOT EXISTS patient_movements (
        id TEXT PRIMARY KEY,
        patient_id TEXT NOT NULL,
        -- The admission the patient STAYS on. A transfer never opens a new one:
        -- clinical continuity (orders, eMAR, vitals, the case room) hangs off
        -- this id.
        admission_id TEXT,
        movement_type TEXT NOT NULL DEFAULT 'transfer' CHECK (movement_type IN (
            'transfer','admission','discharge')),
        from_department TEXT,
        from_ward TEXT,
        from_bed TEXT,
        to_department TEXT,
        to_ward TEXT,
        to_bed TEXT,
        reason TEXT NOT NULL,
        moved_at TIMESTAMP NOT NULL,
        moved_by TEXT,
        moved_by_name TEXT,
        notes TEXT,
        idempotency_key TEXT
    )""",
]

_FRONTDESK_INDEXES: list[str] = [
    "CREATE INDEX IF NOT EXISTS idx_opd_queue ON opd_visits("
    "visit_date, department, status, token_number)",
    "CREATE INDEX IF NOT EXISTS idx_opd_doctor_day ON opd_visits("
    "doctor_id, visit_date, token_number)",
    "CREATE INDEX IF NOT EXISTS idx_opd_patient ON opd_visits(patient_id, visit_date DESC)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_opd_idempotency ON opd_visits(idempotency_key) "
    "WHERE idempotency_key IS NOT NULL",
    # One token number per doctor per day. A partial index over live rows only:
    # a cancelled visit must not hold its token hostage.
    #
    # KNOWN GAP, stated rather than papered over: SQLite treats NULLs as DISTINCT
    # in a unique index, so this does not constrain the "Unassigned doctor" queue
    # (doctor_id IS NULL). What actually serialises token allocation is
    # `db_session(immediate=True)` in register_opd_visit — the index is the
    # second line of defence, and it covers every named doctor.
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_opd_token_unique ON opd_visits("
    "visit_date, department, doctor_id, token_number) WHERE status != 'cancelled'",
    "CREATE INDEX IF NOT EXISTS idx_movements_patient ON patient_movements("
    "patient_id, moved_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_movements_admission ON patient_movements("
    "admission_id, moved_at DESC)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_movements_idempotency ON patient_movements("
    "idempotency_key) WHERE idempotency_key IS NOT NULL",
]

_schema_ready = False
_schema_lock = threading.Lock()


def ensure_frontdesk_schema(force: bool = False) -> None:
    """Create the two front-desk tables if absent, and register this module's
    consultation service item on the billing price list (unpriced, never
    overwriting a row the site has touched). Idempotent, cached, thread-safe.

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
            for statement in _FRONTDESK_TABLES:
                db.execute(statement)
            for statement in _FRONTDESK_INDEXES:
                db.execute(statement)
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
    """
    if dept is None:
        return True
    if not patient_id:
        return False
    if not _patient_has_admission(db, patient_id):
        return True
    from permissions import patient_in_scope
    return patient_in_scope(db, patient_id, dept)


def _next_token(db, *, visit_date: str, department: str, doctor_id: Optional[str]) -> int:
    """The next token for this doctor's clinic today.

    Tokens are per (date, department, doctor) and start at 1 each day. Cancelled
    visits are counted in MAX() on purpose: reusing a cancelled patient's token
    number the same morning means two people were called by the same number, and
    the queue display is the only instruction most OPD patients get.
    """
    row = db.execute(
        """SELECT MAX(token_number) AS top FROM opd_visits
            WHERE visit_date = ? AND department = ?
              AND COALESCE(doctor_id, '') = COALESCE(?, '')""",
        (visit_date, department, doctor_id),
    ).fetchone()
    top = (row["top"] if row else None) or 0
    return int(top) + 1


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
    """
    resolution = resolve_rate(
        service_code,
        tariff_plan_id=tariff_plan_id,
        ward_class=None,
        on_date=service_day,
    )
    description = resolution.description or f"{department} consultation"
    configured = bool(resolution.configured and resolution.rate_paise is not None)
    unit_rate = int(resolution.rate_paise) if configured else 0

    line = compute_line(
        unit_rate_paise=unit_rate,
        gst_rate_bp=resolution.gst_rate_bp if configured else 0,
        gst_treatment=resolution.gst_treatment,
        interstate=interstate,
    )

    # Idempotent: one consultation charge per visit, whatever replays.
    idem = f"opd_visit:{visit_id}:consultation"
    existing = db.execute(
        "SELECT id FROM encounter_charges WHERE idempotency_key = ?", (idem,)
    ).fetchone()
    if existing:
        charge_id = int(existing["id"])
    else:
        cursor = db.execute(
            """INSERT INTO encounter_charges (
                    patient_id, admission_id, encounter_type, service_item_id,
                    service_code, description, quantity_milli, unit_rate_paise,
                    rate_is_configured, tariff_plan_id, ward_class, gross_paise,
                    discount_paise, gst_treatment, gst_rate_bp, taxable_value_paise,
                    cgst_paise, sgst_paise, igst_paise, total_paise, status,
                    source, source_ref, service_date, charged_by, charged_at,
                    idempotency_key, notes
               ) VALUES (?, NULL, 'op', ?, ?, ?, 1000, ?, ?, ?, NULL, ?, ?, ?, ?, ?,
                         ?, ?, ?, ?, 'pending', 'manual', ?, ?, ?, ?, ?, ?)""",
            (
                patient_id,
                resolution.service_item_id,
                service_code,
                description,
                unit_rate,
                1 if configured else 0,
                resolution.tariff_plan_id,
                line.gross_paise,
                line.discount_paise,
                line.gst_treatment,
                line.gst_rate_bp,
                line.taxable_value_paise,
                line.cgst_paise,
                line.sgst_paise,
                line.igst_paise,
                line.total_paise,
                visit_id,
                service_day,      # service_date — the ward's calendar day
                charged_by,
                charged_at,       # charged_at   — the UTC instant
                idem,
                None if configured else "Rate not configured at time of consultation",
            ),
        )
        charge_id = int(cursor.lastrowid)

    return ConsultationCharge(
        charge_id=charge_id,
        service_code=service_code,
        description=description,
        rate_paise=int(resolution.rate_paise) if configured else None,
        configured=configured,
        total_paise=line.total_paise,
        gst_treatment=line.gst_treatment,
        source=resolution.source,
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
) -> dict:
    """Register an outpatient visit and put it in the queue.

    The department is resolved against the COVERING list for the same reason a
    transfer is: an OPD queue in a department nobody staffs is a patient sitting
    in a corridor on a list nobody opens. Callers must have already checked that
    the CALLER is allowed to register into this department (the route does that,
    against `permissions.department_scope`) — this function validates that the
    department is real.
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
                return _serialize_visit(db, prior)

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
        row = db.execute("SELECT * FROM opd_visits WHERE id = ?", (visit_id,)).fetchone()
        return _serialize_visit(db, row)


def _serialize_visit(db, row, *, now: Optional[datetime] = None) -> dict:
    """API shape for one visit, including its live waiting time."""
    if row is None:
        return {}
    data = _row_to_dict(row)
    patient = _patient_row(db, data.get("patient_id") or "")
    data["patient_name"] = patient["name"] if patient else None
    data["patient_mrn"] = patient["mrn"] if patient else None
    data["patient_age"] = patient["age"] if patient else None
    data["patient_sex"] = patient["sex"] if patient else None
    data["waiting_minutes"] = _waiting_minutes(data, now=now)
    data["consultation_minutes"] = _consultation_minutes(data, now=now)
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

    visits: list[dict] = []
    for r in rows:
        data = _row_to_dict(r)
        data["waiting_minutes"] = _waiting_minutes(data, now=now)
        data["consultation_minutes"] = _consultation_minutes(data, now=now)
        visits.append(data)

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

    return {
        "visit_date": day,
        "department": department,
        "queues": queues,
        "total": len(visits),
    }


def get_opd_visit(visit_id: str) -> Optional[dict]:
    ensure_frontdesk_schema()
    with db_session() as db:
        row = db.execute("SELECT * FROM opd_visits WHERE id = ?", (visit_id,)).fetchone()
        if row is None:
            return None
        return _serialize_visit(db, row)


def start_opd_consultation(visit_id: str, *, called_by: Optional[str] = None) -> dict:
    """Call the next patient in — 'waiting' → 'in_consultation'. Idempotent."""
    ensure_frontdesk_schema()
    with db_session(immediate=True) as db:
        row = db.execute("SELECT * FROM opd_visits WHERE id = ?", (visit_id,)).fetchone()
        if row is None:
            raise FrontDeskError(f"No such OPD visit: {visit_id}")
        if row["status"] == "in_consultation":
            return _serialize_visit(db, row)
        if row["status"] != "waiting":
            raise FrontDeskError(
                f"Visit {visit_id} is {row['status']} and cannot be called in"
            )
        db.execute(
            "UPDATE opd_visits SET status = 'in_consultation', called_at = ? WHERE id = ?",
            (_now(), visit_id),
        )
        updated = db.execute(
            "SELECT * FROM opd_visits WHERE id = ?", (visit_id,)
        ).fetchone()
        logger.info("OPD visit %s called in by %s", visit_id, called_by)
        return _serialize_visit(db, updated)


def complete_opd_visit(
    visit_id: str,
    *,
    completed_by: Optional[str] = None,
    service_code: Optional[str] = None,
    raise_charge: bool = True,
    notes: Optional[str] = None,
) -> dict:
    """Finish the consultation and raise the consultation charge.

    The charge is raised HERE and not at registration because a patient who
    registers and leaves before being seen has not been consulted. Idempotent on
    both halves: a replay returns the completed visit and does not double-charge
    (the `encounter_charges` idempotency key is derived from the visit id).
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

        charge: Optional[ConsultationCharge] = None
        if raise_charge:
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

        updated = db.execute(
            "SELECT * FROM opd_visits WHERE id = ?", (visit_id,)
        ).fetchone()
        result = _serialize_visit(db, updated)

    result["consultation_charge"] = charge.as_dict() if charge else None
    return result


def cancel_opd_visit(
    visit_id: str, *, reason: str, cancelled_by: Optional[str] = None
) -> dict:
    """Cancel a visit (no-show, left without being seen). Soft — the row stays."""
    ensure_frontdesk_schema()
    clean_reason = _clean(reason, _MAX_REASON_LEN)
    if not clean_reason:
        raise FrontDeskError("A cancellation reason is required")
    with db_session(immediate=True) as db:
        row = db.execute("SELECT * FROM opd_visits WHERE id = ?", (visit_id,)).fetchone()
        if row is None:
            raise FrontDeskError(f"No such OPD visit: {visit_id}")
        if row["status"] == "cancelled":
            return _serialize_visit(db, row)
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
        updated = db.execute(
            "SELECT * FROM opd_visits WHERE id = ?", (visit_id,)
        ).fetchone()
        logger.info("OPD visit %s cancelled by %s: %s", visit_id, cancelled_by, clean_reason)
        return _serialize_visit(db, updated)


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
        raise FrontDeskError(
            f"{to_department!r} is not a department with clinical cover. "
            "Transferring there would leave the patient admitted on a ward that "
            "appears on nobody's board."
        )

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
                return _transfer_result(db, patient_id, _row_to_dict(prior))

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
        return _transfer_result(db, patient_id, _row_to_dict(movement))


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
    return [_row_to_dict(r) for r in rows]
