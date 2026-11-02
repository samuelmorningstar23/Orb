"""Accounting and clinical-configuration guards; no GPU, network or models."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import benchmark_mlx_ollama as benchmark
import serve_mlx_benchmark as server


def config():
    return {"num_hidden_layers": 62, "sliding_window_pattern": 6,
            "num_key_value_heads": 16, "head_dim": 128, "sliding_window": 1024}


def test_native_global_and_local_cache_budget_keeps_full_context():
    assert benchmark.mlx_kv_bytes(config(), 1) == 1_107_296_256
    assert benchmark.mlx_kv_bytes({"text_config": config()}, 8) == 8_858_370_048
    assert benchmark.mlx_kv_bytes(config(), 1, element_bytes=4) == 2_214_592_512
    for value in (1, 3, 8, True):
        with pytest.raises(ValueError):
            benchmark.mlx_kv_bytes(config(), 1, element_bytes=value)


@pytest.mark.parametrize("key,value", [("sliding_window", 512), ("sliding_window_pattern", 0),
                                      ("num_hidden_layers", 61), ("num_key_value_heads", True)])
def test_cache_preflight_refuses_wrong_architecture(key, value):
    cfg = config()
    cfg[key] = value
    with pytest.raises(ValueError):
        benchmark.mlx_kv_bytes(cfg, 1)


def test_worker_count_is_actual_requested_native_width():
    import json
    settings = benchmark.worker_settings("mlx", 8)
    assert json.loads(settings["ORB_LLM_WORKERS"])[0]["capacity"] == 8
    assert settings["ORB_LLM_POOL_MAX_INFLIGHT"] == "200"


def test_offline_environment_removes_experimental_runtime_overrides(monkeypatch):
    for key in ("MLX_METAL_FAST_SYNCH", "MLX_UNSAFE_SETTING", "OLLAMA_GO_TEMPLATE", "HTTPS_PROXY"):
        monkeypatch.setenv(key, "unsafe-or-uncontrolled")
    monkeypatch.setenv("DYLD_LIBRARY_PATH", "/installed/local/mlx/lib")
    env = benchmark.offline_environment()
    assert not any(key.startswith(("MLX_", "OLLAMA_")) or key.endswith("_PROXY") for key in env)
    assert env["DYLD_LIBRARY_PATH"] == "/installed/local/mlx/lib"
    assert env["HF_HUB_OFFLINE"] == env["TRANSFORMERS_OFFLINE"] == "1"


def test_unused_frameworks_disabled_only_in_mlx_child_not_retrieval():
    env = {"HF_HUB_OFFLINE": "1"}
    assert benchmark.inference_environment(env, "mlx") == {
        "HF_HUB_OFFLINE": "1", "USE_TORCH": "0", "USE_TF": "0", "USE_FLAX": "0"}
    assert benchmark.inference_environment(env, "ollama") == env
    assert env == {"HF_HUB_OFFLINE": "1"}


def test_candidate_policy_cannot_describe_a_different_conversion(tmp_path, monkeypatch):
    import json

    import mlx_ollama_benchmark_proxy as proxy
    manifest = {"status": "converted_candidate_unqualified", "candidate_label": "new-eight-bit-candidate",
                "source_sha256": benchmark.candidate.MODEL_DIGEST.removeprefix("sha256:"),
                "clinical_quality_qualified": False, "files": {"metadata": {"bytes": 0, "sha256": "unused"}}}
    (tmp_path / "conversion-manifest.json").write_text(json.dumps(manifest))
    monkeypatch.setattr(proxy, "load_policy", lambda *a, **k: {"conversion_label": "old-mixed-candidate"})
    with pytest.raises(ValueError, match="provenance"):
        benchmark.validate_mlx_candidate(tmp_path, tmp_path / "policy.json")


def test_lease_profile_preserves_distinct_candidate_paths(tmp_path):
    import benchmark_model_residency_lease as lease
    selected = lease.profile_args(tmp_path / "evidence", "mlx", 1, ["quality", "replay"],
        model_dir=tmp_path / "affine-eight-bit", policy=tmp_path / "eight-bit-policy.json",
        loading_mode="staged-uncached")
    assert selected.model_dir == tmp_path / "affine-eight-bit"
    assert selected.mlx_policy == tmp_path / "eight-bit-policy.json"
    assert selected.mlx_loading_mode == "staged-uncached"


@pytest.mark.parametrize("swa_full", ["on", "off"])
@pytest.mark.parametrize("prefix_cache_mib", [0, 8192])
def test_native_swa_storage_option_is_explicit_and_preserved_by_lease_profile(tmp_path, swa_full, prefix_cache_mib):
    import benchmark_model_residency_lease as lease

    selected = lease.profile_args(tmp_path / "evidence", "ollama", 2,
        ["quality", "sequential", "burst"], full_rag=True, native_swa_full=swa_full,
        native_prefix_cache_mib=prefix_cache_mib)
    assert selected.native_swa_full == swa_full
    assert selected.native_prefix_cache_mib == prefix_cache_mib
    assert selected.phases == ["quality", "sequential", "burst"]
    assert selected.workflow_quality_checks and not selected.engine_only


def test_native_swa_default_remains_full_size_and_invalid_choice_is_refused(tmp_path):
    arguments = ["--engine", "ollama", "--output-dir", str(tmp_path / "evidence")]
    assert benchmark.parser().parse_args(arguments).native_swa_full == "on"
    assert benchmark.parser().parse_args(arguments).native_prefix_cache_mib == 8192
    with pytest.raises(SystemExit):
        benchmark.parser().parse_args([*arguments, "--native-swa-full", "smaller-context"])
    with pytest.raises(SystemExit):
        benchmark.parser().parse_args([*arguments, "--native-prefix-cache-mib", "4096"])


@pytest.mark.parametrize("swa_full", ["on", "off"])
@pytest.mark.parametrize("prefix_cache_mib", [0, 8192])
def test_native_swa_passes_candidate_and_records_storage_without_changing_context_or_memory_guard(tmp_path, monkeypatch, swa_full, prefix_cache_mib):
    import asyncio
    import json
    from types import SimpleNamespace

    args = benchmark.parser().parse_args(["--engine", "ollama", "--engine-only",
        "--parallel", "2", "--phases", "quality", "--native-swa-full", swa_full,
        "--native-prefix-cache-mib", str(prefix_cache_mib),
        "--output-dir", str(tmp_path / "evidence"), "--allow-model-load"])
    snapshot = benchmark.candidate.ModelSnapshot("medgemma:27b", benchmark.candidate.MODEL_DIGEST,
                                               17_397_211_520, "manifest", "Q4_K_M")
    memory = benchmark.candidate.MemorySnapshot(64 * benchmark.candidate.GIB, 80, 60 * benchmark.candidate.GIB,
        "Mach Virtual Memory Statistics: (page size of 16384 bytes)\nSwapouts: 0.\nPages occupied by compressor: 0.\n")
    monkeypatch.setattr(benchmark.candidate, "inspect_memory", lambda: memory)
    monkeypatch.setattr(benchmark.candidate, "inspect_model", lambda _: snapshot)
    monkeypatch.setattr(benchmark.capacity, "hardware_metadata", dict)
    monkeypatch.setattr(benchmark.qualification, "install_synthetic_answer_capture", lambda: None)
    ports = iter((55001, 55002))
    monkeypatch.setattr(benchmark.qualification, "free_port", lambda: next(ports))
    commands = []
    class Process:
        pid, returncode = 1, None
        def __init__(self, command, **kwargs):
            commands.append(command)
        def poll(self):
            return self.returncode
    monkeypatch.setattr(benchmark.subprocess, "Popen", Process)
    async def ready(*args):
        raise benchmark.qualification.QualificationAborted("Synthetic stop before model load")
    async def pressure(*args, **kwargs):
        await asyncio.Event().wait()
    async def terminate(process):
        if process is not None:
            process.returncode = 0
    monkeypatch.setattr(benchmark, "native_ready", ready)
    monkeypatch.setattr(benchmark.qualification, "monitor_pressure", pressure)
    monkeypatch.setattr(benchmark.qualification, "terminate_owned", terminate)
    class ClosedSocket:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return None
        def settimeout(self, value):
            pass
        def connect_ex(self, address):
            return 1
    monkeypatch.setattr(benchmark, "socket", SimpleNamespace(socket=ClosedSocket))
    assert asyncio.run(benchmark.run(args)) == 1
    assert len(commands) == 1
    assert commands[0][commands[0].index("--swa-full") + 1] == swa_full
    assert commands[0][commands[0].index("--native-prefix-cache-mib") + 1] == str(prefix_cache_mib)
    report = json.loads((args.output_dir / "report.json").read_text())
    info = report["profile"]["preflight"]["model"]
    assert info["swa_full"] == swa_full and info["context_per_request"] == 8192 and info["kv_type"] == "f16"
    assert info["prefix_cache_mib"] == prefix_cache_mib
    assert info["idle_slot_prompt_cache"] == info["prompt_cache_requested"] == bool(prefix_cache_mib)
    assert info["model_digest"] == benchmark.candidate.MODEL_DIGEST
    assert report["profile"]["preflight"]["required_available_bytes_estimate"] == benchmark.candidate.required_available_bytes(snapshot, 2)
    assert not report["strict_200_latency_targets_demonstrated"]
    assert not report["clinical_equivalence_qualified"] and not report["production_purchase_gate_passed"]


def test_loading_mode_must_match_explicit_candidate_policy(tmp_path, monkeypatch):
    import mlx_ollama_benchmark_proxy as proxy
    monkeypatch.setattr(proxy, "load_policy", lambda *a, **k: {"loading_mode": "standard"})
    with pytest.raises(ValueError, match="loading mode"):
        benchmark.validate_mlx_candidate(tmp_path, tmp_path / "policy.json", loading_mode="staged-uncached")


@pytest.mark.parametrize("mode", ["trace-standard", "trace-staged", "precision-standard", "precision-staged",
                                 "serial-standard", "serial-staged", "f32-standard", "f32-staged",
                                 "mixed-standard", "mixed-staged"])
def test_trace_policy_pins_every_instrumentation_source(tmp_path, monkeypatch, mode):
    import hashlib
    import json

    import mlx_ollama_benchmark_proxy as proxy
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    sources = {"loading_wrapper_sha256": benchmark.MLX_LAUNCHERS[mode],
               "startup_trace_sha256": "mlx_startup_trace.py"}
    if mode.startswith(("precision-", "serial-", "f32-")):
        sources["precision_repair_sha256"] = "mlx_gemma3_precision.py"
        sources["trace_launcher_sha256"] = "serve_mlx_benchmark_trace.py"
        sources["prompt_trace_sha256"] = "mlx_prompt_token_trace.py"
    if mode.startswith(("serial-", "f32-")):
        sources["precision_launcher_sha256"] = "serve_mlx_benchmark_precision.py"
    if mode.startswith("f32-"):
        sources["f32_embedding_sha256"] = "mlx_gemma3_f32_embedding.py"
    if mode.startswith("mixed-"):
        sources.update({"f32_embedding_sha256": "mlx_gemma3_f32_embedding.py",
                        "mixed_projection_sha256": "mlx_gemma3_mixed_precision.py",
                        "http_backlog_sha256": "mlx_http_backlog.py",
                        "allocator_budget_sha256": "mlx_allocator_budget.py",
                        "serving_scheduler_sha256": "mlx_serving_scheduler.py",
                        "prompt_trace_sha256": "mlx_prompt_token_trace.py",
                        "trace_launcher_sha256": "serve_mlx_benchmark_trace.py"})
    if mode.endswith("-staged"):
        sources["traced_inner_wrapper_sha256"] = "serve_mlx_benchmark_staged.py"
    policy = {"loading_mode": mode, "conversion_label": "synthetic"}
    for key, name in sources.items():
        data = name.encode()
        (scripts / name).write_bytes(data)
        policy[key] = hashlib.sha256(data).hexdigest()
    model = tmp_path / "model"
    model.mkdir()
    (model / "metadata").write_bytes(b"x")
    (model / "conversion-manifest.json").write_text(json.dumps({
        "status": "converted_candidate_unqualified", "candidate_label": "synthetic",
        "source_sha256": benchmark.candidate.MODEL_DIGEST.removeprefix("sha256:"),
        "clinical_quality_qualified": False,
        "matrices": [{"native_bits": 4} for _ in range(435)],
        "files": {"metadata": {"bytes": 1, "sha256": hashlib.sha256(b"x").hexdigest()}}}))
    monkeypatch.setattr(benchmark, "ROOT", tmp_path)
    monkeypatch.setattr(proxy, "load_policy", lambda *a, **k: policy)
    assert benchmark.validate_mlx_candidate(model, tmp_path / "policy", loading_mode=mode)
    for name in sources.values():
        path = scripts / name
        data = path.read_bytes()
        path.write_bytes(data + b"changed")
        with pytest.raises(ValueError, match="instrumentation"):
            benchmark.validate_mlx_candidate(model, tmp_path / "policy", loading_mode=mode)
        path.write_bytes(data)


def test_large_weight_checksum_uses_uncached_fd_without_changing_hash(tmp_path, monkeypatch):
    import fcntl
    import hashlib
    import json

    import mlx_ollama_benchmark_proxy as proxy
    weight = tmp_path / "synthetic.safetensors"
    payload = b"x" * 1024 ** 2
    weight.write_bytes(payload)
    manifest = {"status": "converted_candidate_unqualified", "candidate_label": "synthetic-candidate",
                "source_sha256": benchmark.candidate.MODEL_DIGEST.removeprefix("sha256:"),
                "clinical_quality_qualified": False, "files": {weight.name: {
                    "bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}}}
    (tmp_path / "conversion-manifest.json").write_text(json.dumps(manifest))
    monkeypatch.setattr(proxy, "load_policy", lambda *a, **k: {"conversion_label": "synthetic-candidate"})
    monkeypatch.setattr(benchmark.sys, "platform", "darwin")
    calls = []
    monkeypatch.setattr(fcntl, "fcntl", lambda *args: calls.append(args))
    monkeypatch.setattr(fcntl, "F_NOCACHE", 48, raising=False)
    result = benchmark.validate_mlx_candidate(tmp_path, tmp_path / "policy.json")
    assert len(calls) == 1 and calls[0][1:] == (48, 1)
    assert result["benchmark_checksum_io"]["large_safetensors_uncached_fd_count"] == 1
    assert weight.read_bytes() == payload


def report(phase, **runtime_changes):
    runtime = dict(embedding_calls=134, embedding_completed=134, rerank_calls=134,
                   rerank_completed=134, embedding_failed=0, rerank_failed=0,
                   results_lexical=0, results_other=0, results_cross_encoder=1608)
    runtime.update(runtime_changes)
    return {"status": "complete", "profile": {"phases": [fidelity_phase(), phase],
            "final_fixture_metadata": {"retrieval_runtime": runtime}}}


def fidelity_phase():
    from llm_fidelity_cases import CASES
    return {"name": "quality", "status": "complete", "total": 8,
            "completed_cases": 8, "successful": 8, "all_cases_passed": True,
            "cases": [{"case_id": case.case_id, "completed": True,
                       "terminal_stop_seen": True, "passed": True} for case in CASES]}


def successful_phase():
    return {"name": "burst", "status": "complete", "requests": [
        {"success": True, "completed_within_target": True, "model": "medgemma:27b",
         "cache_hit": False} for _ in range(200)],
        "summary": {"requests": 200, "failed": 0,
                    "all_ai_requests_meet_completed_answer_targets": True}}


def test_latency_success_never_awards_clinical_or_purchase_qualification():
    result = benchmark.acceptance(report(successful_phase()))
    assert result["strict_200_latency_targets_demonstrated"]
    assert not result["clinical_equivalence_qualified"]
    assert not result["production_purchase_gate_passed"]


@pytest.mark.parametrize("change", ["missing_phase", "aborted", "missing_case", "duplicate_case",
                                    "unknown_case", "wrong_total", "wrong_completed", "wrong_successful",
                                    "bool_count", "missing_all_flag", "false_all_flag", "nonboolean_all_flag",
                                    "case_incomplete", "case_unterminated", "case_failed", "case_nonboolean",
                                    "summary_only"])
def test_latency_cannot_qualify_without_eight_actual_completed_fidelity_cases(change):
    document = report(successful_phase())
    phase = document["profile"]["phases"][0]
    if change == "missing_phase":
        document["profile"]["phases"].pop(0)
    elif change == "aborted":
        phase["status"] = "aborted_incomplete"
    elif change == "missing_case":
        phase["cases"].pop()
    elif change == "duplicate_case":
        phase["cases"][1]["case_id"] = phase["cases"][0]["case_id"]
    elif change == "unknown_case":
        phase["cases"][1]["case_id"] = "unknown-case"
    elif change.startswith("wrong_"):
        phase[{"wrong_total": "total", "wrong_completed": "completed_cases",
               "wrong_successful": "successful"}[change]] = 7
    elif change == "bool_count":
        phase["total"] = True
    elif change == "missing_all_flag":
        phase.pop("all_cases_passed")
    elif change == "false_all_flag":
        phase["all_cases_passed"] = False
    elif change == "nonboolean_all_flag":
        phase["all_cases_passed"] = "true"
    elif change == "summary_only":
        phase.pop("cases")
    else:
        key = {"case_incomplete": "completed", "case_unterminated": "terminal_stop_seen",
               "case_failed": "passed", "case_nonboolean": "passed"}[change]
        phase["cases"][0][key] = "true" if change == "case_nonboolean" else False
    result = benchmark.acceptance(document)
    assert not result["strict_200_latency_targets_demonstrated"]
    assert not result["clinical_equivalence_qualified"]
    assert not result["production_purchase_gate_passed"]


def phase_fixture(monkeypatch, *, passed=False):
    from types import SimpleNamespace

    import llm_fidelity_cases

    async def warm(_origin):
        return {"wall_s": .01}
    async def cases(_origin, result_observer):
        result = fidelity_phase()
        result.pop("name")
        result.pop("status")
        if not passed:
            result["cases"][0]["passed"] = False
            result.update(successful=7, all_cases_passed=False)
        for row in result["cases"]:
            result_observer(row)
        return result
    class Client:
        def __init__(self, **kwargs):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            return None
        async def get(self, *args, **kwargs):
            return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {
                "retrieval_executor": {"workers": 4}, "full_retrieval_proof": {"verified": True}})
    monkeypatch.setattr(benchmark.qualification, "warm_model", warm)
    monkeypatch.setattr(llm_fidelity_cases, "run_cases", cases)
    monkeypatch.setattr(benchmark.httpx, "AsyncClient", Client)
    def forbidden(*args, **kwargs):
        pytest.fail("Timing must not start without a passed full workflow fidelity gate")
    monkeypatch.setattr(benchmark.capacity, "run_burst", forbidden)
    return SimpleNamespace(engine="ollama", parallel=2, workflow_quality_checks=True,
                           phases=["quality", "sequential", "burst"])


def test_original_native_full_workflow_stops_before_timing_on_failed_quality(monkeypatch):
    import asyncio
    args = phase_fixture(monkeypatch)
    profile = {"phases": []}
    with pytest.raises(benchmark.qualification.QualificationAborted, match="fidelity_gate_failed"):
        asyncio.run(benchmark.phases("owned-origin", args, profile,
            prepared_fixture=("owned-fixture", {"summary_expectations": {"patient": {}}}, None)))
    assert len(profile["phases"]) == 1
    assert profile["phases"][0]["status"] == "complete"
    assert profile["phases"][0]["successful"] == 7


def test_original_native_full_workflow_without_quality_phase_refuses_before_timing(monkeypatch):
    import asyncio
    args = phase_fixture(monkeypatch, passed=True)
    args.phases = ["sequential", "burst"]
    profile = {"phases": []}
    with pytest.raises(benchmark.qualification.QualificationAborted, match="fidelity_gate_missing"):
        asyncio.run(benchmark.phases("owned-origin", args, profile,
            prepared_fixture=("owned-fixture", {"summary_expectations": {"patient": {}}}, None)))
    assert profile["phases"] == []


def test_original_native_engine_only_keeps_failed_quality_diagnostic_and_continues_replay(monkeypatch):
    import asyncio

    import replay_llm_benchmark

    args = phase_fixture(monkeypatch)
    args.phases, args.replay_captures = ["quality", "replay"], "synthetic-captured-requests"
    observed = []
    async def replay(origin, captures, observer):
        observed.append((origin, captures))
        return {"requests": []}
    monkeypatch.setattr(replay_llm_benchmark, "replay", replay)
    profile = {"phases": []}
    asyncio.run(benchmark.engine_only_phases("owned-origin", args, profile))
    assert profile["phases"][0]["all_cases_passed"] is False
    assert profile["phases"][1]["status"] == "complete"
    assert observed == [("owned-origin", "synthetic-captured-requests")]


@pytest.mark.parametrize("change", ["missing", "failure", "late", "cache", "wrong_model", "aborted"])
def test_no_accepted_partial_or_failed_cohort(change):
    phase = successful_phase()
    if change == "missing":
        phase["requests"].pop()
    elif change == "failure":
        phase["requests"][0]["success"] = False
    elif change == "late":
        phase["requests"][0]["completed_within_target"] = False
    elif change == "cache":
        phase["requests"][0]["cache_hit"] = True
    elif change == "wrong_model":
        phase["requests"][0]["model"] = "medgemma:4b"
    else:
        phase["status"] = "aborted_incomplete_not_acceptance_evidence"
    assert not benchmark.acceptance(report(phase))["strict_200_latency_targets_demonstrated"]


def test_degraded_or_incomplete_retrieval_cannot_qualify():
    for changes in ({"results_lexical": 1}, {"rerank_completed": 133}, {"embedding_failed": 1}):
        assert not benchmark.acceptance(report(successful_phase(), **changes))["strict_200_latency_targets_demonstrated"]


def test_direct_engine_cohort_cannot_award_orb_sla():
    phase = successful_phase()
    phase["name"] = "replay200"
    assert not benchmark.acceptance(report(phase))["strict_200_latency_targets_demonstrated"]


@pytest.mark.parametrize("phases", [["replay"], ["replay200", "quality"]])
def test_mlx_quality_must_precede_timing_before_any_model_load(tmp_path, monkeypatch, phases):
    import asyncio
    args = benchmark.parser().parse_args(["--engine", "mlx", "--engine-only",
        "--phases", *phases, "--output-dir", str(tmp_path / "unused"),
        "--allow-model-load", "--allow-heavy-model-load"])
    def forbidden():
        pytest.fail("Must refuse before inspecting or loading any model")
    monkeypatch.setattr(benchmark.candidate, "inspect_memory", forbidden)
    with pytest.raises(ValueError, match="pass fidelity before timing"):
        asyncio.run(benchmark.run(args))


class SamplerMethods:
    def __init__(self):
        self.calls = []
    def greedy_sampler(self, value):
        return "greedy"
    def apply_top_k(self, value, k):
        self.calls.append(("k", k)); return value
    def apply_top_p(self, value, p):
        self.calls.append(("p", p)); return value
    def categorical_sampling(self, value, temp):
        self.calls.append(("temperature", temp)); return "sample"


def test_pinned_order_and_temperature_preserved():
    methods = SamplerMethods()
    # Native server._make_sampler passes temperature positionally.
    sample = server.sampler_factory(methods, .4, top_k=40, top_p=.9)
    assert sample("logits") == "sample"
    assert methods.calls == [("k", 40), ("p", .9), ("temperature", .4)]


def test_greedy_quality_probe_does_not_sample():
    methods = SamplerMethods()
    assert server.sampler_factory(methods, temp=0, top_k=40, top_p=.9)(None) == "greedy"
    assert not methods.calls


@pytest.mark.parametrize("kwargs", [{"xtc_probability": .2}, {"min_p": .1}, {"min_tokens_to_keep": 2}])
def test_wrapper_does_not_ignore_unimplemented_sampler_policy(kwargs):
    with pytest.raises(ValueError):
        server.sampler_factory(SamplerMethods(), temp=.4, **kwargs)
