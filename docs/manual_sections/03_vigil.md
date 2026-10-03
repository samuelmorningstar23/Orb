## 3. Vigil: Deterioration Early-Warning

Vigil is Orb's patient-safety subsystem. Its job is to watch incoming vital signs and tell clinicians which patients are at risk of getting worse, ideally hours before the deterioration becomes obvious to the eye. It does this in two layers:

1. **NEWS2 baseline**, a rule-based score computed from the latest set of vital signs. Fast, transparent, reproducible by hand. This is the *displayed* risk score today.
2. **CfC overlay**, a family of neural networks (one per clinical outcome) that look at the *trend* across the last 24 observations and predict risk at multiple future time horizons. The overlay can escalate the NEWS2 level, but never lower it.

The rest of this section explains every file involved.

---

### Key concepts: read this before the code

**Vital signs** are the basic measurements taken routinely at the bedside:

| Abbreviation | Full name | Typical adult range |
|---|---|---|
| HR | Heart rate (pulse) | 50–90 bpm |
| SBP / DBP | Systolic / diastolic blood pressure | 110–219 / 60–80 mmHg |
| MAP | Mean arterial pressure = DBP + (SBP − DBP) / 3 | ~70–100 mmHg |
| RR | Respiratory rate (breaths per minute) | 12–20 /min |
| Temp | Body temperature | 36.1–38.0 °C |
| SpO2 | Peripheral oxygen saturation (pulse oximetry) | ≥ 96 % |
| AVPU / ACVPU | Consciousness scale: Alert / (new) Confusion / Voice / Pain / Unresponsive | Alert is normal |

**NEWS2** (National Early Warning Score 2) is the UK Royal College of Physicians standard for flagging patients who may be deteriorating. You take six vital-sign measurements, look each one up in a table, add the points, and the total tells you how urgently the patient needs attention. A single reading that scores 3 points alone is also an immediate escalation trigger (the "single-parameter rule"), regardless of the total.

**MEWS** (Modified Early Warning Score) is an older, similar scoring system. Orb used it previously; `compute_mews` is now just an alias that calls `compute_news2`.

**Sepsis** is a life-threatening systemic infection response. Recognizing it early (before organ failure sets in) dramatically improves survival.

**Mortality risk**, probability of the patient dying during their hospital stay.

**CfC (Closed-Form Continuous-time network)**, also called a "liquid neural network", is a type of recurrent neural network that explicitly accounts for the time gap (Δt) between successive observations. In ICU data, vitals are recorded at irregular intervals (every 15 minutes during a crisis, every few hours when stable), and standard recurrent networks (LSTMs etc.) treat all steps as equally spaced. The CfC avoids that error.

**Multi-horizon prediction** means the model outputs a risk score for each of several future windows simultaneously: "What is the probability this patient deteriorates within 1 hour? Within 3 hours? 6 hours? 12 hours? 24 hours?" This gives clinicians a trajectory rather than a single number.

**NEWS2-as-baseline with CfC overlay** means: NEWS2 defines the floor. The CfC can push the alert level upward (stable → elevated, elevated → critical) if its learned pattern-matching detects a risk that the rule-based score misses. It never pushes the level down.

---

### 3.1 `server/models/vigil_cfc.py`: The neural network architecture

**Role:** Defines the PyTorch classes for the CfC model. Everything else (the service, the trainer, the routes) imports from here. This file has no side effects when imported, it only defines classes and constants.

**Libraries:**
- `torch`, `torch.nn`, `torch.nn.functional`, PyTorch's deep-learning framework. `nn.Module` is the base class for all network layers. `F.softplus` is a smooth version of ReLU that guarantees strictly positive output (needed for the decay rate).

#### Constants

```python
HORIZONS_HOURS = (1, 3, 6, 12, 24)
VIGIL_OUTCOMES = ("deterioration", "sepsis", "mortality")
```

`HORIZONS_HOURS` defines the five future time windows the model predicts. The order matters, index 0 is always 1h, index 4 is always 24h. **If you change this tuple you must retrain all checkpoints and update `vigil_service.py`'s `_ANALYSIS_CACHE_TTL` comment references.**

`VIGIL_OUTCOMES` names the three independent models. Each is a separate `.pt` checkpoint file.

#### `PRODUCTION_FEATURE_ORDER`, `PRODUCTION_FEATURE_MEAN`, `PRODUCTION_FEATURE_STD`

```python
PRODUCTION_FEATURE_ORDER = (
    "heart_rate", "spo2", "temperature", "systolic_bp",
    "map", "diastolic_bp", "respiratory_rate", "avpu", "age",
)
PRODUCTION_FEATURE_MEAN = (83.0, 96.5, 36.9, 122.0, 82.0, 63.0, 19.0, 0.2, 62.0)
PRODUCTION_FEATURE_STD  = (18.0, 3.0,  0.7,  23.0,  17.0, 13.0,  5.0,  0.6, 18.0)
```

This is the 9-feature vector the model expects at every time step, in exactly this order. The mean and standard deviation values are used to normalize the raw measurements to approximately zero-mean, unit-variance, a standard preprocessing step that keeps neural-network training stable. These are *placeholder estimates* based on typical population values; once real data is used for training, the trainer computes corpus-specific values and saves them inside the checkpoint (see `train_vigil_cfc.py`). The checkpoint's saved values override these constants at inference time.

⚠️ **If you change `PRODUCTION_FEATURE_ORDER`** you must simultaneously update `_CFC_FEATURE_ORDER` in `vigil_service.py` (they must be identical), retrain all three models, and redeploy all checkpoints. The service guards against a schema mismatch by comparing `meta["input_dim"]` against `len(_CFC_FEATURE_ORDER)`, but the *order* within those 9 features is not validated, a silent reorder will produce garbage risk scores with no error.

