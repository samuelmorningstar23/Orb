"""Orb Hospital OS — Configuration"""
import os
from pathlib import Path

# ── Strictly offline (standing rule, 4 October 2026) ──
# Hugging Face libraries download a model the moment it is missing from the local
# cache; the knowledge embedder and reranker loaders fall back to a plain model
# name for exactly that. Set before anything imports huggingface_hub or
# transformers, and inherited by every worker process Orb starts, so a missing
# model fails fast instead of reaching the internet. setdefault, so provisioning
# can still pass HF_HUB_OFFLINE=0 explicitly on a machine that is not yet on the
# hospital network (install runbook, section 8).
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

# ── Deployment profile ──
# Resolved FIRST because the fail-closed secret guards below key off it. It is
# stripped (systemd/.env values routinely carry trailing whitespace or CRLF) and
# validated against the allowed set, so a typo like "production" can never be
# mistaken for a non-prod profile and silently unlock the dev JWT secret, the dev
# ingestion key, wildcard CORS and auto-reload on a real PHI deployment.
DEPLOYMENT_PROFILE = os.getenv("ORB_DEPLOYMENT_PROFILE", "prod").strip().lower()
if DEPLOYMENT_PROFILE not in ("dev", "prod"):
    raise ValueError(
        f"ORB_DEPLOYMENT_PROFILE must be 'dev' or 'prod', got {DEPLOYMENT_PROFILE!r}"
    )

# Paths
BASE_DIR = Path(__file__).resolve().parent
# DB_PATH is env-overridable so tests can point at a tmp/in-memory DB instead of
# mutating the live orb.db. Callers (database.py get_db/init_db) read this symbol;
# tests must set ORB_DB_PATH before first import of config to take effect.
DB_PATH = Path(os.getenv("ORB_DB_PATH", str(BASE_DIR / "orb.db")))
# Encryption at rest for both: orb.db through SQLCipher when ORB_DB_ENCRYPTION_KEY is
# set (refused, not plaintext, if the driver is missing), and each upload through
# AES-256-GCM (services/crypto_service; required in the prod profile). Still open:
# the keys live in .env, so FileVault and the key-escrow ceremony carry the rest.
UPLOAD_DIR = BASE_DIR / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)

# THE canonical backup directory: <repo root>/backups, NOT <db parent>/backups.
#
# WHY THIS IS SPELLED OUT (2026-08-15)
# ------------------------------------
# Five places resolved this independently and split two ways. The shell — the
# scripts that actually take and consume backups (scripts/backup.sh,
# restore-drill.sh, standby-restore.sh) — all defaulted to <repo>/backups. The
# Python — backup_db.py, backup_routes.py, ops_health_service.py — defaulted to
# `DB_PATH.parent / "backups"`, and on the stock layout the database is at
# server/orb.db, so that is server/backups. Two real directories, diverged:
# server/backups/ held five schema-v4 artefacts from 2026-08-01 while every real
# nightly backup landed in <repo>/backups.
#
# That split is not cosmetic. It gave the appliance two ways to lie:
#   * the admin "create backup" button (routes/backup_routes.py) wrote into a
#     directory the nightly rotation, the drill and the warm standby never look
#     at — so a manual backup could freshen the ops status page while the real
#     backup directory rotted untouched;
#   * the ops status page graded a directory nothing writes to, which is the
#     "there is no backup at all" false alarm documented at
#     docs/guide/40-ops-health-and-site-config.md.
#
# Anchoring to BASE_DIR.parent rather than DB_PATH.parent means moving the
# database with ORB_DB_PATH (which tests do constantly) no longer silently moves
# where backups are looked for. ORB_BACKUP_DIR still overrides both.
#
# server/tests/test_warm_standby.py::test_every_backup_dir_resolver_agrees pins
# every resolver in the repo to this one, so the split cannot reopen quietly.
BACKUP_DIR = Path(os.getenv("ORB_BACKUP_DIR") or (BASE_DIR.parent / "backups"))

# Server
# Bind address is env-driven with a safe loopback default. Binding to 0.0.0.0 exposes
# the server on every interface, in cleartext unless ORB_TLS_CERT/ORB_TLS_KEY are set
# (main.py then serves HTTPS) or a proxy terminates TLS, so it must be opted into
# explicitly via ORB_HOST when LAN/cluster exposure is actually intended.
HOST = os.getenv("ORB_HOST", "127.0.0.1")
PORT = int(os.getenv("ORB_PORT", "8000"))
# CORS origins are env-driven (comma-separated) so prod can lock them down rather than
# shipping a permissive list. http:// defaults are dev-only; serve via https in prod.
CORS_ORIGINS = [
    o.strip()
    for o in os.getenv(
        "ORB_CORS_ORIGINS",
        "http://localhost:5173,http://localhost:5174,http://127.0.0.1:5173",
    ).split(",")
    if o.strip()
]


def cors_allow_origins() -> list[str]:
    """The origins the CORS middleware actually installs. main.py and the Trust
    Center both read this, so the posture report cannot describe an allow-list
    while the dev branch is serving `*`. Read at call time for tests."""
    return list(CORS_ORIGINS) if DEPLOYMENT_PROFILE == "prod" else ["*"]


# Host names the backend answers to (services/host_gate.py). A request whose Host
# header is a name not in this set gets a 400 before any route or the socket sees
# it. That is the DNS-rebinding fence: a page on attacker.example that re-points
# its own name at the appliance is same-origin to the browser, so CORS never
# applies, but its requests still say Host: attacker.example.
#
# Any IP address passes without being listed (rebinding needs a name, and a
# browser sends an IP as the Host only when it loaded the page from that IP), so
# the set only holds names. It is derived from settings an install already has,
# so a working install is not locked out: localhost, the hosts in ORB_CORS_ORIGINS
# and ORB_PUBLIC_APP_BASE_URL, ORB_HOST when it is a name, and this machine's own
# name with its mDNS .local form. ORB_ALLOWED_HOSTS ADDS names (host, host:port
# or a URL all work); "*" there turns the check off and the Trust Center shows it
# red. A "*" in ORB_CORS_ORIGINS does not: that answers a different question.
ALLOWED_HOSTS_EXTRA = [h.strip() for h in os.getenv("ORB_ALLOWED_HOSTS", "").split(",") if h.strip()]


def _host_name(value: str) -> str | None:
    """The host NAME in a URL or a bare `host[:port]`, lowercased, trailing dot
    dropped. None for an IP address or anything that is not a usable name."""
    import ipaddress
    import re
    from urllib.parse import urlsplit

    text = (value or "").strip()
    try:
        host = urlsplit(text if "//" in text else "//" + text).hostname
    except ValueError:
        return None
    host = (host or "").rstrip(".")
    try:
        ipaddress.ip_address(host)
        return None
    except ValueError:
        pass
    return host if re.fullmatch(r"[a-z0-9_-]+(\.[a-z0-9_-]+)*", host) else None


def allowed_host_names() -> frozenset[str]:
    """The names services/host_gate.py admits; contains "*" when the check is off.
    main.py installs this set and the Trust Center reports it. Read at call time
    for tests."""
    import socket

    names = {"localhost"}
    sources = [o for o in CORS_ORIGINS if o != "*"] + [PUBLIC_APP_BASE_URL, HOST]
    for value in sources + [h for h in ALLOWED_HOSTS_EXTRA if h != "*"]:
        name = _host_name(value)
        if name:
            names.add(name)
    try:
        own = _host_name(socket.gethostname())
    except OSError:
        own = None
    if own:
        label = own.split(".")[0]
        names.update({own, label, label + ".local"})
    if "*" in ALLOWED_HOSTS_EXTRA:
        names.add("*")
    return frozenset(names)

# TLS in transit. Orb serves HTTPS itself when both are set (main.py); otherwise
# a reverse proxy must terminate TLS, and ORB_TLS_TERMINATED_UPSTREAM=1 is the
# operator's declaration that one does. The Trust Center inspects the cert.
TLS_CERT = os.getenv("ORB_TLS_CERT") or None
TLS_KEY = os.getenv("ORB_TLS_KEY") or None
TLS_TERMINATED_UPSTREAM = os.getenv("ORB_TLS_TERMINATED_UPSTREAM", "").strip().lower() in (
    "1", "true", "yes", "on")

# Auth
JWT_SECRET = os.getenv("ORB_JWT_SECRET")
if not JWT_SECRET:
    import sys
    is_pytest = "pytest" in sys.modules or os.getenv("PYTEST_CURRENT_TEST") is not None
    # Fail closed on anything that is not the explicit dev profile.
    if DEPLOYMENT_PROFILE != "dev" and not is_pytest:
        raise ValueError("CRITICAL SECURITY ERROR: ORB_JWT_SECRET environment variable must be set in production mode!")
    JWT_SECRET = "orb-local-dev-secret-key-change-in-prod"
JWT_ALGORITHM = "HS256"
JWT_EXPIRY_HOURS = 24

# Signs the patient discharge-portal grants. A key of its own, so a leak or a
# rotation of the staff secret does not reach patient sessions and the reverse.
# Required in prod like JWT_SECRET; the dev fallback is distinct from the staff
# one, and the grant's aud='discharge' check stays as a second fence.
DISCHARGE_JWT_SECRET = os.getenv("ORB_DISCHARGE_JWT_SECRET")
if not DISCHARGE_JWT_SECRET:
    import sys
    _is_pytest = "pytest" in sys.modules or os.getenv("PYTEST_CURRENT_TEST") is not None
    if DEPLOYMENT_PROFILE != "dev" and not _is_pytest:
        raise ValueError(
            "CRITICAL SECURITY ERROR: ORB_DISCHARGE_JWT_SECRET must be set in "
            "production mode (generate it with: openssl rand -hex 32)."
        )
    DISCHARGE_JWT_SECRET = "orb-local-dev-discharge-secret-change-in-prod"
