"""WHO may give a drug across a charted allergy — and who may not.

THE STATE BEFORE THIS FILE, and why both halves of it were wrong:

  * The interlock blocked correctly (tests/test_allergy_interlock.py) but the
    428 was a dead end from every screen — `AdministerPayload` had no
    `allergy_override_reason` field and no modal offered one. Suresh Reddy's
    Ceftriaxone could not be given through Orb by ANYONE, ever. Safe by default,
    but nobody decided it, and real hospitals do give a drug across a charted
    allergy under supervision: a desensitisation step, an allergy de-labelled
    after formal testing, a risk/benefit call in an emergency.
  * The server, meanwhile, accepted the reason from ANY holder of
    `medication.write` — which is every nurse, because that same permission is
    what lets her record a dose at all. Unreachable through the UI, but it was
    the server's actual answer, and "no client sends that field" is not a
    control.

So the ruling: prescriber only, with a written reason. These cases assert the
invariants that ruling turns into, and each is written to FAIL if the gate is
removed rather than to pass because nothing exercises it:

  1. A NURSE IS REFUSED, and the refusal is recorded. (The load-bearing one.)
  2. A PRESCRIBER may, with a real reason — and may not without one.
  3. The audit row names the actor, the allergen, the drug and the reason.
  4. A dose given under an override is MARKED on the record afterwards.
  5. THE INTERLOCK STILL BLOCKS BY DEFAULT, for everyone, when no override is
     supplied. Asserted separately and last, because it is the property every
     other case here assumes and the one a future UI change could silently
     destroy: if the override became the default path, cases 1–4 would all still
     pass.
"""
import json
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import permissions
from auth import create_token
from database import db_session, verify_audit_chain
from routes.emar_routes import router as emar_router
from services import allergy_interlock, emar_service
from services.allergy_interlock import (
    BLOCK,
    AllergyInterlockBlocked,
    AllergyOverrideNotPermitted,
)
from services.clinical_records_service import add_allergy

# Suresh Reddy's dose, which is the case that prompted all of this: an active
# Ceftriaxone 2g IV order against a charted SEVERE ceftriaxone anaphylaxis.
DRUG = "Ceftriaxone"
GOOD_REASON = (
    "Allergy de-labelled by immunology 12/08 after negative skin test and "
    "graded oral challenge; giving under 1:1 observation."
)


# ── fixtures ─────────────────────────────────────────────────────────────────
@pytest.fixture()
def patient():
    """Discharged on teardown, never deleted — audit and allergy rows written
    during the test legitimately reference this id. Same isolation law as
    tests/test_allergy_interlock.py, and for the same reason: patients.status
    defaults to 'admitted', so a leaked row keeps costing every later test that
    iterates admitted patients."""
    pid = f"PATOVR{uuid.uuid4().hex[:6].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO patients (id, name, mrn) VALUES (?, ?, ?)",
            (pid, "Override Test", f"MRN-{uuid.uuid4().hex[:6]}"),
        )
    yield pid
    with db_session() as db:
        db.execute("UPDATE patients SET status = 'discharged' WHERE id = ?", (pid,))
        db.execute(
            "UPDATE admissions SET status = 'discharged' "
            "WHERE patient_id = ? AND status = 'active'",
            (pid,),
        )


def _user(prefix: str, role: str, name: str) -> str:
    uid = f"{prefix}{uuid.uuid4().hex[:5].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT OR IGNORE INTO users (id, name, role, password_hash) VALUES (?, ?, ?, 'x')",
            (uid, name, role),
        )
    return uid


@pytest.fixture()
def nurse():
    return _user("NUROVR", "nurse", "Nurse Bedside")


@pytest.fixture()
def doctor():
    return _user("DROVR", "doctor", "Dr Prescriber")


@pytest.fixture()
def client():
    app = FastAPI()
    app.include_router(emar_router)
    return TestClient(app)


