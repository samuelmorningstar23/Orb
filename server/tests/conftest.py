"""Shared test fixtures for the Orb server test suite."""
import os
import sys
import tempfile
from types import ModuleType

# Add server root to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# ---------------------------------------------------------------------------
# Test-DB isolation (finding: suite ran against the live orb.db, polluting/
# corrupting demo data and audit logs).
#
# database.py binds DB_PATH at import time via `from config import DB_PATH`,
# and config.py resolves it from the ORB_DB_PATH env var. So we MUST set
# ORB_DB_PATH to a throwaway path BEFORE config/database is first imported —
# which is here, at module load, before any `from database import ...` runs in
# the fixtures below or in the test modules pytest collects after this.
#
# We use a unique temp file per session (not ':memory:') because the app opens
# multiple short-lived sqlite3 connections via get_db(); an in-memory DB is not
# shared across connections, so a file-backed temp DB is required for the
# schema/seed to be visible to every connection.
# ---------------------------------------------------------------------------
if not os.environ.get("ORB_DB_PATH"):
    _tmp = tempfile.NamedTemporaryFile(prefix="orb_test_", suffix=".db", delete=False)
    _tmp.close()
    os.environ["ORB_DB_PATH"] = _tmp.name

# The suite exercises the embedder/reranker logic in-process (test_reranker,
# test_retrieval_*), not through the knowledge worker subprocess — spawning a
# 2 GB model child per test session would be both slow and meaningless for
# these assertions. Individual tests can override to exercise the bridge.
os.environ.setdefault("ORB_KNOWLEDGE_INPROCESS", "1")

# Every TestClient boots the real lifespan, which arms the loop-starvation
# watchdog — so without this the suite would litter server/ops/watchdog with a
# status.json and a dead-man file per run. Point it at a throwaway dir, same
# reasoning as ORB_DB_PATH above. tests/test_loop_watchdog.py overrides this per
# scenario, so the watchdog itself is still exercised for real.
if not os.environ.get("ORB_WATCHDOG_DIR"):
    os.environ["ORB_WATCHDOG_DIR"] = tempfile.mkdtemp(prefix="orb_test_watchdog_")

# Same problem, worse payload. The nightly FHIR self-export
# (services/downtime_service.fhir_self_export_dir) defaults to
# server/downtime_exports — so every run that did not override it wrote whole
# patient $everything bundles into the CHECKOUT, and never swept them: 390
# files / 339 MB had accumulated by 2026-08-08, ~30 per run. The filenames
# alone are PHI (they encode patient ids), which is why the service chmods the
# directory 0o700 — piling it up untracked in a source tree undoes that.
#
# It is also the last shared non-temp path the harness left unisolated: every
# concurrent pytest process wrote to the same directory AND to the same
# .last_fhir_export throttle marker, so one run could silently suppress
# another's export. That has already caused cross-agent stalls.
#
# Same shape as the two above: set it before anything imports downtime_service,
# and only when the caller has not chosen a path. Individual tests still
# monkeypatch.setenv over it (test_downtime_fhir_export.py, test_tier1_small_
# closes.py), because the resolver reads the var at call time.
if not os.environ.get("ORB_FHIR_EXPORT_DIR"):
    os.environ["ORB_FHIR_EXPORT_DIR"] = tempfile.mkdtemp(prefix="orb_test_fhir_export_")

import pytest


@pytest.fixture(autouse=True)
def _isolate_session_cookies():
    """Clear cookies on every module-level TestClient after each test.

    Deferred A gave /api/auth/login an HttpOnly session cookie. httpx's TestClient
    persists cookies in a jar, so a login in one test would otherwise authenticate
    a logically-*unauthenticated* request in a later test (→ false 200s), and the
    CSRF middleware would 403 cookie-present POSTs to alt-auth endpoints. In
    production each browser is its own session, so this only isolates the shared
    test client. Runs AFTER the test (yield first) so within-test cookie flows —
    the cookie-auth / CSRF tests — still work."""
    yield
    from starlette.testclient import TestClient
    for mod in list(sys.modules.values()):
        # Inspect declared globals only. Optional SDKs use module __getattr__
        # for lazy imports; probing a missing "client" can load unrelated vision
        # modules (or raise) during every test teardown.
        client = vars(mod).get("client") if isinstance(mod, ModuleType) else None
        if isinstance(client, TestClient):
            try:
                client.cookies.clear()
            except Exception:
                pass


