"""EHR Downtime Mode — durable local clinical snapshots.

Verifies capture assembles the essential clinical picture from pure DB reads
(demographics, allergies, active meds, latest vitals + NEWS2, signed notes),
that snapshot_json round-trips, that get_latest_snapshot reads it back, and that
capture_all_admitted counts admitted patients.

Also covers the two artifacts layered on top of the snapshots:
  * the WARD-LEVEL PRINT PACK — one self-contained printable HTML file per ward
    on durable local disk, rebuilt by the hourly sweep, retrievable with the
    app down (docs/DRILLS.md §2.1 used to name its absence an honest gap);
  * the SCOPE-REFUSAL SEMANTICS of /api/downtime and /api/ipass — byte-identical
    to the posture the chart/timeline/orders routes established (404 "Patient
    not found", never distinguishable from a missing record; DEMO_READINESS
    §15.1).
"""
import asyncio
import json
import os
import time
import uuid

import pytest
from fastapi.testclient import TestClient

from auth import create_token
from database import db_session
import services.downtime_service as ds
from main import app
from services.downtime_service import (
    build_ward_print_packs,
    capture_snapshot,
    capture_all_admitted,
    get_latest_snapshot,
    list_snapshots,
    prune_ward_packs,
)

client = TestClient(app)

# Distinctive department names so this module cannot collide with any other
# test module's fixtures in the shared session DB.
DEPT_A = "Downtime Scope Medicine"
DEPT_B = "Downtime Scope Surgery"


_SEEDED: list[str] = []


@pytest.fixture(autouse=True)
def _discharge_seeded_patients():
    """TEST-ISOLATION LAW: `_mk_patient` defaults to status='admitted', so every
    patient this module seeded used to stay admitted in the shared session DB —
    4 by the end of the module. That is not a cosmetic leak. The FHIR
    self-export in services/downtime_service.py — the same service this module
    tests — iterates `patients WHERE status='admitted'` and sleeps
    _PATIENT_SLEEP_S (250 ms) BETWEEN PATIENTS, so each leftover adds ~0.275 s
    to every export call in tests/test_downtime_fhir_export.py, which collects
    immediately after this module.

    The seeding happens in a plain helper rather than a fixture, so the helper
    registers each id here and this autouse teardown discharges the batch. Do
    not DELETE — the allergy, problem, medication, vitals and downtime_snapshots
    rows written during the test legitimately reference these ids."""
    yield
    if not _SEEDED:
        return
    with db_session() as db:
        db.executemany(
            "UPDATE patients SET status = 'discharged' WHERE id = ?",
            [(p,) for p in _SEEDED],
        )
    _SEEDED.clear()


