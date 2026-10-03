"""The allergy interlock at medication administration.

The gap this closes: every other bedside guard fired (active order, pharmacist
verification, mandatory not-given reason, typed confirmation) but NOTHING
checked the patient's allergies at the moment the drug went in.

These tests are organised the way the interlock has to be argued:

  1. MATCHING — does it catch the real thing (exact, same class, brand name via
     the hospital's own formulary, cross-reactivity) and, just as importantly,
     does it stay quiet on unrelated drugs? An interlock that cries wolf is the
     documented alert-fatigue hazard and is worse than none.
  2. SEVERITY — anaphylaxis stops a dose; a rash history warns; the reaction
     text overrules a mis-recorded severity in both directions.
  3. OVERRIDE — a hard block with no way through is clinically wrong. Typed
     reason, recorded against the order and the actor, on the tamper-evident
     chain, carried forward for the same order but re-armed when the allergy
     picture changes.
  4. FAIL CLOSED — an allergy list that cannot be read must never read as "no
     allergies".
  5. The ward board previews the same verdict, and cannot weaken the write.
"""
import json
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from auth import create_token
from database import db_session, verify_audit_chain
from routes.emar_routes import router as emar_router
from services import allergy_interlock, emar_service, nurse_shift_service
from services.allergy_interlock import ALLOW, BLOCK, WARN, AllergyInterlockBlocked
from services.clinical_records_service import add_allergy
from services.helix_db import init_helix_db


# ── fixtures ──────────────────────────────────────────────────────────────────
@pytest.fixture()
def patient():
    """TEST-ISOLATION LAW: patients.status DEFAULTS to 'admitted', so every test
    in this module used to leave one more admitted patient in the shared session
    DB — ~42 by the end of the module. That is not a cosmetic leak. The FHIR
    self-export in services/downtime_service.py iterates
    `patients WHERE status='admitted'` and sleeps 250 ms BETWEEN PATIENTS, so
    each leftover adds a quarter-second to every export call in
    tests/test_downtime_fhir_export.py and to everything downstream of it.
    Discharge on teardown; do not DELETE, because audit/allergy rows written
    during the test legitimately reference this id.

    The teardown ALSO closes any admissions row `_admit()` opened below. The
    census surfaces filter patients.status while the department-scoped surfaces
    join admissions, so closing only the patient left this module's residue
    half-visible — clean on the census, still live on the ward board, which is
    worse than an honest leak because it looks fixed."""
    pid = f"PATALG{uuid.uuid4().hex[:6].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO patients (id, name, mrn) VALUES (?, ?, ?)",
            (pid, "Allergy Test", f"MRN-{uuid.uuid4().hex[:6]}"),
        )
    yield pid
    with db_session() as db:
        db.execute("UPDATE patients SET status = 'discharged' WHERE id = ?", (pid,))
        db.execute(
            "UPDATE admissions SET status = 'discharged' "
            "WHERE patient_id = ? AND status = 'active'",
            (pid,),
        )


@pytest.fixture()
def nurse():
    uid = f"NURALG{uuid.uuid4().hex[:5].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT OR IGNORE INTO users (id, name, role, password_hash) VALUES (?, ?, 'nurse', 'x')",
            (uid, "Nurse Interlock"),
        )
    return uid


@pytest.fixture()
def doctor():
    """A PRESCRIBER — the only actor who may clear a charted allergy.

    The override cases below use this fixture rather than `nurse` because since
    2026-08-15 the reason is only accepted from a holder of
    `permissions.MEDICATION_PRESCRIBE`, which the nurse role does not have. The
    nurse fixture stays everywhere else: giving the dose, being blocked by it,
    withholding it and replaying it are all hers. That the two fixtures are not
    interchangeable HERE is the control, and
    tests/test_allergy_override_prescriber.py asserts it directly.
    """
    uid = f"DRALG{uuid.uuid4().hex[:5].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT OR IGNORE INTO users (id, name, role, password_hash) VALUES (?, ?, 'doctor', 'x')",
            (uid, "Dr Interlock"),
        )
    return uid


