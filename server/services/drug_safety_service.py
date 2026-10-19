"""Orb — Drug Safety Service.

Order-time clinical safety checks: patient allergy matching and drug–drug
interaction screening, plus the pure rule layer for the BEDSIDE DOSE GUARD (see
the "DOSE SAFETY" section at the foot of this module). This is the pharmacology
layer whose absence a pharmacist would spot in the first minute of a demo.

Scope + honesty: the built-in knowledge base below is a SMALL, hand-curated,
clearly-extensible starter set — NOT a comprehensive interaction database. It is
enough to demonstrate the safety gate and catch the common, high-consequence
cases (penicillin/cephalosporin cross-reactivity, warfarin +
NSAID/aspirin, etc.). For production it must be backed by a licensed/maintained
source (e.g. an openFDA/DDInter-derived table loaded into a DB). The check
functions read the live patient_allergies + active medications, so swapping the
KB for a bigger table is the only change needed.

Everything dose-related in here is PURE — no DB, no I/O — for the same reason
``match_allergies`` is: the bedside path must be able to screen without a query
per drug, and the rules must be testable without a database. The DB half of the
dose guard (prior administrations, the override ledger, the audit rows) lives in
services/emar_service.py, exactly as allergy matching lives here and the bedside
allergy interlock lives in services/allergy_interlock.py.
"""
from __future__ import annotations

import hashlib
import logging
import re
from typing import Iterable, Optional

from database import db_session
from services import interaction_db

logger = logging.getLogger("orb.drug_safety")

# ── Drug → class mapping (lowercased substrings matched against drug names) ──
# Extend freely; matching is by substring so brand/generic variants are caught.
#
# CLASS MEMBERSHIP IS THE OTHER HALF OF A CLASS ALLERGY (found 2026-08-06 by
# attack, not by test). `classify_allergen` was taught in 2026-08-02 to read an
# allergy CHARTED as "NSAIDs" — but a class term is only worth what the class
# MEMBERSHIP list below covers. Probing the live API with an "NSAIDs /
# anaphylaxis / severe" allergy, these all screened CLEAN at order time AND at
# the bedside, and were administered 200 OK:
#
#     aceclofenac · nimesulide · etoricoxib · meloxicam · piroxicam · ketoprofen
#
# The first two are among the most-prescribed drugs in an Indian hospital, which
# is where this appliance is going. Cefixime and cefpodoxime — the commonest oral
# cephalosporins in India — were likewise absent, so BOTH a cephalosporin-class
# allergy and penicillin↔cephalosporin cross-reactivity missed them entirely.
#
# Two mechanisms are used below, and the split is deliberate:
#
#   * STEMS. A drug family with an unambiguous, chemically-meaningful stem gets
#     the stem ("-cillin", "-penem", "-profen", "-coxib", "-oxicam", "-pril",
#     "-xaban", "-parin"). One entry then covers the members nobody thought to
#     list, which is the failure mode this whole block exists to fix.
#   * EXPLICIT MEMBERS everywhere a stem would over-fire. "-statin" is the
#     cautionary case: NYSTATIN is an antifungal, and a stem rule would classify
#     it as a statin. Over-firing is the documented alert-fatigue hazard, so
#     statins, aminoglycosides and macrolides stay enumerated.
#
# Still a hand-curated starter set, not a licensed drug database — see the module
# docstring. It is now wide enough that a class allergy means what it says.
_DRUG_CLASS_PATTERNS: dict[str, list[str]] = {
    # "-cillin" covers amoxicillin/ampicillin/flucloxacillin/piperacillin/
    # cloxacillin/ticarcillin/benzylpenicillin and every brand spelled with it.
    # NOT "mox": it is a substring of MOXIfloxacin, a fluoroquinolone and one of
    # the standard alternatives FOR a penicillin-allergic patient. Blocking it on
    # a penicillin allergy is both wrong and the alert-fatigue hazard.
    "penicillins": ["penicillin", "cillin", "co-amoxiclav", "amoxiclav",
                    "augmentin", "tazocin", "clavam"],
    # Enumerated members PLUS the "cef"/"ceph" token-prefix rule below, which is
    # what actually makes this family complete.
    "cephalosporins": ["cephalexin", "cefalexin", "cefuroxime", "ceftriaxone",
                       "cefotaxime", "cefazolin", "ceftazidime", "cefixime",
                       "cefpodoxime", "cefadroxil", "cefepime", "cefoperazone",
                       "cefaclor", "cefprozil", "cefdinir", "ceftibuten",
                       "cefditoren", "ceftaroline", "cephradine", "cefalotin"],
    "carbapenems": ["penem"],   # meropenem, imipenem, ertapenem, doripenem, faropenem
    "sulfonamides": ["sulfamethoxazole", "co-trimoxazole", "cotrimoxazole",
                     "trimethoprim-sulfa", "sulfasalazine", "sulfadiazine",
                     "sulfadoxine", "septran", "bactrim"],
    # "-profen" (ibuprofen, ketoprofen, flurbiprofen, dexketoprofen),
    # "-coxib"  (celecoxib, etoricoxib, parecoxib, rofecoxib),
    # "-oxicam" (piroxicam, meloxicam, tenoxicam, lornoxicam).
    # Paracetamol/acetaminophen is deliberately NOT here — it is not an NSAID and
    # must never fire on an NSAID allergy.
    "nsaids": ["ibuprofen", "naproxen", "diclofenac", "aceclofenac", "aspirin",
               "acetylsalicylic", "ketorolac", "indometacin", "indomethacin",
               "mefenamic", "nimesulide", "etodolac", "nabumetone", "ketoprofen",
               "profen", "coxib", "oxicam",
               # India: what is actually written on the drug chart.
               # (kept at >=6 characters: a 4-character brand token is a
               # substring of too much of the formulary to be safe)
               "ecosprin", "disprin", "brufen", "combiflam", "voveran",
               "zerodol", "dolonex", "hifenac", "aceclo", "flexon"],
    "opioids": ["morphine", "codeine", "dihydrocodeine", "oxycodone", "fentanyl",
                "fentanil",  # sufentanil, alfentanil, remifentanil
                "tramadol", "tapentadol", "pethidine", "meperidine", "methadone",
                "buprenorphine", "hydromorphone", "nalbuphine", "pentazocine",
                # common combination/brand products that contain an opioid but whose
                # name does not literally include the ingredient string:
                "co-codamol", "co-dydramol", "solpadol", "kapake", "tylex", "zapain",
                "ultracet", "fortwin"],
    # Enumerated: a "-mycin" stem would swallow vancomycin and clindamycin, which
    # are neither macrolides nor aminoglycosides.
    "macrolides": ["erythromycin", "clarithromycin", "azithromycin",
                   "roxithromycin", "spiramycin", "fidaxomicin"],
    "anticoagulants": ["warfarin", "heparin", "parin",   # enoxaparin, dalteparin, tinzaparin, fondaparinux
                       "xaban",                          # apixaban, rivaroxaban, edoxaban
                       "dabigatran", "acenocoumarol"],
    # NO "-statin" stem: nystatin is an antifungal and would be misclassified.
    "statins": ["simvastatin", "atorvastatin", "rosuvastatin", "pravastatin",
                "lovastatin", "fluvastatin", "pitavastatin"],
    "ace_inhibitors": ["pril"],  # ramipril, lisinopril, enalapril, perindopril, captopril…
    "potassium": ["potassium chloride", "sando-k", "kcl"],
    # Enumerated for the same reason as macrolides.
    "aminoglycosides": ["gentamicin", "amikacin", "tobramycin", "streptomycin",
                        "neomycin", "netilmicin", "kanamycin"],
}

# Short family stems that are only safe when they START A TOKEN. "cef" as a bare
# substring would match anywhere in a name; anchored to a token boundary it
# reliably means a cephalosporin and covers the whole family — including the
# members and brands nobody enumerated, which is the bug this fixes.
_DRUG_CLASS_TOKEN_PREFIXES: dict[str, tuple[str, ...]] = {
    "cephalosporins": ("cef", "ceph"),
}

_WORD_RE = re.compile(r"[a-z][a-z0-9'\-]*")

# Known cross-reactivity, declared as undirected pairs (class_a, class_b, severity).
# Expanded into a SYMMETRIC lookup below so the alert fires regardless of which
# side is the documented allergy (a cephalosporin-allergic patient prescribed a
# penicillin must alert just as a penicillin-allergic patient prescribed a
# cephalosporin does).
_CROSS_REACTIVITY_PAIRS: list[tuple[str, str, str]] = [
    ("penicillins", "cephalosporins", "moderate"),  # ~1-3% cross-reactivity
    ("penicillins", "carbapenems", "moderate"),
]

_CROSS_REACTIVITY: dict[str, list[tuple[str, str]]] = {}
for _a, _b, _sev in _CROSS_REACTIVITY_PAIRS:
    _CROSS_REACTIVITY.setdefault(_a, []).append((_b, _sev))
    _CROSS_REACTIVITY.setdefault(_b, []).append((_a, _sev))

# ── Drug–drug interactions ───────────────────────────────────────────────────
# The hardcoded interaction list that used to live here has been replaced by a
# data-backed table (drug_interactions). Its curated seed — a meaningfully
# expanded superset of the old pairs — lives in services/interaction_db.py and
# is bulk-loadable from external sources (DDInter, etc.). check_interactions
# below queries that table via interaction_db.lookup_interactions.

_SEVERITY_RANK = {"none": 0, "mild": 1, "moderate": 2, "severe": 3}


def classify_drug(drug_name: str) -> list[str]:
    """Return the drug classes a drug name belongs to (possibly several).

    Substring match against :data:`_DRUG_CLASS_PATTERNS`, then the anchored
    token-prefix rules in :data:`_DRUG_CLASS_TOKEN_PREFIXES` (see why the two
    mechanisms are separate in the comment above those tables).
    """
    if not drug_name:
        return []
    n = drug_name.lower()
    classes = []
    for cls, patterns in _DRUG_CLASS_PATTERNS.items():
        if any(p in n for p in patterns):
            classes.append(cls)
    if _DRUG_CLASS_TOKEN_PREFIXES:
        tokens = _WORD_RE.findall(n)
        for cls, prefixes in _DRUG_CLASS_TOKEN_PREFIXES.items():
            if cls in classes:
                continue
            if any(t.startswith(prefixes) for t in tokens):
                classes.append(cls)
    return classes


# How a clinician CHARTS a class-level allergy. Not the same vocabulary as
# _DRUG_CLASS_PATTERNS, which lists class MEMBERS — this maps the class itself.
#
# WHY THIS EXISTS. `classify_drug` answers "what class is this drug in", and the
# matcher was calling it on the allergen too. That works whenever the allergen is
# also a drug name — "Penicillin" is both a substance and a class, so a penicillin
# allergy correctly flagged amoxicillin. It silently failed for classes that are
# NOT drug names: an allergy charted as "NSAIDs" — which is exactly how an NSAID
# allergy is usually recorded — classified as nothing, so ibuprofen, diclofenac
# and naproxen all screened clean. Found 2026-08-02 by probing the matcher rather
# than by a test, because every test used a drug name as the allergen.
#
# Deliberately conservative. Each entry is a term a clinician would actually
# write, matched on WORD BOUNDARIES (see below) rather than as a substring: a
# bare "ace" or "kcl" substring would fire on unrelated text, and an interlock
# that over-fires trains staff to click through, which is worse than no interlock.
_ALLERGEN_CLASS_TERMS: dict[str, list[str]] = {
    "penicillins": ["penicillin", "penicillins"],
    "cephalosporins": ["cephalosporin", "cephalosporins", "cephalosporin antibiotics"],
    "carbapenems": ["carbapenem", "carbapenems"],
    "sulfonamides": ["sulfonamide", "sulfonamides", "sulphonamide", "sulphonamides",
                     "sulfa", "sulpha", "sulfa drugs"],
    "nsaids": ["nsaid", "nsaids", "non-steroidal anti-inflammatory",
               "non steroidal anti inflammatory", "nonsteroidal anti-inflammatory"],
    "opioids": ["opioid", "opioids", "opiate", "opiates"],
    "macrolides": ["macrolide", "macrolides"],
    "anticoagulants": ["anticoagulant", "anticoagulants"],
    "statins": ["statin", "statins"],
    "ace_inhibitors": ["ace inhibitor", "ace inhibitors", "ace-inhibitor", "ace-inhibitors"],
    "aminoglycosides": ["aminoglycoside", "aminoglycosides"],
}