def _auth(uid, role):
    return {"Authorization": f"Bearer {create_token(uid, role)}"}


def _chart_med(patient_id, drug=DRUG, dosage="2 g", route="IV", frequency="OD"):
    with db_session() as db:
        cur = db.execute(
            """INSERT INTO medications (patient_id, drug_name, dosage, route, frequency,
                                        status, prescribed_by, source)
               VALUES (?, ?, ?, ?, ?, 'active', 'D001', 'clinician')""",
            (patient_id, drug, dosage, route, frequency),
        )
        return cur.lastrowid


def _charted_anaphylaxis(patient_id, substance=DRUG):
    add_allergy(patient_id, substance,
                reaction="urticaria, angio-oedema, hypotension (2023)",
                severity="severe")


def _rows(table, med_id):
    with db_session() as db:
        return [dict(r) for r in db.execute(
            f"SELECT * FROM {table} WHERE medication_id = ? ORDER BY id", (med_id,)
        ).fetchall()]


def _audits(action_type, patient_id):
    with db_session() as db:
        return [dict(r) for r in db.execute(
            "SELECT * FROM audit_logs WHERE action_type = ? AND patient_id = ? ORDER BY id",
            (action_type, patient_id),
        ).fetchall()]


# ═════════════════════════════════════════════════════════════════════════════
# 1. THE NURSE IS REFUSED — the load-bearing case
# ═════════════════════════════════════════════════════════════════════════════
def test_a_nurse_cannot_override_a_charted_allergy(patient, nurse):
    """She is the LAST INDEPENDENT CHECK on the prescriber's decision. An account
    that can both question the decision and overrule it is one check wearing two
    hats.

    PROVEN ABLE TO FAIL: this is the assertion that breaks the moment the gate is
    deleted — before 2026-08-15 this exact call recorded the dose and returned a
    200-shaped record. It is not asserting a refusal that some other guard would
    have produced anyway: the same call with the same reason from a prescriber
    succeeds two cases below, so the ONLY difference under test is the actor.
    """
    _charted_anaphylaxis(patient)
    med_id = _chart_med(patient)

    with pytest.raises(AllergyOverrideNotPermitted) as ei:
        emar_service.record_administration(
            med_id, nurse, status="given", allergy_override_reason=GOOD_REASON,
        )

    assert ei.value.actor_role == "nurse"
    assert ei.value.required_permission == "medication.prescribe"
    # The message tells her what is true and what to do — not "forbidden".
    assert "prescriber" in str(ei.value).lower()
    assert "NOT been recorded" in str(ei.value)

    # NOTHING happened: no dose, no ledger row, no half-written state.
    assert _rows("medication_administrations", med_id) == []
    assert _rows("medication_allergy_overrides", med_id) == []


def test_the_refusal_survives_a_perfect_reason(patient, nurse):
    """The gate is on WHO, not on how well it is argued. A nurse typing the
    clinically correct justification is still refused — otherwise the control is
    just a writing test."""
    _charted_anaphylaxis(patient)
    med_id = _chart_med(patient)
    for reason in (GOOD_REASON,
                   "Consultant Dr Rao instructed by phone; desensitisation step 4 of 6.",
                   "Immunology cleared 11/08, negative challenge, patient consented."):
        with pytest.raises(AllergyOverrideNotPermitted):
            emar_service.record_administration(
                med_id, nurse, status="given", allergy_override_reason=reason,
            )
    assert _rows("medication_administrations", med_id) == []


def test_an_unknown_or_missing_actor_may_not_override(patient):
    """FAIL CLOSED on identity. A user id with no row, and no actor at all, both
    resolve to "no authority" — not to "unchecked". A caller that cannot be
    identified is the last one who should be clearing an anaphylaxis interlock.
    """
    _charted_anaphylaxis(patient)
    med_id = _chart_med(patient)

    with pytest.raises(AllergyOverrideNotPermitted):
        emar_service.record_administration(
            med_id, "GHOST-USER-NOT-IN-DB", status="given",
            allergy_override_reason=GOOD_REASON,
        )
    # And directly at the screen, with no actor supplied at all.
    with db_session() as db:
        with pytest.raises(AllergyOverrideNotPermitted):
            allergy_interlock.screen_for_administration(
                db, patient, DRUG, med_id, override_reason=GOOD_REASON,
            )


