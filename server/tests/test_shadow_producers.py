"""Orb — the shadow instrument's PRODUCERS, and the loop that materialises the negatives.

Two defects this file exists to make un-repeatable:

DEFECT 3 — the sweeper was never started. `shadow_service.sweep_loop` is the ONLY
producer of `episode.closed`, `alert.expired` and `action.abandoned`. Its
docstring said "Registered from main.py's lifespan"; nothing registered it, and
4487 live events contained zero negatives. A docstring is what let that pass
review, so this file asserts the REGISTRATION ITSELF, structurally, out of
main.py's parse tree — a comment cannot satisfy it.

DEFECT 4 — eleven of the twenty-three kinds in the controlled vocabulary had no
emitter anywhere in the app. They existed only as PAYLOAD_KEYS declarations, so
`agreement` and `time_saved` could never leave `insufficient_data` and, once the
sweeper ran, every alert would have been swept to `alert.expired` and engagement
would have read a structural 0%. `test_every_kind_has_a_producer` walks the
server tree and fails the moment any kind loses its last write path.

The producer scan is deliberately EVIDENCE-BASED rather than name-based: it looks
for a kind string used as a call argument or as a `{"kind": ...}` entry, in
non-test code, EXCLUDING the two read-side modules (scorecard, export). A module
that merely mentions a kind in prose does not count, and the consumers cannot
vouch for their own inputs.
"""
import ast
import contextlib
import inspect
import os
from datetime import datetime, timedelta, timezone

import pytest

import migrations
from database import db_session, init_db
from services import shadow_service

SERVER_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: Read-side modules. They consume `shadow_events`; letting them count as
#: producers would mean a metric could vouch for the existence of its own input.
CONSUMER_MODULES = {
    os.path.join("services", "shadow_scorecard_service.py"),
    os.path.join("services", "shadow_export_service.py"),
}

#: Directories that are not the running application. Runtime corpora, datasets,
#: uploads and operational output can contain millions of non-source entries;
#: do not traverse them when inventorying application emitters. Keep models,
#: adapters, migrations, services and every other application source directory.
EXCLUDED_DIRS = {
    "tests", "scripts", "__pycache__", ".venv", "venv", "migrations_data",
    "corpus", "datasets", "uploads", "downtime_exports", "ops",
}

# The retrospective evaluation harness is source; its generated predictions
# and vendored external scorer are not application event producers.
EXCLUDED_PATHS = {
    os.path.join("eval", "vigil_retro", "out"),
    os.path.join("eval", "vigil_retro", "vendor"),
}


# ══════════════════════════════════════════════════════════════════════════════
#  DEFECT 3 — the sweeper is registered
# ══════════════════════════════════════════════════════════════════════════════
def _lifespan_ast():
    with open(os.path.join(SERVER_ROOT, "main.py"), "r", encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), filename="main.py")
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "lifespan":
            return node
    raise AssertionError("main.py has no `lifespan` coroutine")


def _sweep_loop_spawns(lifespan_node):
    """Every `_spawn(<...>sweep_loop(...))` inside the lifespan."""
    found = []
    for node in ast.walk(lifespan_node):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "_spawn"):
            continue
        for arg in node.args:
            if not isinstance(arg, ast.Call):
                continue
            target = arg.func
            name = (target.attr if isinstance(target, ast.Attribute)
                    else getattr(target, "id", ""))
            if name.endswith("sweep_loop"):
                found.append(node)
    return found


def test_shadow_sweeper_is_registered_in_lifespan():
    """The sweeper is STARTED, not merely defined.

    Asserted against main.py's parse tree rather than against a running app: the
    failure being guarded is "the function exists, the docstring claims it is
    registered, and no call site exists", which no amount of prose can fix and
    which booting the whole appliance in a unit test would not localise.
    """
    spawns = _sweep_loop_spawns(_lifespan_ast())
    assert spawns, (
        "shadow_service.sweep_loop is NOT spawned from main.py's lifespan. "
        "Without it nothing in Orb ever writes episode.closed, alert.expired or "
        "action.abandoned — the scorecard would score only the successes."
    )


