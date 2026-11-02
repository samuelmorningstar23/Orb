"""Pure fake native limit API tests; no MLX import, model, weights or GPU."""
from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

SPEC = importlib.util.spec_from_file_location("orb_mlx_allocator_budget", Path(__file__).resolve().parents[1] / "scripts/mlx_allocator_budget.py")
assert SPEC is not None and SPEC.loader is not None
budget = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(budget)


def mx_fixture(previous=8 << 30):
    state = {"limit": previous, "calls": []}
    def set_cache_limit(limit):
        old = state["limit"]
        state["limit"] = limit
        state["calls"].append(limit)
        return old
    def forbidden(*args, **kwargs):
        raise AssertionError("No evaluation/clearing/memory/wired-limit mutation")
    return SimpleNamespace(__name__=budget.MODULE_NAME, set_cache_limit=set_cache_limit,
                           eval=forbidden, clear_cache=forbidden, set_memory_limit=forbidden,
                           set_wired_limit=forbidden), state


@pytest.mark.parametrize("previous", [0, 8 << 30, budget.CACHE_LIMIT_BYTES])
def test_sets_exact1gib_and_restores_previous_nonnegative_limit_without_other_allocator_actions(previous):
    mx, state = mx_fixture(previous)
    original, events = mx.set_cache_limit, []
    with budget.install_allocator_cache_budget(mx, events.append):
        assert state["limit"] == 1 << 30 and mx.set_cache_limit is original
        assert state["calls"] == [1 << 30]
    assert state["limit"] == previous and state["calls"] == [1 << 30, previous]
    assert [event["event"] for event in events] == ["mlx_allocator_cache_budget_installed", "mlx_allocator_cache_budget_restored"]
    assert events[0]["active_or_kv_memory_capped"] is False
    assert events[0]["reclamation"] == "on_next_allocation"


def test_nested_scopes_share_one_setting_and_final_idempotent_restore():
    mx, state = mx_fixture()
    first = budget.install_allocator_cache_budget(mx)
    second = budget.install_allocator_cache_budget(mx)
    assert state["calls"] == [1 << 30]
    first.close()
    first.close()
    assert state["limit"] == 1 << 30
    second.close()
    second.close()
    assert state["calls"] == [1 << 30, 8 << 30]


def test_foreign_api_replacement_is_not_called_or_overwritten_and_nested_install_refuses():
    mx, state = mx_fixture()
    handle = budget.install_allocator_cache_budget(mx)
    foreign_calls = []
    def foreign(value):
        foreign_calls.append(value)
        return 0
    mx.set_cache_limit = foreign
    with pytest.raises(budget.AllocatorBudgetRefused, match="foreign"):
        budget.install_allocator_cache_budget(mx)
    with pytest.raises(budget.AllocatorBudgetRefused, match="foreign"):
        handle.close()
    handle.close()
    assert mx.set_cache_limit is foreign and foreign_calls == [] and state["calls"] == [1 << 30]


def test_foreign_limit_change_is_detected_and_preserved_instead_of_clobbered():
    mx, state = mx_fixture()
    handle = budget.install_allocator_cache_budget(mx)
    mx.set_cache_limit(256 << 20)
    with pytest.raises(budget.AllocatorBudgetRefused, match="Foreign cache-limit change preserved"):
        handle.close()
    assert state["limit"] == 256 << 20
    assert state["calls"] == [1 << 30, 256 << 20, 8 << 30, 256 << 20]


@pytest.mark.parametrize("invalid", [None, True, -1, 1.5, "1024"])
def test_invalid_native_return_refused_without_guessing_a_restoration_limit(invalid):
    mx, state = mx_fixture()
    original = mx.set_cache_limit
    def invalid_return(value):
        original(value)
        return invalid
    mx.set_cache_limit = invalid_return
    with pytest.raises(budget.AllocatorBudgetRefused, match="nonnegative integer"):
        budget.install_allocator_cache_budget(mx)
    assert state["calls"] == [1 << 30]
    assert id(mx) not in budget._ACTIVE


def test_optional_emitter_failure_does_not_change_allocator_restoration():
    mx, state = mx_fixture()
    def fail(event):
        raise OSError("synthetic diagnostics failure")
    with budget.install_allocator_cache_budget(mx, fail):
        assert state["limit"] == 1 << 30
    assert state["limit"] == 8 << 30


def test_original_exception_survives_even_if_teardown_detects_foreign_limit():
    mx, state = mx_fixture()
    error = RuntimeError("synthetic primary inference failure")
    with pytest.raises(RuntimeError, match="primary inference") as raised, budget.install_allocator_cache_budget(mx):
        mx.set_cache_limit(0)
        raise error
    assert raised.value is error and state["limit"] == 0
    assert "Foreign cache-limit" in error.__notes__[0]


def test_regular_context_failure_still_restores_limit():
    mx, state = mx_fixture()
    with pytest.raises(RuntimeError, match="original"), budget.install_allocator_cache_budget(mx):
        raise RuntimeError("original")
    assert state["limit"] == 8 << 30


@pytest.mark.parametrize("change", ["module", "setter", "emitter"])
def test_invalid_target_is_rejected_before_api_call(change):
    mx, state = mx_fixture()
    emit = None
    if change == "module":
        mx.__name__ = "other"
    elif change == "setter":
        mx.set_cache_limit = None
    else:
        emit = "not callable"
    with pytest.raises(budget.AllocatorBudgetRefused):
        budget.install_allocator_cache_budget(mx, emit)
    assert state["calls"] == []
