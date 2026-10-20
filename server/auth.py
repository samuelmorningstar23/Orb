"""Orb Hospital OS — JWT Authentication"""

import hashlib
import hmac
import os
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import bcrypt
import jwt
from config import JWT_ALGORITHM, JWT_EXPIRY_HOURS, JWT_SECRET
from database import db_session, run_db
from fastapi import Depends, HTTPException, Request, Response

# ── Cookie-based session (Deferred A) ─────────────────────────────────────────
# The JWT can be delivered EITHER as an `Authorization: Bearer` header (machine
# clients, the test suite, backward compat) OR as an HttpOnly `orb_session`
# cookie. The cookie is unreadable to JavaScript, so a single XSS (or a
# compromised npm dependency) can no longer exfiltrate a clinician's persistent
# session — the previous localStorage token could be read by any script on the
# origin. Cookies are auto-sent by the browser, which reintroduces CSRF risk, so
# cookie-authenticated *mutations* additionally require a double-submit CSRF
# token (see csrf_token_valid + the middleware in main.py). Bearer-authenticated
# requests carry an explicit header a cross-site attacker cannot forge, so they
# are CSRF-exempt.
SESSION_COOKIE = "orb_session"
CSRF_COOKIE = "orb_csrf"
CSRF_HEADER = "x-csrf-token"
UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

# ── Device credential (PIN-on-device identity) ───────────────────────────────
# A shared ward tablet is enrolled ONCE by an administrator and holds a
# long-lived device credential; a PERSON then signs in on top of it with a short
# PIN. The credential travels either as an HttpOnly cookie (the normal state,
# after POST /api/auth/device/bind exchanges the raw secret for it) or as an
# explicit header (the one-time bind call, and machine clients). HttpOnly is the
# point: the raw secret never has to live in localStorage where a single XSS
# could read it off the tablet — the same hole the session JWT was moved out of.
DEVICE_COOKIE = "orb_device"
DEVICE_HEADER = "x-orb-device-token"
# A tablet is bound for as long as it lives on the ward; re-binding every day
# would put the raw secret back in a human's hands over and over, which is worse
# than a long-lived cookie the admin can revoke server-side in one click.
DEVICE_COOKIE_MAX_AGE = 365 * 24 * 3600


def _int_env(name: str, default: int, *, minimum: int = 1) -> int:
    """Read a positive integer setting, falling back on anything unparseable.

    Deliberately local rather than in config.py: these are auth-cluster knobs and
    a malformed value must never take the login path down at import time.
    """
    try:
        value = int(str(os.getenv(name, "")).strip())
    except (TypeError, ValueError):
        return default
    return value if value >= minimum else default


# Requirement: SHORT auto-logout. A tablet abandoned on a trolley is the threat,
# so a PIN session is minted with a lifetime measured in minutes rather than the
# JWT_EXPIRY_HOURS a password session gets. This is the SERVER-side floor — the
# SPA additionally arms a shorter idle timer — and it is enforced by the token's
# own `exp`, so it holds even if the tablet's clock or JS is tampered with.
DEVICE_PIN_SESSION_MINUTES = _int_env("ORB_DEVICE_PIN_SESSION_MINUTES", 15)


def generate_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def _effective_https(request: Request) -> bool:
    """True if the client-facing connection is HTTPS, honouring a terminating
    proxy's X-Forwarded-Proto (ngrok/LAN reverse proxies terminate TLS)."""
    fwd = request.headers.get("x-forwarded-proto", "")
    if fwd:
        return fwd.split(",")[0].strip().lower() == "https"
    return request.url.scheme == "https"


def cookie_secure(request: Request) -> bool:
    """Whether to mark session cookies Secure. `ORB_COOKIE_SECURE` forces it on
    (1/true) or off (0/false); the default 'auto' sets Secure per-request based on
    the effective scheme — so an HTTPS (ngrok) session gets a Secure cookie while
    a plain-HTTP LAN pilot still works instead of silently dropping the cookie."""
    override = os.getenv("ORB_COOKIE_SECURE", "auto").lower()
    if override in ("1", "true", "yes"):
        return True
    if override in ("0", "false", "no"):
        return False
    return _effective_https(request)