def test_shadow_sweeper_task_is_strongly_referenced():
    """The spawned task is appended to `lifespan_tasks`.

    The loop keeps only a WEAK reference to a task (see main.py's own comment), so
    a `_spawn(...)` whose result is dropped can be garbage-collected mid-flight and
    the sweeper would stop silently — no exception, no log line, and the negatives
    would quietly stop appearing again.
    """
    lifespan_node = _lifespan_ast()
    spawns = _sweep_loop_spawns(lifespan_node)
    assert spawns, "sweep_loop is not spawned at all (see the previous test)"

    retained = False
    for node in ast.walk(lifespan_node):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "append"
                and any(arg in spawns for arg in node.args)):
            retained = True
    assert retained, (
        "sweep_loop's task is spawned but never appended to lifespan_tasks — it can "
        "be garbage-collected mid-run and it is never cancelled on shutdown."
    )


def test_sweep_loop_is_awaitable_and_calls_sweep_once():
    """The registered symbol is a coroutine function that drives `sweep_once`."""
    assert inspect.iscoroutinefunction(shadow_service.sweep_loop)
    source = inspect.getsource(shadow_service.sweep_loop)
    assert "sweep_once" in source


# ══════════════════════════════════════════════════════════════════════════════
#  DEFECT 3 — and the loop, once running, materialises all three negatives
# ══════════════════════════════════════════════════════════════════════════════
@pytest.fixture(scope="session", autouse=True)
def _schema():
    init_db()


@pytest.fixture
def _fresh_instrument(monkeypatch):
    monkeypatch.setenv("ORB_SHADOW_ENABLED", "true")
    for key in list(shadow_service.STATS):
        monkeypatch.setitem(shadow_service.STATS, key, 0)
    shadow_service.reset_registry()
    with db_session() as conn:
        conn.execute("DROP VIEW IF EXISTS shadow_events_current")
        conn.execute("DROP TABLE IF EXISTS shadow_events")
        migrations._m4_shadow_events(conn)
        migrations._m18_shadow_none_confirmed_once(conn)
    yield
    with db_session() as conn:
        conn.execute("DROP VIEW IF EXISTS shadow_events_current")
        conn.execute("DROP TABLE IF EXISTS shadow_events")
        migrations._m4_shadow_events(conn)
        migrations._m18_shadow_none_confirmed_once(conn)


def _kinds_in_db():
    with db_session() as conn:
        return sorted(r["kind"] for r in conn.execute(
            "SELECT kind FROM shadow_events_current").fetchall())


def test_sweep_once_materialises_every_negative(_fresh_instrument):
    """One stale episode with an unopened alert and an unanswered proposal must
    produce alert.expired + action.abandoned + episode.closed."""
    now = datetime(2026, 7, 25, 12, 0, tzinfo=timezone.utc)
    stale = now - timedelta(hours=9)          # past the 240-minute window
    iso = stale.isoformat(timespec="milliseconds")
    episode = shadow_service.new_episode_id()
    with db_session() as conn:
        for kind, kwargs in (
            ("episode.opened", {}),
            ("alert.persisted", {"subject_type": "messages", "subject_id": 991,
                                 "payload": {"message_id": 991, "channel_id": "c1"}}),
            ("action.proposed", {"payload": {"action_class": "add_medication"}}),
        ):
            assert shadow_service.emit(kind, episode, "chat", occurred_at=iso,
                                       patient_id="SWP1", ward="Ward SWP",
                                       db=conn, **kwargs) is not None

    counts = shadow_service.sweep_once(now=now)
    assert counts == {"closed": 1, "expired": 1, "abandoned": 1, "ok": True}, counts
    kinds = _kinds_in_db()
    for negative in ("alert.expired", "action.abandoned", "episode.closed"):
        assert negative in kinds, f"{negative} was not materialised: {kinds}"