def _mk_patient(status="admitted", ward="Ward A"):
    pid = f"PATDT{uuid.uuid4().hex[:6].upper()}"
    mrn = f"MRN-{uuid.uuid4().hex[:6]}"
    with db_session() as db:
        db.execute(
            """INSERT INTO patients (id, name, mrn, bed, ward, diagnosis, status,
                                     age, sex, on_scale_2)
               VALUES (?, ?, ?, 'B3', ?, 'Sepsis', ?, 67, 'M', 0)""",
            (pid, "Downtime Test", mrn, ward, status),
        )
        # Active + inactive allergy — only the active one should be captured.
        db.execute(
            """INSERT INTO patient_allergies (patient_id, substance, reaction, severity, status)
               VALUES (?, 'penicillin', 'anaphylaxis', 'severe', 'active')""",
            (pid,),
        )
        db.execute(
            """INSERT INTO patient_allergies (patient_id, substance, reaction, severity, status)
               VALUES (?, 'latex', 'rash', 'mild', 'inactive')""",
            (pid,),
        )
        # Active problem.
        db.execute(
            """INSERT INTO problem_list (patient_id, problem, icd10_code, status)
               VALUES (?, 'Community-acquired pneumonia', 'J18.9', 'active')""",
            (pid,),
        )
        # Active + discontinued meds — only the active one should be captured.
        db.execute(
            """INSERT INTO medications (patient_id, drug_name, dosage, route, frequency, status, source)
               VALUES (?, 'Amoxicillin', '500mg', 'Oral', 'TDS', 'active', 'clinician')""",
            (pid,),
        )
        db.execute(
            """INSERT INTO medications (patient_id, drug_name, dosage, route, frequency, status, source)
               VALUES (?, 'Paracetamol', '1g', 'Oral', 'QDS', 'discontinued', 'clinician')""",
            (pid,),
        )
        # Latest vitals (deranged enough to score on NEWS2).
        db.execute(
            """INSERT INTO vitals (patient_id, timestamp, heart_rate, systolic_bp,
                                   diastolic_bp, spo2, temperature, respiratory_rate,
                                   consciousness, on_oxygen)
               VALUES (?, '2026-01-01T09:00:00+00:00', 118, 92, 60, 91, 38.7, 24, 'alert', 1)""",
            (pid,),
        )
        # A signed scribe note (should be included) + a draft (should not).
        db.execute(
            """INSERT INTO scribe_notes (patient_id, author_id, raw_transcript,
                                         structured_note, note_format, status, signed_by, signed_at)
               VALUES (?, 'D001', 'raw', 'S: signed handoff note', 'handoff', 'signed', 'D001',
                       '2026-01-01T10:00:00+00:00')""",
            (pid,),
        )
        db.execute(
            """INSERT INTO scribe_notes (patient_id, author_id, raw_transcript,
                                         structured_note, note_format, status)
               VALUES (?, 'D001', 'raw', 'S: draft only', 'soap', 'draft')""",
            (pid,),
        )
        # A risk score with JSON drivers.
        db.execute(
            """INSERT INTO risk_scores (patient_id, score, level, drivers)
               VALUES (?, 0.4, 'elevated', ?)""",
            (pid, json.dumps(["Tachycardia (HR 118)", "Hypoxia (SpO2 91%)"])),
        )
    _SEEDED.append(pid)
    return pid


def test_capture_snapshot_persists_row_and_round_trips():
    pid = _mk_patient()
    result = capture_snapshot(pid, created_by="D001")

    # A patient_snapshots row exists.
    with db_session() as db:
        rows = db.execute(
            "SELECT id, patient_id, snapshot_json, summary, created_by FROM patient_snapshots WHERE patient_id = ?",
            (pid,),
        ).fetchall()
    assert len(rows) == 1
    row = rows[0]
    assert row["patient_id"] == pid
    assert row["created_by"] == "D001"
    assert row["summary"]

    # snapshot_json round-trips and matches what capture returned.
    stored = json.loads(row["snapshot_json"])
    assert stored == result["snapshot"]

    snap = result["snapshot"]
    assert snap["demographics"]["name"] == "Downtime Test"
    assert snap["patient_id"] == pid

    # Allergies: only the active one, severe first.
    assert len(snap["allergies"]) == 1
    assert snap["allergies"][0]["substance"] == "penicillin"
    assert snap["allergies"][0]["severity"] == "severe"

    # Active meds only.
    med_names = [m["drug_name"] for m in snap["medications"]]
    assert med_names == ["Amoxicillin"]
    assert snap["medications"][0]["dosage"] == "500mg"
    assert snap["medications"][0]["route"] == "Oral"
    assert snap["medications"][0]["frequency"] == "TDS"
    assert snap["medications"][0]["source"] == "clinician"

    # Active problems.
    assert len(snap["problems"]) == 1
    assert snap["problems"][0]["icd10_code"] == "J18.9"

    # Latest vitals populated.
    assert snap["latest_vitals"]["heart_rate"] == 118
    assert snap["latest_vitals"]["spo2"] == 91

    # NEWS2 computed from DB only (no LLM). Deranged vitals -> non-zero score.
    assert snap["news2"] is not None
    assert snap["news2"]["scoring_model"] == "NEWS2"
    assert snap["news2"]["raw_news2"] > 0

    # Only the signed note is captured.
    assert len(snap["recent_notes"]) == 1
    assert snap["recent_notes"][0]["structured_note"] == "S: signed handoff note"

    # Risk score drivers decoded from JSON string to a list.
    assert len(snap["recent_risk_scores"]) == 1
    assert isinstance(snap["recent_risk_scores"][0]["drivers"], list)


