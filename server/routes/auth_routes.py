"""Orb — Auth Routes"""
import hashlib
import re
import uuid
import time
import logging
from collections import defaultdict
from fastapi import APIRouter, HTTPException, Request, Response
from models.user import (
    UserLogin, UserOut, TokenResponse, PinLogin, DeviceEnrollBody,
    DeviceBindBody, DevicePinBody, AdminDevicePinBody,
)
from auth import (
    hash_password, verify_password, verify_password_ct, verify_device_pin_ct,
    create_token, generate_csrf_token, set_session_cookies, clear_session_cookies,
    generate_device_secret, hash_device_secret, extract_device_token,
    set_device_cookie, clear_device_cookie, load_active_device,
    legacy_sha256_allowed, count_legacy_password_hashes,
    DEVICE_COOKIE, DEVICE_PIN_SESSION_MINUTES,
)
from database import get_db, run_db
from services.audit_service import log_audit_event

logger = logging.getLogger("orb.auth")

router = APIRouter(prefix="/api/auth", tags=["auth"])


def _log_legacy_hash_posture() -> None:
    """Startup posture line: how many accounts are still on the unsalted-SHA-256
    hash tier (login rejects them unless ORB_ALLOW_LEGACY_SHA256 is set). A
    COUNT only — usernames never reach the log.

    Runs at module import, which IS process startup for this router (main.py
    imports it before the app exists); main.py's lifespan is owned by other
    work, so the auth cluster reports its own posture here. Fail-soft: on a
    first-ever boot the users table does not exist yet until lifespan's
    init_db(), and an empty database has no legacy accounts to count anyway.
    """
    try:
        n = count_legacy_password_hashes()
    except Exception:
        return
    if n:
        logger.warning(
            "%d account(s) still hold legacy unsalted-SHA-256 password hashes and "
            "cannot log in until an administrator resets their passwords "
            "(temporary escape hatch: ORB_ALLOW_LEGACY_SHA256=1, which also lets "
            "a successful login upgrade the hash to bcrypt in place).", n,
        )
    else:
        logger.info("Password hash posture: no legacy SHA-256 hashes remain.")


_log_legacy_hash_posture()

# In-memory sliding-window rate limiter: IP -> [timestamps].
#
# DEFERRED (cross-file): this state is per-process and lost on restart, keys on
# the raw TCP peer IP (so behind a reverse proxy one shared IP either does
# nothing or locks out an entire ward), and is purely per-IP — credential
# stuffing spread across many IPs against one account is unthrottled. The
# recommended fix is a persistent `login_attempts` SQLite table (mirroring
# discharge_access_tokens.failed_attempts/locked_until) plus a per-account
# failure counter, and resolving the client IP from a trusted proxy header only
# when behind a known proxy. That requires a schema change (database.py) and is
# recorded as deferred. The server currently runs single-process (uvicorn
# workers=1), so the in-memory limiter is functional for the present deploy.
LOGIN_ATTEMPTS = defaultdict(list)
MAX_LOGIN_ATTEMPTS = 5
LOCKOUT_DURATION_SECONDS = 900  # 15 minutes
# Hard cap on tracked IPs to bound memory against a spray of unique source IPs.
_MAX_TRACKED_IPS = 10000


def _prune_login_attempts(now: float) -> None:
    """Drop IP buckets whose attempts have all aged out of the window.

    Bounds memory growth (finding: unbounded dict of every attacking IP).
    """
    stale = [ip for ip, ts in LOGIN_ATTEMPTS.items()
             if not ts or now - ts[-1] >= LOCKOUT_DURATION_SECONDS]
    for ip in stale:
        del LOGIN_ATTEMPTS[ip]
    # If still oversized (active spray), evict the oldest-touched buckets.
    if len(LOGIN_ATTEMPTS) > _MAX_TRACKED_IPS:
        for ip in sorted(LOGIN_ATTEMPTS, key=lambda k: LOGIN_ATTEMPTS[k][-1])[:len(LOGIN_ATTEMPTS) - _MAX_TRACKED_IPS]:
            del LOGIN_ATTEMPTS[ip]


def is_ip_rate_limited(ip: str) -> bool:
    if not ip:
        return False
    from config import DEPLOYMENT_PROFILE
    if DEPLOYMENT_PROFILE == "dev" and ip in ("127.0.0.1", "::1", "localhost"):
        return False
    now = time.time()
    _prune_login_attempts(now)
    # Filter out attempts older than lockout duration
    LOGIN_ATTEMPTS[ip] = [t for t in LOGIN_ATTEMPTS[ip] if now - t < LOCKOUT_DURATION_SECONDS]
    return len(LOGIN_ATTEMPTS[ip]) >= MAX_LOGIN_ATTEMPTS


def record_login_attempt(ip: str):
    if ip:
        LOGIN_ATTEMPTS[ip].append(time.time())


# ── Per-account lockout (persistent) ─────────────────────────────────────────
# Backed by users.failed_login_attempts + users.login_locked_until (additive
# migration). Unlike the per-IP limiter this survives restarts and can't be
# bypassed by rotating source IPs against one account.
ACCOUNT_MAX_FAILURES = 8
ACCOUNT_LOCK_SECONDS = 900  # 15 minutes


def _account_is_locked(user_row) -> bool:
    keys = user_row.keys()
    locked_until = user_row["login_locked_until"] if "login_locked_until" in keys else None
    if not locked_until:
        return False
    try:
        return time.time() < float(locked_until)
    except (TypeError, ValueError):
        return False


def _record_account_failure(user_id: str) -> None:
    """Increment the account's failure counter; lock it once the threshold is hit."""
    try:
        db = get_db()
        try:
            row = db.execute(
                "SELECT failed_login_attempts FROM users WHERE id = ?", (user_id,)
            ).fetchone()
            fails = (row["failed_login_attempts"] or 0) + 1 if row else 1
            if fails >= ACCOUNT_MAX_FAILURES:
                db.execute(
                    "UPDATE users SET failed_login_attempts = ?, login_locked_until = ? WHERE id = ?",
                    (0, time.time() + ACCOUNT_LOCK_SECONDS, user_id),
                )
            else:
                db.execute(
                    "UPDATE users SET failed_login_attempts = ? WHERE id = ?",
                    (fails, user_id),
                )
            db.commit()
        finally:
            db.close()
    except Exception as e:  # pragma: no cover - lockout must never 500 a login
        logger.warning("Failed to record account failure for %s: %s", user_id, e)


def _clear_account_failures(user_id: str) -> None:
    try:
        db = get_db()
        try:
            db.execute(
                "UPDATE users SET failed_login_attempts = 0, login_locked_until = NULL WHERE id = ?",
                (user_id,),
            )
            db.commit()
        finally:
            db.close()
    except Exception as e:  # pragma: no cover
        logger.warning("Failed to clear account failures for %s: %s", user_id, e)


# ── PIN-on-device throttling ─────────────────────────────────────────────────
# A 4-6 digit PIN is trivially guessable, so the online path has to be the only
# thing standing between an attacker and 10^4 candidates. TWO counters, because
# they catch two different attacks and neither sees the other's:
#
#   * PER ACCOUNT (users.device_pin_*) — someone repeatedly guessing ONE nurse's
#     PIN. 5 failures then a 15-minute lock.
#
#   * PER DEVICE (enrolled_devices.failed_pin_attempts) — the SWEEP. Five guesses
#     each across forty staff is two hundred guesses on one tablet, and no single
#     account ever gets close to its own limit. Without this the per-account
#     control is close to decorative on a shared device.
#
# Both are SHORT and self-healing, and both are separate from the password
# lockout on purpose (see migration 12): a PIN lock must degrade to "use your
# password", never to "you are out of the EHR mid-shift".
DEVICE_PIN_MAX_FAILURES = 5
DEVICE_PIN_LOCK_SECONDS = 900          # 15 minutes
DEVICE_SWEEP_MAX_FAILURES = 15
DEVICE_SWEEP_LOCK_SECONDS = 900        # 15 minutes
# Format rule for a tablet PIN. 4-8 digits, and a few catastrophically weak
# sequences are refused outright — '1234' on a shared ward tablet is not a
# credential. This is a floor, not a policy engine.
_PIN_FORMAT = re.compile(r"^\d{4,8}$", re.ASCII)
_BANNED_PINS = {
    "0000", "1111", "2222", "3333", "4444", "5555", "6666", "7777", "8888",
    "9999", "1234", "4321", "0123", "1212", "123456", "654321", "111111",
    "000000", "12345678",
}


