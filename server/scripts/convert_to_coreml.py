#!/usr/bin/env python3
"""Convert Orb PyTorch models to CoreML format for native Apple Silicon inference.

Converts:
  1. Vigil CfC LNN (vigil_cfc_checkpoint.pt) → vigil_cfc.mlpackage
  2. Forecast LSTM (forecast_checkpoint.pt) → forecast_lstm.mlpackage

Requirements:
  pip install torch coremltools

Usage:
  python convert_to_coreml.py                       # Convert both models
  python convert_to_coreml.py --vigil-only           # Convert Vigil CfC only
  python convert_to_coreml.py --forecast-only        # Convert Forecast LSTM only
  python convert_to_coreml.py --output-dir ./models  # Custom output directory
"""

import argparse
import sys
from pathlib import Path

# Add parent dirs to path for model imports
SERVER_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVER_DIR))


def convert_vigil_cfc(checkpoint_path: str, output_dir: str) -> str:
    """Convert Vigil CfC Liquid Neural Network to CoreML.

    The CfC model uses a sequential loop over the time dimension, which
    requires tracing with a fixed sequence length. We trace with seq_len=24
    (the maximum used in inference).
    """
    import coremltools as ct
    import torch
    from models.vigil_cfc import OrbVigilCfC

    print("\n━━━ Converting Vigil CfC LNN ━━━")

    # Load checkpoint
    checkpoint = torch.load(checkpoint_path, map_location="cpu", weights_only=False)
    hidden_dim = checkpoint.get("hidden_dim", 64)
    input_dim = checkpoint.get("input_dim", 7)

    print(f"  Input dim: {input_dim}, Hidden dim: {hidden_dim}")
    print(f"  Epoch: {checkpoint.get('epoch', '?')}, Val AUC: {checkpoint.get('val_auc', 0):.4f}")

    # Instantiate and load weights
    model = OrbVigilCfC(input_dim=input_dim, hidden_dim=hidden_dim)
    model.load_state_dict(checkpoint["model_state_dict"])
    model.eval()

    # Trace with fixed sequence length
    seq_len = 24
    dummy_vitals = torch.randn(1, seq_len, input_dim)
    dummy_dt = torch.ones(1, seq_len, 1) * 1.0  # 1 hour between observations

    print(f"  Tracing with seq_len={seq_len}...")
    traced = torch.jit.trace(model, (dummy_vitals, dummy_dt))

    # Convert to CoreML
    print("  Converting to CoreML...")
    mlmodel = ct.convert(
        traced,
        inputs=[
            ct.TensorType(name="vitals", shape=(1, seq_len, input_dim)),
            ct.TensorType(name="dt", shape=(1, seq_len, 1)),
        ],
        outputs=[
            ct.TensorType(name="risk_probability"),
        ],
        compute_precision=ct.precision.FLOAT16,
        minimum_deployment_target=ct.target.macOS14,
    )

    # Add metadata
    mlmodel.author = "Orb Hospital OS"
    mlmodel.short_description = (
        "CfC Liquid Neural Network for patient deterioration risk prediction. "
        f"Input: {input_dim} vitals features × {seq_len} timesteps + continuous Δt. "
        "Output: Sepsis/deterioration probability (0.0-1.0)."
    )
    mlmodel.input_description["vitals"] = (
        "Z-score normalized vitals sequence (1, 24, 7). "
        "Features: [HR, SpO2, Temp, SBP, MAP, DBP, RR]."
    )
    mlmodel.input_description["dt"] = "Elapsed hours between observations (1, 24, 1)."
    mlmodel.output_description["risk_probability"] = (
        "Deterioration probability (1, 1). Range: 0.0 (stable) to 1.0 (critical)."
    )

    # Save
    output_path = Path(output_dir) / "vigil_cfc.mlpackage"
    mlmodel.save(str(output_path))
    print(f"  ✅ Saved to: {output_path}")
    print(
        f"     Size: {sum(f.stat().st_size for f in output_path.rglob('*') if f.is_file()) / 1024:.1f} KB"
    )

    return str(output_path)


