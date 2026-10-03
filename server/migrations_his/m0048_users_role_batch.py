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
"""
from migrations import Migration, _rebuild_users_role_check

FROZEN_ROLES: tuple = (
    "doctor", "nurse", "admin", "patient", "pharmacist", "cashier",
    "receptionist", "ward_clerk", "lab_technician", "pathologist", "phlebotomist",
)


def apply(conn) -> None:
    _rebuild_users_role_check(conn, FROZEN_ROLES)


MIGRATION = Migration(48, "users_role_batch", apply)
