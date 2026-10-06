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
passes `trust_env=False`. Every one of them talks to loopback (Ollama at
OLLAMA_BASE_URL, and anything else on 127.0.0.1 / localhost / the appliance
itself), where a proxy is meaningless, so the OS lookup is pure risk. That
includes the site that wedged (services/model_router.py:probe_residency).

Top-level `httpx.get/post/...` calls are checked too: they build a throwaway
Client internally and pay exactly the same cost.

THE OFFLINE GUARD
-----------------
There used to be one exception, an "external lane" for Pulse's internet feeds
(open-meteo, api.fda.gov, data.cdc.gov), which kept the hospital's proxy
settings instead. Orb is strictly offline (standing rule, 4 October 2026), and
on 6 October 2026 those feeds were removed with services/pulse_service.py.
EXTERNAL_LANES is now empty and must stay empty, and a second scan below fails
the build if any server module (tests and scripts/download_* excepted) makes a
network call to a literal host that is not loopback: httpx, requests, aiohttp,
urllib.request, websockets, any client's .get/.post/... A URL that is only
data (the FHIR code-system names such as http://loinc.org) is not a call and
is not flagged.

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
import re
from pathlib import Path

import pytest

SERVER_ROOT = Path(__file__).resolve().parent.parent

# Directories that are not application code.
SKIP_DIRS = {".venv", "__pycache__", "node_modules", ".git", "uploads", "corpus",
             "backups", "downtime_exports", "migrations"}

# Files whose network calls leave the appliance. EMPTY, and a test below keeps
# it empty: Orb is strictly offline. Pulse's internet feeds were the only entry
# until 6 October 2026, when they were removed.
EXTERNAL_LANES: dict[str, str] = {}

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


def test_there_is_no_external_lane():
    """Orb is strictly offline. A file listed here would be a claim that some
    server code is meant to reach the internet, and none is."""
    assert EXTERNAL_LANES == {}, (
        f"EXTERNAL_LANES lists {sorted(EXTERNAL_LANES)}. Orb is strictly offline: "
        f"no server module may talk to anything but the appliance itself."
    )


@pytest.mark.parametrize(
    "path,lineno,what,kwargs,splat",
    ALL_SITES,
    ids=[f"{p.relative_to(SERVER_ROOT).as_posix()}:{ln}" for p, ln, *_ in ALL_SITES],
)
def test_every_httpx_client_declares_its_network_lane(path, lineno, what, kwargs, splat):
    _refuse_if_the_file_moved(path)
    rel = path.relative_to(SERVER_ROOT).as_posix()

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
        f"  * Leaving the hospital network? Orb does not: it is strictly offline,\n"
        f"    and the offline guard in {Path(__file__).name} refuses it.\n"
    )
    if not splat:
        assert _trust_env_false(kwargs, path, lineno), (
            f"{rel}:{lineno} passes trust_env, but not the literal `trust_env=False` "
            f"this rule requires."
        )


# ═════════════════════════════════════════════════════════════════════════════
#  The offline guard
# ═════════════════════════════════════════════════════════════════════════════
# Orb is strictly offline: no server module may call a host that is not the
# appliance itself. This scan reads the source, never runs it, so it judges
# only LITERAL hosts. A base URL taken from configuration (OLLAMA_BASE_URL) is
# not a literal; its default is checked by the env-default test further down.
#
# A call is any of the verbs below on any object (httpx, requests, aiohttp,
# urllib.request, websockets, or a client from `async with ... as client`)
# whose URL argument is, or is assigned from, a string beginning with
# http(s):// or ws(s)://. A URL that is never handed to a call is data (the
# FHIR export names code systems such as http://loinc.org) and is ignored.
_NETWORK_VERBS = ONESHOT_METHODS | {
    "Client", "AsyncClient", "ClientSession", "Request", "urlopen", "urlretrieve",
    "open", "connect", "ws_connect",
}
# The positional slots a URL can sit in: first, or second for
# `request("GET", url)` / `stream("GET", url)`; plus these keywords.
_URL_KEYWORDS = {"url", "base_url"}
_URL_RE = re.compile(
    r"^(?:https?|wss?)://(?:[^@/?#]*@)?(\[[^\]]*\]|[^/:?#\[\]]*)", re.IGNORECASE
)