def test_get_latest_snapshot_returns_most_recent():
    pid = _mk_patient()
    first = capture_snapshot(pid, created_by="auto")
    second = capture_snapshot(pid, created_by="D001")

    latest = get_latest_snapshot(pid)
    assert latest is not None
    assert latest["id"] == second["id"]
    assert latest["id"] != first["id"]
    assert latest["created_by"] == "D001"
    assert latest["snapshot"]["patient_id"] == pid

    history = list_snapshots(pid)
    assert len(history) == 2
    # Newest first.
    assert history[0]["id"] == second["id"]


def test_get_latest_snapshot_none_when_never_captured():
    pid = _mk_patient()
    assert get_latest_snapshot(pid) is None
    assert list_snapshots(pid) == []


def test_capture_snapshot_missing_patient_raises():
    with pytest.raises(ValueError):
        capture_snapshot("NON_EXISTENT_PATIENT")


def test_capture_all_admitted_counts_admitted_patient():
    admitted = _mk_patient(status="admitted")
    discharged = _mk_patient(status="discharged")

    result = capture_all_admitted(created_by="auto")

    # At least our admitted patient is counted; the discharged one is not snapshotted.
    assert result["count"] >= 1
    assert get_latest_snapshot(admitted) is not None
    assert get_latest_snapshot(discharged) is None


# ═════════════════════════════════════════════════════════════════════════════
# Ward-level print pack — one durable printable HTML artifact per ward
# ═════════════════════════════════════════════════════════════════════════════

def _mrn(pid: str) -> str:
    with db_session() as db:
        return db.execute("SELECT mrn FROM patients WHERE id = ?", (pid,)).fetchone()["mrn"]


def _mk_user(role, department):
    uid = f"DTU{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO users (id, name, role, password_hash, department) "
            "VALUES (?, ?, ?, 'x', ?)",
            (uid, f"Downtime {role}", role, department),
        )
    return uid


def _auth(user_id, role):
    return {"Authorization": f"Bearer {create_token(user_id, role)}"}


def _admit(pid, department):
    """Give the patient the admissions row per-patient scoping derives from
    (permissions.PATIENT_DEPARTMENT_SQL — the ADMISSION, never patients.ward)."""
    with db_session() as db:
        db.execute(
            "INSERT OR IGNORE INTO users (id, name, role, password_hash) "
            "VALUES ('DTADM', 'Downtime Admitter', 'doctor', 'x')"
        )
        db.execute(
            "INSERT INTO admissions (patient_id, department, ward, bed, status, admitted_by) "
            "VALUES (?, ?, ?, 'B3', 'active', 'DTADM')",
            (pid, department, department),
        )


def _ward() -> str:
    return f"WardPK-{uuid.uuid4().hex[:6]}"


def test_ward_pack_contains_all_admitted_of_the_ward_and_no_others(tmp_path, monkeypatch):
    monkeypatch.setenv("ORB_FHIR_EXPORT_DIR", str(tmp_path))
    ward, other_ward = _ward(), _ward()
    p1 = _mk_patient(ward=ward)
    p2 = _mk_patient(ward=ward)
    p_other = _mk_patient(ward=other_ward)
    p_gone = _mk_patient(status="discharged", ward=ward)

    result = build_ward_print_packs(created_by="auto")
    assert result["patients"] >= 3
    assert result["directory"] == str(tmp_path)

    latest = ds._ward_pack_path(tmp_path, ward, "latest")
    assert os.path.exists(latest)
    with open(latest, encoding="utf-8") as f:
        doc = f.read()

    # Every admitted patient of the ward, and nobody else's.
    assert _mrn(p1) in doc and _mrn(p2) in doc
    assert _mrn(p_other) not in doc
    assert _mrn(p_gone) not in doc

    # Census header + the clinical must-knows from the snapshot shapes.
    assert "2 admitted patient(s)" in doc
    assert "penicillin" in doc          # allergies, prominent
    assert "Amoxicillin" in doc and "500mg" in doc  # active meds with doses
    assert "NEWS2" in doc
    assert "Code status:" in doc

    # The other ward got its own pack.
    other_latest = ds._ward_pack_path(tmp_path, other_ward, "latest")
    assert os.path.exists(other_latest)
    with open(other_latest, encoding="utf-8") as f:
        assert _mrn(p_other) in f.read()


