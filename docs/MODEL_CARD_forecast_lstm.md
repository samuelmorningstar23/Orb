# Model Card: Orb Forecast LSTM (`forecast_lstm_synth_v1`)

> Honest, working document, not marketing. Companion to
> [`ORB_TRAINING_PLAYBOOK.md`](./ORB_TRAINING_PLAYBOOK.md) and
> [`MODEL_TRAINING.md`](./MODEL_TRAINING.md).

| | |
|---|---|
| **Model** | Orb Forecast, multi-task Bidirectional LSTM with temporal attention |
| **Code** | `server/models/forecast_lstm.py` (`OrbForecastLSTM`, `load_model`, `INPUT_DIM`) |
| **This checkpoint** | `forecast_lstm_synth_v1.pt` |
| **Training data** | **SYNTHETIC v1, deterministic self-contained generator. NOT real clinical data.** |
| **Produced by** | `server/scripts/train_forecast_synth.py` (seed `1234`) |
| **Status** | Demo / plumbing only. **NOT for any clinical use.** Not regulatory-cleared. |
| **Date** | Checkpoint stamps `training_date` in UTC at generation time |

---

## ⚠️ One-paragraph honesty notice

This checkpoint contains **real trained weights** (not random init), so
`load_model()` reports `trained=True` and the service exercises the neural
inference path instead of the heuristic fallback. But it was trained **only on
synthetic, algorithmically generated data**, it has **never seen a real
patient** and has **not been validated** against any real outcome. Its LoS,
discharge-probability, and acuity outputs are **not clinically meaningful**.
Its purpose is to (a) un-block the end-to-end plumbing and (b) provide a
reproducible training + export harness that we later re-point at real data.
**Before any clinical use it must be retrained on MIMIC-IV and externally
validated on eICU** per [`ORB_TRAINING_PLAYBOOK.md`](./ORB_TRAINING_PLAYBOOK.md)
§2.1 / §7.

## The checkpoint is NOT committed to git

