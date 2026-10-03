"""Orb Hospital OS — outbound appliance heartbeat (no PHI, pages Samuel).

WHAT THIS IS
────────────
The appliance sits in a cupboard in a hospital in India. Nobody there watches a
dashboard, and nothing about Orb's design lets us reach INTO the box: it is
local-first on purpose, behind the hospital's NAT, with no inbound path. So the
only way a silent failure becomes a phone call is if the box calls out.

This is that call. Every N minutes it POSTs a small JSON document describing the
appliance's operational state — health, disk headroom, backup age, model warmth,
loop-watchdog history, access posture — to an endpoint we control.

It is not monitoring in the general sense. It is a dead-man's switch for four
specific failures that have each already happened to this project or its class:

  1. THE BACKUP GOES STALE SILENTLY. scripts/backup.sh once snapshotted with the
     stock `sqlite3` CLI, which cannot open a SQLCipher database — it printed an
     error, exited 0, wrote a zero-byte file, passed integrity_check (an empty
     file IS a valid empty database), and then ROTATED, pruning real backups to
     make room for empty ones. Fourteen scheduled runs would have destroyed every
     good backup while reporting success every night. `backup.newest_age_s` is
     the number that catches the whole family: nobody has to read a log.
  2. THE EVENT LOOP FREEZES. On 2026-08-01 the process sat at 0.2% CPU and
     needed kill -9; /api/health could not report it, because /api/health is
     served BY the loop. See services/loop_watchdog.py.
  3. THE DISK FILLS. A 270 MB encrypted DB, 14 rotated backups, a nightly FHIR
     self-export of ~283 × 1 MB bundles and per-freeze thread dumps all live on
     one volume. Full disk = failed writes = failed audit rows = fail-closed
     refusals on clinical actions.
  4. THE APPLIANCE HAS NO ADMIN, OR A DEMO PASSWORD. See
     services/bootstrap_service.py.

WHY IT DOES NOT LIVE ON THE EVENT LOOP  ← the load-bearing design decision
─────────────────────────────────────────────────────────────────────────
A heartbeat that runs as an asyncio task is useless for failure (2). When the
loop is starved the task is never scheduled, the beat simply stops, and the
absence of a beat is indistinguishable from "the ward's internet is down" —
which in an Indian district hospital is a nightly event. An alarm that cries wolf
every evening gets muted, and then the real freeze is silent too.

So:
  * The sender runs on a DAEMON THREAD with its own threading.Event timer. It is
    scheduled by the OS, not by asyncio, and keeps beating through a wedged loop
    exactly as loop_watchdog's monitor thread does.
  * It reads loop health from the watchdog's status.json ON DISK — written by
    that daemon thread, never by the loop — instead of calling /api/health.
    During a freeze the file's own staleness (`loop.status_age_s`) is the signal.
  * It never touches the database, never acquires an application lock, and never
    calls into the loop. Nothing in a clinical path can wait on it.
  * For total independence there is a standalone mode:
        server/.venv/bin/python -m services.heartbeat_service --loop
    as its own launchd job. A daemon thread still needs the GIL; a separate
    process does not. Both modes build the identical payload from the identical
    on-disk sources. In-process is the default because it needs no ops work;
    out-of-process is what you run when you want the beat to survive the
    interpreter itself. See docs/WARM_STANDBY.md.

NO PHI — how that is guaranteed rather than intended
────────────────────────────────────────────────────
This document leaves the hospital. India's DPDP Act and every contract we will
sign make that a one-strike matter, so "we were careful" is not a control. Four
independent ones:

  1. NO DATABASE READ. build_payload() issues zero SQL. It reads the filesystem,
     Ollama's /api/ps, config values, and two status.json files. There is no code
     path from a patients/vitals/notes row into this payload, so there is nothing
     to redact.
  2. KEY ALLOWLIST. _ALLOWED_KEYS is exhaustive. A key not on it is dropped by
     the guard before send, in production, not just in tests. Adding a field
     without adding its name does not leak — it disappears.
  3. VALUE SHAPE. Every leaf must be a bool, a number, None, or a string of at
     most 64 chars matching ^[A-Za-z0-9:._+/-]+$ — no spaces, no punctuation a
     human name or an address needs. Anything else is dropped.
  4. NO ERROR MESSAGES. This is the subtle one and the one most systems get
     wrong. An exception string routinely embeds its inputs — "no such patient:
     P001", "UNIQUE constraint failed: patients.mrn = ...". Forwarding
     `last_error` verbatim would be a PHI leak dressed as diagnostics. So every
     failure is reported as a fixed ENUM CODE from _ERROR_CODES and nothing else,
     including the loop watchdog's own last_error, which is deliberately reduced
     to the boolean `loop.watchdog_error`.

Counts are the remaining judgement call, and the rule applied is: operational
counts only, never clinical ones. Number of backups, number of admin accounts,
number of warm models — yes. Number of patients, admissions, alerts or notes —
NO, even though /metrics exposes some of those on the LAN, because those numbers
leave the building here and a small ward's census is a re-identification vector
when combined with anything else. `orb_patients_total` deliberately does not
appear below.

CONFIGURATION (off by default — no URL, no beat)
────────────────────────────────────────────────
  ORB_HEARTBEAT_URL              (required to enable) https endpoint to POST to
  ORB_HEARTBEAT_SITE             short slug identifying the appliance ([a-z0-9-])
  ORB_HEARTBEAT_INTERVAL_S       300
  ORB_HEARTBEAT_TIMEOUT_S        10
  ORB_HEARTBEAT_TOKEN            sent as `Authorization: Bearer <token>`
  ORB_HEARTBEAT_SECRET           HMAC-SHA256 over the body -> X-Orb-Signature
  ORB_HEARTBEAT_BACKUP_MAX_AGE_H 36    (older than this ⇒ alert backup_stale)
  ORB_HEARTBEAT_DISK_MIN_PCT     10    (free % below this ⇒ alert disk_low)
  ORB_HEARTBEAT_ALLOW_INSECURE   0     (1 permits a plain-http endpoint)
  ORB_HEARTBEAT_JITTER_PCT       10    (spread beats so N appliances don't sync)
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import shutil
import socket
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("orb.heartbeat")

SCHEMA = "orb.heartbeat/1"

# ── The PHI guard ────────────────────────────────────────────────────────────
# Exhaustive key allowlist. Nested objects are keyed by their own names; the
# guard walks the whole tree. Anything absent here never leaves the appliance.
_ALLOWED_KEYS: frozenset[str] = frozenset({
    # envelope
    "schema", "sent_utc", "site", "appliance_id", "orb_version", "profile",
    "pid", "uptime_s", "python", "hostname_hash", "severity", "alerts", "errors",
    # db posture (sizes and flags only — never contents, never row counts)
    "db", "encrypted_at_rest", "uploads_encrypted", "size_bytes", "wal_bytes",
    # disk
    "disk", "db_volume", "backup_volume", "total_bytes", "free_bytes", "free_pct",
    # backup age — the headline number
    "backup", "dir_present", "count", "newest_age_s", "newest_size_bytes",
    "stale", "max_age_h", "media_archive_present", "offsite_present",
    "offsite_newest_age_s",
    # model warmth
    "models", "ollama_reachable", "configured", "warm", "cold",
    # event loop / watchdog (from status.json on disk)
    "loop", "watchdog_enabled", "status_age_s", "healthy", "stall_active",
    "stalls_total", "max_stall_s", "beat_age_s", "deadman_fired",
    "deadman_armed", "watchdog_error",
    # access posture (from bootstrap status.json)
    "access", "admins_active", "admins_with_mfa", "default_credential_accounts",
    "bootstrap_state", "scan_complete",
    # standby receipt (written by scripts/standby-restore.sh)
    "standby", "last_restore_age_s", "last_restore_ok", "receipt_present",
})

# Strings may only look like this. No spaces: a human name, an address, a free
# text note or a SQL error message cannot survive it. Empty is allowed — `site`
# is legitimately blank on an appliance that has not been labelled, and dropping
# it there would trip the guard on every beat and train us to ignore it.
_SAFE_STR = re.compile(r"^[A-Za-z0-9:._+/-]{0,64}$")

# THE KEYS THAT MAY HOLD A STRING AT ALL. Everything else in the payload is a
# number, a bool or None, and a string appearing under one of those keys is
# dropped outright.
#
# This exists because _SAFE_STR alone is not enough, and it is worth being
# precise about why: an MRN like "MRN-99-ZZTOP-4471" is letters, digits and
# hyphens — it passes the character class cleanly. The character class stops
# names, addresses, diagnoses and exception messages; only a per-key type
# contract stops an identifier-shaped string appearing where a byte count
# belongs. Both are backstops behind the real control (no database read at all),
# and defence in depth is the point.
_STRING_KEYS: frozenset[str] = frozenset({
    "schema", "sent_utc", "site", "appliance_id", "orb_version", "profile",
    "python", "severity", "bootstrap_state", "alerts", "errors", "cold",
})

# List members that must additionally come from a closed vocabulary.
_ENUM_LISTS: dict[str, frozenset[str]] = {}  # populated after the code sets below

# Every failure this module can report, as a closed set. Nothing else is ever
# emitted — see the module docstring, point 4.
_ERROR_CODES = frozenset({
    "db_path_unreadable", "backup_dir_unreadable", "disk_unreadable",
    "ollama_unreachable", "watchdog_status_unreadable", "bootstrap_status_unreadable",
    "standby_receipt_unreadable", "payload_guard_tripped",
})

# Alert codes — the paging vocabulary.
_ALERT_CODES = frozenset({
    "no_admin", "admin_without_mfa", "default_credentials",
    "backup_missing", "backup_stale", "disk_low", "loop_stalled",
    "loop_status_stale", "deadman_fired", "models_cold", "standby_stale",
    "db_unencrypted",
})

# `alerts` and `errors` are the two lists whose members must come from a closed
# vocabulary. `cold` holds model names, which are configuration, not data.
_ENUM_LISTS.update({"alerts": _ALERT_CODES, "errors": _ERROR_CODES})


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name, "") or default).strip()


def _env_float(name: str, default: float) -> float:
    try:
        return float(_env(name) or default)
    except ValueError:
        return default


def _env_flag(name: str, default: bool) -> bool:
    raw = _env(name).lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def sanitise_site(raw: str) -> str:
    """Reduce an operator-set site label to a slug that cannot carry text.

    This is the only string in the payload an operator supplies, and it comes
    from the environment, never from the database — so it cannot contain patient
    data unless somebody types patient data into ORB_HEARTBEAT_SITE. Constraining
    it to [a-z0-9-]{0,32} means even then it would arrive as a mangled token, not
    a name. Empty is legal (the appliance_id still identifies the box).
    """
    slug = re.sub(r"[^a-z0-9-]", "-", raw.strip().lower())
    slug = re.sub(r"-{2,}", "-", slug).strip("-")
    return slug[:32]


# ── Path resolution (works in-process AND standalone) ────────────────────────
def _server_dir() -> Path:
    return Path(__file__).resolve().parent.parent


def _db_path() -> Path:
    env = _env("ORB_DB_PATH")
    return Path(env) if env else _server_dir() / "orb.db"


def _backup_dir() -> Path:
    """<repo root>/backups — the canonical directory (see config.BACKUP_DIR).

    This resolver was already right when the other four disagreed (2026-08-15),
    which is precisely why the split was hard to see: the heartbeat reported on
    the real backups while the ops status page graded server/backups. Two
    monitors, one appliance, different answers. Kept anchored to _server_dir()
    rather than the database path, and deliberately not importing config, since
    this module is also run standalone.
    """
    env = _env("ORB_BACKUP_DIR")
    return Path(env) if env else _server_dir().parent / "backups"


def _watchdog_dir() -> Path:
    env = _env("ORB_WATCHDOG_DIR")
    return Path(env) if env else _server_dir() / "ops" / "watchdog"


def _bootstrap_dir() -> Path:
    env = _env("ORB_BOOTSTRAP_DIR")
    return Path(env) if env else _server_dir() / "ops" / "bootstrap"


def _standby_receipt_path() -> Path:
    env = _env("ORB_STANDBY_RECEIPT")
    return Path(env) if env else _server_dir() / "ops" / "standby" / "last-restore.json"


def appliance_id() -> str:
    """A stable, pseudonymous identifier for this box.

    sha256 over the hostname and the absolute database path, truncated. Stable
    across restarts, distinct per appliance, and reveals neither — we hold the
    mapping on our side. A raw hostname would be a hospital's own name in most
    real deployments, which is not PHI but is customer-identifying data we have
    no reason to put in flight.
    """
    material = f"{socket.gethostname()}|{_db_path()}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


# ── Collectors. Each returns plain data and NEVER raises. ────────────────────
def _collect_db(errors: set[str]) -> dict[str, Any]:
    path = _db_path()
    size = wal = None
    try:
        if path.is_file():
            size = path.stat().st_size
        wal_path = Path(f"{path}-wal")
        wal = wal_path.stat().st_size if wal_path.exists() else 0
    except OSError:
        errors.add("db_path_unreadable")
    return {
        "encrypted_at_rest": bool(_env("ORB_DB_ENCRYPTION_KEY")),
        "uploads_encrypted": bool(_env("ORB_UPLOAD_ENCRYPTION_KEY")),
        "size_bytes": size,
        "wal_bytes": wal,
    }


def _volume(path: Path, errors: set[str]) -> dict[str, Any]:
    """Free space on the volume holding `path`, walking up to the nearest parent
    that exists (a backup dir that has never been created still lives on a real
    volume, and its headroom is exactly what we want to know)."""
    probe = path
    for _ in range(6):
        if probe.exists():
            break
        probe = probe.parent
    try:
        usage = shutil.disk_usage(str(probe))
    except OSError:
        errors.add("disk_unreadable")
        return {"total_bytes": None, "free_bytes": None, "free_pct": None}
    pct = round(100.0 * usage.free / usage.total, 1) if usage.total else None
    return {"total_bytes": usage.total, "free_bytes": usage.free, "free_pct": pct}


# Same conventions as scripts/backup.sh and server/scripts/backup_db.py. Kept in
# agreement with them deliberately: a heartbeat that measures a different set of
# files than the backup job writes would report a healthy age forever.
_BACKUP_PREFIX = "orb-backup-"
_BACKUP_SUFFIX = ".db"


def _collect_backup(errors: set[str]) -> dict[str, Any]:
    """Backup freshness. THE field this heartbeat exists for.

    Age is measured from mtime, not from the filename timestamp: a file copied or
    restored keeps its name but gets a new mtime, and mtime is what actually
    tells you when bytes last landed. Size of the newest is reported too, because
    the historical failure wrote a technically-present zero-byte file.
    """
    out: dict[str, Any] = {
        "dir_present": False, "count": 0, "newest_age_s": None,
        "newest_size_bytes": None, "media_archive_present": False,
        "offsite_present": False, "offsite_newest_age_s": None,
    }
    directory = _backup_dir()
    try:
        if not directory.is_dir():
            return out
        out["dir_present"] = True
        files = sorted(
            directory.glob(f"{_BACKUP_PREFIX}*{_BACKUP_SUFFIX}"),
            key=lambda p: p.stat().st_mtime, reverse=True,
        )
        out["count"] = len(files)
        if files:
            newest = files[0]
            stat = newest.stat()
            out["newest_age_s"] = int(max(0.0, time.time() - stat.st_mtime))
            out["newest_size_bytes"] = stat.st_size
            # orb.db holds "see the chest X-ray"; uploads/ holds the X-ray. A
            # database-only backup restores a chart full of dangling references.
            media = directory / f"{newest.name[:-len(_BACKUP_SUFFIX)]}-uploads.tar"
            out["media_archive_present"] = media.exists()
    except OSError:
        errors.add("backup_dir_unreadable")

    offsite = _env("ORB_BACKUP_OFFSITE_DIR")
    if offsite:
        try:
            odir = Path(offsite)
            if odir.is_dir():
                out["offsite_present"] = True
                ofiles = sorted(odir.glob(f"{_BACKUP_PREFIX}*{_BACKUP_SUFFIX}"),
                                key=lambda p: p.stat().st_mtime, reverse=True)
                if ofiles:
                    out["offsite_newest_age_s"] = int(
                        max(0.0, time.time() - ofiles[0].stat().st_mtime))
        except OSError:
            errors.add("backup_dir_unreadable")
    return out


def _collect_models(errors: set[str], timeout: float = 3.0) -> dict[str, Any]:
    """Which configured tiers are resident in Ollama right now.

    Uses urllib against /api/ps with proxies explicitly disabled — see
    _post_json for why touching the system proxy configuration on macOS is a
    hazard in this process. Model names are configuration, not data.
    """
    import urllib.request

    base = _env("ORB_OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/")
    configured = [m for m in dict.fromkeys([
        _env("ORB_LLM_MODEL", "medgemma:27b"),
        _env("ORB_AMBIENT_LLM_MODEL") or _env("ORB_LLM_MODEL", "medgemma:27b"),
    ]) if m]
    out = {"ollama_reachable": False, "configured": len(configured), "warm": 0, "cold": []}
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f"{base}/api/ps", timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        resident = {m.get("name", "") for m in body.get("models", [])}
        out["ollama_reachable"] = True
        out["warm"] = sum(1 for m in configured if m in resident)
        out["cold"] = [m for m in configured if m not in resident]
    except Exception:
        errors.add("ollama_unreachable")
        out["cold"] = list(configured)
    return out


def _read_json(path: Path) -> Optional[dict]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _collect_loop(errors: set[str]) -> dict[str, Any]:
    """Event-loop health, read from the watchdog's status.json ON DISK.

    Never from /api/health. status.json is written by loop_watchdog's daemon
    thread, so it keeps moving while the loop is dead — and when even that stops,
    `status_age_s` growing past the watchdog's write cadence IS the alarm. That
    is the whole reason this is a file read and not an HTTP call.
    """
    path = _watchdog_dir() / "status.json"
    out: dict[str, Any] = {
        "watchdog_enabled": False, "status_age_s": None, "healthy": None,
        "stall_active": None, "stalls_total": None, "max_stall_s": None,
        "beat_age_s": None, "deadman_fired": None, "deadman_armed": None,
        "watchdog_error": False,
    }
    data = _read_json(path)
    if data is None:
        if path.exists():
            errors.add("watchdog_status_unreadable")
        return out
    try:
        out["status_age_s"] = int(max(0.0, time.time() - path.stat().st_mtime))
    except OSError:
        errors.add("watchdog_status_unreadable")
    out["watchdog_enabled"] = bool(data.get("enabled", True))
    out["healthy"] = bool(data.get("healthy")) if data.get("healthy") is not None else None
    out["stall_active"] = bool(data.get("stall_active"))
    out["stalls_total"] = data.get("stalls_total")
    out["max_stall_s"] = data.get("max_stall_seconds")
    out["beat_age_s"] = data.get("beat_age_seconds")
    out["deadman_fired"] = bool(data.get("deadman_fired"))
    # The watchdog's status.json records the dead-man FILE, not an "armed" flag
    # (the flag only exists as a Prometheus gauge, and /metrics is served by the
    # loop — useless during the freeze this lane exists for). A file path present
    # means the GIL-free lane has somewhere to write, which is the thing worth
    # knowing off the box.
    out["deadman_armed"] = bool(data.get("deadman_file")) if "deadman_file" in data else None
    # REDUCED TO A BOOLEAN ON PURPOSE. The watchdog's last_error is a repr() of
    # an arbitrary exception; those routinely carry their inputs, and an input on
    # this appliance can be a patient id. The fact that the watchdog is erroring
    # is actionable; the text is not worth the risk of shipping it.
    out["watchdog_error"] = bool(data.get("last_error"))
    return out


def _collect_access(errors: set[str]) -> dict[str, Any]:
    """Admin/credential posture from services/bootstrap_service.

    Prefers ops/bootstrap/status.json so the standalone process sees the same
    thing the in-process thread does; falls back to the in-memory snapshot when
    the file has not been written yet (first boot, or a read-only ops dir).

    Only COUNTS cross the wire, never the account ids. Staff ids are not PHI, but
    a list of exactly which accounts hold a guessable password is an attack map,
    and it does not need to be in flight to be actionable — "1 account" is enough
    to start the phone call.
    """
    out = {
        "admins_active": None, "admins_with_mfa": None,
        "default_credential_accounts": None, "bootstrap_state": "unknown",
        "scan_complete": None,
    }
    data = _read_json(_bootstrap_dir() / "status.json")
    if data is None:
        try:
            from services.bootstrap_service import bootstrap_snapshot
            data = bootstrap_snapshot()
        except Exception:
            data = None
    if not data:
        return out
    state = str(data.get("state", "unknown"))
    out["bootstrap_state"] = state if state in ("ok", "error", "not_run") else "unknown"
    for key in ("admins_active", "admins_with_mfa"):
        val = data.get(key)
        out[key] = int(val) if isinstance(val, (int, float)) else None
    dca = data.get("default_credential_accounts")
    out["default_credential_accounts"] = len(dca) if isinstance(dca, list) else None
    if data.get("scan_skipped"):
        out["scan_complete"] = None
    elif data.get("scan_budget_exhausted") is not None:
        out["scan_complete"] = not bool(data.get("scan_budget_exhausted"))
    return out


def _collect_standby(errors: set[str]) -> dict[str, Any]:
    """Warm-standby freshness, from the receipt scripts/standby-restore.sh writes.

    On the PRIMARY this is normally absent and that is correct — the receipt
    lives on the standby Mac. Where the standby also beats, this is how we learn
    the nightly restore stopped working, which is the failure that turns a warm
    standby back into a cold one without anybody noticing.
    """
    out = {"receipt_present": False, "last_restore_age_s": None, "last_restore_ok": None}
    path = _standby_receipt_path()
    if not path.exists():
        return out
    data = _read_json(path)
    if data is None:
        errors.add("standby_receipt_unreadable")
        return out
    out["receipt_present"] = True
    out["last_restore_ok"] = bool(data.get("ok"))
    finished = data.get("finished_unixtime")
    if isinstance(finished, (int, float)):
        out["last_restore_age_s"] = int(max(0.0, time.time() - finished))
    return out


# ── Alerting ─────────────────────────────────────────────────────────────────
def _derive_alerts(payload: dict[str, Any]) -> tuple[list[str], str]:
    """Turn the measurements into a small set of paging codes plus a severity.

    Severity exists so the receiving end can decide what wakes somebody up.
    `critical` is reserved for states where patient-facing data is at risk RIGHT
    NOW or is unrecoverable: no usable backup, a frozen loop, a live default
    credential, an unencrypted PHI database. Everything else is `warn`.
    """
    alerts: list[str] = []
    access = payload.get("access", {})
    backup = payload.get("backup", {})
    loop = payload.get("loop", {})
    disk = payload.get("disk", {})
    models = payload.get("models", {})
    standby = payload.get("standby", {})

    max_age_h = _env_float("ORB_HEARTBEAT_BACKUP_MAX_AGE_H", 36.0)
    min_pct = _env_float("ORB_HEARTBEAT_DISK_MIN_PCT", 10.0)

    if access.get("admins_active") == 0:
        alerts.append("no_admin")
    if (access.get("default_credential_accounts") or 0) > 0:
        alerts.append("default_credentials")
    if access.get("admins_active") and not access.get("admins_with_mfa"):
        alerts.append("admin_without_mfa")

    if not backup.get("count"):
        alerts.append("backup_missing")
    else:
        age = backup.get("newest_age_s")
        if age is not None and age > max_age_h * 3600:
            alerts.append("backup_stale")
    payload["backup"]["stale"] = "backup_stale" in alerts
    payload["backup"]["max_age_h"] = max_age_h

    for vol in ("db_volume", "backup_volume"):
        pct = (disk.get(vol) or {}).get("free_pct")
        if pct is not None and pct < min_pct:
            if "disk_low" not in alerts:
                alerts.append("disk_low")

    if loop.get("stall_active"):
        alerts.append("loop_stalled")
    if loop.get("deadman_fired"):
        alerts.append("deadman_fired")
    # The watchdog refreshes status.json every 30 s by default. Six missed
    # refreshes means its daemon thread is not running either — a process that is
    # wedged below the level the Python watchdog can see.
    age = loop.get("status_age_s")
    if age is not None and age > 300:
        alerts.append("loop_status_stale")

    if models.get("configured") and models.get("warm") == 0:
        alerts.append("models_cold")
    if not payload.get("db", {}).get("encrypted_at_rest"):
        alerts.append("db_unencrypted")
    if standby.get("receipt_present"):
        r_age = standby.get("last_restore_age_s")
        if standby.get("last_restore_ok") is False or (r_age is not None and r_age > 36 * 3600):
            alerts.append("standby_stale")

    critical = {"no_admin", "default_credentials", "backup_missing", "loop_stalled",
                "deadman_fired", "db_unencrypted", "loop_status_stale"}
    severity = "critical" if any(a in critical for a in alerts) else ("warn" if alerts else "ok")
    return [a for a in alerts if a in _ALERT_CODES], severity


# ── The guard (runs in production, not only in tests) ────────────────────────
def _leaf_ok(value: Any, key: str = "") -> bool:
    """Is this leaf allowed to leave the appliance under this key?

    `key` is the immediate parent key (for a list member, the list's key). A
    string is only ever acceptable under a key in _STRING_KEYS, and where that
    key has a closed vocabulary the value must be in it.
    """
    if value is None or isinstance(value, (bool, int, float)):
        return True
    if isinstance(value, str):
        if key not in _STRING_KEYS:
            return False
        vocab = _ENUM_LISTS.get(key)
        if vocab is not None and value not in vocab:
            return False
        return bool(_SAFE_STR.match(value))
    return False


def scrub(node: Any, path: str = "", key: str = "") -> tuple[Any, list[str]]:
    """Recursively drop anything not on the allowlist or not leaf-shaped.

    Returns (clean_node, dropped_paths). Dropping rather than raising is
    deliberate: a heartbeat that refuses to send because one new field is
    malformed silences the alarm channel at the exact moment somebody has just
    changed the code. Losing a field is survivable; losing the beat is not, and
    leaking is not. Dropped paths are logged locally and counted in the payload
    as the `payload_guard_tripped` error code, so the loss is never silent.
    """
    dropped: list[str] = []
    if isinstance(node, dict):
        clean: dict[str, Any] = {}
        for child_key, value in node.items():
            here = f"{path}.{child_key}" if path else child_key
            if not isinstance(child_key, str) or child_key not in _ALLOWED_KEYS:
                dropped.append(here)
                continue
            sub, sub_dropped = scrub(value, here, child_key)
            dropped.extend(sub_dropped)
            clean[child_key] = sub
        return clean, dropped
    if isinstance(node, list):
        clean_list = []
        for i, item in enumerate(node):
            if _leaf_ok(item, key):
                clean_list.append(item)
            else:
                dropped.append(f"{path}[{i}]")
        return clean_list, dropped
    if _leaf_ok(node, key):
        return node, dropped
    dropped.append(path)
    return None, dropped


def build_payload() -> dict[str, Any]:
    """Assemble the heartbeat document. Issues ZERO database queries.

    Never raises: every collector swallows its own failures into an error code.
    A heartbeat that can throw is a heartbeat that stops during an incident.
    """
    errors: set[str] = set()

    db_info = _collect_db(errors)
    backup = _collect_backup(errors)
    loop = _collect_loop(errors)
    access = _collect_access(errors)
    models = _collect_models(errors)
    standby = _collect_standby(errors)

    disk = {
        "db_volume": _volume(_db_path(), errors),
        "backup_volume": _volume(_backup_dir(), errors),
    }

    # Uptime and pid: in-process we know them directly; standalone we take them
    # from the watchdog's status.json, which records the server process's start
    # time. That keeps one payload shape for both modes.
    pid: Optional[int] = os.getpid()
    started = _read_json(_watchdog_dir() / "status.json") or {}
    if _env("ORB_HEARTBEAT_STANDALONE") == "1":
        pid = started.get("process_pid")
        began = started.get("process_started_unixtime")
        uptime = int(time.time() - began) if isinstance(began, (int, float)) else None
    else:
        uptime = int(time.time() - _PROCESS_START)

    import sys as _sys
    payload: dict[str, Any] = {
        "schema": SCHEMA,
        "sent_utc": _utc_now(),
        "site": sanitise_site(_env("ORB_HEARTBEAT_SITE")),
        "appliance_id": appliance_id(),
        "orb_version": _env("ORB_VERSION", "0.1.0"),
        "profile": _env("ORB_DEPLOYMENT_PROFILE", "prod").lower(),
        "pid": pid,
        "uptime_s": uptime,
        "python": ".".join(str(p) for p in _sys.version_info[:3]),
        "db": db_info,
        "disk": disk,
        "backup": backup,
        "models": models,
        "loop": loop,
        "access": access,
        "standby": standby,
    }

    alerts, severity = _derive_alerts(payload)
    payload["alerts"] = alerts
    payload["severity"] = severity
    payload["errors"] = sorted(e for e in errors if e in _ERROR_CODES)

    clean, dropped = scrub(payload)
    if dropped:
        logger.warning(
            "Heartbeat payload guard dropped %d field(s) — they were not on the "
            "allowlist or not leaf-shaped: %s. Fix services/heartbeat_service."
            "_ALLOWED_KEYS if the field is legitimate; NOTHING was sent for them.",
            len(dropped), ", ".join(dropped[:10]),
        )
        errs = set(clean.get("errors") or [])
        errs.add("payload_guard_tripped")
        clean["errors"] = sorted(errs)
    return clean


_PROCESS_START = time.time()


# ── Transport ────────────────────────────────────────────────────────────────
def _post_json(url: str, body: bytes, timeout: float) -> int:
    """POST `body` and return the HTTP status. Raises on transport failure.

    urllib with an EXPLICITLY EMPTY ProxyHandler, not requests/httpx and not the
    default opener. Two reasons, both learned here:
      * urllib's default opener calls getproxies(), which on macOS goes through a
        configd IPC that can hang. A blocking call with no timeout on a daemon
        thread in a process that is already wedged is not a diagnosis tool, it is
        another wedge. The repo's httpx call sites pass trust_env=False for the
        same reason; this is that discipline in stdlib.
      * No new dependency, so the standalone mode runs on any python3 on the
        appliance, including one that cannot import the app.
    """
    import urllib.request

    headers = {
        "Content-Type": "application/json",
        "User-Agent": f"orb-heartbeat/{SCHEMA}",
        "Content-Length": str(len(body)),
    }
    token = _env("ORB_HEARTBEAT_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    secret = _env("ORB_HEARTBEAT_SECRET")
    if secret:
        # Sign the exact bytes on the wire. Lets the receiver reject a forged
        # "everything is fine" beat — which, for a dead-man's switch, is the one
        # message an attacker would want to send on the appliance's behalf.
        mac = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
        headers["X-Orb-Signature"] = f"sha256={mac}"

    import urllib.error

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with opener.open(req, timeout=timeout) as resp:
            return int(resp.status)
    except urllib.error.HTTPError as exc:
        # urllib RAISES on 4xx/5xx rather than returning them. A rejected beat is
        # a receiver-side problem we want reported as `http_<code>` — not
        # collapsed into "transport", which would make an expired token look
        # identical to a dead uplink and send somebody to check the wrong thing.
        return int(exc.code)


def send_once(url: Optional[str] = None) -> dict[str, Any]:
    """Build and send one heartbeat. Returns a small local result dict.

    NEVER raises and never blocks anything but its own thread. Disabled (no URL)
    is a normal, successful outcome, not an error — off by default is the
    shipped posture.
    """
    url = url or _env("ORB_HEARTBEAT_URL")
    payload = build_payload()
    if not url:
        return {"sent": False, "reason": "disabled", "severity": payload.get("severity")}

    if not url.lower().startswith("https://") and not _env_flag("ORB_HEARTBEAT_ALLOW_INSECURE", False):
        # The payload has no PHI, but it does describe exactly how to attack this
        # appliance (no admin / default credential / free disk / stale backup).
        # In cleartext on a hospital LAN that is a gift. Refuse unless somebody
        # has explicitly accepted it, once, in the environment.
        logger.warning(
            "ORB_HEARTBEAT_URL is not https and ORB_HEARTBEAT_ALLOW_INSECURE is not "
            "set — refusing to send. The payload has no PHI but does describe this "
            "appliance's weak points."
        )
        return {"sent": False, "reason": "insecure_url", "severity": payload.get("severity")}

    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    timeout = _env_float("ORB_HEARTBEAT_TIMEOUT_S", 10.0)
    try:
        status = _post_json(url, body, timeout)
    except Exception as exc:
        # Fails silently by contract. Logged at WARNING locally — the operator on
        # the box can see it — but nothing upstream is disturbed and no clinical
        # path knows this happened. Note the exception is logged, never sent.
        logger.warning("Heartbeat POST failed (%s): %s", type(exc).__name__, exc)
        return {"sent": False, "reason": "transport", "severity": payload.get("severity")}

    if status >= 400:
        logger.warning("Heartbeat endpoint returned HTTP %s", status)
        return {"sent": False, "reason": f"http_{status}", "severity": payload.get("severity")}
    return {"sent": True, "status": status, "severity": payload.get("severity"),
            "alerts": payload.get("alerts", []), "bytes": len(body)}


# ── In-process daemon thread ─────────────────────────────────────────────────
class _Heartbeat:
    def __init__(self) -> None:
        self.interval = max(30.0, _env_float("ORB_HEARTBEAT_INTERVAL_S", 300.0))
        self.jitter_pct = max(0.0, min(50.0, _env_float("ORB_HEARTBEAT_JITTER_PCT", 10.0)))
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.beats = 0
        self.failures = 0
        self.last_result: dict[str, Any] = {}

    def _sleep(self) -> None:
        """Wait one interval with jitter, interruptible by stop().

        Jitter so a fleet of appliances does not stampede the receiver on the
        same second after a regional power cut brings them all back at once.
        """
        import random
        span = self.interval * (self.jitter_pct / 100.0)
        self._stop.wait(max(5.0, self.interval + random.uniform(-span, span)))

    def _run(self) -> None:
        # A first beat immediately would race the bootstrap check and the
        # watchdog's first status write, and report "unknown" for both on every
        # restart. 45 s in, both files exist and the numbers are real.
        self._stop.wait(45.0)
        while not self._stop.is_set():
            try:
                result = send_once()
                self.last_result = result
                self.beats += 1
                if not result.get("sent") and result.get("reason") not in ("disabled",):
                    self.failures += 1
                if result.get("alerts"):
                    logger.warning("Heartbeat reported alerts: %s (severity=%s)",
                                   ", ".join(result["alerts"]), result.get("severity"))
            except Exception:  # a heartbeat must never die of its own bug
                self.failures += 1
                logger.exception("Heartbeat iteration failed")
            self._sleep()

    def start(self) -> threading.Thread:
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="orb-heartbeat", daemon=True)
        self._thread.start()
        return self._thread

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        self._thread = None


_hb: Optional[_Heartbeat] = None
_hb_lock = threading.Lock()


def start_heartbeat() -> Optional[threading.Thread]:
    """Start the in-process heartbeat thread, or return None if not configured.

    Called from main.py's lifespan. Off unless ORB_HEARTBEAT_URL is set, so the
    shipped default is silence — no appliance ever calls home by accident.
    """
    global _hb
    if not _env("ORB_HEARTBEAT_URL"):
        logger.info("Outbound heartbeat disabled (ORB_HEARTBEAT_URL not set)")
        return None
    if not _env_flag("ORB_HEARTBEAT_ENABLED", True):
        logger.info("Outbound heartbeat disabled (ORB_HEARTBEAT_ENABLED=0)")
        return None
    with _hb_lock:
        if _hb is not None:
            return _hb._thread
        hb = _Heartbeat()
        thread = hb.start()
        _hb = hb
    logger.info(
        "Outbound heartbeat armed — every %.0fs (±%.0f%%) to %s. No PHI: the payload "
        "is a fixed key allowlist of operational values and makes no database read.",
        hb.interval, hb.jitter_pct, _env("ORB_HEARTBEAT_URL").split("?")[0],
    )
    return thread


def stop_heartbeat() -> None:
    global _hb
    with _hb_lock:
        if _hb is None:
            return
        _hb.stop()
        _hb = None


def heartbeat_status() -> dict[str, Any]:
    """Local status for an admin/health surface. Never raises."""
    hb = _hb
    if hb is None:
        return {"enabled": False}
    return {
        "enabled": True,
        "interval_s": hb.interval,
        "beats": hb.beats,
        "failures": hb.failures,
        "last_result": dict(hb.last_result),
    }


# ── Standalone mode ──────────────────────────────────────────────────────────
def _main(argv: list[str] | None = None) -> int:
    """`python -m services.heartbeat_service` — out-of-process heartbeat.

    Run this as its own launchd job when you want the beat to survive not just a
    stalled event loop but a wedged interpreter. It reads the same on-disk
    sources and produces a byte-identical payload shape.
    """
    import argparse

    parser = argparse.ArgumentParser(
        prog="heartbeat_service",
        description="Send Orb's no-PHI appliance heartbeat.",
    )
    parser.add_argument("--once", action="store_true", help="Send a single beat and exit (default).")
    parser.add_argument("--loop", action="store_true", help="Beat forever on the configured interval.")
    parser.add_argument("--print", dest="show", action="store_true",
                        help="Print the exact JSON that would be sent, and do not send it.")
    parser.add_argument("--url", default=None, help="Override ORB_HEARTBEAT_URL.")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    os.environ.setdefault("ORB_HEARTBEAT_STANDALONE", "1")

    if args.show:
        print(json.dumps(build_payload(), indent=2))
        return 0

    if args.loop:
        hb = _Heartbeat()
        try:
            while True:
                result = send_once(args.url)
                logger.info("beat: %s", result)
                hb._sleep()
        except KeyboardInterrupt:
            return 0

    result = send_once(args.url)
    logger.info("beat: %s", result)
    return 0 if result.get("sent") or result.get("reason") == "disabled" else 1


if __name__ == "__main__":  # pragma: no cover
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    raise SystemExit(_main())
