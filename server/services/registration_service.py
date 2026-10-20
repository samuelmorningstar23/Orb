"""Orb: the patient registration desk (opd-4 and opd-5, W4-A, 4 October 2026).

Behind the 'registration' feature (ORB_REGISTRATION, default off): nothing here
is reachable while it is off, because every route sits behind
routes/opt_in_gates.require_registration_feature.

WHAT REGISTERING A PATIENT WRITES, in ONE immediate transaction
  * patients: status 'registered', admission_date NULL, NO portal login and NO
    allergy field (ruling OPD-S5: a clerk-typed "no known allergies" would
    silence the allergy interlock; a clinician records allergies later);
  * patient_demographics (one row, row_version 1);
  * patient_match_keys for the new row;
  * the ABHA the patient presented, through abdm_service.record_abha_on, which
    is ALWAYS unverified;
  * the worklist rows a reviewed duplicate warning opens (ruling OPD-S2);
  * the payer the patient names, when the desk records one (opd-13): one
    unverified patient_payer_policies row with no admission, checked against
    the payer master (billing_masters.record_patient_payer_on);
  * the registration fee, once, only while the site has a registration-fee
    charge rule (opd_charges_service.raise_registration_fee_on), priced on
    that payer's tariff and unpriced rather than free when nobody set a rate;
  * the patient's consent to treatment, with the privacy notice they were
    given (version and language), only when the desk sends the consent step
    and the account holds consent.record (opd-19; ruling OPD-N3 keeps it from
    the receptionist until counsel agrees): one consent_records row through
    consent_service.record_consent_on;
  * the patient_identity audit rows (fail-closed): if one cannot be written the
    whole registration rolls back and the desk gets 503.
Either all of it exists or none of it does.

A REPLAY (the same Idempotency-Key from the same registrar, the same name, sex,
date of birth or stated age, phone and UHID) within 24 hours lands on the same
row and answers ONLY its id and hospital number. It never returns the record
as it stands now: by then the patient may be another department's inpatient
with corrected details, and a replay is not a scoped read. Later than 24 hours
the key is refused (409 registration_key_expired).

A CORRECTION keeps the registration rule on the record after the change: a
date of birth or a stated age, never both, never neither. Removing a date of
birth needs the stated age in the same request, recorded with today's date.

THE UHID (ruling OPD-N1)
  * hospital mode (the default): the desk types the hospital's existing number.
    It must match ORB_UHID_PATTERN when one is set (an invalid pattern refuses
    every registration rather than accepting anything), and it must be unique,
    exactly and once case, spaces, hyphens, slashes and dots are ignored.
  * orb mode: the number comes from the UHID series (billing_masters.
    allocate_series_number on this transaction's connection), never resets
    yearly and carries a Verhoeff check digit. Refused with 409
    uhid_series_not_configured until a site configures the series.

DUPLICATES (ruling OPD-S2, both modes built)
  * An exact UHID or an active ABHA already on another record is a HARD match:
    refused in both modes. The desk opens the existing record instead.
  * config.DUPLICATE_MODE 'block' (the shipped default: the strictest option
    named, until Samuel confirms OPD-S2) refuses any probable duplicate (score
    70 or more). A possible duplicate (50 to 69) needs a reviewed override.
  * 'warn' lets the desk register past a probable or possible duplicate with a
    duplicate_review: decision 'register_new', the ids of EVERY candidate the
    server found (a stale list is refused with the fresh one) and a reason. The
    override is audited and opens a worklist row per candidate; the person who
    overrode may not resolve those rows.
  * A CUT SCAN FAILS CLOSED. The matcher scores at most MAX_CANDIDATE_ROWS
    records sharing the phone, name or date of birth (a helpline number, a
    1 January date of birth), strongest first, and says how high a record it
    left out could score (unseen_band). When that is 'possible' or 'probable'
    the check is incomplete and the desk is never told it found nothing:
    'warn' needs a duplicate_review (with a reason, even when no record is
    listed); 'block' refuses outright when a record left out could be a
    probable duplicate (duplicate_check_incomplete) and otherwise needs the
    review it needs for a possible one. Every such refusal and override is
    audited with check_incomplete. When unseen_band is None the records left
    out could not have scored in any band, so the result is exactly a full
    scan's; the registration audit row still records the cut.
  * A caller with a department never has a typed date of birth compared with
    the record of a patient outside it (patient_in_scope, admission arm): the
    matcher scores such a record on its age alone, the full +30 as
    'dob_hidden' when the record holds a date of birth and the ages are equal
    (the strict side). POST /match and a refused registration therefore answer
    every guess of the same age alike, so neither recovers the date of birth
    the identity check rate-limits (ruling OPD-S1). A desk with no department
    compares every date of birth, as it reads every record.

SEARCH AND IDENTITY (rulings OPD-N4 and OPD-S1)
  * The index search is hospital-wide and minimal: UHID, name, sex, age, the
    last 4 digits of the phone, the last visit date and whether the patient is
    an inpatient now. At least 3 characters, at most 20 results, every search
    audited with the matched ids. Never a date of birth, address or full phone.
  * check_identity compares what the patient STATES (name, a date of birth or
    stated age, and the phone's last 4 digits or the UHID) with the record and
    answers only 'match' or 'no match'. It never returns a stored value. Every
    answer is audited; after 5 failures by one user for one patient within an
    hour, further checks are refused with 429 whatever they would have said.

The full record, its change history and the UHID card are read through
require_patient_access(..., opd_arm=True) in the routes: an account with a
department sees only patients in its scope; only an account with no
department (a central desk) reads hospital-wide.

No AI model anywhere in this lane. Every function is plain sync and opens its
own session unless it is named *_on, which takes the caller's connection.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any, Never

import config
from database import db_session
from db_backend import DBIntegrityError

from services import patient_matching as pm
from services.audit_service import AuditWriteError, log_audit_event
from services.clinical_time import parse_to_utc, utc_iso_now
from services.patient_name_rule import PatientNameError, validate_patient_name

logger = logging.getLogger("orb.registration")

__all__ = [
    "RegistrationError",
    "RegistrationConflict",
    "RegistrationForbidden",
    "RegistrationNotFound",
    "RegistrationRateLimited",
    "AuditWriteError",
    "UHID_SERIES",
    "SEARCH_MIN_CHARS",
    "SEARCH_MAX_RESULTS",
    "REASON_MIN_CHARS",
    "IDENTITY_FAILURE_LIMIT",
    "ID_DOCUMENT_TYPES",
    "PATIENT_FIELDS",
    "DEMOGRAPHIC_FIELDS",
    "registration_config",
    "uhid_check",
    "index_cards_on",
    "search_index",
    "find_matches",
    "register_patient",
    "get_registration",
    "get_registration_on",
    "change_history",
    "uhid_card",
    "correct_demographics",
    "evaluate_identity_on",
    "check_identity",
    "list_duplicates",
    "resolve_duplicate",
    "sweep_duplicates",
    "NOTICE_LANGUAGES",
    "REGISTRATION_CONSENT_PURPOSE",
    "REGISTRATION_RETENTION_TEXT",
    "registration_consent_options",
]

UHID_SERIES = "UHID"
SEARCH_MIN_CHARS = 3
SEARCH_MAX_RESULTS = 20
REASON_MIN_CHARS = 8
IDENTITY_FAILURE_LIMIT = 5
IDENTITY_FAILURE_WINDOW = timedelta(hours=1)
#: How long after a registration its idempotency key may be replayed (a lost
#: response retried, or a queued request sent on reconnect). Later, the same
#: key is refused with registration_key_expired.
REPLAY_WINDOW = timedelta(hours=24)
#: A stated age is accepted within this many years of the record's age.
IDENTITY_AGE_TOLERANCE = 1
DUPLICATE_LIST_MAX = 100
SWEEP_MAX_PAIRS = 200
SWEEP_PAIRS_CEILING = 500
#: A group of records sharing one key larger than this is not paired up by the
#: sweep (a hospital helpline typed for every walk-in would otherwise open
#: thousands of pairs); it is counted and reported instead.
SWEEP_MAX_GROUP = 25

MAX_TEXT = 200
MAX_UHID = 40

#: Identity document kinds the desk may record, as the type and the last four
#: characters only, never a full number (ruling OPD-N10).
ID_DOCUMENT_TYPES = (
    "aadhaar",
    "pan",
    "voter_id",
    "passport",
    "driving_licence",
    "ration_card",
    "other",
)

#: patients columns the desk writes and may correct.
PATIENT_FIELDS = (
    "name",
    "sex",
    "date_of_birth",
    "age_years",
    "phone",
    "emergency_contact_name",
    "emergency_contact_phone",
)
#: patient_demographics columns the desk writes and may correct.
DEMOGRAPHIC_FIELDS = (
    "guardian_relation",
    "guardian_name",
    "marital_status",
    "preferred_language",
    "address_line",
    "city",
    "district",
    "state_code",
    "pin_code",
    "alt_phone",
    "email",
    "id_document_type",
    "id_document_last4",
    "occupation",
)
_ADDRESS_PARTS = ("address_line", "city", "district", "state_code", "pin_code")

DUPLICATE_RESOLUTIONS = ("not_duplicate", "confirmed_duplicate")

#: What a reader who reached the record ONLY through an outpatient visit (the
#: OPD scope arm) does not see and may not correct: the date of birth (the
#: discharge-link second factor), every contact and address detail, next of
#: kin, the identity document and the ABHA. The same line the OPD record view
#: draws (services/opd_service.py), because until the stated-identifier check
#: gates visit registration (opd-6a), a visit, even a self-registered one, is
#: all it takes to open the arm. The admitting ward and a desk with no
#: department see and correct everything.
OPD_ARM_HIDDEN_FIELDS = (
    "date_of_birth",
    "phone",
    "emergency_contact_name",
    "emergency_contact_phone",
    "address",
)
OPD_ARM_HIDDEN_DEMOGRAPHICS = (
    "guardian_relation",
    "guardian_name",
    "address_line",
    "city",
    "district",
    "state_code",
    "pin_code",
    "alt_phone",
    "email",
    "id_document_type",
    "id_document_last4",
)
OPD_ARM_HIDDEN = frozenset(OPD_ARM_HIDDEN_FIELDS + OPD_ARM_HIDDEN_DEMOGRAPHICS)

NO_DOB_HINT = (
    "No date of birth on file, so a secure discharge link cannot be issued for this patient."
)

# ── Notice and consent at the desk (opd-19, W9-G, 10 October 2026) ───────────
# An optional step of POST /patients. When the desk sends it, ONE consent row
# (purpose 'treatment') is written through consent_service.record_consent_on in
# the registration's own transaction, so the patient and the consent exist
# together or not at all. The row carries the notice the patient was given
# (version and language): DPDP s.5 makes the notice part of the consent, which
# is why consent_records has those two columns (migration 14). It is withdrawn
# like any other consent, as a new row (consent_service.withdraw_consent, POST
# /api/consent/{patient_id}/{consent_id}/withdraw).
#
# WHO MAY RECORD IT: a holder of consent.record only. Ruling OPD-N3 grants it
# to the receptionist only once counsel agrees, so until then a receptionist
# who sends the step is refused with 403 before anything is written, and the
# desk screen does not offer it. The doctor's walk-in (the Outpatients tab)
# records it today.

#: DPDP Act 2023 s.5(3): the notice is offered in English or a language of the
#: Eighth Schedule to the Constitution. The consent row names which one.
NOTICE_LANGUAGES = (
    "English",
    "Assamese",
    "Bengali",
    "Bodo",
    "Dogri",
    "Gujarati",
    "Hindi",
    "Kannada",
    "Kashmiri",
    "Konkani",
    "Maithili",
    "Malayalam",
    "Manipuri",
    "Marathi",
    "Nepali",
    "Odia",
    "Punjabi",
    "Sanskrit",
    "Santali",
    "Sindhi",
    "Tamil",
    "Telugu",
    "Urdu",
)
NOTICE_VERSION_MAX = 40
CONSENT_EVIDENCE_MAX = 120
#: The purpose a registration consent records (consent_service.CONSENT_PURPOSES).
REGISTRATION_CONSENT_PURPOSE = "treatment"
#: How a desk may take it: every consent method except the patient portal,
#: which is a tap in the Bridge app, never something the desk records.
REGISTRATION_CONSENT_EXCLUDED_METHODS = frozenset({"digital_portal"})
#: Methods that say a guardian or someone acting for the patient gave the
#: consent. A row with one of these must name that person (given_by guardian
#: or proxy, with their name and relationship), never given_by patient.
REGISTRATION_CONSENT_GUARDIAN_METHODS = frozenset({"proxy_guardian"})
#: DPDP Act 2023 s.2(f) and s.9: a person under 18 is a child, and a child's
#: consent is given by a parent or lawful guardian. A patient registered as
#: younger than this (date of birth or stated age) cannot consent for
#: themselves at the desk.
CONSENT_ADULT_AGE = 18
#: What the desk tells the patient about their registration details, shown on
#: the screen and stored with the consent as its scope. Registration details
#: are retained as part of the medical record (consent_service._RETENTION_SOURCES
#: files patient_demographics under clinical_record).
REGISTRATION_RETENTION_TEXT = (
    "Registration details (name, age, contact details and address) are part of the "
    "medical record. The hospital keeps them with the record for as long as the law "
    "requires the record to be kept, so they are not erased on request during that time."
)
REGISTRATION_CONSENT_SCOPE = (
    "Care and treatment at this hospital, given at registration. " + REGISTRATION_RETENTION_TEXT
)

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_PIN = re.compile(r"^\d{6}$")
_LAST4 = re.compile(r"^[A-Za-z0-9]{1,4}$")


# ── Errors ───────────────────────────────────────────────────────────────────


class RegistrationError(ValueError):
    """A refusal the desk can act on. `code` is the machine word, `message` the
    plain sentence for the screen; extra keyword fields ride in the detail."""

    status = 422

    def __init__(self, code: str, message: str, *, status: int | None = None, **extra):
        super().__init__(message)
        self.code = code
        self.message = message
        self.extra = extra
        if status is not None:
            self.status = status

    def detail(self) -> dict:
        return {"error": self.code, "message": self.message, **self.extra}


class RegistrationConflict(RegistrationError):
    status = 409


class RegistrationForbidden(RegistrationError):
    status = 403


class RegistrationNotFound(RegistrationError):
    status = 404


class RegistrationRateLimited(RegistrationError):
    status = 429


# ── Small helpers ────────────────────────────────────────────────────────────


def _clean(value: Any, *, limit: int = MAX_TEXT) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if len(text) > limit:
        raise RegistrationError("value_too_long", f"A value is longer than {limit} characters.")
    return text


def _reason(value: Any, *, code: str = "reason_required") -> str:
    text = _clean(value, limit=1000)
    if not text or len(text) < REASON_MIN_CHARS:
        raise RegistrationError(
            code,
            f"Give a reason of at least {REASON_MIN_CHARS} characters. It is "
            "the only thing that explains this to whoever reads it next.",
        )
    return text


def _today(today: date | None = None) -> date:
    from services.hospital_time import hospital_today

    return today or hospital_today()


def _local_date(stamp: Any) -> str | None:
    from services.hospital_time import to_hospital

    local = to_hospital(stamp)
    return local.date().isoformat() if local else None


def _user_names(db, ids) -> dict:
    ids = sorted({i for i in ids if i})
    if not ids:
        return {}
    marks = ",".join("?" for _ in ids)
    return {
        r["id"]: r["name"]
        for r in db.execute(f"SELECT id, name FROM users WHERE id IN ({marks})", ids).fetchall()
    }


def _audit(
    db,
    *,
    action: str,
    user: dict | None,
    outcome: str = "success",
    patient_id: str | None = None,
    target_type: str | None = "patient",
    target_id: str | int | None = None,
    summary: str = "",
    metadata: dict | None = None,
    request_meta: dict | None = None,
    correlation_id: str | None = None,
) -> None:
    """One patient_identity row (fail-closed). With `db` it is written inside
    the caller's transaction, so a failure rolls the caller's work back."""
    meta = dict(request_meta or {})
    log_audit_event(
        correlation_id=correlation_id or str(uuid.uuid4()),
        event_category="patient_identity",
        action_type=action,
        outcome=outcome,
        db=db,
        summary_message=summary or action,
        actor_user_id=(user or {}).get("id"),
        actor_role=(user or {}).get("role"),
        target_type=target_type,
        target_id=str(target_id)
        if target_id is not None
        else (str(patient_id) if patient_id else None),
        patient_id=patient_id,
        request_path=meta.get("request_path"),
        http_method=meta.get("http_method"),
        ip_address=meta.get("ip_address"),
        user_agent=meta.get("user_agent"),
        metadata={"department": _department(user), **(metadata or {})},
    )


def _department(user: dict | None) -> str | None:
    from permissions import department_scope

    return department_scope(user or {})


# ── Configuration and the UHID ───────────────────────────────────────────────


def _uhid_series_on(db) -> dict | None:
    row = db.execute(
        "SELECT * FROM document_number_series WHERE series_code = ?", (UHID_SERIES,)
    ).fetchone()
    return dict(row) if row is not None else None


def _uhid_pattern():
    """(compiled pattern or None, problem or None)."""
    raw = getattr(config, "UHID_PATTERN", None)
    if not raw:
        return None, None
    try:
        return re.compile(raw), None
    except re.error as exc:
        return None, f"ORB_UHID_PATTERN is not a valid pattern ({exc})"


def registration_config() -> dict:
    """What the desk needs to know about this site. No patient data."""
    from services.abdm_service import NO_GATEWAY_SHORT

    with db_session() as db:
        series = _uhid_series_on(db)
    pattern, problem = _uhid_pattern()
    source = config.UHID_SOURCE
    return {
        "uhid_source": source,
        "uhid_pattern_set": bool(getattr(config, "UHID_PATTERN", None)),
        "uhid_pattern_valid": problem is None,
        "uhid_series_configured": bool(series and int(series.get("is_configured") or 0)),
        "uhid_ready": (problem is None)
        if source == "hospital"
        else bool(series and int(series.get("is_configured") or 0)),
        "duplicate_mode": config.DUPLICATE_MODE,
        "abdm_enabled": _abdm_on(),
        "abha_notice": NO_GATEWAY_SHORT,
        "search_min_chars": SEARCH_MIN_CHARS,
        "search_max_results": SEARCH_MAX_RESULTS,
        "reason_min_chars": REASON_MIN_CHARS,
        "thresholds": {"probable": pm.PROBABLE_THRESHOLD, "possible": pm.POSSIBLE_THRESHOLD},
        "id_document_types": list(ID_DOCUMENT_TYPES),
        "allergies_at_desk": False,
        # opd-19: the notice and consent step. Who may send it is the caller's
        # consent.record grant, which this site-wide answer does not decide.
        "consent": registration_consent_options(),
    }


def registration_consent_options() -> dict:
    """The closed sets the desk's consent step offers, in the server's words
    (consent_service vocabularies). No patient data."""
    from services.consent_service import CONSENT_METHODS, CONSENT_PURPOSES, GIVEN_BY

    return {
        "purpose": REGISTRATION_CONSENT_PURPOSE,
        "purpose_label": CONSENT_PURPOSES[REGISTRATION_CONSENT_PURPOSE],
        "methods": [
            {"value": k, "label": v}
            for k, v in CONSENT_METHODS.items()
            if k not in REGISTRATION_CONSENT_EXCLUDED_METHODS
        ],
        "given_by": list(GIVEN_BY),
        # The two rules the desk mirrors before sending (_consent_values
        # refuses both): these methods need a named guardian or proxy, and a
        # patient younger than adult_age cannot consent for themselves.
        "guardian_methods": sorted(REGISTRATION_CONSENT_GUARDIAN_METHODS),
        "adult_age": CONSENT_ADULT_AGE,
        "notice_languages": list(NOTICE_LANGUAGES),
        "notice_version_max": NOTICE_VERSION_MAX,
        "retention_text": REGISTRATION_RETENTION_TEXT,
    }


def _abdm_on() -> bool:
    return bool(config.enabled_modules().get("abdm", False))


def uhid_check(text: str | None, series: dict | None = None) -> bool:
    """Does a typed Orb-mode UHID carry a valid Verhoeff check digit?

    The configured prefix and suffix are removed (any case) and what is left
    must be digits whose last digit checks. Always True in hospital mode,
    where the number is the hospital's own and its format is the site
    pattern's business.
    """
    if config.UHID_SOURCE != "orb":
        return True
    from services.billing_masters import verhoeff_valid

    value = (text or "").strip().upper()
    if series is None:
        with db_session() as db:
            series = _uhid_series_on(db)
    prefix = str((series or {}).get("prefix") or "").upper()
    suffix = str((series or {}).get("suffix") or "").upper()
    if prefix and value.startswith(prefix):
        value = value[len(prefix) :]
    if suffix and value.endswith(suffix):
        value = value[: -len(suffix)]
    return verhoeff_valid(value)


def _hospital_uhid(raw: str | None) -> str:
    """Validate a typed hospital-mode UHID against the site pattern."""
    uhid = _clean(raw, limit=MAX_UHID)
    if not uhid:
        raise RegistrationError("uhid_required", "Type the patient's hospital number (UHID).")
    pattern, problem = _uhid_pattern()
    if problem:
        logger.error("Registration refused: %s", problem)
        raise RegistrationConflict(
            "uhid_pattern_invalid",
            "The hospital number format set for this site is not valid, so no "
            "patient can be registered until an administrator corrects it.",
        )
    if pattern is not None and not pattern.fullmatch(uhid):
        raise RegistrationError(
            "uhid_format", "That hospital number is not in this hospital's format."
        )
    return uhid


def _uhid_in_use_on(db, uhid: str, *, exclude_patient_id: str | None = None) -> list[str]:
    key = pm.uhid_key(uhid)
    rows = db.execute(
        f"SELECT id FROM patients WHERE mrn = ? OR {pm.UHID_KEY_SQL.format(col='mrn')} = ?",
        (uhid, key),
    ).fetchall()
    return [r[0] for r in rows if r[0] != exclude_patient_id]


# ── Minimal index cards (OPD-N4) ─────────────────────────────────────────────

#: The ONLY fields a hospital-wide reader sees about a patient.
INDEX_CARD_FIELDS = (
    "patient_id",
    "uhid",
    "name",
    "sex",
    "age",
    "age_stated",
    "phone_last4",
    "last_visit_date",
    "inpatient_now",
)


def index_cards_on(db, ids, *, today: date | None = None) -> dict:
    """{patient_id: card} for the minimal-field index view. Nothing else."""
    ids = [i for i in dict.fromkeys(ids) if i]
    if not ids:
        return {}
    marks = ",".join("?" for _ in ids)
    rows = db.execute(
        f"""SELECT p.id, p.mrn, p.name, p.sex, p.age, p.date_of_birth, p.phone,
                   p.created_at, d.age_recorded_on,
                   (SELECT MAX(v.visit_date) FROM opd_visits v
                     WHERE v.patient_id = p.id AND v.status != 'cancelled') AS last_opd,
                   (SELECT MAX(COALESCE(a.admitted_at, a.created_at)) FROM admissions a
                     WHERE a.patient_id = p.id) AS last_admission,
                   EXISTS (SELECT 1 FROM admissions a
                            WHERE a.patient_id = p.id AND a.status = 'active') AS inpatient_now
              FROM patients p LEFT JOIN patient_demographics d ON d.patient_id = p.id
             WHERE p.id IN ({marks})""",
        ids,
    ).fetchall()
    out = {}
    for r in rows:
        age, stated = pm.current_age(
            date_of_birth=r["date_of_birth"],
            stated_age=r["age"],
            age_recorded_on=r["age_recorded_on"],
            created_at=r["created_at"],
            today=today,
        )
        visits = [d for d in (r["last_opd"], _local_date(r["last_admission"])) if d]
        out[r["id"]] = {
            "patient_id": r["id"],
            "uhid": r["mrn"],
            "name": r["name"],
            "sex": pm.sex_key(r["sex"]) or r["sex"],
            "age": age,
            "age_stated": stated,
            "phone_last4": pm.phone_last4(r["phone"]),
            "last_visit_date": max(visits) if visits else None,
            "inpatient_now": bool(r["inpatient_now"]),
        }
    return out


def _cards_in_order(db, ids, *, today=None) -> list[dict]:
    cards = index_cards_on(db, ids, today=today)
    return [cards[i] for i in ids if i in cards]


# ── Search (hospital-wide, minimal fields) ───────────────────────────────────


def _like_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def search_index(query: str | None, *, today: date | None = None) -> dict:
    """Find patients by UHID, phone or name across the whole hospital.

    Returns {"results": [card], "truncated": bool, "kinds": [...]} where
    each card holds INDEX_CARD_FIELDS only. The route audits the matched ids
    before anything is returned.
    """
    q = (query or "").strip()
    if len(q) < SEARCH_MIN_CHARS:
        raise RegistrationError(
            "query_too_short", f"Type at least {SEARCH_MIN_CHARS} characters to search."
        )
    if len(q) > MAX_TEXT:
        raise RegistrationError("value_too_long", "That search is too long.")
    limit = SEARCH_MAX_RESULTS
    ordered: list[str] = []
    kinds: list[str] = []

    def add(ids, kind):
        new = [i for i in ids if i not in ordered]
        if new:
            if kind not in kinds:
                kinds.append(kind)
            ordered.extend(new)

    with db_session() as db:
        pm.sync_keys(db)
        # UHID: exact, then ignoring case and separators.
        add(_uhid_in_use_on(db, q), "uhid")
        # Phone: the whole number only. A partial number is not a search key.
        digits_only = re.fullmatch(r"[\d\s()+\-.]+", q) is not None
        phone = pm.phone_key(q) if digits_only else None
        if phone:
            add(
                [
                    r[0]
                    for r in db.execute(
                        "SELECT k.patient_id FROM patient_match_keys k JOIN patients p ON p.id = k.patient_id "
                        "WHERE k.phone_key = ? ORDER BY p.name LIMIT ?",
                        (phone, limit + 1),
                    ).fetchall()
                ],
                "phone",
            )
        # Name: every word typed must start a word of the normalised name, or
        # the typed text must appear in the name as written.
        words = [w for w in pm.name_tokens(q) if w != pm.BABY_MARKER and len(w) >= 2]
        if not digits_only and words:
            key = pm.name_key(q)
            clauses = " AND ".join("(' ' || k.name_key || ' ') LIKE ? ESCAPE '\\'" for _ in words)
            params = [f"% {_like_escape(w)}%" for w in words]
            rows = db.execute(
                "SELECT k.patient_id, k.name_key, p.name FROM patient_match_keys k "
                "JOIN patients p ON p.id = k.patient_id "
                f"WHERE ({clauses}) OR p.name LIKE ? ESCAPE '\\' "
                "ORDER BY (k.name_key = ?) DESC, p.name, k.patient_id LIMIT ?",
                (*params, f"%{_like_escape(q)}%", key, limit + 1),
            ).fetchall()
            add([r["patient_id"] for r in rows], "name")
        truncated = len(ordered) > limit
        results = _cards_in_order(db, ordered[:limit], today=today)
    return {"results": results, "truncated": truncated, "kinds": kinds}


# ── Input validation ─────────────────────────────────────────────────────────


def _phone(value: Any, *, field: str = "phone") -> str | None:
    text = _clean(value, limit=30)
    if text is None:
        return None
    digits = pm.phone_digits(text)
    if not (6 <= len(digits) <= 15) or re.search(r"[A-Za-z]", text):
        raise RegistrationError(
            "bad_phone", "That phone number is not a number Orb can store.", field=field
        )
    return text


def _dob(value: Any) -> str | None:
    text = _clean(value, limit=40)
    if text is None:
        return None
    dob = pm.dob_key(text)
    if dob is None:
        raise RegistrationError(
            "bad_date_of_birth",
            "That date of birth cannot be used. Give it as YYYY-MM-DD, not in the future.",
        )
    return dob


def _stated_age(value: Any) -> int | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    try:
        age = int(value)
    except (TypeError, ValueError):
        raise RegistrationError("bad_age", "A stated age is a whole number of years.") from None
    if isinstance(value, float) and value != age:
        raise RegistrationError("bad_age", "A stated age is a whole number of years.")
    if not (0 <= age <= 130):
        raise RegistrationError("bad_age", "A stated age must be between 0 and 130 years.")
    return age


def _sex(value: Any) -> str:
    sex = pm.sex_key(value)
    if sex is None:
        raise RegistrationError("sex_required", "Record the patient's sex: M, F or O.")
    return sex


def _demographic(field: str, value: Any) -> str | None:
    if field == "alt_phone":
        return _phone(value, field="alt_phone")
    text = _clean(value)
    if text is None:
        return None
    if field == "email" and not _EMAIL.match(text):
        raise RegistrationError("bad_email", "That email address is not valid.")
    if field == "pin_code" and not _PIN.match(text):
        raise RegistrationError("bad_pin_code", "A PIN code is 6 digits.")
    if field == "id_document_type":
        text = text.lower()
        if text not in ID_DOCUMENT_TYPES:
            raise RegistrationError(
                "bad_id_document_type", "Choose the identity document type from the list."
            )
    if field == "id_document_last4" and not _LAST4.match(text):
        raise RegistrationError(
            "bad_id_document_last4",
            "Record only the last 4 characters of an identity document, never the full number.",
        )
    return text


def _compose_address(parts: dict) -> str | None:
    address_parts = [parts.get(k) for k in _ADDRESS_PARTS]
    words = [w for w in address_parts if w]
    return ", ".join(words) if words else None


def _abha_values(data: dict) -> tuple[str | None, str | None, str]:
    """(normalised number, normalised address, source) or 422."""
    from services.abdm_service import (
        IDENTITY_SOURCES,
        validate_abha_address,
        validate_abha_number,
    )

    number_raw = _clean(data.get("abha_number"), limit=40)
    address_raw = _clean(data.get("abha_address"), limit=120)
    if not number_raw and not address_raw:
        return None, None, "counter_keyed"
    if not _abdm_on():
        raise RegistrationError(
            "abdm_off", "ABHA recording is switched off on this site, so leave the ABHA blank."
        )
    number = address = None
    if number_raw:
        check = validate_abha_number(number_raw)
        if not check.valid:
            raise RegistrationError("bad_abha", check.reason or "That is not an ABHA number.")
        number = check.normalised
    if address_raw:
        address_check = validate_abha_address(address_raw)
        if not address_check.valid:
            raise RegistrationError(
                "bad_abha", address_check.reason or "That is not an ABHA address."
            )
        address = address_check.normalised
    source = _clean(data.get("abha_source")) or "counter_keyed"
    if source not in {code for code, _ in IDENTITY_SOURCES}:
        raise RegistrationError("bad_abha_source", "Choose where the ABHA came from from the list.")
    return number, address, source


def _payer_values(payer: Any) -> dict | None:
    """Who the patient says pays for their outpatient care (opd-13): a payer
    code from the site's payer master, and the policy or member number if
    they have one. None when the desk recorded no payer (the patient is then
    priced as self-pay, the resolver's honest default). Checked against the
    payer master inside the registration's transaction."""
    if payer is None:
        return None
    if not isinstance(payer, dict):
        raise RegistrationError("bad_payer", "The payer is not readable.")
    from services.billing_masters import MAX_POLICY_TEXT

    code = _clean(payer.get("payer_code"), limit=MAX_POLICY_TEXT)
    if not code:
        raise RegistrationError("bad_payer", "Choose who pays from the list.")
    return {
        "payer_code": code,
        "policy_number": _clean(payer.get("policy_number"), limit=MAX_POLICY_TEXT),
        "member_id": _clean(payer.get("member_id"), limit=MAX_POLICY_TEXT),
    }


def _notice_language(value: Any) -> str | None:
    """The notice language in its listed spelling, or None when the text is
    not English or an Eighth Schedule language (case and spaces ignored)."""
    text = _clean(value)
    if text is None:
        return None
    wanted = " ".join(text.split()).casefold()
    for language in NOTICE_LANGUAGES:
        if language.casefold() == wanted:
            return language
    return None


def _younger_than(years: int, date_of_birth: str | None, age_years: int | None) -> bool:
    """Is a patient with this validated date of birth (YYYY-MM-DD) or stated
    age younger than `years` today?

    The stated age is taken as given. A date of birth is counted to the
    earlier of the appliance's local date and the UTC date, so a birthday the
    two clocks disagree about counts the patient as the younger age. A
    patient with neither (never reaches here: the registration refuses that
    first) counts as younger, the side that asks for a guardian."""
    if age_years is not None:
        return age_years < years
    if not date_of_birth:
        return True
    try:
        born = date.fromisoformat(date_of_birth)
    except ValueError:
        return True
    today = min(date.today(), datetime.now(UTC).date())
    age = today.year - born.year - ((today.month, today.day) < (born.month, born.day))
    return age < years


def _consent_values(
    raw: Any, *, preferred_language: str | None, date_of_birth: str | None, age_years: int | None
) -> dict | None:
    """The desk's notice and consent step (opd-19), checked before anything
    is written. None when the desk did not send one (the registration then
    writes no consent, exactly as before).

    Both facts must be stated as true: the patient was given the notice (in a
    named version and language) and agrees to treatment. A desk whose patient
    does not agree leaves the step out; a doctor records consent later. The
    notice language defaults to the patient's preferred language when that is
    one the notice may be given in.

    Who gave it must agree with how and with the patient's age (DPDP s.9; a
    consent row is immutable, so a contradiction is refused, never stored): a
    guardian method (REGISTRATION_CONSENT_GUARDIAN_METHODS) needs a guardian
    or proxy, and a patient under CONSENT_ADULT_AGE by the validated date of
    birth or stated age cannot give it themselves. Either way the guardian or
    proxy is named, with their relationship."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise RegistrationError("bad_consent", "The consent step is not readable.")
    if raw.get("notice_acknowledged") is not True:
        raise RegistrationError(
            "consent_notice_required",
            "Give the patient the privacy notice and record that they have it before "
            "recording their consent.",
        )
    if raw.get("treatment_consent") is not True:
        raise RegistrationError(
            "consent_treatment_required",
            "Record consent only when the patient agrees to treatment. Otherwise leave "
            "the consent step out; a doctor records it later.",
        )
    version = _clean(raw.get("notice_version"), limit=NOTICE_VERSION_MAX)
    if not version:
        raise RegistrationError(
            "notice_version_required",
            "Record the version printed on the notice the patient was given.",
        )
    language_text = _clean(raw.get("notice_language"))
    language = _notice_language(language_text if language_text else preferred_language)
    if language is None:
        raise RegistrationError(
            "bad_notice_language",
            "The notice is given in English or a language of the Eighth Schedule. Choose "
            "the language the patient was given it in.",
        )
    from services.consent_service import CONSENT_METHODS, GIVEN_BY

    method = _clean(raw.get("method"), limit=40)
    if method not in CONSENT_METHODS or method in REGISTRATION_CONSENT_EXCLUDED_METHODS:
        raise RegistrationError(
            "bad_consent_method", "Choose how the consent was given from the list."
        )
    given_by = _clean(raw.get("given_by"), limit=20) or "patient"
    if given_by not in GIVEN_BY:
        raise RegistrationError(
            "bad_consent_given_by", "Say whether the patient, a guardian or a proxy gave consent."
        )
    proxy_name = _clean(raw.get("proxy_name"))
    proxy_relationship = _clean(raw.get("proxy_relationship"), limit=80)
    if given_by == "patient" and method in REGISTRATION_CONSENT_GUARDIAN_METHODS:
        # A row saying "given by a guardian" and "given by the patient" at
        # once can never be corrected (consent rows are immutable), and it
        # would carry no guardian's name.
        raise RegistrationError(
            "consent_given_by_mismatch",
            "This consent was given by a guardian. Under who gave consent, choose a guardian "
            "or someone acting for the patient, and record their name and relationship.",
        )
    if given_by == "patient" and _younger_than(CONSENT_ADULT_AGE, date_of_birth, age_years):
        raise RegistrationError(
            "consent_guardian_required",
            f"This patient is under {CONSENT_ADULT_AGE}, so a parent or guardian gives "
            "consent for them. Record who gave it, with their name and relationship.",
        )
    if given_by != "patient" and not (proxy_name and proxy_relationship):
        raise RegistrationError(
            "consent_proxy_required",
            "A guardian's or proxy's consent needs their name and their relationship "
            "to the patient.",
        )
    if given_by == "patient":
        proxy_name = proxy_relationship = None
    return {
        "notice_version": version,
        "notice_language": language,
        "method": method,
        "given_by": given_by,
        "proxy_name": proxy_name,
        "proxy_relationship": proxy_relationship,
        "evidence_ref": _clean(raw.get("evidence_ref"), limit=CONSENT_EVIDENCE_MAX),
    }


def _may_record_consent(user: dict | None) -> bool:
    from permissions import CONSENT_RECORD, has_permission

    return has_permission(str((user or {}).get("role") or ""), CONSENT_RECORD)


def _refuse_consent_without_grant(user: dict, request_meta: dict | None) -> Never:
    """The consent step sent by an account without consent.record (ruling
    OPD-N3: the receptionist, until counsel agrees). Audited like any other
    permission refusal, then 403 before anything is written."""
    from permissions import CONSENT_RECORD

    meta = dict(request_meta or {})
    log_audit_event(
        correlation_id=str(uuid.uuid4()),
        event_category="authorization",
        action_type="permission_denied",
        outcome="denied",
        summary_message=f"Missing permissions: {CONSENT_RECORD} (consent at registration)",
        actor_user_id=user.get("id"),
        actor_role=user.get("role"),
        request_path=meta.get("request_path"),
        http_method=meta.get("http_method"),
        ip_address=meta.get("ip_address"),
        user_agent=meta.get("user_agent"),
        metadata={
            "required_permissions": [CONSENT_RECORD],
            "missing_permissions": [CONSENT_RECORD],
            "user_role": user.get("role"),
            "lane": "registration_consent",
        },
    )
    raise RegistrationForbidden(
        "consent_permission_required",
        "This account does not record consent. Register the patient without the consent "
        "step; a doctor records it.",
        missing=[CONSENT_RECORD],
    )


def _consent_view(row: dict) -> dict:
    """The consent a registration recorded, as the desk's answer shows it."""
    keys = (
        "id",
        "purpose",
        "purpose_label",
        "notice_version",
        "notice_language",
        "method",
        "method_label",
        "given_by",
        "granted_at",
        "effective_status",
    )
    return {k: row.get(k) for k in keys}


def _validate_registration(data: dict) -> dict:
    data = dict(data or {})
    try:
        name = validate_patient_name(data.get("name"))
    except PatientNameError as exc:
        raise RegistrationError("name_invalid", str(exc)) from exc
    if len(name) > MAX_TEXT:
        raise RegistrationError("value_too_long", "That name is too long.")
    dob = _dob(data.get("date_of_birth"))
    age = _stated_age(data.get("age_years"))
    if dob and age is not None:
        raise RegistrationError(
            "age_with_date_of_birth", "Give a date of birth or a stated age, not both."
        )
    if dob is None and age is None:
        raise RegistrationError(
            "age_or_date_of_birth_required",
            "Record a date of birth, or the age the patient states if they do not know it.",
        )
    clean: dict[str, Any] = {
        "name": name,
        "sex": _sex(data.get("sex")),
        "date_of_birth": dob,
        "age_years": age,
        "phone": _phone(data.get("phone")),
        "emergency_contact_name": _clean(data.get("emergency_contact_name")),
        "emergency_contact_phone": _phone(
            data.get("emergency_contact_phone"), field="emergency_contact_phone"
        ),
        "uhid": _clean(data.get("uhid"), limit=MAX_UHID),
    }
    for field in DEMOGRAPHIC_FIELDS:
        clean[field] = _demographic(field, data.get(field))
    if clean["id_document_last4"] and not clean["id_document_type"]:
        raise RegistrationError(
            "bad_id_document_type", "Say which identity document the last 4 characters belong to."
        )
    clean["abha_number"], clean["abha_address"], clean["abha_source"] = _abha_values(data)
    clean["payer"] = _payer_values(data.get("payer"))
    # opd-19: the notice and consent step, when the desk sent one.
    clean["consent"] = _consent_values(
        data.get("consent"),
        preferred_language=clean["preferred_language"],
        date_of_birth=dob,
        age_years=age,
    )
    review = data.get("duplicate_review")
    if review is not None:
        if not isinstance(review, dict):
            raise RegistrationError("bad_duplicate_review", "The duplicate review is not readable.")
        if review.get("decision") != "register_new":
            raise RegistrationError(
                "bad_duplicate_review", "A duplicate review must say 'register_new'."
            )
        ids = review.get("candidate_ids") or []
        if not isinstance(ids, list) or not all(isinstance(i, str) and i for i in ids):
            raise RegistrationError(
                "bad_duplicate_review", "List the records you checked by their ids."
            )
        clean["duplicate_review"] = {
            "decision": "register_new",
            "candidate_ids": sorted(set(ids)),
            "reason": _reason(review.get("reason"), code="override_reason_required"),
        }
    else:
        clean["duplicate_review"] = None
    return clean


# ── Matching (opd-5) ─────────────────────────────────────────────────────────


def _decorate_candidates(db, candidates: list[dict], *, today=None) -> list[dict]:
    cards = index_cards_on(db, [c["patient_id"] for c in candidates], today=today)
    out = []
    for c in candidates:
        card = cards.get(c["patient_id"])
        if card is None:
            continue
        out.append(
            {
                **card,
                "score": c["score"],
                "band": c["band"],
                "reasons": list(c["reasons"]),
                "reason_labels": pm.reason_labels(c["reasons"]),
            }
        )
    return out


def _probe(clean: dict, *, today=None) -> dict:
    return pm.probe_from(
        name=clean.get("name"),
        phone=clean.get("phone"),
        date_of_birth=clean.get("date_of_birth"),
        age_years=clean.get("age_years"),
        sex=clean.get("sex"),
        uhid=clean.get("uhid"),
        abha_number=clean.get("abha_number"),
        abha_address=clean.get("abha_address"),
        today=today,
    )


def _dob_visible_to(db, department: str | None):
    """The matcher's dob_visible predicate for a caller scoped to `department`.

    None (compare every date of birth) for an account with no department, which
    reads every record in full. Otherwise permissions.patient_in_scope, the
    admission arm only: a patient a clinic reaches only through an outpatient
    visit shows that clinic no date of birth (OPD_ARM_HIDDEN_FIELDS), so the
    visit opens nothing here either. Without this, a typed date of birth scored
    against a record the caller may not read would tell a right guess from a
    wrong one (patient_matching's module docstring)."""
    if department is None:
        return None
    from permissions import patient_in_scope

    return lambda patient_id: patient_in_scope(db, patient_id, department)


def _review_needs(candidates: list[dict], *, unseen_band: str | None = None) -> dict:
    """What the desk must do about these candidates under config.DUPLICATE_MODE.

    `unseen_band` is the matcher's band for the records a cut scan left out
    (patient_matching.find_candidates_on). A record that was not scored is
    treated as the strongest candidate it could have been, never as no
    candidate: 'probable' blocks in 'block' mode, and either band needs a
    review (the module docstring, A CUT SCAN FAILS CLOSED)."""
    hard = [c for c in candidates if c["band"] == "hard"]
    probable = [c for c in candidates if c["band"] == "probable"]
    possible = [c for c in candidates if c["band"] == "possible"]
    mode = config.DUPLICATE_MODE
    unseen = unseen_band if unseen_band in ("probable", "possible") else None
    blocked = bool(hard) or (mode == "block" and (bool(probable) or unseen == "probable"))
    return {
        "hard": hard,
        "probable": probable,
        "possible": possible,
        "mode": mode,
        "check_incomplete": unseen is not None,
        "unseen_band": unseen,
        "blocked": blocked,
        "review_required": (bool(probable or possible) or unseen is not None) and not blocked,
    }


def _scan_facts(found: dict, needs: dict) -> dict:
    """The cut-scan facts every refusal, override and registration audits."""
    return {
        "truncated": bool(found.get("truncated")),
        "check_incomplete": needs["check_incomplete"],
        "unseen_band": needs["unseen_band"],
    }


def find_matches(data: dict, *, user: dict, today: date | None = None) -> dict:
    """POST /match: what registering these details would run into.

    Takes the same fields as a registration (name, sex, date_of_birth or
    age_years, phone, uhid, abha_number, abha_address); none is required, but
    at least one identifier must be given. Returns {"candidates": [card +
    score, band, reasons, reason_labels], "truncated", "check_incomplete",
    "mode", "blocked", "review_required"}. check_incomplete is True when a cut
    scan left out records that could have scored in a band; blocked and
    review_required then say what register_patient will do about them.

    `user` is the authenticated caller: for an account with a department, a
    typed date of birth is never compared with the record of a patient outside
    that department (_dob_visible_to), exactly as register_patient scores it,
    so this preview and the registration agree.
    """
    data = dict(data or {})
    probe_fields = {
        "name": _clean(data.get("name")),
        "sex": pm.sex_key(data.get("sex")),
        "date_of_birth": _dob(data.get("date_of_birth")),
        "age_years": _stated_age(data.get("age_years")),
        "phone": _phone(data.get("phone")),
        "uhid": _clean(data.get("uhid"), limit=MAX_UHID),
    }
    abha = {k: data.get(k) for k in ("abha_number", "abha_address", "abha_source")}
    probe_fields["abha_number"], probe_fields["abha_address"], _src = (
        _abha_values(abha)
        if (abha.get("abha_number") or abha.get("abha_address"))
        else (None, None, None)
    )
    if not any(
        probe_fields.get(k)
        for k in ("name", "phone", "date_of_birth", "uhid", "abha_number", "abha_address")
    ):
        raise RegistrationError(
            "nothing_to_match", "Type a name, phone, date of birth, UHID or ABHA to check."
        )
    with db_session() as db:
        found = pm.find_candidates_on(
            db,
            _probe(probe_fields, today=today),
            today=today,
            dob_visible=_dob_visible_to(db, _department(user)),
        )
        candidates = _decorate_candidates(db, found["candidates"], today=today)
    needs = _review_needs(candidates, unseen_band=found.get("unseen_band"))
    return {
        "candidates": candidates,
        "truncated": found["truncated"],
        "check_incomplete": needs["check_incomplete"],
        "mode": needs["mode"],
        "blocked": needs["blocked"],
        "review_required": needs["review_required"],
    }


# ── Registering a patient ────────────────────────────────────────────────────


def _patient_id_for(user_id: str, idempotency_key: str | None) -> str:
    """A new patient id. With an idempotency key it is derived from the
    registrar and the key, so a replay lands on the same row; 12 hex digits, a
    different length from the 8 the admission door mints, so the two can never
    collide."""
    if idempotency_key:
        digest = hashlib.sha256(
            f"registration\x00{user_id}\x00{idempotency_key}".encode()
        ).hexdigest()
        return f"PAT-{digest[:12].upper()}"
    return f"PAT-{uuid.uuid4().hex[:12].upper()}"


def _same_registration(row, clean: dict) -> bool:
    if (row["name"] or "").strip() != clean["name"]:
        return False
    if pm.sex_key(row["sex"]) != clean["sex"]:
        return False
    if (pm.dob_key(row["date_of_birth"]) or None) != clean["date_of_birth"]:
        return False
    if clean["date_of_birth"] is None and row["age"] != clean["age_years"]:
        return False
    if pm.phone_key(row["phone"]) != pm.phone_key(clean["phone"]) or pm.phone_digits(
        row["phone"]
    ) != pm.phone_digits(clean["phone"]):
        return False
    if config.UHID_SOURCE == "hospital" and clean["uhid"] and row["mrn"] != clean["uhid"]:
        return False
    return True


class _Refusal(Exception):
    """Carries a refusal out of the transaction so its audit row can be written
    on its own (an audit row written inside a rolled-back transaction is lost)."""

    def __init__(
        self, error: RegistrationError, *, action: str, metadata: dict, patient_ids: list[str]
    ):
        super().__init__(error.message)
        self.error = error
        self.action = action
        self.metadata = metadata
        self.patient_ids = patient_ids


def _refuse_with_candidates(
    code: str,
    message: str,
    candidates: list[dict],
    mode: str,
    *,
    action: str = "registration_refused",
    scan: dict | None = None,
) -> _Refusal:
    ids = [c["patient_id"] for c in candidates]
    scan = scan or {"truncated": False, "check_incomplete": False, "unseen_band": None}
    return _Refusal(
        RegistrationConflict(
            code,
            message,
            candidates=candidates,
            mode=mode,
            check_incomplete=scan["check_incomplete"],
        ),
        action=action,
        metadata={
            "reason_code": code,
            "mode": mode,
            "matched_patient_ids": ids,
            "bands": [c["band"] for c in candidates],
            **scan,
        },
        patient_ids=ids,
    )


def _key_conflict() -> _Refusal:
    return _Refusal(
        RegistrationConflict(
            "registration_key_conflict",
            "This request was already used for a different registration. Start a new registration.",
        ),
        action="registration_replay_refused",
        metadata={"reason_code": "registration_key_conflict"},
        patient_ids=[],
    )


def _replay_window_open(registered_at: Any, *, now: datetime | None = None) -> bool:
    """Is a registration still inside REPLAY_WINDOW, so its idempotency key
    may be replayed? A missing or unreadable stamp is outside (fail closed)."""
    if not registered_at:
        return False
    try:
        at = parse_to_utc(registered_at)
    except (TypeError, ValueError):
        return False
    return (now or datetime.now(UTC)) - at <= REPLAY_WINDOW


def _replay_record(row) -> dict:
    """What a replay answers for the patient: the id and the hospital number,
    which the first response established and the hospital-wide index search
    shows any registration desk. Nothing else of the record as it stands now
    (address, contacts, next of kin, identity document, ABHA, whether the
    patient is an inpatient): a replay is not a scoped read."""
    return {"patient_id": row["id"], "uhid": row["mrn"], "record_returned": False}


def register_patient(
    data: dict,
    *,
    user: dict,
    idempotency_key: str | None = None,
    request_meta: dict | None = None,
    today: date | None = None,
) -> dict:
    """Register a new patient. See the module docstring for what one call
    writes and refuses. Returns {"patient": record, "replayed": bool,
    "duplicate_review": {...} or None}.

    A replay of the same idempotency key by the same registrar, with the same
    identifiers and within REPLAY_WINDOW, returns {"patient": {"patient_id",
    "uhid", "record_returned": False}, "replayed": True, ...} and writes
    nothing but its audit row; after the window the key is refused with 409
    registration_key_expired.

    Raises RegistrationError (422), RegistrationConflict (409, with
    'candidates' when the refusal is about other records),
    RegistrationForbidden (403 consent_permission_required: the consent step
    sent by an account without consent.record; nothing is written),
    AuditWriteError (the caller answers 503; nothing was written).

    With the notice and consent step (opd-19) the answer adds "consents": the
    one consent row written in the same transaction.
    """
    if (data or {}).get("consent") is not None and not _may_record_consent(user):
        _refuse_consent_without_grant(user, request_meta)
    clean = _validate_registration(data)
    key = _clean(idempotency_key, limit=200)
    pid = _patient_id_for(user["id"], key)
    dept = _department(user)
    now = utc_iso_now()
    try:
        with db_session(immediate=True) as db:
            # 1. A replay of a request that already registered someone. It
            # answers only what the first response established (the id and
            # the hospital number), never the record as it stands now: the
            # patient may since have been admitted to another department and
            # corrected, and a replay carries no scope check. The full record
            # is read through GET /patients/{id}, which is scoped and audited.
            if key:
                existing = db.execute(
                    "SELECT p.*, d.registered_by, d.registration_source, d.registered_at "
                    "FROM patients p "
                    "LEFT JOIN patient_demographics d ON d.patient_id = p.id WHERE p.id = ?",
                    (pid,),
                ).fetchone()
                if existing is not None:
                    if (
                        existing["registered_by"] != user["id"]
                        or existing["registration_source"] != "desk"
                    ):
                        raise _key_conflict()
                    if not _replay_window_open(existing["registered_at"]):
                        raise _Refusal(
                            RegistrationConflict(
                                "registration_key_expired",
                                "This request already registered a patient more than a day "
                                "ago. Search for the patient instead of registering them "
                                "again.",
                            ),
                            action="registration_replay_refused",
                            metadata={"reason_code": "registration_key_expired"},
                            patient_ids=[],
                        )
                    if not _same_registration(existing, clean):
                        raise _key_conflict()
                    _audit(
                        db,
                        action="patient_registered",
                        user=user,
                        patient_id=pid,
                        summary=f"Registration replayed for {pid}",
                        metadata={
                            "replay": True,
                            "registration_source": "desk",
                            "record_returned": False,
                        },
                        request_meta=request_meta,
                    )
                    return {
                        "patient": _replay_record(existing),
                        "replayed": True,
                        "duplicate_review": None,
                    }

            # 2. The UHID.
            series = None
            if config.UHID_SOURCE == "orb":
                if clean["uhid"]:
                    raise RegistrationError(
                        "uhid_assigned_by_orb",
                        "This site issues hospital numbers itself. Leave the UHID blank.",
                    )
                series = _uhid_series_on(db)
                if not series or not int(series.get("is_configured") or 0):
                    raise RegistrationConflict(
                        "uhid_series_not_configured",
                        "Hospital numbers cannot be issued yet: the UHID series is not set "
                        "up on this site. Ask an administrator to configure it.",
                    )
            else:
                clean["uhid"] = _hospital_uhid(clean["uhid"])

            # 3. Who this might already be.
            # A scoped registrar's date of birth is never compared with a
            # record outside their department (_dob_visible_to): the refusal
            # and its candidates would otherwise answer a guess. Never weaker
            # for a candidate the phone or name finds (the same age scores the
            # full +30); the administrator's sweep pairs the rest.
            found = pm.find_candidates_on(
                db, _probe(clean, today=today), today=today, dob_visible=_dob_visible_to(db, dept)
            )
            candidates = _decorate_candidates(db, found["candidates"], today=today)
            # A cut scan never reads as "no duplicate": records it left out
            # count as the strongest candidates they could have been
            # (_review_needs), so the desk is refused or asked for a review.
            needs = _review_needs(candidates, unseen_band=found.get("unseen_band"))
            scan = _scan_facts(found, needs)
            mode = needs["mode"]
            if needs["hard"]:
                reasons = {r for c in needs["hard"] for r in c["reasons"]}
                what = (
                    "UHID"
                    if "uhid" in reasons and "abha" not in reasons
                    else (
                        "ABHA" if "abha" in reasons and "uhid" not in reasons else "UHID and ABHA"
                    )
                )
                raise _refuse_with_candidates(
                    "existing_record",
                    f"A patient with this {what} is already registered. Open their record "
                    "instead of registering them again.",
                    needs["hard"],
                    mode,
                    scan=scan,
                )
            if needs["blocked"] and needs["probable"]:
                raise _refuse_with_candidates(
                    "probable_duplicate",
                    "This looks like a patient who is already registered. Open their record. "
                    "This site does not register past a probable duplicate.",
                    needs["probable"] + needs["possible"],
                    mode,
                    scan=scan,
                )
            if needs["blocked"]:
                # 'block', and a record the cut left out could be a probable
                # duplicate: the strict side of OPD-S2 refuses it unseen.
                raise _refuse_with_candidates(
                    "duplicate_check_incomplete",
                    "Too many registered records share this phone number, name or date of "
                    "birth for the duplicate check to compare them all, and one it did not "
                    "compare may be this patient. This site does not register a patient the "
                    "check could not clear. Search the patient index for them, and leave out "
                    "a phone number that is not the patient's own, such as a hospital number.",
                    needs["possible"],
                    mode,
                    scan=scan,
                )
            reviewed: list[dict] = []
            review_used = False
            review = clean["duplicate_review"]
            if needs["review_required"]:
                listed = needs["probable"] + needs["possible"]
                if review is None:
                    if needs["check_incomplete"] and not listed:
                        message = (
                            "Too many registered records share this phone number, name or date "
                            "of birth for the duplicate check to compare them all, so this may "
                            "be a patient who is already registered. Search the patient index "
                            "for them. To register a new patient anyway, give a reason."
                        )
                    elif needs["check_incomplete"]:
                        message = (
                            "This may be a patient who is already registered. Check each record "
                            "below. The check could not compare every record that shares these "
                            "details, so search the patient index too. To register a new "
                            "patient anyway, give a reason."
                        )
                    else:
                        message = (
                            "This may be a patient who is already registered. Check each "
                            "record below. To register a new patient anyway, give a reason."
                        )
                    raise _refuse_with_candidates(
                        "duplicate_review_required", message, listed, mode, scan=scan
                    )
                missing = [c for c in listed if c["patient_id"] not in review["candidate_ids"]]
                if missing:
                    raise _refuse_with_candidates(
                        "duplicate_review_stale",
                        "More possible matches were found since you checked. Review the list "
                        "again.",
                        listed,
                        mode,
                        scan=scan,
                    )
                reviewed = listed
                review_used = True

            # 4. The writes.
            if series is not None:
                from services.billing_masters import (
                    SeriesNotConfigured,
                    allocate_series_number,
                )

                try:
                    clean["uhid"] = allocate_series_number(
                        db, UHID_SERIES, require_configured=True
                    )["number"]
                except SeriesNotConfigured:
                    raise RegistrationConflict(
                        "uhid_series_not_configured",
                        "Hospital numbers cannot be issued yet: the UHID series is not set "
                        "up on this site.",
                    ) from None
                if _uhid_in_use_on(db, clean["uhid"]):
                    raise RegistrationConflict(
                        "uhid_series_collision",
                        "The next number in the UHID series is already in use. Ask an "
                        "administrator to check the series before registering anyone.",
                    )
            dob = clean["date_of_birth"]
            age = (
                pm.full_years(date.fromisoformat(dob), _today(today)) if dob else clean["age_years"]
            )
            address = _compose_address(clean)
            try:
                db.execute(
                    """INSERT INTO patients (id, user_id, name, mrn, age, date_of_birth, sex,
                           phone, emergency_contact_name, emergency_contact_phone, address,
                           status, admission_date, created_at)
                       VALUES (?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'registered', NULL, ?)""",
                    (
                        pid,
                        clean["name"],
                        clean["uhid"],
                        age,
                        dob,
                        clean["sex"],
                        clean["phone"],
                        clean["emergency_contact_name"],
                        clean["emergency_contact_phone"],
                        address,
                        now,
                    ),
                )
            except DBIntegrityError as exc:
                raise RegistrationConflict(
                    "uhid_in_use", "That hospital number is already in use."
                ) from exc
            db.execute(
                """INSERT INTO patient_demographics (patient_id, registered_at, registered_by,
                       registration_source, uhid_source, age_recorded_on, guardian_relation,
                       guardian_name, marital_status, preferred_language, address_line, city,
                       district, state_code, pin_code, alt_phone, email, id_document_type,
                       id_document_last4, occupation, row_version, updated_at, updated_by)
                   VALUES (?, ?, ?, 'desk', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)""",
                (
                    pid,
                    now,
                    user["id"],
                    config.UHID_SOURCE,
                    _today(today).isoformat() if dob is None else None,
                    *(clean[f] for f in DEMOGRAPHIC_FIELDS),
                    now,
                    user["id"],
                ),
            )
            pm.upsert_keys_on(
                db,
                pid,
                {
                    "name": clean["name"],
                    "phone": clean["phone"],
                    "date_of_birth": dob,
                    "sex": clean["sex"],
                },
            )
            abha_recorded = False
            if clean["abha_number"] or clean["abha_address"]:
                from services.abdm_service import (
                    AbdmError,
                    IdentityError,
                    record_abha_on,
                )

                try:
                    record_abha_on(
                        db,
                        patient_id=pid,
                        abha_number=clean["abha_number"],
                        abha_address=clean["abha_address"],
                        source=clean["abha_source"],
                        recorded_by=user["id"],
                        department=dept,
                    )
                except IdentityError as exc:
                    raise RegistrationConflict("abha_in_use", str(exc)) from exc
                except AbdmError as exc:
                    raise RegistrationError("bad_abha", str(exc)) from exc
                abha_recorded = True
            for c in reviewed:
                a, b = sorted((pid, c["patient_id"]))
                db.execute(
                    """INSERT INTO patient_duplicate_candidates (patient_id, candidate_patient_id,
                           score, reasons_json, status, source, detected_at, detected_by,
                           overridden_by, override_reason)
                       VALUES (?, ?, ?, ?, 'open', 'registration', ?, ?, ?, ?)
                       ON CONFLICT(patient_id, candidate_patient_id) DO NOTHING""",
                    (
                        a,
                        b,
                        int(c["score"]),
                        json.dumps(c["reasons"]),
                        now,
                        user["id"],
                        user["id"],
                        review["reason"],
                    ),
                )
            # opd-13: the payer the patient names (no admission, unverified)
            # and, when the site charges one, the registration fee, once.
            payer_recorded = None
            if clean["payer"] is not None:
                from services.billing_masters import (
                    MasterDataError,
                    record_patient_payer_on,
                )

                try:
                    payer_recorded = record_patient_payer_on(
                        db,
                        patient_id=pid,
                        payer_code=clean["payer"]["payer_code"],
                        policy_number=clean["payer"]["policy_number"],
                        member_id=clean["payer"]["member_id"],
                        created_by=user["id"],
                    )
                except MasterDataError as exc:
                    raise RegistrationError("bad_payer", str(exc)) from exc
            from services.opd_charges_service import raise_registration_fee_on

            registration_fee = raise_registration_fee_on(
                db, patient_id=pid, charged_by=user["id"], service_day=_today(today).isoformat()
            )
            correlation = f"registration-{pid}"
            # opd-19: the notice and consent step, in this transaction and
            # through consent_service, so the patient and the consent exist
            # together or not at all. Audited fail-closed with the
            # registration.
            consent_row = None
            if clean["consent"] is not None:
                from services.consent_service import record_consent_on

                taken = clean["consent"]
                try:
                    consent_row = record_consent_on(
                        db,
                        patient_id=pid,
                        purpose=REGISTRATION_CONSENT_PURPOSE,
                        method=taken["method"],
                        recorded_by=user["id"],
                        recorded_by_role=user.get("role"),
                        scope_detail=REGISTRATION_CONSENT_SCOPE,
                        notice_version=taken["notice_version"],
                        notice_language=taken["notice_language"],
                        granted_at=now,
                        given_by=taken["given_by"],
                        proxy_name=taken["proxy_name"],
                        proxy_relationship=taken["proxy_relationship"],
                        evidence_ref=taken["evidence_ref"],
                    )
                except ValueError as exc:
                    raise RegistrationError("bad_consent", str(exc)) from exc
                _audit(
                    db,
                    action="consent_recorded",
                    user=user,
                    patient_id=pid,
                    target_type="consent_record",
                    target_id=consent_row["id"],
                    summary=f"Consent to treatment recorded at registration for {pid}",
                    metadata={
                        "lane": "registration",
                        "purpose": REGISTRATION_CONSENT_PURPOSE,
                        "method": taken["method"],
                        "given_by": taken["given_by"],
                        "notice_acknowledged": True,
                        "notice_version": taken["notice_version"],
                        "notice_language": taken["notice_language"],
                    },
                    request_meta=request_meta,
                    correlation_id=correlation,
                )
            _audit(
                db,
                action="patient_registered",
                user=user,
                patient_id=pid,
                summary=f"Patient {pid} registered at the desk",
                metadata={
                    "replay": False,
                    "registration_source": "desk",
                    "uhid_source": config.UHID_SOURCE,
                    "abha_recorded": abha_recorded,
                    "stated_age": dob is None,
                    "idempotency_key": bool(key),
                    "duplicate_review": review_used,
                    "candidate_scan_truncated": scan["truncated"],
                    "check_incomplete": scan["check_incomplete"],
                    **(
                        {
                            "payer_code": payer_recorded["payer_code"],
                            "payer_type": payer_recorded["payer_type"],
                            "payer_policy_id": payer_recorded["policy_id"],
                        }
                        if payer_recorded
                        else {}
                    ),
                    **(
                        {"registration_fee_charge_id": registration_fee.charge_id}
                        if registration_fee
                        else {}
                    ),
                    **({"consent_id": consent_row["id"]} if consent_row else {}),
                },
                request_meta=request_meta,
                correlation_id=correlation,
            )
            if review_used:
                summary = (
                    f"Registered {pid} past {len(reviewed)} possible duplicate(s)"
                    if reviewed
                    else f"Registered {pid} past a duplicate check that could not compare "
                    "every record"
                )
                _audit(
                    db,
                    action="registration_duplicate_override",
                    user=user,
                    patient_id=pid,
                    summary=summary,
                    metadata={
                        "mode": mode,
                        "reason": review["reason"],
                        "candidate_ids": [c["patient_id"] for c in reviewed],
                        "scores": [c["score"] for c in reviewed],
                        "bands": [c["band"] for c in reviewed],
                        **scan,
                    },
                    request_meta=request_meta,
                    correlation_id=correlation,
                )
            record = get_registration_on(db, pid, today=today)
    except _Refusal as refusal:
        # The refusal is audited on its own write (fail-closed): if it cannot
        # be written, the desk gets 503 and sees no candidates.
        with db_session() as db:
            _audit(
                db,
                action=refusal.action,
                user=user,
                outcome="denied",
                patient_id=None,
                target_type="patient_index",
                target_id=None,
                summary=f"Registration refused: {refusal.error.code}",
                metadata=refusal.metadata,
                request_meta=request_meta,
            )
        raise refusal.error from refusal
    result = {
        "patient": record,
        "replayed": False,
        "duplicate_review": (
            {
                "reason": review["reason"],
                "candidate_ids": [c["patient_id"] for c in reviewed],
                "check_incomplete": scan["check_incomplete"],
            }
            if review_used
            else None
        ),
    }
    # opd-13: present only when the desk recorded a payer, or the site
    # charges a registration fee, so every other registration answers as
    # before.
    if payer_recorded:
        result["payer"] = {
            k: payer_recorded[k]
            for k in ("policy_id", "payer_code", "payer_name", "payer_type", "verified")
        }
    if registration_fee:
        result["registration_fee"] = registration_fee.as_dict()
    # opd-19: present only when the desk recorded the consent step.
    if consent_row:
        result["consents"] = [_consent_view(consent_row)]
    return result


# ── The record, its history and the card ─────────────────────────────────────


def _abha_rows_on(db, patient_id: str) -> list[dict]:
    from services.abdm_service import VERIFICATION_LABEL, format_abha_number

    rows = db.execute(
        "SELECT id, abha_number, abha_address, verification_status, source, is_primary, "
        "created_at FROM abha_identities WHERE patient_id = ? AND status = 'active' "
        "ORDER BY is_primary DESC, id DESC",
        (patient_id,),
    ).fetchall()
    out = []
    for r in rows:
        status = r["verification_status"] or "unverified"
        out.append(
            {
                "id": r["id"],
                "abha_number": format_abha_number(r["abha_number"]) if r["abha_number"] else None,
                "abha_address": r["abha_address"],
                "verification_status": status,
                "verification_label": VERIFICATION_LABEL.get(status, status),
                "is_verified": status == "verified",
                "is_primary": bool(r["is_primary"]),
                "source": r["source"],
            }
        )
    return out


def get_registration_on(db, patient_id: str, *, today: date | None = None) -> dict:
    """The full registration record for a reader the route already scoped."""
    from services.abdm_service import NO_GATEWAY_SHORT

    row = db.execute(
        "SELECT p.*, EXISTS (SELECT 1 FROM admissions a WHERE a.patient_id = p.id "
        "AND a.status = 'active') AS inpatient_now FROM patients p WHERE p.id = ?",
        (patient_id,),
    ).fetchone()
    if row is None:
        raise RegistrationNotFound("patient_not_found", "Patient not found")
    demo = db.execute(
        "SELECT * FROM patient_demographics WHERE patient_id = ?", (patient_id,)
    ).fetchone()
    demo = dict(demo) if demo is not None else {}
    age, stated = pm.current_age(
        date_of_birth=row["date_of_birth"],
        stated_age=row["age"],
        age_recorded_on=demo.get("age_recorded_on"),
        created_at=row["created_at"],
        today=today,
    )
    names = _user_names(db, [demo.get("registered_by"), demo.get("updated_by")])
    dob = pm.dob_key(row["date_of_birth"])
    return {
        "patient_id": row["id"],
        "uhid": row["mrn"],
        "name": row["name"],
        "sex": pm.sex_key(row["sex"]) or row["sex"],
        "date_of_birth": dob,
        "age": age,
        "age_stated": stated,
        "age_recorded_on": demo.get("age_recorded_on"),
        "phone": row["phone"],
        "emergency_contact_name": row["emergency_contact_name"],
        "emergency_contact_phone": row["emergency_contact_phone"],
        "address": row["address"],
        "record_status": row["status"],
        "inpatient_now": bool(row["inpatient_now"]),
        "demographics": {f: demo.get(f) for f in DEMOGRAPHIC_FIELDS},
        "registration": {
            "registered_at": demo.get("registered_at"),
            "registered_by": demo.get("registered_by"),
            "registered_by_name": names.get(demo.get("registered_by")),
            "registration_source": demo.get("registration_source"),
            "uhid_source": demo.get("uhid_source"),
            "updated_at": demo.get("updated_at"),
            "updated_by_name": names.get(demo.get("updated_by")),
        },
        "row_version": int(demo.get("row_version") or 0),
        "discharge_link_possible": dob is not None,
        "date_of_birth_hint": None if dob else NO_DOB_HINT,
        "abha": _abha_rows_on(db, patient_id),
        "abha_notice": NO_GATEWAY_SHORT,
        "allergies_recorded_here": False,
    }


def get_registration(patient_id: str, *, today: date | None = None) -> dict:
    with db_session() as db:
        return get_registration_on(db, patient_id, today=today)


def redact_for_opd_arm(record: dict) -> dict:
    """The registration record as a reader who came in through an OPD visit
    sees it (see OPD_ARM_HIDDEN_FIELDS): whether a date of birth is on file but
    never what it is, the phone's last 4 digits only, no address, next of kin,
    identity document or ABHA."""
    out = dict(record)
    out["phone_last4"] = pm.phone_last4(record.get("phone"))
    for field in OPD_ARM_HIDDEN_FIELDS:
        out[field] = None
    out["demographics"] = {
        k: (None if k in OPD_ARM_HIDDEN_DEMOGRAPHICS else v)
        for k, v in (record.get("demographics") or {}).items()
    }
    out["abha_on_file"] = bool(record.get("abha"))
    out["abha"] = []
    out["hidden_fields"] = sorted(OPD_ARM_HIDDEN)
    return out


def redact_changes_for_opd_arm(history: dict) -> dict:
    """The change history with the old and new values of hidden fields removed."""
    out = dict(history)
    rows = []
    for row in history.get("changes") or []:
        row = dict(row)
        if row.get("field") in OPD_ARM_HIDDEN:
            row["old_value"] = None
            row["new_value"] = None
            row["values_hidden"] = True
        rows.append(row)
    out["changes"] = rows
    return out


def change_history(patient_id: str) -> dict:
    with db_session() as db:
        if db.execute("SELECT 1 FROM patients WHERE id = ?", (patient_id,)).fetchone() is None:
            raise RegistrationNotFound("patient_not_found", "Patient not found")
        rows = [
            dict(r)
            for r in db.execute(
                "SELECT id, field, old_value, new_value, reason, changed_by, changed_at, "
                "correlation_id FROM patient_identity_changes WHERE patient_id = ? "
                "ORDER BY changed_at DESC, id DESC",
                (patient_id,),
            ).fetchall()
        ]
        names = _user_names(db, [r["changed_by"] for r in rows])
    for r in rows:
        r["changed_by_name"] = names.get(r["changed_by"])
    return {"patient_id": patient_id, "changes": rows}


def uhid_card(patient_id: str, *, today: date | None = None) -> dict:
    """What the printed UHID card carries: number, name, age and sex. No date
    of birth, phone or address."""
    with db_session() as db:
        card = index_cards_on(db, [patient_id], today=today).get(patient_id)
        if card is None:
            raise RegistrationNotFound("patient_not_found", "Patient not found")
        demo = db.execute(
            "SELECT registered_at, uhid_source FROM patient_demographics WHERE patient_id = ?",
            (patient_id,),
        ).fetchone()
        created = db.execute(
            "SELECT created_at FROM patients WHERE id = ?", (patient_id,)
        ).fetchone()[0]
    return {
        "patient_id": patient_id,
        "uhid": card["uhid"],
        "name": card["name"],
        "sex": card["sex"],
        "age": card["age"],
        "age_stated": card["age_stated"],
        "registered_on": _local_date((demo["registered_at"] if demo else None) or created),
        "uhid_source": demo["uhid_source"] if demo else None,
    }


# ── Corrections ──────────────────────────────────────────────────────────────


def _old_text(value: Any) -> str | None:
    return None if value is None else str(value)


def correct_demographics(
    patient_id: str,
    changes: dict,
    *,
    row_version: Any,
    reason: Any,
    user: dict,
    may_edit_fields: bool,
    may_correct_uhid: bool,
    request_meta: dict | None = None,
    opd_arm_only: bool = False,
    today: date | None = None,
) -> dict:
    """Correct a registered identity, with a reason, against the row_version
    the editor read. Every changed field is appended to
    patient_identity_changes (append-only) in the same transaction as the
    change and its patient_identity audit rows.

    `changes` holds only the fields the caller sent (a field sent as None or
    '' clears it, where clearing is allowed). The route decides
    `may_edit_fields` (registration.write) and `may_correct_uhid` (the
    administrator); this function refuses what they do not allow.

    The date of birth and the stated age follow the rule registration
    enforces, worked out on the record AFTER the change: a date of birth or a
    stated age, never both, never neither, never an estimated age. Removing a
    date of birth needs the age the patient states in the same request, and
    that age is recorded with today's date (age_recorded_on); the age worked
    out from the removed date of birth is never kept as if the patient had
    stated it. A stated age cannot be cleared without a date of birth in its
    place.
    """
    changes = dict(changes or {})
    unknown = set(changes) - set(PATIENT_FIELDS) - set(DEMOGRAPHIC_FIELDS) - {"uhid"}
    if unknown:
        raise RegistrationError(
            "unknown_field",
            "Only registration details can be corrected here.",
            fields=sorted(unknown),
        )
    if not changes:
        raise RegistrationError("no_change", "Nothing was changed.")
    hidden = sorted(set(changes) & OPD_ARM_HIDDEN)
    if opd_arm_only and hidden:
        raise RegistrationForbidden(
            "needs_full_scope",
            "Date of birth, contact, address and identity document details can be corrected "
            "only by the registration desk serving the whole hospital or by the admitting ward.",
            fields=hidden,
        )
    if "uhid" in changes and not may_correct_uhid:
        raise RegistrationForbidden(
            "uhid_correction_admin_only", "Only an administrator can correct a hospital number."
        )
    if set(changes) - {"uhid"} and not may_edit_fields:
        raise RegistrationForbidden(
            "registration_write_required", "Your account cannot correct registration details."
        )
    why = _reason(reason)
    try:
        expected = int(row_version)
    except (TypeError, ValueError):
        raise RegistrationError(
            "row_version_required", "Reload the record before correcting it."
        ) from None

    new: dict[str, Any] = {}
    for field, value in changes.items():
        if field == "name":
            try:
                new["name"] = validate_patient_name(value)
            except PatientNameError as exc:
                raise RegistrationError("name_invalid", str(exc)) from exc
        elif field == "sex":
            new["sex"] = _sex(value)
        elif field == "date_of_birth":
            new["date_of_birth"] = _dob(value)
        elif field == "age_years":
            new["age_years"] = _stated_age(value)
        elif field in ("phone", "emergency_contact_phone"):
            new[field] = _phone(value, field=field)
        elif field == "emergency_contact_name":
            new[field] = _clean(value)
        elif field == "uhid":
            new["uhid"] = _clean(value, limit=MAX_UHID)
        else:
            new[field] = _demographic(field, value)

    now = utc_iso_now()
    correlation = f"identity-correction-{uuid.uuid4()}"
    with db_session(immediate=True) as db:
        row = db.execute("SELECT * FROM patients WHERE id = ?", (patient_id,)).fetchone()
        if row is None:
            raise RegistrationNotFound("patient_not_found", "Patient not found")
        demo = db.execute(
            "SELECT * FROM patient_demographics WHERE patient_id = ?", (patient_id,)
        ).fetchone()
        demo = dict(demo) if demo is not None else None
        current_version = int(demo["row_version"]) if demo else 0
        if expected != current_version:
            raise RegistrationConflict(
                "stale_row_version",
                "Someone changed this record after you opened it. Reload it and make your "
                "correction again.",
                current_row_version=current_version,
            )

        old_dob = pm.dob_key(row["date_of_birth"])
        # The age the patient STATED. Registration, a date of birth correction
        # and the admission door all store the age worked out from a date of
        # birth in patients.age, and that is not a stated age.
        old_stated_age = row["age"] if old_dob is None else None
        old_patient = {
            "name": row["name"],
            "sex": row["sex"],
            "phone": row["phone"],
            "date_of_birth": row["date_of_birth"],
            "age_years": old_stated_age,
            "emergency_contact_name": row["emergency_contact_name"],
            "emergency_contact_phone": row["emergency_contact_phone"],
            "uhid": row["mrn"],
        }
        old_demo = {f: (demo or {}).get(f) for f in DEMOGRAPHIC_FIELDS}

        # Date of birth and stated age, worked out on the record AFTER the
        # change: one of them, never both, never neither, never an estimated
        # DOB (the rule _validate_registration enforces at registration).
        dob_after = new["date_of_birth"] if "date_of_birth" in new else old_dob
        if dob_after:
            if "age_years" in new and new["age_years"] is not None:
                raise RegistrationError(
                    "age_with_date_of_birth", "Give a date of birth or a stated age, not both."
                )
            stated_age_after = None
        elif "age_years" in new:
            stated_age_after = new["age_years"]
        elif old_dob is not None:
            # The date of birth is removed and no stated age came with it: the
            # age worked out from a date of birth the desk has just declared
            # wrong must never stay on the record as if the patient stated it.
            stated_age_after = None
        else:
            stated_age_after = old_stated_age
        # Only a correction that touches the date of birth or the age is held
        # to this, so a record that already lacked both (an old admission-door
        # row) can still have its phone or address corrected; no correction
        # can leave a record in that state.
        if (
            dob_after is None
            and stated_age_after is None
            and ("date_of_birth" in new or "age_years" in new)
        ):
            raise RegistrationError(
                "age_or_date_of_birth_required",
                "Removing the date of birth needs the age the patient states instead, in the "
                "same correction."
                if old_dob is not None
                else "A stated age cannot be removed without a date of birth in its place. Record "
                "the date of birth, or the age the patient states now.",
            )
        if "uhid" in new:
            if not new["uhid"]:
                raise RegistrationError("uhid_required", "A hospital number cannot be blank.")
            if config.UHID_SOURCE == "hospital":
                new["uhid"] = _hospital_uhid(new["uhid"])
            elif not uhid_check(new["uhid"], _uhid_series_on(db)):
                raise RegistrationError(
                    "uhid_check_digit", "That hospital number fails its check digit."
                )
            if _uhid_in_use_on(db, new["uhid"], exclude_patient_id=patient_id):
                raise RegistrationConflict("uhid_in_use", "That hospital number is already in use.")

        diffs: list[tuple[str, str | None, str | None]] = []
        for field, value in new.items():
            old = old_patient.get(field) if field in old_patient else old_demo.get(field)
            if field == "sex":
                same = pm.sex_key(old) == value
            elif field == "date_of_birth":
                same = pm.dob_key(old) == value
            elif field == "age_years":
                same = (old if dob_after is None else None) == value
            else:
                same = (_old_text(old) or None) == (_old_text(value) or None)
            if not same:
                diffs.append((field, _old_text(old), _old_text(value)))
        if not diffs:
            raise RegistrationError("no_change", "Nothing was changed.")

        changed = {f for f, _o, _n in diffs}
        # patients
        sets, params = [], []
        mapping = {
            "name": "name",
            "sex": "sex",
            "phone": "phone",
            "uhid": "mrn",
            "emergency_contact_name": "emergency_contact_name",
            "emergency_contact_phone": "emergency_contact_phone",
        }
        for field, column in mapping.items():
            if field in changed:
                sets.append(f"{column} = ?")
                params.append(new[field])
        age_recorded_on = (demo or {}).get("age_recorded_on")
        if "date_of_birth" in changed:
            sets.append("date_of_birth = ?")
            params.append(new["date_of_birth"])
            if new["date_of_birth"]:
                sets.append("age = ?")
                params.append(
                    pm.full_years(date.fromisoformat(new["date_of_birth"]), _today(today))
                )
                age_recorded_on = None
        if "age_years" in changed:
            sets.append("age = ?")
            params.append(new["age_years"])
            age_recorded_on = _today(today).isoformat() if new["age_years"] is not None else None
        demo_after = {**old_demo, **{f: new[f] for f in DEMOGRAPHIC_FIELDS if f in changed}}
        if changed & set(_ADDRESS_PARTS):
            sets.append("address = ?")
            params.append(_compose_address(demo_after))
        if sets:
            try:
                db.execute(
                    f"UPDATE patients SET {', '.join(sets)} WHERE id = ?", (*params, patient_id)
                )
            except DBIntegrityError as exc:
                raise RegistrationConflict(
                    "uhid_in_use", "That hospital number is already in use."
                ) from exc
        # patient_demographics (created on a record's first correction)
        if demo is None:
            source = (
                "import"
                if row["import_run_id"]
                else (
                    "admission"
                    if db.execute(
                        "SELECT 1 FROM admissions WHERE patient_id = ?", (patient_id,)
                    ).fetchone()
                    else None
                )
            )
            db.execute(
                f"""INSERT INTO patient_demographics (patient_id, registration_source,
                        age_recorded_on, {", ".join(DEMOGRAPHIC_FIELDS)}, row_version,
                        updated_at, updated_by)
                    VALUES (?, ?, ?, {", ".join("?" for _ in DEMOGRAPHIC_FIELDS)}, 1, ?, ?)""",
                (
                    patient_id,
                    source,
                    age_recorded_on,
                    *(demo_after[f] for f in DEMOGRAPHIC_FIELDS),
                    now,
                    user["id"],
                ),
            )
        else:
            demo_sets = [f"{f} = ?" for f in DEMOGRAPHIC_FIELDS if f in changed]
            demo_params = [demo_after[f] for f in DEMOGRAPHIC_FIELDS if f in changed]
            cur = db.execute(
                f"""UPDATE patient_demographics SET {
                    ", ".join(
                        demo_sets
                        + [
                            "age_recorded_on = ?",
                            "row_version = row_version + 1",
                            "updated_at = ?",
                            "updated_by = ?",
                        ]
                    )
                }
                     WHERE patient_id = ? AND row_version = ?""",
                (*demo_params, age_recorded_on, now, user["id"], patient_id, current_version),
            )
            if cur.rowcount != 1:
                raise RegistrationConflict(
                    "stale_row_version",
                    "Someone changed this record after you opened it.",
                    current_row_version=current_version,
                )
        for field, old, value in diffs:
            db.execute(
                """INSERT INTO patient_identity_changes (patient_id, field, old_value,
                       new_value, reason, changed_by, changed_at, correlation_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (patient_id, field, old, value, why, user["id"], now, correlation),
            )
        after = db.execute(
            "SELECT name, phone, date_of_birth, sex FROM patients WHERE id = ?", (patient_id,)
        ).fetchone()
        pm.upsert_keys_on(db, patient_id, after)
        fields = sorted(changed - {"uhid"})
        if fields:
            _audit(
                db,
                action="patient_demographics_corrected",
                user=user,
                patient_id=patient_id,
                summary=f"Registration details corrected for {patient_id}",
                metadata={"fields": fields, "row_version": current_version + 1},
                request_meta=request_meta,
                correlation_id=correlation,
            )
        if "uhid" in changed:
            _audit(
                db,
                action="patient_uhid_corrected",
                user=user,
                patient_id=patient_id,
                summary=f"Hospital number corrected for {patient_id}",
                metadata={"row_version": current_version + 1},
                request_meta=request_meta,
                correlation_id=correlation,
            )
        record = get_registration_on(db, patient_id, today=today)
    return {"patient": record, "changed_fields": sorted(changed)}


# ── Identity check (OPD-S1) ──────────────────────────────────────────────────


def _stated(stated: dict) -> dict:
    stated = dict(stated or {})
    name = _clean(stated.get("name"))
    dob = _dob(stated.get("date_of_birth")) if stated.get("date_of_birth") else None
    age = _stated_age(stated.get("age_years"))
    last4 = _clean(stated.get("phone_last4"), limit=10)
    if last4 is not None and not re.fullmatch(r"\d{4}", last4):
        raise RegistrationError("bad_phone_last4", "The last 4 digits of the phone are 4 digits.")
    uhid = _clean(stated.get("uhid"), limit=MAX_UHID)
    if not name or (dob is None and age is None) or (last4 is None and uhid is None):
        raise RegistrationError(
            "identifiers_required",
            "Ask the patient for their name, their date of birth or age, and the last 4 "
            "digits of their phone or their UHID.",
        )
    return {
        "name": name,
        "date_of_birth": dob,
        "age_years": age,
        "phone_last4": last4,
        "uhid": uhid,
    }


def evaluate_identity_on(db, patient_id: str, stated: dict, *, today: date | None = None) -> dict:
    """Compare what the patient stated with the record. Writes nothing and
    returns {"result": 'match'|'no_match', "checked": [kinds], "mismatched":
    [kinds]}. `mismatched` is for the audit row only and must never reach the
    screen: the desk learns 'match' or 'no match' and nothing else.
    `stated` must already have passed _stated (check_identity does that)."""
    row = db.execute(
        "SELECT p.name, p.mrn, p.phone, p.date_of_birth, p.age, p.created_at, "
        "d.age_recorded_on, d.alt_phone FROM patients p "
        "LEFT JOIN patient_demographics d ON d.patient_id = p.id WHERE p.id = ?",
        (patient_id,),
    ).fetchone()
    if row is None:
        raise RegistrationNotFound("patient_not_found", "Patient not found")
    checked, mismatched = ["name"], []
    if not pm.stated_name_matches(stated["name"], row["name"]):
        mismatched.append("name")
    stored_dob = pm.dob_key(row["date_of_birth"])
    stored_age, _ = pm.current_age(
        date_of_birth=row["date_of_birth"],
        stated_age=row["age"],
        age_recorded_on=row["age_recorded_on"],
        created_at=row["created_at"],
        today=today,
    )
    if stated.get("date_of_birth"):
        checked.append("date_of_birth")
        if stored_dob is not None:
            ok = stated["date_of_birth"] == stored_dob
        elif stored_age is not None:
            said_age = pm.full_years(date.fromisoformat(stated["date_of_birth"]), _today(today))
            ok = abs(said_age - stored_age) <= IDENTITY_AGE_TOLERANCE
        else:
            ok = False
        if not ok:
            mismatched.append("date_of_birth")
    if stated.get("age_years") is not None:
        checked.append("age_years")
        if (
            stored_age is None
            or abs(int(stated["age_years"]) - stored_age) > IDENTITY_AGE_TOLERANCE
        ):
            mismatched.append("age_years")
    if stated.get("phone_last4"):
        checked.append("phone_last4")
        held = {pm.phone_last4(row["phone"]), pm.phone_last4(row["alt_phone"])} - {None}
        if stated["phone_last4"] not in held:
            mismatched.append("phone_last4")
    if stated.get("uhid"):
        checked.append("uhid")
        if pm.uhid_key(stated["uhid"]) != pm.uhid_key(row["mrn"]):
            mismatched.append("uhid")
    return {
        "result": "no_match" if mismatched else "match",
        "checked": checked,
        "mismatched": mismatched,
    }


def _recent_failures_on(db, user_id: str, patient_id: str) -> int:
    since = (datetime.now(UTC) - IDENTITY_FAILURE_WINDOW).isoformat()
    return int(
        db.execute(
            "SELECT COUNT(*) FROM audit_logs WHERE patient_id = ? AND action_type = "
            "'identity_check_failed' AND actor_user_id = ? AND timestamp >= ?",
            (patient_id, user_id, since),
        ).fetchone()[0]
    )


_NOT_DECIDED = object()


def check_identity(
    patient_id: str,
    stated: dict,
    *,
    user: dict,
    department: Any = _NOT_DECIDED,
    request_meta: dict | None = None,
    purpose: str | None = None,
    today: date | None = None,
) -> dict:
    """The stated-identifier check (ruling OPD-S1). Returns {"result":
    'match'|'no_match', "checked": [kinds], "checked_at"}; never a stored
    value. Audited every time (fail-closed). After IDENTITY_FAILURE_LIMIT
    failures by this user for this patient within the last hour, raises
    RegistrationRateLimited (429) without looking at the identifiers.

    SCOPE. The check exists for a patient found by the hospital-wide index
    search, so it reaches past the caller's ward, but no further than a visit
    could: `department` (permissions.department_scope of the authenticated
    caller; derived from `user` when not passed) may check any patient in its
    own scope or with no ACTIVE admission. Another ward's inpatient answers 404
    exactly like an unknown id, and the attempt is audited.
    """
    said = _stated(stated)
    pid = _clean(patient_id, limit=80)
    if not pid:
        raise RegistrationError("patient_required", "Choose the patient to check.")
    dept = _department(user) if department is _NOT_DECIDED else department
    limited = out_of_scope = False
    with db_session(immediate=True) as db:
        if db.execute("SELECT 1 FROM patients WHERE id = ?", (pid,)).fetchone() is None:
            raise RegistrationNotFound("patient_not_found", "Patient not found")
        if dept is not None:
            from permissions import patient_in_scope

            admitted = (
                db.execute(
                    "SELECT 1 FROM admissions WHERE patient_id = ? AND status = 'active' LIMIT 1",
                    (pid,),
                ).fetchone()
                is not None
            )
            out_of_scope = admitted and not patient_in_scope(db, pid, dept)
        failures = _recent_failures_on(db, user["id"], pid) if not out_of_scope else 0
        if out_of_scope:
            pass
        elif failures >= IDENTITY_FAILURE_LIMIT:
            limited = True
        else:
            verdict = evaluate_identity_on(db, pid, said, today=today)
            passed = verdict["result"] == "match"
            _audit(
                db,
                action="identity_check_passed" if passed else "identity_check_failed",
                user=user,
                outcome="success" if passed else "failure",
                patient_id=pid,
                summary=f"Identity check for {pid}: {verdict['result']}",
                metadata={
                    "checked": verdict["checked"],
                    "mismatched": verdict["mismatched"],
                    "purpose": purpose,
                    "failures_in_window": failures + (0 if passed else 1),
                },
                request_meta=request_meta,
            )
    if out_of_scope:
        log_audit_event(
            correlation_id=str(uuid.uuid4()),
            event_category="authorization",
            action_type="patient_scope_denied",
            outcome="denied",
            summary_message=(
                f"Identity check: patient {pid} is another department's inpatient ({dept})"
            ),
            actor_user_id=user.get("id"),
            actor_role=user.get("role"),
            patient_id=pid,
            request_path=(request_meta or {}).get("request_path"),
            http_method=(request_meta or {}).get("http_method"),
            ip_address=(request_meta or {}).get("ip_address"),
            user_agent=(request_meta or {}).get("user_agent"),
            metadata={
                "department": dept,
                "scope_source": "authenticated_user",
                "identity_check": True,
            },
        )
        raise RegistrationNotFound("patient_not_found", "Patient not found")
    if limited:
        with db_session() as db:
            _audit(
                db,
                action="identity_check_rate_limited",
                user=user,
                outcome="denied",
                patient_id=pid,
                summary=f"Identity check refused for {pid}: too many failures",
                metadata={"failures_in_window": failures, "purpose": purpose},
                request_meta=request_meta,
            )
        raise RegistrationRateLimited(
            "identity_check_locked",
            "Too many identity checks for this patient did not match in the last hour. "
            "Ask a colleague to confirm who the patient is.",
            retry_after_seconds=int(IDENTITY_FAILURE_WINDOW.total_seconds()),
        )
    return {"result": verdict["result"], "checked": verdict["checked"], "checked_at": utc_iso_now()}


# ── The duplicate worklist ───────────────────────────────────────────────────


def list_duplicates(*, status: str = "open", limit: int = 50, today: date | None = None) -> dict:
    statuses = ("open", "not_duplicate", "confirmed_duplicate", "merged", "all")
    if status not in statuses:
        raise RegistrationError("bad_status", "Choose open, resolved or all.")
    limit = max(1, min(int(limit or 50), DUPLICATE_LIST_MAX))
    with db_session() as db:
        where, params = ("", []) if status == "all" else ("WHERE status = ?", [status])
        rows = [
            dict(r)
            for r in db.execute(
                f"SELECT * FROM patient_duplicate_candidates {where} "
                "ORDER BY detected_at DESC, id DESC LIMIT ?",
                (*params, limit),
            ).fetchall()
        ]
        ids = [i for r in rows for i in (r["patient_id"], r["candidate_patient_id"])]
        cards = index_cards_on(db, ids, today=today)
        names = _user_names(
            db, [u for r in rows for u in (r["overridden_by"], r["resolved_by"], r["detected_by"])]
        )
        counts = {
            r["status"]: r["n"]
            for r in db.execute(
                "SELECT status, COUNT(*) AS n FROM patient_duplicate_candidates GROUP BY status"
            )
        }
    items = []
    for r in rows:
        reasons = json.loads(r["reasons_json"] or "[]")
        items.append(
            {
                "id": r["id"],
                "status": r["status"],
                "score": r["score"],
                "band": pm.band_for(int(r["score"] or 0), reasons),
                "reasons": reasons,
                "reason_labels": pm.reason_labels(reasons),
                "source": r["source"],
                "detected_at": r["detected_at"],
                "detected_by_name": names.get(r["detected_by"]),
                "overridden_by": r["overridden_by"],
                "overridden_by_name": names.get(r["overridden_by"]),
                "override_reason": r["override_reason"],
                "resolved_by_name": names.get(r["resolved_by"]),
                "resolved_at": r["resolved_at"],
                "resolution_reason": r["resolution_reason"],
                "patient": cards.get(r["patient_id"]),
                "candidate": cards.get(r["candidate_patient_id"]),
            }
        )
    return {"status": status, "items": items, "counts": counts, "patient_ids": sorted(set(ids))}


def resolve_duplicate(
    candidate_id: Any, *, resolution: str, reason: Any, user: dict, request_meta: dict | None = None
) -> dict:
    """Mark a worklist pair as not the same person, or as the same person.
    Records are never merged here (merging is a later, separate executor). The
    person who registered past the warning may not resolve it."""
    if resolution not in DUPLICATE_RESOLUTIONS:
        raise RegistrationError(
            "bad_resolution", "Choose 'not the same person' or 'the same person'."
        )
    why = _reason(reason)
    try:
        cid = int(candidate_id)
    except (TypeError, ValueError):
        raise RegistrationNotFound(
            "duplicate_not_found", "That worklist item does not exist."
        ) from None
    refusal = None
    with db_session(immediate=True) as db:
        row = db.execute(
            "SELECT * FROM patient_duplicate_candidates WHERE id = ?", (cid,)
        ).fetchone()
        if row is None:
            raise RegistrationNotFound("duplicate_not_found", "That worklist item does not exist.")
        if row["status"] != "open":
            raise RegistrationConflict(
                "already_resolved",
                "Someone has already resolved this item.",
                status_now=row["status"],
            )
        if row["overridden_by"] and row["overridden_by"] == user["id"]:
            refusal = row
        else:
            now = utc_iso_now()
            db.execute(
                "UPDATE patient_duplicate_candidates SET status = ?, resolved_by = ?, "
                "resolved_at = ?, resolution_reason = ? WHERE id = ? AND status = 'open'",
                (resolution, user["id"], now, why, cid),
            )
            _audit(
                db,
                action="duplicate_resolved",
                user=user,
                patient_id=row["patient_id"],
                target_type="patient_duplicate_candidate",
                target_id=str(cid),
                summary=f"Duplicate worklist item {cid} resolved: {resolution}",
                metadata={
                    "resolution": resolution,
                    "reason": why,
                    "pair": [row["patient_id"], row["candidate_patient_id"]],
                    "source": row["source"],
                },
                request_meta=request_meta,
            )
    if refusal is not None:
        with db_session() as db:
            _audit(
                db,
                action="duplicate_resolution_refused",
                user=user,
                outcome="denied",
                patient_id=refusal["patient_id"],
                target_type="patient_duplicate_candidate",
                target_id=str(cid),
                summary=f"Worklist item {cid}: the person who overrode cannot resolve it",
                metadata={
                    "reason_code": "resolver_is_overrider",
                    "pair": [refusal["patient_id"], refusal["candidate_patient_id"]],
                },
                request_meta=request_meta,
            )
        raise RegistrationForbidden(
            "resolver_is_overrider",
            "You registered past this warning, so someone else must resolve it.",
        )
    return {"id": cid, "status": resolution, "resolved_by": user["id"]}


def sweep_duplicates(
    *,
    user: dict,
    max_pairs: int = SWEEP_MAX_PAIRS,
    request_meta: dict | None = None,
    today: date | None = None,
) -> dict:
    """A bounded pass over the whole index that opens worklist rows (source
    'sweep') for pairs scoring in a band, and for records whose hospital
    numbers differ only in case or separators.

    One ordered scan per key (UHID, phone, name, date of birth), grouped in
    memory one group at a time, so the cost is a sort per key rather than a
    query per group. Opens at most `max_pairs` new rows (ceiling
    SWEEP_PAIRS_CEILING) and skips, and counts, any group of more than
    SWEEP_MAX_GROUP records sharing one key.
    """
    from itertools import groupby

    try:
        cap = int(max_pairs)
    except (TypeError, ValueError):
        raise RegistrationError("bad_limit", "The sweep limit is a whole number.") from None
    cap = max(1, min(cap, SWEEP_PAIRS_CEILING))
    now = utc_iso_now()
    groups = skipped = 0
    truncated = False
    to_open: list[tuple] = []
    facts_sql = (
        "p.id AS patient_id, k.name_key, k.phone_key, k.dob, k.sex, p.age, "
        "p.date_of_birth, p.created_at, d.age_recorded_on "
        "FROM patients p JOIN patient_match_keys k ON k.patient_id = p.id "
        "LEFT JOIN patient_demographics d ON d.patient_id = p.id"
    )
    scans = (
        ("uhid", f"SELECT {pm.UHID_KEY_SQL.format(col='p.mrn')} AS g, {facts_sql} ORDER BY g"),
        ("phone", f"SELECT k.phone_key AS g, {facts_sql} WHERE k.phone_key IS NOT NULL ORDER BY g"),
        ("name", f"SELECT k.name_key AS g, {facts_sql} WHERE k.name_key IS NOT NULL ORDER BY g"),
        ("dob", f"SELECT k.dob AS g, {facts_sql} WHERE k.dob IS NOT NULL ORDER BY g"),
    )
    with db_session(immediate=True) as db:
        synced = pm.sync_keys(db, remove_orphans=True)
        seen: set = {
            (r[0], r[1])
            for r in db.execute(
                "SELECT patient_id, candidate_patient_id FROM patient_duplicate_candidates"
            )
        }
        for kind, sql in scans:
            if truncated:
                break
            rows = db.execute(sql).fetchall()
            for g, member_iter in groupby(rows, key=lambda r: r["g"]):
                if g is None:
                    continue
                members = list(member_iter)
                if len(members) < 2:
                    continue
                if len(members) > SWEEP_MAX_GROUP:
                    skipped += 1
                    continue
                groups += 1
                facts = {}
                for r in members:
                    age, _ = pm.current_age(
                        date_of_birth=r["date_of_birth"],
                        stated_age=r["age"],
                        age_recorded_on=r["age_recorded_on"],
                        created_at=r["created_at"],
                        today=today,
                    )
                    facts[r["patient_id"]] = {
                        "name_key": r["name_key"],
                        "phone_key": r["phone_key"],
                        "dob": r["dob"],
                        "sex": r["sex"],
                        "age": age,
                    }
                ids = sorted(facts)
                for i, a in enumerate(ids):
                    for b in ids[i + 1 :]:
                        if (a, b) in seen:
                            continue
                        if kind == "uhid":
                            score, reasons = pm.HARD_SCORE, ["uhid"]
                        else:
                            score, reasons = pm.score_pair(facts[a], facts[b])
                            if pm.band_for(score, reasons) is None:
                                continue
                        if len(to_open) >= cap:
                            truncated = True
                            break
                        to_open.append((a, b, int(score), json.dumps(reasons), now, user["id"]))
                        seen.add((a, b))
                    if truncated:
                        break
                if truncated:
                    break
        if to_open:
            db.executemany(
                """INSERT INTO patient_duplicate_candidates (patient_id, candidate_patient_id,
                       score, reasons_json, status, source, detected_at, detected_by)
                   VALUES (?, ?, ?, ?, 'open', 'sweep', ?, ?)
                   ON CONFLICT(patient_id, candidate_patient_id) DO NOTHING""",
                to_open,
            )
        result = {
            "pairs_opened": len(to_open),
            "groups_checked": groups,
            "groups_too_large": skipped,
            "truncated": truncated,
            "keys_recomputed": synced["recomputed"],
            "orphan_keys_removed": synced["removed"],
            "max_pairs": cap,
        }
        _audit(
            db,
            action="duplicate_sweep_run",
            user=user,
            patient_id=None,
            target_type="patient_index",
            target_id=None,
            summary=f"Duplicate sweep opened {len(to_open)} worklist item(s)",
            metadata=result,
            request_meta=request_meta,
        )
    return result