def validate_pin_format(pin: str) -> None:
    """Raise 400 unless `pin` is an acceptable tablet PIN."""
    candidate = (pin or "").strip()
    if not _PIN_FORMAT.fullmatch(candidate):
        raise HTTPException(status_code=400, detail="PIN must be 4-8 digits.")
    if candidate in _BANNED_PINS or len(set(candidate)) == 1:
        raise HTTPException(
            status_code=400,
            detail="That PIN is too easy to guess. Choose a different one.",
        )


def _locked_until(row, column: str) -> bool:
    """Is `row[column]` a lock expiry still in the future?"""
    if row is None:
        return False
    try:
        keys = row.keys()
    except AttributeError:
        keys = row
    if column not in keys:
        return False
    value = row[column]
    if not value:
        return False
    try:
        return time.time() < float(value)
    except (TypeError, ValueError):
        return False


def _record_pin_failure(user_id: str, device_id: str | None) -> None:
    """Bump BOTH counters for one failed PIN attempt, locking either that crosses
    its threshold. Never raises — a throttling bookkeeping error must not turn a
    failed sign-in into a 500 on the ward's only tablet."""
    try:
        db = get_db()
        try:
            if user_id:
                row = db.execute(
                    "SELECT device_pin_failed_attempts FROM users WHERE id = ?", (user_id,)
                ).fetchone()
                if row is not None:
                    fails = (row["device_pin_failed_attempts"] or 0) + 1
                    if fails >= DEVICE_PIN_MAX_FAILURES:
                        db.execute(
                            "UPDATE users SET device_pin_failed_attempts = 0, "
                            "device_pin_locked_until = ? WHERE id = ?",
                            (time.time() + DEVICE_PIN_LOCK_SECONDS, user_id),
                        )
                    else:
                        db.execute(
                            "UPDATE users SET device_pin_failed_attempts = ? WHERE id = ?",
                            (fails, user_id),
                        )
            if device_id:
                drow = db.execute(
                    "SELECT failed_pin_attempts FROM enrolled_devices WHERE id = ?",
                    (device_id,),
                ).fetchone()
                if drow is not None:
                    dfails = (drow["failed_pin_attempts"] or 0) + 1
                    if dfails >= DEVICE_SWEEP_MAX_FAILURES:
                        db.execute(
                            "UPDATE enrolled_devices SET failed_pin_attempts = 0, "
                            "pin_locked_until = ? WHERE id = ?",
                            (time.time() + DEVICE_SWEEP_LOCK_SECONDS, device_id),
                        )
                    else:
                        db.execute(
                            "UPDATE enrolled_devices SET failed_pin_attempts = ? WHERE id = ?",
                            (dfails, device_id),
                        )
            db.commit()
        finally:
            db.close()
    except Exception as e:  # pragma: no cover — throttling must never 500 a login
        logger.warning("Failed to record PIN failure for %s: %s", user_id, e)


def _clear_pin_failures(user_id: str, device_id: str | None) -> None:
    """A single CORRECT PIN resets both counters — so the ordinary fat-finger
    never creeps toward a lock across a twelve-hour shift."""
    try:
        db = get_db()
        try:
            if user_id:
                db.execute(
                    "UPDATE users SET device_pin_failed_attempts = 0, "
                    "device_pin_locked_until = NULL WHERE id = ?",
                    (user_id,),
                )
            if device_id:
                db.execute(
                    "UPDATE enrolled_devices SET failed_pin_attempts = 0, "
                    "pin_locked_until = NULL, last_seen_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (device_id,),
                )
            db.commit()
        finally:
            db.close()
    except Exception as e:  # pragma: no cover
        logger.warning("Failed to clear PIN failures for %s: %s", user_id, e)


def consume_totp(user_id: str, secret: str, code: str) -> bool:
    """Verify a TOTP code AND enforce single-use (anti-replay).

    Returns True only if the code is valid AND its time-step is strictly greater
    than the last step already consumed by this user (persisted in
    users.totp_last_step). On success the consumed step is recorded so the same
    code — or any earlier-step code still inside the skew window — cannot be
    replayed to mint a second session. RFC 6238 §5.2.
    """
    from services import mfa_service
    from database import get_db

    step = mfa_service.verify_code_step(secret, code)
    if step is None:
        return False
    db = get_db()
    try:
        row = db.execute("SELECT totp_last_step FROM users WHERE id = ?", (user_id,)).fetchone()
        last = row["totp_last_step"] if row and row["totp_last_step"] is not None else -1
        if step <= last:
            return False  # replay: this step was already consumed
        db.execute("UPDATE users SET totp_last_step = ? WHERE id = ?", (step, user_id))
        db.commit()
    finally:
        db.close()
    return True


# ── MFA enrolment tickets ─────────────────────────────────────────────────────
# A role listed in ORB_MFA_REQUIRED_ROLES is refused a session until it holds a
# TOTP factor — but both enrolment endpoints sit behind a full session, and no
# admin endpoint provisions TOTP for another user. Without a bridge, turning the
# setting on is an unrecoverable lockout for every mandated account at once
# (including the admins needed to turn it back off), fixable only by editing the
# DB or restarting without the env var.
#
# So a correct password on a mandated-but-unenrolled account mints a short-lived
# ENROLMENT TICKET instead of a session. It is signed with a key DERIVED from
# JWT_SECRET, never JWT_SECRET itself, so auth.decode_token rejects its signature
# outright — the ticket cannot be replayed as a session token on any other route.
# Only _enrolment_principal accepts it, and only on the two enrol endpoints.
MFA_ENROLL_TICKET_HEADER = "X-MFA-Enrollment-Ticket"
_MFA_ENROLL_SCOPE = "mfa_enroll"
_MFA_ENROLL_TTL_SECONDS = 600  # long enough to scan a QR code, short enough to not linger


def _mfa_enroll_key() -> str:
    """Signing key for enrolment tickets: derived from, and never equal to, the
    session-token secret."""
    from config import JWT_SECRET
    return hashlib.sha256(f"{JWT_SECRET}|{_MFA_ENROLL_SCOPE}".encode()).hexdigest()


def issue_mfa_enroll_ticket(user_id: str, role: str, token_version: int) -> str:
    import jwt
    from datetime import datetime, timedelta, timezone
    from config import JWT_ALGORITHM

    now = datetime.now(timezone.utc)
    payload = {
        "sub": user_id,
        "role": role,
        "tv": int(token_version),
        "scope": _MFA_ENROLL_SCOPE,
        "jti": str(uuid.uuid4()),
        "iat": now,
        "exp": now + timedelta(seconds=_MFA_ENROLL_TTL_SECONDS),
    }
    return jwt.encode(payload, _mfa_enroll_key(), algorithm=JWT_ALGORITHM)


def _decode_mfa_enroll_ticket(raw: str) -> dict | None:
    """Return the ticket payload, or None if it is not a valid, unexpired,
    correctly-scoped enrolment ticket."""
    import jwt
    from config import JWT_ALGORITHM

    try:
        payload = jwt.decode(raw, _mfa_enroll_key(), algorithms=[JWT_ALGORITHM])
    except jwt.PyJWTError:
        return None
    if payload.get("scope") != _MFA_ENROLL_SCOPE or not payload.get("sub"):
        return None
    return payload