def test_pharmacist_and_admin_are_refused_too(patient):
    """Not a nurse-shaped rule — a PERMISSION-shaped one. Nobody outside the
    prescribing grant clears it, including the accounts that hold more
    governance power than the nurse does."""
    _charted_anaphylaxis(patient)
    for role, prefix in (("pharmacist", "PHOVR"), ("admin", "ADMOVR")):
        med_id = _chart_med(patient)
        actor = _user(prefix, role, f"{role} override")
        with pytest.raises(AllergyOverrideNotPermitted) as ei:
            emar_service.record_administration(
                med_id, actor, status="given", allergy_override_reason=GOOD_REASON,
            )
        assert ei.value.actor_role == role
        assert _rows("medication_administrations", med_id) == []


def test_the_gate_is_the_permission_not_the_role_name(patient, monkeypatch):
    """The rule is `medication.prescribe`, and it MOVES WITH THE GRANT — which is
    the whole reason it is a permission and not a `role == "doctor"` test.

    Both directions are asserted, because a role-name gate would pass one and
    fail the other. Granting the permission to a role that does not have it
    (here `pharmacist`, standing in for the site that licenses a prescribing
    pharmacist or nurse practitioner) admits that account; REVOKING it from the
    doctor refuses him, name notwithstanding.

    An invented role is deliberately NOT used: `users.role` carries a CHECK
    constraint (database.py) limiting it to five values, so a site adding a new
    prescribing role needs a schema migration as well as a grant. The permission
    layer is ready for it; the schema is not, and pretending otherwise in a test
    would hide that.
    """
    _charted_anaphylaxis(patient)
    monkeypatch.setitem(
        permissions.ROLE_PERMISSIONS, "pharmacist",
        set(permissions.ROLE_PERMISSIONS["pharmacist"]) | {permissions.MEDICATION_PRESCRIBE},
    )
    prescribing_pharmacist = _user("RXOVR", "pharmacist", "Prescribing Pharmacist")
    rec = emar_service.record_administration(
        _chart_med(patient), prescribing_pharmacist, status="given",
        allergy_override_reason=GOOD_REASON,
    )
    assert rec["allergy_check"]["override"]["reason"] == GOOD_REASON
    assert rec["allergy_check"]["override"]["carried"] is False

    # …and the doctor stripped of the grant loses it.
    stripped = set(permissions.ROLE_PERMISSIONS["doctor"]) - {permissions.MEDICATION_PRESCRIBE}
    monkeypatch.setitem(permissions.ROLE_PERMISSIONS, "doctor", stripped)
    with pytest.raises(AllergyOverrideNotPermitted):
        emar_service.record_administration(
            _chart_med(patient), _user("DRSTRIP", "doctor", "Dr Stripped"),
            status="given", allergy_override_reason=GOOD_REASON,
        )


