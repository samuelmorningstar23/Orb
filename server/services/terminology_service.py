"""Orb Hospital OS — Terminology Service

Validation and seeding for the local `terminology` subset used by CPOE-lite to
VALIDATE order codes on write (LOINC labs, SNOMED CT procedures/findings, ICD-10
diagnoses). Unknown codes are flagged, not silently accepted.

⚠️  CURATED SEED ONLY — this module ships a small, hand-picked starter set of
real codes with real displays so the Orders workflow can validate against
something out of the box. It is NOT a substitute for the full reference
terminologies. The real-data step is to bulk-load the licensed/complete
releases into the SAME `terminology` table via `scripts/load_terminology.py`:

  * LOINC       — freely redistributable (Regenstrief licence, no cost).
  * ICD-10-CM   — freely redistributable (CMS/WHO).
  * SNOMED CT   — requires a national/affiliate licence (UMLS Metathesaurus is
                  the usual delivery vehicle); do NOT redistribute the content,
                  only load it locally.

`validate_code` / `get_display` read whatever is in the table, so once the full
releases are loaded they transparently validate against millions of codes.

THE RELEASE LOADER (opd-20, W9-G, 10 October 2026). scripts/load_terminology.py
now exists and loads an ICD-10 release (code_system ICD10, the only system in
LOADABLE_CODE_SYSTEMS) through load_release below:
  * NO RELEASE IS LOADED UNTIL COUNSEL CONFIRMS ITS LICENCE (ruling OPD-N11).
    A load writes nothing without a licence reference (counsel's confirmation)
    and is otherwise a dry run that reports what it would do.
  * Idempotent: the same file twice changes nothing the second time and
    leaves the same row count. Codes are never deleted; a code the release
    marks inactive (or, with retire_missing, leaves out) is set inactive and
    no longer validates.
  * One version row per code system records the release loaded: code_system
    RELEASE_CODE_SYSTEM ('RELEASE'), code the code system, display a JSON
    description, is_active 0 ALWAYS, so no validator, picker or order check
    ever reads it as a code (every reader asks for is_active = 1 and a named
    code system).
  * Each load is audited fail-closed (clinical_config) in its own transaction.
  * Stored 'unvalidated' diagnoses are re-checked only on request
    (revalidate_draft_diagnoses), on UNSIGNED outpatient notes only. A signed
    note is never rewritten (a trigger refuses it too); it is only counted.
Nothing here downloads anything: the release is a file the hospital brings.
"""

import json
import re
import uuid
from datetime import UTC, datetime

from database import db_session

# ══════════════════════════════════════════════════════════════════════════
#  VALIDATION / LOOKUP
# ══════════════════════════════════════════════════════════════════════════


def validate_code(code_system: str, code: str) -> bool:
    """Return True iff (code_system, code) exists in `terminology` and is active."""
    if not code_system or not code:
        return False
    with db_session() as conn:
        row = conn.execute(
            "SELECT 1 FROM terminology WHERE code_system = ? AND code = ? AND is_active = 1 LIMIT 1",
            (code_system.upper(), code),
        ).fetchone()
        return row is not None


def get_display(code_system: str, code: str) -> str | None:
    """Return the human-readable display for an active code, or None if unknown."""
    if not code_system or not code:
        return None
    with db_session() as conn:
        row = conn.execute(
            "SELECT display FROM terminology WHERE code_system = ? AND code = ? AND is_active = 1 LIMIT 1",
            (code_system.upper(), code),
        ).fetchone()
        return row["display"] if row else None


# ══════════════════════════════════════════════════════════════════════════
#  SEARCH: THE OUTPATIENT PICKERS (opd-11, W6-A, 9 October 2026)
# ══════════════════════════════════════════════════════════════════════════
# GET /api/opd/terminology/search reads these. Two lists, both read only:
#
#   * DIAGNOSIS: ICD-10 codes, and SNOMED CT disorders and findings (a SNOMED
#     procedure, body structure or substance is not a diagnosis). A diagnosis
#     is free text first (ruling OPD-N11): the picker only offers a code to go
#     with the text, and an unknown code is still saved, as unvalidated
#     (opd_service.normalise_diagnoses).
#   * INVESTIGATION: what a clinic may order for an outpatient, laboratory
#     tests and imaging only. No medicine (an outpatient prescription is
#     opd-12, behind the allergy review and the interlocks), no procedure, no
#     nursing order, and no bedside test (ruling LIS-R18: ketones, VBG and ABG
#     are done at a ward bedside, which a clinic does not have). While the
#     laboratory module is on, a laboratory test comes from the laboratory's
#     own catalogue only, the rule the Orders screen keeps, so the laboratory
#     can take in every test a clinic orders.
#
# describe_investigation is the server's own check on a code a clinic sends:
# the route trusts the order type and the words it returns, never the
# client's. Nothing here scores, bands or advises.

