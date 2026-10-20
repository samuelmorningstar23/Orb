"""Downtime packs, exports and backup statements per facility (multisite-15, W12-D).

Samuel ruled on 3 October 2026 (multi-site M1) that a hospital group runs one
appliance holding several facilities with a row-level facility id, and (M2)
that isolation is keyed on data: once a second facilities row exists, each
facility's patients are kept apart. MS-R12 (a): a backup is the whole
appliance, so a restore rolls back every facility, and the screens say so.
This file pins what multisite-15 adds:

  * ward packs per facility: facility 1 keeps the export folder, any other
    facility writes to <export>/facilities/<CODE>/; a ward name two
    facilities share gives two packs; each facility's latest packs go to its
    own site.toml mirror, ORB_WARD_PACK_MIRROR_DIR is facility 1's and never
    receives another facility's patients; a mirror that is the same folder
    as, sits inside or holds another facility's mirror or folder (the shared
    mirror, the export folder, a satellite's folder) is refused for both
    facilities of the pair; rebuilding one facility's ward leaves the other's
    mirror unchanged; every pack, bundle and mirror file is scanned for the
    other facility's planted identifiers;
  * the nightly FHIR self-export writes each bundle into the patient's
    facility folder;
  * FHIR bundles name the facility: Organization with the Health Facility
    Registry id, Encounter.serviceProvider, Patient.managingOrganization;
    with one facility and ORB_MULTI_SITE off a bundle is as before;
  * the facility extract CLI (scripts/facility_extract.py): the facility's
    encounters only, the allergies, problems and current medicines whole, the
    whole history only when every encounter is at the facility; refusals,
    fail-closed audit rows, nothing left behind on failure;
  * site.toml: two facilities may not share a downtime mirror;
  * the capture-all drill is limited to the caller's facilities;
  * GET /api/admin/backups states what a restore rolls back;
  * with one facility on record every file lands where it always did.

The module owns its database (isolated_module_database), so the second
facility never reaches the session database. Test patients carry invented
names that hold no word a clinician types. Run with the test env, never the
shipping .env.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import config
import facility_fence as F
import main
from auth import create_token
from database import db_session
from scripts import facility_extract as FX
from services import downtime_service as ds
from services import facility_service as FS
from services import fhir_export_service as fx
from services import site_config_service as scs
from services.audit_service import AuditWriteError
from services.clinical_time import utc_iso_now

pytestmark = pytest.mark.usefixtures("isolated_module_database")

client = TestClient(main.app, raise_server_exceptions=False)

TAG = uuid.uuid4().hex[:6].upper()
F2 = 9511  # an id no other module writes
F2_CODE = f"XP{TAG[:4]}"
F2_HFR = f"IN27{TAG}"
WARD = f"Larkspur Ward {TAG}"  # one ward name both facilities use
F1_DEPT = f"Quenthorpe Medicine {TAG}"
F2_DEPT = f"Velmira Medicine {TAG}"

# Facility 1's inpatient: planted identifiers nothing else holds.
P1 = f"XQA{TAG}"
P1_NAME = "Quorbel Wintergast"
P1_MRN = f"XQ1MRN{TAG}"
P1_DX = "Vexmorian ague"
P1_DRUG = "Plurquinex"
P1_ALLERGY = "zandrofol"
F1_MARKS = (P1, P1_NAME, P1_MRN, P1_DX, P1_DRUG, P1_ALLERGY)

# Facility 2's inpatient, on the ward with the same name.
P2 = f"XQB{TAG}"
P2_NAME = "Yselde Brannock"
P2_MRN = f"XQ2MRN{TAG}"
P2_DX = "Corvantic gout"
P2_DRUG = "Tolvarex"
P2_ALLERGY = "kesperine"
F2_MARKS = (P2, P2_NAME, P2_MRN, P2_DX, P2_DRUG, P2_ALLERGY)

# Discharged from facility 1, then seen in facility 2's clinic.
P3 = f"XQC{TAG}"
P3_NAME = "Morwenna Quillfeather"
P3_ALLERGY = "brimtesol"
P3_CURRENT = "Halvexine"
P3_STOPPED = "Ostrapine"
P3_F1_STAY = f"XQADM3{TAG}"

ADMIN_F2 = f"XPADM{TAG}"


# ── fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module", autouse=True)
def world(isolated_module_database):
    now = utc_iso_now()
    with db_session() as db:
        db.execute(
            "INSERT INTO departments (name, facility_id, seeded_from) VALUES (?, 1, 'test')",
            (F1_DEPT,),
        )
        db.execute(
            "INSERT INTO departments (name, facility_id, seeded_from) VALUES (?, ?, 'test')",
            (F2_DEPT, F2),
        )
        db.execute(
            "INSERT INTO users (id, name, role, department, password_hash) "
            "VALUES (?, 'Velmira Administrator', 'admin', NULL, 'x')",
            (ADMIN_F2,),
        )
        db.execute(
            "INSERT INTO user_facilities (user_id, facility_id, kind, granted_by, granted_at, "
            "reason) VALUES (?, ?, 'home', 'system', CURRENT_TIMESTAMP, 'exports test')",
            (ADMIN_F2, F2),
        )
        for pid, name, mrn, dx, fid, dept, bed in (
            (P1, P1_NAME, P1_MRN, P1_DX, 1, F1_DEPT, "L-3"),
            (P2, P2_NAME, P2_MRN, P2_DX, F2, F2_DEPT, "L-4"),
        ):
            db.execute(
                "INSERT INTO patients (id, name, mrn, age, sex, ward, bed, diagnosis, status, "
                "registered_facility_id) VALUES (?, ?, ?, 58, 'F', ?, ?, ?, 'admitted', ?)",
                (pid, name, mrn, WARD, bed, dx, fid),
            )
            db.execute(
                "INSERT INTO admissions (id, patient_id, admitted_by, department, ward, bed, "
                "status, facility_id, created_at) VALUES (?, ?, 'system', ?, ?, ?, 'active', "
                "?, ?)",
                (f"XQADM{pid}", pid, dept, WARD, bed, fid, now),
            )
            db.execute(
                "INSERT INTO vitals (patient_id, timestamp, heart_rate, systolic_bp, "
                "diastolic_bp, spo2, temperature, respiratory_rate, consciousness, "
                "carried_from) VALUES (?, ?, 88, 124, 78, 97, 37.1, 16, 'alert', NULL)",
                (pid, now),
            )
        for pid, drug in ((P1, P1_DRUG), (P2, P2_DRUG)):
            db.execute(
                "INSERT INTO medications (patient_id, drug_name, dosage, route, frequency, "
                "status, source) VALUES (?, ?, '5 mg', 'oral', 'OD', 'active', 'clinician')",
                (pid, drug),
            )
        for pid, allergen in ((P1, P1_ALLERGY), (P2, P2_ALLERGY), (P3, P3_ALLERGY)):
            if pid == P3:
                db.execute(
                    "INSERT INTO patients (id, name, mrn, age, sex, ward, bed, status, "
                    "registered_facility_id) VALUES (?, ?, ?, 44, 'M', NULL, NULL, "
                    "'discharged', 1)",
                    (P3, P3_NAME, f"XQ3MRN{TAG}"),
                )
            db.execute(
                "INSERT INTO patient_allergies (patient_id, substance, reaction, severity, "
                "status, source) VALUES (?, ?, 'anaphylaxis', 'severe', 'active', 'clinician')",
                (pid, allergen),
            )
        # P3: a finished stay at facility 1 (with its vitals), then a clinic
        # visit at facility 2, which makes them facility 2's patient now.
        db.execute(
            "INSERT INTO admissions (id, patient_id, admitted_by, department, ward, bed, "
            "status, facility_id, created_at, discharged_at) VALUES (?, ?, 'system', ?, "
            "'Thornmere', 'T-1', 'discharged', 1, '2026-09-01T08:00:00+00:00', "
            "'2026-09-04T10:00:00+00:00')",
            (P3_F1_STAY, P3, F1_DEPT),
        )
        db.execute(
            "INSERT INTO vitals (patient_id, timestamp, heart_rate, systolic_bp, diastolic_bp, "
            "spo2, temperature, respiratory_rate, consciousness, carried_from) "
            "VALUES (?, '2026-09-02T06:00:00+00:00', 131, 91, 55, 88, 39.2, 29, 'alert', NULL)",
            (P3,),
        )
        db.execute(
            "INSERT INTO opd_visits (id, patient_id, department, visit_date, token_number, "
            "status, registered_at, facility_id) VALUES (?, ?, ?, ?, 1, 'completed', ?, ?)",
            (f"XQOPD{TAG}", P3, F2_DEPT, now[:10], now, F2),
        )
        for drug, status in ((P3_CURRENT, "active"), (P3_STOPPED, "discontinued")):
            db.execute(
                "INSERT INTO medications (patient_id, drug_name, dosage, route, frequency, "
                "status, source) VALUES (?, ?, '10 mg', 'oral', 'BD', ?, 'clinician')",
                (P3, drug, status),
            )
    FS.invalidate_cache()
    yield
    FS.invalidate_cache()


@pytest.fixture(autouse=True)
def _fresh(monkeypatch, tmp_path):
    """A throwaway export folder, no shared mirror, no site file, fresh caches."""
    export = tmp_path / "exports"
    export.mkdir()
    monkeypatch.setenv("ORB_FHIR_EXPORT_DIR", str(export))
    monkeypatch.delenv("ORB_WARD_PACK_MIRROR_DIR", raising=False)
    monkeypatch.setattr(scs, "_loaded", scs.SiteConfig())
    monkeypatch.setattr(config, "MULTI_SITE_ENABLED", False)
    FS.invalidate_cache()
    yield export
    FS.invalidate_cache()


@pytest.fixture()
def export_dir(_fresh):
    return _fresh


@pytest.fixture()
def two_facilities():
    with db_session() as db:
        db.execute(
            "INSERT INTO facilities (id, uuid, code, name, hfr_id, address_line1, city, state, "
            "pincode) VALUES (?, ?, ?, 'Velmira Satellite Hospital', ?, '4 Orchard Lane', "
            "'Nashik', 'Maharashtra', '422001')",
            (F2, str(uuid.uuid4()), F2_CODE, F2_HFR),
        )
    FS.invalidate_cache()
    F._audit_last.clear()
    assert FS.multi_facility_data() is True
    try:
        yield
    finally:
        with db_session() as db:
            db.execute("DELETE FROM facilities WHERE id = ?", (F2,))
        FS.invalidate_cache()
        F._audit_last.clear()


def _site_mirrors(
    monkeypatch,
    tmp_path,
    *,
    main_mirror=True,
    f2_mirror=True,
    main_folder=None,
    f2_folder=None,
) -> dict:
    """Load a real site.toml naming each facility's mirror (validated by the
    loader), as the server would at boot. main_folder and f2_folder name a
    facility's mirror instead of its default sibling folder."""
    folders = {
        "MAIN": Path(main_folder) if main_folder is not None else tmp_path / "mirror-main",
        F2_CODE: Path(f2_folder) if f2_folder is not None else tmp_path / "mirror-f2",
    }
    blocks = ['[site]\nname = "Quenthorpe Group Hospital"\n']
    for code, wanted in (("MAIN", main_mirror), (F2_CODE, f2_mirror)):
        line = f'downtime_mirror_dir = "{folders[code]}"\n' if wanted else ""
        blocks.append(f'[[facilities]]\ncode = "{code}"\n{line}')
    site = tmp_path / "site.toml"
    site.write_text("\n".join(blocks), encoding="utf-8")
    monkeypatch.setattr(scs, "_loaded", scs._loaded)  # restored after the test
    scs.load_site_config(site, apply_env=False)
    return folders


