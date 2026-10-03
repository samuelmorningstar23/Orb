## 4. Forecast: Capacity, Length-of-Stay & ICU-Transfer

This section covers **Orb Forecast**, the module that predicts how long each patient will stay in hospital, how likely they are to be discharged soon, how sick they are and where they are heading clinically, and whether they may need to be moved to the ICU within 3 hours. It also aggregates those per-patient predictions into a hospital-wide view: expected bed demand, ward occupancy, and daily admission/discharge counts for the next 7 days.

### Plain-English Glossary

Before diving into the code, here are the key clinical concepts this module deals with:

**Length of Stay (LOS)**, The total number of days a patient stays in hospital from admission to discharge. Predicting the *remaining* LOS lets planners know how long each occupied bed will be needed.

**Discharge probability**, A 0–1 score for the likelihood a patient is ready for discharge within the next 24 hours. High score = this patient is likely going home soon.

**Acuity**, How sick or resource-intensive a patient currently is, and which direction they are heading. Orb uses three trajectory labels: *improving* (getting better), *stable* (no significant change), and *worsening* (deteriorating). Acuity feeds decisions about ward transfers, staffing, and care escalation.

**ICU transfer risk**, The probability that a patient will need to be moved to the Intensive Care Unit within the next 3 hours. This is the highest-stakes prediction: a missed escalation can be fatal. Because of this, the code has safety gates that suppress this number when the model has not been properly trained on real ICU-transfer data.

**BiLSTM (Bidirectional Long Short-Term Memory)**, A type of neural network designed for sequences (like time-series of vital signs). An LSTM can "remember" earlier parts of a sequence when making decisions about the current moment. *Bidirectional* means it processes the sequence both forwards and backwards, giving it context from both past and "future" timesteps within the training window. This makes it better at picking up patterns that span multiple time points, for example, a gradual rise in heart rate over 12 hours.

**Multi-task model**, One neural network that produces several different outputs simultaneously. Instead of training a separate model for LOS, one for discharge probability, one for acuity, etc., a single BiLSTM shares a common representation of the patient and then splits into separate "heads" for each output. This is efficient and often improves accuracy because the tasks share useful signal.

**Hospital-level capacity forecasting**, Rolling up all individual patient predictions into aggregate numbers: how many beds will be occupied in each ward tomorrow, the day after, etc. This uses each patient's remaining LOS to estimate when discharges will happen, combined with a statistical model of future admissions calibrated to Indian hospital patterns and weather/environmental data from Orb Pulse.

---

### Files in This Module

| File | Role |
|---|---|
| `server/models/forecast_lstm.py` | The neural network class, its `predict()` method, and `load_model()` |
| `server/services/forecast_service.py` | Runtime inference: feature extraction, per-patient and hospital-level predictions, caching, scheduling |
| `server/scripts/train_forecast.py` | Offline training pipeline: synthetic data generation, training loop, checkpoint saving |
| `server/routes/forecast_routes.py` | FastAPI HTTP endpoints that expose all of the above to the frontend |

---

### `server/models/forecast_lstm.py`

**Role:** Defines the neural network architecture (`OrbForecastLSTM`), the `predict()` high-level inference method, and `load_model()` which builds and optionally loads a saved checkpoint.

**Libraries:**
- `torch` / `torch.nn` / `torch.nn.functional`, PyTorch: builds and runs the neural network.
- `numpy`, Array conversion utilities before tensor creation.
- `pathlib.Path`, Finds the checkpoint file relative to the source file's own location.

---

#### Module-level constants

```python
INPUT_DIM = 19      # Number of input features per timestep
HIDDEN_DIM = 64     # BiLSTM units per direction (so 128 after combining both directions)
NUM_LAYERS = 2      # Stacked LSTM layers
ICU_TRANSFER_WINDOW_H = 3   # The ICU-transfer head predicts risk within this many hours
ACUITY_LABELS = {0: "improving", 1: "stable", 2: "worsening"}
```

These constants are **imported by both the training script and the service layer**, so they act as a shared contract across the whole module. If you change any of them, every consumer must be updated simultaneously (see warning notes below).

`INPUT_DIM = 19` means every timestep in the input sequence has 19 numbers:
- Features 0–5: vital signs (heart rate, systolic BP, diastolic BP, SpO2, temperature, respiratory rate)
- Features 6–8: demographics (age, sex_M flag, sex_F flag)
- Features 9–12: ward one-hot flags (ICU, W1, W2, W3)
- Features 13–15: severity one-hot flags (critical, elevated, stable)
- Feature 16: days since admission
- Feature 17: count of active medications
- Feature 18: numerical risk score from the risk-scoring module

`_CHECKPOINT_PATH` is set to `server/models/forecast_checkpoint.pt` relative to the Python file's own location. This is where `load_model()` looks for, and the training script saves to, the trained weights.

---

#### `class OrbForecastLSTM(nn.Module)`

This is the neural network itself. It extends PyTorch's `nn.Module`, which is the standard base class for all neural networks in PyTorch.

**Constructor arguments:**
- `input_dim` (default: `INPUT_DIM = 19`), Input feature count. Can be overridden to accommodate extended feature sets (e.g., adding lab values).
- `hidden_dim` (default: 64), Number of LSTM units per direction.
- `num_layers` (default: 2), Number of stacked LSTM layers.
- `dropout` (default: 0.3), Fraction of connections randomly dropped during training to prevent overfitting.

**Provenance flags (safety critical):**

Two boolean attributes are set by `load_model()`, not by the constructor:

