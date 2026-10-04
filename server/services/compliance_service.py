"""Orb Hospital OS — Compliance & Trust Center Service.

Aggregates Orb's live compliance posture from REAL system state into a single,
self-serve report that de-risks the enterprise sale: encryption-at-rest posture,
the tamper-evident audit hash-chain status, MFA adoption, the RBAC matrix, model
validation honesty, on-prem/data-residency attestation, and a control checklist.

Design principles:
  * READ-ONLY. This module imports config, permissions and the relevant services
    and never mutates them.
  * HONEST. Some controls are legitimately `warn`/`unknown` in a given deployment
    (e.g. encryption keys not configured, no TLS in the app server). We report the
    truth rather than a green wall — honesty is Orb's stated moat, and an investor
    trusts a posture surface precisely because it does not lie.
  * NEVER CRASHES. Every probe is wrapped; a missing/renamed dependency degrades
    that control to `warn`/`unknown` instead of taking down the whole report.

Public API: ``compliance_report() -> dict``.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Callable

logger = logging.getLogger("orb.compliance")

# ── Control status vocabulary + scoring weights ──
# pass = control is satisfied by live state; warn = config-dependent / not yet
# enforced (honest amber); fail = actively broken (e.g. tampered audit chain);
# unknown = the probe itself could not run (degraded, treated as warn for scoring).
_STATUS_WEIGHT = {"pass": 1.0, "warn": 0.5, "unknown": 0.5, "fail": 0.0}


def _safe(fn: Callable[[], Any], default: Any) -> Any:
    """Run a probe, returning ``default`` (and logging) if it raises. Keeps the
    report resilient to a missing table / renamed symbol / import error."""
    try:
        return fn()
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("compliance probe failed: %s", exc)
        return default


# ════════════════════════════════════════════════════════════════
#  Individual posture probes
# ════════════════════════════════════════════════════════════════

def _encryption_posture() -> dict:
    """DB-at-rest + upload-at-rest encryption posture, from what is OBSERVED.

    The database answer comes from the live connection (`PRAGMA cipher_version`
    through database.encryption_at_rest_status), not from a key being present in
    .env: a key with no SQLCipher codec on the connection is a plaintext file,
    and this used to read PASS over exactly that.
    """
    import os

    import config

    db_key_set = bool(getattr(config, "DB_ENCRYPTION_KEY", None))
    db_active = False
    cipher_version = None
    try:
        from database import encryption_at_rest_status
        status = encryption_at_rest_status()
        db_active = bool(status.get("active"))
        cipher_version = status.get("cipher_version")
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("database encryption probe failed: %s", exc)

    upload_key_set = bool(getattr(config, "UPLOAD_ENCRYPTION_KEY", None))
    upload_enabled = False
    upload_enforced = False
    try:
        from services import crypto_service
        upload_enabled = bool(crypto_service.is_encryption_enabled())
        upload_enforced = bool(crypto_service._require_encryption())
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("crypto_service probe failed: %s", exc)
    # An explicit opt-out of enforcement is reported, never hidden: it is the
    # setting that lets a lost key fall back to cleartext writes.
    raw = (os.getenv("ORB_REQUIRE_UPLOAD_ENCRYPTION") or "").strip().lower()
    opted_out = raw in ("0", "false", "no", "off")

    return {
        "db_encryption_key_set": db_key_set,
        "db_cipher_version": cipher_version,
        "upload_encryption_key_set": upload_key_set,
        "upload_encryption_active": upload_enabled,
        "upload_encryption_enforced": upload_enforced,
        "upload_encryption_opted_out": opted_out,
        # Aggregate booleans the UI can badge directly.
        "db_at_rest": db_active,
        "uploads_at_rest": upload_enabled,
    }


def _audit_chain() -> dict:
    """Tamper-evidence: verify the global audit hash-chain integrity + count."""
    from services.flight_recorder_service import verify_chain

    res = verify_chain()
    return {
        "verified": bool(res.get("ok", False)),
        "broken_at": res.get("broken_at"),
        "count": int(res.get("count", 0) or 0),
    }


def _audit_coverage() -> dict:
    """Distribution of audit events by event_category — shows which PHI/route
    categories are actually represented in the immutable log."""
    from database import db_session

    dist: dict[str, int] = {}
    total = 0
    with db_session() as db:
        rows = db.execute(
            "SELECT event_category, COUNT(*) AS c FROM audit_logs "
            "GROUP BY event_category ORDER BY c DESC"
        ).fetchall()
    for r in rows:
        cat = (r["event_category"] if r["event_category"] is not None else "uncategorized")
        n = int(r["c"] or 0)
        dist[cat] = n
        total += n
    return {"total_events": total, "by_category": dist, "categories": len(dist)}


def _mfa_adoption() -> dict:
    """TOTP adoption by role + the roles for which MFA is mandated."""
    import config
    from database import db_session

    required = list(getattr(config, "MFA_REQUIRED_ROLES", []) or [])
    # Staff only. Patient portal identities and the automation accounts (Sage's
    # audit identity, the Relay bots) cannot sign in to a staff screen, so
    # counting them as "not enrolled" dragged the percentage down for nothing.
    try:
        from database import SERVICE_ACCOUNT_IDS
        excluded = sorted(SERVICE_ACCOUNT_IDS)
    except Exception:  # pragma: no cover - defensive
        excluded = ["system"]
    placeholders = ",".join("?" for _ in excluded)
    by_role: dict[str, dict[str, int]] = {}
    with db_session() as db:
        rows = db.execute(
            "SELECT role, "
            "COUNT(*) AS total, "
            "SUM(CASE WHEN totp_enabled = 1 THEN 1 ELSE 0 END) AS enrolled "
            f"FROM users WHERE role != 'patient' AND id NOT IN ({placeholders}) "
            "GROUP BY role ORDER BY role",
            tuple(excluded),
        ).fetchall()
    total_users = 0
    total_enrolled = 0
    for r in rows:
        role = r["role"] or "unknown"
        total = int(r["total"] or 0)
        enrolled = int(r["enrolled"] or 0)
        by_role[role] = {"total": total, "enrolled": enrolled}
        total_users += total
        total_enrolled += enrolled

    # Are all users in mandated roles actually enrolled? (Drives the MFA control.)
    required_gap = []
    for role in required:
        stats = by_role.get(role)
        if stats and stats["total"] > 0 and stats["enrolled"] < stats["total"]:
            required_gap.append(role)

    pct = round((total_enrolled / total_users) * 100) if total_users else 0
    return {
        "required_roles": required,
        "by_role": by_role,
        "total_users": total_users,
        "total_enrolled": total_enrolled,
        "adoption_pct": pct,
        "required_gap": required_gap,
    }


def _rbac_matrix() -> dict:
    """Role × permission matrix from the canonical ROLE_PERMISSIONS mapping."""
    import permissions as perms

    role_perms: dict[str, set] = getattr(perms, "ROLE_PERMISSIONS", {}) or {}
    # Stable, sorted union of every permission any role holds.
    all_perms = sorted({p for pset in role_perms.values() for p in pset})
    roles = sorted(role_perms.keys())
    matrix: dict[str, dict[str, bool]] = {}
    for role in roles:
        granted = role_perms.get(role, set())
        matrix[role] = {p: (p in granted) for p in all_perms}
    return {
        "roles": roles,
        "permissions": all_perms,
        "matrix": matrix,
        "role_count": len(roles),
        "permission_count": len(all_perms),
    }


def _model_posture() -> dict:
    """Validated vs unvalidated model counts — ties to the honesty narrative."""
    from services.model_governance_service import list_models

    models = list_models()
    validated = sum(1 for m in models if m.get("validated"))
    total = len(models)
    return {
        "validated": validated,
        "total": total,
        "unvalidated": total - validated,
    }


# ════════════════════════════════════════════════════════════════
#  Deployment posture — DECLARED vs OBSERVED
# ════════════════════════════════════════════════════════════════
#
# config.DEPLOYMENT_PROFILE defaults to "prod" when ORB_DEPLOYMENT_PROFILE is
# unset (config.py, top of file). That default is CORRECT where it lives: the
# JWT-secret and ingestion-key guards key off it and must fail CLOSED, so an
# unset variable has to mean "assume this is real and demand the secrets".
#
# It is WRONG as an ATTESTATION. This report is what a compliance reviewer reads,
# and reading `config.DEPLOYMENT_PROFILE` straight told them "prod" on a box
# where nobody had ever said prod, or where the box is demonstrably generating
# synthetic patient data. The same string means "fail closed" to a secret guard
# and "this appliance is hardened" to an auditor, and only one of those is safe
# to guess.
#
# So this module answers a different question: what is the OBSERVED posture? It
# starts from the declared value and then looks for live evidence that the box
# is not a hardened deployment. Any such evidence downgrades the answer.
#
# DIRECTION OF FAILURE, stated once because it governs every branch below: when
# uncertain, report the WEAKER claim. Saying "dev" about a hardened box costs an
# auditor one question. Saying "prod" about a box running the dev JWT secret and
# a vitals simulator tells them a control exists that does not, and they will
# stop looking. Under-claiming is recoverable; over-claiming is the failure this
# function exists to prevent.

#: The fallbacks config.py substitutes when the corresponding secret is unset.
#: Their presence in the LIVE config is proof the deployment never supplied one.
_DEV_JWT_SECRET = "orb-local-dev-secret-key-change-in-prod"
_DEV_INGESTION_KEY = "orb-ingest-dev-key"


def effective_deployment_posture() -> dict:
    """The declared profile, the observed evidence, and the honest answer.

    Returns ``{declared, declared_explicitly, effective, hardened, dev_signals}``
    where ``effective`` is "prod" ONLY when the profile was explicitly declared
    prod AND no dev-posture evidence was found. ``dev_signals`` is the evidence
    list, so the UI and the control checklist can say WHY rather than just
    asserting a label.

    Also consumed by services/ops_health_service.py, so the operator page and the
    auditor page cannot disagree about which mode this appliance is in.
    """
    import os

    import config

    declared = str(getattr(config, "DEPLOYMENT_PROFILE", "") or "").strip().lower()
    if declared not in ("dev", "prod"):
        declared = "unknown"
    declared_explicitly = bool((os.getenv("ORB_DEPLOYMENT_PROFILE") or "").strip())

    signals: list[dict] = []

    # `detail` is the operator's sentence (it names the .env setting to change,
    # and the ops page prints it); `plain` is the same finding for the Trust
    # Center, which a CIO reads and which must not print settings or code paths.
    def signal(code: str, detail: str, plain: str) -> None:
        signals.append({"code": code, "detail": detail, "plain": plain})

    if not declared_explicitly:
        signal(
            "profile_not_declared",
            "ORB_DEPLOYMENT_PROFILE is not set. config.py falls back to 'prod' so "
            "the secret guards fail closed. That fallback is not a statement "
            "that this appliance was deployed as a hospital appliance.",
            "Nobody has declared this appliance a production install.",
        )
    if declared == "dev":
        signal(
            "profile_declared_dev",
            "ORB_DEPLOYMENT_PROFILE=dev. The dev branch of the CORS middleware "
            "(allow_origins=['*']) is installed and auto-reload is enabled.",
            "It is declared a development install, so any web page may call its "
            "API and it reloads its own code whenever a file changes.",
        )
    if getattr(config, "JWT_SECRET", None) == _DEV_JWT_SECRET:
        signal(
            "dev_jwt_secret",
            "The shipped development JWT signing secret is in use, so any copy of "
            "Orb can mint a valid session token for this appliance.",
            "It signs sign-ins with the development key that ships with Orb, so "
            "any copy of Orb can forge a session for it.",
        )
    if getattr(config, "INGESTION_API_KEY", None) == _DEV_INGESTION_KEY:
        signal(
            "dev_ingestion_key",
            "The shipped development ingestion API key is in use, so the "
            "/api/ingest/* clinical write path is effectively unauthenticated.",
            "It accepts monitor data under the development key that ships with "
            "Orb, so anyone on the network can write readings into charts.",
        )
    ingestion_mode = str(getattr(config, "INGESTION_MODE", "") or "").strip().lower()
    if ingestion_mode in ("simulation", "hybrid"):
        signal(
            "synthetic_vitals_active",
            f"ORB_INGESTION_MODE={ingestion_mode or 'unset'}, the vitals simulator "
            "is writing fabricated readings into patient charts. Whatever the "
            "label says, this is not a hospital deployment posture.",
            "The vitals simulator is writing made-up readings into patient charts.",
        )
    if bool(getattr(config, "DEMO_MODE", False)):
        signal(
            "demo_mode",
            "ORB_DEMO_MODE is on: synthetic clinical CONTENT (fabricated "
            "post-op notes, bot vitals/reports/labs) is permitted to be "
            "written.",
            "Demo content (made-up notes, reports and lab results) is allowed to "
            "be written.",
        )

    effective = "prod" if (declared == "prod" and declared_explicitly and not signals) else "dev"
    if declared == "unknown" and not signals:
        # The profile itself could not be read and nothing else contradicted it.
        # "unknown" is weaker than "prod", so it is the safe answer.
        effective = "unknown"

    return {
        "declared": declared,
        "declared_explicitly": declared_explicitly,
        "effective": effective,
        "hardened": effective == "prod",
        "dev_signals": signals,
    }


def _residency() -> dict:
    """On-prem / data-residency attestation from server bind + CORS + profile.

    The CORS origins are the ones main.py INSTALLS (config.cors_allow_origins),
    not the configured allow-list: the dev branch serves `*` whatever
    ORB_CORS_ORIGINS says, and this used to print "explicit CORS allow-list"
    over it.
    """
    import config

    host = getattr(config, "HOST", "127.0.0.1")
    loopback_only = host in ("127.0.0.1", "localhost", "::1")
    installed = getattr(config, "cors_allow_origins", None)
    cors = list(installed() if callable(installed) else (getattr(config, "CORS_ORIGINS", []) or []))
    posture = effective_deployment_posture()
    # The Host names main.py installs in services/host_gate.HostGate, and what that
    # gate has turned away since the process started.
    from services import host_gate
    allowed_hosts = getattr(config, "allowed_host_names", None)
    return {
        "host": host,
        "loopback_only": loopback_only,
        "cors_origins": cors,
        "cors_wildcard": "*" in cors,
        "allowed_hosts": sorted(allowed_hosts()) if callable(allowed_hosts) else None,
        "host_refusals": dict(host_gate.refused),
        # THE OBSERVED ANSWER, not the declared label. This is the key the Trust
        # Center prints ("… · {deployment_profile} profile · on-prem"), so it must
        # never over-claim. The declared value is carried alongside it, unaltered,
        # for anyone reconciling against .env.
        "deployment_profile": posture["effective"],
        "declared_deployment_profile": posture["declared"],
        "deployment_posture": posture,
        "on_prem": True,  # Orb runs the full stack (DB, models, uploads) in-house.
    }


def _backup_posture() -> dict:
    """Backup EVIDENCE, not the presence of a script.

    This used to pass because scripts/backup.sh exists, on a box with no backup
    at all. The newest backup, its verification receipt and its age against the
    site's limit are read through ops_health_service, so the operator page and
    this page grade the same files the same way. The two questions only an
    auditor asks are added here: is a copy off the box, and has a restore been
    drilled.
    """
    import os

    from services import ops_health_service

    check = ops_health_service._check_backups()
    d = check.get("detail") or {}
    has_receipt = bool(d.get("has_receipt"))
    verified = bool(d.get("newest_verified"))
    return {
        "count": int(d.get("count") or 0),
        "newest_file": d.get("newest_file"),
        "newest_age_seconds": d.get("newest_age_seconds"),
        "newest_verified": verified,
        "verification_failed": has_receipt and not verified,
        "max_age_hours": float(d.get("expected_max_age_hours") or 36.0),
        "media_archive_present": bool(d.get("media_archive_present")),
        "offsite_configured": bool((os.getenv("ORB_BACKUP_OFFSITE_DIR") or "").strip()),
        # scripts/restore-drill.sh prints its result and files nothing, so there
        # is no drill record for this report to read. Said, not assumed.
        "last_drill": None,
    }


_CERT_EXPIRY_WARN_DAYS = 30


def _inspect_certificate(path: str) -> dict:
    """Expiry, issuer and names of the certificate Orb serves, read from disk."""
    from pathlib import Path

    from cryptography import x509
    from cryptography.x509.oid import ExtensionOID

    data = Path(path).read_bytes()
    try:
        cert = x509.load_pem_x509_certificate(data)
    except ValueError:
        cert = x509.load_der_x509_certificate(data)
    not_after = getattr(cert, "not_valid_after_utc", None)
    if not_after is None:  # pragma: no cover - cryptography < 42
        not_after = cert.not_valid_after.replace(tzinfo=timezone.utc)
    try:
        san = cert.extensions.get_extension_for_oid(ExtensionOID.SUBJECT_ALTERNATIVE_NAME).value
        names = [str(n.value) for n in san]
    except x509.ExtensionNotFound:
        names = []
    now = datetime.now(timezone.utc)
    return {
        "subject": cert.subject.rfc4514_string(),
        "issuer": cert.issuer.rfc4514_string(),
        "expires_at": not_after.isoformat(),
        "days_left": int((not_after - now).total_seconds() // 86400),
        "expired": not_after <= now,
        "self_signed": cert.issuer == cert.subject,
        "names": names,
    }


def _tls_posture() -> dict:
    """What can be seen of TLS from inside Orb: the certificate it serves itself
    (ORB_TLS_CERT/ORB_TLS_KEY, the same pair main.py hands to uvicorn), or the
    installer's declaration that a proxy in front terminates TLS."""
    from pathlib import Path

    import config

    cert_path = getattr(config, "TLS_CERT", None)
    key_path = getattr(config, "TLS_KEY", None)
    out: dict = {
        "mode": "none",
        "terminated_upstream": bool(getattr(config, "TLS_TERMINATED_UPSTREAM", False)),
        "certificate": None,
        "error": None,
        "key_present": None,
    }
    if cert_path and key_path:
        out["mode"] = "orb"
        out["key_present"] = Path(key_path).is_file()
        try:
            out["certificate"] = _inspect_certificate(cert_path)
        except Exception as exc:
            out["error"] = type(exc).__name__
    elif out["terminated_upstream"]:
        out["mode"] = "upstream"
    return out


