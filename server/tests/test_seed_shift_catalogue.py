"""The dev seed rosters on the hospital's shift catalogue, not a second one.

Migration 24 installs MORNING / EVENING / NIGHT (07-14, 14-21, 21-07) plus
GENERAL. scripts/seed_modules.py used to install its own M / E / N set with
different hours beside them (07-15, 15-23, 23-07), so the roster screen offered
two catalogues for one hospital and a seeded roster ran on the one no site
configured. Runs in a subprocess against its own database, like
test_seed_modules, and for the same reason.
"""
from __future__ import annotations

from tests.test_seed_modules import _FIXTURE, _run

_SCRIPT = _FIXTURE + """
from database import db_session
from scripts.seed_modules import seed_modules

# What an earlier run of the seed left behind, plus a site's own "M" shift
# that merely shares a code and must not be touched.
with db_session() as db:
    db.execute(
        "INSERT OR REPLACE INTO duty_shift_types "
        "(code, name, start_minute, duration_minutes, allowance_code, is_active) "
        "VALUES ('E', 'Evening 15:00–23:00', 900, 480, 'evening', 1)")
    db.execute(
        "INSERT OR REPLACE INTO duty_shift_types "
        "(code, name, start_minute, duration_minutes, allowance_code, is_active) "
        "VALUES ('M', 'Medical officer day', 480, 600, NULL, 1)")

seed_modules(verbose=False)

with db_session() as db:
    roster_codes = sorted({r["shift_code"] for r in db.execute(
        "SELECT shift_code FROM duty_roster WHERE status <> 'cancelled'").fetchall()})
    active = {r["code"]: r["is_active"] for r in db.execute(
        "SELECT code, is_active FROM duty_shift_types").fetchall()}

print("RESULT" + json.dumps({
    "roster_codes": roster_codes,
    "active": active,
}))
"""


def test_seeded_roster_uses_only_the_migration_shifts(tmp_path):
    result = _run(_SCRIPT, tmp_path / "orb.db", "dev")
    assert result["roster_codes"], "the seed wrote no roster"
    assert set(result["roster_codes"]) <= {"MORNING", "EVENING", "NIGHT"}, result["roster_codes"]
    # The legacy seed shift is retired, not deleted: a roster row may name it.
    assert result["active"]["E"] == 0
    # A site's own shift that only shares a code is left exactly as it was.
    assert result["active"]["M"] == 1
    for code in ("MORNING", "EVENING", "NIGHT", "GENERAL"):
        assert result["active"][code] == 1
