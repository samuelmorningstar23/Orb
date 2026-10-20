"""Orb — bulk historical record import (legacy EHR → Orb).

Orb REPLACES the incumbent EHR rather than sitting beside it, so on cutover day
years of existing clinical history have to arrive from a system nobody here
designed, exported by someone who will not be available to answer questions
about it. This module is that door.

It is built on the shape proven by ``services/roster_import.py`` — tolerant
parse, explicit column mapping, dry run first, idempotent apply — and reuses
that module's value-level parsing primitives rather than forking them (see
REUSE below). What is different here is the blast radius. Roster import writes
tens of rows describing who is on the ward today, and a mistake is visible
within a shift. A history import writes hundreds of thousands of rows
describing people who are not in the building, and a mistake is invisible until
a clinician goes looking for something that is silently not there.

Four rules follow from that, and they are the whole design.

═══════════════════════════════════════════════════════════════════════════
 1. IMPORTED HISTORY MUST NOT BECOME LIVE CLINICAL STATE
═══════════════════════════════════════════════════════════════════════════
Roster import exists to make "imported" mean "on the ward board". History
import must mean the exact opposite, and every default in this module is set
that way — because the schema's own defaults all point the dangerous direction:

  * ``patients.status`` defaults to 'admitted'  → imported patients are written
    'discharged' and NO admission is opened. Otherwise a five-year export puts
    thousands of people who are not in the building onto Vigil's patient board
    and the nurse shift board.
  * ``medications.status`` defaults to 'active' → an imported drug with no
    status in the file is written 'discontinued'. ``status='active'`` is read by
    services/drug_safety_service.py for interaction checking, by Sage, and by
    the discharge summary: a drug the patient stopped in 2023 arriving as
    'active' is screened and handed over as a CURRENT medication. That is a
    direct patient-safety hazard, not a display bug.
  * ``problem_list.status`` defaults to 'active' → an imported diagnosis with no
    status in the file is written 'inactive'. Filing it active would assert
    something about the patient's problem list TODAY that the export never
    claimed.
  * an encounter the export marks still open is imported CLOSED and listed
    under ``still_open_in_source``. A current inpatient belongs to roster
    import, which has the ward/bed/department discipline this path does not.

In each case the file's OWN statement wins if it makes one; the safe value is
only the answer to silence, and every row that took it says so in its issues.

═══════════════════════════════════════════════════════════════════════════
 2. PROVENANCE IS CLINICAL
═══════════════════════════════════════════════════════════════════════════
A clinician weighing a record needs to know where it came from. An allergy
typed by our own pharmacist and one migrated out of a legacy free-text field are
not the same evidence. So every row this module writes carries
``import_source_system`` / ``import_external_id`` / ``import_run_id``
(migration 15), and all-NULL keeps its meaning: Orb-native.

The converse matters just as much: this module NEVER stamps import provenance on
a row Orb already held, and NEVER overwrites a value Orb already holds. A
patient matched by MRN keeps their Orb-written name; only NULL holes are filled.
The live record is fresher than a legacy export, and a migration that quietly
reverts a correction a clinician made in Orb is worse than one that fails.

═══════════════════════════════════════════════════════════════════════════
 3. IDEMPOTENCY IS ON A STABLE EXTERNAL ID, AND THE DATABASE ENFORCES IT
═══════════════════════════════════════════════════════════════════════════
Every record type requires ``external_id`` — the old system's own id for that
record. Re-running a file, double-clicking commit, or two admins committing the
same export produces zero duplicates, because (import_source_system,
import_external_id) is UNIQUE per table in the schema, not merely checked in
this module's memory.

Patient identity across two systems is a MAPPING, not a column:
``history_import_patient_xref`` translates the legacy patient id to Orb's, and
is written whether the patient was created by an import or matched to one Orb
already held. Child records (diagnoses, medications, results, documents)
resolve their patient through it, or through MRN, or through both — and when
both are present and disagree, the row FAILS rather than picking one, because
picking one attaches a result to the wrong chart.

═══════════════════════════════════════════════════════════════════════════
 4. DIRTY DATA IS NORMAL: HANDLE IT, REPORT IT, NEVER GUESS A CLINICAL VALUE
═══════════════════════════════════════════════════════════════════════════
Real legacy exports carry three date formats in one column, honorifics in names,
duplicate patients under two MRNs, cp1252 bytes, junk preamble lines and
unquoted commas inside addresses. All of that is handled and reported. What is
never done is invent a clinical value:

  * an ambiguous date (03/04/1990) is left blank and reported, not guessed;
  * a lab value of '<0.01' or 'NEGATIVE' keeps its TEXT and leaves the numeric
    column NULL — coercing it would fabricate a result that trend graphs read;
  * a medication with no dose is recorded with the literal ``UNKNOWN`` rather
    than a plausible number, and rather than being dropped: losing the fact that
    the patient was exposed to the drug is what allergy and interaction history
    exists to prevent. ``UNKNOWN`` is not a guess, it is a recorded refusal to
    guess;
  * a row whose columns have shifted (an unquoted comma) is rejected outright,
    because every value after the break is in the wrong field.

REPORTING. The dry run's per-row detail is CAPPED, because a 200 000-row file
cannot be returned over HTTP. Its COUNTS ARE NOT — ``counts`` and
``issue_summary`` are exact totals over every row read, and truncation is
flagged explicitly. On commit, EVERY rejected row is written to
``history_import_rejects`` with its physical line number and reason. A migration
that silently drops 4% of records is a catastrophe found months later; that
table is the instrument that makes it impossible to not notice.

═══════════════════════════════════════════════════════════════════════════
 MEMORY AND THE EVENT LOOP
═══════════════════════════════════════════════════════════════════════════
Files are STREAMED from disk, never read whole: the route spools the upload to a
temp file and passes a path. Rows are processed in batches of ``BATCH_SIZE``,
with per-batch DB lookups, so peak memory is a batch — not a file. All work is
synchronous and belongs on ``database.run_db``; nothing here may run on the
event loop.

ONE thing is proportional to row count and bounds ``MAX_ROWS``: the set of
external ids seen so far, which is what lets an in-file duplicate be reported
rather than silently written twice. At the 200 000-row cap that is tens of
megabytes. Everything else — the patient lookups, the reject ledger, the
returned detail — is bounded by one batch, one page, or a hard cap. Split a
larger export by year; the importer is idempotent, so files may be loaded in any
order and re-run safely.

The apply path runs in ONE transaction: all-or-nothing. A half-applied migration
is the worst outcome available, so a partially written import is not a state
this module can leave behind.

═══════════════════════════════════════════════════════════════════════════
 REUSE
═══════════════════════════════════════════════════════════════════════════
The value-level parsing helpers imported from ``roster_import`` (_clean,
_parse_date, _parse_int, _parse_sex, _normalise_mrn, _same, Issue, …) are
hard-won behaviour against exactly this class of file, and duplicating them
would mean fixing every dirty-data bug twice. They are private names, so nothing
on roster import's side would catch a breaking edit — that contract is pinned by
a test in tests/test_history_import.py instead.

ENCODING IS DETECTED SEPARATELY, BUT THE TWO IMPORTERS NOW AGREE. Both gate
UTF-16 on an actual byte-order mark and then fall through utf-8-sig → cp1252 →
latin-1. ``roster_import._decode`` used to try UTF-16 first without checking for
a mark, which silently decoded an ordinary even-length cp1252 export into
mojibake; that was fixed on 2026-08-08 and is pinned by four tests in
tests/test_roster_import.py (even-length not read as UTF-16, odd-length decoding
identically to even, a mis-decoded file planning rather than blaming the header,
and a genuine BOM-marked UTF-16 export still decoding).

ONE DELIBERATE DIFFERENCE REMAINS, and it follows from what each function is
handed. ``_detect_encoding`` below reads a 64 KB HEAD, so a multi-byte character
can be sliced by the chunk boundary; it therefore tolerates a UTF-8 decode error
in the final 4 bytes and still calls the file UTF-8. ``roster_import._decode`` is
always given the WHOLE file, where there is no boundary to forgive — a trailing
byte that fails UTF-8 there is genuinely not UTF-8, and must fall through to
cp1252 rather than be replaced. Copying either tolerance into the other would be
wrong: it would make this module reject a valid file, or make roster import
corrupt the last character of a legacy Windows export.

═══════════════════════════════════════════════════════════════════════════
 WHAT THIS DOES NOT DO
═══════════════════════════════════════════════════════════════════════════
It imports CSV only. It does not merge duplicate patients (it reports them), it
does not delete or supersede anything, it has no undo beyond the per-run trail,
and it never creates users — an unknown attending doctor is reported and the
link is left blank.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import os
import re
import uuid
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, NotRequired, TextIO, TypedDict

from database import db_session

from services.roster_import import (
    _NULL_TOKENS,
    IST,
    Issue,
    _clean,
    _norm_header,
    _normalise_mrn,
    _parse_date,
    _parse_int,
    _parse_sex,
    _same,
    _sniff_delimiter,
)

logger = logging.getLogger("orb.history_import")


# ── Limits ───────────────────────────────────────────────────────────────────
# Far larger than roster import's: this is a one-off migration of years of
# history, not a daily ward census. Both are checked before any DB work.
MAX_BYTES = 200 * 1024 * 1024
MAX_ROWS = 200_000
BATCH_SIZE = 500

# How much per-row detail a dry run returns inline. The COUNTS are never capped.
DEFAULT_DETAIL_LIMIT = 200
DEFAULT_REJECT_LIMIT = 2_000

# A header is near the top of the file or it is not a header.
_HEADER_SEARCH_LINES = 100
_MIN_HEADER_HITS = 2

# The recorded refusal to guess. Written into NOT NULL clinical columns whose
# value the export did not carry — never a plausible-looking substitute.
UNKNOWN_VALUE = "UNKNOWN"

# Written to the pre-existing `source` columns on medications / problem_list /
# observations, which other code already reads for provenance. Deliberately NOT
# one of pharmacy_verification_service._UNVERIFIED_SOURCES: a discontinued
# historical drug is not a live order awaiting a pharmacist.
SOURCE_TAG = "history_import"

MIN_AGE, MAX_AGE = 0, 130

# Hex characters of randomness in a generated row id. 16 is not decoration.
#
# The rest of Orb mints ids as `PREFIX-` + ``uuid4().hex[:8]`` — 32 bits. That is
# fine for the paths it was written for, which insert tens of rows: roster import
# puts one ward on the board. It is NOT fine here. By the birthday bound, 32 bits
# collides with probability 25% over 50 000 rows and 99% over the 200 000-row cap
# this module allows — so a large migration was not unlikely to fail, it was
# expected to. Worse, the failure lands at the END of a single all-or-nothing
# transaction: the entire import rolls back after doing all of the work, with an
# opaque UNIQUE-constraint error and no indication which row was at fault.
# (Hit for real on a 50 000-row rehearsal, 2026-08-07.)
#
# 16 hex characters is 64 bits: 1e-9 at the row cap, 3e-8 against a lifetime of a
# million patients. If it ever did collide the transaction aborts having written
# nothing, which is the safe direction.
_ID_HEX = 16


def _new_id(prefix: str) -> str:
    return f"{prefix}{uuid.uuid4().hex[:_ID_HEX].upper()}"


# ══════════════════════════════════════════════════════════════════════════
#  Field + record-type catalogue
# ══════════════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class TargetField:
    name: str
    label: str
    required: bool
    kind: str  # text | int | date | instant | enum | mrn | id | number
    synonyms: tuple[str, ...]
    note: str = ""


def _f(name, label, required, kind, synonyms, note="") -> TargetField:
    return TargetField(name, label, required, kind, tuple(synonyms), note)


# The two identity fields every CHILD record type shares. At least one must be
# mapped; both may be, and then they must agree.
_PATIENT_LINK_FIELDS: tuple[TargetField, ...] = (
    _f(
        "external_patient_id",
        "Patient id in the old system",
        False,
        "id",
        (
            "patient id",
            "patientid",
            "pid",
            "patient no",
            "patient code",
            "legacy patient id",
            "source patient id",
            "his patient id",
        ),
        "Resolved through the cross-system patient map built by the 'patients' import.",
    ),
    _f(
        "mrn",
        "MRN / Hospital number",
        False,
        "mrn",
        (
            "mrn",
            "uhid",
            "mr no",
            "mrno",
            "mr number",
            "hospital number",
            "hosp no",
            "reg no",
            "regno",
            "registration number",
            "ip no",
            "file no",
            "cr no",
        ),
        "Alternative to the old system's patient id. If both are given they must agree.",
    ),
)


@dataclass(frozen=True)
class RecordType:
    name: str
    label: str
    table: str
    fields: tuple[TargetField, ...]
    note: str

    @property
    def by_name(self) -> dict[str, TargetField]:
        return {f.name: f for f in self.fields}

    @property
    def required(self) -> tuple[str, ...]:
        return tuple(f.name for f in self.fields if f.required)


_EXTERNAL_ID_NOTE = (
    "The old system's own id for THIS record. Required: it is what makes a "
    "re-run safe and what lets a clinician trace the row back years later."
)

RECORD_TYPES: dict[str, RecordType] = {}


def _register(rt: RecordType) -> RecordType:
    RECORD_TYPES[rt.name] = rt
    return rt


_register(
    RecordType(
        "patients",
        "Patients (demographics)",
        "patients",
        (
            _f(
                "external_id",
                "Patient id in the old system",
                True,
                "id",
                (
                    "patient id",
                    "patientid",
                    "pid",
                    "patient no",
                    "patient code",
                    "legacy id",
                    "source id",
                    "external id",
                    "his id",
                    "record id",
                ),
                _EXTERNAL_ID_NOTE,
            ),
            _f(
                "mrn",
                "MRN / Hospital number",
                True,
                "mrn",
                (
                    "mrn",
                    "uhid",
                    "mr no",
                    "mrno",
                    "mr number",
                    "hospital number",
                    "hosp no",
                    "reg no",
                    "regno",
                    "registration number",
                    "ip no",
                    "file no",
                    "cr no",
                ),
                "Orb's own natural key, and how staff find a patient. Required: a "
                "patient with no hospital number can never be looked up at the "
                "bedside. If the legacy export genuinely has none, map the legacy "
                "patient id column here as well; that is an explicit admin decision, "
                "which a silently invented MRN would not be.",
            ),
            _f(
                "name",
                "Patient name",
                True,
                "text",
                ("name", "patient name", "patientname", "full name", "pt name"),
                "",
            ),
            _f(
                "date_of_birth",
                "Date of birth",
                False,
                "date",
                ("dob", "date of birth", "birth date", "birthdate", "d o b"),
                "",
            ),
            _f(
                "age",
                "Age (years)",
                False,
                "int",
                ("age", "age years", "age in years", "yrs"),
                "Ignored when date of birth is mapped and parses.",
            ),
            _f(
                "sex",
                "Sex",
                False,
                "enum",
                ("sex", "gender", "m f"),
                "Normalised to M / F / O; anything else is reported, not defaulted.",
            ),
            _f(
                "phone",
                "Phone",
                False,
                "text",
                ("phone", "mobile", "contact", "contact no", "phone no", "cell"),
                "",
            ),
            _f("address", "Address", False, "text", ("address", "addr", "residence"), ""),
            _f(
                "emergency_contact_name",
                "Emergency contact name",
                False,
                "text",
                (
                    "emergency contact",
                    "emergency contact name",
                    "next of kin",
                    "nok",
                    "attendant name",
                ),
                "",
            ),
            _f(
                "emergency_contact_phone",
                "Emergency contact phone",
                False,
                "text",
                ("emergency contact phone", "nok phone", "attendant contact", "emergency phone"),
                "",
            ),
        ),
        "Creates patients as DISCHARGED with no admission. Historical patients are "
        "not in the building. Never overwrites a value Orb already holds.",
    )
)

_register(
    RecordType(
        "admissions",
        "Past admissions (closed encounters)",
        "admissions",
        (
            _f(
                "external_id",
                "Encounter id in the old system",
                True,
                "id",
                (
                    "encounter id",
                    "encid",
                    "admission id",
                    "admissionid",
                    "visit id",
                    "visitid",
                    "ip no",
                    "ipno",
                    "ipd no",
                    "episode id",
                    "case no",
                ),
                _EXTERNAL_ID_NOTE,
            ),
            *_PATIENT_LINK_FIELDS,
            _f(
                "admitted_at",
                "Admission date",
                True,
                "instant",
                (
                    "admission date",
                    "admitdate",
                    "admitted on",
                    "doa",
                    "date of admission",
                    "adm date",
                    "admission datetime",
                ),
                "",
            ),
            _f(
                "discharged_at",
                "Discharge date",
                False,
                "instant",
                (
                    "discharge date",
                    "dischdate",
                    "discharged on",
                    "dod",
                    "date of discharge",
                    "disch date",
                ),
                "Recorded on the admission so length of stay survives the migration.",
            ),
            _f(
                "department",
                "Department / speciality",
                False,
                "text",
                ("department", "dept", "speciality", "specialty", "service", "clinical unit"),
                "The clinical service ('General Medicine'), not the ward ('GM-1'). "
                "Department-scoped boards filter on this.",
            ),
            _f("ward", "Ward", False, "text", ("ward", "ward name", "unit", "location"), ""),
            _f("bed", "Bed", False, "text", ("bed", "bed no", "bedno", "bed number"), ""),
            _f(
                "admission_type",
                "Admission type",
                False,
                "text",
                ("admission type", "type", "admit type", "elective emergency"),
                "",
            ),
            _f(
                "chief_complaint",
                "Chief complaint",
                False,
                "text",
                ("chief complaint", "complaint", "presenting complaint", "reason for admission"),
                "",
            ),
            _f(
                "diagnosis",
                "Diagnosis at admission",
                False,
                "text",
                ("diagnosis", "final diagnosis", "provisional diagnosis", "dx"),
                "",
            ),
            _f(
                "attending_doctor_id",
                "Attending doctor (Orb user id)",
                False,
                "text",
                ("attending", "attending doctor", "consultant", "doctor id", "under care of"),
                "Must be an existing Orb user id; unknown ids are reported, never created.",
            ),
            _f(
                "status",
                "Status in the old system",
                False,
                "enum",
                ("status", "encounter status", "admission status"),
                "A row that says the encounter is still OPEN is imported closed and "
                "listed for the admin, current inpatients belong to roster import.",
            ),
            _f(
                "notes",
                "Admission notes",
                False,
                "text",
                ("notes", "admitting notes", "remarks", "comments"),
                "",
            ),
        ),
        "Closed, past encounters. Never opens a live admission.",
    )
)

_register(
    RecordType(
        "diagnoses",
        "Diagnoses / problems",
        "problem_list",
        (
            _f(
                "external_id",
                "Diagnosis id in the old system",
                True,
                "id",
                (
                    "diagnosis id",
                    "dxid",
                    "dx id",
                    "problem id",
                    "problemid",
                    "record id",
                    "external id",
                ),
                _EXTERNAL_ID_NOTE,
            ),
            *_PATIENT_LINK_FIELDS,
            _f(
                "problem",
                "Diagnosis / problem",
                True,
                "text",
                (
                    "diagnosis",
                    "problem",
                    "condition",
                    "dx",
                    "problem name",
                    "diagnosis description",
                    "disease",
                ),
                "",
            ),
            _f(
                "icd10_code",
                "ICD-10 code",
                False,
                "text",
                ("icd10", "icd 10", "icd10 code", "icd", "icd code"),
                "",
            ),
            _f(
                "snomed_code",
                "SNOMED code",
                False,
                "text",
                ("snomed", "snomed code", "snomed ct"),
                "",
            ),
            _f(
                "status",
                "Status",
                False,
                "enum",
                ("status", "problem status", "diagnosis status", "active"),
                "Normalised to active / resolved / inactive. WITHOUT this column an "
                "imported diagnosis is filed INACTIVE, asserting an old diagnosis as "
                "a current problem is a claim the export never made.",
            ),
            _f(
                "onset_date",
                "Onset date",
                False,
                "date",
                ("onset date", "onsetdate", "onset", "diagnosed on", "date diagnosed"),
                "",
            ),
            _f(
                "resolved_at",
                "Resolved date",
                False,
                "date",
                ("resolved date", "resolved at", "resolution date", "date resolved"),
                "",
            ),
        ),
        "Historical problems. Defaults to INACTIVE unless the file states otherwise.",
    )
)

_register(
    RecordType(
        "medications",
        "Medications",
        "medications",
        (
            _f(
                "external_id",
                "Prescription id in the old system",
                True,
                "id",
                (
                    "prescription id",
                    "rxid",
                    "rx id",
                    "medication id",
                    "medid",
                    "order id",
                    "drug id",
                    "external id",
                ),
                _EXTERNAL_ID_NOTE,
            ),
            *_PATIENT_LINK_FIELDS,
            _f(
                "drug_name",
                "Drug name",
                True,
                "text",
                (
                    "drug",
                    "drug name",
                    "medicine",
                    "medication",
                    "product",
                    "medicine name",
                    "item name",
                ),
                "",
            ),
            _f(
                "dosage",
                "Dose",
                False,
                "text",
                ("dose", "dosage", "strength", "dose given"),
                "Missing → recorded as the literal UNKNOWN. Never invented, and never "
                "a reason to drop the drug: the exposure itself is the clinical fact.",
            ),
            _f(
                "route",
                "Route",
                False,
                "text",
                ("route", "route of administration", "admin route"),
                "Missing → UNKNOWN. Oral and IV are not interchangeable, so no default.",
            ),
            _f(
                "frequency",
                "Frequency",
                False,
                "text",
                ("frequency", "freq", "sig", "dose frequency", "schedule"),
                "Missing → recorded as the literal UNKNOWN.",
            ),
            _f(
                "status",
                "Status",
                False,
                "enum",
                ("status", "drug status", "medication status"),
                "Normalised to active / discontinued / prn. WITHOUT this column an "
                "imported drug is filed DISCONTINUED, 'active' is read by the live "
                "drug-interaction check and by the discharge summary.",
            ),
            _f(
                "started_at",
                "Started on",
                False,
                "instant",
                (
                    "start date",
                    "started on",
                    "startdate",
                    "prescribed on",
                    "date prescribed",
                    "from date",
                ),
                "",
            ),
            _f(
                "notes",
                "Notes",
                False,
                "text",
                ("notes", "remarks", "comments", "instructions"),
                "",
            ),
        ),
        "Historical drugs. Defaults to DISCONTINUED unless the file states otherwise.",
    )
)

_register(
    RecordType(
        "results",
        "Results / observations",
        "observations",
        (
            _f(
                "external_id",
                "Result id in the old system",
                True,
                "id",
                (
                    "result id",
                    "resultid",
                    "lab id",
                    "labid",
                    "observation id",
                    "obsid",
                    "test id",
                    "report id",
                    "external id",
                ),
                _EXTERNAL_ID_NOTE,
            ),
            *_PATIENT_LINK_FIELDS,
            _f(
                "observation_name",
                "Test / observation name",
                True,
                "text",
                (
                    "test",
                    "test name",
                    "observation",
                    "observation name",
                    "analyte",
                    "parameter",
                    "investigation",
                ),
                "",
            ),
            _f(
                "value",
                "Result value",
                False,
                "text",
                ("value", "result", "result value", "observation value", "reading"),
                "Kept verbatim. Parsed into the numeric column only when the whole "
                "cell is a clean number, '<0.01' and 'NEGATIVE' keep their text and "
                "leave the number NULL rather than being coerced into a fake result.",
            ),
            _f("unit", "Unit", False, "text", ("unit", "units", "uom"), ""),
            _f(
                "reference_range",
                "Reference range",
                False,
                "text",
                (
                    "reference range",
                    "ref range",
                    "normal range",
                    "range",
                    "biological reference interval",
                ),
                "",
            ),
            _f(
                "abnormal_flag",
                "Abnormal flag",
                False,
                "text",
                ("abnormal flag", "flag", "abnormal", "h l", "interpretation"),
                "",
            ),
            _f(
                "observed_at",
                "Observed / collected at",
                False,
                "instant",
                (
                    "observed at",
                    "observedat",
                    "collected",
                    "collection date",
                    "sample date",
                    "result date",
                    "report date",
                    "date",
                    "datetime",
                ),
                "",
            ),
            _f(
                "observation_code",
                "Code (LOINC or local)",
                False,
                "text",
                ("loinc", "loinc code", "test code", "code", "observation code"),
                "",
            ),
            _f(
                "observation_code_system",
                "Code system",
                False,
                "text",
                ("code system", "coding system", "terminology"),
                "",
            ),
            _f(
                "panel_name",
                "Panel",
                False,
                "text",
                ("panel", "panel name", "profile", "group", "department test"),
                "",
            ),
            _f(
                "status",
                "Status",
                False,
                "enum",
                ("status", "result status"),
                "Normalised to final / preliminary / corrected.",
            ),
        ),
        "Historical results. Numeric values are never inferred from non-numeric text.",
    )
)

_register(
    RecordType(
        "documents",
        "Clinical documents",
        "clinical_documents",
        (
            _f(
                "external_id",
                "Document id in the old system",
                True,
                "id",
                (
                    "document id",
                    "docid",
                    "doc id",
                    "note id",
                    "report id",
                    "record id",
                    "external id",
                ),
                _EXTERNAL_ID_NOTE,
            ),
            *_PATIENT_LINK_FIELDS,
            _f(
                "title",
                "Title",
                True,
                "text",
                ("title", "document title", "subject", "heading", "name"),
                "",
            ),
            _f(
                "content",
                "Content",
                True,
                "text",
                ("content", "body", "text", "note", "document text", "narrative", "report text"),
                "An empty document is not a document, such rows are reported, not imported.",
            ),
            _f(
                "doc_type",
                "Document type",
                False,
                "text",
                ("doc type", "document type", "type", "category", "note type"),
                "Missing → 'imported_document', which is honest about what it is.",
            ),
            _f(
                "authored_at",
                "Authored at",
                False,
                "instant",
                (
                    "authored at",
                    "date",
                    "created",
                    "created at",
                    "document date",
                    "note date",
                    "report date",
                ),
                "",
            ),
            _f(
                "author_id",
                "Author (Orb user id)",
                False,
                "text",
                ("author", "author id", "doctor", "created by", "consultant"),
                "Must be an existing Orb user id; unknown ids are reported, never created.",
            ),
        ),
        "Historical notes, summaries and reports as free text.",
    )
)


def record_type_catalogue() -> list[dict]:
    """Serialisable catalogue of every record type — drives the UI's first step."""
    return [
        {
            "name": rt.name,
            "label": rt.label,
            "table": rt.table,
            "note": rt.note,
            "required": list(rt.required),
        }
        for rt in RECORD_TYPES.values()
    ]