def classify_allergen(substance: str) -> list[str]:
    """Classes implied by an allergen AS CHARTED.

    Union of (a) treating it as a drug name — "Penicillin" → penicillins — and
    (b) reading it as a class term — "NSAIDs" → nsaids. Both are needed: charting
    practice mixes substances and classes freely in the same column.

    Matched on word boundaries so "ACE inhibitor" does not fire on "face", and
    "sulfa" does not fire on "sulfasalazine" by accident (that one is a real
    member and is caught by classify_drug anyway).
    """
    if not substance:
        return []
    n = re.sub(r"[^a-z0-9 -]+", " ", substance.lower())
    padded = f" {re.sub(r'[-]', ' ', n)} "
    classes = set(classify_drug(substance))
    for cls, terms in _ALLERGEN_CLASS_TERMS.items():
        for t in terms:
            if f" {t.replace('-', ' ')} " in padded:
                classes.add(cls)
                break
    return sorted(classes)


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


# ── Interaction vocabulary ───────────────────────────────────────────────────
# WHY THIS IS NOT classify_drug. The curated interaction rows (interaction_db
# _BUILTIN) and every loaded DDInter row name drugs by INGREDIENT ("warfarin",
# "amiodarone") or by a pharmacological GROUP ("azoles", "qt_prolonging") that
# classify_drug never emits: its twelve classes are ALLERGY classes. Screening
# with classify_drug alone meant 48 of the 55 curated rows could never fire, and
# warfarin with amiodarone on the chart came back "screened" with no alert
# (found 2026-10-01). The fix is not to widen classify_drug: a shared
# "qt_prolonging" class would make an amiodarone allergy alert on haloperidol.
#
# So interaction screening asks for interaction_terms(): classify_drug's classes
# PLUS the groups below PLUS the ingredient words of the name itself, which is
# what lets an ingredient row (curated or DDInter) match "Warfarin 5mg tablet".
#
# Groups are ENUMERATED members, never bare stems, wherever a stem over-fires
# ("prazole" is inside aripiprazole, "azole" inside metronidazole and every PPI).
# Brand aliases map a common Indian brand to the ingredient term the rows use.
# Deliberately left out pending clinical review: ondansetron and domperidone
# from qt_prolonging (each pairs with the other and with every QT drug as a
# severe, near-universal alert), and linezolid from maoi (it would make every
# opioid a severe alert). Those drugs are reported as NOT screened instead.
_INTERACTION_GROUP_PATTERNS: dict[str, tuple[str, ...]] = {
    # Ingredient terms reached through brand or salt names.
    "aspirin": ("aspirin", "acetylsalicylic", "ecosprin", "disprin"),
    "digoxin": ("digoxin", "lanoxin"),
    "metronidazole": ("metronidazole", "flagyl", "metrogyl"),
    "trimethoprim": ("trimethoprim", "co-trimoxazole", "cotrimoxazole", "septran", "bactrim"),
    "spironolactone": ("spironolactone", "aldactone"),
    "tramadol": ("tramadol", "ultracet"),
    "ciclosporin": ("ciclosporin", "cyclosporin"),
    "theophylline": ("theophylline", "aminophylline", "deriphyllin"),
    # Groups named by the curated rows.
    "azoles": ("fluconazole", "itraconazole", "ketoconazole", "voriconazole",
               "posaconazole", "isavuconazole", "miconazole"),
    "ssri": ("fluoxetine", "sertraline", "paroxetine", "citalopram", "fluvoxamine"),
    "proton_pump_inhibitors": ("omeprazole", "pantoprazole", "lansoprazole",
                               "rabeprazole", "ilaprazole"),   # esomeprazole contains omeprazole
    # The clopidogrel row is specific to these two (CYP2C19); pantoprazole is the
    # usual alternative and must not fire.
    "cyp2c19_inhibiting_ppis": ("omeprazole",),                # and esomeprazole
    "potassium_sparing_diuretics": ("spironolactone", "aldactone", "eplerenone",
                                    "amiloride", "triamterene"),
    "fibrates": ("fenofibrate", "gemfibrozil", "bezafibrate", "ciprofibrate"),
    "maoi": ("phenelzine", "tranylcypromine", "isocarboxazid", "moclobemide",
             "selegiline", "rasagiline"),
    "triptans": ("triptan",),
    "sympathomimetics": ("ephedrine", "phenylephrine", "phenylpropanolamine"),
    # Potassium-losing diuretics (loop and thiazide-type): the digoxin and lithium
    # rows are about the hypokalaemia and sodium loss these cause.
    "diuretics": ("furosemide", "frusemide", "lasix", "bumetanide", "torasemide",
                  "torsemide", "thiazide", "chlorthalidone", "chlortalidone",
                  "indapamide", "metolazone"),
    "thiazides": ("thiazide", "chlorthalidone", "chlortalidone", "indapamide", "metolazone"),
    # CredibleMeds "known risk" agents outside the macrolide and fluoroquinolone
    # rows, which carry their own (moderate) QT pairs.
    "qt_prolonging": ("amiodarone", "dronedarone", "sotalol", "quinidine",
                      "disopyramide", "procainamide", "flecainide", "haloperidol",
                      "droperidol", "chlorpromazine", "thioridazine", "pimozide",
                      "methadone", "citalopram", "chloroquine"),
    "antipsychotics": ("haloperidol", "chlorpromazine", "thioridazine", "pimozide",
                       "trifluoperazine", "fluphenazine", "flupentixol", "zuclopenthixol",
                       "olanzapine", "quetiapine", "risperidone", "paliperidone",
                       "aripiprazole", "ziprasidone", "clozapine", "amisulpride",
                       "lurasidone", "asenapine"),
    "fluoroquinolones": ("floxacin",),   # cipro-, levo-, moxi-, oflo-, norfloxacin
    "benzodiazepines": ("azepam", "azolam", "chlordiazepoxide", "clobazam"),
    "gabapentinoids": ("gabapentin", "pregabalin"),
    "beta_blockers": ("olol", "carvedilol", "labetalol"),
    "nitrates": ("isosorbide", "glyceryl trinitrate", "nitroglycerin", "nicorandil"),
    "iodinated_contrast": ("iohexol", "iopamidol", "iodixanol", "ioversol", "iopromide",
                           "iomeprol", "omnipaque", "visipaque"),
}

_INGREDIENT_WORD_RE = re.compile(r"[a-z]+(?:['-][a-z]+)*")
# Single words shorter than this are dose units and form words ("mg", "tab"),
# never an ingredient a row could name.
_MIN_INGREDIENT_WORD = 4
# Salt and form words that are never the drug on their own. "potassium" is the
# dangerous one: "Losartan potassium" is not a potassium supplement.
_SINGLE_WORD_STOPLIST = frozenset({
    "sodium", "potassium", "calcium", "magnesium", "hydrochloride", "chloride",
    "sulphate", "sulfate", "acetate", "phosphate", "citrate", "maleate",
    "succinate", "tartrate", "besylate", "mesylate", "bromide", "tablet",
    "tablets", "capsule", "capsules", "injection", "infusion", "syrup",
    "suspension", "solution", "oral", "cream", "ointment", "drops", "vial",
    "ampoule", "inhaler", "extended", "release", "units",
})


def ingredient_key(name: str) -> str:
    """The normalised form an ingredient row is stored and matched under."""
    return " ".join(_INGREDIENT_WORD_RE.findall((name or "").lower()))


def _ingredient_phrases(drug_name: str) -> set[str]:
    """Every 1 to 3 word run of the name, so 'Isosorbide Mononitrate 20mg' can
    meet a row stored as 'isosorbide mononitrate'."""
    words = _INGREDIENT_WORD_RE.findall((drug_name or "").lower())
    phrases: set[str] = set()
    for size in (1, 2, 3):
        for i in range(len(words) - size + 1):
            phrase = " ".join(words[i:i + size])
            if size == 1 and (len(phrase) < _MIN_INGREDIENT_WORD
                              or phrase in _SINGLE_WORD_STOPLIST):
                continue
            phrases.add(phrase)
    # A class or group term is reached only through its own member list: the
    # word "potassium" in a name must not stand in for the potassium class.
    return phrases - _VOCABULARY_KEYS


_VOCABULARY_KEYS = frozenset(_DRUG_CLASS_PATTERNS) | frozenset(_INTERACTION_GROUP_PATTERNS)


def _vocabulary_terms(drug_name: str) -> set[str]:
    """Classes and groups this module itself recognises the drug as."""
    if not drug_name:
        return set()
    n = drug_name.lower()
    terms = set(classify_drug(drug_name))
    for term, patterns in _INTERACTION_GROUP_PATTERNS.items():
        if any(p in n for p in patterns):
            terms.add(term)
    return terms


def interaction_terms(drug_name: str) -> set[str]:
    """Every term a drug_interactions row could name this drug by."""
    if not drug_name:
        return set()
    return _vocabulary_terms(drug_name) | _ingredient_phrases(drug_name)


def interaction_recognised(drug_name: str, terms: Optional[set[str]] = None) -> bool:
    """True when the interaction knowledge base knows this drug at all.

    Known only when some interaction row (curated or loaded) names one of the
    drug's terms. Being classified is not enough: classify_drug emits allergy
    classes (cephalosporins, carbapenems, aminoglycosides, sulfonamides) that no
    row names, so ceftriaxone or gentamicin used to read "screened" with no
    pair able to fire. A drug the rows do not know was NOT screened, which must
    never read as "screened and clean".
    """
    if terms is None:
        terms = interaction_terms(drug_name)
    return bool(interaction_db.known_terms(terms))


# Shortest string permitted to participate in a *substring* name match. Token
# equality is always allowed; free substring matching is not, below this length.
#
# Why: a 2–3 character allergy record substring-matches a large, arbitrary slice
# of the formulary in BOTH directions — "pen" is inside meroPENem, PENtazocine
# and hapPENs-to-contain; "ten" is inside aTENolol and ondanseTEN-nothing. An
# interlock that fires on unrelated drugs is not a stricter interlock, it is the
# documented alert-fatigue hazard: staff learn to click through, and the real
# anaphylaxis alert dies with the noise. Sub-4-character allergen records are
# reported separately as UNINTERPRETED (see allergy_interlock) rather than
# silently matched or silently dropped.
_MIN_SUBSTRING_MATCH = 4

_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9'\-]*")


def _tokens(s: str) -> set[str]:
    return set(_TOKEN_RE.findall(s or ""))


