"""Orb Hospital OS — FHIR R4 Export / Write-Back Service

Serialises Orb's own clinical data OUT of the internal SQLite store INTO valid
FHIR R4 resource dicts, so they can be written back into a hospital's EHR. This
is the mirror image of ``services/ingestion_adapters.py`` (FHIR → Orb); coding
conventions (LOINC systems, ``Patient/{id}`` reference formatting, UCUM units)
are kept consistent with that adapter so an Orb export can be round-tripped back
through the ingestion FHIRAdapter without loss.

Provenance & safety
───────────────────
Every exported resource carries a ``meta.tag`` marking it as originating from
Orb (system ``https://orb.health/source``, code ``orb``). Resources derived from
an AI/LLM extraction (rather than a clinician) ALSO carry an AI-draft tag
(system ``https://orb.health/provenance``, code ``ai-draft``) and — for
MedicationRequest — are downgraded so an unverified AI-suggested medication is
NEVER exported as an active order (status ``draft`` / intent ``proposal`` until
a clinician verifies it). This keeps AI-assisted content clearly flagged when it
lands in the receiving EHR.

All functions are read-only. Each accepts an optional ``db`` connection so a
caller (e.g. ``patient_bundle``) can share a single connection across queries;
if none is given the function opens and closes its own.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

from database import get_db

from services.clinical_time import to_utc_iso
from services.medorders import held as held_labels

# ════════════════════════════════════════════
#  Provenance tag scheme
# ════════════════════════════════════════════

# Marks a resource as having been exported from Orb (present on EVERY resource).
ORB_SOURCE_SYSTEM = "https://orb.health/source"
ORB_SOURCE_CODE = "orb"
ORB_SOURCE_DISPLAY = "Exported from Orb Hospital OS"

# Marks a resource as AI-assisted and requiring clinician verification.
ORB_PROVENANCE_SYSTEM = "https://orb.health/provenance"
AI_DRAFT_CODE = "ai-draft"
AI_DRAFT_DISPLAY = "AI-assisted draft: requires clinician verification"

# Provenance source values (medications / patient_allergies / problem_list
# `source` column) that indicate AI/LLM-derived data as opposed to a clinician
# entry or a trusted external EHR feed (`ingested_fhir`). Only these get the
# ai-draft tag — ingested_fhir is real data from another system, not AI output.
_AI_DERIVED_SOURCES = frozenset({"llm_extracted", "agentic", "ai", "ai_extracted", "ai_draft"})


# ════════════════════════════════════════════
#  Coding systems (mirror ingestion_adapters conventions)
# ════════════════════════════════════════════

_LOINC_SYSTEM = "http://loinc.org"
_UCUM_SYSTEM = "http://unitsofmeasure.org"
_OBS_CATEGORY_SYSTEM = "http://terminology.hl7.org/CodeSystem/observation-category"
_ALLERGY_CLINICAL_SYSTEM = "http://terminology.hl7.org/CodeSystem/allergyintolerance-clinical"
_CONDITION_CLINICAL_SYSTEM = "http://terminology.hl7.org/CodeSystem/condition-clinical"
_ICD10_SYSTEM = "http://hl7.org/fhir/sid/icd-10"
_SNOMED_SYSTEM = "http://snomed.info/sct"
_MRN_SYSTEM = "https://orb.health/identifier/mrn"

# Facilities (multisite-15). The Health Facility Registry id goes in the
# namespace ABDM's FHIR profiles give it, typed as a provider number (v2-0203
# PRN); Orb's own facility code is the second identifier. Encounters use the
# v3 ActCode classes for an inpatient stay and an outpatient visit.
_HFR_SYSTEM = "https://facility.ndhm.gov.in"
_FACILITY_CODE_SYSTEM = "https://orb.health/identifier/facility"
_ADMISSION_ID_SYSTEM = "https://orb.health/identifier/admission"
_OPD_VISIT_ID_SYSTEM = "https://orb.health/identifier/opd-visit"
_IDENTIFIER_TYPE_SYSTEM = "http://terminology.hl7.org/CodeSystem/v2-0203"
_ORGANIZATION_TYPE_SYSTEM = "http://terminology.hl7.org/CodeSystem/organization-type"
_ENCOUNTER_CLASS_SYSTEM = "http://terminology.hl7.org/CodeSystem/v3-ActCode"

# admissions.status and opd_visits.status as FHIR Encounter.status.
_ADMISSION_ENCOUNTER_STATUS = {
    "active": "in-progress",
    "discharged": "finished",
    "transferred": "finished",
}
_OPD_ENCOUNTER_STATUS = {
    "waiting": "arrived",
    "in_consultation": "in-progress",
    "completed": "finished",
    "cancelled": "cancelled",
}

# A facility extract (scripts/facility_extract.py) marks its bundles, and says
# when a patient's history could not be attributed to the one facility.
ORB_EXTRACT_SYSTEM = "https://orb.health/facility-extract"
EXTRACT_CODE = "facility-extract"
EXTRACT_DISPLAY = "Facility extract: this facility's encounters only"
GROUP_LEVEL_CODE = "group-level-only"
GROUP_LEVEL_DISPLAY = (
    "This patient also has encounters at another facility, so only this "
    "facility's encounters, the allergies, the problem list and the current "
    "medicines are included"
)

# Maps a `vitals` DB column to (LOINC code, display, human unit, UCUM code).
# LOINC codes are the exact inverse of ingestion_adapters._VITAL_CODE_MAP so an
# exported Observation re-ingests to the same internal vital key.
_VITAL_FHIR: dict[str, tuple[str, str, str, str]] = {
    "heart_rate": ("8867-4", "Heart rate", "/min", "/min"),
    "systolic_bp": ("8480-6", "Systolic blood pressure", "mmHg", "mm[Hg]"),
    "diastolic_bp": ("8462-4", "Diastolic blood pressure", "mmHg", "mm[Hg]"),
    "respiratory_rate": ("9279-1", "Respiratory rate", "/min", "/min"),
    "temperature": ("8310-5", "Body temperature", "°C", "Cel"),
    "spo2": ("2708-6", "Oxygen saturation", "%", "%"),
}
# Deterministic emission order (matches a typical vitals panel).
_VITAL_ORDER = (
    "heart_rate",
    "systolic_bp",
    "diastolic_bp",
    "respiratory_rate",
    "temperature",
    "spo2",
)

# Orb medications.status → FHIR MedicationRequest.status (draft handled separately).
_MED_STATUS_MAP = {"active": "active", "discontinued": "stopped", "prn": "active"}


# ════════════════════════════════════════════
#  Connection helper
# ════════════════════════════════════════════


@contextmanager
def _reader(db=None):
    """Yield a read connection. Reuses a caller-supplied ``db`` (leaving it open)
    or opens and closes a fresh one."""
    if db is not None:
        yield db
    else:
        conn = get_db()
        try:
            yield conn
        finally:
            conn.close()


# ════════════════════════════════════════════
#  Small formatting helpers
# ════════════════════════════════════════════


def _orb_meta(ai_draft: bool = False) -> dict:
    """Build a FHIR ``meta`` element carrying Orb provenance tags.

    Always includes the Orb source tag; adds the ai-draft tag when the resource
    is AI-derived and requires clinician verification.
    """
    tags = [
        {
            "system": ORB_SOURCE_SYSTEM,
            "code": ORB_SOURCE_CODE,
            "display": ORB_SOURCE_DISPLAY,
        }
    ]
    if ai_draft:
        tags.append(
            {
                "system": ORB_PROVENANCE_SYSTEM,
                "code": AI_DRAFT_CODE,
                "display": AI_DRAFT_DISPLAY,
            }
        )
    return {"source": "https://orb.health", "tag": tags}


def _is_ai_source(source: str | None) -> bool:
    """True if a provenance `source` value denotes AI/LLM-derived data."""
    return (source or "").strip().lower() in _AI_DERIVED_SOURCES


def _to_fhir_datetime(value) -> str | None:
    """Best-effort normalise a stored timestamp into a FHIR dateTime string.

    Accepts ISO 8601 (passed through) and ``YYYY-MM-DD HH:MM:SS`` (space →
    ``T``, ``Z`` appended when no zone is present). Returns None for empties.
    """
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    if "T" in s:
        return s
    if " " in s:
        s = s.replace(" ", "T", 1)
        if not (s.endswith("Z") or "+" in s):
            s += "Z"
        return s
    return s  # date-only value


def _sex_to_gender(sex: str | None) -> str:
    """Map Orb `patients.sex` to a FHIR administrative gender code."""
    s = (sex or "").strip().lower()
    if s in ("m", "male"):
        return "male"
    if s in ("f", "female"):
        return "female"
    if s in ("o", "other"):
        return "other"
    return "unknown"


def _name_to_fhir(name: str | None) -> list[dict]:
    """Serialise a single Orb name string into a FHIR HumanName list.

    Keeps the full string in ``text`` (which the ingestion adapter reads first)
    and additionally splits into given/family for downstream EHRs.
    """
    full = (name or "").strip()
    if not full:
        return [{"text": "Unknown"}]
    parts = full.split()
    hn: dict = {"use": "official", "text": full}
    if len(parts) >= 2:
        hn["family"] = parts[-1]
        hn["given"] = parts[:-1]
    else:
        hn["family"] = parts[0]
    return [hn]


def _severity_to_criticality(severity: str | None) -> str:
    """Map Orb allergy severity → FHIR AllergyIntolerance.criticality."""
    s = (severity or "").strip().lower()
    if s == "severe":
        return "high"
    if s in ("mild", "moderate"):
        return "low"
    return "unable-to-assess"


def _clinical_status(system: str, code: str | None, allowed: tuple[str, ...]) -> dict:
    """Build a clinicalStatus CodeableConcept, defaulting to 'active'."""
    c = (code or "active").strip().lower()
    if c not in allowed:
        c = "active"
    return {"coding": [{"system": system, "code": c}]}


# ════════════════════════════════════════════
#  Patient
# ════════════════════════════════════════════


def patient_to_fhir(patient_id: str, db=None) -> dict | None:
    """Serialise a patient row into a FHIR R4 Patient resource.

    Returns None when the patient does not exist.
    """
    with _reader(db) as conn:
        row = conn.execute(
            "SELECT id, name, mrn, sex, date_of_birth FROM patients WHERE id = ?",
            (patient_id,),
        ).fetchone()

    if row is None:
        return None

    resource: dict = {
        "resourceType": "Patient",
        "id": str(row["id"]),
        "meta": _orb_meta(),
        "name": _name_to_fhir(row["name"]),
        "gender": _sex_to_gender(row["sex"]),
    }
    if row["mrn"]:
        resource["identifier"] = [
            {
                "use": "official",
                "system": _MRN_SYSTEM,
                "value": str(row["mrn"]),
            }
        ]
    if row["date_of_birth"]:
        resource["birthDate"] = str(row["date_of_birth"])[:10]
    return resource


# ════════════════════════════════════════════
#  Observations (vitals)
# ════════════════════════════════════════════


def observations_to_fhir(patient_id: str, since: str | None = None, db=None) -> list[dict]:
    """Serialise vitals rows into FHIR R4 Observation resources.

    One Observation is emitted per non-null vital per row (a standards-valid
    alternative to a single BP Observation with components). ``since`` optionally
    restricts to rows with ``timestamp >= since``.

    ``since`` is compared in the stored spelling (services/clinical_time.py),
    so '+05:30', 'Z' and naive (UTC) all mean the instant they name; compared
    as typed, an IST ``since`` filtered 5.5 h too late. Raises ValueError when
    it is not a timestamp, rather than filtering on whatever it sorts like.
    """
    if since:
        try:
            since = to_utc_iso(since)
        except ValueError:
            raise ValueError(f"since is not a timestamp: {since!r}") from None
    with _reader(db) as conn:
        if since:
            rows = conn.execute(
                "SELECT * FROM vitals WHERE patient_id = ? AND timestamp >= ? "
                "AND voided_at IS NULL ORDER BY timestamp DESC, id DESC",
                (patient_id, since),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM vitals WHERE patient_id = ? AND voided_at IS NULL "
                "ORDER BY timestamp DESC, id DESC",
                (patient_id,),
            ).fetchall()

    subject = {"reference": f"Patient/{patient_id}"}
    observations: list[dict] = []
    for row in rows:
        effective = _to_fhir_datetime(row["timestamp"])
        for col in _VITAL_ORDER:
            value = row[col]
            if value is None:
                continue
            loinc, display, unit, ucum = _VITAL_FHIR[col]
            obs: dict = {
                "resourceType": "Observation",
                "id": f"orb-obs-{row['id']}-{loinc}",
                "meta": _orb_meta(),
                "status": "final",
                "category": [
                    {
                        "coding": [
                            {
                                "system": _OBS_CATEGORY_SYSTEM,
                                "code": "vital-signs",
                                "display": "Vital Signs",
                            }
                        ],
                    }
                ],
                "code": {
                    "text": display,
                    "coding": [{"system": _LOINC_SYSTEM, "code": loinc, "display": display}],
                },
                "subject": subject,
                "valueQuantity": {
                    "value": value,
                    "unit": unit,
                    "system": _UCUM_SYSTEM,
                    "code": ucum,
                },
            }
            if effective:
                obs["effectiveDateTime"] = effective
            observations.append(obs)
    return observations


# ════════════════════════════════════════════
#  MedicationRequest
# ════════════════════════════════════════════


def _dosage_instruction(row) -> dict:
    """Build a FHIR Dosage from Orb dosage/route/frequency columns."""
    parts = [str(p) for p in (row["dosage"], row["route"], row["frequency"]) if p]
    dosage: dict = {"text": ", ".join(parts) if parts else "See order"}
    if row["route"]:
        dosage["route"] = {"text": str(row["route"])}
    if row["frequency"]:
        dosage["timing"] = {"code": {"text": str(row["frequency"])}}
    return dosage


def medications_to_fhir(
    patient_id: str, db: Any = None, *, current_only: bool = False
) -> list[dict]:
    """Serialise medication rows into FHIR R4 MedicationRequest resources.

    current_only keeps the rows the ward is giving now (status active or
    prn, held ones included): the facility extract's group-level medicines.

    Provenance rule: an AI-derived (`source` in the AI-derived set) medication
    that has NOT been clinician-verified is exported as a draft (status
    ``draft``, intent ``proposal``) with the ai-draft provenance tag — it is
    never emitted as an active order. Clinician-entered (or clinician-verified)
    meds map their Orb status to the FHIR status normally.

    A held order (medorders-6b; its ward row stays active with held_at set) is
    exported 'on-hold' with the hold reason as statusReason. An unverified
    AI-derived row stays a draft whether held or not.
    """
    with _reader(db) as conn:
        rows = conn.execute(
            "SELECT id, drug_name, dosage, route, frequency, status, source, "
            "verified_by, notes, started_at, held_at, hold_reason FROM medications "
            "WHERE patient_id = ? ORDER BY started_at DESC",
            (patient_id,),
        ).fetchall()
    if current_only:
        rows = [r for r in rows if (r["status"] or "").strip().lower() in ("active", "prn")]

    subject = {"reference": f"Patient/{patient_id}"}
    resources: list[dict] = []
    for row in rows:
        ai_derived = _is_ai_source(row["source"])
        verified = bool(row["verified_by"])
        is_draft = ai_derived and not verified

        if is_draft:
            # Unverified AI-derived med: never emit 'active'. Preserve a stopped
            # state (a discontinued draft is still stopped), otherwise mark draft.
            orb_status = (row["status"] or "active").lower()
            fhir_status = "stopped" if orb_status == "discontinued" else "draft"
            intent = "proposal"
        else:
            fhir_status = _MED_STATUS_MAP.get((row["status"] or "active").lower(), "active")
            intent = "order"
        on_hold = not is_draft and fhir_status == "active" and held_labels.is_held(row)
        if on_hold:
            fhir_status = "on-hold"

        resource: dict = {
            "resourceType": "MedicationRequest",
            "id": f"orb-medreq-{row['id']}",
            "meta": _orb_meta(ai_draft=is_draft),
            "status": fhir_status,
            "intent": intent,
            "medicationCodeableConcept": {"text": row["drug_name"]},
            "subject": subject,
            "dosageInstruction": [_dosage_instruction(row)],
        }
        if on_hold and (row["hold_reason"] or "").strip():
            resource["statusReason"] = {"text": str(row["hold_reason"]).strip()}
        if row["started_at"]:
            resource["authoredOn"] = _to_fhir_datetime(row["started_at"])
        if row["notes"]:
            resource["note"] = [{"text": str(row["notes"])}]
        resources.append(resource)
    return resources


# ════════════════════════════════════════════
#  AllergyIntolerance
# ════════════════════════════════════════════


def allergies_to_fhir(patient_id: str, db=None) -> list[dict]:
    """Serialise patient_allergies rows into FHIR R4 AllergyIntolerance resources."""
    with _reader(db) as conn:
        rows = conn.execute(
            "SELECT id, substance, reaction, severity, status, source, recorded_at "
            "FROM patient_allergies WHERE patient_id = ? ORDER BY recorded_at DESC",
            (patient_id,),
        ).fetchall()

    patient_ref = {"reference": f"Patient/{patient_id}"}
    resources: list[dict] = []
    for row in rows:
        ai_derived = _is_ai_source(row["source"])
        resource: dict = {
            "resourceType": "AllergyIntolerance",
            "id": f"orb-allergy-{row['id']}",
            "meta": _orb_meta(ai_draft=ai_derived),
            "clinicalStatus": _clinical_status(
                _ALLERGY_CLINICAL_SYSTEM,
                row["status"],
                ("active", "inactive", "resolved"),
            ),
            "code": {"text": row["substance"]},
            "patient": patient_ref,
            "criticality": _severity_to_criticality(row["severity"]),
        }
        if row["reaction"]:
            resource["reaction"] = [{"manifestation": [{"text": str(row["reaction"])}]}]
        if row["recorded_at"]:
            resource["recordedDate"] = _to_fhir_datetime(row["recorded_at"])
        resources.append(resource)
    return resources


# ════════════════════════════════════════════
#  Condition (problem list)
# ════════════════════════════════════════════


def conditions_to_fhir(patient_id: str, db=None) -> list[dict]:
    """Serialise problem_list rows into FHIR R4 Condition resources."""
    with _reader(db) as conn:
        rows = conn.execute(
            "SELECT id, problem, icd10_code, snomed_code, status, onset_date, "
            "source, recorded_at FROM problem_list WHERE patient_id = ? "
            "ORDER BY recorded_at DESC",
            (patient_id,),
        ).fetchall()

    subject = {"reference": f"Patient/{patient_id}"}
    resources: list[dict] = []
    for row in rows:
        ai_derived = _is_ai_source(row["source"])
        code: dict = {"text": row["problem"]}
        coding: list[dict] = []
        if row["icd10_code"]:
            coding.append({"system": _ICD10_SYSTEM, "code": str(row["icd10_code"])})
        if row["snomed_code"]:
            coding.append({"system": _SNOMED_SYSTEM, "code": str(row["snomed_code"])})
        if coding:
            code["coding"] = coding

        resource: dict = {
            "resourceType": "Condition",
            "id": f"orb-condition-{row['id']}",
            "meta": _orb_meta(ai_draft=ai_derived),
            "clinicalStatus": _clinical_status(
                _CONDITION_CLINICAL_SYSTEM,
                row["status"],
                ("active", "recurrence", "relapse", "inactive", "remission", "resolved"),
            ),
            "code": code,
            "subject": subject,
        }
        if row["onset_date"]:
            resource["onsetDateTime"] = _to_fhir_datetime(row["onset_date"])
        if row["recorded_at"]:
            resource["recordedDate"] = _to_fhir_datetime(row["recorded_at"])
        resources.append(resource)
    return resources


# ════════════════════════════════════════════
#  Organization and Encounter (multisite-15)
# ════════════════════════════════════════════
#
# One appliance can hold several facilities of a hospital group (ruling MS-R1
# (a)). A bundle then says which facility each stay or visit was at
# (Encounter.serviceProvider), which facility the patient belongs to now
# (Patient.managingOrganization, the rule that decides who may see them) and
# who each facility is (Organization, with its Health Facility Registry id).
# With one facility on record and ORB_MULTI_SITE off (every install today) a
# bundle carries none of this and is exactly as it was.


def facility_context_on(db: Any = None) -> bool:
    """Whether a bundle names facilities: once more than one facility is on
    record, or while ORB_MULTI_SITE is on (so a site can see its own
    Organization in an export before a second facility exists)."""
    import config

    from services import facility_service

    if config.feature_enabled("multi_site"):
        return True
    return bool(facility_service.multi_facility_data(db=db))


def _organization_id(facility_id: int) -> str:
    return f"orb-facility-{int(facility_id)}"


def organization_reference(facility_id: int) -> str:
    """The reference a resource uses to name a facility's Organization."""
    return f"Organization/{_organization_id(facility_id)}"


