"""The SQL-site registry ratchet and the generated fence list (multisite-5b, wave 11).

tests/facility_sql_sites/ classifies every owner of a SQL statement on
patients, admissions, opd_visits, appointments, surgeries, orders,
medications, invoices, payments or audit_logs, and every route group the
application serves. Pinned here:

  * every SQL site in the server is registered, and no row is stale; an owner
    judged safe (facility, patient_keyed, document_keyed) still has exactly
    the tables and the number of statements it was judged with;
  * a scratch module with an unregistered statement on patients fails the
    ratchet, in every spelling the scan reads (a plain string, an f-string, a
    '+' chain, a module-level constant, a method, a helper that takes the
    department as an argument);
  * the fence's FACILITY_BLIND_SURFACES, FACILITY_CLEARED_SURFACES and
    NO_PATIENT_DATA_SURFACES equal the registry's route group rows;
  * pharmacy_routes, flight_recorder_routes and audit_routes are blind;
  * every route the application serves sits under a classified group, an
    open one or the UNFENCED list, and every route or socket handler that can
    reach a blind or unregistered SQL owner (on the call graph) is fenced;
  * a patient_keyed owner is reached only by routes that check the patient's
    facility-aware scope, and by no socket handler;
  * the fence fails closed on a route group nobody has classified.

Run with the test env, never the shipping .env.
"""
from __future__ import annotations

import ast
import textwrap

import pytest

import facility_fence as F
import main
from tests import facility_sql_sites as R


@pytest.fixture(scope="module")
def found():
    return R.scan()


@pytest.fixture(scope="module")
def rows():
    return R.registry()


@pytest.fixture(scope="module")
def graph():
    return R.graph()


@pytest.fixture(scope="module")
def routes(graph):
    return R.app_routes(main.app, graph)


def _report(problems: list) -> str:
    drafts = R.draft_rows()
    return "\n".join(problems) + (("\n\nDraft rows:\n" + drafts) if drafts else "")


# ── 1. The ratchet ───────────────────────────────────────────────────────────

def test_every_sql_site_is_registered_and_no_row_is_stale(found, rows):
    problems = R.ratchet(found, rows)
    assert problems == [], _report(problems)


def test_every_row_follows_the_registry_rules(rows):
    assert R.row_problems(rows) == []


def test_the_scan_sees_the_whole_server(found, rows):
    """A broken scan would make the ratchet pass vacuously."""
    modules = {m for m, _ in found}
    assert sum(f.count for f in found.values()) > 700
    assert len(found) > 450
    for module in ("permissions.py", "routes/vigil_routes.py", "services/vigil_service.py",
                   "services/billing_service.py", "services/audit_service.py",
                   "routes/audit_routes.py", "services/pharmacy_verification_service.py",
                   "services/adt/core.py", "services/medorders/core.py", "migrations.py"):
        assert module in modules, module
    tables = {t for f in found.values() for t in f.tables}
    assert tables == set(R.TABLES)
    # Every kind is in use, and the platform is covered whole.
    kinds = {row.kind for row in rows.values()}
    assert kinds >= {R.FACILITY, R.PATIENT_KEYED, R.APPLIANCE, R.BLIND}
    for whole in (("migrations.py", "*"), ("migrations_his/", "*"), ("scripts/", "*")):
        assert rows[whole].kind == R.APPLIANCE


def _scratch(tmp_path, source: str, name: str = "services/scratch_lane.py"):
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(source))
    return R.scan(tmp_path)


SCRATCH = '''
    """Docstrings name FROM patients without being a site."""
    from permissions import department_scope

    EVERYONE_SQL = "SELECT id, name FROM patients WHERE status = 'admitted'"


    def list_everyone(db):
        return db.execute("SELECT name, mrn FROM patients").fetchall()


    def board_for(db, dept):
        # A helper that takes the department as an argument and never calls
        # department_scope itself.
        return db.execute(f"SELECT p.* FROM patients p JOIN admissions a "
                          f"ON a.patient_id = p.id WHERE a.department = '{dept}'").fetchall()


    def chained(db, limit):
        return db.execute("SELECT * FROM invoices " + "ORDER BY id LIMIT " + str(limit))


    class Ledger:
        def rows(self, db):
            return db.execute("SELECT * FROM payments").fetchall()


    def house_wide(db):
        """No department_scope anywhere: the scan does not care."""
        return db.execute("SELECT COUNT(*) FROM audit_logs").fetchone()
'''