def names_overlap(substance: str, name: str) -> bool:
    """True when an allergen string and a drug-name string denote the same thing.

    Exact equality → token containment → substring, the last only when both
    sides clear ``_MIN_SUBSTRING_MATCH``. Substring matching is what catches
    "sulfa" → "sulfamethoxazole" and "amoxicillin" → "amoxicillin 500mg", so it
    stays; it is only the degenerate short-string case that is refused.
    """
    if not substance or not name:
        return False
    if substance == name:
        return True
    if substance in _tokens(name) or name in _tokens(substance):
        return True
    if len(substance) >= _MIN_SUBSTRING_MATCH and len(name) >= _MIN_SUBSTRING_MATCH:
        return substance in name or name in substance
    return False


def _active_allergies(db, patient_id: str) -> list[dict]:
    rows = db.execute(
        "SELECT id, substance, substance_class, reaction, severity FROM patient_allergies "
        "WHERE patient_id = ? AND status = 'active'",
        (patient_id,),
    ).fetchall()
    return [dict(r) for r in rows]


# ── Allergy severity policy (shared by order time and the bedside) ───────────
# Moved here from allergy_interlock 2026-10-03, unchanged, so BOTH screens read
# one policy. Before, the order-time gate graded a cross-reactivity from the
# table above ('moderate', whatever the reaction) and a direct match from the
# severity column alone, while the bedside read the reaction text. A penicillin
# anaphylaxis therefore charted ceftriaxone on every order lane and only eMAR
# stopped it; a parity sweep of 480 allergy/reaction/severity/drug cases found
# 163 that the bedside blocked and the order screen let through.
#
# The recorded `severity` column is frequently left at its 'unknown' default,
# while the free-text `reaction` carries the real clinical weight. These three
# expressions read that text.
#
# ESCALATE: an airway/circulatory or severe-cutaneous reaction is treated as
# severe REGARDLESS of the severity column. "anaphylaxis, severity: mild" is a
# data-entry artefact, not a clinical judgement: believe the reaction.
_ANAPHYLAXIS_RE = re.compile(
    r"anaphyla|angio-?\s?o?edema|laryng|bronchospasm|airway|throat swell|"
    r"tongue swell|respiratory arrest|cardiac arrest|cardiovascular collapse|"
    r"\bshock\b|stevens\s*-?\s*johnson|\bsjs\b|toxic epidermal|\bten\b|\bdress\b|"
    r"epipen|adrenaline|epinephrine|resuscitat|\bitu\b|\bicu admission\b",
    re.IGNORECASE,
)

# DE-ESCALATE: nausea with codeine is an intolerance, not an allergy. Blocking
# every opioid dose on it is exactly how an interlock gets trained out of
# existence. Applied ONLY when no severity was recorded (an explicitly
# clinician-graded 'severe' is never talked down by this), and ONLY when nothing
# in the same text describes a genuine hypersensitivity (see _ALLERGIC_RE).
_INTOLERANCE_RE = re.compile(
    r"nausea|vomit|\bgi upset|stomach upset|indigestion|dyspepsia|diarrh|"
    r"constipat|head\s?ache|drowsi|dizz|intoleran|metallic taste|loose stool|"
    r"bad taste|bloat",
    re.IGNORECASE,
)

# VETO on de-escalation. Reactions are free text and routinely list several
# things at once: "rash and nausea" is an ALLERGY that also caused nausea, not
# an intolerance. Without this veto the intolerance rule would talk a genuine
# hypersensitivity down to a warning because of one co-occurring word, the
# worst kind of bug this module could have, because it would fail silently and
# in the unsafe direction.
_ALLERGIC_RE = re.compile(
    r"rash|hives|urticaria|itch|pruritu|swell|wheez|blister|erythema|"
    r"angio|anaphyl|desquamat|eosinophil|serum sickness|flush|weal|welt",
    re.IGNORECASE,
)


def effective_severity(alert: dict) -> tuple[str, str]:
    """The severity an allergy alert should be ACTED on, and why.

    Returns ``(severity, basis)`` where severity ∈ mild|moderate|severe. Reads
    ``recorded_severity`` and ``reaction`` from a match_allergies alert.

    Precedence, strongest first:
      1. reaction text describes an airway/circulatory/severe-cutaneous event
         → severe, whatever the severity column says;
      2. an explicitly recorded mild/moderate/severe → that value;
      3. severity 'unknown' + a reaction that reads as a pure intolerance, with
         NOTHING hypersensitivity-shaped in the same text → mild;
      4. severity 'unknown' with nothing else to go on → severe (fail safe).
    """
    recorded = (alert.get("recorded_severity") or "unknown").strip().lower()
    reaction = alert.get("reaction") or ""
    if reaction and _ANAPHYLAXIS_RE.search(reaction):
        return "severe", "reaction_is_anaphylaxis_grade"
    if recorded in ("mild", "moderate", "severe"):
        return recorded, "recorded_severity"
    if reaction and _INTOLERANCE_RE.search(reaction) and not _ALLERGIC_RE.search(reaction):
        return "mild", "reaction_reads_as_intolerance"
    return "severe", "severity_unknown_fail_safe"


def _worse_severity(a: str, b: str) -> str:
    return a if _SEVERITY_RANK.get(a, 0) >= _SEVERITY_RANK.get(b, 0) else b


def match_allergies(
    allergies: list[dict],
    drug_name: str,
    extra_names: tuple[str, ...] | list[str] = (),
) -> list[dict]:
    """PURE allergy matcher — no DB, no I/O. The single matching implementation.

    ``allergies`` is a list of patient_allergies rows (dicts). ``extra_names``
    carries additional names for the SAME product — typically the generic /
    ingredient names resolved from the hospital's own ``helix_medication_master``
    formulary, so a brand-name order ("Monocef") is matched on its ingredient
    ("Ceftriaxone") too. Callers that have no formulary context pass nothing and
    get exactly the previous behaviour.

    Matches on (a) direct substance-name overlap, (b) drug-class membership and
    (c) documented cross-reactivity (e.g. penicillin-allergic → cephalosporin).

    Kept pure so the bedside interlock and the whole-ward nurse board can screen
    N patients × M medications without N×M database round trips, and so both
    order-time and administration-time screening share ONE vocabulary.
    """
    alerts: list[dict] = []
    names = [_norm(n) for n in (drug_name, *extra_names)]
    names = [n for n in names if n]
    drug_classes: set[str] = set()
    for n in (drug_name, *extra_names):
        drug_classes.update(classify_drug(n))
    label = drug_name

    for a in allergies:
        sub = _norm(a["substance"])
        # classify_allergen, not classify_drug: the allergen may be charted as a
        # CLASS ("NSAIDs") rather than a substance ("Ibuprofen"). See its docstring.
        sub_classes = set(classify_allergen(a["substance"]))
        if a.get("substance_class"):
            sub_classes.add(a["substance_class"])
        sev = a.get("severity") or "unknown"
        # `severity` is the ORDER-TIME grade and keeps its historical meaning
        # (unknown is treated as severe). `recorded_severity` carries the raw
        # chart value so the administration interlock can apply its own,
        # reaction-aware policy without re-reading the row.
        common = {
            "type": "allergy",
            "allergy_id": a.get("id"),
            "substance": a["substance"],
            "reaction": a.get("reaction"),
            "recorded_severity": sev,
        }
        # The order-time grade never sits BELOW the bedside's (see
        # effective_severity): whenever the bedside would block the dose, the
        # order is already blocked. It may sit above it (unknown + an
        # intolerance is still severe here), as it always did.
        floor, _basis = effective_severity(common)

        # (a) direct name match — against the charted name AND any resolved
        #     ingredient/generic name for the same product.
        matched_name = next((n for n in names if names_overlap(sub, n)), None)
        if matched_name is not None:
            alerts.append({
                **common,
                "severity": _worse_severity("severe" if sev in ("severe", "unknown") else sev, floor),
                "message": f"Patient is allergic to {a['substance']} (documented reaction: {a.get('reaction') or 'unknown'}).",
                "match": "direct",
                "matched_on": matched_name,
            })
            continue

        # (b) same-class match
        shared = drug_classes & sub_classes
        if shared:
            alerts.append({
                **common,
                "severity": _worse_severity("severe" if sev in ("severe", "unknown") else sev, floor),
                "message": f"{label} shares a drug class ({', '.join(sorted(shared))}) with the "
                           f"documented allergy to {a['substance']}.",
                "match": "class",
                "matched_on": ", ".join(sorted(shared)),
            })
            continue

        # (c) cross-reactivity
        for sc in sorted(sub_classes):
            for (target_cls, xsev) in _CROSS_REACTIVITY.get(sc, []):
                if target_cls in drug_classes:
                    reaction = (a.get("reaction") or "").strip()
                    alerts.append({
                        **common,
                        "severity": _worse_severity(xsev, floor),
                        "message": f"Cross-reactivity: {label} ({target_cls}) with documented "
                                   f"{sc} allergy ({a['substance']}"
                                   + (f"; reaction: {reaction}" if reaction else "") + ").",
                        "match": "cross_reactivity",
                        "matched_on": f"{sc}->{target_cls}",
                    })
    return alerts


def check_allergies(patient_id: str, drug_name: str) -> list[dict]:
    """Return allergy alerts for prescribing `drug_name` to `patient_id`.

    Thin DB-reading wrapper over :func:`match_allergies` (the one matcher).
    """
    with db_session() as db:
        allergies = _active_allergies(db, patient_id)
    return match_allergies(allergies, drug_name)


def _screen_interactions(patient_id: str, drug_name: str,
                         exclude_med_id: int | None = None,
                         exclude_order_id: str | None = None) -> tuple[list[dict], list[str]]:
    """Interaction alerts for ``drug_name`` against the patient's active meds,
    plus the active meds the knowledge base does not know (not screened).

    One alert per co-medication: when several rows match the same pair (warfarin
    with aspirin matches the class row, the warfarin/NSAID row and the
    warfarin/aspirin row) the worst severity leads and every distinct mechanism
    is kept in the message, so nothing is lost and the pair is not listed three
    times.

    WHAT "ACTIVE MEDS" MEANS (medorders-9, ruling MO-R3 (a), held for Samuel).
    With ORB_MED_ORDER_LIFECYCLE on, the comparison set is read from the
    medication-order store (services/medorders/screening.comparison_rows):
    live orders, PRN and held included, drafts and ended orders excluded, plus
    live ward rows with no order behind them, less the screened order's own
    rows (``exclude_med_id`` and ``exclude_order_id``). An alert against a held
    or as-needed drug says so. With the lifecycle off: the patient's 'active'
    ward rows, exactly as before.
    """
    interaction_db.ensure_seeded()
    new_terms = interaction_terms(drug_name)
    from services.medorders import screening
    labels: list[Optional[str]]
    if screening.store_screen_on():
        with db_session() as db:
            store_rows = screening.comparison_rows(db, patient_id, exclude_med_id=exclude_med_id,
                                                   exclude_order_id=exclude_order_id)
        rows = [{"drug_name": r["drug_name"]} for r in store_rows]
        labels = [screening.row_label(r) for r in store_rows]
    else:
        sql = "SELECT drug_name FROM medications WHERE patient_id = ? AND status = 'active'"
        params: list = [patient_id]
        if exclude_med_id is not None:
            sql += " AND id != ?"
            params.append(exclude_med_id)
        with db_session() as db:
            if exclude_order_id:
                # The order's own ward row, when it has one.
                own = db.execute("SELECT legacy_medication_id FROM med_orders WHERE id = ?",
                                 (exclude_order_id,)).fetchone()
                if own is not None and own["legacy_medication_id"] is not None:
                    sql += " AND id != ?"
                    params.append(own["legacy_medication_id"])
            rows = db.execute(sql, params).fetchall()
        labels = [None] * len(rows)

    alerts: list[dict] = []
    unscreened: list[str] = []
    for r, label in zip(rows, labels):
        existing = r["drug_name"]
        existing_terms = interaction_terms(existing)
        if not interaction_recognised(existing, existing_terms):
            if existing not in unscreened:
                unscreened.append(existing)
            continue
        hits = interaction_db.lookup_interactions(new_terms, existing_terms) if new_terms else []
        if not hits:
            continue
        hits.sort(key=lambda h: (-_SEVERITY_RANK.get(h["severity"], 0),
                                 h.get("match_type") != "ingredient"))
        mechanisms: list[str] = []
        for h in hits:
            m = (h["mechanism"] or "").strip()
            if m and m not in mechanisms:
                mechanisms.append(m)
        alert = {
            "type": "interaction",
            "severity": hits[0]["severity"],
            "with": existing,
            "message": (f"{drug_name} + {existing}"
                        + (f" ({label})" if label else "")
                        + f": {' '.join(mechanisms)}"),
            "classes": sorted({t for h in hits for t in (h["term_a"], h["term_b"])}),
        }
        if label:
            alert["with_status"] = label
        alerts.append(alert)
    return alerts, unscreened


