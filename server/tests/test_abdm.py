"""Orb — the ABDM / ABHA rails (services/abdm_service.py + routes/abdm_routes.py).

WHAT THESE TESTS ARE GUARDING, in the order the harm lands on a patient:

  1. THE HONESTY. Orb is not connected to the ABDM gateway, and the words that
     say so are asserted in the service, in EVERY route payload, and in the panel
     source. Deleting the sentence turns this file red. The failure it prevents
     is a desk that believes Orb linked the care context and therefore does not
     link it — after which the patient's discharge summary never reaches their
     PHR and nobody finds out for months.

  2. AN UNVERIFIED ABHA READS AS UNVERIFIED. Never as verified, never as blank,
     never as "probably fine". There is no parameter anywhere that can create a
     verified identity, correcting the number resets the tick, and an unverified
     ABHA cannot carry a care-context link at all — because linking a mistyped
     national health id publishes one patient's record into another patient's
     health account, and there is no undo that reaches the copy they downloaded.

  3. A LINKED CARE CONTEXT CANNOT SILENTLY UNLINK. The row is never deleted, the
     reason is mandatory, an event is written, and the ABHA underneath a live
     link cannot be edited.

  4. A REVOKED CONSENT NEVER COMES BACK. Revocation is the only real control the
     national stack gives a patient. See the prove-can-fail block at the bottom.

PROVEN ABLE TO FAIL. The revocation assertions were watched go RED with the
defect installed — see the note above test_revoked_consent_can_never_be_granted_
again for exactly what was changed and what the failure looked like.
"""
import os
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import config  # noqa: E402
from auth import hash_password  # noqa: E402
from database import db_session, get_db  # noqa: E402
from main import app  # noqa: E402
from services import abdm_service as ab  # noqa: E402

client = TestClient(app)

REPO_ROOT = Path(__file__).resolve().parents[2]
PANEL_PATH = REPO_ROOT / "src" / "pages" / "panels" / "AbdmPanel.tsx"


# ── Fixtures ─────────────────────────────────────────────────────────────────

def _ensure_user(user_id: str, *, role: str = "admin", department: str | None = None) -> str:
    db = get_db()
    try:
        db.execute(
            "INSERT OR IGNORE INTO users (id, name, role, department, password_hash) "
            "VALUES (?, ?, ?, ?, ?)",
            (user_id, "ABDM Desk", role, department, hash_password("password")),
        )
        db.commit()
    finally:
        db.close()
    return user_id


def _login(user_id: str) -> dict:
    resp = client.post(
        "/api/auth/login", json={"employee_id": user_id, "password": "password"}
    )
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['token']}"}


_SEQ = {"patient": 0, "abha": 0}


def _next_abha() -> str:
    """A distinct 14-digit ABHA per call. Test data only — these are not issued
    numbers and nothing in Orb pretends they are."""
    _SEQ["abha"] += 1
    return f"9{_SEQ['abha']:013d}"


def _ensure_patient(*, department: str = "Medicine", ward: str = "W1") -> tuple[str, str]:
    """A patient with an admission, so department scoping has something to
    derive from. Returns (patient_id, admission_id)."""
    _SEQ["patient"] += 1
    pid = f"ABDM-PT-{_SEQ['patient']:04d}"
    aid = f"ABDM-ADM-{_SEQ['patient']:04d}"
    _ensure_user("ABDM-ADMITTER")
    db = get_db()
    try:
        db.execute(
            "INSERT OR IGNORE INTO patients (id, name, mrn, ward, bed, status) "
            "VALUES (?, ?, ?, ?, ?, 'admitted')",
            (pid, f"ABDM Test {_SEQ['patient']}", f"MRN-{pid}", ward, "1"),
        )
        db.execute(
            "INSERT OR IGNORE INTO admissions "
            "(id, patient_id, admitted_by, department, ward, status) "
            "VALUES (?, ?, 'ABDM-ADMITTER', ?, ?, 'active')",
            (aid, pid, department, ward),
        )
        db.commit()
    finally:
        db.close()
    return pid, aid


@pytest.fixture()
def desk() -> dict:
    """A hospital-wide administrator — no department, so no ward scoping."""
    return _login(_ensure_user("ABDM-ADM"))


def _verified_identity(patient_id: str) -> dict:
    identity = ab.record_abha(
        patient_id=patient_id, abha_number=_next_abha(), recorded_by="ABDM-ADM"
    )
    return ab.record_verification(
        identity_id=identity["id"],
        method="aadhaar_otp",
        outcome="verified",
        performed_by="ABDM-ADM",
        performed_where="abdm_portal",
        reference="TXN-TEST",
    )


# ═════════════════════════════════════════════════════════════════════════════
#  1. ABHA FORMAT VALIDATION
# ═════════════════════════════════════════════════════════════════════════════