@pytest.fixture(scope="module", autouse=True)
def _formulary():
    """The hospital's own catalogue, which the interlock reuses as its drug
    vocabulary instead of inventing a second ontology."""
    init_helix_db()
    with db_session() as db:
        for mid, name, generic in (
            ("TST-MONOCEF", "Monocef 1g Injection", "Ceftriaxone"),
            ("TST-AUGMENTIN", "Augmentin 625 DUO Tablet", "Amoxicillin/Clavulanate"),
            ("TST-DOLO", "Dolo 650 Tablet", "Paracetamol"),
        ):
            db.execute(
                "INSERT OR IGNORE INTO helix_medication_master (id, name, generic_name, form, strength) "
                "VALUES (?, ?, ?, 'Tablet', '1')",
                (mid, name, generic),
            )


def _chart_med(patient_id, drug, dosage="500mg", route="Oral", frequency="QDS"):
    with db_session() as db:
        cur = db.execute(
            """INSERT INTO medications (patient_id, drug_name, dosage, route, frequency,
                                        status, prescribed_by, source)
               VALUES (?, ?, ?, ?, ?, 'active', 'D001', 'clinician')""",
            (patient_id, drug, dosage, route, frequency),
        )
        return cur.lastrowid


def _screen(patient_id, drug, med_id=None, **kw):
    with db_session() as db:
        return allergy_interlock.screen_for_administration(db, patient_id, drug, med_id, **kw)


def _plain_screen(patient_id, drug):
    with db_session() as db:
        return allergy_interlock.screen(db, patient_id, drug)


def _audit_rows(action_type, patient_id):
    with db_session() as db:
        return [dict(r) for r in db.execute(
            "SELECT * FROM audit_logs WHERE action_type = ? AND patient_id = ? ORDER BY id",
            (action_type, patient_id),
        ).fetchall()]


# ═════════════════════════════════════════════════════════════════════════════
# 1. MATCHING
# ═════════════════════════════════════════════════════════════════════════════
def test_exact_match_blocks(patient):
    add_allergy(patient, "amoxicillin", reaction="anaphylaxis", severity="severe")
    s = _plain_screen(patient, "Amoxicillin 500mg")
    assert s["decision"] == BLOCK
    assert [a["match"] for a in s["alerts"]] == ["direct"]


def test_same_class_match_blocks(patient):
    """The case a literal string match would miss: allergic to penicillin,
    prescribed amoxicillin. Different words, same class."""
    add_allergy(patient, "penicillin", severity="severe")
    s = _plain_screen(patient, "Amoxicillin 500mg")
    assert s["decision"] == BLOCK
    assert s["alerts"][0]["match"] == "class"
    assert "penicillins" in s["alerts"][0]["message"]


def test_no_match_is_silent(patient):
    """The other half of getting matching right. An over-broad interlock trains
    staff to click through, which is worse than no interlock at all."""
    add_allergy(patient, "penicillin", reaction="anaphylaxis", severity="severe")
    for drug in ("Paracetamol 500mg", "Furosemide 40mg", "Metoclopramide 10mg",
                 "Insulin Aspart", "Enoxaparin 40mg"):
        s = _plain_screen(patient, drug)
        assert s["decision"] == ALLOW, f"{drug} should not alert on a penicillin allergy: {s['alerts']}"
        assert s["alerts"] == []


def test_brand_name_resolved_through_hospital_formulary(patient):
    """'Monocef' is not in any drug-class map — it is a brand. The hospital's own
    helix_medication_master says it is ceftriaxone, which IS classifiable. Without
    this resolution a ceftriaxone-allergic patient given Monocef sails through."""
    add_allergy(patient, "ceftriaxone", severity="severe")
    s = _plain_screen(patient, "Monocef 1g")
    assert "ceftriaxone" in [i.lower() for i in s["ingredients"]]
    assert s["decision"] == BLOCK
    assert s["alerts"][0]["match"] == "direct"


def test_brand_name_cross_reactivity_via_formulary(patient):
    """Penicillin-allergic, prescribed a brand-name cephalosporin: the alert only
    exists because the brand resolved to its ingredient first."""
    add_allergy(patient, "penicillin", reaction="anaphylaxis", severity="severe")
    s = _plain_screen(patient, "Monocef 1g")
    assert [a["match"] for a in s["alerts"]] == ["cross_reactivity"]
    assert s["decision"] == BLOCK          # anaphylaxis history escalates it


def test_cross_reactivity_without_severe_history_only_warns(patient):
    """~1-3% cross-reactivity plus a rash history is a warning, not a stop. This
    is the single biggest source of over-alerting if you get it wrong."""
    add_allergy(patient, "penicillin", reaction="mild rash", severity="mild")
    s = _plain_screen(patient, "cefuroxime")
    assert [a["match"] for a in s["alerts"]] == ["cross_reactivity"]
    assert s["decision"] == WARN


