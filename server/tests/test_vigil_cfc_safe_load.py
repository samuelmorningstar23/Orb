"""The Vigil CfC checkpoint loader must never unpickle arbitrary objects.

weights_only=False made a tampered .pt in server/models/ code execution at
boot. The loader now uses weights_only=True: a checkpoint shaped like the one
scripts/train_vigil_cfc.py writes still loads, and anything else is refused,
logged, and leaves Vigil NEWS2-only.
"""
import pytest

torch = pytest.importorskip("torch")

from services import vigil_service  # noqa: E402
from models.vigil_cfc import OrbVigilCfC, HORIZONS_HOURS  # noqa: E402


class _NotATensor:
    """Any class at all: the safe loader must refuse to rebuild it."""


@pytest.fixture
def fresh_loader(tmp_path, monkeypatch):
    monkeypatch.setattr(vigil_service, "_MODELS_DIR", tmp_path)
    monkeypatch.setattr(vigil_service, "_cfc_models", {})
    monkeypatch.setattr(vigil_service, "_cfc_meta", {})
    monkeypatch.setattr(vigil_service, "_cfc_loaded", False)
    return tmp_path


def _trainer_shaped_checkpoint():
    feature_order = list(vigil_service._CFC_FEATURE_ORDER)
    model = OrbVigilCfC(input_dim=len(feature_order), hidden_dim=64,
                        horizons=tuple(HORIZONS_HOURS), outcome="deterioration")
    return {
        "model_state_dict": model.state_dict(),
        "outcome": "deterioration",
        "input_dim": len(feature_order),
        "hidden_dim": 64,
        "horizons": list(HORIZONS_HOURS),
        "feature_order": feature_order,
        "feature_mean": list(vigil_service._CFC_FEATURE_MEAN),
        "feature_std": list(vigil_service._CFC_FEATURE_STD),
        "epoch": 3, "val_loss": 0.41, "val_auroc": 0.71, "val_auprc": 0.22,
        "data_source": "unknown",
    }


def test_a_trainer_shaped_checkpoint_still_loads(fresh_loader):
    torch.save(_trainer_shaped_checkpoint(), str(fresh_loader / "vigil_cfc_deterioration.pt"))
    assert vigil_service._load_cfc_models() is True
    assert "deterioration" in vigil_service._cfc_models


def test_a_checkpoint_carrying_an_arbitrary_object_is_refused(fresh_loader):
    ckpt = _trainer_shaped_checkpoint()
    ckpt["payload"] = _NotATensor()
    torch.save(ckpt, str(fresh_loader / "vigil_cfc_deterioration.pt"))
    assert vigil_service._load_cfc_models() is False
    assert vigil_service._cfc_models == {}
