"""Orb: outpatient charges (opd-13, W7-A, 9 October 2026).

What an outpatient is charged, decided from rows a site sets and never guessed.
Behind the 'opd' feature (ORB_OPD) for everything about a visit, and behind
the 'registration' feature for the registration fee, both off by default. With
them off the front desk charges exactly what it always did: one OPD-CONSULT
line (or the code its caller names) when a visit is completed.

THE RULES (migration 67, opd_charge_rules; configuration, no patient data)
  * consultation: the code a visit's consultation is charged against. The
    doctor's own rule wins, then the department's, then a hospital-wide one; a
    rule for one visit type beats a rule for every type at the same level.
    With no rule the code is OPD-CONSULT, as before.
  * follow_up: a visit in the same clinic within a rule's window (days) after
    a COMPLETED visit is charged against the rule's code, OPD-FOLLOWUP by
    default (ruling OPD-N6: a follow-up window maps to a code the site prices,
    never to an implicit zero). A doctor's rule counts only that doctor's
    visits; a department or hospital-wide rule counts the clinic's. The window
    is counted in hospital-local calendar days (opd_visits.visit_date), so a
    visit at 02:00 IST is on the right day.
  * registration: the registration fee, REG-FEE by default, raised ONCE per
    patient when the registration desk registers them, and only while a
    registration rule exists. No rule, no fee.
  A caller that names a code (the completion routes' service_code) keeps it.

UNPRICED IS NOT FREE. Every line is priced through billing_masters.resolve_rate
under the patient's payer; a code nobody priced is raised with
rate_is_configured 0 and zero totals for the counter to price, exactly as the
front desk's consultation line always was. OPD-FOLLOWUP and REG-FEE are put on
the price list UNPRICED the first time a rule names them; any other code must
already be on the price list.

THE PAYER. The registration desk may record the payer category the patient
names (a patient_payer_policies row with no admission, unverified), and in OPD
mode every new visit keeps a snapshot of the payer and tariff it was
registered under (opd_visits.payer_id, tariff_plan_id), so its lines are
priced on that tariff even if the payer changes later.

WHEN THE CONSULTATION IS CHARGED (config.OPD_CHARGE_AT, ruling OPD-N6):
  * 'completion' (the default): when the visit is completed, as before. A
    visit cancelled or left without being seen raises nothing.
  * 'registration' (prepaid): the line is raised when the visit is registered,
    once per visit whatever replays (the visit's idempotency key), and the
    visit records it in registration_charge_id. A later cancel, a 'left
    without being seen' included, voids that line through
    billing_service.cancel_charge (audited there) while it is still pending;
    a line already on an invoice is left for a refund through the payments
    desk, and the cancel says so.

Every line this module raises writes a 'billing' audit row in the
transaction that raised it (billing is not fail-closed: a counter must not
lose a registration over an audit outage). Every rule change writes a
'configuration' audit row in its own transaction.

Plain sync throughout. Functions named *_on take the caller's connection and
never commit; the rest open their own session.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

import config
import migrations  # noqa: F401  (loads migrations_his in version order first)
from database import db_session
from migrations_his.m0067_opd_charge_rules import (
    MAX_FOLLOW_UP_WINDOW_DAYS,
    RULE_KINDS,
    VISIT_TYPES,
)

from services import billing_masters
from services.audit_service import log_audit_event
from services.clinical_time import utc_iso_now
from services.money import compute_line

logger = logging.getLogger("orb.opd_charges")

__all__ = [
    "CONSULTATION_CODE",
    "FOLLOW_UP_CODE",
    "REGISTRATION_CODE",
    "DEFAULT_CODES",
    "CHARGE_AT_CHOICES",
    "OpdChargeError",
    "CodeChoice",
    "ChargeLine",
    "charge_at",
    "list_rules",
    "create_rule",
    "retire_rule",
    "resolve_consultation_code_on",
    "registration_fee_rule_on",
    "raise_op_line_on",
    "raise_registration_fee_on",
    "raise_consultation_at_registration_on",
    "void_prepaid_consultation_on",
    "payer_snapshot_on",
]

CONSULTATION_CODE = "OPD-CONSULT"
FOLLOW_UP_CODE = "OPD-FOLLOWUP"
REGISTRATION_CODE = "REG-FEE"

#: The code a rule of each kind charges when it names none.
DEFAULT_CODES: dict = {
    "consultation": CONSULTATION_CODE,
    "follow_up": FOLLOW_UP_CODE,
    "registration": REGISTRATION_CODE,
}

#: The items this module puts on the price list, unpriced, the first time a
#: rule needs them: (name, category, note on the price list).
DEFAULT_ITEMS: dict = {
    FOLLOW_UP_CODE: (
        "OPD follow-up consultation",
        "consultation",
        "Charged for an outpatient visit inside a follow-up window. Set the rate "
        "here; the front desk never invents one.",
    ),
    REGISTRATION_CODE: (
        "Registration fee",
        "other",
        "Charged once, when the registration desk first registers a patient. Set "
        "the rate here; the desk never invents one.",
    ),
}

CHARGE_AT_CHOICES = ("completion", "registration")

#: The audit category of a rule change (configuration, no patient data).
AUDIT_CATEGORY = "configuration"

REASON_MIN_CHARS = 8
_MAX_CODE_LEN = 60
_MAX_NOTE_LEN = 500
_MAX_TEXT = 200


class OpdChargeError(ValueError):
    """A refusal with the HTTP status it maps to and a machine-readable code.
    The message is plain words for the desk."""

    def __init__(self, code: str, message: str, *, status: int = 422, **extra: Any):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.extra = extra

    def detail(self) -> dict:
        return {"error": self.code, "message": self.message, **self.extra}


def charge_at() -> str:
    """When the consultation is charged: 'completion' or 'registration'.
    Read at call time, so a test or an ops restart sees the current setting."""
    value = str(getattr(config, "OPD_CHARGE_AT", "completion") or "completion").strip().lower()
    return value if value in CHARGE_AT_CHOICES else "completion"


def _clean(value: Any, limit: int = _MAX_TEXT) -> str | None:
    if value is None:
        return None
    text = " ".join(str(value).split())
    if not text:
        return None
    if len(text) > limit:
        raise OpdChargeError("value_too_long", f"A value is longer than {limit} characters.")
    return text


# ═════════════════════════════════════════════════════════════════════════════
#  Rules
# ═════════════════════════════════════════════════════════════════════════════

_RULE_COLUMNS = (
    "id",
    "rule_kind",
    "department",
    "doctor_id",
    "visit_type",
    "service_code",
    "follow_up_window_days",
    "is_active",
    "notes",
    "created_by",
    "created_at",
    "retired_by",
    "retired_at",
    "retired_reason",
)


def _rule_dict(row, names: dict | None = None, items: dict | None = None) -> dict:
    data = {k: row[k] for k in _RULE_COLUMNS}
    data["is_active"] = bool(data["is_active"])
    names = names or {}
    data["doctor_name"] = names.get(data["doctor_id"]) if data["doctor_id"] else None
    item = (items or {}).get(data["service_code"])
    data["service_name"] = item["name"] if item else None
    data["service_priced"] = bool(item and int(item["rate_is_configured"] or 0))
    data["scope"] = (
        "doctor" if data["doctor_id"] else "department" if data["department"] else "hospital"
    )
    return data


def _decorate(db, rows) -> list[dict]:
    doctor_ids = sorted({r["doctor_id"] for r in rows if r["doctor_id"]})
    names: dict = {}
    if doctor_ids:
        marks = ",".join("?" for _ in doctor_ids)
        names = {
            r["id"]: r["name"]
            for r in db.execute(
                f"SELECT id, name FROM users WHERE id IN ({marks})", doctor_ids
            ).fetchall()
        }
    codes = sorted({r["service_code"] for r in rows})
    items: dict = {}
    if codes:
        marks = ",".join("?" for _ in codes)
        items = {
            r["code"]: r
            for r in db.execute(
                f"SELECT code, name, rate_is_configured FROM service_items WHERE code IN ({marks})",
                codes,
            ).fetchall()
        }
    return [_rule_dict(r, names, items) for r in rows]


def list_rules(*, include_retired: bool = False) -> dict:
    """Every charge rule (live ones first), with the doctor's name, whether the
    code is priced, and when the consultation is charged. No patient data."""
    sql = "SELECT * FROM opd_charge_rules"
    if not include_retired:
        sql += " WHERE is_active = 1"
    sql += " ORDER BY is_active DESC, rule_kind, created_at, id"
    with db_session() as db:
        rows = db.execute(sql).fetchall()
        rules = _decorate(db, rows)
    return {
        "rules": rules,
        "charge_at": charge_at(),
        "defaults": dict(DEFAULT_CODES),
        "rule_kinds": list(RULE_KINDS),
        "visit_types": list(VISIT_TYPES),
        "max_follow_up_window_days": MAX_FOLLOW_UP_WINDOW_DAYS,
    }


def _audit_rule(
    db,
    *,
    action: str,
    actor: dict,
    rule_id: str,
    summary: str,
    metadata: dict,
    request_meta: dict | None,
) -> None:
    meta = dict(request_meta or {})
    log_audit_event(
        correlation_id=f"opd-charge-rule-{rule_id}",
        event_category=AUDIT_CATEGORY,
        action_type=action,
        outcome="success",
        db=db,
        summary_message=summary,
        actor_user_id=(actor or {}).get("id"),
        actor_role=(actor or {}).get("role"),
        target_type="opd_charge_rule",
        target_id=str(rule_id),
        request_path=meta.get("request_path"),
        http_method=meta.get("http_method"),
        ip_address=meta.get("ip_address"),
        user_agent=meta.get("user_agent"),
        metadata=metadata,
    )


def _consulting_roles() -> tuple:
    from permissions import OPD_CONSULT, ROLE_PERMISSIONS

    return tuple(sorted(r for r, perms in ROLE_PERMISSIONS.items() if OPD_CONSULT in perms))


def create_rule(fields: Mapping, *, actor: dict, request_meta: dict | None = None) -> dict:
    """Add one charge rule. Refusals (OpdChargeError): bad_rule_kind,
    bad_visit_type, bad_window, registration_rule_is_hospital_wide,
    doctor_or_department, follow_up_takes_no_visit_type,
    not_a_consulting_doctor, unknown_department, unknown_service_code (422)
    and rule_exists (409, with the live rule's id)."""
    fields = dict(fields or {})
    kind = str(fields.get("rule_kind") or "").strip().lower()
    if kind not in RULE_KINDS:
        raise OpdChargeError("bad_rule_kind", "Choose consultation, follow-up or registration fee.")
    code = _clean(fields.get("service_code"), _MAX_CODE_LEN) or DEFAULT_CODES[kind]
    department = _clean(fields.get("department"))
    doctor_id = _clean(fields.get("doctor_id"), 64)
    visit_type = _clean(fields.get("visit_type"), 20)
    notes = _clean(fields.get("notes"), _MAX_NOTE_LEN)
    window_raw = fields.get("follow_up_window_days")

    if kind == "registration" and (department or doctor_id or visit_type):
        raise OpdChargeError(
            "registration_rule_is_hospital_wide",
            "The registration fee is the same for every clinic. Leave the doctor, "
            "the department and the visit type empty.",
        )
    if department and doctor_id:
        raise OpdChargeError(
            "doctor_or_department",
            "Name a doctor or a department, not both: a doctor's rule already "
            "belongs to that doctor's clinic.",
        )
    if visit_type is not None:
        if kind != "consultation":
            raise OpdChargeError(
                "follow_up_takes_no_visit_type",
                "Only a consultation rule can be for one visit type. A follow-up "
                "is decided by the window.",
            )
        visit_type = visit_type.lower()
        if visit_type not in VISIT_TYPES:
            raise OpdChargeError("bad_visit_type", "Choose new, follow-up, review or procedure.")
    window: int | None = None
    if kind == "follow_up":
        try:
            window = int(window_raw) if window_raw is not None else 0
        except (TypeError, ValueError):
            window = None
        if window is None or not 1 <= window <= MAX_FOLLOW_UP_WINDOW_DAYS:
            raise OpdChargeError(
                "bad_window",
                f"Give the follow-up window in days, from 1 to {MAX_FOLLOW_UP_WINDOW_DAYS}.",
            )
    elif window_raw not in (None, ""):
        raise OpdChargeError("bad_window", "Only a follow-up rule has a window.")

    if department:
        # The covering spelling (users.department), the same resolver the
        # front desk registers a visit with, so the rule and the visit name a
        # clinic one way.
        from services.frontdesk_service import resolve_covering_department

        canonical = resolve_covering_department(department)
        if canonical is None:
            raise OpdChargeError(
                "unknown_department", f"{department!r} is not a department with clinical cover."
            )
        department = canonical

    rule_id = f"OCR-{uuid.uuid4().hex[:12].upper()}"
    now = utc_iso_now()
    with db_session(immediate=True) as db:
        if doctor_id:
            roles = _consulting_roles()
            doctor = db.execute(
                "SELECT id, name, role, TRIM(COALESCE(department, '')) AS department, "
                "COALESCE(is_active, 1) AS active FROM users WHERE id = ?",
                (doctor_id,),
            ).fetchone()
            if (
                doctor is None
                or doctor["role"] not in roles
                or not doctor["active"]
                or not doctor["department"]
            ):
                raise OpdChargeError(
                    "not_a_consulting_doctor",
                    "That account is not an active doctor who holds a clinic.",
                )
        item = db.execute(
            "SELECT code, is_active FROM service_items WHERE code = ?", (code,)
        ).fetchone()
        registered_item = False
        if item is None:
            if code not in DEFAULT_ITEMS:
                raise OpdChargeError(
                    "unknown_service_code",
                    f"{code} is not on the price list. Add it there first, then make the rule.",
                )
            name, category, note = DEFAULT_ITEMS[code]
            registered_item = billing_masters.register_unpriced_item_on(
                db, code=code, name=name, category=category, notes=note
            )
        live = db.execute(
            "SELECT id FROM opd_charge_rules WHERE is_active = 1 AND rule_kind = ? "
            "AND COALESCE(doctor_id, '') = COALESCE(?, '') "
            "AND lower(COALESCE(department, '')) = lower(COALESCE(?, '')) "
            "AND COALESCE(visit_type, '') = COALESCE(?, '')",
            (kind, doctor_id, department, visit_type),
        ).fetchone()
        if live is not None:
            raise OpdChargeError(
                "rule_exists",
                "A live rule already covers this. Retire it first to change it.",
                status=409,
                rule_id=live["id"],
            )
        db.execute(
            """INSERT INTO opd_charge_rules (id, rule_kind, department, doctor_id, visit_type,
                   service_code, follow_up_window_days, is_active, notes, created_by, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)""",
            (
                rule_id,
                kind,
                department,
                doctor_id,
                visit_type,
                code,
                window,
                notes,
                actor.get("id"),
                now,
            ),
        )
        row = db.execute("SELECT * FROM opd_charge_rules WHERE id = ?", (rule_id,)).fetchone()
        rule = _decorate(db, [row])[0]
        _audit_rule(
            db,
            action="opd_charge_rule_created",
            actor=actor,
            rule_id=rule_id,
            summary=f"Outpatient charge rule {rule_id} ({kind}) charges {code}",
            metadata={
                "after": {k: rule[k] for k in _RULE_COLUMNS},
                "price_list_item_registered": registered_item,
            },
            request_meta=request_meta,
        )
    return rule


def retire_rule(
    rule_id: str, *, reason: Any, actor: dict, request_meta: dict | None = None
) -> dict:
    """Retire a live rule with a reason (at least 8 characters). Nothing is
    deleted. 404 rule_not_found, 409 rule_retired, 422 reason_required."""
    text = _clean(reason, _MAX_NOTE_LEN)
    if not text or len(text) < REASON_MIN_CHARS:
        raise OpdChargeError(
            "reason_required", f"Give a reason of at least {REASON_MIN_CHARS} characters."
        )
    now = utc_iso_now()
    with db_session(immediate=True) as db:
        row = db.execute("SELECT * FROM opd_charge_rules WHERE id = ?", (rule_id,)).fetchone()
        if row is None:
            raise OpdChargeError("rule_not_found", "No such charge rule.", status=404)
        if not int(row["is_active"] or 0):
            raise OpdChargeError("rule_retired", "This rule is already retired.", status=409)
        before = {k: row[k] for k in _RULE_COLUMNS}
        db.execute(
            "UPDATE opd_charge_rules SET is_active = 0, retired_by = ?, retired_at = ?, "
            "retired_reason = ? WHERE id = ? AND is_active = 1",
            (actor.get("id"), now, text, rule_id),
        )
        fresh = db.execute("SELECT * FROM opd_charge_rules WHERE id = ?", (rule_id,)).fetchone()
        rule = _decorate(db, [fresh])[0]
        _audit_rule(
            db,
            action="opd_charge_rule_retired",
            actor=actor,
            rule_id=rule_id,
            summary=f"Outpatient charge rule {rule_id} retired: {text}",
            metadata={"before": before, "reason": text},
            request_meta=request_meta,
        )
    return rule


# ═════════════════════════════════════════════════════════════════════════════
#  Which code
# ═════════════════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class CodeChoice:
    """The code a visit's consultation is charged against, and why."""

    service_code: str
    #: 'requested' (the caller named it), 'follow_up', 'doctor', 'department',
    #: 'hospital' (a hospital-wide rule) or 'default' (OPD-CONSULT, no rule).
    basis: str
    rule_id: str | None = None
    follow_up_of_visit_id: str | None = None
    window_days: int | None = None

    def as_facts(self) -> dict:
        facts = {"charge_code": self.service_code, "charge_code_basis": self.basis}
        if self.rule_id:
            facts["charge_rule_id"] = self.rule_id
        if self.follow_up_of_visit_id:
            facts["follow_up_of_visit_id"] = self.follow_up_of_visit_id
        return facts


def _level(rule, *, doctor_id: str | None, department: str | None) -> int | None:
    """0 for the doctor's own rule, 1 for the clinic's, 2 for a hospital-wide
    rule, None when the rule does not apply to this visit."""
    if rule["doctor_id"]:
        return 0 if doctor_id and rule["doctor_id"] == doctor_id else None
    if rule["department"]:
        wanted = (department or "").strip().casefold()
        return 1 if wanted and rule["department"].strip().casefold() == wanted else None
    return 2


def _best_rule(rules, *, doctor_id, department, visit_type=None):
    """The most specific live rule for this visit, or (None, None)."""
    best = None
    best_key = None
    for rule in rules:
        level = _level(rule, doctor_id=doctor_id, department=department)
        if level is None:
            continue
        if rule["visit_type"] and rule["visit_type"] != visit_type:
            continue
        key = (level, 0 if rule["visit_type"] else 1)
        if best_key is None or key < best_key:
            best, best_key = rule, key
    return best, (best_key[0] if best_key else None)


def _live_rules(db, kind: str) -> list:
    return db.execute(
        "SELECT * FROM opd_charge_rules WHERE is_active = 1 AND rule_kind = ? "
        "ORDER BY created_at, id",
        (kind,),
    ).fetchall()


def resolve_consultation_code_on(
    db, visit: Mapping, *, requested_code: str | None = None
) -> CodeChoice:
    """The code this visit's consultation is charged against.

    `visit` carries id, patient_id, department, doctor_id, visit_type and
    visit_date (hospital-local YYYY-MM-DD). A follow-up rule is looked at
    first: a COMPLETED visit of the same patient in the same clinic (and, for
    a doctor's rule, with the same doctor) dated within the window before
    this one makes it a follow-up. Otherwise the most specific consultation
    rule, otherwise OPD-CONSULT."""
    if requested_code:
        return CodeChoice(service_code=requested_code, basis="requested")
    doctor_id = visit.get("doctor_id") or None
    department = visit.get("department") or None
    visit_type = visit.get("visit_type") or None

    follow, follow_level = _best_rule(
        _live_rules(db, "follow_up"), doctor_id=doctor_id, department=department
    )
    if follow is not None and visit.get("visit_date"):
        try:
            day = date.fromisoformat(str(visit["visit_date"])[:10])
        except ValueError:
            day = None
        if day is not None:
            window = int(follow["follow_up_window_days"])
            first = (day - timedelta(days=window)).isoformat()
            rows = db.execute(
                "SELECT id, department, doctor_id FROM opd_visits "
                " WHERE patient_id = ? AND id != ? AND status = 'completed' "
                "   AND visit_date >= ? AND visit_date <= ? "
                " ORDER BY visit_date DESC, completed_at DESC, id DESC",
                (visit.get("patient_id"), visit.get("id") or "", first, day.isoformat()),
            ).fetchall()
            clinic = (department or "").strip().casefold()
            for prior in rows:
                if (prior["department"] or "").strip().casefold() != clinic:
                    continue
                if follow_level == 0 and prior["doctor_id"] != doctor_id:
                    continue
                return CodeChoice(
                    service_code=follow["service_code"],
                    basis="follow_up",
                    rule_id=follow["id"],
                    follow_up_of_visit_id=str(prior["id"]),
                    window_days=window,
                )

    rule, level = _best_rule(
        _live_rules(db, "consultation"),
        doctor_id=doctor_id,
        department=department,
        visit_type=visit_type,
    )
    if rule is not None:
        return CodeChoice(
            service_code=rule["service_code"],
            basis=("doctor", "department", "hospital")[level],
            rule_id=rule["id"],
        )
    return CodeChoice(service_code=CONSULTATION_CODE, basis="default")


def registration_fee_rule_on(db) -> dict | None:
    """The live registration-fee rule, or None (no fee is charged)."""
    row = db.execute(
        "SELECT * FROM opd_charge_rules WHERE is_active = 1 AND rule_kind = 'registration' "
        "ORDER BY created_at, id LIMIT 1"
    ).fetchone()
    return dict(row) if row is not None else None


# ═════════════════════════════════════════════════════════════════════════════
#  Raising and voiding lines
# ═════════════════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class ChargeLine:
    """One encounter_charges line this module raised (or found raised)."""

    charge_id: int
    service_code: str
    description: str
    rate_paise: int | None
    configured: bool
    total_paise: int
    gst_treatment: str
    rate_source: str
    status: str
    tariff_plan_id: int | None
    newly_raised: bool

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
            "rate_source": self.rate_source,
            "status": self.status,
        }