@pytest.fixture(autouse=True)
def _isolate_discharge_verify_throttle():
    """Give every test a full per-IP budget on /api/discharge-access/verify.

    THE FAILURE (found 2026-10-04). services/discharge_access_service keeps an
    in-process sliding-window throttle: _VERIFY_IP_MAX_ATTEMPTS (15) verify
    calls per IP per window, recorded on EVERY call. Every TestClient request
    comes from the one address 'testclient' and nothing reset the dict, so the
    whole session shared a single budget. test_discharge_access.py spends 14
    and test_discharge_review.py needs 1, so one extra verify call anywhere in
    between got the review test throttled (KeyError: 'access_grant'). The suite
    passed only because it sat exactly on the edge.

    Cleared before AND after each test, under the service's own lock. A test
    still meets the real throttle and can exhaust it on its own (proved by
    test_discharge_access.py::test_the_per_ip_budget_throttles_verify_within_one_test),
    but nothing it spends reaches the next test. Production behaviour is
    untouched: this only forgets what the shared test address spent.

    Looked up in sys.modules, not imported: a test that never loads the
    discharge service has no budget to leak, and importing it here would pull
    database and the app's services into tests that deliberately avoid them."""
    def _clear():
        das = sys.modules.get("services.discharge_access_service")
        if das is not None:
            with das._VERIFY_IP_LOCK:
                das._VERIFY_IP_ATTEMPTS.clear()

    _clear()
    yield
    _clear()


@pytest.fixture(autouse=True)
def _isolate_login_ip_limiter():
    """Give every test an empty per-IP failed sign-in record.

    THE FAILURE (found 2026-10-04). routes/auth_routes keeps LOGIN_ATTEMPTS in
    process: MAX_LOGIN_ATTEMPTS (5) failed sign-ins from one IP inside
    LOCKOUT_DURATION_SECONDS lock that IP out, and the check runs BEFORE the
    password is verified. Every TestClient request comes from 'testclient'. A
    successful login clears the IP, which is why the suite got away with it,
    but failures from different tests add up: the three service-account
    reset-password tests in test_admin_users_guards.py leave 3, test_audit.py's
    test_login_failure leaves 1, and one more locked 'testclient' out. A
    correct password is then refused too, so nothing clears it, and every later
    login in the session got 429 for 15 minutes.

    Same shape as _isolate_discharge_verify_throttle above: cleared before AND
    after each test, found via sys.modules. A test still meets the real limiter
    and can exhaust it on its own (test_login_ip_limiter.py proves it), and the
    tests that clear it on purpose mid-test, to stand in for a fresh source IP,
    work as before."""
    def _clear():
        auth_routes = sys.modules.get("routes.auth_routes")
        if auth_routes is not None:
            auth_routes.LOGIN_ATTEMPTS.clear()

    _clear()
    yield
    _clear()