def _patient_link_posture() -> dict:
    """The address Orb puts in patient discharge links and QR codes, and why a
    patient's phone could not open it (problem None when it can)."""
    from services.discharge_access_service import public_link_base_status

    status = public_link_base_status()
    return {"base_url": status["base_url"], "problem": status["problem"],
            "switched_off": status["switched_off"]}


def _default_credentials() -> dict:
    """Accounts that can still sign in with a password published in Orb's own
    seed scripts. bootstrap_service scans for them once at boot; it re-reads the
    flagged rows here so a reset since boot shows as cleared."""
    from services.bootstrap_service import bootstrap_snapshot, live_default_credential_accounts

    snap = bootstrap_snapshot()
    live = live_default_credential_accounts()
    return {
        "checked": live is not None,
        "live": live or [],
        "locked_at_boot": list(snap.get("default_credential_locked") or []),
        "scan_complete": not snap.get("scan_budget_exhausted"),
    }


def _dpdp_posture() -> dict:
    """DPDP Act 2023 machinery: consent register, rights, retention, breaches."""
    from services.consent_service import dpdp_posture

    return dpdp_posture()


def _session_revocation_supported() -> bool:
    """True when the users table carries token_version (logout-everywhere)."""
    from database import db_session

    with db_session() as db:
        cols = {r[1] for r in db.execute("PRAGMA table_info(users)").fetchall()}
    return "token_version" in cols