def _files_under(*folders: Path, recursive: bool = True) -> list[Path]:
    out: list[Path] = []
    for folder in folders:
        if not folder.exists():
            continue
        if recursive:
            out.extend(p for p in folder.rglob("*") if p.is_file())
        else:
            out.extend(p for p in folder.iterdir() if p.is_file())
    return out


def _leaks(files: list[Path], marks: tuple) -> list[tuple[str, str]]:
    found = []
    for path in files:
        text = path.read_text(encoding="utf-8", errors="replace")
        for mark in marks:
            if mark in text or mark in path.name:
                found.append((str(path), mark))
    return found


def _resources(bundle: dict, kind: str) -> list[dict]:
    return [e["resource"] for e in bundle["entry"] if e["resource"]["resourceType"] == kind]


def _f2_folder(export: Path) -> Path:
    return export / ds.FACILITY_FOLDER / F2_CODE


# ── one facility on record: exactly as before ────────────────────────────────


def test_with_one_facility_packs_and_bundles_land_where_they_always_did(
    export_dir, monkeypatch, tmp_path
):
    shared = tmp_path / "shared-mirror"
    monkeypatch.setenv("ORB_WARD_PACK_MIRROR_DIR", str(shared))
    assert FS.multi_facility_data() is False

    rows, _ = ds._admitted_for_packs(WARD)
    assert {r["id"] for r in rows} == {P1, P2}
    assert all("facility_id" not in r for r in rows)  # rows unchanged

    result = ds.build_ward_print_packs()
    assert "facilities" not in result
    assert not (export_dir / ds.FACILITY_FOLDER).exists()
    latest = Path(ds._ward_pack_path(export_dir, WARD, "latest"))
    doc = latest.read_text(encoding="utf-8")
    # One facility: one ward, one pack, both patients, as before.
    assert P1_MRN in doc and P2_MRN in doc
    assert str(Path(ds._ward_pack_path(shared, WARD, "latest"))) in result["mirrored"]

    exported = ds.run_nightly_fhir_self_export(
        min_interval_hours=0, chunk_bytes=0, chunk_sleep_s=0, patient_sleep_s=0
    )
    assert "facilities" not in exported
    assert Path(ds._bundle_path(export_dir, P2)).is_file()
    assert ds.facility_export_dir(F2) == str(export_dir)