def field_catalogue(record_type: str) -> list[dict]:
    """Serialisable target-field catalogue for one record type."""
    rt = _record_type(record_type)
    return [
        {"name": f.name, "label": f.label, "required": f.required, "kind": f.kind, "note": f.note}
        for f in rt.fields
    ]


def _record_type(name: str) -> RecordType:
    rt = RECORD_TYPES.get((name or "").strip())
    if rt is None:
        raise ValueError(f"Unknown record type {name!r}. Expected one of: {sorted(RECORD_TYPES)}.")
    return rt


# ══════════════════════════════════════════════════════════════════════════
#  Value normalisation beyond what roster_import already provides
# ══════════════════════════════════════════════════════════════════════════

# Honorifics an Indian HIS routinely stores inside the name field. Detected and
# REPORTED, never stripped: the name is a bedside identity check and rewriting
# it is not this module's call.
_HONORIFICS = {
    "mr",
    "mrs",
    "ms",
    "miss",
    "dr",
    "shri",
    "smt",
    "sri",
    "kum",
    "master",
    "baby",
    "b o",
    "prof",
    "late",
}

_DIAGNOSIS_STATUS = {"active", "resolved", "inactive"}
_DIAGNOSIS_SYNONYMS = {
    "active": "active",
    "current": "active",
    "ongoing": "active",
    "open": "active",
    "chronic": "active",
    "confirmed": "active",
    "resolved": "resolved",
    "cured": "resolved",
    "closed": "resolved",
    "recovered": "resolved",
    "healed": "resolved",
    "inactive": "inactive",
    "past": "inactive",
    "historical": "inactive",
    "history of": "inactive",
    "h o": "inactive",
    "old": "inactive",
}

