"""Boot Orb as an Orb Ward Record computer and prove no screen scores (6 October 2026).

tests/ward_record_probe.py boots the real app in a fresh interpreter with
ORB_WARD_RECORD=true on a fresh database with seeded patients and vitals, runs
the background lanes once, and calls every GET route under /api as admin,
doctor, nurse, ward clerk and pharmacist. This test fails on any 500, on any
call a scoring entry point refused (vigil_service.ScoringSwitchedOff, counted
even when a caller swallowed it), and on any response that carries the band a
stored risk_scores row held from before the profile was switched on.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SERVER = Path(__file__).resolve().parents[1]
PROBE = SERVER / "tests" / "ward_record_probe.py"


def run_probe(extra_env: dict) -> dict:
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("ORB_DB_PATH", "ORB_WARD_RECORD", "ORB_SCORING_ENABLED",
                                "ORB_MODELS_ENABLED", "ORB_LENS_ENABLED",
                                "ORB_SEPSIS_AUTOSTART"))}
    env["ORB_DEPLOYMENT_PROFILE"] = "dev"
    env.update(extra_env)
    out = subprocess.run([sys.executable, str(PROBE)], cwd=SERVER, env=env,
                         capture_output=True, text=True, timeout=900)
    lines = [ln for ln in out.stdout.splitlines() if ln.startswith("WRPROBE-RESULT ")]
    assert out.returncode == 0 and lines, (
        f"the probe did not finish (exit {out.returncode}):\n{out.stderr[-6000:]}")
    return json.loads(lines[-1][len("WRPROBE-RESULT "):])


@pytest.fixture(scope="module")
def ward_record_boot():
    return run_probe({"ORB_WARD_RECORD": "true"})


def _report(items) -> str:
    return "\n\n".join(items[:15]) + (f"\n\n... and {len(items) - 15} more" if len(items) > 15 else "")


def test_the_probe_booted_the_profile(ward_record_boot):
    assert ward_record_boot["profile"] == {"ward_record": True, "scoring": False, "models": False}
    assert ward_record_boot["calls"] > 300


def test_no_route_answers_500_under_the_profile(ward_record_boot):
    assert not ward_record_boot["server_errors"], _report(ward_record_boot["server_errors"])


def test_nothing_reaches_a_scoring_entry_point_under_the_profile(ward_record_boot):
    assert not ward_record_boot["scoring_calls"], _report(ward_record_boot["scoring_calls"])


def test_no_route_shows_a_band_stored_before_the_profile(ward_record_boot):
    assert not ward_record_boot["marker_leaks"], _report(ward_record_boot["marker_leaks"])


def test_a_chain_stored_before_the_profile_is_listed_without_its_score_and_does_not_climb(
        ward_record_boot):
    # Listed, so it can still be closed with a reason; the marker check above
    # proves its stored drivers and sentence are not sent.
    assert ward_record_boot["escalations_listed"] == 1
    assert ward_record_boot["escalation_tier"] == 1


def test_the_background_lanes_ran_clean_under_the_profile(ward_record_boot):
    assert not ward_record_boot["lane_errors"], _report(ward_record_boot["lane_errors"])


# ── Models off (6 October 2026) ──────────────────────────────────────────────
# The probe makes every way to a model fail and records it: services/
# ollama_gateway post and stream, any httpx or urllib request to the Ollama
# address (the provider seams, the warm-up and residency pings, config's model
# lookup at import), and the knowledge worker pool. Whisper and the in-process
# embedder refuse through services/model_switch before they load.

@pytest.fixture(scope="module")
def models_off_boot():
    return run_probe({"ORB_MODELS_ENABLED": "false"})


def test_no_model_is_called_under_the_profile(ward_record_boot):
    assert not ward_record_boot["model_calls"], _report(ward_record_boot["model_calls"])


def test_the_models_switch_alone_boots_with_scoring_on(models_off_boot):
    assert models_off_boot["profile"] == {"ward_record": False, "scoring": True, "models": False}
    assert models_off_boot["calls"] > 300


def test_no_model_is_called_with_the_models_switched_off(models_off_boot):
    assert not models_off_boot["model_calls"], _report(models_off_boot["model_calls"])


def test_no_lane_asks_the_gateway_with_the_models_switched_off(models_off_boot, ward_record_boot):
    # The gateway refuses with the models off, so a lane that still asks it
    # makes no model call; but it was not switched off where it starts (the
    # critical alert's recommendation sentence took a queue slot and asked).
    for boot in (models_off_boot, ward_record_boot):
        assert not boot["gateway_asked"], _report(boot["gateway_asked"])


def test_no_route_answers_500_with_the_models_switched_off(models_off_boot):
    assert not models_off_boot["server_errors"], _report(models_off_boot["server_errors"])
    assert not models_off_boot["lane_errors"], _report(models_off_boot["lane_errors"])


@pytest.mark.parametrize("path", [
    "/api/sage/ward-brief", "/api/sage/insights", "/api/scribe/asr/status",
    "/api/vigil/patients/{patient_id}/timeline-projection",
    "/api/relay/channels/none/sage_actions",
])
def test_the_model_features_answer_switched_off(models_off_boot, ward_record_boot, path):
    for boot in (models_off_boot, ward_record_boot):
        assert boot["spot"][path] == [404, "Switched off on this computer"], (path, boot["spot"][path])


def test_the_handover_is_switched_off_only_under_the_profile(models_off_boot, ward_record_boot):
    path = "/api/ipass/{patient_id}/draft"
    assert ward_record_boot["spot"][path] == [404, "The handover is switched off on this computer"]
    assert models_off_boot["spot"][path][0] == 200


def test_the_model_status_chip_is_told_the_models_are_off(ward_record_boot, models_off_boot):
    # Review, 9 October 2026: this checked only that the route answered.
    for boot in (ward_record_boot, models_off_boot):
        assert boot["spot"]["/api/health/models"] == [200, False]
