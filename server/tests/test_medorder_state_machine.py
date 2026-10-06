"""The medication-order state machine (medorders-1, W4-E).

Pinned here:
  * every legal and illegal transition pair, pure and through the writer; a
    hold remembers where it came from and a resume goes back only there;
  * the guarded write: a stale version or an illegal step writes nothing, a
    reason is required where the next nurse needs one, and an unwritable audit
    rolls the whole step back;
  * the ward projection: a hold keeps the ward status and sets the marker, a
    resume clears it, an end discontinues, a verifier is copied and never
    cleared (I1);
  * order_allows_dose, legacy_status_for, fhir_status_for, cpoe_status_for and
    the screen fingerprint;
  * the writers refuse to run outside a transaction;
  * enforce_medication_safety's exclude_med_id: an anticoagulant screened with
    its own chart row excluded raises no self-alert, and the default screens
    exactly as before.
"""
from __future__ import annotations

import itertools
import json
import uuid

import pytest

from database import db_session
from services.audit_service import AuditWriteError
from services.medorders import core

STATUSES = core.STATUSES


# ── fixtures and helpers ──────────────────────────────────────────────────────

_PATIENTS: list[str] = []


@pytest.fixture(scope="module", autouse=True)
def _cleanup_store_rows():
    """Remove what this module wrote to the store, so no later module meets
    orders it did not create."""
    yield
    if not _PATIENTS:
        return
    marks = ", ".join("?" for _ in _PATIENTS)
    with db_session() as db:
        db.execute(f"DELETE FROM med_order_events WHERE patient_id IN ({marks})", _PATIENTS)
        db.execute(f"DELETE FROM med_orders WHERE patient_id IN ({marks})", _PATIENTS)
        db.execute(f"UPDATE medications SET med_order_id = NULL, held_at = NULL, "
                   f"hold_reason = NULL WHERE patient_id IN ({marks})", _PATIENTS)


@pytest.fixture()
def patient():
    pid = f"PMOSM{uuid.uuid4().hex[:6].upper()}"
    with db_session() as db:
        db.execute("INSERT INTO patients (id, name, mrn) VALUES (?, ?, ?)",
                   (pid, "Order State Test", f"MRN-{uuid.uuid4().hex[:8]}"))
    _PATIENTS.append(pid)
    return pid


def _chart(patient_id, drug="Paracetamol", status="active", source="clinician",
           verified_by=None):
    with db_session() as db:
        cur = db.execute(
            "INSERT INTO medications (patient_id, drug_name, dosage, route, frequency, status, "
            "prescribed_by, source, verified_by) VALUES (?, ?, '500 mg', 'Oral', 'TDS', ?, "
            "'D001', ?, ?)",
            (patient_id, drug, status, source, verified_by))
        return cur.lastrowid


def _new_order(patient_id, status="prescribed", *, linked=True, drug="Paracetamol",
               is_prn=False, verified_by=None, idempotency_key=None):
    """An order in `status`, made through the writer. Held is reached by a
    hold from prescribed, since no order is born held."""
    start = "prescribed" if status == "held" else status
    ward_status = ("discontinued" if start in core.TERMINAL_STATUSES
                   else "prn" if is_prn else "active")
    med_id = _chart(patient_id, drug=drug, status=ward_status) if linked else None
    with db_session(immediate=True) as db:
        write = core.create_in_txn(
            db, patient_id=patient_id, drug_name=drug, source="chart", status=start,
            entered_by="D001", prescriber_id="D001", actor_id="D001", actor_role="doctor",
            entry_point="test.create", is_prn=is_prn,
            verified_by=verified_by or ("PH1" if start == "verified" else None),
            status_reason="ended for the test" if start in core.TERMINAL_STATUSES else None,
            idempotency_key=idempotency_key)
        order = write.order
        if med_id is not None and start != "draft":
            core.link_projection_in_txn(db, order["id"], med_id)
            core.project_in_txn(db, core.get_order_in_txn(db, order["id"]))
        if status == "held":
            order = core.transition_in_txn(
                db, order["id"], "held", expected_version=None, actor_id="D001",
                actor_role="doctor", entry_point="test.hold", reason="nil by mouth").order
        return core.get_order_in_txn(db, order["id"]), med_id


