"""Orb Vigil — Risk Scoring Service (MEWS + CfC Liquid Neural Network)

Modified Early Warning Score + continuous-time neural network for hybrid
patient deterioration risk scoring. Outputs a normalized 0.0-1.0 risk
score with driver explanations and LNN trajectory overlay.
"""

from typing import TYPE_CHECKING

from services.local_model_http import local_model_ssl_context

if TYPE_CHECKING:
    from models.vigil_cfc import OrbVigilCfC
import asyncio
import logging
import re
import time
from datetime import UTC
from pathlib import Path
from typing import Any

import httpx
from database import db_session

from services.inference_cache import InferenceCache
from services.vital_charting import as_charted

logger = logging.getLogger("orb.vigil")

# The chart-open analysis cache lived here. It went with
# `generate_patient_analysis` (see the removal note further down) — and it is
# worth recording that the cache existed and was DISABLED in the code that
# shipped ("Disable cache temporarily for debugging"), which is what turned one
# 27B stream per patient into one per chart open.

# ── CfC Liquid Neural Networks — per-outcome, multi-horizon risk overlay ──
# One independent CfC model per outcome; each predicts risk at 1/3/6/12/24h.
# Phase 1: NEWS2 is the displayed risk; these are a learned overlay that may
# escalate (never de-escalate) the baseline. See models/vigil_cfc.py.
_MODELS_DIR = Path(__file__).resolve().parent.parent / "models"
_cfc_models: dict = {}  # outcome -> loaded OrbVigilCfC
_cfc_meta: dict = {}  # outcome -> {input_dim, feature_order, mean, std, horizons}
_cfc_device = None
_cfc_loaded = False
_cfc_prediction_cache = InferenceCache[dict](max_entries=128)

# Per-outcome checkpoint filenames (legacy single checkpoint maps to 'deterioration').
_CFC_CHECKPOINTS = {
    "deterioration": ["vigil_cfc_deterioration.pt", "vigil_cfc_checkpoint.pt"],
    "sepsis": ["vigil_cfc_sepsis.pt"],
    "mortality": ["vigil_cfc_mortality.pt"],
}

# Canonical PRODUCTION feature vector assembled from the live vitals table:
#   7 vitals + AVPU + age. Case-specific labs are appended once available.
_CFC_FEATURE_ORDER = [
    "heart_rate",
    "spo2",
    "temperature",
    "systolic_bp",
    "map",
    "diastolic_bp",
    "respiratory_rate",
    "avpu",
    "age",
]
_CFC_FEATURE_MEAN = [83.0, 96.5, 36.9, 122.0, 82.0, 63.0, 19.0, 0.2, 62.0]
_CFC_FEATURE_STD = [18.0, 3.0, 0.7, 23.0, 17.0, 13.0, 5.0, 0.6, 18.0]
_CFC_VITALS_DEFAULTS = {
    "heart_rate": 80.0,
    "spo2": 97.0,
    "temperature": 36.8,
    "systolic_bp": 120.0,
    "map": 80.0,
    "diastolic_bp": 70.0,
    "respiratory_rate": 18.0,
    "avpu": 0.0,
    "age": 62.0,
}
# AVPU/ACVPU as a graded model input (alert=0 ... unresponsive=3).
_AVPU_MAP = {
    "alert": 0.0,
    "a": 0.0,
    "verbal": 1.0,
    "v": 1.0,
    "pain": 2.0,
    "p": 2.0,
    "confusion": 3.0,
    "c": 3.0,
    "new_confusion": 3.0,
    "unresponsive": 3.0,
    "u": 3.0,
}


def _load_cfc_models() -> bool:
    """Lazy-load whichever per-outcome CfC checkpoints exist. Idempotent."""
    global _cfc_models, _cfc_meta, _cfc_device, _cfc_loaded
    if _cfc_loaded:
        return len(_cfc_models) > 0
    _cfc_loaded = True
    _cfc_prediction_cache.clear()

    try:
        import torch
        from models.vigil_cfc import HORIZONS_HOURS, OrbVigilCfC
    except Exception as e:
        logger.info("[Vigil-CfC] torch/model import failed (NEWS2-only): %s", e)
        return False

    # These models are tiny (hidden≈64) and score in milliseconds — but torch's
    # default intra-op pool spawns one OpenMP thread per core, and those threads
    # compete for the GIL/cores with the API process. One thread is plenty and
    # keeps in-process inference from contributing to event-loop starvation.
    try:
        torch.set_num_threads(1)
    except Exception:
        pass

    _cfc_device = torch.device("mps" if torch.backends.mps.is_available() else "cpu")

    for outcome, filenames in _CFC_CHECKPOINTS.items():
        path = next((_MODELS_DIR / f for f in filenames if (_MODELS_DIR / f).exists()), None)
        if path is None:
            continue
        try:
            # SECURITY: weights_only=True. weights_only=False unpickles arbitrary
            # objects, so a tampered .pt in models/ was code execution at boot.
            # scripts/train_vigil_cfc.py saves only a state_dict plus plain
            # ints, strings, floats and lists (feature stats go through
            # .tolist()), all of which the safe loader accepts. A checkpoint
            # pickled any other way now fails to load, is logged below, and
            # Vigil runs NEWS2-only: the safe failure.
            ckpt = torch.load(str(path), map_location=_cfc_device, weights_only=True)
            input_dim = ckpt.get("input_dim", len(_CFC_FEATURE_ORDER))
            hidden_dim = ckpt.get("hidden_dim", 64)
            horizons = tuple(ckpt.get("horizons", HORIZONS_HOURS))
            model = OrbVigilCfC(
                input_dim=input_dim, hidden_dim=hidden_dim, horizons=horizons, outcome=outcome
            )
            model.load_state_dict(ckpt["model_state_dict"])
            model.to(_cfc_device)
            model.eval()
            _cfc_models[outcome] = model
            _cfc_meta[outcome] = {
                "input_dim": input_dim,
                "feature_order": ckpt.get("feature_order", _CFC_FEATURE_ORDER),
                "mean": ckpt.get("feature_mean", _CFC_FEATURE_MEAN),
                "std": ckpt.get("feature_std", _CFC_FEATURE_STD),
                "horizons": horizons,
            }
            params = sum(p.numel() for p in model.parameters())
            logger.info(
                "[Vigil-CfC] Loaded '%s' (%s params) on %s", outcome, f"{params:,}", _cfc_device
            )
        except Exception as e:
            logger.error("[Vigil-CfC] Failed to load '%s' from %s: %s", outcome, path.name, e)

    if not _cfc_models:
        logger.info("[Vigil-CfC] No checkpoints — NEWS2-only (CfC overlay disabled).")
    return len(_cfc_models) > 0


def _assemble_feature_sequence(patient_id: str, feature_order, mean, std):
    """Build a normalized (1, seq, F) feature tensor + (1, seq, 1) Δt tensor
    from the patient's recent live vitals. Returns (None, None) if too sparse."""
    from datetime import datetime

    import torch

    with db_session() as db:
        rows = db.execute(
            """SELECT heart_rate, spo2, temperature, systolic_bp, diastolic_bp,
                      respiratory_rate, consciousness, timestamp
               FROM vitals WHERE patient_id = ? AND voided_at IS NULL
               ORDER BY timestamp DESC, id DESC LIMIT 24""",
            (patient_id,),
        ).fetchall()
        # A readmission's earlier stay is not this stay's history (ADT-S9).
        rows = this_stay_rows(rows, current_stay_start_on(db, patient_id))
        prow = db.execute("SELECT age FROM patients WHERE id = ?", (patient_id,)).fetchone()

    if not rows or len(rows) < 3:
        return None, None

    rows = list(reversed(rows))  # chronological
    age = float(prow["age"]) if prow and prow["age"] is not None else _CFC_VITALS_DEFAULTS["age"]

    feats, dt_seq, prev_ts = [], [0.0], None
    for row in rows:
        keys = row.keys()
        raw = {}
        for col in (
            "heart_rate",
            "spo2",
            "temperature",
            "systolic_bp",
            "diastolic_bp",
            "respiratory_rate",
        ):
            val = row[col] if col in keys and row[col] is not None else None
            raw[col] = float(val) if val is not None else _CFC_VITALS_DEFAULTS[col]
        raw["map"] = raw["diastolic_bp"] + (raw["systolic_bp"] - raw["diastolic_bp"]) / 3.0
        cons = row["consciousness"] if "consciousness" in keys and row["consciousness"] else "alert"
        raw["avpu"] = _AVPU_MAP.get(str(cons).lower(), 0.0)
        raw["age"] = age

        vec = [
            (float(raw.get(col, _CFC_VITALS_DEFAULTS.get(col, 0.0))) - mean[j]) / std[j]
            for j, col in enumerate(feature_order)
        ]
        feats.append(vec)

        ts = row["timestamp"]
        try:
            ts = datetime.fromisoformat(ts.replace("Z", "+00:00")) if isinstance(ts, str) else ts
            if prev_ts is not None:
                dt_seq.append(max(0.0, (ts - prev_ts).total_seconds() / 3600.0))
            prev_ts = ts
        except Exception:
            if len(dt_seq) < len(feats):
                dt_seq.append(1.0)
    while len(dt_seq) < len(feats):
        dt_seq.append(1.0)

    vitals_t = torch.tensor([feats], dtype=torch.float32, device=_cfc_device)  # (1,seq,F)
    dt_t = torch.tensor(
        [[[d] for d in dt_seq]], dtype=torch.float32, device=_cfc_device
    )  # (1,seq,1)
    return vitals_t, dt_t


