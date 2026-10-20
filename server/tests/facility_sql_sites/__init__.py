"""The SQL-site registry for multi-site isolation (multisite-5b, wave 11, 20 October 2026).

Why it exists. Orb keeps two facilities' patients apart with row-level facility
ids (MS-R1 (a)). A statement that lists patients, admissions, visits or money
without asking which facility the caller works at shows one facility's
patients to the other the moment a second facilities row exists. The runtime
fence (server/facility_fence.py) answers 503 on every route group that still
reaches such a statement. This registry is how the fence's list is made, and
the ratchet that keeps it honest while the code changes.

What counts as a SQL site: a string literal (an f-string's literal parts, and
a '+' or '%' chain of literals, included) that names one of TABLES after FROM,
JOIN, UPDATE, INTO, TABLE or EXISTS, in any Python module under server/ except
the tests and the data folders (SKIP_PARTS). Docstrings are not sites. A site
belongs to the function, method or module-level constant it is written in
(qualname 'Class.method', 'outer.inner' or the constant's name), or to
'<module>' for other module-level code. A statement whose table name is built
at run time (f"FROM {table}") is invisible to the scan; the leak sweep is the
net for those.

Every owner of a site has one row, in the area file of the sweep that will
make it facility-aware (clinical, sage, audit, exports, money, admin, opd_adt,
lab_meds, pharmacy), so each later cluster edits only its own file. A row is
one of five kinds:

  facility        the statement limits its rows to a facility, or is the
                  scope and attribution machinery itself (permissions.py,
                  facility_service, facility_identity).
  patient_keyed   keyed on one patient (or the signed-in patient's own
                  record), and every route that reaches it checks that
                  patient through a facility-aware helper first
                  (SCOPE_HELPERS). The registry test checks the second half
                  on the call graph.
  document_keyed  keyed on one document (an invoice, a payment, an order)
                  whose own facility, or whose patient's scope, the caller is
                  checked against before anything is shown.
  appliance       never answers one person's request with another facility's
                  rows: schema and migrations, console scripts, writers that
                  disclose nothing, counts that name no patient. The row says
                  why.
  blind           not facility-aware yet. Every route that reaches it must
                  stay behind the fence until its owning increment sweeps it.
                  In wave 11 this also covers keyed statements no sweep has
                  checked: a sweep moves a row to a safe kind only after
                  checking every lane that reaches it.

The safe kinds (facility, patient_keyed, document_keyed) pin the tables their
owner touches and how many sites it holds, so a new statement in a function
someone judged safe fails until it is judged again. Blind and appliance rows
pin only their existence: a new statement in a blind function is still behind
the fence. A whole-module or whole-folder row (qualname '*', a folder ending
in '/') is allowed for appliance rows only (migrations, console scripts).

Route groups. SURFACES (split by area too) classes every route prefix the
application serves under the fence's roots: 'blind' (fenced while more than
one facility is on record; the increment that clears it), 'cleared' (swept:
it keeps each facility's patients apart) or 'no_patient_data' (it carries no
patient data at all). facility_fence's FACILITY_BLIND_SURFACES,
FACILITY_CLEARED_SURFACES and NO_PATIENT_DATA_SURFACES are generated from
these rows (`python -m tests.facility_sql_sites` prints them) and
tests/test_facility_sql_registry.py pins them equal. Routes outside the
fence's roots are listed in UNFENCED with the reason they carry no patient.

The call graph. CallGraph resolves every name and attribute reference in the
server's own code (calls, functions handed to run_db, asyncio.to_thread or
Depends, constants) through each module's imports, so the registry test can
follow each route and socket handler to the SQL owners it reaches and check
that one reaching a blind or unregistered owner is fenced. A reference it
cannot resolve (a call on an instance, getattr) is missed, so the graph can
prove a route unsafe but never safe: a group is cleared by a person, with a
reason, and the leak sweep (tests/test_cross_facility_leak_sweep.py) checks it
strictly.
"""
from __future__ import annotations

import ast
import importlib
import os
import re
from collections import deque
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Callable, Iterable, Optional