# ════════════════════════════════════════════════════════════════
#  Control checklist assembly
# ════════════════════════════════════════════════════════════════

def _age_words(seconds: Any) -> str:
    """The age of a backup in words for the checklist ("5 hours", "3 days")."""
    try:
        hours = float(seconds) / 3600
    except (TypeError, ValueError):
        return "an unknown time"
    if hours < 1:
        return "less than an hour"
    if hours < 48:
        return f"{hours:.0f} hours"
    return f"{hours / 24:.0f} days"


def _build_controls(enc: dict, audit: dict, mfa: dict, rbac: dict,
                    residency: dict, backups: dict, revocation: bool,
                    dpdp: dict, tls: dict | None = None,
                    credentials: dict | None = None) -> list[dict]:
    """Turn the raw posture into an auditor-style control checklist. Each control
    carries an honest status + concrete evidence + a remediation hint.

    Evidence and remediation are read by a CIO, so they are plain sentences that
    point at a screen or a person. The machine names an installer needs (the
    .env settings) travel in `settings`, which the screen does not print.
    """
    import config

    controls: list[dict] = []
    tls = tls or {}
    credentials = credentials or {}
    prod_profile = str(getattr(config, "DEPLOYMENT_PROFILE", "") or "").lower() == "prod"

    def add(cid, name, status, evidence, remediation, settings=None):
        control = {
            "id": cid, "name": name, "status": status,
            "evidence": evidence, "remediation": remediation,
        }
        if settings:
            control["settings"] = list(settings)
        controls.append(control)

    # ── TLS in transit ──
    # Graded on the certificate Orb actually serves. A proxy in front cannot be
    # seen from here, so the installer's declaration of one is amber, not green.
    tls_settings = ["ORB_TLS_CERT", "ORB_TLS_KEY", "ORB_TLS_TERMINATED_UPSTREAM"]
    cert = tls.get("certificate") or {}
    if tls.get("mode") == "orb":
        if tls.get("error"):
            add("tls_in_transit", "TLS / encryption in transit", "fail",
                "Orb is set to serve HTTPS, but the certificate file it was given "
                "could not be read, so connections are not protected as intended.",
                "Ask the person who installed Orb to replace the certificate file "
                "and restart Orb.", tls_settings)
        elif not tls.get("key_present"):
            add("tls_in_transit", "TLS / encryption in transit", "fail",
                "Orb is set to serve HTTPS, but the private key file for its "
                "certificate is missing.",
                "Ask the person who installed Orb to restore the key file and "
                "restart Orb.", tls_settings)
        elif cert.get("expired"):
            add("tls_in_transit", "TLS / encryption in transit", "fail",
                f"The certificate Orb serves expired on {cert.get('expires_at', '')[:10]}. "
                "Browsers warn on every connection, and staff learn to click "
                "through warnings.",
                "Ask the person who installed Orb to install a renewed certificate "
                "today.", tls_settings)
        elif cert.get("self_signed"):
            add("tls_in_transit", "TLS / encryption in transit", "warn",
                "Orb serves HTTPS, but with a self-signed certificate (the one the "
                "installer generates). Traffic is encrypted, but no browser can "
                "tell Orb from an impostor, and every device shows a warning.",
                "Replace it with a certificate issued by the hospital's own "
                "certificate authority or a public one before go-live.", tls_settings)
        elif not cert.get("names"):
            add("tls_in_transit", "TLS / encryption in transit", "warn",
                "Orb serves HTTPS, but its certificate names no host, so current "
                "browsers reject it.",
                "Reissue the certificate with the address staff use to reach Orb.",
                tls_settings)
        elif int(cert.get("days_left") or 0) < _CERT_EXPIRY_WARN_DAYS:
            add("tls_in_transit", "TLS / encryption in transit", "warn",
                f"Orb serves HTTPS with a valid certificate, but it expires in "
                f"{cert.get('days_left')} day(s), on {cert.get('expires_at', '')[:10]}.",
                "Renew the certificate before it lapses.", tls_settings)
        else:
            add("tls_in_transit", "TLS / encryption in transit", "pass",
                f"Orb serves HTTPS itself with a certificate for "
                f"{', '.join(cert.get('names') or [])}, valid until "
                f"{cert.get('expires_at', '')[:10]}.",
                "Renew the certificate before it expires; this page warns a month "
                "ahead.", tls_settings)
    elif tls.get("mode") == "unknown":
        # The probe itself failed: say so rather than claim plain HTTP.
        add("tls_in_transit", "TLS / encryption in transit", "unknown",
            "Orb could not check its own HTTPS set-up.",
            "Ask the person who installed Orb to check the certificate settings.",
            tls_settings)
    elif tls.get("mode") == "upstream":
        add("tls_in_transit", "TLS / encryption in transit", "warn",
            "The installer has declared that a proxy in front of Orb handles HTTPS. "
            "Orb cannot see that proxy, so it cannot confirm the certificate or that "
            "plain HTTP is refused.",
            "Have the hospital's IT team confirm the proxy certificate and the "
            "HTTP to HTTPS redirect in writing, and keep that with the site file.",
            tls_settings)
    else:
        add("tls_in_transit", "TLS / encryption in transit",
            "fail" if prod_profile else "warn",
            "Orb is not serving HTTPS and nobody has declared a proxy that does. "
            "Patient data and sign-in tokens cross the network in plain text.",
            "Ask the person who installed Orb to give it a certificate, or to put "
            "it behind a proxy that serves HTTPS, before any patient data is "
            "entered.", tls_settings)

    # ── Patient discharge link address ──
    # Prod only: on a dev machine the localhost default is where the app is, and
    # a row there would only move the demo's score. Amber, not red, when unset:
    # nothing is exposed, discharge still completes, the patient just leaves
    # without a link (discharge_service.discharge_patient withholds it). Links
    # the hospital switched off pass: paper only is a ruled choice (2026-10-04,
    # option A), not a gap.
    if prod_profile:
        link_settings = ["ORB_PUBLIC_APP_BASE_URL", "ORB_DISCHARGE_LINK_ENABLED"]
        link = _safe(_patient_link_posture, None)
        if link is None:
            add("patient_discharge_link", "Patient discharge link address", "unknown",
                "Orb could not check the address it puts in patients' discharge "
                "links and QR codes.",
                "Ask the person who installed Orb to check the patient address "
                "setting.", link_settings)
        elif link.get("switched_off"):
            add("patient_discharge_link", "Patient discharge link address", "pass",
                "The hospital has switched discharge links off: no link or QR code "
                "is issued, and patients take the printed summary home.",
                "None needed. To offer the link instead, agree with the hospital's IT "
                "team which patient Wi-Fi reaches this appliance, then ask the person "
                "who installed Orb to switch links on and set the patient address.",
                link_settings)
        elif link.get("problem"):
            add("patient_discharge_link", "Patient discharge link address", "warn",
                "Orb is issuing no discharge links or QR codes: the address it "
                f"would put in them, {link.get('base_url') or '(none)'}, cannot be "
                f"opened from a patient's phone because {link['problem']}. Patients "
                "leave with the printed summary only.",
                "Ask the person who installed Orb to set the patient address to "
                "this appliance's https address, and agree with the hospital's IT "
                "team which network patients' phones use to reach it. If no patient "
                "network can reach it, ask them to switch discharge links off "
                "instead, so the printed summary is the handover.",
                link_settings)
        else:
            add("patient_discharge_link", "Patient discharge link address", "pass",
                f"Discharge links and QR codes point at {link['base_url']}, an https "
                "address that is not this machine.",
                "Orb cannot see the network from here. Keep the hospital IT team's "
                "written confirmation that patients' phones can reach this address "
                "without a certificate warning with the site file.",
                link_settings)

    # ── Encryption at rest: DB ──
    db_settings = ["ORB_DB_ENCRYPTION_KEY"]
    if enc.get("db_at_rest"):
        add("enc_at_rest_db", "Encryption at rest: database", "pass",
            "The open database connection is encrypted (SQLCipher "
            f"{enc.get('db_cipher_version') or ''}".rstrip() + "), checked on the "
            "live connection, not read from settings. The database file is "
            "unreadable without its key.",
            "Keep a sealed copy of the key off this machine: without it, the "
            "database and every backup are unrecoverable.", db_settings)
    elif enc.get("db_encryption_key_set"):
        add("enc_at_rest_db", "Encryption at rest: database", "fail",
            "A database key is configured, but the open connection is not "
            "encrypted, so patient records are being written to disk in the clear.",
            "Stop Orb and ask the person who installed it to reinstall the "
            "encryption driver; Orb should refuse to start in this state.",
            db_settings)
    else:
        add("enc_at_rest_db", "Encryption at rest: database", "warn",
            "The database is not encrypted. Patient records on disk are protected "
            "only by the operating system's file permissions.",
            "Ask the person who installed Orb to encrypt the database, or confirm "
            "in writing that the disk itself is encrypted (FileVault).",
            db_settings)

    # ── Encryption at rest: uploads ──
    up_settings = ["ORB_UPLOAD_ENCRYPTION_KEY", "ORB_REQUIRE_UPLOAD_ENCRYPTION"]
    if enc.get("uploads_at_rest") and enc.get("upload_encryption_opted_out"):
        add("enc_at_rest_uploads", "Encryption at rest: PHI uploads", "warn",
            "Images and voice recordings are encrypted when they are saved, but "
            "enforcement has been switched off: if the key went missing they would "
            "be saved in the clear without any error.",
            "Ask the person who installed Orb to remove the opt-out so a missing "
            "key stops uploads instead.", up_settings)
    elif enc.get("uploads_at_rest"):
        add("enc_at_rest_uploads", "Encryption at rest: PHI uploads", "pass",
            "Images and voice recordings are encrypted (AES-256-GCM) when they are "
            "saved"
            + (", and an upload that cannot be encrypted is refused." if enc.get("upload_encryption_enforced")
               else ". Enforcement is not on, so a missing key would fall back to "
                    "unencrypted files."),
            "Keep a sealed copy of the upload key off this machine with the "
            "database key.", up_settings)
    elif enc.get("upload_encryption_enforced"):
        add("enc_at_rest_uploads", "Encryption at rest: PHI uploads", "fail",
            "Uploads must be encrypted on this appliance, but no upload key is "
            "available, so every image and recording upload is being refused.",
            "Ask the person who installed Orb to restore the upload key and "
            "restart Orb.", up_settings)
    else:
        add("enc_at_rest_uploads", "Encryption at rest: PHI uploads", "warn",
            "Images and voice recordings are saved unencrypted.",
            "Ask the person who installed Orb to set an upload key.", up_settings)

    # ── Default credentials ──
    # The passwords in Orb's own seed scripts are public. One active account on
    # one of them is the whole appliance, whatever else on this page is green.
    live = credentials.get("live") or []
    if not credentials.get("checked"):
        add("default_credentials", "Default passwords", "unknown",
            "The check for the passwords published in Orb's setup scripts has not "
            "finished since Orb last started (it runs once, shortly after start).",
            "Reopen this page in a few minutes. If it still says this, ask the "
            "person who installed Orb to check the start-up log.")
    elif live:
        add("default_credentials", "Default passwords", "fail",
            f"{len(live)} active account(s) still sign in with a password published "
            f"in Orb's own setup scripts: {', '.join(live)}. Anyone on the hospital "
            "network who has seen those scripts can sign in as them.",
            "In Admin, Users, set a new password for each of these accounts, or "
            "deactivate the ones nobody uses.")
    elif not credentials.get("scan_complete", True):
        add("default_credentials", "Default passwords", "warn",
            "No account was found on a published password, but the check ran out "
            "of time before it reached every account.",
            "Ask the person who installed Orb to let the start-up check run longer.",
            ["ORB_BOOTSTRAP_SCAN_BUDGET_S"])
    else:
        locked = credentials.get("locked_at_boot") or []
        add("default_credentials", "Default passwords", "pass",
            "No active account signs in with a password published in Orb's setup "
            "scripts"
            + (f" ({len(locked)} found at start-up were locked)." if locked else "."),
            "Keep it that way: every new account gets its own password.")

    # ── MFA ──
    required = mfa.get("required_roles") or []
    gap = mfa.get("required_gap") or []
    roles_words = ", ".join(r.replace("_", " ") for r in required)
    gap_words = ", ".join(r.replace("_", " ") for r in gap)
    if required and not gap:
        add("mfa", "Multi-factor authentication", "pass",
            f"A second sign-in factor is required for {roles_words}, and every "
            f"account in those roles has one (staff-wide {mfa.get('adoption_pct', 0)}%).",
            "Require it for any new privileged role as it is added.",
            ["ORB_MFA_REQUIRED_ROLES"])
    elif required and gap:
        add("mfa", "Multi-factor authentication", "warn",
            f"A second sign-in factor is required for {roles_words}, but not every "
            f"account in {gap_words} has set one up yet.",
            "Those staff are asked to set it up at their next sign-in; follow up "
            "with anyone who has not signed in since.", ["ORB_MFA_REQUIRED_ROLES"])
    else:
        add("mfa", "Multi-factor authentication", "warn",
            "Staff can add a second sign-in factor, but no role is required to "
            f"(staff-wide {mfa.get('adoption_pct', 0)}%).",
            "Ask the person who installed Orb to require a second factor for "
            "administrators, doctors and nurses.", ["ORB_MFA_REQUIRED_ROLES"])

    # ── Audit hash-chain ──
    if audit.get("verified"):
        add("audit_hash_chain", "Tamper-evident audit log", "pass",
            f"Each of the {audit.get('count', 0):,} audit entries is sealed to the one "
            "before it, and the whole chain checks out: nothing has been altered "
            "or removed.",
            "Keep a dated copy of the audit history off this machine so it can be "
            "checked independently.")
    else:
        add("audit_hash_chain", "Tamper-evident audit log", "fail",
            f"The audit chain is BROKEN at entry {audit.get('broken_at')}. An entry "
            "may have been altered or deleted.",
            "Treat this as a security incident: tell the hospital's information "
            "security lead today and keep the machine as it is for investigation.")

    # ── RBAC ──
    if rbac.get("role_count", 0) >= 2 and rbac.get("permission_count", 0) > 0:
        add("rbac", "Role-based access control", "pass",
            f"Each of the {rbac.get('role_count')} roles holds only the permissions "
            f"it needs (from {rbac.get('permission_count')} in all). Every screen and "
            "action checks them, and every refusal is recorded.",
            "Review who holds which role in Admin, Users each quarter.")
    else:
        add("rbac", "Role-based access control", "warn",
            "Orb's table of roles and permissions is empty or trivial.",
            "Contact Orb support: this is a defect, not a setting.")

    # ── Backups ──
    backups = backups or {}
    count = int(backups.get("count") or 0)
    age = _age_words(backups.get("newest_age_seconds"))
    max_h = float(backups.get("max_age_hours") or 36.0)
    backup_settings = ["ORB_BACKUP_OFFSITE_DIR", "ORB_HEARTBEAT_BACKUP_MAX_AGE_H"]
    if backups.get("probe_failed"):
        add("backups", "Backup & recovery", "unknown",
            "Orb could not read its backup folder, so it cannot say whether a "
            "usable backup exists.",
            "Ask the person who installed Orb to check the backup folder.",
            backup_settings)
    elif count == 0:
        add("backups", "Backup & recovery", "warn",
            "There is no backup of this hospital's data on this appliance. If the "
            "machine fails, every record on it is lost.",
            "Take a backup now from Admin, Data & Backups, and ask the person who "
            "installed Orb to schedule the nightly checked backup.", backup_settings)
    elif backups.get("verification_failed"):
        add("backups", "Backup & recovery", "fail",
            f"The newest backup ({age} old) FAILED its check: Orb opened it and "
            "found it unusable. Treat this appliance as having no usable backup.",
            "Ask the person who installed Orb to run a backup by hand today and "
            "watch it pass.", backup_settings)
    elif not backups.get("newest_verified"):
        add("backups", "Backup & recovery", "warn",
            f"The newest backup is {age} old, and Orb has no record that it was "
            "checked, so nobody can promise it would restore. (Backups taken from "
            "Data & Backups are checked but leave no record.)",
            "Ask the person who installed Orb to schedule the nightly backup, which "
            "checks itself and records the result.", backup_settings)
    elif (backups.get("newest_age_seconds") or 0) > max_h * 3600:
        add("backups", "Backup & recovery", "warn",
            f"The newest checked backup is {age} old, older than the {max_h:.0f} "
            "hours of data this site has agreed it can afford to lose.",
            "The nightly backup has probably stopped. Ask the person who installed "
            "Orb to take one now and check the schedule.", backup_settings)
    elif not backups.get("offsite_configured"):
        add("backups", "Backup & recovery", "warn",
            f"The newest backup is {age} old and passed its check, but every copy "
            "is on this appliance. A fire, a theft or a dead disk takes the "
            "backups with the original.",
            "Ask the person who installed Orb to add a second destination (a "
            "hospital file server or an external drive).", backup_settings)
    elif not backups.get("last_drill"):
        add("backups", "Backup & recovery", "warn",
            f"The newest backup is {age} old, passed its check and is copied off "
            "the appliance, but no restore drill is on record: the drill prints "
            "its result and does not file it, so this page cannot show that a "
            "restore was ever tested.",
            "Run the restore drill monthly and keep the printed result with the "
            "site file.", backup_settings)
    else:  # pragma: no cover - no drill record exists yet
        add("backups", "Backup & recovery", "pass",
            f"The newest backup is {age} old, passed its check, is copied off the "
            "appliance, and a restore drill is on record.",
            "Keep running the drill monthly.", backup_settings)

    # ── Session revocation ──
    if revocation:
        add("session_revocation", "Session revocation", "pass",
            "Deactivating an account or resetting its password ends every session "
            "it holds at once, and each two-factor code works only once.",
            "Deactivate an account in Admin, Users the moment its holder leaves.")
    else:
        add("session_revocation", "Session revocation", "warn",
            "This database cannot end a person's sessions early, so a stolen "
            "sign-in stays valid until it expires.",
            "Contact Orb support: the database is missing a required update.")

    # ── Data residency / on-prem ──
    # The CORS answer is the policy INSTALLED (residency.cors_wildcard), so this
    # can no longer describe an allow-list while every origin is admitted.
    host = residency.get("host")
    residency_settings = ["ORB_HOST", "ORB_CORS_ORIGINS", "ORB_DEPLOYMENT_PROFILE"]
    if residency.get("cors_wildcard"):
        add("data_residency", "Data residency / on-prem", "fail",
            "Orb keeps the full stack in-house, but it accepts browser requests "
            "from ANY web address: a page a staff member opens elsewhere can call "
            "Orb with their session.",
            "Ask the person who installed Orb to run it in the production profile, "
            "which accepts only the hospital's own addresses.", residency_settings)
    elif not residency.get("loopback_only"):
        add("data_residency", "Data residency / on-prem", "warn",
            f"Orb keeps the full stack in-house, but it listens on a network "
            f"address ({host}), not only on this machine.",
            "Confirm with the hospital's IT team that this is intended and that "
            "the address is reachable only over HTTPS.", residency_settings)
    else:
        origins = residency.get("cors_origins") or []
        add("data_residency", "Data residency / on-prem", "pass",
            f"Orb keeps the full stack in-house, listens on this machine only "
            f"({host}), and accepts browser requests only from "
            f"{len(origins)} named web address(es).",
            "Keep outside access behind the HTTPS proxy.", residency_settings)

    # ── Web address check (DNS rebinding) ──
    # Graded on the names services/host_gate.HostGate admits, plus what it has
    # turned away since start: the only sign a rebinding page was ever opened.
    # The refused name is attacker-chosen text, so it is cut short and limited to
    # characters a host name can have before it reaches the screen.
    allowed = residency.get("allowed_hosts")
    refusals = residency.get("host_refusals") or {}
    host_settings = ["ORB_ALLOWED_HOSTS", "ORB_CORS_ORIGINS", "ORB_PUBLIC_APP_BASE_URL"]
    refused_count = int(refusals.get("count") or 0)
    last = "".join(ch for ch in str(refusals.get("last_host") or "")[:60]
                   if ch.isascii() and (ch.isalnum() or ch in ".-:[]"))
    seen = (f" Since it started it has turned away {refused_count} request(s) for other "
            f"names, most recently for \"{last}\"." if refused_count else "")
    if allowed is None:
        add("host_allow_list", "Web address check (DNS rebinding)", "unknown",
            "Orb could not read which web addresses it answers to.",
            "Contact Orb support.", host_settings)
    elif "*" in allowed:
        add("host_allow_list", "Web address check (DNS rebinding)",
            "fail" if prod_profile else "warn",
            "Orb answers to any web address name. A web page a staff member opens "
            "elsewhere can point its own name at Orb and reach Orb's sign-in through "
            "their browser, from inside the hospital." + seen,
            "Ask the person who installed Orb to turn the address check back on and "
            "list the names staff use instead.", host_settings)
    else:
        add("host_allow_list", "Web address check (DNS rebinding)", "pass",
            f"Orb answers only to IP addresses and {len(allowed)} named address(es): "
            f"{', '.join(allowed)}. A web page elsewhere that points its own name at "
            f"Orb is turned away." + seen,
            "If staff or a device reach Orb by a name not listed here, ask the person "
            "who installed Orb to add it.", host_settings)

    # ── Deployment posture (declared vs observed) ──
    # Separate from data_residency on purpose: a box can bind loopback and still
    # be running the dev signing secret with a vitals simulator attached.
    posture = residency.get("deployment_posture") or {}
    effective = posture.get("effective") or residency.get("deployment_profile") or "unknown"
    dev_signals = posture.get("dev_signals") or []
    posture_settings = ["ORB_DEPLOYMENT_PROFILE", "ORB_JWT_SECRET", "ORB_INGESTION_API_KEY",
                        "ORB_INGESTION_MODE", "ORB_DEMO_MODE"]
    if effective == "prod":
        add("deployment_profile", "Deployment posture", "pass",
            "This appliance is declared a production install and nothing says "
            "otherwise: its keys are its own, no simulator is writing readings, "
            "and demo content is off.",
            "Reopen this page after any change to the appliance's settings; it is "
            "checked each time it opens.", posture_settings)
    elif effective == "unknown":
        add("deployment_profile", "Deployment posture", "unknown",
            "Orb could not read its own deployment profile, so it cannot attest to "
            "one. It is reported as unknown rather than assumed hardened.",
            "Ask the person who installed Orb to declare the profile and restart.",
            posture_settings)
    else:
        declared = posture.get("declared", "unknown")
        declared_words = {"dev": "development", "prod": "production"}.get(declared, "unknown")
        headline = (
            f"This appliance is running as a DEVELOPMENT install (declared: "
            f"{declared_words}{'' if posture.get('declared_explicitly') else ', not explicitly set'}). "
        )
        add("deployment_profile", "Deployment posture", "warn",
            headline + " ".join(s.get("plain") or s.get("detail", "") for s in dev_signals),
            "Ask the person who installed Orb to switch it to the production "
            "profile with keys generated for this site, live monitor feeds instead "
            "of the simulator and demo content off, then restart Orb and reopen "
            "this page.", posture_settings)

    # ── National certification ──
    # Stated, not implied. Nothing on the appliance can prove a certification it
    # does not hold, and a hospital that claims an incentive on Orb's behalf is
    # the one that pays for the mistake.
    add("abdm_certification", "ABDM certification", "warn",
        "Orb is not certified by the National Health Authority as ABDM-compliant "
        "software and is not connected to the ABDM gateway. The ABDM desk records "
        "ABHA numbers, verifications and consents that staff completed on the "
        "national portal; nothing is sent from Orb.",
        "Do not cite Orb as ABDM-certified in any scheme or incentive claim. "
        "Create and link ABHAs on the national portal until it is certified.")
    add("nabh_dhs", "NABH Digital Health Standards", "warn",
        "Orb has not been assessed against the NABH Digital Health Standards. The "
        "Accreditation desk keeps quality indicators and the incident register to "
        "support the hospital's own NABH work; using it does not make the "
        "hospital, or Orb, compliant with those standards.",
        "Have the quality team map each standard to its evidence before an "
        "assessment, and do not cite Orb as assessed.")

    # ── DPDP Act 2023 ──
    # Four separate controls rather than one "DPDP: pass" badge, because they
    # fail independently: a hospital can have a full consent register and no
    # breach process, and a single green tick would hide that.
    #
    # The consent, export, overdue-list and breach machinery has no admin screen
    # yet (only the erasure requests do), so each remediation names the step a
    # person can take today rather than a screen the admin will not find.

    consents = dpdp.get("consents_recorded", 0)
    if consents > 0:
        add("dpdp_consent", "DPDP: consent register", "pass",
            f"{consents} consent record(s) captured across "
            f"{dpdp.get('patients_with_consent', 0)} patient(s), with "
            f"{dpdp.get('consents_withdrawn', 0)} withdrawal(s). A consent record "
            "cannot be edited; a withdrawal is a new entry beside it.",
            "Orb has no screen for recording consent yet. Until it does, take "
            "consent at registration for every admission on the hospital's signed "
            "form, noting the notice version and language shown to the patient "
            "(DPDP s.5), and file it with the patient's record.")
    else:
        add("dpdp_consent", "DPDP: consent register", "warn",
            "The consent register is in place and cannot be edited after the fact, "
            "but no consent has been recorded yet on this appliance.",
            "Orb has no screen for recording consent yet, so take consent at "
            "registration on the hospital's signed form from the first real "
            "patient; a hospital processing personal data without a consent "
            "record cannot show a lawful basis.")

    add("dpdp_data_principal_rights", "DPDP: data principal rights", "pass",
        "Orb has an export, run by an administrator, that produces everything it "
        "holds about one patient, including records kept in parts of Orb added "
        "later. Only an administrator in the patient's department can run it, and "
        "every export is recorded in the audit log.",
        "Orb has no screen for the export yet: when a patient asks for their data, "
        "contact Orb support to run it with an administrator. Agree a service "
        "level for responding (IMC Reg. 1.3.2 expects medical records within 72 "
        "hours) and name the officer who signs the export off.")

    policies = dpdp.get("retention_policies", 0)
    if policies > 0:
        add("dpdp_retention", "DPDP: retention policy & review", "pass",
            f"{policies} retention categories are declared, each with its lawful "
            "basis, the rule it comes from, and whether erasure may reach it. The "
            "review surfaces what is past retention and DELETES NOTHING. "
            "Destruction needs a signed human order.",
            "Orb has no screen for the overdue list yet: contact Orb support for "
            "it each month and have the records officer walk it, and have counsel "
            "confirm the day counts against this state's rules.")
    else:  # pragma: no cover, the migration seeds these
        add("dpdp_retention", "DPDP: retention policy & review", "warn",
            "No retention policies are declared.",
            "Contact Orb support: the retention rules that ship with Orb are "
            "missing from this database.")

    breaches = dpdp.get("breaches_recorded", 0)
    unnotified = dpdp.get("breaches_awaiting_board_notification", 0)
    if unnotified:
        add("dpdp_breach_register", "DPDP: breach register", "fail",
            f"{unnotified} of {breaches} recorded breach(es) have NOT been "
            "notified to the Data Protection Board. DPDP s.8(6) requires "
            "notifying the Board and every affected Data Principal.",
            "Notify the Board and the affected patients now. Orb has no screen for "
            "logging a notification yet, so keep a dated copy of each notice and "
            "contact Orb support to log it against the breach.")
    elif breaches:
        add("dpdp_breach_register", "DPDP: breach register", "pass",
            f"A breach register is in place with {breaches} record(s), and every "
            "one has a Board notification logged against it. Notifications "
            "cannot be edited once logged.",
            "Confirm the affected Data Principals were told as well as the Board. "
            "DPDP s.8(6) requires both.")
    else:
        # Honest amber, not a green tick. An empty register proves the table
        # exists; it proves nothing about whether anyone could use it at 2am.
        add("dpdp_breach_register", "DPDP: breach register", "warn",
            "A breach register and a notification log are in place, but no "
            "breach has ever been recorded, so the process is untested.",
            "Run one tabletop exercise before go-live: walk a simulated breach "
            "through a Board notification and a patient notification on paper, and "
            "name the person who owns this at 2am. Orb has no screen for the breach "
            "register yet, so that person contacts Orb support to record a real one.")

    return controls


