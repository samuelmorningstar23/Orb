"""Orb Hospital OS — Revenue Integrity / Coding & CDI Service

An AI-assisted medical-coding and Clinical Documentation Improvement (CDI)
engine. Given a patient it:

  1. Suggests ICD-10 / SNOMED codes by lexically scanning the patient's
     diagnosis, coded problem list, documented comorbidities, clinical notes
     and scribe notes for known clinical terms.
  2. Flags documentation / coding gaps ("CDI flags"), for example a documented
     comorbidity missing from the coded problem list, or a low-specificity
     ("unspecified") code that could be sharpened. A condition only a
     medication implies is never a code: it is a documentation query to the
     clinician ("Treated and not documented: ask the clinician").
  3. Produces an ILLUSTRATIVE reimbursement-capture estimate (captured today vs
     potential-with-CDI vs the uplift), from a transparent, editable assumption
     table.

DESIGN — deterministic first, LLM optional
──────────────────────────────────────────
The engine is fully deterministic and runs WITHOUT any LLM: it is a curated
keyword/lexical mapper plus a rules-based CDI layer plus an arithmetic
reimbursement model. Every emitted code is validated against the local
`terminology` table via services.terminology_service, so we never surface a
code that does not exist in the deployment's loaded terminology.

An optional `use_llm` hook (`suggest_codes(patient_id, use_llm=True)`) is wired
so that, WHERE an LLM (Ollama/MedGemma) is available, it could refine/re-rank
the deterministic suggestions. In this environment the LLM is not available, so
the hook is a clearly-marked no-op stub and the deterministic path is what runs.

⚠️  REIMBURSEMENT NUMBERS ARE ILLUSTRATIVE — NOT REAL PAYER RATES
The dollar figures are derived from an internal, documented default table of
relative weights times an illustrative base rate. They are modelled loosely on
the *concept* of CMS MS-DRG relative weights (a code's relative resource
weight × a hospital base payment rate), but the specific weights and base rate
here are hand-picked placeholders for demonstration/planning. They are NOT
CMS/payer fee-schedule amounts and must not be used for actual billing. A
deploying hospital is expected to replace `DEFAULT_CODE_WEIGHTS` / `BASE_RATE_INR`
with their own contracted rates. See CITATIONS below.

CITATIONS / references for the illustrative model
  * CMS MS-DRG relative weights & the "relative weight × base rate" payment
    concept: CMS Acute Inpatient PPS, https://www.cms.gov/medicare/payment/prospective-payment-systems/acute-inpatient-pps
  * ICD-10-CM (freely redistributable, CMS/WHO) — the code system suggested here.
  * CDI concepts (query for specificity, CC/MCC capture): ACDIS / AHIMA CDI
    practice briefs (general professional guidance, not a rate source).
"""
from __future__ import annotations

import json
import re

from database import db_session
from services import terminology_service


# ══════════════════════════════════════════════════════════════════════════
#  TERM → CODE MAP  (curated, validated at emit time against `terminology`)
# ══════════════════════════════════════════════════════════════════════════
# Each entry maps a clinical concept (with a list of surface terms / synonyms /
# abbreviations) to an ICD-10 code and, where one exists in the seed, a SNOMED
# code. Only codes actually present + active in the `terminology` table are
# ever emitted (validated via terminology_service.validate_code), so a partial
# terminology load simply yields fewer suggestions rather than bogus codes.
#
# `symptom=True` marks findings/symptoms that are documented but do not, on
# their own, drive inpatient reimbursement (they carry zero illustrative weight).