SEARCH_MIN_CHARS = 2
SEARCH_MAX_RESULTS = 20
SEARCH_KINDS = ("diagnosis", "investigation")
DIAGNOSIS_CODE_SYSTEMS = ("ICD10", "SNOMED")
#: SNOMED CT semantic tags that name a diagnosis.
_SNOMED_DIAGNOSIS_TAGS = ("(disorder)", "(finding)")
#: The order types an outpatient clinic may raise as an investigation.
INVESTIGATION_ORDER_TYPES = ("lab", "imaging")
#: Orb's local codes say what they are in their prefix.
_LOCAL_PREFIX_TYPES = (
    ("ORB-LAB-", "lab"),
    ("ORB-IMG-", "imaging"),
    ("ORB-PROC-", "procedure"),
    ("ORB-MED-", "medication"),
)
#: Orb's local laboratory codes for tests done at the bedside (ruling LIS-R18).
BEDSIDE_LOCAL_CODES = frozenset({"ORB-LAB-ABG", "ORB-LAB-VBG", "ORB-LAB-BLOOD-KETONES"})


def _like_pattern(text: str) -> str:
    return "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _has_table(conn, name: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
        ).fetchone()
        is not None
    )


def _local_type(code: str) -> str | None:
    upper = (code or "").upper()
    for prefix, kind in _LOCAL_PREFIX_TYPES:
        if upper.startswith(prefix):
            return kind
    return None


def _catalogue_row(conn, code_system: str, code: str):
    if not _has_table(conn, "lab_tests"):
        return None
    return conn.execute(
        "SELECT name, performed_at, active, orderable FROM lab_tests "
        "WHERE code_system = ? AND code = ?",
        (code_system, code),
    ).fetchone()


def describe_investigation(code_system: str, code: str, *, lab_catalogue: bool = False) -> dict:
    """Whether a clinic may order this code as an outpatient investigation.

    Returns {order_type, display, refusal}: `refusal` is None when it may,
    else one of 'unknown_code' (not active in terminology, nor in the
    laboratory catalogue while it is in use), 'not_an_investigation' (a
    medicine, a procedure, a diagnosis or anything else that is not a
    laboratory test or imaging), 'bedside_test' (ruling LIS-R18) or
    'not_in_lab_catalogue' (the laboratory module is on and the laboratory
    does not offer this test). `display` is the stored wording, which the
    order carries as its description; the client's own text is never used.
    """
    system = (code_system or "").strip().upper()
    value = (code or "").strip()
    out = {"order_type": None, "display": None, "refusal": "unknown_code"}
    if not system or not value:
        return out
    with db_session() as conn:
        term = conn.execute(
            "SELECT display FROM terminology WHERE code_system = ? AND code = ? AND is_active = 1",
            (system, value),
        ).fetchone()
        catalogue = _catalogue_row(conn, system, value) if system in ("LOINC", "LOCAL") else None
    in_catalogue = bool(catalogue and catalogue["active"] and catalogue["orderable"])
    if system == "LOINC":
        kind: str | None
        kind = "lab"
    elif system == "LOCAL":
        kind = _local_type(value) or ("lab" if catalogue is not None else None)
    else:
        kind = None
    display = (catalogue["name"] if in_catalogue and catalogue is not None else None) or (
        term["display"] if term else None
    )
    if term is None and not in_catalogue:
        return out
    out.update(order_type=kind, display=display, refusal=None)
    if kind not in INVESTIGATION_ORDER_TYPES:
        out["refusal"] = "not_an_investigation"
    elif kind == "lab" and (
        value.upper() in BEDSIDE_LOCAL_CODES
        or (catalogue is not None and catalogue["performed_at"] == "ward_poct")
    ):
        out["refusal"] = "bedside_test"
    elif kind == "lab" and lab_catalogue and not in_catalogue:
        out["refusal"] = "not_in_lab_catalogue"
    return out


def search_terms(
    query: str, *, kind: str, limit: int = SEARCH_MAX_RESULTS, lab_catalogue: bool = False
) -> dict:
    """Codes whose wording or code contains `query` (case ignored), for the
    outpatient pickers. `kind` is 'diagnosis' or 'investigation' (ValueError
    otherwise). Fewer than SEARCH_MIN_CHARS characters searches nothing.

    Returns {kind, results, truncated, min_chars}; each result is
    {code_system, code, display} and, for an investigation, its order_type.
    No patient data is read or returned."""
    if kind not in SEARCH_KINDS:
        raise ValueError(f"kind must be one of {', '.join(SEARCH_KINDS)}")
    text = (query or "").strip()[:120]
    limit = max(1, min(int(limit or SEARCH_MAX_RESULTS), SEARCH_MAX_RESULTS))
    answer = {"kind": kind, "results": [], "truncated": False, "min_chars": SEARCH_MIN_CHARS}
    if len(text) < SEARCH_MIN_CHARS:
        return answer
    pattern = _like_pattern(text.lower())
    rows: list[dict] = []
    with db_session() as conn:
        if kind == "diagnosis":
            tags = " OR ".join("display LIKE ?" for _ in _SNOMED_DIAGNOSIS_TAGS)
            found = conn.execute(
                f"""SELECT code_system, code, display FROM terminology
                     WHERE is_active = 1
                       AND (code_system = 'ICD10' OR (code_system = 'SNOMED' AND ({tags})))
                       AND (lower(display) LIKE ? ESCAPE '\\' OR lower(code) LIKE ? ESCAPE '\\')
                     ORDER BY CASE WHEN lower(code) = ? THEN 0
                                   WHEN lower(display) LIKE ? ESCAPE '\\' THEN 1 ELSE 2 END,
                              code_system, display, code
                     LIMIT ?""",
                (
                    *(f"%{t}" for t in _SNOMED_DIAGNOSIS_TAGS),
                    pattern,
                    pattern,
                    text.lower(),
                    _like_pattern(text.lower())[1:],
                    limit + 1,
                ),
            ).fetchall()
            rows = [
                {"code_system": r["code_system"], "code": r["code"], "display": r["display"]}
                for r in found
            ]
        else:
            rows = _investigation_rows(
                conn, text.lower(), pattern, limit + 1, lab_catalogue=lab_catalogue
            )
    answer["truncated"] = len(rows) > limit
    answer["results"] = rows[:limit]
    return answer