elif DISCHARGE_JWT_SECRET == JWT_SECRET and DEPLOYMENT_PROFILE == "prod":
    raise ValueError(
        "CRITICAL SECURITY ERROR: ORB_DISCHARGE_JWT_SECRET must differ from ORB_JWT_SECRET."
    )

# Multi-factor auth (TOTP). Optional per-user; issuer shown in authenticator apps.
MFA_ISSUER = os.getenv("ORB_MFA_ISSUER", "Orb Hospital OS")
# Require MFA for privileged roles once they have enrolled (enforced in auth).
MFA_REQUIRED_ROLES = [
    r.strip() for r in os.getenv("ORB_MFA_REQUIRED_ROLES", "").split(",") if r.strip()
]

# Encryption at rest.
# ORB_DB_ENCRYPTION_KEY: when set, the SQLite DB is opened via SQLCipher
# (encrypted). With the key set and no SQLCipher driver, Orb refuses to open the
# database rather than fall back to plaintext, and boot checks the connection's
# cipher version (database._open_raw_connection, verify_encryption_at_rest).
DB_ENCRYPTION_KEY = os.getenv("ORB_DB_ENCRYPTION_KEY")
# ORB_UPLOAD_ENCRYPTION_KEY: when set, uploaded PHI media (Lens images, Scribe
# audio) are encrypted at rest with AES-256-GCM (see services/crypto_service).
UPLOAD_ENCRYPTION_KEY = os.getenv("ORB_UPLOAD_ENCRYPTION_KEY")


# Ollama
# Env-driven so the app can reach an Ollama that is not on localhost, such as a
# remote GPU box. On the appliance it runs on the same machine. Trailing slash is
# stripped because every call site interpolates f"{OLLAMA_BASE_URL}/api/...".
OLLAMA_BASE_URL = os.getenv("ORB_OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/")
# Single shared MedGemma 1 27B Multimodal instance for all modules (Sage, Lens, Bridge, Scribe).
# Each service passes its own system prompt and options per-request.
# This keeps exactly ONE model loaded in VRAM (~17GB) instead of two separate instances.

_installed_cache: "list[str] | None | object" = ...  # ... = not yet probed


def _installed_model_names() -> "list[str] | None":
    """Model names Ollama has PULLED (`/api/tags`), or None when unreachable.

    Cached for the life of the process: this runs at import time and is consulted
    by every tier resolver below, and two blocking urlopen calls in module import
    is two chances to add 2 s to boot for no new information. `None` (unreachable)
    and `[]` (reachable, nothing pulled) stay distinct — the resolvers treat both
    as "cannot verify", but only None means the engine did not answer.
    """
    global _installed_cache
    if _installed_cache is not ...:
        return _installed_cache  # type: ignore[return-value]
    try:
        import json as _json
        import urllib.request
        with urllib.request.urlopen(f"{OLLAMA_BASE_URL}/api/tags", timeout=2) as resp:
            _installed_cache = [m.get("name", "") for m in _json.load(resp).get("models", [])]
    except Exception:
        _installed_cache = None
    return _installed_cache  # type: ignore[return-value]


def _resolve_llm_model(preferred: str) -> str:
    """Return `preferred` if it is pulled in Ollama; otherwise fall back to an
    installed medgemma variant. Modules `from config import LLM_MODEL` by value,
    so this must resolve at import time — a missing model otherwise 404s every
    AI feature (Sage, Lens, Vigil analysis) with no visible error in the UI."""
    installed = _installed_model_names()
    if installed is None:
        return preferred  # Ollama unreachable — keep the configured name
    if not installed:
        return preferred
    base = preferred.split(":")[0]
    if any(name == preferred or name.split(":")[0] == base for name in installed):
        return preferred
    fallback = next((n for n in installed if "medgemma" in n.lower()), None)
    if fallback:
        print(f"  ! LLM model '{preferred}' not pulled in Ollama — falling back to '{fallback}'. "
              f"Set ORB_LLM_MODEL or `ollama pull {preferred}` to override.")
        return fallback
    return preferred

LLM_MODEL = _resolve_llm_model(os.getenv("ORB_LLM_MODEL", "medgemma:27b"))
# Tiered models: the CHEAP tier serves the bounded, non-interactive ambient work
# (Relay message classification, background extraction) so the big interactive
# model stays free for clinicians. Defaults to the same model (no behaviour
# change) until an operator points it at a smaller quantized model, e.g.
# ORB_AMBIENT_LLM_MODEL=medgemma:4b. Reduces contention on the single GPU slot.
AMBIENT_LLM_MODEL = os.getenv("ORB_AMBIENT_LLM_MODEL", LLM_MODEL)
# Residency fallback: when the primary tier is NOT resident in Ollama (an
# operator evicted it from the header AI panel to reclaim memory), route the
# turn to AMBIENT_LLM_MODEL instead of asking Ollama to transparently re-load
# ~17 GB — a measured ~80 s to first token that also silently undoes the
# operator's reclaim. The answer is labelled as coming from the smaller tier.
# Set false to always use the primary model, cold-load cost included.
MODEL_FALLBACK_ENABLED = os.getenv("ORB_MODEL_FALLBACK_ENABLED", "true").lower() == "true"
# Micro-batching window (ms) for coalescing concurrent ambient classify calls
# into fewer GPU passes. 0 disables batching (each call runs alone).
AMBIENT_BATCH_WINDOW_MS = int(os.getenv("ORB_AMBIENT_BATCH_WINDOW_MS", "150"))
AMBIENT_BATCH_MAX = int(os.getenv("ORB_AMBIENT_BATCH_MAX", "8"))

# ── THINKING MODE — the clinician-facing effort switch ───────────────────────
# One control a doctor actually understands: think harder, or answer fast.
#   thinking ON  -> LLM_MODEL          (medgemma:27b — the careful model)
#   thinking OFF -> THINKING_FAST_MODEL (medgemma:4b — the fast model)
# The decision logic, the safety floor and the per-turn labelling all live in
# services/thinking_mode.py; these are only its knobs.
#
# DEFAULT IS ON, and an absent/unparsable flag resolves to ON. An old client, a
# scripted caller, or a dropped JSON field must never quietly downgrade a
# clinician's answer to the model that is wrong 40% of the time on drug recall.
THINKING_DEFAULT_ON = os.getenv("ORB_THINKING_DEFAULT", "on").strip().lower() not in (
    "0", "false", "off", "no",
)
# Master switch for offering the toggle at all. OFF pins every Sage turn to the
# careful model regardless of what the client sends — the panic switch for a
# site that decides the fast lane is not for them.
THINKING_FAST_MODE_ENABLED = os.getenv("ORB_FAST_MODE_ENABLED", "true").strip().lower() not in (
    "0", "false", "off", "no",
)


def _resolve_fast_model(preferred: str) -> str:
    """Pick the model that serves 'thinking OFF', erring toward the CAREFUL one.

    Fallback chain, and the reason for its direction: an unavailable fast tier
    must degrade to a slower CORRECT answer, never to a 404 or to a model that
    isn't there. So —
      * `preferred` is pulled          -> use it;
      * an operator declared an ambient
        tier distinct from LLM_MODEL   -> use that (they already chose a small tier);
      * otherwise / Ollama unreachable -> LLM_MODEL, i.e. fast mode becomes a
                                          no-op and the toggle is inert but safe.
    Matching is EXACT on the tag. `_resolve_llm_model` matches on the base name
    ("medgemma"), which is right for the primary tier and wrong here: it would
    happily return "medgemma:4b" on a box that has only medgemma:27b pulled, and
    every fast turn would 404.
    """
    installed = _installed_model_names()
    if installed and preferred in installed:
        return preferred
    if AMBIENT_LLM_MODEL and AMBIENT_LLM_MODEL != LLM_MODEL:
        if installed is None or AMBIENT_LLM_MODEL in installed:
            return AMBIENT_LLM_MODEL
    if installed is not None:
        print(f"  ! Fast-mode model '{preferred}' is not pulled in Ollama — thinking-OFF "
              f"will be served by '{LLM_MODEL}' (no speed win, but no wrong answers). "
              f"`ollama pull {preferred}` or set ORB_FAST_MODEL.")
    return LLM_MODEL


THINKING_FAST_MODEL = _resolve_fast_model(os.getenv("ORB_FAST_MODEL", "medgemma:4b"))

# ── THE SAFETY FLOOR — read services/thinking_mode.py before touching this ────
# When ON (the default), certain question classes are routed to the CAREFUL
# model even when the clinician has thinking OFF, because the fast model has
# been measured getting them dangerously wrong on this appliance. The
# measurements and the exact rules are documented at the definition in
# services/thinking_mode.py — that comment is the reason this switch defaults to
# on, and it is what you must read before setting this to 0.
#   ORB_FAST_MODE_SAFETY_FLOOR=0   disables the floor (fast mode answers
#                                  everything, including dosing and patient
#                                  questions). Do not do this on a pilot ward.
FAST_MODE_SAFETY_FLOOR = os.getenv("ORB_FAST_MODE_SAFETY_FLOOR", "1").strip().lower() not in (
    "0", "false", "off", "no",
)