def check_interactions(patient_id: str, drug_name: str,
                       exclude_med_id: int | None = None) -> list[dict]:
    """Return interaction alerts between `drug_name` and the patient's active meds.

    Queries the data-backed `drug_interactions` table (via
    `interaction_db.lookup_interactions`) rather than a hardcoded list, matching
    on :func:`interaction_terms` (classes, groups and ingredient names) so both
    class rows and ingredient rows can fire.

    ``exclude_med_id`` leaves one medications row out of the comparison set: the
    row being screened when it is already on the chart (an unverified AI med is
    charted active), which would otherwise interact with itself.
    """
    return _screen_interactions(patient_id, drug_name, exclude_med_id)[0]


def check_medication_safety(patient_id: str, drug_name: str,
                            exclude_med_id: int | None = None,
                            exclude_order_id: str | None = None) -> dict:
    """Full order-time safety screen. Returns allergy + interaction alerts and the
    single worst severity, plus a `blocking` flag (severe alerts block auto-orders).

    `interaction_screened` is False when no interaction row names any of the
    drug's terms (:func:`interaction_recognised`): the interaction check then
    had nothing to compare, which is NOT the same as
    "screened and clean". Callers must surface that distinction; an unscreened
    drug returning zero alerts previously looked identical to a clean screen.
    (Allergy matching still runs regardless: direct name matches don't need a
    class.)

    ``exclude_med_id`` is passed through to :func:`check_interactions` when the
    drug being screened is itself a charted row (pharmacist verification).
    ``exclude_order_id`` does the same for a medication order (medorders-9):
    the order and its ward row are left out of the comparison.
    """
    allergy_alerts = check_allergies(patient_id, drug_name)
    if exclude_order_id is None:
        interaction_alerts, unscreened_with = _screen_interactions(patient_id, drug_name,
                                                                   exclude_med_id)
    else:
        interaction_alerts, unscreened_with = _screen_interactions(
            patient_id, drug_name, exclude_med_id, exclude_order_id)
    all_alerts = allergy_alerts + interaction_alerts
    max_sev = "none"
    for a in all_alerts:
        if _SEVERITY_RANK.get(a["severity"], 0) > _SEVERITY_RANK.get(max_sev, 0):
            max_sev = a["severity"]
    interaction_screened = interaction_recognised(drug_name)
    if not interaction_screened:
        coverage_note = (
            f"'{drug_name}' is outside the interaction knowledge base. "
            "Drug–drug interactions were NOT screened. Verify manually."
        )
    elif unscreened_with:
        # The new drug is known but some of the chart is not: those pairs were
        # not checked, which must not read as clean (warfarin with amiodarone
        # used to come back screened with no alert).
        coverage_note = (
            f"Not screened against {', '.join(unscreened_with)}: outside the "
            "interaction knowledge base. Check those pairs manually."
        )
    else:
        coverage_note = None
    return {
        "drug": drug_name,
        "allergy_alerts": allergy_alerts,
        "interaction_alerts": interaction_alerts,
        "max_severity": max_sev,
        "blocking": max_sev == "severe",
        "safe": not all_alerts,
        "interaction_screened": interaction_screened,
        "unscreened_with": unscreened_with,
        "coverage_note": coverage_note,
    }


def formulary_interaction_coverage() -> dict:
    """Which formulary items the interaction knowledge base can screen.

    One row per helix_medication_master item, recognised by its name or its
    generic name. An item that is not covered is screened for allergies only;
    this is the list a pharmacist reviews before trusting a clean screen.
    """
    interaction_db.ensure_seeded()
    with db_session() as db:
        try:
            rows = db.execute(
                "SELECT id, name, generic_name FROM helix_medication_master ORDER BY name"
            ).fetchall()
        except Exception:   # Helix tables not initialised on this database
            rows = []
    items = []
    for r in rows:
        names = [n for n in (r["name"], r["generic_name"]) if n]
        candidates: set[str] = set()
        for n in names:
            candidates |= interaction_terms(n)
        # The terms some interaction row actually names: a class the rows never
        # use (cephalosporins, say) covers nothing.
        terms = interaction_db.known_terms(candidates)
        covered = bool(terms)
        items.append({
            "id": r["id"],
            "name": r["name"],
            "generic_name": r["generic_name"],
            "covered": covered,
            "terms": sorted(terms),
        })
    covered_count = sum(1 for i in items if i["covered"])
    return {"total": len(items), "covered": covered_count,
            "not_covered": len(items) - covered_count, "items": items}


# ═════════════════════════════════════════════════════════════════════════════
# DOSE SAFETY — the pure rule layer for the bedside dose guard
# ═════════════════════════════════════════════════════════════════════════════
"""
Nothing sat between a nurse's keystroke and the legal record of what went into
the patient. A decimal slip on digoxin, insulin or an opioid charted clean, and
a repeated dose on a rounds handoff charted clean.

WHAT THIS DELIBERATELY IS AND IS NOT
------------------------------------
It is NOT a dose-range checking engine. Orb has no maximum-dose data, before
this, `helix_medication_master` held (id, name, generic_name, form, strength)
and 14 rows, and there was no maximum dose anywhere in the schema. A real one
needs a licensed, maintained, weight- and renal-function-aware source, and this
is not that.

It is a SHORT, HAND-CURATED, CITED list of drugs where a slip is lethal and the
ceiling is uncontroversial (the ISMP high-alert classes) with a graded
response designed around one fact:

    A HARD BLOCK ON EVERYTHING UNUSUAL WILL BE WORKED AROUND, AND WHEN IT IS,
    THE 10x BLOCK GOES WITH IT.

So the response is graded, and the grades mean different things:

  * BLOCK: reserved for the order-of-magnitude error. Digoxin 1.25 mg for
    0.125 mg. Insulin 100 units for 10. These are wrong by a factor that no
    legitimate indication reaches, so a stop is defensible and will be believed.
  * WARN: a dose on the high side of normal. Oncology, palliative care, renal
    dosing and opioid tolerance all produce doses that look alarming and are
    correct. Blocking those stops legitimate care mid-round, which is how a
    guard earns the reputation that gets it disabled.
  * ALLOW (silently): everything else, INCLUDING every drug not on the list.
    Only a fraction of any formulary will ever be curated; blocking what is not
    in the table stops the ward dead. Unknown means proceed, quietly.

Three things it refuses to guess at, because a wrong dose alert is worse than
no dose alert:

  1. WEIGHT-BASED DOSING. "0.5 mg/kg" is not a dose without a weight, and Orb's
     weights are neither mandatory nor freshly measured. Anything per-kg, per-m²
     or per-hour is declined outright.
  2. UNIT AMBIGUITY. "10 units" of insulin, "10 mL" of a solution and "10 mg"
     are three different statements. A rule written in mg does not get to judge
     a dose charted in mL, and a dose whose unit cannot be resolved is not
     judged at all.
  3. ANYTHING IT CANNOT PARSE CONFIDENTLY. A range ("1-2 tablets"), a ratio
     product ("1:1000"), two quantities it cannot reconcile ("2 tablets (1g)"),
     a bare count ("5 puffs"): an unparseable dose is NOT evidence of an error,
     and firing on one teaches staff the guard is noise.

ROUTE IS PART OF THE RULE where route is what makes the dose lethal,
concentrated potassium chloride by IV push, vincristine intrathecally, and
where the therapeutic ceiling genuinely differs by route (adrenaline is 0.5 mg
IM, 1 mg IV and 5 mg nebulised; the same number means different things).

This module is PURE. The DB half (prior administrations, the override ledger,
the audit rows) is in services/emar_service.py, mirroring how allergy matching
lives here and the bedside allergy interlock lives in allergy_interlock.py.
"""

# ── Decisions ────────────────────────────────────────────────────────────────
# Same vocabulary as allergy_interlock's ALLOW/WARN/BLOCK, on purpose: a nurse
# reads one banner component, and a reviewer reads one set of audit values.
DOSE_ALLOW = "allow"
DOSE_WARN = "warn"
DOSE_BLOCK = "block"

_DOSE_RANK = {DOSE_ALLOW: 0, DOSE_WARN: 1, DOSE_BLOCK: 2}


def worse_dose_decision(a: str, b: str) -> str:
    """The more serious of two dose decisions — one check must never talk
    another one down."""
    return a if _DOSE_RANK.get(a, 0) >= _DOSE_RANK.get(b, 0) else b


class DoseGuardBlocked(Exception):
    """A dose was stopped by the dose guard.

    Carries ``.check`` — the full payload — so the route can render the alert
    and the caller can re-submit with a typed reason. Deliberately NOT a
    ValueError: the caller must be able to tell "you may not do this" from
    "you may do this, but say why". Same contract as AllergyInterlockBlocked.
    """

    def __init__(self, check: dict, message: str):
        self.check = check
        super().__init__(message)


