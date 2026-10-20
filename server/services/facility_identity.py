"""Orb: facility identity, the supplier snapshot and the create gate
(multisite-2, W9-E, 10 October 2026).

WHAT THIS MODULE ANSWERS
  * Who a facility is, for print: identity_block(facility_id) gives the legal
    name, the one-line address, GSTIN, PAN, state code and the registration
    numbers (HFR, ABDM HIP, NABH, clinical establishment), plus whether the
    block is complete enough for a GST tax invoice. Facility 1 works whatever
    ORB_MULTI_SITE says (W162: the HFR half needs no ruling, and until now
    invoices printed "Not configured").
  * What a numbered bill or receipt says about its supplier: supplier_snapshot
    gives the six values (SNAPSHOT_COLUMNS, migration 72) that billing and
    payments write in the SAME statement that assigns the document's number.
    supplier_block turns a row back into the printed block: a numbered row
    with a snapshot prints the snapshot, a numbered row without one (issued
    before migration 72) prints the old block exactly as it always printed,
    and only a draft prints the live identity. An issued GST invoice never
    changes after issue; the migration's triggers refuse any later change.
  * The name a printed discharge record and the patient's discharge page carry:
    print_site_name, the facility's stored name or the site settings file's
    [site] name for facility 1, else None (the screens say nothing rather than
    invent one, as before).
  * Editing identity: update_facility validates (GSTIN format and check
    character, state code equal to the GSTIN's first two digits, one PAN
    across every facility because one appliance is one legal entity, PIN
    code, e-mail, timezone equal to the appliance's in v1 per MS-R8), writes
    one audit row (category site_admin, action facility_updated) carrying the
    field-by-field diff, and forgets every facility cache.
  * Growth: can_create_facility lists why a second facility may not be added.
    It always refuses while the facility fence holds its sentinel '*'
    (facility_fence.FACILITY_BLIND_SURFACES), so no second facility can be
    created through the product until the scope sweep has finished.
    create_facility and deactivate_facility run only after that gate.

A facility's code is fixed once created: ward labels (wards.facility_code is
'MAIN' on every row) and later the per-facility keys are spelled with it.

Every function here takes the caller's connection (db=) or opens its own
session; none commits a connection it was given. Nothing here is async: async
callers go through database.run_db.
"""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import Iterable
from datetime import UTC, date, datetime
from typing import Any

logger = logging.getLogger("orb.facility_identity")

# ═════════════════════════════════════════════════════════════════════════════
#  Words and fields
# ═════════════════════════════════════════════════════════════════════════════

NOT_CONFIGURED = "Not configured"

#: The notice a supplier block without a GSTIN or address carries, word for
#: word what invoice_print_view has printed since billing shipped.
LEGACY_NOTICE = (
    "This hospital's GSTIN and registered address are not configured in "
    "Orb, so this document is a BILL, not a GST tax invoice."
)

#: Migration 72's columns on invoices and payments, in the order every writer
#: binds them (tests/test_invoice_supplier_snapshot.py pins the match).
SNAPSHOT_COLUMNS = (
    "supplier_facility_id",
    "supplier_legal_name",
    "supplier_address",
    "supplier_gstin",
    "supplier_state_code",
    "supplier_snapshot_at",
)
SNAPSHOT_COLUMNS_SQL = ", ".join(SNAPSHOT_COLUMNS)
SNAPSHOT_PLACEHOLDERS = ",".join("?" for _ in SNAPSHOT_COLUMNS)

#: Fields an identity edit may change, with the words a screen shows for each.
FIELD_LABELS = {
    "name": "Name",
    "short_name": "Short name",
    "legal_name": "Legal name",
    "address_line1": "Address line 1",
    "address_line2": "Address line 2",
    "city": "City",
    "district": "District",
    "state": "State",
    "state_code": "GST state code",
    "pincode": "PIN code",
    "phone": "Phone",
    "email": "E-mail",
    "gstin": "GSTIN",
    "pan": "PAN",
    "hfr_id": "Health facility registry ID",
    "abdm_hip_id": "ABDM health information provider ID",
    "nabh_accreditation_no": "NABH accreditation number",
    "nabh_valid_until": "NABH accreditation valid until",
    "clinical_establishment_reg_no": "Clinical establishment registration number",
    "timezone": "Timezone",
}
EDITABLE_FIELDS = tuple(FIELD_LABELS)

#: Set on create only, never edited: the code labels wards and keys.
CREATE_ONLY_FIELDS = ("code",)

#: Longest text kept in any identity field.
_MAX_TEXT = 200