class TestAbhaNumberFormat:
    @pytest.mark.parametrize("raw,expected", [
        ("12-3456-7890-1234", "12345678901234"),   # card form
        ("12 3456 7890 1234", "12345678901234"),   # spaces
        ("12345678901234", "12345678901234"),      # bare
        ("  91-1234-5678-9012  ", "91123456789012"),  # padded
        ("12–3456–7890–1234", "12345678901234"),   # en-dashes from a PDF paste
    ])
    def test_accepts_the_forms_a_human_actually_types(self, raw, expected):
        check = ab.validate_abha_number(raw)
        assert check.valid, check.reason
        assert check.normalised == expected
        assert check.formatted == "-".join(
            [expected[0:2], expected[2:6], expected[6:10], expected[10:14]]
        )

    @pytest.mark.parametrize("raw,fragment", [
        ("1234567890123", "14 digits"),            # 13
        ("123456789012345", "14 digits"),          # 15
        ("", "No ABHA number"),
        (None, "No ABHA number"),
        ("12-3456-7890-123A", "digits only"),
        ("abcdefghijklmn", "digits only"),
        ("00000000000000", "same digit fourteen times"),
        ("11111111111111", "same digit fourteen times"),
    ])
    def test_refuses_what_is_not_an_abha_number(self, raw, fragment):
        check = ab.validate_abha_number(raw)
        assert not check.valid
        assert check.normalised is None
        # The refusal names the rule that failed. "Invalid" tells a clerk with a
        # queue behind them nothing they can act on.
        assert fragment in (check.reason or ""), check.reason

    def test_says_out_loud_that_it_checked_nothing_with_abdm(self):
        """A green tick that means 'fourteen digits' must not be readable as a
        green tick that means 'this ABHA exists'."""
        payload = ab.validate_abha_number("12-3456-7890-1234").as_dict()
        assert payload["checked_against_abdm"] is False
        assert "cannot confirm" in payload["note"]

    def test_no_check_digit_rule_is_implemented(self):
        """Orb deliberately implements NO checksum: the ABHA number's internal
        structure is not something Orb has a specification for, and a guessed
        rule either refuses real patients at a counter or manufactures false
        confidence. If someone adds one later, this test is where they must
        justify it — a number that passes today must not start failing because a
        checksum was invented.
        """
        # Sequential digits: no plausible check-digit scheme accepts every one of
        # these, so a checksum sneaking in would break at least one.
        for raw in ("12345678901234", "98765432109876", "20000000000001"):
            assert ab.validate_abha_number(raw).valid, raw


class TestAbhaAddressFormat:
    @pytest.mark.parametrize("raw,normalised,recognised", [
        ("hina.patel@abdm", "hina.patel@abdm", True),
        ("Hina.Patel@ABDM", "hina.patel@abdm", True),     # case folded
        ("  ravi_kumar@sbx  ", "ravi_kumar@sbx", True),   # sandbox suffix
        ("meera-99@abdm", "meera-99@abdm", True),
        ("clinic1@somefuture", "clinic1@somefuture", False),  # unknown suffix: allowed
    ])
    def test_accepts_and_normalises(self, raw, normalised, recognised):
        check = ab.validate_abha_address(raw)
        assert check.valid, check.reason
        assert check.normalised == normalised
        assert check.suffix_recognised is recognised

    def test_an_unrecognised_suffix_is_flagged_not_refused(self):
        """NHA can add a suffix any week. Refusing a real patient's address over
        a stale hard-coded list is the worse failure, so the list is
        informational."""
        check = ab.validate_abha_address("someone@newsuffix")
        assert check.valid and check.suffix_recognised is False

    @pytest.mark.parametrize("raw,fragment", [
        ("hinapatel", "exactly one '@'"),
        ("hina@patel@abdm", "exactly one '@'"),
        ("@abdm", "nothing before"),
        ("hina@", "nothing after"),
        ("hi@abdm", "shorter than"),
        ("hina patel@abdm", "no spaces"),
        ("hina..patel@abdm", "two separators in a row"),
        (".hina@abdm", "must start and end"),
        ("hina.@abdm", "must start and end"),
        ("hina!patel@abdm", "must start and end"),
        ("hina@1abdm", "does not look like an ABHA suffix"),
        ("", "No ABHA address"),
        (None, "No ABHA address"),
    ])
    def test_refuses_what_is_not_an_abha_address(self, raw, fragment):
        check = ab.validate_abha_address(raw)
        assert not check.valid
        assert check.normalised is None
        assert fragment in (check.reason or ""), check.reason


# ═════════════════════════════════════════════════════════════════════════════
#  2. UNVERIFIED IS UNVERIFIED
# ═════════════════════════════════════════════════════════════════════════════