def test_an_unregistered_statement_on_patients_fails_the_ratchet(tmp_path, rows):
    scratch = _scratch(tmp_path, SCRATCH)
    problems = [p for p in R.ratchet(scratch, rows) if p.startswith("unregistered:")]
    joined = "\n".join(problems)
    for owner, tables in (("EVERYONE_SQL", "patients"), ("list_everyone", "patients"),
                          ("board_for", "admissions, patients"), ("chained", "invoices"),
                          ("Ledger.rows", "payments"), ("house_wide", "audit_logs")):
        assert f"services/scratch_lane.py {owner} touches {tables}" in joined, joined
    assert len(problems) == 6, joined
    assert "draft" not in joined.lower()
    # The docstring and the comment are not sites.
    assert ("services/scratch_lane.py", "<module>") not in scratch


def test_a_registered_scratch_owner_passes_and_a_changed_safe_owner_fails(tmp_path):
    scratch = _scratch(tmp_path, '''
        def one(db, pid):
            return db.execute("SELECT name FROM patients WHERE id = ?", (pid,)).fetchone()
    ''')
    key = ("services/scratch_lane.py", "one")
    judged = {key: R.patient_keyed(["patients"], 1, "keyed on one patient for the test")}
    assert R.ratchet(scratch, judged) == []
    grown = _scratch(tmp_path, '''
        def one(db, pid):
            row = db.execute("SELECT name FROM patients WHERE id = ?", (pid,)).fetchone()
            return row, db.execute("SELECT * FROM admissions").fetchall()
    ''')
    problems = R.ratchet(grown, judged)
    assert len(problems) == 1 and problems[0].startswith("changed: services/scratch_lane.py one")
    # A blind row pins only its existence: the new statement stays behind the fence.
    assert R.ratchet(grown, {key: R.blind("multisite-8")}) == []
    # A row whose owner lost its last statement is stale.
    gone = _scratch(tmp_path, "def one(db):\n    return None\n")
    assert R.ratchet(gone, judged) == [
        "stale: services/scratch_lane.py one is registered but holds no SQL site any more."]


def test_whole_module_rows_are_for_appliance_rows_only():
    bad = {("services/scratch_lane.py", "*"): R.blind("multisite-8"),
           ("routes/x.py", "f"): R.blind("multisite-99"),
           ("routes/y.py", "g"): R.patient_keyed(["patients"], 0, "too short"),
           ("routes/z.py", "h"): R.appliance("")}
    problems = "\n".join(R.row_problems(bad, groups={}))
    assert "only an appliance row covers a whole module" in problems
    assert "names its owning increment" in problems
    assert "pins its tables and site count" in problems
    assert "says why" in problems


# ── 2. The fence list is the registry's ──────────────────────────────────────

def test_the_fence_lists_equal_the_registry_route_groups():
    assert F.FACILITY_BLIND_SURFACES == R.blind_prefixes()
    assert F.FACILITY_CLEARED_SURFACES == R.cleared_prefixes()
    assert F.NO_PATIENT_DATA_SURFACES == R.no_patient_data_prefixes()
    assert F.FACILITY_BLIND_SURFACES != F.SENTINEL
    # The generated source is what facility_fence.py carries, value for value.
    generated: dict = {}
    exec(R.generated_fence_source(), {}, generated)
    assert generated["FACILITY_BLIND_SURFACES"] == F.FACILITY_BLIND_SURFACES
    assert generated["FACILITY_CLEARED_SURFACES"] == F.FACILITY_CLEARED_SURFACES
    assert generated["NO_PATIENT_DATA_SURFACES"] == F.NO_PATIENT_DATA_SURFACES
    # No prefix is both, and an open prefix is never listed.
    assert not set(F.FACILITY_BLIND_SURFACES) & set(F.FACILITY_CLEARED_SURFACES)
    for prefix in F.FACILITY_BLIND_SURFACES + F.FACILITY_CLEARED_SURFACES:
        assert not any(R.under(prefix, o) for o in F.OPEN_SURFACES), prefix


def test_the_named_house_wide_lanes_are_blind(rows, routes):
    for prefix in ("/api/pharmacy", "/api/flight-recorder", "/api/audit"):
        assert prefix in F.FACILITY_BLIND_SURFACES
    by_module = {}
    for r in routes:
        if r.node:
            by_module.setdefault(r.node[0], []).append(r)
    for module in ("routes/pharmacy_routes.py", "routes/flight_recorder_routes.py",
                   "routes/audit_routes.py"):
        assert by_module.get(module), module
        for r in by_module[module]:
            assert F.is_fenced(r.path), (module, r.path)
    for key in (("routes/audit_routes.py", "get_audit_logs"),
                ("routes/audit_routes.py", "get_audit_facets"),
                ("services/flight_recorder_service.py", "get_timeline"),
                ("services/flight_recorder_service.py", "get_event_detail"),
                ("services/pharmacy_verification_service.py", "list_pending_verification"),
                ("services/pharmacy_verification_service.py", "verify_medication"),
                ("services/pharmacy_verification_service.py", "reject_medication")):
        assert rows[key].kind == R.BLIND, key