- `self.trained: bool = False`, Starts False. Set to True by `load_model()` only when a checkpoint is successfully loaded. When False, the service layer refuses to present LSTM outputs as clinical predictions, routing to the heuristic fallback instead.
- `self.icu_head_trained: bool = False`, Starts False. Set to True only when the checkpoint includes `icu_head_trained: True` in its metadata. This flag is never set by the current synthetic training script (because synthetic data has no real ICU-transfer labels). When False, `predict()` omits the `icu_transfer_risk_3h` key entirely.

**Network layers:**

```
LayerNorm(19) → BiLSTM(64 units × 2 directions, 2 layers) → Temporal Attention → Context (128-d) → 4 task heads
```

1. `self.input_norm = nn.LayerNorm(input_dim)`, Normalises each timestep's features before they enter the LSTM. This helps training stability and speeds convergence.

2. `self.lstm = nn.LSTM(...)` with `bidirectional=True`, The core sequence model. Bidirectional means there are actually two LSTMs running in parallel: one processes the 48 timesteps in order (oldest to newest), the other processes them in reverse. Their outputs are concatenated, giving each timestep a 128-dimensional representation (64 forward + 64 backward). `batch_first=True` means the input tensor shape is `(batch, sequence, features)` not `(sequence, batch, features)`.

3. `self.attn = nn.Sequential(Linear → Tanh → Linear)`, **Temporal attention.** After the BiLSTM produces a 128-d vector for each of the 48 timesteps, this small sub-network produces a single weight for each timestep. Those weights are passed through softmax (so they sum to 1.0), then used to take a weighted average over the 48 timesteps. The result is a single 128-dimensional "context vector" that summarises the whole sequence, with more weight given to the most clinically significant moments. This is why the model can focus on a sudden deterioration event rather than averaging it away.

4. **Four task heads** (all linear layers operating on the 128-d context vector):
   - `self.los_head` → single number, passed through Softplus (ensures output ≥ 0) → predicted remaining days
   - `self.discharge_head` → single number, passed through Sigmoid (maps to 0–1) → discharge probability
   - `self.acuity_head` → 3 numbers (logits for improving/stable/worsening) → acuity trajectory after softmax
   - `self.icu_head` → single number, passed through Sigmoid → ICU-transfer probability within 3 h

---

#### `forward(x: torch.Tensor) -> dict`

The raw forward pass. Takes a tensor of shape `(batch, seq_len, input_dim)` and returns a dict of raw head outputs:

```python
{
  "los": tensor of shape (B, 1),           # >= 0, Softplus-activated
  "discharge_prob": tensor of shape (B, 1), # [0,1], Sigmoid-activated
  "acuity_logits": tensor of shape (B, 3),  # raw logits, not yet softmaxed
  "icu_transfer_prob": tensor of shape (B, 1) # [0,1], Sigmoid-activated
}
```

If given a 2-D input `(seq, features)`, it automatically adds the batch dimension. The steps are: normalise → BiLSTM → compute attention weights → weighted-sum to context vector → four heads in parallel.

---

#### `predict(features) -> dict`

The public-facing inference method. Decorated with `@torch.no_grad()` which disables gradient computation (not needed at inference time, saves memory and compute). Sets the model to evaluation mode (`self.eval()`).

**Input:** A feature array of shape `(seq, 19)` or `(1, seq, 19)`. Accepts both numpy arrays and PyTorch tensors.

**Output dict (always present):**
```python
{
  "predicted_los_days": float,       # Remaining LOS in days, rounded to 1 d.p.
  "discharge_probability": float,    # [0.0, 1.0], rounded to 4 d.p.
  "acuity_trajectory": str,          # "improving", "stable", or "worsening"
  "confidence": float,               # Softmax probability of the predicted acuity class
  "model_trained": bool,             # Whether the model was loaded from a checkpoint
}
```

**Conditionally present:**
```python
  "icu_transfer_risk_3h": float   # Only present when self.icu_head_trained == True
```

The ICU gate is a clinical safety feature. If `self.icu_head_trained` is False (as it is in every current deployment), the key is simply absent from the dict. This means the frontend `ForecastPanel` will not render an ICU-transfer risk score, and no caller can accidentally interpret random-initialisation sigmoid output as a clinical probability.

---

#### `load_model(checkpoint_path=None, device=None) -> OrbForecastLSTM`

Builds an `OrbForecastLSTM` and optionally loads weights from the checkpoint file.

**Device selection:** Prefers Apple MPS (Metal Performance Shaders, for M-series Macs) over CPU. If you later run on a CUDA machine, add `torch.cuda.is_available()` as a higher-priority check.

**Happy path (checkpoint exists and loads cleanly):**
1. Loads the `.pt` file with `weights_only=True`. This flag is a security measure: it disables arbitrary Python object unpickling, so a malicious checkpoint cannot execute code.
2. Reads `input_dim` from the checkpoint metadata, allowing checkpoints trained on extended feature sets to work correctly.
3. Builds `OrbForecastLSTM(input_dim=...)` and calls `load_state_dict()` to transfer the saved weights.
4. Sets `model.trained = True` and reads `model.icu_head_trained` from the checkpoint's `icu_head_trained` key (defaults to False if absent).
5. Logs the epoch, data source, input_dim, and device.

**Fallback paths (both leave `model.trained = False`):**
- Checkpoint file does not exist → logs a warning, returns untrained model.
- Checkpoint loads but something goes wrong → logs an error, returns untrained model.