def payer_snapshot_on(db, patient_id: str) -> billing_masters.PayerResolution:
    """Who pays for this patient's outpatient care right now (no admission)."""
    return billing_masters.resolve_patient_payer_on(db, patient_id)


def raise_op_line_on(
    db,
    *,
    patient_id: str,
    service_code: str,
    tariff_plan_id: int | None,
    service_day: str,
    charged_at: str,
    charged_by: str | None,
    idempotency_key: str,
    source_ref: str | None,
    description_fallback: str,
    unpriced_note: str,
    interstate: bool = False,
) -> ChargeLine:
    """Write ONE outpatient line (encounter_type 'op', no admission) into the
    billing spine's table, or return the line the idempotency key already
    names. Unpriced is not free: with no tariff row and no configured default
    the line is still written, rate_is_configured 0, zero totals, pending.

    `service_day` is the hospital-local calendar day the bill prints and the
    tariff is looked up against; `charged_at` is the UTC instant it was
    posted. `source` is 'manual' (migration 20's CHECK has no OPD member) and
    `source_ref` carries what raised it. On the caller's connection."""
    resolution = billing_masters.resolve_rate_on(
        db, service_code, tariff_plan_id=tariff_plan_id, ward_class=None, on_date=service_day
    )
    existing = db.execute(
        "SELECT * FROM encounter_charges WHERE idempotency_key = ?", (idempotency_key,)
    ).fetchone()
    if existing is not None:
        stored_configured = bool(int(existing["rate_is_configured"] or 0))
        return ChargeLine(
            charge_id=int(existing["id"]),
            service_code=existing["service_code"],
            description=existing["description"],
            rate_paise=int(existing["unit_rate_paise"]) if stored_configured else None,
            configured=stored_configured,
            total_paise=int(existing["total_paise"] or 0),
            gst_treatment=existing["gst_treatment"],
            rate_source=resolution.source,
            status=existing["status"],
            tariff_plan_id=existing["tariff_plan_id"],
            newly_raised=False,
        )

    description = resolution.description or description_fallback
    configured = bool(resolution.configured and resolution.rate_paise is not None)
    unit_rate = (
        int(resolution.rate_paise) if configured and resolution.rate_paise is not None else 0
    )
    line = compute_line(
        unit_rate_paise=unit_rate,
        gst_rate_bp=resolution.gst_rate_bp if configured else 0,
        gst_treatment=resolution.gst_treatment,
        interstate=interstate,
    )
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
            source_ref,
            service_day,
            charged_by,
            charged_at,
            idempotency_key,
            None if configured else unpriced_note,
        ),
    )
    return ChargeLine(
        charge_id=int(cursor.lastrowid),
        service_code=service_code,
        description=description,
        rate_paise=unit_rate if configured else None,
        configured=configured,
        total_paise=line.total_paise,
        gst_treatment=line.gst_treatment,
        rate_source=resolution.source,
        status="pending",
        tariff_plan_id=resolution.tariff_plan_id,
        newly_raised=True,
    )


