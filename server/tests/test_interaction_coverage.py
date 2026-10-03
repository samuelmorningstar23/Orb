"""Interaction screening end to end THROUGH the classifier.

The older tests called lookup_interactions with hand-written class names, so
they stayed green while 48 of the 55 curated rows could never fire: the
classifier emitted allergy classes the rows never use, and warfarin with
amiodarone on the chart came back "screened" with no alert. Every test here
starts from a drug NAME, as a prescriber types it.
"""
import uuid

import pytest

from database import db_session
from services import interaction_db
from services.drug_safety_service import (
    check_interactions, check_medication_safety, formulary_interaction_coverage,
    interaction_terms, match_allergies,
)

# One prescribable name per curated term; SECOND gives a different member for
# the self-pairs (two anticoagulants, two QT drugs, ...).
EXAMPLE = {
    "anticoagulants": "Enoxaparin", "nsaids": "Ibuprofen", "warfarin": "Warfarin",
    "aspirin": "Aspirin", "azoles": "Fluconazole", "macrolides": "Clarithromycin",
    "amiodarone": "Amiodarone", "metronidazole": "Metronidazole",
    "trimethoprim": "Co-trimoxazole", "ssri": "Sertraline", "clopidogrel": "Clopidogrel",
    "cyp2c19_inhibiting_ppis": "Omeprazole", "ace_inhibitors": "Ramipril",
    "potassium": "Potassium Chloride", "potassium_sparing_diuretics": "Eplerenone",
    "spironolactone": "Spironolactone", "statins": "Atorvastatin",
    "fibrates": "Fenofibrate", "verapamil": "Verapamil", "ciclosporin": "Ciclosporin",
    "maoi": "Phenelzine", "triptans": "Sumatriptan", "tramadol": "Tramadol",
    "opioids": "Morphine", "sympathomimetics": "Pseudoephedrine", "digoxin": "Digoxin",
    "diuretics": "Furosemide", "methotrexate": "Methotrexate",
    "penicillins": "Amoxicillin", "proton_pump_inhibitors": "Pantoprazole",
    "qt_prolonging": "Sotalol", "antipsychotics": "Quetiapine",
    "fluoroquinolones": "Ciprofloxacin", "benzodiazepines": "Midazolam",
    "gabapentinoids": "Pregabalin", "lithium": "Lithium Carbonate",
    "thiazides": "Hydrochlorothiazide", "theophylline": "Theophylline",
    "beta_blockers": "Metoprolol", "nitrates": "Isosorbide Mononitrate",
    "sildenafil": "Sildenafil", "allopurinol": "Allopurinol",
    "azathioprine": "Azathioprine", "metformin": "Metformin",
    "iodinated_contrast": "Iohexol",
}
SECOND = {"anticoagulants": "Heparin", "nsaids": "Diclofenac",
          "qt_prolonging": "Amiodarone", "opioids": "Fentanyl"}


@pytest.fixture()
def new_patient():
    def make():
        pid = f"PATIC{uuid.uuid4().hex[:6].upper()}"
        with db_session() as db:
            db.execute("INSERT INTO patients (id, name, mrn) VALUES (?, ?, ?)",
                       (pid, "Coverage Test", f"MRN-{uuid.uuid4().hex[:6]}"))
        return pid
    return make


def _chart(pid, drug):
    with db_session() as db:
        db.execute(
            "INSERT INTO medications (patient_id, drug_name, dosage, route, frequency, status) "
            "VALUES (?, ?, '1', 'Oral', 'OD', 'active')",
            (pid, drug),
        )


def test_every_curated_term_is_emitted_by_the_classifier():
    missing = sorted(t for t in interaction_db.builtin_terms()
                     if t not in interaction_terms(EXAMPLE.get(t, "")))
    assert not missing, f"curated terms no drug name can reach: {missing}"


@pytest.mark.parametrize("row", interaction_db._BUILTIN, ids=lambda r: f"{r[0]}+{r[1]}")
def test_every_curated_row_fires_end_to_end(row, new_patient):
    term_a, term_b, _, severity, _ = row
    new_drug = EXAMPLE[term_a]
    on_chart = SECOND[term_b] if term_a == term_b else EXAMPLE[term_b]
    pid = new_patient()
    _chart(pid, on_chart)
    alerts = [a for a in check_interactions(pid, new_drug) if a["with"] == on_chart]
    assert alerts, f"{new_drug} with {on_chart} on the chart raised nothing"
    assert {term_a, term_b} <= set(alerts[0]["classes"])
    # one alert per co-medication, led by the worst matching severity
    assert len(alerts) == 1
    rank = {"mild": 1, "moderate": 2, "severe": 3}
    assert rank[alerts[0]["severity"]] >= rank[severity]


@pytest.mark.parametrize("first,second", [("Warfarin", "Amiodarone"),
                                          ("Amiodarone", "Warfarin")])
def test_warfarin_and_amiodarone_is_severe_in_either_order(first, second, new_patient):
    pid = new_patient()
    _chart(pid, first)
    result = check_medication_safety(pid, second)
    assert result["max_severity"] == "severe"
    assert result["interaction_screened"] is True
    assert result["coverage_note"] is None
    assert any("INR" in a["message"] for a in result["interaction_alerts"])


def test_warfarin_with_aspirin_is_one_alert_keeping_every_mechanism(new_patient):
    pid = new_patient()
    _chart(pid, "Warfarin 5mg")
    alerts = check_interactions(pid, "Aspirin 75mg")
    assert len(alerts) == 1
    assert alerts[0]["severity"] == "severe"
    assert "Additive antiplatelet + anticoagulant effect" in alerts[0]["message"]
    assert "Additive bleeding risk" in alerts[0]["message"]