@pytest.fixture(autouse=True)
def _isolate_clinician_pin_limiter():
    """Give every test an empty clinician PIN failure record.

    services/pin_security keeps PIN_ATTEMPTS in process: MAX_PIN_ATTEMPTS (5)
    wrong clinician PINs on a Relay agentic confirmation inside
    LOCKOUT_DURATION_SECONDS lock that user out, checked before the PIN. It is
    keyed by user id, not IP, so it leaks only between tests that share a user,
    and a locked user's correct PIN is refused too, so nothing clears it for 15
    minutes. Nothing failed from it as of 2026-10-04: the one test that leaves a
    lock (test_optimized_agentic_orders.py::test_wrong_pin_five_times_locks_out,
    D-PRIMARY-DOC) is followed only by tests that pull in that module's own
    autouse fixture, which clears it. A later test confirming as that doctor
    without that fixture, or one locking a shared seeded user such as D001,
    would have failed in every test after it.

    Same shape as the two fixtures above: cleared before AND after each test,
    found via sys.modules. A test still meets the real limiter and can exhaust
    it on its own (test_wrong_pin_five_times_locks_out, and
    test_clinician_pin_limiter.py, which also proves the boundary)."""
    def _clear():
        pin_security = sys.modules.get("services.pin_security")
        if pin_security is not None:
            pin_security.PIN_ATTEMPTS.clear()

    _clear()
    yield
    _clear()


@pytest.fixture(autouse=True)
def _reset_async_singletons():
    """Reset lazily-bound asyncio singletons before EVERY test.

    llm_queue (Lock/PriorityQueue), the shared GPU gate, umsq and the ambient
    batch coordinator are process-wide singletons whose asyncio primitives are
    created lazily on whichever event loop first touches them. pytest-asyncio
    gives every test a FRESH loop, so state left by an earlier test (a bound
    lock, a queued waiter event from a now-dead loop, a held gate) can wedge a
    later test's unbounded `async with lock:` / `await fut` forever — the suite
    then hangs at 0% CPU (seen at test_batch_coordinator after ~100 tests).
    Production runs a single uvicorn loop, so this is purely test-environment
    contamination; a pre-test reset gives each test virgin primitives, which is
    exactly what individual tests were already doing ad-hoc with
    `q.llm_queue.active_workers = 0`.
    """
    import services.llm_queue_service as _q
    import services.gpu_coordinator as _g
    import services.batch_coordinator as _b
    _q.llm_queue._lock = None
    _q.llm_queue._queue = None
    _q.llm_queue.counter = 0
    _q.llm_queue.active_workers = 0
    _q.llm_queue._gate_token = None
    _g._gpu_gate = None
    _g._gpu_gate_owner = None
    _g.umsq._queue = None
    _g.umsq._guard_lock = None
    _g.umsq._counter = 0
    _g.umsq._active = False
    _g.umsq._active_task = ""
    _b._ambient_coordinator = None
    yield


@pytest.fixture(autouse=True, scope="module")
def _sweep_admitted_leftovers():
    """Discharge any `status='admitted'` patient a test module leaves behind.

    THE FAILURE THIS EXISTS TO STOP (2026-08-15). Synthetic admitted patients
    are not an inert leak, because something else in the suite acts on ALL of
    them: `bot_service._generate_vitals` appends a reading to every admitted
    patient on a simulation tick. A leftover seeded with one vitals row grows a
    trend it was never meant to have, starts counting as `worsening` in
    `world_model_service.compute_ward_state`, and carries whatever risk level
    its seed row named into a hospital-wide aggregate. Leftovers are the fuel;
    a periodic job is the spark. That combination failed a test in a module the
    polluting change never touched, five separate times, and five modules grew
    their own cleanup fixture in response.

    MODULE scope is the point, and is why this is not a session-scoped sweep. A
    session-end sweep would tidy the DB after every aggregate has already been
    read, fixing nothing. Per-TEST would be worse than nothing: module-scoped
    fixtures legitimately create a patient in the first test and use it in the
    tenth. The module boundary is exactly the line a fixture must not cross.

    Autouse fixtures declared here are set up before a test module's own, so
    this one tears down LAST — after that module's fixtures have run, which is
    what lets it see what they failed to clean up.

    Baseline `init_db` seed patients are captured in the pre-snapshot on every
    module and are therefore never touched; only rows that appeared while the
    module ran are swept. Discharge rather than DELETE: vitals, channel and
    audit rows written during the test legitimately reference these ids.

    Set ORB_TEST_SWEEP_REPORT=1 to print what each module leaked — that is the
    cheap version of the bespoke pytest plugin it took to identify the
    polluters by hand.
    """
    from database import db_session
    with db_session() as db:
        before = {r["id"] for r in db.execute(
            "SELECT id FROM patients WHERE status = 'admitted'"
        ).fetchall()}
    yield
    try:
        with db_session() as db:
            leaked = [r["id"] for r in db.execute(
                "SELECT id FROM patients WHERE status = 'admitted'"
            ).fetchall() if r["id"] not in before]
            if leaked:
                db.executemany(
                    "UPDATE patients SET status = 'discharged' WHERE id = ?",
                    [(p,) for p in leaked],
                )
        if leaked and os.environ.get("ORB_TEST_SWEEP_REPORT"):
            print(f"\n[sweep] {len(leaked)} admitted patient(s) left behind: "
                  f"{leaked[:10]}{' …' if len(leaked) > 10 else ''}")
    except Exception as e:
        # Housekeeping must never be the reason a suite goes red.
        print(f"[sweep] failed: {e!r}")