def test_short_allergen_record_is_reported_not_guessed(patient):
    """'pen' substring-matches meroPENem and PENtazocine. Rather than match it
    (false positives) or drop it (silent miss), the screen reports that there is
    an allergy record it could not interpret."""
    add_allergy(patient, "pen", severity="severe")
    s = _plain_screen(patient, "Meropenem 1g")
    assert s["alerts"] == []
    assert s["decision"] == ALLOW
    assert [u["substance"] for u in s["uninterpreted_allergies"]] == ["pen"]


def test_allergies_on_file_count_is_reported(patient):
    """Zero alerts against zero recorded allergies is not the same statement as
    zero alerts against a documented list — the screen says which it was."""
    s = _plain_screen(patient, "Paracetamol")
    assert s["allergies_on_file"] == 0
    add_allergy(patient, "penicillin", severity="severe")
    assert _plain_screen(patient, "Paracetamol")["allergies_on_file"] == 1


# ═════════════════════════════════════════════════════════════════════════════
# 2. SEVERITY
# ═════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("severity,reaction,expected", [
    ("severe",   "anaphylaxis",     BLOCK),
    ("severe",   None,              BLOCK),
    ("moderate", "widespread rash", WARN),
    ("mild",     "mild rash",       WARN),
    # No severity recorded and nothing in the reaction to go on: fail SAFE.
    ("unknown",  None,              BLOCK),
    # Recorded 'mild' but the reaction is airway/circulatory — believe the
    # reaction. "anaphylaxis, severity: mild" is a data-entry artefact.
    ("mild",     "anaphylaxis, adrenaline given", BLOCK),
    # No severity recorded, reaction reads as an intolerance rather than an
    # allergy. Blocking every dose on "nausea" is how an interlock gets trained
    # out of existence.
    ("unknown",  "nausea and vomiting", WARN),
    # ...but an explicitly clinician-graded severe is never talked down.
    ("severe",   "nausea and vomiting", BLOCK),
])
def test_severity_decides_block_vs_warn(patient, severity, reaction, expected):
    add_allergy(patient, "amoxicillin", reaction=reaction, severity=severity)
    s = _plain_screen(patient, "Amoxicillin 500mg")
    assert s["decision"] == expected, s["alerts"]


def test_severity_basis_is_recorded(patient):
    """Why a dose was stopped has to be inspectable, not a black box."""
    add_allergy(patient, "amoxicillin", reaction="throat swelling", severity="mild")
    s = _plain_screen(patient, "Amoxicillin 500mg")
    assert s["alerts"][0]["severity_basis"] == "reaction_is_anaphylaxis_grade"
    assert s["alerts"][0]["effective_severity"] == "severe"


# ═════════════════════════════════════════════════════════════════════════════
# 3. THE INTERLOCK AT ADMINISTRATION (the actual control)
# ═════════════════════════════════════════════════════════════════════════════
def test_administration_blocked_and_nothing_written(patient, nurse):
    add_allergy(patient, "penicillin", reaction="anaphylaxis", severity="severe")
    med_id = _chart_med(patient, "Amoxicillin")

    with pytest.raises(AllergyInterlockBlocked) as ei:
        emar_service.record_administration(med_id, nurse, status="given")
    assert ei.value.screen["decision"] == BLOCK

    with db_session() as db:
        n = db.execute(
            "SELECT COUNT(*) AS c FROM medication_administrations WHERE medication_id = ?",
            (med_id,),
        ).fetchone()["c"]
    assert n == 0, "a blocked dose must not chart"


def test_clean_administration_records_the_screen(patient, nurse):
    """A dose that came back clean says so on the record — 'screened, nothing
    found' and 'never screened' must be distinguishable forever after."""
    add_allergy(patient, "penicillin", severity="severe")
    med_id = _chart_med(patient, "Paracetamol")
    rec = emar_service.record_administration(med_id, nurse, status="given")
    assert rec["allergy_check"]["decision"] == ALLOW
    assert rec["allergy_check"]["allergies_on_file"] == 1


def test_warning_grade_allergy_does_not_stop_the_dose_but_is_recorded(patient, nurse):
    add_allergy(patient, "amoxicillin", reaction="mild rash", severity="mild")
    med_id = _chart_med(patient, "Amoxicillin")
    rec = emar_service.record_administration(med_id, nurse, status="given")
    assert rec["allergy_check"]["decision"] == WARN
    assert rec["allergy_check"]["alerts"][0]["substance"] == "amoxicillin"