@router.post("/login", response_model=TokenResponse)
def login(credentials: UserLogin, request: Request, response: Response):
    """Authenticate user and return JWT token.

    On success the JWT is ALSO set as an HttpOnly `orb_session` cookie (plus a
    JS-readable `orb_csrf` double-submit token). The token stays in the response
    body for backward compat (machine clients, tests), but browser clients should
    rely on the cookie and NOT persist the JWT — closing the localStorage
    XSS-exfiltration hole. See auth.set_session_cookies."""
    correlation_id = str(uuid.uuid4())
    client_host = request.client.host if request.client else None
    user_agent = request.headers.get("user-agent")

    # Rate limiting check
    if is_ip_rate_limited(client_host):
        log_audit_event(
            correlation_id=correlation_id,
            event_category="authentication",
            action_type="login_rate_limited",
            outcome="failure",
            summary_message="Too many login attempts. IP temporarily locked out.",
            request_path=request.url.path,
            http_method=request.method,
            ip_address=client_host,
            user_agent=user_agent,
            metadata={"attempted_id": credentials.employee_id}
        )
        raise HTTPException(status_code=429, detail="Too many login attempts. Please try again in 15 minutes.")

    db = get_db()

    # Look up by ID (employee ID for clinicians, patient ID for patients)
    user = db.execute(
        "SELECT * FROM users WHERE id = ?", (credentials.employee_id,)
    ).fetchone()

    db.close()

    # Per-account lockout (persistent, survives restart, proxy-independent):
    # complements the per-IP limiter to throttle credential stuffing spread
    # across many source IPs against a single account.
    if user and _account_is_locked(user):
        log_audit_event(
            correlation_id=correlation_id, event_category="authentication",
            action_type="account_locked", outcome="denied",
            summary_message="Login blocked: account temporarily locked after repeated failures.",
            actor_user_id=user["id"], request_path=request.url.path, http_method=request.method,
            ip_address=client_host, user_agent=user_agent,
        )
        raise HTTPException(status_code=429, detail="Account temporarily locked after too many failed attempts. Try again later.")

    # Constant-work verification: verify_password_ct ALWAYS runs one bcrypt op,
    # even when `user` is None, so an unknown employee_id costs the same ~200 ms
    # as a real account with a wrong password. Short-circuiting on `user` here
    # (the previous `not user or not verify_password(...)`) leaked a ~90x timing
    # gap that let an attacker enumerate the staff list off response latency
    # alone. The response body/status below are already identical for both cases;
    # this closes the remaining TIMING side of the enumeration oracle.
    password_ok = verify_password_ct(credentials.password, user["password_hash"] if user else None)
    if not user or not password_ok:
        record_login_attempt(client_host)
        if user:
            _record_account_failure(user["id"])
        log_audit_event(
            correlation_id=correlation_id,
            event_category="authentication",
            action_type="login_failure",
            outcome="failure",
            summary_message="Invalid credentials provided",
            request_path=request.url.path,
            http_method=request.method,
            ip_address=client_host,
            user_agent=user_agent,
            metadata={"attempted_id": credentials.employee_id}
        )
        raise HTTPException(status_code=401, detail="Invalid credentials")

    # Deactivated accounts cannot log in (checked after password verification so
    # the response doesn't reveal which ids are deactivated to an unauthenticated
    # caller). Deactivation elsewhere also bumps token_version, but this closes
    # the "can still re-login with valid credentials" gap.
    if "is_active" in user.keys() and user["is_active"] is not None and not user["is_active"]:
        log_audit_event(
            correlation_id=correlation_id, event_category="authentication",
            action_type="login_denied_inactive", outcome="denied",
            summary_message="Login denied: account is deactivated.",
            actor_user_id=user["id"], actor_role=user["role"],
            request_path=request.url.path, http_method=request.method,
            ip_address=client_host, user_agent=user_agent,
        )
        raise HTTPException(status_code=403, detail="Account deactivated. Contact an administrator.")

    # Bridge is for the admission only (ruling R029): a patient login is refused
    # from the end of the hospital-local day of their discharge. Checked after
    # password verification, like the gate above, so only the account's holder
    # learns why. get_current_user applies the same rule to live sessions.
    if user["role"] == "patient":
        from services.patient_portal_access import portal_access_for
        portal = portal_access_for(user["id"])
        if not portal["open"]:
            log_audit_event(
                correlation_id=correlation_id, event_category="authentication",
                action_type="login_denied_portal_closed", outcome="denied",
                summary_message=(
                    f"Patient portal sign-in refused: patient status "
                    f"'{portal['status']}', portal closed"
                    + (f" at {portal['until']}" if portal["until"] else "") + "."
                ),
                actor_user_id=user["id"], actor_role=user["role"],
                request_path=request.url.path, http_method=request.method,
                ip_address=client_host, user_agent=user_agent,
            )
            raise HTTPException(status_code=403, detail=portal["reason"])

    # Legacy-hash gate: an unsalted-SHA-256 password hash is offline-crackable at
    # GPU speed, so by default the account is refused a session until an
    # administrator resets the password. ORB_ALLOW_LEGACY_SHA256=1 (temporary,
    # see .env.example) re-opens the window — the upgrade-on-login below then
    # rotates the hash to bcrypt, which is the intended way OFF this tier.
    # Checked AFTER password verification, like the deactivated gate above, so
    # the distinct error is only ever shown to a caller who already holds the
    # correct password — an unauthenticated probe still sees the same 401 as an
    # unknown id, and the message itself names no account.
    if not user["password_hash"].startswith("$2") and not legacy_sha256_allowed():
        log_audit_event(
            correlation_id=correlation_id, event_category="authentication",
            action_type="login_denied_legacy_hash", outcome="denied",
            summary_message="Login denied: account still holds a legacy unsalted-SHA-256 "
                            "password hash. An administrator must reset the password "
                            "(temporary escape hatch: ORB_ALLOW_LEGACY_SHA256=1).",
            actor_user_id=user["id"], actor_role=user["role"],
            request_path=request.url.path, http_method=request.method,
            ip_address=client_host, user_agent=user_agent,
        )
        raise HTTPException(status_code=403, detail="Password reset required by an administrator.")

    # MFA gate: password is correct, but if this account (or its role) requires a
    # TOTP second factor, verify it before issuing a token. A correct password
    # with a missing/invalid code is a failed login (counts toward lockout).
    from config import MFA_REQUIRED_ROLES
    from services import mfa_service

    totp_enabled = bool(user["totp_enabled"]) if "totp_enabled" in user.keys() else False
    role_requires_mfa = user["role"] in MFA_REQUIRED_ROLES

    if role_requires_mfa and not totp_enabled:
        # Privileged role that has not yet enrolled — no session is issued, but
        # the password WAS verified, so hand back a scoped enrolment ticket that
        # unlocks /mfa/enroll/begin + /confirm and nothing else. Without it the
        # account would be permanently unable to reach the only endpoints that
        # can clear this gate. The detail string is unchanged — the SPA branches
        # on it — and the ticket rides a response header.
        _tv_enroll = user["token_version"] if "token_version" in user.keys() and user["token_version"] is not None else 0
        ticket = issue_mfa_enroll_ticket(user["id"], user["role"], _tv_enroll)
        log_audit_event(
            correlation_id=correlation_id,
            event_category="authentication",
            action_type="mfa_enrollment_ticket_issued",
            outcome="denied",
            summary_message="Login denied: role requires MFA and the account is not enrolled. "
                            "Issued a 10-minute enrolment ticket scoped to /api/auth/mfa/enroll/*.",
            actor_user_id=user["id"],
            actor_role=user["role"],
            request_path=request.url.path,
            http_method=request.method,
            ip_address=client_host,
            user_agent=user_agent,
        )
        raise HTTPException(
            status_code=403,
            detail="mfa_enrollment_required",
            headers={MFA_ENROLL_TICKET_HEADER: ticket},
        )

    if totp_enabled:
        code = (credentials.totp_code or "").strip()
        if not code:
            # Password verified but second factor still required. Do NOT count
            # this toward IP lockout — it is a normal two-step flow, not a
            # credential-guessing attempt.
            raise HTTPException(status_code=401, detail="mfa_required")
        if not consume_totp(user["id"], user["totp_secret"], code):
            record_login_attempt(client_host)
            # A wrong second factor is a guess and MUST count toward the per-account
            # lock, not just the per-IP limiter. Otherwise an attacker who already
            # has the password (phished/shoulder-surfed) can brute-force the 6-digit
            # TOTP indefinitely by rotating source IPs — the per-IP limiter is the
            # only thing that fires, and it is defeated by NAT/IP rotation. Counting
            # MFA failures here means 8 wrong codes lock the account exactly like 8
            # wrong passwords do. (Verified: pre-fix, 30 wrong codes left
            # failed_login_attempts at 0.)
            _record_account_failure(user["id"])
            log_audit_event(
                correlation_id=correlation_id,
                event_category="authentication",
                action_type="mfa_failure",
                outcome="failure",
                summary_message="Invalid MFA code",
                actor_user_id=user["id"],
                actor_role=user["role"],
                request_path=request.url.path,
                http_method=request.method,
                ip_address=client_host,
                user_agent=user_agent,
            )
            raise HTTPException(status_code=401, detail="Invalid MFA code")

    # If login succeeds, clear lockout attempts (per-IP and per-account).
    if client_host in LOGIN_ATTEMPTS:
        del LOGIN_ATTEMPTS[client_host]
    _clear_account_failures(user["id"])

    # Legacy hash auto-upgrade check
    if not user["password_hash"].startswith('$2'):
        try:
            new_hash = hash_password(credentials.password)
            db = get_db()
            db.execute("UPDATE users SET password_hash = ? WHERE id = ?", (new_hash, user["id"]))
            db.commit()
            db.close()
            logger.info("Password hash upgraded to bcrypt for user %s", user["id"])
        except Exception as e:
            logger.warning("Failed to upgrade password hash for user %s: %s", user["id"], e)

    _tv = user["token_version"] if "token_version" in user.keys() and user["token_version"] is not None else 0
    token = create_token(user["id"], user["role"], token_version=_tv)

    # Set the HttpOnly session cookie + JS-readable CSRF token so browser clients
    # can drop the localStorage JWT. Machine clients ignore the cookies and keep
    # using the Bearer token from the response body.
    set_session_cookies(response, request, token, generate_csrf_token())

    log_audit_event(
        correlation_id=correlation_id,
        event_category="authentication",
        action_type="login_success",
        outcome="success",
        summary_message="User logged in successfully",
        actor_user_id=user["id"],
        actor_role=user["role"],
        request_path=request.url.path,
        http_method=request.method,
        ip_address=client_host,
        user_agent=user_agent,
    )

    return TokenResponse(
        token=token,
        user=UserOut(
            id=user["id"],
            name=user["name"],
            role=user["role"],
            department=user["department"],
            registration_number=user["registration_number"],
        ),
    )



