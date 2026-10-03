"""Orb — HIS roster import (tolerant CSV → patients).

On day one of shadow mode Orb must know who is on the ward, and the incumbent is
a "basic HIS" whose export format is not known in advance. So this importer is
built around three properties rather than around one assumed schema:

  1. TOLERANT PARSE — sniff the delimiter, tolerate a UTF-8 BOM, CRLF, quoted
     fields, ragged rows, blank rows and junk preamble lines. Never assume a
     column name.
  2. EXPLICIT MAPPING — the caller supplies ``{orb_field: csv_header}``. We
     *suggest* a mapping from header synonyms, but nothing is imported off a
     guess: the admin confirms the mapping in the UI before any write.
  3. DRY RUN FIRST — ``plan()`` reports exactly what would be created, updated
     and skipped, with a per-row issue list, and touches nothing. ``apply()``
     re-plans from the same input and executes that plan in ONE transaction.

REPORT, DO NOT COERCE
─────────────────────
Real hospital exports are dirty. A silently mis-imported patient is a clinical
safety issue, so every departure from clean data is surfaced as an ``Issue``
(``error`` = row not imported, ``warning`` = imported with a named caveat) and
the offending FIELD is left NULL rather than guessed at. Specifically:

  * ambiguous dates (03/04/2026 — is that 3 April or 4 March?) are only accepted
    when the ``date_order`` hint disambiguates them, otherwise reported;
  * an unrecognised sex/gender token is reported, not mapped to a default;
  * an out-of-range or non-numeric age is reported, not clamped;
  * duplicate MRNs inside one file are reported and only the FIRST row is used —
    later rows are never merged into the first;
  * MRNs that differ only by case/whitespace are reported as a possible
    duplicate rather than silently unified (case-folding two distinct hospital
    MRNs together would merge two patients' records).

IDEMPOTENCY
───────────
The natural key is ``patients.mrn`` (UNIQUE in the schema). Re-running the same
file produces zero creates and zero updates — an existing patient whose CSV
values already match is classified ``unchanged``. Updates are FIELD-WISE and
additive-only: a column absent from the mapping, or blank in the file, never
blanks out a value Orb already holds.

ADMISSIONS — "IMPORTED" MUST MEAN "ON THE BOARD"
────────────────────────────────────────────────
A ``patients`` row on its own is INVISIBLE. Both readers of the ward — Vigil's
patient board (routes/vigil_routes.py) and the nurse shift board
(services/nurse_shift_service.py) — INNER JOIN ``admissions`` on
``status = 'active'``. An import that writes only ``patients`` therefore reports
success and puts nobody on any screen. So the encounter is opened here too, in
the SAME transaction, and four questions had to be answered:

  * RE-IMPORT — an open admission is never duplicated. The check is repeated
    inside the write transaction, not just in the plan, so two admins committing
    the same roster at the same moment cannot open two encounters for one
    patient.
  * WARD/BED CHANGED between exports — that is a MOVE within one hospital stay,
    not a new stay, so the OPEN ADMISSION IS UPDATED IN PLACE. Closing it and
    opening another would fabricate a discharge the hospital never performed,
    detach the notes/medications/channels that hang off the encounter id, and
    split one length of stay into two. The move is reported as a
    ``ward_transfer`` warning so it is visible rather than silent.
  * ABSENT FROM A LATER ROSTER — deliberately NOT treated as a discharge. A
    partial export (one ward, a filtered report, a truncated file) would
    otherwise empty the board of real inpatients — the same failure as importing
    nobody, only harder to notice, because the import would report success.
    Patients holding an open admission in a ward the file covers but having no
    row in the file are LISTED under ``absent_from_roster`` for a human to act
    on; Orb writes nothing. A discharge needs an explicit status column in the
    file, or the discharge workflow.
  * DEPARTMENT — both boards filter on ``admissions.department``, which is a
    clinical service ('General Medicine'), not a ward ('GM-1'). Map a department
    column if the export has one, else pass ``default_department`` for the whole
    file. Failing both, the ward name is used so the column is at least not
    NULL (a NULL department is invisible to every user who has a department on
    record), and the exact department that will be written is shown per row in
    the dry run.

WHAT THIS DOES NOT DO
─────────────────────
It does not create Bridge portal logins (see services/admission_service.py — that
path mints a one-time password that has to be handed to the patient), and it does
not discharge anybody. Roster import answers "who is on the ward"; leaving is a
deliberate clinical action.
"""
from __future__ import annotations

import csv
import io
import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable

from database import db_session
from services.clinical_records_service import allergy_entries, record_allergies_on

# The pilot hospital is in India. A roster gives a DATE ("28/07/2026"); the
# admissions row wants an instant, and every other row in that column is UTC
# ISO-8601. "28 July" means 28 July *in India*, so a date-only admission date is
# anchored at IST midnight and converted to UTC — stamping UTC midnight instead
# would file a patient who arrived at 02:00 IST as having arrived the day before.
IST = timezone(timedelta(hours=5, minutes=30))

# ── Limits ───────────────────────────────────────────────────────────────────
# A roster for 2 wards / 20-60 beds is tens of rows; the cap is a DoS guard, not
# a product limit. Both are checked before any DB work happens.
MAX_CSV_BYTES = 5 * 1024 * 1024
MAX_ROWS = 20_000

# Plausible human age range. Outside this the value is reported, never clamped.
MIN_AGE = 0
MAX_AGE = 130


# ══════════════════════════════════════════════════════════════════════════
#  Target field catalogue
# ══════════════════════════════════════════════════════════════════════════
# `required` fields must resolve to a non-empty value or the row is an error.
# `synonyms` drive mapping SUGGESTION only — never automatic import.

@dataclass(frozen=True)
class TargetField:
    name: str
    label: str
    required: bool
    kind: str                     # text | int | date | enum | mrn
    synonyms: tuple[str, ...]
    note: str = ""


TARGET_FIELDS: tuple[TargetField, ...] = (
    TargetField("mrn", "MRN / Hospital number", True, "mrn",
                ("mrn", "mr no", "mrno", "mr number", "hospital number", "hospital no",
                 "hosp no", "hosp number", "uhid no", "uhid number",
                 "hospno", "uhid", "patient id", "patientid", "pid", "reg no",
                 "regno", "registration number", "ip no", "ipno", "ip number",
                 "ipd no", "file no", "case no", "cr no", "crno"),
                "Natural key. Re-import matches on this."),
    TargetField("name", "Patient name", True, "text",
                ("name", "patient name", "patientname", "full name", "fullname",
                 "pt name", "ptname", "patient"),
                "Given+family as the HIS holds it; initials are accepted."),
    TargetField("ward", "Ward", False, "text",
                ("ward", "ward name", "unit", "ward/unit", "location", "room"),
                "The physical ward. Written to the patient AND to the admission."),
    TargetField("department", "Department / speciality", False, "text",
                ("department", "dept", "speciality", "specialty", "service",
                 "department name", "clinical unit", "under dept"),
                "Written to the ADMISSION, not to the patient row. This is what "
                "department-scoped boards (Vigil, the nurse shift board) filter on, "
                "so a wrong or missing value hides the patient from staff in that "
                "department. Defaults to `default_department`, then to the ward."),
    TargetField("bed", "Bed", False, "text",
                ("bed", "bed no", "bedno", "bed number", "bed_no", "cot"), ""),
    TargetField("age", "Age (years)", False, "int",
                ("age", "age (y)", "age_years", "age in years", "yrs", "years"),
                "Ignored when date_of_birth is mapped and parses."),
    TargetField("date_of_birth", "Date of birth", False, "date",
                ("dob", "date of birth", "birth date", "birthdate", "d.o.b",
                 "d.o.b.", "dateofbirth"), ""),
    TargetField("sex", "Sex", False, "enum",
                ("sex", "gender", "m/f", "sex/gender"), "Normalised to M / F / O."),
    TargetField("phone", "Phone", False, "text",
                ("phone", "mobile", "contact", "contact no", "contact number",
                 "phone no", "phoneno", "mobile no", "cell"), ""),
    TargetField("address", "Address", False, "text",
                ("address", "addr", "residence"), ""),
    TargetField("diagnosis", "Diagnosis", False, "text",
                ("diagnosis", "provisional diagnosis", "dx", "final diagnosis",
                 "chief complaint", "complaint", "reason for admission"), ""),
    TargetField("admission_date", "Admission date", False, "date",
                ("admission date", "admitted on", "doa", "date of admission",
                 "admit date", "admissiondate", "adm date"), ""),
    TargetField("attending_doctor_id", "Attending doctor (Orb user id)", False, "text",
                ("attending", "attending doctor", "consultant", "doctor id",
                 "doctor", "primary physician", "under care of"),
                "Must be an existing Orb user id; unknown ids are reported, not created."),
    TargetField("emergency_contact_name", "Emergency contact name", False, "text",
                ("emergency contact", "emergency contact name", "next of kin",
                 "nok", "kin name", "attendant name"), ""),
    TargetField("emergency_contact_phone", "Emergency contact phone", False, "text",
                ("emergency contact phone", "nok phone", "kin phone",
                 "attendant contact", "emergency phone"), ""),
    TargetField("status", "Status", False, "enum",
                ("status", "patient status", "admission status"),
                "Normalised to admitted / discharged / transferred."),
    TargetField("allergies", "Allergies", False, "list",
                ("allergies", "allergy", "known allergies", "drug allergies",
                 "drug allergy", "allergic to", "allergy details"),
                "Separate several with ';' or ','. Charted as active allergies "
                "that the prescribing and bedside allergy checks screen against. "
                "Never removes or changes an allergy already on the chart."),
)

