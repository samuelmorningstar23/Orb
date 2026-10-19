"""Orb Hospital OS — FastAPI Main Entry Point"""
import asyncio
import faulthandler
import logging
import os
import signal
import socketio

# Ops hook: `kill -USR1 <pid>` dumps every thread's Python stack to stderr —
# the way to identify a wedged writer/task in a live process without killing it.
try:
    faulthandler.register(signal.SIGUSR1)
except (AttributeError, ValueError):
    pass  # unavailable on this platform/thread — purely diagnostic, never fatal
from contextlib import asynccontextmanager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("orb")

# ── Per-site configuration (server/site_config/site.toml) ────────────────────
# FIRST, and before `import config`. The file is translated into the ORB_*
# variables the existing readers already consult (escalation ladder, NEWS2
# threshold, HL7 default timezone, operational thresholds), and config.py binds
# several of those AT IMPORT TIME — so loading it any later would apply half of
# the site's settings and silently drop the rest.
#
# An absent file is normal and means "shipped defaults". An INVALID file stops
# the boot, deliberately: a config Orb could not understand must never turn into
# an appliance that is running with an unknown subset of a hospital's settings.
# The message is printed plainly and the process exits — no traceback, because
# the person reading it is hospital IT, not the author.
from services.site_config_service import SiteConfigError, load_site_config  # noqa: E402
try:
    load_site_config()
except SiteConfigError as _site_config_error:
    print(str(_site_config_error), file=__import__("sys").stderr)
    raise SystemExit(2)

from fastapi import FastAPI, Depends
from fastapi.middleware.cors import CORSMiddleware

from config import HOST, PORT, CORS_ORIGINS, VITALS_INTERVAL_SECONDS, INGESTION_MODE
from database import init_db
from auth import get_current_user
from models.user import UserOut

# Import route modules
from routes.auth_routes import router as auth_router
from routes.vigil_routes import router as vigil_router
from routes.relay_routes import router as relay_router
from routes.sage_routes import router as sage_router
from routes.sage_proactive_routes import router as sage_proactive_router
from routes.scribe_routes import router as scribe_router
from routes.bridge_routes import router as bridge_router
from routes.lens_routes import router as lens_router
from routes.forecast_routes import router as forecast_router
from routes.surgery_routes import router as surgery_router
from routes.ingestion_routes import router as ingestion_router
from routes.admission_routes import router as admission_router
from routes.audit_routes import router as audit_router
from routes.admin_routes import router as admin_router
from routes.discharge_access_routes import router as discharge_access_router
from routes.helix_routes import router as helix_router
from routes.pulse_routes import router as pulse_router
from routes.clinical_records_routes import router as clinical_records_router
from routes.model_governance_routes import router as model_governance_router
from routes.bundle_routes import router as bundle_router
from routes.ipass_routes import router as ipass_router
from routes.downtime_routes import router as downtime_router
from routes.fhir_export_routes import router as fhir_export_router
from routes.pharmacy_routes import router as pharmacy_router
from routes.orders_routes import router as orders_router
from routes.flight_recorder_routes import router as flight_recorder_router
from routes.appointments_routes import router as appointments_router
from routes.search_routes import router as search_router
from routes.patient_timeline_routes import router as chart_timeline_router
from routes.admin_users_routes import router as admin_users_router
from routes.roster_import_routes import router as roster_import_router
from routes.backup_routes import router as backup_router
from routes.metrics_routes import router as metrics_router
# Investor / executive intelligence surfaces
from routes.impact_routes import router as impact_router
from routes.compliance_routes import router as compliance_router
# DPDP Act 2023 consent capture — bedside, clinician-gated, NOT admin-gated.
from routes.compliance_routes import consent_router
from routes.coding_routes import router as coding_router
from routes.command_center_routes import router as command_center_router
from routes.capacity_sim_routes import router as capacity_sim_router
from routes.emar_routes import router as emar_router
from routes.nurse_routes import router as nurse_router
from routes.shadow_routes import router as shadow_router
from routes.ops_routes import router as ops_router
from routes.billing_routes import router as billing_router
from routes.frontdesk_routes import router as frontdesk_router
from routes.payer_routes import router as payer_router
from routes.procurement_routes import router as procurement_router
from routes.nabh_routes import router as nabh_router
from routes.ward_record_routes import router as ward_record_router
from routes.dietary_routes import router as dietary_router
from routes.workforce_routes import router as workforce_router
from routes.biomedical_routes import router as biomedical_router
from routes.housekeeping_routes import router as housekeeping_router
from routes.payments_routes import router as payments_router
from routes.abdm_routes import router as abdm_router
# The hospital-system build's route packages and files (wave 1, 3 October 2026).
# Registered once here, each behind its opt-in switch (routes/opt_in_gates.py),
# so later increments add endpoints inside them and never edit this list.
from routes.adt import router as adt_router
from routes.lab import router as lab_router
from routes.pharmacy_store import router as pharmacy_store_router
from routes.opd_routes import router as opd_router
from routes.registration_routes import router as registration_router
from routes.schedule_routes import router as schedule_router
# The medication-order store's package (wave 4), behind ORB_MED_ORDER_STORE.
from routes.medorders import router as medorders_router
# Facility identity (multi-site, wave 7). Registered once; later increments add
# routes inside it. Facility 1 works with ORB_MULTI_SITE off.
from routes.facility_routes import router as facility_router


# Import WebSocket and Bots
from ws.realtime import sio
# run_vitals_bot is no longer imported here: it is owned by
# services.vitals_source.SimulatorSource, which is what drives the tick now.
from services.bot_service import run_reports_bot, run_lab_bot


# ── Long-lived background tasks ──
# The event loop keeps only a WEAK reference to a task, so a task whose
# create_task() result is dropped can be garbage-collected mid-execution — the
# loops below would then stop silently (no exception, no log line). Holding a
# strong reference until the task completes is the documented fix; the
# done-callback discard keeps the set from growing.
_BG_TASKS: set[asyncio.Task] = set()


def _spawn(coro) -> asyncio.Task:
    """Start `coro` as a background task that cannot be GC'd while it runs."""
    task = asyncio.create_task(coro)
    _BG_TASKS.add(task)
    task.add_done_callback(_BG_TASKS.discard)
    return task


# ── Background task: Sage proactive surveillance (always-on) ──
async def proactive_sweep():
    """Continuously scan admitted patients for clinical insights and push them live.
    Always-on real analysis over whatever data exists; never raises."""
    from services.sage_proactive_service import evaluate_all_admitted
    from ws.realtime import sio
    await asyncio.sleep(8)  # let startup settle
    while True:
        try:
            # A stood-down Rule 1 card that goes back up keeps its id, so it is
            # not in `ids`; without its own push the feed showed it only on its
            # next 20 s poll (sage_proactive_service.evaluate_patient).
            raised_again: list = []
            ids = await asyncio.to_thread(evaluate_all_admitted, raised_again)
            if ids or raised_again:
                await sio.emit("sage:insight", {"event": "new", "count": len(ids),
                                                "raised_again": len(raised_again)})
        except Exception as e:
            logger.error("Proactive sweep error: %s", e)
        await asyncio.sleep(15)


# ── Background task: periodic EHR-downtime snapshots ──
async def downtime_snapshot_sweep():
    """Periodically capture a durable, LLM-/network-free snapshot of every admitted
    patient so the ward has a recent read-only picture the instant the EHR/network/
    LLM goes down. Runs hourly; never raises."""
    from services.downtime_service import (
        capture_all_admitted,
        build_ward_print_packs,
        run_nightly_fhir_self_export_async,
    )
    await asyncio.sleep(30)  # let startup settle
    hours = 0
    while True:
        try:
            result = await asyncio.to_thread(capture_all_admitted, "auto")
            logger.info("[Downtime] snapshotted %s admitted patient(s)", result.get("count"))
        except Exception as e:
            logger.error("Downtime snapshot sweep error: %s", e)
        # Ward-level print packs render from the snapshots captured just above,
        # so they are rebuilt (and pruned) in the same tick — one durable HTML
        # artifact per ward on local disk, retrievable even with the app down.
        # Separate try: a pack failure must not cost the ward its snapshots,
        # and a capture failure still leaves packs from the last good rows.
        try:
            packs = await asyncio.to_thread(build_ward_print_packs, "auto")
            logger.info("[Downtime] ward pack(s): %s ward(s), %s patient(s) -> %s",
                        packs.get("wards"), packs.get("patients"), packs.get("directory"))
        except Exception as e:
            logger.error("Downtime ward pack build error: %s", e)
        # Once every 24 hourly ticks, also write a durable FHIR $everything
        # self-export of every admitted patient to local disk — a standards-based,
        # on-prem escrow copy the ward can fall back to if the EHR is down.
        if hours % 24 == 0:
            try:
                # NOT asyncio.to_thread: this runs on downtime_service's own
                # single-thread lane, and it self-throttles + skips if a run
                # already completed within the last 24 h (so a restart loop
                # cannot re-write ~282 MB on every boot).
                fx = await run_nightly_fhir_self_export_async("auto")
                logger.info("[Downtime] FHIR self-export wrote %s bundle(s) to %s",
                            fx.get("count"), fx.get("directory"))
            except Exception as e:
                logger.error("Downtime FHIR self-export error: %s", e)
        hours += 1
        await asyncio.sleep(3600)  # hourly


# ── Background task: Sage provenance retention sweep ──
async def sage_provenance_prune_sweep():
    """Daily retention pass over the Sage provenance store. Without this caller
    prune() is dead code and the store grows ~213 MB/year unbounded — the
    settle-at-~234-MB disk plan in sage_provenance_service.py assumes this runs.
    prune() reads ORB_SAGE_PROVENANCE_RETENTION_DAYS at CALL time, so ops can
    change the horizon without a restart. Logs COUNTS only — a retention log
    line must never carry the content it deleted. Runs daily; never raises."""
    from database import run_db
    from services import sage_provenance_service as sage_provenance
    await asyncio.sleep(120)  # let startup settle
    while True:
        try:
            if sage_provenance.enabled():
                result = await run_db(sage_provenance.prune)
                logger.info("[Provenance] retention prune: %s turn(s), %s blob(s), "
                            "%s B reclaimed", result.get("turns_deleted"),
                            result.get("blobs_deleted"), result.get("bytes_reclaimed"))
        except Exception as e:
            logger.error("Sage provenance prune sweep error: %s", e)
        await asyncio.sleep(86400)  # daily


