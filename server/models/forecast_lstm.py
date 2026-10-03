"""Orb Forecast — multi-task Bidirectional LSTM with temporal attention.

Consumes a 48-step patient time-series and predicts four clinical milestones
simultaneously (see docs/archive/orb_system_overview.md §4.1):

    Head 1  Length-of-Stay        Linear -> Softplus   (remaining inpatient days)
    Head 2  Discharge prob.       Linear -> Sigmoid    (fit for discharge < 24h)
    Head 3  Acuity trajectory     Linear -> 3 logits   (improving / stable / worsening)
    Head 4  ICU-transfer risk     Linear -> Sigmoid    (transfer within 3h)   ← added

Architecture:
    LayerNorm(D) -> BiLSTM(2 layers, 64 hidden, dropout 0.3)
                 -> temporal attention (tanh -> softmax) -> 128-d context
                 -> 4 task heads

Optional feature extension: the base schema is 19 channels (INPUT_DIM). When a
dataset carries extra signal (labs, comorbidity count, post-op flag, ...),
construct with a larger `input_dim` and assemble the matching feature vector —
the rest of the network adapts automatically.

Interface (call sites depend on it):
    from models.forecast_lstm import OrbForecastLSTM, load_model, INPUT_DIM, HIDDEN_DIM, NUM_LAYERS
    out  = model(features)            # (B,48,D) -> dict(los, discharge_prob, acuity_logits, icu_transfer_prob)
    pred = model.predict(features)    # -> dict(predicted_los_days, discharge_probability,
                                      #         acuity_trajectory, confidence, icu_transfer_risk_3h)
    model = load_model()              # build + load models/forecast_checkpoint.pt
"""
from __future__ import annotations

import logging
from pathlib import Path

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F

logger = logging.getLogger("orb.forecast")

# ── Channel / architecture constants (imported by training + service) ──────────
INPUT_DIM = 19      # vitals(6) + demographics(3) + ward(4) + severity(3) + ops markers(3)
HIDDEN_DIM = 64
NUM_LAYERS = 2

ICU_TRANSFER_WINDOW_H = 3   # ICU-transfer-risk head horizon (hours)
ACUITY_LABELS = {0: "improving", 1: "stable", 2: "worsening"}

_CHECKPOINT_PATH = Path(__file__).resolve().parent / "forecast_checkpoint.pt"