def compute_cfc_risk(patient_id: str) -> dict | None:
    """Run every loaded per-outcome CfC model -> multi-horizon risk trajectories.

    A model runs only if its input_dim matches the live production feature schema
    (9: vitals + AVPU + age); models trained on a richer schema (e.g. sepsis+labs)
    are skipped until matching feature assembly exists. Returns None if no model
    runs or data is insufficient.

    None, and nothing loaded, while the models are switched off
    (ORB_MODELS_ENABLED, or the Orb Ward Record): the overlay is a learned
    model, and the NEWS2 baseline stands alone.
    """
    from services.model_switch import models_enabled

    if not models_enabled():
        return None
    if not _load_cfc_models():
        return None

    import torch

    from services.gpu_coordinator import vram_guard

    outcomes_out, seq_len = {}, 0
    for outcome, model in _cfc_models.items():
        meta = _cfc_meta[outcome]
        if meta["input_dim"] != len(_CFC_FEATURE_ORDER):
            continue  # schema mismatch — not runnable on live vitals yet
        vitals_t, dt_t = _assemble_feature_sequence(
            patient_id, meta["feature_order"], meta["mean"], meta["std"]
        )
        if vitals_t is None:
            # Data-sufficiency is PER-MODEL: one sparse outcome must not suppress
            # the whole overlay (previously `return None` here disabled every
            # outcome for the patient the moment any single one lacked data).
            logger.debug(
                "[Vigil-CfC] Skipping outcome '%s' for %s — insufficient vitals history.",
                outcome,
                patient_id,
            )
            continue
        seq_len = vitals_t.shape[1]
        # Reuse only identical model state AND freshly assembled full inputs.
        # A latest-vitals ID or a TTL misses age/history corrections/readmissions.
        # Tensor versions also invalidate after load_state_dict/in-place updates.
        cache_key = (
            patient_id,
            outcome,
            model,
            tuple(p._version for p in model.parameters()),
            tuple(b._version for b in model.buffers()),
            repr(meta),
            str(vitals_t.dtype),
            tuple(vitals_t.shape),
            vitals_t.detach().cpu().contiguous().numpy().tobytes(),
            str(dt_t.dtype),
            tuple(dt_t.shape),
            dt_t.detach().cpu().contiguous().numpy().tobytes(),
        )

        def predict(
            outcome: str = outcome,
            model: "OrbVigilCfC" = model,
            vitals_t: torch.Tensor = vitals_t,
            dt_t: torch.Tensor = dt_t,
        ) -> dict:
            with vram_guard(f"Vigil CfC:{outcome}"):
                with torch.no_grad():
                    return model.predict_trajectory(vitals_t, dt_t)

        traj = _cfc_prediction_cache.get_or_compute(cache_key, predict)
        outcomes_out[outcome] = {
            "risk_by_horizon": traj["risk_by_horizon"],
            "peak_risk": traj["peak_risk"],
        }

    if not outcomes_out:
        return None

    if "deterioration" in outcomes_out:
        primary = outcomes_out["deterioration"]["peak_risk"]
    else:
        primary = max(o["peak_risk"] for o in outcomes_out.values())

    return {
        "outcomes": outcomes_out,
        "primary_risk": primary,
        "seq_len": seq_len,
        "model": "CfC-LNN",
    }


# Realistic maximum NEWS2 aggregate (RR 3 + SpO2 3 + O2 2 + SBP 3 + HR 3 +
# Temp 2 + ACVPU 3 = 19). Used to normalize the ordinal aggregate into a 0-1
# index WITHOUT compressing the sickest patients (the old /10 made every NEWS2
# >=10 read as 1.0). This index is NOT a probability — see compute_news2.
_NEWS2_MAX_AGGREGATE = 20.0


# ── CLINICAL SEVERITY IS NOT THE POINT BAND ──────────────────────────────────
# The RCP 3-point bands are ESCALATION triggers, deliberately set well inside
# the range where a patient is still only moderately abnormal — that is the
# whole point of an early warning score. The driver labels used to take their
# adjective from the band, so a NEWS2 of 3 printed "Severe hypoxia (SpO2 91%)",
# "Severe tachypnea (RR 25)" and "Severe hypotension (SBP 85)". None of those
# three values is severe, a physician spots it in the first minute, and because
# the same strings are injected into Sage's prompt the model repeated the
# overstatement back in prose (see docs/perf/sage-eval-3.json).
#
# So: the POINTS stay bolted to the RCP table (they are the pinned authoritative
# value and must never move); the WORD is chosen from the value against the
# thresholds below. Chosen to be the point at which a clinician would actually
# use the word "severe":
#
#   SpO2 < 88 %   — (Scale 1) the floor of the accepted COPD target range, and
#                   the knee of the oxyhaemoglobin dissociation curve (PaO2
#                   ~55 mmHg) below which small further falls cost a lot of
#                   oxygen content. 88-91 % is real hypoxaemia, not severe.
#   SpO2 < 84 %   — (Scale 2) SEVERITY IS RELATIVE TO THE TARGET RANGE, which is
#                   the entire reason Scale 2 exists. A chronic type-2 patient
#                   whose target is 88-92 % is not in severe hypoxia at 86 % the
#                   way a Scale-1 patient at 86 % is; imposing the Scale-1 word
#                   on them would be the same category error as imposing the
#                   Scale-1 score. This does coincide with the RCP Scale-2
#                   3-point edge — not because the label is bolted to the band,
#                   but because RCP put that band at the clinically severe point
#                   for this population. test_vigil_driver_labels.py sweeps the
#                   whole range to show the label follows the value.
#   RR  >= 30     — marked respiratory distress / impending failure. 25-29 is a
#                   genuinely worrying rate, not a peri-arrest one.
#   RR  <= 8      — respiratory depression; the whole RCP low band is severe.
#   SBP < 80      — shock-range perfusion failure. 80-90 is hypotension that
#                   needs treating, not a periarrest blood pressure.
#   SBP >= 220    — hypertensive-crisis range; the whole RCP high band is severe.
#   HR  >= 140    — sustained rate at which diastolic filling and coronary
#                   perfusion drop off. 131-139 is tachycardia, not severe.
#   HR  <= 40     — the whole RCP low band is severe bradycardia.
#   Temp < 32 C   — the mild/moderate hypothermia boundary; 32-35 C (the whole
#                   RCP 3-point band) is MILD hypothermia and saying otherwise
#                   would overstate in the same way.
#
# Anything that scores 3 without reaching one of these gets "Moderate", which
# sits above the unqualified 2-point wording ("Hypoxia", "Tachypnea",
# "Hypotension", "Tachycardia") and below "Severe" — so the four-rung ladder
# reads Mild -> (unqualified) -> Moderate -> Severe while the points stay put.
_SEVERE_THRESHOLDS = {
    "spo2_low": 88.0,  # severe if SpO2  <  88 % (Scale 1)
    "spo2_low_scale_2": 84.0,  # severe if SpO2  <  84 % (Scale 2 — see above)
    "rr_high": 30.0,  # severe if RR    >= 30
    "rr_low": 8.0,  # severe if RR    <=  8
    "sbp_low": 80.0,  # severe if SBP   <  80
    "sbp_high": 220.0,  # severe if SBP   >= 220
    "hr_high": 140.0,  # severe if HR    >= 140
    "hr_low": 40.0,  # severe if HR    <=  40
    "temp_low": 32.0,  # severe if Temp  <  32.0 C
}


# ── ONE NUMBER PER VITAL: SCORE THE VALUE AS CHARTED ─────────────────────────
# Ruled by Samuel 2026-10-02; found 2026-10-01 by the PhysioNet replay
# fact-check (docs/fundraising/research_2026-10-01/R5_real_data_validation_
# VERIFY.md, section 8). The RCP NEWS2 chart is written in whole numbers
# (RR, SpO2, SBP, HR) and in tenths of a degree (temperature). Vitals reach this
# function as floats (the monitor lane and the simulator produce 29.6, 83.1, and
# the PhysioNet replay feeds hourly medians), and a float such as RR 20.4 is not
# on the chart at all: it sits in the gap between 20 and 21.
#
# Before this rule the POINTS came from the raw float and the LABEL printed it
# rounded, so the gap was resolved by whichever comparison operator each branch
# happened to use, and the number on screen could contradict its own points:
#   RR 20.4   -> 2 points, "Tachypnea (RR 20)"      chart says RR 20 = 0
#   SBP 90.4  -> 2 points, "Hypotension (SBP 90)"   chart says SBP 90 = 3
# The second kind is the dangerous one. On every LOW-side edge (RR 8, HR 40,
# SBP 90, Temp 35.0, Scale-2 SpO2 83) raw banding UNDER-scored and dropped the
# single-parameter-3 escalation, while printing a number that earns it.
#
# THE RULE: each numeric vital is rounded HALF-UP to the chart's precision
# BEFORE banding, and that one rounded value is what earns the points, picks the
# severity word, is printed in the label, and is returned as breakdown `value`.
# A nurse can therefore check any row against the paper chart and get the same
# points. The raw measurement stays in the vitals row; it is deliberately NOT
# echoed in the breakdown, because the breakdown is injected into Sage's prompt
# and a raw number beside its points would hand the contradiction to the model.
#
# The rounding itself (half-up, on the value's decimal form rather than its
# binary one) lives in services/vital_charting.py, shared with every surface
# that prints a vital beside a NEWS2 score. tests/test_news2_charted_value.py
# pins every band edge with fractional input.


# The RCP NEWS2 chart, top to bottom. Parameters with the same points and the
# same severity are listed in this order, in the drivers and in the Relay
# reason line alike (ruled 2026-10-02). It is NOT the order compute_news2
# checks them in: the engine checks temperature before consciousness, the
# chart the other way.
_RCP_CHART_ORDER = {
    "Respiratory rate": 0,
    "SpO2": 1,
    "SpO2 (Scale 2)": 1,
    "Supplemental O2": 2,
    "Systolic BP": 3,
    "Heart rate": 4,
    "Consciousness (ACVPU)": 5,
    "Temperature": 6,
}


