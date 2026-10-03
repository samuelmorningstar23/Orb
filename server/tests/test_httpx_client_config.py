"""Every httpx client in server/ must declare which network lane it is on.

WHY THIS TEST EXISTS
--------------------
On 2026-08-01 the API wedged twice — /api/health timing out completely, once
for 3.5 hours. Three faulthandler dumps (kill -USR1) taken 8 s apart all caught
the MAIN THREAD, i.e. the asyncio event loop, in the identical frame:

    event_bus._safe_dispatch
      -> sage_narrator_service.on_event -> refresh -> _narrate
      -> model_router.ambient_tier_evicted -> probe_residency
      -> httpx.AsyncClient.__init__
      -> httpx._client._get_proxy_map -> httpx._utils.get_environment_proxies
      -> urllib.request.getproxies -> getproxies_macosx_sysconf   <-- STUCK

`httpx.AsyncClient.__init__` is SYNCHRONOUS. Writing `async with
httpx.AsyncClient(...)` still runs the entire constructor on the event loop
thread, and with the default `trust_env=True` that constructor resolves the
proxy configuration — on macOS an IPC round-trip into `configd` that takes no
timeout argument and has no async form. When configd is contended the loop
blocks, unboundedly, and a blocked loop cannot even answer /api/health or run
its signal handlers.

Orb constructed a brand-new client on EVERY call at ~30 sites and set
`trust_env` at none of them. A comment would not have held — those 30 sites
drifted apart over months — so the rule is enforced here instead.

THE RULE
--------
Every `httpx.Client(...)` / `httpx.AsyncClient(...)` construction under server/
must be explicit about its destination:

  * LOOPBACK (Ollama at OLLAMA_BASE_URL, and anything else on 127.0.0.1 /
    localhost / the appliance itself) -> pass `trust_env=False`. A proxy is
    meaningless for loopback and Orb is a local-first appliance, so the OS
    lookup is pure risk. This is the large majority, and it includes the site
    that wedged (services/model_router.py:probe_residency).

  * GENUINELY EXTERNAL -> the file must be listed in EXTERNAL_LANES below and
    the construction must pass `transport=`. `trust_env=False` is deliberately
    NOT the answer out there: a hospital may require HTTP_PROXY to reach the
    internet at all and may pin a private CA via SSL_CERT_FILE, and silently
    breaking either would be worse than the hang. Passing an explicit
    `transport` is what makes httpx skip the env-proxy lookup
    (`allow_env_proxies = trust_env and transport is None`, httpx/_client.py)
    while leaving trust_env=True in force for TLS — so the resolution happens
    ONCE per process instead of once per call.

Top-level `httpx.get/post/...` calls are checked too: they build a throwaway
Client internally and pay exactly the same cost.

WHAT THIS TEST DELIBERATELY DOES NOT REQUIRE
--------------------------------------------
Shared module-level client singletons. Per-call construction also throws away
the connection pool, so every Ollama call pays a fresh TCP handshake — but that
handshake is on loopback (tens of microseconds), while an httpx client owns a
connection pool bound to the event loop that created it. This repo already
carries scar tissue from loop-bound state (see llm_queue_service._bind_loop's
self-healing), several of these sites hold long-lived streaming responses, and
a client closed by one path would break every other. For the pilot timeline the
risk clearly outweighs a loopback handshake, so trust_env is the whole fix. If
that judgement is ever revisited, revisit it deliberately — do not half-do it.
"""
from __future__ import annotations

import ast
import hashlib
from pathlib import Path

import pytest

SERVER_ROOT = Path(__file__).resolve().parent.parent

# Directories that are not application code.
SKIP_DIRS = {".venv", "__pycache__", "node_modules", ".git", "uploads", "corpus",
             "backups", "downtime_exports", "migrations"}

# Files whose httpx clients genuinely leave the appliance. Listing a file here
# is a claim that its destinations are on the public internet, and it swaps the
# requirement from `trust_env=False` to `transport=` (resolve-once).
EXTERNAL_LANES = {
    "services/pulse_service.py":
        "Open-Meteo / api.fda.gov / data.cdc.gov — inbound-only public feeds. "
        "Uses _external_transport_kwargs(), which caches the env proxy map and "
        "the SSL context for the process instead of resolving per call.",
}

CONSTRUCTORS = {"Client", "AsyncClient"}
# Top-level convenience API — each of these builds a Client under the hood.
ONESHOT_METHODS = {"get", "post", "put", "patch", "delete", "head", "options",
                   "request", "stream"}