def _fhir_id(prefix: str, raw: Any) -> str:
    """A FHIR id (letters, digits, '-' and '.', at most 64) for an Orb row id."""
    safe = "".join(c if (c.isascii() and c.isalnum()) or c in "-." else "-" for c in str(raw or ""))
    return f"{prefix}-{safe}"[:64]


def _has_table(conn: Any, table: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
        ).fetchone()
        is not None
    )


def organization_to_fhir(facility_id: int, db: Any = None) -> dict | None:
    """A facility as a FHIR R4 Organization, or None for an id no
    facilities row holds.

    identifier: Orb's facility code always, and the Health Facility Registry
    id when the Facilities screen records one. name is the name every Orb
    print uses (facility 1 without a stored name takes the site settings
    file's). The address and contact are the facility's own identity fields;
    there is nothing about any patient here.
    """
    from services import facility_service

    with _reader(db) as conn:
        row = facility_service.get_facility(int(facility_id), db=conn)
    if row is None:
        return None
    identifiers: list[dict] = []
    code = str(row.get("code") or "").strip()
    if code:
        identifiers.append({"use": "usual", "system": _FACILITY_CODE_SYSTEM, "value": code})
    hfr = str(row.get("hfr_id") or "").strip()
    if hfr:
        identifiers.append(
            {
                "use": "official",
                "type": {
                    "coding": [
                        {
                            "system": _IDENTIFIER_TYPE_SYSTEM,
                            "code": "PRN",
                            "display": "Provider number",
                        }
                    ],
                    "text": "Health Facility Registry ID",
                },
                "system": _HFR_SYSTEM,
                "value": hfr,
            }
        )
    name = facility_service.display_name(int(facility_id), facility=row)
    resource: dict = {
        "resourceType": "Organization",
        "id": _organization_id(int(facility_id)),
        "meta": _orb_meta(),
        "active": bool(row.get("is_active", 1)),
        "type": [
            {
                "coding": [
                    {
                        "system": _ORGANIZATION_TYPE_SYSTEM,
                        "code": "prov",
                        "display": "Healthcare Provider",
                    }
                ]
            }
        ],
        "name": name,
    }
    if identifiers:
        resource["identifier"] = identifiers
    legal = str(row.get("legal_name") or "").strip()
    if legal and legal != name:
        resource["alias"] = [legal]
    lines = [
        str(v).strip()
        for v in (row.get("address_line1"), row.get("address_line2"))
        if str(v or "").strip()
    ]
    address: dict = {}
    if lines:
        address["line"] = lines
    for key, column in (
        ("city", "city"),
        ("district", "district"),
        ("state", "state"),
        ("postalCode", "pincode"),
    ):
        value = str(row.get(column) or "").strip()
        if value:
            address[key] = value
    if address:
        address["country"] = "IN"
        resource["address"] = [address]
    telecom = [
        {"system": system, "value": str(row.get(column)).strip(), "use": "work"}
        for system, column in (("phone", "phone"), ("email", "email"))
        if str(row.get(column) or "").strip()
    ]
    if telecom:
        resource["telecom"] = telecom
    return resource