def test_not_given_is_never_interlocked(patient, nurse):
    """Withholding a dose because of an allergy must always be easy. Interlocking
    the withhold path would push nurses toward giving it."""
    add_allergy(patient, "amoxicillin", reaction="anaphylaxis", severity="severe")
    med_id = _chart_med(patient, "Amoxicillin")
    rec = emar_service.record_administration(
        med_id, nurse, status="not_given", not_given_reason="allergy — bleeped Dr",
    )
    assert rec["status"] == "not_given"
    assert rec["allergy_check"] is None


def test_idempotent_replay_of_a_now_blocked_dose_still_returns(patient, nurse):
    """The dose was given and screened clean; the allergy was charted afterwards.
    Re-blocking the offline client's replay would not un-give the dose — it would
    only wedge the client into retrying forever."""
    med_id = _chart_med(patient, "Amoxicillin")
    key = f"emar-{uuid.uuid4().hex}"
    first = emar_service.record_administration(med_id, nurse, status="given",
                                               idempotency_key=key)
    add_allergy(patient, "amoxicillin", reaction="anaphylaxis", severity="severe")
    replay = emar_service.record_administration(med_id, nurse, status="given",
                                                idempotency_key=key)
    assert replay["id"] == first["id"] and replay["replayed"] is True


# ═════════════════════════════════════════════════════════════════════════════
# 4. OVERRIDE — who overrode what, and why
# ═════════════════════════════════════════════════════════════════════════════
def test_override_with_typed_reason_proceeds_and_is_recorded(patient, doctor):
    add_allergy(patient, "penicillin", reaction="anaphylaxis", severity="severe")
    med_id = _chart_med(patient, "Amoxicillin")
    reason = "Allergy de-labelled by immunology 12/06; oral challenge tolerated. Dr Rao aware."

    rec = emar_service.record_administration(
        med_id, doctor, status="given", allergy_override_reason=reason,
    )
    assert rec["allergy_check"]["decision"] == BLOCK
    assert rec["allergy_check"]["override"]["reason"] == reason
    assert rec["allergy_check"]["override"]["carried"] is False

    with db_session() as db:
        rows = [dict(r) for r in db.execute(
            "SELECT * FROM medication_allergy_overrides WHERE medication_id = ?", (med_id,)
        ).fetchall()]
    assert len(rows) == 1
    assert rows[0]["overridden_by"] == doctor
    assert rows[0]["reason"] == reason
    assert rows[0]["administration_id"] == rec["id"]
    assert json.loads(rows[0]["alerts_json"])[0]["substance"] == "penicillin"
    # The ledger row is readable COLD, without joining `medications` or `users`
    # — both of which move. Migration 32.
    assert rows[0]["drug_name"] == "Amoxicillin"
    assert rows[0]["overridden_by_role"] == "doctor"

    audits = _audit_rows("medication_allergy_override", patient)
    assert len(audits) == 1
    meta = json.loads(audits[0]["metadata_json"])
    assert meta["reason"] == reason
    assert meta["carried_forward"] is False
    assert meta["alerts"][0]["match"] == "class"
    assert meta["allergens"] == ["penicillin"]
    assert meta["authorised_by_role"] == "doctor"
    assert audits[0]["actor_user_id"] == doctor
    # Fail-closed category, so the override cannot be missing from a dose that
    # committed.
    assert audits[0]["event_category"] == "clinical_access"
    assert verify_audit_chain()["ok"]


@pytest.mark.parametrize("bad", ["", "   ", "ok", "yes", "n/a", "aaaaaaaaaaa", "urgent"])
def test_junk_override_reasons_are_refused(patient, nurse, bad):
    """A length floor and a junk list are the cheapest honest barrier against the
    reflex clear-through that makes overrides meaningless."""
    add_allergy(patient, "amoxicillin", reaction="anaphylaxis", severity="severe")
    med_id = _chart_med(patient, "Amoxicillin")
    with pytest.raises(AllergyInterlockBlocked):
        emar_service.record_administration(med_id, nurse, status="given",
                                           allergy_override_reason=bad)
    with db_session() as db:
        assert db.execute(
            "SELECT COUNT(*) AS c FROM medication_administrations WHERE medication_id = ?",
            (med_id,),
        ).fetchone()["c"] == 0