def set_session_cookies(
    response: Response,
    request: Request,
    token: str,
    csrf: str,
    max_age: int | None = None,
) -> None:
    """Set the session + CSRF cookies.

    `max_age` overrides the default JWT_EXPIRY_HOURS window. A PIN session passes
    its own short lifetime so the COOKIE dies with the token — leaving a 24-hour
    cookie carrying a 15-minute JWT would mean every request from an idle tablet
    arrives authenticated-looking and 401s, which is exactly the shape that trips
    the SPA's global "session expired" interceptor at random.
    """
    secure = cookie_secure(request)
    if max_age is None:
        max_age = JWT_EXPIRY_HOURS * 3600
    # HttpOnly session (JS cannot read it) — the actual credential.
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=max_age,
        httponly=True,
        secure=secure,
        samesite="lax",
        path="/",
    )
    # CSRF token — deliberately readable by JS so the SPA can echo it back in the
    # X-CSRF-Token header (double-submit). It is NOT a credential on its own.
    response.set_cookie(
        CSRF_COOKIE,
        csrf,
        max_age=max_age,
        httponly=False,
        secure=secure,
        samesite="lax",
        path="/",
    )


def clear_session_cookies(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.delete_cookie(CSRF_COOKIE, path="/")


def csrf_token_valid(request: Request) -> bool:
    """Double-submit check: the X-CSRF-Token header must match the orb_csrf cookie
    (both present, constant-time compared). A cross-site attacker can trigger the
    browser to send the cookie but cannot read it to set the matching header."""
    cookie = request.cookies.get(CSRF_COOKIE)
    header = request.headers.get(CSRF_HEADER)
    if not cookie or not header:
        return False
    return hmac.compare_digest(cookie, header)


def request_has_bearer(request: Request) -> bool:
    auth = request.headers.get("authorization") or request.headers.get("Authorization") or ""
    return auth.lower().startswith("bearer ")


def _extract_token(request: Request) -> tuple[str | None, str]:
    """Pull the JWT from the Authorization header (preferred) or the session
    cookie. Returns (token, source) where source is 'header' | 'cookie' | 'none'."""
    auth = request.headers.get("authorization") or request.headers.get("Authorization")
    if auth and auth.lower().startswith("bearer "):
        return auth[7:].strip(), "header"
    cookie = request.cookies.get(SESSION_COOKIE)
    if cookie:
        return cookie, "cookie"
    return None, "none"


def hash_password(password: str) -> str:
    """Hash password using salted bcrypt."""
    pwd_bytes = password.encode("utf-8")
    salt = bcrypt.gensalt()
    hashed = bcrypt.hashpw(pwd_bytes, salt)
    return hashed.decode("utf-8")


def verify_password(password: str, hashed: str) -> bool:
    """Verify password against hashed. Supports legacy SHA-256 and bcrypt hashes."""
    if not hashed.startswith("$2"):
        # Fallback to legacy SHA-256 hash. Use a constant-time comparison so the
        # legacy path does not leak hash length/prefix via short-circuit timing.
        legacy_hash = hashlib.sha256(password.encode()).hexdigest()
        return hmac.compare_digest(legacy_hash, hashed)

    try:
        return bcrypt.checkpw(password.encode("utf-8"), hashed.encode("utf-8"))
    except Exception:
        return False


def legacy_sha256_allowed() -> bool:
    """May an account still holding an unsalted-SHA-256 password hash log in?

    Default NO: login rejects such accounts with a 'password reset required'
    error until an administrator resets the password (or the account signs in
    once while this hatch is open, which upgrades the hash to bcrypt in place).
    ORB_ALLOW_LEGACY_SHA256=1 is the temporary escape hatch for a migration
    window — documented as such in .env.example.

    Read from the environment at CALL time, not import time, so an operator can
    close the window with a restart-free `unset` in a supervisor drop-in and so
    tests can exercise both sides of the gate.
    """
    return os.getenv("ORB_ALLOW_LEGACY_SHA256", "").strip().lower() in ("1", "true", "yes", "on")


def count_legacy_password_hashes() -> int:
    """How many accounts still hold a legacy unsalted-SHA-256 password hash.

    A COUNT only — never ids or names; the number is an operational posture
    fact, but which accounts are stuck is enumeration bait in a log file.
    BLOCKING: call at startup or via run_db, never bare on the event loop.

    Only a value SHAPED like an unsalted SHA-256 digest (64 hex characters,
    which is all verify_password's legacy branch can ever match) counts. The
    built-in non-login accounts carry sentinel strings instead ('system_hash'
    for the Sage system user in database.py, 'system' for the lab and Vigil
    bots), and counting those made every fresh appliance log "3 account(s)
    ... cannot log in until an administrator resets their passwords" on each
    boot: an instruction to reset passwords on accounts that have none.
    """
    with db_session() as db:
        row = db.execute(
            "SELECT COUNT(*) AS n FROM users "
            "WHERE password_hash IS NOT NULL AND password_hash NOT LIKE '$2%' "
            "AND length(password_hash) = 64 "
            "AND lower(password_hash) NOT GLOB '*[^0-9a-f]*'"
        ).fetchone()
        return int(row["n"] if row else 0)


# A stable, real bcrypt hash of a value no one can log in with. It exists ONLY so
# the "no such user" branch of login can spend the SAME ~bcrypt time as a real
# account, closing the user-enumeration timing oracle (see verify_password_ct).
# Computed once at import with the default cost factor, matching hash_password(),
# so the burnt time tracks the cost real accounts actually pay.
_DUMMY_BCRYPT_HASH = bcrypt.hashpw(b"orb-timing-equalizer-not-a-real-password", bcrypt.gensalt())


def verify_password_ct(password: str, hashed: str | None) -> bool:
    """Constant-work password check that does NOT reveal whether the account exists.

    The plain login path used to read `not user or not verify_password(...)`,
    which short-circuits: for an unknown employee_id bcrypt was never run, so the
    reply came back in ~2 ms instead of the ~200 ms a real bcrypt account costs.
    That timing gap is a perfect user-enumeration oracle — an attacker can sweep
    candidate ids and read the staff list straight off the response latency, no
    valid password required (measured 90x: real≈223 ms vs fake≈2.5 ms, fully
    separable).

    This helper ALWAYS performs one bcrypt verification. When `hashed` is None
    (no such user) it verifies against a throwaway dummy hash and returns False,
    so the missing-user branch costs the same as the real-user branch. Callers
    MUST route both branches through here rather than short-circuiting on `user`.
    """
    if not hashed:
        # Burn the equivalent bcrypt time against a dummy hash, then fail. The
        # result is discarded — an unknown user can never authenticate — but the
        # work makes the timing indistinguishable from a real wrong-password.
        try:
            bcrypt.checkpw(password.encode("utf-8"), _DUMMY_BCRYPT_HASH)
        except Exception:
            pass
        return False
    return verify_password(password, hashed)


def verify_device_pin_ct(pin: str | None, hashed: str | None) -> bool:
    """Constant-work PIN check that reveals NOTHING about the account.

    A 4-6 digit PIN is trivially guessable, so the online path is rate-limited —
    but rate limiting is worthless if latency answers "does this staff id exist,
    and does it have a tablet PIN?" for free. That is not hypothetical: the plain
    login path shipped exactly that oracle this week (unknown user skipped
    bcrypt, ~2.5 ms vs ~223 ms, 90x and fully separable), and the response bodies
    here are identical by construction, so timing would be the ONLY signal.

    So this ALWAYS performs exactly ONE bcrypt verification, on every path:
      * no such user           → bcrypt against the dummy hash, return False
      * user with no PIN set   → bcrypt against the dummy hash, return False
      * legacy/corrupt hash    → bcrypt against the dummy hash, return False
      * real PIN               → bcrypt against the stored hash

    Note the third case. Unlike passwords there is no legacy SHA-256 tier to
    support (device_pin_hash is a new column that only ever receives bcrypt), so
    a non-bcrypt value is corruption and must fail — but it must fail in bcrypt
    time, not instantly.

    Callers MUST run this BEFORE branching on lockout or device state, or the
    early return re-opens the oracle through a different door.
    """
    stored = hashed if (hashed and hashed.startswith("$2")) else None
    try:
        ok = bcrypt.checkpw(
            (pin or "").encode("utf-8"),
            stored.encode("utf-8") if stored else _DUMMY_BCRYPT_HASH,
        )
    except Exception:
        ok = False
    return bool(ok) and stored is not None


# ── Device credential helpers ────────────────────────────────────────────────


def generate_device_secret() -> str:
    """A 256-bit random device credential, shown to the administrator once."""
    return secrets.token_urlsafe(32)


def hash_device_secret(secret: str) -> str:
    """SHA-256 of the device secret — NOT bcrypt, on purpose.

    bcrypt exists to make LOW-entropy secrets expensive to guess. This secret is
    256 bits of `secrets.token_urlsafe`; no work factor makes it more unguessable.
    Paying ~200 ms of bcrypt here would buy nothing and would land on the hot
    path of the one interaction this whole feature exists to make fast — and it
    would do so on TOP of the bcrypt the PIN itself already costs. A plain digest
    also lets the lookup be a single UNIQUE-index probe instead of a table scan
    comparing every enrolled tablet.
    """
    return hashlib.sha256((secret or "").encode("utf-8")).hexdigest()


def extract_device_token(request: Request) -> str | None:
    """The tablet's device credential: HttpOnly cookie first, header as fallback.

    The cookie is the steady state (set by /api/auth/device/bind). The header is
    for the bind call itself — which is the one moment the raw secret is in the
    client's hands — and for machine clients that hold no cookie jar.
    """
    cookie = request.cookies.get(DEVICE_COOKIE)
    if cookie:
        return cookie.strip() or None
    # Starlette's Headers mapping is case-insensitive, so the lowercase key
    # matches whatever casing the client actually sent.
    header = request.headers.get(DEVICE_HEADER)
    if header:
        return header.strip() or None
    return None


def set_device_cookie(response: Response, request: Request, secret: str) -> None:
    response.set_cookie(
        DEVICE_COOKIE,
        secret,
        max_age=DEVICE_COOKIE_MAX_AGE,
        httponly=True,
        secure=cookie_secure(request),
        samesite="lax",
        path="/",
    )


def clear_device_cookie(response: Response) -> None:
    response.delete_cookie(DEVICE_COOKIE, path="/")


def load_active_device(secret: str):
    """The ACTIVE enrolled device for this secret, or None.

    BLOCKING. Call it from a plain `def` route (FastAPI threadpools those, which
    is how the PIN endpoints use it); from an `async def`, go through run_db.

    Fail-closed on every ambiguity: an unknown digest, a revoked tablet, or a
    database that has not yet run migration 12 all return None, and the caller
    refuses the PIN login. A missing table must not 500 the login path — during
    a partial upgrade the correct answer is "this device is not enrolled", not a
    stack trace on the ward's only tablet.
    """
    if not secret:
        return None
    try:
        with db_session() as db:
            return db.execute(
                "SELECT * FROM enrolled_devices WHERE secret_hash = ? AND revoked_at IS NULL",
                (hash_device_secret(secret),),
            ).fetchone()
    except Exception:
        return None


def create_token(
    user_id: str,
    role: str,
    token_version: int | None = None,
    *,
    device_id: str | None = None,
    auth_method: str | None = None,
    expires_in_minutes: int | None = None,
    facility_id: int | None = None,
) -> str:
    # Include a unique jti so tokens are individually identifiable, plus the
    # user's token_version so all outstanding sessions can be revoked at once by
    # bumping users.token_version (see get_current_user + POST /api/auth/logout).
    # NOTE: `role` is embedded for convenience only; get_current_user re-reads
    # the authoritative role from the users row, and require_permission uses
    # that DB-row role — so a stale token role is not trusted for authz.
    if token_version is None:
        # Read the current version so the freshly minted token is valid. Callers
        # that already hold the users row should pass it to avoid this query.
        with db_session() as db:
            row = db.execute("SELECT token_version FROM users WHERE id = ?", (user_id,)).fetchone()
        token_version = row["token_version"] if row and row["token_version"] is not None else 0
    now = datetime.now(UTC)
    lifetime = (
        timedelta(minutes=expires_in_minutes)
        if expires_in_minutes is not None
        else timedelta(hours=JWT_EXPIRY_HOURS)
    )
    payload = {
        "sub": user_id,
        "role": role,
        "tv": int(token_version),
        "jti": str(uuid.uuid4()),
        "exp": now + lifetime,
        "iat": now,
    }
    if device_id:
        # `did` binds this session to the tablet it was minted on: get_current_user
        # re-checks that the device is still enrolled on every request, so an
        # administrator revoking a lost tablet kills its live sessions instead of
        # waiting out the token. `amr` records HOW the person authenticated, so
        # the audit trail can tell a PIN unlock from a full password sign-in.
        payload["did"] = device_id
        payload["amr"] = auth_method or "pin"
    if facility_id is not None:
        # `fac` (multisite-19): the facility a VISITING session works at,
        # minted only by POST /api/auth/facility and by /refresh of such a
        # session. authenticate_session_token checks it on every request. A
        # token without it is a home session, as every earlier token was.
        payload["fac"] = int(facility_id)
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def decode_token(token: str) -> dict:
    try:
        # Orb supports no critical JWT header extensions. Libraries that
        # ignore `crit` must not silently accept a policy they do not implement.
        if jwt.get_unverified_header(token).get("crit"):
            raise jwt.InvalidTokenError("Unsupported critical header")
        return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired") from None
    except (jwt.InvalidTokenError, TypeError, ValueError, RecursionError, OverflowError):
        raise HTTPException(status_code=401, detail="Invalid token") from None


def _load_session_context(user_id: str, device_id: str | None) -> tuple[Any, Any, Any]:
    """(users row, enrolled-device row, patient portal verdict): the snapshot
    _load_session_snapshot reads, for a session with no visiting claim."""
    row, device, portal, _visiting = _load_session_snapshot(user_id, device_id, None)
    return row, device, portal


def _visiting_verdict(db: Any, row: Any, facility_id: int) -> dict[str, Any]:
    """{access, problem} for a visiting claim (multisite-19), judged on the
    snapshot's own connection: the live visiting row at that facility (or
    None) and facility_service.visiting_access_problem's code (None when the
    session may work there). A read that fails is a problem: fail closed."""
    from services import facility_service

    try:
        access = facility_service.visiting_access_on(db, row["id"], facility_id)
        problem = facility_service.visiting_access_problem(row, access, facility_id, db=db)
    except Exception:
        return {"access": None, "problem": "unreadable"}
    return {"access": access, "problem": problem}


def _load_session_snapshot(
    user_id: str, device_id: str | None, facility_id: int | None
) -> tuple[Any, Any, Any, dict[str, Any] | None]:
    """Blocking read of the authoritative users row, plus the enrolled-device row
    when the session is bound to a tablet. Always call via run_db().

    Module level, not a closure inside get_current_user: this runs on every
    authenticated request, so rebuilding the function object per call is pure
    waste on the hottest path in the process.

    ONE session, deliberately. get_current_user makes four decisions off this
    snapshot (exists / is_active / token_version / device-still-enrolled) and
    they must all be judged against the SAME one. Splitting the reads into
    separate run_db() hops would put an await between them, letting an admin
    deactivation, a logout-driven token_version bump, or a device revocation land
    in the gap — the request would then be validated against a half-old,
    half-new picture. There is no write here, so no read-modify-write hazard.

    `device_id` is None for ordinary password sessions, which is the overwhelming
    majority of traffic — those pay exactly the single query they always did.

    A patient login also gets its portal verdict here, in the same session
    (services/patient_portal_access, ruling R029: Bridge is for the admission
    only). Staff sessions pay nothing for it.

    A visiting session (`facility_id`, the token's 'fac' claim, multisite-19)
    also gets its access row's verdict here, in the same session, so a revoke
    and the row's checks are judged against the same picture as the users row
    (_visiting_verdict). A home session (None) reads nothing more.
    """
    with db_session() as db:
        row = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        device = None
        if device_id:
            try:
                device = db.execute(
                    "SELECT id, label, department, revoked_at FROM enrolled_devices WHERE id = ?",
                    (device_id,),
                ).fetchone()
            except Exception:
                # Table missing (pre-migration-12 database). Fail CLOSED: a token
                # claiming a device we cannot verify is not honoured.
                device = None
        portal = None
        if row is not None and row["role"] == "patient":
            from services.patient_portal_access import portal_access

            portal = portal_access(db, row["id"])
        visiting = None
        if facility_id is not None and row is not None:
            visiting = _visiting_verdict(db, row, facility_id)
        return row, device, portal, visiting


def _visiting_claim(payload: dict) -> int | None:
    """The token's 'fac' claim (multisite-19): None for a home session, else
    the facility id. Anything but a whole number from 1 up is a bad token."""
    if "fac" not in payload:
        return None
    value = payload["fac"]
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise HTTPException(status_code=401, detail="Invalid token payload")
    return value


def _visiting_session(user: dict, device_id: Any, visiting: dict[str, Any] | None) -> dict:
    """The session's user dict for a valid visiting claim, with the access
    row's department overlaid (facility_service.visiting_session_user). Any
    doubt ends the session: 401, so the app's re-sign-in path takes over. A
    shared tablet's PIN session never carries the claim."""
    from services import facility_service

    if device_id or not visiting or visiting["problem"] is not None or not visiting["access"]:
        raise HTTPException(status_code=401, detail=facility_service.VISITING_ENDED_MESSAGE)
    return facility_service.visiting_session_user(user, visiting["access"])


def authenticate_session_token(token: str) -> dict:
    """Authoritative HTTP/socket policy. Blocking; async callers use run_db."""
    payload = decode_token(token)
    user_id = payload.get("sub")
    if not user_id or user_id == "discharge_access":
        raise HTTPException(status_code=401, detail="Invalid token payload")
    device_id = payload.get("did")
    facility_claim = _visiting_claim(payload)
    row, device_row, portal, visiting = _load_session_snapshot(user_id, device_id, facility_claim)
    if not row:
        raise HTTPException(status_code=401, detail="User not found")

    user = dict(row)

    # Device binding. A session minted by a PIN on an enrolled tablet is only
    # valid while that tablet is still enrolled — so an administrator revoking a
    # lost or stolen device severs its live sessions at the next request, rather
    # than leaving them alive until the token expires. 401 (not 403) so the SPA's
    # existing re-auth path handles it.
    #
    # WHAT THIS DELIBERATELY DOES NOT DO: it checks that the device is still
    # ENROLLED, not that the request re-presents the device credential. Requiring
    # the credential on every call would be strictly stronger on paper, but the
    # realistic way a PIN session is stolen is physical possession of the tablet
    # — and whoever holds the tablet holds both cookies, so the extra check buys
    # nothing against the actual threat. What it WOULD buy is a new failure mode:
    # a browser evicting the device cookie while keeping the session one would
    # strand a ward mid-shift needing an administrator's setup code. The
    # 15-minute session lifetime is what bounds a leaked token instead.
    if device_id:
        if not device_row or device_row["revoked_at"]:
            raise HTTPException(
                status_code=401,
                detail="This device is no longer enrolled; sign in again",
            )
        # Carried so callers can put the DEVICE in audit metadata. The ACTOR is
        # always user["id"] — the person — and never this.
        user["device_id"] = device_id
        user["auth_method"] = payload.get("amr") or "pin"

    # Deactivated accounts are rejected on EVERY request, not just at login — an
    # admin deactivating a terminated clinician must sever their access
    # immediately, even on a token that was already minted.
    if "is_active" in user and user["is_active"] is not None and not user["is_active"]:
        raise HTTPException(status_code=403, detail="Account deactivated")

    # Session revocation: a token is only valid while its embedded token_version
    # matches the user's current token_version. POST /api/auth/logout (and
    # password/MFA changes) bump the DB value, instantly invalidating every
    # previously-issued token for that user without a server-side blocklist.
    token_tv = payload.get("tv", 0)
    current_tv = user.get("token_version") or 0
    try:
        matches = int(token_tv) == int(current_tv)
    except (ValueError, TypeError):
        matches = False
    if not matches:
        raise HTTPException(status_code=401, detail="Session revoked; please log in again")

    # A visiting session (claim 'fac', multisite-19, ruling MS-R7 (a)) works
    # at another facility of the group through a live visiting row, checked
    # here on every request: the row's department stands in for the account's
    # own, so every scope helper answers for that facility. A row revoked, a
    # department moved, the switch turned off: the next request is a 401.
    if facility_claim is not None:
        user = _visiting_session(user, device_id, visiting)

    # A patient's portal closes at the end of their discharge day (ruling R029).
    # 401, not 403: the session is over, so the app's session-expiry path takes
    # the patient back to sign-in, which then says why (auth_routes.login).
    if portal is not None and not portal["open"]:
        raise HTTPException(status_code=401, detail=portal["reason"])

    return user


async def get_current_user(request: Request) -> dict:
    """Dependency: extract and validate current user from the JWT, taken from the
    Authorization header OR the HttpOnly session cookie."""
    token, source = _extract_token(request)
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")

    # CSRF: a browser auto-sends the session cookie, so a cookie-authenticated
    # state-changing request must additionally carry a valid double-submit token.
    # Bearer auth (an explicit header a cross-site attacker can't forge) and safe
    # methods are exempt. Enforced here — the single dependency every session-
    # protected route passes through — so it never falsely blocks public or
    # alt-auth endpoints that don't authenticate via the session cookie.
    if source == "cookie" and request.method in UNSAFE_METHODS and not csrf_token_valid(request):
        raise HTTPException(status_code=403, detail="CSRF token missing or invalid")

    return await run_db(authenticate_session_token, token)


def require_role(*roles: str):
    """Dependency factory: restrict endpoint to specific roles.

    DEPRECATED: prefer permissions.require_permission, which consults the
    central ROLE_PERMISSIONS map and audit-logs denials. require_role bypasses
    both and is kept only for backward compatibility with existing routes.
    Do not protect new endpoints with this helper.

    Behaviour note: the role is read from the user dict returned by
    get_current_user, which is the authoritative DB row (not the JWT payload),
    so a stale token role cannot widen access here.
    """

    _CHECKER_USER_DEFAULT = Depends(get_current_user)

    async def checker(user: dict = _CHECKER_USER_DEFAULT):
        if user.get("role") not in roles:
            raise HTTPException(status_code=403, detail="Insufficient permissions")
        return user

    return checker