def _move(order_id, to_status, *, expected_version=None, actor="D001", reason="clinical reason",
          **kw):
    with db_session(immediate=True) as db:
        return core.transition_in_txn(
            db, order_id, to_status, expected_version=expected_version, actor_id=actor,
            actor_role="doctor", entry_point="test.move", reason=reason, **kw)


def _order(order_id):
    with db_session() as db:
        return core.get_order_in_txn(db, order_id)


def _events(order_id):
    with db_session() as db:
        return core.events_in_txn(db, order_id)


def _ward_row(med_id):
    with db_session() as db:
        return dict(db.execute("SELECT * FROM medications WHERE id = ?", (med_id,)).fetchone())


def _audits(order_id):
    with db_session() as db:
        return [dict(r) for r in db.execute(
            "SELECT * FROM audit_logs WHERE target_type = 'med_order' AND target_id = ? "
            "ORDER BY id", (order_id,)).fetchall()]


# ── 1. The transition table, pure ─────────────────────────────────────────────

EXPECTED = {
    ("draft", "prescribed"), ("draft", "cancelled"),
    ("prescribed", "verified"), ("prescribed", "rejected"), ("prescribed", "held"),
    ("prescribed", "stopped"), ("prescribed", "cancelled"),
    ("verified", "held"), ("verified", "stopped"), ("verified", "completed"),
    ("held", "stopped"),
}


@pytest.mark.parametrize("src,dst", list(itertools.product(STATUSES, STATUSES)))
def test_every_pair_is_legal_exactly_when_the_design_says(src, dst):
    if src == "held":
        for held_from in core.HOLDABLE_FROM:
            legal = (dst == "stopped") or (dst == held_from)
            assert core.is_legal_transition(src, dst, held_from=held_from) is legal, (
                src, dst, held_from)
    else:
        assert core.is_legal_transition(src, dst) is ((src, dst) in EXPECTED), (src, dst)


def test_the_final_states_go_nowhere():
    for status in core.TERMINAL_STATUSES:
        assert core.LEGAL_TRANSITIONS[status] == frozenset()
    assert set(core.LEGAL_TRANSITIONS) == set(STATUSES)


def test_the_store_statuses_equal_the_migration_check():
    from migrations_his.m0061_med_order_store import STATUSES as MIGRATION_STATUSES
    assert core.STATUSES == MIGRATION_STATUSES


def test_event_kinds_for_each_step():
    assert core.event_kind_for("draft", "prescribed") == "signed"
    assert core.event_kind_for("held", "prescribed") == "resumed"
    assert core.event_kind_for("held", "verified") == "resumed"
    for dst in ("verified", "rejected", "held", "stopped", "completed", "cancelled"):
        assert core.event_kind_for("prescribed", dst) == dst
        assert dst in core.EVENT_KINDS


def test_the_unverified_sources_are_one_list_everywhere():
    from services import emar_service, pharmacy_verification_service
    assert core.UNVERIFIED_SOURCES == pharmacy_verification_service._UNVERIFIED_SOURCES
    assert core.UNVERIFIED_SOURCES == emar_service._UNVERIFIED_SOURCES


# ── 2. The transition table, through the writer ───────────────────────────────

LEGAL_STEPS = sorted(EXPECTED | {("held", "prescribed"), ("held", "verified")})


@pytest.mark.parametrize("src,dst", LEGAL_STEPS)
def test_every_legal_step_writes_order_event_and_audit(patient, src, dst):
    held_from = "verified" if (src, dst) == ("held", "verified") else "prescribed"
    if src == "held" and held_from == "verified":
        order, _ = _new_order(patient, "verified")
        order = _move(order["id"], "held", reason="procedure").order
    else:
        order, _ = _new_order(patient, src)
    before_events = len(_events(order["id"]))
    write = _move(order["id"], dst, expected_version=order["version"])
    after = write.order
    assert after["status"] == dst
    assert after["version"] == order["version"] + 1
    assert after["held_from"] == (src if dst == "held" else None)
    events = _events(order["id"])
    assert len(events) == before_events + 1
    assert events[-1]["kind"] == core.event_kind_for(src, dst)
    assert (events[-1]["from_status"], events[-1]["to_status"]) == (src, dst)
    audit = _audits(order["id"])[-1]
    assert audit["event_category"] == "clinical_access"
    assert audit["action_type"] == f"med_order_{core.event_kind_for(src, dst)}"
    assert audit["target_id"] == order["id"]
    meta = json.loads(audit["metadata_json"])
    assert (meta["from"], meta["to"], meta["event_id"]) == (src, dst, write.event_id)


