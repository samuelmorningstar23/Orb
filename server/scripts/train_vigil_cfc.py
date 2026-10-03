"""Orb Vigil — CfC trainer (multi-horizon, per-outcome).

Trains ONE OrbVigilCfC per clinical outcome to predict the risk of that event
within each horizon in models.vigil_cfc.HORIZONS_HOURS (1/3/6/12/24h). Each
outcome is an independent model on its own dataset:

    sepsis        -> PhysioNet/CinC 2019 Sepsis Challenge   (OPEN, runnable now)
    deterioration -> ICU transfer / rapid-response          (needs MIMIC-IV/eICU*)
    mortality     -> in-hospital death                      (needs MIMIC-IV/eICU*)

  * MIMIC-IV / eICU are CREDENTIALED and licensed "scientific research only" —
    fine for R&D/validation, NOT for commercial deployment. See
    docs/ORB_TRAINING_PLAYBOOK.md and the data-sourcing report.

Features use the canonical PRODUCTION schema (vitals + AVPU + age) so a trained
checkpoint runs on live data. PhysioNet has no consciousness field, so AVPU is
imputed as 0 (alert); labs are intentionally excluded from v1 so the model is
live-runnable (case-specific labs are a planned extended schema).

Usage:
  cd server && .venv/bin/python scripts/train_vigil_cfc.py --outcome sepsis
"""
import sys
import argparse
import random
from pathlib import Path

import numpy as np
import torch
import torch.nn as nn
from torch.utils.data import Dataset, DataLoader

SERVER_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVER_DIR))

from models.vigil_cfc import (
    OrbVigilCfC, HORIZONS_HOURS,
    PRODUCTION_FEATURE_ORDER, PRODUCTION_FEATURE_MEAN, PRODUCTION_FEATURE_STD,
)

# ── Config ─────────────────────────────────────────────────────────────────────
HIDDEN_DIM = 64
MAX_SEQ_LEN = 48           # cap on observation window (hours)
MIN_OBS = 3                # need at least this many observations to predict
MAX_WINDOWS_PER_PATIENT = 6
BATCH_SIZE = 64
EPOCHS = 30
LR = 1e-3
TRAIN_SPLIT = 0.85
SEED = 42

random.seed(SEED); np.random.seed(SEED); torch.manual_seed(SEED)

# PhysioNet column -> production feature (avpu/age handled specially).
# TODO (train/serve skew, deferred — needs vigil_service.py change, other cluster):
# PhysioNet provides MAP directly, but production (services/vigil_service.py)
# derives MAP = DBP + (SBP-DBP)/3. Pick ONE definition across train + serve
# (either always measured or always derived) or this channel is skewed at inference.
_PSV_VITALS = {
    "heart_rate": "HR", "spo2": "O2Sat", "temperature": "Temp",
    "systolic_bp": "SBP", "map": "MAP", "diastolic_bp": "DBP",
    "respiratory_rate": "Resp",
}
_DEFAULTS = {"heart_rate": 80.0, "spo2": 97.0, "temperature": 36.8, "systolic_bp": 120.0,
             "map": 82.0, "diastolic_bp": 63.0, "respiratory_rate": 18.0, "avpu": 0.0, "age": 62.0}


# ── PhysioNet sepsis adapter ───────────────────────────────────────────────────
def parse_psv(filepath: Path):
    """Parse one PhysioNet .psv -> (per-hour feature dicts, onset_idx or None)."""
    try:
        lines = filepath.read_text().splitlines()
    except Exception:
        return None
    if len(lines) < 2:
        return None
    header = lines[0].split("|")
    idx = {c: header.index(c) for c in header}
    if "SepsisLabel" not in idx:
        return None

    rows, onset_idx = [], None
    for t, line in enumerate(lines[1:]):
        f = line.split("|")
        if len(f) <= max(idx.values()):
            continue

        def val(col):
            if col not in idx:
                return None
            s = f[idx[col]]
            if s in ("", "NaN"):
                return None
            try:
                return float(s)
            except ValueError:
                return None

        row = {}
        for feat, psv_col in _PSV_VITALS.items():
            row[feat] = val(psv_col)
        row["age"] = val("Age")
        row["avpu"] = 0.0  # PhysioNet has no consciousness field -> assume alert
        rows.append(row)

        if onset_idx is None and (val("SepsisLabel") or 0) >= 1:
            onset_idx = t

    return (rows, onset_idx) if len(rows) >= MIN_OBS else None


