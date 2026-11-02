"""Staged loader ownership/FD/materialization contract; fake arrays only."""
from __future__ import annotations

import io
import sys
import types
from pathlib import Path
from typing import Any

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import serve_mlx_benchmark_staged as staged


class Array:
    def __init__(self, dtype: str):
        self.dtype = dtype


def test_evaluates_only_arrays_before_fd_close_preserving_object_dtypes_and_kwargs(tmp_path: Path) -> None:
    file = tmp_path / "model.safetensors"
    file.write_bytes(b"synthetic")
    events, opened = [], []
    first, second = Array("uint32"), Array("float16")
    output = ({"weight": first, "nested": [second, first]}, {"metadata": "not an array", "count": 4})

    def opener(path: Path) -> Any:
        assert path == file
        stream = path.open("rb")
        opened.append(stream)
        return stream

    def nocache(stream: Any) -> None:
        assert not stream.closed
        events.append("nocache")

    def original(stream: Any, **kwargs: Any) -> Any:
        assert stream is opened[-1] and not stream.closed
        assert kwargs == {"format": "safetensors", "return_metadata": True}
        events.append("read")
        return output

    def evaluate(*arrays: Any) -> None:
        assert not opened[-1].closed
        assert arrays == (first, second)
        events.append("eval")

    def clear() -> None:
        assert opened[-1].closed
        events.append("clear")

    def synchronize() -> None:
        assert not opened[-1].closed
        events.append("sync")

    load = staged.make_staged_load(original, model_dir=tmp_path,
        is_array=lambda value: isinstance(value, Array), evaluate=evaluate, clear_cache=clear,
        open_file=opener, nocache=nocache, synchronize=synchronize)
    assert load(file, return_metadata=True) is output
    assert events == ["nocache", "read", "eval", "sync", "clear"] and opened[0].closed
    assert first.dtype == "uint32" and second.dtype == "float16"


@pytest.mark.parametrize("kind", ["outside", "sibling_prefix", "other_extension", "explicit_other_format", "file_object", "positional"])
def test_unrelated_loads_are_forwarded_unchanged(tmp_path: Path, kind: str) -> None:
    directory = tmp_path / "model"
    directory.mkdir()
    candidate: Any = directory / "model.safetensors"
    kwargs: dict[str, Any] = {"return_metadata": True}
    args: tuple[Any, ...] = ()
    if kind == "outside":
        candidate = tmp_path / "outside.safetensors"
    elif kind == "sibling_prefix":
        candidate = tmp_path / "model-other/model.safetensors"
    elif kind == "other_extension":
        candidate = directory / "cache.npz"
    elif kind == "explicit_other_format":
        kwargs["format"] = "npz"
    elif kind == "file_object":
        candidate = io.BytesIO(b"existing file handle")
    elif kind == "positional":
        args = ("safetensors",)
    calls = []
    marker = object()

    def original(file: Any, *actual_args: Any, **actual_kwargs: Any) -> Any:
        calls.append((file, actual_args, actual_kwargs))
        return marker

    def forbidden(*_args: Any) -> None:
        raise AssertionError("Unrelated loads must not open, evaluate or clear anything")

    load = staged.make_staged_load(original, model_dir=directory, is_array=lambda _: False,
        evaluate=forbidden, clear_cache=forbidden, open_file=forbidden, nocache=forbidden)
    assert load(candidate, *args, **kwargs) is marker
    assert calls == [(candidate, args, kwargs)]


def test_symlink_escaping_model_directory_is_not_staged(tmp_path: Path) -> None:
    directory = tmp_path / "model"
    directory.mkdir()
    outside = tmp_path / "outside.safetensors"
    outside.write_bytes(b"synthetic")
    link = directory / "model.safetensors"
    link.symlink_to(outside)
    calls = []
    load = staged.make_staged_load(lambda file, **kwargs: calls.append((file, kwargs)), model_dir=directory,
        is_array=lambda _: False, evaluate=lambda *_: pytest.fail("must not evaluate"),
        clear_cache=lambda: pytest.fail("must not clear"), nocache=lambda _: pytest.fail("must not F_NOCACHE"))
    load(link)
    assert calls == [(link, {})]


@pytest.mark.parametrize("failure", ["nocache", "read", "eval", "sync"])
def test_failure_always_closes_our_fd_and_clears_unused_cache(tmp_path: Path, failure: str) -> None:
    file = tmp_path / "model.safetensors"
    file.write_bytes(b"synthetic")
    opened, cleared = [], []

    def opener(path: Path) -> Any:
        stream = path.open("rb")
        opened.append(stream)
        return stream

    def step(name: str) -> None:
        assert not opened[-1].closed
        if failure == name:
            raise RuntimeError("original " + name + " failure")

    def original(_file: Any, **_kwargs: Any) -> Any:
        step("read")
        return {"weight": Array("float32")}

    load = staged.make_staged_load(original, model_dir=tmp_path,
        is_array=lambda value: isinstance(value, Array), evaluate=lambda *_: step("eval"),
        clear_cache=lambda: cleared.append(opened[-1].closed), open_file=opener,
        nocache=lambda _: step("nocache"), synchronize=lambda: step("sync"))
    with pytest.raises(RuntimeError, match="original " + failure):
        load(file)
    assert opened[0].closed and cleared == [True]


def test_array_walk_handles_metadata_cycles_without_evaluating_nonarrays() -> None:
    array = Array("float16")
    metadata: dict[str, Any] = {"text": "metadata", "numeric": 3}
    metadata["cycle"] = metadata
    assert list(staged.arrays_only(({"weight": array, "alias": array}, metadata), lambda value: isinstance(value, Array))) == [array]


def test_cli_requires_one_existing_absolute_base_model_before_mlx_import(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Exactly one base"):
        staged.main([])
    with pytest.raises(ValueError, match="Exactly one base"):
        staged.main(["--model", str(tmp_path), "--model=" + str(tmp_path)])
    with pytest.raises(ValueError, match="existing absolute"):
        staged.main(["--model", str(tmp_path / "missing")])
    with pytest.raises(ValueError, match="existing absolute"):
        staged.main(["--model", "."])


def test_main_forwards_base_arguments_and_restores_wrapper_after_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    original = lambda *_args, **_kwargs: None
    fake_mx = types.ModuleType("mlx.core")
    fake_mx.load = original
    fake_mx.array = Array
    fake_mx.eval = lambda *_args: None
    fake_mx.clear_cache = lambda: None
    fake_mx.synchronize = lambda: None
    package = types.ModuleType("mlx")
    package.core = fake_mx
    fake_base = types.ModuleType("serve_mlx_benchmark")
    arguments = ["--model", str(tmp_path), "--host", "127.0.0.1", "--port", "9991"]
    original_argv = sys.argv

    def base_main() -> None:
        assert sys.argv[1:] == arguments and fake_mx.load is not original
        raise RuntimeError("base startup failed")

    fake_base.main = base_main
    monkeypatch.setitem(sys.modules, "mlx", package)
    monkeypatch.setitem(sys.modules, "mlx.core", fake_mx)
    monkeypatch.setitem(sys.modules, "serve_mlx_benchmark", fake_base)
    monkeypatch.setattr(staged.platform, "system", lambda: "Darwin")
    with pytest.raises(RuntimeError, match="base startup failed"):
        staged.main(arguments)
    assert fake_mx.load is original and sys.argv is original_argv