#### `class CfCCell(nn.Module)`: one recurrent step

This is the mathematical core. It takes the current vital-sign vector `x`, the previous hidden state `h`, and the time elapsed since the last observation `dt`, and returns an updated hidden state.

**What it contains:**
- `self.Wx`, a linear projection (`nn.Linear(input_dim, hidden_dim)`) that maps the 9-feature input into the 64-dimensional hidden space. Think of it as "what does this vital-sign snapshot look like in the model's internal vocabulary?"
- `self.f`, a two-layer neural network (Linear → Tanh → Linear) that takes the concatenation of `x` and `h` and outputs a *decay rate* for each hidden dimension. After `F.softplus`, this is strictly positive, ensuring the exponential decay is always between 0 and 1.
- `self.g`, a parallel two-layer network that outputs the *steady-state target*, the hidden state the system would converge to if vital signs stayed constant forever.

**The forward pass** (`forward(self, x, h, dt)`):

```
combined = [x, h] concatenated
decay_rate = softplus(f(combined))      # strictly positive
steady     = g(combined)
decay      = exp(-decay_rate * dt)      # 0..1, small dt → close to 1, large dt → close to 0
h_new      = Wx(x) * decay + steady
```

In plain English: the new hidden state is a blend of "what the current vitals look like" (scaled down by how much time has passed) and "where the system would settle." If `dt` is large (the last reading was six hours ago), `decay` is small and the fresh reading `Wx(x)` dominates. If `dt` is tiny (the nurse just charted 2 minutes ago), `decay` is close to 1 and the model keeps more memory of the trajectory.

**Inputs:** `x` shape `(B, input_dim)`, `h` shape `(B, hidden_dim)`, `dt` shape `(B, 1)` where B is batch size.
**Output:** new `h` shape `(B, hidden_dim)`.

#### `class OrbVigilCfC(nn.Module)`: the full model for one outcome

This wraps `CfCCell` into a complete model that handles a sequence of observations and produces multi-horizon risk scores.

**Constructor parameters:**
- `input_dim`, must equal `len(PRODUCTION_FEATURE_ORDER)` = 9 for live use.
- `hidden_dim`, default 64. Stored in the checkpoint; the service reads it back.
- `horizons`, default `HORIZONS_HOURS`. Stored in the checkpoint.
- `outcome`, string label ("deterioration", "sepsis", "mortality"). Cosmetic but saved in the checkpoint for bookkeeping.

**What it contains:**
- `self.cell`, one `CfCCell` instance (shared across all time steps).
- `self.readout`, a small two-layer network (Linear → ReLU → Dropout(0.2) → Linear) that maps the final hidden state to `num_horizons` raw logit scores, one per prediction horizon.

**`forward(self, vitals, dt, return_logits=False)`**

