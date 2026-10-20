"""Orb Hospital OS — the operations status page, in plain language.

WHO THIS IS FOR
───────────────
One person: the hospital's own IT staff member, on site, at an hour when there
is no vendor to ring. Orb ships with NO remote support by design, which means
every failure this appliance can have must be diagnosable by someone standing in
front of it who did not write it.

That constraint decides the whole shape of this module. It answers eight
questions in sentences, and each answer that is not "fine" comes with the thing
to DO about it:

    1. Is Orb running?                     5. Are the bedside feeds flowing?
    2. Is the patient database healthy?    6. Is there disk space left?
    3. Is there a backup, and DID IT       7. Is this site's own configuration
       ACTUALLY VERIFY?                       loaded and consistent?
    4. Is the medical AI loaded?           8. Can anyone still sign in as admin?
                                           9. Which hospital sites does this
                                              appliance hold, and are their
                                              records kept apart? (multi-site)

WHY QUESTION 3 IS PHRASED THAT WAY
──────────────────────────────────
"Is there a backup" was already answerable and was already answered wrongly. For
fourteen consecutive nights this system wrote zero-byte files, passed its own
integrity gate (an empty database is a valid database), pruned the real backups
to make room, and reported success every time. A file existing proves nothing.

So a backup counts as verified here ONLY when something opened it, checked it,
found real clinical rows in it, and left a dated receipt beside it
(`scripts/backup.sh` writes one through scripts/lib/verify_restore.py
--receipt, on pass and on fail; the schema is `VERIFICATION_SCHEMA` below).
A snapshot with no receipt is reported as exactly that: present, unproven. That
includes backups made from the admin dashboard, which do not write receipts —
and saying so is more useful than pretending otherwise.

NO PHI. ANYWHERE.
─────────────────
This page is the single most likely thing in the product to be screenshotted
into a group chat while someone asks for help. It carries counts, ages, sizes,
department names and configuration — never a patient name, a record number, or
an identifier that leads to one. `tests/test_ops_health.py` seeds a patient with
an unmistakable name and asserts it appears nowhere in any rendering.

WHY TWO THRESHOLDS ARE CALLED ORB_HEARTBEAT_*
─────────────────────────────────────────────
Backup staleness and minimum free disk are read from
ORB_HEARTBEAT_BACKUP_MAX_AGE_H and ORB_HEARTBEAT_DISK_MIN_PCT. The names are
left over from the outbound heartbeat, which was removed when Orb went strictly
offline (no connection of any kind leaves the building). They keep those names
because a site may already set them, and this module is now the only thing that
interprets them.

NEVER RAISES. A status page that can throw is a status page that goes blank
during the incident it exists for. Every collector converts its own failure into
a check that says, in words, that it could not find out.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger("orb.ops_health")

# ── Status vocabulary ────────────────────────────────────────────────────────
# Three levels, not five. An operator triaging at 3 a.m. can act on "urgent /
# needs attention / fine"; a nine-point severity scale is a thing to interpret
# rather than a thing to act on.
OK = "ok"
ATTENTION = "attention"
URGENT = "urgent"
_RANK = {OK: 0, ATTENTION: 1, URGENT: 2}
_LABEL = {OK: "Working", ATTENTION: "Needs attention", URGENT: "Urgent"}

# The receipt scripts/backup.sh writes beside every backup it has proven good.
# Versioned because a future check (restore rehearsal, off-box confirmation)
# will want to add fields without an old receipt being misread as a new one.
VERIFICATION_SCHEMA = "orb.backup.verification/1"

_BACKUP_PREFIX = "orb-backup-"
_BACKUP_SUFFIX = ".db"
_RECEIPT_SUFFIX = ".verified.json"

_PROCESS_START = time.time()

# Probing the model engine costs a network round trip. The status page is the
# kind of thing a dashboard polls, so the answer is held briefly — long enough
# that refreshing the page twice does not cost two probes, short enough that an
# operator who has just started the engine sees it within a few seconds.
_MODEL_CACHE_TTL_S = 5.0
_model_cache: tuple[float, dict] | None = None


# ═════════════════════════════════════════════════════════════════════════════
#  Small helpers
# ═════════════════════════════════════════════════════════════════════════════
def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def _env_float(name: str, default: float) -> float:
    try:
        return float(_env(name) or default)
    except ValueError:
        return default


def _server_dir() -> Path:
    return Path(__file__).resolve().parent.parent


def _db_path() -> Path:
    return Path(_env("ORB_DB_PATH") or (_server_dir() / "orb.db"))


def _backup_dir() -> Path:
    """Where backups live: <repo root>/backups, matching config.BACKUP_DIR.

    Anchored to _server_dir().parent, NOT to _db_path().parent. Those are the
    same on the stock layout only if ORB_DB_PATH is unset — and when it was set
    (a test, a relocated database) this check silently began grading a directory
    nobody writes to. Worse, unset it resolved to server/backups while
    scripts/backup.sh wrote to <repo>/backups, so this page could report "there
    is no backup of this hospital's data at all" with last night's backup sitting
    one directory up. Deliberately does not import config: this module is run
    standalone (`python server/services/ops_health_service.py`) where server/ is
    not yet on sys.path at import time.
    """
    env = _env("ORB_BACKUP_DIR")
    return Path(env) if env else _server_dir().parent / "backups"


def _bytes_human(n: int | float | None) -> str:
    """Sizes as an operator would say them out loud."""
    if n is None:
        return "unknown"
    n = float(n)
    for unit in ("bytes", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit in ("bytes", "KB") else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"  # pragma: no cover


def _age_human(seconds: float | None) -> str:
    """Durations in the units a person would use, never in seconds past a minute."""
    if seconds is None:
        return "unknown"
    seconds = max(0.0, float(seconds))
    if seconds < 90:
        return "less than a minute" if seconds < 60 else "a minute"
    minutes = seconds / 60
    if minutes < 90:
        return f"{minutes:.0f} minutes"
    hours = minutes / 60
    if hours < 36:
        return f"{hours:.0f} hours"
    return f"{hours / 24:.0f} days"


def _check(
    key: str,
    title: str,
    status: str,
    summary: str,
    *,
    what_to_do: str | None = None,
    detail: dict | None = None,
    notes: list[str] | None = None,
) -> dict:
    return {
        "key": key,
        "title": title,
        "status": status,
        "status_label": _LABEL[status],
        "summary": summary,
        "what_to_do": what_to_do,
        "notes": notes or [],
        "detail": detail or {},
    }


def _broken(key: str, title: str, exc: Exception) -> dict:
    """A collector that failed still owes the operator a sentence.

    It reports that Orb could not find out — which is itself actionable — rather
    than vanishing from the page and letting absence read as health.
    """
    logger.warning("ops status collector %s failed: %s", key, exc, exc_info=True)
    return _check(
        key,
        title,
        ATTENTION,
        "Orb could not check this. That does not mean it is broken. It means nobody knows.",
        what_to_do=(
            "Run the on-site check from a terminal for the full reason:\n"
            "    ./scripts/orb.sh status"
        ),
        detail={"error": type(exc).__name__},
    )


# ═════════════════════════════════════════════════════════════════════════════
#  1. Is Orb running?
# ═════════════════════════════════════════════════════════════════════════════
def _check_server() -> dict:
    # THE PROFILE IS OBSERVED, NOT READ OFF A LABEL.
    #
    # This used to be `_env("ORB_DEPLOYMENT_PROFILE", "prod")`, and that default
    # inverted the whole check: an appliance where nobody had ever set the
    # variable read back "prod", the `profile != "prod"` branch never fired, and
    # the page told the on-site operator everything was fine. The one reader this
    # page exists for — the person standing in front of the box at 3 a.m. with no
    # vendor to ring — was the person most harmed by guessing in the reassuring
    # direction.
    #
    # `effective_deployment_posture` (services/compliance_service.py) is the
    # single definition of the answer, shared with the auditor-facing Trust
    # Center so the two screens cannot disagree about which mode this box is in.
    # It returns "prod" only when prod was explicitly declared AND no development
    # posture is observable (dev signing secret, dev ingestion key, running
    # vitals simulator, demo mode).
    posture: dict = {}
    try:
        from services.compliance_service import effective_deployment_posture

        posture = effective_deployment_posture()
    except Exception as exc:  # pragma: no cover - defensive; this page never raises
        logger.warning("ops status: deployment posture probe failed: %s", exc)
    # No posture => unknown => the WEAKER claim. Never "prod".
    profile = str(posture.get("effective") or "unknown")
    signals = posture.get("dev_signals") or []
    uptime = time.time() - _PROCESS_START
    detail = {
        "uptime_seconds": int(uptime),
        "version": _env("ORB_VERSION", "0.1.0"),
        "profile": profile,
        "declared_profile": posture.get("declared", "unknown"),
        "dev_signals": [s.get("code") for s in signals],
        "process_id": os.getpid(),
    }
    if profile != "prod":
        # Name the actual reasons. "Set ORB_DEPLOYMENT_PROFILE=prod" is the wrong
        # instruction when the variable already says prod and what is really
        # happening is that a vitals simulator is filling charts with fiction.
        reasons = "\n".join(f"  · {s.get('detail', '')}" for s in signals)
        return _check(
            "server",
            "Orb server",
            ATTENTION,
            f"Orb is running (started {_age_human(uptime)} ago) but it is in "
            f"DEVELOPER mode, not hospital mode.",
            what_to_do=(
                "Developer mode relaxes several protections that a hospital "
                "needs. What Orb can see right now:\n"
                f"{reasons if reasons else '  · The deployment profile could not be read at all.'}\n"
                "Fix each line above in the settings file `.env` (profile, "
                "ORB_JWT_SECRET, ORB_INGESTION_API_KEY, ORB_INGESTION_MODE, "
                "ORB_DEMO_MODE), then restart Orb."
            ),
            detail=detail,
        )
    return _check(
        "server",
        "Orb server",
        OK,
        f"Orb is running and answering. It has been up for {_age_human(uptime)}.",
        detail=detail,
    )


# ═════════════════════════════════════════════════════════════════════════════
#  2. Is the patient database healthy?
# ═════════════════════════════════════════════════════════════════════════════
def _check_database(deep: bool) -> dict:
    path = _db_path()
    detail: dict[str, Any] = {"file": path.name, "deep_check_run": deep}
    notes: list[str] = []

    if not path.is_file():
        return _check(
            "database",
            "Patient database",
            URGENT,
            "The patient database file is missing. Orb cannot store or retrieve "
            "any patient information.",
            what_to_do=(
                "Do NOT let staff carry on entering data. It has nowhere to go.\n"
                "Restore the most recent backup, then restart Orb:\n"
                "    ./scripts/orb.sh restore --list"
            ),
            detail=detail,
        )

    detail["size"] = _bytes_human(path.stat().st_size)
    detail["size_bytes"] = path.stat().st_size
    pending = Path(f"{path}-wal")
    detail["pending_writes_bytes"] = pending.stat().st_size if pending.exists() else 0

    encrypted = bool(_env("ORB_DB_ENCRYPTION_KEY"))
    uploads_encrypted = bool(_env("ORB_UPLOAD_ENCRYPTION_KEY"))
    detail["encrypted_on_disk"] = encrypted
    detail["scans_and_recordings_encrypted"] = uploads_encrypted

    # Can we actually read it? A file of the right size that no longer opens is
    # the failure worth catching, and it costs one trivial query.
    readable = True
    counts: dict[str, int] = {}
    try:
        from database import db_session

        with db_session() as db:
            for table in ("patients", "admissions", "users", "audit_logs"):
                try:
                    counts[table] = db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                except Exception:
                    counts[table] = -1
            if deep:
                # Bounded on purpose: the thorough check reads every page and can
                # take minutes on a large database, which is not something a
                # status page may do to a live ward.
                result = db.execute("PRAGMA quick_check(1)").fetchone()[0]
                detail["structure_check"] = result
    except Exception as e:
        readable = False
        detail["open_error"] = type(e).__name__

    detail["records"] = counts

    if not readable:
        return _check(
            "database",
            "Patient database",
            URGENT,
            "The patient database exists but Orb cannot open it.",
            what_to_do=(
                "The usual cause is a missing or changed encryption key. Check "
                "that ORB_DB_ENCRYPTION_KEY in the settings file `.env` is the "
                "same value this appliance was installed with. If the key is "
                "genuinely lost, the data cannot be recovered. Restore from a "
                "backup taken with the key you still have."
            ),
            detail=detail,
        )

    if detail.get("structure_check") not in (None, "ok"):
        return _check(
            "database",
            "Patient database",
            URGENT,
            "The patient database opens, but its internal structure check "
            "failed. Some records may be damaged.",
            what_to_do=(
                "Take a backup NOW so the current state is preserved, then "
                "restore the most recent verified backup:\n"
                "    ./scripts/orb.sh backup\n"
                "    ./scripts/orb.sh restore --list"
            ),
            detail=detail,
        )

    if not encrypted:
        return _check(
            "database",
            "Patient database",
            URGENT,
            "The patient database is readable, but it is NOT encrypted on disk. "
            "Anyone who takes this machine can read every patient record on it.",
            what_to_do=(
                "Stop, and encrypt it before real patients are entered:\n"
                "    ./scripts/orb.sh encrypt-database\n"
                "Keep a copy of the encryption key somewhere that does not "
                "depend on this machine, without it the data is unrecoverable."
            ),
            detail=detail,
        )

    if not uploads_encrypted:
        notes.append(
            "Scans and voice recordings are stored unencrypted. The database is "
            "encrypted but these files are not."
        )

    patients = counts.get("patients", 0)
    return _check(
        "database",
        "Patient database",
        OK,
        f"The database is open and healthy, holding {patients} patient "
        f"record(s) in {detail['size']}. It is encrypted on disk.",
        detail=detail,
        notes=notes,
    )


# ═════════════════════════════════════════════════════════════════════════════
#  3. Is there a backup, and did it VERIFY?
# ═════════════════════════════════════════════════════════════════════════════
def _read_receipt(backup: Path) -> dict | None:
    """The proof-of-verification left beside a backup, if there is one."""
    receipt = backup.with_name(backup.name[: -len(_BACKUP_SUFFIX)] + _RECEIPT_SUFFIX)
    if not receipt.is_file():
        return None
    try:
        data = json.loads(receipt.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    if str(data.get("schema", "")).split("/")[0] != VERIFICATION_SCHEMA.split("/")[0]:
        return None
    return data


def _check_backups() -> dict:
    directory = _backup_dir()
    max_age_h = _env_float("ORB_HEARTBEAT_BACKUP_MAX_AGE_H", 36.0)
    detail: dict[str, Any] = {
        "directory": str(directory),
        "count": 0,
        "newest_verified": False,
        "expected_max_age_hours": max_age_h,
    }
    notes: list[str] = []

    files: list[Path] = []
    if directory.is_dir():
        files = sorted(
            directory.glob(f"{_BACKUP_PREFIX}*{_BACKUP_SUFFIX}"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
    detail["count"] = len(files)

    if not files:
        return _check(
            "backups",
            "Backups",
            URGENT,
            "There is no backup of this hospital's data at all. If this machine "
            "fails right now, everything on it is gone.",
            what_to_do=(
                "Take one immediately, then schedule it nightly:\n"
                "    ./scripts/orb.sh backup\n"
                "    ./scripts/orb.sh install-backup-schedule"
            ),
            detail=detail,
        )

    newest = files[0]
    stat = newest.stat()
    age_s = max(0.0, time.time() - stat.st_mtime)
    receipt = _read_receipt(newest)
    verified = bool(receipt and receipt.get("ok") is True)

    detail.update(
        {
            "newest_file": newest.name,
            "newest_age_seconds": int(age_s),
            "newest_size": _bytes_human(stat.st_size),
            "newest_size_bytes": stat.st_size,
            "newest_verified": verified,
            "has_receipt": receipt is not None,
            "media_archive_present": newest.with_name(
                newest.name[: -len(_BACKUP_SUFFIX)] + "-uploads.tar"
            ).exists(),
        }
    )
    if receipt:
        detail["verification"] = {
            "passed": bool(receipt.get("ok")),
            "structure_check": receipt.get("integrity_check"),
            "records_found": receipt.get("row_counts"),
            "checked_at": receipt.get("verified_utc"),
        }

    stale = age_s > max_age_h * 3600
    age_phrase = f"The newest backup is {_age_human(age_s)} old"

    # Verification failed outright — the most dangerous state on this page,
    # because a scheduled job is running and reporting into the void.
    if receipt is not None and not verified:
        return _check(
            "backups",
            "Backups",
            URGENT,
            f"{age_phrase}, and it FAILED its check: Orb opened it and found it "
            f"unusable. Treat this appliance as having no usable backup.",
            what_to_do=(
                "Run a backup by hand and watch it finish:\n"
                "    ./scripts/orb.sh backup\n"
                "If it fails again, note the message it prints and stop using "
                "this machine for new patients until it succeeds. There is "
                "nothing to fall back to."
            ),
            detail=detail,
            notes=notes,
        )

    if not verified:
        summary = (
            f"{age_phrase}. Orb has NO record that it was ever checked, so "
            f"nobody can promise it would restore."
        )
        if stale:
            summary = (
                f"{age_phrase}, older than the {max_age_h:.0f} hours this site "
                f"allows, and Orb has no record that it was checked."
            )
        return _check(
            "backups",
            "Backups",
            ATTENTION,
            summary,
            what_to_do=(
                "Run the backup command, which checks its own work and leaves a "
                "dated receipt beside the file:\n"
                "    ./scripts/orb.sh backup\n"
                "Backups taken from the admin screen do not leave a receipt. "
                "That is why this says 'unchecked' rather than 'broken'."
            ),
            detail=detail,
            notes=notes,
        )

    if stale:
        return _check(
            "backups",
            "Backups",
            ATTENTION,
            f"{age_phrase}, which is older than the {max_age_h:.0f} hours this "
            f"site allows. It was checked successfully when it was made, so it "
            f"is usable. It is just out of date.",
            what_to_do=(
                "The nightly backup has probably stopped running. Take one now, "
                "then confirm the schedule:\n"
                "    ./scripts/orb.sh backup\n"
                "    ./scripts/orb.sh install-backup-schedule"
            ),
            detail=detail,
            notes=notes,
        )

    offsite = _env("ORB_BACKUP_OFFSITE_DIR")
    if not offsite:
        notes.append(
            "Backups are kept only on this machine. A fire, a theft or a dead "
            "disk takes the backups with the original. Set a second destination "
            "(a hospital file server or an external drive) so a copy leaves the "
            "room."
        )
    if not detail["media_archive_present"]:
        notes.append(
            "Scans and voice recordings were not included in the newest backup. "
            "Restoring it would give you notes that refer to images which no "
            "longer exist."
        )

    rows = (receipt or {}).get("row_counts") or {}
    proof = ""
    if rows:
        proof = (
            f" It contains {rows.get('patients', '?')} patient record(s) "
            f"and {rows.get('audit_logs', '?')} history entries."
        )
    return _check(
        "backups",
        "Backups",
        OK,
        f"{age_phrase} ({detail['newest_size']}) and was checked successfully: "
        f"it opens and holds real records.{proof}",
        detail=detail,
        notes=notes,
    )


# ═════════════════════════════════════════════════════════════════════════════
#  4. Is the medical AI loaded?
# ═════════════════════════════════════════════════════════════════════════════
def _probe_models(timeout: float = 1.5) -> dict:
    global _model_cache
    now = time.time()
    if _model_cache and now - _model_cache[0] < _MODEL_CACHE_TTL_S:
        return _model_cache[1]

    import urllib.request

    base = (_env("ORB_OLLAMA_BASE_URL", "http://localhost:11434")).rstrip("/")
    configured = _configured_models()
    out: dict[str, Any] = {
        "reachable": False,
        "configured": configured,
        "loaded": [],
        "not_loaded": configured,
    }
    try:
        # An explicitly empty proxy handler: the default opener asks the
        # operating system for proxy settings, and on macOS that call can hang
        # with no timeout. A status page must not be able to hang.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f"{base}/api/ps", timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        resident = {m.get("name", "") for m in body.get("models", [])}
        out["reachable"] = True
        out["loaded"] = [m for m in configured if m in resident]
        out["not_loaded"] = [m for m in configured if m not in resident]
    except Exception:
        pass
    _model_cache = (now, out)
    return out


def _configured_models() -> list[str]:
    """The models the running app actually uses, as config resolved them.

    Reading ORB_LLM_MODEL from the environment was wrong twice over: config
    substitutes an installed medgemma variant when the configured one is not
    pulled, so the page could report "not loaded" for a model the app never
    calls, and the thinking-off 4B tier was never probed at all. The raw
    environment is only the fallback for a standalone run where config cannot
    be imported.
    """
    try:
        import config  # lazy: this module also runs standalone

        models = [config.LLM_MODEL, config.AMBIENT_LLM_MODEL, config.THINKING_FAST_MODEL]
    except Exception:
        primary = _env("ORB_LLM_MODEL", "medgemma:27b")
        models = [
            primary,
            _env("ORB_AMBIENT_LLM_MODEL") or primary,
            _env("ORB_FAST_MODEL", "medgemma:4b"),
        ]
    return [m for m in dict.fromkeys(models) if m]


def _models_switched_off() -> bool:
    """True when the models are switched off on this computer
    (ORB_MODELS_ENABLED off, or the Orb Ward Record profile). Read through
    services.model_switch, the one reader; from the environment when this
    module runs standalone and the server cannot be imported."""
    try:
        from services.model_switch import models_enabled

        return not models_enabled()
    except Exception:
        on = ("1", "true", "yes", "on")
        return (
            _env("ORB_WARD_RECORD", "false").lower() in on
            or _env("ORB_MODELS_ENABLED", "true").lower() not in on
        )


def _check_models() -> dict:
    # Switched off on this computer: Ollama is not asked at all, and a model
    # that is not running is the expected state, not a fault.
    if _models_switched_off():
        return _check(
            "models",
            "Medical AI",
            OK,
            "The medical AI is switched off on this computer. Nothing is sent "
            "to it and nothing is loaded. Charts, observations and the rest of "
            "Orb work normally.",
            detail={
                "models_enabled": False,
                "reachable": None,
                "configured": [],
                "loaded": [],
                "not_loaded": [],
            },
        )
    info = _probe_models()
    detail = dict(info)

    if not info["reachable"]:
        return _check(
            "models",
            "Medical AI",
            URGENT,
            "The medical AI engine is not answering. Sage, dictation and image "
            "reading will all fail; the rest of Orb (charts, observations, "
            "medications, alerts) keeps working normally.",
            what_to_do=(
                "Start the AI engine on this machine:\n"
                "    ollama serve\n"
                "Then check it is back:\n"
                "    ./scripts/orb.sh status"
            ),
            detail=detail,
        )

    if not info["loaded"]:
        return _check(
            "models",
            "Medical AI",
            ATTENTION,
            "The AI engine is running but no model is loaded into memory. The "
            "first question a doctor asks will take about a minute to answer; "
            "everything after that will be fast.",
            what_to_do=(
                "Nothing is broken. To pre-load it before a ward round, ask "
                "Sage one question and wait for the answer. Admin → Model "
                "Governance lists the models this site runs."
            ),
            detail=detail,
        )

    return _check(
        "models",
        "Medical AI",
        OK,
        f"The medical AI is loaded and warm ({len(info['loaded'])} of "
        f"{len(info['configured'])} model(s) in memory). Answers will be prompt.",
        detail=detail,
    )


# ═════════════════════════════════════════════════════════════════════════════
#  5. Are the bedside feeds flowing?
# ═════════════════════════════════════════════════════════════════════════════
def _check_integrations() -> dict:
    mode = _env("ORB_INGESTION_MODE", "ingestion")
    monitor_on = _env("ORB_MONITOR_ENABLED", "false").lower() in ("1", "true", "yes", "on")
    detail: dict[str, Any] = {"data_mode": mode, "bedside_monitors_enabled": monitor_on}
    notes: list[str] = []

    sources: list[dict] = []
    try:
        from services.vitals_source.registry import registry

        status = registry.status()
        sources = status.get("sources", []) or []
        detail["feed_count"] = status.get("count", 0)
    except Exception as e:  # the registry is optional at import time
        detail["feed_count"] = 0
        detail["error"] = type(e).__name__

    if mode == "simulation":
        return _check(
            "integrations",
            "Bedside data feeds",
            ATTENTION,
            "Orb is generating FAKE observations for demonstration. Nothing on "
            "screen came from a real patient.",
            what_to_do=(
                "Before real patients are entered, set ORB_INGESTION_MODE="
                "ingestion in the settings file `.env` and restart Orb."
            ),
            detail=detail,
        )

    if not monitor_on and not sources:
        return _check(
            "integrations",
            "Bedside data feeds",
            OK,
            "No bedside monitor feed is connected, so observations are the ones "
            "nurses type in. That is a supported way to run Orb.",
            what_to_do=None,
            detail=detail,
            notes=[
                "Connecting the ward monitors removes the typing step and "
                "lets Orb notice a deterioration between rounds."
            ],
        )

    running = [s for s in sources if s.get("running")]
    quiet: list[str] = []
    errored: list[str] = []
    for source in sources:
        stats = source.get("stats") or {}
        if stats.get("last_error"):
            errored.append(str(source.get("id") or source.get("kind") or "feed"))
        last = stats.get("last_reading_at")
        if not last:
            quiet.append(str(source.get("id") or source.get("kind") or "feed"))
    detail["running"] = len(running)
    detail["never_delivered"] = len(quiet)
    detail["reporting_errors"] = len(errored)

    if sources and not running:
        return _check(
            "integrations",
            "Bedside data feeds",
            URGENT,
            f"{len(sources)} bedside feed(s) are configured but none is running. "
            f"Observations are NOT arriving from the monitors, and staff may not "
            f"realise they now have to enter them by hand.",
            what_to_do=(
                "Tell the ward to chart observations manually until this is "
                "fixed, then restart Orb:\n"
                "    ./scripts/orb.sh restart\n"
                "If it still does not connect, check the network cable and the "
                "monitor gateway's address in the settings file `.env`."
            ),
            detail=detail,
        )

    if errored:
        return _check(
            "integrations",
            "Bedside data feeds",
            ATTENTION,
            f"{len(running)} bedside feed(s) are running, but {len(errored)} "
            f"reported an error recently. Some readings may be missing.",
            what_to_do=(
                "Ask the ward whether observations are appearing for every bed. "
                "The full message is in the Admin → Ingestion screen."
            ),
            detail=detail,
        )

    if quiet:
        notes.append(
            f"{len(quiet)} feed(s) are connected but have never delivered a "
            f"reading. That usually means the monitor is switched off or is "
            f"pointed at the wrong address."
        )

    if monitor_on and not sources:
        return _check(
            "integrations",
            "Bedside data feeds",
            ATTENTION,
            "Bedside monitors are switched on in the settings, but no feed has started.",
            what_to_do=(
                "Restart Orb and watch the start-up messages:\n    ./scripts/orb.sh restart"
            ),
            detail=detail,
        )

    return _check(
        "integrations",
        "Bedside data feeds",
        OK,
        f"{len(running)} bedside feed(s) are connected and delivering readings.",
        detail=detail,
        notes=notes,
    )


# ═════════════════════════════════════════════════════════════════════════════
#  6. Is there disk space left?
# ═════════════════════════════════════════════════════════════════════════════
def _volume(path: Path) -> dict:
    probe = path
    for _ in range(6):
        if probe.exists():
            break
        probe = probe.parent
    usage = shutil.disk_usage(str(probe))
    return {
        "free_bytes": usage.free,
        "total_bytes": usage.total,
        "free": _bytes_human(usage.free),
        "free_percent": round(100.0 * usage.free / usage.total, 1) if usage.total else None,
    }


def _check_disk() -> dict:
    min_pct = _env_float("ORB_HEARTBEAT_DISK_MIN_PCT", 10.0)
    volumes = {"database": _volume(_db_path()), "backups": _volume(_backup_dir())}
    detail: dict[str, Any] = {"volumes": volumes, "warn_below_percent": min_pct}

    # `is None`, never `or`: 0.0 free is falsy, and `free_percent or 100.0`
    # read a FULL disk as an empty one (a 0.0%-rounded volume over ~4 TB still
    # has >2 GB free, so the byte floor below did not catch it). An unknown
    # percentage sorts last; free bytes break a tie.
    def _pct(vol: dict) -> float:
        return 100.0 if vol["free_percent"] is None else float(vol["free_percent"])

    worst_name, worst = min(volumes.items(), key=lambda kv: (_pct(kv[1]), kv[1]["free_bytes"]))
    pct = _pct(worst)

    # Below a quarter of the warning level, or under 2 GB, writes start failing
    # outright — including the audit trail, which must never fail to record.
    if pct < min_pct / 4 or worst["free_bytes"] < 2 * 1024**3:
        return _check(
            "disk",
            "Storage space",
            URGENT,
            f"The disk holding the {worst_name} is almost full, only "
            f"{worst['free']} left ({pct:.0f}%). Orb is close to being unable to "
            f"save new records.",
            what_to_do=(
                "Free space now. The safe things to remove, in order:\n"
                "  1. Old backups already copied off this machine.\n"
                "  2. Log files under the ops folder.\n"
                "Do not delete anything under the uploads folder. Those are "
                "patient scans and recordings."
            ),
            detail=detail,
        )

    if pct < min_pct:
        return _check(
            "disk",
            "Storage space",
            ATTENTION,
            f"The disk holding the {worst_name} has {worst['free']} free "
            f"({pct:.0f}%), below the {min_pct:.0f}% this site expects.",
            what_to_do=(
                "Plan to free space or add a disk this week. Old backups that "
                "have already been copied off the machine are the usual "
                "candidate."
            ),
            detail=detail,
        )

    return _check(
        "disk",
        "Storage space",
        OK,
        f"There is plenty of room: {volumes['database']['free']} free on the "
        f"database disk, {volumes['backups']['free']} on the backup disk.",
        detail=detail,
    )


# ═════════════════════════════════════════════════════════════════════════════
#  7. Is this site's own configuration loaded and consistent?
# ═════════════════════════════════════════════════════════════════════════════
def _check_site_config(deep: bool) -> dict:
    from services.site_config_service import current_site_config, site_config_snapshot

    snapshot = site_config_snapshot()
    cfg = current_site_config()
    detail: dict[str, Any] = dict(snapshot)
    notes: list[str] = list(snapshot.get("notes") or [])

    if not snapshot["present"]:
        return _check(
            "site_config",
            "Site settings",
            ATTENTION,
            "This appliance has no site settings file, so it is running on the "
            "defaults Orb ships with, including the standard escalation "
            "timings, which may not match this hospital.",
            what_to_do=(
                "Create it from the annotated example and restart Orb:\n"
                "    cp server/site_config/site.example.toml "
                "server/site_config/site.toml\n"
                "Then edit the departments, wards and escalation timings to "
                "match this hospital."
            ),
            detail=detail,
            notes=notes,
        )

    conflicts = snapshot.get("env_conflicts") or []
    if conflicts:
        notes.append(
            f"{len(conflicts)} setting(s) are written in both the site settings "
            f"file and the machine settings file. The site settings file wins. "
            f"Remove the duplicates from `.env` so there is one place to look."
        )

    # Departments in the file versus departments patients are actually admitted
    # to. A mismatch is not cosmetic: staff see patients by department, so a
    # department the file never declared is a ward whose staff may be looking at
    # an empty screen while the patients sit in the database.
    unknown: dict[str, int] = {}
    if deep and cfg.departments:
        try:
            from database import db_session

            declared = {d["name"].strip().lower() for d in cfg.departments}
            with db_session() as db:
                rows = db.execute(
                    "SELECT department, COUNT(*) AS n FROM admissions "
                    "WHERE status = 'active' AND department IS NOT NULL "
                    "GROUP BY department"
                ).fetchall()
            for row in rows:
                name = (row["department"] or "").strip()
                if name and name.lower() not in declared:
                    unknown[name] = row["n"]
        except Exception as e:
            detail["department_check_error"] = type(e).__name__
    detail["undeclared_departments"] = unknown

    if unknown:
        listed = ", ".join(f"{k} ({v} patient(s))" for k, v in sorted(unknown.items()))
        return _check(
            "site_config",
            "Site settings",
            ATTENTION,
            f"Patients are admitted under department names this hospital's "
            f"settings file does not list: {listed}. Staff recorded under the "
            f"listed spelling will not see those patients.",
            what_to_do=(
                "Either add the department to the site settings file, or correct "
                "the spelling on the admissions. Department names must match "
                "exactly, including capital letters."
            ),
            detail=detail,
            notes=notes,
        )

    tiers = snapshot.get("escalation_tiers") or []
    ladder = " → ".join(t["label"] for t in tiers) if tiers else "Orb's default ladder"
    return _check(
        "site_config",
        "Site settings",
        OK,
        f"Settings loaded for {snapshot['site_name']}: "
        f"{snapshot['department_count']} department(s), "
        f"{len(snapshot.get('wards') or [])} ward(s). Alerts escalate {ladder}.",
        detail=detail,
        notes=notes,
    )


# ═════════════════════════════════════════════════════════════════════════════
#  8. Can anyone still sign in as an administrator?
# ═════════════════════════════════════════════════════════════════════════════
def _check_access() -> dict:
    from services.bootstrap_service import bootstrap_snapshot

    snap = bootstrap_snapshot() or {}
    admins = snap.get("admins_active")
    with_mfa = snap.get("admins_with_mfa")
    weak = snap.get("default_credential_accounts")
    weak_count = len(weak) if isinstance(weak, list) else (weak or 0)
    detail = {
        "administrators": admins,
        "administrators_with_second_factor": with_mfa,
        "accounts_with_a_shipped_password": weak_count,
        "state": snap.get("state"),
    }

    if weak_count:
        return _check(
            "access",
            "Sign-in and administrators",
            URGENT,
            f"{weak_count} staff account(s) still use one of the passwords Orb "
            f"ships with. Anyone who has read the manual can sign in as them "
            f"and open patient records.",
            what_to_do=(
                "Change them today. List them with:\n"
                "    ./scripts/orb.sh accounts\n"
                "then reset each one from Admin → Users."
            ),
            detail=detail,
        )

    if admins == 0:
        return _check(
            "access",
            "Sign-in and administrators",
            URGENT,
            "There is no administrator account on this appliance. Nobody can "
            "add staff, reset a password, or change any setting from the screen.",
            what_to_do=(
                "Create one at this machine's keyboard:\n"
                "    ./scripts/orb.sh create-admin\n"
                "There is no way to do this over the network, by design."
            ),
            detail=detail,
        )

    if admins and not with_mfa:
        return _check(
            "access",
            "Sign-in and administrators",
            ATTENTION,
            f"{admins} administrator account(s) exist, but none uses a second "
            f"step at sign-in. A guessed password is enough to reach every "
            f"patient record.",
            what_to_do=(
                "Ask each administrator to turn on the second step from their "
                "profile screen. It takes a phone and about a minute."
            ),
            detail=detail,
        )

    if admins is None:
        return _check(
            "access",
            "Sign-in and administrators",
            ATTENTION,
            "Orb has not finished checking the staff accounts yet.",
            what_to_do="Refresh this page in a minute.",
            detail=detail,
        )

    return _check(
        "access",
        "Sign-in and administrators",
        OK,
        f"{admins} administrator account(s), all using a second step at "
        f"sign-in. No account is using a password Orb ships with.",
        detail=detail,
    )


# ═════════════════════════════════════════════════════════════════════════════
#  9. Which hospital sites does this appliance hold? (multi-site, multisite-1)
# ═════════════════════════════════════════════════════════════════════════════
def _spellings(items: list[dict], limit: int = 4) -> str:
    shown = [
        f"'{v['spelling']}' on {v['rows']} {v['where']} (listed as '{v['master_spelling']}')"
        for v in items[:limit]
    ]
    more = len(items) - limit
    return "; ".join(shown) + (f"; and {more} more" if more > 0 else "")


def _check_facilities() -> dict:
    """Facility 1 present and active; no record naming a facility that does
    not exist; no patient with admissions at two facilities (the count must be
    0); department spellings that match the departments list; and, once more
    than one facility is on record, what the facility fence is holding shut.
    Counts, facility codes and department names only, never a patient."""
    import config
    import facility_fence
    from database import db_session

    from services import facility_service, ingestion_service

    with db_session() as db:
        report = facility_service.facility_report(db)
        main_name = facility_service.display_name(db=db)
        # multisite-16: which facility each ingestion source files to.
        sources = ingestion_service.source_binding_report(db)
    multi = report["facilities"] > 1
    variants = report["department_variants"]
    unmastered = report["departments_not_in_master"]
    unknown = report["unknown_facility_ids"]
    several = report["patients_at_several_facilities"]
    switch_on = bool(config.feature_enabled("multi_site"))
    detail = {
        "facilities": report["facilities"],
        "active_facilities": report["active_facilities"],
        "more_than_one_facility_on_record": multi,
        "multi_site_switch": switch_on,
        "departments_listed": report["department_master"],
        "records_naming_an_unknown_facility": unknown,
        "department_spelling_variants": variants,
        "departments_not_in_the_list": unmastered,
        "patients_with_admissions_at_several_facilities": several,
        "facility_fence": {**facility_fence.describe(), "holding": multi},
        "ingestion_sources": sources,
    }

    problems: list[tuple[str, str, str]] = []
    if report["facility_one_problem"]:
        problems.append(
            (
                URGENT,
                "This hospital's own facility record (facility 1) is missing or marked "
                "inactive. Orb is running now, but it will refuse to start after the "
                "next restart.",
                "The database was changed by hand. Restore the most recent verified "
                "backup before restarting Orb:\n    ./scripts/orb.sh restore --list",
            )
        )
    if several:
        problems.append(
            (
                URGENT,
                f"{several} patient record(s) have admissions at more than one facility. "
                f"Orb works out which facility a chart belongs to from its admissions, "
                f"so these charts cannot be kept to one facility.",
                "Do not admit these patients at another facility again. Ask the person "
                "who installed Orb to review them before any further facility is set up.",
            )
        )
    if unknown:
        listed = ", ".join(f"{n} {label}" for label, n in sorted(unknown.items()))
        problems.append(
            (
                ATTENTION,
                f"Some records name a facility that is not on record: {listed}.",
                "These records were changed by hand or came back from a damaged "
                "restore. Restore the most recent verified backup, or ask the person "
                "who installed Orb to correct them.",
            )
        )
    if multi:
        problems.append(
            (
                ATTENTION,
                f"{report['facilities']} facilities are on record, so Orb keeps each "
                f"facility's patients apart. Screens not yet ready for that answer "
                f'"not available yet" instead of showing every facility\'s patients.',
                "This is expected until every screen has been prepared for more than one "
                "facility. If the extra facility was added by mistake, restore the backup "
                "taken before it was added, from this machine's keyboard (a facility is "
                "never deleted from a screen):\n    ./scripts/orb.sh restore --list",
            )
        )
    if variants:
        problems.append(
            (
                ATTENTION,
                f"Some records spell a department differently from the departments "
                f"list: {_spellings(variants)}. Staff see patients by the exact "
                f"spelling, so these patients may be missing from their ward's screens.",
                "Correct the spelling on those records to match the departments list "
                "exactly, including capital letters.",
            )
        )
    if unmastered and multi:
        names = ", ".join(sorted({u["name"] for u in unmastered})[:6])
        problems.append(
            (
                ATTENTION,
                f"{len(unmastered)} department name(s) in use are not in the departments "
                f"list ({names}), so Orb cannot tell which facility they belong to.",
                "Add each one to the departments list of the right facility, or correct "
                "the spelling to a listed department.",
            )
        )
    if multi and (sources["unbound"] or sources["refused"]):
        # multisite-16, ruling MS-R14 (a): such readings were filed to no patient.
        told = sources["unbound"] + [
            f"'{r['source_name'] or 'unnamed'}' was refused {r['refusals']} time(s): {r['words']}"
            for r in sources["refused"]
        ]
        problems.append(
            (
                ATTENTION,
                f"Some bedside or laboratory feeds are not filing to any patient, because more "
                f"than one facility is on record and each feed files only to its own "
                f"facility's patients: {'; '.join(told[:4])}"
                + (f"; and {len(told) - 4} more." if len(told) > 4 else "."),
                "Bind each feed to its facility. A bedside monitor feed: list its source name "
                "under that facility in the site settings file, for example\n"
                '    [[facilities]]\n    code = "MAIN"\n    ingestion_sources = ["monitor_hl7"]\n'
                "then restart Orb. A laboratory or gateway that sends over the network: give it "
                "its facility's own key, ORB_INGESTION_API_KEY_<facility code>, in the machine "
                "settings file. Readings refused so far were put on no patient's chart; each "
                "message is kept in the ingestion record, and laboratory results wait as "
                "unmatched where that list is switched on.",
            )
        )

    notes: list[str] = []
    if unmastered and not multi:
        notes.append(
            f"{len(unmastered)} department name(s) in use are not in the departments "
            f"list yet. That matters only once a second facility is set up, because "
            f"the list says which facility each department belongs to."
        )
    if switch_on and not multi:
        notes.append(
            "ORB_MULTI_SITE is on. Only one facility is on record, so nothing "
            "changes until a second one is set up."
        )

    if problems:
        rank = max(_RANK[p[0]] for p in problems)
        first = next(p for p in problems if _RANK[p[0]] == rank)
        notes = [p[1] for p in problems if p is not first] + notes
        return _check(
            "facilities",
            "Facilities",
            first[0],
            first[1],
            what_to_do=first[2],
            detail=detail,
            notes=notes,
        )
    return _check(
        "facilities",
        "Facilities",
        OK,
        f"One facility is on record ({main_name}, code "
        f"{facility_service.MAIN_FACILITY_CODE}), with "
        f"{report['department_master']} department(s) in the departments list.",
        detail=detail,
        notes=notes,
    )


# ═════════════════════════════════════════════════════════════════════════════
#  Assembly
# ═════════════════════════════════════════════════════════════════════════════
_COLLECTORS = (
    ("server", "Orb server", lambda deep: _check_server()),
    ("database", "Patient database", _check_database),
    ("backups", "Backups", lambda deep: _check_backups()),
    ("models", "Medical AI", lambda deep: _check_models()),
    ("integrations", "Bedside data feeds", lambda deep: _check_integrations()),
    ("disk", "Storage space", lambda deep: _check_disk()),
    ("site_config", "Site settings", _check_site_config),
    ("access", "Sign-in and administrators", lambda deep: _check_access()),
    ("facilities", "Facilities", lambda deep: _check_facilities()),
)


def build_status(deep: bool = False) -> dict:
    """The whole status page as data. Blocking; never raises.

    `deep` adds the checks that cost real work — a structure check on the
    database, and a comparison of the departments patients are admitted under
    against the departments this site declared. Off by default so a dashboard
    can poll the light version without touching the disk hard.
    """
    checks: list[dict] = []
    for key, title, collector in _COLLECTORS:
        try:
            checks.append(collector(deep))
        except Exception as e:
            checks.append(_broken(key, title, e))

    overall = OK
    for check in checks:
        if _RANK[check["status"]] > _RANK[overall]:
            overall = check["status"]

    urgent = [c for c in checks if c["status"] == URGENT]
    attention = [c for c in checks if c["status"] == ATTENTION]
    if urgent:
        headline = f"{len(urgent)} thing(s) need dealing with now" + (
            f", and {len(attention)} more need attention soon." if attention else "."
        )
    elif attention:
        headline = f"Orb is working. {len(attention)} thing(s) need attention soon."
    else:
        headline = "Orb is working normally. Nothing needs attention."

    from services.site_config_service import current_site_config

    site = current_site_config().site_name

    return {
        "generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "site_name": site,
        "version": _env("ORB_VERSION", "0.1.0"),
        "overall": overall,
        "overall_label": _LABEL[overall],
        "headline": headline,
        "deep": deep,
        "checks": checks,
    }


# ═════════════════════════════════════════════════════════════════════════════
#  Rendering — the same answer, for a terminal
# ═════════════════════════════════════════════════════════════════════════════
_MARK = {OK: "[ ok ]", ATTENTION: "[ !! ]", URGENT: "[FAIL]"}


def _wrap(text: str, width: int, indent: str) -> list[str]:
    out: list[str] = []
    for paragraph in text.split("\n"):
        line = indent
        for word in paragraph.split():
            if len(line) + len(word) + 1 > width and line.strip():
                out.append(line.rstrip())
                line = indent
            line += word + " "
        out.append(line.rstrip())
    return out


def render_text(payload: dict, width: int = 78) -> str:
    """The status page for someone at a keyboard with no browser.

    This is the rendering that matters most: it is what an engineer sees over
    a remote shell, what gets pasted into an email, and what the installer
    prints when it refuses to proceed.
    """
    lines = [
        "═" * width,
        f"  ORB STATUS: {payload.get('site_name') or 'unnamed site'}",
        f"  {payload['generated_utc']}   ·   Orb {payload['version']}",
        "═" * width,
        "",
        f"  {payload['overall_label'].upper()}: {payload['headline']}",
        "",
    ]
    for check in payload["checks"]:
        lines.append(f"{_MARK[check['status']]}  {check['title']}")
        lines += _wrap(check["summary"], width, "        ")
        for note in check.get("notes") or []:
            lines += _wrap("- " + note, width, "        ")
        if check.get("what_to_do"):
            lines.append("")
            lines.append("        WHAT TO DO:")
            lines += _wrap(check["what_to_do"], width, "          ")
        lines.append("")
    lines += [
        "─" * width,
        "  Full detail:   ./scripts/orb.sh status --json",
        "  How to fix:    docs/OPERATIONS.md",
        "",
    ]
    return "\n".join(lines)


if __name__ == "__main__":  # pragma: no cover - operator entry point
    import argparse

    parser = argparse.ArgumentParser(description="Orb on-site status check")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--deep", action="store_true", help="run the slower checks too")
    args = parser.parse_args()

    report = build_status(deep=args.deep)
    print(json.dumps(report, indent=2) if args.json else render_text(report))
    # Exit code so a scheduled job can act on it: 0 fine, 1 attention, 2 urgent.
    raise SystemExit(_RANK[report["overall"]])
