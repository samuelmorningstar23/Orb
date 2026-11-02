# Offline labelled extracts for Vigil CfC

`scripts/train_vigil_cfc.py` now accepts local, already-labelled JSONL windows for
`deterioration` and `mortality`. It does not obtain credentials, download a dataset,
query Orb's database, decide clinical outcome definitions, or promote a model into
service. The existing PhysioNet sepsis adapter remains separate.

## Input contract

Prepare an extract using data you are authorized to use. The data owner must
establish and document the outcome definition, observation availability, censoring
rules, pseudonymization, and permitted use before exporting. `label_definition` is
an opaque versioned reference to that documentation, not free text or a diagnosis.

Each UTF-8 JSONL line contains one prediction window:

| Field | Contract |
| --- | --- |
| `schema_version` | Integer `1` |
| `outcome` | Exactly the requested `deterioration` or `mortality` |
| `subject_id` | Stable pseudonymous subject identifier across all encounters; never a name/MRN |
| `encounter_id` | Pseudonymous encounter identifier, associated with exactly one subject |
| `encounter_started_at` | Timezone-aware ISO time; observations cannot precede it |
| `prediction_at` | Timezone-aware ISO time; no feature may be observed or available after it |
| `age`, `age_available_at` | Optional finite numeric age and its known-at time; supplying age requires availability at or before prediction |
| `observations` | Three to 24 entries, strictly increasing in `observed_at` |
| `labels` | Exactly `1h`, `3h`, `6h`, `12h`, `24h`, each integer `0` or `1`, cumulative in horizon order |
| `labels_complete` | Boolean `true`; all horizons must be ascertained under the declared definition |
| `data_source`, `label_definition` | Consistent opaque identifiers throughout one extract |

Identifiers use ASCII letters/numbers plus `_`, `.`, `:`, or `-`, with a maximum
of 128 characters. Lines are limited to 64 KiB and extracts to 250,000 windows.
Unknown fields, duplicate prediction windows, non-finite numbers, boolean numeric
values, mixed definitions, and inconsistent encounter ownership are rejected.
A censored or unknown outcome must not be supplied as a negative label. The
adapter checks the declared schema; it cannot establish that an owner's clinical
labels or availability timestamps are scientifically correct.

Each observation requires `observed_at` and `available_at`. It may include only
`heart_rate`, `spo2`, `temperature`, `systolic_bp`, `diastolic_bp`,
`respiratory_rate`, and `consciousness`. Availability cannot precede observation
or follow prediction. The consciousness vocabulary matches the current live
assembler: `alert`/`a`, `verbal`/`v`, `pain`/`p`, and
`confusion`/`c`/`new_confusion`/`unresponsive`/`u` (case-insensitive).

This example is entirely synthetic; its labels demonstrate the format only:

```json
{"schema_version":1,"outcome":"mortality","subject_id":"synthetic-001","encounter_id":"synthetic-stay-001","encounter_started_at":"2026-10-11T00:00:00Z","prediction_at":"2026-10-11T03:00:00Z","age":50,"age_available_at":"2026-10-11T00:00:00Z","observations":[{"observed_at":"2026-10-11T00:00:00Z","available_at":"2026-10-11T00:05:00Z","heart_rate":80,"consciousness":"alert"},{"observed_at":"2026-10-11T01:00:00Z","available_at":"2026-10-11T01:05:00Z","heart_rate":82},{"observed_at":"2026-10-11T02:30:00Z","available_at":"2026-10-11T02:35:00Z","heart_rate":81}],"labels":{"1h":0,"3h":0,"6h":0,"12h":0,"24h":1},"labels_complete":true,"data_source":"synthetic-only-v1","label_definition":"example-authored-label-v1"}
```

At least two distinct subjects are required. All windows and encounters for one
subject stay together in the deterministic 85%/15% subject split (seed 42).
Normalization mean/std and class weights use the training partition only.
Validation subjects never contribute to those statistics. The existing trainer
requires at least 100 total windows before actual training.

## Feature and sequence parity

The adapter emits the production nine-feature order: heart rate, SpO₂,
temperature, systolic BP, derived MAP, diastolic BP, respiratory rate, AVPU, age.
MAP is `DBP + (SBP - DBP) / 3`, using the same per-observation defaults as the live
assembler. Missing values are not filled from future or earlier observations.
Missing consciousness maps to alert and missing age to 62, matching current
production behavior; those defaults require the same clinical validation as the
live model. Measured MAP and additional laboratory/outcome columns are rejected.

The first elapsed-time value is zero; later values are actual positive elapsed
hours. Windows contain at most the 24 observations accepted by the live assembler.
Equal-length batches preserve every sample without padding: the current CfC
reads the final recurrent state and has no padding mask. Padding shorter windows
would change the modeled sequence. None of this validates a clinical endpoint.

The legacy PhysioNet sepsis path still has its documented measured-MAP,
imputation, window-level split, and corpus-normalization limitations. This new
adapter does not silently claim to repair that separate training path.

## Validate and train

From `server`, using the environment with the repository's training dependencies:

```bash
python scripts/train_vigil_cfc.py --outcome mortality \
  --labelled-extract /approved/local/extract.jsonl --validate-only

python scripts/train_vigil_cfc.py --outcome deterioration \
  --labelled-extract /approved/local/extract.jsonl \
  --output /approved/research/artifacts/vigil-deterioration-experiment.pt
```

Validation prints window counts only and writes no checkpoint. Input errors
identify line numbers and fixed field names, without printing submitted values,
subject identifiers, or input paths. Training requires a new explicit output
artifact outside `server/models`, so it does not overwrite a live model or an
existing research artifact. The checkpoint records source/definition identifiers,
extract SHA-256, schema version, split seed/counts and `clinical_validation:
not_assessed`; it does not include source paths or patient/window identifiers.

Clinical label acceptance, calibration, discrimination against NEWS2, external
validation, deployment authorization, and live threshold selection remain
separate work. Only synthetic contract, leakage, batching, feature parity and
safe-loader tests were run for this implementation; no model was trained.