# ── The boot guard: scoring switched off (Orb Ward Record) ────────────────────
# With ORB_SCORING_ENABLED off (or ORB_WARD_RECORD on, which forces it off) no
# NEWS2 may be computed, shown or logged anywhere. Every scorer funnels through
# compute_news2 (the RCP table), score_vitals_row (table plus the CfC overlay)
# or risk_for_row (the chart's entry point), and each of the three refuses with
# ScoringSwitchedOff instead of answering. A lane that still tries to score
# fails loudly rather than quietly putting a band on a Ward Record screen, and
# tests/test_ward_record_profile_boot.py boots the profile and fails on any
# such call. Callers ask scoring_enabled() first and return their no-score
# shape (SCORING_OFF_REASON is the sentence a screen shows).

#: What a screen says in place of a score while scoring is switched off.
SCORING_OFF_REASON = "Scores are switched off on this computer."


class ScoringSwitchedOff(RuntimeError):
    """A scoring entry point was called while scoring is switched off."""


#: Where each refused call came from, newest last, capped. The profile boot
#: test reads it, because a caller that catches the exception and carries on
#: would otherwise hide the call it made.
SWITCHED_OFF_CALLS: list[str] = []
_SWITCHED_OFF_CALLS_CAP = 200


def _refuse_when_switched_off(entry: str) -> None:
    if scoring_enabled():
        return
    import traceback

    where = "".join(traceback.format_stack(limit=8)[:-2])
    if len(SWITCHED_OFF_CALLS) < _SWITCHED_OFF_CALLS_CAP:
        SWITCHED_OFF_CALLS.append(f"{entry}\n{where}")
    logger.error(
        "%s was called with scoring switched off; no score was computed. Caller:\n%s", entry, where
    )
    raise ScoringSwitchedOff(
        f"{entry} was called with scoring switched off "
        "(ORB_SCORING_ENABLED is off or ORB_WARD_RECORD is on)"
    )


