"""Orb — Data-backed drug–drug interaction store.

Replaces the small hardcoded interaction list that used to live inside
``drug_safety_service`` with a real, loadable table (``drug_interactions``).
This module owns:

  * a MEANINGFULLY EXPANDED curated ("builtin") interaction knowledge base
    (>40 clinically important class/ingredient pairs), idempotently upserted;
  * a symmetric ``lookup_interactions`` query used by the order-time safety gate;
  * bulk-loadable rows from external open sources (DDInter, etc.) via
    ``scripts/load_interactions.py`` (source != 'builtin').

Design notes
------------
Terms are stored lowercased. ``match_type='class'`` rows pair *drug classes or
groups*; ``match_type='ingredient'`` rows pair specific active ingredients. The
safety gate maps a drug name to every term it could be stored under
(``drug_safety_service.interaction_terms``: classes, groups and the ingredient
words of the name) and calls :func:`lookup_interactions`, which is symmetric, so
the alert fires regardless of which side of the pair the new vs. existing drug
matches. Every term used below must be one that function emits;
tests/test_interaction_coverage.py fails the build when a row cannot fire.

Every curated pair below carries a short mechanism and its clinical source in a
trailing comment (BNF / Stockley's / FDA labelling / DDInter). This is a
starter clinical set, NOT a comprehensive database; production deployments
should additionally bulk-load a licensed/maintained source.
"""

from __future__ import annotations

import logging

from database import db_session

logger = logging.getLogger("orb.interaction_db")