def patient_facility_id(patient_id: str, db: Any = None) -> int | None:
    """The facility of the patient's latest encounter: the same rule that
    decides who may see them (permissions.FACILITY_PATIENT_SQL). None for an
    id no patient holds."""
    from permissions import FACILITY_PATIENT_SQL

    with _reader(db) as conn:
        row = conn.execute(
            f"SELECT {FACILITY_PATIENT_SQL} AS facility_id FROM patients p WHERE p.id = ?",
            (patient_id,),
        ).fetchone()
    if row is None or row["facility_id"] is None:
        return None
    return int(row["facility_id"])


def _period(start: Any, end: Any) -> dict:
    period: dict = {}
    if _to_fhir_datetime(start):
        period["start"] = _to_fhir_datetime(start)
    if _to_fhir_datetime(end):
        period["end"] = _to_fhir_datetime(end)
    return period


def _service_provider(facility_id: int, conn: Any) -> dict:
    from services import facility_service

    ident = facility_service.facility_identity(int(facility_id), db=conn) or {}
    ref: dict = {"reference": organization_reference(int(facility_id))}
    if ident.get("name"):
        ref["display"] = ident["name"]
    return ref


def encounters_to_fhir(
    patient_id: str, *, facility_id: int | None = None, db: Any = None
) -> list[dict]:
    """The patient's admissions (inpatient stays) and outpatient visits as
    FHIR R4 Encounter resources, each naming its facility as serviceProvider.
    facility_id keeps only the encounters at that facility (the facility
    extract). Stays first, then visits, newest first in each."""
    subject = {"reference": f"Patient/{patient_id}"}
    out: list[dict] = []
    with _reader(db) as conn:
        params: list = [patient_id]
        where = ""
        if facility_id is not None:
            where = " AND facility_id = ?"
            params.append(int(facility_id))
        stays = conn.execute(
            "SELECT id, status, department, ward, bed, created_at, admitted_at, "
            "discharged_at, facility_id FROM admissions WHERE patient_id = ?"
            + where
            + " ORDER BY julianday(COALESCE(admitted_at, created_at)) DESC, rowid DESC",
            params,
        ).fetchall()
        visits = (
            conn.execute(
                "SELECT id, status, department, registered_at, completed_at, "
                "cancelled_at, facility_id FROM opd_visits WHERE patient_id = ?"
                + where
                + " ORDER BY julianday(registered_at) DESC, rowid DESC",
                params,
            ).fetchall()
            if _has_table(conn, "opd_visits")
            else []
        )
        for row in stays:
            fid = int(row["facility_id"] or 1)
            enc: dict = {
                "resourceType": "Encounter",
                "id": _fhir_id("orb-encounter", row["id"]),
                "meta": _orb_meta(),
                "identifier": [{"system": _ADMISSION_ID_SYSTEM, "value": str(row["id"])}],
                "status": _ADMISSION_ENCOUNTER_STATUS.get(
                    str(row["status"] or "").strip().lower(), "unknown"
                ),
                "class": {
                    "system": _ENCOUNTER_CLASS_SYSTEM,
                    "code": "IMP",
                    "display": "inpatient encounter",
                },
                "subject": subject,
                "serviceProvider": _service_provider(fid, conn),
            }
            period = _period(row["admitted_at"] or row["created_at"], row["discharged_at"])
            if period:
                enc["period"] = period
            if row["department"]:
                enc["serviceType"] = {"text": str(row["department"])}
            where_now = " / ".join(str(v) for v in (row["ward"], row["bed"]) if v)
            if where_now:
                enc["location"] = [{"location": {"display": where_now}}]
            out.append(enc)
        for row in visits:
            fid = int(row["facility_id"] or 1)
            enc = {
                "resourceType": "Encounter",
                "id": _fhir_id("orb-opd-visit", row["id"]),
                "meta": _orb_meta(),
                "identifier": [{"system": _OPD_VISIT_ID_SYSTEM, "value": str(row["id"])}],
                "status": _OPD_ENCOUNTER_STATUS.get(
                    str(row["status"] or "").strip().lower(), "unknown"
                ),
                "class": {
                    "system": _ENCOUNTER_CLASS_SYSTEM,
                    "code": "AMB",
                    "display": "ambulatory",
                },
                "subject": subject,
                "serviceProvider": _service_provider(fid, conn),
            }
            period = _period(row["registered_at"], row["completed_at"] or row["cancelled_at"])
            if period:
                enc["period"] = period
            if row["department"]:
                enc["serviceType"] = {"text": str(row["department"])}
            out.append(enc)
    return out