SERVER = Path(__file__).resolve().parents[2]

#: The tables whose SQL sites the registry classifies (the design's list).
TABLES = ("patients", "admissions", "opd_visits", "appointments", "surgeries",
          "orders", "medications", "invoices", "payments", "audit_logs")

SITE_SQL = re.compile(
    r"\b(?:FROM|JOIN|UPDATE|INTO|TABLE|EXISTS)\s+[\"`\[]?(%s)\b" % "|".join(TABLES),
    re.IGNORECASE)

#: Folders that hold no server code, and the tests themselves.
SKIP_PARTS = frozenset({"venv", "tests", "__pycache__", "node_modules", "datasets", "eval",
                        "corpus", "uploads", "docs", "downtime_exports"})

AREAS = ("clinical", "sage", "audit", "exports", "money", "admin", "opd_adt", "lab_meds",
         "pharmacy")

FACILITY = "facility"
PATIENT_KEYED = "patient_keyed"
DOCUMENT_KEYED = "document_keyed"
APPLIANCE = "appliance"
BLIND = "blind"
KINDS = (FACILITY, PATIENT_KEYED, DOCUMENT_KEYED, APPLIANCE, BLIND)
SAFE_KINDS = frozenset({FACILITY, PATIENT_KEYED, DOCUMENT_KEYED})

CLEARED = "cleared"
NO_PATIENT_DATA = "no_patient_data"
SURFACE_KINDS = (BLIND, CLEARED, NO_PATIENT_DATA)

#: The increments that own blind rows and blind route groups, and the wave
#: cluster each is planned in (docs/HIS_BUILD_PLAN_2026-10.md, waves 12 and 13).
OWNERS = {
    "multisite-8": "W12-A clinical lanes",
    "multisite-10": "W12-B Sage lane, out-of-scope locator and socket rooms",
    "multisite-11": "W12-C audit attribution and every audit reader",
    "multisite-15": "W12-D downtime packs, exports and backups per facility",
    "multisite-9a": "W12-E pharmacy queue, money and front desk",
    "multisite-17": "W12-E group-wide identity match for the desk",
    "multisite-8h1": "W12-F OPD and ADT lanes",
    "multisite-9b": "W13-A remaining administrative lanes",
    "multisite-12": "W13-A Command Center, capacity and NABH per facility",
    "multisite-13": "W13-B price lists per facility",
    "multisite-14": "W13-C pharmacy stock per facility",
    "multisite-8h3": "W13-C pharmacy store lanes",
    "multisite-8h2": "W13-D LIS and medication-order lanes",
    "multisite-19": "W13-E visiting consultants",
}

#: The facility-aware scope helpers a patient_keyed owner's routes must call
#: (require_patient_access and enforce_patient_scope apply facility scope since
#: multisite-5a; require_patient_self binds a portal login to its own record).
#: patient_in_scope and scope_patient_ids_sql are not here: without
#: facilities= they check the department only.
SCOPE_HELPERS = frozenset({
    ("permissions.py", "require_patient_access"),
    ("permissions.py", "enforce_patient_scope"),
    ("permissions.py", "require_patient_self"),
    ("permissions.py", "facility_denial"),
    ("permissions.py", "_facility_denial_blocking"),
})

MODULE = "<module>"
WHOLE = "*"

#: The fence's roots, as facility_fence names them.
FENCED_ROOTS = ("/api", "/uploads", "/socket.io")


# ═══════════════════════════════════════════════════════════════════════════
#  Rows
# ═══════════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class Site:
    """One registry row: how every SQL site in one owner is classed."""
    kind: str
    why: str = ""
    owner: Optional[str] = None
    tables: Optional[tuple] = None
    count: Optional[int] = None


def blind(owner: str, why: str = "") -> Site:
    return Site(BLIND, why, owner=owner)


def appliance(why: str) -> Site:
    return Site(APPLIANCE, why)


def facility(tables: Iterable[str], count: int, why: str) -> Site:
    return Site(FACILITY, why, tables=tuple(sorted(tables)), count=count)


