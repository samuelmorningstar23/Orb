"""Per-file migration discovery (his-1, wave 1, 3 October 2026).

From migration 48 on, each migration is one file in server/migrations_his/,
collected by version and merged into migrations.MIGRATIONS. These tests prove
the collector finds them, merges them in order, and REFUSES every way a file
could otherwise be skipped or doubled without a word: a misnamed file, a
MIGRATION that disagrees with its file name, a missing MIGRATION, version 0,
and a version defined twice (in the directory, or against migrations 1 to 47).
"""
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

import migrations
import migrations_his
from migrations_his import MigrationDiscoveryError, collect_from, merge

SERVER_DIR = Path(__file__).resolve().parents[1]


def test_the_package_holds_48_and_49():
    # A superset on purpose: later waves add their own files here (50 to 75 are
    # allocated in docs/HIS_BUILD_PLAN_2026-10.md) without editing this test.
    found = {m.version: m.name for m in migrations_his.collect()}
    assert found[48] == "users_role_batch"
    assert found[49] == "helix_tables_in_runner"
    assert min(found) >= 48, "migrations 1 to 47 stay in migrations.py"


def test_the_runner_list_is_sorted_unique_and_includes_the_files():
    versions = [m.version for m in migrations.MIGRATIONS]
    assert versions == sorted(versions)
    assert len(versions) == len(set(versions))
    assert {48, 49} <= set(versions)
    assert set(range(1, 48)) <= set(versions), "migrations 1 to 47 still run"
    by_version = {m.version: m for m in migrations.MIGRATIONS}
    assert by_version[48].name == "users_role_batch"
    assert by_version[49].name == "helix_tables_in_runner"


def test_every_file_in_the_package_is_a_collected_migration():
    """Nothing in the directory can sit there unrun."""
    names = sorted(p.name for p in (SERVER_DIR / "migrations_his").glob("*.py")
                   if p.name != "__init__.py")
    assert names == [f"m{m.version:04d}_{m.name}.py" for m in migrations_his.collect()]


def _write(directory: Path, name: str, body: str) -> None:
    (directory / name).write_text(textwrap.dedent(body))


_GOOD = """
    from migrations import Migration

    def apply(conn):
        pass

    MIGRATION = Migration({v}, "{n}", apply)
"""


def test_a_clean_directory_collects_in_version_order(tmp_path):
    _write(tmp_path, "m0091_second.py", _GOOD.format(v=91, n="second"))
    _write(tmp_path, "m0090_first.py", _GOOD.format(v=90, n="first"))
    (tmp_path / "notes.txt").write_text("not python, ignored")
    (tmp_path / "__pycache__").mkdir()
    got = collect_from(tmp_path)
    assert [(m.version, m.name) for m in got] == [(90, "first"), (91, "second")]


@pytest.mark.parametrize("filename", [
    "m90_short.py",            # three digits short
    "0090_noprefix.py",
    "m0090-dash.py",
    "m0090_CamelCase.py",
    "helpers.py",              # a helper module would never run as a migration
])
def test_a_misnamed_file_is_refused(tmp_path, filename):
    _write(tmp_path, filename, _GOOD.format(v=90, n="x"))
    with pytest.raises(MigrationDiscoveryError, match="not named"):
        collect_from(tmp_path)


def test_a_version_that_disagrees_with_the_file_name_is_refused(tmp_path):
    _write(tmp_path, "m0090_alpha.py", _GOOD.format(v=91, n="alpha"))
    with pytest.raises(MigrationDiscoveryError, match="must agree"):
        collect_from(tmp_path)


def test_a_name_that_disagrees_with_the_file_name_is_refused(tmp_path):
    _write(tmp_path, "m0090_alpha.py", _GOOD.format(v=90, n="beta"))
    with pytest.raises(MigrationDiscoveryError, match="must agree"):
        collect_from(tmp_path)


def test_a_file_without_a_migration_is_refused(tmp_path):
    _write(tmp_path, "m0090_empty.py", "X = 1\n")
    with pytest.raises(MigrationDiscoveryError, match="defines no MIGRATION"):
        collect_from(tmp_path)


def test_version_zero_is_refused(tmp_path):
    _write(tmp_path, "m0000_zero.py", _GOOD.format(v=0, n="zero"))
    with pytest.raises(MigrationDiscoveryError, match="start at 1"):
        collect_from(tmp_path)


def test_two_files_with_one_version_are_refused(tmp_path):
    _write(tmp_path, "m0090_alpha.py", _GOOD.format(v=90, n="alpha"))
    _write(tmp_path, "m0090_beta.py", _GOOD.format(v=90, n="beta"))
    with pytest.raises(MigrationDiscoveryError, match="defined twice"):
        collect_from(tmp_path)


def test_a_file_that_takes_a_core_version_is_refused(tmp_path):
    _write(tmp_path, "m0047_clash.py", _GOOD.format(v=47, n="clash"))
    core = [m for m in migrations.MIGRATIONS if m.version <= 47]
    with pytest.raises(MigrationDiscoveryError, match="defined twice"):
        merge(core, collect_from(tmp_path))


def test_merge_keeps_the_runner_order(tmp_path):
    _write(tmp_path, "m0090_late.py", _GOOD.format(v=90, n="late"))
    core = [migrations.Migration(5, "five", lambda c: None),
            migrations.Migration(1, "one", lambda c: None)]
    merged = merge(core, collect_from(tmp_path))
    assert [m.version for m in merged] == [1, 5, 90]


@pytest.mark.parametrize("first", ["migrations", "migrations_his"])
def test_either_import_order_works(first):
    """migrations imports the package at its bottom; the package imports
    migrations lazily. A script that imports the package first must not break
    on the cycle."""
    body = (f"import {first}\n"
            "import migrations, migrations_his\n"
            "vs = {m.version for m in migrations.MIGRATIONS}\n"
            "print('HAS_48_49' if {48, 49} <= vs else 'MISSING')\n")
    proc = subprocess.run(
        [sys.executable, "-c", body], cwd=str(SERVER_DIR),
        env={"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", ""),
             "ORB_DEPLOYMENT_PROFILE": "dev",
             "ORB_JWT_SECRET": "probe-only-secret-0123456789abcdef"},
        capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    assert "HAS_48_49" in proc.stdout


def test_loading_migrations_py_by_path_still_collects_the_files(tmp_path):
    """scripts/lib/verify_restore.py (the backup and restore-drill verifier)
    loads server/migrations.py BY PATH under another name, with server/ not on
    sys.path, to learn the schema version a restore must reach. The per-file
    migrations must still be found that way, twice in one process, without
    leaving a 'migrations' alias behind. (Before this guard, every drill failed
    its schema-version check with "No module named 'migrations_his'".)"""
    body = textwrap.dedent(f"""
        import importlib.util, sys
        assert not any(p.rstrip('/').endswith('server') for p in sys.path)
        versions = []
        for name in ('manifest_a', 'manifest_b'):
            spec = importlib.util.spec_from_file_location(name, {str(SERVER_DIR / 'migrations.py')!r})
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            versions.append(max(m.version for m in mod.MIGRATIONS))
        print('VERSIONS', versions, 'ALIAS', 'migrations' in sys.modules)
    """)
    proc = subprocess.run(
        [sys.executable, "-c", body], cwd=str(tmp_path),
        env={"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", "")},
        capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    top = max(m.version for m in migrations.MIGRATIONS)
    assert f"VERSIONS [{top}, {top}] ALIAS False" in proc.stdout, proc.stdout