# ── Dose parsing ─────────────────────────────────────────────────────────────
# Canonical unit per family. A rule declares the family it is written in and
# never judges a dose from another family.
_UNIT_ALIASES: dict[str, tuple[str, float]] = {
    # mass → mg
    "mg": ("mg", 1.0), "milligram": ("mg", 1.0), "milligrams": ("mg", 1.0),
    "mgs": ("mg", 1.0),
    "g": ("mg", 1000.0), "gm": ("mg", 1000.0), "gms": ("mg", 1000.0),
    "gram": ("mg", 1000.0), "grams": ("mg", 1000.0), "gr": ("mg", 1000.0),
    "mcg": ("mg", 0.001), "ug": ("mg", 0.001), "µg": ("mg", 0.001),
    "μg": ("mg", 0.001), "microgram": ("mg", 0.001), "micrograms": ("mg", 0.001),
    "mcgs": ("mg", 0.001),
    "ng": ("mg", 1e-6), "nanogram": ("mg", 1e-6), "nanograms": ("mg", 1e-6),
    # biological activity → units
    "u": ("units", 1.0), "unit": ("units", 1.0), "units": ("units", 1.0),
    "iu": ("units", 1.0), "ius": ("units", 1.0), "ui": ("units", 1.0),
    # amount of substance → mmol.
    # mEq is treated as mmol: every drug this matters for here (potassium
    # chloride) is monovalent, where 1 mEq == 1 mmol exactly. A polyvalent salt
    # would need its own conversion, which is why no polyvalent rule exists.
    "mmol": ("mmol", 1.0), "mmols": ("mmol", 1.0),
    "meq": ("mmol", 1.0), "meqs": ("mmol", 1.0), "mequiv": ("mmol", 1.0),
    # volume → ml. No rule is written in mL (a volume is not a dose without a
    # concentration), but the family is recognised so that a dose charted in mL
    # is understood to be OUTSIDE a mg rule rather than silently compared to it.
    "ml": ("ml", 1.0), "mls": ("ml", 1.0), "cc": ("ml", 1.0),
    "millilitre": ("ml", 1.0), "millilitres": ("ml", 1.0),
    "milliliter": ("ml", 1.0), "milliliters": ("ml", 1.0),
    "l": ("ml", 1000.0), "litre": ("ml", 1000.0), "litres": ("ml", 1000.0),
}

# Dose FORMS. A count of these is not a measured dose, and their presence
# alongside a measured quantity ("2 tablets (1g)") means the string is saying
# two things at once — which we decline to reconcile.
_COUNT_UNITS = {
    "tab", "tabs", "tablet", "tablets", "cap", "caps", "capsule", "capsules",
    "puff", "puffs", "drop", "drops", "spray", "sprays", "sachet", "sachets",
    "amp", "amps", "ampoule", "ampoules", "vial", "vials", "patch", "patches",
    "supp", "supps", "suppository", "suppositories", "neb", "nebs", "scoop",
    "scoops", "piece", "pieces", "unitdose", "dose", "doses",
}

_QUANTITY_RE = re.compile(r"(\d+(?:\.\d+)?)\s*([a-zµμ]+)", re.IGNORECASE)

# Shapes that mean "this is not a single unambiguous quantity". Each one is a
# real charting convention, and each one would produce a WRONG number if parsed.
_DOSE_REFUSALS: tuple[tuple[re.Pattern, str], ...] = (
    # A slash that is part of a RATE or a COMBINATION product: a digit on either
    # side of it ("500mg/125mg", "1/2"), or a per-unit after it ("mg/kg",
    # "units/hr"). NOT a bare slash: "10 units s/c" and "40mg p/o" are how a
    # route is abbreviated on a real chart, and refusing those would let a
    # 10x insulin dose through simply because the nurse wrote the route in the
    # dose box. Found by probing the parser, not by a test.
    (re.compile(r"\d\s*/|/\s*\d", re.I), "rate_or_combination"),
    (re.compile(r"/\s*(?:kg|kgs|hr|hrs|hour|hours|h|min|mins|minute|minutes|"
                r"day|days|d|dose|m2|week)\b", re.I), "rate_or_per_unit"),
    (re.compile(r"\bper\b", re.I), "rate_or_per_unit"),
    (re.compile(r"\d\s*[:∶]\s*\d"), "ratio_product"),  # adrenaline 1:1000
    (re.compile(r"%"), "percentage_strength"),
    (re.compile(r"\d\s*(?:-|–|—|\bto\b)\s*\d", re.I), "dose_range"),
    (re.compile(r"\bm2\b|\bm\^?2\b|\bsq\s*m\b", re.I), "body_surface_area"),
)


def _decimalise(text: str) -> str:
    """Normalise digit grouping before any number is read.

    "10,000 units" is how a heparin dose is written on an Indian or UK chart. A
    naive float parse reads that as 10 — a 1000-fold UNDER-read, which would
    silently disarm the guard on exactly the drug it matters most for.
    """
    text = re.sub(r"(?<=\d),(?=\d{3}(?!\d))", "", text)   # thousands separator
    text = re.sub(r"(?<=\d),(?=\d)", ".", text)            # decimal comma
    return text


def parse_dose(text: Optional[str]) -> Optional[dict]:
    """One unambiguous quantity, converted to its family's canonical unit.

    Returns ``{"value", "unit", "raw_value", "raw_unit", "text"}`` or None.

    None is the honest answer far more often than it looks, and every None below
    is a case where guessing produces a number that is wrong rather than absent.
    An unparseable dose is not evidence of an error, so a None must never fire
    an alert — see :func:`evaluate_dose`.
    """
    if not text or not str(text).strip():
        return None
    raw = _decimalise(str(text).strip())

    for pattern, _why in _DOSE_REFUSALS:
        if pattern.search(raw):
            return None

    measured: list[tuple[float, str, str, float]] = []
    counts = 0
    for m in _QUANTITY_RE.finditer(raw):
        number, unit = m.group(1), m.group(2).lower()
        if unit in _COUNT_UNITS:
            counts += 1
            continue
        alias = _UNIT_ALIASES.get(unit)
        if alias is None:
            continue          # an unrecognised token is not a unit claim
        canonical, factor = alias
        try:
            value = float(number)
        except ValueError:      # pragma: no cover — regex guarantees a number
            return None
        measured.append((value, unit, canonical, factor))

    # Two measured quantities in one string ("500mg/125mg" is already refused
    # above; "1000 mcg (1 mg)" is not) — we cannot tell which one is the dose.
    if len(measured) != 1:
        return None
    # A dose form count next to a measured quantity ("2 tablets (1g)") is the
    # same problem: the string is stating a count AND a strength, and which one
    # the nurse means is not ours to decide.
    if counts:
        return None

    value, unit, canonical, factor = measured[0]
    return {
        "value": value * factor,
        "unit": canonical,
        "raw_value": value,
        "raw_unit": unit,
        "text": str(text).strip(),
    }


# ── Route classification ─────────────────────────────────────────────────────
# Returns TAGS, not a single route: "IV push" is both intravenous and a push,
# and the push is what kills with potassium.
_ROUTE_TOKENS: dict[str, set[str]] = {
    "oral": {"oral", "orally", "po", "p.o", "p.o.", "ng", "ngt", "peg", "rt",
             "sl", "sublingual", "buccal", "mouth", "swallow"},
    "iv": {"iv", "i.v", "i.v.", "ivi", "intravenous", "intravenously"},
    "im": {"im", "i.m", "i.m.", "intramuscular", "intramuscularly"},
    "sc": {"sc", "s.c", "s.c.", "sq", "subcut", "subcutaneous", "subcutaneously"},
    "nebulised": {"neb", "nebs", "nebulised", "nebulized", "nebuliser",
                  "nebulizer", "inh", "inhaled", "inhalation"},
    "transdermal": {"transdermal", "patch", "td"},
    "topical": {"topical", "top", "skin"},
    "intrathecal": {"intrathecal", "intrathecally", "it", "spinal"},
    "rectal": {"pr", "rectal", "rectally"},
    "push": {"push", "bolus", "stat", "direct"},
    "infusion": {"infusion", "infusions", "drip", "continuous", "driver"},
}
# Long, unambiguous phrases are matched as substrings so "IV-push", "IVpush" and
# "intravenous push" all land. Short tokens (iv, im, sc, it) are NEVER matched
# as substrings — "it" inside "vomiting" is exactly the kind of match that turns
# a guard into noise.
_ROUTE_SUBSTRINGS: dict[str, tuple[str, ...]] = {
    "oral": ("oral", "nasogastric", "sublingual", "buccal", "by mouth"),
    "iv": ("intravenous",),
    "im": ("intramuscular",),
    "sc": ("subcutaneous", "subcut"),
    "nebulised": ("nebulis", "nebuliz", "inhalation"),
    "transdermal": ("transdermal", "patch"),
    "intrathecal": ("intrathecal",),
    "push": ("push", "bolus"),
    "infusion": ("infusion", "syringe driver"),
}

_ROUTE_TOKEN_RE = re.compile(r"[a-z]+")
# "S/C", "I.V.", "P/O" — a punctuated single-letter abbreviation is how routes
# are actually written on an Indian/UK chart. Collapsed to "sc", "iv", "po"
# BEFORE tokenising, or a route-specific rule silently never applies and the
# guard falls back to its weakest ceiling. Found by probing, not by a test.
_ROUTE_ABBREV_RE = re.compile(r"(?<=\b[a-z])[./](?=[a-z]\b)")


def classify_route(route: Optional[str]) -> frozenset[str]:
    """Route text → a set of tags. Empty when the route says nothing we know.

    An empty set is meaningful: a rule that depends on route does not apply to a
    route we could not read (see :func:`find_dose_rule`).
    """
    if not route:
        return frozenset()
    n = _norm(route)
    compact = _ROUTE_ABBREV_RE.sub("", n).replace(".", " ")
    tokens = set(_ROUTE_TOKEN_RE.findall(compact))
    tags: set[str] = set()
    for tag, exact in _ROUTE_TOKENS.items():
        if tokens & exact:
            tags.add(tag)
    for tag, subs in _ROUTE_SUBSTRINGS.items():
        if any(s in n for s in subs):
            tags.add(tag)
    if tags & {"iv", "im", "sc"}:
        tags.add("parenteral")
    return frozenset(tags)