def make_windows(parsed):
    """Build (raw_features[T,F], dt[T,1], labels[H]) samples from one patient.

    For sepsis we only emit PRE-onset windows (early-warning framing); a horizon
    label is 1 if onset falls within that many hours after the window end.

    Features here are RAW (forward-fill + default impute only). Normalization is
    applied corpus-wide in load_sepsis_dataset() using stats computed from the
    actual training data — NOT the hardcoded PRODUCTION_FEATURE_MEAN/STD guesses —
    and those computed stats are what gets stored in the checkpoint so train/serve
    normalization stays consistent.
    """
    rows, onset_idx = parsed
    n = len(rows)
    # forward-fill + default impute per production schema (no normalization yet)
    filled, prev = [], dict(_DEFAULTS)
    for r in rows:
        cur = {}
        for feat in PRODUCTION_FEATURE_ORDER:
            v = r.get(feat)
            if v is None:
                v = prev[feat]
            else:
                prev[feat] = v
            cur[feat] = v
        filled.append(cur)

    samples = []
    last_pred = onset_idx if onset_idx is not None else n
    pred_points = list(range(MIN_OBS, min(last_pred, n)))
    if len(pred_points) > MAX_WINDOWS_PER_PATIENT:
        pred_points = sorted(random.sample(pred_points, MAX_WINDOWS_PER_PATIENT))

    for t in pred_points:
        window = filled[max(0, t - MAX_SEQ_LEN):t]
        if len(window) < MIN_OBS:
            continue
        feats = [[row[feat] for feat in PRODUCTION_FEATURE_ORDER] for row in window]
        dt = [[1.0]] * len(window)  # PhysioNet is hourly -> Δt = 1h
        labels = [1.0 if (onset_idx is not None and 0 <= onset_idx - t <= h) else 0.0
                  for h in HORIZONS_HOURS]
        samples.append((torch.tensor(feats, dtype=torch.float32),
                        torch.tensor(dt, dtype=torch.float32),
                        torch.tensor(labels)))
    return samples


def _compute_corpus_stats(samples):
    """Per-feature mean/std over all observed timesteps in the corpus.

    Replaces the hardcoded PRODUCTION_FEATURE_MEAN/STD: the trainer must normalize
    by the real training distribution and persist exactly those stats so inference
    uses the same calibration (avoids silent train/serve skew).
    """
    stacked = torch.cat([f.reshape(-1, f.shape[-1]) for f, _, _ in samples], dim=0)
    mean = stacked.mean(0)
    std = stacked.std(0).clamp(min=1e-3)   # guard against constant channels
    return mean, std


def load_sepsis_dataset():
    data_dir = SERVER_DIR / "datasets" / "physionet_2019"
    psv_files = sorted(data_dir.glob("*.psv"))
    if not psv_files:
        print(f"❌ No .psv files in {data_dir}. Run scripts/download_physionet.py first.")
        sys.exit(1)
    print(f"[Data] {len(psv_files)} PhysioNet patient files")
    samples = []
    for fp in psv_files:
        parsed = parse_psv(fp)
        if parsed:
            samples.extend(make_windows(parsed))
    if not samples:
        print("❌ No windows produced from corpus."); sys.exit(1)

    # Compute normalization from the real corpus, then z-score every window.
    mean, std = _compute_corpus_stats(samples)
    norm_samples = [((f - mean) / std, d, y) for f, d, y in samples]

    pos = sum(int(s[2].max() > 0.5) for s in norm_samples)
    print(f"[Data] {len(norm_samples)} windows | {pos} with a positive horizon "
          f"({100*pos/max(1,len(norm_samples)):.1f}%)")
    return norm_samples, mean, std


