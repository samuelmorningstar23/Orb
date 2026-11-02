"""CPU fakes verify tracing without importing MLX, loading models or sockets."""
from __future__ import annotations

import importlib.util
import json
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/mlx_startup_trace.py"
SPEC = importlib.util.spec_from_file_location("orb_mlx_startup_trace_test", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
trace: Any = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = trace
SPEC.loader.exec_module(trace)


class FakeArray:
    def __init__(self, nbytes: int, dtype: str):
        self.nbytes, self.dtype = nbytes, dtype

    def item(self) -> None:
        raise AssertionError("Tracing must not read array values")

    def __repr__(self) -> str:
        raise AssertionError("Tracing must not represent array values")


class Runtime:
    def __init__(self):
        self.operations: list[str] = []
        self.metrics = {"active": 0, "cache": 7, "peak": 0}
        self.files: Any = {"private_tensor_name": FakeArray(128, "mlx.core.uint32"),
                      "private_scale_name": FakeArray(32, "mlx.core.float16")}
        self.load_results: list[Any] = []
        runtime = self

        class Module:
            def __init__(self, _args: Any = None):
                runtime.operations.append("construct")
                self.weights: dict[str, Any] = {}

            def load_weights(self, values: Any, *, strict: bool = True) -> Module:
                runtime.operations.append("attach")
                assert strict is True
                self.weights = dict(values)
                return self

            def parameters(self) -> Any:
                return self.weights

        def load(file: Any, *args: Any, **kwargs: Any) -> Any:
            runtime.operations.append("load")
            result = runtime.files
            runtime.load_results.append((file, args, kwargs, result))
            return result

        def evaluate(*args: Any, **kwargs: Any) -> str:
            runtime.operations.append("eval")
            runtime.metrics["active"] += 160
            runtime.metrics["peak"] = max(runtime.metrics["peak"], runtime.metrics["active"])
            return "original-eval-result"

        def quantize(*args: Any, **kwargs: Any) -> str:
            runtime.operations.append("quantize")
            return "original-quantize-result"

        def classes(*args: Any, **kwargs: Any) -> Any:
            runtime.operations.append("classes")
            return Module, lambda: "original-model-arguments"

        def load_model(model_path: Any, lazy: bool = False, strict: bool = True,
                       model_config: Any = None, get_model_classes: Any = classes,
                       trust_remote_code: bool = False) -> Any:
            weights = runtime.mx.load(model_path)
            model_class, argument_class = get_model_classes(config=model_config)
            model = model_class(argument_class())
            runtime.nn.quantize(model, bits=4)
            model.load_weights(list(weights.items()), strict=strict)
            if not lazy:
                runtime.mx.eval(model.parameters())
            return model, {"unchanged": True, "trust_remote_code": trust_remote_code}

        def forbidden(*_args: Any, **_kwargs: Any) -> Any:
            raise AssertionError("Trace performed an extra device/cache mutation")

        self.mx = SimpleNamespace(array=FakeArray, load=load, eval=evaluate,
            get_active_memory=lambda: self.metrics["active"],
            get_cache_memory=lambda: self.metrics["cache"],
            get_peak_memory=lambda: self.metrics["peak"],
            clear_cache=forbidden, synchronize=forbidden, set_cache_limit=forbidden,
            set_memory_limit=forbidden, set_wired_limit=forbidden)
        self.nn = SimpleNamespace(Module=Module, quantize=quantize)
        self.utils = SimpleNamespace(load_model=load_model)
        self.classes = classes


def install(runtime: Runtime, rows: list[dict[str, Any]], **options: Any) -> Any:
    return trace.install_startup_trace(runtime.mx, runtime.nn, runtime.utils, rows.append,
        max_rss_reader=lambda: 1000, current_rss_reader=lambda: 900, **options)


def test_exact_original_calls_values_order_and_automatic_restoration(tmp_path: Path) -> None:
    runtime, rows = Runtime(), list[dict[str, Any]]()
    folder = tmp_path / "Patient-Synth-Secret"
    folder.mkdir()
    path = folder / "model-00001.safetensors"
    path.write_bytes(b"tiny synthetic bytes")
    originals = (runtime.mx.load, runtime.mx.eval, runtime.nn.quantize,
                 runtime.nn.Module.load_weights, runtime.utils.load_model)
    handle = install(runtime, rows)
    model, config = runtime.utils.load_model(path, model_config={"private_config": "not emitted"})
    assert type(model) is runtime.nn.Module
    assert config == {"unchanged": True, "trust_remote_code": False}
    assert runtime.operations == ["load", "classes", "construct", "quantize", "attach", "eval"]
    assert model.weights["private_tensor_name"] is runtime.files["private_tensor_name"]
    assert runtime.load_results[0] == (path, (), {}, runtime.files)
    assert (runtime.mx.load, runtime.mx.eval, runtime.nn.quantize,
            runtime.nn.Module.load_weights, runtime.utils.load_model) == originals
    events = [row["event"] for row in rows]
    assert events == ["model_load_enter", "weight_file_enter", "weight_file_exit",
        "model_construct_enter", "model_construct_exit", "quantize_enter", "quantize_exit",
        "weight_attach_enter", "weight_attach_exit", "eval_enter", "eval_exit",
        "model_load_exit", "hooks_restored"]
    final = next(row for row in rows if row["event"] == "model_load_exit")
    assert final["counts"] == {"file_load_calls": 1, "file_load_errors": 0,
        "declared_file_bytes": path.stat().st_size, "declared_array_nbytes": 160,
        "array_occurrences": 2, "dtype_array_counts": {"mlx.core.uint32": 1, "mlx.core.float16": 1},
        "dtype_declared_nbytes": {"mlx.core.uint32": 128, "mlx.core.float16": 32},
        "eval_calls": 1, "eval_errors": 0, "quantize_calls": 1,
        "weight_attachment_calls": 1, "constructor_calls": 1}
    assert final["memory"] == {"mlx_active_bytes": 160, "mlx_cache_bytes": 7,
        "mlx_peak_bytes": 160, "process_max_rss_bytes": 1000, "process_current_rss_bytes": 900}
    text = json.dumps(rows)
    assert "Patient-Synth-Secret" not in text and "private_tensor_name" not in text
    assert "private_config" not in text and str(tmp_path) not in text
    assert next(row for row in rows if row["event"] == "weight_file_enter")["file_basename"] == path.name
    before = len(rows)
    handle.close()
    runtime.utils.load_model(path)
    assert len(rows) == before  # No inference/hot-path tracing after the first startup.


def test_no_model_call_closes_context_and_no_device_mutations() -> None:
    runtime, rows = Runtime(), list[dict[str, Any]]()
    original = runtime.utils.load_model
    with install(runtime, rows):
        assert runtime.utils.load_model is not original
    assert runtime.utils.load_model is original
    assert runtime.operations == []
    assert [row["event"] for row in rows] == ["hooks_restored"]


@pytest.mark.parametrize("which", ["load", "construct", "quantize", "attach", "eval"])
def test_original_error_identity_restoration_and_private_message_not_emitted(
        tmp_path: Path, which: str) -> None:
    runtime, rows = Runtime(), list[dict[str, Any]]()
    failure = ValueError("Patient-Synth-Secret at /private/sensitive/path")

    def fail(*_args: Any, **_kwargs: Any) -> Any:
        raise failure

    if which == "load":
        runtime.mx.load = fail
    elif which == "construct":
        runtime.classes = lambda **_kwargs: (fail, lambda: None)
    elif which == "quantize":
        runtime.nn.quantize = fail
    elif which == "attach":
        runtime.nn.Module.load_weights = fail
    else:
        runtime.mx.eval = fail
    originals = (runtime.mx.load, runtime.mx.eval, runtime.nn.quantize,
                 runtime.nn.Module.load_weights, runtime.utils.load_model)
    install(runtime, rows)
    with pytest.raises(ValueError) as caught:
        runtime.utils.load_model(tmp_path / "model-1.safetensors", get_model_classes=runtime.classes)
    assert caught.value is failure
    assert (runtime.mx.load, runtime.mx.eval, runtime.nn.quantize,
            runtime.nn.Module.load_weights, runtime.utils.load_model) == originals
    assert any(row["event"] == "model_load_error" for row in rows)
    assert rows[-1]["event"] == "hooks_restored"
    assert "Patient-Synth-Secret" not in json.dumps(rows)
    assert "/private/sensitive/path" not in json.dumps(rows)


def test_emission_and_memory_reader_errors_do_not_change_original_result(tmp_path: Path) -> None:
    runtime = Runtime()

    def fail(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("private diagnostics error")

    runtime.mx.get_active_memory = fail
    runtime.mx.get_cache_memory = lambda: -1
    rows: list[dict[str, Any]] = []
    handle = trace.install_startup_trace(runtime.mx, runtime.nn, runtime.utils, rows.append,
        max_rss_reader=fail, current_rss_reader=None)
    model, _config = runtime.utils.load_model(tmp_path / "missing-size.safetensors")
    assert model.weights == runtime.files
    assert rows[0]["memory"]["mlx_active_bytes"] is None
    assert rows[0]["memory"]["mlx_cache_bytes"] is None
    assert rows[0]["memory"]["process_max_rss_bytes"] is None
    assert rows[0]["memory"]["process_current_rss_bytes"] is None
    assert handle.emit_failures == 0
    runtime2 = Runtime()
    handle2 = trace.install_startup_trace(runtime2.mx, runtime2.nn, runtime2.utils, fail,
        max_rss_reader=lambda: 1, current_rss_reader=None)
    runtime2.utils.load_model(tmp_path / "model-1.safetensors")
    assert handle2.emit_failures > 0 and runtime2.operations[-1] == "eval"


def test_fileobject_metadata_does_not_read_or_seek_and_nonstandard_names_redacted(tmp_path: Path) -> None:
    runtime, rows = Runtime(), list[dict[str, Any]]()
    path = tmp_path / "private-patient-name.safetensors"
    path.write_bytes(b"tiny")

    class NoRead:
        name = str(path)

        def __init__(self, stream: Any):
            self.stream = stream

        def fileno(self) -> int:
            return self.stream.fileno()

        def read(self, *_args: Any) -> Any:
            raise AssertionError("Trace must not read weights")

        def seek(self, *_args: Any) -> Any:
            raise AssertionError("Trace must not seek weights")

    install(runtime, rows)
    with path.open("rb") as stream:
        marker = NoRead(stream)
        runtime.utils.load_model(marker)
        assert stream.tell() == 0
    file_event = next(row for row in rows if row["event"] == "weight_file_exit")
    assert file_event["file_size_bytes"] == 4 and file_event["file_basename"] is None
    assert "private-patient-name" not in json.dumps(rows)


def test_first_every25_largest_file_and_eval_observations_are_bounded(tmp_path: Path) -> None:
    runtime, rows = Runtime(), list[dict[str, Any]]()
    files = []
    for index in range(51):
        path = tmp_path / f"model-{index:05d}.safetensors"
        path.write_bytes(b"x" * (200 if index == 16 else 10))
        files.append(path)

    def load_model(_path: Any) -> Any:
        for path in files:
            runtime.mx.load(path)
            runtime.mx.eval(runtime.files)
        return "same-value"

    runtime.utils.load_model = load_model
    install(runtime, rows)
    assert runtime.utils.load_model(tmp_path) == "same-value"
    assert [row["file_index"] for row in rows if row["event"] == "weight_file_enter"] == [1, 17, 25, 50]
    assert [row["eval_index"] for row in rows if row["event"] == "eval_enter"] == [1, 25, 50]
    final = next(row for row in rows if row["event"] == "model_load_exit")
    assert final["counts"]["file_load_calls"] == final["counts"]["eval_calls"] == 51
    assert final["counts"]["declared_array_nbytes"] == 51 * 160


def test_nested_loader_hook_and_later_independent_hook_are_not_clobbered(tmp_path: Path) -> None:
    runtime, rows = Runtime(), list[dict[str, Any]]()
    original_load = runtime.mx.load
    handle = install(runtime, rows)
    observed_load = runtime.mx.load
    outer_calls = []

    def outer(file: Any, *args: Any, **kwargs: Any) -> Any:
        outer_calls.append(file)
        return observed_load(file, *args, **kwargs)

    runtime.mx.load = outer
    runtime.utils.load_model(tmp_path / "model-1.safetensors")
    assert runtime.mx.load is outer
    assert runtime.mx.load("later") is runtime.files
    assert len(outer_calls) == 2
    assert len([row for row in rows if row["event"] == "weight_file_enter"]) == 1
    runtime.mx.load = original_load
    handle.close()


def test_other_thread_operations_are_unobserved(tmp_path: Path) -> None:
    runtime, rows = Runtime(), list[dict[str, Any]]()

    def load_model(path: Any) -> Any:
        worker = threading.Thread(target=lambda: runtime.mx.load("sensitive-other-thread-file"))
        worker.start()
        worker.join()
        return runtime.mx.load(path)

    runtime.utils.load_model = load_model
    install(runtime, rows)
    runtime.utils.load_model(tmp_path / "model-1.safetensors")
    assert next(row for row in rows if row["event"] == "model_load_exit")["counts"]["file_load_calls"] == 1
    assert runtime.operations == ["load", "load"]


def test_existing_wired_limit_call_is_observed_without_extra_device_queries_or_settings(tmp_path: Path) -> None:
    runtime, rows = Runtime(), list[dict[str, Any]]()
    calls = []

    def info() -> dict[str, Any]:
        calls.append("device_info")
        return {"max_recommended_working_set_size": 48 * 1024 ** 3, "private_device_name": "not emitted"}

    def wired() -> int:
        calls.append("existing_wired_call")
        recommended = runtime.mx.device_info()["max_recommended_working_set_size"]
        assert recommended == 48 * 1024 ** 3
        calls.append("existing_setting")
        return 0

    runtime.mx.device_info = info
    server = SimpleNamespace(maybe_set_recommended_wired_limit=wired)
    original_info = runtime.mx.device_info
    handle = install(runtime, rows, server=server)
    assert server.maybe_set_recommended_wired_limit() == 0
    assert calls == ["existing_wired_call", "device_info", "existing_setting"]
    end = next(row for row in rows if row["event"] == "wired_limit_exit")
    assert end["previous_wired_limit_bytes"] == 0
    assert end["observed_recommended_working_set_bytes"] == 48 * 1024 ** 3
    assert end["elapsed_s"] is None
    runtime.utils.load_model(tmp_path / "model-1.safetensors")
    assert server.maybe_set_recommended_wired_limit is wired and runtime.mx.device_info is original_info
    assert "private_device_name" not in json.dumps(rows)
    handle.close()


def test_lazy_request_and_custom_factory_argument_preserved(tmp_path: Path) -> None:
    runtime, rows = Runtime(), list[dict[str, Any]]()
    seen = []

    def factory(*args: Any, **kwargs: Any) -> Any:
        seen.append((args, kwargs))
        return runtime.nn.Module, lambda: "same args"

    install(runtime, rows)
    model, _config = runtime.utils.load_model(tmp_path / "model-1.safetensors", True, True,
        {"configuration": "unchanged"}, factory, False)
    assert seen == [((), {"config": {"configuration": "unchanged"}})]
    assert type(model) is runtime.nn.Module
    assert "eval" not in runtime.operations
    assert next(row for row in rows if row["event"] == "model_load_exit")["counts"]["eval_calls"] == 0


def test_declared_array_count_handles_aliases_metadata_cycles_and_unsafe_dtype(tmp_path: Path) -> None:
    runtime, rows = Runtime(), list[dict[str, Any]]()
    bad = FakeArray(16, "/private/secret dtype")
    nested: dict[str, Any] = {"private_name": bad, "alias": bad}
    nested["cycle"] = nested
    runtime.files = (nested, {"metadata_secret": "never emitted"})

    def load_model(path: Any) -> Any:
        return runtime.mx.load(path)

    runtime.utils.load_model = load_model
    install(runtime, rows)
    result = runtime.utils.load_model(tmp_path / "model-1.safetensors")
    assert result is runtime.files
    counts = next(row for row in rows if row["event"] == "model_load_exit")["counts"]
    assert counts["array_occurrences"] == 1 and counts["declared_array_nbytes"] == 16
    assert counts["dtype_array_counts"] == {"other": 1}
    assert "/private/secret" not in json.dumps(rows) and "metadata_secret" not in json.dumps(rows)


def test_raw_maxrss_platform_units_and_missing_optional_rss(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(trace.resource, "getrusage", lambda _who: SimpleNamespace(ru_maxrss=321))
    monkeypatch.setattr(trace.sys, "platform", "darwin")
    assert trace.process_max_rss_bytes() == 321
    monkeypatch.setattr(trace.sys, "platform", "linux")
    assert trace.process_max_rss_bytes() == 321 * 1024


def test_absolute_monotonic_clock_can_align_stages_with_external_pressure(tmp_path: Path) -> None:
    runtime, rows = Runtime(), list[dict[str, Any]]()
    instant = [1000.0]

    def clock() -> float:
        now = instant[0]
        instant[0] += 0.25
        return now

    install(runtime, rows, clock=clock)
    runtime.utils.load_model(tmp_path / "model-1.safetensors")
    assert rows[0]["monotonic_s"] == 1000.25
    assert rows[0]["elapsed_s"] == 0.25
    assert [row["monotonic_s"] for row in rows] == sorted(row["monotonic_s"] for row in rows)
    assert all(row["elapsed_s"] == row["monotonic_s"] - 1000.0 for row in rows)
