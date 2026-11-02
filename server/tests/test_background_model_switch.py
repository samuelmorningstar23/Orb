"""An idle benchmark must preserve interactive inference and the ward profile."""
import pytest

import config
from services import model_switch


@pytest.mark.parametrize("models,ward,background,expected", [
    (True, False, True, True),
    (True, False, False, False),
    (False, False, True, False),
    (True, True, True, False),
    (False, True, False, False),
])
def test_background_jobs_respect_master_and_ward_profile(monkeypatch, models, ward, background, expected):
    monkeypatch.setattr(config, "MODELS_ENABLED", models)
    monkeypatch.setattr(config, "WARD_RECORD", ward)
    monkeypatch.setattr(config, "BACKGROUND_MODELS_ENABLED", background)
    assert model_switch.background_models_enabled() is expected
    if models and not ward:
        # The isolated idle setting must not rewrite the interactive master switch.
        model_switch.refuse_when_models_off("Synthetic interactive acceptance")
    else:
        with pytest.raises(model_switch.ModelsSwitchedOff):
            model_switch.refuse_when_models_off("Synthetic interactive acceptance")
