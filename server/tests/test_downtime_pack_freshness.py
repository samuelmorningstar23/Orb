"""Downtime snapshot completeness and ward pack freshness (2026-10-03, F082).

Four gaps closed here, each pinned below:

  * The snapshot froze signed Scribe notes only. A downtime copy with no chart
    notes, no lab results, no code status and no open escalation is missing
    exactly what a crash call or a night handover asks for first.
  * The ward pack route served the hourly file as it was, so a patient
    admitted after the last sweep was not in the pack printed for the ward.
    It now rebuilds when the file is stale or misses an admitted patient,
    and lists anyone it still cannot cover on the first page.
  * The pack lived only on the appliance it covers. ORB_WARD_PACK_MIRROR_DIR
    now gets a copy of every latest pack.
  * An empty allergy list printed as "NKDA", and AI-extracted drugs printed
    like prescriptions. Neither may happen on a downtime sheet.
"""
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

client = TestClient(app)

DEPT = "Downtime Freshness Medicine"
_SEEDED: list[str] = []


@pytest.fixture(autouse=True)
def _discharge_seeded_patients():
    """Leave nobody admitted in the shared session DB (see test_downtime.py:
    every leftover admitted patient slows the FHIR export tests)."""
    yield
    if not _SEEDED:
        return
    with db_session() as db:
        db.executemany("UPDATE patients SET status = 'discharged' WHERE id = ?",
                       [(p,) for p in _SEEDED])
        db.executemany("UPDATE clinical_escalations SET status = 'resolved' WHERE patient_id = ?",
                       [(p,) for p in _SEEDED])
    _SEEDED.clear()


@pytest.fixture()
def export_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("ORB_FHIR_EXPORT_DIR", str(tmp_path))
    monkeypatch.delenv("ORB_WARD_PACK_MIRROR_DIR", raising=False)
    return tmp_path


def _ward() -> str:
    return f"WardFR-{uuid.uuid4().hex[:6]}"


def _user(role="doctor", department=DEPT, name=None) -> str:
    uid = f"DFU{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO users (id, name, role, password_hash, department) VALUES (?, ?, ?, 'x', ?)",
            (uid, name or f"Freshness {role}", role, department),
        )
    return uid


def _patient(ward: str, *, allergies: bool = True, status: str = "admitted") -> tuple[str, str]:
    pid = f"PATFR{uuid.uuid4().hex[:6].upper()}"
    mrn = f"MRN-FR-{uuid.uuid4().hex[:6]}"
    with db_session() as db:
        db.execute(
            """INSERT INTO patients (id, name, mrn, bed, ward, diagnosis, status, age, sex, on_scale_2)
               VALUES (?, 'Freshness Test', ?, 'B9', ?, 'Pneumonia', ?, 70, 'F', 0)""",
            (pid, mrn, ward, status),
        )
        if allergies:
            db.execute(
                """INSERT INTO patient_allergies (patient_id, substance, reaction, severity, status)
                   VALUES (?, 'penicillin', 'anaphylaxis', 'severe', 'active')""",
                (pid,),
            )
        db.execute(
            """INSERT INTO vitals (patient_id, timestamp, heart_rate, systolic_bp, diastolic_bp,
                                   spo2, temperature, respiratory_rate, consciousness, on_oxygen)
               VALUES (?, '2026-10-03T06:00:00+00:00', 96, 118, 70, 95, 37.6, 18, 'alert', 0)""",
            (pid,),
        )
    _SEEDED.append(pid)
    return pid, mrn


def _admit(pid: str, code_status: str = "dnr") -> None:
    with db_session() as db:
        db.execute(
            "INSERT OR IGNORE INTO users (id, name, role, password_hash) "
            "VALUES ('DFADM', 'Freshness Admitter', 'doctor', 'x')"
        )
        db.execute(
            "INSERT INTO admissions (id, patient_id, department, ward, bed, status, admitted_by, code_status) "
            "VALUES (?, ?, ?, ?, 'B9', 'active', 'DFADM', ?)",
            (f"ADM-{uuid.uuid4().hex[:8]}", pid, DEPT, DEPT, code_status),
        )


