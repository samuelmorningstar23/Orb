"""The diagnosis code release loader (opd-20, W9-G).

What this file pins:

  1. LICENCE GATE (ruling OPD-N11): nothing is written without counsel's
     licence reference; the script then runs as a dry run and says so.
  2. IDEMPOTENT: the same file twice gives the same row count and the second
     run writes nothing (no second audit row); a version row names the release
     and is never read as a code.
  3. AN INACTIVE CODE NO LONGER VALIDATES: a code the release marks inactive,
     or (with retire_missing) leaves out, stops validating; nothing is deleted;
     the starter seed never brings a retired code back.
  4. SIGNED CONSULTATIONS ARE UNTOUCHED: re-validation runs only on request,
     changes unsigned notes only, never ticks a diagnosis for the problem list,
     never bumps the draft's row_version, and only counts signed notes.
  5. The file formats, code normalisation, refusals, the audit row and a
     failed audit write (nothing kept).

Every test restores the terminology table and removes the notes it made, so
later files in the session see the starter set exactly as before.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

from database import db_session
from services import terminology_service as ts

SERVER = Path(__file__).resolve().parent.parent
SCRIPT = SERVER / "scripts" / "load_terminology.py"
SAMPLE = SERVER / "scripts" / "fixtures" / "icd10_release_sample.csv"


def _script():
    spec = importlib.util.spec_from_file_location("load_terminology_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def _restore_terminology():
    """Snapshot ICD10 and RELEASE rows; put them back exactly afterwards."""
    ts.seed_terminology()
    with db_session() as db:
        before = {(r["code_system"], r["code"]): (r["display"], r["is_active"]) for r in db.execute(
            "SELECT code_system, code, display, is_active FROM terminology "
            "WHERE code_system IN ('ICD10', ?)", (ts.RELEASE_CODE_SYSTEM,)).fetchall()}
    yield
    with db_session() as db:
        now = {(r["code_system"], r["code"]) for r in db.execute(
            "SELECT code_system, code FROM terminology WHERE code_system IN ('ICD10', ?)",
            (ts.RELEASE_CODE_SYSTEM,)).fetchall()}
        for key in now - set(before):
            db.execute("DELETE FROM terminology WHERE code_system = ? AND code = ?", key)
        for (system, code), (display, active) in before.items():
            db.execute("UPDATE terminology SET display = ?, is_active = ? "
                       "WHERE code_system = ? AND code = ?", (display, active, system, code))


def _count(system="ICD10"):
    with db_session() as db:
        return db.execute("SELECT COUNT(*) FROM terminology WHERE code_system = ?",
                          (system,)).fetchone()[0]


def _audits(action):
    with db_session() as db:
        return [dict(r) for r in db.execute(
            "SELECT * FROM audit_logs WHERE action_type = ? ORDER BY id", (action,)).fetchall()]


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _tag():
    return uuid.uuid4().hex[:2].upper()


def _codes(n=3):
    """Synthetic ICD-10-shaped codes no starter row or other test uses."""
    tag = str(uuid.uuid4().int % 90 + 10)
    return [f"U9{tag[0]}.{tag[1]}{i}" for i in range(n)]


def _load(entries, **kw):
    params = dict(code_system="ICD10", release="Test release", version="T1",
                  source_sha256=_sha(json.dumps(entries)), source_name="test.csv",
                  licence_ref="Counsel test reference", operator="pytest")
    params.update(kw)
    return ts.load_release(entries, **params)


# ═════════════════════════════════════════════════════════════════════════════
# 1. The licence gate
# ═════════════════════════════════════════════════════════════════════════════

def test_a_load_without_a_licence_reference_is_refused_and_writes_nothing():
    codes = _codes()
    before = _count()
    with pytest.raises(ts.TerminologyLoadError) as err:
        _load([(c, f"Synthetic {c}", True) for c in codes], licence_ref=None)
    assert err.value.code == "licence_required"
    assert _count() == before
    assert ts.release_info("ICD10") is None or ts.release_info("ICD10").get("release") != "Test release"


def test_a_dry_run_reports_and_writes_nothing():
    codes = _codes()
    before = _count()
    audits = len(_audits("terminology_release_loaded"))
    report = _load([(c, f"Synthetic {c}", True) for c in codes], licence_ref=None, dry_run=True)
    assert report["dry_run"] is True and report["applied"] is False
    assert report["inserted"] == 3 and report["active_after"] == report["active_before"] + 3
    assert _count() == before
    assert not ts.validate_code("ICD10", codes[0])
    assert len(_audits("terminology_release_loaded")) == audits


def test_the_script_without_a_licence_reference_runs_as_a_dry_run(tmp_path, capsys):
    codes = _codes()
    path = tmp_path / "release.csv"
    path.write_text("code,display\n" + "".join(f"{c},Synthetic {c}\n" for c in codes), "utf-8")
    before = _count()
    rc = _script().main(["--file", str(path), "--release", "Test release", "--version", "T1"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "ruling OPD-N11" in out and "Dry run: nothing was written." in out
    assert _count() == before


# ═════════════════════════════════════════════════════════════════════════════
# 2. Idempotent, with a version row
# ═════════════════════════════════════════════════════════════════════════════

def test_loading_twice_gives_the_same_row_count_and_writes_once():
    codes = _codes()
    entries = [(c, f"Synthetic {c}", True) for c in codes]
    audits = len(_audits("terminology_release_loaded"))
    first = _load(entries)
    count = _count()
    second = _load(entries)
    assert first["applied"] is True and first["inserted"] == 3
    assert second["applied"] is False and second["already_loaded"] is True
    assert second["inserted"] == 0 and second["unchanged"] == 3
    assert _count() == count
    assert len(_audits("terminology_release_loaded")) == audits + 1
    for c in codes:
        assert ts.validate_code("ICD10", c)
        assert ts.get_display("ICD10", c) == f"Synthetic {c}"


def test_the_version_row_names_the_release_and_is_never_a_code():
    codes = _codes()
    entries = [(c, f"Synthetic {c}", True) for c in codes]
    sha = _sha("version row")
    _load(entries, source_sha256=sha, release="WHO ICD-10 (test)", version="2019-T")
    info = ts.release_info("ICD10")
    assert info["release"] == "WHO ICD-10 (test)" and info["version"] == "2019-T"
    assert info["source_sha256"] == sha and info["licence_ref"] == "Counsel test reference"
    assert info["codes_in_release"] == 3 and info["loaded_by"] == "pytest"
    with db_session() as db:
        row = db.execute("SELECT is_active FROM terminology WHERE code_system = ? AND code = 'ICD10'",
                         (ts.RELEASE_CODE_SYSTEM,)).fetchone()
        assert row["is_active"] == 0
        assert db.execute("SELECT COUNT(*) FROM terminology WHERE code_system = ?",
                          (ts.RELEASE_CODE_SYSTEM,)).fetchone()[0] == 1
    assert ts.validate_code(ts.RELEASE_CODE_SYSTEM, "ICD10") is False
    assert ts.get_display(ts.RELEASE_CODE_SYSTEM, "ICD10") is None
    assert ts.search_terms("ICD10", kind="diagnosis")["results"] == [] or all(
        r["code_system"] != ts.RELEASE_CODE_SYSTEM
        for r in ts.search_terms("ICD10", kind="diagnosis")["results"])
    assert ts.RELEASE_CODE_SYSTEM not in ts.seed_terminology()["counts"]
    # A new version of the same file contents still records the new label.
    again = _load(entries, source_sha256=sha, release="WHO ICD-10 (test)", version="2019-T2")
    assert again["applied"] is True and again["inserted"] == 0
    assert ts.release_info("ICD10")["version"] == "2019-T2"


def test_the_load_is_audited_as_clinical_configuration():
    codes = _codes()
    _load([(c, f"Synthetic {c}", True) for c in codes], version="T-audit")
    row = _audits("terminology_release_loaded")[-1]
    assert row["event_category"] == "clinical_config"
    assert row["actor_role"] == "console" and row["target_id"] == "ICD10"
    meta = json.loads(row["metadata_json"])
    assert meta["version"] == "T-audit" and meta["inserted"] == 3
    assert meta["licence_ref"] == "Counsel test reference" and meta["operator"] == "pytest"


def test_a_failed_audit_write_keeps_nothing(monkeypatch):
    from services import audit_service
    from services.audit_service import AuditWriteError

    def failing(*args, **kwargs):
        raise AuditWriteError("simulated outage")

    monkeypatch.setattr(audit_service, "log_audit_event", failing)
    codes = _codes()
    before = _count()
    with pytest.raises(AuditWriteError):
        _load([(c, f"Synthetic {c}", True) for c in codes], version="T-outage")
    assert _count() == before
    assert not ts.validate_code("ICD10", codes[0])
    info = ts.release_info("ICD10")
    assert info is None or info.get("version") != "T-outage"


# ═════════════════════════════════════════════════════════════════════════════
# 3. An inactive code no longer validates
# ═════════════════════════════════════════════════════════════════════════════

def test_a_code_the_release_marks_inactive_no_longer_validates():
    a, b, c = _codes()
    _load([(a, f"Synthetic {a}", True), (b, f"Synthetic {b}", True), (c, f"Synthetic {c}", True)])
    assert ts.validate_code("ICD10", b)
    count = _count()
    report = _load([(a, f"Synthetic {a}", True), (b, f"Synthetic {b}", False),
                    (c, f"Synthetic {c} reworded", True)], version="T2")
    assert report["deactivated"] == 1 and report["display_updated"] == 1
    assert not ts.validate_code("ICD10", b)
    assert ts.get_display("ICD10", c) == f"Synthetic {c} reworded"
    assert _count() == count, "nothing is deleted"
    # The release makes it current again.
    report = _load([(b, f"Synthetic {b}", True)], version="T3")
    assert report["reactivated"] == 1 and ts.validate_code("ICD10", b)


def test_retire_missing_retires_codes_the_release_leaves_out_and_the_seed_never_revives_them():
    a, b = _codes(2)
    starter = "E11.9"
    assert ts.validate_code("ICD10", starter)
    count = _count()
    report = _load([(a, f"Synthetic {a}", True)], retire_missing=True, version="T-retire")
    assert report["retired_missing"] >= 1
    assert not ts.validate_code("ICD10", starter)
    assert ts.validate_code("ICD10", a)
    assert _count() == count + 1, "retired codes stay in the table"
    ts.seed_terminology()
    assert not ts.validate_code("ICD10", starter), "the starter seed does not bring it back"
    # Without retire_missing a later release leaves out codes and retires none.
    report = _load([(b, f"Synthetic {b}", True)], version="T-keep")
    assert report["retired_missing"] == 0 and ts.validate_code("ICD10", a)


# ═════════════════════════════════════════════════════════════════════════════
# 4. Signed consultations are untouched
# ═════════════════════════════════════════════════════════════════════════════

def _note(status, diagnoses):
    cid = f"OPC-T{uuid.uuid4().hex[:7].upper()}"
    now = datetime.now(timezone.utc).isoformat()
    with db_session() as db:
        db.execute(
            "INSERT INTO opd_consultations (id, visit_id, patient_id, department, author_id, "
            "status, diagnoses_json, row_version, created_at, updated_at, updated_by, "
            "signed_by, signed_at) VALUES (?, ?, ?, 'Terminology Test Clinic', 'TERMDOC', ?, ?, "
            "4, ?, ?, 'TERMDOC', ?, ?)",
            (cid, f"OPD-T{uuid.uuid4().hex[:7]}", f"PAT-T{uuid.uuid4().hex[:7]}", status,
             json.dumps(diagnoses), now, now,
             "TERMDOC" if status == "signed" else None, now if status == "signed" else None))
    return cid


def _row(cid):
    with db_session() as db:
        return dict(db.execute("SELECT * FROM opd_consultations WHERE id = ?", (cid,)).fetchone())


@pytest.fixture
def notes():
    made = []
    yield made
    with db_session() as db:
        for cid in made:
            db.execute("DELETE FROM opd_consultations WHERE id = ?", (cid,))


def test_revalidation_changes_drafts_only_on_request_and_never_a_signed_note(notes):
    new_code, gone_code = _codes(2)
    _load([(gone_code, f"Synthetic {gone_code}", True)], version="T-pre")
    diagnoses = [
        {"text": "Something new", "code_system": "ICD10", "code": new_code,
         "code_status": "unvalidated", "add_to_problems": False},
        {"text": "Something retired", "code_system": "ICD10", "code": gone_code,
         "code_status": "validated", "add_to_problems": True},
        {"text": "Free text only", "code_system": None, "code": None,
         "code_status": "none", "add_to_problems": False},
    ]
    draft = _note("draft", diagnoses)
    signed = _note("signed", diagnoses)
    notes.extend([draft, signed])
    signed_before = _row(signed)
    draft_before = _row(draft)

    # The load alone touches no note.
    _load([(new_code, f"Synthetic {new_code}", True), (gone_code, f"Synthetic {gone_code}", False)],
          version="T-post")
    assert _row(draft) == draft_before and _row(signed) == signed_before

    dry = ts.revalidate_draft_diagnoses(operator="pytest", dry_run=True)
    assert dry["applied"] is False and dry["diagnoses_validated"] >= 1
    assert _row(draft) == draft_before

    audits = len(_audits("opd_diagnosis_codes_revalidated"))
    report = ts.revalidate_draft_diagnoses(operator="pytest")
    assert report["applied"] is True and report["signed_untouched"] is True
    assert report["signed_with_codes_now_valid"] >= 1
    after = _row(draft)
    items = json.loads(after["diagnoses_json"])
    assert items[0]["code_status"] == "validated" and items[0]["add_to_problems"] is False
    assert items[1]["code_status"] == "unvalidated" and items[1]["add_to_problems"] is False
    assert items[2] == diagnoses[2]
    assert [i["text"] for i in items] == [d["text"] for d in diagnoses]
    assert [i["code"] for i in items] == [d["code"] for d in diagnoses]
    assert after["row_version"] == draft_before["row_version"]
    assert after["updated_by"] == draft_before["updated_by"]
    assert after["updated_at"] == draft_before["updated_at"]
    assert _row(signed) == signed_before, "a signed note is never rewritten"
    mine = [a for a in _audits("opd_diagnosis_codes_revalidated")[audits:] if a["target_id"] == draft]
    assert len(mine) == 1 and mine[0]["event_category"] == "clinical_record"
    assert not [a for a in _audits("opd_diagnosis_codes_revalidated")[audits:]
                if a["target_id"] == signed]
    # A second run finds nothing more to change on this draft.
    again = ts.revalidate_draft_diagnoses(operator="pytest")
    assert _row(draft)["diagnoses_json"] == after["diagnoses_json"]
    assert again["signed_untouched"] is True


def test_the_signed_note_trigger_still_refuses_a_change(notes):
    signed = _note("signed", [{"text": "x", "code_system": "ICD10", "code": "U99.9",
                               "code_status": "unvalidated", "add_to_problems": False}])
    notes.append(signed)
    with pytest.raises(Exception):
        with db_session() as db:
            db.execute("UPDATE opd_consultations SET diagnoses_json = '[]' WHERE id = ?", (signed,))


# ═════════════════════════════════════════════════════════════════════════════
# 5. Formats, normalisation and refusals
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("raw, stored", [
    ("E119", "E11.9"), ("e11.9", "E11.9"), (" I10 ", "I10"), ("J45909", "J45.909"),
    ("G01*", "G01"), ("A17.0†", "A17.0"), ("S72001A", "S72.001A"), ("E11.", "E11"),
    ("A00-A09", None), ("XIV", None), ("", None), ("123", None), ("E11.12345", None),
])
def test_codes_are_stored_dotted_and_ranges_are_not_codes(raw, stored):
    assert ts.normalise_release_code("ICD10", raw) == stored


def test_only_icd10_is_loadable():
    with pytest.raises(ts.TerminologyLoadError) as err:
        _load([("2345-7", "Glucose", True)], code_system="LOINC")
    assert err.value.code == "code_system_not_loadable"


@pytest.mark.parametrize("entries, code", [
    ([], "release_empty"),
    ([("A00-A09", "A range", True)], "release_empty"),
    ([("U98.1", "", True)], "release_row_without_display"),
    ([("U98.1", "One", True), ("U981", "Two", True)], "release_code_twice"),
])
def test_a_malformed_release_is_refused_whole(entries, code):
    before = _count()
    with pytest.raises(ts.TerminologyLoadError) as err:
        _load(entries)
    assert err.value.code == code
    assert _count() == before


def test_a_release_needs_its_name_version_and_file_hash():
    with pytest.raises(ts.TerminologyLoadError) as err:
        _load([("U98.1", "One", True)], release=" ")
    assert err.value.code == "release_unnamed"
    with pytest.raises(ts.TerminologyLoadError) as err:
        _load([("U98.1", "One", True)], source_sha256="not-a-hash")
    assert err.value.code == "source_unidentified"


def test_the_file_readers(tmp_path):
    mod = _script()
    tsv = tmp_path / "release.tsv"
    tsv.write_text("ICD10_Code\tLong_Description\tStatus\nE119\tDiabetes\tactive\n"
                   "U98.1\tRetired thing\tretired\n", "utf-8")
    assert mod.read_release(tsv) == [("E119", "Diabetes", True), ("U98.1", "Retired thing", False)]
    plain = tmp_path / "codes.txt"
    plain.write_text("E119    Type 2 diabetes mellitus without complications\nI10     Essential\n",
                     "utf-8")
    assert mod.read_release(plain, "code-title") == [
        ("E119", "Type 2 diabetes mellitus without complications", True), ("I10", "Essential", True)]
    with pytest.raises(mod.ReleaseFileError):
        mod.read_release(plain)  # .txt needs --format
    bad = tmp_path / "bad.csv"
    bad.write_text("code,display,active\nE119,Diabetes,maybe\n", "utf-8")
    with pytest.raises(mod.ReleaseFileError):
        mod.read_release(bad)
    headless = tmp_path / "headless.csv"
    headless.write_text("E119,Diabetes\n", "utf-8")
    with pytest.raises(mod.ReleaseFileError):
        mod.read_release(headless)
    rows = mod.read_release(SAMPLE)
    assert ("A00-A09", "A block range: skipped as not a code", True) in rows


def test_the_script_loads_the_sample_once_and_reports_status(capsys):
    mod = _script()
    before = _count()
    args = ["--file", str(SAMPLE), "--release", "Sample", "--version", "S1",
            "--licence-ref", "Counsel test reference", "--json"]
    assert mod.main(args) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["applied"] is True and first["skipped_not_a_code"] == 1
    # The sample is the starter set's own wording, so no code changes.
    assert first["inserted"] == 0 and first["unchanged"] == 4
    assert _count() == before
    assert mod.main(args) == 0
    second = json.loads(capsys.readouterr().out)
    assert second["already_loaded"] is True and second["applied"] is False
    assert mod.main(["--status"]) == 0
    status = capsys.readouterr().out
    assert "ICD10: Sample S1" in status and "Counsel test reference" in status


def test_the_script_refuses_an_unknown_code_system(tmp_path, capsys):
    path = tmp_path / "loinc.csv"
    path.write_text("code,display\n2345-7,Glucose\n", "utf-8")
    rc = _script().main(["--file", str(path), "--code-system", "LOINC", "--release", "L",
                         "--version", "1", "--licence-ref", "x"])
    assert rc == 1
    assert "ICD10 only" in capsys.readouterr().err