# ── The curated table ────────────────────────────────────────────────────────
# CURATION RULES, so this table can be extended without losing its nerve:
#
#   * Start from the ISMP List of High-Alert Medications in Acute Care Settings
#     — the standard reference for "a slip here kills" — and add only entries
#     whose ceiling is uncontroversial.
#   * EVERY entry cites a source and states what a normal dose is. A threshold
#     with no provenance is deleted by the first person it annoys, and rightly
#     so. tests/test_dose_guard.py enforces both fields.
#   * `warn_above` is the top of the ordinary adult range: above this is
#     unusual, not wrong. `block_at` is the number no legitimate indication
#     reaches — set it at the order-of-magnitude error, not at the top of
#     normal. `block_at = None` means "this drug's ceiling is genuinely
#     contested; warn only" and that is an honest, deliberate answer (see the
#     oral opioids, where palliative doses overlap the 10x error).
#   * Thresholds are in the family's CANONICAL unit (mg / units / mmol).
#     `display` renders the number back in the unit a clinician thinks in.
#
# Adult inpatient dosing throughout. Paediatric dosing is per-kg, which this
# guard declines outright (see the module note above), so no rule here is
# doing a child any harm by existing.
DOSE_RULES: list[dict] = [
    # ── INSULIN ─────────────────────────────────────────────────────────────
    {
        "id": "insulin",
        "match": ("insulin", "humalog", "lantus", "novorapid", "actrapid",
                  "mixtard", "huminsulin", "glargine", "aspart", "lispro",
                  "degludec", "detemir"),
        "routes": None,
        "unit": "units",
        "warn_above": 50.0,
        "block_at": 100.0,
        "basis": "Adult subcutaneous doses are usually 4–40 units; 40–60 units "
                 "per injection is ordinary in insulin-resistant type 2 diabetes.",
        "source": "ISMP List of High-Alert Medications in Acute Care Settings "
                  "(insulin, all formulations): ISMP names 'U' misread as a "
                  "zero as the classic error, i.e. 10U charted as 100. Insulin "
                  "has no fixed maximum (it is titrated to response), which is "
                  "why the ordinary range is a WARN band and only the 10x point "
                  "is a block.",
    },
    # ── DIGOXIN ─────────────────────────────────────────────────────────────
    {
        "id": "digoxin",
        "match": ("digoxin", "lanoxin", "digitoxin"),
        "routes": None,
        "unit": "mg",
        "display": ("mcg", 1000.0),
        "warn_above": 0.25,
        "block_at": 1.0,
        "basis": "Maintenance 62.5–250 mcg once daily. Rapid digitalisation is "
                 "0.75–1.5 mg TOTAL over 24h in DIVIDED doses, of which the "
                 "largest single component is ~500 mcg.",
        "source": "ISMP high-alert (digoxin); BNF 'Digoxin': oral loading "
                  "0.75–1.5 mg in divided doses, maintenance 62.5–250 mcg daily. "
                  "1.25 mg is exactly 10x the commonest 125 mcg tablet and is "
                  "the textbook decimal slip; no single dose reaches 1 mg.",
    },
    # ── OPIOIDS ─────────────────────────────────────────────────────────────
    # Route-specific rules come FIRST so that a parenteral dose is judged
    # against a parenteral ceiling. The route-agnostic fallback that follows is
    # deliberately warn-only: if we cannot read the route we cannot tell a
    # 100 mg oral MR tablet (real, palliative) from 100 mg IV (lethal).
    {
        "id": "morphine_parenteral",
        "match": ("morphine",),
        "routes": ("parenteral",),
        "unit": "mg",
        "warn_above": 10.0,
        "block_at": 50.0,
        "basis": "Adult IV/IM/SC morphine is 2.5–10 mg per dose, titrated. "
                 "Opioid-tolerant patients go higher, but by increments.",
        "source": "ISMP high-alert (opioids, IV); BNF 'Morphine sulfate': acute "
                  "pain 5–10 mg IV/SC every 4h. A 24-hour palliative syringe "
                  "driver may total far more, but is charted as a rate and is "
                  "therefore declined by the parser, not blocked here.",
    },
    {
        "id": "morphine_oral",
        "match": ("morphine", "mst", "oramorph", "sevredol", "zomorph"),
        "routes": ("oral",),
        "unit": "mg",
        "warn_above": 30.0,
        "block_at": None,
        "basis": "Oral immediate-release 5–20 mg every 4h; modified-release "
                 "tablets exist up to 200 mg and are genuinely prescribed in "
                 "opioid-tolerant palliative patients.",
        "source": "BNF 'Morphine sulfate' (oral); ISMP high-alert (opioids). "
                  "NO BLOCK ON PURPOSE: the 10x error (10 mg -> 100 mg oral) "
                  "lands inside the real palliative range, so a block here "
                  "cannot be made specific enough to be believed.",
    },
    {
        "id": "morphine_route_unknown",
        "match": ("morphine",),
        "routes": None,
        "unit": "mg",
        "warn_above": 30.0,
        "block_at": None,
        "basis": "Route could not be read, so the ceiling is unknown, the oral "
                 "(higher) warn threshold is used and nothing is blocked.",
        "source": "Fallback, not a clinical claim. See morphine_parenteral and "
                  "morphine_oral for the sourced thresholds.",
    },
    {
        "id": "fentanyl_transdermal",
        "match": ("fentanyl",),
        "routes": ("transdermal",),
        "skip": True,
        "basis": "A patch is dosed as a RATE (mcg/hour), not as a dose.",
        "source": "BNF 'Fentanyl' (transdermal). Judging a patch strength "
                  "against a bolus ceiling would fire on every correct patch.",
    },
    {
        "id": "fentanyl_parenteral",
        "match": ("fentanyl",),
        "routes": ("parenteral",),
        "unit": "mg",
        "display": ("mcg", 1000.0),
        "warn_above": 0.2,
        "block_at": 1.0,
        "basis": "Ward analgesia 25–100 mcg IV per dose; anaesthetic induction "
                 "reaches ~3 mcg/kg (200–300 mcg in an adult).",
        "source": "ISMP high-alert (opioids, IV); BNF 'Fentanyl': 50–100 mcg "
                  "IV. 1000 mcg (1 mg) is the 10x slip from 100 mcg and is not "
                  "a ward dose by any indication.",
    },
    {
        "id": "hydromorphone_parenteral",
        "match": ("hydromorphone", "dilaudid"),
        "routes": ("parenteral",),
        "unit": "mg",
        "warn_above": 2.0,
        "block_at": 20.0,
        "basis": "Roughly 5x the potency of morphine; parenteral doses are "
                 "0.2–1 mg.",
        "source": "ISMP high-alert (opioids); ISMP specifically names "
                  "hydromorphone/morphine confusion as a recurring fatal error.",
    },
    {
        "id": "oxycodone_parenteral",
        "match": ("oxycodone", "oxynorm"),
        "routes": ("parenteral",),
        "unit": "mg",
        "warn_above": 10.0,
        "block_at": None,
        "basis": "Parenteral oxycodone is 1–10 mg per dose, roughly half the "
                 "oral dose for the same effect.",
        "source": "ISMP high-alert (opioids); BNF 'Oxycodone hydrochloride' "
                  "(parenteral). Listed separately from the oral rule because "
                  "the oral ceiling is 3x this one, and an id that claimed to "
                  "be 'oral' while judging an IV dose would misreport itself in "
                  "the record and the audit trail.",
    },
    {
        "id": "oxycodone",
        "match": ("oxycodone", "oxycontin", "oxynorm", "targin"),
        "routes": None,
        "unit": "mg",
        "warn_above": 30.0,
        "block_at": None,
        "basis": "5–20 mg every 4–6h opioid-naive; 80 mg modified-release "
                 "tablets exist and are prescribed in tolerant patients.",
        "source": "ISMP high-alert (opioids); BNF 'Oxycodone'. NO BLOCK for the "
                  "same reason as oral morphine, the tolerant range overlaps "
                  "the 10x error. Route-agnostic so that an unread route still "
                  "gets the (higher, safer-to-not-fire) oral threshold.",
    },
    {
        "id": "tramadol",
        "match": ("tramadol", "ultram", "tramazac", "contramal"),
        "routes": None,
        "unit": "mg",
        "warn_above": 100.0,
        "block_at": 400.0,
        "basis": "50–100 mg per dose, maximum 400 mg in 24 hours.",
        "source": "BNF 'Tramadol': max 400 mg daily. Unlike the strong opioids "
                  "tramadol HAS a hard daily ceiling, so a single dose at the "
                  "whole-day maximum is a defensible block. Included because it "
                  "is the most-prescribed opioid on an Indian ward.",
    },
    # ── ANTICOAGULANTS ──────────────────────────────────────────────────────
    {
        "id": "heparin_unfractionated",
        "match": ("heparin",),
        "exclude": ("enoxaparin", "dalteparin", "tinzaparin", "fondaparinux"),
        "routes": None,
        "unit": "units",
        "warn_above": 10000.0,
        "block_at": 25000.0,
        "basis": "Loading bolus ~5000 units (or 75–80 units/kg); prophylaxis "
                 "5000 units SC. Infusions run at 1000–2000 units/hour.",
        "source": "ISMP List of High-Alert Medications (heparin, unfractionated, "
                  "IV); BNF 'Heparin (unfractionated)'. 25,000 units is a whole "
                  "infusion BAG, never a single dose, and a bag is charted as "
                  "'X units in Y mL', which the parser declines.",
    },
    {
        "id": "enoxaparin",
        "match": ("enoxaparin", "clexane", "lovenox", "dalteparin", "fragmin",
                  "tinzaparin", "innohep"),
        "routes": None,
        "unit": "mg",
        "warn_above": 150.0,
        "block_at": 300.0,
        "basis": "Prophylaxis 40 mg once daily; treatment 1 mg/kg twice daily or "
                 "1.5 mg/kg once daily, i.e. up to ~150 mg in a heavy adult.",
        "source": "ISMP high-alert (anticoagulants); BNF 'Enoxaparin sodium'. "
                  "400 mg is the 10x slip from the 40 mg prophylactic dose.",
    },
    {
        "id": "warfarin",
        "match": ("warfarin", "coumadin", "acitrom", "acenocoumarol"),
        "routes": None,
        "unit": "mg",
        "warn_above": 15.0,
        "block_at": 50.0,
        "basis": "Usual daily dose 1–10 mg; loading up to 10 mg; genuinely "
                 "resistant patients occasionally need 15–20 mg.",
        "source": "ISMP high-alert (anticoagulants); BNF 'Warfarin sodium'. "
                  "50 mg is the 10x slip from the common 5 mg dose.",
    },
    # ── POTASSIUM ───────────────────────────────────────────────────────────
    {
        "id": "potassium_chloride",
        "match": ("potassium chloride", "kcl", "potassium cl", "sando-k",
                  "sando k"),
        "routes": None,
        "unit": "mmol",
        "warn_above": 40.0,
        "block_at": 100.0,
        "basis": "Oral supplements are 12–24 mmol per dose. IV replacement is "
                 "conventionally 20–40 mmol per bag, infused, never pushed.",
        "source": "ISMP List of High-Alert Medications (potassium chloride "
                  "concentrate for injection). The lethal error with potassium "
                  "is the ROUTE, not the number. See ROUTE_RULES below.",
    },
    # ── ADRENALINE ──────────────────────────────────────────────────────────
    # The clearest case for route-aware ceilings: 0.5 mg IM, 1 mg IV and 5 mg
    # nebulised are all correct, and each is an overdose by another route.
    {
        "id": "adrenaline_nebulised",
        "match": ("adrenaline", "epinephrine", "adrenalin"),
        "exclude": ("noradrenaline", "norepinephrine"),
        "routes": ("nebulised",),
        "unit": "mg",
        "warn_above": 5.0,
        "block_at": 20.0,
        "basis": "Nebulised adrenaline for croup/post-extubation stridor is up "
                 "to 5 mg (5 mL of 1:1000).",
        "source": "BNF/APLS nebulised adrenaline 400 micrograms/kg to a max of "
                  "5 mg. Listed separately because a 5 mg ceiling copied from "
                  "the IM rule would block every correct nebuliser.",
    },
    {
        "id": "adrenaline_im_sc",
        "match": ("adrenaline", "epinephrine", "adrenalin"),
        "exclude": ("noradrenaline", "norepinephrine"),
        "routes": ("im", "sc"),
        "unit": "mg",
        "warn_above": 0.5,
        "block_at": 2.0,
        "basis": "Anaphylaxis: 0.5 mg IM (0.5 mL of 1:1000) in an adult, "
                 "repeatable every 5 minutes.",
        "source": "Resuscitation Council anaphylaxis algorithm: 500 micrograms "
                  "IM. Giving the 1 mg CARDIAC ARREST ampoule (1:10,000) by the "
                  "anaphylaxis route is a named, recurring, fatal confusion.",
    },
    {
        "id": "adrenaline_other",
        "match": ("adrenaline", "epinephrine", "adrenalin"),
        "exclude": ("noradrenaline", "norepinephrine"),
        "routes": None,
        "unit": "mg",
        "warn_above": 1.0,
        "block_at": 10.0,
        "basis": "Cardiac arrest: 1 mg IV every 3–5 minutes. No route uses a "
                 "single dose above the 5 mg nebulised maximum.",
        "source": "Resuscitation Council ALS algorithm: 1 mg IV adrenaline. "
                  "The block sits at 2x the largest legitimate dose by ANY "
                  "route, so it cannot contradict the two rules above.",
    },
    # ── SEDATIVES ───────────────────────────────────────────────────────────
    {
        "id": "midazolam",
        "match": ("midazolam", "dormicum", "versed", "buccolam"),
        "routes": None,
        "unit": "mg",
        "warn_above": 10.0,
        "block_at": 50.0,
        "basis": "Procedural sedation 1–5 mg IV titrated; 10 mg buccal is the "
                 "standard adult seizure dose.",
        "source": "ISMP List of High-Alert Medications (moderate sedation "
                  "agents, IV); BNF 'Midazolam'. 50 mg is the 10x slip from 5 mg.",
    },
    {
        "id": "propofol",
        "match": ("propofol", "diprivan",),
        "routes": ("iv",),
        "unit": "mg",
        "warn_above": 300.0,
        "block_at": 1000.0,
        "basis": "Induction 1.5–2.5 mg/kg, i.e. 100–200 mg in an adult.",
        "source": "ISMP high-alert (anaesthetic agents, IV); BNF 'Propofol'. "
                  "Maintenance is a rate (mg/kg/hour) and is declined by the "
                  "parser rather than judged here.",
    },
    # ── METHOTREXATE ────────────────────────────────────────────────────────
    # The famous methotrexate death is not a big dose — it is a WEEKLY drug
    # given DAILY. That is why this entry carries `min_interval_hours`, which
    # the duplicate guard uses as a floor (see duplicate_window_hours).
    {
        "id": "methotrexate_parenteral",
        "match": ("methotrexate", "mtx"),
        "routes": ("parenteral",),
        "skip": True,
        "basis": "High-dose oncology methotrexate is dosed per m² and reaches "
                 "grams. There is no ceiling here we could state honestly.",
        "source": "Deliberate non-coverage. Blocking oncology protocols is "
                  "exactly the behaviour that gets a guard switched off.",
    },
    {
        "id": "methotrexate_oral",
        "match": ("methotrexate", "mtx"),
        "routes": ("oral",),
        "unit": "mg",
        "warn_above": 30.0,
        "block_at": 100.0,
        "min_interval_hours": 144.0,   # 6 days
        "basis": "Oral methotrexate for rheumatoid arthritis/psoriasis is "
                 "7.5–25 mg ONCE WEEKLY.",
        "source": "ISMP List of High-Alert Medications (methotrexate, oral, "
                  "non-oncologic use). ISMP's stated hazard is daily "
                  "administration of a weekly drug, which is why this entry "
                  "constrains the INTERVAL as well as the amount.",
    },
    # ── PARACETAMOL ─────────────────────────────────────────────────────────
    # Not an ISMP high-alert drug, and included on purpose anyway: it is the
    # single most-administered drug on an Indian ward, the 10x slip (500 mg ->
    # 5 g) is hepatotoxic, and duplicate administration across a branded and a
    # generic order is the commonest real duplicate there is.
    {
        "id": "paracetamol",
        "match": ("paracetamol", "acetaminophen", "panadol", "calpol",
                  "crocin", "tylenol", "perfalgan"),
        "routes": None,
        "unit": "mg",
        "warn_above": 1000.0,
        "block_at": 3000.0,
        "basis": "500 mg–1 g per dose, maximum 4 g in 24 hours.",
        "source": "BNF 'Paracetamol': 0.5–1 g every 4–6h, max 4 g daily. The "
                  "block sits below the 24-hour maximum because 3 g has never "
                  "been a single dose, and above 1 g so the standard dose is "
                  "silent.",
    },
    # ── VINCA ALKALOIDS ─────────────────────────────────────────────────────
    {
        "id": "vincristine",
        "match": ("vincristine", "vinblastine", "vinorelbine", "vindesine"),
        "routes": None,
        "unit": "mg",
        "warn_above": 2.0,
        "block_at": 5.0,
        "basis": "Vincristine is conventionally capped at 2 mg per dose.",
        "source": "Standard oncology practice caps vincristine at 2 mg/dose. "
                  "The fatal vincristine error is the ROUTE. See ROUTE_RULES.",
    },
]