# ── Curated builtin interaction set ──────────────────────────────────────────
# (term_a, term_b, match_type, severity, mechanism)
# Ordered roughly by drug family. Sources noted inline.
_BUILTIN: list[tuple[str, str, str, str, str]] = [
    # ── Anticoagulants / antiplatelets — bleeding ───────────────────────────
    (
        "anticoagulants",
        "nsaids",
        "class",
        "severe",
        "Additive bleeding risk (GI/intracranial).",
    ),  # BNF: warfarin + NSAID
    (
        "anticoagulants",
        "anticoagulants",
        "class",
        "severe",
        "Compounded anticoagulation, major bleeding risk.",
    ),  # BNF
    (
        "warfarin",
        "nsaids",
        "ingredient",
        "severe",
        "Displacement + antiplatelet effect markedly raises bleeding risk.",
    ),  # Stockley's
    (
        "warfarin",
        "aspirin",
        "ingredient",
        "severe",
        "Additive antiplatelet + anticoagulant effect, major bleeding.",
    ),  # Stockley's
    (
        "warfarin",
        "azoles",
        "ingredient",
        "severe",
        "CYP2C9 inhibition (fluconazole/miconazole) raises INR, bleeding.",
    ),  # BNF
    (
        "warfarin",
        "macrolides",
        "ingredient",
        "severe",
        "CYP3A4 inhibition raises warfarin effect, raised INR.",
    ),  # Stockley's
    (
        "warfarin",
        "amiodarone",
        "ingredient",
        "severe",
        "CYP2C9/3A4 inhibition raises INR over days–weeks.",
    ),  # BNF
    (
        "warfarin",
        "metronidazole",
        "ingredient",
        "severe",
        "Inhibits warfarin metabolism, markedly raised INR.",
    ),  # Stockley's
    (
        "warfarin",
        "trimethoprim",
        "ingredient",
        "severe",
        "Co-trimoxazole inhibits CYP2C9 and displaces warfarin, raised INR.",
    ),  # BNF
    (
        "anticoagulants",
        "ssri",
        "class",
        "moderate",
        "Impaired platelet function adds to anticoagulation, GI bleed.",
    ),  # BNF
    # Omeprazole and esomeprazole only: pantoprazole is the usual alternative.
    (
        "clopidogrel",
        "cyp2c19_inhibiting_ppis",
        "ingredient",
        "moderate",
        "Omeprazole/esomeprazole inhibit CYP2C19 → reduced clopidogrel activation.",
    ),  # FDA
    # ── ACE inhibitors / ARBs / potassium — hyperkalaemia & renal ───────────
    ("ace_inhibitors", "potassium", "class", "moderate", "Hyperkalaemia risk."),  # BNF
    (
        "ace_inhibitors",
        "nsaids",
        "class",
        "moderate",
        "Reduced renal function; hyperkalaemia; blunted antihypertensive effect.",
    ),  # BNF
    (
        "ace_inhibitors",
        "potassium_sparing_diuretics",
        "class",
        "moderate",
        "Additive hyperkalaemia.",
    ),  # BNF
    (
        "potassium",
        "potassium_sparing_diuretics",
        "class",
        "severe",
        "Additive potassium load, dangerous hyperkalaemia.",
    ),  # BNF
    (
        "potassium_sparing_diuretics",
        "nsaids",
        "class",
        "moderate",
        "Reduced renal excretion of potassium, hyperkalaemia.",
    ),  # BNF
    (
        "spironolactone",
        "potassium",
        "ingredient",
        "severe",
        "Potassium-sparing diuretic + potassium, severe hyperkalaemia.",
    ),  # BNF
    # ── Statins — myopathy / rhabdomyolysis ─────────────────────────────────
    (
        "statins",
        "macrolides",
        "class",
        "moderate",
        "Increased statin levels → myopathy/rhabdomyolysis risk.",
    ),  # BNF (simvastatin)
    (
        "statins",
        "azoles",
        "class",
        "moderate",
        "CYP3A4 inhibition raises statin levels, myopathy.",
    ),  # BNF
    (
        "statins",
        "fibrates",
        "class",
        "moderate",
        "Additive muscle toxicity, rhabdomyolysis.",
    ),  # BNF
    (
        "statins",
        "amiodarone",
        "class",
        "moderate",
        "Raised simvastatin levels, dose-related myopathy.",
    ),  # FDA labelling
    (
        "statins",
        "verapamil",
        "class",
        "moderate",
        "CYP3A4 inhibition raises simvastatin, myopathy.",
    ),  # BNF
    (
        "statins",
        "ciclosporin",
        "ingredient",
        "severe",
        "Markedly raised statin exposure, rhabdomyolysis.",
    ),  # BNF
    # ── SSRIs / MAOIs / serotonergics — serotonin syndrome ──────────────────
    (
        "ssri",
        "maoi",
        "class",
        "severe",
        "Serotonin syndrome, contraindicated; observe washout period.",
    ),  # BNF (contraindicated)
    (
        "ssri",
        "nsaids",
        "class",
        "moderate",
        "Impaired platelet aggregation + GI irritation, GI bleeding.",
    ),  # BNF
    (
        "ssri",
        "triptans",
        "class",
        "moderate",
        "Additive serotonergic effect, serotonin syndrome.",
    ),  # FDA
    (
        "ssri",
        "tramadol",
        "ingredient",
        "moderate",
        "Additive serotonergic + seizure risk, serotonin syndrome.",
    ),  # BNF
    (
        "maoi",
        "opioids",
        "class",
        "severe",
        "Pethidine/tramadol + MAOI, serotonin syndrome / CNS excitation.",
    ),  # BNF
    ("maoi", "sympathomimetics", "class", "severe", "Hypertensive crisis."),  # BNF
    # ── Digoxin — raised levels / toxicity ──────────────────────────────────
    (
        "digoxin",
        "amiodarone",
        "ingredient",
        "severe",
        "Amiodarone raises digoxin levels, toxicity; halve digoxin dose.",
    ),  # BNF
    (
        "digoxin",
        "verapamil",
        "ingredient",
        "moderate",
        "Reduced digoxin clearance, raised levels.",
    ),  # BNF
    (
        "digoxin",
        "macrolides",
        "ingredient",
        "moderate",
        "Raised digoxin absorption/levels, toxicity.",
    ),  # Stockley's
    (
        "digoxin",
        "diuretics",
        "ingredient",
        "moderate",
        "Diuretic-induced hypokalaemia potentiates digoxin toxicity.",
    ),  # BNF
    # ── Methotrexate — antifolate toxicity ──────────────────────────────────
    (
        "methotrexate",
        "nsaids",
        "ingredient",
        "severe",
        "Reduced renal clearance of methotrexate, myelosuppression.",
    ),  # BNF
    (
        "methotrexate",
        "trimethoprim",
        "ingredient",
        "severe",
        "Additive antifolate effect, severe myelosuppression.",
    ),  # BNF (avoid)
    (
        "methotrexate",
        "penicillins",
        "ingredient",
        "moderate",
        "Reduced renal excretion of methotrexate, raised levels.",
    ),  # Stockley's
    (
        "methotrexate",
        "proton_pump_inhibitors",
        "ingredient",
        "moderate",
        "Reduced clearance of high-dose methotrexate.",
    ),  # FDA
    # ── QT prolongation — torsades ──────────────────────────────────────────
    (
        "qt_prolonging",
        "qt_prolonging",
        "class",
        "severe",
        "Additive QT prolongation, torsades de pointes.",
    ),  # CredibleMeds
    (
        "macrolides",
        "qt_prolonging",
        "class",
        "moderate",
        "Additive QT prolongation.",
    ),  # CredibleMeds
    (
        "antipsychotics",
        "qt_prolonging",
        "class",
        "severe",
        "Additive QT prolongation, torsades.",
    ),  # CredibleMeds
    (
        "fluoroquinolones",
        "qt_prolonging",
        "class",
        "moderate",
        "Additive QT prolongation.",
    ),  # CredibleMeds
    # ── CNS / respiratory depression ────────────────────────────────────────
    (
        "opioids",
        "opioids",
        "class",
        "moderate",
        "Additive sedation / respiratory depression.",
    ),  # BNF
    (
        "opioids",
        "benzodiazepines",
        "class",
        "severe",
        "Profound sedation, respiratory depression, coma and death.",
    ),  # FDA black box
    (
        "opioids",
        "gabapentinoids",
        "class",
        "moderate",
        "Additive CNS/respiratory depression.",
    ),  # MHRA
    # ── Lithium — narrow therapeutic index ──────────────────────────────────
    (
        "lithium",
        "nsaids",
        "ingredient",
        "severe",
        "Reduced renal lithium clearance, toxicity.",
    ),  # BNF
    ("lithium", "thiazides", "ingredient", "severe", "Reduced lithium excretion, toxicity."),  # BNF
    (
        "lithium",
        "ace_inhibitors",
        "ingredient",
        "moderate",
        "Reduced lithium clearance, raised levels.",
    ),  # BNF
    (
        "lithium",
        "diuretics",
        "ingredient",
        "moderate",
        "Sodium depletion reduces lithium clearance, toxicity.",
    ),  # BNF
    # ── Theophylline — narrow therapeutic index ─────────────────────────────
    (
        "theophylline",
        "fluoroquinolones",
        "ingredient",
        "severe",
        "Ciprofloxacin inhibits CYP1A2, theophylline toxicity/seizures.",
    ),  # BNF
    (
        "theophylline",
        "macrolides",
        "ingredient",
        "moderate",
        "Reduced theophylline clearance, raised levels.",
    ),  # Stockley's
    # ── Cardiovascular / miscellaneous high-consequence ─────────────────────
    (
        "beta_blockers",
        "verapamil",
        "class",
        "severe",
        "Additive negative chronotropy, bradycardia, AV block, asystole.",
    ),  # BNF (IV avoid)
    (
        "nitrates",
        "sildenafil",
        "ingredient",
        "severe",
        "PDE5 inhibitor + nitrate, profound hypotension; contraindicated.",
    ),  # FDA
    (
        "allopurinol",
        "azathioprine",
        "ingredient",
        "severe",
        "Xanthine-oxidase inhibition raises 6-MP, life-threatening myelosuppression.",
    ),  # BNF
    (
        "metformin",
        "iodinated_contrast",
        "ingredient",
        "moderate",
        "Contrast nephrotoxicity + metformin, lactic acidosis risk; withhold.",
    ),  # BNF
    ("nsaids", "nsaids", "class", "moderate", "Additive GI/renal toxicity."),  # BNF
]