def patient_keyed(tables: Iterable[str], count: int, why: str) -> Site:
    return Site(PATIENT_KEYED, why, tables=tuple(sorted(tables)), count=count)


def document_keyed(tables: Iterable[str], count: int, why: str) -> Site:
    return Site(DOCUMENT_KEYED, why, tables=tuple(sorted(tables)), count=count)


@dataclass(frozen=True)
class Surface:
    """One route group: a path prefix the fence holds shut or lets through."""
    kind: str
    why: str
    owner: Optional[str] = None


def blind_surface(owner: str, why: str = "") -> Surface:
    return Surface(BLIND, why, owner=owner)


def cleared_surface(why: str) -> Surface:
    return Surface(CLEARED, why)


def no_patient_data_surface(why: str) -> Surface:
    return Surface(NO_PATIENT_DATA, why)


# ═══════════════════════════════════════════════════════════════════════════
#  The scan
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class Found:
    module: str
    qualname: str
    tables: tuple
    count: int
    lines: tuple


def module_files(root: Path = SERVER) -> list:
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_PARTS and not d.startswith("."))
        out.extend(Path(dirpath) / name for name in sorted(filenames) if name.endswith(".py"))
    return out


def rel(path: Path, root: Path = SERVER) -> str:
    return path.relative_to(root).as_posix()