The fallback is intentional: the server boots and serves heuristic predictions rather than crashing because training has not been run yet.

⚠️ **If you change `INPUT_DIM`:** The shape of the input normalisation layer (`LayerNorm`) and the LSTM's `input_size` are both determined by `input_dim`. If you change `INPUT_DIM` in `forecast_lstm.py` but load an old checkpoint (which has a different `input_dim`), `load_state_dict()` will throw a shape mismatch error. Solution: always re-train after changing the feature set, and the checkpoint's saved `input_dim` will be updated automatically.

⚠️ **If you add, remove, or rename output heads:** The `forward()` method returns a dict, and `predict()` reads keys from that dict. `forecast_service.py` calls `model.predict()` and passes results to the API. The `forecast_routes.py` endpoints pass these results directly to the frontend. `ForecastPanel` in the frontend reads specific keys. A head rename cascades through all of these.

---

### `server/services/forecast_service.py`

**Role:** The runtime engine of the Forecast module. It sits between the neural network (`forecast_lstm.py`) and the HTTP layer (`forecast_routes.py`). It handles feature extraction from the database, per-patient prediction with heuristic fallback, hospital-level flow forecasting, bed capacity projections, discharge boards, admission pattern analysis, and appointment scheduling.

This file is approximately 950 lines and can be understood in six functional groups.

**Libraries:**
- `torch`, Builds the feature tensor passed to the LSTM.
- `numpy`, Assembles the raw feature matrix before converting to a tensor.
- `json`, Serialises/deserialises the `feature_snapshot` column in `forecast_predictions`.
- `datetime`, `timedelta`, `timezone`, All date arithmetic for LOS, estimated discharge dates, and forecast day labelling.
- `time`, For the 1-hour hospital flow cache TTL check.
- `math`, `random`, Smoothing maths and seeded random sampling for the appointments schedule.
- `pathlib.Path`, Not used directly in this file; imported transitively via `forecast_lstm`.
- `logging`, All log output via `logger = logging.getLogger("orb.forecast")`.
- `database.get_db`, `database.db_session`, Orb's database utilities for reading patients, vitals, risk scores, medications, and writing predictions.
- `models.forecast_lstm.OrbForecastLSTM`, `load_model`, `INPUT_DIM`, The model class, loader, and feature dimension constant.

---

#### Group 1: Module-level state and configuration

```python
_model: OrbForecastLSTM | None = None          # Singleton model instance

_hospital_flow_cache: dict | None = None        # Cached 7-day forecast
_hospital_flow_cache_time: float = 0
HOSPITAL_FLOW_CACHE_TTL = 3600                 # Cache lives for 1 hour

WARD_CAPACITY = {"ICU": 8, "W1": 8, "W2": 8, "W3": 8}   # Beds per ward
TOTAL_CAPACITY = 32                            # Sum of all beds

DOW_MULTIPLIERS = {0: 1.35, ..., 6: 0.65}    # Monday surge, Sunday quiet
```

`WARD_CAPACITY` and `TOTAL_CAPACITY` are used in several places. If your hospital has different bed counts, update `WARD_CAPACITY` here, `TOTAL_CAPACITY` is derived automatically.

`DOW_MULTIPLIERS` encodes the pattern that Indian hospitals see ~35% more admissions on Mondays (post-weekend backlog) and ~35% fewer on Sundays. These are applied when forecasting future admission counts.

#### `get_model() -> OrbForecastLSTM`

A lazy singleton loader. The first call to this function triggers `load_model()`, which builds the network and tries to load the checkpoint from disk. All subsequent calls return the cached `_model` instance. This avoids re-loading the model on every prediction request.

---

#### Group 2: Pulse integration (`_get_environmental_multipliers`)

**`_get_environmental_multipliers(days_ahead: int = 7) -> list[dict]`**

Reads cached weather, AQI (air quality index), and flu surveillance data that was fetched by Orb Pulse (a separate service). Returns one dict per forecast day with:
- `multiplier`, A number like 1.20, meaning "expect 20% more admissions today due to environmental factors"
- `factors`, Human-readable explanation strings (e.g., "Thunderstorm (+20% trauma)")
- `icons`, Icon keys for the frontend (e.g., "storm", "rain", "heat", "aqi", "flu")

The logic stacks multipliers multiplicatively. For example, extreme heat (×1.20) combined with very poor air quality (×1.20) gives a multiplier of 1.44, a 44% admission surge above baseline. The thresholds are:

| Signal | Threshold | Multiplier | Clinical rationale |
|---|---|---|---|
| Weather code ≥ 95 (thunderstorm) | n/a | ×1.20 | Trauma surge (accidents, falls) |
| Weather code ≥ 80 (heavy rain) | n/a | ×1.10 | Trauma surge |
| Max temp ≥ 42°C | Extreme heat | ×1.20 | Cardiac/renal emergencies |
| Max temp ≥ 38°C | Heat advisory | ×1.10 | Cardiac |
| Min temp ≤ 5°C | Cold snap | ×1.10 | Cardiac/respiratory |
| AQI > 200 | Very Unhealthy | ×1.20 | Respiratory admissions |
| AQI > 150 | Unhealthy | ×1.15 | |
| AQI > 100 | Sensitive groups | ×1.05 | |
| ILI% > 5.0 | Flu surge | ×1.35 | Respiratory surge |
| ILI% > 3.0 | Elevated flu | ×1.15 | |

If `pulse_service` is not importable (e.g., Pulse module not installed), all multipliers default to 1.0 silently.