def test_with_one_facility_a_bundle_names_no_facility():
    bundle = fx.patient_bundle(P2)
    kinds = {e["resource"]["resourceType"] for e in bundle["entry"]}
    assert "Organization" not in kinds and "Encounter" not in kinds
    assert "managingOrganization" not in _resources(bundle, "Patient")[0]


def test_the_multi_site_switch_alone_names_facility_one(monkeypatch):
    monkeypatch.setattr(config, "MULTI_SITE_ENABLED", True)
    bundle = fx.patient_bundle(P1)
    orgs = _resources(bundle, "Organization")
    assert [o["id"] for o in orgs] == ["orb-facility-1"]
    assert orgs[0]["identifier"][0] == {
        "use": "usual",
        "system": "https://orb.health/identifier/facility",
        "value": "MAIN",
    }
    enc = _resources(bundle, "Encounter")
    assert [e["serviceProvider"]["reference"] for e in enc] == ["Organization/orb-facility-1"]


def test_with_one_facility_a_site_mirror_for_main_gets_the_packs(export_dir, monkeypatch, tmp_path):
    folders = _site_mirrors(monkeypatch, tmp_path, f2_mirror=False)
    result = ds.build_ward_print_packs()
    copy = Path(ds._ward_pack_path(folders["MAIN"], WARD, "latest"))
    assert str(copy) in result["mirrored"]
    assert (copy.stat().st_mode & 0o777) == 0o600


# ── ward packs per facility ──────────────────────────────────────────────────


def test_an_f2_pack_holds_only_f2_patients_and_lands_in_the_f2_mirror(
    export_dir, two_facilities, monkeypatch, tmp_path
):
    shared = tmp_path / "shared-mirror"
    monkeypatch.setenv("ORB_WARD_PACK_MIRROR_DIR", str(shared))
    folders = _site_mirrors(monkeypatch, tmp_path)

    result = ds.build_ward_print_packs()

    f2_folder = _f2_folder(export_dir)
    assert result["directory"] == str(export_dir)
    by_facility = {f["facility_id"]: f for f in result["facilities"]}
    assert by_facility[F2]["directory"] == str(f2_folder)
    assert by_facility[F2]["mirror_directories"] == [str(folders[F2_CODE])]
    assert by_facility[1]["mirror_directories"] == [str(shared), str(folders["MAIN"])]
    assert by_facility[F2]["patients"] == 1 and by_facility[1]["patients"] == 1
    assert not by_facility[F2]["mirror_missing"]
    assert (os.stat(f2_folder).st_mode & 0o777) == 0o700

    # One ward name, two packs: one per facility folder.
    f1_doc = Path(ds._ward_pack_path(export_dir, WARD, "latest")).read_text(encoding="utf-8")
    f2_doc = Path(ds._ward_pack_path(f2_folder, WARD, "latest")).read_text(encoding="utf-8")
    assert P1_MRN in f1_doc and P2_MRN not in f1_doc
    assert P2_MRN in f2_doc and P1_MRN not in f2_doc
    assert "1 admitted patient(s)" in f2_doc

    # Every file a facility 2 reader can open holds nothing of facility 1's...
    f2_files = _files_under(f2_folder, folders[F2_CODE])
    assert f2_files
    assert _leaks(f2_files, F1_MARKS) == []
    # ...and facility 1's folder (its own files) and mirrors nothing of 2's.
    f1_files = _files_under(export_dir, recursive=False) + _files_under(shared, folders["MAIN"])
    assert f1_files
    assert _leaks(f1_files, F2_MARKS) == []
    # The facility 2 mirror holds only the latest pack.
    assert [p.name for p in _files_under(folders[F2_CODE])] == [
        Path(ds._ward_pack_path(folders[F2_CODE], WARD, "latest")).name
    ]