_FIELDS_BY_NAME: dict[str, TargetField] = {f.name: f for f in TARGET_FIELDS}
REQUIRED_FIELDS: tuple[str, ...] = tuple(f.name for f in TARGET_FIELDS if f.required)

# patients.status CHECK constraint — kept in sync with database.py SCHEMA.
_STATUS_VALUES = {"admitted", "discharged", "transferred"}
_STATUS_SYNONYMS = {
    "admitted": "admitted", "admit": "admitted", "active": "admitted",
    "inpatient": "admitted", "ip": "admitted", "in": "admitted",
    "current": "admitted", "occupied": "admitted",
    "discharged": "discharged", "discharge": "discharged", "dc": "discharged",
    "d/c": "discharged", "out": "discharged", "closed": "discharged",
    "transferred": "transferred", "transfer": "transferred", "xfer": "transferred",
    "referred out": "transferred",
}

_SEX_SYNONYMS = {
    "m": "M", "male": "M", "man": "M", "boy": "M", "1": "M",
    "f": "F", "female": "F", "woman": "F", "girl": "F", "2": "F",
    "o": "O", "other": "O", "third gender": "O", "transgender": "O",
    "tg": "O", "3": "O", "x": "O",
}

# Tokens a basic HIS writes to mean "nothing here". Treated as blank, and — for
# a REQUIRED field — reported as a placeholder rather than imported verbatim.
_NULL_TOKENS = {"", "-", "--", "n/a", "na", "nil", "null", "none", "unknown",
                "not known", "not available", "?", ".", "#n/a", "#value!"}


def field_catalogue() -> list[dict]:
    """Serialisable target-field catalogue — drives the UI mapping step."""
    return [
        {"name": f.name, "label": f.label, "required": f.required,
         "kind": f.kind, "note": f.note}
        for f in TARGET_FIELDS
    ]


# ══════════════════════════════════════════════════════════════════════════
#  Issues
# ══════════════════════════════════════════════════════════════════════════

@dataclass
class Issue:
    """One thing that was wrong with the input, tied to a physical line number.

    ``severity='error'`` means the row is NOT imported. ``severity='warning'``
    means it is imported, with the named field left NULL or the named caveat.
    """
    line: int | None
    code: str
    severity: str
    message: str
    field: str | None = None
    value: str | None = None

    def to_dict(self) -> dict:
        return {"line": self.line, "code": self.code, "severity": self.severity,
                "message": self.message, "field": self.field, "value": self.value}


# ══════════════════════════════════════════════════════════════════════════
#  Value normalisation  (each returns (value, issues))
# ══════════════════════════════════════════════════════════════════════════

def _clean(raw: Any) -> str:
    """Trim, collapse internal whitespace, drop 'null' placeholder tokens."""
    if raw is None:
        return ""
    s = re.sub(r"\s+", " ", str(raw)).strip()
    if s.lower() in _NULL_TOKENS:
        return ""
    return s


def _normalise_mrn(raw: str) -> str:
    """Whitespace-normalise only.

    Deliberately NOT case-folded or stripped of punctuation: two MRNs that
    differ only in case may be two different patients in a hospital that uses a
    case-significant numbering scheme. Near-collisions are REPORTED by
    ``_detect_near_duplicate_mrns`` instead of being merged.
    """
    return re.sub(r"\s+", "", raw or "")


def _parse_int(value: str, fname: str, line: int,
               lo: int, hi: int) -> tuple[int | None, list[Issue]]:
    if not value:
        return None, []
    cleaned = value.replace(",", "").strip()
    m = re.fullmatch(r"(\d+)\s*(?:y|yr|yrs|year|years|/m|m|d)?", cleaned, re.IGNORECASE)
    if not m:
        return None, [Issue(line, "unparseable_number", "warning",
                            f"{fname} '{value}' is not a number, left blank rather than guessed.",
                            fname, value)]
    n = int(m.group(1))
    if n < lo or n > hi:
        return None, [Issue(line, "out_of_range", "warning",
                            f"{fname} {n} is outside the plausible range {lo}-{hi}, left blank.",
                            fname, value)]
    return n, []


# Accepted date layouts. Two-digit years are rejected outright rather than
# century-guessed (a DOB of '55' could be 1955 or 2055).
_DATE_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"^(\d{4})-(\d{1,2})-(\d{1,2})$", "ymd"),
    (r"^(\d{4})/(\d{1,2})/(\d{1,2})$", "ymd"),
    (r"^(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{4})$", "ambiguous"),
)
_MONTH_NAMES = {m.lower(): i for i, m in enumerate(
    ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
     "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"), start=1)}


# Issue codes for a date that was in the file but is imported blank.
_DATE_BLANKED_CODES = frozenset({
    "ambiguous_date", "invalid_date", "two_digit_year", "unrecognised_date_format",
})


def _parse_date(value: str, fname: str, line: int,
                date_order: str) -> tuple[str | None, list[Issue]]:
    """Parse one date into YYYY-MM-DD.

    ``date_order`` is 'dmy', 'mdy' or 'auto'. Under 'auto' a value such as
    03/04/2026 that is valid BOTH ways is refused and reported — importing it
    would silently pick a birth date that could be a month out.
    """
    if not value:
        return None, []
    raw = value.strip()
    # Drop a trailing clock time ("2026-07-30 09:15", "30/07/2026 09:15:00").
    raw = re.sub(r"[ T]\d{1,2}:\d{2}(:\d{2})?(\s*[AaPp][Mm])?$", "", raw).strip()

    # 12-Mar-2026 / 12 March 2026 / Mar 12, 2026 — unambiguous, take them first.
    m = re.fullmatch(r"(\d{1,2})[\s\-/]*([A-Za-z]{3,9})[\s\-/,]*(\d{4})", raw)
    if m and m.group(2)[:3].lower() in _MONTH_NAMES:
        return _build_date(int(m.group(3)), _MONTH_NAMES[m.group(2)[:3].lower()],
                           int(m.group(1)), fname, line, value)
    m = re.fullmatch(r"([A-Za-z]{3,9})[\s\-/]*(\d{1,2})[\s\-/,]*(\d{4})", raw)
    if m and m.group(1)[:3].lower() in _MONTH_NAMES:
        return _build_date(int(m.group(3)), _MONTH_NAMES[m.group(1)[:3].lower()],
                           int(m.group(2)), fname, line, value)

    for pattern, kind in _DATE_PATTERNS:
        m = re.fullmatch(pattern, raw)
        if not m:
            continue
        if kind == "ymd":
            return _build_date(int(m.group(1)), int(m.group(2)), int(m.group(3)),
                               fname, line, value)
        a, b, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
        dmy_ok = _valid_ymd(year, b, a)
        mdy_ok = _valid_ymd(year, a, b)
        if date_order == "dmy" and dmy_ok:
            return _build_date(year, b, a, fname, line, value)
        if date_order == "mdy" and mdy_ok:
            return _build_date(year, a, b, fname, line, value)
        if date_order in ("dmy", "mdy"):
            return None, [Issue(line, "invalid_date", "warning",
                                f"{fname} '{value}' is not a valid date in {date_order.upper()} order, left blank.",
                                fname, value)]
        # auto
        if dmy_ok and mdy_ok and a != b:
            return None, [Issue(line, "ambiguous_date", "warning",
                                f"{fname} '{value}' is ambiguous (could be D/M/Y or M/D/Y), left blank. "
                                f"Re-run with an explicit date order to import it.",
                                fname, value)]
        if dmy_ok:
            return _build_date(year, b, a, fname, line, value)
        if mdy_ok:
            return _build_date(year, a, b, fname, line, value)
        return None, [Issue(line, "invalid_date", "warning",
                            f"{fname} '{value}' is not a valid calendar date, left blank.",
                            fname, value)]

    if re.fullmatch(r"\d{1,2}[/\-.]\d{1,2}[/\-.]\d{2}", raw):
        return None, [Issue(line, "two_digit_year", "warning",
                            f"{fname} '{value}' uses a two-digit year: the century is a guess, so it was left blank.",
                            fname, value)]
    return None, [Issue(line, "unrecognised_date_format", "warning",
                        f"{fname} '{value}' is not in a recognised date format, left blank.",
                        fname, value)]


