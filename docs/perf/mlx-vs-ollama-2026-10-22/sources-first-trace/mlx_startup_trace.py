"""Content-free, observational tracing of one MLX-LM model startup.

Importing this helper never imports MLX, opens weights, evaluates an array or
starts a service. Hooks call the original operations exactly once. Memory
snapshots are observations, not allocation limits or physical-headroom claims.
"""
from __future__ import annotations

import inspect
import os
import re
import resource
import sys
import threading
import time
from collections import Counter
from collections.abc import Callable, Iterator
from functools import wraps
from pathlib import Path
from typing import Any, Self

_AUTO = object()
MAX_TREE_NODES = 10_000
_SAFE_DTYPE = re.compile(r"[A-Za-z0-9_.]{1,64}\Z")
_SAFE_WEIGHT_BASENAME = re.compile(r"model(?:-\d{1,8}(?:-of-\d{1,8})?)?\.safetensors\Z")


def process_max_rss_bytes() -> int:
    """macOS reports ru_maxrss in bytes; other supported Unix hosts use KiB."""
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(value) * (1 if sys.platform == "darwin" else 1024)


def optional_current_rss_reader() -> Callable[[], int] | None:
    """Use psutil only if it is already installed; never install a dependency."""
    try:
        import psutil
        process = psutil.Process(os.getpid())
    except (ImportError, OSError):
        return None
    return lambda: int(process.memory_info().rss)


def _nonnegative(value: Any) -> int | None:
    if type(value) is int and value >= 0:
        return value
    return None