# ── Vitals sources (simulator / manual / bedside monitor are peers) ──
async def _demo_companion_bots():
    """The reports + lab demo bots, which have always shared the vitals tick.

    They are NOT vitals, but they ran inside the same 5-second loop, after the
    vitals bot, in the same try/except. Keeping them there — rather than giving
    them their own task — preserves their ordering and their failure coupling
    exactly as they were before the VitalsSource refactor.
    """
    await run_reports_bot()
    await run_lab_bot()


def _configure_vitals_sources():
    """Register this deployment's vitals sources. Started later, in lifespan.

    Respects INGESTION_MODE for the SIMULATOR only:
      - 'simulation': synthetic bots run
      - 'ingestion':  bots disabled, real feeds only (default)
      - 'hybrid':     both
    The monitor feed is independent of that switch — it carries real patient
    data and is gated solely on ORB_MONITOR_ENABLED. Manual entry is always
    registered; it is the redundancy layer.
    """
    from services import vitals_source

    bots_enabled = INGESTION_MODE in ("simulation", "hybrid")
    if not bots_enabled:
        logger.info("[Mode: %s] Simulation bots disabled", INGESTION_MODE)

    return vitals_source.configure_from_env(
        sio=sio,
        simulator_enabled=bots_enabled,
        simulator_interval=VITALS_INTERVAL_SECONDS,
        simulator_companion_tick=_demo_companion_bots,
    )


# ── keep-warm policy ─────────────────────────────────────────────────────────
# Module level, not nested in the lifespan, so both decisions are reachable by
# tests. They are the whole policy: how long a warm ping may hold the one
# background LLM slot, and how hard to back off when it does not work.

#: Consecutive failures are capped here so a recovered engine is still picked
#: up within roughly an hour of 300 s intervals.
KEEP_WARM_MAX_BACKOFF_TICKS = 12


from collections.abc import Collection, Iterable
from services.warm_policy import warm_budget_seconds as _warm_budget_seconds
from services.warm_policy import keep_warm_backoff_ticks


#: Tiers an administrator unloaded from the header AI panel. The unload route
#: adds to it and the load route clears it. Before this set, `keep_warm_loop`
#: saw an unloaded tier as "evicted" and re-loaded it within one 300 s interval,
#: so the memory the confirmation promised to free came straight back.
#: In-process on purpose: a restart is a fresh boot and warms every tier.
_OPERATOR_UNLOADED: set[str] = set()


