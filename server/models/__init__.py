"""Orb model package.

Contains:
  - Pydantic API schemas (user.py, message.py)
  - Neural-net architectures (vigil_cfc.py, forecast_lstm.py)

NOTE: This package was reconstructed from the training scripts, services, and
docs (docs/archive/orb_system_overview.md) after the original source was
lost in a machine migration. The architectures match every call site; trained
checkpoints must be regenerated via scripts/train_vigil_cfc.py and
scripts/train_forecast.py (the old .pt checkpoints were not recovered).
"""