# Load shedding logic will now rely on llm_queue size
def compute_news2(
    hr, sbp, rr, temp, spo2, consciousness=None, on_oxygen=None, on_scale_2=False
) -> dict:
    """UK National Early Warning Score 2 (NEWS2).

    Supports both SpO2 scoring scales:
      - Scale 1 (default): target SpO2 >= 96%.
      - Scale 2 (on_scale_2=True): target 88-92%, for patients with COPD /
        chronic type-2 respiratory failure. Selected per-patient via the
        patients.on_scale_2 flag.

    A single parameter scoring 3 elevates the patient to at least 'elevated'
    regardless of total (the NEWS2 single-parameter rule). Returns the same shape
    as the legacy MEWS output (score 0-1, level, drivers) so callers keep working.

    ``consciousness`` and ``on_oxygen`` default to None, meaning NOT ASSESSED —
    not "alert" and not "on air". They SCORE the same as alert/on-air (an
    unassessed parameter contributes 0 points, exactly as an unmeasured heart
    rate does), but they are reported as missing in ``missing_parameters`` /
    ``complete`` rather than being silently counted as a normal finding. These
    two are the parameters a monitor feed can never carry — they are human
    assessments — so in the ingestion lane "absent" is the normal case and the
    coverage flag has to say so. A caller that genuinely has the assessment
    passes it explicitly (``consciousness='alert'``, ``on_oxygen=False`` are
    positive statements that a human looked).

    Every numeric vital is scored AS CHARTED: rounded half-up to the RCP chart's
    precision before banding, so the points, the severity word, the number in
    the label and breakdown ``value`` are all the same number (see
    ``services.vital_charting.as_charted``). A raw RR of 20.4 is scored,
    printed and returned as 20.
    """
    _refuse_when_switched_off("compute_news2")
    # Rebinding on purpose: below this line no branch can reach the raw float,
    # so no band, word or label can be computed from a different number than
    # the one the others use.
    rr, spo2, sbp, hr = (as_charted(v) for v in (rr, spo2, sbp, hr))
    temp = as_charted(temp, 1)

    score = 0
    any_param_3 = False
    # (points, severe, chart rank, text) until the worst-first sort below.
    drivers: list[tuple[int, bool, int, str]] = []
    # Per-parameter attribution: which vital contributed how many NEWS2 points,
    # so the UI can show a transparent, deterministic "why this score" derived
    # straight from the RCP scoring table — NOT a learned/validated model.
    breakdown: list[dict] = []

    def add(points, msg, *, parameter=None, value=None, severe=False):
        nonlocal score, any_param_3
        score += points
        if points == 3:
            any_param_3 = True
        if points > 0 and msg:
            drivers.append(
                (points, severe, _RCP_CHART_ORDER.get(parameter, len(_RCP_CHART_ORDER)), msg)
            )
        if parameter is not None:
            breakdown.append(
                {
                    "parameter": parameter,
                    "value": value,
                    "points": points,
                    "note": msg or None,
                    # Outranks other entries with the same points (ruled
                    # 2026-10-03): set where the word "Severe" is chosen, and on an
                    # ACVPU change, which sorts with Severe. Never parsed from `note`.
                    "severe": severe,
                }
            )

    # Respiration rate
    if rr is not None:
        if rr <= 8:
            _w = "Severe" if rr <= _SEVERE_THRESHOLDS["rr_low"] else "Moderate"
            add(
                3,
                f"{_w} bradypnea (RR {rr:.0f})",
                parameter="Respiratory rate",
                value=rr,
                severe=_w == "Severe",
            )
        elif rr <= 11:
            add(1, f"Low RR ({rr:.0f})", parameter="Respiratory rate", value=rr)
        elif rr <= 20:
            add(0, "", parameter="Respiratory rate", value=rr)
        elif rr <= 24:
            add(2, f"Tachypnea (RR {rr:.0f})", parameter="Respiratory rate", value=rr)
        else:
            _w = "Severe" if rr >= _SEVERE_THRESHOLDS["rr_high"] else "Moderate"
            add(
                3,
                f"{_w} tachypnea (RR {rr:.0f})",
                parameter="Respiratory rate",
                value=rr,
                severe=_w == "Severe",
            )

    # SpO2 — Scale 1 (default) or Scale 2 (COPD / chronic type-2 resp failure).
    # Per RCP NEWS2. Scale 2's upper band depends on whether the patient is on
    # air or supplemental oxygen (over-oxygenation is dangerous in this group).
    if spo2 is not None:
        _spo2_label = "SpO2 (Scale 2)" if on_scale_2 else "SpO2"
        # One rule, two thresholds: the word comes from the saturation measured
        # against THIS patient's target range, because that is what Scale 2 is.
        _spo2_severe_at = _SEVERE_THRESHOLDS["spo2_low_scale_2" if on_scale_2 else "spo2_low"]
        _spo2_w = "Severe" if spo2 < _spo2_severe_at else "Moderate"
        if on_scale_2:
            if spo2 <= 83:
                add(
                    3,
                    f"{_spo2_w} hypoxia (SpO2 {spo2:.0f}%, Scale 2)",
                    parameter=_spo2_label,
                    value=spo2,
                    severe=_spo2_w == "Severe",
                )
            elif spo2 <= 85:
                add(2, f"Hypoxia (SpO2 {spo2:.0f}%, Scale 2)", parameter=_spo2_label, value=spo2)
            elif spo2 <= 87:
                add(1, f"Low SpO2 ({spo2:.0f}%, Scale 2)", parameter=_spo2_label, value=spo2)
            elif spo2 <= 92:
                add(0, "", parameter=_spo2_label, value=spo2)  # target range
            elif not on_oxygen:
                add(0, "", parameter=_spo2_label, value=spo2)  # >=93 on air is fine
            elif spo2 <= 94:
                add(
                    1,
                    f"Over-target SpO2 on O2 ({spo2:.0f}%, Scale 2)",
                    parameter=_spo2_label,
                    value=spo2,
                )
            elif spo2 <= 96:
                add(
                    2,
                    f"Over-target SpO2 on O2 ({spo2:.0f}%, Scale 2)",
                    parameter=_spo2_label,
                    value=spo2,
                )
            else:
                add(
                    3,
                    f"Over-oxygenation on O2 ({spo2:.0f}%, Scale 2)",
                    parameter=_spo2_label,
                    value=spo2,
                )
        else:
            if spo2 >= 96:
                add(0, "", parameter=_spo2_label, value=spo2)
            elif spo2 >= 94:
                add(1, f"Mild hypoxia (SpO2 {spo2:.0f}%)", parameter=_spo2_label, value=spo2)
            elif spo2 >= 92:
                add(2, f"Hypoxia (SpO2 {spo2:.0f}%)", parameter=_spo2_label, value=spo2)
            else:
                add(
                    3,
                    f"{_spo2_w} hypoxia (SpO2 {spo2:.0f}%)",
                    parameter=_spo2_label,
                    value=spo2,
                    severe=_spo2_w == "Severe",
                )

    # Supplemental oxygen parameter (+2, applies in both scales)
    if on_oxygen:
        add(2, "On supplemental oxygen", parameter="Supplemental O2", value=True)

    # Systolic BP
    if sbp is not None:
        if sbp <= 90:
            _w = "Severe" if sbp < _SEVERE_THRESHOLDS["sbp_low"] else "Moderate"
            add(
                3,
                f"{_w} hypotension (SBP {sbp:.0f})",
                parameter="Systolic BP",
                value=sbp,
                severe=_w == "Severe",
            )
        elif sbp <= 100:
            add(2, f"Hypotension (SBP {sbp:.0f})", parameter="Systolic BP", value=sbp)
        elif sbp <= 110:
            add(1, f"Low BP (SBP {sbp:.0f})", parameter="Systolic BP", value=sbp)
        elif sbp <= 219:
            add(0, "", parameter="Systolic BP", value=sbp)
        else:
            # Reads the threshold rather than hardcoding "Severe" even though
            # today's band floor (220) is already above it. Every severity word
            # in this function is derived from the VALUE — no branch is allowed
            # to assert "this band is always severe" by hand, because that is
            # precisely the assumption that produced D5.
            _w = "Severe" if sbp >= _SEVERE_THRESHOLDS["sbp_high"] else "Moderate"
            add(
                3,
                f"{_w} hypertension (SBP {sbp:.0f})",
                parameter="Systolic BP",
                value=sbp,
                severe=_w == "Severe",
            )

    # Pulse
    if hr is not None:
        if hr <= 40:
            _w = "Severe" if hr <= _SEVERE_THRESHOLDS["hr_low"] else "Moderate"
            add(
                3,
                f"{_w} bradycardia (HR {hr:.0f})",
                parameter="Heart rate",
                value=hr,
                severe=_w == "Severe",
            )
        elif hr <= 50:
            add(1, f"Bradycardia (HR {hr:.0f})", parameter="Heart rate", value=hr)
        elif hr <= 90:
            add(0, "", parameter="Heart rate", value=hr)
        elif hr <= 110:
            add(1, f"Mild tachycardia (HR {hr:.0f})", parameter="Heart rate", value=hr)
        elif hr <= 130:
            add(2, f"Tachycardia (HR {hr:.0f})", parameter="Heart rate", value=hr)
        else:
            _w = "Severe" if hr >= _SEVERE_THRESHOLDS["hr_high"] else "Moderate"
            add(
                3,
                f"{_w} tachycardia (HR {hr:.0f})",
                parameter="Heart rate",
                value=hr,
                severe=_w == "Severe",
            )

    # Temperature
    if temp is not None:
        if temp <= 35.0:
            # 32-35 C is MILD hypothermia. The RCP 3-point band starts at 35.0,
            # so the band alone cannot carry the word.
            _w = "Severe hypothermia" if temp < _SEVERE_THRESHOLDS["temp_low"] else "Hypothermia"
            add(
                3,
                f"{_w} ({temp:.1f}°C)",
                parameter="Temperature",
                value=temp,
                severe=_w == "Severe hypothermia",
            )
        elif temp <= 36.0:
            add(1, f"Low temp ({temp:.1f}°C)", parameter="Temperature", value=temp)
        elif temp <= 38.0:
            add(0, "", parameter="Temperature", value=temp)
        elif temp <= 39.0:
            add(1, f"Fever ({temp:.1f}°C)", parameter="Temperature", value=temp)
        else:
            add(2, f"High fever ({temp:.1f}°C)", parameter="Temperature", value=temp)

    # Consciousness (ACVPU): Alert=0; new Confusion/Voice/Pain/Unresponsive=3
    #
    # .strip() is load-bearing, not tidiness. Without it a whitespace-only value
    # — "   " from a padded HL7 field or an imported chart — is truthy, so it
    # missed the `or "alert"` fallback, failed the membership test below, and
    # FABRICATED a 3-point altered-consciousness escalation on a patient nobody
    # had assessed. A blank field is an absence: it scores 0 and is reported in
    # `missing_parameters` (below) rather than invented as a finding either way.
    cons = str(consciousness or "alert").strip().lower()
    if cons not in ("alert", "a", ""):
        # A new ACVPU change has no Severe/Moderate word; it sorts with Severe
        # (ruled 2026-10-03): new confusion or worse is a red flag.
        add(
            3,
            "Altered consciousness (ACVPU)",
            parameter="Consciousness (ACVPU)",
            value=cons,
            severe=True,
        )

    # Level from NEWS2 clinical bands, honouring the single-parameter-3 rule.
    if score >= 7:
        level = "critical"
    elif score >= 5 or any_param_3:
        level = "elevated"
    else:
        level = "stable"

    # `score` is a 0-1 NORMALIZED INDEX of the ordinal NEWS2 aggregate — it is
    # NOT a calibrated probability of deterioration, and callers/UI must label it
    # as such (or prefer raw_news2 + level). It is normalized against the realistic
    # NEWS2 maximum aggregate (_NEWS2_MAX_AGGREGATE) rather than the old /10 cap,
    # which flattened the sickest patients (NEWS2 10, 14, 20 all read as 1.0).
    # The clinical band/escalation decision is taken from the raw integer above,
    # never from this index.
    normalized = min(1.0, score / _NEWS2_MAX_AGGREGATE)

    # COVERAGE HONESTY, the same statement `interaction_screened` makes in
    # drug_safety_service and `allergies_on_file` makes in allergy_interlock.
    # A NEWS2 of 0 computed from a heart rate ALONE was indistinguishable from a
    # NEWS2 of 0 computed from a full set (probed 2026-08-06: HR 70 by itself
    # returned raw_news2 0, level 'stable', and nothing in the payload said six
    # of the seven parameters were never measured). An unmeasured parameter
    # contributes 0 points, so a partial set can only ever UNDER-score — which
    # is the direction that matters. The aggregate is still returned, because a
    # partial score is genuinely useful; it is now labelled.
    #
    # ALL SEVEN, not five (fixed 2026-08-14). The flag used to track the five
    # NUMERIC vitals only, so a row with no ACVPU assessment and no oxygen
    # documentation was scored Alert + on-air (0 points each) and reported
    # `complete: true` — a 5-of-7 score presented as a full NEWS2. A monitor feed
    # NEVER carries ACVPU or supplemental O2 (both are human assessments), so in
    # the ingestion lane the pilot will actually run, that was the normal case,
    # and the two parameters it hid are the two biggest single escalators on the
    # RCP table (+3 and +2). `None` is "not assessed"; an explicit value —
    # including 'alert' or False — is a positive statement that someone looked.
    _measured = {
        "Respiratory rate": rr is not None,
        "SpO2": spo2 is not None,
        "Systolic BP": sbp is not None,
        "Heart rate": hr is not None,
        "Temperature": temp is not None,
        "Consciousness (ACVPU)": consciousness is not None and str(consciousness).strip() != "",
        "Supplemental O2": on_oxygen is not None,
    }
    missing = sorted(k for k, present in _measured.items() if not present)

    # WORST FIRST (ruled 2026-10-02): by points, then Severe before Moderate
    # (2026-10-03), then RCP chart order. The list is cut
    # to three below and used to keep the first three in CHECK order, so a new
    # ACVPU change behind three 1-point vitals (RR 10, SpO2 95, HR 105) was not
    # in it: the alert card strip, Sage's prompt, the guideline query and the
    # handoff never said the patient was confused. Same order as the Relay
    # reason line (`news2_reason_line`), so the two cannot disagree.
    driver_texts = [text for *_, text in sorted(drivers, key=lambda d: (-d[0], not d[1], d[2]))]

    if not driver_texts:
        driver_texts = (
            ["All vitals within normal range"]
            if not missing
            else [
                f"Within range on what was measured ({len(_measured) - len(missing)}/"
                f"{len(_measured)} parameters)"
            ]
        )

    return {
        "score": round(normalized, 2),
        "level": level,
        "raw_news2": score,
        "raw_mews": score,  # legacy key alias
        "single_param_3": any_param_3,
        "drivers": driver_texts[:3],
        # True only when every scored vital was actually measured. False means
        # this aggregate is a FLOOR, not a NEWS2 — callers and the UI must say so
        # rather than presenting it as a complete score.
        "complete": not missing,
        "missing_parameters": missing,
        "coverage_note": None
        if not missing
        else (
            "INCOMPLETE observation set: "
            + ", ".join(missing)
            + " not measured. An unmeasured parameter scores 0, so this NEWS2 is a "
            "MINIMUM and the true score can only be higher."
        ),
        # Full per-parameter attribution, worst-contributor first, so the UI can
        # render a transparent "why this NEWS2" panel. Deterministic from the RCP
        # table — explicitly NOT a validated deterioration probability.
        "breakdown": sorted(breakdown, key=lambda b: -b["points"]),
        "scoring_model": "NEWS2",
        "spo2_scale": 2 if on_scale_2 else 1,
    }


# ── THE RELAY REASON LINE IS THE NEWS2 BREAKDOWN (ruled 2026-10-02) ──────────
# The three Relay alert lanes (bot_service.evaluate_and_dispatch_alerts,
# nurse_shift_service.dispatch_observation_alert and
# ingestion_service._check_ingested_alert) each composed their own reason
# phrases against their own ad-hoc cutoffs, and the copies drifted from the
# table that actually scored the patient. Measured through the real lanes on
# 2026-10-02:
#   SBP 88 + HR 105          -> "Tachycardia: HR 105 bpm". The 3-point
#                               hypotension that escalated the patient: gone.
#   new confusion + three 1-point vitals -> "Tachycardia: HR 105 bpm".
#   Scale 2, SpO2 90 on air  -> "SpO2 dropping: 90%". On target, 0 points.
#   SBP 170 (0 points)       -> "Hypertension" in the third slot of a critical
#                               alert, pushing the 3-point RR 26 off the line.
#   HR 125                   -> "Severe tachycardia" in two lanes, plain
#                               "Tachycardia" in the third and in the drivers.
# The reason line is now composed HERE, from the per-parameter breakdown, so its
# words and numbers ARE the driver text: every parameter that scored, worst
# first, the first three named and the rest counted ("+N more") so nothing
# that scored is hidden without saying so. Worst first means by points, then
# Severe before Moderate, then RCP chart order. The middle key was added on
# 2026-10-03 after a review found septic shock (HR 142, SBP 82, RR 26, SpO2
# 90) naming three Moderate findings and counting Severe tachycardia, which
# the old line had shown. A new ACVPU change sorts with Severe. A parameter
# that scored 0 is never a reason, however striking the number; it is on the
# vitals line printed underneath.
#
# Built from `breakdown`, not `drivers`. Both are worst first in chart order
# since 2026-10-02, but `drivers` is cut to three (and the CfC overlay can
# prepend to it) while the "+N more" count needs every parameter that scored.