def _organizations(facility_ids: list[int], conn: Any) -> list[dict]:
    out: list[dict] = []
    for fid in facility_ids:
        org = organization_to_fhir(fid, db=conn)
        if org is not None:
            out.append(org)
    return out


def _referenced_facilities(home: int | None, encounters: list[dict]) -> list[int]:
    """Every facility a bundle names, in first-named order."""
    ids: list[int] = []
    if home is not None:
        ids.append(int(home))
    prefix = organization_reference(0)[:-1]
    for enc in encounters:
        ref = str((enc.get("serviceProvider") or {}).get("reference") or "")
        tail = ref[len(prefix) :] if ref.startswith(prefix) else ""
        if tail.isdigit() and int(tail) not in ids:
            ids.append(int(tail))
    return ids


# ════════════════════════════════════════════
#  Bundle ($everything)
# ════════════════════════════════════════════


def _collection(resources: list[dict], meta: dict | None = None) -> dict:
    entries = [
        {
            "fullUrl": f"https://orb.health/fhir/{res.get('resourceType', 'Resource')}/{res.get('id', '')}",
            "resource": res,
        }
        for res in resources
    ]

    return {
        "resourceType": "Bundle",
        "type": "collection",
        "meta": meta if meta is not None else _orb_meta(),
        "timestamp": datetime.now(UTC).isoformat(),
        "entry": entries,
    }


