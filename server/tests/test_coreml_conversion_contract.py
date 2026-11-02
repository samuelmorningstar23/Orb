"""Synthetic checkpoint compatibility; CoreML conversion/install is never run."""
import json
import sys
from types import SimpleNamespace

import pytest
import torch

from models.vigil_cfc import HORIZONS_HOURS, OrbVigilCfC
from scripts.convert_to_coreml import convert_vigil_cfc


@pytest.mark.parametrize("explicit", [False, True])
def test_converter_reconstructs_checkpoint_horizons_and_labels(tmp_path, monkeypatch, explicit):
    horizons = (2, 8, 16) if explicit else HORIZONS_HOURS
    outcome = "sepsis" if explicit else "deterioration"
    original = OrbVigilCfC(input_dim=3, hidden_dim=4, horizons=horizons, outcome=outcome).eval()
    checkpoint = {"input_dim": 3, "hidden_dim": 4, "model_state_dict": original.state_dict()}
    if explicit:
        checkpoint.update(horizons=list(horizons), outcome=outcome, feature_order=["a", "b", "c"])
    path = tmp_path / "synthetic.pt"
    torch.save(checkpoint, path)
    captured = {}
    exported = SimpleNamespace(input_description={}, output_description={}, user_defined_metadata={},
                               save=lambda name: captured.update(saved=name))

    def trace(model, inputs):
        captured.update(model=model, inputs=inputs)
        with torch.inference_mode():
            torch.testing.assert_close(model(*inputs), original(*inputs))
        return "synthetic-trace"

    def convert(traced, **kwargs):
        assert traced == "synthetic-trace"
        captured.update(convert=kwargs)
        return exported

    monkeypatch.setattr(torch.jit, "trace", trace)
    monkeypatch.setitem(sys.modules, "coremltools", SimpleNamespace(
        convert=convert, TensorType=lambda **kwargs: kwargs,
        precision=SimpleNamespace(FLOAT16="synthetic-fp16"), target=SimpleNamespace(macOS14="synthetic-macos14")))
    output = convert_vigil_cfc(str(path), str(tmp_path / "unused-output"))
    assert output == captured["saved"]
    assert captured["model"].horizons == horizons and captured["model"].outcome == outcome
    assert captured["convert"]["inputs"][0]["shape"] == (1, 24, 3)
    assert captured["convert"]["outputs"] == [{"name": "risk_probability"}]
    assert json.loads(exported.user_defined_metadata["orb.horizons_hours"]) == list(horizons)
    assert exported.user_defined_metadata["orb.outcome"] == outcome
    if explicit:
        assert json.loads(exported.user_defined_metadata["orb.feature_order"]) == ["a", "b", "c"]
    assert f"(1, {len(horizons)})" in exported.output_description["risk_probability"]
    assert not (tmp_path / "unused-output").exists(), "the CoreML stub must write no model artifacts"


def test_checkpoint_head_mismatch_stops_before_trace_or_conversion(tmp_path, monkeypatch):
    original = OrbVigilCfC(input_dim=3, hidden_dim=4, horizons=(2, 8)).eval()
    path = tmp_path / "synthetic-mismatch.pt"
    torch.save({"input_dim": 3, "hidden_dim": 4, "horizons": [2, 8, 16],
                "model_state_dict": original.state_dict()}, path)
    monkeypatch.setitem(sys.modules, "coremltools", SimpleNamespace())
    monkeypatch.setattr(torch.jit, "trace", lambda *a, **k: pytest.fail("mismatched checkpoint reached trace"))
    with pytest.raises(RuntimeError, match="size mismatch"):
        convert_vigil_cfc(str(path), str(tmp_path / "unused-output"))
    assert not (tmp_path / "unused-output").exists()