def string_text(node) -> Optional[str]:
    """The literal text of a string expression, with '{}' where an f-string
    or a '+' chain holds something computed; None for anything else."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(v.value if isinstance(v, ast.Constant) and isinstance(v.value, str)
                       else "{}" for v in node.values)
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Mod)):
        left = string_text(node.left)
        right = string_text(node.right) if isinstance(node.op, ast.Add) else ""
        if left is None and right is None:
            return None
        return (left if left is not None else "{}") + (right if right is not None else "{}")
    return None


def site_tables(text: str) -> tuple:
    return tuple(sorted({m.group(1).lower() for m in SITE_SQL.finditer(text)}))


def _docstring_ids(tree) -> set:
    ids = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if (body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                ids.add(id(body[0].value))
    return ids


_DEFS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
_COMPOUND = (ast.If, ast.Try, ast.With, ast.AsyncWith, ast.For, ast.AsyncFor, ast.While) + \
    ((ast.TryStar,) if hasattr(ast, "TryStar") else ())


def _scope_statements(body):
    """The statements of one scope, looking inside if, try, with and loops
    (which open no scope) but not inside definitions."""
    for stmt in body:
        yield stmt
        if isinstance(stmt, _COMPOUND):
            for field in ("body", "orelse", "finalbody"):
                yield from _scope_statements(getattr(stmt, field, None) or [])
            for handler in getattr(stmt, "handlers", None) or []:
                yield from _scope_statements(handler.body)


def _named_target(stmt) -> Optional[str]:
    """The name a module- or class-level assignment binds, when it binds one."""
    if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name):
        return stmt.targets[0].id
    if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name) and stmt.value is not None:
        return stmt.target.id
    return None


class _Scanner:
    """Walks one module and attributes each matching string to its owner.
    `match` turns a string's text into the tags it carries (the tables it
    names, for the registry); a string with no tags is not a site."""

    def __init__(self, module: str, tree, match: Callable[[str], tuple] = site_tables):
        self.module = module
        self.match = match
        self.docs = _docstring_ids(tree)
        self.found: dict = {}
        self._body(tree.body, [])

    def _add(self, owner: str, node, text: str) -> None:
        tags = self.match(text)
        if tags:
            self.found.setdefault(owner, []).append((node.lineno, tags))

    def _strings(self, node, owner: str, stack: list) -> None:
        """Every outermost string expression under node, minus docstrings;
        a nested definition is its own owner."""
        todo = [node]
        while todo:
            cur = todo.pop()
            if isinstance(cur, _DEFS):
                self._def(cur, stack)
                continue
            if id(cur) in self.docs:
                continue
            if isinstance(cur, (ast.Constant, ast.JoinedStr, ast.BinOp)):
                text = string_text(cur)
                if text is not None:
                    self._add(owner, cur, text)
                    if isinstance(cur, ast.BinOp):
                        # The chain is one site; its computed parts may hold calls.
                        todo.extend(c for c in ast.iter_child_nodes(cur) if string_text(c) is None)
                    continue
            todo.extend(ast.iter_child_nodes(cur))

    def _def(self, node, stack: list) -> None:
        inner = stack + [node.name]
        for dec in node.decorator_list:
            self._strings(dec, ".".join(stack) or MODULE, stack)
        if isinstance(node, ast.ClassDef):
            self._body(node.body, inner)
            return
        args = node.args
        for default in list(args.defaults) + [d for d in args.kw_defaults if d is not None]:
            self._strings(default, ".".join(inner), stack)
        for stmt in node.body:
            self._strings(stmt, ".".join(inner), inner)

    def _body(self, body, stack: list) -> None:
        """A module or class body: a named assignment owns its strings."""
        here = ".".join(stack) or MODULE
        for stmt in body:
            if isinstance(stmt, _DEFS):
                self._def(stmt, stack)
            elif isinstance(stmt, _COMPOUND):
                # if, try, with and loops open no scope: their own expressions
                # belong here, their bodies are read like this one.
                for field in ("test", "iter", "target"):
                    if getattr(stmt, field, None) is not None:
                        self._strings(getattr(stmt, field), here, stack)
                for item in getattr(stmt, "items", None) or []:
                    self._strings(item, here, stack)
                for field in ("body", "orelse", "finalbody"):
                    self._body(getattr(stmt, field, None) or [], stack)
                for handler in getattr(stmt, "handlers", None) or []:
                    if handler.type is not None:
                        self._strings(handler.type, here, stack)
                    self._body(handler.body, stack)
            else:
                name = _named_target(stmt)
                self._strings(stmt, ".".join(stack + [name]) if name else here, stack)


def _parse(path: Path, module: str):
    try:
        return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError as e:  # a module the interpreter cannot load either
        raise AssertionError(f"{module} does not parse: {e}") from e


def scan(root: Path = SERVER, match: Callable[[str], tuple] = site_tables) -> dict:
    """{(module path, qualname): Found} for every owner of a matching string."""
    out: dict = {}
    for path in module_files(root):
        module = rel(path, root)
        for owner, hits in _Scanner(module, _parse(path, module), match).found.items():
            tags = tuple(sorted({t for _, ts in hits for t in ts}))
            out[(module, owner)] = Found(module, owner, tags, len(hits),
                                         tuple(sorted(line for line, _ in hits)))
    return out


# ═══════════════════════════════════════════════════════════════════════════
#  The registry
# ═══════════════════════════════════════════════════════════════════════════

def _areas():
    for name in AREAS:
        yield name, importlib.import_module(f"{__name__}.{name}")


def _merged(attr: str) -> dict:
    rows: dict = {}
    where: dict = {}
    for name, mod in _areas():
        for key, row in getattr(mod, attr, {}).items():
            if key in rows:
                raise AssertionError(f"{key!r} is in {attr} of both {where[key]} and {name}")
            rows[key] = row
            where[key] = name
    return rows


def registry() -> dict:
    """Every area's SITES merged; a key in two areas is an error."""
    return _merged("SITES")


def surfaces() -> dict:
    """Every area's SURFACES merged: {path prefix: Surface}."""
    return _merged("SURFACES")


def unfenced() -> dict:
    """Routes outside the fence's roots: {path: why it carries no patient}."""
    return _merged("UNFENCED")


def row_areas() -> dict:
    return {key: name for name, mod in _areas() for key in mod.SITES}


def blind_prefixes() -> tuple:
    return tuple(sorted(p for p, s in surfaces().items() if s.kind == BLIND))


def cleared_prefixes() -> tuple:
    return tuple(sorted(p for p, s in surfaces().items() if s.kind in (CLEARED, NO_PATIENT_DATA)))


def no_patient_data_prefixes() -> tuple:
    return tuple(sorted(p for p, s in surfaces().items() if s.kind == NO_PATIENT_DATA))