def _investigation_rows(
    conn, text: str, pattern: str, limit: int, *, lab_catalogue: bool
) -> list[dict]:
    """Laboratory tests and imaging a clinic may order, best match first.
    Every row also passes describe_investigation's rules."""
    bedside = sorted(BEDSIDE_LOCAL_CODES)
    marks = ",".join("?" for _ in bedside)
    poct = ""
    if _has_table(conn, "lab_tests"):
        poct = (
            " AND NOT EXISTS (SELECT 1 FROM lab_tests p WHERE p.code_system = t.code_system "
            "AND p.code = t.code AND p.performed_at = 'ward_poct')"
        )
    rows: list[dict] = []
    if lab_catalogue and _has_table(conn, "lab_tests"):
        for r in conn.execute(
            """SELECT code_system, code, name FROM lab_tests
                WHERE active = 1 AND orderable = 1 AND performed_at = 'lab'
                  AND (lower(name) LIKE ? ESCAPE '\\' OR lower(short_name) LIKE ? ESCAPE '\\'
                       OR lower(code) LIKE ? ESCAPE '\\')
                ORDER BY CASE WHEN lower(code) = ? THEN 0 ELSE 1 END, name, code
                LIMIT ?""",
            (pattern, pattern, pattern, text, limit),
        ).fetchall():
            if r["code"].upper() in BEDSIDE_LOCAL_CODES:
                continue
            rows.append(
                {
                    "code_system": r["code_system"],
                    "code": r["code"],
                    "display": r["name"],
                    "order_type": "lab",
                }
            )
        lab_sql = None
    else:
        lab_sql = (
            f"""SELECT t.code_system, t.code, t.display, 'lab' AS order_type
                        FROM terminology t
                       WHERE t.is_active = 1
                         AND (t.code_system = 'LOINC'
                              OR (t.code_system = 'LOCAL' AND upper(t.code) LIKE 'ORB-LAB-%'))
                         AND upper(t.code) NOT IN ({marks}){poct}
                         AND (lower(t.display) LIKE ? ESCAPE '\\' OR lower(t.code) LIKE ? ESCAPE '\\')""",
            (*bedside, pattern, pattern),
        )
    imaging_sql = (
        """SELECT t.code_system, t.code, t.display, 'imaging' AS order_type
                       FROM terminology t
                      WHERE t.is_active = 1 AND t.code_system = 'LOCAL'
                        AND upper(t.code) LIKE 'ORB-IMG-%'
                        AND (lower(t.display) LIKE ? ESCAPE '\\' OR lower(t.code) LIKE ? ESCAPE '\\')""",
        (pattern, pattern),
    )
    for sql, params in ([lab_sql] if lab_sql else []) + [imaging_sql]:
        for r in conn.execute(
            f"SELECT * FROM ({sql}) ORDER BY CASE WHEN lower(code) = ? THEN 0 ELSE 1 END, "
            "display, code LIMIT ?",
            (*params, text, limit),
        ).fetchall():
            rows.append(
                {
                    "code_system": r["code_system"],
                    "code": r["code"],
                    "display": r["display"],
                    "order_type": r["order_type"],
                }
            )
    rows.sort(
        key=lambda r: (
            0 if r["code"].lower() == text else 1,
            0 if r["display"].lower().startswith(text) else 1,
            r["display"].lower(),
            r["code"],
        )
    )
    return rows[:limit]


# ══════════════════════════════════════════════════════════════════════════
#  CURATED STARTER SET
# ══════════════════════════════════════════════════════════════════════════
# (code_system, code, display) — all real codes / real displays.