def _valid_ymd(y: int, m: int, d: int) -> bool:
    try:
        date(y, m, d)
        return True
    except ValueError:
        return False


def _build_date(y: int, m: int, d: int, fname: str, line: int,
                original: str) -> tuple[str | None, list[Issue]]:
    if not _valid_ymd(y, m, d):
        return None, [Issue(line, "invalid_date", "warning",
                            f"{fname} '{original}' is not a valid calendar date, left blank.",
                            fname, original)]
    parsed = date(y, m, d)
    if parsed > date.today():
        return parsed.isoformat(), [Issue(line, "future_date", "warning",
                                          f"{fname} '{original}' is in the future, imported as-is; verify against the HIS.",
                                          fname, original)]
    return parsed.isoformat(), []


def _parse_sex(value: str, line: int) -> tuple[str | None, list[Issue]]:
    if not value:
        return None, []
    key = value.strip().lower().rstrip(".")
    if key in _SEX_SYNONYMS:
        return _SEX_SYNONYMS[key], []
    return None, [Issue(line, "unrecognised_sex", "warning",
                        f"sex '{value}' was not recognised, left blank rather than defaulted.",
                        "sex", value)]


def _parse_status(value: str, line: int) -> tuple[str | None, list[Issue]]:
    if not value:
        return None, []
    key = value.strip().lower()
    if key in _STATUS_VALUES:
        return key, []
    if key in _STATUS_SYNONYMS:
        return _STATUS_SYNONYMS[key], []
    return None, [Issue(line, "unrecognised_status", "warning",
                        f"status '{value}' was not recognised, defaulting to 'admitted' for a roster row.",
                        "status", value)]


_NAME_PLACEHOLDERS = {"baby of", "b/o", "unknown", "unnamed", "test", "test patient",
                      "xxx", "abc", "dummy"}


def _check_name(value: str, line: int) -> list[Issue]:
    """Name quality checks. Never rewrites the name — initials are legitimate."""
    issues: list[Issue] = []
    stripped = value.replace(".", "").replace(" ", "")
    if value.lower() in _NAME_PLACEHOLDERS:
        issues.append(Issue(line, "placeholder_name", "warning",
                            f"name '{value}' looks like a placeholder, imported verbatim; confirm against the HIS.",
                            "name", value))
    elif not re.search(r"[A-Za-zऀ-ॿ]", value):
        issues.append(Issue(line, "name_without_letters", "warning",
                            f"name '{value}' contains no letters, imported verbatim; confirm against the HIS.",
                            "name", value))
    elif len(stripped) <= 3 and re.fullmatch(r"[A-Za-z.\s]+", value):
        issues.append(Issue(line, "initials_only_name", "warning",
                            f"name '{value}' appears to be initials only, imported verbatim, "
                            f"but a bedside identity check needs a full name.",
                            "name", value))
    return issues


# ══════════════════════════════════════════════════════════════════════════
#  Sniffing / mapping suggestion
# ══════════════════════════════════════════════════════════════════════════