def test_rebuilding_an_f2_ward_leaves_the_f1_mirror_unchanged(
    export_dir, two_facilities, monkeypatch, tmp_path
):
    shared = tmp_path / "shared-mirror"
    monkeypatch.setenv("ORB_WARD_PACK_MIRROR_DIR", str(shared))
    folders = _site_mirrors(monkeypatch, tmp_path)
    ds.build_ward_print_packs()

    def state(*dirs: Path) -> dict:
        return {str(p): (p.stat().st_mtime_ns, p.read_bytes()) for p in _files_under(*dirs)}

    f1_before = state(shared, folders["MAIN"])
    f1_root_before = {str(p): p.read_bytes() for p in _files_under(export_dir, recursive=False)}
    f2_copy = Path(ds._ward_pack_path(folders[F2_CODE], WARD, "latest"))
    old = f2_copy.stat().st_mtime - 600
    os.utime(f2_copy, (old, old))

    result = ds.rebuild_ward_pack(WARD, facility_id=F2, refresh_older_than_s=0)

    assert result is not None and result["patients"] == 1
    assert result["mirrored"] == [str(f2_copy)]
    assert f2_copy.stat().st_mtime > old
    assert state(shared, folders["MAIN"]) == f1_before
    assert {
        str(p): p.read_bytes() for p in _files_under(export_dir, recursive=False)
    } == f1_root_before


def test_a_satellite_with_no_mirror_of_its_own_gets_none_from_the_shared_one(
    export_dir, two_facilities, monkeypatch, tmp_path
):
    shared = tmp_path / "shared-mirror"
    monkeypatch.setenv("ORB_WARD_PACK_MIRROR_DIR", str(shared))
    _site_mirrors(monkeypatch, tmp_path, main_mirror=False, f2_mirror=False)

    result = ds.build_ward_print_packs()

    by_facility = {f["facility_id"]: f for f in result["facilities"]}
    assert by_facility[F2]["mirror_directories"] == []
    assert by_facility[F2]["mirror_missing"] is True
    assert by_facility[1]["mirror_directories"] == [str(shared)]
    shared_files = _files_under(shared)
    assert [p.name for p in shared_files] == [Path(ds._ward_pack_path(shared, WARD, "latest")).name]
    assert _leaks(shared_files, F2_MARKS) == []
    # Facility 2's pack is still written on the appliance, in its folder.
    assert Path(ds._ward_pack_path(_f2_folder(export_dir), WARD, "latest")).is_file()


def _f1_state(export: Path, *mirrors: Path) -> dict:
    """Every facility 1 file a reader can open, with its bytes and mtime:
    the files at the top of the export folder and everything under its
    mirrors."""
    files = _files_under(export, recursive=False) + _files_under(*mirrors)
    return {str(p): (p.stat().st_mtime_ns, p.read_bytes()) for p in files}


@pytest.mark.parametrize("layout", ["same", "nested", "export_root"])
def test_a_satellite_mirror_that_overlaps_facility_one_s_folders_is_not_used(
    export_dir, two_facilities, monkeypatch, tmp_path, caplog, layout
):
    """Facility 2's site.toml mirror is the shared mirror itself, a folder
    inside it (the natural layout of one share), or the export folder.
    site.toml loads either way, so the build must refuse it: facility 2's
    patients never reach facility 1's mirror or folder, and facility 1's
    copy of the ward name both facilities use stays facility 1's."""
    shared = tmp_path / "WardPacks"
    monkeypatch.setenv("ORB_WARD_PACK_MIRROR_DIR", str(shared))
    f2_mirror = {"same": shared, "nested": shared / "SAT1", "export_root": export_dir}[layout]
    _site_mirrors(monkeypatch, tmp_path, main_mirror=False, f2_folder=f2_mirror)

    with caplog.at_level(logging.WARNING, logger="orb.downtime"):
        result = ds.build_ward_print_packs()

    by_facility = {f["facility_id"]: f for f in result["facilities"]}
    assert by_facility[F2]["mirror_directories"] == []
    assert by_facility[F2]["mirror_missing"] is True
    assert [r["directory"] for r in by_facility[F2]["mirrors_refused"]] == [str(f2_mirror)]
    assert any(
        "is not used" in r.getMessage() and F2_CODE in r.getMessage() for r in caplog.records
    )
    if layout == "export_root":
        # The shared mirror is a folder of its own, so facility 1 keeps it.
        assert by_facility[1]["mirror_directories"] == [str(shared)]
        assert by_facility[1]["mirrors_refused"] == []
    else:
        # Checked from both sides: the shared mirror holds (or is) facility
        # 2's, so facility 1 does not use it either, and nothing lands there.
        assert by_facility[1]["mirror_directories"] == []
        assert by_facility[1]["mirror_missing"] is True
        assert by_facility[1]["mirrors_refused"] == [
            {"directory": str(shared), "overlaps": f"the mirror of {F2_CODE}"}
        ]
        assert _files_under(shared) == []
    assert not any(p.startswith(str(f2_mirror)) for p in result["mirrored"])

    # Not one of facility 2's planted identifiers is in a file facility 1's
    # readers open, and facility 1's latest pack of the shared ward name is
    # still facility 1's.
    f1_files = _files_under(export_dir, recursive=False) + _files_under(shared)
    assert f1_files
    assert _leaks(f1_files, F2_MARKS) == []
    f1_doc = Path(ds._ward_pack_path(export_dir, WARD, "latest")).read_text(encoding="utf-8")
    assert P1_MRN in f1_doc and P2_MRN not in f1_doc
    # Facility 2's pack is still written on the appliance, in its own folder.
    f2_doc = Path(ds._ward_pack_path(_f2_folder(export_dir), WARD, "latest"))
    assert P2_MRN in f2_doc.read_text(encoding="utf-8")

    # Rebuilding facility 2's ward on request copies it nowhere and leaves
    # every facility 1 file as it was.
    before = _f1_state(export_dir, shared)
    rebuilt = ds.rebuild_ward_pack(WARD, facility_id=F2, refresh_older_than_s=0)
    assert rebuilt is not None and rebuilt["patients"] == 1
    assert rebuilt["mirrored"] == []
    assert _f1_state(export_dir, shared) == before


