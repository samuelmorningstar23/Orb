"""The event loop is sacred — enforced, not merely documented.

WHY THIS FILE EXISTS. Orb has shipped THREE separate production freezes, every one
from blocking the asyncio event loop (see docs/WORKLOG.md §3):

  1. GIL starvation from in-process embedding/reranking.
  2. `httpx.AsyncClient.__init__` — a SYNCHRONOUS constructor — doing a macOS
     `configd` proxy lookup with no timeout, on the loop.
  3. The sqlcipher3 driver holding the GIL across `sqlite3_close_v2`, so a WAL
     checkpoint froze the whole process ~1 ms per MB of WAL.

The rule the repo converged on is absolute: **nothing that can block runs on the
event loop.** Sync DB work goes through `database.run_db()`; a route handler doing
heavy scans is a plain `def` so FastAPI threadpools it.

The rule was written down and then rotted anyway. On 2026-08-02, four LIVE
violations were found by reading the code for the technical manual — including the
audit middleware writing SQLite on the loop for *every mutation* in the system,
and `serve_upload` doing a fail-closed audit write plus a whole-file AES decrypt of
PHI media in an `async def` that awaited nothing.

They survived because the only enforcement that existed covered ONE router
(`tests/test_shadow_routes.py`), in a test whose own docstring called it "the rule
most likely to rot". It rotted. A convention with no test is a comment.

WHAT THIS ASSERTS. Across every route module, the middleware and the websocket
layer: no `async def` may CALL a known-blocking function directly.

The check is deliberately call-shaped rather than name-shaped, and that precision
is what makes it usable: `await run_db(log_audit_event, ...)` passes the blocking
function as a *reference*, which is the correct pattern and is not a call, so it
passes. `log_audit_event(...)` inside an `async def` is a call, and fails. The test
therefore needs no allow-list of "blessed" wrappers — it distinguishes the right
pattern from the wrong one structurally.

ONE LEVEL OF CALL GRAPH (added 2026-10-03). `post_message`, the route behind
every Relay send, called `get_channels_for_user()` and `send_message()` straight
from its `async def`. Neither name is in the symbol list, and both open a
session one frame down, so the direct check passed it for months. The second
test below resolves each call an `async def` makes, through the module's own
defs and its absolute `from services.x import name` / `import module` imports,
and fails when the callee is a module-level SYNC def whose own body makes a
blocking call. `services/` is scanned too: its async functions run on the same
loop as the routes.

WHAT IT DOES NOT CATCH, stated so nobody trusts it further than it deserves:
  * Blocking work reached more than ONE frame down (an async handler calls a
    sync helper that calls another helper that opens a connection), methods,
    and names bound any other way than a plain import.
  * Blocking that is not in the symbol list below (a new C extension, a large
    `json.dump`, `time.sleep`). Add symbols as they are discovered.
  * CPU-bound pure-Python work, which blocks just as effectively as I/O.
It is a floor, not a proof.
"""
import ast
import pathlib

import pytest

SERVER = pathlib.Path(__file__).resolve().parent.parent

# Directories whose async functions run ON the event loop.
SCANNED_DIRS = ("routes", "ws", "services")
SCANNED_FILES = ("main.py", "permissions.py", "services/audit_middleware.py")

# Calling any of these from an `async def` blocks the loop. Each entry earned its
# place by causing, or being one edit away from causing, a real freeze.
BLOCKING_CALLS = {
    "db_session",          # opens a SQLCipher connection; close() holds the GIL
    "get_db",              # same
    "log_audit_event",     # hash-chained SQLite write, BEGIN IMMEDIATE
    "verify_audit_chain",  # full-table scan over the audit chain
    "read_decrypted",      # whole-file AES decrypt
    "_open_raw_connection",
    "consistent_backup",   # backup + integrity_check + a real close
}


def _python_files():
    seen = set()
    for d in SCANNED_DIRS:
        for p in sorted((SERVER / d).rglob("*.py")):
            if "__pycache__" not in p.parts:
                seen.add(p)
    for f in SCANNED_FILES:
        p = SERVER / f
        if p.exists():
            seen.add(p)
    return sorted(seen)