def test_override_carries_forward_for_the_same_order(patient, nurse, doctor):
    """QDS for a week is 28 doses. Demanding a fresh typed reason 28 times
    guarantees the 28th is typed without reading. The decision is order-scoped
    and re-recorded on every dose.

    NOTE WHO GIVES WHICH DOSE. The prescriber makes the decision; the NURSE
    gives the second dose under it, and must be able to — a carried-forward
    reason is not a new decision, and requiring the prescriber at the bedside
    for every subsequent dose of the course would stop the treatment or move it
    onto paper."""
    add_allergy(patient, "penicillin", reaction="anaphylaxis", severity="severe")
    med_id = _chart_med(patient, "Amoxicillin")
    reason = "Desensitisation protocol in progress, step 4 — Dr Iyer at bedside."
    emar_service.record_administration(med_id, doctor, status="given",
                                       allergy_override_reason=reason)

    second = emar_service.record_administration(med_id, nurse, status="given")
    assert second["allergy_check"]["decision"] == BLOCK
    assert second["allergy_check"]["override"]["carried"] is True
    assert second["allergy_check"]["override"]["reason"] == reason

    with db_session() as db:
        # Carried, not re-written: one acceptance of this hazard, two doses.
        assert db.execute(
            "SELECT COUNT(*) AS c FROM medication_allergy_overrides WHERE medication_id = ?",
            (med_id,),
        ).fetchone()["c"] == 1
    # ...but BOTH doses are audited as having been given despite the interlock.
    assert len(_audit_rows("medication_allergy_override", patient)) == 2
    assert json.loads(_audit_rows("medication_allergy_override", patient)[1]["metadata_json"])[
        "carried_forward"] is True


def test_new_allergy_re_arms_a_carried_override(patient, nurse, doctor):
    """The override is scoped to the allergy picture it was typed against. A
    newly-charted allergy changes the fingerprint and the interlock re-arms —
    a stale acknowledgement must not cover a new hazard."""
    add_allergy(patient, "penicillin", reaction="anaphylaxis", severity="severe")
    med_id = _chart_med(patient, "Amoxicillin")
    emar_service.record_administration(
        med_id, doctor, status="given",
        allergy_override_reason="De-labelled 2024, challenge tolerated on this admission.",
    )
    emar_service.record_administration(med_id, nurse, status="given")  # carried

    add_allergy(patient, "co-amoxiclav", reaction="angioedema", severity="severe")
    with pytest.raises(AllergyInterlockBlocked):
        emar_service.record_administration(med_id, nurse, status="given")


def test_override_does_not_leak_to_a_different_order(patient, nurse, doctor):
    add_allergy(patient, "penicillin", reaction="anaphylaxis", severity="severe")
    med_a = _chart_med(patient, "Amoxicillin")
    med_b = _chart_med(patient, "Flucloxacillin")
    emar_service.record_administration(
        med_a, doctor, status="given",
        allergy_override_reason="Challenge dose under supervision, consultant present.",
    )
    with pytest.raises(AllergyInterlockBlocked):
        emar_service.record_administration(med_b, nurse, status="given")


# ═════════════════════════════════════════════════════════════════════════════
# 5. FAIL CLOSED
# ═════════════════════════════════════════════════════════════════════════════
def test_unreadable_allergy_data_blocks(patient, nurse, monkeypatch):
    """If the allergy list cannot be read the dose does NOT proceed as if the
    patient had no allergies. An unscreened dose must never look like a clean
    one."""
    def _boom(db, pid):
        raise RuntimeError("patient_allergies unreadable (decryption failure)")

    med_id = _chart_med(patient, "Amoxicillin")
    monkeypatch.setattr(allergy_interlock, "read_active_allergies", _boom)

    with pytest.raises(AllergyInterlockBlocked) as ei:
        emar_service.record_administration(med_id, nurse, status="given")
    screen = ei.value.screen
    assert screen["screen_failed"] is True
    assert screen["decision"] == BLOCK
    assert screen["allergies_on_file"] is None      # not 0 — unknown, not empty
    assert screen["alerts"] == []
    assert "NOT been screened" in screen["message"]

    with db_session() as db:
        assert db.execute(
            "SELECT COUNT(*) AS c FROM medication_administrations WHERE medication_id = ?",
            (med_id,),
        ).fetchone()["c"] == 0