# Byte-order marks that mean "this really is UTF-16/32". The 4-byte UTF-32 marks
# are listed because b"\xff\xfe\x00\x00" also *starts* with the UTF-16-LE mark —
# they are checked so the shape is explicit, not so UTF-32 is separately decoded.
_UTF16_BOMS = (b"\xff\xfe", b"\xfe\xff")
_UTF32_BOMS = (b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff")


def _decode(content: bytes | str) -> tuple[str, list[Issue]]:
    """Decode to text, tolerating a BOM and a non-UTF-8 export.

    UTF-16 IS ONLY CONSIDERED WHEN THERE IS A BYTE-ORDER MARK, and that is the
    whole subtlety here. Python's 'utf-16' codec without a BOM assumes
    little-endian and raises only on an odd byte count or an unpaired surrogate,
    so an ordinary cp1252 CSV of EVEN length DECODES SUCCESSFULLY into CJK
    mojibake — 'MRN,Name,Ward' comes back as '前ⱎ慎敭圬牡'. Trying utf-16 ahead
    of cp1252 without a BOM check therefore does not fail loudly on a legacy
    Windows HIS export; it silently corrupts every name, MRN and date in the
    file, and because the header row is then unrecognisable the importer refuses
    with an AMBIGUOUS-HEADER error — pointing the admin at entirely the wrong
    cause. Whether it happens at all is a coin flip on file length, which is
    exactly why the plain cp1252 test passed either way. (Verified against a
    cp1252 fixture 2026-08-07.)

    This mirrors ``history_import_service._detect_encoding``; the two importers
    must agree about what a legacy export is. The one thing NOT carried over is
    that function's tolerance for a UTF-8 error in the final few bytes: it reads
    a 64 KB head, where a trailing error is a multi-byte character sliced by the
    chunk boundary. ``_decode`` is always handed the WHOLE file (both callers
    pass the complete upload), so here a bad byte at the end is a real cp1252
    byte — an unterminated final line — and must fall through, not be replaced.

    Order after the BOM check is deliberate: UTF-8 first (the common case), then
    cp1252 (what a Windows-era Indian HIS actually emits), then latin-1, which
    cannot fail and so is the terminal fallback.
    """
    if isinstance(content, str):
        return content, []

    head = bytes(content[:4])
    if head[:2] in _UTF16_BOMS or head in _UTF32_BOMS:
        # errors="replace" rather than a fall-through: the file has declared
        # itself UTF-16, so a truncated tail is damage to report, not evidence
        # that it was really cp1252 all along.
        return content.decode("utf-16", errors="replace"), [
            Issue(None, "non_utf8_encoding", "warning",
                  "File is UTF-16 (it carries a byte-order mark). "
                  "Check that non-Latin names came through correctly.")]

    for encoding in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            text = content.decode(encoding)
        except (UnicodeDecodeError, UnicodeError):
            continue
        if encoding == "utf-8-sig":
            return text, []
        return text, [Issue(None, "non_utf8_encoding", "warning",
                            f"File is not UTF-8; decoded as {encoding}. "
                            f"Check that non-Latin names came through correctly.")]
    return content.decode("utf-8", errors="replace"), [
        Issue(None, "undecodable_bytes", "warning",
              "File contained bytes that are not valid in any tried encoding; "
              "they were replaced. Names may be corrupted.")]


def _sniff_delimiter(sample: str) -> str:
    try:
        return csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
    except csv.Error:
        # Fall back to whichever candidate appears most on the busiest line.
        counts = {d: sample.count(d) for d in (",", ";", "\t", "|")}
        best = max(counts, key=lambda d: counts[d])
        return best if counts[best] else ","


def _norm_header(h: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (h or "").lower()).strip()


def suggest_mapping(headers: Iterable[str]) -> dict[str, str]:
    """Suggest ``{orb_field: csv_header}`` from header synonyms.

    A SUGGESTION only — nothing is imported until an admin confirms it. Exact
    synonym hits win over substring hits, and each CSV header is offered to at
    most one Orb field so 'emergency contact phone' cannot also land on 'phone'.
    """
    normalised = [(h, _norm_header(h)) for h in headers if h is not None]
    mapping: dict[str, str] = {}
    taken: set[str] = set()

    for exact_pass in (True, False):
        for f in TARGET_FIELDS:
            if f.name in mapping:
                continue
            for original, norm in normalised:
                if original in taken or not norm:
                    continue
                hit = (norm in f.synonyms) if exact_pass else any(
                    s in norm or norm in s for s in f.synonyms if len(s) > 2
                )
                if hit:
                    mapping[f.name] = original
                    taken.add(original)
                    break
    return mapping


def sniff(content: bytes | str, *, header_line: int | None = None) -> dict:
    """Inspect an uploaded file without importing anything.

    Returns delimiter, headers, a suggested mapping, the headers we could not
    place, a few sample rows and any file-level issues. Unlike ``plan`` this
    NEVER refuses on an ambiguous header — it is the tool the admin uses to see
    the candidate header lines and pick one, so it always answers.
    """
    issues: list[Issue] = []
    if isinstance(content, (bytes, bytearray)) and len(content) > MAX_CSV_BYTES:
        raise ValueError(f"File exceeds the {MAX_CSV_BYTES // (1024 * 1024)} MB limit")

    text, decode_issues = _decode(content)
    issues.extend(decode_issues)
    if not text.strip():
        raise ValueError("File is empty")

    header = _locate_header(text, header_line)
    issues.extend(header.issues)
    header_offset, delimiter = header.index, header.delimiter

    reader = csv.reader(io.StringIO(header.line), delimiter=delimiter)
    headers = [h.strip() for h in next(reader, [])]
    blank_headers = [i for i, h in enumerate(headers) if not h]
    if blank_headers:
        issues.append(Issue(header_offset + 1, "blank_header", "warning",
                            f"Column(s) {[i + 1 for i in blank_headers]} have no header name "
                            f"and cannot be mapped."))
    dupes = sorted({h for h in headers if h and headers.count(h) > 1})
    if dupes:
        issues.append(Issue(header_offset + 1, "duplicate_header", "warning",
                            f"Duplicate column header(s) {dupes}; only the LAST column with "
                            f"each name is readable."))

    mapping = suggest_mapping(headers)
    rows, row_issues, _ = _read_rows(text, delimiter, header_offset)
    issues.extend(row_issues)

    # Samples show what WOULD be imported, so a row rejected for a field-count
    # mismatch is counted but not shown — its columns are, by definition, in the
    # wrong places and displaying them would teach the admin the wrong shape.
    usable = [r for r in rows if r[2] is None]
    samples = [{h: row.get(h, "") for h in headers if h} for _, row, _ in usable[:5]]

    return {
        "delimiter": delimiter,
        "headers": headers,
        "header_line": header_offset + 1,
        "header_confident": header.confident,
        "header_candidates": [c.to_dict() for c in header.candidates],
        "row_count": len(rows),
        "rejected_row_count": len(rows) - len(usable),
        "suggested_mapping": mapping,
        "unmapped_headers": [h for h in headers if h and h not in mapping.values()],
        "missing_required": [f for f in REQUIRED_FIELDS if f not in mapping],
        "sample_rows": samples,
        "issues": [i.to_dict() for i in issues],
    }


# ── Header detection ─────────────────────────────────────────────────────────
# Getting this wrong is not a cosmetic error: read a preamble line as the header
# and EVERY column is then read out of the wrong position, so the ward of one
# patient becomes the diagnosis of another. The rule is therefore evidence, not
# optimism — a line is the header only if it names at least two columns Orb
# recognises EXACTLY (not by substring). A report title such as
# "Ward Census Report, printed 30/07/2026" contains the word "ward" and would
# pass a substring test; it names zero columns exactly, so it fails this one.
#
# When no line clears the bar we do not fall back to a guess. The three-step
# flow exists precisely so a human can settle this: `sniff` returns the ranked
# candidates and `plan` refuses until the admin names the line via `header_line`.

_MIN_HEADER_HITS = 2
_HEADER_SEARCH_LINES = 100      # a header is near the top or it is not a header


@dataclass
class HeaderCandidate:
    index: int                  # 0-based line index in the file
    line: str
    fields: list[str]
    exact_hits: tuple[str, ...]
    loose_hits: tuple[str, ...]

    @property
    def named_fields(self) -> int:
        return sum(1 for f in self.fields if f.strip())

    def to_dict(self) -> dict:
        return {"line": self.index + 1, "text": self.line[:200],
                "fields": self.fields,
                "recognised_columns": list(self.exact_hits),
                "possible_columns": list(self.loose_hits)}


@dataclass
class HeaderLocation:
    index: int                  # 0-based line index of the chosen header
    line: str
    delimiter: str
    confident: bool
    candidates: list[HeaderCandidate]
    issues: list[Issue]


def _exact_header_hits(fields: Iterable[str]) -> tuple[str, ...]:
    """Orb fields named EXACTLY by these column headers (one column each)."""
    normalised = [_norm_header(f) for f in fields]
    hits: list[str] = []
    taken: set[int] = set()
    for target in TARGET_FIELDS:
        for i, norm in enumerate(normalised):
            if i in taken or not norm:
                continue
            if norm in target.synonyms:
                hits.append(target.name)
                taken.add(i)
                break
    return tuple(hits)


def _header_candidate(index: int, line: str, delimiter: str) -> HeaderCandidate:
    parts = next(csv.reader(io.StringIO(line), delimiter=delimiter), [])
    fields = [p.strip() for p in parts]
    return HeaderCandidate(index, line, fields,
                           _exact_header_hits(fields),
                           tuple(suggest_mapping(fields)))


def _locate_header(text: str, header_line: int | None = None) -> HeaderLocation:
    """Find the header row, skipping a junk preamble (title/date lines).

    ``header_line`` is a 1-based physical line number that overrides detection —
    the admin's answer when we asked. Otherwise the first line that names
    ``_MIN_HEADER_HITS`` recognised columns exactly wins; if none does, the
    result is marked ``confident=False`` with the candidates attached, and it is
    the CALLER's job to refuse rather than import off a guess.
    """
    lines = text.splitlines()
    delimiter = _sniff_delimiter("\n".join(lines[:20]) or text[:4096])
    issues: list[Issue] = []

    non_blank = [i for i, line in enumerate(lines) if line.strip()]
    if not non_blank:
        raise ValueError("File is empty")

    if header_line is not None:
        idx = int(header_line) - 1
        if idx < 0 or idx >= len(lines) or not lines[idx].strip():
            raise ValueError(
                f"header_line {header_line} is not a usable line: the file has "
                f"{len(lines)} line(s) and line {header_line} is out of range or blank."
            )
        chosen = _header_candidate(idx, lines[idx], delimiter)
        if chosen.named_fields < 2:
            raise ValueError(
                f"header_line {header_line} splits into {chosen.named_fields} named column(s) "
                f"under delimiter '{delimiter}'. That cannot be a header row."
            )
        if not chosen.exact_hits and not chosen.loose_hits:
            issues.append(Issue(
                header_line, "header_line_unrecognised", "warning",
                f"Line {header_line} was used as the header on your instruction, but none of its "
                f"{chosen.named_fields} column name(s) match anything Orb recognises. Check the "
                f"mapping column by column before committing."))
        if idx > 0:
            issues.append(_preamble_issue(idx))
        return HeaderLocation(idx, lines[idx], delimiter, True, [chosen], issues)

    candidates = [_header_candidate(i, lines[i], delimiter)
                  for i in non_blank[:_HEADER_SEARCH_LINES]]
    strong = [c for c in candidates
              if c.named_fields >= 2 and len(set(c.exact_hits)) >= _MIN_HEADER_HITS]
    if strong:
        chosen = strong[0]
        if chosen.index > 0:
            issues.append(_preamble_issue(chosen.index))
        return HeaderLocation(chosen.index, chosen.line, delimiter, True, strong[:5], issues)

    weak = [c for c in candidates if c.named_fields >= 2 and (c.exact_hits or c.loose_hits)]
    ranked = weak or [candidates[0]]
    best = ranked[0]
    issues.append(Issue(
        best.index + 1, "header_ambiguous", "error",
        f"No line in this file clearly looks like a column-header row: a header is expected to name "
        f"at least {_MIN_HEADER_HITS} columns Orb recognises (MRN, Patient Name, Ward, …). Reading "
        f"the wrong line as the header shifts EVERY column into the wrong field, so Orb will not "
        f"guess. Best candidate is line {best.index + 1}: {best.line[:120]!r}. Re-run with "
        f"header_line set to the 1-based line number that holds the column names."))
    return HeaderLocation(best.index, best.line, delimiter, False, ranked[:5], issues)


def _preamble_issue(header_index: int) -> Issue:
    return Issue(header_index, "preamble_skipped", "warning",
                 f"{header_index} line(s) before the header row were ignored "
                 f"(they did not look like column headers).")


def _read_rows(text: str, delimiter: str, header_offset: int
               ) -> tuple[list[tuple[int, dict, Issue | None]], list[Issue], list[str]]:
    """Read data rows as (physical_line_number, {header: value}, rejection).

    Blank and ragged rows are reported, not dropped silently. Line numbers are
    1-based physical lines in the uploaded file so an admin can open the export
    and look straight at the offending row.

    A row with MORE fields than the header and a non-blank surplus is REJECTED
    (third element = an ``error`` Issue), never imported. The reason is that a
    surplus field is indistinguishable from an unquoted delimiter inside a field
    — and Indian addresses carry commas constantly, so
    ``M200,Ramesh Kumar,12 Nehru Street, New Delhi,ICU,B1`` under the header
    ``MRN,Patient Name,Address,Ward,Bed`` reads as ward='New Delhi', bed='ICU'.
    Every column after the break is silently one place to the left; a patient is
    relocated to a ward that does not exist and the import reports success.
    Truncating the surplus does not help — the damage is the SHIFT, not the
    overflow. Too FEW fields is different and stays a warning: nothing shifts,
    the absent trailing columns simply read as blank.
    """
    body = "\n".join(text.splitlines()[header_offset:])
    reader = csv.reader(io.StringIO(body), delimiter=delimiter)
    headers = [h.strip() for h in next(reader, [])]
    width = len(headers)

    rows: list[tuple[int, dict, Issue | None]] = []
    issues: list[Issue] = []
    blank_count = 0

    for offset, parts in enumerate(reader, start=1):
        line_no = header_offset + offset + 1
        if not any(p.strip() for p in parts):
            blank_count += 1
            continue
        if len(rows) >= MAX_ROWS:
            issues.append(Issue(line_no, "row_limit", "error",
                                f"File exceeds the {MAX_ROWS}-row limit; rows from line "
                                f"{line_no} onward were not read."))
            break

        rejection: Issue | None = None
        if len(parts) > width:
            surplus = [p.strip() for p in parts[width:] if p.strip()]
            if surplus:
                rejection = Issue(
                    line_no, "field_count_mismatch", "error",
                    f"Row has {len(parts)} fields but the header has {width}, and the surplus "
                    f"value(s) {surplus[:3]} are not empty, so Orb cannot tell which column each "
                    f"value belongs to. The usual cause is an unquoted '{delimiter}' inside a "
                    f"field (an address such as '12 Nehru Street, New Delhi'), which shifts every "
                    f"later column one place and would file the patient under the wrong ward and "
                    f"bed. The row was NOT imported. Re-export with fields quoted, or fix this row.",
                    value=delimiter.join(parts)[:200])
            else:
                issues.append(Issue(line_no, "trailing_empty_fields", "warning",
                                    f"Row has {len(parts)} fields but the header has {width}; the "
                                    f"surplus field(s) are empty, so nothing shifted and they were "
                                    f"ignored."))
        elif len(parts) < width:
            issues.append(Issue(line_no, "ragged_row", "warning",
                                f"Row has {len(parts)} fields but the header has {width}; "
                                f"missing fields read as blank."))

        row = {headers[i]: (parts[i] if i < len(parts) else "") for i in range(width)}
        rows.append((line_no, row, rejection))

    if blank_count:
        issues.append(Issue(None, "blank_rows", "warning",
                            f"{blank_count} blank row(s) were skipped."))
    return rows, issues, headers


# ══════════════════════════════════════════════════════════════════════════
#  Planning
# ══════════════════════════════════════════════════════════════════════════

@dataclass
class PlannedRow:
    line: int
    mrn: str | None
    name: str | None
    action: str                                  # create | update | unchanged | skip
    reason: str = ""
    patient_id: str | None = None
    values: dict[str, Any] = field(default_factory=dict)   # normalised, non-null only
    changes: dict[str, dict] = field(default_factory=dict)  # field -> {from, to}
    issues: list[Issue] = field(default_factory=list)
    # What happens to this patient's ENCOUNTER — planned and reported separately
    # from the patient row, because the two move independently: a patient whose
    # every field already matches is `unchanged` and may still be missing the
    # admission that puts them on the ward board.
    admission: dict[str, Any] = field(default_factory=dict)
    # Allergies to chart, planned apart from the patient row for the same
    # reason: they live in patient_allergies, and an `unchanged` patient can
    # still arrive with an allergy Orb does not hold.
    allergies: dict[str, list[str]] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"line": self.line, "mrn": self.mrn, "name": self.name,
                "action": self.action, "reason": self.reason,
                "patient_id": self.patient_id, "values": self.values,
                "changes": self.changes, "admission": self.admission,
                "allergies": self.allergies,
                "issues": [i.to_dict() for i in self.issues]}