_MED_STATUS = {"active", "discontinued", "prn"}
_MED_SYNONYMS = {
    "active": "active",
    "ongoing": "active",
    "continuing": "active",
    "current": "active",
    "running": "active",
    "discontinued": "discontinued",
    "stopped": "discontinued",
    "completed": "discontinued",
    "ceased": "discontinued",
    "dc": "discontinued",
    "d c": "discontinued",
    "cancelled": "discontinued",
    "held": "discontinued",
    "finished": "discontinued",
    "closed": "discontinued",
    "inactive": "discontinued",
    "prn": "prn",
    "sos": "prn",
    "as required": "prn",
    "as needed": "prn",
    "when required": "prn",
}

_RESULT_STATUS = {"final", "preliminary", "corrected"}
_RESULT_SYNONYMS = {
    "final": "final",
    "f": "final",
    "complete": "final",
    "completed": "final",
    "authorised": "final",
    "authorized": "final",
    "verified": "final",
    "preliminary": "preliminary",
    "prelim": "preliminary",
    "p": "preliminary",
    "partial": "preliminary",
    "pending": "preliminary",
    "corrected": "corrected",
    "c": "corrected",
    "amended": "corrected",
    "revised": "corrected",
}

# Encounter statuses that mean "this patient has NOT left" — out of contract for
# a history import.
_OPEN_ENCOUNTER_TOKENS = {
    "active",
    "open",
    "admitted",
    "inpatient",
    "ip",
    "current",
    "ongoing",
    "in progress",
    "occupied",
}


def _check_name(value: str, line: int) -> list[Issue]:
    """Name quality checks. Never rewrites the name."""
    issues: list[Issue] = []
    first = re.split(r"[\s.]+", value.strip().lower())[0] if value.strip() else ""
    if first and first.rstrip(".") in _HONORIFICS:
        issues.append(
            Issue(
                line,
                "honorific_in_name",
                "warning",
                f"name '{value}' begins with the honorific '{first}'. Imported VERBATIM: "
                f"Orb will not edit a name, because the name is what a bedside identity "
                f"check reads. Clean it in the export if the honorific is not wanted.",
                "name",
                value,
            )
        )
    if not re.search(r"[A-Za-zऀ-ॿ]", value):
        issues.append(
            Issue(
                line,
                "name_without_letters",
                "warning",
                f"name '{value}' contains no letters, imported verbatim; confirm it against "
                f"the old system.",
                "name",
                value,
            )
        )
    return issues


def _parse_enum(
    value: str,
    line: int,
    fname: str,
    valid: set[str],
    synonyms: dict[str, str],
    fallback: str,
    fallback_code: str,
) -> tuple[str | None, list[Issue]]:
    """Normalise one coded value. An unrecognised token is REPORTED, and the
    caller's safe fallback is used with the reason named."""
    if not value:
        return None, []
    key = re.sub(r"[^a-z0-9]+", " ", value.strip().lower()).strip()
    if key in valid:
        return key, []
    if key in synonyms:
        return synonyms[key], []
    return None, [
        Issue(
            line,
            fallback_code,
            "warning",
            f"{fname} '{value}' was not recognised, so it was filed as '{fallback}', the "
            f"safe reading. Check this row against the old system.",
            fname,
            value,
        )
    ]


_TIME_RE = re.compile(r"[ T](\d{1,2}):(\d{2})(?::(\d{2}))?\s*([AaPp][Mm])?\s*$")


def _parse_instant(
    value: str, fname: str, line: int, date_order: str
) -> tuple[str | None, list[Issue]]:
    """Parse a legacy date (optionally with a clock time) into UTC ISO-8601.

    The hospital is in India and the columns Orb writes are UTC, so a bare date
    is anchored at IST midnight — stamping UTC midnight would file a patient
    admitted at 02:00 IST as having arrived the previous day, and would shift
    every historical result five and a half hours earlier.

    The DATE half is delegated to roster_import._parse_date, which already
    refuses two-digit years and genuinely ambiguous D/M vs M/D values.
    """
    if not value:
        return None, []
    hour = minute = second = 0
    has_time = False
    m = _TIME_RE.search(value.strip())
    if m:
        hour, minute = int(m.group(1)), int(m.group(2))
        second = int(m.group(3) or 0)
        meridiem = (m.group(4) or "").lower()
        if meridiem == "pm" and hour < 12:
            hour += 12
        elif meridiem == "am" and hour == 12:
            hour = 0
        has_time = 0 <= hour <= 23 and 0 <= minute <= 59 and 0 <= second <= 59
        if not has_time:
            hour = minute = second = 0

    iso_date, issues = _parse_date(value, fname, line, date_order)
    if not iso_date:
        return None, issues
    d = date.fromisoformat(iso_date)
    stamp = datetime(d.year, d.month, d.day, hour, minute, second, tzinfo=IST)
    return stamp.astimezone(UTC).isoformat(), issues


def _parse_result_value(raw: str, line: int) -> tuple[str | None, float | None, list[Issue]]:
    """Split a lab value into (text, numeric, issues).

    The text is ALWAYS the verbatim cell. The numeric column is filled only when
    the entire cell is a clean number, because it is what trend graphs and
    thresholds read: turning '<0.01' into 0.01 invents a result that is
    quantitatively wrong and clinically consequential, and turning 'NEGATIVE'
    into 0 invents one out of nothing.
    """
    if not raw:
        return None, None, []
    text = raw.strip()
    try:
        return text, float(text.replace(",", "")), []
    except (TypeError, ValueError):
        pass
    return (
        text,
        None,
        [
            Issue(
                line,
                "non_numeric_value",
                "warning",
                f"result value '{text}' is not a plain number, so it was stored as TEXT with no "
                f"numeric value. Orb will not convert it, '<0.01' is not 0.01 and 'NEGATIVE' is "
                f"not 0, so this result will not appear on numeric trends.",
                "value",
                text,
            )
        ],
    )


# ══════════════════════════════════════════════════════════════════════════
#  Source — bytes or a path on disk, always streamed
# ══════════════════════════════════════════════════════════════════════════


class _Source:
    """An uploaded export, readable as text more than once without buffering it.

    ``bytes`` is content (tests, small files). ``str``/``Path`` is a filesystem
    path, which is how the route passes a large upload: the bytes are spooled to
    a temp file and never held in memory. The distinction is by TYPE and not by
    guessing whether a string looks path-shaped.
    """

    def __init__(self, raw: bytes | bytearray | str | os.PathLike):
        self.issues: list[Issue] = []
        self.path: Path | None = None
        self._text: str | None = None

        if isinstance(raw, (bytes, bytearray)):
            if len(raw) > MAX_BYTES:
                raise ValueError(f"File exceeds the {MAX_BYTES // (1024 * 1024)} MB limit")
            self.size = len(raw)
            self.sha256 = hashlib.sha256(raw).hexdigest()
            self.encoding, self.issues = _detect_encoding(bytes(raw))
            self._text = bytes(raw).decode(self.encoding, errors="replace")
            if not self._text.strip():
                raise ValueError("File is empty")
            return

        path = Path(raw)
        if not path.is_file():
            raise ValueError(f"No such file: {path}")
        self.size = path.stat().st_size
        if self.size == 0:
            raise ValueError("File is empty")
        if self.size > MAX_BYTES:
            raise ValueError(f"File exceeds the {MAX_BYTES // (1024 * 1024)} MB limit")
        self.path = path

        digest = hashlib.sha256()
        with path.open("rb") as fh:
            head = b""
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                digest.update(chunk)
                if len(head) < 65536:
                    head += chunk[: 65536 - len(head)]
        self.sha256 = digest.hexdigest()
        # Decide the encoding from the head only — the point of a path source is
        # never to hold the whole file.
        self.encoding, self.issues = _detect_encoding(head)

    def open_text(self) -> TextIO:
        if self._text is not None:
            return io.StringIO(self._text)
        assert self.path is not None
        return self.path.open("r", encoding=self.encoding, errors="replace", newline="")

    def head_lines(self, limit: int) -> list[str]:
        out: list[str] = []
        with self.open_text() as fh:
            for line in fh:
                out.append(line.rstrip("\r\n"))
                if len(out) >= limit:
                    break
        return out


def _detect_encoding(head: bytes) -> tuple[str, list[Issue]]:
    """Pick a text encoding from the first bytes of the file.

    UTF-16 IS ONLY CONSIDERED WHEN THERE IS A BYTE-ORDER MARK, and that is the
    whole subtlety here. Python's 'utf-16' codec without a BOM assumes
    little-endian and raises only on an odd byte count or an unpaired surrogate,
    so an ordinary cp1252 CSV of even length DECODES SUCCESSFULLY into CJK
    mojibake — 'PatientID,MRN,Name' comes back as '慐楴湥䥴ⱄ前ⱎ慎敭'. Trying
    utf-16 ahead of cp1252 without a BOM check therefore does not fail loudly on
    a legacy Windows export; it silently corrupts every name, MRN and date in
    the file and hands the importer a header row it cannot recognise. (Verified
    against a cp1252 fixture 2026-08-07 — it is a coin flip on file length.)

    Order after that is deliberate: UTF-8 first (the common case), then cp1252
    (what a Windows-era Indian HIS actually emits), then latin-1, which cannot
    fail and so is the terminal fallback.
    """
    if head[:2] in (b"\xff\xfe", b"\xfe\xff") or head[:4] in (
        b"\xff\xfe\x00\x00",
        b"\x00\x00\xfe\xff",
    ):
        return "utf-16", [
            Issue(
                None,
                "non_utf8_encoding",
                "warning",
                "File is UTF-16 (it carries a byte-order mark). Check that "
                "non-Latin names came through correctly.",
            )
        ]

    try:
        head.decode("utf-8-sig")
        return "utf-8-sig", []
    except UnicodeDecodeError as exc:
        # A 64 KB window can slice a multi-byte character in half. A failure in
        # the last few bytes is that, not a non-UTF-8 file.
        if exc.start >= len(head) - 4:
            return "utf-8-sig", []

    for encoding in ("cp1252", "latin-1"):
        try:
            head.decode(encoding)
        except (UnicodeDecodeError, UnicodeError):
            continue
        return encoding, [
            Issue(
                None,
                "non_utf8_encoding",
                "warning",
                f"File is not UTF-8; decoded as {encoding}. Check that "
                f"non-Latin names came through correctly, an export in the "
                f"wrong encoding is how a patient's name arrives mangled.",
            )
        ]

    return "latin-1", [
        Issue(
            None,
            "undecodable_bytes",
            "warning",
            "File contained bytes that are not valid in any tried encoding; "
            "they were replaced. Names may be corrupted.",
        )
    ]


# ══════════════════════════════════════════════════════════════════════════
#  Header detection + mapping suggestion (per record type)
# ══════════════════════════════════════════════════════════════════════════