def test_helpers_given_the_department_and_house_wide_routes_are_blind(rows):
    """The two shapes a department_scope-keyed ratchet could not see."""
    assert rows[("services/vigil_service.py", "scored_admitted_patients")].kind == R.BLIND
    assert rows[("routes/search_routes.py", "search_patients")].kind == R.BLIND
    assert rows[("services/sage_service.py", "_out_of_scope_locators")].kind == R.BLIND
    assert rows[("services/sage_service.py", "_out_of_scope_locators")].owner == "multisite-10"


# ── 3. Routes and the call graph ─────────────────────────────────────────────

def _fenced_root(path: str) -> bool:
    return any(R.under(path, root) for root in F.FENCED_ROOTS)


def test_every_route_is_classified(routes):
    groups = R.surfaces()
    unfenced = R.unfenced()
    loose = []
    for r in routes:
        if r.methods == ("MOUNT",):
            # Only the built screens may be mounted (StaticFiles, no data).
            assert r.endpoint == "StaticFiles", r
            assert not _fenced_root(r.path), r
            continue
        if _fenced_root(r.path):
            if any(R.under(r.path, o) for o in F.OPEN_SURFACES):
                continue
            if R.surface_for(r.path, groups) is None:
                loose.append(f"{','.join(r.methods)} {r.path} ({r.endpoint})")
        elif r.path not in unfenced:
            loose.append(f"{','.join(r.methods)} {r.path} outside the fence ({r.endpoint})")
    assert loose == [], ("Routes no route group or UNFENCED entry covers; add a group to "
                         "the area file of the sweep that owns them:\n" + "\n".join(loose))
    # Every group and UNFENCED entry still names something the app serves.
    paths = [r.path for r in routes]
    for prefix in groups:
        assert any(R.under(p, prefix) for p in paths), f"route group {prefix} serves nothing"
    for path in unfenced:
        assert path in paths, f"UNFENCED {path} serves nothing"


def test_the_graph_follows_routes_to_their_statements(graph, routes):
    """A graph that lost its edges would make the fence check pass vacuously."""
    def reaches(method, path, key):
        r = next(r for r in routes if r.path == path and method in r.methods)
        return key in graph.reach(r.node)

    assert reaches("GET", "/api/vigil/patients", ("services/vigil_service.py", "scored_admitted_patients"))
    assert reaches("GET", "/api/pharmacy/pending",
                   ("services/pharmacy_verification_service.py", "list_pending_verification"))
    assert reaches("GET", "/api/search/patients", ("routes/search_routes.py", "search_patients"))
    assert reaches("GET", "/api/audit/logs", ("routes/audit_routes.py", "get_audit_logs"))
    assert reaches("GET", "/api/auth/me", ("permissions.py", "FACILITY_PATIENT_SQL"))
    assert graph.socket_handlers(), "no Socket.IO handler found under ws/"
    found = R.scan()
    connected = sum(1 for r in routes if r.node and graph.reach(r.node) & set(found))
    assert connected > 500


def _reached_problem_owners(graph, node, found, rows):
    out = []
    for key in sorted(graph.reach(node) & set(found)):
        _, row = R.row_for(rows, *key)
        if row is None or row.kind == R.BLIND:
            out.append(f"{key[0]} {key[1]} ({'unregistered' if row is None else row.owner})")
    return out


def test_every_route_reaching_a_blind_statement_is_fenced(graph, routes, found, rows):
    open_leaks = []
    for r in routes:
        if r.node is None:
            continue
        owners = _reached_problem_owners(graph, r.node, found, rows)
        if owners and not F.is_fenced(r.path):
            open_leaks.append(f"{','.join(r.methods)} {r.path} reaches " + "; ".join(owners))
    for node in graph.socket_handlers():
        owners = _reached_problem_owners(graph, node, found, rows)
        if owners and not F.is_fenced("/socket.io/", F.SOCKET_ROOTS):
            open_leaks.append(f"socket {node[1]} reaches " + "; ".join(owners))
    assert open_leaks == [], ("Routes the fence lets through while more than one facility is "
                              "on record that reach blind or unregistered SQL:\n"
                              + "\n".join(open_leaks))


