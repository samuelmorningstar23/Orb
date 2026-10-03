"""Guard: no module talks to the model's inference endpoints except through
services/ollama_gateway (or a named seam whose callers write the audit row).

F038 (2026-10-03): the collateral said every model inference was audited while
about eleven lanes posted to Ollama directly and wrote no row. The gateway now
writes one row per call with the prompt's hash, and this file is what keeps a
new lane from going round it: a rule kept only in a comment had drifted once.

What counts as a raw call: any string that builds an /api/chat or /api/generate
URL (an f-string, a concatenation) or passes one to a call other than
ollama_gateway.post / ollama_gateway.stream. Comments and docstrings are not
code and are not scanned.
"""
import ast
from pathlib import Path

SERVER = Path(__file__).resolve().parents[1]
SCANNED = [SERVER / "services", SERVER / "routes", SERVER / "ws", SERVER / "main.py"]
ENDPOINTS = ("/api/chat", "/api/generate")

# (file, enclosing function) -> why it may build the URL itself. Every entry is
# a place where the audit row is written somewhere else, or where no answer and
# no patient data are involved.
ALLOWED_RAW = {
    ("services/llm_providers.py", "OllamaChatProvider.astream_chat"):
        "Sage chat and Sage Tier 2 seam; sage_service and sage_assessment_service write the row",
    ("services/scribe_providers.py", "OllamaProvider.generate"):
        "Scribe seam; scribe_service writes the row for every provider",
    ("services/scribe_providers.py", "OllamaProvider.agenerate"):
        "Scribe seam; scribe_service writes the row for every provider",
    ("services/scribe_providers.py", "OllamaProvider.agenerate_stream"):
        "Scribe seam; scribe_service writes the row for every provider",
    ("main.py", "lifespan._warm_one"): "residency warm ping, num_predict 1, no patient data",
    ("main.py", "load_model"): "admin model load, a one-word prompt, no patient data",
    ("main.py", "unload_model"): "admin model unload, keep_alive 0, no prompt",
}

# Names that run an inference through a provider seam.
SEAM_MODULES = {"services.llm_providers", "services.scribe_providers"}
SEAM_NAMES = {"local_provider", "OllamaChatProvider", "provider_for_model", "get_provider"}
# Modules that import a seam name without running an inference themselves.
SEAM_PASS_THROUGH = {
    "services/thinking_mode.py": "selects the provider and returns it to sage_service, which writes the row",
}
AUDIT_WRITERS = ("record_inference", "record_inference_async", "record_inference_soon")


def _files():
    for root in SCANNED:
        if root.is_file():
            yield root
        elif root.is_dir():
            yield from sorted(root.rglob("*.py"))


def _rel(path: Path) -> str:
    return path.relative_to(SERVER).as_posix()


def _has_endpoint(node) -> bool:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return any(e in node.value for e in ENDPOINTS)
    if isinstance(node, ast.JoinedStr):
        return any(_has_endpoint(v) for v in node.values)
    if isinstance(node, ast.BinOp):
        return _has_endpoint(node.left) or _has_endpoint(node.right)
    return False


def _is_gateway_call(call: ast.Call) -> bool:
    f = call.func
    return (isinstance(f, ast.Attribute) and f.attr in ("post", "stream")
            and isinstance(f.value, ast.Name) and f.value.id == "ollama_gateway")


class _Scanner(ast.NodeVisitor):
    def __init__(self):
        self.stack: list[str] = []
        self.hits: list[tuple[str, int]] = []
        self.gateway_calls: list[ast.Call] = []

    def _scoped(self, node):
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    visit_FunctionDef = visit_AsyncFunctionDef = visit_ClassDef = _scoped

    def _where(self) -> str:
        return ".".join(self.stack)

    def visit_JoinedStr(self, node):
        if _has_endpoint(node):
            self.hits.append((self._where(), node.lineno))
        # No generic_visit: the parts are Constants already judged as a whole.

    def visit_BinOp(self, node):
        if _has_endpoint(node) and isinstance(node.op, ast.Add):
            self.hits.append((self._where(), node.lineno))
            return
        self.generic_visit(node)

    def visit_Call(self, node):
        if _is_gateway_call(node):
            self.gateway_calls.append(node)
        else:
            for arg in list(node.args) + [k.value for k in node.keywords]:
                if isinstance(arg, ast.Constant) and _has_endpoint(arg):
                    self.hits.append((self._where(), node.lineno))
        self.generic_visit(node)