def news2_reason_line(news2: dict, limit: int = 3) -> str:
    """The reason line of a Relay alert: the NEWS2 parameters that scored.

    Worst first (points, then Severe before Moderate), ties in RCP chart
    order, the first `limit` named and any
    others counted, e.g. "Severe tachypnea (RR 32); Severe hypoxia (SpO2 86%);
    Severe hypotension (SBP 78) (+4 more)". Wording only, it never decides
    whether an alert is sent. A result with no breakdown (a hand-built dict)
    falls back to its drivers, then to the band.

    NEWS2 points only. The three alert lanes score with the CfC overlay OFF,
    so every alert they raise is explained by the breakdown. A caller passing
    an OVERLAID result would lose its "CfC: ..." reason, which
    `_apply_cfc_overlay` puts in `drivers`, not `breakdown`, and must add it.
    """
    news2 = news2 or {}
    scored = [
        b for b in (news2.get("breakdown") or []) if (b.get("points") or 0) > 0 and b.get("note")
    ]
    if not scored:
        drivers = news2.get("drivers") or []
        return "; ".join(drivers[:limit]) if drivers else f"NEWS2 {news2.get('level', 'stable')}"
    scored.sort(
        key=lambda b: (
            -b["points"],
            not b.get("severe"),
            _RCP_CHART_ORDER.get(b.get("parameter"), len(_RCP_CHART_ORDER)),
        )
    )
    named = "; ".join(b["note"] for b in scored[:limit])
    more = len(scored) - limit
    return f"{named} (+{more} more)" if more > 0 else named


def compute_mews(hr, sbp, rr, temp, spo2, consciousness="alert") -> dict:
    """Deprecated alias — Orb now scores with NEWS2 (UK standard)."""
    return compute_news2(hr, sbp, rr, temp, spo2, consciousness)


# ── The one displayed-score entry point ──────────────────────────────────────
# `compute_news2` is the RCP table. It is NOT the whole displayed score: once a
# CfC checkpoint is present the learned overlay may ESCALATE the level, and any
# surface that calls compute_news2 directly and then shows the result is a
# SECOND source of truth for "how sick is this patient".
#
# That already happened. `get_patient_risk` applied the overlay; the nurse shift
# board's `_score_from_vitals` called compute_news2 and stopped there. With no
# checkpoint loaded the two agreed, so nothing was red — the divergence would
# have appeared silently on the day a model landed, with the doctor's chart
# saying 'critical' and the nurse's board saying 'stable' for the same row. Both
# now go through `score_vitals_row`, which is the only place the baseline and the
# overlay are combined.


def _row_value(row, key, default=None):
    """Read `key` from a sqlite3.Row OR a plain dict, absent column → default."""
    if row is None:
        return default
    try:
        return row[key] if key in row.keys() else default
    except Exception:  # pragma: no cover — defensive on exotic row types
        return default


def _apply_cfc_overlay(result: dict, patient_id: str) -> dict:
    """Attach the CfC multi-horizon overlay and let it escalate the baseline.

    Mutates and returns `result`. A no-op (and cheap — `_load_cfc_models` short
    circuits) while no checkpoint is present.
    """
    cfc = compute_cfc_risk(patient_id)
    if cfc is None:
        return result

    result["cfc"] = cfc["outcomes"]
    result["cfc_primary_risk"] = cfc["primary_risk"]
    result["cfc_seq_len"] = cfc["seq_len"]
    result["scoring_model"] = "NEWS2+CfC-LNN"

    # Overlay may escalate (never de-escalate) the NEWS2 baseline. Escalation
    # is a function of model risk MAGNITUDE, not a fixed single-step jump, so
    # a clinically-quiet (NEWS2-stable) patient with a very high learned risk
    # — the precise case a learned overlay exists to catch — can reach
    # 'critical' rather than being capped at 'elevated'.
    primary = cfc["primary_risk"]
    if primary >= 0.9 and result["level"] in ("stable", "elevated"):
        if result["level"] != "critical":
            logger.info(
                "[Vigil-CfC] Overlay escalated %s %s→critical (primary_risk=%.2f).",
                patient_id,
                result["level"],
                primary,
            )
        result["level"] = "critical"
        result["drivers"] = (["CfC: high near-term deterioration risk"] + result["drivers"])[:3]
    elif primary >= 0.85 and result["level"] == "elevated":
        result["level"] = "critical"
    elif primary >= 0.7 and result["level"] == "stable":
        result["level"] = "elevated"
        result["drivers"] = (["CfC: rising near-term deterioration risk"] + result["drivers"])[:3]

    return result


def score_vitals_row(
    vitals, *, patient_id: str | None = None, on_scale_2: bool = False, overlay: bool = True
) -> dict | None:
    """NEWS2 + CfC overlay for ONE stored vitals row. The shared entry point.

    `vitals` is a sqlite3.Row or a dict with the vitals-table column names; None
    in gives None out (the caller has no observation for this patient).

    `on_scale_2` comes from patients.on_scale_2 and MUST be supplied by the
    caller — this function will not open a second connection to look it up,
    because several callers already hold one.

    `overlay=False` is for a CANDIDATE observation set that is not yet in the
    chart, or for a caller inside an open write transaction: the overlay reads
    the patient's stored vitals history on its own connection, which from inside
    an open write transaction is the documented WAL self-deadlock.
    """
    _refuse_when_switched_off("score_vitals_row")
    if vitals is None:
        return None

    on_oxygen_raw = _row_value(vitals, "on_oxygen")
    result = compute_news2(
        _row_value(vitals, "heart_rate"),
        _row_value(vitals, "systolic_bp"),
        _row_value(vitals, "respiratory_rate"),
        _row_value(vitals, "temperature"),
        _row_value(vitals, "spo2"),
        # Pass the stored values THROUGH, including their absence: a NULL
        # consciousness is "nobody assessed ACVPU", not "alert". compute_news2
        # scores it 0 either way and reports it missing — see its docstring.
        _row_value(vitals, "consciousness"),
        None if on_oxygen_raw is None else bool(on_oxygen_raw),
        on_scale_2=bool(on_scale_2),
    )

    if overlay and patient_id:
        _apply_cfc_overlay(result, patient_id)
    return result


def risk_for_row(row, *, patient_id: str, on_scale_2: bool) -> dict | None:
    """The displayed risk for a vitals row the caller ALREADY HOLDS.

    The chart route prints vitals[0] in the command bar, so it must score that
    row and not ask `get_patient_risk` to read "the latest" again. That second
    read was a separate session (autocommit SELECTs, no shared snapshot), and a
    bot tick or monitor message landing between the two gave a badge, gauge and
    breakdown for an observation the bar was not showing.

    No row returns the same no-vitals stub `get_patient_risk` always returned;
    otherwise this is `score_vitals_row` with the overlay ON, as the chart has
    always been scored. `on_scale_2` has no default on purpose: a caller that
    forgets the patient's scale gets a TypeError, not a Scale 1 score.
    """
    # The no-vitals stub below is a band too ("stable"), so this refuses
    # before it, not only through score_vitals_row.
    _refuse_when_switched_off("risk_for_row")
    # `not row`, not `is None`: the falsy test get_patient_risk always made.
    if not row:
        return {
            "score": 0,
            "level": "stable",
            "drivers": ["No vitals recorded"],
            "scoring_model": "NEWS2",
        }
    return score_vitals_row(row, patient_id=patient_id, on_scale_2=on_scale_2)


# ── A readmission's previous stay is never 'latest' (ADT-S9 (a), W8-A) ───────
# A patient readmitted onto the same record (opd-17c, ORB_READMISSION) keeps
# every vitals and risk_scores row of the earlier stay, keyed on the patient.
# Until a set is charted in the new stay, every "latest" reader would print the
# last stay's set, and score it, as if it were taken now. Ruling ADT-S9 (a),
# built as the safety default: the previous stay's rows are shown only with
# their date and a 'previous stay' label, never as the current latest.
#
# THE RULE. A patient on an active admission who also has an ended admission
# has a boundary: the start of the current stay (services/stay_times: the
# admission's admitted_at, else its created_at). A vitals or risk row from
# before it belongs to an earlier stay. Every reader keeps its ORDER BY
# timestamp DESC, id DESC and drops those rows AFTER reading: newest first,
# every row of this stay comes before every row of an earlier one, so what is
# left is exactly this stay's newest rows. A patient with one stay (everyone
# who has not been readmitted) has no boundary, and every reader gives exactly
# the answer it gave before. A row whose time cannot be read is never current.
# The same rule as the stay-keyed NABH and scorecard readers (W6-B): a vital
# belongs to the stay whose [start, next start) holds it.
#
# THE STAYS NEVER OVERLAP. A readmission's admission time is refused before
# the earlier stay ended (admission_service._check_readmission_time_on), so
# the boundary is the stay start. For any rows written before that refusal
# existed, the boundary is also never earlier than an earlier stay's recorded
# end (_earlier_stay_ends_on): a set charted while the earlier stay was still
# open is that stay's, whatever the later admission time says.

#: The words a screen puts beside anything from the earlier stay.
PREVIOUS_STAY_LABEL = "Previous stay"


