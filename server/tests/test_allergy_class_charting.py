"""An allergy charted as a CLASS must screen like one.

THE DEFECT THIS PINS (found 2026-08-02, by probing the matcher rather than by a
test — every existing test used a drug name as the allergen, so the whole class
of failure was invisible).

`match_allergies` classified the allergen with `classify_drug`, i.e. by asking
"what class is this DRUG in". That works whenever the charted allergen happens to
also be a drug name: "Penicillin" is both a substance and a class, so a penicillin
allergy correctly flagged amoxicillin, and the interlock looked sound.

It silently failed for classes that are not drug names. An allergy charted as
**"NSAIDs"** — which is how an NSAID allergy is normally recorded — classified as
nothing at all, so ibuprofen, diclofenac and naproxen all screened CLEAN for a
patient documented as allergic to them.

Both directions are asserted, and the second matters as much as the first: an
interlock that fires on everything trains staff to click through, and alert
fatigue is a documented patient-safety hazard. A too-eager allergy check is not a
safer allergy check.
"""
import pytest

from services.drug_safety_service import classify_allergen, match_allergies


def _allergy(substance, severity="severe"):
    return [{"substance": substance, "severity": severity, "status": "active"}]


# (charted allergen, drug being given) — every one of these is a real charting
# style, and every one must raise an alert.
MUST_FIRE = [
    ("NSAIDs", "Ibuprofen"),
    ("NSAID", "Diclofenac"),
    ("nsaids", "Naproxen"),
    ("non-steroidal anti-inflammatory", "Ibuprofen"),
    ("Sulfa", "Co-trimoxazole"),
    ("Sulphonamides", "Sulfamethoxazole"),
    ("Opiates", "Morphine"),
    ("Opioid", "Oxycodone"),
    ("Statins", "Atorvastatin"),
    ("ACE inhibitors", "Ramipril"),
    ("ace-inhibitor", "Lisinopril"),
    ("Macrolides", "Clarithromycin"),
    ("Aminoglycosides", "Gentamicin"),
    ("Carbapenems", "Meropenem"),
    # Substance-charted allergies must keep working — this is the behaviour that
    # already existed and that the fix must not disturb.
    ("Penicillin", "Amoxicillin"),
    ("Penicillin", "Flucloxacillin"),
    ("Ibuprofen", "Naproxen"),
    # Cross-reactivity survives class-charting on the allergen side.
    ("Penicillin", "Ceftriaxone"),
]

# The other half of the contract. Each is a plausible near-miss.
MUST_NOT_FIRE = [
    ("NSAIDs", "Paracetamol"),        # the classic "safe alternative" — must stay clean
    ("NSAIDs", "Metoprolol"),
    ("Penicillin", "Paracetamol"),
    ("Statins", "Ramipril"),
    ("Opiates", "Ibuprofen"),
    ("Macrolides", "Amoxicillin"),
    # Word-boundary probes: substring matching would fire on all three.
    ("face cream", "Ramipril"),        # "ace" inside "face"
    ("latex", "Atorvastatin"),
    ("Dust", "Ibuprofen"),
]


@pytest.mark.parametrize("charted,drug", MUST_FIRE, ids=[f"{a}+{d}" for a, d in MUST_FIRE])
def test_a_class_charted_allergy_flags_its_members(charted, drug):
    alerts = match_allergies(_allergy(charted), drug)
    assert alerts, (
        f"{drug} screened CLEAN for a patient charted allergic to {charted!r}. "
        "This is the exact shape of the 2026-08-02 defect: an allergen charted as "
        "a class matched nothing, so every member of that class passed the "
        "administration interlock."
    )


@pytest.mark.parametrize("charted,drug", MUST_NOT_FIRE, ids=[f"{a}+{d}" for a, d in MUST_NOT_FIRE])
def test_unrelated_drugs_do_not_trip_the_interlock(charted, drug):
    alerts = match_allergies(_allergy(charted), drug)
    assert not alerts, (
        f"{drug} raised an allergy alert against {charted!r}. Over-flagging is a "
        "safety hazard in its own right — an interlock that fires on everything "
        f"gets clicked through. Alerts raised: {alerts}"
    )


def test_classify_allergen_unions_both_readings():
    """It must read the allergen BOTH ways — as a substance and as a class —
    because charting practice mixes them in the same column."""
    assert "nsaids" in classify_allergen("NSAIDs")          # class term only
    assert "nsaids" in classify_allergen("Ibuprofen")       # substance only
    assert "penicillins" in classify_allergen("Penicillin")  # both readings agree
    assert classify_allergen("") == []
    assert classify_allergen("Shellfish") == []              # not a drug class at all


def test_severity_survives_a_class_match():
    """A class match is still a real allergy — it must not be quietly downgraded
    relative to a direct name match."""
    direct = match_allergies(_allergy("Ibuprofen"), "Ibuprofen")
    via_class = match_allergies(_allergy("NSAIDs"), "Ibuprofen")
    assert direct and via_class
    assert via_class[0]["severity"] == direct[0]["severity"] == "severe"