#: GST state and union territory codes (the first two digits of a GSTIN).
GST_STATE_CODES = {
    "01": "Jammu and Kashmir",
    "02": "Himachal Pradesh",
    "03": "Punjab",
    "04": "Chandigarh",
    "05": "Uttarakhand",
    "06": "Haryana",
    "07": "Delhi",
    "08": "Rajasthan",
    "09": "Uttar Pradesh",
    "10": "Bihar",
    "11": "Sikkim",
    "12": "Arunachal Pradesh",
    "13": "Nagaland",
    "14": "Manipur",
    "15": "Mizoram",
    "16": "Tripura",
    "17": "Meghalaya",
    "18": "Assam",
    "19": "West Bengal",
    "20": "Jharkhand",
    "21": "Odisha",
    "22": "Chhattisgarh",
    "23": "Madhya Pradesh",
    "24": "Gujarat",
    "25": "Daman and Diu",
    "26": "Dadra and Nagar Haveli and Daman and Diu",
    "27": "Maharashtra",
    "28": "Andhra Pradesh (before 2014)",
    "29": "Karnataka",
    "30": "Goa",
    "31": "Lakshadweep",
    "32": "Kerala",
    "33": "Tamil Nadu",
    "34": "Puducherry",
    "35": "Andaman and Nicobar Islands",
    "36": "Telangana",
    "37": "Andhra Pradesh",
    "38": "Ladakh",
    "97": "Other territory",
}

_GSTIN_RE = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")
_PAN_RE = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$")
_CODE_RE = re.compile(r"^[A-Z0-9]{2,12}$")
_PINCODE_RE = re.compile(r"^[1-9][0-9]{5}$")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_PHONE_RE = re.compile(r"^[0-9+()\- ]{6,20}$")
_REG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ./_\-]{0,63}$")
_GSTIN_CHARS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"


class FacilityIdentityError(Exception):
    """A refused identity read or write. `status` is the HTTP status a route
    answers with; `errors` lists {field, code, message} for each bad field."""

    def __init__(
        self,
        message: str,
        *,
        code: str,
        status: int = 400,
        errors: list | None = None,
        detail: dict | None = None,
    ):
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status
        self.errors = errors or []
        self.detail = detail or {}

    def body(self) -> dict:
        out: dict[str, Any] = {"error": self.code, "message": self.message}
        if self.errors:
            out["errors"] = self.errors
        out.update(self.detail)
        return out


# ═════════════════════════════════════════════════════════════════════════════
#  Validation
# ═════════════════════════════════════════════════════════════════════════════


def gstin_check_character(first14: str) -> str:
    """The GSTIN's fifteenth character for its first fourteen (the base-36
    Luhn variant the GST network publishes)."""
    total = 0
    for i, ch in enumerate(first14):
        product = _GSTIN_CHARS.index(ch) * (1 if i % 2 == 0 else 2)
        total += product // 36 + product % 36
    return _GSTIN_CHARS[(36 - total % 36) % 36]


def gstin_problem(value: str) -> str | None:
    """None for a well-formed GSTIN, else what is wrong, in words."""
    if not _GSTIN_RE.match(value):
        return (
            "A GSTIN is 15 characters: the 2-digit state code, the 10-character "
            "PAN, an entity number, the letter Z and a check character."
        )
    if value[:2] not in GST_STATE_CODES:
        return f"{value[:2]} is not a GST state code."
    if gstin_check_character(value[:14]) != value[14]:
        return (
            "The last character does not match the rest of the GSTIN. Check it for a typing slip."
        )
    return None


def pan_of_gstin(gstin: str | None) -> str | None:
    return gstin[2:12] if gstin and len(gstin) == 15 else None


def _clean_text(value: Any, field: str, errors: list) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        errors.append(
            {
                "field": field,
                "code": "not_text",
                "message": f"{FIELD_LABELS.get(field, field)} must be text.",
            }
        )
        return None
    text = " ".join(value.split())
    if not text:
        return None
    if len(text) > _MAX_TEXT:
        errors.append(
            {
                "field": field,
                "code": "too_long",
                "message": f"{FIELD_LABELS.get(field, field)} must be "
                f"{_MAX_TEXT} characters or fewer.",
            }
        )
        return None
    return text


def appliance_zone_problem(value: str) -> str | None:
    """None when `value` is an IANA timezone keeping the appliance's offsets
    (ruling MS-R8: one zone in v1), else what is wrong."""
    try:
        from zoneinfo import ZoneInfo

        zone = ZoneInfo(value)
    except Exception:
        return f"{value} is not a timezone name Orb knows, for example Asia/Kolkata."
    from services.hospital_time import hospital_timezone

    appliance = hospital_timezone()
    year = datetime.now(UTC).year
    for month in (1, 7):
        probe = datetime(year, month, 15, 12, tzinfo=UTC)
        if probe.astimezone(zone).utcoffset() != probe.astimezone(appliance).utcoffset():
            return (
                "Every facility on this computer keeps the computer's own timezone "
                "for now, so this one must match it."
            )
    return None