def test_unreadable_allergy_data_is_overridable_with_a_reason(patient, nurse, monkeypatch):
    """A total unoverridable block is its own hazard — you must still be able to
    give adrenaline during a partial outage. But nothing proceeds silently."""
    def _boom(db, pid):
        raise RuntimeError("patient_allergies unreadable")

    # 1 mg IV is the cardiac-arrest dose. Charted explicitly because this
    # module's _chart_med defaults to "500mg Oral", and 500 mg of adrenaline is
    # 500x the largest legitimate dose by ANY route — the bedside dose guard now
    # (correctly) stops it, which would mask what this test is actually about.
    med_id = _chart_med(patient, "Adrenaline", dosage="1mg", route="IV")
    monkeypatch.setattr(allergy_interlock, "read_active_allergies", _boom)
    rec = emar_service.record_administration(
        med_id, nurse, status="given",
        allergy_override_reason="Allergy store down; peri-arrest, giving per resus protocol.",
    )
    assert rec["allergy_check"]["screen_failed"] is True
    audits = _audit_rows("medication_allergy_override", patient)
    assert json.loads(audits[-1]["metadata_json"])["screen_failed"] is True
    assert verify_audit_chain()["ok"]


def test_failed_screen_is_never_carried_forward(patient, nurse, monkeypatch):
    """An unreadable screen is not a decision anyone made about a known risk, so
    it gets no fingerprint and can never be inherited by a later dose."""
    def _boom(db, pid):
        raise RuntimeError("down")

    # Realistic dose/route — see the note in the test above.
    med_id = _chart_med(patient, "Adrenaline", dosage="1mg", route="IV")
    monkeypatch.setattr(allergy_interlock, "read_active_allergies", _boom)
    emar_service.record_administration(
        med_id, nurse, status="given",
        allergy_override_reason="Allergy store down; peri-arrest per resus protocol.",
    )
    with pytest.raises(AllergyInterlockBlocked):
        emar_service.record_administration(med_id, nurse, status="given")


def test_missing_formulary_degrades_but_does_not_blind_the_screen(patient, monkeypatch):
    """The formulary is enrichment. Losing it costs brand-name resolution; it
    must not turn the allergy check itself into a silent pass."""
    add_allergy(patient, "amoxicillin", reaction="anaphylaxis", severity="severe")

    class _Broken:
        def execute(self, *a, **k):
            raise RuntimeError("helix_medication_master missing")

    with db_session() as db:
        assert allergy_interlock.resolve_ingredients_bulk(_Broken(), ["Monocef"]) == {"monocef": []}
        s = allergy_interlock.screen(db, patient, "Amoxicillin 500mg")
    assert s["decision"] == BLOCK


# ═════════════════════════════════════════════════════════════════════════════
# 6. HTTP contract
# ═════════════════════════════════════════════════════════════════════════════
@pytest.fixture()
def client():
    app = FastAPI()
    app.include_router(emar_router)
    return TestClient(app)


def _auth(uid, role="nurse"):
    return {"Authorization": f"Bearer {create_token(uid, role)}"}