def _chart_extras(pid: str, author: str) -> None:
    """A chart note, a lab report, an interfaced result (plus a cancelled
    one), an open escalation and an unverified AI-extracted drug."""
    with db_session() as db:
        db.execute(
            """INSERT INTO clinical_documents (patient_id, doc_type, title, content, author_id, created_at)
               VALUES (?, 'Progress Note', 'Evening review', 'Chest clearer, plan oral switch tomorrow.', ?,
                       '2026-10-03 05:30:00')""",
            (pid, author),
        )
        db.execute(
            """INSERT INTO clinical_documents (patient_id, doc_type, title, content, author_id, created_at)
               VALUES (?, 'lab_result', 'Renal panel',
                       '  Creatinine: 2.5 mg/dL  [H]  (ref: 0.7-1.3)', ?, '2026-10-03 04:00:00')""",
            (pid, author),
        )
        db.execute(
            """INSERT INTO observations (patient_id, source, observation_name, value_numeric, unit,
                                         reference_range, abnormal_flag, status, observed_at)
               VALUES (?, 'ingested_hl7', 'Potassium', 5.9, 'mmol/L', '3.5-5.1', 'H', 'final',
                       '2026-10-03T03:00:00+00:00')""",
            (pid,),
        )
        db.execute(
            """INSERT INTO observations (patient_id, source, observation_name, value_numeric, unit,
                                         reference_range, abnormal_flag, status, observed_at)
               VALUES (?, 'ingested_hl7', 'Sodium', 101, 'mmol/L', '135-145', 'LL', 'entered-in-error',
                       '2026-10-03T03:00:00+00:00')""",
            (pid,),
        )
        db.execute(
            """INSERT INTO clinical_escalations (id, patient_id, status, trigger, news2, level,
                                                 current_tier, max_tier, tier_label, raised_at)
               VALUES (?, ?, 'open', 'news2', 6, 'critical', 2, 3, 'Registrar review',
                       '2026-10-03T05:00:00+00:00')""",
            (f"ESC-{uuid.uuid4().hex[:8]}", pid),
        )
        db.execute(
            """INSERT INTO medications (patient_id, drug_name, dosage, route, frequency, status, source)
               VALUES (?, 'Furosemide', '40mg', 'IV', 'BD', 'active', 'llm_extracted')""",
            (pid,),
        )


def _auth(user_id: str, role: str = "doctor") -> dict:
    return {"Authorization": f"Bearer {create_token(user_id, role)}"}


# ── The snapshot ─────────────────────────────────────────────────────────────

def test_snapshot_carries_notes_labs_code_status_and_escalations(export_dir):
    pid, _ = _patient(_ward())
    _admit(pid, code_status="dnr")
    author = _user(name="Dr Meera Iyer")
    _chart_extras(pid, author)

    snap = ds.capture_snapshot(pid, created_by="auto")["snapshot"]

    assert snap["schema_version"] == ds.SNAPSHOT_SCHEMA_VERSION == 2
    assert snap["code_status"] == "dnr"
    assert [e["tier_label"] for e in snap["open_escalations"]] == ["Registrar review"]
    assert [d["title"] for d in snap["recent_documents"]] == ["Evening review"]
    assert [r["title"] for r in snap["recent_lab_reports"]] == ["Renal panel"]
    # The cancelled sodium is not a result anyone should act on.
    assert [r["observation_name"] for r in snap["lab_results"]] == ["Potassium"]
    furosemide = next(m for m in snap["medications"] if m["drug_name"] == "Furosemide")
    assert "verified_by" in furosemide and furosemide["verified_by"] is None
    assert snap["counts"]["documents"] == 1 and snap["counts"]["lab_results"] == 1


def test_summary_and_pack_never_say_nkda(export_dir):
    ward = _ward()
    pid, mrn = _patient(ward, allergies=False)
    env = ds.capture_snapshot(pid)
    assert "NKDA" not in env["summary"]
    assert "No allergies recorded" in env["summary"]

    ds.build_ward_print_packs()
    with open(ds._ward_pack_path(export_dir, ward, "latest"), encoding="utf-8") as f:
        doc = f.read()
    assert "NKDA" not in doc
    assert "No allergies recorded. Confirm with the patient." in doc


def test_pack_prints_the_new_sections_and_flags_unverified_extractions(export_dir):
    ward = _ward()
    pid, mrn = _patient(ward)
    _admit(pid, code_status="dnr")
    _chart_extras(pid, _user(name="Dr Meera Iyer"))

    ds.build_ward_print_packs()
    with open(ds._ward_pack_path(export_dir, ward, "latest"), encoding="utf-8") as f:
        doc = f.read()

    assert "DNR (do not resuscitate)" in doc
    assert "Registrar review" in doc
    assert "Recent lab results" in doc and "Potassium" in doc and "5.9" in doc
    assert "Creatinine: 2.5 mg/dL" in doc
    assert "Sodium" not in doc  # entered in error, so not printed
    assert "Recent chart notes" in doc and "Chest clearer" in doc
    assert "Dr Meera Iyer" in doc  # named, not numbered
    assert "AI-extracted, not verified by a pharmacist" in doc


# ── Freshness on request ─────────────────────────────────────────────────────

def test_pack_requested_after_a_new_admission_includes_the_patient(export_dir):
    ward = _ward()
    first, first_mrn = _patient(ward)
    _admit(first)
    ds.build_ward_print_packs()

    # Admitted after the sweep wrote the file, which is still minutes fresh.
    late, late_mrn = _patient(ward)
    _admit(late)
    doctor = _user()

    resp = client.get(f"/api/downtime/{first}/ward-pack", headers=_auth(doctor))
    assert resp.status_code == 200, resp.text
    assert first_mrn in resp.text and late_mrn in resp.text
    assert "NOT IN THIS PACK" not in resp.text
    assert "2 admitted patient(s)" in resp.text