def models_to_warm(configured: Iterable[str], resident: Collection[str] = frozenset()) -> list[str]:
    from services.warm_policy import select_models_to_warm
    return select_models_to_warm(configured, resident, _OPERATOR_UNLOADED)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """App startup / shutdown."""
    logger.info("═══ Orb Hospital OS — Starting (profile=%s) ═══", DEPLOYMENT_PROFILE)
    # The Orb Ward Record's two clinical switches, stated once so an operator
    # can see from the log which mode this computer is in.
    import config as _clinical_config
    logger.info("Clinical switches: scoring %s (ORB_SCORING_ENABLED), sepsis bundle "
                "autostart %s (ORB_SEPSIS_AUTOSTART), models %s (ORB_MODELS_ENABLED)",
                "on" if _clinical_config.SCORING_ENABLED else "off",
                "on" if _clinical_config.SEPSIS_AUTOSTART else "off",
                "on" if _clinical_config.MODELS_ENABLED else "off")
    # ORB_WARD_RECORD: one line naming every setting the profile forced off.
    _ward_record_line = _clinical_config.ward_record_boot_line()
    if _ward_record_line:
        logger.warning(_ward_record_line)

    # The patient discharge link is built on ORB_PUBLIC_APP_BASE_URL. In prod a
    # value a patient's phone cannot open (unset, not https, or this machine)
    # means no discharge link is issued at all, so say it once, loudly, at boot
    # rather than first at the discharge desk. Log, never refuse to start: the
    # wards must run whether or not the patient portal is reachable.
    try:
        from services.discharge_access_service import log_public_address_problem
        log_public_address_problem(logger)
    except Exception as e:
        logger.warning("Discharge link address check failed: %s", e)

    init_db()

    # The facility spine's one boot refusal (multisite-1): facility 1 must exist
    # and be active. Only tampering produces either; nothing else about
    # facilities stops a boot (not ORB_MULTI_SITE, not a second facility). Then
    # read the facilities table once, so /api/health and the facility fence
    # start from the real answer.
    from services import facility_service as _facility_service
    _facility_service.assert_facility_one()
    if _facility_service.prime_cache():
        logger.warning("More than one facility is on record: screens not yet ready to "
                       "keep each facility's patients apart answer 503 (facility fence).")

    # Reconcile the AI model registry with what Orb actually ships, once per boot.
    #
    # DELIBERATELY NOT ON THE READ PATH. `list_models()` runs the seeder only when
    # the registry is EMPTY, which meant a retired model was served forever on any
    # database that already had rows (migration 38 has the full history). The
    # tempting one-line fix, reconciling inside `list_models()`, is wrong twice
    # over: that function backs `GET /admin/models/provenance`, which any
    # authenticated clinician hits inline on an AI output, and SQLite is
    # single-writer, so N clinicians opening the app would take N write locks for
    # zero needed writes. It is also not a reliable REPAIR, because a box nobody
    # opened would stay wrong indefinitely: the moment of repair would depend on
    # traffic rather than on deployment.
    #
    # So: migration 38 repairs databases that are already stale, and this keeps the
    # next retirement from drifting. Both are deterministic and neither waits for a
    # clinician to open a page.
    #
    # Warn rather than fail. A registry that cannot be reconciled must never stop a
    # hospital appliance from booting; the stale-registry state it leaves behind is
    # the status quo ante, not a new harm.
    try:
        from services.model_governance_service import seed_default_models
        seed_default_models()
    except Exception as e:
        logger.warning("Model registry reconcile failed; registry may be stale: %s", e)

    # Discover this site's real wards before anything reads the capacity map.
    # Must follow init_db() (it queries `patients`) and precede the Command
    # Center / Forecast pillars, which both import WARD_CAPACITY.
    try:
        from services.forecast_service import refresh_ward_capacity
        refresh_ward_capacity()
    except Exception as e:
        logger.warning("Ward capacity discovery failed, using fallback map: %s", e)

    # Every long-lived loop started below is collected here so shutdown can cancel
    # them deterministically instead of leaving them pending.
    lifespan_tasks: list[asyncio.Task] = []

    # Initialize reactive nervous system triggers
    try:
        from services.nervous_system_triggers import register_triggers
        register_triggers()
    except Exception as e:
        logger.warning("Failed to register event bus triggers: %s", e)

    # ORB_MODELS_ENABLED off (or the Orb Ward Record): no model lane starts at
    # boot. Read once here; services/model_switch.py is the one reader.
    from services.model_switch import models_enabled as _models_enabled
    _models_on = _models_enabled()
    if not _models_on:
        logger.warning("Models switched off on this computer (ORB_MODELS_ENABLED or "
                       "ORB_WARD_RECORD): no model is warmed and the ward narrator, Sage "
                       "insights, the knowledge workers and the voice agent do not start.")

    # Situational-awareness ward narrator (event-driven; deterministic floor +
    # debounced local-LLM narrative at background priority)
    try:
        from services.sage_narrator_service import register_narrator, ward_narrator
        if _models_on:
            register_narrator()
            lifespan_tasks.append(_spawn(ward_narrator.idle_loop()))
    except Exception as e:
        logger.warning("Failed to start ward narrator: %s", e)

    # At-least-once event delivery: replay anything a prior crash / subscriber
    # exception left un-acked, and keep a periodic poller running. Started AFTER
    # all subscribers are registered so replay can find their callbacks.
    try:
        from services.event_bus import event_bus
        event_bus.start_replay_poller()
    except Exception as e:
        logger.warning("Failed to start event delivery poller: %s", e)

    try:
        from services.helix_db import init_helix_db
        init_helix_db()
    except Exception as e:
        logger.warning("Helix DB init failed (non-fatal): %s", e)

    # ── Medication-order store (ORB_MED_ORDER_STORE, default off) ──
    # With the store on, give every chart medication that has no order one
    # (medorders-2). HERE, at boot, and never on a GET: SQLite has one writer.
    # A no-op with the switch off; never raises (a failure is logged and the
    # unadopted rows keep today's behaviour). Off the event loop, batch by batch.
    from database import run_db as _run_db
    from services.medorders.adoption import boot_adoption
    await _run_db(boot_adoption)

    # Fold the hospital's own formulary into Sage's deterministic mutation gate,
    # so "stop the Dolo" / "hold the Monocef" trip the lock the same way "stop
    # the heparin" does. Read ONCE, here, because the gate itself runs inline on
    # the event loop for every Sage turn and must stay a pure function (a sync
    # SQLite read on the loop is the known wedge class). Non-fatal: the built-in
    # generic-drug lexicon is the floor.
    try:
        from services.sage_service import refresh_formulary_terms_from_db
        _n_formulary = refresh_formulary_terms_from_db()
        logger.info("Sage mutation gate: %d formulary terms registered", _n_formulary)
    except Exception as e:
        logger.warning("Sage formulary lexicon load failed (non-fatal): %s", e)

    # Seed clinical terminology (LOINC/SNOMED/ICD-10) for CPOE order validation.
    try:
        from services.terminology_service import seed_terminology
        seed_terminology()
    except Exception as e:
        logger.warning("Terminology seed failed (non-fatal): %s", e)

    # ── Laboratory module (ORB_LAB, default off) ──
    # The one lifespan hook the LIS needs: state (on, draining, off), cutover
    # event, starter catalogue while on, and its own background loops. See
    # services/lab_module.py. Never raises.
    from services import lab_module as _lab_module
    await _lab_module.startup()

    # Seed the built-in drug-interaction pairs (data-backed DDI screening).
    try:
        from services.interaction_db import ensure_seeded
        ensure_seeded()
    except Exception as e:
        logger.warning("Interaction DB seed failed (non-fatal): %s", e)

    # Reconcile any verification jobs orphaned by previous crash/restart
    try:
        from routes.scribe_routes import reconcile_stale_pending_verifications
        reconcile_stale_pending_verifications()
    except Exception as e:
        logger.warning("Scribe verification reconciliation failed (non-fatal): %s", e)

    # Initialize clinical knowledge base (embeds WHO/SSC/AHA guidelines).
    # OFF the startup critical path: on a corpus change this embeds the whole
    # corpus (now inside the knowledge worker subprocess), and the API must
    # answer /api/health while that happens. The fingerprint fast-path makes
    # the usual boot a no-op; until a rebuild finishes, Sage retrieval simply
    # degrades (lexical-only or LLM-only), which every caller already handles.
    async def _init_knowledge_background():
        try:
            from services.clinical_knowledge import init_knowledge_base
            await asyncio.to_thread(init_knowledge_base)
        except Exception as e:
            logger.warning("Clinical knowledge base init failed (non-fatal): %s", e)

    if _models_on:
        lifespan_tasks.append(_spawn(_init_knowledge_background()))
    
    # Pre-load BOTH inference tiers into unified memory so first queries are
    # fast, and keep them there. ORB_OLLAMA_KEEP_ALIVE=-1 tells Ollama never to
    # idle-unload; the heartbeat below re-warms after an Ollama restart and
    # refreshes finite keep_alive settings. Pings are lowest-priority and skip
    # entirely when the queue is busy — a model actively serving IS warm.
    async def _warm_one(model: str, log_success: bool = True) -> bool:
        import httpx
        from config import OLLAMA_BASE_URL, OLLAMA_KEEP_ALIVE, LLM_MODEL
        from services.sage_service import INTERACTIVE_NUM_CTX
        # Warm with SAGE'S OWN SYSTEM PROMPT, not "hi". llama.cpp keeps the KV
        # cache of the previous request and reuses the common prefix, so a
        # warmup that shares Sage's prefix makes the first clinician question
        # skip a full prefill of the ~1,070-token contract. Measured on the
        # appliance: 6.9 s cold vs 0.4 s warm — the difference between missing
        # and meeting the ≤5 s first-words bar on question one.
        warm_messages = [{"role": "user", "content": "hi"}]
        if model == LLM_MODEL:
            try:
                # Must be the SAME prompt the live path sends, or the warmup
                # actively HURTS: llama.cpp caches the last request's prefix, so
                # priming with a different system prompt EVICTS the one Sage is
                # about to use. This heartbeat repeats every 5 minutes, so a
                # mismatch means the clinician pays a cold ~3.4 s prefill on
                # most turns. Fast-simple is the prompt the overwhelming
                # majority of turns use.
                from services.sage_service import (
                    SAGE_FAST_SIMPLE_PROMPT, SAGE_SIMPLE_SYSTEM_PROMPT,
                    _xml_contract_forced,
                )
                _p = SAGE_SIMPLE_SYSTEM_PROMPT if _xml_contract_forced() else SAGE_FAST_SIMPLE_PROMPT
                warm_messages = [
                    {"role": "system", "content": _p},
                    {"role": "user", "content": "hi"},
                ]
            except Exception:
                pass
        # THE WARMUP MUST LOAD THE WINDOW THE LIVE PATH USES (2026-08-04).
        # Ollama keys a runner by (model, num_ctx), so "a cheap re-size" is not
        # what happens when the two disagree — the runner is torn down and
        # rebuilt, which costs ~2.8 s AND discards the KV cache this warmup
        # exists to populate. Warming the interactive tier at 4096 therefore
        # guaranteed that the FIRST clinician question paid a reload, and every
        # 5-minute heartbeat re-armed that trap. Measured before this fix: the
        # first chart-open prefetch after a restart took 18.7 s, most of it the
        # 4096 -> 8192 swap. The ambient tier keeps 4096 — that IS its live
        # window (narrator, timeline, lens, surgery escalation all pin it).
        _warm_ctx = INTERACTIVE_NUM_CTX if model == LLM_MODEL else 4096
        # THIS TIMEOUT IS A LANE BUDGET (2026-08-15). It was a flat 120.0, and
        # `keep_warm_loop` holds the one background LLM slot across this call —
        # so a warm ping that hangs is a two-minute blackout of Sage's alert
        # advice, not a slow no-op. See the loop below for what that measured.
        async with httpx.AsyncClient(timeout=_warm_budget_seconds(),
                                     trust_env=False) as client:
            resp = await client.post(
                f"{OLLAMA_BASE_URL}/api/chat",
                json={
                    "model": model,
                    "messages": warm_messages,
                    "stream": False,
                    "keep_alive": OLLAMA_KEEP_ALIVE,
                    # Explicit ctx: without it Ollama loads the model at its
                    # DEFAULT context — 128K for medgemma, which ballooned the
                    # 4B runner to ~10 GB and OOM-pressured the box.
                    "options": {"num_predict": 1, "num_ctx": _warm_ctx},
                },
            )
        if resp.status_code == 200:
            if log_success:
                logger.info("Model '%s' pre-loaded (GPU warm, keep_alive=%s)", model, OLLAMA_KEEP_ALIVE)
            return True
        # Loud, actionable error — a silent 404 here means Sage/Lens return empty
        # responses with no visible cause (the model name isn't pulled in Ollama).
        logger.error(
            "Warmup got HTTP %s for model '%s'. It is likely not installed in "
            "Ollama — run `ollama list` and set ORB_LLM_MODEL / ORB_AMBIENT_LLM_MODEL "
            "to a pulled model. AI features using it will fail until fixed.",
            resp.status_code, model,
        )
        return False

    async def warmup_models():
        try:
            from config import LLM_MODEL, AMBIENT_LLM_MODEL
            from services.llm_queue_service import llm_queue
            await llm_queue.acquire(priority=5)  # Lowest priority — defers to everything
            try:
                for model in dict.fromkeys([LLM_MODEL, AMBIENT_LLM_MODEL]):
                    # Checked per model, after the queue wait: an administrator
                    # can unload a tier while this one waits or loads.
                    if not models_to_warm([model]):
                        continue
                    await _warm_one(model)
            finally:
                llm_queue.release()
        except Exception as e:
            logger.warning("LLM warmup failed (non-fatal): %s", e)

    async def keep_warm_loop(interval_seconds: int = 300):
        """Re-warm a tier Ollama has dropped — WITHOUT starving the alert path.

        WHAT THIS COST BEFORE THE BUDGET AND THE BACKOFF (measured 2026-08-15,
        5.5 h of running ward). `medgemma:27b` is pinned and was serving Tier-2
        drafts back to back, so Ollama had no window to load `medgemma:4b`; the
        warm POST therefore hung to its full 120 s client timeout, EVERY TIME,
        while this loop held the one background LLM slot. Thirty-three of them:
        3939 s = 20 % of wall clock and 60 % of all lane-occupancy, 58.9 % of
        Tier-2 drafts lost inside those windows against 4.0 % outside, and 53 of
        the 65 surviving `queue_timeout` rows fell inside one. The failure was
        silent — the timeout landed on `logger.debug`.

        THE LOOP CANNOT WIN A FIGHT IT KEEPS RE-STARTING. Retrying a load that
        cannot complete, every interval, forever, converts a transient eviction
        into a permanent 2-minutes-in-10 blackout. So a failed re-warm now backs
        off exponentially and says so at WARNING; a success resets it. When the
        engine genuinely has no room for the ambient tier, the honest outcome is
        that the tier stays evicted (every ambient caller already checks
        `model_router.ambient_tier_evicted` and skips) — not that the alert lane
        pays for the attempt on a loop.
        """
        import asyncio as _aio
        import httpx
        from config import OLLAMA_BASE_URL, LLM_MODEL, AMBIENT_LLM_MODEL
        from services.llm_queue_service import llm_queue
        backoff_ticks = 0
        failures = 0
        while True:
            await _aio.sleep(interval_seconds)
            if backoff_ticks > 0:
                backoff_ticks -= 1
                continue
            try:
                # Cheap residency check first — /api/ps costs nothing and, when
                # everything is already loaded, avoids a generate ping that
                # would re-size a model's context (reload churn).
                try:
                    async with httpx.AsyncClient(timeout=3.0, trust_env=False) as client:
                        resp = await client.get(f"{OLLAMA_BASE_URL}/api/ps")
                        resp.raise_for_status()
                        resident = {m.get("name", "") for m in resp.json().get("models", [])}
                except Exception:
                    resident = set()
                # A tier an administrator unloaded is not "evicted": leave it out
                # until they load it again.
                missing = models_to_warm([LLM_MODEL, AMBIENT_LLM_MODEL], resident)
                if not missing:
                    continue
                # Busy queue ⇒ a model is mid-generation ⇒ warm by definition;
                # never queue a warm-load behind real work.
                try:
                    await llm_queue.acquire(priority=5, timeout=2.0)
                except _aio.CancelledError:
                    # NEVER swallow the shutdown cancel. Merged with TimeoutError
                    # below, this `continue` made the loop UNCANCELLABLE: the task
                    # absorbed the cancel, slept, and carried on, so the gather at
                    # the end of this lifespan waited on it forever and the whole
                    # process hung at "Waiting for application shutdown." while the
                    # worker kept writing to the encrypted DB. Observed 2026-08-14.
                    raise
                except _aio.TimeoutError:
                    continue
                try:
                    ok = True
                    for model in missing:
                        # Re-checked: an unload can land during the queue wait
                        # or while the previous tier loads.
                        if not models_to_warm([model]):
                            continue
                        logger.info("keep-warm: re-loading evicted model '%s'", model)
                        try:
                            ok = await _warm_one(model, log_success=False) and ok
                        except _aio.CancelledError:
                            raise
                        except Exception as e:
                            # httpx.ReadTimeout str()s to '' — say which model and
                            # which exception type, or the log reads as a blank.
                            ok = False
                            logger.warning(
                                "keep-warm: '%s' did not load within the %.0fs "
                                "budget (%s); the background lane was held for "
                                "that whole time and Tier-2 advice could not "
                                "run. Leaving the tier evicted.",
                                model, _warm_budget_seconds(),
                                type(e).__name__, )
                finally:
                    llm_queue.release()
                if ok:
                    failures = 0
                else:
                    failures += 1
                    backoff_ticks = keep_warm_backoff_ticks(failures)
                    logger.warning(
                        "keep-warm: %d consecutive failure(s) — backing off for "
                        "%d interval(s) (~%.0f min) rather than blacking out the "
                        "alert lane every %ds.",
                        failures, backoff_ticks,
                        backoff_ticks * interval_seconds / 60.0, interval_seconds)
            except _aio.CancelledError:
                raise
            except Exception as e:
                logger.debug("keep-warm ping skipped: %s", e)

    if _models_on:
        lifespan_tasks.append(_spawn(warmup_models()))
        lifespan_tasks.append(_spawn(keep_warm_loop()))

    # Pre-load the CfC risk checkpoints + forecast LSTM in the background:
    # their lazy first load (3× torch.load + MPS device init) was measured at
    # ~12s by the soak harness — paid by whichever clinician opened the FIRST
    # patient chart after boot. Warming here makes every chart open equal.
    async def warmup_torch_models():
        try:
            from services.vigil_service import _load_cfc_models
            await asyncio.to_thread(_load_cfc_models)
            from services.forecast_service import get_model
            await asyncio.to_thread(get_model)
            logger.info("CfC + forecast checkpoints pre-loaded")
        except Exception as e:
            logger.debug("torch model warmup skipped (non-fatal): %s", e)

    if _models_on:
        lifespan_tasks.append(_spawn(warmup_torch_models()))
    
    # Pre-load MedGemma 4B voice agent into Apple Silicon Unified Memory
    async def warmup_voice_agent():
        try:
            from config import VOICE
            if VOICE.get('conversational', {}).get('enabled', False):
                from services.voice_agent_service import get_voice_agent
                agent = get_voice_agent()
                await asyncio.get_event_loop().run_in_executor(None, agent.load)
                logger.info("MedGemma 4B voice agent pre-loaded (Unified Memory)")
        except Exception as e:
            logger.warning("Voice agent warmup failed (non-fatal): %s", e)
    
    if _models_on:
        lifespan_tasks.append(_spawn(warmup_voice_agent()))

    # Start Sage proactive surveillance (always on: real analysis over live data).
    # Sage insights are a model feature: off with the models.
    if _models_on:
        lifespan_tasks.append(_spawn(proactive_sweep()))
    lifespan_tasks.append(_spawn(downtime_snapshot_sweep()))
    lifespan_tasks.append(_spawn(sage_provenance_prune_sweep()))

    # ── Shadow instrument: the negatives sweeper ──
    # THE NEGATIVES DO NOT EXIST UNTIL THIS LOOP RUNS. Nothing in Orb writes a
    # row when an alert is never opened, a Sage proposal is never actioned or an
    # episode simply ends — `shadow_service.sweep_once` is the only producer of
    # `alert.expired`, `action.abandoned` and `episode.closed`. Without it the
    # scorecard silently scores the successes only, which is the single most
    # flattering failure mode the instrument has. Registered here, next to the
    # other sweeps, and asserted by tests/test_shadow_producers.py — a docstring
    # claiming "registered from main.py's lifespan" is what let its absence pass
    # review once already.
    #
    # A FAILURE TO START IS NOT "non-fatal". It is non-fatal to the HOSPITAL —
    # nothing clinical depends on the sweep — and terminal to the INSTRUMENT, so
    # it is logged at error and recorded in `shadow_service`'s own health state
    # (`sweeper_health()`, and the sweep_* counters on STATS that the scorecard's
    # instrument block already surfaces). A boot-time warning nobody re-reads is
    # how "the sweeper is not running" survived once already.
    try:
        from services.shadow_service import sweep_loop as _shadow_sweep_loop
        _shadow_sweep_interval = max(30, int(os.getenv("ORB_SHADOW_SWEEP_INTERVAL_S", "300")))
        lifespan_tasks.append(_spawn(_shadow_sweep_loop(_shadow_sweep_interval)))
        logger.info("Shadow sweeper started (every %ss)", _shadow_sweep_interval)
    except Exception as e:
        logger.error("Shadow sweeper failed to start: %s", e, exc_info=True)
        try:
            from services.shadow_service import note_sweeper_unavailable
            note_sweeper_unavailable(str(e))
        except Exception:
            logger.error("Shadow sweeper failure could not be recorded", exc_info=True)

    # ── Escalation clock ──
    # nurse → registrar → consultant only climbs when something calls
    # escalation_service.sweep. Before this loop that was a board read, so on an
    # idle appliance (nobody has Vigil open at 03:00) an unacknowledged
    # deterioration never reached the next tier. Asserted structurally by
    # tests/test_escalation_sweep_loop.py, like the shadow sweeper above.
    try:
        from services.escalation_service import (
            sweep_interval_seconds as _escalation_sweep_interval,
            sweep_loop as _escalation_sweeper,
        )
        _esc_interval = _escalation_sweep_interval()
        lifespan_tasks.append(_spawn(_escalation_sweeper(_esc_interval)))
        logger.info("Escalation sweep started (every %ss)", _esc_interval)
    except Exception as e:
        logger.error("Escalation sweep failed to start: %s", e, exc_info=True)

    # ── Event-loop starvation watchdog ──
    # /api/health cannot detect the failure this catches: health is SERVED BY the
    # loop, so when the loop stops being scheduled the check stops with it (the
    # 2026-08-01 freeze — 0.2% CPU, three identical faulthandler dumps 8 s apart,
    # needed kill -9). The watchdog lives on a daemon thread plus a GIL-free
    # C-level dead-man timer, writes a pre-diagnosed thread dump on a stall, and
    # NEVER restarts anything. Started LAST, after all the synchronous startup
    # seeds above, so a slow first boot is never mistaken for a stall.
    try:
        from services.loop_watchdog import start_watchdog
        _wd_task = start_watchdog()
        if _wd_task is not None:
            lifespan_tasks.append(_wd_task)
    except Exception as e:
        logger.warning("Loop watchdog failed to start (non-fatal): %s", e)

    # ── Fresh-install access posture ──
    # Answers two questions every boot, on its OWN daemon thread (bcrypt over the
    # staff table is ~minutes on a cold box and must never sit in front of
    # /api/health): is there a login-capable admin at all, and is any account
    # still holding one of Orb's shipped demo passwords. In the prod profile a
    # default-credential account is DEACTIVATED, not merely logged — see
    # services/bootstrap_service.py for why that ordering is deliberate even when
    # it empties the admin roster.
    try:
        from services.bootstrap_service import start_bootstrap_check
        start_bootstrap_check()
    except Exception as e:
        logger.warning("Bootstrap access check failed to start (non-fatal): %s", e)

    # ── Vitals sources ──
    # One registry owns the simulator, manual entry and (when configured) the
    # bedside-monitor feed. start_all never raises: a monitor whose VLAN is down
    # must not stop Orb booting, because manual entry lives in this same process
    # and is the ward's redundancy layer.
    try:
        _vitals_registry = _configure_vitals_sources()
        _started = await _vitals_registry.start_all()
        logger.info("Vitals sources started: %s", ", ".join(_started) or "none")
    except Exception as e:
        _vitals_registry = None
        logger.error("Vitals source configuration failed: %s", e)

    mode_label = INGESTION_MODE.upper()
    if INGESTION_MODE in ("simulation", "hybrid"):
        logger.info("Live vitals simulation (every %ss)", VITALS_INTERVAL_SECONDS)
    if INGESTION_MODE in ("ingestion", "hybrid"):
        logger.info("Ingestion endpoints active")
    logger.info("Mode: %s", mode_label)
    logger.info("API server ready on http://%s:%s", HOST, PORT)

    yield

    # Disarm the watchdog FIRST: an orderly shutdown drains the loop, and a
    # dead-man timer still armed through that drain would write a dump for a
    # stall that never happened.
    try:
        from services.loop_watchdog import stop_watchdog
        stop_watchdog()
    except Exception:
        pass

    # Close listening sockets and drain the source tasks BEFORE cancelling the
    # generic background set: a monitor feed holds a bound TCP port, and leaving
    # it open across a reload is what turns "restart Orb" into "port already in
    # use" on the appliance.
    if _vitals_registry is not None:
        try:
            await _vitals_registry.stop_all()
            await _vitals_registry.reset()
        except Exception as e:
            logger.warning("Vitals source shutdown issue: %s", e)

    await _lab_module.shutdown()

    # BOUNDED shutdown. An unbounded gather here means ONE background task that
    # swallows its cancel holds the entire appliance open — and the appliance
    # restarts for upgrades, power events and launchd. Worse, the old worker keeps
    # running (vitals simulator, alert bot) and writing to the encrypted database
    # while a replacement starts: the multi-writer hazard this repo has already
    # been burned by. Wait on the clock, then name the survivors and proceed.
    #
    # asyncio.wait, NOT wait_for: wait_for implements its timeout by cancelling
    # the AWAITING task, which forwards the cancel straight back into the task
    # that is already refusing to die. asyncio.wait returns on the clock and
    # leaves the task alone — the same reasoning as vitals_source.base.stop_task.
    for bg in lifespan_tasks:
        bg.cancel()
    if lifespan_tasks:
        _done, _pending = await asyncio.wait(lifespan_tasks, timeout=10.0)
        for task in _pending:
            logger.error(
                "shutdown: background task %s did not stop within 10s — abandoning it. "
                "It is swallowing its CancelledError; find the `except` that does not "
                "re-raise.", task.get_name() or task,
            )
    # Reap the knowledge worker child so reloads/shutdowns never leave an
    # orphaned 2-4 GB model process holding memory (the orphaned-worker class).
    try:
        from services.knowledge_worker import shutdown as _kw_shutdown
        _kw_shutdown()
    except Exception:
        pass
    logger.info("═══ Orb Hospital OS — Shutdown ═══")