@pytest.mark.parametrize(
    "layout", ["export_root", "inside_satellite_folder", "site_main_inside_satellite_folder"]
)
def test_facility_one_s_mirror_that_overlaps_a_satellite_folder_is_not_used(
    export_dir, two_facilities, monkeypatch, tmp_path, layout
):
    """The other way round: facility 1's mirror (the shared one, or its own
    site.toml folder) that is the export folder, which holds the satellites'
    folders, or sits inside facility 2's folder would put facility 1's
    patients where facility 2's are kept. It is refused; facility 2 keeps its
    own mirror."""
    f2_appliance = _f2_folder(export_dir)
    env_mirror = {
        "export_root": export_dir,
        "inside_satellite_folder": f2_appliance / "main",
        "site_main_inside_satellite_folder": None,
    }[layout]
    if env_mirror is not None:
        monkeypatch.setenv("ORB_WARD_PACK_MIRROR_DIR", str(env_mirror))
    main_folder = f2_appliance / "site-main"
    folders = _site_mirrors(
        monkeypatch,
        tmp_path,
        main_mirror=layout == "site_main_inside_satellite_folder",
        main_folder=main_folder,
    )
    refused = env_mirror if env_mirror is not None else main_folder

    result = ds.build_ward_print_packs()

    by_facility = {f["facility_id"]: f for f in result["facilities"]}
    assert by_facility[1]["mirror_directories"] == []
    assert by_facility[1]["mirror_missing"] is True
    assert by_facility[1]["mirrors_refused"] == [
        {"directory": str(refused), "overlaps": "the satellites' folders"}
    ]
    assert by_facility[F2]["mirror_directories"] == [str(folders[F2_CODE])]
    assert result["mirrored"] == [str(Path(ds._ward_pack_path(folders[F2_CODE], WARD, "latest")))]
    f2_files = _files_under(f2_appliance, folders[F2_CODE])
    assert f2_files
    assert _leaks(f2_files, F1_MARKS) == []


def test_a_mirror_inside_another_facility_s_mirror_is_refused_for_both(
    export_dir, two_facilities, monkeypatch, tmp_path
):
    """Facility 1's shared mirror inside facility 2's site.toml mirror: whoever
    reads facility 2's share would read facility 1's packs, so neither is used."""
    f2_mirror = tmp_path / "mirror-f2"
    monkeypatch.setenv("ORB_WARD_PACK_MIRROR_DIR", str(f2_mirror / "MAIN"))
    _site_mirrors(monkeypatch, tmp_path, main_mirror=False, f2_folder=f2_mirror)

    result = ds.build_ward_print_packs()

    by_facility = {f["facility_id"]: f for f in result["facilities"]}
    assert by_facility[1]["mirror_directories"] == []
    assert by_facility[F2]["mirror_directories"] == []
    assert by_facility[F2]["mirrors_refused"] == [
        {"directory": str(f2_mirror), "overlaps": "the mirror of facility 1"}
    ]
    assert result["mirrored"] == []
    assert _files_under(f2_mirror) == []


def test_with_one_facility_no_mirror_is_refused(export_dir, monkeypatch, tmp_path):
    """One facility on record: the overlap rule reads nothing new, and a
    mirror set to the export folder is skipped by the copy as it always was."""
    monkeypatch.setenv("ORB_WARD_PACK_MIRROR_DIR", str(export_dir))
    assert ds._mirror_plan(1) == ([str(export_dir)], [])
    assert ds.ward_pack_mirror_dirs(1) == [str(export_dir)]
    result = ds.build_ward_print_packs()
    assert "facilities" not in result
    assert result["mirrored"] == []
    assert Path(ds._ward_pack_path(export_dir, WARD, "latest")).is_file()