class TestVerificationHonesty:
    def test_a_recorded_abha_is_unverified(self):
        pid, _ = _ensure_patient()
        identity = ab.record_abha(
            patient_id=pid, abha_number=_next_abha(), recorded_by="ABDM-ADM"
        )
        assert identity["verification_status"] == "unverified"
        assert identity["is_verified"] is False
        assert identity["verification_label"] == "Not verified"
        assert identity["verified_at"] is None and identity["verified_by"] is None

    def test_there_is_no_parameter_that_creates_a_verified_identity(self):
        """The signature itself is the guard. If someone adds a
        `verification_status` kwarg to record_abha, this test tells them why they
        must not."""
        import inspect
        params = set(inspect.signature(ab.record_abha).parameters)
        assert "verification_status" not in params
        assert "verified" not in params
        assert "is_verified" not in params

    def test_the_api_cannot_smuggle_a_verified_status_in(self, desk):
        """A client that posts `verification_status: 'verified'` gets an
        unverified record. Pydantic ignores the unknown field; the assertion is
        that the SERVER'S answer is unverified, whatever was sent."""
        pid, _ = _ensure_patient()
        resp = client.post(
            "/api/abdm/identities",
            json={
                "patient_id": pid,
                "abha_number": _next_abha(),
                "verification_status": "verified",
                "is_verified": True,
                "verified_by": "somebody",
            },
            headers=desk,
        )
        assert resp.status_code == 200, resp.text
        identity = resp.json()["identity"]
        assert identity["verification_status"] == "unverified"
        assert identity["is_verified"] is False

    def test_verification_records_who_when_and_how(self):
        pid, _ = _ensure_patient()
        identity = ab.record_abha(
            patient_id=pid, abha_number=_next_abha(), recorded_by="ABDM-ADM"
        )
        out = ab.record_verification(
            identity_id=identity["id"],
            method="aadhaar_otp",
            outcome="verified",
            performed_by="MRD-CLERK-7",
            performed_at="2026-08-10 09:15",
            performed_where="abdm_portal",
            reference="TXN-88231",
        )
        assert out["verification_status"] == "verified" and out["is_verified"] is True
        assert out["verified_by"] == "MRD-CLERK-7"
        assert out["verification_method"] == "aadhaar_otp"
        assert out["verified_at"].startswith("2026-08-10 09:15")
        record = out["verifications"][0]
        assert record["performed_where"] == "abdm_portal"
        assert record["reference"] == "TXN-88231"
        # recorded_at (when Orb heard) is kept distinct from performed_at (when
        # the human did it on the portal).
        assert record["recorded_at"] != record["performed_at"]

    def test_a_failed_verification_is_kept_and_leaves_it_unverified(self):
        pid, _ = _ensure_patient()
        identity = ab.record_abha(
            patient_id=pid, abha_number=_next_abha(), recorded_by="ABDM-ADM"
        )
        out = ab.record_verification(
            identity_id=identity["id"],
            method="mobile_otp",
            outcome="failed",
            performed_by="MRD-CLERK-7",
            failure_reason="OTP never arrived; patient's number has changed",
        )
        assert out["verification_status"] == "verification_failed"
        assert out["is_verified"] is False
        assert out["verifications"][0]["failure_reason"].startswith("OTP never arrived")

    def test_a_failed_verification_needs_a_reason(self):
        pid, _ = _ensure_patient()
        identity = ab.record_abha(
            patient_id=pid, abha_number=_next_abha(), recorded_by="ABDM-ADM"
        )
        with pytest.raises(ab.AbdmError, match="reason it failed"):
            ab.record_verification(
                identity_id=identity["id"], method="mobile_otp",
                outcome="failed", performed_by="MRD-CLERK-7",
            )

    def test_an_inconclusive_attempt_does_not_downgrade_a_verified_abha(self):
        """A try that proved nothing must not un-verify what was verified, and
        must not verify what was not."""
        pid, _ = _ensure_patient()
        identity = _verified_identity(pid)
        out = ab.record_verification(
            identity_id=identity["id"], method="demographic_match",
            outcome="inconclusive", performed_by="MRD-CLERK-7",
        )
        assert out["verification_status"] == "verified"

    def test_correcting_the_number_resets_the_verification(self):
        """What was verified was the OLD number. Carrying the tick across is
        exactly the lie this module exists to prevent."""
        pid, _ = _ensure_patient()
        identity = _verified_identity(pid)
        out = ab.correct_abha(
            identity_id=identity["id"],
            abha_number=_next_abha(),
            reason="Digit transposed at the counter; card re-read",
            actor_id="ABDM-ADM",
        )
        assert out["verification_status"] == "unverified"
        assert out["is_verified"] is False
        assert out["verified_at"] is None and out["verification_method"] is None

    def test_the_same_abha_cannot_be_attached_to_two_patients(self):
        """Two people sharing a national health id inside one hospital is a merge
        error with a PHI blast radius; it is refused with the conflict named."""
        first, _ = _ensure_patient()
        second, _ = _ensure_patient()
        number = _next_abha()
        ab.record_abha(patient_id=first, abha_number=number, recorded_by="ABDM-ADM")
        with pytest.raises(ab.IdentityError, match="different"):
            ab.record_abha(patient_id=second, abha_number=number, recorded_by="ABDM-ADM")

    def test_the_duplicate_refusal_names_the_other_patient_in_scope(self):
        """'A different patient (record #12)' sent the desk hunting by surrogate
        id. The refusal names the chart it collides with, the way the ward does."""
        first, _ = _ensure_patient(department="Medicine")
        second, _ = _ensure_patient(department="Medicine")
        number = _next_abha()
        ab.record_abha(patient_id=first, abha_number=number, recorded_by="ABDM-ADM")
        with pytest.raises(ab.IdentityError) as caught:
            ab.record_abha(
                patient_id=second, abha_number=number, recorded_by="ABDM-ADM",
                department="Medicine",
            )
        n = int(first.rsplit("-", 1)[1])
        assert f"ABDM Test {n}, MRN-{first}, W1" in str(caught.value)

    def test_the_duplicate_refusal_never_names_a_patient_outside_scope(self):
        """The other chart is on a ward this desk cannot see. Naming it would be
        the disclosure the scope exists to stop, so the refusal stays anonymous."""
        theirs, _ = _ensure_patient(department="Cardiology", ward="CCU")
        mine, _ = _ensure_patient(department="Medicine")
        number = _next_abha()
        ab.record_abha(patient_id=theirs, abha_number=number, recorded_by="ABDM-ADM")
        with pytest.raises(ab.IdentityError, match="different patient") as caught:
            ab.record_abha(
                patient_id=mine, abha_number=number, recorded_by="ABDM-ADM",
                department="Medicine",
            )
        message = str(caught.value)
        assert theirs not in message and "ABDM Test" not in message and "CCU" not in message

    def test_the_route_scopes_the_duplicate_refusal_to_the_caller(self):
        theirs, _ = _ensure_patient(department="Cardiology", ward="CCU")
        mine, _ = _ensure_patient(department="Medicine")
        number = _next_abha()
        ab.record_abha(patient_id=theirs, abha_number=number, recorded_by="ABDM-ADM")
        scoped = _login(_ensure_user("ABDM-ADM-MED-DUP", department="Medicine"))
        resp = client.post(
            "/api/abdm/identities",
            json={"patient_id": mine, "abha_number": number},
            headers=scoped,
        )
        assert resp.status_code == 400, resp.text
        assert "different patient" in resp.json()["detail"]
        assert "ABDM Test" not in resp.json()["detail"]

    def test_an_abha_record_with_neither_number_nor_address_is_refused(self):
        pid, _ = _ensure_patient()
        with pytest.raises(ab.AbhaFormatError, match="says nothing"):
            ab.record_abha(patient_id=pid, recorded_by="ABDM-ADM")


