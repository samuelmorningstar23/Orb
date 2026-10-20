"""Orb — Model Governance Service.

Turns the project's best narrative (transparent, locally-validated AI vs the
Epic Sepsis Model that failed external validation) into a product surface: a
registry that records, for every AI/ML model Orb ships, its version, training-data
lineage, whether a trained checkpoint is actually present on disk, and its
validation status. No competitor exposes this as a first-class feature.

Honesty is enforced structurally: `checkpoint_present` is computed from the
filesystem, and `validated` defaults to 0 — a model cannot claim to be validated
just because a checkpoint exists.
"""

from __future__ import annotations

import functools
import json
import logging

from config import BASE_DIR
from database import db_session

logger = logging.getLogger("orb.model_governance")


def _checkpoint_exists(*relative_paths: str) -> bool:
    for rel in relative_paths:
        if (BASE_DIR / rel).exists():
            return True
    return False


def _cfc_checkpoint_present() -> bool:
    """The same files vigil_service loads the overlay from. This used to look for
    models/vigil_cfc.pt, a name nothing loads, so the registry and the live
    overlay could disagree about whether a model was running."""
    try:
        from services.vigil_service import _CFC_CHECKPOINTS

        names = [n for files in _CFC_CHECKPOINTS.values() for n in files]
    except Exception:  # pragma: no cover - defensive
        names = ["vigil_cfc_deterioration.pt", "vigil_cfc_checkpoint.pt"]
    return _checkpoint_exists(
        *(f"models/{n}" for n in names), *(f"models/checkpoints/{n}" for n in names)
    )


def _embedding_cached(model_name: str) -> bool:
    """Whether the embedding weights are on this machine (a local lookup only:
    Sage falls back to keyword search without them)."""
    try:
        from huggingface_hub import snapshot_download

        snapshot_download(model_name, local_files_only=True)
        return True
    except Exception:
        return False


def _asr_engine_installed() -> bool:
    import importlib.util

    try:
        return importlib.util.find_spec("lightning_whisper_mlx") is not None
    except Exception:  # pragma: no cover - defensive
        return False


# What each Sage lane serves, in words. The same features /api/health/models
# lists when an operator unloads a tier, so the two screens describe one thing.
_CAREFUL_IMPACTS = [
    "Sage answers with thinking on",
    "Every patient-specific or dosing question (the safety floor keeps them here)",
    "Lens image descriptions, Scribe note structuring, discharge summaries",
    "The ward brief narrative",
]
_FAST_IMPACTS = ["Sage answers with thinking off, for general questions only"]
_AMBIENT_IMPACTS = [
    "Vigil alert recommendations in Relay",
    "Relay drafted actions and room memory",
]