def collate(batch):
    feats, dts, labels = zip(*batch)
    max_len = max(f.shape[0] for f in feats)
    F = feats[0].shape[1]
    vp = torch.zeros(len(batch), max_len, F)
    dp = torch.ones(len(batch), max_len, 1)
    for i, (f, d) in enumerate(zip(feats, dts)):
        vp[i, :f.shape[0]] = f
        dp[i, :d.shape[0]] = d
    return vp, dp, torch.stack(labels)


# ── Outcome registry ───────────────────────────────────────────────────────────
def load_dataset_for(outcome: str):
    """Returns (samples, feature_mean, feature_std)."""
    if outcome == "sepsis":
        return load_sepsis_dataset()
    raise NotImplementedError(
        f"Outcome '{outcome}' needs timestamped {outcome} labels from a credentialed "
        "ICU dataset (MIMIC-IV / eICU / AmsterdamUMCdb). Build that adapter once data "
        "access clears. See docs/ORB_TRAINING_PLAYBOOK.md.")


# ── Validation metrics ───────────────────────────────────────────────────────────
def _auroc(probs: np.ndarray, targets: np.ndarray) -> float:
    """Rank-based AUROC (Mann-Whitney U). Returns nan if single-class."""
    pos = probs[targets == 1]
    neg = probs[targets == 0]
    if len(pos) == 0 or len(neg) == 0:
        return float("nan")
    order = np.argsort(probs, kind="mergesort")
    ranks = np.empty_like(order, dtype=np.float64)
    ranks[order] = np.arange(1, len(probs) + 1)
    # average ties
    sorted_p = probs[order]
    i = 0
    while i < len(sorted_p):
        j = i
        while j + 1 < len(sorted_p) and sorted_p[j + 1] == sorted_p[i]:
            j += 1
        if j > i:
            ranks[order[i:j + 1]] = (i + 1 + j + 1) / 2.0
        i = j + 1
    sum_pos = ranks[targets == 1].sum()
    n_pos, n_neg = len(pos), len(neg)
    return float((sum_pos - n_pos * (n_pos + 1) / 2.0) / (n_pos * n_neg))


def _auprc(probs: np.ndarray, targets: np.ndarray) -> float:
    """Average precision (area under PR curve). Returns nan if no positives."""
    if targets.sum() == 0:
        return float("nan")
    order = np.argsort(-probs, kind="mergesort")
    t = targets[order]
    tp = np.cumsum(t)
    fp = np.cumsum(1 - t)
    precision = tp / np.maximum(tp + fp, 1)
    recall = tp / t.sum()
    # step-wise integration over recall increments
    ap, prev_recall = 0.0, 0.0
    for p, r in zip(precision, recall):
        ap += p * (r - prev_recall)
        prev_recall = r
    return float(ap)


def _val_metrics(probs: torch.Tensor, targets: torch.Tensor) -> tuple[float, float]:
    """Pooled AUROC/AUPRC across all horizons (nan-safe averaging)."""
    p = probs.reshape(-1).numpy()
    y = targets.reshape(-1).numpy()
    auroc = _auroc(p, y)
    auprc = _auprc(p, y)
    return (0.0 if np.isnan(auroc) else auroc,
            0.0 if np.isnan(auprc) else auprc)