def row_for(rows: dict, module: str, qualname: str):
    """(key, row) covering this owner: its own row, else a whole-module row,
    else a whole-folder row ('scripts/', '*'); (None, None) when none does."""
    for key in ((module, qualname), (module, WHOLE)):
        if key in rows:
            return key, rows[key]
    parts = module.split("/")
    for i in range(len(parts) - 1, 0, -1):
        key = ("/".join(parts[:i]) + "/", WHOLE)
        if key in rows:
            return key, rows[key]
    return None, None


def ratchet(found: Optional[dict] = None, rows: Optional[dict] = None) -> list:
    """Every way the registry and the code disagree, in words."""
    found = scan() if found is None else found
    rows = registry() if rows is None else rows
    problems: list = []
    used: set = set()
    for key in sorted(found):
        f = found[key]
        rkey, row = row_for(rows, f.module, f.qualname)
        if row is None:
            problems.append(
                f"unregistered: {f.module} {f.qualname} touches {', '.join(f.tables)} "
                f"({f.count} site(s), line(s) {', '.join(map(str, f.lines))}). Classify it in "
                f"tests/facility_sql_sites/<area>.py.")
            continue
        used.add(rkey)
        if row.kind in SAFE_KINDS and (row.tables != f.tables or row.count != f.count):
            problems.append(
                f"changed: {f.module} {f.qualname} is registered {row.kind} with "
                f"{row.count} site(s) on {', '.join(row.tables or ())} but now has "
                f"{f.count} on {', '.join(f.tables)} (line(s) "
                f"{', '.join(map(str, f.lines))}). Judge it again before changing the row.")
    for key in sorted(set(rows) - used):
        problems.append(f"stale: {key[0]} {key[1]} is registered but holds no SQL site any more.")
    return problems


def row_problems(rows: Optional[dict] = None, groups: Optional[dict] = None) -> list:
    """Rows that break the registry's own rules."""
    rows = registry() if rows is None else rows
    groups = surfaces() if groups is None else groups
    out = []
    for key, row in sorted(rows.items()):
        module, qualname = key
        whole = qualname == WHOLE or module.endswith("/")
        if row.kind not in KINDS:
            out.append(f"{key}: unknown kind {row.kind!r}")
        if row.kind == BLIND and row.owner not in OWNERS:
            out.append(f"{key}: a blind row names its owning increment (OWNERS), not {row.owner!r}")
        if row.kind != BLIND and row.owner is not None:
            out.append(f"{key}: only a blind row has an owner")
        if row.kind != BLIND and len((row.why or "").strip()) < 12:
            out.append(f"{key}: a {row.kind} row says why")
        if whole and row.kind != APPLIANCE:
            out.append(f"{key}: only an appliance row covers a whole module or folder")
        if row.kind in SAFE_KINDS:
            if not row.tables or row.count is None or row.count < 1:
                out.append(f"{key}: a {row.kind} row pins its tables and site count")
            elif not set(row.tables) <= set(TABLES):
                out.append(f"{key}: unknown table in {row.tables}")
        elif row.tables is not None or row.count is not None:
            out.append(f"{key}: only the safe kinds pin tables and counts")
    for prefix, s in sorted(groups.items()):
        if not prefix.startswith("/") or prefix.endswith("/") or "//" in prefix:
            out.append(f"route group {prefix!r} is not a clean path prefix")
        elif not any(prefix == r or prefix.startswith(r + "/") for r in FENCED_ROOTS):
            out.append(f"route group {prefix} is outside the fence's roots")
        if s.kind not in SURFACE_KINDS:
            out.append(f"route group {prefix}: unknown kind {s.kind!r}")
        if s.kind == BLIND and s.owner not in OWNERS:
            out.append(f"route group {prefix}: a blind group names its owning increment")
        if s.kind != BLIND and (s.owner is not None or len((s.why or "").strip()) < 12):
            out.append(f"route group {prefix}: a cleared group says why and has no owner")
    return out


# ═══════════════════════════════════════════════════════════════════════════
#  The call graph
# ═══════════════════════════════════════════════════════════════════════════