def seed_builtin_interactions() -> int:
    """Idempotently upsert the curated builtin interaction set.

    Returns the number of curated rows processed. Terms are stored lowercased;
    conflicts on (term_a, term_b, match_type) refresh severity/mechanism/source.
    Builtin rows no longer in the curated set are removed (a retired row would
    otherwise keep firing on every database seeded before it changed); rows
    from any other source are never touched.
    """
    current = {(a.lower().strip(), b.lower().strip(), mt) for a, b, mt, _, _ in _BUILTIN}
    with db_session() as db:
        for row in db.execute(
            "SELECT id, term_a, term_b, match_type FROM drug_interactions WHERE source = 'builtin'"
        ).fetchall():
            if (row["term_a"], row["term_b"], row["match_type"]) not in current:
                db.execute("DELETE FROM drug_interactions WHERE id = ?", (row["id"],))
        for term_a, term_b, match_type, severity, mechanism in _BUILTIN:
            db.execute(
                """INSERT INTO drug_interactions
                       (term_a, term_b, match_type, severity, mechanism, source)
                   VALUES (?, ?, ?, ?, ?, 'builtin')
                   ON CONFLICT(term_a, term_b, match_type) DO UPDATE SET
                       severity=excluded.severity,
                       mechanism=excluded.mechanism,
                       source=excluded.source""",
                (term_a.lower().strip(), term_b.lower().strip(), match_type, severity, mechanism),
            )
    logger.info("Seeded %d builtin drug interactions", len(_BUILTIN))
    return len(_BUILTIN)