# Orb columns a roster import is allowed to write. `mrn` is the key, not a
# payload — it is never updated on an existing row. `department` is deliberately
# absent: there is no such column on `patients`, it belongs to the admission.
_WRITABLE = ("name", "ward", "bed", "age", "date_of_birth", "sex", "phone",
             "address", "diagnosis", "admission_date", "attending_doctor_id",
             "emergency_contact_name", "emergency_contact_phone", "status")


def plan(content: bytes | str, mapping: dict[str, str], *,
         date_order: str = "auto", update_existing: bool = True,
         header_line: int | None = None,
         default_department: str | None = None) -> dict:
    """Build the full import plan. Reads the DB; writes NOTHING.

    ``mapping`` is ``{orb_field: csv_header}``. ``date_order`` is 'auto', 'dmy'
    or 'mdy'. With ``update_existing=False`` known MRNs are reported as skipped
    rather than updated (an add-only import).

    ``header_line`` (1-based) pins the header row when detection is not
    confident — without it an ambiguous file is REFUSED rather than guessed at.
    ``default_department`` is the clinical service written to every admission
    that has no department column of its own; see the module docstring for why
    that column decides who can see the patient.
    """
    issues: list[Issue] = []

    if isinstance(content, (bytes, bytearray)) and len(content) > MAX_CSV_BYTES:
        raise ValueError(f"File exceeds the {MAX_CSV_BYTES // (1024 * 1024)} MB limit")
    if date_order not in ("auto", "dmy", "mdy"):
        raise ValueError("date_order must be one of: auto, dmy, mdy")

    default_department = _clean(default_department) or None

    mapping = {k: v for k, v in (mapping or {}).items() if v}
    unknown = [k for k in mapping if k not in _FIELDS_BY_NAME]
    if unknown:
        raise ValueError(f"Unknown target field(s) in mapping: {sorted(unknown)}")
    missing_required = [f for f in REQUIRED_FIELDS if f not in mapping]
    if missing_required:
        raise ValueError(
            f"Mapping is missing required field(s): {missing_required}. "
            f"A roster row cannot be matched or identified without them."
        )

    text, decode_issues = _decode(content)
    issues.extend(decode_issues)
    header = _locate_header(text, header_line)
    issues.extend(header.issues)
    if not header.confident:
        # Ask, do not guess. Importing off the wrong header line writes every
        # column into the wrong field and reports success while doing it.
        raise ValueError(
            f"Could not identify the header row with confidence: no line names at least "
            f"{_MIN_HEADER_HITS} columns Orb recognises. Reading the wrong line as the header "
            f"shifts every column, so nothing was planned. Best candidate is line "
            f"{header.index + 1}: {header.line[:120]!r}. Re-run with header_line=<1-based line "
            f"number> to confirm which line holds the column names "
            f"(the preview step lists the candidates)."
        )
    header_offset, delimiter = header.index, header.delimiter
    rows, row_issues, headers = _read_rows(text, delimiter, header_offset)
    issues.extend(row_issues)

    unresolved = [f"{k} -> '{v}'" for k, v in mapping.items() if v not in headers]
    if unresolved:
        raise ValueError(f"Mapped column(s) not present in the file: {unresolved}")

    # ── Pass 1: normalise every row ─────────────────────────────────────────
    planned: list[PlannedRow] = []
    seen_mrn: dict[str, int] = {}          # exact mrn -> first line
    seen_folded: dict[str, tuple[str, int]] = {}  # casefolded -> (mrn, line)
    mrns_in_file: set[str] = set()         # incl. rejected rows: they ARE in the file

    for line_no, raw, rejection in rows:
        if rejection is not None:
            # The MRN column is almost always column 0, i.e. before the break,
            # so it is worth showing — but the row is not imported and never
            # takes part in duplicate detection or DB matching.
            mrn_cell = _normalise_mrn(_clean(raw.get(mapping.get("mrn", "")))) or None
            if mrn_cell:
                mrns_in_file.add(mrn_cell)
            planned.append(PlannedRow(line_no, mrn_cell, None, "skip",
                                      "field_count_mismatch", issues=[rejection]))
            continue

        pr = _normalise_row(raw, mapping, line_no, date_order)
        if pr.mrn:
            mrns_in_file.add(pr.mrn)
        if pr.action == "skip":
            planned.append(pr)
            continue

        mrn = pr.mrn or ""
        if mrn in seen_mrn:
            pr.action = "skip"
            pr.reason = "duplicate_mrn_in_file"
            pr.issues.append(Issue(line_no, "duplicate_mrn_in_file", "error",
                                   f"MRN '{mrn}' already appeared on line {seen_mrn[mrn]}. "
                                   f"Only the first row is used. The rows were NOT merged.",
                                   "mrn", mrn))
            planned.append(pr)
            continue
        folded = mrn.casefold()
        if folded in seen_folded and seen_folded[folded][0] != mrn:
            other_mrn, other_line = seen_folded[folded]
            pr.issues.append(Issue(line_no, "near_duplicate_mrn", "warning",
                                   f"MRN '{mrn}' differs only by case from '{other_mrn}' on line "
                                   f"{other_line}. Treated as SEPARATE patients: confirm they are not "
                                   f"the same person.", "mrn", mrn))
        seen_mrn[mrn] = line_no
        seen_folded.setdefault(folded, (mrn, line_no))
        planned.append(pr)

    # ── Pass 2: resolve against the DB in one session ───────────────────────
    candidate_mrns = [p.mrn for p in planned if p.action != "skip" and p.mrn]
    existing = _load_existing(candidate_mrns)
    open_admissions = _load_open_admissions([r["id"] for r in existing.values()])
    doctor_ids = {p.values.get("attending_doctor_id") for p in planned
                  if p.values.get("attending_doctor_id")}
    known_doctors = _load_users(doctor_ids)
    charted = _load_allergies([r["id"] for r in existing.values()])

    for pr in planned:
        if pr.action == "skip":
            continue

        did = pr.values.get("attending_doctor_id")
        if did and did not in known_doctors:
            pr.issues.append(Issue(pr.line, "unknown_attending_doctor", "warning",
                                   f"attending_doctor_id '{did}' is not an Orb user, left blank. "
                                   f"Create the user first, then re-run the import to attach them.",
                                   "attending_doctor_id", did))
            pr.values.pop("attending_doctor_id", None)
        elif did and known_doctors[did] not in ("doctor", "clinician"):
            pr.issues.append(Issue(pr.line, "attending_not_a_doctor", "warning",
                                   f"attending_doctor_id '{did}' is an Orb user with role "
                                   f"'{known_doctors[did]}', not a doctor, linked anyway; verify.",
                                   "attending_doctor_id", did))

        row = existing.get(pr.mrn)
        if row is None:
            pr.action = "create"
            _plan_admission(pr, None, None, default_department)
            _plan_allergies(pr, None, charted)
            continue

        pr.patient_id = row["id"]
        if not update_existing:
            pr.action = "skip"
            pr.reason = "already_exists"
            pr.issues.append(Issue(pr.line, "already_exists", "warning",
                                   f"MRN '{pr.mrn}' already exists (patient {row['id']}) and "
                                   f"update_existing is off, row skipped."))
            continue

        changes: dict[str, dict] = {}
        for fname, new_value in pr.values.items():
            if fname not in _WRITABLE:
                continue
            current = row[fname] if fname in row.keys() else None
            if _same(current, new_value):
                continue
            changes[fname] = {"from": current, "to": new_value}
        pr.changes = changes
        pr.action = "update" if changes else "unchanged"
        if not changes:
            pr.reason = "no_field_changed"
        _plan_admission(pr, row, open_admissions.get(row["id"]), default_department)
        _plan_allergies(pr, row, charted)

    counts = {a: sum(1 for p in planned if p.action == a)
              for a in ("create", "update", "unchanged", "skip")}
    counts["total_rows"] = len(planned)
    counts["errors"] = sum(1 for p in planned
                           for i in p.issues if i.severity == "error")
    counts["warnings"] = (sum(1 for p in planned for i in p.issues if i.severity == "warning")
                          + sum(1 for i in issues if i.severity == "warning"))
    for adm_action in ("create", "update", "close"):
        counts[f"admissions_{adm_action}"] = sum(
            1 for p in planned if p.admission.get("action") == adm_action)
    counts["allergies_add"] = sum(len(p.allergies.get("add", [])) for p in planned
                                  if p.action != "skip")
    # Dates the file gave that will be written BLANK. The admin UI holds Apply
    # on this until a date order is chosen or the blanks are accepted: the
    # warnings used to be counted and never shown, and a blank DOB is a
    # discharge link that cannot open.
    counts["dates_left_blank"] = sum(1 for p in planned if p.action != "skip"
                                     for i in p.issues if i.code in _DATE_BLANKED_CODES)

    return {
        "delimiter": delimiter,
        "mapping": mapping,
        "date_order": date_order,
        "update_existing": update_existing,
        "header_line": header_offset + 1,
        "default_department": default_department,
        "counts": counts,
        "rows": [p.to_dict() for p in planned],
        "file_issues": [i.to_dict() for i in issues],
        "absent_from_roster": _absent_from_roster(planned, mrns_in_file),
    }


