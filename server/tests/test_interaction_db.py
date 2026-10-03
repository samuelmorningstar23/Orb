"""Data-backed drug-interaction store: seeding, symmetric lookup, bulk loaders,
and end-to-end integration with the order-time safety gate.
"""
import uuid
from pathlib import Path

import pytest

from database import db_session
from services import interaction_db
from services.drug_safety_service import check_medication_safety, check_interactions

SERVER_DIR = Path(__file__).resolve().parent.parent
FIXTURES = SERVER_DIR / "scripts" / "fixtures"


@pytest.fixture()
def patient():
    pid = f"PATIX{uuid.uuid4().hex[:6].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO patients (id, name, mrn) VALUES (?, ?, ?)",
            (pid, "Interaction Test", f"MRN-{uuid.uuid4().hex[:6]}"),
        )
    return pid


def _add_active_med(pid, drug):
    with db_session() as db:
        db.execute(
            "INSERT INTO medications (patient_id, drug_name, dosage, route, frequency, status) "
            "VALUES (?,?,?,?,?, 'active')",
            (pid, drug, "1", "Oral", "OD"),
        )


# ── seeding ──────────────────────────────────────────────────────────────────
def test_seed_is_idempotent_and_large():
    interaction_db.seed_builtin_interactions()
    n1 = interaction_db.interaction_count()
    assert n1 >= 40, f"expected >=40 curated interactions, got {n1}"
    # Re-seeding must not duplicate (UNIQUE + upsert).
    interaction_db.seed_builtin_interactions()
    assert interaction_db.interaction_count() == n1


# ── symmetric lookup ─────────────────────────────────────────────────────────
def test_lookup_finds_warfarin_nsaid_severe():
    interaction_db.seed_builtin_interactions()
    # class-level anticoagulant/NSAID pair (what classify_drug produces)
    hits = interaction_db.lookup_interactions({"anticoagulants"}, {"nsaids"})
    assert any(h["severity"] == "severe" for h in hits)
    # ingredient-level warfarin<->nsaids pair, matched in the reverse column order
    rev = interaction_db.lookup_interactions({"nsaids"}, {"warfarin"})
    assert any(h["severity"] == "severe" for h in rev)


def test_lookup_is_symmetric():
    interaction_db.seed_builtin_interactions()
    fwd = interaction_db.lookup_interactions({"opioids"}, {"benzodiazepines"})
    bwd = interaction_db.lookup_interactions({"benzodiazepines"}, {"opioids"})
    assert fwd and bwd
    assert {(h["term_a"], h["term_b"]) for h in fwd} == {(h["term_a"], h["term_b"]) for h in bwd}


def test_lookup_empty_for_unrelated():
    interaction_db.seed_builtin_interactions()
    assert interaction_db.lookup_interactions({"penicillins"}, set()) == []
    assert interaction_db.lookup_interactions({"paracetamol_only"}, {"vitamin_c"}) == []


# ── loaders ──────────────────────────────────────────────────────────────────
def test_ddinter_loader_adds_rows():
    from scripts.load_interactions import load_file
    before = interaction_db.interaction_count()
    read, upserted = load_file(FIXTURES / "ddinter_sample.csv", source="ddinter")
    assert read == upserted == 8
    assert interaction_db.interaction_count() >= before
    # a loaded ingredient pair is queryable
    hits = interaction_db.lookup_interactions({"ciprofloxacin"}, {"theophylline"})
    assert any(h["severity"] == "severe" for h in hits)
    with db_session() as db:
        n = db.execute(
            "SELECT COUNT(*) FROM drug_interactions WHERE source='ddinter'"
        ).fetchone()[0]
    assert n >= 8


def test_rxnorm_formulary_loader_upserts():
    from services.helix_db import init_helix_db
    init_helix_db()  # ensure helix_medication_master exists in the test DB
    from scripts.load_rxnorm_formulary import load_file
    read, upserted = load_file(FIXTURES / "rxnorm_sample.csv")
    assert read == upserted == 6
    with db_session() as db:
        row = db.execute(
            "SELECT name, generic_name FROM helix_medication_master WHERE id='RX-855332'"
        ).fetchone()
    assert row is not None
    assert row["generic_name"] == "Warfarin"
    # idempotent: second load does not error or duplicate the id
    load_file(FIXTURES / "rxnorm_sample.csv")
    with db_session() as db:
        cnt = db.execute(
            "SELECT COUNT(*) FROM helix_medication_master WHERE id='RX-855332'"
        ).fetchone()[0]
    assert cnt == 1


# ── integration with the safety gate ─────────────────────────────────────────
def test_lazy_seed_via_check_interactions(patient):
    # Even without an explicit seed call, the gate seeds on first use.
    _add_active_med(patient, "warfarin")
    alerts = check_interactions(patient, "ibuprofen")
    assert any(a["severity"] == "severe" for a in alerts)
    assert interaction_db.interaction_count() >= 40


def test_check_medication_safety_blocks_warfarin_nsaid(patient):
    _add_active_med(patient, "warfarin")
    result = check_medication_safety(patient, "ibuprofen")
    assert result["blocking"] is True
    assert result["max_severity"] == "severe"
    assert any(i["type"] == "interaction" and i["severity"] == "severe"
               for i in result["interaction_alerts"])


def test_check_medication_safety_moderate_warns_not_blocks(patient):
    # ACEi + NSAID is moderate: an alert fires but does not block.
    _add_active_med(patient, "ramipril")
    result = check_medication_safety(patient, "ibuprofen")
    assert result["interaction_alerts"]
    assert result["max_severity"] == "moderate"
    assert result["blocking"] is False