def interaction_count() -> int:
    """Total number of interaction rows in the table (all sources)."""
    with db_session() as db:
        return db.execute("SELECT COUNT(*) FROM drug_interactions").fetchone()[0]


_synced_this_process = False


def ensure_seeded() -> None:
    """Bring the builtin set up to date once per process, and again if the table
    is ever found empty.

    Seeding used to happen only into an EMPTY table, so a database seeded once
    kept its first curated set forever and a corrected row never reached it.
    """
    global _synced_this_process
    if not _synced_this_process or interaction_count() == 0:
        seed_builtin_interactions()
        _synced_this_process = True


def builtin_terms() -> set[str]:
    """Every term the curated set names (for the coverage self-check)."""
    return {t for a, b, _, _, _ in _BUILTIN for t in (a, b)}


def known_terms(candidates) -> set[str]:
    """Which of ``candidates`` appear on either side of any interaction row."""
    terms = {c.lower().strip() for c in candidates if c}
    if not terms:
        return set()
    ph = ",".join("?" * len(terms))
    params = list(terms)
    with db_session() as db:
        rows = db.execute(
            f"SELECT term_a AS t FROM drug_interactions WHERE term_a IN ({ph}) "
            f"UNION SELECT term_b FROM drug_interactions WHERE term_b IN ({ph})",
            params + params,
        ).fetchall()
    return {r["t"] for r in rows}


def lookup_interactions(classes_a: set, classes_b: set) -> list[dict]:
    """Symmetric interaction lookup between two sets of terms.

    Returns pairs where one side is in ``classes_a`` and the other in
    ``classes_b`` (in either column order). Terms are matched lowercased against
    both ``term_a`` and ``term_b`` so the caller need not know pair ordering.

    Each result: {"severity", "mechanism", "term_a", "term_b", "match_type"}.
    """
    a = {c.lower().strip() for c in classes_a if c}
    b = {c.lower().strip() for c in classes_b if c}
    if not a or not b:
        return []

    a_ph = ",".join("?" * len(a))
    b_ph = ",".join("?" * len(b))
    a_list, b_list = list(a), list(b)
    sql = (
        "SELECT DISTINCT severity, mechanism, term_a, term_b, match_type FROM drug_interactions "
        f"WHERE (term_a IN ({a_ph}) AND term_b IN ({b_ph})) "
        f"   OR (term_a IN ({b_ph}) AND term_b IN ({a_ph}))"
    )
    params = a_list + b_list + b_list + a_list
    with db_session() as db:
        rows = db.execute(sql, params).fetchall()
    return [
        {
            "severity": r["severity"],
            "mechanism": r["mechanism"],
            "term_a": r["term_a"],
            "term_b": r["term_b"],
            "match_type": r["match_type"],
        }
        for r in rows
    ]