@pytest.mark.parametrize("src,dst", [
    (s, d) for s, d in itertools.product(STATUSES, STATUSES)
    if s != "held" and (s, d) not in EXPECTED
])
def test_every_illegal_step_is_refused_and_writes_nothing(patient, src, dst):
    order, _ = _new_order(patient, src)
    before = (_order(order["id"]), len(_events(order["id"])), len(_audits(order["id"])))
    with pytest.raises(core.TransitionNotAllowed) as exc:
        _move(order["id"], dst)
    assert exc.value.http_status == 409
    assert (_order(order["id"]), len(_events(order["id"])), len(_audits(order["id"]))) == before


def test_held_from_round_trips_and_a_resume_goes_back_only_there(patient):
    for origin in core.HOLDABLE_FROM:
        order, _ = _new_order(patient, origin)
        held = _move(order["id"], "held", reason="nil by mouth").order
        assert (held["status"], held["held_from"]) == ("held", origin)
        other = "verified" if origin == "prescribed" else "prescribed"
        with pytest.raises(core.TransitionNotAllowed):
            _move(order["id"], other)
        resumed = _move(order["id"], origin, reason="eating again").order
        assert (resumed["status"], resumed["held_from"]) == (origin, None)
        assert _events(order["id"])[-1]["kind"] == "resumed"


def test_a_stale_version_is_refused_and_writes_nothing(patient):
    order, _ = _new_order(patient, "prescribed")
    _move(order["id"], "held", reason="first change")
    current = _order(order["id"])
    with pytest.raises(core.StaleOrderVersion) as exc:
        _move(order["id"], "stopped", expected_version=order["version"])
    assert exc.value.http_status == 409
    assert core.http_detail(exc.value)["order"]["version"] == current["version"]
    assert _order(order["id"]) == current


@pytest.mark.parametrize("dst", ["held", "stopped", "cancelled", "rejected"])
def test_a_step_that_changes_what_the_ward_does_needs_a_reason(patient, dst):
    order, _ = _new_order(patient, "prescribed")
    for blank in (None, "", "   "):
        with pytest.raises(core.NeedsInput) as exc:
            _move(order["id"], dst, reason=blank)
        assert exc.value.code == "reason_required"
    assert _order(order["id"])["status"] == "prescribed"


def test_a_resume_needs_a_reason_too(patient):
    order, _ = _new_order(patient, "held")
    with pytest.raises(core.NeedsInput):
        _move(order["id"], "prescribed", reason=" ")


def test_sign_names_the_signer_as_prescriber_and_sets_the_ward_status(patient):
    order, _ = _new_order(patient, "draft", linked=False, is_prn=True)
    assert order["projection_status"] is None
    signed = _move(order["id"], "prescribed", actor="D002", reason=None).order
    assert (signed["prescriber_id"], signed["projection_status"]) == ("D002", "prn")
    assert signed["prescribed_at"]


def test_verify_records_the_verifier_and_neither_step_may_be_anonymous(patient):
    order, _ = _new_order(patient, "prescribed")
    with pytest.raises(core.MedOrderInvalid):
        _move(order["id"], "verified", actor=None)
    verified = _move(order["id"], "verified", actor="PH9", reason=None).order
    assert verified["verified_by"] == "PH9" and verified["verified_at"]