# ═════════════════════════════════════════════════════════════════════════════
#  3. CARE CONTEXTS — A LINK CANNOT SILENTLY UNLINK
# ═════════════════════════════════════════════════════════════════════════════

class TestCareContexts:
    def test_an_unverified_abha_cannot_carry_a_link(self):
        pid, aid = _ensure_patient()
        identity = ab.record_abha(
            patient_id=pid, abha_number=_next_abha(), recorded_by="ABDM-ADM"
        )
        with pytest.raises(ab.CareContextError, match="not verified"):
            ab.link_care_context(
                identity_id=identity["id"], care_context_reference=aid,
                linked_by="MRD-CLERK-7",
            )

    def test_unlink_without_a_reason_is_refused(self):
        pid, aid = _ensure_patient()
        identity = _verified_identity(pid)
        cc = ab.link_care_context(
            identity_id=identity["id"], care_context_reference=aid,
            encounter_ref=aid, linked_by="MRD-CLERK-7",
        )
        for blank in ("", "   ", ".", None):
            with pytest.raises(ab.CareContextError, match="reason"):
                ab.unlink_care_context(
                    care_context_id=cc["id"], reason=blank, actor_id="ABDM-ADM"
                )
        # And nothing moved.
        assert ab.list_care_contexts(patient_id=pid)[0]["status"] == "linked"

    def test_an_unlink_keeps_the_row_and_leaves_a_trail(self):
        """THE INVARIANT: 'was this episode ever linked?' stays answerable.

        A DELETE would make it unanswerable — which is precisely the question a
        patient asks when their discharge summary vanishes from their app.

        PROVEN ABLE TO FAIL, and watched go red: replacing the UPDATE in
        `unlink_care_context` with `DELETE FROM abdm_care_contexts WHERE id = ?`
        turns this test and three of its neighbours red. Done, observed, and
        restored on 2026-08-12.
        """
        pid, aid = _ensure_patient()
        identity = _verified_identity(pid)
        cc = ab.link_care_context(
            identity_id=identity["id"], care_context_reference=aid,
            encounter_ref=aid, hi_type="DischargeSummary", linked_by="MRD-CLERK-7",
        )
        out = ab.unlink_care_context(
            care_context_id=cc["id"],
            reason="Episode merged into the readmission record",
            actor_id="ABDM-ADM",
        )

        assert out["status"] == "unlinked" and out["is_linked"] is False
        assert out["unlink_reason"].startswith("Episode merged")
        assert out["unlinked_by"] == "ABDM-ADM" and out["unlinked_at"]
        # The original link is INTACT on the same row.
        assert out["linked_by"] == "MRD-CLERK-7" and out["linked_at"]
        assert out["care_context_reference"] == aid

        with db_session() as db:
            rows = db.execute(
                "SELECT COUNT(*) AS n FROM abdm_care_contexts WHERE id = ?", (cc["id"],)
            ).fetchone()["n"]
            events = [
                dict(r) for r in db.execute(
                    "SELECT event_type, from_status, to_status, reason, actor_id "
                    "FROM abdm_events WHERE entity_type = 'care_context' AND entity_id = ? "
                    "ORDER BY id",
                    (cc["id"],),
                ).fetchall()
            ]
        assert rows == 1, "the row must never be deleted"
        assert [e["event_type"] for e in events] == ["linked", "unlinked"]
        assert events[1]["from_status"] == "linked" and events[1]["to_status"] == "unlinked"
        assert events[1]["reason"].startswith("Episode merged")

    def test_unlinked_rows_are_listed_by_default(self):
        """A list that hides unlinked rows makes an unlink look like a deletion —
        the failure this table's whole design is against."""
        pid, aid = _ensure_patient()
        identity = _verified_identity(pid)
        cc = ab.link_care_context(
            identity_id=identity["id"], care_context_reference=aid,
            encounter_ref=aid, linked_by="MRD-CLERK-7",
        )
        ab.unlink_care_context(
            care_context_id=cc["id"], reason="Wrong episode selected", actor_id="ABDM-ADM"
        )
        rows = ab.list_care_contexts(patient_id=pid)
        assert len(rows) == 1 and rows[0]["status"] == "unlinked"

    def test_the_same_context_cannot_be_linked_twice_at_once(self):
        pid, aid = _ensure_patient()
        identity = _verified_identity(pid)
        ab.link_care_context(
            identity_id=identity["id"], care_context_reference=aid,
            encounter_ref=aid, linked_by="MRD-CLERK-7",
        )
        with pytest.raises(ab.CareContextError, match="already recorded as linked"):
            ab.link_care_context(
                identity_id=identity["id"], care_context_reference=aid,
                encounter_ref=aid, linked_by="MRD-CLERK-7",
            )

    def test_relinking_after_an_unlink_creates_a_second_row(self):
        """Re-linking is a NEW fact, not a resurrection of the old one."""
        pid, aid = _ensure_patient()
        identity = _verified_identity(pid)
        first = ab.link_care_context(
            identity_id=identity["id"], care_context_reference=aid,
            encounter_ref=aid, linked_by="MRD-CLERK-7",
        )
        ab.unlink_care_context(
            care_context_id=first["id"], reason="Linked to the wrong ABHA",
            actor_id="ABDM-ADM",
        )
        second = ab.link_care_context(
            identity_id=identity["id"], care_context_reference=aid,
            encounter_ref=aid, linked_by="MRD-CLERK-7",
        )
        assert second["id"] != first["id"]
        rows = ab.list_care_contexts(patient_id=pid)
        assert sorted(r["status"] for r in rows) == ["linked", "unlinked"]

    def test_the_abha_under_a_live_link_cannot_be_edited(self):
        """Repointing the number underneath a live link is an unlink and a
        re-link that nobody performed and nobody can see."""
        pid, aid = _ensure_patient()
        identity = _verified_identity(pid)
        ab.link_care_context(
            identity_id=identity["id"], care_context_reference=aid,
            encounter_ref=aid, linked_by="MRD-CLERK-7",
        )
        with pytest.raises(ab.IdentityError, match="Unlink them first"):
            ab.correct_abha(
                identity_id=identity["id"], abha_number=_next_abha(),
                reason="Clerk says the number is wrong", actor_id="ABDM-ADM",
            )

    def test_a_second_unlink_is_refused_rather_than_rewriting_history(self):
        pid, aid = _ensure_patient()
        identity = _verified_identity(pid)
        cc = ab.link_care_context(
            identity_id=identity["id"], care_context_reference=aid,
            encounter_ref=aid, linked_by="MRD-CLERK-7",
        )
        ab.unlink_care_context(
            care_context_id=cc["id"], reason="First reason, the true one",
            actor_id="ABDM-ADM",
        )
        with pytest.raises(ab.CareContextError, match="already unlinked"):
            ab.unlink_care_context(
                care_context_id=cc["id"], reason="Second reason, overwriting",
                actor_id="ABDM-ADM",
            )

    def test_the_api_unlink_requires_a_reason_in_the_body(self, desk):
        pid, aid = _ensure_patient()
        identity = _verified_identity(pid)
        cc = ab.link_care_context(
            identity_id=identity["id"], care_context_reference=aid,
            encounter_ref=aid, linked_by="MRD-CLERK-7",
        )
        # No reason at all: refused by the request schema (422).
        assert client.post(
            f"/api/abdm/care-contexts/{cc['id']}/unlink", json={}, headers=desk
        ).status_code == 422
        # A blank reason: refused by the service (400).
        assert client.post(
            f"/api/abdm/care-contexts/{cc['id']}/unlink",
            json={"reason": "  "}, headers=desk,
        ).status_code == 400
        assert ab.list_care_contexts(patient_id=pid)[0]["status"] == "linked"