def test_patient_keyed_owners_are_reached_only_through_scope_checks(graph, routes, rows):
    socket_reach = set()
    for node in graph.socket_handlers():
        socket_reach |= graph.reach(node)
    keyed = [k for k, row in rows.items() if row.kind == R.PATIENT_KEYED]
    assert keyed
    problems = []
    for key in keyed:
        reaching = [r for r in routes if r.node and key in graph.reach(r.node)]
        if not reaching:
            problems.append(f"{key}: no route reaches it (move it to the kind that fits)")
        for r in reaching:
            if not graph.checks_scope(r.node):
                problems.append(f"{key}: reached by {r.path} with no facility-aware scope check")
        if key in socket_reach:
            problems.append(f"{key}: reached by a socket handler")
    assert problems == []


def test_the_open_routes_reach_only_safe_statements(graph, routes, found, rows):
    """Sign-in, health, facility identity and the operations page are never
    fenced, so everything they reach must be safe or appliance-level now."""
    checked = 0
    for r in routes:
        if r.node is None or not any(R.under(r.path, o) for o in F.OPEN_SURFACES):
            continue
        checked += 1
        assert _reached_problem_owners(graph, r.node, found, rows) == [], r.path
    assert checked > 20


# ── 4. The fence reads the lists ─────────────────────────────────────────────

@pytest.mark.parametrize("path, fenced", [
    ("/api/vigil/patients", True),
    ("/api/pharmacy/pending", True),
    ("/api/pharmacy-store/stock", True),
    ("/uploads/scan.png", True),
    ("/socket.io/", True),
    ("/api/pulse/season", False),
    ("/api/pulse", False),
    ("/api/auth/me", False),
    ("/api/facilities", False),
    ("/api/ops/status", False),
    # A route group nobody has classified fails closed.
    ("/api/mortuary/register", True),
    ("/api/pulsex", True),
    ("/api", True),
    # A crafted spelling of a cleared prefix is under none.
    ("/api/pulse/../vigil/patients", True),
    ("/api/pulse//season", True),
    # Outside the roots nothing is fenced.
    ("/metrics", False),
    ("/dashboard/vigil", False),
])
def test_the_fence_holds_blind_and_unlisted_groups_shut(path, fenced):
    assert F.is_fenced(path) is fenced


def test_an_empty_blind_list_holds_nothing_shut(monkeypatch):
    for path in ("/api/vigil/patients", "/api/mortuary/register", "/api/pulse/season"):
        assert F.is_fenced(path, blind=()) is False
    # A caller's own list fences only what it names, unless it names the
    # cleared groups too (then the remainder fails closed, as the runtime does).
    assert F.is_fenced("/api/sage/query", blind=("/api/sage",)) is True
    assert F.is_fenced("/api/vigil/patients", blind=("/api/sage",)) is False
    assert F.is_fenced("/api/vigil/patients", blind=("/api/sage",), cleared=("/api/vigil",)) is False
    assert F.is_fenced("/api/vigil/patients", blind=("/api/sage",), cleared=()) is True
    # The tests that open the fence set the module's list to ().
    monkeypatch.setattr(F, "FACILITY_BLIND_SURFACES", ())
    for path in ("/api/vigil/patients", "/api/mortuary/register", "/uploads/scan.png"):
        assert F.is_fenced(path) is False
    # The sentinel still fences everything.
    monkeypatch.setattr(F, "FACILITY_BLIND_SURFACES", F.SENTINEL)
    assert F.is_fenced("/api/pulse/season") is True
    assert F.describe()["blind_surfaces"] == ["all"]


def test_the_operations_page_describes_the_list():
    d = F.describe()
    assert d["every_patient_screen"] is True     # only /api/pulse is cleared, and it has no patient
    assert d["blind_surfaces"] == sorted(F.FACILITY_BLIND_SURFACES)
    assert d["cleared_surfaces"] == ["/api/pulse"]
    assert d["unlisted_surfaces_fenced"] is True
    assert d["always_open"] == list(F.OPEN_SURFACES)


def test_the_area_files_import_nothing_but_the_registry():
    """Each area file is data for one sweep cluster: rows only."""
    for name in R.AREAS:
        path = R.SERVER / "tests" / "facility_sql_sites" / f"{name}.py"
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                pytest.fail(f"{name}.py imports {[a.name for a in node.names]}")
            if isinstance(node, ast.ImportFrom):
                assert node.level == 1 and node.module is None, f"{name}.py imports {node.module}"