def test_route_answers_the_nurse_with_403_and_records_the_attempt(patient, nurse, client):
    """OVER HTTP, and audited.

    403, not 428: 428 is this API's "you may do this, but say why", and answering
    her with it would invite her to keep typing reasons at a gate that will never
    open for her. The body still carries the allergy screen so the bedside screen
    can name the allergen and who can authorise it.
    """
    _charted_anaphylaxis(patient)
    med_id = _chart_med(patient)

    resp = client.post(
        f"/api/emar/medications/{med_id}/administer",
        json={"status": "given", "allergy_override_reason": GOOD_REASON},
        headers=_auth(nurse, "nurse"),
    )
    assert resp.status_code == 403, resp.text
    detail = resp.json()["detail"]
    assert detail["error"] == "allergy_override_not_permitted"
    assert detail["required_permission"] == "medication.prescribe"
    # The screen travels with the refusal — the nurse still learns WHY.
    assert detail["allergy"]["decision"] == BLOCK
    assert detail["allergy"]["alerts"][0]["substance"].lower() == DRUG.lower()
    assert detail["allergy"]["override_requires_permission"] == "medication.prescribe"

    assert emar_service.list_administrations(patient) == []

    # The ATTEMPT is on the fail-closed chain: an incident review asking "did
    # anyone try to give this?" gets an answer, including the reason she typed.
    refusals = _audits("medication_allergy_override_refused", patient)
    assert len(refusals) == 1
    assert refusals[0]["event_category"] == "security"
    assert refusals[0]["outcome"] == "denied"
    assert refusals[0]["actor_user_id"] == nurse
    assert refusals[0]["actor_role"] == "nurse"
    meta = json.loads(refusals[0]["metadata_json"])
    assert meta["allergens"] == [DRUG.lower()]
    assert meta["drug_name"] == DRUG
    assert meta["attempted_reason"] == GOOD_REASON
    assert verify_audit_chain()["ok"]


# ═════════════════════════════════════════════════════════════════════════════
# 2. THE PRESCRIBER MAY — with a reason, and only with one
# ═════════════════════════════════════════════════════════════════════════════
def test_a_prescriber_may_override_with_a_written_reason(patient, doctor):
    _charted_anaphylaxis(patient)
    med_id = _chart_med(patient)
    rec = emar_service.record_administration(
        med_id, doctor, status="given", allergy_override_reason=GOOD_REASON,
    )
    assert rec["status"] == "given"
    assert rec["allergy_check"]["decision"] == BLOCK      # still a block…
    assert rec["allergy_check"]["override"]["reason"] == GOOD_REASON   # …that was cleared
    assert rec["allergy_check"]["override"]["carried"] is False


@pytest.mark.parametrize("bad,why", [
    (None, "no reason at all"),
    ("", "an empty string"),
    ("   ", "whitespace"),
    ("ok", "a keystroke"),
    ("per doctor", "a known non-reason"),
    ("aaaaaaaaaaaa", "one repeated character"),
    ("urgent", "a word, not a justification — under the length floor"),
])
def test_a_prescriber_without_a_real_reason_is_still_blocked(patient, doctor, bad, why):
    """The reason is the point. Being a prescriber is permission to make the
    decision, not permission to skip recording it — the row is what a colleague
    reads in an incident review.

    Enforced SERVER-SIDE (allergy_interlock.validate_override_reason), which is
    why this is asserted here and not only in the modal: a client-side length
    check is a courtesy to the typist, not a control.
    """
    _charted_anaphylaxis(patient)
    med_id = _chart_med(patient)
    with pytest.raises(AllergyInterlockBlocked):
        emar_service.record_administration(
            med_id, doctor, status="given", allergy_override_reason=bad,
        )
    assert _rows("medication_administrations", med_id) == [], why
    assert _rows("medication_allergy_overrides", med_id) == []


def test_an_unreadable_allergy_list_is_not_prescriber_only(patient, nurse, monkeypatch):
    """THE DELIBERATE SEAM, asserted so it stays deliberate.

    A screen that FAILED is not a charted allergy being overruled — there is no
    clinical decision in front of anyone, only an outage. Restricting that to a
    prescriber would let an infrastructure fault stop a nurse giving adrenaline
    in a peri-arrest, which is a larger hazard than the one being guarded. So
    the nurse may clear it, and every instance is audited as screen_failed for
    review.
    """
    def _boom(db, pid):
        raise RuntimeError("patient_allergies unreadable (decryption failure)")

    med_id = _chart_med(patient, "Adrenaline", dosage="1mg", route="IV")
    monkeypatch.setattr(allergy_interlock, "read_active_allergies", _boom)
    rec = emar_service.record_administration(
        med_id, nurse, status="given",
        allergy_override_reason="Allergy store down; peri-arrest, giving per resus protocol.",
    )
    assert rec["allergy_check"]["screen_failed"] is True
    meta = json.loads(_audits("medication_allergy_override", patient)[-1]["metadata_json"])
    assert meta["screen_failed"] is True
    assert meta["authorised_by_role"] == "nurse"    # visible for review, not hidden