def _earlier_stay_ends_on(db, patient_ids) -> dict:
    """{patient_id: the latest recorded end of an earlier stay, an aware UTC
    datetime}: an ended admission's admissions.discharged_at, or a discharge
    summary keyed to that stay (discharge_summaries.admission_id, migration
    65). A history import's encounters are left out, as the stay-keyed census
    leaves them out, and so is any end after now: a time not yet reached ended
    nothing, and counting it would hide this stay's own sets. Absent for a
    patient with no such end. Never writes."""
    from datetime import datetime

    from services.clinical_time import parse_to_utc

    ids = sorted({str(p) for p in patient_ids if p})
    if not ids:
        return {}
    acols = {r[1] for r in db.execute("PRAGMA table_info(admissions)").fetchall()}
    scols = {r[1] for r in db.execute("PRAGMA table_info(discharge_summaries)").fetchall()}
    native = " AND e.import_run_id IS NULL" if "import_run_id" in acols else ""
    now = datetime.now(UTC)
    out: dict = {}
    for i in range(0, len(ids), 400):
        chunk = ids[i : i + 400]
        marks = ",".join("?" for _ in chunk)
        sql = (
            "SELECT e.patient_id, e.discharged_at AS ended_at FROM admissions e "
            f"WHERE e.status <> 'active' AND e.discharged_at IS NOT NULL{native} "
            f"AND e.patient_id IN ({marks})"
        )
        args = list(chunk)
        if "admission_id" in scols:
            sql += (
                " UNION ALL SELECT e.patient_id, ds.discharged_at AS ended_at "
                "FROM discharge_summaries ds JOIN admissions e ON e.id = ds.admission_id "
                f"WHERE e.status <> 'active' AND ds.discharged_at IS NOT NULL{native} "
                f"AND e.patient_id IN ({marks})"
            )
            args += chunk
        for row in db.execute(sql, tuple(args)).fetchall():
            try:
                ended = parse_to_utc(row["ended_at"])
            except (TypeError, ValueError):
                continue
            if ended <= now and (row["patient_id"] not in out or ended > out[row["patient_id"]]):
                out[row["patient_id"]] = ended
    return out


def readmission_stay_starts_on(db, patient_ids=None) -> dict:
    """{patient_id: start of the current stay, an aware UTC datetime} for each
    patient on an active admission who also has an ended one (a readmission
    onto the same record). Absent for everyone else. `patient_ids` None means
    every such patient. Takes the caller's connection; never writes.

    The boundary is never earlier than an earlier stay's recorded end
    (_earlier_stay_ends_on): stays that overlap, which a readmission can no
    longer be given, still never show the earlier stay's sets as current."""
    from services.clinical_time import parse_to_utc

    sql = (
        "SELECT a.patient_id, COALESCE(a.admitted_at, a.created_at) AS stay_start "
        "FROM admissions a WHERE a.status = 'active' "
        "AND EXISTS (SELECT 1 FROM admissions e WHERE e.patient_id = a.patient_id "
        "AND e.rowid <> a.rowid AND e.status <> 'active')"
    )
    args: tuple = ()
    if patient_ids is not None:
        wanted = sorted({str(p) for p in patient_ids if p})
        if not wanted:
            return {}
        sql += f" AND a.patient_id IN ({','.join('?' for _ in wanted)})"
        args = tuple(wanted)
    out: dict = {}
    for row in db.execute(sql, args).fetchall():
        try:
            out[row["patient_id"]] = parse_to_utc(row["stay_start"])
        except (TypeError, ValueError):
            # An unreadable stay start: nothing before now can be shown as
            # this stay's, so the boundary is the latest possible one.
            from datetime import datetime

            logger.warning(
                "Readmission boundary: unreadable stay start %r for %s",
                row["stay_start"],
                row["patient_id"],
            )
            out[row["patient_id"]] = datetime.now(UTC)
    if out:
        for pid, ended in _earlier_stay_ends_on(db, list(out)).items():
            if ended > out[pid]:
                logger.debug(
                    "Readmission boundary: the stay of %s starts before an "
                    "earlier stay ended; bounded at that end",
                    pid,
                )
                out[pid] = ended
    return out


def current_stay_start_on(db, patient_id: str):
    """The current stay's start for a readmitted patient, else None."""
    return readmission_stay_starts_on(db, [patient_id]).get(patient_id)


def from_previous_stay(timestamp, stay_start) -> bool:
    """True when a row taken at `timestamp` predates `stay_start` (None: no
    boundary, nothing is from a previous stay). An unreadable time is never
    current, so it counts as the previous stay's."""
    if stay_start is None:
        return False
    from services.clinical_time import parse_to_utc

    try:
        return parse_to_utc(timestamp) < stay_start
    except (TypeError, ValueError):
        return True


def this_stay_rows(rows, stay_start) -> list:
    """`rows` without the ones from before `stay_start`, order kept. On a
    newest-first read that is exactly this stay's newest rows."""
    if stay_start is None:
        return list(rows)
    return [r for r in rows if not from_previous_stay(_row_value(r, "timestamp"), stay_start)]


def previous_stay_on(db, patient_id: str, stay_start, *, risk: bool = False) -> dict | None:
    """What a screen may show of the previous stay, labelled and dated: the
    last observation set taken before this stay began, and with `risk` the
    last stored risk row. None for a patient who was not readmitted.

    Nothing here is scored or banded afresh: the last set is the values as
    stored, so no screen can print a previous stay's NEWS2 as the patient's
    current one. `risk` is for callers that already checked scoring is on."""
    if stay_start is None:
        return None
    from services.clinical_time import format_utc

    bound = format_utc(stay_start)
    last = db.execute(
        "SELECT * FROM vitals WHERE patient_id = ? AND voided_at IS NULL AND timestamp < ? "
        "ORDER BY timestamp DESC, id DESC LIMIT 1",
        (patient_id, bound),
    ).fetchone()
    out: dict[str, Any] = {
        "label": PREVIOUS_STAY_LABEL,
        "this_stay_started_at": bound,
        "last_observed_at": last["timestamp"] if last else None,
        "last_vitals": (
            {
                "timestamp": last["timestamp"],
                "heart_rate": last["heart_rate"],
                "systolic_bp": last["systolic_bp"],
                "diastolic_bp": last["diastolic_bp"],
                "spo2": last["spo2"],
                "temperature": last["temperature"],
                "respiratory_rate": last["respiratory_rate"],
                "previous_stay": True,
            }
            if last
            else None
        ),
    }
    if risk:
        row = db.execute(
            "SELECT score, level, drivers, timestamp FROM risk_scores "
            "WHERE patient_id = ? AND timestamp < ? ORDER BY timestamp DESC, id DESC LIMIT 1",
            (patient_id, bound),
        ).fetchone()
        out["last_risk"] = (
            {
                "level": row["level"],
                "timestamp": row["timestamp"],
                "previous_stay": True,
            }
            if row
            else None
        )
    return out


# ── The ward's scored rows: ONE read for the board and the Sage brief ────────
# The Vigil board (GET /api/vigil/patients) and the Sage ward brief rendered
# above it (sage_narrator_service) both say who on the ward is critical. The
# brief used to band from risk_scores while the board scored the latest vitals
# row, and the simulator and the Scribe lane write vitals with no risk row, so
# on 2026-10-03 one screen showed "Priya Nair, GM-103, ELEVATED" in the brief
# over her board row at NEWS2 8, critical. Both now take their rows from
# `scored_admitted_patients`, so the two cannot disagree again.

_BAND_RANK = {"critical": 2, "elevated": 1, "stable": 0}


def board_order(patient_id: str, news2: dict | None) -> tuple:
    """The ward board's sort key. VigilPanel's `boardOrder` is its twin.

    Critical first (the CfC overlay can make a lower aggregate critical), then
    NEWS2 high to low, then at an equal NEWS2 the single-parameter-3 'elevated'
    patient above a 'stable' one, then nothing recorded, then id so a page
    boundary is stable from one poll to the next.
    """
    if news2 is None:
        return (1, 0, 0, 0, patient_id)
    level = news2.get("level")
    return (
        0,
        0 if level == "critical" else 1,
        -(news2.get("raw_news2") or 0),
        -_BAND_RANK.get((level or ""), 0),
        patient_id,
    )