def _normalise(field: str, value: Any, errors: list) -> str | None:
    """One field's stored value, or None for blank; problems go to `errors`."""
    text = _clean_text(value, field, errors)
    if text is None:
        return None
    label = FIELD_LABELS.get(field, field)

    def bad(code: str, message: str) -> None:
        errors.append({"field": field, "code": code, "message": message})

    if field in ("gstin", "pan", "code", "state_code"):
        text = text.replace(" ", "").upper()
    if field == "gstin":
        problem = gstin_problem(text)
        if problem:
            bad("bad_gstin", problem)
            return None
    elif field == "pan":
        if not _PAN_RE.match(text):
            bad("bad_pan", "A PAN is 10 characters: five letters, four digits and a letter.")
            return None
    elif field == "state_code":
        if len(text) == 1 and text.isdigit():
            text = "0" + text
        if text not in GST_STATE_CODES:
            bad("bad_state_code", f"{text} is not a GST state code.")
            return None
    elif field == "code":
        if not _CODE_RE.match(text):
            bad("bad_code", "A facility code is 2 to 12 capital letters or digits.")
            return None
    elif field == "pincode":
        text = text.replace(" ", "")
        if not _PINCODE_RE.match(text):
            bad("bad_pincode", "A PIN code is six digits and does not start with 0.")
            return None
    elif field == "email":
        if not _EMAIL_RE.match(text):
            bad("bad_email", "Write the e-mail address in full, for example accounts@hospital.in.")
            return None
    elif field == "phone":
        if not _PHONE_RE.match(text):
            bad("bad_phone", "A phone number is digits, with spaces, + or - if needed.")
            return None
    elif field == "nabh_valid_until":
        try:
            text = date.fromisoformat(text).isoformat()
        except ValueError:
            bad("bad_date", f"{label} is a date written YYYY-MM-DD.")
            return None
    elif field == "timezone":
        problem = appliance_zone_problem(text)
        if problem:
            bad("bad_timezone", problem)
            return None
    elif field in (
        "hfr_id",
        "abdm_hip_id",
        "nabh_accreditation_no",
        "clinical_establishment_reg_no",
    ):
        if not _REG_RE.match(text):
            bad(
                "bad_registration_number",
                f"{label} is letters and digits, with spaces, dots, slashes or dashes, "
                "at most 64 characters.",
            )
            return None
    return text


def _effective_pan(row: dict) -> str | None:
    return row.get("pan") or pan_of_gstin(row.get("gstin"))


def cross_field_problems(merged: dict, others: Iterable[dict]) -> list:
    """The rules that span fields and facilities: the state code is the
    GSTIN's first two digits, the PAN is the GSTIN's characters 3 to 12, and
    every facility on this computer carries the same PAN (one legal entity,
    ruling MS-R1)."""
    errors: list = []
    gstin = merged.get("gstin")
    if gstin:
        if merged.get("state_code") and merged["state_code"] != gstin[:2]:
            errors.append(
                {
                    "field": "state_code",
                    "code": "state_mismatch",
                    "message": (
                        f"The GST state code is {merged['state_code']} but the GSTIN "
                        f"starts with {gstin[:2]}. They must be the same."
                    ),
                }
            )
        if merged.get("pan") and merged["pan"] != pan_of_gstin(gstin):
            errors.append(
                {
                    "field": "pan",
                    "code": "pan_mismatch",
                    "message": (
                        "The PAN is not the one inside the GSTIN (characters 3 to 12). "
                        "They must be the same."
                    ),
                }
            )
    mine = _effective_pan(merged)
    if mine:
        for other in others:
            theirs = _effective_pan(other)
            if theirs and theirs != mine:
                errors.append(
                    {
                        "field": "pan" if merged.get("pan") else "gstin",
                        "code": "pan_differs_from_group",
                        "message": (
                            "Every facility on this computer belongs to one legal entity, "
                            f"so they share one PAN. Facility {other.get('code')} carries "
                            "a different one."
                        ),
                    }
                )
                break
    return errors


# ═════════════════════════════════════════════════════════════════════════════
#  Reading identity
# ═════════════════════════════════════════════════════════════════════════════


def _db_session(immediate: bool = False):
    import database

    return database.db_session(immediate=immediate) if immediate else database.db_session()


def _row_dict(row) -> dict:
    return {k: row[k] for k in row.keys()}


def _facility_row(db, facility_id: int) -> dict | None:
    from services import facility_service

    if not facility_service._table_exists(db, "facilities"):
        return None
    row = db.execute("SELECT * FROM facilities WHERE id = ?", (int(facility_id),)).fetchone()
    return _row_dict(row) if row else None


def _site_name() -> str | None:
    from services import facility_service

    return facility_service._site_name()


def configured_name(row: dict | None, facility_id: int) -> str | None:
    """The name a facility is known by when someone set one: its stored name,
    or for facility 1 the site settings file's [site] name. None otherwise
    (never the "This hospital" fallback, which is not a configured name)."""
    from services.facility_service import MAIN_FACILITY_ID

    stored = ((row or {}).get("name") or "").strip()
    if stored:
        return stored
    if int(facility_id) == MAIN_FACILITY_ID:
        return _site_name()
    return None