def test_a_nurse_may_give_a_later_dose_under_the_prescribers_carried_override(
    patient, nurse, doctor,
):
    """The prescriber decides ONCE per order per allergy picture; the nurse gives
    the course. A carried-forward reason is not a new decision, and requiring the
    prescriber at the bedside for all 28 doses of a QDS week would either stop
    the treatment or move it onto paper.

    What is NOT permitted stays not permitted: the nurse cannot start one (case 1
    above), and a changed allergy picture re-arms the block for her too."""
    _charted_anaphylaxis(patient)
    med_id = _chart_med(patient)
    emar_service.record_administration(
        med_id, doctor, status="given", allergy_override_reason=GOOD_REASON,
    )
    second = emar_service.record_administration(med_id, nurse, status="given")
    assert second["allergy_check"]["override"]["carried"] is True
    assert second["allergy_check"]["override"]["reason"] == GOOD_REASON

    # A NEW allergy changes the fingerprint → the carried decision no longer
    # covers this dose → the nurse is blocked again, not quietly carried.
    add_allergy(patient, "cefuroxime", reaction="anaphylaxis", severity="severe")
    with pytest.raises(AllergyInterlockBlocked):
        emar_service.record_administration(med_id, nurse, status="given")


# ═════════════════════════════════════════════════════════════════════════════
# 3. ATTRIBUTION — the row an incident review asks for
# ═════════════════════════════════════════════════════════════════════════════
def test_the_override_is_fully_attributed_on_the_ledger_and_the_chain(patient, doctor):
    """WHO, WHEN, WHY, WHICH ALLERGEN, WHICH DRUG — and readable cold, without
    joining tables that keep moving."""
    _charted_anaphylaxis(patient)
    med_id = _chart_med(patient)
    rec = emar_service.record_administration(
        med_id, doctor, status="given", allergy_override_reason=GOOD_REASON,
    )

    ledger = _rows("medication_allergy_overrides", med_id)
    assert len(ledger) == 1
    row = ledger[0]
    assert row["overridden_by"] == doctor            # who
    assert row["overridden_by_role"] == "doctor"     # …with what authority
    assert row["overridden_at"]                      # when
    assert row["reason"] == GOOD_REASON              # why
    assert row["drug_name"] == DRUG                  # what was given
    assert row["administration_id"] == rec["id"]     # which dose it authorised
    assert row["patient_id"] == patient
    assert json.loads(row["alerts_json"])[0]["substance"].lower() == DRUG.lower()

    audits = _audits("medication_allergy_override", patient)
    assert len(audits) == 1
    assert audits[0]["actor_user_id"] == doctor
    assert audits[0]["actor_role"] == "doctor"
    # Fail-closed category: the dose and the reason for it commit together, so
    # "the dose is charted but nobody knows why it was allowed" is unreachable.
    assert audits[0]["event_category"] == "clinical_access"
    # The summary line names the allergen. A review reads summaries first, and
    # "gave X despite the interlock" without naming the allergy has to be
    # re-derived before it can be understood.
    assert DRUG.lower() in audits[0]["summary_message"].lower()
    assert GOOD_REASON in audits[0]["summary_message"]
    meta = json.loads(audits[0]["metadata_json"])
    assert meta["allergens"] == [DRUG.lower()]
    assert meta["drug_name"] == DRUG
    assert meta["reason"] == GOOD_REASON
    assert meta["authorised_by_role"] == "doctor"
    assert meta["required_permission"] == "medication.prescribe"
    assert verify_audit_chain()["ok"]


