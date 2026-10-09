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
"""
import uuid
from datetime import datetime, timezone

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
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone() is not None


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
        kind = "lab"
    elif system == "LOCAL":
        kind = _local_type(value) or ("lab" if catalogue is not None else None)
    else:
        kind = None
    display = (catalogue["name"] if in_catalogue else None) or (term["display"] if term else None)
    if term is None and not in_catalogue:
        return out
    out.update(order_type=kind, display=display, refusal=None)
    if kind not in INVESTIGATION_ORDER_TYPES:
        out["refusal"] = "not_an_investigation"
    elif kind == "lab" and (value.upper() in BEDSIDE_LOCAL_CODES
                            or (catalogue is not None and catalogue["performed_at"] == "ward_poct")):
        out["refusal"] = "bedside_test"
    elif kind == "lab" and lab_catalogue and not in_catalogue:
        out["refusal"] = "not_in_lab_catalogue"
    return out


def search_terms(query: str, *, kind: str, limit: int = SEARCH_MAX_RESULTS,
                 lab_catalogue: bool = False) -> dict:
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
                (*(f"%{t}" for t in _SNOMED_DIAGNOSIS_TAGS), pattern, pattern,
                 text.lower(), _like_pattern(text.lower())[1:], limit + 1),
            ).fetchall()
            rows = [{"code_system": r["code_system"], "code": r["code"],
                     "display": r["display"]} for r in found]
        else:
            rows = _investigation_rows(conn, text.lower(), pattern, limit + 1,
                                       lab_catalogue=lab_catalogue)
    answer["truncated"] = len(rows) > limit
    answer["results"] = rows[:limit]
    return answer


def _investigation_rows(conn, text: str, pattern: str, limit: int, *,
                        lab_catalogue: bool) -> list[dict]:
    """Laboratory tests and imaging a clinic may order, best match first.
    Every row also passes describe_investigation's rules."""
    bedside = sorted(BEDSIDE_LOCAL_CODES)
    marks = ",".join("?" for _ in bedside)
    poct = ""
    params_poct: tuple = ()
    if _has_table(conn, "lab_tests"):
        poct = (" AND NOT EXISTS (SELECT 1 FROM lab_tests p WHERE p.code_system = t.code_system "
                "AND p.code = t.code AND p.performed_at = 'ward_poct')")
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
            rows.append({"code_system": r["code_system"], "code": r["code"],
                         "display": r["name"], "order_type": "lab"})
        lab_sql = None
    else:
        lab_sql = (f"""SELECT t.code_system, t.code, t.display, 'lab' AS order_type
                        FROM terminology t
                       WHERE t.is_active = 1
                         AND (t.code_system = 'LOINC'
                              OR (t.code_system = 'LOCAL' AND upper(t.code) LIKE 'ORB-LAB-%'))
                         AND upper(t.code) NOT IN ({marks}){poct}
                         AND (lower(t.display) LIKE ? ESCAPE '\\' OR lower(t.code) LIKE ? ESCAPE '\\')""",
                   (*bedside, pattern, pattern))
    imaging_sql = ("""SELECT t.code_system, t.code, t.display, 'imaging' AS order_type
                       FROM terminology t
                      WHERE t.is_active = 1 AND t.code_system = 'LOCAL'
                        AND upper(t.code) LIKE 'ORB-IMG-%'
                        AND (lower(t.display) LIKE ? ESCAPE '\\' OR lower(t.code) LIKE ? ESCAPE '\\')""",
                   (pattern, pattern))
    for sql, params in ([lab_sql] if lab_sql else []) + [imaging_sql]:
        for r in conn.execute(
            f"SELECT * FROM ({sql}) ORDER BY CASE WHEN lower(code) = ? THEN 0 ELSE 1 END, "
            "display, code LIMIT ?",
            (*params, text, limit),
        ).fetchall():
            rows.append({"code_system": r["code_system"], "code": r["code"],
                         "display": r["display"], "order_type": r["order_type"]})
    rows.sort(key=lambda r: (0 if r["code"].lower() == text else 1,
                             0 if r["display"].lower().startswith(text) else 1,
                             r["display"].lower(), r["code"]))
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
    ("LOINC", "33762-6", "Natriuretic peptide B prohormone N-Terminal [Mass/volume] in Serum or Plasma"),
    ("LOINC", "1988-5", "C reactive protein [Mass/volume] in Serum or Plasma"),
    ("LOINC", "30341-2", "Erythrocyte sedimentation rate"),
    ("LOINC", "1920-8", "Aspartate aminotransferase [Enzymatic activity/volume] in Serum or Plasma"),
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
        rows = conn.execute(
            "SELECT code_system, COUNT(*) AS n FROM terminology GROUP BY code_system"
        ).fetchall()
    counts = {r["code_system"]: r["n"] for r in rows}
    return {
        "seeded": len(STARTER_TERMINOLOGY),
        "counts": counts,
        "timestamp": datetime.now(timezone.utc).isoformat(),
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


if __name__ == "__main__":
    # Standalone seeding: `python -m services.terminology_service`
    from database import init_db

    init_db()
    result = seed_terminology()
    print(f"Terminology seeded at {result['timestamp']}")
    print(f"  starter rows: {result['seeded']}")
    for system, n in sorted(result["counts"].items()):
        print(f"  {system}: {n} active codes")
