"""Synthetic tensors prove model/history identity invalidates the CfC cache."""
from contextlib import nullcontext
from copy import deepcopy

import pytest
import torch

from services import vigil_service as vigil
from services.inference_cache import InferenceCache


@pytest.fixture
def prediction(monkeypatch):
    calls = []
    class Model(torch.nn.Module):
        def __init__(self):
            super().__init__()
            self.weight = torch.nn.Parameter(torch.ones(1))
        def predict_trajectory(self, vitals, dt):
            calls.append((vitals.clone(), dt.clone()))
            return {"risk_by_horizon": {"1h": 0.25}, "peak_risk": 0.25}
    model = Model()
    tensors = [torch.zeros((1, 3, 9)), torch.zeros((1, 3, 1))]
    meta = {"input_dim": 9, "feature_order": list(vigil._CFC_FEATURE_ORDER), "mean": [0] * 9, "std": [1] * 9}
    monkeypatch.setattr("config.MODELS_ENABLED", True)
    monkeypatch.setattr("config.WARD_RECORD", False)
    monkeypatch.setattr(vigil, "_load_cfc_models", lambda: True)
    monkeypatch.setattr(vigil, "_cfc_models", {"deterioration": model})
    monkeypatch.setattr(vigil, "_cfc_meta", {"deterioration": meta})
    monkeypatch.setattr(vigil, "_cfc_prediction_cache", InferenceCache[dict]())
    monkeypatch.setattr(vigil, "_assemble_feature_sequence", lambda *args: tuple(tensors))
    monkeypatch.setattr("services.gpu_coordinator.vram_guard", lambda *args: nullcontext())
    return calls, model, tensors, meta


def test_repeated_cards_reuse_identical_prediction_and_isolate_returned_objects(prediction):
    calls, model, tensors, meta = prediction
    first = vigil.compute_cfc_risk("synthetic-A")
    original = deepcopy(first)
    first["outcomes"]["deterioration"]["risk_by_horizon"]["1h"] = 99
    assert vigil.compute_cfc_risk("synthetic-A") == original
    assert len(calls) == 1


@pytest.mark.parametrize("change", ["age_or_history", "relative_time", "model_state", "normalization", "patient", "new_model"])
def test_every_model_input_and_patient_context_invalidates(prediction, change):
    calls, model, tensors, meta = prediction
    vigil.compute_cfc_risk("synthetic-A")
    patient = "synthetic-A"
    if change == "age_or_history":
        tensors[0][0, 0, 8] = 1
    elif change == "relative_time":
        tensors[1][0, 1, 0] = 1
    elif change == "model_state":
        model.load_state_dict({"weight": torch.full((1,), 2.)})
    elif change == "normalization":
        meta["mean"][0] = 1
    elif change == "patient":
        patient = "synthetic-B"
    else:
        vigil._cfc_models["deterioration"] = type(model)()
    vigil.compute_cfc_risk(patient)
    assert len(calls) == 2


def test_sparse_or_switched_off_never_replays_a_previous_score(prediction, monkeypatch):
    vigil.compute_cfc_risk("synthetic-A")
    monkeypatch.setattr(vigil, "_assemble_feature_sequence", lambda *args: (None, None))
    assert vigil.compute_cfc_risk("synthetic-A") is None
    monkeypatch.setattr("config.MODELS_ENABLED", False)
    assert vigil.compute_cfc_risk("synthetic-A") is None