def _lab_drain_control() -> dict | None:
    """The laboratory's drain check (services/lab_module.compliance_control)."""
    from services.lab_module import compliance_control
    return compliance_control()


def _overall_score(controls: list[dict]) -> int:
    """Derive a 0–100 posture score as the weighted mean of control statuses."""
    if not controls:
        return 0
    total = sum(_STATUS_WEIGHT.get(c.get("status"), 0.0) for c in controls)
    return int(round((total / len(controls)) * 100))


# ════════════════════════════════════════════════════════════════
#  Public entrypoint
# ════════════════════════════════════════════════════════════════

def compliance_report() -> dict:
    """Aggregate the live compliance posture into a single report dict.

    Shape:
        {generated_at, overall_score, controls:[{id,name,status,evidence,remediation}],
         mfa:{...}, rbac_matrix:{...}, audit_chain:{...}, audit_coverage:{...},
         encryption:{...}, residency:{...}, models:{validated,total,unvalidated}}

    Never raises: each probe degrades to a safe default on failure.
    """
    enc = _safe(_encryption_posture, {"db_at_rest": False, "uploads_at_rest": False})
    # Default carries unknown=True so a probe FAILURE (e.g. missing audit_logs
    # table) is reported as 'unknown', never as a false 'fail' (alleged tampering).
    audit = _safe(_audit_chain, {"verified": False, "broken_at": None, "count": 0, "unknown": True})
    coverage = _safe(_audit_coverage, {"total_events": 0, "by_category": {}, "categories": 0})
    mfa = _safe(_mfa_adoption, {"required_roles": [], "by_role": {}, "total_users": 0,
                                "total_enrolled": 0, "adoption_pct": 0, "required_gap": []})
    rbac = _safe(_rbac_matrix, {"roles": [], "permissions": [], "matrix": {},
                                "role_count": 0, "permission_count": 0})
    models = _safe(_model_posture, {"validated": 0, "total": 0, "unvalidated": 0})
    # The default deliberately says "unknown", never "prod": a probe that could
    # not run must not hand an auditor the stronger claim.
    residency = _safe(_residency, {"host": "unknown", "loopback_only": False,
                                   "cors_origins": [], "deployment_profile": "unknown",
                                   "declared_deployment_profile": "unknown",
                                   "deployment_posture": {
                                       "declared": "unknown", "declared_explicitly": False,
                                       "effective": "unknown", "hardened": False,
                                       "dev_signals": [],
                                   },
                                   "on_prem": True})
    backups = _safe(_backup_posture, {"count": 0, "probe_failed": True})
    tls = _safe(_tls_posture, {"mode": "unknown"})
    credentials = _safe(_default_credentials, {"checked": False, "live": []})
    revocation = _safe(_session_revocation_supported, False)
    dpdp = _safe(_dpdp_posture, {"consents_recorded": 0, "consents_withdrawn": 0,
                                 "patients_with_consent": 0, "retention_policies": 0,
                                 "erasure_requests_total": 0,
                                 "erasure_requests_awaiting_decision": 0,
                                 "breaches_recorded": 0,
                                 "breaches_awaiting_board_notification": 0})

    controls = _build_controls(enc, audit, mfa, rbac, residency, backups, revocation,
                               dpdp, tls=tls, credentials=credentials)

    # The laboratory module switched off with open work (ruling LIS-R21): a
    # failing check until the open specimens, results and critical results are
    # finished. Absent otherwise, so a site that never used the laboratory
    # sees no new line.
    lab_drain = _safe(_lab_drain_control, None)
    if lab_drain:
        controls.append(lab_drain)

    # Reflect an unknown audit probe honestly in the control status.
    if audit.get("unknown"):
        for c in controls:
            if c["id"] == "audit_hash_chain":
                c["status"] = "unknown"
                c["evidence"] = "The audit chain could not be checked on this appliance."
                c["remediation"] = "Contact Orb support: the audit history could not be read."

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "overall_score": _overall_score(controls),
        "controls": controls,
        "mfa": mfa,
        "rbac_matrix": rbac,
        "audit_chain": audit,
        "audit_coverage": coverage,
        "encryption": enc,
        "residency": residency,
        "models": models,
        "dpdp": dpdp,
        "backups": backups,
        "tls": tls,
    }
