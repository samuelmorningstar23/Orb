# Model Training: Orb Forecast LSTM

How to train and export the Forecast LSTM, and how a produced checkpoint gets
picked up at runtime. For the real-data critical path (datasets, credentialing,
evaluation gates, governance) see
[`ORB_TRAINING_PLAYBOOK.md`](./ORB_TRAINING_PLAYBOOK.md). For the current
synthetic checkpoint's provenance and limits see
[`MODEL_CARD_forecast_lstm.md`](./MODEL_CARD_forecast_lstm.md).

---

## TL;DR

- The Forecast LSTM is tiny (~160k params, ~0.6 MB) and **trains on CPU in
  seconds**, no GPU needed (playbook §1: "CfC / LSTM: tiny, train directly on
  the Mac or Kaggle CPU").
- Two training entry points, both writing a checkpoint in the **same format**
  `models/forecast_lstm.load_model()` reads:
  - `server/scripts/train_forecast_synth.py`, reproducible, DB-free, **synthetic
    v1** smoke-test harness → `models/checkpoints/forecast_lstm_synth_v1.pt`.
  - `server/scripts/train_forecast.py`, the fuller Indian-calibrated synthetic
    pipeline that also seeds `admission_history` in `orb.db` → writes the default
    runtime path `models/forecast_checkpoint.pt`.
- **Checkpoints are never committed.** `*.pt`, `server/models/*/`, and `*.db`
  are all in `.gitignore`. You regenerate weights locally; the scripts + docs
  are the deliverable.

---

## Environment

```bash
cd server
. .venv-dev/bin/activate        # dev venv: torch, numpy, pytest
#   or the runtime venv:  . .venv/bin/activate
```

Both scripts import `OrbForecastLSTM`, `INPUT_DIM`, `HIDDEN_DIM`, `NUM_LAYERS`
from `models/forecast_lstm.py` and add the `server/` dir to `sys.path`, so run
them from `server/`.

---

## Option A: reproducible synthetic harness (recommended for plumbing)

`scripts/train_forecast_synth.py` generates a deterministic synthetic dataset
(`numpy.random.default_rng(seed)`), trains a few CPU epochs, and exports a
**named, versioned** checkpoint carrying `data_source="synthetic_v1"`.

```bash
python scripts/train_forecast_synth.py                 # defaults: seed=1234, 8 epochs
# tunables:
python scripts/train_forecast_synth.py --seed 7 --epochs 12 --patients 600 \
       --out models/checkpoints/forecast_lstm_synth_v2.pt
```

- Writes `server/models/checkpoints/forecast_lstm_synth_v1.pt` (~637 KB).
- Prints the final train loss (reference: **≈ 2.6247** at seed `1234`, defaults).
- Does **not** touch `orb.db` and makes no network calls.
- Sets `icu_head_trained=False` (no real ICU-transfer label), so the runtime's
  ICU-transfer head stays gated off, see the model card's fail-closed section.

**This produces demo/plumbing weights only, NOT clinically valid.** Read
[`MODEL_CARD_forecast_lstm.md`](./MODEL_CARD_forecast_lstm.md) before relying on
any output.

## Option B: fuller synthetic pipeline + DB seeding

`scripts/train_forecast.py` trains on a larger Indian-calibrated synthetic
corpus, runs an inference smoke test, and populates 30 days of
`admission_history` in `orb.db` (used by the hospital-flow forecast).

```bash
python scripts/train_forecast.py
# -> writes models/forecast_checkpoint.pt   (the DEFAULT runtime path)
# -> populates the admission_history table in orb.db
```

Use this when you also want the admission-history data seeded. It writes to the
default checkpoint path, so the runtime picks it up automatically (below).

## Option C: real data (production path: not yet implemented here)

Follow [`ORB_TRAINING_PLAYBOOK.md`](./ORB_TRAINING_PLAYBOOK.md):
1. **§4 PhysioNet credentialing** (1–2 week lead time) → gates MIMIC-IV + eICU.
2. **§2.1 label engineering** on real EHR: LoS from admit→discharge; discharge
   -within-24h event; acuity from early-warning-score trajectory; ICU transfer
   within N hours (only then may `icu_head_trained=True` be set honestly).
3. Train (CPU/MPS or Kaggle CPU, the net is tiny), then **§7 evaluate**:
   AUROC / AUPRC for deterioration, **MAE for LoS**, calibration plots.
4. **External-validate on eICU** before any rollout (§5). Emit a fresh model
   card per checkpoint (§7). Governance: real data never touches git or any
   uncontrolled bucket (§8).

Any real-data trainer should reuse the **same 19-channel feature schema and
checkpoint format** as the scripts above so `load_model()` consumes it unchanged.

---

## CPU-train → export → runtime pickup

### Export format (what every trainer must write)

`load_model()` calls `torch.load(path, weights_only=True)`, so a checkpoint is a
plain dict of a tensor state-dict plus JSON-safe scalars:

```python
torch.save({
    "model_state_dict": model.state_dict(),  # REQUIRED, load_state_dict target
    "input_dim": INPUT_DIM,                   # load_model honors this (extended schemas)
    "hidden_dim": HIDDEN_DIM,
    "num_layers": NUM_LAYERS,
    "icu_head_trained": False,                # True ONLY if a real ICU-transfer label was used
    "data_source": "synthetic_v1",            # provenance, logged on load
    "epoch": epochs,
    "final_train_loss": loss,                 # metadata
}, checkpoint_path)
```

Keep values to tensors / `int` / `float` / `str` / `bool`, `weights_only=True`
rejects arbitrary pickled objects on load.

### How a checkpoint becomes the runtime model

`load_model(checkpoint_path=None)` in `models/forecast_lstm.py`:

1. **Path.** With no argument it looks at the default
   `server/models/forecast_checkpoint.pt`. Pass `checkpoint_path=...` to load a
   specific file (e.g. the synth checkpoint).
2. **Present →** loads `model_state_dict`, honors the checkpoint's `input_dim`,
   sets `model.trained = True`, and sets `model.icu_head_trained` from the flag.
3. **Absent / load error →** returns an **untrained** model (`trained=False`)
   with a warning. `forecast_service.predict_patient_los()` then routes to the
   rule-based heuristic (`prediction_source="heuristic"`, `model_trained=false`)
   random weights are never surfaced as predictions.

The service reaches the model via `forecast_service.get_model()` →
`load_model()` (no argument), so it reads the **default** path. To make a
freshly trained checkpoint the live runtime model, put it at the default path:

```bash
# Option A produces a named synth checkpoint; activate it as the runtime model by
# copying (or symlinking) it to the default path get_model() reads:
cp models/checkpoints/forecast_lstm_synth_v1.pt models/forecast_checkpoint.pt
# (Option B / a real-data trainer that writes forecast_checkpoint.pt directly
#  needs no copy.)
```

Then restart the server (the model is lazy-loaded and cached as a singleton in
`forecast_service._model`, so a running process won't hot-reload a new file).

### Verify a checkpoint loads and reports trained=True

```bash
cd server && . .venv-dev/bin/activate
python -c "from models.forecast_lstm import load_model; \
m = load_model(checkpoint_path='models/checkpoints/forecast_lstm_synth_v1.pt'); \
assert m.trained is True; print('trained =', m.trained, \
'| icu_head_trained =', m.icu_head_trained)"
```
