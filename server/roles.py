"""Orb — the single authority for which role strings a users row may hold.

STORABLE_ROLES drives every rendering of the users.role CHECK constraint:

  * database.SCHEMA (fresh databases),
  * the newest users-rebuild migration, which FREEZES a copy of this list
    (see migrations.LATEST_USERS_ROLE_CHECK and the parity test in
    tests/test_migrations.py),
  * routes/admin_users_routes.ALLOWED_ROLES — the staffing path.

(services/helix_db.py once rebuilt users too; it no longer touches the table.)

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
     new migration is three lines (one file under server/migrations_his/
     since migration 48), and reassign migrations.LATEST_USERS_ROLE_CHECK.

A role that belongs to an opt-in part of Orb also gets a ROLE_FEATURE entry
below, so it cannot be staffed while that part is switched off.

This module must stay import-free: permissions.py imports database.py, and
database.py needs this list at import time to render SCHEMA — any import
here could close that cycle.
"""

# Every role a users row may store. Order is cosmetic (it is the order the
# CHECK renders in); membership is the contract.
#
# The last five arrived together in migration 48 (users_role_batch, 3 October
# 2026), one users rebuild for the whole hospital-system build rather than one
# per epic, because every users rebuild is an FK-parent rebuild that commits the
# migration runner's transaction (see migrations._rebuild_users_role_check).
# No epic may add a users COLUMN either: the rebuild copies a frozen column
# list and aborts on a column it does not know.
STORABLE_ROLES: tuple[str, ...] = (
    "doctor",
    "nurse",
    "admin",
    "patient",
    "pharmacist",
    "cashier",
    # Registration desk (OPD epic). Department optional: blank means a
    # central desk that serves every clinic.
    "receptionist",
    # Beds and admissions clerk (ADT epic). Department required.
    "ward_clerk",
    # Laboratory staff (LIS epic). Hospital-wide, no department.
    "lab_technician",
    "pathologist",
    "phlebotomist",
)

# Permission-lookup aliases: accepted by permissions.ROLE_PERMISSIONS (legacy
# token vocabulary), never stored in users.role, so never in the CHECK.
ROLE_ALIASES: dict[str, str] = {"clinician": "doctor"}

# A role that only makes sense while its part of Orb is switched on. User
# Management refuses to create such an account (or to move an account into the
# role) while the switch is off, so a site never holds a login whose whole job
# is a screen this appliance does not offer. The value is a switch id that
# config.switch_enabled() understands: an opt-in module id ('adt', 'lab') or a
# feature flag id ('registration'). Strings only, so this module stays
# import-free (see the module docstring).
#
# An existing account keeps working if its switch is later turned off: it is
# not deleted or locked, and the shell shows it a plain "switched off" notice.
ROLE_FEATURE: dict[str, str] = {
    "receptionist": "registration",
    "ward_clerk": "adt",
    "lab_technician": "lab",
    "pathologist": "lab",
    "phlebotomist": "lab",
}


def role_check_sql() -> str:
    """The users.role CHECK's IN-list, rendered identically at every site."""
    return ", ".join(f"'{r}'" for r in STORABLE_ROLES)