def test_an_unwritable_audit_rolls_the_whole_step_back(patient, monkeypatch):
    order, med_id = _new_order(patient, "prescribed")
    before = (_order(order["id"]), len(_events(order["id"])), _ward_row(med_id))

    def refuse(*a, **k):
        raise AuditWriteError("audit store unavailable")

    monkeypatch.setattr(core, "log_audit_event", refuse)
    with pytest.raises(AuditWriteError):
        _move(order["id"], "held", reason="nil by mouth")
    assert (_order(order["id"]), len(_events(order["id"])), _ward_row(med_id)) == before


def test_a_repeated_idempotency_key_replays_without_writing(patient):
    order, _ = _new_order(patient, "prescribed")
    first = _move(order["id"], "held", reason="theatre", idempotency_key="hold-key-1")
    again = _move(order["id"], "held", reason="theatre", idempotency_key="hold-key-1")
    assert again.replayed and again.event_id == first.event_id
    assert again.order["version"] == first.order["version"]
    other, _ = _new_order(patient, "prescribed")
    with pytest.raises(core.MedOrderConflict):
        _move(other["id"], "held", reason="theatre", idempotency_key="hold-key-1")


# ── 3. The ward projection ────────────────────────────────────────────────────

def test_a_hold_keeps_the_ward_status_and_sets_the_marker(patient):
    order, med_id = _new_order(patient, "prescribed")
    held = _move(order["id"], "held", reason="nil by mouth").order
    row = _ward_row(med_id)
    assert row["status"] == "active"
    assert row["held_at"] == held["status_changed_at"]
    assert row["hold_reason"] == "nil by mouth"
    _move(order["id"], "prescribed", reason="eating again")
    row = _ward_row(med_id)
    assert (row["status"], row["held_at"], row["hold_reason"]) == ("active", None, None)


def test_a_prn_order_keeps_prn_on_the_ward_through_a_hold(patient):
    order, med_id = _new_order(patient, "prescribed", is_prn=True)
    _move(order["id"], "held", reason="drowsy")
    assert _ward_row(med_id)["status"] == "prn"


@pytest.mark.parametrize("end", ["stopped", "cancelled", "rejected"])
def test_an_ended_order_discontinues_its_ward_row(patient, end):
    order, med_id = _new_order(patient, "prescribed")
    after = _move(order["id"], end, reason="no longer needed").order
    assert after["projection_status"] is None
    row = _ward_row(med_id)
    assert (row["status"], row["held_at"]) == ("discontinued", None)


def test_the_verifier_is_copied_to_the_ward_row_and_never_cleared(patient):
    order, med_id = _new_order(patient, "prescribed")
    _move(order["id"], "verified", actor="PH7", reason=None)
    assert _ward_row(med_id)["verified_by"] == "PH7"
    _move(order["id"], "held", reason="procedure")
    _move(order["id"], "verified", reason="procedure done")
    _move(order["id"], "stopped", reason="course changed")
    assert _ward_row(med_id)["verified_by"] == "PH7"


def test_a_draft_has_no_ward_row():
    assert core.projection_for({"status": "draft"}) is None


def test_link_refuses_a_draft_and_a_row_that_says_something_else(patient):
    draft, _ = _new_order(patient, "draft", linked=False)
    live_row = _chart(patient)
    with db_session(immediate=True) as db:
        with pytest.raises(core.MedOrderConflict) as exc:
            core.link_projection_in_txn(db, draft["id"], live_row)
    assert exc.value.code == "draft_has_no_ward_row"
    stopped, _ = _new_order(patient, "stopped", linked=False)
    with db_session(immediate=True) as db:
        with pytest.raises(core.MedOrderConflict) as exc:
            core.link_projection_in_txn(db, stopped["id"], live_row)
    assert exc.value.code == "projection_mismatch"
    assert _ward_row(live_row)["med_order_id"] is None