def scored_admitted_patients(
    department: str | None, *, facilities: frozenset[int] | None = None
) -> list[tuple]:
    """Every admitted patient on an active admission, each with the NEWS2 of
    their latest vitals row, sorted with `board_order`. `department` None means
    the whole hospital; otherwise only that admission department.

    `facilities` is the caller's permissions.facility_scope() (multisite-8,
    W12-A): None (one facility on record, or a group-wide account) leaves both
    statements exactly as they were; otherwise only patients whose active
    admission is at one of those facilities are read, and the empty set reads
    nobody. The ward narrator's shared snapshot passes no facilities and keeps
    each reader to theirs when it serves the brief.

    Returns (patient, vitals, news2) triples. `patient` is the patients row as a
    dict plus `attending_doctor_name`, `admission_department` and
    `admission_facility_id`, and for a
    readmitted patient `previous_stay` (previous_stay_on). `vitals` is the
    latest vitals row of the current stay as a dict, or None (ruling ADT-S9:
    an earlier stay's set is never the latest). `news2` is `score_vitals_row` with the
    overlay ON on the patient's own SpO2 scale, or None when nothing is recorded
    (never a stub score: that is why this does not use `risk_for_row`).

    Opens and closes its own session and scores AFTER it closes, because the CfC
    overlay reads the vitals history on its own connection. Never call this from
    inside a db_session or a write transaction.
    """
    from services.facility_filter import column_clause

    facility_sql, facility_args = column_clause("a.facility_id", facilities)
    scope_sql = ("AND a.department = ?" if department else "") + facility_sql
    scope_args = ((department,) if department else ()) + facility_args
    with db_session() as db:
        patients = db.execute(
            f"""SELECT p.*,
                       u.name as attending_doctor_name,
                       a.department as admission_department,
                       a.facility_id as admission_facility_id
                FROM patients p
                JOIN admissions a ON p.id = a.patient_id AND a.status = 'active'
                LEFT JOIN users u ON u.id = p.attending_doctor_id
                WHERE p.status = 'admitted' {scope_sql}""",
            scope_args,
        ).fetchall()
        # ONE moment per patient: the latest vitals row, with id DESC breaking a
        # timestamp tie (a same-time correction is the higher id). Same scope as
        # the patients query above, so a tie or a scope edge cannot slip between
        # them. Timestamps are one UTC spelling since migration 43, so this text
        # order is chronological.
        latest_vitals = {
            v["patient_id"]: dict(v)
            for v in db.execute(
                f"""SELECT v.* FROM patients p
                    JOIN admissions a ON p.id = a.patient_id AND a.status = 'active'
                    JOIN vitals v ON v.id = (
                        SELECT v2.id FROM vitals v2 WHERE v2.patient_id = p.id
                          AND v2.voided_at IS NULL
                        ORDER BY v2.timestamp DESC, v2.id DESC LIMIT 1
                    )
                    WHERE p.status = 'admitted' {scope_sql}""",
                scope_args,
            ).fetchall()
        }
        # A readmitted patient's latest row from the earlier stay is not their
        # latest (ADT-S9 (a)): newest first, so no row means none this stay.
        # The patient row carries the dated, labelled previous stay instead.
        stay_starts = readmission_stay_starts_on(db, [p["id"] for p in patients])
        previous_stays = {
            pid: previous_stay_on(db, pid, start) for pid, start in stay_starts.items()
        }
        for pid, start in stay_starts.items():
            held = latest_vitals.get(pid)
            if held is not None and from_previous_stay(held.get("timestamp"), start):
                del latest_vitals[pid]

    # ORB_SCORING_ENABLED off (the Orb Ward Record): no NEWS2 is computed, so
    # every triple carries news2 None and the board falls back to ward, then
    # bed, then id (ward_bed_order). Read at call time, like the write path.
    scoring = scoring_enabled()

    scored = []
    for p in patients:
        patient = dict(p)
        if patient["id"] in previous_stays:
            patient["previous_stay"] = previous_stays[patient["id"]]
        vitals = latest_vitals.get(patient["id"])
        news2 = (
            score_vitals_row(
                vitals,
                patient_id=patient["id"],
                on_scale_2=bool(patient["on_scale_2"]),
            )
            if scoring
            else None
        )
        scored.append((patient, vitals, news2))
    if scoring:
        scored.sort(key=lambda s: board_order(s[0]["id"], s[2]))
    else:
        scored.sort(key=lambda s: ward_bed_order(s[0]))
    return scored


def _natural_key(text) -> tuple:
    """'B-10' after 'B-2': digit runs compare as numbers, the rest as text."""
    parts = re.split(r"(\d+)", str(text or "").strip().lower())
    return tuple(
        (0, int(part), "") if part.isdigit() else (1, 0, part) for part in parts if part != ""
    )


def scoring_enabled() -> bool:
    """ORB_SCORING_ENABLED, read at call time so a test or an operator flip is
    seen at once. Off (the Orb Ward Record), no NEWS2 is computed for any
    screen that reads it here: the appointments list, the Surgical Suite day
    list and the Command Center use it beside scored_admitted_patients.
    ORB_WARD_RECORD forces it off at boot (config.py)."""
    import config

    return bool(getattr(config, "SCORING_ENABLED", True)) and not bool(
        getattr(config, "WARD_RECORD", False)
    )


def ward_bed_order(patient: dict) -> tuple:
    """The board's sort key with scoring switched off: ward, then bed, then id.
    A patient with no ward or bed sorts after those that have one."""
    ward = patient.get("ward") or ""
    bed = patient.get("bed") or ""
    return (
        ward == "",
        _natural_key(ward),
        bed == "",
        _natural_key(bed),
        str(patient.get("id") or ""),
    )


def get_patient_risk(patient_id: str) -> dict | None:
    """Latest risk for a patient — NEWS2 baseline + CfC multi-horizon overlay.

    Phase 1 the displayed score/level is NEWS2 (rule-based, auditable). The CfC
    ensemble (deterioration / sepsis / mortality, each a multi-horizon
    trajectory) is attached as an overlay and may ESCALATE the level when it
    flags high near-term risk, but never replaces or lowers the NEWS2 baseline.
    Does NOT write to risk_scores (that's done by the bot).
    """
    with db_session() as db:
        row = db.execute(
            """SELECT * FROM vitals WHERE patient_id = ? AND voided_at IS NULL
               -- id DESC breaks a timestamp tie. Two readings CAN share an
               -- observation time (a corrected obs set re-charted at the same
               -- bedside time; two monitor messages in the same second), and
               -- without this the "latest" row was whichever SQLite happened to
               -- return — observed 2026-08-06 returning the SUPERSEDED benign
               -- set, so a board read 'stable' over a charted peri-arrest one.
               ORDER BY timestamp DESC, id DESC LIMIT 1""",
            (patient_id,),
        ).fetchone()
        # An earlier stay's last set is not this stay's latest (ADT-S9 (a)).
        if row is not None and from_previous_stay(
            row["timestamp"], current_stay_start_on(db, patient_id)
        ):
            row = None
        prow = db.execute("SELECT on_scale_2 FROM patients WHERE id = ?", (patient_id,)).fetchone()

    on_scale_2 = bool(prow["on_scale_2"]) if prow and prow["on_scale_2"] is not None else False

    # NEWS2 baseline + CfC overlay, through the SHARED entry point the nurse
    # board also uses. Nothing patient-facing may re-derive either half here.
    return risk_for_row(row, patient_id=patient_id, on_scale_2=on_scale_2)


def is_latest_vitals_row(db, patient_id: str, vitals_id: int) -> bool:
    """True when `vitals_id` is the row get_patient_risk above would score now.

    Same ORDER BY (timestamp first, id as the tiebreak), so a writer that pushes
    `vitals:update` only when this is True pushes exactly what a chart reload
    would show. A back-charted set (older than the newest) is False and stays
    history; a correction re-charted at the same bedside time wins the id
    tiebreak and is True. Call it on the writer's own connection, inside its
    transaction, where its INSERT is visible.
    """
    row = db.execute(
        "SELECT id, timestamp FROM vitals WHERE patient_id = ? AND voided_at IS NULL "
        "ORDER BY timestamp DESC, id DESC LIMIT 1",
        (patient_id,),
    ).fetchone()
    if row is None or row["id"] != vitals_id:
        return False
    # A set back-charted into a readmitted patient's earlier stay is never the
    # latest, even with nothing charted in this stay yet (ADT-S9 (a)).
    return not from_previous_stay(row["timestamp"], current_stay_start_on(db, patient_id))


def get_vitals_trend(patient_id: str, hours: int = 6) -> dict:
    """Compare recent vitals against the previous period for trend detection.

    A TREND NEEDS TWO WINDOWS. Fixed 2026-08-06, found by attack: a patient
    admitted this shift with ONE observation set — HR 132, SBP 88, SpO2 89,
    NEWS2 14 'critical' — was handed over on the I-PASS document as
    ``improving (HR +132.0, SBP +88.0, SpO2 +89.0)``.

    ``AVG()`` over zero rows returns a ROW whose columns are all NULL, not no row
    at all. ``previous`` was therefore truthy, only ``recent["avg_hr"]`` was
    null-checked, and ``(previous["avg_hr"] or 0)`` turned every absolute value
    into a delta from zero. SpO2 89 read as "+89, improving". That is a handover
    document lying in the dangerous direction, on exactly the patients who have
    only just arrived — the ones an oncoming nurse knows least about.

    With no comparison window the honest answer is ``insufficient_data``, which
    every caller already handles (it was the documented return for "no recent
    vitals" and ipass_service renders it as such).
    """
    from datetime import datetime, timedelta

    now = datetime.now(UTC)
    recent_cutoff = (now - timedelta(hours=hours)).isoformat()
    prev_cutoff = (now - timedelta(hours=hours * 2)).isoformat()

    with db_session() as db:
        # A readmitted patient's trend never reaches back into the earlier
        # stay (ADT-S9 (a)): both windows start no earlier than this stay.
        # Unchanged for a patient with one stay.
        stay_start = current_stay_start_on(db, patient_id)
        if stay_start is not None:
            from services.clinical_time import format_utc

            # A microsecond before the start, so a set taken at the very
            # start of the stay is in the recent window ('>').
            floor = stay_start - timedelta(microseconds=1)
            recent_cutoff = format_utc(max(now - timedelta(hours=hours), floor))
            prev_cutoff = format_utc(max(now - timedelta(hours=hours * 2), floor))
        recent = db.execute(
            """SELECT AVG(heart_rate) as avg_hr, AVG(systolic_bp) as avg_sbp,
                      AVG(spo2) as avg_spo2, AVG(respiratory_rate) as avg_rr
               FROM vitals WHERE patient_id = ? AND voided_at IS NULL
               AND timestamp > ?""",
            (patient_id, recent_cutoff),
        ).fetchone()

        previous = db.execute(
            """SELECT AVG(heart_rate) as avg_hr, AVG(systolic_bp) as avg_sbp,
                      AVG(spo2) as avg_spo2, AVG(respiratory_rate) as avg_rr
               FROM vitals WHERE patient_id = ? AND voided_at IS NULL
               AND timestamp BETWEEN ? AND ?""",
            (patient_id, prev_cutoff, recent_cutoff),
        ).fetchone()

    if not recent or recent["avg_hr"] is None:
        return {"trend": "insufficient_data", "reason": "no vitals in the recent window"}
    # BOTH windows must be populated. An empty previous window is not a baseline
    # of zero — it is no baseline.
    if not previous or previous["avg_hr"] is None:
        return {"trend": "insufficient_data", "reason": "no earlier vitals to compare against"}

    # Determine trend based on key indicators. Each delta needs BOTH sides
    # present; a vital measured in only one window contributes nothing rather
    # than a delta computed against zero.
    def _delta(key):
        a, b = recent[key], previous[key]
        return None if a is None or b is None else a - b

    hr_delta = _delta("avg_hr")
    sbp_delta = _delta("avg_sbp")
    spo2_delta = _delta("avg_spo2")

    # Worsening: HR up, BP down, SpO2 down
    concern_score = 0
    if hr_delta is not None and hr_delta > 10:
        concern_score += 1
    if sbp_delta is not None and sbp_delta < -10:
        concern_score += 1
    if spo2_delta is not None and spo2_delta < -2:
        concern_score += 1

    improving = (spo2_delta is not None and spo2_delta > 1) or (
        sbp_delta is not None and sbp_delta > 5
    )

    if concern_score >= 2:
        trend = "deteriorating"
    elif concern_score == 0 and improving:
        # Checked BEFORE the "stable" default: a genuine recovery (SpO2 88 → 97)
        # used to be swallowed by `abs(hr_delta) < 5` and reported as stable.
        trend = "improving"
    elif concern_score == 0 and hr_delta is not None and abs(hr_delta) < 5:
        trend = "stable"
    else:
        trend = "improving" if improving else "stable"

    out: dict[str, Any] = {"trend": trend}
    for key, val in (("hr_delta", hr_delta), ("sbp_delta", sbp_delta), ("spo2_delta", spo2_delta)):
        out[key] = None if val is None else round(val, 1)
    return out