# ═════════════════════════════════════════════════════════════════════════════
# 4. NEVER SILENT — the chart shows it, afterwards, to whoever reads it next
# ═════════════════════════════════════════════════════════════════════════════
def test_an_overridden_dose_is_marked_as_such_on_the_record(patient, doctor, nurse, client):
    """A safety event that leaves no trace on the clinical record is the failure
    mode this whole feature exists to avoid. The colleague reading the chart
    tomorrow must see that the drug was given ACROSS a charted allergy, and why,
    without going to the audit log for it."""
    _charted_anaphylaxis(patient)
    med_id = _chart_med(patient)
    emar_service.record_administration(
        med_id, doctor, status="given", allergy_override_reason=GOOD_REASON,
    )
    emar_service.record_administration(med_id, nurse, status="given")  # carried

    history = emar_service.list_administrations(patient)
    assert len(history) == 2
    for record in history:
        check = record["allergy_check"]
        assert check["decision"] == BLOCK
        assert check["override"]["reason"] == GOOD_REASON
        assert check["alerts"][0]["substance"].lower() == DRUG.lower()
        assert check["alerts"][0]["effective_severity"] == "severe"
    # …and the same thing over HTTP, which is what the eMAR actually renders.
    resp = client.get(f"/api/emar/patients/{patient}/administrations",
                      headers=_auth(doctor, "doctor"))
    assert resp.status_code == 200
    served = resp.json()["administrations"]
    assert all(a["allergy_check"]["override"] for a in served)
    # The two doses are distinguishable: one decision, one act of carrying it.
    assert [a["allergy_check"]["override"]["carried"] for a in served] == [True, False]


def test_a_clean_dose_is_not_marked_and_the_two_are_distinguishable(patient, nurse):
    """The marker has to MEAN something. A dose that screened clean carries no
    override, and a dose that was never screened carries no check at all —
    'checked and fine', 'given anyway' and 'never checked' are three different
    facts and the record keeps them apart."""
    _charted_anaphylaxis(patient)
    clean = emar_service.record_administration(
        _chart_med(patient, "Paracetamol", dosage="1 g", route="Oral", frequency="QDS"),
        nurse, status="given",
    )
    assert clean["allergy_check"]["decision"] == "allow"
    assert clean["allergy_check"]["override"] is None

    withheld = emar_service.record_administration(
        _chart_med(patient), nurse, status="not_given",
        not_given_reason="allergy — bleeped the prescriber",
    )
    assert withheld["allergy_check"] is None


# ═════════════════════════════════════════════════════════════════════════════
# 5. THE DEFAULT IS STILL A BLOCK — asserted separately, on purpose
# ═════════════════════════════════════════════════════════════════════════════
# Everything above assumes the interlock fires. If a future change made the
# override the default path — a UI that always sends a reason, a client that
# echoes one back, a service default flipped from None — every case above would
# still pass and the gate would be gone. So the base case gets its own
# assertions, with no override supplied by anyone.
@pytest.mark.parametrize("role,prefix", [
    ("nurse", "NURDEF"), ("doctor", "DRDEF"), ("admin", "ADMDEF"),
])
def test_no_override_supplied_means_blocked_for_everyone(patient, role, prefix):
    """Including the prescriber. Holding the permission is not the same as having
    used it: the reason is what clears the dose, and nobody gets a free pass for
    being entitled to give one."""
    _charted_anaphylaxis(patient)
    med_id = _chart_med(patient)
    actor = _user(prefix, role, f"{role} default")

    with pytest.raises(AllergyInterlockBlocked) as ei:
        emar_service.record_administration(med_id, actor, status="given")

    # And it is the PLAIN block, not the permission refusal — a prescriber who
    # simply pressed Given must be told what the allergy is, not that he lacks
    # authority he in fact has.
    assert not isinstance(ei.value, AllergyOverrideNotPermitted)
    assert ei.value.screen["decision"] == BLOCK
    assert _rows("medication_administrations", med_id) == []