---

#### Group 3: Feature extraction (`extract_patient_features`)

**`extract_patient_features(patient_id: str, seq_len: int = 48) -> torch.Tensor | None`**

Queries the database and builds the `(1, 48, 19)` input tensor the LSTM expects.

**Database queries:**
1. `patients` table → demographics (age, sex, ward, admission_date)
2. `vitals` table → up to 48 most recent vitals rows, ordered newest-first, then reversed so the tensor is chronological (oldest timestep first)
3. `risk_scores` table → most recent risk score and level
4. `medications` table → count of active medications

**Returns `None` if:** the patient does not exist in the database, or fewer than 3 vital sign readings are available (the LSTM needs some history to be meaningful).

**Padding/truncating:** If fewer than 48 vitals readings exist, the last available reading is repeated forward to fill the remaining timesteps. This is a conservative choice: it means the LSTM sees a "flat" signal for missing periods rather than zeros, which is closer to clinical reality (vitals do not usually drop to zero).

**Feature assembly (per timestep t, matching the training schema exactly):**

| Index | Feature | Source | Fallback |
|---|---|---|---|
| 0 | Heart rate | vitals.heart_rate | 80.0 |
| 1 | Systolic BP | vitals.systolic_bp | 120.0 |
| 2 | Diastolic BP | vitals.diastolic_bp | 75.0 |
| 3 | SpO2 | vitals.spo2 | 97.0 |
| 4 | Temperature | vitals.temperature | 37.0 |
| 5 | Respiratory rate | vitals.respiratory_rate | 16.0 |
| 6 | Age | patients.age | 50 |
| 7 | Sex_M | patients.sex == "M" | 0.0 |
| 8 | Sex_F | patients.sex == "F" | 0.0 |
| 9 | Ward_ICU | patients.ward == "ICU" | 0.0 |
| 10 | Ward_W1 | patients.ward in W1 variants | 0.0 |
| 11 | Ward_W2 | patients.ward in W2 variants | 0.0 |
| 12 | Ward_W3 | patients.ward in W3 variants | 0.0 |
| 13 | Severity_critical | risk_scores.level == "critical" | 0.0 |
| 14 | Severity_elevated | risk_scores.level == "elevated" | 0.0 |
| 15 | Severity_stable | risk_scores.level == "stable" | 0.0 |
| 16 | Days since admission | calculated from admission_date | 3.0 |
| 17 | Medication count | COUNT(medications where active) | 3 |
| 18 | Risk score | risk_scores.score | 0.5 |

The result is wrapped in `torch.tensor(..., dtype=torch.float32).unsqueeze(0)` to add the batch dimension, giving shape `(1, 48, 19)`.

⚠️ **This feature schema must match `INPUT_DIM = 19` and the column ordering used in `train_forecast.py`'s `SyntheticPatientTrajectory.generate_sequence()`.** If you add a feature here, you must add the same feature in the same column position in the training data generator, increment `INPUT_DIM`, and re-train.

---

#### Group 4: Per-patient predictions

**`predict_patient_los(patient_id: str) -> dict`**

The main public function for a single patient prediction. It gates on whether the model is trained, and routes accordingly.

**Flow diagram:**

```
get_model()
    │
    ├─ model.trained == False?
    │       └─ _heuristic_prediction() → tag source="heuristic", model_trained=False → _cache_prediction() → return
    │
    └─ model.trained == True
            │
            ├─ extract_patient_features() == None (< 3 vitals)?
            │       └─ _heuristic_prediction() → tag source="heuristic", model_trained=True → _cache_prediction() → return
            │
            └─ features extracted
                    └─ vram_guard("Forecast LSTM")  ← coordinates GPU memory with other models
                            └─ model.predict(features)
                                    └─ add estimated_discharge_date
                                            └─ _cache_prediction() → return
```

The output dict always contains: `predicted_los_days`, `discharge_probability`, `acuity_trajectory`, `confidence`, `model_trained`, `prediction_source` ("lstm" or "heuristic"), and `estimated_discharge_date`. `icu_transfer_risk_3h` is conditionally present (only when `model.icu_head_trained == True`).

In the current pre-trained state (no checkpoint), every call returns a heuristic result tagged `model_trained=False` and `prediction_source="heuristic"`. The frontend and API consumers can check `model_trained` to display appropriate confidence indicators.

---

**`_heuristic_prediction(patient_id: str) -> dict`**

A rule-based fallback. Queries the patient's ward and admission date, then:

1. Looks up a baseline LOS by ward: ICU=7.0 d, W1=4.0 d, W2=3.0 d, W3=3.5 d
2. Subtracts days already spent in hospital to get remaining LOS
3. Computes discharge probability as `1 / (remaining_los + 0.5)`, a simple formula that increases as remaining LOS approaches 0
4. Returns `acuity_trajectory="stable"` always (no data to infer trajectory)
5. Returns `confidence=0.45` (lower than LSTM predictions would be, reflecting uncertainty)

If the patient is not found, returns conservative defaults: 3-day LOS, 15% discharge probability, stable.

---

**`_cache_prediction(patient_id: str, result: dict)`**

Writes the prediction to the `forecast_predictions` table with columns: `patient_id`, `predicted_los_days`, `discharge_probability`, `acuity_trajectory`, `confidence`, and a JSON blob `feature_snapshot` containing the estimated discharge date and timestamp. Silently logs and swallows database errors (cache failures should not crash the prediction pipeline).

---

#### Group 5: Hospital-level forecasting