def test_link_refuses_a_second_order_or_another_patients_row(patient):
    order, med_id = _new_order(patient, "prescribed")
    other, _ = _new_order(patient, "prescribed", linked=False)
    with db_session(immediate=True) as db:
        with pytest.raises(core.MedOrderConflict):
            core.link_projection_in_txn(db, other["id"], med_id)
    second = f"PMOSM{uuid.uuid4().hex[:6].upper()}"
    with db_session() as db:
        db.execute("INSERT INTO patients (id, name, mrn) VALUES (?, 'Other', ?)",
                   (second, f"MRN-{uuid.uuid4().hex[:8]}"))
    _PATIENTS.append(second)
    foreign = _chart(second)
    with db_session(immediate=True) as db:
        with pytest.raises(core.MedOrderConflict) as exc:
            core.link_projection_in_txn(db, other["id"], foreign)
    assert exc.value.code == "projection_patient_mismatch"


# ── 4. The pure readers ───────────────────────────────────────────────────────

@pytest.mark.parametrize("status,allowed,code", [
    ("prescribed", True, None), ("verified", True, None),
    ("held", False, "order_held"), ("draft", False, "order_not_signed"),
    ("stopped", False, "order_ended"), ("completed", False, "order_ended"),
    ("cancelled", False, "order_ended"), ("rejected", False, "order_ended"),
    ("bogus", False, "order_state_unknown"),
])
def test_order_allows_dose(status, allowed, code):
    order = {"status": status, "status_changed_by": "D001",
             "status_changed_at": "2026-10-04T08:00:00Z", "status_reason": "nil by mouth"}
    verdict = core.order_allows_dose(order, "given")
    assert (verdict["allowed"], verdict["code"]) == (allowed, code)
    if not allowed:
        assert verdict["message"] and "\u2014" not in verdict["message"]
    if status == "held":
        assert verdict["hold_reason"] == "nil by mouth" and verdict["held_by"] == "D001"
        assert "not given (held)" in verdict["message"]
    # Withholding is always allowed, a held order included.
    assert core.order_allows_dose(order, "not_given")["allowed"] is True


@pytest.mark.parametrize("legacy,source,verified_by,expected", [
    ("active", "clinician", None, ("prescribed", "active", 0)),
    ("active", "clinician", "PH1", ("verified", "active", 0)),
    ("prn", "clinician", None, ("prescribed", "prn", 0)),
    ("active", "llm_extracted", None, ("prescribed", "active", 1)),
    ("active", "agentic", "PH1", ("verified", "active", 1)),
    ("discontinued", "llm_extracted", "PH1", ("rejected", None, 1)),
    ("discontinued", "agentic", "PH1", ("rejected", None, 1)),
    ("discontinued", "llm_extracted", None, ("stopped", None, 1)),
    ("discontinued", "clinician", "PH1", ("stopped", None, 0)),
    ("discontinued", "clinician", None, ("stopped", None, 0)),
])
def test_legacy_status_for(legacy, source, verified_by, expected):
    got = core.legacy_status_for({"status": legacy, "source": source,
                                  "verified_by": verified_by, "verified_at": "t"})
    assert (got["status"], got["projection_status"], got["needs_verification"]) == expected
    assert got["adoptable"] is True
    if got["status"] == "stopped":
        assert got["status_reason"] == core.LEGACY_STOPPED_REASON
    assert got["is_prn"] == (1 if legacy == "prn" else 0)


def test_legacy_status_for_never_guesses_an_unknown_status():
    for status in (None, "", "held", "Active"):
        assert core.legacy_status_for({"status": status})["adoptable"] is False


def test_fhir_and_cpoe_statuses():
    assert core.fhir_status_for("held") == {"status": "on-hold", "status_reason": None}
    assert core.fhir_status_for("rejected") == {"status": "cancelled", "status_reason": "rejected"}
    assert core.fhir_status_for("verified")["status"] == "active"
    orders_check = ("draft", "active", "in_progress", "completed", "resulted", "cancelled")
    for status in STATUSES:
        assert core.cpoe_status_for(status) in orders_check
        assert core.fhir_status_for(status)["status"] in (
            "draft", "active", "on-hold", "stopped", "completed", "cancelled")
    with pytest.raises(core.MedOrderInvalid):
        core.cpoe_status_for("nope")