def test_folders_overlap_when_they_are_the_same_or_one_holds_the_other(tmp_path):
    packs = tmp_path / "WardPacks"
    assert ds._folders_overlap(packs, packs)
    assert ds._folders_overlap(packs, f"{packs}/")
    assert ds._folders_overlap(packs, tmp_path / "wardpacks")  # the share may ignore case
    assert ds._folders_overlap(packs, packs / "SAT1")
    assert ds._folders_overlap(packs / "SAT1", packs)
    assert ds._folders_overlap("/", packs)
    assert not ds._folders_overlap(packs, tmp_path / "WardPacks2")
    assert not ds._folders_overlap(packs / "MAIN", packs / "SAT1")
    packs.mkdir()
    link = tmp_path / "packs-link"
    link.symlink_to(packs, target_is_directory=True)
    assert ds._folders_overlap(link / "SAT1", packs)


def test_the_packs_served_for_a_patient_are_their_facility_s_ward(
    export_dir, two_facilities, monkeypatch
):
    served = ds.serve_ward_pack(P2, max_age_s=0)
    assert served["ward"] == WARD and served["rebuilt"] is True
    assert P2_MRN in served["html"] and P1_MRN not in served["html"]
    assert Path(ds._ward_pack_path(_f2_folder(export_dir), WARD, "latest")).is_file()
    assert not Path(ds._ward_pack_path(export_dir, WARD, "latest")).exists()
    ward, doc = ds.read_latest_ward_pack(P2)
    assert ward == WARD and doc is not None and P1_MRN not in doc

    # The signed-in lane: even an account the scope check lets see both
    # facilities gets only the anchor patient's facility's ward.
    import permissions

    monkeypatch.setattr(permissions, "enforce_patient_scope", lambda user, pid: None)
    signed_in = ds.serve_ward_pack(P1, user={"id": ADMIN_F2, "role": "admin"})
    assert P1_MRN in signed_in["html"] and P2_MRN not in signed_in["html"]


def test_facility_folders_and_mirrors_age_out_like_the_root(
    export_dir, two_facilities, monkeypatch, tmp_path
):
    folders = _site_mirrors(monkeypatch, tmp_path)
    f2_folder = Path(ds.facility_export_dir(F2))
    for folder in (f2_folder, folders[F2_CODE]):
        folder.mkdir(parents=True, exist_ok=True)
        stale = folder / "ward-pack.Gone_Ward.0000aaaa.latest.html"
        stale.write_text("old census", encoding="utf-8")
        old = stale.stat().st_mtime - 10 * 86400
        os.utime(stale, (old, old))

    ds.build_ward_print_packs(retention_days=3)

    assert not (f2_folder / "ward-pack.Gone_Ward.0000aaaa.latest.html").exists()
    assert not (folders[F2_CODE] / "ward-pack.Gone_Ward.0000aaaa.latest.html").exists()


# ── the nightly FHIR self-export ─────────────────────────────────────────────


def test_the_nightly_export_writes_each_bundle_into_its_facility_folder(export_dir, two_facilities):
    result = ds.run_nightly_fhir_self_export(
        min_interval_hours=0, chunk_bytes=0, chunk_sleep_s=0, patient_sleep_s=0
    )
    f2_folder = _f2_folder(export_dir)
    assert result["count"] == 2
    assert {f["facility_id"]: f["patients"] for f in result["facilities"]} == {1: 1, F2: 1}
    assert Path(ds._bundle_path(export_dir, P1)).is_file()
    assert not Path(ds._bundle_path(export_dir, P2)).exists()
    f2_bundle = Path(ds._bundle_path(f2_folder, P2))
    assert (f2_bundle.stat().st_mode & 0o777) == 0o600
    assert _leaks(_files_under(f2_folder), F1_MARKS) == []
    assert _leaks(_files_under(export_dir, recursive=False), F2_MARKS) == []

    bundle = json.loads(f2_bundle.read_text(encoding="utf-8"))
    org = _resources(bundle, "Organization")[0]
    assert {"system": "https://facility.ndhm.gov.in", "value": F2_HFR} == {
        k: org["identifier"][1][k] for k in ("system", "value")
    }
    assert _resources(bundle, "Encounter")[0]["serviceProvider"]["reference"] == (
        f"Organization/orb-facility-{F2}"
    )


# ── FHIR names the facility ──────────────────────────────────────────────────


def test_the_bundle_carries_the_organization_with_its_hfr_id(two_facilities):
    bundle = fx.patient_bundle(P2)
    patient = _resources(bundle, "Patient")[0]
    assert patient["managingOrganization"] == {"reference": f"Organization/orb-facility-{F2}"}

    (org,) = _resources(bundle, "Organization")
    assert org["id"] == f"orb-facility-{F2}"
    assert org["name"] == "Velmira Satellite Hospital"
    hfr = next(i for i in org["identifier"] if i["system"] == "https://facility.ndhm.gov.in")
    assert hfr["value"] == F2_HFR
    assert hfr["type"]["coding"][0]["code"] == "PRN"
    assert org["address"][0]["postalCode"] == "422001"
    assert org["address"][0]["country"] == "IN"
    assert any(t["code"] == "orb" for t in org["meta"]["tag"])

    (enc,) = _resources(bundle, "Encounter")
    assert enc["class"]["code"] == "IMP"
    assert enc["status"] == "in-progress"
    assert enc["serviceProvider"] == {
        "reference": f"Organization/orb-facility-{F2}",
        "display": "Velmira Satellite Hospital",
    }
    assert enc["subject"] == {"reference": f"Patient/{P2}"}
    assert enc["serviceType"] == {"text": F2_DEPT}