def convert_forecast_lstm(checkpoint_path: str, output_dir: str) -> str:
    """Convert Forecast LSTM to CoreML.

    The bidirectional LSTM with temporal attention is natively supported
    by CoreML. We trace with the fixed input shape (1, 48, 19).
    """
    import coremltools as ct
    import torch
    from models.forecast_lstm import INPUT_DIM, OrbForecastLSTM

    print("\n━━━ Converting Forecast LSTM ━━━")

    # Load checkpoint
    model = OrbForecastLSTM()
    state = torch.load(checkpoint_path, map_location="cpu", weights_only=True)
    model.load_state_dict(state["model_state_dict"])
    model.eval()

    print(f"  Input dim: {INPUT_DIM}, Hidden dim: 64, Layers: 2, Bidirectional: True")
    print(f"  Epoch: {state.get('epoch', '?')}, Best loss: {state.get('best_loss', '?')}")

    # The model.forward() returns a dict, which CoreML can't handle directly.
    # Create a wrapper that returns a tuple of tensors.
    class ForecastWrapper(torch.nn.Module):
        def __init__(self, model):
            super().__init__()
            self.model = model

        def forward(self, x):
            out = self.model(x)
            # Apply softmax to acuity logits at export time
            import torch.nn.functional as F

            acuity_probs = F.softmax(out["acuity_logits"], dim=-1)
            return out["los"], out["discharge_prob"], acuity_probs

    wrapper = ForecastWrapper(model)
    wrapper.eval()

    # Trace
    seq_len = 48
    dummy_input = torch.randn(1, seq_len, INPUT_DIM)

    print(f"  Tracing with seq_len={seq_len}...")
    traced = torch.jit.trace(wrapper, dummy_input)

    # Convert to CoreML
    print("  Converting to CoreML...")
    mlmodel = ct.convert(
        traced,
        inputs=[
            ct.TensorType(name="features", shape=(1, seq_len, INPUT_DIM)),
        ],
        outputs=[
            ct.TensorType(name="los_days"),
            ct.TensorType(name="discharge_probability"),
            ct.TensorType(name="acuity_probs"),
        ],
        compute_precision=ct.precision.FLOAT16,
        minimum_deployment_target=ct.target.macOS14,
    )

    # Add metadata
    mlmodel.author = "Orb Hospital OS"
    mlmodel.short_description = (
        f"Multi-task Bidirectional LSTM for clinical forecasting. "
        f"Input: {INPUT_DIM} features × {seq_len} timesteps. "
        "Outputs: LoS (days), discharge probability, acuity trajectory."
    )
    mlmodel.input_description["features"] = (
        "Per-patient time-series features (1, 48, 19). "
        "Includes normalized vitals, demographics, ward/severity one-hot, "
        "days since admission, medication count, risk score."
    )
    mlmodel.output_description["los_days"] = "Predicted remaining length-of-stay in days (1, 1)."
    mlmodel.output_description["discharge_probability"] = (
        "P(discharge within 24h) (1, 1). Range: 0.0-1.0."
    )
    mlmodel.output_description["acuity_probs"] = (
        "Acuity trajectory probabilities (1, 3). Indices: [0]=improving, [1]=stable, [2]=worsening."
    )

    # Save
    output_path = Path(output_dir) / "forecast_lstm.mlpackage"
    mlmodel.save(str(output_path))
    print(f"  ✅ Saved to: {output_path}")
    print(
        f"     Size: {sum(f.stat().st_size for f in output_path.rglob('*') if f.is_file()) / 1024:.1f} KB"
    )

    return str(output_path)


def main():
    parser = argparse.ArgumentParser(description="Convert Orb PyTorch models to CoreML")
    parser.add_argument("--vigil-only", action="store_true", help="Convert Vigil CfC only")
    parser.add_argument("--forecast-only", action="store_true", help="Convert Forecast LSTM only")
    parser.add_argument(
        "--output-dir",
        default=str(SERVER_DIR / "models" / "coreml"),
        help="Output directory for .mlpackage files",
    )
    parser.add_argument(
        "--vigil-checkpoint",
        default=str(SERVER_DIR / "models" / "vigil_cfc_checkpoint.pt"),
        help="Path to Vigil CfC checkpoint",
    )
    parser.add_argument(
        "--forecast-checkpoint",
        default=str(SERVER_DIR / "models" / "forecast_checkpoint.pt"),
        help="Path to Forecast LSTM checkpoint",
    )
    args = parser.parse_args()

    # Create output directory
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    print("╔══════════════════════════════════════════════════╗")
    print("║    Orb PyTorch → CoreML Model Conversion        ║")
    print("╚══════════════════════════════════════════════════╝")
    print(f"  Output: {output_dir}")

    converted = []

    if not args.forecast_only:
        if Path(args.vigil_checkpoint).exists():
            path = convert_vigil_cfc(args.vigil_checkpoint, str(output_dir))
            converted.append(("Vigil CfC", path))
        else:
            print(f"\n⚠️  Vigil checkpoint not found: {args.vigil_checkpoint}")

    if not args.vigil_only:
        if Path(args.forecast_checkpoint).exists():
            path = convert_forecast_lstm(args.forecast_checkpoint, str(output_dir))
            converted.append(("Forecast LSTM", path))
        else:
            print(f"\n⚠️  Forecast checkpoint not found: {args.forecast_checkpoint}")

    print("\n━━━ Summary ━━━")
    if converted:
        for name, path in converted:
            print(f"  ✅ {name} → {path}")
        print("\nCopy the .mlpackage files to swift/Sources/OrbAI/Resources/")
        print("and add them to the Xcode project as CoreML model resources.")
    else:
        print("  ❌ No models converted.")


if __name__ == "__main__":
    main()
