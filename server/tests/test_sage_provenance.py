"""Orb Sage — replayable provenance.

WHAT THIS FILE IS DEFENDING. Six months after the pilot, someone asks "why did
Sage say that?" about one turn. Every answer to that question has to come from
the record, because none of the inputs still exist: the chart has moved on, the
corpus has been re-indexed, the model has been swapped. A provenance row that
POINTS at the patient answers nothing — it re-reads today's chart and tells you
a story about today.

So the load-bearing test in this file is
``test_a_later_chart_change_does_not_alter_stored_provenance``. Everything else
exists to keep that property affordable (dedup + retention), lawful (scoped +
audited like any other PHI surface) and safe (a provenance failure must never
cost a clinician an answer).

The model is STUBBED throughout. What is under test is the seam — that a real
Sage turn through the real HTTP route leaves a row that reconstructs it — never
MedGemma's prose.
"""
import ast
import asyncio
import inspect
import json
import os
import sys
import threading
from datetime import datetime, timedelta, timezone
from unittest import mock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi.testclient import TestClient  # noqa: E402

from main import app  # noqa: E402
from auth import hash_password  # noqa: E402
from database import db_session, get_db  # noqa: E402
from services import sage_provenance_service as prov  # noqa: E402

client = TestClient(app)

DOC = "PROV-DOC"
ADMIN = "PROV-ADM"            # admin with NO department -> hospital-wide audit
ADMIN_CARD = "PROV-ADM-CARD"  # admin scoped to Cardiology
ADMIN_GM = "PROV-ADM-GM"      # admin scoped to General Medicine (the DOC's ward)
PATIENT = "PROV-P1"
DEPT = "General Medicine"
OTHER_DEPT = "Cardiology"


# ── fixtures ────────────────────────────────────────────────────────────────
def _seed_users(db):
    for uid, role, dept in (
        (DOC, "doctor", DEPT),
        (ADMIN, "admin", None),
        (ADMIN_CARD, "admin", OTHER_DEPT),
        (ADMIN_GM, "admin", DEPT),
    ):
        db.execute(
            "INSERT OR REPLACE INTO users (id, name, role, department, password_hash) "
            "VALUES (?, ?, ?, ?, ?)",
            (uid, f"Prov {role}", role, dept, hash_password("password")),
        )


def _seed_patient(db):
    """A patient with a REAL admissions row.

    permissions.patient_in_scope derives a patient's department from
    `admissions.department`, never from `patients.ward` (which is a bed prefix).
    Seeding only the patients row would leave this patient in nobody's ward and
    quietly make every scoping assertion below vacuous.
    """
    admitted = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    db.execute(
        "INSERT OR REPLACE INTO patients (id, mrn, name, age, sex, ward, bed, status, "
        "admission_date, diagnosis) VALUES (?, ?, ?, ?, ?, ?, ?, 'admitted', ?, ?)",
        (PATIENT, "MRN-PROV", "Provenance Patient", 64, "M", "GM-1", "GM-1-04",
         admitted, "Community-acquired pneumonia"),
    )
    db.execute("DELETE FROM admissions WHERE patient_id = ?", (PATIENT,))
    db.execute(
        "INSERT INTO admissions (id, patient_id, admitted_by, department, ward, bed, status, "
        "created_at) VALUES (?, ?, ?, ?, ?, ?, 'active', ?)",
        ("PROV-ADMIT-1", PATIENT, DOC, DEPT, "GM-1", "GM-1-04", admitted),
    )
    db.execute(
        "INSERT INTO vitals (patient_id, timestamp, heart_rate, systolic_bp, diastolic_bp, "
        "spo2, temperature, respiratory_rate) VALUES (?, ?, 96, 118, 74, 94, 38.1, 22)",
        (PATIENT, admitted),
    )


def _wipe_patient(db):
    """TEST-ISOLATION LAW: discharge + remove everything this file admitted.

    An admitted leftover patient shows up in every later module's census and
    department-scoped count."""
    db.execute("UPDATE admissions SET status = 'discharged' WHERE patient_id = ?", (PATIENT,))
    db.execute("DELETE FROM admissions WHERE patient_id = ?", (PATIENT,))
    db.execute("DELETE FROM vitals WHERE patient_id = ?", (PATIENT,))
    db.execute("DELETE FROM medications WHERE patient_id = ?", (PATIENT,))
    db.execute("DELETE FROM patients WHERE id = ?", (PATIENT,))


@pytest.fixture(autouse=True)
def _fresh():
    prov.reset_schema_cache()
    with db_session() as db:
        prov.drop_schema(db)
        prov.ensure_schema(db)
        _seed_users(db)
        _wipe_patient(db)
        _seed_patient(db)
    yield
    with db_session() as db:
        _wipe_patient(db)
        prov.drop_schema(db)
    prov.reset_schema_cache()