def test_the_route_still_returns_428_with_no_reason_supplied(patient, doctor, nurse, client):
    """The unchanged contract, over HTTP, for both actors — this is the request
    the ward actually sends on every drug round."""
    _charted_anaphylaxis(patient)
    med_id = _chart_med(patient)
    for actor, role in ((nurse, "nurse"), (doctor, "doctor")):
        resp = client.post(f"/api/emar/medications/{med_id}/administer",
                           json={"status": "given"}, headers=_auth(actor, role))
        assert resp.status_code == 428, resp.text
        assert resp.json()["detail"]["error"] == "allergy_acknowledgement_required"
    assert emar_service.list_administrations(patient) == []


# ── 6. A carried override only carries if a PRESCRIBER authored it ───────────
# Found in a browser check on 2026-08-15, not by this suite: the gate above
# guards the WRITE lane, and `find_carried_override` reads the same ledger on
# the CARRY lane without asking who wrote the row. On the dev appliance override
# id 1 had been authored by a NURSE on 2026-08-13, before the gate existed — and
# a nurse gave ceftriaxone to a patient with charted anaphylaxis at 200 OK, no
# prescriber involved at any point. Cases 1–5 all passed while that was true,
# because every one of them exercises a FRESH reason.

def _plant_override(med_id, patient_id, *, by, role):
    """Write a ledger row directly, the way a pre-gate release (or any future
    path that reaches the table without going through screen_for_administration)
    would have. `role=None` is the shape of every row written before migration
    32 added the stamp."""
    with db_session() as db:
        # screen(), not screen_for_administration() — the latter now refuses a
        # non-prescriber outright, which is the very gate this row is meant to
        # predate. We want the fingerprint the dose will be screened against.
        screen = allergy_interlock.screen(db, patient_id, DRUG)
        db.execute(
            """INSERT INTO medication_allergy_overrides
                   (medication_id, patient_id, alert_fingerprint, decision, reason,
                    overridden_by, overridden_by_role)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (med_id, patient_id, screen["fingerprint"], BLOCK, GOOD_REASON, by, role),
        )


@pytest.mark.parametrize("role", [None, "", "nurse", "receptionist"])
def test_an_override_authored_without_prescribing_authority_does_not_carry(
    patient, nurse, doctor, role
):
    """The seam in the rule is "a PRESCRIBER's decision carries". A row that
    cannot prove that — authored by a non-prescriber, or too old to carry the
    stamp at all — is not a standing authorisation for anyone, INCLUDING the
    prescriber. It fails closed: the interlock re-arms and a fresh reason is
    typed, which costs a prompt, not a treatment."""
    _charted_anaphylaxis(patient)
    med_id = _chart_med(patient)
    _plant_override(med_id, patient, by=nurse, role=role)

    for actor in (nurse, doctor):
        with pytest.raises(AllergyInterlockBlocked) as ei:
            emar_service.record_administration(med_id, actor, status="given")
        # The PLAIN block — the dose is stopped by the allergy, and the message
        # names the allergen rather than implying the actor lacks authority.
        assert not isinstance(ei.value, AllergyOverrideNotPermitted)
        assert ei.value.screen["decision"] == BLOCK
        assert ei.value.screen.get("override") is None
    assert _rows("medication_administrations", med_id) == []


def test_a_prescriber_authored_override_still_carries_to_the_nurse(patient, nurse, doctor):
    """The other half, asserted in the same file so the fix above cannot be
    'made to pass' by refusing to carry anything. A QDS course must still run."""
    _charted_anaphylaxis(patient)
    med_id = _chart_med(patient)
    _plant_override(med_id, patient, by=doctor, role="doctor")

    rec = emar_service.record_administration(med_id, nurse, status="given")
    assert rec["allergy_check"]["override"]["carried"] is True
    assert rec["allergy_check"]["override"]["by_role"] == "doctor"