# ── Create FastAPI app ──
app = FastAPI(
    title="Orb Hospital OS",
    description="Local-first hospital software API",
    version="0.1.0",
    lifespan=lifespan,
)

# ── Malformed-JSON guard ──
# Several routes call `await request.json()` directly; a malformed body raises
# json.JSONDecodeError which would otherwise surface as a 500. Return a clean 400.
import json as _json
from fastapi import Request as _Request
from fastapi.responses import JSONResponse as _JSONResponse


@app.exception_handler(_json.JSONDecodeError)
async def _json_decode_handler(request: _Request, exc: _json.JSONDecodeError):
    return _JSONResponse(status_code=400, content={"detail": "Malformed JSON in request body"})


# ── Request body-size guard ──
# JSON routes read the whole body into memory; without a cap a client could POST a
# multi-MB (or larger) body and force the server to buffer it. Reject oversized bodies
# with 413 up front. File uploads (Lens/Scribe) use their own multipart routes with a
# higher ceiling, so this cap targets the JSON API surface.
import os as _os_body
_MAX_JSON_BODY_BYTES = int(_os_body.getenv("ORB_MAX_JSON_BODY_BYTES", str(512 * 1024)))  # 512 KB


@app.middleware("http")
async def _limit_body_size(request: _Request, call_next):
    # Skip multipart/form-data (uploads) — they are bounded by their own route logic.
    ctype = request.headers.get("content-type", "")
    if not ctype.startswith("multipart/"):
        cl = request.headers.get("content-length")
        if cl is not None:
            try:
                if int(cl) > _MAX_JSON_BODY_BYTES:
                    return _JSONResponse(status_code=413, content={"detail": "Request body too large"})
            except ValueError:
                return _JSONResponse(status_code=400, content={"detail": "Invalid Content-Length"})
    return await call_next(request)