# ── Background LLM volume bound (services/llm_queue_service.BackgroundGovernor) ──
# The reserved interactive lane fixed QUEUE WAIT, not GPU CONTENTION. Measured
# 2026-08-01 under a 15-patient ward alert storm: the interactive lane was
# acquired immediately with bg=0 and still held 19,973 ms, because the ambient
# 4B was running 554 background calls (peak 57/min, 76% GPU duty) in a SEPARATE
# Ollama process. At >60% overlap that costs prefill 8% and decode 26%. The
# lever is therefore background VOLUME, and these are its knobs.
#
# BG_LLM_GOVERNOR_ENABLED=false restores the pre-2026-08-02 behaviour exactly
# (every background call is submitted, nothing is coalesced or shed).
#
# Default OFF under pytest (same is_pytest pattern as the JWT/ingestion guards):
# the governor is a WALL-CLOCK rate limiter, and the suite compresses hours of
# simulated traffic into a real minute — with the 12/min ceiling live, unrelated
# tests' background acquisitions would shed each other. Governor tests enable it
# explicitly (tests/test_background_governor.py); ORB_BG_LLM_GOVERNOR still
# overrides in both directions.
import sys as _sys
_BG_GOV_DEFAULT = (
    "false"
    if ("pytest" in _sys.modules or os.getenv("PYTEST_CURRENT_TEST") is not None)
    else "true"
)
BG_LLM_GOVERNOR_ENABLED = os.getenv("ORB_BG_LLM_GOVERNOR", _BG_GOV_DEFAULT).lower() not in ("0", "false", "off")
# Ceiling on ADMITTED background LLM calls per rolling 60 s, across all
# background producers. 12/min ≈ one every 5 s, which is what the ambient tier
# can serve without holding the GPU for most of the minute.
BG_LLM_MAX_PER_MIN = int(os.getenv("ORB_BG_LLM_MAX_PER_MIN", "12"))
# How long a background caller will wait for a clinician's turn to finish before
# giving up and taking its deterministic fallback. Bounded on purpose: a
# background job that waits forever is a background job that eventually runs at
# the worst possible moment.
BG_LLM_YIELD_WAIT_SECONDS = float(os.getenv("ORB_BG_LLM_YIELD_WAIT_SECONDS", "3.0"))
# Quiet tail after the last interactive turn releases. A clinician reading an
# answer is usually about to ask a follow-up; resuming background GPU work the
# instant the stream ends puts the storm back under the next question.
BG_LLM_QUIET_AFTER_INTERACTIVE_SECONDS = float(
    os.getenv("ORB_BG_LLM_QUIET_AFTER_INTERACTIVE_SECONDS", "2.0")
)

# ── Sage conversation-history summarization (services/sage_history_service.py) ──
# History is the dominant and the only GROWING term of the Sage prompt (604 →
# 1,238 tokens measured across a session; 6 msgs × 1,000 chars on the
# patient/clinical lanes, and each 1,000 chars costs ~1 s of prefill on this
# appliance). When ON, the 2 most recent history messages stay verbatim and
# older ones are replaced by ≤300-char summaries produced ASYNCHRONOUSLY (P3
# background, careful tier, fact-guarded — see sage_history_service.py). A turn
# whose summary is not ready falls back to today's deterministic head-trim.
#
# DEFAULT OFF, and the default is the point: the continuity evals (Eval Groups
# 3+4, the 40-turn suites) have NOT been re-run against summarized history.
# Shipping this default-on unverified would put an untested change in front of
# what the clinical model reads on every follow-up — the same bar that keeps a
# green-but-unproven test out of this repo. Flip ORB_SAGE_HISTORY_SUMMARIZE=1
# only AFTER those evals pass on the summarized path. When OFF the assembled
# prompt is byte-identical to today's (regression-pinned in
# tests/test_sage_history_summarize.py).
SAGE_HISTORY_SUMMARIZE = os.getenv("ORB_SAGE_HISTORY_SUMMARIZE", "0").strip().lower() in (
    "1", "true", "yes", "on",
)

VISION_MODEL = os.getenv("ORB_VISION_MODEL", LLM_MODEL)  # Shares same model instance as LLM
OLLAMA_KEEP_ALIVE_RAW = os.getenv("ORB_OLLAMA_KEEP_ALIVE", "1h")
try:
    OLLAMA_KEEP_ALIVE = int(OLLAMA_KEEP_ALIVE_RAW)
except ValueError:
    OLLAMA_KEEP_ALIVE = OLLAMA_KEEP_ALIVE_RAW

# ── AI egress posture ──
# Orb's thesis is "PHI never leaves the building": the local Ollama/MedGemma
# instance configured above is the ONLY inference engine, so no model call can
# carry patient data off the box.
#
# There is no browser or cloud speech recognition, and no switch for it: Orb is
# strictly offline (owner ruling, 4 October 2026). Speech is transcribed by the
# local Whisper lane only (SCRIBE["asr"], VOICE["daemon"] below).

# Embeddings
EMBEDDING_MODEL = "BAAI/bge-m3"
EMBEDDING_DIM = 1024  # BGE-M3 output dimension

# Whisper (Scribe) — legacy top-level vars kept for backward compat
WHISPER_MODEL = "large-v3"
WHISPER_BATCH_SIZE = 12

# ── Deployment Profile ──
# DEPLOYMENT_PROFILE is resolved and validated at the top of this module (the
# secret guards above depend on it).

# Profile-based defaults
is_dev = DEPLOYMENT_PROFILE == "dev"

# Demo mode: permits synthetic/simulated clinical CONTENT (e.g. the mock post-op
# note, and the report and lab bots in services/bot_service, which check it
# before every write). ORB_INGESTION_MODE (simulation or hybrid) is what starts
# the simulator at all; the vitals simulator needs only that, and its rows carry
# their own "simulated" label. The report and lab bots need BOTH, and every row
# they write is marked source = 'simulated'. Defaults ON in the dev profile and
# OFF in prod, so fabricated results can never be written into a real
# deployment. Override explicitly with ORB_DEMO_MODE.
DEMO_MODE = os.getenv("ORB_DEMO_MODE", "true" if is_dev else "false").strip().lower() in ("1", "true", "yes", "on")

# Lens (medical image analysis) ships NO validated imaging model — it is a
# general-purpose vision LLM writing reference prose, in the most regulated space
# Orb touches. It is therefore gated demo-only: enabled in dev/demo, and OFF in
# prod unless an operator explicitly opts in (ORB_LENS_ENABLED), accepting that it
# is clinician decision-support reference only and never a diagnostic device.
LENS_ENABLED = os.getenv("ORB_LENS_ENABLED", "true" if is_dev else "false").strip().lower() in ("1", "true", "yes", "on")

# The dev profile's cheap Scribe stages run on the fast 4B through Ollama, the
# same as .env sets on the dev box. They used to default to an MLX path for
# MedGemma 1.5 4B whose weights were never downloaded, so a fresh dev install
# without those .env keys pointed correction at nothing. Not 1.5 through Ollama
# either: it is a reasoning model, too slow for a cheap stage (see SETUP-NOTES.md).
# THINKING_FAST_MODEL is matched on the exact tag and falls back to LLM_MODEL
# when the 4B is not pulled. The voice agent's MLX path (VOICE below) is separate.
DEFAULT_CORRECTION_MODEL = THINKING_FAST_MODEL if is_dev else LLM_MODEL
DEFAULT_CORRECTION_PROVIDER = "ollama"
DEFAULT_DRAFT_MODEL = LLM_MODEL  # Drafting always prioritizes quality
DEFAULT_VERIFY_MODEL = THINKING_FAST_MODEL if is_dev else LLM_MODEL
DEFAULT_VERIFY_PROVIDER = "ollama"
DEFAULT_VERIFY_ENABLED = "false"

# ── Scribe Pipeline Configuration ──
# Each stage is independently configurable via env vars.
SCRIBE = {
    "asr": {
        "model": os.getenv("ORB_SCRIBE_ASR_MODEL", WHISPER_MODEL),
        "provider": os.getenv("ORB_SCRIBE_ASR_PROVIDER", "whisper-mlx"),
        "batch_size": int(os.getenv("ORB_SCRIBE_ASR_BATCH_SIZE", str(WHISPER_BATCH_SIZE))),
        "quant": os.getenv("ORB_SCRIBE_ASR_QUANT", "") or None,
        # Default transcription language. Whisper large-v3 is multilingual, so the
        # India market (Hindi + English code-switching) is supported by selecting
        # a language per session. "auto" lets Whisper detect. Callers may override
        # per request; "supported" is the picker list surfaced in the UI.
        "language": os.getenv("ORB_SCRIBE_ASR_LANGUAGE", "en"),
        "supported": [
            {"code": "en", "label": "English"},
            {"code": "hi", "label": "Hindi (हिन्दी)"},
            {"code": "auto", "label": "Auto-detect / code-switching"},
        ],
    },
    "correction": {
        "enabled": os.getenv("ORB_SCRIBE_CORRECTION_ENABLED", "true").lower() == "true",
        "model": os.getenv("ORB_SCRIBE_CORRECTION_MODEL", DEFAULT_CORRECTION_MODEL),
        "provider": os.getenv("ORB_SCRIBE_CORRECTION_PROVIDER", DEFAULT_CORRECTION_PROVIDER),
        "temperature": float(os.getenv("ORB_SCRIBE_CORRECTION_TEMP", "0.1")),
        "max_tokens": int(os.getenv("ORB_SCRIBE_CORRECTION_MAX_TOKENS", "2048")),
        "ctx_size": int(os.getenv("ORB_SCRIBE_CORRECTION_CTX", "8192")),
    },
    "draft": {
        "model": os.getenv("ORB_SCRIBE_DRAFT_MODEL", DEFAULT_DRAFT_MODEL),
        "provider": os.getenv("ORB_SCRIBE_DRAFT_PROVIDER", "ollama"),
        "temperature": float(os.getenv("ORB_SCRIBE_DRAFT_TEMP", "0.3")),
        "max_tokens": int(os.getenv("ORB_SCRIBE_DRAFT_MAX_TOKENS", "4096")),
        "ctx_size": int(os.getenv("ORB_SCRIBE_DRAFT_CTX", "8192")),
    },
    "verify": {
        "enabled": os.getenv("ORB_SCRIBE_VERIFY_ENABLED", DEFAULT_VERIFY_ENABLED).lower() == "true",
        "model": os.getenv("ORB_SCRIBE_VERIFY_MODEL", DEFAULT_VERIFY_MODEL),
        "provider": os.getenv("ORB_SCRIBE_VERIFY_PROVIDER", DEFAULT_VERIFY_PROVIDER),
        "temperature": float(os.getenv("ORB_SCRIBE_VERIFY_TEMP", "0.1")),
        "max_tokens": int(os.getenv("ORB_SCRIBE_VERIFY_MAX_TOKENS", "1024")),
        "ctx_size": int(os.getenv("ORB_SCRIBE_VERIFY_CTX", "8192")),
    },
}