def suggest_mapping(headers: Iterable[str], record_type: str) -> dict[str, str]:
    """Suggest ``{orb_field: csv_header}`` from header synonyms.

    A SUGGESTION only — nothing is imported until an admin confirms it. Exact
    synonym hits win over substring hits, and each CSV header is offered to at
    most one Orb field, so 'emergency contact phone' cannot also land on 'phone'.
    Fields are tried in catalogue order, which is why ``external_id`` claims a
    'Patient ID' column ahead of ``mrn``.
    """
    rt = _record_type(record_type)
    normalised = [(h, _norm_header(h)) for h in headers if h is not None]
    mapping: dict[str, str] = {}
    taken: set[str] = set()
    for exact_pass in (True, False):
        for f in rt.fields:
            if f.name in mapping:
                continue
            for original, norm in normalised:
                if original in taken or not norm:
                    continue
                hit = (
                    (norm in f.synonyms)
                    if exact_pass
                    else any(s in norm or norm in s for s in f.synonyms if len(s) > 3)
                )
                if hit:
                    mapping[f.name] = original
                    taken.add(original)
                    break
    return mapping


def _exact_header_hits(fields: Iterable[str], rt: RecordType) -> tuple[str, ...]:
    normalised = [_norm_header(f) for f in fields]
    hits: list[str] = []
    taken: set[int] = set()
    for target in rt.fields:
        for i, norm in enumerate(normalised):
            if i in taken or not norm:
                continue
            if norm in target.synonyms:
                hits.append(target.name)
                taken.add(i)
                break
    return tuple(hits)


class _HeaderCandidate(TypedDict):
    line: int
    text: str
    fields: NotRequired[list[str]]
    recognised_columns: NotRequired[list[str]]
    possible_columns: NotRequired[list[str]]


@dataclass
class _HeaderLocation:
    index: int  # 0-based physical line index
    fields: list[str]
    delimiter: str
    confident: bool
    candidates: list[_HeaderCandidate]
    issues: list[Issue]


def _split(line: str, delimiter: str) -> list[str]:
    return [p.strip() for p in next(csv.reader(io.StringIO(line), delimiter=delimiter), [])]


def _locate_header(source: _Source, rt: RecordType, header_line: int | None) -> _HeaderLocation:
    """Find the column-header row, skipping a junk preamble.

    Getting this wrong is not cosmetic: read a report title as the header and
    EVERY column lands in the wrong field, so one patient's ward becomes
    another's diagnosis and the import reports success. The bar is therefore
    evidence, not optimism — a line is the header only if it names at least
    ``_MIN_HEADER_HITS`` columns this record type recognises EXACTLY. When
    nothing clears it, the caller is expected to refuse and ask.
    """
    lines = source.head_lines(_HEADER_SEARCH_LINES)
    if not any(line.strip() for line in lines):
        raise ValueError("File is empty")
    delimiter = _sniff_delimiter("\n".join(lines[:20]))
    issues: list[Issue] = []

    if header_line is not None:
        idx = int(header_line) - 1
        all_lines = source.head_lines(max(idx + 1, _HEADER_SEARCH_LINES))
        if idx < 0 or idx >= len(all_lines) or not all_lines[idx].strip():
            raise ValueError(
                f"header_line {header_line} is not a usable line: it is out of range or blank."
            )
        fields = _split(all_lines[idx], delimiter)
        named = sum(1 for f in fields if f.strip())
        if named < 2:
            raise ValueError(
                f"header_line {header_line} splits into {named} named column(s) under "
                f"delimiter '{delimiter}'. That cannot be a header row."
            )
        if not _exact_header_hits(fields, rt) and not suggest_mapping(fields, rt.name):
            issues.append(
                Issue(
                    header_line,
                    "header_line_unrecognised",
                    "warning",
                    f"Line {header_line} was used as the header on your instruction, but none of "
                    f"its {named} column name(s) match anything Orb recognises for "
                    f"'{rt.name}': check the mapping column by column before committing.",
                )
            )
        if idx > 0:
            issues.append(_preamble_issue(idx))
        return _HeaderLocation(idx, fields, delimiter, True, [], issues)

    candidates: list[_HeaderCandidate] = []
    chosen: int | None = None
    for i, line in enumerate(lines):
        if not line.strip():
            continue
        fields = _split(line, delimiter)
        if sum(1 for f in fields if f.strip()) < 2:
            continue
        hits = _exact_header_hits(fields, rt)
        loose = tuple(suggest_mapping(fields, rt.name))
        candidates.append(
            {
                "line": i + 1,
                "text": line[:200],
                "fields": fields,
                "recognised_columns": list(hits),
                "possible_columns": list(loose),
            }
        )
        if chosen is None and len(set(hits)) >= _MIN_HEADER_HITS:
            chosen = i

    if chosen is not None:
        if chosen > 0:
            issues.append(_preamble_issue(chosen))
        return _HeaderLocation(
            chosen, _split(lines[chosen], delimiter), delimiter, True, candidates[:5], issues
        )

    best: _HeaderCandidate = candidates[0] if candidates else {"line": 1, "text": lines[0][:200]}
    issues.append(
        Issue(
            best["line"],
            "header_ambiguous",
            "error",
            f"No line in this file clearly looks like a column-header row for record type "
            f"'{rt.name}': a header is expected to name at least {_MIN_HEADER_HITS} columns Orb "
            f"recognises. Reading the wrong line as the header shifts EVERY column into the "
            f"wrong field, so Orb will not guess. Best candidate is line {best['line']}: "
            f"{best['text']!r}. Re-run with header_line set to the 1-based line number that "
            f"holds the column names.",
        )
    )
    return _HeaderLocation(
        best["line"] - 1,
        _split(lines[best["line"] - 1], delimiter),
        delimiter,
        False,
        candidates[:5],
        issues,
    )


def _preamble_issue(header_index: int) -> Issue:
    return Issue(
        header_index,
        "preamble_skipped",
        "warning",
        f"{header_index} line(s) before the header row were ignored "
        f"(they did not look like column headers).",
    )


# ══════════════════════════════════════════════════════════════════════════
#  Row streaming
# ══════════════════════════════════════════════════════════════════════════


def _iter_rows(
    source: _Source, header: _HeaderLocation
) -> Iterator[tuple[int, dict, Issue | None, str]]:
    """Stream data rows as (physical_line, {header: value}, rejection, raw_text).

    Generator, deliberately: this is what keeps peak memory at one row rather
    than one file. Blank and ragged rows are reported, never dropped silently,
    and line numbers are 1-based physical lines so an admin can open the export
    and look straight at the offending row.

    A row with MORE fields than the header and a NON-BLANK surplus is REJECTED,
    never imported — the same rule roster import proved. A surplus field is
    indistinguishable from an unquoted delimiter inside a value, and Indian
    addresses carry commas constantly; every column after the break is then one
    place to the left, which files a result under the wrong test and the wrong
    date while reporting success. Truncating the surplus does not help: the
    damage is the SHIFT, not the overflow. Too FEW fields is different and stays
    a warning — nothing shifts, the absent trailing columns simply read blank.

    LINE NUMBERS COME FROM ``reader.line_num``, NOT FROM COUNTING RECORDS. A
    clinical document is the record type most likely to contain a properly
    quoted field with newlines inside it — a discharge note IS multi-line — and
    one such row makes a per-record counter drift permanently, so every line
    number reported after it points at the wrong row of the file. Since the
    whole promise of the reject ledger is "open the export at this line", a
    number that quietly stops meaning that is worse than none.
    """
    headers = header.fields
    width = len(headers)
    delimiter = header.delimiter
    emitted = 0

    with source.open_text() as fh:
        for _ in range(header.index + 1):  # skip preamble + the header line
            if fh.readline() == "":
                return
        reader = csv.reader(fh, delimiter=delimiter)
        base = header.index + 1  # physical lines already consumed
        consumed = 0
        for parts in reader:
            line_no = base + consumed + 1  # FIRST physical line of this record
            consumed = reader.line_num
            if not any(p.strip() for p in parts):
                yield line_no, {}, None, ""
                continue
            if emitted >= MAX_ROWS:
                yield (
                    line_no,
                    {},
                    Issue(
                        line_no,
                        "row_limit",
                        "error",
                        f"File exceeds the {MAX_ROWS}-row limit; rows from line {line_no} onward "
                        f"were NOT read. Split the export (by year, say) and re-run, the import "
                        f"is idempotent, so the files can be loaded in any order.",
                    ),
                    "",
                )
                return

            raw_text = delimiter.join(parts)[:500]
            rejection: Issue | None = None
            if len(parts) > width:
                surplus = [p.strip() for p in parts[width:] if p.strip()]
                if surplus:
                    rejection = Issue(
                        line_no,
                        "field_count_mismatch",
                        "error",
                        f"Row has {len(parts)} fields but the header has {width}, and the "
                        f"surplus value(s) {surplus[:3]} are not empty, so Orb cannot tell "
                        f"which column each value belongs to. The usual cause is an unquoted "
                        f"'{delimiter}' inside a field (an address such as '12 Nehru Street, "
                        f"New Delhi'), which shifts every later column one place. The row was "
                        f"NOT imported. Re-export with fields quoted, or fix this row.",
                        value=raw_text,
                    )
            row = {headers[i]: (parts[i] if i < len(parts) else "") for i in range(width)}
            emitted += 1
            yield line_no, row, rejection, raw_text


# ══════════════════════════════════════════════════════════════════════════
#  Accumulator — exact counts, capped detail
# ══════════════════════════════════════════════════════════════════════════


@dataclass
class _PlannedRow:
    line: int
    external_id: str | None = None
    external_patient_id: str | None = None
    mrn: str | None = None
    action: str = "pending"  # create | update | unchanged | fail
    reason: str = ""
    patient_id: str | None = None
    target_id: str | None = None
    values: dict[str, Any] = field(default_factory=dict)
    changes: dict[str, dict] = field(default_factory=dict)
    issues: list[Issue] = field(default_factory=list)
    raw_text: str = ""
    # multisite-4: the facility an admission this row creates is at, set by
    # _check_facility only while more than one facility is on record.
    facility_id: int | None = None

    def fail(
        self, code: str, message: str, field_name: str | None = None, value: str | None = None
    ) -> _PlannedRow:
        self.action = "fail"
        self.reason = code
        self.issues.append(Issue(self.line, code, "error", message, field_name, value))
        return self

    def to_dict(self) -> dict:
        return {
            "line": self.line,
            "external_id": self.external_id,
            "external_patient_id": self.external_patient_id,
            "mrn": self.mrn,
            "action": self.action,
            "reason": self.reason,
            "patient_id": self.patient_id,
            "target_id": self.target_id,
            "values": self.values,
            "changes": self.changes,
            "issues": [i.to_dict() for i in self.issues],
        }


class _Report:
    """Exact totals, capped detail. The counts are the safety instrument."""

    def __init__(self, detail_limit: int, reject_limit: int):
        self.detail_limit = max(0, int(detail_limit))
        self.reject_limit = max(0, int(reject_limit))
        self.counts = {
            "total_rows": 0,
            "create": 0,
            "update": 0,
            "unchanged": 0,
            "failed": 0,
            "warnings": 0,
        }
        self.issue_summary: dict[str, int] = {}
        self.rows: list[dict] = []
        self.failures: list[dict] = []
        self.rows_truncated = False
        self.failures_truncated = False
        self.still_open_in_source: list[dict] = []
        self.blank_rows = 0

    def add(self, pr: _PlannedRow) -> None:
        self.counts["total_rows"] += 1
        if pr.action == "fail":
            self.counts["failed"] += 1
        else:
            self.counts[pr.action] = self.counts.get(pr.action, 0) + 1
        for issue in pr.issues:
            self.issue_summary[issue.code] = self.issue_summary.get(issue.code, 0) + 1
            if issue.severity == "warning":
                self.counts["warnings"] += 1

        payload = pr.to_dict()
        if pr.action == "fail":
            if len(self.failures) < self.reject_limit:
                self.failures.append(payload)
            else:
                self.failures_truncated = True
        elif len(self.rows) < self.detail_limit:
            self.rows.append(payload)
        else:
            self.rows_truncated = True

    def note_file_issue(self, issue: Issue) -> None:
        self.issue_summary[issue.code] = self.issue_summary.get(issue.code, 0) + 1
        if issue.severity == "warning":
            self.counts["warnings"] += 1


# ══════════════════════════════════════════════════════════════════════════
#  Row normalisation, per record type
# ══════════════════════════════════════════════════════════════════════════


class _Cells:
    """Accessor for one CSV row's mapped values.

    ``cell(name)`` is the ordinary single-line read: whitespace collapsed,
    'N/A'-style placeholder tokens treated as blank. ``cell.block(name)`` is for
    values whose LINE STRUCTURE is part of the content.

    That distinction is not cosmetic. Collapsing all whitespace is right for a
    ward or an MRN and destructive for a discharge summary, which is written in
    paragraphs and often carries a medication list one drug per line. Run
    together into a single block of prose, an imported summary is materially
    harder to read than the one the old system held — a quiet downgrade of the
    clinical record performed by the tool that was supposed to preserve it.
    (Caught by an end-to-end rehearsal, not by a unit test, 2026-08-07.)
    """

    def __init__(self, raw: dict, mapping: dict[str, str]):
        self._raw = raw
        self._mapping = mapping

    def __call__(self, name: str) -> str:
        column = self._mapping.get(name)
        return _clean(self._raw.get(column)) if column else ""

    def block(self, name: str) -> str:
        column = self._mapping.get(name)
        if not column:
            return ""
        value = self._raw.get(column)
        if value is None:
            return ""
        text = str(value).replace("\r\n", "\n").replace("\r", "\n")
        # Tidy each line, keep the breaks between them, and cap runs of blank
        # lines so a legacy export's padding does not become the document.
        lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in text.split("\n")]
        text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
        return "" if text.lower() in _NULL_TOKENS else text


