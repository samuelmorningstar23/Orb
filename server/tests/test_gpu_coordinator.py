"""Tests for Orb GPU Coordinator — VramGuard & MetalQueueCoordinator (UMSQ)

Validates:
  1. VramGuard executes safely with/without PyTorch/MLX (graceful no-ops)
  2. VramGuard calls cleanup functions when available (mocked)
  3. UMSQ serializes concurrent tasks — no overlap
  4. UMSQ respects priority ordering — P1 (Sage) always runs before P3 (Background)
  5. UMSQ handles exceptions without deadlocking
  6. Priority constants are exported correctly
"""
import os
import sys
import gc
import time
import asyncio

import pytest

# Add server root to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from services.gpu_coordinator import (
    vram_guard,
    MetalQueueCoordinator,
    umsq,
    PRIORITY_SAGE,
    PRIORITY_SCRIBE,
    PRIORITY_BACKGROUND,
)


# ══════════════════════════════════════════════════════════════════════
# VramGuard Tests
# ══════════════════════════════════════════════════════════════════════

class TestVramGuard:
    """Test the VramGuard context manager in isolation."""

    def test_basic_execution(self):
        """VramGuard wraps a block and exits cleanly."""
        result = None
        with vram_guard("test-basic"):
            result = 42
        assert result == 42

    def test_gc_collect_called(self, monkeypatch):
        """VramGuard calls gc.collect() on exit."""
        calls = []
        original_collect = gc.collect
        monkeypatch.setattr(gc, "collect", lambda: calls.append("gc") or original_collect())

        with vram_guard("test-gc"):
            pass

        assert "gc" in calls, "gc.collect() was not called"

    def test_mps_cache_flush_called(self, monkeypatch):
        """VramGuard calls torch.mps.empty_cache() if available."""
        calls = []

        class MockMPS:
            @staticmethod
            def empty_cache():
                calls.append("mps_flush")

        class MockTorch:
            mps = MockMPS()
            class backends:
                class mps:
                    @staticmethod
                    def is_available():
                        return False

        # Save and replace torch in sys.modules
        saved = sys.modules.get("torch")
        sys.modules["torch"] = MockTorch()

        try:
            with vram_guard("test-mps"):
                pass
            assert "mps_flush" in calls, "torch.mps.empty_cache() was not called"
        finally:
            if saved is not None:
                sys.modules["torch"] = saved
            else:
                sys.modules.pop("torch", None)

    def test_mlx_cache_flush_called(self, monkeypatch):
        """VramGuard calls mx.metal.clear_cache() if available."""
        calls = []

        class MockMetal:
            @staticmethod
            def clear_cache():
                calls.append("mlx_flush")

        class MockMLXCore:
            metal = MockMetal()

        # Save and replace mlx modules
        saved_mlx = sys.modules.get("mlx")
        saved_mlx_core = sys.modules.get("mlx.core")
        sys.modules["mlx"] = type(sys)("mlx")
        sys.modules["mlx.core"] = MockMLXCore()

        try:
            with vram_guard("test-mlx"):
                pass
            assert "mlx_flush" in calls, "mx.metal.clear_cache() was not called"
        finally:
            if saved_mlx is not None:
                sys.modules["mlx"] = saved_mlx
            else:
                sys.modules.pop("mlx", None)
            if saved_mlx_core is not None:
                sys.modules["mlx.core"] = saved_mlx_core
            else:
                sys.modules.pop("mlx.core", None)

    def test_exception_propagation(self):
        """VramGuard re-raises exceptions from the wrapped block."""
        with pytest.raises(ValueError, match="test error"):
            with vram_guard("test-exception"):
                raise ValueError("test error")

    def test_cleanup_runs_on_exception(self, monkeypatch):
        """VramGuard still runs cleanup even when the block raises."""
        calls = []
        original_collect = gc.collect
        monkeypatch.setattr(gc, "collect", lambda: calls.append("gc") or original_collect())

        with pytest.raises(RuntimeError):
            with vram_guard("test-cleanup-on-error"):
                raise RuntimeError("boom")

        assert "gc" in calls, "gc.collect() should run even on exception"

    def test_no_crash_without_torch_or_mlx(self):
        """VramGuard completes cleanly when neither PyTorch nor MLX is installed."""
        saved_torch = sys.modules.pop("torch", None)
        saved_mlx = sys.modules.pop("mlx", None)
        saved_mlx_core = sys.modules.pop("mlx.core", None)

        # Block imports by setting to None
        sys.modules["torch"] = None
        sys.modules["mlx"] = None
        sys.modules["mlx.core"] = None

        try:
            with vram_guard("test-no-frameworks"):
                x = 1 + 1
            assert x == 2
        finally:
            if saved_torch is not None:
                sys.modules["torch"] = saved_torch
            else:
                sys.modules.pop("torch", None)
            if saved_mlx is not None:
                sys.modules["mlx"] = saved_mlx
            else:
                sys.modules.pop("mlx", None)
            if saved_mlx_core is not None:
                sys.modules["mlx.core"] = saved_mlx_core
            else:
                sys.modules.pop("mlx.core", None)