# ═════════════════════════════════════════════════════════════════════════════
#  4. THE CONSENT LIFECYCLE
# ═════════════════════════════════════════════════════════════════════════════

def _consent(patient_id: str, **kwargs) -> dict:
    body = dict(
        patient_id=patient_id,
        requester_name="City Care HIU",
        purpose_code="CAREMGT",
        hi_types=["DischargeSummary", "Prescription"],
        recorded_by="ABDM-ADM",
    )
    body.update(kwargs)
    return ab.record_consent_request(**body)


class TestConsentLifecycle:
    def test_a_request_is_not_a_consent(self):
        pid, _ = _ensure_patient()
        artefact = _consent(pid)
        assert artefact["status"] == "requested"
        assert artefact["is_effective"] is False
        assert artefact["artefact_id"] is None, (
            "an artefact id at request time would be inventing the patient's answer"
        )

    def test_grant_then_revoke(self):
        pid, _ = _ensure_patient()
        artefact = _consent(pid, expires_at="2027-12-31")
        granted = ab.record_consent_grant(
            consent_id=artefact["id"], artefact_id="CM-ART-0001"
        )
        assert granted["status"] == "granted" and granted["is_effective"] is True
        assert granted["hi_types"] == ["DischargeSummary", "Prescription"]

        revoked = ab.revoke_consent(
            consent_id=artefact["id"],
            reason="Patient withdrew at the registration counter",
            actor_id="ABDM-ADM",
        )
        assert revoked["status"] == "revoked"
        assert revoked["effective_status"] == "revoked"
        assert revoked["is_effective"] is False
        assert revoked["is_revoked"] is True
        assert revoked["can_be_reinstated"] is False
        assert revoked["revoked_by"] == "ABDM-ADM"
        assert revoked["revocation_reason"].startswith("Patient withdrew")

    def test_revocation_needs_a_reason_and_an_actor(self):
        pid, _ = _ensure_patient()
        artefact = _consent(pid)
        ab.record_consent_grant(consent_id=artefact["id"], artefact_id="CM-ART-0002")
        with pytest.raises(ab.ConsentError, match="reason"):
            ab.revoke_consent(consent_id=artefact["id"], reason="  ", actor_id="ABDM-ADM")
        with pytest.raises(ab.ConsentError, match="person"):
            ab.revoke_consent(
                consent_id=artefact["id"], reason="Patient withdrew", actor_id="",
            )
        assert ab.get_consent(artefact["id"])["status"] == "granted"

    def test_a_grant_needs_the_consent_managers_artefact_id(self):
        pid, _ = _ensure_patient()
        artefact = _consent(pid)
        with pytest.raises(ab.ConsentError, match="artefact id"):
            ab.record_consent_grant(consent_id=artefact["id"], artefact_id="  ")

    def test_a_refusal_is_terminal_too(self):
        pid, _ = _ensure_patient()
        artefact = _consent(pid)
        ab.record_consent_denial(
            consent_id=artefact["id"], reason="Patient declined on the phone"
        )
        with pytest.raises(ab.IllegalConsentTransition):
            ab.record_consent_grant(consent_id=artefact["id"], artefact_id="CM-ART-0003")

    def test_expiry_is_derived_at_read_time_not_trusted_from_the_column(self):
        """A stored 'granted' whose expiry passed must never READ as live
        permission. There is no sweeper job that can be relied on to have run,
        and the window between the expiry and the sweep is exactly when somebody
        looks at the screen.
        """
        pid, _ = _ensure_patient()
        artefact = _consent(pid)
        granted = ab.record_consent_grant(
            consent_id=artefact["id"], artefact_id="CM-ART-0004",
            expires_at="2020-01-01 00:00",
        )
        assert granted["status"] == "granted", "the stored row is reported honestly"
        assert granted["effective_status"] == "expired"
        assert granted["is_expired"] is True
        assert granted["is_effective"] is False

    def test_an_unreadable_date_is_refused_rather_than_stored_as_null(self):
        """NULL in expires_at means 'no expiry recorded' — a different and
        dangerous claim from 'the clerk typed something Orb could not parse'."""
        pid, _ = _ensure_patient()
        with pytest.raises(ab.AbdmError, match="not a date Orb can read"):
            _consent(pid, expires_at="next tuesday")

    def test_unknown_hi_types_are_refused(self):
        """ABDM's HI-type list is the NATIONAL vocabulary, unlike the local
        denial codes elsewhere in Orb, so a value outside it is a mistake."""
        pid, _ = _ensure_patient()
        with pytest.raises(ab.ConsentError, match="unknown health-information"):
            _consent(pid, hi_types=["DischargeSummary", "EverythingPlease"])

    def test_purpose_other_needs_words(self):
        pid, _ = _ensure_patient()
        with pytest.raises(ab.ConsentError, match="in words"):
            _consent(pid, purpose_code="OTHER")

    def test_the_transition_table_has_no_way_back_from_revoked(self):
        assert ab.CONSENT_TRANSITIONS["revoked"] == frozenset()
        assert ab.CONSENT_TRANSITIONS["denied"] == frozenset()
        assert "granted" not in ab.CONSENT_TRANSITIONS["revoked"]