_LOINC_SEED = [
    ("LOINC", "58410-2", "CBC panel - Blood by Automated count"),
    ("LOINC", "57021-8", "CBC W Auto Differential panel - Blood"),
    ("LOINC", "718-7", "Hemoglobin [Mass/volume] in Blood"),
    ("LOINC", "4544-3", "Hematocrit [Volume Fraction] of Blood by Automated count"),
    ("LOINC", "789-8", "Erythrocytes [#/volume] in Blood by Automated count"),
    ("LOINC", "6690-2", "Leukocytes [#/volume] in Blood by Automated count"),
    ("LOINC", "777-3", "Platelets [#/volume] in Blood by Automated count"),
    ("LOINC", "51990-0", "Basic metabolic panel - Blood"),
    ("LOINC", "24323-8", "Comprehensive metabolic 2000 panel - Serum or Plasma"),
    ("LOINC", "2345-7", "Glucose [Mass/volume] in Serum or Plasma"),
    ("LOINC", "2160-0", "Creatinine [Mass/volume] in Serum or Plasma"),
    ("LOINC", "3094-0", "Urea nitrogen [Mass/volume] in Serum or Plasma"),
    ("LOINC", "2951-2", "Sodium [Moles/volume] in Serum or Plasma"),
    ("LOINC", "2823-3", "Potassium [Moles/volume] in Serum or Plasma"),
    ("LOINC", "2075-0", "Chloride [Moles/volume] in Serum or Plasma"),
    ("LOINC", "2028-9", "Carbon dioxide, total [Moles/volume] in Serum or Plasma"),
    ("LOINC", "17861-6", "Calcium [Mass/volume] in Serum or Plasma"),
    ("LOINC", "2524-7", "Lactate [Moles/volume] in Serum or Plasma"),
    ("LOINC", "6598-7", "Troponin T.cardiac [Mass/volume] in Serum or Plasma"),
    ("LOINC", "10839-9", "Troponin I.cardiac [Mass/volume] in Serum or Plasma"),
    (
        "LOINC",
        "33762-6",
        "Natriuretic peptide B prohormone N-Terminal [Mass/volume] in Serum or Plasma",
    ),
    ("LOINC", "1988-5", "C reactive protein [Mass/volume] in Serum or Plasma"),
    ("LOINC", "30341-2", "Erythrocyte sedimentation rate"),
    (
        "LOINC",
        "1920-8",
        "Aspartate aminotransferase [Enzymatic activity/volume] in Serum or Plasma",
    ),
    ("LOINC", "1742-6", "Alanine aminotransferase [Enzymatic activity/volume] in Serum or Plasma"),
    ("LOINC", "1975-2", "Bilirubin.total [Mass/volume] in Serum or Plasma"),
    ("LOINC", "1751-7", "Albumin [Mass/volume] in Serum or Plasma"),
    ("LOINC", "5902-2", "Prothrombin time (PT)"),
    ("LOINC", "3173-2", "aPTT in Blood by Coagulation assay"),
    ("LOINC", "6301-6", "INR in Platelet poor plasma by Coagulation assay"),
    ("LOINC", "4548-4", "Hemoglobin A1c/Hemoglobin.total in Blood"),
    ("LOINC", "3016-3", "Thyrotropin [Units/volume] in Serum or Plasma"),
    ("LOINC", "2093-3", "Cholesterol [Mass/volume] in Serum or Plasma"),
]

_SNOMED_SEED = [
    ("SNOMED", "386661006", "Fever (finding)"),
    ("SNOMED", "29857009", "Chest pain (finding)"),
    ("SNOMED", "267036007", "Dyspnea (finding)"),
    ("SNOMED", "25064002", "Headache (finding)"),
    ("SNOMED", "422587007", "Nausea (finding)"),
    ("SNOMED", "62315008", "Diarrhea (finding)"),
    ("SNOMED", "21522001", "Abdominal pain (finding)"),
    ("SNOMED", "49727002", "Cough (finding)"),
    ("SNOMED", "404640003", "Dizziness (finding)"),
    ("SNOMED", "271737000", "Anemia (disorder)"),
    ("SNOMED", "233604007", "Pneumonia (disorder)"),
    ("SNOMED", "195967001", "Asthma (disorder)"),
    ("SNOMED", "73211009", "Diabetes mellitus (disorder)"),
    ("SNOMED", "38341003", "Hypertensive disorder, systemic arterial (disorder)"),
    ("SNOMED", "22298006", "Myocardial infarction (disorder)"),
    ("SNOMED", "18286008", "Transfusion of blood product (procedure)"),
    ("SNOMED", "232717009", "Coronary artery bypass grafting (procedure)"),
    ("SNOMED", "76601001", "Intramuscular injection (procedure)"),
    ("SNOMED", "71388002", "Procedure (procedure)"),
    ("SNOMED", "103693007", "Diagnostic procedure (procedure)"),
    ("SNOMED", "386053000", "Evaluation procedure (procedure)"),
]

_ICD10_SEED = [
    ("ICD10", "E11.9", "Type 2 diabetes mellitus without complications"),
    ("ICD10", "I10", "Essential (primary) hypertension"),
    ("ICD10", "J18.9", "Pneumonia, unspecified organism"),
    ("ICD10", "J44.9", "Chronic obstructive pulmonary disease, unspecified"),
    ("ICD10", "J45.909", "Unspecified asthma, uncomplicated"),
    ("ICD10", "N39.0", "Urinary tract infection, site not specified"),
    ("ICD10", "A41.9", "Sepsis, unspecified organism"),
    ("ICD10", "I21.9", "Acute myocardial infarction, unspecified"),
    ("ICD10", "I50.9", "Heart failure, unspecified"),
    ("ICD10", "I48.91", "Unspecified atrial fibrillation"),
    ("ICD10", "E78.5", "Hyperlipidemia, unspecified"),
    ("ICD10", "D64.9", "Anemia, unspecified"),
    ("ICD10", "K21.9", "Gastro-esophageal reflux disease without esophagitis"),
    ("ICD10", "N17.9", "Acute kidney failure, unspecified"),
    ("ICD10", "R07.9", "Chest pain, unspecified"),
    ("ICD10", "R51.9", "Headache, unspecified"),
    ("ICD10", "R10.9", "Unspecified abdominal pain"),
    ("ICD10", "R50.9", "Fever, unspecified"),
    ("ICD10", "E86.0", "Dehydration"),
    ("ICD10", "I63.9", "Cerebral infarction, unspecified"),
    ("ICD10", "G40.909", "Epilepsy, unspecified, not intractable, without status epilepticus"),
    ("ICD10", "F32.9", "Major depressive disorder, single episode, unspecified"),
]