**`predict_hospital_flow(days_ahead: int = 7) -> dict`**

Produces the 7-day hospital flow forecast shown in the Forecast Panel. Results are cached in the module-level `_hospital_flow_cache` for 1 hour (`HOSPITAL_FLOW_CACHE_TTL = 3600`), so UI refreshes do not cause the forecast to flicker.

**Step-by-step logic:**

1. **Cache check**, Returns cached result immediately if it is less than 1 hour old.

2. **Current census**, Queries all admitted patients and counts them by normalised ward (ICU / W1 / W2 / W3).

3. **Admission rate estimation**, Queries the `admission_history` table for the last 30 days (one row per date with a count). Computes the simple average (`avg_admission_rate`). If no history exists, defaults to 4.5 admissions/day (a calibrated small-Indian-hospital baseline).

4. **Per-patient predictions**, For each admitted patient, calls `_get_cached_or_predict(patient_id)` to get their predicted remaining LOS. This uses the cache table first and only triggers live inference if no cached prediction exists.

5. **Environmental multipliers**, Calls `_get_environmental_multipliers(days_ahead)` to get a per-day multiplier from Pulse weather/AQI/flu data.

6. **Daily forecast loop**, For each future day (1 through 7):

   - **Admissions:** `avg_admission_rate × DOW_MULTIPLIERS[weekday] × env_multiplier`. The result is the Poisson *mean* (not a single random draw), this keeps the forecast stable across cache refreshes.
   - **Discharges:** Counts patients whose `predicted_remaining_days` falls in the window `(day, day+1]`. Then applies two smoothing rules:
     - Cap at `n_patients // 3` (no more than a third of patients can leave in one day, prevents unrealistic spikes)
     - If no discharges were predicted and `day > 0`, floor to `max(1, n_patients * 0.08)`, about 8% base discharge rate to keep the curve plausible in sparse-data conditions. **This is explicitly labelled in the code as a demo placeholder, not a model output.**
   - **Occupancy:** `running_occupancy = previous_occupancy - discharges + admissions`, clamped to `[0, TOTAL_CAPACITY + 5]`.
   - **Ward breakdown:** Distributes projected occupancy proportionally to current ward distribution.
   - Each day's entry includes `env_multiplier`, `env_factors`, and `env_icons` for the frontend to show environmental context.

7. **Summary statistics:** peak day and occupancy, average occupancy, total predicted 7-day discharges and admissions, beds available in 48 h.

---

**`get_bed_capacity_forecast() -> dict`**

Simpler than `predict_hospital_flow`. Returns current and 48-hour-projected occupancy for each ward.

- **Current:** For each ward, counts admitted patients whose ward normalises to that ward's key, computes `occupied / capacity * 100`.
- **48h projection:** For each admitted patient, if their `predicted_los_days` is ≤ 2.0, they are counted as a likely discharge within 48 h. Projected occupancy = current - expected discharges per ward (no admission additions, making this a conservative floor estimate).

Output format:
```python
{
  "current": {"ICU": {"total": 8, "occupied": 3, "available": 5, "utilization_pct": 37.5}, ...},
  "projected_48h": {"ICU": {"projected_occupied": 2, "projected_available": 6, "projected_utilization_pct": 25.0}, ...},
  "total": {"capacity": 32, "occupied": N, "available": M}
}
```

---

**`get_discharge_board() -> list`**

Builds the Discharge Readiness Board: a list of all admitted patients sorted by descending discharge probability (most likely to be discharged first).

For each patient: queries demographics and admission date, calls `_get_cached_or_predict()`, computes `days_admitted`, and assembles the entry. The sort key is `discharge_probability` descending.

Returned fields per patient: `patient_id`, `name`, `ward`, `bed`, `diagnosis`, `age`, `sex`, `days_admitted`, `predicted_remaining_days`, `discharge_probability`, `acuity_trajectory`, `confidence`, `estimated_discharge_date`.

---

**`get_admission_patterns() -> dict`**

Historical analysis of the `admission_history` table. Returns:
- `daily_summary`, 7-element list (Mon–Sun), each with average admissions on that day of the week
- `total_30d`, total admission records
- `avg_daily`, records / 30
- `avg_los`, average length of stay from historical data
- `severity_distribution`, counts of critical / elevated / stable
- `ward_distribution`, counts per ward
- `discharge_rate`, percentage of historical admissions that are marked discharged

If the `admission_history` table is empty (e.g., training has not been run), returns safe zero-filled defaults.

---

#### Group 6: Refresh loop and helpers

**`refresh_all_predictions()`**

Called by Orb's bot service on a ~60-second timer. Iterates all admitted patients and calls `predict_patient_los()` for each, updating the `forecast_predictions` cache table. After refreshing, runs a SQL window-function query to delete all but the 5 most recent predictions per patient, preventing unbounded table growth. Errors on individual patients are logged and swallowed so one bad patient does not abort the whole refresh.

---

**`_normalize_ward(ward: str) -> str`**

Maps free-text ward names to the four canonical keys (ICU, W1, W2, W3). Logic: uppercase and strip → exact match for "ICU" → contains "1" → contains "2" → contains "3" → default to "W1". This handles variants like "Ward 1", "WARD1", "w1", etc.

---

**`_get_cached_or_predict(patient_id: str) -> dict`**

Used by all hospital-level functions to avoid re-running LSTM inference on every request. Queries `forecast_predictions` for the most recent row for this patient. If found, returns it as a dict (deserialising the JSON `feature_snapshot` for the discharge date). If not found or on any database error, falls back to `predict_patient_los()`.