# ═════════════════════════════════════════════════════════════════════════════
#  5. THE WORK QUEUE
# ═════════════════════════════════════════════════════════════════════════════

class TestQueue:
    def test_an_encounter_with_no_abha_is_in_the_no_abha_bucket(self):
        pid, aid = _ensure_patient()
        queue = ab.unlinked_encounter_queue(limit=1000)
        entry = next(e for e in queue["encounters"] if e["encounter_ref"] == aid)
        assert entry["bucket"] == "no_abha"
        assert entry["verification_label"] == "No ABHA on record"
        assert entry["abha_number"] is None

    def test_an_unverified_abha_does_not_read_as_ready_to_link(self):
        pid, aid = _ensure_patient()
        ab.record_abha(patient_id=pid, abha_number=_next_abha(), recorded_by="ABDM-ADM")
        queue = ab.unlinked_encounter_queue(limit=1000)
        entry = next(e for e in queue["encounters"] if e["encounter_ref"] == aid)
        assert entry["bucket"] == "abha_unverified"
        assert entry["verification_label"] == "Not verified"

    def test_a_verified_abha_is_ready_to_link_and_leaves_when_linked(self):
        pid, aid = _ensure_patient()
        identity = _verified_identity(pid)
        entry = next(
            e for e in ab.unlinked_encounter_queue(limit=1000)["encounters"]
            if e["encounter_ref"] == aid
        )
        assert entry["bucket"] == "ready_to_link"

        cc = ab.link_care_context(
            identity_id=identity["id"], care_context_reference=aid,
            encounter_ref=aid, linked_by="MRD-CLERK-7",
        )
        assert not [
            e for e in ab.unlinked_encounter_queue(limit=1000)["encounters"]
            if e["encounter_ref"] == aid
        ]

        # ...and comes BACK when the link is undone. An unlinked episode is
        # unlinked work, not finished work.
        ab.unlink_care_context(
            care_context_id=cc["id"], reason="Linked against the wrong episode",
            actor_id="ABDM-ADM",
        )
        assert [
            e for e in ab.unlinked_encounter_queue(limit=1000)["encounters"]
            if e["encounter_ref"] == aid
        ]

    def test_the_queue_takes_no_department_from_the_request(self, desk):
        """The scope comes from the authenticated user. A `department=` query
        parameter here would be exactly the hole permissions.department_scope
        exists to close."""
        resp = client.get("/api/abdm/queue?department=Cardiology", headers=desk)
        assert resp.status_code == 200, resp.text
        import inspect
        from routes import abdm_routes
        assert "department" not in inspect.signature(abdm_routes.queue).parameters