def format_address(row: dict | None) -> str | None:
    """The registered address on one line: lines, city, district, then the
    state with the PIN code. None when no part is set."""
    row = row or {}
    address_parts = [row.get(k) for k in ("address_line1", "address_line2", "city", "district")]
    parts = [p.strip() for p in address_parts if isinstance(p, str) and p.strip()]
    tail = " ".join(
        p.strip()
        for p in (row.get("state"), row.get("pincode"))
        if isinstance(p, str) and p.strip()
    )
    if tail:
        parts.append(tail)
    return ", ".join(parts) or None


def identity_block(facility_id: int = 1, *, db=None, row: dict | None = None) -> dict:
    """Everything a print needs to say who the facility is. Keys that are not
    set are None; `invoice_ready` is True once the supplier name, address and
    GSTIN are all set, and `missing_for_invoice` names what is not."""
    from services import facility_service

    if row is None:
        if db is None:
            with _db_session() as own:
                return identity_block(facility_id, db=own)
        row = _facility_row(db, facility_id)
    row = row or {}
    fid = int(row.get("id") or facility_id)
    legal = (row.get("legal_name") or "").strip() or None
    name = configured_name(row, fid)
    supplier = legal or name
    address = format_address(row)
    missing = [
        label
        for value, label in (
            (supplier, "Legal name"),
            (address, "Address"),
            (row.get("gstin"), "GSTIN"),
        )
        if not value
    ]
    state_code = row.get("state_code")
    return {
        "facility_id": fid,
        "code": row.get("code"),
        "name": facility_service.display_name(fid, facility=row),
        "configured_name": name,
        "legal_name": legal,
        "supplier_name": supplier,
        "address": address,
        "gstin": row.get("gstin"),
        "pan": row.get("pan"),
        "state": row.get("state"),
        "state_code": state_code,
        "state_code_name": GST_STATE_CODES.get(state_code or ""),
        "pincode": row.get("pincode"),
        "phone": row.get("phone"),
        "email": row.get("email"),
        "hfr_id": row.get("hfr_id"),
        "abdm_hip_id": row.get("abdm_hip_id"),
        "nabh_accreditation_no": row.get("nabh_accreditation_no"),
        "nabh_valid_until": row.get("nabh_valid_until"),
        "clinical_establishment_reg_no": row.get("clinical_establishment_reg_no"),
        "invoice_ready": not missing,
        "missing_for_invoice": missing,
    }


def document_facility_id(
    db, *, patient_id: str | None = None, admission_id: str | None = None
) -> int:
    """The facility a bill or receipt belongs to: facility 1 while only one is
    on record (no statement beyond the cached count); otherwise the
    admission's facility, else the facility of the patient's latest encounter
    (permissions.FACILITY_PATIENT_SQL), else facility 1. Invoices gain their
    own facility_id in multisite-7b."""
    from services import facility_service

    if not facility_service.multi_facility_data(db=db):
        return facility_service.MAIN_FACILITY_ID
    if admission_id:
        row = db.execute(
            "SELECT facility_id FROM admissions WHERE id = ?", (admission_id,)
        ).fetchone()
        if row is not None and row[0] is not None:
            return int(row[0])
    if patient_id:
        from permissions import FACILITY_PATIENT_SQL

        row = db.execute(
            f"SELECT {FACILITY_PATIENT_SQL} FROM patients p WHERE p.id = ?", (patient_id,)
        ).fetchone()
        if row is not None and row[0] is not None:
            return int(row[0])
    return facility_service.MAIN_FACILITY_ID


def print_site_name(patient_id: str | None = None, *, db=None) -> str | None:
    """The hospital name a printed record or the patient's discharge page
    carries: the configured name of the patient's facility (facility 1 while
    only one is on record), or None when nobody set one. Never raises: a
    missing name prints nothing, as before."""
    try:
        if db is None:
            with _db_session() as own:
                return print_site_name(patient_id, db=own)
        fid = document_facility_id(db, patient_id=patient_id)
        return configured_name(_facility_row(db, fid), fid)
    except Exception:
        logger.debug("facility name unavailable for a print", exc_info=True)
        return _site_name()


# ═════════════════════════════════════════════════════════════════════════════
#  The supplier snapshot (migration 72)
# ═════════════════════════════════════════════════════════════════════════════


def _utc_now() -> str:
    from services.clinical_time import utc_iso_now

    return utc_iso_now()


def supplier_snapshot(
    db, *, patient_id: str | None = None, admission_id: str | None = None
) -> tuple:
    """The six SNAPSHOT_COLUMNS values for a document numbered now, read from
    the facility's live identity inside the caller's transaction."""
    fid = document_facility_id(db, patient_id=patient_id, admission_id=admission_id)
    block = identity_block(fid, db=db)
    return (
        fid,
        block["supplier_name"],
        block["address"],
        block["gstin"],
        block["state_code"],
        _utc_now(),
    )


