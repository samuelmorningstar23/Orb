"""The laboratory module's state: on, draining or off (lis-1), and its
startup and shutdown (the only lifespan hook the LIS needs in main.py).

THE SWITCH. ORB_LAB (config.LAB_ENABLED) is the laboratory module. It is off
unless a site turns it on (Samuel's ruling of 3 October 2026: the three new
hospital-system modules default off).

THE THREE STATES (ruling LIS-R21, built to the recommended option)
  on        ORB_LAB is set. Every /api/lab route answers.
  draining  ORB_LAB is off, but laboratory work is still open: a test not yet
            released or cancelled, a result entered or technically validated
            but not released, or a critical result not yet closed. New work
            stops (a strict gate 404s), while laboratory staff keep the Lab tab
            to finish, cancel or release what is open, and the critical-result
            surfaces keep running. The compliance report carries a failing
            check until the work is done. Switching the module off must never
            hide an open critical result or a half-processed specimen.
  off       Nothing is open. Every /api/lab route answers 404.

/api/health publishes the state through config.module_states(), which asks
the drain check registered here. That check is DB-free: it answers from a
value computed off the event loop at startup (run_db) and refreshed by a
small loop that runs only while the module is draining, by
refresh_drain_state() after laboratory work changes, and by the compliance
report. /api/health never opens a database connection for it.

THE CUTOVER (lab_module_events, migration 55). At the first boot with ORB_LAB
on, an 'enabled' event is written; at the first boot with it off again, a
'disabled' event. One event per transition, never one per boot. Order intake
(lis-6) considers only orders activated after the latest 'enabled'
(current_enable_instant), so months of historical laboratory orders never
flood the collection lists, and an order activated while the module was off
stays on the free-text result path for good.

STARTUP, while ORB_LAB is on, also loads the starter catalogue into an empty
catalogue and registers the orderable codes in terminology
(services/lab_master_service.py), then runs the order intake sweep once
(services/lab_order_service.ensure_intake, lis-6) for any laboratory order
activated since the cutover that the post-commit hook missed. With ORB_LAB
off none of this happens: the catalogue tables stay empty, terminology is
unchanged and no order is taken in.

Every database step tolerates the laboratory tables being absent, so this
runs on a database that has not had migration 55 yet, and a failing step logs
an error and never stops the hospital's boot.

BACKGROUND LOOPS. Later LIS increments (the critical-result sweep, the
analyser listener) register their loops with register_loop(), with the states
they run in, from a module listed in _LOOP_MODULES below (startup imports
each one first), so main.py never changes for them.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Awaitable, Callable, Optional

import config

logger = logging.getLogger("orb.lab")

MODULE_ID = "lab"

#: Order item statuses that are finished; anything else is open work.
CLOSED_ITEM_STATUSES = ("released", "cancelled")
#: Result statuses still waiting for a person.
OPEN_RESULT_STATUSES = ("entered", "tech_validated")
#: Critical notification statuses still waiting for a callback.
OPEN_CRITICAL_STATUSES = ("open",)

_EMPTY_COUNTS = {"specimens": 0, "tests": 0, "results": 0, "critical_results": 0}

# The cached drain answer. open: True, False, or None for "not known", which
# reads as draining (an unknown answer must not hide open work).
_drain: dict = {"open": None, "counts": None}

LoopFactory = Callable[[], Awaitable[None]]
# (name, factory, states it runs in)
_LOOPS: list[tuple[str, LoopFactory, tuple[str, ...]]] = []
_tasks: list[asyncio.Task] = []

#: Modules startup() imports before it starts the loops, so a later LIS
#: increment's module can call register_loop() at import. A later increment
#: adds its own dotted path here (for example "services.lab_critical_service").
#: A module that fails to import is logged and skipped; boot continues.
#: lis-6: the order intake sweep (services/lab_order_service.py), 'on' only.
#: lis-15: the critical-result sweep (services/lab_critical_service.py),
#: registered for every state and running while a critical surface is open
#: (on, draining, or interfaced criticals switched on).
_LOOP_MODULES: tuple[str, ...] = ("services.lab_order_service",
                                  "services.lab_critical_service")


# ════════════════════════════════════════════════════════════════════════════
#  State
# ════════════════════════════════════════════════════════════════════════════

def module_state() -> str:
    """'on', 'draining' or 'off', as /api/health publishes it."""
    return config.module_state(MODULE_ID)


def module_on() -> bool:
    """True only while ORB_LAB is on (not while draining)."""
    return module_state() == "on"


def drain_check() -> bool:
    """The DB-free drain check registered with config: is laboratory work
    still open? Answers from the cached value; not yet known reads as open."""
    cached = _drain.get("open")
    return True if cached is None else bool(cached)


def cached_open_work() -> Optional[dict]:
    """The counts behind the cached answer, or None before the first check."""
    counts = _drain.get("counts")
    return dict(counts) if counts is not None else None


def _table_names(conn) -> set:
    return {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name LIKE 'lab_%'").fetchall()}


def _placeholders(values) -> str:
    return ", ".join("?" for _ in values)


def open_work_counts(conn) -> dict:
    """What laboratory work is still open, counted fresh. Absent tables count
    as nothing open.

    specimens         distinct specimens holding a test not yet released or
                      cancelled;
    tests             order items not yet released or cancelled;
    results           results entered or technically validated, not released;
    critical_results  critical-result notifications not yet closed.
    """
    tables = _table_names(conn)
    counts = dict(_EMPTY_COUNTS)
    if "lab_order_items" in tables:
        row = conn.execute(
            f"""SELECT COUNT(*) AS tests, COUNT(DISTINCT specimen_id) AS specimens
                FROM lab_order_items
                WHERE status NOT IN ({_placeholders(CLOSED_ITEM_STATUSES)})""",
            CLOSED_ITEM_STATUSES).fetchone()
        counts["tests"] = int(row[0] or 0)
        counts["specimens"] = int(row[1] or 0)
    if "lab_results" in tables:
        counts["results"] = int(conn.execute(
            f"SELECT COUNT(*) FROM lab_results WHERE status IN ({_placeholders(OPEN_RESULT_STATUSES)})",
            OPEN_RESULT_STATUSES).fetchone()[0] or 0)
    if "lab_critical_notifications" in tables:
        counts["critical_results"] = int(conn.execute(
            "SELECT COUNT(*) FROM lab_critical_notifications "
            f"WHERE status IN ({_placeholders(OPEN_CRITICAL_STATUSES)})",
            OPEN_CRITICAL_STATUSES).fetchone()[0] or 0)
    return counts


def refresh_drain_state(conn=None) -> bool:
    """Count open laboratory work and cache the answer. Sync and blocking:
    call it from a def handler, a worker thread or run_db, never on the loop.

    Returns True when work is open. A failure leaves the answer unknown,
    which reads as draining."""
    try:
        if conn is None:
            from database import db_session
            with db_session() as db:
                counts = open_work_counts(db)
        else:
            counts = open_work_counts(conn)
    except Exception:
        logger.exception("Laboratory open-work check failed; reporting the module as draining")
        _drain["open"] = None
        _drain["counts"] = None
        return True
    _drain["counts"] = counts
    _drain["open"] = any(counts.values())
    return _drain["open"]


# ════════════════════════════════════════════════════════════════════════════
#  The cutover
# ════════════════════════════════════════════════════════════════════════════

def latest_module_event(conn) -> Optional[dict]:
    """The newest lab_module_events row, or None."""
    if "lab_module_events" not in _table_names(conn):
        return None
    row = conn.execute(
        "SELECT id, event, occurred_at, detail FROM lab_module_events "
        "ORDER BY julianday(occurred_at) DESC, id DESC LIMIT 1").fetchone()
    return dict(row) if row else None


def current_enable_instant(conn) -> Optional[str]:
    """When the module was last switched on, if it is on now by the events
    (the latest event is 'enabled'); else None. Intake (lis-6) uses this: an
    order activated before it is never taken into the laboratory, and None
    means take nothing."""
    latest = latest_module_event(conn)
    if latest and latest["event"] == "enabled":
        return latest["occurred_at"]
    return None


def record_cutover(conn, *, lab_on: bool) -> Optional[str]:
    """Write 'enabled' or 'disabled' if the switch changed since the last
    event, with its audit row (clinical_config, fail-closed) in the same
    transaction. Returns the event written, or None when nothing changed.

    'enabled' when the switch is on and the latest event is not 'enabled';
    'disabled' when the switch is off and the latest event is 'enabled'. A
    site that never turned the laboratory on writes nothing."""
    if "lab_module_events" not in _table_names(conn):
        return None
    from services.audit_service import log_audit_event
    from services.clinical_time import utc_iso_now

    latest = latest_module_event(conn)
    latest_event = latest["event"] if latest else None
    if lab_on and latest_event != "enabled":
        event = "enabled"
    elif not lab_on and latest_event == "enabled":
        event = "disabled"
    else:
        return None
    now = utc_iso_now()
    detail = ("ORB_LAB switched on" if event == "enabled" else "ORB_LAB switched off")
    cur = conn.execute(
        "INSERT INTO lab_module_events (event, occurred_at, detail) VALUES (?, ?, ?)",
        (event, now, detail))
    log_audit_event(
        correlation_id=str(uuid.uuid4()),
        event_category="clinical_config",
        action_type=f"lab_module_{event}",
        outcome="success",
        db=conn,
        summary_message=(
            "Laboratory module switched on: orders activated from now on go to the laboratory"
            if event == "enabled" else
            "Laboratory module switched off: new laboratory work stops, open work drains"),
        actor_user_id="system",
        actor_role="system",
        target_type="lab_module_event",
        target_id=str(cur.lastrowid),
        metadata={"event": event, "occurred_at": now},
    )
    logger.info("Laboratory module %s at %s", event, now)
    return event


# ════════════════════════════════════════════════════════════════════════════
#  Compliance
# ════════════════════════════════════════════════════════════════════════════

def compliance_control() -> Optional[dict]:
    """The compliance report's laboratory check: present (and failing) only
    while the module is switched off with open work. None otherwise, so a site
    that never used the laboratory sees no new line. Counts fresh from the
    database and refreshes the cached drain answer as it goes."""
    if config.LAB_ENABLED is True:
        return None
    try:
        from database import db_session
        with db_session() as db:
            counts = open_work_counts(db)
    except Exception:
        # Keep whatever the module already reports: a failed count here must
        # neither invent a laboratory line on a site that never used one nor
        # hide one that is draining.
        logger.warning("Laboratory open-work count failed in the compliance report",
                       exc_info=True)
        counts = None
    else:
        _drain["counts"] = counts
        _drain["open"] = any(counts.values())
        if not _drain["open"]:
            return None
    if counts is None:
        if module_state() != "draining":
            return None
        return {
            "id": "lab_module_draining",
            "name": "Laboratory module switched off with open work",
            "status": "unknown",
            "evidence": ("The laboratory module is switched off, and Orb could not count the "
                         "open laboratory work, so it keeps the laboratory screens open."),
            "remediation": "Contact Orb support: the laboratory work list could not be read.",
            "settings": ["ORB_LAB"],
        }
    parts = [f"{counts['specimens']} specimen(s) with tests not yet released"]
    if counts["results"]:
        parts.append(f"{counts['results']} result(s) waiting for validation or authorisation")
    parts.append(f"{counts['critical_results']} critical result(s) not yet closed")
    return {
        "id": "lab_module_draining",
        "name": "Laboratory module switched off with open work",
        "status": "fail",
        "evidence": ("The laboratory module is switched off on this site, but laboratory "
                     "work is still open: " + ", ".join(parts) + ". Laboratory staff can "
                     "still finish, cancel or release it, and critical results stay on the "
                     "ward until they are closed."),
        "remediation": ("Have the laboratory finish, cancel or release the open work, and "
                        "close every critical result by a phone call or a read-back on the "
                        "ward. Or switch the laboratory module back on."),
        "settings": ["ORB_LAB"],
    }


# ════════════════════════════════════════════════════════════════════════════
#  Startup and shutdown
# ════════════════════════════════════════════════════════════════════════════

def register_loop(name: str, factory: LoopFactory, *,
                  states: tuple[str, ...] = ("on", "draining")) -> None:
    """Have startup() run `factory()` as a background task while the module
    is in one of `states` at boot. For later LIS increments (the critical
    sweep, the analyser listener); main.py never changes for them. The
    coroutine must re-raise CancelledError."""
    bad = set(states) - set(config.MODULE_STATES)
    if bad or not states:
        raise ValueError(f"unknown module states: {sorted(bad) or states}")
    for i, (existing, _f, _s) in enumerate(_LOOPS):
        if existing == name:
            _LOOPS[i] = (name, factory, tuple(states))
            return
    _LOOPS.append((name, factory, tuple(states)))


def _startup_sync() -> dict:
    """The blocking half of startup, run in a worker thread by startup()."""
    from database import db_session

    lab_on = config.LAB_ENABLED is True
    summary: dict = {"lab_on": lab_on, "cutover": None, "catalogue": None, "open": None}
    try:
        with db_session(immediate=True) as conn:
            summary["cutover"] = record_cutover(conn, lab_on=lab_on)
    except Exception:
        logger.error("Laboratory cutover event could not be recorded", exc_info=True)
    if lab_on:
        try:
            from services.lab_master_service import load_starter_catalogue
            summary["catalogue"] = load_starter_catalogue()
        except Exception:
            logger.error("Laboratory starter catalogue could not be loaded", exc_info=True)
        # lis-6: the intake backstop, once at boot, for any laboratory order
        # activated since the cutover that the post-commit hook missed (the
        # process stopped between the order's commit and the hook). Never
        # raises; a background loop repeats it while the module is on.
        try:
            from services.lab_order_service import ensure_intake
            summary["intake"] = ensure_intake()
        except Exception:
            logger.error("Laboratory order intake sweep could not run at startup", exc_info=True)
    summary["open"] = refresh_drain_state()
    return summary


async def _drain_refresh_loop(interval_seconds: int) -> None:
    """While the module drains, recount open work so the state goes to 'off'
    once the laboratory has finished. Stops itself then: nothing can become
    open again while the switch is off."""
    from database import run_db

    while True:
        await asyncio.sleep(interval_seconds)
        try:
            still_open = await run_db(refresh_drain_state)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("Laboratory drain refresh failed; will retry", exc_info=True)
            continue
        if not still_open and _drain.get("open") is False:
            logger.info("Laboratory open work is finished; the module is now off")
            return


async def startup() -> dict:
    """Called once from main.py's lifespan, after init_db(). Computes the
    module state off the event loop, writes the cutover event, loads the
    starter catalogue while ORB_LAB is on, registers the DB-free drain check
    and starts the background loops for the current state. Never raises."""
    from database import run_db

    summary: dict = {}
    try:
        summary = await run_db(_startup_sync)
    except Exception:
        logger.error("Laboratory module startup failed", exc_info=True)
        _drain["open"] = None
    config.register_module_drain_check(MODULE_ID, drain_check)
    state = module_state()
    summary["state"] = state
    import importlib
    for dotted in _LOOP_MODULES:
        try:
            importlib.import_module(dotted)
        except Exception:
            logger.error("Laboratory module %s failed to import; its loop will not run",
                         dotted, exc_info=True)
    if state == "draining":
        _tasks.append(asyncio.create_task(
            _drain_refresh_loop(max(5, int(config.LAB_CRITICAL_SWEEP_S))),
            name="lab-drain-refresh"))
        logger.warning("Laboratory module is switched off with open work; it stays reachable "
                       "for laboratory staff until the work is finished")
    for name, factory, states in list(_LOOPS):
        if state in states:
            try:
                _tasks.append(asyncio.create_task(factory(), name=f"lab-{name}"))
            except Exception:
                logger.error("Laboratory loop %s failed to start", name, exc_info=True)
    if state != "off":
        logger.info("Laboratory module %s", state)
    return summary


async def shutdown() -> None:
    """Cancel the module's background tasks, bounded like main.py's own
    shutdown, and unregister the drain check. Never raises."""
    tasks = [t for t in _tasks if not t.done()]
    for task in tasks:
        task.cancel()
    if tasks:
        _done, pending = await asyncio.wait(tasks, timeout=10.0)
        for task in pending:
            logger.error("Laboratory task %s did not stop within 10s; abandoning it",
                         task.get_name())
    _tasks.clear()
    config.register_module_drain_check(MODULE_ID, None)
