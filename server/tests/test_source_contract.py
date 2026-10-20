"""Named defaults must preserve the guard inventory without inventing guards."""
import ast

from fastapi import Body, Depends
from tests.source_contract import contract_source


def _patient_guard():
    return {}


def _admin_guard():
    return {}


_PATIENT_USER_DEFAULT = Depends(_patient_guard)
_ADMIN_USER_DEFAULT = Depends(_admin_guard)
_BODY_DEFAULT = Body(None)


def _guarded(user=_PATIENT_USER_DEFAULT):
    return user


def _wrong_guard(user=_ADMIN_USER_DEFAULT):
    return user


def _unguarded(user=_BODY_DEFAULT):
    """The unused _PATIENT_USER_DEFAULT global is not this endpoint's guard."""
    return user


def test_named_default_retains_the_actual_guard_and_function_identity():
    source = contract_source(_guarded)
    assert "Depends(_patient_guard)" in source
    assert "_PATIENT_USER_DEFAULT" not in source
    assert ast.parse(source).body[0].name == _guarded.__name__


def test_wrong_guard_cannot_satisfy_the_patient_guard_inventory():
    source = contract_source(_wrong_guard)
    assert "Depends(_admin_guard)" in source
    assert "Depends(_patient_guard)" not in source


def test_unused_guard_and_documentation_cannot_invent_authorization():
    source = contract_source(_unguarded)
    assert "Body(None)" in source
    assert "Depends(_patient_guard)" not in source
    assert "unused _PATIENT_USER_DEFAULT global" in source
