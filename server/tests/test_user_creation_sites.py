"""Every place the server creates a users row, and which facility the account
lands at (multisite-5b, wave 11; design risk 2 and critique issue 1).

A department-less account with no live home row in user_facilities counts as
facility 1 (facility_service.facility_scope_for). So a lane that creates a
department-less staff account at another facility without writing its home
row would put that account in facility 1's scope. This ratchet lists the 17
INSERT INTO users statements the server holds (multisite-5a counted the same
17 on 3 October) and why each lands where it should; a new one fails until it
is classified here. Kinds:

  home_row       staff created by an administrator or at the console; the same
                 transaction writes the home row (W9-H multisite-6), and the
                 owner is checked to call the writer.
  service        an internal account listed in database.SERVICE_ACCOUNT_IDS,
                 which facility_scope_for treats as group-wide (no patient ever
                 signs in as it); the inserted ids are checked against the list.
  patient_login  a Bridge portal login (role 'patient', UNBOUND_ROLES): bound to
                 its own record, never to a facility's list.
  console_seed   a console seed or benchmark script for a one-facility
                 development or demonstration database; no request reaches it.

What counts as a site: the same scan as the SQL-site registry
(tests/facility_sql_sites), for INSERT [OR ...] INTO users. Run with the test
env, never the shipping .env.
"""
from __future__ import annotations

import re
import textwrap

import pytest

from tests import facility_sql_sites as R

USERS_INSERT = re.compile(r"\bINSERT\s+(?:OR\s+\w+\s+)?INTO\s+[\"`\[]?users\b", re.IGNORECASE)

HOME_ROW, SERVICE, PATIENT_LOGIN, CONSOLE_SEED = "home_row", "service", "patient_login", "console_seed"

#: (module, owner): (statements, kind, detail). For service rows the detail
#: names the inserted ids (a literal, or module.CONSTANT); for home_row rows,
#: the home-row writer the owner calls.
SITES = {
    ("routes/admin_users_routes.py", "create_user"):
        (1, HOME_ROW, ("routes/admin_users_routes.py", "_write_home_row")),
    ("scripts/create_admin.py", "main"):
        (1, HOME_ROW, ("scripts/create_admin.py", "_write_home_row")),
    ("database.py", "init_db"): (1, SERVICE, ("system",)),
    ("services/sage_assessment_service.py", "_finalise_ready"): (1, SERVICE, ("system",)),
    ("services/bot_service.py", "_persist_alerts"): (1, SERVICE, ("services.bot_service.VIGIL_BOT_ID",)),
    ("services/bot_service.py", "_create_report"): (1, SERVICE, ("services.bot_service.LENS_BOT_ID",)),
    ("services/bot_service.py", "_generate_lab_result"):
        (1, SERVICE, ("services.bot_service.LAB_BOT_ID",)),
    ("services/ingestion_service.py", "_persist_relay_alert"):
        (1, SERVICE, ("services.bot_service.VIGIL_BOT_ID",)),
    ("services/bridge_service.py", "_store_red_flag"):
        (1, SERVICE, ("services.bridge_service.BRIDGE_BOT_ID",)),
    ("services/lab_critical_service.py", "ensure_system_lab_user"):
        (1, SERVICE, ("services.lab_critical_service.SYSTEM_LAB_ID",)),
    ("services/nervous_system_triggers.py", "_persist_extraction"): (1, SERVICE, ("bot_scribe",)),
    ("services/admission_service.py", "_insert_portal_login_on"): (1, PATIENT_LOGIN, None),
    ("scripts/seed_clinical_data.py", "seed"): (1, CONSOLE_SEED, None),
    ("scripts/seed_relay_chats.py", "seed_chats"): (1, CONSOLE_SEED, None),
    ("scripts/wedge_bench.py", "seed_staff"): (2, CONSOLE_SEED, None),
    ("scripts/wedge_bench.py", "seed_patient"): (1, CONSOLE_SEED, None),
}


def _match(text: str) -> tuple:
    return ("users",) if USERS_INSERT.search(text) else ()


def _scan(root=R.SERVER) -> dict:
    return R.scan(root, match=_match)


def _problems(found: dict) -> list:
    out = []
    for key in sorted(found):
        f = found[key]
        row = SITES.get(key)
        if row is None:
            out.append(f"unclassified: {key[0]} {key[1]} creates users rows "
                       f"(line(s) {', '.join(map(str, f.lines))}). Say in "
                       "tests/test_user_creation_sites.py which facility the account lands at.")
        elif row[0] != f.count:
            out.append(f"changed: {key[0]} {key[1]} had {row[0]} users insert(s), now {f.count}.")
    for key in sorted(set(SITES) - set(found)):
        out.append(f"stale: {key[0]} {key[1]} no longer creates users rows.")
    return out


@pytest.fixture(scope="module")
def found():
    return _scan()


def test_every_users_insert_is_classified(found):
    assert _problems(found) == []


def test_there_are_seventeen_today(found):
    assert sum(f.count for f in found.values()) == 17 == sum(r[0] for r in SITES.values())


def test_a_new_users_insert_fails(tmp_path, found):
    lane = tmp_path / "routes" / "new_lane.py"
    lane.parent.mkdir(parents=True)
    lane.write_text(textwrap.dedent('''
        def make_cashier(db, uid):
            db.execute("INSERT INTO users (id, name, role, password_hash) "
                       "VALUES (?, 'Cashier', 'cashier', 'x')", (uid,))

        def make_clerk(db, uid):
            db.execute(f"INSERT OR IGNORE INTO users (id, name, role) VALUES ('{uid}', 'C', 'ward_clerk')")

        def not_a_site(db):
            # user_facilities and other tables are not users rows.
            db.execute("INSERT INTO user_facilities (user_id, facility_id, kind) VALUES (?, 1, 'home')")
            db.execute("INSERT INTO users_archive (id) VALUES (?)")
    '''))
    problems = _problems({**found, **_scan(tmp_path)})
    assert problems == [
        "unclassified: routes/new_lane.py make_cashier creates users rows (line(s) 3). Say in "
        "tests/test_user_creation_sites.py which facility the account lands at.",
        "unclassified: routes/new_lane.py make_clerk creates users rows (line(s) 7). Say in "
        "tests/test_user_creation_sites.py which facility the account lands at.",
    ]


def test_every_service_account_is_group_wide_by_list():
    """An internal account outside SERVICE_ACCOUNT_IDS with no department would
    count as facility 1's, and its alerts would be scoped there."""
    import importlib

    import database
    for key, (_, kind, ids) in SITES.items():
        if kind != SERVICE:
            continue
        for ident in ids:
            if "." in ident:
                module, attr = ident.rsplit(".", 1)
                ident = getattr(importlib.import_module(module), attr)
            assert ident in database.SERVICE_ACCOUNT_IDS, (key, ident)


def test_staff_creation_writes_the_home_row_in_the_same_owner():
    g = R.graph()
    for key, (_, kind, writer) in SITES.items():
        if kind == HOME_ROW:
            assert writer in g.edges.get(key, set()), (key, writer)
            # The writer does write a home row.
            src = (R.SERVER / writer[0]).read_text()
            assert "INSERT INTO user_facilities" in src


def test_a_portal_login_is_a_patient_login():
    import inspect

    from services import admission_service
    src = inspect.getsource(admission_service._insert_portal_login_on)
    assert "'patient'" in src
    import services.facility_service as FS
    assert "patient" in FS.UNBOUND_ROLES


def test_console_seeds_are_scripts():
    for key, (_, kind, _) in SITES.items():
        if kind == CONSOLE_SEED:
            assert key[0].startswith("scripts/"), key