STARTER_TERMINOLOGY = _LOINC_SEED + _SNOMED_SEED + _ICD10_SEED


def seed_terminology() -> dict:
    """Idempotently load the curated starter set into `terminology`.

    Uses INSERT OR IGNORE against the UNIQUE(code_system, code) constraint, so
    it is safe to call on every startup and safe to run standalone. Returns a
    per-system count summary of what is now present.
    """
    with db_session() as conn:
        conn.executemany(
            "INSERT OR IGNORE INTO terminology (code_system, code, display, is_active) "
            "VALUES (?, ?, ?, 1)",
            STARTER_TERMINOLOGY,
        )

    with db_session() as conn:
        # The release loader's version rows (opd-20) are not codes.
        rows = conn.execute(
            "SELECT code_system, COUNT(*) AS n FROM terminology WHERE code_system != ? "
            "GROUP BY code_system",
            (RELEASE_CODE_SYSTEM,),
        ).fetchall()
    counts = {r["code_system"]: r["n"] for r in rows}
    return {
        "seeded": len(STARTER_TERMINOLOGY),
        "counts": counts,
        "timestamp": datetime.now(UTC).isoformat(),
    }


def register_codes(conn, rows) -> int:
    """Add (code_system, code, display) rows that `terminology` does not hold
    yet, on the caller's connection and inside the caller's transaction.

    For a module that brings its own orderable codes (the laboratory catalogue,
    services/lab_master_service.py, only while ORB_LAB is on). A code already
    present is left exactly as it is, display and is_active included, so a
    licensed release loaded over the starter set always wins. Never commits.
    Returns how many rows were added.
    """
    added = 0
    for code_system, code, display in rows:
        system = (code_system or "").strip().upper()
        value = (code or "").strip()
        text = (display or "").strip()
        if not system or not value or not text:
            raise ValueError("a terminology row needs a code system, a code and a display")
        cur = conn.execute(
            "INSERT OR IGNORE INTO terminology (code_system, code, display, is_active) "
            "VALUES (?, ?, ?, 1)",
            (system, value, text),
        )
        added += max(cur.rowcount or 0, 0)
    return added


# ══════════════════════════════════════════════════════════════════════════
#  RELEASE LOADER (opd-20, W9-G, 10 October 2026)
# ══════════════════════════════════════════════════════════════════════════
# scripts/load_terminology.py reads the hospital's release file and calls
# load_release; the rules live here so nothing else can load past them. See
# the module docstring for the licence gate (ruling OPD-N11), the version row
# and what is never rewritten.

#: The code_system of the version rows: one per loaded code system, code = the
#: code system, display = a JSON description of the release, is_active 0.
RELEASE_CODE_SYSTEM = "RELEASE"
#: What the loader may load. ICD-10 only (opd-20); LOINC and SNOMED CT each
#: need their own licence answer and their own rules for codes the laboratory
#: catalogue and the order screens already use, so they are refused for now.
LOADABLE_CODE_SYSTEMS = ("ICD10",)
#: The longest display text kept from a release.
MAX_RELEASE_DISPLAY = 1000
#: Samples of skipped rows shown in a report.
_SAMPLE_LIMIT = 10
#: ICD-10 and ICD-10-CM codes, dotted after the third character: a letter, a
#: digit, a digit or letter, then up to four characters (S72.001A).
_ICD10_CODE = re.compile(r"^[A-Z][0-9][0-9A-Z](\.[0-9A-Z]{1,4})?$")
#: Marks some releases print after a code (the WHO dagger and asterisk).
_CODE_MARKS = "*†+"


