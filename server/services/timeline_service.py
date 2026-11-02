"""Orb Vigil — Sage-deduced clinical timeline projection.

Instead of drawing one node per calendar day since admission (which made a simple
2-3 day illness look like a 40-day stay because of the seeded admission date), this
asks MedGemma to reason about the *expected clinical course* from the diagnosis,
comorbidities, age, and sex, and returns an ordered set of care phases plus an
expected length of stay. The projection is stable for a given clinical picture, so
it is cached per patient and only recomputed when those inputs change.

This is background work, so the generation runs on the AMBIENT tier
(config.AMBIENT_LLM_MODEL). Measured on the appliance 2026-07-31, this exact
prompt holds Orb's single non-preemptive LLM slot for 30.1 s on medgemma:27b
against 4.3 s on medgemma:4b — it is the longest background occupant in the
system and the most likely source of the ~31 s queue wait a clinician's Sage
turn was measured sitting behind. Projection quality is traded for that wait;
the deterministic heuristic projection remains the floor whenever generation is
unavailable.

The patient's *current position* on that course is derived separately from their
live risk level (acute → early phase, stable → near discharge), so it stays fresh
without invalidating the cached projection and does not depend on the wall-clock
admission date.
"""

import asyncio
import hashlib
import json
import logging
import time

import httpx
from database import db_session, run_db

from config import AMBIENT_LLM_MODEL, OLLAMA_KEEP_ALIVE
from services.local_model_http import local_model_ssl_context

logger = logging.getLogger(__name__)


def _ensure_table(db) -> None:
    db.execute(
        """CREATE TABLE IF NOT EXISTS timeline_projections (
               patient_id TEXT PRIMARY KEY,
               input_hash TEXT NOT NULL,
               projection_json TEXT NOT NULL,
               source TEXT NOT NULL DEFAULT 'llm',
               created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
           )"""
    )


def _load_clinical_inputs(db, patient_id: str):
    patient = db.execute(
        "SELECT name, diagnosis, age, sex FROM patients WHERE id = ?", (patient_id,)
    ).fetchone()
    if not patient:
        return None
    prof = db.execute(
        "SELECT comorbidities FROM patient_profiles WHERE patient_id = ?", (patient_id,)
    ).fetchone()
    comorbidities: list[str]
    comorbidities = []
    if prof and prof["comorbidities"]:
        try:
            comorbidities = json.loads(prof["comorbidities"]) or []
        except json.JSONDecodeError:
            comorbidities = []
    return {
        "name": patient["name"],
        "diagnosis": patient["diagnosis"] or "Unspecified condition",
        "age": patient["age"],
        "sex": patient["sex"],
        "comorbidities": comorbidities,
    }


def _input_hash(inputs: dict) -> str:
    key = json.dumps(
        {
            "dx": inputs["diagnosis"],
            "age": inputs["age"],
            "sex": inputs["sex"],
            "cmb": sorted(inputs["comorbidities"]),
        },
        sort_keys=True,
    )
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def _heuristic_projection(inputs: dict) -> dict:
    """Transparent fallback when the LLM is unavailable or returns unusable output.
    A generic acute-care arc, lengthened when comorbidities are present."""
    base = 4
    bump = min(len(inputs["comorbidities"]), 4)
    los = base + bump
    return {
        "expected_los_days": los,
        "phases": [
            {
                "name": "Stabilization",
                "focus": "Resuscitate, correct acute derangements, start targeted therapy",
                "typical_days": "Day 0–1",
            },
            {
                "name": "Active Treatment",
                "focus": "Treat the primary condition; monitor response and labs",
                "typical_days": f"Day 1–{max(2, los - 2)}",
            },
            {
                "name": "Recovery",
                "focus": "Step down therapy, mobilize, confirm clinical stability",
                "typical_days": f"Day {max(2, los - 2)}–{los}",
            },
            {
                "name": "Discharge Readiness",
                "focus": "Meet discharge criteria, arrange follow-up and medications",
                "typical_days": f"~Day {los}",
            },
        ],
        "discharge_criteria": [
            "Hemodynamically stable off escalating support",
            "Primary condition controlled and improving",
            "Tolerating oral intake and medications",
        ],
        "rationale": (
            f"Baseline course for {inputs['diagnosis']}"
            + (
                f", extended for comorbidities ({', '.join(inputs['comorbidities'])})."
                if inputs["comorbidities"]
                else "."
            )
        ),
    }