def test_alert_viewed_suppresses_the_expiry(_fresh_instrument):
    """The discriminator the whole engagement number rests on.

    Before defect 4 was fixed this test would have been vacuous in production:
    `alert.viewed` had no emitter, so the sweeper marked 100% of alerts expired
    and engagement read a structural 0%.
    """
    now = datetime(2026, 7, 25, 12, 0, tzinfo=timezone.utc)
    iso = (now - timedelta(hours=9)).isoformat(timespec="milliseconds")
    episode = shadow_service.new_episode_id()
    with db_session() as conn:
        shadow_service.emit("episode.opened", episode, "chat", occurred_at=iso,
                            patient_id="SWP2", db=conn)
        shadow_service.emit("alert.persisted", episode, "chat", occurred_at=iso,
                            patient_id="SWP2", subject_type="messages", subject_id=992,
                            payload={"message_id": 992, "channel_id": "c1"}, db=conn)
        shadow_service.emit("alert.viewed", episode, "chat", occurred_at=iso,
                            patient_id="SWP2", subject_type="messages", subject_id=992,
                            payload={"message_id": 992, "channel_id": "c1"}, db=conn)

    counts = shadow_service.sweep_once(now=now)
    assert counts["expired"] == 0, "a viewed alert must not be swept to alert.expired"
    assert "alert.expired" not in _kinds_in_db()


# ══════════════════════════════════════════════════════════════════════════════
#  DEFECT 5 — a failing sweep is LOUD  (prove-can-fail)
# ══════════════════════════════════════════════════════════════════════════════
#
# The sweeper being registered (above) is only half the guarantee. It ran, it
# failed, and it said so at `logger.debug` — a level nothing in the field records
# — so the negatives stopped appearing and the scorecard went on reporting. And
# it does not report an ERROR when that happens: it reports SUCCESS. Every alert
# looks engaged with, every proposal looks actioned, because the rows that say
# otherwise are the ones that stopped being written.
#
# So: a failed cycle logs at error, increments a counter that rides to the
# scorecard on `STATS`, and leaves a state `sweeper_health()` can be asked about.

@pytest.fixture
def _sweeper_state(monkeypatch):
    """Isolate the module-level sweeper counters for one test."""
    for key in list(shadow_service.STATS):
        monkeypatch.setitem(shadow_service.STATS, key, 0)
    original = dict(shadow_service._SWEEPER)
    shadow_service._SWEEPER.update({
        "started": False, "cycles": 0, "failures": 0, "consecutive_failures": 0,
        "last_attempt_at": None, "last_success_at": None, "last_error": None,
    })
    yield
    shadow_service._SWEEPER.update(original)


@contextlib.contextmanager
def _sweep_broken(exc=None):
    """Make the sweep's very first DB call fail, the way a locked, corrupt or
    absent `shadow_events_current` actually would.

    Not `monkeypatch.setattr`: these tests need the DB back MID-TEST to prove a
    recovery, and `monkeypatch.undo()` would also undo the STATS isolation the
    fixture set up.
    """
    real = shadow_service.db_session

    def _boom(*args, **kwargs):
        raise exc or RuntimeError("no such view: shadow_events_current")

    shadow_service.db_session = _boom
    try:
        yield
    finally:
        shadow_service.db_session = real


def test_a_failed_sweep_is_logged_at_error_and_counted(_sweeper_state, caplog):
    """THE INSTRUMENT GOING DEAD MUST NEVER BE QUIET.

    PROVEN ABLE TO FAIL: put `logger.debug`/`logger.warning` back in
    `shadow_service.sweep_once`'s except branch (and drop the `_record_sweep`
    call) and this goes red on both halves — nothing at ERROR, and
    `sweep_consecutive_failures` stuck at 0 while the negatives are not being
    written. Verified by doing exactly that and restoring it.
    """
    with caplog.at_level("ERROR", logger="orb.shadow"), _sweep_broken():
        counts = shadow_service.sweep_once()

    # 1. The caller can tell "nothing to sweep" from "the sweep blew up".
    assert counts["ok"] is False, counts
    assert counts["closed"] == 0 and counts["expired"] == 0 and counts["abandoned"] == 0

    # 2. It is LOUD. Not debug, not warning.
    errors = [r for r in caplog.records if r.levelname == "ERROR"]
    assert errors, "a failed sweep cycle produced no ERROR log record"
    assert any("sweep" in r.getMessage().lower() for r in errors), \
        [r.getMessage() for r in errors]

    # 3. It is COUNTED, on the dict the scorecard's instrument block copies.
    assert shadow_service.STATS["sweep_failures"] == 1
    assert shadow_service.STATS["sweep_consecutive_failures"] == 1

    # 4. And it is READABLE as a state, not just a log line that has scrolled.
    health = shadow_service.sweeper_health()
    assert health["healthy"] is False
    assert health["consecutive_failures"] == 1
    assert "shadow_events_current" in (health["last_error"] or "")
    assert "negatives" in health["warning"]


