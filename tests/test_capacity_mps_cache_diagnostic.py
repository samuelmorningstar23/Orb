"""Content-free allocator log extraction only; no GPU, worker or model."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from loadtest_capacity import extract_mps_cache_events, mps_cache_diagnostic

LINE = ("MPS unused-cache release driver_before_bytes=5000 live_before_bytes=1000 "
        "driver_after_bytes=2000 live_after_bytes=1000 released_bytes=3000 trigger_bytes=1024")


@pytest.mark.parametrize("prefix", ["", "WARNING:orb.knowledge_worker:"])
def test_only_exact_numeric_worker_event_is_extracted(prefix):
    report = extract_mps_cache_events([prefix + LINE + "\n"])
    assert report["observation_status"] == "numeric_events_only"
    assert report["total_records"] == 1 and report["total_released_bytes"] == 3000
    assert report["records"] == [{"driver_before_bytes": 5000, "live_before_bytes": 1000,
        "driver_after_bytes": 2000, "live_after_bytes": 1000, "released_bytes": 3000, "trigger_bytes": 1024}]
    assert all(type(value) is int for value in report["records"][0].values())


@pytest.mark.parametrize("line", [
    LINE + " patient=PRIVATE-NAME", "PRIVATE-NAME " + LINE,
    "WARNING:another.logger:" + LINE, LINE.replace("5000", "-5000"),
    LINE.replace("5000", "5e3"), LINE.replace("5000", "5000.0"),
    LINE.replace("5000", "NaN"), LINE.replace("5000", "99999999999999999999"),
    LINE.replace("5000", "9223372036854775808"),
    LINE.replace("released_bytes=3000", "released_bytes=4000"),
    LINE.replace("live_before_bytes=1000", "live_before_bytes=6000"),
    LINE.replace("live_after_bytes=1000", "live_after_bytes=6000"),
    LINE.replace("driver_before_bytes", "unexpected_field"),
    LINE.replace(" trigger_bytes=1024", ""), "MPS unused-cache cleanup unavailable error_type=RuntimeError",
    "/private/patient-data/PRIVATE-NAME: secret medical record", "x" * 513, None,
])
def test_malformed_or_unrelated_lines_are_discarded_without_retaining_any_text(line):
    report = extract_mps_cache_events([line])
    assert report["records"] == [] and report["total_records"] == 0
    assert report["total_released_bytes"] == 0
    encoded = json.dumps(report)
    assert "PRIVATE-NAME" not in encoded and "patient-data" not in encoded and "RuntimeError" not in encoded


def test_bounded_records_keep_total_count_and_released_sum_for_all_events():
    report = extract_mps_cache_events(LINE for _ in range(300))
    assert len(report["records"]) == 256 and report["records_truncated"]
    assert report["total_records"] == 300 and report["total_released_bytes"] == 900_000


def test_owned_log_observation_preserves_only_numeric_events(tmp_path):
    (tmp_path / "worker.log").write_text("PRIVATE-NAME confidential medical note\n" + LINE + "\n")
    report = mps_cache_diagnostic(tmp_path)
    assert report["total_records"] == 1
    assert "PRIVATE-NAME" not in json.dumps(report) and str(tmp_path) not in json.dumps(report)


def test_log_observation_failure_cannot_mask_original_benchmark_error_or_leak_exception(tmp_path, monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("PRIVATE-NAME /private/patient-data")
    monkeypatch.setattr(Path, "open", fail)
    try:
        raise RuntimeError("original benchmark failure")
    except RuntimeError as original:
        report = mps_cache_diagnostic(tmp_path)
        assert str(original) == "original benchmark failure"
    assert report["observation_status"] == "unavailable" and report["records"] == []
    assert "PRIVATE-NAME" not in json.dumps(report) and "patient-data" not in json.dumps(report)
