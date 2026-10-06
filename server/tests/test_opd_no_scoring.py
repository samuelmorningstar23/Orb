"""Outpatients never score anything (opd-10, W4-F, 4 October 2026).

The OPD record, the queue and the consultation note show vitals a consulting
doctor typed, with their time, and nothing else: no NEWS2, no band, no
colour, no risk. Rulings OPD-S3 and OPD-S6 (applied) keep OPD vitals out of
the vitals and risk_scores tables, so they can never become a ward board's
latest reading or its score, and R023 (held) governs any automatic wording
about them.

This guard reads the SOURCE of every outpatient and registration module and
fails if one:
  * imports or calls compute_news2, score_vitals_row or any other scoring
    function of the Vigil or nurse-shift lanes, or imports those modules;
  * holds SQL that writes vitals, risk_scores or medications (INSERT, UPDATE,
    REPLACE or DELETE).

A file not yet written is skipped, except the two this increment owns, which
must exist. Behaviour is tested beside it in test_opd_consultation.py (no
vitals, risk_scores or medications row after a full consultation).
"""
from __future__ import annotations

import ast
import pathlib
import re

import pytest

SERVER = pathlib.Path(__file__).resolve().parent.parent

#: The outpatient and registration modules this guard covers.
GUARDED = (
    "services/opd_service.py",
    "routes/opd_routes.py",
    "services/opd_queue_service.py",
    "services/registration_service.py",
    "routes/registration_routes.py",
    "services/patient_matching.py",
    "services/clinic_schedule_service.py",
)
MUST_EXIST = {"services/opd_service.py", "routes/opd_routes.py",
              "services/opd_queue_service.py"}

#: Scoring functions anywhere in the server. Importing or calling any is a fail.
SCORING_NAMES = frozenset({
    "compute_news2", "score_vitals_row", "compute_mews", "compute_cfc_risk",
    "score_observation", "compute_patient_state", "compute_ward_state",
    "news2_reason_line", "scored_admitted_patients",
})

#: Modules whose import alone brings the scoring lanes in.
SCORING_MODULES = frozenset({
    "services.vigil_service", "services.nurse_shift_service",
    "services.world_model_service", "services.news2_text",
})

_WRITE_SQL = re.compile(
    r"\b(?:INSERT(?:\s+OR\s+\w+)?\s+INTO|REPLACE\s+INTO|UPDATE|DELETE\s+FROM)\s+"
    r"[\"'`\[]?(vitals|risk_scores|medications)\b",
    re.IGNORECASE,
)


def _tree(rel: str):
    path = SERVER / rel
    if not path.exists():
        if rel in MUST_EXIST:
            pytest.fail(f"{rel} is missing")
        pytest.skip(f"{rel} is not written yet")
    return ast.parse(path.read_text(), filename=rel)


def _string_constants(tree) -> list[str]:
    """Every string literal, and the literal parts of every f-string, except
    docstrings (prose may say 'never written to vitals')."""
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                docstrings.add(id(body[0].value))
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                and id(node) not in docstrings:
            out.append(node.value)
    return out


@pytest.mark.parametrize("rel", GUARDED)
def test_no_scoring_function_is_imported_or_called(rel):
    tree = _tree(rel)
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module in SCORING_MODULES:
                found.append(f"from {module} import ...")
            for alias in node.names:
                if alias.name in SCORING_NAMES:
                    found.append(f"imports {alias.name}")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in SCORING_MODULES:
                    found.append(f"import {alias.name}")
        elif isinstance(node, ast.Call):
            func = node.func
            name = func.id if isinstance(func, ast.Name) else (
                func.attr if isinstance(func, ast.Attribute) else None)
            if name in SCORING_NAMES:
                found.append(f"calls {name}")
        elif isinstance(node, ast.Attribute) and node.attr in SCORING_NAMES:
            found.append(f"reaches {node.attr}")
    assert not found, f"{rel} touches a scoring lane: {sorted(set(found))}"


@pytest.mark.parametrize("rel", GUARDED)
def test_no_sql_writes_vitals_risk_scores_or_medications(rel):
    tree = _tree(rel)
    hits = [m.group(0) for s in _string_constants(tree) for m in _WRITE_SQL.finditer(s)]
    assert not hits, f"{rel} writes a table OPD must never write: {hits}"


def test_the_guard_sees_what_it_is_meant_to_see():
    """The detectors themselves: a planted import, call and write are caught,
    and the words in a docstring are not."""
    planted = ast.parse(
        '"""OPD vitals are never written to vitals or risk_scores."""\n'
        "from services.vigil_service import compute_news2\n"
        "def f(db):\n"
        "    '''Nothing here INSERTs INTO vitals.'''\n"
        "    compute_news2(1, 2, 3, 4, 5)\n"
        "    db.execute(\"INSERT INTO risk_scores (patient_id) VALUES (?)\", ('p',))\n"
        "    db.execute(f\"UPDATE medications SET status = 'x' WHERE id = {1}\")\n"
    )
    names = {n.attr if isinstance(n, ast.Attribute) else getattr(n, "id", None)
             for n in ast.walk(planted) if isinstance(n, (ast.Name, ast.Attribute))}
    assert "compute_news2" in names
    hits = [m.group(1) for s in _string_constants(planted) for m in _WRITE_SQL.finditer(s)]
    assert hits == ["risk_scores", "medications"]