_CONCEPTS: list[dict] = [
    # ── Conditions (reimbursement-relevant) ──────────────────────────────────
    {"label": "Sepsis", "terms": ["sepsis", "septic", "septicaemia", "septicemia", "bacteremia", "bacteraemia"],
     "icd10": "A41.9"},
    {"label": "Pneumonia", "terms": ["pneumonia", "cap", "hap", "lower respiratory tract infection", "lrti", "chest infection"],
     "icd10": "J18.9", "snomed": "233604007"},
    {"label": "Type 2 diabetes mellitus", "terms": ["diabetes", "diabetic", "diabetes mellitus", "t2dm", "dm2", "niddm", "hyperglycemia", "hyperglycaemia"],
     "icd10": "E11.9", "snomed": "73211009"},
    {"label": "Essential hypertension", "terms": ["hypertension", "hypertensive", "htn", "high blood pressure", "elevated blood pressure"],
     "icd10": "I10", "snomed": "38341003"},
    {"label": "Heart failure", "terms": ["heart failure", "chf", "congestive heart failure", "cardiac failure", "hfref", "hfpef", "decompensated heart failure"],
     "icd10": "I50.9"},
    {"label": "COPD", "terms": ["copd", "chronic obstructive pulmonary disease", "emphysema", "chronic bronchitis", "aecopd"],
     "icd10": "J44.9"},
    {"label": "Asthma", "terms": ["asthma", "asthmatic", "reactive airway disease", "bronchospasm"],
     "icd10": "J45.909", "snomed": "195967001"},
    {"label": "Acute kidney injury", "terms": ["acute kidney injury", "aki", "acute renal failure", "arf", "acute kidney failure"],
     "icd10": "N17.9"},
    {"label": "Acute myocardial infarction", "terms": ["myocardial infarction", "mi", "nstemi", "stemi", "heart attack", "acute coronary syndrome", "acs"],
     "icd10": "I21.9", "snomed": "22298006"},
    {"label": "Atrial fibrillation", "terms": ["atrial fibrillation", "afib", "a-fib", "af", "atrial flutter"],
     "icd10": "I48.91"},
    {"label": "Urinary tract infection", "terms": ["urinary tract infection", "uti", "cystitis", "urosepsis", "pyelonephritis"],
     "icd10": "N39.0"},
    {"label": "Hyperlipidemia", "terms": ["hyperlipidemia", "hyperlipidaemia", "dyslipidemia", "dyslipidaemia", "hypercholesterolemia", "high cholesterol"],
     "icd10": "E78.5"},
    {"label": "Anemia", "terms": ["anemia", "anaemia", "anemic", "low haemoglobin", "low hemoglobin"],
     "icd10": "D64.9", "snomed": "271737000"},
    {"label": "GERD", "terms": ["gerd", "gord", "gastro-esophageal reflux", "gastroesophageal reflux", "acid reflux", "reflux disease"],
     "icd10": "K21.9"},
    {"label": "Cerebral infarction (stroke)", "terms": ["stroke", "cva", "cerebral infarction", "ischemic stroke", "ischaemic stroke", "cerebrovascular accident"],
     "icd10": "I63.9"},
    {"label": "Epilepsy", "terms": ["epilepsy", "seizure disorder", "epileptic"],
     "icd10": "G40.909"},
    {"label": "Major depressive disorder", "terms": ["depression", "major depressive disorder", "mdd", "depressive episode"],
     "icd10": "F32.9"},
    {"label": "Dehydration", "terms": ["dehydration", "dehydrated", "volume depletion", "hypovolemia", "hypovolaemia"],
     "icd10": "E86.0"},

    # ── Symptoms / findings (documented but not independently reimbursable) ──
    {"label": "Chest pain", "terms": ["chest pain", "chest discomfort", "chest tightness"],
     "icd10": "R07.9", "snomed": "29857009", "symptom": True},
    {"label": "Headache", "terms": ["headache", "cephalgia"],
     "icd10": "R51.9", "snomed": "25064002", "symptom": True},
    {"label": "Abdominal pain", "terms": ["abdominal pain", "abdo pain", "stomach pain", "belly pain"],
     "icd10": "R10.9", "snomed": "21522001", "symptom": True},
    {"label": "Fever", "terms": ["fever", "pyrexia", "febrile"],
     "icd10": "R50.9", "snomed": "386661006", "symptom": True},
    {"label": "Dyspnea", "terms": ["dyspnea", "dyspnoea", "shortness of breath", "sob", "breathlessness"],
     "snomed": "267036007", "symptom": True},
    {"label": "Cough", "terms": ["cough", "productive cough", "dry cough"],
     "snomed": "49727002", "symptom": True},
    {"label": "Nausea", "terms": ["nausea", "nauseated", "nauseous"],
     "snomed": "422587007", "symptom": True},
    {"label": "Diarrhea", "terms": ["diarrhea", "diarrhoea", "loose stools"],
     "snomed": "62315008", "symptom": True},
    {"label": "Dizziness", "terms": ["dizziness", "dizzy", "lightheadedness", "vertigo"],
     "snomed": "404640003", "symptom": True},
]