# ── Facility fence (multi-site, ruling M2) ──
# Once more than one facilities row exists, a route not yet taught to keep each
# facility's patients apart answers 503 instead of answering for every facility
# (server/facility_fence.py). With one facility (every install today) it does
# nothing but read a cached answer. Added BEFORE CORS and the security headers,
# so both wrap the 503. A second copy in the Socket.IO app covers /socket.io/
# (see socket_app below), which no FastAPI middleware sees.
from facility_fence import FacilityFence, HTTP_ROOTS as _FENCE_HTTP_ROOTS
app.add_middleware(FacilityFence, roots=_FENCE_HTTP_ROOTS)


# ── CORS ──
from config import DEPLOYMENT_PROFILE
from routes.auth_routes import MFA_ENROLL_TICKET_HEADER

# Response headers the BROWSER is allowed to read cross-origin. `allow_headers`
# governs REQUEST headers and does nothing for this — without an explicit
# expose list, fetch() silently returns null for any non-CORS-safelisted
# response header.
#
# The MFA self-enrolment ticket is the case that matters: it is the only way an
# account in ORB_MFA_REQUIRED_ROLES with no TOTP factor can ever enrol, and on a
# fresh appliance that account is the FIRST AND ONLY admin. Single-origin
# (SPA served from :8000) worked by accident; the Vite dev server on :5173 and
# any split-origin/LAN deployment did not, and the failure is invisible — the
# header is simply absent, so the UI would show its dead-end message again.
_EXPOSE_HEADERS = [MFA_ENROLL_TICKET_HEADER]

# The origin list comes from config.cors_allow_origins(), the same function the
# Trust Center reads, so the posture report describes the policy installed here
# and cannot claim an allow-list while the dev branch admits every origin.
from config import cors_allow_origins as _cors_allow_origins
_CORS_INSTALLED = _cors_allow_origins()
app.add_middleware(
    CORSMiddleware,
    allow_origins=_CORS_INSTALLED,  # dev: ["*"] for ngrok/LAN
    allow_credentials="*" not in _CORS_INSTALLED,  # Must be False if allow_origins is ["*"]
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=_EXPOSE_HEADERS,
)


# ── Security headers ──
# Pure ASGI rather than @app.middleware("http"): BaseHTTPMiddleware buffers a
# streaming body, and Sage streams its answers. Added after CORS so it wraps the
# CORS layer and preflight replies carry the headers too.
#
# The CSP is deliberately narrow (framing, plugins, <base>). index.html still
# carries an inline script, so script-src 'self' would blank the app; it can be
# tightened once that script is gone. HSTS only on a connection that is already
# HTTPS (directly, or as the proxy reports it): browsers ignore it over plain
# HTTP, and a dev box on http://localhost must not be pinned to HTTPS.
from security_headers import SecurityHeadersMiddleware as _SecurityHeadersMiddleware


app.add_middleware(_SecurityHeadersMiddleware)

# ── PHI mutation audit middleware (HIPAA §164.312(b) safety net) ──
# Logs every state-changing API request generically so no mutation goes
# unaudited, on top of the richer route-level audit events.
from services.audit_middleware import audit_mutations
app.middleware("http")(audit_mutations)


# CSRF protection for cookie-authenticated mutations (Deferred A) is enforced
# inside auth.get_current_user — the single point every session-protected route
# passes through — so it fires ONLY for requests actually authenticated by the
# session cookie, and never falsely blocks public/alt-auth endpoints (discharge
# access, X-API-Key ingestion) that a logged-in clinician's browser might POST
# to while carrying a session cookie.


# ── Register route modules ──
# ORB_MODELS_ENABLED off (or the Orb Ward Record): Sage, Sage insights and
# Lens are model features, and every route of theirs answers 404 "Switched off
# on this computer" before it reads anything (services/model_switch.py). The
# model routes inside other routers (Scribe drafting, Relay's ambient cards,
# the alert assessment, the expected course, the patient portal's questions)
# carry the same dependency themselves.
from services.model_switch import require_models_on as _require_models_on

app.include_router(auth_router)
app.include_router(vigil_router)
app.include_router(relay_router)
app.include_router(sage_router, dependencies=[Depends(_require_models_on)])
app.include_router(sage_proactive_router, dependencies=[Depends(_require_models_on)])
app.include_router(scribe_router)
app.include_router(bridge_router)
app.include_router(lens_router, dependencies=[Depends(_require_models_on)])
app.include_router(forecast_router)
app.include_router(surgery_router)
app.include_router(ingestion_router)
app.include_router(audit_router)
app.include_router(admin_router)
app.include_router(discharge_access_router)
app.include_router(helix_router)
app.include_router(pulse_router)
app.include_router(clinical_records_router)
app.include_router(model_governance_router)
app.include_router(bundle_router)
app.include_router(ipass_router)
app.include_router(downtime_router)
app.include_router(fhir_export_router)
app.include_router(pharmacy_router)
app.include_router(orders_router)
app.include_router(flight_recorder_router)
app.include_router(appointments_router)
app.include_router(search_router)
app.include_router(chart_timeline_router)
app.include_router(admin_users_router)
app.include_router(roster_import_router)
app.include_router(backup_router)
app.include_router(metrics_router)
app.include_router(impact_router)
app.include_router(compliance_router)
app.include_router(consent_router)
app.include_router(coding_router)
app.include_router(command_center_router)
app.include_router(capacity_sim_router)
app.include_router(emar_router)
app.include_router(nurse_router)
app.include_router(shadow_router)
app.include_router(ops_router)
app.include_router(frontdesk_router)
app.include_router(payer_router)
app.include_router(procurement_router)
app.include_router(dietary_router)
app.include_router(workforce_router)
app.include_router(biomedical_router)
app.include_router(housekeeping_router)
app.include_router(payments_router)
app.include_router(abdm_router)
app.include_router(billing_router)
app.include_router(nabh_router)
app.include_router(ward_record_router)

app.include_router(admission_router)

# Hospital-system build: every route below answers 404 while its switch is off.
app.include_router(adt_router)
app.include_router(lab_router)
app.include_router(pharmacy_store_router)
app.include_router(opd_router)
app.include_router(registration_router)
app.include_router(schedule_router)
# The medication order store is the drug chart, which an Orb Ward Record
# computer keeps on paper: every route answers 404 "The drug chart is not part
# of Orb Ward Record" first (services/drug_chart). Declared here, not in
# routes/medorders, which another build owns.
from services.drug_chart import require_drug_chart as _require_drug_chart
app.include_router(medorders_router, dependencies=[Depends(_require_drug_chart)])
# Facility identity: configuration, no patient data, open through the facility
# fence (facility_fence.OPEN_SURFACES).
app.include_router(facility_router)

# ── Serve uploaded media (PHI) behind authentication ──
# SECURITY: uploads contain medical images (Lens) and consultation audio
# (Scribe) — both PHI. The previous public StaticFiles mount served the entire
# directory to any unauthenticated client. We now require a valid JWT, resolve
# the file back to its owning patient (registered-media allowlist), enforce the
# caller's ward scope against that patient, audit the access fail-closed, and
# stream the file from disk, rejecting any path-traversal attempt.
import os as _os
from fastapi.responses import FileResponse
from fastapi import HTTPException
from config import UPLOAD_DIR

if not UPLOAD_DIR.exists():
    UPLOAD_DIR.mkdir(parents=True)

_UPLOAD_ROOT = UPLOAD_DIR.resolve()


def _resolve_media_patient(filename: str):
    """Map an uploads filename back to its owning patient via the media records
    that reference it (Lens images, Scribe audio). Returns (patient_id, kind) or
    (None, None) if the file is not a registered PHI media record."""
    from database import db_session
    base = _os.path.basename(filename)
    like = f"%{base}"
    with db_session() as db:
        row = db.execute(
            "SELECT patient_id FROM lens_analyses WHERE image_path LIKE ? LIMIT 1", (like,)
        ).fetchone()
        if row:
            return row["patient_id"], "lens"
        row = db.execute(
            "SELECT patient_id FROM scribe_notes WHERE audio_path LIKE ? LIMIT 1", (like,)
        ).fetchone()
        if row:
            return row["patient_id"], "scribe"
    return None, None