# ── MFA (TOTP) enrolment + session revocation ──────────────────────────────────
# These endpoints require an authenticated user. get_current_user is applied via
# Depends so the caller must already hold a valid token.
from fastapi import Depends
from auth import get_current_user
from permissions import require_permission
from models.user import MfaEnrollBody, MfaDisableBody


def _load_user_row(user_id: str):
    """Pure-sync users lookup, for `run_db` to execute off the event loop.

    Opens and closes its own connection inside the worker thread, which is
    `run_db`'s contract — the transaction must begin and end there, not span the
    hop back to the loop.
    """
    db = get_db()
    try:
        return db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    finally:
        db.close()


async def _enrolment_principal(request: Request) -> dict:
    """Dependency for the two enrolment endpoints ONLY.

    Accepts a normal session, or — for an account the mandated-role gate is
    holding out of every session — the short-lived enrolment ticket that login
    handed back, presented as `Authorization: Bearer <ticket>`. The ticket is
    honoured nowhere else: it is signed with a key the session decoder does not
    hold, so every other route sees an invalid signature.

    Ticket holders are re-checked against the live users row (existence,
    is_active, token_version) exactly as get_current_user does, so deactivating
    a user or bumping their token_version revokes outstanding tickets too. The
    ticket arrives in a header, not the session cookie, so it carries no CSRF
    exposure of its own.
    """
    auth_header = request.headers.get("authorization") or request.headers.get("Authorization") or ""
    if auth_header.lower().startswith("bearer "):
        payload = _decode_mfa_enroll_ticket(auth_header[7:].strip())
        if payload:
            # OFF THE EVENT LOOP. This ran `get_db()` directly in an `async def`,
            # which opens a SQLCipher connection and — on the driver Orb ships —
            # holds the GIL across close(), the mechanism behind one of the three
            # production freezes (docs/WORKLOG.md §3.3). The lookup is handed to
            # run_db as a REFERENCE so it executes on the private DB executor.
            row = await run_db(_load_user_row, payload["sub"])
            if not row:
                raise HTTPException(status_code=401, detail="User not found")
            principal = dict(row)
            if principal.get("is_active") is not None and not principal["is_active"]:
                raise HTTPException(status_code=403, detail="Account deactivated")
            if int(payload.get("tv", 0)) != int(principal.get("token_version") or 0):
                raise HTTPException(status_code=401, detail="Enrolment ticket revoked; please log in again")
            return principal
    return await get_current_user(request)


@router.post("/mfa/enroll/begin")
def mfa_enroll_begin(request: Request, user: dict = Depends(_enrolment_principal)):
    """Phase 1 of MFA enrolment: generate a secret + otpauth URI for QR scan.

    The secret is stored immediately but totp_enabled stays 0 until the user
    proves possession by confirming a live code (enroll/confirm). Re-calling
    before confirmation rotates the pending secret.
    """
    from services import mfa_service
    from config import MFA_ISSUER

    if user.get("totp_enabled"):
        raise HTTPException(status_code=400, detail="MFA already enabled")

    secret = mfa_service.generate_secret()
    db = get_db()
    db.execute(
        "UPDATE users SET totp_secret = ?, totp_enabled = 0 WHERE id = ?",
        (secret, user["id"]),
    )
    db.commit()
    db.close()

    account = user.get("registration_number") or user.get("name") or user["id"]
    uri = mfa_service.provisioning_uri(secret, str(account), MFA_ISSUER)
    payload = {"secret": secret, "otpauth_uri": uri, "issuer": MFA_ISSUER}
    # QR render of the SAME provisioning URI, in the SAME response — so it is
    # gated by exactly the authorization above (_enrolment_principal: session or
    # enrolment ticket) and adds no new exposure surface. qr_svg_data_uri never
    # raises: if the qrcode package is missing/broken it returns None and the
    # field is simply omitted — the manual setup key remains the enrolment path.
    qr = mfa_service.qr_svg_data_uri(uri)
    if qr:
        payload["qr_svg"] = qr
    return payload