def test_consecutive_failures_accumulate_and_a_good_cycle_clears_them(
        _sweeper_state, _fresh_instrument):
    """One bad cycle is an incident; three in a row is a dead instrument. The
    counter has to distinguish them, and a recovery has to be believable."""
    with _sweep_broken():
        for expected in (1, 2, 3):
            shadow_service.sweep_once()
            assert shadow_service.STATS["sweep_consecutive_failures"] == expected
        assert shadow_service.STATS["sweep_failures"] == 3
        assert shadow_service.sweeper_health()["healthy"] is False

    # The DB comes back.
    counts = shadow_service.sweep_once(now=datetime(2026, 7, 25, 12, 0, tzinfo=timezone.utc))
    assert counts["ok"] is True
    assert shadow_service.STATS["sweep_consecutive_failures"] == 0
    # The lifetime total is NOT reset — three failed cycles happened, and the
    # window they covered has no negatives in it for ever.
    assert shadow_service.STATS["sweep_failures"] == 3
    health = shadow_service.sweeper_health()
    assert health["healthy"] is True and health["last_success_at"]


def test_sweeper_health_is_visible_before_the_first_cycle(_sweeper_state):
    """A sweeper that has never run has produced no negatives either, and must
    not report itself healthy just because nothing has failed yet."""
    health = shadow_service.sweeper_health()
    assert health["healthy"] is False
    assert health["cycles"] == 0
    assert "not completed a cycle" in health["warning"]


def test_a_sweeper_that_never_started_is_recorded_not_just_logged(_sweeper_state, caplog):
    """main.py's lifespan calls this when the loop cannot be spawned at all.

    "Never started" and "started and failing" lose the same class of row, so they
    are reported through the same counter — a boot-time log line is not something
    anyone re-reads three weeks into a pilot.
    """
    with caplog.at_level("ERROR", logger="orb.shadow"):
        shadow_service.note_sweeper_unavailable("ImportError: boom")
    assert shadow_service.STATS["sweep_consecutive_failures"] == 1
    assert shadow_service.sweeper_health()["healthy"] is False
    assert any(r.levelname == "ERROR" for r in caplog.records)


def test_main_records_the_failure_when_the_sweeper_cannot_start():
    """The lifespan's except branch does more than log.

    Asserted against main.py's parse tree for the same reason the registration
    test is: the failure being guarded is "the sweeper is not running and nothing
    that survives the boot says so", which a comment cannot fix.
    """
    source = ast.dump(_lifespan_ast())
    assert "note_sweeper_unavailable" in source, (
        "main.py logs a failed sweeper start but records nothing an operator or "
        "the scorecard can read back later"
    )


def test_stats_carry_the_sweeper_to_the_scorecards_instrument_block(_sweeper_state):
    """The counters must reach a surface a human actually looks at.

    `shadow_scorecard_service._instrument` and `routes/shadow_routes._attach_stats`
    both copy `STATS` wholesale onto the instrument block, so this is the whole
    plumbing — asserted here so that a later refactor of STATS into private
    counters cannot silently take the sweeper off the scorecard.
    """
    with _sweep_broken():
        shadow_service.sweep_once()

    from services import shadow_scorecard_service
    health = shadow_scorecard_service.instrument_health()
    runtime = health.get("runtime") or {}
    assert runtime.get("sweep_consecutive_failures") == 1, runtime
    assert runtime.get("sweep_failures") == 1, runtime


