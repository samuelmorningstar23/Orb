"""Text on a screen must not send staff to a control that does not exist (F146).

Three texts did, each pointing at a step nobody could take:

  * the Relay escalation post said "acknowledge it in Vigil", and no screen can
    acknowledge a chain (the escalation board is API-only), so a ward went
    looking for a button that is not there while the chain kept climbing;
  * the Trust Center told the administrator to capture consent, walk the
    overdue list, log a Board notification and record a simulated breach, and
    none of those has an admin screen;
  * the ABDM duplicate refusal said to "resolve which patient it is", and the
    desk has no control to correct or withdraw an ABHA record.

Each now names the step a person can take today. The Helix stockout half of the
finding (it pointed at a restock step Orb does not have) is pinned in
test_helix_order_lifecycle.py.
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from database import db_session
from services import abdm_service as ab
from services import compliance_service
from services import escalation_service as esc
from services import ingestion_service
from services.vigil_service import compute_news2

# NEWS2 6: over the urgent-response threshold, so the default ladder opens the
# chain at tier 1 (ward nurse, 10 minutes).
DETERIORATING = dict(heart_rate=95, systolic_bp=120, respiratory_rate=22,
                     temperature=38.5, spo2=93)


def _news2(v):
    return compute_news2(v["heart_rate"], v["systolic_bp"], v["respiratory_rate"],
                         v["temperature"], v["spo2"], "alert", False)


# ── the Relay escalation post ────────────────────────────────────────────────
@pytest.fixture()
def posts(monkeypatch):
    """Every case-room post the chain writes, captured before it reaches Relay."""
    sent: list[str] = []
    monkeypatch.setattr(
        ingestion_service, "_persist_relay_alert",
        lambda _pid, content: (sent.append(content) or (False, None, None, None, None)))
    monkeypatch.setenv("ORB_ESCALATION_RELAY_NOTIFY", "1")
    monkeypatch.delenv("ORB_ESCALATION_TIERS", raising=False)
    monkeypatch.delenv("ORB_ESCALATION_NEWS2_THRESHOLD", raising=False)
    return sent


@pytest.fixture()
def patient():
    dept = f"F146 Ward {uuid.uuid4().hex[:8]}"
    pid = f"PATF146{uuid.uuid4().hex[:6].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT OR IGNORE INTO users (id, name, role, password_hash) "
            "VALUES ('F146ADMITTER', 'F146 Admitter', 'doctor', 'x')")
        db.execute(
            "INSERT INTO patients (id, name, mrn, bed, ward, age, sex, diagnosis, status) "
            "VALUES (?, 'F146 Patient', ?, 'F-1', 'FW', 70, 'F', 'Pneumonia', 'admitted')",
            (pid, f"MRN-{uuid.uuid4().hex[:8]}"))
        db.execute(
            "INSERT INTO admissions (patient_id, department, status, admitted_by) "
            "VALUES (?, ?, 'active', 'F146ADMITTER')", (pid, dept))
    yield pid
    with db_session() as db:
        db.execute("DELETE FROM escalation_events WHERE escalation_id IN "
                   "(SELECT id FROM clinical_escalations WHERE patient_id = ?)", (pid,))
        db.execute("DELETE FROM clinical_escalations WHERE patient_id = ?", (pid,))
        db.execute("UPDATE admissions SET status = 'discharged' WHERE patient_id = ?", (pid,))
        db.execute("UPDATE patients SET status = 'discharged' WHERE id = ?", (pid,))


def test_the_escalation_post_names_what_a_reader_can_do(patient, posts):
    chain = esc.evaluate_observation(patient, _news2(DETERIORATING), notify=True)
    assert chain["current_tier"] == 1
    mine = [p for p in posts if chain["id"] in p]
    assert len(mine) == 1, posts
    post = mine[0]
    assert "in Vigil" not in post, post
    assert "acknowledges it" not in post, post
    assert "Orb does not page or phone anyone" in post
    assert "if not, phone them" in post
    # Tier 1 has a timeout, so the next tier is real.
    assert "moves to the next tier automatically" in post
    assert "—" not in post


def test_the_last_tier_post_does_not_promise_a_next_tier(patient, posts):
    raised_at = datetime.now(timezone.utc) - timedelta(hours=2)
    chain = esc.evaluate_observation(patient, _news2(DETERIORATING),
                                     now=raised_at, notify=False)
    assert chain["current_tier"] == 1

    moved = esc.advance_due(now=datetime.now(timezone.utc))
    climbed = next(m for m in moved if m["id"] == chain["id"])
    assert climbed["current_tier"] == 3 and climbed["tier_due_at"] is None

    mine = [p for p in posts if chain["id"] in p]
    assert len(mine) == 1, posts
    post = mine[0]
    assert "[TIER 3:" in post
    assert "It is at the last tier, so it does not move on." in post
    assert "next tier" not in post, post
    assert "in Vigil" not in post


# ── the Trust Center ─────────────────────────────────────────────────────────
_DEAD_ENDS = (
    "Capture consent at registration",
    "Start capturing consent",
    "walk the overdue list monthly",
    "record each notification against the breach",
    "record a simulated breach",
)


def _dpdp_controls(dpdp: dict) -> dict:
    controls = compliance_service._build_controls(
        {}, {}, {}, {}, {}, {}, False, dpdp)
    return {c["id"]: c for c in controls if c["id"].startswith("dpdp_")}


@pytest.mark.parametrize("dpdp", [
    # Nothing recorded yet: consent and breach register are amber.
    {"consents_recorded": 0, "retention_policies": 14,
     "breaches_recorded": 0, "breaches_awaiting_board_notification": 0},
    # Consents on file, a breach the Board has not been told about.
    {"consents_recorded": 3, "patients_with_consent": 2, "consents_withdrawn": 1,
     "retention_policies": 14, "breaches_recorded": 2,
     "breaches_awaiting_board_notification": 1},
], ids=["empty-registers", "unnotified-breach"])
def test_dpdp_remediation_names_a_step_that_exists(dpdp):
    controls = _dpdp_controls(dpdp)
    assert set(controls) == {"dpdp_consent", "dpdp_data_principal_rights",
                             "dpdp_retention", "dpdp_breach_register"}
    for cid, control in controls.items():
        for phrase in _DEAD_ENDS:
            assert phrase not in control["remediation"], (cid, control["remediation"])
        # Each of these points at machinery with no admin screen, and says so.
        assert "Orb has no screen for" in control["remediation"], (cid, control["remediation"])
        assert "—" not in control["remediation"]


def test_a_fully_notified_breach_register_needs_no_screen_caveat():
    """The pass branch asks for a step outside Orb, so it carries no caveat."""
    controls = _dpdp_controls({"consents_recorded": 1, "retention_policies": 14,
                               "breaches_recorded": 1,
                               "breaches_awaiting_board_notification": 0})
    breach = controls["dpdp_breach_register"]
    assert breach["status"] == "pass"
    assert "Orb has no screen" not in breach["remediation"]
    assert "DPDP s.8(6) requires both" in breach["remediation"]


# ── the ABDM duplicate refusal ───────────────────────────────────────────────
def _abdm_patient() -> str:
    pid = f"F146-ABDM-{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT OR IGNORE INTO users (id, name, role, password_hash) "
            "VALUES ('F146-DESK', 'F146 Desk', 'admin', 'x')")
        db.execute(
            "INSERT INTO patients (id, name, mrn, ward, bed, status) "
            "VALUES (?, 'F146 ABDM Patient', ?, 'W1', '1', 'admitted')",
            (pid, f"MRN-{pid}"))
    return pid


def test_the_duplicate_abha_refusal_points_at_a_step_that_exists():
    first, second = _abdm_patient(), _abdm_patient()
    number = f"7{uuid.uuid4().int % 10**13:013d}"
    ab.record_abha(patient_id=first, abha_number=number, recorded_by="F146-DESK")
    with pytest.raises(ab.IdentityError, match="different patient") as caught:
        ab.record_abha(patient_id=second, abha_number=number, recorded_by="F146-DESK")
    message = str(caught.value)
    assert "Resolve which patient" not in message, message
    assert "Check the number against the patient's ABHA card" in message
    assert "this screen cannot correct or withdraw it yet: contact Orb support" in message
    assert "—" not in message