def _normalise(
    raw: dict, mapping: dict[str, str], rt: RecordType, line: int, date_order: str
) -> _PlannedRow:
    """CSV row → normalised Orb values + issues. Touches no database."""
    pr = _PlannedRow(line)
    cell = _Cells(raw, mapping)

    pr.external_id = cell("external_id") or None
    pr.external_patient_id = cell("external_patient_id") or None
    pr.mrn = _normalise_mrn(cell("mrn")) or None

    if not pr.external_id:
        return pr.fail(
            "missing_external_id",
            "Row has no id from the old system, so it can neither be matched on a re-run "
            "nor traced back to its source record. It was NOT imported.",
            "external_id",
            str(raw.get(mapping.get("external_id", "")) or ""),
        )

    handler = _NORMALISERS[rt.name]
    handler(pr, cell, line, date_order)
    return pr


def _n_patients(pr: _PlannedRow, cell, line: int, date_order: str) -> None:
    if not pr.mrn:
        pr.fail(
            "missing_mrn",
            "Row has no MRN. Orb finds patients by hospital number, so a patient "
            "imported without one could never be looked up at the bedside. Orb will not "
            "invent an identifier. The row was NOT imported.",
            "mrn",
            "",
        )
        return
    name = cell("name")
    if not name:
        pr.fail(
            "missing_name",
            f"Row for MRN '{pr.mrn}' has no patient name, not imported (a bedside "
            f"identity check needs a name).",
            "name",
            "",
        )
        return

    # THE registration name rule (services/patient_name_rule.py), applied at
    # import exactly as at the admit desk: fewer than two letters is not a
    # name, so the row is recorded as rejected and not written.
    from services.patient_name_rule import MIN_NAME_LETTERS, name_is_too_short

    if name_is_too_short(name):
        pr.fail(
            "name_too_short",
            f"Row for MRN '{pr.mrn}' has the name '{name}', which has fewer than "
            f"{MIN_NAME_LETTERS} letters and cannot identify a patient at the "
            f"bedside, not imported. Correct it in the export and re-run.",
            "name",
            name,
        )
        return

    pr.values["name"] = name
    pr.issues.extend(_check_name(name, line))
    pr.values["mrn"] = pr.mrn

    for fname in ("phone", "address", "emergency_contact_name", "emergency_contact_phone"):
        value = cell(fname)
        if value:
            pr.values[fname] = value

    dob_raw = cell("date_of_birth")
    if dob_raw:
        dob, issues = _parse_date(dob_raw, "date_of_birth", line, date_order)
        pr.issues.extend(issues)
        if dob:
            pr.values["date_of_birth"] = dob

    age_raw = cell("age")
    if age_raw:
        age, issues = _parse_int(age_raw, "age", line, MIN_AGE, MAX_AGE)
        pr.issues.extend(issues)
        if age is not None:
            pr.values["age"] = age

    sex_raw = cell("sex")
    if sex_raw:
        sex, issues = _parse_sex(sex_raw, line)
        pr.issues.extend(issues)
        if sex:
            pr.values["sex"] = sex

    # Historical patients are not in the building. See rule 1 in the module
    # docstring: this single line is what keeps a five-year export off the live
    # ward board, and there is deliberately no way for the file to override it.
    pr.values["status"] = "discharged"


def _n_admissions(pr: _PlannedRow, cell, line: int, date_order: str) -> None:
    admitted_raw = cell("admitted_at")
    if not admitted_raw:
        pr.fail(
            "missing_admission_date",
            "Past admission has no admission date. An encounter with no start cannot be "
            "placed on a timeline, so it was NOT imported.",
            "admitted_at",
            "",
        )
        return
    admitted, issues = _parse_instant(admitted_raw, "admitted_at", line, date_order)
    pr.issues.extend(issues)
    if not admitted:
        pr.fail(
            "unparseable_admission_date",
            f"Admission date '{admitted_raw}' could not be read as a date, and an "
            f"encounter with no start cannot be placed on a timeline. NOT imported.",
            "admitted_at",
            admitted_raw,
        )
        return
    pr.values["created_at"] = admitted

    disch_raw = cell("discharged_at")
    if disch_raw:
        discharged, issues = _parse_instant(disch_raw, "discharged_at", line, date_order)
        pr.issues.extend(issues)
        if discharged:
            if discharged < admitted:
                pr.issues.append(
                    Issue(
                        line,
                        "discharge_before_admission",
                        "warning",
                        f"discharge date '{disch_raw}' is BEFORE the admission date "
                        f"'{admitted_raw}'. Both imported as given: Orb will not reorder a "
                        f"clinical timeline, but length of stay for this encounter is wrong.",
                        "discharged_at",
                        disch_raw,
                    )
                )
            pr.values["discharged_at"] = discharged

    for fname, column in (
        ("department", "department"),
        ("ward", "ward"),
        ("bed", "bed"),
        ("admission_type", "admission_type"),
        ("chief_complaint", "chief_complaint"),
        ("diagnosis", "initial_diagnosis"),
        ("attending_doctor_id", "attending_doctor_id"),
    ):
        value = cell(fname)
        if value:
            pr.values[column] = value

    # Admitting notes are clinical free text — keep the line structure.
    notes = cell.block("notes")
    if notes:
        pr.values["admitting_notes"] = notes

    status_raw = cell("status")
    token = re.sub(r"[^a-z0-9]+", " ", status_raw.lower()).strip()
    if token in _OPEN_ENCOUNTER_TOKENS:
        # Out of contract — see rule 1. Imported CLOSED and surfaced.
        pr.issues.append(
            Issue(
                line,
                "admission_open_in_source",
                "warning",
                f"The export says this encounter is still open ('{status_raw}'), but a history "
                f"import files CLOSED encounters, so it was imported as discharged and listed "
                f"under 'still_open_in_source'. If this patient is genuinely on the ward today, "
                f"put them there with the ROSTER import, which has the ward, bed and department "
                f"discipline that a live admission needs.",
                "status",
                status_raw,
            )
        )
    pr.values["status"] = (
        "transferred" if token in ("transferred", "referred out") else "discharged"
    )


def _n_diagnoses(pr: _PlannedRow, cell, line: int, date_order: str) -> None:
    problem = cell("problem")
    if not problem:
        pr.fail(
            "missing_problem",
            "Diagnosis row has no diagnosis text. There is nothing clinical to record, "
            "so it was NOT imported.",
            "problem",
            "",
        )
        return
    pr.values["problem"] = problem
    for fname in ("icd10_code", "snomed_code"):
        value = cell(fname)
        if value:
            pr.values[fname] = value

    for fname in ("onset_date", "resolved_at"):
        raw = cell(fname)
        if raw:
            parsed, issues = _parse_date(raw, fname, line, date_order)
            pr.issues.extend(issues)
            if parsed:
                pr.values[fname] = parsed

    status_raw = cell("status")
    if status_raw:
        status, issues = _parse_enum(
            status_raw,
            line,
            "status",
            _DIAGNOSIS_STATUS,
            _DIAGNOSIS_SYNONYMS,
            "inactive",
            "unrecognised_diagnosis_status",
        )
        pr.issues.extend(issues)
        pr.values["status"] = status or "inactive"
    else:
        # See rule 1: problem_list defaults to 'active', which would assert this
        # old diagnosis as a CURRENT problem the export never claimed.
        pr.values["status"] = "inactive"
        pr.issues.append(
            Issue(
                line,
                "diagnosis_status_defaulted",
                "warning",
                "The export carries no status column, so this diagnosis was filed INACTIVE "
                "rather than asserted as an active problem. Map a status column to import the "
                "old system's own answer.",
                "status",
                None,
            )
        )


def _n_medications(pr: _PlannedRow, cell, line: int, date_order: str) -> None:
    drug = cell("drug_name")
    if not drug:
        pr.fail(
            "missing_drug_name",
            "Medication row has no drug name. There is nothing clinical to record, so "
            "it was NOT imported.",
            "drug_name",
            "",
        )
        return
    pr.values["drug_name"] = drug

    # dosage/route/frequency are NOT NULL in the schema. Recording the literal
    # UNKNOWN is a refusal to guess, not a guess — and it keeps the drug
    # EXPOSURE, which is the fact allergy and interaction history is built on.
    missing: list[str] = []
    for fname in ("dosage", "route", "frequency"):
        value = cell(fname)
        if value:
            pr.values[fname] = value
        else:
            pr.values[fname] = UNKNOWN_VALUE
            missing.append(fname)
    if missing:
        pr.issues.append(
            Issue(
                line,
                "missing_clinical_value",
                "warning",
                f"The export gave no {', '.join(missing)} for '{drug}', so {'it was' if len(missing) == 1 else 'they were'} "
                f"recorded as '{UNKNOWN_VALUE}'. Orb will not invent a dose, a route or a "
                f"schedule; the row is kept because the fact that the patient was exposed to "
                f"this drug is itself clinically important.",
                missing[0],
                None,
            )
        )

    notes = cell.block("notes")
    if notes:
        pr.values["notes"] = notes

    started_raw = cell("started_at")
    if started_raw:
        started, issues = _parse_instant(started_raw, "started_at", line, date_order)
        pr.issues.extend(issues)
        if started:
            pr.values["started_at"] = started

    status_raw = cell("status")
    if status_raw:
        status, issues = _parse_enum(
            status_raw,
            line,
            "status",
            _MED_STATUS,
            _MED_SYNONYMS,
            "discontinued",
            "unrecognised_medication_status",
        )
        pr.issues.extend(issues)
        pr.values["status"] = status or "discontinued"
    else:
        # See rule 1. 'active' is read by drug_safety_service's live interaction
        # check, by Sage and by the discharge summary.
        pr.values["status"] = "discontinued"
        pr.issues.append(
            Issue(
                line,
                "medication_status_defaulted",
                "warning",
                "The export carries no status column, so this drug was filed DISCONTINUED "
                "rather than added to the patient's CURRENT medication list, where it would be "
                "screened for interactions and handed over as if it were still prescribed. Map "
                "a status column to import the old system's own answer.",
                "status",
                None,
            )
        )


def _n_results(pr: _PlannedRow, cell, line: int, date_order: str) -> None:
    name = cell("observation_name")
    if not name:
        pr.fail(
            "missing_observation_name",
            "Result row has no test name. A value with no test is uninterpretable, so "
            "it was NOT imported.",
            "observation_name",
            "",
        )
        return
    pr.values["observation_name"] = name

    text, numeric, issues = _parse_result_value(cell("value"), line)
    pr.issues.extend(issues)
    if text is not None:
        pr.values["value_text"] = text
    if numeric is not None:
        pr.values["value_numeric"] = numeric
    if text is None:
        pr.issues.append(
            Issue(
                line,
                "result_without_value",
                "warning",
                f"Result '{name}' has no value. Imported as a record that the test was done, "
                f"with no result. Orb will not fabricate one.",
                "value",
                None,
            )
        )

    for fname, column in (
        ("unit", "unit"),
        ("reference_range", "reference_range"),
        ("abnormal_flag", "abnormal_flag"),
        ("observation_code", "observation_code"),
        ("observation_code_system", "observation_code_system"),
        ("panel_name", "panel_name"),
    ):
        value = cell(fname)
        if value:
            pr.values[column] = value

    observed_raw = cell("observed_at")
    if observed_raw:
        observed, issues = _parse_instant(observed_raw, "observed_at", line, date_order)
        pr.issues.extend(issues)
        if observed:
            pr.values["observed_at"] = observed
    else:
        pr.issues.append(
            Issue(
                line,
                "result_without_time",
                "warning",
                f"Result '{name}' has no observation date, so it cannot be placed on the "
                f"patient's timeline and will not appear in date-ordered views.",
                "observed_at",
                None,
            )
        )

    status_raw = cell("status")
    if status_raw:
        status, issues = _parse_enum(
            status_raw,
            line,
            "status",
            _RESULT_STATUS,
            _RESULT_SYNONYMS,
            "final",
            "unrecognised_result_status",
        )
        pr.issues.extend(issues)
        pr.values["status"] = status or "final"
    else:
        pr.values["status"] = "final"


def _n_documents(pr: _PlannedRow, cell, line: int, date_order: str) -> None:
    title = cell("title")
    # A clinical note is written in lines and paragraphs — see _Cells.block.
    content = cell.block("content")
    if not title:
        pr.fail("missing_title", "Document row has no title, not imported.", "title", "")
        return
    if not content:
        pr.fail(
            "missing_content",
            f"Document '{title}' has no text. An empty document in a chart reads as "
            f"'nothing was written', which is not what the old system holds, the row "
            f"was NOT imported so the gap stays visible.",
            "content",
            "",
        )
        return
    pr.values["title"] = title
    pr.values["content"] = content
    pr.values["doc_type"] = cell("doc_type") or "imported_document"

    author = cell("author_id")
    if author:
        pr.values["author_id"] = author

    authored_raw = cell("authored_at")
    if authored_raw:
        authored, issues = _parse_instant(authored_raw, "authored_at", line, date_order)
        pr.issues.extend(issues)
        if authored:
            pr.values["created_at"] = authored