# ══════════════════════════════════════════════════════════════════════════════
#  DEFECT 4 — every kind in the vocabulary has a write path
# ══════════════════════════════════════════════════════════════════════════════
def _app_modules():
    for dirpath, dirnames, filenames in os.walk(SERVER_ROOT):
        dirnames[:] = [
            d for d in dirnames
            if d not in EXCLUDED_DIRS and not d.startswith(".")
            and os.path.relpath(os.path.join(dirpath, d), SERVER_ROOT) not in EXCLUDED_PATHS
        ]
        for filename in filenames:
            if not filename.endswith(".py"):
                continue
            path = os.path.join(dirpath, filename)
            rel = os.path.relpath(path, SERVER_ROOT)
            if rel in CONSUMER_MODULES:
                continue
            yield rel, path


def _producers_in(path: str) -> set:
    """Kinds this module can WRITE.

    Counted only where the kind literal is used as evidence of a write:
      * any argument of a call — `emit("alert.viewed", ...)`,
        `build("sage.drafted", ...)`, `_emit_action_outcome(db, "action.approved", ...)`;
      * a `{"kind": "alert.fired"}` entry — the shape `record_batch` takes.
    A kind named in a docstring, a comment, a SQL fragment or a vocabulary
    declaration (PAYLOAD_KEYS' dict KEYS, ONCE_KINDS' set members) is NOT a
    producer, which is precisely the distinction defect 4 turned on.
    """
    with open(path, "r", encoding="utf-8") as fh:
        try:
            tree = ast.parse(fh.read(), filename=path)
        except SyntaxError:
            return set()
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            args = list(node.args) + [kw.value for kw in node.keywords]
            for arg in args:
                if isinstance(arg, ast.Constant) and arg.value in shadow_service.KINDS:
                    found.add(arg.value)
        elif isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if (isinstance(key, ast.Constant) and key.value == "kind"
                        and isinstance(value, ast.Constant)
                        and value.value in shadow_service.KINDS):
                    found.add(value.value)
    return found


def _producer_map() -> dict:
    producers = {kind: [] for kind in shadow_service.KINDS}
    for rel, path in _app_modules():
        for kind in _producers_in(path):
            producers[kind].append(rel)
    return producers


def test_every_kind_has_a_producer():
    """No kind may exist in the vocabulary with nothing able to write it.

    An orphan kind is not a harmless declaration. Each one silently pins a metric:
    no `action.proposed` pins `agreement` at insufficient_data; no `sage.drafted`
    pins `time_saved` — the figure most likely to be quoted to an investor — at
    insufficient_data with no source at all; no `alert.viewed` turns the sweeper
    into a machine that expires 100% of alerts.
    """
    producers = _producer_map()
    orphans = sorted(kind for kind, files in producers.items() if not files)
    assert not orphans, (
        "kinds declared in shadow_service.PAYLOAD_KEYS with NO write path in the app: "
        + ", ".join(orphans)
        + ". Either add the emit call site or delete the kind from the vocabulary — "
        "a kind that cannot be written is a metric that cannot be reported."
    )


@pytest.mark.parametrize("kind", sorted({
    # The eleven that defect 4 proved were orphans, pinned individually so a
    # regression names the exact one that lost its emitter.
    "context.compiled", "sage.drafted", "sage.refused", "sage.failed",
    "action.proposed", "action.approved", "action.declined", "clinical.action",
    "alert.viewed", "alert.acknowledged", "outcome.candidate",
}))
def test_defect_4_kind_has_a_producer(kind):
    producers = _producer_map()
    assert producers[kind], f"{kind} has no emitter in non-test code"


def test_producer_scan_would_have_caught_the_original_defect():
    """The scan itself is not vacuous.

    A vocabulary declaration must NOT read as a producer — otherwise the test
    passes on exactly the code that shipped the defect. shadow_service.py declares
    all 23 kinds in PAYLOAD_KEYS; it may only be credited with the handful it
    actually writes (episode lifecycle, the sweeper's negatives, and the two
    human-entry helpers).
    """
    declared_only = _producers_in(os.path.join(SERVER_ROOT, "services", "shadow_service.py"))
    assert declared_only == {
        "episode.opened", "episode.closed", "alert.expired", "action.abandoned",
        "outcome.recorded", "adjudication.recorded",
    }, sorted(declared_only)
    assert len(declared_only) < len(shadow_service.KINDS)
