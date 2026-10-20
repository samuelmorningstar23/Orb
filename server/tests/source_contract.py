"""Inspect route contracts through named FastAPI dependency defaults.

Only Name loads referring to module-level *_DEFAULT bindings are expanded.
Permission calls and arguments stay intact; source identity checks still see
exactly the original function and decorators.
"""
from __future__ import annotations

import ast
import inspect
import textwrap
from pathlib import Path
from typing import Any


def contract_source(target: Any) -> str:
    source = textwrap.dedent(inspect.getsource(target))
    module = target if inspect.ismodule(target) else inspect.getmodule(target)
    if module is None or not getattr(module, "__file__", None):
        return source
    module_source = Path(module.__file__).read_text()
    bindings = {
        node.targets[0].id: ast.get_source_segment(module_source, node.value)
        for node in ast.parse(module_source).body
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id.endswith("_DEFAULT")
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
        and node.value.func.id in {"Depends", "Body", "Query", "File", "Form"}
    }
    lines = source.split("\n")
    offsets = []
    offset = 0
    for line in lines:
        offsets.append(offset)
        offset += len(line) + 1
    replacements = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Name) or not isinstance(node.ctx, ast.Load):
            continue
        replacement = bindings.get(node.id)
        if replacement is None:
            continue
        start = offsets[node.lineno - 1] + len(lines[node.lineno - 1].encode()[:node.col_offset].decode())
        end = offsets[node.end_lineno - 1] + len(lines[node.end_lineno - 1].encode()[:node.end_col_offset].decode())
        replacements.append((start, end, replacement))
    for start, end, replacement in sorted(replacements, reverse=True):
        source = source[:start] + replacement + source[end:]
    return source