def _audit_line(
    db,
    *,
    line: ChargeLine,
    patient_id: str,
    actor: str | None,
    raised_for: str,
    facts: dict | None = None,
) -> None:
    """The billing row for a line this module raised, in its transaction."""
    log_audit_event(
        correlation_id=str(uuid.uuid4()),
        event_category="billing",
        action_type="billing_charge_posted",
        outcome="success",
        db=db,
        summary_message=(
            f"Charge {line.charge_id} raised: {line.description} ({raised_for}), "
            + ("priced" if line.configured else "rate not configured")
        ),
        actor_user_id=actor,
        patient_id=patient_id,
        target_type="encounter_charge",
        target_id=str(line.charge_id),
        metadata={
            "service_code": line.service_code,
            "source": "manual",
            "raised_for": raised_for,
            "rate_is_configured": line.configured,
            "total_paise": line.total_paise,
            "rate_source": line.rate_source,
            "tariff_plan_id": line.tariff_plan_id,
            **(facts or {}),
        },
    )


def raise_registration_fee_on(
    db, *, patient_id: str, charged_by: str | None, service_day: str
) -> ChargeLine | None:
    """The registration fee, once per patient, when a live registration rule
    exists (else None and nothing is written). Priced under the payer the
    desk recorded in the same transaction, if any."""
    rule = registration_fee_rule_on(db)
    if rule is None:
        return None
    code = rule["service_code"]
    if code in DEFAULT_ITEMS:
        name, category, note = DEFAULT_ITEMS[code]
        billing_masters.register_unpriced_item_on(
            db, code=code, name=name, category=category, notes=note
        )
    payer = payer_snapshot_on(db, patient_id)
    line = raise_op_line_on(
        db,
        patient_id=patient_id,
        service_code=code,
        tariff_plan_id=payer.tariff_plan_id,
        service_day=service_day,
        charged_at=utc_iso_now(),
        charged_by=charged_by,
        idempotency_key=f"opd_registration_fee:{patient_id}",
        source_ref=patient_id,
        description_fallback="Registration fee",
        unpriced_note="Rate not configured at registration",
    )
    if line.newly_raised:
        _audit_line(
            db,
            line=line,
            patient_id=patient_id,
            actor=charged_by,
            raised_for="registration_fee",
            facts={"charge_rule_id": rule["id"], "payer_code": payer.payer_code},
        )
    return line


