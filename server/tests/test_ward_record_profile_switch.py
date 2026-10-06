"""ORB_WARD_RECORD: one switch for the Orb Ward Record profile (6 October 2026).

On, it forces ORB_SCORING_ENABLED, ORB_SEPSIS_AUTOSTART, ORB_LENS_ENABLED and
ORB_MODELS_ENABLED off whatever those variables say, and the boot log names
every setting it forced. /api/health publishes the profile and the models
switch beside the other clinical switches.

config.py is read in a fresh interpreter for each case, because the forcing
happens once, when config is imported.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import config

SERVER = Path(__file__).resolve().parents[1]

_PROBE = (
    "import json, config; print(json.dumps({"
    "'ward_record': config.WARD_RECORD, 'scoring': config.SCORING_ENABLED, "
    "'sepsis': config.SEPSIS_AUTOSTART, 'lens': config.LENS_ENABLED, "
    "'models': config.MODELS_ENABLED, 'forced': list(config.WARD_RECORD_FORCED), "
    "'line': config.ward_record_boot_line()}))"
)


def _config_with(env: dict) -> dict:
    full = {k: v for k, v in os.environ.items()
            if not k.startswith(("ORB_WARD_RECORD", "ORB_SCORING_ENABLED",
                                 "ORB_SEPSIS_AUTOSTART", "ORB_LENS_ENABLED",
                                 "ORB_MODELS_ENABLED"))}
    full["ORB_DEPLOYMENT_PROFILE"] = "dev"
    full.update(env)
    out = subprocess.run([sys.executable, "-c", _PROBE], cwd=SERVER, env=full,
                         capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_the_profile_is_off_by_default_and_changes_nothing():
    got = _config_with({})
    assert got["ward_record"] is False
    assert got["scoring"] is True and got["sepsis"] is True and got["models"] is True
    assert got["forced"] == [] and got["line"] is None


def test_the_profile_forces_every_switch_off_whatever_they_say():
    got = _config_with({
        "ORB_WARD_RECORD": "true",
        "ORB_SCORING_ENABLED": "true",
        "ORB_SEPSIS_AUTOSTART": "true",
        "ORB_LENS_ENABLED": "true",
        "ORB_MODELS_ENABLED": "true",
    })
    assert got["ward_record"] is True
    assert (got["scoring"], got["sepsis"], got["lens"], got["models"]) == (False, False, False, False)
    assert got["forced"] == ["ORB_SCORING_ENABLED", "ORB_SEPSIS_AUTOSTART",
                             "ORB_LENS_ENABLED", "ORB_MODELS_ENABLED"]


def test_the_boot_line_names_every_forced_setting_and_what_it_asked_for():
    line = _config_with({"ORB_WARD_RECORD": "1", "ORB_SCORING_ENABLED": "true"})["line"]
    assert line.startswith("Orb Ward Record profile (ORB_WARD_RECORD) is on.")
    for name in ("ORB_SCORING_ENABLED", "ORB_SEPSIS_AUTOSTART", "ORB_LENS_ENABLED",
                 "ORB_MODELS_ENABLED"):
        assert f"{name}=false" in line
    assert "ORB_SCORING_ENABLED=false (the variable says true)" in line
    assert "ORB_MODELS_ENABLED=false (the variable says nothing)" in line
    assert "\u2014" not in line and "\u2013" not in line


def test_models_can_be_switched_off_without_the_profile():
    got = _config_with({"ORB_MODELS_ENABLED": "false"})
    assert got["models"] is False
    assert got["ward_record"] is False and got["scoring"] is True and got["line"] is None


def test_health_publishes_the_profile_and_the_models_switch(monkeypatch):
    from fastapi.testclient import TestClient
    from main import app
    monkeypatch.setattr(config, "WARD_RECORD", True, raising=False)
    monkeypatch.setattr(config, "MODELS_ENABLED", False, raising=False)
    monkeypatch.setattr(config, "SCORING_ENABLED", False, raising=False)
    body = TestClient(app).get("/api/health").json()
    assert body["clinical"]["ward_record"] is True
    assert body["clinical"]["models_enabled"] is False
    assert body["clinical"]["scoring_enabled"] is False
    assert set(body["clinical"]) == {"scoring_enabled", "lens_enabled", "sepsis_autostart",
                                     "models_enabled", "ward_record"}


def test_health_says_the_profile_is_off_on_an_ordinary_site(monkeypatch):
    from main import clinical_switches
    monkeypatch.setattr(config, "WARD_RECORD", False, raising=False)
    monkeypatch.setattr(config, "MODELS_ENABLED", True, raising=False)
    got = clinical_switches()
    assert got["ward_record"] is False and got["models_enabled"] is True