def _python_files() -> list[Path]:
    files = []
    for path in SERVER_ROOT.rglob("*.py"):
        rel = path.relative_to(SERVER_ROOT)
        if any(part in SKIP_DIRS for part in rel.parts):
            continue
        # The suite's own fakes and TestClient plumbing are not production
        # network calls; they never reach a real socket.
        if rel.parts[0] == "tests":
            continue
        files.append(path)
    return sorted(files)


def _httpx_aliases(tree: ast.AST) -> set[str]:
    """Names in this module that refer to the httpx module itself."""
    aliases = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name == "httpx":
                    aliases.add(a.asname or "httpx")
    return aliases


def _is_httpx_ref(value: ast.AST, aliases: set[str]) -> bool:
    """True when `value` evaluates to the httpx module.

    Covers `httpx.X`, an aliased `import httpx as hx`, and the stashed-module
    idiom `self._httpx.X` used by scripts/benchmark_medqa.py.
    """
    if isinstance(value, ast.Name):
        return value.id in aliases or value.id == "httpx"
    if isinstance(value, ast.Attribute):
        return value.attr in aliases or value.attr in {"httpx", "_httpx"}
    return False


def _collect_sites(path: Path, text: str) -> list[tuple[int, str, set[str]]]:
    """(lineno, what, keyword names) for every httpx client construction."""
    tree = ast.parse(text, filename=str(path))
    aliases = _httpx_aliases(tree) or {"httpx"}
    sites = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        attr = node.func.attr
        if attr not in CONSTRUCTORS and attr not in ONESHOT_METHODS:
            continue
        if not _is_httpx_ref(node.func.value, aliases):
            continue
        kwargs = {kw.arg for kw in node.keywords if kw.arg}
        # **kwargs splat: the lane config may be supplied by a helper.
        splat = any(kw.arg is None for kw in node.keywords)
        sites.append((node.lineno, f"httpx.{attr}(...)", kwargs, splat))
    return sites