class StartupTrace:
    """Installed hook handle; supports ``with`` and idempotent ``close()``.

    Hooks automatically restore after the first selected ``utils.load_model``
    invocation finishes or raises. Only that invocation's thread is observed.
    No path, tensor name/value, request, prompt or exception message is emitted.
    """

    def __init__(self, mx: Any, nn: Any, utils: Any, emit: Callable[[dict[str, Any]], object], *,
                 clock: Callable[[], float] = time.monotonic,
                 max_rss_reader: Callable[[], int] = process_max_rss_bytes,
                 current_rss_reader: Any = _AUTO, server: Any = None):
        self.mx, self.nn, self.utils, self.emit = mx, nn, utils, emit
        self.clock, self.max_rss_reader = clock, max_rss_reader
        self.current_rss_reader = (optional_current_rss_reader()
                                   if current_rss_reader is _AUTO else current_rss_reader)
        self._hooks: list[tuple[Any, str, Any, Any]] = []
        self._claim_lock = threading.Lock()
        self._owner: int | None = None
        self._claimed = self._closed = False
        self._started: float | None = None
        self._sequence = 0
        self._file_calls = self._file_errors = self._file_bytes = 0
        self._declared_bytes = self._arrays = 0
        self._dtype_arrays: Counter[str] = Counter()
        self._dtype_bytes: Counter[str] = Counter()
        self._largest_file = -1
        self._eval_calls = self._eval_errors = 0
        self._quantize_calls = self._attach_calls = self._constructor_calls = 0
        self.emit_failures = 0
        self._server = server
        self._wired_owner: int | None = None
        self._wired_recommended: int | None = None
        self._model_signature = inspect.signature(utils.load_model)
        self._install()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def _active(self) -> bool:
        return not self._closed and self._owner == threading.get_ident()

    def _snapshot(self) -> dict[str, Any]:
        values: dict[str, Any] = {}
        unavailable = []
        readers = [("mlx_active_bytes", getattr(self.mx, "get_active_memory", None)),
                   ("mlx_cache_bytes", getattr(self.mx, "get_cache_memory", None)),
                   ("mlx_peak_bytes", getattr(self.mx, "get_peak_memory", None)),
                   ("process_max_rss_bytes", self.max_rss_reader),
                   ("process_current_rss_bytes", self.current_rss_reader)]
        for name, reader in readers:
            try:
                value = _nonnegative(reader()) if reader is not None else None
            except Exception:  # noqa: BLE001 - failed diagnostics must not alter model behavior.
                value = None
            values[name] = value
            if value is None:
                unavailable.append(name)
        if unavailable:
            values["unavailable_fields"] = unavailable
        return values

    def _counts(self) -> dict[str, Any]:
        return {"file_load_calls": self._file_calls, "file_load_errors": self._file_errors,
                "declared_file_bytes": self._file_bytes, "declared_array_nbytes": self._declared_bytes,
                "array_occurrences": self._arrays, "dtype_array_counts": dict(self._dtype_arrays),
                "dtype_declared_nbytes": dict(self._dtype_bytes), "eval_calls": self._eval_calls,
                "eval_errors": self._eval_errors, "quantize_calls": self._quantize_calls,
                "weight_attachment_calls": self._attach_calls, "constructor_calls": self._constructor_calls}

    def _event(self, event: str, **fields: Any) -> None:
        """Metrics failures must not change the model's original operation."""
        try:
            now = self.clock()
            self._sequence += 1
            row = {"trace": "mlx_startup", "schema_version": 1, "event": event,
                   "sequence": self._sequence,
                   "elapsed_s": max(0.0, now - self._started) if self._started is not None else None,
                   "memory": self._snapshot(), "counts": self._counts(), **fields}
            self.emit(row)
        except Exception:  # noqa: BLE001 - observer failures cannot fail the original operation.
            self.emit_failures += 1

    def _hook(self, target: Any, name: str, wrapper: Any) -> None:
        original = getattr(target, name)
        setattr(target, name, wrapper)
        self._hooks.append((target, name, original, wrapper))

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        restored = []
        for target, name, original, wrapper in reversed(self._hooks):
            # Preserve any later independent hook rather than overwrite it.
            if getattr(target, name, None) is wrapper:
                setattr(target, name, original)
                restored.append(name)
        self._event("hooks_restored", restored_hook_count=len(restored))

    def _span(self, event: str, operation: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        self._event(event + "_enter")
        try:
            result = operation(*args, **kwargs)
        except BaseException as error:
            self._event(event + "_error", error_type=type(error).__name__)
            raise
        self._event(event + "_exit")
        return result

    def _model_factory(self, factory: Callable[..., Any]) -> Callable[..., Any]:
        @wraps(factory)
        def get_classes(*args: Any, **kwargs: Any) -> Any:
            model_class, argument_class = factory(*args, **kwargs)

            @wraps(model_class)
            def construct(*construct_args: Any, **construct_kwargs: Any) -> Any:
                if not self._active():
                    return model_class(*construct_args, **construct_kwargs)
                self._constructor_calls += 1
                return self._span("model_construct", model_class, *construct_args, **construct_kwargs)

            return construct, argument_class
        return get_classes

    def _array_metadata(self, value: Any) -> None:
        seen: set[int] = set()
        nodes = 0

        def visit(item: Any) -> Iterator[Any]:
            nonlocal nodes
            if id(item) in seen:
                return
            nodes += 1
            if nodes > MAX_TREE_NODES:
                return
            seen.add(id(item))
            if isinstance(item, self.mx.array):
                yield item
            elif isinstance(item, dict):
                for child in item.values():
                    yield from visit(child)
            elif isinstance(item, (tuple, list)):
                for child in item:
                    yield from visit(child)

        try:
            for array in visit(value):
                size = _nonnegative(array.nbytes)
                dtype = str(array.dtype)
                dtype = dtype if _SAFE_DTYPE.fullmatch(dtype) else "other"
                self._arrays += 1
                self._dtype_arrays[dtype] += 1
                if size is not None:
                    self._declared_bytes += size
                    self._dtype_bytes[dtype] += size
        except Exception:  # noqa: BLE001 - metadata-only diagnostics never read values or fail loading.
            self._event("array_metadata_unavailable")

    @staticmethod
    def _file_size(file: Any) -> int | None:
        try:
            if isinstance(file, (str, os.PathLike)):
                return _nonnegative(Path(file).stat().st_size)
            return _nonnegative(os.fstat(file.fileno()).st_size)
        except (AttributeError, OSError, TypeError, ValueError):
            return None

    @staticmethod
    def _file_basename(file: Any) -> str | None:
        try:
            name = file if isinstance(file, (str, os.PathLike)) else getattr(file, "name", None)
            if not isinstance(name, (str, os.PathLike)):
                return None
            basename = Path(name).name
            return basename if _SAFE_WEIGHT_BASENAME.fullmatch(basename) else None
        except (TypeError, ValueError):
            return None

    def _install(self) -> None:
        original_model = self.utils.load_model
        original_load = self.mx.load
        original_quantize = self.nn.quantize
        original_attach = self.nn.Module.load_weights
        original_eval = self.mx.eval

        @wraps(original_model)
        def load_model(*args: Any, **kwargs: Any) -> Any:
            with self._claim_lock:
                selected = not self._closed and not self._claimed
                if selected:
                    self._claimed = True
                    self._owner = threading.get_ident()
                    self._started = self.clock()
            if not selected:
                return original_model(*args, **kwargs)
            try:
                if "get_model_classes" in self._model_signature.parameters:
                    bound = self._model_signature.bind_partial(*args, **kwargs)
                    parameter = self._model_signature.parameters["get_model_classes"]
                    factory = bound.arguments.get("get_model_classes", parameter.default)
                    if callable(factory):
                        bound.arguments["get_model_classes"] = self._model_factory(factory)
                        args, kwargs = bound.args, bound.kwargs
                return self._span("model_load", original_model, *args, **kwargs)
            finally:
                self._owner = None
                self.close()

        @wraps(original_load)
        def load(file: Any, *args: Any, **kwargs: Any) -> Any:
            if not self._active():
                return original_load(file, *args, **kwargs)
            self._file_calls += 1
            size = self._file_size(file)
            if size is not None:
                self._file_bytes += size
            largest = size is not None and size > self._largest_file
            if largest and size is not None:
                self._largest_file = size
            emit_file = self._file_calls == 1 or self._file_calls % 25 == 0 or largest
            fields = {"file_index": self._file_calls, "file_size_bytes": size,
                      "file_basename": self._file_basename(file), "new_largest_file": largest}
            if emit_file:
                self._event("weight_file_enter", **fields)
            try:
                result = original_load(file, *args, **kwargs)
            except BaseException as error:
                self._file_errors += 1
                self._event("weight_file_error", error_type=type(error).__name__, **fields)
                raise
            self._array_metadata(result)
            if emit_file:
                self._event("weight_file_exit", **fields)
            return result

        @wraps(original_quantize)
        def quantize(*args: Any, **kwargs: Any) -> Any:
            if not self._active():
                return original_quantize(*args, **kwargs)
            self._quantize_calls += 1
            return self._span("quantize", original_quantize, *args, **kwargs)

        @wraps(original_attach)
        def attach(model: Any, *args: Any, **kwargs: Any) -> Any:
            if not self._active():
                return original_attach(model, *args, **kwargs)
            self._attach_calls += 1
            return self._span("weight_attach", original_attach, model, *args, **kwargs)

        @wraps(original_eval)
        def evaluate(*args: Any, **kwargs: Any) -> Any:
            if not self._active():
                return original_eval(*args, **kwargs)
            self._eval_calls += 1
            # Bound staged-loader verbosity; first/every25th eval plus summary.
            observe = self._eval_calls == 1 or self._eval_calls % 25 == 0
            if observe:
                self._event("eval_enter", eval_index=self._eval_calls)
            try:
                result = original_eval(*args, **kwargs)
            except BaseException as error:
                self._eval_errors += 1
                self._event("eval_error", eval_index=self._eval_calls, error_type=type(error).__name__)
                raise
            if observe:
                self._event("eval_exit", eval_index=self._eval_calls)
            return result

        try:
            self._hook(self.mx, "load", load)
            self._hook(self.nn, "quantize", quantize)
            self._hook(self.nn.Module, "load_weights", attach)
            self._hook(self.mx, "eval", evaluate)
            self._hook(self.utils, "load_model", load_model)
            if self._server is not None and callable(getattr(self._server, "maybe_set_recommended_wired_limit", None)):
                original_wired = self._server.maybe_set_recommended_wired_limit
                original_info = getattr(self.mx, "device_info", None)

                if callable(original_info):
                    @wraps(original_info)
                    def device_info(*args: Any, **kwargs: Any) -> Any:
                        result = original_info(*args, **kwargs)
                        if self._wired_owner == threading.get_ident() and isinstance(result, dict):
                            self._wired_recommended = _nonnegative(result.get("max_recommended_working_set_size"))
                        return result
                    self._hook(self.mx, "device_info", device_info)

                @wraps(original_wired)
                def wired_limit(*args: Any, **kwargs: Any) -> Any:
                    self._wired_owner = threading.get_ident()
                    self._wired_recommended = None
                    self._event("wired_limit_enter")
                    try:
                        result = original_wired(*args, **kwargs)
                    except BaseException as error:
                        self._event("wired_limit_error", error_type=type(error).__name__)
                        raise
                    else:
                        self._event("wired_limit_exit", previous_wired_limit_bytes=_nonnegative(result),
                                    observed_recommended_working_set_bytes=self._wired_recommended)
                        return result
                    finally:
                        self._wired_owner = None
                self._hook(self._server, "maybe_set_recommended_wired_limit", wired_limit)
        except BaseException:
            self.close()
            raise


def install_startup_trace(mx: Any, nn: Any, utils: Any,
                          emit: Callable[[dict[str, Any]], object], **options: Any) -> StartupTrace:
    """Install immediately; return a closable context handle that restores itself.

    Optional clock/max_rss_reader/current_rss_reader injection is for CPU-only
    tests. Snapshots never force evaluation or synchronize a device. Array byte
    counts are declared logical sizes, not unique allocated-buffer footprints.
    """
    return StartupTrace(mx, nn, utils, emit, **options)