---

#### Group 7: Appointment scheduling (`get_appointments`)

**`get_appointments(days_ahead: int = 7) -> list`**

Generates a deterministic appointment schedule for the next N days, used to populate the Appointment Calendar view.

- **Sources:** Queries all admitted patients (demographics, ward, bed, diagnosis, admission date, most recent risk level, active medication count) and up to 10 recently discharged patients (from `discharge_summaries` joined to `patients`).
- **Daily schedule generation:** For each non-Sunday day in the window, seeds Python's `random.Random` with the date as an integer (e.g., 20260626). This seeding makes results deterministic: the same date always produces the same appointments, so refreshing the page does not flicker.
- **Selects 3–5 admitted patients** per day (seeded-random sample), plus a 40% chance of adding one recently discharged patient for a Post-Discharge Review.
- **Assigns time slots** from `_APPT_TIMES` (09:00 through 16:00 in 30-minute slots), shuffled by the seeded RNG.
- **Appointment types** from `_APPT_TYPES` (Follow-Up, Lab Results Review, etc.); discharged patients always get "Post-Discharge Review".
- **Visit reasons** from `_VISIT_REASONS` (randomly selected per appointment).
- Sundays are skipped entirely (no appointments).
- Results are sorted by date then time before returning.

Each entry includes: `date`, `day_label`, `time`, `patient_id`, `patient_name`, `age`, `sex`, `ward`, `bed`, `diagnosis`, `type`, `reason`, `acuity` (from risk level), `days_admitted`, `active_medications`, `is_discharged`.

---

### `server/scripts/train_forecast.py`

**Role:** Offline training pipeline. Run once (or whenever you have new data) to produce `server/models/forecast_checkpoint.pt`. Not involved in runtime serving.

**Usage:**
```bash
cd server && .venv/bin/python3 scripts/train_forecast.py
```

**Libraries:**
- `torch`, `torch.nn`, `torch.optim`, `torch.utils.data`, Neural network, loss functions, optimiser, DataLoader.
- `numpy`, Numerical feature generation.
- `sqlite3`, Direct database access for writing `admission_history` records.
- `random`, `math`, Sampling and arithmetic in data generation.
- `pathlib.Path`, Locates `orb.db` and the checkpoint output path.
- Standard library: `sys`, `os`, `json`, `datetime`.

---

#### Indian clinical calibration data

The script opens with a large block of constants calibrated to Indian hospital statistics:

**`INDIAN_DIAGNOSES`**, A dict with three severity keys (critical / elevated / stable), each mapping to a list of diagnosis profiles. Each profile has:
- `diag`, Diagnosis name (e.g., "Acute MI, STEMI")
- `los_mu`, `los_sigma`, Parameters of a log-normal distribution for LOS in days. Note: these are *log-space* mean and sigma, not the actual mean days. `max(1.0, lognormal(mu, sigma))` is used to sample, giving realistic right-skewed LOS distributions.
- `base_hr`, `base_sbp`, Starting vital signs for this diagnosis, around which the synthetic time-series drifts.

Examples of calibration: ICU-level sepsis (critical) has `los_mu=2.2, los_sigma=0.5` (median ~9 days at log-normal, reflecting Indian multi-organ sepsis stays). Cholecystitis post-surgery (stable) has `los_mu=0.7, los_sigma=0.2` (short stay).

**`INDIAN_MALE_NAMES`, `INDIAN_FEMALE_NAMES`**, Census-representative Indian name pools for generating realistic patient names in the admission history.

**`sample_indian_age() -> int`**, A weighted random age sampler. The distribution skews toward older patients (>55), reflecting that hospital admissions in India are concentrated in the 46–75 age range. Specific breakpoints and probabilities are drawn from NITI Aayog health data.

**`WARD_MAP`**, Probability-weighted ward assignments by severity (e.g., critical patients go to ICU 80% of the time).

**`DOW_MULTIPLIERS`**, Same day-of-week admission multipliers as in `forecast_service.py`.

---

#### `class SyntheticPatientTrajectory`

Generates a single synthetic patient's full clinical trajectory for training.

**Constructor:** Randomly assigns severity (25% critical, 30% elevated, 45% stable), picks a diagnosis profile, samples true LOS from a log-normal distribution, assigns demographics (sex, age, ward from `WARD_MAP`), and medication count by severity.

**`generate_sequence(current_day=None) -> (tensor, labels_dict)`**

Generates the `(48, 19)` feature tensor and ground-truth labels for one observation of this patient.

- `current_day`, How far into the patient's stay the observation is made. If None, randomly sampled between 0.5 days and (true_los - 0.5) days, simulating a model that observes patients at various stages of their admission.
- `remaining_los = true_los - current_day`, The regression label.

**Time-series generation (per timestep t):**
- `improvement_factor = min(1.0, day_progress / true_los)`, A 0-to-1 progress signal that drives vital sign trajectories. As this increases, heart rate drifts toward 78, BP toward 120, SpO2 toward 98, temperature toward 36.8, respiratory rate toward 16. Critical patients have `improvement_factor = 0`, they do not improve in this model.
- Random Gaussian noise is added to each vital at each timestep (realistic measurement noise).
- Static features (age, sex, ward, severity, days admitted, medication count, risk score) are repeated identically at every timestep.
- With 5% probability per timestep, critical patients get a "crisis spike": HR jumps, SpO2 drops, SBP drops. This simulates acute deterioration events.