# ══════════════════════════════════════════════════════════════════════════
#  MEDICATION → CONDITION rules (drive the documentation queries)
# ══════════════════════════════════════════════════════════════════════════
# An active medication strongly indicative of a chronic condition is a classic
# CDI signal: the drug is documented but the diagnosis it treats was never
# coded, so its reimbursement weight is being left on the table. Only rules
# whose target code exists in `terminology` fire.
_MED_CONDITION_RULES: list[dict] = [
    {"drugs": ["metformin", "glipizide", "glimepiride", "gliclazide", "insulin", "sitagliptin", "empagliflozin", "dapagliflozin"],
     "icd10": "E11.9", "label": "Type 2 diabetes mellitus"},
    {"drugs": ["atorvastatin", "simvastatin", "rosuvastatin", "pravastatin", "lovastatin"],
     "icd10": "E78.5", "label": "Hyperlipidemia"},
    {"drugs": ["lisinopril", "ramipril", "amlodipine", "losartan", "valsartan", "hydrochlorothiazide", "enalapril"],
     "icd10": "I10", "label": "Essential hypertension"},
    {"drugs": ["furosemide", "lasix", "bumetanide", "sacubitril", "spironolactone", "carvedilol", "bisoprolol"],
     "icd10": "I50.9", "label": "Heart failure"},
    {"drugs": ["tiotropium", "ipratropium", "salbutamol", "albuterol", "budesonide/formoterol", "roflumilast"],
     "icd10": "J44.9", "label": "COPD"},
    {"drugs": ["warfarin", "apixaban", "rivaroxaban", "dabigatran", "edoxaban"],
     "icd10": "I48.91", "label": "Atrial fibrillation"},
    {"drugs": ["levetiracetam", "keppra", "phenytoin", "valproate", "lamotrigine", "carbamazepine"],
     "icd10": "G40.909", "label": "Epilepsy"},
    {"drugs": ["sertraline", "fluoxetine", "citalopram", "escitalopram", "mirtazapine", "venlafaxine"],
     "icd10": "F32.9", "label": "Major depressive disorder"},
]


# ══════════════════════════════════════════════════════════════════════════
#  REIMBURSEMENT ASSUMPTION TABLE  (ILLUSTRATIVE — see module docstring)
# ══════════════════════════════════════════════════════════════════════════
# Illustrative hospital base payment rate, in RUPEES. Modelled on the CMS MS-DRG
# concept of "payment = relative weight x base rate", but this specific number is a
# placeholder for demonstration/planning, NOT a real payer amount. Sized for an
# Indian private-hospital inpatient case; PM-JAY package rates run lower.
BASE_RATE_INR: float = 50000.0

