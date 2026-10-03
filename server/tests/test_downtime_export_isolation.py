"""The FHIR self-export must never write PHI into the source tree.

FINDING (2026-08-08). Every full-suite run left patient FHIR `$everything`
bundles in `server/downtime_exports/` and never swept them: 390 files / 339 MB
at the time of writing, ~30 added per run. That is bad on three counts —

  1. PHI. The bundles are whole patient records, and the FILENAMES alone are
     PHI (`PAT-1BAE82C6.9992ec5a.fhir.json` encodes the patient id). The
     service is careful enough about this to chmod the directory 0o700; piling
     it up untracked inside a source checkout undoes that care.
  2. Concurrency. `downtime_exports/` is a SHARED, non-temp path that every
     concurrent pytest process writes to — including the `.last_fhir_export`
     throttle marker, which is process-global state one run can hide from
     another. It has already caused cross-agent stalls.
  3. Hygiene. 339 MB of unreferenced files in a working tree.

ROOT CAUSE, precisely. `downtime_service.fhir_self_export_dir()` was ALREADY
env-overridable via `ORB_FHIR_EXPORT_DIR`, resolved lazily at call time — the
same shape as `loop_watchdog._default_dir()`'s `ORB_WATCHDOG_DIR`. The missing
half was in the test harness: `tests/conftest.py` redirects `ORB_DB_PATH` and
`ORB_WATCHDOG_DIR` to throwaway paths at module import, but not
`ORB_FHIR_EXPORT_DIR`. So any export that did not set the var itself fell
through to the repo default. Twelve tests in `test_downtime_fhir_export.py` do
monkeypatch it; anything that does not — the lifespan's `downtime_snapshot_sweep`
under a long-lived TestClient, `prune_*` with no argument, a new test written
tomorrow — writes into the checkout.

These tests pin BOTH halves: that the env var is honoured, and that a test
session's ambient default is not the source tree.
"""
import os
import time
import uuid
from pathlib import Path

import pytest

from config import BASE_DIR
from database import db_session
from services.downtime_service import (
    fhir_self_export_dir,
    run_nightly_fhir_self_export,
)

#: The in-repo location the export falls back to when nothing is configured.
#: This is the correct PRODUCTION default (an appliance ships as a directory,
#: not as a checkout) — it is only wrong for a test run.
REPO_EXPORT_DIR = Path(BASE_DIR) / "downtime_exports"

#: Snapshot taken at COLLECTION time, before any test in the session has run.
#: Used as a canary: if the source tree gained a bundle while the suite ran,
#: something is still resolving the export path to the checkout.
_BUNDLES_AT_COLLECTION = (
    {p.name for p in REPO_EXPORT_DIR.glob("*.fhir.json")}
    if REPO_EXPORT_DIR.is_dir()
    else set()
)