# ── Never-correct route combinations ────────────────────────────────────────
# Not dose rules: the amount is irrelevant, the route IS the error. Both of
# these are on every "never events" list in medicine.
ROUTE_RULES: list[dict] = [
    {
        "id": "kcl_iv_push",
        "match": ("potassium chloride", "kcl", "potassium cl"),
        "forbidden": ("push",),
        "message": "Concentrated potassium chloride must NEVER be given by IV "
                   "push or bolus, it causes immediate fatal cardiac arrest. "
                   "It must be diluted and infused.",
        "basis": "IV potassium is always diluted and infused, rate-limited "
                 "(conventionally ≤10 mmol/hour peripherally).",
        "source": "ISMP List of High-Alert Medications (potassium chloride "
                  "concentrate) and the ISMP/WHO 'never push concentrated "
                  "potassium' rule, one of the oldest medication-safety "
                  "recommendations there is. NOTE: a route charted as plain "
                  "'IV' is deliberately NOT blocked, because it cannot be "
                  "distinguished from a correct infusion, and blocking every "
                  "correct potassium infusion would train the ward to click "
                  "through this alert.",
    },
    {
        "id": "vinca_intrathecal",
        "match": ("vincristine", "vinblastine", "vinorelbine", "vindesine"),
        "forbidden": ("intrathecal",),
        "message": "Vinca alkaloids given intrathecally are universally fatal. "
                   "This route is never correct.",
        "basis": "Vincristine is intravenous only.",
        "source": "WHO/ISMP alert on fatal intrathecal vincristine "
                  "administration; this is a designated never-event worldwide.",
    },
]


def _rule_matches_name(rule: dict, names: Iterable[str]) -> bool:
    """Does any of the drug's names (charted name + resolved ingredients) hit
    this rule, without hitting one of its exclusions?

    Exclusions are load-bearing, not decoration: "noradrenaline" contains
    "adrenaline", and judging a noradrenaline infusion against the adrenaline
    anaphylaxis ceiling would fire on every correct vasopressor in the ICU.
    """
    for n in names:
        if not n:
            continue
        if any(pat in n for pat in rule["match"]):
            if any(bad in n for bad in rule.get("exclude", ())):
                continue
            return True
    return False


def find_dose_rule(names: Iterable[str], route_tags: frozenset[str]) -> Optional[dict]:
    """First rule matching the drug AND applicable to the route, or None.

    Order in :data:`DOSE_RULES` is significant: route-specific entries precede
    route-agnostic ones so a parenteral dose is judged against a parenteral
    ceiling. A rule that names routes does NOT apply when the route could not be
    classified — an unread route is not permission to use someone else's ceiling.
    """
    names = [_norm(n) for n in names if n]
    for rule in DOSE_RULES:
        if not _rule_matches_name(rule, names):
            continue
        wanted = rule.get("routes")
        if wanted is not None and not (route_tags & set(wanted)):
            continue
        return rule
    return None


def _fmt(value: float, rule: dict) -> str:
    """Render a canonical value in the unit a clinician thinks in."""
    unit = rule.get("unit", "")
    display = rule.get("display")
    if display:
        unit, factor = display
        value = value * factor
    text = f"{value:.10g}"
    return f"{text} {unit}".strip()


def evaluate_dose(
    drug_name: str,
    dose_text: Optional[str],
    route_text: Optional[str] = None,
    ingredients: Iterable[str] = (),
    ordered_dose: Optional[str] = None,
) -> dict:
    """PURE magnitude + route screen for ONE dose. No DB, no I/O.

    Returns ``{"decision", "rule", "parsed_dose", "route_tags", "findings"}``.
    Silence (ALLOW with no findings) is the answer for an unknown drug, an
    unparseable dose, a dose in a unit the rule is not written in, and a dose
    inside the ordinary range — four different reasons, all of which must look
    identical to the ward: nothing happens.
    """
    names = [drug_name, *[i for i in ingredients if i]]
    route_tags = classify_route(route_text)
    findings: list[dict] = []
    decision = DOSE_ALLOW

    # (a) Never-correct route combinations. Checked FIRST and independently of
    #     the dose: the amount is not the error here.
    for rule in ROUTE_RULES:
        if not _rule_matches_name(rule, [_norm(n) for n in names if n]):
            continue
        if route_tags & set(rule["forbidden"]):
            findings.append({
                "kind": "route_forbidden",
                "decision": DOSE_BLOCK,
                "rule": rule["id"],
                "drug": drug_name,
                "route_given": route_text,
                "message": rule["message"],
                "source": rule["source"],
            })
            decision = DOSE_BLOCK

    # (b) Magnitude.
    parsed = parse_dose(dose_text)
    rule = find_dose_rule(names, route_tags)
    rule_id = rule["id"] if rule else None

    if rule and not rule.get("skip") and parsed:
        if parsed["unit"] == rule["unit"]:
            value = parsed["value"]
            block_at = rule.get("block_at")
            warn_above = rule.get("warn_above")
            if block_at is not None and value >= block_at:
                findings.append({
                    "kind": "dose_magnitude",
                    "decision": DOSE_BLOCK,
                    "rule": rule_id,
                    "drug": drug_name,
                    "dose_given": parsed["text"],
                    "ordered_dose": ordered_dose,
                    "message": (
                        f"{drug_name} {parsed['text']} is at or above "
                        f"{_fmt(block_at, rule)}, no adult indication reaches "
                        f"this as a single dose. {rule['basis']} "
                        + (f"The order reads {ordered_dose}. " if ordered_dose else "")
                        + "Check for a decimal or unit error before proceeding."
                    ),
                    "threshold": block_at,
                    "unit": rule["unit"],
                    "basis": rule["basis"],
                    "source": rule["source"],
                })
                decision = DOSE_BLOCK
            elif warn_above is not None and value > warn_above:
                findings.append({
                    "kind": "dose_magnitude",
                    "decision": DOSE_WARN,
                    "rule": rule_id,
                    "drug": drug_name,
                    "dose_given": parsed["text"],
                    "ordered_dose": ordered_dose,
                    "message": (
                        f"{drug_name} {parsed['text']} is above the ordinary "
                        f"adult range ({_fmt(warn_above, rule)}). {rule['basis']} "
                        "This may well be deliberate. Proceed if it is intended."
                    ),
                    "threshold": warn_above,
                    "unit": rule["unit"],
                    "basis": rule["basis"],
                    "source": rule["source"],
                })
                if _DOSE_RANK[DOSE_WARN] > _DOSE_RANK[decision]:
                    decision = DOSE_WARN

    return {
        "decision": decision,
        "rule": rule_id if (rule and not rule.get("skip")) else None,
        "parsed_dose": parsed,
        "route_tags": sorted(route_tags),
        "findings": findings,
    }


# ── Frequency + the duplicate window ─────────────────────────────────────────
# "Too soon" is a property of the DRUG, not a constant. Paracetamol q6h and
# enoxaparin once daily have completely different answers, and a flat window
# would either miss the first or cry wolf on the second.
UNKNOWN_FREQUENCY_WINDOW_HOURS = 4.0
"""Window used when the order's frequency cannot be read.

Chosen at 4 hours because it is SHORTER than every standard inpatient interval
(q6h/QDS, TDS, BD, OD, weekly) and equal to the shortest common PRN interval, so
an unknown frequency produces an alert only for a repeat that is implausible
under ANY ordinary schedule. Erring shorter is the right direction for a warning
that a nurse must read: a missed duplicate is a clinical event, a false one is a
click, but a stream of false ones is how the real one gets clicked through too.
"""

