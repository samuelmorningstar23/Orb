"""Orb — ABDM / ABHA RAILS: the record of India's national health stack, kept
honestly by a system that is not connected to it.

╔══════════════════════════════════════════════════════════════════════════════╗
║ ORB HAS NO CONNECTION TO ANY ABDM GATEWAY. READ THIS BEFORE ANYTHING ELSE.   ║
╚══════════════════════════════════════════════════════════════════════════════╝

There is no ABDM API client in Orb. No HIP callback URL, no HIU registration, no
consent-manager subscription, no gateway token, no outbox, no retry queue, no
sandbox credential. Orb has not been onboarded to the National Health Authority
in any capacity, and nothing in this module changes that.

What this module is: THE HOSPITAL'S OWN RECORD OF ABDM WORK THAT HAPPENED
SOMEWHERE ELSE. A registration clerk keys an ABHA number off a card. Someone
verifies it on the ABDM portal, in the ABHA app, or against Aadhaar OTP on
whatever software is actually onboarded, and then writes down here that they did
— who, when, by which method. Someone links an episode to the patient's ABHA on
a HIP console; that link is recorded here. A consent manager issues an artefact;
its terms are recorded here.

WHY THAT IS WORTH BUILDING AT ALL. Because the record is the compliance artefact.
An NHA or NABH assessor asks "show me that the ABHA numbers you hold were
verified, by whom, and when" — and a hospital with an `abha_number` column and no
verification trail cannot answer. The rails also mean that the day Orb IS
onboarded, the schema, the vocabulary and the screens already exist and are full
of real data; what gets added is a transport, not a module.

WHY IT IS DANGEROUS TO GET WRONG, and the rule that follows
───────────────────────────────────────────────────────────
The failure mode here is silent and it lands on the patient. A desk that believes
Orb linked the care context does not link the care context, and the patient's
discharge summary never appears in their PHR — a fact nobody discovers for
months, because there is no error and no empty screen. A desk that believes an
ABHA number is verified because Orb rendered it in confident black type attaches
that number to the wrong human being, and a national health id pointed at the
wrong patient is a durable, portable clinical error.

So, without exception:

  * ``NO_GATEWAY_NOTICE`` rides on every payload this module produces and is
    rendered, undismissable, on the panel that drives it.
  * Every public function is ``record_*`` or ``link_*``; there is no ``send_*``,
    ``sync_*``, ``push_*`` or ``fetch_from_abdm``. The names cannot imply a
    transmission because there is none to imply.
  * ``verification_status`` DEFAULTS TO ``'unverified'`` and there is NO
    parameter, anywhere in this module, by which a caller can create an identity
    that is already verified. The only way to 'verified' is
    ``record_verification`` with ``outcome='verified'`` — which itself demands a
    person, a time and a method.
  * ORB DOES NOT CHECK AN ABHA CHECK DIGIT, and says so. See
    ``validate_abha_number``.
  * tests/test_abdm.py asserts the wording is present in this service, in the
    route payloads and in the panel source. Deleting the sentence turns the suite
    red on purpose.

WHAT THIS MODULE MUST NEVER DO
──────────────────────────────
Nothing here may touch, gate or delay a clinical action. No ABHA is required to
admit, to order, to administer, to record vitals or to discharge. A revoked
consent artefact does not hide a chart from the ward that is treating the
patient — ABDM consent governs sharing OUTSIDE the hospital through the national
exchange, which Orb does not do, and conflating it with in-hospital access
control would be a patient-safety defect wearing a compliance badge. This module
reads `patients`/`admissions` for names and encounter lists and writes only to
its own five tables (migration 23).

CONVENTIONS (same as the rest of server/services)
  * Every function is PURE SYNC and opens its own ``db_session()``. Async callers
    go through ``database.run_db``; blocking DB calls inside ``async def`` are
    banned and tests/test_event_loop_discipline.py enforces it.
  * Never catch a stdlib ``sqlite3`` exception class — on the shipping encrypted
    database sqlcipher3 raises its own hierarchy and those handlers are dead
    code. Import ``DBIntegrityError`` from db_backend.
  * No foreign keys on these tables (migration 23): a dangling id yields
    "unknown", never a failed clinical write.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from database import db_session
from db_backend import DBIntegrityError

from services import facility_stamp

logger = logging.getLogger("orb.abdm")

__all__ = [
    "NO_GATEWAY_NOTICE",
    "NO_GATEWAY_SHORT",
    "AbdmError",
    "AbhaFormatError",
    "IdentityError",
    "CareContextError",
    "ConsentError",
    "IllegalConsentTransition",
    "ABHA_NUMBER_DIGITS",
    "VERIFICATION_METHODS",
    "VERIFICATION_OUTCOMES",
    "VERIFICATION_PLACES",
    "IDENTITY_SOURCES",
    "VERIFICATION_STATUSES",
    "HI_TYPES",
    "LINK_METHODS",
    "ENCOUNTER_TYPES",
    "CONSENT_STATUSES",
    "CONSENT_TRANSITIONS",
    "PURPOSE_CODES",
    "REQUESTER_TYPES",
    "RECORDED_PLACES",
    "RECOGNISED_ABHA_SUFFIXES",
    "AbhaNumberCheck",
    "AbhaAddressCheck",
    "validate_abha_number",
    "validate_abha_address",
    "format_abha_number",
    "record_abha",
    "record_abha_on",
    "correct_abha",
    "get_abha",
    "list_abha",
    "record_verification",
    "record_deactivation",
    "link_care_context",
    "unlink_care_context",
    "list_care_contexts",
    "record_consent_request",
    "record_consent_grant",
    "record_consent_denial",
    "revoke_consent",
    "expire_consent",
    "get_consent",
    "list_consents",
    "unlinked_encounter_queue",
    "abdm_summary",
]


# ══════════════════════════════════════════════════════════════════════════════
#  The honesty notice
# ══════════════════════════════════════════════════════════════════════════════

# The exact words. Referenced by the routes, rendered by the panel, asserted by
# the tests. One constant rather than three strings, so a product decision that
# changes them moves all three together.
NO_GATEWAY_NOTICE = (
    "Orb is not connected to the ABDM gateway. "
    "Nothing on this screen is transmitted to ABDM, to a health information "
    "exchange or to a consent manager. "
    "Recording an ABHA, a verification, a care-context link or a consent "
    "artefact here records what a person did elsewhere, on the ABDM portal, in "
    "the ABHA app, or on another system's HIP console."
)

# The short form, for a column header or a button's helper line. It must remain
# unambiguous alone, because it sometimes appears without the long form beside it.
NO_GATEWAY_SHORT = "Recorded in Orb only: nothing was sent to ABDM."


# ══════════════════════════════════════════════════════════════════════════════
#  Errors
# ══════════════════════════════════════════════════════════════════════════════


class AbdmError(ValueError):
    """Base for every refusal in this module."""


class AbhaFormatError(AbdmError):
    """An ABHA number or address that is not structurally an ABHA number or
    address. Its own class because the route maps it to 422 with the specific
    rule that failed, so a clerk sees "14 digits, got 13" rather than "invalid"."""


class IdentityError(AbdmError):
    """Unknown identity, unknown patient, or a change that would orphan a link."""


class CareContextError(AbdmError):
    """A care-context link operation that cannot be recorded truthfully."""


class ConsentError(AbdmError):
    """A consent artefact operation that cannot be recorded truthfully."""


class IllegalConsentTransition(ConsentError):
    """A consent lifecycle move that is not in ``CONSENT_TRANSITIONS``.

    Its own class because ONE of these matters more than all the rest: A REVOKED
    CONSENT CAN NEVER BECOME GRANTED AGAIN. Revocation is the patient exercising
    the only control the national stack actually gives them. If a status dropdown
    or a bulk update could quietly put it back to granted, that control is
    decorative — and the record would then show a consent that the patient
    believes they withdrew. A fresh grant is a NEW artefact with a new id, which
    is exactly what a consent manager issues in real life.
    """


# ══════════════════════════════════════════════════════════════════════════════
#  Vocabulary — mirrors migration 23's CHECK constraints exactly
# ══════════════════════════════════════════════════════════════════════════════

ABHA_NUMBER_DIGITS = 14

VERIFICATION_STATUSES: tuple[str, ...] = (
    "unverified",
    "verified",
    "verification_failed",
    "deactivated",
)

#: How a human verified the ABHA, ELSEWHERE. Every one of these happens outside
#: Orb; the list is about evidence quality, not about a call Orb made.
VERIFICATION_METHODS: tuple[tuple[str, str], ...] = (
    ("aadhaar_otp", "Aadhaar OTP on the ABDM portal"),
    ("mobile_otp", "Mobile OTP on the ABDM portal"),
    ("demographic_match", "Demographic match against the ABHA record"),
    ("abha_card_sighted", "Physical / PDF ABHA card sighted"),
    ("abha_app_qr", "Scanned from the patient's ABHA app"),
    ("other", "Other, described in the note"),
)

VERIFICATION_OUTCOMES: tuple[str, ...] = ("verified", "failed", "inconclusive")

#: WHERE the human did it. There is deliberately no value meaning "Orb called the
#: gateway", because Orb cannot, and a vocabulary that offered one would be the
#: first place the lie crept in.
VERIFICATION_PLACES: tuple[tuple[str, str], ...] = (
    ("abdm_portal", "ABDM / NHA portal"),
    ("abha_app", "Patient's ABHA app"),
    ("partner_software", "Another system that is ABDM-onboarded"),
    ("in_person", "In person at the counter"),
    ("outside_orb", "Elsewhere, described in the note"),
)

IDENTITY_SOURCES: tuple[tuple[str, str], ...] = (
    ("counter_keyed", "Keyed at the registration counter"),
    ("abha_card", "Copied from the patient's ABHA card"),
    ("abha_app_qr", "Scanned from the patient's ABHA app"),
    ("patient_stated", "Stated by the patient from memory"),
    ("other", "Other, described in the note"),
)

#: ABDM's health-information types. These ARE the national vocabulary (unlike the
#: denial codes in the payer module, which are Orb's own), so the list is fixed
#: and a value outside it is refused rather than stored verbatim.
HI_TYPES: tuple[str, ...] = (
    "OPConsultation",
    "DischargeSummary",
    "Prescription",
    "DiagnosticReport",
    "ImmunizationRecord",
    "HealthDocumentRecord",
    "WellnessRecord",
)

LINK_METHODS: tuple[tuple[str, str], ...] = (
    ("abdm_portal", "ABDM / NHA portal"),
    ("hip_console", "HIP console of an onboarded system"),
    ("partner_software", "Another system that is ABDM-onboarded"),
    ("other", "Other, described in the note"),
)

ENCOUNTER_TYPES: tuple[str, ...] = ("admission", "opd_visit", "other")

#: ABDM purpose-of-use codes. 'OTHER' exists so an artefact whose purpose Orb
#: does not recognise is recorded verbatim rather than forced into a near-miss.
PURPOSE_CODES: tuple[tuple[str, str], ...] = (
    ("CAREMGT", "Care management"),
    ("BTG", "Break the glass: emergency access"),
    ("PUBHLTH", "Public health"),
    ("HPAYMT", "Healthcare payment"),
    ("DSRCH", "Disease-specific research"),
    ("PATRQT", "Self-requested (patient)"),
    ("SLFRPT", "Self-reported data"),
    ("OTHER", "Other, recorded in words"),
)

REQUESTER_TYPES: tuple[str, ...] = ("hiu", "hip", "patient", "other")

RECORDED_PLACES: tuple[str, ...] = (
    "consent_manager",
    "abdm_portal",
    "partner_software",
    "in_person",
    "outside_orb",
)

CONSENT_STATUSES: tuple[str, ...] = (
    "requested",
    "granted",
    "denied",
    "expired",
    "revoked",
)

# ── THE CONSENT LIFECYCLE ────────────────────────────────────────────────────
#
#   requested ──► granted ──► revoked        (patient withdrew)
#       │            └──────► expired        (the artefact's own clock ran out)
#       ├──────────► denied                  (patient said no)
#       └──────────► expired                 (the request itself lapsed)
#
# THE TRANSITIONS THAT ARE DELIBERATELY MISSING: revoked ──► granted, and
# denied ──► granted.
#
# A withdrawn consent cannot be un-withdrawn, and a refused one cannot be
# talked into a yes. Both need a NEW artefact from the consent manager, with its
# own id and its own date, because that is what actually happens in the national
# stack — and because the alternative is a row whose current status contradicts
# what the patient did, with nothing on screen to show it ever changed.
CONSENT_TRANSITIONS: dict[str, frozenset[str]] = {
    "requested": frozenset({"granted", "denied", "expired"}),
    "granted": frozenset({"revoked", "expired"}),
    "denied": frozenset(),
    "expired": frozenset(),
    "revoked": frozenset(),
}

#: Suffixes Orb recognises today: `@abdm` is the production ABHA address suffix
#: and `@sbx` is the NHA sandbox's. This list is INFORMATIONAL — an address with
#: an unrecognised suffix is accepted and flagged, never refused, because NHA can
#: add a suffix any week and Orb refusing a real patient's address over a stale
#: hard-coded list is the worse failure.
RECOGNISED_ABHA_SUFFIXES: frozenset[str] = frozenset({"abdm", "sbx"})

#: How long an unlinked encounter sits in the queue before it is called stale. A
#: display hint, nothing more — it does not act on anything.
STALE_AFTER_DAYS = 7


# ══════════════════════════════════════════════════════════════════════════════
#  ABHA format validation — PURE, and deliberately modest about what it proves
# ══════════════════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class AbhaNumberCheck:
    """The result of looking at a candidate ABHA number.

    `valid` means STRUCTURALLY PLAUSIBLE — fourteen digits that are not obviously
    filler. It does not mean the number exists, belongs to this patient, or is
    active. Only ABDM can say that, and only through a human, recorded as a
    verification.
    """

    valid: bool
    normalised: str | None
    formatted: str | None
    reason: str | None

    def as_dict(self) -> dict:
        return {
            "valid": self.valid,
            "normalised": self.normalised,
            "formatted": self.formatted,
            "reason": self.reason,
            # Said out loud in the payload so no client can render a green tick
            # and mean more by it than this function does.
            "checked_against_abdm": False,
            "note": (
                "Structural check only. Orb cannot confirm that this number "
                "exists or belongs to this patient."
            ),
        }


@dataclass(frozen=True)
class AbhaAddressCheck:
    valid: bool
    normalised: str | None
    username: str | None
    suffix: str | None
    suffix_recognised: bool
    reason: str | None

    def as_dict(self) -> dict:
        return {
            "valid": self.valid,
            "normalised": self.normalised,
            "username": self.username,
            "suffix": self.suffix,
            "suffix_recognised": self.suffix_recognised,
            "reason": self.reason,
            "checked_against_abdm": False,
            "note": ("Structural check only. Orb cannot confirm that this ABHA address exists."),
        }


_DIGITS_ONLY = re.compile(r"^\d+$")
# Separators a human might type inside an ABHA number, and nothing else.
_NUMBER_SEPARATORS = re.compile(r"[\s\-‐-―]")
_ADDRESS_USERNAME = re.compile(r"^[a-z0-9]([a-z0-9._-]*[a-z0-9])?$")
_ADDRESS_SUFFIX = re.compile(r"^[a-z][a-z0-9]{1,15}$")
_CONSECUTIVE_SEPARATORS = re.compile(r"[._-]{2,}")

# Orb's local floor and ceiling for the username part of an ABHA address. These
# are ORB'S numbers, not NHA's: they are wide enough that no real address should
# hit them and narrow enough to catch a clerk who pasted half a sentence. They
# can be widened without a migration.
_USERNAME_MIN = 3
_USERNAME_MAX = 64


def format_abha_number(digits: str) -> str:
    """14 bare digits → the XX-XXXX-XXXX-XXXX form printed on an ABHA card.

    Display only. The database stores the bare digits, so a search for a number
    typed with or without hyphens finds the same row.
    """
    d = str(digits)
    if len(d) != ABHA_NUMBER_DIGITS or not _DIGITS_ONLY.match(d):
        return d
    return f"{d[0:2]}-{d[2:6]}-{d[6:10]}-{d[10:14]}"


def validate_abha_number(raw: str | None) -> AbhaNumberCheck:
    """Structural validation of an ABHA number. Never a network call.

    ACCEPTS the forms a human actually types: ``12-3456-7890-1234``,
    ``12 3456 7890 1234``, ``12345678901234`` — hyphens, spaces and the unicode
    dashes a copy-paste from a PDF drags in are stripped before checking.

    REFUSES: anything that is not exactly fourteen digits after stripping, any
    non-digit character, and repdigits (``00000000000000``,
    ``11111111111111``) — which are never issued and are what gets typed when a
    field is mandatory and nobody has the card.

    ═══ WHAT THIS FUNCTION DELIBERATELY DOES NOT DO ═══════════════════════════
    IT DOES NOT CHECK A CHECK DIGIT. Aadhaar's Verhoeff check digit is public and
    well documented; the ABHA number's internal structure is not, and Orb has no
    NHA specification in hand to implement against. Guessing would produce one of
    two failures, both worse than not checking:

        · a wrong rule that REFUSES real patients' real ABHA numbers at a
          registration counter, which is an outage with a queue behind it;
        · a rule that appears to pass and manufactures FALSE CONFIDENCE — a
          number rendered as "checks out" that nobody has actually confirmed.

    The honest position is the one the whole module is built on: nothing offline
    can validate an ABHA number. Only ABDM can, through a person, recorded as a
    verification. So this function proves the number is fourteen digits, and the
    UI says exactly that and no more.
    """
    if raw is None:
        return AbhaNumberCheck(False, None, None, "No ABHA number was given.")
    text = str(raw).strip()
    if not text:
        return AbhaNumberCheck(False, None, None, "No ABHA number was given.")

    stripped = _NUMBER_SEPARATORS.sub("", text)
    if not _DIGITS_ONLY.match(stripped):
        bad = sorted({c for c in stripped if not c.isdigit()})
        return AbhaNumberCheck(
            False,
            None,
            None,
            "An ABHA number is digits only. Unexpected character(s): "
            + ", ".join(repr(c) for c in bad)
            + ".",
        )
    if len(stripped) != ABHA_NUMBER_DIGITS:
        return AbhaNumberCheck(
            False,
            None,
            None,
            f"An ABHA number is {ABHA_NUMBER_DIGITS} digits; this has {len(stripped)}.",
        )
    if len(set(stripped)) == 1:
        return AbhaNumberCheck(
            False,
            None,
            None,
            "That is the same digit fourteen times, which is not an issued ABHA "
            "number. Leave the field empty rather than filling it.",
        )
    return AbhaNumberCheck(True, stripped, format_abha_number(stripped), None)


def validate_abha_address(raw: str | None) -> AbhaAddressCheck:
    """Structural validation of an ABHA (PHR) address — ``username@suffix``.

    Normalises to lowercase, because ABHA addresses are case-insensitive and
    storing ``Hina.Patel@abdm`` and ``hina.patel@abdm`` as two identities is how a
    patient acquires a duplicate.

    ORB'S RULES, AND THEY ARE ORB'S. NHA's authoritative rules for what makes a
    valid ABHA address are not published in a form Orb can pin a version to, so
    what is enforced here is the structure that is not in dispute — exactly one
    ``@``, a non-empty username of ordinary address characters that starts and
    ends alphanumerically with no doubled separators, and a plausible suffix. The
    suffix itself is NOT restricted to the recognised list: an unfamiliar suffix
    is accepted and flagged (`suffix_recognised=False`), because NHA can add one
    at any time and refusing a real patient over a stale hard-coded list is the
    worse failure. Widening these rules needs no migration.
    """
    if raw is None:
        return AbhaAddressCheck(False, None, None, None, False, "No ABHA address was given.")
    text = str(raw).strip().lower()
    if not text:
        return AbhaAddressCheck(False, None, None, None, False, "No ABHA address was given.")
    if any(ch.isspace() for ch in text):
        return AbhaAddressCheck(
            False,
            None,
            None,
            None,
            False,
            "An ABHA address contains no spaces.",
        )
    if text.count("@") != 1:
        return AbhaAddressCheck(
            False,
            None,
            None,
            None,
            False,
            "An ABHA address looks like username@abdm. It needs exactly one '@'.",
        )

    username, suffix = text.split("@", 1)
    if not username:
        return AbhaAddressCheck(
            False,
            None,
            None,
            suffix or None,
            False,
            "There is nothing before the '@'.",
        )
    if not suffix:
        return AbhaAddressCheck(
            False,
            None,
            username,
            None,
            False,
            "There is nothing after the '@'.",
        )
    if len(username) < _USERNAME_MIN:
        return AbhaAddressCheck(
            False,
            None,
            username,
            suffix,
            suffix in RECOGNISED_ABHA_SUFFIXES,
            f"The part before the '@' is shorter than {_USERNAME_MIN} characters.",
        )
    if len(username) > _USERNAME_MAX:
        return AbhaAddressCheck(
            False,
            None,
            username,
            suffix,
            suffix in RECOGNISED_ABHA_SUFFIXES,
            f"The part before the '@' is longer than {_USERNAME_MAX} characters.",
        )
    if not _ADDRESS_USERNAME.match(username):
        return AbhaAddressCheck(
            False,
            None,
            username,
            suffix,
            suffix in RECOGNISED_ABHA_SUFFIXES,
            "The part before the '@' may use letters, digits, dot, hyphen and "
            "underscore, and must start and end with a letter or digit.",
        )
    if _CONSECUTIVE_SEPARATORS.search(username):
        return AbhaAddressCheck(
            False,
            None,
            username,
            suffix,
            suffix in RECOGNISED_ABHA_SUFFIXES,
            "The part before the '@' has two separators in a row.",
        )
    if not _ADDRESS_SUFFIX.match(suffix):
        return AbhaAddressCheck(
            False,
            None,
            username,
            suffix,
            False,
            "The part after the '@' does not look like an ABHA suffix (for example 'abdm').",
        )
    return AbhaAddressCheck(
        True,
        f"{username}@{suffix}",
        username,
        suffix,
        suffix in RECOGNISED_ABHA_SUFFIXES,
        None,
    )


# ══════════════════════════════════════════════════════════════════════════════
#  Small helpers
# ══════════════════════════════════════════════════════════════════════════════


def _utc_now_str() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")


def _row(r) -> dict:
    return {k: r[k] for k in r.keys()}


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _require_reason(value: str | None, *, what: str, error: type[AbdmError]) -> str:
    """A reason that is blank, or is punctuation, is not a reason.

    Used by every destructive step in this module (unlink, revoke, deactivate).
    The whole value of these records is that a later reader can see WHY, and a
    mandatory field that accepts "." trains people to type ".".
    """
    text = _clean(value)
    if not text or len(text) < 3:
        raise error(
            f"{what} needs a reason of at least 3 characters. It is the only "
            "thing that explains this row to whoever reads it next."
        )
    return text


def _parse_ts(value: Any) -> datetime | None:
    """Parse a stored/supplied timestamp into an aware UTC datetime, or None.

    Tolerant on input (dates, 'T' separators, trailing 'Z', fractional seconds)
    because these values arrive from humans transcribing a portal screen, and
    strict about the OUTPUT being timezone-aware so comparisons cannot silently
    mix naive and aware datetimes.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=UTC)
    text = str(value).strip()
    if not text:
        return None
    text = text.replace("T", " ").replace("Z", "").strip()
    if "." in text:
        text = text.split(".", 1)[0]
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    return None