# ═══════════════════════════════════════════════════════════════════════════
#  REMOVED 2026-08-15: `generate_patient_analysis`
#
#  The generator behind `GET /patients/{id}/analysis`, deleted with that route
#  (see the block in routes/vigil_routes.py, which deferred this half). Left
#  unreferenced it was a loaded gun, not dead weight: it took PRIORITY_SCRIBE —
#  the SAME one-slot background lane at the SAME priority as Sage's alert
#  advice — and then streamed with no generation budget at all, bounded only by
#  its own httpx `read` timeout of 300 s. Measured on the running ward
#  2026-08-14/15: three holds of exactly 300.012 s, each ending in the empty
#  `[Vigil] Stream error:` that an httpx ReadTimeout prints, and during every
#  one of them 97.9 % of Tier-2 drafts resolved to `queue_timeout` — including
#  drafts that arrived to an EMPTY queue. That is why the advice tier read 22 %
#  overall and 0 % across a quiet five-minute control: a five-minute hold
#  covers a five-minute window exactly, and no amount of debouncing the
#  DEMAND fixes a slot nobody can enter.
#
#  Removing the last caller of that pattern is the point. The general guard now
#  lives in two places — `llm_providers._stream_read_timeout` (a stall bound
#  sized on measured prefill, not five minutes) and the lane-hold watchdog in
#  llm_queue_service — so a future caller that reintroduces it is audible.
#
#  `tests/test_vigil_analysis_endpoint_removed.py` pins the route's absence.
# ═══════════════════════════════════════════════════════════════════════════


# Circuit breaker for the recommendation LLM. Simulation mode fires this on
# every vitals tick; when Ollama is absent entirely (CI, cold deploys) each call
# burned a connect-timeout and an ERROR log line — enough to fill a CI runner's
# disk. After _LLM_BREAKER_THRESHOLD consecutive *connection* failures we skip
# straight to the deterministic fallback for _LLM_BREAKER_COOLDOWN_S seconds,
# then probe again. HTTP errors from a live Ollama never trip it.
_LLM_BREAKER = {"failures": 0, "open_until": 0.0}
_LLM_BREAKER_THRESHOLD = 3
_LLM_BREAKER_COOLDOWN_S = 120.0

_FALLBACK_RECOMMENDATION = "Ensure continuous monitoring and prepare for possible intervention."


async def generate_ai_recommendation(vitals_summary: str, risk_level: str, drivers: list) -> str:
    """Generate a single concise sentence recommendation based on vital sign deviations using MedGemma."""
    prompt = f"Vitals: {vitals_summary}\nRisk Level: {risk_level}\nKey Drivers: {', '.join(drivers)}\n\nProvide a single concise sentence recommending the next clinical step or monitoring action."

    from config import AMBIENT_LLM_MODEL
    from services import ollama_gateway
    from services.llm_queue_service import llm_queue

    if time.monotonic() < _LLM_BREAKER["open_until"]:
        return _FALLBACK_RECOMMENDATION

    # Models switched off (ORB_MODELS_ENABLED, or the Orb Ward Record): the
    # static sentence, without taking a queue slot or asking the gateway, which
    # would only refuse. The critical alert still goes out with scoring on.
    from services.model_switch import models_enabled

    if not models_enabled():
        return _FALLBACK_RECOMMENDATION

    # AMBIENT TIER (verified 2026-07-31, unchanged): this per-alert garnish
    # already targets AMBIENT_LLM_MODEL directly, which is exactly the pattern
    # the other background jobs were moved to. Note num_ctx below is 4096 —
    # matching the context the ambient runner is loaded at, so this call cannot
    # make Ollama spawn a second 4B runner. Keep new ambient callers aligned.
    #
    # If the operator evicted the ambient tier to reclaim memory, calling
    # Ollama here would silently RELOAD it on the next vitals cycle — the
    # header's unload button appeared not to work. The static sentence is this
    # feature's documented degraded mode; use it while the tier is out.
    from services.model_router import ambient_tier_evicted

    if await ambient_tier_evicted(AMBIENT_LLM_MODEL):
        return _FALLBACK_RECOMMENDATION

    # This one-sentence garnish is called PER critical patient from inside the
    # vitals-simulator cycle. With the default 120s queue timeout on the shared
    # 27B, ten critical patients serialized the whole vitals pipeline (and its
    # DB writes) for tens of minutes under LLM contention — vitals froze in the
    # UI and every other writer starved. Alert dispatch must never wait long on
    # a garnish: wait at most a few seconds for the AMBIENT (4B) tier, else
    # fall back to the static sentence. A timeout must also not raise — that
    # previously killed the whole dispatch cycle.
    try:
        ticket = await llm_queue.acquire(priority=3, timeout=5.0, model=AMBIENT_LLM_MODEL)
    except asyncio.CancelledError:
        # A timeout must not raise (that once killed the dispatch cycle), but a
        # CANCEL must. This function hangs off the vitals tick, so swallowing the
        # shutdown cancel here made the simulator loop uncancellable and hung the
        # whole process at "Waiting for application shutdown." (observed
        # 2026-08-14). Degrading to a static sentence is right for a busy GPU and
        # wrong for a process that has been told to stop.
        raise
    except TimeoutError:
        return _FALLBACK_RECOMMENDATION
    try:
        async with httpx.AsyncClient(verify=local_model_ssl_context(), timeout=30.0, trust_env=False) as client:
            # Through the gateway (one audit row, prompt hashed) and yielding:
            # this garnish must not share the GPU with a clinician's turn, and
            # a yield is a TimeoutError that lands on the static sentence below.
            resp = await ollama_gateway.post(
                client,
                "/api/chat",
                {
                    "model": AMBIENT_LLM_MODEL,
                    "messages": [
                        {
                            "role": "system",
                            "content": "You are a clinical AI assistant. Provide exactly one short, actionable sentence. No preamble.",
                        },
                        {"role": "user", "content": prompt},
                    ],
                    "stream": False,
                    "options": {
                        "temperature": 0.1,
                        "num_predict": 64,
                        # Without an explicit ctx Ollama loads the model at its
                        # DEFAULT context (128K for medgemma) — a ~7 GB KV cache
                        # for a one-sentence garnish. 4K is ample.
                        "num_ctx": 4096,
                    },
                },
                purpose="vigil_recommendation",
                yield_to_clinician=True,
            )
            resp.raise_for_status()
            data = resp.json()
            _LLM_BREAKER["failures"] = 0
            return (
                data.get("message", {})
                .get("content", "Continue monitoring vitals closely.")
                .strip()
            )
    except (httpx.ConnectError, httpx.ConnectTimeout) as e:
        _LLM_BREAKER["failures"] += 1
        if _LLM_BREAKER["failures"] >= _LLM_BREAKER_THRESHOLD:
            _LLM_BREAKER["open_until"] = time.monotonic() + _LLM_BREAKER_COOLDOWN_S
            _LLM_BREAKER["failures"] = 0
            logger.warning(
                "[Vigil] LLM unreachable %d times (%s) — pausing recommendation calls for %.0fs",
                _LLM_BREAKER_THRESHOLD,
                e,
                _LLM_BREAKER_COOLDOWN_S,
            )
        else:
            logger.error("[Vigil] Error generating recommendation: %s", e)
        return _FALLBACK_RECOMMENDATION
    except ollama_gateway.LaneYielded as e:
        # Not an error: the GPU went to a clinician, as designed.
        logger.info("[Vigil] Recommendation yielded the lane (%s), static sentence used", e)
        return _FALLBACK_RECOMMENDATION
    except Exception as e:
        logger.error("[Vigil] Error generating recommendation: %s", e)
        return _FALLBACK_RECOMMENDATION
    finally:
        llm_queue.release(ticket)