def raise_consultation_at_registration_on(db, *, visit: Mapping, charged_by: str | None) -> tuple:
    """OPD_CHARGE_AT 'registration': the visit's consultation line, raised
    now, once per visit (the same idempotency key completion uses, so
    completing the visit later finds it and raises nothing more). The visit
    records it as both its consultation charge and its registration-time
    charge. Returns (ChargeLine, CodeChoice)."""
    choice = resolve_consultation_code_on(db, visit)
    if choice.service_code in DEFAULT_ITEMS:
        name, category, note = DEFAULT_ITEMS[choice.service_code]
        billing_masters.register_unpriced_item_on(
            db, code=choice.service_code, name=name, category=category, notes=note
        )
    visit_id = str(visit["id"])
    line = raise_op_line_on(
        db,
        patient_id=str(visit["patient_id"]),
        service_code=choice.service_code,
        tariff_plan_id=visit.get("tariff_plan_id"),
        service_day=str(visit["visit_date"]),
        charged_at=utc_iso_now(),
        charged_by=charged_by,
        idempotency_key=f"opd_visit:{visit_id}:consultation",
        source_ref=visit_id,
        description_fallback=f"{visit.get('department') or 'Outpatient'} consultation",
        unpriced_note="Rate not configured at registration",
    )
    db.execute(
        "UPDATE opd_visits SET consultation_charge_id = ?, registration_charge_id = ? WHERE id = ?",
        (line.charge_id, line.charge_id, visit_id),
    )
    if line.newly_raised:
        _audit_line(
            db,
            line=line,
            patient_id=str(visit["patient_id"]),
            actor=charged_by,
            raised_for="opd_consultation_prepaid",
            facts={"opd_visit_id": visit_id, **choice.as_facts()},
        )
    return line, choice


