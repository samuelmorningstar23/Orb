"""Orb Hospital OS — the registry that owns every live `VitalsSource`.

One place that knows which sources exist, starts and stops them together, and
can answer "is the ward's vitals feed alive?" without anybody grepping logs. On
an appliance in a hospital basement that last property is the one that matters:
a monitor feed that quietly stopped at 03:00 looks exactly like a ward where
nothing happened.

Failure policy is deliberate and asymmetric:

  * a source that fails to START is logged loudly and skipped — Orb must come up
    even if the monitor VLAN is down, because manual entry is the redundancy
    layer and it lives in the same process;
  * a source that fails to STOP is logged and the rest are still stopped, so one
    stuck socket cannot block shutdown.
"""
from __future__ import annotations

import logging
from typing import Optional

from .base import SourceKind, VitalsSource

logger = logging.getLogger("orb.vitals_source.registry")


class VitalsSourceRegistry:
    """Holds the process's vitals sources and supervises their lifecycle."""

    def __init__(self):
        self._sources: dict[str, VitalsSource] = {}

    # ── registration ──

    def register(self, source: VitalsSource) -> VitalsSource:
        if source.source_id in self._sources:
            raise ValueError(f"vitals source {source.source_id!r} already registered")
        self._sources[source.source_id] = source
        return source

    def unregister(self, source_id: str) -> None:
        self._sources.pop(source_id, None)

    def get(self, source_id: str) -> Optional[VitalsSource]:
        return self._sources.get(source_id)

    def of_kind(self, kind: SourceKind) -> list[VitalsSource]:
        return [s for s in self._sources.values() if s.kind is kind]

    @property
    def sources(self) -> list[VitalsSource]:
        return list(self._sources.values())

    def __len__(self) -> int:
        return len(self._sources)

    def __contains__(self, source_id: object) -> bool:
        return source_id in self._sources

    # ── lifecycle ──

    async def start_all(self) -> list[str]:
        """Start every registered source. Returns the ids that came up."""
        started: list[str] = []
        for source in self._sources.values():
            try:
                await source.start()
                started.append(source.source_id)
            except Exception as e:
                source.stats.note_error(f"start failed: {e}")
                logger.error(
                    "Vitals source %r failed to start (%s). Orb continues without it; "
                    "manual entry remains available for those beds.",
                    source.source_id, e,
                )
        return started

    async def stop_all(self) -> None:
        for source in self._sources.values():
            try:
                await source.stop()
            except Exception as e:
                logger.warning("Vitals source %r failed to stop cleanly: %s",
                               source.source_id, e)

    async def reset(self) -> None:
        """Stop everything and forget it (used by tests and by re-configuration)."""
        await self.stop_all()
        self._sources.clear()

    # ── observability ──

    def status(self) -> dict:
        """Feed health for the admin surface. No PHI."""
        return {
            "count": len(self._sources),
            "sources": [s.status() for s in self._sources.values()],
            "kinds": sorted({s.kind.value for s in self._sources.values()}),
        }


# Process-global registry. Single-process appliance; a multi-worker deployment
# would need each worker to own its own listener port, which is exactly the kind
# of thing that must be decided deliberately rather than inherited by accident —
# see the deployment note in the workstream report.
registry = VitalsSourceRegistry()