def test_the_screen_fingerprint_is_stable_and_moves_with_what_the_screen_reads():
    allergy = {"id": 3, "substance": "penicillin", "substance_class": "penicillins",
               "reaction": "rash", "severity": "mild"}
    base = core.screen_fingerprint([allergy], ["o1", "o2"], [5])
    assert base == core.screen_fingerprint([dict(allergy)], ["o2", "o1"], [5])
    assert base.startswith("v1:")
    assert base != core.screen_fingerprint([{**allergy, "reaction": "anaphylaxis"}],
                                           ["o1", "o2"], [5])
    assert base != core.screen_fingerprint([allergy], ["o1"], [5])
    assert base != core.screen_fingerprint([allergy], ["o1", "o2"], [5, 6])
    assert base != core.screen_fingerprint([], ["o1", "o2"], [5])


def test_the_fingerprint_reader_sees_a_new_allergy_and_a_new_drug(patient):
    with db_session() as db:
        first = core.screen_fingerprint_in_txn(db, patient)
    with db_session() as db:
        db.execute("INSERT INTO patient_allergies (patient_id, substance, severity) "
                   "VALUES (?, 'penicillin', 'severe')", (patient,))
    with db_session() as db:
        second = core.screen_fingerprint_in_txn(db, patient)
    assert second != first
    _chart(patient, drug="Aspirin")
    with db_session() as db:
        third = core.screen_fingerprint_in_txn(db, patient)
    assert third != second
    order, _ = _new_order(patient, "prescribed", linked=False)
    with db_session() as db:
        fourth = core.screen_fingerprint_in_txn(db, patient)
    assert fourth != third
    _move(order["id"], "stopped", reason="done")
    with db_session() as db:
        assert core.screen_fingerprint_in_txn(db, patient) == third


# ── 5. Writers refuse to run outside a transaction ────────────────────────────

def test_the_writers_refuse_an_autocommit_connection(patient):
    with db_session() as db:
        assert not db.in_transaction
        with pytest.raises(core.StoreUsageError):
            core.create_in_txn(db, patient_id=patient, drug_name="Aspirin", source="chart",
                               status="prescribed", entered_by="D001", actor_id="D001",
                               entry_point="test")
        with pytest.raises(core.StoreUsageError):
            core.transition_in_txn(db, "missing", "held", expected_version=None,
                                   actor_id="D001", actor_role="doctor", entry_point="test",
                                   reason="x")
        with pytest.raises(core.StoreUsageError):
            core.record_event_in_txn(db, order={"id": "x", "patient_id": patient},
                                     kind="created")


# ── 6. Creating an order ──────────────────────────────────────────────────────

@pytest.mark.parametrize("bad,code", [
    ({"status": "held"}, "invalid_status"),
    ({"source": "telepathy"}, "invalid_source"),
    ({"drug_name": "  "}, "drug_name_required"),
    ({"status": "verified"}, "verified_by_required"),
    ({"priority": "asap"}, "invalid_priority"),
    ({"supply_source": "shop"}, "invalid_supply_source"),
    ({"status": "stopped", "projection_status": "active"}, "invalid_projection_status"),
    ({"encounter_type": "er"}, "invalid_encounter_type"),
])
def test_create_refuses_what_the_store_cannot_hold(patient, bad, code):
    args = dict(patient_id=patient, drug_name="Aspirin", source="chart", status="prescribed",
                entered_by="D001", actor_id="D001", entry_point="test")
    args.update(bad)
    with db_session(immediate=True) as db:
        with pytest.raises(core.MedOrderError) as exc:
            core.create_in_txn(db, **args)
    assert exc.value.code == code


def test_create_writes_the_order_its_event_and_audit(patient):
    order, _ = _new_order(patient, "prescribed", linked=False, drug="Ondansetron")
    assert order["version"] == 1 and order["projection_status"] == "active"
    from services.clinical_time import to_utc_iso
    assert order["created_at"] == to_utc_iso(order["created_at"])  # the canonical spelling
    assert order["prescribed_at"]
    assert [e["kind"] for e in _events(order["id"])] == ["created"]
    audit = _audits(order["id"])
    assert len(audit) == 1 and audit[0]["action_type"] == "med_order_created"
    assert audit[0]["event_category"] == "clinical_access"