def _is_inside(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


@pytest.fixture
def admitted_patient():
    """An admitted patient, DISCHARGED on teardown.

    Discharging matters beyond tidiness: run_nightly_fhir_self_export exports
    every *admitted* patient, so a fixture that leaves its patient admitted makes
    every later export in the session slower and larger — which is exactly how
    server/downtime_exports/ reached 339 MB.
    """
    pid = _mk_admitted_patient()
    try:
        yield pid
    finally:
        with db_session() as db:
            db.execute("UPDATE patients SET status = 'discharged' WHERE id = ?", (pid,))


def _mk_admitted_patient() -> str:
    pid = f"PATISO{uuid.uuid4().hex[:6].upper()}"
    with db_session() as db:
        db.execute(
            """INSERT INTO patients (id, name, mrn, bed, ward, diagnosis, status, age, sex)
               VALUES (?, ?, ?, 'B9', 'Ward Z', 'Sepsis', 'admitted', 58, 'M')""",
            (pid, "Export Isolation Test", f"MRN-{uuid.uuid4().hex[:6]}"),
        )
        for i in range(5):
            db.execute(
                """INSERT INTO vitals (patient_id, timestamp, heart_rate, systolic_bp,
                                       diastolic_bp, spo2, temperature, respiratory_rate)
                   VALUES (?, ?, 88, 120, 78, 97, 37.0, 17)""",
                (pid, f"2026-08-0{i + 1}T0{i}:00:00Z"),
            )
    return pid


# ── The env var itself ───────────────────────────────────────────────────────

def test_export_dir_honours_the_env_var(tmp_path, monkeypatch):
    """ORB_FHIR_EXPORT_DIR wins over the built-in default, and is read at CALL
    time — not bound once at import. Lazy resolution is what lets conftest and
    per-test monkeypatch both work, exactly as ORB_WATCHDOG_DIR does."""
    monkeypatch.setenv("ORB_FHIR_EXPORT_DIR", str(tmp_path / "escrow"))
    assert Path(str(fhir_self_export_dir())) == tmp_path / "escrow"

    # Re-point it mid-process: a stale value would prove import-time binding.
    monkeypatch.setenv("ORB_FHIR_EXPORT_DIR", str(tmp_path / "second"))
    assert Path(str(fhir_self_export_dir())) == tmp_path / "second"


def test_export_dir_is_created_owner_only(tmp_path, monkeypatch):
    """The directory listing is PHI-adjacent (filenames encode patient ids), so
    the resolver must create it 0o700 wherever it is pointed."""
    target = tmp_path / "fresh"
    monkeypatch.setenv("ORB_FHIR_EXPORT_DIR", str(target))
    fhir_self_export_dir()
    assert target.is_dir()
    assert oct(target.stat().st_mode & 0o777) == "0o700"


def test_unset_env_var_falls_back_to_the_appliance_default(tmp_path, monkeypatch):
    """With nothing configured the service still has a sane on-appliance home.
    Asserted so the conftest isolation below can never be mistaken for the
    production default having moved."""
    monkeypatch.delenv("ORB_FHIR_EXPORT_DIR", raising=False)
    assert Path(str(fhir_self_export_dir())) == REPO_EXPORT_DIR


@pytest.mark.parametrize("blank", ["", "   ", "\n", "\r\n", "\t"])
def test_blank_env_var_is_treated_as_unset(blank, monkeypatch):
    """`ORB_FHIR_EXPORT_DIR=` in a systemd unit or .env must not resolve the
    PHI export root to the process CWD."""
    monkeypatch.setenv("ORB_FHIR_EXPORT_DIR", blank)
    assert Path(str(fhir_self_export_dir())) == REPO_EXPORT_DIR


def test_env_var_whitespace_is_stripped(tmp_path, monkeypatch):
    """Unit files and .env lines carry trailing whitespace and CRLFs. Unstripped,
    `"<path>\\r"` is a DIFFERENT directory — created silently, 0o700, with a
    control character in its name, and invisible to the operator who set it."""
    target = tmp_path / "escrow"
    monkeypatch.setenv("ORB_FHIR_EXPORT_DIR", f"  {target}\r\n")
    assert Path(str(fhir_self_export_dir())) == target
    assert not list(tmp_path.glob("escrow?*")), (
        f"a whitespace-suffixed twin was created: {[p.name for p in tmp_path.iterdir()]}"
    )


# ── The half that was missing: harness isolation ─────────────────────────────

def test_ambient_export_dir_is_isolated_from_the_source_tree():
    """THE REGRESSION TEST.

    With no per-test override in force, the export directory a test session
    resolves must be a throwaway path — not the checkout. conftest.py sets
    ORB_FHIR_EXPORT_DIR at module import for the same reason it sets
    ORB_DB_PATH and ORB_WATCHDOG_DIR.
    """
    assert os.environ.get("ORB_FHIR_EXPORT_DIR"), (
        "tests/conftest.py must point ORB_FHIR_EXPORT_DIR at a throwaway "
        "directory at import time, the same way it does ORB_WATCHDOG_DIR. "
        "Without it every unoverridden export writes patient FHIR bundles "
        "into the source tree."
    )
    out = Path(str(fhir_self_export_dir()))
    assert out != REPO_EXPORT_DIR
    assert not _is_inside(out, Path(BASE_DIR)), (
        f"test-session FHIR exports resolve to {out}, inside the repo at {BASE_DIR}"
    )


def test_export_with_no_override_lands_outside_the_source_tree(admitted_patient):
    """End to end: a real export run that configures nothing writes its bundles
    to the isolated directory and leaves the checkout byte-for-byte alone.

    Ordered deliberately — the isolation guard is asserted BEFORE anything is
    written, so a failing run cannot itself deposit PHI in the source tree.
    """
    out = Path(str(fhir_self_export_dir()))
    assert not _is_inside(out, Path(BASE_DIR)), (
        f"refusing to run an export: it would write PHI into the repo at {out}"
    )

    before = (
        {p.name for p in REPO_EXPORT_DIR.glob("*")} if REPO_EXPORT_DIR.is_dir() else set()
    )
    pid = admitted_patient

    res = run_nightly_fhir_self_export("test", min_interval_hours=0)

    assert res["count"] >= 1
    assert Path(str(res["directory"])) == out
    written = {p.name for p in out.glob("*.fhir.json")}
    assert any(name.startswith(pid) for name in written), (
        f"{pid}'s bundle is missing from {out}: {sorted(written)[:5]}"
    )

    after = (
        {p.name for p in REPO_EXPORT_DIR.glob("*")} if REPO_EXPORT_DIR.is_dir() else set()
    )
    assert after == before, (
        f"export run added files to the source tree: {sorted(after - before)}"
    )


def test_source_tree_gains_no_bundles_during_the_session():
    """Canary over the window from collection to here. Cheap, and it is the
    assertion that would have caught the 390-file pile-up."""
    if not REPO_EXPORT_DIR.is_dir():
        pytest.skip("no in-repo export directory on this checkout")
    now = {p.name for p in REPO_EXPORT_DIR.glob("*.fhir.json")}
    assert now == _BUNDLES_AT_COLLECTION, (
        "patient FHIR bundles appeared in the source tree while the suite ran: "
        f"{sorted(now - _BUNDLES_AT_COLLECTION)}. Either the harness isolation "
        "above regressed, or a real server/seed script was running against this "
        "checkout at the same time — both are worth knowing about."
    )
