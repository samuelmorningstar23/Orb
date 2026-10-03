"""Shared test fixtures for the Orb server test suite."""
import os
import sys
import tempfile

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
        client = getattr(mod, "client", None)
        if isinstance(client, TestClient):
            try:
                client.cookies.clear()
            except Exception:
                pass


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