def test_losartan_potassium_is_not_a_potassium_supplement(new_patient):
    pid = new_patient()
    _chart(pid, "Spironolactone 25mg")
    assert check_interactions(pid, "Losartan Potassium 50mg") == []


def test_clopidogrel_fires_on_omeprazole_family_not_pantoprazole(new_patient):
    pid = new_patient()
    _chart(pid, "Clopidogrel 75mg")
    assert check_interactions(pid, "Pantoprazole 40mg") == []
    for ppi in ("Omeprazole 20mg", "Esomeprazole 40mg"):
        alerts = check_interactions(pid, ppi)
        assert [a["severity"] for a in alerts] == ["moderate"], ppi


def test_interaction_groups_do_not_leak_into_allergy_matching():
    """qt_prolonging is an interaction group, not an allergy class."""
    allergy = [{"id": 1, "substance": "Amiodarone", "reaction": "rash", "severity": "severe"}]
    assert match_allergies(allergy, "Haloperidol") == []


def test_unknown_co_medication_is_reported_not_screened(new_patient):
    pid = new_patient()
    _chart(pid, "Obscuremab")
    result = check_medication_safety(pid, "Warfarin")
    assert result["interaction_alerts"] == []
    assert result["unscreened_with"] == ["Obscuremab"]
    assert "Obscuremab" in result["coverage_note"]
    assert "Not screened" in result["coverage_note"]


def test_unknown_new_drug_is_not_screened(new_patient):
    pid = new_patient()
    _chart(pid, "Warfarin")
    result = check_medication_safety(pid, "Ondansetron")
    assert result["interaction_screened"] is False
    assert "outside the interaction knowledge base" in result["coverage_note"]


@pytest.mark.parametrize("drug", ["Ceftriaxone 1g", "Meropenem 1g", "Gentamicin 80mg",
                                  "Sulfasalazine 500mg"])
def test_a_class_no_row_names_is_not_screened(drug, new_patient):
    """classify_drug knows these (allergy classes) but no interaction row does,
    so a clean result would be a pair that could never fire."""
    pid = new_patient()
    _chart(pid, "Warfarin")
    result = check_medication_safety(pid, drug)
    assert result["interaction_screened"] is False
    assert "outside the interaction knowledge base" in result["coverage_note"]

    pid2 = new_patient()
    _chart(pid2, drug)
    result = check_medication_safety(pid2, "Warfarin")
    assert result["unscreened_with"] == [drug]
    assert "Not screened" in result["coverage_note"]


def test_reseed_retires_stale_builtin_rows_only():
    with db_session() as db:
        db.execute(
            "INSERT OR IGNORE INTO drug_interactions (term_a, term_b, match_type, severity, "
            "mechanism, source) VALUES ('clopidogrel', 'proton_pump_inhibitors', "
            "'ingredient', 'moderate', 'retired row', 'builtin')")
        db.execute(
            "INSERT OR IGNORE INTO drug_interactions (term_a, term_b, match_type, severity, "
            "mechanism, source) VALUES ('zzloaded', 'zzother', 'ingredient', 'mild', "
            "'loaded row', 'ddinter')")
    interaction_db.seed_builtin_interactions()
    with db_session() as db:
        terms = {(r["term_a"], r["term_b"]) for r in db.execute(
            "SELECT term_a, term_b FROM drug_interactions").fetchall()}
    assert ("clopidogrel", "proton_pump_inhibitors") not in terms
    assert ("zzloaded", "zzother") in terms


def test_loaded_ingredient_rows_fire_through_the_name(new_patient):
    """A DDInter row stored as an ingredient phrase matches a charted name."""
    from scripts.load_interactions import load_file
    from pathlib import Path
    load_file(Path(__file__).resolve().parent.parent / "scripts" / "fixtures"
              / "ddinter_sample.csv")
    pid = new_patient()
    _chart(pid, "Isosorbide Mononitrate 20mg Tablet")
    alerts = check_interactions(pid, "Sildenafil 50mg")
    assert alerts and alerts[0]["severity"] == "severe"


def test_formulary_coverage_names_each_item():
    from services.helix_db import init_helix_db
    init_helix_db()
    with db_session() as db:
        db.execute("INSERT OR REPLACE INTO helix_medication_master (id, name, generic_name, "
                   "form, strength) VALUES ('COV-WARF', 'Warf 5', 'Warfarin', 'Tablet', '5mg')")
        db.execute("INSERT OR REPLACE INTO helix_medication_master (id, name, generic_name, "
                   "form, strength) VALUES ('COV-ODT', 'Emeset 4', 'Ondansetron', "
                   "'Tablet', '4mg')")
        db.execute("INSERT OR REPLACE INTO helix_medication_master (id, name, generic_name, "
                   "form, strength) VALUES ('COV-CTX', 'Monocef 1g', 'Ceftriaxone', "
                   "'Injection', '1g')")
    report = formulary_interaction_coverage()
    by_id = {i["id"]: i for i in report["items"]}
    assert by_id["COV-WARF"]["covered"] is True
    assert "warfarin" in by_id["COV-WARF"]["terms"]
    assert by_id["COV-ODT"]["covered"] is False
    assert by_id["COV-CTX"]["covered"] is False   # a class no row names
    assert report["covered"] + report["not_covered"] == report["total"]
