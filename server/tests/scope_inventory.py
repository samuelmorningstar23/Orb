"""Narrow static recognition of reviewed route-to-service patient scope contracts.

This supplements the inventory; live domain denial/replay tests prove behavior.
"""
import ast
import inspect
import textwrap
import typing
from types import ModuleType

from pydantic import BaseModel


def _resolved_callable(node, namespace):
    """Resolve declared function/module globals without invoking descriptors."""
    if isinstance(node, ast.Name):
        return namespace.get(node.id)
    if isinstance(node, ast.Attribute):
        owner = _resolved_callable(node.value, namespace)
        if isinstance(owner, ModuleType):
            return vars(owner).get(node.attr)
    return None


def _calls_exact(function, *required):
    """Guard paths must be executable calls to the reviewed functions, not text."""
    try:
        tree = ast.parse(textwrap.dedent(inspect.getsource(function)))
        namespace = function.__globals__
    except (OSError, TypeError, AttributeError, SyntaxError):
        return False
    calls = [_resolved_callable(node.func, namespace) for node in ast.walk(tree)
             if isinstance(node, ast.Call)]
    return all(any(call is target for call in calls) for target in required)


def _reviewed_scope_delegates(*, mutation):
    """Explicit service contracts reviewed alongside their live scope probes.

    This is not a route exemption: the endpoint must only forward its typed
    body and authenticated user, and the real helper chain must still call the
    canonical patient guards. As with the original source inventory, presence
    cannot prove control flow; the domain tests exercise denial, replay and
    transaction behavior. Do not replace these contracts with generic names
    such as ``create``, ``call`` or ``patient_on``.
    """
    from permissions import patient_in_scope, scope_patient_ids_sql
    from routes.local_operations_http import call
    from routes.record_governance_routes import call as governance_call
    from services import (feedback_service, infection_surveillance_service,
                          local_operations_common as common, mortuary_service,
                          offline_clinical_records as clinical,
                          offline_workflows as workflows, record_governance_service)

    contracts = {}
    if not mutation:
        if (_calls_exact(clinical.list_records, clinical._user, clinical._scope)
                and _calls_exact(clinical._scope, scope_patient_ids_sql)):
            contracts[clinical.list_records] = (None, 2, 1)
        if (_calls_exact(record_governance_service.list_records,
                         record_governance_service._scope, scope_patient_ids_sql,
                         workflows.require_patient)
                and _calls_exact(record_governance_service._scope, workflows.scope_for)):
            contracts[record_governance_service.list_records] = (governance_call, "patient_id", "actor")
        return contracts
    if (_calls_exact(clinical.create, clinical._user, clinical._links)
            and _calls_exact(clinical._links, clinical._allowed)
            and _calls_exact(clinical._allowed, patient_in_scope)):
        contracts[clinical.create] = (None, 1, 2)
    if _calls_exact(common.patient_on, workflows.scope_for, workflows.require_patient):
        for function, body_index in ((feedback_service.register, 0),
                                     (mortuary_service.intake, 0),
                                     (infection_surveillance_service.create, 1)):
            if _calls_exact(function, common.actor_on, common.patient_on):
                contracts[function] = (call, body_index, "actor")
    if (_calls_exact(record_governance_service.create, record_governance_service._scope,
                     workflows.require_patient)
            and _calls_exact(record_governance_service._scope, workflows.scope_for)):
        contracts[record_governance_service.create] = (governance_call, 1, "actor")
    return contracts


def _is_scoped_service_delegation(endpoint, source, *, mutation=True):
    """Recognize only a single return of an exact reviewed service delegation."""
    import permissions
    from auth import get_current_user

    try:
        signature = inspect.signature(endpoint)
        dependency = signature.parameters["user"].default.dependency
        # Recognize the real permission dependency, which authenticates first.
        if (dependency.__globals__ is not vars(permissions)
                or dependency.__qualname__ != "require_permission.<locals>._checker"
                or inspect.signature(dependency).parameters["user"].default.dependency
                is not get_current_user):
            return False
        if mutation:
            body_type = typing.get_type_hints(endpoint)["body"]
            if not issubclass(body_type, BaseModel) or "patient_id" not in body_type.model_fields:
                return False
        elif "patient_id" not in signature.parameters:
            return False
        tree = ast.parse(textwrap.dedent(source))
        function, = tree.body
        if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return False
        statements = function.body
        if (statements and isinstance(statements[0], ast.Expr)
                and isinstance(statements[0].value, ast.Constant)
                and isinstance(statements[0].value.value, str)):
            statements = statements[1:]
        statement, = statements
        if not isinstance(statement, ast.Return) or not isinstance(statement.value, ast.Call):
            return False
        invocation = statement.value
        target = _resolved_callable(invocation.func, endpoint.__globals__)
        contracts = _reviewed_scope_delegates(mutation=mutation)
        arguments = invocation.args
        if target not in contracts:
            if not arguments:
                return False
            delegated = _resolved_callable(arguments[0], endpoint.__globals__)
            contract = contracts.get(delegated)
            if contract is None or contract[0] is not target:
                return False
            target, arguments = delegated, arguments[1:]
        wrapper, body_index, actor_index = contracts[target]
        if wrapper is None and _resolved_callable(invocation.func, endpoint.__globals__) is not target:
            return False

        def name(node, expected):
            return isinstance(node, ast.Name) and node.id == expected

        def body_dump(node):
            return (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and name(node.func.value, "body") and node.func.attr == "model_dump"
                    and not node.args and not node.keywords)

        keywords = {item.arg: item.value for item in invocation.keywords}
        body = arguments[body_index] if isinstance(body_index, int) else keywords.get(body_index)
        if mutation:
            if not (name(body, "body") if wrapper is None else body_dump(body)):
                return False
        elif not name(body, "patient_id"):
            return False
        actor = arguments[actor_index] if isinstance(actor_index, int) else keywords.get(actor_index)
        if not name(actor, "user"):
            return False
        # Even argument expressions can do unguarded work before the service
        # runs. Only parameter forwarding, constants and the body dump qualify.
        values = [*arguments, *keywords.values()]
        return all(isinstance(value, ast.Constant)
                   or (isinstance(value, ast.Name) and value.id in signature.parameters)
                   or body_dump(value) for value in values) and None not in keywords
    except (KeyError, IndexError, ValueError, TypeError, AttributeError, SyntaxError):
        return False