`*.pt` and `server/models/*/` are both in `.gitignore`, weights are
intentionally never committed. **The deliverable is the reproducible harness +
this card**, not the binary. You regenerate the checkpoint locally by running
the script (see [Reproduce](#reproduce)). The runtime falls back to the
transparent heuristic (`prediction_source="heuristic"`, `model_trained=false`)
whenever no checkpoint is present, so a clean checkout is safe by default.

---

## Intended use

- **In scope (as decision *support* only):** surfacing an estimated
  length-of-stay, a discharge-readiness probability, and an acuity-trajectory
  label to clinicians on the Discharge Readiness Board and hospital-flow
  forecasts, as **one input among many**, clearly labelled with its provenance
  and confidence.
- **Explicitly out of scope:** autonomous or automated discharge, triage,
  bed-allocation, or resource decisions; any use where the number is treated as
  ground truth; anything patient-facing. The model **does not** make decisions
  and must never be the sole basis for one.
- **Users:** Orb developers validating the pipeline. Not clinicians, not
  patients, until a real-data checkpoint replaces this one.

## Training data: SYNTHETIC v1

- Generated in-process by `train_forecast_synth.py` with
  `numpy.random.default_rng(seed)` (default seed `1234`), fully deterministic,
  no database or network access.
- 400 synthetic "patients" × 3 observation windows = 1,200 windows; each window
  is a `48 × 19` time-series matching the runtime feature schema (see below).
- Severity (`critical` / `elevated` / `stable`) drives log-normal length-of-stay
  and baseline vitals; vitals drift toward normal as the (synthetic) stay
  progresses, with per-timestep Gaussian noise and occasional crisis spikes for
  critical patients.
- **Label honesty:** discharge probability is deliberately **not** the exact
  analytic inverse of the LoS label, it carries independent noise plus a
  deterioration competing-risk term, so the discharge and LoS heads do not
  collapse to a single transform. Even so, every label is a function of the same
  latent severity, so the network can only recover the generator's own algebra.
  **This is a smoke-test distribution, not a validated corpus.**
- **Not** trained or validated on MIMIC-IV, eICU, HiRID, or any real EHR. No PHI
  is involved.

## Architecture

- `LayerNorm(19)` input calibration → `BiLSTM(2 layers, hidden 64, dropout 0.3)`
  → temporal attention (`tanh → softmax` over 48 timesteps) → 128-d context
  vector → four task heads.
- **Heads:** (1) LoS `Linear→Softplus` (remaining days); (2) discharge prob
  `Linear→Sigmoid`; (3) acuity `Linear→3 logits` (improving / stable /
  worsening); (4) ICU-transfer risk `Linear→Sigmoid`.
- ~160k parameters (~0.6 MB checkpoint). Trains on CPU in seconds.
- **Input schema (`INPUT_DIM=19`),** identical to
  `forecast_service.extract_patient_features()`:

  | idx | feature | idx | feature | idx | feature |
  |---|---|---|---|---|---|
  | 0 | heart_rate | 7 | sex==M | 14 | severity==elevated |
  | 1 | systolic_bp | 8 | sex==F | 15 | severity==stable |
  | 2 | diastolic_bp | 9 | ward==ICU | 16 | days_since_admit |
  | 3 | spo2 | 10 | ward==W1 | 17 | active_med_count |
  | 4 | temperature | 11 | ward==W2 | 18 | risk_score |
  | 5 | respiratory_rate | 12 | ward==W3 | | |
  | 6 | age | 13 | severity==critical | | |

## Fail-closed behavior (safety design)

- **No checkpoint → heuristic, not noise.** With no checkpoint on disk,
  `load_model()` returns an untrained network (`trained=False`) and
  `forecast_service.predict_patient_los()` routes to the transparent rule-based
  heuristic, tagging results `model_trained=false`,
  `prediction_source="heuristic"`. Random-init weights are never surfaced as
  predictions.
- **ICU-transfer head stays gated off.** This checkpoint sets
  `icu_head_trained=false` because the synthetic data carries **no** real
  ICU-transfer label. `load_model()` reads that flag and `predict()`
  **omits `icu_transfer_risk_3h` entirely**, a `sigmoid(random-init)` value is
  never exposed as a transfer probability.
- **Provenance travels with every prediction.** `predict()` returns
  `model_trained`; the service adds `prediction_source` (`"lstm"` vs
  `"heuristic"`). Downstream UI can distinguish neural output from fallback.

## Known limitations

- **Not clinically valid.** Trained on synthetic data only; no AUROC / AUPRC /
  MAE / calibration against real outcomes has been measured. Do not report
  metrics from this checkpoint as evidence of clinical performance.
- **Learns the generator, not medicine.** Because labels are deterministic
  functions of synthetic severity, the model can at best reproduce the
  simulator's rules.
- **Region calibration is baked in.** Vital-sign baselines follow the
  Indian-calibrated assumptions carried over from `train_forecast.py`; a
  multi-region product needs per-region calibration profiles (playbook §5).
- **No ICU-transfer capability** in this checkpoint (see fail-closed above).
- **Small and shallow by design**, sized for a smoke test, not tuned for
  accuracy.

## Path to a clinically usable checkpoint

Per [`ORB_TRAINING_PLAYBOOK.md`](./ORB_TRAINING_PLAYBOOK.md) §2.1, §4, §7:
1. Complete PhysioNet credentialing; obtain MIMIC-IV (+ MIMIC-IV-ED) and eICU.
2. Engineer real labels (LoS from admit→discharge; discharge-within-24h event;
   acuity from early-warning-score trajectory; ICU transfer within N hours to
   legitimately enable the ICU head).
3. Retrain, then **externally validate on eICU** (cross-site). Report AUROC /
   AUPRC / LoS MAE / calibration as engineering gates (§7), not clinical proof.
4. Issue a new model card per checkpoint recording data sources, date, metrics,
   and limits. Clinical claims additionally require validation + regulatory
   clearance (§8).

## Reproduce

```bash
cd server
. .venv-dev/bin/activate                       # or: . .venv/bin/activate
python scripts/train_forecast_synth.py         # deterministic; seed=1234
# -> writes server/models/checkpoints/forecast_lstm_synth_v1.pt
```

Confirm it loads and reports `trained=True`:

```bash
python -c "from models.forecast_lstm import load_model; \
m = load_model(checkpoint_path='models/checkpoints/forecast_lstm_synth_v1.pt'); \
assert m.trained is True and m.icu_head_trained is False; \
print('trained =', m.trained, '| icu_head_trained =', m.icu_head_trained)"
```

Reference run (seed `1234`, defaults): 1,200 windows, 8 epochs, **final train
loss ≈ 2.6247**, checkpoint ≈ 637 KB. The seed makes the loss reproducible
run-to-run.

## Checkpoint format

`torch.save` of a plain dict loaded by `load_model()` with `weights_only=True`
(so every value is a tensor or a JSON-safe scalar):

| key | meaning |
|---|---|
| `model_state_dict` | network weights (**required** by `load_model`) |
| `input_dim` / `hidden_dim` / `num_layers` | architecture (`load_model` honors `input_dim`) |
| `icu_head_trained` | `false` here, gates the ICU-transfer head off |
| `data_source` | `"synthetic_v1"` (provenance) |
| `epoch`, `final_train_loss`, `seed`, `training_date`, `provenance` | metadata / logging |