@app.get("/uploads/{filename:path}")
def serve_upload(filename: str, user: dict = Depends(get_current_user)):
    """Authenticated, patient-scoped, audited access to uploaded PHI media.

    Requires a valid staff JWT, refuses path traversal, and now:
      - serves ONLY files that are registered PHI media (a row in lens_analyses
        or scribe_notes references them) — an arbitrary file dropped in uploads/
        is no longer served;
      - enforces the caller's ward scope against the patient who OWNS the media
        (enforce_patient_scope — the same derivation and 404 refusal as every
        other patient-addressed surface); and
      - records an audit event for every media access, resolved to the owning
        patient, so PHI image/audio views are traceable (they were not before).

    DELIBERATELY `def`, NOT `async def`. Every statement in this handler blocks:
    two encrypted-SQLite lookups (_resolve_media_patient), a fail-closed audit
    write, and a whole-file AES decrypt of media that can run to many MB. It
    awaits nothing, so FastAPI runs a `def` endpoint in the anyio worker
    threadpool for free and the event loop never sees any of it.

    Why not `run_db` hops instead: (a) it would need an `await` inserted between
    the fail-closed audit write and the read that the write authorises, where
    today there is none — a suspension point in the middle of a safety control;
    (b) run_db's private pool is FOUR workers sized for short DB statements, so
    pushing a multi-MB radiology decrypt through it would convoy every
    interactive query in the appliance behind one image download. The bulk
    decrypt belongs in the general threadpool; the fail-closed ordering is
    preserved verbatim because nothing about the code moved.
    """
    if user.get("role") not in ("doctor", "nurse", "admin"):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    target = (_UPLOAD_ROOT / filename).resolve()
    try:
        target.relative_to(_UPLOAD_ROOT)
    except ValueError:
        raise HTTPException(status_code=404, detail="Not found")
    if not target.is_file():
        raise HTTPException(status_code=404, detail="Not found")

    # Registered-media allowlist: only serve files that belong to a clinical record.
    patient_id, kind = _resolve_media_patient(filename)
    if patient_id is None and kind is None:
        raise HTTPException(status_code=404, detail="Not found")

    # Ward scope: may THIS caller see the patient who owns the media? Same
    # derivation and refusal as every other patient-addressed surface — 404,
    # never 403 (a 403 confirms the patient exists), and an unscoped governance
    # account (department None) still passes. Blocking is fine in this plain
    # `def` route (threadpooled), and it MUST run before the audit write below
    # so a denied caller leaves no media_view/success row for media they never
    # received. The refusal is re-worded to this route's uniform "Not found" so
    # an out-of-scope caller cannot even learn that the filename maps to a
    # registered clinical record.
    from permissions import enforce_patient_scope
    try:
        enforce_patient_scope(user, patient_id)
    except HTTPException:
        raise HTTPException(status_code=404, detail="Not found")

    # Audit the PHI media access (resolved to the owning patient). phi_access is
    # a FAIL-CLOSED audit category: if the access cannot be durably recorded we
    # must NOT serve the PHI (that is the whole point of the control), so an
    # AuditWriteError aborts the request rather than being swallowed.
    from services.audit_service import log_audit_event, AuditWriteError
    import uuid as _uuid
    try:
        log_audit_event(
            correlation_id=str(_uuid.uuid4()),
            event_category="phi_access",
            action_type="media_view",
            outcome="success",
            summary_message=f"{kind} media accessed: {_os.path.basename(filename)}",
            actor_user_id=user.get("id"),
            actor_role=user.get("role"),
            patient_id=patient_id,
            target_type="upload_media",
            target_id=_os.path.basename(filename),
        )
    except AuditWriteError:
        raise HTTPException(status_code=503, detail="Audit temporarily unavailable; media not served")

    # Force download/non-executable handling to blunt stored-XSS via served HTML/SVG.
    # PHI media is encrypted at rest (crypto_service); decrypt transparently for
    # the authorized caller. read_decrypted passes plaintext files through
    # unchanged, so this is safe for legacy (pre-encryption) uploads too.
    from services.crypto_service import read_decrypted
    from fastapi.responses import Response
    try:
        body = read_decrypted(str(target))
    except ValueError:
        # Generic message — do not leak internal crypto exception detail. The
        # specific failure (tamper / wrong key / missing driver) is logged.
        logger.warning("Upload decrypt failed for %s", _os.path.basename(filename))
        raise HTTPException(status_code=503, detail="Media temporarily unavailable")
    return Response(
        content=body,
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{_os.path.basename(filename)}"'},
    )
# ── Auth-protected /me endpoint ──
@app.get("/api/auth/me", response_model=UserOut)
async def get_me(user: dict = Depends(get_current_user)):
    # The same body the sign-in lanes return, facility included (multisite-5a).
    # The facility label may read the facilities tables on a cache miss, so it
    # is built off the event loop.
    from database import run_db
    from routes.auth_routes import user_out
    return await run_db(user_out, user)

# ── Health check ──
@app.get("/api/health")
async def health():
    """Liveness, plus the deployment's module switch board.

    `modules` is the ONE place the frontend learns which hospital-administration
    modules this site runs (config.enabled_modules — billing/payments/payer/
    procurement/frontdesk, all default ON). It rides on the existing health
    endpoint rather than a new /api/config because this is the only unauthenticated
    endpoint the shell can read before a session exists, and the shell has to know
    which tabs to build at first paint. It carries no PHI and no secret: it is a
    list of which screens this appliance offers.

    `modules` also carries the opt-in modules (adt, pharmacy-store, lab; default
    OFF), true while 'on' or 'draining'. `module_states` gives each opt-in
    module's state word, and `features` every feature flag of the
    hospital-system build (default OFF, prerequisites applied). `platform`
    (multi-site) is {multi_site, multi_facility_data}: the ORB_MULTI_SITE
    switch and whether more than one facility is on record, the second read
    from facility_service's cache. Built from config and cached values only:
    this endpoint never opens a database connection.
    """
    from config import enabled_modules, feature_flags, module_states
    from services.facility_service import platform_flags
    return {
        "status": "ok",
        "service": "orb-hospital-os",
        "version": "0.1.0",
        "modules": enabled_modules(),
        "module_states": module_states(),
        "features": feature_flags(),
        "clinical": clinical_switches(),
        "platform": platform_flags(),
    }


def clinical_switches() -> dict:
    """The clinical switches the front end hides screens by (Orb Ward Record):
    ORB_SCORING_ENABLED, ORB_LENS_ENABLED, ORB_SEPSIS_AUTOSTART,
    ORB_MODELS_ENABLED and ORB_WARD_RECORD (the profile, which forces the other
    four off). Read from config at call time. Five booleans, no PHI, safe on the
    unauthenticated health endpoint. src/shellConfig.tsx
    currentClinicalSwitches() reads it."""
    import config
    return {
        "scoring_enabled": bool(getattr(config, "SCORING_ENABLED", True)),
        "lens_enabled": bool(getattr(config, "LENS_ENABLED", False)),
        "sepsis_autostart": bool(getattr(config, "SEPSIS_AUTOSTART", True)),
        "models_enabled": bool(getattr(config, "MODELS_ENABLED", True)),
        "ward_record": bool(getattr(config, "WARD_RECORD", False)),
    }


@app.get("/api/health/loop")
async def health_loop():
    """Event-loop watchdog state — stall history, not liveness.

    A 200 here CANNOT prove the loop is alive; it is served by the loop, so
    during a stall the request simply never returns. What it gives an operator
    is the record: how many stalls this process has seen, how long the worst was
    and where the automatic thread dump was written. The two channels that DO
    survive a frozen loop are out-of-process: the watchdog's status.json (written
    by a daemon thread) and the dead-man dump file (written by a GIL-free C
    thread). Both paths are reported in this payload so whoever is checking the
    box knows which files to open. Unauthenticated, same as /api/health, since
    it carries no PHI.
    """
    from services.loop_watchdog import watchdog_status
    return watchdog_status()


from fastapi import HTTPException as _HTTPException
from pydantic import BaseModel as _BaseModel
from permissions import require_permission


@app.get("/api/health/access")
def health_access(user: dict = Depends(require_permission("admin.overview"))):
    """Fresh-install access posture, for an admin.

    Answers the two questions services/bootstrap_service.py checks at every
    boot — is there a login-capable administrator, and is any staff account
    still holding one of Orb's shipped demo passwords.

    Admin-gated because it enumerates the appliance's weak points, and a plain
    `def` (FastAPI runs it in the threadpool) because reading the snapshot is
    trivial but must never be tempted onto the loop as it grows. It returns the
    CACHED bootstrap snapshot; it never triggers a bcrypt sweep, so it cannot be
    used to make the box do minutes of work per request.
    """
    from services.bootstrap_service import bootstrap_snapshot
    return {"access": bootstrap_snapshot()}


@app.get("/api/health/models")
async def health_models(user: dict = Depends(get_current_user)):
    """Warmth of the local inference tiers, for the shell's AI-status chip.

    Reports which configured models are resident in Ollama right now (via
    /api/ps) so clinicians can see at a glance whether the first AI response
    will be instant (warm) or pay a cold-load. Authenticated: model topology is
    deployment metadata, not for anonymous callers."""
    import httpx
    from config import OLLAMA_BASE_URL, LLM_MODEL, AMBIENT_LLM_MODEL
    from services.model_switch import models_enabled
    if not models_enabled():
        # Switched off on this computer: nothing is asked of Ollama, and the
        # chip has no tier to report.
        return {"ollama_reachable": False, "models_enabled": False, "models": []}
    configured = list(dict.fromkeys([LLM_MODEL, AMBIENT_LLM_MODEL]))
    loaded: dict = {}
    reachable = True
    try:
        async with httpx.AsyncClient(timeout=3.0, trust_env=False) as client:
            resp = await client.get(f"{OLLAMA_BASE_URL}/api/ps")
            resp.raise_for_status()
            for m in resp.json().get("models", []):
                loaded[m.get("name", "")] = {
                    "size_vram": m.get("size_vram") or m.get("size"),
                    "expires_at": m.get("expires_at"),
                }
    except Exception:
        reachable = False
    return {
        "ollama_reachable": reachable,
        "models_enabled": True,
        "models": [
            {
                "model": name,
                "role": "primary" if name == LLM_MODEL else "ambient",
                "warm": name in loaded,
                # Unloaded by an administrator: background warming leaves it
                # alone, so the chip must not call it "warming up".
                "held": name in _OPERATOR_UNLOADED,
                "detail": loaded.get(name),
                "impacts": _model_impacts(name),
            }
            for name in configured
        ],
    }