def test_the_whole_chart_names_every_facility_it_touches(two_facilities):
    """MS-R2 (a): a patient's stay at another facility of the group is still
    theirs, so the bundle carries it with that facility's Organization."""
    bundle = fx.patient_bundle(P3)
    encounters = {e["id"]: e for e in _resources(bundle, "Encounter")}
    assert {e["class"]["code"] for e in encounters.values()} == {"IMP", "AMB"}
    providers = {e["serviceProvider"]["reference"] for e in encounters.values()}
    assert providers == {"Organization/orb-facility-1", f"Organization/orb-facility-{F2}"}
    assert {o["id"] for o in _resources(bundle, "Organization")} == {
        "orb-facility-1",
        f"orb-facility-{F2}",
    }
    # The clinic visit is the latest encounter, so facility 2 manages them now.
    assert _resources(bundle, "Patient")[0]["managingOrganization"]["reference"] == (
        f"Organization/orb-facility-{F2}"
    )


# ── the facility extract ─────────────────────────────────────────────────────


def _extract(tmp_path, code=F2_CODE, reason="Velmira moves to its own appliance") -> dict:
    return FX.write_extract(code, tmp_path / "extract", reason)


def _audit_rows(action: str) -> list[dict]:
    with db_session() as db:
        rows = db.execute(
            "SELECT patient_id, actor_role, source_type, metadata_json, event_category "
            "FROM audit_logs WHERE action_type = ? ORDER BY id",
            (action,),
        ).fetchall()
    return [dict(r) for r in rows]


def test_the_extract_holds_f2_encounters_and_the_group_level_allergies(two_facilities, tmp_path):
    before = len(_audit_rows("facility_extract_written"))
    result = _extract(tmp_path)

    out = Path(result["directory"])
    assert (os.stat(out).st_mode & 0o777) == 0o700
    manifest = json.loads((out / FX.MANIFEST).read_text(encoding="utf-8"))
    assert manifest["facility"]["code"] == F2_CODE
    assert manifest["facility"]["hfr_id"] == F2_HFR
    history = {f["patient_id"]: f["history"] for f in manifest["files"]}
    assert history == {P2: "full", P3: "group_level_only"}  # never the F1-only patient
    assert result["patients"] == 2 and result["group_level_only"] == 1

    bundles = {
        f["patient_id"]: json.loads((out / f["file"]).read_text(encoding="utf-8"))
        for f in manifest["files"]
    }
    for f in manifest["files"]:
        assert (os.stat(out / f["file"]).st_mode & 0o777) == 0o600

    full = bundles[P2]
    assert [e["serviceProvider"]["reference"] for e in _resources(full, "Encounter")] == [
        f"Organization/orb-facility-{F2}"
    ]
    assert [a["code"]["text"] for a in _resources(full, "AllergyIntolerance")] == [P2_ALLERGY]
    assert _resources(full, "Observation")  # the whole history is this facility's
    assert [o["id"] for o in _resources(full, "Organization")] == [f"orb-facility-{F2}"]

    mixed = bundles[P3]
    tags = {t["code"] for t in mixed["meta"]["tag"]}
    assert {"facility-extract", "group-level-only"} <= tags
    encounters = _resources(mixed, "Encounter")
    assert [e["class"]["code"] for e in encounters] == ["AMB"]  # not the facility 1 stay
    assert all(P3_F1_STAY not in json.dumps(e) for e in encounters)
    # The allergy recorded during the facility 1 stay travels with the patient.
    assert [a["code"]["text"] for a in _resources(mixed, "AllergyIntolerance")] == [P3_ALLERGY]
    # Current medicines only; no observation history that facility 1 wrote.
    meds = [m["medicationCodeableConcept"]["text"] for m in _resources(mixed, "MedicationRequest")]
    assert meds == [P3_CURRENT]
    assert _resources(mixed, "Observation") == []

    assert _leaks(_files_under(out), F1_MARKS) == []

    written = _audit_rows("facility_extract_written")
    assert len(written) == before + 1
    meta = json.loads(written[-1]["metadata_json"])
    assert written[-1]["event_category"] == "phi_access"
    assert written[-1]["actor_role"] == "console" and written[-1]["source_type"] == "console"
    assert meta["facility_id"] == F2 and meta["patients"] == 2 and meta["group_level_only"] == 1
    included = {r["patient_id"] for r in _audit_rows("facility_extract_patient_included")}
    assert {P2, P3} <= included and P1 not in included


def test_the_extract_of_facility_one_leaves_out_the_satellite_s_patient(two_facilities, tmp_path):
    result = _extract(tmp_path, code="main")
    manifest = json.loads(Path(result["manifest"]).read_text(encoding="utf-8"))
    history = {f["patient_id"]: f["history"] for f in manifest["files"]}
    assert history[P1] == "full"
    assert history[P3] == "group_level_only"  # stayed at 1, now seen at 2
    assert P2 not in history
    assert _leaks(_files_under(Path(result["directory"])), F2_MARKS) == []


def test_the_extract_refuses_and_leaves_nothing(two_facilities, tmp_path):
    with pytest.raises(FX.ExtractRefused, match="no facility"):
        FX.write_extract("NOPE99", tmp_path / "a", "a reason")
    with pytest.raises(FX.ExtractRefused, match="reason"):
        FX.write_extract(F2_CODE, tmp_path / "b", " ")
    busy = tmp_path / "busy"
    busy.mkdir()
    (busy / "keep.txt").write_text("someone's file", encoding="utf-8")
    with pytest.raises(FX.ExtractRefused, match="never writes over"):
        FX.write_extract(F2_CODE, busy, "a reason")
    assert [p.name for p in busy.iterdir()] == ["keep.txt"]
    assert not (tmp_path / "a").exists() and not (tmp_path / "b").exists()


