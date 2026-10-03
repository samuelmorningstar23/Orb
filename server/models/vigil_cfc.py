"""Orb Vigil — Closed-form Continuous-time (CfC) Liquid Neural Networks.

Vigil's learned risk overlay. Design (rebuilt to spec 2026-06):

  * ONE model per clinical outcome — train a separate OrbVigilCfC for each of
    `deterioration`, `sepsis`, `mortality`, on the dataset appropriate to that
    outcome. They are independent "algorithms" so each can be trained, swapped,
    and improved on its own (and so a weak label for one doesn't pollute another).
  * MULTI-HORIZON output — each model predicts the risk of its event within
    1h / 3h / 6h / 12h / 24h, i.e. a risk *trajectory*, not a single number.
  * Continuous-time — the CfC consumes the elapsed Δt between observations
    explicitly, so irregular sampling is handled natively.

Phase 1 the displayed risk is NEWS2 (rule-based, see services/vigil_service.py)
with these CfC models as a learned overlay. The long-term goal is for the CfC
ensemble to surpass NEWS2 as real data accrues.

Closed-form CfC update (Hasani et al., as specified for Orb):
    h(t) = (Wx·x(t)) ⊙ exp(-f(x,h;θ)·Δt) + g(x,h;θ)

Feature vector `x(t)` is assembled by the caller (vigil_service / trainer):
    [ 7 vitals, AVPU, age, *optional case-specific labs* ... ]
`input_dim` is therefore a constructor argument — keep it consistent between
training and inference for a given checkpoint. Optional labs are handled at the
feature-assembly layer (zero-filled + a missingness mask channel), so the model
stays agnostic to which labs are present for a given case.

Interface (call sites depend on it):
    model = OrbVigilCfC(input_dim, hidden_dim=64)          # one outcome
    risks = model(vitals, dt)        # vitals (B,T,input_dim), dt (B,T,1)
                                     #   -> (B, num_horizons) sigmoid risks
"""
from __future__ import annotations

import torch
import torch.nn as nn
import torch.nn.functional as F

# Risk horizons (hours ahead). The output vector is ordered to match this tuple.
HORIZONS_HOURS: tuple[int, ...] = (1, 3, 6, 12, 24)

# One independent model is trained per outcome.
VIGIL_OUTCOMES: tuple[str, ...] = ("deterioration", "sepsis", "mortality")

# Canonical PRODUCTION feature schema: 7 vitals + AVPU + age. A checkpoint
# trained with this exact order + normalization can run on live vitals.
# MUST stay in sync with services/vigil_service.py (_CFC_FEATURE_*).
PRODUCTION_FEATURE_ORDER: tuple[str, ...] = (
    "heart_rate", "spo2", "temperature", "systolic_bp",
    "map", "diastolic_bp", "respiratory_rate", "avpu", "age",
)
PRODUCTION_FEATURE_MEAN: tuple[float, ...] = (83.0, 96.5, 36.9, 122.0, 82.0, 63.0, 19.0, 0.2, 62.0)
PRODUCTION_FEATURE_STD: tuple[float, ...] = (18.0, 3.0, 0.7, 23.0, 17.0, 13.0, 5.0, 0.6, 18.0)


class CfCCell(nn.Module):
    """A single closed-form continuous-time recurrent cell."""

    def __init__(self, input_dim: int, hidden_dim: int):
        super().__init__()
        self.input_dim = input_dim
        self.hidden_dim = hidden_dim

        # Wx — projects raw input into hidden space (the "x branch").
        self.Wx = nn.Linear(input_dim, hidden_dim)

        backbone_in = input_dim + hidden_dim
        # f -> positive continuous-time decay constant; g -> steady-state target.
        self.f = nn.Sequential(
            nn.Linear(backbone_in, hidden_dim), nn.Tanh(),
            nn.Linear(hidden_dim, hidden_dim),
        )
        self.g = nn.Sequential(
            nn.Linear(backbone_in, hidden_dim), nn.Tanh(),
            nn.Linear(hidden_dim, hidden_dim),
        )

    def forward(self, x: torch.Tensor, h: torch.Tensor, dt: torch.Tensor) -> torch.Tensor:
        # x: (B, input_dim) | h: (B, hidden_dim) | dt: (B, 1)
        combined = torch.cat([x, h], dim=-1)
        decay_rate = F.softplus(self.f(combined))     # strictly positive
        steady = self.g(combined)
        decay = torch.exp(-decay_rate * dt)           # dt broadcasts over hidden
        return self.Wx(x) * decay + steady


class OrbVigilCfC(nn.Module):
    """CfC encoder + multi-horizon risk head for a SINGLE outcome."""

    def __init__(
        self,
        input_dim: int,
        hidden_dim: int = 64,
        horizons: tuple[int, ...] = HORIZONS_HOURS,
        outcome: str = "deterioration",
    ):
        super().__init__()
        self.input_dim = input_dim
        self.hidden_dim = hidden_dim
        self.horizons = tuple(horizons)
        self.num_horizons = len(self.horizons)
        self.outcome = outcome

        self.cell = CfCCell(input_dim, hidden_dim)
        self.readout = nn.Sequential(
            nn.Linear(hidden_dim, hidden_dim), nn.ReLU(), nn.Dropout(0.2),
            nn.Linear(hidden_dim, self.num_horizons),
        )

    def forward(self, vitals: torch.Tensor, dt: torch.Tensor,
                return_logits: bool = False) -> torch.Tensor:
        """vitals (B,T,input_dim), dt (B,T,1) -> risk (B, num_horizons).

        By default returns sigmoid probabilities in [0,1] (inference). Pass
        ``return_logits=True`` during training so the raw per-horizon logits can
        be fed straight into ``nn.BCEWithLogitsLoss`` — reconstructing logits from
        clamped sigmoids (log(p/(1-p))) is numerically lossy and caps the effective
        logit at ~±13.8, which kills gradients on confidently-wrong saturated
        predictions. The trainer must use this path.
        """
        if vitals.dim() == 2:
            vitals = vitals.unsqueeze(0)
        if dt.dim() == 2:
            dt = dt.unsqueeze(0)

        batch, seq_len, _ = vitals.shape
        h = torch.zeros(batch, self.hidden_dim, device=vitals.device, dtype=vitals.dtype)
        for t in range(seq_len):
            h = self.cell(vitals[:, t, :], h, dt[:, t, :])

        # Per-horizon logits (risk of event within horizon h).
        logits = self.readout(h)
        if return_logits:
            return logits
        return torch.sigmoid(logits)

    @torch.no_grad()
    def predict_trajectory(self, vitals: torch.Tensor, dt: torch.Tensor) -> dict:
        """Single-sample convenience: returns {outcome, risk_by_horizon, peak_risk}."""
        self.eval()
        probs = self.forward(vitals, dt)[0]   # (num_horizons,) sigmoid risks
        risk_by_horizon = {f"{h}h": round(float(probs[i].item()), 4)
                           for i, h in enumerate(self.horizons)}
        return {
            "outcome": self.outcome,
            "risk_by_horizon": risk_by_horizon,
            "peak_risk": round(float(probs.max().item()), 4),
        }