# Illustrative relative weight per code (a code's relative resource/acuity
# weight). Higher-acuity conditions and CC/MCC-type comorbidities carry more
# weight; symptoms/findings carry 0 because they do not independently drive an
# inpatient DRG. EDIT THESE to your contracted case-mix — they are placeholders.
DEFAULT_CODE_WEIGHTS: dict[str, float] = {
    # higher-acuity conditions
    "A41.9": 1.80,   # sepsis
    "I21.9": 1.60,   # acute MI
    "I63.9": 1.40,   # stroke
    "I50.9": 1.20,   # heart failure
    "J18.9": 1.00,   # pneumonia
    "N17.9": 1.00,   # acute kidney injury
    "J44.9": 0.90,   # COPD
    "G40.909": 0.60, # epilepsy
    "N39.0": 0.50,   # UTI
    "J45.909": 0.40, # asthma
    "I48.91": 0.40,  # atrial fibrillation
    "E86.0": 0.40,   # dehydration
    # chronic comorbidities (CC-type capture)
    "E11.9": 0.35,   # diabetes
    "D64.9": 0.35,   # anemia
    "F32.9": 0.30,   # depression
    "I10": 0.15,     # hypertension
    "E78.5": 0.10,   # hyperlipidemia
    "K21.9": 0.10,   # GERD
    # symptoms / findings — no independent reimbursement weight
    "R07.9": 0.0, "R51.9": 0.0, "R10.9": 0.0, "R50.9": 0.0,
}

# Illustrative uplift attributed to sharpening a single low-specificity
# ("unspecified") code toward a more specific code that captures a CC/MCC.
# A fixed placeholder, cited as an editable assumption.
NONSPECIFIC_SPECIFICITY_UPLIFT_INR: float = 2000.0


def _code_value_inr(code: str) -> float:
    """Illustrative dollar value of a code = relative weight × base rate."""
    return round(DEFAULT_CODE_WEIGHTS.get(code, 0.0) * BASE_RATE_INR, 2)


# ══════════════════════════════════════════════════════════════════════════
#  DB READS  (pure reads — this service never writes)
# ══════════════════════════════════════════════════════════════════════════