def _called_name(node: ast.Call) -> str | None:
    """The bare name being CALLED. `f()` -> 'f'; `mod.f()` -> 'f'; `f` as an
    argument is not a Call and never reaches here — which is the whole point."""
    fn = node.func
    if isinstance(fn, ast.Name):
        return fn.id
    if isinstance(fn, ast.Attribute):
        return fn.attr
    return None


def _violations_in(path: pathlib.Path):
    """(function, line, symbol) for every blocking call made inside an async def.

    Nested function definitions are attributed to the async frame that encloses
    them only when they are themselves async; a plain `def` nested inside an
    `async def` is a closure that may legitimately be handed to `run_db`.
    """
    tree = ast.parse(path.read_text(), filename=str(path))
    found = []

    class Walker(ast.NodeVisitor):
        def __init__(self):
            self.async_stack = []

        def visit_AsyncFunctionDef(self, node):
            self.async_stack.append(node.name)
            self.generic_visit(node)
            self.async_stack.pop()

        def visit_FunctionDef(self, node):
            # A sync def is off the loop the moment it is threadpooled, and we
            # cannot tell from here whether it was. Do not descend into it.
            pass

        def visit_Call(self, node):
            if self.async_stack:
                name = _called_name(node)
                if name in BLOCKING_CALLS:
                    found.append((self.async_stack[-1], node.lineno, name))
            self.generic_visit(node)

    Walker().visit(tree)
    return found


@pytest.mark.parametrize("path", _python_files(), ids=lambda p: str(p.relative_to(SERVER)))
def test_no_blocking_call_inside_an_async_function(path):
    violations = _violations_in(path)
    if violations:
        rel = path.relative_to(SERVER)
        detail = "\n".join(
            f"  {rel}:{line}  async def {fn}() calls {sym}()" for fn, line, sym in violations
        )
        pytest.fail(
            "Blocking call on the event loop — this is the failure class behind all "
            "three of Orb's production freezes:\n" + detail +
            "\n\nFix: hand the function to `database.run_db(fn, *args)` and await it "
            "(passing it as a REFERENCE, not calling it), or make the handler a plain "
            "`def` so FastAPI threadpools it. See docs/WORKLOG.md §3."
        )


# ── One level of call graph ─────────────────────────────────────────────────

# (file, async function, callee) pairs known to call a blocking sync helper
# directly, each with the reason it is not fixed here. A RATCHET: the test below
# fails if any of these stops being a violation, so a fixed one leaves the list.
_KNOWN_INDIRECT = {
    # Boot, before the server accepts a request: nothing else is on the loop yet.
    ("main.py", "lifespan", "init_db"),
    ("main.py", "lifespan", "seed_default_models"),
    ("main.py", "lifespan", "refresh_ward_capacity"),
    ("main.py", "lifespan", "init_helix_db"),
    ("main.py", "lifespan", "refresh_formulary_terms_from_db"),
    ("main.py", "lifespan", "seed_terminology"),
    ("main.py", "lifespan", "reconcile_stale_pending_verifications"),
    # Admin history import and roster import: an audit write per call, owned by
    # those modules (open, 2026-10-03).
    ("routes/admin_routes.py", "history_import_preview", "_history_audit"),
    ("routes/admin_routes.py", "history_import_dry_run", "_history_audit"),
    ("routes/admin_routes.py", "history_import_commit", "_history_audit"),
    ("routes/admin_routes.py", "history_import_rejects", "_history_audit"),
    ("routes/roster_import_routes.py", "dry_run", "_audit"),
    ("routes/roster_import_routes.py", "commit", "_audit"),
    # Whisper hint vocabulary read at session start (open, 2026-10-03).
    ("ws/scribe_stream.py", "handle_start_session", "build_whisper_hints"),
}

_BLOCKING_DEFS_CACHE: dict = {}