# ══════════════════════════════════════════════════════════════════════════
#  Admissions — the difference between "imported" and "on the ward board"
# ══════════════════════════════════════════════════════════════════════════

def _resolve_department(values: dict, patient_row: Any,
                        default_department: str | None) -> tuple[str | None, str]:
    """The department to file the encounter under, and where it came from.

    Both ward boards filter on ``admissions.department``, so NULL here means the
    patient is invisible to every user who has a department on record. The ward
    is the last resort rather than the first choice because a ward ('GM-1') is
    not a service ('General Medicine') — but a ward name at least gives the
    column a value an admin can see and correct.
    """
    from_csv = values.get("department")
    if from_csv:
        return from_csv, "csv"
    if default_department:
        return default_department, "default"
    ward = values.get("ward") or _row_value(patient_row, "ward")
    if ward:
        return ward, "ward"
    return None, "none"


def _row_value(row: Any, column: str) -> Any:
    if row is None:
        return None
    try:
        return row[column] if column in row.keys() else None
    except (TypeError, AttributeError):
        return (row or {}).get(column)


def _admission_timestamp(admission_date: str | None) -> str:
    """The instant to stamp on the admissions row, as UTC ISO-8601.

    The roster gives a DATE; the column holds an instant and every other row in
    it is UTC. The hospital is in India, so the date is anchored at IST midnight
    (see the IST constant) rather than UTC midnight, which would file everyone
    admitted before 05:30 IST on the previous day.
    """
    if admission_date:
        try:
            d = date.fromisoformat(admission_date)
        except (ValueError, TypeError):
            d = None
        if d is not None:
            return datetime(d.year, d.month, d.day, tzinfo=IST).astimezone(timezone.utc).isoformat()
    return datetime.now(timezone.utc).isoformat()


def _plan_admission(pr: PlannedRow, patient_row: Any, open_adm: Any,
                    default_department: str | None) -> None:
    """Decide what happens to this patient's encounter. Sets ``pr.admission``.

    Independent of the patient action on purpose: a patient imported before this
    module opened admissions is ``unchanged`` on every re-run and still needs the
    encounter that puts them on the board, so a repair run fixes them.
    """
    values = pr.values
    csv_status = values.get("status")           # present only if the file said so
    db_status = _row_value(patient_row, "status")
    status = csv_status or db_status or "admitted"
    department, dept_source = _resolve_department(values, patient_row, default_department)

    if csv_status and csv_status != "admitted":
        # The file EXPLICITLY says this patient is no longer an inpatient. That is
        # a statement, not an inference from absence, so it is safe to act on.
        if open_adm is None:
            pr.admission = {"action": "none",
                            "reason": f"file gives status '{csv_status}' and no admission is open"}
            return
        pr.admission = {
            "action": "close",
            "admission_id": open_adm["id"],
            "to_status": "discharged" if csv_status == "discharged" else "transferred",
        }
        pr.issues.append(Issue(
            pr.line, "admission_closed", "warning",
            f"The file gives status '{csv_status}', so the open admission {open_adm['id']} is being "
            f"closed. The patient leaves the ward board.", "status", csv_status))
        return

    if status != "admitted":
        # Orb holds them as discharged and the file does not contradict it. Being
        # listed in an export is not a readmission — it might be a month-to-date
        # report — so nothing is inferred, but the consequence is stated because
        # both ward boards also require patients.status = 'admitted'.
        pr.admission = {"action": "none",
                        "reason": f"Orb holds this patient as '{status}' and the file has no status column"}
        pr.issues.append(Issue(
            pr.line, "not_admitted_in_orb", "warning",
            f"Orb currently holds MRN '{pr.mrn}' as '{status}' and this file carries no status "
            f"column, so no admission was opened and the patient will not appear on the ward "
            f"boards. If the export means they are back on the ward, map a status column and "
            f"re-run.", "status", status))
        return

    if open_adm is None:
        ward = values.get("ward") or _row_value(patient_row, "ward")
        bed = values.get("bed") or _row_value(patient_row, "bed")
        pr.admission = {
            "action": "create",
            "ward": ward,
            "bed": bed,
            "department": department,
            "department_source": dept_source,
            "admitted_at": _admission_timestamp(
                values.get("admission_date") or _row_value(patient_row, "admission_date")),
            "attending_doctor_id": (values.get("attending_doctor_id")
                                    or _row_value(patient_row, "attending_doctor_id")),
            "initial_diagnosis": values.get("diagnosis") or _row_value(patient_row, "diagnosis"),
        }
        if department is None:
            pr.issues.append(Issue(
                pr.line, "admission_without_department", "warning",
                "No department and no ward for this row, so the admission is filed with a NULL "
                "department. The patient will NOT appear on any department-scoped board (Vigil, "
                "the nurse shift board) for staff who have a department on record. Map a department "
                "column or pass a default department."))
        return

    # An open encounter already exists. A ward/bed move is a move WITHIN it.
    changes: dict[str, dict] = {}
    for column in ("ward", "bed"):
        new_value = values.get(column)
        if new_value and not _same(open_adm[column], new_value):
            changes[column] = {"from": open_adm[column], "to": new_value}
    if department and dept_source in ("csv", "default"):
        if not _same(open_adm["department"], department):
            changes["department"] = {"from": open_adm["department"], "to": department}
    elif department and dept_source == "ward" and not (open_adm["department"] or "").strip():
        # Only fill a hole. Never overwrite a real service ('General Medicine')
        # with a ward name because the export happened not to carry a department.
        changes["department"] = {"from": open_adm["department"], "to": department}

    pr.admission = {
        "action": "update" if changes else "unchanged",
        "admission_id": open_adm["id"],
        "changes": changes,
        "department_source": dept_source,
    }
    if "ward" in changes:
        pr.issues.append(Issue(
            pr.line, "ward_transfer", "warning",
            f"Ward changed from '{changes['ward']['from']}' to '{changes['ward']['to']}'"
            + (f" (bed '{changes['bed']['from']}' → '{changes['bed']['to']}')"
               if "bed" in changes else "")
            + f". Treated as a MOVE inside the existing admission {open_adm['id']}, the admission "
              f"is updated in place, not closed and re-opened, so the encounter and everything "
              f"hanging off it stays intact.", "ward", changes["ward"]["to"]))