def test_ward_pack_survives_app_down_retrieval(tmp_path, monkeypatch):
    """THE POINT of the artifact: it must be a plain file on local disk that a
    browser can open with Orb not running — nothing here goes through the app."""
    monkeypatch.setenv("ORB_FHIR_EXPORT_DIR", str(tmp_path))
    ward = _ward()
    pid = _mk_patient(ward=ward)
    build_ward_print_packs()

    latest = ds._ward_pack_path(tmp_path, ward, "latest")
    assert os.path.exists(latest)
    assert (os.stat(latest).st_mode & 0o777) == 0o600  # PHI on disk stays private
    with open(latest, encoding="utf-8") as f:
        doc = f.read()
    assert doc.startswith("<!DOCTYPE html>")
    assert _mrn(pid) in doc

    # A predictable timestamped sibling exists next to the stable 'latest' name.
    stamped = [
        n for n in os.listdir(tmp_path)
        if n.startswith("ward-pack.") and n.endswith(".html")
        and not n.endswith(".latest.html")
    ]
    assert stamped


def test_ward_pack_escapes_untrusted_text(tmp_path, monkeypatch):
    """Names and notes are free text; a patient name must never become markup
    in an artifact the ward opens in a browser."""
    monkeypatch.setenv("ORB_FHIR_EXPORT_DIR", str(tmp_path))
    ward = _ward()
    pid = _mk_patient(ward=ward)
    with db_session() as db:
        db.execute(
            "UPDATE patients SET name = ? WHERE id = ?",
            ('<script>alert("x")</script>', pid),
        )
    build_ward_print_packs()
    with open(ds._ward_pack_path(tmp_path, ward, "latest"), encoding="utf-8") as f:
        doc = f.read()
    assert "<script>alert" not in doc
    assert "&lt;script&gt;" in doc


def test_sweep_tick_builds_ward_packs(tmp_path, monkeypatch):
    """One tick of the lifespan's hourly downtime_snapshot_sweep must leave a
    fresh ward pack on disk — the pack has no scheduler of its own."""
    monkeypatch.setenv("ORB_FHIR_EXPORT_DIR", str(tmp_path))
    # A fresh marker makes the nightly FHIR branch skip as "too soon", so the
    # tick under test is snapshot capture + ward packs only.
    (tmp_path / ds._LAST_RUN_MARKER).touch()
    ward = _ward()
    pid = _mk_patient(ward=ward)

    import main as main_mod

    async def fake_sleep(seconds, *args, **kwargs):
        if seconds >= 3600:  # end of the first tick
            raise asyncio.CancelledError

    monkeypatch.setattr(main_mod.asyncio, "sleep", fake_sleep)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(main_mod.downtime_snapshot_sweep())

    latest = ds._ward_pack_path(tmp_path, ward, "latest")
    assert os.path.exists(latest)
    with open(latest, encoding="utf-8") as f:
        assert _mrn(pid) in f.read()
    # The tick also captured the snapshot the pack renders from.
    assert get_latest_snapshot(pid) is not None


def test_ward_pack_retention_prunes_old_packs_only(tmp_path, monkeypatch):
    monkeypatch.setenv("ORB_FHIR_EXPORT_DIR", str(tmp_path))
    stale_epoch = time.time() - 4 * 86400  # past the 3-day default

    old_pack = tmp_path / "ward-pack.OldWard.deadbeef.20260701-000000.html"
    old_pack.write_text("<!DOCTYPE html>old")
    os.utime(old_pack, (stale_epoch, stale_epoch))
    # An equally old NON-pack file must be untouched — the FHIR bundles have
    # their own retention (prune_fhir_self_exports).
    old_bundle = tmp_path / "PAT-OLD.12345678.fhir.json"
    old_bundle.write_text("{}")
    os.utime(old_bundle, (stale_epoch, stale_epoch))

    ward = _ward()
    _mk_patient(ward=ward)
    result = build_ward_print_packs()  # prunes in the same call the sweep makes

    assert result["pruned"]["removed"] == 1
    assert not old_pack.exists()
    assert old_bundle.exists()
    assert os.path.exists(ds._ward_pack_path(tmp_path, ward, "latest"))