def test_route_returns_428_then_accepts_the_reason(patient, doctor, client):
    """The PRESCRIBER's round trip: blocked, then cleared with a reason. The
    nurse's half of this contract — 428, then 403 if she supplies a reason
    anyway — is in tests/test_allergy_override_prescriber.py."""
    add_allergy(patient, "penicillin", reaction="anaphylaxis", severity="severe")
    med_id = _chart_med(patient, "Amoxicillin")
    hdr = _auth(doctor, "doctor")

    resp = client.post(f"/api/emar/medications/{med_id}/administer",
                       json={"status": "given"}, headers=hdr)
    assert resp.status_code == 428, resp.text
    detail = resp.json()["detail"]
    assert detail["error"] == "allergy_acknowledgement_required"
    assert detail["allergy"]["decision"] == BLOCK
    assert detail["allergy"]["alerts"][0]["substance"] == "penicillin"
    # The 428 names the permission that clears it, so no client has to hard-code
    # "ask a doctor".
    assert detail["allergy"]["override_requires_permission"] == "medication.prescribe"

    resp = client.post(
        f"/api/emar/medications/{med_id}/administer",
        json={"status": "given",
              "allergy_override_reason": "Immunology de-labelled 02/07; challenge tolerated."},
        headers=hdr,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["administration"]["allergy_check"]["override"]["carried"] is False


def test_route_428_distinguishes_an_unavailable_screen(patient, nurse, client, monkeypatch):
    def _boom(db, pid):
        raise RuntimeError("down")

    med_id = _chart_med(patient, "Amoxicillin")
    monkeypatch.setattr(allergy_interlock, "read_active_allergies", _boom)
    resp = client.post(f"/api/emar/medications/{med_id}/administer",
                       json={"status": "given"}, headers=_auth(nurse))
    assert resp.status_code == 428
    assert resp.json()["detail"]["error"] == "allergy_screen_unavailable"


def test_history_carries_the_screen(patient, nurse, client):
    add_allergy(patient, "amoxicillin", reaction="mild rash", severity="mild")
    med_id = _chart_med(patient, "Amoxicillin")
    emar_service.record_administration(med_id, nurse, status="given")
    resp = client.get(f"/api/emar/patients/{patient}/administrations", headers=_auth(nurse))
    assert resp.status_code == 200
    assert resp.json()["administrations"][0]["allergy_check"]["decision"] == WARN


# ═════════════════════════════════════════════════════════════════════════════
# 7. The ward board previews the same verdict (courtesy, not control)
# ═════════════════════════════════════════════════════════════════════════════
def _admit(pid, dept):
    with db_session() as db:
        db.execute("UPDATE patients SET status = 'admitted', ward = ?, bed = '1' WHERE id = ?",
                   (dept, pid))
        db.execute(
            "INSERT OR IGNORE INTO users (id, name, role, password_hash) "
            "VALUES ('ALGTESTADM', 'Allergy Test Admitter', 'doctor', 'x')"
        )
        db.execute(
            "INSERT INTO admissions (patient_id, department, status, admitted_by) "
            "VALUES (?, ?, 'active', 'ALGTESTADM')",
            (pid, dept),
        )


def test_board_flags_the_dose_before_the_nurse_walks_to_the_bedside(patient, nurse):
    dept = f"ALGWARD{uuid.uuid4().hex[:5].upper()}"
    _admit(patient, dept)
    add_allergy(patient, "penicillin", reaction="anaphylaxis", severity="severe")
    _chart_med(patient, "Amoxicillin")
    _chart_med(patient, "Paracetamol")

    board = nurse_shift_service.build_board(dept)
    card = board["patients"][0]
    by_drug = {m["drug_name"]: m for m in card["medications"]}
    assert by_drug["Amoxicillin"]["allergy"]["decision"] == BLOCK
    assert by_drug["Amoxicillin"]["allergy"]["alerts"][0]["substance"] == "penicillin"
    assert by_drug["Paracetamol"]["allergy"]["decision"] == ALLOW
    assert card["meds_allergy_blocked"] == 1
    assert board["totals"]["meds_allergy_blocked"] == 1


def test_board_reflects_an_override_already_on_record(patient, doctor):
    dept = f"ALGWARD{uuid.uuid4().hex[:5].upper()}"
    _admit(patient, dept)
    add_allergy(patient, "penicillin", reaction="anaphylaxis", severity="severe")
    med_id = _chart_med(patient, "Amoxicillin")
    emar_service.record_administration(
        med_id, doctor, status="given",
        allergy_override_reason="De-labelled by immunology; challenge tolerated 30/07.",
    )
    card = nurse_shift_service.build_board(dept)["patients"][0]
    med = card["medications"][0]
    assert med["allergy"]["decision"] == BLOCK
    assert med["allergy"]["override"]["carried"] is True
    assert card["meds_allergy_blocked"] == 0     # already accepted for this order


def test_board_preview_cannot_weaken_the_write(patient, nurse, monkeypatch):
    """The board is a courtesy. If its preview were the control, stubbing it
    clean would let the dose through — it must not."""
    dept = f"ALGWARD{uuid.uuid4().hex[:5].upper()}"
    _admit(patient, dept)
    add_allergy(patient, "penicillin", reaction="anaphylaxis", severity="severe")
    med_id = _chart_med(patient, "Amoxicillin")

    monkeypatch.setattr(nurse_shift_service, "_preview_allergy",
                        lambda *a, **k: {"decision": ALLOW, "alerts": [], "override": None})
    card = nurse_shift_service.build_board(dept)["patients"][0]
    assert card["medications"][0]["allergy"]["decision"] == ALLOW  # lying board
    with pytest.raises(AllergyInterlockBlocked):                   # honest server
        emar_service.record_administration(med_id, nurse, status="given")