def dotted_module(module: str) -> str:
    """'services/adt/core.py' -> 'services.adt.core'; a package's __init__ is
    the package."""
    parts = module[:-3].split("/")
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _chain(node) -> Optional[list]:
    names = []
    while isinstance(node, ast.Attribute):
        names.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        names.append(node.id)
        return list(reversed(names))
    return None


#: Constructors of the application objects: what they were built with (the
#: lifespan, the routers) runs at boot, not when a route reads app.state.
_APP_OBJECTS = frozenset({"FastAPI", "ASGIApp", "AsyncServer", "HostGate", "fenced_socketio_app",
                          "_fenced_socketio_app"})


def _decorator_args(node) -> list:
    """What a definition's decorators hand over: the arguments of a decorator
    call (dependencies=[Depends(gate)]) and a bare decorator, but not the
    object a route decorator hangs off (@router.get, @app.get), which would
    tie every route of a module to whatever built its router or app."""
    out = []
    for dec in node.decorator_list:
        if isinstance(dec, ast.Call):
            out.extend(dec.args)
            out.extend(k.value for k in dec.keywords)
            if not isinstance(dec.func, ast.Attribute):
                out.append(dec.func)
        elif not isinstance(dec, ast.Attribute):
            out.append(dec)
    return out


class _Module:
    def __init__(self, module: str, tree):
        self.module = module
        self.dotted = dotted_module(module)
        self.is_package = module.endswith("__init__.py")
        self.tree = tree
        self.aliases: dict = {}
        self.nodes: dict = {}   # qualname -> definition, named value or the module
        self.kinds: dict = {}
        self._collect_imports()
        self._collect_nodes(tree.body, [])
        self.nodes[MODULE] = tree
        self.kinds[MODULE] = "module"

    def _package(self, level: int) -> str:
        parts = self.dotted.split(".") if self.dotted else []
        if not self.is_package:
            parts = parts[:-1]
        if level > 1:
            parts = parts[:len(parts) - (level - 1)]
        return ".".join(parts)

    def _collect_imports(self) -> None:
        # Every import anywhere in the module, function-local ones included:
        # Orb imports lazily inside functions to dodge import cycles.
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    if a.asname:
                        self.aliases[a.asname] = a.name
                    else:
                        top = a.name.split(".")[0]
                        self.aliases.setdefault(top, top)
            elif isinstance(node, ast.ImportFrom):
                base = node.module or ""
                if node.level:
                    base = ".".join(p for p in (self._package(node.level), base) if p)
                for a in node.names:
                    if a.name != "*":
                        self.aliases.setdefault(a.asname or a.name,
                                                f"{base}.{a.name}" if base else a.name)

    def _collect_nodes(self, body, stack: list) -> None:
        values = not stack or self.kinds.get(".".join(stack)) == "class"
        for stmt in _scope_statements(body):
            if isinstance(stmt, _DEFS):
                q = ".".join(stack + [stmt.name])
                self.nodes.setdefault(q, stmt)
                self.kinds.setdefault(q, "class" if isinstance(stmt, ast.ClassDef) else "def")
                self._collect_nodes(stmt.body, stack + [stmt.name])
            elif values:
                name = _named_target(stmt)
                if name:
                    q = ".".join(stack + [name])
                    self.nodes.setdefault(q, stmt)
                    self.kinds.setdefault(q, "value")