def _block_from(
    *,
    name: str | None,
    address: str | None,
    gstin: str | None,
    state_code: str | None,
    facility_id: int | None,
    source: str,
    snapshot_at: str | None,
) -> dict:
    return {
        "name": name or NOT_CONFIGURED,
        "name_is_configured": bool(name),
        "address": address or NOT_CONFIGURED,
        "gstin": gstin or NOT_CONFIGURED,
        "state_code": state_code,
        "facility_id": facility_id,
        # A tax invoice needs the supplier's GSTIN and address on its face; a
        # document without them says so in words, never with a placeholder.
        "notice": None if (gstin and address) else LEGACY_NOTICE,
        "source": source,
        "snapshot_at": snapshot_at,
    }


def legacy_supplier_block() -> dict:
    """The block every bill printed before migration 72, unchanged: the site
    settings file's name, and address and GSTIN not configured."""
    site_name = _site_name()
    return {
        "name": site_name or NOT_CONFIGURED,
        "name_is_configured": bool(site_name),
        "address": NOT_CONFIGURED,
        "gstin": NOT_CONFIGURED,
        "state_code": None,
        "facility_id": None,
        "notice": LEGACY_NOTICE,
        "source": "legacy",
        "snapshot_at": None,
    }


def supplier_block(row: dict, *, number_field: str, db=None) -> dict:
    """The supplier block a document prints. `row` is the invoices or payments
    row (SELECT *); `number_field` names its document number column.

      * numbered, with a snapshot: the snapshot, never re-read;
      * numbered, without one (issued before migration 72): the old block;
      * a draft: the live identity of the document's facility."""
    if row.get(number_field):
        if row.get("supplier_snapshot_at"):
            return _block_from(
                name=row.get("supplier_legal_name"),
                address=row.get("supplier_address"),
                gstin=row.get("supplier_gstin"),
                state_code=row.get("supplier_state_code"),
                facility_id=row.get("supplier_facility_id"),
                source="snapshot",
                snapshot_at=row.get("supplier_snapshot_at"),
            )
        return legacy_supplier_block()
    if db is None:
        with _db_session() as own:
            return supplier_block(row, number_field=number_field, db=own)
    fid = document_facility_id(
        db, patient_id=row.get("patient_id"), admission_id=row.get("admission_id")
    )
    block = identity_block(fid, db=db)
    return _block_from(
        name=block["supplier_name"],
        address=block["address"],
        gstin=block["gstin"],
        state_code=block["state_code"],
        facility_id=fid,
        source="live",
        snapshot_at=None,
    )


# ═════════════════════════════════════════════════════════════════════════════
#  Who may edit, and the facility as the screen sees it
# ═════════════════════════════════════════════════════════════════════════════


def actor_facility_id(user: dict, *, db=None) -> int | None:
    from services import facility_service

    return facility_service.user_facility_id(user, db=db)


def actor_is_group_wide(user: dict, *, db=None) -> bool:
    """An admin holding a live enterprise grant (ruling MS-R6 (a))."""
    from services import facility_service

    if (user or {}).get("role") not in facility_service.ENTERPRISE_GRANT_ROLES:
        return False
    return facility_service.has_enterprise_grant((user or {}).get("id"), db=db)


def can_edit(user: dict, facility_id: int, *, db=None) -> bool:
    """facility.manage, and the facility is the account's own (or the account
    is group-wide). Another facility than 1 also needs ORB_MULTI_SITE."""
    import config
    from permissions import FACILITY_MANAGE, has_permission

    from services.facility_service import MAIN_FACILITY_ID

    if not has_permission((user or {}).get("role", ""), FACILITY_MANAGE):
        return False
    if int(facility_id) != MAIN_FACILITY_ID and not config.feature_enabled("multi_site"):
        return False
    if actor_facility_id(user, db=db) == int(facility_id):
        return True
    return actor_is_group_wide(user, db=db)


def facility_view(row: dict, *, user: dict | None = None, db=None) -> dict:
    """One facility as the Facilities screen and GET /api/facilities read it."""
    from services.facility_service import MAIN_FACILITY_ID

    fid = int(row["id"])
    block = identity_block(fid, row=row)
    out = {
        k: row.get(k)
        for k in (
            "id",
            "uuid",
            "code",
            "created_at",
            "created_by",
            "updated_at",
            "updated_by",
            "audit_epoch_id",
        )
    }
    for field in EDITABLE_FIELDS:
        out[field] = row.get(field)
    out.update(
        {
            "is_active": bool(row.get("is_active")),
            "is_main": fid == MAIN_FACILITY_ID,
            "display_name": block["name"],
            "site_name": _site_name() if fid == MAIN_FACILITY_ID else None,
            "address": block["address"],
            "supplier_name": block["supplier_name"],
            "state_code_name": block["state_code_name"],
            "invoice_ready": block["invoice_ready"],
            "missing_for_invoice": block["missing_for_invoice"],
            "can_edit": user is not None and bool(user) and can_edit(user, fid, db=db),
        }
    )
    return out


