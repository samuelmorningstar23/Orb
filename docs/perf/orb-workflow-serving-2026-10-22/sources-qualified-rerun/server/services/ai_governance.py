"""Orb — AI Governance: PHI classification and per-inference audit.

Orb's thesis is "PHI never leaves the building": every inference runs on the
local Ollama/MedGemma engine, so there is no lane a request can take off the
box. This module supplies the two controls that keep that claim honest:

  1. classify_query_sensitivity() — a LOCAL-ONLY classifier that decides whether
     a request carries (or can resolve to) PHI. It runs regexes and a local DB
     name/ward/bed match; the query text itself NEVER leaves the process to make
     this decision, because patient names inside the text ARE the PHI. Callers
     use the verdict to label the audit record and to decide how much chart
     context a turn is allowed to pull.
  2. record_inference() — a per-inference record of which provider/model served
     each request, written to the tamper-evident audit hash chain
     (services/audit_service.py), so "which model saw this patient" is
     answerable after the fact. Each row carries a SHA-256 of the prompt
     (hash_prompt), never the prompt. Direct Ollama calls get their row from
     services/ollama_gateway.py, which every module must use; calls through
     the provider seams (llm_providers, scribe_providers) get theirs from the
     caller (sage_service, sage_assessment_service, scribe_service).
     tests/test_ollama_gateway_guard.py holds both rules.

PHI posture (fail-closed by design):
  * Any patient-scoped query (explicit name/ward/bed match, an attached
    patient_id, or a pronoun that may resolve to a patient) is PHI.
  * The hospital census / patient list / discharge board / per-patient forecast
    is PHI — a bed+ward+diagnosis+status row per patient identifies people even
    without names. Orb's broad-scope context builder returns exactly such rows,
    so EVERY broad-scope query is treated as PHI.
  * Only general medical-knowledge / guideline questions with no patient
    reference of any kind are PHI-free.
  * De-identification of free-text clinical narrative is a compliance
    determination (Safe-Harbor / expert determination), not a string strip —
    residual detail re-identifies. The classifier therefore never claims to
    sanitise text, only to label it.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from functools import lru_cache

from services.ward_reference import ward_named_in

logger = logging.getLogger("orb.ai_governance")


# ── Local PHI / sensitivity classifier ──

# Word-boundary pronouns + phrases that make a chat turn resolvable to a patient
# (mirrors the referential detection in sage_service.get_relevant_context — if
# retrieval can resolve it to a chart, the router must assume it will).
_PRONOUN_PATTERNS = [
    r"\bhe\b",
    r"\bshe\b",
    r"\bhim\b",
    r"\bhis\b",
    r"\bher\b",
    r"\bthey\b",
    r"\btheir\b",
    r"\bthem\b",
]
_PATIENT_PHRASES = [
    "this patient",
    "this guy",
    "this lady",
    "this one",
    "the patient",
    "my patient",
    "current patient",
    "their vitals",
    "their labs",
    "their medications",
    "their meds",
    "their diagnosis",
]

# Census / roster / board asks. Broad-scope context is one row PER PATIENT
# (name | bed | age | dx | vitals | risk), so these are PHI even when the
# question sounds statistical. Kept in sync with sage_service broad detection
# via classify-time import (see _broad_keywords()).
_CENSUS_EXTRA_KEYWORDS = [
    "discharge board",
    "discharge list",
    "patient list",
    "bed board",
    "forecast for",
    "length of stay",
]

_BED_REF = re.compile(
    r"\bbed\s*[a-z]?-?\d+\b"
    r"|\bbed\s+(?:one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)\b",
    re.IGNORECASE,
)

# Person/locative DEIXIS that scopes to a specific patient without a pronoun in
# the base list ("that one's creatinine", "the fellow in the corner bed").
_DEIXIS = re.compile(
    r"\b(that one|that patient|the fellow|the gentleman|the lady|the guy|the chap|"
    r"corner bed|in the corner|over there|in here)\b",
    re.IGNORECASE,
)

# Confusable homoglyphs (Cyrillic/Greek that render like Latin letters) used to
# smuggle a census surname past name matching. Normalised to Latin before match.
_HOMOGLYPHS = str.maketrans(
    {
        "А": "A",
        "а": "a",
        "Е": "E",
        "е": "e",
        "О": "O",
        "о": "o",
        "Р": "P",
        "р": "p",
        "С": "C",
        "с": "c",
        "Х": "X",
        "х": "x",
        "І": "I",
        "і": "i",
        "К": "K",
        "к": "k",
        "М": "M",
        "Т": "T",
        "Β": "B",
        "Η": "H",
        "Ο": "O",
        "Ρ": "P",
        "Ι": "I",
        "ο": "o",
    }
)


def _fold_homoglyphs(text: str) -> str:
    import unicodedata

    return unicodedata.normalize("NFKC", text).translate(_HOMOGLYPHS)


# Patient-ENUMERATION intent: asking to list/name specific patients is a roster
# request (PHI) even when phrased as a count follow-on ("...also show me the ICU
# patients by name"). These are the same signals that EXCLUDE a query from the
# aggregate lane, promoted to a positive PHI signal so an excluded query can't
# fall through to 'general knowledge'.
_PATIENT_ENUMERATION = re.compile(
    r"\bpatients?\s+by\s+name\b"
    r"|\b(?:list|show\s+me|show\s+all|name|enumerate|give\s+me)\b[^.?!]{0,30}\bpatients?\b"
    r"|\bwhich\s+patients?\b"
    r"|\bwho\s+(?:is|are)\s+(?:the\s+|my\s+)?(?:patients?|admitted|critical|elevated)\b",
    re.IGNORECASE,
)


def _broad_keywords() -> list[str]:
    """The census/broad keyword list, sourced from sage_service so the router
    and the retrieval layer can never disagree about what counts as broad."""
    try:
        from services.sage_service import BROAD_QUERY_KEYWORDS

        return list(BROAD_QUERY_KEYWORDS) + _CENSUS_EXTRA_KEYWORDS
    except ImportError:  # circular-import fallback during startup
        return _CENSUS_EXTRA_KEYWORDS


@dataclass
class QueryClassification:
    contains_phi: bool
    query_class: str  # patient_scoped | census_broad | contextual | general_knowledge
    reasons: list[str] = field(default_factory=list)


def _identifier_name_parts_uncached(name: str) -> tuple[str, ...]:
    return tuple(
        part for part in re.split(r"[^a-zA-Z]", name)
        if part and part not in ("mr", "mrs", "ms", "dr", "prof")
    )


@lru_cache(maxsize=2048)
def _identifier_name_parts_cached(name: str) -> tuple[str, ...]:
    return _identifier_name_parts_uncached(name)


def _identifier_name_parts(name: str) -> tuple[str, ...]:
    # Reuse only pure parsing of the exact observed string. Patient rows and
    # matching verdicts remain fresh; oversized labels never enter the cache.
    if len(name) > 256:
        return _identifier_name_parts_uncached(name)
    return _identifier_name_parts_cached(name)


@lru_cache(maxsize=2048)
def _identifier_bed_cached(bed: str) -> str:
    return re.sub(r"[^a-z0-9]", "", bed)


def _identifier_bed(bed: str) -> str:
    if len(bed) > 256:
        return re.sub(r"[^a-z0-9]", "", bed)
    return _identifier_bed_cached(bed)


def _match_patient_identifiers(
    query_lower: str, include_ward: bool = True, query: str | None = None
) -> bool:
    """LOCAL DB check: does the query mention an admitted/discharged patient's
    name or bed (and optionally ward)? Best-effort — any failure classifies as
    no-match, but the other signals still apply and routing defaults local.

    A patient NAME or BED identifies a person → always PHI. A WARD is a shared
    location, not an identifier: `include_ward=False` excludes it so a
    ward-scoped AGGREGATE question ("ICU bed occupancy") isn't misclassified as
    per-patient PHI. The ward is still a PHI signal for non-aggregate turns
    (checked separately with include_ward=True).

    The question is read ONCE (2026-10-04). This used to run one `\\bpart\\b`
    regex over the whole question per name part per patient row (every
    status), and a 512 KB question that named nobody took 13 s per call with
    2,000 rows; classify_query_sensitivity can make two calls. A name part is
    a-z only, so `\\bpart\\b` finds it exactly when it is one of the question's
    whole a-z words, and each distinct bed label needs testing only once.
    Pinned against the old loop in tests/test_ai_governance_identifier_scan.py.

    WARDS read through services.ward_reference (2026-10-04), the SAME rule Sage
    retrieval's ward match reads: the squashed-substring test found ward "OR"
    inside "for" and CCU inside "occur", and labelled general questions PHI.
    That rule needs the question AS TYPED for its capitals half ("back from
    OR"); pass it as `query`. Without it the lower-cased text is used, which
    still finds every other ward and "ward or". Beds keep the substring test:
    bed references are ruling R026's work, in retrieval and here alike."""
    try:
        from database import get_db

        db = get_db()
        try:
            rows = db.execute("SELECT name, ward, bed FROM patients").fetchall()
        finally:
            db.close()
    except Exception as e:
        logger.debug("PHI classifier: patient lookup unavailable (%s)", e)
        return False

    # Fold confusable homoglyphs so a Cyrillic/Greek look-alike surname can't
    # slip past the match.
    query_lower = _fold_homoglyphs(query_lower)
    typed = _fold_homoglyphs(query) if query is not None else query_lower
    norm_query = re.sub(r"[^a-z0-9]", "", query_lower)
    words = frozenset(re.findall(r"\b[a-z]+\b", query_lower))
    tested_beds = set()
    tested_wards: set[str] = set()
    for r in rows:
        name = str(r["name"] or "").lower()
        name_parts = _identifier_name_parts(name)
        for part in name_parts:
            if len(part) > 2 and part in words:
                return True
        norm_bed = _identifier_bed(str(r["bed"] or "").lower())
        if norm_bed and norm_bed not in tested_beds:
            tested_beds.add(norm_bed)
            if norm_bed in norm_query:
                return True
        ward = r["ward"]
        if include_ward and ward and (not isinstance(ward, str) or ward not in tested_wards):
            if isinstance(ward, str):
                tested_wards.add(ward)
            if ward_named_in(ward, typed):
                return True
    return False


def classify_query_sensitivity(
    query: str,
    patient_id: str | None = None,
    view_context: dict | None = None,
) -> QueryClassification:
    """Classify a chat turn's PHI sensitivity using LOCAL signals only.

    The classifier itself is an egress boundary: it must never hand the query to
    anything outside this process, because the raw text carries the patient
    names/beds/wards it is being asked to detect.
    """
    reasons: list[str] = []
    q = _fold_homoglyphs((query or "").lower())

    # ── HARD per-patient signals (definitive PHI, cannot be overridden) ──
    if patient_id:
        reasons.append("patient_id attached to request")
    if view_context:
        if view_context.get("patient_id"):
            reasons.append("active view is patient-scoped")
        sel = view_context.get("selection") or {}
        if isinstance(sel, dict) and sel.get("patient_id"):
            reasons.append("panel selection is patient-scoped")
    # Referential pronouns + person/locative deixis — resolvable to a patient.
    if (
        any(re.search(p, q) for p in _PRONOUN_PATTERNS)
        or any(kw in q for kw in _PATIENT_PHRASES)
        or _DEIXIS.search(q)
    ):
        reasons.append("referential pronoun/deixis (may resolve to a patient)")
    # Bed references and patient NAME/BED matches against the local DB are HARD
    # signals (a name/bed identifies a person). A ward match alone is soft (a
    # ward is a shared location) and is applied below so it can't block an
    # aggregate ward-occupancy question.
    if _BED_REF.search(q):
        reasons.append("bed reference in query")
    elif not reasons and _match_patient_identifiers(q, include_ward=False):
        reasons.append("query matches a patient name/bed on record")

    hard_patient_signal = bool(reasons)

    # ── PHI-FREE AGGREGATE (takes precedence over census keywords) ──
    # A pure count/distribution ask with NONE of the hard per-patient signals
    # above is PHI-free BY CONSTRUCTION: the aggregate context builder emits zero
    # patient rows, so the turn can be served (and audited) without patient
    # context. This is checked BEFORE census keywords because count phrasings
    # like "how many patients are admitted" also match the (asserted-PHI) census
    # vocabulary — but a bare count with no enumeration/per-patient signal is
    # genuinely aggregate.
    if not hard_patient_signal:
        try:
            from services.sage_service import _is_aggregate_only

            if _is_aggregate_only(q):
                return QueryClassification(
                    contains_phi=False,
                    query_class="aggregate_stats",
                    reasons=["aggregate count/distribution query with no per-patient signal"],
                )
        except Exception:
            pass

    # ── Patient enumeration (roster) request → PHI ──
    # Reached only when NOT aggregate-only (aggregate returned above). Catches
    # "show me the ICU patients by name" and friends that ask to enumerate
    # specific patients — a roster is per-patient PHI.
    if _PATIENT_ENUMERATION.search(q):
        reasons.append("patient enumeration/roster request")

    # ── Narrow demographic/clinical COHORT filter → PHI (small-cell) ──
    # A count restricted to a narrow cohort ("47yo males with pancreatic cancer",
    # "post-op day one Whipple aged 71") can isolate an individual, so it is
    # treated as PHI even though it names nobody. Reached only when NOT
    # aggregate-only (the same filter excludes it from the aggregate lane).
    try:
        from services.sage_service import _COHORT_FILTER

        if _COHORT_FILTER.search(q):
            reasons.append("narrow demographic/clinical cohort (small-cell re-identification risk)")
    except Exception:
        pass

    # ── Census / roster / board / forecast asks → per-patient rows → PHI ──
    census_hit = next((kw for kw in _broad_keywords() if kw in q), None)
    if census_hit:
        reasons.append(f"census/broad keyword: {census_hit!r}")

    # Soft ward match: a ward reference in a NON-aggregate turn (we already
    # returned above for genuine aggregate ward questions) may be scoping to that
    # ward's patients → treat as PHI.
    if (
        not hard_patient_signal
        and not census_hit
        and _match_patient_identifiers(q, include_ward=True, query=query or "")
    ):
        reasons.append("query references a ward on record")

    if reasons:
        if census_hit and not hard_patient_signal:
            qclass = "census_broad"
        elif (
            patient_id
            or (view_context or {}).get("patient_id")
            or "name/ward/bed" in " ".join(reasons)
        ):
            qclass = "patient_scoped"
        elif census_hit:
            qclass = "census_broad"
        else:
            qclass = "contextual"
        return QueryClassification(contains_phi=True, query_class=qclass, reasons=reasons)

    return QueryClassification(
        contains_phi=False,
        query_class="general_knowledge",
        reasons=["no patient reference detected (general medical knowledge)"],
    )


# ── Per-inference audit records (tamper-evident hash chain) ──


def _prompt_messages(payload: dict) -> list[dict]:
    """The prompt material of one Ollama request as a message list.

    /api/generate's system + prompt is folded into the same shape /api/chat
    sends, so one prompt hashes the same whichever endpoint carried it, and a
    caller that holds (system, prompt) rather than the wire body (the Scribe
    providers) can hash it without rebuilding the request.
    """
    messages = payload.get("messages")
    if messages is None:
        messages = []
        if payload.get("system"):
            messages.append({"role": "system", "content": payload["system"]})
        user = {"role": "user", "content": payload.get("prompt") or ""}
        if payload.get("images"):
            user["images"] = payload["images"]
        messages.append(user)
    return [
        {k: m.get(k) for k in ("role", "content", "images") if m.get(k) is not None}
        for m in messages
        if isinstance(m, dict)
    ]


def hash_prompt(payload: dict) -> str:
    """SHA-256 of the exact prompt material (messages, or system + prompt, and
    any images) in canonical JSON.

    The audit chain stores this digest, never the prompt: the prompt carries
    the chart, and the chain is read by people who may not see the chart. The
    digest still ties a row to a prompt held elsewhere (a captured request, a
    replayed draft) byte for byte. Model and sampling options are not part of
    it; the row records the model separately.
    """
    canonical = json.dumps(
        _prompt_messages(payload), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def record_inference(
    *,
    provider: str,
    model: str,
    purpose: str,
    outcome: str,  # "served" | "denied" | "error" | "attempt"
    classification: QueryClassification | None = None,
    egress_reason: str | None = None,
    user_id: str | None = None,
    patient_id: str | None = None,
    correlation_id: str | None = None,
    timing_ms: dict | None = None,
    detail: str | None = None,
    prompt_sha256: str | None = None,
) -> str:
    """Append one inference record to the tamper-evident audit hash chain.

    Written under event_category="ai_inference", which audit_service treats as
    FAIL-CLOSED (services/audit_service._FAIL_CLOSED_CATEGORIES, since
    2026-09-04): if the row cannot be persisted, log_audit_event raises
    AuditWriteError instead of logging and continuing. Every caller
    (services/sage_service.py, routes/sage_routes.py, scribe_service's
    correction stage, and through record_inference_async / _soon the gateway,
    Sage Tier 2 and the rest of Scribe) still catches that error and serves
    the turn, so whether an unaudited inference is refused is decided at the
    call site, not here (the open ruling of 2026-09-04).

    The record carries lane="local" because local IS the only engine — keeping
    the field explicit means an auditor reading the chain sees where each
    inference ran without inferring it from the absence of a field.

    `prompt_sha256` is hash_prompt() of what the model was sent; the prompt
    itself never enters the chain.

    Returns the correlation_id used (generated when absent) so callers can tie
    the attempt/served pair together.
    """
    from services.audit_service import log_audit_event

    corr = correlation_id or f"ai-{uuid.uuid4().hex[:12]}"
    metadata = {
        "provider": provider,
        "model": model,
        "lane": "local",
        "purpose": purpose,
        "phase": outcome,
        "phi": classification.contains_phi if classification else None,
        "query_class": classification.query_class if classification else None,
        "phi_reasons": classification.reasons if classification else None,
        "egress_reason": egress_reason,
        "timing_ms": timing_ms,
        "detail": detail,
        "prompt_sha256": prompt_sha256,
    }
    # audit_logs.outcome is CHECK-constrained to a fixed enum; the precise
    # inference phase lives in action_type + metadata.phase.
    db_outcome = {
        "served": "success",
        "attempt": "success",
        "denied": "denied",
        "error": "failure",
    }.get(outcome, "failure")
    action_type = "model_inference_attempt" if outcome == "attempt" else "model_inference"
    log_audit_event(
        correlation_id=corr,
        event_category="ai_inference",
        action_type=action_type,
        outcome=db_outcome,
        summary_message=f"local inference via {provider}/{model} for {purpose}: {outcome}",
        actor_user_id=user_id,
        patient_id=patient_id,
        target_type="ai_provider",
        target_id=provider,
        source_type="ai_router",
        metadata=metadata,
    )
    return corr


# Strong references to in-flight audit writes. A write whose caller was
# cancelled is no longer awaited by anyone, and the loop holds tasks weakly.
_PENDING_AUDITS: set = set()


def _audit_write_done(task: asyncio.Task) -> None:
    _PENDING_AUDITS.discard(task)
    if task.cancelled():
        return
    err = task.exception()
    if err is not None:
        # Logged here, once, whether or not the caller was still waiting.
        logger.error("inference audit write failed: %r", err)


def record_inference_soon(**kwargs) -> asyncio.Task:
    """Start record_inference() on the DB lane and return without waiting.

    For call sites that must not wait on a DB write: a Tier 2 pass that is
    being cancelled, or a streaming generator's teardown. The task is held
    strongly here and a failure is logged by _audit_write_done.
    """
    from database import run_db

    task = asyncio.ensure_future(run_db(record_inference, **kwargs))
    _PENDING_AUDITS.add(task)
    task.add_done_callback(_audit_write_done)
    return task


async def record_inference_async(**kwargs) -> str | None:
    """record_inference() on the DB lane (database.run_db), never on the loop.

    The audit append is a SQLite write on the hash chain; on the event loop it
    is the 2026-07-30 wedge shape. Returns the correlation id, or None when the
    write failed: failures are logged and swallowed like every other caller's,
    pending the 2026-09-04 ruling on refusing unaudited inferences.

    Shielded: a caller cancelled mid-write (a reader hanging up on a stream, a
    lane yield) still gets its row, because the write finishes on its own.
    """
    task = record_inference_soon(**kwargs)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        raise
    except Exception:
        return None


def ai_posture() -> dict:
    """The client-visible governance posture (no secrets).

    local_model is the provenance the UI shows: there is exactly one engine, and
    naming it is how a clinician can tell what answered them. There is no
    browser or cloud speech to report: Orb is strictly offline, so the only
    speech recognition is the local Whisper lane below.
    """
    from config import LLM_MODEL

    return {
        "local_model": LLM_MODEL,
        # Whether ON-APPLIANCE transcription can actually run, a CAPABILITY
        # fact. Without it every dictation control in the product looked live,
        # and a clinician learned otherwise only after speaking for thirty
        # seconds into a recorder whose transcription was always going to fail.
        # Reported honestly rather than used to hide the feature: the control
        # stays, it just says what it can and cannot do right now.
        "local_asr_available": local_asr_available(),
    }


def local_asr_available() -> bool:
    """True when the local Whisper lane could plausibly serve a transcription.

    Deliberately cheap and non-invasive: it reports CONFIGURATION, not a live
    probe, because this is called on every client boot. The daemon flag is the
    operator's own switch, and the model weights are the thing that is actually
    missing on an appliance where ASR was never installed.
    """
    try:
        from config import SCRIBE, VOICE
    except Exception:  # pragma: no cover: a config shape change must not 500
        return False

    try:
        if not VOICE.get("daemon", {}).get("enabled", False):
            return False
    except Exception:  # pragma: no cover
        return False

    # The weights have to be where the daemon and the subprocess worker load
    # them from: server/mlx_models/<model>/. Nothing downloads them (Orb is
    # offline), so a missing folder means no transcription. This used to look
    # for a "whisper" folder under server/models, where nothing loads from, so
    # an installed model read as missing.
    try:
        from services.asr_model_files import is_installed

        return is_installed(SCRIBE["asr"]["model"], SCRIBE["asr"].get("quant")) or is_installed(
            VOICE["daemon"]["model"]
        )
    except Exception:  # pragma: no cover
        return False
