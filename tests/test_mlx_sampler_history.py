"""Pure fake tests; no MLX import, arrays, model, inference or network."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SPEC = importlib.util.spec_from_file_location("orb_mlx_sampler_history", Path(__file__).resolve().parents[1] / "scripts/mlx_sampler_history.py")
assert SPEC is not None and SPEC.loader is not None
MODULES_BEFORE = set(sys.modules)
history = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(history)


class Tokens:
    def __init__(self, values):
        self.values = tuple(values)
        self.slices = []

    def __len__(self):
        return len(self.values)

    def __getitem__(self, key):
        assert isinstance(key, slice)
        self.slices.append(key)
        return Tokens(self.values[key])

    def __repr__(self):
        raise AssertionError("No token content in diagnostics")


def server_fixture():
    calls, created, lists = [], [], []

    def make_logits_processors(logit_bias=None, repetition_penalty=None, repetition_context_size=20,
                               presence_penalty=None, presence_context_size=20,
                               frequency_penalty=None, frequency_context_size=20):
        calls.append((logit_bias, repetition_penalty, repetition_context_size,
                      presence_penalty, presence_context_size, frequency_penalty, frequency_context_size))
        processors = []
        if logit_bias:
            def bias(tokens, logits):
                created.append(("bias", tokens, logits))
                for token, value in logit_bias.items():
                    logits[token] += value
                return logits
            processors.append(bias)

        def make_penalty(kind, penalty, context_size):
            def processor(tokens, logits):
                created.append((kind, tokens, logits))
                selected = tokens.values[-context_size:]
                for token in set(selected):
                    if kind == "repeat":
                        logits[token] = logits[token] * penalty if logits[token] < 0 else logits[token] / penalty
                    elif kind == "presence":
                        logits[token] -= penalty
                    else:
                        logits[token] -= penalty * selected.count(token)
                return logits
            return processor

        for kind, penalty, size in (("repeat", repetition_penalty, repetition_context_size),
                                    ("presence", presence_penalty, presence_context_size),
                                    ("frequency", frequency_penalty, frequency_context_size)):
            if penalty is not None and penalty != 0:
                processors.append(make_penalty(kind, penalty, size))
        lists.append(processors)
        return processors

    return SimpleNamespace(__name__=history.MODULE_NAME, make_logits_processors=make_logits_processors,
                           calls=calls, created=created, lists=lists)


def apply(processors, tokens, logits):
    for processor in processors:
        logits = processor(tokens, logits)
    return logits


def test_import_is_pure_and_explicitly_experimental():
    assert history.EXPERIMENTAL and history.NATIVE_HISTORY_PARITY_REQUIRED
    assert not any(name == "mlx" or name.startswith("mlx.") for name in set(sys.modules) - MODULES_BEFORE)


def test_prompt_unpenalized_and_generated_history_preserves_native_slice_type_values_and_sign_math():
    server = server_fixture()
    events = []
    with history.install_generated_only_penalties(server, events.append):
        processors = server.make_logits_processors(repetition_penalty=2, repetition_context_size=256)
        prompt = Tokens([1, 2, 1])
        logits = {1: 8., 2: -8., 3: 4.}
        assert apply(processors, prompt, logits) is logits and logits == {1: 8., 2: -8., 3: 4.}
        later = Tokens([1, 2, 1, 2, 3])
        assert apply(processors, later, logits) is logits
        assert logits == {1: 8., 2: -16., 3: 2.}
        assert [entry[1].values for entry in server.created] == [(), (2, 3)]
        assert prompt.slices == [slice(3, None)] and later.slices == [slice(3, None)]
        assert all(type(entry[1]) is Tokens for entry in server.created)
        assert server.calls == [(None, 2, 256, None, 20, None, 20)]
    assert len(events) == 1 and events[0]["processor_visible_prompt_token_count"] == 3
    assert events[0]["token_values_emitted"] is False


def test_bias_is_identical_and_unscoped_and_all_penalties_share_one_boundary():
    server = server_fixture()
    with history.install_generated_only_penalties(server):
        processors = server.make_logits_processors({1: 3.}, 2., 256, 1., 256, .5, 256)
        assert processors[0] is server.lists[-1][0]
        prompt = Tokens([1, 2])
        logits = {1: 8., 2: 8., 3: 8.}
        apply(processors, prompt, logits)
        assert logits == {1: 11., 2: 8., 3: 8.}
        assert server.created[0][1] is prompt
        assert [item[1].values for item in server.created[1:]] == [(), (), ()]
        server.created.clear()
        apply(processors, Tokens([1, 2, 3, 3]), logits)
        assert [item[1].values for item in server.created[1:]] == [(3, 3)] * 3
        assert logits == {1: 14., 2: 8., 3: 2.}


def test_each_factory_has_independent_boundary_even_when_requests_interleave():
    server = server_fixture()
    with history.install_generated_only_penalties(server):
        a = server.make_logits_processors(repetition_penalty=2)
        b = server.make_logits_processors(repetition_penalty=2)
        apply(a, Tokens([1, 1, 1]), {1: 8.})
        apply(b, Tokens([1]), {1: 8.})
        assert apply(a, Tokens([1, 1, 1, 1]), {1: 8.})[1] == 4.
        assert apply(b, Tokens([1, 1]), {1: 8.})[1] == 4.
        assert [item[1].values for item in server.created] == [(), (), (1,), (1,)]


@pytest.mark.parametrize("penalty", [None, 0, 0.0])
def test_zero_or_absent_penalty_preserves_exact_original_list_and_factory_count(penalty):
    server = server_fixture()
    with history.install_generated_only_penalties(server):
        empty = server.make_logits_processors(repetition_penalty=penalty)
        assert empty is server.lists[-1] and empty == []
        bias_only = server.make_logits_processors(logit_bias={1: 2.}, repetition_penalty=penalty)
        assert bias_only is server.lists[-1] and len(bias_only) == 1
    assert len(server.calls) == 2


def test_one_penalty_identity_is_still_constructed_and_called():
    server = server_fixture()
    with history.install_generated_only_penalties(server):
        processors = server.make_logits_processors(repetition_penalty=1.)
        assert len(processors) == 1 and len(server.calls) == 1
        apply(processors, Tokens([1]), {1: 5.})
        assert apply(processors, Tokens([1, 1]), {1: 5.}) == {1: 5.}
        assert [item[1].values for item in server.created] == [(), (1,)]


def test_downstream_256_context_limit_and_values_are_unchanged():
    server = server_fixture()
    with history.install_generated_only_penalties(server):
        processors = server.make_logits_processors(repetition_penalty=2., repetition_context_size=256)
        apply(processors, Tokens([1, 2]), {1: 8., 2: 8., 3: 8.})
        result = apply(processors, Tokens([1, 2, 1] + [3] * 256), {1: 8., 2: 8., 3: 8.})
        assert len(server.created[-1][1]) == 257  # Adapter does not truncate.
        assert result == {1: 8., 2: 8., 3: 4.}  # Original processor retains 256.


def test_empty_prompt_has_zero_boundary_and_later_generation_is_penalized():
    server = server_fixture()
    with history.install_generated_only_penalties(server):
        processors = server.make_logits_processors(repetition_penalty=2.)
        logits = {1: 8.}
        assert apply(processors, Tokens([]), logits) is logits and logits[1] == 8.
        assert apply(processors, Tokens([1]), logits)[1] == 4.


def test_shrinking_history_fails_closed_and_stays_failed_but_new_request_works():
    server = server_fixture()
    events = []
    with history.install_generated_only_penalties(server, events.append):
        processors = server.make_logits_processors(repetition_penalty=2.)
        apply(processors, Tokens([1, 1]), {1: 8.})
        apply(processors, Tokens([1, 1, 1]), {1: 8.})
        with pytest.raises(history.HistoryPatchRefused, match="shrank"):
            apply(processors, Tokens([1, 1]), {1: 8.})
        with pytest.raises(history.HistoryPatchRefused, match="new per-request"):
            apply(processors, Tokens([1] * 4), {1: 8.})
        assert len(server.created) == 2
        fresh = server.make_logits_processors(repetition_penalty=2.)
        assert apply(fresh, Tokens([1]), {1: 8.}) == {1: 8.}
    assert [event["event"] for event in events] == ["generated_only_penalty_history_first_call",
                                                       "generated_only_penalty_history_refused",
                                                       "generated_only_penalty_history_first_call"]


def test_original_factory_and_processor_errors_propagate_and_diagnostic_errors_are_ignored():
    server = server_fixture()
    with history.install_generated_only_penalties(server, lambda event: (_ for _ in ()).throw(OSError("diagnostic"))):
        processors = server.make_logits_processors(repetition_penalty=2.)
        assert apply(processors, Tokens([1]), {1: 8.}) == {1: 8.}
        with pytest.raises(KeyError):
            apply(processors, Tokens([1, 2]), {1: 8.})
        with pytest.raises(TypeError):
            server.make_logits_processors(unknown=2)


def test_unsliceable_history_fails_without_calling_penalty_and_poisoned_request_requires_new_factory():
    server = server_fixture()
    class BadTokens(Tokens):
        def __getitem__(self, key):
            raise TypeError("synthetic unsupported native slice")
    with history.install_generated_only_penalties(server):
        processors = server.make_logits_processors(repetition_penalty=2.)
        with pytest.raises(TypeError, match="native slice"):
            apply(processors, BadTokens([1]), {1: 8.})
        assert server.created == []
        with pytest.raises(history.HistoryPatchRefused, match="new per-request"):
            apply(processors, Tokens([1]), {1: 8.})


def test_nested_installations_restore_once_and_existing_request_processor_keeps_its_scope():
    server = server_fixture()
    original = server.make_logits_processors
    first = history.install_generated_only_penalties(server)
    wrapper = server.make_logits_processors
    second = history.install_generated_only_penalties(server)
    assert server.make_logits_processors is wrapper
    processors = server.make_logits_processors(repetition_penalty=2.)
    apply(processors, Tokens([1]), {1: 8.})
    first.close()
    first.close()
    assert server.make_logits_processors is wrapper
    second.close()
    second.close()
    assert server.make_logits_processors is original
    assert apply(processors, Tokens([1, 1]), {1: 8.}) == {1: 4.}


def test_foreign_replacement_is_refused_and_not_overwritten():
    server = server_fixture()
    handle = history.install_generated_only_penalties(server)
    foreign = lambda *args, **kwargs: []
    server.make_logits_processors = foreign
    with pytest.raises(history.HistoryPatchRefused, match="foreign"):
        history.install_generated_only_penalties(server)
    handle.close()
    assert server.make_logits_processors is foreign


@pytest.mark.parametrize("change", ["wrong_module", "bad_signature", "bad_emitter", "extra_processor"])
def test_unpinned_layouts_and_invalid_targets_are_refused(change):
    server = server_fixture()
    emit = None
    if change == "wrong_module":
        server.__name__ = "mlx_lm.other"
    elif change == "bad_signature":
        server.make_logits_processors = lambda *args, **kwargs: []
    elif change == "bad_emitter":
        emit = "not callable"
    else:
        original = server.make_logits_processors
        def extra(logit_bias=None, repetition_penalty=None, repetition_context_size=20,
                  presence_penalty=None, presence_context_size=20,
                  frequency_penalty=None, frequency_context_size=20):
            return original(logit_bias, repetition_penalty, repetition_context_size,
                            presence_penalty, presence_context_size, frequency_penalty,
                            frequency_context_size) + [lambda tokens, logits: logits]
        server.make_logits_processors = extra
    with pytest.raises(history.HistoryPatchRefused), history.install_generated_only_penalties(server, emit):
        server.make_logits_processors(repetition_penalty=2.)