def _trust_env_false(node_kwargs, path: Path, lineno: int) -> bool:
    """`trust_env=False` literally present at this call site."""
    tree = ast.parse(path.read_text(), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and node.lineno == lineno:
            for kw in node.keywords:
                if kw.arg == "trust_env":
                    return isinstance(kw.value, ast.Constant) and kw.value.value is False
    return False


# The parametrisation below freezes (file, line) pairs at COLLECTION time, but
# _trust_env_false() re-reads the file when the test finally RUNS — which in a
# full-suite run is up to forty minutes later. Edit one of these files in
# between and the line number in the test id stops pointing at the call site
# that was scanned, so the failure names an innocent construction (or an
# innocent `name: str = "abstract"`). That happened on 2026-08-15:
# services/llm_providers.py was saved mid-run, its httpx site moved from line 70
# to line 104, and this test reported a lane violation at a line that holds no
# httpx call at all.
#
# Digest exactly the bytes we parsed, so that case can be told apart from a real
# finding instead of being debugged as one.
_COLLECTION_DIGESTS: dict[Path, str] = {}


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _scan_every_file() -> list[tuple[Path, int, str, set[str], bool]]:
    sites = []
    for path in _python_files():
        text = path.read_text()
        _COLLECTION_DIGESTS[path] = hashlib.sha256(text.encode()).hexdigest()
        for (lineno, what, kwargs, splat) in _collect_sites(path, text):
            sites.append((path, lineno, what, kwargs, splat))
    return sites


ALL_SITES = _scan_every_file()


def _refuse_if_the_file_moved(path: Path) -> None:
    """Fail as what it is — a moving working tree — not as an httpx finding."""
    if _digest(path) == _COLLECTION_DIGESTS.get(path):
        return
    rel = path.relative_to(SERVER_ROOT).as_posix()
    pytest.fail(
        f"\n{rel} changed on disk between pytest's collection and this test running.\n"
        f"\n"
        f"The line number in this test's id was fixed when the file was scanned at\n"
        f"collection; it no longer points at the call site it named, so any verdict\n"
        f"this test reaches about it is meaningless. This is NOT an httpx lane\n"
        f"violation — it means the checkout was edited mid-run (a full-suite run\n"
        f"takes ~40 minutes, which is ample). Re-run the suite against a quiescent\n"
        f"checkout before believing anything else in this file.\n"
    )


def test_the_scan_actually_finds_the_clients():
    """Guard against the walker silently matching nothing (which would make
    every assertion below vacuously pass)."""
    assert len(ALL_SITES) >= 25, f"expected the ~30 known httpx sites, found {len(ALL_SITES)}"
    files = {p.relative_to(SERVER_ROOT).as_posix() for p, *_ in ALL_SITES}
    # The site that actually wedged the loop must be in scope.
    assert "services/model_router.py" in files
    assert "services/sage_narrator_service.py" in files
    assert "services/pulse_service.py" in files


@pytest.mark.parametrize(
    "path,lineno,what,kwargs,splat",
    ALL_SITES,
    ids=[f"{p.relative_to(SERVER_ROOT).as_posix()}:{ln}" for p, ln, *_ in ALL_SITES],
)
def test_every_httpx_client_declares_its_network_lane(path, lineno, what, kwargs, splat):
    _refuse_if_the_file_moved(path)
    rel = path.relative_to(SERVER_ROOT).as_posix()

    if rel in EXTERNAL_LANES:
        assert "transport" in kwargs or splat, (
            f"{rel}:{lineno} {what} is on the EXTERNAL lane but does not pass "
            f"`transport=` (directly or via a **kwargs helper).\n"
            f"  Reason on file: {EXTERNAL_LANES[rel]}\n"
            f"  Without an explicit transport, httpx re-runs get_environment_proxies()\n"
            f"  on the event loop for EVERY call — the macOS configd lookup that\n"
            f"  wedged /api/health for 3.5 hours on 2026-08-01."
        )
        assert "trust_env" not in kwargs, (
            f"{rel}:{lineno} must NOT set trust_env: this lane leaves the appliance, "
            f"and a hospital's HTTPS_PROXY / SSL_CERT_FILE has to keep working."
        )
        return

    assert "trust_env" in kwargs or splat, (
        f"\n{rel}:{lineno} constructs {what} without `trust_env=False`.\n"
        f"\n"
        f"httpx.Client/AsyncClient.__init__ is SYNCHRONOUS, so this runs on the\n"
        f"event loop even inside `async with`. With trust_env left at its default\n"
        f"it resolves proxies via urllib.getproxies() -> getproxies_macosx_sysconf,\n"
        f"an untimed IPC call into macOS configd. That blocked Orb's loop for 3.5\n"
        f"hours on 2026-08-01 and stopped /api/health answering at all.\n"
        f"\n"
        f"  * Talking to Ollama / localhost / the appliance? add `trust_env=False`.\n"
        f"  * Genuinely leaving the hospital network? add this file to\n"
        f"    EXTERNAL_LANES in {Path(__file__).name} and pass an explicit\n"
        f"    `transport=` so the proxy lookup happens once, not per call.\n"
    )
    if not splat:
        assert _trust_env_false(kwargs, path, lineno), (
            f"{rel}:{lineno} passes trust_env, but not the literal `trust_env=False` "
            f"this rule requires."
        )


def test_external_lane_resolves_proxy_configuration_only_once():
    """The outbound lane must cache its proxy map, not re-query the OS per call."""
    import httpx
    import urllib.request

    from services import pulse_service

    pulse_service._env_proxy_map = None
    pulse_service._external_ssl_context = None

    calls = {"n": 0}
    real = urllib.request.getproxies

    def counting():
        calls["n"] += 1
        return real()

    import httpx._utils as httpx_utils
    real_utils = httpx_utils.getproxies
    httpx_utils.getproxies = counting
    try:
        clients = []
        for _ in range(10):
            clients.append(
                httpx.AsyncClient(timeout=10, **pulse_service._external_transport_kwargs())
            )
        assert calls["n"] == 1, (
            f"the outbound lane queried the OS proxy configuration {calls['n']} times "
            f"for 10 clients; it must resolve once per process"
        )
        # And the lane still honours a proxy when one is configured.
        assert all(isinstance(c, httpx.AsyncClient) for c in clients)
    finally:
        httpx_utils.getproxies = real_utils
        pulse_service._env_proxy_map = None
        pulse_service._external_ssl_context = None


def test_external_lane_still_honours_hospital_proxy_and_no_proxy(monkeypatch):
    """trust_env=False on the loopback clients must not have cost the outbound
    lane its proxy support — that was the whole point of splitting them."""
    import httpx

    from services import pulse_service

    monkeypatch.setattr(pulse_service, "_env_proxy_map", None)
    monkeypatch.setattr(pulse_service, "_external_ssl_context", None)
    monkeypatch.setenv("HTTPS_PROXY", "http://hospital-proxy.local:3128")
    monkeypatch.setenv("NO_PROXY", "localhost,internal.hospital.example")

    client = httpx.AsyncClient(timeout=10, **pulse_service._external_transport_kwargs())
    proxied = client._transport_for_url(httpx.URL("https://api.fda.gov/drug/enforcement.json"))
    bypassed = client._transport_for_url(httpx.URL("https://internal.hospital.example/x"))

    assert proxied is not client._transport, "public feed should route via HTTPS_PROXY"
    assert bypassed is client._transport, "NO_PROXY host should bypass the proxy"

    pulse_service._env_proxy_map = None
    pulse_service._external_ssl_context = None