_NORMALISERS = {
    "patients": _n_patients,
    "admissions": _n_admissions,
    "diagnoses": _n_diagnoses,
    "medications": _n_medications,
    "results": _n_results,
    "documents": _n_documents,
}


# ══════════════════════════════════════════════════════════════════════════
#  Resolution + write, per record type
# ══════════════════════════════════════════════════════════════════════════

# Columns each record type is allowed to write, beyond patient_id + provenance.
_WRITABLE: dict[str, tuple[str, ...]] = {
    "patients": (
        "name",
        "date_of_birth",
        "age",
        "sex",
        "phone",
        "address",
        "emergency_contact_name",
        "emergency_contact_phone",
    ),
    "admissions": (
        "department",
        "ward",
        "bed",
        "admission_type",
        "chief_complaint",
        "initial_diagnosis",
        "admitting_notes",
        "attending_doctor_id",
        "status",
        "created_at",
        "discharged_at",
    ),
    "diagnoses": ("problem", "icd10_code", "snomed_code", "status", "onset_date", "resolved_at"),
    "medications": ("drug_name", "dosage", "route", "frequency", "status", "started_at", "notes"),
    "results": (
        "observation_name",
        "value_text",
        "value_numeric",
        "unit",
        "reference_range",
        "abnormal_flag",
        "observation_code",
        "observation_code_system",
        "panel_name",
        "status",
        "observed_at",
    ),
    "documents": ("doc_type", "title", "content", "author_id", "created_at"),
}


class _Resolver:
    """Per-BATCH DB lookups, shared by the dry run and the apply pass.

    WHY THESE ARE CLEARED EVERY BATCH. They look like caches and were written as
    ones, growing for the whole run. ``by_mrn`` holds a full ``SELECT *``
    patients row per MRN, so a 50 000-row rehearsal peaked at 316 MB of RSS and
    the 200 000-row cap would have reached roughly a gigabyte — which is exactly
    the "must not exhaust memory" promise the streaming is for, broken by the
    lookup layer instead of the parser. (Measured 2026-08-07.)

    Keeping them across batches was never load-bearing: ``prefetch`` queries for
    precisely what the current batch needs, and because the apply pass runs in
    ONE transaction, a patient written in an earlier batch is visible to that
    query. Rows created within the batch being processed are folded in
    incrementally by _write_patient. So clearing costs nothing but a repeated
    lookup in the rare case where the same patient appears in two batches, and
    it bounds peak memory to one batch instead of one file.

    ``known_users`` is deliberately NOT cleared: a hospital has hundreds of
    staff, not hundreds of thousands, so it is bounded by the user table.
    """

    def __init__(self, db: Any, source_system: str, rt: RecordType) -> None:
        self.db = db
        self.source_system = source_system
        self.rt = rt
        self.xref: dict[str, str] = {}  # external_patient_id -> orb patient id
        self.by_mrn: dict[str, Any] = {}  # mrn -> patients row
        self.existing: dict[str, Any] = {}  # external_id -> target row
        self.known_users: dict[str, str] = {}
        # multisite-4: None while one facility is on record, else the run's
        # {actor, scope, run_facility} (_facility_context), and per batch
        # each patient's facility as _check_facility read it.
        self.facility: dict | None = None
        self.patient_facility: dict[str, int] = {}

    # ── batch prefetch ───────────────────────────────────────────────────
    def prefetch(self, rows: list[_PlannedRow]) -> None:
        self.xref.clear()
        self.by_mrn.clear()
        self.existing.clear()
        self.patient_facility.clear()
        ext_patients = {
            r.external_patient_id
            for r in rows
            if r.external_patient_id and r.external_patient_id not in self.xref
        }
        if self.rt.name == "patients":
            ext_patients |= {
                r.external_id for r in rows if r.external_id and r.external_id not in self.xref
            }
        for chunk in _chunks(sorted(ext_patients)):
            marks = ",".join("?" * len(chunk))
            for row in self.db.execute(
                f"SELECT external_patient_id, patient_id FROM history_import_patient_xref "
                f"WHERE source_system = ? AND external_patient_id IN ({marks})",
                (self.source_system, *chunk),
            ).fetchall():
                self.xref[row["external_patient_id"]] = row["patient_id"]

        mrns = {r.mrn for r in rows if r.mrn and r.mrn not in self.by_mrn}
        for chunk in _chunks(sorted(mrns)):
            marks = ",".join("?" * len(chunk))
            for row in self.db.execute(
                f"SELECT * FROM patients WHERE mrn IN ({marks})", tuple(chunk)
            ).fetchall():
                self.by_mrn[row["mrn"]] = row

        if self.rt.name != "patients":
            ext_ids = {
                r.external_id for r in rows if r.external_id and r.external_id not in self.existing
            }
            table = self.rt.table
            for chunk in _chunks(sorted(ext_ids)):
                marks = ",".join("?" * len(chunk))
                for row in self.db.execute(
                    f'SELECT * FROM "{table}" WHERE import_source_system = ? '
                    f"AND import_external_id IN ({marks})",
                    (self.source_system, *chunk),
                ).fetchall():
                    self.existing[row["import_external_id"]] = row

        user_ids = set()
        for r in rows:
            for key in ("attending_doctor_id", "author_id"):
                value = r.values.get(key)
                if value and value not in self.known_users:
                    user_ids.add(value)
        for chunk in _chunks(sorted(user_ids)):
            marks = ",".join("?" * len(chunk))
            for row in self.db.execute(
                f"SELECT id, role FROM users WHERE id IN ({marks})", tuple(chunk)
            ).fetchall():
                self.known_users[row["id"]] = row["role"]

    # ── patient identity ─────────────────────────────────────────────────
    def resolve_patient(self, pr: _PlannedRow) -> str | None:
        """Orb patient id for a CHILD row, or None with the reason recorded.

        Two identifiers that disagree is the duplicate-patient mess every legacy
        export carries. Picking either one attaches a clinical record to the
        wrong chart, so the row fails instead.
        """
        by_ext = self.xref.get(pr.external_patient_id) if pr.external_patient_id else None
        mrn_row = self.by_mrn.get(pr.mrn) if pr.mrn else None
        by_mrn = mrn_row["id"] if mrn_row is not None else None

        if by_ext and by_mrn and by_ext != by_mrn:
            pr.fail(
                "patient_identifier_conflict",
                f"This row names patient '{pr.external_patient_id}' (which Orb maps to "
                f"{by_ext}) AND MRN '{pr.mrn}' (which is patient {by_mrn}), two "
                f"different people. Orb will not choose between them, because attaching "
                f"this record to the wrong chart is worse than not importing it. Fix the "
                f"export, or import only one identifier column.",
            )
            return None
        resolved = by_ext or by_mrn
        if resolved:
            return resolved

        pr.fail(
            "unknown_patient",
            "No patient in Orb matches "
            + (f"old-system id '{pr.external_patient_id}'" if pr.external_patient_id else "")
            + (" or " if pr.external_patient_id and pr.mrn else "")
            + (f"MRN '{pr.mrn}'" if pr.mrn else "")
            + ". Import the PATIENTS file for this source system first, a clinical "
            "record with no patient is invisible in Orb and cannot be traced.",
        )
        return None

    def check_user(self, pr: _PlannedRow, key: str) -> None:
        value = pr.values.get(key)
        if not value:
            return
        if value not in self.known_users:
            pr.issues.append(
                Issue(
                    pr.line,
                    "unknown_orb_user",
                    "warning",
                    f"{key} '{value}' is not an Orb user. The link was left blank. Orb does not "
                    f"create staff accounts from a clinical export. Create the user, then re-run "
                    f"the import to attach them.",
                    key,
                    value,
                )
            )
            pr.values.pop(key, None)


def _chunks(items: list[str], size: int = 400) -> Iterator[list[str]]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


# ══════════════════════════════════════════════════════════════════════════
#  The engine
# ══════════════════════════════════════════════════════════════════════════


def plan(
    source: Any,
    record_type: str,
    mapping: dict[str, str],
    *,
    source_system: str,
    date_order: str = "auto",
    header_line: int | None = None,
    detail_limit: int = DEFAULT_DETAIL_LIMIT,
    reject_limit: int = DEFAULT_REJECT_LIMIT,
    actor_user_id: str | None = None,
) -> dict:
    """DRY RUN. Reports exactly what an import would do. Writes NOTHING.

    ``source`` is bytes (content) or a path (streamed). ``mapping`` is
    ``{orb_field: csv_header}`` — nothing is imported off a guessed mapping.

    ``counts`` and ``issue_summary`` are EXACT over every row read. ``rows`` and
    ``failures`` are capped at ``detail_limit``/``reject_limit`` because a
    200 000-row file cannot come back over HTTP; truncation is flagged rather
    than hidden, and commit persists the complete reject list regardless.

    ``actor_user_id`` is the account running the dry run. It is read only
    while more than one facility is on record (multisite-4, see
    _check_facility), and then it is required, so the dry run fails the same
    rows the commit would.
    """
    return _execute(
        source,
        record_type,
        mapping,
        source_system=source_system,
        date_order=date_order,
        header_line=header_line,
        detail_limit=detail_limit,
        reject_limit=reject_limit,
        write=False,
        actor_user_id=actor_user_id,
    )


def apply_import(
    source: Any,
    record_type: str,
    mapping: dict[str, str],
    *,
    source_system: str,
    actor_user_id: str,
    source_filename: str | None = None,
    date_order: str = "auto",
    header_line: int | None = None,
    note: str | None = None,
    detail_limit: int = DEFAULT_DETAIL_LIMIT,
    reject_limit: int = DEFAULT_REJECT_LIMIT,
) -> dict:
    """Apply the import in ONE transaction. All-or-nothing.

    Recomputed server-side from the same bytes by the same code as the dry run,
    so a commit can never write something the dry run did not show for the same
    file and mapping.

    Idempotent on (source_system, external_id), which is UNIQUE in the schema:
    a second run over an unchanged file creates nothing.
    """
    if not (actor_user_id or "").strip():
        raise ValueError("actor_user_id is required. A bulk clinical write must be attributable.")
    return _execute(
        source,
        record_type,
        mapping,
        source_system=source_system,
        date_order=date_order,
        header_line=header_line,
        detail_limit=detail_limit,
        reject_limit=reject_limit,
        write=True,
        actor_user_id=actor_user_id,
        source_filename=source_filename,
        note=note,
    )