def test_a_dry_run_counts_and_writes_nothing(two_facilities, tmp_path):
    before = len(_audit_rows("facility_extract_written"))
    result = FX.write_extract(F2_CODE, tmp_path / "dry", None, dry_run=True)
    assert result["dry_run"] is True and result["patients"] == 2
    assert not (tmp_path / "dry").exists()
    assert len(_audit_rows("facility_extract_written")) == before


def test_an_unwritable_audit_trail_leaves_no_extract(two_facilities, tmp_path, monkeypatch):
    from services import audit_service

    def refuse(*_a, **_k):
        raise AuditWriteError("audit store unavailable")

    monkeypatch.setattr(audit_service, "log_audit_event", refuse)
    exit_folder = tmp_path / "exit"
    exit_folder.mkdir()
    with pytest.raises(AuditWriteError):
        FX.write_extract(F2_CODE, exit_folder / "extract", "Velmira moves to its own appliance")
    assert list(exit_folder.iterdir()) == []  # no extract, no hidden staging folder


def test_the_extract_command_line(two_facilities, tmp_path, capsys):
    assert FX.main(["--facility", F2_CODE, "--dry-run", "--json"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["patients"] == 2 and printed["dry_run"] is True
    assert FX.main(["--facility", "NOPE99", "--dry-run"]) == 1
    assert "no facility" in capsys.readouterr().err
    assert FX.main(["--unknown-flag"]) == 2
    out = tmp_path / "cli"
    assert FX.main(["--facility", F2_CODE, "--out", str(out), "--reason", "Leaving"]) == 0
    assert (out / FX.MANIFEST).is_file()


# ── site.toml mirrors ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "second",
    ["/srv/packs/main", "/srv/packs/MAIN/", "/srv/packs/main/sat1", "/srv/packs"],
)
def test_two_facilities_may_not_share_a_mirror(tmp_path, second):
    site = tmp_path / "site.toml"
    site.write_text(
        '[site]\nname = "Quenthorpe Group Hospital"\n\n'
        '[[facilities]]\ncode = "MAIN"\ndowntime_mirror_dir = "/srv/packs/main"\n\n'
        f'[[facilities]]\ncode = "SAT1"\ndowntime_mirror_dir = "{second}"\n',
        encoding="utf-8",
    )
    with pytest.raises(scs.SiteConfigError, match="overlaps the mirror of facility 'MAIN'"):
        scs.load_site_config(site, apply_env=False)


def test_separate_mirrors_are_read_by_facility_code(tmp_path, monkeypatch):
    folders = _site_mirrors(monkeypatch, tmp_path)
    assert scs.facility_downtime_mirror("main") == str(folders["MAIN"])
    assert scs.facility_downtime_mirror(F2_CODE.lower()) == str(folders[F2_CODE])
    assert scs.facility_downtime_mirror("NOPE99") is None
    assert scs.site_config_snapshot()["facilities"][1]["downtime_mirror_set"] is True


# ── the capture-all drill ────────────────────────────────────────────────────


def test_the_capture_all_drill_keeps_to_the_callers_facilities(two_facilities):
    assert ds.capture_all_admitted(facilities=frozenset({F2}))["total_admitted"] == 1
    assert ds.capture_all_admitted(facilities=frozenset())["total_admitted"] == 0
    assert ds.capture_all_admitted()["total_admitted"] == 2


def test_with_one_facility_the_drill_ignores_the_facility_limit():
    assert ds.capture_all_admitted(facilities=frozenset({F2}))["total_admitted"] == 2


def test_the_capture_all_route_passes_the_callers_facility_scope(two_facilities, monkeypatch):
    monkeypatch.setattr(F, "FACILITY_BLIND_SURFACES", ())  # the fence opened, as once swept
    headers = {"Authorization": f"Bearer {create_token(ADMIN_F2, 'admin')}"}
    resp = client.post("/api/downtime/capture-all", headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["total_admitted"] == 1
    assert P1 not in resp.text


def test_the_capture_all_route_is_fenced_while_two_facilities_exist(two_facilities):
    headers = {"Authorization": f"Bearer {create_token(ADMIN_F2, 'admin')}"}
    assert client.post("/api/downtime/capture-all", headers=headers).status_code == 503


# ── what a restore rolls back ────────────────────────────────────────────────


def test_the_backups_page_states_a_restore_rolls_back_the_whole_appliance(monkeypatch, tmp_path):
    monkeypatch.setenv("ORB_BACKUP_DIR", str(tmp_path / "backups"))
    headers = {"Authorization": f"Bearer {create_token(ADMIN_F2, 'admin')}"}
    resp = client.get("/api/admin/backups", headers=headers)
    assert resp.status_code == 200, resp.text
    scope = resp.json()["restore_scope"]
    assert scope["whole_appliance"] is True and scope["facilities_on_record"] == 1
    assert "facilities" not in scope
    assert "whole database" in scope["statement"]


def test_with_two_facilities_the_statement_names_every_facility(
    two_facilities, monkeypatch, tmp_path
):
    monkeypatch.setattr(F, "FACILITY_BLIND_SURFACES", ())
    monkeypatch.setenv("ORB_BACKUP_DIR", str(tmp_path / "backups"))
    headers = {"Authorization": f"Bearer {create_token(ADMIN_F2, 'admin')}"}
    scope = client.get("/api/admin/backups", headers=headers).json()["restore_scope"]
    assert scope["facilities_on_record"] == 2
    assert [f["code"] for f in scope["facilities"]] == ["MAIN", F2_CODE]
    assert "2 facilities" in scope["statement"]
    assert "One facility cannot be restored on its own." in scope["statement"]
    assert chr(0x2014) not in scope["statement"]  # no long dash in a sentence staff read