def _load_patient_context(patient_id: str) -> dict:
    """Gather every free-text + coded source we scan, in one place.

    Returns dict with: diagnosis, comorbidities, note_text, coded_problems
    (list of {code, system}), and medications (list of drug names).
    """
    ctx = {
        "exists": False,
        "diagnosis": "",
        "comorbidities": "",
        "notes": [],           # (label, text)
        # The same notes with where they came from, for the evidence sentence:
        # {label, text, note_id, note_type, author_id, author}
        "note_sources": [],
        "coded_problems": [],  # {"code","system"}
        "medications": [],     # drug name strings
    }
    with db_session() as conn:
        prow = conn.execute(
            "SELECT id, diagnosis FROM patients WHERE id = ?", (patient_id,)
        ).fetchone()
        if not prow:
            return ctx
        ctx["exists"] = True
        ctx["diagnosis"] = prow["diagnosis"] or ""

        prof = conn.execute(
            "SELECT comorbidities, past_medical_history FROM patient_profiles WHERE patient_id = ?",
            (patient_id,),
        ).fetchone()
        if prof:
            parts = [prof["comorbidities"] or "", prof["past_medical_history"] or ""]
            ctx["comorbidities"] = " ; ".join(p for p in parts if p)

        # Coded problem list — what is ALREADY captured.
        for r in conn.execute(
            "SELECT icd10_code, snomed_code FROM problem_list WHERE patient_id = ? AND status = 'active'",
            (patient_id,),
        ).fetchall():
            if r["icd10_code"]:
                ctx["coded_problems"].append({"code": r["icd10_code"], "system": "ICD10"})
            if r["snomed_code"]:
                ctx["coded_problems"].append({"code": r["snomed_code"], "system": "SNOMED"})

        # Narrative note text — clinical documents + scribe notes.
        for r in conn.execute(
            "SELECT d.id, d.content, d.author_id, u.name AS author_name "
            "FROM clinical_documents d LEFT JOIN users u ON u.id = d.author_id "
            "WHERE d.patient_id = ? ORDER BY d.created_at DESC LIMIT 25",
            (patient_id,),
        ).fetchall():
            if r["content"]:
                ctx["notes"].append(("clinical note", r["content"]))
                ctx["note_sources"].append({
                    "label": "clinical note", "text": r["content"], "note_id": r["id"],
                    "note_type": "clinical_document", "author_id": r["author_id"],
                    "author": r["author_name"]})
        for r in conn.execute(
            "SELECT s.id, s.raw_transcript, s.structured_note, s.author_id, u.name AS author_name "
            "FROM scribe_notes s LEFT JOIN users u ON u.id = s.author_id "
            "WHERE s.patient_id = ? ORDER BY s.created_at DESC LIMIT 25",
            (patient_id,),
        ).fetchall():
            text = " ".join(t for t in (r["raw_transcript"], r["structured_note"]) if t)
            if text:
                ctx["notes"].append(("scribe note", text))
                # The structured note is JSON. The sentence quoted to the
                # coder is read from its words, never from the JSON around
                # them ('{"assessment": "COPD ...').
                from services.note_text import note_plain_text
                plain = "\n".join(t for t in (r["raw_transcript"],
                                               note_plain_text(r["structured_note"])) if t)
                ctx["note_sources"].append({
                    "label": "scribe note", "text": text, "note_id": r["id"],
                    "note_type": "scribe_note", "author_id": r["author_id"],
                    "author": r["author_name"], "evidence_text": plain})

        # Active medications — drive med→condition CDI inference.
        # `status != 'discontinued'` alone drops rows whose status is NULL
        # (SQL three-valued logic: NULL != 'x' is NULL, not TRUE). The column is
        # nullable, and ingestion/import paths do not always set it — so a whole
        # class of charted drugs was silently invisible to CDI, under-reporting
        # capture gaps. NULL is treated as active, matching the column DEFAULT.
        for r in conn.execute(
            "SELECT drug_name FROM medications "
            "WHERE patient_id = ? AND (status IS NULL OR status != 'discontinued')",
            (patient_id,),
        ).fetchall():
            if r["drug_name"]:
                ctx["medications"].append(r["drug_name"])

    return ctx


# ══════════════════════════════════════════════════════════════════════════
#  LEXICAL MATCHING
# ══════════════════════════════════════════════════════════════════════════

def _term_matches(term: str, text: str) -> bool:
    """Whole-token, case-insensitive match so short abbreviations (MI, AKI, AF)
    don't match inside unrelated words."""
    if not text:
        return False
    pattern = r"(?<![A-Za-z0-9])" + re.escape(term) + r"(?![A-Za-z0-9])"
    return re.search(pattern, text, flags=re.IGNORECASE) is not None


# Sentence boundaries: end punctuation followed by space, or a line break. A
# structured note is JSON, so quotes and braces are trimmed off the ends too.
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")
EVIDENCE_SENTENCE_MAX = 240