def _blocking_sync_defs(path: pathlib.Path) -> set:
    """Names of module-level SYNC defs in `path` whose own body (not a nested
    def or lambda) calls a BLOCKING_CALLS symbol."""
    if path in _BLOCKING_DEFS_CACHE:
        return _BLOCKING_DEFS_CACHE[path]
    out = set()
    try:
        tree = ast.parse(path.read_text(), filename=str(path))
    except (OSError, SyntaxError):
        _BLOCKING_DEFS_CACHE[path] = out
        return out

    class Body(ast.NodeVisitor):
        def __init__(self):
            self.hit = False

        def visit_FunctionDef(self, node):
            pass

        def visit_AsyncFunctionDef(self, node):
            pass

        def visit_Lambda(self, node):
            pass

        def visit_Call(self, node):
            if _called_name(node) in BLOCKING_CALLS:
                self.hit = True
            self.generic_visit(node)

    for node in tree.body:
        if isinstance(node, ast.FunctionDef):
            body = Body()
            for stmt in node.body:
                body.visit(stmt)
            if body.hit:
                out.add(node.name)
    _BLOCKING_DEFS_CACHE[path] = out
    return out


def _module_file(root: pathlib.Path, dotted: str):
    p = root / (dotted.replace(".", "/") + ".py")
    return p if p.exists() else None


def _indirect_violations_in(path: pathlib.Path, root: pathlib.Path = SERVER):
    """(function, line, callee) for every call an async def makes, directly, to
    a sync def that blocks. A reference handed to run_db is not a call."""
    tree = ast.parse(path.read_text(), filename=str(path))
    names = {n: n for n in _blocking_sync_defs(path)}
    modules = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            target = _module_file(root, node.module)
            for alias in node.names:
                bound = alias.asname or alias.name
                if target is not None:
                    if alias.name in _blocking_sync_defs(target):
                        names[bound] = alias.name
                else:
                    sub = _module_file(root, f"{node.module}.{alias.name}")
                    if sub is not None:
                        modules[bound] = sub
        elif isinstance(node, ast.Import):
            for alias in node.names:
                target = _module_file(root, alias.name)
                if target is not None:
                    modules[alias.asname or alias.name.split(".")[-1]] = target

    found = []

    class Walker(ast.NodeVisitor):
        def __init__(self):
            self.async_stack = []

        def visit_AsyncFunctionDef(self, node):
            self.async_stack.append(node.name)
            self.generic_visit(node)
            self.async_stack.pop()

        def visit_FunctionDef(self, node):
            pass

        def visit_Lambda(self, node):
            pass

        def visit_Call(self, node):
            if self.async_stack:
                fn = node.func
                if isinstance(fn, ast.Name) and fn.id in names:
                    found.append((self.async_stack[-1], node.lineno, fn.id))
                elif (isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name)
                      and fn.value.id in modules
                      and fn.attr in _blocking_sync_defs(modules[fn.value.id])):
                    found.append((self.async_stack[-1], node.lineno, fn.attr))
            self.generic_visit(node)

    Walker().visit(tree)
    return found


@pytest.mark.parametrize("path", _python_files(), ids=lambda p: str(p.relative_to(SERVER)))
def test_no_async_function_calls_a_blocking_sync_helper(path):
    rel = str(path.relative_to(SERVER))
    violations = [
        (fn, line, callee) for fn, line, callee in _indirect_violations_in(path)
        if (rel, fn, callee) not in _KNOWN_INDIRECT
    ]
    if violations:
        detail = "\n".join(
            f"  {rel}:{line}  async def {fn}() calls {callee}(), which blocks"
            for fn, line, callee in violations
        )
        pytest.fail(
            "A sync helper that opens a session (or writes the audit chain) is "
            "called straight from an async function, one frame above the "
            "blocking call:\n" + detail +
            "\n\nFix: `await run_db(helper, *args)`, passing the helper as a "
            "REFERENCE."
        )


