"""Orb Forecast LSTM — reproducible SYNTHETIC training + checkpoint export harness.

⚠️  HONESTY NOTICE
    This trains the multi-task Forecast LSTM on a *self-contained, deterministic,
    fully SYNTHETIC* dataset. The resulting checkpoint is a real (non-random-init)
    set of weights, but it is NOT trained or validated on any real clinical data.
    Its only jobs are (a) to un-block the plumbing so `load_model()` reports
    `trained=True` and the service exercises the neural path end-to-end, and
    (b) to give us a reproducible harness we later re-point at MIMIC-IV / eICU.

    DO NOT present its numbers as clinical predictions. See:
      - docs/MODEL_CARD_forecast_lstm.md   (honest model card for this checkpoint)
      - docs/MODEL_TRAINING.md             (how real training/export works)
      - docs/ORB_TRAINING_PLAYBOOK.md      (the real-data critical path)

Why a *separate* script from scripts/train_forecast.py?
    train_forecast.py writes models/forecast_checkpoint.pt (the path load_model()
    picks up by default) and also mutates orb.db (admission_history). This harness
    is deliberately narrower: no DB writes, one deterministic seed, and it exports
    to a *named, versioned* file (models/checkpoints/forecast_lstm_synth_v1.pt) so
    the provenance ("synthetic v1") is legible from the filename and the checkpoint
    metadata. It does not import from train_forecast.py so it stays self-contained.

Checkpoint format (must match exactly what models.forecast_lstm.load_model reads):
    torch.save(dict) loaded with weights_only=True, so every value is either a
    tensor (state_dict) or a JSON-safe scalar. load_model() reads:
        ckpt["model_state_dict"]          -> load_state_dict (REQUIRED)
        ckpt.get("input_dim", INPUT_DIM)  -> constructs the network
        ckpt.get("icu_head_trained")      -> gates the ICU-transfer head
        ckpt.get("epoch"), ("data_source")-> logged only
    On success load_model() sets model.trained = True unconditionally.

    icu_head_trained is left FALSE: this harness supplies no real ICU-transfer
    label, so the ICU head is meaningless and predict() correctly omits it.

Reproduce:
    cd server
    . .venv-dev/bin/activate            # or: .venv/bin/activate
    python scripts/train_forecast_synth.py            # defaults: seed=1234
    # -> writes server/models/checkpoints/forecast_lstm_synth_v1.pt
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import torch
import torch.nn as nn
import torch.optim as optim

# Import the network + schema constants from the (unmodified) model module.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from models.forecast_lstm import (  # noqa: E402
    OrbForecastLSTM,
    INPUT_DIM,
    HIDDEN_DIM,
    NUM_LAYERS,
)

# Default export target. Both this dir (server/models/*/) and *.pt are gitignored,
# so the weights are produced locally and never committed — the HARNESS is the
# deliverable, not the .pt file.
DEFAULT_CKPT = (
    Path(__file__).resolve().parent.parent
    / "models"
    / "checkpoints"
    / "forecast_lstm_synth_v1.pt"
)
DATA_SOURCE = "synthetic_v1"       # legible provenance tag stored in the checkpoint
SEQ_LEN = 48                       # 48 steps == same window the service extracts

# Acuity classes: 0 improving, 1 stable, 2 worsening (matches ACUITY_LABELS).
_SEVERITIES = ("critical", "elevated", "stable")


# ══════════════════════════════════════════════════════════════════════
# SYNTHETIC DATA GENERATION  (deterministic via numpy default_rng)
# ══════════════════════════════════════════════════════════════════════
#
# The feature vector is the SAME 19-channel schema the runtime builds in
# forecast_service.extract_patient_features(), so a checkpoint trained here is
# dimensionally compatible with live inference:
#
#   0 heart_rate     6 age            9  ward==ICU     13 sev==critical  16 days_since_admit
#   1 systolic_bp    7 sex==M         10 ward==W1      14 sev==elevated  17 active_med_count
#   2 diastolic_bp   8 sex==F         11 ward==W2      15 sev==stable    18 risk_score
#   3 spo2                            12 ward==W3
#   4 temperature
#   5 respiratory_rate
#
# Every label is a (noisy) function of severity + how far through the stay we
# are. This is a SMOKE-TEST distribution, not clinical truth — the net can only
# recover the generator's algebra. Discharge prob is deliberately NOT the exact
# analytic inverse of LoS (independent noise + a deterioration competing-risk
# term) so the two heads don't collapse to a single transform.

# Per-severity length-of-stay (log-normal params, days) and baseline vitals.
_LOS_PARAMS = {
    "critical": dict(mu=2.0, sigma=0.45, hr=118.0, sbp=88.0, spo2=90.0, temp=38.8, rr=26.0),
    "elevated": dict(mu=1.4, sigma=0.35, hr=96.0, sbp=106.0, spo2=93.5, temp=37.9, rr=22.0),
    "stable":   dict(mu=0.7, sigma=0.25, hr=80.0, sbp=120.0, spo2=97.0, temp=36.8, rr=16.0),
}
_WARD_WEIGHTS = {
    # (ICU, W1, W2, W3) sampling weights per severity
    "critical": np.array([0.80, 0.20, 0.0, 0.0]),
    "elevated": np.array([0.0, 0.30, 0.30, 0.40]),
    "stable":   np.array([0.0, 0.20, 0.40, 0.40]),
}
_WARD_ORDER = ("ICU", "W1", "W2", "W3")
_MED_RANGE = {"critical": (6, 12), "elevated": (3, 8), "stable": (1, 5)}
_RISK_SCORE = {"critical": 0.78, "elevated": 0.52, "stable": 0.25}


def _sample_age(rng: np.random.Generator) -> float:
    """Age skewed toward older inpatients (mixture of normals, clipped)."""
    if rng.random() < 0.65:
        return float(np.clip(rng.normal(62, 12), 18, 95))
    return float(np.clip(rng.normal(40, 12), 18, 95))


def _make_patient_window(rng: np.random.Generator) -> tuple[np.ndarray, dict]:
    """One observation window: (SEQ_LEN, INPUT_DIM) features + label dict."""
    severity = _SEVERITIES[int(rng.choice(3, p=[0.25, 0.30, 0.45]))]
    p = _LOS_PARAMS[severity]

    true_los = float(max(1.0, rng.lognormal(p["mu"], p["sigma"])))
    # Observation point somewhere inside the stay.
    current_day = float(rng.uniform(0.3, max(0.6, true_los - 0.4)))
    remaining_los = max(0.1, true_los - current_day)

    # Recovery signal: fraction of the stay elapsed (critical patients recover
    # slower, so their improvement is damped).
    improvement = min(1.0, current_day / true_los)
    if severity == "critical":
        improvement *= 0.4

    sex_m = rng.random() < 0.52
    age = _sample_age(rng)
    ward_idx = int(rng.choice(4, p=_WARD_WEIGHTS[severity]))
    ward = _WARD_ORDER[ward_idx]
    med_count = float(rng.integers(*_MED_RANGE[severity]))

    feats = np.zeros((SEQ_LEN, INPUT_DIM), dtype=np.float32)
    for t in range(SEQ_LEN):
        # Vitals drift from the acute baseline toward normal as the patient
        # improves, plus per-timestep gaussian noise.
        feats[t, 0] = p["hr"] + (78.0 - p["hr"]) * improvement * 0.5 + rng.normal(0, 4.0)
        feats[t, 1] = p["sbp"] + (120.0 - p["sbp"]) * improvement * 0.4 + rng.normal(0, 5.0)
        feats[t, 2] = feats[t, 1] * 0.6 + rng.normal(0, 3.0)
        feats[t, 3] = min(100.0, p["spo2"] + (98.0 - p["spo2"]) * improvement * 0.3 + rng.normal(0, 1.0))
        feats[t, 4] = p["temp"] + (36.8 - p["temp"]) * improvement * 0.5 + rng.normal(0, 0.15)
        feats[t, 5] = max(8.0, p["rr"] + (16.0 - p["rr"]) * improvement * 0.4 + rng.normal(0, 1.5))

        feats[t, 6] = age
        feats[t, 7] = 1.0 if sex_m else 0.0
        feats[t, 8] = 0.0 if sex_m else 1.0
        feats[t, 9] = 1.0 if ward == "ICU" else 0.0
        feats[t, 10] = 1.0 if ward == "W1" else 0.0
        feats[t, 11] = 1.0 if ward == "W2" else 0.0
        feats[t, 12] = 1.0 if ward == "W3" else 0.0
        feats[t, 13] = 1.0 if severity == "critical" else 0.0
        feats[t, 14] = 1.0 if severity == "elevated" else 0.0
        feats[t, 15] = 1.0 if severity == "stable" else 0.0
        feats[t, 16] = current_day
        feats[t, 17] = med_count
        feats[t, 18] = _RISK_SCORE[severity]

        # Occasional acute crisis spike for critical patients.
        if severity == "critical" and rng.random() < 0.05:
            feats[t, 0] += rng.uniform(10, 25)
            feats[t, 3] -= rng.uniform(2, 6)
            feats[t, 1] -= rng.uniform(10, 20)

    # ── Labels ──
    # Discharge: base rate falls with remaining LoS, damped hard for
    # deteriorating critical patients (competing risk), plus independent noise
    # so it is not a pure algebraic inverse of the LoS label.
    base_discharge = 1.0 / (remaining_los + 0.5)
    if severity == "critical" and improvement < 0.2:
        base_discharge *= 0.3
    discharge_prob = float(np.clip(base_discharge + rng.normal(0, 0.08), 0.0, 0.95))

    if improvement > 0.5:
        acuity = 0  # improving
    elif severity == "critical" and improvement < 0.2:
        acuity = 2  # worsening
    else:
        acuity = 1  # stable

    return feats, {"los": remaining_los, "discharge_prob": discharge_prob, "acuity": acuity}


def build_dataset(rng: np.random.Generator, n_patients: int, n_obs: int):
    """Return stacked tensors (X, los, discharge, acuity) for all windows."""
    X, los, dis, acu = [], [], [], []
    for _ in range(n_patients):
        for _ in range(n_obs):
            feats, labels = _make_patient_window(rng)
            X.append(feats)
            los.append(labels["los"])
            dis.append(labels["discharge_prob"])
            acu.append(labels["acuity"])
    X_t = torch.from_numpy(np.stack(X)).float()
    los_t = torch.tensor(los, dtype=torch.float32).unsqueeze(1)
    dis_t = torch.tensor(dis, dtype=torch.float32).unsqueeze(1)
    acu_t = torch.tensor(acu, dtype=torch.long)
    return X_t, los_t, dis_t, acu_t


# ══════════════════════════════════════════════════════════════════════
# TRAINING
# ══════════════════════════════════════════════════════════════════════

def train(args) -> float:
    device = torch.device("cpu")  # tiny model — CPU is the reproducible target
    rng = np.random.default_rng(args.seed)
    torch.manual_seed(args.seed)  # weight init + dropout determinism

    print("=" * 66)
    print("  Orb Forecast LSTM — SYNTHETIC v1 training harness")
    print("  (synthetic data only — NOT validated on real clinical data)")
    print("=" * 66)
    print(f"→ seed={args.seed}  device={device}  seq_len={SEQ_LEN}  input_dim={INPUT_DIM}")

    X, los_t, dis_t, acu_t = build_dataset(rng, args.patients, args.obs_per_patient)
    n = X.shape[0]
    print(f"→ dataset: {n} windows from {args.patients} synthetic patients "
          f"({args.obs_per_patient} obs each)")

    model = OrbForecastLSTM(input_dim=INPUT_DIM).to(device)
    n_params = sum(p.numel() for p in model.parameters())
    print(f"→ model: OrbForecastLSTM(input_dim={INPUT_DIM}, hidden={HIDDEN_DIM}, "
          f"layers={NUM_LAYERS}) — {n_params:,} params")

    los_criterion = nn.MSELoss()
    dis_criterion = nn.BCELoss()
    acu_criterion = nn.CrossEntropyLoss()
    alpha, beta, gamma = 1.0, 0.5, 0.3  # LoS / discharge / acuity task weights

    optimizer = optim.AdamW(model.parameters(), lr=args.lr, weight_decay=1e-4)
    scheduler = optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=args.epochs)

    # Deterministic mini-batch order per epoch from the same rng.
    batch = args.batch_size
    print(f"→ training: epochs={args.epochs} batch={batch} lr={args.lr} "
          f"(weights LoS={alpha} Dis={beta} Acu={gamma})\n")

    final_avg = float("nan")
    for epoch in range(args.epochs):
        model.train()
        perm = torch.from_numpy(rng.permutation(n))
        running = los_run = dis_run = acu_run = 0.0
        n_batches = 0
        for start in range(0, n - batch + 1, batch):
            idx = perm[start:start + batch]
            xb, lb, db_, ab = X[idx], los_t[idx], dis_t[idx], acu_t[idx]

            optimizer.zero_grad()
            out = model(xb)
            l_los = los_criterion(out["los"], lb)
            l_dis = dis_criterion(out["discharge_prob"], db_)
            l_acu = acu_criterion(out["acuity_logits"], ab)
            loss = alpha * l_los + beta * l_dis + gamma * l_acu
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), max_norm=1.0)
            optimizer.step()

            running += loss.item()
            los_run += l_los.item()
            dis_run += l_dis.item()
            acu_run += l_acu.item()
            n_batches += 1

        scheduler.step()
        final_avg = running / max(1, n_batches)
        print(f"  epoch {epoch + 1:2d}/{args.epochs} │ train {final_avg:.4f} "
              f"(LoS {los_run / n_batches:.3f}  Dis {dis_run / n_batches:.3f}  "
              f"Acu {acu_run / n_batches:.3f})")

    print(f"\n✓ Final train loss: {final_avg:.4f}")

    # ── Export in the EXACT format load_model() consumes ──
    ckpt_path = Path(args.out)
    ckpt_path.parent.mkdir(parents=True, exist_ok=True)
    checkpoint = {
        "model_state_dict": model.state_dict(),
        "input_dim": INPUT_DIM,
        "hidden_dim": HIDDEN_DIM,
        "num_layers": NUM_LAYERS,
        "epoch": args.epochs,
        "final_train_loss": round(float(final_avg), 6),
        "data_source": DATA_SOURCE,
        # No real ICU-transfer label was trained here, so the ICU head stays gated
        # off. load_model() reads this and predict() omits icu_transfer_risk_3h.
        "icu_head_trained": False,
        "seed": args.seed,
        "training_date": datetime.now(timezone.utc).isoformat(),
        "provenance": (
            "SYNTHETIC v1, deterministic self-contained generator; "
            "NOT trained or validated on real clinical data; demo/plumbing only. "
            "Retrain on MIMIC-IV + external-validate on eICU before any clinical use "
            "(see docs/ORB_TRAINING_PLAYBOOK.md)."
        ),
    }
    torch.save(checkpoint, str(ckpt_path))
    size_kb = ckpt_path.stat().st_size / 1024
    print(f"✓ Checkpoint written: {ckpt_path}  ({size_kb:.1f} KB)")
    print("  provenance: synthetic_v1 — NOT for clinical use")
    print(f"\nTo activate as the runtime model, load it explicitly, e.g.:")
    print(f"  load_model(checkpoint_path='{ckpt_path}')  # .trained == True")
    print("  (or copy/symlink to models/forecast_checkpoint.pt for the default path)")
    return final_avg


def parse_args():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--seed", type=int, default=1234, help="deterministic RNG seed")
    ap.add_argument("--epochs", type=int, default=8, help="training epochs (CPU)")
    ap.add_argument("--patients", type=int, default=400, help="synthetic patients")
    ap.add_argument("--obs-per-patient", type=int, default=3, help="windows per patient")
    ap.add_argument("--batch-size", type=int, default=32)
    ap.add_argument("--lr", type=float, default=1e-3)
    ap.add_argument("--out", type=str, default=str(DEFAULT_CKPT),
                    help="checkpoint output path (.pt)")
    return ap.parse_args()


if __name__ == "__main__":
    train(parse_args())
