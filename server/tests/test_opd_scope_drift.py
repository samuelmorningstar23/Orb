"""The OPD scope arm stays on its whitelist (opd-7, W3-A, 4 October 2026).

permissions.patient_in_opd_scope opens a patient's OUTPATIENT record view to a
clinic that saw them recently, with no admission. It is reached ONLY through
require_patient_access(..., opd_arm=True) or enforce_patient_scope(...,
opd_arm=True), and only on the routes pinned below, each with its reason.

Why a whitelist and not a convention: require_patient_access is one guard for
reads AND writes. If a chart route (medication add, nurse obs, allergy and
problem writes, bundles, discharge, the Vigil chart, FHIR export) ever passed
opd_arm=True, a clinician could open any non-admitted patient's chart by
registering them a visit. test_opd_chart_writes_closed.py proves those routes
stay closed today; this file stops tomorrow's route from opening one.

Rules enforced:
  1. Every live route that carries the arm (through a dependency marked
     orb_opd_arm, or opd_arm=True in its own body) is in OPD_ARM_WHITELIST,
     and every whitelisted route exists (no permit for a route that is not
     there yet: the increment that builds it adds its line with its reason).
  2. No non-GET route outside opd_routes, registration_routes and the
     appointment staff branch (appointments_routes) may carry it.
  3. The predicate itself is called from permissions.py only, and opd_arm=True
     appears only in those three route files. Sage is untouched.
"""
from __future__ import annotations

import ast
import inspect
import pathlib

from fastapi.routing import APIRoute

SERVER = pathlib.Path(__file__).resolve().parent.parent

#: (method, path) -> why this route may open the outpatient record view.
#: Add a line only with the reason written down, in the increment that builds
#: the route: opd-10 (GET /api/opd/patients/{patient_id}/consultations), opd-4
#: (GET and PATCH /api/registration/patients/{patient_id}, /changes, /card) and
#: opd-16 (the staff branch of POST /api/appointments) are expected next.
OPD_ARM_WHITELIST: dict[tuple[str, str], str] = {
    ("GET", "/api/opd/patients/{patient_id}/record"): (
        "The outpatient record view itself (opd-7): identity without the date of "
        "birth, allergies, problems, OPD-linked results and signed OPD "
        "consultations (ruling OPD-N2 extent ii). Read-only, opd.consult, audited "
        "with the arm that let the reader in."
    ),
    ("GET", "/api/opd/patients/{patient_id}/consultations"): (
        "The OPD consultation notes in full (opd-10): every clinic's signed notes "
        "(ruling OPD-N2 extent ii) and the reader's own clinic's drafts, with their "
        "addenda. Read-only, opd.consult, audited with the arm that let the reader in. "
        "OPD vitals are shown as typed, never scored."
    ),
    ("GET", "/api/registration/patients/{patient_id}"): (
        "The registration record (opd-4), registration.read, audited as "
        "registration_record_viewed with the arm; no chart data, no allergies. A reader "
        "let in by an OPD visit sees no date of birth, contact, address, next of kin, "
        "identity document or ABHA (the OPD record view's line)."
    ),
    ("PATCH", "/api/registration/patients/{patient_id}"): (
        "A reasoned demographic correction (opd-4) against row_version: fields need "
        "registration.write, the UHID the administrator; through an OPD visit the hidden "
        "details cannot be corrected. Writes patients identity columns, "
        "patient_demographics and the append-only change log, audited in-transaction."
    ),
    ("GET", "/api/registration/patients/{patient_id}/changes"): (
        "The append-only identity change history of one registration (opd-4), "
        "registration.read, audited as registration_changes_viewed with the arm; values "
        "of hidden details are removed for a reader let in by an OPD visit."
    ),
    ("GET", "/api/registration/patients/{patient_id}/card"): (
        "The UHID card print data (opd-4): UHID, name, age and sex only, "
        "registration.read, audited as uhid_card_printed with the arm."
    ),
    ("POST", "/api/appointments"): (
        "The staff branch of a booking in OPD mode (opd-16): patient.write or "
        "registration.write books an appointment for the patient named in the typed "
        "body (BookAppointmentRequest.patient_id), scoped through "
        "enforce_patient_scope(..., opd_arm=True); a departmented desk books only into "
        "its own clinic. Writes one appointments row, never the chart. With the 'opd' "
        "feature off the branch is the old patient.write one with the ordinary scope."
    ),
}