# The model surfaces Orb ships, DERIVED FROM THE RESOLVED CONFIG. This used to
# be a hand-written list of four that named the 27B as "the only inference lane"
# while the fast 4B answered every thinking-off turn, and left out the embedding
# and speech models entirely. Checkpoint presence is resolved live so the
# registry never over-claims; `validated` stays 0 for everything but NEWS2.
def _default_models() -> list[dict]:
    import config

    forecast_ckpt = _checkpoint_exists(
        "models/forecast_checkpoint.pt",
        "models/checkpoints/forecast_lstm_synth_v1.pt",
    )
    cfc_ckpt = _cfc_checkpoint_present()
    careful = getattr(config, "LLM_MODEL", "medgemma:27b")
    fast = getattr(config, "THINKING_FAST_MODEL", careful)
    fast_on = bool(getattr(config, "THINKING_FAST_MODE_ENABLED", True))
    ambient = getattr(config, "AMBIENT_LLM_MODEL", careful)
    floor_on = bool(getattr(config, "FAST_MODE_SAFETY_FLOOR", True))
    embedding = getattr(config, "EMBEDDING_MODEL", "BAAI/bge-m3")
    asr = (getattr(config, "SCRIBE", {}) or {}).get("asr", {}) or {}
    medgemma = "Google MedGemma (pretrained); not fine-tuned on Orb or hospital data"

    models: list[dict] = [
        {
            "model_key": "vigil_news2",
            "display_name": "NEWS2 score",
            "version": "rcp-news2-2017",
            "task": "NEWS2 score (including Scale 2 for COPD)",
            "training_data": "Rule-based (Royal College of Physicians NEWS2), no training",
            "validated": 1,  # NEWS2 is a published, validated clinical standard
            "validation_metrics": json.dumps({"source": "Royal College of Physicians NEWS2"}),
            "is_active": 1,
            "checkpoint_present": 1,
            "role": "The deterioration score on every board",
            "impacts": ["Vigil scores, bands and alerts", "Escalation and the Relay alert line"],
            "notes": "A fixed published rule: the live clinical signal Orb ships on today.",
        },
        {
            "model_key": "vigil_cfc",
            "display_name": "Deterioration overlay (CfC network)",
            "version": "cfc-lnn-v0",
            "task": "Deterioration, sepsis and mortality risk over the next 1 to 24 hours",
            "training_data": (
                "A trained checkpoint is present, but it has not been validated on "
                "this hospital's patients"
                if cfc_ckpt
                else "Not trained: no checkpoint on this appliance, so the overlay is off"
            ),
            "validated": 0,
            "validation_metrics": json.dumps({}),
            "is_active": 1 if cfc_ckpt else 0,
            "checkpoint_present": 1 if cfc_ckpt else 0,
            "role": "May raise, never lower, the NEWS2 risk",
            "impacts": ["The learned risk figure beside NEWS2 on Vigil"],
            "notes": "Fails closed: with no checkpoint the risk shown is NEWS2 alone.",
        },
        {
            "model_key": "forecast_lstm",
            "display_name": "Length-of-stay forecast",
            "version": "synthetic-v1" if forecast_ckpt else "untrained",
            "task": "Length of stay, discharge and acuity forecasting",
            "training_data": (
                "Trained on synthetic data only: not trained or validated on real patients"
                if forecast_ckpt
                else "Not trained: no checkpoint on this appliance, so Forecast shows a "
                "labelled rule of thumb"
            ),
            "validated": 0,
            "validation_metrics": json.dumps({}),
            "is_active": 1 if forecast_ckpt else 0,
            "checkpoint_present": 1 if forecast_ckpt else 0,
            "role": "Bed and discharge planning",
            "impacts": ["Forecast screen predictions"],
            "notes": "Must be retrained on real data and externally validated before "
            "any clinical use.",
        },
        {
            "model_key": "sage_llm",
            "display_name": "Sage, careful model",
            "version": careful,
            "task": "Clinical assistant: answers, summaries and drafts (local, via Ollama)",
            "training_data": medgemma,
            "validated": 0,
            "validation_metrics": json.dumps({}),
            "is_active": 1,
            "checkpoint_present": 1,
            "role": "Thinking on" + (", and the safety floor" if floor_on else ""),
            "impacts": _CAREFUL_IMPACTS + (_AMBIENT_IMPACTS if ambient == careful else []),
            "notes": "Decision support only: every output is a draft for a clinician "
            "to review, never an order. Runs on this appliance; nothing "
            "leaves the building.",
        },
    ]
    if fast_on and fast and fast != careful:
        models.append(
            {
                "model_key": "sage_llm_fast",
                "display_name": "Sage, fast model",
                "version": fast,
                "task": "Quick answers when a clinician turns thinking off (local, via Ollama)",
                "training_data": medgemma,
                "validated": 0,
                "validation_metrics": json.dumps({}),
                "is_active": 1,
                "checkpoint_present": 1,
                "role": "Thinking off",
                "impacts": _FAST_IMPACTS + (_AMBIENT_IMPACTS if ambient == fast else []),
                "notes": (
                    "The safety floor sends patient and dosing questions to the careful "
                    "model even with thinking off."
                    if floor_on
                    else "The safety floor is switched off: this model answers every "
                    "thinking-off question, including patient and dosing questions."
                ),
            }
        )
    if ambient and ambient not in (careful, fast if fast_on else careful):
        models.append(
            {
                "model_key": "sage_llm_ambient",
                "display_name": "Sage, background model",
                "version": ambient,
                "task": "Background work: alert recommendations and Relay drafting",
                "training_data": medgemma,
                "validated": 0,
                "validation_metrics": json.dumps({}),
                "is_active": 1,
                "checkpoint_present": 1,
                "role": "Background tasks",
                "impacts": _AMBIENT_IMPACTS,
                "notes": "Runs without a clinician asking; its drafts are shown, never acted on.",
            }
        )
    models.append(
        {
            "model_key": "sage_embedding",
            "display_name": "Guideline search",
            "version": embedding,
            "task": "Finds the guideline passages Sage cites",
            "training_data": "Pretrained open embedding model; not trained on Orb data",
            "validated": 0,
            "validation_metrics": json.dumps({}),
            "is_active": 1,
            "checkpoint_present": 1 if _embedding_cached(embedding) else 0,
            "role": "Retrieval for Sage",
            "impacts": ["Which guideline passages Sage reads before answering"],
            "notes": "Without these weights on the machine, search falls back to keyword matching.",
        }
    )
    asr_model = asr.get("model") or getattr(config, "WHISPER_MODEL", "large-v3")
    models.append(
        {
            "model_key": "scribe_asr",
            "display_name": "Scribe speech recognition",
            "version": f"whisper-{asr_model}",
            "task": "Turns a dictated consultation into a transcript",
            "training_data": "OpenAI Whisper (pretrained); not trained on Orb data",
            "validated": 0,
            "validation_metrics": json.dumps({}),
            "is_active": 1 if _asr_engine_installed() else 0,
            "checkpoint_present": 1 if _asr_engine_installed() else 0,
            "role": "Dictation",
            "impacts": ["Scribe transcripts, before the note is structured"],
            "notes": "Present means the speech engine is installed on this appliance. "
            "Every transcript is shown to the clinician before it is used.",
        }
    )
    return models