# ═════════════════════════════════════════════════════════════════════════════
#  6. THE HONESTY — the wording, everywhere
# ═════════════════════════════════════════════════════════════════════════════

class TestNoTransmissionWording:
    def test_the_service_says_it(self):
        assert "not connected to the ABDM gateway" in ab.NO_GATEWAY_NOTICE
        assert "Nothing on this screen is transmitted" in ab.NO_GATEWAY_NOTICE
        assert "records what a person did elsewhere" in ab.NO_GATEWAY_NOTICE
        assert "nothing was sent to ABDM" in ab.NO_GATEWAY_SHORT

    def test_every_route_payload_carries_it(self, desk):
        """Not "the screen that shows ABHA numbers" — every payload, because
        payloads get consumed by exports, reports, curl and slides."""
        pid, aid = _ensure_patient()
        identity = _verified_identity(pid)
        cc = ab.link_care_context(
            identity_id=identity["id"], care_context_reference=aid,
            encounter_ref=aid, linked_by="MRD-CLERK-7",
        )
        artefact = _consent(pid)

        gets = [
            "/api/abdm/meta",
            "/api/abdm/summary",
            "/api/abdm/queue",
            f"/api/abdm/identities?patient_id={pid}",
            f"/api/abdm/identities/{identity['id']}",
            f"/api/abdm/care-contexts?patient_id={pid}",
            f"/api/abdm/consents?patient_id={pid}",
            f"/api/abdm/consents/{artefact['id']}",
        ]
        for path in gets:
            resp = client.get(path, headers=desk)
            assert resp.status_code == 200, f"{path}: {resp.text}"
            body = resp.json()
            assert body["notice"] == ab.NO_GATEWAY_NOTICE, path
            assert body["notice_short"] == ab.NO_GATEWAY_SHORT, path
            assert body["gateway_connected"] is False, path
            assert body["transmitted_to_abdm"] is False, path

        posts = [
            ("/api/abdm/check", {"abha_number": "12-3456-7890-1234"}),
            (f"/api/abdm/care-contexts/{cc['id']}/unlink", {"reason": "Wrong episode"}),
            (f"/api/abdm/consents/{artefact['id']}/denial", {"reason": "Patient said no"}),
        ]
        for path, body in posts:
            resp = client.post(path, json=body, headers=desk)
            assert resp.status_code == 200, f"{path}: {resp.text}"
            assert resp.json()["notice"] == ab.NO_GATEWAY_NOTICE, path
            assert resp.json()["gateway_connected"] is False, path

    def test_no_function_in_this_module_is_named_like_a_transmission(self):
        """`record_submission`, never `submit`. A clerk who believes Orb sent it
        does not send it, and the name is the first place that belief forms."""
        forbidden = ("send_", "submit_", "sync_", "push_", "transmit_", "fetch_from")
        for name in ab.__all__:
            assert not any(name.startswith(f) for f in forbidden), name

    def test_the_panel_renders_the_notice(self):
        """The words must be on screen before the first network round trip and
        still there when every fetch fails, so they are asserted in the SOURCE,
        not only in the API."""
        assert PANEL_PATH.exists(), f"{PANEL_PATH} is missing"
        source = PANEL_PATH.read_text(encoding="utf-8")
        assert "not connected to the ABDM gateway" in source
        assert "Nothing on this screen is transmitted" in source
        # And nothing on the panel may claim a gateway call happened.
        for lie in ("Sent to ABDM", "Submitted to ABDM", "Synced with ABDM"):
            assert lie not in source, lie

    def test_the_meta_payload_admits_what_orb_is_not(self, desk):
        body = client.get("/api/abdm/meta", headers=desk).json()
        assert body["checks_abha_check_digit"] is False
        assert "not an onboarded" in body["milestone_status"]


# ═════════════════════════════════════════════════════════════════════════════
#  7. THE MODULE SWITCH AND PHI SCOPE
# ═════════════════════════════════════════════════════════════════════════════

