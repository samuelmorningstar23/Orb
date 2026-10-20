"""Orb: one file per migration, from migration 48 on.

WHY THIS PACKAGE EXISTS. Until 3 October 2026 every migration lived in
server/migrations.py, so every cluster that needed a schema change had to edit
the same file in the same wave. The hospital-system build runs several
clusters per wave, so migrations moved to one file each here, picked up by
version number. migrations.py keeps migrations 1 to 47, the runner, the users
rebuild helper and LATEST_USERS_ROLE_CHECK; it merges what this package
collects into its sorted MIGRATIONS list at import.

THE CONTRACT FOR A FILE IN THIS DIRECTORY
-----------------------------------------
* Its name is ``m<NNNN>_<name>.py``: four digits, then a lowercase snake_case
  name, for example ``m0050_registration_opd_foundation.py``.
* It defines ``MIGRATION = Migration(NN, "<name>", apply)`` where NN equals the
  four digits and "<name>" equals the rest of the file name. ``Migration`` is
  ``migrations.Migration``.
* ``apply(conn)`` follows every rule in migrations.py: idempotent, guarded by
  PRAGMA introspection, one statement per ``conn.execute`` (never
  ``executescript``, which commits the runner's transaction), ALTER TABLE ADD
  COLUMN with constant defaults only, no new FOREIGN KEY, and an FK-parent
  table rebuilt only through database._atomic_table_rebuild (see
  migrations._rebuild_users_role_check).
* Numbers 48 to 75 are allocated in docs/HIS_BUILD_PLAN_2026-10.md, one owner
  per number. Never renumber a migration that has reached any database.

Every breach of the contract is refused when migrations.py is imported, which
is at boot and at the start of every test session: a stray file, a misnamed
file, a version that does not match its file name, a missing MIGRATION, or a
version that another migration already holds. A silently skipped migration is
the failure this guards against, so nothing here is skipped quietly.
"""

from __future__ import annotations

import importlib
import importlib.util
import re
from pathlib import Path

FILENAME_RE = re.compile(r"^m(\d{4})_([a-z0-9]+(?:_[a-z0-9]+)*)\.py$")


class MigrationDiscoveryError(RuntimeError):
    """A migration file breaks the package contract; boot must not continue."""


def _load_file(path: Path, module_name: str):
    """Import one file by path (used for directories outside this package,
    which is how the tests exercise the refusals)."""
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise MigrationDiscoveryError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _still_importing(module) -> bool:
    """True while the import system is still executing this module's body
    (CPython marks the spec with _initializing for exactly that window)."""
    spec = getattr(module, "__spec__", None)
    return bool(getattr(spec, "_initializing", False))


def _is_migration(obj) -> bool:
    """A migrations.Migration (a NamedTuple of version, name, apply)."""
    return (
        isinstance(obj, tuple)
        and getattr(obj, "_fields", None) == ("version", "name", "apply")
        and isinstance(getattr(obj, "version", None), int)
        and isinstance(getattr(obj, "name", None), str)
    )


def collect_from(directory: Path, *, package: str | None = None) -> list:
    """Every Migration defined in `directory`, sorted by version.

    With `package`, files are imported as ``<package>.<stem>`` (the normal
    import system, so a module is loaded once). Without it, each file is
    loaded from its path under a private module name.
    """
    directory = Path(directory)
    found: dict[int, object] = {}
    for path in sorted(directory.iterdir()):
        if path.is_dir() or path.suffix != ".py" or path.name == "__init__.py":
            continue
        match = FILENAME_RE.match(path.name)
        if not match:
            raise MigrationDiscoveryError(
                f"{path.name} is in {directory.name}/ but is not named "
                "m<4 digits>_<snake_case name>.py, so it would never run. "
                "Rename it or move it out of the migrations directory."
            )
        version, name = int(match.group(1)), match.group(2)
        if version < 1:
            raise MigrationDiscoveryError(f"{path.name}: migration versions start at 1")
        if package:
            module = importlib.import_module(f"{package}.{path.stem}")
        else:
            module = _load_file(path, f"_orb_migration_{directory.name}_{path.stem}")
        mig = getattr(module, "MIGRATION", None)
        if mig is None and _still_importing(module):
            # The file is half-imported further up this very stack: something
            # imported it, it ran "from migrations import ...", and that import
            # is what is collecting now. Its MIGRATION line has simply not run
            # yet. Say so, rather than claim the file defines no MIGRATION.
            raise MigrationDiscoveryError(
                f"{path.name} was being imported when migrations.py started "
                "collecting this directory, so its MIGRATION is not defined yet "
                "(an import cycle, not a broken file). Import migrations first "
                '("import migrations"), then the migration file.'
            )
        # Checked by shape, not isinstance: scripts/lib/verify_restore.py loads
        # migrations.py by path, so its Migration class is a different object
        # from the one a cached migration file was built with.
        if mig is None or not _is_migration(mig):
            raise MigrationDiscoveryError(
                f"{path.name} defines no MIGRATION = Migration(version, name, apply)"
            )
        if mig.version != version or mig.name != name:
            raise MigrationDiscoveryError(
                f"{path.name} declares Migration({mig.version}, {mig.name!r}); the file "
                f"name says ({version}, {name!r}). They must agree."
            )
        if not callable(mig.apply):
            raise MigrationDiscoveryError(f"{path.name}: MIGRATION.apply is not callable")
        if version in found:
            raise MigrationDiscoveryError(
                f"migration {version} is defined twice in {directory.name}/"
            )
        found[version] = mig
    return [found[v] for v in sorted(found)]


def collect() -> list:
    """Every migration in this package, sorted by version."""
    return collect_from(Path(__file__).resolve().parent, package=__name__)


def merge(core: list, extra: list) -> list:
    """The core list plus the collected files, sorted by version. A version
    held by both, or twice in either, is refused."""
    seen: dict[int, str] = {}
    for mig in list(core) + list(extra):
        if mig.version in seen:
            raise MigrationDiscoveryError(
                f"migration {mig.version} is defined twice ({seen[mig.version]!r} and "
                f"{mig.name!r}). Take the next free number; never renumber one that "
                "has reached a database."
            )
        seen[mig.version] = mig.name
    return sorted(list(core) + list(extra), key=lambda m: m.version)


# IMPORTING A MIGRATION FILE BEFORE migrations.py. A file here starts with
# "from migrations import Migration", and importing migrations collects this
# directory, so "from migrations_his.m0048_users_role_batch import FROZEN_ROLES"
# in a fresh process used to re-enter the very file being imported and fail
# with a misleading "defines no MIGRATION". Importing migrations here, at the
# END of the package (after every function above exists), makes the package
# import always finish collection first; the file import then finds its module
# already complete. When migrations.py is the one importing this package, it
# is already in sys.modules and this line returns it at once. When migrations.py
# is loaded by file path (scripts/lib/verify_restore.py), it registers a module
# under the name "migrations" before importing this package (see
# migrations._with_his_migrations), so this line finds that one.

import migrations as _migrations  # noqa: E402,F401
