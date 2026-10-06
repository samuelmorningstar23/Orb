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
from datetime import datetime, timezone
from typing import Optional

from database import get_db
from services.clinical_time import to_utc_iso


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

# Maps a `vitals` DB column to (LOINC code, display, human unit, UCUM code).
# LOINC codes are the exact inverse of ingestion_adapters._VITAL_CODE_MAP so an
# exported Observation re-ingests to the same internal vital key.
_VITAL_FHIR: dict[str, tuple[str, str, str, str]] = {
    "heart_rate":       ("8867-4", "Heart rate", "/min", "/min"),
    "systolic_bp":      ("8480-6", "Systolic blood pressure", "mmHg", "mm[Hg]"),
    "diastolic_bp":     ("8462-4", "Diastolic blood pressure", "mmHg", "mm[Hg]"),
    "respiratory_rate": ("9279-1", "Respiratory rate", "/min", "/min"),
    "temperature":      ("8310-5", "Body temperature", "°C", "Cel"),
    "spo2":             ("2708-6", "Oxygen saturation", "%", "%"),
}
# Deterministic emission order (matches a typical vitals panel).
_VITAL_ORDER = ("heart_rate", "systolic_bp", "diastolic_bp",
                "respiratory_rate", "temperature", "spo2")

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
    tags = [{
        "system": ORB_SOURCE_SYSTEM,
        "code": ORB_SOURCE_CODE,
        "display": ORB_SOURCE_DISPLAY,
    }]
    if ai_draft:
        tags.append({
            "system": ORB_PROVENANCE_SYSTEM,
            "code": AI_DRAFT_CODE,
            "display": AI_DRAFT_DISPLAY,
        })
    return {"source": "https://orb.health", "tag": tags}


def _is_ai_source(source: Optional[str]) -> bool:
    """True if a provenance `source` value denotes AI/LLM-derived data."""
    return (source or "").strip().lower() in _AI_DERIVED_SOURCES


def _to_fhir_datetime(value) -> Optional[str]:
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


def _sex_to_gender(sex: Optional[str]) -> str:
    """Map Orb `patients.sex` to a FHIR administrative gender code."""
    s = (sex or "").strip().lower()
    if s in ("m", "male"):
        return "male"
    if s in ("f", "female"):
        return "female"
    if s in ("o", "other"):
        return "other"
    return "unknown"


def _name_to_fhir(name: Optional[str]) -> list[dict]:
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


def _severity_to_criticality(severity: Optional[str]) -> str:
    """Map Orb allergy severity → FHIR AllergyIntolerance.criticality."""
    s = (severity or "").strip().lower()
    if s == "severe":
        return "high"
    if s in ("mild", "moderate"):
        return "low"
    return "unable-to-assess"


def _clinical_status(system: str, code: Optional[str], allowed: tuple[str, ...]) -> dict:
    """Build a clinicalStatus CodeableConcept, defaulting to 'active'."""
    c = (code or "active").strip().lower()
    if c not in allowed:
        c = "active"
    return {"coding": [{"system": system, "code": c}]}


# ════════════════════════════════════════════
#  Patient
# ════════════════════════════════════════════

def patient_to_fhir(patient_id: str, db=None) -> Optional[dict]:
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
        resource["identifier"] = [{
            "use": "official",
            "system": _MRN_SYSTEM,
            "value": str(row["mrn"]),
        }]
    if row["date_of_birth"]:
        resource["birthDate"] = str(row["date_of_birth"])[:10]
    return resource


# ════════════════════════════════════════════
#  Observations (vitals)
# ════════════════════════════════════════════

def observations_to_fhir(patient_id: str, since: Optional[str] = None, db=None) -> list[dict]:
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
                "category": [{
                    "coding": [{
                        "system": _OBS_CATEGORY_SYSTEM,
                        "code": "vital-signs",
                        "display": "Vital Signs",
                    }],
                }],
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


def medications_to_fhir(patient_id: str, db=None) -> list[dict]:
    """Serialise medication rows into FHIR R4 MedicationRequest resources.

    Provenance rule: an AI-derived (`source` in the AI-derived set) medication
    that has NOT been clinician-verified is exported as a draft (status
    ``draft``, intent ``proposal``) with the ai-draft provenance tag — it is
    never emitted as an active order. Clinician-entered (or clinician-verified)
    meds map their Orb status to the FHIR status normally.
    """
    with _reader(db) as conn:
        rows = conn.execute(
            "SELECT id, drug_name, dosage, route, frequency, status, source, "
            "verified_by, notes, started_at FROM medications "
            "WHERE patient_id = ? ORDER BY started_at DESC",
            (patient_id,),
        ).fetchall()

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
                _ALLERGY_CLINICAL_SYSTEM, row["status"],
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
                _CONDITION_CLINICAL_SYSTEM, row["status"],
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
#  Bundle ($everything)
# ════════════════════════════════════════════

def patient_bundle(patient_id: str, since: Optional[str] = None, db=None) -> Optional[dict]:
    """Assemble a FHIR ``collection`` Bundle of everything Orb knows about a
    patient: the Patient plus all Observations, MedicationRequests,
    AllergyIntolerances and Conditions.

    Every entry carries a ``fullUrl`` and every resource carries the Orb source
    provenance tag; AI-derived resources also carry the ai-draft tag. Returns
    None when the patient does not exist.
    """
    with _reader(db) as conn:
        patient = patient_to_fhir(patient_id, db=conn)
        if patient is None:
            return None
        resources: list[dict] = [patient]
        resources.extend(observations_to_fhir(patient_id, since=since, db=conn))
        resources.extend(medications_to_fhir(patient_id, db=conn))
        resources.extend(allergies_to_fhir(patient_id, db=conn))
        resources.extend(conditions_to_fhir(patient_id, db=conn))

    entries = [{
        "fullUrl": f"https://orb.health/fhir/{res.get('resourceType', 'Resource')}/{res.get('id', '')}",
        "resource": res,
    } for res in resources]

    return {
        "resourceType": "Bundle",
        "type": "collection",
        "meta": _orb_meta(),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "entry": entries,
    }