# Vitals simulation
VITALS_INTERVAL_SECONDS = 5

# Ingestion
# "ingestion"  = real data only, simulation bots disabled (default — safe)
# "simulation" = synthetic bots only (NOT the default; must be opted into explicitly)
# "hybrid"     = both simulation bots and ingestion endpoints active
INGESTION_MODE = os.getenv("ORB_INGESTION_MODE", "ingestion")

# The ingestion API key protects a real-patient-data write path (/api/ingest/*),
# so it must be set explicitly in production — no shipped default. This mirrors the
# fail-closed JWT_SECRET guard above. Callers must compare it with hmac.compare_digest
# (timing-safe); see ingestion_routes.py.
INGESTION_API_KEY = os.getenv("ORB_INGESTION_API_KEY")
if not INGESTION_API_KEY:
    import sys as _sys
    _is_pytest = "pytest" in _sys.modules or os.getenv("PYTEST_CURRENT_TEST") is not None
    if DEPLOYMENT_PROFILE != "dev" and not _is_pytest:
        raise ValueError(
            "CRITICAL SECURITY ERROR: ORB_INGESTION_API_KEY environment variable "
            "must be set in production mode!"
        )
    # Dev/test only fallback. Never use this value in a deployment that handles real data.
    INGESTION_API_KEY = "orb-ingest-dev-key"

# ── Bedside monitor integration (services/vitals_source) ──
# OFF by default and read directly from the environment by
# services.vitals_source.configure_from_env. Until the site visit names the
# ward's monitor make/model there is nothing to point a listener at, and an
# open port on a hospital network with no sender is a liability, not a feature.
#   ORB_MONITOR_ENABLED    1/true to run the monitor feed
#   ORB_MONITOR_PROTOCOL   hl7v2-mllp (only supported value today)
#   ORB_MONITOR_MODE       listen (monitor connects to Orb) | connect (Orb dials out)
#   ORB_MONITOR_HOST/PORT  bind address in listen mode, target in connect mode
#                          (port defaults to 2575, IANA's HL7 MLLP port)
#   ORB_MONITOR_SOURCE_ID  provenance/source id, when a ward runs more than one feed
#
# CLOCKS AND TIMEZONES — set these on the appliance; they are clinical settings.
#   ORB_HL7_DEFAULT_TZ     IANA zone an HL7 timestamp with NO UTC offset is read
#                          in (e.g. Asia/Kolkata). An offset-less DTM is legal
#                          HL7 and is what most bedside monitors emit — they
#                          print local wall-clock time. Defaults to the
#                          appliance's own OS zone. Reading it as UTC in a
#                          UTC+05:30 hospital stamped every reading 5.5 hours in
#                          the FUTURE, corrupting ordering against every feed.
#                          It is also THE hospital clock (services/hospital_time):
#                          shift windows, the surgical schedule date and every
#                          time printed on a sheet or pack. One setting, so the
#                          monitors and the paper cannot disagree.
#   ORB_MONITOR_TZ         per-device override of the above, for a gateway
#                          configured in a different zone from the appliance.
#                          Assuming every device on a ward agrees is how a
#                          timezone bug bites a second time.
#   ORB_MONITOR_CLOCK_SKEW_TOLERANCE_SECONDS
#                          how far a device's clock may differ from ours before
#                          we raise a visible fault (default 900). This does NOT
#                          discard readings or suppress alerting — a monitor with
#                          a wrong clock is an operational fault to be fixed, not
#                          a reason to stop scoring the patient. See
#                          services/vitals_source/sink.py.
MONITOR_ENABLED = os.getenv("ORB_MONITOR_ENABLED", "false").strip().lower() in ("1", "true", "yes", "on")
MONITOR_PROTOCOL = os.getenv("ORB_MONITOR_PROTOCOL", "hl7v2-mllp")
MONITOR_MODE = os.getenv("ORB_MONITOR_MODE", "listen")
MONITOR_HOST = os.getenv("ORB_MONITOR_HOST", "127.0.0.1")
MONITOR_PORT = int(os.getenv("ORB_MONITOR_PORT", "2575"))
MONITOR_TZ = os.getenv("ORB_MONITOR_TZ") or None
HL7_DEFAULT_TZ = os.getenv("ORB_HL7_DEFAULT_TZ") or None

# Audit configuration
AUDIT_SIMULATION_EVENTS = os.getenv("ORB_AUDIT_SIMULATION_EVENTS", "false").lower() == "true"

# Discharge Access
DISCHARGE_ACCESS_TTL_DAYS = int(os.getenv("ORB_DISCHARGE_ACCESS_TTL_DAYS", "30"))
DISCHARGE_ACCESS_MAX_ATTEMPTS = int(os.getenv("ORB_DISCHARGE_ACCESS_MAX_ATTEMPTS", "5"))
DISCHARGE_ACCESS_LOCK_MINUTES = int(os.getenv("ORB_DISCHARGE_ACCESS_LOCK_MINUTES", "15"))
PUBLIC_APP_BASE_URL = os.getenv("ORB_PUBLIC_APP_BASE_URL", "http://localhost:5173")
# Ruling 2026-10-04 (option A): the appliance never answers from outside the
# hospital, so the discharge QR opens only on a network routed to it, and the
# patient saves a copy before leaving. A hospital with no patient Wi-Fi routed to
# the appliance sets this false and hands over the printed summary instead. Off
# is a decision, not a fault: no link is issued, no boot error, and the Trust
# Center row passes. See services/discharge_access_service.public_link_base_status.
DISCHARGE_LINK_ENABLED = os.getenv("ORB_DISCHARGE_LINK_ENABLED", "true").strip().lower() not in (
    "0", "false", "no", "off")

# ── Sage Ambient ──
SAGE_AMBIENT_ENABLED = os.getenv("ORB_SAGE_AMBIENT", "true").lower() == "true"
SAGE_AMBIENT_DEBOUNCE_SECONDS = int(os.getenv("ORB_SAGE_AMBIENT_DEBOUNCE", "3"))
SAGE_AMBIENT_CONTEXT_WINDOW = int(os.getenv("ORB_SAGE_AMBIENT_WINDOW", "15"))
SAGE_AMBIENT_MAX_SUMMARY_CHARS = int(os.getenv("ORB_SAGE_AMBIENT_MAX_SUMMARY", "1500"))
SAGE_AMBIENT_MAX_INTENTS = int(os.getenv("ORB_SAGE_AMBIENT_MAX_INTENTS", "8"))
SAGE_AMBIENT_TIMEOUT_SECONDS = int(os.getenv("ORB_SAGE_AMBIENT_TIMEOUT", "30"))
SAGE_AMBIENT_SAMPLE_RATE = float(os.getenv("ORB_SAGE_AMBIENT_SAMPLE_RATE", "1.0"))

# ── Sage chart-open PREWARM ──
# When a clinician opens a chart, Orb already pre-assembles that patient's facts
# into sage_decision_cache (nervous_system_triggers.warm_patient_context). The
# prewarm goes one step further and pushes the assembled system prompt + chart
# block through the primary model as a 1-TOKEN request, so the KV cache holds
# that prefix before the clinician has finished typing. Ollama's reuse is
# prefix-anchored, so the first real question then pays for its question only.
#
# Bounded deliberately: chart-open ONLY (never the critical-alert warm path,
# which re-fires per patient per vitals cycle and would become exactly the
# background storm this codebase has fought twice), one at a time per patient,
# and not more often than PREWARM_MIN_INTERVAL for the same patient.
SAGE_PREWARM_ENABLED = os.getenv("ORB_SAGE_PREWARM", "true").lower() == "true"
SAGE_PREWARM_MIN_INTERVAL_SECONDS = int(os.getenv("ORB_SAGE_PREWARM_MIN_INTERVAL", "45"))
SAGE_PREWARM_TIMEOUT_SECONDS = int(os.getenv("ORB_SAGE_PREWARM_TIMEOUT", "90"))
# How long a prewarm may WAIT for the background lane before giving up. It runs
# at background priority, so it can queue behind a long job (measured on this
# box: a ward-narrator/timeline holder kept the background lane for 170 s). A
# prewarm that lands after the clinician has already asked their question is
# pure waste — it prefills a prefix that turn just paid for. Give up instead.
SAGE_PREWARM_QUEUE_WAIT_SECONDS = int(os.getenv("ORB_SAGE_PREWARM_QUEUE_WAIT", "25"))