def _plan_allergies(pr: PlannedRow, patient_row: Any,
                    charted: dict[str, dict[str, str]]) -> None:
    """Split the row's allergies into ones to chart and ones Orb already holds.

    Add-only: an allergy a clinician deactivated in Orb stays deactivated (they
    may have marked it entered-in-error), but the disagreement is reported so
    someone looks at the chart rather than trusting either side silently.
    """
    wanted = pr.values.get("allergies") or []
    if not wanted:
        return
    on_chart = charted.get(patient_row["id"], {}) if patient_row is not None else {}
    add, already, inactive = [], [], []
    for substance in wanted:
        status = on_chart.get(substance.strip().lower())
        if status is None:
            add.append(substance)
        elif status == "active":
            already.append(substance)
        else:
            inactive.append(substance)
            pr.issues.append(Issue(
                pr.line, "allergy_inactive_in_orb", "warning",
                f"The file lists an allergy to '{substance}', but it is marked inactive on "
                f"this patient's chart in Orb. Left as it is: review the chart.",
                "allergies", substance))
    pr.allergies = {"add": add, "already_charted": already, "inactive_in_orb": inactive}


def _load_allergies(patient_ids: list[str]) -> dict[str, dict[str, str]]:
    """{patient_id: {substance: status}} for every allergy row, active or not."""
    out: dict[str, dict[str, str]] = {}
    if not patient_ids:
        return out
    with db_session() as db:
        for i in range(0, len(patient_ids), 500):
            chunk = patient_ids[i:i + 500]
            placeholders = ",".join("?" * len(chunk))
            for r in db.execute(
                f"SELECT patient_id, substance, status FROM patient_allergies "
                f"WHERE patient_id IN ({placeholders})", chunk,
            ).fetchall():
                out.setdefault(r["patient_id"], {})[(r["substance"] or "").lower()] = r["status"]
    return out


def _absent_from_roster(planned: list[PlannedRow], mrns_in_file: set[str]) -> dict:
    """Patients on the ward per Orb but missing from this file. REPORTED ONLY.

    Deliberately not acted on. A partial export — one ward, a filtered report, a
    file that stopped early — would otherwise discharge real inpatients off the
    board while reporting a successful import, which is the failure this whole
    module exists to avoid. Scoped to the wards the file actually covers so a
    single-ward roster does not accuse the rest of the hospital of absence.
    """
    wards = sorted({p.values.get("ward") for p in planned if p.values.get("ward")})[:400]
    note = ("These patients hold an OPEN admission in a ward this file covers but have no row in "
            "the file. Orb has NOT discharged them: an absent row is not evidence of a discharge, "
            "and a partial export would otherwise empty the ward board. Discharge them through the "
            "discharge workflow, or map a status column and re-run.")
    if not wards:
        return {"wards": [], "patients": [], "note": note}

    with db_session() as db:
        marks = ",".join("?" * len(wards))
        rows = db.execute(
            f"""SELECT p.id, p.mrn, p.name, p.ward, p.bed
                FROM patients p
                JOIN admissions a ON a.patient_id = p.id AND a.status = 'active'
                WHERE p.status = 'admitted' AND p.ward IN ({marks})
                ORDER BY p.ward, p.bed""",
            tuple(wards),
        ).fetchall()
    absent = [dict(r) for r in rows if r["mrn"] not in mrns_in_file]
    return {"wards": wards, "patients": absent[:200], "count": len(absent), "note": note}


def _same(current: Any, new: Any) -> bool:
    """Field-wise equality that tolerates SQLite's int/str/None looseness."""
    if current is None:
        return False
    if isinstance(new, int) and not isinstance(new, bool):
        try:
            return int(current) == new
        except (TypeError, ValueError):
            return False
    return str(current).strip() == str(new).strip()


def _normalise_row(raw: dict, mapping: dict[str, str], line_no: int,
                   date_order: str) -> PlannedRow:
    """Normalise one CSV row into Orb values + issues."""
    issues: list[Issue] = []
    values: dict[str, Any] = {}

    def cell(fname: str) -> str:
        col = mapping.get(fname)
        return _clean(raw.get(col)) if col else ""

    mrn = _normalise_mrn(cell("mrn"))
    name = cell("name")

    if not mrn:
        return PlannedRow(line_no, None, name or None, "skip", "missing_mrn", issues=[
            Issue(line_no, "missing_mrn", "error",
                  "Row has no MRN: it cannot be matched or safely re-imported, so it was not imported.",
                  "mrn", str(raw.get(mapping.get("mrn", "")) or ""))])
    if not name:
        return PlannedRow(line_no, mrn, None, "skip", "missing_name", issues=[
            Issue(line_no, "missing_name", "error",
                  f"Row for MRN '{mrn}' has no patient name, not imported "
                  f"(a bedside identity check needs a name).", "name", "")])

    # THE registration name rule (services/patient_name_rule.py), applied at
    # import exactly as at the admit desk: a name with fewer than two letters
    # cannot identify anyone at the bedside, so the row is reported and not
    # imported. Initials-only names ("S. R.") still import with a warning.
    from services.patient_name_rule import MIN_NAME_LETTERS, name_is_too_short
    if name_is_too_short(name):
        return PlannedRow(line_no, mrn, name, "skip", "name_too_short", issues=[
            Issue(line_no, "name_too_short", "error",
                  f"Row for MRN '{mrn}' has the name '{name}', which has fewer than "
                  f"{MIN_NAME_LETTERS} letters and cannot identify a patient at the "
                  f"bedside; not imported. Correct it in the export and re-run.",
                  "name", name)])

    values["name"] = name
    issues.extend(_check_name(name, line_no))

    for fname in ("ward", "department", "bed", "phone", "address", "diagnosis",
                  "emergency_contact_name", "emergency_contact_phone",
                  "attending_doctor_id"):
        v = cell(fname)
        if v:
            values[fname] = v

    dob_raw = cell("date_of_birth")
    if dob_raw:
        dob, dob_issues = _parse_date(dob_raw, "date_of_birth", line_no, date_order)
        issues.extend(dob_issues)
        if dob:
            values["date_of_birth"] = dob

    age_raw = cell("age")
    if age_raw:
        age, age_issues = _parse_int(age_raw, "age", line_no, MIN_AGE, MAX_AGE)
        issues.extend(age_issues)
        if age is not None:
            values["age"] = age

    # DOB is the stronger source; when both are present and disagree by more
    # than a year, report it — do NOT silently prefer one.
    if values.get("date_of_birth") and values.get("age") is not None:
        derived = _age_from_dob(values["date_of_birth"])
        if derived is not None and abs(derived - values["age"]) > 1:
            issues.append(Issue(line_no, "age_dob_mismatch", "warning",
                                f"age {values['age']} disagrees with date_of_birth "
                                f"{values['date_of_birth']} (implies {derived}). Both imported as given; "
                                f"reconcile in the HIS.", "age", age_raw))

    adm_raw = cell("admission_date")
    if adm_raw:
        adm, adm_issues = _parse_date(adm_raw, "admission_date", line_no, date_order)
        issues.extend(adm_issues)
        if adm:
            values["admission_date"] = adm

    sex_raw = cell("sex")
    if sex_raw:
        sex, sex_issues = _parse_sex(sex_raw, line_no)
        issues.extend(sex_issues)
        if sex:
            values["sex"] = sex

    status_raw = cell("status")
    if status_raw:
        status, status_issues = _parse_status(status_raw, line_no)
        issues.extend(status_issues)
        values["status"] = status or "admitted"

    allergies = allergy_entries(cell("allergies"))
    if allergies:
        values["allergies"] = allergies

    return PlannedRow(line_no, mrn, name, "pending", values=values, issues=issues)


def _age_from_dob(dob_iso: str) -> int | None:
    try:
        dob = date.fromisoformat(dob_iso)
    except ValueError:
        return None
    today = date.today()
    return today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))


def _load_existing(mrns: list[str]) -> dict[str, Any]:
    """Fetch existing patients by MRN, chunked to stay under SQLite's variable cap."""
    out: dict[str, Any] = {}
    if not mrns:
        return out
    with db_session() as db:
        for i in range(0, len(mrns), 500):
            chunk = mrns[i:i + 500]
            placeholders = ",".join("?" * len(chunk))
            for r in db.execute(
                f"SELECT * FROM patients WHERE mrn IN ({placeholders})", chunk
            ).fetchall():
                out[r["mrn"]] = r
    return out