#: Whitelisted arm routes that name their patient in a TYPED BODY rather than
#: the path, and call enforce_patient_scope(..., opd_arm=True) on it in the
#: endpoint itself. The caller-named-patient sweep
#: (tests/test_caller_named_patient_scope.py) sees the body; each entry here is
#: checked to have a patient_id field on its body model and the call in its
#: own source.
BODY_NAMED_ARM_ROUTES = frozenset({("POST", "/api/appointments")})

#: Route modules that may carry the arm on a non-GET route (the design's
#: registration PATCH, OPD visit-scoped writes and the appointment staff branch).
NON_GET_ALLOWED_MODULES = frozenset({
    "routes.opd_routes", "routes.registration_routes", "routes.appointments_routes",
})

#: Source files allowed to pass opd_arm=True at all.
OPD_ARM_FILES = frozenset({
    "routes/opd_routes.py", "routes/registration_routes.py", "routes/appointments_routes.py",
})

#: Names that ARE the arm. Only permissions.py may use them.
ARM_NAMES = frozenset({
    "patient_in_opd_scope", "patient_in_opd_scope_blocking",
    "opd_scope_decision", "opd_scope_decision_blocking", "_opd_scope_visit",
})


def _dependency_calls(dependant):
    for dep in dependant.dependencies:
        yield dep.call
        yield from _dependency_calls(dep)


def _arm_routes(app=None):
    """(method, path) -> endpoint module for every live route carrying the arm."""
    if app is None:
        import main
        app = main.app

    found: dict[tuple[str, str], str] = {}
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        marked = any(getattr(call, "orb_opd_arm", False)
                     for call in _dependency_calls(route.dependant))
        if not marked:
            try:
                src = inspect.getsource(route.endpoint)
            except (OSError, TypeError):
                src = ""
            marked = "opd_arm=True" in src.replace(" ", "")
        if marked:
            for method in route.methods:
                found[(method, route.path)] = route.endpoint.__module__
    return found


#: The application's code: the top-level modules plus these packages. The
#: evaluation corpus, datasets, uploads and the virtualenv hold hundreds of
#: thousands of files and no route, so they are not walked.
_CODE_DIRS = ("routes", "services", "ws", "scripts", "migrations_his", "dev_tools")


def _python_sources():
    for path in sorted(SERVER.glob("*.py")):
        yield path.name, path
    for top in _CODE_DIRS:
        for path in sorted((SERVER / top).rglob("*.py")):
            if "__pycache__" not in path.parts:
                yield path.relative_to(SERVER).as_posix(), path


# ── 1. The mark itself ───────────────────────────────────────────────────────

def test_the_dependency_carries_the_mark_only_when_asked():
    from permissions import require_patient_access

    assert require_patient_access("patient.read_all").orb_opd_arm is False
    assert require_patient_access("patient.read_all", audit_action="x").orb_opd_arm is False
    assert require_patient_access("opd.consult", opd_arm=True).orb_opd_arm is True


def test_the_walk_finds_the_arm_in_a_dependency_and_in_a_body():
    """Prove the guard can fail: a throwaway app with a chart-shaped write that
    carries the arm both ways is found, and the write rule would refuse it."""
    from fastapi import Depends, FastAPI

    from permissions import enforce_patient_scope, require_patient_access

    probe = FastAPI()

    @probe.post("/probe/patients/{patient_id}/medications")
    def _dep_write(patient_id: str,
                   user: dict = Depends(require_patient_access("medication.write",
                                                               opd_arm=True))):
        return {}

    @probe.post("/probe/patients/{patient_id}/notes")
    def _body_write(patient_id: str):
        enforce_patient_scope({}, patient_id, opd_arm=True)
        return {}

    @probe.get("/probe/patients/{patient_id}")
    def _plain(patient_id: str,
               user: dict = Depends(require_patient_access("patient.read_all"))):
        return {}

    found = _arm_routes(probe)
    assert set(found) == {
        ("POST", "/probe/patients/{patient_id}/medications"),
        ("POST", "/probe/patients/{patient_id}/notes"),
    }
    assert all(mod not in NON_GET_ALLOWED_MODULES for mod in found.values())


# ── 2. The whitelist ─────────────────────────────────────────────────────────

def test_every_route_carrying_the_arm_is_whitelisted_with_a_reason():
    found = _arm_routes()
    assert found, "no route carries the OPD arm: the record view lost it or the walk broke"
    unlisted = sorted(set(found) - set(OPD_ARM_WHITELIST))
    assert not unlisted, (
        "these routes open the OUTPATIENT scope arm without a whitelist entry. Add "
        "each to OPD_ARM_WHITELIST with its reason, and never on a chart route:\n  "
        + "\n  ".join(f"{m} {p}" for m, p in unlisted))
    for key, reason in OPD_ARM_WHITELIST.items():
        assert len((reason or "").strip()) >= 40, f"{key} has no real reason written down"