# ── Voice Intelligence Pipeline ──
VOICE = {
    "daemon": {
        "socket": os.getenv("ORB_WHISPER_SOCKET", "ipc:///tmp/orb-whisper.sock"),
        "model": os.getenv("ORB_WHISPER_STREAMING_MODEL", WHISPER_MODEL),
        "timeout_ms": int(os.getenv("ORB_WHISPER_TIMEOUT_MS", "5000")),
        "enabled": os.getenv("ORB_WHISPER_DAEMON_ENABLED", "true").lower() == "true",
    },
    "vad": {
        "model_path": str(BASE_DIR / "models" / "silero-vad" / "silero_vad.onnx"),
        "threshold": float(os.getenv("ORB_VAD_THRESHOLD", "0.5")),
        "min_speech_ms": int(os.getenv("ORB_VAD_MIN_SPEECH_MS", "250")),
        "min_silence_ms": int(os.getenv("ORB_VAD_MIN_SILENCE_MS", "300")),
    },
    "corrector": {
        "rxnorm_db": str(BASE_DIR / "datasets" / "rxnorm" / "rxnorm_drugs.db"),
        "rxnorm_csv": str(BASE_DIR / "datasets" / "rxnorm" / "rxnorm_drugs.csv"),
        "max_edit_distance": int(os.getenv("ORB_CORRECTOR_MAX_EDIT", "2")),
        "enabled": os.getenv("ORB_CORRECTOR_ENABLED", "true").lower() == "true",
    },
    "conversational": {
        # Default MedGemma Model: "medgemma-1.5-4b-it"
        # After running fine-tuning (fuse_medgemma.sh), switch to: "medgemma-1.5-4b-it-fused"
        "model_path": str(BASE_DIR / "models" / "medgemma-1.5-4b-it"),
        
        # After running train_whisper.sh, specify the Whisper LoRA adapter path here if using mlx_whisper
        "whisper_adapter_path": str(BASE_DIR / "adapters" / "whisper-lora"),
        
        "max_tokens": int(os.getenv("ORB_VOICE_AGENT_MAX_TOKENS", "256")),
        "temperature": float(os.getenv("ORB_VOICE_AGENT_TEMP", "0.3")),
        # DEFAULT OFF — deliberately. This flag gates ONE thing: the
        # conversational Q&A lane, POST /api/sage/voice (routes/sage_routes.py).
        # That lane answers CLINICAL questions on the 4B and SPEAKS the answer.
        #
        # Two reasons it must not default on for a real ward:
        #   1. It violates the team's own written policy. docs/AUGUST_SAGE_WORKFLOW.md:72
        #      "Decision: no clinical question goes to the 4B" — the 4B was measured
        #      wrong on 8/20 single-fact recall questions, including a 4 mL/kg sepsis
        #      bolus (one-seventh of the correct dose) and a double-dose hydrocortisone.
        #      The route's own docstring example is a drug question.
        #   2. The spoken lane carries none of the chat lane's trust apparatus. The
        #      SSE terminal frame emits only {ttft_ms, total_ms, tokens} — no sources,
        #      no trust_tier, no citation_basis. The client CANNOT render the
        #      "verify against them before acting" caveat because the server never
        #      sends the evidence. Audio is transient, sounds authoritative, and is
        #      audible to the patient and the next bed.
        #
        # This does NOT disable Scribe dictation: scribe_providers.py calls
        # get_voice_agent()/agent.load() directly and never consults this flag.
        # It also skips the startup preload in main.py:warmup_voice_agent (~3 GB of
        # unified memory), which Scribe re-acquires lazily on first use.
        #
        # Turn it on (ORB_VOICE_AGENT_ENABLED=true) only once the lane runs on the
        # 27B, streams its sources, and refuses dose/drug-identity questions.
        "enabled": os.getenv("ORB_VOICE_AGENT_ENABLED", "false").lower() == "true",
    },
}

# Reranking configuration
SAGE_RERANKING_ENABLED = False


# ── Hospital administration modules ──────────────────────────────────────────
#
# Orb REPLACES the hospital information system, so it has to carry the parts a
# hospital cannot run without: billing, payments, payer/insurance, procurement
# and the front desk. Founder ruling 2026-08-11: build them all NOW and ship them
# VISIBLE, with one switch each so a site that already has (say) a purchasing
# system can hide that module without a code change or a rebuild.
#
# ALL DEFAULT ON. A blank or unset value means ON, because "visible" is the
# ruling and an operator who fat-fingers ORB_BILLING= to empty must land on the
# documented default rather than silently lose the billing screen. Only an
# explicit 0/false/off/no turns a module off.
#
# NONE OF THESE GATE A CLINICAL PATH. Turning every one of them off removes five
# navigation tabs and their routes; medication ordering, administration, vitals,
# alerts, shadow capture, audit and the offline queue are untouched.

_MODULE_OFF_VALUES = ("0", "false", "off", "no")


def _module_enabled(env_name: str) -> bool:
    """Read one module switch. Set the variable to 0 to hide this module for a
    deployment; anything else (including unset and blank) leaves it visible."""
    raw = (os.getenv(env_name) or "").strip().lower()
    if not raw:
        return True
    return raw not in _MODULE_OFF_VALUES


# Patient billing: service/tariff masters, charge capture, invoices.
# Set ORB_BILLING=0 to hide this module for a deployment.
BILLING_ENABLED = _module_enabled("ORB_BILLING")
# Receipts, advances/deposits, refunds, discount approvals, cash counter.
# Set ORB_PAYMENTS=0 to hide this module for a deployment.
PAYMENTS_ENABLED = _module_enabled("ORB_PAYMENTS")
# Payer master, patient policies, TPA/insurer/PM-JAY claims and denials.
# Set ORB_PAYER=0 to hide this module for a deployment.
PAYER_ENABLED = _module_enabled("ORB_PAYER")
# Vendors, non-pharmacy item master, indents, purchase orders, GRN, stock.
# Set ORB_PROCUREMENT=0 to hide this module for a deployment.
PROCUREMENT_ENABLED = _module_enabled("ORB_PROCUREMENT")
# Registration desk, counters, patient financial summary at the window.
# Set ORB_FRONTDESK=0 to hide this module for a deployment.
FRONTDESK_ENABLED = _module_enabled("ORB_FRONTDESK")

# ── ABDM / ABHA rails ────────────────────────────────────────────────────────
#
# ABHA capture, verification records, care-context links and consent artefacts.
# Same switch semantics as the five above: DEFAULT ON, only an explicit
# 0/false/off/no hides it. Set ORB_ABDM=0 for a site that has no interest in the
# national health stack (a purely private OPD chain, say) — the tab disappears
# and /api/abdm/* 404s. No clinical path consults it.
#
# ABDM / ABHA rails: ABHA capture and verification records, care-context
# links and consent artefacts. Orb transmits nothing to any ABDM gateway —
# these are records of steps performed elsewhere, and the module says so.
# Set ORB_ABDM=0 to hide this module for a deployment.
ABDM_ENABLED = _module_enabled("ORB_ABDM")
# Biomedical asset register, preventive-maintenance and CALIBRATION schedules,
# breakdown/downtime log, AMC/CMC contracts.
# Set ORB_BIOMEDICAL=0 to hide this module for a deployment.
#
# A NOTE ON TURNING THIS ONE OFF, because it is not quite like the other five.
# Billing, payments and procurement are things another system can do just as
# well. The calibration register is the record that says whether the number on a
# monitor can be believed — and Orb READS those numbers. A site that switches
# this off is not choosing a different purchasing system; it is choosing to keep
# that record somewhere Orb cannot see. That is a legitimate choice (most
# hospitals keep it in a file cabinet today) and the switch exists for it, but it
# should be a decision somebody makes out loud rather than a default.
BIOMEDICAL_ENABLED = _module_enabled("ORB_BIOMEDICAL")
# Room/bed turnaround state, cleaning requests, turnaround reporting, linen.
# Set ORB_HOUSEKEEPING=0 to hide this module for a deployment.
#
# Turning it off removes one tab and its routes. It does NOT change any capacity
# or forecast number, because this module never fed one — see the CAPACITY
# section of services/housekeeping_service.py.
HOUSEKEEPING_ENABLED = _module_enabled("ORB_HOUSEKEEPING")

# ── Expected turnaround, in minutes ──────────────────────────────────────────
#
# THESE ARE DEFAULTS A SITE MUST REVIEW, NOT MEASUREMENTS. They are the target a
# cleaning task is judged against ("overdue by 20 min"), and they are the one
# place in this module where a number is asserted rather than observed. Two of
# them, because a terminal clean after an isolation patient is a materially
# longer job than wiping down a bed space — contact time for the disinfectant,
# curtain and linen change, PPE on and off — and a board that expects both to
# take 45 minutes will report every isolation room as late.
#
# The DEFAULTS below are ordinary hospital practice, not this hospital's data.
# Once the module has run for a few weeks the turnaround report shows a site its
# real median and p90, and the site should set these to what it intends to
# achieve. Until then the panel labels them "site setting".
HOUSEKEEPING_STANDARD_CLEAN_MINUTES = max(1, int(os.getenv("ORB_HK_STANDARD_CLEAN_MINUTES", "45")))
HOUSEKEEPING_TERMINAL_CLEAN_MINUTES = max(1, int(os.getenv("ORB_HK_TERMINAL_CLEAN_MINUTES", "120")))

#: How far back `sync_discharge_turnarounds` looks for discharges that have not
#: yet produced a cleaning task. Bounded because the sweep runs on every board
#: load; a discharge older than this that never produced a task is reported as
#: unmatched rather than silently back-filled days later.
HOUSEKEEPING_DISCHARGE_LOOKBACK_HOURS = max(1, int(os.getenv("ORB_HK_DISCHARGE_LOOKBACK_HOURS", "72")))