def _scan(path: Path) -> _Scanner:
    scanner = _Scanner()
    scanner.visit(ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))
    return scanner


def test_no_module_posts_to_an_inference_endpoint_itself():
    offenders = []
    for path in _files():
        rel = _rel(path)
        if rel == "services/ollama_gateway.py":
            continue
        for where, line in _scan(path).hits:
            if (rel, where) not in ALLOWED_RAW:
                offenders.append(f"{rel}:{line} in {where or '<module>'}")
    assert not offenders, (
        "These build an /api/chat or /api/generate request outside "
        "services/ollama_gateway, so the inference writes no audit row. Use "
        "ollama_gateway.post / ollama_gateway.stream:\n  " + "\n  ".join(offenders))


def test_every_allowance_still_exists():
    """A stale allowance is a hole a new raw call could hide in."""
    found = set()
    for path in _files():
        rel = _rel(path)
        for where, _ in _scan(path).hits:
            found.add((rel, where))
    stale = [f"{f}::{w}" for (f, w) in ALLOWED_RAW if (f, w) not in found]
    assert not stale, "Remove these from ALLOWED_RAW, they no longer post:\n  " + "\n  ".join(stale)


def test_every_gateway_call_names_its_purpose_as_a_literal():
    """The purpose is how an auditor finds a lane's rows, so it is greppable."""
    bad = []
    for path in _files():
        for call in _scan(path).gateway_calls:
            purpose = next((k.value for k in call.keywords if k.arg == "purpose"), None)
            if not (isinstance(purpose, ast.Constant) and isinstance(purpose.value, str)
                    and purpose.value):
                bad.append(f"{_rel(path)}:{call.lineno}")
    assert not bad, "ollama_gateway call without a literal purpose:\n  " + "\n  ".join(bad)


def test_every_module_that_runs_a_seam_writes_the_audit_row():
    missing = []
    for path in _files():
        rel = _rel(path)
        if rel in ("services/llm_providers.py", "services/scribe_providers.py"):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        uses_seam = any(
            isinstance(n, ast.ImportFrom) and n.module in SEAM_MODULES
            and any(a.name in SEAM_NAMES for a in n.names)
            for n in ast.walk(tree)
        )
        if not uses_seam or rel in SEAM_PASS_THROUGH:
            continue
        writes = any(
            isinstance(n, (ast.Name, ast.Attribute, ast.alias))
            and (getattr(n, "id", None) or getattr(n, "attr", None)
                 or getattr(n, "name", None)) in AUDIT_WRITERS
            for n in ast.walk(tree)
        )
        if not writes:
            missing.append(rel)
    assert not missing, (
        "These run an inference through a provider seam and never write an "
        "ai_inference row:\n  " + "\n  ".join(missing))


def test_the_scanner_catches_the_shapes_that_went_round_the_audit():
    """The guard is only as good as its detector, so prove it on each shape."""
    src = '''
async def f(client, OLLAMA_BASE_URL, base):
    await client.post(f"{OLLAMA_BASE_URL}/api/chat", json={})
    await client.post("http://localhost:11434/api/generate", json={})
    url = base + "/api/chat"
    async with client.stream("POST", f"{base}/api/generate") as r:
        pass
    await ollama_gateway.post(client, "/api/chat", {}, purpose="ok")
    async with ollama_gateway.stream(client, "/api/generate", {}, purpose="ok") as r:
        pass
    """A docstring that mentions /api/chat is not a call."""
'''
    scanner = _Scanner()
    scanner.visit(ast.parse(src))
    assert sorted(line for _, line in scanner.hits) == [3, 4, 5, 6]
    assert len(scanner.gateway_calls) == 2