# ── Train ──────────────────────────────────────────────────────────────────────
def train(outcome: str):
    samples, feat_mean, feat_std = load_dataset_for(outcome)
    if len(samples) < 100:
        print("❌ Insufficient samples."); sys.exit(1)

    random.shuffle(samples)
    split = int(len(samples) * TRAIN_SPLIT)
    train_loader = DataLoader(samples[:split], batch_size=BATCH_SIZE, shuffle=True,
                              collate_fn=collate, drop_last=True)
    val_loader = DataLoader(samples[split:], batch_size=BATCH_SIZE, collate_fn=collate)

    device = torch.device("mps" if torch.backends.mps.is_available() else "cpu")
    model = OrbVigilCfC(input_dim=len(PRODUCTION_FEATURE_ORDER), hidden_dim=HIDDEN_DIM,
                        horizons=HORIZONS_HOURS, outcome=outcome).to(device)
    print(f"[Model] OrbVigilCfC '{outcome}' — {sum(p.numel() for p in model.parameters()):,} params on {device}")

    # Class imbalance: weight positives per horizon.
    all_labels = torch.stack([s[2] for s in samples])
    pos_rate = all_labels.mean(0).clamp(1e-3, 1 - 1e-3)
    pos_weight = ((1 - pos_rate) / pos_rate).clamp(max=20.0).to(device)
    criterion = nn.BCEWithLogitsLoss(pos_weight=pos_weight)
    opt = torch.optim.AdamW(model.parameters(), lr=LR, weight_decay=1e-4)
    sched = torch.optim.lr_scheduler.CosineAnnealingLR(opt, T_max=EPOCHS)

    ckpt_path = SERVER_DIR / "models" / f"vigil_cfc_{outcome}.pt"
    ckpt_path.parent.mkdir(exist_ok=True)
    best_val = float("inf")

    for epoch in range(1, EPOCHS + 1):
        model.train(); tr = 0.0
        for v, d, y in train_loader:
            v, d, y = v.to(device), d.to(device), y.to(device)
            # Train on RAW logits — feeding BCEWithLogitsLoss reconstructed logits
            # from clamped sigmoids is numerically lossy and saturates gradients.
            logits = model(v, d, return_logits=True)
            loss = criterion(logits, y)
            opt.zero_grad(); loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0); opt.step()
            tr += loss.item()
        sched.step()

        model.eval(); vl = 0.0
        val_probs, val_targets = [], []
        with torch.no_grad():
            for v, d, y in val_loader:
                v, d, y = v.to(device), d.to(device), y.to(device)
                logits = model(v, d, return_logits=True)
                vl += criterion(logits, y).item()
                val_probs.append(torch.sigmoid(logits).cpu())
                val_targets.append(y.cpu())
        vl /= max(1, len(val_loader))

        # Discrimination metrics on the held-out set (don't checkpoint on loss
        # alone — a clinical risk model must be judged on AUROC/AUPRC). Computed
        # over all horizons pooled; falls back gracefully if a horizon is single-class.
        auroc, auprc = _val_metrics(torch.cat(val_probs), torch.cat(val_targets))
        print(f"  epoch {epoch:>2} | train {tr/max(1,len(train_loader)):.4f} | "
              f"val {vl:.4f} | AUROC {auroc:.3f} | AUPRC {auprc:.3f}")

        if vl < best_val:
            best_val = vl
            torch.save({
                "model_state_dict": model.state_dict(),
                "outcome": outcome,
                "input_dim": len(PRODUCTION_FEATURE_ORDER),
                "hidden_dim": HIDDEN_DIM,
                "horizons": list(HORIZONS_HOURS),
                "feature_order": list(PRODUCTION_FEATURE_ORDER),
                # Stats computed from the ACTUAL training corpus (not the
                # hardcoded PRODUCTION_FEATURE_* guesses) so inference normalizes
                # with the same calibration the model was trained under.
                "feature_mean": feat_mean.tolist(),
                "feature_std": feat_std.tolist(),
                "epoch": epoch, "val_loss": vl,
                "val_auroc": round(auroc, 4), "val_auprc": round(auprc, 4),
                "data_source": "physionet_cinc2019" if outcome == "sepsis" else "unknown",
            }, str(ckpt_path))

    print(f"\n✓ Best val {best_val:.4f} — saved {ckpt_path}")
    # TODO (deferred, cross-cutting): gate promotion of any checkpoint on beating
    # the NEWS2 baseline on held-out AUROC/AUPRC, add reliability/Brier calibration,
    # and derive the live escalation thresholds (vigil_service.py:337-342) from a
    # target-sensitivity operating point rather than hardcoded 0.85/0.7 literals.
    # See docs/ORB_TRAINING_PLAYBOOK.md.


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--outcome", default="sepsis", choices=["sepsis", "deterioration", "mortality"])
    train(ap.parse_args().outcome)