def test_ward_pack_route_serves_in_scope_and_refuses_out_of_scope(tmp_path, monkeypatch):
    monkeypatch.setenv("ORB_FHIR_EXPORT_DIR", str(tmp_path))
    ward = _ward()
    pid = _mk_patient(ward=ward)
    _admit(pid, DEPT_A)
    build_ward_print_packs()

    doc_a = _mk_user("doctor", DEPT_A)
    doc_b = _mk_user("doctor", DEPT_B)

    resp = client.get(f"/api/downtime/{pid}/ward-pack", headers=_auth(doc_a, "doctor"))
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/html")
    assert _mrn(pid) in resp.text

    resp = client.get(f"/api/downtime/{pid}/ward-pack", headers=_auth(doc_b, "doctor"))
    assert resp.status_code == 404, resp.text
    assert resp.json()["detail"] == "Patient not found"
    assert _mrn(pid) not in resp.text


def test_ward_pack_route_builds_a_missing_pack_on_request(tmp_path, monkeypatch):
    """Since 2026-10-03 the route rebuilds rather than 404ing when the hourly
    sweep has not written this ward's pack yet (a new ward, a fresh boot)."""
    monkeypatch.setenv("ORB_FHIR_EXPORT_DIR", str(tmp_path))
    ward = _ward()
    pid = _mk_patient(ward=ward)
    _admit(pid, DEPT_A)
    doc_a = _mk_user("doctor", DEPT_A)

    resp = client.get(f"/api/downtime/{pid}/ward-pack", headers=_auth(doc_a, "doctor"))
    assert resp.status_code == 200, resp.text
    assert _mrn(pid) in resp.text
    # Authenticated responses never publish a shared ward artifact.
    assert not os.path.exists(ds._ward_pack_path(tmp_path, ward, "latest"))


def test_ward_pack_route_404s_when_the_ward_has_no_pack_and_nobody_admitted(tmp_path, monkeypatch):
    monkeypatch.setenv("ORB_FHIR_EXPORT_DIR", str(tmp_path))
    pid = _mk_patient(status="discharged", ward=_ward())
    _admit(pid, DEPT_A)
    doc_a = _mk_user("doctor", DEPT_A)

    resp = client.get(f"/api/downtime/{pid}/ward-pack", headers=_auth(doc_a, "doctor"))
    assert resp.status_code == 404, resp.text
    assert resp.json()["detail"] == "No ward pack generated for this ward yet"


# ═════════════════════════════════════════════════════════════════════════════
# Scope-refusal semantics — DEMO_READINESS §15.1
# ═════════════════════════════════════════════════════════════════════════════
# The posture the chart/timeline/orders/eMAR routes established: an out-of-ward
# patient answers 404 {"detail": "Patient not found"}, byte-identical to a
# missing record, so the refusal never confirms the record exists. /downtime and
# /handoff (ipass) must give the same answer — capture especially, which used to
# skip the scope check entirely and hand the WHOLE snapshot to any vitals.read
# holder in any department.