# ══════════════════════════════════════════════════════════════════════
# MetalQueueCoordinator (UMSQ) Tests — use asyncio.run() directly
# ══════════════════════════════════════════════════════════════════════

class TestMetalQueueCoordinator:
    """Test the priority-aware async GPU lock."""

    def _new_coordinator(self):
        """Fresh coordinator for each test (not the global singleton)."""
        return MetalQueueCoordinator()

    def test_basic_acquire_release(self):
        """Single task can acquire and release the GPU."""
        coord = self._new_coordinator()

        async def _test():
            async with coord.acquire("test-task", priority=PRIORITY_BACKGROUND):
                assert coord.is_busy
                assert coord.active_task == "test-task"
            assert not coord.is_busy

        asyncio.run(_test())

    def test_serialization(self):
        """Two concurrent tasks are serialized — no overlap."""
        coord = self._new_coordinator()
        execution_log = []

        async def gpu_task(name, priority, delay=0.05):
            await asyncio.sleep(delay * 0.1)  # Small stagger
            async with coord.acquire(name, priority=priority):
                execution_log.append(f"{name}:start")
                await asyncio.sleep(delay)
                execution_log.append(f"{name}:end")

        async def _test():
            await asyncio.gather(
                gpu_task("A", PRIORITY_BACKGROUND, delay=0.05),
                gpu_task("B", PRIORITY_BACKGROUND, delay=0.05),
            )

        asyncio.run(_test())

        a_start = execution_log.index("A:start")
        a_end = execution_log.index("A:end")
        b_start = execution_log.index("B:start")
        b_end = execution_log.index("B:end")

        assert (a_end < b_start) or (b_end < a_start), (
            f"GPU tasks overlapped! Log: {execution_log}"
        )

    def test_priority_ordering(self):
        """Higher priority tasks (lower number) execute before lower priority ones."""
        coord = self._new_coordinator()
        execution_order = []

        async def blocking_task():
            async with coord.acquire("blocker", priority=PRIORITY_BACKGROUND):
                await asyncio.sleep(0.1)

        async def queued_task(name, priority):
            await asyncio.sleep(0.02)  # Let blocker acquire first
            async with coord.acquire(name, priority=priority):
                execution_order.append(name)

        async def _test():
            await asyncio.gather(
                blocking_task(),
                queued_task("sage", PRIORITY_SAGE),
                queued_task("scribe", PRIORITY_SCRIBE),
                queued_task("vigil", PRIORITY_BACKGROUND),
            )

        asyncio.run(_test())

        assert execution_order[0] == "sage", (
            f"Sage (P1) should execute first but got: {execution_order}"
        )
        assert execution_order[1] == "scribe", (
            f"Scribe (P2) should execute second but got: {execution_order}"
        )
        assert execution_order[2] == "vigil", (
            f"Vigil (P3) should execute last but got: {execution_order}"
        )

    def test_exception_releases_lock(self):
        """Exception inside the acquired context releases the GPU — no deadlock."""
        coord = self._new_coordinator()

        async def _test():
            with pytest.raises(RuntimeError, match="gpu exploded"):
                async with coord.acquire("failing-task"):
                    raise RuntimeError("gpu exploded")

            assert not coord.is_busy, "GPU should be released after exception"

            # Another task should be able to acquire normally
            async with coord.acquire("recovery-task"):
                assert coord.is_busy
            assert not coord.is_busy

        asyncio.run(_test())

    def test_properties(self):
        """is_busy and active_task reflect correct state."""
        coord = self._new_coordinator()

        async def _test():
            assert not coord.is_busy
            assert coord.active_task == ""

            async with coord.acquire("my-task", priority=PRIORITY_SAGE):
                assert coord.is_busy
                assert coord.active_task == "my-task"

            assert not coord.is_busy
            assert coord.active_task == ""

        asyncio.run(_test())


# ══════════════════════════════════════════════════════════════════════
# Priority Constants Tests
# ══════════════════════════════════════════════════════════════════════

class TestPriorityConstants:
    """Verify priority constants are correctly defined."""

    def test_sage_highest(self):
        assert PRIORITY_SAGE < PRIORITY_SCRIBE < PRIORITY_BACKGROUND

    def test_sage_value(self):
        assert PRIORITY_SAGE == 1

    def test_scribe_value(self):
        assert PRIORITY_SCRIBE == 2

    def test_background_value(self):
        assert PRIORITY_BACKGROUND == 3


# ══════════════════════════════════════════════════════════════════════
# Singleton Tests
# ══════════════════════════════════════════════════════════════════════

class TestSingleton:
    """Verify the global singleton is a proper MetalQueueCoordinator."""

    def test_umsq_is_coordinator(self):
        assert isinstance(umsq, MetalQueueCoordinator)

    def test_umsq_acquire(self):
        """Global singleton can be acquired and released."""
        async def _test():
            async with umsq.acquire("singleton-test", priority=PRIORITY_BACKGROUND):
                assert umsq.is_busy
            assert not umsq.is_busy

        asyncio.run(_test())