def test_a_stale_pack_is_rebuilt_from_fresh_snapshots(export_dir):
    ward = _ward()
    pid, mrn = _patient(ward)
    _admit(pid)
    ds.build_ward_print_packs()
    with db_session() as db:
        db.execute(
            """INSERT INTO patient_allergies (patient_id, substance, reaction, severity, status)
               VALUES (?, 'chlorhexidine', 'urticaria', 'moderate', 'active')""",
            (pid,),
        )
    latest = ds._ward_pack_path(export_dir, ward, "latest")
    old = time.time() - 600
    os.utime(latest, (old, old))

    result = ds.serve_ward_pack(pid, created_by="auto", max_age_s=0)
    assert result["rebuilt"] is True
    assert result["captured"] >= 1
    assert "chlorhexidine" in result["html"]


def test_a_fresh_complete_pack_is_served_without_rebuilding(export_dir):
    ward = _ward()
    pid, _ = _patient(ward)
    ds.build_ward_print_packs()
    before = os.path.getmtime(ds._ward_pack_path(export_dir, ward, "latest"))

    result = ds.serve_ward_pack(pid)
    assert result["rebuilt"] is False and result["missing"] == []
    assert os.path.getmtime(ds._ward_pack_path(export_dir, ward, "latest")) == before


def test_a_pack_that_could_not_be_rebuilt_lists_who_it_leaves_out(export_dir, monkeypatch):
    ward = _ward()
    pid, mrn = _patient(ward)
    ds.build_ward_print_packs()
    late, late_mrn = _patient(ward)

    def boom(*_a, **_k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(ds, "rebuild_ward_pack", boom)
    result = ds.serve_ward_pack(pid)

    assert result["rebuilt"] is False
    assert [p["id"] for p in result["missing"]] == [late]
    banner = result["html"].split('<section class="wp-page">')[0]
    assert "NOT IN THIS PACK: 1 admitted patient(s)" in banner
    assert late_mrn in banner and mrn not in banner


def test_a_rebuild_that_captured_snapshots_is_audited(export_dir):
    ward = _ward()
    pid, _ = _patient(ward)
    _admit(pid)
    doctor = _user()

    resp = client.get(f"/api/downtime/{pid}/ward-pack", headers=_auth(doctor))
    assert resp.status_code == 200, resp.text
    with db_session() as db:
        row = db.execute(
            """SELECT actor_user_id, metadata_json FROM audit_logs
               WHERE action_type = 'downtime_ward_pack_rebuilt' AND target_id = ?
               ORDER BY id DESC LIMIT 1""",
            (ward,),
        ).fetchone()
    assert row is not None
    assert row["actor_user_id"] == doctor
    meta = json.loads(row["metadata_json"])
    assert meta["captured"] >= 1


# ── Off the appliance ────────────────────────────────────────────────────────

def test_latest_packs_are_mirrored_when_a_mirror_is_set(export_dir, tmp_path_factory, monkeypatch):
    mirror = tmp_path_factory.mktemp("pack-mirror")
    monkeypatch.setenv("ORB_WARD_PACK_MIRROR_DIR", f"  {mirror}\n")  # stripped
    ward = _ward()
    pid, mrn = _patient(ward)

    result = ds.build_ward_print_packs()

    copy = ds._ward_pack_path(mirror, ward, "latest")
    assert copy in result["mirrored"]
    assert (os.stat(copy).st_mode & 0o777) == 0o600
    with open(copy, encoding="utf-8") as f, \
            open(ds._ward_pack_path(export_dir, ward, "latest"), encoding="utf-8") as g:
        assert f.read() == g.read()
    # Only the latest file goes to the mirror, never the timestamped history.
    assert all(n.endswith(".latest.html") for n in os.listdir(mirror) if n.startswith("ward-pack."))


def test_a_dead_mirror_never_costs_the_local_pack(export_dir, tmp_path_factory, monkeypatch):
    blocker = tmp_path_factory.mktemp("not-a-dir") / "file"
    blocker.write_text("x")
    monkeypatch.setenv("ORB_WARD_PACK_MIRROR_DIR", str(blocker / "sub"))
    ward = _ward()
    _patient(ward)

    result = ds.build_ward_print_packs()
    assert result["mirrored"] == []
    assert os.path.exists(ds._ward_pack_path(export_dir, ward, "latest"))


def test_a_pack_from_before_the_markers_is_checked_by_mrn(export_dir, monkeypatch):
    """A pack written by the previous build has no page markers; its pages
    are still recognised by the MRN they print, so nobody is listed as
    missing who is in fact on paper."""
    ward = _ward()
    present, present_mrn = _patient(ward)
    absent, absent_mrn = _patient(ward)
    legacy = ("<!DOCTYPE html><html><body><section class=\"wp-page\">census</section>"
              f"<section class=\"wp-page\"><span><b>MRN</b>{present_mrn}</span></section>"
              "</body></html>")
    with open(ds._ward_pack_path(export_dir, ward, "latest"), "w", encoding="utf-8") as f:
        f.write(legacy)
    monkeypatch.setattr(ds, "rebuild_ward_pack", lambda *a, **k: None)  # lock busy

    result = ds.serve_ward_pack(present)
    assert [p["id"] for p in result["missing"]] == [absent]
    assert absent_mrn in result["html"].split('<section class="wp-page">')[0]