def _offline_files() -> list[Path]:
    """Server modules the guard covers: everything but tests, and the model
    and dataset download scripts an engineer runs by hand on a connected
    machine before the appliance ships (scripts/download_*)."""
    return [
        p for p in _python_files()
        if not (p.relative_to(SERVER_ROOT).parts[0] == "scripts"
                and p.name.startswith("download_"))
    ]


def _assignments(tree: ast.AST) -> dict[str, list[ast.AST]]:
    """Every value assigned to each plain name, anywhere in the module.

    Module-wide rather than per scope on purpose: it can only over-report (a
    `url` assigned in one function judged against a call in another), never
    miss the `url = f"https://..."; client.get(url)` shape the old Pulse
    fetchers used.
    """
    names: dict[str, list[ast.AST]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.setdefault(target.id, []).append(node.value)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) \
                and node.value is not None:
            names.setdefault(node.target.id, []).append(node.value)
    return names


def _url_prefixes(node: ast.AST, names: dict, depth: int = 0) -> list[tuple[str, bool]]:
    """The literal text each possible value of `node` starts with, and whether
    that text is the WHOLE value (False when an interpolation follows it)."""
    if depth > 6:
        return []
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [(node.value, True)]
    if isinstance(node, ast.JoinedStr):
        text = ""
        for part in node.values:
            if not isinstance(part, ast.Constant):
                return [(text, False)]
            text += str(part.value)
        return [(text, True)]
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Mod)):
        return [(t, False) for t, _ in _url_prefixes(node.left, names, depth + 1)]
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
            and node.func.attr == "format":
        return [(t, False) for t, _ in _url_prefixes(node.func.value, names, depth + 1)]
    if isinstance(node, ast.Name):
        out: list[tuple[str, bool]] = []
        for value in names.get(node.id, []):
            out += _url_prefixes(value, names, depth + 1)
        return out
    return []


def _is_loopback(host: str, complete: bool) -> bool:
    import ipaddress

    h = host.strip("[]").lower()
    if not complete:
        # The literal stops inside the host name (f"https://api.{tld}"). It is
        # loopback only if it can only become loopback.
        return h.startswith(("localhost", "127.", "::1"))
    if h == "localhost" or h.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False


def _off_appliance_calls(source: str, filename: str = "<src>") -> list[tuple[int, str, str]]:
    """(line, verb, host) for every call to a literal host that is not loopback."""
    tree = ast.parse(source, filename=filename)
    names = _assignments(tree)
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        verb = func.attr if isinstance(func, ast.Attribute) else \
            func.id if isinstance(func, ast.Name) else None
        if verb not in _NETWORK_VERBS:
            continue
        candidates = list(node.args[:2]) + [
            kw.value for kw in node.keywords if kw.arg in _URL_KEYWORDS
        ]
        for candidate in candidates:
            for text, whole in _url_prefixes(candidate, names):
                m = _URL_RE.match(text)
                if not m or not m.group(1):
                    continue  # not a URL, or the host itself is interpolated
                host = m.group(1)
                # The host is complete when something follows it in the
                # literal (a port, a path) or the literal is the whole value.
                complete = whole or len(m.group(0)) < len(text)
                if not _is_loopback(host, complete):
                    found.append((node.lineno, verb, host))
    return sorted(set(found))