# What unloading a tier actually does, feature by feature. Shown in the unload
# confirmation so nobody frees memory without knowing the cost. It used to say
# every listed feature "stops working", which was wrong both ways: Sage answers
# on the smaller model (labelled), and the lanes that ask for a model by name
# load it straight back. `_model_impacts` adds those for this configuration.
MODEL_IMPACTS = {
    # The careful tier, while the router can answer on the smaller one.
    "primary": [
        "Sage answers come from the smaller model, and each answer says so",
        "Sage does not suggest next steps for Vigil alerts",
        "Bridge tells patients the assistant is unavailable and to ask their nurse",
        "Discharge summaries hold the recorded data only, with no drafted hospital course",
    ],
    "ambient": [
        "Vigil alerts in Relay carry a standard monitoring line instead of a drafted recommendation",
        "Relay stops reading case-room messages for suggested actions and room memory; rule-based action cards still appear",
        "The Sage ward brief keeps its ward figures current but stops rewriting its summary",
    ],
}


def _model_impacts(name: str) -> list[str]:
    """MODEL_IMPACTS for the tier(s) `name` serves, plus the features that ask
    for it by name and so load it again on first use."""
    from config import (
        AMBIENT_LLM_MODEL, LLM_MODEL, MODEL_FALLBACK_ENABLED, SCRIBE,
        THINKING_FAST_MODE_ENABLED, THINKING_FAST_MODEL, VISION_MODEL,
    )
    impacts: list[str] = []
    reloads: list[str] = []
    if name == LLM_MODEL:
        if MODEL_FALLBACK_ENABLED and AMBIENT_LLM_MODEL != LLM_MODEL:
            impacts += MODEL_IMPACTS["primary"]
        else:
            # No smaller tier to answer on (or fallback is switched off), so the
            # router keeps asking for this model.
            reloads.append("Sage, Bridge and discharge summaries")
    if name == AMBIENT_LLM_MODEL:
        impacts += MODEL_IMPACTS["ambient"]
    if name == VISION_MODEL:
        reloads.append("Lens image analysis")
    for stage, label in (("draft", "Scribe note structuring"),
                         ("correction", "Scribe transcript correction"),
                         ("verify", "Scribe note checking")):
        cfg = SCRIBE.get(stage) or {}
        if (cfg.get("enabled", True) and cfg.get("provider") == "ollama"
                and cfg.get("model") == name):
            reloads.append(label)
    if THINKING_FAST_MODE_ENABLED and THINKING_FAST_MODEL == name and name != LLM_MODEL:
        reloads.append("Sage fast mode")
    if reloads:
        joined = reloads[0] if len(reloads) == 1 else ", ".join(reloads[:-1]) + " and " + reloads[-1]
        impacts.append(f"{joined} load it again on first use, which can take a minute or two")
    return impacts


def _invalidate_router_cache() -> None:
    """Drop the model router's residency snapshot after an operator load/unload.

    Without this the router could keep routing on a one-second-stale view of
    what Ollama holds — e.g. still sending turns to a model that was just
    evicted."""
    try:
        from services.model_router import invalidate_residency_cache
        invalidate_residency_cache()
    except Exception:
        logger.debug("model router cache invalidation skipped", exc_info=True)


class ModelActionRequest(_BaseModel):
    model: str


def _resolve_configured_model(name: str) -> str:
    """Only the CONFIGURED tiers may be loaded/unloaded.

    Without this allowlist the endpoint would be an arbitrary
    load-anything-on-the-host primitive (memory-exhaustion DoS, and a way to
    pull unvetted weights into a clinical deployment)."""
    from config import LLM_MODEL, AMBIENT_LLM_MODEL
    allowed = list(dict.fromkeys([LLM_MODEL, AMBIENT_LLM_MODEL]))
    if name not in allowed:
        raise _HTTPException(
            status_code=400,
            detail=f"'{name}' is not a configured Orb model. Allowed: {', '.join(allowed)}",
        )
    return name


@app.post("/api/health/models/load", dependencies=[Depends(_require_models_on)])
async def load_model(req: ModelActionRequest, user: dict = Depends(require_permission("admin.overview"))):
    """Pull a configured tier into memory and pin it (keep_alive from config)."""
    import httpx
    from config import OLLAMA_BASE_URL, OLLAMA_KEEP_ALIVE, LLM_MODEL
    from services.sage_service import INTERACTIVE_NUM_CTX
    model = _resolve_configured_model(req.model)
    # Same window the live path uses for this tier — see _warm_one. Loading the
    # interactive tier at 4096 here would mean an operator "loading" the model
    # actually queued a runner swap for the next clinician turn.
    _load_ctx = INTERACTIVE_NUM_CTX if model == LLM_MODEL else 4096
    # Cleared first, whatever Ollama answers: the administrator wants this tier
    # back, so background warming may retry it if this load fails.
    _OPERATOR_UNLOADED.discard(model)
    try:
        async with httpx.AsyncClient(timeout=300.0, trust_env=False) as client:
            resp = await client.post(
                f"{OLLAMA_BASE_URL}/api/chat",
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": "hi"}],
                    "stream": False,
                    "keep_alive": OLLAMA_KEEP_ALIVE,
                    "options": {"num_predict": 1, "num_ctx": _load_ctx},
                },
            )
    except Exception as e:
        raise _HTTPException(status_code=503, detail=f"Ollama unreachable: {e}")
    if resp.status_code != 200:
        raise _HTTPException(status_code=502, detail=f"Ollama returned HTTP {resp.status_code} loading '{model}'")
    _invalidate_router_cache()
    logger.info("[Models] '%s' loaded on request by %s", model, user.get("id"))
    return {"status": "loaded", "model": model}


@app.post("/api/health/models/unload", dependencies=[Depends(_require_models_on)])
async def unload_model(req: ModelActionRequest, user: dict = Depends(require_permission("admin.overview"))):
    """Evict a configured tier from memory.

    keep_alive=0 is Ollama's documented "unload now" signal. Non-destructive:
    the weights stay on disk. The tier is held unloaded (`_OPERATOR_UNLOADED`)
    so background warming does not bring it back; only a load clears that.
    """
    import httpx
    from config import OLLAMA_BASE_URL
    model = _resolve_configured_model(req.model)
    # Held BEFORE the unload is sent, so a keep-warm pass already in flight
    # re-checks and skips it rather than reloading it straight after.
    _OPERATOR_UNLOADED.add(model)
    try:
        async with httpx.AsyncClient(timeout=60.0, trust_env=False) as client:
            resp = await client.post(
                f"{OLLAMA_BASE_URL}/api/chat",
                json={"model": model, "messages": [], "keep_alive": 0},
            )
    except Exception as e:
        _OPERATOR_UNLOADED.discard(model)
        raise _HTTPException(status_code=503, detail=f"Ollama unreachable: {e}")
    if resp.status_code != 200:
        _OPERATOR_UNLOADED.discard(model)
        raise _HTTPException(status_code=502, detail=f"Ollama returned HTTP {resp.status_code} unloading '{model}'")

    # Ollama's unload is ASYNCHRONOUS: the runner enters a "Stopping..." state
    # and the model keeps appearing in /api/ps for seconds afterwards. Returning
    # immediately made "unloaded" a lie — a request arriving in that window saw
    # the model as resident, routed to it, and paid a full cold reload. Wait for
    # it to actually leave /api/ps so the reported state is the real state.
    freed = False
    for _ in range(60):  # ~30s ceiling; a 17 GB teardown is normally 1-3s
        await asyncio.sleep(0.5)
        try:
            async with httpx.AsyncClient(timeout=3.0, trust_env=False) as client:
                ps = await client.get(f"{OLLAMA_BASE_URL}/api/ps")
                ps.raise_for_status()
                if model not in {m.get("name") for m in ps.json().get("models", [])}:
                    freed = True
                    break
        except Exception:
            break  # can't confirm; fall through and report what we know
    _invalidate_router_cache()
    logger.info("[Models] '%s' unloaded on request by %s (confirmed=%s)", model, user.get("id"), freed)
    return {"status": "unloaded", "model": model, "confirmed": freed}

# (uploads now served via the authenticated /uploads/{filename} route above)

# ── Wrong-entrypoint tripwire (see also the argv guard by socket_app below) ──
# The ASGI target MUST be `main:socket_app`, never `main:app`. socketio.ASGIApp
# claims every /socket.io/* path and hands the rest to FastAPI, so under the
# CORRECT entrypoint the route below is physically unreachable — Socket.IO
# answers first and FastAPI never sees the request.
#
# Under `main:app` there is no such interception, so /socket.io/* falls through
# to FastAPI. Before this route that meant a plain 404: the client's polling
# handshake failed silently, live vitals / Relay / wrong-room warnings simply
# never arrived, and nothing anywhere said why. This turns that silence into a
# 500 with the fix in the body, on the very first handshake attempt.
#
# Registered BEFORE the SPA mount so the catch-all static route can't shadow it.
@app.api_route(
    "/socket.io/{_path:path}",
    methods=["GET", "POST", "OPTIONS"],
    include_in_schema=False,
)
async def _socketio_wrong_entrypoint(_path: str):
    logger.error(
        "WRONG ENTRYPOINT: a /socket.io/ request reached FastAPI, which means the "
        "server was started as `main:app`. ALL realtime is dead (live vitals, Relay "
        "messages, wrong-room warnings). Restart as `main:socket_app` — or ./run-orb.sh."
    )
    return _JSONResponse(
        status_code=500,
        content={
            "detail": (
                "Orb was started as `main:app`. Socket.IO is not mounted, so all "
                "realtime is dead. Restart with `python main.py`, `./run-orb.sh`, or "
                "`uvicorn main:socket_app`, never `uvicorn main:app`."
            ),
            "error": "wrong_asgi_entrypoint",
            "expected": "main:socket_app",
        },
    )