# Fraction of the dosing interval inside which a repeat is implausible.
_WINDOW_FRACTION_SHORT = 0.5    # intervals under a day
_WINDOW_FRACTION_LONG = 0.75    # daily and longer

# (pattern, hours-per-captured-unit). A numeric interval always wins over a
# word: "q4h PRN" is a q4h order that happens to be as-required.
_FREQUENCY_PATTERNS: tuple[tuple[re.Pattern, float], ...] = (
    (re.compile(r"\bq\.?\s?(\d+(?:\.\d+)?)\s?h\b", re.I), 1.0),      # q6h
    (re.compile(r"\bevery\s+(\d+(?:\.\d+)?)\s*(?:h|hr|hrs|hour|hours)\b", re.I), 1.0),
    (re.compile(r"\b(\d+(?:\.\d+)?)\s*(?:-|\s)?hourly\b", re.I), 1.0),
    (re.compile(r"\bq\.?\s?(\d+)\s?d\b", re.I), 24.0),               # q2d
    (re.compile(r"\bevery\s+(\d+)\s*(?:d|day|days)\b", re.I), 24.0),
)

_FREQUENCY_WORDS: tuple[tuple[re.Pattern, float], ...] = (
    (re.compile(r"\b(?:qds|qid|q\.i\.d|four times (?:a )?(?:a )?day)\b", re.I), 6.0),
    (re.compile(r"\b(?:tds|tid|t\.i\.d|thrice daily|three times (?:a )?day)\b", re.I), 8.0),
    (re.compile(r"\b(?:bd|bid|b\.i\.d|twice daily|twice a day|12 hourly)\b", re.I), 12.0),
    (re.compile(r"\balternate days?\b|\beod\b|\balt\.? days?\b|\bevery other day\b", re.I), 48.0),
    (re.compile(r"\bmonthly\b|\bq4wk\b|\bevery month\b", re.I), 720.0),
    (re.compile(r"\bfortnightly\b|\bevery (?:2|two) weeks\b|\bq2wk\b", re.I), 336.0),
    (re.compile(r"\bweekly\b|\bonce a week\b|\bq7d\b|\bqwk\b|\bevery week\b", re.I), 168.0),
    (re.compile(r"\b(?:od|qd|o\.d|once daily|once a day|daily|nocte|mane|"
                r"om|on|hs|at night|morning)\b", re.I), 24.0),
)


def parse_frequency_hours(text: Optional[str]) -> Optional[float]:
    """Dosing interval in hours from an order's frequency text, or None.

    None means "the order does not tell me", which includes PRN / SOS / STAT /
    'as required'. That is an honest answer and the duplicate guard has a
    declared default for it — inventing an interval for a PRN order would be
    the sort of guess that produces confident nonsense.
    """
    if not text:
        return None
    s = str(text)
    for pattern, hours_per_unit in _FREQUENCY_PATTERNS:
        m = pattern.search(s)
        if m:
            try:
                n = float(m.group(1))
            except (TypeError, ValueError):     # pragma: no cover
                continue
            if n <= 0:
                continue
            return n * hours_per_unit
    for pattern, hours in _FREQUENCY_WORDS:
        if pattern.search(s):
            return hours
    return None


def duplicate_window_hours(
    frequency_text: Optional[str],
    drug_floor_hours: Optional[float] = None,
) -> tuple[float, str]:
    """(window_hours, basis) — how soon is "too soon" for THIS order.

    basis ∈ ``frequency`` | ``unknown_frequency_default`` | ``drug_floor``.

    A drug-level floor overrides a shorter frequency-derived window, and exists
    for exactly one class of problem: where the ORDER'S OWN FREQUENCY IS THE
    ERROR. Oral methotrexate charted daily is the canonical case — deriving the
    window from "OD" would make the guard agree with the mistake it exists to
    catch.
    """
    interval = parse_frequency_hours(frequency_text)
    if interval is None:
        window, basis = UNKNOWN_FREQUENCY_WINDOW_HOURS, "unknown_frequency_default"
    elif interval < 24.0:
        window, basis = interval * _WINDOW_FRACTION_SHORT, "frequency"
    else:
        window, basis = interval * _WINDOW_FRACTION_LONG, "frequency"
    if drug_floor_hours and drug_floor_hours > window:
        return drug_floor_hours, "drug_floor"
    return window, basis


def same_product(names_a: Iterable[str], names_b: Iterable[str]) -> bool:
    """Do two charted drugs denote the same product?

    Runs through :func:`names_overlap` — the SAME vocabulary the allergy
    interlock uses, including the minimum-substring guard — so "Paracetamol"
    and "Paracetamol 650" are one drug while "Insulin Glargine" and "Insulin
    Aspart" (basal + bolus, correctly given together) are two.
    """
    a = [_norm(n) for n in names_a if _norm(n or "")]
    b = [_norm(n) for n in names_b if _norm(n or "")]
    return any(names_overlap(x, y) for x in a for y in b)


def evaluate_duplicate(
    drug_name: str,
    ingredients: Iterable[str],
    frequency_text: Optional[str],
    prior_doses: list[dict],
    drug_floor_hours: Optional[float] = None,
) -> Optional[dict]:
    """PURE duplicate screen. ``prior_doses`` are dicts with ``names`` (the
    charted name plus any resolved ingredient names) and ``hours_ago``.

    Returns the finding for the most recent implausibly-close dose, or None.
    Only ever a WARNING: the nurse may know something the chart does not (a
    dropped tablet, a split dose, a dose given and not yet charted), and a
    block here would be wrong far more often than it was right.
    """
    window, basis = duplicate_window_hours(frequency_text, drug_floor_hours)
    mine = [drug_name, *[i for i in ingredients if i]]

    closest = None
    for prior in prior_doses:
        hours_ago = prior.get("hours_ago")
        if hours_ago is None or hours_ago < 0 or hours_ago >= window:
            continue
        if not same_product(mine, prior.get("names") or []):
            continue
        if closest is None or hours_ago < closest["hours_ago"]:
            closest = {**prior, "hours_ago": hours_ago}
    if closest is None:
        return None

    minutes = closest["hours_ago"] * 60.0
    return {
        "kind": "duplicate",
        "decision": DOSE_WARN,
        "drug": drug_name,
        "previous_administration_id": closest.get("administration_id"),
        "previous_medication_id": closest.get("medication_id"),
        "previous_drug_name": closest.get("drug_name"),
        "previous_administered_at": closest.get("administered_at"),
        "previous_administered_by": closest.get("administered_by"),
        "minutes_since": round(minutes, 1),
        "window_hours": window,
        "window_basis": basis,
        "message": (
            f"{closest.get('drug_name') or drug_name} was already given to this "
            f"patient {_humanise_gap(minutes)} ago"
            + (" (on a different order)"
               if closest.get("medication_id") is not None
               and closest.get("same_order") is False else "")
            + f". The order's schedule makes a repeat inside {window:g}h "
            f"implausible ({basis.replace('_', ' ')}). Confirm this is not a "
            f"double dose before charting."
        ),
    }


def _humanise_gap(minutes: float) -> str:
    if minutes < 90:
        return f"{int(round(minutes))} minutes"
    hours = minutes / 60.0
    if hours < 48:
        return f"{hours:.1f} hours"
    return f"{hours / 24:.1f} days"


# ── Override ─────────────────────────────────────────────────────────────────
# Deliberately the SAME mechanism as the allergy interlock: a typed clinical
# reason, validated against reflex clear-through, written to a ledger row in the
# same transaction as the dose, carried forward only while a fingerprint holds.
# The policy constants are restated rather than imported because
# allergy_interlock imports THIS module (importing back would be circular); the
# wording differs because a nurse reading it is being asked a different question.
MIN_OVERRIDE_REASON_CHARS = 10
_JUNK_DOSE_REASONS = {
    "ok", "okay", "yes", "y", "n/a", "na", "none", "fine", "done", "test",
    "asdf", "aaaa", "override", "no reason", "as discussed", "per doctor",
    "as per doctor", "doctor said", "verbal order", "urgent", "emergency",
    "correct", "confirmed", "checked", "double checked", "as charted",
    "as prescribed", "-", ".", "..", "...",
}
#: The same reflex clear-throughs refused for any written override at order
#: time (services/medorders/screening.validate_duplicate_reason reads it).
JUNK_OVERRIDE_REASONS = frozenset(_JUNK_DOSE_REASONS)


def validate_dose_override_reason(reason: Optional[str]) -> str:
    """Clean a typed dose-override reason, or raise ValueError.

    Same shape as allergy_interlock.validate_override_reason and for the same
    reason: "ok" must not clear a 10x insulin dose.
    """
    cleaned = re.sub(r"\s+", " ", (reason or "").strip())
    if not cleaned:
        raise ValueError("a dose override requires a typed reason")
    if cleaned.lower() in _JUNK_DOSE_REASONS:
        raise ValueError(
            "a dose override requires a clinical reason "
            "(e.g. 'digitalisation loading dose, cardiology at bedside, ECG monitored')"
        )
    if len(set(cleaned.replace(" ", ""))) <= 1:
        raise ValueError("a dose override requires a clinical reason")
    if len(cleaned) < MIN_OVERRIDE_REASON_CHARS:
        raise ValueError(
            f"a dose override reason must be at least {MIN_OVERRIDE_REASON_CHARS} "
            f"characters describing WHY this dose is correct for this patient"
        )
    return cleaned


def dose_fingerprint(
    drug_name: str,
    rule_id: Optional[str],
    parsed_dose: Optional[dict],
    route_tags: Iterable[str],
    matches_order: bool,
) -> Optional[str]:
    """Stable identity of "this drug at this dose by this route on this order".

    An override is carried forward only while the fingerprint holds, so changing
    the dose, the route or the rule that fired RE-ARMS the guard rather than
    letting a stale acknowledgement cover a new number.

    ``matches_order`` is part of the identity on purpose — see
    emar_service._screen_dose for why a bedside deviation is never carried.

    No rule fired means nothing was decided, so there is nothing to carry:
    None. A route rule with no parseable dose (concentrated potassium by IV
    push) still gets one — there the ROUTE is the whole finding.
    """
    if not rule_id:
        return None
    raw = "\x1f".join([
        _norm(drug_name), rule_id,
        f"{parsed_dose['value']:.10g}" if parsed_dose else "unparsed",
        parsed_dose["unit"] if parsed_dose else "n/a",
        ",".join(sorted(route_tags)),
        "order" if matches_order else "deviation",
    ])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def summarize_dose_check(check: Optional[dict]) -> Optional[dict]:
    """Compact form stored on the administration row and sent to the ward board.

    The full finding list (with sources) goes to the audit chain; this is what a
    UI needs to render a banner without re-deriving anything.
    """
    if not check:
        return None
    return {
        "decision": check.get("decision"),
        "rule": check.get("rule"),
        "dose_given": check.get("dose_given"),
        "route_given": check.get("route_given"),
        "parsed_dose": check.get("parsed_dose"),
        "unavailable": bool(check.get("unavailable")),
        "unavailable_reason": check.get("unavailable_reason"),
        "fingerprint": check.get("fingerprint"),
        "duplicate": check.get("duplicate"),
        "override": check.get("override"),
        "findings": [
            {
                "kind": f.get("kind"),
                "decision": f.get("decision"),
                "rule": f.get("rule"),
                "message": f.get("message"),
                "threshold": f.get("threshold"),
                "unit": f.get("unit"),
            }
            for f in (check.get("findings") or [])
        ],
        "message": check.get("message"),
    }