def list_views(user: dict, *, db=None) -> list[dict]:
    from services import facility_service

    if db is None:
        with _db_session() as own:
            return list_views(user, db=own)
    return [facility_view(r, user=user, db=db) for r in facility_service.list_facilities(db=db)]


def get_view(facility_id: int, user: dict, *, db=None) -> dict:
    if db is None:
        with _db_session() as own:
            return get_view(facility_id, user, db=own)
    row = _facility_row(db, facility_id)
    if row is None:
        raise FacilityIdentityError("No facility has that number.", code="not_found", status=404)
    return facility_view(row, user=user, db=db)


# ═════════════════════════════════════════════════════════════════════════════
#  Editing identity
# ═════════════════════════════════════════════════════════════════════════════


def _audit(db, *, action: str, user: dict, facility: dict, summary: str, metadata: dict) -> None:
    from services.audit_service import log_audit_event

    log_audit_event(
        correlation_id=str(uuid.uuid4()),
        event_category="site_admin",
        action_type=action,
        outcome="success",
        db=db,
        summary_message=summary,
        actor_user_id=(user or {}).get("id"),
        actor_role=(user or {}).get("role"),
        target_type="facility",
        target_id=str(facility["id"]),
        metadata={"facility_id": int(facility["id"]), "code": facility.get("code"), **metadata},
    )


def _forget_caches() -> None:
    from services import facility_service

    facility_service.invalidate_cache()


def _validated_changes(changes: dict, *, allow: Iterable[str]) -> tuple[dict, list]:
    allowed = set(allow)
    errors: list = []
    out: dict = {}
    if not isinstance(changes, dict):
        raise FacilityIdentityError(
            "Send the fields to change as an object.", code="invalid_identity"
        )
    for field, value in changes.items():
        if field in CREATE_ONLY_FIELDS and field not in allowed:
            errors.append(
                {
                    "field": field,
                    "code": "code_fixed",
                    "message": "A facility's code cannot be changed once it is created.",
                }
            )
            continue
        if field not in allowed:
            errors.append(
                {"field": field, "code": "not_editable", "message": f"{field} cannot be set here."}
            )
            continue
        out[field] = _normalise(field, value, errors)
    return out, errors


def _others(db, facility_id: int | None) -> list[dict]:
    from services import facility_service

    return [
        r
        for r in facility_service.list_facilities(db=db)
        if facility_id is None or int(r["id"]) != int(facility_id)
    ]


def update_facility(facility_id: int, changes: dict, *, user: dict, db=None) -> dict:
    """Apply an identity edit and return the facility as the screen reads it.

    Refusals (FacilityIdentityError): 404 for a facility that does not exist,
    a field error list (400) for anything malformed or inconsistent, 403 when
    the account may not edit this facility. A blank value clears a field,
    except a name on any facility but 1 (the schema requires one). The state
    code is filled from the GSTIN when it is blank. An edit that changes
    nothing writes nothing."""
    if db is None:
        with _db_session(immediate=True) as own:
            result = update_facility(facility_id, changes, user=user, db=own)
        _forget_caches()
        return result
    from services.facility_service import MAIN_FACILITY_ID

    row = _facility_row(db, facility_id)
    if row is None:
        raise FacilityIdentityError("No facility has that number.", code="not_found", status=404)
    if not can_edit(user, int(facility_id), db=db):
        raise FacilityIdentityError(
            "You can change only your own facility's details.", code="not_your_facility", status=403
        )
    cleaned, errors = _validated_changes(changes, allow=EDITABLE_FIELDS)
    merged = {**row, **cleaned}
    if int(facility_id) != MAIN_FACILITY_ID and "name" in cleaned and not cleaned["name"]:
        errors.append(
            {
                "field": "name",
                "code": "name_required",
                "message": "Every facility but the first needs a name.",
            }
        )
    if merged.get("gstin") and not merged.get("state_code"):
        merged["state_code"] = merged["gstin"][:2]
        cleaned["state_code"] = merged["state_code"]
    errors.extend(cross_field_problems(merged, _others(db, facility_id)))
    if errors:
        raise FacilityIdentityError(
            "Some details need correcting before they can be saved.",
            code="invalid_identity",
            errors=errors,
        )
    diff = {
        f: {"from": row.get(f), "to": cleaned[f]}
        for f in cleaned
        if (row.get(f) or None) != (cleaned[f] or None)
    }
    if not diff:
        return facility_view(row, user=user, db=db)
    now = _utc_now()
    assignments = ", ".join(f"{f} = ?" for f in diff)
    db.execute(
        f"UPDATE facilities SET {assignments}, updated_at = ?, updated_by = ? WHERE id = ?",
        (*[cleaned[f] for f in diff], now, (user or {}).get("id"), int(facility_id)),
    )
    _audit(
        db,
        action="facility_updated",
        user=user,
        facility=row,
        summary=(
            f"Facility {row.get('code')} details changed: "
            + ", ".join(FIELD_LABELS.get(f, f) for f in diff)
        ),
        metadata={"changes": diff},
    )
    fresh = _facility_row(db, facility_id)
    assert fresh is not None
    return facility_view(fresh, user=user, db=db)