_SEED_USERS_HASH = None


@pytest.fixture(autouse=True)
def _reseed_users():
    """Re-ensure baseline seed users before EVERY test. Some tests wipe the `users`
    table for their own isolation; without this, later tests' author FK constraints
    (e.g. scribe_notes.author_id) would fail. Hash is computed once and cached."""
    global _SEED_USERS_HASH
    from database import get_db
    if _SEED_USERS_HASH is None:
        from auth import hash_password
        _SEED_USERS_HASH = hash_password("password")
    rows = [(u, n, r, "General Medicine", _SEED_USERS_HASH) for (u, n, r) in (
        ("D001", "Dr. Test", "doctor"), ("D002", "Dr. Test2", "doctor"),
        ("N001", "Nurse Test", "nurse"), ("DOC001", "Dr. Legacy", "doctor"),
    )]
    db = get_db()
    try:
        db.executemany(
            "INSERT OR IGNORE INTO users (id, name, role, department, password_hash) VALUES (?, ?, ?, ?, ?)",
            rows,
        )
        # The 'system' row too, exactly as database.init_db() seeds it. It is the
        # sender of every system message (messages.sender_id REFERENCES users),
        # including the one admit_patient() writes into the new case room, so a
        # test that wipes `users` used to leave every later admission through the
        # route failing with "FOREIGN KEY constraint failed" (first seen 2026-09-05
        # in tests/test_patient_name_rule.py, which passed alone and failed after
        # any of several earlier wipers). Nothing but init_db() recreates it.
        db.execute(
            "INSERT OR IGNORE INTO users (id, name, role, department, password_hash) "
            "VALUES ('system', 'Orb Sage', 'admin', NULL, 'system_hash')"
        )
        db.commit()
    finally:
        db.close()
    yield


@pytest.fixture(autouse=True, scope="session")
def init_test_db():
    """Initialize a fresh, isolated test database once per session with seed data."""
    from database import init_db, get_db
    from config import DB_PATH

    # Safety net: never run the destructive/seed-writing suite against the live
    # demo DB even if ORB_DB_PATH was misconfigured to point at it.
    assert "orb_test_" in os.path.basename(str(DB_PATH)) or str(DB_PATH) == ":memory:", (
        f"Refusing to run tests against non-test DB at {DB_PATH}. "
        "Set ORB_DB_PATH to a temp/test database."
    )

    init_db()

    # Seed test users so FK constraints on author_id pass. Use a parameterized
    # executemany (matching test_rbac.py:_create_user) rather than f-string SQL —
    # the password hash could contain a quote and string-built SQL is a bad
    # pattern to normalize in a PHI codebase.
    db = get_db()
    try:
        from auth import hash_password
        hashed_pwd = hash_password("password")
        seed_users = [
            ("D001", "Dr. Test", "doctor", "General Medicine", hashed_pwd),
            ("D002", "Dr. Test2", "doctor", "General Medicine", hashed_pwd),
            ("N001", "Nurse Test", "nurse", "General Medicine", hashed_pwd),
            ("DOC001", "Dr. Legacy", "doctor", "General Medicine", hashed_pwd),
        ]
        db.executemany(
            "INSERT OR IGNORE INTO users (id, name, role, department, password_hash) "
            "VALUES (?, ?, ?, ?, ?)",
            seed_users,
        )
        db.commit()
    finally:
        db.close()

    yield

    # Best-effort cleanup of the throwaway DB file we created.
    try:
        db_file = str(DB_PATH)
        if "orb_test_" in os.path.basename(db_file) and os.path.exists(db_file):
            os.unlink(db_file)
    except OSError:
        pass


