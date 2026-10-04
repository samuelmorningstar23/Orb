"""Fail-closed audit categories must be real names, and must actually fail closed.

Found 2026-09-03: services/audit_service._FAIL_CLOSED_CATEGORIES named "auth"
and "ai_egress", but no call site under server/ ever emitted either string.
Logins are written as "authentication" and the per-inference AI record as
"ai_inference", so a failed audit write for a login or an AI call was fail-open
in practice while the set claimed otherwise. Two guards live here:

1. Every name in the set is emitted by at least one real call site under
   server/ (an AST scan, tests excluded). A category renamed at its call sites
   without the set following goes red here instead of silently going dead.
2. A persistence failure under "authentication" (and every other fail-closed
   name) raises AuditWriteError rather than being swallowed, while a fail-soft
   category under the identical failure still returns quietly and logs.
"""
from __future__ import annotations

import ast
import logging
import sqlite3
from collections import defaultdict
from pathlib import Path

import pytest

from db_backend import DBError
from services.audit_service import (
    _FAIL_CLOSED_CATEGORIES,
    AuditWriteError,
    log_audit_event,
)

SERVER_ROOT = Path(__file__).resolve().parents[1]

# Not product code: the suite itself, virtualenvs, bytecode caches, vendored JS.
_EXCLUDED_DIRS = {"tests", ".venv", "venv", "node_modules", "__pycache__"}

# log_audit_event takes event_category=; the per-route _audit wrappers take
# category= and forward it.
_CATEGORY_KEYWORDS = ("event_category", "category")


def _is_str(node) -> bool:
    return isinstance(node, ast.Constant) and isinstance(node.value, str)


def emitted_categories(root: Path = SERVER_ROOT) -> dict[str, list[str]]:
    """Map each literal audit category to the source sites that emit it.

    A site counts when a call passes ``event_category=`` or ``category=`` as a
    string literal, or when a function declares a string default for a
    parameter of that name (the wrappers' default categories). Comments and
    docstrings do not count: the two dead names lived on in both for weeks,
    which is exactly why a plain grep was not enough.
    """
    found: dict[str, list[str]] = defaultdict(list)
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root)
        if _EXCLUDED_DIRS.intersection(rel.parts):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                for kw in node.keywords:
                    if kw.arg in _CATEGORY_KEYWORDS and _is_str(kw.value):
                        found[kw.value.value].append(f"{rel}:{node.lineno}")
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                a = node.args
                positional = a.posonlyargs + a.args
                with_defaults = positional[len(positional) - len(a.defaults):]
                for arg, default in zip(with_defaults, a.defaults):
                    if arg.arg in _CATEGORY_KEYWORDS and _is_str(default):
                        found[default.value].append(f"{rel}:{node.lineno}")
                for arg, default in zip(a.kwonlyargs, a.kw_defaults):
                    if arg.arg in _CATEGORY_KEYWORDS and _is_str(default):
                        found[default.value].append(f"{rel}:{node.lineno}")
    return dict(found)


# ── 1. The set may only name categories that something really emits ──────────

# Registered ahead of their first writer by the hospital-system build's wave 1
# (3 October 2026), so each epic's first audited write is fail-closed from the
# start. Each maps to the increment that will emit it. When that writer lands
# its name may be deleted here (the guard below then covers it like any other);
# this list may only shrink. A name that is still unemitted after its increment
# shipped is the dead-guard failure this file exists for, so check it then.
_PRE_REGISTERED = {
    "patient_identity": "opd-4 registration_service (W4-A)",
    # patient_movement left this list with its writer: services/adt/core.py
    # (adt-3, W3-B, 4 October 2026).
}


def test_every_fail_closed_category_is_emitted_by_a_real_call_site():
    emitted = emitted_categories()
    dead = sorted(_FAIL_CLOSED_CATEGORIES - emitted.keys() - set(_PRE_REGISTERED))
    assert not dead, (
        f"fail-closed categories that no call site under server/ emits: {dead}. "
        "A name here that nothing writes guards nothing: the events it was meant "
        f"to protect are fail-open. Emitted categories: {sorted(emitted)}"
    )


def test_pre_registered_categories_only_ever_shrink():
    # Pinned to the three wave-1 names, so the exemption above can lose a name
    # as its writer lands but can never grow to excuse a typo or a new name.
    assert set(_PRE_REGISTERED) <= {"patient_identity", "patient_movement", "clinical_config"}
    assert set(_PRE_REGISTERED) <= _FAIL_CLOSED_CATEGORIES


def test_the_three_his_categories_are_fail_closed():
    assert {"patient_identity", "patient_movement", "clinical_config"} <= _FAIL_CLOSED_CATEGORIES


def test_the_two_misnamed_guards_now_name_what_is_emitted():
    # The 2026-09-03 finding, pinned: logins and AI inference are fail-closed
    # under the names their call sites use, alongside the three that were
    # already right.
    assert {"authentication", "ai_inference"} <= _FAIL_CLOSED_CATEGORIES
    assert {"security", "clinical_access", "phi_access"} <= _FAIL_CLOSED_CATEGORIES


# ── 2. A fail-closed write failure raises; a fail-soft one is logged ─────────

def _broken_db() -> sqlite3.Connection:
    """A live sqlite handle with no audit_logs table.

    Passed as ``db=`` it takes the caller-owned-transaction path, so the INSERT
    fails through the real driver exactly as a missing or unreadable audit
    table fails, without touching the shared test database.
    """
    return sqlite3.connect(":memory:")


def test_authentication_audit_write_failure_raises_instead_of_being_swallowed():
    db = _broken_db()
    try:
        with pytest.raises(AuditWriteError) as exc:
            log_audit_event(
                correlation_id="fc-authentication-login",
                event_category="authentication",
                action_type="login_success",
                outcome="success",
                db=db,
                actor_user_id="DOC001",
                actor_role="doctor",
                summary_message="probe: audit outage during login",
            )
    finally:
        db.close()
    assert "authentication/login_success" in str(exc.value)
    # The underlying driver error is chained, not replaced, so the CRITICAL log
    # and the traceback both say why the write failed.
    assert isinstance(exc.value.__cause__, DBError)


@pytest.mark.parametrize("category", sorted(_FAIL_CLOSED_CATEGORIES))
def test_each_fail_closed_category_raises_on_write_failure(category):
    db = _broken_db()
    try:
        with pytest.raises(AuditWriteError):
            log_audit_event(
                correlation_id=f"fc-{category}",
                event_category=category,
                action_type="probe",
                outcome="success",
                db=db,
            )
    finally:
        db.close()


@pytest.mark.parametrize("category", ["financial", "facilities", "api_mutation"])
def test_fail_soft_category_logs_and_returns_on_the_same_failure(category, caplog):
    db = _broken_db()
    try:
        with caplog.at_level(logging.CRITICAL, logger="orb.audit"):
            result = log_audit_event(
                correlation_id=f"fs-{category}",
                event_category=category,
                action_type="probe",
                outcome="success",
                db=db,
            )
    finally:
        db.close()
    assert result is None
    assert any("Audit Log Failure" in r.getMessage() for r in caplog.records), (
        "a fail-soft audit failure must still be logged at CRITICAL"
    )