# ═════════════════════════════════════════════════════════════════════════════
#  Growth: the create gate, create and deactivate
# ═════════════════════════════════════════════════════════════════════════════

#: The audit facility index a second facility needs first (built by the
#: maintenance command the design names, never by a migration).
AUDIT_FACILITY_INDEX = "idx_audit_facility"


def can_create_facility(user: dict, *, db=None) -> list[dict]:
    """Why a second facility may not be added now, as [{code, message}]. An
    empty list means it may. While facility_fence holds its sentinel '*'
    this always refuses: no screen has been taught to keep two facilities'
    patients apart yet."""
    if db is None:
        with _db_session() as own:
            return can_create_facility(user, db=own)
    import config
    import facility_fence

    from services import facility_service

    reasons: list[dict] = []

    def no(code: str, message: str) -> None:
        reasons.append({"code": code, "message": message})

    if not config.feature_enabled("multi_site"):
        no("switch_off", "The multi-site switch (ORB_MULTI_SITE) is off on this computer.")
    if not actor_is_group_wide(user, db=db):
        no(
            "no_group_grant",
            "Only an administrator with group-wide access can add a facility. That access "
            "is granted at the computer itself, and every grant is recorded.",
        )
    blind = facility_fence.FACILITY_BLIND_SURFACES
    if blind == facility_fence.SENTINEL:
        no(
            "screens_not_ready",
            "Orb's screens are not yet ready to keep two facilities' patients apart, so "
            "every patient screen would close the moment a second facility exists.",
        )
    elif blind:
        no(
            "screens_not_ready",
            f"{len(blind)} group(s) of screens are not yet ready to keep two facilities' "
            "patients apart.",
        )
    has_index = (
        db.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'index' AND name = ?", (AUDIT_FACILITY_INDEX,)
        ).fetchone()
        is not None
    )
    if not has_index:
        no(
            "audit_index_missing",
            "The audit log has not been indexed by facility yet. That is done once, in a "
            "maintenance window, before a second facility is added.",
        )
    main = _facility_row(db, facility_service.MAIN_FACILITY_ID) or {}
    if not main.get("gstin"):
        no("main_gstin_missing", "Facility 1 has no GSTIN yet. Enter it on its details first.")
    pans = {_effective_pan(r) for r in facility_service.list_facilities(db=db)}
    pans.discard(None)
    if len(pans) > 1:
        no(
            "pan_rule",
            "The facilities on record carry more than one PAN. One computer holds "
            "one legal entity.",
        )
    return reasons


def create_facility(body: dict, *, user: dict, db=None) -> dict:
    """Add a facility. Refused (409, with the reasons) unless can_create_facility
    is empty; then the code (2 to 12 capital letters or digits, unique) and
    the name are required, and every identity rule applies."""
    if db is None:
        with _db_session(immediate=True) as own:
            result = create_facility(body, user=user, db=own)
        _forget_caches()
        return result
    reasons = can_create_facility(user, db=db)
    if reasons:
        raise FacilityIdentityError(
            "A facility cannot be added yet.",
            code="facility_create_refused",
            status=409,
            detail={"reasons": reasons},
        )
    cleaned, errors = _validated_changes(body or {}, allow=EDITABLE_FIELDS + CREATE_ONLY_FIELDS)
    if not cleaned.get("code") and not any(e["field"] == "code" for e in errors):
        errors.append(
            {
                "field": "code",
                "code": "code_required",
                "message": "A new facility needs a code, 2 to 12 capital letters or digits.",
            }
        )
    if not cleaned.get("name"):
        errors.append(
            {"field": "name", "code": "name_required", "message": "A new facility needs a name."}
        )
    if (
        cleaned.get("code")
        and db.execute(
            "SELECT 1 FROM facilities WHERE code = ? COLLATE NOCASE", (cleaned["code"],)
        ).fetchone()
    ):
        errors.append(
            {
                "field": "code",
                "code": "code_taken",
                "message": f"Another facility already uses the code {cleaned['code']}.",
            }
        )
    if cleaned.get("gstin") and not cleaned.get("state_code"):
        cleaned["state_code"] = cleaned["gstin"][:2]
    errors.extend(cross_field_problems(cleaned, _others(db, None)))
    if errors:
        raise FacilityIdentityError(
            "Some details need correcting before the facility can be added.",
            code="invalid_identity",
            errors=errors,
        )
    epoch = db.execute("SELECT MAX(id) FROM audit_logs").fetchone()[0]
    fields = [f for f in cleaned if cleaned[f] is not None]
    now = _utc_now()
    cur = db.execute(
        f"INSERT INTO facilities (uuid, {', '.join(fields)}, audit_epoch_id, is_active, "
        "created_at, created_by) VALUES (?, " + ", ".join("?" for _ in fields) + ", ?, 1, ?, ?)",
        (str(uuid.uuid4()), *[cleaned[f] for f in fields], epoch, now, (user or {}).get("id")),
    )
    row = _facility_row(db, int(cur.lastrowid))
    assert row is not None
    _audit(
        db,
        action="facility_created",
        user=user,
        facility=row,
        summary=f"Facility {row.get('code')} added: {row.get('name')}",
        metadata={"fields": {f: cleaned[f] for f in fields}, "audit_epoch_id": epoch},
    )
    return facility_view(row, user=user, db=db)