@router.post("/mfa/enroll/confirm")
def mfa_enroll_confirm(body: MfaEnrollBody, request: Request, user: dict = Depends(_enrolment_principal)):
    """Phase 2: verify the first code and flip totp_enabled on."""
    from services import mfa_service

    if user.get("totp_enabled"):
        raise HTTPException(status_code=400, detail="MFA already enabled")
    secret = user.get("totp_secret")
    if not secret:
        raise HTTPException(status_code=400, detail="Call /mfa/enroll/begin first")
    if not consume_totp(user["id"], secret, body.totp_code):
        raise HTTPException(status_code=400, detail="Invalid code")

    db = get_db()
    db.execute("UPDATE users SET totp_enabled = 1 WHERE id = ?", (user["id"],))
    db.commit()
    db.close()

    log_audit_event(
        correlation_id=str(uuid.uuid4()),
        event_category="authentication",
        action_type="mfa_enrolled",
        outcome="success",
        summary_message="MFA enabled for account",
        actor_user_id=user["id"],
        actor_role=user["role"],
        request_path=request.url.path,
        http_method=request.method,
        ip_address=request.client.host if request.client else None,
    )
    return {"status": "enabled"}


@router.post("/mfa/disable")
def mfa_disable(body: MfaDisableBody, request: Request, user: dict = Depends(get_current_user)):
    """Disable MFA. Requires current password AND a live code (defence in depth)."""
    from services import mfa_service

    if not user.get("totp_enabled"):
        raise HTTPException(status_code=400, detail="MFA not enabled")
    # Use 400 (not 401) for these validation failures: the caller is already
    # authenticated (get_current_user passed), so a 401 here would trip the web
    # client's global "session expired" interceptor instead of surfacing an inline
    # "wrong password / code" error. 400 keeps the failure local to this form.
    if not verify_password(body.password, user["password_hash"]):
        raise HTTPException(status_code=400, detail="Invalid credentials")
    if not consume_totp(user["id"], user.get("totp_secret"), body.totp_code):
        raise HTTPException(status_code=400, detail="Invalid MFA code")

    db = get_db()
    db.execute(
        "UPDATE users SET totp_enabled = 0, totp_secret = NULL WHERE id = ?",
        (user["id"],),
    )
    db.commit()
    db.close()

    log_audit_event(
        correlation_id=str(uuid.uuid4()),
        event_category="authentication",
        action_type="mfa_disabled",
        outcome="success",
        summary_message="MFA disabled for account",
        actor_user_id=user["id"],
        actor_role=user["role"],
        request_path=request.url.path,
        http_method=request.method,
        ip_address=request.client.host if request.client else None,
    )
    return {"status": "disabled"}