def _build_prompt(inputs: dict) -> str:
    cmb = ", ".join(inputs["comorbidities"]) if inputs["comorbidities"] else "none reported"
    return (
        "You are Orb Sage, a clinical planning assistant. Deduce the EXPECTED inpatient "
        "clinical course for this patient and return it as STRICT JSON only (no prose, no "
        "markdown fences).\n\n"
        f"Diagnosis: {inputs['diagnosis']}\n"
        f"Age: {inputs['age']}\n"
        f"Sex: {inputs['sex']}\n"
        f"Comorbidities: {cmb}\n\n"
        "Adjust the expected length of stay and phases for the diagnosis, and lengthen or "
        "add caution for the age and comorbidities.\n\n"
        "RULES:\n"
        '- The "phases" array MUST contain 3 to 5 ordered phases spanning admission to discharge '
        "(stabilization → active treatment → recovery/step-down → discharge readiness). Never return "
        "fewer than 3 phases.\n"
        '- Keep each "focus" to one short clause. Put target values in discharge_criteria, not in phases.\n\n'
        "EXAMPLE (for a DIFFERENT condition, community-acquired pneumonia, so do NOT copy it, follow its shape):\n"
        "{\n"
        '  "expected_los_days": 5,\n'
        '  "phases": [\n'
        '    {"name": "Stabilization", "focus": "Oxygen, IV antibiotics, sepsis screen", "typical_days": "Day 0-1"},\n'
        '    {"name": "Active Treatment", "focus": "Continue antibiotics, monitor response", "typical_days": "Day 1-3"},\n'
        '    {"name": "Step-Down & Recovery", "focus": "Switch to oral antibiotics, wean oxygen, mobilize", "typical_days": "Day 3-5"},\n'
        '    {"name": "Discharge Readiness", "focus": "Confirm stability, arrange follow-up", "typical_days": "~Day 5"}\n'
        "  ],\n"
        '  "discharge_criteria": ["Afebrile >24h", "SpO2 >=92% on room air", "Tolerating oral intake"],\n'
        '  "rationale": "Typical CAP course, extended for age and comorbidities."\n'
        "}\n\n"
        "Now return the JSON for THIS patient's diagnosis with the same shape (3-5 phases)."
    )


def _extract_json(text: str):
    """Pull the first balanced JSON object out of an LLM response."""
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    for i in range(start, len(text)):
        c = text[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start : i + 1])
                except json.JSONDecodeError:
                    return None
    return None


def _validate(proj: dict, inputs: dict) -> dict:
    """Coerce the LLM output into a safe, renderable shape; fall back per-field."""
    if not isinstance(proj, dict):
        return _heuristic_projection(inputs)
    phases = proj.get("phases")
    if not isinstance(phases, list) or not phases:
        return _heuristic_projection(inputs)
    clean_phases = []
    for p in phases[:5]:
        if not isinstance(p, dict):
            continue
        clean_phases.append(
            {
                "name": str(p.get("name", "Phase"))[:60],
                "focus": str(p.get("focus", ""))[:240],
                "typical_days": str(p.get("typical_days", ""))[:40],
            }
        )
    if not clean_phases:
        return _heuristic_projection(inputs)
    try:
        los = int(round(float(proj.get("expected_los_days", 0))))
    except (TypeError, ValueError):
        los = 0
    if los <= 0 or los > 120:
        los = _heuristic_projection(inputs)["expected_los_days"]
    crit = proj.get("discharge_criteria")
    if not isinstance(crit, list):
        crit = []
    return {
        "expected_los_days": los,
        "phases": clean_phases,
        "discharge_criteria": [str(c)[:160] for c in crit][:6],
        "rationale": str(proj.get("rationale", ""))[:400],
    }


