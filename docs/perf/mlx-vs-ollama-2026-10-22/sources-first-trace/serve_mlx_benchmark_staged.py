#!/usr/bin/env python3
"""Owned, experimental eager-per-file loading for the unchanged MLX benchmark.

Only safetensors paths inside an explicitly selected model directory are
affected. On Darwin, F_NOCACHE prevents retaining redundant read-cache pages.
All arrays are evaluated before their source descriptor closes; no arrays,
weights, dtypes, sampling parameters or memory guards are changed. Importing
this module does not import MLX, load a model or start a listener.
"""
from __future__ import annotations

import fcntl
import os
import platform
import sys
from collections.abc import Callable, Iterator, Sequence
from contextlib import suppress
from functools import wraps
from pathlib import Path
from typing import Any, BinaryIO

F_NOCACHE = 48  # Darwin SDK fcntl.h; not exposed by every Python fcntl build.


def darwin_nocache(stream: BinaryIO) -> None:
    if platform.system() != "Darwin":
        raise OSError("The staged benchmark requires Darwin F_NOCACHE")
    fcntl.fcntl(stream.fileno(), F_NOCACHE, 1)


def arrays_only(value: Any, is_array: Callable[[Any], bool]) -> Iterator[Any]:
    """Find arrays in mx.load's dict or (arrays, metadata) return shape."""
    seen: set[int] = set()

    def visit(item: Any) -> Iterator[Any]:
        if id(item) in seen:
            return
        if is_array(item):
            seen.add(id(item))
            yield item
        elif isinstance(item, dict):
            seen.add(id(item))
            for child in item.values():
                yield from visit(child)
        elif isinstance(item, (list, tuple)):
            seen.add(id(item))
            for child in item:
                yield from visit(child)

    yield from visit(value)


def make_staged_load(original_load: Callable[..., Any], *, model_dir: Path,
                     is_array: Callable[[Any], bool], evaluate: Callable[..., Any],
                     clear_cache: Callable[[], Any],
                     open_file: Callable[[Path], Any] | None = None,
                     nocache: Callable[[BinaryIO], None] = darwin_nocache,
                     synchronize: Callable[[], Any] | None = None) -> Callable[..., Any]:
    if not model_dir.is_absolute() or not model_dir.is_dir():
        raise ValueError("Staged loading requires an existing absolute model directory")
    root = model_dir.resolve(strict=True)
    opener = open_file if open_file is not None else lambda path: path.open("rb")

    @wraps(original_load)
    def load(file: Any, *args: Any, **kwargs: Any) -> Any:
        if not isinstance(file, (str, os.PathLike)):
            return original_load(file, *args, **kwargs)
        path = Path(file)
        # Other explicit formats and positional calls retain original behavior;
        # MLX-LM0.32.2's weight loader calls mx.load(path) with no extra args.
        if (args or path.suffix != ".safetensors" or kwargs.get("format") not in {None, "safetensors"}
                or not path.resolve().is_relative_to(root)):
            return original_load(file, *args, **kwargs)
        options = {**kwargs, "format": "safetensors"}
        try:
            with opener(path) as stream:
                nocache(stream)
                result = original_load(stream, **options)
                arrays = list(arrays_only(result, is_array))
                if arrays:
                    evaluate(*arrays)
                    if synchronize is not None:
                        synchronize()
        except BaseException:
            # Preserve the original read/evaluation error even if cache cleanup
            # also fails. The with block has already closed our descriptor.
            with suppress(Exception):
                clear_cache()
            raise
        clear_cache()
        return result

    return load


def model_argument(arguments: Sequence[str]) -> list[str]:
    models = []
    for index, argument in enumerate(arguments):
        if argument == "--model":
            if index + 1 == len(arguments):
                raise ValueError("The base --model argument requires a local directory")
            models.append(arguments[index + 1])
        elif argument.startswith("--model="):
            models.append(argument.partition("=")[2])
    return models


def main(argv: Sequence[str] | None = None) -> None:
    remaining = list(sys.argv[1:] if argv is None else argv)
    selected = model_argument(remaining)
    if len(selected) != 1:
        raise ValueError("Exactly one base --model argument must select the staged model directory")
    model_dir = Path(selected[0])
    if not model_dir.is_absolute() or not model_dir.is_dir():
        raise ValueError("Staged loading requires an existing absolute model directory")
    if platform.system() != "Darwin":
        raise OSError("This staged benchmark wrapper requires Darwin")
    import mlx.core as mx
    import serve_mlx_benchmark as base

    original_load, original_argv = mx.load, sys.argv
    mx.load = make_staged_load(original_load, model_dir=model_dir,
        is_array=lambda value: isinstance(value, mx.array), evaluate=mx.eval,
        clear_cache=mx.clear_cache, synchronize=mx.synchronize)
    sys.argv = ["orb-mlx-staged-benchmark", *remaining]
    try:
        print("Orb benchmark: experimental eager-per-file safetensors loading with Darwin F_NOCACHE; unchanged weights and sampler", flush=True)
        base.main()
    finally:
        mx.load = original_load
        sys.argv = original_argv


if __name__ == "__main__":
    main()