# ── Workforce: duty roster, attendance, payroll EXPORT ───────────────────────
#
# Duty roster per department and shift, attendance capture, overtime, and a CSV
# export for the hospital's payroll bureau.
# Set ORB_WORKFORCE=0 to hide this module for a deployment.
#
# WHAT THIS MODULE DELIBERATELY DOES NOT DO, stated here as well as in the
# service, because a config file is where somebody looks for a missing feature:
# it does not compute pay, PF, ESI, Professional Tax or TDS. Those rules change
# with each Union and State budget and getting them wrong is a statutory
# liability, not a bug. Orb owns the roster and the attendance — the facts only
# it can see — and exports them; the payroll provider owns the money.
WORKFORCE_ENABLED = _module_enabled("ORB_WORKFORCE")

#: Daily overtime threshold, in minutes. Minutes worked BEYOND this on a single
#: day accrue as overtime.
#:
#: 480 (8 h) is a DEFAULT A SITE MUST REVIEW, not a legal finding. Indian
#: working-hours law for a hospital is state law — most hospitals sit under a
#: State Shops & Establishments Act, a few under the Factories Act (9 h/day,
#: 48 h/week) — and the daily threshold, the weekly cap and the overtime
#: multiplier all vary. Orb only COUNTS minutes past a threshold the site sets;
#: it never prices them, and it never enforces a cap.
WORKFORCE_OT_DAILY_MINUTES = max(1, int(os.getenv("ORB_WORKFORCE_OT_DAILY_MINUTES", "480")))

#: Grace period before an arrival counts as late, in minutes. Purely a reporting
#: courtesy — nothing in Orb acts on a late mark.
WORKFORCE_LATE_GRACE_MINUTES = max(0, int(os.getenv("ORB_WORKFORCE_LATE_GRACE_MINUTES", "10")))

#: The longest single attendance span Orb will accept, in minutes. A 16-hour
#: default covers a double shift and a badly-timed handover; anything longer is
#: almost always a punch-out that was never recorded, and silently accepting it
#: would put a fictional 30 hours of overtime into a payroll file.
WORKFORCE_MAX_SPAN_MINUTES = max(60, int(os.getenv("ORB_WORKFORCE_MAX_SPAN_MINUTES", "960")))

# ── Diet & kitchen ───────────────────────────────────────────────────────────
#
# Diet orders per admitted patient, ward meal plans, kitchen counts and the
# kitchen indent, plus NPO (nil by mouth) handling.
# Set ORB_DIETARY=0 to hide this module for a deployment.
#
# THIS ONE IS CLINICAL-ADJACENT, AND THE SWITCH MEANS SOMETHING DIFFERENT HERE.
# A diabetic's tray and a renal patient's salt are part of their care. Turning
# this module off does not make Orb hold diet orders it will not show — it means
# Orb holds NO diet orders at all and the hospital's own kitchen system owns the
# whole question, which is the honest posture for a site that already runs one.
# What the switch can never do is hide an NPO order from a ward while the order
# still exists: with the module on, NPO is rendered on the ward view; with it
# off, there is nothing to render because nothing was ordered here.
#
# It gates no clinical write path. Medication ordering, administration, vitals,
# alerts, shadow capture, the audit chain, auth and the offline queue are
# untouched by this flag in either position.
DIETARY_ENABLED = _module_enabled("ORB_DIETARY")

# ── NABH accreditation evidence pack ─────────────────────────────────────────
#
# The incident / adverse-event register, the NABH-style quality-indicator
# dashboard computed from data Orb already holds, and the date-ranged evidence
# export a hospital hands an assessor.
#
# DEFAULT ON, like every other module here. A site that already runs a quality
# department on paper or in a separate system sets ORB_NABH=0 and the tab, the
# routes and the register disappear; nothing else changes.
#
# IT GATES NO CLINICAL WRITE PATH, and this one is worth spelling out because
# the module's subject matter is clinical. Reporting an incident does not
# create, block, delay or annotate any order, administration, vital, alert,
# escalation or shadow-mode record — the register is a parallel administrative
# ledger that READS the clinical tables and writes only to its own. With the
# flag off, a nurse's day is byte-identical.
NABH_ENABLED = _module_enabled("ORB_NABH")

# ── Opt-in switches: the whole-hospital-system build (3 October 2026) ────────
#
# THE OPPOSITE CONVENTION TO EVERYTHING ABOVE, AND ON PURPOSE. The modules above
# default ON (ruling 2026-08-11) and none of them gates a clinical path. The
# switches below default OFF, and they are allowed to gate clinical paths: bed
# moves, outpatient consultations, laboratory results, controlled-drug stock.
# Samuel ruled on 3 October 2026 that these three modules (ADT, pharmacy store,
# laboratory) depart from "every module on", and that every new safety ruling
# built behind a switch stays off at every site until he confirms it.
#
# ONE helper reads every one of them, so there is one answer to "what turns
# this on?": only 1, true, yes or on (any case, surrounding spaces ignored).
# Unset, blank, a typo and every other value mean OFF. An operator who writes
# ORB_LAB= must land on the documented default, which here is off.
#
# Two kinds:
#   * OPT_IN_MODULES: a whole module with its own tab. Published inside
#     /api/health "modules" next to the default-on modules, so the shell has one
#     place to look, and in "module_states" ('on', 'draining' or 'off').
#   * FEATURE_FLAGS: a behaviour inside a module or across modules. Published
#     as /api/health "features". A feature whose prerequisite is off is off,
#     whatever its own variable says, and the boot log names what is missing.
#
# Every new route behind these answers 404 while its switch is off (see
# routes/opt_in_gates.py), so the permissions granted for them in
# permissions.py are inert until a site turns the switch on.

_OPT_IN_ON_VALUES = ("1", "true", "yes", "on")

import logging as _logging  # noqa: E402 (kept inside this block on purpose)

_config_log = _logging.getLogger("orb.config")


def _opt_in(env_name: str) -> bool:
    """Read one opt-in switch. ON only for 1, true, yes or on; anything else,
    including unset and blank, is OFF."""
    return (os.getenv(env_name) or "").strip().lower() in _OPT_IN_ON_VALUES


# The six designs proposed four names for this one helper. They are the same
# function, kept so no later increment has to rename a call.
_opt_in_enabled = _opt_in
_module_opt_in = _opt_in
_feature_enabled = _opt_in

#: Opt-in module id -> env var. The ids are shell tab ids in src/shellConfig.tsx.
OPT_IN_MODULES = {
    "adt": "ORB_ADT",
    "pharmacy-store": "ORB_PHARMACY_STORE",
    "lab": "ORB_LAB",
}

# Beds and admissions: bed master, bed board, census, master-aware admit,
# transfer and end of stay.
# RELEASE GATE, stated where the switch lives: do not turn ORB_ADT on at a site
# where Sage runs until adt-6c (the Relay transfer lane) has landed, and do not
# rely on census or NABH figures before adt-8a, adt-8c and adt-6c.
ADT_ENABLED = _opt_in("ORB_ADT")
# Pharmacy store: drug purchasing, goods receipt, batches and expiry, transfers,
# stock take, NDPS register. Helix stays the only owner of drug stock.
PHARMACY_STORE_ENABLED = _opt_in("ORB_PHARMACY_STORE")
# Laboratory information system. With the switch off but laboratory work still
# open, the module reports 'draining' (see register_module_drain_check below)
# so open specimens and critical results are never hidden.
LAB_ENABLED = _opt_in("ORB_LAB")

# Module id -> the constant that holds it. Read at call time, so a test that
# monkeypatches config.LAB_ENABLED changes what every reader sees.
_OPT_IN_MODULE_CONSTANTS = {
    "adt": "ADT_ENABLED",
    "pharmacy-store": "PHARMACY_STORE_ENABLED",
    "lab": "LAB_ENABLED",
}

MODULE_STATES = ("on", "draining", "off")

# Module id -> a cheap, cached, DB-free check that answers "is there open work
# that must stay reachable although the switch is off?". Registered by the
# module's own service at startup (the laboratory registers one in lab_module),
# so /api/health learns about draining without main.py changing again.
_MODULE_DRAIN_CHECKS: dict = {}


def register_module_drain_check(module_id: str, check) -> None:
    """Let an opt-in module report 'draining' while its switch is off.

    `check()` must not open a database connection: /api/health calls it on
    the event loop. Register None to remove it."""
    if module_id not in OPT_IN_MODULES:
        raise ValueError(f"{module_id!r} is not an opt-in module")
    if check is None:
        _MODULE_DRAIN_CHECKS.pop(module_id, None)
    else:
        _MODULE_DRAIN_CHECKS[module_id] = check


def module_state(module_id: str) -> str:
    """'on', 'draining' or 'off' for an opt-in module.

    The switch alone decides 'on'. With the switch off, a registered drain check
    that says open work exists makes it 'draining'. A drain check that fails is
    read as 'draining', because the hazard a drain state exists for is hiding
    open clinical work (LIS-R21), and an unknown answer must not hide it."""
    const = _OPT_IN_MODULE_CONSTANTS[module_id]
    if globals().get(const, False):
        return "on"
    check = _MODULE_DRAIN_CHECKS.get(module_id)
    if check is None:
        return "off"
    try:
        return "draining" if check() else "off"
    except Exception:
        _config_log.exception("Drain check for module %s failed; reporting draining", module_id)
        return "draining"


def module_states() -> dict[str, str]:
    """Every opt-in module's state, as /api/health publishes it."""
    return {m: module_state(m) for m in OPT_IN_MODULES}


#: Module id → env var. The ids are the contract shared with the frontend: they
#: are also the shell tab ids in src/shellConfig.tsx, so there is one vocabulary
#: for "is the billing module on?" across the whole system. The opt-in modules
#: are listed here too: one switch board, two default conventions.
HOSPITAL_MODULE_ENV = {
    "billing": "ORB_BILLING",
    "payments": "ORB_PAYMENTS",
    "payer": "ORB_PAYER",
    "procurement": "ORB_PROCUREMENT",
    "frontdesk": "ORB_FRONTDESK",
    "biomedical": "ORB_BIOMEDICAL",
    "housekeeping": "ORB_HOUSEKEEPING",
    "workforce": "ORB_WORKFORCE",
    "dietary": "ORB_DIETARY",
    "nabh": "ORB_NABH",
    "abdm": "ORB_ABDM",
    **OPT_IN_MODULES,
}


