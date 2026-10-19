"""Bounded in-memory inference reuse, keyed by complete caller-supplied inputs."""

from collections import OrderedDict
from collections.abc import Callable, Hashable
from copy import deepcopy
from threading import Lock


class InferenceCache[T]:
    """Serialize identical work; return private copies, never cache failures.

    The producer must not recurse into this cache. No TTL can substitute for a
    complete input/model key, so callers must include both before requesting it.
    """

    def __init__(self, max_entries: int = 128) -> None:
        if max_entries < 1:
            raise ValueError("Cache capacity must be positive")
        self._max_entries = max_entries
        self._values: OrderedDict[Hashable, T] = OrderedDict()
        self._lock = Lock()

    def get_or_compute(self, key: Hashable, produce: Callable[[], T]) -> T:
        with self._lock:
            if key in self._values:
                self._values.move_to_end(key)
                return deepcopy(self._values[key])
            value = produce()
            self._values[key] = deepcopy(value)
            if len(self._values) > self._max_entries:
                self._values.popitem(last=False)
            return value

    def clear(self) -> None:
        with self._lock:
            self._values.clear()