def patient_bundle(
    patient_id: str,
    since: str | None = None,
    db: Any = None,
    *,
    facility_context: bool | None = None,
) -> dict | None:
    """Assemble a FHIR ``collection`` Bundle of everything Orb knows about a
    patient: the Patient plus all Observations, MedicationRequests,
    AllergyIntolerances and Conditions.

    With facility_context (by default: more than one facility on record, or
    ORB_MULTI_SITE on; see facility_context_on) the bundle also carries every
    stay and visit as an Encounter naming its facility, the Organization of
    each facility it names (with the Health Facility Registry id) and the
    patient's managingOrganization. The whole chart, as ruling MS-R2 (a) has
    it: an Encounter at another facility of the group is the patient's too.

    Every entry carries a ``fullUrl`` and every resource carries the Orb source
    provenance tag; AI-derived resources also carry the ai-draft tag. Returns
    None when the patient does not exist.
    """
    with _reader(db) as conn:
        patient = patient_to_fhir(patient_id, db=conn)
        if patient is None:
            return None
        named = facility_context_on(db=conn) if facility_context is None else facility_context
        resources: list[dict] = [patient]
        if named:
            home = patient_facility_id(patient_id, db=conn)
            encounters = encounters_to_fhir(patient_id, db=conn)
            if home is not None:
                patient["managingOrganization"] = {"reference": organization_reference(home)}
            resources.extend(_organizations(_referenced_facilities(home, encounters), conn))
            resources.extend(encounters)
        resources.extend(observations_to_fhir(patient_id, since=since, db=conn))
        resources.extend(medications_to_fhir(patient_id, db=conn))
        resources.extend(allergies_to_fhir(patient_id, db=conn))
        resources.extend(conditions_to_fhir(patient_id, db=conn))

    return _collection(resources)