def enabled_modules() -> dict[str, bool]:
    """The module switch board, as the frontend receives it from /api/health.

    Re-read from the module-level constants (not from the environment) so the
    answer a client gets is exactly the answer the server's own routes use. A
    test that wants a module off must patch the constant, the same as any other
    config value.

    An opt-in module is true while it is 'on' or 'draining' (module_state), so a
    laboratory switched off with open work keeps its tab for the lab staff who
    must finish that work.
    """
    return {
        "billing": BILLING_ENABLED,
        "payments": PAYMENTS_ENABLED,
        "payer": PAYER_ENABLED,
        "procurement": PROCUREMENT_ENABLED,
        "frontdesk": FRONTDESK_ENABLED,
        "biomedical": BIOMEDICAL_ENABLED,
        "housekeeping": HOUSEKEEPING_ENABLED,
        "workforce": WORKFORCE_ENABLED,
        "dietary": DIETARY_ENABLED,
        "nabh": NABH_ENABLED,
        "abdm": ABDM_ENABLED,
        **{m: module_state(m) != "off" for m in OPT_IN_MODULES},
    }


# ── Feature flags (default OFF, may gate clinical paths) ─────────────────────
#
# Feature id -> env var. The ids are the contract with the frontend
# (src/shellConfig.tsx FeatureFlagId) and with every later increment of the
# hospital-system build. Same reader as the opt-in modules: only 1/true/yes/on.
FEATURE_FLAGS = {
    # Patient registration desk: UHID, demographics, duplicate check.
    "registration": "ORB_REGISTRATION",
    # Outpatient consultations. Needs registration and the front desk module.
    "opd": "ORB_OPD",
    # Refuse a ward or bed that is not in the bed master (F104). Needs ORB_ADT.
    "adt_strict_locations": "ORB_ADT_STRICT_LOCATIONS",
    # Ward imprest and crash-cart drug stores (GRN-RS5). Needs ORB_PHARMACY_STORE.
    "pharmacy_ward_stock": "ORB_PHARMACY_WARD_STOCK",
    # The NDPS register hook. Needs ORB_PHARMACY_STORE AND a failure mode in
    # ORB_PHARMACY_NDPS_ON_FAILURE ('block' or 'record', ruling GRN-RS7). Without
    # the mode it refuses to turn on and logs an error.
    "ndps_register": "ORB_PHARMACY_NDPS_REGISTER",
    # The analyser MLLP listener. Needs ORB_LAB (on, not draining).
    "lab_analyser_listener": "ORB_LAB_ANALYSER_LISTENER",
    # Unmatched ingestion results go to a queue instead of a patient-less row.
    "result_quarantine": "ORB_RESULT_QUARANTINE",
    # An external OBX 'C' supersedes the earlier final result.
    "result_corrections": "ORB_RESULT_CORRECTIONS",
    # External ORU results link to and close CPOE laboratory orders.
    "oru_order_link": "ORB_ORU_ORDER_LINK",
    # An external lab's own HH/LL/AA flag raises a critical callback (LIS-R22).
    "lab_critical_interfaced": "ORB_LAB_CRITICAL_INTERFACED",
    # Laboratory reports in the patient portal (LIS-R11).
    "lab_portal_reports": "ORB_LAB_PORTAL_REPORTS",
}

# The failure mode for an NDPS register write that fails during a dispense
# (ruling GRN-RS7, held for Samuel). None until a site sets one of the two
# built modes; with None the register cannot be switched on.
_NDPS_FAILURE_MODES = ("block", "record")
_ndps_mode_raw = (os.getenv("ORB_PHARMACY_NDPS_ON_FAILURE") or "").strip().lower()
PHARMACY_NDPS_ON_FAILURE = _ndps_mode_raw if _ndps_mode_raw in _NDPS_FAILURE_MODES else None

# What each feature needs besides its own switch. ('feature', id), ('module',
# id) for an opt-in or default-on module that must be on (an opt-in module must
# be 'on', not 'draining'), or ('setting', name) for a value that must be set.
FEATURE_PREREQUISITES = {
    "opd": (("feature", "registration"), ("module", "frontdesk")),
    "adt_strict_locations": (("module", "adt"),),
    "pharmacy_ward_stock": (("module", "pharmacy-store"),),
    "ndps_register": (("module", "pharmacy-store"),
                      ("setting", "ORB_PHARMACY_NDPS_ON_FAILURE")),
    "lab_analyser_listener": (("module", "lab"),),
}

# Feature id -> the constant that holds its switch. Each constant is the
# switch AND its prerequisites as read at import; feature_enabled() re-checks
# the prerequisites at call time, so patching one constant in a test cannot
# switch on a feature whose prerequisite is off.
_FEATURE_CONSTANTS = {
    "registration": "REGISTRATION_ENABLED",
    "opd": "OPD_ENABLED",
    "adt_strict_locations": "ADT_STRICT_LOCATIONS_ENABLED",
    "pharmacy_ward_stock": "PHARMACY_WARD_STOCK_ENABLED",
    "ndps_register": "PHARMACY_NDPS_REGISTER_ENABLED",
    "lab_analyser_listener": "LAB_ANALYSER_LISTENER_ENABLED",
    "result_quarantine": "RESULT_QUARANTINE_ENABLED",
    "result_corrections": "RESULT_CORRECTIONS_ENABLED",
    "oru_order_link": "ORU_ORDER_LINK_ENABLED",
    "lab_critical_interfaced": "LAB_CRITICAL_INTERFACED_ENABLED",
    "lab_portal_reports": "LAB_PORTAL_REPORTS_ENABLED",
}

_PREREQUISITE_WORDS = {
    ("feature", "registration"): "ORB_REGISTRATION is off",
    ("module", "frontdesk"): "the front desk module is off (ORB_FRONTDESK=0)",
    ("module", "adt"): "ORB_ADT is off",
    ("module", "pharmacy-store"): "ORB_PHARMACY_STORE is off",
    ("module", "lab"): "ORB_LAB is off",
    ("setting", "ORB_PHARMACY_NDPS_ON_FAILURE"): (
        "ORB_PHARMACY_NDPS_ON_FAILURE is not 'block' or 'record' (ruling GRN-RS7)"
    ),
}


def _prerequisite_problems(flag_id: str) -> list[str]:
    """The unmet prerequisites of one feature, as sentences for a log line."""
    problems = []
    for kind, ref in FEATURE_PREREQUISITES.get(flag_id, ()):
        if kind == "feature":
            ok = bool(globals().get(_FEATURE_CONSTANTS[ref], False)) and not _prerequisite_problems(ref)
        elif kind == "module":
            ok = (module_state(ref) == "on") if ref in OPT_IN_MODULES else bool(enabled_modules().get(ref))
        elif kind == "setting" and ref == "ORB_PHARMACY_NDPS_ON_FAILURE":
            ok = globals().get("PHARMACY_NDPS_ON_FAILURE") in _NDPS_FAILURE_MODES
        else:  # an unknown prerequisite kind fails closed
            ok = False
        if not ok:
            problems.append(_PREREQUISITE_WORDS.get((kind, ref), f"{kind} {ref} is off"))
    return problems


def feature_enabled(flag_id: str) -> bool:
    """Is this feature on, its prerequisites included? Unknown ids are off."""
    const = _FEATURE_CONSTANTS.get(flag_id)
    if const is None or not globals().get(const, False):
        return False
    return not _prerequisite_problems(flag_id)


def feature_flags() -> dict[str, bool]:
    """Every feature flag, as /api/health publishes it under "features"."""
    return {f: feature_enabled(f) for f in FEATURE_FLAGS}


def switch_enabled(switch_id: str) -> bool:
    """A feature id or a module id (default-on or opt-in), one question. Used by
    roles.ROLE_FEATURE. An id that is neither is off: a typo must not unlock a
    role."""
    if switch_id in FEATURE_FLAGS:
        return feature_enabled(switch_id)
    return bool(enabled_modules().get(switch_id, False))


_FEATURE_REQUESTED = {f: _opt_in(env) for f, env in FEATURE_FLAGS.items()}

REGISTRATION_ENABLED = _FEATURE_REQUESTED["registration"]
OPD_ENABLED = (_FEATURE_REQUESTED["opd"] and REGISTRATION_ENABLED and FRONTDESK_ENABLED)
ADT_STRICT_LOCATIONS_ENABLED = _FEATURE_REQUESTED["adt_strict_locations"] and ADT_ENABLED
PHARMACY_WARD_STOCK_ENABLED = _FEATURE_REQUESTED["pharmacy_ward_stock"] and PHARMACY_STORE_ENABLED
PHARMACY_NDPS_REGISTER_ENABLED = (_FEATURE_REQUESTED["ndps_register"] and PHARMACY_STORE_ENABLED
                                  and PHARMACY_NDPS_ON_FAILURE is not None)
LAB_ANALYSER_LISTENER_ENABLED = _FEATURE_REQUESTED["lab_analyser_listener"] and LAB_ENABLED
RESULT_QUARANTINE_ENABLED = _FEATURE_REQUESTED["result_quarantine"]
RESULT_CORRECTIONS_ENABLED = _FEATURE_REQUESTED["result_corrections"]
ORU_ORDER_LINK_ENABLED = _FEATURE_REQUESTED["oru_order_link"]
LAB_CRITICAL_INTERFACED_ENABLED = _FEATURE_REQUESTED["lab_critical_interfaced"]
LAB_PORTAL_REPORTS_ENABLED = _FEATURE_REQUESTED["lab_portal_reports"]