def test_the_known_indirect_list_only_holds_live_violations():
    """The ratchet: an entry whose call has been fixed must leave the list, or
    the list quietly becomes a licence for the next one."""
    live = set()
    for path in _python_files():
        rel = str(path.relative_to(SERVER))
        live.update((rel, fn, callee) for fn, _line, callee in _indirect_violations_in(path))
    stale = sorted(_KNOWN_INDIRECT - live)
    assert not stale, f"fixed, so remove from _KNOWN_INDIRECT: {stale}"


def test_the_indirect_detector_detects(tmp_path):
    """Planted shapes, as for the direct check: a sync helper that blocks,
    called from an async def in the same module and across an import, must be
    caught; the run_db reference and a helper that does not block must pass."""
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("")
    (tmp_path / "pkg" / "helpers.py").write_text(
        "def opens():\n    with db_session() as db:\n        pass\n"
        "def pure():\n    return 1\n"
    )
    cases = {
        "same module": ("def helper():\n    get_db()\n"
                        "async def h():\n    helper()\n", True),
        "from import": ("from pkg.helpers import opens\n"
                        "async def h():\n    opens()\n", True),
        "lazy import inside the handler": ("async def h():\n"
                                           "    from pkg.helpers import opens\n"
                                           "    opens()\n", True),
        "module attribute": ("from pkg import helpers\n"
                             "async def h():\n    helpers.opens()\n", True),
        "run_db reference": ("from pkg.helpers import opens\n"
                             "async def h():\n    await run_db(opens)\n", False),
        "helper that does not block": ("from pkg.helpers import pure\n"
                                       "async def h():\n    pure()\n", False),
    }
    for label, (src, should_fail) in cases.items():
        mod = tmp_path / f"case_{abs(hash(label))}.py"
        mod.write_text(src)
        caught = bool(_indirect_violations_in(mod, root=tmp_path))
        assert caught == should_fail, f"{label}: caught={caught}"


def test_the_scan_actually_covers_the_surface():
    """A guard that matches nothing passes forever. Pin the floor so a moved
    directory or a renamed package cannot silently empty this file."""
    files = _python_files()
    assert len(files) >= 20, f"expected the route/ws surface, found {len(files)} files"
    names = {p.name for p in files}
    for expected in ("main.py", "permissions.py", "audit_middleware.py", "realtime.py",
                     "relay_routes.py", "discharge_service.py", "sage_service.py"):
        assert expected in names, f"{expected} dropped out of the scan"


def test_the_detector_detects():
    """The test that makes the rest of this file trustworthy.

    A linter nobody has watched fail is indistinguishable from one that cannot
    fail. Plant each violation shape and require a catch — including the
    near-miss that MUST pass, because a check that flags the correct pattern
    gets disabled by the first engineer it inconveniences.
    """
    import tempfile

    must_fail = {
        "direct call": "async def h():\n    with db_session() as db:\n        pass\n",
        "attribute call": "async def h():\n    database.get_db()\n",
        "audit on the loop": "async def h():\n    log_audit_event(a, b)\n",
        "nested async": "async def outer():\n    async def inner():\n        get_db()\n",
        "decrypt on the loop": "async def h():\n    body = read_decrypted(p)\n",
    }
    must_pass = {
        # The correct pattern: a REFERENCE handed to run_db, never a call.
        "run_db reference": "async def h():\n    await run_db(log_audit_event, a, b)\n",
        # A plain def is threadpooled by FastAPI; blocking there is fine.
        "sync handler": "def h():\n    with db_session() as db:\n        pass\n",
        # A sync closure defined inside an async frame, to hand to run_db.
        "closure for run_db": (
            "async def h():\n"
            "    def work():\n"
            "        with db_session() as db:\n"
            "            pass\n"
            "    await run_db(work)\n"
        ),
    }

    def scan(src):
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
            f.write(src)
            tmp = pathlib.Path(f.name)
        try:
            return _violations_in(tmp)
        finally:
            tmp.unlink()

    for label, src in must_fail.items():
        assert scan(src), f"detector MISSED a real violation: {label}"
    for label, src in must_pass.items():
        assert not scan(src), f"detector FALSE-POSITIVED on the correct pattern: {label}"