def _execute(
    source: Any,
    record_type: str,
    mapping: dict[str, str],
    *,
    source_system: str,
    date_order: str,
    header_line: int | None,
    detail_limit: int,
    reject_limit: int,
    write: bool,
    actor_user_id: str | None,
    source_filename: str | None = None,
    note: str | None = None,
) -> dict:
    """Shared streaming engine for the dry run and the apply pass.

    ONE code path so the plan the admin approved and the plan that executes
    cannot drift. When ``write`` is true everything happens inside a single
    transaction: a half-applied historical migration is the worst outcome
    available, so it is not a state this can leave behind.
    """
    rt = _record_type(record_type)
    source_system = (source_system or "").strip()
    if not source_system:
        raise ValueError(
            "source_system is required: it names the system these records came from and is "
            "half of the idempotency key, so an import without one could never be re-run "
            "safely nor traced back."
        )
    if date_order not in ("auto", "dmy", "mdy"):
        raise ValueError("date_order must be one of: auto, dmy, mdy")

    mapping = {k: v for k, v in (mapping or {}).items() if v}
    unknown = [k for k in mapping if k not in rt.by_name]
    if unknown:
        raise ValueError(f"Unknown target field(s) for '{rt.name}': {sorted(unknown)}")
    missing = [f for f in rt.required if f not in mapping]
    if missing:
        raise ValueError(f"Mapping is missing required field(s) for '{rt.name}': {missing}.")
    if rt.name != "patients" and not ({"external_patient_id", "mrn"} & set(mapping)):
        raise ValueError(
            f"'{rt.name}' rows must be attachable to a patient: map the old system's patient "
            f"id (external_patient_id) or the MRN, or both."
        )

    src = _Source(source)
    report = _Report(detail_limit, reject_limit)
    file_issues = list(src.issues)

    header = _locate_header(src, rt, header_line)
    file_issues.extend(header.issues)
    if not header.confident:
        raise ValueError(
            "Could not identify the header row with confidence. Reading the wrong line as "
            "the header shifts every column into the wrong field, so nothing was planned. "
            "Re-run with header_line=<1-based line number> (the preview step lists the "
            "candidates)."
        )
    unresolved = [f"{k} -> '{v}'" for k, v in mapping.items() if v not in header.fields]
    if unresolved:
        raise ValueError(f"Mapped column(s) not present in the file: {unresolved}")

    for issue in file_issues:
        report.note_file_issue(issue)

    run_id = f"HI-{uuid.uuid4().hex[:10].upper()}"
    started = datetime.now(UTC).isoformat()
    seen_external: dict[str, int] = {}
    written = {"created": 0, "updated": 0}
    # multisite-4: None while one facility is on record (nothing is read).
    facility = _facility_context(actor_user_id)

    with db_session() as db:
        resolver = _Resolver(db, source_system, rt)
        resolver.facility = facility
        batch: list[_PlannedRow] = []
        reject_buffer: list[dict] = []

        if write:
            # The ledger row is opened FIRST, with zero counts, so rejects can be
            # flushed against its foreign key as they are found instead of being
            # held until the end. It is finalised by _record_run in the same
            # transaction, so a run that never completes leaves nothing at all.
            _open_run(
                db,
                run_id=run_id,
                rt=rt,
                source_system=source_system,
                actor_user_id=actor_user_id,
                source=src,
                source_filename=source_filename,
                mapping=mapping,
                date_order=date_order,
                note=note,
                started=started,
                facility_id=facility["run_facility"] if facility else 1,
            )

        def note_reject(pr: _PlannedRow) -> None:
            """Record a rejected row durably, in batches.

            Only buffered when WRITING. A dry run's failure detail is the capped
            ``report.failures``; accumulating every reject as well would break
            the module's memory promise in the most likely failure case there
            is — a mis-mapped column makes every row in the file fail at once.
            """
            if not write:
                return
            reject_buffer.append(_reject_row(pr))
            if len(reject_buffer) >= BATCH_SIZE:
                _flush_rejects(db, run_id, reject_buffer)

        def flush() -> None:
            if not batch:
                return
            resolver.prefetch(batch)
            for pr in batch:
                _classify(pr, resolver, rt, source_system)
                if facility is not None:
                    _check_facility(pr, resolver, rt)
                if write and pr.action in ("create", "update"):
                    _write_row(
                        db,
                        pr,
                        resolver,
                        rt,
                        source_system,
                        run_id,
                        actor_user_id or "system",
                        written,
                    )
                if pr.action == "fail":
                    note_reject(pr)
                report.add(pr)
                if (
                    rt.name == "admissions"
                    and pr.action != "fail"
                    and any(i.code == "admission_open_in_source" for i in pr.issues)
                    and len(report.still_open_in_source) < 500
                ):
                    report.still_open_in_source.append(
                        {
                            "line": pr.line,
                            "external_id": pr.external_id,
                            "patient_id": pr.patient_id,
                            "mrn": pr.mrn,
                        }
                    )
            batch.clear()

        for line_no, raw, rejection, raw_text in _iter_rows(src, header):
            if rejection is not None and not raw:
                # row_limit — a file-level refusal, not a row.
                file_issues.append(rejection)
                report.note_file_issue(rejection)
                break
            if not raw:
                report.blank_rows += 1
                continue
            if rejection is not None:
                pr = _PlannedRow(line_no, raw_text=raw_text)
                pr.action, pr.reason = "fail", rejection.code
                pr.issues.append(rejection)
                note_reject(pr)
                report.add(pr)
                continue

            pr = _normalise(raw, mapping, rt, line_no, date_order)
            pr.raw_text = raw_text
            if pr.action == "fail":
                note_reject(pr)
                report.add(pr)
                continue

            first_line = seen_external.get(pr.external_id or "")
            if first_line is not None:
                pr.fail(
                    "duplicate_external_id_in_file",
                    f"Id '{pr.external_id}' already appeared on line {first_line}. Only "
                    f"the FIRST row is used. The rows were NOT merged, because two "
                    f"records sharing an id in the export is a defect in the export and "
                    f"guessing which one is current would be a clinical decision.",
                    "external_id",
                    pr.external_id,
                )
                note_reject(pr)
                report.add(pr)
                continue
            assert pr.external_id is not None
            seen_external[pr.external_id] = line_no

            batch.append(pr)
            if len(batch) >= BATCH_SIZE:
                flush()
        flush()

        if report.blank_rows:
            issue = Issue(
                None, "blank_rows", "warning", f"{report.blank_rows} blank row(s) were skipped."
            )
            file_issues.append(issue)
            report.note_file_issue(issue)

        if report.counts["total_rows"] == 0:
            # "0 created, 0 failed" otherwise renders as a clean success, which is
            # the silent-failure class this whole module exists to prevent — just
            # arrived at from the other end. An empty export, a header line picked
            # one row too low, or a filter left on in the old system all land here.
            issue = Issue(
                None,
                "no_data_rows",
                "error",
                f"The file has a header row on line {header.index + 1} but NO data "
                f"rows beneath it, so nothing was imported. This reports as a clean "
                f"run with zero records. Check that the export actually produced "
                f"rows, and that the header line is the one you meant.",
            )
            file_issues.append(issue)
            report.note_file_issue(issue)

        if write:
            _flush_rejects(db, run_id, reject_buffer)
            _record_run(db, run_id=run_id, report=report, written=written)

    result = {
        "run_id": run_id if write else None,
        "applied": bool(write),
        "record_type": rt.name,
        "target_table": rt.table,
        "source_system": source_system,
        "source_filename": source_filename,
        "source_sha256": src.sha256,
        "source_bytes": src.size,
        "mapping": mapping,
        "date_order": date_order,
        "delimiter": header.delimiter,
        "header_line": header.index + 1,
        "headers": header.fields,
        "counts": report.counts,
        "issue_summary": report.issue_summary,
        "rows": report.rows,
        "rows_truncated": report.rows_truncated,
        "failures": report.failures,
        "failures_truncated": report.failures_truncated,
        "still_open_in_source": report.still_open_in_source,
        "file_issues": [i.to_dict() for i in file_issues],
    }
    if write:
        result["summary"] = {
            "created": written["created"],
            "updated": written["updated"],
            "unchanged": report.counts["unchanged"],
            "failed": report.counts["failed"],
            "warnings": report.counts["warnings"],
        }
    return result


def _reject_row(pr: _PlannedRow) -> dict:
    worst = next(
        (i for i in pr.issues if i.severity == "error"), pr.issues[0] if pr.issues else None
    )
    return {
        "line": pr.line,
        "severity": "error",
        "code": pr.reason or (worst.code if worst else "unknown"),
        "field": worst.field if worst else None,
        "message": worst.message if worst else "Row was not imported.",
        "external_id": pr.external_id,
        "external_patient_id": pr.external_patient_id,
        "patient_id": pr.patient_id,
        "raw_row": pr.raw_text,
    }


# ══════════════════════════════════════════════════════════════════════════
#  Classification
# ══════════════════════════════════════════════════════════════════════════


def _classify(pr: _PlannedRow, resolver: _Resolver, rt: RecordType, source_system: str) -> None:
    """Decide create / update / unchanged / fail for one normalised row."""
    if pr.action == "fail":
        return
    if rt.name == "patients":
        _classify_patient(pr, resolver, source_system)
        return

    patient_id = resolver.resolve_patient(pr)
    if patient_id is None:
        return
    pr.patient_id = patient_id
    resolver.check_user(pr, "attending_doctor_id")
    resolver.check_user(pr, "author_id")

    existing = resolver.existing.get(pr.external_id or "")
    if existing is None:
        pr.action = "create"
        return

    # Only ever reachable for a row THIS source system previously wrote — the
    # lookup is on (import_source_system, import_external_id) — so an update
    # here can never touch Orb-native clinical data.
    pr.target_id = str(existing["id"])
    changes: dict[str, dict] = {}
    for column in _WRITABLE[rt.name]:
        if column not in pr.values:
            continue
        current = existing[column] if column in existing.keys() else None
        if _same(current, pr.values[column]):
            continue
        changes[column] = {"from": current, "to": pr.values[column]}
    pr.changes = changes
    pr.action = "update" if changes else "unchanged"
    if not changes:
        pr.reason = "no_field_changed"


def _classify_patient(pr: _PlannedRow, resolver: _Resolver, source_system: str) -> None:
    """Patients are matched on the legacy id first, then MRN.

    FILL HOLES ONLY. A patient Orb already holds has a record that is fresher
    than any legacy export, and a migration that quietly reverts a correction a
    clinician made in Orb is worse than one that fails loudly. So an existing
    non-NULL value is kept and the difference is reported.
    """
    by_ext = resolver.xref.get(pr.external_id or "")
    mrn_row = resolver.by_mrn.get(pr.mrn) if pr.mrn else None

    if by_ext and mrn_row is not None and mrn_row["id"] != by_ext:
        pr.fail(
            "external_id_mrn_conflict",
            f"Old-system patient '{pr.external_id}' is already mapped to Orb patient "
            f"{by_ext}, but this row gives MRN '{pr.mrn}', which is patient "
            f"{mrn_row['id']}. That is the same legacy id claiming two different people, "
            f"usually one patient held twice in the old system. Orb will not merge or "
            f"choose; reconcile them in the export.",
        )
        return
    if by_ext and mrn_row is None:
        pr.fail(
            "external_id_mrn_conflict",
            f"Old-system patient '{pr.external_id}' is already mapped to Orb patient "
            f"{by_ext}, but this row gives a DIFFERENT MRN '{pr.mrn}' that Orb does not "
            f"hold. Importing it would either rename an existing patient or split one "
            f"person across two records. Reconcile the duplicate in the export.",
        )
        return

    existing = mrn_row
    if existing is None:
        pr.action = "create"
        return

    pr.patient_id = pr.target_id = existing["id"]
    changes: dict[str, dict] = {}
    kept: list[str] = []
    for column in _WRITABLE["patients"]:
        if column not in pr.values:
            continue
        current = existing[column] if column in existing.keys() else None
        if current is None or str(current).strip() == "":
            changes[column] = {"from": current, "to": pr.values[column]}
        elif not _same(current, pr.values[column]):
            kept.append(column)
    if kept:
        pr.issues.append(
            Issue(
                pr.line,
                "kept_orb_value",
                "warning",
                f"Orb already holds a different {', '.join(kept)} for MRN '{pr.mrn}'. The Orb "
                f"value was KEPT and the legacy value discarded. The live record is fresher "
                f"than the export, and an import must never revert a correction made in Orb. "
                f"Change it in Orb if the legacy value is the right one.",
            )
        )
    pr.changes = changes
    pr.action = "update" if changes else "unchanged"
    if not changes:
        pr.reason = "no_field_changed"


# ══════════════════════════════════════════════════════════════════════════
#  Facilities (multisite-4, W11-A)
# ══════════════════════════════════════════════════════════════════════════
# While more than one facility is on record a history import runs AT a
# facility: the importing account's (facility_service.user_facility_id). The
# run row, every patient it creates (registered_facility_id) and every past
# admission it creates (facility_id) carry that facility, or for an admission
# whose department the departments master lists, the department's facility.
# A row FAILS, with the reason in the reject ledger like any other failure,
# when it would:
#   * write anything for a patient whose latest encounter
#     (permissions.FACILITY_PATIENT_SQL) is outside the account's facility
#     scope, or create anything at all from an account whose scope is empty
#     (facility_scope);
#   * create a past admission at another facility than the one the patient's
#     admissions are at, which would split the patient across two facilities
#     (cross_facility_move_refused; ruling MS-R3 stays at (c), refuse).
# A failed row shows no patient id and no current values, because the patient
# may be another facility's. With one facility on record none of this reads
# anything and the import is exactly as before.

FACILITY_SCOPE_WORDS = (
    "This record belongs to a patient at a facility your account does not work at, "
    "so it was NOT imported. Import it from an account at that facility."
)
CROSS_FACILITY_WORDS = (
    "This patient's admissions are at another facility of the group, and Orb keeps "
    "each patient's admissions at one facility until moving a patient between "
    "facilities is supported. The past admission was NOT imported."
)


def _facility_context(actor_user_id: str | None) -> dict | None:
    """None while one facility is on record. Otherwise {actor, scope,
    run_facility} for the importing account. ValueError with no account,
    because whose facilities to check cannot be guessed."""
    from services import facility_service

    if not facility_service.multi_facility_data():
        return None
    if not (actor_user_id or "").strip():
        raise ValueError(
            "More than one facility is on record, so the account running the import is "
            "needed to check each record's facility. Nothing was planned."
        )
    with db_session() as db:
        found = db.execute(
            "SELECT id, role, department FROM users WHERE id = ?", (actor_user_id,)
        ).fetchone()
    actor = (
        dict(found)
        if found is not None
        else {"id": actor_user_id, "role": None, "department": None}
    )
    return {
        "actor": actor,
        "scope": facility_service.facility_scope_for(actor),
        "run_facility": facility_service.user_facility_id(actor)
        or facility_service.MAIN_FACILITY_ID,
    }


def _fail_for_facility(pr: _PlannedRow, code: str, message: str) -> None:
    pr.fail(
        code, message, "mrn" if pr.mrn else "external_patient_id", pr.mrn or pr.external_patient_id
    )
    pr.patient_id = None
    pr.target_id = None
    pr.changes = {}