def evidence_sentence(term: str, text: str) -> str | None:
    """The sentence of ``text`` that contains ``term``, capped at 240 characters.

    The whole-token match :func:`_term_matches` uses, so the sentence returned
    is the one the code was actually matched in. A sentence longer than the
    cap is cut around the term rather than from its start, so the term shows.
    """
    if not text or not term:
        return None
    for raw in _SENTENCE_SPLIT.split(text):
        sentence = raw.strip().strip('{}[]",').strip()
        if not sentence or not _term_matches(term, sentence):
            continue
        sentence = " ".join(sentence.split())
        if len(sentence) <= EVIDENCE_SENTENCE_MAX:
            return sentence
        # Room for "..." on both ends, so the result never passes the cap.
        body = EVIDENCE_SENTENCE_MAX - 6
        at = sentence.lower().find(term.lower())
        start = max(0, min(at - body // 3, len(sentence) - body))
        cut = sentence[start:start + body].strip()
        return ("..." if start > 0 else "") + cut + "..."
    return None


DOCUMENTATION_QUERY_TEXT = "Treated and not documented: ask the clinician"


def _emit_code(system: str, code: str, display_fallback: str) -> dict | None:
    """Validate against terminology; return a code dict or None if unknown."""
    if not code:
        return None
    if not terminology_service.validate_code(system, code):
        return None
    display = terminology_service.get_display(system, code) or display_fallback
    return {"code": code, "system": system.upper(), "display": display}


# ══════════════════════════════════════════════════════════════════════════
#  PUBLIC API
# ══════════════════════════════════════════════════════════════════════════

def suggest_codes(patient_id: str, use_llm: bool = False) -> dict:
    """Suggest codes, flag CDI gaps and estimate reimbursement for a patient.

    Deterministic and LLM-free by default. Pass use_llm=True to route through
    the optional refinement hook (a no-op stub unless an LLM is wired up).

    Returns:
        {
          patient_id,
          patient_found: bool,
          suggested_codes: [{code, system, display, evidence, confidence,
                             evidence_sentence, evidence_source,
                             evidence_note_id, evidence_note_type,
                             evidence_author_id, evidence_author}],
          documentation_queries: [{condition, medication, code, system,
                                   display, message}],
          cdi_flags: [{type, message, rationale, est_impact_inr}],
          reimbursement: {captured_inr, potential_inr, uplift_inr, assumptions},
        }
    """
    ctx = _load_patient_context(patient_id)

    # Ordered scan sources with an evidence label and a confidence tier.
    sources = [
        ("diagnosis", ctx["diagnosis"], 0.9, None),
        ("comorbidity list", ctx["comorbidities"], 0.75, None),
    ]
    for note in ctx["note_sources"]:
        sources.append((note["label"], note["text"], 0.7, note))

    coded_set = {(p["system"].upper(), p["code"]) for p in ctx["coded_problems"]}

    # ── 1. Lexical code suggestions ──────────────────────────────────────────
    # code_key (system, code) -> suggestion dict (keep highest-confidence hit).
    suggestions: dict[tuple[str, str], dict] = {}

    def _add_suggestion(system, code, display_fallback, evidence, confidence, where=None):
        emitted = _emit_code(system, code, display_fallback)
        if not emitted:
            return None
        key = (emitted["system"], emitted["code"])
        existing = suggestions.get(key)
        if existing is None or confidence > existing["confidence"]:
            suggestions[key] = {
                **emitted,
                "evidence": evidence,
                "confidence": round(confidence, 2),
                # The sentence the term was found in, and the note it came from
                # (None for the diagnosis and comorbidity fields).
                **(where or {}),
            }
        return key

    for concept in _CONCEPTS:
        for src_label, src_text, tier, note in sources:
            matched_term = next((t for t in concept["terms"] if _term_matches(t, src_text)), None)
            if not matched_term:
                continue
            evidence = f"Matched “{matched_term}” in {src_label}"
            where = {
                "evidence_sentence": (
                    evidence_sentence(matched_term, (note or {}).get("evidence_text") or "")
                    or evidence_sentence(matched_term, src_text)),
                "evidence_source": src_label,
                "evidence_note_id": note["note_id"] if note else None,
                "evidence_note_type": note["note_type"] if note else None,
                "evidence_author_id": note["author_id"] if note else None,
                "evidence_author": note["author"] if note else None,
            }
            if concept.get("icd10"):
                _add_suggestion("ICD10", concept["icd10"], concept["label"], evidence, tier, where)
            if concept.get("snomed"):
                _add_suggestion("SNOMED", concept["snomed"], concept["label"], evidence, tier, where)
            break  # first (highest-priority) source wins for this concept

    # ── 2. Documentation queries: a drug treats a condition nobody wrote ─────
    # A condition inferred from a medication is NOT a code suggestion: nothing
    # documents it, so it cannot be coded or billed. It used to be added to the
    # suggested codes at 0.5 and then flagged. It is now a query to the
    # clinician, listed on its own and kept out of the suggested codes (and so
    # out of the potential total, which is coded plus suggested). A condition
    # that IS documented, or already coded, raises no query.
    meds_text = " ; ".join(ctx["medications"])
    documentation_queries: list[dict] = []
    for rule in _MED_CONDITION_RULES:
        drug = next((d for d in rule["drugs"] if _term_matches(d, meds_text)), None)
        if not drug:
            continue
        code = rule["icd10"]
        if ("ICD10", code) in coded_set or ("ICD10", code) in suggestions:
            continue
        emitted = _emit_code("ICD10", code, rule["label"])
        if not emitted:
            continue
        medication = next((m for m in ctx["medications"] if _term_matches(drug, m)), drug)
        documentation_queries.append({
            "condition": rule["label"],
            "medication": medication,
            "code": code,
            "system": "ICD10",
            "display": emitted["display"],
            "message": DOCUMENTATION_QUERY_TEXT,
        })

    suggested_codes = sorted(
        suggestions.values(),
        key=lambda s: (-_code_value_inr(s["code"]), -s["confidence"], s["code"]),
    )

    # ── 3. CDI gap flags ─────────────────────────────────────────────────────
    cdi_flags: list[dict] = []
    flagged_codes: set[str] = set()  # avoid double-flagging one code as uncoded

    # (a) was "medication present, condition not coded". A condition only a
    #     drug implies is now a documentation query (step 2), not a flag.

    # (b) Condition documented in narrative/diagnosis but not on the coded
    #     problem list (excludes symptoms).
    for s in suggested_codes:
        if s["system"] != "ICD10":
            continue
        code = s["code"]
        if code in flagged_codes:
            continue
        if ("ICD10", code) in coded_set:
            continue
        value = _code_value_inr(code)
        if value <= 0:
            continue  # symptoms / zero-weight → not a capture gap
        flagged_codes.add(code)
        cdi_flags.append({
            "type": "documented_not_coded",
            "message": f"{s['display']} ({code}) appears in documentation but is not on the coded problem list.",
            "rationale": f"Evidence: {s['evidence']}. Documented conditions that are not coded are not reimbursed. Review and add to the coded problem list if supported.",
            "est_impact_inr": value,
        })

    # (c) Low-specificity ("unspecified") codes that could be sharpened.
    for s in suggested_codes:
        display = s["display"].lower()
        if "unspecified" not in display and "not specified" not in display:
            continue
        cdi_flags.append({
            "type": "nonspecific_code",
            "message": f"{s['display']} ({s['code']}) is a low-specificity code: a more specific code may capture additional acuity.",
            "rationale": "Unspecified codes frequently under-capture CC/MCC severity. A documentation query for laterality/organism/acuity/type can support a more specific code.",
            "est_impact_inr": NONSPECIFIC_SPECIFICITY_UPLIFT_INR,
        })

    # ── 4. Illustrative reimbursement estimate ───────────────────────────────
    # captured = value of codes ALREADY on the coded problem list.
    # potential = value of the union (coded ∪ suggested).
    # ICD-10 only on both sides: DEFAULT_CODE_WEIGHTS is an ICD-10-keyed table, so
    # a SNOMED problem-list entry contributed 0 to `captured` while still being
    # listed in the published `code_weights` assumption block as a weight-0 code —
    # which read as "this code is worth nothing" rather than "this model does not
    # price SNOMED". The money math and the assumptions it publishes now agree.
    coded_codes = {p["code"] for p in ctx["coded_problems"] if p["system"].upper() == "ICD10"}
    suggested_icd = {s["code"] for s in suggested_codes if s["system"] == "ICD10"}
    all_potential = coded_codes | suggested_icd

    captured = round(sum(_code_value_inr(c) for c in coded_codes), 2)
    # The sharper-code flags (c) carry their own placeholder amount. They were
    # listed with "+₹2,000" each but left out of the total, so the headline
    # read ₹4,000 short of the flags printed under it.
    specificity_uplift = round(
        sum(f["est_impact_inr"] for f in cdi_flags if f["type"] == "nonspecific_code"), 2)
    potential = round(sum(_code_value_inr(c) for c in all_potential) + specificity_uplift, 2)
    uplift = round(max(potential - captured, 0.0), 2)

    reimbursement = {
        "captured_inr": captured,
        "potential_inr": potential,
        "uplift_inr": uplift,
        "specificity_uplift_inr": specificity_uplift,
        "assumptions": {
            "model": "ILLUSTRATIVE: not real payer rates",
            "method": "per-code value = relative weight × base rate; captured = coded problem list, potential = coded ∪ suggested plus the sharper-code amount for each low-specificity code, uplift = potential − captured. Symptoms carry zero weight.",
            "base_rate_inr": BASE_RATE_INR,
            "nonspecific_specificity_uplift_inr": NONSPECIFIC_SPECIFICITY_UPLIFT_INR,
            "code_weights": {c: DEFAULT_CODE_WEIGHTS.get(c, 0.0) for c in sorted(all_potential)},
            "caveats": [
                "Figures are hand-picked placeholders for demonstration/planning, NOT CMS/payer fee-schedule amounts.",
                "Rupee placeholders. Replace BASE_RATE_INR and DEFAULT_CODE_WEIGHTS with this hospital's contracted case-mix / package rates (e.g. PM-JAY HBP or your private tariff).",
                "Estimates assume documented conditions are clinically valid; a coder/CDI specialist must confirm before billing.",
            ],
            "citations": [
                "CMS MS-DRG relative weights & 'relative weight × base rate' concept: https://www.cms.gov/medicare/payment/prospective-payment-systems/acute-inpatient-pps",
                "ICD-10-CM (CMS/WHO, freely redistributable).",
                "CDI specificity / CC-MCC capture practice: ACDIS / AHIMA CDI briefs.",
            ],
        },
    }

    result = {
        "patient_id": patient_id,
        # Callers MUST distinguish "no capture gaps on this patient" from "this
        # patient id does not exist". Both used to return the same all-zero
        # report, so a typo'd or stale id read as a clean CDI review.
        "patient_found": ctx["exists"],
        "suggested_codes": suggested_codes,
        "documentation_queries": documentation_queries,
        "cdi_flags": cdi_flags,
        "reimbursement": reimbursement,
    }

    if use_llm:
        result = _llm_refine(patient_id, ctx, result)

    return result


# ══════════════════════════════════════════════════════════════════════════
#  OPTIONAL LLM REFINEMENT  (stub — deterministic path is what runs)
# ══════════════════════════════════════════════════════════════════════════

def _llm_available() -> bool:
    """Whether a local LLM (Ollama/MedGemma) is reachable. In this environment
    it is not, so this returns False and the deterministic result stands."""
    try:
        # Wired for later: a real deployment would probe the configured Ollama
        # host / model here (e.g. services.llm_service.health()). We keep this a
        # pure, dependency-free no-op so importing the module never spins up or
        # requires the model.
        return False
    except Exception:
        return False


def _llm_refine(patient_id: str, ctx: dict, result: dict) -> dict:
    """Optional enhancement hook.

    IF an LLM were available, this is where MedGemma would re-rank / de-duplicate
    the deterministic suggestions, adjust confidences from full-note context, and
    draft CDI query language. It is intentionally a marked no-op stub so the
    deterministic engine remains the source of truth and this environment (no
    LLM) behaves identically with or without `use_llm=True`.
    """
    if not _llm_available():
        result.setdefault("meta", {})["llm"] = "unavailable, deterministic result returned unchanged"
        return result
    # (Unreached here.) Real refinement would go here and must still only emit
    # codes validated via terminology_service.validate_code.
    return result
