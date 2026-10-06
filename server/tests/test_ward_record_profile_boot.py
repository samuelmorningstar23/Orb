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


def test_the_background_lanes_ran_clean_under_the_profile(ward_record_boot):
    assert not ward_record_boot["lane_errors"], _report(ward_record_boot["lane_errors"])