async def _call_llm(inputs: dict, patient_id: str | None = None) -> dict:
    from services.llm_queue_service import llm_queue

    # AMBIENT TIER, deliberately — the expected-course projection is
    # non-interactive: cached per input hash, redrawn from cache on every chart
    # open, with a deterministic heuristic fallback below.
    #
    # WHY (measured 2026-07-31): llm_queue is ONE global non-preemptive slot and
    # this job is its longest occupant — 30.1 s on medgemma:27b (325 output
    # tokens at ~12 tok/s decode) vs 4.3 s on medgemma:4b, a 7.0x cut. Any Sage
    # turn that arrives mid-generation waits that whole time; the sprint
    # benchmark caught a clinician sitting 31 s behind exactly one background
    # job. `?refresh=true` is client-controllable, so this was also the easiest
    # background job to trigger repeatedly.
    #
    # NOT the reason: KV-prefix eviction. Tested directly against this stack
    # (Ollama 0.32.5) and it did not reproduce — an intervening prompt on the
    # 27B left an exact-match prompt state restorable in ~80-230 ms, and Sage
    # gets no partial-prefix reuse to protect anyway. The win here is queue time.
    #
    # Fallback is the SAME tier on purpose: no spill lane back onto the 27B. If
    # the ambient tier is not resident, raise so the caller renders the
    # heuristic projection rather than reloading a tier the operator evicted.
    from services.model_router import ambient_tier_evicted, select_chat_model

    model_choice = await select_chat_model(preferred=AMBIENT_LLM_MODEL, fallback=AMBIENT_LLM_MODEL)
    if await ambient_tier_evicted(model_choice.model):
        raise RuntimeError("ambient tier not resident, expected-course projection skipped")

    from services import ollama_gateway

    ticket = await llm_queue.acquire(priority=3, model=model_choice.model)
    try:
        async with httpx.AsyncClient(verify=local_model_ssl_context(), 
            timeout=httpx.Timeout(connect=15.0, read=120.0, write=15.0, pool=15.0), trust_env=False
        ) as client:
            # Yields the lane to a clinician's turn (or at the P3 hold cap):
            # LaneYielded is a TimeoutError, and the caller renders the
            # heuristic projection for any failure here.
            resp = await ollama_gateway.post(
                client,
                "/api/chat",
                {
                    "model": model_choice.model,
                    "messages": [
                        {
                            "role": "system",
                            "content": "You output only valid JSON. No commentary, no markdown.",
                        },
                        {"role": "user", "content": _build_prompt(inputs)},
                    ],
                    "stream": False,
                    "keep_alive": OLLAMA_KEEP_ALIVE,
                    "format": "json",
                    # num_ctx MUST match the context the ambient runner is
                    # already loaded at (4096 — see main.py's warmup and
                    # `ollama ps`). Ollama keys a runner by model+num_ctx, so
                    # asking the 4B for 2048 would spawn a second runner and
                    # reload the tier on every projection. The prompt is ~600
                    # tokens plus 700 predicted, so 4096 is ample.
                    "options": {"temperature": 0.2, "num_predict": 700, "num_ctx": 4096},
                },
                purpose="expected_course_projection",
                patient_id=patient_id,
                yield_to_clinician=True,
            )
        resp.raise_for_status()
        content = resp.json().get("message", {}).get("content", "")
        parsed = _extract_json(content)
        projection = _validate(parsed, inputs)
        # Provenance: record the tier that ACTUALLY ran, never a hardcoded
        # model name — this path is now ambient-tier and the panel must be able
        # to say so. Cached rows written before this field exists read as None.
        projection["engine"] = model_choice.model
        return projection
    finally:
        llm_queue.release(ticket)