def void_prepaid_consultation_on(db, *, visit_row: Mapping, actor: str | None, reason: str) -> dict:
    """A cancelled visit's registration-time line: voided while still pending
    (billing_service.cancel_charge, which audits it), left alone once it is on
    an invoice (the patient is refunded through the payments desk). Returns
    the facts for the cancel's audit row; {} when the visit raised no line at
    registration."""
    charge_id = (
        visit_row["registration_charge_id"]
        if "registration_charge_id" in visit_row.keys()
        else None
    )
    if not charge_id:
        return {}
    row = db.execute(
        "SELECT id, status, invoice_id FROM encounter_charges WHERE id = ?", (int(charge_id),)
    ).fetchone()
    if row is None:
        return {"prepaid_charge_id": int(charge_id), "prepaid_charge": "missing"}
    if row["status"] == "cancelled":
        return {"prepaid_charge_id": int(charge_id), "prepaid_charge": "already_cancelled"}
    if row["status"] == "pending":
        from services import billing_service

        billing_service.cancel_charge(
            int(charge_id),
            reason=f"Outpatient visit cancelled before the consultation: {reason}"[:500],
            actor=actor,
        )
        return {"prepaid_charge_id": int(charge_id), "prepaid_charge": "voided"}
    return {
        "prepaid_charge_id": int(charge_id),
        "prepaid_charge": "invoiced",
        "prepaid_invoice_id": row["invoice_id"],
    }