def _norm_ts(value: str | None, *, field: str) -> str | None:
    """Normalise a caller-supplied timestamp for storage, refusing gibberish.

    `field` is printed in the refusal, so callers pass the person's word for
    the date ("consent expiry"), never the column name.

    A date that cannot be parsed is REFUSED rather than stored as typed: a
    consent expiry of "next tuesday" in a TIMESTAMP column reads as NULL to every
    query that matters, and NULL here means "no expiry recorded", which is a
    different and dangerous claim.
    """
    if value is None:
        return None
    if not _clean(str(value)):
        return None
    parsed = _parse_ts(value)
    if parsed is None:
        raise AbdmError(
            f"The {field} is not a date Orb can read. Use YYYY-MM-DD or YYYY-MM-DD HH:MM."
        )
    return parsed.strftime("%Y-%m-%d %H:%M:%S")


def _norm_date(value: str | None, *, field: str) -> str | None:
    ts = _norm_ts(value, field=field)
    return ts[:10] if ts else None


def _age_days(anchor: datetime | None, *, now: datetime | None = None) -> int | None:
    if anchor is None:
        return None
    now = now or datetime.now(UTC)
    return max(0, int((now - anchor).total_seconds() // 86400))


def _write_event(
    db,
    *,
    entity_type: str,
    entity_id: int,
    event_type: str,
    patient_id: str | None = None,
    from_status: str | None = None,
    to_status: str | None = None,
    detail: str | None = None,
    reason: str | None = None,
    actor_id: str | None = None,
    occurred_at: str | None = None,
    metadata: dict | None = None,
) -> None:
    """Append one immutable line to this module's own history.

    `occurred_at` is when the HUMAN did the thing (possibly on the ABDM portal
    yesterday); `recorded_at` defaults to now and is when Orb heard about it.
    Keeping both is what makes a link date defensible when a patient asks why
    their discharge summary took three weeks to appear.
    """
    db.execute(
        """INSERT INTO abdm_events
             (entity_type, entity_id, patient_id, event_type, from_status,
              to_status, detail, reason, actor_id, occurred_at, recorded_at,
              metadata_json)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            entity_type,
            int(entity_id),
            patient_id,
            event_type,
            from_status,
            to_status,
            _clean(detail),
            _clean(reason),
            actor_id,
            occurred_at or _utc_now_str(),
            _utc_now_str(),
            json.dumps(metadata) if metadata else None,
        ),
    )


def _require_patient(db, patient_id: str) -> dict:
    pid = _clean(patient_id)
    if not pid:
        raise IdentityError("a patient id is required")
    row = db.execute("SELECT id, name, mrn, ward FROM patients WHERE id = ?", (pid,)).fetchone()
    if row is None:
        raise IdentityError(f"unknown patient {pid}")
    return _row(row)


def _clash_patient_line(db, patient_id: str, department: str | None) -> str | None:
    """'Kavitha Menon, MRN-CC002, CCU' for the patient a duplicate ABHA already
    belongs to, or None when that patient is outside the caller's scope (or has
    no name on record), so the refusal never discloses another ward's patient."""
    from permissions import patient_in_scope

    if not patient_in_scope(db, patient_id, department):
        return None
    row = db.execute("SELECT name, mrn, ward FROM patients WHERE id = ?", (patient_id,)).fetchone()
    if row is None or not _clean(row["name"]):
        return None
    return ", ".join(p for p in (_clean(row["name"]), _clean(row["mrn"]), _clean(row["ward"])) if p)


def _load_identity(db, identity_id: int):
    row = db.execute("SELECT * FROM abha_identities WHERE id = ?", (int(identity_id),)).fetchone()
    if row is None:
        raise IdentityError(f"ABHA record {identity_id} not found")
    return row


VERIFICATION_LABEL: dict[str, str] = {
    # The words a screen shows. 'unverified' reads as a plain statement of fact,
    # never as a warning ornament and never as an empty cell — an empty cell is
    # read as "fine".
    "unverified": "Not verified",
    "verified": "Verified",
    "verification_failed": "Verification failed",
    "deactivated": "Reported deactivated",
}


def _decorate_identity(row: dict, *, verifications: list[dict] | None = None) -> dict:
    """Add the derived fields every screen needs, so no client invents them."""
    out = dict(row)
    number = out.get("abha_number")
    out["abha_number_formatted"] = format_abha_number(number) if number else None
    status = out.get("verification_status") or "unverified"
    out["verification_status"] = status
    out["verification_label"] = VERIFICATION_LABEL.get(status, status)
    # ONE boolean, computed in ONE place, from the stored status only. A screen
    # that derived "verified" from the presence of a number would show every
    # keyed-in number as trustworthy.
    out["is_verified"] = status == "verified"
    out["is_primary"] = bool(out.get("is_primary"))
    out["transmitted_to_abdm"] = False
    if verifications is not None:
        out["verifications"] = verifications
    return out


# ══════════════════════════════════════════════════════════════════════════════
#  ABHA identities
# ══════════════════════════════════════════════════════════════════════════════


def record_abha(
    *,
    patient_id: str,
    abha_number: str | None = None,
    abha_address: str | None = None,
    source: str = "counter_keyed",
    recorded_by: str | None = None,
    notes: str | None = None,
    is_primary: bool = True,
    department: str | None = None,
) -> dict:
    """Record an ABHA a patient presented. IT IS UNVERIFIED. ALWAYS.

    THERE IS NO `verification_status` PARAMETER ON THIS FUNCTION, and adding one
    would defeat the module. A number keyed at a counter has been confirmed by
    nobody: not by ABDM, which Orb cannot ask, and not by the clerk, who read it
    off a photograph on a phone. It becomes verified only through
    ``record_verification``, which demands a person, a time and a method.

    At least one of number/address is required — an identity row that holds
    neither is a row that says a patient has an ABHA without saying which.

    `department` is the CALLER'S scope (permissions.department_scope), used only
    to decide whether a duplicate refusal may name the other patient. None is
    the hospital-wide convention, as in ``list_abha``.

    Opens its own session; ``record_abha_on`` is the same write on a caller's
    connection.
    """
    with db_session() as db:
        return record_abha_on(
            db,
            patient_id=patient_id,
            abha_number=abha_number,
            abha_address=abha_address,
            source=source,
            recorded_by=recorded_by,
            notes=notes,
            is_primary=is_primary,
            department=department,
        )


def record_abha_on(
    db,
    *,
    patient_id: str,
    abha_number: str | None = None,
    abha_address: str | None = None,
    source: str = "counter_keyed",
    recorded_by: str | None = None,
    notes: str | None = None,
    is_primary: bool = True,
    department: str | None = None,
) -> dict:
    """``record_abha`` on the CALLER'S connection, inside the caller's
    transaction. It never opens, commits or rolls back a session, so the
    registration desk (opd-4) records the ABHA a new patient presented in the
    same transaction as the patient itself: either both exist or neither does.

    Identical rules to ``record_abha``, and the same promise: IT IS UNVERIFIED.
    ALWAYS. There is no parameter here either by which a caller could record a
    verified identity; the only way to 'verified' is ``record_verification``.
    """
    number_check = validate_abha_number(abha_number) if _clean(abha_number) else None
    address_check = validate_abha_address(abha_address) if _clean(abha_address) else None

    if number_check is None and address_check is None:
        raise AbhaFormatError(
            "Record an ABHA number, an ABHA address, or both, an ABHA record "
            "with neither says nothing."
        )
    if number_check is not None and not number_check.valid:
        raise AbhaFormatError(number_check.reason or "That is not an ABHA number.")
    if address_check is not None and not address_check.valid:
        raise AbhaFormatError(address_check.reason or "That is not an ABHA address.")

    valid_sources = {code for code, _ in IDENTITY_SOURCES}
    if source not in valid_sources:
        raise AbdmError(f"unknown source {source!r}; expected one of {sorted(valid_sources)}")

    number = number_check.normalised if number_check else None
    address = address_check.normalised if address_check else None

    _require_patient(db, patient_id)

    # A live ABHA already attached to a DIFFERENT patient is a merge error and
    # a PHI hazard: two people would share a national health id inside one
    # hospital. Refused with the conflict named, so the desk resolves it
    # rather than the database rejecting it with an index name.
    for column, value, label in (
        ("abha_number", number, "ABHA number"),
        ("abha_address", address, "ABHA address"),
    ):
        if not value:
            continue
        clash = db.execute(
            f"SELECT id, patient_id FROM abha_identities WHERE {column} = ? AND status = 'active'",
            (value,),
        ).fetchone()
        if clash is not None:
            if clash["patient_id"] == patient_id:
                raise IdentityError(
                    f"That {label} is already recorded for this patient (record #{clash['id']})."
                )
            # Named, so the desk can find the other chart and resolve it.
            # Only inside the caller's own scope: naming a patient on a ward
            # the caller cannot see would be a disclosure, so out of scope
            # the refusal stays anonymous.
            other = _clash_patient_line(db, clash["patient_id"], department)
            where = f"{other}; record #{clash['id']}" if other else f"record #{clash['id']}"
            # The desk has no correct or withdraw control yet, so the
            # refusal names what a person can do today, not an edit the
            # screen cannot make.
            raise IdentityError(
                f"That {label} is already recorded against a different "
                f"patient ({where}). One ABHA belongs to one person. Check "
                "the number against the patient's ABHA card. If the other "
                "record is the mistake, this screen cannot correct or "
                "withdraw it yet: contact Orb support."
            )

    if is_primary:
        db.execute(
            "UPDATE abha_identities SET is_primary = 0, updated_at = ? "
            "WHERE patient_id = ? AND status = 'active'",
            (_utc_now_str(), patient_id),
        )

    try:
        cur = db.execute(
            """INSERT INTO abha_identities
                 (patient_id, abha_number, abha_address, verification_status,
                  is_primary, status, source, notes, recorded_by,
                  created_at, updated_at)
               VALUES (?,?,?,'unverified',?,'active',?,?,?,?,?)""",
            (
                patient_id,
                number,
                address,
                1 if is_primary else 0,
                source,
                _clean(notes),
                recorded_by,
                _utc_now_str(),
                _utc_now_str(),
            ),
        )
    except DBIntegrityError as exc:
        # The partial-unique indexes are the backstop for a race the SELECT
        # above cannot close. Never catch stdlib sqlite3 here: on the
        # shipping encrypted DB sqlcipher3 raises its own hierarchy.
        raise IdentityError(
            "That ABHA is already recorded against an active patient record."
        ) from exc

    identity_id = int(cur.lastrowid)
    _write_event(
        db,
        entity_type="abha_identity",
        entity_id=identity_id,
        patient_id=patient_id,
        event_type="recorded",
        to_status="unverified",
        detail=f"ABHA recorded ({dict(IDENTITY_SOURCES).get(source, source)})",
        actor_id=recorded_by,
    )
    row = _row(_load_identity(db, identity_id))

    return _decorate_identity(row, verifications=[])


def correct_abha(
    *,
    identity_id: int,
    abha_number: str | None = None,
    abha_address: str | None = None,
    reason: str,
    actor_id: str | None = None,
) -> dict:
    """Correct a mis-keyed ABHA. THE VERIFICATION IS RESET.

    Two rules, and both of them exist because the alternative is a lie on a
    screen:

    1. CHANGING THE VALUE RESETS `verification_status` TO 'unverified'. What was
       verified was the OLD number. Carrying the tick across to a new number
       would produce the exact thing this module exists to prevent: a national
       health id that reads verified and was confirmed by nobody. The reset is
       recorded as its own event so the history shows it.

    2. IT IS REFUSED WHILE LIVE CARE-CONTEXT LINKS EXIST. Episodes were linked to
       the ABHA as it was; silently repointing them at a different number is an
       unlink and a re-link that nobody performed and nobody can see. Unlink
       first — with a reason — then correct, then link again.
    """
    reason = _require_reason(reason, what="Correcting an ABHA", error=IdentityError)

    number_check = validate_abha_number(abha_number) if _clean(abha_number) else None
    address_check = validate_abha_address(abha_address) if _clean(abha_address) else None
    if number_check is None and address_check is None:
        raise AbhaFormatError("Give the corrected ABHA number or address.")
    if number_check is not None and not number_check.valid:
        raise AbhaFormatError(number_check.reason or "That is not an ABHA number.")
    if address_check is not None and not address_check.valid:
        raise AbhaFormatError(address_check.reason or "That is not an ABHA address.")

    with db_session() as db:
        row = _load_identity(db, identity_id)
        if row["status"] != "active":
            raise IdentityError(
                f"ABHA record {identity_id} is {row['status']} and is kept as "
                "history; record a new one instead of editing it."
            )

        new_number = number_check.normalised if number_check else row["abha_number"]
        new_address = address_check.normalised if address_check else row["abha_address"]
        changed = (new_number != row["abha_number"]) or (new_address != row["abha_address"])
        if not changed:
            raise IdentityError("That is the value already on record, nothing to correct.")

        live_links = db.execute(
            "SELECT COUNT(*) AS n FROM abdm_care_contexts "
            "WHERE abha_identity_id = ? AND status = 'linked'",
            (int(identity_id),),
        ).fetchone()["n"]
        if live_links:
            raise IdentityError(
                f"{live_links} care context(s) are linked to this ABHA. Unlink "
                "them first, with a reason, correcting the number underneath a "
                "live link would move those episodes to a different ABHA with "
                "nothing on record to show it."
            )

        was_status = row["verification_status"]
        db.execute(
            """UPDATE abha_identities
                  SET abha_number = ?, abha_address = ?,
                      verification_status = 'unverified',
                      verified_at = NULL, verified_by = NULL,
                      verification_method = NULL, verification_reference = NULL,
                      updated_at = ?
                WHERE id = ?""",
            (new_number, new_address, _utc_now_str(), int(identity_id)),
        )
        _write_event(
            db,
            entity_type="abha_identity",
            entity_id=int(identity_id),
            patient_id=row["patient_id"],
            event_type="corrected",
            from_status=was_status,
            to_status="unverified",
            detail="ABHA value corrected; verification reset to unverified",
            reason=reason,
            actor_id=actor_id,
        )
        fresh = _row(_load_identity(db, identity_id))
        verifications = [
            _row(r)
            for r in db.execute(
                "SELECT * FROM abha_verifications WHERE abha_identity_id = ? ORDER BY id DESC",
                (int(identity_id),),
            ).fetchall()
        ]
    return _decorate_identity(fresh, verifications=verifications)


def record_verification(
    *,
    identity_id: int,
    method: str,
    outcome: str,
    performed_by: str,
    performed_at: str | None = None,
    performed_where: str = "outside_orb",
    reference: str | None = None,
    failure_reason: str | None = None,
    notes: str | None = None,
    recorded_by: str | None = None,
) -> dict:
    """Record a verification a human performed ELSEWHERE. Orb verified nothing.

    THIS IS THE ONLY ROUTE TO 'verified'. Nothing else in this module writes that
    status, which is what makes "unverified" mean something.

    `outcome='failed'` is recorded, kept, and moves the identity to
    'verification_failed' — a failed attempt is evidence, and a module that only
    stored successes would show a desk that never fails to verify anything.
    """
    valid_methods = {code for code, _ in VERIFICATION_METHODS}
    if method not in valid_methods:
        raise AbdmError(f"unknown method {method!r}; expected one of {sorted(valid_methods)}")
    if outcome not in VERIFICATION_OUTCOMES:
        raise AbdmError(
            f"unknown outcome {outcome!r}; expected one of {sorted(VERIFICATION_OUTCOMES)}"
        )
    valid_places = {code for code, _ in VERIFICATION_PLACES}
    if performed_where not in valid_places:
        raise AbdmError(
            f"unknown place {performed_where!r}; expected one of {sorted(valid_places)}"
        )
    who = _clean(performed_by)
    if not who:
        raise AbdmError(
            "A verification needs the person who performed it. 'Someone verified "
            "this' is not a verification record."
        )
    if outcome == "failed" and not _clean(failure_reason):
        raise AbdmError("A failed verification needs the reason it failed.")

    when = _norm_ts(performed_at, field="verification time") or _utc_now_str()

    with db_session() as db:
        row = _load_identity(db, identity_id)
        if row["status"] != "active":
            raise IdentityError(
                f"ABHA record {identity_id} is {row['status']}; verifying a "
                "record that is no longer in use would be recording work on a "
                "value nobody uses."
            )

        cur = db.execute(
            """INSERT INTO abha_verifications
                 (abha_identity_id, patient_id, method, outcome, performed_where,
                  performed_by, performed_at, recorded_at, recorded_by, reference,
                  failure_reason, notes)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                int(identity_id),
                row["patient_id"],
                method,
                outcome,
                performed_where,
                who,
                when,
                _utc_now_str(),
                recorded_by or who,
                _clean(reference),
                _clean(failure_reason),
                _clean(notes),
            ),
        )
        verification_id = int(cur.lastrowid)

        # 'inconclusive' deliberately leaves the status ALONE. An attempt that
        # neither confirmed nor refuted anything must not downgrade a previously
        # verified ABHA, and must not upgrade an unverified one.
        if outcome == "verified":
            new_status = "verified"
            db.execute(
                """UPDATE abha_identities
                      SET verification_status = 'verified', verified_at = ?,
                          verified_by = ?, verification_method = ?,
                          verification_reference = ?, updated_at = ?
                    WHERE id = ?""",
                (when, who, method, _clean(reference), _utc_now_str(), int(identity_id)),
            )
        elif outcome == "failed":
            new_status = "verification_failed"
            db.execute(
                """UPDATE abha_identities
                      SET verification_status = 'verification_failed',
                          verified_at = NULL, verified_by = NULL,
                          verification_method = NULL, verification_reference = NULL,
                          updated_at = ?
                    WHERE id = ?""",
                (_utc_now_str(), int(identity_id)),
            )
        else:
            new_status = row["verification_status"]

        _write_event(
            db,
            entity_type="abha_identity",
            entity_id=int(identity_id),
            patient_id=row["patient_id"],
            event_type="verification_recorded",
            from_status=row["verification_status"],
            to_status=new_status,
            detail=(
                f"{dict(VERIFICATION_METHODS).get(method, method)}: {outcome}, "
                f"by {who}, at {dict(VERIFICATION_PLACES).get(performed_where, performed_where)}"
            ),
            reason=_clean(failure_reason),
            actor_id=recorded_by or who,
            occurred_at=when,
            metadata={"verification_id": verification_id, "outcome": outcome},
        )

        fresh = _row(_load_identity(db, identity_id))
        verifications = [
            _row(r)
            for r in db.execute(
                "SELECT * FROM abha_verifications WHERE abha_identity_id = ? ORDER BY id DESC",
                (int(identity_id),),
            ).fetchall()
        ]
    return _decorate_identity(fresh, verifications=verifications)


def record_deactivation(*, identity_id: int, reason: str, actor_id: str | None = None) -> dict:
    """Record that the desk was TOLD (elsewhere) this ABHA is no longer valid.

    Orb cannot discover this — there is no gateway to hear it from — so the row
    says a human was told, and by leaving the value in place it stays searchable:
    the next clerk who types that number finds the deactivated record and its
    reason instead of creating a second one.
    """
    reason = _require_reason(reason, what="Marking an ABHA deactivated", error=IdentityError)
    with db_session() as db:
        row = _load_identity(db, identity_id)
        db.execute(
            "UPDATE abha_identities SET verification_status = 'deactivated', "
            "updated_at = ? WHERE id = ?",
            (_utc_now_str(), int(identity_id)),
        )
        _write_event(
            db,
            entity_type="abha_identity",
            entity_id=int(identity_id),
            patient_id=row["patient_id"],
            event_type="deactivation_recorded",
            from_status=row["verification_status"],
            to_status="deactivated",
            detail="Reported deactivated (heard elsewhere; Orb cannot check)",
            reason=reason,
            actor_id=actor_id,
        )
        fresh = _row(_load_identity(db, identity_id))
    return _decorate_identity(fresh)


def get_abha(identity_id: int, *, include_events: bool = True) -> dict:
    with db_session() as db:
        row = _row(_load_identity(db, identity_id))
        verifications = [
            _row(r)
            for r in db.execute(
                "SELECT * FROM abha_verifications WHERE abha_identity_id = ? ORDER BY id DESC",
                (int(identity_id),),
            ).fetchall()
        ]
        contexts = [
            _row(r)
            for r in db.execute(
                "SELECT * FROM abdm_care_contexts WHERE abha_identity_id = ? ORDER BY id DESC",
                (int(identity_id),),
            ).fetchall()
        ]
        events = []
        if include_events:
            events = [
                _row(r)
                for r in db.execute(
                    "SELECT * FROM abdm_events WHERE entity_type = 'abha_identity' "
                    "AND entity_id = ? ORDER BY id DESC",
                    (int(identity_id),),
                ).fetchall()
            ]
    out = _decorate_identity(row, verifications=verifications)
    out["care_contexts"] = [_decorate_care_context(c) for c in contexts]
    out["events"] = events
    return out


def list_abha(
    *,
    patient_id: str | None = None,
    verification_status: str | None = None,
    department: str | None = None,
    active_only: bool = True,
    limit: int = 200,
) -> list[dict]:
    """ABHA records, newest first.

    `department` is the CALLER'S scope, resolved from the authenticated user by
    the route — never from the request. None means an account with no department
    (the hospital-wide governance convention in permissions.department_scope),
    not "show me everything".
    """
    sql = [
        "SELECT i.*, p.name AS patient_name, p.mrn AS patient_mrn, p.ward AS patient_ward",
        "  FROM abha_identities i",
        "  LEFT JOIN patients p ON p.id = i.patient_id",
        " WHERE 1=1",
    ]
    params: list[Any] = []
    if patient_id:
        sql.append(" AND i.patient_id = ?")
        params.append(patient_id)
    if verification_status:
        sql.append(" AND i.verification_status = ?")
        params.append(verification_status)
    if active_only:
        sql.append(" AND i.status = 'active'")
    if department is not None:
        from permissions import scope_patient_ids_sql

        scope_sql, scope_params = scope_patient_ids_sql(department)
        sql.append(f" AND i.patient_id IN ({scope_sql})")
        params.extend(scope_params)
    sql.append(" ORDER BY i.id DESC LIMIT ?")
    params.append(max(1, min(int(limit), 1000)))

    with db_session() as db:
        rows = [_row(r) for r in db.execute("\n".join(sql), params).fetchall()]
    return [_decorate_identity(r) for r in rows]


# ══════════════════════════════════════════════════════════════════════════════
#  Care-context links
# ══════════════════════════════════════════════════════════════════════════════


def _decorate_care_context(row: dict) -> dict:
    out = dict(row)
    out["is_linked"] = out.get("status") == "linked"
    out["transmitted_to_abdm"] = False
    method = out.get("link_method")
    out["link_method_label"] = dict(LINK_METHODS).get((method or ""), method)
    return out


def link_care_context(
    *,
    identity_id: int,
    care_context_reference: str,
    hi_type: str = "OPConsultation",
    display: str | None = None,
    encounter_type: str = "admission",
    encounter_ref: str | None = None,
    encounter_date: str | None = None,
    linked_by: str,
    linked_at: str | None = None,
    link_method: str = "abdm_portal",
    link_reference: str | None = None,
    notes: str | None = None,
    recorded_by: str | None = None,
) -> dict:
    """RECORD that a human linked this episode to this ABHA, somewhere else.

    Named `link_care_context` and not `send_link` for the reason the whole module
    exists: it records a noun that already happened. Orb performs no link.

    THE ABHA MUST BE VERIFIED FIRST. This is the one place this module refuses
    work on a compliance ground, and it is not bureaucratic: linking an episode
    to an UNVERIFIED number publishes one patient's discharge summary into
    another patient's PHR if the digits were mistyped, and there is no undo that
    reaches the copy the other patient already downloaded. A desk that genuinely
    linked an unverified ABHA on the portal records the verification it actually
    performed first — the portal made them do one to get there.
    """
    if hi_type not in HI_TYPES:
        raise CareContextError(
            f"unknown health-information type {hi_type!r}; expected one of {sorted(HI_TYPES)}"
        )
    if encounter_type not in ENCOUNTER_TYPES:
        raise CareContextError(
            f"unknown encounter type {encounter_type!r}; expected one of {sorted(ENCOUNTER_TYPES)}"
        )
    valid_methods = {code for code, _ in LINK_METHODS}
    if link_method not in valid_methods:
        raise CareContextError(
            f"unknown link method {link_method!r}; expected one of {sorted(valid_methods)}"
        )
    reference = _clean(care_context_reference)
    if not reference:
        raise CareContextError(
            "A care context needs a reference. The episode identifier that was "
            "linked. Without it this row cannot be matched to anything later."
        )
    who = _clean(linked_by)
    if not who:
        raise CareContextError("A link record needs the person who performed it.")

    when = _norm_ts(linked_at, field="link time") or _utc_now_str()
    enc_date = _norm_date(encounter_date, field="encounter date")

    with db_session() as db:
        identity = _load_identity(db, identity_id)
        if identity["status"] != "active":
            raise CareContextError(
                f"ABHA record {identity_id} is {identity['status']}; it cannot carry new links."
            )
        if identity["verification_status"] != "verified":
            raise CareContextError(
                "This ABHA is not verified. Record the verification that was "
                "performed (who, when, by what method) before recording a link. "
                "Linking a mistyped number puts this patient's record into "
                "somebody else's health account, and that cannot be undone."
            )

        existing = db.execute(
            "SELECT id FROM abdm_care_contexts WHERE abha_identity_id = ? "
            "AND care_context_reference = ? AND status = 'linked'",
            (int(identity_id), reference),
        ).fetchone()
        if existing is not None:
            raise CareContextError(
                f"That care context is already recorded as linked to this ABHA "
                f"(record #{existing['id']})."
            )

        # The facility whose episode was linked (multisite-7b, migration 75):
        # the admission's, when the link names one of this patient's
        # admissions, else the patient's admission or latest encounter.
        # Facility 1, with nothing read, while only one is on record.
        patient_id = identity["patient_id"]
        admission_ref = _clean(encounter_ref) if encounter_type == "admission" else None
        facility_id = facility_stamp.resolve(
            db,
            (
                (
                    "the admission",
                    lambda: facility_stamp.admission_facility(
                        db, admission_ref, patient_id=patient_id
                    ),
                ),
            ),
            what="care context",
            fallback=lambda: facility_stamp.patient_facility(db, patient_id=patient_id),
        )

        try:
            cur = db.execute(
                """INSERT INTO abdm_care_contexts
                     (abha_identity_id, patient_id, care_context_reference, display,
                      hi_type, encounter_type, encounter_ref, encounter_date, status,
                      linked_at, linked_by, link_method, link_reference, notes,
                      recorded_by, created_at, updated_at, facility_id)
                   VALUES (?,?,?,?,?,?,?,?,'linked',?,?,?,?,?,?,?,?,?)""",
                (
                    int(identity_id),
                    identity["patient_id"],
                    reference,
                    _clean(display),
                    hi_type,
                    encounter_type,
                    _clean(encounter_ref),
                    enc_date,
                    when,
                    who,
                    link_method,
                    _clean(link_reference),
                    _clean(notes),
                    recorded_by or who,
                    _utc_now_str(),
                    _utc_now_str(),
                    facility_id,
                ),
            )
        except DBIntegrityError as exc:
            raise CareContextError(
                "That care context is already recorded as linked to this ABHA."
            ) from exc

        context_id = int(cur.lastrowid)
        _write_event(
            db,
            entity_type="care_context",
            entity_id=context_id,
            patient_id=identity["patient_id"],
            event_type="linked",
            to_status="linked",
            detail=(
                f"{hi_type} '{reference}' linked by {who} "
                f"({dict(LINK_METHODS).get(link_method, link_method)})"
            ),
            actor_id=recorded_by or who,
            occurred_at=when,
            metadata={"abha_identity_id": int(identity_id), "hi_type": hi_type},
        )
        row = _row(
            db.execute("SELECT * FROM abdm_care_contexts WHERE id = ?", (context_id,)).fetchone()
        )
    return _decorate_care_context(row)


def unlink_care_context(
    *,
    care_context_id: int,
    reason: str,
    actor_id: str,
    unlinked_at: str | None = None,
) -> dict:
    """RECORD that a human unlinked this care context, elsewhere.

    A LINKED CARE CONTEXT CANNOT SILENTLY UNLINK. Three things enforce that, and
    all three are load-bearing:

      1. THE ROW IS NEVER DELETED. Status moves to 'unlinked' and every field
         about the original link survives. A DELETE would make "was this episode
         ever linked?" unanswerable — which is precisely the question a patient
         asks when their discharge summary disappears from their app.
      2. A REASON IS MANDATORY. Not a checkbox, not an optional note: at least
         three characters of why, refused otherwise.
      3. AN EVENT IS WRITTEN. `abdm_events` gets a row naming who, when and why,
         so the unlink shows up in the history beside the link.

    ``correct_abha`` refuses to change a number underneath live links for the
    same reason: that would be an unlink nobody performed and nobody can see.
    """
    reason = _require_reason(reason, what="Unlinking a care context", error=CareContextError)
    who = _clean(actor_id)
    if not who:
        raise CareContextError("An unlink record needs the person who performed it.")
    when = _norm_ts(unlinked_at, field="unlink time") or _utc_now_str()

    with db_session() as db:
        row = db.execute(
            "SELECT * FROM abdm_care_contexts WHERE id = ?", (int(care_context_id),)
        ).fetchone()
        if row is None:
            raise CareContextError(f"care context {care_context_id} not found")
        if row["status"] != "linked":
            raise CareContextError(
                f"That care context is already {row['status']}; it was unlinked "
                f"on {row['unlinked_at']}."
            )

        db.execute(
            """UPDATE abdm_care_contexts
                  SET status = 'unlinked', unlinked_at = ?, unlinked_by = ?,
                      unlink_reason = ?, updated_at = ?
                WHERE id = ?""",
            (when, who, reason, _utc_now_str(), int(care_context_id)),
        )
        _write_event(
            db,
            entity_type="care_context",
            entity_id=int(care_context_id),
            patient_id=row["patient_id"],
            event_type="unlinked",
            from_status="linked",
            to_status="unlinked",
            detail=f"'{row['care_context_reference']}' unlinked by {who}",
            reason=reason,
            actor_id=who,
            occurred_at=when,
        )
        fresh = _row(
            db.execute(
                "SELECT * FROM abdm_care_contexts WHERE id = ?", (int(care_context_id),)
            ).fetchone()
        )
    return _decorate_care_context(fresh)


def list_care_contexts(
    *,
    patient_id: str | None = None,
    identity_id: int | None = None,
    status: str | None = None,
    department: str | None = None,
    limit: int = 200,
) -> list[dict]:
    """Care-context records. UNLINKED ONES ARE INCLUDED BY DEFAULT.

    That default is deliberate: a list that silently hides unlinked rows is a
    list in which an unlink looks like a deletion, which is the failure this
    table's whole design is against.
    """
    sql = [
        "SELECT c.*, p.name AS patient_name, p.mrn AS patient_mrn,",
        "       i.abha_number, i.abha_address, i.verification_status",
        "  FROM abdm_care_contexts c",
        "  LEFT JOIN patients p ON p.id = c.patient_id",
        "  LEFT JOIN abha_identities i ON i.id = c.abha_identity_id",
        " WHERE 1=1",
    ]
    params: list[Any] = []
    if patient_id:
        sql.append(" AND c.patient_id = ?")
        params.append(patient_id)
    if identity_id:
        sql.append(" AND c.abha_identity_id = ?")
        params.append(int(identity_id))
    if status:
        sql.append(" AND c.status = ?")
        params.append(status)
    if department is not None:
        from permissions import scope_patient_ids_sql

        scope_sql, scope_params = scope_patient_ids_sql(department)
        sql.append(f" AND c.patient_id IN ({scope_sql})")
        params.extend(scope_params)
    sql.append(" ORDER BY c.id DESC LIMIT ?")
    params.append(max(1, min(int(limit), 1000)))

    with db_session() as db:
        rows = [_row(r) for r in db.execute("\n".join(sql), params).fetchall()]
    out = []
    for row in rows:
        decorated = _decorate_care_context(row)
        decorated["abha_number_formatted"] = (
            format_abha_number(row["abha_number"]) if row.get("abha_number") else None
        )
        out.append(decorated)
    return out


# ══════════════════════════════════════════════════════════════════════════════
#  Consent artefacts
# ══════════════════════════════════════════════════════════════════════════════


def _consent_effective(row: dict, *, now: datetime | None = None) -> dict:
    """Derive what this artefact means RIGHT NOW, at read time.

    WHY EXPIRY IS DERIVED AND NOT TRUSTED FROM THE COLUMN. A stored status of
    'granted' on an artefact whose `expires_at` passed last month is a row that
    reads as live permission. There is no sweeper job that can be relied on to
    have run — and even if there were, the window between the expiry and the
    sweep is exactly when somebody reads the screen. So every read computes it,
    and `status` is reported beside `effective_status` rather than instead of it,
    so a desk can see that the stored row has not been updated yet.
    """
    now = now or datetime.now(UTC)
    status = row.get("status") or "requested"
    expires = _parse_ts(row.get("expires_at"))
    is_expired = bool(expires and expires <= now)

    if status in ("revoked", "denied", "expired"):
        effective = status
    elif is_expired:
        effective = "expired"
    else:
        effective = status

    return {
        "effective_status": effective,
        "is_expired": is_expired,
        # 'granted AND in date' — the ONE boolean a screen should key on. Note
        # what it is NOT: it is not permission for anything inside Orb. Orb does
        # not share data through the national exchange, so this flag governs
        # nothing but the accuracy of the record.
        "is_effective": effective == "granted",
        "expires_at_display": (
            expires.strftime("%Y-%m-%d %H:%M") if expires else "No expiry recorded"
        ),
        "days_to_expiry": (
            int((expires - now).total_seconds() // 86400) if expires and not is_expired else None
        ),
    }


def _decorate_consent(row: dict, *, now: datetime | None = None) -> dict:
    out = dict(row)
    try:
        out["hi_types"] = json.loads(row.get("hi_types_json") or "[]")
    except (TypeError, ValueError):
        out["hi_types"] = []
    out["hi_types_recorded"] = bool(out["hi_types"])
    out.update(_consent_effective(row, now=now))
    out["purpose_label"] = dict(PURPOSE_CODES).get(
        (row.get("purpose_code") or ""), row.get("purpose_code")
    )
    out["transmitted_to_abdm"] = False
    # Said explicitly rather than left to be inferred: a revoked artefact is
    # never coming back, and the screen renders the sentence, not a disabled
    # dropdown the user has to reason about.
    out["can_be_reinstated"] = False
    out["is_revoked"] = row.get("status") == "revoked"
    return out


def _require_consent_transition(current: str, target: str) -> None:
    allowed = CONSENT_TRANSITIONS.get(current)
    if allowed is None:
        raise ConsentError(f"unknown consent status on record: {current!r}")
    if target not in allowed:
        if current == "revoked":
            raise IllegalConsentTransition(
                "This consent was REVOKED by the patient and cannot be "
                f"'{target}' again. Revocation is the one control the patient "
                "actually has. If they have consented afresh, the consent "
                "manager issued a NEW artefact. Record that one."
            )
        raise IllegalConsentTransition(
            f"a consent that is '{current}' cannot become '{target}'. "
            f"Legal next steps: {', '.join(sorted(allowed)) or 'none, this artefact is closed'}."
        )


def record_consent_request(
    *,
    patient_id: str,
    requester_name: str,
    purpose_code: str = "CAREMGT",
    purpose_text: str | None = None,
    hi_types: list[str] | None = None,
    date_range_from: str | None = None,
    date_range_to: str | None = None,
    expires_at: str | None = None,
    request_id: str | None = None,
    identity_id: int | None = None,
    requester_type: str = "hiu",
    recorded_where: str = "consent_manager",
    recorded_by: str | None = None,
    notes: str | None = None,
) -> dict:
    """Record a consent REQUEST that was raised on the consent manager elsewhere.

    Status starts at 'requested'. An artefact id is not required yet, because a
    consent artefact does not exist until the patient grants it — recording one
    at request time would be inventing the patient's answer.
    """
    valid_purposes = {code for code, _ in PURPOSE_CODES}
    if purpose_code not in valid_purposes:
        raise ConsentError(
            f"unknown purpose {purpose_code!r}; expected one of {sorted(valid_purposes)}"
        )
    if requester_type not in REQUESTER_TYPES:
        raise ConsentError(
            f"unknown requester type {requester_type!r}; expected one of {sorted(REQUESTER_TYPES)}"
        )
    if recorded_where not in RECORDED_PLACES:
        raise ConsentError(
            f"unknown place {recorded_where!r}; expected one of {sorted(RECORDED_PLACES)}"
        )
    name = _clean(requester_name)
    if not name:
        raise ConsentError("A consent record needs the name of who asked for the data.")
    if purpose_code == "OTHER" and not _clean(purpose_text):
        raise ConsentError(
            "Purpose 'OTHER' needs the purpose in words. A code that means "
            "'something else' explains nothing to whoever reads this next."
        )

    types = list(hi_types or [])
    unknown = [t for t in types if t not in HI_TYPES]
    if unknown:
        raise ConsentError(
            f"unknown health-information type(s): {', '.join(sorted(unknown))}. "
            f"ABDM's list is {', '.join(HI_TYPES)}."
        )

    frm = _norm_date(date_range_from, field="start of the date range")
    to = _norm_date(date_range_to, field="end of the date range")
    if frm and to and frm > to:
        raise ConsentError("The record date range ends before it starts.")
    expiry = _norm_ts(expires_at, field="consent expiry")

    with db_session() as db:
        _require_patient(db, patient_id)
        if identity_id is not None:
            _load_identity(db, identity_id)
        cur = db.execute(
            """INSERT INTO abdm_consent_artefacts
                 (request_id, patient_id, abha_identity_id, requester_name,
                  requester_type, purpose_code, purpose_text, hi_types_json,
                  date_range_from, date_range_to, expires_at, status, requested_at,
                  recorded_where, recorded_by, notes, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,'requested',?,?,?,?,?,?)""",
            (
                _clean(request_id),
                patient_id,
                int(identity_id) if identity_id else None,
                name,
                requester_type,
                purpose_code,
                _clean(purpose_text),
                json.dumps(types),
                frm,
                to,
                expiry,
                _utc_now_str(),
                recorded_where,
                recorded_by,
                _clean(notes),
                _utc_now_str(),
                _utc_now_str(),
            ),
        )
        consent_id = int(cur.lastrowid)
        _write_event(
            db,
            entity_type="consent_artefact",
            entity_id=consent_id,
            patient_id=patient_id,
            event_type="request_recorded",
            to_status="requested",
            detail=f"{name} requested data for {purpose_code}",
            actor_id=recorded_by,
        )
        row = _row(
            db.execute(
                "SELECT * FROM abdm_consent_artefacts WHERE id = ?", (consent_id,)
            ).fetchone()
        )
    return _decorate_consent(row)


def _consent_transition(
    *,
    consent_id: int,
    target: str,
    columns: dict[str, Any],
    event_type: str,
    detail: str,
    reason: str | None = None,
    actor_id: str | None = None,
    occurred_at: str | None = None,
) -> dict:
    """The ONE write path for every consent status change.

    Every lifecycle step goes through here, so the transition table is consulted
    exactly once and cannot be bypassed by a function that "just" sets a column.
    """
    with db_session() as db:
        row = db.execute(
            "SELECT * FROM abdm_consent_artefacts WHERE id = ?", (int(consent_id),)
        ).fetchone()
        if row is None:
            raise ConsentError(f"consent artefact {consent_id} not found")
        current = row["status"]
        _require_consent_transition(current, target)

        sets = ", ".join(f"{k} = ?" for k in columns)
        params = list(columns.values())
        db.execute(
            f"UPDATE abdm_consent_artefacts SET status = ?, {sets}, updated_at = ? WHERE id = ?",
            [target, *params, _utc_now_str(), int(consent_id)],
        )
        _write_event(
            db,
            entity_type="consent_artefact",
            entity_id=int(consent_id),
            patient_id=row["patient_id"],
            event_type=event_type,
            from_status=current,
            to_status=target,
            detail=detail,
            reason=reason,
            actor_id=actor_id,
            occurred_at=occurred_at,
        )
        fresh = _row(
            db.execute(
                "SELECT * FROM abdm_consent_artefacts WHERE id = ?", (int(consent_id),)
            ).fetchone()
        )
    return _decorate_consent(fresh)


def record_consent_grant(
    *,
    consent_id: int,
    artefact_id: str,
    granted_at: str | None = None,
    expires_at: str | None = None,
    hi_types: list[str] | None = None,
    actor_id: str | None = None,
) -> dict:
    """Record that the patient GRANTED the consent, on their consent manager.

    The artefact id is required here and only here: it is the consent manager's
    own identifier, and an artefact without one cannot be matched to anything the
    patient can see in their own app.
    """
    aid = _clean(artefact_id)
    if not aid:
        raise ConsentError(
            "A granted consent needs the consent manager's artefact id, without "
            "it this record cannot be matched to what the patient sees."
        )
    when = _norm_ts(granted_at, field="time consent was granted") or _utc_now_str()
    expiry = _norm_ts(expires_at, field="consent expiry")

    columns: dict[str, Any] = {"artefact_id": aid, "granted_at": when}
    if expiry:
        columns["expires_at"] = expiry
    if hi_types is not None:
        unknown = [t for t in hi_types if t not in HI_TYPES]
        if unknown:
            raise ConsentError(f"unknown health-information type(s): {', '.join(sorted(unknown))}.")
        columns["hi_types_json"] = json.dumps(list(hi_types))

    try:
        return _consent_transition(
            consent_id=consent_id,
            target="granted",
            columns=columns,
            event_type="granted",
            detail=f"Granted by the patient; artefact {aid}",
            actor_id=actor_id,
            occurred_at=when,
        )
    except DBIntegrityError as exc:
        raise ConsentError(
            f"artefact id {aid} is already recorded against another consent row."
        ) from exc


def record_consent_denial(
    *,
    consent_id: int,
    reason: str,
    denied_at: str | None = None,
    actor_id: str | None = None,
) -> dict:
    """Record that the patient REFUSED. Kept forever, like every other answer."""
    reason = _require_reason(reason, what="Recording a refusal", error=ConsentError)
    when = _norm_ts(denied_at, field="time consent was denied") or _utc_now_str()
    return _consent_transition(
        consent_id=consent_id,
        target="denied",
        columns={"denied_at": when, "denial_reason": reason},
        event_type="denied",
        detail="Refused by the patient",
        reason=reason,
        actor_id=actor_id,
        occurred_at=when,
    )


def revoke_consent(
    *,
    consent_id: int,
    reason: str,
    actor_id: str,
    revoked_at: str | None = None,
) -> dict:
    """Record that the patient WITHDREW the consent. ONE WAY. NO RETURN.

    This is the sharpest edge in the module. Revocation is the only real control
    the national stack gives a patient over their own data, and a record that can
    quietly walk it back is worse than no record at all — it would show a live
    permission the patient believes they cancelled.

    So: a revoked artefact is terminal. ``CONSENT_TRANSITIONS['revoked']`` is
    empty, every status change goes through ``_consent_transition`` which
    consults it, and there is no other write path. A fresh consent is a NEW
    artefact with the consent manager's new id — which is what actually happens
    in the national stack anyway.

    A reason and a named actor are both mandatory: "revoked" with neither is a
    row that cannot be defended to the patient who asked for it.
    """
    reason = _require_reason(reason, what="Revoking a consent", error=ConsentError)
    who = _clean(actor_id)
    if not who:
        raise ConsentError("A revocation needs the person who recorded it.")
    when = _norm_ts(revoked_at, field="revoked_at") or _utc_now_str()
    return _consent_transition(
        consent_id=consent_id,
        target="revoked",
        columns={"revoked_at": when, "revoked_by": who, "revocation_reason": reason},
        event_type="revoked",
        detail=f"Withdrawn by the patient; recorded by {who}",
        reason=reason,
        actor_id=who,
        occurred_at=when,
    )


def expire_consent(
    *,
    consent_id: int,
    actor_id: str | None = None,
    occurred_at: str | None = None,
) -> dict:
    """Write down that an artefact's own clock ran out.

    Reads already DERIVE expiry (see ``_consent_effective``) so nothing depends
    on this being called; it exists so a desk can settle the stored row and stop
    seeing it in the live list.
    """
    when = _norm_ts(occurred_at, field="occurred_at") or _utc_now_str()
    return _consent_transition(
        consent_id=consent_id,
        target="expired",
        columns={},
        event_type="expired",
        detail="The artefact's validity period ended",
        actor_id=actor_id,
        occurred_at=when,
    )


def get_consent(consent_id: int, *, include_events: bool = True) -> dict:
    with db_session() as db:
        row = db.execute(
            "SELECT c.*, p.name AS patient_name, p.mrn AS patient_mrn "
            "FROM abdm_consent_artefacts c LEFT JOIN patients p ON p.id = c.patient_id "
            "WHERE c.id = ?",
            (int(consent_id),),
        ).fetchone()
        if row is None:
            raise ConsentError(f"consent artefact {consent_id} not found")
        events = []
        if include_events:
            events = [
                _row(r)
                for r in db.execute(
                    "SELECT * FROM abdm_events WHERE entity_type = 'consent_artefact' "
                    "AND entity_id = ? ORDER BY id DESC",
                    (int(consent_id),),
                ).fetchall()
            ]
    out = _decorate_consent(_row(row))
    out["events"] = events
    return out


def list_consents(
    *,
    patient_id: str | None = None,
    status: str | None = None,
    department: str | None = None,
    limit: int = 200,
) -> list[dict]:
    sql = [
        "SELECT c.*, p.name AS patient_name, p.mrn AS patient_mrn",
        "  FROM abdm_consent_artefacts c",
        "  LEFT JOIN patients p ON p.id = c.patient_id",
        " WHERE 1=1",
    ]
    params: list[Any] = []
    if patient_id:
        sql.append(" AND c.patient_id = ?")
        params.append(patient_id)
    if status:
        sql.append(" AND c.status = ?")
        params.append(status)
    if department is not None:
        from permissions import scope_patient_ids_sql

        scope_sql, scope_params = scope_patient_ids_sql(department)
        sql.append(f" AND c.patient_id IN ({scope_sql})")
        params.extend(scope_params)
    sql.append(" ORDER BY c.id DESC LIMIT ?")
    params.append(max(1, min(int(limit), 1000)))

    with db_session() as db:
        rows = [_row(r) for r in db.execute("\n".join(sql), params).fetchall()]
    return [_decorate_consent(r) for r in rows]


# ══════════════════════════════════════════════════════════════════════════════
#  The work queue: encounters nobody has linked
# ══════════════════════════════════════════════════════════════════════════════

#: The three states an unlinked encounter can be in, in the order a desk can act
#: on them. The bucket is what makes the queue workable: "no ABHA at all" is a
#: conversation with the patient, "unverified" is a verification, and "ready" is
#: two minutes on the portal.
QUEUE_BUCKETS: tuple[tuple[str, str], ...] = (
    ("ready_to_link", "Verified ABHA: link it"),
    ("abha_unverified", "ABHA recorded but not verified"),
    ("no_abha", "No ABHA on record"),
)


def unlinked_encounter_queue(
    *,
    department: str | None = None,
    bucket: str | None = None,
    include_discharged: bool = True,
    limit: int = 200,
    now: datetime | None = None,
) -> dict:
    """Encounters with no live care-context link, oldest first.

    THIS IS THE WHOLE MODULE'S REASON TO EXIST ON A SCREEN. Everything else here
    is a record of work already done; this is the list of work NOT done, which is
    the only part a desk can act on. An ABDM compliance story built on "we have
    the fields" and no queue is a story about a database, not about a hospital
    that links its episodes.

    `department` is the CALLER'S scope, resolved from the authenticated user by
    the route — never accepted from the request.
    """
    now = now or datetime.now(UTC)
    if bucket is not None and bucket not in {b for b, _ in QUEUE_BUCKETS}:
        raise AbdmError(f"unknown bucket {bucket!r}")

    sql = [
        "SELECT a.id AS encounter_ref, a.patient_id, a.department, a.ward,",
        "       a.status AS encounter_status, a.created_at AS encounter_at,",
        "       a.admission_type, p.name AS patient_name, p.mrn AS patient_mrn,",
        "       (SELECT i.id FROM abha_identities i",
        "         WHERE i.patient_id = a.patient_id AND i.status = 'active'",
        "         ORDER BY (i.verification_status = 'verified') DESC,",
        "                  i.is_primary DESC, i.id DESC LIMIT 1) AS abha_identity_id",
        "  FROM admissions a",
        "  LEFT JOIN patients p ON p.id = a.patient_id",
        " WHERE NOT EXISTS (",
        "        SELECT 1 FROM abdm_care_contexts c",
        "         WHERE c.encounter_ref = a.id AND c.status = 'linked')",
    ]
    params: list[Any] = []
    if not include_discharged:
        sql.append(" AND a.status = 'active'")
    if department is not None:
        from permissions import scope_patient_ids_sql

        scope_sql, scope_params = scope_patient_ids_sql(department)
        sql.append(f" AND a.patient_id IN ({scope_sql})")
        params.extend(scope_params)
    sql.append(" ORDER BY a.created_at ASC, a.rowid ASC LIMIT ?")
    params.append(max(1, min(int(limit), 1000)))

    with db_session() as db:
        rows = [_row(r) for r in db.execute("\n".join(sql), params).fetchall()]
        identities: dict[int, dict] = {}
        ids = [r["abha_identity_id"] for r in rows if r.get("abha_identity_id")]
        if ids:
            placeholders = ",".join("?" for _ in ids)
            for r in db.execute(
                f"SELECT * FROM abha_identities WHERE id IN ({placeholders})", ids
            ).fetchall():
                identities[int(r["id"])] = _row(r)

    out: list[dict] = []
    counts = {b: 0 for b, _ in QUEUE_BUCKETS}
    for row in rows:
        identity = identities.get(row.get("abha_identity_id") or -1)
        if identity is None:
            row_bucket = "no_abha"
        elif identity.get("verification_status") == "verified":
            row_bucket = "ready_to_link"
        else:
            row_bucket = "abha_unverified"
        counts[row_bucket] += 1
        if bucket is not None and row_bucket != bucket:
            continue

        age = _age_days(_parse_ts(row.get("encounter_at")), now=now)
        entry = dict(row)
        entry["bucket"] = row_bucket
        entry["bucket_label"] = dict(QUEUE_BUCKETS)[row_bucket]
        entry["age_days"] = age
        entry["is_stale"] = bool(age is not None and age >= STALE_AFTER_DAYS)
        entry["abha_number"] = identity.get("abha_number") if identity else None
        entry["abha_number_formatted"] = (
            format_abha_number(identity["abha_number"])
            if identity and identity.get("abha_number")
            else None
        )
        entry["abha_address"] = identity.get("abha_address") if identity else None
        entry["verification_status"] = identity.get("verification_status") if identity else None
        entry["verification_label"] = (
            VERIFICATION_LABEL.get((identity.get("verification_status") or ""), "Not verified")
            if identity
            else "No ABHA on record"
        )
        out.append(entry)

    return {
        "encounters": out,
        "total": len(out),
        "counts": counts,
        "buckets": [
            {"id": b, "label": left_item, "count": counts[b]} for b, left_item in QUEUE_BUCKETS
        ],
        "stale_after_days": STALE_AFTER_DAYS,
    }


def abdm_summary(*, department: str | None = None, now: datetime | None = None) -> dict:
    """The board a CIO reads: how much ABHA coverage exists, how much of it is
    actually verified, and what is waiting."""
    now = now or datetime.now(UTC)
    scope_clause = ""
    scope_params: list[Any] = []
    if department is not None:
        from permissions import scope_patient_ids_sql

        scope_sql, params = scope_patient_ids_sql(department)
        scope_clause = f" AND patient_id IN ({scope_sql})"
        scope_params = list(params)

    with db_session() as db:
        identity_counts = {
            r["verification_status"]: r["n"]
            for r in db.execute(
                "SELECT verification_status, COUNT(*) AS n FROM abha_identities "
                f"WHERE status = 'active'{scope_clause} GROUP BY verification_status",
                scope_params,
            ).fetchall()
        }
        patients_with_abha = db.execute(
            "SELECT COUNT(DISTINCT patient_id) AS n FROM abha_identities "
            f"WHERE status = 'active'{scope_clause}",
            scope_params,
        ).fetchone()["n"]
        links = {
            r["status"]: r["n"]
            for r in db.execute(
                "SELECT status, COUNT(*) AS n FROM abdm_care_contexts "
                f"WHERE 1=1{scope_clause} GROUP BY status",
                scope_params,
            ).fetchall()
        }
        consent_rows = [
            _row(r)
            for r in db.execute(
                f"SELECT * FROM abdm_consent_artefacts WHERE 1=1{scope_clause}",
                scope_params,
            ).fetchall()
        ]

    consents = {s: 0 for s in CONSENT_STATUSES}
    for row in consent_rows:
        eff = _consent_effective(row, now=now)["effective_status"]
        consents[eff] = consents.get(eff, 0) + 1

    queue = unlinked_encounter_queue(department=department, limit=1000, now=now)

    verified = int(identity_counts.get("verified", 0))
    total_identities = sum(int(v) for v in identity_counts.values())
    return {
        "identities": {
            "total": total_identities,
            "verified": verified,
            "unverified": int(identity_counts.get("unverified", 0)),
            "verification_failed": int(identity_counts.get("verification_failed", 0)),
            "deactivated": int(identity_counts.get("deactivated", 0)),
            "patients_with_abha": int(patients_with_abha),
            # Reported as a fraction with both numbers, never as a bare
            # percentage: "62%" of an unstated denominator is how a compliance
            # dashboard becomes decorative.
            "verified_of_total": f"{verified} of {total_identities}",
        },
        "care_contexts": {
            "linked": int(links.get("linked", 0)),
            "unlinked": int(links.get("unlinked", 0)),
        },
        "consents": consents,
        "queue": {
            "total": queue["total"],
            "counts": queue["counts"],
        },
        # Repeated in the summary payload as well as everywhere else, because a
        # summary is the payload most likely to be screenshotted into a slide.
        "gateway_connected": False,
    }