def facility_extract_bundle(
    patient_id: str, facility_id: int, *, attributable: bool, db: Any = None
) -> dict | None:
    """One patient's bundle for a facility extract (scripts/facility_extract.py,
    a facility leaving the group): the Patient (managed by this facility),
    this facility's Organization and only the encounters at this facility.

    attributable says every encounter the patient has is at this facility.
    Then the observations and the whole medicine history are this facility's
    and are included. Otherwise they cannot be told apart from another
    facility's (clinical rows carry no encounter id), so they are left out
    and the bundle is tagged group-level-only. Either way the allergies, the
    problem list and the current medicines are included whole: they are the
    patient's, never filtered by facility (ruling MS-R2 (a)), because hiding
    them is how a known anaphylaxis is missed at the next site.
    """
    with _reader(db) as conn:
        patient = patient_to_fhir(patient_id, db=conn)
        if patient is None:
            return None
        patient["managingOrganization"] = {"reference": organization_reference(facility_id)}
        resources: list[dict] = [patient]
        resources.extend(_organizations([int(facility_id)], conn))
        resources.extend(encounters_to_fhir(patient_id, facility_id=facility_id, db=conn))
        if attributable:
            resources.extend(observations_to_fhir(patient_id, db=conn))
            resources.extend(medications_to_fhir(patient_id, db=conn))
        else:
            resources.extend(medications_to_fhir(patient_id, db=conn, current_only=True))
        resources.extend(allergies_to_fhir(patient_id, db=conn))
        resources.extend(conditions_to_fhir(patient_id, db=conn))

    meta = _orb_meta()
    meta["tag"].append(
        {"system": ORB_EXTRACT_SYSTEM, "code": EXTRACT_CODE, "display": EXTRACT_DISPLAY}
    )
    if not attributable:
        meta["tag"].append(
            {"system": ORB_EXTRACT_SYSTEM, "code": GROUP_LEVEL_CODE, "display": GROUP_LEVEL_DISPLAY}
        )
    return _collection(resources, meta)