**Labels:**
- `los`: `remaining_los` (regression target)
- `discharge_prob`: `1.0 / (remaining_los + 0.5)` + Gaussian noise (0.08 std), clipped to [0, 0.95]. For critical patients early in their stay, this is multiplied by 0.3 (discharge is unlikely even if remaining LOS is low). The code comment notes that in a production system, discharge probability should be trained on actual discharge events rather than derived from LOS.
- `acuity`: 0 (improving) if `improvement_factor > 0.5`, 2 (worsening) if critical and early, 1 (stable) otherwise.

**No ICU-transfer label is generated.** This is why `icu_head_trained` is never set in the saved checkpoint.

---

#### `class ClinicalDataset(Dataset)`

A standard PyTorch `Dataset` wrapping a list of `(features_tensor, labels_dict)` samples.

- Takes `n_patients` (default 500) synthetic patients.
- If `augment=True` (default for training), generates 3 observation points per patient at different stages of their stay, for a total of ~1,500 samples.
- If `augment=False` (for validation set), generates 1 observation per patient.
- `__getitem__` returns `(features, los_target, discharge_target, acuity_target)`, separate tensors for each label, typed appropriately (float32 for regression/probability, long for the integer acuity class index).

---

#### `train_model(epochs=50, lr=1e-3, batch_size=32) -> OrbForecastLSTM`

The main training loop.

**Setup:**
- Creates training dataset (500 patients × 3 obs = 1,500 samples) and validation dataset (100 patients × 1 obs = 100 samples).
- Builds `OrbForecastLSTM()` with default dimensions.
- Prints parameter count (~60K–100K depending on exact architecture).

**Loss functions:**
| Head | Loss function | Why |
|---|---|---|
| LOS (regression) | `MSELoss` | Mean squared error; penalises large prediction errors more heavily |
| Discharge probability | `BCELoss` | Binary cross-entropy; appropriate for a single sigmoid output |
| Acuity trajectory | `CrossEntropyLoss` | Multi-class classification loss; takes raw logits, not softmax output |

**Task weights:** `alpha=1.0` (LOS), `beta=0.5` (discharge), `gamma=0.3` (acuity). LOS prediction is the primary task; the other heads are regularisers that share signal.

**Optimiser:** `AdamW` with `lr=1e-3` and `weight_decay=1e-4` (L2 regularisation). AdamW is a standard choice for small-to-medium networks.

**Scheduler:** `CosineAnnealingLR` over `T_max=epochs`, the learning rate follows a cosine curve from `lr` down to ~0, then potentially back up. This helps escape local minima late in training.

**Gradient clipping:** `clip_grad_norm_(max_norm=1.0)`, caps the gradient norm before each optimiser step, preventing the exploding-gradient problem that LSTMs are prone to.

**Checkpoint saving:** Saves the state dict whenever validation loss improves. The checkpoint dict includes: `model_state_dict`, `epoch`, `best_loss`, `input_dim`, `hidden_dim`, `num_layers`, `training_date`, `data_source`. Note that `icu_head_trained` is **not** included, so `load_model()` defaults it to False for any checkpoint produced by this script.

**Print cadence:** Prints a loss breakdown every 5 epochs. Full training (50 epochs, 1,500 samples, batch=32) takes a few seconds on a modern laptop CPU.

---

#### `generate_admission_history()`

Populates the `admission_history` table in `orb.db` with 30 days of synthetic Indian hospital admissions. This provides the historical data that `predict_hospital_flow()` uses to estimate the baseline admission rate.

- Creates the `admission_history` table if it does not exist.
- Deletes all existing rows first (idempotent, safe to re-run).
- For each of the 30 past days: samples a Poisson-distributed number of admissions using `np.random.poisson(base_rate * DOW_MULTIPLIERS[dow])` where `base_rate = 4.5`.
- For each admission: randomly selects severity, diagnosis, LOS, demographics, ward.
- Sets `discharged=1` if `admission_date + los_days < today`, else 0.
- Commits all records in a single transaction.

---

#### `__main__` block

When the script is run directly (`python3 scripts/train_forecast.py`):

1. Calls `train_model(epochs=50)`, trains and saves the checkpoint.
2. Runs a quick inference test with random noise input to confirm the model's forward pass works.
3. Calls `generate_admission_history()`, populates `admission_history` table.

---

### `server/routes/forecast_routes.py`

**Role:** Thin HTTP layer. Defines the FastAPI router at prefix `/api/forecast` and maps URL paths to service functions. All business logic lives in `forecast_service.py`; this file is almost entirely boilerplate.

**Libraries:**
- `fastapi.APIRouter`, `Depends`, `HTTPException`, FastAPI's routing primitives.
- `auth.get_current_user`, `auth.require_role`, Imported but not directly used in route signatures (role checking is handled via `require_permission`).
- `permissions.require_permission`, A FastAPI dependency that checks the logged-in user has the named permission. All forecast routes require `"forecast.read"`.

**Router setup:** `router = APIRouter(prefix="/api/forecast", tags=["forecast"])`, all routes below are mounted under `/api/forecast`.

---

#### `GET /api/forecast/hospital`

**Function:** `hospital_forecast(days: int = 7, user: dict = Depends(require_permission("forecast.read")))`

- Accepts optional `days` query parameter (default 7, capped at 14 by `min(days, 14)`).
- Calls `predict_hospital_flow(days_ahead=...)`.
- Returns the full forecast dict (current occupancy, 7-day daily forecasts with admissions/discharges/occupancy/ward breakdown/environmental factors, summary statistics).
- On error: 500 with `"Forecast error: <detail>"`.