class TerminologyLoadError(ValueError):
    """A release the loader will not load. `code` is the machine word."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def normalise_release_code(code_system: str, raw) -> str | None:
    """A release's code as `terminology` stores it, or None when the text is
    not a code (a block range such as A00-A09, a chapter heading, a blank).

    ICD-10: upper case, marks after the code removed, and the dot put after
    the third character when the release prints codes without it (E119 is
    E11.9), the spelling the starter set and the pickers use."""
    text = "".join(str(raw or "").split()).upper().rstrip(_CODE_MARKS)
    if not text:
        return None
    if code_system == "ICD10":
        if "." not in text and len(text) > 3:
            text = f"{text[:3]}.{text[3:]}"
        if text.endswith("."):
            text = text[:-1]
        return text if _ICD10_CODE.match(text) else None
    return text


def release_info(code_system: str, conn=None) -> dict | None:
    """The release last loaded for `code_system` (its version row), or None
    when no release was ever loaded (only the starter set is there)."""
    system = (code_system or "").strip().upper()

    def _read(c):
        row = c.execute(
            "SELECT display FROM terminology WHERE code_system = ? AND code = ?",
            (RELEASE_CODE_SYSTEM, system),
        ).fetchone()
        if row is None:
            return None
        try:
            info = json.loads(row["display"])
        except (TypeError, ValueError):
            return {"code_system": system, "unreadable": True}
        return info if isinstance(info, dict) else {"code_system": system, "unreadable": True}

    if conn is not None:
        return _read(conn)
    with db_session() as c:
        return _read(c)


def _clean_entries(code_system: str, entries) -> tuple[dict, dict]:
    """(release codes {code: (display, active)}, skipped facts). Refuses a
    release with a code but no wording, or one code with two wordings."""
    codes: dict[str, tuple[str, bool]] = {}
    rows_read = 0
    skipped: list[str] = []
    no_display: list[str] = []
    conflicts: list[str] = []
    for entry in entries:
        rows_read += 1
        raw_code, raw_display, active = entry
        code = normalise_release_code(code_system, raw_code)
        if code is None:
            skipped.append(str(raw_code or "").strip())
            continue
        display = " ".join(str(raw_display or "").split())[:MAX_RELEASE_DISPLAY]
        if not display:
            no_display.append(code)
            continue
        active = bool(active)
        seen = codes.get(code)
        if seen is not None and seen != (display, active):
            conflicts.append(code)
            continue
        codes[code] = (display, active)
    if no_display:
        raise TerminologyLoadError(
            "release_row_without_display",
            f"{len(no_display)} code(s) in the release have no wording, for example "
            f"{', '.join(no_display[:_SAMPLE_LIMIT])}. Nothing was loaded.",
        )
    if conflicts:
        raise TerminologyLoadError(
            "release_code_twice",
            f"{len(set(conflicts))} code(s) appear twice with different wording or status, "
            f"for example {', '.join(sorted(set(conflicts))[:_SAMPLE_LIMIT])}. Nothing was loaded.",
        )
    if not codes:
        raise TerminologyLoadError(
            "release_empty", "The release holds no codes Orb can read. Nothing was loaded."
        )
    return codes, {
        "rows_read": rows_read,
        "skipped_not_a_code": len(skipped),
        "skipped_samples": [s for s in skipped if s][:_SAMPLE_LIMIT],
    }


def _release_identity(info: dict | None) -> tuple | None:
    if not info:
        return None
    return (info.get("release"), info.get("version"), info.get("source_sha256"))


def load_release(
    entries,
    *,
    code_system: str,
    release: str,
    version: str,
    source_sha256: str,
    source_name: str | None = None,
    licence_ref: str | None = None,
    operator: str | None = None,
    retire_missing: bool = False,
    dry_run: bool = False,
) -> dict:
    """Load one release of `code_system` into `terminology`.

    `entries` are (code, display, active) as the release file states them.
    Every code in the release is inserted or brought in line (wording and
    active flag); a code the table holds and the release leaves out is set
    inactive only with `retire_missing`. Nothing is ever deleted.

    Writes nothing when `dry_run`, or when the table already matches the
    release and its version row already names it (a second run of the same
    file), and answers the same report either way. A write needs
    `licence_ref` (ruling OPD-N11): without one the call is refused before
    anything is read.

    Raises TerminologyLoadError (and AuditWriteError when the audit row
    cannot be written; nothing is then kept)."""
    system = (code_system or "").strip().upper()
    if system not in LOADABLE_CODE_SYSTEMS:
        raise TerminologyLoadError(
            "code_system_not_loadable",
            f"This loader loads {', '.join(LOADABLE_CODE_SYSTEMS)} only, not {system or 'nothing'}.",
        )
    release = " ".join(str(release or "").split())
    version = " ".join(str(version or "").split())
    if not release or not version:
        raise TerminologyLoadError(
            "release_unnamed", "Name the release and its version, as the publisher prints them."
        )
    licence = " ".join(str(licence_ref or "").split()) or None
    if not dry_run and not licence:
        raise TerminologyLoadError(
            "licence_required",
            "No release is loaded until counsel confirms its licence (ruling OPD-N11). Give "
            "counsel's confirmation reference to load it, or run a dry run.",
        )
    sha = str(source_sha256 or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", sha):
        raise TerminologyLoadError(
            "source_unidentified", "The release file's SHA-256 is needed to record what was loaded."
        )

    codes, facts = _clean_entries(system, entries)

    with db_session(immediate=not dry_run) as conn:
        existing = {
            r["code"]: (r["display"], bool(r["is_active"]))
            for r in conn.execute(
                "SELECT code, display, is_active FROM terminology WHERE code_system = ?", (system,)
            ).fetchall()
        }
        previous = release_info(system, conn)

        inserts, updates = [], []
        counts = {
            "inserted": 0,
            "inserted_inactive": 0,
            "display_updated": 0,
            "reactivated": 0,
            "deactivated": 0,
            "retired_missing": 0,
            "unchanged": 0,
        }
        for code, (display, active) in codes.items():
            had = existing.get(code)
            if had is None:
                inserts.append((system, code, display, 1 if active else 0))
                counts["inserted" if active else "inserted_inactive"] += 1
                continue
            old_display, old_active = had
            if (old_display, old_active) == (display, active):
                counts["unchanged"] += 1
                continue
            if old_display != display:
                counts["display_updated"] += 1
            if old_active != active:
                counts["reactivated" if active else "deactivated"] += 1
            updates.append((display, 1 if active else 0, system, code))
        retirements = []
        if retire_missing:
            for code, (_display, old_active) in existing.items():
                if code not in codes and old_active:
                    retirements.append((system, code))
            counts["retired_missing"] = len(retirements)

        active_before = sum(1 for _d, a in existing.values() if a)
        active_after = (
            active_before
            + counts["inserted"]
            + counts["reactivated"]
            - counts["deactivated"]
            - counts["retired_missing"]
        )
        changes = bool(inserts or updates or retirements)
        identity = (release, version, sha)
        already_loaded = (not changes) and _release_identity(previous) == identity
        report = {
            "code_system": system,
            "release": release,
            "version": version,
            "source_sha256": sha,
            "source_name": source_name,
            "licence_ref": licence,
            "retire_missing": bool(retire_missing),
            "dry_run": bool(dry_run),
            "already_loaded": already_loaded,
            "applied": False,
            **facts,
            "codes_in_release": len(codes),
            "active_in_release": sum(1 for _d, a in codes.values() if a),
            **counts,
            "total_before": len(existing),
            "total_after": len(existing) + len(inserts),
            "active_before": active_before,
            "active_after": active_after,
            "previous_release": previous,
        }
        if dry_run or already_loaded:
            return report

        loaded_at = datetime.now(UTC).isoformat()
        if inserts:
            conn.executemany(
                "INSERT INTO terminology (code_system, code, display, is_active) "
                "VALUES (?, ?, ?, ?)",
                inserts,
            )
        if updates:
            conn.executemany(
                "UPDATE terminology SET display = ?, is_active = ? "
                "WHERE code_system = ? AND code = ?",
                updates,
            )
        if retirements:
            conn.executemany(
                "UPDATE terminology SET is_active = 0 WHERE code_system = ? AND code = ?",
                retirements,
            )
        info = {
            "code_system": system,
            "release": release,
            "version": version,
            "source_sha256": sha,
            "source_name": source_name,
            "licence_ref": licence,
            "codes_in_release": len(codes),
            "active_in_release": report["active_in_release"],
            "retire_missing": bool(retire_missing),
            "loaded_at": loaded_at,
            "loaded_by": operator,
        }
        conn.execute(
            "INSERT INTO terminology (code_system, code, display, is_active) VALUES (?, ?, ?, 0) "
            "ON CONFLICT(code_system, code) DO UPDATE SET display = excluded.display, is_active = 0",
            (RELEASE_CODE_SYSTEM, system, json.dumps(info, sort_keys=True)),
        )
        from services.audit_service import log_audit_event

        log_audit_event(
            correlation_id=f"terminology-release-{system}-{uuid.uuid4().hex[:8]}",
            event_category="clinical_config",
            action_type="terminology_release_loaded",
            outcome="success",
            db=conn,
            summary_message=(
                f"{release} {version} loaded into {system}: "
                f"{counts['inserted']} new, {counts['display_updated']} reworded, "
                f"{counts['deactivated'] + counts['retired_missing']} no longer valid"
            ),
            actor_user_id=None,
            actor_role="console",
            source_type="console",
            target_type="terminology_release",
            target_id=system,
            metadata={
                **{k: v for k, v in report.items() if k != "previous_release"},
                "previous_release": (previous is not None and _release_identity(previous))
                and {
                    "release": previous.get("release"),
                    "version": previous.get("version"),
                    "source_sha256": previous.get("source_sha256"),
                },
                "operator": operator,
                "script": "load_terminology.py",
            },
        )
        report["applied"] = True
        report["loaded_at"] = loaded_at
    return report


def _diagnosis_list(text) -> list | None:
    try:
        value = json.loads(text) if text else []
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, list) else None


def revalidate_draft_diagnoses(*, operator: str | None = None, dry_run: bool = False) -> dict:
    """Re-check the coded diagnoses on UNSIGNED outpatient notes against the
    codes `terminology` holds now, on request only (opd-20).

    A coded diagnosis on a draft becomes 'validated' when its code is active
    now and 'unvalidated' when it is not, by the same exact lookup the note
    used when it was saved (validate_code). The doctor's text and code are
    never changed, a diagnosis is never newly ticked for the problem list,
    and one that is no longer valid loses its tick. The draft's row_version
    is not bumped (the doctor's next save checks every code again anyway, so
    an open editor is never pushed into a conflict).

    A SIGNED note is never rewritten: the UPDATE names status 'draft' and the
    table's trigger refuses any change to a signed row. Signed notes that hold
    an unvalidated code which would validate now are only counted.

    Each changed draft is audited (clinical_record, in the transaction) and
    the run once (clinical_config, fail-closed)."""
    report = {
        "dry_run": bool(dry_run),
        "applied": False,
        "drafts_checked": 0,
        "drafts_changed": 0,
        "diagnoses_validated": 0,
        "diagnoses_unvalidated": 0,
        "unreadable_drafts": 0,
        "signed_with_codes_now_valid": 0,
        "signed_untouched": True,
    }
    with db_session(immediate=not dry_run) as conn:
        if (
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'opd_consultations'"
            ).fetchone()
            is None
        ):
            report["table_absent"] = True
            return report

        def active(system: str, code: str) -> bool:
            return (
                conn.execute(
                    "SELECT 1 FROM terminology WHERE code_system = ? AND code = ? AND is_active = 1 "
                    "LIMIT 1",
                    (system, code),
                ).fetchone()
                is not None
            )

        changed: list[tuple[dict, list, list, list]] = []
        for row in conn.execute(
            "SELECT id, visit_id, patient_id, diagnoses_json FROM opd_consultations "
            "WHERE status = 'draft' AND diagnoses_json IS NOT NULL ORDER BY id"
        ).fetchall():
            report["drafts_checked"] += 1
            items = _diagnosis_list(row["diagnoses_json"])
            if items is None:
                report["unreadable_drafts"] += 1
                continue
            now_valid: list[dict]
            now_invalid: list[dict]
            now_valid, now_invalid, out = [], [], []
            for item in items:
                if not isinstance(item, dict):
                    out.append(item)
                    continue
                item = dict(item)
                system = str(item.get("code_system") or "").upper()
                code = item.get("code")
                status = item.get("code_status")
                if (
                    code
                    and system in DIAGNOSIS_CODE_SYSTEMS
                    and status in ("validated", "unvalidated")
                ):
                    fresh = "validated" if active(system, str(code)) else "unvalidated"
                    if fresh != status:
                        item["code_status"] = fresh
                        (now_valid if fresh == "validated" else now_invalid).append(code)
                    if fresh != "validated":
                        item["add_to_problems"] = False
                out.append(item)
            if now_valid or now_invalid:
                changed.append((dict(row), out, now_valid, now_invalid))
                report["diagnoses_validated"] += len(now_valid)
                report["diagnoses_unvalidated"] += len(now_invalid)
        report["drafts_changed"] = len(changed)

        for row in conn.execute(
            "SELECT diagnoses_json FROM opd_consultations WHERE status = 'signed' "
            "AND diagnoses_json LIKE '%unvalidated%'"
        ).fetchall():
            for item in _diagnosis_list(row["diagnoses_json"]) or []:
                if (
                    isinstance(item, dict)
                    and item.get("code_status") == "unvalidated"
                    and item.get("code")
                    and str(item.get("code_system") or "").upper() in DIAGNOSIS_CODE_SYSTEMS
                    and active(str(item.get("code_system")).upper(), str(item["code"]))
                ):
                    report["signed_with_codes_now_valid"] += 1
                    break

        if dry_run or not changed:
            return report

        from services.audit_service import log_audit_event

        correlation = f"terminology-revalidate-{uuid.uuid4().hex[:8]}"
        for row, items, now_valid, now_invalid in changed:
            cur = conn.execute(
                "UPDATE opd_consultations SET diagnoses_json = ? WHERE id = ? AND status = 'draft'",
                (json.dumps(items), row["id"]),
            )
            if (cur.rowcount or 0) != 1:
                # Signed (or gone) since it was read: left exactly as it is.
                report["drafts_changed"] -= 1
                report["diagnoses_validated"] -= len(now_valid)
                report["diagnoses_unvalidated"] -= len(now_invalid)
                continue
            log_audit_event(
                correlation_id=correlation,
                event_category="clinical_record",
                action_type="opd_diagnosis_codes_revalidated",
                outcome="success",
                db=conn,
                summary_message=(
                    f"Diagnosis codes on draft {row['id']} checked again: "
                    f"{len(now_valid)} now valid, {len(now_invalid)} no longer valid"
                ),
                actor_user_id=None,
                actor_role="console",
                source_type="console",
                patient_id=row["patient_id"],
                target_type="opd_consultation",
                target_id=str(row["id"]),
                metadata={
                    "visit_id": row["visit_id"],
                    "now_valid": now_valid,
                    "no_longer_valid": now_invalid,
                    "operator": operator,
                    "script": "load_terminology.py",
                    "written": "in_transaction",
                },
            )
        log_audit_event(
            correlation_id=correlation,
            event_category="clinical_config",
            action_type="terminology_drafts_revalidated",
            outcome="success",
            db=conn,
            summary_message=(
                f"Diagnosis codes on {report['drafts_changed']} draft outpatient "
                "note(s) checked again; signed notes untouched"
            ),
            actor_user_id=None,
            actor_role="console",
            source_type="console",
            target_type="terminology_release",
            target_id="drafts",
            metadata={**report, "operator": operator, "script": "load_terminology.py"},
        )
        report["applied"] = True
    return report


if __name__ == "__main__":
    # Standalone seeding: `python -m services.terminology_service`
    from database import init_db

    init_db()
    result = seed_terminology()
    print(f"Terminology seeded at {result['timestamp']}")
    print(f"  starter rows: {result['seeded']}")
    for system, n in sorted(result["counts"].items()):
        print(f"  {system}: {n} active codes")