class CallGraph:
    """Who can reach whom, over the server's own modules. Nodes are
    (module path, qualname), the same keys as the registry's rows."""

    def __init__(self, root: Path = SERVER):
        self.modules: dict = {}
        self.by_dotted: dict = {}
        for path in module_files(root):
            module = rel(path, root)
            mod = _Module(module, _parse(path, module))
            self.modules[module] = mod
            self.by_dotted[mod.dotted] = mod
        self.edges: dict = {}
        for mod in self.modules.values():
            for q, node in mod.nodes.items():
                self.edges[(mod.module, q)] = self._edges(mod, q, node)

    # ── resolution ──────────────────────────────────────────────────────────
    def resolve_dotted(self, dotted: str, depth: int = 0) -> Optional[tuple]:
        if depth > 6:
            return None
        parts = dotted.split(".")
        for i in range(len(parts), 0, -1):
            mod = self.by_dotted.get(".".join(parts[:i]))
            if mod is None:
                continue
            rest = parts[i:]
            if not rest:
                return None  # a module, not a node
            q = ".".join(rest)
            if q in mod.nodes:
                return (mod.module, q)
            target = mod.aliases.get(rest[0])  # a name the module re-exports
            if target is not None:
                return self.resolve_dotted(".".join([target] + rest[1:]), depth + 1)
            return None
        return None

    def _resolve(self, mod: _Module, scope: list, chain: list) -> Optional[tuple]:
        head = chain[0]
        if head in ("self", "cls"):
            if len(chain) < 2:
                return None
            for i in range(len(scope), 0, -1):
                if mod.kinds.get(".".join(scope[:i])) == "class":
                    q = ".".join(scope[:i] + chain[1:2])
                    return (mod.module, q) if q in mod.nodes else None
            return None
        # Definitions nested in the enclosing functions, innermost first.
        for i in range(len(scope), 0, -1):
            if mod.kinds.get(".".join(scope[:i])) == "class":
                continue
            q = ".".join(scope[:i] + chain)
            if q in mod.nodes:
                return (mod.module, q)
        q = ".".join(chain)
        if q in mod.nodes:
            return (mod.module, q)
        target = mod.aliases.get(head)
        if target is not None:
            for i in range(len(chain), 0, -1):
                found = self.resolve_dotted(".".join([target] + chain[1:i]))
                if found is not None:
                    return found
        return None

    def _refs(self, mod: _Module, scope: list, nodes: Iterable) -> set:
        out = set()
        todo = list(nodes)
        while todo:
            cur = todo.pop()
            if isinstance(cur, _DEFS):
                # A nested definition is its own node: only what its header
                # evaluates here belongs to this one.
                todo.extend(_decorator_args(cur))
                if not isinstance(cur, ast.ClassDef):
                    todo.extend(cur.args.defaults)
                    todo.extend(d for d in cur.args.kw_defaults if d is not None)
                continue
            if isinstance(cur, (ast.Name, ast.Attribute)):
                chain = _chain(cur)
                if chain is not None:
                    for i in range(len(chain), 0, -1):
                        hit = self._resolve(mod, scope, chain[:i])
                        if hit is not None:
                            out.add(hit)
                            break
                    if isinstance(cur, ast.Attribute):
                        continue
            todo.extend(ast.iter_child_nodes(cur))
        return out

    def _edges(self, mod: _Module, q: str, node) -> set:
        kind = mod.kinds[q]
        scope = q.split(".") if q != MODULE else []
        if kind == "module":
            body = [s for s in node.body if not isinstance(s, _DEFS) and not _named_target(s)]
            return self._refs(mod, [], body)
        if kind == "value":
            value = node.value
            if isinstance(value, ast.Call) and (_chain(value.func) or [""])[-1] in _APP_OBJECTS:
                return set()
            return self._refs(mod, scope[:-1], [value] if value is not None else [])
        if kind == "class":
            # Using a class can run any of its methods.
            out = {(mod.module, f"{q}.{c.name}") for c in node.body if isinstance(c, _DEFS)}
            return out | self._refs(mod, scope[:-1], list(node.bases) + _decorator_args(node))
        header = _decorator_args(node) + list(node.args.defaults) + \
            [d for d in node.args.kw_defaults if d is not None]
        return self._refs(mod, scope[:-1], header) | self._refs(mod, scope, node.body)

    # ── queries ─────────────────────────────────────────────────────────────
    def node_for(self, module_dotted: str, qualname: str) -> Optional[tuple]:
        mod = self.by_dotted.get(module_dotted)
        if mod is None:
            return None
        q = qualname.replace(".<locals>", "")
        return (mod.module, q) if q in mod.nodes else None

    def reach(self, start: tuple) -> set:
        seen = {start}
        todo = deque([start])
        while todo:
            for nxt in self.edges.get(todo.popleft(), ()):
                if nxt not in seen:
                    seen.add(nxt)
                    todo.append(nxt)
        return seen

    def checks_scope(self, node: tuple) -> bool:
        """Does this route call a facility-aware scope helper itself, through
        a dependency it declares, or through a helper of its own module?"""
        direct = self.edges.get(node, set())
        if direct & SCOPE_HELPERS:
            return True
        return any(n[0] == node[0] and self.edges.get(n, set()) & SCOPE_HELPERS for n in direct)

    def socket_handlers(self) -> list:
        """Socket.IO event handlers (@sio.event, @sio.on(...)) under ws/."""
        out = []
        for mod in self.modules.values():
            if not mod.module.startswith("ws/"):
                continue
            for q, node in mod.nodes.items():
                if mod.kinds[q] != "def":
                    continue
                for dec in node.decorator_list:
                    chain = _chain(dec.func if isinstance(dec, ast.Call) else dec) or []
                    if len(chain) >= 2 and chain[-1] in ("event", "on") and "sio" in chain[0]:
                        out.append((mod.module, q))
                        break
        return sorted(out)


