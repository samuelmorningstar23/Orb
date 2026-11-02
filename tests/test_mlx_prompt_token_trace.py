"""Pure fake observations; no model, tokenizer execution, MLX or array eval."""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

SPEC = importlib.util.spec_from_file_location("orb_mlx_prompt_token_trace", Path(__file__).resolve().parents[1] / "scripts/mlx_prompt_token_trace.py")
assert SPEC is not None and SPEC.loader is not None
trace = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(trace)


def server_fixture(result=None):
    if result is None:
        result = ([2, 105, 2430, 7, 106], [[2, 105], [2430, 7], [106]], ["system", "user", "assistant"], "normal", None)
    class ResponseGenerator:
        def __init__(self):
            self.calls = []
        def _tokenize(self, tokenizer, request, args):
            self.calls.append((tokenizer, request, args))
            return result
    return SimpleNamespace(__name__=trace.MODULE_NAME, ResponseGenerator=ResponseGenerator), result


def test_exact_actual_token_hash_concat_and_original_return_identity_are_preserved():
    module, result = server_fixture()
    instance, events = module.ResponseGenerator(), []
    request, tokenizer, args = SimpleNamespace(request_type="chat"), object(), object()
    before = json.dumps(result)
    with trace.install_prompt_token_trace(module, events.append):
        assert instance._tokenize(tokenizer, request, args) is result
    assert instance.calls == [(tokenizer, request, args)] and json.dumps(result) == before
    event = events[0]
    expected = hashlib.sha256(b"[2,105,2430,7,106]").hexdigest()
    assert event["prompt_token_ids_sha256"] == event["segments_concat_token_ids_sha256"] == expected
    assert event["prompt_token_count"] == 5 and event["segment_token_counts"] == [2, 2, 1]
    assert event["segments_concat_matches_prompt"] is True and event["request_type"] == "chat"
    assert event["token_values_emitted"] is False and event["text_emitted"] is False
    assert "2430" not in json.dumps(event)


def test_mismatching_segment_concatenation_is_reported_without_correction_or_mutation():
    module, result = server_fixture(([1, 2], [[1], [3]], [], "normal", None))
    events = []
    with trace.install_prompt_token_trace(module, events.append):
        assert module.ResponseGenerator()._tokenize(None, SimpleNamespace(request_type="completion"), None) is result
    assert events[0]["segments_concat_matches_prompt"] is False
    assert events[0]["prompt_token_ids_sha256"] != events[0]["segments_concat_token_ids_sha256"]


@pytest.mark.parametrize("prompt,segments", [([True], [[True]]), ([-1], [[-1]]), ([trace.VOCAB_SIZE], [[trace.VOCAB_SIZE]]),
                                          ([1], [[1]] * (trace.MAX_SEGMENTS + 1)), ("private text", [[1]])])
def test_bad_vocabulary_or_layout_is_content_free_and_never_fails_generation(prompt, segments):
    module, result = server_fixture((prompt, segments, [], "normal", None))
    events = []
    with trace.install_prompt_token_trace(module, events.append):
        assert module.ResponseGenerator()._tokenize(None, SimpleNamespace(request_type="chat"), None) is result
    assert len(events) == 1
    assert not (events[0]["prompt_tokens_valid"] and events[0]["segments_tokens_valid"])
    assert "private text" not in json.dumps(events)


def test_non_list_arrays_are_never_evaluated_or_represented():
    class HostileArray:
        def __len__(self):
            raise AssertionError("No array length/eval")
        def __iter__(self):
            raise AssertionError("No array iteration/eval")
        def __repr__(self):
            raise AssertionError("No contents")
    module, result = server_fixture((HostileArray(), [HostileArray()], [], "normal", None))
    events = []
    with trace.install_prompt_token_trace(module, events.append):
        assert module.ResponseGenerator()._tokenize(None, SimpleNamespace(request_type="chat"), None) is result
    assert events[0]["prompt_tokens_valid"] is False and events[0]["segments_tokens_valid"] is False


def test_maximum32_events_do_not_skip_original_calls_or_leak_unknown_request_type():
    module, result = server_fixture()
    instance, events = module.ResponseGenerator(), []
    with trace.install_prompt_token_trace(module, events.append):
        for _ in range(40):
            assert instance._tokenize(None, SimpleNamespace(request_type="sensitive arbitrary content"), None) is result
    assert len(instance.calls) == 40 and len(events) == 32
    assert [event["event_sequence"] for event in events] == list(range(1, 33))
    assert all(event["request_type"] == "other" for event in events)


def test_emitter_failure_and_malformed_return_do_not_change_return():
    module, result = server_fixture({"text": "sensitive"})
    events = []
    def failing(event):
        events.append(event)
        raise OSError("synthetic log failure")
    with trace.install_prompt_token_trace(module, failing):
        assert module.ResponseGenerator()._tokenize(None, SimpleNamespace(request_type="chat"), None) is result
    assert events[0]["returned_five_tuple"] is False and "sensitive" not in json.dumps(events)


def test_original_failure_propagates_once_without_exception_or_prompt_contents_in_events():
    module, _ = server_fixture()
    calls, events = [], []
    def failure(self, tokenizer, request, args):
        calls.append(1)
        raise RuntimeError("synthetic private exception")
    module.ResponseGenerator._tokenize = failure
    with pytest.raises(RuntimeError, match="private exception"), trace.install_prompt_token_trace(module, events.append):
        module.ResponseGenerator()._tokenize(None, SimpleNamespace(request_type="chat"), None)
    assert calls == [1] and events == []


def test_nested_idempotent_scopes_share_budget_and_restore_only_owned_method():
    module, _ = server_fixture()
    original = module.ResponseGenerator._tokenize
    first_events, second_events = [], []
    first = trace.install_prompt_token_trace(module, first_events.append)
    wrapper = module.ResponseGenerator._tokenize
    second = trace.install_prompt_token_trace(module, second_events.append)
    assert module.ResponseGenerator._tokenize is wrapper
    module.ResponseGenerator()._tokenize(None, SimpleNamespace(request_type="chat"), None)
    assert len(first_events) == 1 and second_events == []
    first.close()
    first.close()
    assert module.ResponseGenerator._tokenize is wrapper
    second.close()
    second.close()
    assert module.ResponseGenerator._tokenize is original
    handle = trace.install_prompt_token_trace(module, first_events.append)
    foreign = lambda *args, **kwargs: None
    module.ResponseGenerator._tokenize = foreign
    with pytest.raises(trace.PromptTraceRefused, match="foreign"):
        trace.install_prompt_token_trace(module, first_events.append)
    handle.close()
    assert module.ResponseGenerator._tokenize is foreign


@pytest.mark.parametrize("change", ["name", "class", "emitter", "signature"])
def test_unexpected_targets_are_refused(change):
    module, _ = server_fixture()
    emit = lambda event: None
    if change == "name":
        module.__name__ = "other"
    elif change == "class":
        module.ResponseGenerator = None
    elif change == "emitter":
        emit = None
    else:
        module.ResponseGenerator._tokenize = lambda *args, **kwargs: None
    with pytest.raises(trace.PromptTraceRefused):
        trace.install_prompt_token_trace(module, emit)