class OrbForecastLSTM(nn.Module):
    def __init__(
        self,
        input_dim: int = INPUT_DIM,
        hidden_dim: int = HIDDEN_DIM,
        num_layers: int = NUM_LAYERS,
        dropout: float = 0.3,
    ):
        super().__init__()
        self.input_dim = input_dim
        self.hidden_dim = hidden_dim
        self.num_layers = num_layers

        # Provenance flags set by load_model(). Until a validated checkpoint is
        # loaded the network is at random init and MUST NOT surface numbers as
        # clinical predictions (see predict()).
        self.trained: bool = False
        # The ICU-transfer head only carries signal if the checkpoint that loaded
        # it was trained with a real ICU-transfer label + BCE loss term. The
        # synthetic trainer (scripts/train_forecast.py) does NOT supply one, so
        # this stays False even for a "trained" forecast checkpoint — its sigmoid
        # is meaningless and predict() omits icu_transfer_risk_3h accordingly.
        self.icu_head_trained: bool = False

        # Learnable input calibration (init from data stats during training).
        self.input_norm = nn.LayerNorm(input_dim)

        self.lstm = nn.LSTM(
            input_size=input_dim,
            hidden_size=hidden_dim,
            num_layers=num_layers,
            batch_first=True,
            bidirectional=True,
            dropout=dropout if num_layers > 1 else 0.0,
        )

        ctx_dim = hidden_dim * 2  # bidirectional -> 128

        # Temporal attention: tanh scoring -> softmax over timesteps.
        self.attn = nn.Sequential(
            nn.Linear(ctx_dim, ctx_dim),
            nn.Tanh(),
            nn.Linear(ctx_dim, 1),
        )

        # Task heads.
        self.los_head = nn.Linear(ctx_dim, 1)
        self.discharge_head = nn.Linear(ctx_dim, 1)
        self.acuity_head = nn.Linear(ctx_dim, 3)
        self.icu_head = nn.Linear(ctx_dim, 1)   # ICU-transfer within ICU_TRANSFER_WINDOW_H

    def forward(self, x: torch.Tensor) -> dict:
        """x: (B, seq_len, input_dim) -> dict of raw head outputs."""
        if x.dim() == 2:
            x = x.unsqueeze(0)

        x = self.input_norm(x)
        lstm_out, _ = self.lstm(x)            # (B, seq, ctx_dim)

        attn_weights = torch.softmax(self.attn(lstm_out), dim=1)
        context = torch.sum(attn_weights * lstm_out, dim=1)   # (B, ctx_dim)

        return {
            "los": F.softplus(self.los_head(context)),                  # (B,1) >= 0
            "discharge_prob": torch.sigmoid(self.discharge_head(context)),
            "acuity_logits": self.acuity_head(context),                 # (B,3)
            "icu_transfer_prob": torch.sigmoid(self.icu_head(context)), # (B,1)
        }

    @torch.no_grad()
    def predict(self, features) -> dict:
        """Inference on a single patient window: (seq,D) or (1,seq,D)."""
        self.eval()
        if not torch.is_tensor(features):
            features = torch.tensor(np.asarray(features), dtype=torch.float32)
        else:
            features = features.float()
        if features.dim() == 2:
            features = features.unsqueeze(0)

        device = next(self.parameters()).device
        features = features.to(device)

        out = self.forward(features)
        acuity_probs = torch.softmax(out["acuity_logits"][0], dim=-1)
        idx = int(torch.argmax(acuity_probs).item())

        result = {
            "predicted_los_days": round(float(out["los"][0, 0].item()), 1),
            "discharge_probability": round(float(out["discharge_prob"][0, 0].item()), 4),
            "acuity_trajectory": ACUITY_LABELS[idx],
            "confidence": round(float(acuity_probs[idx].item()), 4),
            "model_trained": bool(self.trained),
        }
        # FAIL CLOSED on the ICU-transfer head: only expose it when the loaded
        # checkpoint actually trained icu_head against a labelled ICU-transfer
        # target. Otherwise it is sigmoid(random-init) — a clinical-safety defect
        # if surfaced as a 3h transfer probability. Omit the key entirely so no
        # caller mistakes noise for a prediction.
        if self.icu_head_trained:
            result["icu_transfer_risk_3h"] = round(float(out["icu_transfer_prob"][0, 0].item()), 4)
        return result


def load_model(checkpoint_path=None, device=None) -> OrbForecastLSTM:
    """Build an OrbForecastLSTM and load the trained checkpoint if present.

    Falls back to an (untrained) model with a warning when no checkpoint exists,
    so the backend still boots before the model has been trained. The service
    layer applies heuristic fallbacks for predictions in that case.
    """
    if device is None:
        device = torch.device("mps" if torch.backends.mps.is_available() else "cpu")

    path = Path(checkpoint_path) if checkpoint_path else _CHECKPOINT_PATH

    # Honor a checkpoint's own input_dim (extended feature schemas).
    input_dim = INPUT_DIM
    try:
        if path.exists():
            # weights_only=True: our checkpoints are plain dicts of tensors +
            # JSON-safe scalars, so we don't need (and shouldn't allow) arbitrary
            # pickle execution when loading a file from disk.
            ckpt = torch.load(str(path), map_location=device, weights_only=True)
            input_dim = ckpt.get("input_dim", INPUT_DIM)
            model = OrbForecastLSTM(input_dim=input_dim)
            model.load_state_dict(ckpt["model_state_dict"])
            model.trained = True
            # Only trust the ICU head if the checkpoint declares it was trained
            # against a real ICU-transfer label. The current synthetic trainer
            # never sets this, so the head stays gated off.
            model.icu_head_trained = bool(ckpt.get("icu_head_trained", False))
            logger.info(
                "Loaded checkpoint (epoch %s, source=%s, input_dim=%s, "
                "icu_head_trained=%s) on %s",
                ckpt.get("epoch", "?"), ckpt.get("data_source", "?"),
                input_dim, model.icu_head_trained, device,
            )
        else:
            model = OrbForecastLSTM()  # trained stays False
            logger.warning(
                "No checkpoint at %s — using UNTRAINED model. Predictions will be "
                "flagged model_trained=false; run scripts/train_forecast.py once "
                "real data is available.", path,
            )
    except Exception as e:
        model = OrbForecastLSTM()  # trained stays False
        logger.error("Failed to load checkpoint: %s — using untrained model.", e)

    model.to(device)
    model.eval()
    return model