@lru_cache(maxsize=1)
def graph() -> CallGraph:
    return CallGraph()


@dataclass(frozen=True)
class Route:
    path: str
    methods: tuple
    node: Optional[tuple]
    endpoint: str


def app_routes(app, g: Optional[CallGraph] = None) -> list:
    """Every route the application serves, with its call-graph node (None for
    the framework's own pages and mounts)."""
    from starlette.routing import Mount
    from starlette.staticfiles import StaticFiles
    g = graph() if g is None else g
    out = []
    for r in app.routes:
        path = getattr(r, "path", "")
        if isinstance(r, Mount):
            # The built screens (main.py mounts dist/ when it exists).
            kind = "StaticFiles" if isinstance(r.app, StaticFiles) else type(r.app).__name__
            out.append(Route(path, ("MOUNT",), None, kind))
            continue
        ep = getattr(r, "endpoint", None)
        module = getattr(ep, "__module__", "") or ""
        qualname = getattr(ep, "__qualname__", "") or ""
        methods = tuple(sorted(getattr(r, "methods", None) or ("WEBSOCKET",)))
        out.append(Route(path, methods, g.node_for(module, qualname), f"{module}:{qualname}"))
    return out


def under(path: str, prefix: str) -> bool:
    return path == prefix or path.startswith(prefix + "/")


def surface_for(path: str, prefixes: Iterable[str]) -> Optional[str]:
    """The longest classified prefix a path sits under."""
    best = None
    for p in prefixes:
        if under(path, p) and (best is None or len(p) > len(best)):
            best = p
    return best


# ═══════════════════════════════════════════════════════════════════════════
#  Generation (python -m tests.facility_sql_sites)
# ═══════════════════════════════════════════════════════════════════════════

def generated_fence_source() -> str:
    def block(name, items):
        return f"{name} = (\n" + "".join(f"    {p!r},\n" for p in items) + ")\n"
    return (block("FACILITY_BLIND_SURFACES", blind_prefixes())
            + block("FACILITY_CLEARED_SURFACES", cleared_prefixes())
            + block("NO_PATIENT_DATA_SURFACES", no_patient_data_prefixes()))


def draft_rows(found: Optional[dict] = None, rows: Optional[dict] = None) -> str:
    """Draft rows for every unregistered owner, as blind with the owner left
    for a person to choose (and the kind to judge)."""
    found = scan() if found is None else found
    rows = registry() if rows is None else rows
    lines = []
    for key in sorted(found):
        f = found[key]
        if row_for(rows, f.module, f.qualname)[1] is None:
            lines.append(f"    ({f.module!r}, {f.qualname!r}): blind(\"<increment>\"),  "
                         f"# {', '.join(f.tables)}; {f.count} site(s)")
    return "\n".join(lines)


def main() -> int:
    print("# Generated from the route group rows; server/facility_fence.py carries these.")
    print(generated_fence_source())
    problems = ratchet() + row_problems()
    for p in problems:
        print("#", p)
    drafts = draft_rows()
    if drafts:
        print("# Draft rows for the unregistered owners:")
        print(drafts)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