@router.post("/mfa/reset/{user_id}")
def mfa_admin_reset(user_id: str, request: Request, actor: dict = Depends(require_permission("admin.overview"))):
    """Admin break-glass: clear another user's TOTP factor so they can re-enrol.

    Complements the enrolment ticket, which only covers the never-enrolled case.
    A clinician in an MFA-mandated role who LOSES their authenticator is
    otherwise locked out permanently: /mfa/disable needs a live code, and login
    needs one too. Resetting also bumps token_version, so any session still
    riding the old factor is revoked at the same moment.
    """
    db = get_db()
    try:
        row = db.execute("SELECT id, role FROM users WHERE id = ?", (user_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="User not found")
        db.execute(
            """UPDATE users
                  SET totp_enabled = 0, totp_secret = NULL, totp_last_step = NULL,
                      token_version = COALESCE(token_version, 0) + 1
                WHERE id = ?""",
            (user_id,),
        )
        db.commit()
    finally:
        db.close()

    log_audit_event(
        correlation_id=str(uuid.uuid4()),
        event_category="authentication",
        action_type="mfa_admin_reset",
        outcome="success",
        summary_message=f"Administrator cleared the MFA factor for {user_id}; the account must re-enrol.",
        actor_user_id=actor["id"],
        actor_role=actor["role"],
        target_type="user",
        target_id=user_id,
        request_path=request.url.path,
        http_method=request.method,
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )
    return {"status": "reset", "user_id": user_id}


@router.post("/unlock/{user_id}")
def admin_unlock_account(user_id: str, request: Request, actor: dict = Depends(require_permission("admin.overview"))):
    """Admin break-glass: immediately clear a login lockout so a clinician can get
    back into the EHR without waiting out the window.

    WHY A HARD LOCK PLUS THIS, RATHER THAN A LONG HARD LOCK ALONE
    ------------------------------------------------------------
    Locking a clinician out of the record mid-shift is itself a patient-safety
    event: a doctor who fat-fingers a password during a crash call must not be
    stranded for 15 minutes. The per-account control is deliberately tuned for a
    hospital, not a bank:
      * ACCOUNT_MAX_FAILURES is generous (8) and a SINGLE correct password resets
        the counter to zero (see _clear_account_failures on the success path), so
        the ordinary typo-then-correct never trips it — only a sustained run of
        wrong credentials does.
      * The lock is a SHORT window (ACCOUNT_LOCK_SECONDS = 15 min), not indefinite,
        so even with no admin around it self-heals, and a correct password works
        again the moment it lapses.
      * This endpoint is the immediate escape hatch: an admin (or the charge nurse
        with admin rights) clears it in one click, so a genuinely-locked-out
        clinician is back in seconds. That is the safety valve that makes the hard
        lock acceptable — it is the "admin needs a way to clear it immediately"
        requirement, and it is why a hard lock is defensible here instead of a
        pure exponential-response delay (which would block the async event loop
        for every waiting request and cannot be cleared early).
    The unlock is audited as a security event.
    """
    db = get_db()
    try:
        row = db.execute(
            "SELECT id, failed_login_attempts, login_locked_until, device_pin_locked_until "
            "FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="User not found")
        was_locked = _account_is_locked(row)
        was_pin_locked = _locked_until(row, "device_pin_locked_until")
        # Clears the PASSWORD lock and the TABLET-PIN lock in one action. They
        # are separate counters on purpose (a PIN brute-force must not strand a
        # clinician on the password path), but an administrator asked to "let her
        # back in" means both — making them clear the same person twice through
        # two endpoints is how one of them stays locked at 3 a.m.
        db.execute(
            "UPDATE users SET failed_login_attempts = 0, login_locked_until = NULL, "
            "device_pin_failed_attempts = 0, device_pin_locked_until = NULL WHERE id = ?",
            (user_id,),
        )
        db.commit()
    finally:
        db.close()

    log_audit_event(
        correlation_id=str(uuid.uuid4()),
        event_category="authentication",
        action_type="account_unlocked",
        outcome="success",
        summary_message=f"Administrator cleared the login lockout for {user_id}.",
        actor_user_id=actor["id"],
        actor_role=actor["role"],
        target_type="user",
        target_id=user_id,
        request_path=request.url.path,
        http_method=request.method,
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
        metadata={"was_locked": bool(was_locked), "was_pin_locked": bool(was_pin_locked)},
    )
    return {
        "status": "unlocked",
        "user_id": user_id,
        "was_locked": bool(was_locked),
        "was_pin_locked": bool(was_pin_locked),
    }


@router.post("/logout")
def logout(request: Request, response: Response, user: dict = Depends(get_current_user)):
    """Revoke ALL of this user's outstanding sessions by bumping token_version.

    Every previously-issued JWT embeds the token_version it was minted at;
    get_current_user rejects any token whose tv no longer matches the row, so a
    single increment invalidates them all (this device and every other)."""
    db = get_db()
    db.execute(
        "UPDATE users SET token_version = COALESCE(token_version, 0) + 1 WHERE id = ?",
        (user["id"],),
    )
    db.commit()
    db.close()

    # Clear the session + CSRF cookies for browser clients (the tv bump already
    # invalidates the token itself, but drop the cookie so it isn't re-sent).
    clear_session_cookies(response)

    log_audit_event(
        correlation_id=str(uuid.uuid4()),
        event_category="authentication",
        action_type="logout",
        outcome="success",
        summary_message="User logged out; all sessions revoked",
        actor_user_id=user["id"],
        actor_role=user["role"],
        request_path=request.url.path,
        http_method=request.method,
        ip_address=request.client.host if request.client else None,
    )
    return {"status": "logged_out"}


@router.post("/refresh", response_model=TokenResponse)
def refresh(request: Request, response: Response, user: dict = Depends(get_current_user)):
    """Slide the session forward: re-mint the JWT at the CURRENT token_version and
    re-set the session + CSRF cookies, extending expiry without a re-login. Runs
    behind get_current_user, so a revoked/expired/deactivated session cannot be
    refreshed (it 401s first). Cookie clients must pass the CSRF header (enforced
    by the middleware, since this is a cookie-authenticated POST).

    A PIN-on-device session refreshes AS a PIN-on-device session: same device
    binding, same short lifetime. Re-minting it with the default 24-hour expiry
    would make the short window theatre — one /refresh from a tablet on a trolley
    and it is holding a day-long session again — and dropping `did` would quietly
    un-bind it from the tablet, so revoking that device would no longer kill it.
    """
    _tv = user["token_version"] if "token_version" in user.keys() and user["token_version"] is not None else 0
    device_id = user.get("device_id")
    token = create_token(
        user["id"], user["role"], token_version=_tv,
        device_id=device_id,
        auth_method=user.get("auth_method") if device_id else None,
        expires_in_minutes=DEVICE_PIN_SESSION_MINUTES if device_id else None,
    )
    set_session_cookies(
        response, request, token, generate_csrf_token(),
        max_age=DEVICE_PIN_SESSION_MINUTES * 60 if device_id else None,
    )
    return TokenResponse(
        token=token,
        user=UserOut(
            id=user["id"],
            name=user["name"],
            role=user["role"],
            department=user["department"],
            registration_number=user["registration_number"],
        ),
    )


# ══════════════════════════════════════════════════════════════════════════════
#  PIN-ON-DEVICE IDENTITY (shared ward tablet)
# ══════════════════════════════════════════════════════════════════════════════
# THE PROBLEM. Typing a full password before every patient interaction is what
# makes staff share a login, and a shared login destroys the audit trail: every
# row says NUR001 when three different nurses touched the chart. Attribution is
# the entire point of the trail, so the fix has to make signing in as YOURSELF
# the path of least resistance on a shared tablet.
#
# THE SHAPE. An administrator enrols the tablet ONCE; it holds a long-lived
# device credential. A person then signs in on top of it with a short PIN. The
# session that results names the PERSON — the device is context in the audit
# metadata, never the actor.
#
# WHAT MAKES IT SAFE RATHER THAN JUST CONVENIENT:
#   * The PIN is worthless off the tablet (a stolen PIN on a personal laptop
#     carries no device credential and gets a flat 401).
#   * PIN verification is constant-work (auth.verify_device_pin_ct) — the 90x
#     unknown-user timing oracle closed on the password path this week is not
#     reintroduced here, where the response bodies are identical and latency
#     would be the ONLY signal.
#   * Two independent throttles: per account, and per device (the sweep).
#   * The session expires in minutes, and /refresh cannot launder it longer.
#   * Revoking the tablet severs its live sessions on the next request.
#   * An administrator can clear either lock instantly — a clinician locked out
#     mid-shift is itself a patient-safety event.


def _device_public(row) -> dict:
    """The admin-visible view of an enrolled device. NEVER includes secret_hash:
    the digest is not a usable credential, but publishing it turns an admin-read
    into an offline target for no benefit at all."""
    return {
        "device_id": row["id"],
        "label": row["label"],
        "department": row["department"],
        "enrolled_by": row["enrolled_by"],
        "enrolled_at": row["enrolled_at"],
        "last_seen_at": row["last_seen_at"],
        "revoked_at": row["revoked_at"],
        "active": row["revoked_at"] is None,
        "pin_locked": _locked_until(row, "pin_locked_until"),
    }


@router.post("/devices", status_code=201)
def enroll_device(
    body: DeviceEnrollBody,
    request: Request,
    actor: dict = Depends(require_permission("admin.overview")),
):
    """Admin enrols a ward tablet and receives its secret ONCE.

    The secret is returned in this response and never again — only its SHA-256
    digest is stored, so a later database read (or a stolen backup) does not hand
    over a working device credential. If the tablet is re-imaged, enrol it again
    and revoke the old row; that is deliberate, and cheaper than the alternative
    of a retrievable secret sitting in the users' own governance screen.
    """
    secret = generate_device_secret()
    device_id = f"dev_{uuid.uuid4().hex[:16]}"
    db = get_db()
    try:
        db.execute(
            """INSERT INTO enrolled_devices (id, label, department, secret_hash, enrolled_by)
               VALUES (?, ?, ?, ?, ?)""",
            (device_id, body.label.strip(), (body.department or "").strip() or None,
             hash_device_secret(secret), actor["id"]),
        )
        db.commit()
    finally:
        db.close()

    log_audit_event(
        correlation_id=str(uuid.uuid4()),
        event_category="authentication",
        action_type="device_enrolled",
        outcome="success",
        summary_message=f"Administrator enrolled tablet '{body.label}' for PIN sign-in.",
        actor_user_id=actor["id"],
        actor_role=actor["role"],
        target_type="device",
        target_id=device_id,
        request_path=request.url.path,
        http_method=request.method,
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
        metadata={"label": body.label, "department": body.department},
    )
    return {
        "device_id": device_id,
        "label": body.label,
        "department": body.department,
        # Shown once. The caller must transfer it to the tablet now.
        "device_secret": secret,
    }


@router.get("/devices")
def list_devices(
    request: Request,
    actor: dict = Depends(require_permission("admin.overview")),
):
    """The tablet inventory: which devices can take PIN sign-ins, and which are
    revoked or currently throttled. No secrets, in any form."""
    db = get_db()
    try:
        rows = db.execute(
            "SELECT * FROM enrolled_devices ORDER BY (revoked_at IS NOT NULL), enrolled_at DESC"
        ).fetchall()
    finally:
        db.close()
    return {"devices": [_device_public(r) for r in rows]}


@router.post("/devices/{device_id}/revoke")
def revoke_device(
    device_id: str,
    request: Request,
    actor: dict = Depends(require_permission("admin.overview")),
):
    """Retire a tablet — lost, stolen, re-imaged, or leaving the ward.

    This is the control that makes a long-lived device credential acceptable. It
    stops new PIN sign-ins AND severs the sessions already minted on that tablet:
    every such session carries the device id, and auth.get_current_user re-checks
    the device on each request, so access ends at the next call rather than
    whenever the token happened to expire.

    The row is marked, never deleted: the audit rows written from this tablet
    name its id, and deleting it would make them unreadable.
    """
    db = get_db()
    try:
        row = db.execute("SELECT * FROM enrolled_devices WHERE id = ?", (device_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Device not found")
        if row["revoked_at"] is None:
            db.execute(
                "UPDATE enrolled_devices SET revoked_at = CURRENT_TIMESTAMP, revoked_by = ? "
                "WHERE id = ?",
                (actor["id"], device_id),
            )
            db.commit()
    finally:
        db.close()

    log_audit_event(
        correlation_id=str(uuid.uuid4()),
        event_category="authentication",
        action_type="device_revoked",
        outcome="success",
        summary_message=f"Administrator revoked tablet '{row['label']}'; its PIN sessions are ended.",
        actor_user_id=actor["id"],
        actor_role=actor["role"],
        target_type="device",
        target_id=device_id,
        request_path=request.url.path,
        http_method=request.method,
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )
    return {"status": "revoked", "device_id": device_id}


@router.post("/devices/{device_id}/unlock")
def unlock_device(
    device_id: str,
    request: Request,
    actor: dict = Depends(require_permission("admin.overview")),
):
    """Clear a tablet's PIN throttle immediately.

    The device lock exists to stop a PIN sweep across the whole roster, but it
    takes the WARD's tablet offline for everyone on it — so it self-heals in 15
    minutes and an administrator can end it in one click, exactly like the
    per-account unlock above. Same reasoning: a hard lock is only defensible when
    there is an immediate way out of it.
    """
    db = get_db()
    try:
        row = db.execute(
            "SELECT id, label, pin_locked_until FROM enrolled_devices WHERE id = ?",
            (device_id,),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Device not found")
        was_locked = _locked_until(row, "pin_locked_until")
        db.execute(
            "UPDATE enrolled_devices SET failed_pin_attempts = 0, pin_locked_until = NULL "
            "WHERE id = ?",
            (device_id,),
        )
        db.commit()
    finally:
        db.close()

    log_audit_event(
        correlation_id=str(uuid.uuid4()),
        event_category="authentication",
        action_type="device_unlocked",
        outcome="success",
        summary_message=f"Administrator cleared the PIN throttle on tablet '{row['label']}'.",
        actor_user_id=actor["id"],
        actor_role=actor["role"],
        target_type="device",
        target_id=device_id,
        request_path=request.url.path,
        http_method=request.method,
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
        metadata={"was_locked": bool(was_locked)},
    )
    return {"status": "unlocked", "device_id": device_id, "was_locked": bool(was_locked)}


@router.post("/device/bind")
def bind_device(body: DeviceBindBody, request: Request, response: Response):
    """One-time exchange: the raw device secret for an HttpOnly device cookie.

    UNAUTHENTICATED on purpose — the secret IS the credential, and this runs on a
    freshly-imaged tablet before any person has signed in. It is rate-limited by
    the same per-IP limiter as /login, and the secret is 256 bits of randomness,
    so guessing it is not an attack that finishes.

    WHY BOTHER instead of letting the SPA keep the secret and send a header. A
    long-lived credential in localStorage is readable by any script on the origin
    — the exact hole the session JWT was moved out of. After this call the raw
    secret can be destroyed on the client and the browser carries an HttpOnly
    cookie that JavaScript cannot read.
    """
    client_host = request.client.host if request.client else None
    if is_ip_rate_limited(client_host):
        raise HTTPException(status_code=429, detail="Too many attempts. Please try again in 15 minutes.")

    device = load_active_device(body.device_secret.strip())
    if not device:
        record_login_attempt(client_host)
        log_audit_event(
            correlation_id=str(uuid.uuid4()),
            event_category="authentication",
            action_type="device_bind_failure",
            outcome="failure",
            summary_message="Device binding refused: unknown or revoked device credential.",
            request_path=request.url.path,
            http_method=request.method,
            ip_address=client_host,
            user_agent=request.headers.get("user-agent"),
        )
        raise HTTPException(status_code=401, detail="This device credential is not valid.")

    set_device_cookie(response, request, body.device_secret.strip())
    log_audit_event(
        correlation_id=str(uuid.uuid4()),
        event_category="authentication",
        action_type="device_bound",
        outcome="success",
        summary_message=f"Tablet '{device['label']}' bound to a browser for PIN sign-in.",
        target_type="device",
        target_id=device["id"],
        request_path=request.url.path,
        http_method=request.method,
        ip_address=client_host,
        user_agent=request.headers.get("user-agent"),
    )
    return {
        "device_id": device["id"],
        "label": device["label"],
        "department": device["department"],
    }


@router.get("/device")
def device_status(request: Request, response: Response):
    """Does THIS browser hold a valid device credential?

    The login screen calls it before any session exists, to decide whether to
    offer the PIN pad. It discloses only the tablet's own label, and only to a
    caller that already holds its credential — deliberately NOT the staff roster:
    a name-picker would be friendlier, but it would also mean a stolen tablet
    hands over the ward's staff list. Staff type their own ID.
    """
    secret = extract_device_token(request)
    device = load_active_device(secret) if secret else None
    if not device:
        # A cookie that no longer resolves (the tablet was revoked, or the
        # appliance was rebuilt) is dropped here rather than left to be re-sent
        # on every request for a year. Cheap, and it means a re-enrolled tablet
        # starts from a clean state instead of carrying a dead credential.
        if request.cookies.get(DEVICE_COOKIE):
            clear_device_cookie(response)
        return {"enrolled": False}
    return {
        "enrolled": True,
        "device_id": device["id"],
        "label": device["label"],
        "department": device["department"],
        "session_minutes": DEVICE_PIN_SESSION_MINUTES,
    }


@router.post("/pin-login", response_model=TokenResponse)
def pin_login(credentials: PinLogin, request: Request, response: Response):
    """Sign in as a PERSON, with a short PIN, on an enrolled tablet.

    ORDER OF OPERATIONS IS LOAD-BEARING. The constant-work PIN verification runs
    BEFORE the account-lock branch, not after. Checking the lock first would
    return early without spending bcrypt time and re-open the enumeration oracle
    through a side door: "locked" implies "exists", and latency would say so on
    every probe. Same reason the unknown-user and no-PIN-provisioned branches
    still pay a full bcrypt (see auth.verify_device_pin_ct).
    """
    correlation_id = str(uuid.uuid4())
    client_host = request.client.host if request.client else None
    user_agent = request.headers.get("user-agent")

    if is_ip_rate_limited(client_host):
        raise HTTPException(
            status_code=429,
            detail="Too many sign-in attempts. Please try again in 15 minutes.",
        )

    # ── 1. The device. No enrolled tablet, no PIN sign-in — full stop. ────────
    # This is what makes a stolen PIN worth nothing on a personal laptop. The
    # error is deliberately distinct from a wrong PIN: a tablet that has lost its
    # binding must be able to tell "re-enrol me" from "wrong PIN", and confirming
    # the validity of a credential the caller already holds escalates nothing.
    secret = extract_device_token(request)
    device = load_active_device(secret) if secret else None
    if not device:
        record_login_attempt(client_host)
        log_audit_event(
            correlation_id=correlation_id,
            event_category="authentication",
            action_type="pin_login_no_device",
            outcome="denied",
            summary_message="PIN sign-in refused: no enrolled device credential on this client.",
            request_path=request.url.path,
            http_method=request.method,
            ip_address=client_host,
            user_agent=user_agent,
            metadata={"attempted_id": credentials.employee_id},
        )
        raise HTTPException(
            status_code=401,
            detail="This device is not enrolled for PIN sign-in. Use your password, "
                   "or ask an administrator to enrol this tablet.",
        )

    # ── 2. Device-wide throttle (the roster sweep). ───────────────────────────
    if _locked_until(device, "pin_locked_until"):
        log_audit_event(
            correlation_id=correlation_id,
            event_category="authentication",
            action_type="pin_login_device_locked",
            outcome="denied",
            summary_message="PIN sign-in blocked: this tablet is temporarily throttled "
                            "after repeated failed PINs.",
            target_type="device",
            target_id=device["id"],
            request_path=request.url.path,
            http_method=request.method,
            ip_address=client_host,
            user_agent=user_agent,
        )
        raise HTTPException(
            status_code=429,
            detail="This tablet is temporarily locked after too many failed PINs. "
                   "Sign in with your password, or ask an administrator to clear it.",
        )

    db = get_db()
    try:
        user = db.execute(
            "SELECT * FROM users WHERE id = ?", (credentials.employee_id,)
        ).fetchone()
    finally:
        db.close()

    # ── 3. Constant-work verification, BEFORE any user-dependent branch. ──────
    keys = user.keys() if user is not None else ()
    stored_pin = user["device_pin_hash"] if "device_pin_hash" in keys else None
    pin_ok = verify_device_pin_ct(credentials.pin, stored_pin)

    # ── 4. Per-account throttle. ─────────────────────────────────────────────
    if user is not None and _locked_until(user, "device_pin_locked_until"):
        log_audit_event(
            correlation_id=correlation_id,
            event_category="authentication",
            action_type="pin_login_account_locked",
            outcome="denied",
            summary_message="PIN sign-in blocked: this account's PIN is temporarily locked.",
            actor_user_id=user["id"],
            actor_role=user["role"],
            target_type="device",
            target_id=device["id"],
            request_path=request.url.path,
            http_method=request.method,
            ip_address=client_host,
            user_agent=user_agent,
        )
        raise HTTPException(
            status_code=429,
            detail="Your PIN is temporarily locked after too many failed attempts. "
                   "Sign in with your password, or ask an administrator to clear it.",
        )

    if user is None or not pin_ok:
        record_login_attempt(client_host)
        _record_pin_failure(user["id"] if user is not None else None, device["id"])
        log_audit_event(
            correlation_id=correlation_id,
            event_category="authentication",
            action_type="pin_login_failure",
            outcome="failure",
            summary_message="Invalid staff ID or PIN on an enrolled device.",
            target_type="device",
            target_id=device["id"],
            request_path=request.url.path,
            http_method=request.method,
            ip_address=client_host,
            user_agent=user_agent,
            metadata={"attempted_id": credentials.employee_id, "device_id": device["id"]},
        )
        raise HTTPException(status_code=401, detail="Invalid staff ID or PIN")

    # ── 5. Post-authentication gates. ────────────────────────────────────────
    # Checked AFTER verification so an unauthenticated caller cannot use the
    # response to learn which ids are deactivated or privileged.
    if "is_active" in keys and user["is_active"] is not None and not user["is_active"]:
        log_audit_event(
            correlation_id=correlation_id, event_category="authentication",
            action_type="pin_login_denied_inactive", outcome="denied",
            summary_message="PIN sign-in denied: account is deactivated.",
            actor_user_id=user["id"], actor_role=user["role"],
            request_path=request.url.path, http_method=request.method,
            ip_address=client_host, user_agent=user_agent,
        )
        raise HTTPException(status_code=403, detail="Account deactivated. Contact an administrator.")

    from config import MFA_REQUIRED_ROLES
    # A PIN must not become the back door around a TOTP mandate. Device + PIN is
    # genuinely two factors, but it is not the factor the hospital mandated for
    # this role, and silently accepting a weaker one is how a policy stops being
    # a policy. Patients never use ward tablets, so they are excluded outright.
    if user["role"] == "patient" or user["role"] in MFA_REQUIRED_ROLES:
        log_audit_event(
            correlation_id=correlation_id, event_category="authentication",
            action_type="pin_login_denied_role", outcome="denied",
            summary_message=f"PIN sign-in is not permitted for role '{user['role']}'.",
            actor_user_id=user["id"], actor_role=user["role"],
            request_path=request.url.path, http_method=request.method,
            ip_address=client_host, user_agent=user_agent,
        )
        raise HTTPException(
            status_code=403,
            detail="PIN sign-in is not available for this account. Use your password.",
        )

    # ── 6. Success. ──────────────────────────────────────────────────────────
    if client_host in LOGIN_ATTEMPTS:
        del LOGIN_ATTEMPTS[client_host]
    _clear_pin_failures(user["id"], device["id"])

    _tv = user["token_version"] if "token_version" in keys and user["token_version"] is not None else 0
    token = create_token(
        user["id"], user["role"], token_version=_tv,
        device_id=device["id"], auth_method="pin",
        expires_in_minutes=DEVICE_PIN_SESSION_MINUTES,
    )
    set_session_cookies(
        response, request, token, generate_csrf_token(),
        max_age=DEVICE_PIN_SESSION_MINUTES * 60,
    )

    # ATTRIBUTION — the reason this feature exists. actor_user_id is the PERSON.
    # The device is context in metadata and must never be the actor: a trail that
    # names the tablet is exactly the shared-login trail we are replacing.
    log_audit_event(
        correlation_id=correlation_id,
        event_category="authentication",
        action_type="pin_login_success",
        outcome="success",
        summary_message=f"{user['name']} signed in with a PIN on tablet '{device['label']}'.",
        actor_user_id=user["id"],
        actor_role=user["role"],
        target_type="device",
        target_id=device["id"],
        request_path=request.url.path,
        http_method=request.method,
        ip_address=client_host,
        user_agent=user_agent,
        metadata={
            "device_id": device["id"],
            "device_label": device["label"],
            "auth_method": "pin",
            "session_minutes": DEVICE_PIN_SESSION_MINUTES,
        },
    )

    return TokenResponse(
        token=token,
        user=UserOut(
            id=user["id"],
            name=user["name"],
            role=user["role"],
            department=user["department"],
            registration_number=user["registration_number"],
        ),
    )


def _write_device_pin(user_id: str, pin: str) -> None:
    from services import pin_security
    db = get_db()
    try:
        db.execute(
            "UPDATE users SET device_pin_hash = ?, device_pin_set_at = CURRENT_TIMESTAMP, "
            "device_pin_failed_attempts = 0, device_pin_locked_until = NULL WHERE id = ?",
            (pin_security.hash_pin(pin), user_id),
        )
        db.commit()
    finally:
        db.close()


@router.post("/device-pin")
def set_own_device_pin(
    body: DevicePinBody,
    request: Request,
    user: dict = Depends(get_current_user),
):
    """Set (or change) your own tablet PIN.

    The current PASSWORD is required even though the caller already holds a
    session: a tablet left unlocked for ninety seconds is the threat this whole
    feature is built around, and without the password re-check the first thing an
    opportunist could do with a borrowed session is mint themselves a permanent
    PIN credential for that nurse.

    400, not 401, on a wrong password — the caller is already authenticated, so a
    401 would trip the SPA's global "session expired" interceptor instead of
    showing an inline error (same reasoning as /mfa/disable).
    """
    if not verify_password(body.password, user["password_hash"]):
        raise HTTPException(status_code=400, detail="Invalid credentials")
    validate_pin_format(body.pin)
    _write_device_pin(user["id"], body.pin)

    log_audit_event(
        correlation_id=str(uuid.uuid4()),
        event_category="authentication",
        action_type="device_pin_set",
        outcome="success",
        summary_message="Tablet PIN set by the account owner.",
        actor_user_id=user["id"],
        actor_role=user["role"],
        target_type="user",
        target_id=user["id"],
        request_path=request.url.path,
        http_method=request.method,
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )
    return {"status": "pin_set", "id": user["id"]}


@router.post("/device-pin/{user_id}")
def admin_set_device_pin(
    user_id: str,
    body: AdminDevicePinBody,
    request: Request,
    actor: dict = Depends(require_permission("admin.overview")),
):
    """Admin provisions a tablet PIN for a member of staff.

    The onboarding path (a new nurse cannot set a PIN she has not been given) and
    the recovery path (a forgotten PIN, mid-shift). Setting a PIN also clears any
    PIN lockout on that account, so this doubles as the fastest way to get a
    stranded clinician back onto the ward tablet.
    """
    validate_pin_format(body.pin)
    db = get_db()
    try:
        row = db.execute("SELECT id, role FROM users WHERE id = ?", (user_id,)).fetchone()
    finally:
        db.close()
    if not row:
        raise HTTPException(status_code=404, detail="User not found")
    _write_device_pin(user_id, body.pin)

    log_audit_event(
        correlation_id=str(uuid.uuid4()),
        event_category="authentication",
        action_type="device_pin_provisioned",
        outcome="success",
        summary_message=f"Administrator provisioned a tablet PIN for {user_id}.",
        actor_user_id=actor["id"],
        actor_role=actor["role"],
        target_type="user",
        target_id=user_id,
        request_path=request.url.path,
        http_method=request.method,
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )
    return {"status": "pin_set", "id": user_id}
