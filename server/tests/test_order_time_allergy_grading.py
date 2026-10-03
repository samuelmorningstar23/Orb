"""The order-time allergy screen never grades a match lower than the bedside does.

Found 2026-10-02, ruled 2026-10-03 ("fix it all"): the order-time gate took a
cross-reactivity grade from a fixed table (penicillins against cephalosporins
= 'moderate') and blocks only on 'severe', whatever reaction was documented.
The bedside interlock reads the reaction text and treats anaphylaxis as severe.
So every ORDER lane charted ceftriaxone for a patient with a documented
penicillin anaphylaxis and only eMAR stopped it, at the moment of giving it.
A direct match had the same hole: a 'mild' severity typed next to an
anaphylaxis reaction let the drug itself through at order time.

Both screens now share one severity policy (drug_safety_service.
effective_severity). The invariant pinned here: whenever the bedside would
BLOCK a dose, the order-time screen already blocks the order.
"""
import uuid

import pytest
from fastapi.testclient import TestClient

from auth import hash_password
from database import db_session
from main import app
from services import allergy_interlock
from services.drug_safety_service import check_medication_safety, match_allergies
from services.clinical_records_service import add_allergy
from services.sage_handoff_service import HandoffBlocked, _dispatch_to_helix

client = TestClient(app)


def _allergy(substance, reaction=None, severity="unknown"):
    return [{"id": 1, "substance": substance, "substance_class": None,
             "reaction": reaction, "severity": severity}]


# ── the pure matcher ────────────────────────────────────────────────────────
def test_penicillin_anaphylaxis_makes_a_cephalosporin_severe():
    alerts = match_allergies(_allergy("penicillin", "anaphylaxis", "severe"), "Ceftriaxone 1g")
    assert [(a["match"], a["severity"]) for a in alerts] == [("cross_reactivity", "severe")]


def test_a_moderate_rash_stays_moderate():
    """Not over-blocking: ~1-3% cross-reactivity with a rash history is a
    warning, as it always was."""
    alerts = match_allergies(_allergy("penicillin", "rash", "moderate"), "Ceftriaxone 1g")
    assert [(a["match"], a["severity"]) for a in alerts] == [("cross_reactivity", "moderate")]


def test_anaphylaxis_typed_as_mild_is_believed_as_anaphylaxis():
    alerts = match_allergies(_allergy("amoxicillin", "anaphylaxis", "mild"), "Amoxicillin 500mg")
    assert alerts[0]["match"] == "direct" and alerts[0]["severity"] == "severe"


def test_cross_reactivity_message_names_the_reaction():
    alerts = match_allergies(_allergy("penicillin", "anaphylaxis", "severe"), "Ceftriaxone 1g")
    assert "anaphylaxis" in alerts[0]["message"].lower()


# Every combination the bedside can see; the order screen must block at least
# whatever the bedside blocks.
CASES = [
    (sub, reaction, sev, drug)
    for sub in ("penicillin", "amoxicillin", "ceftriaxone", "cephalosporins", "meropenem")
    for reaction in (None, "anaphylaxis", "rash", "nausea", "rash and nausea", "angioedema")
    for sev in ("unknown", "mild", "moderate", "severe")
    for drug in ("Ceftriaxone 1g", "Amoxicillin 500mg", "Meropenem 1g", "Cefuroxime 750mg")
]


@pytest.mark.parametrize("sub,reaction,sev,drug", CASES)
def test_bedside_block_implies_order_time_block(sub, reaction, sev, drug):
    allergies = _allergy(sub, reaction, sev)
    bedside = allergy_interlock.evaluate(allergies, drug)
    order_alerts = match_allergies(allergies, drug)
    order_blocks = any(a["severity"] == "severe" for a in order_alerts)
    if bedside["decision"] == allergy_interlock.BLOCK:
        assert order_blocks, (
            f"bedside blocks {drug} for {sub}/{reaction}/{sev} but the order screen "
            f"graded it {[a['severity'] for a in order_alerts]}"
        )


# ── through the real gate and lanes ─────────────────────────────────────────
@pytest.fixture()
def patient():
    pid = f"PATOTG{uuid.uuid4().hex[:6].upper()}"
    with db_session() as db:
        db.execute("INSERT INTO patients (id, name, mrn) VALUES (?, ?, ?)",
                   (pid, "Order-time Grading", f"MRN-{pid}"))
    yield pid
    with db_session() as db:
        db.execute("UPDATE patients SET status = 'discharged' WHERE id = ?", (pid,))


def _charted(pid, drug):
    with db_session() as db:
        return db.execute("SELECT count(*) FROM medications WHERE patient_id = ? AND drug_name = ?",
                          (pid, drug)).fetchone()[0]


def test_gate_blocks_a_cephalosporin_for_penicillin_anaphylaxis(patient):
    add_allergy(patient, "penicillin", reaction="anaphylaxis", severity="severe")
    safety = check_medication_safety(patient, "Ceftriaxone 1g")
    assert safety["blocking"] is True and safety["max_severity"] == "severe"


def test_vigil_order_returns_428_for_penicillin_anaphylaxis(patient):
    """The interactive lane: the prescriber must acknowledge with a reason."""
    uid = f"DOTG{uuid.uuid4().hex[:5].upper()}"
    with db_session() as db:
        db.execute("INSERT OR IGNORE INTO users (id, name, role, password_hash) VALUES (?,?,?,?)",
                   (uid, f"Dr {uid}", "doctor", hash_password("secretpass")))
    token = client.post("/api/auth/login", json={"employee_id": uid, "password": "secretpass"}).json()["token"]
    add_allergy(patient, "penicillin", reaction="anaphylaxis", severity="severe")
    resp = client.post(f"/api/vigil/patients/{patient}/medications",
                       json={"drug_name": "Ceftriaxone 1g", "dosage": "1g", "frequency": "OD"},
                       headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 428, resp.text
    assert _charted(patient, "Ceftriaxone 1g") == 0


def test_agentic_lane_blocks_it_too(patient):
    """The no-human lane (a confirmed Sage card): blocked, nothing charted."""
    add_allergy(patient, "penicillin", reaction="anaphylaxis", severity="severe")
    with pytest.raises(HandoffBlocked) as blocked:
        _dispatch_to_helix({"patient_id": patient, "target": "Ceftriaxone 1g", "rationale": "start iv abx"},
                           actor_id="D001")
    assert "anaphylaxis" in blocked.value.reason.lower()
    assert _charted(patient, "Ceftriaxone 1g") == 0