---

#### `GET /api/forecast/patient/{patient_id}`

**Function:** `patient_forecast(patient_id: str, user: dict = Depends(require_permission("forecast.read")))`

- `patient_id` is a path parameter.
- Calls `predict_patient_los(patient_id)`.
- Returns the prediction dict (LOS, discharge probability, acuity, confidence, model_trained, prediction_source, estimated discharge date, and optionally ICU transfer risk).
- On error: 500 with `"Prediction error: <detail>"`.

---

#### `GET /api/forecast/capacity`

**Function:** `capacity_forecast(user: dict = Depends(require_permission("forecast.read")))`

- No query parameters.
- Calls `get_bed_capacity_forecast()`.
- Returns current and 48h-projected occupancy by ward.
- On error: 500 with `"Capacity error: <detail>"`.

---

#### `GET /api/forecast/discharge-board`

**Function:** `discharge_board(user: dict = Depends(require_permission("forecast.read")))`

- No query parameters.
- Calls `get_discharge_board()`.
- Returns list of admitted patients sorted by discharge probability.
- On error: 500 with `"Board error: <detail>"`.

---

#### `GET /api/forecast/admission-patterns`

**Function:** `admission_patterns(user: dict = Depends(require_permission("forecast.read")))`

- No query parameters.
- Calls `get_admission_patterns()`.
- Returns historical admission statistics (day-of-week summary, severity/ward distributions, discharge rate).
- On error: 500 with `"Pattern error: <detail>"`.

---

#### `GET /api/forecast/appointments`

**Function:** `appointments_schedule(days: int = 7, user: dict = Depends(require_permission("forecast.read")))`

- Accepts optional `days` query parameter (default 7, capped at 14).
- Calls `get_appointments(days_ahead=...)`.
- Returns sorted list of appointment entries for the next N non-Sunday days.
- On error: 500 with `"Appointments error: <detail>"`. Unlike the other routes, this one also calls `traceback.print_exc()` on error for easier debugging during development.

---

### Pre-Trained State: What the System Does Today

**Current state:** No checkpoint file exists at `server/models/forecast_checkpoint.pt`. The model is at random initialisation.

**What happens:**
- `load_model()` logs a warning and returns an `OrbForecastLSTM` with `trained=False` and `icu_head_trained=False`.
- Every call to `predict_patient_los()` hits the `if not getattr(model, "trained", False)` guard and routes to `_heuristic_prediction()` instead of running the LSTM.
- All predictions are tagged `model_trained=False` and `prediction_source="heuristic"`.
- The ICU-transfer risk key never appears in any response.
- Hospital-level forecasting still works using the heuristic LOS values from `_get_cached_or_predict()`.
- The system is fully functional for demo purposes; predictions are clearly low-confidence and rule-based.

**To activate the LSTM:**
1. Obtain real patient data (e.g., MIMIC-IV or hospital data exports).
2. Adapt `train_forecast.py` to load from that source instead of generating synthetic data.
3. Run `python3 scripts/train_forecast.py`.
4. Restart the server. `load_model()` will find the checkpoint, set `trained=True`, and all subsequent predictions will use the LSTM.

---

### Summary of Safety Gates and Change Warnings

**`model_trained` flag:**
- Controlled by `OrbForecastLSTM.trained` (set by `load_model()`).
- Checked in `predict_patient_los()` before running the LSTM.
- When False: heuristic fallback, predictions labelled accordingly.
- Do not remove this gate. An untrained LSTM at random initialisation produces meaningless clinical numbers.

**`icu_head_trained` flag:**
- Controlled by `OrbForecastLSTM.icu_head_trained` (set by `load_model()` from checkpoint metadata).
- Checked in `predict()` before including `icu_transfer_risk_3h` in the output.
- Currently always False (synthetic trainer does not set it).
- To activate: train with real ICU-transfer labels, add `"icu_head_trained": True` to the checkpoint dict saved by the training script.
- Do not remove this gate. The ICU-transfer head on a randomly initialised or LOS-only-trained network is not a clinical prediction, it is noise.

⚠️ **Changing `INPUT_DIM` or the 19-column feature schema:**
- Breaks `load_model()` (shape mismatch when loading old checkpoints).
- Breaks `extract_patient_features()` (tensor shape changes).
- Requires re-training.
- Both the training data generator in `train_forecast.py` and the live feature extractor in `forecast_service.py` must use exactly the same feature ordering and normalisation.

⚠️ **Changing the output heads (adding/removing/renaming):**
- `forecast_lstm.py`: update `forward()` return dict and `predict()` consumer.
- `forecast_service.py`: `predict_patient_los()` passes `model.predict()` output downstream; check all downstream consumers.
- `forecast_routes.py`: routes pass dicts through; check what the frontend expects.
- Frontend `ForecastPanel`: reads specific keys from the API response; update accordingly.

⚠️ **Changing `WARD_CAPACITY`:**
- Updates `TOTAL_CAPACITY` automatically (derived via `sum()`).
- Affects `get_bed_capacity_forecast()` utilization percentages, `predict_hospital_flow()` occupancy clamping and ward breakdown, and any frontend display that shows ward capacity.

⚠️ **Changing `HOSPITAL_FLOW_CACHE_TTL`:**
- The hospital flow forecast is stable for up to 1 hour. If you lower this value, forecast calls become more frequent, increasing database load. If you raise it, the forecast becomes more stale relative to new admissions/discharges.