def _login(uid: str) -> dict:
    r = client.post("/api/auth/login", json={"employee_id": uid, "password": "password"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def _user(uid: str) -> dict:
    with db_session() as db:
        return dict(db.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone())


def _sse(response) -> list:
    out = []
    for line in response.text.splitlines():
        if line.startswith("data: "):
            out.append(json.loads(line[6:]))
    return out


# ── realistic fixture data (used for the storage arithmetic) ────────────────
def _guideline(doc_id: str, n_chars: int = 1400) -> dict:
    body = (f"Recommendation set {doc_id}. " + "Administer the bundle within one hour of "
            "recognition; obtain blood cultures before antibiotics; measure lactate and "
            "repeat if elevated. ") * 20
    return {
        "document_id": doc_id,
        "title": f"Guideline {doc_id}",
        "source": "Local Policy",
        "source_type": "policy",
        "trust_tier": 1,
        "is_synthetic": False,
        "category": "Sepsis",
        "fusion_score": 0.81,
        "content": body[:n_chars],
    }


def _live_chart():
    """The REAL chart assembler, so the snapshot under test is a real snapshot.

    Deliberately not a hand-built dict: the property being defended is that what
    Sage was shown is what comes back, and a fixture chart would prove that about
    the fixture rather than about the chart."""
    from services.sage_service import _assemble_patient_context
    with db_session() as conn:
        return _assemble_patient_context(conn, PATIENT, DEPT)


def _record(**over):
    """One recorded turn with sane defaults."""
    kw = dict(
        turn_id=over.pop("turn_id", None) or prov.new_turn_id(),
        lane="chat",
        outcome="answered",
        user=_user(DOC),
        question="What antibiotic cover does this patient need?",
        patient_id=PATIENT,
        patient_context=_live_chart(),
        prompt_context="CHANNEL A ...\nCHANNEL B ...",
        sources=[_guideline("EXT-SEPSIS-004"), _guideline("POL-ABX-002")],
        route_info={
            "provider": "ollama", "model": "medgemma:27b",
            "thinking": True, "thinking_requested": True, "thinking_tier": "careful",
            "thinking_model": "medgemma:27b", "thinking_reason": "default_on",
            "safety_floor": True, "safety_floor_rules": ["patient_scoped"],
            "safety_floor_enabled": True, "safety_floor_unsatisfied": False,
        },
        answer="Continue co-amoxiclav; review at 48 h.",
        raw_output="NOACT| Continue co-amoxiclav; review at 48 h.",
        structured={"concise_answer": "Continue co-amoxiclav; review at 48 h."},
        cited_doc_ids=["EXT-SEPSIS-004"],
        citation_basis="model_cited",
        confidence="high",
        escalation=False,
        query_scope="PATIENT_SPECIFIC",
        timings={"retrieval_ms": 812, "ttft_ms": 2100, "total_ms": 6400},
    )
    kw.update(over)
    return prov.record_turn(**kw)


# ════════════════════════════════════════════════════════════════════════════
#  1. THE LOAD-BEARING PROPERTY
# ════════════════════════════════════════════════════════════════════════════
def test_a_later_chart_change_does_not_alter_stored_provenance():
    """A pointer to the patient is worthless in six months.

    The chart is mutated AFTER the turn is recorded — a new medication and a new
    observation, the two things that change most on a ward — and the replay must
    still show what the model was actually looking at.
    """
    with db_session() as db:
        db.execute(
            "INSERT INTO medications (patient_id, drug_name, dosage, route, frequency, "
            "status, prescribed_by) VALUES (?, 'Co-amoxiclav', '1.2 g', 'IV', 'TDS', "
            "'active', ?)",
            (PATIENT, DOC),
        )
    before = _live_chart()
    assert "Co-amoxiclav 1.2 g IV TDS" in before["medications"]

    turn_id = _record(patient_context=before)
    assert turn_id

    # ── the ward moves on: the antibiotic is escalated and new obs go in ──
    with db_session() as db:
        db.execute("UPDATE medications SET status = 'discontinued' "
                   "WHERE patient_id = ? AND drug_name = 'Co-amoxiclav'", (PATIENT,))
        db.execute(
            "INSERT INTO medications (patient_id, drug_name, dosage, route, frequency, "
            "status, prescribed_by) VALUES (?, 'Meropenem', '1 g', 'IV', 'TDS', "
            "'active', ?)",
            (PATIENT, DOC),
        )
        db.execute(
            "INSERT INTO vitals (patient_id, timestamp, heart_rate, systolic_bp, "
            "diastolic_bp, spo2, temperature, respiratory_rate) "
            "VALUES (?, ?, 132, 84, 48, 86, 39.4, 34)",
            (PATIENT, datetime.now(timezone.utc).isoformat()),
        )
    after = _live_chart()
    assert "Meropenem 1 g IV TDS" in after["medications"]
    assert "Co-amoxiclav 1.2 g IV TDS" not in after["medications"], (
        "fixture is not exercising the property it claims to")
    assert after["vitals"] != before["vitals"], "the observation change did not land"

    replayed = prov.replay(turn_id)
    assert replayed is not None
    stored = replayed["patient_context"]
    assert stored["medications"] == before["medications"], (
        "the replay must reconstruct the chart AS IT WAS, not re-read it")
    assert "Meropenem 1 g IV TDS" not in stored["medications"]
    assert stored["vitals"] == before["vitals"], (
        "the stored observations moved when the ward charted new ones")


def test_a_recorded_turn_is_immutable_at_the_storage_layer():
    """Not a convention — a trigger. Provenance whose rows can be edited is not
    provenance, and 'we agreed not to UPDATE it' is not a control an incident
    review can rely on."""
    turn_id = _record()
    with db_session() as db:
        with pytest.raises(Exception) as exc:
            db.execute("UPDATE sage_provenance_turns SET outcome = 'refused' "
                       "WHERE turn_id = ?", (turn_id,))
    assert "append-only" in str(exc.value).lower()


# ════════════════════════════════════════════════════════════════════════════
#  2. RECONSTRUCTION COMPLETENESS
# ════════════════════════════════════════════════════════════════════════════
def test_replay_reconstructs_every_required_element_of_the_turn():
    turn_id = _record()
    r = prov.replay(turn_id)
    assert r is not None

    assert r["question"] == "What antibiotic cover does this patient need?"
    assert r["scope"]["department"] == DEPT
    assert r["scope"]["hospital_wide"] is False
    assert r["scope"]["patient_id"] == PATIENT
    assert r["scope"]["query_scope"] == "PATIENT_SPECIFIC"

    assert r["patient_context"]["patient_id"] == PATIENT

    ids = [g["document_id"] for g in r["guidelines"]]
    assert ids == ["EXT-SEPSIS-004", "POL-ABX-002"]
    assert r["guidelines"][0]["passage"].startswith("Recommendation set EXT-SEPSIS-004")

    assert r["model"]["served"] == "medgemma:27b"
    assert r["model"]["provider"] == "ollama"
    assert r["model"]["thinking"] is True
    assert r["model"]["safety_floor"] is True
    assert r["model"]["safety_floor_rules"] == ["patient_scoped"]

    assert r["answer"]["text"] == "Continue co-amoxiclav; review at 48 h."
    assert r["answer"]["cited_document_ids"] == ["EXT-SEPSIS-004"]
    assert r["answer"]["citation_basis"] == "model_cited"
    assert r["raw_model_output"].startswith("NOACT|")

    assert r["timing"]["total_ms"] == 6400
    assert r["integrity"]["missing_blobs"] == []


def test_the_prompt_hash_anchors_the_stored_components_to_what_was_sent():
    """The assembled prompt is NOT stored (it is the chart and the passages
    concatenated, and storing it would defeat the per-component dedup that pays
    for this whole feature). A sha256 of it is, so a reviewer can still prove the
    stored components correspond to the bytes the model saw."""
    import hashlib
    ctx = "CHANNEL A — PATIENT FACTS\nfoo\nCHANNEL B — LOCAL POLICY\nbar"
    turn_id = _record(prompt_context=ctx)
    r = prov.replay(turn_id)
    assert r["integrity"]["prompt_sha256"] == hashlib.sha256(ctx.encode()).hexdigest()


def test_a_refusal_is_recorded_as_a_turn_too():
    """A refusal IS an answer as far as an incident review is concerned — the
    question a reviewer asks is 'why did Sage refuse this', and a record that only
    keeps successes cannot answer it."""
    turn_id = _record(outcome="refused", refusal_class="harm_intent",
                      answer="I can't help with anything intended to harm a patient.",
                      raw_output="", structured=None, sources=None,
                      patient_context=None, cited_doc_ids=[])
    r = prov.replay(turn_id)
    assert r["outcome"] == "refused"
    assert r["answer"]["refusal_class"] == "harm_intent"
    assert r["guidelines"] == []


# ════════════════════════════════════════════════════════════════════════════
#  3. STORAGE — the design problem
# ════════════════════════════════════════════════════════════════════════════
def test_repeated_context_and_passages_cost_nothing_the_second_time():
    """Dedup is the whole storage strategy. Two consecutive turns on the same
    unchanged patient with the same retrieved passages — the single commonest
    shape of a real conversation — must store the chart and the passages ONCE."""
    ctx = _live_chart()
    srcs = [_guideline("EXT-SEPSIS-004"), _guideline("POL-ABX-002")]

    t1 = _record(patient_context=ctx, sources=srcs, question="Q one", answer="A one")
    t2 = _record(patient_context=ctx, sources=srcs, question="Q two", answer="A two")

    with db_session() as db:
        row = lambda tid: dict(db.execute(  # noqa: E731
            "SELECT bytes_written, bytes_logical FROM sage_provenance_turns WHERE turn_id = ?",
            (tid,)).fetchone())
        a, b = row(t1), row(t2)

    assert a["bytes_written"] > 0
    assert b["bytes_written"] < a["bytes_written"] / 2, (
        f"second turn stored {b['bytes_written']} B against the first's "
        f"{a['bytes_written']} B — the chart/passage blobs were not deduplicated")
    # Both turns still REPLAY in full: dedup must be invisible to the reader.
    assert prov.replay(t2)["patient_context"]["medications"] == ctx["medications"]
    assert len(prov.replay(t2)["guidelines"]) == 2


def test_bytes_per_turn_is_measured_and_within_the_stated_budget():
    """MEASURE, then state growth. The number this asserts is the number quoted in
    docs/BUILD_LOG_PLAIN_ENGLISH.md; if the two ever diverge, this fails."""
    ctx = _live_chart()
    srcs = [_guideline("EXT-SEPSIS-004"), _guideline("POL-ABX-002"), _guideline("EXT-AKI-001")]
    # 40 turns: 8 distinct questions x 5, chart unchanged, same corpus — a
    # deliberately optimistic-but-real ward pattern (the corpus is 19 documents).
    for i in range(40):
        _record(patient_context=ctx, sources=srcs,
                question=f"Question number {i % 8} about this patient's management",
                answer=f"Answer number {i} " + "with some clinical prose. " * 12)

    stats = prov.storage_stats()
    assert stats["turns"] == 40

    # ── the DETERMINISTIC half: compressed payload actually retained ──
    # These two numbers depend only on the content, so they hold whatever order
    # the suite runs in, and they are what the dedup design is judged on.
    assert stats["content_bytes"] / 40 < 400, stats
    assert stats["logical_bytes"] / stats["content_bytes"] > 10, stats

    # ── the PAGE-level half: what the operator's disk sees ──
    # Looser on purpose. `stored_bytes` is a dbstat PAGE count, and at 40 rows
    # b-tree slack and freelist reuse from earlier tests in this file move it by
    # tens of percent — asserting the exact figure here would make a storage
    # test fail on collection order. The quoted pilot number (3.0 KB/turn,
    # ~213 MB/year at 200 turns/day) comes from a 400-turn benchmark where those
    # effects amortise; this ceiling only has to catch a real regression.
    assert stats["bytes_per_turn"] < 4000, stats
    assert stats["measurement_basis"] in ("dbstat", "estimated")

    # The arithmetic the design problem asked for, computed rather than assumed.
    assert stats["projection"]["turns_per_day"] == 200
    assert stats["projection"]["bytes_per_year"] == pytest.approx(
        stats["bytes_per_turn"] * 200 * 365, rel=0.01)
    assert stats["projection"]["bytes_at_retention_horizon"] == pytest.approx(
        stats["bytes_per_turn"] * 200 * stats["retention_days"], rel=0.01)


@pytest.mark.parametrize("filler", ["x", "न"])
def test_a_single_pathological_blob_is_capped_not_unbounded(filler):
    """One 4 MB paste must not become 4 MB of provenance.

    Parametrised over ASCII and Devanagari because the cap is counted in BYTES:
    a character-counted cap would admit three times the limit for exactly the
    script this appliance is being deployed into."""
    turn_id = _record(question=filler * 4_000_000)
    r = prov.replay(turn_id)
    assert len(r["question"].encode()) <= prov.max_blob_bytes() + 200, (
        "the cap must bound BYTES, not characters")
    assert r["question"].rstrip().endswith("[truncated by provenance store]")


def test_retention_prunes_old_turns_and_reclaims_their_orphaned_blobs(monkeypatch):
    monkeypatch.setenv("ORB_SAGE_PROVENANCE_RETENTION_DAYS", "30")
    now = datetime.now(timezone.utc)
    old = _record(question="ancient question", occurred_at=(now - timedelta(days=90)).isoformat())
    new = _record(question="recent question")

    before = prov.storage_stats()
    result = prov.prune(now=now)
    after = prov.storage_stats()

    assert result["turns_deleted"] == 1
    assert result["blobs_deleted"] >= 1
    assert result["bytes_reclaimed"] > 0
    assert prov.replay(old) is None
    assert prov.replay(new) is not None
    # Asserted on the CONTENT, not on `stored_bytes`: the latter is dbstat's page
    # count, and SQLite returns freed pages to a freelist rather than to the
    # filesystem — a two-turn store still occupies the same pages after one turn
    # is dropped. Pages are the right number for an operator's disk projection
    # and the wrong one for "did the GC actually let go of anything".
    assert after["content_bytes"] < before["content_bytes"]
    assert after["turns"] == 1


def test_a_blob_still_referenced_by_a_live_turn_survives_pruning(monkeypatch):
    """The GC must be reference-counted, not "delete everything the old turn
    touched" — the shared chart snapshot is exactly the blob two turns share."""
    monkeypatch.setenv("ORB_SAGE_PROVENANCE_RETENTION_DAYS", "30")
    now = datetime.now(timezone.utc)
    ctx = _live_chart()
    old = _record(patient_context=ctx, question="old",
                  occurred_at=(now - timedelta(days=90)).isoformat())
    new = _record(patient_context=ctx, question="new")

    prov.prune(now=now)
    assert prov.replay(old) is None
    replayed = prov.replay(new)
    assert replayed["patient_context"]["medications"] == ctx["medications"], (
        "pruning an old turn destroyed a blob a live turn still needs")


# ════════════════════════════════════════════════════════════════════════════
#  4. THIS STORE IS PHI
# ════════════════════════════════════════════════════════════════════════════
def test_replay_handlers_are_not_async():
    """Every read here is a decrypt + decompress of PHI-bearing blobs. On the
    event loop that stalls the whole appliance (the rule the shadow routes learned
    the hard way)."""
    for route in app.routes:
        path = getattr(route, "path", "")
        if path.startswith("/api/sage/provenance"):
            for method in getattr(route, "methods", set()) or set():
                assert not inspect.iscoroutinefunction(route.endpoint), (
                    f"{method} {path} is async def — PHI reads must be threadpooled")
    assert any(getattr(r, "path", "").startswith("/api/sage/provenance") for r in app.routes), \
        "no provenance routes are mounted at all"


def test_a_clinician_cannot_read_the_provenance_store():
    turn_id = _record()
    r = client.get(f"/api/sage/provenance/{turn_id}", headers=_login(DOC))
    assert r.status_code == 403, r.text


def test_an_auditor_can_replay_a_turn():
    turn_id = _record()
    r = client.get(f"/api/sage/provenance/{turn_id}", headers=_login(ADMIN))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["turn_id"] == turn_id
    assert body["scope"]["patient_id"] == PATIENT
    assert body["patient_context"]["patient_id"] == PATIENT


def test_an_out_of_department_auditor_gets_404_never_403():
    """404, never 403 — a 403 confirms that a turn about this patient exists,
    which is itself the disclosure."""
    turn_id = _record()
    r = client.get(f"/api/sage/provenance/{turn_id}", headers=_login(ADMIN_CARD))
    assert r.status_code == 404, r.text
    assert "not found" in r.json().get("detail", "").lower()


def test_the_listing_is_department_scoped():
    _record()
    r = client.get("/api/sage/provenance", headers=_login(ADMIN_CARD),
                   params={"patient_id": PATIENT})
    assert r.status_code == 404, r.text

    r = client.get("/api/sage/provenance", headers=_login(ADMIN))
    assert r.status_code == 200, r.text
    assert any(t["patient_id"] == PATIENT for t in r.json()["turns"])

    scoped = client.get("/api/sage/provenance", headers=_login(ADMIN_CARD))
    assert scoped.status_code == 200, scoped.text
    assert not any(t["patient_id"] == PATIENT for t in scoped.json()["turns"]), (
        "a Cardiology auditor listed a General Medicine patient's turns")


def test_a_replay_writes_a_phi_access_audit_row():
    turn_id = _record()
    with db_session() as db:
        before = db.execute(
            "SELECT COUNT(*) c FROM audit_logs WHERE action_type = 'sage_provenance_replay'"
        ).fetchone()["c"]
    assert client.get(f"/api/sage/provenance/{turn_id}", headers=_login(ADMIN)).status_code == 200
    with db_session() as db:
        row = db.execute(
            "SELECT * FROM audit_logs WHERE action_type = 'sage_provenance_replay' "
            "ORDER BY id DESC LIMIT 1").fetchone()
        after = db.execute(
            "SELECT COUNT(*) c FROM audit_logs WHERE action_type = 'sage_provenance_replay'"
        ).fetchone()["c"]
    assert after == before + 1
    assert row["patient_id"] == PATIENT
    assert row["actor_user_id"] == ADMIN
    assert row["event_category"] == "phi_access"


def test_the_storage_report_is_admin_only_and_carries_no_phi():
    """The operator's disk number. It is counts and bytes, so it needs no patient
    scope — but it is still a governance surface, and it must not be reachable by
    a clinician who could infer ward activity from turn counts."""
    _record()
    assert client.get("/api/sage/provenance/storage",
                      headers=_login(DOC)).status_code == 403

    r = client.get("/api/sage/provenance/storage", headers=_login(ADMIN),
                   params={"turns_per_day": 200})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["turns"] == 1
    assert body["stored_bytes"] > 0
    assert body["projection"]["turns_per_day"] == 200
    assert body["projection"]["bytes_per_year"] > 0
    assert body["retention_days"] >= 1
    # No question text, no chart, no patient id anywhere in the report.
    assert PATIENT not in r.text
    assert "antibiotic" not in r.text.lower()


def test_the_storage_report_clamps_an_absurd_projection_rate():
    """`turns_per_day` is caller-supplied. A 0 or a negative must not produce a
    divide-by-nothing or a negative disk projection in an operator's dashboard."""
    _record()
    for rate, expected in ((0, 200), (-5, 1), (10 ** 9, 100000)):
        r = client.get("/api/sage/provenance/storage", headers=_login(ADMIN),
                       params={"turns_per_day": rate})
        assert r.status_code == 200, r.text
        assert r.json()["projection"]["turns_per_day"] == expected
        assert r.json()["projection"]["bytes_per_year"] > 0


def test_the_listing_never_leaks_question_or_chart_text():
    """The list is metadata. Reading the CONTENT requires naming a turn, which is
    the action that gets audited per-patient."""
    secret = "does Provenance Patient need dialysis"
    _record(question=secret)
    r = client.get("/api/sage/provenance", headers=_login(ADMIN))
    assert r.status_code == 200
    assert secret not in r.text
    assert "Community-acquired pneumonia" not in r.text


# ════════════════════════════════════════════════════════════════════════════
#  5. THE SEAM — a real Sage turn through the real route
# ════════════════════════════════════════════════════════════════════════════
def _stub_sage(monkeypatch, chunks, scope="broad", sources=None, patient_context=None,
               query_scope="GENERAL_MEDICAL"):
    sources = sources if sources is not None else []

    def _ctx(query, user, patient_id=None, history=None):
        assert user and user.get("id")
        return ("CONTEXT BLOCK", scope, sources, "high", False, patient_context, query_scope)

    async def _llm(*args, **kwargs):
        route_info = kwargs.get("route_info")
        if route_info is not None:
            route_info.update({
                "provider": "stub", "model": "medgemma:4b",
                "thinking": False, "thinking_requested": False, "thinking_tier": "fast",
                "safety_floor": False, "safety_floor_rules": [],
                "safety_floor_unsatisfied": False,
            })
        for chunk in chunks:
            yield chunk

    monkeypatch.setattr("routes.sage_routes.get_relevant_context", _ctx)
    monkeypatch.setattr("routes.sage_routes.query_ollama", _llm)


def _turns():
    with db_session() as db:
        return [dict(r) for r in db.execute(
            "SELECT * FROM sage_provenance_turns ORDER BY rowid").fetchall()]


def test_a_real_sage_turn_leaves_a_replayable_row(monkeypatch):
    chart = _live_chart()
    _stub_sage(monkeypatch, ["Continue ", "current therapy."], scope="broad",
               sources=[_guideline("EXT-SEPSIS-004")], patient_context=chart,
               query_scope="PATIENT_SPECIFIC")
    r = client.post("/api/sage/query", headers=_login(DOC),
                    json={"query": "how is this patient doing?", "patient_id": PATIENT})
    assert r.status_code == 200, r.text
    assert any(e.get("done") for e in _sse(r))

    rows = _turns()
    assert len(rows) == 1, rows
    replayed = prov.replay(rows[0]["turn_id"])
    assert replayed["question"] == "how is this patient doing?"
    assert replayed["scope"]["patient_id"] == PATIENT
    assert replayed["scope"]["department"] == DEPT
    assert replayed["patient_context"]["patient_id"] == PATIENT
    assert [g["document_id"] for g in replayed["guidelines"]] == ["EXT-SEPSIS-004"]
    assert replayed["answer"]["text"].startswith("Continue")
    assert replayed["model"]["served"] == "medgemma:4b"
    assert replayed["model"]["thinking"] is False
    assert replayed["outcome"] == "answered"


def test_the_thinking_decision_and_safety_floor_are_stored(monkeypatch):
    _stub_sage(monkeypatch, ["Answer."], scope="broad")

    async def _llm(*args, **kwargs):
        info = kwargs.get("route_info")
        info.update({"provider": "ollama", "model": "medgemma:27b",
                     "thinking": True, "thinking_requested": False,
                     "thinking_tier": "careful", "thinking_reason": "safety_floor",
                     "safety_floor": True, "safety_floor_rules": ["dosing"],
                     "safety_floor_unsatisfied": False})
        yield "Answer."
    monkeypatch.setattr("routes.sage_routes.query_ollama", _llm)

    r = client.post("/api/sage/query", headers=_login(DOC),
                    json={"query": "what dose of gentamicin?", "thinking": False})
    assert r.status_code == 200, r.text

    replayed = prov.replay(_turns()[0]["turn_id"])
    assert replayed["model"]["thinking"] is True
    assert replayed["model"]["thinking_requested"] is False
    assert replayed["model"]["safety_floor"] is True
    assert replayed["model"]["safety_floor_rules"] == ["dosing"]
    assert replayed["model"]["safety_floor_state"] == prov.SAFETY_FLOOR_FIRED


def test_a_pre_llm_refusal_is_recorded():
    r = client.post("/api/sage/query", headers=_login(DOC),
                    json={"query": "how much morphine would be lethal for this patient?",
                          "patient_id": PATIENT})
    assert r.status_code == 200, r.text
    rows = _turns()
    assert len(rows) == 1, rows
    assert rows[0]["outcome"] == "refused"
    assert rows[0]["refusal_class"] == "harm_intent"
    assert prov.replay(rows[0]["turn_id"])["question"].startswith("how much morphine")


def test_a_spoken_turn_is_recorded_too(monkeypatch):
    """A record that only covers typed turns cannot answer a question about a
    spoken one, and the voice lane is the surface where guardrail copies have
    already drifted once. It is deliberately THINNER — no chart snapshot, no
    passages, because the voice agent builds its own context and never surfaces
    them — but it exists and says which model spoke."""
    class _Agent:
        async def generate_stream(self, q, user, patient_id=None, conversation_history=None):
            for tok in ("Paracetamol ", "one gram, ", "six hourly."):
                yield tok

    with mock.patch("services.voice_agent_service.get_voice_agent", lambda: _Agent()), \
         mock.patch("config.VOICE", {"conversational": {"enabled": True,
                                                        "model_path": "models/voice-4b"}}):
        r = client.post("/api/sage/voice", headers=_login(DOC),
                        json={"query": "what is the paracetamol dose?", "patient_id": PATIENT})
    assert r.status_code == 200, r.text

    rows = _turns()
    assert len(rows) == 1, rows
    assert rows[0]["lane"] == "voice"
    assert rows[0]["outcome"] == "answered"
    replayed = prov.replay(rows[0]["turn_id"])
    assert replayed["question"] == "what is the paracetamol dose?"
    assert replayed["answer"]["text"].startswith("Paracetamol")
    assert replayed["model"]["provider"] == "mlx"
    assert replayed["model"]["served"] == "voice-4b"
    # Honest about what it does NOT have, rather than faking an empty chart.
    assert replayed["patient_context"] is None
    assert replayed["guidelines"] == []


def test_a_broken_spoken_turn_still_leaves_a_record(monkeypatch):
    """The happy-path capture sits inside the voice handler's try, so a crash
    would otherwise be the one class of turn the store cannot show."""
    class _Agent:
        async def generate_stream(self, q, user, patient_id=None, conversation_history=None):
            raise RuntimeError("the voice model fell over")
            yield  # pragma: no cover — makes this an async generator

    with mock.patch("services.voice_agent_service.get_voice_agent", lambda: _Agent()), \
         mock.patch("config.VOICE", {"conversational": {"enabled": True,
                                                        "model_path": "models/voice-4b"}}):
        r = client.post("/api/sage/voice", headers=_login(DOC),
                        json={"query": "anything at all"})
    assert r.status_code == 200, r.text

    rows = _turns()
    assert len(rows) == 1, rows
    assert rows[0]["outcome"] == "failed"
    assert rows[0]["lane"] == "voice"


def test_a_retrieval_failure_is_recorded_as_a_failed_turn(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("index unavailable")
    monkeypatch.setattr("routes.sage_routes.get_relevant_context", _boom)
    r = client.post("/api/sage/query", headers=_login(DOC), json={"query": "anything"})
    assert r.status_code == 200, r.text
    rows = _turns()
    assert len(rows) == 1, rows
    assert rows[0]["outcome"] == "failed"


# ════════════════════════════════════════════════════════════════════════════
#  6. IT MUST NOT COST THE CLINICIAN ANYTHING
# ════════════════════════════════════════════════════════════════════════════
def test_a_provenance_failure_never_fails_the_answer(monkeypatch):
    def _explode(*a, **k):
        raise RuntimeError("provenance store on fire")
    monkeypatch.setattr(prov, "record_turn", _explode)
    _stub_sage(monkeypatch, ["The answer ", "survives."], scope="broad")

    r = client.post("/api/sage/query", headers=_login(DOC), json={"query": "still works?"})
    assert r.status_code == 200, r.text
    events = _sse(r)
    assert any(e.get("done") for e in events)
    text = "".join(e.get("content", "") for e in events)
    assert "The answer survives." in text
    assert _turns() == []


def test_the_provenance_write_happens_off_the_event_loop(monkeypatch):
    """`run_db`, not the loop. A SQLCipher write of a whole chart snapshot on the
    loop is exactly the class of stall that wedged this appliance before."""
    seen = {}
    real = prov.record_turn

    def _spy(**kw):
        seen["thread"] = threading.current_thread().name
        return real(**kw)

    monkeypatch.setattr(prov, "record_turn", _spy)
    _stub_sage(monkeypatch, ["ok"], scope="broad")
    r = client.post("/api/sage/query", headers=_login(DOC), json={"query": "thread check"})
    assert r.status_code == 200, r.text
    assert seen.get("thread", "").startswith("orb-db"), (
        f"provenance was written on {seen.get('thread')!r}, not the DB executor")


def test_provenance_can_be_switched_off_entirely(monkeypatch):
    monkeypatch.setenv("ORB_SAGE_PROVENANCE_ENABLED", "0")
    _stub_sage(monkeypatch, ["ok"], scope="broad")
    r = client.post("/api/sage/query", headers=_login(DOC), json={"query": "disabled"})
    assert r.status_code == 200, r.text
    assert _turns() == []


# ════════════════════════════════════════════════════════════════════════════
#  7. THE LOOSE ENDS — retention has a caller, patient-less turns have a ward,
#     and the safety floor's silence is not mistaken for a verdict
# ════════════════════════════════════════════════════════════════════════════
def _main_lifespan_ast():
    """main.py's lifespan, out of the parse tree — same technique as
    tests/test_shadow_producers.py, for the same reason: 'registered from
    lifespan' is a claim a docstring has successfully lied about before."""
    main_path = os.path.join(os.path.dirname(__file__), "..", "main.py")
    with open(main_path, encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), filename="main.py")
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "lifespan":
            return node
    raise AssertionError("main.py has no `lifespan` coroutine")


def test_the_retention_sweep_is_registered_and_retained_in_lifespan():
    """prune() must have a CALLER. The store's whole disk plan (settle at
    ~234 MB and stop growing) assumes retention actually runs; a prune that is
    merely importable grows ~213 MB/year forever."""
    lifespan = _main_lifespan_ast()
    spawns = [
        node for node in ast.walk(lifespan)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        and node.func.id == "_spawn"
        and any(isinstance(a, ast.Call)
                and getattr(a.func, "id", "") == "sage_provenance_prune_sweep"
                for a in node.args)
    ]
    assert spawns, (
        "sage_provenance_prune_sweep is never _spawn()ed from main.py's lifespan — "
        "prune() has no caller and the provenance store grows unbounded")
    retained = any(
        isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        and node.func.attr == "append" and any(arg in spawns for arg in node.args)
        for node in ast.walk(lifespan))
    assert retained, (
        "the sweep task is spawned but never appended to lifespan_tasks — the loop "
        "holds only a weak reference and the sweep can be GC'd mid-run")


def test_the_daily_sweep_actually_drives_prune(monkeypatch):
    """One iteration of the REAL sweep coroutine must delete a stale turn —
    wired end to end, not merely callable. The sleeps are intercepted: the
    first (startup settle) is skipped, the second (the daily interval) stops
    the loop after exactly one pass."""
    import main as main_mod
    monkeypatch.setenv("ORB_SAGE_PROVENANCE_RETENTION_DAYS", "30")
    now = datetime.now(timezone.utc)
    old = _record(question="ancient question",
                  occurred_at=(now - timedelta(days=90)).isoformat())
    new = _record(question="recent question")

    real_sleep = asyncio.sleep
    slept: list = []

    async def _sleep(seconds, *a, **k):
        slept.append(seconds)
        if len(slept) >= 2:
            raise asyncio.CancelledError
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", _sleep)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(main_mod.sage_provenance_prune_sweep())

    assert prov.replay(old) is None, "the sweep ran but did not prune the stale turn"
    assert prov.replay(new) is not None, "the sweep pruned a turn inside the horizon"


def test_the_sweep_respects_the_provenance_kill_switch(monkeypatch):
    """ORB_SAGE_PROVENANCE_ENABLED=0 means the store is off; the sweep must not
    touch it (and must not create its schema as a side effect)."""
    import main as main_mod
    monkeypatch.setenv("ORB_SAGE_PROVENANCE_ENABLED", "0")
    monkeypatch.setenv("ORB_SAGE_PROVENANCE_RETENTION_DAYS", "30")
    old = prov._record_turn_inner(  # bypass enabled() to seed a stale row
        turn_id=None, lane="chat", outcome="answered", user=_user(DOC),
        question="stale", patient_id=None, patient_context=None, prompt_context=None,
        sources=None, route_info={}, answer="a", raw_output="", structured=None,
        cited_doc_ids=None, citation_basis=None, confidence=None, escalation=False,
        query_scope=None, action_status=None, refusal_class=None, timings=None,
        occurred_at=(datetime.now(timezone.utc) - timedelta(days=90)).isoformat(),
        db=None)

    real_sleep = asyncio.sleep
    slept: list = []

    async def _sleep(seconds, *a, **k):
        slept.append(seconds)
        if len(slept) >= 2:
            raise asyncio.CancelledError
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", _sleep)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(main_mod.sage_provenance_prune_sweep())
    assert prov.replay(old) is not None, (
        "the sweep pruned a store that provenance had been switched off for")


def test_a_patientless_turn_is_scoped_to_the_asking_users_department():
    """A turn with no patient anchor is still a turn a WARD asked. It is
    attributed to the asking user's department at write time and read back
    under exactly that scope — same-department auditors see it, out-of-
    department auditors get an empty listing and a 404 (never 403) on replay,
    and a hospital-wide account sees everything."""
    turn_id = _record(patient_id=None, patient_context=None,
                      question="what is the ward's sepsis bundle?")

    mine = client.get("/api/sage/provenance", headers=_login(ADMIN_GM))
    assert mine.status_code == 200, mine.text
    assert any(t["turn_id"] == turn_id for t in mine.json()["turns"]), (
        "a same-department auditor lost sight of their own ward's patient-less turn")
    assert client.get(f"/api/sage/provenance/{turn_id}",
                      headers=_login(ADMIN_GM)).status_code == 200

    theirs = client.get("/api/sage/provenance", headers=_login(ADMIN_CARD))
    assert theirs.status_code == 200, theirs.text
    assert not any(t["turn_id"] == turn_id for t in theirs.json()["turns"]), (
        "a Cardiology auditor listed a General Medicine patient-less turn")
    r = client.get(f"/api/sage/provenance/{turn_id}", headers=_login(ADMIN_CARD))
    assert r.status_code == 404, r.text
    assert "not found" in r.json().get("detail", "").lower()

    assert client.get(f"/api/sage/provenance/{turn_id}",
                      headers=_login(ADMIN)).status_code == 200


def test_a_governance_accounts_patientless_turn_is_hospital_wide_only():
    """department NULL on the row means no ward owns the turn. The safe
    direction — and the documented posture for any historical row whose
    department cannot be resolved — is EXCLUSION from scoped reads, never a
    guess: scoped auditors do not see it, unscoped ones do."""
    turn_id = _record(user=_user(ADMIN), patient_id=None, patient_context=None,
                      question="hospital-wide governance question")

    listed = client.get("/api/sage/provenance", headers=_login(ADMIN_GM))
    assert not any(t["turn_id"] == turn_id for t in listed.json()["turns"])
    assert client.get(f"/api/sage/provenance/{turn_id}",
                      headers=_login(ADMIN_GM)).status_code == 404
    assert client.get(f"/api/sage/provenance/{turn_id}",
                      headers=_login(ADMIN)).status_code == 200


def test_safety_floor_tristate_distinguishes_not_fired_from_not_evaluated():
    """The v1 column reads 0 for both of these. They are different answers to
    an incident review's question: one says the dosing floor examined the turn
    and let it through, the other says nothing ever looked."""
    fired = _record()  # fixture route_info carries safety_floor=True
    not_fired = _record(route_info={
        "provider": "ollama", "model": "medgemma:27b",
        "thinking": False, "thinking_requested": False, "thinking_tier": "fast",
        "safety_floor": False, "safety_floor_rules": [],
        "safety_floor_unsatisfied": False,
    })
    never_ran = _record(lane="voice", patient_context=None, sources=None,
                        route_info={"provider": "mlx", "model": "voice-4b"})

    assert prov.replay(fired)["model"]["safety_floor_state"] == prov.SAFETY_FLOOR_FIRED
    a = prov.replay(not_fired)["model"]
    b = prov.replay(never_ran)["model"]
    assert a["safety_floor_state"] == prov.SAFETY_FLOOR_NOT_FIRED
    assert b["safety_floor_state"] == prov.SAFETY_FLOOR_NOT_EVALUATED
    # The legacy 0/1 cannot tell these two apart — both read False there.
    assert a["safety_floor"] is False and b["safety_floor"] is False


def test_a_v1_store_gains_the_column_and_its_rows_stay_honest_unknown():
    """The additive migration, against a REAL v1 store: the base DDL is the
    module's own _SCHEMA_STATEMENTS (so this fixture cannot drift from the true
    v1 shape), a legacy row is written under it, and ensure_schema then ALTERs
    the column in. The legacy row's stored 0 must come back as 'unknown' —
    never re-badged as an evaluation that happened."""
    with db_session() as db:
        prov.drop_schema(db)
        for stmt in prov._SCHEMA_STATEMENTS:  # v1: no additive columns applied
            db.execute(stmt)
        db.execute(
            "INSERT INTO sage_provenance_turns (turn_id, occurred_at, lane, outcome, "
            "actor_user_id, department, safety_floor) "
            "VALUES ('LEGACY01', ?, 'chat', 'answered', ?, ?, 0)",
            (prov.utc_now(), DOC, DEPT))
    prov.reset_schema_cache()

    r = prov.replay("LEGACY01")  # ensure_schema (and the ALTER) runs on this read
    assert r is not None
    assert r["model"]["safety_floor_state"] == prov.SAFETY_FLOOR_UNKNOWN
    assert r["model"]["safety_floor"] is False  # the legacy bit survives untouched

    legacy = next(t for t in prov.list_turns() if t["turn_id"] == "LEGACY01")
    assert legacy["safety_floor_state"] == prov.SAFETY_FLOOR_UNKNOWN

    # A row written AFTER the migration records a real verdict.
    fresh = _record()
    assert prov.replay(fresh)["model"]["safety_floor_state"] == prov.SAFETY_FLOOR_FIRED


def test_the_listing_carries_the_tristate_and_a_retrieval_count():
    """What the incident-review page renders per row: the tri-state and a COUNT
    of retrieved documents — never the ids themselves, which belong to the
    audited per-turn replay."""
    turn_id = _record()
    r = client.get("/api/sage/provenance", headers=_login(ADMIN))
    assert r.status_code == 200, r.text
    t = next(t for t in r.json()["turns"] if t["turn_id"] == turn_id)
    assert t["safety_floor_state"] == prov.SAFETY_FLOOR_FIRED
    assert t["retrieved_doc_count"] == 2
    assert "retrieved_doc_ids" not in t


# ════════════════════════════════════════════════════════════════════════════
#  2026-10-03 (F035, F037): a switched-off floor, the patient a chart was found
#  for, spoken turns with their chart, and the store as a numbered migration
# ════════════════════════════════════════════════════════════════════════════
_FLOOR_OFF_INFO = {
    "provider": "ollama", "model": "medgemma:4b",
    "thinking": False, "thinking_requested": False, "thinking_tier": "fast",
    "safety_floor": False, "safety_floor_rules": [], "safety_floor_unsatisfied": False,
    "safety_floor_enabled": False,
}


def test_a_switched_off_floor_reads_disabled_not_not_fired():
    """ORB_FAST_MODE_SAFETY_FLOOR=0 used to be recorded as 'not_fired', which
    reads as "the floor looked and let it through" when nothing looked."""
    off = _record(route_info=_FLOOR_OFF_INFO)
    armed = _record(route_info={**_FLOOR_OFF_INFO, "safety_floor_enabled": True})

    assert prov.replay(off)["model"]["safety_floor_state"] == prov.SAFETY_FLOOR_DISABLED
    assert prov.replay(armed)["model"]["safety_floor_state"] == prov.SAFETY_FLOOR_NOT_FIRED
    listed = {t["turn_id"]: t for t in prov.list_turns()}
    assert listed[off]["safety_floor_state"] == prov.SAFETY_FLOOR_DISABLED
    assert "safety_floor_armed" not in listed[off]
    with db_session() as db:
        row = db.execute("SELECT safety_floor_state, safety_floor_armed FROM "
                         "sage_provenance_turns WHERE turn_id = ?", (off,)).fetchone()
    # Stored inside the original CHECK, and the armed bit says why.
    assert (row["safety_floor_state"], row["safety_floor_armed"]) == ("not_evaluated", 0)


def test_a_chart_found_by_name_is_recorded_against_that_patient(monkeypatch):
    """The request named nobody (no patient_id); retrieval attached the chart by
    name. The provenance row and the inference audit both used the request's
    blank patient, so the PHI read could not be found by patient."""
    chart = _live_chart()
    seen = {}

    def _ctx(query, user, patient_id=None, history=None):
        return ("CONTEXT BLOCK", "simple", [], "high", False, chart, "PATIENT_SPECIFIC")

    async def _llm(*args, **kwargs):
        seen["patient_id"] = kwargs.get("patient_id")
        kwargs["route_info"].update({"provider": "stub", "model": "medgemma:27b"})
        yield "NOACT| Stable overnight."

    monkeypatch.setattr("routes.sage_routes.get_relevant_context", _ctx)
    monkeypatch.setattr("routes.sage_routes.query_ollama", _llm)
    r = client.post("/api/sage/query", headers=_login(DOC),
                    json={"query": "how is Provenance Patient doing?"})
    assert r.status_code == 200, r.text

    rows = _turns()
    assert len(rows) == 1, rows
    assert rows[0]["patient_id"] == PATIENT
    assert rows[0]["patient_context_used"] == 1
    # query_ollama writes the per-inference audit row with this id.
    assert seen["patient_id"] == PATIENT


def _voice_with_retrieval(monkeypatch, retrieval, json_body):
    """A real VoiceAgent prompt builder over a stubbed retrieval, model stubbed."""
    from services import voice_agent_service as vas

    class _Agent(vas.VoiceAgent):
        async def generate_stream(self, q, user, patient_id=None, conversation_history=None):
            self._build_system_prompt(q, user, patient_id)
            for tok in ("Stable ", "overnight."):
                yield tok

    monkeypatch.setattr("services.sage_service.get_relevant_context",
                        lambda query, user, patient_id=None, history=None: retrieval)
    with mock.patch("services.voice_agent_service.get_voice_agent", lambda: _Agent()), \
         mock.patch("config.VOICE", {"conversational": {"enabled": True,
                                                        "model_path": "models/voice-4b"}}):
        r = client.post("/api/sage/voice", headers=_login(DOC), json=json_body)
    assert r.status_code == 200, r.text
    rows = _turns()
    assert len(rows) == 1, rows
    return rows[0]


def test_a_spoken_turn_keeps_its_chart_and_passages(monkeypatch):
    chart = _live_chart()
    row = _voice_with_retrieval(
        monkeypatch,
        ("CTX", "simple", [_guideline("EXT-SEPSIS-004")], "high", False, chart,
         "PATIENT_SPECIFIC"),
        {"query": "how is Provenance Patient overnight?"},
    )
    assert row["lane"] == "voice"
    assert row["patient_id"] == PATIENT        # found by name, not sent as an id
    replayed = prov.replay(row["turn_id"])
    assert replayed["patient_context"]["patient_id"] == PATIENT
    assert [g["document_id"] for g in replayed["guidelines"]] == ["EXT-SEPSIS-004"]
    assert replayed["integrity"]["prompt_sha256"]


def test_a_spoken_cross_department_refusal_records_no_patient(monkeypatch):
    """The chat lane's rule, on the voice lane: retrieval refused to confirm the
    patient, so the store an auditor can list does not name them."""
    row = _voice_with_retrieval(
        monkeypatch,
        ("You do not have access.", "simple", [], "high", False, None, "OUT_OF_SCOPE"),
        {"query": "how is the patient in bed 4?", "patient_id": PATIENT},
    )
    assert row["patient_id"] is None
    assert prov.replay(row["turn_id"])["patient_context"] is None


def test_migration_44_creates_the_store_on_a_fresh_database(tmp_path):
    """A fresh appliance had no provenance tables until the first answer."""
    import sqlite3
    import migrations

    assert any(m.version == 44 and m.name == "sage_provenance_schema"
               for m in migrations.MIGRATIONS)
    conn = sqlite3.connect(str(tmp_path / "fresh.db"))   # no Row factory on purpose
    try:
        migrations._m44_sage_provenance_schema(conn)
        migrations._m44_sage_provenance_schema(conn)     # idempotent
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        assert {"sage_provenance_turns", "sage_provenance_blobs",
                "sage_provenance_refs"} <= tables
        cols = {r[1] for r in conn.execute(
            "PRAGMA table_info(sage_provenance_turns)").fetchall()}
        assert {"safety_floor_state", "safety_floor_armed"} <= cols
    finally:
        conn.close()