def _check_facility(pr: _PlannedRow, resolver: _Resolver, rt: RecordType) -> None:
    """Fail the row for its facility (see the block comment above), or stamp
    the facility a past admission it creates is at. Called only while more
    than one facility is on record, after _classify."""
    from services.facility_identity import document_facility_id
    from services.frontdesk_service import department_facility_on, patient_facility_ids_on

    if pr.action not in ("create", "update", "unchanged"):
        return
    ctx = resolver.facility
    if ctx is None:
        # One facility on record: flush() does not call this then.
        return
    scope = ctx["scope"]
    db = resolver.db
    if scope is not None and not scope:
        _fail_for_facility(pr, "facility_scope", FACILITY_SCOPE_WORDS)
        return
    if pr.patient_id and scope is not None:
        current = resolver.patient_facility.get(pr.patient_id)
        if current is None:
            current = document_facility_id(db, patient_id=pr.patient_id)
            resolver.patient_facility[pr.patient_id] = current
        if current not in scope:
            _fail_for_facility(pr, "facility_scope", FACILITY_SCOPE_WORDS)
            return
    if rt.name != "admissions" or pr.action != "create":
        return
    department = pr.values.get("department")
    target = (department_facility_on(db, department) if department else None) or ctx["run_facility"]
    if scope is not None and target not in scope:
        _fail_for_facility(pr, "facility_scope", FACILITY_SCOPE_WORDS)
        return
    if pr.patient_id and patient_facility_ids_on(db, pr.patient_id) - {target}:
        _fail_for_facility(pr, "cross_facility_move_refused", CROSS_FACILITY_WORDS)
        return
    pr.facility_id = target


# ══════════════════════════════════════════════════════════════════════════
#  Writing
# ══════════════════════════════════════════════════════════════════════════


def _write_row(
    db: Any,
    pr: _PlannedRow,
    resolver: _Resolver,
    rt: RecordType,
    source_system: str,
    run_id: str,
    actor_user_id: str,
    written: dict[str, int],
) -> None:
    if rt.name == "patients":
        _write_patient(db, pr, resolver, source_system, run_id, written)
        return

    table = rt.table
    if pr.action == "update":
        columns = [c for c in _WRITABLE[rt.name] if c in pr.changes]
        db.execute(
            f'UPDATE "{table}" SET {", ".join(f"{c} = ?" for c in columns)}, '
            f"import_run_id = ? WHERE id = ?",
            [pr.changes[c]["to"] for c in columns] + [run_id, pr.target_id],
        )
        written["updated"] += 1
        return

    values = dict(pr.values)
    insert_values: dict[str, Any] = {"patient_id": pr.patient_id}
    for column in _WRITABLE[rt.name]:
        if column in values:
            insert_values[column] = values[column]

    if rt.name == "admissions":
        insert_values["id"] = _new_id("ADM-HI")
        insert_values["admitted_by"] = actor_user_id
        # `admission_source` is the machine-readable marker. An "Imported from X
        # by run Y" banner was prefixed onto admitting_notes here and has been
        # removed: the provenance insert_values already say it, and pushing generated
        # text into a CLINICAL free-text field broke idempotency — the next run
        # compared the file's plain note against the stored banner+note, found a
        # difference, and reported an update on every single re-run forever.
        insert_values["admission_source"] = SOURCE_TAG
        if resolver.facility is not None:
            # multisite-4: _check_facility's facility; the column's default,
            # 1, stands while one facility is on record.
            insert_values["facility_id"] = pr.facility_id or resolver.facility["run_facility"]
    elif rt.name in ("diagnoses", "medications", "results"):
        insert_values["source"] = SOURCE_TAG

    insert_values["import_source_system"] = source_system
    insert_values["import_external_id"] = pr.external_id
    insert_values["import_run_id"] = run_id

    names = list(insert_values)
    cursor = db.execute(
        f'INSERT INTO "{table}" ({", ".join(names)}) VALUES ({", ".join("?" * len(names))})',
        [insert_values[n] for n in names],
    )
    pr.target_id = str(insert_values.get("id") or cursor.lastrowid)
    written["created"] += 1


def _write_patient(
    db: Any,
    pr: _PlannedRow,
    resolver: _Resolver,
    source_system: str,
    run_id: str,
    written: dict[str, int],
) -> None:
    if pr.action == "create":
        patient_id = _new_id("PAT-HI")
        columns: dict[str, Any] = {"id": patient_id, "mrn": pr.mrn}
        for column in _WRITABLE["patients"]:
            if column in pr.values:
                columns[column] = pr.values[column]
        # Rule 1: historical patients are NOT in the building.
        columns["status"] = "discharged"
        if resolver.facility is not None:
            # multisite-4: registered at the facility the run is at.
            columns["registered_facility_id"] = resolver.facility["run_facility"]
        columns["import_source_system"] = source_system
        columns["import_external_id"] = pr.external_id
        columns["import_run_id"] = run_id
        names = list(columns)
        db.execute(
            f"INSERT INTO patients ({', '.join(names)}) VALUES ({', '.join('?' * len(names))})",
            [columns[n] for n in names],
        )
        pr.patient_id = pr.target_id = patient_id
        written["created"] += 1
        created_by_import = 1
    else:
        if pr.action == "update":
            update_columns = [c for c in _WRITABLE["patients"] if c in pr.changes]
            db.execute(
                f"UPDATE patients SET {', '.join(f'{c} = ?' for c in update_columns)} WHERE id = ?",
                [pr.changes[c]["to"] for c in update_columns] + [pr.patient_id],
            )
            written["updated"] += 1
        created_by_import = 0

    # The cross-system mapping is written whether the patient was created here
    # or already existed — child records resolve through it either way, and an
    # Orb-native row must NOT be stamped with import provenance to make that work.
    db.execute(
        """INSERT OR IGNORE INTO history_import_patient_xref
               (source_system, external_patient_id, patient_id, mrn_at_import,
                created_by_import, import_run_id)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (source_system, pr.external_id, pr.patient_id, pr.mrn, created_by_import, run_id),
    )
    assert pr.external_id is not None and pr.patient_id is not None
    resolver.xref[pr.external_id] = pr.patient_id
    if pr.mrn and pr.mrn not in resolver.by_mrn:
        row = db.execute("SELECT * FROM patients WHERE id = ?", (pr.patient_id,)).fetchone()
        if row is not None:
            resolver.by_mrn[pr.mrn] = row


def _open_run(
    db: Any,
    *,
    run_id: str,
    rt: RecordType,
    source_system: str,
    actor_user_id: str | None,
    source: _Source,
    source_filename: str | None,
    mapping: dict[str, str],
    date_order: str,
    note: str | None,
    started: str,
    facility_id: int = 1,
) -> None:
    """Open the run ledger row with zero counts, before any row is processed.

    Exists so rejects can be written as they are found rather than accumulated
    in memory (they carry a foreign key to this row). ``finished_at`` stays NULL
    until _record_run fills it in — and because both happen in ONE transaction,
    a NULL finished_at is never observable: either the whole run committed or
    none of it did.

    ``facility_id`` (migration 74) is the facility the run is at: 1 while one
    facility is on record.
    """
    db.execute(
        """INSERT INTO history_import_runs
               (id, record_type, source_system, actor_user_id, source_filename,
                source_sha256, source_bytes, mapping_json, date_order, note, started_at,
                facility_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            run_id,
            rt.name,
            source_system,
            actor_user_id,
            source_filename,
            source.sha256,
            source.size,
            json.dumps(mapping),
            date_order,
            note,
            started,
            facility_id,
        ),
    )


def _flush_rejects(db, run_id: str, buffer: list[dict]) -> None:
    """Persist and clear a batch of rejected rows.

    The HTTP response caps its reject detail; this never does. A migration that
    silently drops 4% of records is a catastrophe found months later by a
    clinician looking for something that is simply not there — this table is the
    complete work list that makes that impossible to miss.
    """
    if not buffer:
        return
    db.executemany(
        """INSERT INTO history_import_rejects
               (run_id, line, severity, code, field, message, external_id,
                external_patient_id, patient_id, raw_row)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [
            (
                run_id,
                r["line"],
                r["severity"],
                r["code"],
                r["field"],
                r["message"],
                r["external_id"],
                r["external_patient_id"],
                r["patient_id"],
                r["raw_row"],
            )
            for r in buffer
        ],
    )
    buffer.clear()


def _record_run(db, *, run_id: str, report: _Report, written: dict[str, int]) -> None:
    """Finalise the run ledger with the exact totals. Same transaction as the data."""
    summary = {
        "created": written["created"],
        "updated": written["updated"],
        "unchanged": report.counts["unchanged"],
        "failed": report.counts["failed"],
        "warnings": report.counts["warnings"],
        "issue_summary": report.issue_summary,
        "still_open_in_source": len(report.still_open_in_source),
    }
    db.execute(
        """UPDATE history_import_runs
              SET rows_total = ?, rows_created = ?, rows_updated = ?,
                  rows_unchanged = ?, rows_failed = ?, warning_count = ?,
                  summary_json = ?, finished_at = ?
            WHERE id = ?""",
        (
            report.counts["total_rows"],
            written["created"],
            written["updated"],
            report.counts["unchanged"],
            report.counts["failed"],
            report.counts["warnings"],
            json.dumps(summary),
            datetime.now(UTC).isoformat(),
            run_id,
        ),
    )


# ══════════════════════════════════════════════════════════════════════════
#  Preview + history
# ══════════════════════════════════════════════════════════════════════════


def sniff(source: Any, record_type: str, *, header_line: int | None = None) -> dict:
    """Inspect an upload without importing anything. Reads no patient data.

    Unlike ``plan`` this NEVER refuses on an ambiguous header — it is the tool an
    admin uses to SEE the candidate header lines and pick one, so it always
    answers, with ``header_confident: false`` when it could not decide.
    """
    rt = _record_type(record_type)
    src = _Source(source)
    header = _locate_header(src, rt, header_line)
    issues = list(src.issues) + list(header.issues)

    headers = header.fields
    blank = [i + 1 for i, h in enumerate(headers) if not h]
    if blank:
        issues.append(
            Issue(
                header.index + 1,
                "blank_header",
                "warning",
                f"Column(s) {blank} have no header name and cannot be mapped.",
            )
        )
    dupes = sorted({h for h in headers if h and headers.count(h) > 1})
    if dupes:
        issues.append(
            Issue(
                header.index + 1,
                "duplicate_header",
                "warning",
                f"Duplicate column header(s) {dupes}; only the LAST column with "
                f"each name is readable.",
            )
        )

    mapping = suggest_mapping(headers, rt.name)
    samples: list[dict] = []
    total = 0
    for _, raw, rejection, _raw_text in _iter_rows(src, header):
        if not raw:
            continue
        total += 1
        # A rejected row's columns are by definition in the wrong places;
        # showing them would teach the admin the wrong shape.
        if rejection is None and len(samples) < 5:
            samples.append({h: raw.get(h, "") for h in headers if h})
        if total >= 2000:
            break

    return {
        "record_type": rt.name,
        "delimiter": header.delimiter,
        "headers": headers,
        "header_line": header.index + 1,
        "header_confident": header.confident,
        "header_candidates": header.candidates,
        "suggested_mapping": mapping,
        "unmapped_headers": [h for h in headers if h and h not in mapping.values()],
        "missing_required": [f for f in rt.required if f not in mapping],
        "sample_rows": samples,
        "sampled_rows": total,
        "source_sha256": src.sha256,
        "source_bytes": src.size,
        "issues": [i.to_dict() for i in issues],
    }


def list_runs(limit: int = 50, record_type: str | None = None) -> list[dict]:
    """Applied-import history, newest first. Dry runs are not recorded. Each
    run's facility (migration 74) is listed only while more than one facility
    is on record, so the list is exactly as before on a one-facility appliance."""
    from services import facility_service

    limit = max(1, min(int(limit), 500))
    sql = "SELECT * FROM history_import_runs"
    params: list[Any] = []
    if record_type:
        sql += " WHERE record_type = ?"
        params.append(record_type)
    sql += " ORDER BY started_at DESC, rowid DESC LIMIT ?"
    params.append(limit)
    with db_session() as db:
        rows = db.execute(sql, tuple(params)).fetchall()
        multi = facility_service.multi_facility_data(db=db)
    out = []
    for r in rows:
        d = dict(r)
        if not multi:
            d.pop("facility_id", None)
        for jf in ("mapping_json", "summary_json"):
            try:
                d[jf] = json.loads(d[jf]) if d.get(jf) else None
            except (json.JSONDecodeError, TypeError):
                d[jf] = None
        out.append(d)
    return out


def list_rejects(run_id: str, limit: int = 500, offset: int = 0) -> dict:
    """The COMPLETE, durable list of rows a run did not import.

    This is the work list a migration hands back to the hospital. It is paged
    rather than capped, so nothing here is unreachable however large the file.
    """
    limit = max(1, min(int(limit), 5000))
    offset = max(0, int(offset))
    with db_session() as db:
        run = db.execute("SELECT * FROM history_import_runs WHERE id = ?", (run_id,)).fetchone()
        if run is None:
            return {}
        total = db.execute(
            "SELECT COUNT(*) c FROM history_import_rejects WHERE run_id = ?", (run_id,)
        ).fetchone()["c"]
        rows = db.execute(
            "SELECT * FROM history_import_rejects WHERE run_id = ? ORDER BY line LIMIT ? OFFSET ?",
            (run_id, limit, offset),
        ).fetchall()
        by_code = db.execute(
            "SELECT code, COUNT(*) c FROM history_import_rejects WHERE run_id = ? "
            "GROUP BY code ORDER BY c DESC",
            (run_id,),
        ).fetchall()
    return {
        "run_id": run_id,
        "record_type": run["record_type"],
        "source_system": run["source_system"],
        "source_filename": run["source_filename"],
        "count": total,
        "limit": limit,
        "offset": offset,
        "by_code": {r["code"]: r["c"] for r in by_code},
        "rejects": [dict(r) for r in rows],
    }