def test_every_whitelisted_route_exists_and_still_carries_the_arm():
    found = _arm_routes()
    stale = sorted(set(OPD_ARM_WHITELIST) - set(found))
    assert not stale, (
        "whitelisted routes that do not exist or no longer carry the arm. A permit "
        "for a route that is not there is how a hole gets one:\n  "
        + "\n  ".join(f"{m} {p}" for m, p in stale))


def test_every_whitelisted_route_names_its_patient_in_the_path():
    for method, path in OPD_ARM_WHITELIST:
        if (method, path) in BODY_NAMED_ARM_ROUTES:
            continue
        assert "{patient_id}" in path, (
            f"{method} {path}: the arm scopes the patient named in the path; a route "
            "without one must use enforce_patient_scope(..., opd_arm=True) and say why")


def test_a_body_named_arm_route_scopes_the_body_patient_in_its_own_source():
    """The one exception to the path rule (the appointment staff branch,
    opd-16): its body model carries patient_id, and the endpoint itself calls
    enforce_patient_scope(..., opd_arm=True), so neither sweep loses it."""
    import main

    assert BODY_NAMED_ARM_ROUTES <= set(OPD_ARM_WHITELIST)
    for route in main.app.routes:
        if not isinstance(route, APIRoute):
            continue
        for method in route.methods:
            if (method, route.path) not in BODY_NAMED_ARM_ROUTES:
                continue
            src = inspect.getsource(route.endpoint).replace(" ", "")
            assert "enforce_patient_scope(user,patient_id,opd_arm=True)" in src, route.path
            assert route.body_field is not None, route.path
            model = route.body_field.type_
            assert "patient_id" in getattr(model, "model_fields", {}), route.path


def test_no_write_route_outside_the_outpatient_homes_carries_the_arm():
    bad = sorted(
        f"{m} {p} ({mod})" for (m, p), mod in _arm_routes().items()
        if m != "GET" and mod not in NON_GET_ALLOWED_MODULES
    )
    assert not bad, (
        "a non-GET route outside opd_routes, registration_routes and the appointment "
        "staff branch carries the OPD arm. Chart writes stay admission-only:\n  "
        + "\n  ".join(bad))


# ── 3. The predicate is reached only through the arm ─────────────────────────

def test_the_arm_predicate_is_used_only_by_permissions():
    offenders = []
    for rel, path in _python_sources():
        if rel == "permissions.py":
            continue
        tree = ast.parse(path.read_text(), filename=rel)
        for node in ast.walk(tree):
            name = None
            if isinstance(node, ast.Name):
                name = node.id
            elif isinstance(node, ast.Attribute):
                name = node.attr
            elif isinstance(node, ast.alias):
                name = node.name.split(".")[-1]
            if name in ARM_NAMES:
                offenders.append(f"{rel}:{getattr(node, 'lineno', '?')} {name}")
    assert not offenders, (
        "the OPD arm predicate is reached outside permissions.py. Go through "
        "require_patient_access(..., opd_arm=True) or enforce_patient_scope(..., "
        "opd_arm=True) on a whitelisted route instead:\n  " + "\n  ".join(offenders))


def test_opd_arm_true_appears_only_in_the_outpatient_route_files():
    offenders = []
    for rel, path in _python_sources():
        if rel == "permissions.py":
            continue
        tree = ast.parse(path.read_text(), filename=rel)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for kw in node.keywords:
                if kw.arg != "opd_arm":
                    continue
                if isinstance(kw.value, ast.Constant) and kw.value.value is False:
                    continue
                if rel not in OPD_ARM_FILES:
                    offenders.append(f"{rel}:{node.lineno}")
    assert not offenders, (
        "opd_arm passed (or passed a non-constant) outside opd_routes, "
        "registration_routes and appointments_routes:\n  " + "\n  ".join(offenders))


def test_the_shared_scope_and_the_sage_lane_are_untouched():
    """patient_in_scope, scope_patient_ids_sql and PATIENT_DEPARTMENT_SQL keep
    their admission-only shape, and Sage knows nothing of the arm."""
    import permissions

    assert "opd" not in inspect.getsource(permissions.patient_in_scope).lower()
    assert "opd" not in inspect.getsource(permissions.scope_patient_ids_sql).lower()
    assert "opd" not in permissions.PATIENT_DEPARTMENT_SQL.lower()
    for rel in ("services/sage_service.py", "routes/sage_routes.py"):
        src = (SERVER / rel).read_text()
        assert "opd_arm" not in src and "patient_in_opd_scope" not in src, rel