def test_create_replays_a_repeated_key_and_refuses_it_for_another_drug(patient):
    key = f"idem-{uuid.uuid4().hex}"
    first, _ = _new_order(patient, "prescribed", linked=False, idempotency_key=key)
    with db_session(immediate=True) as db:
        again = core.create_in_txn(db, patient_id=patient, drug_name="Paracetamol",
                                   source="chart", status="prescribed", entered_by="D001",
                                   actor_id="D001", entry_point="test", idempotency_key=key)
        assert again.replayed and again.order["id"] == first["id"]
        with pytest.raises(core.MedOrderConflict):
            core.create_in_txn(db, patient_id=patient, drug_name="Aspirin", source="chart",
                               status="prescribed", entered_by="D001", actor_id="D001",
                               entry_point="test", idempotency_key=key)


# ── 7. The gate's exclusion passthrough ───────────────────────────────────────

def _interaction_drugs(safety):
    return [a["with"] for a in safety["interaction_alerts"]]


def test_an_anticoagulant_screened_with_its_own_row_excluded_does_not_alert_on_itself(patient):
    from services.medication_safety_gate import enforce_medication_safety
    med_id = _chart(patient, drug="Enoxaparin")
    excluded = enforce_medication_safety(patient, "Enoxaparin", actor_id="D001",
                                         entry_point="test.exclude", interactive=False,
                                         exclude_med_id=med_id)
    assert "Enoxaparin" not in _interaction_drugs(excluded)
    assert excluded["max_severity"] != "severe" and not excluded["blocking"]


def test_without_the_exclusion_the_gate_screens_exactly_as_before(patient):
    """The default (None) still screens the drug against every active row, its
    own included: today's behaviour, unchanged for every existing caller."""
    from services.medication_safety_gate import MedicationSafetyBlocked, enforce_medication_safety
    from services.drug_safety_service import check_medication_safety
    _chart(patient, drug="Enoxaparin")
    with pytest.raises(MedicationSafetyBlocked) as exc:
        enforce_medication_safety(patient, "Enoxaparin", actor_id="D001",
                                  entry_point="test.default", interactive=False)
    assert "Enoxaparin" in _interaction_drugs(exc.value.safety)
    direct = check_medication_safety(patient, "Enoxaparin")
    assert direct["interaction_alerts"] == exc.value.safety["interaction_alerts"]


def test_the_exclusion_is_on_the_audit_row_only_when_used(patient):
    from services.medication_safety_gate import enforce_medication_safety
    med_id = _chart(patient, drug="Paracetamol")
    enforce_medication_safety(patient, "Paracetamol", actor_id="D001",
                              entry_point="test.audit.none", interactive=False)
    enforce_medication_safety(patient, "Paracetamol", actor_id="D001",
                              entry_point="test.audit.excluded", interactive=False,
                              exclude_med_id=med_id)
    with db_session() as db:
        rows = {r["entry"]: json.loads(r["metadata_json"]) for r in db.execute(
            "SELECT json_extract(metadata_json, '$.entry_point') AS entry, metadata_json "
            "FROM audit_logs WHERE patient_id = ? AND event_category = 'clinical_safety'",
            (patient,)).fetchall()}
    assert "excluded_medication_id" not in rows["test.audit.none"]
    assert rows["test.audit.excluded"]["excluded_medication_id"] == med_id


def test_the_exclusion_does_not_touch_the_allergy_match(patient):
    from services.medication_safety_gate import MedicationSafetyBlocked, enforce_medication_safety
    with db_session() as db:
        db.execute("INSERT INTO patient_allergies (patient_id, substance, substance_class, "
                   "reaction, severity) VALUES (?, 'penicillin', 'penicillins', "
                   "'anaphylaxis', 'severe')", (patient,))
    med_id = _chart(patient, drug="Amoxicillin")
    with pytest.raises(MedicationSafetyBlocked) as exc:
        enforce_medication_safety(patient, "Amoxicillin", actor_id="D001",
                                  entry_point="test.allergy", interactive=False,
                                  exclude_med_id=med_id)
    assert exc.value.safety["allergy_alerts"]