def test_downtime_scope_refusals_match_the_established_posture():
    pid = _mk_patient()
    _admit(pid, DEPT_B)
    doc_a = _mk_user("doctor", DEPT_A)
    doc_b = _mk_user("doctor", DEPT_B)
    mrn = _mrn(pid)

    # In-scope control: the owning department captures and reads normally.
    resp = client.post(f"/api/downtime/capture/{pid}", headers=_auth(doc_b, "doctor"))
    assert resp.status_code == 200, resp.text
    assert mrn in resp.text
    resp = client.get(f"/api/downtime/{pid}/latest", headers=_auth(doc_b, "doctor"))
    assert resp.status_code == 200, resp.text

    # Out-of-scope: every downtime surface answers the one established refusal —
    # and leaks nothing, even though a snapshot now exists.
    hdr = _auth(doc_a, "doctor")
    for method, path in (
        ("post", f"/api/downtime/capture/{pid}"),
        ("get", f"/api/downtime/{pid}/latest"),
        ("get", f"/api/downtime/{pid}/history"),
        ("get", f"/api/downtime/{pid}/ward-pack"),
    ):
        resp = getattr(client, method)(path, headers=hdr)
        assert resp.status_code == 404, f"{method} {path}: {resp.text}"
        assert resp.json()["detail"] == "Patient not found", f"{method} {path}"
        assert mrn not in resp.text, f"{method} {path} leaked PHI"


def test_handoff_scope_refusal_matches_the_established_posture():
    pid = _mk_patient()
    _admit(pid, DEPT_B)
    doc_a = _mk_user("doctor", DEPT_A)
    doc_b = _mk_user("doctor", DEPT_B)

    # In-scope control.
    resp = client.get(f"/api/ipass/{pid}/draft", headers=_auth(doc_b, "doctor"))
    assert resp.status_code == 200, resp.text

    # Out-of-scope refusal, identical body.
    resp = client.get(f"/api/ipass/{pid}/draft", headers=_auth(doc_a, "doctor"))
    assert resp.status_code == 404, resp.text
    assert resp.json()["detail"] == "Patient not found"
    assert _mrn(pid) not in resp.text


def test_unknown_id_and_out_of_ward_id_are_indistinguishable():
    """Whether the caller is ward-scoped (refusal from the dependency) or
    unscoped (refusal from the route body), a missing id must produce the exact
    body an out-of-ward id produces — no ': <id>' echo, no existence signal."""
    doc_a = _mk_user("doctor", DEPT_A)
    doc_unscoped = _mk_user("doctor", None)
    missing = f"NOSUCH-{uuid.uuid4().hex[:8].upper()}"

    for hdr in (_auth(doc_a, "doctor"), _auth(doc_unscoped, "doctor")):
        for method, path in (
            ("post", f"/api/downtime/capture/{missing}"),
            ("get", f"/api/ipass/{missing}/draft"),
        ):
            resp = getattr(client, method)(path, headers=hdr)
            assert resp.status_code == 404, f"{method} {path}: {resp.text}"
            assert resp.json()["detail"] == "Patient not found", f"{method} {path}"
            assert missing not in resp.text, f"{method} {path} echoed the id"


def test_snapshot_names_the_people_not_their_ids():
    """The printed sheet named the note's signer and the capturer by user id
    ("DOC001"). Names travel with the ids now; the scheduled capture has none."""
    uid = f"DTN{uuid.uuid4().hex[:6].upper()}"
    pid = _mk_patient()
    with db_session() as db:
        db.execute(
            "INSERT INTO users (id, name, role, password_hash) VALUES (?, 'Dr. Downtime Namecheck', 'doctor', 'x')",
            (uid,),
        )
        db.execute(
            """INSERT INTO scribe_notes (patient_id, author_id, raw_transcript, structured_note,
                                         note_format, status, signed_by, signed_at)
               VALUES (?, ?, 'raw', 'S: named note', 'soap', 'signed', ?, '2026-01-02T10:00:00+00:00')""",
            (pid, uid, uid),
        )

    captured = capture_snapshot(pid, created_by=uid)
    assert captured["created_by_name"] == "Dr. Downtime Namecheck"
    named = [n for n in captured["snapshot"]["recent_notes"] if n["structured_note"] == "S: named note"]
    assert named and named[0]["signed_by_name"] == "Dr. Downtime Namecheck"
    assert named[0]["author_name"] == "Dr. Downtime Namecheck"

    latest = get_latest_snapshot(pid)
    assert latest["created_by"] == uid
    assert latest["created_by_name"] == "Dr. Downtime Namecheck"

    auto = capture_snapshot(pid, created_by="auto")
    assert auto["created_by_name"] is None