def open_work_problems(db, facility_id: int) -> list[dict]:
    """Why a facility cannot be closed yet: active admissions there, and work
    whose facility cannot be told yet (outpatient visits and cash counters
    carry no facility until multisite-4 and 7b), which keeps it open."""
    from services import facility_service

    reasons: list[dict] = []
    n = db.execute(
        "SELECT COUNT(*) FROM admissions WHERE facility_id = ? AND status = 'active'",
        (int(facility_id),),
    ).fetchone()[0]
    if n:
        reasons.append(
            {"code": "active_admissions", "message": f"{n} patient(s) are still admitted there."}
        )
    for table, words, open_sql in (
        ("opd_visits", "outpatient visits", "status IN ('waiting', 'in_consultation')"),
        ("cash_counter_sessions", "cash counters", "status = 'open'"),
    ):
        if not facility_service._table_exists(db, table):
            continue
        cols = facility_service._columns(db, table)
        if "facility_id" not in cols:
            reasons.append(
                {
                    "code": f"{table}_unattributed",
                    "message": f"Orb cannot yet tell which facility {words} belong to, "
                    "so no facility can be closed.",
                }
            )
            continue
        if "status" in cols:
            m = db.execute(
                f"SELECT COUNT(*) FROM {table} WHERE facility_id = ? AND {open_sql}",
                (int(facility_id),),
            ).fetchone()[0]
            if m:
                reasons.append(
                    {"code": f"open_{table}", "message": f"{m} {words} are still open there."}
                )
    return reasons


def deactivate_facility(facility_id: int, *, user: dict, reason: str | None, db=None) -> dict:
    """Close a facility (rows are never deleted). Facility 1 never; otherwise
    only after the create gate passes, with a reason, and with no open work."""
    if db is None:
        with _db_session(immediate=True) as own:
            result = deactivate_facility(facility_id, user=user, reason=reason, db=own)
        _forget_caches()
        return result
    from services.facility_service import MAIN_FACILITY_ID

    if int(facility_id) == MAIN_FACILITY_ID:
        raise FacilityIdentityError(
            "Facility 1 can never be closed.", code="main_facility_permanent", status=409
        )
    reasons = can_create_facility(user, db=db)
    if reasons:
        raise FacilityIdentityError(
            "A facility cannot be closed yet.",
            code="facility_deactivate_refused",
            status=409,
            detail={"reasons": reasons},
        )
    row = _facility_row(db, facility_id)
    if row is None:
        raise FacilityIdentityError("No facility has that number.", code="not_found", status=404)
    if not row.get("is_active"):
        return facility_view(row, user=user, db=db)
    text = " ".join((reason or "").split())
    if not text:
        raise FacilityIdentityError("Say why the facility is being closed.", code="reason_required")
    work = open_work_problems(db, int(facility_id))
    if work:
        raise FacilityIdentityError(
            "The facility still has open work.",
            code="facility_has_open_work",
            status=409,
            detail={"reasons": work},
        )
    now = _utc_now()
    db.execute(
        "UPDATE facilities SET is_active = 0, updated_at = ?, updated_by = ? WHERE id = ?",
        (now, (user or {}).get("id"), int(facility_id)),
    )
    _audit(
        db,
        action="facility_deactivated",
        user=user,
        facility=row,
        summary=f"Facility {row.get('code')} closed: {text}",
        metadata={"reason": text},
    )
    refreshed = _facility_row(db, facility_id)
    assert refreshed is not None
    return facility_view(refreshed, user=user, db=db)


__all__ = [
    "FacilityIdentityError",
    "NOT_CONFIGURED",
    "LEGACY_NOTICE",
    "SNAPSHOT_COLUMNS",
    "SNAPSHOT_COLUMNS_SQL",
    "SNAPSHOT_PLACEHOLDERS",
    "FIELD_LABELS",
    "EDITABLE_FIELDS",
    "GST_STATE_CODES",
    "gstin_check_character",
    "gstin_problem",
    "identity_block",
    "document_facility_id",
    "print_site_name",
    "supplier_snapshot",
    "supplier_block",
    "legacy_supplier_block",
    "can_edit",
    "facility_view",
    "list_views",
    "get_view",
    "update_facility",
    "can_create_facility",
    "create_facility",
    "deactivate_facility",
    "open_work_problems",
    "cross_field_problems",
    "AUDIT_FACILITY_INDEX",
]