# ── Serve the built SPA (production single-origin deploy) ──
# Previously NO shipped path served the frontend: main.py mounted no static
# route, docker-compose had no proxy, and install.sh started only the backend —
# so the UI was reachable only via `npm run dev`. When a production build exists
# at ../dist we now serve it from the API origin with SPA history fallback, so a
# single `uvicorn main:socket_app` serves the whole product. This mount is
# registered LAST (after every API router and the authenticated /uploads route),
# and StaticFiles only handles GETs that didn't match an earlier route, so it
# can't shadow /api, /uploads, or /socket.io. Absent (dev), it's simply skipped.
from config import BASE_DIR as _BASE_DIR
_DIST_DIR = _BASE_DIR.parent / "dist"


# ── The computer's switches, written into the page (Orb Ward Record, 9 Oct 2026) ──
# The client used to assume every clinical switch was on until /api/health
# answered, so a Ward Record computer showed the full rail and the score widgets
# for a moment after sign-in or a reload. index.html now carries the switches in
# an inline script that runs before the app's first module script, so the very
# first render already knows them (src/shellConfig.tsx reads
# window.__ORB_SWITCHES__). The health probe still runs and still wins.
def spa_switches() -> dict:
    """The same clinical, modules and features maps /api/health publishes."""
    from config import enabled_modules, feature_flags
    return {
        "clinical": clinical_switches(),
        "modules": enabled_modules(),
        "features": feature_flags(),
    }


from spa_bootstrap import spa_switches_script, spa_index_with_switches


_SPA_INDEX_TEXT: str | None = None
# The (modified time, size) of the index.html read into _SPA_INDEX_TEXT. A
# rebuild while the server runs (npm run build empties dist/) replaces the file
# and deletes the old hashed chunks; a copy held for the life of the process
# would keep naming chunks that are gone, and every screen would load blank
# until a restart. The file is read again only when it changed.
_SPA_INDEX_STAMP: tuple[int, int] | None = None


def _spa_index_response():
    """index.html for this request: the file is read once per build (again
    only when it changes on disk), the switches are written in per request.
    Never a 304, so a browser cannot keep a page that carries switches from
    before a restart."""
    global _SPA_INDEX_TEXT, _SPA_INDEX_STAMP
    from starlette.responses import HTMLResponse
    index = _DIST_DIR / "index.html"
    stat_now = index.stat()
    stamp = (stat_now.st_mtime_ns, stat_now.st_size)
    if _SPA_INDEX_TEXT is None or _SPA_INDEX_STAMP != stamp:
        _SPA_INDEX_TEXT = index.read_text(encoding="utf-8")
        _SPA_INDEX_STAMP = stamp
    return HTMLResponse(spa_index_with_switches(_SPA_INDEX_TEXT, spa_switches()),
                        headers={"Cache-Control": "no-cache"})


if _DIST_DIR.is_dir():
    from starlette.staticfiles import StaticFiles as _StaticFiles
    from starlette.responses import FileResponse as _FileResponse
    from starlette.exceptions import HTTPException as _StarletteHTTPException

    def _should_spa_fallback(path: str) -> bool:
        # Fall back to index.html only for client-route-shaped paths: no file
        # extension in the last segment, and NOT an API/asset/socket path (an
        # unmatched /api/* must keep its JSON 404, not be masked by the SPA).
        if path.startswith(("api", "api/", "uploads", "uploads/", "socket.io", "socket.io/")):
            return False
        return "." not in path.rsplit("/", 1)[-1]

    class _SpaStaticFiles(_StaticFiles):
        """StaticFiles that falls back to index.html for client-side routes
        (e.g. /dashboard, /bridge) so a hard refresh / deep link works, while
        still 404-ing missing asset files and unmatched API paths. Starlette's
        StaticFiles RAISES 404 for a missing path rather than returning it, so
        the fallback is caught here.

        Every path that serves index.html serves it with the computer's
        switches written in: the SPA fallback below, and file_response, which
        is how StaticFiles serves "/" (html=True) and "/index.html"."""
        def file_response(self, full_path, stat_result, scope, status_code=200):
            if os.path.realpath(full_path) == os.path.realpath(_DIST_DIR / "index.html"):
                return _spa_index_response()
            return super().file_response(full_path, stat_result, scope, status_code)

        async def get_response(self, path, scope):
            try:
                response = await super().get_response(path, scope)
            except _StarletteHTTPException as exc:
                if exc.status_code == 404 and _should_spa_fallback(path):
                    return _spa_index_response()
                raise
            if response.status_code == 404 and _should_spa_fallback(path):
                return _spa_index_response()
            # Cache policy: hashed build assets are content-addressed → immutable.
            # Everything else (index.html, sw.js, registerSW.js, workbox-*.js,
            # manifest.webmanifest, icons) must revalidate every time, otherwise a
            # stale cached index.html can reference chunks a rebuild has deleted.
            if path.startswith("assets/"):
                response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
            else:
                response.headers["Cache-Control"] = "no-cache"
            return response

    app.mount("/", _SpaStaticFiles(directory=str(_DIST_DIR), html=True), name="spa")
    logger.info("Serving built SPA from %s (single-origin deploy)", _DIST_DIR)
else:
    logger.info("No ../dist build found — SPA not served (dev mode: use `npm run dev`)")

# ── Mount Socket.IO, behind the Host allow-list ──
# HostGate wraps the outermost app: socketio.ASGIApp answers /socket.io/ before any
# FastAPI middleware runs, so this is the one place a check covers REST and the
# socket. It refuses a Host name Orb was not told about (DNS rebinding); see
# services/host_gate.py and config.allowed_host_names().
from services.host_gate import HostGate
# The Socket.IO app carries the facility fence for /socket.io/ (a subclass of
# socketio.ASGIApp, facility_fence.fenced_socketio_app); /api/ and /uploads/
# are fenced inside the FastAPI app.
from facility_fence import fenced_socketio_app as _fenced_socketio_app
socket_app = HostGate(_fenced_socketio_app(sio, app))


# ── Refuse to boot on the wrong ASGI target ──
# Companion to the /socket.io/ tripwire above. That one is a backstop that fires
# on the first handshake; this one fails at import, before a single request is
# served, because "the demo silently has no realtime" is not a state worth
# reaching. Scoped narrowly to an actual uvicorn/gunicorn/hypercorn invocation
# naming `main:app` on the command line, so importing main (pytest, tooling,
# `from main import app` in ~20 test modules) is completely unaffected.
def _assert_correct_asgi_entrypoint() -> None:
    import sys as _sys

    argv = _sys.argv
    joined = " ".join(argv)
    server_cli = any(
        s in joined for s in ("uvicorn", "gunicorn", "hypercorn")
    )
    # Match the target token exactly — `main:app` must fail, `main:socket_app`
    # must not, and a substring test would wrongly flag the latter.
    names_bare_app = any(
        tok == "main:app" or tok.endswith(("=main:app", " main:app"))
        for tok in argv
    )
    if server_cli and names_bare_app:
        raise RuntimeError(
            "\n"
            "══════════════════════════════════════════════════════════════════\n"
            "  Orb started with the WRONG ASGI target: `main:app`\n"
            "══════════════════════════════════════════════════════════════════\n"
            "  `main:app` is the bare FastAPI app. Socket.IO is NOT mounted on\n"
            "  it, so every /socket.io/ request 404s and ALL realtime dies\n"
            "  silently, live vitals, Relay messages, wrong-room warnings.\n"
            "\n"
            "  Use one of:\n"
            "      ./run-orb.sh                 (recommended, whole stack)\n"
            "      python main.py               (backend only)\n"
            "      uvicorn main:socket_app ...  (manual)\n"
            "══════════════════════════════════════════════════════════════════\n"
        )


_assert_correct_asgi_entrypoint()


if __name__ == "__main__":
    import os
    import uvicorn
    from pathlib import Path

    # TLS in transit (HIPAA §164.312(e)). Opt-in: set ORB_TLS_CERT + ORB_TLS_KEY
    # to serve HTTPS directly (otherwise terminate TLS at a reverse proxy — see
    # docs/DEPLOYMENT.md). PHI + JWTs must never traverse an untrusted network in
    # cleartext; in prod one of the two must be true.
    tls_cert = os.getenv("ORB_TLS_CERT")
    tls_key = os.getenv("ORB_TLS_KEY")
    ssl_kwargs = {}
    if tls_cert and tls_key:
        ssl_kwargs = {"ssl_certfile": tls_cert, "ssl_keyfile": tls_key}
        logger.info("TLS enabled (serving HTTPS on %s:%s)", HOST, PORT)
    elif DEPLOYMENT_PROFILE == "prod":
        logger.warning(
            "Running in prod profile WITHOUT ORB_TLS_CERT/ORB_TLS_KEY — ensure TLS "
            "is terminated at a reverse proxy; PHI must not travel in cleartext."
        )

    # Auto-reload is a dev convenience only — in prod it doubles memory (two
    # process trees) and reloads on any file touch. Gate it on the profile.
    is_dev = DEPLOYMENT_PROFILE != "prod"
    # Excludes MUST be absolute paths to existing directories: uvicorn's FileFilter
    # only turns those into exclude_dirs (matched with `dir in path.parents`).
    # A glob like ".venv/*" stays a right-anchored PurePath.match pattern that
    # matches only .venv's DIRECT children — so every site-packages .py still
    # passed the "*.py" include and any pip/uv install triggered a reload storm.
    # server/models/ is deliberately NOT excluded: it holds app source that should
    # still trigger reload.
    _SRV = Path(__file__).resolve().parent
    uvicorn.run(
        "main:socket_app",
        host=HOST,
        port=PORT,
        reload=is_dev,
        reload_excludes=[
            str(_SRV / ".venv"),
            str(_SRV / "uploads"),
            str(_SRV / "adapters"),
            "*.db",
        ],
        log_level="info",
        **ssl_kwargs,
    )