- Inputs: `vitals` shape `(B, T, input_dim)`, a batch of B patients, each with T sequential observations. `dt` shape `(B, T, 1)`, the time gap before each observation in hours.
- Squeezes 2D inputs to 3D if a single sequence is passed without a batch dimension.
- Initializes hidden state `h` as zeros.
- Loops over each time step `t` from 0 to T-1, calling `self.cell(vitals[: t:], h, dt[: t:])` and replacing `h` with the result.
- Feeds the *final* `h` (representing everything the model has learned from the patient's trajectory) into `self.readout`.
- Returns either raw logits (for training, to feed into `BCEWithLogitsLoss` directly) or sigmoid-transformed probabilities in [0, 1] (for inference).

⚠️ **`return_logits=True` must be used during training.** Reconstructing logits from clamped sigmoids (via `log(p/(1-p))`) is numerically unstable and saturates gradients for confident predictions. The trainer explicitly passes this flag.

**`predict_trajectory(self, vitals, dt)`**

A convenience wrapper for single-sample inference. Calls `forward` with `torch.no_grad()` (no gradient tracking, faster and safer at inference), takes the first (and only) element of the batch dimension, and returns a dict:

```python
{
    "outcome": "deterioration",   # or "sepsis" / "mortality"
    "risk_by_horizon": {          # per-horizon sigmoid risk, 0.0 to 1.0
        "1h": 0.0312,
        "3h": 0.0891,
        "6h": 0.1430,
        "12h": 0.2100,
        "24h": 0.3000,
    },
    "peak_risk": 0.3000,          # max across all horizons
}
```

---

### 3.2 `server/services/vigil_service.py`: Risk scoring and patient analysis

**Role:** The business logic layer. It owns the NEWS2 implementation, the CfC model-loading and inference orchestration, and the LLM-powered patient analysis stream. Nothing writes to the database here (risk scores are written by a background bot; this service is read-only except for the `clinical_documents` inserts routed through `vigil_routes.py`).

**Libraries:**
- `torch`, loaded lazily inside `_load_cfc_models()` so the service starts even without PyTorch installed (NEWS2 still works).
- `httpx`, async HTTP client for streaming to the Ollama LLM.
- `asyncio`, for the analysis cache lock (deduplicating concurrent requests for the same patient).
- `pathlib.Path`, to locate the `server/models/` directory relative to this file.
- `config`, `OLLAMA_BASE_URL`, `LLM_MODEL`, `OLLAMA_KEEP_ALIVE`.
- `database`, `get_db()` (raw connection, caller closes) and `db_session()` (context-manager, auto-closes).

#### Module-level state

```python
_analysis_cache: dict[str, tuple[float, str]] = {}   # patient_id -> (unix_ts, text)
_analysis_locks: dict[str, asyncio.Lock]             # patient_id -> lock
_ANALYSIS_CACHE_TTL = 300   # seconds (5 minutes)

_cfc_models: dict   # outcome -> loaded OrbVigilCfC
_cfc_meta:   dict   # outcome -> {input_dim, feature_order, mean, std, horizons}
_cfc_device         # torch.device ("mps" or "cpu")
_cfc_loaded         # bool flag, prevents re-scanning the models/ directory
```

The analysis cache prevents duplicate LLM calls when a clinician has the same patient open in two browser tabs. The `_analysis_locks` dict prevents a race where two simultaneous first-opens both start generating; the second waits behind a lock and then re-uses the cache result.

#### CfC feature schema (mirrors `vigil_cfc.py`)

```python
_CFC_FEATURE_ORDER = ["heart_rate", "spo2", "temperature", "systolic_bp",
                      "map", "diastolic_bp", "respiratory_rate", "avpu", "age"]
_CFC_FEATURE_MEAN  = [83.0, 96.5, 36.9, 122.0, 82.0, 63.0, 19.0, 0.2, 62.0]
_CFC_FEATURE_STD   = [18.0, 3.0,  0.7,  23.0,  17.0, 13.0, 5.0,  0.6, 18.0]
```

These are the *fallback* normalization constants used only if no sidecar values are found in the checkpoint. In practice a trained checkpoint always carries the real corpus stats, so these are rarely used for inference.

```python
_AVPU_MAP = {
    "alert": 0.0, "a": 0.0,
    "verbal": 1.0, "v": 1.0,
    "pain": 2.0,  "p": 2.0,
    "confusion": 3.0, "c": 3.0, "new_confusion": 3.0,
    "unresponsive": 3.0, "u": 3.0,
}
```

Consciousness is stored as a string in the `vitals` table. Before feeding it to the model it is mapped to a graded float: Alert = 0, Voice = 1, Pain = 2, any impairment including new confusion = 3. Note that this differs slightly from the NEWS2 ACVPU scoring, where any non-Alert level scores 3 as a binary flag (not a gradient).

#### `_load_cfc_models() -> bool`

**Purpose:** Lazy-load whichever per-outcome checkpoint files exist in `server/models/`. Idempotent, returns immediately if already called (using `_cfc_loaded` flag).

**Logic:**
1. Tries to import `torch` and `OrbVigilCfC`. If either fails (e.g., torch not installed), logs a message and returns `False`. NEWS2 still works.
2. Sets `_cfc_device` to Apple MPS (GPU) if available on the host Mac, otherwise CPU.
3. For each outcome in `_CFC_CHECKPOINTS`, finds the first matching filename that actually exists on disk. The deterioration outcome has a fallback filename (`vigil_cfc_checkpoint.pt`) for backward compatibility.
4. Loads each checkpoint with `torch.load(..., weights_only=False)`. ⚠️ This deserializes arbitrary Python pickle, a tampered checkpoint is remote code execution. A comment in the code marks this as a deferred security fix (switch to `weights_only=True` + JSON sidecar for metadata).
5. Reads `input_dim`, `hidden_dim`, and `horizons` from the checkpoint dict (falling back to defaults if not found).
6. Constructs an `OrbVigilCfC` instance, loads the weights, moves to the target device, sets to `eval()` mode.
7. Stores the model in `_cfc_models[outcome]` and its metadata in `_cfc_meta[outcome]`.

If no checkpoints exist, logs a message and returns `False` (NEWS2-only mode).

#### `_assemble_feature_sequence(patient_id, feature_order, mean, std)`

**Purpose:** Build the `(1, T, 9)` vitals tensor and `(1, T, 1)` delta-time tensor the CfC model needs, from the patient's raw database rows.

**Inputs:** patient ID string; the feature order, mean, and std taken from `_cfc_meta[outcome]` (i.e., matched to the checkpoint that is about to run).

**Steps:**
1. Queries the `vitals` table for the most recent 24 rows for this patient, ordered oldest-first after reversal. Also reads `age` from the `patients` table.
2. Returns `(None, None)` if fewer than 3 rows exist, the model needs at least a minimal history.
3. For each row: reads the six raw vital columns (`heart_rate`, `spo2`, `temperature`, `systolic_bp`, `diastolic_bp`, `respiratory_rate`), substituting the default value from `_CFC_VITALS_DEFAULTS` for any NULL. Derives MAP as `diastolic_bp + (systolic_bp - diastolic_bp) / 3.0`. Looks up the `consciousness` string in `_AVPU_MAP`. Appends `age`.
4. Z-score normalizes each value: `(raw - mean[j]) / std[j]`.
5. Computes `dt` for each row: the elapsed hours since the previous row's timestamp. The first row gets `dt = 0.0`. If timestamp parsing fails, `dt = 1.0` is assumed.
6. Returns PyTorch tensors on `_cfc_device`.

⚠️ **MAP derivation mismatch (known, deferred):** PhysioNet provides MAP directly; the production service derives it. If you train on PhysioNet's MAP column and serve with the derived formula, the MAP channel will have a systematic offset. The code comments flag this as a `train/serve skew` TODO. Until resolved, MAP predictions are slightly biased.

#### `compute_cfc_risk(patient_id: str) -> dict | None`

**Purpose:** Run all loaded CfC models for a patient and return their multi-horizon predictions.

**Logic:**
1. Calls `_load_cfc_models()`. Returns `None` if no models are available.
2. For each outcome model, checks `meta["input_dim"] == len(_CFC_FEATURE_ORDER)` (currently 9). Models trained on a richer schema (e.g., with lab values, `input_dim = 15`) are skipped, they cannot run on the live 9-feature vitals without a matching feature-assembly path.
3. Calls `_assemble_feature_sequence` for this outcome's `feature_order`/`mean`/`std`. If the patient has insufficient data, logs a debug message and *continues to the next outcome* (does not abort the whole call, a patient might have enough data for deterioration but not sepsis).
4. Runs `model.predict_trajectory(vitals_t, dt_t)` inside `torch.no_grad()` and a `vram_guard` context (from `services/gpu_coordinator.py`, prevents memory overrun on the 8 GB Mini).
5. After all outcomes run, selects the "primary" risk: the deterioration model's `peak_risk` if that model ran, otherwise the maximum `peak_risk` across all outcomes.
6. Returns:
```python
{
    "outcomes": {
        "deterioration": {"risk_by_horizon": {...}, "peak_risk": 0.12},
        "sepsis":        {"risk_by_horizon": {...}, "peak_risk": 0.07},
    },
    "primary_risk": 0.12,
    "seq_len": 8,
    "model": "CfC-LNN"
}
```
Returns `None` if no outcome produced a result.

#### `_NEWS2_MAX_AGGREGATE = 20.0`

The theoretical maximum raw NEWS2 score (RR:3 + SpO2:3 + oxygen:2 + SBP:3 + HR:3 + Temp:2 + ACVPU:3 = 19, rounded up to 20 for headroom). Used as the denominator when normalizing the raw integer score to a 0–1 index. The old code divided by 10, which meant any score ≥ 10 showed as 1.0, making a score of 10 and a score of 19 look identical. The `/20` divisor preserves discrimination across the full severity range.

#### `compute_news2(hr, sbp, rr, temp, spo2, consciousness="alert", on_oxygen=False) -> dict`

This is the core clinical algorithm. It accepts the six vital sign measurements plus consciousness and on-oxygen flag, applies the NEWS2 scoring table, and returns a structured result.

**NEWS2 scoring table (Scale 1, standard oxygen target 94–98%)**

Each parameter contributes 0, 1, 2, or 3 points:

**Respiratory rate (RR, breaths/min):**
| Range | Points | Meaning |
|---|---|---|
| ≤ 8 | 3 | Dangerously slow (bradypnea) |
| 9–11 | 1 | Slow |
| 12–20 | 0 | Normal |
| 21–24 | 2 | Fast |
| ≥ 25 | 3 | Dangerously fast |

**SpO2 (%, Scale 1):**
| Range | Points |
|---|---|
| ≥ 96 | 0 |
| 94–95 | 1 |
| 92–93 | 2 |
| ≤ 91 | 3 |

**Supplemental oxygen:**
| Situation | Points |
|---|---|
| On oxygen | +2 |
| Not on oxygen | 0 |

**Systolic blood pressure (SBP, mmHg):**
| Range | Points |
|---|---|
| ≤ 90 | 3 |
| 91–100 | 2 |
| 101–110 | 1 |
| 111–219 | 0 |
| ≥ 220 | 3 |

**Heart rate (HR, bpm):**
| Range | Points |
|---|---|
| ≤ 40 | 3 |
| 41–50 | 1 |
| 51–90 | 0 |
| 91–110 | 1 |
| 111–130 | 2 |
| ≥ 131 | 3 |

**Temperature (°C):**
| Range | Points |
|---|---|
| ≤ 35.0 | 3 |
| 35.1–36.0 | 1 |
| 36.1–38.0 | 0 |
| 38.1–39.0 | 1 |
| ≥ 39.1 | 2 |

**Consciousness (ACVPU):**
| State | Points |
|---|---|
| Alert ("a", "alert", or blank) | 0 |
| Any other state (C, V, P, U) | 3 |

**Worked example, a patient arriving to A&E:**
- HR = 118 → 2 points (tachycardia)
- SpO2 = 93% → 2 points (hypoxia)
- On oxygen → +2 points
- SBP = 96 → 2 points (hypotension)
- RR = 22 → 2 points (tachypnea)
- Temp = 38.5°C → 1 point (fever)
- Consciousness = "alert" → 0 points
- **Raw NEWS2 total: 11**
- Normalized score: 11 / 20 = 0.55
- Level: critical (≥ 7)
- Drivers: ["Tachycardia (HR 118)", "Hypoxia (SpO2 93%)", "On supplemental oxygen"]

**Clinical bands (from raw score + single-parameter rule):**

| Condition | Level |
|---|---|
| Raw score ≥ 7 | critical |
| Raw score 5–6, OR any single parameter = 3 | elevated |
| Raw score 0–4 with no parameter = 3 | stable |

The `any_param_3` boolean is set inside `add()` whenever a parameter contributes 3 points. This implements the single-parameter rule: a patient with e.g. SBP ≤ 90 (3 points) but all other vitals normal (total score = 3) is still classified "elevated" rather than "stable."

**Scale 2 / COPD patients (implemented; cannot yet be prescribed):** COPD patients who chronically retain CO2 have a target SpO2 of 88–92%, not the standard 94–98%. On Scale 1 their normal looks like hypoxia and dangerous over-oxygenation on O2 scores nothing. `compute_news2(..., on_scale_2=True)` scores the RCP Scale 2 table, chosen per patient by `patients.on_scale_2`, and every scoring lane (chart, nurse entry, ingestion, simulator, admission baseline, Sage, downtime, escalation) passes it; `tests/test_news2_scale2_lanes.py` fails any call that omits it. The open gap is upstream: nothing in Orb can SET the flag yet (no route, no UI control, no audit event; gap register X003), so today every patient is scored on Scale 1.

**Return value:**
```python
{
    "score": 0.55,                   # normalized 0-1 index (NOT a probability)
    "level": "critical",             # "stable" / "elevated" / "critical"
    "raw_news2": 11,                 # the integer sum, use this for clinical decisions
    "raw_mews": 11,                  # legacy alias, same value
    "single_param_3": False,         # True if any parameter alone scored 3
    "drivers": [                     # up to 3 contributing factors
        "Tachycardia (HR 118)",
        "Hypoxia (SpO2 93%)",
        "On supplemental oxygen",
    ],
    "scoring_model": "NEWS2",
}
```

⚠️ The `score` field is a normalized index for display purposes, not a calibrated probability. Clinical escalation decisions should use `raw_news2` + `level`. The `generate_patient_analysis` function explicitly reports `raw_news2` rather than `score` to avoid implying a percentage probability.

#### `compute_mews(hr, sbp, rr, temp, spo2, consciousness="alert") -> dict`

A deprecated alias that calls `compute_news2` without the `on_oxygen` flag. Kept for backward compatibility with any callers that still use the MEWS name.

#### `get_patient_risk(patient_id: str) -> dict`

**Purpose:** The top-level risk function called by routes and the monitoring bot. Returns the combined NEWS2 + optional CfC result for a patient.

**Steps:**
1. Queries the `vitals` table for the single most recent row for this patient.
2. If no vitals exist, returns a safe default (score 0, level "stable", driver "No vitals recorded").
3. Reads `consciousness` (falls back to "alert") and `on_oxygen` (falls back to False) from the row.
4. Calls `compute_news2(...)` to get the NEWS2 baseline.
5. Calls `compute_cfc_risk(patient_id)`. If it returns data, merges it into the result:
   - Adds `"cfc"` (the full outcomes dict), `"cfc_primary_risk"`, `"cfc_seq_len"`, and updates `"scoring_model"` to `"NEWS2+CfC-LNN"`.
   - **CfC escalation logic** (overlay can only raise the level, never lower it):
     - `primary_risk ≥ 0.9` and current level is "stable" or "elevated" → escalate to "critical". Driver prepended: `"CfC: high near-term deterioration risk"`.
     - `primary_risk ≥ 0.85` and current level is "elevated" → escalate to "critical".
     - `primary_risk ≥ 0.7` and current level is "stable" → escalate to "elevated". Driver prepended: `"CfC: rising near-term deterioration risk"`.
     - In all other cases: the NEWS2 level stands.

⚠️ The thresholds 0.9 / 0.85 / 0.7 are hardcoded. A trained-model comment notes that these should be derived from a target-sensitivity operating point on held-out data rather than hand-tuned constants. Changing them changes escalation behavior visible in the VigilPanel frontend and in audit events tagged `news2_recomputed`.

#### `get_vitals_trend(patient_id: str, hours: int = 6) -> dict`

**Purpose:** Detect whether a patient's vitals are improving, stable, or deteriorating by comparing the average HR, SBP, and SpO2 in the last `hours` hours against the preceding `hours` period.

**Logic:**
- Queries two time windows: `[now-2h, now-h]` (previous) and `[now-h, now]` (recent).
- Computes deltas: `hr_delta`, `sbp_delta`, `spo2_delta`.
- Adds to a `concern_score`: HR rising > 10 (+1), SBP falling > 10 (+1), SpO2 falling > 2 (+1).
- `concern_score ≥ 2` → `"deteriorating"`. `concern_score == 0` and HR very stable → `"stable"`. SpO2 up or SBP up → `"improving"`. Otherwise `"stable"`.
- Returns `{"trend": "...", "hr_delta": ..., "sbp_delta": ..., "spo2_delta": ...}` or `{"trend": "insufficient_data"}`.

#### `generate_patient_analysis(patient_id: str)` (async generator)

**Purpose:** Stream a natural-language clinical summary of a patient's current state. Used by `GET /api/vigil/patients/{patient_id}/analysis`.

**Flow:**
1. Fetches patient demographics, the latest vitals row, the three most recent clinical documents, and active medications from the database.
2. Calls `get_patient_risk` and `get_vitals_trend` to get computed risk.
3. Builds a structured plain-text summary in Python (no LLM needed yet) covering: patient identity/admission context, vitals with interpretation, NEWS2 band, trend, medications, and recent note excerpts.
4. Constructs a prompt asking the LLM (MedGemma via Ollama) to write a 4-sentence clinical assessment in paragraph form.
5. Acquires a slot in the LLM queue (`llm_queue.acquire(priority=2)`) to prevent multiple heavy LLM calls competing.
6. Streams the response from Ollama's `/api/chat` endpoint, yielding Server-Sent Events (`data: {"content": "..."}`) as they arrive.
7. Yields a final `data: [DONE]` sentinel.
8. Releases the LLM queue slot in `finally`.

The function uses a low temperature (0.2) and a short context window (2048 tokens) and caps output at 512 tokens, these are conservative settings appropriate for a demo where inference hardware is limited.

#### `generate_ai_recommendation(vitals_summary, risk_level, drivers)` (async)

A simpler non-streaming LLM call that returns a single sentence recommending a clinical action. Uses `priority=3` (lower priority than the full analysis) and temperature 0.1 (very deterministic). Returns a safe fallback string if the LLM call fails.

---

### 3.3 `server/scripts/train_vigil_cfc.py`: Training pipeline

**Role:** A standalone command-line script that trains one `OrbVigilCfC` checkpoint from scratch. Run it directly:

```bash
cd server && .venv/bin/python scripts/train_vigil_cfc.py --outcome sepsis
```

**Libraries:**
- `torch`, `torch.nn`, PyTorch for training.
- `torch.utils.data.Dataset`, `DataLoader`, mini-batch iteration.
- `numpy`, for AUROC/AUPRC metric computation.
- `argparse`, parses `--outcome` flag.
- `pathlib.Path`, `sys`, resolves paths and patches `sys.path` so `from models.vigil_cfc import ...` works when the script is run from inside `server/`.

⚠️ **Training status:** As of the writing of this manual, NONE of the three models have been trained on real clinical data. The sepsis model *can* be trained on the publicly available PhysioNet/CinC 2019 dataset (open access, no credentialing required). The deterioration and mortality models require credentialed access to MIMIC-IV or eICU, which is the funded milestone. Until a checkpoint exists, `_load_cfc_models()` returns `False` and `compute_cfc_risk` returns `None`, the CfC overlay is entirely absent and the displayed risk is pure NEWS2.

#### Training hyperparameters (module-level constants)

```python
HIDDEN_DIM = 64            # CfCCell and readout width
MAX_SEQ_LEN = 48           # maximum observation window (hours)
MIN_OBS = 3                # discard patients with fewer than 3 observations
MAX_WINDOWS_PER_PATIENT = 6  # cap on training samples per patient (class balance)
BATCH_SIZE = 64
EPOCHS = 30
LR = 1e-3                  # AdamW learning rate
TRAIN_SPLIT = 0.85         # 85% train, 15% val
SEED = 42
```

#### `parse_psv(filepath: Path)`: PhysioNet file reader

Each PhysioNet/CinC 2019 patient is a pipe-delimited `.psv` file (one row per hourly observation, a `SepsisLabel` column that flips to 1 at sepsis onset). This function reads one such file and returns `(rows, onset_idx)`, a list of per-hour feature dicts and the index of the first row where SepsisLabel = 1 (or `None` if the patient never develops sepsis).

Missing values ("", "NaN") are returned as `None`. The AVPU field is imputed as 0.0 (alert) because PhysioNet has no consciousness column.

Returns `None` if the file is too short (< `MIN_OBS` rows) or malformed.

#### `make_windows(parsed)`: sample generation

Converts a parsed patient into a list of training samples. The framing is **early-warning** (pre-onset only): a window that ends *after* sepsis onset is useless as a warning signal, so windows are only drawn from `t ∈ [MIN_OBS, onset_idx)`. For non-sepsis patients, windows can be drawn from anywhere.

For each selected prediction point `t`:
- Feature matrix: the last `MAX_SEQ_LEN` rows before `t`, as a `(T, 9)` float tensor.
- Delta-time: `[[1.0]] * T`, PhysioNet is hourly, so all gaps are exactly 1 hour.
- Labels: a binary vector of length 5 (one per `HORIZONS_HOURS`). Label `h` is 1.0 if `0 ≤ onset_idx - t ≤ h`, i.e., if sepsis will occur within `h` hours of this window end.

Features at this stage are **not yet normalized**, normalization is applied corpus-wide later, after all samples are assembled.

⚠️ Up to `MAX_WINDOWS_PER_PATIENT = 6` windows are sampled randomly per patient to limit class imbalance (most patients never get sepsis).

#### `_compute_corpus_stats(samples)`: real normalization

After all samples are built, this function stacks every feature timestep from every sample into a single matrix and computes per-feature mean and standard deviation. This is the normalization that gets saved in the checkpoint and used at inference time. It replaces the hardcoded `PRODUCTION_FEATURE_MEAN/STD` placeholders in `vigil_cfc.py`, which are population-level estimates rather than dataset-specific values.

**Why this matters:** if the training data has a different mean or spread than the hardcoded constants (e.g., a PhysioNet ICU cohort skews sicker than the general adult population), normalizing inference inputs with the wrong mean/std will shift every feature and degrade predictions. Saving the correct stats inside the checkpoint is the fix.

#### `load_sepsis_dataset()`: dataset loader

Scans `server/datasets/physionet_2019/` for `.psv` files. Calls `parse_psv` + `make_windows` on each. Calls `_compute_corpus_stats`. Z-scores all samples: `normalized_feats = (feats - mean) / std`. Returns `(samples, feat_mean, feat_std)`.

#### `load_dataset_for(outcome)`: outcome registry

Routes to the correct dataset loader by outcome name. Currently only "sepsis" is implemented; "deterioration" and "mortality" raise `NotImplementedError` with a message directing the developer to build MIMIC-IV/eICU adapters once data access clears.

#### Validation metrics: `_auroc`, `_auprc`, `_val_metrics`

These implement the two standard clinical-model evaluation metrics without importing scikit-learn (keeping the dependency footprint small):

- **AUROC** (Area Under the Receiver Operating Characteristic Curve): probability that the model scores a randomly chosen positive patient higher than a randomly chosen negative patient. Computed via the Mann-Whitney U statistic. Random chance = 0.5; perfect = 1.0. Returns `nan` if only one class is represented.
- **AUPRC** (Area Under the Precision-Recall Curve): summary statistic for how well the model trades off finding true positives against raising false alarms. More informative than AUROC for imbalanced datasets (like sepsis, where most patients are negative). Returns `nan` if no positives.
- `_val_metrics` pools predictions across all 5 horizons and returns `(AUROC, AUPRC)`, replacing `nan` with 0.0 for display.

⚠️ The code notes that checkpointing on validation loss alone is insufficient, a clinical risk model should be gated on outperforming the NEWS2 baseline on AUROC/AUPRC. That gate is not yet implemented; any checkpoint with a lower val loss than the previous best is saved, regardless of discrimination.

#### `collate(batch)`: mini-batch padding

Patient sequences have varying lengths. This function zero-pads all feature sequences in a batch to the length of the longest sequence, and fills the corresponding delta-time positions with 1.0 (so padded positions don't disrupt the CfC's time-decay logic).

#### `train(outcome: str)`: main training loop

1. Loads and shuffles the dataset. Splits 85/15 into `DataLoader`s.
2. Selects MPS (Apple GPU) if available, otherwise CPU.
3. Builds `OrbVigilCfC` with `input_dim = 9`.
4. Computes `pos_weight` per horizon from the training label distribution: `(1 - pos_rate) / pos_rate`, clamped to max 20. This tells `BCEWithLogitsLoss` to treat each positive example as if it were `pos_weight` negatives, compensating for the class imbalance typical of early-warning datasets (e.g., 1 in 20 patients develops sepsis).
5. Uses `AdamW` optimizer with `weight_decay=1e-4` (mild L2 regularization) and `CosineAnnealingLR` learning-rate schedule (cosine annealing decays LR smoothly from `LR` to ~0 over `EPOCHS`).
6. Training passes use `return_logits=True` to feed raw logits into `BCEWithLogitsLoss`. Validation passes compute metrics using `torch.sigmoid(logits)`.
7. Clips gradients to norm 1.0 each step to prevent exploding-gradient instability.
8. Each epoch prints: epoch number, train loss, val loss, AUROC, AUPRC.
9. Saves the best checkpoint (by val loss) to `server/models/vigil_cfc_{outcome}.pt`. The checkpoint dict contains:
   - `model_state_dict`, the trained weights.
   - `outcome`, `input_dim`, `hidden_dim`, `horizons`, `feature_order`.
   - `feature_mean`, `feature_std`, the corpus-computed normalization stats.
   - `epoch`, `val_loss`, `val_auroc`, `val_auprc`, training provenance.
   - `data_source`, "physionet_cinc2019" for sepsis, "unknown" otherwise.

---

### 3.4 `server/routes/vigil_routes.py`: HTTP API

**Role:** Defines all FastAPI endpoints under the `/api/vigil` prefix. Authentication and permission checking are handled by `Depends(require_permission(...))` on every endpoint (except the test-stream stub). No risk computation or database logic lives here, this file delegates to `vigil_service.py` and other services.

**Libraries:**
- `fastapi.APIRouter`, `Depends`, `HTTPException`, FastAPI routing.
- `fastapi.responses.FileResponse`, `StreamingResponse`, `HTMLResponse`, response types.
- `auth.get_current_user`, JWT token validation.
- `permissions.require_permission`, RBAC guard returning the authenticated user dict.
- `database.get_db`, `db_session`, database access.
- `services.vigil_service.get_patient_risk`, `get_vitals_trend`, risk computation.

#### `GET /api/vigil/test-stream`

A development stub that streams a hardcoded sentence, word by word, with 15ms delays. Used to verify that the SSE streaming infrastructure is working without triggering a real LLM call. No authentication.

#### `GET /api/vigil/patients`

Permission: `patient.read_all`

Returns all currently admitted patients visible to the requesting user's department. If the user has a `department` set, filters patients to only those with an active admission in that department. Returns them ordered by `risk_score DESC` (highest risk first), which is the cached score from the `risk_scores` table (written by the background bot), not a live recomputation.

Each patient object returned:

```json
{
  "id": "PAT3A1B2C",
  "name": "Jane Smith",
  "mrn": "MRN-12345",
  "bed": "4B",
  "ward": "ICU",
  "department": "Critical Care",
  "admission_date": "2026-06-20T08:00:00",
  "diagnosis": "Pneumonia",
  "age": 72,
  "sex": "F",
  "risk_score": 0.45,
  "risk_level": "elevated",
  "risk_drivers": ["Tachycardia (HR 105)", "Fever (38.8°C)"],
  "attending_doctor_name": "Dr. Patel"
}
```

`risk_drivers` is stored as JSON in the database and deserialized here.

#### `GET /api/vigil/discharged`

Permission: `patient.read_all`

Returns all patients with `status = 'discharged'`, joined to their most recent `discharge_summaries` row. Ordered by discharge date, most recent first.

#### `GET /api/vigil/patients/{patient_id}`

Permission: `patient.read_all`

The richest endpoint, returns everything needed to render a full patient card:

| Field | Source |
|---|---|
| `patient` | `patients` table + attending doctor name |
| `risk` | `get_patient_risk()`, live NEWS2 + CfC computation |
| `trend` | `get_vitals_trend()`, HR/SBP/SpO2 trend over 6h |
| `vitals` | Last 288 rows (~24h at 5-min intervals) from `vitals` table |
| `notes` | `clinical_documents` table, all rows, newest first |
| `scribe_notes` | `scribe_notes` table, newest first |
| `risk_history` | Last 50 `risk_scores` rows |
| `lens_analyses` | `lens_analyses` table (Lens module outputs) |
| `medications` | `medications` table, active first |

**Important implementation note:** `get_patient_risk()` runs CfC inference synchronously (PyTorch forward pass). To avoid blocking the asyncio event loop (which would stall live telemetry for other patients), the call is wrapped in `asyncio.to_thread()`:

```python
risk = await asyncio.to_thread(get_patient_risk, patient_id)
trend = await asyncio.to_thread(get_vitals_trend, patient_id)
```

A TODO comment notes that once checkpoints are deployed, even this may be too slow for per-card-open inference, the background bot's precomputed `risk_scores` should be served instead, with CfC only re-run when specifically requested.

#### `GET /api/vigil/patients/{patient_id}/vitals`

Permission: `vitals.read`

Parameters: `hours` (int, default 6).

Returns vital sign rows from the last `hours` hours, ordered oldest first (suitable for charting). Returns only the columns needed for sparklines: timestamp, HR, SBP, DBP, SpO2, temperature, RR.

#### `GET /api/vigil/patients/{patient_id}/notes`

Permission: `patient.read_all`

Returns all clinical documents for a patient, newest first.

#### `POST /api/vigil/patients/{patient_id}/notes`

Permission: `patient.read_all`

Body: `{"content": "...", "doc_type": "Progress Note", "title": "Manual Entry"}`. Inserts a new row into `clinical_documents` with `author_id = user["id"]`. Returns `{"status": "success", "id": <row_id>}`.

#### `GET /api/vigil/patients/{patient_id}/analysis`

Permission: `patient.read_all`

Streams a clinical summary using Server-Sent Events. Implements two layers of deduplication:

1. **In-flight dedup:** If another request for the same `patient_id` is already being generated (lock is held), the second request waits behind the lock and then replays the cached result word by word at 15ms intervals (simulating streaming).
2. **Cache** (currently disabled by commented-out code): Would skip the LLM call entirely if a cached result for this patient is < 5 minutes old. The cache path is disabled pending debugging.

Returns `Content-Type: text/event-stream`. Each chunk is `data: {"content": "<token>"}\n\n`. Final chunk is `data: [DONE]\n\n`.

#### `POST /api/vigil/patients/{patient_id}/discharge`

Permission: `discharge.generate`

Triggers the full discharge workflow:
1. Verifies the patient exists and is currently admitted.
2. Calls `build_patient_timeline(patient_id)` from `discharge_service`.
3. Calls `generate_discharge_summary(patient_id)` (LLM-generated). Falls back to a plain-text summary if the LLM is unavailable.
4. Calls `discharge_patient(patient_id, user_id, summary, timeline)`.

Returns the result from `discharge_patient`.

#### `GET /api/vigil/patients/{patient_id}/discharge-summary`

Permission: `discharge.read`

Returns the most recent discharge summary for a patient: `summary` text, `timeline` (JSON-parsed), QR code URL, and `discharged_at` timestamp.

#### `GET /api/vigil/patients/{patient_id}/qr`

Permission: `discharge.read`

Serves the QR code PNG for a discharged patient from disk. Note: this endpoint was previously unauthenticated, which was a PHI exposure risk (the QR encodes a long-lived bearer token granting access to discharge data). It now requires `discharge.read`.

#### `POST /api/vigil/patients/{patient_id}/medications`

Permission: `medication.write`

Body: `{"drug_name": "...", "dosage": "...", "route": "Oral", "frequency": "...", "status": "active"}`. All of `drug_name`, `dosage`, and `frequency` are required. Inserts into `medications` with `prescribed_by = user["id"]`.

#### `POST /api/vigil/patients`

Permission: `patient.write`

Creates a new patient record and an associated Relay Case Room in one transaction:
1. Generates a patient ID (`PAT` + 6 hex chars) and a user ID (`P` + 6 hex chars).
2. Inserts a `users` row (role `"patient"`, default password `"patient123"`, **this is a demo credential, not for production**).
3. Inserts a `patients` row with the supplied demographics.
4. Inserts a `channels` row (`type = "case_room"`) for the Relay messaging module.
5. Inserts an initial admission message in that channel.

Returns `{"id": "<patient_id>", "user_id": "<uid>", "channel_id": "<cid>", "message": "Patient created and Case Room opened"}`.

---

### 3.5 Cross-cutting change checklist

If you need to modify Vigil, the following table shows which files are affected by each type of change:

| Change | Files that MUST be updated |
|---|---|
| Add or remove a vital-sign feature (change `PRODUCTION_FEATURE_ORDER`) | `vigil_cfc.py` constants, `vigil_service.py` `_CFC_FEATURE_ORDER` + `_CFC_FEATURE_DEFAULTS`, `train_vigil_cfc.py` `_PSV_VITALS` mapping; retrain all 3 models |
| Change normalization mean/std | Only in the *checkpoint* (the trainer computes and saves these); the service reads from the checkpoint, not hardcoded values |
| Change `HORIZONS_HOURS` | `vigil_cfc.py`, `vigil_service.py` (the escalation logic may need revisiting), trainer output dict keys, the VigilPanel frontend that displays per-horizon bars |
| Add a new outcome model | Add to `VIGIL_OUTCOMES` in `vigil_cfc.py`; add to `_CFC_CHECKPOINTS` in `vigil_service.py`; add a loader branch in `load_dataset_for()` in the trainer |
| Change NEWS2 thresholds/bands | `compute_news2` in `vigil_service.py` only; but audit events tagged `news2_recomputed` in the monitoring bot must also be reviewed |
| Change CfC escalation thresholds (0.9/0.85/0.7) | `get_patient_risk` in `vigil_service.py`; derive from held-out operating point data before changing |
| Switch checkpoint format to `weights_only=True` | `_load_cfc_models` in `vigil_service.py`; requires adding a JSON sidecar for `input_dim`/`horizons`/feature stats; the trainer's `torch.save` call must be updated in lockstep |
| Let a clinician prescribe Scale 2 (COPD) | Scoring is done (`on_scale_2` flag, Scale 2 branch, every lane passes it). Missing: a doctor-only route and UI control to set and clear `patients.on_scale_2`, with an audit event (gap register X003) |