def test_no_server_module_calls_a_host_off_the_appliance():
    offenders = []
    for path in _offline_files():
        rel = path.relative_to(SERVER_ROOT).as_posix()
        for lineno, verb, host in _off_appliance_calls(path.read_text(), rel):
            offenders.append(f"  {rel}:{lineno}  {verb}(...) -> {host}")
    assert not offenders, (
        "\nOrb is strictly offline (standing rule, 4 October 2026): no connection "
        "of any kind leaves the appliance. These calls name a host that is not "
        "loopback:\n" + "\n".join(offenders) + "\n\n"
        "If the URL is only data (a code-system name, a citation), keep it out of "
        "any network call. If the code really needs that host, it does not belong "
        "on the appliance."
    )


def test_the_offline_scan_catches_what_it_must_and_nothing_else():
    """The guard above passes vacuously if the scanner finds nothing. Hold it to
    the shapes the removed Pulse code used, and to the data URLs it must leave
    alone."""
    flagged = """
import httpx, requests, aiohttp, urllib.request, websockets

async def weather(lat):
    url = (f"https://api.open-meteo.com/v1/forecast?latitude={lat}"
           f"&daily=weather_code")
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(url)

def recalls():
    return requests.get("https://api.fda.gov/drug/enforcement.json")

def flu():
    return urllib.request.urlopen("https://data.cdc.gov/resource/x.json")

async def feed():
    async with aiohttp.ClientSession() as session:
        await session.post("https://example.org/ingest", json={})

def oneshot():
    return httpx.request("GET", "https://api.example.com/v1")

def client():
    return httpx.Client(base_url="https://partner.example.net", trust_env=False)

async def sock():
    return await websockets.connect("wss://stream.example.com/socket")

def partial(tld):
    return httpx.get(f"https://api.{tld}/x", trust_env=False)
"""
    hosts = {host for _, _, host in _off_appliance_calls(flagged)}
    assert hosts == {
        "api.open-meteo.com", "api.fda.gov", "data.cdc.gov", "example.org",
        "api.example.com", "partner.example.net", "stream.example.com", "api.",
    }, hosts

    allowed = """
import httpx, urllib.request
from config import OLLAMA_BASE_URL

LOINC = "http://loinc.org"
SNOMED_SYSTEM = "http://snomed.info/sct"
OLLAMA_URL = "http://localhost:11434/api/generate"

def coding(code):
    return {"system": LOINC, "code": code, "url": "https://hl7.org/fhir"}

def lookup(table):
    return table.get("status")

async def ps():
    async with httpx.AsyncClient(trust_env=False) as client:
        await client.get(f"{OLLAMA_BASE_URL}/api/ps")
        await client.get("http://127.0.0.1:8000/api/health")
        await client.get("http://[::1]:11434/api/tags")
        await client.get(f"http://localhost:{8000}/x")

def generate():
    return urllib.request.urlopen(urllib.request.Request(OLLAMA_URL))
"""
    assert _off_appliance_calls(allowed) == []


def test_url_defaults_in_the_environment_are_loopback():
    """`os.getenv("ORB_X_URL", "<default>")`: a default that names a host off
    the appliance would make a fresh install reach out before anyone set it."""
    offenders = []
    for path in _offline_files():
        rel = path.relative_to(SERVER_ROOT).as_posix()
        tree = ast.parse(path.read_text(), filename=rel)
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr in {"getenv", "get"} and len(node.args) >= 2):
                continue
            receiver = node.func.value
            is_env = (isinstance(receiver, ast.Name) and receiver.id == "os") or (
                isinstance(receiver, ast.Attribute) and receiver.attr == "environ")
            if not is_env:
                continue
            for text, whole in _url_prefixes(node.args[1], {}):
                m = _URL_RE.match(text)
                if m and m.group(1):
                    complete = whole or len(m.group(0)) < len(text)
                    if not _is_loopback(m.group(1), complete):
                        offenders.append(f"  {rel}:{node.lineno}  default {text!r}")
    assert not offenders, (
        "\nThese settings default to a host off the appliance. Orb is strictly "
        "offline; a default must be loopback:\n" + "\n".join(offenders)
    )