# Ordered acuity → which phase the patient currently occupies. Acute states sit in
# the earliest phases; a stable patient is near discharge. Derived from live risk so
# it stays current without recomputing (or re-caching) the projection.
def _current_phase_index(risk_level: str, num_phases: int) -> int:
    lvl = (risk_level or "").lower()
    if num_phases <= 1:
        return 0
    if lvl == "critical":
        return 0
    if lvl in ("elevated", "high", "warning"):
        return min(num_phases - 1, max(1, num_phases // 2))
    # stable / low → approaching discharge (second-to-last, or last if only 2 phases)
    return max(0, num_phases - 2)


# Guard the expensive LLM path: `?refresh=true` is client-controllable, and each
# refresh occupies the shared LLM queue. Coalesce concurrent refreshes per patient
# (one generation, others read the fresh cache) and impose a cooldown so the endpoint
# can't be spammed into pinning the queue away from Sage/Lens/Vigil.
_REFRESH_COOLDOWN_S = 20.0
_last_generated: dict[str, float] = {}
_gen_locks: dict[str, asyncio.Lock] = {}


# The three run_db payloads below are module-level rather than closures defined
# inside get_timeline_projection. They take every value as an explicit argument
# (they close over nothing), and keeping the `with db_session()` out of an
# `async def` body keeps the repo-wide "blocking DB call on the event loop" scan
# honest — nested inside an async def they read as on-loop hits even though
# run_db hands them to a worker thread.
def _load_inputs_and_cache(patient_id, force_refresh):
    # Table check, clinical inputs and the cache probe share ONE db_session so
    # the hash is computed from, and compared against, a single consistent read.
    with db_session() as db:
        _ensure_table(db)
        inputs = _load_clinical_inputs(db, patient_id)
        if inputs is None:
            return None  # sentinel: patient not found
        ihash = _input_hash(inputs)

        cached = None
        if not force_refresh:
            row = db.execute(
                "SELECT input_hash, projection_json, source FROM timeline_projections WHERE patient_id = ?",
                (patient_id,),
            ).fetchone()
            if row and row["input_hash"] == ihash:
                try:
                    cached = {
                        "projection": json.loads(row["projection_json"]),
                        "source": row["source"],
                    }
                except json.JSONDecodeError:
                    cached = None
    return inputs, ihash, cached


def _reread_cached_row(patient_id):
    with db_session() as db:
        row = db.execute(
            "SELECT input_hash, projection_json, source FROM timeline_projections WHERE patient_id = ?",
            (patient_id,),
        ).fetchone()
    return row


def _persist_projection(patient_id, ihash, projection, source):
    with db_session() as db:
        _ensure_table(db)
        # Upsert keyed on patient_id, so a racing writer overwrites rather than
        # duplicating. Safe to lose the race: both writers persist a projection
        # derived from the same input hash. Nothing here is read-modify-write.
        db.execute(
            """INSERT INTO timeline_projections (patient_id, input_hash, projection_json, source)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(patient_id) DO UPDATE SET
                   input_hash = excluded.input_hash,
                   projection_json = excluded.projection_json,
                   source = excluded.source,
                   created_at = CURRENT_TIMESTAMP""",
            (patient_id, ihash, json.dumps(projection), source),
        )


async def get_timeline_projection(
    patient_id: str, risk_level: str = "stable", force_refresh: bool = False
) -> dict:
    """Return the Sage-deduced clinical timeline for a patient (cached)."""
    # Downgrade a spammed refresh to a cache read once one just ran for this patient.
    if (
        force_refresh
        and (time.monotonic() - _last_generated.get(patient_id, 0.0)) < _REFRESH_COOLDOWN_S
    ):
        force_refresh = False

    loaded = await run_db(_load_inputs_and_cache, patient_id, force_refresh)
    if loaded is None:
        return {"error": "patient_not_found"}
    inputs, ihash, cached = loaded

    if cached is not None:
        projection = cached["projection"]
        source = cached["source"]
    else:
        # Per-patient lock: concurrent misses/refreshes generate once; the rest wait
        # and re-read the just-written cache instead of each hitting the LLM.
        lock = _gen_locks.setdefault(patient_id, asyncio.Lock())
        async with lock:
            fresh = None

            row = await run_db(_reread_cached_row, patient_id)
            # Only accept the in-lock re-read if another request generated it moments ago;
            # otherwise fall through and generate (covers a genuine forced refresh).
            if (
                row
                and row["input_hash"] == ihash
                and (time.monotonic() - _last_generated.get(patient_id, 0.0)) < _REFRESH_COOLDOWN_S
            ):
                try:
                    fresh = {
                        "projection": json.loads(row["projection_json"]),
                        "source": row["source"],
                    }
                except json.JSONDecodeError:
                    fresh = None

            if fresh is not None:
                projection = fresh["projection"]
                source = fresh["source"]
            else:
                from services.ollama_gateway import LaneYielded

                persist = True
                try:
                    projection = await _call_llm(inputs, patient_id)
                    source = "llm"
                except LaneYielded as e:
                    # Gave the GPU to a clinician's turn. Show the heuristic
                    # now but do not cache it: a cached row is reused for as
                    # long as the inputs hash matches, so caching would pin
                    # this patient to the heuristic for a busy minute's yield.
                    logger.info("[Timeline] projection for %s yielded: %s", patient_id, e)
                    projection = _heuristic_projection(inputs)
                    source = "heuristic"
                    persist = False
                except Exception as e:  # Ollama down, timeout, etc. — never break the panel.
                    logger.warning("[Timeline] LLM projection failed for %s: %s", patient_id, e)
                    projection = _heuristic_projection(inputs)
                    source = "heuristic"
                _last_generated[patient_id] = time.monotonic()

                if persist:
                    await run_db(_persist_projection, patient_id, ihash, projection, source)

    num_phases = len(projection.get("phases", []))
    current_index = _current_phase_index(risk_level, num_phases)

    return {
        "patient_name": inputs["name"],
        "diagnosis": inputs["diagnosis"],
        "age": inputs["age"],
        "sex": inputs["sex"],
        "comorbidities": inputs["comorbidities"],
        "expected_los_days": projection.get("expected_los_days"),
        "phases": projection.get("phases", []),
        "discharge_criteria": projection.get("discharge_criteria", []),
        "rationale": projection.get("rationale", ""),
        "current_phase_index": current_index,
        "source": source,
        # None for heuristic projections and for rows cached before provenance
        # was recorded; otherwise the exact Ollama model that generated it.
        "engine": projection.get("engine"),
    }