@pytest.fixture(scope="module")
def isolated_module_database(tmp_path_factory):
    """An opt-in fresh census for tests that require an absent patient.

    The session database intentionally retains discharged charts for audit
    references. A resolver also searches those charts, so changing a name or
    bed in a test cannot establish absence from that accumulated database.
    Keep the real schema and resolver, with a database owned by this module.
    """
    import config
    import database
    from db_backend import discard_pooled

    path = tmp_path_factory.mktemp("orb_test_module") / "orb_test_module.db"
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("ORB_DB_PATH", str(path))
        patch.setattr(config, "DB_PATH", path)
        patch.setattr(database, "DB_PATH", path)
        database.init_db()
        try:
            yield path
        finally:
            discard_pooled(database.get_db())


# ---------------------------------------------------------------------------
# The real knowledge worker pool must never be spawned by a test. Each spawn is
# a ~1.8 GB model child, and a pytest run that is SIGKILLed mid-session leaves
# that child to its own parent-watchdog. The suite default above
# (ORB_KNOWLEDGE_INPROCESS=1) keeps callers on the in-process path, but tests
# that drop the variable to exercise the bridge (test_knowledge_worker's
# `isolated` fixture) can still reach knowledge_worker._submit, and an
# inherited ORB_KNOWLEDGE_INPROCESS=0 in the shell flips every retrieval onto
# it. So the pool factory is stubbed: those paths take the documented degraded
# route (KnowledgeWorkerError -> lexical fallback) instead of a spawn. A test
# that genuinely needs the real pool opts in with
# @pytest.mark.real_knowledge_pool, and the session finalizer reaps whatever
# such a test started so a normal exit never waits on a model load.
# ---------------------------------------------------------------------------
def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "real_knowledge_pool: the test may spawn the real knowledge worker pool "
        "(skips the autouse stub on knowledge_worker._ensure_pool).",
    )


@pytest.fixture(autouse=True)
def _no_real_knowledge_pool(request):
    # Deliberately not via the monkeypatch fixture: an autouse conftest fixture
    # that depends on monkeypatch instantiates it first for EVERY test, which
    # flips the teardown order a module-level autouse fixture may rely on
    # (a fixture that asserts the environment is back to its snapshot must tear
    # down after monkeypatch's undo; with the dependency it tore down before,
    # and every test in its module errored).
    if request.node.get_closest_marker("real_knowledge_pool"):
        yield
        return
    from services import knowledge_worker as kw

    def _refuse():
        raise kw.KnowledgeWorkerError(
            "the knowledge worker pool is stubbed out in the test suite; mark the "
            "test @pytest.mark.real_knowledge_pool to spawn the real worker"
        )

    original = kw._ensure_pool
    kw._ensure_pool = _refuse
    try:
        yield
    finally:
        kw._ensure_pool = original


@pytest.fixture(autouse=True, scope="session")
def _reap_knowledge_pool():
    yield
    from services import knowledge_worker as kw
    kw._shutdown_pool()