def _descriptions() -> dict[str, dict]:
    """Display fields that are derived, not stored (the registry table has no
    column for them), keyed by model_key.

    Cached on the config values they depend on: list_models backs the provenance
    chip every clinician loads, and the checkpoint probes have no place there.
    """
    import config

    key = tuple(
        getattr(config, name, None)
        for name in (
            "LLM_MODEL",
            "THINKING_FAST_MODEL",
            "THINKING_FAST_MODE_ENABLED",
            "AMBIENT_LLM_MODEL",
            "FAST_MODE_SAFETY_FLOOR",
            "EMBEDDING_MODEL",
        )
    )
    return _descriptions_for(key)


@functools.lru_cache(maxsize=4)
def _descriptions_for(_config_key: tuple) -> dict[str, dict]:
    return {
        m["model_key"]: {k: m[k] for k in ("display_name", "role", "impacts")}
        for m in _default_models()
    }


def seed_default_models() -> int:
    """Idempotently upsert the known model surfaces. Returns count."""
    models = _default_models()
    with db_session() as db:
        for m in models:
            db.execute(
                """INSERT INTO model_registry
                       (model_key, version, task, training_data, validated,
                        validation_metrics, calibration_stats, is_active, checkpoint_present, notes)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(model_key, version) DO UPDATE SET
                       task=excluded.task,
                       training_data=excluded.training_data,
                       validated=excluded.validated,
                       is_active=excluded.is_active,
                       checkpoint_present=excluded.checkpoint_present,
                       notes=excluded.notes""",
                (
                    m["model_key"],
                    m["version"],
                    m["task"],
                    m["training_data"],
                    m["validated"],
                    m["validation_metrics"],
                    m.get("calibration_stats"),
                    m["is_active"],
                    m["checkpoint_present"],
                    m["notes"],
                ),
            )
        # This registry is written from _default_models() and nowhere else (the
        # only INSERT and the only DELETE are both in this function), so a row
        # this pass did not just write describes a surface Orb does not have.
        # Drop it: a lingering row over-claims in the governance UI and in the
        # compliance export, which is the exact failure this registry exists to
        # prevent.
        #
        # KEYED ON (model_key, version), NOT model_key ALONE (2026-09-07). The
        # upsert conflicts on (model_key, version), so a version change INSERTS a
        # second row rather than replacing the first, and a key-only DELETE keeps
        # both because the key is still shipped. That is not a hypothetical
        # retirement: `_default_models()` derives two versions from the
        # FILESYSTEM — forecast_lstm is "untrained" or "synthetic-v1" depending
        # on `_checkpoint_exists`, and vigil_cfc's row moves with its checkpoint
        # the same way. So the day anyone trains the forecast model, the registry
        # gains a permanent phantom `forecast_lstm/untrained` row beside the real
        # one and the compliance posture export reads 5 models / 4 unvalidated
        # where the truth is 4 / 3. Reproduced exactly that way before this
        # change; pinned in tests/test_model_governance.py.
        #
        # Retiring a KEY still works, because a retired key cannot appear in any
        # pair. This only additionally catches the retired VERSION of a key Orb
        # still ships, which migration 38 (a one-time key-only repair) does not.
        db.execute(
            "DELETE FROM model_registry WHERE (model_key, version) NOT IN "
            f"(VALUES {','.join(['(?,?)'] * len(models))})",
            [value for m in models for value in (m["model_key"], m["version"])],
        )
    return len(models)


def list_models() -> list[dict]:
    with db_session() as db:
        n = db.execute("SELECT COUNT(*) FROM model_registry").fetchone()[0]
    if n == 0:
        seed_default_models()
    with db_session() as db:
        rows = db.execute("SELECT * FROM model_registry ORDER BY model_key, version").fetchall()
    described = _descriptions()
    out = []
    for r in rows:
        d = dict(r)
        d.update(
            described.get(
                d["model_key"],
                {
                    "display_name": d["model_key"],
                    "role": None,
                    "impacts": [],
                },
            )
        )
        for jf in ("validation_metrics", "calibration_stats"):
            if d.get(jf):
                try:
                    d[jf] = json.loads(d[jf])
                except (json.JSONDecodeError, TypeError):
                    pass
        out.append(d)
    return out