def _load_open_admissions(patient_ids: list[str]) -> dict[str, Any]:
    """The OPEN encounter per patient, if any — the row both ward boards join to.

    Only one is expected per patient; if a previous bug left two, the newest is
    the one the boards effectively show, so that is the one we plan against.
    """
    out: dict[str, Any] = {}
    if not patient_ids:
        return out
    with db_session() as db:
        for i in range(0, len(patient_ids), 500):
            chunk = patient_ids[i:i + 500]
            placeholders = ",".join("?" * len(chunk))
            for r in db.execute(
                f"""SELECT * FROM admissions
                    WHERE patient_id IN ({placeholders}) AND status = 'active'
                    ORDER BY created_at ASC""",
                chunk,
            ).fetchall():
                out[r["patient_id"]] = r
    return out


def _load_users(user_ids: Iterable[str]) -> dict[str, str]:
    ids = [u for u in user_ids if u]
    if not ids:
        return {}
    out: dict[str, str] = {}
    with db_session() as db:
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            placeholders = ",".join("?" * len(chunk))
            for r in db.execute(
                f"SELECT id, role FROM users WHERE id IN ({placeholders})", chunk
            ).fetchall():
                out[r["id"]] = r["role"]
    return out


# ══════════════════════════════════════════════════════════════════════════
#  Apply
# ══════════════════════════════════════════════════════════════════════════

def apply_import(content: bytes | str, mapping: dict[str, str], *,
                 actor_user_id: str, source_filename: str | None = None,
                 date_order: str = "auto", update_existing: bool = True,
                 note: str | None = None, header_line: int | None = None,
                 default_department: str | None = None) -> dict:
    """Execute the plan for ``content`` in ONE transaction.

    Re-plans from the same bytes rather than trusting a plan posted back by the
    client — the plan the admin approved and the plan we execute are derived
    from the same input by the same code, so a tampered or stale plan cannot
    write anything the dry run did not show.

    Patients AND their admissions are written here, in the same transaction: a
    patient row without an open admission is invisible to both ward boards, so
    committing one without the other would be a half-import that reports success.

    Idempotent: a second run over an unchanged file creates and updates nothing,
    and never opens a second admission for a patient who already has one.
    """
    result = plan(content, mapping, date_order=date_order,
                  update_existing=update_existing, header_line=header_line,
                  default_department=default_department)

    run_id = f"RI-{uuid.uuid4().hex[:10].upper()}"
    started = datetime.now(timezone.utc).isoformat()
    created_ids: list[str] = []
    updated_ids: list[str] = []
    admissions_created: list[str] = []
    admissions_updated: list[str] = []
    admissions_closed: list[str] = []
    allergies_charted = 0

    with db_session() as db:
        for row in result["rows"]:
            if row["action"] == "create":
                patient_id = f"PAT-{uuid.uuid4().hex[:8].upper()}"
                values = dict(row["values"])
                values.setdefault("status", "admitted")
                cols = ["id", "mrn"] + [c for c in _WRITABLE if c in values]
                params = [patient_id, row["mrn"]] + [values[c] for c in cols[2:]]
                db.execute(
                    f"INSERT INTO patients ({', '.join(cols)}) "
                    f"VALUES ({', '.join('?' * len(cols))})",
                    params,
                )
                row["patient_id"] = patient_id
                created_ids.append(patient_id)
            elif row["action"] == "update":
                changed = [c for c in _WRITABLE if c in row["changes"]]
                if changed:
                    db.execute(
                        f"UPDATE patients SET {', '.join(f'{c} = ?' for c in changed)} WHERE id = ?",
                        [row["changes"][c]["to"] for c in changed] + [row["patient_id"]],
                    )
                    updated_ids.append(row["patient_id"])

            # ── the encounter ────────────────────────────────────────────────
            # Driven by row["admission"], not by row["action"]: a patient whose
            # every column already matches is 'unchanged' and may still be
            # missing the admissions row that puts them on the ward board (every
            # patient imported before this module opened admissions is in exactly
            # that state), so a re-run repairs them.
            if row["action"] == "skip":
                continue
            patient_id = row.get("patient_id")
            to_chart = (row.get("allergies") or {}).get("add") or []
            if patient_id and to_chart:
                # Same transaction as the patient row: an import that rolls back
                # must not leave allergies on a chart that does not exist.
                allergies_charted += len(record_allergies_on(
                    db, patient_id, to_chart, source="roster_import",
                    recorded_by=actor_user_id,
                ))
            adm = row.get("admission") or {}
            if not patient_id or not adm:
                continue

            if adm.get("action") == "create":
                # Re-checked INSIDE the transaction, not just in the plan: two
                # admins committing the same roster at once would otherwise open
                # two encounters for one patient (there is no partial unique
                # index on admissions to catch it).
                already = db.execute(
                    "SELECT id FROM admissions WHERE patient_id = ? AND status = 'active' LIMIT 1",
                    (patient_id,),
                ).fetchone()
                if already:
                    adm["action"] = "unchanged"
                    adm["admission_id"] = already["id"]
                    adm["reason"] = "an open admission already existed at write time"
                    continue
                admission_id = f"ADM-{uuid.uuid4().hex[:8].upper()}"
                db.execute(
                    """INSERT INTO admissions
                           (id, patient_id, admitted_by, admission_source, department,
                            ward, bed, attending_doctor_id, initial_diagnosis,
                            admitting_notes, status, created_at)
                       VALUES (?, ?, ?, 'roster_import', ?, ?, ?, ?, ?, ?, 'active', ?)""",
                    (admission_id, patient_id, actor_user_id, adm.get("department"),
                     adm.get("ward"), adm.get("bed"), adm.get("attending_doctor_id"),
                     adm.get("initial_diagnosis"),
                     f"Opened by roster import {run_id}"
                     + (f" from '{source_filename}'." if source_filename else "."),
                     adm.get("admitted_at")),
                )
                adm["admission_id"] = admission_id
                admissions_created.append(admission_id)

            elif adm.get("action") == "update":
                changed = [c for c in ("ward", "bed", "department") if c in adm.get("changes", {})]
                if changed:
                    db.execute(
                        f"UPDATE admissions SET {', '.join(f'{c} = ?' for c in changed)} "
                        f"WHERE id = ? AND status = 'active'",
                        [adm["changes"][c]["to"] for c in changed] + [adm["admission_id"]],
                    )
                    admissions_updated.append(adm["admission_id"])

            elif adm.get("action") == "close":
                db.execute(
                    "UPDATE admissions SET status = ? WHERE id = ? AND status = 'active'",
                    (adm["to_status"], adm["admission_id"]),
                )
                admissions_closed.append(adm["admission_id"])

        summary = {
            "created": len(created_ids),
            "updated": len(updated_ids),
            "unchanged": result["counts"]["unchanged"],
            "skipped": result["counts"]["skip"],
            "errors": result["counts"]["errors"],
            "warnings": result["counts"]["warnings"],
        }
        admissions = {
            "created": len(admissions_created),
            "updated": len(admissions_updated),
            "closed": len(admissions_closed),
        }
        db.execute(
            """INSERT INTO roster_import_runs
                   (id, actor_user_id, source_filename, mapping_json, date_order,
                    update_existing, rows_total, rows_created, rows_updated,
                    rows_unchanged, rows_skipped, issue_count, summary_json, note, started_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (run_id, actor_user_id, source_filename, json.dumps(result["mapping"]),
             date_order, 1 if update_existing else 0, result["counts"]["total_rows"],
             summary["created"], summary["updated"], summary["unchanged"],
             summary["skipped"], summary["errors"] + summary["warnings"],
             json.dumps({**summary, "admissions": admissions,
                         "allergies": {"charted": allergies_charted}}), note, started),
        )

    result["run_id"] = run_id
    result["applied"] = True
    result["summary"] = summary
    result["admissions"] = admissions
    result["allergies"] = {"charted": allergies_charted}
    result["created_patient_ids"] = created_ids
    result["updated_patient_ids"] = updated_ids
    result["created_admission_ids"] = admissions_created
    return result


def list_runs(limit: int = 50) -> list[dict]:
    """Import history, newest first."""
    limit = max(1, min(int(limit), 500))
    with db_session() as db:
        rows = db.execute(
            "SELECT * FROM roster_import_runs ORDER BY started_at DESC, rowid DESC LIMIT ?",
            (limit,),
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        for jf in ("mapping_json", "summary_json"):
            try:
                d[jf] = json.loads(d[jf]) if d.get(jf) else None
            except (json.JSONDecodeError, TypeError):
                d[jf] = None
        out.append(d)
    return out