def feature_flag_problems() -> dict[str, list[str]]:
    """Features a site asked for (env set) that are off because a prerequisite
    is missing, each with the reasons. Empty when nothing was asked for."""
    return {
        f: _prerequisite_problems(f)
        for f, requested in _FEATURE_REQUESTED.items()
        if requested and _prerequisite_problems(f)
    }


# Say so at boot, once, when a site switched something on that cannot be on.
for _flag, _why in feature_flag_problems().items():
    if _flag == "ndps_register" and PHARMACY_NDPS_ON_FAILURE is None:
        _config_log.error(
            "ORB_PHARMACY_NDPS_REGISTER is set but the NDPS register stays OFF "
            "(ruling GRN-RS7 is held for Samuel): %s", "; ".join(_why))
    else:
        _config_log.warning("Feature %s (%s) is set but stays OFF: %s",
                            _flag, FEATURE_FLAGS[_flag], "; ".join(_why))


# ── Settings for the hospital-system build ───────────────────────────────────
# Read once at import, each with its default. An unparseable value falls back to
# the default (a typo must not become a zero), and choice settings fall back to
# their default too. The defaults on safety settings are the STRICTEST option
# the architect named, pending Samuel's confirmation of the ruling cited.

def _int_setting(env_name: str, default: int, minimum: int = 0) -> int:
    raw = (os.getenv(env_name) or "").strip()
    if not raw:
        return default
    try:
        return max(minimum, int(raw))
    except ValueError:
        _config_log.warning("%s=%r is not a whole number; using %d", env_name, raw, default)
        return default


def _choice_setting(env_name: str, choices: tuple, default: str) -> str:
    raw = (os.getenv(env_name) or "").strip().lower()
    if not raw:
        return default
    if raw in choices:
        return raw
    _config_log.warning("%s=%r is not one of %s; using %r", env_name, raw, choices, default)
    return default


# Registration and outpatients.
#: Where UHIDs come from: 'hospital' (type the existing HIS number, OPD-N1
#: default) or 'orb' (Orb's own series with a Verhoeff check digit, refused
#: until the series is configured).
UHID_SOURCE = _choice_setting("ORB_UHID_SOURCE", ("hospital", "orb"), "hospital")
#: Optional site regex a hospital-mode UHID must match. Kept as typed; the
#: registration service compiles it and refuses to register when it is invalid.
UHID_PATTERN = (os.getenv("ORB_UHID_PATTERN") or "").strip() or None
#: How many days an OPD visit opens the record view to its clinic (OPD-N2).
OPD_SCOPE_DAYS = _int_setting("ORB_OPD_SCOPE_DAYS", 7, minimum=0)
#: When the consultation fee is charged: at 'completion' (today) or 'registration'.
OPD_CHARGE_AT = _choice_setting("ORB_OPD_CHARGE_AT", ("completion", "registration"), "completion")
#: What a probable duplicate does at registration (ruling OPD-S2, held for
#: Samuel). 'block' is the strictest option named and is the default until he
#: confirms; 'warn' requires a reason, is audited and opens a worklist item.
#: An exact UHID or active ABHA match is refused in both modes.
DUPLICATE_MODE = _choice_setting("ORB_DUPLICATE_MODE", ("warn", "block"), "block")
#: How often an open front desk queue re-reads until live nudges land (opd-9b).
OPD_QUEUE_POLL_SECONDS = _int_setting("ORB_OPD_QUEUE_POLL_SECONDS", 30, minimum=5)

# Beds and admissions.
#: How far back an admission, transfer or departure time may be entered (ADT-P3).
ADT_BACKTIME_HOURS = _int_setting("ORB_ADT_BACKTIME_HOURS", 72, minimum=0)
#: Bed reservation expiry for ER and transfer reservations (ADT-P9).
ADT_RESERVATION_MINUTES = _int_setting("ORB_ADT_RESERVATION_MINUTES", 120, minimum=1)
#: Bed reservation expiry for elective and post-operative reservations (ADT-P9).
ADT_RESERVATION_ELECTIVE_MINUTES = _int_setting("ORB_ADT_RESERVATION_ELECTIVE_MINUTES", 1440, minimum=1)
#: After this long, the board prompts the bed manager to close an absconded
#: stay. Advisory only; nothing closes on a timer (ADT-S3).
ADT_ABSCONDED_CLOSE_HOURS = _int_setting("ORB_ADT_ABSCONDED_CLOSE_HOURS", 24, minimum=1)
#: A leave longer than this prompts the doctor to discharge (ADT-P6).
ADT_LEAVE_PROMPT_HOURS = _int_setting("ORB_ADT_LEAVE_PROMPT_HOURS", 48, minimum=1)
#: Whether room rent is charged for leave days (ADT-P6: charged by default).
#: Default ON like the modules above: only 0/false/off/no turns it off.
ADT_LEAVE_RENT_CHARGED = _module_enabled("ORB_ADT_LEAVE_RENT_CHARGED")

# Pharmacy store.
#: Near-expiry window shown on the stock screens (GRN-R16).
PHARMACY_NEAR_EXPIRY_DAYS = _int_setting("ORB_PHARMACY_NEAR_EXPIRY_DAYS", 90, minimum=1)
#: Shortest shelf life accepted at goods receipt without an approve-tier
#: override (GRN-R16).
PHARMACY_MIN_SHELF_LIFE_DAYS = _int_setting("ORB_PHARMACY_MIN_SHELF_LIFE_DAYS", 90, minimum=0)
#: Warn this many days before a vendor's drug licence lapses (GRN-R10).
PHARMACY_LICENCE_WARN_DAYS = _int_setting("ORB_PHARMACY_LICENCE_WARN_DAYS", 30, minimum=0)
#: Largest dispense quantity a pharmacist may enter (GRN-R13). 1 by default,
#: which is exactly what a dispense moves today.
PHARMACY_MAX_DISPENSE_UNITS = _int_setting("ORB_PHARMACY_MAX_DISPENSE_UNITS", 1, minimum=1)

# Laboratory.
#: The analyser MLLP listener port (2575 is the bedside monitor feed).
LAB_MLLP_PORT = _int_setting("ORB_LAB_MLLP_PORT", 2576, minimum=1)
#: Critical callback sweep period, in seconds.
LAB_CRITICAL_SWEEP_S = _int_setting("ORB_LAB_CRITICAL_SWEEP_S", 60, minimum=5)
#: A result must be entered and authorised by two different people (ruling
#: LIS-R5, held for Samuel). ON by default, the strictest option; only
#: 0/false/off/no lets one signatory do every step.
LAB_REQUIRE_DISTINCT_AUTHORISER = _module_enabled("ORB_LAB_REQUIRE_DISTINCT_AUTHORISER")
#: How far back a specimen collection time may be set, in minutes (ruling
#: LIS-R6, held for Samuel). 0, the default, refuses any back-dating.
LAB_COLLECTION_BACKDATE_MINUTES = _int_setting("ORB_LAB_COLLECTION_BACKDATE_MINUTES", 0, minimum=0)


# ── Discharge financial clearance: ADVISORY BY DEFAULT ───────────────────────
#
# DEFAULT OFF, AND IT MUST STAY OFF FOR THE PILOT.
#
# When OFF (the default), a discharge may compute and DISPLAY the patient's
# financial position — outstanding balance, unbilled charges, open claims — and
# it may warn about it. It may not stop the discharge. The discharge write path
# does not consult this module for permission; it records what the money side
# saw (`discharge_financial_checks`) and proceeds.
#
# WHY. A patient's discharge must never wait on an unpaid bill. Two reasons, and
# the first one alone is sufficient:
#
#   1. Clinical. A discharge decision is a clinical decision. A patient who is
#      medically ready to leave and is held on a ward by a software rule is
#      occupying a bed somebody sicker needs, and is accumulating the risks that
#      come with being in a hospital. In India, holding a patient over a bill is
#      also unlawful.
#   2. Pilot scope. The shadow pilot does NOT bill through Orb — the incumbent
#      system keeps running the money — so Orb's idea of an outstanding balance
#      is definitionally incomplete during the pilot. Enforcing on an incomplete
#      figure would block discharges over bills that were already paid somewhere
#      Orb cannot see.
#
# The flag exists at all so that a POST-pilot site which has moved its billing
# into Orb can turn on a soft-stop with an override, rather than someone adding
# the enforcement later as an un-flagged `if`. Turning it on is a deliberate,
# site-level, documented act — never a default, never a dev convenience.
_ENFORCE_RAW = (os.getenv("ORB_DISCHARGE_FINANCIAL_ENFORCE") or "").strip().lower()
DISCHARGE_FINANCIAL_ENFORCE = _ENFORCE_RAW in ("1", "true", "yes", "on")



# ── Retention: the minority protection (services/consent_service) ─────────────
# A minor's medical record cannot be destroyed on the ordinary 3-year clinical
# clock. Under the Limitation Act 1963 s.6 the limitation period does not begin
# until the person under a legal disability attains majority, so the claim
# window for care given to a child closes at 18 + 3 = 21. A retention review
# that listed a paediatric record as destroyable three years after the last
# visit would be inviting a hospital to destroy the exact evidence it will be
# asked for.
#
# This is a FLOOR applied on top of retention_policies, not a row in it, because
# it is a property of the PATIENT and the policy table is keyed by category. It
# is configurable rather than hardcoded for the same reason retention_policies
# is mutable: the law changes, and a site whose counsel reads s.6 differently
# must be able to say so without a code deploy.
MINOR_RETENTION_AGE = int(os.getenv("ORB_MINOR_RETENTION_AGE", "21"))
