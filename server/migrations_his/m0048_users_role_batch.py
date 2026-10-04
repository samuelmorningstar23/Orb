"""Migration 48: admit the five hospital-system roles into users.role.

receptionist (registration desk, OPD epic), ward_clerk (beds and admissions,
ADT epic), and lab_technician, pathologist and phlebotomist (laboratory, LIS
epic). One rebuild for all five, because every users rebuild is an FK-parent
rebuild that commits the runner's transaction (migrations.
_rebuild_users_role_check explains why it cannot run inside one). Batching
them means one such step at deploy time instead of three.

The role list is FROZEN here as a literal. A shipped migration must produce
the same schema forever, so it never reads roles.STORABLE_ROLES; the parity
test in tests/test_migrations.py compares this tuple, migrations.
LATEST_USERS_ROLE_CHECK and roles.STORABLE_ROLES.

Each new role is inert until its switch is on: User Management refuses to
create one while the switch is off (roles.ROLE_FEATURE), and every route its
permissions open answers 404 while its module or feature is off. On the Ward
Record profile and the demo databases the only visible effect is the wider
CHECK.

Idempotent: a no-op once the stored CHECK already names every role (a fresh
database renders the full list from roles.STORABLE_ROLES in database.SCHEMA,
so there it never rebuilds at all).

DEPLOYING IT on a database that already holds users: take a verified
scripts/backup_db.py backup first, and dry-run init_db() on a copy of that
backup on the isolated verify stack before the real database (docs/
HIS_BUILD_PLAN_2026-10.md, "The two FK-parent rebuilds").

THE FOREIGN-KEY CHECK (added in wave 2, 4 October 2026, for future runs; the
migration's version, name and effect are unchanged). The rebuild runs with
foreign_keys OFF, so nothing enforces the ~20 child tables' references to users
while it runs. When the rebuild actually happens, apply() runs PRAGMA
foreign_key_check on every table that references users, before and after, and
refuses to continue if the rebuild left any child row without its users parent
that was not already orphaned before it. A pre-existing orphan is not this
migration's doing and does not block it. The rebuild has already committed by
then (it owns its own transaction), so the refusal stops the runner before the
ledger records 48: restore the verified backup taken before the deploy rather
than run on a users table whose children no longer resolve.
"""
from migrations import Migration, _rebuild_users_role_check

FROZEN_ROLES: tuple = (
    "doctor", "nurse", "admin", "patient", "pharmacist", "cashier",
    "receptionist", "ward_clerk", "lab_technician", "pathologist", "phlebotomist",
)


def _check_names_every_role(conn) -> bool:
    """The same guard _rebuild_users_role_check uses: True when the stored
    users CHECK already names every frozen role, so the rebuild will no-op."""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='users'"
    ).fetchone()
    ddl = (row[0] or "") if row else ""
    return all(f"'{r}'" in ddl for r in FROZEN_ROLES)


def _users_fk_violations(conn) -> set:
    """Every (table, rowid, parent, fkid) PRAGMA foreign_key_check reports
    against users, over the tables that reference users. Read-only, and
    correct with foreign_keys on or off."""
    found: set = set()
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()]
    for name in tables:
        quoted = name.replace('"', '""')
        refs_users = any(
            str(fk[2]).lower() == "users"
            for fk in conn.execute(f'PRAGMA foreign_key_list("{quoted}")').fetchall()
        )
        if not refs_users:
            continue
        for v in conn.execute(f'PRAGMA foreign_key_check("{quoted}")').fetchall():
            if str(v[2]).lower() == "users":
                found.add((v[0], v[1], v[2], v[3]))
    return found


def apply(conn) -> None:
    rebuilds = not _check_names_every_role(conn)
    before = _users_fk_violations(conn) if rebuilds else set()
    _rebuild_users_role_check(conn, FROZEN_ROLES)
    if not rebuilds:
        return
    new = sorted(_users_fk_violations(conn) - before, key=repr)
    if new:
        raise RuntimeError(
            f"migration 48: the users rebuild left {len(new)} child row(s) without "
            f"their users parent, for example {new[:5]}. Stop here and restore the "
            "verified backup taken before this deploy; do not run Orb on it."
        )


MIGRATION = Migration(48, "users_role_batch", apply)
