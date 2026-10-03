"""Orb — the single authority for which role strings a users row may hold.

STORABLE_ROLES drives every rendering of the users.role CHECK constraint:

  * database.SCHEMA (fresh databases),
  * the newest users-rebuild migration, which FREEZES a copy of this list
    (see migrations.LATEST_USERS_ROLE_CHECK and the parity test in
    tests/test_migrations.py),
  * services/helix_db.py's legacy rebuild branch,
  * routes/admin_users_routes.ALLOWED_ROLES — the staffing path.

Why this exists: the 2026-08-18 payments split minted a `cashier` role in
permissions.ROLE_PERMISSIONS, but the CHECK constraint predated it. The
seeding idiom `INSERT OR IGNORE` swallows a CHECK violation silently, so a
cashier account simply never landed and its token 401'd "User not found" —
a role that existed everywhere except in the one place that stores users.
Two authorities, one drift, zero errors. Now there is one authority and a
test that fails the suite when any rendering falls behind.

ADDING A ROLE is a three-step change, each step loud when missed:

  1. add the string here,
  2. give it a permission set in permissions.ROLE_PERMISSIONS,
  3. add a users-rebuild migration freezing the new list — SQLite cannot
     ALTER a CHECK constraint, so every widening is a table rebuild;
     migrations._rebuild_users_role_check does the heavy lifting and the
     new migration is three lines.

This module must stay import-free: permissions.py imports database.py, and
database.py needs this list at import time to render SCHEMA — any import
here could close that cycle.
"""

# Every role a users row may store. Order is cosmetic (it is the order the
# CHECK renders in); membership is the contract.
STORABLE_ROLES: tuple[str, ...] = (
    "doctor",
    "nurse",
    "admin",
    "patient",
    "pharmacist",
    "cashier",
)

# Permission-lookup aliases: accepted by permissions.ROLE_PERMISSIONS (legacy
# token vocabulary), never stored in users.role, so never in the CHECK.
ROLE_ALIASES: dict[str, str] = {"clinician": "doctor"}


def role_check_sql() -> str:
    """The users.role CHECK's IN-list, rendered identically at every site."""
    return ", ".join(f"'{r}'" for r in STORABLE_ROLES)