class TestModuleSwitchAndScope:
    def test_the_flag_defaults_on(self):
        assert config.ABDM_ENABLED is True

    def test_turning_it_off_404s_the_desk(self, desk, monkeypatch):
        """404, not 403: a site with ORB_ABDM=0 does not HAVE an ABDM desk, which
        is a different statement from 'you may not use it'."""
        monkeypatch.setattr(config, "ABDM_ENABLED", False)
        for path in ("/api/abdm/meta", "/api/abdm/summary", "/api/abdm/queue"):
            resp = client.get(path, headers=desk)
            assert resp.status_code == 404, path
            assert "not enabled" in resp.json()["detail"]

    def test_a_ward_scoped_desk_cannot_read_another_wards_abha(self):
        """`patient_id` on the QUERY STRING is the spelling that leaked six routes
        on 2026-08-05. enforce_patient_scope, and a 404 rather than a 403."""
        other_pid, _ = _ensure_patient(department="Cardiology")
        ab.record_abha(
            patient_id=other_pid, abha_number=_next_abha(), recorded_by="ABDM-ADM"
        )
        scoped = _login(_ensure_user("ABDM-ADM-MED", department="Medicine"))
        resp = client.get(f"/api/abdm/identities?patient_id={other_pid}", headers=scoped)
        assert resp.status_code == 404, resp.text

    def test_a_ward_scoped_desks_lists_are_filtered(self):
        mine, _ = _ensure_patient(department="Neurology")
        theirs, _ = _ensure_patient(department="Cardiology")
        for pid in (mine, theirs):
            ab.record_abha(patient_id=pid, abha_number=_next_abha(), recorded_by="ABDM-ADM")
        scoped = _login(_ensure_user("ABDM-ADM-NEURO", department="Neurology"))
        body = client.get("/api/abdm/identities?limit=1000", headers=scoped).json()
        seen = {row["patient_id"] for row in body["identities"]}
        assert mine in seen and theirs not in seen

    def test_an_abha_never_travels_in_a_url(self):
        """Every route that carries an ABHA value takes it in the BODY. In a
        query string it lands in the browser history, the proxy log and the
        access log — none of which are the encrypted database."""
        from routes import abdm_routes
        for route in abdm_routes.router.routes:
            assert "abha" not in route.path.lower(), route.path
        import inspect
        for fn in (abdm_routes.identities, abdm_routes.care_contexts, abdm_routes.consents,
                   abdm_routes.queue):
            params = set(inspect.signature(fn).parameters)
            assert not (params & {"abha_number", "abha_address"}), fn.__name__


# ═════════════════════════════════════════════════════════════════════════════
#  8. THE PROVE-CAN-FAIL TEST
# ═════════════════════════════════════════════════════════════════════════════

def test_revoked_consent_can_never_be_granted_again(desk):
    """THE ONE THAT MATTERS.

    Revocation is the only real control the ABDM stack gives a patient over their
    own data. A record that can quietly walk it back is worse than no record at
    all: it shows a live permission the patient believes they cancelled, and it
    shows it to whoever is deciding whether to release the data.

    The defence is that `CONSENT_TRANSITIONS['revoked']` is EMPTY and every
    status change in the module goes through `_consent_transition`, which
    consults it. There is no second write path — no "just set the column"
    helper, no bulk update, no route.

    PROVEN ABLE TO FAIL, and watched go red: adding `"granted"` to
    `CONSENT_TRANSITIONS["revoked"]` in services/abdm_service.py turns THIS test
    and `test_the_transition_table_has_no_way_back_from_revoked` red — the first
    of them on assertion 1 ("DID NOT RAISE IllegalConsentTransition"), because
    the service simply performs the re-grant: the artefact then reads
    `status: granted`, `is_effective: True`, and the revocation survives only as
    a stale timestamp in a column no screen shows. Done, observed, and restored
    on 2026-08-12.
    """
    pid, _ = _ensure_patient()
    artefact = _consent(pid)
    ab.record_consent_grant(consent_id=artefact["id"], artefact_id="CM-ART-REVOKE")
    ab.revoke_consent(
        consent_id=artefact["id"],
        reason="Patient withdrew consent in writing",
        actor_id="ABDM-ADM",
    )

    # 1. The service refuses, with the reason a human can read.
    with pytest.raises(ab.IllegalConsentTransition, match="REVOKED"):
        ab.record_consent_grant(consent_id=artefact["id"], artefact_id="CM-ART-NEW")

    # 2. The API refuses with 409 CONFLICT — a conflict with the artefact's
    #    state, not a malformed request — and hands the sentence to the screen.
    resp = client.post(
        f"/api/abdm/consents/{artefact['id']}/grant",
        json={"artefact_id": "CM-ART-NEW"},
        headers=desk,
    )
    assert resp.status_code == 409, resp.text
    assert "REVOKED" in resp.json()["detail"]
    assert "NEW artefact" in resp.json()["detail"], (
        "the refusal must tell the desk what to do instead, or somebody will "
        "edit the database"
    )

    # 3. Nothing moved: the stored row, the derived reading and the event trail
    #    all still say revoked.
    after = ab.get_consent(artefact["id"])
    assert after["status"] == "revoked"
    assert after["effective_status"] == "revoked"
    assert after["is_effective"] is False
    assert after["can_be_reinstated"] is False
    assert [e["event_type"] for e in after["events"]][-3:] == [
        "revoked", "granted", "request_recorded",
    ]

    # 4. There is no OTHER route that could do it either.
    from routes import abdm_routes
    paths = {r.path for r in abdm_routes.router.routes}
    assert not any(
        p.endswith(("/reinstate", "/unrevoke", "/restore", "/status")) for p in paths
    ), paths
